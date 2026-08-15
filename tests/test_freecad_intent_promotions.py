from __future__ import annotations

import dataclasses
import hashlib

import pytest

from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
    CapabilitySupportStatus,
    CapabilityVerificationRef,
)
from vibecad.execution.freecad_builtin_intent_capabilities import (
    build_current_freecad_intent_capability_catalog,
    current_freecad_intent_capability_specs,
    current_freecad_intent_promotion_specs,
)
from vibecad.execution.freecad_capabilities import (
    FreeCadNativeTypeCategory,
    FreeCadRegisteredType,
)
from vibecad.execution.freecad_capability_projection_v2 import (
    FreeCadPromotionVerificationBinding,
    build_freecad_capability_projection_v2,
)
from vibecad.execution.freecad_discovery_v2 import (
    FREECAD_DISCOVERY_V2_SCHEMA_VERSION,
    FreeCadDiscoverySnapshotV2,
    build_paged_freecad_type_catalog,
)
from vibecad.execution.freecad_intent_promotions import (
    build_freecad_intent_capability_promotion_packs,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _discovery(extra: tuple[FreeCadRegisteredType, ...] = ()):
    native_types = sorted(
        {item.native_type_id for item in current_freecad_intent_capability_specs()}
    )
    snapshot = FreeCadDiscoverySnapshotV2(
        schema_version=FREECAD_DISCOVERY_V2_SCHEMA_VERSION,
        backend_version=(1, 1, 0),
        build_fingerprint_sha256=_sha("freecad-build"),
        platform_id="macos.arm64",
        probe_profile=CapabilityExecutionProfile.HEADLESS,
        probe_modules=("Part", "PartDesign", "Sketcher"),
        registered_types=(
            FreeCadRegisteredType(
                native_type_id="Base::BaseClass",
                declaring_module="Base",
                parent_native_type_id=None,
                category=FreeCadNativeTypeCategory.NATIVE_TYPE,
            ),
            FreeCadRegisteredType(
                native_type_id="App::DocumentObject",
                declaring_module="App",
                parent_native_type_id="Base::BaseClass",
                category=FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
            ),
            FreeCadRegisteredType(
                native_type_id="Part::Feature",
                declaring_module="Part",
                parent_native_type_id="App::DocumentObject",
                category=FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
            ),
            *(
                FreeCadRegisteredType(
                    native_type_id=native_type_id,
                    declaring_module=native_type_id.split("::", 1)[0],
                    parent_native_type_id="Part::Feature",
                    category=FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
                )
                for native_type_id in native_types
            ),
            *extra,
        ),
    )
    return build_paged_freecad_type_catalog(snapshot, max_descriptors_per_page=5)


def _fact(entry, key: str):
    return next(item.decoded_value for item in entry.facts if item.key_term_ref_id == key)


def test_current_specs_build_eighteen_packs_and_promote_all_one_hundred_two_type_ids() -> None:
    discovery = _discovery()
    specs = current_freecad_intent_promotion_specs()
    packs = build_freecad_intent_capability_promotion_packs(discovery=discovery, specs=specs)
    assert len(packs) == 18
    assert sum(len(item.entries) for item in packs) == 102
    assert {item.adapter_id for item in packs} == {item.adapter_id for item in specs}
    assert all(
        entry.target_status is CapabilitySupportStatus.EXECUTABLE
        for pack in packs
        for entry in pack.entries
    )

    projection = build_freecad_capability_projection_v2(
        discovery=discovery,
        promotion_packs=packs,
        formal_catalogs=(
            build_current_freecad_intent_capability_catalog(backend=discovery.snapshot.backend),
        ),
    )
    promoted = {
        item.native_type_id: item.status
        for item in projection.manifest.entries
        if item.native_type_id in {spec.native_type_id for spec in specs}
    }
    assert promoted == {item.native_type_id: CapabilitySupportStatus.EXECUTABLE for item in specs}
    assert len(projection.manifest.formal_bindings) == 124


def test_multiple_semantics_for_one_type_are_consolidated() -> None:
    discovery = _discovery()
    source = next(
        item
        for item in current_freecad_intent_capability_specs()
        if item.native_type_id == "PartDesign::Groove"
    )
    second = dataclasses.replace(
        source,
        operation_id="partdesign.groove.edit",
        semantic_operation="operation.partdesign-groove-edit",
    )
    packs = build_freecad_intent_capability_promotion_packs(
        discovery=discovery, specs=(source, second)
    )
    assert len(packs) == 1
    assert len(packs[0].entries) == 1
    operations = _fact(packs[0].entries[0], "fact.intent.promotion.operations")
    assert [item["operation_id"] for item in operations] == [
        "partdesign.groove.angle",
        "partdesign.groove.edit",
    ]


def test_verified_requires_strong_native_verification_binding() -> None:
    discovery = _discovery()
    source = next(
        item
        for item in current_freecad_intent_capability_specs()
        if item.native_type_id == "PartDesign::Groove"
    )
    binding = FreeCadPromotionVerificationBinding(
        runtime_build_sha256=discovery.snapshot.backend.build_fingerprint_sha256,
        adapter_contract_sha256=source.adapter_contract_sha256,
        test_contract_sha256=_sha("test-contract"),
        test_receipt_sha256=_sha("test-receipt"),
        test_receipt_size_bytes=1024,
        verifier_id="managed-freecad-conformance",
        verifier_version="1.0.0",
    )
    packs = build_freecad_intent_capability_promotion_packs(
        discovery=discovery,
        specs=(source,),
        verification_by_native_type={source.native_type_id: binding},
    )
    assert packs[0].entries[0].target_status is CapabilitySupportStatus.VERIFIED
    assert packs[0].entries[0].verification is binding


def test_unknown_type_wrong_category_and_cross_adapter_collision_fail_closed() -> None:
    discovery = _discovery()
    source = current_freecad_intent_capability_specs()[0]
    unknown = dataclasses.replace(source, native_type_id="PartDesign::Missing")
    with pytest.raises(CapabilityCatalogError) as missing:
        build_freecad_intent_capability_promotion_packs(discovery=discovery, specs=(unknown,))
    assert missing.value.code is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE

    property_type = FreeCadRegisteredType(
        native_type_id="App::PropertyExample",
        declaring_module="App",
        parent_native_type_id="Base::BaseClass",
        category=FreeCadNativeTypeCategory.PROPERTY_TYPE,
    )
    wrong_discovery = _discovery((property_type,))
    wrong_category = dataclasses.replace(source, native_type_id=property_type.native_type_id)
    with pytest.raises(CapabilityCatalogError) as category:
        build_freecad_intent_capability_promotion_packs(
            discovery=wrong_discovery, specs=(wrong_category,)
        )
    assert category.value.code is CapabilityCatalogErrorCode.INVALID_INPUT

    rebound = dataclasses.replace(
        source,
        operation_id="different.adapter",
        semantic_operation="operation.different-adapter",
        adapter_id="different_adapter",
        adapter_contract_sha256="f" * 64,
    )
    with pytest.raises(CapabilityCatalogError) as collision:
        build_freecad_intent_capability_promotion_packs(
            discovery=discovery, specs=(source, rebound)
        )
    assert collision.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE


def test_generic_formal_verification_cannot_impersonate_native_verification() -> None:
    discovery = _discovery()
    source = current_freecad_intent_capability_specs()[0]
    verified_formal = dataclasses.replace(
        source,
        verification=CapabilityVerificationRef(
            receipt_sha256=_sha("generic"),
            receipt_size_bytes=12,
            verifier_id="generic",
            verifier_version="1.0",
        ),
    )
    with pytest.raises(CapabilityCatalogError) as caught:
        build_freecad_intent_capability_promotion_packs(
            discovery=discovery, specs=(verified_formal,)
        )
    assert caught.value.code is CapabilityCatalogErrorCode.INVALID_INPUT


def test_duplicate_formal_identity_is_rejected_before_pack_construction() -> None:
    discovery = _discovery()
    source = current_freecad_intent_capability_specs()[0]
    with pytest.raises(CapabilityCatalogError) as duplicate:
        build_freecad_intent_capability_promotion_packs(
            discovery=discovery,
            specs=(source, source),
        )
    assert duplicate.value.code is CapabilityCatalogErrorCode.INVALID_INPUT

    semantic_alias = dataclasses.replace(source, operation_id="semantic.alias")
    with pytest.raises(CapabilityCatalogError) as semantic:
        build_freecad_intent_capability_promotion_packs(
            discovery=discovery,
            specs=(source, semantic_alias),
        )
    assert semantic.value.code is CapabilityCatalogErrorCode.INVALID_INPUT


def test_discovery_binding_changes_pack_identity() -> None:
    source = current_freecad_intent_capability_specs()[0]
    before = build_freecad_intent_capability_promotion_packs(
        discovery=_discovery(), specs=(source,)
    )[0]
    extra = FreeCadRegisteredType(
        native_type_id="PartDesign::Extra",
        declaring_module="PartDesign",
        parent_native_type_id="Part::Feature",
        category=FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
    )
    after = build_freecad_intent_capability_promotion_packs(
        discovery=_discovery((extra,)), specs=(source,)
    )[0]
    assert before.pack_id != after.pack_id
    assert before.pack_sha256 != after.pack_sha256
