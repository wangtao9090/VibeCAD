from __future__ import annotations

from dataclasses import replace

import pytest

from vibecad.execution.capabilities import CapabilityCatalogError, CapabilityCatalogErrorCode
from vibecad.execution.freecad_reviewed_family_capabilities import (
    MAX_REVIEWED_CAPABILITY_FAMILIES,
    build_reviewed_family_capability_specs,
)
from vibecad.intent_bridge.contracts import AdapterDescriptor
from vibecad.intent_bridge.freecad_part_core_adapter import PART_CORE_MANIFEST
from vibecad.intent_bridge.freecad_part_curve_adapter import PART_CURVE_MANIFEST
from vibecad.intent_bridge.freecad_partdesign_residual_adapter import (
    PARTDESIGN_RESIDUAL_MANIFEST,
)

_MANIFESTS = (
    PART_CORE_MANIFEST,
    PART_CURVE_MANIFEST,
    PARTDESIGN_RESIDUAL_MANIFEST,
)


def test_reviewed_family_manifests_project_without_feature_specific_code() -> None:
    forward = build_reviewed_family_capability_specs(_MANIFESTS)
    reverse = build_reviewed_family_capability_specs(tuple(reversed(_MANIFESTS)))

    assert forward == reverse
    assert len(forward) == 31
    assert len({item.operation_id for item in forward}) == 31
    assert len({item.semantic_operation for item in forward}) == 31
    assert len({item.native_type_id for item in forward}) == 31
    by_operation = {item.operation_id: item for item in forward}
    box = by_operation["freecad_part_core.box"]
    assert box.semantic_operation == "operation.part-box"
    assert box.native_type_id == "Part::Box"
    assert box.adapter_contract_sha256 == PART_CORE_MANIFEST.adapter.adapter_contract_sha256
    assert box.rule_contract_sha256 == PART_CORE_MANIFEST.rule_contract_sha256
    assert box.verification is None


def test_reviewed_family_projection_rejects_backend_and_native_adapter_drift() -> None:
    wrong_backend = replace(PART_CURVE_MANIFEST, backend_engine="OtherCAD")
    with pytest.raises(CapabilityCatalogError) as caught:
        build_reviewed_family_capability_specs((PART_CORE_MANIFEST, wrong_backend))
    assert caught.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE

    rebound_adapter = replace(
        PART_CURVE_MANIFEST,
        adapter=AdapterDescriptor(
            adapter_id="different_part_curve_adapter",
            adapter_version=PART_CURVE_MANIFEST.adapter.adapter_version,
            adapter_contract_sha256=PART_CURVE_MANIFEST.adapter.adapter_contract_sha256,
        ),
        operations=(
            replace(
                PART_CURVE_MANIFEST.operations[0],
                native_type_id=PART_CORE_MANIFEST.operations[0].native_type_id,
            ),
            *PART_CURVE_MANIFEST.operations[1:],
        ),
    )
    with pytest.raises(CapabilityCatalogError) as caught:
        build_reviewed_family_capability_specs((PART_CORE_MANIFEST, rebound_adapter))
    assert caught.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE


def test_reviewed_family_projection_has_bounded_exact_input() -> None:
    for value in (None, [], (), (object(),)):
        with pytest.raises(CapabilityCatalogError) as caught:
            build_reviewed_family_capability_specs(value)  # type: ignore[arg-type]
        assert caught.value.code is CapabilityCatalogErrorCode.INVALID_INPUT

    with pytest.raises(CapabilityCatalogError) as caught:
        build_reviewed_family_capability_specs(
            tuple(PART_CORE_MANIFEST for _ in range(MAX_REVIEWED_CAPABILITY_FAMILIES + 1))
        )
    assert caught.value.code is CapabilityCatalogErrorCode.BUDGET_EXCEEDED
