import pytest

from db import get_connection, initialize_db
from logic import calculate_urgency, haversine_distance_km, match_request, validate_request


@pytest.fixture
def connection(tmp_path):
    database_path = tmp_path / "reliefmatch_test.sqlite3"
    initialize_db(database_path)
    conn = get_connection(database_path)
    yield conn
    conn.close()


def insert_request(connection, *, request_type="water", people=10, severity=3, lat=13.08, lon=80.27):
    cursor = connection.execute(
        """
        INSERT INTO requests (type, people, severity, lat, lon, expires_at)
        VALUES (?, ?, ?, ?, ?, '2099-01-01T00:00:00Z')
        """,
        (request_type, people, severity, lat, lon),
    )
    return cursor.lastrowid


def insert_resource(connection, *, request_type="water", quantity=10, lat=13.08, lon=80.27):
    cursor = connection.execute(
        """
        INSERT INTO resources (type, quantity, lat, lon, provider)
        VALUES (?, ?, ?, ?, 'Test provider')
        """,
        (request_type, quantity, lat, lon),
    )
    return cursor.lastrowid


def test_urgency_formula_and_caps():
    assert calculate_urgency("medicine", people=100, severity=4, wait_hours=5) == pytest.approx(1200)
    assert calculate_urgency("water", people=1500, severity=2, wait_hours=48) == pytest.approx(
        16666.6666667
    )
    assert calculate_urgency("shelter", people=6, severity=1, wait_hours=0) == pytest.approx(1)


def test_haversine_distance_in_kilometers():
    assert haversine_distance_km(0, 0, 0, 1) == pytest.approx(111.195, abs=0.01)
    assert haversine_distance_km(13.08, 80.27, 13.08, 80.27) == 0


def test_matching_chooses_nearest_sufficient_resource(connection):
    request_id = insert_request(connection, people=8)
    farther_id = insert_resource(connection, quantity=20, lat=13.2, lon=80.27)
    nearest_id = insert_resource(connection, quantity=8, lat=13.09, lon=80.27)

    matches = match_request(connection, request_id)

    assert len(matches) == 1
    assert matches[0]["resource_id"] == nearest_id
    assert matches[0]["resource_id"] != farther_id
    assert matches[0]["quantity"] == 8
    assert connection.execute(
        "SELECT status FROM requests WHERE id = ?", (request_id,)
    ).fetchone()["status"] == "matched"


def test_partial_fulfillment_splits_across_nearest_resources(connection):
    request_id = insert_request(connection, people=10)
    first_id = insert_resource(connection, quantity=4, lat=13.081, lon=80.27)
    second_id = insert_resource(connection, quantity=6, lat=13.09, lon=80.27)
    insert_resource(connection, quantity=50, request_type="food")

    matches = match_request(connection, request_id)

    assert [(item["resource_id"], item["quantity"]) for item in matches] == [
        (first_id, 4),
        (second_id, 6),
    ]
    assert connection.execute(
        "SELECT status FROM requests WHERE id = ?", (request_id,)
    ).fetchone()["status"] == "matched"


def test_partial_fulfillment_when_total_inventory_is_insufficient(connection):
    request_id = insert_request(connection, people=10)
    first_id = insert_resource(connection, quantity=3)
    second_id = insert_resource(connection, quantity=2, lat=13.09, lon=80.27)

    matches = match_request(connection, request_id)

    assert sum(item["quantity"] for item in matches) == 5
    assert {item["resource_id"] for item in matches} == {first_id, second_id}
    assert connection.execute(
        "SELECT status FROM requests WHERE id = ?", (request_id,)
    ).fetchone()["status"] == "partially_fulfilled"


@pytest.mark.parametrize(
    "fields",
    [
        ("clothing", 10, 3, 13.08, 80.27),
        ("water", 10, 0, 13.08, 80.27),
        ("water", 10, 6, 13.08, 80.27),
        ("water", 0, 3, 13.08, 80.27),
        ("water", -1, 3, 13.08, 80.27),
        ("water", 10, 3, 7.99, 80.27),
        ("water", 10, 3, 13.61, 80.27),
        ("water", 10, 3, 13.08, 75.99),
        ("water", 10, 3, 13.08, 80.41),
    ],
)
def test_validation_rejects_invalid_request_fields(fields):
    with pytest.raises(ValueError):
        validate_request(*fields)


@pytest.mark.parametrize(
    "fields",
    [
        ("water", 1, 1, 8, 76),
        ("medicine", 5, 5, 13.6, 80.4),
    ],
)
def test_validation_accepts_tamil_nadu_bounds(fields):
    assert validate_request(*fields) is None


def test_database_uses_wal_and_server_timestamp(connection):
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    request_id = insert_request(connection)
    created_at = connection.execute(
        "SELECT created_at FROM requests WHERE id = ?", (request_id,)
    ).fetchone()["created_at"]
    assert created_at.endswith("Z")
    assert created_at


def test_repeated_matching_does_not_duplicate_allocations(connection):
    request_id = insert_request(connection, people=7)
    insert_resource(connection, quantity=4)
    insert_resource(connection, quantity=3, lat=13.09, lon=80.27)

    first_result = match_request(connection, request_id)
    second_result = match_request(connection, request_id)

    assert len(first_result) == len(second_result) == 2
    assert sum(item["quantity"] for item in second_result) == 7


def test_matching_rejects_missing_request(connection):
    with pytest.raises(ValueError, match="does not exist"):
        match_request(connection, 999)
