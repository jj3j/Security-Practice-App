-- Schema fixture from 89ceb54c83b25bf242bf650d0d0aa02549ca8d20
BEGIN TRANSACTION;
CREATE TABLE admin_activity_events (
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
CREATE TABLE admin_audit_events (
                    event_id INTEGER PRIMARY KEY,
                    actor_issuer TEXT NOT NULL,
                    actor_subject TEXT NOT NULL,
                    action TEXT NOT NULL CHECK (action IN ('approve_pending_identity')),
                    target_issuer TEXT NOT NULL,
                    target_subject TEXT NOT NULL,
                    target_role TEXT NOT NULL CHECK (target_role IN ('learner', 'owner')),
                    created_at TEXT NOT NULL
                ) STRICT;
CREATE TABLE approved_identities (
                    issuer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    label TEXT,
                    role TEXT NOT NULL DEFAULT 'learner' CHECK (role IN ('learner', 'owner')),
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (issuer, subject)
                ) STRICT;
CREATE TABLE attempt_answers (
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
CREATE TABLE attempt_questions (
                    attempt_id TEXT NOT NULL REFERENCES exam_attempts(attempt_id) ON DELETE CASCADE,
                    position INTEGER NOT NULL CHECK (position >= 0),
                    question_id TEXT NOT NULL,
                    PRIMARY KEY (attempt_id, position),
                    UNIQUE (attempt_id, question_id)
                ) STRICT;
CREATE TABLE chapter_progress (
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
CREATE TABLE exam_attempts (
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
CREATE TABLE flashcard_progress (
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
CREATE TABLE identity_course_access (
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
CREATE TABLE learner_items (
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    course_id TEXT NOT NULL,
                    item_type TEXT NOT NULL CHECK (item_type IN ('lesson', 'question')),
                    item_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, course_id, item_type, item_id)
                ) STRICT;
CREATE TABLE login_transactions (
                    state_hash TEXT PRIMARY KEY,
                    nonce TEXT NOT NULL,
                    code_verifier TEXT NOT NULL,
                    return_path TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL CHECK (expires_at > created_at)
                ) STRICT;
CREATE TABLE metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                ) STRICT;
INSERT INTO "metadata" VALUES('schema_version','4');
INSERT INTO "metadata" VALUES('created_at','2026-09-05T07:54:14.514519+00:00');
CREATE TABLE oidc_identities (
                    issuer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    email TEXT,
                    last_login_at TEXT NOT NULL,
                    PRIMARY KEY (issuer, subject)
                ) STRICT;
CREATE TABLE pending_identities (
                    issuer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    email TEXT,
                    display_name TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (issuer, subject)
                ) STRICT;
CREATE TABLE sessions (
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
CREATE TABLE users (
                    user_id TEXT PRIMARY KEY,
                    display_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                ) STRICT;
CREATE INDEX oidc_identities_user_idx
                    ON oidc_identities(user_id);
CREATE INDEX admin_audit_events_created_idx
                    ON admin_audit_events(created_at DESC, event_id DESC);
CREATE INDEX admin_activity_events_created_idx
                    ON admin_activity_events(created_at DESC, event_id DESC);
CREATE INDEX login_transactions_expiry_idx
                    ON login_transactions(expires_at);
CREATE INDEX sessions_user_idx ON sessions(user_id);
CREATE INDEX sessions_expiry_idx
                    ON sessions(absolute_expires_at);
CREATE INDEX learner_items_user_created_idx
                    ON learner_items(user_id, created_at DESC);
CREATE INDEX exam_attempts_user_started_idx
                    ON exam_attempts(user_id, started_at DESC);
COMMIT;
