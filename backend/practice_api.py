from __future__ import annotations

import json
import logging
import math
import os
import random
import secrets
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import sha256
from http import HTTPStatus
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from urllib.parse import parse_qs

from auth import (
    CSRF_COOKIE_NAME,
    LOGIN_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuthenticationDenied,
    AuthenticationError,
    AuthenticationFailureCategory,
    AuthenticationService,
    clear_auth_cookies,
    clear_login_cookie,
    csrf_cookie,
    parse_cookie_header,
    login_cookie,
    session_cookie,
)
from learning_db import (
    LearnerSession,
    LearningAuthorizationError,
    LearningDataError,
    PendingIdentityNotFoundError,
)
from practice_db import PracticeDataError, QuestionRepository
from study_content import StudyCatalog, StudyContentError, load_study_catalog
from study_rag import StudyRagError, StudyRagService


EXAM_QUESTION_COUNT = 75
EXAM_DURATION_SECONDS = 2 * 60 * 60
PASSING_SCORE_PERCENT = 65
CISSP_BUNDLE_QUESTION_COUNT = 100
CISSP_BUNDLE_DURATION_SECONDS = 180 * 60
PRACTICE_HISTORY_DURATION_SECONDS = 365 * 24 * 60 * 60
MAX_REQUEST_BYTES = 1024 * 1024
GENERIC_INTERNAL_ERROR_MESSAGE = "Internal server error."
LOGGER = logging.getLogger("gdsa_practice.api")
CISSP_COURSE_ID = "CISSP"
GMON_COURSE_ID = "GIAC GMON"
GDSA_EXAM_BUNDLE_PREFIX = "gdsa-exam-bundle"
GDSA_PRACTICE_BUNDLE_PREFIX = "gdsa-practice-bundle"
CISSP_EXAM_BUNDLE_PREFIX = "cissp-exam-bundle"
CISSP_PRACTICE_BUNDLE_PREFIX = "cissp-practice-bundle"
GMON_EXAM_BUNDLE_PREFIX = "gmon-exam-bundle"
GMON_PRACTICE_BUNDLE_PREFIX = "gmon-practice-bundle"


@dataclass(frozen=True)
class CourseConfig:
    course_id: str
    title: str
    exam_label: str
    target_question_count: int
    duration_seconds: int
    passing_score_percent: int
    bundle_id: str | None
    study_available: bool
    practice_available: bool = True
    exam_available: bool = True
    practice_label: str = "Practice"
    practice_target_question_count: int | None = None
    practice_bundle_prefix: str | None = None
    exam_bundle_prefix: str | None = None

    def exam_payload(self, available_question_count: int) -> dict[str, Any]:
        return {
            "label": self.exam_label,
            "bundle_id": self.bundle_id,
            "bundles": self.exam_bundles(available_question_count),
            "target_question_count": self.target_question_count,
            "duration_seconds": self.duration_seconds,
            "passing_score_percent": self.passing_score_percent,
            "available_question_count": available_question_count,
            "full_exam_available": available_question_count >= self.target_question_count,
        }

    def practice_payload(self, available_question_count: int) -> dict[str, Any]:
        target_question_count = self.practice_target_question_count or available_question_count
        return {
            "label": self.practice_label,
            "bundle_id": None,
            "bundles": self.practice_bundles(available_question_count),
            "target_question_count": target_question_count,
            "timed": False,
            "available_question_count": available_question_count,
            "full_practice_available": available_question_count >= target_question_count,
        }

    def exam_bundles(self, available_question_count: int) -> list[dict[str, Any]]:
        if self.exam_bundle_prefix is None:
            return []
        return _bundle_options(
            prefix=self.exam_bundle_prefix,
            label_prefix="Bundle",
            available_question_count=available_question_count,
            question_count=self.target_question_count,
        )

    def practice_bundles(self, available_question_count: int) -> list[dict[str, Any]]:
        if self.practice_bundle_prefix is None:
            return []
        target_question_count = self.practice_target_question_count or available_question_count
        return _bundle_options(
            prefix=self.practice_bundle_prefix,
            label_prefix="Bundle",
            available_question_count=available_question_count,
            question_count=target_question_count,
        )


class StartResponse(Protocol):
    def __call__(
        self,
        status: str,
        headers: list[tuple[str, str]],
        exc_info: tuple[type[BaseException], BaseException, Any] | None = None,
    ) -> Callable[[bytes], Any] | None: ...


class PracticeRequestError(ValueError):
    """Raised when an API request fails boundary validation."""


class AuthenticationRequired(PermissionError):
    """Raised when a protected endpoint has no valid learner session."""


class CsrfValidationError(PermissionError):
    """Raised when a state-changing request fails CSRF validation."""


def _answer_labels(value: Any, *, allow_unanswered: bool = False) -> list[str]:
    if value is None and allow_unanswered:
        return []
    if isinstance(value, str):
        raw_labels = value.replace(";", ",").split(",")
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        raw_labels = value
    else:
        raise PracticeRequestError("Answer must be a choice label or a list of choice labels.")

    labels: list[str] = []
    for raw_label in raw_labels:
        label = raw_label.strip().upper()
        if label and label not in labels:
            labels.append(label)
    if not labels and not allow_unanswered:
        raise PracticeRequestError("Answer must include at least one choice label, such as B or B,D.")
    return labels


def _choice_labels(record: dict[str, Any]) -> set[str]:
    return {
        str(choice["label"]).strip().upper()
        for choice in record["choices"]
        if isinstance(choice, dict) and isinstance(choice.get("label"), str)
    }


def _score_question(
    record: dict[str, Any], selected_value: Any, *, allow_unanswered: bool = False
) -> dict[str, Any]:
    submitted = _answer_labels(selected_value, allow_unanswered=allow_unanswered)
    allowed_labels = _choice_labels(record)
    invalid_labels = [label for label in submitted if label not in allowed_labels]
    if invalid_labels:
        raise PracticeRequestError(f"Answer includes invalid choice label(s): {','.join(invalid_labels)}")

    correct = _answer_labels(record["answer"])
    is_correct = len(submitted) == len(correct) and set(submitted) == set(correct)
    return {
        "question_id": record["question_id"],
        "selected": submitted,
        "correct_answer": correct,
        "correct_answer_text": ",".join(correct),
        "is_correct": is_correct,
        "explanation": record["explanation"],
        "source_file": record["source_file"],
    }


def _public_question(record: dict[str, Any]) -> dict[str, Any]:
    page_start = record["page_start"]
    page_end = record["page_end"]
    page_range = f"{page_start}-{page_end}" if page_start is not None and page_end is not None else None
    return {
        "question_id": record["question_id"],
        "question_number": record["question_number"],
        "question": record["question"],
        "choices": record["choices"],
        "multi_select": len(record["answer"]) > 1,
        "answer_available": True,
        "source_file": record["source_file"],
        "page_range": page_range,
    }


def _bundle_options(
    *,
    prefix: str,
    label_prefix: str,
    available_question_count: int,
    question_count: int,
) -> list[dict[str, Any]]:
    if question_count <= 0:
        raise PracticeDataError("Bundle question count must be positive.")
    if available_question_count <= 0:
        return []
    bundle_count = math.ceil(available_question_count / question_count)
    bundles: list[dict[str, Any]] = []
    for bundle_number in range(1, bundle_count + 1):
        start_index = (bundle_number - 1) * question_count
        remaining = max(0, available_question_count - start_index)
        effective_count = min(question_count, remaining)
        bundles.append(
            {
                "bundle_id": f"{prefix}-{bundle_number:03d}",
                "label": f"{label_prefix} {bundle_number}",
                "bundle_number": bundle_number,
                "question_count": effective_count,
                "full_size": effective_count == question_count,
            }
        )
    return bundles


def _bundle_records(
    records: list[dict[str, Any]],
    bundles: list[dict[str, Any]],
    bundle_id: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized_bundle_id = bundle_id.strip() if isinstance(bundle_id, str) else ""
    if not normalized_bundle_id:
        normalized_bundle_id = str(bundles[0]["bundle_id"])
    bundle = next(
        (item for item in bundles if item["bundle_id"] == normalized_bundle_id),
        None,
    )
    if bundle is None:
        raise PracticeRequestError(f"Bundle is not configured: {normalized_bundle_id}")
    first_bundle_question_count = int(bundles[0]["question_count"])
    start_index = (int(bundle["bundle_number"]) - 1) * first_bundle_question_count
    selected_records = records[start_index : start_index + int(bundle["question_count"])]
    if not selected_records:
        raise PracticeDataError(f"No questions were found for bundle {normalized_bundle_id!r}.")
    return bundle, selected_records


class PracticeApi:
    def __init__(
        self,
        repository: QuestionRepository,
        authentication: AuthenticationService | None = None,
        study_catalog: StudyCatalog | None = None,
        study_rag: StudyRagService | None = None,
        *,
        study_catalogs: dict[str, StudyCatalog] | None = None,
        study_rags: dict[str, StudyRagService] | None = None,
    ) -> None:
        if authentication is None:
            raise AuthenticationError(
                "Authentication is required to construct the practice application.",
                category=AuthenticationFailureCategory.CONFIGURATION_INVALID,
            )
        self.repository = repository
        self.authentication = authentication
        self.study_catalogs = dict(study_catalogs or {})
        if study_catalog is not None:
            self.study_catalogs.setdefault(study_catalog.course_id, study_catalog)
        for course_id, catalog in self.study_catalogs.items():
            if course_id != catalog.course_id:
                raise PracticeDataError(
                    f"Study catalog key {course_id!r} does not match {catalog.course_id!r}."
                )
        self.study_rags = dict(study_rags or {})
        if study_rag is not None and study_catalog is not None:
            self.study_rags.setdefault(study_catalog.course_id, study_rag)
        self.study_catalog = self.study_catalogs.get(repository.project_id)
        self.study_rag = self.study_rags.get(repository.project_id)
        self.learning_repository = authentication.repository if authentication is not None else None

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/"))
        request_id = f"req-{uuid.uuid4().hex}"
        try:
            if path.startswith("/auth/"):
                return self._dispatch_auth(method, path, environ, start_response)
            status, payload, extra_headers = self._dispatch(method, path, environ)
        except AuthenticationRequired:
            status, payload, extra_headers = HTTPStatus.UNAUTHORIZED, {"error": "Authentication required."}, []
        except CsrfValidationError:
            status, payload, extra_headers = HTTPStatus.FORBIDDEN, {"error": "Request validation failed."}, []
        except LearningAuthorizationError as exc:
            status, payload, extra_headers = HTTPStatus.FORBIDDEN, {"error": str(exc)}, []
        except PendingIdentityNotFoundError:
            status, payload, extra_headers = HTTPStatus.CONFLICT, {"error": "Pending identity is no longer available."}, []
        except AuthenticationDenied as exc:
            status, payload, extra_headers = HTTPStatus.FORBIDDEN, {"error": str(exc)}, []
        except AuthenticationError as exc:
            LOGGER.warning(
                "Authentication request failed request_id=%s method=%s path=%s failure_category=%s",
                request_id,
                method,
                path,
                exc.category.value,
            )
            status, payload, extra_headers = HTTPStatus.BAD_REQUEST, {"error": "Login could not be completed."}, []
        except (PracticeRequestError, StudyRagError) as exc:
            status, payload, extra_headers = HTTPStatus.BAD_REQUEST, {"error": str(exc)}, []
        except (PracticeDataError, LearningDataError, StudyContentError):
            LOGGER.exception(
                "Question database request failed request_id=%s method=%s path=%s", request_id, method, path
            )
            status = HTTPStatus.SERVICE_UNAVAILABLE
            payload = {"error": "Service unavailable.", "request_id": request_id}
            extra_headers = []
        except Exception as exc:
            LOGGER.error(
                "Unexpected API error request_id=%s method=%s path=%s exception_type=%s",
                request_id,
                method,
                path,
                type(exc).__name__,
            )
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            payload = {"error": GENERIC_INTERNAL_ERROR_MESSAGE, "request_id": request_id}
            extra_headers = []
        return self._json_response(start_response, status, payload, extra_headers)

    def _dispatch(
        self, method: str, path: str, environ: dict[str, Any]
    ) -> tuple[HTTPStatus, dict[str, Any], list[tuple[str, str]]]:
        routes = {
            "/health": {"GET"},
            "/api/session": {"GET"},
            "/api/logout": {"POST"},
            "/api/courses": {"GET"},
            "/api/dashboard": {"GET"},
            "/api/questions": {"GET"},
            "/api/answer": {"POST"},
            "/api/practice/start": {"POST"},
            "/api/exam/start": {"POST"},
            "/api/score": {"POST"},
            "/api/study": {"GET"},
            "/api/study/explain": {"POST"},
            "/api/attempts": {"GET"},
            "/api/attempt-detail": {"GET"},
            "/api/learner-items": {"GET", "POST"},
            "/api/search": {"GET"},
            "/api/admin/pending-identities": {"GET"},
            "/api/admin/approved-identities": {"GET"},
            "/api/admin/pending-identities/approve": {"POST"},
            "/api/admin/pending-identities/dismiss": {"POST"},
            "/api/admin/approved-identities/update": {"POST"},
            "/api/admin/course-access/update": {"POST"},
            "/api/admin/audit-events": {"GET"},
        }
        allowed_methods = routes.get(path) or self._study_resource_methods(path)
        if allowed_methods is None:
            return HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."}, []
        if method not in allowed_methods:
            allow_header = ", ".join(sorted(allowed_methods))
            return HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Method not allowed."}, [("Allow", allow_header)]

        if path == "/health":
            health = {"ok": True, **self.repository.health()}
            if self.authentication is not None:
                health.update(self.authentication.repository.health())
            return HTTPStatus.OK, health, []
        learner = self._authenticated_session(environ, require_csrf=method == "POST")
        if path.startswith("/api/admin/") and learner.role != "owner":
            raise LearningAuthorizationError("Owner authorization is required.")
        if path == "/api/session":
            return (
                HTTPStatus.OK,
                {
                    "authenticated": True,
                    "user": {
                        "user_id": learner.user_id,
                        "email": learner.email,
                        "display_name": learner.display_name,
                        "role": learner.role,
                    },
                },
                [],
            )
        if path == "/api/logout":
            if self.authentication is not None:
                cookies = parse_cookie_header(str(environ.get("HTTP_COOKIE", "")))
                self.authentication.logout(cookies.get(SESSION_COOKIE_NAME, ""))
            return (
                HTTPStatus.OK,
                {"ok": True},
                [("Set-Cookie", value) for value in clear_auth_cookies()],
            )
        if path == "/api/courses":
            return HTTPStatus.OK, {"courses": self._course_options(learner)}, []
        if path == "/api/dashboard":
            course_id = self._course_id_from_query(environ)
            self._require_course_access(learner, course_id)
            return HTTPStatus.OK, self._dashboard_payload(learner, course_id), []
        if path == "/api/admin/pending-identities":
            limit = self._bounded_query_integer(environ, "limit", default=50, minimum=1, maximum=100)
            offset = self._bounded_query_integer(
                environ, "offset", default=0, minimum=0, maximum=100_000
            )
            identities = self.learning_repository.list_pending_identities(
                limit=limit, offset=offset
            )
            total = self.learning_repository.count_pending_identities()
            next_offset = offset + len(identities)
            return (
                HTTPStatus.OK,
                {
                    "identities": identities,
                    "limit": limit,
                    "offset": offset,
                    "total": total,
                    "next_offset": next_offset if next_offset < total else None,
                },
                [],
            )
        if path == "/api/admin/approved-identities":
            limit = self._bounded_query_integer(environ, "limit", default=50, minimum=1, maximum=100)
            offset = self._bounded_query_integer(
                environ, "offset", default=0, minimum=0, maximum=100_000
            )
            identities = self.learning_repository.list_approved_identities(
                limit=limit, offset=offset
            )
            configured_courses = tuple(self._course_configs())
            for identity in identities:
                explicit_access = self.learning_repository.identity_course_access(
                    str(identity["issuer"]), str(identity["subject"])
                )
                identity["course_access"] = {
                    course_id: explicit_access.get(course_id, True)
                    for course_id in configured_courses
                }
            total = self.learning_repository.count_approved_identities()
            next_offset = offset + len(identities)
            return (
                HTTPStatus.OK,
                {
                    "identities": identities,
                    "limit": limit,
                    "offset": offset,
                    "total": total,
                    "next_offset": next_offset if next_offset < total else None,
                },
                [],
            )
        if path == "/api/admin/audit-events":
            limit = self._bounded_query_integer(environ, "limit", default=50, minimum=1, maximum=100)
            offset = self._bounded_query_integer(
                environ, "offset", default=0, minimum=0, maximum=100_000
            )
            events = self.learning_repository.list_admin_activity(limit=limit, offset=offset)
            total = self.learning_repository.count_admin_activity()
            next_offset = offset + len(events)
            return (
                HTTPStatus.OK,
                {
                    "events": events,
                    "limit": limit,
                    "offset": offset,
                    "total": total,
                    "next_offset": next_offset if next_offset < total else None,
                },
                [],
            )
        if path == "/api/attempts":
            course_id = self._course_id_from_query(environ)
            self._require_course_access(learner, course_id)
            limit = self._bounded_query_integer(environ, "limit", default=20, minimum=1, maximum=100)
            offset = self._bounded_query_integer(
                environ, "offset", default=0, minimum=0, maximum=100_000
            )
            attempts = self.learning_repository.list_attempts(
                learner.user_id, course_id=course_id, limit=limit, offset=offset
            )
            total = self.learning_repository.count_attempts(
                learner.user_id, course_id=course_id
            )
            return HTTPStatus.OK, {"attempts": attempts, "total": total}, []
        if path == "/api/attempt-detail":
            attempt_id = self._required_query_value(environ, "attempt_id", max_length=64)
            return HTTPStatus.OK, self._attempt_detail_payload(learner, attempt_id), []
        if path == "/api/learner-items" and method == "GET":
            course_id = self._course_id_from_query(environ)
            self._require_course_access(learner, course_id)
            return HTTPStatus.OK, self._learner_items_payload(learner, course_id), []
        if path == "/api/search":
            course_id = self._course_id_from_query(environ)
            self._require_course_access(learner, course_id)
            query = self._required_query_value(environ, "q", max_length=100)
            return HTTPStatus.OK, self._search_payload(course_id, query), []
        if path == "/api/questions":
            course_id = self._course_id_from_query(environ)
            self._require_course_access(learner, course_id)
            return HTTPStatus.OK, self._question_payload(course_id), []
        if path == "/api/study":
            study_course_id = self._course_id_from_query(environ)
            self._require_course_access(learner, study_course_id)
            return HTTPStatus.OK, self._study_catalog_payload(learner, study_course_id), []
        chapter_id = self._chapter_resource_id(path)
        if chapter_id is not None and method == "GET":
            study_course_id = (
                self._course_id_from_query(environ)
                if str(environ.get("QUERY_STRING", "")).strip()
                else self.repository.project_id
            )
            self._require_course_access(learner, study_course_id)
            return HTTPStatus.OK, self._study_chapter_payload(
                learner, study_course_id, chapter_id
            ), []

        data = self._read_json_body(environ)
        if path == "/api/admin/pending-identities/approve":
            unexpected_fields = sorted(set(data) - {"issuer", "subject", "role"})
            if unexpected_fields:
                raise PracticeRequestError(
                    "Approval accepts only exact issuer, subject, and role fields."
                )
            issuer = data.get("issuer")
            subject = data.get("subject")
            if not isinstance(issuer, str) or not issuer.strip():
                raise PracticeRequestError("issuer is required.")
            if not isinstance(subject, str) or not subject.strip():
                raise PracticeRequestError("subject is required.")
            role = data.get("role", "learner")
            if role not in {"learner", "owner"}:
                raise PracticeRequestError("role must be learner or owner.")
            try:
                self.learning_repository.approve_pending_identity(
                    actor_issuer=learner.issuer,
                    actor_subject=learner.subject,
                    issuer=issuer,
                    subject=subject,
                    role=role,
                )
            except PendingIdentityNotFoundError:
                raise
            except LearningDataError as exc:
                raise PracticeRequestError(str(exc)) from exc
            return (
                HTTPStatus.OK,
                {
                    "approved": True,
                    "identity": {
                        "issuer": issuer.strip(),
                        "subject": subject.strip(),
                        "role": role,
                    },
                },
                [],
            )
        if path == "/api/admin/pending-identities/dismiss":
            unexpected_fields = sorted(set(data) - {"issuer", "subject"})
            if unexpected_fields:
                raise PracticeRequestError("Dismissal accepts only issuer and subject fields.")
            issuer, subject = self._identity_fields(data)
            self.learning_repository.dismiss_pending_identity(
                actor_issuer=learner.issuer,
                actor_subject=learner.subject,
                issuer=issuer,
                subject=subject,
            )
            return HTTPStatus.OK, {"dismissed": True}, []
        if path == "/api/admin/approved-identities/update":
            unexpected_fields = sorted(
                set(data) - {"issuer", "subject", "role", "enabled", "label", "updated_at"}
            )
            if unexpected_fields:
                raise PracticeRequestError(
                    "Identity update contains unsupported fields: "
                    f"{','.join(unexpected_fields)}"
                )
            issuer, subject = self._identity_fields(data)
            role = data.get("role")
            enabled = data.get("enabled")
            label = data.get("label")
            updated_at = data.get("updated_at")
            if role not in {"learner", "owner"}:
                raise PracticeRequestError("role must be learner or owner.")
            if not isinstance(enabled, bool):
                raise PracticeRequestError("enabled must be a boolean.")
            if label is not None and not isinstance(label, str):
                raise PracticeRequestError("label must be a string or null.")
            if updated_at is not None and not isinstance(updated_at, str):
                raise PracticeRequestError("updated_at must be a string or null.")
            try:
                identity = self.learning_repository.update_identity_by_owner(
                    actor_issuer=learner.issuer,
                    actor_subject=learner.subject,
                    issuer=issuer,
                    subject=subject,
                    role=role,
                    enabled=enabled,
                    label=label,
                    expected_updated_at=updated_at,
                )
            except LearningDataError as exc:
                raise PracticeRequestError(str(exc)) from exc
            return HTTPStatus.OK, {"identity": identity}, []
        if path == "/api/admin/course-access/update":
            unexpected_fields = sorted(
                set(data) - {"issuer", "subject", "course_id", "enabled"}
            )
            if unexpected_fields:
                raise PracticeRequestError(
                    "Course access update contains unsupported fields: "
                    f"{','.join(unexpected_fields)}"
                )
            issuer, subject = self._identity_fields(data)
            course_id = data.get("course_id")
            enabled = data.get("enabled")
            if not isinstance(course_id, str) or course_id not in self._course_configs():
                raise PracticeRequestError("course_id is not configured.")
            if not isinstance(enabled, bool):
                raise PracticeRequestError("enabled must be a boolean.")
            try:
                access = self.learning_repository.set_identity_course_access(
                    actor_issuer=learner.issuer,
                    actor_subject=learner.subject,
                    issuer=issuer,
                    subject=subject,
                    course_id=course_id,
                    enabled=enabled,
                )
            except LearningDataError as exc:
                raise PracticeRequestError(str(exc)) from exc
            return HTTPStatus.OK, {"access": access}, []
        if path == "/api/learner-items":
            unexpected_fields = sorted(
                set(data) - {"course_id", "item_type", "item_id", "present"}
            )
            if unexpected_fields:
                raise PracticeRequestError(
                    "Learner item update contains unsupported fields: "
                    f"{','.join(unexpected_fields)}"
                )
            course_id = self._course_id_from_body(data)
            self._require_course_access(learner, course_id)
            item_type = data.get("item_type")
            item_id = data.get("item_id")
            present = data.get("present")
            if item_type not in {"lesson", "question"}:
                raise PracticeRequestError("item_type must be lesson or question.")
            if not isinstance(item_id, str) or not item_id.strip():
                raise PracticeRequestError("item_id is required.")
            if not isinstance(present, bool):
                raise PracticeRequestError("present must be a boolean.")
            self._validate_learner_item(course_id, item_type, item_id)
            self.learning_repository.set_learner_item(
                learner.user_id,
                course_id,
                item_type,
                item_id,
                present=present,
            )
            return HTTPStatus.OK, {"present": present}, []
        progress_chapter_id = self._chapter_progress_resource_id(path)
        if progress_chapter_id is not None:
            study_course_id = self._course_id_from_body(data)
            self._require_course_access(learner, study_course_id)
            return (
                HTTPStatus.OK,
                self._update_chapter_progress(
                    learner, study_course_id, progress_chapter_id, data
                ),
                [],
            )
        flashcard_id = self._flashcard_review_resource_id(path)
        if flashcard_id is not None:
            study_course_id = self._course_id_from_body(data)
            self._require_course_access(learner, study_course_id)
            return HTTPStatus.OK, self._review_flashcard(
                learner, study_course_id, flashcard_id, data
            ), []
        if path == "/api/study/explain":
            study_course_id = self._course_id_from_body(data)
            self._require_course_access(learner, study_course_id)
            return HTTPStatus.OK, self._grounded_explanation(
                study_course_id, data
            ), []
        if path == "/api/answer":
            question_id = data.get("question_id")
            if not isinstance(question_id, str) or not question_id.strip():
                raise PracticeRequestError("question_id is required.")
            course_id = self._course_id_from_body(data)
            self._require_course_access(learner, course_id)
            record = self.repository.get_question(question_id, course_id)
            if record is None:
                raise PracticeRequestError(f"Sample question not found by id: {question_id}")
            result = _score_question(record, data.get("selected"))
            attempt_id = data.get("attempt_id")
            if attempt_id is not None:
                if not isinstance(attempt_id, str) or not attempt_id.strip():
                    raise PracticeRequestError("attempt_id must be a non-empty string.")
                try:
                    progress = self.learning_repository.record_practice_answer(
                        learner.user_id,
                        attempt_id,
                        question_id,
                        list(result["selected"]),
                        is_correct=bool(result["is_correct"]),
                    )
                except LearningDataError as exc:
                    raise PracticeRequestError(str(exc)) from exc
                result["attempt_progress"] = progress
            return HTTPStatus.OK, result, []
        if path == "/api/practice/start":
            course_id = self._course_id_from_body(data)
            self._require_course_access(learner, course_id)
            return HTTPStatus.OK, self._start_practice_bundle(learner, course_id, data), []
        if path == "/api/exam/start":
            course_id = self._course_id_from_body(data)
            self._require_course_access(learner, course_id)
            return HTTPStatus.OK, self._start_exam_attempt(learner, course_id, data), []

        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise PracticeRequestError("answers must be an object keyed by question_id.")
        course_id = self._course_id_from_body(data)
        self._require_course_access(learner, course_id)
        attempt_id = data.get("attempt_id")
        if attempt_id is not None and not isinstance(attempt_id, str):
            raise PracticeRequestError("attempt_id must be a string.")
        return HTTPStatus.OK, self._score_answer_set(learner, course_id, answers, attempt_id), []

    @staticmethod
    def _study_resource_methods(path: str) -> set[str] | None:
        if PracticeApi._chapter_resource_id(path) is not None:
            return {"GET"}
        if PracticeApi._chapter_progress_resource_id(path) is not None:
            return {"POST"}
        if PracticeApi._flashcard_review_resource_id(path) is not None:
            return {"POST"}
        return None

    @staticmethod
    def _resource_id(path: str, prefix: str, suffix: str = "") -> str | None:
        if not path.startswith(prefix) or (suffix and not path.endswith(suffix)):
            return None
        end = len(path) - len(suffix) if suffix else len(path)
        value = path[len(prefix) : end]
        if not value or "/" in value:
            return None
        return value

    @staticmethod
    def _chapter_resource_id(path: str) -> str | None:
        return PracticeApi._resource_id(path, "/api/study/chapters/")

    @staticmethod
    def _chapter_progress_resource_id(path: str) -> str | None:
        return PracticeApi._resource_id(path, "/api/study/chapters/", "/progress")

    @staticmethod
    def _flashcard_review_resource_id(path: str) -> str | None:
        return PracticeApi._resource_id(path, "/api/study/flashcards/", "/review")

    def _study_dependencies(self, course_id: str) -> tuple[StudyCatalog, Any]:
        catalog = self.study_catalogs.get(course_id)
        if catalog is None or self.learning_repository is None:
            raise PracticeDataError(f"Study mode is not configured for {course_id!r}.")
        return catalog, self.learning_repository

    def _course_configs(self) -> dict[str, CourseConfig]:
        gdsa_title = (
            self.study_catalogs[self.repository.project_id].payload["title"]
            if self.repository.project_id in self.study_catalogs
            else "GIAC GDSA SEC530: Defensible Security Architecture and Engineering"
        )
        cissp_title = (
            self.study_catalogs[CISSP_COURSE_ID].payload["title"]
            if CISSP_COURSE_ID in self.study_catalogs
            else "CISSP: Certified Information Systems Security Professional"
        )
        gmon_title = (
            self.study_catalogs[GMON_COURSE_ID].payload["title"]
            if GMON_COURSE_ID in self.study_catalogs
            else "GIAC GMON: Continuous Monitoring Certification"
        )
        return {
            self.repository.project_id: CourseConfig(
                course_id=self.repository.project_id,
                title=gdsa_title,
                exam_label="GDSA Exam",
                target_question_count=EXAM_QUESTION_COUNT,
                duration_seconds=EXAM_DURATION_SECONDS,
                passing_score_percent=PASSING_SCORE_PERCENT,
                bundle_id=None,
                study_available=self.repository.project_id in self.study_catalogs,
                practice_label="GDSA Practice Bundle",
                practice_target_question_count=EXAM_QUESTION_COUNT,
                practice_bundle_prefix=GDSA_PRACTICE_BUNDLE_PREFIX,
                exam_bundle_prefix=GDSA_EXAM_BUNDLE_PREFIX,
            ),
            CISSP_COURSE_ID: CourseConfig(
                course_id=CISSP_COURSE_ID,
                title=cissp_title,
                exam_label="CISSP Exam",
                target_question_count=CISSP_BUNDLE_QUESTION_COUNT,
                duration_seconds=CISSP_BUNDLE_DURATION_SECONDS,
                passing_score_percent=70,
                bundle_id=None,
                study_available=CISSP_COURSE_ID in self.study_catalogs,
                practice_label="CISSP Practice Bundle",
                practice_target_question_count=CISSP_BUNDLE_QUESTION_COUNT,
                practice_bundle_prefix=CISSP_PRACTICE_BUNDLE_PREFIX,
                exam_bundle_prefix=CISSP_EXAM_BUNDLE_PREFIX,
            ),
            GMON_COURSE_ID: CourseConfig(
                course_id=GMON_COURSE_ID,
                title=gmon_title,
                exam_label="GMON Exam",
                target_question_count=82,
                duration_seconds=3 * 60 * 60,
                passing_score_percent=74,
                bundle_id=None,
                study_available=GMON_COURSE_ID in self.study_catalogs,
                practice_label="GMON Practice Exam",
                practice_target_question_count=82,
                practice_bundle_prefix=GMON_PRACTICE_BUNDLE_PREFIX,
                exam_bundle_prefix=GMON_EXAM_BUNDLE_PREFIX,
            ),
        }
    def _course_config(self, course_id: str) -> CourseConfig:
        config = self._course_configs().get(course_id)
        if config is None:
            raise PracticeRequestError(f"Course is not configured: {course_id}")
        return config

    def _course_options(self, learner: LearnerSession) -> list[dict[str, Any]]:
        project_counts = self.repository.available_projects()
        courses: list[dict[str, Any]] = []
        for course_id, config in self._course_configs().items():
            if learner.role != "owner" and not self.learning_repository.course_access_allowed(
                learner.issuer, learner.subject, course_id
            ):
                continue
            question_count = project_counts.get(course_id, 0)
            practice_available = config.practice_available and question_count > 0
            exam_available = config.exam_available and question_count > 0
            available = config.study_available or practice_available or exam_available
            courses.append(
                {
                    "course_id": config.course_id,
                    "title": config.title,
                    "available": available,
                    "status": None if available else "Coming soon",
                    "study_available": config.study_available,
                    "practice_available": practice_available,
                    "exam_available": exam_available,
                    "question_count": question_count,
                    "practice": config.practice_payload(question_count),
                    "exam": config.exam_payload(question_count),
                }
            )
        return courses

    def _require_course_access(self, learner: LearnerSession, course_id: str) -> None:
        self._course_config(course_id)
        if learner.role == "owner":
            return
        if not self.learning_repository.course_access_allowed(
            learner.issuer, learner.subject, course_id
        ):
            raise LearningAuthorizationError(
                f"Access to course {course_id!r} is not enabled for this account."
            )

    def _course_id_from_query(self, environ: dict[str, Any]) -> str:
        try:
            query = parse_qs(
                str(environ.get("QUERY_STRING", "")),
                keep_blank_values=True,
                strict_parsing=False,
                max_num_fields=10,
            )
        except ValueError as exc:
            raise PracticeRequestError("Query parameters are invalid.") from exc
        values = query.get("course_id")
        if values is None:
            return self.repository.project_id
        if len(values) != 1 or not values[0].strip():
            raise PracticeRequestError("course_id must be provided once.")
        return values[0].strip()

    @staticmethod
    def _required_query_value(
        environ: dict[str, Any], name: str, *, max_length: int
    ) -> str:
        try:
            query = parse_qs(
                str(environ.get("QUERY_STRING", "")),
                keep_blank_values=True,
                strict_parsing=False,
                max_num_fields=10,
            )
        except ValueError as exc:
            raise PracticeRequestError("Query parameters are invalid.") from exc
        values = query.get(name)
        if values is None or len(values) != 1 or not values[0].strip():
            raise PracticeRequestError(f"{name} must be provided once.")
        value = values[0].strip()
        if len(value) > max_length:
            raise PracticeRequestError(f"{name} must not exceed {max_length} characters.")
        return value

    @staticmethod
    def _identity_fields(data: dict[str, Any]) -> tuple[str, str]:
        issuer = data.get("issuer")
        subject = data.get("subject")
        if not isinstance(issuer, str) or not issuer.strip():
            raise PracticeRequestError("issuer is required.")
        if not isinstance(subject, str) or not subject.strip():
            raise PracticeRequestError("subject is required.")
        return issuer.strip(), subject.strip()

    def _course_id_from_body(self, data: dict[str, Any]) -> str:
        course_id = data.get("course_id", self.repository.project_id)
        if not isinstance(course_id, str) or not course_id.strip():
            raise PracticeRequestError("course_id must be a non-empty string.")
        return course_id.strip()

    def _question_payload(self, course_id: str) -> dict[str, Any]:
        config = self._course_config(course_id)
        records = self.repository.load_questions(course_id)
        questions = [_public_question(record) for record in records]
        return {
            "project_id": course_id,
            "course": {
                "course_id": config.course_id,
                "title": config.title,
                "study_available": config.study_available,
            },
            "practice": config.practice_payload(len(questions)),
            "exam": config.exam_payload(len(questions)),
            "questions": questions,
        }

    def _dashboard_payload(
        self, learner: LearnerSession, course_id: str
    ) -> dict[str, Any]:
        attempts = self.learning_repository.list_attempts(
            learner.user_id, course_id=course_id, limit=8
        )
        assessment_metrics = self.learning_repository.assessment_metrics(
            learner.user_id, course_id
        )
        items = self.learning_repository.list_learner_items(
            learner.user_id, course_id=course_id
        )
        chapter_total = 0
        chapter_completed = 0
        flashcard_total = 0
        flashcard_mastered = 0
        study_catalog = self.study_catalogs.get(course_id)
        if study_catalog is not None:
            chapter_total = len(study_catalog.payload["chapters"])
            progress = self.learning_repository.list_chapter_progress(
                learner.user_id, course_id
            )
            chapter_completed = sum(item["status"] == "completed" for item in progress)
            flashcard_total = len(study_catalog.flashcards_by_id)
            card_progress = self.learning_repository.list_flashcard_progress(
                learner.user_id, course_id
            )
            flashcard_mastered = sum(item["state"] == "mastered" for item in card_progress)
        return {
            "course_id": course_id,
            "metrics": {
                "chapter_completed": chapter_completed,
                "chapter_total": chapter_total,
                "flashcard_mastered": flashcard_mastered,
                "flashcard_total": flashcard_total,
                "completed_assessments": assessment_metrics["completed_count"],
                "average_score": assessment_metrics["average_score"],
                "best_score": assessment_metrics["best_score"],
                "bookmark_count": sum(item["item_type"] == "lesson" for item in items),
                "review_count": sum(item["item_type"] == "question" for item in items),
            },
            "recent_attempts": attempts,
            "items": self._enrich_learner_items(course_id, items),
        }

    def _attempt_detail_payload(
        self, learner: LearnerSession, attempt_id: str
    ) -> dict[str, Any]:
        try:
            detail = self.learning_repository.attempt_detail(learner.user_id, attempt_id)
        except LearningDataError as exc:
            raise PracticeRequestError(str(exc)) from exc
        attempt = dict(detail["attempt"])
        course_id = str(attempt["course_id"])
        self._require_course_access(learner, course_id)
        stored_questions = list(detail["questions"])
        question_ids = [str(item["question_id"]) for item in stored_questions]
        records = self.repository.get_questions_by_ids(course_id, question_ids)
        records_by_id = {str(record["question_id"]): record for record in records}
        review_items = {
            item["item_id"]
            for item in self.learning_repository.list_learner_items(
                learner.user_id, course_id=course_id, item_type="question"
            )
        }
        results: list[dict[str, Any]] = []
        for stored in stored_questions:
            question_id = str(stored["question_id"])
            record = records_by_id.get(question_id)
            if record is None:
                raise PracticeDataError(
                    f"Question {question_id!r} is unavailable for this assessment result."
                )
            selected = stored.get("selected", [])
            scored = _score_question(record, selected, allow_unanswered=True)
            result = {
                **_public_question(record),
                **scored,
                "position": int(stored["position"]),
                "in_review_queue": question_id in review_items,
            }
            results.append(result)
        config = self._course_config(course_id)
        assessment_kind = str(attempt["assessment_kind"])
        percent = attempt.get("score_percent")
        return {
            "attempt": attempt,
            "summary": {
                "correct_count": int(attempt.get("correct_count") or 0),
                "question_count": int(attempt["question_count"]),
                "percent": percent,
                "passing_score_percent": (
                    config.passing_score_percent if assessment_kind == "exam" else None
                ),
                "passed": (
                    float(percent) >= config.passing_score_percent
                    if assessment_kind == "exam" and percent is not None
                    else None
                ),
            },
            "results": results,
        }

    def _validate_learner_item(
        self, course_id: str, item_type: str, item_id: str
    ) -> None:
        normalized_item_id = item_id.strip()
        if item_type == "question":
            if self.repository.get_question(normalized_item_id, course_id) is None:
                raise PracticeRequestError("Question review item was not found.")
            return
        if self._study_section(course_id, normalized_item_id) is None:
            raise PracticeRequestError("Lesson bookmark was not found.")

    def _study_section(
        self, course_id: str, section_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        catalog = self.study_catalogs.get(course_id)
        if catalog is None:
            return None
        for chapter in catalog.payload["chapters"]:
            for section in chapter["sections"]:
                if section["section_id"] == section_id:
                    return chapter, section
        return None

    @staticmethod
    def _study_unit_subtitle(unit_singular: str, number: Any, title: Any) -> str:
        prefix = f"{unit_singular} {number}"
        normalized_title = str(title).strip()
        if normalized_title.casefold().startswith(prefix.casefold()):
            return normalized_title
        return f"{prefix}: {normalized_title}"

    def _enrich_learner_items(
        self, course_id: str, items: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        study_catalog = self.study_catalogs.get(course_id)
        unit_singular = (
            str(study_catalog.payload.get("unit_labels", {}).get("singular", "Chapter"))
            if study_catalog is not None
            else "Chapter"
        )
        for item in items:
            item_id = item["item_id"]
            if item["item_type"] == "lesson":
                match = self._study_section(course_id, item_id)
                if match is None:
                    continue
                chapter, section = match
                enriched.append(
                    {
                        **item,
                        "title": section["heading"],
                        "subtitle": self._study_unit_subtitle(
                            unit_singular, chapter["number"], chapter["title"]
                        ),
                        "chapter_id": chapter["chapter_id"],
                    }
                )
            else:
                record = self.repository.get_question(item_id, course_id)
                if record is None:
                    continue
                enriched.append(
                    {
                        **item,
                        "title": str(record["question"]),
                        "subtitle": f"{course_id} review question",
                    }
                )
        return enriched

    def _learner_items_payload(
        self, learner: LearnerSession, course_id: str
    ) -> dict[str, Any]:
        items = self.learning_repository.list_learner_items(
            learner.user_id, course_id=course_id
        )
        return {"course_id": course_id, "items": self._enrich_learner_items(course_id, items)}

    def _search_payload(self, course_id: str, query: str) -> dict[str, Any]:
        normalized_query = query.strip().casefold()
        if len(normalized_query) < 2:
            raise PracticeRequestError("Search query must contain at least two characters.")
        results: list[dict[str, Any]] = []

        def matches(*values: object) -> bool:
            return any(normalized_query in str(value).casefold() for value in values)

        study_catalog = self.study_catalogs.get(course_id)
        if study_catalog is not None:
            unit_singular = str(
                study_catalog.payload.get("unit_labels", {}).get("singular", "Chapter")
            )
            for chapter in study_catalog.payload["chapters"]:
                if matches(chapter["title"], chapter["summary"], *chapter["objectives"]):
                    results.append(
                        {
                            "type": "chapter",
                            "id": chapter["chapter_id"],
                            "title": chapter["title"],
                            "subtitle": f"{unit_singular} {chapter['number']}",
                        }
                    )
                for section in chapter["sections"]:
                    if matches(section["heading"], section["body"], *section["key_points"]):
                        results.append(
                            {
                                "type": "lesson",
                                "id": section["section_id"],
                                "chapter_id": chapter["chapter_id"],
                                "title": section["heading"],
                                "subtitle": self._study_unit_subtitle(
                                    unit_singular, chapter["number"], chapter["title"]
                                ),
                            }
                        )
                for flashcard in chapter["flashcards"]:
                    if matches(flashcard["front"]):
                        results.append(
                            {
                                "type": "flashcard",
                                "id": flashcard["flashcard_id"],
                                "chapter_id": chapter["chapter_id"],
                                "title": flashcard["front"],
                                "subtitle": f"{unit_singular} {chapter['number']} flashcard",
                            }
                        )
        config = self._course_config(course_id)
        question_count = self.repository.available_projects().get(course_id, 0)
        for assessment_kind, bundles in (
            ("practice", config.practice_bundles(question_count)),
            ("exam", config.exam_bundles(question_count)),
        ):
            for bundle in bundles:
                label = f"{config.title} {assessment_kind} {bundle['label']}"
                if matches(label, bundle["bundle_id"]):
                    results.append(
                        {
                            "type": "bundle",
                            "id": bundle["bundle_id"],
                            "mode": assessment_kind,
                            "title": f"{bundle['label']} · {assessment_kind.title()}",
                            "subtitle": f"{bundle['question_count']} questions",
                        }
                    )
        return {"course_id": course_id, "query": query.strip(), "results": results[:40]}

    @staticmethod
    def _rotated_question_ids(
        *,
        user_id: str,
        course_id: str,
        bundle_id: str,
        ordered_question_ids: list[str],
        attempted_question_ids: set[str],
        target_count: int,
        attempt_sequence: int,
    ) -> list[str]:
        if target_count <= 0:
            raise PracticeDataError("Exam target question count must be positive.")
        if not ordered_question_ids:
            raise PracticeDataError(f"No questions were found for project {course_id!r}.")
        selected: list[str] = []
        remaining = [
            question_id
            for question_id in ordered_question_ids
            if question_id not in attempted_question_ids
        ]
        pools = [remaining]
        if len(remaining) < min(target_count, len(ordered_question_ids)):
            pools.append(
                [
                    question_id
                    for question_id in ordered_question_ids
                    if question_id not in set(selected)
                ]
            )
        for pool_index, pool in enumerate(pools):
            if len(selected) >= target_count:
                break
            shuffled = list(pool)
            seed = sha256(
                f"{user_id}:{course_id}:{bundle_id}:{attempt_sequence}:{pool_index}".encode("utf-8")
            ).hexdigest()
            random.Random(seed).shuffle(shuffled)
            for question_id in shuffled:
                if question_id in selected:
                    continue
                selected.append(question_id)
                if len(selected) >= target_count:
                    break
        return selected[: min(target_count, len(ordered_question_ids))]

    def _start_practice_bundle(
        self,
        learner: LearnerSession,
        course_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        config = self._course_config(course_id)
        if not config.practice_available:
            raise PracticeRequestError(f"Practice mode is not configured for {course_id!r}.")
        records = self.repository.load_questions(course_id)
        practice = config.practice_payload(len(records))
        bundles = practice["bundles"]
        if not bundles:
            questions = [_public_question(record) for record in records]
            return {
                "project_id": course_id,
                "practice": practice,
                "questions": questions,
            }
        bundle, selected_records = _bundle_records(records, bundles, str(data.get("bundle_id", "")))
        attempt = self.learning_repository.create_exam_attempt(
            learner.user_id,
            course_id,
            "mock",
            f"practice:{bundle['bundle_id']}",
            [str(record["question_id"]) for record in selected_records],
            duration_seconds=PRACTICE_HISTORY_DURATION_SECONDS,
            assessment_kind="practice",
        )
        return {
            "attempt_id": attempt["attempt_id"],
            "project_id": course_id,
            "practice": {**practice, "selected_bundle": bundle},
            "questions": [_public_question(record) for record in selected_records],
        }

    def _start_exam_attempt(
        self,
        learner: LearnerSession,
        course_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        config = self._course_config(course_id)
        if not config.exam_available:
            raise PracticeRequestError(f"Exam mode is not configured for {course_id!r}.")
        records = self.repository.load_questions(course_id)
        exam = config.exam_payload(len(records))
        bundles = exam["bundles"]
        if bundles:
            bundle, selected_records = _bundle_records(records, bundles, str(data.get("bundle_id", "")))
            content_version = str(bundle["bundle_id"])
        else:
            bundle = None
            content_version = config.bundle_id or "exam"
            ordered_question_ids = [str(record["question_id"]) for record in records]
            attempted_ids = set(
                self.learning_repository.list_attempted_question_ids(
                    learner.user_id,
                    course_id,
                    content_version,
                )
            )
            attempt_sequence = self.learning_repository.count_exam_attempts(
                learner.user_id,
                course_id,
                content_version,
            ) + 1
            selected_ids = self._rotated_question_ids(
                user_id=learner.user_id,
                course_id=course_id,
                bundle_id=content_version,
                ordered_question_ids=ordered_question_ids,
                attempted_question_ids=attempted_ids,
                target_count=config.target_question_count,
                attempt_sequence=attempt_sequence,
            )
            records_by_id = {str(record["question_id"]): record for record in records}
            selected_records = [records_by_id[question_id] for question_id in selected_ids]
        attempt = self.learning_repository.create_exam_attempt(
            learner.user_id,
            course_id,
            "mock",
            content_version,
            [str(record["question_id"]) for record in selected_records],
            duration_seconds=config.duration_seconds,
        )
        return {
            "attempt": attempt,
            "attempt_id": attempt["attempt_id"],
            "project_id": course_id,
            "exam": {**exam, "selected_bundle": bundle},
            "questions": [_public_question(record) for record in selected_records],
        }

    def _study_catalog_payload(
        self, learner: LearnerSession, course_id: str
    ) -> dict[str, Any]:
        catalog, learning_repository = self._study_dependencies(course_id)
        chapter_progress = {
            item["chapter_id"]: item
            for item in learning_repository.list_chapter_progress(
                learner.user_id, catalog.course_id
            )
        }
        flashcard_progress = {
            item["flashcard_id"]: item
            for item in learning_repository.list_flashcard_progress(
                learner.user_id, catalog.course_id
            )
        }
        payload = catalog.public_catalog()
        chapters: list[dict[str, Any]] = []
        for chapter in payload["chapters"]:
            progress = chapter_progress.get(chapter["chapter_id"])
            card_ids = [
                card["flashcard_id"]
                for card in catalog.chapters_by_id[chapter["chapter_id"]]["flashcards"]
            ]
            mastered_count = sum(
                1
                for card_id in card_ids
                if flashcard_progress.get(card_id, {}).get("state") == "mastered"
            )
            chapters.append(
                {
                    **chapter,
                    "status": progress["status"] if progress is not None else "not_started",
                    "last_viewed_at": progress["last_viewed_at"] if progress is not None else None,
                    "completed_at": progress["completed_at"] if progress is not None else None,
                    "mastered_flashcard_count": mastered_count,
                }
            )
        payload["chapters"] = chapters
        return payload

    def _study_chapter_payload(
        self, learner: LearnerSession, course_id: str, chapter_id: str
    ) -> dict[str, Any]:
        catalog, learning_repository = self._study_dependencies(course_id)
        chapter = catalog.chapter(chapter_id)
        if chapter is None:
            raise PracticeRequestError(f"Study chapter not found: {chapter_id}")
        chapter_progress = {
            item["chapter_id"]: item
            for item in learning_repository.list_chapter_progress(
                learner.user_id, catalog.course_id
            )
        }.get(chapter_id)
        flashcard_progress = {
            item["flashcard_id"]: item
            for item in learning_repository.list_flashcard_progress(
                learner.user_id, catalog.course_id
            )
        }
        cards: list[dict[str, Any]] = []
        resume_index = 0
        resume_found = False
        for index, card in enumerate(chapter["flashcards"]):
            progress = flashcard_progress.get(card["flashcard_id"])
            state = progress["state"] if progress is not None else "new"
            if not resume_found and state != "mastered":
                resume_index = index
                resume_found = True
            cards.append(
                {
                    **card,
                    "state": state,
                    "review_count": progress["review_count"] if progress is not None else 0,
                    "correct_count": progress["correct_count"] if progress is not None else 0,
                    "last_reviewed_at": (
                        progress["last_reviewed_at"] if progress is not None else None
                    ),
                }
            )
        return {
            "course_id": catalog.course_id,
            "content_version": catalog.content_version,
            "chapter": {
                **chapter,
                "source_pdf": catalog.payload.get("source_pdf"),
                "sources": catalog.payload.get("sources", []),
                "unit_labels": catalog.payload.get(
                    "unit_labels", {"singular": "Chapter", "plural": "Chapters"}
                ),
                "flashcards": cards,
                "status": (
                    chapter_progress["status"]
                    if chapter_progress is not None
                    else "not_started"
                ),
                "completed_at": (
                    chapter_progress["completed_at"]
                    if chapter_progress is not None
                    else None
                ),
                "resume_flashcard_index": resume_index,
            },
        }

    def _update_chapter_progress(
        self,
        learner: LearnerSession,
        course_id: str,
        chapter_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        catalog, learning_repository = self._study_dependencies(course_id)
        if catalog.chapter(chapter_id) is None:
            raise PracticeRequestError(f"Study chapter not found: {chapter_id}")
        status = data.get("status")
        if status not in {"in_progress", "completed"}:
            raise PracticeRequestError("status must be in_progress or completed.")
        return {
            "progress": learning_repository.set_chapter_progress(
                learner.user_id,
                catalog.course_id,
                chapter_id,
                status,
            )
        }

    def _review_flashcard(
        self,
        learner: LearnerSession,
        course_id: str,
        flashcard_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        catalog, learning_repository = self._study_dependencies(course_id)
        if catalog.flashcard(flashcard_id) is None:
            raise PracticeRequestError(f"Study flashcard not found: {flashcard_id}")
        known = data.get("known")
        if not isinstance(known, bool):
            raise PracticeRequestError("known must be a boolean.")
        return {
            "progress": learning_repository.record_flashcard_review(
                learner.user_id,
                catalog.course_id,
                flashcard_id,
                known=known,
            )
        }

    def _grounded_explanation(
        self, course_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        catalog, _ = self._study_dependencies(course_id)
        chapter_id = data.get("chapter_id")
        question = data.get("question")
        if not isinstance(chapter_id, str) or not chapter_id.strip():
            raise PracticeRequestError("chapter_id is required.")
        if not isinstance(question, str) or not question.strip():
            raise PracticeRequestError("question is required.")
        chapter = catalog.chapter(chapter_id.strip())
        if chapter is None:
            raise PracticeRequestError(f"Study chapter not found: {chapter_id}")
        study_rag = self.study_rags.get(course_id)
        if study_rag is None:
            raise PracticeDataError("Grounded explanations are not configured.")
        rag_scope = chapter.get("rag_scope")
        if isinstance(rag_scope, dict) and isinstance(rag_scope.get("module_id"), str):
            results = study_rag.retrieve(
                question,
                module_id=str(rag_scope["module_id"]),
            )
        else:
            results = study_rag.retrieve(
                question,
                page_start=int(chapter["page_start"]),
                page_end=int(chapter["page_end"]),
            )
        return {
            "chapter_id": chapter_id,
            "question": question.strip(),
            "grounded": bool(results),
            "message": (
                "The strongest source evidence is shown below."
                if results
                else "No sufficiently relevant indexed evidence was found in this chapter."
            ),
            "evidence": [result.public_payload() for result in results],
        }

    def _authenticated_session(
        self,
        environ: dict[str, Any],
        *,
        require_csrf: bool,
    ) -> LearnerSession:
        cookies = parse_cookie_header(str(environ.get("HTTP_COOKIE", "")))
        session_token = cookies.get(SESSION_COOKIE_NAME, "")
        csrf_header: str | None = None
        if require_csrf:
            origin = str(environ.get("HTTP_ORIGIN", ""))
            if origin != self.authentication.config.origin:
                raise CsrfValidationError("Request origin is invalid.")
            csrf_cookie_value = cookies.get(CSRF_COOKIE_NAME, "")
            csrf_header = str(environ.get("HTTP_X_CSRF_TOKEN", ""))
            if (
                not csrf_cookie_value
                or not csrf_header
                or not secrets.compare_digest(csrf_cookie_value, csrf_header)
            ):
                raise CsrfValidationError("CSRF token is invalid.")
        learner = self.authentication.session(session_token, csrf_token=csrf_header)
        if learner is None:
            raise AuthenticationRequired("Authentication required.")
        return learner

    def _dispatch_auth(
        self,
        method: str,
        path: str,
        environ: dict[str, Any],
        start_response: StartResponse,
    ) -> Iterable[bytes]:
        if self.authentication is None:
            raise AuthenticationError(
                "Authentication is not configured.",
                category=AuthenticationFailureCategory.CONFIGURATION_INVALID,
            )
        allowed = {"/auth/login": "GET", "/auth/callback": "GET"}
        expected_method = allowed.get(path)
        if expected_method is None:
            return self._json_response(
                start_response, HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."}, []
            )
        if method != expected_method:
            return self._json_response(
                start_response,
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": "Method not allowed."},
                [("Allow", expected_method)],
            )
        try:
            query = parse_qs(
                str(environ.get("QUERY_STRING", "")),
                keep_blank_values=True,
                strict_parsing=False,
                max_num_fields=10,
            )
        except ValueError as exc:
            raise AuthenticationError(
                "Authentication query is invalid.",
                category=AuthenticationFailureCategory.REQUEST_INVALID,
            ) from exc
        if path == "/auth/login":
            location, browser_state = self.authentication.start_login("/")
            return self._redirect_response(
                start_response,
                location,
                [("Set-Cookie", login_cookie(browser_state))],
            )

        if "error" in query:
            raise AuthenticationError(
                "Google denied the authentication request.",
                category=AuthenticationFailureCategory.GOOGLE_AUTHORIZATION_DENIED,
            )
        state = self._single_query_value(query, "state")
        code = self._single_query_value(query, "code")
        cookies = parse_cookie_header(str(environ.get("HTTP_COOKIE", "")))
        session_token, csrf_token, return_path = self.authentication.complete_login(
            state=state,
            browser_state=cookies.get(LOGIN_COOKIE_NAME, ""),
            code=code,
        )
        max_age = self.authentication.config.session_lifetime_seconds
        return self._redirect_response(
            start_response,
            return_path,
            [
                ("Set-Cookie", session_cookie(session_token, max_age=max_age)),
                ("Set-Cookie", csrf_cookie(csrf_token, max_age=max_age)),
                ("Set-Cookie", clear_login_cookie()),
            ],
        )

    @staticmethod
    def _single_query_value(query: dict[str, list[str]], name: str) -> str:
        values = query.get(name)
        if values is None or len(values) != 1 or not values[0]:
            raise AuthenticationError(f"Authentication response is missing {name}.")
        return values[0]

    @staticmethod
    def _bounded_query_integer(
        environ: dict[str, Any],
        name: str,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        try:
            query = parse_qs(
                str(environ.get("QUERY_STRING", "")),
                keep_blank_values=True,
                strict_parsing=False,
                max_num_fields=10,
            )
        except ValueError as exc:
            raise PracticeRequestError("Query parameters are invalid.") from exc
        values = query.get(name)
        if values is None:
            return default
        if len(values) != 1 or not values[0]:
            raise PracticeRequestError(f"{name} must be provided once as an integer.")
        try:
            value = int(values[0])
        except ValueError as exc:
            raise PracticeRequestError(f"{name} must be an integer.") from exc
        if value < minimum or value > maximum:
            raise PracticeRequestError(f"{name} must be between {minimum} and {maximum}.")
        return value

    @staticmethod
    def _redirect_response(
        start_response: StartResponse,
        location: str,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> list[bytes]:
        headers = [
            ("Location", location),
            ("Content-Length", "0"),
            ("Cache-Control", "no-store"),
            ("Referrer-Policy", "no-referrer"),
        ]
        headers.extend(extra_headers or [])
        start_response("303 See Other", headers)
        return []

    def _score_answer_set(
        self,
        learner: LearnerSession,
        course_id: str,
        answers: dict[str, Any],
        attempt_id: str | None,
    ) -> dict[str, Any]:
        if attempt_id:
            question_ids = self.learning_repository.exam_attempt_question_ids(learner.user_id, attempt_id)
            records = self.repository.get_questions_by_ids(course_id, question_ids)
        else:
            records = self.repository.load_questions(course_id)
        records_by_id = {record["question_id"]: record for record in records}
        results: list[dict[str, Any]] = []
        correct_count = 0

        for question_id in records_by_id:
            if not isinstance(question_id, str):
                raise PracticeRequestError("answers keys must be question_id strings.")
            selected = answers.get(question_id, [])
            record = records_by_id.get(question_id)
            if record is None:
                raise PracticeRequestError(f"Sample question not found by id: {question_id}")
            result = _score_question(record, selected, allow_unanswered=True)
            results.append(result)
            if result["is_correct"]:
                correct_count += 1

        unexpected = sorted(str(question_id) for question_id in answers if question_id not in records_by_id)
        if unexpected:
            raise PracticeRequestError(f"answers include unknown question_id(s): {','.join(unexpected)}")

        graded_count = len(results)
        percent = round((correct_count / graded_count) * 100, 2) if graded_count else None
        config = self._course_config(course_id)
        required_correct = (
            math.ceil((config.passing_score_percent / 100) * graded_count)
            if graded_count
            else None
        )
        if attempt_id:
            normalized_answers = {
                question_id: _answer_labels(answers.get(question_id), allow_unanswered=True)
                for question_id in records_by_id
            }
            self.learning_repository.submit_exam_attempt(
                learner.user_id,
                attempt_id,
                normalized_answers,
                correct_count=correct_count,
                score_percent=percent,
                correctness={
                    str(result["question_id"]): bool(result["is_correct"])
                    for result in results
                },
            )
        return {
            "correct_count": correct_count,
            "graded_count": graded_count,
            "percent": percent,
            "passing_score_percent": config.passing_score_percent,
            "required_correct": required_correct,
            "passed": percent is not None and percent >= config.passing_score_percent,
            "results": results,
        }

    @staticmethod
    def _read_json_body(environ: dict[str, Any]) -> dict[str, Any]:
        content_length_value = str(environ.get("CONTENT_LENGTH", "")).strip()
        if not content_length_value:
            raise PracticeRequestError("Content-Length is required.")
        try:
            content_length = int(content_length_value)
        except ValueError as exc:
            raise PracticeRequestError("Content-Length must be an integer.") from exc
        if content_length < 0:
            raise PracticeRequestError("Content-Length cannot be negative.")
        if content_length > MAX_REQUEST_BYTES:
            raise PracticeRequestError("Request body is too large.")
        content_type = str(environ.get("CONTENT_TYPE", "")).split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise PracticeRequestError("Content-Type must be application/json.")
        request_stream = environ.get("wsgi.input")
        if request_stream is None or not hasattr(request_stream, "read"):
            raise PracticeRequestError("Request body is unavailable.")
        body = request_stream.read(content_length)
        if len(body) != content_length:
            raise PracticeRequestError("Request body ended before Content-Length bytes were received.")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PracticeRequestError("Request body must be valid JSON.") from exc
        if not isinstance(data, dict):
            raise PracticeRequestError("Request body must be a JSON object.")
        return data

    @staticmethod
    def _json_response(
        start_response: StartResponse,
        status: HTTPStatus,
        payload: dict[str, Any],
        extra_headers: list[tuple[str, str]],
    ) -> list[bytes]:
        body = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
        ]
        headers.extend(extra_headers)
        start_response(f"{status.value} {status.phrase}", headers)
        return [body]


def create_application(
    database_path: Path,
    project_id: str,
    authentication: AuthenticationService,
    study_catalog: StudyCatalog | None = None,
    study_rag: StudyRagService | None = None,
    *,
    study_catalogs: dict[str, StudyCatalog] | None = None,
    study_rags: dict[str, StudyRagService] | None = None,
) -> PracticeApi:
    if not project_id.strip():
        raise PracticeDataError("GDSA_PROJECT_ID must be a non-empty string.")
    return PracticeApi(
        QuestionRepository(database_path, project_id),
        authentication,
        study_catalog,
        study_rag,
        study_catalogs=study_catalogs,
        study_rags=study_rags,
    )


def create_application_from_env() -> PracticeApi:
    database_value = os.environ.get("GDSA_DATABASE_PATH", "").strip()
    project_id = os.environ.get("GDSA_PROJECT_ID", "").strip()
    if not database_value:
        raise PracticeDataError("GDSA_DATABASE_PATH is required.")
    if not project_id:
        raise PracticeDataError("GDSA_PROJECT_ID is required.")
    study_content_value = os.environ.get("GDSA_STUDY_CONTENT_PATH", "").strip()
    rag_index_value = os.environ.get("GDSA_RAG_INDEX_PATH", "").strip()
    if not study_content_value:
        raise StudyContentError("GDSA_STUDY_CONTENT_PATH is required.")
    if not rag_index_value:
        raise StudyRagError("GDSA_RAG_INDEX_PATH is required.")
    authentication = AuthenticationService.from_env()
    study_catalog = load_study_catalog(Path(study_content_value), project_id)
    study_rag = StudyRagService(Path(rag_index_value), project_id)
    study_catalogs = {project_id: study_catalog}
    study_rags = {project_id: study_rag}

    cissp_content_value = os.environ.get("CISSP_STUDY_CONTENT_PATH", "").strip()
    cissp_content_path = (
        Path(cissp_content_value)
        if cissp_content_value
        else Path(study_content_value).expanduser().resolve().with_name("cissp-study.json")
    )
    if cissp_content_value or cissp_content_path.is_file():
        cissp_rag_value = os.environ.get("CISSP_RAG_INDEX_PATH", "").strip()
        cissp_rag_path = (
            Path(cissp_rag_value)
            if cissp_rag_value
            else cissp_content_path.expanduser().resolve().with_name(
                "cissp-study-sources.jsonl"
            )
        )
        if not cissp_rag_path.is_file():
            raise StudyRagError(
                "CISSP study content is configured but its retrieval index is missing."
            )
        study_catalogs[CISSP_COURSE_ID] = load_study_catalog(
            cissp_content_path, CISSP_COURSE_ID
        )
        study_rags[CISSP_COURSE_ID] = StudyRagService(
            cissp_rag_path, CISSP_COURSE_ID
        )

    gmon_content_value = os.environ.get("GMON_STUDY_CONTENT_PATH", "").strip()
    gmon_content_path = (
        Path(gmon_content_value)
        if gmon_content_value
        else Path(study_content_value).expanduser().resolve().with_name("gmon-study.json")
    )
    if gmon_content_value or gmon_content_path.is_file():
        gmon_rag_value = os.environ.get("GMON_RAG_INDEX_PATH", "").strip()
        gmon_rag_path = (
            Path(gmon_rag_value)
            if gmon_rag_value
            else gmon_content_path.expanduser().resolve().with_name(
                "gmon-study-sources.jsonl"
            )
        )
        if not gmon_rag_path.is_file():
            raise StudyRagError(
                "GMON study content is configured but its retrieval index is missing."
            )
        study_catalogs[GMON_COURSE_ID] = load_study_catalog(
            gmon_content_path, GMON_COURSE_ID
        )
        study_rags[GMON_COURSE_ID] = StudyRagService(
            gmon_rag_path, GMON_COURSE_ID
        )
    return create_application(
        Path(database_value),
        project_id,
        authentication,
        study_catalog,
        study_rag,
        study_catalogs=study_catalogs,
        study_rags=study_rags,
    )


class LazyApplication:
    def __init__(self) -> None:
        self._application: PracticeApi | None = None
        self._lock = Lock()

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        if self._application is None:
            with self._lock:
                if self._application is None:
                    self._application = create_application_from_env()
        return self._application(environ, start_response)


application = LazyApplication()
