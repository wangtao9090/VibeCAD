"""Focused tests for projecting reviewed operations into capability metadata."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
from pathlib import Path

import pytest

from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
    CapabilityKind,
    CapabilityLifecycleStage,
    CapabilitySupportStatus,
    CapabilityVerificationRef,
    decode_capability_catalog,
    encode_capability_catalog,
)
from vibecad.execution.operation_capabilities import (
    build_operation_capability_catalog,
    operation_capability_id,
)
from vibecad.execution.registry import (
    DEFAULT_OPERATION_REGISTRY,
    OperationRegistry,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _backend() -> CapabilityBackend:
    return CapabilityBackend(
        backend_id="freecad",
        backend_version=(1, 1, 0),
        build_fingerprint_sha256=_sha("managed-freecad-build"),
        platform_id="macos.x86_64",
        discovery_profile=CapabilityExecutionProfile.HEADLESS,
    )


def _receipt(label: str) -> CapabilityVerificationRef:
    return CapabilityVerificationRef(
        receipt_sha256=_sha(label),
        receipt_size_bytes=1024,
        verifier_id="vcad.managed.freecad.conformance",
        verifier_version="1.0",
    )


def test_default_registry_projects_all_operations_as_executable() -> None:
    catalog = build_operation_capability_catalog(
        registry=DEFAULT_OPERATION_REGISTRY,
        backend=_backend(),
    )
    assert decode_capability_catalog(encode_capability_catalog(catalog)) == catalog
    assert len(catalog.descriptors) == len(DEFAULT_OPERATION_REGISTRY) + 1
    operations = [item for item in catalog.descriptors if item.kind is CapabilityKind.OPERATION]
    assert len(operations) == 18
    assert all(item.status is CapabilitySupportStatus.EXECUTABLE for item in operations)
    assert all(
        item.execution_profiles == (CapabilityExecutionProfile.HEADLESS,) for item in operations
    )
    assert catalog.lookup(operation_capability_id("inspect_model")).lifecycle_stages == (
        CapabilityLifecycleStage.INSPECT,
    )
    assert catalog.lookup(operation_capability_id("create_box")).lifecycle_stages == (
        CapabilityLifecycleStage.EXECUTE,
    )


def test_only_receipted_operation_is_promoted_to_verified() -> None:
    catalog = build_operation_capability_catalog(
        registry=DEFAULT_OPERATION_REGISTRY,
        backend=_backend(),
        verification_by_operation={"create_box": _receipt("create-box")},
    )
    create_box = catalog.lookup(operation_capability_id("create_box"))
    create_cylinder = catalog.lookup(operation_capability_id("create_cylinder"))
    assert create_box.status is CapabilitySupportStatus.VERIFIED
    assert create_box.verification == _receipt("create-box")
    assert create_cylinder.status is CapabilitySupportStatus.EXECUTABLE
    assert create_cylinder.verification is None


def test_operation_facts_bind_full_registry_field_contract() -> None:
    catalog = build_operation_capability_catalog(
        registry=DEFAULT_OPERATION_REGISTRY,
        backend=_backend(),
    )
    descriptor = catalog.lookup(operation_capability_id("create_box"))
    facts = {item.key_term_ref_id: item.decoded_value for item in descriptor.facts}
    fields = facts["fact.operation.fields"]
    assert [item["name"] for item in fields["arguments"]] == [
        "length_mm",
        "width_mm",
        "height_mm",
        "position_mm",
    ]
    assert [item["handler_parameter"] for item in fields["arguments"]] == [
        "length",
        "width",
        "height",
        "position",
    ]
    assert [item["value_shape"] for item in fields["arguments"]] == [
        "positive_number",
        "positive_number",
        "positive_number",
        "vector3",
    ]
    assert fields["arguments"][-1]["required"] is False
    assert fields["targets"][0]["value_shape"] == "entity_target"
    assert fields["targets"][0]["referenced_value_shape"] == "object_id"
    assert facts["fact.operation.handler_binding"] == "create_box"
    assert facts["fact.operation.resource_budget"]["max_created_objects"] == 1


def test_registry_order_does_not_change_catalog_identity() -> None:
    entries = tuple(DEFAULT_OPERATION_REGISTRY.operations.values())
    forward = build_operation_capability_catalog(
        registry=OperationRegistry(entries),
        backend=_backend(),
    )
    reverse = build_operation_capability_catalog(
        registry=OperationRegistry(tuple(reversed(entries))),
        backend=_backend(),
    )
    assert forward.catalog_sha256 == reverse.catalog_sha256
    assert encode_capability_catalog(forward) == encode_capability_catalog(reverse)


def test_registry_metadata_change_changes_receipt_and_catalog() -> None:
    entries = tuple(DEFAULT_OPERATION_REGISTRY.operations.values())
    modified = tuple(
        dataclasses.replace(item, evidence_required=not item.evidence_required)
        if item.operation == "create_box"
        else item
        for item in entries
    )
    original = build_operation_capability_catalog(
        registry=OperationRegistry(entries),
        backend=_backend(),
    )
    changed = build_operation_capability_catalog(
        registry=OperationRegistry(modified),
        backend=_backend(),
    )
    assert original.discovery_receipt_sha256 != changed.discovery_receipt_sha256
    assert original.catalog_sha256 != changed.catalog_sha256


def test_unknown_or_malformed_verification_map_is_rejected() -> None:
    with pytest.raises(CapabilityCatalogError) as unknown:
        build_operation_capability_catalog(
            registry=DEFAULT_OPERATION_REGISTRY,
            backend=_backend(),
            verification_by_operation={"missing_operation": _receipt("missing")},
        )
    assert unknown.value.code is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE

    with pytest.raises(CapabilityCatalogError) as malformed:
        build_operation_capability_catalog(
            registry=DEFAULT_OPERATION_REGISTRY,
            backend=_backend(),
            verification_by_operation={"create_box": object()},
        )
    assert malformed.value.code is CapabilityCatalogErrorCode.INVALID_INPUT


def test_projection_module_does_not_import_freecad_or_execute_handlers() -> None:
    path = Path(__file__).parents[1] / "src/vibecad/execution/operation_capabilities.py"
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
