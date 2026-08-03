"""Neutral executor errors safe to import in the parent control process."""

from __future__ import annotations

from enum import StrEnum

from vibecad.workflow.errors import SCHEMA_VERSION


class ExecutorErrorCode(StrEnum):
    """Stable failures owned by the trusted executor boundary."""

    INVALID_INPUT = "invalid_input"
    INVALID_CANDIDATE = "invalid_candidate"
    INVALID_LEASE = "invalid_lease"
    CAD_FAILURE = "cad_failure"
    ARTIFACT_FAILURE = "artifact_failure"
    INTEGRITY_FAILURE = "integrity_failure"


_ERROR_MESSAGES = {
    ExecutorErrorCode.INVALID_INPUT: "The executor input is invalid.",
    ExecutorErrorCode.INVALID_CANDIDATE: "The candidate capability is invalid.",
    ExecutorErrorCode.INVALID_LEASE: "The project write lease is invalid.",
    ExecutorErrorCode.CAD_FAILURE: "The CAD operation failed.",
    ExecutorErrorCode.ARTIFACT_FAILURE: "The CAD artifact is invalid.",
    ExecutorErrorCode.INTEGRITY_FAILURE: "The candidate integrity check failed.",
}


class ExecutorError(ValueError):
    """Fixed, non-reflective executor failure."""

    __slots__ = ("code", "message", "schema_version")

    def __init__(self, code: ExecutorErrorCode) -> None:
        if type(code) is not ExecutorErrorCode:
            raise TypeError("code must be an ExecutorErrorCode")
        self.schema_version = SCHEMA_VERSION
        self.code = code
        self.message = _ERROR_MESSAGES[code]
        self.args = (self.message,)

    def to_mapping(self) -> dict[str, int | str]:
        """Return the fixed schema-v1 JSON-compatible error record."""

        return {
            "schema_version": self.schema_version,
            "code": self.code.value,
            "message": self.message,
        }


__all__ = ("ExecutorError", "ExecutorErrorCode")
