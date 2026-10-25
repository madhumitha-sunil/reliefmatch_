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


def test_role_gated_pages_and_local_assets_render(api):
    client, _database_path = api

    assert client.get("/login").status_code == 200
    assert client.get("/dashboard").headers["Location"].endswith("/login")
    assert login(client, "volunteer", "vol-password").status_code == 200
    assert client.get("/request").status_code == 200
    assert client.get("/dashboard").headers["Location"].endswith("/request")
    assert client.get("/static/css/reliefmatch.css").status_code == 200

    assert client.post("/api/logout").status_code == 200
    assert login(client).status_code == 200
    assert client.get("/dashboard").status_code == 200
    assert client.get("/request").headers["Location"].endswith("/dashboard")


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
        database_path, "SELECT matched_at FROM requests WHERE id = ?", (first_request,)
    )
    listed = client.get("/api/requests").get_json()
    assert next(item for item in listed if item["id"] == first_request)["matched_at"]
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
    assert get_scalar(
        database_path, "SELECT matched_at FROM requests WHERE id = ?", (request_id,)
    ) is None


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
    resource_id = insert_resource(database_path, quantity=8)
    request_id = insert_request(database_path, people=3)

    response = client.post(f"/api/matches/{request_id}/reject")

    assert response.status_code == 200
    assert response.get_json()["status"] == "pending"
    assert response.get_json()["resource_id"] == resource_id
    assert get_scalar(
        database_path, "SELECT status FROM requests WHERE id = ?", (request_id,)
    ) == "pending"
    assert get_scalar(
        database_path,
        "SELECT COUNT(*) FROM audit_log WHERE action = 'reject' AND \"user\" = 'coordinator'",
    ) == 1


def test_rejected_resource_is_skipped_by_later_suggestions(api):
    client, database_path = api
    login(client)
    nearest_id = insert_resource(database_path, quantity=30, lat=10.7901)
    alternate_id = insert_resource(database_path, quantity=30, lat=10.82)
    request_id = insert_request(database_path, people=8)

    first_suggestion = client.get("/api/suggestions").get_json()[0]
    assert first_suggestion["matches"][0]["resource_id"] == nearest_id

    rejected = client.post(
        f"/api/matches/{request_id}/reject",
        json={"resource_id": nearest_id},
    )
    next_suggestion = client.get("/api/suggestions").get_json()[0]

    assert rejected.status_code == 200
    assert next_suggestion["matches"][0]["resource_id"] == alternate_id
    assert nearest_id not in {item["resource_id"] for item in next_suggestion["matches"]}
    assert get_scalar(
        database_path,
        "SELECT COUNT(*) FROM rejections WHERE request_id = ? AND resource_id = ?",
        (request_id, nearest_id),
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


# ---------------------------------------------------------------------------
# client_uuid: duplicate-safe request creation (offline queue sync)
# ---------------------------------------------------------------------------

UUID_PAYLOAD = {
    "type": "water",
    "people": 12,
    "severity": 4,
    "lat": 10.79,
    "lon": 78.69,
    "client_uuid": "a1b2c3d4-0001-4000-8000-000000000001",
}


def test_client_uuid_is_stored_on_first_submit(api):
    client, database_path = api
    login(client, "volunteer", "vol-password")

    response = client.post("/api/requests", json=UUID_PAYLOAD)

    assert response.status_code == 201
    assert response.get_json()["already_received"] is False
    assert get_scalar(
        database_path, "SELECT client_uuid FROM requests WHERE id = ?", (response.get_json()["id"],)
    ) == UUID_PAYLOAD["client_uuid"]


def test_repeat_client_uuid_returns_already_received_without_new_row(api):
    client, database_path = api
    login(client, "volunteer", "vol-password")

    first = client.post("/api/requests", json=UUID_PAYLOAD)
    second = client.post("/api/requests", json=UUID_PAYLOAD)
    third = client.post("/api/requests", json=UUID_PAYLOAD)

    assert first.status_code == 201
    assert second.status_code == 200
    assert third.status_code == 200
    assert second.get_json()["already_received"] is True
    assert second.get_json()["id"] == first.get_json()["id"]
    assert get_scalar(database_path, "SELECT COUNT(*) FROM requests") == 1


def test_repeat_client_uuid_does_not_hit_rate_limit_or_nearby_duplicate_check(api):
    client, database_path = api
    login(client, "volunteer", "vol-password")
    first = client.post("/api/requests", json=UUID_PAYLOAD)
    assert first.status_code == 201

    # 15 re-sends of the same request: more than the 10-per-minute limit, and the
    # request sits at the same spot as itself (nearby-duplicate rule).
    for _ in range(15):
        again = client.post("/api/requests", json=UUID_PAYLOAD)
        assert again.status_code == 200
        assert again.get_json()["already_received"] is True
    assert get_scalar(database_path, "SELECT COUNT(*) FROM requests") == 1


def test_requests_without_client_uuid_still_work_as_before(api):
    client, database_path = api
    login(client, "volunteer", "vol-password")
    payload = {k: v for k, v in UUID_PAYLOAD.items() if k != "client_uuid"}

    first = client.post("/api/requests", json=payload)
    second = client.post("/api/requests", json={**payload, "lat": 11.5})

    assert first.status_code == 201
    assert second.status_code == 201
    assert get_scalar(database_path, "SELECT COUNT(*) FROM requests") == 2
    assert get_scalar(
        database_path, "SELECT COUNT(*) FROM requests WHERE client_uuid IS NULL"
    ) == 2


def test_different_client_uuids_create_separate_requests(api):
    client, database_path = api
    login(client, "volunteer", "vol-password")

    first = client.post("/api/requests", json=UUID_PAYLOAD)
    second = client.post(
        "/api/requests",
        json={**UUID_PAYLOAD, "lat": 11.5, "client_uuid": "a1b2c3d4-0002-4000-8000-000000000002"},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.get_json()["id"] != second.get_json()["id"]


def test_invalid_client_uuid_is_rejected(api):
    client, database_path = api
    login(client, "volunteer", "vol-password")

    for bad in ["short", "has spaces in it!!", "x" * 65, 12345, ""]:
        response = client.post("/api/requests", json={**UUID_PAYLOAD, "client_uuid": bad})
        assert response.status_code == 400, bad
    assert get_scalar(database_path, "SELECT COUNT(*) FROM requests") == 0


def test_unique_index_blocks_duplicate_uuid_even_if_code_is_bypassed(api):
    _client, database_path = api
    connection = get_connection(database_path)
    sql = (
        "INSERT INTO requests (type, people, severity, lat, lon, expires_at, client_uuid) "
        "VALUES ('water', 5, 3, 10.8, 78.7, '2099-01-01T00:00:00.000Z', ?)"
    )
    connection.execute(sql, ("same-uuid-0001",))
    with pytest.raises(Exception) as error:
        connection.execute(sql, ("same-uuid-0001",))
    assert "UNIQUE" in str(error.value)
    connection.close()


def test_migration_adds_client_uuid_to_an_existing_old_database(tmp_path):
    import sqlite3
    from db import initialize_db

    old_path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(old_path)
    old.executescript(
        """
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY,
            type TEXT NOT NULL, people INTEGER NOT NULL, severity INTEGER NOT NULL,
            lat REAL NOT NULL, lon REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT 'x',
            matched_at TEXT,
            expires_at TEXT NOT NULL
        );
        INSERT INTO requests (type, people, severity, lat, lon, expires_at)
        VALUES ('water', 8, 3, 10.8, 78.7, '2099-01-01T00:00:00.000Z'),
               ('food', 3, 2, 10.9, 78.8, '2099-01-01T00:00:00.000Z');
        """
    )
    old.commit()
    old.close()

    initialize_db(old_path)
    initialize_db(old_path)  # running twice must be safe

    connection = get_connection(old_path)
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(requests)")}
    assert "client_uuid" in columns
    assert connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 2
    assert connection.execute(
        "SELECT COUNT(*) FROM requests WHERE client_uuid IS NULL"
    ).fetchone()[0] == 2
    connection.close()


def test_request_page_loads_offline_queue_script_and_status_bar(api):
    client, _database_path = api
    login(client, "volunteer", "vol-password")

    page = client.get("/request")

    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'id="queue-status"' in html
    assert "js/queue.js" in html
    assert "js/request.js" in html
    # queue.js must load before request.js, which depends on it
    assert html.index("js/queue.js") < html.index("js/request.js")
    assert client.get("/static/js/queue.js").status_code == 200


# ---------------------------------------------------------------------------
# Installable app (PWA): manifest, service worker, icons
# ---------------------------------------------------------------------------


def test_manifest_is_valid_and_all_its_icons_exist(api):
    import json
    import struct

    client, _database_path = api

    response = client.get("/manifest.webmanifest")

    assert response.status_code == 200
    assert response.mimetype == "application/manifest+json"
    manifest = json.loads(response.get_data(as_text=True))
    assert manifest["name"] == "ReliefMatch"
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(icon["purpose"] == "maskable" for icon in manifest["icons"])
    for icon in manifest["icons"]:
        image_response = client.get(icon["src"])
        assert image_response.status_code == 200, icon["src"]
        # PNG header: 8-byte signature, then the IHDR chunk holding width and height.
        assert image_response.data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", image_response.data[16:24])
        assert f"{width}x{height}" == icon["sizes"]


def test_service_worker_is_served_from_site_root_and_never_cached_by_the_browser(api):
    client, _database_path = api

    response = client.get("/sw.js")

    assert response.status_code == 200
    assert "javascript" in response.mimetype
    assert response.headers["Service-Worker-Allowed"] == "/"
    assert "no-cache" in response.headers["Cache-Control"]
    script = response.get_data(as_text=True)
    assert 'startsWith("/api/")' in script  # live data is never intercepted
    assert 'request.method !== "GET"' in script  # submissions are never intercepted


def test_every_page_links_manifest_icon_and_registers_the_service_worker(api):
    client, _database_path = api

    login_page = client.get("/login").get_data(as_text=True)
    assert 'rel="manifest"' in login_page
    assert "apple-touch-icon" in login_page
    assert "js/pwa.js" in login_page

    login(client, "volunteer", "vol-password")
    assert "js/pwa.js" in client.get("/request").get_data(as_text=True)
    client.post("/api/logout")
    login(client)
    assert "js/pwa.js" in client.get("/dashboard").get_data(as_text=True)


def test_everything_the_service_worker_precaches_actually_exists(api):
    import re

    client, _database_path = api
    script = client.get("/sw.js").get_data(as_text=True)
    block = script[script.index("const PRECACHE"):script.index("];", script.index("const PRECACHE"))]
    paths = re.findall(r'"(/static/[^"]+)"', block)

    assert len(paths) >= 6
    for path in paths:
        assert client.get(path).status_code == 200, path
