from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LOCAL_HASH_MODEL = "local-hash-v1"
LOCAL_HASH_DIMENSIONS = 384
MIN_SEMANTIC_SCORE = 0.18
MAX_QUESTION_LENGTH = 500
MAX_SNIPPET_LENGTH = 420
WORD_RE = re.compile(r"[A-Za-z0-9_'-]+")
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    }
)


class StudyRagError(RuntimeError):
    """Raised when the read-only project index cannot serve grounded evidence."""


@dataclass(frozen=True)
class RagResult:
    source_file: str
    page_start: int | None
    page_end: int | None
    source_title: str | None
    locator: str | None
    snippet: str
    keyword_coverage: float
    semantic_score: float
    combined_score: float

    def public_payload(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "source_title": self.source_title,
            "locator": self.locator,
            "snippet": self.snippet,
        }


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in WORD_RE.findall(text)]


def _local_hash_vector(text: str, dimensions: int = LOCAL_HASH_DIMENSIONS) -> list[float]:
    vector = [0.0] * dimensions
    tokens = _tokenize(text)
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [round(value / norm, 8) for value in vector]


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return numerator / (left_norm * right_norm)


def _record_vector(record: dict[str, Any]) -> list[float] | None:
    embedding = record.get("embedding")
    if not isinstance(embedding, dict):
        return None
    if embedding.get("model") != LOCAL_HASH_MODEL:
        return None
    if embedding.get("dimensions") != LOCAL_HASH_DIMENSIONS:
        return None
    if embedding.get("provider") not in {None, "local"}:
        return None
    raw_vector = embedding.get("vector")
    if not isinstance(raw_vector, list) or len(raw_vector) != LOCAL_HASH_DIMENSIONS:
        return None
    if not all(isinstance(value, int | float) and not isinstance(value, bool) for value in raw_vector):
        return None
    return [float(value) for value in raw_vector]


def _snippet(text: str, query_tokens: set[str]) -> str:
    cleaned = re.sub(r"~\s*Get Latest Exclusive Courses at https://hide01\.ir\s*~", " ", text)
    cleaned = re.sub(r"## Page \d+", ". ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    candidates = [sentence.strip() for sentence in sentences if sentence.strip()]
    if not candidates:
        return ""
    best = max(
        candidates,
        key=lambda sentence: (
            len(query_tokens & set(_tokenize(sentence))),
            -abs(len(sentence) - 240),
        ),
    )
    if len(best) <= MAX_SNIPPET_LENGTH:
        return best
    truncated = best[: MAX_SNIPPET_LENGTH - 3].rstrip()
    boundary = truncated.rfind(" ")
    if boundary >= MAX_SNIPPET_LENGTH // 2:
        truncated = truncated[:boundary]
    return f"{truncated}..."


class StudyRagService:
    def __init__(self, index_path: Path, project_id: str) -> None:
        self.index_path = index_path.expanduser().resolve()
        self.project_id = project_id
        self.records = self._load_records()

    def _load_records(self) -> list[dict[str, Any]]:
        try:
            handle = self.index_path.open("r", encoding="utf-8")
        except OSError as exc:
            raise StudyRagError(f"Could not open project index: {exc}") from exc
        records: list[dict[str, Any]] = []
        with handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise StudyRagError(f"Invalid project index JSONL at line {line_number}.") from exc
                if not isinstance(record, dict):
                    raise StudyRagError(f"Invalid project index record at line {line_number}.")
                if record.get("project_id") != self.project_id:
                    continue
                if record.get("source_type") not in {"pdf_chunk", "epub_chunk"}:
                    continue
                page_start = record.get("page_start")
                page_end = record.get("page_end")
                if (page_start is None) != (page_end is None):
                    raise StudyRagError(
                        f"Study index record at line {line_number} has an incomplete page range."
                    )
                if page_start is not None and (
                    not isinstance(page_start, int)
                    or isinstance(page_start, bool)
                    or not isinstance(page_end, int)
                    or isinstance(page_end, bool)
                    or page_start <= 0
                    or page_end < page_start
                ):
                    raise StudyRagError(
                        f"Study index record at line {line_number} has invalid page metadata."
                    )
                if not isinstance(record.get("text"), str) or not isinstance(record.get("source_file"), str):
                    raise StudyRagError(f"Study index record at line {line_number} is incomplete.")
                module_ids = record.get("module_ids")
                if module_ids is not None and (
                    not isinstance(module_ids, list)
                    or not module_ids
                    or not all(isinstance(item, str) and item.strip() for item in module_ids)
                ):
                    raise StudyRagError(
                        f"Study index record at line {line_number} has invalid module_ids."
                    )
                records.append(record)
        if not records:
            raise StudyRagError(f"Project index has no study chunks for {self.project_id!r}.")
        return records

    def retrieve(
        self,
        question: str,
        *,
        page_start: int | None = None,
        page_end: int | None = None,
        module_id: str | None = None,
        limit: int = 4,
    ) -> list[RagResult]:
        if not isinstance(question, str) or not question.strip():
            raise StudyRagError("question must be a non-empty string.")
        normalized_question = question.strip()
        if len(normalized_question) > MAX_QUESTION_LENGTH:
            raise StudyRagError(f"question must not exceed {MAX_QUESTION_LENGTH} characters.")
        if module_id is None:
            if page_start is None or page_end is None or page_start <= 0 or page_end < page_start:
                raise StudyRagError("Chapter page range is invalid.")
        elif not isinstance(module_id, str) or not module_id.strip():
            raise StudyRagError("module_id must be a non-empty string.")
        if limit <= 0 or limit > 8:
            raise StudyRagError("limit must be between 1 and 8.")

        tokens = set(_tokenize(normalized_question))
        query_tokens = {token for token in tokens if token not in STOPWORDS} or tokens
        if not query_tokens:
            raise StudyRagError("question must contain at least one word or number.")
        query_vector = _local_hash_vector(normalized_question)
        results: list[RagResult] = []
        for record in self.records:
            record_start = record.get("page_start")
            record_end = record.get("page_end")
            if module_id is not None:
                if module_id.strip() not in record.get("module_ids", []):
                    continue
            else:
                if record_start is None or record_end is None:
                    continue
                if int(record_end) < int(page_start) or int(record_start) > int(page_end):
                    continue
            text = f"{record.get('title', '')} {record['text']}"
            record_tokens = _tokenize(text)
            token_set = set(record_tokens)
            keyword_score = sum(1 for token in record_tokens if token in query_tokens)
            keyword_coverage = len(query_tokens & token_set) / len(query_tokens)
            vector = _record_vector(record)
            semantic_score = _cosine_similarity(query_vector, vector) if vector is not None else 0.0
            if keyword_score <= 0 and semantic_score < MIN_SEMANTIC_SCORE:
                continue
            keyword_frequency = min(keyword_score, 20) / 20
            combined_score = (
                keyword_coverage * 5.0
                + keyword_frequency * 2.0
                + max(semantic_score, 0.0) * 6.0
            )
            results.append(
                RagResult(
                    source_file=str(record["source_file"]),
                    page_start=(
                        int(record_start) if record_start is not None else None
                        if module_id is not None
                        else max(int(record_start), int(page_start))
                    ),
                    page_end=(
                        int(record_end) if record_end is not None else None
                        if module_id is not None
                        else min(int(record_end), int(page_end))
                    ),
                    source_title=(
                        str(record["source_title"])
                        if isinstance(record.get("source_title"), str)
                        else None
                    ),
                    locator=(
                        str(record["locator"])
                        if isinstance(record.get("locator"), str)
                        else None
                    ),
                    snippet=_snippet(str(record["text"]), query_tokens),
                    keyword_coverage=keyword_coverage,
                    semantic_score=semantic_score,
                    combined_score=combined_score,
                )
            )
        results.sort(
            key=lambda result: (
                -result.combined_score,
                result.source_file,
                result.page_start or 0,
            )
        )
        return results[:limit]
