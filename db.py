"""SQLite connection and schema setup for ReliefMatch."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "reliefmatch.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('coordinator', 'volunteer'))
);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('water', 'food', 'medicine', 'shelter')),
    people INTEGER NOT NULL CHECK (people > 0),
    severity INTEGER NOT NULL CHECK (severity BETWEEN 1 AND 5),
    lat REAL NOT NULL CHECK (lat BETWEEN 8 AND 13.6),
    lon REAL NOT NULL CHECK (lon BETWEEN 76 AND 80.4),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'matched', 'delivered')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('water', 'food', 'medicine', 'shelter')),
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    lat REAL NOT NULL CHECK (lat BETWEEN 8 AND 13.6),
    lon REAL NOT NULL CHECK (lon BETWEEN 76 AND 80.4),
    provider TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    "user" TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS idx_resources_type ON resources(type);
CREATE INDEX IF NOT EXISTS idx_matches_request ON matches(request_id);
CREATE INDEX IF NOT EXISTS idx_matches_resource ON matches(resource_id);
"""


def get_connection(database_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a configured SQLite connection with WAL and foreign keys enabled."""
    path = str(database_path)
    if path != ":memory:":
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize_db(database_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Create the database schema if it does not already exist."""
    connection = get_connection(database_path)
    try:
        connection.executescript(SCHEMA)
    finally:
        connection.close()
