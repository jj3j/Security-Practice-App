from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from merge_question_bank import merge_question_bank  # noqa: E402
from practice_api import PracticeApi  # noqa: E402
from practice_db import QuestionBankImport, QuestionRepository, import_jsonl_banks  # noqa: E402


GMON_PROJECT_ID = "GIAC GMON"
GMON_BANK_PATH = ROOT / "question_banks" / "gmon-questions.jsonl"


def _record(project_id: str, question_id: str, question: str) -> dict[str, object]:
    return {
        "answer": ["A"],
        "choices": [
            {"label": "A", "text": "Correct"},
            {"label": "B", "text": "Incorrect"},
        ],
        "explanation": "Fixture explanation",
        "page_end": None,
        "page_start": None,
        "project_id": project_id,
        "question": question,
        "question_id": question_id,
        "question_number": 1,
        "source_file": "fixture.md",
    }


class GmonQuestionBankTest(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            json.loads(line)
            for line in GMON_BANK_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_generated_bank_preserves_all_four_validated_exams(self) -> None:
        self.assertEqual(len(self.records), 328)
        self.assertEqual([record["question_number"] for record in self.records], list(range(1, 329)))
        self.assertTrue(all(record["project_id"] == GMON_PROJECT_ID for record in self.records))
        self.assertEqual(Counter(record["answer"][0] for record in self.records), Counter("ABCD" * 82))
        self.assertEqual(len({record["question_id"] for record in self.records}), 328)
        self.assertTrue(all(len(record["choices"]) == 4 for record in self.records))
        self.assertTrue(
            all([choice["label"] for choice in record["choices"]] == ["A", "B", "C", "D"] for record in self.records)
        )
        self.assertTrue(all("Objective:" in record["explanation"] for record in self.records))
        self.assertTrue(all("Source: GIAC GMON / SEC511" in record["explanation"] for record in self.records))
        self.assertTrue(all(6 <= record["page_start"] <= record["page_end"] <= 957 for record in self.records))
        self.assertEqual(self.records[0]["question_id"], "gmon-sample-exam-01-q001")
        self.assertEqual(self.records[81]["question_id"], "gmon-sample-exam-01-q082")
        self.assertEqual(self.records[82]["question_id"], "gmon-sample-exam-02-q001")
        self.assertEqual(self.records[-1]["question_id"], "gmon-sample-exam-04-q082")

        for record in self.records:
            stored_hash = record["content_hash"]
            canonical_record = dict(record)
            canonical_record.pop("content_hash")
            canonical = json.dumps(
                canonical_record, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
            self.assertEqual(stored_hash, hashlib.sha256(canonical.encode("utf-8")).hexdigest())

    def test_atomic_merge_preserves_existing_banks_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            gdsa_path = temporary / "gdsa.jsonl"
            cissp_path = temporary / "cissp.jsonl"
            database_path = temporary / "questions.sqlite3"
            gdsa_path.write_text(
                json.dumps(_record("SEC530 - GDSA", "gdsa-1", "Existing GDSA question")) + "\n",
                encoding="utf-8",
            )
            cissp_path.write_text(
                json.dumps(_record("CISSP", "cissp-1", "Existing CISSP question")) + "\n",
                encoding="utf-8",
            )
            import_jsonl_banks(
                [
                    QuestionBankImport(gdsa_path, "SEC530 - GDSA"),
                    QuestionBankImport(cissp_path, "CISSP"),
                ],
                database_path,
            )
            repository = QuestionRepository(database_path, "SEC530 - GDSA")
            gdsa_before = repository.load_questions("SEC530 - GDSA")
            cissp_before = repository.load_questions("CISSP")

            self.assertTrue(
                merge_question_bank(
                    GMON_BANK_PATH,
                    database_path,
                    GMON_PROJECT_ID,
                    expected_count=328,
                )
            )
            repository = QuestionRepository(database_path, "SEC530 - GDSA")
            self.assertEqual(
                repository.available_projects(),
                {"CISSP": 1, GMON_PROJECT_ID: 328, "SEC530 - GDSA": 1},
            )
            self.assertEqual(repository.load_questions("SEC530 - GDSA"), gdsa_before)
            self.assertEqual(repository.load_questions("CISSP"), cissp_before)
            self.assertEqual(len(repository.load_questions(GMON_PROJECT_ID)), 328)

            database_hash = hashlib.sha256(database_path.read_bytes()).hexdigest()
            self.assertFalse(
                merge_question_bank(
                    GMON_BANK_PATH,
                    database_path,
                    GMON_PROJECT_ID,
                    expected_count=328,
                )
            )
            self.assertEqual(database_hash, hashlib.sha256(database_path.read_bytes()).hexdigest())

    def test_gmon_course_exposes_four_practice_and_exam_bundles(self) -> None:
        application = object.__new__(PracticeApi)
        application.repository = SimpleNamespace(project_id="SEC530 - GDSA")
        application.study_catalogs = {}
        config = application._course_configs()[GMON_PROJECT_ID]

        self.assertTrue(config.practice_available)
        self.assertTrue(config.exam_available)
        self.assertEqual(config.target_question_count, 82)
        self.assertEqual(config.duration_seconds, 3 * 60 * 60)
        self.assertEqual(config.passing_score_percent, 74)
        self.assertEqual(
            [bundle["question_count"] for bundle in config.practice_bundles(328)],
            [82, 82, 82, 82],
        )
        self.assertEqual(
            [bundle["question_count"] for bundle in config.exam_bundles(328)],
            [82, 82, 82, 82],
        )


if __name__ == "__main__":
    unittest.main()
