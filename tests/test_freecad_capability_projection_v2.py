"""Focused tests for the internal complete FreeCAD TypeId projection."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityCatalogSegment,
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityFact,
    CapabilityKind,
    CapabilityLifecycleStage,
    CapabilityRiskClass,
    CapabilitySupportStatus,
    CapabilityTermRef,
    CapabilityVerificationRef,
    ExternalCapabilityRef,
)
from vibecad.execution.capability_index import CapabilityCatalogIndex
from vibecad.execution.compiler_capabilities import (
    build_current_compiler_capability_catalog,
)
from vibecad.execution.freecad_capabilities import (
    FreeCadNativeTypeCategory,
    FreeCadRegisteredType,
    freecad_type_capability_id,
)
from vibecad.execution.freecad_capability_projection_v2 import (
    FREECAD_CAPABILITY_PROMOTION_PACK_SCHEMA_VERSION,
    MAX_FREECAD_CAPABILITY_PROMOTION_ENTRIES,
    FreeCadCapabilityPromotionEntry,
    FreeCadCapabilityPromotionPack,
    FreeCadCapabilitySemanticKind,
    FreeCadPromotionVerificationBinding,
    build_freecad_capability_projection_v2,
    encode_freecad_capability_projection_manifest_v2,
    encode_freecad_capability_promotion_pack,
)
from vibecad.execution.freecad_discovery_runtime_v2 import (
    FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
    collect_managed_freecad_discovery_v2,
)
from vibecad.execution.freecad_discovery_v2 import (
    FREECAD_DISCOVERY_V2_SCHEMA_VERSION,
    FreeCadDiscoverySnapshotV2,
    FreeCadPagedCapabilityCatalog,
    build_paged_freecad_type_catalog,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _registered(
    native_type_id: str,
    module: str,
    category: FreeCadNativeTypeCategory,
    parent: str | None = None,
) -> FreeCadRegisteredType:
    return FreeCadRegisteredType(
        native_type_id=native_type_id,
        declaring_module=module,
        parent_native_type_id=parent,
        category=category,
    )


def _discovery() -> FreeCadPagedCapabilityCatalog:
    snapshot = FreeCadDiscoverySnapshotV2(
        schema_version=FREECAD_DISCOVERY_V2_SCHEMA_VERSION,
        backend_version=(1, 1, 0),
        build_fingerprint_sha256=_sha("freecad-build"),
        platform_id="macos.arm64",
        probe_profile=CapabilityExecutionProfile.HEADLESS,
        probe_modules=("PartDesign", "Part"),
        registered_types=(
            _registered(
                "PartDesign::Pad",
                "PartDesign",
                FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
                "Part::Feature",
            ),
            _registered(
                "App::Extension",
                "App",
                FreeCadNativeTypeCategory.EXTENSION_TYPE,
                "Base::BaseClass",
            ),
            _registered(
                "Part::Feature",
                "Part",
                FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
                "App::DocumentObject",
            ),
            _registered(
                "App::Property",
                "App",
                FreeCadNativeTypeCategory.PROPERTY_TYPE,
                "Base::BaseClass",
            ),
            _registered(
                "Base::BaseClass",
                "Base",
                FreeCadNativeTypeCategory.NATIVE_TYPE,
            ),
            _registered(
                "App::DocumentObject",
                "App",
                FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
                "Base::BaseClass",
            ),
        ),
    )
    return build_paged_freecad_type_catalog(snapshot, max_descriptors_per_page=2)


def _term(label: str, *, changed_identity: bool = False) -> CapabilityTermRef:
    term_id = f"semantic/{label}"
    return CapabilityTermRef(
        term_ref_id=f"semantic.lane.{label}{'.changed' if changed_identity else ''}",
        namespace="vcad.test.promotion",
        vocabulary_version="1.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"{label}{'-changed' if changed_identity else ''}"),
    )


def _entry(
    *,
    native_type_id: str = "PartDesign::Pad",
    semantic_kind: FreeCadCapabilitySemanticKind = (FreeCadCapabilitySemanticKind.DOCUMENT_OBJECT),
    status: CapabilitySupportStatus,
    terms: tuple[CapabilityTermRef, ...],
    backend: CapabilityBackend,
    adapter_contract_sha256: str = _sha("adapter-contract"),
    test_receipt: str = "verified-test-receipt",
) -> FreeCadCapabilityPromotionEntry:
    executable = status.rank >= CapabilitySupportStatus.EXECUTABLE.rank
    return FreeCadCapabilityPromotionEntry(
        native_type_id=native_type_id,
        semantic_kind=semantic_kind,
        target_status=status,
        risk_class=CapabilityRiskClass.MUTATING,
        semantic_term_ref_ids=tuple(item.term_ref_id for item in terms),
        execution_profiles=(CapabilityExecutionProfile.HEADLESS,) if executable else (),
        lifecycle_stages=(CapabilityLifecycleStage.CREATE,) if executable else (),
        verification=(
            FreeCadPromotionVerificationBinding(
                runtime_build_sha256=backend.build_fingerprint_sha256,
                adapter_contract_sha256=adapter_contract_sha256,
                test_contract_sha256=_sha("test-contract"),
                test_receipt_sha256=_sha(test_receipt),
                test_receipt_size_bytes=1_024,
                verifier_id="vcad.test.verifier",
                verifier_version="1.0",
            )
            if status is CapabilitySupportStatus.VERIFIED
            else None
        ),
    )


def _pack(
    discovery: FreeCadPagedCapabilityCatalog,
    *,
    pack_id: str,
    status: CapabilitySupportStatus,
    terms: tuple[CapabilityTermRef, ...],
    native_type_id: str = "PartDesign::Pad",
    semantic_kind: FreeCadCapabilitySemanticKind = (FreeCadCapabilitySemanticKind.DOCUMENT_OBJECT),
    test_receipt: str = "verified-test-receipt",
) -> FreeCadCapabilityPromotionPack:
    adapter_contract = _sha("adapter-contract")
    return FreeCadCapabilityPromotionPack(
        schema_version=FREECAD_CAPABILITY_PROMOTION_PACK_SCHEMA_VERSION,
        pack_id=pack_id,
        lane_id="semantic.partdesign",
        adapter_id="vcad.freecad.partdesign",
        adapter_version="1.0",
        adapter_contract_sha256=adapter_contract,
        discovery_snapshot_sha256=discovery.snapshot.snapshot_sha256,
        discovery_manifest_sha256=discovery.manifest.manifest_sha256,
        backend=discovery.snapshot.backend,
        terms=terms,
        entries=(
            _entry(
                native_type_id=native_type_id,
                semantic_kind=semantic_kind,
                status=status,
                terms=terms,
                backend=discovery.snapshot.backend,
                adapter_contract_sha256=adapter_contract,
                test_receipt=test_receipt,
            ),
        ),
    )


def _formal_catalog(
    backend: CapabilityBackend,
    *,
    native_identifier: str = "PartDesign::Pad",
    status: CapabilitySupportStatus = CapabilitySupportStatus.EXECUTABLE,
    kind: CapabilityKind = CapabilityKind.OPERATION,
    capability_id: str = "vibecad.operation.test.pad",
) -> CapabilityCatalogSegment:
    module_term = _term("formal-module")
    operation_term = _term("formal-operation")
    module_id = "vibecad.module.test.compiler"
    executable = status.rank >= CapabilitySupportStatus.EXECUTABLE.rank
    receipt = (
        CapabilityVerificationRef(
            receipt_sha256=_sha("formal-verification"),
            receipt_size_bytes=42,
            verifier_id="vcad.formal.verifier",
            verifier_version="1.0",
        )
        if status is CapabilitySupportStatus.VERIFIED
        else None
    )
    return CapabilityCatalogSegment(
        schema_version=1,
        segment_id=f"formal.{_sha(capability_id)[:24]}",
        backend=backend,
        discovery_receipt_sha256=_sha(f"formal-{capability_id}-{status.value}"),
        discovery_algorithm_id="vcad.test.formal",
        discovery_algorithm_version="1.0",
        terms=(module_term, operation_term),
        descriptors=(
            CapabilityDescriptor(
                capability_id=module_id,
                kind=CapabilityKind.MODULE,
                native_identifier="vibecad.test.compiler",
                declaring_module_id=module_id,
                status=CapabilitySupportStatus.REPRESENTABLE,
                risk_class=CapabilityRiskClass.READ_ONLY,
                semantic_term_ref_ids=(module_term.term_ref_id,),
            ),
            CapabilityDescriptor(
                capability_id=capability_id,
                kind=kind,
                native_identifier=native_identifier,
                declaring_module_id=module_id,
                status=status,
                risk_class=CapabilityRiskClass.MUTATING,
                semantic_term_ref_ids=(operation_term.term_ref_id,),
                execution_profiles=(CapabilityExecutionProfile.HEADLESS,) if executable else (),
                lifecycle_stages=(CapabilityLifecycleStage.CREATE,) if executable else (),
                verification=receipt,
            ),
        ),
    )


def _error_code(call) -> CapabilityCatalogErrorCode:
    with pytest.raises(CapabilityCatalogError) as failure:
        call()
    return failure.value.code


def test_complete_classification_and_formal_binding_do_not_promote_type_ids() -> None:
    discovery = _discovery()
    formal = _formal_catalog(discovery.snapshot.backend)
    projection = build_freecad_capability_projection_v2(
        discovery=discovery,
        formal_catalogs=(formal,),
    )
    entries = {item.native_type_id: item for item in projection.manifest.entries}

    assert len(entries) == discovery.manifest.type_count == 6
    assert dict(projection.manifest.semantic_kind_index) == {
        "document_object": (
            "App::DocumentObject",
            "Part::Feature",
            "PartDesign::Pad",
        ),
        "extension_type": ("App::Extension",),
        "native_type": ("Base::BaseClass",),
        "property_type": ("App::Property",),
    }
    assert dict(projection.manifest.module_index)["App"] == (
        "App::DocumentObject",
        "App::Extension",
        "App::Property",
    )
    assert entries["PartDesign::Pad"].inheritance_family_native_type_id == "Part::Feature"
    assert all(item.status is CapabilitySupportStatus.DISCOVERED for item in entries.values())
    assert all(len(item.layers) == 1 for item in entries.values())
    assert len(projection.manifest.formal_bindings) == 1
    binding = projection.manifest.formal_bindings[0]
    assert binding.formal_capability_id == "vibecad.operation.test.pad"
    assert binding.formal_catalog_sha256 == formal.catalog_sha256
    assert binding.native_type_id == "PartDesign::Pad"
    assert (
        projection.index.lookup(freecad_type_capability_id("PartDesign::Pad")).status
        is CapabilitySupportStatus.DISCOVERED
    )
    assert (
        json.loads(encode_freecad_capability_projection_manifest_v2(projection.manifest))[
            "manifest_sha256"
        ]
        == projection.manifest.manifest_sha256
    )


def test_parallel_promotion_packs_merge_stably_and_preserve_all_four_layers() -> None:
    discovery = _discovery()
    representable_term = _term("pad-representable")
    executable_term = _term("pad-executable")
    verified_term = _term("pad-verified")
    packs = (
        _pack(
            discovery,
            pack_id="pad.representable",
            status=CapabilitySupportStatus.REPRESENTABLE,
            terms=(representable_term,),
        ),
        _pack(
            discovery,
            pack_id="pad.executable",
            status=CapabilitySupportStatus.EXECUTABLE,
            terms=(representable_term, executable_term),
        ),
        _pack(
            discovery,
            pack_id="pad.verified",
            status=CapabilitySupportStatus.VERIFIED,
            terms=(representable_term, executable_term, verified_term),
        ),
    )
    forward = build_freecad_capability_projection_v2(
        discovery=discovery,
        promotion_packs=packs,
    )
    reverse = build_freecad_capability_projection_v2(
        discovery=discovery,
        promotion_packs=tuple(reversed(packs)),
    )
    pad = next(
        item for item in forward.manifest.entries if item.native_type_id == "PartDesign::Pad"
    )

    assert forward.manifest.manifest_sha256 == reverse.manifest.manifest_sha256
    assert encode_freecad_capability_projection_manifest_v2(forward.manifest) == (
        encode_freecad_capability_projection_manifest_v2(reverse.manifest)
    )
    assert tuple(layer.status for layer in pad.layers) == tuple(CapabilitySupportStatus)
    assert pad.status is CapabilitySupportStatus.VERIFIED
    assert pad.layer(CapabilitySupportStatus.VERIFIED).promotion_pack_sha256 == packs[2].pack_sha256
    assert dict(forward.manifest.layer_status_index)["discovered"] == tuple(
        sorted(item.native_type_id for item in forward.manifest.entries)
    )
    assert dict(forward.manifest.layer_status_index)["verified"] == ("PartDesign::Pad",)
    assert dict(forward.manifest.status_index) == {
        "discovered": (
            "App::DocumentObject",
            "App::Extension",
            "App::Property",
            "Base::BaseClass",
            "Part::Feature",
        ),
        "verified": ("PartDesign::Pad",),
    }
    tampered_layer = dataclasses.replace(
        pad.layers[0],
        descriptor_sha256=_sha("tampered-layer"),
    )
    tampered_entry = dataclasses.replace(
        pad,
        layers=(tampered_layer, *pad.layers[1:]),
    )
    tampered_manifest = dataclasses.replace(
        forward.manifest,
        entries=tuple(
            tampered_entry if item.native_type_id == "PartDesign::Pad" else item
            for item in forward.manifest.entries
        ),
    )
    assert (
        _error_code(lambda: dataclasses.replace(forward, manifest=tampered_manifest))
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )


def test_verification_receipt_is_bound_to_runtime_adapter_test_pack_and_entry() -> None:
    discovery = _discovery()
    term = _term("verified-pad")
    first = _pack(
        discovery,
        pack_id="pad.verified.first",
        status=CapabilitySupportStatus.VERIFIED,
        terms=(term,),
        test_receipt="first-receipt",
    )
    second = _pack(
        discovery,
        pack_id="pad.verified.second",
        status=CapabilitySupportStatus.VERIFIED,
        terms=(term,),
        test_receipt="second-receipt",
    )
    first_projection = build_freecad_capability_projection_v2(
        discovery=discovery,
        promotion_packs=(first,),
    )
    second_projection = build_freecad_capability_projection_v2(
        discovery=discovery,
        promotion_packs=(second,),
    )
    capability_id = freecad_type_capability_id("PartDesign::Pad")
    first_descriptor = first_projection.index.lookup(capability_id)
    second_descriptor = second_projection.index.lookup(capability_id)

    assert first.pack_sha256 != second.pack_sha256
    assert first_descriptor.verification.receipt_sha256 != (
        second_descriptor.verification.receipt_sha256
    )
    assert first_descriptor.descriptor_sha256 != second_descriptor.descriptor_sha256
    assert first_projection.manifest.manifest_sha256 != (second_projection.manifest.manifest_sha256)


def test_unknown_kind_duplicate_layer_and_version_drift_fail_closed() -> None:
    discovery = _discovery()
    term = _term("representable")
    valid = _pack(
        discovery,
        pack_id="pad.representable.first",
        status=CapabilitySupportStatus.REPRESENTABLE,
        terms=(term,),
    )
    duplicate = dataclasses.replace(valid, pack_id="pad.representable.duplicate")
    unknown = _pack(
        discovery,
        pack_id="unknown.representable",
        status=CapabilitySupportStatus.REPRESENTABLE,
        terms=(term,),
        native_type_id="PartDesign::Unknown",
    )
    wrong_kind = _pack(
        discovery,
        pack_id="pad.wrong-kind",
        status=CapabilitySupportStatus.REPRESENTABLE,
        terms=(term,),
        semantic_kind=FreeCadCapabilitySemanticKind.PROPERTY_TYPE,
    )
    drifted = dataclasses.replace(
        valid,
        discovery_snapshot_sha256=_sha("different-snapshot"),
    )

    assert (
        _error_code(
            lambda: build_freecad_capability_projection_v2(
                discovery=discovery,
                promotion_packs=(valid, duplicate),
            )
        )
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )
    assert (
        _error_code(
            lambda: build_freecad_capability_projection_v2(
                discovery=discovery,
                promotion_packs=(unknown,),
            )
        )
        is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE
    )
    assert (
        _error_code(
            lambda: build_freecad_capability_projection_v2(
                discovery=discovery,
                promotion_packs=(wrong_kind,),
            )
        )
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )
    assert (
        _error_code(
            lambda: build_freecad_capability_projection_v2(
                discovery=discovery,
                promotion_packs=(drifted,),
            )
        )
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )


def test_formal_catalog_cannot_bypass_promotion_or_name_unknown_type_id() -> None:
    discovery = _discovery()
    representable = _formal_catalog(
        discovery.snapshot.backend,
        status=CapabilitySupportStatus.REPRESENTABLE,
    )
    unknown = _formal_catalog(
        discovery.snapshot.backend,
        native_identifier="PartDesign::Unknown",
    )
    native_kind = _formal_catalog(
        discovery.snapshot.backend,
        kind=CapabilityKind.DOCUMENT_OBJECT,
        capability_id=freecad_type_capability_id("PartDesign::Pad"),
    )
    api_path = _formal_catalog(
        discovery.snapshot.backend,
        native_identifier="Part.makeLoft",
    )

    assert (
        _error_code(
            lambda: build_freecad_capability_projection_v2(
                discovery=discovery,
                formal_catalogs=(representable,),
            )
        )
        is CapabilityCatalogErrorCode.INVALID_STATUS
    )
    assert (
        _error_code(
            lambda: build_freecad_capability_projection_v2(
                discovery=discovery,
                formal_catalogs=(unknown,),
            )
        )
        is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE
    )
    assert (
        _error_code(
            lambda: build_freecad_capability_projection_v2(
                discovery=discovery,
                formal_catalogs=(native_kind,),
            )
        )
        is CapabilityCatalogErrorCode.INVALID_STATUS
    )
    projection = build_freecad_capability_projection_v2(
        discovery=discovery,
        formal_catalogs=(api_path,),
    )
    assert projection.manifest.formal_bindings == ()


def test_semantic_identity_collisions_and_invalid_discovery_graphs_fail_closed() -> None:
    discovery = _discovery()
    first_term = _term("same-identity")
    conflicting_term = _term("same-identity", changed_identity=True)
    first = _pack(
        discovery,
        pack_id="pad.same-identity",
        status=CapabilitySupportStatus.REPRESENTABLE,
        terms=(first_term,),
    )
    second = _pack(
        discovery,
        pack_id="feature.same-identity",
        status=CapabilitySupportStatus.REPRESENTABLE,
        terms=(conflicting_term,),
        native_type_id="Part::Feature",
    )

    assert (
        _error_code(
            lambda: build_freecad_capability_projection_v2(
                discovery=discovery,
                promotion_packs=(first, second),
            )
        )
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )
    missing_parent = tuple(
        item
        for item in discovery.snapshot.registered_types
        if item.native_type_id != "App::DocumentObject"
    )
    assert (
        _error_code(
            lambda: dataclasses.replace(
                discovery.snapshot,
                registered_types=missing_parent,
            )
        )
        is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE
    )
    assert (
        _error_code(
            lambda: dataclasses.replace(
                discovery.snapshot,
                registered_types=(
                    *discovery.snapshot.registered_types,
                    discovery.snapshot.registered_types[0],
                ),
            )
        )
        is CapabilityCatalogErrorCode.INVALID_INPUT
    )


def test_promotion_pack_is_canonical_and_has_exact_n_plus_one_budget() -> None:
    discovery = _discovery()
    first_term = _term("first")
    second_term = _term("second")
    first_entry = _entry(
        status=CapabilitySupportStatus.REPRESENTABLE,
        terms=(first_term, second_term),
        backend=discovery.snapshot.backend,
    )
    second_entry = _entry(
        native_type_id="Part::Feature",
        status=CapabilitySupportStatus.REPRESENTABLE,
        terms=(first_term, second_term),
        backend=discovery.snapshot.backend,
    )
    first = FreeCadCapabilityPromotionPack(
        schema_version=FREECAD_CAPABILITY_PROMOTION_PACK_SCHEMA_VERSION,
        pack_id="canonical.pack",
        lane_id="semantic.part",
        adapter_id="vcad.freecad.part",
        adapter_version="1.0",
        adapter_contract_sha256=_sha("adapter-contract"),
        discovery_snapshot_sha256=discovery.snapshot.snapshot_sha256,
        discovery_manifest_sha256=discovery.manifest.manifest_sha256,
        backend=discovery.snapshot.backend,
        terms=(first_term, second_term),
        entries=(first_entry, second_entry),
    )
    reordered = dataclasses.replace(
        first,
        terms=tuple(reversed(first.terms)),
        entries=tuple(reversed(first.entries)),
    )

    assert first.pack_sha256 == reordered.pack_sha256
    assert encode_freecad_capability_promotion_pack(first) == (
        encode_freecad_capability_promotion_pack(reordered)
    )
    many_entries = tuple(
        _entry(
            native_type_id=f"Vendor::Type{index:04d}",
            status=CapabilitySupportStatus.REPRESENTABLE,
            terms=(first_term,),
            backend=discovery.snapshot.backend,
        )
        for index in range(MAX_FREECAD_CAPABILITY_PROMOTION_ENTRIES + 1)
    )
    assert (
        _error_code(lambda: dataclasses.replace(first, terms=(first_term,), entries=many_entries))
        is CapabilityCatalogErrorCode.BUDGET_EXCEEDED
    )


def test_pack_carries_bounded_facts_and_content_addressed_dependencies() -> None:
    discovery = _discovery()
    fact_term = _term("representation-family")
    dependency = CapabilityCatalogIndex(discovery.pages).lookup(
        freecad_type_capability_id("Part::Feature")
    )
    entry = dataclasses.replace(
        _entry(
            status=CapabilitySupportStatus.REPRESENTABLE,
            terms=(fact_term,),
            backend=discovery.snapshot.backend,
        ),
        facts=(
            CapabilityFact(
                key_term_ref_id=fact_term.term_ref_id,
                value={"family": "pad", "revision": 1},
            ),
        ),
        dependency_ids=(dependency.capability_id,),
    )
    pack = FreeCadCapabilityPromotionPack(
        schema_version=FREECAD_CAPABILITY_PROMOTION_PACK_SCHEMA_VERSION,
        pack_id="pad.representable.with-dependency",
        lane_id="semantic.partdesign",
        adapter_id="vcad.freecad.partdesign",
        adapter_version="1.0",
        adapter_contract_sha256=_sha("adapter-contract"),
        discovery_snapshot_sha256=discovery.snapshot.snapshot_sha256,
        discovery_manifest_sha256=discovery.manifest.manifest_sha256,
        backend=discovery.snapshot.backend,
        terms=(fact_term,),
        entries=(entry,),
        external_refs=(
            ExternalCapabilityRef(
                capability_id=dependency.capability_id,
                descriptor_sha256=dependency.descriptor_sha256,
            ),
        ),
    )
    projection = build_freecad_capability_projection_v2(
        discovery=discovery,
        promotion_packs=(pack,),
    )
    promoted = projection.index.lookup(freecad_type_capability_id("PartDesign::Pad"))

    assert promoted.dependency_ids == (dependency.capability_id,)
    assert promoted.facts[0].decoded_value == {"family": "pad", "revision": 1}
    wrong_ref = dataclasses.replace(
        pack.external_refs[0],
        descriptor_sha256=_sha("wrong-dependency"),
    )
    tampered = dataclasses.replace(pack, external_refs=(wrong_ref,))
    assert (
        _error_code(
            lambda: build_freecad_capability_projection_v2(
                discovery=discovery,
                promotion_packs=(tampered,),
            )
        )
        is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE
    )


@pytest.mark.slow
def test_real_449_type_snapshot_gate_and_formal_associations() -> None:
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
    before_gui_module = "FreeCADGui" in sys.modules
    discovery = collect_managed_freecad_discovery_v2(
        freecad=FreeCAD,
        probe_modules=FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
    )
    formal = build_current_compiler_capability_catalog(
        backend=discovery.snapshot.backend,
    )
    projection = build_freecad_capability_projection_v2(
        discovery=discovery,
        formal_catalogs=(formal,),
    )

    assert before_documents == FreeCAD.listDocuments() == {}
    assert before_gui_up == FreeCAD.GuiUp == 0
    assert before_gui_module is False
    assert "FreeCADGui" not in sys.modules
    assert discovery.manifest.type_count == len(projection.manifest.entries) == 449
    assert {key: len(values) for key, values in projection.manifest.module_index} == {
        "App": 195,
        "Attacher": 5,
        "Base": 4,
        "Data": 2,
        "Image": 1,
        "Materials": 19,
        "Part": 141,
        "PartDesign": 71,
        "Sketcher": 11,
    }
    assert {key: len(values) for key, values in projection.manifest.semantic_kind_index} == {
        "document_object": 175,
        "extension_type": 18,
        "native_type": 121,
        "property_type": 135,
    }
    assert len(projection.manifest.formal_bindings) == 11
    assert all(
        item.status is CapabilitySupportStatus.DISCOVERED
        and tuple(layer.status for layer in item.layers) == (CapabilitySupportStatus.DISCOVERED,)
        for item in projection.manifest.entries
    )
    assert len(encode_freecad_capability_projection_manifest_v2(projection.manifest)) < (
        8 * 1024 * 1024
    )
