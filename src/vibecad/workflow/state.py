"""Pure, durable schema-v1 state contracts for deterministic task runs.

This module deliberately owns only immutable task state.  It does not persist
records, acquire a project lease, invoke a handler, or initialize a CAD, MCP,
model, filesystem, or network integration.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Self

from vibecad.workflow.contracts import ModelProgram, StepError, StepResult
from vibecad.workflow.errors import (
    MAX_SAFE_JSON_INTEGER,
    SCHEMA_VERSION,
    ContractValidationError,
    is_canonical_json_pointer,
    join_json_pointer,
)

MAX_STEP_RECORDS = 64
MAX_TRANSITION_RECORDS = 136
MAX_VERIFICATION_REPORTS = 16
MAX_ARTIFACT_REFS = 128
MAX_CRITERION_VERDICTS = 128
MAX_VERDICT_EVIDENCE = 32
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 1024
_MAX_TEXT_LENGTH = 256
_MAX_MAPPING_FIELDS = 64
_MAX_NESTED_PREFLIGHT_DEPTH = 72
_MAX_NESTED_PREFLIGHT_NODES = 4096
_MAX_JSON_STRING_BYTES = 4096
_MAX_JSON_KEY_BYTES = 256
_MAX_EVIDENCE_POINTER_BYTES = 256
_MAX_ERROR_PATH_LENGTH = 256
_MAX_RENDERED_ERROR_LENGTH = 512
_MAX_ORDINARY_TRANSITION_RECORDS = 128
_TRUNCATED_POINTER_TOKEN = "__truncated__"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_TASK_CREATE_KEY_RE = re.compile(r"^task_create_[0-9a-f]{32}$")
_TASK_CREATE_ID_DOMAIN = b"vibecad-task-create-v1\0"
_IDENTIFIER_RE = {
    "task": re.compile(r"^task_[0-9a-f]{32}$"),
    "project": re.compile(r"^project_[0-9a-f]{32}$"),
    "revision": re.compile(r"^revision_[0-9a-f]{32}$"),
    "draft": re.compile(r"^draft_[0-9a-f]{32}$"),
    "artifact": re.compile(r"^artifact_[0-9a-f]{32}$"),
    "verification": re.compile(r"^verification_[0-9a-f]{32}$"),
}


class TaskStateErrorCode(StrEnum):
    """Stable machine-readable state-contract rejection reasons."""

    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    UNSUPPORTED_VERSION = "unsupported_version"
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"
    INVALID_IDENTIFIER = "invalid_identifier"
    INVALID_TRANSITION = "invalid_transition"
    TERMINAL_STATE = "terminal_state"
    INVARIANT_VIOLATION = "invariant_violation"
    PROGRAM_MISMATCH = "program_mismatch"
    BUDGET_EXCEEDED = "budget_exceeded"
    MISSING_ERROR = "missing_error"
    DUPLICATE_IDENTIFIER = "duplicate_identifier"


class TaskStateError(ValueError):
    """A bounded, non-reflective state-contract failure."""

    def __init__(self, code: TaskStateErrorCode, path: str, message: str) -> None:
        if type(code) is not TaskStateErrorCode:
            raise TypeError("code must be a TaskStateErrorCode")
        if (
            type(path) is not str
            or len(path) > _MAX_ERROR_PATH_LENGTH
            or not is_canonical_json_pointer(path)
        ):
            raise ValueError("path must be a canonical RFC 6901 JSON Pointer")
        path = _bounded_error_path(path)
        if (
            type(message) is not str
            or len(message) > _MAX_TEXT_LENGTH
            or not message.strip()
            or not message.isprintable()
            or len(message.splitlines()) != 1
        ):
            raise ValueError("message must be bounded nonblank text")
        self.schema_version = SCHEMA_VERSION
        self.code = code
        self.path = path
        self.message = message
        rendered = f"task state error [{code.value}] at {json.dumps(path)}: {json.dumps(message)}"
        if len(rendered) > _MAX_RENDERED_ERROR_LENGTH:
            rendered = (
                f"task state error [{code.value}] at {json.dumps(path)}: "
                '"bounded error details omitted"'
            )
        super().__init__(rendered)


class ReasoningOwner(StrEnum):
    """The one permitted source of a task's semantic program."""

    EXTERNAL_PLAN = "external_plan"
    MCP_SAMPLING = "mcp_sampling"
    BYOK = "byok"


class ReviewPolicy(StrEnum):
    """Explicit policy controlling whether a verified candidate may auto-commit."""

    AUTO_COMMIT = "auto_commit"
    REQUIRE_REVIEW = "require_review"


class TaskStatus(StrEnum):
    """Durable task lifecycle states for the deterministic kernel."""

    CREATED = "created"
    NEEDS_PLAN = "needs_plan"
    PROGRAM_READY = "program_ready"
    VALIDATING_PROGRAM = "validating_program"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMMITTING = "committing"
    PREPARING_REVIEW = "preparing_review"
    AWAITING_USER_REVIEW = "awaiting_user_review"
    ACCEPTING_DRAFT = "accepting_draft"
    ROLLING_BACK = "rolling_back"
    NEEDS_INPUT = "needs_input"
    RECOVERY_REQUIRED = "recovery_required"
    CLEANUP_REQUIRED = "cleanup_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


class TaskEvent(StrEnum):
    """Explicit events that can advance a task state."""

    REQUEST_PLAN = "request_plan"
    SUBMIT_PROGRAM = "submit_program"
    START_VALIDATION = "start_validation"
    VALIDATE_PROGRAM = "validate_program"
    REJECT_PROGRAM = "reject_program"
    COMPLETE_EXECUTION = "complete_execution"
    FAIL_EXECUTION = "fail_execution"
    PASS_VERIFICATION = "pass_verification"
    PREPARE_REVIEW = "prepare_review"
    PUBLISH_DRAFT = "publish_draft"
    ACCEPT_DRAFT = "accept_draft"
    REJECT_DRAFT = "reject_draft"
    ABORT_ACCEPT = "abort_accept"
    CONFIRM_DRAFT_UNCOMMITTED = "confirm_draft_uncommitted"
    FAIL_VERIFICATION = "fail_verification"
    COMMIT = "commit"
    COMPLETE_ROLLBACK = "complete_rollback"
    REQUEST_INPUT = "request_input"
    REQUIRE_RECOVERY = "require_recovery"
    REQUIRE_CLEANUP = "require_cleanup"
    CONFIRM_COMMITTED = "confirm_committed"
    CONFIRM_UNCOMMITTED = "confirm_uncommitted"
    CONFIRM_PRE_CANDIDATE = "confirm_pre_candidate"
    REQUEST_CANCEL = "request_cancel"
    START_CANCELLATION = "start_cancellation"
    CONFIRM_CANCELLED = "confirm_cancelled"


class NextAction(StrEnum):
    """Deterministic caller guidance derived solely from ``TaskStatus``."""

    REQUEST_PLAN = "request_plan"
    SUBMIT_PROGRAM = "submit_program"
    VALIDATE_PROGRAM = "validate_program"
    PROVIDE_INPUT = "provide_input"
    RECONCILE = "reconcile"
    CLEANUP = "cleanup"
    REVIEW_DRAFT = "review_draft"
    WAIT = "wait"
    NONE = "none"


class CriterionOutcome(StrEnum):
    """A verifier result that can distinguish failure from unsupported work."""

    PASS = "pass"
    FAIL = "fail"
    UNSUPPORTED = "unsupported"


_TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.REJECTED,
        TaskStatus.CANCELLED,
    }
)
_CANCELLATION_STATUSES = frozenset(
    {
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.CANCELLING,
        TaskStatus.CANCELLED,
    }
)
_CANCELLATION_FORBIDDEN_RECOVERY_EVENTS = frozenset(
    {
        TaskEvent.CONFIRM_PRE_CANDIDATE,
        TaskEvent.CONFIRM_UNCOMMITTED,
        TaskEvent.CONFIRM_DRAFT_UNCOMMITTED,
    }
)
_CANCELLATION_TAIL_EVENTS = frozenset(
    {
        TaskEvent.REQUEST_CANCEL,
        TaskEvent.START_CANCELLATION,
        TaskEvent.REQUIRE_RECOVERY,
        TaskEvent.REQUIRE_CLEANUP,
        TaskEvent.CONFIRM_COMMITTED,
        TaskEvent.CONFIRM_CANCELLED,
    }
)
_CANDIDATE_ACTIVE_STATUSES = frozenset(
    {
        TaskStatus.EXECUTING,
        TaskStatus.VERIFYING,
        TaskStatus.COMMITTING,
        TaskStatus.PREPARING_REVIEW,
        TaskStatus.AWAITING_USER_REVIEW,
        TaskStatus.ACCEPTING_DRAFT,
        TaskStatus.ROLLING_BACK,
    }
)
_REVIEW_STATUSES = frozenset(
    {
        TaskStatus.PREPARING_REVIEW,
        TaskStatus.AWAITING_USER_REVIEW,
        TaskStatus.ACCEPTING_DRAFT,
        TaskStatus.REJECTED,
    }
)
_REVIEW_EVENTS = frozenset(
    {
        TaskEvent.PREPARE_REVIEW,
        TaskEvent.PUBLISH_DRAFT,
        TaskEvent.ACCEPT_DRAFT,
        TaskEvent.REJECT_DRAFT,
        TaskEvent.ABORT_ACCEPT,
        TaskEvent.CONFIRM_DRAFT_UNCOMMITTED,
    }
)
_TRANSITIONS: dict[tuple[TaskStatus, TaskEvent], TaskStatus] = {
    (TaskStatus.CREATED, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCELLED,
    (TaskStatus.CREATED, TaskEvent.REQUEST_PLAN): TaskStatus.NEEDS_PLAN,
    (TaskStatus.NEEDS_PLAN, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCELLED,
    (TaskStatus.NEEDS_PLAN, TaskEvent.SUBMIT_PROGRAM): TaskStatus.PROGRAM_READY,
    (TaskStatus.PROGRAM_READY, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCELLED,
    (TaskStatus.NEEDS_INPUT, TaskEvent.SUBMIT_PROGRAM): TaskStatus.PROGRAM_READY,
    (TaskStatus.NEEDS_INPUT, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCELLED,
    (TaskStatus.PROGRAM_READY, TaskEvent.START_VALIDATION): TaskStatus.VALIDATING_PROGRAM,
    (TaskStatus.VALIDATING_PROGRAM, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCEL_REQUESTED,
    (TaskStatus.VALIDATING_PROGRAM, TaskEvent.VALIDATE_PROGRAM): TaskStatus.EXECUTING,
    (TaskStatus.VALIDATING_PROGRAM, TaskEvent.REJECT_PROGRAM): TaskStatus.NEEDS_INPUT,
    (TaskStatus.VALIDATING_PROGRAM, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.VALIDATING_PROGRAM, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
    (TaskStatus.EXECUTING, TaskEvent.COMPLETE_EXECUTION): TaskStatus.VERIFYING,
    (TaskStatus.EXECUTING, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCEL_REQUESTED,
    (TaskStatus.EXECUTING, TaskEvent.FAIL_EXECUTION): TaskStatus.ROLLING_BACK,
    (TaskStatus.EXECUTING, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.EXECUTING, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
    (TaskStatus.VERIFYING, TaskEvent.PASS_VERIFICATION): TaskStatus.COMMITTING,
    (TaskStatus.VERIFYING, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCEL_REQUESTED,
    (TaskStatus.VERIFYING, TaskEvent.PREPARE_REVIEW): TaskStatus.PREPARING_REVIEW,
    (TaskStatus.VERIFYING, TaskEvent.FAIL_VERIFICATION): TaskStatus.ROLLING_BACK,
    (TaskStatus.VERIFYING, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.VERIFYING, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
    (TaskStatus.COMMITTING, TaskEvent.COMMIT): TaskStatus.SUCCEEDED,
    (TaskStatus.COMMITTING, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCEL_REQUESTED,
    (TaskStatus.COMMITTING, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.COMMITTING, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
    (TaskStatus.PREPARING_REVIEW, TaskEvent.PUBLISH_DRAFT): TaskStatus.AWAITING_USER_REVIEW,
    (TaskStatus.PREPARING_REVIEW, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCEL_REQUESTED,
    (TaskStatus.PREPARING_REVIEW, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.PREPARING_REVIEW, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
    (TaskStatus.AWAITING_USER_REVIEW, TaskEvent.ACCEPT_DRAFT): TaskStatus.ACCEPTING_DRAFT,
    (TaskStatus.AWAITING_USER_REVIEW, TaskEvent.REJECT_DRAFT): TaskStatus.REJECTED,
    (TaskStatus.ACCEPTING_DRAFT, TaskEvent.COMMIT): TaskStatus.SUCCEEDED,
    (TaskStatus.ACCEPTING_DRAFT, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCEL_REQUESTED,
    (TaskStatus.ACCEPTING_DRAFT, TaskEvent.CONFIRM_COMMITTED): TaskStatus.SUCCEEDED,
    (TaskStatus.ACCEPTING_DRAFT, TaskEvent.ABORT_ACCEPT): TaskStatus.AWAITING_USER_REVIEW,
    (TaskStatus.ACCEPTING_DRAFT, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.ACCEPTING_DRAFT, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
    (TaskStatus.ROLLING_BACK, TaskEvent.COMPLETE_ROLLBACK): TaskStatus.FAILED,
    (TaskStatus.ROLLING_BACK, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.ROLLING_BACK, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
    (TaskStatus.RECOVERY_REQUIRED, TaskEvent.CONFIRM_COMMITTED): TaskStatus.SUCCEEDED,
    (TaskStatus.RECOVERY_REQUIRED, TaskEvent.CONFIRM_UNCOMMITTED): TaskStatus.ROLLING_BACK,
    (
        TaskStatus.RECOVERY_REQUIRED,
        TaskEvent.CONFIRM_DRAFT_UNCOMMITTED,
    ): TaskStatus.AWAITING_USER_REVIEW,
    (TaskStatus.RECOVERY_REQUIRED, TaskEvent.CONFIRM_PRE_CANDIDATE): TaskStatus.PROGRAM_READY,
    (TaskStatus.CLEANUP_REQUIRED, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.CLEANUP_REQUIRED, TaskEvent.CONFIRM_COMMITTED): TaskStatus.SUCCEEDED,
    (TaskStatus.CLEANUP_REQUIRED, TaskEvent.CONFIRM_UNCOMMITTED): TaskStatus.ROLLING_BACK,
    (
        TaskStatus.CLEANUP_REQUIRED,
        TaskEvent.CONFIRM_DRAFT_UNCOMMITTED,
    ): TaskStatus.AWAITING_USER_REVIEW,
    (TaskStatus.CLEANUP_REQUIRED, TaskEvent.CONFIRM_PRE_CANDIDATE): TaskStatus.PROGRAM_READY,
    (TaskStatus.CANCEL_REQUESTED, TaskEvent.START_CANCELLATION): TaskStatus.CANCELLING,
    (TaskStatus.CANCELLING, TaskEvent.CONFIRM_CANCELLED): TaskStatus.CANCELLED,
    (TaskStatus.CANCELLING, TaskEvent.CONFIRM_COMMITTED): TaskStatus.SUCCEEDED,
    (TaskStatus.CANCELLING, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.CANCELLING, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
    (TaskStatus.RECOVERY_REQUIRED, TaskEvent.CONFIRM_CANCELLED): TaskStatus.CANCELLED,
    (TaskStatus.CLEANUP_REQUIRED, TaskEvent.CONFIRM_CANCELLED): TaskStatus.CANCELLED,
}


def _bounded_error_path(path: str) -> str:
    if len(path) <= _MAX_ERROR_PATH_LENGTH and len(json.dumps(path)) <= _MAX_ERROR_PATH_LENGTH:
        return path
    prefix = ""
    suffix = f"/{_TRUNCATED_POINTER_TOKEN}"
    for token in path.split("/")[1:]:
        candidate = f"{prefix}/{token}{suffix}"
        if (
            len(candidate) > _MAX_ERROR_PATH_LENGTH
            or len(json.dumps(candidate)) > _MAX_ERROR_PATH_LENGTH
        ):
            break
        prefix = f"{prefix}/{token}"
    return f"{prefix}{suffix}"


def _error_pointer(parent: str, token: str) -> str:
    if len(token) > _MAX_ERROR_PATH_LENGTH:
        return _bounded_error_path(f"{parent}/{_TRUNCATED_POINTER_TOKEN}")
    return _bounded_error_path(join_json_pointer(parent, token))


def _failure(code: TaskStateErrorCode, path: str, message: str) -> TaskStateError:
    return TaskStateError(code, path, message)


def _validate_utf8_budget(value: str, path: str, maximum: int) -> None:
    if len(value) > maximum:
        raise _failure(TaskStateErrorCode.BUDGET_EXCEEDED, path, "text byte budget exceeded")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _failure(
            TaskStateErrorCode.INVALID_VALUE, path, "text must be valid Unicode"
        ) from exc
    if size > maximum:
        raise _failure(TaskStateErrorCode.BUDGET_EXCEEDED, path, "text byte budget exceeded")


def _enum(value: object, enum_type: type[StrEnum], path: str) -> StrEnum:
    if type(value) is enum_type:
        return value
    if type(value) is not str:
        raise _failure(TaskStateErrorCode.INVALID_TYPE, path, "expected a string enum value")
    if len(value) > _MAX_TEXT_LENGTH:
        raise _failure(TaskStateErrorCode.INVALID_VALUE, path, "unsupported enum value")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise _failure(TaskStateErrorCode.INVALID_VALUE, path, "unsupported enum value") from exc


def _text(value: object, path: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str:
        raise _failure(TaskStateErrorCode.INVALID_TYPE, path, "expected a string")
    if (
        len(value) > _MAX_TEXT_LENGTH
        or not value.strip()
        or not value.isprintable()
        or len(value.splitlines()) != 1
    ):
        raise _failure(
            TaskStateErrorCode.INVALID_VALUE, path, "expected bounded printable single-line text"
        )
    return value


def _identifier(value: object, kind: str, path: str, *, optional: bool = False) -> str | None:
    if value is None and not optional:
        raise _failure(
            TaskStateErrorCode.INVALID_IDENTIFIER, path, f"expected canonical {kind} identifier"
        )
    text = _text(value, path, optional=optional)
    if text is None:
        return None
    if not _IDENTIFIER_RE[kind].fullmatch(text):
        raise _failure(
            TaskStateErrorCode.INVALID_IDENTIFIER, path, f"expected canonical {kind} identifier"
        )
    return text


def _digest(value: object, path: str) -> str:
    text = _text(value, path)
    assert text is not None
    if not _DIGEST_RE.fullmatch(text):
        raise _failure(TaskStateErrorCode.INVALID_VALUE, path, "expected lowercase SHA-256 digest")
    return text


def task_creation_identity(create_key: object) -> tuple[str, str]:
    """Derive the frozen task identifier and complete replay digest for a create key."""

    text = _text(create_key, "/create_key")
    assert text is not None
    if _TASK_CREATE_KEY_RE.fullmatch(text) is None:
        raise _failure(
            TaskStateErrorCode.INVALID_VALUE,
            "/create_key",
            "expected canonical task creation key",
        )
    digest = hashlib.sha256(_TASK_CREATE_ID_DOMAIN + text.encode("utf-8")).hexdigest()
    return f"task_{digest[:32]}", digest


def _integer(value: object, path: str, *, minimum: int = 0) -> int:
    if type(value) is not int:
        raise _failure(TaskStateErrorCode.INVALID_TYPE, path, "expected an integer")
    if value < minimum or value > MAX_SAFE_JSON_INTEGER:
        raise _failure(TaskStateErrorCode.INVALID_VALUE, path, "integer is out of range")
    return value


def _mapping(
    value: object, path: str, *, allowed: set[str], required: set[str]
) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise _failure(TaskStateErrorCode.INVALID_TYPE, path, "expected a mapping")
    if len(value) > _MAX_MAPPING_FIELDS:
        raise _failure(TaskStateErrorCode.BUDGET_EXCEEDED, path, "mapping field budget exceeded")
    keys = tuple(value)
    if not all(type(key) is str for key in keys):
        raise _failure(TaskStateErrorCode.INVALID_TYPE, path, "mapping field names must be strings")
    unknown = tuple(key for key in keys if key not in allowed)
    if unknown:
        overlong = next(
            (key for key in unknown if len(key) > _MAX_ERROR_PATH_LENGTH),
            None,
        )
        name = overlong if overlong is not None else sorted(unknown)[0]
        raise _failure(
            TaskStateErrorCode.UNKNOWN_FIELD, _error_pointer(path, name), "unknown field"
        )
    missing = sorted(required - set(keys))
    if missing:
        raise _failure(
            TaskStateErrorCode.MISSING_FIELD,
            _error_pointer(path, missing[0]),
            "required field missing",
        )
    return dict(value)


def _sequence(value: object, path: str) -> Sequence[Any]:
    if type(value) not in {tuple, list}:
        raise _failure(TaskStateErrorCode.INVALID_TYPE, path, "expected a list")
    return tuple(value)


def _bounded_sequence(value: object, path: str, maximum: int) -> tuple[Any, ...]:
    if type(value) in {tuple, list} and len(value) > maximum:
        raise _failure(TaskStateErrorCode.BUDGET_EXCEEDED, path, "history budget exceeded")
    result = _sequence(value, path)
    return tuple(result)


def _prefixed_contract_error(exc: ContractValidationError, parent: str) -> TaskStateError:
    code = TaskStateErrorCode(exc.code.value)
    path = _bounded_error_path(f"{parent}{exc.path}" if exc.path else parent)
    return _failure(code, path, "nested contract rejected")


def _guard_exact_nested_containers(
    value: object,
    path: str,
) -> None:
    pending: list[tuple[object, str, int]] = [(value, path, 0)]
    cursor = 0
    nodes = 1
    visited: set[int] = set()
    while cursor < len(pending):
        current, current_path, ancestor_depth = pending[cursor]
        cursor += 1
        if isinstance(current, Mapping):
            if type(current) is not dict:
                raise _failure(
                    TaskStateErrorCode.INVALID_TYPE,
                    current_path,
                    "expected an exact mapping",
                )
            container_depth = ancestor_depth + 1
            if container_depth > _MAX_NESTED_PREFLIGHT_DEPTH:
                raise _failure(
                    TaskStateErrorCode.BUDGET_EXCEEDED,
                    path,
                    "nested preflight depth budget exceeded",
                )
            identity = id(current)
            if identity in visited:
                raise _failure(
                    TaskStateErrorCode.INVALID_VALUE,
                    current_path,
                    "repeated or cyclic nested container",
                )
            visited.add(identity)
            children = current.items()
            mapping_children = True
        elif isinstance(current, (list, tuple)):
            if type(current) not in {list, tuple}:
                raise _failure(
                    TaskStateErrorCode.INVALID_TYPE,
                    current_path,
                    "expected an exact list",
                )
            container_depth = ancestor_depth + 1
            if container_depth > _MAX_NESTED_PREFLIGHT_DEPTH:
                raise _failure(
                    TaskStateErrorCode.BUDGET_EXCEEDED,
                    path,
                    "nested preflight depth budget exceeded",
                )
            identity = id(current)
            if identity in visited:
                raise _failure(
                    TaskStateErrorCode.INVALID_VALUE,
                    current_path,
                    "repeated or cyclic nested container",
                )
            visited.add(identity)
            children = enumerate(current)
            mapping_children = False
        else:
            if type(current) is str:
                _validate_utf8_budget(current, path, _MAX_JSON_STRING_BYTES)
            elif type(current) is int and (
                current < -MAX_SAFE_JSON_INTEGER or current > MAX_SAFE_JSON_INTEGER
            ):
                raise _failure(
                    TaskStateErrorCode.INVALID_VALUE,
                    current_path,
                    "integer is out of range",
                )
            continue
        for token, child in children:
            nodes += 1
            if nodes > _MAX_NESTED_PREFLIGHT_NODES:
                raise _failure(
                    TaskStateErrorCode.BUDGET_EXCEEDED,
                    path,
                    "nested preflight node budget exceeded",
                )
            if mapping_children:
                if type(token) is str:
                    _validate_utf8_budget(token, path, _MAX_JSON_KEY_BYTES)
                    child_path = _error_pointer(current_path, token)
                else:
                    child_path = current_path
            else:
                child_path = _error_pointer(current_path, str(token))
            pending.append((child, child_path, container_depth))


def _parse_nested[ResultT](
    value: object,
    parser: Any,
    parent: str,
    *,
    optional: bool = False,
) -> ResultT | None:
    if value is None and optional:
        return None
    _guard_exact_nested_containers(value, parent)
    try:
        return parser(value)
    except ContractValidationError as exc:
        raise _prefixed_contract_error(exc, parent) from exc
    except TaskStateError as exc:
        path = _bounded_error_path(f"{parent}{exc.path}" if exc.path else parent)
        raise _failure(exc.code, path, exc.message) from exc


def _freeze_json(
    value: object,
    path: str,
    *,
    depth: int = 0,
    nodes: list[int] | None = None,
    seen: set[int] | None = None,
    root_path: str | None = None,
) -> object:
    counter = nodes if nodes is not None else [0]
    budget_path = root_path if root_path is not None else path
    counter[0] += 1
    if counter[0] > MAX_JSON_NODES:
        raise _failure(TaskStateErrorCode.BUDGET_EXCEEDED, budget_path, "JSON node budget exceeded")
    if value is None or type(value) is bool:
        return value
    if type(value) is str:
        _validate_utf8_budget(value, budget_path, _MAX_JSON_STRING_BYTES)
        return value
    if type(value) is int:
        if value < -MAX_SAFE_JSON_INTEGER or value > MAX_SAFE_JSON_INTEGER:
            raise _failure(TaskStateErrorCode.INVALID_VALUE, path, "integer is out of range")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _failure(TaskStateErrorCode.INVALID_VALUE, path, "number must be finite")
        return value
    if type(value) not in {dict, list, tuple}:
        raise _failure(TaskStateErrorCode.INVALID_TYPE, budget_path, "expected a JSON value")
    if depth >= MAX_JSON_DEPTH:
        raise _failure(
            TaskStateErrorCode.BUDGET_EXCEEDED,
            budget_path,
            "JSON nesting budget exceeded",
        )
    visited = seen if seen is not None else set()
    identity = id(value)
    if identity in visited:
        raise _failure(TaskStateErrorCode.INVALID_VALUE, path, "repeated or cyclic JSON value")
    visited.add(identity)
    if type(value) is dict:
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise _failure(
                    TaskStateErrorCode.INVALID_TYPE, budget_path, "JSON keys must be strings"
                )
            _validate_utf8_budget(key, budget_path, _MAX_JSON_KEY_BYTES)
            frozen[key] = _freeze_json(
                item,
                _error_pointer(path, key),
                depth=depth + 1,
                nodes=counter,
                seen=visited,
                root_path=budget_path,
            )
        return MappingProxyType(frozen)
    return tuple(
        _freeze_json(
            item,
            _error_pointer(path, str(index)),
            depth=depth + 1,
            nodes=counter,
            seen=visited,
            root_path=budget_path,
        )
        for index, item in enumerate(value)
    )


def _thaw_json(value: object) -> object:
    if type(value) is MappingProxyType:
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


def _measurement(
    value: object, path: str, *, nonnegative: bool
) -> int | float | tuple[int | float, ...] | None:
    if value is None:
        return None
    scalar = type(value) in {bool, int, float}
    values = (value,) if scalar else _bounded_sequence(value, path, 16)
    if not values:
        raise _failure(TaskStateErrorCode.INVALID_VALUE, path, "numeric vector must not be empty")
    result: list[int | float] = []
    for index, item in enumerate(values):
        item_path = path if scalar else _error_pointer(path, str(index))
        if type(item) not in {int, float} or type(item) is bool:
            raise _failure(
                TaskStateErrorCode.INVALID_VALUE,
                item_path,
                "expected finite number",
            )
        if type(item) is int and (item < -MAX_SAFE_JSON_INTEGER or item > MAX_SAFE_JSON_INTEGER):
            raise _failure(
                TaskStateErrorCode.INVALID_VALUE,
                item_path,
                "integer is out of range",
            )
        if type(item) is float and not math.isfinite(item):
            raise _failure(TaskStateErrorCode.INVALID_VALUE, item_path, "expected finite number")
        if nonnegative and item < 0:
            raise _failure(
                TaskStateErrorCode.INVALID_VALUE,
                item_path,
                "must be non-negative",
            )
        result.append(item)
    return result[0] if scalar else tuple(result)


def _thaw_measurement(value: object) -> object:
    return list(value) if type(value) is tuple else value


def _schema_version(value: object, path: str = "/schema_version") -> int:
    if type(value) is not int:
        raise _failure(TaskStateErrorCode.INVALID_TYPE, path, "schema_version must be an integer")
    if value != SCHEMA_VERSION:
        code = (
            TaskStateErrorCode.INVALID_VALUE
            if value < -MAX_SAFE_JSON_INTEGER or value > MAX_SAFE_JSON_INTEGER
            else TaskStateErrorCode.UNSUPPORTED_VERSION
        )
        raise _failure(code, path, "unsupported schema_version")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskArtifactRef:
    """A content-addressed artifact reference with no filesystem pathname."""

    id: str
    name: str
    format: str
    sha256: str
    size_bytes: int
    candidate_revision: str
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "id", _identifier(self.id, "artifact", "/id"))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        object.__setattr__(self, "format", _text(self.format, "/format"))
        object.__setattr__(self, "sha256", _digest(self.sha256, "/sha256"))
        object.__setattr__(self, "size_bytes", _integer(self.size_bytes, "/size_bytes"))
        object.__setattr__(
            self,
            "candidate_revision",
            _identifier(self.candidate_revision, "revision", "/candidate_revision"),
        )
        if self.name in {".", ".."} or "/" in self.name or "\\" in self.name:
            raise _failure(
                TaskStateErrorCode.INVALID_VALUE, "/name", "artifact name must not be a path"
            )

    def to_mapping(self) -> dict[str, int | str]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "format": self.format,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "candidate_revision": self.candidate_revision,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _mapping(
            value,
            "",
            allowed={
                "schema_version",
                "id",
                "name",
                "format",
                "sha256",
                "size_bytes",
                "candidate_revision",
            },
            required={
                "schema_version",
                "id",
                "name",
                "format",
                "sha256",
                "size_bytes",
            },
        )
        return cls(
            schema_version=data["schema_version"],
            id=data["id"],
            name=data["name"],
            format=data["format"],
            sha256=data["sha256"],
            size_bytes=data["size_bytes"],
            candidate_revision=data.get("candidate_revision"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CriterionVerdict:
    """One durable acceptance-criterion result."""

    criterion_id: str
    required: bool
    message: str
    outcome: CriterionOutcome
    expected: object = None
    observed: object = None
    delta: int | float | tuple[int | float, ...] | None = None
    tolerance: int | float | tuple[int | float, ...] | None = None
    evidence: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "criterion_id", _text(self.criterion_id, "/criterion_id"))
        if type(self.required) is not bool:
            raise _failure(TaskStateErrorCode.INVALID_TYPE, "/required", "expected a boolean")
        outcome = _enum(self.outcome, CriterionOutcome, "/outcome")
        assert type(outcome) is CriterionOutcome
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "expected", _freeze_json(self.expected, "/expected"))
        object.__setattr__(self, "observed", _freeze_json(self.observed, "/observed"))
        object.__setattr__(self, "delta", _measurement(self.delta, "/delta", nonnegative=False))
        object.__setattr__(
            self,
            "tolerance",
            _measurement(self.tolerance, "/tolerance", nonnegative=True),
        )
        evidence = _bounded_sequence(self.evidence, "/evidence", MAX_VERDICT_EVIDENCE)
        for pointer in evidence:
            if type(pointer) is not str:
                raise _failure(
                    TaskStateErrorCode.INVALID_VALUE,
                    "/evidence",
                    "expected canonical evidence pointers",
                )
            _validate_utf8_budget(pointer, "/evidence", _MAX_EVIDENCE_POINTER_BYTES)
            if not is_canonical_json_pointer(pointer):
                raise _failure(
                    TaskStateErrorCode.INVALID_VALUE,
                    "/evidence",
                    "expected canonical evidence pointers",
                )
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "message", _text(self.message, "/message"))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "criterion_id": self.criterion_id,
            "required": self.required,
            "outcome": self.outcome.value,
            "expected": _thaw_json(self.expected),
            "observed": _thaw_json(self.observed),
            "delta": _thaw_measurement(self.delta),
            "tolerance": _thaw_measurement(self.tolerance),
            "evidence": list(self.evidence),
            "message": self.message,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _mapping(
            value,
            "",
            allowed={
                "schema_version",
                "criterion_id",
                "required",
                "outcome",
                "expected",
                "observed",
                "delta",
                "tolerance",
                "evidence",
                "message",
            },
            required={
                "schema_version",
                "criterion_id",
                "required",
                "outcome",
                "expected",
                "observed",
                "delta",
                "tolerance",
                "evidence",
                "message",
            },
        )
        return cls(
            schema_version=data["schema_version"],
            criterion_id=data["criterion_id"],
            required=data["required"],
            outcome=data["outcome"],
            expected=data["expected"],
            observed=data["observed"],
            delta=data["delta"],
            tolerance=data["tolerance"],
            evidence=_bounded_sequence(data["evidence"], "/evidence", MAX_VERDICT_EVIDENCE),
            message=data["message"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class VerificationReport:
    """Trusted verifier output, bound to one candidate snapshot."""

    id: str
    acceptance_id: str
    candidate_revision: str
    manifest_sha256: str
    observation_digest: str
    passed: bool
    verdicts: tuple[CriterionVerdict, ...]
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "id", _identifier(self.id, "verification", "/id"))
        object.__setattr__(self, "acceptance_id", _text(self.acceptance_id, "/acceptance_id"))
        object.__setattr__(
            self,
            "candidate_revision",
            _identifier(self.candidate_revision, "revision", "/candidate_revision"),
        )
        object.__setattr__(
            self, "manifest_sha256", _digest(self.manifest_sha256, "/manifest_sha256")
        )
        object.__setattr__(
            self, "observation_digest", _digest(self.observation_digest, "/observation_digest")
        )
        if type(self.passed) is not bool:
            raise _failure(TaskStateErrorCode.INVALID_TYPE, "/passed", "expected a boolean")
        verdicts = _bounded_sequence(self.verdicts, "/verdicts", MAX_CRITERION_VERDICTS)
        if not verdicts:
            raise _failure(
                TaskStateErrorCode.INVALID_VALUE,
                "/verdicts",
                "verification report must contain verdicts",
            )
        if not all(type(verdict) is CriterionVerdict for verdict in verdicts):
            raise _failure(
                TaskStateErrorCode.INVALID_TYPE, "/verdicts", "expected CriterionVerdict values"
            )
        ids = [verdict.criterion_id for verdict in verdicts]
        if len(ids) != len(set(ids)):
            raise _failure(
                TaskStateErrorCode.DUPLICATE_IDENTIFIER,
                "/verdicts",
                "criterion identifiers must be unique",
            )
        if self.passed != all(
            not verdict.required or verdict.outcome is CriterionOutcome.PASS for verdict in verdicts
        ):
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/passed",
                "passed must match required verdicts",
            )
        object.__setattr__(self, "verdicts", verdicts)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "acceptance_id": self.acceptance_id,
            "candidate_revision": self.candidate_revision,
            "manifest_sha256": self.manifest_sha256,
            "observation_digest": self.observation_digest,
            "passed": self.passed,
            "verdicts": [item.to_mapping() for item in self.verdicts],
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _mapping(
            value,
            "",
            allowed={
                "schema_version",
                "id",
                "acceptance_id",
                "candidate_revision",
                "manifest_sha256",
                "observation_digest",
                "passed",
                "verdicts",
            },
            required={
                "schema_version",
                "id",
                "acceptance_id",
                "manifest_sha256",
                "observation_digest",
                "passed",
                "verdicts",
            },
        )
        raw_verdicts = _bounded_sequence(data["verdicts"], "/verdicts", MAX_CRITERION_VERDICTS)
        verdicts = tuple(
            _parse_nested(
                item,
                CriterionVerdict.from_mapping,
                join_json_pointer("/verdicts", str(index)),
            )
            for index, item in enumerate(raw_verdicts)
        )
        return cls(
            schema_version=data["schema_version"],
            id=data["id"],
            acceptance_id=data["acceptance_id"],
            candidate_revision=data.get("candidate_revision"),
            manifest_sha256=data["manifest_sha256"],
            observation_digest=data["observation_digest"],
            passed=data["passed"],
            verdicts=verdicts,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewDraft:
    """Path-free durable identity for one immutable verified draft revision."""

    id: str
    task_id: str
    project_id: str
    base_revision: str
    base_generation: int
    base_manifest_sha256: str
    revision_id: str
    manifest_sha256: str
    verification_id: str
    acceptance_id: str
    observation_digest: str
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "id", _identifier(self.id, "draft", "/id"))
        object.__setattr__(self, "task_id", _identifier(self.task_id, "task", "/task_id"))
        object.__setattr__(
            self,
            "project_id",
            _identifier(self.project_id, "project", "/project_id"),
        )
        object.__setattr__(
            self,
            "base_revision",
            _identifier(self.base_revision, "revision", "/base_revision"),
        )
        object.__setattr__(
            self,
            "base_generation",
            _integer(self.base_generation, "/base_generation"),
        )
        object.__setattr__(
            self,
            "base_manifest_sha256",
            _digest(self.base_manifest_sha256, "/base_manifest_sha256"),
        )
        object.__setattr__(
            self,
            "revision_id",
            _identifier(self.revision_id, "revision", "/revision_id"),
        )
        object.__setattr__(
            self,
            "manifest_sha256",
            _digest(self.manifest_sha256, "/manifest_sha256"),
        )
        object.__setattr__(
            self,
            "verification_id",
            _identifier(self.verification_id, "verification", "/verification_id"),
        )
        object.__setattr__(self, "acceptance_id", _text(self.acceptance_id, "/acceptance_id"))
        object.__setattr__(
            self,
            "observation_digest",
            _digest(self.observation_digest, "/observation_digest"),
        )
        expected_id = f"draft_{self.revision_id.removeprefix('revision_')}"
        if self.id != expected_id:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/id",
                "draft identifier must derive from revision identifier",
            )

    def to_mapping(self) -> dict[str, int | str]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "base_revision": self.base_revision,
            "base_generation": self.base_generation,
            "base_manifest_sha256": self.base_manifest_sha256,
            "revision_id": self.revision_id,
            "manifest_sha256": self.manifest_sha256,
            "verification_id": self.verification_id,
            "acceptance_id": self.acceptance_id,
            "observation_digest": self.observation_digest,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        fields = {
            "schema_version",
            "id",
            "task_id",
            "project_id",
            "base_revision",
            "base_generation",
            "base_manifest_sha256",
            "revision_id",
            "manifest_sha256",
            "verification_id",
            "acceptance_id",
            "observation_digest",
        }
        data = _mapping(value, "", allowed=fields, required=fields)
        return cls(
            schema_version=data["schema_version"],
            id=data["id"],
            task_id=data["task_id"],
            project_id=data["project_id"],
            base_revision=data["base_revision"],
            base_generation=data["base_generation"],
            base_manifest_sha256=data["base_manifest_sha256"],
            revision_id=data["revision_id"],
            manifest_sha256=data["manifest_sha256"],
            verification_id=data["verification_id"],
            acceptance_id=data["acceptance_id"],
            observation_digest=data["observation_digest"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskTransitionRecord:
    """Monotonic audit record for one legal state transition."""

    sequence: int
    event: TaskEvent
    from_status: TaskStatus
    to_status: TaskStatus
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "sequence", _integer(self.sequence, "/sequence", minimum=1))
        object.__setattr__(self, "event", _enum(self.event, TaskEvent, "/event"))
        object.__setattr__(self, "from_status", _enum(self.from_status, TaskStatus, "/from_status"))
        object.__setattr__(self, "to_status", _enum(self.to_status, TaskStatus, "/to_status"))

    def to_mapping(self) -> dict[str, int | str]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "event": self.event.value,
            "from_status": self.from_status.value,
            "to_status": self.to_status.value,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _mapping(
            value,
            "",
            allowed={"schema_version", "sequence", "event", "from_status", "to_status"},
            required={"schema_version", "sequence", "event", "from_status", "to_status"},
        )
        return cls(
            schema_version=data["schema_version"],
            sequence=data["sequence"],
            event=data["event"],
            from_status=data["from_status"],
            to_status=data["to_status"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskStepRecord:
    """A monotonic, immutable record of an adapter ``StepResult``."""

    sequence: int
    result: StepResult
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "sequence", _integer(self.sequence, "/sequence", minimum=1))
        if type(self.result) is not StepResult:
            raise _failure(TaskStateErrorCode.INVALID_TYPE, "/result", "expected StepResult")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "result": self.result.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _mapping(
            value,
            "",
            allowed={"schema_version", "sequence", "result"},
            required={"schema_version", "sequence", "result"},
        )
        result = _parse_nested(data["result"], StepResult.from_mapping, "/result")
        assert isinstance(result, StepResult)
        return cls(schema_version=data["schema_version"], sequence=data["sequence"], result=result)


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskRun:
    """Strict schema-v1 durable task state, with no host object references."""

    id: str
    project_id: str
    base_revision: str
    reasoning_owner: ReasoningOwner
    review_policy: ReviewPolicy
    status: TaskStatus
    creation_digest: str | None = None
    program: ModelProgram | None = None
    candidate_revision: str | None = None
    committed_revision: str | None = None
    draft: ReviewDraft | None = None
    steps: tuple[TaskStepRecord, ...] = ()
    verification_reports: tuple[VerificationReport, ...] = ()
    artifacts: tuple[TaskArtifactRef, ...] = ()
    last_error: StepError | None = None
    transitions: tuple[TaskTransitionRecord, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "id", _identifier(self.id, "task", "/id"))
        object.__setattr__(
            self, "project_id", _identifier(self.project_id, "project", "/project_id")
        )
        object.__setattr__(
            self, "base_revision", _identifier(self.base_revision, "revision", "/base_revision")
        )
        object.__setattr__(
            self, "reasoning_owner", _enum(self.reasoning_owner, ReasoningOwner, "/reasoning_owner")
        )
        if type(self.review_policy) is not ReviewPolicy:
            raise _failure(
                TaskStateErrorCode.INVALID_TYPE,
                "/review_policy",
                "expected ReviewPolicy",
            )
        object.__setattr__(self, "status", _enum(self.status, TaskStatus, "/status"))
        if self.creation_digest is not None:
            object.__setattr__(
                self,
                "creation_digest",
                _digest(self.creation_digest, "/creation_digest"),
            )
            if self.id != f"task_{self.creation_digest[:32]}":
                raise _failure(
                    TaskStateErrorCode.INVARIANT_VIOLATION,
                    "/creation_digest",
                    "creation digest must bind task id",
                )
        if self.program is not None and type(self.program) is not ModelProgram:
            raise _failure(
                TaskStateErrorCode.INVALID_TYPE, "/program", "expected ModelProgram or null"
            )
        if self.program is not None:
            if self.program.task_id != self.id:
                raise _failure(
                    TaskStateErrorCode.PROGRAM_MISMATCH,
                    "/program/task_id",
                    "program task id must match task id",
                )
            if self.program.base_revision != self.base_revision:
                raise _failure(
                    TaskStateErrorCode.PROGRAM_MISMATCH,
                    "/program/base_revision",
                    "program base revision must match task base revision",
                )
        object.__setattr__(
            self,
            "candidate_revision",
            _identifier(self.candidate_revision, "revision", "/candidate_revision", optional=True),
        )
        object.__setattr__(
            self,
            "committed_revision",
            _identifier(self.committed_revision, "revision", "/committed_revision", optional=True),
        )
        if self.draft is not None and type(self.draft) is not ReviewDraft:
            raise _failure(
                TaskStateErrorCode.INVALID_TYPE,
                "/draft",
                "expected ReviewDraft or null",
            )
        steps = _tuple_of(self.steps, TaskStepRecord, "/steps", MAX_STEP_RECORDS)
        reports = _tuple_of(
            self.verification_reports,
            VerificationReport,
            "/verification_reports",
            MAX_VERIFICATION_REPORTS,
        )
        artifacts = _tuple_of(self.artifacts, TaskArtifactRef, "/artifacts", MAX_ARTIFACT_REFS)
        transitions = _tuple_of(
            self.transitions, TaskTransitionRecord, "/transitions", MAX_TRANSITION_RECORDS
        )
        _unique([report.id for report in reports], "/verification_reports")
        _unique([artifact.id for artifact in artifacts], "/artifacts")
        _sequences([record.sequence for record in steps], "/steps")
        _sequences([record.sequence for record in transitions], "/transitions")
        _transition_history(transitions, self.status)
        if self.last_error is not None and type(self.last_error) is not StepError:
            raise _failure(
                TaskStateErrorCode.INVALID_TYPE, "/last_error", "expected StepError or null"
            )
        object.__setattr__(self, "steps", steps)
        object.__setattr__(self, "verification_reports", reports)
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "transitions", transitions)
        self._validate_invariants()

    def _validate_invariants(self) -> None:
        candidate_created = any(
            record.event is TaskEvent.VALIDATE_PROGRAM for record in self.transitions
        )
        candidate_required = self.status in _CANDIDATE_ACTIVE_STATUSES | {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.REJECTED,
        } or (
            candidate_created
            and self.status in {TaskStatus.RECOVERY_REQUIRED, TaskStatus.CLEANUP_REQUIRED}
        )
        if candidate_required and self.candidate_revision is None:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/candidate_revision",
                "candidate revision is required for this status",
            )
        if candidate_created != (self.candidate_revision is not None):
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/candidate_revision",
                "candidate revision must match transition provenance",
            )
        transition_events = tuple(record.event for record in self.transitions)
        program_submitted = TaskEvent.SUBMIT_PROGRAM in transition_events
        if self.status in _CANCELLATION_STATUSES and (
            program_submitted != (self.program is not None)
        ):
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/program",
                "cancel state program must match submission provenance",
            )
        review_started = TaskEvent.PREPARE_REVIEW in transition_events
        if self.review_policy is ReviewPolicy.AUTO_COMMIT:
            if (
                self.draft is not None
                or any(event in _REVIEW_EVENTS for event in transition_events)
                or self.status in _REVIEW_STATUSES
            ):
                raise _failure(
                    TaskStateErrorCode.INVARIANT_VIOLATION,
                    "/review_policy",
                    "auto-commit task cannot contain review state",
                )
        else:
            if TaskEvent.PASS_VERIFICATION in transition_events or any(
                record.to_status is TaskStatus.COMMITTING for record in self.transitions
            ):
                raise _failure(
                    TaskStateErrorCode.INVARIANT_VIOLATION,
                    "/review_policy",
                    "review task cannot use auto-commit transitions",
                )
            if review_started != (self.draft is not None):
                raise _failure(
                    TaskStateErrorCode.INVARIANT_VIOLATION,
                    "/draft",
                    "draft presence must match review provenance",
                )
        if self.status in _REVIEW_STATUSES and not review_started:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/draft",
                "review status requires draft provenance",
            )
        if self.draft is not None:
            draft_bindings = (
                (self.draft.task_id, self.id, "/draft/task_id"),
                (self.draft.project_id, self.project_id, "/draft/project_id"),
                (self.draft.base_revision, self.base_revision, "/draft/base_revision"),
                (
                    self.draft.revision_id,
                    self.candidate_revision,
                    "/draft/revision_id",
                ),
            )
            for actual, expected, path in draft_bindings:
                if actual != expected:
                    raise _failure(
                        TaskStateErrorCode.INVARIANT_VIOLATION,
                        path,
                        "draft identity does not bind owning task",
                    )
            matching_reports = tuple(
                report
                for report in self.verification_reports
                if report.id == self.draft.verification_id
            )
            if len(matching_reports) != 1:
                raise _failure(
                    TaskStateErrorCode.INVARIANT_VIOLATION,
                    "/draft/verification_id",
                    "draft must bind one persisted verification report",
                )
            report = matching_reports[0]
            if not report.passed:
                raise _failure(
                    TaskStateErrorCode.INVARIANT_VIOLATION,
                    "/draft/verification_id",
                    "draft verification report must pass",
                )
            report_bindings = (
                (
                    self.draft.manifest_sha256,
                    report.manifest_sha256,
                    "/draft/manifest_sha256",
                ),
                (
                    self.draft.acceptance_id,
                    report.acceptance_id,
                    "/draft/acceptance_id",
                ),
                (
                    self.draft.observation_digest,
                    report.observation_digest,
                    "/draft/observation_digest",
                ),
                (
                    self.draft.revision_id,
                    report.candidate_revision,
                    "/draft/revision_id",
                ),
            )
            for actual, expected, path in report_bindings:
                if actual != expected:
                    raise _failure(
                        TaskStateErrorCode.INVARIANT_VIOLATION,
                        path,
                        "draft does not bind its passing report",
                    )
            if self.program is None or self.draft.acceptance_id != self.program.acceptance.id:
                raise _failure(
                    TaskStateErrorCode.INVARIANT_VIOLATION,
                    "/draft/acceptance_id",
                    "draft must bind submitted acceptance",
                )
        if self.status in {TaskStatus.CREATED, TaskStatus.NEEDS_PLAN} and self.program is not None:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/program",
                "program is not allowed before submission",
            )
        if (
            self.status
            in {TaskStatus.PROGRAM_READY, TaskStatus.VALIDATING_PROGRAM, TaskStatus.NEEDS_INPUT}
            and self.program is None
        ):
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/program",
                "program is required after submission",
            )
        if (
            self.status
            in _CANDIDATE_ACTIVE_STATUSES
            | {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.REJECTED,
                TaskStatus.RECOVERY_REQUIRED,
                TaskStatus.CLEANUP_REQUIRED,
            }
            and self.program is None
        ):
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/program",
                "program is required after candidate creation",
            )
        if self.status is TaskStatus.SUCCEEDED:
            if self.committed_revision is None:
                raise _failure(
                    TaskStateErrorCode.INVARIANT_VIOLATION,
                    "/committed_revision",
                    "succeeded task requires committed revision",
                )
            if self.committed_revision != self.candidate_revision:
                raise _failure(
                    TaskStateErrorCode.INVARIANT_VIOLATION,
                    "/committed_revision",
                    "committed revision must equal the verified candidate",
                )
            if not any(
                report.passed and report.candidate_revision == self.candidate_revision
                for report in self.verification_reports
            ):
                raise _failure(
                    TaskStateErrorCode.INVARIANT_VIOLATION,
                    "/verification_reports",
                    "succeeded task requires passing candidate verification",
                )
            if self.review_policy is ReviewPolicy.REQUIRE_REVIEW:
                if self.draft is None or self.committed_revision != self.draft.revision_id:
                    raise _failure(
                        TaskStateErrorCode.INVARIANT_VIOLATION,
                        "/committed_revision",
                        "reviewed success must commit its exact draft",
                    )
                if TaskEvent.ACCEPT_DRAFT not in transition_events:
                    raise _failure(
                        TaskStateErrorCode.INVARIANT_VIOLATION,
                        "/transitions",
                        "reviewed success requires explicit acceptance provenance",
                    )
        elif self.committed_revision is not None:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/committed_revision",
                "only succeeded task may contain committed revision",
            )
        if self.status is TaskStatus.REJECTED:
            if self.review_policy is not ReviewPolicy.REQUIRE_REVIEW or self.draft is None:
                raise _failure(
                    TaskStateErrorCode.INVARIANT_VIOLATION,
                    "/draft",
                    "rejected task requires durable review evidence",
                )
        last_transition = self.transitions[-1] if self.transitions else None
        reconciled_success = (
            self.status is TaskStatus.SUCCEEDED
            and last_transition is not None
            and last_transition.event is TaskEvent.CONFIRM_COMMITTED
            and last_transition.from_status
            in {TaskStatus.RECOVERY_REQUIRED, TaskStatus.CLEANUP_REQUIRED}
        )
        error_required = (
            self.status
            in {
                TaskStatus.ROLLING_BACK,
                TaskStatus.NEEDS_INPUT,
                TaskStatus.RECOVERY_REQUIRED,
                TaskStatus.CLEANUP_REQUIRED,
                TaskStatus.FAILED,
            }
            or reconciled_success
        )
        if error_required and self.last_error is None:
            raise _failure(
                TaskStateErrorCode.MISSING_ERROR,
                "/last_error",
                "failure outcome requires structured error",
            )
        if not error_required and self.last_error is not None:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/last_error",
                "structured error is not allowed for this status",
            )
        if (
            self.status is TaskStatus.NEEDS_INPUT
            and self.last_error is not None
            and not self.last_error.needs_input
        ):
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/last_error/needs_input",
                "needs_input requires an input error",
            )
        if self.candidate_revision is None and (
            self.steps or self.verification_reports or self.artifacts
        ):
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/candidate_revision",
                "candidate data requires candidate revision",
            )
        reached_verifying = any(
            record.to_status is TaskStatus.VERIFYING for record in self.transitions
        )
        if self.verification_reports and not reached_verifying:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/verification_reports",
                "reports require verifying transition provenance",
            )
        if any(
            report.candidate_revision != self.candidate_revision
            for report in self.verification_reports
        ):
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/verification_reports",
                "reports must bind current candidate",
            )
        if any(
            artifact.candidate_revision != self.candidate_revision for artifact in self.artifacts
        ):
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/artifacts",
                "artifacts must bind current candidate",
            )
        if self.program is not None and any(
            report.acceptance_id != self.program.acceptance.id
            for report in self.verification_reports
        ):
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/verification_reports",
                "reports must bind submitted acceptance",
            )
        if any(step.result.revision != self.candidate_revision for step in self.steps):
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/steps",
                "steps must bind current candidate",
            )
        if self.status is TaskStatus.COMMITTING and not any(
            report.passed and report.candidate_revision == self.candidate_revision
            for report in self.verification_reports
        ):
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/verification_reports",
                "committing task requires passing candidate verification",
            )
        if self.status is TaskStatus.FAILED and TaskStatus.ROLLING_BACK not in {
            record.to_status for record in self.transitions
        }:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/transitions",
                "candidate failure must pass through rolling_back",
            )
        if self.status is TaskStatus.NEEDS_INPUT and self.candidate_revision is not None:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/candidate_revision",
                "needs_input cannot retain a published candidate",
            )

    @property
    def next_action(self) -> NextAction:
        """Return deterministic, non-persisted caller guidance."""

        return next_action_for(self.status)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "project_id": self.project_id,
            "base_revision": self.base_revision,
            "reasoning_owner": self.reasoning_owner.value,
            "review_policy": self.review_policy.value,
            "status": self.status.value,
            "creation_digest": self.creation_digest,
            "program": None if self.program is None else self.program.to_mapping(),
            "candidate_revision": self.candidate_revision,
            "committed_revision": self.committed_revision,
            "draft": None if self.draft is None else self.draft.to_mapping(),
            "steps": [item.to_mapping() for item in self.steps],
            "verification_reports": [item.to_mapping() for item in self.verification_reports],
            "artifacts": [item.to_mapping() for item in self.artifacts],
            "last_error": None if self.last_error is None else self.last_error.to_mapping(),
            "transitions": [item.to_mapping() for item in self.transitions],
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        fields = {
            "schema_version",
            "id",
            "project_id",
            "base_revision",
            "reasoning_owner",
            "review_policy",
            "status",
            "creation_digest",
            "program",
            "candidate_revision",
            "committed_revision",
            "draft",
            "steps",
            "verification_reports",
            "artifacts",
            "last_error",
            "transitions",
        }
        data = _mapping(
            value,
            "",
            allowed=fields,
            required=fields - {"creation_digest"},
        )
        program_raw = data["program"]
        draft_raw = data["draft"]
        error_raw = data["last_error"]
        raw_steps = _bounded_sequence(data["steps"], "/steps", MAX_STEP_RECORDS)
        raw_reports = _bounded_sequence(
            data["verification_reports"], "/verification_reports", MAX_VERIFICATION_REPORTS
        )
        raw_artifacts = _bounded_sequence(data["artifacts"], "/artifacts", MAX_ARTIFACT_REFS)
        raw_transitions = _bounded_sequence(
            data["transitions"], "/transitions", MAX_TRANSITION_RECORDS
        )
        program = _parse_nested(program_raw, ModelProgram.from_mapping, "/program", optional=True)
        draft = _parse_nested(draft_raw, ReviewDraft.from_mapping, "/draft", optional=True)
        steps = tuple(
            _parse_nested(
                item, TaskStepRecord.from_mapping, join_json_pointer("/steps", str(index))
            )
            for index, item in enumerate(raw_steps)
        )
        reports = tuple(
            _parse_nested(
                item,
                VerificationReport.from_mapping,
                join_json_pointer("/verification_reports", str(index)),
            )
            for index, item in enumerate(raw_reports)
        )
        artifacts = tuple(
            _parse_nested(
                item, TaskArtifactRef.from_mapping, join_json_pointer("/artifacts", str(index))
            )
            for index, item in enumerate(raw_artifacts)
        )
        error = _parse_nested(error_raw, StepError.from_mapping, "/last_error", optional=True)
        transitions = tuple(
            _parse_nested(
                item,
                TaskTransitionRecord.from_mapping,
                join_json_pointer("/transitions", str(index)),
            )
            for index, item in enumerate(raw_transitions)
        )
        return cls(
            schema_version=data["schema_version"],
            id=data["id"],
            project_id=data["project_id"],
            base_revision=data["base_revision"],
            reasoning_owner=data["reasoning_owner"],
            review_policy=_enum(data["review_policy"], ReviewPolicy, "/review_policy"),
            status=data["status"],
            creation_digest=data.get("creation_digest"),
            program=program,
            candidate_revision=data["candidate_revision"],
            committed_revision=data["committed_revision"],
            draft=draft,
            steps=steps,
            verification_reports=reports,
            artifacts=artifacts,
            last_error=error,
            transitions=transitions,
        )


def _tuple_of(value: object, element_type: type[Any], path: str, maximum: int) -> tuple[Any, ...]:
    if type(value) not in {tuple, list}:
        raise _failure(TaskStateErrorCode.INVALID_TYPE, path, "expected a list")
    if len(value) > maximum:
        raise _failure(TaskStateErrorCode.BUDGET_EXCEEDED, path, "history budget exceeded")
    result = tuple(value)
    if not all(type(item) is element_type for item in result):
        raise _failure(
            TaskStateErrorCode.INVALID_TYPE, path, f"expected {element_type.__name__} values"
        )
    return result


def _unique(values: list[str], path: str) -> None:
    if len(values) != len(set(values)):
        raise _failure(TaskStateErrorCode.DUPLICATE_IDENTIFIER, path, "identifiers must be unique")


def _sequences(values: list[int], path: str) -> None:
    if values != list(range(1, len(values) + 1)):
        raise _failure(
            TaskStateErrorCode.INVARIANT_VIOLATION,
            path,
            "sequences must start at one and be contiguous",
        )


def _transition_history(records: tuple[TaskTransitionRecord, ...], status: TaskStatus) -> None:
    current = TaskStatus.CREATED
    candidate_published = False
    review_started = False
    cancel_requested = False
    cancellation_started = False
    for record in records:
        if record.from_status is not current:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/transitions",
                "transition history is discontinuous",
            )
        expected = _TRANSITIONS.get((record.from_status, record.event))
        if expected is not record.to_status:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/transitions",
                "transition history contains an illegal event",
            )
        if record.sequence > _MAX_ORDINARY_TRANSITION_RECORDS and not (
            record.event in _CANCELLATION_TAIL_EVENTS
            and (cancel_requested or record.event is TaskEvent.REQUEST_CANCEL)
        ):
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/transitions",
                "transition history exceeds the ordinary budget without cancellation",
            )
        if cancel_requested and record.event in _CANCELLATION_FORBIDDEN_RECOVERY_EVENTS:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/transitions",
                "cancelled work cannot resume an execution path",
            )
        if record.event is TaskEvent.REQUEST_CANCEL:
            if cancel_requested:
                raise _failure(
                    TaskStateErrorCode.INVARIANT_VIOLATION,
                    "/transitions",
                    "cancel request must be unique",
                )
            cancel_requested = True
        elif record.event is TaskEvent.START_CANCELLATION:
            if not cancel_requested or cancellation_started:
                raise _failure(
                    TaskStateErrorCode.INVARIANT_VIOLATION,
                    "/transitions",
                    "cancellation start requires one durable request",
                )
            cancellation_started = True
        elif record.event is TaskEvent.CONFIRM_CANCELLED and not cancellation_started:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/transitions",
                "cancel confirmation requires a started cancellation",
            )
        if record.event is TaskEvent.CONFIRM_PRE_CANDIDATE and candidate_published:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/transitions",
                "pre-candidate confirmation has candidate provenance",
            )
        if record.event in {TaskEvent.CONFIRM_COMMITTED, TaskEvent.CONFIRM_UNCOMMITTED} and not (
            candidate_published
        ):
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/transitions",
                "candidate confirmation lacks candidate provenance",
            )
        if record.event is TaskEvent.CONFIRM_DRAFT_UNCOMMITTED and not review_started:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/transitions",
                "draft confirmation lacks review provenance",
            )
        if record.event is TaskEvent.CONFIRM_UNCOMMITTED and review_started:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/transitions",
                "review recovery requires draft confirmation",
            )
        if record.event is TaskEvent.VALIDATE_PROGRAM:
            candidate_published = True
        if record.event is TaskEvent.PREPARE_REVIEW:
            review_started = True
        current = record.to_status
    if status in _CANCELLATION_STATUSES and not cancel_requested:
        raise _failure(
            TaskStateErrorCode.INVARIANT_VIOLATION,
            "/transitions",
            "cancel state requires durable request provenance",
        )
    if status is TaskStatus.CANCELLING and not cancellation_started:
        raise _failure(
            TaskStateErrorCode.INVARIANT_VIOLATION,
            "/transitions",
            "cancelling state requires start provenance",
        )
    if current is not status:
        raise _failure(
            TaskStateErrorCode.INVARIANT_VIOLATION,
            "/status",
            "status must match transition history",
        )


def new_task_run(
    *,
    task_id: str,
    project_id: str,
    base_revision: str,
    reasoning_owner: ReasoningOwner,
    review_policy: ReviewPolicy,
    creation_digest: str | None = None,
) -> TaskRun:
    """Create a new immutable task at the durable ``created`` state."""

    return TaskRun(
        id=task_id,
        project_id=project_id,
        base_revision=base_revision,
        reasoning_owner=reasoning_owner,
        review_policy=review_policy,
        status=TaskStatus.CREATED,
        creation_digest=creation_digest,
    )


def transition_task(
    task: TaskRun,
    event: TaskEvent,
    *,
    program: ModelProgram | None = None,
    candidate_revision: str | None = None,
    committed_revision: str | None = None,
    error: StepError | None = None,
    verification: VerificationReport | None = None,
    draft: ReviewDraft | None = None,
) -> TaskRun:
    """Apply one legal event without mutating the prior task record."""

    if type(task) is not TaskRun:
        raise _failure(TaskStateErrorCode.INVALID_TYPE, "/task", "expected TaskRun")
    event = _enum(event, TaskEvent, "/event")
    assert type(event) is TaskEvent
    if task.status in _TERMINAL_STATUSES:
        raise _failure(
            TaskStateErrorCode.TERMINAL_STATE, "/status", "terminal task has no successors"
        )
    target = _TRANSITIONS.get((task.status, event))
    if target is None:
        raise _failure(
            TaskStateErrorCode.INVALID_TRANSITION,
            "/event",
            "event is not allowed from current status",
        )
    cancel_requested = any(record.event is TaskEvent.REQUEST_CANCEL for record in task.transitions)
    cancellation_started = any(
        record.event is TaskEvent.START_CANCELLATION for record in task.transitions
    )
    if cancel_requested and event in _CANCELLATION_FORBIDDEN_RECOVERY_EVENTS:
        raise _failure(
            TaskStateErrorCode.INVALID_TRANSITION,
            "/event",
            "cancelled work cannot resume an execution path",
        )
    if event is TaskEvent.CONFIRM_CANCELLED and not (cancel_requested and cancellation_started):
        raise _failure(
            TaskStateErrorCode.INVALID_TRANSITION,
            "/event",
            "cancel confirmation requires durable start provenance",
        )
    if len(task.transitions) >= MAX_TRANSITION_RECORDS:
        raise _failure(
            TaskStateErrorCode.BUDGET_EXCEEDED,
            "/transitions",
            "transition history budget exceeded",
        )
    if len(task.transitions) >= _MAX_ORDINARY_TRANSITION_RECORDS and not (
        event is TaskEvent.REQUEST_CANCEL
        or (cancel_requested and event in _CANCELLATION_TAIL_EVENTS)
    ):
        raise _failure(
            TaskStateErrorCode.BUDGET_EXCEEDED,
            "/transitions",
            "ordinary transition history budget exceeded",
        )
    if event is TaskEvent.PASS_VERIFICATION and task.review_policy is not ReviewPolicy.AUTO_COMMIT:
        raise _failure(
            TaskStateErrorCode.INVALID_TRANSITION,
            "/event",
            "review policy forbids auto-commit verification",
        )
    if event in _REVIEW_EVENTS and task.review_policy is not ReviewPolicy.REQUIRE_REVIEW:
        raise _failure(
            TaskStateErrorCode.INVALID_TRANSITION,
            "/event",
            "review event requires explicit review policy",
        )
    if event is TaskEvent.CONFIRM_PRE_CANDIDATE and task.candidate_revision is not None:
        raise _failure(
            TaskStateErrorCode.INVALID_TRANSITION,
            "/event",
            "pre-candidate confirmation requires no published candidate",
        )
    if (
        event
        in {
            TaskEvent.CONFIRM_COMMITTED,
            TaskEvent.CONFIRM_UNCOMMITTED,
            TaskEvent.CONFIRM_DRAFT_UNCOMMITTED,
        }
        and task.candidate_revision is None
    ):
        raise _failure(
            TaskStateErrorCode.INVALID_TRANSITION,
            "/event",
            "candidate confirmation requires a published candidate",
        )
    if event is TaskEvent.CONFIRM_DRAFT_UNCOMMITTED and task.draft is None:
        raise _failure(
            TaskStateErrorCode.INVALID_TRANSITION,
            "/event",
            "draft confirmation requires review provenance",
        )
    if event is TaskEvent.CONFIRM_UNCOMMITTED and task.draft is not None:
        raise _failure(
            TaskStateErrorCode.INVALID_TRANSITION,
            "/event",
            "review recovery requires draft confirmation",
        )
    if event is TaskEvent.SUBMIT_PROGRAM:
        if type(program) is not ModelProgram:
            raise _failure(
                TaskStateErrorCode.INVALID_TYPE, "/program", "submit_program requires ModelProgram"
            )
        if program.task_id != task.id:
            raise _failure(
                TaskStateErrorCode.PROGRAM_MISMATCH,
                "/program/task_id",
                "program task id must match task id",
            )
        if program.base_revision != task.base_revision:
            raise _failure(
                TaskStateErrorCode.PROGRAM_MISMATCH,
                "/program/base_revision",
                "program base revision must match task base revision",
            )
    elif program is not None:
        raise _failure(
            TaskStateErrorCode.INVALID_VALUE,
            "/program",
            "program is only accepted on submit_program",
        )
    if event is TaskEvent.VALIDATE_PROGRAM:
        validated_candidate = _identifier(candidate_revision, "revision", "/candidate_revision")
        assert validated_candidate is not None
        if task.candidate_revision is not None and validated_candidate != task.candidate_revision:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/candidate_revision",
                "candidate continuation must preserve revision identity",
            )
        candidate_revision = task.candidate_revision or validated_candidate
    elif candidate_revision is not None:
        raise _failure(
            TaskStateErrorCode.INVALID_VALUE,
            "/candidate_revision",
            "candidate revision is only accepted on validate_program",
        )
    failure_events = {
        TaskEvent.REJECT_PROGRAM,
        TaskEvent.FAIL_EXECUTION,
        TaskEvent.FAIL_VERIFICATION,
        TaskEvent.REQUIRE_RECOVERY,
        TaskEvent.REQUIRE_CLEANUP,
    }
    if event in failure_events:
        if error is None:
            raise _failure(
                TaskStateErrorCode.MISSING_ERROR, "/error", "failure event requires StepError"
            )
        if type(error) is not StepError:
            raise _failure(TaskStateErrorCode.INVALID_TYPE, "/error", "expected StepError")
    elif error is not None:
        raise _failure(
            TaskStateErrorCode.INVALID_VALUE, "/error", "error is only accepted on failure events"
        )
    verification_events = {TaskEvent.PASS_VERIFICATION, TaskEvent.PREPARE_REVIEW}
    if event in verification_events:
        if type(verification) is not VerificationReport:
            raise _failure(
                TaskStateErrorCode.INVALID_TYPE,
                "/verification",
                "verification event requires VerificationReport",
            )
        if not verification.passed:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/verification/passed",
                "verification event requires a passing report",
            )
        if verification.candidate_revision != task.candidate_revision:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/verification/candidate_revision",
                "verification must bind current candidate",
            )
        if task.program is None or verification.acceptance_id != task.program.acceptance.id:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/verification/acceptance_id",
                "verification must bind submitted acceptance",
            )
    elif verification is not None:
        raise _failure(
            TaskStateErrorCode.INVALID_VALUE,
            "/verification",
            "verification is only accepted on a verification event",
        )
    if event is TaskEvent.PREPARE_REVIEW:
        if type(draft) is not ReviewDraft:
            raise _failure(
                TaskStateErrorCode.INVALID_TYPE,
                "/draft",
                "prepare_review requires ReviewDraft",
            )
    elif draft is not None:
        raise _failure(
            TaskStateErrorCode.INVALID_VALUE,
            "/draft",
            "draft is only accepted on prepare_review",
        )
    if event in {TaskEvent.COMMIT, TaskEvent.CONFIRM_COMMITTED}:
        committed_revision = _identifier(committed_revision, "revision", "/committed_revision")
        if committed_revision is None:
            raise _failure(
                TaskStateErrorCode.INVALID_IDENTIFIER,
                "/committed_revision",
                "committed revision is required",
            )
        if committed_revision != task.candidate_revision:
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/committed_revision",
                "committed revision must equal the verified candidate",
            )
        if not any(
            report.passed and report.candidate_revision == task.candidate_revision
            for report in task.verification_reports
        ):
            raise _failure(
                TaskStateErrorCode.INVARIANT_VIOLATION,
                "/verification_reports",
                "commit requires passing candidate verification",
            )
    elif committed_revision is not None:
        raise _failure(
            TaskStateErrorCode.INVALID_VALUE,
            "/committed_revision",
            "committed revision is only accepted on commit",
        )
    reports = task.verification_reports
    if verification is not None:
        reports = reports + (verification,)
    return replace(
        task,
        status=target,
        program=program if event is TaskEvent.SUBMIT_PROGRAM else task.program,
        candidate_revision=candidate_revision
        if event is TaskEvent.VALIDATE_PROGRAM
        else task.candidate_revision,
        committed_revision=committed_revision
        if event in {TaskEvent.COMMIT, TaskEvent.CONFIRM_COMMITTED}
        else task.committed_revision,
        draft=draft if event is TaskEvent.PREPARE_REVIEW else task.draft,
        verification_reports=reports,
        last_error=(
            None
            if event
            in {
                TaskEvent.SUBMIT_PROGRAM,
                TaskEvent.CONFIRM_PRE_CANDIDATE,
                TaskEvent.CONFIRM_DRAFT_UNCOMMITTED,
                TaskEvent.ABORT_ACCEPT,
                TaskEvent.REQUEST_CANCEL,
                TaskEvent.CONFIRM_CANCELLED,
            }
            else error
            if event in failure_events
            else task.last_error
        ),
        transitions=task.transitions
        + (
            TaskTransitionRecord(
                sequence=len(task.transitions) + 1,
                event=event,
                from_status=task.status,
                to_status=target,
            ),
        ),
    )


def append_step_result(task: TaskRun, result: StepResult) -> TaskRun:
    """Append one immutable adapter result while a candidate executes."""

    if type(task) is not TaskRun:
        raise _failure(TaskStateErrorCode.INVALID_TYPE, "/task", "expected TaskRun")
    if task.status is not TaskStatus.EXECUTING:
        raise _failure(
            TaskStateErrorCode.INVALID_TRANSITION,
            "/status",
            "step results require executing status",
        )
    if type(result) is not StepResult:
        raise _failure(TaskStateErrorCode.INVALID_TYPE, "/result", "expected StepResult")
    if result.revision != task.candidate_revision:
        raise _failure(
            TaskStateErrorCode.INVARIANT_VIOLATION,
            "/result/revision",
            "step result must bind current candidate",
        )
    if len(task.steps) >= MAX_STEP_RECORDS:
        raise _failure(TaskStateErrorCode.BUDGET_EXCEEDED, "/steps", "step history budget exceeded")
    return replace(
        task, steps=task.steps + (TaskStepRecord(sequence=len(task.steps) + 1, result=result),)
    )


def append_verification(task: TaskRun, report: VerificationReport) -> TaskRun:
    """Append one trusted report while the candidate is being verified."""

    if type(task) is not TaskRun:
        raise _failure(TaskStateErrorCode.INVALID_TYPE, "/task", "expected TaskRun")
    if task.status is not TaskStatus.VERIFYING:
        raise _failure(
            TaskStateErrorCode.INVALID_TRANSITION,
            "/status",
            "verification reports require verifying status",
        )
    if type(report) is not VerificationReport:
        raise _failure(TaskStateErrorCode.INVALID_TYPE, "/report", "expected VerificationReport")
    if report.candidate_revision != task.candidate_revision:
        raise _failure(
            TaskStateErrorCode.INVARIANT_VIOLATION,
            "/report/candidate_revision",
            "verification must bind current candidate",
        )
    if task.program is None or report.acceptance_id != task.program.acceptance.id:
        raise _failure(
            TaskStateErrorCode.INVARIANT_VIOLATION,
            "/report/acceptance_id",
            "verification must bind submitted acceptance",
        )
    if len(task.verification_reports) >= MAX_VERIFICATION_REPORTS:
        raise _failure(
            TaskStateErrorCode.BUDGET_EXCEEDED,
            "/verification_reports",
            "verification history budget exceeded",
        )
    if any(existing.id == report.id for existing in task.verification_reports):
        raise _failure(
            TaskStateErrorCode.DUPLICATE_IDENTIFIER,
            "/report/id",
            "verification identifier already exists",
        )
    return replace(task, verification_reports=task.verification_reports + (report,))


def append_artifact(task: TaskRun, artifact: TaskArtifactRef) -> TaskRun:
    """Attach one coordinator-owned, candidate-bound artifact reference."""

    if type(task) is not TaskRun:
        raise _failure(TaskStateErrorCode.INVALID_TYPE, "/task", "expected TaskRun")
    if task.status not in {TaskStatus.EXECUTING, TaskStatus.VERIFYING}:
        raise _failure(
            TaskStateErrorCode.INVALID_TRANSITION,
            "/status",
            "artifacts require candidate execution or verification",
        )
    if type(artifact) is not TaskArtifactRef:
        raise _failure(TaskStateErrorCode.INVALID_TYPE, "/artifact", "expected TaskArtifactRef")
    if artifact.candidate_revision != task.candidate_revision:
        raise _failure(
            TaskStateErrorCode.INVARIANT_VIOLATION,
            "/artifact/candidate_revision",
            "artifact must bind current candidate",
        )
    if len(task.artifacts) >= MAX_ARTIFACT_REFS:
        raise _failure(
            TaskStateErrorCode.BUDGET_EXCEEDED, "/artifacts", "artifact history budget exceeded"
        )
    if any(existing.id == artifact.id for existing in task.artifacts):
        raise _failure(
            TaskStateErrorCode.DUPLICATE_IDENTIFIER,
            "/artifact/id",
            "artifact identifier already exists",
        )
    return replace(task, artifacts=task.artifacts + (artifact,))


def next_action_for(status: TaskStatus) -> NextAction:
    """Map every durable status to one deterministic next action."""

    status = _enum(status, TaskStatus, "/status")
    assert type(status) is TaskStatus
    mapping = {
        TaskStatus.CREATED: NextAction.REQUEST_PLAN,
        TaskStatus.NEEDS_PLAN: NextAction.SUBMIT_PROGRAM,
        TaskStatus.PROGRAM_READY: NextAction.VALIDATE_PROGRAM,
        TaskStatus.VALIDATING_PROGRAM: NextAction.WAIT,
        TaskStatus.EXECUTING: NextAction.WAIT,
        TaskStatus.VERIFYING: NextAction.WAIT,
        TaskStatus.COMMITTING: NextAction.WAIT,
        TaskStatus.PREPARING_REVIEW: NextAction.RECONCILE,
        TaskStatus.AWAITING_USER_REVIEW: NextAction.REVIEW_DRAFT,
        TaskStatus.ACCEPTING_DRAFT: NextAction.RECONCILE,
        TaskStatus.ROLLING_BACK: NextAction.WAIT,
        TaskStatus.NEEDS_INPUT: NextAction.PROVIDE_INPUT,
        TaskStatus.RECOVERY_REQUIRED: NextAction.RECONCILE,
        TaskStatus.CLEANUP_REQUIRED: NextAction.CLEANUP,
        TaskStatus.SUCCEEDED: NextAction.NONE,
        TaskStatus.FAILED: NextAction.NONE,
        TaskStatus.REJECTED: NextAction.NONE,
        TaskStatus.CANCEL_REQUESTED: NextAction.RECONCILE,
        TaskStatus.CANCELLING: NextAction.RECONCILE,
        TaskStatus.CANCELLED: NextAction.NONE,
    }
    return mapping[status]


__all__ = [
    "MAX_ARTIFACT_REFS",
    "MAX_CRITERION_VERDICTS",
    "MAX_STEP_RECORDS",
    "MAX_TRANSITION_RECORDS",
    "MAX_VERIFICATION_REPORTS",
    "MAX_VERDICT_EVIDENCE",
    "CriterionOutcome",
    "CriterionVerdict",
    "NextAction",
    "ReasoningOwner",
    "ReviewDraft",
    "ReviewPolicy",
    "TaskArtifactRef",
    "TaskEvent",
    "TaskRun",
    "TaskStateError",
    "TaskStateErrorCode",
    "TaskStatus",
    "TaskStepRecord",
    "TaskTransitionRecord",
    "VerificationReport",
    "append_step_result",
    "append_artifact",
    "append_verification",
    "new_task_run",
    "next_action_for",
    "transition_task",
]
