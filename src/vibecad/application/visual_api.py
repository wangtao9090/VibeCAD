"""Strict, transport-neutral API for visual reconstruction drafts.

The adapter validates small JSON-shaped requests, delegates lifecycle effects
to :class:`~vibecad.visual.service.VisualReconstructionService`, and projects
only bounded information that a host needs to drive the next user action.  It
does not accept paths, image bytes, resource URIs, provider selection, or model
parameters.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from vibecad.runtime.contracts import RuntimeBudget
from vibecad.visual.drafts import (
    BaseHeadBinding,
    ReconstructionDraft,
    ReconstructionDraftError,
    ReconstructionDraftErrorCode,
)
from vibecad.visual.inputs import VisualInputStoreError, VisualInputStoreErrorCode
from vibecad.visual.reconstruction import (
    ClarificationKind,
    ClarificationQuestion,
    ReconstructionProposal,
    ReconstructionStatus,
    VisualObservation,
    reconstruction_identity,
)
from vibecad.visual.review_store import (
    VisualReviewStoreError,
    VisualReviewStoreErrorCode,
)
from vibecad.visual.service import (
    VisualReconstructionService,
    VisualServiceError,
    VisualServiceErrorCode,
)
from vibecad.visual.store import (
    ReconstructionDraftStoreError,
    ReconstructionDraftStoreErrorCode,
)
from vibecad.workflow.errors import (
    MAX_SAFE_JSON_INTEGER,
    SCHEMA_VERSION,
    is_canonical_json_pointer,
)

__all__ = ("VisualApi", "VisualApiErrorCode", "VisualCreateIngressRequest")

_MAX_REQUEST_BYTES = 16 * 1024
_MAX_JSON_DEPTH = 16
_MAX_JSON_NODES = 256
_MAX_JSON_KEY_BYTES = 128
_MAX_PUBLIC_ERROR_PATH_BYTES = 256
_MAX_ANSWER_BYTES = 512
_MAX_QUESTION_PROMPT_BYTES = 512
_MAX_PROPOSAL_PART_TYPE_BYTES = 128
_MAX_PROPOSAL_SUMMARY_BYTES = 2 * 1024

_CREATE_KEY = re.compile(r"^reconstruction_create_[0-9a-f]{32}$")
_RECONSTRUCTION_ID = re.compile(r"^reconstruction_[0-9a-f]{32}$")
_IMAGE_SET_ID = re.compile(r"^image_set_[0-9a-f]{32}$")
_PROJECT_ID = re.compile(r"^project_[0-9a-f]{32}$")
_REVISION_ID = re.compile(r"^revision_[0-9a-f]{32}$")
_QUESTION_ID = re.compile(r"^clarification_question_[0-9a-f]{32}$")
_TASK_ID = re.compile(r"^task_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class VisualApiErrorCode(StrEnum):
    """Closed public failure taxonomy for all visual API actions."""

    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    UNSUPPORTED_VERSION = "unsupported_version"
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"
    BUDGET_EXCEEDED = "budget_exceeded"
    INVALID_INPUT = "invalid_input"
    INVALID_STATE = "invalid_state"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    ADOPTION_UNAVAILABLE = "adoption_unavailable"
    LEASE_UNAVAILABLE = "lease_unavailable"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    STORE_FAILURE = "store_failure"
    INTEGRITY_FAILURE = "integrity_failure"
    RECOVERY_REQUIRED = "recovery_required"
    INTERNAL_ERROR = "internal_error"


_ERROR_MESSAGES = {
    VisualApiErrorCode.MISSING_FIELD: "A required request field is missing.",
    VisualApiErrorCode.UNKNOWN_FIELD: "The request contains an unknown field.",
    VisualApiErrorCode.UNSUPPORTED_VERSION: "The request schema version is not supported.",
    VisualApiErrorCode.INVALID_TYPE: "A request value has an invalid type.",
    VisualApiErrorCode.INVALID_VALUE: "A request value is invalid.",
    VisualApiErrorCode.BUDGET_EXCEEDED: "The request exceeds a resource budget.",
    VisualApiErrorCode.INVALID_INPUT: "The visual reconstruction request is invalid.",
    VisualApiErrorCode.INVALID_STATE: "The reconstruction is not ready for this operation.",
    VisualApiErrorCode.NOT_FOUND: "The visual reconstruction or source was not found.",
    VisualApiErrorCode.CONFLICT: "The reconstruction changed concurrently.",
    VisualApiErrorCode.ADOPTION_UNAVAILABLE: "The reviewed Task path is unavailable.",
    VisualApiErrorCode.LEASE_UNAVAILABLE: "The visual reconstruction lease is unavailable.",
    VisualApiErrorCode.RESOURCE_EXHAUSTED: "The visual reconstruction capacity is exhausted.",
    VisualApiErrorCode.STORE_FAILURE: "The visual reconstruction store operation failed.",
    VisualApiErrorCode.INTEGRITY_FAILURE: "The visual reconstruction failed integrity validation.",
    VisualApiErrorCode.RECOVERY_REQUIRED: "The reconstruction requires explicit recovery.",
    VisualApiErrorCode.INTERNAL_ERROR: "The request could not be completed.",
}

_SERVICE_ERROR_MAP = {
    VisualServiceErrorCode.INVALID_INPUT: VisualApiErrorCode.INVALID_INPUT,
    VisualServiceErrorCode.INVALID_STATE: VisualApiErrorCode.INVALID_STATE,
    VisualServiceErrorCode.CONFLICT: VisualApiErrorCode.CONFLICT,
    VisualServiceErrorCode.ADOPTION_UNAVAILABLE: VisualApiErrorCode.ADOPTION_UNAVAILABLE,
    VisualServiceErrorCode.PROVIDER_RECEIPT_MISMATCH: VisualApiErrorCode.INTEGRITY_FAILURE,
}
_INPUT_ERROR_MAP = {
    VisualInputStoreErrorCode.INVALID_INPUT: VisualApiErrorCode.INVALID_INPUT,
    VisualInputStoreErrorCode.NOT_FOUND: VisualApiErrorCode.NOT_FOUND,
    VisualInputStoreErrorCode.CONFLICT: VisualApiErrorCode.CONFLICT,
    VisualInputStoreErrorCode.BUDGET_EXCEEDED: VisualApiErrorCode.RESOURCE_EXHAUSTED,
    VisualInputStoreErrorCode.INTEGRITY_FAILURE: VisualApiErrorCode.INTEGRITY_FAILURE,
    VisualInputStoreErrorCode.STORE_FAILURE: VisualApiErrorCode.STORE_FAILURE,
    VisualInputStoreErrorCode.LEASE_UNAVAILABLE: VisualApiErrorCode.LEASE_UNAVAILABLE,
    VisualInputStoreErrorCode.RECOVERY_REQUIRED: VisualApiErrorCode.RECOVERY_REQUIRED,
}
_DRAFT_STORE_ERROR_MAP = {
    ReconstructionDraftStoreErrorCode.INVALID_ID: VisualApiErrorCode.INVALID_INPUT,
    ReconstructionDraftStoreErrorCode.NOT_FOUND: VisualApiErrorCode.NOT_FOUND,
    ReconstructionDraftStoreErrorCode.ALREADY_EXISTS: VisualApiErrorCode.CONFLICT,
    ReconstructionDraftStoreErrorCode.CONFLICT: VisualApiErrorCode.CONFLICT,
    ReconstructionDraftStoreErrorCode.CORRUPT_RECORD: VisualApiErrorCode.INTEGRITY_FAILURE,
    ReconstructionDraftStoreErrorCode.RECORD_TOO_LARGE: VisualApiErrorCode.RESOURCE_EXHAUSTED,
    ReconstructionDraftStoreErrorCode.UNSAFE_STORE: VisualApiErrorCode.INTEGRITY_FAILURE,
    ReconstructionDraftStoreErrorCode.LOCK_UNAVAILABLE: VisualApiErrorCode.LEASE_UNAVAILABLE,
    ReconstructionDraftStoreErrorCode.IO_ERROR: VisualApiErrorCode.STORE_FAILURE,
    ReconstructionDraftStoreErrorCode.DURABILITY_UNCERTAIN: VisualApiErrorCode.RECOVERY_REQUIRED,
    ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED: VisualApiErrorCode.RESOURCE_EXHAUSTED,
}
_DRAFT_ERROR_MAP = {
    ReconstructionDraftErrorCode.INVALID_INPUT: VisualApiErrorCode.INTEGRITY_FAILURE,
    ReconstructionDraftErrorCode.UNSUPPORTED_VERSION: VisualApiErrorCode.INTEGRITY_FAILURE,
    ReconstructionDraftErrorCode.INTEGRITY_FAILURE: VisualApiErrorCode.INTEGRITY_FAILURE,
    ReconstructionDraftErrorCode.BUDGET_EXCEEDED: VisualApiErrorCode.RESOURCE_EXHAUSTED,
    ReconstructionDraftErrorCode.INVALID_TRANSITION: VisualApiErrorCode.INVALID_STATE,
}
_REVIEW_STORE_ERROR_MAP = {
    VisualReviewStoreErrorCode.INVALID_INPUT: VisualApiErrorCode.INTERNAL_ERROR,
    VisualReviewStoreErrorCode.NOT_FOUND: VisualApiErrorCode.NOT_FOUND,
    VisualReviewStoreErrorCode.CONFLICT: VisualApiErrorCode.INTEGRITY_FAILURE,
    VisualReviewStoreErrorCode.DELETED: VisualApiErrorCode.INVALID_STATE,
    VisualReviewStoreErrorCode.BUDGET_EXCEEDED: VisualApiErrorCode.RESOURCE_EXHAUSTED,
    VisualReviewStoreErrorCode.INTEGRITY_FAILURE: VisualApiErrorCode.INTEGRITY_FAILURE,
    VisualReviewStoreErrorCode.STORE_FAILURE: VisualApiErrorCode.STORE_FAILURE,
    VisualReviewStoreErrorCode.LEASE_UNAVAILABLE: VisualApiErrorCode.LEASE_UNAVAILABLE,
    VisualReviewStoreErrorCode.RECOVERY_REQUIRED: VisualApiErrorCode.RECOVERY_REQUIRED,
    VisualReviewStoreErrorCode.DURABILITY_UNCERTAIN: VisualApiErrorCode.RECOVERY_REQUIRED,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualCreateIngressRequest:
    """Validated public create values before application-owned HEAD capture."""

    create_key: str
    project_id: str
    image_set_id: str
    image_set_manifest_sha256: str

    def __post_init__(self) -> None:
        for name, pattern in (
            ("create_key", _CREATE_KEY),
            ("project_id", _PROJECT_ID),
            ("image_set_id", _IMAGE_SET_ID),
            ("image_set_manifest_sha256", _DIGEST),
        ):
            value = getattr(self, name)
            if type(value) is not str or pattern.fullmatch(value) is None:
                raise ValueError(f"{name} must be a canonical identifier")


class _ApiFailure(Exception):
    __slots__ = ("code", "path")

    def __init__(self, code: VisualApiErrorCode, path: str = "") -> None:
        self.code = code
        self.path = path
        super().__init__(code.value)


def _raise(code: VisualApiErrorCode, path: str = "") -> None:
    raise _ApiFailure(code, path)


def _pointer(parent: str, token: str) -> str:
    escaped = token.replace("~", "~0").replace("/", "~1")
    candidate = f"{parent}/{escaped}"
    try:
        if len(candidate.encode("utf-8")) <= _MAX_PUBLIC_ERROR_PATH_BYTES:
            return candidate
    except UnicodeError:
        pass
    return "/_truncated"


def _utf8_length(value: str, path: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError:
        _raise(VisualApiErrorCode.INVALID_VALUE, path)


def _validate_json(value: object) -> None:
    count = 0
    active: set[int] = set()
    stack: list[tuple[object, str, int, bool]] = [(value, "", 0, False)]
    while stack:
        current, path, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(current))
            continue
        count += 1
        if count > _MAX_JSON_NODES:
            _raise(VisualApiErrorCode.BUDGET_EXCEEDED, path)
        if current is None or type(current) is bool:
            continue
        if type(current) is int:
            if abs(current) > MAX_SAFE_JSON_INTEGER:
                _raise(VisualApiErrorCode.INVALID_VALUE, path)
            continue
        if type(current) is float:
            if not math.isfinite(current):
                _raise(VisualApiErrorCode.INVALID_VALUE, path)
            continue
        if type(current) is str:
            if _utf8_length(current, path) > _MAX_REQUEST_BYTES:
                _raise(VisualApiErrorCode.BUDGET_EXCEEDED, path)
            continue
        if type(current) not in {dict, list}:
            _raise(VisualApiErrorCode.INVALID_TYPE, path)
        if depth >= _MAX_JSON_DEPTH:
            _raise(VisualApiErrorCode.BUDGET_EXCEEDED, path)
        identity = id(current)
        if identity in active:
            _raise(VisualApiErrorCode.INVALID_VALUE, path)
        active.add(identity)
        stack.append((current, path, depth, True))
        if type(current) is list:
            for index in range(len(current) - 1, -1, -1):
                stack.append((current[index], _pointer(path, str(index)), depth + 1, False))
            continue
        for key, item in reversed(tuple(current.items())):
            if type(key) is not str:
                _raise(VisualApiErrorCode.INVALID_TYPE, path)
            if _utf8_length(key, path) > _MAX_JSON_KEY_BYTES:
                _raise(VisualApiErrorCode.BUDGET_EXCEEDED, path)
            stack.append((item, _pointer(path, key), depth + 1, False))


def _canonical_size(value: object) -> int:
    total = 0
    try:
        encoder = json.JSONEncoder(
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for chunk in encoder.iterencode(value):
            total += len(chunk.encode("utf-8"))
            if total > _MAX_REQUEST_BYTES:
                break
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _raise(VisualApiErrorCode.INVALID_VALUE)
    return total


def _request(request: object, fields: frozenset[str]) -> dict[str, object]:
    if type(request) is not dict:
        _raise(VisualApiErrorCode.INVALID_TYPE)
    _validate_json(request)
    if _canonical_size(request) > _MAX_REQUEST_BYTES:
        _raise(VisualApiErrorCode.BUDGET_EXCEEDED)
    keys = set(request)
    unknown = sorted(keys - fields)
    if unknown:
        _raise(VisualApiErrorCode.UNKNOWN_FIELD, _pointer("", unknown[0]))
    missing = sorted(fields - keys)
    if missing:
        _raise(VisualApiErrorCode.MISSING_FIELD, _pointer("", missing[0]))
    _schema_version(request["schema_version"], "/schema_version")
    return request


def _exact_object(value: object, fields: frozenset[str], path: str) -> dict[str, object]:
    if type(value) is not dict:
        _raise(VisualApiErrorCode.INVALID_TYPE, path)
    keys = set(value)
    unknown = sorted(keys - fields)
    if unknown:
        _raise(VisualApiErrorCode.UNKNOWN_FIELD, _pointer(path, unknown[0]))
    missing = sorted(fields - keys)
    if missing:
        _raise(VisualApiErrorCode.MISSING_FIELD, _pointer(path, missing[0]))
    return value


def _schema_version(value: object, path: str) -> int:
    if type(value) is not int:
        _raise(VisualApiErrorCode.INVALID_TYPE, path)
    if value != SCHEMA_VERSION:
        _raise(VisualApiErrorCode.UNSUPPORTED_VERSION, path)
    return value


def _identifier(value: object, pattern: re.Pattern[str], path: str) -> str:
    if type(value) is not str:
        _raise(VisualApiErrorCode.INVALID_TYPE, path)
    if pattern.fullmatch(value) is None:
        _raise(VisualApiErrorCode.INVALID_VALUE, path)
    return value


def _safe_integer(value: object, path: str, *, positive: bool = False) -> int:
    if type(value) is not int:
        _raise(VisualApiErrorCode.INVALID_TYPE, path)
    minimum = 1 if positive else 0
    if not minimum <= value <= MAX_SAFE_JSON_INTEGER:
        _raise(VisualApiErrorCode.INVALID_VALUE, path)
    return value


def _base_head(value: object) -> BaseHeadBinding:
    path = "/base_head"
    data = _exact_object(
        value,
        frozenset(
            {
                "schema_version",
                "project_id",
                "generation",
                "revision_id",
                "manifest_sha256",
            }
        ),
        path,
    )
    _schema_version(data["schema_version"], f"{path}/schema_version")
    project_id = _identifier(data["project_id"], _PROJECT_ID, f"{path}/project_id")
    generation = _safe_integer(data["generation"], f"{path}/generation")
    revision_id = _identifier(data["revision_id"], _REVISION_ID, f"{path}/revision_id")
    manifest = _identifier(data["manifest_sha256"], _DIGEST, f"{path}/manifest_sha256")
    try:
        return BaseHeadBinding(
            project_id=project_id,
            generation=generation,
            revision_id=revision_id,
            manifest_sha256=manifest,
        )
    except (TypeError, ValueError):
        _raise(VisualApiErrorCode.INVALID_VALUE, path)


def _runtime_inputs(
    budget_value: object,
    deadline_value: object,
) -> tuple[RuntimeBudget | None, int | None]:
    if budget_value is None and deadline_value is None:
        return None, None
    if budget_value is None or deadline_value is None:
        _raise(VisualApiErrorCode.INVALID_VALUE, "/budget")
    data = _exact_object(
        budget_value,
        frozenset({"max_elapsed_ms", "max_memory_bytes", "max_output_bytes"}),
        "/budget",
    )
    values = {
        name: _safe_integer(data[name], f"/budget/{name}", positive=True)
        for name in ("max_elapsed_ms", "max_memory_bytes", "max_output_bytes")
    }
    deadline = _safe_integer(deadline_value, "/deadline_ms", positive=True)
    try:
        return RuntimeBudget(**values), deadline
    except (TypeError, ValueError):
        _raise(VisualApiErrorCode.INVALID_VALUE, "/budget")


def _answer(value: object) -> bool | int | float | str:
    if type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > MAX_SAFE_JSON_INTEGER:
            _raise(VisualApiErrorCode.INVALID_VALUE, "/response")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _raise(VisualApiErrorCode.INVALID_VALUE, "/response")
        return value
    if type(value) is str:
        if (
            not value
            or value.strip() != value
            or not value.isprintable()
            or _utf8_length(value, "/response") > _MAX_ANSWER_BYTES
        ):
            _raise(VisualApiErrorCode.INVALID_VALUE, "/response")
        return value
    _raise(VisualApiErrorCode.INVALID_TYPE, "/response")


def _public_text(value: object, *, maximum: int) -> str:
    if type(value) is not str or not value or value.strip() != value or not value.isprintable():
        _raise(VisualApiErrorCode.INTERNAL_ERROR)
    try:
        if len(value.encode("utf-8")) > maximum:
            _raise(VisualApiErrorCode.INTERNAL_ERROR)
    except UnicodeError:
        _raise(VisualApiErrorCode.INTERNAL_ERROR)
    return value


def _question_projection(value: object) -> dict[str, object]:
    if type(value) is not ClarificationQuestion:
        _raise(VisualApiErrorCode.INTERNAL_ERROR)
    if (
        type(value.id) is not str
        or _QUESTION_ID.fullmatch(value.id) is None
        or type(value.kind) is not ClarificationKind
    ):
        _raise(VisualApiErrorCode.INTERNAL_ERROR)
    return {
        "question_id": value.id,
        "kind": value.kind.value,
        "prompt": _public_text(value.prompt, maximum=_MAX_QUESTION_PROMPT_BYTES),
    }


def _proposal_projection(value: object) -> dict[str, object]:
    if type(value) is not ReconstructionProposal:
        _raise(VisualApiErrorCode.INTERNAL_ERROR)
    return {
        "part_type": _public_text(value.part_type, maximum=_MAX_PROPOSAL_PART_TYPE_BYTES),
        "summary": _public_text(value.summary, maximum=_MAX_PROPOSAL_SUMMARY_BYTES),
    }


def _success(result: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "result": result,
        "error": None,
    }


def _failure(error: _ApiFailure) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "result": None,
        "error": {
            "schema_version": SCHEMA_VERSION,
            "code": error.code.value,
            "path": error.path,
            "message": _ERROR_MESSAGES[error.code],
        },
    }


class VisualApi:
    """Seven strict request methods over the visual reconstruction service."""

    __slots__ = ("_service",)

    def __init__(self, *, service: VisualReconstructionService) -> None:
        if type(service) is not VisualReconstructionService:
            raise TypeError("service must be an exact VisualReconstructionService")
        self._service = service

    @staticmethod
    def parse_create_request(
        request: object,
    ) -> VisualCreateIngressRequest | dict[str, object]:
        """Validate public create ingress before application-owned HEAD capture.

        A successful parse returns inert identifier values only.  Any rejected
        request returns the same bounded failure envelope as the seven public
        lifecycle actions; rejected keys or values are never reflected.
        """

        try:
            data = _request(
                request,
                frozenset(
                    {
                        "schema_version",
                        "create_key",
                        "project_id",
                        "image_set_id",
                        "image_set_manifest_sha256",
                    }
                ),
            )
            return VisualCreateIngressRequest(
                create_key=_identifier(data["create_key"], _CREATE_KEY, "/create_key"),
                project_id=_identifier(data["project_id"], _PROJECT_ID, "/project_id"),
                image_set_id=_identifier(data["image_set_id"], _IMAGE_SET_ID, "/image_set_id"),
                image_set_manifest_sha256=_identifier(
                    data["image_set_manifest_sha256"],
                    _DIGEST,
                    "/image_set_manifest_sha256",
                ),
            )
        except _ApiFailure as error:
            return _failure(error)
        except BaseException:
            return _failure(_ApiFailure(VisualApiErrorCode.INTERNAL_ERROR))

    @staticmethod
    def failure(
        code: VisualApiErrorCode,
        path: str = "",
    ) -> dict[str, object]:
        """Build the same bounded envelope for failures detected by composition."""

        if type(code) is not VisualApiErrorCode:
            raise TypeError("code must be an exact VisualApiErrorCode")
        try:
            path_size = len(path.encode("utf-8")) if type(path) is str else 0
        except UnicodeError as error:
            raise ValueError("path must be a bounded canonical JSON Pointer") from error
        if (
            type(path) is not str
            or not is_canonical_json_pointer(path)
            or path_size > _MAX_PUBLIC_ERROR_PATH_BYTES
        ):
            raise ValueError("path must be a bounded canonical JSON Pointer")
        return _failure(_ApiFailure(code, path))

    @staticmethod
    def _guard(action: Callable[[], dict[str, object]]) -> dict[str, object]:
        try:
            return _success(action())
        except _ApiFailure as error:
            return _failure(error)
        except VisualServiceError as error:
            return _failure(_ApiFailure(_SERVICE_ERROR_MAP[error.code]))
        except VisualInputStoreError as error:
            return _failure(_ApiFailure(_INPUT_ERROR_MAP[error.code]))
        except VisualReviewStoreError as error:
            return _failure(_ApiFailure(_REVIEW_STORE_ERROR_MAP[error.code]))
        except ReconstructionDraftStoreError as error:
            return _failure(_ApiFailure(_DRAFT_STORE_ERROR_MAP[error.code]))
        except ReconstructionDraftError as error:
            return _failure(_ApiFailure(_DRAFT_ERROR_MAP[error.code]))
        except BaseException:
            return _failure(_ApiFailure(VisualApiErrorCode.INTERNAL_ERROR))

    def _projection(
        self,
        draft: object,
        *,
        expected_reconstruction_id: str,
    ) -> dict[str, object]:
        if (
            type(draft) is not ReconstructionDraft
            or draft.reconstruction_id != expected_reconstruction_id
            or _RECONSTRUCTION_ID.fullmatch(draft.reconstruction_id) is None
            or type(draft.generation) is not int
            or not 0 <= draft.generation <= MAX_SAFE_JSON_INTEGER
            or type(draft.status) is not ReconstructionStatus
        ):
            _raise(VisualApiErrorCode.INTERNAL_ERROR)
        observation, proposal = self._service.load_presentation(draft)
        questions: list[dict[str, object]] = []
        if draft.status is ReconstructionStatus.NEEDS_INPUT:
            if type(observation) is not VisualObservation or not observation.questions:
                _raise(VisualApiErrorCode.INTERNAL_ERROR)
            questions = [_question_projection(item) for item in observation.questions]
        proposal_summary = None
        if proposal is not None:
            proposal_summary = _proposal_projection(proposal)
        if (
            draft.status
            in {
                ReconstructionStatus.PROPOSED,
                ReconstructionStatus.ADOPTING,
                ReconstructionStatus.ADOPTED,
            }
            and proposal_summary is None
        ):
            _raise(VisualApiErrorCode.INTERNAL_ERROR)
        result: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "reconstruction_id": draft.reconstruction_id,
            "status": draft.status.value,
            "generation": draft.generation,
            "next_action": draft.next_action.value,
            "questions": questions,
            "proposal_summary": proposal_summary,
        }
        if draft.adopted_task_id is not None:
            if (
                type(draft.adopted_task_id) is not str
                or _TASK_ID.fullmatch(draft.adopted_task_id) is None
            ):
                _raise(VisualApiErrorCode.INTERNAL_ERROR)
            result["adopted_task_id"] = draft.adopted_task_id
        return result

    def create_reconstruction(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _request(
                request,
                frozenset(
                    {
                        "schema_version",
                        "create_key",
                        "image_set_id",
                        "image_set_manifest_sha256",
                        "base_head",
                    }
                ),
            )
            create_key = _identifier(data["create_key"], _CREATE_KEY, "/create_key")
            image_set_id = _identifier(data["image_set_id"], _IMAGE_SET_ID, "/image_set_id")
            manifest = _identifier(
                data["image_set_manifest_sha256"],
                _DIGEST,
                "/image_set_manifest_sha256",
            )
            base_head = _base_head(data["base_head"])
            expected_id, _digest_value = reconstruction_identity(create_key)
            draft = self._service.create(
                create_key=create_key,
                image_set_id=image_set_id,
                image_set_manifest_sha256=manifest,
                base_head=base_head,
            )
            return self._projection(draft, expected_reconstruction_id=expected_id)

        return self._guard(action)

    def get_reconstruction(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _request(request, frozenset({"schema_version", "reconstruction_id"}))
            reconstruction_id = _identifier(
                data["reconstruction_id"],
                _RECONSTRUCTION_ID,
                "/reconstruction_id",
            )
            return self._projection(
                self._service.get(reconstruction_id),
                expected_reconstruction_id=reconstruction_id,
            )

        return self._guard(action)

    def run_reconstruction(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _request(
                request,
                frozenset(
                    {
                        "schema_version",
                        "reconstruction_id",
                        "expected_generation",
                        "budget",
                        "deadline_ms",
                    }
                ),
            )
            reconstruction_id = _identifier(
                data["reconstruction_id"],
                _RECONSTRUCTION_ID,
                "/reconstruction_id",
            )
            generation = _safe_integer(data["expected_generation"], "/expected_generation")
            budget, deadline = _runtime_inputs(data["budget"], data["deadline_ms"])
            draft = self._service.run(
                reconstruction_id,
                expected_generation=generation,
                budget=budget,
                deadline_ms=deadline,
            )
            return self._projection(draft, expected_reconstruction_id=reconstruction_id)

        return self._guard(action)

    def answer_reconstruction(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _request(
                request,
                frozenset(
                    {
                        "schema_version",
                        "reconstruction_id",
                        "expected_generation",
                        "question_id",
                        "response",
                    }
                ),
            )
            reconstruction_id = _identifier(
                data["reconstruction_id"],
                _RECONSTRUCTION_ID,
                "/reconstruction_id",
            )
            generation = _safe_integer(data["expected_generation"], "/expected_generation")
            question_id = _identifier(data["question_id"], _QUESTION_ID, "/question_id")
            draft = self._service.answer(
                reconstruction_id,
                expected_generation=generation,
                question_id=question_id,
                response=_answer(data["response"]),
            )
            return self._projection(draft, expected_reconstruction_id=reconstruction_id)

        return self._guard(action)

    def adopt_reconstruction(self, request: object) -> dict[str, object]:
        return self._simple_mutation(request, self._service.adopt)

    def reject_reconstruction(self, request: object) -> dict[str, object]:
        return self._simple_mutation(request, self._service.reject)

    def delete_reconstruction(self, request: object) -> dict[str, object]:
        return self._simple_mutation(request, self._service.delete)

    def _simple_mutation(
        self,
        request: object,
        operation: Callable[..., ReconstructionDraft],
    ) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _request(
                request,
                frozenset({"schema_version", "reconstruction_id", "expected_generation"}),
            )
            reconstruction_id = _identifier(
                data["reconstruction_id"],
                _RECONSTRUCTION_ID,
                "/reconstruction_id",
            )
            generation = _safe_integer(data["expected_generation"], "/expected_generation")
            draft = operation(reconstruction_id, expected_generation=generation)
            return self._projection(draft, expected_reconstruction_id=reconstruction_id)

        return self._guard(action)
