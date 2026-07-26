"""Bounded transcript conformance checks for generic runtime adapters.

The evaluator in this module consumes immutable observations.  It inspects a
control class statically and never constructs it or invokes provider code.
"""

from __future__ import annotations

import inspect
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from vibecad.runtime.contracts import (
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeInvocation,
    RuntimeLifecycleState,
    RuntimeResult,
    RuntimeStatus,
)

_MAX_CASES = 32
_MAX_FINDINGS = 128
_MAX_CASE_ID_LENGTH = 64
_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_FINDING_TEXT = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_WORD_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_IDENTIFIER_TOKEN = re.compile(r"[A-Za-z0-9]+")
_FORBIDDEN_AUTHORITY_TOKENS = frozenset(
    {
        "accept",
        "commit",
        "head",
        "reject",
        "review",
    }
)
_CONTROL_SIGNATURES = {
    "start": (
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("invocation", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ),
    "get_status": (
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("invocation_id", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ),
    "cancel": (
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("invocation_id", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("reason", inspect.Parameter.KEYWORD_ONLY),
    ),
    "reconcile": (
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("invocation_id", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ),
    "health": (
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("identity", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ),
}


def _finding_text(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) > _MAX_CASE_ID_LENGTH
        or _FINDING_TEXT.fullmatch(value) is None
    ):
        raise ValueError(f"{name} must be fixed bounded ASCII contract text")
    return value


def _valid_case_id(value: object) -> bool:
    return (
        type(value) is str
        and len(value) <= _MAX_CASE_ID_LENGTH
        and _CASE_ID.fullmatch(value) is not None
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ConformanceFinding:
    """One stable machine-readable conformance finding."""

    code: str
    case_id: str
    subject: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _finding_text(self.code, "code"))
        if not _valid_case_id(self.case_id):
            raise ValueError("case_id must be bounded ASCII contract text")
        object.__setattr__(self, "subject", _finding_text(self.subject, "subject"))


def _snapshot_findings(findings: object) -> tuple[ConformanceFinding, ...]:
    if isinstance(findings, (str, bytes, bytearray)):
        raise TypeError("findings must be an iterable")
    try:
        iterator = iter(findings)  # type: ignore[arg-type]
    except Exception as exc:
        raise ValueError("findings could not be enumerated") from exc
    result: list[ConformanceFinding] = []
    for index in range(_MAX_FINDINGS + 1):
        try:
            finding = next(iterator)
        except StopIteration:
            break
        except Exception as exc:
            raise ValueError("findings could not be enumerated") from exc
        if index == _MAX_FINDINGS:
            raise ValueError("findings exceed the maximum of 128")
        if type(finding) is not ConformanceFinding:
            raise TypeError("findings must contain only ConformanceFinding values")
        result.append(finding)
    return tuple(sorted(result, key=lambda item: (item.case_id, item.code, item.subject)))


@dataclass(frozen=True, slots=True, kw_only=True)
class ConformanceReport:
    """Bounded deterministic conformance result."""

    findings: tuple[ConformanceFinding, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", _snapshot_findings(self.findings))

    @property
    def conforms(self) -> bool:
        return not self.findings


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeSuccessTranscript:
    """Caller-supplied observations for one successful invocation."""

    invocation: RuntimeInvocation
    start_status: RuntimeStatus
    final_status: RuntimeStatus
    result: RuntimeResult


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeCancellationTranscript:
    """Caller-supplied observations for a distinct cancelled invocation."""

    invocation: RuntimeInvocation
    start_status: RuntimeStatus
    cancel_status: RuntimeStatus
    reconciled_status: RuntimeStatus


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeConformanceCase:
    """One immutable generic runtime transcript case."""

    case_id: str
    descriptor: RuntimeDescriptor
    control_class: type
    success: RuntimeSuccessTranscript
    cancellation: RuntimeCancellationTranscript
    health: RuntimeHealth


class _FindingCollector:
    __slots__ = ("_findings", "_limit_code", "_overflow")

    def __init__(self, limit_code: str) -> None:
        self._findings: list[ConformanceFinding] = []
        self._limit_code = limit_code
        self._overflow = False

    def add(self, code: str, case_id: str, subject: str) -> None:
        finding = ConformanceFinding(code=code, case_id=case_id, subject=subject)
        if len(self._findings) < _MAX_FINDINGS - 1:
            self._findings.append(finding)
        else:
            self._overflow = True

    def extend(self, findings: Iterable[ConformanceFinding]) -> None:
        for finding in findings:
            self.add(finding.code, finding.case_id, finding.subject)

    def report(self) -> ConformanceReport:
        if self._overflow:
            self._findings.append(
                ConformanceFinding(
                    code=self._limit_code,
                    case_id="report-limit",
                    subject="findings",
                )
            )
        return ConformanceReport(findings=tuple(self._findings))


def _prepare_cases(
    cases: object,
    *,
    case_type: type,
    code_prefix: str = "",
) -> tuple[
    tuple[tuple[str, object], ...],
    tuple[ConformanceFinding, ...],
]:
    def code(name: str) -> str:
        return f"{code_prefix}{name}"

    try:
        if isinstance(cases, (str, bytes, bytearray)):
            raise TypeError
        iterator = iter(cases)  # type: ignore[arg-type]
    except Exception:
        return (
            (),
            (
                ConformanceFinding(
                    code=code("case_collection_invalid"),
                    case_id="case-collection",
                    subject="cases",
                ),
            ),
        )

    snapshot: list[object] = []
    try:
        for index in range(_MAX_CASES + 1):
            try:
                item = next(iterator)
            except StopIteration:
                break
            if index == _MAX_CASES:
                return (
                    (),
                    (
                        ConformanceFinding(
                            code=code("case_limit_exceeded"),
                            case_id="case-limit",
                            subject="cases",
                        ),
                    ),
                )
            snapshot.append(item)
    except Exception:
        return (
            (),
            (
                ConformanceFinding(
                    code=code("case_collection_invalid"),
                    case_id="case-collection",
                    subject="cases",
                ),
            ),
        )

    findings: list[ConformanceFinding] = []
    candidates: list[tuple[str, object]] = []
    for ordinal, item in enumerate(snapshot, start=1):
        fallback = f"case-{ordinal:04d}"
        if type(item) is not case_type:
            findings.append(
                ConformanceFinding(
                    code=code("invalid_case"),
                    case_id=fallback,
                    subject="case",
                )
            )
            continue
        case_id = item.case_id
        if not _valid_case_id(case_id):
            findings.append(
                ConformanceFinding(
                    code=code("invalid_case_id"),
                    case_id=fallback,
                    subject="case_id",
                )
            )
            continue
        candidates.append((case_id, item))

    counts = Counter(case_id for case_id, _ in candidates)
    duplicates = sorted(case_id for case_id, count in counts.items() if count > 1)
    findings.extend(
        ConformanceFinding(
            code=code("duplicate_case_id"),
            case_id=case_id,
            subject="case_id",
        )
        for case_id in duplicates
    )
    prepared = tuple(
        sorted(
            ((case_id, item) for case_id, item in candidates if counts[case_id] == 1),
            key=lambda item: item[0],
        )
    )
    return prepared, tuple(findings)


def _identifier_tokens(name: str) -> tuple[str, ...]:
    separated = _CAMEL_ACRONYM_BOUNDARY.sub(r"\1_\2", name)
    separated = _CAMEL_WORD_BOUNDARY.sub(r"\1_\2", separated)
    return tuple(item.lower() for item in _IDENTIFIER_TOKEN.findall(separated))


def _class_namespace_value(control_class: type, name: str) -> object | None:
    for owner in type.__getattribute__(control_class, "__mro__"):
        namespace = type.__getattribute__(owner, "__dict__")
        if name in namespace:
            return namespace[name]
    return None


def _inspect_control_class(
    control_class: object,
    *,
    case_id: str,
    findings: _FindingCollector,
) -> None:
    if type(control_class) is not type:
        findings.add("control_class_invalid", case_id, "control_class")
        return

    public_names: set[str] = set()
    for owner in type.__getattribute__(control_class, "__mro__"):
        namespace = type.__getattribute__(owner, "__dict__")
        public_names.update(
            name for name in namespace if type(name) is str and not name.startswith("_")
        )
    authority_tokens = {
        token
        for name in public_names
        for token in _identifier_tokens(name)
        if token in _FORBIDDEN_AUTHORITY_TOKENS
    }
    for token in sorted(authority_tokens):
        findings.add("control_forbidden_authority", case_id, token)

    for name, expected in _CONTROL_SIGNATURES.items():
        value = _class_namespace_value(control_class, name)
        if value is None:
            findings.add("control_method_missing", case_id, name)
            continue
        if inspect.iscoroutinefunction(value):
            findings.add("control_method_async", case_id, name)
            continue
        if not inspect.isfunction(value):
            findings.add("control_method_signature", case_id, name)
            continue
        try:
            custom_signature = object.__getattribute__(value, "__signature__")
        except AttributeError:
            custom_signature = None
        if custom_signature is not None:
            findings.add("control_method_signature", case_id, name)
            continue
        try:
            parameters = tuple(
                inspect.signature(
                    value,
                    follow_wrapped=False,
                    eval_str=False,
                ).parameters.values()
            )
        except (TypeError, ValueError):
            findings.add("control_method_signature", case_id, name)
            continue
        actual = tuple((parameter.name, parameter.kind) for parameter in parameters)
        if actual != expected or any(
            parameter.default is not inspect.Parameter.empty for parameter in parameters
        ):
            findings.add("control_method_signature", case_id, name)


def _check_invocation_contract(
    invocation: RuntimeInvocation,
    descriptor: RuntimeDescriptor,
    *,
    case_id: str,
    subject: str,
    findings: _FindingCollector,
) -> None:
    if invocation.runtime != descriptor.identity:
        findings.add("runtime_identity_mismatch", case_id, subject)
    if invocation.capability not in descriptor.capabilities:
        findings.add("runtime_capability_mismatch", case_id, subject)
    profile_matches = (
        invocation.execution_profile in descriptor.execution_profiles
        if descriptor.execution_profiles
        else invocation.execution_profile is None
    )
    if not profile_matches:
        findings.add("runtime_profile_mismatch", case_id, subject)


def _check_status_correlation(
    status: RuntimeStatus,
    invocation: RuntimeInvocation,
    *,
    case_id: str,
    code_prefix: str,
    subject: str,
    findings: _FindingCollector,
) -> None:
    if status.invocation_id != invocation.invocation_id:
        findings.add(f"{code_prefix}_invocation_mismatch", case_id, subject)
    if status.runtime != invocation.runtime:
        findings.add(f"{code_prefix}_runtime_mismatch", case_id, subject)


def _evaluate_success(
    success: object,
    descriptor: RuntimeDescriptor | None,
    *,
    case_id: str,
    findings: _FindingCollector,
) -> RuntimeInvocation | None:
    if type(success) is not RuntimeSuccessTranscript:
        findings.add("success_transcript_invalid", case_id, "success")
        return None

    invocation = success.invocation
    start_status = success.start_status
    final_status = success.final_status
    result = success.result
    if type(invocation) is not RuntimeInvocation:
        findings.add("success_invocation_invalid", case_id, "success.invocation")
        invocation = None
    if type(start_status) is not RuntimeStatus:
        findings.add("success_start_status_invalid", case_id, "success.start_status")
        start_status = None
    if type(final_status) is not RuntimeStatus:
        findings.add("success_final_status_invalid", case_id, "success.final_status")
        final_status = None
    if type(result) is not RuntimeResult:
        findings.add("success_result_invalid", case_id, "success.result")
        result = None

    if invocation is not None and descriptor is not None:
        _check_invocation_contract(
            invocation,
            descriptor,
            case_id=case_id,
            subject="success.invocation",
            findings=findings,
        )
    if invocation is not None and start_status is not None:
        _check_status_correlation(
            start_status,
            invocation,
            case_id=case_id,
            code_prefix="success_start",
            subject="success.start_status",
            findings=findings,
        )
        if start_status.state not in {
            RuntimeLifecycleState.PENDING,
            RuntimeLifecycleState.RUNNING,
        }:
            findings.add("success_start_not_accepted", case_id, "success.start_status")
    if invocation is not None and final_status is not None:
        _check_status_correlation(
            final_status,
            invocation,
            case_id=case_id,
            code_prefix="success_final",
            subject="success.final_status",
            findings=findings,
        )
        if final_status.state is not RuntimeLifecycleState.SUCCEEDED:
            findings.add("success_final_not_succeeded", case_id, "success.final_status")
    if invocation is not None and result is not None:
        if result.invocation_id != invocation.invocation_id:
            findings.add("result_invocation_mismatch", case_id, "success.result")
        if result.runtime != invocation.runtime:
            findings.add("result_runtime_mismatch", case_id, "success.result")
        if result.state is not RuntimeLifecycleState.SUCCEEDED:
            findings.add("result_state_mismatch", case_id, "success.result")
        expected_inputs = tuple(sorted(item.artifact_id for item in invocation.input_artifacts))
        provenance = result.provenance
        if (
            provenance is None
            or provenance.runtime != invocation.runtime
            or provenance.invocation_id != invocation.invocation_id
            or provenance.input_artifact_ids != expected_inputs
        ):
            findings.add("result_provenance_mismatch", case_id, "success.result")
        if any(item.runtime != invocation.runtime for item in result.artifacts):
            findings.add("result_artifact_runtime_mismatch", case_id, "success.result")
    if final_status is not None and result is not None and result.state != final_status.state:
        findings.add("result_final_state_mismatch", case_id, "success.result")
    return invocation


def _evaluate_cancellation(
    cancellation: object,
    descriptor: RuntimeDescriptor | None,
    success_invocation: RuntimeInvocation | None,
    *,
    case_id: str,
    findings: _FindingCollector,
) -> None:
    if type(cancellation) is not RuntimeCancellationTranscript:
        findings.add("cancellation_transcript_invalid", case_id, "cancellation")
        return

    invocation = cancellation.invocation
    statuses = (
        ("start", cancellation.start_status),
        ("cancel", cancellation.cancel_status),
        ("reconcile", cancellation.reconciled_status),
    )
    if type(invocation) is not RuntimeInvocation:
        findings.add(
            "cancellation_invocation_invalid",
            case_id,
            "cancellation.invocation",
        )
        invocation = None
    valid_statuses: dict[str, RuntimeStatus] = {}
    for name, status in statuses:
        if type(status) is not RuntimeStatus:
            findings.add(
                f"cancellation_{name}_status_invalid",
                case_id,
                f"cancellation.{name}_status",
            )
        else:
            valid_statuses[name] = status

    if invocation is None:
        return
    if descriptor is not None:
        _check_invocation_contract(
            invocation,
            descriptor,
            case_id=case_id,
            subject="cancellation.invocation",
            findings=findings,
        )
    if (
        success_invocation is not None
        and invocation.invocation_id == success_invocation.invocation_id
    ):
        findings.add(
            "cancellation_invocation_not_distinct",
            case_id,
            "cancellation.invocation",
        )
    for name, status in valid_statuses.items():
        _check_status_correlation(
            status,
            invocation,
            case_id=case_id,
            code_prefix=f"cancellation_{name}",
            subject=f"cancellation.{name}_status",
            findings=findings,
        )
    start = valid_statuses.get("start")
    if start is not None and start.state not in {
        RuntimeLifecycleState.PENDING,
        RuntimeLifecycleState.RUNNING,
    }:
        findings.add(
            "cancellation_start_not_accepted",
            case_id,
            "cancellation.start_status",
        )
    cancelled = valid_statuses.get("cancel")
    if cancelled is not None and cancelled.state is not RuntimeLifecycleState.CANCELLED:
        findings.add(
            "cancellation_not_cancelled",
            case_id,
            "cancellation.cancel_status",
        )
    reconciled = valid_statuses.get("reconcile")
    if reconciled is not None and reconciled.state is not RuntimeLifecycleState.CANCELLED:
        findings.add(
            "cancellation_reconcile_not_cancelled",
            case_id,
            "cancellation.reconciled_status",
        )


def _evaluate_case(
    case_id: str,
    case: RuntimeConformanceCase,
    findings: _FindingCollector,
) -> None:
    descriptor = case.descriptor
    if type(descriptor) is not RuntimeDescriptor:
        findings.add("descriptor_invalid", case_id, "descriptor")
        descriptor = None

    _inspect_control_class(
        case.control_class,
        case_id=case_id,
        findings=findings,
    )
    success_invocation = _evaluate_success(
        case.success,
        descriptor,
        case_id=case_id,
        findings=findings,
    )
    _evaluate_cancellation(
        case.cancellation,
        descriptor,
        success_invocation,
        case_id=case_id,
        findings=findings,
    )
    if type(case.health) is not RuntimeHealth:
        findings.add("health_invalid", case_id, "health")
    elif descriptor is not None and case.health.runtime != descriptor.identity:
        findings.add("health_identity_mismatch", case_id, "health")


def evaluate_runtime_conformance(cases: object) -> ConformanceReport:
    """Evaluate bounded immutable transcripts without executing a provider."""

    prepared, initial = _prepare_cases(
        cases,
        case_type=RuntimeConformanceCase,
    )
    findings = _FindingCollector("finding_limit_exceeded")
    findings.extend(initial)
    for case_id, value in prepared:
        _evaluate_case(case_id, value, findings)  # type: ignore[arg-type]
    return findings.report()
