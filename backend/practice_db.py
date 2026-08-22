from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = "2"
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1", SCHEMA_VERSION})
LOGGER = logging.getLogger("gdsa_practice.database")


class PracticeDataError(RuntimeError):
    """Raised when the hosted question data is invalid or unavailable."""


@dataclass(frozen=True)
class Choice:
    label: str
    text: str


@dataclass(frozen=True)
class Question:
    question_id: str
    project_id: str
    question_number: int | None
    question: str
    choices: tuple[Choice, ...]
    answers: tuple[str, ...]
    explanation: str | None
    source_file: str
    page_start: int | None
    page_end: int | None
    content_hash: str

    def as_record(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "project_id": self.project_id,
            "question_number": self.question_number,
            "question": self.question,
            "choices": [{"label": choice.label, "text": choice.text} for choice in self.choices],
            "answer": list(self.answers),
            "explanation": self.explanation,
            "source_file": self.source_file,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class ImportResult:
    project_id: str
    source_path: Path
    database_path: Path
    question_count: int
    skipped_count: int
    issues: tuple[str, ...]


@dataclass(frozen=True)
class QuestionBankImport:
    source_path: Path
    project_id: str


def _required_text(record: dict[str, Any], field: str, line_number: int) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PracticeDataError(f"Line {line_number}: {field} must be a non-empty string.")
    return value.strip()


def _optional_int(record: dict[str, Any], field: str, line_number: int) -> int | None:
    value = record.get(field)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PracticeDataError(f"Line {line_number}: {field} must be a non-negative integer or null.")
    return value


def _answer_labels(value: Any, line_number: int) -> tuple[str, ...]:
    if isinstance(value, str):
        raw_labels = value.replace(";", ",").split(",")
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        raw_labels = value
    else:
        raise PracticeDataError(f"Line {line_number}: answer must be a string or list of strings.")

    labels: list[str] = []
    for raw_label in raw_labels:
        label = raw_label.strip().upper()
        if label and label not in labels:
            labels.append(label)
    if not labels:
        raise PracticeDataError(f"Line {line_number}: answer must contain at least one choice label.")
    return tuple(labels)


def _validated_question(record: Any, line_number: int, project_id: str) -> Question:
    if not isinstance(record, dict):
        raise PracticeDataError(f"Line {line_number}: each JSONL row must be an object.")

    record_project_id = _required_text(record, "project_id", line_number)
    if record_project_id != project_id:
        raise PracticeDataError(
            f"Line {line_number}: project_id is {record_project_id!r}; expected {project_id!r}."
        )

    raw_choices = record.get("choices")
    if not isinstance(raw_choices, list) or len(raw_choices) < 2:
        raise PracticeDataError(f"Line {line_number}: choices must contain at least two entries.")
    choices: list[Choice] = []
    labels: set[str] = set()
    for choice_number, raw_choice in enumerate(raw_choices, start=1):
        if not isinstance(raw_choice, dict):
            raise PracticeDataError(f"Line {line_number}: choice {choice_number} must be an object.")
        label_value = raw_choice.get("label")
        text_value = raw_choice.get("text")
        if not isinstance(label_value, str) or not label_value.strip():
            raise PracticeDataError(f"Line {line_number}: choice {choice_number} is missing a label.")
        if not isinstance(text_value, str) or not text_value.strip():
            raise PracticeDataError(f"Line {line_number}: choice {choice_number} is missing text.")
        label = label_value.strip().upper()
        if label in labels:
            raise PracticeDataError(f"Line {line_number}: duplicate choice label {label!r}.")
        labels.add(label)
        choices.append(Choice(label=label, text=text_value.strip()))

    answers = _answer_labels(record.get("answer"), line_number)
    invalid_answers = [label for label in answers if label not in labels]
    if invalid_answers:
        raise PracticeDataError(
            f"Line {line_number}: answer references unknown choice label(s): {','.join(invalid_answers)}."
        )

    page_start = _optional_int(record, "page_start", line_number)
    page_end = _optional_int(record, "page_end", line_number)
    if (page_start is None) != (page_end is None):
        raise PracticeDataError(f"Line {line_number}: page_start and page_end must both be set or both be null.")
    if page_start is not None and page_end is not None and page_end < page_start:
        raise PracticeDataError(f"Line {line_number}: page_end cannot be before page_start.")

    explanation_value = record.get("explanation")
    if explanation_value is not None and not isinstance(explanation_value, str):
        raise PracticeDataError(f"Line {line_number}: explanation must be a string or null.")
    explanation = explanation_value.strip() if isinstance(explanation_value, str) else None
    if not explanation:
        explanation = None

    content_hash_value = record.get("content_hash")
    content_hash = content_hash_value.strip() if isinstance(content_hash_value, str) else ""
    if not content_hash:
        canonical = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return Question(
        question_id=_required_text(record, "question_id", line_number),
        project_id=record_project_id,
        question_number=_optional_int(record, "question_number", line_number),
        question=_required_text(record, "question", line_number),
        choices=tuple(choices),
        answers=answers,
        explanation=explanation,
        source_file=_required_text(record, "source_file", line_number),
        page_start=page_start,
        page_end=page_end,
        content_hash=content_hash,
    )


def _read_questions(
    source_path: Path,
    project_id: str,
    *,
    skip_invalid: bool,
    issues: list[str],
) -> Iterator[Question]:
    seen_question_ids: set[str] = set()
    try:
        source_file = source_path.open("r", encoding="utf-8")
    except OSError as exc:
        raise PracticeDataError(f"Could not open question source {source_path}: {exc}") from exc

    with source_file:
        for line_number, line in enumerate(source_file, start=1):
            if not line.strip():
                continue
            try:
                try:
                    raw_record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise PracticeDataError(f"Line {line_number}: invalid JSON: {exc.msg}.") from exc
                question = _validated_question(raw_record, line_number, project_id)
                if question.question_id in seen_question_ids:
                    raise PracticeDataError(
                        f"Line {line_number}: duplicate question_id {question.question_id!r}."
                    )
            except PracticeDataError as exc:
                if not skip_invalid:
                    raise
                issues.append(str(exc))
                continue
            seen_question_ids.add(question.question_id)
            yield question


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) STRICT;
        CREATE TABLE question_banks (
            project_id TEXT PRIMARY KEY,
            question_count INTEGER NOT NULL CHECK (question_count > 0),
            skipped_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
            source_file TEXT NOT NULL,
            imported_at TEXT NOT NULL
        ) STRICT;
        CREATE TABLE questions (
            question_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            sort_order INTEGER NOT NULL UNIQUE CHECK (sort_order >= 0),
            question_number INTEGER,
            question TEXT NOT NULL CHECK (length(question) > 0),
            answer_json TEXT NOT NULL CHECK (json_valid(answer_json)),
            explanation TEXT,
            source_file TEXT NOT NULL,
            page_start INTEGER,
            page_end INTEGER,
            content_hash TEXT NOT NULL
        ) STRICT;
        CREATE INDEX questions_project_order_idx ON questions(project_id, sort_order);
        CREATE TABLE choices (
            question_id TEXT NOT NULL REFERENCES questions(question_id) ON DELETE CASCADE,
            position INTEGER NOT NULL CHECK (position >= 0),
            label TEXT NOT NULL,
            text TEXT NOT NULL CHECK (length(text) > 0),
            PRIMARY KEY (question_id, label),
            UNIQUE (question_id, position)
        ) STRICT;
        """
    )


def _insert_question(
    connection: sqlite3.Connection,
    question: Question,
    sort_order: int,
) -> None:
    connection.execute(
        """
        INSERT INTO questions (
            question_id, project_id, sort_order, question_number, question,
            answer_json, explanation, source_file, page_start, page_end, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            question.question_id,
            question.project_id,
            sort_order,
            question.question_number,
            question.question,
            json.dumps(question.answers, separators=(",", ":")),
            question.explanation,
            question.source_file,
            question.page_start,
            question.page_end,
            question.content_hash,
        ),
    )
    connection.executemany(
        "INSERT INTO choices (question_id, position, label, text) VALUES (?, ?, ?, ?)",
        [
            (question.question_id, position, choice.label, choice.text)
            for position, choice in enumerate(question.choices)
        ],
    )


def import_jsonl_banks(
    banks: list[QuestionBankImport],
    database_path: Path,
    *,
    skip_invalid: bool = False,
) -> tuple[ImportResult, ...]:
    if not banks:
        raise PracticeDataError("At least one question bank source is required.")
    normalized_banks: list[QuestionBankImport] = []
    seen_projects: set[str] = set()
    for bank in banks:
        project_id = bank.project_id.strip()
        if not project_id:
            raise PracticeDataError("project_id must be a non-empty string.")
        if project_id in seen_projects:
            raise PracticeDataError(f"Duplicate question bank project_id: {project_id!r}.")
        seen_projects.add(project_id)
        source = bank.source_path.expanduser().resolve()
        if not source.is_file():
            raise PracticeDataError(f"Question source does not exist or is not a file: {source}")
        normalized_banks.append(QuestionBankImport(source, project_id))

    destination = database_path.expanduser().resolve()
    if any(bank.source_path == destination for bank in normalized_banks):
        raise PracticeDataError("Question source and database destination must be different files.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    results: list[ImportResult] = []

    try:
        with closing(sqlite3.connect(temporary_path)) as connection:
            _initialize_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            sort_order = 0
            imported_at = datetime.now(UTC).isoformat()
            for bank in normalized_banks:
                question_count = 0
                issues: list[str] = []
                for question in _read_questions(
                    bank.source_path,
                    bank.project_id,
                    skip_invalid=skip_invalid,
                    issues=issues,
                ):
                    _insert_question(connection, question, sort_order)
                    sort_order += 1
                    question_count += 1

                if question_count == 0:
                    raise PracticeDataError(
                        f"Question source for {bank.project_id!r} did not contain any records."
                    )
                connection.execute(
                    """
                    INSERT INTO question_banks (
                        project_id, question_count, skipped_count, source_file, imported_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        bank.project_id,
                        question_count,
                        len(issues),
                        bank.source_path.name,
                        imported_at,
                    ),
                )
                results.append(
                    ImportResult(
                        project_id=bank.project_id,
                        source_path=bank.source_path,
                        database_path=destination,
                        question_count=question_count,
                        skipped_count=len(issues),
                        issues=tuple(issues),
                    )
                )

            total_questions = sum(result.question_count for result in results)
            total_skipped = sum(result.skipped_count for result in results)
            metadata = {
                "schema_version": SCHEMA_VERSION,
                "project_ids": json.dumps([bank.project_id for bank in normalized_banks]),
                "question_count": str(total_questions),
                "skipped_count": str(total_skipped),
                "imported_at": imported_at,
                "source_file": ",".join(bank.source_path.name for bank in normalized_banks),
            }
            connection.executemany("INSERT INTO metadata (key, value) VALUES (?, ?)", metadata.items())
            connection.commit()
            integrity_result = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity_result is None or integrity_result[0] != "ok":
                raise PracticeDataError("SQLite integrity check failed after import.")

        os.replace(temporary_path, destination)
    except (OSError, sqlite3.Error) as exc:
        raise PracticeDataError(f"Could not create SQLite question database: {exc}") from exc
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError as exc:
            LOGGER.warning(
                "Could not remove temporary SQLite import file path=%s exception_type=%s",
                temporary_path,
                type(exc).__name__,
            )

    return tuple(results)


def import_jsonl_database(
    source_path: Path,
    database_path: Path,
    project_id: str,
    *,
    skip_invalid: bool = False,
) -> ImportResult:
    results = import_jsonl_banks(
        [QuestionBankImport(source_path=source_path, project_id=project_id)],
        database_path,
        skip_invalid=skip_invalid,
    )
    return results[0]


class QuestionRepository:
    def __init__(self, database_path: Path, project_id: str) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.project_id = project_id

    def _connect(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise PracticeDataError(f"SQLite question database was not found: {self.database_path}")
        database_uri = f"{self.database_path.as_uri()}?mode=ro"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(database_uri, uri=True, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA query_only = ON")
            self._validate_metadata(connection)
            return connection
        except PracticeDataError:
            if connection is not None:
                connection.close()
            raise
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise PracticeDataError(f"Could not open SQLite question database: {exc}") from exc

    def _validate_metadata(self, connection: sqlite3.Connection) -> None:
        try:
            metadata = dict(connection.execute("SELECT key, value FROM metadata").fetchall())
        except sqlite3.Error as exc:
            raise PracticeDataError("SQLite question database is missing required metadata.") from exc
        schema_version = metadata.get("schema_version")
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise PracticeDataError(
                f"Unsupported SQLite schema version {schema_version!r}; expected {SCHEMA_VERSION!r}."
            )
        if schema_version == "1":
            database_project_id = metadata.get("project_id")
            if database_project_id != self.project_id:
                raise PracticeDataError(
                    f"SQLite project_id is {database_project_id!r}; expected {self.project_id!r}."
                )
            return
        bank = connection.execute(
            "SELECT 1 FROM question_banks WHERE project_id = ?", (self.project_id,)
        ).fetchone()
        if bank is None:
            raise PracticeDataError(f"SQLite question bank is missing project {self.project_id!r}.")

    @staticmethod
    def _row_to_record(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        choices = connection.execute(
            "SELECT label, text FROM choices WHERE question_id = ? ORDER BY position",
            (row["question_id"],),
        ).fetchall()
        try:
            answers = json.loads(row["answer_json"])
        except json.JSONDecodeError as exc:
            raise PracticeDataError(f"Stored answer is invalid for question {row['question_id']!r}.") from exc
        return {
            "question_id": row["question_id"],
            "project_id": row["project_id"],
            "question_number": row["question_number"],
            "question": row["question"],
            "choices": [{"label": choice["label"], "text": choice["text"]} for choice in choices],
            "answer": answers,
            "explanation": row["explanation"],
            "source_file": row["source_file"],
            "page_start": row["page_start"],
            "page_end": row["page_end"],
            "content_hash": row["content_hash"],
        }

    def available_projects(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            schema_version = dict(connection.execute("SELECT key, value FROM metadata").fetchall()).get(
                "schema_version"
            )
            if schema_version == "1":
                question_count = connection.execute(
                    "SELECT COUNT(*) FROM questions WHERE project_id = ?", (self.project_id,)
                ).fetchone()[0]
                return {self.project_id: int(question_count)}
            rows = connection.execute(
                "SELECT project_id, question_count FROM question_banks ORDER BY project_id"
            ).fetchall()
            return {str(row["project_id"]): int(row["question_count"]) for row in rows}

    def load_questions(self, project_id: str | None = None) -> list[dict[str, Any]]:
        effective_project_id = project_id or self.project_id
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM questions WHERE project_id = ? ORDER BY sort_order", (effective_project_id,)
            ).fetchall()
            if not rows:
                raise PracticeDataError(f"No questions were found for project {effective_project_id!r}.")
            return [self._row_to_record(connection, row) for row in rows]

    def get_question(self, question_id: str, project_id: str | None = None) -> dict[str, Any] | None:
        effective_project_id = project_id or self.project_id
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM questions WHERE project_id = ? AND question_id = ?",
                (effective_project_id, question_id),
            ).fetchone()
            return self._row_to_record(connection, row) if row is not None else None

    def get_questions_by_ids(self, project_id: str, question_ids: list[str]) -> list[dict[str, Any]]:
        if not question_ids:
            return []
        placeholders = ",".join("?" for _ in question_ids)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM questions WHERE project_id = ? AND question_id IN ({placeholders})",
                (project_id, *question_ids),
            ).fetchall()
            records_by_id = {
                str(row["question_id"]): self._row_to_record(connection, row)
                for row in rows
            }
        missing = [question_id for question_id in question_ids if question_id not in records_by_id]
        if missing:
            raise PracticeDataError(
                f"Question bank {project_id!r} is missing attempt question(s): {','.join(missing)}."
            )
        return [records_by_id[question_id] for question_id in question_ids]

    def health(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM metadata").fetchall())
            question_count = connection.execute(
                "SELECT COUNT(*) FROM questions WHERE project_id = ?", (self.project_id,)
            ).fetchone()[0]
            if question_count <= 0:
                raise PracticeDataError(f"No questions were found for project {self.project_id!r}.")
            if metadata.get("schema_version") == "1":
                available_projects = {self.project_id: int(question_count)}
            else:
                rows = connection.execute(
                    "SELECT project_id, question_count FROM question_banks ORDER BY project_id"
                ).fetchall()
                available_projects = {
                    str(row["project_id"]): int(row["question_count"]) for row in rows
                }
            return {
                "schema_version": metadata.get("schema_version"),
                "project_id": self.project_id,
                "question_count": question_count,
                "available_projects": available_projects,
            }
