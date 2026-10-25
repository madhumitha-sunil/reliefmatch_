import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backup import create_backup, list_backups, restore_backup, start_scheduler
from db import get_connection, initialize_db


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "reliefmatch.sqlite3"
    initialize_db(path)
    return path


def add_resources(path, count, start=0):
    connection = get_connection(path)
    for index in range(start, start + count):
        connection.execute(
            "INSERT INTO resources (type, quantity, lat, lon, provider) VALUES ('water', ?, 10.8, 78.7, ?)",
            (100 + index, f"provider-{index}"),
        )
    connection.commit()
    connection.close()


def count_resources(path):
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
    finally:
        connection.close()


def test_backup_is_a_complete_valid_copy_in_a_backups_folder(database, tmp_path):
    add_resources(database, 5)

    saved = create_backup(database)

    assert saved.parent == tmp_path / "backups"
    assert saved.name.startswith("reliefmatch-") and saved.name.endswith(".sqlite3")
    assert count_resources(saved) == 5
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_backup_captures_recent_writes_still_in_the_wal_file(database):
    # Keep a connection open and do not checkpoint: the new rows live in the -wal file.
    connection = get_connection(database)
    connection.execute(
        "INSERT INTO resources (type, quantity, lat, lon, provider) VALUES ('food', 50, 10.8, 78.7, 'wal-only')"
    )
    connection.commit()

    saved = create_backup(database)

    assert count_resources(saved) == 1
    connection.close()


def test_only_the_newest_backups_are_kept(database):
    for second in range(7):
        create_backup(
            database,
            keep=3,
            now=datetime(2026, 10, 25, 10, 0, second, tzinfo=timezone.utc),
        )

    names = [item.name for item in list_backups(database.parent / "backups")]

    assert len(names) == 3
    assert names[0] == "reliefmatch-20261025T100006Z.sqlite3"  # newest first
    assert names[-1] == "reliefmatch-20261025T100004Z.sqlite3"


def test_two_backups_in_the_same_second_do_not_overwrite_each_other(database):
    same = datetime(2026, 10, 25, 10, 0, 0, tzinfo=timezone.utc)

    first = create_backup(database, now=same)
    second = create_backup(database, now=same)

    assert first != second
    assert first.exists() and second.exists()
    assert list_backups(first.parent)[0] == second  # the later one counts as newer


def test_restore_brings_back_deleted_data_and_keeps_a_safety_copy(database):
    add_resources(database, 4)
    saved = create_backup(database)
    connection = get_connection(database)
    connection.execute("DELETE FROM resources")  # disaster: data wiped
    connection.commit()
    connection.close()
    assert count_resources(database) == 0

    safety = restore_backup(saved, database)

    assert count_resources(database) == 4
    assert safety.exists() and safety.name.startswith("before-restore-")
    assert count_resources(safety) == 0  # the wiped state is kept, so restore is undoable
    # the restored database is fully usable by the app
    add_resources(database, 1, start=10)
    assert count_resources(database) == 5


def test_restore_refuses_a_damaged_backup_and_leaves_the_database_alone(database, tmp_path):
    add_resources(database, 3)
    broken = tmp_path / "backups" / "reliefmatch-20261025T100000Z.sqlite3"
    broken.parent.mkdir(exist_ok=True)
    broken.write_bytes(b"this is not a database" * 100)

    with pytest.raises(Exception):
        restore_backup(broken, database)

    assert count_resources(database) == 3


def test_backup_of_missing_database_fails_clearly(tmp_path):
    with pytest.raises(FileNotFoundError):
        create_backup(tmp_path / "nope.sqlite3")


def test_scheduler_backs_up_immediately_and_can_be_stopped(database):
    messages = []
    stop = start_scheduler(database, interval_minutes=0.01, keep=5, log=messages.append)
    deadline = time.time() + 5
    while time.time() < deadline and len(list_backups(database.parent / "backups")) < 2:
        time.sleep(0.1)
    stop.set()

    assert len(list_backups(database.parent / "backups")) >= 2  # repeated on the timer
    assert any("saved" in message for message in messages)


def test_scheduler_reports_failure_without_crashing(tmp_path):
    messages = []
    stop = start_scheduler(tmp_path / "missing.sqlite3", interval_minutes=0.01, log=messages.append)
    deadline = time.time() + 3
    while time.time() < deadline and not messages:
        time.sleep(0.05)
    stop.set()

    assert any("FAILED" in message for message in messages)


def test_scheduler_is_off_when_interval_is_zero(database):
    assert start_scheduler(database, interval_minutes=0) is None
    assert not (database.parent / "backups").exists()
