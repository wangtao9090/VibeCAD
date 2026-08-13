"""Focused tests for representable FreeCAD document-object property schemas."""

from __future__ import annotations

import dataclasses
import hashlib

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
from vibecad.execution.capability_index import CapabilityCatalogIndex
from vibecad.execution.freecad_capabilities import (
    FreeCadNativeTypeCategory,
    FreeCadRegisteredType,
    FreeCadTypeRegistrySnapshot,
    build_freecad_type_catalog,
    freecad_type_capability_id,
)
from vibecad.execution.freecad_object_schemas import (
    MAX_FREECAD_PROPERTIES_PER_OBJECT,
    FreeCadDocumentObjectSchema,
    FreeCadInstantiationMode,
    FreeCadObjectSchemaSnapshot,
    FreeCadPropertySchema,
    build_freecad_object_schema_catalog,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _native_catalog():
    snapshot = FreeCadTypeRegistrySnapshot(
        schema_version=1,
        backend_version=(1, 1, 0),
        build_fingerprint_sha256=_sha("managed-build"),
        platform_id="macos.x86_64",
        probe_profile=CapabilityExecutionProfile.HEADLESS,
        probe_modules=("Part",),
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
                native_type_id="Part::Box",
                declaring_module="Part",
                parent_native_type_id="App::DocumentObject",
                category=FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
            ),
        ),
    )
    return build_freecad_type_catalog(snapshot)


def _property(
    name: str,
    property_type: str = "App::PropertyLength",
    **changes: object,
) -> FreeCadPropertySchema:
    values = {
        "native_property_name": name,
        "native_property_type_id": property_type,
        "group_name": "Box",
        "editor_modes": (),
        "status_flags": (),
        "enumeration_values": (),
        "documentation_sha256": _sha(f"documentation:{name}"),
    }
    values.update(changes)
    return FreeCadPropertySchema(**values)


def _schema(**changes: object) -> FreeCadDocumentObjectSchema:
    values = {
        "native_type_id": "Part::Box",
        "instantiation_mode": FreeCadInstantiationMode.TYPE_INSTANCE,
        "properties": (
            _property("Width"),
            _property("Length"),
            _property("Height"),
            _property(
                "MapMode",
                "App::PropertyEnumeration",
                group_name="Attachment",
                enumeration_values=("Deactivated", "FlatFace"),
            ),
        ),
    }
    values.update(changes)
    return FreeCadDocumentObjectSchema(**values)


def _snapshot(catalog, **changes: object) -> FreeCadObjectSchemaSnapshot:
    values = {
        "native_type_catalog_sha256": catalog.catalog_sha256,
        "schemas": (_schema(),),
    }
    values.update(changes)
    return FreeCadObjectSchemaSnapshot(**values)


def test_schema_projection_promotes_discovered_type_to_representable() -> None:
    native = _native_catalog()
    snapshot = _snapshot(native)
    schema_catalog = build_freecad_object_schema_catalog(
        snapshot=snapshot,
        native_type_catalog=native,
    )
    assert decode_capability_catalog(encode_capability_catalog(schema_catalog)) == schema_catalog
    descriptor = schema_catalog.lookup(freecad_type_capability_id("Part::Box"))
    assert descriptor.status is CapabilitySupportStatus.REPRESENTABLE
    assert descriptor.kind is CapabilityKind.DOCUMENT_OBJECT
    assert not descriptor.execution_profiles
    facts = {item.key_term_ref_id: item.decoded_value for item in descriptor.facts}
    assert facts["fact.freecad.instantiation_mode"] == "type_instance"
    assert facts["fact.freecad.native_catalog"] == native.catalog_sha256
    assert [item["native_property_name"] for item in facts["fact.freecad.property_schema"]] == [
        "Height",
        "Length",
        "MapMode",
        "Width",
    ]
    assert len(schema_catalog.external_refs) == 1

    index = CapabilityCatalogIndex((native, schema_catalog))
    assert (
        index.lookup(freecad_type_capability_id("Part::Box")).status
        is CapabilitySupportStatus.REPRESENTABLE
    )


def test_property_schema_is_order_independent_and_binds_enumerations() -> None:
    native = _native_catalog()
    forward = _snapshot(native)
    reverse = _snapshot(
        native,
        schemas=(dataclasses.replace(_schema(), properties=tuple(reversed(_schema().properties))),),
    )
    assert forward.receipt_sha256 == reverse.receipt_sha256
    first = build_freecad_object_schema_catalog(
        snapshot=forward,
        native_type_catalog=native,
    )
    second = build_freecad_object_schema_catalog(
        snapshot=reverse,
        native_type_catalog=native,
    )
    assert first.catalog_sha256 == second.catalog_sha256
    descriptor = first.lookup(freecad_type_capability_id("Part::Box"))
    properties = {
        item["native_property_name"]: item
        for item in next(
            fact.decoded_value
            for fact in descriptor.facts
            if fact.key_term_ref_id == "fact.freecad.property_schema"
        )
    }
    assert properties["MapMode"]["enumeration_values"] == ["Deactivated", "FlatFace"]


def test_schema_requires_exact_native_catalog_and_known_document_object() -> None:
    native = _native_catalog()
    with pytest.raises(CapabilityCatalogError) as digest:
        build_freecad_object_schema_catalog(
            snapshot=_snapshot(native, native_type_catalog_sha256=_sha("wrong")),
            native_type_catalog=native,
        )
    assert digest.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE

    with pytest.raises(CapabilityCatalogError) as unknown:
        build_freecad_object_schema_catalog(
            snapshot=_snapshot(
                native,
                schemas=(_schema(native_type_id="Vendor::Unknown"),),
            ),
            native_type_catalog=native,
        )
    assert unknown.value.code is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE


def test_property_schema_rejects_duplicates_and_budget_overflow() -> None:
    with pytest.raises(CapabilityCatalogError) as duplicate:
        _schema(properties=(_property("Width"), _property("Width")))
    assert duplicate.value.code is CapabilityCatalogErrorCode.INVALID_INPUT

    too_many = tuple(
        _property(f"Property{index}") for index in range(MAX_FREECAD_PROPERTIES_PER_OBJECT + 1)
    )
    with pytest.raises(CapabilityCatalogError) as budget:
        _schema(properties=too_many)
    assert budget.value.code is CapabilityCatalogErrorCode.BUDGET_EXCEEDED


def test_snapshot_change_in_any_property_contract_changes_receipt() -> None:
    native = _native_catalog()
    original = _snapshot(native)
    changed_schema = dataclasses.replace(
        _schema(),
        properties=(
            dataclasses.replace(_schema().properties[0], editor_modes=("ReadOnly",)),
            *_schema().properties[1:],
        ),
    )
    changed = _snapshot(native, schemas=(changed_schema,))
    assert original.receipt_sha256 != changed.receipt_sha256
    first = build_freecad_object_schema_catalog(
        snapshot=original,
        native_type_catalog=native,
    )
    second = build_freecad_object_schema_catalog(
        snapshot=changed,
        native_type_catalog=native,
    )
    assert first.catalog_sha256 != second.catalog_sha256
