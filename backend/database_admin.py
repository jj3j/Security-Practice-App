from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from practice_db import PracticeDataError, QuestionRepository


LOGGER = logging.getLogger("gdsa_practice.database_admin")


def _temporary_path(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(file_descriptor)
    return Path(temporary_name)


def _remove_temporary(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        LOGGER.warning(
            "Could not remove temporary database file path=%s exception_type=%s",
            path,
            type(exc).__name__,
        )


def _verify_database(database_path: Path, project_id: str) -> int:
    health = QuestionRepository(database_path, project_id).health()
    return int(health["question_count"])


def backup_database(database_path: Path, backup_directory: Path, project_id: str) -> Path:
    source = database_path.expanduser().resolve()
    destination_directory = backup_directory.expanduser().resolve()
    question_count = _verify_database(source, project_id)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = destination_directory / f"gdsa-practice-{timestamp}.sqlite3"
    temporary = _temporary_path(destination)

    try:
        source_uri = f"{source.as_uri()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True, timeout=5.0)) as source_connection:
            with closing(sqlite3.connect(temporary)) as destination_connection:
                source_connection.backup(destination_connection)
                integrity = destination_connection.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise PracticeDataError("Backup failed SQLite integrity validation.")
        if _verify_database(temporary, project_id) != question_count:
            raise PracticeDataError("Backup question count does not match the source database.")
        os.replace(temporary, destination)
        if os.name != "nt":
            destination.chmod(0o640)
    except (OSError, sqlite3.Error) as exc:
        raise PracticeDataError(f"Could not back up SQLite question database: {exc}") from exc
    finally:
        _remove_temporary(temporary)
    return destination


def restore_database(backup_path: Path, database_path: Path, project_id: str) -> int:
    source = backup_path.expanduser().resolve()
    destination = database_path.expanduser().resolve()
    if source == destination:
        raise PracticeDataError("Backup source and database destination must be different files.")
    question_count = _verify_database(source, project_id)
    temporary = _temporary_path(destination)

    try:
        source_uri = f"{source.as_uri()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True, timeout=5.0)) as source_connection:
            with closing(sqlite3.connect(temporary)) as destination_connection:
                source_connection.backup(destination_connection)
                integrity = destination_connection.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise PracticeDataError("Restored database failed SQLite integrity validation.")
        if _verify_database(temporary, project_id) != question_count:
            raise PracticeDataError("Restored database question count does not match the backup.")
        os.replace(temporary, destination)
        if os.name != "nt":
            destination.chmod(0o640)
    except (OSError, sqlite3.Error) as exc:
        raise PracticeDataError(f"Could not restore SQLite question database: {exc}") from exc
    finally:
        _remove_temporary(temporary)
    return question_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up or restore the hosted GDSA SQLite database.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="Create and validate an online SQLite backup.")
    backup_parser.add_argument("--database", type=Path, required=True)
    backup_parser.add_argument("--backup-dir", type=Path, required=True)
    backup_parser.add_argument("--project", required=True)

    restore_parser = subparsers.add_parser("restore", help="Validate and atomically restore a SQLite backup.")
    restore_parser.add_argument("--backup", type=Path, required=True)
    restore_parser.add_argument("--database", type=Path, required=True)
    restore_parser.add_argument("--project", required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "backup":
            backup_path = backup_database(args.database, args.backup_dir, args.project)
            print(f"Backup written: {backup_path}")
        else:
            question_count = restore_database(args.backup, args.database, args.project)
            print(f"Database restored: {args.database.expanduser().resolve()}")
            print(f"Questions restored: {question_count}")
    except PracticeDataError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
