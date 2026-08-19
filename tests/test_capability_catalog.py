"""Focused tests for the backend-neutral capability catalog contract."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from vibecad.execution.capabilities import (
    MAX_CAPABILITY_DESCRIPTORS,
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityCatalogSegment,
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityFact,
    CapabilityKind,
    CapabilityLifecycleStage,
    CapabilityRelation,
    CapabilityRiskClass,
    CapabilitySupportStatus,
    CapabilityTermRef,
    CapabilityVerificationRef,
    ExternalCapabilityRef,
    decode_capability_catalog,
    encode_capability_catalog,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _term(term_ref_id: str, term_id: str) -> CapabilityTermRef:
    return CapabilityTermRef(
        term_ref_id=term_ref_id,
        namespace="vcad.capability",
        vocabulary_version="1.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"term:{term_id}"),
    )


def _terms() -> tuple[CapabilityTermRef, ...]:
    return (
        _term("fact.native.type", "fact/native-type"),
        _term("relation.inherits", "relation/inherits"),
        _term("semantic.module", "semantic/module"),
        _term("semantic.object.box", "semantic/object/box"),
    )


def _module() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id="freecad.module.part",
        kind=CapabilityKind.MODULE,
        native_identifier="Part",
        declaring_module_id="freecad.module.part",
        status=CapabilitySupportStatus.DISCOVERED,
        risk_class=CapabilityRiskClass.READ_ONLY,
        semantic_term_ref_ids=("semantic.module",),
    )


def _box(*, status: CapabilitySupportStatus = CapabilitySupportStatus.REPRESENTABLE):
    executable = status.rank >= CapabilitySupportStatus.EXECUTABLE.rank
    verified = status is CapabilitySupportStatus.VERIFIED
    return CapabilityDescriptor(
        capability_id="freecad.object.part.box",
        kind=CapabilityKind.DOCUMENT_OBJECT,
        native_identifier="Part::Box",
        declaring_module_id="freecad.module.part",
        status=status,
        risk_class=CapabilityRiskClass.MUTATING,
        semantic_term_ref_ids=("semantic.object.box",),
        facts=(
            CapabilityFact(
                key_term_ref_id="fact.native.type",
                value={"property_count": 18, "type_id": "Part::Box"},
            ),
        ),
        execution_profiles=(CapabilityExecutionProfile.HEADLESS,) if executable else (),
        lifecycle_stages=(
            (
                CapabilityLifecycleStage.CREATE,
                CapabilityLifecycleStage.EDIT,
                CapabilityLifecycleStage.RECOMPUTE,
                CapabilityLifecycleStage.SAVE,
                CapabilityLifecycleStage.REOPEN,
            )
            if executable
            else ()
        ),
        dependency_ids=("freecad.module.part",),
        verification=(
            CapabilityVerificationRef(
                receipt_sha256=_sha("box-verification"),
                receipt_size_bytes=512,
                verifier_id="vcad.freecad.conformance",
                verifier_version="1.0",
            )
            if verified
            else None
        ),
    )


def _catalog(
    *,
    descriptors: tuple[CapabilityDescriptor, ...] | None = None,
    external_refs: tuple[ExternalCapabilityRef, ...] = (),
    relations: tuple[CapabilityRelation, ...] | None = None,
) -> CapabilityCatalogSegment:
    values = (_module(), _box()) if descriptors is None else descriptors
    edges = (
        (
            CapabilityRelation(
                relation_id="relation.box.inherits",
                relation_term_ref_id="relation.inherits",
                source_capability_id="freecad.object.part.box",
                target_capability_ids=("freecad.module.part",),
            ),
        )
        if relations is None
        else relations
    )
    return CapabilityCatalogSegment(
        schema_version=1,
        segment_id="freecad.part.headless",
        backend=CapabilityBackend(
            backend_id="freecad",
            backend_version=(1, 1, 3),
            build_fingerprint_sha256=_sha("freecad-build"),
            platform_id="macos.x86_64",
            discovery_profile=CapabilityExecutionProfile.HEADLESS,
        ),
        discovery_receipt_sha256=_sha("discovery-receipt"),
        discovery_algorithm_id="vcad.freecad.typeid.probe",
        discovery_algorithm_version="1.0",
        terms=tuple(reversed(_terms())),
        descriptors=tuple(reversed(values)),
        external_refs=external_refs,
        relations=tuple(reversed(edges)),
    )


def test_catalog_round_trip_is_canonical_and_order_independent() -> None:
    catalog = _catalog()
    reordered = _catalog(
        descriptors=tuple(reversed(catalog.descriptors)),
        relations=tuple(reversed(catalog.relations)),
    )

    raw = encode_capability_catalog(catalog)
    decoded = decode_capability_catalog(raw)

    assert decoded == catalog
    assert decoded.catalog_id.startswith("capability_catalog_")
    assert encode_capability_catalog(reordered) == raw
    assert decoded.lookup("freecad.object.part.box").native_identifier == "Part::Box"
    assert dict(decoded.support_counts()) == {
        CapabilitySupportStatus.DISCOVERED: 1,
        CapabilitySupportStatus.REPRESENTABLE: 1,
        CapabilitySupportStatus.EXECUTABLE: 0,
        CapabilitySupportStatus.VERIFIED: 0,
    }


def test_fact_values_are_snapshot_immutable() -> None:
    source = {"nested": [1, 2], "type_id": "Part::Box"}
    fact = CapabilityFact(key_term_ref_id="fact.native.type", value=source)
    source["nested"].append(3)
    decoded = fact.decoded_value
    assert decoded == {"nested": [1, 2], "type_id": "Part::Box"}
    decoded["nested"].append(4)
    assert fact.decoded_value == {"nested": [1, 2], "type_id": "Part::Box"}


def test_support_status_cannot_overstate_execution_or_verification() -> None:
    with pytest.raises(CapabilityCatalogError) as absent_profile:
        dataclasses.replace(_box(), status=CapabilitySupportStatus.EXECUTABLE)
    assert absent_profile.value.code is CapabilityCatalogErrorCode.INVALID_STATUS

    with pytest.raises(CapabilityCatalogError) as absent_receipt:
        dataclasses.replace(
            _box(status=CapabilitySupportStatus.EXECUTABLE),
            status=CapabilitySupportStatus.VERIFIED,
        )
    assert absent_receipt.value.code is CapabilityCatalogErrorCode.INVALID_STATUS

    verified = _box(status=CapabilitySupportStatus.VERIFIED)
    assert verified.verification is not None
    assert verified.descriptor_sha256 != _box().descriptor_sha256


def test_discovered_capability_cannot_smuggle_executable_metadata() -> None:
    with pytest.raises(CapabilityCatalogError) as failure:
        dataclasses.replace(
            _module(),
            execution_profiles=(CapabilityExecutionProfile.HEADLESS,),
            lifecycle_stages=(CapabilityLifecycleStage.CREATE,),
        )
    assert failure.value.code is CapabilityCatalogErrorCode.INVALID_STATUS


def test_external_content_addressed_refs_close_cross_segment_relations() -> None:
    module = _module()
    external = ExternalCapabilityRef(
        capability_id=module.capability_id,
        descriptor_sha256=module.descriptor_sha256,
    )
    object_descriptor = _box()
    relation = CapabilityRelation(
        relation_id="relation.box.inherits",
        relation_term_ref_id="relation.inherits",
        source_capability_id=object_descriptor.capability_id,
        target_capability_ids=(module.capability_id,),
    )
    catalog = _catalog(
        descriptors=(object_descriptor,),
        external_refs=(external,),
        relations=(relation,),
    )
    assert decode_capability_catalog(encode_capability_catalog(catalog)) == catalog


@pytest.mark.parametrize(
    "change,path",
    (
        ({"semantic_term_ref_ids": ("term.unknown",)}, "freecad.object.part.box"),
        ({"dependency_ids": ("freecad.module.missing",)}, "freecad.object.part.box"),
    ),
)
def test_unknown_semantic_and_capability_refs_fail_closed(change: dict, path: str) -> None:
    descriptor = dataclasses.replace(_box(), **change)
    with pytest.raises(CapabilityCatalogError) as failure:
        _catalog(descriptors=(_module(), descriptor))
    assert failure.value.code is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE
    assert failure.value.path == path


def test_duplicate_local_and_external_capability_is_rejected() -> None:
    module = _module()
    with pytest.raises(CapabilityCatalogError) as failure:
        _catalog(
            external_refs=(
                ExternalCapabilityRef(
                    capability_id=module.capability_id,
                    descriptor_sha256=module.descriptor_sha256,
                ),
            )
        )
    assert failure.value.code is CapabilityCatalogErrorCode.INVALID_INPUT


def test_descriptor_budget_fails_before_record_encoding() -> None:
    descriptors = tuple(
        dataclasses.replace(
            _module(),
            capability_id=f"freecad.module.part.{index}",
            declaring_module_id=f"freecad.module.part.{index}",
            native_identifier=f"Part{index}",
        )
        for index in range(MAX_CAPABILITY_DESCRIPTORS + 1)
    )
    with pytest.raises(CapabilityCatalogError) as failure:
        _catalog(descriptors=descriptors, relations=())
    assert failure.value.code is CapabilityCatalogErrorCode.BUDGET_EXCEEDED
    assert failure.value.path == "descriptors"


def test_decoder_rejects_noncanonical_duplicate_and_nonfinite_json() -> None:
    raw = encode_capability_catalog(_catalog())
    value = json.loads(raw)
    noncanonical = json.dumps(value, indent=2, sort_keys=True).encode("ascii")
    duplicate = raw[:-1] + b',"catalog_sha256":"' + b"0" * 64 + b'"}'
    nonfinite = raw.replace(b'"property_count":18', b'"property_count":NaN')

    for rejected in (noncanonical, duplicate, nonfinite):
        with pytest.raises(CapabilityCatalogError) as failure:
            decode_capability_catalog(rejected)
        assert failure.value.code is CapabilityCatalogErrorCode.INVALID_INPUT


def test_digest_tampering_is_distinguished_from_shape_failure() -> None:
    raw = encode_capability_catalog(_catalog())
    value = json.loads(raw)
    value["catalog_sha256"] = "0" * 64
    tampered = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("ascii")
    with pytest.raises(CapabilityCatalogError) as failure:
        decode_capability_catalog(tampered)
    assert failure.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE


def test_unknown_native_identifiers_remain_inert_metadata() -> None:
    descriptor = dataclasses.replace(
        _box(),
        native_identifier="VendorAddon::FutureObject",
        capability_id="vendor.addon.future.object",
    )
    catalog = _catalog(descriptors=(_module(), descriptor), relations=())
    assert (
        catalog.lookup("vendor.addon.future.object").status is CapabilitySupportStatus.REPRESENTABLE
    )
    assert not hasattr(catalog.lookup("vendor.addon.future.object"), "handler")


def test_contract_has_no_workflow_backend_or_dynamic_execution_dependency() -> None:
    path = Path(__file__).parents[1] / "src/vibecad/execution/capabilities.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not {
        name
        for name in imports
        if name.startswith(("FreeCAD", "Part", "vibecad.workflow", "vibecad.parametric"))
    }
    forbidden = {"eval", "exec", "compile", "__import__", "getattr"}
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert forbidden.isdisjoint(called)
