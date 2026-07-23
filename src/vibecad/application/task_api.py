"""Bounded, transport-neutral public task API.

This module deliberately depends on immutable workflow contracts and a neutral
service port only.  It does not compose a CAD runtime, register MCP tools, or
import the concrete :mod:`vibecad.workflow.service` implementation.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from vibecad.execution.registry import (
    DEFAULT_OPERATION_REGISTRY,
    FieldMetadata,
    OperationMetadata,
    OperationRegistry,
    ResultSlotMetadata,
)
from vibecad.workflow.contracts import ModelProgram
from vibecad.workflow.errors import (
    MAX_SAFE_JSON_INTEGER,
    SCHEMA_VERSION,
    ContractValidationError,
    join_json_pointer,
)
from vibecad.workflow.state import (
    NextAction,
    ReasoningOwner,
    ReviewPolicy,
    TaskStatus,
    TaskTransitionRecord,
    task_creation_identity,
)
from vibecad.workflow.store import StoredTaskRun

_MAX_SMALL_REQUEST_BYTES = 4_096
_MAX_PROGRAM_JSON_BYTES = 512 * 1_024
_MAX_SUBMIT_LOGICAL_BYTES = _MAX_SMALL_REQUEST_BYTES + _MAX_PROGRAM_JSON_BYTES
_MAX_PROGRAM_JSON_DEPTH = 64
_MAX_PROGRAM_JSON_NODES = 8_192
_MAX_PROGRAM_JSON_STRING_BYTES = 64 * 1_024
_MAX_PROGRAM_JSON_KEY_BYTES = 256
_MAX_PUBLIC_ERROR_PATH_BYTES = 256
_MAX_OUTER_JSON_NODES = 8_192
_TASK_ID = re.compile(r"^task_[0-9a-f]{32}$")
_TASK_CREATE_KEY = re.compile(r"^task_create_[0-9a-f]{32}$")
_REVERT_CREATE_KEY = re.compile(r"^revert_create_[0-9a-f]{32}$")
_TASK_LIST_CURSOR = re.compile(r"^task_list_cursor_[0-9a-f]{64}$")
_TASK_EVENT_CURSOR = re.compile(r"^task_event_cursor_[0-9a-f]{64}$")
_PROJECT_ID = re.compile(r"^project_[0-9a-f]{32}$")
_REVISION_ID = re.compile(r"^revision_[0-9a-f]{32}$")
_DRAFT_ID = re.compile(r"^draft_[0-9a-f]{32}$")


class TaskApiErrorCode(StrEnum):
    """Closed public failure taxonomy for every task API method."""

    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    UNSUPPORTED_VERSION = "unsupported_version"
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"
    BUDGET_EXCEEDED = "budget_exceeded"
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_REASONING_OWNER = "unsupported_reasoning_owner"
    INVALID_STATE = "invalid_state"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    STORE_FAILURE = "store_failure"
    LEASE_UNAVAILABLE = "lease_unavailable"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    RECOVERY_REQUIRED = "recovery_required"
    INTERNAL_ERROR = "internal_error"


_ERROR_MESSAGES = {
    TaskApiErrorCode.MISSING_FIELD: "A required request field is missing.",
    TaskApiErrorCode.UNKNOWN_FIELD: "The request contains an unknown field.",
    TaskApiErrorCode.UNSUPPORTED_VERSION: "The request schema version is not supported.",
    TaskApiErrorCode.INVALID_TYPE: "A request value has an invalid type.",
    TaskApiErrorCode.INVALID_VALUE: "A request value is invalid.",
    TaskApiErrorCode.BUDGET_EXCEEDED: "The request exceeds a resource budget.",
    TaskApiErrorCode.INVALID_INPUT: "The request is invalid.",
    TaskApiErrorCode.UNSUPPORTED_REASONING_OWNER: (
        "The requested reasoning owner is not supported."
    ),
    TaskApiErrorCode.INVALID_STATE: "The task is not ready for this operation.",
    TaskApiErrorCode.NOT_FOUND: "The task record was not found.",
    TaskApiErrorCode.CONFLICT: "The task record changed concurrently.",
    TaskApiErrorCode.STORE_FAILURE: "The task record operation failed.",
    TaskApiErrorCode.LEASE_UNAVAILABLE: "The project write lease is unavailable.",
    TaskApiErrorCode.RESOURCE_EXHAUSTED: ("The application resource capacity is exhausted."),
    TaskApiErrorCode.RECOVERY_REQUIRED: "The task requires explicit reconciliation.",
    TaskApiErrorCode.INTERNAL_ERROR: "The request could not be completed.",
}


class TaskServicePortErrorCode(StrEnum):
    """Stable failures accepted from the neutral task-service port."""

    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_REASONING_OWNER = "unsupported_reasoning_owner"
    INVALID_STATE = "invalid_state"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    STORE_FAILURE = "store_failure"
    LEASE_UNAVAILABLE = "lease_unavailable"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    RECOVERY_REQUIRED = "recovery_required"


_PORT_ERROR_MAP = {
    TaskServicePortErrorCode.INVALID_INPUT: TaskApiErrorCode.INVALID_INPUT,
    TaskServicePortErrorCode.UNSUPPORTED_REASONING_OWNER: (
        TaskApiErrorCode.UNSUPPORTED_REASONING_OWNER
    ),
    TaskServicePortErrorCode.INVALID_STATE: TaskApiErrorCode.INVALID_STATE,
    TaskServicePortErrorCode.NOT_FOUND: TaskApiErrorCode.NOT_FOUND,
    TaskServicePortErrorCode.CONFLICT: TaskApiErrorCode.CONFLICT,
    TaskServicePortErrorCode.STORE_FAILURE: TaskApiErrorCode.STORE_FAILURE,
    TaskServicePortErrorCode.LEASE_UNAVAILABLE: TaskApiErrorCode.LEASE_UNAVAILABLE,
    TaskServicePortErrorCode.RESOURCE_EXHAUSTED: TaskApiErrorCode.RESOURCE_EXHAUSTED,
    TaskServicePortErrorCode.RECOVERY_REQUIRED: TaskApiErrorCode.RECOVERY_REQUIRED,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskServicePortFailure:
    """Path-free failure value returned by a conforming service bridge."""

    code: TaskServicePortErrorCode

    def __post_init__(self) -> None:
        if type(self.code) is not TaskServicePortErrorCode:
            raise TypeError("code must be an exact TaskServicePortErrorCode")


class TaskServicePort(Protocol):
    """Transport-neutral subset of the deterministic task service."""

    def revert_project(
        self,
        *,
        revert_key: str,
        project_id: str,
        source_revision: str,
        expected_head: str,
    ) -> StoredTaskRun | TaskServicePortFailure: ...

    def create_task(
        self,
        *,
        create_key: str,
        project_id: str,
        reasoning_owner: ReasoningOwner,
        review_policy: ReviewPolicy,
    ) -> StoredTaskRun | TaskServicePortFailure: ...

    def get_task(self, *, task_id: str) -> StoredTaskRun | TaskServicePortFailure: ...

    def list_tasks(
        self, *, limit: int, cursor: str | None
    ) -> dict[str, object] | TaskServicePortFailure: ...

    def get_task_events(
        self, *, task_id: str, limit: int, cursor: str | None
    ) -> dict[str, object] | TaskServicePortFailure: ...

    def submit_model_program(
        self, *, task_id: str, expected_generation: int, program: ModelProgram
    ) -> StoredTaskRun | TaskServicePortFailure: ...

    def continue_task(
        self, *, task_id: str, expected_generation: int
    ) -> StoredTaskRun | TaskServicePortFailure: ...

    def reconcile_task(
        self, *, task_id: str, expected_generation: int
    ) -> StoredTaskRun | TaskServicePortFailure: ...

    def cancel_task(
        self, *, task_id: str, expected_generation: int
    ) -> StoredTaskRun | TaskServicePortFailure: ...

    def accept_draft(
        self, *, task_id: str, draft_id: str, expected_generation: int
    ) -> StoredTaskRun | TaskServicePortFailure: ...

    def reject_draft(
        self, *, task_id: str, draft_id: str, expected_generation: int
    ) -> StoredTaskRun | TaskServicePortFailure: ...


class _ApiFailure(Exception):
    __slots__ = ("code", "path")

    def __init__(self, code: TaskApiErrorCode, path: str = "") -> None:
        self.code = code
        self.path = path
        super().__init__(code.value)


class _JsonIngressFailure(Exception):
    __slots__ = ("code",)

    def __init__(self, code: TaskApiErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


def _raise(code: TaskApiErrorCode, path: str = "") -> None:
    raise _ApiFailure(code, path)


def _bounded_pointer(parent: str, token: str) -> str:
    if len(parent) + len(token) + 1 > _MAX_PUBLIC_ERROR_PATH_BYTES:
        return "/_truncated"
    candidate = join_json_pointer(parent, token)
    try:
        if len(candidate.encode("utf-8")) <= _MAX_PUBLIC_ERROR_PATH_BYTES:
            return candidate
    except UnicodeEncodeError:
        pass
    return "/_truncated"


def _bounded_contract_path(path: str) -> str:
    if len(path) + len("/program_json") > _MAX_PUBLIC_ERROR_PATH_BYTES:
        return "/program_json/_truncated"
    candidate = f"/program_json{path}"
    try:
        if len(candidate.encode("utf-8")) <= _MAX_PUBLIC_ERROR_PATH_BYTES:
            return candidate
    except UnicodeEncodeError:
        pass
    return "/program_json/_truncated"


def _utf8_length(value: str, path: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        _raise(TaskApiErrorCode.INVALID_VALUE, path)


def _validate_exact_json(value: object, *, program_json_path: str | None) -> None:
    count = 0
    seen: set[int] = set()
    stack: list[tuple[object, str, int]] = [(value, "", 0)]
    while stack:
        current, path, depth = stack.pop()
        count += 1
        if count > _MAX_OUTER_JSON_NODES:
            _raise(TaskApiErrorCode.BUDGET_EXCEEDED, path)

        if current is None or type(current) is bool:
            continue
        if type(current) is int:
            if abs(current) > MAX_SAFE_JSON_INTEGER:
                _raise(TaskApiErrorCode.INVALID_VALUE, path)
            continue
        if type(current) is float:
            if not math.isfinite(current):
                _raise(TaskApiErrorCode.INVALID_VALUE, path)
            continue
        if type(current) is str:
            maximum = (
                _MAX_PROGRAM_JSON_BYTES
                if program_json_path is not None and path == program_json_path
                else _MAX_SMALL_REQUEST_BYTES
            )
            if len(current) > maximum:
                _raise(TaskApiErrorCode.BUDGET_EXCEEDED, path)
            if _utf8_length(current, path) > maximum:
                _raise(TaskApiErrorCode.BUDGET_EXCEEDED, path)
            continue
        if type(current) not in {dict, list}:
            _raise(TaskApiErrorCode.INVALID_TYPE, path)
        if depth >= _MAX_PROGRAM_JSON_DEPTH:
            _raise(TaskApiErrorCode.BUDGET_EXCEEDED, path)

        identity = id(current)
        if identity in seen:
            _raise(TaskApiErrorCode.INVALID_VALUE, path)
        seen.add(identity)

        if type(current) is list:
            for index in range(len(current) - 1, -1, -1):
                stack.append((current[index], _bounded_pointer(path, str(index)), depth + 1))
            continue

        assert type(current) is dict
        items = tuple(current.items())
        for key, item in reversed(items):
            if type(key) is not str:
                _raise(TaskApiErrorCode.INVALID_TYPE, path)
            if len(key) > _MAX_SMALL_REQUEST_BYTES:
                _raise(TaskApiErrorCode.BUDGET_EXCEEDED, path)
            if _utf8_length(key, path) > _MAX_SMALL_REQUEST_BYTES:
                _raise(TaskApiErrorCode.BUDGET_EXCEEDED, path)
            stack.append((item, _bounded_pointer(path, key), depth + 1))


def _canonical_json_size(value: object, *, maximum: int) -> int:
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
            if total > maximum:
                return total
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _raise(TaskApiErrorCode.INVALID_VALUE)
    return total


def _validate_request(
    request: object,
    *,
    required: frozenset[str],
    allowed: frozenset[str] | None = None,
    submit: bool = False,
) -> dict[str, object]:
    if type(request) is not dict:
        _raise(TaskApiErrorCode.INVALID_TYPE)
    assert type(request) is dict
    if len(request) > _MAX_SMALL_REQUEST_BYTES:
        _raise(TaskApiErrorCode.BUDGET_EXCEEDED)

    keys = tuple(request)
    if not all(type(key) is str for key in keys):
        _raise(TaskApiErrorCode.INVALID_TYPE)
    accepted = required if allowed is None else allowed
    unknown = sorted(set(keys) - accepted)
    if unknown:
        _raise(TaskApiErrorCode.UNKNOWN_FIELD, _bounded_pointer("", unknown[0]))
    missing = sorted(required - set(keys))
    if missing:
        _raise(TaskApiErrorCode.MISSING_FIELD, _bounded_pointer("", missing[0]))

    for key, value in request.items():
        if type(value) not in {type(None), bool, int, float, str}:
            _raise(TaskApiErrorCode.INVALID_TYPE, _bounded_pointer("", key))
    if submit:
        raw = request["program_json"]
        if type(raw) is str and len(raw) > _MAX_PROGRAM_JSON_BYTES:
            _raise(TaskApiErrorCode.BUDGET_EXCEEDED, "/program_json")
    _validate_exact_json(
        request,
        program_json_path="/program_json" if submit else None,
    )

    budget_value: object = request
    if submit:
        budget_value = {key: value for key, value in request.items() if key != "program_json"}
    if _canonical_json_size(budget_value, maximum=_MAX_SMALL_REQUEST_BYTES) > (
        _MAX_SMALL_REQUEST_BYTES
    ):
        _raise(TaskApiErrorCode.BUDGET_EXCEEDED)

    version = request["schema_version"]
    if type(version) is not int:
        _raise(TaskApiErrorCode.INVALID_TYPE, "/schema_version")
    if abs(version) > MAX_SAFE_JSON_INTEGER:
        _raise(TaskApiErrorCode.INVALID_VALUE, "/schema_version")
    if version != SCHEMA_VERSION:
        _raise(TaskApiErrorCode.UNSUPPORTED_VERSION, "/schema_version")
    return request


def _identifier(value: object, path: str, pattern: re.Pattern[str]) -> str:
    if type(value) is not str:
        _raise(TaskApiErrorCode.INVALID_TYPE, path)
    assert type(value) is str
    if pattern.fullmatch(value) is None:
        _raise(TaskApiErrorCode.INVALID_VALUE, path)
    return value


def _generation(value: object) -> int:
    if type(value) is not int:
        _raise(TaskApiErrorCode.INVALID_TYPE, "/expected_generation")
    if value < 0 or value > MAX_SAFE_JSON_INTEGER:
        _raise(TaskApiErrorCode.INVALID_VALUE, "/expected_generation")
    return value


def _page_limit(value: object) -> int:
    if type(value) is not int:
        _raise(TaskApiErrorCode.INVALID_TYPE, "/limit")
    if value < 1 or value > 100:
        _raise(TaskApiErrorCode.INVALID_VALUE, "/limit")
    return value


def _page_cursor(value: object, pattern: re.Pattern[str]) -> str | None:
    if value is None:
        return None
    return _identifier(value, "/cursor", pattern)


def _review_policy(value: object) -> ReviewPolicy:
    if type(value) is not str:
        _raise(TaskApiErrorCode.INVALID_TYPE, "/review_policy")
    try:
        return ReviewPolicy(value)
    except ValueError:
        _raise(TaskApiErrorCode.INVALID_VALUE, "/review_policy")


def _json_depth_within_budget(raw: str) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for character in raw:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_PROGRAM_JSON_DEPTH:
                return False
        elif character in "]}":
            depth -= 1
    return True


def _duplicate_checked_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _JsonIngressFailure(TaskApiErrorCode.INVALID_INPUT)
        result[key] = value
    return result


def _parse_json_integer(token: str) -> int:
    digits = token[1:] if token.startswith("-") else token
    if len(digits) > 16:
        raise _JsonIngressFailure(TaskApiErrorCode.INVALID_VALUE)
    result = int(token)
    if abs(result) > MAX_SAFE_JSON_INTEGER:
        raise _JsonIngressFailure(TaskApiErrorCode.INVALID_VALUE)
    return result


def _parse_json_float(token: str) -> float:
    if len(token) > 64:
        raise _JsonIngressFailure(TaskApiErrorCode.BUDGET_EXCEEDED)
    result = float(token)
    if not math.isfinite(result):
        raise _JsonIngressFailure(TaskApiErrorCode.INVALID_VALUE)
    return result


def _reject_json_constant(_token: str) -> None:
    raise _JsonIngressFailure(TaskApiErrorCode.INVALID_VALUE)


def _validate_program_json_resources(value: object) -> None:
    count = 0
    stack = [value]
    while stack:
        current = stack.pop()
        count += 1
        if count > _MAX_PROGRAM_JSON_NODES:
            _raise(TaskApiErrorCode.BUDGET_EXCEEDED, "/program_json")
        if type(current) is str:
            if len(current) > _MAX_PROGRAM_JSON_STRING_BYTES:
                _raise(TaskApiErrorCode.BUDGET_EXCEEDED, "/program_json")
            if _utf8_length(current, "/program_json") > _MAX_PROGRAM_JSON_STRING_BYTES:
                _raise(TaskApiErrorCode.BUDGET_EXCEEDED, "/program_json")
        elif type(current) is list:
            stack.extend(current)
        elif type(current) is dict:
            for key in current:
                if len(key) > _MAX_PROGRAM_JSON_KEY_BYTES:
                    _raise(TaskApiErrorCode.BUDGET_EXCEEDED, "/program_json")
                if _utf8_length(key, "/program_json") > _MAX_PROGRAM_JSON_KEY_BYTES:
                    _raise(TaskApiErrorCode.BUDGET_EXCEEDED, "/program_json")
            stack.extend(current.values())
            stack.extend(current.keys())
        elif current is None or type(current) in {bool, int}:
            continue
        elif type(current) is float:
            if not math.isfinite(current):
                _raise(TaskApiErrorCode.INVALID_VALUE, "/program_json")
        else:
            _raise(TaskApiErrorCode.INVALID_TYPE, "/program_json")


def _decode_model_program(raw: object, *, metadata_bytes: int, task_id: str) -> ModelProgram:
    if type(raw) is not str:
        _raise(TaskApiErrorCode.INVALID_TYPE, "/program_json")
    assert type(raw) is str
    if len(raw) > _MAX_PROGRAM_JSON_BYTES:
        _raise(TaskApiErrorCode.BUDGET_EXCEEDED, "/program_json")
    raw_bytes = _utf8_length(raw, "/program_json")
    if metadata_bytes + raw_bytes > _MAX_SUBMIT_LOGICAL_BYTES:
        _raise(TaskApiErrorCode.BUDGET_EXCEEDED, "/program_json")
    if raw_bytes > _MAX_PROGRAM_JSON_BYTES:
        _raise(TaskApiErrorCode.BUDGET_EXCEEDED, "/program_json")
    if not _json_depth_within_budget(raw):
        _raise(TaskApiErrorCode.BUDGET_EXCEEDED, "/program_json")

    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=_duplicate_checked_object,
            parse_int=_parse_json_integer,
            parse_float=_parse_json_float,
            parse_constant=_reject_json_constant,
        )
    except _JsonIngressFailure as error:
        _raise(error.code, "/program_json")
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError):
        _raise(TaskApiErrorCode.INVALID_INPUT, "/program_json")
    _validate_program_json_resources(decoded)

    try:
        program = ModelProgram.from_mapping(decoded)
    except ContractValidationError as error:
        try:
            code = TaskApiErrorCode(error.code.value)
        except ValueError:
            _raise(TaskApiErrorCode.INVALID_INPUT, "/program_json")
        _raise(code, _bounded_contract_path(error.path))
    except BaseException:
        _raise(TaskApiErrorCode.INTERNAL_ERROR)
    if program.task_id != task_id:
        _raise(TaskApiErrorCode.INVALID_INPUT, "/program_json/task_id")
    return program


def _field_projection(field: FieldMetadata) -> dict[str, object]:
    return {
        "name": field.name,
        "value_shape": field.value_shape.value,
        "required": field.required,
        "enum_values": sorted(field.enum_values),
        "allowed_units": sorted(field.allowed_units),
        "referenced_value_shape": (
            None if field.referenced_value_shape is None else field.referenced_value_shape.value
        ),
    }


def _result_slot_projection(slot: ResultSlotMetadata) -> dict[str, object]:
    return {
        "name": slot.name,
        "value_shape": slot.value_shape.value,
        "enum_values": sorted(slot.enum_values),
        "allowed_units": sorted(slot.allowed_units),
    }


def _operation_projection(metadata: OperationMetadata) -> dict[str, object]:
    return {
        "operation": metadata.operation,
        "risk_class": metadata.risk_class.value,
        "evidence_required": metadata.evidence_required,
        "target_fields": [
            _field_projection(field)
            for field in sorted(metadata.target_fields, key=lambda x: x.name)
        ],
        "argument_fields": [
            _field_projection(field)
            for field in sorted(metadata.argument_fields, key=lambda x: x.name)
        ],
        "execution_profiles": sorted(profile.value for profile in metadata.execution_profiles),
        "minimum_freecad_version": list(metadata.minimum_freecad_version),
        "maximum_freecad_version_exclusive": list(metadata.maximum_freecad_version_exclusive),
        "requires_gui_main_thread": metadata.requires_gui_main_thread,
        "resource_budget": {
            "max_runtime_ms": metadata.resource_budget.max_runtime_ms,
            "max_created_objects": metadata.resource_budget.max_created_objects,
            "max_result_bytes": metadata.resource_budget.max_result_bytes,
        },
        "direct_exposed": metadata.direct_exposed,
        "result_slots": [
            _result_slot_projection(slot)
            for slot in sorted(metadata.result_slots, key=lambda x: x.name)
        ],
        "preservation_fields": sorted(metadata.preservation_fields),
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


class TaskApi:
    """Strict public adapter over an injected deterministic task-service port."""

    __slots__ = ("_port", "_registry")

    def __init__(
        self,
        *,
        port: TaskServicePort,
        registry: OperationRegistry = DEFAULT_OPERATION_REGISTRY,
    ) -> None:
        if type(registry) is not OperationRegistry:
            raise TypeError("registry must be an exact OperationRegistry")
        self._port = port
        self._registry = registry

    @staticmethod
    def _guard(action: Callable[[], dict[str, object]]) -> dict[str, object]:
        try:
            return _success(action())
        except _ApiFailure as error:
            return _failure(error)
        except BaseException:
            return _failure(_ApiFailure(TaskApiErrorCode.INTERNAL_ERROR))

    @staticmethod
    def _task_result(stored: StoredTaskRun, *, task_id: str) -> dict[str, object]:
        if type(stored) is not StoredTaskRun or stored.task_run.id != task_id:
            _raise(TaskApiErrorCode.INTERNAL_ERROR)
        return {
            "generation": stored.generation,
            "next_action": stored.task_run.next_action.value,
            "task_run": stored.task_run.to_mapping(),
        }

    @staticmethod
    def _invoke_untrusted(action: Callable[[], object]) -> object:
        try:
            return action()
        except BaseException:
            _raise(TaskApiErrorCode.INTERNAL_ERROR)

    @staticmethod
    def _port_result(value: object, *, task_id: str) -> StoredTaskRun:
        if type(value) is TaskServicePortFailure:
            try:
                port_code = value.code
            except BaseException:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            if type(port_code) is not TaskServicePortErrorCode:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            _raise(_PORT_ERROR_MAP[port_code])
        if type(value) is not StoredTaskRun or value.task_run.id != task_id:
            _raise(TaskApiErrorCode.INTERNAL_ERROR)
        return value

    @staticmethod
    def _mapping_port_result(value: object) -> dict[str, object]:
        if type(value) is TaskServicePortFailure:
            try:
                port_code = value.code
            except BaseException:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            if type(port_code) is not TaskServicePortErrorCode:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            _raise(_PORT_ERROR_MAP[port_code])
        if type(value) is not dict:
            _raise(TaskApiErrorCode.INTERNAL_ERROR)
        return dict(value)

    @staticmethod
    def _validated_cursor(value: object, pattern: re.Pattern[str]) -> str | None:
        if value is None:
            return None
        if type(value) is not str or pattern.fullmatch(value) is None:
            _raise(TaskApiErrorCode.INTERNAL_ERROR)
        return value

    @staticmethod
    def _validated_summary(value: object) -> dict[str, object]:
        keys = {
            "task_id",
            "project_id",
            "generation",
            "base_revision",
            "reasoning_owner",
            "review_policy",
            "status",
            "next_action",
            "candidate_revision",
            "committed_revision",
            "draft_id",
        }
        if type(value) is not dict or set(value) != keys:
            _raise(TaskApiErrorCode.INTERNAL_ERROR)
        if (
            type(value["task_id"]) is not str
            or _TASK_ID.fullmatch(value["task_id"]) is None
            or type(value["project_id"]) is not str
            or _PROJECT_ID.fullmatch(value["project_id"]) is None
            or type(value["base_revision"]) is not str
            or re.fullmatch(r"revision_[0-9a-f]{32}", value["base_revision"]) is None
            or type(value["generation"]) is not int
            or value["generation"] < 0
            or value["generation"] > MAX_SAFE_JSON_INTEGER
        ):
            _raise(TaskApiErrorCode.INTERNAL_ERROR)
        enum_fields = (
            ("reasoning_owner", ReasoningOwner),
            ("review_policy", ReviewPolicy),
            ("status", TaskStatus),
            ("next_action", NextAction),
        )
        for field, enum_type in enum_fields:
            if type(value[field]) is not str:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            try:
                enum_type(value[field])
            except (TypeError, ValueError):
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
        for field, pattern in (
            ("candidate_revision", r"revision_[0-9a-f]{32}"),
            ("committed_revision", r"revision_[0-9a-f]{32}"),
            ("draft_id", r"draft_[0-9a-f]{32}"),
        ):
            selected = value[field]
            if selected is not None and (
                type(selected) is not str or re.fullmatch(pattern, selected) is None
            ):
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
        return value

    def create_task(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _validate_request(
                request,
                required=frozenset({"schema_version", "create_key", "project_id", "review_policy"}),
            )
            create_key = _identifier(data["create_key"], "/create_key", _TASK_CREATE_KEY)
            project_id = _identifier(data["project_id"], "/project_id", _PROJECT_ID)
            review_policy = _review_policy(data["review_policy"])
            generated, creation_digest = task_creation_identity(create_key)
            stored = self._port_result(
                self._invoke_untrusted(
                    lambda: self._port.create_task(
                        create_key=create_key,
                        project_id=project_id,
                        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
                        review_policy=review_policy,
                    )
                ),
                task_id=generated,
            )
            task = stored.task_run
            if not (
                task.project_id == project_id
                and task.reasoning_owner is ReasoningOwner.EXTERNAL_PLAN
                and task.review_policy is review_policy
                and task.creation_digest == creation_digest
                and task.status is not TaskStatus.CREATED
            ):
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            return self._task_result(stored, task_id=generated)

        return self._guard(action)

    def revert_project(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            from vibecad.workflow.revert import (
                RevertProgramError,
                require_matching_revert_task,
                revert_task_identity,
            )

            data = _validate_request(
                request,
                required=frozenset(
                    {
                        "schema_version",
                        "revert_key",
                        "project_id",
                        "source_revision",
                        "expected_head",
                    }
                ),
            )
            revert_key = _identifier(
                data["revert_key"],
                "/revert_key",
                _REVERT_CREATE_KEY,
            )
            project_id = _identifier(data["project_id"], "/project_id", _PROJECT_ID)
            source_revision = _identifier(
                data["source_revision"],
                "/source_revision",
                _REVISION_ID,
            )
            expected_head = _identifier(
                data["expected_head"],
                "/expected_head",
                _REVISION_ID,
            )
            try:
                expected_task_id, _creation_digest = revert_task_identity(revert_key)
            except RevertProgramError:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            value = self._invoke_untrusted(
                lambda: self._port.revert_project(
                    revert_key=revert_key,
                    project_id=project_id,
                    source_revision=source_revision,
                    expected_head=expected_head,
                )
            )
            stored = self._port_result(value, task_id=expected_task_id)
            try:
                require_matching_revert_task(
                    stored,
                    revert_key=revert_key,
                    project_id=project_id,
                    source_revision=source_revision,
                    expected_head=expected_head,
                )
            except RevertProgramError:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            return self._task_result(stored, task_id=expected_task_id)

        return self._guard(action)

    def accept_draft(self, request: object) -> dict[str, object]:
        return self._review_decision(request, accept=True)

    def reject_draft(self, request: object) -> dict[str, object]:
        return self._review_decision(request, accept=False)

    def _review_decision(self, request: object, *, accept: bool) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _validate_request(
                request,
                required=frozenset(
                    {"schema_version", "task_id", "draft_id", "expected_generation"}
                ),
            )
            task_id = _identifier(data["task_id"], "/task_id", _TASK_ID)
            draft_id = _identifier(data["draft_id"], "/draft_id", _DRAFT_ID)
            expected_generation = _generation(data["expected_generation"])
            if accept:
                port_value = self._invoke_untrusted(
                    lambda: self._port.accept_draft(
                        task_id=task_id,
                        draft_id=draft_id,
                        expected_generation=expected_generation,
                    )
                )
            else:
                port_value = self._invoke_untrusted(
                    lambda: self._port.reject_draft(
                        task_id=task_id,
                        draft_id=draft_id,
                        expected_generation=expected_generation,
                    )
                )
            stored = self._port_result(
                port_value,
                task_id=task_id,
            )
            task = stored.task_run
            draft = task.draft
            if not (
                task.review_policy is ReviewPolicy.REQUIRE_REVIEW
                and draft is not None
                and draft.id == draft_id
            ):
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            if accept:
                if not (
                    task.status is TaskStatus.SUCCEEDED
                    and task.committed_revision == draft.revision_id
                ):
                    _raise(TaskApiErrorCode.INTERNAL_ERROR)
            elif not (task.status is TaskStatus.REJECTED and task.committed_revision is None):
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            return self._task_result(stored, task_id=task_id)

        return self._guard(action)

    def get_task(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _validate_request(
                request,
                required=frozenset({"schema_version", "task_id"}),
            )
            task_id = _identifier(data["task_id"], "/task_id", _TASK_ID)
            stored = self._port_result(
                self._invoke_untrusted(lambda: self._port.get_task(task_id=task_id)),
                task_id=task_id,
            )
            return self._task_result(stored, task_id=task_id)

        return self._guard(action)

    def list_tasks(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _validate_request(
                request,
                required=frozenset({"schema_version"}),
                allowed=frozenset({"schema_version", "limit", "cursor"}),
            )
            limit = _page_limit(data.get("limit", 50))
            cursor = _page_cursor(data.get("cursor"), _TASK_LIST_CURSOR)
            result = self._mapping_port_result(
                self._invoke_untrusted(lambda: self._port.list_tasks(limit=limit, cursor=cursor))
            )
            if set(result) != {"tasks", "next_cursor"} or type(result["tasks"]) is not list:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            if len(result["tasks"]) > limit:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            tasks = [dict(self._validated_summary(item)) for item in result["tasks"]]
            ids = [item["task_id"] for item in tasks]
            if ids != sorted(ids) or len(ids) != len(set(ids)):
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            next_cursor = self._validated_cursor(result["next_cursor"], _TASK_LIST_CURSOR)
            if len(tasks) < limit and next_cursor is not None:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            return {
                "tasks": tasks,
                "next_cursor": next_cursor,
            }

        return self._guard(action)

    def get_task_events(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _validate_request(
                request,
                required=frozenset({"schema_version", "task_id"}),
                allowed=frozenset({"schema_version", "task_id", "limit", "cursor"}),
            )
            task_id = _identifier(data["task_id"], "/task_id", _TASK_ID)
            limit = _page_limit(data.get("limit", 50))
            cursor = _page_cursor(data.get("cursor"), _TASK_EVENT_CURSOR)
            result = self._mapping_port_result(
                self._invoke_untrusted(
                    lambda: self._port.get_task_events(
                        task_id=task_id,
                        limit=limit,
                        cursor=cursor,
                    )
                )
            )
            if set(result) != {
                "task_id",
                "generation",
                "transitions",
                "next_cursor",
            }:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            generation = result["generation"]
            if (
                type(result["task_id"]) is not str
                or result["task_id"] != task_id
                or type(generation) is not int
                or generation < 0
                or generation > MAX_SAFE_JSON_INTEGER
                or type(result["transitions"]) is not list
                or len(result["transitions"]) > limit
            ):
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            transitions: list[dict[str, object]] = []
            for value in result["transitions"]:
                try:
                    record = TaskTransitionRecord.from_mapping(value)
                except BaseException:
                    _raise(TaskApiErrorCode.INTERNAL_ERROR)
                if record.to_mapping() != value:
                    _raise(TaskApiErrorCode.INTERNAL_ERROR)
                transitions.append(dict(value))
            sequences = [item["sequence"] for item in transitions]
            if any(
                current != prior + 1
                for prior, current in zip(sequences, sequences[1:], strict=False)
            ) or (cursor is None and sequences and sequences[0] != 1):
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            next_cursor = self._validated_cursor(result["next_cursor"], _TASK_EVENT_CURSOR)
            if len(transitions) < limit and next_cursor is not None:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            return {
                "task_id": task_id,
                "generation": generation,
                "transitions": transitions,
                "next_cursor": next_cursor,
            }

        return self._guard(action)

    def submit_model_program(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _validate_request(
                request,
                required=frozenset(
                    {"schema_version", "task_id", "expected_generation", "program_json"}
                ),
                submit=True,
            )
            task_id = _identifier(data["task_id"], "/task_id", _TASK_ID)
            expected_generation = _generation(data["expected_generation"])
            metadata = {key: value for key, value in data.items() if key != "program_json"}
            program = _decode_model_program(
                data["program_json"],
                metadata_bytes=_canonical_json_size(
                    metadata,
                    maximum=_MAX_SMALL_REQUEST_BYTES,
                ),
                task_id=task_id,
            )
            stored = self._port_result(
                self._invoke_untrusted(
                    lambda: self._port.submit_model_program(
                        task_id=task_id,
                        expected_generation=expected_generation,
                        program=program,
                    )
                ),
                task_id=task_id,
            )
            return self._task_result(stored, task_id=task_id)

        return self._guard(action)

    def resume_task(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _validate_request(
                request,
                required=frozenset({"schema_version", "task_id", "expected_generation"}),
            )
            task_id = _identifier(data["task_id"], "/task_id", _TASK_ID)
            expected_generation = _generation(data["expected_generation"])
            stored = self._port_result(
                self._invoke_untrusted(lambda: self._port.get_task(task_id=task_id)),
                task_id=task_id,
            )
            if stored.generation != expected_generation:
                _raise(TaskApiErrorCode.CONFLICT)

            status = stored.task_run.status
            if status is TaskStatus.PROGRAM_READY:
                stored = self._port_result(
                    self._invoke_untrusted(
                        lambda: self._port.continue_task(
                            task_id=task_id,
                            expected_generation=expected_generation,
                        )
                    ),
                    task_id=task_id,
                )
            elif status in {
                TaskStatus.VALIDATING_PROGRAM,
                TaskStatus.EXECUTING,
                TaskStatus.VERIFYING,
                TaskStatus.COMMITTING,
                TaskStatus.ROLLING_BACK,
                TaskStatus.PREPARING_REVIEW,
                TaskStatus.ACCEPTING_DRAFT,
                TaskStatus.RECOVERY_REQUIRED,
                TaskStatus.CLEANUP_REQUIRED,
            }:
                stored = self._port_result(
                    self._invoke_untrusted(
                        lambda: self._port.reconcile_task(
                            task_id=task_id,
                            expected_generation=expected_generation,
                        )
                    ),
                    task_id=task_id,
                )
            elif status in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.REJECTED,
                TaskStatus.CANCELLED,
            }:
                pass
            elif status in {
                TaskStatus.CREATED,
                TaskStatus.NEEDS_PLAN,
                TaskStatus.NEEDS_INPUT,
                TaskStatus.AWAITING_USER_REVIEW,
                TaskStatus.CANCEL_REQUESTED,
                TaskStatus.CANCELLING,
            }:
                _raise(TaskApiErrorCode.INVALID_STATE)
            else:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            return self._task_result(stored, task_id=task_id)

        return self._guard(action)

    def cancel_task(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _validate_request(
                request,
                required=frozenset({"schema_version", "task_id", "expected_generation"}),
            )
            task_id = _identifier(data["task_id"], "/task_id", _TASK_ID)
            expected_generation = _generation(data["expected_generation"])
            stored = self._port_result(
                self._invoke_untrusted(
                    lambda: self._port.cancel_task(
                        task_id=task_id,
                        expected_generation=expected_generation,
                    )
                ),
                task_id=task_id,
            )
            if stored.generation < expected_generation:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            if stored.task_run.status not in {
                TaskStatus.CANCEL_REQUESTED,
                TaskStatus.CANCELLING,
                TaskStatus.CANCELLED,
            }:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            return self._task_result(stored, task_id=task_id)

        return self._guard(action)

    def get_capabilities(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            _validate_request(
                request,
                required=frozenset({"schema_version"}),
            )
            operations = [
                _operation_projection(self._registry.operations[name])
                for name in sorted(self._registry.operations)
            ]
            return {
                "registry_schema_version": SCHEMA_VERSION,
                "operations": operations,
            }

        return self._guard(action)


__all__ = [
    "TaskApi",
    "TaskApiErrorCode",
    "TaskServicePort",
    "TaskServicePortErrorCode",
    "TaskServicePortFailure",
]
