"""Immutable metadata for the Phase-1 semantic CAD operation allowlist.

This module describes how provider-neutral program fields bind to an injected
handler name.  It intentionally contains no callables, import paths, source
text, shell commands, filesystem/network behavior, or CAD imports.  Validation
and execution are separate later stages.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Self

from vibecad.execution.selectors import SelectorV1
from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER, SCHEMA_VERSION

_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_OBJECT_ID = re.compile(r"^object_[0-9a-f]{32}$")
_MAX_NAME_LENGTH = 64
_MAX_VALUE_TEXT_LENGTH = 256
_MAX_ERROR_MESSAGE_LENGTH = 256
_MAX_DESCRIPTION_BYTES = 256
_INVALID_ERROR_MESSAGE = "message must be bounded printable single-line text"
_UNSAFE_NAME_TOKENS = frozenset(
    {
        "awk",
        "bash",
        "callable",
        "cmd",
        "code",
        "command",
        "cscript",
        "csh",
        "dash",
        "eval",
        "exec",
        "execute",
        "fish",
        "fork",
        "import",
        "javascript",
        "ksh",
        "lua",
        "macro",
        "osascript",
        "perl",
        "php",
        "powershell",
        "python",
        "pwsh",
        "ruby",
        "script",
        "sh",
        "shell",
        "spawn",
        "subprocess",
        "tcsh",
        "wscript",
        "zsh",
    }
)


def _is_safe_error_message(value: object) -> bool:
    return (
        type(value) is str
        and bool(value.strip())
        and len(value) <= _MAX_ERROR_MESSAGE_LENGTH
        and value.isprintable()
        and len(value.splitlines()) == 1
    )


class RiskClass(StrEnum):
    """Maximum side-effect class of a registered semantic operation."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"


class ExecutionProfile(StrEnum):
    """Closed FreeCAD execution surfaces; routing never silently downgrades."""

    HEADLESS = "headless"
    OFFSCREEN_GUI = "offscreen_gui"
    INTERACTIVE_GUI = "interactive_gui"


class ValueShape(StrEnum):
    """Closed value shapes available to program and result-slot validation."""

    NONBLANK_STRING = "nonblank_string"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    FINITE_NUMBER = "finite_number"
    POSITIVE_NUMBER = "positive_number"
    ENUM = "enum"
    VECTOR2 = "vector2"
    VECTOR3 = "vector3"
    QUANTITY = "quantity"
    RESULT_REF = "result_ref"
    OBJECT_SELECTOR = "object_selector"
    OBJECT_ID = "object_id"
    ENTITY_TARGET = "entity_target"
    ANGLE_DEGREES = "angle_degrees"


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceBudget:
    """Static per-operation limits declared for trusted execution adapters."""

    max_runtime_ms: int = 30_000
    max_created_objects: int = 1
    max_result_bytes: int = 65_536

    def __post_init__(self) -> None:
        values = (
            ("max_runtime_ms", self.max_runtime_ms, False),
            ("max_created_objects", self.max_created_objects, True),
            ("max_result_bytes", self.max_result_bytes, False),
        )
        for name, value, allow_zero in values:
            if (
                type(value) is not int
                or value > MAX_SAFE_JSON_INTEGER
                or value < (0 if allow_zero else 1)
            ):
                raise RegistryError(
                    RegistryErrorCode.INVALID_METADATA,
                    f"{name} must be a bounded integer budget",
                )


def _is_safe_value_text(value: object) -> bool:
    return (
        type(value) is str
        and bool(value.strip())
        and len(value) <= _MAX_VALUE_TEXT_LENGTH
        and value.isprintable()
        and len(value.splitlines()) == 1
    )


def _is_finite_number(value: object, *, positive: bool) -> bool:
    if type(value) not in {int, float}:
        return False
    if type(value) is int and abs(value) > MAX_SAFE_JSON_INTEGER:
        return False
    return math.isfinite(value) and (not positive or value > 0)


def _snapshot_strict_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        keys = tuple(value)
        if any(type(key) is not str for key in keys) or len(set(keys)) != len(keys):
            return None
        return MappingProxyType({key: value[key] for key in keys})
    except Exception:
        return None


def _matches_value_shape(
    value: object,
    shape: ValueShape,
    *,
    enum_values: tuple[str, ...] = (),
    allowed_units: tuple[str, ...] = (),
) -> bool:
    """Match one already-frozen JSON value against a closed registry shape."""

    if shape is ValueShape.NONBLANK_STRING:
        return _is_safe_value_text(value)
    if shape is ValueShape.BOOLEAN:
        return type(value) is bool
    if shape is ValueShape.INTEGER:
        return type(value) is int and abs(value) <= MAX_SAFE_JSON_INTEGER
    if shape is ValueShape.FINITE_NUMBER:
        return _is_finite_number(value, positive=False)
    if shape is ValueShape.POSITIVE_NUMBER:
        return _is_finite_number(value, positive=True)
    if shape is ValueShape.ANGLE_DEGREES:
        return _is_finite_number(value, positive=False) and value != 0 and -360 < value < 360
    if shape is ValueShape.ENUM:
        return type(value) is str and value in enum_values
    if shape in {ValueShape.VECTOR2, ValueShape.VECTOR3}:
        expected = 2 if shape is ValueShape.VECTOR2 else 3
        return (
            type(value) is tuple
            and len(value) == expected
            and all(_is_finite_number(component, positive=False) for component in value)
        )
    if shape is ValueShape.QUANTITY:
        snapshot = _snapshot_strict_mapping(value)
        return (
            snapshot is not None
            and set(snapshot) == {"value", "unit"}
            and _is_finite_number(snapshot["value"], positive=False)
            and type(snapshot["unit"]) is str
            and snapshot["unit"] in allowed_units
        )
    if shape is ValueShape.RESULT_REF:
        snapshot = _snapshot_strict_mapping(value)
        return (
            snapshot is not None
            and set(snapshot) == {"command_id", "slot"}
            and _is_safe_value_text(snapshot["command_id"])
            and _is_safe_value_text(snapshot["slot"])
        )
    if shape is ValueShape.OBJECT_SELECTOR:
        try:
            SelectorV1.from_mapping(value)
        except Exception:
            return False
        return True
    if shape is ValueShape.OBJECT_ID:
        return type(value) is str and _OBJECT_ID.fullmatch(value) is not None
    if shape is ValueShape.ENTITY_TARGET:
        snapshot = _snapshot_strict_mapping(value)
        if snapshot is None:
            return False
        if set(snapshot) == {"command_id", "slot"}:
            return _matches_value_shape(snapshot, ValueShape.RESULT_REF)
        try:
            SelectorV1.from_mapping(snapshot)
        except Exception:
            return False
        return True
    return False


class RegistryErrorCode(StrEnum):
    """Stable machine-readable reasons registry metadata is rejected."""

    INVALID_NAME = "invalid_name"
    UNSAFE_NAME = "unsafe_name"
    INVALID_METADATA = "invalid_metadata"
    DUPLICATE_OPERATION = "duplicate_operation"
    DUPLICATE_FIELD = "duplicate_field"
    DUPLICATE_BINDING = "duplicate_binding"
    UNKNOWN_OPERATION = "unknown_operation"
    INVALID_ERROR_RECORD = "invalid_error_record"
    UNSUPPORTED_VERSION = "unsupported_version"


class RegistryError(ValueError):
    """Deterministic registry failure with structured context."""

    def __init__(
        self,
        code: RegistryErrorCode,
        message: str,
        *,
        operation: str | None = None,
        field: str | None = None,
    ) -> None:
        if not isinstance(code, RegistryErrorCode):
            raise TypeError("code must be a RegistryErrorCode")
        if not _is_safe_error_message(message):
            raise ValueError(_INVALID_ERROR_MESSAGE)
        assert isinstance(message, str)
        for label, value in (("operation", operation), ("field", field)):
            if value is not None and not _is_bounded_name(value):
                raise ValueError(f"{label} must be a bounded snake_case name or null")
        self.schema_version = SCHEMA_VERSION
        self.code = code
        self.operation = operation
        self.field = field
        self.message = message
        super().__init__(f"execution registry error ({code.value}): {message}")

    def to_mapping(self) -> dict[str, int | str | None]:
        """Return the strict schema-v1 JSON-compatible error record."""

        return {
            "schema_version": self.schema_version,
            "code": self.code.value,
            "operation": self.operation,
            "field": self.field,
            "message": self.message,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        """Parse a strict schema-v1 error record without accepting extensions."""

        if not isinstance(value, Mapping):
            raise cls(
                RegistryErrorCode.INVALID_ERROR_RECORD,
                "error record must be a mapping",
            )
        try:
            keys = tuple(value)
        except RegistryError:
            raise
        except Exception as exc:
            raise cls(
                RegistryErrorCode.INVALID_ERROR_RECORD,
                "error record keys are invalid",
            ) from exc
        if not all(type(key) is str for key in keys):
            raise cls(
                RegistryErrorCode.INVALID_ERROR_RECORD,
                "error record keys must be strings",
            )
        allowed = {"schema_version", "code", "operation", "field", "message"}
        if set(keys) != allowed:
            raise cls(
                RegistryErrorCode.INVALID_ERROR_RECORD,
                "error record fields must exactly match schema v1",
            )

        try:
            version = value["schema_version"]
            raw_code = value["code"]
            operation = value["operation"]
            record_field = value["field"]
            message = value["message"]
        except RegistryError:
            raise
        except Exception as exc:
            raise cls(
                RegistryErrorCode.INVALID_ERROR_RECORD,
                "error record values could not be read",
            ) from exc

        if type(version) is not int:
            raise cls(
                RegistryErrorCode.INVALID_ERROR_RECORD,
                "schema_version must be an integer",
            )
        if version != SCHEMA_VERSION:
            raise cls(
                RegistryErrorCode.UNSUPPORTED_VERSION,
                "unsupported error-record schema version",
            )

        if type(raw_code) is not str:
            raise cls(
                RegistryErrorCode.INVALID_ERROR_RECORD,
                "error code must be a string",
            )
        try:
            code = RegistryErrorCode(raw_code)
        except ValueError as exc:
            raise cls(
                RegistryErrorCode.INVALID_ERROR_RECORD,
                "error code is not supported",
            ) from exc

        if operation is not None and not _is_bounded_name(operation):
            raise cls(
                RegistryErrorCode.INVALID_ERROR_RECORD,
                "operation context must be a bounded snake_case name or null",
            )
        if record_field is not None and not _is_bounded_name(record_field):
            raise cls(
                RegistryErrorCode.INVALID_ERROR_RECORD,
                "field context must be a bounded snake_case name or null",
            )

        if not _is_safe_error_message(message):
            raise cls(
                RegistryErrorCode.INVALID_ERROR_RECORD,
                _INVALID_ERROR_MESSAGE,
            )
        assert isinstance(message, str)
        return cls(code, message, operation=operation, field=record_field)


def _is_bounded_name(value: object) -> bool:
    return (
        type(value) is str
        and len(value) <= _MAX_NAME_LENGTH
        and _SNAKE_CASE.fullmatch(value) is not None
    )


def _validate_name(
    value: object,
    *,
    label: str,
    operation: str | None = None,
    field_name: str | None = None,
) -> str:
    if not _is_bounded_name(value):
        raise RegistryError(
            RegistryErrorCode.INVALID_NAME,
            f"{label} must be bounded nonblank snake_case",
            operation=operation if _is_bounded_name(operation) else None,
            field=field_name if _is_bounded_name(field_name) else None,
        )
    assert isinstance(value, str)
    unsafe = sorted(set(value.split("_")) & _UNSAFE_NAME_TOKENS)
    if unsafe:
        raise RegistryError(
            RegistryErrorCode.UNSAFE_NAME,
            f"{label} contains forbidden token {unsafe[0]!r}",
            operation=operation if _is_bounded_name(operation) else None,
            field=field_name if _is_bounded_name(field_name) else None,
        )
    return value


def _freeze_operation_description(value: object, *, operation: str) -> str:
    if value is None:
        value = f"Execute the {operation} CAD operation."
    if not (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and value.isprintable()
        and len(value.splitlines()) == 1
    ):
        raise RegistryError(
            RegistryErrorCode.INVALID_METADATA,
            "description must be bounded printable single-line text",
            operation=operation,
        )
    try:
        within_budget = len(value.encode("utf-8")) <= _MAX_DESCRIPTION_BYTES
    except UnicodeError:
        within_budget = False
    if not within_budget:
        raise RegistryError(
            RegistryErrorCode.INVALID_METADATA,
            "description must be bounded printable single-line text",
            operation=operation,
        )
    return value


def _freeze_choices(
    values: Iterable[str],
    *,
    label: str,
    field_name: str,
) -> tuple[str, ...]:
    try:
        frozen = tuple(values)
    except RegistryError:
        raise
    except Exception as exc:
        raise RegistryError(
            RegistryErrorCode.INVALID_METADATA,
            f"{label} must be an iterable of bounded strings",
            field=field_name,
        ) from exc
    if (
        not frozen
        or not all(_is_safe_value_text(item) for item in frozen)
        or len(set(frozen)) != len(frozen)
    ):
        raise RegistryError(
            RegistryErrorCode.INVALID_METADATA,
            f"{label} must contain unique bounded strings",
            field=field_name,
        )
    return frozen


def _validate_shape_constraints(
    *,
    value_shape: ValueShape,
    enum_values: Iterable[str],
    allowed_units: Iterable[str],
    referenced_value_shape: ValueShape | None,
    field_name: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(value_shape, ValueShape):
        raise RegistryError(
            RegistryErrorCode.INVALID_METADATA,
            "value_shape must be a ValueShape",
            field=field_name,
        )

    if isinstance(enum_values, (str, bytes, bytearray)) or isinstance(
        allowed_units,
        (str, bytes, bytearray),
    ):
        raise RegistryError(
            RegistryErrorCode.INVALID_METADATA,
            "shape constraints must be string collections, not scalar text",
            field=field_name,
        )
    try:
        enums = tuple(enum_values)
        units = tuple(allowed_units)
    except RegistryError:
        raise
    except Exception as exc:
        raise RegistryError(
            RegistryErrorCode.INVALID_METADATA,
            "shape constraints must be bounded string collections",
            field=field_name,
        ) from exc
    if value_shape is ValueShape.ENUM:
        enums = _freeze_choices(enums, label="enum_values", field_name=field_name)
    elif enums:
        raise RegistryError(
            RegistryErrorCode.INVALID_METADATA,
            "enum_values are only valid for enum fields",
            field=field_name,
        )
    if value_shape is ValueShape.QUANTITY:
        units = _freeze_choices(units, label="allowed_units", field_name=field_name)
    elif units:
        raise RegistryError(
            RegistryErrorCode.INVALID_METADATA,
            "allowed_units are only valid for quantity fields",
            field=field_name,
        )
    if value_shape in {ValueShape.RESULT_REF, ValueShape.ENTITY_TARGET}:
        if not isinstance(referenced_value_shape, ValueShape) or referenced_value_shape in {
            ValueShape.ENUM,
            ValueShape.QUANTITY,
            ValueShape.RESULT_REF,
            ValueShape.OBJECT_SELECTOR,
            ValueShape.ENTITY_TARGET,
        }:
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "reference-bearing fields require a concrete referenced_value_shape",
                field=field_name,
            )
        if (
            value_shape is ValueShape.ENTITY_TARGET
            and referenced_value_shape is not ValueShape.OBJECT_ID
        ):
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "entity_target must reference an object_id result slot",
                field=field_name,
            )
    elif referenced_value_shape is not None:
        raise RegistryError(
            RegistryErrorCode.INVALID_METADATA,
            "referenced_value_shape is only valid for reference-bearing fields",
            field=field_name,
        )
    return enums, units


@dataclass(frozen=True, slots=True)
class FieldMetadata:
    """One target/argument field and its injected-handler parameter binding."""

    name: str
    handler_parameter: str
    value_shape: ValueShape
    required: bool = True
    enum_values: tuple[str, ...] = ()
    allowed_units: tuple[str, ...] = ()
    referenced_value_shape: ValueShape | None = None

    def __post_init__(self) -> None:
        _validate_name(self.name, label="field name", field_name=self.name)
        _validate_name(
            self.handler_parameter,
            label="handler parameter",
            field_name=self.name,
        )
        if type(self.required) is not bool:
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "required must be a boolean",
                field=self.name,
            )
        enums, units = _validate_shape_constraints(
            value_shape=self.value_shape,
            enum_values=self.enum_values,
            allowed_units=self.allowed_units,
            referenced_value_shape=self.referenced_value_shape,
            field_name=self.name,
        )
        object.__setattr__(self, "enum_values", enums)
        object.__setattr__(self, "allowed_units", units)


@dataclass(frozen=True, slots=True)
class ResultSlotMetadata:
    """One typed value extracted from a normalized successful handler result."""

    name: str
    result_field: str
    value_shape: ValueShape
    enum_values: tuple[str, ...] = ()
    allowed_units: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_name(self.name, label="result slot", field_name=self.name)
        _validate_name(
            self.result_field,
            label="result field",
            field_name=self.name,
        )
        enums, units = _validate_shape_constraints(
            value_shape=self.value_shape,
            enum_values=self.enum_values,
            allowed_units=self.allowed_units,
            referenced_value_shape=None,
            field_name=self.name,
        )
        if self.value_shape is ValueShape.RESULT_REF:
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "result slots must expose concrete values",
                field=self.name,
            )
        object.__setattr__(self, "enum_values", enums)
        object.__setattr__(self, "allowed_units", units)


@dataclass(frozen=True, slots=True)
class OperationMetadata:
    """Frozen description of one semantic operation; never an executable hook."""

    operation: str
    handler_name: str
    risk_class: RiskClass
    evidence_required: bool
    target_fields: tuple[FieldMetadata, ...] = ()
    argument_fields: tuple[FieldMetadata, ...] = ()
    execution_profiles: tuple[ExecutionProfile, ...] = (ExecutionProfile.HEADLESS,)
    minimum_freecad_version: tuple[int, int] = (1, 0)
    maximum_freecad_version_exclusive: tuple[int, int] = (2, 0)
    requires_gui_main_thread: bool = False
    resource_budget: ResourceBudget = ResourceBudget()
    direct_exposed: bool = False
    description: str | None = None
    result_slots: tuple[ResultSlotMetadata, ...] = ()
    preservation_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        operation = _validate_name(
            self.operation,
            label="operation",
            operation=self.operation if type(self.operation) is str else None,
        )
        _validate_name(
            self.handler_name,
            label="handler name",
            operation=operation,
        )
        if not isinstance(self.risk_class, RiskClass):
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "risk_class must be a RiskClass",
                operation=operation,
            )
        if type(self.evidence_required) is not bool:
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "evidence_required must be a boolean",
                operation=operation,
            )

        profiles = self._freeze_profiles(self.execution_profiles, operation=operation)
        minimum = self._freeze_version(
            self.minimum_freecad_version,
            operation=operation,
            label="minimum_freecad_version",
        )
        maximum = self._freeze_version(
            self.maximum_freecad_version_exclusive,
            operation=operation,
            label="maximum_freecad_version_exclusive",
        )
        if minimum >= maximum:
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "FreeCAD version range must be non-empty",
                operation=operation,
            )
        if type(self.requires_gui_main_thread) is not bool:
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "requires_gui_main_thread must be a boolean",
                operation=operation,
            )
        if self.requires_gui_main_thread and ExecutionProfile.HEADLESS in profiles:
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "headless execution cannot require the GUI main thread",
                operation=operation,
            )
        if type(self.resource_budget) is not ResourceBudget:
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "resource_budget must be a ResourceBudget",
                operation=operation,
            )
        if type(self.direct_exposed) is not bool:
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "direct_exposed must be a boolean",
                operation=operation,
            )
        description = _freeze_operation_description(self.description, operation=operation)
        slots = self._freeze_result_slots(self.result_slots, operation=operation)
        preservation_fields = self._freeze_preservation_fields(
            self.preservation_fields,
            operation=operation,
        )
        object.__setattr__(self, "execution_profiles", profiles)
        object.__setattr__(self, "minimum_freecad_version", minimum)
        object.__setattr__(self, "maximum_freecad_version_exclusive", maximum)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "result_slots", slots)
        object.__setattr__(self, "preservation_fields", preservation_fields)

        targets = self._freeze_fields(self.target_fields, operation=operation, group="target")
        arguments = self._freeze_fields(
            self.argument_fields,
            operation=operation,
            group="argument",
        )
        object.__setattr__(self, "target_fields", targets)
        object.__setattr__(self, "argument_fields", arguments)

        names: set[str] = set()
        bindings: set[str] = set()
        for item in (*targets, *arguments):
            if item.name in names:
                raise RegistryError(
                    RegistryErrorCode.DUPLICATE_FIELD,
                    "program field is bound more than once",
                    operation=operation,
                    field=item.name,
                )
            names.add(item.name)
            if item.handler_parameter in bindings:
                raise RegistryError(
                    RegistryErrorCode.DUPLICATE_BINDING,
                    "handler parameter is bound more than once",
                    operation=operation,
                    field=item.handler_parameter,
                )
            bindings.add(item.handler_parameter)

        slot_names: set[str] = set()
        result_fields: set[str] = set()
        for slot in slots:
            if slot.name in slot_names:
                raise RegistryError(
                    RegistryErrorCode.DUPLICATE_FIELD,
                    "result slot is declared more than once",
                    operation=operation,
                    field=slot.name,
                )
            slot_names.add(slot.name)
            if slot.result_field in result_fields:
                raise RegistryError(
                    RegistryErrorCode.DUPLICATE_BINDING,
                    "normalized result field is bound more than once",
                    operation=operation,
                    field=slot.result_field,
                )
            result_fields.add(slot.result_field)

    @staticmethod
    def _freeze_fields(
        values: Iterable[FieldMetadata],
        *,
        operation: str,
        group: str,
    ) -> tuple[FieldMetadata, ...]:
        try:
            frozen = tuple(values)
        except RegistryError:
            raise
        except Exception as exc:
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                f"{group}_fields must be an iterable of FieldMetadata",
                operation=operation,
            ) from exc
        if not all(isinstance(item, FieldMetadata) for item in frozen):
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                f"{group}_fields must contain only FieldMetadata",
                operation=operation,
            )
        return frozen

    @staticmethod
    def _freeze_profiles(
        values: Iterable[ExecutionProfile],
        *,
        operation: str,
    ) -> tuple[ExecutionProfile, ...]:
        try:
            frozen = tuple(values)
        except RegistryError:
            raise
        except Exception as exc:
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "execution_profiles must be an iterable of ExecutionProfile",
                operation=operation,
            ) from exc
        if (
            not frozen
            or not all(type(item) is ExecutionProfile for item in frozen)
            or len(set(frozen)) != len(frozen)
        ):
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "execution_profiles must contain unique ExecutionProfile values",
                operation=operation,
            )
        return frozen

    @staticmethod
    def _freeze_version(
        value: Iterable[int],
        *,
        operation: str,
        label: str,
    ) -> tuple[int, int]:
        try:
            frozen = tuple(value)
        except RegistryError:
            raise
        except Exception as exc:
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                f"{label} must be a major/minor pair",
                operation=operation,
            ) from exc
        if len(frozen) != 2 or not all(type(item) is int and 0 <= item <= 999 for item in frozen):
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                f"{label} must be a bounded major/minor pair",
                operation=operation,
            )
        return (frozen[0], frozen[1])

    @staticmethod
    def _freeze_result_slots(
        values: Iterable[ResultSlotMetadata],
        *,
        operation: str,
    ) -> tuple[ResultSlotMetadata, ...]:
        try:
            frozen = tuple(values)
        except RegistryError:
            raise
        except Exception as exc:
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "result_slots must be an iterable of ResultSlotMetadata",
                operation=operation,
            ) from exc
        if not all(type(item) is ResultSlotMetadata for item in frozen):
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "result_slots must contain only ResultSlotMetadata",
                operation=operation,
            )
        return frozen

    @staticmethod
    def _freeze_preservation_fields(
        values: Iterable[str],
        *,
        operation: str,
    ) -> tuple[str, ...]:
        if isinstance(values, (str, bytes, bytearray)):
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "preservation_fields must be a collection of field names",
                operation=operation,
            )
        try:
            frozen = tuple(values)
        except RegistryError:
            raise
        except Exception as exc:
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "preservation_fields must be a collection of field names",
                operation=operation,
            ) from exc
        if not all(_is_bounded_name(item) for item in frozen) or len(set(frozen)) != len(frozen):
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "preservation_fields must contain unique bounded field names",
                operation=operation,
            )
        return tuple(sorted(frozen))


@dataclass(frozen=True, slots=True)
class OperationRegistry:
    """Immutable name-to-metadata lookup for semantic operations."""

    _operations: Mapping[str, OperationMetadata] = field(init=False, repr=False)

    def __init__(self, operations: Iterable[OperationMetadata]) -> None:
        entries: dict[str, OperationMetadata] = {}
        try:
            candidates = tuple(operations)
        except RegistryError:
            raise
        except Exception as exc:
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "operations must be an iterable of OperationMetadata",
            ) from exc
        for metadata in candidates:
            if not isinstance(metadata, OperationMetadata):
                raise RegistryError(
                    RegistryErrorCode.INVALID_METADATA,
                    "operations must contain only OperationMetadata",
                )
            if metadata.operation in entries:
                raise RegistryError(
                    RegistryErrorCode.DUPLICATE_OPERATION,
                    "operation is registered more than once",
                    operation=metadata.operation,
                )
            entries[metadata.operation] = metadata
        object.__setattr__(self, "_operations", MappingProxyType(entries))

    @property
    def operations(self) -> Mapping[str, OperationMetadata]:
        """Expose the immutable metadata mapping for validation and inspection."""

        return self._operations

    def lookup(self, operation: str) -> OperationMetadata:
        """Resolve a registered operation or raise a structured failure."""

        name = _validate_name(
            operation,
            label="operation",
            operation=operation if type(operation) is str else None,
        )
        try:
            return self._operations[name]
        except KeyError as exc:
            raise RegistryError(
                RegistryErrorCode.UNKNOWN_OPERATION,
                "operation is not registered",
                operation=name,
            ) from exc

    def __iter__(self):
        return iter(self._operations)

    def __len__(self) -> int:
        return len(self._operations)


_ENTITY_PRESERVATION_FIELDS = (
    "angle",
    "area_mm2",
    "bbox_mm",
    "center_of_mass_mm",
    "geometry",
    "height",
    "length",
    "parameters",
    "placement",
    "radius",
    "solid_count",
    "valid_shape",
    "volume_mm3",
    "width",
)


DEFAULT_OPERATION_REGISTRY = OperationRegistry(
    (
        OperationMetadata(
            operation="create_box",
            handler_name="create_box",
            risk_class=RiskClass.MUTATING,
            evidence_required=True,
            argument_fields=(
                FieldMetadata("length_mm", "length", ValueShape.POSITIVE_NUMBER),
                FieldMetadata("width_mm", "width", ValueShape.POSITIVE_NUMBER),
                FieldMetadata("height_mm", "height", ValueShape.POSITIVE_NUMBER),
                FieldMetadata("position_mm", "position", ValueShape.VECTOR3, required=False),
            ),
            resource_budget=ResourceBudget(
                max_runtime_ms=30_000,
                max_created_objects=1,
                max_result_bytes=65_536,
            ),
            direct_exposed=True,
            description="向任务提交一个长方体直接操作",
            result_slots=(
                ResultSlotMetadata(
                    "object",
                    "object_id",
                    ValueShape.OBJECT_ID,
                ),
            ),
        ),
        OperationMetadata(
            operation="create_cylinder",
            handler_name="create_cylinder",
            risk_class=RiskClass.MUTATING,
            evidence_required=True,
            argument_fields=(
                FieldMetadata("radius_mm", "radius", ValueShape.POSITIVE_NUMBER),
                FieldMetadata("height_mm", "height", ValueShape.POSITIVE_NUMBER),
                FieldMetadata("position_mm", "position", ValueShape.VECTOR3, required=False),
                FieldMetadata(
                    "axis",
                    "axis",
                    ValueShape.ENUM,
                    required=False,
                    enum_values=("x", "y", "z"),
                ),
            ),
            resource_budget=ResourceBudget(
                max_runtime_ms=30_000,
                max_created_objects=1,
                max_result_bytes=65_536,
            ),
            direct_exposed=True,
            description="向任务提交一个圆柱体直接操作",
            result_slots=(
                ResultSlotMetadata(
                    "object",
                    "object_id",
                    ValueShape.OBJECT_ID,
                ),
            ),
        ),
        OperationMetadata(
            operation="modify_parameter",
            handler_name="modify_parameter",
            risk_class=RiskClass.MUTATING,
            evidence_required=True,
            target_fields=(
                FieldMetadata(
                    "object",
                    "target",
                    ValueShape.ENTITY_TARGET,
                    referenced_value_shape=ValueShape.OBJECT_ID,
                ),
            ),
            argument_fields=(
                FieldMetadata(
                    "parameter",
                    "parameter",
                    ValueShape.ENUM,
                    enum_values=("height", "length", "radius", "width"),
                ),
                FieldMetadata("value_mm", "value", ValueShape.POSITIVE_NUMBER),
            ),
            resource_budget=ResourceBudget(
                max_runtime_ms=30_000,
                max_created_objects=0,
                max_result_bytes=65_536,
            ),
            direct_exposed=True,
            description="按显式验收条件修改选定对象参数",
            preservation_fields=_ENTITY_PRESERVATION_FIELDS,
        ),
        OperationMetadata(
            operation="move_part",
            handler_name="move_part",
            risk_class=RiskClass.MUTATING,
            evidence_required=True,
            target_fields=(
                FieldMetadata(
                    "object",
                    "target",
                    ValueShape.ENTITY_TARGET,
                    referenced_value_shape=ValueShape.OBJECT_ID,
                ),
            ),
            argument_fields=(FieldMetadata("position_mm", "position", ValueShape.VECTOR3),),
            resource_budget=ResourceBudget(
                max_runtime_ms=30_000,
                max_created_objects=0,
                max_result_bytes=65_536,
            ),
            direct_exposed=True,
            description="按显式验收条件移动选定对象",
            preservation_fields=_ENTITY_PRESERVATION_FIELDS,
        ),
        OperationMetadata(
            operation="rotate_part",
            handler_name="rotate_part",
            risk_class=RiskClass.MUTATING,
            evidence_required=True,
            target_fields=(
                FieldMetadata(
                    "object",
                    "target",
                    ValueShape.ENTITY_TARGET,
                    referenced_value_shape=ValueShape.OBJECT_ID,
                ),
            ),
            argument_fields=(
                FieldMetadata(
                    "axis",
                    "axis",
                    ValueShape.ENUM,
                    enum_values=("x", "y", "z"),
                ),
                FieldMetadata("angle_deg", "angle", ValueShape.ANGLE_DEGREES),
            ),
            resource_budget=ResourceBudget(
                max_runtime_ms=30_000,
                max_created_objects=0,
                max_result_bytes=65_536,
            ),
            direct_exposed=True,
            description="按显式验收条件旋转选定对象",
            preservation_fields=_ENTITY_PRESERVATION_FIELDS,
        ),
        OperationMetadata(
            operation="inspect_model",
            handler_name="inspect_model",
            risk_class=RiskClass.READ_ONLY,
            evidence_required=False,
            resource_budget=ResourceBudget(
                max_runtime_ms=10_000,
                max_created_objects=0,
                max_result_bytes=262_144,
            ),
            direct_exposed=True,
            description="检查指定任务版本的模型事实",
        ),
    )
)

__all__ = [
    "DEFAULT_OPERATION_REGISTRY",
    "ExecutionProfile",
    "FieldMetadata",
    "OperationMetadata",
    "OperationRegistry",
    "RegistryError",
    "RegistryErrorCode",
    "ResourceBudget",
    "ResultSlotMetadata",
    "RiskClass",
    "ValueShape",
]
