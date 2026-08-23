from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend" / "modern-midnight.css").read_text(encoding="utf-8")


class ModernMidnightFrontendTests(unittest.TestCase):
    def test_single_authoritative_stylesheet_is_loaded(self) -> None:
        self.assertIn('/modern-midnight.css?v=1', INDEX)
        self.assertNotIn('/styles.css?', INDEX)
        self.assertNotIn('/layout-v17.css?', INDEX)

    def test_study_information_architecture_remains_separate(self) -> None:
        study_views = (
            "studyView",
            "chapterView",
            "lessonView",
            "flashcardView",
            "groundedView",
        )
        for view_id in study_views:
            self.assertEqual(INDEX.count(f'id="{view_id}"'), 1)
        self.assertIn(
            '["chapterView", "lessonView", "flashcardView", "groundedView"]',
            APP,
        )

    def test_modern_midnight_geometry_tokens(self) -> None:
        compact = re.sub(r"\s+", "", CSS)
        self.assertIn("--rail:80px", compact)
        self.assertIn("--topbar:64px", compact)
        self.assertIn("--radius:8px", compact)
        self.assertIn("grid-template-columns:repeat(4,1fr)", compact)
        self.assertIn("bottom:0", compact)

    def test_mobile_question_map_drawer_is_accessible_and_wired(self) -> None:
        for element_id in (
            "questionMapToggle",
            "questionMapBackdrop",
            "questionSessionPanel",
            "questionMapClose",
        ):
            self.assertEqual(INDEX.count(f'id="{element_id}"'), 1)
        self.assertIn('aria-controls="questionSessionPanel"', INDEX)
        self.assertIn('aria-expanded="false"', INDEX)
        self.assertIn("function openQuestionMap()", APP)
        self.assertIn("function closeQuestionMap(", APP)
        self.assertIn('setAttribute("aria-expanded", "true")', APP)
        self.assertIn('setAttribute("aria-expanded", "false")', APP)
        self.assertIn('event.key === "Escape"', APP)


if __name__ == "__main__":
    unittest.main()