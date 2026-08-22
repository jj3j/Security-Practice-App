#!/usr/bin/env python3
"""Validate the complete persistent production study-content bundle."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from study_content import StudyContentError, load_study_catalog  # noqa: E402
from study_rag import StudyRagError, StudyRagService  # noqa: E402


CATALOGS = (
    ("sec530-study.json", "SEC530 - GDSA", None),
    ("cissp-study.json", "CISSP", "cissp-study-sources.jsonl"),
    ("gmon-study.json", "GIAC GMON", "gmon-study-sources.jsonl"),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("content_directory", type=Path)
    args = parser.parse_args()
    content_directory = args.content_directory.expanduser().resolve()

    if not content_directory.is_dir():
        print(
            f"Persistent study content directory is missing: {content_directory}",
            file=sys.stderr,
        )
        return 1

    try:
        for catalog_name, project_id, index_name in CATALOGS:
            load_study_catalog(content_directory / catalog_name, project_id)
            if index_name is not None:
                StudyRagService(content_directory / index_name, project_id)
    except (StudyContentError, StudyRagError) as exc:
        print(f"Persistent study content validation failed: {exc}", file=sys.stderr)
        return 1

    print(f"Persistent study content validation passed: {content_directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
