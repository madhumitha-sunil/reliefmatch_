"""Seed a fresh ReliefMatch SQLite database with demo users and flood data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from werkzeug.security import generate_password_hash

from db import DEFAULT_DB_PATH, get_connection, initialize_db

UTC = timezone.utc
CENTER_LAT = 10.79
CENTER_LON = 78.69
SEED_PASSWORD = "relief123"

RESOURCE_DATA = [
    ("water", 180, 0.006, 0.003, "Trichy Water Team"),
    ("water", 150, -0.010, 0.012, "Cauvery Relief"),
    ("water", 140, 0.018, -0.011, "Municipal Water Unit"),
    ("food", 160, -0.014, -0.008, "Community Kitchen"),
    ("food", 150, 0.015, 0.016, "District Food Bank"),
    ("medicine", 120, 0.004, -0.019, "Mobile Health Unit"),
    ("medicine", 110, -0.021, 0.006, "Trichy Medical Aid"),
    ("medicine", 100, 0.023, 0.009, "Primary Care Network"),
    ("shelter", 130, -0.006, 0.024, "Emergency Shelter Group"),
    ("shelter", 120, 0.020, -0.021, "District Shelter Team"),
]

REQUEST_DATA = [
    ("water", 80, 4),
    ("water", 210, 5),
    ("water", 60, 2),
    ("water", 320, 5),
    ("water", 45, 1),
    ("food", 45, 2),
    ("food", 220, 4),
    ("food", 65, 3),
    ("food", 180, 5),
    ("food", 40, 1),
    ("medicine", 20, 3),
    ("medicine", 180, 5),
    ("medicine", 90, 4),
    ("medicine", 260, 5),
    ("medicine", 75, 2),
    ("shelter", 50, 3),
    ("shelter", 190, 5),
    ("shelter", 70, 2),
    ("shelter", 230, 4),
    ("shelter", 60, 1),
]


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def seed_database(database_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Seed an empty database; leave any non-empty database untouched."""
    initialize_db(database_path)
    connection = get_connection(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("users", "requests", "resources", "matches", "audit_log")
        }
        if any(counts.values()):
            connection.commit()
            return {"seeded": False, "reason": "database_not_empty"}

        connection.executemany(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            [
                ("coordinator", generate_password_hash(SEED_PASSWORD), "coordinator"),
                ("volunteer", generate_password_hash(SEED_PASSWORD), "volunteer"),
            ],
        )

        connection.executemany(
            """
            INSERT INTO resources (type, quantity, lat, lon, provider)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    resource_type,
                    quantity,
                    CENTER_LAT + lat_offset,
                    CENTER_LON + lon_offset,
                    provider,
                )
                for resource_type, quantity, lat_offset, lon_offset, provider in RESOURCE_DATA
            ],
        )

        now = datetime.now(UTC)
        request_rows = []
        for index, (request_type, people, severity) in enumerate(REQUEST_DATA):
            type_index = index // 5
            within_type_index = index % 5
            # Spread same-type requests beyond the 200 m duplicate radius.
            lat = CENTER_LAT + type_index * 0.012 + within_type_index * 0.003
            lon = CENTER_LON - type_index * 0.009 + within_type_index * 0.003
            created_at = _timestamp(now - timedelta(minutes=index + 1))
            expires_at = _timestamp(now + timedelta(hours=24) - timedelta(minutes=index))
            request_rows.append(
                (
                    request_type,
                    people,
                    severity,
                    lat,
                    lon,
                    "pending",
                    created_at,
                    expires_at,
                )
            )
        connection.executemany(
            """
            INSERT INTO requests
                (type, people, severity, lat, lon, status, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            request_rows,
        )
        connection.commit()
        return {"seeded": True, "users": 2, "requests": 20, "resources": 10}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    print(seed_database())
