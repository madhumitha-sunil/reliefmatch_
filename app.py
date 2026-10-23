"""Flask endpoints for approving and undoing aid matches."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import Flask, jsonify

from db import DEFAULT_DB_PATH, get_connection, initialize_db
from logic import suggest_match


def create_app(database_path: str | Path = DEFAULT_DB_PATH) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE_PATH"] = str(database_path)
    initialize_db(database_path)

    @app.post("/requests/<int:request_id>/approve")
    def approve_request(request_id: int):
        connection = get_connection(app.config["DATABASE_PATH"])
        try:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute(
                "SELECT id, status FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
            if request is None:
                connection.rollback()
                return jsonify({"error": "Request not found"}), 404
            if request["status"] != "pending":
                connection.rollback()
                return jsonify({"error": "Only pending requests can be approved"}), 409

            suggestions = suggest_match(connection, request_id)
            if suggestions.shortfall:
                connection.rollback()
                return (
                    jsonify(
                        {
                            "error": "Insufficient stock",
                            "shortfall": suggestions.shortfall,
                        }
                    ),
                    409,
                )

            # Confirm each allocation against current stock while holding the
            # IMMEDIATE transaction lock before writing any part of the match.
            for suggestion in suggestions:
                stock = connection.execute(
                    "SELECT quantity FROM resources WHERE id = ?",
                    (suggestion["resource_id"],),
                ).fetchone()
                if stock is None or stock["quantity"] < suggestion["quantity"]:
                    connection.rollback()
                    return jsonify({"error": "Stock changed; request was not approved"}), 409

            for suggestion in suggestions:
                updated = connection.execute(
                    """
                    UPDATE resources
                    SET quantity = quantity - ?
                    WHERE id = ? AND quantity >= ?
                    """,
                    (
                        suggestion["quantity"],
                        suggestion["resource_id"],
                        suggestion["quantity"],
                    ),
                )
                if updated.rowcount != 1:
                    connection.rollback()
                    return jsonify({"error": "Stock changed; request was not approved"}), 409
                connection.execute(
                    """
                    INSERT INTO matches (request_id, resource_id, quantity, status)
                    VALUES (?, ?, ?, 'active')
                    """,
                    (request_id, suggestion["resource_id"], suggestion["quantity"]),
                )

            updated = connection.execute(
                "UPDATE requests SET status = 'matched' WHERE id = ? AND status = 'pending'",
                (request_id,),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return jsonify({"error": "Request status changed; approval was cancelled"}), 409

            connection.commit()
            return jsonify(
                {
                    "request_id": request_id,
                    "status": "matched",
                    "matches": list(suggestions),
                    "shortfall": 0,
                }
            ), 201
        except sqlite3.Error:
            connection.rollback()
            app.logger.exception("Failed to approve request %s", request_id)
            return jsonify({"error": "Could not approve request"}), 500
        finally:
            connection.close()

    @app.post("/requests/<int:request_id>/undo")
    def undo_approval(request_id: int):
        connection = get_connection(app.config["DATABASE_PATH"])
        try:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute(
                "SELECT id, status FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
            if request is None:
                connection.rollback()
                return jsonify({"error": "Request not found"}), 404
            if request["status"] != "matched":
                connection.rollback()
                return jsonify({"error": "Only matched requests can be undone"}), 409

            active_matches = connection.execute(
                """
                SELECT id, resource_id, quantity
                FROM matches
                WHERE request_id = ? AND status = 'active'
                """,
                (request_id,),
            ).fetchall()
            if not active_matches:
                connection.rollback()
                return jsonify({"error": "No active matches to undo"}), 409

            for match in active_matches:
                restored = connection.execute(
                    "UPDATE resources SET quantity = quantity + ? WHERE id = ?",
                    (match["quantity"], match["resource_id"]),
                )
                if restored.rowcount != 1:
                    connection.rollback()
                    return jsonify({"error": "Could not restore reserved stock"}), 409

            connection.execute(
                "UPDATE matches SET status = 'cancelled' WHERE request_id = ? AND status = 'active'",
                (request_id,),
            )
            connection.execute(
                "UPDATE requests SET status = 'pending' WHERE id = ?",
                (request_id,),
            )
            connection.commit()
            return jsonify({"request_id": request_id, "status": "pending"}), 200
        except sqlite3.Error:
            connection.rollback()
            app.logger.exception("Failed to undo request %s", request_id)
            return jsonify({"error": "Could not undo request approval"}), 500
        finally:
            connection.close()

    return app

