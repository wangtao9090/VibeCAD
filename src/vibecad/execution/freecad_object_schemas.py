"""Representable FreeCAD document-object property schemas.

Object schemas promote a discovered document-object type to ``representable``;
they do not make it executable.  Native property identifiers remain inert and
the complete schema is bound to one exact native TypeId catalog digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityCatalogSegment,
    CapabilityDescriptor,
    CapabilityFact,
    CapabilityKind,
    CapabilitySupportStatus,
    CapabilityTermRef,
    ExternalCapabilityRef,
)

MAX_FREECAD_OBJECT_SCHEMAS = 128
MAX_FREECAD_PROPERTIES_PER_OBJECT = 256
MAX_FREECAD_TOTAL_PROPERTIES = 4_096
MAX_FREECAD_ENUM_VALUES_PER_PROPERTY = 256
MAX_FREECAD_PROPERTY_FLAGS = 32
MAX_FREECAD_PROPERTY_TEXT_BYTES = 256
MAX_FREECAD_PROPERTY_DOCUMENTATION_BYTES = 64 * 1024
MAX_FREECAD_OBJECT_SCHEMA_SNAPSHOT_BYTES = 256 * 1024

_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._:+/@ -]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SNAPSHOT_DOMAIN = b"vibecad-freecad-object-schema-snapshot-v1\0"


def _fail(code: CapabilityCatalogErrorCode, path: str) -> None:
    raise CapabilityCatalogError(code, path)


def _text(value: object, path: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if size > MAX_FREECAD_PROPERTY_TEXT_BYTES or (
        not allow_empty and (not value or _NAME.fullmatch(value) is None)
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if value and (not value.isprintable() or len(value.splitlines()) != 1):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _text_tuple(
    value: object,
    path: str,
    *,
    maximum: int,
    allow_empty_items: bool = False,
) -> tuple[str, ...]:
    if type(value) is not tuple:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if len(value) > maximum:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, path)
    result = tuple(
        _text(item, f"{path}/{index}", allow_empty=allow_empty_items)
        for index, item in enumerate(value)
    )
    if len(set(result)) != len(result):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return tuple(sorted(result))


class FreeCadInstantiationMode(StrEnum):
    TYPE_INSTANCE = "type_instance"
    DOCUMENT_OBJECT = "document_object"


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadPropertySchema:
    native_property_name: str
    native_property_type_id: str
    group_name: str
    editor_modes: tuple[str, ...]
    status_flags: tuple[str, ...]
    enumeration_values: tuple[str, ...]
    documentation_sha256: str

    def __post_init__(self) -> None:
        _text(self.native_property_name, "native_property_name")
        _text(self.native_property_type_id, "native_property_type_id")
        _text(self.group_name, "group_name", allow_empty=True)
        object.__setattr__(
            self,
            "editor_modes",
            _text_tuple(
                self.editor_modes,
                "editor_modes",
                maximum=MAX_FREECAD_PROPERTY_FLAGS,
            ),
        )
        object.__setattr__(
            self,
            "status_flags",
            _text_tuple(
                self.status_flags,
                "status_flags",
                maximum=MAX_FREECAD_PROPERTY_FLAGS,
            ),
        )
        object.__setattr__(
            self,
            "enumeration_values",
            _text_tuple(
                self.enumeration_values,
                "enumeration_values",
                maximum=MAX_FREECAD_ENUM_VALUES_PER_PROPERTY,
                allow_empty_items=True,
            ),
        )
        _digest(self.documentation_sha256, "documentation_sha256")


def _property_mapping(value: FreeCadPropertySchema) -> dict[str, object]:
    return {
        "documentation_sha256": value.documentation_sha256,
        "editor_modes": list(value.editor_modes),
        "enumeration_values": list(value.enumeration_values),
        "group_name": value.group_name,
        "native_property_name": value.native_property_name,
        "native_property_type_id": value.native_property_type_id,
        "status_flags": list(value.status_flags),
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadDocumentObjectSchema:
    native_type_id: str
    instantiation_mode: FreeCadInstantiationMode
    properties: tuple[FreeCadPropertySchema, ...]

    def __post_init__(self) -> None:
        _text(self.native_type_id, "native_type_id")
        if type(self.instantiation_mode) is not FreeCadInstantiationMode:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "instantiation_mode")
        if type(self.properties) is not tuple:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "properties")
        if len(self.properties) > MAX_FREECAD_PROPERTIES_PER_OBJECT:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "properties")
        if not all(type(item) is FreeCadPropertySchema for item in self.properties):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "properties")
        names = tuple(item.native_property_name for item in self.properties)
        if len(set(names)) != len(names):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "properties")
        object.__setattr__(
            self,
            "properties",
            tuple(sorted(self.properties, key=lambda item: item.native_property_name)),
        )


def _schema_mapping(value: FreeCadDocumentObjectSchema) -> dict[str, object]:
    return {
        "instantiation_mode": value.instantiation_mode.value,
        "native_type_id": value.native_type_id,
        "properties": [_property_mapping(item) for item in value.properties],
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadObjectSchemaSnapshot:
    native_type_catalog_sha256: str
    schemas: tuple[FreeCadDocumentObjectSchema, ...]
    probe_algorithm_version: str = "1.0"

    def __post_init__(self) -> None:
        _digest(self.native_type_catalog_sha256, "native_type_catalog_sha256")
        if type(self.schemas) is not tuple:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "schemas")
        if len(self.schemas) > MAX_FREECAD_OBJECT_SCHEMAS:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "schemas")
        if not all(type(item) is FreeCadDocumentObjectSchema for item in self.schemas):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "schemas")
        native_ids = tuple(item.native_type_id for item in self.schemas)
        if len(set(native_ids)) != len(native_ids):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "schemas")
        if sum(len(item.properties) for item in self.schemas) > MAX_FREECAD_TOTAL_PROPERTIES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "schemas/properties")
        _text(self.probe_algorithm_version, "probe_algorithm_version")
        object.__setattr__(
            self,
            "schemas",
            tuple(sorted(self.schemas, key=lambda item: item.native_type_id)),
        )
        raw = self._canonical_bytes()
        if len(raw) > MAX_FREECAD_OBJECT_SCHEMA_SNAPSHOT_BYTES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "schemas")

    def _body(self) -> dict[str, object]:
        return {
            "native_type_catalog_sha256": self.native_type_catalog_sha256,
            "probe_algorithm_version": self.probe_algorithm_version,
            "schemas": [_schema_mapping(item) for item in self.schemas],
        }

    def _canonical_bytes(self) -> bytes:
        try:
            return json.dumps(
                self._body(),
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "schemas")

    @property
    def receipt_sha256(self) -> str:
        return hashlib.sha256(_SNAPSHOT_DOMAIN + self._canonical_bytes()).hexdigest()


_TERM_SPECS = {
    "fact.freecad.instantiation_mode": "fact/freecad-instantiation-mode",
    "fact.freecad.native_catalog": "fact/freecad-native-catalog",
    "fact.freecad.property_schema": "fact/freecad-property-schema",
    "semantic.freecad.document_object": "semantic/freecad-document-object",
    "semantic.freecad.object_schema": "semantic/freecad-object-schema",
}


def _terms() -> tuple[CapabilityTermRef, ...]:
    return tuple(
        CapabilityTermRef(
            term_ref_id=term_ref_id,
            namespace=(
                "vcad.freecad.capability"
                if term_ref_id == "semantic.freecad.document_object"
                else "vcad.freecad.object-schema"
            ),
            vocabulary_version="1.0",
            term_id=term_id,
            term_definition_sha256=hashlib.sha256(
                (
                    f"vcad.freecad.capability/1.0/{term_id}"
                    if term_ref_id == "semantic.freecad.document_object"
                    else f"vcad.freecad.object-schema/1.0/{term_id}"
                ).encode("ascii")
            ).hexdigest(),
        )
        for term_ref_id, term_id in sorted(_TERM_SPECS.items())
    )


def build_freecad_object_schema_catalog(
    *,
    snapshot: FreeCadObjectSchemaSnapshot,
    native_type_catalog: CapabilityCatalogSegment,
) -> CapabilityCatalogSegment:
    """Promote exact native document-object descriptors to representable."""

    if type(snapshot) is not FreeCadObjectSchemaSnapshot:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "snapshot")
    if type(native_type_catalog) is not CapabilityCatalogSegment:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "native_type_catalog")
    if snapshot.native_type_catalog_sha256 != native_type_catalog.catalog_sha256:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "native_type_catalog")
    by_native = {
        item.native_identifier: item
        for item in native_type_catalog.descriptors
        if item.kind is CapabilityKind.DOCUMENT_OBJECT
    }
    all_by_id = {item.capability_id: item for item in native_type_catalog.descriptors}
    descriptors: list[CapabilityDescriptor] = []
    external: dict[str, ExternalCapabilityRef] = {}
    for schema in snapshot.schemas:
        base = by_native.get(schema.native_type_id)
        if base is None or base.status is not CapabilitySupportStatus.DISCOVERED:
            _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, schema.native_type_id)
        module = all_by_id.get(base.declaring_module_id)
        if module is None or module.kind is not CapabilityKind.MODULE:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, schema.native_type_id)
        external[module.capability_id] = ExternalCapabilityRef(
            capability_id=module.capability_id,
            descriptor_sha256=module.descriptor_sha256,
        )
        descriptors.append(
            CapabilityDescriptor(
                capability_id=base.capability_id,
                kind=base.kind,
                native_identifier=base.native_identifier,
                declaring_module_id=base.declaring_module_id,
                status=CapabilitySupportStatus.REPRESENTABLE,
                risk_class=base.risk_class,
                semantic_term_ref_ids=(
                    "semantic.freecad.document_object",
                    "semantic.freecad.object_schema",
                ),
                facts=(
                    CapabilityFact(
                        key_term_ref_id="fact.freecad.instantiation_mode",
                        value=schema.instantiation_mode.value,
                    ),
                    CapabilityFact(
                        key_term_ref_id="fact.freecad.native_catalog",
                        value=snapshot.native_type_catalog_sha256,
                    ),
                    CapabilityFact(
                        key_term_ref_id="fact.freecad.property_schema",
                        value=[_property_mapping(item) for item in schema.properties],
                    ),
                ),
                dependency_ids=base.dependency_ids,
            )
        )
    return CapabilityCatalogSegment(
        schema_version=1,
        segment_id=f"freecad.object_schemas.{snapshot.receipt_sha256[:32]}",
        backend=native_type_catalog.backend,
        discovery_receipt_sha256=snapshot.receipt_sha256,
        discovery_algorithm_id="vcad.freecad.object-property-schema",
        discovery_algorithm_version=snapshot.probe_algorithm_version,
        terms=_terms(),
        descriptors=tuple(descriptors),
        external_refs=tuple(external.values()),
    )


__all__ = ()
