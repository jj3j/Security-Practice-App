"""Convert the four validated GMON Markdown exams into hosted-app JSONL."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


PROJECT_ID = "GIAC GMON"
EXAM_COUNT = 4
QUESTIONS_PER_EXAM = 82
QUESTION_RE = re.compile(r"(?m)^(\d+)\.\s+(.+)$")
CHOICE_RE = re.compile(r"(?m)^([A-D])\.\s+(.+)$")
ANSWER_RE = re.compile(r"(?m)^##\s+(\d+)\.\s+([A-D])\s+[—-]\s+(.+)$")
FIELD_PATTERNS = {
    "explanation": re.compile(r"(?m)^-\s+\*\*Explanation:\*\*\s+(.+)$"),
    "distractors": re.compile(r"(?m)^-\s+\*\*Why the other choices are wrong:\*\*\s+(.+)$"),
    "objective": re.compile(r"(?m)^-\s+\*\*Objective:\*\*\s+(.+)$"),
    "source": re.compile(r"(?m)^-\s+\*\*Source:\*\*\s+(.+)$"),
}


def _blocks(pattern: re.Pattern[str], text: str) -> list[tuple[re.Match[str], str]]:
    matches = list(pattern.finditer(text))
    return [
        (match, text[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(text)])
        for index, match in enumerate(matches)
    ]


def _required_field(block: str, name: str, exam: int, number: int) -> str:
    match = FIELD_PATTERNS[name].search(block)
    if match is None:
        raise ValueError(f"Exam {exam} question {number}: missing {name} field")
    return match.group(1).strip()


def _source_pages(source: str, exam: int, number: int) -> tuple[str, int, int]:
    book = re.search(r"Book\s+511\.([1-5])", source)
    pages = re.search(r"PDF Page(?:\(s\)|s)?\s+([^;]+)", source, re.IGNORECASE)
    if book is None or pages is None:
        raise ValueError(f"Exam {exam} question {number}: unparseable source citation: {source}")
    page_values = [int(value) for value in re.findall(r"\d+", pages.group(1))]
    if not page_values:
        raise ValueError(f"Exam {exam} question {number}: source citation has no PDF pages")
    return f"GIAC GMON / SEC511, Book 511.{book.group(1)}", min(page_values), max(page_values)


def build_records(source_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for exam in range(1, EXAM_COUNT + 1):
        question_path = source_root / "Questions" / f"GMON_Sample_Exam_{exam:02d}_Questions.md"
        answer_path = source_root / "Answers" / f"GMON_Sample_Exam_{exam:02d}_Answers.md"
        question_blocks = _blocks(QUESTION_RE, question_path.read_text(encoding="utf-8"))
        answer_blocks = _blocks(ANSWER_RE, answer_path.read_text(encoding="utf-8"))
        if len(question_blocks) != QUESTIONS_PER_EXAM or len(answer_blocks) != QUESTIONS_PER_EXAM:
            raise ValueError(
                f"Exam {exam}: expected {QUESTIONS_PER_EXAM} questions and answers, "
                f"found {len(question_blocks)} and {len(answer_blocks)}"
            )

        for index, ((question_match, question_body), (answer_match, answer_body)) in enumerate(
            zip(question_blocks, answer_blocks, strict=True), start=1
        ):
            question_number = int(question_match.group(1))
            answer_number = int(answer_match.group(1))
            if question_number != index or answer_number != index:
                raise ValueError(
                    f"Exam {exam}: expected question/answer {index}, found {question_number}/{answer_number}"
                )
            choices = [
                {"label": label, "text": text.strip()}
                for label, text in CHOICE_RE.findall(question_body)
            ]
            if [choice["label"] for choice in choices] != ["A", "B", "C", "D"]:
                raise ValueError(f"Exam {exam} question {index}: choices are not exactly A-D")

            explanation = _required_field(answer_body, "explanation", exam, index)
            distractors = _required_field(answer_body, "distractors", exam, index)
            objective = _required_field(answer_body, "objective", exam, index)
            source = _required_field(answer_body, "source", exam, index)
            source_file, page_start, page_end = _source_pages(source, exam, index)
            record: dict[str, Any] = {
                "answer": [answer_match.group(2)],
                "choices": choices,
                "explanation": (
                    f"{explanation}\n\nWhy the other choices are wrong: {distractors}"
                    f"\n\nObjective: {objective}\n\nSource: {source}"
                ),
                "page_end": page_end,
                "page_start": page_start,
                "project_id": PROJECT_ID,
                "question": question_match.group(2).strip(),
                "question_id": f"gmon-sample-exam-{exam:02d}-q{index:03d}",
                "question_number": (exam - 1) * QUESTIONS_PER_EXAM + index,
                "source_file": source_file,
            }
            canonical = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            record["content_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            records.append(record)

    expected_total = EXAM_COUNT * QUESTIONS_PER_EXAM
    if len(records) != expected_total:
        raise ValueError(f"Expected {expected_total} GMON records, found {len(records)}")
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = build_records(args.source_root.expanduser().resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    )
    args.output.write_text(payload, encoding="utf-8", newline="\n")
    print(f"Wrote {len(records)} GMON questions: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
