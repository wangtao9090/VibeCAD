from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityExecutionProfile,
)
from vibecad.execution.freecad_reviewed_verification import (
    REQUIRED_REVIEWED_CONFORMANCE_FACETS,
    ReviewedCaseManifestKind,
    ReviewedConformanceEvidenceKind,
)
from vibecad.execution.freecad_reviewed_verification_wave_d import (
    WAVE_D_FAMILY_MANIFESTS,
    WAVE_D_REVIEWED_CASE_DESCRIPTORS,
    WAVE_D_REVIEWED_CASE_MANIFESTS,
    WAVE_D_VERIFIER_CONTRACT_SHA256,
    WaveDReviewedCaseDescriptor,
    build_managed_freecad_wave_d_verification,
)


def test_wave_d_descriptors_close_exact_seven_by_seven_reviewed_matrix() -> None:
    assert tuple(len(item.operations) for item in WAVE_D_FAMILY_MANIFESTS) == (3, 3, 1)
    assert len(WAVE_D_REVIEWED_CASE_DESCRIPTORS) == 7 * 7
    assert len({item.case.case_sha256 for item in WAVE_D_REVIEWED_CASE_DESCRIPTORS}) == 49
    assert len({item.case_contract_sha256 for item in WAVE_D_REVIEWED_CASE_DESCRIPTORS}) == 49
    assert len({item.fixture_bundle_sha256 for item in WAVE_D_REVIEWED_CASE_DESCRIPTORS}) == 7
    assert len(WAVE_D_VERIFIER_CONTRACT_SHA256) == 64
    for manifest, case_manifest in zip(
        WAVE_D_FAMILY_MANIFESTS,
        WAVE_D_REVIEWED_CASE_MANIFESTS,
        strict=True,
    ):
        assert case_manifest.manifest_kind is ReviewedCaseManifestKind.REVIEWED_HOST
        assert case_manifest.family_manifest_sha256 == manifest.manifest_sha256
        assert len(case_manifest.cases) == len(manifest.operations) * 7
        assert {(item.operation_id, item.facet) for item in case_manifest.cases} == {
            (operation.operation_id, facet)
            for operation in manifest.operations
            for facet in REQUIRED_REVIEWED_CONFORMANCE_FACETS
        }


def test_wave_d_descriptor_rejects_rebound_fixture_or_manifest_contract() -> None:
    original = WAVE_D_REVIEWED_CASE_DESCRIPTORS[0]
    for changes in (
        {"fixture_bundle_sha256": "f" * 64},
        {"family_manifest_sha256": "e" * 64},
        {"operation_specification_sha256": "d" * 64},
    ):
        values = {
            "family_id": original.family_id,
            "family_manifest_sha256": original.family_manifest_sha256,
            "operation_id": original.operation_id,
            "operation_specification_sha256": original.operation_specification_sha256,
            "native_type_id": original.native_type_id,
            "facet": original.facet,
            "fixture_contract_version": original.fixture_contract_version,
            "fixture_bundle_sha256": original.fixture_bundle_sha256,
            **changes,
        }
        with pytest.raises(ValueError, match="does not close"):
            WaveDReviewedCaseDescriptor(**values)


def test_wave_d_builder_has_no_caller_result_persistence_or_promotion_seam() -> None:
    signature = inspect.signature(build_managed_freecad_wave_d_verification)
    assert tuple(signature.parameters) == ("freecad",)
    assert signature.parameters["freecad"].kind is inspect.Parameter.KEYWORD_ONLY
    source = inspect.getsource(build_managed_freecad_wave_d_verification)
    assert "execute_case=execute" in source
    assert "observation" not in signature.parameters
    assert "results" not in signature.parameters
    assert "apply_promotion" not in source
    assert "write_receipt" not in source


def test_wave_d_import_is_freecad_and_gui_free() -> None:
    source_root = Path(__file__).parents[1] / "src"
    code = """
import sys
import vibecad.execution.freecad_reviewed_verification_wave_d
assert 'FreeCAD' not in sys.modules
assert 'FreeCADGui' not in sys.modules
print('WAVE_D_BOOTSTRAP_OK')
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
    assert "WAVE_D_BOOTSTRAP_OK" in completed.stdout


def test_wave_d_synthetic_runtime_cannot_issue_managed_evidence() -> None:
    class FakeFreeCAD:
        GuiUp = 0

        @staticmethod
        def listDocuments() -> dict[str, object]:
            return {}

    with pytest.raises(CapabilityCatalogError):
        build_managed_freecad_wave_d_verification(freecad=FakeFreeCAD())


@pytest.mark.slow
def test_real_managed_freecad_wave_d_seven_by_seven_receipt_batch() -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 for the managed Wave D gate")
    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime import status as runtime_status

    runtime_python = runtime_paths.active_runtime_python()
    if not runtime_python.is_file() or not runtime_paths.ready_sentinel().is_file():
        pytest.fail("an existing ready managed FreeCAD runtime is required")
    if not runtime_status.engine_compatible(runtime_python):
        pytest.fail("the existing managed FreeCAD runtime does not match engine pins")
    if Path(sys.executable).resolve() != runtime_python.resolve():
        pytest.fail("this slow gate must execute inside the managed FreeCAD process")

    sys.path.insert(0, str(Path(sys.prefix) / "lib"))
    import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

    assert FreeCAD.GuiUp == 0
    assert FreeCAD.listDocuments() == {}
    assert "FreeCADGui" not in sys.modules
    batch = build_managed_freecad_wave_d_verification(freecad=FreeCAD)
    assert len(batch.receipts) == 3
    assert len(batch.promotion_bindings) == 3
    assert sum(len(item.results) for item in batch.receipts) == 49
    assert not batch.grants_execution_authority
    for receipt, binding, manifest, case_manifest in zip(
        batch.receipts,
        batch.promotion_bindings,
        WAVE_D_FAMILY_MANIFESTS,
        WAVE_D_REVIEWED_CASE_MANIFESTS,
        strict=True,
    ):
        assert receipt.contract.family_manifest_sha256 == manifest.manifest_sha256
        assert receipt.case_manifest == case_manifest
        assert receipt.contract.evidence_kind is ReviewedConformanceEvidenceKind.MANAGED_FREECAD
        assert (
            receipt.contract.runtime_backend.discovery_profile
            is CapabilityExecutionProfile.HEADLESS
        )
        assert len(receipt.results) == len(manifest.operations) * 7
        assert binding.test_receipt_sha256 == receipt.test_receipt_sha256
        assert binding.test_contract_sha256 == receipt.test_contract_sha256
    assert FreeCAD.listDocuments() == {}
    assert "FreeCADGui" not in sys.modules
