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


class MatchSuggestions(list[dict[str, Any]]):
    """A list of proposed allocations with the unmet quantity attached."""

    def __init__(self, allocations: list[dict[str, Any]], shortfall: int = 0):
        super().__init__(allocations)
        self.shortfall = shortfall


def suggest_match(connection: sqlite3.Connection, request_id: int) -> MatchSuggestions:
    """Return read-only nearest-resource allocations and any unmet quantity.

    A sufficient single resource is preferred, choosing the nearest such
    resource. Otherwise, stock is allocated nearest-first across matching
    resources. The ``shortfall`` attribute is the quantity still unmet.
    """
    request = connection.execute(
        "SELECT id, type, people, lat, lon FROM requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    if request is None:
        raise ValueError(f"Request {request_id} does not exist")

    resource_rows = connection.execute(
        """
        SELECT r.id, r.quantity, r.lat, r.lon
        FROM resources AS r
        WHERE r.type = ? AND r.quantity > 0
          AND NOT EXISTS (
              SELECT 1 FROM rejections AS rejected
              WHERE rejected.request_id = ? AND rejected.resource_id = r.id
          )
        """,
        (request["type"], request_id),
    ).fetchall()
    nearby = sorted(
        (
            (
                haversine_distance_km(
                    request["lat"], request["lon"], row["lat"], row["lon"]
                ),
                row["id"],
                row["quantity"],
            )
            for row in resource_rows
        ),
        key=lambda item: (item[0], item[1]),
    )

    # Prefer the nearest one-resource solution before considering a split.
    complete_candidate = next(
        (candidate for candidate in nearby if candidate[2] >= request["people"]),
        None,
    )
    allocations: list[dict[str, Any]] = []
    if complete_candidate is not None:
        distance, resource_id, _ = complete_candidate
        allocations.append(
            {
                "resource_id": resource_id,
                "quantity": request["people"],
                "distance_km": distance,
            }
        )
        return MatchSuggestions(allocations)

    remaining = request["people"]
    for distance, resource_id, available in nearby:
        quantity = min(remaining, available)
        if quantity:
            allocations.append(
                {
                    "resource_id": resource_id,
                    "quantity": quantity,
                    "distance_km": distance,
                }
            )
            remaining -= quantity
        if remaining == 0:
            break

    return MatchSuggestions(allocations, shortfall=remaining)


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
