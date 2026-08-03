"""Versioned, provider-neutral data contracts for the VibeCAD workflow core.

The contracts in this module define data shape only.  They do not validate a
program's dependency graph or operation allowlist, execute CAD operations, or
perform any filesystem, network, MCP, model, or FreeCAD work.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Self

from vibecad.workflow.errors import (
    MAX_SAFE_JSON_INTEGER,
    SCHEMA_VERSION,
    SCHEMA_VERSION_RANGE_MESSAGE,
    ContractErrorCode,
    ContractValidationError,
    join_json_pointer,
)

MAX_JSON_CONTAINER_DEPTH = 64

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
type PlainJsonValue = JsonScalar | list["PlainJsonValue"] | dict[str, "PlainJsonValue"]


class IntentKind(StrEnum):
    """The user-visible CAD task family."""

    CREATE = "create"
    MODIFY = "modify"
    ASSEMBLE = "assemble"
    EXPORT = "export"


class AcceptanceKind(StrEnum):
    """Deterministic acceptance-check families."""

    GEOMETRY = "geometry"
    TOPOLOGY = "topology"
    ASSEMBLY = "assembly"
    ARTIFACT = "artifact"
    PRESERVATION = "preservation"
    VISUAL = "visual"


class ValueSource(StrEnum):
    """Provenance for a model-program operation."""

    USER = "user"
    MODEL = "model"
    SYSTEM = "system"
    IMPORTED = "imported"


class EvidenceKind(StrEnum):
    """Evidence produced while executing or verifying an operation."""

    FACT = "fact"
    ARTIFACT = "artifact"
    OBSERVATION = "observation"
    ASSERTION = "assertion"


class ErrorCategory(StrEnum):
    """Stable workflow failure taxonomy from the accepted architecture."""

    VALIDATION = "validation"
    CONFLICT = "conflict"
    LABEL_EXPIRED = "label_expired"
    GEOMETRY = "geometry"
    RUNTIME = "runtime"
    POLICY = "policy"
    CANCELLED = "cancelled"


def _error(code: ContractErrorCode, path: str, message: str) -> ContractValidationError:
    return ContractValidationError(code, path, message)


def _expect_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(ContractErrorCode.INVALID_TYPE, path, "expected a mapping")
    for key in value:
        if type(key) is not str:
            raise _error(
                ContractErrorCode.INVALID_TYPE,
                path,
                "mapping field names must be strings",
            )
    return value


def _fields(
    value: object,
    *,
    allowed: set[str],
    required: set[str],
    path: str = "",
) -> Mapping[str, Any]:
    mapping = _expect_mapping(value, path)
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        name = unknown[0]
        raise _error(
            ContractErrorCode.UNKNOWN_FIELD,
            join_json_pointer(path, name),
            "unknown field",
        )
    missing = sorted(required - set(mapping))
    if missing:
        name = missing[0]
        raise _error(
            ContractErrorCode.MISSING_FIELD,
            join_json_pointer(path, name),
            "required field missing",
        )
    return mapping


def _schema_version(value: object, path: str = "/schema_version") -> int:
    if type(value) is not int:
        raise _error(ContractErrorCode.INVALID_TYPE, path, "schema_version must be an integer")
    if abs(value) > MAX_SAFE_JSON_INTEGER:
        raise _error(ContractErrorCode.INVALID_VALUE, path, SCHEMA_VERSION_RANGE_MESSAGE)
    if value != SCHEMA_VERSION:
        raise _error(
            ContractErrorCode.UNSUPPORTED_VERSION,
            path,
            f"unsupported schema_version {value}; expected {SCHEMA_VERSION}",
        )
    return value


def _text(value: object, path: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str:
        expected = "a string or null" if optional else "a string"
        raise _error(ContractErrorCode.INVALID_TYPE, path, f"expected {expected}")
    if not value.strip():
        raise _error(ContractErrorCode.INVALID_VALUE, path, "must not be blank")
    return value


def _boolean(value: object, path: str) -> bool:
    if type(value) is not bool:
        raise _error(ContractErrorCode.INVALID_TYPE, path, "expected a boolean")
    return value


def _number(value: object, path: str, *, optional: bool = False) -> int | float | None:
    if value is None and optional:
        return None
    if type(value) not in {int, float}:
        expected = "a number or null" if optional else "a number"
        raise _error(ContractErrorCode.INVALID_TYPE, path, f"expected {expected}")
    if type(value) is int and abs(value) > MAX_SAFE_JSON_INTEGER:
        raise _error(
            ContractErrorCode.INVALID_VALUE,
            path,
            f"integer must be between {-MAX_SAFE_JSON_INTEGER} and {MAX_SAFE_JSON_INTEGER}",
        )
    if type(value) is float and not math.isfinite(value):
        raise _error(ContractErrorCode.INVALID_VALUE, path, "number must be finite")
    return value


def _enum[EnumT: StrEnum](value: object, enum_type: type[EnumT], path: str) -> EnumT:
    if isinstance(value, enum_type):
        return value
    if type(value) is not str:
        raise _error(ContractErrorCode.INVALID_TYPE, path, "expected a string enum value")
    try:
        return enum_type(value)
    except ValueError as exc:
        supported = ", ".join(item.value for item in enum_type)
        raise _error(
            ContractErrorCode.INVALID_VALUE,
            path,
            f"unsupported value {value!r}; expected one of: {supported}",
        ) from exc


def _sequence(value: object, path: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, (list, tuple)):
        raise _error(ContractErrorCode.INVALID_TYPE, path, "expected a list")
    return value


def _text_tuple(value: object, path: str) -> tuple[str, ...]:
    result: list[str] = []
    for index, item in enumerate(_sequence(value, path)):
        result.append(_text(item, join_json_pointer(path, str(index))) or "")
    return tuple(result)


def _contract_tuple[ContractT](
    value: object,
    contract_type: type[ContractT],
    path: str,
) -> tuple[ContractT, ...]:
    result: list[ContractT] = []
    for index, item in enumerate(_sequence(value, path)):
        if not isinstance(item, contract_type):
            raise _error(
                ContractErrorCode.INVALID_TYPE,
                join_json_pointer(path, str(index)),
                f"expected {contract_type.__name__}",
            )
        result.append(item)
    return tuple(result)


def _freeze_json(
    value: object,
    path: str,
    *,
    _container_depth: int = 0,
    _active_ancestors: set[int] | None = None,
) -> JsonValue:
    if value is None or type(value) in {bool, str}:
        return value  # type: ignore[return-value]
    if type(value) is int:
        if abs(value) > MAX_SAFE_JSON_INTEGER:
            raise _error(
                ContractErrorCode.INVALID_VALUE,
                path,
                f"integer must be between {-MAX_SAFE_JSON_INTEGER} and {MAX_SAFE_JSON_INTEGER}",
            )
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _error(ContractErrorCode.INVALID_VALUE, path, "number must be finite")
        return value
    if isinstance(value, (Mapping, list, tuple)):
        next_depth = _container_depth + 1
        if next_depth > MAX_JSON_CONTAINER_DEPTH:
            raise _error(
                ContractErrorCode.INVALID_VALUE,
                path,
                f"JSON container nesting exceeds {MAX_JSON_CONTAINER_DEPTH}",
            )
        active = _active_ancestors if _active_ancestors is not None else set()
        identity = id(value)
        if identity in active:
            raise _error(ContractErrorCode.INVALID_VALUE, path, "cyclic JSON value")
        active.add(identity)
        try:
            if isinstance(value, Mapping):
                frozen: dict[str, JsonValue] = {}
                for key, item in value.items():
                    if type(key) is not str:
                        raise _error(
                            ContractErrorCode.INVALID_TYPE,
                            path,
                            "JSON object keys must be strings",
                        )
                    frozen[key] = _freeze_json(
                        item,
                        join_json_pointer(path, key),
                        _container_depth=next_depth,
                        _active_ancestors=active,
                    )
                return MappingProxyType(frozen)
            return tuple(
                _freeze_json(
                    item,
                    join_json_pointer(path, str(index)),
                    _container_depth=next_depth,
                    _active_ancestors=active,
                )
                for index, item in enumerate(value)
            )
        finally:
            active.remove(identity)
    raise _error(
        ContractErrorCode.INVALID_TYPE,
        path,
        "expected a JSON-compatible value",
    )


def _json_mapping(value: object, path: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise _error(ContractErrorCode.INVALID_TYPE, path, "expected a mapping")
    frozen = _freeze_json(value, path)
    assert isinstance(frozen, Mapping)
    return frozen


def _thaw_json(value: JsonValue) -> PlainJsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _parse_contract_list[ContractT](
    value: object,
    contract_type: type[ContractT],
    path: str,
) -> tuple[ContractT, ...]:
    items = _sequence(value, path)
    parsed: list[ContractT] = []
    for index, item in enumerate(items):
        item_path = join_json_pointer(path, str(index))
        if not isinstance(item, Mapping):
            raise _error(
                ContractErrorCode.INVALID_TYPE,
                item_path,
                f"expected a {contract_type.__name__} mapping",
            )
        try:
            parsed.append(contract_type.from_mapping(item))  # type: ignore[attr-defined]
        except ContractValidationError as exc:
            raise ContractValidationError(exc.code, f"{item_path}{exc.path}", exc.message) from exc
    return tuple(parsed)


def _parse_nested_contract[ContractT](
    value: object,
    contract_type: type[ContractT],
    path: str,
) -> ContractT:
    if not isinstance(value, Mapping):
        raise _error(
            ContractErrorCode.INVALID_TYPE,
            path,
            f"expected a {contract_type.__name__} mapping",
        )
    try:
        return contract_type.from_mapping(value)  # type: ignore[attr-defined,no-any-return]
    except ContractValidationError as exc:
        raise ContractValidationError(exc.code, f"{path}{exc.path}", exc.message) from exc


@dataclass(frozen=True, slots=True, kw_only=True)
class IntentAssumption:
    """An allowed assumption kept separate from user-supplied requirements."""

    id: str
    statement: str
    source: ValueSource
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "id", _text(self.id, "/id"))
        object.__setattr__(self, "statement", _text(self.statement, "/statement"))
        object.__setattr__(self, "source", _enum(self.source, ValueSource, "/source"))

    def to_mapping(self) -> dict[str, PlainJsonValue]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "statement": self.statement,
            "source": self.source.value,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            allowed={"schema_version", "id", "statement", "source"},
            required={"schema_version", "id", "statement", "source"},
        )
        return cls(
            schema_version=data["schema_version"],
            id=data["id"],
            statement=data["statement"],
            source=data["source"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Intent:
    """A provider-neutral statement of the user's requested CAD outcome."""

    id: str
    task_type: IntentKind
    goal: str
    input_project: str | None = None
    artifacts: tuple[str, ...] = ()
    requirements: Mapping[str, JsonValue] = field(default_factory=dict)
    allowed_assumptions: tuple[IntentAssumption, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "id", _text(self.id, "/id"))
        object.__setattr__(self, "task_type", _enum(self.task_type, IntentKind, "/task_type"))
        object.__setattr__(self, "goal", _text(self.goal, "/goal"))
        object.__setattr__(
            self, "input_project", _text(self.input_project, "/input_project", optional=True)
        )
        object.__setattr__(self, "artifacts", _text_tuple(self.artifacts, "/artifacts"))
        object.__setattr__(self, "requirements", _json_mapping(self.requirements, "/requirements"))
        object.__setattr__(
            self,
            "allowed_assumptions",
            _contract_tuple(self.allowed_assumptions, IntentAssumption, "/allowed_assumptions"),
        )
        object.__setattr__(
            self,
            "unresolved_questions",
            _text_tuple(self.unresolved_questions, "/unresolved_questions"),
        )

    def to_mapping(self) -> dict[str, PlainJsonValue]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "task_type": self.task_type.value,
            "goal": self.goal,
            "input_project": self.input_project,
            "artifacts": list(self.artifacts),
            "requirements": _thaw_json(self.requirements),
            "allowed_assumptions": [item.to_mapping() for item in self.allowed_assumptions],
            "unresolved_questions": list(self.unresolved_questions),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            allowed={
                "schema_version",
                "id",
                "task_type",
                "goal",
                "input_project",
                "artifacts",
                "requirements",
                "allowed_assumptions",
                "unresolved_questions",
            },
            required={"schema_version", "id", "task_type", "goal"},
        )
        return cls(
            schema_version=data["schema_version"],
            id=data["id"],
            task_type=data["task_type"],
            goal=data["goal"],
            input_project=data.get("input_project"),
            artifacts=data.get("artifacts", ()),
            requirements=data.get("requirements", {}),
            allowed_assumptions=_parse_contract_list(
                data.get("allowed_assumptions", ()),
                IntentAssumption,
                "/allowed_assumptions",
            ),
            unresolved_questions=data.get("unresolved_questions", ()),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptanceCriterion:
    """One deterministic condition used to accept or reject a candidate."""

    id: str
    kind: AcceptanceKind
    check: str
    target: str | None = None
    expected: JsonValue = None
    tolerance: int | float | None = None
    parameters: Mapping[str, JsonValue] = field(default_factory=dict)
    required: bool = True
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "id", _text(self.id, "/id"))
        object.__setattr__(self, "kind", _enum(self.kind, AcceptanceKind, "/kind"))
        object.__setattr__(self, "check", _text(self.check, "/check"))
        object.__setattr__(self, "target", _text(self.target, "/target", optional=True))
        object.__setattr__(self, "expected", _freeze_json(self.expected, "/expected"))
        tolerance = _number(self.tolerance, "/tolerance", optional=True)
        if tolerance is not None and tolerance < 0:
            raise _error(ContractErrorCode.INVALID_VALUE, "/tolerance", "must be non-negative")
        object.__setattr__(self, "tolerance", tolerance)
        object.__setattr__(self, "parameters", _json_mapping(self.parameters, "/parameters"))
        object.__setattr__(self, "required", _boolean(self.required, "/required"))

    def to_mapping(self) -> dict[str, PlainJsonValue]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": self.kind.value,
            "check": self.check,
            "target": self.target,
            "expected": _thaw_json(self.expected),
            "tolerance": self.tolerance,
            "parameters": _thaw_json(self.parameters),
            "required": self.required,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            allowed={
                "schema_version",
                "id",
                "kind",
                "check",
                "target",
                "expected",
                "tolerance",
                "parameters",
                "required",
            },
            required={"schema_version", "id", "kind", "check"},
        )
        return cls(
            schema_version=data["schema_version"],
            id=data["id"],
            kind=data["kind"],
            check=data["check"],
            target=data.get("target"),
            expected=data.get("expected"),
            tolerance=data.get("tolerance"),
            parameters=data.get("parameters", {}),
            required=data.get("required", True),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptanceSpec:
    """A versioned set of candidate acceptance criteria."""

    id: str
    criteria: tuple[AcceptanceCriterion, ...]
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "id", _text(self.id, "/id"))
        object.__setattr__(
            self,
            "criteria",
            _contract_tuple(self.criteria, AcceptanceCriterion, "/criteria"),
        )

    def to_mapping(self) -> dict[str, PlainJsonValue]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "criteria": [criterion.to_mapping() for criterion in self.criteria],
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            allowed={"schema_version", "id", "criteria"},
            required={"schema_version", "id", "criteria"},
        )
        _schema_version(data["schema_version"])
        return cls(
            schema_version=data["schema_version"],
            id=data["id"],
            criteria=_parse_contract_list(data["criteria"], AcceptanceCriterion, "/criteria"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelCommand:
    """One declarative semantic operation in a model program."""

    id: str
    op: str
    target: Mapping[str, JsonValue] = field(default_factory=dict)
    args: Mapping[str, JsonValue] = field(default_factory=dict)
    preserve: tuple[str, ...] = ()
    source: ValueSource
    depends_on: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "id", _text(self.id, "/id"))
        object.__setattr__(self, "op", _text(self.op, "/op"))
        object.__setattr__(self, "target", _json_mapping(self.target, "/target"))
        object.__setattr__(self, "args", _json_mapping(self.args, "/args"))
        object.__setattr__(self, "preserve", _text_tuple(self.preserve, "/preserve"))
        object.__setattr__(self, "source", _enum(self.source, ValueSource, "/source"))
        object.__setattr__(self, "depends_on", _text_tuple(self.depends_on, "/depends_on"))

    def to_mapping(self) -> dict[str, PlainJsonValue]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "op": self.op,
            "target": _thaw_json(self.target),
            "args": _thaw_json(self.args),
            "preserve": list(self.preserve),
            "source": self.source.value,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            allowed={
                "schema_version",
                "id",
                "op",
                "target",
                "args",
                "preserve",
                "source",
                "depends_on",
            },
            required={"schema_version", "id", "op", "source"},
        )
        return cls(
            schema_version=data["schema_version"],
            id=data["id"],
            op=data["op"],
            target=data.get("target", {}),
            args=data.get("args", {}),
            preserve=data.get("preserve", ()),
            source=data["source"],
            depends_on=data.get("depends_on", ()),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelProgram:
    """A versioned sequence of declarative CAD operations and acceptance rules."""

    task_id: str
    base_revision: str
    operations: tuple[ModelCommand, ...]
    acceptance: AcceptanceSpec
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "task_id", _text(self.task_id, "/task_id"))
        object.__setattr__(self, "base_revision", _text(self.base_revision, "/base_revision"))
        object.__setattr__(
            self,
            "operations",
            _contract_tuple(self.operations, ModelCommand, "/operations"),
        )
        if not isinstance(self.acceptance, AcceptanceSpec):
            raise _error(
                ContractErrorCode.INVALID_TYPE,
                "/acceptance",
                "expected AcceptanceSpec",
            )

    def to_mapping(self) -> dict[str, PlainJsonValue]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "base_revision": self.base_revision,
            "operations": [operation.to_mapping() for operation in self.operations],
            "acceptance": self.acceptance.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            allowed={
                "schema_version",
                "task_id",
                "base_revision",
                "operations",
                "acceptance",
            },
            required={
                "schema_version",
                "task_id",
                "base_revision",
                "operations",
                "acceptance",
            },
        )
        _schema_version(data["schema_version"])
        return cls(
            schema_version=data["schema_version"],
            task_id=data["task_id"],
            base_revision=data["base_revision"],
            operations=_parse_contract_list(data["operations"], ModelCommand, "/operations"),
            acceptance=_parse_nested_contract(data["acceptance"], AcceptanceSpec, "/acceptance"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionEvidence:
    """A typed fact, artifact reference, observation, or assertion."""

    id: str
    kind: EvidenceKind
    name: str
    value: JsonValue = None
    operation_id: str | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "id", _text(self.id, "/id"))
        object.__setattr__(self, "kind", _enum(self.kind, EvidenceKind, "/kind"))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        object.__setattr__(self, "value", _freeze_json(self.value, "/value"))
        object.__setattr__(
            self,
            "operation_id",
            _text(self.operation_id, "/operation_id", optional=True),
        )
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, "/metadata"))

    def to_mapping(self) -> dict[str, PlainJsonValue]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": self.kind.value,
            "name": self.name,
            "value": _thaw_json(self.value),
            "operation_id": self.operation_id,
            "metadata": _thaw_json(self.metadata),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            allowed={
                "schema_version",
                "id",
                "kind",
                "name",
                "value",
                "operation_id",
                "metadata",
            },
            required={"schema_version", "id", "kind", "name"},
        )
        return cls(
            schema_version=data["schema_version"],
            id=data["id"],
            kind=data["kind"],
            name=data["name"],
            value=data.get("value"),
            operation_id=data.get("operation_id"),
            metadata=data.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class StepError:
    """Normalized structured error attached to a failed step result."""

    category: ErrorCategory
    code: str
    message: str
    retryable: bool
    needs_input: bool
    related_objects: tuple[str, ...]
    diagnostic_artifacts: tuple[str, ...]
    operation_id: str | None = None
    details: Mapping[str, JsonValue] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "category", _enum(self.category, ErrorCategory, "/category"))
        object.__setattr__(self, "code", _text(self.code, "/code"))
        object.__setattr__(self, "message", _text(self.message, "/message"))
        object.__setattr__(self, "retryable", _boolean(self.retryable, "/retryable"))
        object.__setattr__(self, "needs_input", _boolean(self.needs_input, "/needs_input"))
        object.__setattr__(
            self,
            "related_objects",
            _text_tuple(self.related_objects, "/related_objects"),
        )
        object.__setattr__(
            self,
            "diagnostic_artifacts",
            _text_tuple(self.diagnostic_artifacts, "/diagnostic_artifacts"),
        )
        object.__setattr__(
            self,
            "operation_id",
            _text(self.operation_id, "/operation_id", optional=True),
        )
        object.__setattr__(self, "details", _json_mapping(self.details, "/details"))

    def to_mapping(self) -> dict[str, PlainJsonValue]:
        return {
            "schema_version": self.schema_version,
            "category": self.category.value,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "needs_input": self.needs_input,
            "related_objects": list(self.related_objects),
            "diagnostic_artifacts": list(self.diagnostic_artifacts),
            "operation_id": self.operation_id,
            "details": _thaw_json(self.details),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            allowed={
                "schema_version",
                "category",
                "code",
                "message",
                "retryable",
                "needs_input",
                "related_objects",
                "diagnostic_artifacts",
                "operation_id",
                "details",
            },
            required={
                "schema_version",
                "category",
                "code",
                "message",
                "retryable",
                "needs_input",
                "related_objects",
                "diagnostic_artifacts",
            },
        )
        return cls(
            schema_version=data["schema_version"],
            category=data["category"],
            code=data["code"],
            message=data["message"],
            retryable=data["retryable"],
            needs_input=data["needs_input"],
            related_objects=data["related_objects"],
            diagnostic_artifacts=data["diagnostic_artifacts"],
            operation_id=data.get("operation_id"),
            details=data.get("details", {}),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class StepResult:
    """Normalized operation result envelope used by the workflow core."""

    ok: bool
    value: JsonValue
    elapsed_ms: int | float
    operation_id: str | None = None
    revision: str | None = None
    facts: Mapping[str, JsonValue] = field(default_factory=dict)
    artifacts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    evidence: tuple[ExecutionEvidence, ...] = ()
    error: StepError | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "ok", _boolean(self.ok, "/ok"))
        object.__setattr__(self, "value", _freeze_json(self.value, "/value"))
        elapsed_ms = _number(self.elapsed_ms, "/elapsed_ms")
        assert elapsed_ms is not None
        if elapsed_ms < 0:
            raise _error(ContractErrorCode.INVALID_VALUE, "/elapsed_ms", "must be non-negative")
        object.__setattr__(self, "elapsed_ms", elapsed_ms)
        object.__setattr__(
            self,
            "operation_id",
            _text(self.operation_id, "/operation_id", optional=True),
        )
        object.__setattr__(self, "revision", _text(self.revision, "/revision", optional=True))
        object.__setattr__(self, "facts", _json_mapping(self.facts, "/facts"))
        object.__setattr__(self, "artifacts", _text_tuple(self.artifacts, "/artifacts"))
        object.__setattr__(self, "warnings", _text_tuple(self.warnings, "/warnings"))
        object.__setattr__(
            self,
            "evidence",
            _contract_tuple(self.evidence, ExecutionEvidence, "/evidence"),
        )
        if self.error is not None and not isinstance(self.error, StepError):
            raise _error(ContractErrorCode.INVALID_TYPE, "/error", "expected StepError or null")
        if self.ok and self.error is not None:
            raise _error(
                ContractErrorCode.INVALID_VALUE,
                "/error",
                "successful result must not contain an error",
            )
        if not self.ok and self.error is None:
            raise _error(
                ContractErrorCode.MISSING_FIELD,
                "/error",
                "failed result must contain an error",
            )

    def to_mapping(self) -> dict[str, PlainJsonValue]:
        return {
            "schema_version": self.schema_version,
            "ok": self.ok,
            "value": _thaw_json(self.value),
            "elapsed_ms": self.elapsed_ms,
            "operation_id": self.operation_id,
            "revision": self.revision,
            "facts": _thaw_json(self.facts),
            "artifacts": list(self.artifacts),
            "warnings": list(self.warnings),
            "evidence": [item.to_mapping() for item in self.evidence],
            "error": self.error.to_mapping() if self.error is not None else None,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            allowed={
                "schema_version",
                "ok",
                "value",
                "elapsed_ms",
                "operation_id",
                "revision",
                "facts",
                "artifacts",
                "warnings",
                "evidence",
                "error",
            },
            required={"schema_version", "ok", "value", "elapsed_ms"},
        )
        _schema_version(data["schema_version"])
        error_value = data.get("error")
        error = (
            None
            if error_value is None
            else _parse_nested_contract(error_value, StepError, "/error")
        )
        return cls(
            schema_version=data["schema_version"],
            ok=data["ok"],
            value=data["value"],
            elapsed_ms=data["elapsed_ms"],
            operation_id=data.get("operation_id"),
            revision=data.get("revision"),
            facts=data.get("facts", {}),
            artifacts=data.get("artifacts", ()),
            warnings=data.get("warnings", ()),
            evidence=_parse_contract_list(data.get("evidence", ()), ExecutionEvidence, "/evidence"),
            error=error,
        )
