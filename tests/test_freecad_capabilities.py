"""Focused projection tests for FreeCAD TypeId capability discovery."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
from pathlib import Path

import pytest

from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
    CapabilityKind,
    CapabilitySupportStatus,
    decode_capability_catalog,
    encode_capability_catalog,
)
from vibecad.execution.freecad_capabilities import (
    MAX_FREECAD_REGISTERED_TYPES,
    FreeCadNativeTypeCategory,
    FreeCadRegisteredType,
    FreeCadTypeRegistrySnapshot,
    build_freecad_type_catalog,
    freecad_module_capability_id,
    freecad_type_capability_id,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _types() -> tuple[FreeCadRegisteredType, ...]:
    return (
        FreeCadRegisteredType(
            native_type_id="Part::Box",
            declaring_module="Part",
            parent_native_type_id="App::DocumentObject",
            category=FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
        ),
        FreeCadRegisteredType(
            native_type_id="App::DocumentObject",
            declaring_module="App",
            parent_native_type_id="Base::BaseClass",
            category=FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
        ),
        FreeCadRegisteredType(
            native_type_id="App::PropertyLength",
            declaring_module="App",
            parent_native_type_id="Base::BaseClass",
            category=FreeCadNativeTypeCategory.PROPERTY_TYPE,
        ),
        FreeCadRegisteredType(
            native_type_id="App::Extension",
            declaring_module="App",
            parent_native_type_id="Base::BaseClass",
            category=FreeCadNativeTypeCategory.EXTENSION_TYPE,
        ),
        FreeCadRegisteredType(
            native_type_id="Base::BaseClass",
            declaring_module="Base",
            parent_native_type_id=None,
            category=FreeCadNativeTypeCategory.NATIVE_TYPE,
        ),
    )


def _snapshot(**changes: object) -> FreeCadTypeRegistrySnapshot:
    values = {
        "schema_version": 1,
        "backend_version": (1, 1, 3),
        "build_fingerprint_sha256": _sha("freecad-build"),
        "platform_id": "macos.x86_64",
        "probe_profile": CapabilityExecutionProfile.HEADLESS,
        "probe_modules": ("Part",),
        "registered_types": _types(),
    }
    values.update(changes)
    return FreeCadTypeRegistrySnapshot(**values)


def test_snapshot_and_projection_are_order_independent_and_canonical() -> None:
    snapshot = _snapshot()
    reordered = _snapshot(
        probe_modules=tuple(reversed(snapshot.probe_modules)),
        registered_types=tuple(reversed(snapshot.registered_types)),
    )
    assert snapshot.receipt_sha256 == reordered.receipt_sha256

    catalog = build_freecad_type_catalog(snapshot)
    decoded = decode_capability_catalog(encode_capability_catalog(catalog))
    assert decoded == catalog
    assert catalog.discovery_receipt_sha256 == snapshot.receipt_sha256
    assert len(catalog.descriptors) == 8  # 5 native types + Base/App/Part modules
    assert len(catalog.relations) == 4
    assert all(
        descriptor.status is CapabilitySupportStatus.DISCOVERED
        for descriptor in catalog.descriptors
    )


def test_native_type_categories_map_without_feature_specific_enums() -> None:
    catalog = build_freecad_type_catalog(_snapshot())
    expected = {
        "Part::Box": CapabilityKind.DOCUMENT_OBJECT,
        "App::DocumentObject": CapabilityKind.DOCUMENT_OBJECT,
        "App::PropertyLength": CapabilityKind.PROPERTY_TYPE,
        "App::Extension": CapabilityKind.EXTENSION_TYPE,
        "Base::BaseClass": CapabilityKind.NATIVE_TYPE,
    }
    for native_type_id, kind in expected.items():
        descriptor = catalog.lookup(freecad_type_capability_id(native_type_id))
        assert descriptor.native_identifier == native_type_id
        assert descriptor.kind is kind
        assert not descriptor.execution_profiles
        assert descriptor.verification is None


def test_native_ids_are_stable_across_runtime_builds_but_catalog_is_not() -> None:
    first = build_freecad_type_catalog(_snapshot())
    second = build_freecad_type_catalog(
        _snapshot(
            backend_version=(1, 2, 0),
            build_fingerprint_sha256=_sha("next-build"),
        )
    )
    assert freecad_type_capability_id("Part::Box") == freecad_type_capability_id("Part::Box")
    assert freecad_module_capability_id("Part") == freecad_module_capability_id("Part")
    assert first.catalog_sha256 != second.catalog_sha256


def test_snapshot_rejects_dangling_parent_and_cycles() -> None:
    dangling = (
        FreeCadRegisteredType(
            native_type_id="Part::Box",
            declaring_module="Part",
            parent_native_type_id="App::Missing",
            category=FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
        ),
    )
    with pytest.raises(CapabilityCatalogError) as missing:
        _snapshot(registered_types=dangling)
    assert missing.value.code is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE

    cycle = (
        FreeCadRegisteredType(
            native_type_id="Vendor::A",
            declaring_module="Vendor",
            parent_native_type_id="Vendor::B",
            category=FreeCadNativeTypeCategory.NATIVE_TYPE,
        ),
        FreeCadRegisteredType(
            native_type_id="Vendor::B",
            declaring_module="Vendor",
            parent_native_type_id="Vendor::A",
            category=FreeCadNativeTypeCategory.NATIVE_TYPE,
        ),
    )
    with pytest.raises(CapabilityCatalogError) as cyclic:
        _snapshot(registered_types=cycle)
    assert cyclic.value.code is CapabilityCatalogErrorCode.INVALID_INPUT


def test_snapshot_rejects_duplicate_type_and_probe_module() -> None:
    with pytest.raises(CapabilityCatalogError) as duplicate_type:
        _snapshot(registered_types=(_types()[0], _types()[0]))
    assert duplicate_type.value.code is CapabilityCatalogErrorCode.INVALID_INPUT

    with pytest.raises(CapabilityCatalogError) as duplicate_module:
        _snapshot(probe_modules=("Part", "Part"))
    assert duplicate_module.value.code is CapabilityCatalogErrorCode.INVALID_INPUT


def test_registry_and_catalog_budgets_fail_closed_independently() -> None:
    too_many = tuple(
        FreeCadRegisteredType(
            native_type_id=f"Vendor::Type{index}",
            declaring_module="Vendor",
            parent_native_type_id=None,
            category=FreeCadNativeTypeCategory.NATIVE_TYPE,
        )
        for index in range(MAX_FREECAD_REGISTERED_TYPES + 1)
    )
    with pytest.raises(CapabilityCatalogError) as registry_budget:
        _snapshot(registered_types=too_many)
    assert registry_budget.value.code is CapabilityCatalogErrorCode.BUDGET_EXCEEDED

    descriptor_overflow = tuple(
        FreeCadRegisteredType(
            native_type_id=f"Vendor{index % 33}::Type{index}",
            declaring_module=f"Vendor{index % 33}",
            parent_native_type_id=None,
            category=FreeCadNativeTypeCategory.NATIVE_TYPE,
        )
        for index in range(MAX_FREECAD_REGISTERED_TYPES)
    )
    snapshot = _snapshot(probe_modules=(), registered_types=descriptor_overflow)
    with pytest.raises(CapabilityCatalogError) as catalog_budget:
        build_freecad_type_catalog(snapshot)
    assert catalog_budget.value.code is CapabilityCatalogErrorCode.BUDGET_EXCEEDED


def test_probe_and_declaring_modules_are_both_represented() -> None:
    catalog = build_freecad_type_catalog(_snapshot(probe_modules=("PartDesign",)))
    identifiers = {descriptor.native_identifier for descriptor in catalog.descriptors}
    assert {"App", "Base", "Part", "PartDesign"} <= identifiers


def test_discovery_projection_never_promotes_native_types() -> None:
    source = _snapshot()
    catalog = build_freecad_type_catalog(source)
    box = catalog.lookup(freecad_type_capability_id("Part::Box"))
    with pytest.raises(CapabilityCatalogError) as failure:
        dataclasses.replace(box, status=CapabilitySupportStatus.EXECUTABLE)
    assert failure.value.code is CapabilityCatalogErrorCode.INVALID_STATUS


def test_discovery_module_has_no_freecad_or_workflow_import() -> None:
    path = Path(__file__).parents[1] / "src/vibecad/execution/freecad_capabilities.py"
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
