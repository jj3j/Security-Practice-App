from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from learning_db import LearningRepository, LearningDataError, initialize_learning_database
from practice_api import PracticeApi
from practice_db import QuestionRepository, QuestionBankImport, import_jsonl_banks

COURSE = "SEC530 - GDSA"
OTHER = "GIAC GMON"


def record(qid, course=COURSE):
    return {"question_id": qid, "project_id": course, "question_number": 1,
            "question": "Fixture question " + qid,
            "choices": [{"label": "A", "text": "First"}, {"label": "B", "text": "Second"}],
            "answer": ["A"], "explanation": "SECRET EXPLANATION", "source_file": "fixture.md",
            "page_start": None, "page_end": None}


class LearnerStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "learner.sqlite3"
        initialize_learning_database(self.db)
        self.repo = LearningRepository(self.db)
        self.identities = []
        self.sessions = []
        for subject in ("alice", "bob"):
            self.repo.approve_identity("issuer", subject)
            identity = self.repo.authenticate_approved_identity("issuer", subject)
            self.identities.append(identity)
            self.sessions.append(self.repo.create_session(identity.user_id, idle_seconds=600, lifetime_seconds=3600))
        imports = []
        for course, ids in ((COURSE, ["q1", "q2"]), (OTHER, ["g1"])):
            bank = self.root / (ids[0] + ".jsonl")
            bank.write_text("\n".join(json.dumps(record(q, course)) for q in ids))
            imports.append(QuestionBankImport(bank, course))
        qdb = self.root / "questions.sqlite3"
        import_jsonl_banks(imports, qdb)
        chapters = [
            {"chapter_id": "c1", "title": "Chapter one", "number": 1,
             "sections": [{"section_id": "s1", "heading": "First lesson"}, {"section_id": "s2", "heading": "Second lesson"}],
             "flashcards": [{"flashcard_id": "f1", "chapter_id": "c1", "section_id": "s1"}]},
            {"chapter_id": "c2", "title": "Chapter two", "number": 2,
             "sections": [{"section_id": "s3", "heading": "Third lesson"}], "flashcards": []}]
        self.catalog = SimpleNamespace(course_id=COURSE, payload={"title": "Course", "chapters": chapters},
                                       chapter=lambda cid: next((c for c in chapters if c["chapter_id"] == cid), None),
                                       flashcards_by_id={"f1": chapters[0]["flashcards"][0]})
        self.auth = SimpleNamespace(repository=self.repo, config=SimpleNamespace(origin="https://local.test"),
                                    session=lambda token, csrf_token=None: self.repo.get_session(token, idle_seconds=600, csrf_token=csrf_token))
        self.app = PracticeApi(QuestionRepository(qdb, COURSE), self.auth, study_catalogs={COURSE: self.catalog})

    def request(self, path, data=None, *, user=0, query=None, csrf=True, authenticated=True):
        raw = json.dumps(data).encode() if data is not None else b""
        token, csrf_token = self.sessions[user]
        environ = {"REQUEST_METHOD": "POST" if data is not None else "GET", "PATH_INFO": path,
                   "QUERY_STRING": urlencode(query or {}), "CONTENT_LENGTH": str(len(raw)),
                   "CONTENT_TYPE": "application/json", "wsgi.input": io.BytesIO(raw),
                   "HTTP_ORIGIN": "https://local.test"}
        if authenticated:
            environ["HTTP_COOKIE"] = f"__Host-gdsa_session={token}; __Host-gdsa_csrf={csrf_token}"
        if csrf:
            environ["HTTP_X_CSRF_TOKEN"] = csrf_token
        status = []
        body = b"".join(self.app(environ, lambda s, h: status.append(int(s.split()[0]))))
        return status[0], json.loads(body)

    def start(self, **kwargs):
        status, body = self.request("/api/exam/start", {"course_id": COURSE}, **kwargs)
        self.assertEqual(status, 200, body)
        return body["attempt_id"]

    def save(self, aid, **data):
        return self.request("/api/attempt-state", {"attempt_id": aid, **data})

    def state(self, aid, **kwargs):
        return self.request("/api/attempt-state", query={"attempt_id": aid}, **kwargs)

    def dashboard(self, **kwargs):
        status, body = self.request("/api/dashboard", query={"course_id": COURSE}, **kwargs)
        self.assertEqual(status, 200, body)
        return body

    def assert_no_secrets(self, value):
        if isinstance(value, dict):
            self.assertFalse(set(value) & {"correct_answer", "correct_answer_text", "explanation", "is_correct", "answer"}, value)
            for child in value.values():
                self.assert_no_secrets(child)
        elif isinstance(value, list):
            for child in value:
                self.assert_no_secrets(child)
        self.assertNotIn("SECRET EXPLANATION", str(value))

    def location(self, chapter="c1", section="s2", **kwargs):
        return self.request("/api/study/location", {"course_id": COURSE, "chapter_id": chapter, "section_id": section}, **kwargs)

    def test_resume_location_survives_repository_reopen_and_preserves_completion(self):
        self.assertIsNone(self.dashboard()["resume"])
        self.repo.set_chapter_progress(self.identities[0].user_id, COURSE, "c1", "completed")
        before = self.repo.list_chapter_progress(self.identities[0].user_id, COURSE)[0]["completed_at"]
        self.assertEqual(self.location()[0], 200)
        self.app.learning_repository = LearningRepository(self.db)
        resume = self.dashboard()["resume"]
        self.assertEqual((resume["course_id"], resume["chapter_id"], resume["section_id"], resume["title"]), (COURSE, "c1", "s2", "Second lesson"))
        datetime.fromisoformat(resume["last_viewed_at"])
        progress = self.repo.list_chapter_progress(self.identities[0].user_id, COURSE)[0]
        self.assertEqual(progress["status"], "completed")
        self.assertEqual(progress["completed_at"], before)
        self.assertIsNone(self.dashboard(user=1)["resume"])
        self.assertEqual(self.location("c2", None)[0], 200)
        self.assertEqual(self.dashboard()["resume"]["chapter_id"], "c2")

    def test_location_validates_chapter_section_and_course(self):
        for chapter, section in (("missing", None), ("c1", "s3"), ("c1", []), ("c1", "")):
            self.assertEqual(self.location(chapter, section)[0], 400)
        self.assertEqual(self.request("/api/study/location", {"course_id": OTHER, "chapter_id": "c1"})[0], 400)
        self.assertIsNone(self.dashboard()["resume"])

    def test_autosave_flags_restore_clear_and_no_secrets(self):
        aid = self.start()
        self.assertEqual(self.save(aid, answers={"q1": ["B"]}, flags={"q2": True})[0], 200)
        self.assertEqual(self.save(aid, answers={"q2": ["A"]})[0], 200)
        self.app.learning_repository = LearningRepository(self.db)
        status, state = self.state(aid)
        self.assertEqual(status, 200)
        self.assertEqual(state["answers"], {"q1": ["B"], "q2": ["A"]})
        self.assertTrue(state["flags"]["q2"])
        self.assert_no_secrets(state)
        self.assertEqual(self.save(aid, answers={"q1": []}, flags={"q2": False})[0], 200)
        self.assertEqual(self.state(aid)[1]["answers"]["q1"], [])
        self.assertFalse(self.state(aid)[1]["flags"]["q2"])
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute("SELECT DISTINCT is_correct FROM attempt_answers").fetchall(), [(None,)])

    def test_other_device_session_restores_same_learner(self):
        aid = self.start()
        self.save(aid, answers={"q1": ["A"]})
        self.sessions[0] = self.repo.create_session(self.identities[0].user_id, idle_seconds=600, lifetime_seconds=3600)
        self.assertEqual(self.state(aid)[1]["answers"]["q1"], ["A"])

    def test_ownership_course_access_authentication_and_csrf(self):
        aid = self.start()
        self.assertEqual(self.state(aid, user=1)[0], 400)
        self.assertEqual(self.request("/api/attempt-state", {"attempt_id": aid, "flags": {"q1": True}}, user=1)[0], 400)
        self.assertEqual(self.state(aid, authenticated=False)[0], 401)
        self.assertEqual(self.request("/api/attempt-state", {"attempt_id": aid}, csrf=False)[0], 403)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO identity_course_access VALUES (?, ?, ?, 0, ?)", ("issuer", "alice", COURSE, "now"))
        self.assertEqual(self.state(aid)[0], 403)
        self.assertEqual(self.save(aid, answers={"q1": ["A"]})[0], 403)
        self.assertEqual(self.location()[0], 403)
        self.assertEqual(self.request("/api/dashboard", query={"course_id": COURSE})[0], 403)
        self.assertEqual(self.request("/api/score", {"course_id": COURSE, "attempt_id": aid, "answers": {}})[0], 403)

    def test_rejects_invalid_state_atomically(self):
        aid = self.start()
        for update in ({"answers": {"g1": ["A"]}}, {"answers": {"q1": ["Z"]}},
                       {"answers": {"q1": "A"}}, {"flags": {"q1": 1}},
                       {"answers": {"q1": ["A"]}, "flags": {"g1": True}}, {"status": "submitted"}):
            self.assertEqual(self.save(aid, **update)[0], 400, update)
        self.assertTrue(all(not v for v in self.state(aid)[1]["answers"].values()))

    def test_deadline_rejects_saves_but_preserves_late_submission(self):
        aid = self.start()
        self.save(aid, answers={"q1": ["A"]})
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE exam_attempts SET deadline_at=? WHERE attempt_id=?", (past, aid))
        self.assertTrue(self.state(aid)[1]["deadline_passed"])
        self.assertEqual(self.save(aid, answers={"q1": ["B"]})[0], 400)
        self.assertEqual(self.save(aid, flags={"q1": True})[0], 400)
        status, result = self.request("/api/score", {"course_id": COURSE, "attempt_id": aid, "answers": {}})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["correct_count"], 1)
        self.assertEqual(result["graded_count"], 2)

    def test_submission_uses_saved_answers_and_locks_mutations(self):
        aid = self.start()
        self.save(aid, answers={"q1": ["A"]}, flags={"q2": True})
        status, result = self.request("/api/score", {"course_id": COURSE, "attempt_id": aid, "answers": {"q2": ["A"]}})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["percent"], 100)
        self.assertEqual(self.save(aid, answers={"q1": ["B"]})[0], 400)
        self.assertEqual(self.state(aid)[0], 400)
        self.assertEqual(self.request("/api/score", {"course_id": COURSE, "attempt_id": aid, "answers": {}})[0], 400)
        self.assertTrue(self.repo.attempt_state(self.identities[0].user_id, aid)["flags"]["q2"])
        status, review = self.request("/api/attempt-detail", query={"attempt_id": aid})
        self.assertEqual(status, 200, review)
        self.assertIn("explanation", review["results"][0])

    def test_pre_submission_leak_paths_and_wrong_course_are_blocked(self):
        aid = self.start()
        self.assert_no_secrets(self.state(aid)[1])
        self.assert_no_secrets(self.dashboard())
        for path, data in (("/api/answer", {"course_id": COURSE, "question_id": "q1", "selected": ["A"]}),
                           ("/api/score", {"course_id": COURSE, "answers": {}}),
                           ("/api/score", {"course_id": OTHER, "attempt_id": aid, "answers": {}})):
            status, body = self.request(path, data)
            self.assertEqual(status, 400, body)
            self.assert_no_secrets(body)
        status, body = self.request("/api/attempt-detail", query={"attempt_id": aid})
        self.assertEqual(status, 400)
        self.assert_no_secrets(body)

    def test_recommendation_priority_and_identifiers(self):
        self.assertEqual(self.dashboard()["recommended_next_step"]["reason"], "missing_performance")
        self.location()
        recommendation = self.dashboard()["recommended_next_step"]
        self.assertEqual((recommendation["action_type"], recommendation["section_id"]), ("open_study", "s2"))
        aid = self.start()
        self.assertEqual(self.dashboard()["recommended_next_step"]["attempt_id"], aid)
        self.request("/api/score", {"course_id": COURSE, "attempt_id": aid, "answers": {"q1": ["A"], "q2": ["A"]}})
        self.repo.set_chapter_progress(self.identities[0].user_id, COURSE, "c1", "completed")
        self.assertEqual(self.dashboard()["recommended_next_step"]["flashcard_id"], "f1")
        for _ in range(3):
            self.repo.record_flashcard_review(self.identities[0].user_id, COURSE, "f1", known=True)
        self.assertEqual(self.dashboard()["recommended_next_step"]["chapter_id"], "c2")
        self.repo.set_chapter_progress(self.identities[0].user_id, COURSE, "c2", "completed")
        self.assertEqual(self.dashboard()["recommended_next_step"]["action_type"], "choose_exam")
        self.assertEqual(self.dashboard()["recommended_next_step"], self.dashboard()["recommended_next_step"])

    def test_study_only_and_no_available_pathway(self):
        from unittest.mock import patch
        with patch.object(self.app.repository, "available_projects", return_value={}):
            next_step = self.dashboard()["recommended_next_step"]
            self.assertEqual(next_step["chapter_id"], "c1")
            for chapter in ("c1", "c2"):
                self.repo.set_chapter_progress(self.identities[0].user_id, COURSE, chapter, "completed")
            for _ in range(3):
                self.repo.record_flashcard_review(self.identities[0].user_id, COURSE, "f1", known=True)
            self.assertIsNone(self.dashboard()["recommended_next_step"])

    def test_weak_performance_recommends_practice(self):
        aid = self.start()
        self.request("/api/score", {"course_id": COURSE, "attempt_id": aid, "answers": {"q1": ["B"], "q2": ["B"]}})
        next_step = self.dashboard()["recommended_next_step"]
        self.assertEqual(next_step["reason"], "weak_performance")
        self.assertEqual(next_step["action_type"], "start_practice")
        self.assertTrue(next_step["bundle_id"])

    def test_active_practice_restoration_does_not_expose_grading(self):
        status, payload = self.request("/api/practice/start", {"course_id": COURSE})
        self.assertEqual(status, 200, payload)
        aid = payload["attempt_id"]
        status, result = self.request("/api/answer", {"course_id": COURSE, "attempt_id": aid, "question_id": "q1", "selected": ["A"]})
        self.assertEqual(status, 200, result)
        status, restored = self.state(aid)
        self.assertEqual(status, 200, restored)
        self.assert_no_secrets(restored)
        self.assertEqual(restored["answers"]["q1"], ["A"])
        self.assertEqual(self.dashboard()["recommended_next_step"]["assessment_kind"], "practice")
        self.assertEqual(self.save(aid, answers={"q1": ["B"]})[0], 400)

    def test_parallel_disjoint_saves_do_not_overwrite_each_other(self):
        from concurrent.futures import ThreadPoolExecutor
        aid = self.start()
        uid = self.identities[0].user_id
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(self.repo.save_attempt_state, uid, aid, COURSE, {q: ["A"]}, {q: True}) for q in ("q1", "q2")]
            for job in jobs:
                job.result()
        restored = self.state(aid)[1]
        self.assertTrue(all(restored["flags"].values()))
        self.assertTrue(all(v == ["A"] for v in restored["answers"].values()))

    def test_stale_resume_section_falls_back_to_chapter(self):
        self.location()
        self.catalog.payload["chapters"][0]["sections"] = [{"section_id": "replacement", "heading": "New"}]
        self.assertIsNone(self.dashboard()["resume"]["section_id"])
        self.assertEqual(self.dashboard()["resume"]["title"], "Chapter one")
        self.assertIsNone(self.dashboard()["recommended_next_step"]["section_id"])

    def test_migrate_populated_previous_schema_and_rerun_without_data_loss(self):
        legacy = self.root / "v4.sqlite3"
        with closing(sqlite3.connect(legacy)) as c:
            c.executescript((ROOT / "tests/fixtures/learner_v4.sql").read_text())
        repo = LearningRepository(legacy)
        repo.approve_identity("legacy", "user")
        identity = repo.authenticate_approved_identity("legacy", "user")
        uid = identity.user_id
        repo.set_chapter_progress(uid, COURSE, "c1", "completed")
        repo.set_learner_item(uid, COURSE, "lesson", "s1", present=True)
        repo.record_flashcard_review(uid, COURSE, "f1", known=True)
        aid = repo.create_exam_attempt(uid, COURSE, "mock", "old", ["q1"], duration_seconds=100)
        repo.submit_exam_attempt(uid, aid["attempt_id"], {"q1": ["A"]}, correct_count=1, score_percent=100, correctness={"q1": True})
        repo.create_exam_attempt(uid, COURSE, "mock", "old", ["q2"], duration_seconds=100)
        with closing(sqlite3.connect(legacy)) as c:
            tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'metadata'")]
            before = {t: ([r[1] for r in c.execute(f"PRAGMA table_info({t})")], c.execute(f"SELECT * FROM {t}").fetchall()) for t in tables}
        initialize_learning_database(legacy)
        initialize_learning_database(legacy)
        with closing(sqlite3.connect(legacy)) as c:
            self.assertEqual(c.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], "5")
            for table, (columns, rows) in before.items():
                self.assertEqual(c.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall(), rows, table)
            self.assertEqual(c.execute("SELECT last_section_id FROM chapter_progress").fetchone(), (None,))
            self.assertEqual(c.execute("SELECT DISTINCT flagged FROM attempt_questions").fetchall(), [(0,)])
        repo.record_study_location(uid, COURSE, "c1", "s2")
        self.assertEqual(repo.list_chapter_progress(uid, COURSE)[0]["last_section_id"], "s2")


if __name__ == "__main__":
    unittest.main()
