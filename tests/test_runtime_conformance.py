"""Tests for the bounded, provider-free generic runtime conformance kit."""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from vibecad.runtime.conformance import (
    ConformanceFinding,
    ConformanceReport,
    RuntimeCancellationTranscript,
    RuntimeConformanceCase,
    RuntimeSuccessTranscript,
    evaluate_runtime_conformance,
)
from vibecad.runtime.contracts import (
    RuntimeArtifact,
    RuntimeBudget,
    RuntimeCapability,
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeHealthState,
    RuntimeIdentity,
    RuntimeInvocation,
    RuntimeLifecycleState,
    RuntimeProvenance,
    RuntimeResult,
    RuntimeStatus,
)

_DIGEST = "a" * 64
_CAPABILITY = RuntimeCapability(name="authoring.execute_program", version=1)


def _identity(provider: str = "fixture") -> RuntimeIdentity:
    return RuntimeIdentity(family="cad", provider=provider, version="1.0")


def _descriptor(identity: RuntimeIdentity | None = None) -> RuntimeDescriptor:
    return RuntimeDescriptor(
        identity=identity or _identity(),
        capabilities=(_CAPABILITY,),
        execution_profiles=("headless",),
    )


def _artifact(
    artifact_id: str,
    *,
    identity: RuntimeIdentity | None = None,
    kind: str = "native_model",
    media_type: str = "application/vnd.fixture",
) -> RuntimeArtifact:
    return RuntimeArtifact(
        artifact_id=artifact_id,
        kind=kind,
        media_type=media_type,
        digest=_DIGEST,
        runtime=identity or _identity(),
    )


def _invocation(
    invocation_id: str,
    *,
    identity: RuntimeIdentity | None = None,
    capability: RuntimeCapability = _CAPABILITY,
    execution_profile: str = "headless",
) -> RuntimeInvocation:
    runtime = identity or _identity()
    return RuntimeInvocation(
        invocation_id=invocation_id,
        owner_id="owner-1",
        task_id="task-1",
        runtime=runtime,
        capability=capability,
        budget=RuntimeBudget(
            max_elapsed_ms=1_000,
            max_memory_bytes=1_000_000,
            max_output_bytes=10_000,
        ),
        deadline_ms=2_000,
        input_artifacts=(_artifact(f"{invocation_id}-input", identity=runtime),),
        execution_profile=execution_profile,
    )


class _DeterministicControl:
    constructor_calls = 0

    def __init__(self):
        type(self).constructor_calls += 1
        self.calls: list[tuple[str, str]] = []
        self.invocations: dict[str, RuntimeInvocation] = {}
        self.result_calls = 0

    def start(self, invocation):
        self.calls.append(("start", invocation.invocation_id))
        self.invocations[invocation.invocation_id] = invocation
        return RuntimeStatus(
            invocation_id=invocation.invocation_id,
            runtime=invocation.runtime,
            state=RuntimeLifecycleState.PENDING,
        )

    def get_status(self, invocation_id):
        self.calls.append(("get_status", invocation_id))
        invocation = self.invocations[invocation_id]
        return RuntimeStatus(
            invocation_id=invocation_id,
            runtime=invocation.runtime,
            state=RuntimeLifecycleState.SUCCEEDED,
        )

    def cancel(self, invocation_id, *, reason):
        self.calls.append(("cancel", reason))
        invocation = self.invocations[invocation_id]
        return RuntimeStatus(
            invocation_id=invocation_id,
            runtime=invocation.runtime,
            state=RuntimeLifecycleState.CANCELLED,
        )

    def reconcile(self, invocation_id):
        self.calls.append(("reconcile", invocation_id))
        invocation = self.invocations[invocation_id]
        return RuntimeStatus(
            invocation_id=invocation_id,
            runtime=invocation.runtime,
            state=RuntimeLifecycleState.CANCELLED,
        )

    def health(self, identity):
        self.calls.append(("health", identity.key))
        return RuntimeHealth(runtime=identity, state=RuntimeHealthState.HEALTHY)

    def result_fixture(self, invocation_id):
        self.result_calls += 1
        invocation = self.invocations[invocation_id]
        return RuntimeResult(
            invocation_id=invocation_id,
            runtime=invocation.runtime,
            state=RuntimeLifecycleState.SUCCEEDED,
            artifacts=(_artifact(f"{invocation_id}-output", identity=invocation.runtime),),
            provenance=RuntimeProvenance(
                runtime=invocation.runtime,
                invocation_id=invocation_id,
                input_artifact_ids=tuple(item.artifact_id for item in invocation.input_artifacts),
            ),
        )


def _exercised_case(
    case_id: str = "runtime-good",
) -> tuple[RuntimeConformanceCase, _DeterministicControl]:
    control = _DeterministicControl()
    success_invocation = _invocation("success-1")
    cancellation_invocation = _invocation("cancel-1")

    success_start = control.start(success_invocation)
    success_final = control.get_status(success_invocation.invocation_id)
    concrete_result = control.result_fixture(success_invocation.invocation_id)
    cancellation_start = control.start(cancellation_invocation)
    cancellation_status = control.cancel(
        cancellation_invocation.invocation_id,
        reason="test-requested",
    )
    reconciled = control.reconcile(cancellation_invocation.invocation_id)
    health = control.health(success_invocation.runtime)

    case = RuntimeConformanceCase(
        case_id=case_id,
        descriptor=_descriptor(),
        control_class=_DeterministicControl,
        success=RuntimeSuccessTranscript(
            invocation=success_invocation,
            start_status=success_start,
            final_status=success_final,
            result=concrete_result,
        ),
        cancellation=RuntimeCancellationTranscript(
            invocation=cancellation_invocation,
            start_status=cancellation_start,
            cancel_status=cancellation_status,
            reconciled_status=reconciled,
        ),
        health=health,
    )
    return case, control


def _codes(report: ConformanceReport) -> set[str]:
    return {finding.code for finding in report.findings}


def test_transcript_evaluator_accepts_real_fake_lifecycle_without_calling_provider() -> None:
    before_constructors = _DeterministicControl.constructor_calls
    case, control = _exercised_case()
    calls_before_evaluation = tuple(control.calls)

    report = evaluate_runtime_conformance((case,))

    assert report == ConformanceReport(findings=())
    assert report.conforms is True
    assert _DeterministicControl.constructor_calls == before_constructors + 1
    assert tuple(control.calls) == calls_before_evaluation
    assert control.result_calls == 1
    assert calls_before_evaluation == (
        ("start", "success-1"),
        ("get_status", "success-1"),
        ("start", "cancel-1"),
        ("cancel", "test-requested"),
        ("reconcile", "cancel-1"),
        ("health", "cad/fixture@1.0"),
    )
    assert case.success.result.invocation_id == "success-1"
    assert case.success.result.provenance == RuntimeProvenance(
        runtime=_identity(),
        invocation_id="success-1",
        input_artifact_ids=("success-1-input",),
    )


def test_static_control_inspection_rejects_shape_and_authority_without_execution() -> None:
    case, _ = _exercised_case()
    constructor_calls = 0
    method_calls = 0

    class MissingHealth:
        def start(self, invocation):
            return invocation

        def get_status(self, invocation_id):
            return invocation_id

        def cancel(self, invocation_id, *, reason):
            return invocation_id, reason

        def reconcile(self, invocation_id):
            return invocation_id

    class WrongCancel(_DeterministicControl):
        def cancel(self, invocation_id, reason):
            return invocation_id, reason

    class AsyncReconcile(_DeterministicControl):
        async def reconcile(self, invocation_id):
            return invocation_id

    class AuthorityControl(_DeterministicControl):
        def __init__(self):
            nonlocal constructor_calls
            constructor_calls += 1

        def advanceHEAD(self):
            nonlocal method_calls
            method_calls += 1

        def commit_revision(self):
            nonlocal method_calls
            method_calls += 1

        def result(self):
            nonlocal method_calls
            method_calls += 1

    cases = (
        dataclasses.replace(case, case_id="missing", control_class=MissingHealth),
        dataclasses.replace(case, case_id="wrong", control_class=WrongCancel),
        dataclasses.replace(case, case_id="async", control_class=AsyncReconcile),
        dataclasses.replace(case, case_id="authority", control_class=AuthorityControl),
    )

    report = evaluate_runtime_conformance(cases)
    observed = {(item.case_id, item.code, item.subject) for item in report.findings}

    assert ("missing", "control_method_missing", "health") in observed
    assert ("wrong", "control_method_signature", "cancel") in observed
    assert ("async", "control_method_async", "reconcile") in observed
    assert ("authority", "control_forbidden_authority", "commit") in observed
    assert ("authority", "control_forbidden_authority", "head") in observed
    assert all(item.subject != "result" for item in report.findings)
    assert constructor_calls == method_calls == 0


def test_static_control_inspection_rejects_a_spoofed_custom_signature() -> None:
    case, _ = _exercised_case()
    constructor_calls = 0
    method_calls = 0

    class SignatureSpoof:
        def __init__(self):
            nonlocal constructor_calls
            constructor_calls += 1

        def start(self, invocation):
            nonlocal method_calls
            method_calls += 1
            return invocation

        def get_status(self, invocation_id):
            nonlocal method_calls
            method_calls += 1
            return invocation_id

        def cancel(self, wrong_name, extra_positional):
            nonlocal method_calls
            method_calls += 1
            return wrong_name, extra_positional

        def reconcile(self, invocation_id):
            nonlocal method_calls
            method_calls += 1
            return invocation_id

        def health(self, identity):
            nonlocal method_calls
            method_calls += 1
            return identity

    SignatureSpoof.cancel.__signature__ = inspect.Signature(
        parameters=(
            inspect.Parameter(
                "self",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ),
            inspect.Parameter(
                "invocation_id",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ),
            inspect.Parameter(
                "reason",
                inspect.Parameter.KEYWORD_ONLY,
            ),
        )
    )

    with pytest.raises(TypeError):
        SignatureSpoof.cancel(object(), "invocation-1", reason="test-requested")
    assert constructor_calls == method_calls == 0

    report = evaluate_runtime_conformance(
        (
            dataclasses.replace(
                case,
                case_id="signature-spoof",
                control_class=SignatureSpoof,
            ),
        )
    )

    assert (report.conforms, report.findings) == (
        False,
        (
            ConformanceFinding(
                code="control_method_signature",
                case_id="signature-spoof",
                subject="cancel",
            ),
        ),
    )
    assert constructor_calls == method_calls == 0


def test_generic_semantic_findings_cover_exact_correlations_and_transitions() -> None:
    case, _ = _exercised_case()
    other_identity = _identity("other")
    other_capability = RuntimeCapability(name="authoring.other", version=1)
    mismatches = (
        dataclasses.replace(
            case,
            case_id="identity",
            success=dataclasses.replace(
                case.success,
                invocation=dataclasses.replace(
                    case.success.invocation,
                    runtime=other_identity,
                ),
            ),
        ),
        dataclasses.replace(
            case,
            case_id="capability",
            success=dataclasses.replace(
                case.success,
                invocation=dataclasses.replace(
                    case.success.invocation,
                    capability=other_capability,
                ),
            ),
        ),
        dataclasses.replace(
            case,
            case_id="profile",
            success=dataclasses.replace(
                case.success,
                invocation=dataclasses.replace(
                    case.success.invocation,
                    execution_profile="interactive",
                ),
            ),
        ),
        dataclasses.replace(
            case,
            case_id="start",
            success=dataclasses.replace(
                case.success,
                start_status=dataclasses.replace(
                    case.success.start_status,
                    state=RuntimeLifecycleState.FAILED,
                ),
            ),
        ),
        dataclasses.replace(
            case,
            case_id="final",
            success=dataclasses.replace(
                case.success,
                final_status=dataclasses.replace(
                    case.success.final_status,
                    state=RuntimeLifecycleState.CANCELLED,
                ),
            ),
        ),
        dataclasses.replace(
            case,
            case_id="result",
            success=dataclasses.replace(
                case.success,
                result=dataclasses.replace(
                    case.success.result,
                    invocation_id="other-result",
                    provenance=RuntimeProvenance(
                        runtime=_identity(),
                        invocation_id="other-result",
                        input_artifact_ids=("success-1-input",),
                    ),
                ),
            ),
        ),
        dataclasses.replace(
            case,
            case_id="provenance",
            success=dataclasses.replace(
                case.success,
                result=dataclasses.replace(
                    case.success.result,
                    provenance=RuntimeProvenance(
                        runtime=_identity(),
                        invocation_id="success-1",
                        input_artifact_ids=("wrong-input",),
                    ),
                ),
            ),
        ),
        dataclasses.replace(
            case,
            case_id="cancel-distinct",
            cancellation=dataclasses.replace(
                case.cancellation,
                invocation=dataclasses.replace(
                    case.cancellation.invocation,
                    invocation_id=case.success.invocation.invocation_id,
                ),
                start_status=dataclasses.replace(
                    case.cancellation.start_status,
                    invocation_id=case.success.invocation.invocation_id,
                ),
                cancel_status=dataclasses.replace(
                    case.cancellation.cancel_status,
                    invocation_id=case.success.invocation.invocation_id,
                ),
                reconciled_status=dataclasses.replace(
                    case.cancellation.reconciled_status,
                    invocation_id=case.success.invocation.invocation_id,
                ),
            ),
        ),
        dataclasses.replace(
            case,
            case_id="cancel-state",
            cancellation=dataclasses.replace(
                case.cancellation,
                cancel_status=dataclasses.replace(
                    case.cancellation.cancel_status,
                    state=RuntimeLifecycleState.RUNNING,
                ),
                reconciled_status=dataclasses.replace(
                    case.cancellation.reconciled_status,
                    state=RuntimeLifecycleState.SUCCEEDED,
                ),
            ),
        ),
        dataclasses.replace(
            case,
            case_id="health",
            health=RuntimeHealth(
                runtime=other_identity,
                state=RuntimeHealthState.HEALTHY,
            ),
        ),
    )

    report = evaluate_runtime_conformance(reversed(mismatches))
    codes_by_case = {
        case_id: {item.code for item in report.findings if item.case_id == case_id}
        for case_id in {item.case_id for item in report.findings}
    }

    assert "runtime_identity_mismatch" in codes_by_case["identity"]
    assert "runtime_capability_mismatch" in codes_by_case["capability"]
    assert "runtime_profile_mismatch" in codes_by_case["profile"]
    assert "success_start_not_accepted" in codes_by_case["start"]
    assert "success_final_not_succeeded" in codes_by_case["final"]
    assert "result_invocation_mismatch" in codes_by_case["result"]
    assert "result_provenance_mismatch" in codes_by_case["provenance"]
    assert "cancellation_invocation_not_distinct" in codes_by_case["cancel-distinct"]
    assert "cancellation_not_cancelled" in codes_by_case["cancel-state"]
    assert "cancellation_reconcile_not_cancelled" in codes_by_case["cancel-state"]
    assert "health_identity_mismatch" in codes_by_case["health"]
    assert report.findings == tuple(
        sorted(report.findings, key=lambda item: (item.case_id, item.code, item.subject))
    )


def test_case_overflow_is_atomic_before_semantics_and_reads_only_max_plus_one() -> None:
    case, _ = _exercised_case()
    poisoned = tuple(
        dataclasses.replace(
            case,
            case_id=f"overflow-{index:02d}",
            descriptor=object(),
            control_class=object(),
            success=object(),
            cancellation=object(),
            health=object(),
        )
        for index in range(32)
    )

    class GuardedOverflow:
        def __init__(self):
            self.reads = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.reads += 1
            if self.reads <= 32:
                return poisoned[self.reads - 1]
            if self.reads == 33:
                return object()
            raise AssertionError("case evaluator requested an item beyond MAX+1")

    cases = GuardedOverflow()
    report = evaluate_runtime_conformance(cases)

    assert cases.reads == 33
    assert report.findings == (
        ConformanceFinding(
            code="case_limit_exceeded",
            case_id="case-limit",
            subject="cases",
        ),
    )


def test_case_and_finding_bounds_duplicates_and_hostile_inputs_are_deterministic() -> None:
    case, _ = _exercised_case()
    duplicate_a = dataclasses.replace(
        case,
        case_id="duplicate",
        descriptor=object(),
        control_class=object(),
    )
    duplicate_b = dataclasses.replace(
        case,
        case_id="duplicate",
        success=object(),
        cancellation=object(),
    )

    forward = evaluate_runtime_conformance((duplicate_a, duplicate_b))
    reverse = evaluate_runtime_conformance((duplicate_b, duplicate_a))

    assert forward == reverse
    assert forward.findings == (
        ConformanceFinding(
            code="duplicate_case_id",
            case_id="duplicate",
            subject="case_id",
        ),
    )

    invalid_id = evaluate_runtime_conformance((dataclasses.replace(case, case_id="not valid"),))
    assert invalid_id.findings[0].code == "invalid_case_id"
    assert invalid_id.findings[0].case_id == "case-0001"

    class HostileCases:
        def __iter__(self):
            raise RuntimeError("provider path and repr must not escape")

    hostile = evaluate_runtime_conformance(HostileCases())
    assert hostile.findings == (
        ConformanceFinding(
            code="case_collection_invalid",
            case_id="case-collection",
            subject="cases",
        ),
    )

    class EndlessCases:
        def __iter__(self):
            return self

        def __next__(self):
            return case

    endless = evaluate_runtime_conformance(EndlessCases())
    assert _codes(endless) == {"case_limit_exceeded"}
    assert len(endless.findings) <= 128

    class EmptyControl:
        pass

    noisy = tuple(
        dataclasses.replace(
            case,
            case_id=f"noisy-{index:02d}",
            control_class=EmptyControl,
            descriptor=object(),
            success=object(),
            cancellation=object(),
            health=object(),
        )
        for index in range(32)
    )
    bounded = evaluate_runtime_conformance(noisy)
    assert len(bounded.findings) == 128
    assert "finding_limit_exceeded" in _codes(bounded)
    assert bounded.findings == tuple(
        sorted(bounded.findings, key=lambda item: (item.case_id, item.code, item.subject))
    )


def test_conformance_values_are_frozen_slotted_and_reports_compute_conforms() -> None:
    case, _ = _exercised_case()
    values = (
        ConformanceFinding(code="invalid_case", case_id="case-0001", subject="case"),
        ConformanceReport(findings=()),
        case.success,
        case.cancellation,
        case,
    )

    assert all(dataclasses.is_dataclass(value) for value in values)
    assert all(not hasattr(value, "__dict__") for value in values)
    assert ConformanceReport(findings=()).conforms is True
    assert (
        ConformanceReport(
            findings=(
                ConformanceFinding(
                    code="invalid_case",
                    case_id="case-0001",
                    subject="case",
                ),
            )
        ).conforms
        is False
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        case.case_id = "changed"
