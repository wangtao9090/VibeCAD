"""Deterministic, side-effect-free validation for declarative model programs.

Validation in this module is deliberately separated from execution.  It only
checks an already constructed :class:`~vibecad.workflow.contracts.ModelProgram`
against immutable operation metadata and produces a sealed capability for the
execution layer.  It never resolves or invokes a handler and never imports a
CAD, MCP, model-provider, filesystem, or network integration.
"""

from __future__ import annotations

import heapq
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Self

from vibecad.execution.registry import (
    DEFAULT_OPERATION_REGISTRY,
    ExecutionProfile,
    FieldMetadata,
    OperationMetadata,
    OperationRegistry,
    ResultSlotMetadata,
    RiskClass,
    ValueShape,
    _matches_value_shape,
)
from vibecad.workflow.contracts import ModelCommand, ModelProgram, ValueSource
from vibecad.workflow.errors import (
    SCHEMA_VERSION,
    is_canonical_json_pointer,
    join_json_pointer,
)

DEFAULT_MAX_COMMANDS = 64
_MAX_ERROR_MESSAGE_LENGTH = 256
_MAX_ERROR_PATH_LENGTH = 512
_INVALID_MESSAGE = "message must be bounded printable single-line text"
_INVALID_PATH = "path must be a bounded printable canonical RFC 6901 JSON Pointer"
_VALIDATED_PROGRAM_SEAL = object()


class ProgramErrorCode(StrEnum):
    """Stable machine-readable reasons a model program is rejected."""

    INVALID_INPUT = "invalid_input"
    INVALID_CONFIGURATION = "invalid_configuration"
    EMPTY_PROGRAM = "empty_program"
    BUDGET_EXCEEDED = "budget_exceeded"
    DUPLICATE_COMMAND_ID = "duplicate_command_id"
    DUPLICATE_DEPENDENCY = "duplicate_dependency"
    SELF_DEPENDENCY = "self_dependency"
    UNKNOWN_DEPENDENCY = "unknown_dependency"
    DEPENDENCY_CYCLE = "dependency_cycle"
    UNKNOWN_OPERATION = "unknown_operation"
    MISSING_FIELD = "missing_field"
    EXTRA_FIELD = "extra_field"
    INVALID_VALUE_SHAPE = "invalid_value_shape"
    INVALID_RESULT_REFERENCE = "invalid_result_reference"
    INVALID_ERROR_RECORD = "invalid_error_record"
    UNSUPPORTED_VERSION = "unsupported_version"


def _is_safe_message(value: object) -> bool:
    return (
        type(value) is str
        and bool(value.strip())
        and len(value) <= _MAX_ERROR_MESSAGE_LENGTH
        and value.isprintable()
        and len(value.splitlines()) == 1
    )


def _is_safe_path(value: object) -> bool:
    return (
        type(value) is str
        and len(value) <= _MAX_ERROR_PATH_LENGTH
        and (value == "" or value.isprintable())
        and len(value.splitlines()) <= 1
        and is_canonical_json_pointer(value)
    )


class ProgramValidationError(ValueError):
    """A strict schema-v1 program failure with a canonical input path."""

    def __init__(
        self,
        code: ProgramErrorCode,
        path: str,
        message: str,
        *,
        schema_version: int = SCHEMA_VERSION,
    ) -> None:
        if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be exactly {SCHEMA_VERSION}")
        if not isinstance(code, ProgramErrorCode):
            raise TypeError("code must be a ProgramErrorCode")
        if not _is_safe_path(path):
            raise ValueError(_INVALID_PATH)
        if not _is_safe_message(message):
            raise ValueError(_INVALID_MESSAGE)
        self.schema_version = schema_version
        self.code = code
        self.path = path
        self.message = message
        rendered_path = json.dumps(path)
        rendered_message = json.dumps(message)
        super().__init__(f"program validation error at {rendered_path}: {rendered_message}")

    def to_mapping(self) -> dict[str, int | str]:
        """Return the strict schema-v1 JSON-compatible error record."""

        return {
            "schema_version": self.schema_version,
            "code": self.code.value,
            "path": self.path,
            "message": self.message,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        """Parse a strict error record without reflecting malformed values."""

        if not isinstance(value, Mapping):
            raise _invalid_error_record("error record must be a mapping")
        try:
            keys = tuple(value)
        except ProgramValidationError:
            raise
        except Exception as exc:
            raise _invalid_error_record("error record keys could not be read") from exc
        if not all(type(key) is str for key in keys):
            raise _invalid_error_record("error record keys must be strings")
        required = {"schema_version", "code", "path", "message"}
        if set(keys) != required or len(keys) != len(required):
            raise _invalid_error_record("error record fields must exactly match schema v1")
        try:
            schema_version = value["schema_version"]
            raw_code = value["code"]
            path = value["path"]
            message = value["message"]
        except ProgramValidationError:
            raise
        except Exception as exc:
            raise _invalid_error_record("error record values could not be read") from exc

        if type(schema_version) is not int:
            raise _invalid_error_record("schema_version must be an integer")
        if schema_version != SCHEMA_VERSION:
            raise cls(
                ProgramErrorCode.UNSUPPORTED_VERSION,
                "/schema_version",
                "unsupported error-record schema version",
            )
        if type(raw_code) is not str:
            raise _invalid_error_record("error code must be a string")
        try:
            code = ProgramErrorCode(raw_code)
        except ValueError as exc:
            raise _invalid_error_record("error code is not supported") from exc
        if not _is_safe_path(path):
            raise _invalid_error_record(_INVALID_PATH)
        if not _is_safe_message(message):
            raise _invalid_error_record(_INVALID_MESSAGE)
        assert isinstance(path, str)
        assert isinstance(message, str)
        return cls(code, path, message, schema_version=schema_version)


def _invalid_error_record(message: str) -> ProgramValidationError:
    return ProgramValidationError(ProgramErrorCode.INVALID_ERROR_RECORD, "", message)


def _failure(code: ProgramErrorCode, path: str, message: str) -> ProgramValidationError:
    return ProgramValidationError(code, path, message)


@dataclass(frozen=True, slots=True)
class BoundResultRef:
    """Validated reference to one concrete result slot in this program run."""

    command_id: str
    slot: str
    value_shape: ValueShape


@dataclass(frozen=True, slots=True)
class BoundCommand:
    """One validated command in deterministic execution order."""

    id: str
    operation: str
    handler_name: str
    handler_kwargs: Mapping[str, Any] = field(repr=False)
    depends_on: tuple[str, ...]
    preserve: tuple[str, ...]
    source: ValueSource
    risk_class: RiskClass
    evidence_required: bool
    execution_profiles: tuple[ExecutionProfile, ...] = (ExecutionProfile.HEADLESS,)
    result_slots: tuple[ResultSlotMetadata, ...] = ()

    def __post_init__(self) -> None:
        frozen_kwargs = {
            key: _freeze_bound_value(value) for key, value in self.handler_kwargs.items()
        }
        object.__setattr__(self, "handler_kwargs", MappingProxyType(frozen_kwargs))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        object.__setattr__(self, "preserve", tuple(self.preserve))
        object.__setattr__(self, "execution_profiles", tuple(self.execution_profiles))
        object.__setattr__(self, "result_slots", tuple(self.result_slots))


def _freeze_bound_value(value: object) -> object:
    """Defensively freeze caller-owned JSON containers in bound data."""

    if type(value) is BoundResultRef:
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_bound_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_bound_value(item) for item in value)
    return value


class ValidatedProgram:
    """Authentic, immutable capability created only after full validation.

    Normal construction is intentionally disabled.  The execution adapter can
    use :meth:`require_authentic` before trusting the bound handler metadata.
    """

    __slots__ = ("_commands", "_max_commands", "_program", "_registry", "_seal")

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        del args, kwargs
        raise TypeError("ValidatedProgram objects are created by validate_model_program")

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("ValidatedProgram is immutable")

    @property
    def program(self) -> ModelProgram:
        self.require_authentic()
        return self._program

    @property
    def commands(self) -> tuple[BoundCommand, ...]:
        self.require_authentic()
        return self._commands

    def require_authentic(self) -> None:
        """Reject objects that did not come from successful validation."""

        try:
            seal = object.__getattribute__(self, "_seal")
            registry = object.__getattribute__(self, "_registry")
            max_commands = object.__getattribute__(self, "_max_commands")
        except AttributeError as exc:
            raise TypeError("validated program capability is not authentic") from exc
        if (
            type(self) is not ValidatedProgram
            or seal is not _VALIDATED_PROGRAM_SEAL
            or type(registry) is not OperationRegistry
            or type(max_commands) is not int
            or max_commands <= 0
            or max_commands > DEFAULT_MAX_COMMANDS
        ):
            raise TypeError("validated program capability is not authentic")

    def _revalidate_source(self) -> Self:
        """Rebind the sealed source with the exact authority used originally."""

        self.require_authentic()
        return validate_model_program(
            self._program,
            registry=self._registry,
            max_commands=self._max_commands,
        )

    def __len__(self) -> int:
        return len(self.commands)

    def __iter__(self):
        return iter(self.commands)

    def __copy__(self) -> Self:
        self.require_authentic()
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        del memo
        self.require_authentic()
        return self

    def __reduce__(self):
        raise TypeError("ValidatedProgram capabilities cannot be serialized")

    def __repr__(self) -> str:
        self.require_authentic()
        return f"ValidatedProgram(commands={len(self._commands)})"


def _make_validated_program(
    program: ModelProgram,
    commands: tuple[BoundCommand, ...],
    registry: OperationRegistry,
    max_commands: int,
) -> ValidatedProgram:
    result = object.__new__(ValidatedProgram)
    object.__setattr__(result, "_program", program)
    object.__setattr__(result, "_commands", commands)
    object.__setattr__(result, "_registry", registry)
    object.__setattr__(result, "_max_commands", max_commands)
    object.__setattr__(result, "_seal", _VALIDATED_PROGRAM_SEAL)
    return result


def _operation_path(index: int, *tokens: str) -> str:
    path = join_json_pointer("/operations", str(index))
    for token in tokens:
        path = join_json_pointer(path, token)
    return path


def _validate_configuration(
    program: object,
    registry: object,
    max_commands: object,
) -> tuple[ModelProgram, OperationRegistry, int]:
    if type(program) is not ModelProgram:
        raise _failure(
            ProgramErrorCode.INVALID_INPUT,
            "",
            "program must be a ModelProgram",
        )
    if type(registry) is not OperationRegistry:
        raise _failure(
            ProgramErrorCode.INVALID_CONFIGURATION,
            "/registry",
            "registry must be an immutable OperationRegistry",
        )
    try:
        registry_operations = registry.operations
    except Exception as exc:
        raise _failure(
            ProgramErrorCode.INVALID_CONFIGURATION,
            "/registry",
            "registry metadata could not be read",
        ) from exc
    if type(registry_operations) is not MappingProxyType or not all(
        type(name) is str and type(metadata) is OperationMetadata
        for name, metadata in registry_operations.items()
    ):
        raise _failure(
            ProgramErrorCode.INVALID_CONFIGURATION,
            "/registry",
            "registry must contain immutable operation metadata",
        )
    if type(max_commands) is not int or max_commands <= 0 or max_commands > DEFAULT_MAX_COMMANDS:
        raise _failure(
            ProgramErrorCode.INVALID_CONFIGURATION,
            "/max_commands",
            f"max_commands must be between 1 and {DEFAULT_MAX_COMMANDS}",
        )
    if type(program.operations) is not tuple:
        raise _failure(
            ProgramErrorCode.INVALID_INPUT,
            "/operations",
            "program operations must be an immutable command tuple",
        )
    return program, registry, max_commands


def _validate_command_integrity(command: ModelCommand, index: int) -> None:
    """Defend the boundary even if a frozen C1 object was forged in memory."""

    if type(command.id) is not str or not command.id.strip():
        raise _failure(
            ProgramErrorCode.INVALID_INPUT,
            _operation_path(index, "id"),
            "command id must be a nonblank string",
        )
    if type(command.op) is not str or not command.op.strip():
        raise _failure(
            ProgramErrorCode.INVALID_INPUT,
            _operation_path(index, "op"),
            "operation must be a nonblank string",
        )
    if type(command.target) is not MappingProxyType:
        raise _failure(
            ProgramErrorCode.INVALID_INPUT,
            _operation_path(index, "target"),
            "target must be an immutable mapping",
        )
    if type(command.args) is not MappingProxyType:
        raise _failure(
            ProgramErrorCode.INVALID_INPUT,
            _operation_path(index, "args"),
            "args must be an immutable mapping",
        )
    if type(command.depends_on) is not tuple or not all(
        type(dependency) is str and bool(dependency.strip()) for dependency in command.depends_on
    ):
        raise _failure(
            ProgramErrorCode.INVALID_INPUT,
            _operation_path(index, "depends_on"),
            "depends_on must be an immutable tuple of nonblank strings",
        )
    if type(command.preserve) is not tuple or not all(
        type(item) is str and bool(item.strip()) for item in command.preserve
    ):
        raise _failure(
            ProgramErrorCode.INVALID_INPUT,
            _operation_path(index, "preserve"),
            "preserve must be an immutable tuple of nonblank strings",
        )
    if not isinstance(command.source, ValueSource):
        raise _failure(
            ProgramErrorCode.INVALID_INPUT,
            _operation_path(index, "source"),
            "source must be a ValueSource",
        )


def _validate_graph(
    operations: tuple[ModelCommand, ...],
) -> tuple[int, ...]:
    indices_by_id: dict[str, int] = {}
    for index, command in enumerate(operations):
        if type(command) is not ModelCommand:
            raise _failure(
                ProgramErrorCode.INVALID_INPUT,
                _operation_path(index),
                "operation must be a ModelCommand",
            )
        _validate_command_integrity(command, index)
        if command.id in indices_by_id:
            raise _failure(
                ProgramErrorCode.DUPLICATE_COMMAND_ID,
                _operation_path(index, "id"),
                "command id is already declared",
            )
        indices_by_id[command.id] = index

        seen_dependencies: set[str] = set()
        for dependency_index, dependency in enumerate(command.depends_on):
            path = _operation_path(index, "depends_on", str(dependency_index))
            if dependency in seen_dependencies:
                raise _failure(
                    ProgramErrorCode.DUPLICATE_DEPENDENCY,
                    path,
                    "dependency is listed more than once",
                )
            seen_dependencies.add(dependency)
            if dependency == command.id:
                raise _failure(
                    ProgramErrorCode.SELF_DEPENDENCY,
                    path,
                    "command cannot depend on itself",
                )

    for index, command in enumerate(operations):
        for dependency_index, dependency in enumerate(command.depends_on):
            if dependency not in indices_by_id:
                raise _failure(
                    ProgramErrorCode.UNKNOWN_DEPENDENCY,
                    _operation_path(index, "depends_on", str(dependency_index)),
                    "dependency does not name a command in this program",
                )

    indegree = [len(command.depends_on) for command in operations]
    dependents: list[list[int]] = [[] for _ in operations]
    for dependent_index, command in enumerate(operations):
        for dependency in command.depends_on:
            dependents[indices_by_id[dependency]].append(dependent_index)

    ready = [index for index, degree in enumerate(indegree) if degree == 0]
    heapq.heapify(ready)
    ordered: list[int] = []
    while ready:
        index = heapq.heappop(ready)
        ordered.append(index)
        for dependent_index in dependents[index]:
            indegree[dependent_index] -= 1
            if indegree[dependent_index] == 0:
                heapq.heappush(ready, dependent_index)

    if len(ordered) != len(operations):
        raise _failure(
            ProgramErrorCode.DEPENDENCY_CYCLE,
            "/operations",
            "program dependency graph contains a cycle",
        )
    return tuple(ordered)


def _dependency_closures(
    operations: tuple[ModelCommand, ...],
) -> Mapping[str, frozenset[str]]:
    commands = {command.id: command for command in operations}
    memo: dict[str, frozenset[str]] = {}

    def visit(command_id: str) -> frozenset[str]:
        known = memo.get(command_id)
        if known is not None:
            return known
        dependencies: set[str] = set()
        for dependency in commands[command_id].depends_on:
            dependencies.add(dependency)
            dependencies.update(visit(dependency))
        result = frozenset(dependencies)
        memo[command_id] = result
        return result

    for command in operations:
        visit(command.id)
    return MappingProxyType(memo)


def _invalid_result_reference(path: str) -> ProgramValidationError:
    return _failure(
        ProgramErrorCode.INVALID_RESULT_REFERENCE,
        path,
        "result reference is invalid or unavailable",
    )


def _bind_result_reference(
    value: object,
    metadata: FieldMetadata,
    *,
    path: str,
    dependency_ids: frozenset[str],
    prior_command_ids: frozenset[str],
    operation_metadata: Mapping[str, OperationMetadata],
) -> BoundResultRef:
    if not _matches_value_shape(value, ValueShape.RESULT_REF):
        raise _invalid_result_reference(path)
    assert isinstance(value, Mapping)
    try:
        command_id = value["command_id"]
        slot_name = value["slot"]
    except Exception:
        raise _invalid_result_reference(path) from None
    if type(command_id) is not str or type(slot_name) is not str:
        raise _invalid_result_reference(path)
    if command_id not in dependency_ids or command_id not in prior_command_ids:
        raise _invalid_result_reference(path)
    producer = operation_metadata.get(command_id)
    if producer is None:
        raise _invalid_result_reference(path)
    slot = next((item for item in producer.result_slots if item.name == slot_name), None)
    if slot is None or slot.value_shape is not metadata.referenced_value_shape:
        raise _invalid_result_reference(path)
    return BoundResultRef(
        command_id=command_id,
        slot=slot_name,
        value_shape=slot.value_shape,
    )


def _bind_field_group(
    *,
    values: Mapping[str, Any],
    metadata: tuple[FieldMetadata, ...],
    index: int,
    group: str,
    destination: dict[str, Any],
    dependency_ids: frozenset[str],
    prior_command_ids: frozenset[str],
    operation_metadata: Mapping[str, OperationMetadata],
) -> None:
    path = _operation_path(index, group)
    if not all(type(name) is str for name in values):
        raise _failure(
            ProgramErrorCode.INVALID_INPUT,
            path,
            "field names must be strings",
        )
    allowed = {item.name for item in metadata}
    for item in metadata:
        if item.required and item.name not in values:
            raise _failure(
                ProgramErrorCode.MISSING_FIELD,
                join_json_pointer(path, item.name),
                "required registered field is missing",
            )
    extra = sorted(name for name in values if name not in allowed)
    if extra:
        extra_path = join_json_pointer(path, extra[0])
        raise _failure(
            ProgramErrorCode.EXTRA_FIELD,
            extra_path if _is_safe_path(extra_path) else path,
            "field group contains an unregistered field",
        )
    for item in metadata:
        if item.name not in values:
            continue
        value = values[item.name]
        field_path = join_json_pointer(path, item.name)
        if item.value_shape is ValueShape.RESULT_REF:
            destination[item.handler_parameter] = _bind_result_reference(
                value,
                item,
                path=field_path,
                dependency_ids=dependency_ids,
                prior_command_ids=prior_command_ids,
                operation_metadata=operation_metadata,
            )
            continue
        if not _matches_value_shape(
            value,
            item.value_shape,
            enum_values=item.enum_values,
            allowed_units=item.allowed_units,
        ):
            raise _failure(
                ProgramErrorCode.INVALID_VALUE_SHAPE,
                field_path,
                "field value does not match the registered shape",
            )
        destination[item.handler_parameter] = value


def _bind_command(
    command: ModelCommand,
    metadata: OperationMetadata,
    index: int,
    dependency_ids: frozenset[str],
    prior_command_ids: frozenset[str],
    operation_metadata: Mapping[str, OperationMetadata],
) -> BoundCommand:
    kwargs: dict[str, Any] = {}
    _bind_field_group(
        values=command.target,
        metadata=metadata.target_fields,
        index=index,
        group="target",
        destination=kwargs,
        dependency_ids=dependency_ids,
        prior_command_ids=prior_command_ids,
        operation_metadata=operation_metadata,
    )
    _bind_field_group(
        values=command.args,
        metadata=metadata.argument_fields,
        index=index,
        group="args",
        destination=kwargs,
        dependency_ids=dependency_ids,
        prior_command_ids=prior_command_ids,
        operation_metadata=operation_metadata,
    )
    return BoundCommand(
        id=command.id,
        operation=command.op,
        handler_name=metadata.handler_name,
        handler_kwargs=MappingProxyType(kwargs),
        depends_on=command.depends_on,
        preserve=command.preserve,
        source=command.source,
        risk_class=metadata.risk_class,
        evidence_required=metadata.evidence_required,
        execution_profiles=metadata.execution_profiles,
        result_slots=metadata.result_slots,
    )


def validate_model_program(
    program: ModelProgram,
    *,
    registry: OperationRegistry = DEFAULT_OPERATION_REGISTRY,
    max_commands: int = DEFAULT_MAX_COMMANDS,
) -> ValidatedProgram:
    """Validate and bind *program* without resolving or invoking any handler."""

    checked_program, checked_registry, checked_budget = _validate_configuration(
        program,
        registry,
        max_commands,
    )
    operations = checked_program.operations
    if not operations:
        raise _failure(
            ProgramErrorCode.EMPTY_PROGRAM,
            "/operations",
            "program must contain at least one command",
        )
    if len(operations) > checked_budget:
        raise _failure(
            ProgramErrorCode.BUDGET_EXCEEDED,
            "/operations",
            "program exceeds the configured command budget",
        )

    order = _validate_graph(operations)
    dependency_closures = _dependency_closures(operations)
    operation_metadata: dict[str, OperationMetadata] = {}
    for index, command in enumerate(operations):
        metadata = checked_registry.operations.get(command.op)
        if metadata is None:
            raise _failure(
                ProgramErrorCode.UNKNOWN_OPERATION,
                _operation_path(index, "op"),
                "operation is not registered",
            )
        operation_metadata[command.id] = metadata
    frozen_operation_metadata = MappingProxyType(operation_metadata)

    bound_by_index: list[BoundCommand] = []
    for index, command in enumerate(operations):
        metadata = operation_metadata[command.id]
        bound_by_index.append(
            _bind_command(
                command,
                metadata,
                index,
                dependency_closures[command.id],
                frozenset(item.id for item in operations[:index]),
                frozen_operation_metadata,
            )
        )

    commands = tuple(bound_by_index[index] for index in order)
    return _make_validated_program(
        checked_program,
        commands,
        checked_registry,
        checked_budget,
    )


__all__ = [
    "DEFAULT_MAX_COMMANDS",
    "BoundCommand",
    "BoundResultRef",
    "ProgramErrorCode",
    "ProgramValidationError",
    "ValidatedProgram",
    "validate_model_program",
]
