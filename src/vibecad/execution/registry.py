"""Immutable metadata for the Phase-1 semantic CAD operation allowlist.

This module describes how provider-neutral program fields bind to an injected
handler name.  It intentionally contains no callables, import paths, source
text, shell commands, filesystem/network behavior, or CAD imports.  Validation
and execution are separate later stages.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Self

from vibecad.workflow.errors import SCHEMA_VERSION

_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_MAX_NAME_LENGTH = 64
_MAX_ERROR_MESSAGE_LENGTH = 256
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


class ValueShape(StrEnum):
    """Closed Phase-1 value shapes available to the program validator."""

    NONBLANK_STRING = "nonblank_string"
    POSITIVE_NUMBER = "positive_number"
    BOOLEAN = "boolean"
    VECTOR3 = "vector3"


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


@dataclass(frozen=True, slots=True)
class FieldMetadata:
    """One target/argument field and its injected-handler parameter binding."""

    name: str
    handler_parameter: str
    value_shape: ValueShape
    required: bool = True

    def __post_init__(self) -> None:
        _validate_name(self.name, label="field name", field_name=self.name)
        _validate_name(
            self.handler_parameter,
            label="handler parameter",
            field_name=self.name,
        )
        if not isinstance(self.value_shape, ValueShape):
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "value_shape must be a ValueShape",
                field=self.name,
            )
        if type(self.required) is not bool:
            raise RegistryError(
                RegistryErrorCode.INVALID_METADATA,
                "required must be a boolean",
                field=self.name,
            )


@dataclass(frozen=True, slots=True)
class OperationMetadata:
    """Frozen description of one semantic operation; never an executable hook."""

    operation: str
    handler_name: str
    risk_class: RiskClass
    evidence_required: bool
    target_fields: tuple[FieldMetadata, ...] = ()
    argument_fields: tuple[FieldMetadata, ...] = ()

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


DEFAULT_OPERATION_REGISTRY = OperationRegistry(
    (
        OperationMetadata(
            operation="create_document",
            handler_name="new_document",
            risk_class=RiskClass.DESTRUCTIVE,
            evidence_required=True,
            argument_fields=(FieldMetadata("name", "name", ValueShape.NONBLANK_STRING),),
        ),
        OperationMetadata(
            operation="create_box",
            handler_name="add_box",
            risk_class=RiskClass.MUTATING,
            evidence_required=True,
            argument_fields=(
                FieldMetadata("length", "length", ValueShape.POSITIVE_NUMBER),
                FieldMetadata("width", "width", ValueShape.POSITIVE_NUMBER),
                FieldMetadata("height", "height", ValueShape.POSITIVE_NUMBER),
                FieldMetadata("position", "position", ValueShape.VECTOR3, required=False),
            ),
        ),
        OperationMetadata(
            operation="modify_parameter",
            handler_name="modify_part",
            risk_class=RiskClass.MUTATING,
            evidence_required=True,
            target_fields=(FieldMetadata("object", "name", ValueShape.NONBLANK_STRING),),
            argument_fields=(
                FieldMetadata("parameter", "parameter", ValueShape.NONBLANK_STRING),
                FieldMetadata("value", "value", ValueShape.POSITIVE_NUMBER),
            ),
        ),
        OperationMetadata(
            operation="inspect_model",
            handler_name="describe_part",
            risk_class=RiskClass.READ_ONLY,
            evidence_required=False,
        ),
    )
)

__all__ = [
    "DEFAULT_OPERATION_REGISTRY",
    "FieldMetadata",
    "OperationMetadata",
    "OperationRegistry",
    "RegistryError",
    "RegistryErrorCode",
    "RiskClass",
    "ValueShape",
]
