"""Tests for pure conformance checks over admitted CAD runtime snapshots."""

from __future__ import annotations

import dataclasses

from vibecad.execution.selectors import (
    EntityKind,
    Provenance,
    ProvenanceSource,
    SelectorV1,
    SemanticRole,
)
from vibecad.interaction import cad_conformance as conformance_module
from vibecad.interaction.cad_conformance import (
    CadRuntimeAdmissionCase,
    CadRuntimeConformanceCase,
    evaluate_cad_runtime_admission,
    evaluate_cad_runtime_conformance,
)
from vibecad.interaction.cad_runtime import (
    CAD_EXECUTE_PROGRAM_V1,
    CadArtifactDeclaration,
    CadArtifactProfile,
    CadArtifactRole,
    CadNativeDecision,
    CadRuntimeAdapterRegistry,
    CadRuntimeDescriptor,
    CadRuntimeIdentity,
    CadRuntimeRouter,
    CadSelectorEnvelope,
    NativeLocator,
    NonExecutableCadDecisionError,
)
from vibecad.runtime.conformance import ConformanceFinding
from vibecad.runtime.contracts import (
    RuntimeArtifact,
    RuntimeCapability,
    RuntimeDescriptor,
    RuntimeIdentity,
)

_DIGEST = "b" * 64
_UNSUPPORTED = RuntimeCapability(name="authoring.unsupported", version=1)


def _identity(provider: str = "fixturecad") -> CadRuntimeIdentity:
    return CadRuntimeIdentity(
        runtime=RuntimeIdentity(family="cad", provider=provider, version="1.0")
    )


def _descriptor(
    identity: CadRuntimeIdentity | None = None,
    *,
    capabilities: tuple[RuntimeCapability, ...] = (CAD_EXECUTE_PROGRAM_V1,),
) -> CadRuntimeDescriptor:
    runtime = identity or _identity()
    return CadRuntimeDescriptor(
        runtime_descriptor=RuntimeDescriptor(
            identity=runtime.runtime,
            capabilities=capabilities,
            execution_profiles=("headless",),
        ),
        artifact_profile=CadArtifactProfile(
            runtime=runtime,
            declarations=(
                CadArtifactDeclaration(
                    runtime=runtime,
                    role=CadArtifactRole.NATIVE_MODEL,
                    kind="native_model",
                    media_type="application/vnd.fixturecad",
                    version=1,
                ),
                CadArtifactDeclaration(
                    runtime=runtime,
                    role=CadArtifactRole.EXCHANGE,
                    kind="exchange_model",
                    media_type="model/step",
                    version=1,
                ),
            ),
        ),
    )


class _Adapter:
    def __init__(self, descriptor: CadRuntimeDescriptor):
        self._descriptor = descriptor
        self.descriptor_reads = 0
        self.generation_reads = 0
        self.terminate_calls = 0
        self.close_calls = 0

    @property
    def runtime_descriptor(self):
        self.descriptor_reads += 1
        return self._descriptor

    @property
    def generation_lost(self):
        self.generation_reads += 1
        return False

    def terminate_generation(self):
        self.terminate_calls += 1

    def close_generation(self):
        self.close_calls += 1


def _selector(identity: CadRuntimeIdentity) -> CadSelectorEnvelope:
    semantic = SelectorV1(
        project_id="project_" + "1" * 32,
        revision_id="revision_" + "2" * 32,
        entity_kind=EntityKind.OBJECT,
        object_id="object_" + "3" * 32,
        feature_id=None,
        object_type="Part::Feature",
        semantic_role=SemanticRole.PART,
        provenance=Provenance(
            source=ProvenanceSource.MODEL,
            operation_id="operation-1",
        ),
    )
    return CadSelectorEnvelope(
        runtime=identity,
        semantic=semantic,
        native=NativeLocator(
            runtime=identity,
            revision_id=semantic.revision_id,
            scheme="subelement",
            reference="Face1",
        ),
    )


def _artifact(
    identity: CadRuntimeIdentity,
    *,
    artifact_id: str = "artifact-1",
    kind: str = "native_model",
    media_type: str = "application/vnd.fixturecad",
) -> RuntimeArtifact:
    return RuntimeArtifact(
        artifact_id=artifact_id,
        kind=kind,
        media_type=media_type,
        digest=_DIGEST,
        runtime=identity.runtime,
    )


def _case(
    case_id: str,
    registry: CadRuntimeAdapterRegistry,
    identity: CadRuntimeIdentity,
) -> CadRuntimeConformanceCase:
    return CadRuntimeConformanceCase(
        case_id=case_id,
        registry=registry,
        identity=identity,
        executable_request=CAD_EXECUTE_PROGRAM_V1,
        unsupported_request=_UNSUPPORTED,
        artifacts=(_artifact(identity),),
        selector=_selector(identity),
    )


def _codes(report) -> set[str]:
    return {item.code for item in report.findings}


def test_admission_delegates_once_per_case_and_records_only_stable_failures(
    monkeypatch,
) -> None:
    original_registry = conformance_module.CadRuntimeAdapterRegistry
    constructor_inputs: list[tuple[object, ...]] = []

    def observed_registry(adapters=()):
        snapshot = tuple(adapters)
        constructor_inputs.append(snapshot)
        return original_registry(snapshot)

    monkeypatch.setattr(
        conformance_module,
        "CadRuntimeAdapterRegistry",
        observed_registry,
    )
    good = _Adapter(_descriptor())

    class AuthorityAdapter(_Adapter):
        def commitRevision(self):
            self.close_calls += 1

        def advanceHEAD(self):
            self.terminate_calls += 1

    authority = AuthorityAdapter(_descriptor(_identity("authoritycad")))

    class BrokenMetadata(_Adapter):
        @property
        def runtime_descriptor(self):
            self.descriptor_reads += 1
            raise RuntimeError("/secret/provider/path should not escape")

    broken = BrokenMetadata(_descriptor(_identity("brokencad")))
    report = evaluate_cad_runtime_admission(
        (
            CadRuntimeAdmissionCase(case_id="good", adapter=good),
            CadRuntimeAdmissionCase(case_id="authority", adapter=authority),
            CadRuntimeAdmissionCase(case_id="broken", adapter=broken),
        )
    )

    assert len(constructor_inputs) == 3
    assert all(len(items) == 1 for items in constructor_inputs)
    assert {(item.case_id, item.code, item.subject) for item in report.findings} == {
        ("authority", "cad_admission_authority", "adapter"),
        ("broken", "cad_admission_failed", "adapter"),
    }
    assert all(
        "/secret/" not in value for item in report.findings for value in dataclasses.astuple(item)
    )
    assert good.descriptor_reads == good.generation_reads == 1
    assert authority.descriptor_reads == authority.generation_reads == 0
    assert authority.terminate_calls == authority.close_calls == 0
    assert broken.descriptor_reads == 1
    assert broken.terminate_calls == broken.close_calls == 0


def test_two_admitted_snapshots_route_exactly_without_provider_rereads_or_hooks() -> None:
    first_identity = _identity("firstcad")
    second_identity = _identity("secondcad")
    first = _Adapter(_descriptor(first_identity))
    second = _Adapter(_descriptor(second_identity))
    registry = CadRuntimeAdapterRegistry((second, first))
    reads_before = (
        first.descriptor_reads,
        first.generation_reads,
        second.descriptor_reads,
        second.generation_reads,
    )
    forward_cases = (
        _case("first", registry, first_identity),
        _case("second", registry, second_identity),
    )

    forward = evaluate_cad_runtime_conformance(forward_cases)
    reverse = evaluate_cad_runtime_conformance(reversed(forward_cases))

    assert forward.conforms is True
    assert forward == reverse
    assert (
        first.descriptor_reads,
        first.generation_reads,
        second.descriptor_reads,
        second.generation_reads,
    ) == reads_before
    assert first.terminate_calls == first.close_calls == 0
    assert second.terminate_calls == second.close_calls == 0


def test_unsupported_request_routes_to_exact_nonexecutable_error_without_hooks(
    monkeypatch,
) -> None:
    identity = _identity()
    adapter = _Adapter(_descriptor(identity))
    registry = CadRuntimeAdapterRegistry((adapter,))
    original_adapter_for = CadRuntimeRouter.adapter_for
    unsupported_calls = 0
    exact_error_calls = 0

    def observed_adapter_for(self, routed_identity, requested):
        nonlocal exact_error_calls, unsupported_calls
        if requested == _UNSUPPORTED:
            unsupported_calls += 1
        try:
            return original_adapter_for(self, routed_identity, requested)
        except NonExecutableCadDecisionError:
            if requested == _UNSUPPORTED:
                exact_error_calls += 1
            raise

    monkeypatch.setattr(CadRuntimeRouter, "adapter_for", observed_adapter_for)
    report = evaluate_cad_runtime_conformance((_case("unsupported", registry, identity),))

    assert report.conforms is True
    assert unsupported_calls == exact_error_calls == 1

    invalid = evaluate_cad_runtime_conformance(
        (
            dataclasses.replace(
                _case("invalid", registry, identity),
                unsupported_request=object(),
            ),
        )
    )
    assert _codes(invalid) == {"cad_unsupported_request_invalid"}

    def return_adapter(self, routed_identity, requested):
        if requested == _UNSUPPORTED:
            return adapter
        return original_adapter_for(self, routed_identity, requested)

    monkeypatch.setattr(CadRuntimeRouter, "adapter_for", return_adapter)
    returned = evaluate_cad_runtime_conformance((_case("route-returned", registry, identity),))
    assert _codes(returned) == {"cad_unsupported_route_returned"}

    def raise_wrong_error(self, routed_identity, requested):
        if requested == _UNSUPPORTED:
            raise LookupError("wrong stable error type")
        return original_adapter_for(self, routed_identity, requested)

    monkeypatch.setattr(CadRuntimeRouter, "adapter_for", raise_wrong_error)
    wrong_error = evaluate_cad_runtime_conformance((_case("wrong-error", registry, identity),))
    assert _codes(wrong_error) == {"cad_unsupported_route_wrong_error"}

    assert adapter.descriptor_reads == adapter.generation_reads == 1
    assert adapter.terminate_calls == adapter.close_calls == 0


def test_cad_findings_cover_route_unsupported_artifact_and_selector_fail_closed() -> None:
    identity = _identity()
    other = _identity("othercad")
    adapter = _Adapter(_descriptor(identity, capabilities=(CAD_EXECUTE_PROGRAM_V1, _UNSUPPORTED)))
    registry = CadRuntimeAdapterRegistry((adapter,))
    base = _case("base", registry, identity)
    mismatches = (
        dataclasses.replace(
            base,
            case_id="identity",
            identity=other,
            artifacts=(_artifact(other),),
            selector=_selector(other),
        ),
        dataclasses.replace(
            base,
            case_id="executable",
            executable_request=RuntimeCapability(name="authoring.missing", version=1),
        ),
        dataclasses.replace(
            base,
            case_id="unsupported",
            unsupported_request=_UNSUPPORTED,
        ),
        dataclasses.replace(
            base,
            case_id="artifacts",
            artifacts=(
                _artifact(other, artifact_id="wrong-runtime"),
                _artifact(identity, artifact_id="wrong-kind", kind="mesh_model"),
                _artifact(
                    identity,
                    artifact_id="wrong-media",
                    media_type="application/octet-stream",
                ),
            ),
        ),
        dataclasses.replace(
            base,
            case_id="selector",
            selector=base.selector.native,
        ),
    )

    report = evaluate_cad_runtime_conformance(reversed(mismatches))
    codes_by_case = {
        case_id: {item.code for item in report.findings if item.case_id == case_id}
        for case_id in {item.case_id for item in report.findings}
    }

    assert "cad_registry_identity_mismatch" in codes_by_case["identity"]
    assert "cad_executable_rejected" in codes_by_case["executable"]
    assert "cad_unsupported_accepted" in codes_by_case["unsupported"]
    assert "cad_artifact_runtime_mismatch" in codes_by_case["artifacts"]
    assert "cad_artifact_kind_undeclared" in codes_by_case["artifacts"]
    assert "cad_artifact_media_mismatch" in codes_by_case["artifacts"]
    assert "cad_selector_envelope_required" in codes_by_case["selector"]
    assert adapter.descriptor_reads == adapter.generation_reads == 1
    assert adapter.terminate_calls == adapter.close_calls == 0
    assert report.findings == tuple(
        sorted(report.findings, key=lambda item: (item.case_id, item.code, item.subject))
    )


def test_undeclared_selected_capability_is_rejected_without_adapter_hook(
    monkeypatch,
) -> None:
    identity = _identity()
    adapter = _Adapter(_descriptor(identity))
    registry = CadRuntimeAdapterRegistry((adapter,))
    undeclared = RuntimeCapability(name="authoring.undeclared", version=1)
    forged = object.__new__(CadNativeDecision)
    object.__setattr__(forged, "runtime", identity)
    object.__setattr__(forged, "requested", undeclared)
    original_plan = CadRuntimeDescriptor.plan

    def forged_plan(self, requested):
        if requested == CAD_EXECUTE_PROGRAM_V1:
            return forged
        return original_plan(self, requested)

    monkeypatch.setattr(CadRuntimeDescriptor, "plan", forged_plan)
    report = evaluate_cad_runtime_conformance((_case("forged", registry, identity),))

    assert _codes(report) == {"cad_undeclared_capability"}
    assert adapter.descriptor_reads == adapter.generation_reads == 1
    assert adapter.terminate_calls == adapter.close_calls == 0


def test_cad_bounds_duplicates_hostile_inputs_and_report_order_are_deterministic() -> None:
    identity = _identity()
    adapter = _Adapter(_descriptor(identity))
    registry = CadRuntimeAdapterRegistry((adapter,))
    base = _case("base", registry, identity)
    duplicate_a = dataclasses.replace(base, case_id="duplicate", registry=object())
    duplicate_b = dataclasses.replace(base, case_id="duplicate", artifacts=object())

    assert evaluate_cad_runtime_conformance(
        (duplicate_a, duplicate_b)
    ) == evaluate_cad_runtime_conformance((duplicate_b, duplicate_a))
    assert evaluate_cad_runtime_conformance((duplicate_a, duplicate_b)).findings == (
        ConformanceFinding(
            code="cad_duplicate_case_id",
            case_id="duplicate",
            subject="case_id",
        ),
    )

    class HostileCases:
        def __iter__(self):
            raise RuntimeError("hostile cases")

    hostile = evaluate_cad_runtime_conformance(HostileCases())
    assert hostile.findings == (
        ConformanceFinding(
            code="cad_case_collection_invalid",
            case_id="case-collection",
            subject="cases",
        ),
    )

    class EndlessCases:
        def __iter__(self):
            return self

        def __next__(self):
            return base

    endless = evaluate_cad_runtime_conformance(EndlessCases())
    assert _codes(endless) == {"cad_case_limit_exceeded"}

    class EndlessArtifacts:
        def __iter__(self):
            return self

        def __next__(self):
            return _artifact(identity)

    artifact_bound = evaluate_cad_runtime_conformance(
        (dataclasses.replace(base, case_id="artifact-bound", artifacts=EndlessArtifacts()),)
    )
    assert _codes(artifact_bound) == {"cad_artifact_limit_exceeded"}

    bad_artifacts = (
        _artifact(_identity("wrong"), artifact_id="wrong-runtime"),
        _artifact(identity, artifact_id="wrong-kind", kind="mesh_model"),
        _artifact(
            identity,
            artifact_id="wrong-media",
            media_type="application/octet-stream",
        ),
    )
    noisy_adapter = _Adapter(
        _descriptor(
            identity,
            capabilities=(CAD_EXECUTE_PROGRAM_V1, _UNSUPPORTED),
        )
    )
    noisy_base = _case(
        "noisy-base",
        CadRuntimeAdapterRegistry((noisy_adapter,)),
        identity,
    )
    noisy = tuple(
        dataclasses.replace(
            noisy_base,
            case_id=f"noisy-{index:02d}",
            executable_request=_UNSUPPORTED,
            unsupported_request=_UNSUPPORTED,
            artifacts=bad_artifacts,
            selector=base.selector.native,
        )
        for index in range(32)
    )
    bounded = evaluate_cad_runtime_conformance(noisy)
    assert len(bounded.findings) == 128
    assert "cad_finding_limit_exceeded" in _codes(bounded)
    assert bounded.findings == tuple(
        sorted(bounded.findings, key=lambda item: (item.case_id, item.code, item.subject))
    )


def test_admission_duplicate_ids_are_not_evaluated() -> None:
    identity = _identity()

    class AuthorityAdapter(_Adapter):
        def reviewTask(self):
            self.close_calls += 1

    first = AuthorityAdapter(_descriptor(identity))
    second = AuthorityAdapter(_descriptor(_identity("other")))
    report = evaluate_cad_runtime_admission(
        (
            CadRuntimeAdmissionCase(case_id="duplicate", adapter=first),
            CadRuntimeAdmissionCase(case_id="duplicate", adapter=second),
        )
    )

    assert report.findings == (
        ConformanceFinding(
            code="cad_duplicate_case_id",
            case_id="duplicate",
            subject="case_id",
        ),
    )
    assert first.descriptor_reads == second.descriptor_reads == 0
    assert first.terminate_calls == first.close_calls == 0
    assert second.terminate_calls == second.close_calls == 0


def test_admission_overflow_is_atomic_before_registry_or_provider_metadata(
    monkeypatch,
) -> None:
    original_registry = conformance_module.CadRuntimeAdapterRegistry
    registry_constructor_calls = 0

    def observed_registry(adapters=()):
        nonlocal registry_constructor_calls
        registry_constructor_calls += 1
        return original_registry(adapters)

    monkeypatch.setattr(
        conformance_module,
        "CadRuntimeAdapterRegistry",
        observed_registry,
    )
    adapter = _Adapter(_descriptor())
    cases = tuple(
        CadRuntimeAdmissionCase(case_id=f"overflow-{index:02d}", adapter=adapter)
        for index in range(33)
    )

    report = evaluate_cad_runtime_admission(cases)

    assert report.findings == (
        ConformanceFinding(
            code="cad_case_limit_exceeded",
            case_id="case-limit",
            subject="cases",
        ),
    )
    assert registry_constructor_calls == 0
    assert adapter.descriptor_reads == adapter.generation_reads == 0
    assert adapter.terminate_calls == adapter.close_calls == 0
