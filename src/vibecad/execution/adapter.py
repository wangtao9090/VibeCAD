"""Execute authentic validated programs through explicitly injected handlers.

This module is the pure in-process execution boundary for Phase 1.  It does
not discover handlers, import CAD integrations, retry operations, or own a
candidate revision.  Callers bind synchronous semantic handlers explicitly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic_ns as _monotonic_ns
from types import MappingProxyType
from typing import Any

from vibecad.execution.registry import RiskClass
from vibecad.execution.results import (
    NormalizedToolOutcome,
    normalize_tool_exception,
    normalize_tool_result,
)
from vibecad.workflow.contracts import (
    EvidenceKind,
    ExecutionEvidence,
    ModelProgram,
    StepResult,
    ValueSource,
)
from vibecad.workflow.errors import SCHEMA_VERSION
from vibecad.workflow.program import BoundCommand, ValidatedProgram


class AdapterErrorCode(StrEnum):
    """Stable configuration failures raised before any handler executes."""

    INVALID_PROGRAM = "invalid_program"
    INVALID_HANDLERS = "invalid_handlers"
    INVALID_REVISION = "invalid_revision"
    MISSING_HANDLER = "missing_handler"
    NON_CALLABLE_HANDLER = "non_callable_handler"


_ERROR_MESSAGES = {
    AdapterErrorCode.INVALID_PROGRAM: "Validated program capability is invalid.",
    AdapterErrorCode.INVALID_HANDLERS: "Handler mapping could not be read.",
    AdapterErrorCode.INVALID_REVISION: "Candidate revision is invalid.",
    AdapterErrorCode.MISSING_HANDLER: "A required handler is missing.",
    AdapterErrorCode.NON_CALLABLE_HANDLER: "A required handler is not callable.",
}


class AdapterError(ValueError):
    """A fixed, non-reflective adapter configuration error."""

    def __init__(self, code: AdapterErrorCode) -> None:
        if not isinstance(code, AdapterErrorCode):
            raise TypeError("code must be an AdapterErrorCode")
        self.schema_version = SCHEMA_VERSION
        self.code = code
        self.message = _ERROR_MESSAGES[code]
        super().__init__(f"execution adapter error ({code.value}): {self.message}")

    def to_mapping(self) -> dict[str, int | str]:
        """Return the fixed schema-v1 JSON-compatible error record."""

        return {
            "schema_version": self.schema_version,
            "code": self.code.value,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class _PlannedCommand:
    operation_id: str
    operation: str
    handler_name: str
    handler_kwargs: Mapping[str, Any]
    risk_class: RiskClass
    evidence_required: bool
    handler: Callable[..., object]


@dataclass(frozen=True, slots=True)
class _CommandSnapshot:
    operation_id: str
    operation: str
    handler_name: str
    handler_kwargs: Mapping[str, Any]
    risk_class: RiskClass
    evidence_required: bool


def _invalid_program() -> AdapterError:
    return AdapterError(AdapterErrorCode.INVALID_PROGRAM)


def _freeze_bound_value(value: object) -> object:
    """Copy the already-sealed JSON-like command value into adapter ownership."""

    if type(value) is MappingProxyType:
        try:
            keys = tuple(value)
            if any(type(key) is not str for key in keys) or len(set(keys)) != len(keys):
                raise _invalid_program()
            return MappingProxyType({key: _freeze_bound_value(value[key]) for key in keys})
        except AdapterError:
            raise
        except Exception:
            raise _invalid_program() from None
    if type(value) is tuple:
        return tuple(_freeze_bound_value(item) for item in value)
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise _invalid_program()


def _snapshot_command(command: object) -> _CommandSnapshot:
    if type(command) is not BoundCommand:
        raise _invalid_program()
    try:
        operation_id = command.id
        operation = command.operation
        handler_name = command.handler_name
        handler_kwargs = command.handler_kwargs
        depends_on = command.depends_on
        preserve = command.preserve
        source = command.source
        risk_class = command.risk_class
        evidence_required = command.evidence_required
    except Exception:
        raise _invalid_program() from None

    if any(
        type(value) is not str or not value.strip()
        for value in (operation_id, operation, handler_name)
    ):
        raise _invalid_program()
    if type(handler_kwargs) is not MappingProxyType:
        raise _invalid_program()
    if type(depends_on) is not tuple or not all(
        type(item) is str and bool(item.strip()) for item in depends_on
    ):
        raise _invalid_program()
    if type(preserve) is not tuple or not all(
        type(item) is str and bool(item.strip()) for item in preserve
    ):
        raise _invalid_program()
    if type(source) is not ValueSource or type(risk_class) is not RiskClass:
        raise _invalid_program()
    if type(evidence_required) is not bool:
        raise _invalid_program()

    frozen_kwargs = _freeze_bound_value(handler_kwargs)
    assert isinstance(frozen_kwargs, Mapping)
    return _CommandSnapshot(
        operation_id=operation_id,
        operation=operation,
        handler_name=handler_name,
        handler_kwargs=frozen_kwargs,
        risk_class=risk_class,
        evidence_required=evidence_required,
    )


def _snapshot_program(program: object) -> tuple[_CommandSnapshot, ...]:
    if type(program) is not ValidatedProgram:
        raise _invalid_program()
    try:
        program.require_authentic()
        source_program = program.program
        commands = program.commands
    except Exception:
        raise _invalid_program() from None
    if type(source_program) is not ModelProgram or type(commands) is not tuple or not commands:
        raise _invalid_program()

    snapshots = tuple(_snapshot_command(command) for command in commands)
    identifiers = tuple(command.operation_id for command in snapshots)
    if len(set(identifiers)) != len(identifiers):
        raise _invalid_program()
    return snapshots


def _validated_revision(revision: object) -> str | None:
    try:
        probe = StepResult(
            ok=True,
            value=None,
            elapsed_ms=0,
            revision=revision,  # type: ignore[arg-type]
            error=None,
        )
    except Exception:
        raise AdapterError(AdapterErrorCode.INVALID_REVISION) from None
    return probe.revision


def _freeze_plan(
    commands: tuple[_CommandSnapshot, ...],
    handlers: object,
) -> tuple[_PlannedCommand, ...]:
    if not isinstance(handlers, Mapping):
        raise AdapterError(AdapterErrorCode.INVALID_HANDLERS)

    resolved: dict[str, Callable[..., object]] = {}
    plan: list[_PlannedCommand] = []
    for command in commands:
        handler = resolved.get(command.handler_name)
        if handler is None:
            try:
                candidate = handlers[command.handler_name]
            except KeyError:
                raise AdapterError(AdapterErrorCode.MISSING_HANDLER) from None
            except Exception:
                raise AdapterError(AdapterErrorCode.INVALID_HANDLERS) from None
            if not callable(candidate):
                raise AdapterError(AdapterErrorCode.NON_CALLABLE_HANDLER)
            handler = candidate
            resolved[command.handler_name] = handler
        plan.append(
            _PlannedCommand(
                operation_id=command.operation_id,
                operation=command.operation,
                handler_name=command.handler_name,
                handler_kwargs=command.handler_kwargs,
                risk_class=command.risk_class,
                evidence_required=command.evidence_required,
                handler=handler,
            )
        )
    return tuple(plan)


def _facts(command: _PlannedCommand) -> Mapping[str, object]:
    return {
        "operation": command.operation,
        "handler_name": command.handler_name,
        "risk_class": command.risk_class.value,
    }


def _read_monotonic_ns() -> int | None:
    """Read the diagnostic clock without turning tool success into failure."""

    try:
        value = _monotonic_ns()
    except Exception:
        return None
    return value if type(value) is int else None


def _elapsed_ms(started: int | None, finished: int | None) -> int | float:
    if started is None or finished is None or finished < started:
        return 0
    try:
        return (finished - started) / 1_000_000
    except (ArithmeticError, OverflowError):
        return 0


def _with_execution_evidence(
    outcome: NormalizedToolOutcome,
    operation_id: str,
) -> NormalizedToolOutcome:
    result = outcome.result
    evidence = ExecutionEvidence(
        id=f"{operation_id}:execution",
        kind=EvidenceKind.OBSERVATION,
        name="execution_acknowledged",
        operation_id=operation_id,
        value={"result_ok": True},
    )
    enriched = StepResult(
        ok=result.ok,
        value=result.value,
        elapsed_ms=result.elapsed_ms,
        operation_id=result.operation_id,
        revision=result.revision,
        facts=result.facts,
        artifacts=result.artifacts,
        warnings=result.warnings,
        evidence=(evidence,),
        error=result.error,
    )
    return NormalizedToolOutcome(result=enriched, diagnostic=outcome.diagnostic)


def execute_validated_program(
    program: ValidatedProgram,
    handlers: Mapping[str, Callable[..., object]],
    *,
    revision: str | None = None,
) -> tuple[NormalizedToolOutcome, ...]:
    """Execute a sealed program once per command through a frozen handler plan."""

    commands = _snapshot_program(program)
    trusted_revision = _validated_revision(revision)
    plan = _freeze_plan(commands, handlers)

    outcomes: list[NormalizedToolOutcome] = []
    for command in plan:
        started = _read_monotonic_ns()
        try:
            raw = command.handler(**command.handler_kwargs)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                try:
                    _read_monotonic_ns()
                except BaseException:
                    pass
                raise exc
            finished = _read_monotonic_ns()
            outcome = normalize_tool_exception(
                exc,
                operation_id=command.operation_id,
                elapsed_ms=_elapsed_ms(started, finished),
                revision=trusted_revision,
                facts=_facts(command),
            )
        else:
            finished = _read_monotonic_ns()
            outcome = normalize_tool_result(
                raw,
                operation_id=command.operation_id,
                elapsed_ms=_elapsed_ms(started, finished),
                revision=trusted_revision,
                facts=_facts(command),
            )

        if outcome.result.ok and command.evidence_required:
            outcome = _with_execution_evidence(outcome, command.operation_id)
        outcomes.append(outcome)
        if not outcome.result.ok:
            break

    return tuple(outcomes)


__all__ = [
    "AdapterError",
    "AdapterErrorCode",
    "execute_validated_program",
]
