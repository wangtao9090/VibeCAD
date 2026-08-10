"""Private fail-closed gate before a visual proposal can reach Task authority."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable


class VisualAdmissionGateErrorCode(StrEnum):
    NOT_READY = "not_ready"
    INTEGRITY_FAILURE = "integrity_failure"
    UNAVAILABLE = "unavailable"


class VisualAdmissionGateError(RuntimeError):
    """Bounded application-gate failure without persisted input reflection."""

    def __init__(self, code: VisualAdmissionGateErrorCode) -> None:
        if type(code) is not VisualAdmissionGateErrorCode:
            raise TypeError("code must be an exact VisualAdmissionGateErrorCode")
        self.code = code
        super().__init__(code.value)


@runtime_checkable
class VisualAdmissionGate(Protocol):
    """Application-owned recomputation with no Task or adoption authority."""

    def require_exact(
        self,
        reconstruction_id: str,
        *,
        expected_generation: int,
    ) -> None: ...


__all__: tuple[str, ...] = ()
