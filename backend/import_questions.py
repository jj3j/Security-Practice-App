from __future__ import annotations

import argparse
import sys
from pathlib import Path

from practice_db import PracticeDataError, QuestionBankImport, import_jsonl_banks, import_jsonl_database


def _parse_bank(value: str) -> QuestionBankImport:
    if "=" not in value:
        raise PracticeDataError("--bank must use PROJECT_ID=PATH format.")
    project_id, source = value.split("=", 1)
    if not project_id.strip() or not source.strip():
        raise PracticeDataError("--bank must include both PROJECT_ID and PATH.")
    return QuestionBankImport(source_path=Path(source), project_id=project_id.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import a validated question JSONL file into SQLite.")
    parser.add_argument("--source", type=Path, help="Path to questions.jsonl.")
    parser.add_argument("--database", type=Path, required=True, help="Destination SQLite database path.")
    parser.add_argument("--project", help="Exact project_id expected in every JSONL record.")
    parser.add_argument(
        "--bank",
        action="append",
        default=[],
        help=(
            "Import one bank using PROJECT_ID=PATH. Repeat for multi-course databases. "
            "When provided, --source and --project are ignored."
        ),
    )
    parser.add_argument(
        "--skip-invalid",
        action="store_true",
        help="Exclude invalid records and report every exclusion instead of failing the import.",
    )
    args = parser.parse_args(argv)

    try:
        if args.bank:
            results = import_jsonl_banks(
                [_parse_bank(value) for value in args.bank],
                args.database,
                skip_invalid=args.skip_invalid,
            )
        else:
            if args.source is None or not args.project:
                raise PracticeDataError("--source and --project are required unless --bank is provided.")
            results = (
                import_jsonl_database(
                    args.source,
                    args.database,
                    args.project,
                    skip_invalid=args.skip_invalid,
                ),
            )
    except PracticeDataError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    total_imported = sum(result.question_count for result in results)
    total_skipped = sum(result.skipped_count for result in results)
    for result in results:
        print(f"Project imported: {result.project_id}")
        print(f"Questions imported: {result.question_count}")
        print(f"Questions skipped: {result.skipped_count}")
        for issue in result.issues:
            print(f"Skipped: {result.project_id}: {issue}", file=sys.stderr)
    print(f"Total questions imported: {total_imported}")
    print(f"Total questions skipped: {total_skipped}")
    print(f"Database written: {results[0].database_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
