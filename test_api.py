from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from db import get_connection
from seed import seed_database

UTC = timezone.utc


@pytest.fixture
def api(tmp_path):
    database_path = tmp_path / "reliefmatch_api.sqlite3"
    app = create_app(database_path, session_secret="test-session-secret")
    app.config.update(TESTING=True)

    connection = get_connection(database_path)
    connection.executemany(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
        [
            ("coordinator", generate_password_hash("coord-password"), "coordinator"),
            ("volunteer", generate_password_hash("vol-password"), "volunteer"),
        ],
    )
    connection.commit()
    connection.close()
    return app.test_client(), database_path


def login(client, username="coordinator", password="coord-password"):
    return client.post(
        "/api/login",
        json={"username": username, "password": password},
    )


def insert_request(
    database_path,
    *,
    request_type="water",
    people=10,
    severity=3,
    lat=10.79,
    lon=78.69,
    status="pending",
    created_at=None,
):
    connection = get_connection(database_path)
    now = datetime.now(UTC)
    created_at = created_at or now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    expires_at = (now + timedelta(hours=24)).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    cursor = connection.execute(
        """
        INSERT INTO requests
            (type, people, severity, lat, lon, status, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (request_type, people, severity, lat, lon, status, created_at, expires_at),
    )
    request_id = cursor.lastrowid
    connection.commit()
    connection.close()
    return request_id


def insert_resource(
    database_path,
    *,
    resource_type="water",
    quantity=20,
    lat=10.79,
    lon=78.69,
    provider="Test aid team",
):
    connection = get_connection(database_path)
    cursor = connection.execute(
        """
        INSERT INTO resources (type, quantity, lat, lon, provider)
        VALUES (?, ?, ?, ?, ?)
        """,
        (resource_type, quantity, lat, lon, provider),
    )
    resource_id = cursor.lastrowid
    connection.commit()
    connection.close()
    return resource_id


def get_scalar(database_path, sql, params=()):
    connection = get_connection(database_path)
    value = connection.execute(sql, params).fetchone()[0]
    connection.close()
    return value


def test_login_logout_and_passwords_are_hashed(api):
    client, database_path = api

    bad_login = login(client, password="incorrect")
    assert bad_login.status_code == 401
    assert login(client).status_code == 200
    assert client.get("/api/requests").status_code == 200
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/requests").status_code == 401
    assert get_scalar(
        database_path,
        "SELECT COUNT(*) FROM audit_log WHERE action = 'login'",
    ) == 2
    assert get_scalar(
        database_path,
        "SELECT COUNT(*) FROM audit_log WHERE action = 'logout'",
    ) == 1
    stored_hash = get_scalar(
        database_path,
        "SELECT password_hash FROM users WHERE username = 'coordinator'",
    )
    assert stored_hash != "coord-password"
    assert stored_hash.startswith("scrypt:")


def test_volunteer_can_create_requests_but_cannot_perform_coordinator_actions(api):
    client, database_path = api
    assert login(client, "volunteer", "vol-password").status_code == 200
    created = client.post(
        "/api/requests",
        json={"type": "water", "people": 5, "severity": 3, "lat": 10.79, "lon": 78.69},
    )
    assert created.status_code == 201
    request_id = created.get_json()["id"]
    resource_id = insert_resource(database_path, quantity=10)

    responses = [
        client.post(f"/api/matches/{request_id}/approve"),
        client.post(f"/api/matches/{request_id}/reject"),
        client.post(
            f"/api/matches/{request_id}/override",
            json={"resource_id": resource_id},
        ),
        client.post(f"/api/matches/{request_id}/undo"),
        client.post(f"/api/matches/{request_id}/deliver"),
    ]
    assert [response.status_code for response in responses] == [403] * 5
    assert client.get("/api/audit").status_code == 403
    assert get_scalar(
        database_path,
        "SELECT status FROM requests WHERE id = ?",
        (request_id,),
    ) == "pending"
    assert get_scalar(
        database_path,
        "SELECT quantity FROM resources WHERE id = ?",
        (resource_id,),
    ) == 10


def test_request_creation_sets_server_times_and_detects_nearby_duplicates(api):
    client, database_path = api
    assert login(client, "volunteer", "vol-password").status_code == 200
    payload = {"type": "food", "people": 4, "severity": 2, "lat": 10.79, "lon": 78.69}

    created = client.post("/api/requests", json=payload)
    assert created.status_code == 201
    result = created.get_json()
    created_at = datetime.fromisoformat(result["created_at"].replace("Z", "+00:00"))
    expires_at = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00"))
    assert expires_at - created_at == timedelta(hours=24)
    assert result["status"] == "pending"

    duplicate_payload = {**payload, "lat": 10.7905}
    duplicate = client.post("/api/requests", json=duplicate_payload)
    assert duplicate.status_code == 409
    assert get_scalar(database_path, "SELECT COUNT(*) FROM requests") == 1


def test_request_creation_is_rate_limited_to_ten_per_minute(api):
    client, _database_path = api
    assert login(client, "volunteer", "vol-password").status_code == 200
    statuses = []
    for index in range(11):
        statuses.append(
            client.post(
                "/api/requests",
                json={
                    "type": "water",
                    "people": 2,
                    "severity": 1,
                    "lat": 10.79 + index * 0.01,
                    "lon": 78.69,
                },
            ).status_code
        )
    assert statuses == [201] * 10 + [429]


def test_get_requests_and_urgency_sorted_suggestions(api):
    client, database_path = api
    login(client)
    now = datetime.now(UTC)
    older = (now - timedelta(hours=1)).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    newer = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    low_id = insert_request(
        database_path, people=10, severity=1, created_at=older, lat=10.79, lon=78.69
    )
    high_id = insert_request(
        database_path, people=10, severity=5, created_at=newer, lat=10.80, lon=78.69
    )
    insert_resource(database_path, quantity=3)

    request_list = client.get("/api/requests")
    suggestions = client.get("/api/suggestions")

    assert request_list.status_code == 200
    assert {item["id"] for item in request_list.get_json()} == {low_id, high_id}
    ordered = suggestions.get_json()
    assert [item["id"] for item in ordered] == [high_id, low_id]
    assert all("urgency" in item and "matches" in item and "shortfall" in item for item in ordered)
    assert all(item["shortfall"] > 0 for item in ordered)
    assert all(
        set(match) == {"resource_id", "quantity", "distance_km"}
        for item in ordered
        for match in item["matches"]
    )


def test_approve_reduces_stock_and_prevents_double_allocation(api):
    client, database_path = api
    login(client)
    resource_id = insert_resource(database_path, quantity=10)
    first_request = insert_request(database_path, people=7)
    second_request = insert_request(database_path, people=6, lat=10.80)

    approved = client.post(f"/api/matches/{first_request}/approve")
    blocked = client.post(f"/api/matches/{second_request}/approve")

    assert approved.status_code == 201
    assert blocked.status_code == 409
    assert blocked.get_json()["shortfall"] == 3
    assert get_scalar(
        database_path, "SELECT quantity FROM resources WHERE id = ?", (resource_id,)
    ) == 3
    assert get_scalar(
        database_path, "SELECT COUNT(*) FROM matches WHERE request_id = ?", (second_request,)
    ) == 0


def test_undo_restores_stock_and_cancels_matches(api):
    client, database_path = api
    login(client)
    resource_id = insert_resource(database_path, quantity=10)
    request_id = insert_request(database_path, people=7)
    assert client.post(f"/api/matches/{request_id}/approve").status_code == 201
    assert get_scalar(
        database_path, "SELECT quantity FROM resources WHERE id = ?", (resource_id,)
    ) == 3

    undone = client.post(f"/api/matches/{request_id}/undo")

    assert undone.status_code == 200
    assert get_scalar(
        database_path, "SELECT quantity FROM resources WHERE id = ?", (resource_id,)
    ) == 10
    assert get_scalar(
        database_path, "SELECT status FROM matches WHERE request_id = ?", (request_id,)
    ) == "cancelled"
    assert get_scalar(
        database_path, "SELECT status FROM requests WHERE id = ?", (request_id,)
    ) == "pending"


def test_override_uses_an_alternate_resource_inside_approval_flow(api):
    client, database_path = api
    login(client)
    recommended_id = insert_resource(
        database_path, quantity=20, lat=10.7901, lon=78.69
    )
    alternate_id = insert_resource(
        database_path, quantity=15, lat=10.81, lon=78.69
    )
    request_id = insert_request(database_path, people=8)

    response = client.post(
        f"/api/matches/{request_id}/override",
        json={"resource_id": alternate_id},
    )

    assert response.status_code == 201
    assert response.get_json()["matches"][0]["resource_id"] == alternate_id
    assert get_scalar(
        database_path, "SELECT quantity FROM resources WHERE id = ?", (alternate_id,)
    ) == 7
    assert get_scalar(
        database_path, "SELECT quantity FROM resources WHERE id = ?", (recommended_id,)
    ) == 20
    assert get_scalar(
        database_path, "SELECT status FROM requests WHERE id = ?", (request_id,)
    ) == "matched"


def test_reject_is_audited_without_adding_an_unsupported_status(api):
    client, database_path = api
    login(client)
    request_id = insert_request(database_path, people=3)

    response = client.post(f"/api/matches/{request_id}/reject")

    assert response.status_code == 200
    assert response.get_json()["status"] == "pending"
    assert get_scalar(
        database_path, "SELECT status FROM requests WHERE id = ?", (request_id,)
    ) == "pending"
    assert get_scalar(
        database_path,
        "SELECT COUNT(*) FROM audit_log WHERE action = 'reject' AND \"user\" = 'coordinator'",
    ) == 1


def test_invalid_status_transitions_are_blocked(api):
    client, database_path = api
    login(client)
    insert_resource(database_path, quantity=20)
    request_id = insert_request(database_path, people=5)

    assert client.post(f"/api/matches/{request_id}/deliver").status_code == 409
    assert client.post(f"/api/matches/{request_id}/undo").status_code == 409
    assert client.post(f"/api/matches/{request_id}/approve").status_code == 201
    assert client.post(f"/api/matches/{request_id}/approve").status_code == 409
    assert client.post(f"/api/matches/{request_id}/deliver").status_code == 200
    assert client.post(f"/api/matches/{request_id}/deliver").status_code == 409
    assert client.post(f"/api/matches/{request_id}/undo").status_code == 409
    assert get_scalar(
        database_path, "SELECT status FROM requests WHERE id = ?", (request_id,)
    ) == "delivered"


def test_audit_log_records_all_required_action_types(api):
    client, database_path = api
    login(client)
    resources = [
        insert_resource(database_path, quantity=20, lat=10.7901),
        insert_resource(database_path, quantity=20, lat=10.82),
    ]
    request_ids = []
    for index in range(5):
        response = client.post(
            "/api/requests",
            json={
                "type": "water",
                "people": 5,
                "severity": 3,
                "lat": 10.75 + index * 0.015,
                "lon": 78.69,
            },
        )
        assert response.status_code == 201
        request_ids.append(response.get_json()["id"])

    assert client.post(f"/api/matches/{request_ids[0]}/reject").status_code == 200
    assert client.post(f"/api/matches/{request_ids[1]}/approve").status_code == 201
    assert client.post(f"/api/matches/{request_ids[1]}/undo").status_code == 200
    assert client.post(
        f"/api/matches/{request_ids[2]}/override",
        json={"resource_id": resources[1]},
    ).status_code == 201
    assert client.post(f"/api/matches/{request_ids[3]}/approve").status_code == 201
    assert client.post(f"/api/matches/{request_ids[3]}/deliver").status_code == 200

    audit_response = client.get("/api/audit")
    assert audit_response.status_code == 200
    actions = {row["action"] for row in audit_response.get_json()}
    assert {
        "login",
        "request_created",
        "approve",
        "reject",
        "override",
        "undo",
        "deliver",
    }.issubset(actions)
    assert get_scalar(
        database_path, "SELECT COUNT(*) FROM audit_log WHERE action = 'login'"
    ) >= 1


def test_seed_creates_expected_demo_dataset_and_is_idempotent(tmp_path):
    database_path = tmp_path / "seed_test.sqlite3"

    result = seed_database(database_path)

    assert result == {"seeded": True, "users": 2, "requests": 20, "resources": 10}
    connection = get_connection(database_path)
    assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 20
    assert connection.execute("SELECT COUNT(*) FROM resources").fetchone()[0] == 10
    request_types = {
        row[0] for row in connection.execute("SELECT DISTINCT type FROM requests")
    }
    resource_types = {
        row[0] for row in connection.execute("SELECT DISTINCT type FROM resources")
    }
    assert request_types == resource_types == {"water", "food", "medicine", "shelter"}
    assert connection.execute(
        """
        SELECT COUNT(*) FROM requests AS r
        WHERE r.people > (
            SELECT MAX(x.quantity) FROM resources AS x WHERE x.type = r.type
        )
        """
    ).fetchone()[0] > 0
    coordinator_hash = connection.execute(
        "SELECT password_hash FROM users WHERE username = 'coordinator'"
    ).fetchone()[0]
    assert coordinator_hash != "relief123"
    connection.close()

    assert seed_database(database_path) == {
        "seeded": False,
        "reason": "database_not_empty",
    }
