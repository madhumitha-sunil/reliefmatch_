"""Flask API and server-rendered pages for ReliefMatch."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from db import DEFAULT_DB_PATH, get_connection, initialize_db
from logic import (
    calculate_urgency,
    haversine_distance_km,
    suggest_match,
    validate_request,
)

UTC = timezone.utc
REQUEST_TTL = timedelta(hours=24)
REQUEST_RATE_LIMIT = 10
REQUEST_RATE_WINDOW = timedelta(minutes=1)
DUPLICATE_WINDOW = timedelta(minutes=30)
DUPLICATE_RADIUS_KM = 0.2


CLIENT_UUID_PATTERN = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _write_audit(
    connection: sqlite3.Connection,
    username: str,
    action: str,
    details: dict[str, Any],
) -> None:
    connection.execute(
        'INSERT INTO audit_log ("user", action, details) VALUES (?, ?, ?)',
        (username, action, json.dumps(details, sort_keys=True, separators=(",", ":"))),
    )


def _audit_result(
    connection: sqlite3.Connection,
    username: str,
    action: str,
    success: bool,
    **details: Any,
) -> None:
    _write_audit(connection, username, action, {"success": success, **details})


def _logged_in_user() -> sqlite3.Row | None:
    user_id = session.get("user_id")
    if not isinstance(user_id, int):
        return None
    return g.db.execute(
        "SELECT id, username, role FROM users WHERE id = ?", (user_id,)
    ).fetchone()


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        user = _logged_in_user()
        if user is None:
            session.clear()
            return jsonify({"error": "Authentication required"}), 401
        g.user = user
        return view(*args, **kwargs)

    return wrapped


def coordinator_required(action: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any):
            user = _logged_in_user()
            if user is None:
                session.clear()
                g.db.execute("BEGIN IMMEDIATE")
                _audit_result(g.db, "anonymous", action, False, reason="unauthenticated")
                g.db.commit()
                return jsonify({"error": "Authentication required"}), 401
            g.user = user
            if user["role"] != "coordinator":
                g.db.execute("BEGIN IMMEDIATE")
                _audit_result(
                    g.db, user["username"], action, False, reason="coordinator_required"
                )
                g.db.commit()
                return jsonify({"error": "Coordinator role required"}), 403
            return view(*args, **kwargs)

        return wrapped

    return decorator


def _json_error(error: str, status: int):
    return jsonify({"error": error}), status


def _record_action_failure(
    connection: sqlite3.Connection,
    username: str,
    action: str,
    reason: str,
    **details: Any,
):
    _audit_result(connection, username, action, False, reason=reason, **details)
    connection.commit()


def _request_row(connection: sqlite3.Connection, request_id: int):
    return connection.execute(
        """
        SELECT id, type, people, severity, lat, lon, status, created_at, matched_at, expires_at
        FROM requests WHERE id = ?
        """,
        (request_id,),
    ).fetchone()


def _apply_allocations(
    connection: sqlite3.Connection,
    request_row: sqlite3.Row,
    allocations: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    """Recheck and apply stock changes; caller owns the immediate transaction."""
    if sum(item["quantity"] for item in allocations) != request_row["people"]:
        return False, "allocation_does_not_fulfill_request"

    for allocation in allocations:
        resource = connection.execute(
            "SELECT type, quantity FROM resources WHERE id = ?",
            (allocation["resource_id"],),
        ).fetchone()
        if resource is None:
            return False, "resource_not_found"
        if resource["type"] != request_row["type"]:
            return False, "resource_type_mismatch"
        if resource["quantity"] < allocation["quantity"]:
            return False, "insufficient_stock"

    for allocation in allocations:
        changed = connection.execute(
            """
            UPDATE resources
            SET quantity = quantity - ?
            WHERE id = ? AND quantity >= ?
            """,
            (
                allocation["quantity"],
                allocation["resource_id"],
                allocation["quantity"],
            ),
        )
        if changed.rowcount != 1:
            return False, "insufficient_stock"
        connection.execute(
            """
            INSERT INTO matches (request_id, resource_id, quantity, status)
            VALUES (?, ?, ?, 'active')
            """,
            (request_row["id"], allocation["resource_id"], allocation["quantity"]),
        )

    changed = connection.execute(
        """
        UPDATE requests
        SET status = 'matched', matched_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ? AND status = 'pending'
        """,
        (request_row["id"],),
    )
    if changed.rowcount != 1:
        return False, "invalid_status_transition"
    return True, None


def create_app(
    database_path: str | Path = DEFAULT_DB_PATH,
    session_secret: str | None = None,
) -> Flask:
    app = Flask(__name__)
    secret = session_secret or os.environ.get("SESSION_SECRET")
    if not secret:
        raise RuntimeError("SESSION_SECRET must be configured before starting ReliefMatch")
    app.config.update(
        SECRET_KEY=secret,
        DATABASE_PATH=str(database_path),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    initialize_db(database_path)

    @app.before_request
    def open_database():
        g.db = get_connection(app.config["DATABASE_PATH"])

    @app.teardown_appcontext
    def close_database(_error: BaseException | None = None):
        connection = g.pop("db", None)
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    @app.errorhandler(404)
    def not_found(_error):
        return _json_error("Not found", 404)

    @app.errorhandler(405)
    def method_not_allowed(_error):
        return _json_error("Method not allowed", 405)

    @app.errorhandler(500)
    def internal_error(_error):
        return _json_error("Internal server error", 500)

    @app.get("/")
    def home_page():
        user = _logged_in_user()
        if user is None:
            return redirect(url_for("login_page"))
        return redirect(url_for("dashboard_page" if user["role"] == "coordinator" else "request_page"))

    @app.get("/login")
    def login_page():
        user = _logged_in_user()
        if user is not None:
            return redirect(
                url_for("dashboard_page" if user["role"] == "coordinator" else "request_page")
            )
        return render_template("login.html")

    @app.get("/request")
    def request_page():
        user = _logged_in_user()
        if user is None:
            return redirect(url_for("login_page"))
        if user["role"] != "volunteer":
            return redirect(url_for("dashboard_page"))
        return render_template("request.html")

    @app.get("/dashboard")
    def dashboard_page():
        user = _logged_in_user()
        if user is None:
            return redirect(url_for("login_page"))
        if user["role"] != "coordinator":
            return redirect(url_for("request_page"))
        return render_template("dashboard.html", username=user["username"])

    @app.post("/api/login")
    def login():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            body = {}
        username = body.get("username")
        password = body.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            username = username if isinstance(username, str) else "anonymous"
            g.db.execute("BEGIN IMMEDIATE")
            _audit_result(g.db, username, "login", False, reason="invalid_payload")
            g.db.commit()
            return _json_error("username and password are required", 400)

        username = username.strip()
        g.db.execute("BEGIN IMMEDIATE")
        user = g.db.execute(
            "SELECT id, username, password_hash, role FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        valid = bool(user and check_password_hash(user["password_hash"], password))
        _audit_result(g.db, username or "anonymous", "login", valid)
        g.db.commit()
        if not valid:
            return _json_error("Invalid username or password", 401)

        session.clear()
        session["user_id"] = user["id"]
        return jsonify({"user": {"username": user["username"], "role": user["role"]}})

    @app.post("/api/logout")
    def logout():
        username = "anonymous"
        user = _logged_in_user()
        if user is not None:
            username = user["username"]
        g.db.execute("BEGIN IMMEDIATE")
        _audit_result(g.db, username, "logout", user is not None)
        g.db.commit()
        session.clear()
        return jsonify({"status": "logged_out"})

    @app.post("/api/requests")
    @login_required
    def create_request():
        now = datetime.now(UTC)
        created_at = _timestamp(now)
        expires_at = _timestamp(now + REQUEST_TTL)
        one_minute_ago = _timestamp(now - REQUEST_RATE_WINDOW)
        thirty_minutes_ago = _timestamp(now - DUPLICATE_WINDOW)

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            body = {}
        client_uuid = body.get("client_uuid")
        if client_uuid is not None and not (
            isinstance(client_uuid, str) and CLIENT_UUID_PATTERN.match(client_uuid)
        ):
            return _json_error("client_uuid must be 8-64 letters, digits or hyphens", 400)

        g.db.execute("BEGIN IMMEDIATE")
        if client_uuid is not None:
            # A repeat UUID means the phone is re-sending a request we already hold
            # (for example after a dropped connection). Return the original, create nothing.
            existing = g.db.execute(
                """
                SELECT id, type, people, severity, lat, lon, status, created_at, expires_at
                FROM requests WHERE client_uuid = ?
                """,
                (client_uuid,),
            ).fetchone()
            if existing is not None:
                g.db.rollback()
                return jsonify({**dict(existing), "already_received": True}), 200
        request_count = g.db.execute(
            """
            SELECT COUNT(*) FROM audit_log
            WHERE "user" = ? AND action = 'request_attempt' AND timestamp >= ?
            """,
            (g.user["username"], one_minute_ago),
        ).fetchone()[0]
        if request_count >= REQUEST_RATE_LIMIT:
            _audit_result(
                g.db,
                g.user["username"],
                "request_attempt",
                False,
                reason="rate_limit_exceeded",
            )
            g.db.commit()
            return _json_error("Request rate limit exceeded", 429)
        _audit_result(g.db, g.user["username"], "request_attempt", True)

        try:
            request_type = body.get("type")
            people = body.get("people")
            severity = body.get("severity")
            lat = body.get("lat")
            lon = body.get("lon")
            validate_request(request_type, people, severity, lat, lon)
        except (TypeError, ValueError) as error:
            _audit_result(
                g.db,
                g.user["username"],
                "request_created",
                False,
                reason="invalid_request",
            )
            g.db.commit()
            return _json_error(str(error), 400)

        recent_requests = g.db.execute(
            """
            SELECT lat, lon FROM requests
            WHERE type = ? AND created_at >= ?
            """,
            (request_type, thirty_minutes_ago),
        ).fetchall()
        duplicate = any(
            haversine_distance_km(lat, lon, row["lat"], row["lon"])
            <= DUPLICATE_RADIUS_KM
            for row in recent_requests
        )
        if duplicate:
            _audit_result(
                g.db,
                g.user["username"],
                "request_created",
                False,
                reason="duplicate_nearby",
            )
            g.db.commit()
            return _json_error("A similar request was created nearby in the last 30 minutes", 409)

        cursor = g.db.execute(
            """
            INSERT INTO requests
                (type, people, severity, lat, lon, status, created_at, expires_at, client_uuid)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (request_type, people, severity, lat, lon, created_at, expires_at, client_uuid),
        )
        request_id = cursor.lastrowid
        _audit_result(
            g.db,
            g.user["username"],
            "request_created",
            True,
            request_id=request_id,
            type=request_type,
        )
        g.db.commit()
        return (
            jsonify(
                {
                    "id": request_id,
                    "type": request_type,
                    "people": people,
                    "severity": severity,
                    "lat": lat,
                    "lon": lon,
                    "status": "pending",
                    "created_at": created_at,
                    "expires_at": expires_at,
                    "already_received": False,
                }
            ),
            201,
        )

    @app.get("/api/requests")
    @login_required
    def list_requests():
        rows = g.db.execute(
            """
            SELECT id, type, people, severity, lat, lon, status, created_at, matched_at, expires_at
            FROM requests ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.get("/api/suggestions")
    @login_required
    def list_suggestions():
        now = datetime.now(UTC)
        pending = g.db.execute(
            """
            SELECT id, type, people, severity, lat, lon, status, created_at, matched_at, expires_at
            FROM requests WHERE status = 'pending'
            """
        ).fetchall()
        suggestions = []
        for row in pending:
            wait_hours = max(0.0, (now - _parse_timestamp(row["created_at"])).total_seconds() / 3600)
            matches = suggest_match(g.db, row["id"])
            suggestions.append(
                {
                    **dict(row),
                    "urgency": calculate_urgency(
                        row["type"], row["people"], row["severity"], wait_hours
                    ),
                    "matches": list(matches),
                    "shortfall": matches.shortfall,
                }
            )
        suggestions.sort(key=lambda item: (-item["urgency"], item["id"]))
        return jsonify(suggestions)

    @app.get("/api/resources")
    @login_required
    def list_resources():
        rows = g.db.execute(
            """
            SELECT id, type, quantity, lat, lon, provider
            FROM resources ORDER BY type, id
            """
        ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.post("/api/matches/<int:request_id>/approve")
    @app.post("/requests/<int:request_id>/approve")
    @coordinator_required("approve")
    def approve_request(request_id: int):
        g.db.execute("BEGIN IMMEDIATE")
        row = _request_row(g.db, request_id)
        if row is None:
            _record_action_failure(g.db, g.user["username"], "approve", "request_not_found")
            return _json_error("Request not found", 404)
        if row["status"] != "pending":
            _record_action_failure(
                g.db, g.user["username"], "approve", "invalid_status", status=row["status"]
            )
            return _json_error("Only pending requests can be approved", 409)

        suggestions = suggest_match(g.db, request_id)
        if suggestions.shortfall:
            _record_action_failure(
                g.db,
                g.user["username"],
                "approve",
                "insufficient_stock",
                shortfall=suggestions.shortfall,
            )
            return jsonify({"error": "Insufficient stock", "shortfall": suggestions.shortfall}), 409

        success, reason = _apply_allocations(g.db, row, list(suggestions))
        if not success:
            _record_action_failure(g.db, g.user["username"], "approve", reason or "allocation_failed")
            return jsonify({"error": "Could not approve request", "reason": reason}), 409
        _audit_result(
            g.db,
            g.user["username"],
            "approve",
            True,
            request_id=request_id,
            matches=list(suggestions),
        )
        g.db.commit()
        return (
            jsonify(
                {
                    "request_id": request_id,
                    "status": "matched",
                    "matches": list(suggestions),
                    "shortfall": 0,
                }
            ),
            201,
        )

    @app.post("/api/matches/<int:request_id>/reject")
    @coordinator_required("reject")
    def reject_match(request_id: int):
        g.db.execute("BEGIN IMMEDIATE")
        row = _request_row(g.db, request_id)
        if row is None:
            _record_action_failure(g.db, g.user["username"], "reject", "request_not_found")
            return _json_error("Request not found", 404)
        if row["status"] != "pending":
            _record_action_failure(
                g.db, g.user["username"], "reject", "invalid_status", status=row["status"]
            )
            return _json_error("Only pending requests can be rejected", 409)

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            body = {}
        resource_id = body.get("resource_id")
        if resource_id is not None and (
            isinstance(resource_id, bool) or not isinstance(resource_id, int)
        ):
            _record_action_failure(
                g.db, g.user["username"], "reject", "invalid_resource_id"
            )
            return _json_error("resource_id must be an integer", 400)

        existing_rejection = None
        if resource_id is not None:
            existing_rejection = g.db.execute(
                "SELECT 1 FROM rejections WHERE request_id = ? AND resource_id = ?",
                (request_id, resource_id),
            ).fetchone()
        suggestions = suggest_match(g.db, request_id)
        if resource_id is None:
            if not suggestions:
                _record_action_failure(
                    g.db, g.user["username"], "reject", "no_suggested_resource"
                )
                return _json_error("There is no suggested resource to reject", 409)
            resource_id = suggestions[0]["resource_id"]
        else:
            resource = g.db.execute(
                "SELECT id FROM resources WHERE id = ? AND type = ?",
                (resource_id, row["type"]),
            ).fetchone()
            if resource is None:
                _record_action_failure(
                    g.db, g.user["username"], "reject", "resource_not_found"
                )
                return _json_error("Matching resource not found", 404)
            if not existing_rejection and resource_id not in {
                item["resource_id"] for item in suggestions
            }:
                _record_action_failure(
                    g.db, g.user["username"], "reject", "resource_not_suggested"
                )
                return _json_error("Resource is not currently suggested", 409)

        g.db.execute(
            "INSERT OR IGNORE INTO rejections (request_id, resource_id) VALUES (?, ?)",
            (request_id, resource_id),
        )
        _audit_result(
            g.db,
            g.user["username"],
            "reject",
            True,
            request_id=request_id,
            resource_id=resource_id,
        )
        g.db.commit()
        return jsonify(
            {
                "request_id": request_id,
                "status": "pending",
                "rejected": True,
                "resource_id": resource_id,
            }
        )

    @app.post("/api/matches/<int:request_id>/override")
    @coordinator_required("override")
    def override_match(request_id: int):
        g.db.execute("BEGIN IMMEDIATE")
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            body = {}
        resource_id = body.get("resource_id")
        if isinstance(resource_id, bool) or not isinstance(resource_id, int):
            _record_action_failure(
                g.db, g.user["username"], "override", "invalid_resource_id"
            )
            return _json_error("resource_id must be an integer", 400)

        row = _request_row(g.db, request_id)
        if row is None:
            _record_action_failure(g.db, g.user["username"], "override", "request_not_found")
            return _json_error("Request not found", 404)
        if row["status"] != "pending":
            _record_action_failure(
                g.db, g.user["username"], "override", "invalid_status", status=row["status"]
            )
            return _json_error("Only pending requests can be overridden", 409)

        recommended = suggest_match(g.db, request_id)
        if recommended and recommended[0]["resource_id"] == resource_id:
            _record_action_failure(
                g.db, g.user["username"], "override", "resource_not_different"
            )
            return _json_error("Override resource must differ from the suggested resource", 400)
        resource = g.db.execute(
            "SELECT quantity, lat, lon FROM resources WHERE id = ? AND type = ?",
            (resource_id, row["type"]),
        ).fetchone()
        if resource is None:
            _record_action_failure(g.db, g.user["username"], "override", "resource_not_found")
            return _json_error("Matching resource not found", 404)
        if resource["quantity"] < row["people"]:
            _record_action_failure(g.db, g.user["username"], "override", "insufficient_stock")
            return _json_error("Override resource does not have enough stock", 409)

        distance = haversine_distance_km(row["lat"], row["lon"], resource["lat"], resource["lon"])
        allocations = [
            {
                "resource_id": resource_id,
                "quantity": row["people"],
                "distance_km": distance,
            }
        ]
        success, reason = _apply_allocations(g.db, row, allocations)
        if not success:
            _record_action_failure(
                g.db, g.user["username"], "override", reason or "allocation_failed"
            )
            return jsonify({"error": "Could not override match", "reason": reason}), 409
        _audit_result(
            g.db,
            g.user["username"],
            "override",
            True,
            request_id=request_id,
            resource_id=resource_id,
            quantity=row["people"],
        )
        g.db.commit()
        return jsonify({"request_id": request_id, "status": "matched", "matches": allocations}), 201

    @app.post("/api/matches/<int:request_id>/undo")
    @app.post("/requests/<int:request_id>/undo")
    @coordinator_required("undo")
    def undo_approval(request_id: int):
        g.db.execute("BEGIN IMMEDIATE")
        row = _request_row(g.db, request_id)
        if row is None:
            _record_action_failure(g.db, g.user["username"], "undo", "request_not_found")
            return _json_error("Request not found", 404)
        if row["status"] != "matched":
            _record_action_failure(
                g.db, g.user["username"], "undo", "invalid_status", status=row["status"]
            )
            return _json_error("Only matched requests can be undone", 409)

        active_matches = g.db.execute(
            """
            SELECT id, resource_id, quantity FROM matches
            WHERE request_id = ? AND status = 'active'
            """,
            (request_id,),
        ).fetchall()
        if not active_matches:
            _record_action_failure(g.db, g.user["username"], "undo", "no_active_matches")
            return _json_error("No active matches to undo", 409)
        for match in active_matches:
            changed = g.db.execute(
                "UPDATE resources SET quantity = quantity + ? WHERE id = ?",
                (match["quantity"], match["resource_id"]),
            )
            if changed.rowcount != 1:
                g.db.rollback()
                return _json_error("Could not restore reserved stock", 409)
        g.db.execute(
            "UPDATE matches SET status = 'cancelled' WHERE request_id = ? AND status = 'active'",
            (request_id,),
        )
        g.db.execute(
            "UPDATE requests SET status = 'pending' WHERE id = ? AND status = 'matched'",
            (request_id,),
        )
        g.db.execute(
            "UPDATE requests SET matched_at = NULL WHERE id = ?",
            (request_id,),
        )
        _audit_result(g.db, g.user["username"], "undo", True, request_id=request_id)
        g.db.commit()
        return jsonify({"request_id": request_id, "status": "pending"})

    @app.post("/api/matches/<int:request_id>/deliver")
    @coordinator_required("deliver")
    def deliver_match(request_id: int):
        g.db.execute("BEGIN IMMEDIATE")
        row = _request_row(g.db, request_id)
        if row is None:
            _record_action_failure(g.db, g.user["username"], "deliver", "request_not_found")
            return _json_error("Request not found", 404)
        if row["status"] != "matched":
            _record_action_failure(
                g.db, g.user["username"], "deliver", "invalid_status", status=row["status"]
            )
            return _json_error("Only matched requests can be delivered", 409)
        g.db.execute(
            "UPDATE requests SET status = 'delivered' WHERE id = ? AND status = 'matched'",
            (request_id,),
        )
        _audit_result(g.db, g.user["username"], "deliver", True, request_id=request_id)
        g.db.commit()
        return jsonify({"request_id": request_id, "status": "delivered"})

    @app.get("/api/audit")
    @coordinator_required("view_audit")
    def list_audit():
        rows = g.db.execute(
            """
            SELECT id, "user", action, details, timestamp
            FROM audit_log ORDER BY id DESC
            """
        ).fetchall()
        return jsonify([dict(row) for row in rows])

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=5000)
