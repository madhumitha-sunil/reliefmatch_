"""Core validation, urgency, distance, and resource matching logic."""

from __future__ import annotations

import math
import sqlite3
from typing import Any

REQUEST_TYPES = frozenset({"water", "food", "medicine", "shelter"})
TYPE_WEIGHTS = {
    "medicine": 3.0,
    "water": 2.0,
    "food": 1.5,
    "shelter": 1.0,
}
EARTH_RADIUS_KM = 6371.0


def validate_request(
    request_type: str,
    people: int,
    severity: int,
    lat: float,
    lon: float,
) -> None:
    """Raise ValueError unless the request fields are supported and in range."""
    if not isinstance(request_type, str) or request_type not in REQUEST_TYPES:
        raise ValueError(f"Unknown request type: {request_type!r}")
    if isinstance(severity, bool) or not isinstance(severity, int) or not 1 <= severity <= 5:
        raise ValueError("Severity must be an integer from 1 to 5")
    if isinstance(people, bool) or not isinstance(people, int) or people <= 0:
        raise ValueError("People must be a positive integer")
    if not _is_finite_number(lat) or not 8 <= lat <= 13.6:
        raise ValueError("Latitude must be between 8 and 13.6")
    if not _is_finite_number(lon) or not 76 <= lon <= 80.4:
        raise ValueError("Longitude must be between 76 and 80.4")


def calculate_urgency(
    request_type: str,
    people: int,
    severity: int,
    wait_hours: float,
) -> float:
    """Calculate urgency using the specified severity, wait, and type weights."""
    if not isinstance(request_type, str) or request_type not in TYPE_WEIGHTS:
        raise ValueError(f"Unknown request type: {request_type!r}")
    if isinstance(severity, bool) or not isinstance(severity, int) or not 1 <= severity <= 5:
        raise ValueError("Severity must be an integer from 1 to 5")
    if isinstance(people, bool) or not isinstance(people, int) or people <= 0:
        raise ValueError("People must be a positive integer")
    if not _is_finite_number(wait_hours) or wait_hours < 0:
        raise ValueError("Wait hours must be a non-negative finite number")

    wait_factor = (1 + min(wait_hours, 24)) / 6
    return severity * min(people, 1000) * wait_factor * TYPE_WEIGHTS[request_type]


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance between two points in kilometers."""
    coordinates = (lat1, lon1, lat2, lon2)
    if not all(_is_finite_number(value) for value in coordinates):
        raise ValueError("Coordinates must be finite numbers")
    if not (-90 <= lat1 <= 90 and -90 <= lat2 <= 90):
        raise ValueError("Latitude must be between -90 and 90")
    if not (-180 <= lon1 <= 180 and -180 <= lon2 <= 180):
        raise ValueError("Longitude must be between -180 and 180")

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    # Floating-point rounding can put a just outside [0, 1].
    a = min(1.0, max(0.0, a))
    return 2 * EARTH_RADIUS_KM * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def match_request(connection: sqlite3.Connection, request_id: int) -> list[dict[str, Any]]:
    """Allocate the request from nearest same-type resources.

    A single resource that can cover the remaining need is preferred. If none
    can, available resources are allocated nearest-first until the request is
    fully covered or the eligible inventory is exhausted. Existing non-cancelled
    matches reserve inventory and count toward the request's fulfillment.
    """
    with connection:
        request = connection.execute(
            """
            SELECT id, type, people, lat, lon
            FROM requests
            WHERE id = ?
            """,
            (request_id,),
        ).fetchone()
        if request is None:
            raise ValueError(f"Request {request_id} does not exist")

        already_allocated = connection.execute(
            """
            SELECT COALESCE(SUM(quantity), 0)
            FROM matches
            WHERE request_id = ? AND status != 'cancelled'
            """,
            (request_id,),
        ).fetchone()[0]
        remaining = max(0, request["people"] - already_allocated)

        if remaining:
            resource_rows = connection.execute(
                """
                SELECT r.id, r.quantity, r.lat, r.lon,
                       r.quantity - COALESCE(SUM(
                           CASE WHEN m.status != 'cancelled' THEN m.quantity ELSE 0 END
                       ), 0) AS available
                FROM resources AS r
                LEFT JOIN matches AS m ON m.resource_id = r.id
                WHERE r.type = ?
                GROUP BY r.id, r.quantity, r.lat, r.lon
                HAVING available > 0
                """,
                (request["type"],),
            ).fetchall()

            nearby = sorted(
                (
                    (
                        haversine_distance_km(
                            request["lat"], request["lon"], row["lat"], row["lon"]
                        ),
                        row["id"],
                        row["available"],
                    )
                    for row in resource_rows
                ),
                key=lambda item: (item[0], item[1]),
            )

            # Prefer one complete allocation when any single resource can meet
            # the need; among those candidates, choose the nearest.
            complete_candidate = next(
                (candidate for candidate in nearby if candidate[2] >= remaining),
                None,
            )
            allocations: list[tuple[int, int]] = []
            if complete_candidate is not None:
                allocations.append((complete_candidate[1], remaining))
            else:
                for _, resource_id, available in nearby:
                    quantity = min(remaining, available)
                    if quantity > 0:
                        allocations.append((resource_id, quantity))
                        remaining -= quantity
                    if remaining == 0:
                        break

            for resource_id, quantity in allocations:
                connection.execute(
                    """
                    INSERT INTO matches (request_id, resource_id, quantity, status)
                    VALUES (?, ?, ?, 'proposed')
                    """,
                    (request_id, resource_id, quantity),
                )

        total_allocated = connection.execute(
            """
            SELECT COALESCE(SUM(quantity), 0)
            FROM matches
            WHERE request_id = ? AND status != 'cancelled'
            """,
            (request_id,),
        ).fetchone()[0]
        if total_allocated >= request["people"]:
            new_status = "matched"
        elif total_allocated:
            new_status = "partially_fulfilled"
        else:
            new_status = "pending"
        connection.execute(
            "UPDATE requests SET status = ? WHERE id = ?",
            (new_status, request_id),
        )

        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, request_id, resource_id, quantity, status
                FROM matches
                WHERE request_id = ? AND status != 'cancelled'
                ORDER BY id
                """,
                (request_id,),
            ).fetchall()
        ]


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
