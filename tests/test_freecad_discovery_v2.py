"""Focused contract tests for module-scoped FreeCAD discovery pages."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from vibecad.execution.capabilities import (
    MAX_CAPABILITY_CATALOG_BYTES,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
    CapabilitySupportStatus,
    encode_capability_catalog,
)
from vibecad.execution.capability_index import CapabilityCatalogIndex
from vibecad.execution.freecad_capabilities import (
    FreeCadNativeTypeCategory,
    FreeCadRegisteredType,
    freecad_module_capability_id,
    freecad_type_capability_id,
)
from vibecad.execution.freecad_discovery_v2 import (
    FREECAD_DISCOVERY_V2_SCHEMA_VERSION,
    FreeCadDiscoverySnapshotV2,
    build_paged_freecad_type_catalog,
    decode_freecad_capability_manifest,
    encode_freecad_capability_manifest,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _type(
    native_type_id: str,
    *,
    module: str = "Vendor",
    parent: str | None = None,
) -> FreeCadRegisteredType:
    return FreeCadRegisteredType(
        native_type_id=native_type_id,
        declaring_module=module,
        parent_native_type_id=parent,
        category=FreeCadNativeTypeCategory.NATIVE_TYPE,
    )


def _snapshot(
    registered_types: tuple[FreeCadRegisteredType, ...],
    **changes: object,
) -> FreeCadDiscoverySnapshotV2:
    values = {
        "schema_version": FREECAD_DISCOVERY_V2_SCHEMA_VERSION,
        "backend_version": (1, 1, 0),
        "build_fingerprint_sha256": _sha("freecad-build"),
        "platform_id": "macos.arm64",
        "probe_profile": CapabilityExecutionProfile.HEADLESS,
        "probe_modules": ("Vendor",),
        "registered_types": registered_types,
    }
    values.update(changes)
    return FreeCadDiscoverySnapshotV2(**values)


def test_n_and_n_plus_one_page_boundary_is_exact_and_complete() -> None:
    two_types = (_type("Vendor::A"), _type("Vendor::B"))
    at_boundary = build_paged_freecad_type_catalog(_snapshot(two_types), max_descriptors_per_page=3)
    over_boundary = build_paged_freecad_type_catalog(
        _snapshot(two_types + (_type("Vendor::C"),)), max_descriptors_per_page=3
    )

    assert [len(page.descriptors) for page in at_boundary.pages] == [3]
    assert [len(page.descriptors) for page in over_boundary.pages] == [3, 1]
    assert sum(len(page.descriptors) for page in over_boundary.pages) == 4
    assert at_boundary.pages[0].catalog_sha256 == over_boundary.pages[0].catalog_sha256
    assert at_boundary.manifest.manifest_sha256 != over_boundary.manifest.manifest_sha256
    assert over_boundary.manifest.type_count == 3
    assert over_boundary.manifest.module_count == 1


def test_reordered_inputs_have_identical_snapshot_pages_and_manifest() -> None:
    values = (
        _type("Part::Box", module="Part", parent="App::DocumentObject"),
        _type("Base::BaseClass", module="Base"),
        _type("App::DocumentObject", module="App", parent="Base::BaseClass"),
    )
    first = build_paged_freecad_type_catalog(
        _snapshot(values, probe_modules=("Part", "App")),
        max_descriptors_per_page=2,
    )
    second = build_paged_freecad_type_catalog(
        _snapshot(tuple(reversed(values)), probe_modules=("App", "Part")),
        max_descriptors_per_page=2,
    )

    assert first.snapshot.snapshot_sha256 == second.snapshot.snapshot_sha256
    assert [page.catalog_sha256 for page in first.pages] == [
        page.catalog_sha256 for page in second.pages
    ]
    assert first.manifest.manifest_sha256 == second.manifest.manifest_sha256
    assert encode_freecad_capability_manifest(first.manifest) == (
        encode_freecad_capability_manifest(second.manifest)
    )


def test_cross_page_immediate_parent_refs_have_aggregate_closure() -> None:
    values = (
        _type("Base::Root", module="Base"),
        _type("App::Parent", module="App", parent="Base::Root"),
        _type("Part::Leaf", module="Part", parent="App::Parent"),
    )
    bundle = build_paged_freecad_type_catalog(
        _snapshot(values, probe_modules=("Part",)), max_descriptors_per_page=2
    )
    leaf_page = next(
        page
        for page in bundle.pages
        if any(item.native_identifier == "Part::Leaf" for item in page.descriptors)
    )
    external = {item.capability_id: item.descriptor_sha256 for item in leaf_page.external_refs}

    assert freecad_module_capability_id("Part") not in external
    assert freecad_type_capability_id("App::Parent") in external
    assert freecad_type_capability_id("Base::Root") not in external
    assert CapabilityCatalogIndex(bundle.pages).coverage().total == 6

    parent_id = freecad_type_capability_id("App::Parent")
    bad_parent_digest = dataclasses.replace(
        leaf_page,
        external_refs=tuple(
            dataclasses.replace(item, descriptor_sha256=_sha("wrong-parent"))
            if item.capability_id == parent_id
            else item
            for item in leaf_page.external_refs
        ),
    )
    tampered_pages = tuple(
        bad_parent_digest if page is leaf_page else page for page in bundle.pages
    )
    with pytest.raises(CapabilityCatalogError) as unresolved_parent:
        CapabilityCatalogIndex(tampered_pages)
    assert unresolved_parent.value.code is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE


def test_more_than_v1_480_types_fit_bounded_catalog_pages() -> None:
    values = tuple(_type(f"Vendor::Type{index:04d}") for index in range(650))
    bundle = build_paged_freecad_type_catalog(_snapshot(values))

    assert bundle.manifest.type_count == 650
    assert len(bundle.pages) > 1
    assert sum(len(page.descriptors) for page in bundle.pages) == 651
    assert all(
        len(encode_capability_catalog(page)) <= MAX_CAPABILITY_CATALOG_BYTES
        for page in bundle.pages
    )
    assert CapabilityCatalogIndex(bundle.pages).coverage().total == 651


def test_650_type_deep_parent_chain_stays_page_bounded_and_fully_resolves() -> None:
    values = tuple(
        _type(
            f"Vendor::Type{index:04d}",
            parent=f"Vendor::Type{index - 1:04d}" if index else None,
        )
        for index in range(650)
    )
    bundle = build_paged_freecad_type_catalog(_snapshot(values))
    index = CapabilityCatalogIndex(bundle.pages)
    parent_by_child = {
        relation.source_capability_id: relation.target_capability_ids[0]
        for page in bundle.pages
        for relation in page.relations
    }

    assert index.coverage().total == 651
    assert sum(len(page.relations) for page in bundle.pages) == 649
    assert all(len(page.external_refs) <= 2 for page in bundle.pages)
    cursor = freecad_type_capability_id("Vendor::Type0649")
    for expected_index in range(648, -1, -1):
        cursor = parent_by_child[cursor]
        assert cursor == freecad_type_capability_id(f"Vendor::Type{expected_index:04d}")
        assert index.lookup(cursor).status is CapabilitySupportStatus.DISCOVERED
    assert cursor not in parent_by_child


def test_profile_and_build_are_bound_into_snapshot_pages_and_manifest() -> None:
    values = (_type("Vendor::A"),)
    headless = build_paged_freecad_type_catalog(_snapshot(values))
    offscreen = build_paged_freecad_type_catalog(
        _snapshot(values, probe_profile=CapabilityExecutionProfile.OFFSCREEN_GUI)
    )

    assert headless.snapshot.snapshot_sha256 != offscreen.snapshot.snapshot_sha256
    assert headless.pages[0].catalog_sha256 != offscreen.pages[0].catalog_sha256
    assert headless.manifest.manifest_sha256 != offscreen.manifest.manifest_sha256


def test_manifest_round_trip_is_strict_and_digest_checked() -> None:
    bundle = build_paged_freecad_type_catalog(_snapshot((_type("Vendor::A"),)))
    raw = encode_freecad_capability_manifest(bundle.manifest)
    assert decode_freecad_capability_manifest(raw) == bundle.manifest

    parsed = json.loads(raw)
    parsed["page_descriptors"][0]["catalog_sha256"] = _sha("tampered")
    tampered = json.dumps(parsed, separators=(",", ":"), sort_keys=True).encode("ascii")
    with pytest.raises(CapabilityCatalogError) as integrity:
        decode_freecad_capability_manifest(tampered)
    assert integrity.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE

    duplicate = raw[:-1] + b',"schema_version":2}'
    with pytest.raises(CapabilityCatalogError) as duplicate_key:
        decode_freecad_capability_manifest(duplicate)
    assert duplicate_key.value.code is CapabilityCatalogErrorCode.INVALID_INPUT

    noncanonical = json.dumps(json.loads(raw), indent=2).encode("ascii")
    with pytest.raises(CapabilityCatalogError) as formatting:
        decode_freecad_capability_manifest(noncanonical)
    assert formatting.value.code is CapabilityCatalogErrorCode.INVALID_INPUT


def test_manifest_decode_rejects_semantically_noncanonical_page_order() -> None:
    values = tuple(_type(f"Vendor::Type{index}") for index in range(4))
    bundle = build_paged_freecad_type_catalog(_snapshot(values), max_descriptors_per_page=2)
    parsed = json.loads(encode_freecad_capability_manifest(bundle.manifest))
    parsed["page_descriptors"].reverse()
    reordered = json.dumps(parsed, separators=(",", ":"), sort_keys=True).encode("ascii")

    with pytest.raises(CapabilityCatalogError) as error:
        decode_freecad_capability_manifest(reordered)
    assert error.value.code is CapabilityCatalogErrorCode.INVALID_INPUT


def test_all_adversarial_manifest_decode_failures_are_bounded_contract_errors() -> None:
    bundle = build_paged_freecad_type_catalog(_snapshot((_type("Vendor::A"),)))
    valid = json.loads(encode_freecad_capability_manifest(bundle.manifest))

    bad_profile = json.loads(json.dumps(valid))
    bad_profile["backend"]["discovery_profile"] = []
    bad_page = json.loads(json.dumps(valid))
    bad_page["page_descriptors"][0]["page_index"] = {}
    bad_digest = json.loads(json.dumps(valid))
    bad_digest["manifest_sha256"] = []
    nested: object = None
    for _ in range(26):
        nested = [nested]
    malformed_values = (
        b"[]",
        b"\xff",
        json.dumps(
            {"x" * 300: "y" * 4_097}, separators=(",", ":"), sort_keys=True
        ).encode("ascii"),
        json.dumps(bad_profile, separators=(",", ":"), sort_keys=True).encode("ascii"),
        json.dumps(bad_page, separators=(",", ":"), sort_keys=True).encode("ascii"),
        json.dumps(bad_digest, separators=(",", ":"), sort_keys=True).encode("ascii"),
        json.dumps(nested, separators=(",", ":")).encode("ascii"),
    )

    for malformed in malformed_values:
        with pytest.raises(CapabilityCatalogError) as error:
            decode_freecad_capability_manifest(malformed)
        assert type(error.value) is CapabilityCatalogError
        assert len(error.value.path.encode("utf-8")) <= 256


def test_manifest_and_bundle_reject_page_reordering_or_substitution() -> None:
    values = tuple(_type(f"Vendor::Type{index}") for index in range(4))
    bundle = build_paged_freecad_type_catalog(_snapshot(values), max_descriptors_per_page=2)

    with pytest.raises(CapabilityCatalogError) as reordered:
        dataclasses.replace(bundle, pages=tuple(reversed(bundle.pages)))
    assert reordered.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE

    metadata = bundle.manifest.page_descriptors[0]
    with pytest.raises(CapabilityCatalogError) as bad_index:
        dataclasses.replace(metadata, page_index=99)
    assert bad_index.value.code is CapabilityCatalogErrorCode.INVALID_INPUT


def test_snapshot_rejects_dangling_parent_cycles_and_duplicate_types() -> None:
    with pytest.raises(CapabilityCatalogError) as dangling:
        _snapshot((_type("Vendor::A", parent="Vendor::Missing"),))
    assert dangling.value.code is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE

    cycle = (
        _type("Vendor::A", parent="Vendor::B"),
        _type("Vendor::B", parent="Vendor::A"),
    )
    with pytest.raises(CapabilityCatalogError) as cyclic:
        _snapshot(cycle)
    assert cyclic.value.code is CapabilityCatalogErrorCode.INVALID_INPUT

    with pytest.raises(CapabilityCatalogError) as duplicate:
        _snapshot((_type("Vendor::A"), _type("Vendor::A")))
    assert duplicate.value.code is CapabilityCatalogErrorCode.INVALID_INPUT


def test_projection_is_discovered_only_and_has_no_runtime_or_workflow_imports() -> None:
    bundle = build_paged_freecad_type_catalog(_snapshot((_type("Vendor::A"),)))
    assert all(
        descriptor.status is CapabilitySupportStatus.DISCOVERED
        for page in bundle.pages
        for descriptor in page.descriptors
    )

    path = Path(__file__).parents[1] / "src/vibecad/execution/freecad_discovery_v2.py"
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
