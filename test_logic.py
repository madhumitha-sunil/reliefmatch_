import sqlite3

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from db import get_connection, initialize_db
from logic import (
    calculate_urgency,
    haversine_distance_km,
    suggest_match,
    validate_request,
)


@pytest.fixture
def connection(tmp_path):
    database_path = tmp_path / "reliefmatch_test.sqlite3"
    initialize_db(database_path)
    conn = get_connection(database_path)
    yield conn
    conn.close()


@pytest.fixture
def flask_client(tmp_path):
    database_path = tmp_path / "reliefmatch_api_test.sqlite3"
    app = create_app(database_path, session_secret="test-session-secret")
    connection = get_connection(database_path)
    connection.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
        ("coordinator", generate_password_hash("test-password"), "coordinator"),
    )
    connection.commit()
    connection.close()
    client = app.test_client()
    response = client.post(
        "/api/login",
        json={"username": "coordinator", "password": "test-password"},
    )
    assert response.status_code == 200
    return client, database_path


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

    matches = suggest_match(connection, request_id)

    assert len(matches) == 1
    assert matches[0]["resource_id"] == nearest_id
    assert matches[0]["resource_id"] != farther_id
    assert matches[0]["quantity"] == 8
    assert matches[0]["distance_km"] == pytest.approx(
        haversine_distance_km(13.08, 80.27, 13.09, 80.27)
    )
    assert matches.shortfall == 0


def test_suggestion_splits_across_nearest_resources(connection):
    request_id = insert_request(connection, people=10)
    first_id = insert_resource(connection, quantity=4, lat=13.081, lon=80.27)
    second_id = insert_resource(connection, quantity=6, lat=13.09, lon=80.27)
    insert_resource(connection, quantity=50, request_type="food")

    matches = suggest_match(connection, request_id)

    assert [(item["resource_id"], item["quantity"]) for item in matches] == [
        (first_id, 4),
        (second_id, 6),
    ]
    assert all(set(item) == {"resource_id", "quantity", "distance_km"} for item in matches)
    assert matches.shortfall == 0


def test_suggestion_reports_insufficient_total_inventory(connection):
    request_id = insert_request(connection, people=10)
    first_id = insert_resource(connection, quantity=3)
    second_id = insert_resource(connection, quantity=2, lat=13.09, lon=80.27)

    matches = suggest_match(connection, request_id)

    assert sum(item["quantity"] for item in matches) == 5
    assert {item["resource_id"] for item in matches} == {first_id, second_id}
    assert matches.shortfall == 5


def test_suggest_match_is_read_only_and_uses_resource_quantity_as_stock(connection):
    request_id = insert_request(connection, people=7)
    resource_id = insert_resource(connection, quantity=8)
    connection.execute(
        """
        INSERT INTO matches (request_id, resource_id, quantity, status)
        VALUES (?, ?, 5, 'active')
        """,
        (request_id, resource_id),
    )
    connection.commit()

    before_changes = connection.total_changes
    before_request = tuple(
        connection.execute(
            "SELECT status, people FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
    )
    before_resource_quantity = connection.execute(
        "SELECT quantity FROM resources WHERE id = ?", (resource_id,)
    ).fetchone()["quantity"]
    before_matches = connection.execute(
        "SELECT COUNT(*) FROM matches WHERE request_id = ?", (request_id,)
    ).fetchone()[0]

    suggestions = suggest_match(connection, request_id)

    assert suggestions == [
        {
            "resource_id": resource_id,
            "quantity": 7,
            "distance_km": 0.0,
        }
    ]
    assert suggestions.shortfall == 0
    assert connection.total_changes == before_changes
    assert tuple(
        connection.execute(
            "SELECT status, people FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
    ) == before_request
    assert connection.execute(
        "SELECT quantity FROM resources WHERE id = ?", (resource_id,)
    ).fetchone()["quantity"] == before_resource_quantity
    assert connection.execute(
        "SELECT COUNT(*) FROM matches WHERE request_id = ?", (request_id,)
    ).fetchone()[0] == before_matches


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


def test_suggestion_rejects_missing_request(connection):
    with pytest.raises(ValueError, match="does not exist"):
        suggest_match(connection, 999)


def test_approve_endpoint_reserves_stock_and_sets_matched(flask_client):
    client, database_path = flask_client
    connection = get_connection(database_path)
    request_id = insert_request(connection, people=10)
    nearest_id = insert_resource(connection, quantity=4, lat=13.081, lon=80.27)
    farther_id = insert_resource(connection, quantity=6, lat=13.09, lon=80.27)
    connection.commit()
    connection.close()

    response = client.post(f"/requests/{request_id}/approve")

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["status"] == "matched"
    assert payload["shortfall"] == 0
    assert [(item["resource_id"], item["quantity"]) for item in payload["matches"]] == [
        (nearest_id, 4),
        (farther_id, 6),
    ]
    connection = get_connection(database_path)
    assert connection.execute(
        "SELECT status FROM requests WHERE id = ?", (request_id,)
    ).fetchone()["status"] == "matched"
    assert [
        row["quantity"]
        for row in connection.execute(
            "SELECT quantity FROM resources ORDER BY id"
        ).fetchall()
    ] == [0, 0]
    assert {
        row["status"]
        for row in connection.execute(
            "SELECT status FROM matches WHERE request_id = ?", (request_id,)
        ).fetchall()
    } == {"active"}
    connection.close()


def test_approve_endpoint_rejects_shortfall_without_writes(flask_client):
    client, database_path = flask_client
    connection = get_connection(database_path)
    request_id = insert_request(connection, people=10)
    resource_id = insert_resource(connection, quantity=6)
    connection.commit()
    connection.close()

    response = client.post(f"/requests/{request_id}/approve")

    assert response.status_code == 409
    assert response.get_json()["shortfall"] == 4
    connection = get_connection(database_path)
    assert connection.execute(
        "SELECT status FROM requests WHERE id = ?", (request_id,)
    ).fetchone()["status"] == "pending"
    assert connection.execute(
        "SELECT quantity FROM resources WHERE id = ?", (resource_id,)
    ).fetchone()["quantity"] == 6
    assert connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0
    connection.close()


def test_undo_endpoint_cancels_matches_restores_stock_and_resets_request(flask_client):
    client, database_path = flask_client
    connection = get_connection(database_path)
    request_id = insert_request(connection, people=7)
    resource_id = insert_resource(connection, quantity=10)
    connection.commit()
    connection.close()

    approve_response = client.post(f"/requests/{request_id}/approve")
    assert approve_response.status_code == 201

    undo_response = client.post(f"/requests/{request_id}/undo")

    assert undo_response.status_code == 200
    assert undo_response.get_json()["status"] == "pending"
    connection = get_connection(database_path)
    assert connection.execute(
        "SELECT status FROM requests WHERE id = ?", (request_id,)
    ).fetchone()["status"] == "pending"
    assert connection.execute(
        "SELECT quantity FROM resources WHERE id = ?", (resource_id,)
    ).fetchone()["quantity"] == 10
    assert connection.execute(
        "SELECT status FROM matches WHERE request_id = ?", (request_id,)
    ).fetchone()["status"] == "cancelled"
    connection.close()


def test_database_rejects_unsupported_request_and_match_statuses(connection):
    request_id = insert_request(connection)
    resource_id = insert_resource(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE requests SET status = 'partially_fulfilled' WHERE id = ?",
            (request_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO matches (request_id, resource_id, quantity, status)
            VALUES (?, ?, 1, 'proposed')
            """,
            (request_id, resource_id),
        )
