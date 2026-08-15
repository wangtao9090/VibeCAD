from __future__ import annotations

import inspect

from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_legacy_reviewed_verification import (
    LEGACY_REVIEWED_CASE_MANIFESTS,
    LEGACY_REVIEWED_FAMILY_MANIFESTS,
    LEGACY_REVIEWED_OPERATION_SPEC_BY_ID,
    LEGACY_REVIEWED_OPERATION_SPECS,
)
from vibecad.execution.freecad_reviewed_verification import (
    REQUIRED_REVIEWED_CONFORMANCE_FACETS,
    ReviewedCaseManifestKind,
)

_EXPECTED_ADAPTER_IDS = {
    "freecad_parametric_groove_adapter",
    "freecad_partdesign_promotion_adapter",
    "freecad_partdesign_reference_adapter",
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
