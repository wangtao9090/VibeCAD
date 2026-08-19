from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import vibecad.execution.freecad_sketch_bootstrap_verification as subject
from vibecad.execution.capabilities import CapabilityCatalogError
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_reviewed_family_capabilities import (
    CURRENT_FREECAD_REVIEWED_FAMILY_MANIFESTS,
)
from vibecad.execution.freecad_reviewed_verification import (
    REQUIRED_REVIEWED_CONFORMANCE_FACETS,
    ReviewedCaseManifestKind,
    ReviewedConformanceEvidenceKind,
    encode_reviewed_verification_receipt,
)
from vibecad.intent_bridge.reviewed_family_engine import ReviewedPlanReceipt
from vibecad.parametric.freecad_sketch_bootstrap_rules import (
    SKETCH_BOOTSTRAP_NATIVE_TYPE_ID,
    SketchBootstrapBackendPlan,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def test_reviewed_host_manifest_is_exact_one_by_seven_matrix() -> None:
    manifest = subject.SKETCH_BOOTSTRAP_CANDIDATE_FAMILY_MANIFEST
    cases = subject.SKETCH_BOOTSTRAP_REVIEWED_HOST_CASE_MANIFEST

    assert cases.manifest_kind is ReviewedCaseManifestKind.REVIEWED_HOST
    assert cases.family_manifest_sha256 == manifest.manifest_sha256
    assert len(manifest.operations) == 1
    assert len(cases.cases) == 7
    assert {(item.operation_id, item.facet) for item in cases.cases} == {
        (manifest.operations[0].operation_id, facet)
        for facet in REQUIRED_REVIEWED_CONFORMANCE_FACETS
    }
    assert len({item.case_sha256 for item in cases.cases}) == 7
    assert len({item.case_contract_sha256 for item in cases.cases}) == 7
    assert all(
        item.operation_specification_sha256 == manifest.operations[0].specification_sha256
        for item in cases.cases
    )

    with pytest.raises(CapabilityCatalogError):
        replace(
            cases,
            cases=(
                replace(cases.cases[0], case_contract_sha256="f" * 64),
                *cases.cases[1:],
            ),
        )


def test_internal_exact_adapter_lowering_returns_bound_plan_receipt() -> None:
    plan, payload, plan_document, receipt = subject._lower_exact_plan()  # noqa: SLF001

    assert type(plan) is SketchBootstrapBackendPlan
    assert type(receipt) is ReviewedPlanReceipt
    assert payload == plan.canonical_bytes
    assert plan.source_count == 0
    assert plan.plan_sha256 == plan_document.document_digest
    assert receipt.plan_document == plan_document
    assert receipt.operation is subject.SKETCH_BOOTSTRAP_CANDIDATE_FAMILY_MANIFEST.operations[0]
    assert receipt.executable is False
    assert receipt.grants_execution_authority is False


def test_original_bootstrap_stays_in_catalog126_but_inert_pending_attestation() -> None:
    candidate = subject.SKETCH_BOOTSTRAP_CANDIDATE_FORMAL_SPEC
    handoff = subject.SKETCH_BOOTSTRAP_FORMAL_VERIFICATION_HANDOFF
    current_specs = current_freecad_intent_capability_specs()

    assert len(current_specs) == 126
    assert len({item.operation_id for item in current_specs}) == 126
    assert candidate.operation_id == ("freecad_sketch_bootstrap.create_body_owned_closed_circle")
    assert candidate.native_type_id == SKETCH_BOOTSTRAP_NATIVE_TYPE_ID
    assert candidate.verification is None
    assert candidate == next(
        item for item in current_specs if item.operation_id == candidate.operation_id
    )
    assert subject.SKETCH_BOOTSTRAP_CANDIDATE_FAMILY_MANIFEST in (
        CURRENT_FREECAD_REVIEWED_FAMILY_MANIFESTS
    )
    assert handoff.future_formal_operation_count == 126
    assert handoff.future_reviewed_family_count == 21
    assert handoff.current_catalog_registered is True
    assert handoff.current_family_registered is True
    assert handoff.release_attestation_refreshed is False
    assert handoff.defaults_to_verified is False


def test_managed_builder_has_no_caller_result_pass_or_callback_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signature = inspect.signature(subject.build_sketch_bootstrap_managed_verification)
    assert tuple(signature.parameters) == ("freecad",)
    assert signature.parameters["freecad"].kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError):
        subject.build_sketch_bootstrap_managed_verification(
            freecad=object(),
            execute_case=lambda *_args: b"claimed-pass",
        )
    with pytest.raises(TypeError):
        subject.build_sketch_bootstrap_managed_verification(
            freecad=object(),
            results=("passed",),
        )
    with pytest.raises(TypeError):
        subject.build_sketch_bootstrap_managed_verification(
            freecad=object(),
            passed=True,
        )

    freecad = object()
    host = object()
    receipt = object()
    binding = object()
    captured: dict[str, object] = {}

    def fake_host_builder(**kwargs: object) -> object:
        captured.update(kwargs)
        return host

    def fake_receipt_builder(**kwargs: object) -> object:
        assert kwargs == {
            "manifest": subject.SKETCH_BOOTSTRAP_CANDIDATE_FAMILY_MANIFEST,
            "case_manifest": subject.SKETCH_BOOTSTRAP_REVIEWED_HOST_CASE_MANIFEST,
            "host": host,
        }
        return receipt

    def fake_binding_builder(value: object) -> object:
        assert value is receipt
        return binding

    monkeypatch.setattr(subject, "build_managed_freecad_conformance_host", fake_host_builder)
    monkeypatch.setattr(subject, "build_reviewed_verification_receipt", fake_receipt_builder)
    monkeypatch.setattr(subject, "build_promotion_verification_binding", fake_binding_builder)

    assert subject.build_sketch_bootstrap_managed_verification(freecad=freecad) == (
        receipt,
        binding,
    )
    assert captured["freecad"] is freecad
    assert type(captured["execute_case"]) is subject._SketchBootstrapExecutor  # noqa: SLF001
    assert captured["case_manifest"] is subject.SKETCH_BOOTSTRAP_REVIEWED_HOST_CASE_MANIFEST
    assert captured["verifier_id"] == subject.SKETCH_BOOTSTRAP_VERIFIER_ID
    assert captured["verifier_version"] == subject.SKETCH_BOOTSTRAP_VERIFIER_VERSION


def test_executor_builds_challenge_bound_observations_from_internal_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_run(_executor: object) -> dict[object, dict[str, object]]:
        nonlocal calls
        calls += 1
        return {
            facet: {"facet_proven": facet.value} for facet in REQUIRED_REVIEWED_CONFORMANCE_FACETS
        }

    monkeypatch.setattr(subject._SketchBootstrapExecutor, "_run", fake_run)  # noqa: SLF001
    executor = subject._SketchBootstrapExecutor(object())  # noqa: SLF001
    challenge = "a" * 64
    observations = [
        json.loads(executor(case, challenge))
        for case in subject.SKETCH_BOOTSTRAP_REVIEWED_HOST_CASE_MANIFEST.cases
    ]

    assert calls == 1
    assert {item["facet"] for item in observations} == {
        item.value for item in REQUIRED_REVIEWED_CONFORMANCE_FACETS
    }
    assert all(item["challenge_sha256"] == challenge for item in observations)
    assert all(item["authority"] == "none" for item in observations)
    assert all(item["evidence"] == {"facet_proven": item["facet"]} for item in observations)


def test_verification_module_import_is_freecad_and_gui_free() -> None:
    source_root = Path(__file__).parents[1] / "src"
    code = """
import sys
import vibecad.execution.freecad_sketch_bootstrap_verification
assert 'FreeCAD' not in sys.modules
assert 'FreeCADGui' not in sys.modules
print('SKETCH_BOOTSTRAP_VERIFICATION_IMPORT_OK')
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(source_root)},
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "SKETCH_BOOTSTRAP_VERIFICATION_IMPORT_OK" in completed.stdout


@pytest.mark.slow
def test_real_managed_freecad_110_sketch_bootstrap_receipt_and_binding() -> None:
    python_raw = os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON")
    if not python_raw:
        pytest.skip("managed FreeCAD Python was not requested")
    if Path(python_raw).resolve() != Path(sys.executable).resolve():
        pytest.fail("the test must run inside the requested managed FreeCAD Python")

    from vibecad.freecad_env import prepare_freecad_import

    prepare_freecad_import()
    import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

    assert tuple(FreeCAD.Version())[:3] == ("1", "1", "0")
    before_documents = FreeCAD.listDocuments()
    before_gui_up = FreeCAD.GuiUp
    before_gui_module = "FreeCADGui" in sys.modules
    before_specs = current_freecad_intent_capability_specs()

    receipt, binding = subject.build_sketch_bootstrap_managed_verification(freecad=FreeCAD)

    assert receipt.case_manifest == subject.SKETCH_BOOTSTRAP_REVIEWED_HOST_CASE_MANIFEST
    assert receipt.contract.evidence_kind is ReviewedConformanceEvidenceKind.MANAGED_FREECAD
    assert receipt.contract.family_manifest_sha256 == (
        subject.SKETCH_BOOTSTRAP_CANDIDATE_FAMILY_MANIFEST.manifest_sha256
    )
    assert len(receipt.results) == 7
    assert binding.runtime_build_sha256 == receipt.contract.runtime_backend.build_fingerprint_sha256
    assert binding.adapter_contract_sha256 == (
        subject.SKETCH_BOOTSTRAP_CANDIDATE_FAMILY_MANIFEST.adapter.adapter_contract_sha256
    )
    assert binding.test_contract_sha256 == receipt.test_contract_sha256
    assert binding.test_receipt_sha256 == receipt.test_receipt_sha256
    assert binding.test_receipt_size_bytes == receipt.test_receipt_size_bytes
    assert binding.verifier_id == subject.SKETCH_BOOTSTRAP_VERIFIER_ID
    assert binding.verifier_version == subject.SKETCH_BOOTSTRAP_VERIFIER_VERSION
    assert receipt.executable is False
    assert receipt.grants_execution_authority is False
    encoded = encode_reviewed_verification_receipt(receipt)
    assert _canonical(json.loads(encoded)) == encoded
    assert len(encoded) == receipt.test_receipt_size_bytes

    assert before_documents == FreeCAD.listDocuments() == {}
    assert before_gui_up == FreeCAD.GuiUp == 0
    assert before_gui_module is False
    assert "FreeCADGui" not in sys.modules
    assert current_freecad_intent_capability_specs() == before_specs
    assert all(item.verification is None for item in before_specs)
