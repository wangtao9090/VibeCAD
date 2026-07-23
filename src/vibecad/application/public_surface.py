"""Deterministic public Agent tool metadata and direct-operation adapter.

The direct adapter is deliberately transport neutral.  It validates one
registry-derived request, binds it to one durable task snapshot, constructs one
declarative command, runs the pure validators, and invokes the existing task
service port exactly once.  MCP registration remains a separate boundary.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from vibecad.application.task_api import (
    TaskApi,
    TaskApiErrorCode,
    TaskServicePortErrorCode,
    TaskServicePortFailure,
)
from vibecad.execution.registry import (
    DEFAULT_OPERATION_REGISTRY,
    ExecutionProfile,
    FieldMetadata,
    OperationMetadata,
    OperationRegistry,
    ResourceBudget,
    ResultSlotMetadata,
    RiskClass,
    ValueShape,
)
from vibecad.execution.selectors import (
    EntityKind,
    ProvenanceSource,
    SelectorError,
    SelectorV1,
    SemanticRole,
)
from vibecad.validation import ValidationError, compile_acceptance_spec
from vibecad.workflow.contracts import (
    AcceptanceKind,
    AcceptanceSpec,
    ErrorCategory,
    EvidenceKind,
    ModelCommand,
    ModelProgram,
    ValueSource,
)
from vibecad.workflow.errors import (
    MAX_SAFE_JSON_INTEGER,
    SCHEMA_VERSION,
    ContractValidationError,
)
from vibecad.workflow.program import ProgramValidationError, validate_model_program
from vibecad.workflow.state import (
    MAX_ARTIFACT_REFS,
    MAX_CRITERION_VERDICTS,
    MAX_STEP_RECORDS,
    MAX_TRANSITION_RECORDS,
    MAX_VERDICT_EVIDENCE,
    MAX_VERIFICATION_REPORTS,
    CriterionOutcome,
    NextAction,
    ReasoningOwner,
    ReviewPolicy,
    TaskEvent,
    TaskStatus,
)
from vibecad.workflow.store import StoredTaskRun

_TASK_ID = re.compile(r"^task_[0-9a-f]{32}$")
_OBJECT_ID = re.compile(r"^object_[0-9a-f]{32}$")
_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "expected_generation",
        "target",
        "arguments",
        "preserve",
        "acceptance_json",
    }
)
_MAX_NON_ACCEPTANCE_BYTES = 4_096
_MAX_ACCEPTANCE_BYTES = 262_144
_MAX_LOGICAL_REQUEST_BYTES = _MAX_NON_ACCEPTANCE_BYTES + _MAX_ACCEPTANCE_BYTES
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 8_192
_MAX_JSON_STRING_BYTES = 65_536
_MAX_JSON_KEY_BYTES = 256
_MAX_ACCEPTANCE_CRITERIA = 128
_MAX_ERROR_PATH_BYTES = 256

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
    TaskApiErrorCode.RESOURCE_EXHAUSTED: "The application resource capacity is exhausted.",
    TaskApiErrorCode.RECOVERY_REQUIRED: "The task requires explicit reconciliation.",
    TaskApiErrorCode.INTERNAL_ERROR: "The request could not be completed.",
}
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


class _DirectFailure(Exception):
    __slots__ = ("code", "path")

    def __init__(self, code: TaskApiErrorCode, path: str = "") -> None:
        self.code = code
        self.path = path
        super().__init__(code.value)


class _JsonFailure(Exception):
    __slots__ = ("code",)

    def __init__(self, code: TaskApiErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


def _raise(code: TaskApiErrorCode, path: str = "") -> None:
    raise _DirectFailure(code, path)


def _failure(error: _DirectFailure) -> dict[str, object]:
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


def _bounded_path(path: str) -> str:
    try:
        if len(path.encode("utf-8")) <= _MAX_ERROR_PATH_BYTES:
            return path
    except UnicodeError:
        pass
    return "/_truncated"


def _field_path(group: str, name: str) -> str:
    return _bounded_path(f"/{group}/{name}")


def _utf8_size(value: str, path: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError:
        _raise(TaskApiErrorCode.INVALID_VALUE, path)


def _canonical_size(value: object, maximum: int) -> int:
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


def _validate_json_tree(value: object, path: str = "") -> None:
    nodes = 0
    seen: set[int] = set()
    stack: list[tuple[object, str, int]] = [(value, path, 0)]
    while stack:
        current, current_path, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            _raise(TaskApiErrorCode.BUDGET_EXCEEDED, current_path)
        if current is None or type(current) is bool:
            continue
        if type(current) is int:
            if abs(current) > MAX_SAFE_JSON_INTEGER:
                _raise(TaskApiErrorCode.INVALID_VALUE, current_path)
            continue
        if type(current) is float:
            if not math.isfinite(current):
                _raise(TaskApiErrorCode.INVALID_VALUE, current_path)
            continue
        if type(current) is str:
            if len(current) > _MAX_JSON_STRING_BYTES:
                _raise(TaskApiErrorCode.BUDGET_EXCEEDED, current_path)
            if _utf8_size(current, current_path) > _MAX_JSON_STRING_BYTES:
                _raise(TaskApiErrorCode.BUDGET_EXCEEDED, current_path)
            continue
        if type(current) not in {dict, list}:
            _raise(TaskApiErrorCode.INVALID_TYPE, current_path)
        if depth >= _MAX_JSON_DEPTH:
            _raise(TaskApiErrorCode.BUDGET_EXCEEDED, current_path)
        identity = id(current)
        if identity in seen:
            _raise(TaskApiErrorCode.INVALID_VALUE, current_path)
        seen.add(identity)
        if type(current) is list:
            for index in range(len(current) - 1, -1, -1):
                stack.append((current[index], f"{current_path}/{index}", depth + 1))
            continue
        keys = tuple(current)
        if not all(type(key) is str for key in keys):
            _raise(TaskApiErrorCode.INVALID_TYPE, current_path)
        for key in keys:
            if len(key) > _MAX_JSON_KEY_BYTES:
                _raise(TaskApiErrorCode.BUDGET_EXCEEDED, current_path)
            if _utf8_size(key, current_path) > _MAX_JSON_KEY_BYTES:
                _raise(TaskApiErrorCode.BUDGET_EXCEEDED, current_path)
        for key, item in reversed(tuple(current.items())):
            stack.append((item, f"{current_path}/{key}", depth + 1))


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
            if depth > _MAX_JSON_DEPTH:
                return False
        elif character in "]}":
            depth -= 1
    return True


def _duplicate_checked_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _JsonFailure(TaskApiErrorCode.INVALID_INPUT)
        result[key] = value
    return result


def _parse_integer(token: str) -> int:
    digits = token[1:] if token.startswith("-") else token
    if len(digits) > 16:
        raise _JsonFailure(TaskApiErrorCode.INVALID_VALUE)
    value = int(token)
    if abs(value) > MAX_SAFE_JSON_INTEGER:
        raise _JsonFailure(TaskApiErrorCode.INVALID_VALUE)
    return value


def _parse_float(token: str) -> float:
    if len(token) > 64:
        raise _JsonFailure(TaskApiErrorCode.BUDGET_EXCEEDED)
    value = float(token)
    if not math.isfinite(value):
        raise _JsonFailure(TaskApiErrorCode.INVALID_VALUE)
    return value


def _reject_constant(_token: str) -> None:
    raise _JsonFailure(TaskApiErrorCode.INVALID_VALUE)


def _acceptance_error_path(path: object) -> str:
    if type(path) is not str or (path and not path.startswith("/")):
        return "/acceptance_json"
    if not path:
        return "/acceptance_json"
    raw_tokens = path.split("/")[1:]
    projected: list[str] = []
    state = "spec"
    for token in raw_tokens:
        if state == "spec":
            if token not in {"schema_version", "id", "criteria"}:
                projected.append("_unknown")
                break
            projected.append(token)
            state = "criteria" if token == "criteria" else "leaf"
            continue
        if state == "criteria":
            if not token.isdecimal():
                projected.append("_unknown")
                break
            projected.append(token)
            state = "criterion"
            continue
        if state == "criterion":
            allowed = {
                "schema_version",
                "id",
                "kind",
                "check",
                "target",
                "expected",
                "tolerance",
                "parameters",
                "required",
            }
            if token not in allowed:
                projected.append("_unknown")
                break
            projected.append(token)
            state = "parameters" if token == "parameters" else "leaf"
            continue
        if state == "parameters":
            if token != "unit":
                projected.append("_unknown")
                break
            projected.append(token)
            state = "leaf"
            continue
        projected.append("_unknown")
        break
    return _bounded_path("/acceptance_json/" + "/".join(projected))


def _contract_failure(error: ContractValidationError) -> None:
    try:
        code = TaskApiErrorCode(error.code.value)
    except (AttributeError, ValueError):
        code = TaskApiErrorCode.INVALID_INPUT
    if code not in {
        TaskApiErrorCode.MISSING_FIELD,
        TaskApiErrorCode.UNKNOWN_FIELD,
        TaskApiErrorCode.UNSUPPORTED_VERSION,
        TaskApiErrorCode.INVALID_TYPE,
        TaskApiErrorCode.INVALID_VALUE,
        TaskApiErrorCode.BUDGET_EXCEEDED,
    }:
        code = TaskApiErrorCode.INVALID_INPUT
    _raise(code, _acceptance_error_path(error.path))


def _decode_acceptance(raw: str) -> AcceptanceSpec:
    if len(raw) > _MAX_ACCEPTANCE_BYTES:
        _raise(TaskApiErrorCode.BUDGET_EXCEEDED, "/acceptance_json")
    raw_size = _utf8_size(raw, "/acceptance_json")
    if raw_size > _MAX_ACCEPTANCE_BYTES:
        _raise(TaskApiErrorCode.BUDGET_EXCEEDED, "/acceptance_json")
    if not _json_depth_within_budget(raw):
        _raise(TaskApiErrorCode.BUDGET_EXCEEDED, "/acceptance_json")
    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=_duplicate_checked_object,
            parse_int=_parse_integer,
            parse_float=_parse_float,
            parse_constant=_reject_constant,
        )
    except _JsonFailure as error:
        _raise(error.code, "/acceptance_json")
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError):
        _raise(TaskApiErrorCode.INVALID_INPUT, "/acceptance_json")
    _validate_json_tree(decoded, "/acceptance_json")
    if type(decoded) is not dict:
        _raise(TaskApiErrorCode.INVALID_TYPE, "/acceptance_json")
    criteria = decoded.get("criteria")
    if type(criteria) is list and len(criteria) > _MAX_ACCEPTANCE_CRITERIA:
        _raise(TaskApiErrorCode.BUDGET_EXCEEDED, "/acceptance_json/criteria")
    try:
        acceptance = AcceptanceSpec.from_mapping(decoded)
    except ContractValidationError as error:
        _contract_failure(error)
    except BaseException:
        _raise(TaskApiErrorCode.INTERNAL_ERROR)
    if _canonical_size(acceptance.to_mapping(), _MAX_ACCEPTANCE_BYTES) > (_MAX_ACCEPTANCE_BYTES):
        _raise(TaskApiErrorCode.BUDGET_EXCEEDED, "/acceptance_json")
    return acceptance


def _safe_text(value: object) -> bool:
    return (
        type(value) is str
        and bool(value.strip())
        and len(value) <= 256
        and value.isprintable()
        and len(value.splitlines()) == 1
        and _utf8_size(value, "") <= 256
    )


def _finite(value: object, *, positive: bool = False) -> bool:
    if type(value) not in {int, float}:
        return False
    if type(value) is int and abs(value) > MAX_SAFE_JSON_INTEGER:
        return False
    return math.isfinite(value) and (not positive or value > 0)


def _selector_failure(error: SelectorError, path: str) -> None:
    try:
        code = TaskApiErrorCode(error.code.value)
    except (AttributeError, ValueError):
        code = TaskApiErrorCode.INVALID_INPUT
    if code not in {
        TaskApiErrorCode.MISSING_FIELD,
        TaskApiErrorCode.UNKNOWN_FIELD,
        TaskApiErrorCode.UNSUPPORTED_VERSION,
        TaskApiErrorCode.INVALID_TYPE,
        TaskApiErrorCode.INVALID_VALUE,
        TaskApiErrorCode.INVALID_INPUT,
    }:
        code = TaskApiErrorCode.INVALID_INPUT
    suffix = error.path if type(error.path) is str else ""
    if code is TaskApiErrorCode.UNKNOWN_FIELD:
        suffix = "/_unknown"
    _raise(code, _bounded_path(path + suffix))


def _normalize_field(
    value: object,
    field: FieldMetadata,
    path: str,
) -> tuple[object, SelectorV1 | None]:
    shape = field.value_shape
    if shape is ValueShape.NONBLANK_STRING:
        if not _safe_text(value):
            _raise(TaskApiErrorCode.INVALID_VALUE, path)
        return value, None
    if shape is ValueShape.BOOLEAN:
        if type(value) is not bool:
            _raise(TaskApiErrorCode.INVALID_TYPE, path)
        return value, None
    if shape is ValueShape.INTEGER:
        if type(value) is not int:
            _raise(TaskApiErrorCode.INVALID_TYPE, path)
        if abs(value) > MAX_SAFE_JSON_INTEGER:
            _raise(TaskApiErrorCode.INVALID_VALUE, path)
        return value, None
    if shape in {ValueShape.FINITE_NUMBER, ValueShape.POSITIVE_NUMBER}:
        if type(value) not in {int, float}:
            _raise(TaskApiErrorCode.INVALID_TYPE, path)
        if not _finite(value, positive=shape is ValueShape.POSITIVE_NUMBER):
            _raise(TaskApiErrorCode.INVALID_VALUE, path)
        return value, None
    if shape is ValueShape.ANGLE_DEGREES:
        if type(value) not in {int, float}:
            _raise(TaskApiErrorCode.INVALID_TYPE, path)
        if not _finite(value) or value == 0 or not -360 < value < 360:
            _raise(TaskApiErrorCode.INVALID_VALUE, path)
        return value, None
    if shape is ValueShape.ENUM:
        if type(value) is not str:
            _raise(TaskApiErrorCode.INVALID_TYPE, path)
        if value not in field.enum_values:
            _raise(TaskApiErrorCode.INVALID_VALUE, path)
        return value, None
    if shape in {ValueShape.VECTOR2, ValueShape.VECTOR3}:
        if type(value) is not list:
            _raise(TaskApiErrorCode.INVALID_TYPE, path)
        expected = 2 if shape is ValueShape.VECTOR2 else 3
        if len(value) != expected:
            _raise(TaskApiErrorCode.INVALID_VALUE, path)
        if not all(_finite(component) for component in value):
            _raise(TaskApiErrorCode.INVALID_VALUE, path)
        return list(value), None
    if shape is ValueShape.QUANTITY:
        if type(value) is not dict:
            _raise(TaskApiErrorCode.INVALID_TYPE, path)
        keys = tuple(value)
        if not all(type(key) is str for key in keys) or set(keys) != {"value", "unit"}:
            _raise(TaskApiErrorCode.INVALID_VALUE, path)
        amount = value["value"]
        unit = value["unit"]
        if not _finite(amount) or type(unit) is not str or unit not in field.allowed_units:
            _raise(TaskApiErrorCode.INVALID_VALUE, path)
        return {"value": amount, "unit": unit}, None
    if shape in {ValueShape.OBJECT_SELECTOR, ValueShape.ENTITY_TARGET}:
        if type(value) is not dict:
            _raise(TaskApiErrorCode.INVALID_TYPE, path)
        try:
            selector = SelectorV1.from_mapping(value)
        except SelectorError as error:
            _selector_failure(error, path)
        except BaseException:
            _raise(TaskApiErrorCode.INTERNAL_ERROR)
        return selector.to_mapping(), selector
    if shape is ValueShape.OBJECT_ID:
        if type(value) is not str:
            _raise(TaskApiErrorCode.INVALID_TYPE, path)
        if _OBJECT_ID.fullmatch(value) is None:
            _raise(TaskApiErrorCode.INVALID_VALUE, path)
        return value, None
    if shape is ValueShape.RESULT_REF:
        _raise(TaskApiErrorCode.INVALID_VALUE, path)
    _raise(TaskApiErrorCode.INVALID_INPUT, path)


def _normalize_group(
    values: dict[str, object],
    fields: tuple[FieldMetadata, ...],
    group: str,
) -> tuple[dict[str, object], tuple[tuple[str, SelectorV1], ...]]:
    by_name = {field.name: field for field in fields}
    keys = tuple(values)
    if not all(type(key) is str for key in keys):
        _raise(TaskApiErrorCode.INVALID_TYPE, f"/{group}")
    if set(keys) - set(by_name):
        _raise(TaskApiErrorCode.UNKNOWN_FIELD, f"/{group}/_unknown")
    missing = sorted(field.name for field in fields if field.required and field.name not in values)
    if missing:
        _raise(TaskApiErrorCode.MISSING_FIELD, _field_path(group, missing[0]))
    normalized: dict[str, object] = {}
    selectors: list[tuple[str, SelectorV1]] = []
    for name in sorted(values):
        item, selector = _normalize_field(
            values[name],
            by_name[name],
            _field_path(group, name),
        )
        normalized[name] = item
        if selector is not None:
            selectors.append((_field_path(group, name), selector))
    return normalized, tuple(selectors)


def _normalize_preserve(
    value: list[object],
    metadata: OperationMetadata,
) -> tuple[str, ...]:
    if len(value) > len(metadata.preservation_fields):
        _raise(TaskApiErrorCode.INVALID_VALUE, "/preserve")
    result: list[str] = []
    for index, item in enumerate(value):
        path = f"/preserve/{index}"
        if type(item) is not str:
            _raise(TaskApiErrorCode.INVALID_TYPE, path)
        if item in result or item not in metadata.preservation_fields:
            _raise(TaskApiErrorCode.INVALID_VALUE, path)
        result.append(item)
    return tuple(result)


def _validate_request(
    request: object,
    metadata: OperationMetadata,
) -> tuple[
    str,
    int,
    dict[str, object],
    dict[str, object],
    tuple[str, ...],
    AcceptanceSpec,
    tuple[tuple[str, SelectorV1], ...],
]:
    if type(request) is not dict:
        _raise(TaskApiErrorCode.INVALID_TYPE)
    keys = tuple(request)
    if not all(type(key) is str for key in keys):
        _raise(TaskApiErrorCode.INVALID_TYPE)
    if set(keys) - _REQUEST_FIELDS:
        _raise(TaskApiErrorCode.UNKNOWN_FIELD, "/_unknown")
    missing = sorted(_REQUEST_FIELDS - set(keys))
    if missing:
        _raise(TaskApiErrorCode.MISSING_FIELD, f"/{missing[0]}")

    version = request["schema_version"]
    if type(version) is not int:
        _raise(TaskApiErrorCode.INVALID_TYPE, "/schema_version")
    if abs(version) > MAX_SAFE_JSON_INTEGER:
        _raise(TaskApiErrorCode.INVALID_VALUE, "/schema_version")
    if version != SCHEMA_VERSION:
        _raise(TaskApiErrorCode.UNSUPPORTED_VERSION, "/schema_version")
    task_id = request["task_id"]
    if type(task_id) is not str:
        _raise(TaskApiErrorCode.INVALID_TYPE, "/task_id")
    if _TASK_ID.fullmatch(task_id) is None:
        _raise(TaskApiErrorCode.INVALID_VALUE, "/task_id")
    generation = request["expected_generation"]
    if type(generation) is not int:
        _raise(TaskApiErrorCode.INVALID_TYPE, "/expected_generation")
    if generation < 0 or generation > MAX_SAFE_JSON_INTEGER:
        _raise(TaskApiErrorCode.INVALID_VALUE, "/expected_generation")
    target = request["target"]
    arguments = request["arguments"]
    preserve = request["preserve"]
    raw_acceptance = request["acceptance_json"]
    if type(target) is not dict:
        _raise(TaskApiErrorCode.INVALID_TYPE, "/target")
    if type(arguments) is not dict:
        _raise(TaskApiErrorCode.INVALID_TYPE, "/arguments")
    if type(preserve) is not list:
        _raise(TaskApiErrorCode.INVALID_TYPE, "/preserve")
    if type(raw_acceptance) is not str:
        _raise(TaskApiErrorCode.INVALID_TYPE, "/acceptance_json")

    non_acceptance = {
        "schema_version": version,
        "task_id": task_id,
        "expected_generation": generation,
        "target": target,
        "arguments": arguments,
        "preserve": preserve,
    }
    _validate_json_tree(non_acceptance)
    non_acceptance_size = _canonical_size(
        non_acceptance,
        _MAX_NON_ACCEPTANCE_BYTES,
    )
    if non_acceptance_size > _MAX_NON_ACCEPTANCE_BYTES:
        _raise(TaskApiErrorCode.BUDGET_EXCEEDED)
    acceptance_size = _utf8_size(raw_acceptance, "/acceptance_json")
    if acceptance_size > _MAX_ACCEPTANCE_BYTES:
        _raise(TaskApiErrorCode.BUDGET_EXCEEDED, "/acceptance_json")
    if non_acceptance_size + acceptance_size > _MAX_LOGICAL_REQUEST_BYTES:
        _raise(TaskApiErrorCode.BUDGET_EXCEEDED)

    normalized_target, target_selectors = _normalize_group(
        target,
        metadata.target_fields,
        "target",
    )
    normalized_arguments, argument_selectors = _normalize_group(
        arguments,
        metadata.argument_fields,
        "arguments",
    )
    normalized_preserve = _normalize_preserve(preserve, metadata)
    acceptance = _decode_acceptance(raw_acceptance)
    return (
        task_id,
        generation,
        normalized_target,
        normalized_arguments,
        normalized_preserve,
        acceptance,
        target_selectors + argument_selectors,
    )


_PUBLIC_REGISTRY_ERROR = "registry public metadata is invalid"


@dataclass(frozen=True, slots=True)
class _PublicFieldSnapshot:
    name: str
    handler_parameter: str
    value_shape: ValueShape
    required: bool
    enum_values: tuple[str, ...]
    allowed_units: tuple[str, ...]
    referenced_value_shape: ValueShape | None


@dataclass(frozen=True, slots=True)
class _PublicResultSlotSnapshot:
    name: str
    result_field: str
    value_shape: ValueShape
    enum_values: tuple[str, ...]
    allowed_units: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PublicOperationSnapshot:
    key: str
    operation: str
    handler_name: str
    risk_class: RiskClass
    evidence_required: bool
    target_fields: tuple[_PublicFieldSnapshot, ...]
    argument_fields: tuple[_PublicFieldSnapshot, ...]
    execution_profiles: tuple[ExecutionProfile, ...]
    minimum_freecad_version: tuple[int, int]
    maximum_freecad_version_exclusive: tuple[int, int]
    requires_gui_main_thread: bool
    resource_budget: tuple[int, int, int]
    direct_exposed: bool
    description: str
    result_slots: tuple[_PublicResultSlotSnapshot, ...]
    preservation_fields: tuple[str, ...]


def _reject_public_registry() -> None:
    raise TypeError(_PUBLIC_REGISTRY_ERROR)


def _exact_string_tuple(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or not all(type(item) is str for item in value):
        _reject_public_registry()
    return value


def _snapshot_public_field(field: FieldMetadata) -> _PublicFieldSnapshot:
    if type(field) is not FieldMetadata:
        _reject_public_registry()
    name = field.name
    handler_parameter = field.handler_parameter
    value_shape = field.value_shape
    required = field.required
    enum_values = field.enum_values
    allowed_units = field.allowed_units
    referenced_value_shape = field.referenced_value_shape
    if (
        type(name) is not str
        or type(handler_parameter) is not str
        or type(value_shape) is not ValueShape
        or type(required) is not bool
        or (referenced_value_shape is not None and type(referenced_value_shape) is not ValueShape)
    ):
        _reject_public_registry()
    return _PublicFieldSnapshot(
        name=name,
        handler_parameter=handler_parameter,
        value_shape=value_shape,
        required=required,
        enum_values=_exact_string_tuple(enum_values),
        allowed_units=_exact_string_tuple(allowed_units),
        referenced_value_shape=referenced_value_shape,
    )


def _snapshot_public_slot(slot: ResultSlotMetadata) -> _PublicResultSlotSnapshot:
    if type(slot) is not ResultSlotMetadata:
        _reject_public_registry()
    name = slot.name
    result_field = slot.result_field
    value_shape = slot.value_shape
    enum_values = slot.enum_values
    allowed_units = slot.allowed_units
    if (
        type(name) is not str
        or type(result_field) is not str
        or type(value_shape) is not ValueShape
    ):
        _reject_public_registry()
    return _PublicResultSlotSnapshot(
        name=name,
        result_field=result_field,
        value_shape=value_shape,
        enum_values=_exact_string_tuple(enum_values),
        allowed_units=_exact_string_tuple(allowed_units),
    )


def _snapshot_public_operation(
    key: str,
    metadata: OperationMetadata,
) -> _PublicOperationSnapshot:
    operation = metadata.operation
    handler_name = metadata.handler_name
    risk_class = metadata.risk_class
    evidence_required = metadata.evidence_required
    target_fields = metadata.target_fields
    argument_fields = metadata.argument_fields
    execution_profiles = metadata.execution_profiles
    minimum = metadata.minimum_freecad_version
    maximum = metadata.maximum_freecad_version_exclusive
    requires_gui = metadata.requires_gui_main_thread
    resource_budget = metadata.resource_budget
    direct_exposed = metadata.direct_exposed
    description = metadata.description
    result_slots = metadata.result_slots
    preservation_fields = metadata.preservation_fields

    if (
        type(operation) is not str
        or type(handler_name) is not str
        or type(risk_class) is not RiskClass
        or type(evidence_required) is not bool
        or type(requires_gui) is not bool
        or type(direct_exposed) is not bool
        or not _safe_text(description)
        or description != description.strip()
        or type(target_fields) is not tuple
        or not all(type(field) is FieldMetadata for field in target_fields)
        or type(argument_fields) is not tuple
        or not all(type(field) is FieldMetadata for field in argument_fields)
        or type(execution_profiles) is not tuple
        or not all(type(profile) is ExecutionProfile for profile in execution_profiles)
        or type(minimum) is not tuple
        or len(minimum) != 2
        or not all(type(item) is int for item in minimum)
        or type(maximum) is not tuple
        or len(maximum) != 2
        or not all(type(item) is int for item in maximum)
        or type(resource_budget) is not ResourceBudget
        or type(result_slots) is not tuple
        or not all(type(slot) is ResultSlotMetadata for slot in result_slots)
    ):
        _reject_public_registry()
    max_runtime_ms = resource_budget.max_runtime_ms
    max_created_objects = resource_budget.max_created_objects
    max_result_bytes = resource_budget.max_result_bytes
    if not all(
        type(value) is int for value in (max_runtime_ms, max_created_objects, max_result_bytes)
    ):
        _reject_public_registry()
    return _PublicOperationSnapshot(
        key=key,
        operation=operation,
        handler_name=handler_name,
        risk_class=risk_class,
        evidence_required=evidence_required,
        target_fields=tuple(_snapshot_public_field(field) for field in target_fields),
        argument_fields=tuple(_snapshot_public_field(field) for field in argument_fields),
        execution_profiles=execution_profiles,
        minimum_freecad_version=(minimum[0], minimum[1]),
        maximum_freecad_version_exclusive=(maximum[0], maximum[1]),
        requires_gui_main_thread=requires_gui,
        resource_budget=(max_runtime_ms, max_created_objects, max_result_bytes),
        direct_exposed=direct_exposed,
        description=description,
        result_slots=tuple(_snapshot_public_slot(slot) for slot in result_slots),
        preservation_fields=_exact_string_tuple(preservation_fields),
    )


def _materialize_public_operation(
    snapshot: _PublicOperationSnapshot,
) -> OperationMetadata:
    target_fields = tuple(
        FieldMetadata(
            name=field.name,
            handler_parameter=field.handler_parameter,
            value_shape=field.value_shape,
            required=field.required,
            enum_values=field.enum_values,
            allowed_units=field.allowed_units,
            referenced_value_shape=field.referenced_value_shape,
        )
        for field in snapshot.target_fields
    )
    argument_fields = tuple(
        FieldMetadata(
            name=field.name,
            handler_parameter=field.handler_parameter,
            value_shape=field.value_shape,
            required=field.required,
            enum_values=field.enum_values,
            allowed_units=field.allowed_units,
            referenced_value_shape=field.referenced_value_shape,
        )
        for field in snapshot.argument_fields
    )
    result_slots = tuple(
        ResultSlotMetadata(
            name=slot.name,
            result_field=slot.result_field,
            value_shape=slot.value_shape,
            enum_values=slot.enum_values,
            allowed_units=slot.allowed_units,
        )
        for slot in snapshot.result_slots
    )
    return OperationMetadata(
        operation=snapshot.operation,
        handler_name=snapshot.handler_name,
        risk_class=snapshot.risk_class,
        evidence_required=snapshot.evidence_required,
        target_fields=target_fields,
        argument_fields=argument_fields,
        execution_profiles=snapshot.execution_profiles,
        minimum_freecad_version=snapshot.minimum_freecad_version,
        maximum_freecad_version_exclusive=snapshot.maximum_freecad_version_exclusive,
        requires_gui_main_thread=snapshot.requires_gui_main_thread,
        resource_budget=ResourceBudget(
            max_runtime_ms=snapshot.resource_budget[0],
            max_created_objects=snapshot.resource_budget[1],
            max_result_bytes=snapshot.resource_budget[2],
        ),
        direct_exposed=snapshot.direct_exposed,
        description=snapshot.description,
        result_slots=result_slots,
        preservation_fields=snapshot.preservation_fields,
    )


def _snapshot_public_registry(registry: OperationRegistry) -> OperationRegistry:
    """Copy an exact registry before any public sorting, hashing, or schema work."""

    if type(registry) is not OperationRegistry:
        _reject_public_registry()
    try:
        operations = object.__getattribute__(registry, "_operations")
    except BaseException:
        _reject_public_registry()
    if type(operations) is not MappingProxyType:
        _reject_public_registry()
    try:
        entries = tuple(operations.items())
    except BaseException:
        _reject_public_registry()
    if not all(
        type(key) is str and type(metadata) is OperationMetadata for key, metadata in entries
    ):
        _reject_public_registry()
    try:
        snapshots = tuple(_snapshot_public_operation(key, metadata) for key, metadata in entries)
        if any(snapshot.key != snapshot.operation for snapshot in snapshots):
            _reject_public_registry()
        if any(
            snapshot.direct_exposed and snapshot.operation in _STABLE_TOOL_NAMES
            for snapshot in snapshots
        ):
            _reject_public_registry()
        return OperationRegistry(_materialize_public_operation(snapshot) for snapshot in snapshots)
    except TypeError as error:
        if type(error) is TypeError and error.args == (_PUBLIC_REGISTRY_ERROR,):
            raise
        _reject_public_registry()
    except BaseException:
        _reject_public_registry()


def _direct_names_from_snapshot(registry: OperationRegistry) -> tuple[str, ...]:
    return tuple(
        sorted(name for name, metadata in registry.operations.items() if metadata.direct_exposed)
    )


def direct_operation_names(
    registry: OperationRegistry = DEFAULT_OPERATION_REGISTRY,
) -> tuple[str, ...]:
    """Return sorted direct names from one exact structural registry copy."""

    return _direct_names_from_snapshot(_snapshot_public_registry(registry))


class DirectOperationApi:
    """Compile one strict direct request into one submitted ModelProgram."""

    __slots__ = ("_direct", "_port", "_registry", "_task_api")

    def __init__(
        self,
        *,
        port: object,
        registry: OperationRegistry = DEFAULT_OPERATION_REGISTRY,
    ) -> None:
        snapshot = _snapshot_public_registry(registry)
        direct = {name: snapshot.operations[name] for name in _direct_names_from_snapshot(snapshot)}
        self._port = port
        self._registry = snapshot
        self._direct = MappingProxyType(direct)
        self._task_api = TaskApi(port=port, registry=snapshot)

    @staticmethod
    def _get_stored(port: object, task_id: str) -> StoredTaskRun:
        try:
            value = port.get_task(task_id=task_id)
        except BaseException:
            _raise(TaskApiErrorCode.INTERNAL_ERROR)
        if type(value) is TaskServicePortFailure:
            try:
                code = value.code
            except BaseException:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            if type(code) is not TaskServicePortErrorCode:
                _raise(TaskApiErrorCode.INTERNAL_ERROR)
            _raise(_PORT_ERROR_MAP[code])
        if type(value) is not StoredTaskRun or value.task_run.id != task_id:
            _raise(TaskApiErrorCode.INTERNAL_ERROR)
        return value

    def _invoke(self, operation: object, request: object) -> dict[str, object]:
        if type(operation) is not str:
            _raise(TaskApiErrorCode.INVALID_INPUT)
        metadata = self._direct.get(operation)
        if metadata is None:
            _raise(TaskApiErrorCode.INVALID_INPUT)
        (
            task_id,
            generation,
            target,
            arguments,
            preserve,
            acceptance,
            selectors,
        ) = _validate_request(request, metadata)

        stored = self._get_stored(self._port, task_id)
        if stored.generation != generation:
            _raise(TaskApiErrorCode.CONFLICT)
        task = stored.task_run
        if task.status not in {TaskStatus.NEEDS_PLAN, TaskStatus.NEEDS_INPUT}:
            _raise(TaskApiErrorCode.INVALID_STATE)
        if task.candidate_revision is not None:
            _raise(TaskApiErrorCode.INVALID_STATE)
        for path, selector in selectors:
            if selector.project_id != task.project_id:
                _raise(TaskApiErrorCode.INVALID_INPUT, f"{path}/project_id")
            if selector.revision_id != task.base_revision:
                _raise(TaskApiErrorCode.INVALID_INPUT, f"{path}/revision_id")

        command = ModelCommand(
            id="direct_operation",
            op=operation,
            source=ValueSource.MODEL,
            target=target,
            args=arguments,
            preserve=preserve,
            depends_on=(),
        )
        program = ModelProgram(
            task_id=task.id,
            base_revision=task.base_revision,
            operations=(command,),
            acceptance=acceptance,
        )
        try:
            compile_acceptance_spec(acceptance)
            validate_model_program(program, registry=self._registry)
        except (ValidationError, ProgramValidationError, ValueError, TypeError):
            _raise(TaskApiErrorCode.INVALID_INPUT)
        except BaseException:
            _raise(TaskApiErrorCode.INTERNAL_ERROR)

        program_json = json.dumps(
            program.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return self._task_api.submit_model_program(
            {
                "schema_version": SCHEMA_VERSION,
                "task_id": task_id,
                "expected_generation": generation,
                "program_json": program_json,
            }
        )

    def invoke(self, operation: object, request: object) -> dict[str, object]:
        """Validate and submit one direct operation without retrying effects."""

        try:
            return self._invoke(operation, request)
        except _DirectFailure as error:
            return _failure(error)
        except BaseException:
            return _failure(_DirectFailure(TaskApiErrorCode.INTERNAL_ERROR))


_PUBLIC_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PROJECT_PATTERN = r"^project_[0-9a-f]{32}$"
_REVISION_PATTERN = r"^revision_[0-9a-f]{32}$"
_DRAFT_PATTERN = r"^draft_[0-9a-f]{32}$"
_ARTIFACT_PATTERN = r"^artifact_[0-9a-f]{32}$"
_VERIFICATION_PATTERN = r"^verification_[0-9a-f]{32}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_CREATE_KEY_PATTERN = r"^project_create_[0-9a-f]{32}$"
_TASK_CREATE_KEY_PATTERN = r"^task_create_[0-9a-f]{32}$"
_EXPORT_KEY_PATTERN = r"^export_[0-9a-f]{32}$"
_MATERIALIZATION_PATTERN = r"^materialization_[0-9a-f]{64}$"
_FEATURE_PATTERN = r"^feature_[0-9a-f]{32}$"
_OBJECT_TYPE_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*(?:::[A-Za-z][A-Za-z0-9_]*)+$"
_VERSION_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$"

_STABLE_TOOL_NAMES = (
    "ping",
    "get_runtime_status",
    "ensure_runtime",
    "uninstall_runtime",
    "get_capabilities",
    "create_project",
    "get_project",
    "create_task",
    "get_task",
    "submit_model_program",
    "resume_task",
    "accept_draft",
    "reject_draft",
    "export_task_artifacts",
)

_STABLE_TOOL_DESCRIPTIONS = MappingProxyType(
    {
        "ping": "检查 VibeCAD Agent 服务是否可达",
        "get_runtime_status": "查询受管 FreeCAD 运行时的状态和兼容性",
        "ensure_runtime": "安装、升级或验证受管 FreeCAD 运行时",
        "uninstall_runtime": "预览或确认清理受管 CAD 运行时，保留项目数据",
        "get_capabilities": "返回当前可执行操作的冻结能力元数据",
        "create_project": "创建空项目或导入仅含长方体和圆柱体的 FCStd 作为第零代",
        "get_project": "读取持久化项目的当前版本",
        "create_task": "在指定项目版本上创建可验收任务",
        "get_task": "读取任务的持久化状态与证据",
        "submit_model_program": "提交受约束的 ModelProgram 并生成候选版本",
        "resume_task": "按当前持久化代数恢复可继续任务",
        "accept_draft": "验证并接受指定草案版本",
        "reject_draft": "拒绝指定草案并保留审核记录",
        "export_task_artifacts": "生成可验证的 FCStd 和 STEP 交付资源",
    }
)


def _deep_freeze(value: object) -> object:
    if type(value) in {str, int, float, bool, type(None)}:
        return value
    if type(value) in {tuple, list}:
        return tuple(_deep_freeze(item) for item in value)
    if type(value) in {dict, MappingProxyType}:
        if not all(type(key) is str for key in value):
            raise TypeError("schema object keys must be exact strings")
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    raise TypeError("schema values must be JSON-compatible")


@dataclass(frozen=True, slots=True)
class ToolAnnotations:
    """Transport-neutral, explicit MCP side-effect hints."""

    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool

    def __post_init__(self) -> None:
        if not all(
            type(value) is bool
            for value in (
                self.read_only,
                self.destructive,
                self.idempotent,
                self.open_world,
            )
        ):
            raise TypeError("tool annotation hints must be exact booleans")


@dataclass(frozen=True, slots=True)
class PublicToolSpec:
    """One immutable public tool projection independent of MCP SDK objects."""

    name: str
    description: str
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    annotations: ToolAnnotations

    def __post_init__(self) -> None:
        if type(self.name) is not str or _PUBLIC_NAME.fullmatch(self.name) is None:
            raise ValueError("public tool name is invalid")
        if not _safe_text(self.description) or self.description != self.description.strip():
            raise ValueError("public tool description is invalid")
        if type(self.annotations) is not ToolAnnotations:
            raise TypeError("annotations must be exact ToolAnnotations")
        input_schema = _deep_freeze(self.input_schema)
        output_schema = _deep_freeze(self.output_schema)
        if type(input_schema) is not MappingProxyType:
            raise TypeError("input_schema must be an object schema")
        if type(output_schema) is not MappingProxyType:
            raise TypeError("output_schema must be an object schema")
        object.__setattr__(self, "input_schema", input_schema)
        object.__setattr__(self, "output_schema", output_schema)


def _closed_schema(
    properties: dict[str, object],
    *,
    required: tuple[str, ...] | None = None,
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": tuple(properties) if required is None else required,
        "additionalProperties": False,
    }


def _nullable(schema: dict[str, object]) -> dict[str, object]:
    return {"anyOf": (schema, {"type": "null"})}


def _version_schema() -> dict[str, object]:
    return {"type": "integer", "const": SCHEMA_VERSION}


def _safe_integer_schema(*, minimum: int | None = None) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "integer",
        "minimum": -MAX_SAFE_JSON_INTEGER,
        "maximum": MAX_SAFE_JSON_INTEGER,
    }
    if minimum is not None:
        schema["minimum"] = minimum
    return schema


def _id_schema(pattern: str) -> dict[str, object]:
    return {"type": "string", "pattern": pattern}


def _bounded_text_schema(maximum: int = 256) -> dict[str, object]:
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _string_array_schema(*, maximum: int | None = None) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "array",
        "items": _bounded_text_schema(),
    }
    if maximum is not None:
        schema["maxItems"] = maximum
    return schema


def _empty_input_schema() -> dict[str, object]:
    return _closed_schema({}, required=())


def _selector_schema() -> dict[str, object]:
    provenance = _closed_schema(
        {
            "source": {
                "type": "string",
                "enum": tuple(item.value for item in ProvenanceSource),
            },
            "operation_id": _nullable(_bounded_text_schema()),
        }
    )
    properties = {
        "schema_version": _version_schema(),
        "project_id": _id_schema(_PROJECT_PATTERN),
        "revision_id": _id_schema(_REVISION_PATTERN),
        "entity_kind": {
            "type": "string",
            "enum": tuple(item.value for item in EntityKind),
        },
        "object_id": _id_schema(_OBJECT_ID.pattern),
        "feature_id": _nullable(_id_schema(_FEATURE_PATTERN)),
        "object_type": {
            "type": "string",
            "pattern": _OBJECT_TYPE_PATTERN,
            "maxLength": 128,
        },
        "semantic_role": {
            "type": "string",
            "enum": tuple(item.value for item in SemanticRole),
        },
        "provenance": provenance,
        "expected_cardinality": {"type": "integer", "const": 1},
    }
    return _closed_schema(properties)


def _field_schema(field: FieldMetadata) -> dict[str, object]:
    shape = field.value_shape
    if shape is ValueShape.NONBLANK_STRING:
        return {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "pattern": r".*\S.*",
        }
    if shape is ValueShape.BOOLEAN:
        return {"type": "boolean"}
    if shape is ValueShape.INTEGER:
        return _safe_integer_schema()
    if shape is ValueShape.FINITE_NUMBER:
        return {"type": "number"}
    if shape is ValueShape.POSITIVE_NUMBER:
        return {"type": "number", "exclusiveMinimum": 0}
    if shape is ValueShape.ANGLE_DEGREES:
        return {
            "type": "number",
            "exclusiveMinimum": -360,
            "exclusiveMaximum": 360,
            "not": {"const": 0},
        }
    if shape is ValueShape.ENUM:
        return {"type": "string", "enum": tuple(field.enum_values)}
    if shape in {ValueShape.VECTOR2, ValueShape.VECTOR3}:
        count = 2 if shape is ValueShape.VECTOR2 else 3
        return {
            "type": "array",
            "items": {"type": "number"},
            "minItems": count,
            "maxItems": count,
        }
    if shape is ValueShape.QUANTITY:
        return _closed_schema(
            {
                "value": {"type": "number"},
                "unit": {"type": "string", "enum": tuple(field.allowed_units)},
            }
        )
    if shape in {ValueShape.OBJECT_SELECTOR, ValueShape.ENTITY_TARGET}:
        return _selector_schema()
    if shape is ValueShape.OBJECT_ID:
        return _id_schema(_OBJECT_ID.pattern)
    if shape is ValueShape.RESULT_REF:
        raise ValueError("direct public tools cannot expose result references")
    raise ValueError("unsupported registry value shape")


def _field_group_schema(fields: tuple[FieldMetadata, ...]) -> dict[str, object]:
    properties = {field.name: _field_schema(field) for field in fields}
    required = tuple(field.name for field in fields if field.required)
    return _closed_schema(properties, required=required)


def _direct_input_schema(metadata: OperationMetadata) -> dict[str, object]:
    preserve: dict[str, object] = {
        "type": "array",
        "uniqueItems": True,
        "maxItems": len(metadata.preservation_fields),
    }
    if metadata.preservation_fields:
        preserve["items"] = {
            "type": "string",
            "enum": tuple(metadata.preservation_fields),
        }
    properties = {
        "schema_version": _version_schema(),
        "task_id": _id_schema(_TASK_ID.pattern),
        "expected_generation": _safe_integer_schema(minimum=0),
        "target": _field_group_schema(metadata.target_fields),
        "arguments": _field_group_schema(metadata.argument_fields),
        "preserve": preserve,
        "acceptance_json": {
            "type": "string",
            "maxLength": _MAX_ACCEPTANCE_BYTES,
        },
    }
    return _closed_schema(properties)


def _public_error_schema() -> dict[str, object]:
    codes = (
        "missing_field",
        "unknown_field",
        "unsupported_version",
        "invalid_type",
        "invalid_value",
        "budget_exceeded",
        "invalid_input",
        "unsupported_reasoning_owner",
        "invalid_state",
        "not_found",
        "conflict",
        "lease_unavailable",
        "resource_exhausted",
        "runtime_unavailable",
        "integrity_failure",
        "cad_failure",
        "runtime_failure",
        "store_failure",
        "recovery_required",
        "internal_error",
    )
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "code": {"type": "string", "enum": codes},
            "path": {"type": "string", "maxLength": _MAX_ERROR_PATH_BYTES},
            "message": _bounded_text_schema(),
        }
    )


def _envelope_schema(result_schema: dict[str, object]) -> dict[str, object]:
    schema = _closed_schema(
        {
            "schema_version": _version_schema(),
            "ok": {"type": "boolean"},
            "result": _nullable(result_schema),
            "error": _nullable(_public_error_schema()),
        }
    )
    schema["oneOf"] = (
        {
            "properties": {
                "ok": {"const": True},
                "result": result_schema,
                "error": {"type": "null"},
            }
        },
        {
            "properties": {
                "ok": {"const": False},
                "result": {"type": "null"},
                "error": _public_error_schema(),
            }
        },
    )
    return schema


def _ping_result_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "service": {"type": "string", "const": "vibecad"},
            "version": {
                "type": "string",
                "pattern": _VERSION_PATTERN,
                "maxLength": 64,
            },
        }
    )


def _runtime_status_result_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "phase": {
                "type": "string",
                "enum": (
                    "not_started",
                    "downloading_micromamba",
                    "creating_env",
                    "installing_pip",
                    "verifying",
                    "ready",
                    "failed",
                ),
            },
            "percent": {"type": "number", "minimum": 0, "maximum": 100},
            "message": {"type": "string", "maxLength": 4096},
            "error": _nullable({"type": "string", "maxLength": 4096}),
            "runtime_compatible": {"type": "boolean"},
            "runtime_action": {
                "type": "string",
                "enum": ("ready", "upgrade_required", "repair_required"),
            },
            "installed_version": _nullable(
                {
                    "type": "string",
                    "pattern": _VERSION_PATTERN,
                    "maxLength": 64,
                }
            ),
            "required_version": {
                "type": "string",
                "pattern": _VERSION_PATTERN,
                "maxLength": 64,
            },
            "needs_reconnect": {"type": "boolean"},
        }
    )


def _ensure_result_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "status": {
                "type": "string",
                "enum": ("started", "in_progress", "ready"),
            },
            "message": _bounded_text_schema(4096),
        }
    )


def _uninstall_result_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "status": {
                "type": "string",
                "enum": ("preview", "marked", "already_clean"),
            },
            "confirm_required": {"type": "boolean"},
            "estimated_size_bytes": _safe_integer_schema(minimum=0),
            "data_preserved": {"type": "boolean", "const": True},
            "message": _bounded_text_schema(4096),
        }
    )


def _capability_field_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "name": _bounded_text_schema(),
            "value_shape": {
                "type": "string",
                "enum": tuple(item.value for item in ValueShape),
            },
            "required": {"type": "boolean"},
            "enum_values": _string_array_schema(),
            "allowed_units": _string_array_schema(),
            "referenced_value_shape": _nullable(
                {
                    "type": "string",
                    "enum": tuple(item.value for item in ValueShape),
                }
            ),
        }
    )


def _capability_slot_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "name": _bounded_text_schema(),
            "value_shape": {
                "type": "string",
                "enum": tuple(item.value for item in ValueShape),
            },
            "enum_values": _string_array_schema(),
            "allowed_units": _string_array_schema(),
        }
    )


def _capability_operation_schema() -> dict[str, object]:
    version_pair = {
        "type": "array",
        "items": {"type": "integer", "minimum": 0, "maximum": 999},
        "minItems": 2,
        "maxItems": 2,
    }
    return _closed_schema(
        {
            "operation": _bounded_text_schema(),
            "risk_class": {
                "type": "string",
                "enum": tuple(item.value for item in RiskClass),
            },
            "evidence_required": {"type": "boolean"},
            "target_fields": {
                "type": "array",
                "items": _capability_field_schema(),
            },
            "argument_fields": {
                "type": "array",
                "items": _capability_field_schema(),
            },
            "execution_profiles": _string_array_schema(),
            "minimum_freecad_version": version_pair,
            "maximum_freecad_version_exclusive": version_pair,
            "requires_gui_main_thread": {"type": "boolean"},
            "resource_budget": _closed_schema(
                {
                    "max_runtime_ms": _safe_integer_schema(minimum=1),
                    "max_created_objects": _safe_integer_schema(minimum=0),
                    "max_result_bytes": _safe_integer_schema(minimum=1),
                }
            ),
            "direct_exposed": {"type": "boolean"},
            "result_slots": {
                "type": "array",
                "items": _capability_slot_schema(),
            },
            "preservation_fields": _string_array_schema(),
        }
    )


def _capabilities_result_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "registry_schema_version": _version_schema(),
            "operations": {
                "type": "array",
                "items": _capability_operation_schema(),
            },
        }
    )


def _revision_artifact_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "id": _id_schema(_ARTIFACT_PATTERN),
            "name": {
                "type": "string",
                "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$",
            },
            "format": {"type": "string", "enum": ("fcstd", "step")},
            "sha256": _id_schema(_DIGEST_PATTERN),
            "size_bytes": _safe_integer_schema(minimum=1),
        }
    )


def _project_head_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "project_id": _id_schema(_PROJECT_PATTERN),
            "generation": _safe_integer_schema(minimum=0),
            "revision_id": _id_schema(_REVISION_PATTERN),
            "manifest_sha256": _id_schema(_DIGEST_PATTERN),
        }
    )


def _revision_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "id": _id_schema(_REVISION_PATTERN),
            "project_id": _id_schema(_PROJECT_PATTERN),
            "base_revision": _nullable(_id_schema(_REVISION_PATTERN)),
            "manifest_sha256": _id_schema(_DIGEST_PATTERN),
            "model": _nullable(_revision_artifact_schema()),
            "artifacts": {
                "type": "array",
                "items": _revision_artifact_schema(),
            },
        }
    )


def _project_snapshot_schema() -> dict[str, object]:
    return _closed_schema({"head": _project_head_schema(), "revision": _revision_schema()})


def _project_create_result_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "create_key": _id_schema(_CREATE_KEY_PATTERN),
            "kind": {"type": "string", "enum": ("empty", "import_fcstd")},
            "cleanup_required": {"type": "boolean"},
            "project_id": _id_schema(_PROJECT_PATTERN),
            "generation_zero": _project_snapshot_schema(),
        }
    )


def _project_get_result_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "project_id": _id_schema(_PROJECT_PATTERN),
            "current": _project_snapshot_schema(),
        }
    )


def _materialized_artifact_schema() -> dict[str, object]:
    properties = _revision_artifact_schema()["properties"].copy()
    properties["resource_uri"] = {
        "type": "string",
        "pattern": (
            r"^vibecad://artifact/materialization_[0-9a-f]{64}/"
            r"artifact_[0-9a-f]{32}$"
        ),
    }
    return _closed_schema(properties)


def _artifact_result_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "export_key": _id_schema(_EXPORT_KEY_PATTERN),
            "materialization_id": _id_schema(_MATERIALIZATION_PATTERN),
            "source_kind": {
                "type": "string",
                "enum": ("committed", "draft"),
            },
            "task_id": _id_schema(_TASK_ID.pattern),
            "task_generation": _safe_integer_schema(minimum=0),
            "project_id": _id_schema(_PROJECT_PATTERN),
            "revision_id": _id_schema(_REVISION_PATTERN),
            "manifest_sha256": _id_schema(_DIGEST_PATTERN),
            "authoritative": {"type": "boolean", "const": False},
            "artifacts": {
                "type": "array",
                "items": _materialized_artifact_schema(),
                "minItems": 2,
                "maxItems": 2,
            },
        }
    )


def _acceptance_criterion_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "id": _bounded_text_schema(),
            "kind": {
                "type": "string",
                "enum": tuple(item.value for item in AcceptanceKind),
            },
            "check": _bounded_text_schema(),
            "target": _nullable(_bounded_text_schema()),
            "expected": {},
            "tolerance": _nullable({"type": "number", "minimum": 0}),
            "parameters": {"type": "object"},
            "required": {"type": "boolean"},
        }
    )


def _acceptance_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "id": _bounded_text_schema(),
            "criteria": {
                "type": "array",
                "items": _acceptance_criterion_schema(),
                "maxItems": _MAX_ACCEPTANCE_CRITERIA,
            },
        }
    )


def _model_command_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "id": _bounded_text_schema(),
            "op": _bounded_text_schema(),
            "target": {"type": "object"},
            "args": {"type": "object"},
            "preserve": _string_array_schema(),
            "source": {
                "type": "string",
                "enum": tuple(item.value for item in ValueSource),
            },
            "depends_on": _string_array_schema(),
        }
    )


def _model_program_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "task_id": _id_schema(_TASK_ID.pattern),
            "base_revision": _id_schema(_REVISION_PATTERN),
            "operations": {
                "type": "array",
                "items": _model_command_schema(),
            },
            "acceptance": _acceptance_schema(),
        }
    )


def _execution_evidence_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "id": _bounded_text_schema(),
            "kind": {
                "type": "string",
                "enum": tuple(item.value for item in EvidenceKind),
            },
            "name": _bounded_text_schema(),
            "value": {},
            "operation_id": _nullable(_bounded_text_schema()),
            "metadata": {"type": "object"},
        }
    )


def _step_error_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "category": {
                "type": "string",
                "enum": tuple(item.value for item in ErrorCategory),
            },
            "code": _bounded_text_schema(),
            "message": _bounded_text_schema(),
            "retryable": {"type": "boolean"},
            "needs_input": {"type": "boolean"},
            "related_objects": _string_array_schema(),
            "diagnostic_artifacts": _string_array_schema(),
            "operation_id": _nullable(_bounded_text_schema()),
            "details": {"type": "object"},
        }
    )


def _step_result_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "ok": {"type": "boolean"},
            "value": {},
            "elapsed_ms": {"type": "number", "minimum": 0},
            "operation_id": _nullable(_bounded_text_schema()),
            "revision": _nullable(_id_schema(_REVISION_PATTERN)),
            "facts": {"type": "object"},
            "artifacts": _string_array_schema(),
            "warnings": _string_array_schema(),
            "evidence": {
                "type": "array",
                "items": _execution_evidence_schema(),
            },
            "error": _nullable(_step_error_schema()),
        }
    )


def _task_step_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "sequence": _safe_integer_schema(minimum=1),
            "result": _step_result_schema(),
        }
    )


def _measurement_schema(*, nonnegative: bool) -> dict[str, object]:
    number: dict[str, object] = {"type": "number"}
    if nonnegative:
        number["minimum"] = 0
    vector = {
        "type": "array",
        "items": number,
        "minItems": 1,
        "maxItems": 16,
    }
    return {"anyOf": (number, vector, {"type": "null"})}


def _criterion_verdict_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "criterion_id": _bounded_text_schema(),
            "required": {"type": "boolean"},
            "outcome": {
                "type": "string",
                "enum": tuple(item.value for item in CriterionOutcome),
            },
            "expected": {},
            "observed": {},
            "delta": _measurement_schema(nonnegative=False),
            "tolerance": _measurement_schema(nonnegative=True),
            "evidence": _string_array_schema(maximum=MAX_VERDICT_EVIDENCE),
            "message": _bounded_text_schema(),
        }
    )


def _verification_report_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "id": _id_schema(_VERIFICATION_PATTERN),
            "acceptance_id": _bounded_text_schema(),
            "candidate_revision": _id_schema(_REVISION_PATTERN),
            "manifest_sha256": _id_schema(_DIGEST_PATTERN),
            "observation_digest": _id_schema(_DIGEST_PATTERN),
            "passed": {"type": "boolean"},
            "verdicts": {
                "type": "array",
                "items": _criterion_verdict_schema(),
                "minItems": 1,
                "maxItems": MAX_CRITERION_VERDICTS,
            },
        }
    )


def _review_draft_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "id": _id_schema(_DRAFT_PATTERN),
            "task_id": _id_schema(_TASK_ID.pattern),
            "project_id": _id_schema(_PROJECT_PATTERN),
            "base_revision": _id_schema(_REVISION_PATTERN),
            "base_generation": _safe_integer_schema(minimum=0),
            "base_manifest_sha256": _id_schema(_DIGEST_PATTERN),
            "revision_id": _id_schema(_REVISION_PATTERN),
            "manifest_sha256": _id_schema(_DIGEST_PATTERN),
            "verification_id": _id_schema(_VERIFICATION_PATTERN),
            "acceptance_id": _bounded_text_schema(),
            "observation_digest": _id_schema(_DIGEST_PATTERN),
        }
    )


def _task_artifact_schema() -> dict[str, object]:
    properties = _revision_artifact_schema()["properties"].copy()
    properties["size_bytes"] = _safe_integer_schema(minimum=0)
    properties["candidate_revision"] = _id_schema(_REVISION_PATTERN)
    return _closed_schema(properties)


def _transition_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "sequence": _safe_integer_schema(minimum=1),
            "event": {
                "type": "string",
                "enum": tuple(item.value for item in TaskEvent),
            },
            "from_status": {
                "type": "string",
                "enum": tuple(item.value for item in TaskStatus),
            },
            "to_status": {
                "type": "string",
                "enum": tuple(item.value for item in TaskStatus),
            },
        }
    )


def _task_run_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "schema_version": _version_schema(),
            "id": _id_schema(_TASK_ID.pattern),
            "project_id": _id_schema(_PROJECT_PATTERN),
            "base_revision": _id_schema(_REVISION_PATTERN),
            "reasoning_owner": {
                "type": "string",
                "enum": tuple(item.value for item in ReasoningOwner),
            },
            "review_policy": {
                "type": "string",
                "enum": tuple(item.value for item in ReviewPolicy),
            },
            "status": {
                "type": "string",
                "enum": tuple(item.value for item in TaskStatus),
            },
            "creation_digest": _nullable(_id_schema(_DIGEST_PATTERN)),
            "program": _nullable(_model_program_schema()),
            "candidate_revision": _nullable(_id_schema(_REVISION_PATTERN)),
            "committed_revision": _nullable(_id_schema(_REVISION_PATTERN)),
            "draft": _nullable(_review_draft_schema()),
            "steps": {
                "type": "array",
                "items": _task_step_schema(),
                "maxItems": MAX_STEP_RECORDS,
            },
            "verification_reports": {
                "type": "array",
                "items": _verification_report_schema(),
                "maxItems": MAX_VERIFICATION_REPORTS,
            },
            "artifacts": {
                "type": "array",
                "items": _task_artifact_schema(),
                "maxItems": MAX_ARTIFACT_REFS,
            },
            "last_error": _nullable(_step_error_schema()),
            "transitions": {
                "type": "array",
                "items": _transition_schema(),
                "maxItems": MAX_TRANSITION_RECORDS,
            },
        }
    )


def _task_result_schema() -> dict[str, object]:
    return _closed_schema(
        {
            "generation": _safe_integer_schema(minimum=0),
            "next_action": {
                "type": "string",
                "enum": tuple(item.value for item in NextAction),
            },
            "task_run": _task_run_schema(),
        }
    )


def _stable_input_schema(name: str) -> dict[str, object]:
    if name in {"ping", "get_runtime_status", "ensure_runtime"}:
        return _empty_input_schema()
    if name == "uninstall_runtime":
        return _closed_schema({"confirm": {"type": "boolean"}})
    if name == "get_capabilities":
        return _closed_schema({"schema_version": _version_schema()})
    if name == "create_project":
        return _closed_schema(
            {
                "schema_version": _version_schema(),
                "create_key": _id_schema(_CREATE_KEY_PATTERN),
                "kind": {
                    "type": "string",
                    "enum": ("empty", "import_fcstd"),
                },
                "source_path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4096,
                },
            },
            required=("schema_version", "create_key", "kind"),
        )
    if name == "get_project":
        return _closed_schema(
            {
                "schema_version": _version_schema(),
                "project_id": _id_schema(_PROJECT_PATTERN),
            }
        )
    if name == "create_task":
        return _closed_schema(
            {
                "schema_version": _version_schema(),
                "create_key": _id_schema(_TASK_CREATE_KEY_PATTERN),
                "project_id": _id_schema(_PROJECT_PATTERN),
                "review_policy": {
                    "type": "string",
                    "enum": tuple(item.value for item in ReviewPolicy),
                },
            }
        )
    if name == "get_task":
        return _closed_schema(
            {
                "schema_version": _version_schema(),
                "task_id": _id_schema(_TASK_ID.pattern),
            }
        )
    if name == "submit_model_program":
        return _closed_schema(
            {
                "schema_version": _version_schema(),
                "task_id": _id_schema(_TASK_ID.pattern),
                "expected_generation": _safe_integer_schema(minimum=0),
                "program_json": {"type": "string", "maxLength": 512 * 1024},
            }
        )
    if name == "resume_task":
        return _closed_schema(
            {
                "schema_version": _version_schema(),
                "task_id": _id_schema(_TASK_ID.pattern),
                "expected_generation": _safe_integer_schema(minimum=0),
            }
        )
    if name in {"accept_draft", "reject_draft"}:
        return _closed_schema(
            {
                "schema_version": _version_schema(),
                "task_id": _id_schema(_TASK_ID.pattern),
                "draft_id": _id_schema(_DRAFT_PATTERN),
                "expected_generation": _safe_integer_schema(minimum=0),
            }
        )
    if name == "export_task_artifacts":
        return _closed_schema(
            {
                "schema_version": _version_schema(),
                "export_key": _id_schema(_EXPORT_KEY_PATTERN),
                "task_id": _id_schema(_TASK_ID.pattern),
                "expected_generation": _safe_integer_schema(minimum=0),
                "revision_id": _id_schema(_REVISION_PATTERN),
                "draft_id": _nullable(_id_schema(_DRAFT_PATTERN)),
            }
        )
    raise ValueError("unknown stable tool")


def _stable_result_schema(name: str) -> dict[str, object]:
    if name == "ping":
        return _ping_result_schema()
    if name == "get_runtime_status":
        return _runtime_status_result_schema()
    if name == "ensure_runtime":
        return _ensure_result_schema()
    if name == "uninstall_runtime":
        return _uninstall_result_schema()
    if name == "get_capabilities":
        return _capabilities_result_schema()
    if name == "create_project":
        return _project_create_result_schema()
    if name == "get_project":
        return _project_get_result_schema()
    if name in {
        "create_task",
        "get_task",
        "submit_model_program",
        "resume_task",
        "accept_draft",
        "reject_draft",
    }:
        return _task_result_schema()
    if name == "export_task_artifacts":
        return _artifact_result_schema()
    raise ValueError("unknown stable tool")


def _stable_annotations(name: str) -> ToolAnnotations:
    values = {
        "ping": (True, False, True, False),
        "get_runtime_status": (False, False, True, False),
        "ensure_runtime": (False, True, True, True),
        "uninstall_runtime": (False, True, True, False),
        "get_capabilities": (True, False, True, False),
        "create_project": (False, False, True, True),
        "get_project": (False, False, True, False),
        "create_task": (False, False, True, False),
        "get_task": (False, False, True, False),
        "submit_model_program": (False, True, True, False),
        "resume_task": (False, True, True, False),
        "accept_draft": (False, True, True, False),
        "reject_draft": (False, True, True, False),
        "export_task_artifacts": (False, False, True, False),
    }
    try:
        return ToolAnnotations(*values[name])
    except KeyError:
        raise ValueError("unknown stable tool") from None


def _direct_annotations(metadata: OperationMetadata) -> ToolAnnotations:
    destructive = metadata.risk_class is not RiskClass.READ_ONLY and bool(metadata.target_fields)
    return ToolAnnotations(False, destructive, True, False)


def public_tool_specs(
    registry: OperationRegistry = DEFAULT_OPERATION_REGISTRY,
) -> tuple[PublicToolSpec, ...]:
    """Build one fresh deterministic, registry-derived public tool projection."""

    snapshot = _snapshot_public_registry(registry)
    direct_names = _direct_names_from_snapshot(snapshot)
    public_names = (*_STABLE_TOOL_NAMES, *direct_names)
    if len(public_names) != len(set(public_names)):
        _reject_public_registry()
    specs = [
        PublicToolSpec(
            name=name,
            description=_STABLE_TOOL_DESCRIPTIONS[name],
            input_schema=_stable_input_schema(name),
            output_schema=_envelope_schema(_stable_result_schema(name)),
            annotations=_stable_annotations(name),
        )
        for name in _STABLE_TOOL_NAMES
    ]
    for name in direct_names:
        metadata = snapshot.operations[name]
        specs.append(
            PublicToolSpec(
                name=name,
                description=metadata.description,
                input_schema=_direct_input_schema(metadata),
                output_schema=_envelope_schema(_task_result_schema()),
                annotations=_direct_annotations(metadata),
            )
        )
    return tuple(specs)


__all__ = [
    "DirectOperationApi",
    "PublicToolSpec",
    "ToolAnnotations",
    "direct_operation_names",
    "public_tool_specs",
]
