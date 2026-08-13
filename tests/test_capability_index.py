"""Focused aggregation tests for compatible capability catalog segments."""

from __future__ import annotations

import dataclasses
import hashlib

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
    FreeCadTypeRegistrySnapshot,
    build_freecad_type_catalog,
)
from vibecad.execution.operation_capabilities import (
    build_operation_capability_catalog,
)
from vibecad.execution.registry import DEFAULT_OPERATION_REGISTRY


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _backend(**changes: object) -> CapabilityBackend:
    values = {
        "backend_id": "freecad",
        "backend_version": (1, 1, 0),
        "build_fingerprint_sha256": _sha("managed-freecad-build"),
        "platform_id": "macos.x86_64",
        "discovery_profile": CapabilityExecutionProfile.HEADLESS,
    }
    values.update(changes)
    return CapabilityBackend(**values)


def _native_catalog() -> CapabilityCatalogSegment:
    snapshot = FreeCadTypeRegistrySnapshot(
        schema_version=1,
        backend_version=(1, 1, 0),
        build_fingerprint_sha256=_sha("managed-freecad-build"),
        platform_id="macos.x86_64",
        probe_profile=CapabilityExecutionProfile.HEADLESS,
        probe_modules=("PartDesign",),
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
                native_type_id="PartDesign::Pad",
                declaring_module="PartDesign",
                parent_native_type_id="App::DocumentObject",
                category=FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
            ),
        ),
    )
    return build_freecad_type_catalog(snapshot)


def test_index_aggregates_native_compiler_and_operation_surfaces() -> None:
    native = _native_catalog()
    compiler = build_current_compiler_capability_catalog(
        backend=_backend(),
        native_type_catalog=native,
    )
    operations = build_operation_capability_catalog(
        registry=DEFAULT_OPERATION_REGISTRY,
        backend=_backend(),
    )
    index = CapabilityCatalogIndex((compiler, operations, native))
    reordered = CapabilityCatalogIndex((native, compiler, operations))

    assert index.catalog_sha256 == reordered.catalog_sha256
    assert index.coverage().total == len(index.descriptors)
    assert index.coverage(kind=CapabilityKind.OPERATION).executable == 27
    assert index.coverage(kind=CapabilityKind.DOCUMENT_OBJECT).discovered == 2
    same_native = index.lookup_native("PartDesign::Pad")
    assert {item.kind for item in same_native} == {
        CapabilityKind.DOCUMENT_OBJECT,
        CapabilityKind.OPERATION,
    }


def _term(term_ref_id: str, definition: str | None = None) -> CapabilityTermRef:
    term_id = definition or term_ref_id.replace(".", "/")
    return CapabilityTermRef(
        term_ref_id=term_ref_id,
        namespace="vcad.test",
        vocabulary_version="1.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"term:{term_id}"),
    )


_MODULE = CapabilityDescriptor(
    capability_id="test.module",
    kind=CapabilityKind.MODULE,
    native_identifier="TestModule",
    declaring_module_id="test.module",
    status=CapabilitySupportStatus.REPRESENTABLE,
    risk_class=CapabilityRiskClass.READ_ONLY,
    semantic_term_ref_ids=("semantic.module",),
)


def _promoted_descriptor(status: CapabilitySupportStatus) -> CapabilityDescriptor:
    executable = status.rank >= CapabilitySupportStatus.EXECUTABLE.rank
    return CapabilityDescriptor(
        capability_id="test.object",
        kind=CapabilityKind.DOCUMENT_OBJECT,
        native_identifier="Test::Object",
        declaring_module_id="test.module",
        status=status,
        risk_class=(
            CapabilityRiskClass.UNKNOWN
            if status is CapabilitySupportStatus.DISCOVERED
            else CapabilityRiskClass.MUTATING
        ),
        semantic_term_ref_ids=(
            ("semantic.object",)
            if status is CapabilitySupportStatus.DISCOVERED
            else ("semantic.object", "semantic.refined")
        ),
        facts=(
            ()
            if status is CapabilitySupportStatus.DISCOVERED
            else (CapabilityFact(key_term_ref_id="fact.refined", value=True),)
        ),
        execution_profiles=(CapabilityExecutionProfile.HEADLESS,) if executable else (),
        lifecycle_stages=(CapabilityLifecycleStage.EXECUTE,) if executable else (),
        verification=(
            CapabilityVerificationRef(
                receipt_sha256=_sha("verified-object"),
                receipt_size_bytes=128,
                verifier_id="vcad.test.verifier",
                verifier_version="1.0",
            )
            if status is CapabilitySupportStatus.VERIFIED
            else None
        ),
    )


def _promotion_segment(
    status: CapabilitySupportStatus,
    *,
    suffix: str | None = None,
    descriptor: CapabilityDescriptor | None = None,
    terms: tuple[CapabilityTermRef, ...] | None = None,
    external_refs: tuple[ExternalCapabilityRef, ...] = (),
) -> CapabilityCatalogSegment:
    name = suffix or status.value
    return CapabilityCatalogSegment(
        schema_version=1,
        segment_id=f"test.segment.{name}",
        backend=_backend(),
        discovery_receipt_sha256=_sha(f"receipt:{name}"),
        discovery_algorithm_id="vcad.test.catalog",
        discovery_algorithm_version="1.0",
        terms=(
            terms
            if terms is not None
            else (
                _term("fact.refined"),
                _term("semantic.module"),
                _term("semantic.object"),
                _term("semantic.refined"),
            )
        ),
        descriptors=(_MODULE, descriptor or _promoted_descriptor(status)),
        external_refs=external_refs,
    )


def test_index_selects_only_monotonic_highest_promotion() -> None:
    segments = tuple(_promotion_segment(status) for status in CapabilitySupportStatus)
    index = CapabilityCatalogIndex(tuple(reversed(segments)))
    active = index.lookup("test.object")
    assert active.status is CapabilitySupportStatus.VERIFIED
    assert active.risk_class is CapabilityRiskClass.MUTATING
    assert index.coverage().verified == 1
    assert index.coverage().representable == 1  # the shared module
    assert index.coverage().execution_gap == 1
    assert index.coverage().verification_gap == 0


def test_same_rank_conflict_and_nonmonotonic_mutation_fail_closed() -> None:
    changed = dataclasses.replace(
        _promoted_descriptor(CapabilitySupportStatus.REPRESENTABLE),
        native_identifier="Test::DifferentObject",
    )
    with pytest.raises(CapabilityCatalogError) as same_rank:
        CapabilityCatalogIndex(
            (
                _promotion_segment(CapabilitySupportStatus.REPRESENTABLE),
                _promotion_segment(
                    CapabilitySupportStatus.REPRESENTABLE,
                    suffix="conflict",
                    descriptor=changed,
                ),
            )
        )
    assert same_rank.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE

    incompatible = dataclasses.replace(
        _promoted_descriptor(CapabilitySupportStatus.EXECUTABLE),
        native_identifier="Test::DifferentObject",
    )
    with pytest.raises(CapabilityCatalogError) as mutation:
        CapabilityCatalogIndex(
            (
                _promotion_segment(CapabilitySupportStatus.REPRESENTABLE),
                _promotion_segment(
                    CapabilitySupportStatus.EXECUTABLE,
                    suffix="incompatible",
                    descriptor=incompatible,
                ),
            )
        )
    assert mutation.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE


def test_index_requires_exact_backend_and_external_descriptor_closure() -> None:
    different_backend = dataclasses.replace(
        _promotion_segment(CapabilitySupportStatus.DISCOVERED),
        backend=_backend(build_fingerprint_sha256=_sha("different-build")),
    )
    with pytest.raises(CapabilityCatalogError) as backend:
        CapabilityCatalogIndex(
            (
                _promotion_segment(CapabilitySupportStatus.REPRESENTABLE),
                different_backend,
            )
        )
    assert backend.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE

    external = ExternalCapabilityRef(
        capability_id="external.object",
        descriptor_sha256=_sha("absent-descriptor"),
    )
    with pytest.raises(CapabilityCatalogError) as absent:
        CapabilityCatalogIndex(
            (
                _promotion_segment(
                    CapabilitySupportStatus.REPRESENTABLE,
                    suffix="external",
                    external_refs=(external,),
                ),
            )
        )
    assert absent.value.code is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE


def test_index_rejects_same_term_id_with_different_definition() -> None:
    first = _promotion_segment(CapabilitySupportStatus.DISCOVERED)
    changed_terms = tuple(
        _term("semantic.object", "semantic/different-object")
        if term.term_ref_id == "semantic.object"
        else term
        for term in first.terms
    )
    second = _promotion_segment(
        CapabilitySupportStatus.REPRESENTABLE,
        terms=changed_terms,
    )
    with pytest.raises(CapabilityCatalogError) as failure:
        CapabilityCatalogIndex((first, second))
    assert failure.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
