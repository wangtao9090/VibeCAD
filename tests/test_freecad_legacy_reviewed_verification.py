from __future__ import annotations

import inspect
import os
import subprocess
from pathlib import Path

import pytest

from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_legacy_reviewed_verification import (
    LEGACY_REVIEWED_CASE_MANIFESTS,
    LEGACY_REVIEWED_FAMILY_MANIFESTS,
    LEGACY_REVIEWED_OPERATION_SPEC_BY_ID,
    LEGACY_REVIEWED_OPERATION_SPECS,
    PARTDESIGN_REFERENCE_V2_REVIEWED_HOST_CASE_MANIFEST,
    PARTDESIGN_REFERENCE_V2_VERIFICATION_FAMILY_MANIFEST,
    build_managed_freecad_legacy_reviewed_verification_receipts,
)
from vibecad.execution.freecad_partdesign_reference_reviewed_execution import (
    FREECAD_REFERENCE_REVIEWED_ADAPTER_DESCRIPTOR,
    PARTDESIGN_REFERENCE_REVIEWED_PRODUCT_IDENTITIES,
)
from vibecad.execution.freecad_reviewed_verification import (
    REQUIRED_REVIEWED_CONFORMANCE_FACETS,
    ReviewedCaseManifestKind,
)

_EXPECTED_ADAPTER_IDS = {
    "freecad_parametric_groove_adapter",
    "freecad_partdesign_promotion_adapter",
    "freecad_partdesign_reference_reviewed_adapter",
    "freecad_partdesign_primitive_adapter",
    "freecad_planar_mechanical_v1_adapter",
    "freecad_partdesign_dressup_transform_adapter",
    "freecad_partdesign_pattern_adapter",
    "freecad_partdesign_boolean_adapter",
}


def test_legacy_manifests_close_exact_formal_43_by_7_inventory() -> None:
    assert tuple(len(item.operations) for item in LEGACY_REVIEWED_FAMILY_MANIFESTS) == (
        1,
        6,
        5,
        16,
        3,
        6,
        3,
        3,
    )
    assert len(LEGACY_REVIEWED_OPERATION_SPECS) == 43
    assert len({item.native_type_id for item in LEGACY_REVIEWED_OPERATION_SPECS}) == 41
    assert len(LEGACY_REVIEWED_OPERATION_SPEC_BY_ID) == 43
    assert sum(len(item.cases) for item in LEGACY_REVIEWED_CASE_MANIFESTS) == 43 * 7
    assert {item.adapter.adapter_id for item in LEGACY_REVIEWED_FAMILY_MANIFESTS} == (
        _EXPECTED_ADAPTER_IDS
    )

    formal = {
        item.operation_id: item
        for item in current_freecad_intent_capability_specs()
        if item.adapter_id in _EXPECTED_ADAPTER_IDS
    }
    assert set(formal) == set(LEGACY_REVIEWED_OPERATION_SPEC_BY_ID)
    for manifest, case_manifest in zip(
        LEGACY_REVIEWED_FAMILY_MANIFESTS,
        LEGACY_REVIEWED_CASE_MANIFESTS,
        strict=True,
    ):
        assert case_manifest.manifest_kind is ReviewedCaseManifestKind.REVIEWED_HOST
        assert case_manifest.family_manifest_sha256 == manifest.manifest_sha256
        assert len(case_manifest.cases) == len(manifest.operations) * 7
        assert {(case.operation_id, case.facet) for case in case_manifest.cases} == {
            (operation.operation_id, facet)
            for operation in manifest.operations
            for facet in REQUIRED_REVIEWED_CONFORMANCE_FACETS
        }
        for operation in manifest.operations:
            capability = formal[operation.operation_id]
            assert operation.semantic_term.term_id == capability.semantic_operation
            assert operation.native_type_id == capability.native_type_id
            assert manifest.adapter.adapter_id == capability.adapter_id
            assert manifest.adapter.adapter_version == capability.adapter_version
            assert manifest.adapter.adapter_contract_sha256 == capability.adapter_contract_sha256
            assert manifest.rule_id == capability.rule_id
            assert manifest.rule_contract_sha256 == capability.rule_contract_sha256


def test_legacy_verification_inventory_is_import_only_and_has_no_promotion_side_effect() -> None:
    assert all(item.verification is None for item in current_freecad_intent_capability_specs())
    module = inspect.getmodule(LEGACY_REVIEWED_FAMILY_MANIFESTS[0])
    assert module is not None
    assert "apply_promotion" not in inspect.getsource(module)
    assert "write_receipt" not in inspect.getsource(module)
    assert tuple(
        inspect.signature(build_managed_freecad_legacy_reviewed_verification_receipts).parameters
    ) == ("freecad",)


def test_reference5_v2_verification_manifest_and_all_facets_match_current_formal() -> None:
    manifest = PARTDESIGN_REFERENCE_V2_VERIFICATION_FAMILY_MANIFEST
    case_manifest = PARTDESIGN_REFERENCE_V2_REVIEWED_HOST_CASE_MANIFEST
    formal = {item.operation_id: item for item in current_freecad_intent_capability_specs()}
    operation_ids = tuple(item[0] for item in PARTDESIGN_REFERENCE_REVIEWED_PRODUCT_IDENTITIES)

    assert manifest.adapter is FREECAD_REFERENCE_REVIEWED_ADAPTER_DESCRIPTOR
    assert {item.operation_id for item in manifest.operations} == set(operation_ids)
    assert case_manifest.family_manifest_sha256 == manifest.manifest_sha256
    assert len(case_manifest.cases) == 5 * 7
    assert {(item.operation_id, item.facet) for item in case_manifest.cases} == {
        (operation_id, facet)
        for operation_id in operation_ids
        for facet in REQUIRED_REVIEWED_CONFORMANCE_FACETS
    }
    for operation in manifest.operations:
        spec = formal[operation.operation_id]
        assert spec.adapter_id == manifest.adapter.adapter_id
        assert spec.adapter_version == manifest.adapter.adapter_version == "2.0.0"
        assert spec.adapter_contract_sha256 == manifest.adapter.adapter_contract_sha256
        assert spec.rule_id == manifest.rule_id
        assert spec.rule_contract_sha256 == manifest.rule_contract_sha256
        assert operation.specification_sha256 in {
            item.operation_specification_sha256
            for item in case_manifest.cases
            if item.operation_id == operation.operation_id
        }


@pytest.mark.slow
def test_real_managed_freecad_builds_exact_legacy_43_by_7_receipts() -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the real FreeCAD batch gate")
    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime import status as runtime_status

    runtime_python = runtime_paths.active_runtime_python()
    if not runtime_python.is_file() or not runtime_paths.ready_sentinel().is_file():
        pytest.fail("an existing ready managed FreeCAD runtime is required")
    if not runtime_status.engine_compatible(runtime_python):
        pytest.fail("the existing managed FreeCAD runtime does not match current engine pins")

    source_root = Path(__file__).parents[1] / "src"
    code = f"""
import sys
sys.path.insert(0, {str(source_root)!r})
from vibecad.freecad_env import prepare_freecad_import
prepare_freecad_import()
import FreeCAD
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_legacy_reviewed_verification import (
    LEGACY_REVIEWED_FAMILY_MANIFESTS,
    LEGACY_REVIEWED_OPERATION_SPEC_BY_ID,
    build_managed_freecad_legacy_reviewed_verification_receipts,
)
from vibecad.execution.freecad_reviewed_verification import (
    ReviewedConformanceEvidenceKind,
)

assert FreeCAD.GuiUp == 0 and FreeCAD.listDocuments() == {{}}
pairs = build_managed_freecad_legacy_reviewed_verification_receipts(freecad=FreeCAD)
assert len(pairs) == len(LEGACY_REVIEWED_FAMILY_MANIFESTS) == 8
assert sum(len(receipt.results) for receipt, _binding in pairs) == 43 * 7
assert FreeCAD.listDocuments() == {{}} and FreeCAD.GuiUp == 0
formal = {{item.operation_id: item for item in current_freecad_intent_capability_specs()}}
for manifest, (receipt, binding) in zip(
    LEGACY_REVIEWED_FAMILY_MANIFESTS, pairs, strict=True
):
    contract = receipt.contract
    assert contract.family_manifest_sha256 == manifest.manifest_sha256
    assert contract.adapter_id == manifest.adapter.adapter_id
    assert contract.adapter_version == manifest.adapter.adapter_version
    assert contract.adapter_contract_sha256 == manifest.adapter.adapter_contract_sha256
    assert contract.rule_id == manifest.rule_id
    assert contract.rule_contract_sha256 == manifest.rule_contract_sha256
    assert contract.evidence_kind is ReviewedConformanceEvidenceKind.MANAGED_FREECAD
    assert {{item.operation_id for item in contract.operations}} == {{
        item.operation_id for item in manifest.operations
    }}
    for item in contract.operations:
        reviewed = LEGACY_REVIEWED_OPERATION_SPEC_BY_ID[item.operation_id]
        capability = formal[item.operation_id]
        assert item.operation_specification_sha256 == reviewed.specification_sha256
        assert item.native_type_id == reviewed.native_type_id == capability.native_type_id
        assert capability.verification is None
    assert binding.runtime_build_sha256 == contract.runtime_backend.build_fingerprint_sha256
    assert binding.adapter_contract_sha256 == contract.adapter_contract_sha256
    assert binding.test_contract_sha256 == receipt.test_contract_sha256
    assert binding.test_receipt_sha256 == receipt.test_receipt_sha256
    assert binding.test_receipt_size_bytes == receipt.test_receipt_size_bytes
    assert binding.verifier_id == contract.verifier_id
    assert binding.verifier_version == contract.verifier_version
    assert not receipt.executable and not receipt.grants_execution_authority
print("REAL_FREECAD_LEGACY_REVIEWED_43_BY_7_OK")
"""
    completed = subprocess.run(
        [str(runtime_python), "-c", code],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert completed.returncode == 0, completed.stderr
    assert "REAL_FREECAD_LEGACY_REVIEWED_43_BY_7_OK" in completed.stdout
