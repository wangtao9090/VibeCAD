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
    build_promotion_verification_binding,
)
from vibecad.execution.freecad_reviewed_verification_part_b import (
    PART_B_FAMILY_MANIFESTS,
    PART_B_REVIEWED_CASE_DESCRIPTORS,
    PART_B_REVIEWED_CASE_MANIFESTS,
    PART_B_VERIFIER_CONTRACT_SHA256,
    PartBReviewedCaseDescriptor,
    build_managed_freecad_part_b_verification_receipts,
)


def test_part_b_descriptors_close_exact_16_by_7_reviewed_matrix() -> None:
    assert tuple(len(item.operations) for item in PART_B_FAMILY_MANIFESTS) == (3, 4, 3, 6)
    assert len(PART_B_REVIEWED_CASE_DESCRIPTORS) == 16 * 7
    assert len({item.case.case_sha256 for item in PART_B_REVIEWED_CASE_DESCRIPTORS}) == 112
    assert len({item.case_contract_sha256 for item in PART_B_REVIEWED_CASE_DESCRIPTORS}) == 112
    assert len(PART_B_VERIFIER_CONTRACT_SHA256) == 64
    for manifest, case_manifest in zip(
        PART_B_FAMILY_MANIFESTS,
        PART_B_REVIEWED_CASE_MANIFESTS,
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


def test_part_b_descriptor_rejects_rebound_plan_contract() -> None:
    original = PART_B_REVIEWED_CASE_DESCRIPTORS[0]
    with pytest.raises(ValueError, match="does not close"):
        PartBReviewedCaseDescriptor(
            family_id=original.family_id,
            family_manifest_sha256=original.family_manifest_sha256,
            operation_id=original.operation_id,
            operation_specification_sha256=original.operation_specification_sha256,
            native_type_id=original.native_type_id,
            facet=original.facet,
            fixture_contract_version=original.fixture_contract_version,
            fixture_plan_sha256="f" * 64,
        )


def test_part_b_builder_has_no_caller_observation_or_persistence_seam() -> None:
    signature = inspect.signature(build_managed_freecad_part_b_verification_receipts)
    assert tuple(signature.parameters) == ("freecad",)
    assert signature.parameters["freecad"].kind is inspect.Parameter.KEYWORD_ONLY
    source = inspect.getsource(build_managed_freecad_part_b_verification_receipts)
    assert "execute_case=execute" in source
    assert "apply_promotion" not in source
    assert "write_receipt" not in source


def test_part_b_import_is_freecad_and_gui_free() -> None:
    source_root = Path(__file__).parents[1] / "src"
    code = """
import sys
import vibecad.execution.freecad_reviewed_verification_part_b
assert 'FreeCAD' not in sys.modules
assert 'FreeCADGui' not in sys.modules
print('PART_B_BOOTSTRAP_OK')
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
    assert "PART_B_BOOTSTRAP_OK" in completed.stdout


def test_part_b_synthetic_runtime_cannot_issue_managed_receipts() -> None:
    class FakeFreeCAD:
        GuiUp = 0

        @staticmethod
        def listDocuments() -> dict[str, object]:
            return {}

    with pytest.raises(CapabilityCatalogError):
        build_managed_freecad_part_b_verification_receipts(freecad=FakeFreeCAD())


@pytest.mark.slow
def test_real_managed_freecad_part_b_16_by_7_receipt_batch() -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 for the managed Part B gate")
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
    receipts = build_managed_freecad_part_b_verification_receipts(freecad=FreeCAD)
    assert len(receipts) == 4
    assert sum(len(item.results) for item in receipts) == 112
    for receipt, manifest, case_manifest in zip(
        receipts,
        PART_B_FAMILY_MANIFESTS,
        PART_B_REVIEWED_CASE_MANIFESTS,
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
        binding = build_promotion_verification_binding(receipt)
        assert binding.test_receipt_sha256 == receipt.test_receipt_sha256
        assert binding.test_contract_sha256 == receipt.test_contract_sha256
    assert FreeCAD.listDocuments() == {}
    assert "FreeCADGui" not in sys.modules
