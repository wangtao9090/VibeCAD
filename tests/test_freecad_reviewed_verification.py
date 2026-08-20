from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from vibecad.execution import freecad_reviewed_verification as verification
from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
)
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_discovery_runtime_v2 import (
    FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
    collect_managed_freecad_discovery_v2,
)
from vibecad.execution.freecad_reviewed_verification import (
    MAX_REVIEWED_CONFORMANCE_CASES,
    MAX_REVIEWED_OBSERVATION_BYTES,
    REQUIRED_REVIEWED_CONFORMANCE_FACETS,
    REVIEWED_VERIFICATION_SCHEMA_VERSION,
    ReviewedCaseManifestKind,
    ReviewedConformanceCase,
    ReviewedConformanceCaseManifest,
    ReviewedConformanceEvidenceKind,
    ReviewedVerificationReceipt,
    build_deterministic_synthetic_conformance_host,
    build_managed_freecad_conformance_host,
    build_promotion_verification_binding,
    build_reviewed_family_conformance_case_manifest,
    build_reviewed_verification_receipt,
    encode_reviewed_conformance_case_manifest,
    encode_reviewed_verification_receipt,
    encode_reviewed_verification_test_contract,
)
from vibecad.intent_bridge.freecad_part_core_adapter import PART_CORE_MANIFEST
from vibecad.intent_bridge.freecad_partdesign_residual_adapter import (
    PARTDESIGN_RESIDUAL_MANIFEST,
)


def _sha(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _backend(label: str = "managed-build") -> CapabilityBackend:
    return CapabilityBackend(
        backend_id="freecad",
        backend_version=(1, 1, 0),
        build_fingerprint_sha256=_sha(label),
        platform_id="macos.arm64",
        discovery_profile=CapabilityExecutionProfile.HEADLESS,
    )


def _synthetic_receipt():
    case_manifest = build_reviewed_family_conformance_case_manifest(PARTDESIGN_RESIDUAL_MANIFEST)
    host = build_deterministic_synthetic_conformance_host(
        runtime_backend=_backend(),
        case_manifest=case_manifest,
    )
    return (
        case_manifest,
        host,
        build_reviewed_verification_receipt(
            manifest=PARTDESIGN_RESIDUAL_MANIFEST,
            case_manifest=case_manifest,
            host=host,
        ),
    )


def _private_test_host(base, *, execute_case=None, guard=None, revalidate=None):
    return verification._ReviewedConformanceHost._create(  # noqa: SLF001
        runtime_backend=base.runtime_backend,
        case_manifest_sha256=base.case_manifest_sha256,
        evidence_kind=base.evidence_kind,
        verifier_id=base.verifier_id,
        verifier_version=base.verifier_version,
        execute_case=execute_case or base.execute_case,
        guard=guard or base.guard,
        revalidate=revalidate or base.revalidate,
        builder_token=verification._HOST_BUILDER_TOKEN,  # noqa: SLF001
    )


@pytest.mark.parametrize(
    ("gui_up", "documents", "gui_module", "expected_path"),
    (
        (True, {}, False, "host/runtime/gui"),
        (0, (), False, "host/runtime/documents/type"),
        (0, {"Open": object()}, False, "host/runtime/documents/open"),
        (0, {}, True, "host/runtime/gui_module"),
    ),
)
def test_managed_headless_guard_reports_exact_failed_invariant(
    monkeypatch: pytest.MonkeyPatch,
    gui_up: object,
    documents: object,
    gui_module: bool,
    expected_path: str,
) -> None:
    class FakeFreeCAD:
        GuiUp = gui_up

        @staticmethod
        def listDocuments():
            return documents

    if gui_module:
        monkeypatch.setitem(sys.modules, "FreeCADGui", object())
    else:
        monkeypatch.delitem(sys.modules, "FreeCADGui", raising=False)
    with pytest.raises(CapabilityCatalogError) as caught:
        verification._require_managed_headless_empty(FakeFreeCAD())  # noqa: SLF001
    assert caught.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    assert caught.value.path == expected_path


def test_guard_failure_note_records_exact_case_boundary() -> None:
    case_manifest = build_reviewed_family_conformance_case_manifest(PARTDESIGN_RESIDUAL_MANIFEST)
    base = build_deterministic_synthetic_conformance_host(
        runtime_backend=_backend(),
        case_manifest=case_manifest,
    )
    calls = 0

    def guard() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            verification._fail(  # noqa: SLF001
                CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                "host/runtime/documents/open",
            )

    host = _private_test_host(base, guard=guard)
    with pytest.raises(CapabilityCatalogError) as caught:
        build_reviewed_verification_receipt(
            manifest=PARTDESIGN_RESIDUAL_MANIFEST,
            case_manifest=case_manifest,
            host=host,
        )
    assert caught.value.path == "host/runtime/documents/open"
    assert caught.value.__notes__ == [
        "reviewed host guard failed at host/guard/0: host/runtime/documents/open"
    ]


def test_real_family_descriptors_build_complete_exact_matrix() -> None:
    case_manifest = build_reviewed_family_conformance_case_manifest(PART_CORE_MANIFEST)

    assert len(case_manifest.cases) == len(PART_CORE_MANIFEST.operations) * len(
        REQUIRED_REVIEWED_CONFORMANCE_FACETS
    )
    expected_specs = {
        item.operation_id: item.specification_sha256 for item in PART_CORE_MANIFEST.operations
    }
    matrix = {(item.operation_id, item.facet) for item in case_manifest.cases}
    assert matrix == {
        (operation_id, facet)
        for operation_id in expected_specs
        for facet in REQUIRED_REVIEWED_CONFORMANCE_FACETS
    }
    assert all(
        item.operation_specification_sha256 == expected_specs[item.operation_id]
        for item in case_manifest.cases
    )
    encoded = json.loads(encode_reviewed_conformance_case_manifest(case_manifest))
    assert encoded["family_manifest_sha256"] == PART_CORE_MANIFEST.manifest_sha256
    assert encoded["case_manifest_sha256"] == case_manifest.case_manifest_sha256


def test_synthetic_host_builds_deterministic_receipt_but_cannot_verify() -> None:
    case_manifest, _host, forward = _synthetic_receipt()
    reversed_manifest = ReviewedConformanceCaseManifest(
        schema_version=REVIEWED_VERIFICATION_SCHEMA_VERSION,
        family_manifest_sha256=case_manifest.family_manifest_sha256,
        manifest_kind=case_manifest.manifest_kind,
        cases=tuple(reversed(case_manifest.cases)),
    )
    reverse_host = build_deterministic_synthetic_conformance_host(
        runtime_backend=_backend(),
        case_manifest=reversed_manifest,
    )
    reverse = build_reviewed_verification_receipt(
        manifest=PARTDESIGN_RESIDUAL_MANIFEST,
        case_manifest=reversed_manifest,
        host=reverse_host,
    )

    assert forward.test_contract_sha256 == reverse.test_contract_sha256
    assert forward.test_receipt_sha256 == reverse.test_receipt_sha256
    assert encode_reviewed_verification_receipt(forward) == (
        encode_reviewed_verification_receipt(reverse)
    )
    assert encode_reviewed_verification_test_contract(forward.contract) == (
        forward.contract.canonical_bytes
    )
    assert forward.test_receipt_size_bytes == len(forward.canonical_bytes)
    assert forward.contract.evidence_kind is ReviewedConformanceEvidenceKind.SYNTHETIC
    assert forward.contract.family_manifest_sha256 == PARTDESIGN_RESIDUAL_MANIFEST.manifest_sha256
    assert (
        forward.contract.adapter_contract_sha256
        == PARTDESIGN_RESIDUAL_MANIFEST.adapter.adapter_contract_sha256
    )
    assert (
        forward.contract.rule_contract_sha256 == PARTDESIGN_RESIDUAL_MANIFEST.rule_contract_sha256
    )
    assert {item.operation_specification_sha256 for item in forward.contract.operations} == {
        item.specification_sha256 for item in PARTDESIGN_RESIDUAL_MANIFEST.operations
    }
    assert forward.executable is False
    assert forward.grants_execution_authority is False
    assert all(item.verification is None for item in current_freecad_intent_capability_specs())
    with pytest.raises(CapabilityCatalogError) as caught:
        build_promotion_verification_binding(forward)
    assert caught.value.code is CapabilityCatalogErrorCode.INVALID_STATUS


def test_host_revalidates_family_operation_matrix_and_admitted_case_manifest() -> None:
    case_manifest, host, _receipt = _synthetic_receipt()
    wrong_family = ReviewedConformanceCaseManifest(
        schema_version=REVIEWED_VERIFICATION_SCHEMA_VERSION,
        family_manifest_sha256="f" * 64,
        manifest_kind=case_manifest.manifest_kind,
        cases=case_manifest.cases,
    )
    wrong_family_host = build_deterministic_synthetic_conformance_host(
        runtime_backend=_backend(),
        case_manifest=wrong_family,
    )
    with pytest.raises(CapabilityCatalogError) as family_error:
        build_reviewed_verification_receipt(
            manifest=PARTDESIGN_RESIDUAL_MANIFEST,
            case_manifest=wrong_family,
            host=wrong_family_host,
        )
    assert family_error.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE

    missing = ReviewedConformanceCaseManifest(
        schema_version=REVIEWED_VERIFICATION_SCHEMA_VERSION,
        family_manifest_sha256=case_manifest.family_manifest_sha256,
        manifest_kind=case_manifest.manifest_kind,
        cases=case_manifest.cases[:-1],
    )
    missing_host = build_deterministic_synthetic_conformance_host(
        runtime_backend=_backend(),
        case_manifest=missing,
    )
    with pytest.raises(CapabilityCatalogError) as matrix_error:
        build_reviewed_verification_receipt(
            manifest=PARTDESIGN_RESIDUAL_MANIFEST,
            case_manifest=missing,
            host=missing_host,
        )
    assert matrix_error.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE

    first = case_manifest.cases[0]
    rebound = replace(first, operation_specification_sha256="e" * 64)
    rebound_manifest = ReviewedConformanceCaseManifest(
        schema_version=REVIEWED_VERIFICATION_SCHEMA_VERSION,
        family_manifest_sha256=case_manifest.family_manifest_sha256,
        manifest_kind=case_manifest.manifest_kind,
        cases=(rebound, *case_manifest.cases[1:]),
    )
    rebound_host = build_deterministic_synthetic_conformance_host(
        runtime_backend=_backend(),
        case_manifest=rebound_manifest,
    )
    with pytest.raises(CapabilityCatalogError) as operation_error:
        build_reviewed_verification_receipt(
            manifest=PARTDESIGN_RESIDUAL_MANIFEST,
            case_manifest=rebound_manifest,
            host=rebound_host,
        )
    assert operation_error.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE

    substituted = replace(first, case_contract_sha256="d" * 64)
    substituted_manifest = ReviewedConformanceCaseManifest(
        schema_version=REVIEWED_VERIFICATION_SCHEMA_VERSION,
        family_manifest_sha256=case_manifest.family_manifest_sha256,
        manifest_kind=case_manifest.manifest_kind,
        cases=(substituted, *case_manifest.cases[1:]),
    )
    with pytest.raises(CapabilityCatalogError) as substitution_error:
        build_reviewed_verification_receipt(
            manifest=PARTDESIGN_RESIDUAL_MANIFEST,
            case_manifest=substituted_manifest,
            host=host,
        )
    assert substitution_error.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE


def test_results_are_host_generated_bounded_and_runtime_revalidated() -> None:
    case_manifest = build_reviewed_family_conformance_case_manifest(PARTDESIGN_RESIDUAL_MANIFEST)
    base = build_deterministic_synthetic_conformance_host(
        runtime_backend=_backend(),
        case_manifest=case_manifest,
    )
    exact = _private_test_host(
        base,
        execute_case=lambda _case, _challenge: b"x" * MAX_REVIEWED_OBSERVATION_BYTES,
    )
    receipt = build_reviewed_verification_receipt(
        manifest=PARTDESIGN_RESIDUAL_MANIFEST,
        case_manifest=case_manifest,
        host=exact,
    )
    assert all(
        item.observation_size_bytes == MAX_REVIEWED_OBSERVATION_BYTES for item in receipt.results
    )

    oversized = _private_test_host(
        base,
        execute_case=lambda _case, _challenge: b"x" * (MAX_REVIEWED_OBSERVATION_BYTES + 1),
    )
    with pytest.raises(CapabilityCatalogError) as budget_error:
        build_reviewed_verification_receipt(
            manifest=PARTDESIGN_RESIDUAL_MANIFEST,
            case_manifest=case_manifest,
            host=oversized,
        )
    assert budget_error.value.code is CapabilityCatalogErrorCode.BUDGET_EXCEEDED

    drifted = _private_test_host(base, revalidate=lambda: _backend("drifted"))
    with pytest.raises(CapabilityCatalogError) as runtime_error:
        build_reviewed_verification_receipt(
            manifest=PARTDESIGN_RESIDUAL_MANIFEST,
            case_manifest=case_manifest,
            host=drifted,
        )
    assert runtime_error.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE

    def exits(_case, _challenge):
        raise SystemExit("untrusted detail")

    hostile = _private_test_host(base, execute_case=exits)
    with pytest.raises(CapabilityCatalogError) as hostile_error:
        build_reviewed_verification_receipt(
            manifest=PARTDESIGN_RESIDUAL_MANIFEST,
            case_manifest=case_manifest,
            host=hostile,
        )
    assert hostile_error.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    assert hostile_error.value.path == "host/results/0"
    assert "untrusted detail" not in str(hostile_error.value)


def _bounded_case(index: int) -> ReviewedConformanceCase:
    facet = REQUIRED_REVIEWED_CONFORMANCE_FACETS[index % len(REQUIRED_REVIEWED_CONFORMANCE_FACETS)]
    operation_index = index // len(REQUIRED_REVIEWED_CONFORMANCE_FACETS)
    return ReviewedConformanceCase(
        case_id=f"case.{index:04d}",
        operation_id=f"operation.{operation_index:03d}",
        operation_specification_sha256=_sha(f"operation-{operation_index}"),
        facet=facet,
        case_contract_sha256=_sha(f"case-{index}"),
    )


def test_case_manifest_n_and_n_plus_one_are_bounded() -> None:
    cases = tuple(_bounded_case(index) for index in range(MAX_REVIEWED_CONFORMANCE_CASES))
    manifest = ReviewedConformanceCaseManifest(
        schema_version=REVIEWED_VERIFICATION_SCHEMA_VERSION,
        family_manifest_sha256=_sha("family"),
        manifest_kind=ReviewedCaseManifestKind.SYNTHETIC,
        cases=cases,
    )
    assert len(manifest.cases) == MAX_REVIEWED_CONFORMANCE_CASES
    with pytest.raises(CapabilityCatalogError) as caught:
        ReviewedConformanceCaseManifest(
            schema_version=REVIEWED_VERIFICATION_SCHEMA_VERSION,
            family_manifest_sha256=_sha("family"),
            manifest_kind=ReviewedCaseManifestKind.SYNTHETIC,
            cases=(*cases, _bounded_case(MAX_REVIEWED_CONFORMANCE_CASES)),
        )
    assert caught.value.code is CapabilityCatalogErrorCode.BUDGET_EXCEEDED


def test_ordinary_callers_cannot_construct_or_submit_receipts() -> None:
    case_manifest, _host, receipt = _synthetic_receipt()
    with pytest.raises(CapabilityCatalogError) as admission_error:
        ReviewedConformanceCaseManifest(
            schema_version=REVIEWED_VERIFICATION_SCHEMA_VERSION,
            family_manifest_sha256=case_manifest.family_manifest_sha256,
            manifest_kind=ReviewedCaseManifestKind.REVIEWED_HOST,
            cases=case_manifest.cases,
        )
    assert admission_error.value.code is CapabilityCatalogErrorCode.INVALID_INPUT

    reviewed_host_manifest = verification._admit_reviewed_host_conformance_case_manifest(  # noqa: SLF001
        manifest=PARTDESIGN_RESIDUAL_MANIFEST,
        cases=case_manifest.cases,
    )
    assert reviewed_host_manifest.manifest_kind is ReviewedCaseManifestKind.REVIEWED_HOST
    assert reviewed_host_manifest.case_manifest_sha256 != case_manifest.case_manifest_sha256
    with pytest.raises(CapabilityCatalogError) as synthetic_host_error:
        build_deterministic_synthetic_conformance_host(
            runtime_backend=_backend(),
            case_manifest=reviewed_host_manifest,
        )
    assert synthetic_host_error.value.code is CapabilityCatalogErrorCode.INVALID_STATUS
    with pytest.raises(CapabilityCatalogError) as replaced_admission_error:
        replace(
            reviewed_host_manifest,
            cases=(
                replace(reviewed_host_manifest.cases[0], case_contract_sha256="c" * 64),
                *reviewed_host_manifest.cases[1:],
            ),
        )
    assert replaced_admission_error.value.code is CapabilityCatalogErrorCode.INVALID_INPUT

    mismatched_evidence_host = verification._ReviewedConformanceHost._create(  # noqa: SLF001
        runtime_backend=_backend(),
        case_manifest_sha256=case_manifest.case_manifest_sha256,
        evidence_kind=ReviewedConformanceEvidenceKind.MANAGED_FREECAD,
        verifier_id="vcad.test.mismatched-evidence",
        verifier_version="1.0.0",
        execute_case=lambda _case, _challenge: b"claimed-pass",
        guard=lambda: None,
        revalidate=lambda: _backend(),
        builder_token=verification._HOST_BUILDER_TOKEN,  # noqa: SLF001
    )
    with pytest.raises(CapabilityCatalogError) as evidence_error:
        build_reviewed_verification_receipt(
            manifest=PARTDESIGN_RESIDUAL_MANIFEST,
            case_manifest=case_manifest,
            host=mismatched_evidence_host,
        )
    assert evidence_error.value.code is CapabilityCatalogErrorCode.INVALID_STATUS

    with pytest.raises(TypeError):
        ReviewedVerificationReceipt(  # type: ignore[call-arg]
            contract=receipt.contract,
            case_manifest=case_manifest,
            results=receipt.results,
        )
    with pytest.raises(CapabilityCatalogError) as token_error:
        ReviewedVerificationReceipt._create(  # noqa: SLF001
            contract=receipt.contract,
            case_manifest=case_manifest,
            results=receipt.results,
            builder_token=object(),
        )
    assert token_error.value.code is CapabilityCatalogErrorCode.INVALID_INPUT

    substituted_contract = replace(
        receipt.contract,
        evidence_kind=ReviewedConformanceEvidenceKind.MANAGED_FREECAD,
    )
    with pytest.raises(CapabilityCatalogError) as challenge_error:
        ReviewedVerificationReceipt._create(  # noqa: SLF001
            contract=substituted_contract,
            case_manifest=case_manifest,
            results=receipt.results,
            builder_token=verification._RECEIPT_BUILDER_TOKEN,  # noqa: SLF001
        )
    assert challenge_error.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE

    with pytest.raises(CapabilityCatalogError) as host_error:
        build_reviewed_verification_receipt(
            manifest=PARTDESIGN_RESIDUAL_MANIFEST,
            case_manifest=case_manifest,
            host=object(),
        )
    assert host_error.value.code is CapabilityCatalogErrorCode.INVALID_INPUT


@pytest.mark.slow
def test_real_managed_freecad_runtime_smoke_remains_synthetic_and_unpromoted() -> None:
    python_raw = os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON")
    if not python_raw:
        pytest.skip("managed FreeCAD Python was not requested")
    if Path(python_raw).resolve() != Path(sys.executable).resolve():
        pytest.fail("the test must run inside the requested managed FreeCAD Python")

    from vibecad.freecad_env import prepare_freecad_import

    prepare_freecad_import()
    import FreeCAD  # noqa: PLC0415

    before_documents = FreeCAD.listDocuments()
    before_gui_up = FreeCAD.GuiUp
    bundle = collect_managed_freecad_discovery_v2(
        freecad=FreeCAD,
        probe_modules=FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
    )
    case_manifest = build_reviewed_family_conformance_case_manifest(PARTDESIGN_RESIDUAL_MANIFEST)
    with pytest.raises(CapabilityCatalogError) as managed_error:
        build_managed_freecad_conformance_host(
            freecad=FreeCAD,
            case_manifest=case_manifest,
            execute_case=lambda _case, _challenge: pytest.fail(
                "synthetic cases must never enter the managed host"
            ),
        )
    assert managed_error.value.code is CapabilityCatalogErrorCode.INVALID_STATUS
    reviewed_case_manifest = verification._admit_reviewed_host_conformance_case_manifest(  # noqa: SLF001
        manifest=PARTDESIGN_RESIDUAL_MANIFEST,
        cases=case_manifest.cases,
    )
    managed_host = build_managed_freecad_conformance_host(
        freecad=FreeCAD,
        case_manifest=reviewed_case_manifest,
        execute_case=lambda _case, _challenge: pytest.fail(
            "runtime smoke must not manufacture managed case results"
        ),
    )
    host = build_deterministic_synthetic_conformance_host(
        runtime_backend=bundle.snapshot.backend,
        case_manifest=case_manifest,
    )
    receipt = build_reviewed_verification_receipt(
        manifest=PARTDESIGN_RESIDUAL_MANIFEST,
        case_manifest=case_manifest,
        host=host,
    )

    assert bundle.manifest.type_count == 449
    assert managed_host.runtime_backend == bundle.snapshot.backend
    assert managed_host.evidence_kind is ReviewedConformanceEvidenceKind.MANAGED_FREECAD
    assert receipt.contract.runtime_backend == bundle.snapshot.backend
    assert receipt.contract.evidence_kind is ReviewedConformanceEvidenceKind.SYNTHETIC
    assert before_documents == FreeCAD.listDocuments() == {}
    assert before_gui_up == FreeCAD.GuiUp == 0
    assert "FreeCADGui" not in sys.modules
    with pytest.raises(CapabilityCatalogError) as caught:
        build_promotion_verification_binding(receipt)
    assert caught.value.code is CapabilityCatalogErrorCode.INVALID_STATUS
