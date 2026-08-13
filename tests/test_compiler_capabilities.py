"""Focused tests for current compiler-family capability projection."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
    CapabilityKind,
    CapabilitySupportStatus,
    CapabilityVerificationRef,
    decode_capability_catalog,
    encode_capability_catalog,
)
from vibecad.execution.compiler_capabilities import (
    build_current_compiler_capability_catalog,
    edge_treatment_capability_id,
    freeform_feature_capability_id,
    parametric_feature_capability_id,
)
from vibecad.execution.freecad_capabilities import (
    FreeCadNativeTypeCategory,
    FreeCadRegisteredType,
    FreeCadTypeRegistrySnapshot,
    build_freecad_type_catalog,
)
from vibecad.freeform.contracts import FreeformFeatureKind
from vibecad.parametric.compiler import (
    _EDGE_TREATMENT_TYPE_IDS,
    _FEATURE_TYPE_IDS,
)
from vibecad.parametric.contracts import EdgeTreatmentKind, FeatureKind


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


def _native_catalog():
    children = tuple(
        FreeCadRegisteredType(
            native_type_id=native_type_id,
            declaring_module=native_type_id.split("::", 1)[0],
            parent_native_type_id="App::DocumentObject",
            category=FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
        )
        for native_type_id in sorted(
            set(_FEATURE_TYPE_IDS.values()) | set(_EDGE_TREATMENT_TYPE_IDS.values())
        )
    )
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
            *children,
        ),
    )
    return build_freecad_type_catalog(snapshot)


def _verification(label: str) -> CapabilityVerificationRef:
    return CapabilityVerificationRef(
        receipt_sha256=_sha(label),
        receipt_size_bytes=2048,
        verifier_id="vcad.compiler.managed.freecad",
        verifier_version="1.0",
    )


def test_current_compiler_catalog_exposes_exact_small_semantic_envelope() -> None:
    catalog = build_current_compiler_capability_catalog(
        backend=_backend(),
        native_type_catalog=_native_catalog(),
    )
    assert decode_capability_catalog(encode_capability_catalog(catalog)) == catalog
    operations = [item for item in catalog.descriptors if item.kind is CapabilityKind.OPERATION]
    assert len(operations) == 13
    assert len(catalog.external_refs) == 11
    assert len(catalog.relations) == 11
    assert all(item.status is CapabilitySupportStatus.EXECUTABLE for item in operations)
    assert {
        item.native_identifier
        for item in operations
        if item.capability_id.startswith("vibecad.compiler.parametric.feature")
    } == set(_FEATURE_TYPE_IDS.values())
    assert {
        item.native_identifier
        for item in operations
        if item.capability_id.startswith("vibecad.compiler.parametric.edge_treatment")
    } == set(_EDGE_TREATMENT_TYPE_IDS.values())


def test_every_contract_enum_has_one_stable_capability_id() -> None:
    catalog = build_current_compiler_capability_catalog(backend=_backend())
    expected_ids = {
        *(parametric_feature_capability_id(kind) for kind in FeatureKind),
        *(edge_treatment_capability_id(kind) for kind in EdgeTreatmentKind),
        *(freeform_feature_capability_id(kind) for kind in FreeformFeatureKind),
    }
    actual_ids = {
        item.capability_id for item in catalog.descriptors if item.kind is CapabilityKind.OPERATION
    }
    assert actual_ids == expected_ids


def test_contract_family_and_value_are_bound_as_inert_facts() -> None:
    catalog = build_current_compiler_capability_catalog(backend=_backend())
    pad = catalog.lookup(parametric_feature_capability_id(FeatureKind.PAD))
    facts = {item.key_term_ref_id: item.decoded_value for item in pad.facts}
    assert pad.native_identifier == "PartDesign::Pad"
    assert facts == {
        "fact.compiler.contract_family": "parametric_feature",
        "fact.compiler.contract_value": "pad",
    }
    loft = catalog.lookup(freeform_feature_capability_id(FreeformFeatureKind.LOFT))
    assert loft.native_identifier == "Part.makeLoft"


def test_native_type_binding_is_optional_but_exact_when_supplied() -> None:
    unbound = build_current_compiler_capability_catalog(backend=_backend())
    assert not unbound.external_refs
    assert not unbound.relations

    with pytest.raises(CapabilityCatalogError) as mismatch:
        build_current_compiler_capability_catalog(
            backend=_backend(backend_version=(1, 1, 1)),
            native_type_catalog=_native_catalog(),
        )
    assert mismatch.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE


def test_individual_compiler_semantic_requires_receipt_to_be_verified() -> None:
    pad_id = parametric_feature_capability_id(FeatureKind.PAD)
    catalog = build_current_compiler_capability_catalog(
        backend=_backend(),
        verification_by_capability={pad_id: _verification("pad-real-runtime")},
    )
    assert catalog.lookup(pad_id).status is CapabilitySupportStatus.VERIFIED
    assert (
        catalog.lookup(parametric_feature_capability_id(FeatureKind.POCKET)).status
        is CapabilitySupportStatus.EXECUTABLE
    )

    with pytest.raises(CapabilityCatalogError) as unknown:
        build_current_compiler_capability_catalog(
            backend=_backend(),
            verification_by_capability={"vibecad.compiler.unknown": _verification("unknown")},
        )
    assert unknown.value.code is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE


def test_projection_has_no_freecad_import_or_runtime_execution() -> None:
    path = Path(__file__).parents[1] / "src/vibecad/execution/compiler_capabilities.py"
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
    assert not {name for name in imports if name.startswith(("FreeCAD", "Part"))}
    forbidden = {"eval", "exec", "compile", "__import__", "getattr"}
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert forbidden.isdisjoint(called)
