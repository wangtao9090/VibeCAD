"""Stable, versioned validation errors for workflow contracts.

This module is intentionally pure standard library and has no dependency on
the other workflow contracts.  It also owns the shared schema version so error
reports cannot drift from the contracts they describe.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum

SCHEMA_VERSION = 1
MAX_SAFE_JSON_INTEGER = 2**53 - 1
SCHEMA_VERSION_RANGE_MESSAGE = "schema_version is outside the safe integer range"


class ContractErrorCode(StrEnum):
    """Machine-readable reasons why a workflow contract was rejected."""

    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    UNSUPPORTED_VERSION = "unsupported_version"
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"


def escape_json_pointer_token(token: str) -> str:
    """Encode one RFC 6901 reference token without losing ``~`` or ``/``."""

    return token.replace("~", "~0").replace("/", "~1")


def join_json_pointer(pointer: str, token: str) -> str:
    """Append a token to an already-canonical RFC 6901 JSON Pointer."""

    return f"{pointer}/{escape_json_pointer_token(token)}"


def is_canonical_json_pointer(value: object) -> bool:
    """Return whether *value* is an RFC 6901 pointer in canonical escaped form."""

    if type(value) is not str:
        return False
    if value == "":
        return True
    if not value.startswith("/"):
        return False
    index = 0
    while index < len(value):
        if value[index] != "~":
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] not in {"0", "1"}:
            return False
        index += 2
    return True


class ContractValidationError(ValueError):
    """A deterministic contract error with an RFC 6901 input path."""

    def __init__(
        self,
        code: ContractErrorCode,
        path: str,
        message: str,
        *,
        schema_version: int = SCHEMA_VERSION,
    ) -> None:
        if type(schema_version) is not int:
            raise ValueError(f"schema_version must be exactly {SCHEMA_VERSION}")
        if abs(schema_version) > MAX_SAFE_JSON_INTEGER:
            raise ValueError(SCHEMA_VERSION_RANGE_MESSAGE)
        if schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be exactly {SCHEMA_VERSION}")
        if not isinstance(code, ContractErrorCode):
            raise TypeError("code must be a ContractErrorCode")
        if type(path) is not str:
            raise TypeError("path must be a string")
        if not is_canonical_json_pointer(path):
            raise ValueError("path must be a canonical RFC 6901 JSON Pointer")
        if type(message) is not str:
            raise TypeError("message must be a string")
        if not message.strip():
            raise ValueError("message must not be blank")
        self.schema_version = schema_version
        self.code = code
        self.path = path
        self.message = message
        # JSON quoting makes control characters visible instead of allowing a
        # hostile input path/message to forge extra log lines.
        rendered_path = json.dumps(path)
        rendered_message = json.dumps(message)
        super().__init__(f"contract validation error at {rendered_path}: {rendered_message}")

    def to_mapping(self) -> dict[str, int | str]:
        """Return the canonical JSON-compatible error envelope."""

        return {
            "schema_version": self.schema_version,
            "code": self.code.value,
            "path": self.path,
            "message": self.message,
        }

    @classmethod
    def from_mapping(cls, value: object) -> ContractValidationError:
        """Parse a strict error envelope and reject extension fields."""

        if not isinstance(value, Mapping):
            raise cls(ContractErrorCode.INVALID_TYPE, "", "expected a mapping")
        for key in value:
            if type(key) is not str:
                raise cls(
                    ContractErrorCode.INVALID_TYPE,
                    "",
                    "mapping field names must be strings",
                )
        allowed = {"schema_version", "code", "path", "message"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            name = unknown[0]
            raise cls(
                ContractErrorCode.UNKNOWN_FIELD,
                join_json_pointer("", name),
                "unknown field",
            )
        missing = sorted(allowed - set(value))
        if missing:
            name = missing[0]
            raise cls(
                ContractErrorCode.MISSING_FIELD,
                join_json_pointer("", name),
                "required field missing",
            )

        version = value["schema_version"]
        if type(version) is not int:
            raise cls(
                ContractErrorCode.INVALID_TYPE,
                "/schema_version",
                "schema_version must be an integer",
            )
        if abs(version) > MAX_SAFE_JSON_INTEGER:
            raise cls(
                ContractErrorCode.INVALID_VALUE,
                "/schema_version",
                SCHEMA_VERSION_RANGE_MESSAGE,
            )
        if version != SCHEMA_VERSION:
            raise cls(
                ContractErrorCode.UNSUPPORTED_VERSION,
                "/schema_version",
                f"unsupported schema_version {version}; expected {SCHEMA_VERSION}",
            )

        code_value = value["code"]
        if type(code_value) is not str:
            raise cls(ContractErrorCode.INVALID_TYPE, "/code", "expected a string enum value")
        try:
            code = ContractErrorCode(code_value)
        except ValueError as exc:
            supported = ", ".join(item.value for item in ContractErrorCode)
            raise cls(
                ContractErrorCode.INVALID_VALUE,
                "/code",
                f"unsupported value {code_value!r}; expected one of: {supported}",
            ) from exc

        path = value["path"]
        if type(path) is not str:
            raise cls(ContractErrorCode.INVALID_TYPE, "/path", "expected a string")
        if not is_canonical_json_pointer(path):
            raise cls(
                ContractErrorCode.INVALID_VALUE,
                "/path",
                "expected a canonical RFC 6901 JSON Pointer",
            )

        message = value["message"]
        if type(message) is not str:
            raise cls(ContractErrorCode.INVALID_TYPE, "/message", "expected a string")
        if not message.strip():
            raise cls(ContractErrorCode.INVALID_VALUE, "/message", "must not be blank")
        return cls(code, path, message, schema_version=version)
