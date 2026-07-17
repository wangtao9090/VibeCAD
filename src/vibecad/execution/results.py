"""Pure normalization of legacy semantic-tool outcomes.

The normalizer converts already-returned values into workflow ``StepResult``
contracts.  It never selects or invokes a handler, owns a clock, imports CAD
runtime modules, performs I/O, or serializes its local diagnostic class.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from vibecad.workflow import ErrorCategory, ExecutionEvidence, StepError, StepResult
from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER

_MAX_CODE_LENGTH = 64
_MAX_MESSAGE_LENGTH = 256
_MAX_REFERENCE_LENGTH = 256
_MAX_REFERENCES = 64
_SAFE_TOOL_CODE = re.compile(r"^[a-z][a-z0-9]*(?:[_.-][a-z0-9]+)*$")
_STRUCTURED_ERROR_FIELDS = frozenset(
    {
        "code",
        "message",
        "retryable",
        "needs_input",
        "related_objects",
        "diagnostic_artifacts",
    }
)


class ToolResultCode(StrEnum):
    """Stable public codes emitted by the normalization boundary."""

    TOOL_REPORTED_ERROR = "tool_reported_error"
    INVALID_TOOL_RESULT = "invalid_tool_result"
    CONTRADICTORY_TOOL_RESULT = "contradictory_tool_result"
    UNEXPECTED_TOOL_EXCEPTION = "unexpected_tool_exception"


class ToolDiagnosticClass(StrEnum):
    """Local-only failure classification; never part of ``StepResult``."""

    REPORTED_ERROR = "reported_error"
    INVALID_RESULT = "invalid_result"
    CONTRADICTORY_RESULT = "contradictory_result"
    TIMEOUT_EXCEPTION = "timeout_exception"
    VALUE_EXCEPTION = "value_exception"
    RUNTIME_EXCEPTION = "runtime_exception"
    OTHER_EXCEPTION = "other_exception"


@dataclass(frozen=True, slots=True, kw_only=True)
class NormalizedToolOutcome:
    """Immutable in-process pairing of public result and local classification."""

    result: StepResult
    diagnostic: ToolDiagnosticClass | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.result, StepResult):
            raise TypeError("result must be a StepResult")
        if self.diagnostic is not None and not isinstance(
            self.diagnostic,
            ToolDiagnosticClass,
        ):
            raise TypeError("diagnostic must be a ToolDiagnosticClass or null")


@dataclass(frozen=True, slots=True, kw_only=True)
class _ResultContext:
    elapsed_ms: int | float
    operation_id: str | None
    revision: str | None
    facts: Mapping[str, object]
    artifacts: tuple[str, ...]
    warnings: tuple[str, ...]
    evidence: tuple[ExecutionEvidence, ...]


@dataclass(frozen=True, slots=True)
class _FailureMetadata:
    message: str
    retryable: bool = False
    needs_input: bool = False
    related_objects: tuple[str, ...] = ()
    diagnostic_artifacts: tuple[str, ...] = ()
    tool_code: str | None = None


_PUBLIC_MESSAGES = {
    ToolResultCode.TOOL_REPORTED_ERROR: "CAD tool reported an error.",
    ToolResultCode.INVALID_TOOL_RESULT: "CAD tool returned an invalid result.",
    ToolResultCode.CONTRADICTORY_TOOL_RESULT: ("CAD tool returned a contradictory result."),
    ToolResultCode.UNEXPECTED_TOOL_EXCEPTION: ("CAD tool execution failed unexpectedly."),
}


def _safe_elapsed(value: object) -> int | float | None:
    if type(value) not in {int, float}:
        return None
    if type(value) is int and abs(value) > MAX_SAFE_JSON_INTEGER:
        return None
    if type(value) is float and not math.isfinite(value):
        return None
    if value < 0:
        return None
    return value


def _safe_text(value: object, *, maximum: int) -> bool:
    return (
        type(value) is str
        and bool(value.strip())
        and len(value) <= maximum
        and value.isprintable()
        and len(value.splitlines()) == 1
    )


def _snapshot_mapping(value: Mapping[object, object]) -> dict[str, object] | None:
    """Read one deterministic top-level snapshot or mark the mapping invalid."""

    try:
        keys = tuple(value)
        if any(type(key) is not str for key in keys):
            return None
        if len(set(keys)) != len(keys):
            return None
        return {key: value[key] for key in keys}
    except Exception:
        return None


def _safe_references(value: object) -> tuple[str, ...] | None:
    if type(value) not in {list, tuple}:
        return None
    if len(value) > _MAX_REFERENCES:
        return None
    if not all(_safe_text(item, maximum=_MAX_REFERENCE_LENGTH) for item in value):
        return None
    return tuple(value)


def _generic_failure_metadata() -> _FailureMetadata:
    return _FailureMetadata(_PUBLIC_MESSAGES[ToolResultCode.TOOL_REPORTED_ERROR])


def _parse_failure_fields(fields: Mapping[str, object]) -> _FailureMetadata | None:
    message = _PUBLIC_MESSAGES[ToolResultCode.TOOL_REPORTED_ERROR]
    tool_code: str | None = None
    retryable = False
    needs_input = False
    related_objects: tuple[str, ...] = ()
    diagnostic_artifacts: tuple[str, ...] = ()

    if "code" in fields:
        raw_code = fields["code"]
        if (
            not _safe_text(raw_code, maximum=_MAX_CODE_LENGTH)
            or _SAFE_TOOL_CODE.fullmatch(raw_code) is None  # type: ignore[arg-type]
        ):
            return None
        assert isinstance(raw_code, str)
        tool_code = raw_code

    if "message" in fields:
        raw_message = fields["message"]
        if not _safe_text(raw_message, maximum=_MAX_MESSAGE_LENGTH):
            return None
        assert isinstance(raw_message, str)
        message = raw_message

    if "retryable" in fields:
        if type(fields["retryable"]) is not bool:
            return None
        retryable = fields["retryable"]  # type: ignore[assignment]

    if "needs_input" in fields:
        if type(fields["needs_input"]) is not bool:
            return None
        needs_input = fields["needs_input"]  # type: ignore[assignment]

    if "related_objects" in fields:
        parsed_related = _safe_references(fields["related_objects"])
        if parsed_related is None:
            return None
        related_objects = parsed_related

    if "diagnostic_artifacts" in fields:
        parsed_artifacts = _safe_references(fields["diagnostic_artifacts"])
        if parsed_artifacts is None:
            return None
        diagnostic_artifacts = parsed_artifacts

    return _FailureMetadata(
        message=message,
        retryable=retryable,
        needs_input=needs_input,
        related_objects=related_objects,
        diagnostic_artifacts=diagnostic_artifacts,
        tool_code=tool_code,
    )


def _failure_metadata(
    result: Mapping[str, object],
    *,
    error_is_non_null: bool,
) -> _FailureMetadata:
    if error_is_non_null:
        error = result["error"]
        if type(error) is str:
            if _safe_text(error, maximum=_MAX_MESSAGE_LENGTH):
                return _FailureMetadata(message=error)
            return _generic_failure_metadata()
        if isinstance(error, Mapping):
            nested = _snapshot_mapping(error)
            if nested is None or not set(nested).issubset(_STRUCTURED_ERROR_FIELDS):
                return _generic_failure_metadata()
            return _parse_failure_fields(nested) or _generic_failure_metadata()
        return _generic_failure_metadata()

    top_level = {key: result[key] for key in _STRUCTURED_ERROR_FIELDS if key in result}
    return _parse_failure_fields(top_level) or _generic_failure_metadata()


def _snapshot_context(
    *,
    operation_id: object,
    elapsed_ms: int | float,
    revision: object,
    facts: object,
    artifacts: object,
    warnings: object,
    evidence: object,
) -> _ResultContext:
    for value in (artifacts, warnings, evidence):
        if not isinstance(value, (list, tuple)):
            raise TypeError("context sequences must be lists or tuples")

    probe = StepResult(
        ok=True,
        value=None,
        elapsed_ms=elapsed_ms,
        operation_id=operation_id,  # type: ignore[arg-type]
        revision=revision,  # type: ignore[arg-type]
        facts={} if facts is None else facts,  # type: ignore[arg-type]
        artifacts=artifacts,  # type: ignore[arg-type]
        warnings=warnings,  # type: ignore[arg-type]
        evidence=evidence,  # type: ignore[arg-type]
        error=None,
    )
    return _ResultContext(
        elapsed_ms=probe.elapsed_ms,
        operation_id=probe.operation_id,
        revision=probe.revision,
        facts=probe.facts,
        artifacts=probe.artifacts,
        warnings=probe.warnings,
        evidence=probe.evidence,
    )


def _canonical_operation_id(value: object, elapsed_ms: int | float) -> str | None:
    try:
        probe = StepResult(
            ok=True,
            value=None,
            elapsed_ms=elapsed_ms,
            operation_id=value,  # type: ignore[arg-type]
            error=None,
        )
    except Exception:
        return None
    return probe.operation_id


def _canonical_revision(value: object, elapsed_ms: int | float) -> str | None:
    try:
        probe = StepResult(
            ok=True,
            value=None,
            elapsed_ms=elapsed_ms,
            revision=value,  # type: ignore[arg-type]
            error=None,
        )
    except Exception:
        return None
    return probe.revision


def _minimal_context(
    *,
    operation_id: object,
    revision: object,
    elapsed_ms: int | float,
) -> _ResultContext:
    return _snapshot_context(
        operation_id=_canonical_operation_id(operation_id, elapsed_ms),
        elapsed_ms=elapsed_ms,
        revision=_canonical_revision(revision, elapsed_ms),
        facts=None,
        artifacts=(),
        warnings=(),
        evidence=(),
    )


def _failure(
    code: ToolResultCode,
    diagnostic: ToolDiagnosticClass,
    *,
    context: _ResultContext,
    metadata: _FailureMetadata | None = None,
) -> NormalizedToolOutcome:
    public_metadata = metadata or _FailureMetadata(_PUBLIC_MESSAGES[code])
    details = (
        {"tool_code": public_metadata.tool_code} if public_metadata.tool_code is not None else {}
    )
    error = StepError(
        category=ErrorCategory.RUNTIME,
        code=code.value,
        message=public_metadata.message,
        retryable=public_metadata.retryable,
        needs_input=public_metadata.needs_input,
        related_objects=public_metadata.related_objects,
        diagnostic_artifacts=public_metadata.diagnostic_artifacts,
        operation_id=context.operation_id,
        details=details,
    )
    result = StepResult(
        ok=False,
        value=None,
        elapsed_ms=context.elapsed_ms,
        operation_id=context.operation_id,
        revision=context.revision,
        facts=context.facts,
        artifacts=context.artifacts,
        warnings=context.warnings,
        evidence=context.evidence,
        error=error,
    )
    return NormalizedToolOutcome(result=result, diagnostic=diagnostic)


def _minimal_failure(
    code: ToolResultCode,
    diagnostic: ToolDiagnosticClass,
    *,
    operation_id: object,
    revision: object,
    elapsed_ms: int | float,
) -> NormalizedToolOutcome:
    return _failure(
        code,
        diagnostic,
        context=_minimal_context(
            operation_id=operation_id,
            revision=revision,
            elapsed_ms=elapsed_ms,
        ),
    )


def normalize_tool_result(
    raw: object,
    *,
    operation_id: str | None = None,
    elapsed_ms: int | float,
    revision: str | None = None,
    facts: Mapping[str, object] | None = None,
    artifacts: list[str] | tuple[str, ...] = (),
    warnings: list[str] | tuple[str, ...] = (),
    evidence: list[ExecutionEvidence] | tuple[ExecutionEvidence, ...] = (),
) -> NormalizedToolOutcome:
    """Normalize one returned tool value without executing any operation."""

    safe_elapsed = _safe_elapsed(elapsed_ms)
    if safe_elapsed is None:
        return _minimal_failure(
            ToolResultCode.INVALID_TOOL_RESULT,
            ToolDiagnosticClass.INVALID_RESULT,
            operation_id=operation_id,
            revision=revision,
            elapsed_ms=0,
        )

    try:
        context = _snapshot_context(
            operation_id=operation_id,
            elapsed_ms=safe_elapsed,
            revision=revision,
            facts=facts,
            artifacts=artifacts,
            warnings=warnings,
            evidence=evidence,
        )
    except Exception:
        return _minimal_failure(
            ToolResultCode.INVALID_TOOL_RESULT,
            ToolDiagnosticClass.INVALID_RESULT,
            operation_id=operation_id,
            revision=revision,
            elapsed_ms=safe_elapsed,
        )

    try:
        if isinstance(raw, Mapping):
            snapshot = _snapshot_mapping(raw)
            if snapshot is None:
                raise ValueError("invalid tool mapping")

            has_ok = "ok" in snapshot
            status = snapshot.get("ok")
            error_is_non_null = "error" in snapshot and snapshot["error"] is not None

            if has_ok and type(status) is not bool:
                return _failure(
                    ToolResultCode.INVALID_TOOL_RESULT,
                    ToolDiagnosticClass.INVALID_RESULT,
                    context=context,
                )

            if status is True and error_is_non_null:
                return _failure(
                    ToolResultCode.CONTRADICTORY_TOOL_RESULT,
                    ToolDiagnosticClass.CONTRADICTORY_RESULT,
                    context=context,
                )

            if status is False or error_is_non_null:
                return _failure(
                    ToolResultCode.TOOL_REPORTED_ERROR,
                    ToolDiagnosticClass.REPORTED_ERROR,
                    context=context,
                    metadata=_failure_metadata(
                        snapshot,
                        error_is_non_null=error_is_non_null,
                    ),
                )

            value: object = {
                key: item for key, item in snapshot.items() if key not in {"ok", "error"}
            }
        else:
            value = raw

        result = StepResult(
            ok=True,
            value=value,
            elapsed_ms=context.elapsed_ms,
            operation_id=context.operation_id,
            revision=context.revision,
            facts=context.facts,
            artifacts=context.artifacts,
            warnings=context.warnings,
            evidence=context.evidence,
            error=None,
        )
        return NormalizedToolOutcome(result=result)
    except Exception:
        return _failure(
            ToolResultCode.INVALID_TOOL_RESULT,
            ToolDiagnosticClass.INVALID_RESULT,
            context=context,
        )


def normalize_tool_exception(
    exc: Exception,
    *,
    operation_id: str | None = None,
    elapsed_ms: int | float,
    revision: str | None = None,
    facts: Mapping[str, object] | None = None,
    artifacts: list[str] | tuple[str, ...] = (),
    warnings: list[str] | tuple[str, ...] = (),
    evidence: list[ExecutionEvidence] | tuple[ExecutionEvidence, ...] = (),
) -> NormalizedToolOutcome:
    """Normalize an ordinary tool exception without reading its class name or text."""

    if isinstance(exc, BaseException) and not isinstance(exc, Exception):
        raise exc
    if not isinstance(exc, Exception):
        raise TypeError("exc must be an Exception")

    safe_elapsed = _safe_elapsed(elapsed_ms)
    if safe_elapsed is None:
        return _minimal_failure(
            ToolResultCode.INVALID_TOOL_RESULT,
            ToolDiagnosticClass.INVALID_RESULT,
            operation_id=operation_id,
            revision=revision,
            elapsed_ms=0,
        )

    try:
        context = _snapshot_context(
            operation_id=operation_id,
            elapsed_ms=safe_elapsed,
            revision=revision,
            facts=facts,
            artifacts=artifacts,
            warnings=warnings,
            evidence=evidence,
        )
    except Exception:
        return _minimal_failure(
            ToolResultCode.INVALID_TOOL_RESULT,
            ToolDiagnosticClass.INVALID_RESULT,
            operation_id=operation_id,
            revision=revision,
            elapsed_ms=safe_elapsed,
        )

    if isinstance(exc, TimeoutError):
        diagnostic = ToolDiagnosticClass.TIMEOUT_EXCEPTION
    elif isinstance(exc, ValueError):
        diagnostic = ToolDiagnosticClass.VALUE_EXCEPTION
    elif isinstance(exc, RuntimeError):
        diagnostic = ToolDiagnosticClass.RUNTIME_EXCEPTION
    else:
        diagnostic = ToolDiagnosticClass.OTHER_EXCEPTION

    return _failure(
        ToolResultCode.UNEXPECTED_TOOL_EXCEPTION,
        diagnostic,
        context=context,
    )


__all__ = [
    "NormalizedToolOutcome",
    "ToolDiagnosticClass",
    "ToolResultCode",
    "normalize_tool_exception",
    "normalize_tool_result",
]
