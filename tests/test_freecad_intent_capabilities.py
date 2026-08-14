from __future__ import annotations

import dataclasses

import pytest

from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
    CapabilityLifecycleStage,
    CapabilityRiskClass,
    CapabilitySupportStatus,
    CapabilityVerificationRef,
    encode_capability_catalog,
)
from vibecad.execution.freecad_intent_capabilities import (
    MAX_FREECAD_INTENT_CAPABILITY_SPECS,
    FreeCadIntentCapabilitySpec,
    build_freecad_intent_capability_catalog,
)


def _backend() -> CapabilityBackend:
    return CapabilityBackend(
        backend_id="freecad",
        backend_version=(1, 1, 0),
        build_fingerprint_sha256="a" * 64,
        platform_id="darwin-x86_64",
        discovery_profile=CapabilityExecutionProfile.HEADLESS,
    )


def _spec(
    name: str,
    native_type_id: str,
    *,
    semantic_operation: str | None = None,
    verification: CapabilityVerificationRef | None = None,
) -> FreeCadIntentCapabilitySpec:
    return FreeCadIntentCapabilitySpec(
        operation_id=name,
        semantic_operation=semantic_operation or f"operation.partdesign.{name}",
        native_type_id=native_type_id,
        adapter_id="freecad_partdesign_adapter",
        adapter_version="1.0.0",
        adapter_contract_sha256="b" * 64,
        rule_id="rule.partdesign.shared.v1",
        rule_contract_sha256="c" * 64,
        risk_class=CapabilityRiskClass.MUTATING,
        execution_profiles=(CapabilityExecutionProfile.HEADLESS,),
        lifecycle_stages=(
            CapabilityLifecycleStage.REOPEN,
            CapabilityLifecycleStage.EXECUTE,
            CapabilityLifecycleStage.SAVE,
        ),
        verification=verification,
    )


def _facts(descriptor) -> dict[str, object]:
    return {item.key_term_ref_id: item.decoded_value for item in descriptor.facts}


def test_builds_stable_executable_catalog_from_reordered_specs() -> None:
    groove = _spec("groove", "PartDesign::Groove")
    loft = _spec("additive_loft", "PartDesign::AdditiveLoft")

    forward = build_freecad_intent_capability_catalog(backend=_backend(), specs=(groove, loft))
    reverse = build_freecad_intent_capability_catalog(backend=_backend(), specs=(loft, groove))

    assert encode_capability_catalog(forward) == encode_capability_catalog(reverse)
    assert [item.capability_id for item in forward.descriptors] == [
        "vibecad.intent.operation.additive_loft",
        "vibecad.intent.operation.groove",
        "vibecad.module.intent.adapters",
    ]
    operation = next(item for item in forward.descriptors if item.capability_id.endswith(".groove"))
    assert operation.status is CapabilitySupportStatus.EXECUTABLE
    assert operation.native_identifier == "PartDesign::Groove"
    assert operation.execution_profiles == (CapabilityExecutionProfile.HEADLESS,)
    assert operation.lifecycle_stages == (
        CapabilityLifecycleStage.EXECUTE,
        CapabilityLifecycleStage.REOPEN,
        CapabilityLifecycleStage.SAVE,
    )
    assert _facts(operation) == {
        "fact.intent.adapter_binding": {
            "contract_sha256": "b" * 64,
            "id": "freecad_partdesign_adapter",
            "version": "1.0.0",
        },
        "fact.intent.native_type": "PartDesign::Groove",
        "fact.intent.rule_binding": {
            "contract_sha256": "c" * 64,
            "id": "rule.partdesign.shared.v1",
        },
        "fact.intent.semantic_operation": "operation.partdesign.groove",
    }


def test_exact_receipt_is_the_only_verified_promotion() -> None:
    receipt = CapabilityVerificationRef(
        receipt_sha256="d" * 64,
        receipt_size_bytes=2048,
        verifier_id="managed-freecad-conformance",
        verifier_version="1.0.0",
    )
    catalog = build_freecad_intent_capability_catalog(
        backend=_backend(),
        specs=(
            _spec("groove", "PartDesign::Groove", verification=receipt),
            _spec("subtractive_loft", "PartDesign::SubtractiveLoft"),
        ),
    )

    by_id = {item.capability_id: item for item in catalog.descriptors}
    assert by_id["vibecad.intent.operation.groove"].status is CapabilitySupportStatus.VERIFIED
    assert by_id["vibecad.intent.operation.groove"].verification is receipt
    assert (
        by_id["vibecad.intent.operation.subtractive_loft"].status
        is CapabilitySupportStatus.EXECUTABLE
    )
    assert by_id["vibecad.intent.operation.subtractive_loft"].verification is None


def test_same_native_type_can_expose_distinct_semantic_operations() -> None:
    catalog = build_freecad_intent_capability_catalog(
        backend=_backend(),
        specs=(
            _spec("groove_create", "PartDesign::Groove"),
            _spec(
                "groove_edit",
                "PartDesign::Groove",
                semantic_operation="operation.partdesign.groove-edit",
            ),
        ),
    )
    operations = [item for item in catalog.descriptors if item.status.rank >= 2]
    assert len(operations) == 2
    assert {item.native_identifier for item in operations} == {"PartDesign::Groove"}


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("operation_id", "../groove"),
        ("semantic_operation", "operation//groove"),
        ("native_type_id", "not-a-freecad-type"),
        ("native_type_id", "PartDesign::" + "X" * 190),
        ("adapter_contract_sha256", "A" * 64),
        ("rule_contract_sha256", "0" * 63),
    ),
)
def test_spec_rejects_unbounded_or_noncanonical_identity(field: str, value: object) -> None:
    with pytest.raises(CapabilityCatalogError) as caught:
        dataclasses.replace(_spec("groove", "PartDesign::Groove"), **{field: value})
    assert caught.value.code is CapabilityCatalogErrorCode.INVALID_INPUT
    assert len(caught.value.path.encode("utf-8")) <= 256


def test_executable_spec_requires_known_risk_and_execute_lifecycle() -> None:
    with pytest.raises(CapabilityCatalogError) as unknown_risk:
        dataclasses.replace(
            _spec("groove", "PartDesign::Groove"),
            risk_class=CapabilityRiskClass.UNKNOWN,
        )
    assert unknown_risk.value.code is CapabilityCatalogErrorCode.INVALID_INPUT

    with pytest.raises(CapabilityCatalogError) as no_execute:
        dataclasses.replace(
            _spec("groove", "PartDesign::Groove"),
            lifecycle_stages=(CapabilityLifecycleStage.INSPECT,),
        )
    assert no_execute.value.code is CapabilityCatalogErrorCode.INVALID_INPUT


def test_duplicate_operation_or_semantic_identity_is_rejected() -> None:
    first = _spec("groove", "PartDesign::Groove")
    with pytest.raises(CapabilityCatalogError) as duplicate_operation:
        build_freecad_intent_capability_catalog(backend=_backend(), specs=(first, first))
    assert duplicate_operation.value.code is CapabilityCatalogErrorCode.INVALID_INPUT

    with pytest.raises(CapabilityCatalogError) as duplicate_semantic:
        build_freecad_intent_capability_catalog(
            backend=_backend(),
            specs=(
                first,
                _spec(
                    "groove_alias",
                    "PartDesign::Groove",
                    semantic_operation=first.semantic_operation,
                ),
            ),
        )
    assert duplicate_semantic.value.code is CapabilityCatalogErrorCode.INVALID_INPUT


def test_spec_budget_n_and_n_plus_one() -> None:
    accepted = tuple(
        _spec(f"op{i:03d}", f"PartDesign::Type{i:03d}")
        for i in range(MAX_FREECAD_INTENT_CAPABILITY_SPECS)
    )
    catalog = build_freecad_intent_capability_catalog(backend=_backend(), specs=accepted)
    assert len(catalog.descriptors) == MAX_FREECAD_INTENT_CAPABILITY_SPECS + 1

    with pytest.raises(CapabilityCatalogError) as caught:
        build_freecad_intent_capability_catalog(
            backend=_backend(),
            specs=accepted + (_spec("overflow", "PartDesign::Overflow"),),
        )
    assert caught.value.code is CapabilityCatalogErrorCode.BUDGET_EXCEEDED


def test_contract_changes_rebind_catalog_receipt() -> None:
    original = _spec("groove", "PartDesign::Groove")
    changed = dataclasses.replace(original, adapter_contract_sha256="e" * 64)
    before = build_freecad_intent_capability_catalog(backend=_backend(), specs=(original,))
    after = build_freecad_intent_capability_catalog(backend=_backend(), specs=(changed,))
    assert before.discovery_receipt_sha256 != after.discovery_receipt_sha256
    assert before.catalog_sha256 != after.catalog_sha256


def test_builder_rejects_wrong_top_level_types() -> None:
    with pytest.raises(CapabilityCatalogError) as wrong_specs:
        build_freecad_intent_capability_catalog(backend=_backend(), specs=[])
    assert wrong_specs.value.code is CapabilityCatalogErrorCode.INVALID_INPUT

    with pytest.raises(CapabilityCatalogError) as wrong_backend:
        build_freecad_intent_capability_catalog(
            backend=object(), specs=(_spec("groove", "PartDesign::Groove"),)
        )
    assert wrong_backend.value.code is CapabilityCatalogErrorCode.INVALID_INPUT
