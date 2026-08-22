from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


SCHEMA_VERSION = "4"
PREVIOUS_SCHEMA_VERSIONS = frozenset({"1", "2", "3"})
DEFAULT_ADMIN_PAGE_SIZE = 50
MAX_ADMIN_PAGE_SIZE = 100
MAX_PENDING_IDENTITIES = 1000
ALLOWED_CHAPTER_STATUSES = frozenset({"not_started", "in_progress", "completed"})
ALLOWED_FLASHCARD_STATES = frozenset({"new", "learning", "review", "mastered"})
ALLOWED_EXAM_MODES = frozenset({"mock", "trial"})
ALLOWED_IDENTITY_ROLES = frozenset({"learner", "owner"})
ALLOWED_ASSESSMENT_KINDS = frozenset({"practice", "exam"})
ALLOWED_LEARNER_ITEM_TYPES = frozenset({"lesson", "question"})
ALLOWED_ADMIN_ACTIONS = frozenset(
    {
        "approve_pending_identity",
        "dismiss_pending_identity",
        "update_identity",
        "update_course_access",
    }
)


class LearningDataError(RuntimeError):
    """Raised when learner state cannot be validated or persisted."""


class LearningAuthorizationError(PermissionError):
    """Raised when an OIDC identity is not approved to use the application."""


class PendingIdentityNotFoundError(LearningDataError):
    """Raised when an owner acts on a pending identity that no longer exists."""


@dataclass(frozen=True)
class LearnerIdentity:
    user_id: str
    issuer: str
    subject: str
    email: str | None
    display_name: str | None
    role: str


@dataclass(frozen=True)
class LoginTransaction:
    nonce: str
    code_verifier: str
    return_path: str


@dataclass(frozen=True)
class LearnerSession:
    user_id: str
    issuer: str
    subject: str
    email: str | None
    display_name: str | None
    role: str
    csrf_token: str | None = None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _role(value: str) -> str:
    normalized = _required_text(value, "role", max_length=32).lower()
    if normalized not in ALLOWED_IDENTITY_ROLES:
        raise LearningDataError(
            f"role must be one of: {', '.join(sorted(ALLOWED_IDENTITY_ROLES))}."
        )
    return normalized


def _required_text(value: str, field: str, *, max_length: int = 1024) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LearningDataError(f"{field} must be a non-empty string.")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise LearningDataError(f"{field} must not exceed {max_length} characters.")
    return normalized


def _optional_text(value: str | None, field: str, *, max_length: int = 1024) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LearningDataError(f"{field} must be a string or null.")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise LearningDataError(f"{field} must not exceed {max_length} characters.")
    return normalized


def _connect(database_path: Path) -> sqlite3.Connection:
    path = database_path.expanduser().resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection
    except (OSError, sqlite3.Error) as exc:
        raise LearningDataError(f"Could not open learner database: {exc}") from exc


def _protect_last_enabled_owner(
    connection: sqlite3.Connection,
    issuer: str,
    subject: str,
    new_role: str,
    new_enabled: bool = True,
) -> None:
    target = connection.execute(
        "SELECT role, enabled FROM approved_identities WHERE issuer = ? AND subject = ?",
        (issuer, subject),
    ).fetchone()
    if (
        target is None
        or target["enabled"] != 1
        or target["role"] != "owner"
        or (new_role == "owner" and new_enabled)
    ):
        return
    enabled_owner_count = connection.execute(
        "SELECT COUNT(*) FROM approved_identities WHERE role = 'owner' AND enabled = 1"
    ).fetchone()[0]
    if enabled_owner_count <= 1:
        raise LearningDataError(
            "Refusing to demote the last enabled owner. Approve another owner first."
        )


def initialize_learning_database(database_path: Path) -> None:
    with closing(_connect(database_path)) as connection:
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            existing_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if existing_tables and "metadata" not in existing_tables:
                raise LearningDataError(
                    "Learner database contains tables but is missing schema metadata."
                )
            existing_version: str | None = None
            if "metadata" in existing_tables:
                metadata = dict(connection.execute("SELECT key, value FROM metadata").fetchall())
                existing_version = metadata.get("schema_version")
                if existing_version is None:
                    raise LearningDataError(
                        "Learner database metadata is missing schema_version."
                    )
                if existing_version not in {*PREVIOUS_SCHEMA_VERSIONS, SCHEMA_VERSION}:
                    raise LearningDataError(
                        f"Unsupported learner schema version {existing_version!r}; "
                        f"expected {SCHEMA_VERSION!r}."
                    )

            connection.executescript(
                """
                BEGIN IMMEDIATE;

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                ) STRICT;

                CREATE TABLE IF NOT EXISTS approved_identities (
                    issuer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    label TEXT,
                    role TEXT NOT NULL DEFAULT 'learner' CHECK (role IN ('learner', 'owner')),
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (issuer, subject)
                ) STRICT;

                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    display_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                ) STRICT;

                CREATE TABLE IF NOT EXISTS oidc_identities (
                    issuer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    email TEXT,
                    last_login_at TEXT NOT NULL,
                    PRIMARY KEY (issuer, subject)
                ) STRICT;
                CREATE INDEX IF NOT EXISTS oidc_identities_user_idx
                    ON oidc_identities(user_id);

                CREATE TABLE IF NOT EXISTS pending_identities (
                    issuer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    email TEXT,
                    display_name TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (issuer, subject)
                ) STRICT;

                CREATE TABLE IF NOT EXISTS identity_course_access (
                    issuer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    course_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (issuer, subject, course_id),
                    FOREIGN KEY (issuer, subject)
                        REFERENCES approved_identities(issuer, subject)
                        ON DELETE CASCADE
                ) STRICT;

                CREATE TABLE IF NOT EXISTS admin_audit_events (
                    event_id INTEGER PRIMARY KEY,
                    actor_issuer TEXT NOT NULL,
                    actor_subject TEXT NOT NULL,
                    action TEXT NOT NULL CHECK (action IN ('approve_pending_identity')),
                    target_issuer TEXT NOT NULL,
                    target_subject TEXT NOT NULL,
                    target_role TEXT NOT NULL CHECK (target_role IN ('learner', 'owner')),
                    created_at TEXT NOT NULL
                ) STRICT;
                CREATE INDEX IF NOT EXISTS admin_audit_events_created_idx
                    ON admin_audit_events(created_at DESC, event_id DESC);

                CREATE TABLE IF NOT EXISTS admin_activity_events (
                    event_id INTEGER PRIMARY KEY,
                    actor_issuer TEXT NOT NULL,
                    actor_subject TEXT NOT NULL,
                    action TEXT NOT NULL CHECK (
                        action IN (
                            'approve_pending_identity',
                            'dismiss_pending_identity',
                            'update_identity',
                            'update_course_access'
                        )
                    ),
                    target_issuer TEXT NOT NULL,
                    target_subject TEXT NOT NULL,
                    target_role TEXT CHECK (target_role IN ('learner', 'owner')),
                    target_course_id TEXT,
                    target_enabled INTEGER CHECK (target_enabled IN (0, 1)),
                    created_at TEXT NOT NULL
                ) STRICT;
                CREATE INDEX IF NOT EXISTS admin_activity_events_created_idx
                    ON admin_activity_events(created_at DESC, event_id DESC);

                CREATE TABLE IF NOT EXISTS login_transactions (
                    state_hash TEXT PRIMARY KEY,
                    nonce TEXT NOT NULL,
                    code_verifier TEXT NOT NULL,
                    return_path TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL CHECK (expires_at > created_at)
                ) STRICT;
                CREATE INDEX IF NOT EXISTS login_transactions_expiry_idx
                    ON login_transactions(expires_at);

                CREATE TABLE IF NOT EXISTS sessions (
                    session_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    csrf_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    idle_expires_at INTEGER NOT NULL,
                    absolute_expires_at INTEGER NOT NULL,
                    revoked_at INTEGER,
                    CHECK (idle_expires_at <= absolute_expires_at)
                ) STRICT;
                CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS sessions_expiry_idx
                    ON sessions(absolute_expires_at);

                CREATE TABLE IF NOT EXISTS chapter_progress (
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    course_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('not_started', 'in_progress', 'completed')
                    ),
                    last_viewed_at TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY (user_id, course_id, chapter_id)
                ) STRICT;

                CREATE TABLE IF NOT EXISTS flashcard_progress (
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    course_id TEXT NOT NULL,
                    flashcard_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('new', 'learning', 'review', 'mastered')
                    ),
                    review_count INTEGER NOT NULL DEFAULT 0 CHECK (review_count >= 0),
                    correct_count INTEGER NOT NULL DEFAULT 0 CHECK (
                        correct_count >= 0 AND correct_count <= review_count
                    ),
                    last_reviewed_at TEXT,
                    PRIMARY KEY (user_id, course_id, flashcard_id)
                ) STRICT;

                CREATE TABLE IF NOT EXISTS learner_items (
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    course_id TEXT NOT NULL,
                    item_type TEXT NOT NULL CHECK (item_type IN ('lesson', 'question')),
                    item_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, course_id, item_type, item_id)
                ) STRICT;
                CREATE INDEX IF NOT EXISTS learner_items_user_created_idx
                    ON learner_items(user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS exam_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    course_id TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK (mode IN ('mock', 'trial')),
                    assessment_kind TEXT NOT NULL DEFAULT 'exam' CHECK (
                        assessment_kind IN ('practice', 'exam')
                    ),
                    content_version TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('active', 'submitted', 'expired', 'abandoned')
                    ),
                    started_at TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    submitted_at TEXT,
                    correct_count INTEGER CHECK (correct_count >= 0),
                    question_count INTEGER NOT NULL CHECK (question_count > 0),
                    score_percent REAL CHECK (score_percent >= 0 AND score_percent <= 100)
                ) STRICT;
                CREATE INDEX IF NOT EXISTS exam_attempts_user_started_idx
                    ON exam_attempts(user_id, started_at DESC);

                CREATE TABLE IF NOT EXISTS attempt_questions (
                    attempt_id TEXT NOT NULL REFERENCES exam_attempts(attempt_id) ON DELETE CASCADE,
                    position INTEGER NOT NULL CHECK (position >= 0),
                    question_id TEXT NOT NULL,
                    PRIMARY KEY (attempt_id, position),
                    UNIQUE (attempt_id, question_id)
                ) STRICT;

                CREATE TABLE IF NOT EXISTS attempt_answers (
                    attempt_id TEXT NOT NULL,
                    question_id TEXT NOT NULL,
                    selected_json TEXT NOT NULL CHECK (json_valid(selected_json)),
                    answered_at TEXT NOT NULL,
                    is_correct INTEGER CHECK (is_correct IN (0, 1)),
                    PRIMARY KEY (attempt_id, question_id),
                    FOREIGN KEY (attempt_id, question_id)
                        REFERENCES attempt_questions(attempt_id, question_id)
                        ON DELETE CASCADE
                ) STRICT;
                """
            )

            attempt_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(exam_attempts)")
            }
            if "assessment_kind" not in attempt_columns:
                connection.execute(
                    "ALTER TABLE exam_attempts ADD COLUMN assessment_kind TEXT NOT NULL "
                    "DEFAULT 'exam' CHECK (assessment_kind IN ('practice', 'exam'))"
                )
                connection.execute(
                    "UPDATE exam_attempts SET assessment_kind = 'practice' "
                    "WHERE content_version LIKE 'practice:%'"
                )

            if existing_version is None:
                now = _utc_now()
                connection.executemany(
                    "INSERT INTO metadata (key, value) VALUES (?, ?)",
                    (("schema_version", SCHEMA_VERSION), ("created_at", now)),
                )
            else:
                if existing_version == "1":
                    connection.execute(
                        "ALTER TABLE approved_identities ADD COLUMN role TEXT NOT NULL "
                        "DEFAULT 'learner' CHECK (role IN ('learner', 'owner'))"
                    )
                connection.execute(
                    """
                    INSERT INTO admin_activity_events (
                        actor_issuer, actor_subject, action, target_issuer,
                        target_subject, target_role, created_at
                    )
                    SELECT actor_issuer, actor_subject, action, target_issuer,
                           target_subject, target_role, created_at
                    FROM admin_audit_events
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM admin_activity_events AS activity
                        WHERE activity.action = admin_audit_events.action
                          AND activity.actor_issuer = admin_audit_events.actor_issuer
                          AND activity.actor_subject = admin_audit_events.actor_subject
                          AND activity.target_issuer = admin_audit_events.target_issuer
                          AND activity.target_subject = admin_audit_events.target_subject
                          AND activity.created_at = admin_audit_events.created_at
                    )
                    """
                )
                connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                    (SCHEMA_VERSION,),
                )
            connection.commit()
        except LearningDataError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise LearningDataError(f"Could not initialize learner database: {exc}") from exc


class LearningRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.expanduser().resolve()

    def initialize(self) -> None:
        initialize_learning_database(self.database_path)

    def health(self) -> dict[str, str]:
        with closing(_connect(self.database_path)) as connection:
            try:
                version_row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
            except sqlite3.Error as exc:
                raise LearningDataError("Learner database health check failed.") from exc
        version = str(version_row[0]) if version_row is not None else None
        if version != SCHEMA_VERSION:
            raise LearningDataError(
                f"Unsupported learner schema version {version!r}; expected {SCHEMA_VERSION!r}."
            )
        return {"learner_schema_version": version}

    def list_chapter_progress(self, user_id: str, course_id: str) -> list[dict[str, str | None]]:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        normalized_course_id = _required_text(course_id, "course_id", max_length=255)
        with closing(_connect(self.database_path)) as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT chapter_id, status, last_viewed_at, completed_at
                    FROM chapter_progress
                    WHERE user_id = ? AND course_id = ?
                    ORDER BY chapter_id
                    """,
                    (normalized_user_id, normalized_course_id),
                ).fetchall()
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not list chapter progress: {exc}") from exc
        return [
            {
                "chapter_id": str(row["chapter_id"]),
                "status": str(row["status"]),
                "last_viewed_at": str(row["last_viewed_at"]),
                "completed_at": (
                    str(row["completed_at"]) if row["completed_at"] is not None else None
                ),
            }
            for row in rows
        ]

    def set_chapter_progress(
        self,
        user_id: str,
        course_id: str,
        chapter_id: str,
        status: str,
    ) -> dict[str, str | None]:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        normalized_course_id = _required_text(course_id, "course_id", max_length=255)
        normalized_chapter_id = _required_text(chapter_id, "chapter_id", max_length=128)
        normalized_status = _required_text(status, "status", max_length=32).lower()
        if normalized_status not in ALLOWED_CHAPTER_STATUSES:
            raise LearningDataError(
                f"status must be one of: {', '.join(sorted(ALLOWED_CHAPTER_STATUSES))}."
            )
        now = _utc_now()
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT status, completed_at
                    FROM chapter_progress
                    WHERE user_id = ? AND course_id = ? AND chapter_id = ?
                    """,
                    (normalized_user_id, normalized_course_id, normalized_chapter_id),
                ).fetchone()
                if existing is not None and existing["status"] == "completed":
                    effective_status = "completed"
                    completed_at = (
                        str(existing["completed_at"])
                        if existing["completed_at"] is not None
                        else now
                    )
                else:
                    effective_status = normalized_status
                    completed_at = now if normalized_status == "completed" else None
                connection.execute(
                    """
                    INSERT INTO chapter_progress (
                        user_id, course_id, chapter_id, status, last_viewed_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (user_id, course_id, chapter_id) DO UPDATE SET
                        status = excluded.status,
                        last_viewed_at = excluded.last_viewed_at,
                        completed_at = excluded.completed_at
                    """,
                    (
                        normalized_user_id,
                        normalized_course_id,
                        normalized_chapter_id,
                        effective_status,
                        now,
                        completed_at,
                    ),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not update chapter progress: {exc}") from exc
        return {
            "chapter_id": normalized_chapter_id,
            "status": effective_status,
            "last_viewed_at": now,
            "completed_at": completed_at,
        }

    def list_flashcard_progress(
        self, user_id: str, course_id: str
    ) -> list[dict[str, str | int | None]]:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        normalized_course_id = _required_text(course_id, "course_id", max_length=255)
        with closing(_connect(self.database_path)) as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT flashcard_id, state, review_count, correct_count, last_reviewed_at
                    FROM flashcard_progress
                    WHERE user_id = ? AND course_id = ?
                    ORDER BY flashcard_id
                    """,
                    (normalized_user_id, normalized_course_id),
                ).fetchall()
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not list flashcard progress: {exc}") from exc
        return [
            {
                "flashcard_id": str(row["flashcard_id"]),
                "state": str(row["state"]),
                "review_count": int(row["review_count"]),
                "correct_count": int(row["correct_count"]),
                "last_reviewed_at": (
                    str(row["last_reviewed_at"])
                    if row["last_reviewed_at"] is not None
                    else None
                ),
            }
            for row in rows
        ]

    def record_flashcard_review(
        self,
        user_id: str,
        course_id: str,
        flashcard_id: str,
        *,
        known: bool,
    ) -> dict[str, str | int | None]:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        normalized_course_id = _required_text(course_id, "course_id", max_length=255)
        normalized_flashcard_id = _required_text(
            flashcard_id, "flashcard_id", max_length=128
        )
        if not isinstance(known, bool):
            raise LearningDataError("known must be a boolean.")
        now = _utc_now()
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT review_count, correct_count
                    FROM flashcard_progress
                    WHERE user_id = ? AND course_id = ? AND flashcard_id = ?
                    """,
                    (normalized_user_id, normalized_course_id, normalized_flashcard_id),
                ).fetchone()
                review_count = (int(existing["review_count"]) if existing is not None else 0) + 1
                correct_count = (int(existing["correct_count"]) if existing is not None else 0) + (
                    1 if known else 0
                )
                if not known:
                    state = "learning"
                elif correct_count >= 2:
                    state = "mastered"
                else:
                    state = "review"
                connection.execute(
                    """
                    INSERT INTO flashcard_progress (
                        user_id, course_id, flashcard_id, state, review_count,
                        correct_count, last_reviewed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (user_id, course_id, flashcard_id) DO UPDATE SET
                        state = excluded.state,
                        review_count = excluded.review_count,
                        correct_count = excluded.correct_count,
                        last_reviewed_at = excluded.last_reviewed_at
                    """,
                    (
                        normalized_user_id,
                        normalized_course_id,
                        normalized_flashcard_id,
                        state,
                        review_count,
                        correct_count,
                        now,
                    ),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not update flashcard progress: {exc}") from exc
        return {
            "flashcard_id": normalized_flashcard_id,
            "state": state,
            "review_count": review_count,
            "correct_count": correct_count,
            "last_reviewed_at": now,
        }

    def count_exam_attempts(self, user_id: str, course_id: str, content_version: str) -> int:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        normalized_course_id = _required_text(course_id, "course_id", max_length=255)
        normalized_version = _required_text(content_version, "content_version", max_length=128)
        with closing(_connect(self.database_path)) as connection:
            try:
                return int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM exam_attempts
                        WHERE user_id = ? AND course_id = ? AND content_version = ?
                        """,
                        (normalized_user_id, normalized_course_id, normalized_version),
                    ).fetchone()[0]
                )
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not count exam attempts: {exc}") from exc

    def list_attempted_question_ids(
        self,
        user_id: str,
        course_id: str,
        content_version: str,
    ) -> list[str]:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        normalized_course_id = _required_text(course_id, "course_id", max_length=255)
        normalized_version = _required_text(content_version, "content_version", max_length=128)
        with closing(_connect(self.database_path)) as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT DISTINCT q.question_id
                    FROM attempt_questions AS q
                    JOIN exam_attempts AS a ON a.attempt_id = q.attempt_id
                    WHERE a.user_id = ? AND a.course_id = ? AND a.content_version = ?
                    ORDER BY q.question_id
                    """,
                    (normalized_user_id, normalized_course_id, normalized_version),
                ).fetchall()
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not list attempted questions: {exc}") from exc
        return [str(row["question_id"]) for row in rows]

    def create_exam_attempt(
        self,
        user_id: str,
        course_id: str,
        mode: str,
        content_version: str,
        question_ids: list[str],
        *,
        duration_seconds: int,
        assessment_kind: str = "exam",
    ) -> dict[str, str | int]:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        normalized_course_id = _required_text(course_id, "course_id", max_length=255)
        normalized_mode = _required_text(mode, "mode", max_length=32).lower()
        if normalized_mode not in ALLOWED_EXAM_MODES:
            raise LearningDataError(f"mode must be one of: {', '.join(sorted(ALLOWED_EXAM_MODES))}.")
        normalized_kind = _required_text(
            assessment_kind, "assessment_kind", max_length=32
        ).lower()
        if normalized_kind not in ALLOWED_ASSESSMENT_KINDS:
            raise LearningDataError(
                "assessment_kind must be one of: "
                f"{', '.join(sorted(ALLOWED_ASSESSMENT_KINDS))}."
            )
        normalized_version = _required_text(content_version, "content_version", max_length=128)
        if not isinstance(duration_seconds, int) or isinstance(duration_seconds, bool) or duration_seconds <= 0:
            raise LearningDataError("duration_seconds must be a positive integer.")
        normalized_question_ids = [
            _required_text(question_id, "question_id", max_length=255)
            for question_id in question_ids
        ]
        if not normalized_question_ids:
            raise LearningDataError("question_ids must not be empty.")
        if len(set(normalized_question_ids)) != len(normalized_question_ids):
            raise LearningDataError("question_ids must not contain duplicates.")
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        deadline = (now_dt + timedelta(seconds=duration_seconds)).isoformat()
        attempt_id = str(uuid.uuid4())
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO exam_attempts (
                        attempt_id, user_id, course_id, mode, assessment_kind,
                        content_version, status,
                        started_at, deadline_at, submitted_at, correct_count,
                        question_count, score_percent
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL, NULL, ?, NULL)
                    """,
                    (
                        attempt_id,
                        normalized_user_id,
                        normalized_course_id,
                        normalized_mode,
                        normalized_kind,
                        normalized_version,
                        now,
                        deadline,
                        len(normalized_question_ids),
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO attempt_questions (attempt_id, position, question_id)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (attempt_id, position, question_id)
                        for position, question_id in enumerate(normalized_question_ids)
                    ],
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not create exam attempt: {exc}") from exc
        return {
            "attempt_id": attempt_id,
            "course_id": normalized_course_id,
            "mode": normalized_mode,
            "assessment_kind": normalized_kind,
            "content_version": normalized_version,
            "status": "active",
            "started_at": now,
            "deadline_at": deadline,
            "question_count": len(normalized_question_ids),
        }

    def exam_attempt_question_ids(self, user_id: str, attempt_id: str) -> list[str]:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        normalized_attempt_id = _required_text(attempt_id, "attempt_id", max_length=64)
        with closing(_connect(self.database_path)) as connection:
            try:
                attempt = connection.execute(
                    "SELECT 1 FROM exam_attempts WHERE attempt_id = ? AND user_id = ?",
                    (normalized_attempt_id, normalized_user_id),
                ).fetchone()
                if attempt is None:
                    raise LearningDataError("Exam attempt was not found.")
                rows = connection.execute(
                    """
                    SELECT question_id
                    FROM attempt_questions
                    WHERE attempt_id = ?
                    ORDER BY position
                    """,
                    (normalized_attempt_id,),
                ).fetchall()
            except LearningDataError:
                raise
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not load exam attempt questions: {exc}") from exc
        return [str(row["question_id"]) for row in rows]

    def submit_exam_attempt(
        self,
        user_id: str,
        attempt_id: str,
        answers: dict[str, list[str]],
        *,
        correct_count: int,
        score_percent: float | None,
        correctness: dict[str, bool | None] | None = None,
    ) -> None:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        normalized_attempt_id = _required_text(attempt_id, "attempt_id", max_length=64)
        if isinstance(correct_count, bool) or not isinstance(correct_count, int) or correct_count < 0:
            raise LearningDataError("correct_count must be a non-negative integer.")
        if score_percent is not None and (
            isinstance(score_percent, bool)
            or not isinstance(score_percent, (int, float))
            or score_percent < 0
            or score_percent > 100
        ):
            raise LearningDataError("score_percent must be between 0 and 100.")
        correctness = correctness or {}
        if any(
            not isinstance(question_id, str)
            or (value is not None and not isinstance(value, bool))
            for question_id, value in correctness.items()
        ):
            raise LearningDataError("correctness must map question IDs to booleans or null.")
        now = _utc_now()
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                attempt = connection.execute(
                    """
                    SELECT status
                    FROM exam_attempts
                    WHERE attempt_id = ? AND user_id = ?
                    """,
                    (normalized_attempt_id, normalized_user_id),
                ).fetchone()
                if attempt is None:
                    raise LearningDataError("Exam attempt was not found.")
                if attempt["status"] != "active":
                    raise LearningDataError("Exam attempt has already been submitted.")
                valid_questions = {
                    str(row["question_id"])
                    for row in connection.execute(
                        "SELECT question_id FROM attempt_questions WHERE attempt_id = ?",
                        (normalized_attempt_id,),
                    ).fetchall()
                }
                unexpected = sorted(set(answers) - valid_questions)
                if unexpected:
                    raise LearningDataError(
                        f"Exam answers include question(s) outside this attempt: {','.join(unexpected)}."
                    )
                unexpected_correctness = sorted(set(correctness) - valid_questions)
                if unexpected_correctness:
                    raise LearningDataError(
                        "Exam correctness includes question(s) outside this attempt: "
                        f"{','.join(unexpected_correctness)}."
                    )
                connection.executemany(
                    """
                    INSERT INTO attempt_answers (
                        attempt_id, question_id, selected_json, answered_at, is_correct
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (attempt_id, question_id) DO UPDATE SET
                        selected_json = excluded.selected_json,
                        answered_at = excluded.answered_at,
                        is_correct = excluded.is_correct
                    """,
                    [
                        (
                            normalized_attempt_id,
                            question_id,
                            json.dumps(selected, separators=(",", ":")),
                            now,
                            (
                                None
                                if correctness.get(question_id) is None
                                else int(bool(correctness[question_id]))
                            ),
                        )
                        for question_id, selected in answers.items()
                    ],
                )
                connection.execute(
                    """
                    UPDATE exam_attempts
                    SET status = 'submitted',
                        submitted_at = ?,
                        correct_count = ?,
                        score_percent = ?
                    WHERE attempt_id = ?
                    """,
                    (now, correct_count, score_percent, normalized_attempt_id),
                )
                connection.commit()
            except LearningDataError:
                connection.rollback()
                raise
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not submit exam attempt: {exc}") from exc

    def record_practice_answer(
        self,
        user_id: str,
        attempt_id: str,
        question_id: str,
        selected: list[str],
        *,
        is_correct: bool,
    ) -> dict[str, str | int | float | bool | None]:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        normalized_attempt_id = _required_text(attempt_id, "attempt_id", max_length=64)
        normalized_question_id = _required_text(question_id, "question_id", max_length=255)
        if not isinstance(selected, list) or not selected:
            raise LearningDataError("selected must be a non-empty list.")
        normalized_selected = [
            _required_text(label, "selected label", max_length=32).upper()
            for label in selected
        ]
        if len(set(normalized_selected)) != len(normalized_selected):
            raise LearningDataError("selected must not contain duplicate labels.")
        if not isinstance(is_correct, bool):
            raise LearningDataError("is_correct must be a boolean.")
        now = _utc_now()
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                attempt = connection.execute(
                    """
                    SELECT status, assessment_kind, question_count
                    FROM exam_attempts
                    WHERE attempt_id = ? AND user_id = ?
                    """,
                    (normalized_attempt_id, normalized_user_id),
                ).fetchone()
                if attempt is None:
                    raise LearningDataError("Practice attempt was not found.")
                if attempt["assessment_kind"] != "practice":
                    raise LearningDataError("Attempt is not a practice assessment.")
                if attempt["status"] != "active":
                    raise LearningDataError("Practice attempt has already been completed.")
                question = connection.execute(
                    """
                    SELECT 1 FROM attempt_questions
                    WHERE attempt_id = ? AND question_id = ?
                    """,
                    (normalized_attempt_id, normalized_question_id),
                ).fetchone()
                if question is None:
                    raise LearningDataError("Question is not part of this practice attempt.")
                existing = connection.execute(
                    """
                    SELECT 1 FROM attempt_answers
                    WHERE attempt_id = ? AND question_id = ?
                    """,
                    (normalized_attempt_id, normalized_question_id),
                ).fetchone()
                if existing is not None:
                    raise LearningDataError("Practice question has already been answered.")
                connection.execute(
                    """
                    INSERT INTO attempt_answers (
                        attempt_id, question_id, selected_json, answered_at, is_correct
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_attempt_id,
                        normalized_question_id,
                        json.dumps(normalized_selected, separators=(",", ":")),
                        now,
                        int(is_correct),
                    ),
                )
                totals = connection.execute(
                    """
                    SELECT COUNT(*) AS answered_count,
                           COALESCE(SUM(is_correct), 0) AS correct_count
                    FROM attempt_answers
                    WHERE attempt_id = ?
                    """,
                    (normalized_attempt_id,),
                ).fetchone()
                answered_count = int(totals["answered_count"])
                correct_count = int(totals["correct_count"])
                question_count = int(attempt["question_count"])
                completed = answered_count == question_count
                score_percent = round((correct_count / question_count) * 100, 2)
                if completed:
                    connection.execute(
                        """
                        UPDATE exam_attempts
                        SET status = 'submitted', submitted_at = ?,
                            correct_count = ?, score_percent = ?
                        WHERE attempt_id = ?
                        """,
                        (now, correct_count, score_percent, normalized_attempt_id),
                    )
                connection.commit()
            except LearningDataError:
                connection.rollback()
                raise
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not record practice answer: {exc}") from exc
        return {
            "attempt_id": normalized_attempt_id,
            "status": "submitted" if completed else "active",
            "answered_count": answered_count,
            "question_count": question_count,
            "correct_count": correct_count,
            "score_percent": score_percent,
            "completed": completed,
        }

    def list_attempts(
        self,
        user_id: str,
        *,
        course_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, str | int | float | None]]:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        normalized_course_id = (
            _required_text(course_id, "course_id", max_length=255)
            if course_id is not None
            else None
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise LearningDataError("limit must be an integer between 1 and 100.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise LearningDataError("offset must be a non-negative integer.")
        where = "WHERE user_id = ?"
        parameters: list[str | int] = [normalized_user_id]
        if normalized_course_id is not None:
            where += " AND course_id = ?"
            parameters.append(normalized_course_id)
        parameters.extend((limit, offset))
        with closing(_connect(self.database_path)) as connection:
            try:
                rows = connection.execute(
                    f"""
                    SELECT attempt_id, course_id, mode, assessment_kind,
                           content_version, status, started_at, deadline_at,
                           submitted_at, correct_count, question_count, score_percent
                    FROM exam_attempts
                    {where}
                    ORDER BY started_at DESC, attempt_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    parameters,
                ).fetchall()
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not list assessment attempts: {exc}") from exc
        return [dict(row) for row in rows]

    def count_attempts(self, user_id: str, *, course_id: str | None = None) -> int:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        normalized_course_id = (
            _required_text(course_id, "course_id", max_length=255)
            if course_id is not None
            else None
        )
        query = "SELECT COUNT(*) FROM exam_attempts WHERE user_id = ?"
        parameters: list[str] = [normalized_user_id]
        if normalized_course_id is not None:
            query += " AND course_id = ?"
            parameters.append(normalized_course_id)
        with closing(_connect(self.database_path)) as connection:
            try:
                return int(connection.execute(query, parameters).fetchone()[0])
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not count assessment attempts: {exc}") from exc

    def assessment_metrics(self, user_id: str, course_id: str) -> dict[str, int | float | None]:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        normalized_course_id = _required_text(course_id, "course_id", max_length=255)
        with closing(_connect(self.database_path)) as connection:
            try:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS completed_count,
                           AVG(score_percent) AS average_score,
                           MAX(score_percent) AS best_score
                    FROM exam_attempts
                    WHERE user_id = ? AND course_id = ?
                      AND status = 'submitted' AND score_percent IS NOT NULL
                    """,
                    (normalized_user_id, normalized_course_id),
                ).fetchone()
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not calculate assessment metrics: {exc}") from exc
        return {
            "completed_count": int(row["completed_count"]),
            "average_score": (
                None if row["average_score"] is None else round(float(row["average_score"]), 1)
            ),
            "best_score": (
                None if row["best_score"] is None else round(float(row["best_score"]), 1)
            ),
        }

    def attempt_detail(self, user_id: str, attempt_id: str) -> dict[str, object]:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        normalized_attempt_id = _required_text(attempt_id, "attempt_id", max_length=64)
        with closing(_connect(self.database_path)) as connection:
            try:
                attempt = connection.execute(
                    """
                    SELECT attempt_id, course_id, mode, assessment_kind,
                           content_version, status, started_at, deadline_at,
                           submitted_at, correct_count, question_count, score_percent
                    FROM exam_attempts
                    WHERE attempt_id = ? AND user_id = ?
                    """,
                    (normalized_attempt_id, normalized_user_id),
                ).fetchone()
                if attempt is None:
                    raise LearningDataError("Assessment attempt was not found.")
                if attempt["status"] != "submitted":
                    raise LearningDataError("Assessment results are available after completion.")
                rows = connection.execute(
                    """
                    SELECT q.position, q.question_id, a.selected_json,
                           a.answered_at, a.is_correct
                    FROM attempt_questions AS q
                    LEFT JOIN attempt_answers AS a
                      ON a.attempt_id = q.attempt_id AND a.question_id = q.question_id
                    WHERE q.attempt_id = ?
                    ORDER BY q.position
                    """,
                    (normalized_attempt_id,),
                ).fetchall()
            except LearningDataError:
                raise
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not load assessment results: {exc}") from exc
        questions: list[dict[str, object]] = []
        for row in rows:
            selected = json.loads(str(row["selected_json"])) if row["selected_json"] else []
            questions.append(
                {
                    "position": int(row["position"]),
                    "question_id": str(row["question_id"]),
                    "selected": selected,
                    "answered_at": row["answered_at"],
                    "is_correct": (
                        None if row["is_correct"] is None else bool(row["is_correct"])
                    ),
                }
            )
        return {"attempt": dict(attempt), "questions": questions}

    def set_learner_item(
        self,
        user_id: str,
        course_id: str,
        item_type: str,
        item_id: str,
        *,
        present: bool,
    ) -> bool:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        normalized_course_id = _required_text(course_id, "course_id", max_length=255)
        normalized_type = _required_text(item_type, "item_type", max_length=32).lower()
        if normalized_type not in ALLOWED_LEARNER_ITEM_TYPES:
            raise LearningDataError(
                "item_type must be one of: "
                f"{', '.join(sorted(ALLOWED_LEARNER_ITEM_TYPES))}."
            )
        normalized_item_id = _required_text(item_id, "item_id", max_length=255)
        if not isinstance(present, bool):
            raise LearningDataError("present must be a boolean.")
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                if present:
                    connection.execute(
                        """
                        INSERT INTO learner_items (
                            user_id, course_id, item_type, item_id, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT (user_id, course_id, item_type, item_id) DO NOTHING
                        """,
                        (
                            normalized_user_id,
                            normalized_course_id,
                            normalized_type,
                            normalized_item_id,
                            _utc_now(),
                        ),
                    )
                else:
                    connection.execute(
                        """
                        DELETE FROM learner_items
                        WHERE user_id = ? AND course_id = ?
                          AND item_type = ? AND item_id = ?
                        """,
                        (
                            normalized_user_id,
                            normalized_course_id,
                            normalized_type,
                            normalized_item_id,
                        ),
                    )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not update learner item: {exc}") from exc
        return present

    def list_learner_items(
        self,
        user_id: str,
        *,
        course_id: str | None = None,
        item_type: str | None = None,
    ) -> list[dict[str, str]]:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        conditions = ["user_id = ?"]
        parameters: list[str] = [normalized_user_id]
        if course_id is not None:
            conditions.append("course_id = ?")
            parameters.append(_required_text(course_id, "course_id", max_length=255))
        if item_type is not None:
            normalized_type = _required_text(item_type, "item_type", max_length=32).lower()
            if normalized_type not in ALLOWED_LEARNER_ITEM_TYPES:
                raise LearningDataError(
                    "item_type must be one of: "
                    f"{', '.join(sorted(ALLOWED_LEARNER_ITEM_TYPES))}."
                )
            conditions.append("item_type = ?")
            parameters.append(normalized_type)
        with closing(_connect(self.database_path)) as connection:
            try:
                rows = connection.execute(
                    f"""
                    SELECT course_id, item_type, item_id, created_at
                    FROM learner_items
                    WHERE {' AND '.join(conditions)}
                    ORDER BY created_at DESC, course_id, item_type, item_id
                    """,
                    parameters,
                ).fetchall()
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not list learner items: {exc}") from exc
        return [dict(row) for row in rows]

    def approve_identity(
        self,
        issuer: str,
        subject: str,
        label: str | None = None,
        role: str = "learner",
    ) -> None:
        normalized_issuer = _required_text(issuer, "issuer")
        normalized_subject = _required_text(subject, "subject")
        normalized_label = _optional_text(label, "label", max_length=255)
        normalized_role = _role(role)
        now = _utc_now()
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _protect_last_enabled_owner(
                    connection, normalized_issuer, normalized_subject, normalized_role
                )
                connection.execute(
                    """
                    INSERT INTO approved_identities (
                        issuer, subject, label, role, enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT (issuer, subject) DO UPDATE SET
                        label = excluded.label,
                        role = excluded.role,
                        enabled = 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        normalized_issuer,
                        normalized_subject,
                        normalized_label,
                        normalized_role,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "DELETE FROM pending_identities WHERE issuer = ? AND subject = ?",
                    (normalized_issuer, normalized_subject),
                )
                connection.commit()
            except LearningDataError:
                connection.rollback()
                raise
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not approve OIDC identity: {exc}") from exc

    def disable_identity(
        self,
        issuer: str,
        subject: str,
        *,
        allow_no_owner: bool = False,
    ) -> bool:
        normalized_issuer = _required_text(issuer, "issuer")
        normalized_subject = _required_text(subject, "subject")
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                target = connection.execute(
                    "SELECT role, enabled FROM approved_identities WHERE issuer = ? AND subject = ?",
                    (normalized_issuer, normalized_subject),
                ).fetchone()
                if (
                    target is not None
                    and target["enabled"] == 1
                    and target["role"] == "owner"
                    and not allow_no_owner
                ):
                    enabled_owner_count = connection.execute(
                        "SELECT COUNT(*) FROM approved_identities "
                        "WHERE role = 'owner' AND enabled = 1"
                    ).fetchone()[0]
                    if enabled_owner_count <= 1:
                        raise LearningDataError(
                            "Refusing to disable the last enabled owner. "
                            "Approve another owner first."
                        )
                cursor = connection.execute(
                    """
                    UPDATE approved_identities
                    SET enabled = 0, updated_at = ?
                    WHERE issuer = ? AND subject = ? AND enabled = 1
                    """,
                    (_utc_now(), normalized_issuer, normalized_subject),
                )
                connection.commit()
                return cursor.rowcount == 1
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not disable OIDC identity: {exc}") from exc

    def authenticate_approved_identity(
        self,
        issuer: str,
        subject: str,
        *,
        email: str | None = None,
        display_name: str | None = None,
    ) -> LearnerIdentity:
        normalized_issuer = _required_text(issuer, "issuer")
        normalized_subject = _required_text(subject, "subject")
        normalized_email = _optional_text(email, "email", max_length=320)
        normalized_name = _optional_text(display_name, "display_name", max_length=255)
        now = _utc_now()

        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                approval = connection.execute(
                    """
                    SELECT enabled, role FROM approved_identities
                    WHERE issuer = ? AND subject = ?
                    """,
                    (normalized_issuer, normalized_subject),
                ).fetchone()
                if approval is None or approval["enabled"] != 1:
                    connection.rollback()
                    raise LearningAuthorizationError("OIDC identity is not approved.")

                identity = connection.execute(
                    """
                    SELECT user_id FROM oidc_identities
                    WHERE issuer = ? AND subject = ?
                    """,
                    (normalized_issuer, normalized_subject),
                ).fetchone()
                if identity is None:
                    user_id = str(uuid.uuid4())
                    connection.execute(
                        """
                        INSERT INTO users (user_id, display_name, created_at, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (user_id, normalized_name, now, now),
                    )
                    connection.execute(
                        """
                        INSERT INTO oidc_identities (
                            issuer, subject, user_id, email, last_login_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (normalized_issuer, normalized_subject, user_id, normalized_email, now),
                    )
                else:
                    user_id = str(identity["user_id"])
                    connection.execute(
                        """
                        UPDATE users
                        SET display_name = COALESCE(?, display_name), updated_at = ?
                        WHERE user_id = ?
                        """,
                        (normalized_name, now, user_id),
                    )
                    connection.execute(
                        """
                        UPDATE oidc_identities
                        SET email = ?, last_login_at = ?
                        WHERE issuer = ? AND subject = ?
                        """,
                        (
                            normalized_email,
                            now,
                            normalized_issuer,
                            normalized_subject,
                        ),
                    )
                connection.commit()
                return LearnerIdentity(
                    user_id=user_id,
                    issuer=normalized_issuer,
                    subject=normalized_subject,
                    email=normalized_email,
                    display_name=normalized_name,
                    role=str(approval["role"]),
                )
            except LearningAuthorizationError:
                raise
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not persist OIDC identity: {exc}") from exc

    def list_approved_identities(
        self,
        *,
        limit: int = DEFAULT_ADMIN_PAGE_SIZE,
        offset: int = 0,
    ) -> list[dict[str, str | bool | None]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ADMIN_PAGE_SIZE:
            raise LearningDataError(
                f"limit must be an integer between 1 and {MAX_ADMIN_PAGE_SIZE}."
            )
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise LearningDataError("offset must be a non-negative integer.")
        with closing(_connect(self.database_path)) as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT a.issuer, a.subject, a.label, a.role, a.enabled,
                           a.created_at, a.updated_at, i.email, u.display_name
                    FROM approved_identities AS a
                    LEFT JOIN oidc_identities AS i
                      ON i.issuer = a.issuer AND i.subject = a.subject
                    LEFT JOIN users AS u ON u.user_id = i.user_id
                    ORDER BY a.enabled DESC, a.role DESC, a.issuer, a.subject
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not list approved OIDC identities: {exc}") from exc
        return [
            {
                "issuer": str(row["issuer"]),
                "subject": str(row["subject"]),
                "label": str(row["label"]) if row["label"] is not None else None,
                "role": str(row["role"]),
                "enabled": bool(row["enabled"]),
                "email": str(row["email"]) if row["email"] is not None else None,
                "display_name": (
                    str(row["display_name"]) if row["display_name"] is not None else None
                ),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    def count_approved_identities(self) -> int:
        with closing(_connect(self.database_path)) as connection:
            try:
                return int(
                    connection.execute("SELECT COUNT(*) FROM approved_identities").fetchone()[0]
                )
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not count approved OIDC identities: {exc}") from exc

    def record_pending_identity(
        self,
        issuer: str,
        subject: str,
        *,
        email: str | None = None,
        display_name: str | None = None,
    ) -> None:
        normalized_issuer = _required_text(issuer, "issuer")
        normalized_subject = _required_text(subject, "subject")
        normalized_email = _optional_text(email, "email", max_length=320)
        normalized_name = _optional_text(display_name, "display_name", max_length=255)
        now = _utc_now()
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM pending_identities "
                    "WHERE julianday(last_seen_at) < julianday('now', '-30 days')"
                )
                existing = connection.execute(
                    "SELECT 1 FROM pending_identities WHERE issuer = ? AND subject = ?",
                    (normalized_issuer, normalized_subject),
                ).fetchone()
                if existing is None:
                    pending_count = connection.execute(
                        "SELECT COUNT(*) FROM pending_identities"
                    ).fetchone()[0]
                    if pending_count >= MAX_PENDING_IDENTITIES:
                        raise LearningDataError(
                            "Pending identity capacity has been reached. "
                            "An owner must review the pending queue."
                        )
                connection.execute(
                    """
                    INSERT INTO pending_identities (
                        issuer, subject, email, display_name, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (issuer, subject) DO UPDATE SET
                        email = excluded.email,
                        display_name = excluded.display_name,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        normalized_issuer,
                        normalized_subject,
                        normalized_email,
                        normalized_name,
                        now,
                        now,
                    ),
                )
                connection.commit()
            except LearningDataError:
                connection.rollback()
                raise
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not record pending OIDC identity: {exc}") from exc

    def list_pending_identities(
        self,
        *,
        limit: int = DEFAULT_ADMIN_PAGE_SIZE,
        offset: int = 0,
    ) -> list[dict[str, str | None]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ADMIN_PAGE_SIZE:
            raise LearningDataError(
                f"limit must be an integer between 1 and {MAX_ADMIN_PAGE_SIZE}."
            )
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise LearningDataError("offset must be a non-negative integer.")
        with closing(_connect(self.database_path)) as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT issuer, subject, email, display_name, first_seen_at, last_seen_at
                    FROM pending_identities
                    ORDER BY last_seen_at DESC, issuer, subject
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not list pending OIDC identities: {exc}") from exc
        return [
            {
                "issuer": str(row["issuer"]),
                "subject": str(row["subject"]),
                "email": str(row["email"]) if row["email"] is not None else None,
                "display_name": (
                    str(row["display_name"]) if row["display_name"] is not None else None
                ),
                "first_seen_at": str(row["first_seen_at"]),
                "last_seen_at": str(row["last_seen_at"]),
            }
            for row in rows
        ]

    def count_pending_identities(self) -> int:
        with closing(_connect(self.database_path)) as connection:
            try:
                return int(
                    connection.execute("SELECT COUNT(*) FROM pending_identities").fetchone()[0]
                )
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not count pending OIDC identities: {exc}") from exc

    def approve_pending_identity(
        self,
        *,
        actor_issuer: str,
        actor_subject: str,
        issuer: str,
        subject: str,
        role: str = "learner",
    ) -> None:
        normalized_actor_issuer = _required_text(actor_issuer, "actor_issuer")
        normalized_actor_subject = _required_text(actor_subject, "actor_subject")
        normalized_issuer = _required_text(issuer, "issuer")
        normalized_subject = _required_text(subject, "subject")
        normalized_role = _role(role)
        now = _utc_now()
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                actor = connection.execute(
                    "SELECT role, enabled FROM approved_identities "
                    "WHERE issuer = ? AND subject = ?",
                    (normalized_actor_issuer, normalized_actor_subject),
                ).fetchone()
                if actor is None or actor["enabled"] != 1 or actor["role"] != "owner":
                    raise LearningAuthorizationError("Owner authorization is required.")
                pending = connection.execute(
                    """
                    SELECT email, display_name, last_seen_at
                    FROM pending_identities
                    WHERE issuer = ? AND subject = ?
                    """,
                    (normalized_issuer, normalized_subject),
                ).fetchone()
                if pending is None:
                    raise PendingIdentityNotFoundError("Pending OIDC identity was not found.")
                _protect_last_enabled_owner(
                    connection, normalized_issuer, normalized_subject, normalized_role
                )
                connection.execute(
                    """
                    INSERT INTO approved_identities (
                        issuer, subject, label, role, enabled, created_at, updated_at
                    ) VALUES (?, ?, NULL, ?, 1, ?, ?)
                    ON CONFLICT (issuer, subject) DO UPDATE SET
                        role = excluded.role,
                        enabled = 1,
                        updated_at = excluded.updated_at
                    """,
                    (normalized_issuer, normalized_subject, normalized_role, now, now),
                )
                identity = connection.execute(
                    "SELECT user_id FROM oidc_identities WHERE issuer = ? AND subject = ?",
                    (normalized_issuer, normalized_subject),
                ).fetchone()
                if identity is None:
                    user_id = str(uuid.uuid4())
                    connection.execute(
                        """
                        INSERT INTO users (user_id, display_name, created_at, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (user_id, pending["display_name"], now, now),
                    )
                    connection.execute(
                        """
                        INSERT INTO oidc_identities (
                            issuer, subject, user_id, email, last_login_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            normalized_issuer,
                            normalized_subject,
                            user_id,
                            pending["email"],
                            pending["last_seen_at"],
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE users
                        SET display_name = COALESCE(?, display_name), updated_at = ?
                        WHERE user_id = ?
                        """,
                        (pending["display_name"], now, identity["user_id"]),
                    )
                    connection.execute(
                        """
                        UPDATE oidc_identities
                        SET email = COALESCE(?, email), last_login_at = ?
                        WHERE issuer = ? AND subject = ?
                        """,
                        (
                            pending["email"],
                            pending["last_seen_at"],
                            normalized_issuer,
                            normalized_subject,
                        ),
                    )
                connection.execute(
                    "DELETE FROM pending_identities WHERE issuer = ? AND subject = ?",
                    (normalized_issuer, normalized_subject),
                )
                connection.execute(
                    """
                    INSERT INTO admin_audit_events (
                        actor_issuer, actor_subject, action, target_issuer,
                        target_subject, target_role, created_at
                    ) VALUES (?, ?, 'approve_pending_identity', ?, ?, ?, ?)
                    """,
                    (
                        normalized_actor_issuer,
                        normalized_actor_subject,
                        normalized_issuer,
                        normalized_subject,
                        normalized_role,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO admin_activity_events (
                        actor_issuer, actor_subject, action, target_issuer,
                        target_subject, target_role, target_enabled, created_at
                    ) VALUES (?, ?, 'approve_pending_identity', ?, ?, ?, 1, ?)
                    """,
                    (
                        normalized_actor_issuer,
                        normalized_actor_subject,
                        normalized_issuer,
                        normalized_subject,
                        normalized_role,
                        now,
                    ),
                )
                connection.commit()
            except (LearningAuthorizationError, LearningDataError):
                connection.rollback()
                raise
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not approve pending OIDC identity: {exc}") from exc

    def dismiss_pending_identity(
        self,
        *,
        actor_issuer: str,
        actor_subject: str,
        issuer: str,
        subject: str,
    ) -> None:
        normalized_actor_issuer = _required_text(actor_issuer, "actor_issuer")
        normalized_actor_subject = _required_text(actor_subject, "actor_subject")
        normalized_issuer = _required_text(issuer, "issuer")
        normalized_subject = _required_text(subject, "subject")
        now = _utc_now()
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                actor = connection.execute(
                    "SELECT role, enabled FROM approved_identities "
                    "WHERE issuer = ? AND subject = ?",
                    (normalized_actor_issuer, normalized_actor_subject),
                ).fetchone()
                if actor is None or actor["enabled"] != 1 or actor["role"] != "owner":
                    raise LearningAuthorizationError("Owner authorization is required.")
                deleted = connection.execute(
                    "DELETE FROM pending_identities WHERE issuer = ? AND subject = ?",
                    (normalized_issuer, normalized_subject),
                ).rowcount
                if deleted != 1:
                    raise PendingIdentityNotFoundError("Pending OIDC identity was not found.")
                connection.execute(
                    """
                    INSERT INTO admin_activity_events (
                        actor_issuer, actor_subject, action, target_issuer,
                        target_subject, created_at
                    ) VALUES (?, ?, 'dismiss_pending_identity', ?, ?, ?)
                    """,
                    (
                        normalized_actor_issuer,
                        normalized_actor_subject,
                        normalized_issuer,
                        normalized_subject,
                        now,
                    ),
                )
                connection.commit()
            except (LearningAuthorizationError, LearningDataError):
                connection.rollback()
                raise
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not dismiss pending identity: {exc}") from exc

    def update_identity_by_owner(
        self,
        *,
        actor_issuer: str,
        actor_subject: str,
        issuer: str,
        subject: str,
        role: str,
        enabled: bool,
        label: str | None,
        expected_updated_at: str | None = None,
    ) -> dict[str, str | bool | None]:
        normalized_actor_issuer = _required_text(actor_issuer, "actor_issuer")
        normalized_actor_subject = _required_text(actor_subject, "actor_subject")
        normalized_issuer = _required_text(issuer, "issuer")
        normalized_subject = _required_text(subject, "subject")
        normalized_role = _role(role)
        if not isinstance(enabled, bool):
            raise LearningDataError("enabled must be a boolean.")
        normalized_label = _optional_text(label, "label", max_length=255)
        normalized_expected = _optional_text(
            expected_updated_at, "expected_updated_at", max_length=64
        )
        now = _utc_now()
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                actor = connection.execute(
                    "SELECT role, enabled FROM approved_identities "
                    "WHERE issuer = ? AND subject = ?",
                    (normalized_actor_issuer, normalized_actor_subject),
                ).fetchone()
                if actor is None or actor["enabled"] != 1 or actor["role"] != "owner":
                    raise LearningAuthorizationError("Owner authorization is required.")
                target = connection.execute(
                    """
                    SELECT role, enabled, updated_at
                    FROM approved_identities
                    WHERE issuer = ? AND subject = ?
                    """,
                    (normalized_issuer, normalized_subject),
                ).fetchone()
                if target is None:
                    raise LearningDataError("Approved identity was not found.")
                if normalized_expected is not None and target["updated_at"] != normalized_expected:
                    raise LearningDataError(
                        "Approved identity changed since it was loaded. Refresh and try again."
                    )
                _protect_last_enabled_owner(
                    connection,
                    normalized_issuer,
                    normalized_subject,
                    normalized_role,
                    enabled,
                )
                connection.execute(
                    """
                    UPDATE approved_identities
                    SET label = ?, role = ?, enabled = ?, updated_at = ?
                    WHERE issuer = ? AND subject = ?
                    """,
                    (
                        normalized_label,
                        normalized_role,
                        int(enabled),
                        now,
                        normalized_issuer,
                        normalized_subject,
                    ),
                )
                if not enabled:
                    connection.execute(
                        """
                        UPDATE sessions
                        SET revoked_at = COALESCE(revoked_at, ?)
                        WHERE user_id IN (
                            SELECT user_id FROM oidc_identities
                            WHERE issuer = ? AND subject = ?
                        )
                        """,
                        (int(time.time()), normalized_issuer, normalized_subject),
                    )
                connection.execute(
                    """
                    INSERT INTO admin_activity_events (
                        actor_issuer, actor_subject, action, target_issuer,
                        target_subject, target_role, target_enabled, created_at
                    ) VALUES (?, ?, 'update_identity', ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_actor_issuer,
                        normalized_actor_subject,
                        normalized_issuer,
                        normalized_subject,
                        normalized_role,
                        int(enabled),
                        now,
                    ),
                )
                connection.commit()
            except (LearningAuthorizationError, LearningDataError):
                connection.rollback()
                raise
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not update approved identity: {exc}") from exc
        return {
            "issuer": normalized_issuer,
            "subject": normalized_subject,
            "label": normalized_label,
            "role": normalized_role,
            "enabled": enabled,
            "updated_at": now,
        }

    def set_identity_course_access(
        self,
        *,
        actor_issuer: str,
        actor_subject: str,
        issuer: str,
        subject: str,
        course_id: str,
        enabled: bool,
    ) -> dict[str, str | bool]:
        normalized_actor_issuer = _required_text(actor_issuer, "actor_issuer")
        normalized_actor_subject = _required_text(actor_subject, "actor_subject")
        normalized_issuer = _required_text(issuer, "issuer")
        normalized_subject = _required_text(subject, "subject")
        normalized_course_id = _required_text(course_id, "course_id", max_length=255)
        if not isinstance(enabled, bool):
            raise LearningDataError("enabled must be a boolean.")
        now = _utc_now()
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                actor = connection.execute(
                    "SELECT role, enabled FROM approved_identities "
                    "WHERE issuer = ? AND subject = ?",
                    (normalized_actor_issuer, normalized_actor_subject),
                ).fetchone()
                if actor is None or actor["enabled"] != 1 or actor["role"] != "owner":
                    raise LearningAuthorizationError("Owner authorization is required.")
                target = connection.execute(
                    "SELECT 1 FROM approved_identities WHERE issuer = ? AND subject = ?",
                    (normalized_issuer, normalized_subject),
                ).fetchone()
                if target is None:
                    raise LearningDataError("Approved identity was not found.")
                connection.execute(
                    """
                    INSERT INTO identity_course_access (
                        issuer, subject, course_id, enabled, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (issuer, subject, course_id) DO UPDATE SET
                        enabled = excluded.enabled,
                        updated_at = excluded.updated_at
                    """,
                    (
                        normalized_issuer,
                        normalized_subject,
                        normalized_course_id,
                        int(enabled),
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO admin_activity_events (
                        actor_issuer, actor_subject, action, target_issuer,
                        target_subject, target_course_id, target_enabled, created_at
                    ) VALUES (?, ?, 'update_course_access', ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_actor_issuer,
                        normalized_actor_subject,
                        normalized_issuer,
                        normalized_subject,
                        normalized_course_id,
                        int(enabled),
                        now,
                    ),
                )
                connection.commit()
            except (LearningAuthorizationError, LearningDataError):
                connection.rollback()
                raise
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not update course access: {exc}") from exc
        return {
            "issuer": normalized_issuer,
            "subject": normalized_subject,
            "course_id": normalized_course_id,
            "enabled": enabled,
            "updated_at": now,
        }

    def identity_course_access(self, issuer: str, subject: str) -> dict[str, bool]:
        normalized_issuer = _required_text(issuer, "issuer")
        normalized_subject = _required_text(subject, "subject")
        with closing(_connect(self.database_path)) as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT course_id, enabled
                    FROM identity_course_access
                    WHERE issuer = ? AND subject = ?
                    ORDER BY course_id
                    """,
                    (normalized_issuer, normalized_subject),
                ).fetchall()
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not list course access: {exc}") from exc
        return {str(row["course_id"]): bool(row["enabled"]) for row in rows}

    def course_access_allowed(self, issuer: str, subject: str, course_id: str) -> bool:
        normalized_issuer = _required_text(issuer, "issuer")
        normalized_subject = _required_text(subject, "subject")
        normalized_course_id = _required_text(course_id, "course_id", max_length=255)
        with closing(_connect(self.database_path)) as connection:
            try:
                row = connection.execute(
                    """
                    SELECT enabled FROM identity_course_access
                    WHERE issuer = ? AND subject = ? AND course_id = ?
                    """,
                    (normalized_issuer, normalized_subject, normalized_course_id),
                ).fetchone()
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not check course access: {exc}") from exc
        return True if row is None else bool(row["enabled"])

    def list_admin_activity(
        self,
        *,
        limit: int = DEFAULT_ADMIN_PAGE_SIZE,
        offset: int = 0,
    ) -> list[dict[str, str | bool | int | None]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise LearningDataError("limit must be an integer between 1 and 100.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise LearningDataError("offset must be a non-negative integer.")
        with closing(_connect(self.database_path)) as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT activity.event_id, activity.action,
                           activity.actor_issuer, activity.actor_subject,
                           actor_user.display_name AS actor_name,
                           activity.target_issuer, activity.target_subject,
                           target_user.display_name AS target_name,
                           target_identity.email AS target_email,
                           activity.target_role, activity.target_course_id,
                           activity.target_enabled, activity.created_at
                    FROM admin_activity_events AS activity
                    LEFT JOIN oidc_identities AS actor_identity
                      ON actor_identity.issuer = activity.actor_issuer
                     AND actor_identity.subject = activity.actor_subject
                    LEFT JOIN users AS actor_user
                      ON actor_user.user_id = actor_identity.user_id
                    LEFT JOIN oidc_identities AS target_identity
                      ON target_identity.issuer = activity.target_issuer
                     AND target_identity.subject = activity.target_subject
                    LEFT JOIN users AS target_user
                      ON target_user.user_id = target_identity.user_id
                    ORDER BY activity.created_at DESC, activity.event_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not list admin activity: {exc}") from exc
        events: list[dict[str, str | bool | int | None]] = []
        for row in rows:
            event = dict(row)
            event["target_enabled"] = (
                None if row["target_enabled"] is None else bool(row["target_enabled"])
            )
            events.append(event)
        return events

    def count_admin_activity(self) -> int:
        with closing(_connect(self.database_path)) as connection:
            try:
                return int(
                    connection.execute("SELECT COUNT(*) FROM admin_activity_events").fetchone()[0]
                )
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not count admin activity: {exc}") from exc

    def create_login_transaction(
        self,
        *,
        return_path: str = "/",
        lifetime_seconds: int = 600,
    ) -> tuple[str, str, str]:
        if not return_path.startswith("/") or return_path.startswith("//"):
            raise LearningDataError("return_path must be a local absolute path.")
        if lifetime_seconds < 60 or lifetime_seconds > 900:
            raise LearningDataError("Login transaction lifetime must be between 60 and 900 seconds.")
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        now = int(time.time())
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM login_transactions WHERE expires_at <= ?", (now,))
                connection.execute(
                    """
                    INSERT INTO login_transactions (
                        state_hash, nonce, code_verifier, return_path, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _token_hash(state),
                        nonce,
                        code_verifier,
                        return_path,
                        now,
                        now + lifetime_seconds,
                    ),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not create login transaction: {exc}") from exc
        return state, nonce, code_verifier

    def consume_login_transaction(self, state: str) -> LoginTransaction | None:
        normalized_state = _required_text(state, "state", max_length=512)
        now = int(time.time())
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    DELETE FROM login_transactions
                    WHERE state_hash = ?
                    RETURNING nonce, code_verifier, return_path, expires_at
                    """,
                    (_token_hash(normalized_state),),
                ).fetchone()
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not consume login transaction: {exc}") from exc
        if row is None or int(row["expires_at"]) <= now:
            return None
        return LoginTransaction(
            nonce=str(row["nonce"]),
            code_verifier=str(row["code_verifier"]),
            return_path=str(row["return_path"]),
        )

    def create_session(
        self,
        user_id: str,
        *,
        idle_seconds: int,
        lifetime_seconds: int,
    ) -> tuple[str, str]:
        normalized_user_id = _required_text(user_id, "user_id", max_length=64)
        if idle_seconds < 300 or lifetime_seconds < idle_seconds:
            raise LearningDataError("Session lifetimes are invalid.")
        session_token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        now = int(time.time())
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM sessions WHERE absolute_expires_at <= ? OR revoked_at IS NOT NULL",
                    (now,),
                )
                connection.execute(
                    """
                    INSERT INTO sessions (
                        session_hash, user_id, csrf_hash, created_at, last_seen_at,
                        idle_expires_at, absolute_expires_at, revoked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        _token_hash(session_token),
                        normalized_user_id,
                        _token_hash(csrf_token),
                        now,
                        now,
                        now + idle_seconds,
                        now + lifetime_seconds,
                    ),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not create learner session: {exc}") from exc
        return session_token, csrf_token

    def get_session(
        self,
        session_token: str,
        *,
        idle_seconds: int,
        csrf_token: str | None = None,
    ) -> LearnerSession | None:
        if not isinstance(session_token, str) or not session_token:
            return None
        now = int(time.time())
        with closing(_connect(self.database_path)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT s.user_id, s.csrf_hash, s.idle_expires_at, s.absolute_expires_at,
                           a.issuer, a.subject, a.role, a.enabled,
                           i.email, u.display_name
                    FROM sessions AS s
                    JOIN users AS u ON u.user_id = s.user_id
                    JOIN oidc_identities AS i ON i.user_id = s.user_id
                    JOIN approved_identities AS a
                      ON a.issuer = i.issuer AND a.subject = i.subject
                    WHERE s.session_hash = ? AND s.revoked_at IS NULL
                    """,
                    (_token_hash(session_token),),
                ).fetchone()
                if (
                    row is None
                    or int(row["enabled"]) != 1
                    or int(row["idle_expires_at"]) <= now
                    or int(row["absolute_expires_at"]) <= now
                ):
                    connection.execute(
                        "UPDATE sessions SET revoked_at = ? WHERE session_hash = ? AND revoked_at IS NULL",
                        (now, _token_hash(session_token)),
                    )
                    connection.commit()
                    return None
                if csrf_token is not None and not secrets.compare_digest(
                    str(row["csrf_hash"]), _token_hash(csrf_token)
                ):
                    connection.rollback()
                    return None
                idle_expires_at = min(now + idle_seconds, int(row["absolute_expires_at"]))
                connection.execute(
                    "UPDATE sessions SET last_seen_at = ?, idle_expires_at = ? WHERE session_hash = ?",
                    (now, idle_expires_at, _token_hash(session_token)),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise LearningDataError(f"Could not validate learner session: {exc}") from exc
        return LearnerSession(
            user_id=str(row["user_id"]),
            issuer=str(row["issuer"]),
            subject=str(row["subject"]),
            email=str(row["email"]) if row["email"] is not None else None,
            display_name=str(row["display_name"]) if row["display_name"] is not None else None,
            role=str(row["role"]),
            csrf_token=csrf_token,
        )

    def revoke_session(self, session_token: str) -> bool:
        if not isinstance(session_token, str) or not session_token:
            return False
        with closing(_connect(self.database_path)) as connection:
            try:
                cursor = connection.execute(
                    "UPDATE sessions SET revoked_at = ? WHERE session_hash = ? AND revoked_at IS NULL",
                    (int(time.time()), _token_hash(session_token)),
                )
                connection.commit()
                return cursor.rowcount == 1
            except sqlite3.Error as exc:
                raise LearningDataError(f"Could not revoke learner session: {exc}") from exc
