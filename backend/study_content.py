from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class StudyContentError(RuntimeError):
    """Raised when immutable study content is missing or invalid."""


def _required_text(value: Any, field: str, *, max_length: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StudyContentError(f"{field} must be a non-empty string.")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise StudyContentError(f"{field} must not exceed {max_length} characters.")
    return normalized


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise StudyContentError(f"{field} must be a positive integer.")
    return value


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field)


def _object_list(value: Any, field: str, *, allow_empty: bool = True) -> list[dict[str, Any]]:
    if not isinstance(value, list) or (not allow_empty and not value):
        qualifier = "a list" if allow_empty else "a non-empty list"
        raise StudyContentError(f"{field} must be {qualifier}.")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise StudyContentError(f"{field}[{index}] must be an object.")
    return value


def _citation(
    value: Any,
    field: str,
    *,
    schema_version: int,
    source_ids: set[str],
    chapter_page_start: int | None,
    chapter_page_end: int | None,
) -> None:
    if not isinstance(value, dict):
        raise StudyContentError(f"{field} must be an object.")
    _required_text(value.get("source_file"), f"{field}.source_file", max_length=2048)
    page_start = _optional_positive_int(value.get("page_start"), f"{field}.page_start")
    page_end = _optional_positive_int(value.get("page_end"), f"{field}.page_end")
    if (page_start is None) != (page_end is None):
        raise StudyContentError(f"{field}.page_start and page_end must be provided together.")
    if page_start is not None and page_end is not None and page_end < page_start:
        raise StudyContentError(f"{field}.page_end must not precede page_start.")
    if schema_version == 2:
        if value.get("source_pdf") is not None:
            _required_text(value.get("source_pdf"), f"{field}.source_pdf", max_length=512)
        if page_start is None or page_end is None:
            raise StudyContentError(f"{field} must include a page range.")
        if chapter_page_start is None or chapter_page_end is None:
            raise StudyContentError(f"{field} requires its chapter page range.")
        if page_start < chapter_page_start or page_end > chapter_page_end:
            raise StudyContentError(f"{field} must stay within its chapter page range.")
        return
    source_id = _required_text(value.get("source_id"), f"{field}.source_id", max_length=128)
    if source_id not in source_ids:
        raise StudyContentError(f"{field}.source_id does not identify a configured source.")
    _required_text(value.get("source_title"), f"{field}.source_title", max_length=512)
    _required_text(value.get("locator"), f"{field}.locator", max_length=512)


@dataclass(frozen=True)
class StudyCatalog:
    payload: dict[str, Any]
    chapters_by_id: dict[str, dict[str, Any]]
    flashcards_by_id: dict[str, dict[str, Any]]

    @property
    def course_id(self) -> str:
        return str(self.payload["course_id"])

    @property
    def project_id(self) -> str:
        return str(self.payload["project_id"])

    @property
    def content_version(self) -> str:
        return str(self.payload["content_version"])

    def chapter(self, chapter_id: str) -> dict[str, Any] | None:
        return self.chapters_by_id.get(chapter_id)

    def flashcard(self, flashcard_id: str) -> dict[str, Any] | None:
        return self.flashcards_by_id.get(flashcard_id)

    def public_catalog(self) -> dict[str, Any]:
        chapters: list[dict[str, Any]] = []
        for chapter in self.payload["chapters"]:
            chapter_payload = {
                    "chapter_id": chapter["chapter_id"],
                    "number": chapter["number"],
                    "title": chapter["title"],
                    "summary": chapter["summary"],
                    "source_label": chapter.get("source_label"),
                    "lesson_count": len(chapter["sections"]),
                    "flashcard_count": len(chapter["flashcards"]),
                }
            if chapter.get("page_start") is not None:
                chapter_payload["page_start"] = chapter["page_start"]
                chapter_payload["page_end"] = chapter["page_end"]
            chapters.append(chapter_payload)
        return {
            "course_id": self.course_id,
            "project_id": self.project_id,
            "content_version": self.content_version,
            "title": self.payload["title"],
            "unit_labels": self.payload.get(
                "unit_labels", {"singular": "Chapter", "plural": "Chapters"}
            ),
            "chapters": chapters,
        }


def load_study_catalog(path: Path, expected_project_id: str) -> StudyCatalog:
    resolved = path.expanduser().resolve()
    try:
        parsed = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StudyContentError(f"Could not read study content: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise StudyContentError(f"Study content is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise StudyContentError("Study content root must be a JSON object.")

    project_id = _required_text(parsed.get("project_id"), "project_id", max_length=255)
    if project_id != expected_project_id:
        raise StudyContentError(
            f"Study content project_id {project_id!r} does not match {expected_project_id!r}."
        )
    _required_text(parsed.get("course_id"), "course_id", max_length=255)
    _required_text(parsed.get("content_version"), "content_version", max_length=128)
    _required_text(parsed.get("title"), "title", max_length=255)
    schema_version = _positive_int(parsed.get("schema_version"), "schema_version")
    if schema_version not in {2, 3}:
        raise StudyContentError("schema_version must be 2 or 3.")
    source_ids: set[str] = set()
    if schema_version == 2:
        _required_text(parsed.get("source_pdf"), "source_pdf", max_length=512)
    else:
        unit_labels = parsed.get("unit_labels")
        if not isinstance(unit_labels, dict):
            raise StudyContentError("unit_labels must be an object.")
        _required_text(unit_labels.get("singular"), "unit_labels.singular", max_length=32)
        _required_text(unit_labels.get("plural"), "unit_labels.plural", max_length=32)
        sources = _object_list(parsed.get("sources"), "sources", allow_empty=False)
        for source_index, source in enumerate(sources):
            source_prefix = f"sources[{source_index}]"
            source_id = _required_text(
                source.get("source_id"), f"{source_prefix}.source_id", max_length=128
            )
            if source_id in source_ids:
                raise StudyContentError(f"Duplicate source_id: {source_id}")
            source_ids.add(source_id)
            _required_text(source.get("title"), f"{source_prefix}.title", max_length=512)
            source_type = _required_text(
                source.get("source_type"), f"{source_prefix}.source_type", max_length=32
            )
            if source_type not in {"pdf", "epub"}:
                raise StudyContentError(f"{source_prefix}.source_type must be pdf or epub.")
            _required_text(
                source.get("source_file"), f"{source_prefix}.source_file", max_length=2048
            )

    chapters = parsed.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise StudyContentError("chapters must be a non-empty list.")

    chapters_by_id: dict[str, dict[str, Any]] = {}
    flashcards_by_id: dict[str, dict[str, Any]] = {}
    section_ids: set[str] = set()
    knowledge_check_ids: set[str] = set()
    visual_ids: set[str] = set()
    chapter_numbers: set[int] = set()
    for chapter_index, chapter in enumerate(chapters):
        prefix = f"chapters[{chapter_index}]"
        if not isinstance(chapter, dict):
            raise StudyContentError(f"{prefix} must be an object.")
        chapter_id = _required_text(chapter.get("chapter_id"), f"{prefix}.chapter_id", max_length=128)
        if chapter_id in chapters_by_id:
            raise StudyContentError(f"Duplicate chapter_id: {chapter_id}")
        number = _positive_int(chapter.get("number"), f"{prefix}.number")
        if number in chapter_numbers:
            raise StudyContentError(f"Duplicate chapter number: {number}")
        chapter_numbers.add(number)
        _required_text(chapter.get("title"), f"{prefix}.title", max_length=255)
        _required_text(chapter.get("summary"), f"{prefix}.summary")
        page_start = _optional_positive_int(chapter.get("page_start"), f"{prefix}.page_start")
        page_end = _optional_positive_int(chapter.get("page_end"), f"{prefix}.page_end")
        if schema_version == 2 and (page_start is None or page_end is None):
            raise StudyContentError(f"{prefix} must include a page range.")
        if (page_start is None) != (page_end is None):
            raise StudyContentError(f"{prefix}.page_start and page_end must be provided together.")
        if page_start is not None and page_end is not None and page_end < page_start:
            raise StudyContentError(f"{prefix}.page_end must not precede page_start.")
        if schema_version == 3:
            _required_text(chapter.get("source_label"), f"{prefix}.source_label", max_length=1024)
            source_scope = _object_list(
                chapter.get("source_scope"), f"{prefix}.source_scope", allow_empty=False
            )
            for scope_index, scope in enumerate(source_scope):
                scope_prefix = f"{prefix}.source_scope[{scope_index}]"
                scope_source_id = _required_text(
                    scope.get("source_id"), f"{scope_prefix}.source_id", max_length=128
                )
                if scope_source_id not in source_ids:
                    raise StudyContentError(
                        f"{scope_prefix}.source_id does not identify a configured source."
                    )
                scope_start = _positive_int(
                    scope.get("page_start"), f"{scope_prefix}.page_start"
                )
                scope_end = _positive_int(scope.get("page_end"), f"{scope_prefix}.page_end")
                if scope_end < scope_start:
                    raise StudyContentError(
                        f"{scope_prefix}.page_end must not precede page_start."
                    )
            rag_scope = chapter.get("rag_scope")
            if not isinstance(rag_scope, dict):
                raise StudyContentError(f"{prefix}.rag_scope must be an object.")
            _required_text(
                rag_scope.get("module_id"), f"{prefix}.rag_scope.module_id", max_length=128
            )

        objectives = chapter.get("objectives")
        if not isinstance(objectives, list) or not objectives:
            raise StudyContentError(f"{prefix}.objectives must be a non-empty list.")
        for index, objective in enumerate(objectives):
            _required_text(objective, f"{prefix}.objectives[{index}]")

        sections = chapter.get("sections")
        if not isinstance(sections, list) or not sections:
            raise StudyContentError(f"{prefix}.sections must be a non-empty list.")
        chapter_section_ids: set[str] = set()
        for section_index, section in enumerate(sections):
            if not isinstance(section, dict):
                raise StudyContentError(f"{prefix}.sections[{section_index}] must be an object.")
            section_prefix = f"{prefix}.sections[{section_index}]"
            section_id = _required_text(
                section.get("section_id"), f"{section_prefix}.section_id", max_length=128
            )
            if section_id in section_ids:
                raise StudyContentError(f"Duplicate section_id: {section_id}")
            section_ids.add(section_id)
            chapter_section_ids.add(section_id)
            _required_text(section.get("heading"), f"{section_prefix}.heading")
            _required_text(section.get("body"), f"{section_prefix}.body", max_length=24000)

            architecture = section.get("architecture")
            if not isinstance(architecture, dict):
                raise StudyContentError(f"{section_prefix}.architecture must be an object.")
            _required_text(architecture.get("context"), f"{section_prefix}.architecture.context")
            relationships = architecture.get("relationships")
            if not isinstance(relationships, list) or not relationships:
                raise StudyContentError(
                    f"{section_prefix}.architecture.relationships must be a non-empty list."
                )
            for relationship_index, relationship in enumerate(relationships):
                _required_text(
                    relationship,
                    f"{section_prefix}.architecture.relationships[{relationship_index}]",
                )

            structured_fields = {
                "tradeoffs": ("decision", "benefit", "cost"),
                "examples": ("title", "scenario", "analysis"),
                "failure_modes": ("mistake", "consequence", "correction"),
                "terminology": ("term", "definition"),
            }
            for field_name, required_fields in structured_fields.items():
                entries = _object_list(section.get(field_name), f"{section_prefix}.{field_name}")
                for entry_index, entry in enumerate(entries):
                    for required_field in required_fields:
                        _required_text(
                            entry.get(required_field),
                            f"{section_prefix}.{field_name}[{entry_index}].{required_field}",
                        )

            points = section.get("key_points")
            if not isinstance(points, list) or not points:
                raise StudyContentError(
                    f"{section_prefix}.key_points must be a non-empty list."
                )
            for point_index, point in enumerate(points):
                _required_text(point, f"{section_prefix}.key_points[{point_index}]")

            checks = _object_list(
                section.get("knowledge_checks"),
                f"{section_prefix}.knowledge_checks",
                allow_empty=False,
            )
            for check_index, check in enumerate(checks):
                check_prefix = f"{section_prefix}.knowledge_checks[{check_index}]"
                check_id = _required_text(check.get("check_id"), f"{check_prefix}.check_id")
                if check_id in knowledge_check_ids:
                    raise StudyContentError(f"Duplicate check_id: {check_id}")
                knowledge_check_ids.add(check_id)
                _required_text(check.get("question"), f"{check_prefix}.question")
                _required_text(check.get("answer"), f"{check_prefix}.answer", max_length=6000)

            citations = _object_list(
                section.get("citations"), f"{section_prefix}.citations", allow_empty=False
            )
            for citation_index, citation in enumerate(citations):
                _citation(
                    citation,
                    f"{section_prefix}.citations[{citation_index}]",
                    schema_version=schema_version,
                    source_ids=source_ids,
                    chapter_page_start=page_start,
                    chapter_page_end=page_end,
                )

            visuals = _object_list(section.get("visuals"), f"{section_prefix}.visuals")
            for visual_index, visual in enumerate(visuals):
                visual_prefix = f"{section_prefix}.visuals[{visual_index}]"
                visual_id = _required_text(visual.get("visual_id"), f"{visual_prefix}.visual_id")
                if visual_id in visual_ids:
                    raise StudyContentError(f"Duplicate visual_id: {visual_id}")
                visual_ids.add(visual_id)
                _required_text(visual.get("title"), f"{visual_prefix}.title")
                visual_type = _required_text(visual.get("type"), f"{visual_prefix}.type")
                if visual_type not in {"architecture", "comparison", "flow", "matrix"}:
                    raise StudyContentError(
                        f"{visual_prefix}.type must be architecture, comparison, flow, or matrix."
                    )
                _required_text(visual.get("description"), f"{visual_prefix}.description")
                items = _object_list(
                    visual.get("items"), f"{visual_prefix}.items", allow_empty=False
                )
                for item_index, item in enumerate(items):
                    _required_text(item.get("label"), f"{visual_prefix}.items[{item_index}].label")
                    _required_text(item.get("detail"), f"{visual_prefix}.items[{item_index}].detail")
                _citation(
                    visual.get("citation"),
                    f"{visual_prefix}.citation",
                    schema_version=schema_version,
                    source_ids=source_ids,
                    chapter_page_start=page_start,
                    chapter_page_end=page_end,
                )

            references = _object_list(section.get("references"), f"{section_prefix}.references")
            for reference_index, reference in enumerate(references):
                reference_prefix = f"{section_prefix}.references[{reference_index}]"
                _required_text(reference.get("title"), f"{reference_prefix}.title")
                url = _required_text(reference.get("url"), f"{reference_prefix}.url", max_length=2048)
                if not url.startswith(("https://", "http://")):
                    raise StudyContentError(f"{reference_prefix}.url must be an HTTP(S) URL.")
                if schema_version == 2:
                    source_page = _positive_int(
                        reference.get("source_page"), f"{reference_prefix}.source_page"
                    )
                    if (
                        page_start is None
                        or page_end is None
                        or source_page < page_start
                        or source_page > page_end
                    ):
                        raise StudyContentError(
                            f"{reference_prefix}.source_page must stay within its chapter page range."
                        )
                else:
                    _required_text(
                        reference.get("source_locator"),
                        f"{reference_prefix}.source_locator",
                        max_length=512,
                    )
                _required_text(reference.get("role"), f"{reference_prefix}.role")

        flashcards = chapter.get("flashcards")
        if not isinstance(flashcards, list) or not flashcards:
            raise StudyContentError(f"{prefix}.flashcards must be a non-empty list.")
        for card_index, flashcard in enumerate(flashcards):
            card_prefix = f"{prefix}.flashcards[{card_index}]"
            if not isinstance(flashcard, dict):
                raise StudyContentError(f"{card_prefix} must be an object.")
            flashcard_id = _required_text(
                flashcard.get("flashcard_id"), f"{card_prefix}.flashcard_id", max_length=128
            )
            if flashcard_id in flashcards_by_id:
                raise StudyContentError(f"Duplicate flashcard_id: {flashcard_id}")
            _required_text(flashcard.get("front"), f"{card_prefix}.front")
            _required_text(flashcard.get("back"), f"{card_prefix}.back", max_length=6000)
            card_section_id = _required_text(
                flashcard.get("section_id"), f"{card_prefix}.section_id", max_length=128
            )
            if card_section_id not in chapter_section_ids:
                raise StudyContentError(
                    f"{card_prefix}.section_id does not identify a section in this chapter."
                )
            _citation(
                flashcard.get("citation"),
                f"{card_prefix}.citation",
                schema_version=schema_version,
                source_ids=source_ids,
                chapter_page_start=page_start,
                chapter_page_end=page_end,
            )
            flashcard["chapter_id"] = chapter_id
            flashcards_by_id[flashcard_id] = flashcard

        chapters_by_id[chapter_id] = chapter

    expected_numbers = set(range(1, len(chapters) + 1))
    if chapter_numbers != expected_numbers:
        raise StudyContentError("Chapter numbers must be contiguous and start at 1.")

    return StudyCatalog(
        payload=parsed,
        chapters_by_id=chapters_by_id,
        flashcards_by_id=flashcards_by_id,
    )
