"""Automatic backups for the ReliefMatch SQLite database.

Why not just copy the .sqlite3 file? The database runs in WAL mode, so recent
writes can sit in a separate -wal file. A plain file copy can miss them or catch a
half-written state. SQLite's online backup API takes a consistent snapshot while the
app keeps running.

Command line (run from the project folder, in the virtual environment):
    python backup.py                 make a backup now
    python backup.py list            show saved backups, newest first
    python backup.py restore NAME    restore a backup (stop the app first!)
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from db import DEFAULT_DB_PATH

PREFIX = "reliefmatch-"
SUFFIX = ".sqlite3"
DEFAULT_KEEP = 30
DEFAULT_INTERVAL_MINUTES = 10


def default_backup_dir(database_path: str | Path) -> Path:
    return Path(database_path).resolve().parent / "backups"


def _check_integrity(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if result != "ok":
        raise RuntimeError(f"Backup failed integrity check: {result}")


def list_backups(backup_dir: str | Path) -> list[Path]:
    """Saved backups, newest first. Safety copies made by restore are not listed."""
    folder = Path(backup_dir)
    if not folder.is_dir():
        return []
    files = [
        item
        for item in folder.iterdir()
        if item.is_file() and item.name.startswith(PREFIX) and item.name.endswith(SUFFIX)
    ]

    def order(item: Path) -> tuple[str, int]:
        core = item.name[len(PREFIX):-len(SUFFIX)]  # e.g. 20261025T101010Z or ...Z-1
        stamp, _, extra = core.partition("-")
        return stamp, int(extra) if extra.isdigit() else 0

    return sorted(files, key=order, reverse=True)


def create_backup(
    database_path: str | Path = DEFAULT_DB_PATH,
    backup_dir: str | Path | None = None,
    keep: int = DEFAULT_KEEP,
    now: datetime | None = None,
) -> Path:
    """Take a consistent snapshot, verify it, then delete the oldest beyond `keep`."""
    database_path = Path(database_path)
    if not database_path.exists():
        raise FileNotFoundError(f"Database not found: {database_path}")
    folder = Path(backup_dir) if backup_dir else default_backup_dir(database_path)
    folder.mkdir(parents=True, exist_ok=True)

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    final = folder / f"{PREFIX}{stamp}{SUFFIX}"
    counter = 1
    while final.exists():  # two backups in the same second must not overwrite each other
        final = folder / f"{PREFIX}{stamp}-{counter}{SUFFIX}"
        counter += 1
    partial = final.with_name(final.name + ".partial")

    source = sqlite3.connect(str(database_path), timeout=10)
    try:
        target = sqlite3.connect(str(partial))
        try:
            source.backup(target)
        finally:
            target.close()
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    finally:
        source.close()

    try:
        _check_integrity(partial)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    os.replace(partial, final)  # only a finished, verified backup gets a real name

    for old in list_backups(folder)[max(keep, 1):]:
        old.unlink(missing_ok=True)
    return final


def restore_backup(
    backup_file: str | Path,
    database_path: str | Path = DEFAULT_DB_PATH,
    backup_dir: str | Path | None = None,
) -> Path:
    """Replace the live database with a backup. The app must be stopped first.

    The current database is saved as before-restore-*.sqlite3 so a mistaken restore
    can itself be undone. Returns the path of that safety copy.
    """
    backup_file = Path(backup_file)
    database_path = Path(database_path)
    if not backup_file.exists():
        raise FileNotFoundError(f"Backup not found: {backup_file}")
    _check_integrity(backup_file)  # refuse a damaged backup before touching anything

    folder = Path(backup_dir) if backup_dir else default_backup_dir(database_path)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safety = folder / f"before-restore-{stamp}{SUFFIX}"
    if database_path.exists():
        source = sqlite3.connect(str(database_path), timeout=10)
        try:
            target = sqlite3.connect(str(safety))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()

    # Stale WAL/SHM files from the old database must not be applied to the restored one.
    for extra in ("-wal", "-shm"):
        Path(str(database_path) + extra).unlink(missing_ok=True)
    shutil.copyfile(backup_file, database_path)
    return safety


def start_scheduler(
    database_path: str | Path,
    interval_minutes: float = DEFAULT_INTERVAL_MINUTES,
    keep: int = DEFAULT_KEEP,
    backup_dir: str | Path | None = None,
    log=print,
) -> threading.Event | None:
    """Back up now, then every `interval_minutes`, on a background thread.

    Returns an Event; set() it to stop. Returns None when interval is 0 or less.
    A failed backup is reported but never stops the app.
    """
    if interval_minutes <= 0:
        return None
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                saved = create_backup(database_path, backup_dir, keep)
                log(f"[backup] saved {saved.name}")
            except Exception as error:  # noqa: BLE001 - must never crash the server
                log(f"[backup] FAILED: {error}")
            stop.wait(interval_minutes * 60)

    threading.Thread(target=loop, name="reliefmatch-backup", daemon=True).start()
    return stop


def main(argv: list[str]) -> int:
    folder = default_backup_dir(DEFAULT_DB_PATH)
    command = argv[1] if len(argv) > 1 else "now"
    if command == "now":
        print("Saved", create_backup(DEFAULT_DB_PATH))
        return 0
    if command == "list":
        backups = list_backups(folder)
        if not backups:
            print("No backups yet.")
        for item in backups:
            print(item.name)
        return 0
    if command == "restore" and len(argv) > 2:
        name = Path(argv[2])
        chosen = name if name.exists() else folder / name.name
        safety = restore_backup(chosen, DEFAULT_DB_PATH)
        print(f"Restored {chosen.name}. Previous database kept as {safety.name}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
