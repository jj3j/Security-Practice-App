"""Atomically add or replace one validated bank in a schema-v2 question database."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from practice_db import (
    SCHEMA_VERSION,
    PracticeDataError,
    Question,
    _insert_question,
    _read_questions,
)


def _validated_questions(source_path: Path, project_id: str) -> list[Question]:
    issues: list[str] = []
    questions = list(
        _read_questions(source_path, project_id, skip_invalid=False, issues=issues)
    )
    if issues:
        raise PracticeDataError("Strict bank validation unexpectedly produced skipped records.")
    if not questions:
        raise PracticeDataError(f"Question source for {project_id!r} did not contain any records.")
    return questions


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        return dict(connection.execute("SELECT key, value FROM metadata").fetchall())
    except sqlite3.Error as exc:
        raise PracticeDataError("SQLite question database is missing required metadata.") from exc


def _bank_signature(connection: sqlite3.Connection, project_id: str) -> list[tuple[str, str]]:
    return [
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT question_id, content_hash FROM questions WHERE project_id = ? ORDER BY sort_order",
            (project_id,),
        ).fetchall()
    ]


def merge_question_bank(
    source_path: Path,
    database_path: Path,
    project_id: str,
    *,
    expected_count: int | None = None,
) -> bool:
    source = source_path.expanduser().resolve()
    destination = database_path.expanduser().resolve()
    normalized_project_id = project_id.strip()
    if not normalized_project_id:
        raise PracticeDataError("project_id must be a non-empty string.")
    if not source.is_file():
        raise PracticeDataError(f"Question source does not exist or is not a file: {source}")
    if not destination.is_file():
        raise PracticeDataError(f"SQLite question database was not found: {destination}")
    if source == destination:
        raise PracticeDataError("Question source and database destination must be different files.")

    questions = _validated_questions(source, normalized_project_id)
    if expected_count is not None and len(questions) != expected_count:
        raise PracticeDataError(
            f"Question source for {normalized_project_id!r} contains {len(questions)} records; "
            f"expected {expected_count}."
        )
    desired_signature = [(question.question_id, question.content_hash) for question in questions]
    original_stat = destination.stat()
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".merge.tmp", dir=destination.parent
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)

    try:
        source_uri = f"{destination.as_uri()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True, timeout=5.0)) as source_connection:
            with closing(sqlite3.connect(temporary_path)) as temporary_connection:
                source_connection.backup(temporary_connection)

        with closing(sqlite3.connect(temporary_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            metadata = _metadata(connection)
            if metadata.get("schema_version") != SCHEMA_VERSION:
                raise PracticeDataError(
                    "Single-bank merge requires a schema-v2 multi-course question database."
                )
            existing_signature = _bank_signature(connection, normalized_project_id)
            bank_row = connection.execute(
                "SELECT question_count FROM question_banks WHERE project_id = ?",
                (normalized_project_id,),
            ).fetchone()
            if (
                existing_signature == desired_signature
                and bank_row is not None
                and int(bank_row[0]) == len(questions)
            ):
                return False

            placeholders = ",".join("?" for _ in questions)
            conflicting = connection.execute(
                f"SELECT question_id, project_id FROM questions "
                f"WHERE question_id IN ({placeholders}) AND project_id != ?",
                (*[question.question_id for question in questions], normalized_project_id),
            ).fetchone()
            if conflicting is not None:
                raise PracticeDataError(
                    f"Question ID {conflicting[0]!r} already belongs to project {conflicting[1]!r}."
                )

            imported_at = datetime.now(UTC).isoformat()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM questions WHERE project_id = ?", (normalized_project_id,))
            connection.execute("DELETE FROM question_banks WHERE project_id = ?", (normalized_project_id,))
            next_sort_order = int(
                connection.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM questions").fetchone()[0]
            )
            for offset, question in enumerate(questions):
                _insert_question(connection, question, next_sort_order + offset)
            connection.execute(
                """
                INSERT INTO question_banks (
                    project_id, question_count, skipped_count, source_file, imported_at
                ) VALUES (?, ?, 0, ?, ?)
                """,
                (normalized_project_id, len(questions), source.name, imported_at),
            )

            bank_rows = connection.execute(
                "SELECT project_id, question_count, skipped_count, source_file "
                "FROM question_banks ORDER BY rowid"
            ).fetchall()
            metadata_updates = {
                "schema_version": SCHEMA_VERSION,
                "project_ids": json.dumps([str(row[0]) for row in bank_rows]),
                "question_count": str(sum(int(row[1]) for row in bank_rows)),
                "skipped_count": str(sum(int(row[2]) for row in bank_rows)),
                "imported_at": imported_at,
                "source_file": ",".join(str(row[3]) for row in bank_rows),
            }
            connection.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                metadata_updates.items(),
            )
            integrity_result = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity_result is None or integrity_result[0] != "ok":
                raise PracticeDataError("SQLite integrity check failed after bank merge.")
            connection.commit()

        os.chmod(temporary_path, stat.S_IMODE(original_stat.st_mode))
        if os.name == "posix" and hasattr(os, "chown"):
            os.chown(temporary_path, original_stat.st_uid, original_stat.st_gid)
        os.replace(temporary_path, destination)
        return True
    except PracticeDataError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise PracticeDataError(f"Could not merge SQLite question bank: {exc}") from exc
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--expected-count", type=int)
    args = parser.parse_args()
    try:
        changed = merge_question_bank(
            args.source,
            args.database,
            args.project,
            expected_count=args.expected_count,
        )
    except PracticeDataError as exc:
        print(f"Error: {exc}", file=os.sys.stderr)
        return 2
    if changed:
        print(f"Question bank merged: {args.project}")
    else:
        print(f"Question bank already current: {args.project}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
