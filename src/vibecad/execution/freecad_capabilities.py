"""Pure FreeCAD TypeId discovery records and capability-catalog projection.

This module does not import FreeCAD and does not execute a discovered type.  A
runtime-owned probe supplies an exact snapshot of the TypeId registry; the
snapshot is reduced to an authority-free ``CapabilityCatalogSegment`` whose
entries all begin at ``discovered``.  Execution adapters and conformance gates
must promote individual descriptors separately.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum

from vibecad.execution.capabilities import (
    MAX_CAPABILITY_DESCRIPTORS,
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityCatalogSegment,
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityKind,
    CapabilityRelation,
    CapabilityRiskClass,
    CapabilitySupportStatus,
    CapabilityTermRef,
)

FREECAD_TYPE_REGISTRY_SNAPSHOT_SCHEMA_VERSION = 1
MAX_FREECAD_REGISTERED_TYPES = 480
MAX_FREECAD_DECLARING_MODULES = 64
MAX_FREECAD_PROBE_MODULES = 64
MAX_FREECAD_TYPE_SNAPSHOT_BYTES = 192 * 1024

_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_JSON_DEPTH = 16
_MAX_JSON_NODES = 8_192
_MAX_JSON_STRING_BYTES = 4_096
_MAX_NAME_BYTES = 192
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,191}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_DOMAIN = b"vibecad-freecad-type-registry-snapshot-v1\0"
_NATIVE_TYPE_ID_DOMAIN = b"vibecad-freecad-native-type-id-v1\0"
_MODULE_ID_DOMAIN = b"vibecad-freecad-module-id-v1\0"
_RELATION_ID_DOMAIN = b"vibecad-freecad-type-relation-id-v1\0"


class FreeCadNativeTypeCategory(StrEnum):
    NATIVE_TYPE = "native_type"
    DOCUMENT_OBJECT = "document_object"
    PROPERTY_TYPE = "property_type"
    EXTENSION_TYPE = "extension_type"


def _fail(code: CapabilityCatalogErrorCode, path: str) -> None:
    raise CapabilityCatalogError(code, path)


def _name(value: object, path: str) -> str:
    if type(value) is not str:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if not value or size > _MAX_NAME_BYTES or _NAME.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if ".." in value or "//" in value:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _version(value: object, path: str) -> tuple[int, ...]:
    if (
        type(value) is not tuple
        or not 1 <= len(value) <= 4
        or not all(type(item) is int and 0 <= item <= 999_999 for item in value)
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _json_tree(value: object, path: str, depth: int, remaining: list[int]) -> None:
    remaining[0] -= 1
    if remaining[0] < 0 or depth > _MAX_JSON_DEPTH:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, path)
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > _MAX_SAFE_INTEGER:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
        return
    if type(value) is str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
        if size > _MAX_JSON_STRING_BYTES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, path)
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _json_tree(item, f"{path}/{index}", depth + 1, remaining)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
            _json_tree(key, path, depth + 1, remaining)
            _json_tree(item, f"{path}/{key}", depth + 1, remaining)
        return
    _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)


def _canonical(value: object) -> bytes:
    _json_tree(value, "", 0, [_MAX_JSON_NODES])
    try:
        raw = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "")
    if not raw or len(raw) > MAX_FREECAD_TYPE_SNAPSHOT_BYTES:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "")
    return raw


def _stable_id(domain: bytes, value: str, prefix: str) -> str:
    digest = hashlib.sha256(domain + value.encode("utf-8")).hexdigest()
    return f"{prefix}.{digest[:32]}"


def freecad_module_capability_id(native_module: str) -> str:
    _name(native_module, "native_module")
    return _stable_id(_MODULE_ID_DOMAIN, native_module, "freecad.module")


def freecad_type_capability_id(native_type_id: str) -> str:
    _name(native_type_id, "native_type_id")
    return _stable_id(_NATIVE_TYPE_ID_DOMAIN, native_type_id, "freecad.type")


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadRegisteredType:
    native_type_id: str
    declaring_module: str
    parent_native_type_id: str | None
    category: FreeCadNativeTypeCategory

    def __post_init__(self) -> None:
        _name(self.native_type_id, "native_type_id")
        _name(self.declaring_module, "declaring_module")
        if self.parent_native_type_id is not None:
            _name(self.parent_native_type_id, "parent_native_type_id")
            if self.parent_native_type_id == self.native_type_id:
                _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "parent_native_type_id")
        if type(self.category) is not FreeCadNativeTypeCategory:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "category")


def _type_mapping(value: FreeCadRegisteredType) -> dict[str, object]:
    return {
        "category": value.category.value,
        "declaring_module": value.declaring_module,
        "native_type_id": value.native_type_id,
        "parent_native_type_id": value.parent_native_type_id,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadTypeRegistrySnapshot:
    schema_version: int
    backend_version: tuple[int, ...]
    build_fingerprint_sha256: str
    platform_id: str
    probe_profile: CapabilityExecutionProfile
    probe_modules: tuple[str, ...]
    registered_types: tuple[FreeCadRegisteredType, ...]
    probe_algorithm_version: str = "1.0"

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != FREECAD_TYPE_REGISTRY_SNAPSHOT_SCHEMA_VERSION
        ):
            _fail(CapabilityCatalogErrorCode.UNSUPPORTED_VERSION, "schema_version")
        object.__setattr__(
            self, "backend_version", _version(self.backend_version, "backend_version")
        )
        _digest(self.build_fingerprint_sha256, "build_fingerprint_sha256")
        _name(self.platform_id, "platform_id")
        if type(self.probe_profile) is not CapabilityExecutionProfile:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "probe_profile")
        if type(self.probe_modules) is not tuple:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "probe_modules")
        if len(self.probe_modules) > MAX_FREECAD_PROBE_MODULES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "probe_modules")
        modules = tuple(_name(item, "probe_modules") for item in self.probe_modules)
        if len(set(modules)) != len(modules):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "probe_modules")
        if type(self.registered_types) is not tuple:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "registered_types")
        if len(self.registered_types) > MAX_FREECAD_REGISTERED_TYPES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "registered_types")
        if not all(type(item) is FreeCadRegisteredType for item in self.registered_types):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "registered_types")
        type_ids = tuple(item.native_type_id for item in self.registered_types)
        if len(set(type_ids)) != len(type_ids):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "registered_types")
        known = set(type_ids)
        if any(
            item.parent_native_type_id is not None and item.parent_native_type_id not in known
            for item in self.registered_types
        ):
            _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "registered_types")
        _verify_parent_graph(self.registered_types)
        declaring = {item.declaring_module for item in self.registered_types}
        if len(declaring) > MAX_FREECAD_DECLARING_MODULES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "registered_types")
        _name(self.probe_algorithm_version, "probe_algorithm_version")
        object.__setattr__(self, "probe_modules", tuple(sorted(modules)))
        object.__setattr__(
            self,
            "registered_types",
            tuple(sorted(self.registered_types, key=lambda item: item.native_type_id)),
        )
        _canonical(self._mapping())

    def _mapping(self) -> dict[str, object]:
        return {
            "backend_version": list(self.backend_version),
            "build_fingerprint_sha256": self.build_fingerprint_sha256,
            "platform_id": self.platform_id,
            "probe_algorithm_version": self.probe_algorithm_version,
            "probe_modules": list(self.probe_modules),
            "probe_profile": self.probe_profile.value,
            "registered_types": [_type_mapping(item) for item in self.registered_types],
            "schema_version": self.schema_version,
        }

    @property
    def receipt_sha256(self) -> str:
        return hashlib.sha256(_RECEIPT_DOMAIN + _canonical(self._mapping())).hexdigest()


def _verify_parent_graph(values: tuple[FreeCadRegisteredType, ...]) -> None:
    parents = {item.native_type_id: item.parent_native_type_id for item in values}
    state: dict[str, int] = {}

    def visit(native_type_id: str) -> None:
        mark = state.get(native_type_id, 0)
        if mark == 1:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "registered_types")
        if mark == 2:
            return
        state[native_type_id] = 1
        parent = parents[native_type_id]
        if parent is not None:
            visit(parent)
        state[native_type_id] = 2

    for native_type_id in parents:
        visit(native_type_id)


_TERM_SPECS = {
    "relation.native.inherits": "relation/native-inherits",
    "semantic.freecad.document_object": "semantic/freecad-document-object",
    "semantic.freecad.extension_type": "semantic/freecad-extension-type",
    "semantic.freecad.module": "semantic/freecad-module",
    "semantic.freecad.native_type": "semantic/freecad-native-type",
    "semantic.freecad.property_type": "semantic/freecad-property-type",
}


def _terms() -> tuple[CapabilityTermRef, ...]:
    return tuple(
        CapabilityTermRef(
            term_ref_id=term_ref_id,
            namespace="vcad.freecad.capability",
            vocabulary_version="1.0",
            term_id=term_id,
            term_definition_sha256=hashlib.sha256(
                f"vcad.freecad.capability/1.0/{term_id}".encode("ascii")
            ).hexdigest(),
        )
        for term_ref_id, term_id in sorted(_TERM_SPECS.items())
    )


def _kind(category: FreeCadNativeTypeCategory) -> CapabilityKind:
    return {
        FreeCadNativeTypeCategory.NATIVE_TYPE: CapabilityKind.NATIVE_TYPE,
        FreeCadNativeTypeCategory.DOCUMENT_OBJECT: CapabilityKind.DOCUMENT_OBJECT,
        FreeCadNativeTypeCategory.PROPERTY_TYPE: CapabilityKind.PROPERTY_TYPE,
        FreeCadNativeTypeCategory.EXTENSION_TYPE: CapabilityKind.EXTENSION_TYPE,
    }[category]


def _semantic_term(category: FreeCadNativeTypeCategory) -> str:
    return {
        FreeCadNativeTypeCategory.NATIVE_TYPE: "semantic.freecad.native_type",
        FreeCadNativeTypeCategory.DOCUMENT_OBJECT: "semantic.freecad.document_object",
        FreeCadNativeTypeCategory.PROPERTY_TYPE: "semantic.freecad.property_type",
        FreeCadNativeTypeCategory.EXTENSION_TYPE: "semantic.freecad.extension_type",
    }[category]


def build_freecad_type_catalog(
    snapshot: FreeCadTypeRegistrySnapshot,
) -> CapabilityCatalogSegment:
    """Project one exact registry snapshot into discovered-only descriptors."""

    if type(snapshot) is not FreeCadTypeRegistrySnapshot:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "snapshot")
    declaring_modules = {item.declaring_module for item in snapshot.registered_types}
    modules = tuple(sorted(declaring_modules | set(snapshot.probe_modules)))
    if len(modules) + len(snapshot.registered_types) > MAX_CAPABILITY_DESCRIPTORS:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "registered_types")
    descriptors: list[CapabilityDescriptor] = []
    for native_module in modules:
        capability_id = freecad_module_capability_id(native_module)
        descriptors.append(
            CapabilityDescriptor(
                capability_id=capability_id,
                kind=CapabilityKind.MODULE,
                native_identifier=native_module,
                declaring_module_id=capability_id,
                status=CapabilitySupportStatus.DISCOVERED,
                risk_class=CapabilityRiskClass.UNKNOWN,
                semantic_term_ref_ids=("semantic.freecad.module",),
            )
        )
    relations: list[CapabilityRelation] = []
    for native_type in snapshot.registered_types:
        capability_id = freecad_type_capability_id(native_type.native_type_id)
        descriptors.append(
            CapabilityDescriptor(
                capability_id=capability_id,
                kind=_kind(native_type.category),
                native_identifier=native_type.native_type_id,
                declaring_module_id=freecad_module_capability_id(native_type.declaring_module),
                status=CapabilitySupportStatus.DISCOVERED,
                risk_class=CapabilityRiskClass.UNKNOWN,
                semantic_term_ref_ids=(_semantic_term(native_type.category),),
            )
        )
        if native_type.parent_native_type_id is not None:
            parent_id = freecad_type_capability_id(native_type.parent_native_type_id)
            relation_digest = hashlib.sha256(
                _RELATION_ID_DOMAIN
                + native_type.native_type_id.encode("utf-8")
                + b"\0"
                + native_type.parent_native_type_id.encode("utf-8")
            ).hexdigest()
            relations.append(
                CapabilityRelation(
                    relation_id=f"freecad.relation.inherits.{relation_digest[:32]}",
                    relation_term_ref_id="relation.native.inherits",
                    source_capability_id=capability_id,
                    target_capability_ids=(parent_id,),
                )
            )
    return CapabilityCatalogSegment(
        schema_version=1,
        segment_id=f"freecad.types.{snapshot.receipt_sha256[:32]}",
        backend=CapabilityBackend(
            backend_id="freecad",
            backend_version=snapshot.backend_version,
            build_fingerprint_sha256=snapshot.build_fingerprint_sha256,
            platform_id=snapshot.platform_id,
            discovery_profile=snapshot.probe_profile,
        ),
        discovery_receipt_sha256=snapshot.receipt_sha256,
        discovery_algorithm_id="vcad.freecad.typeid.registry",
        discovery_algorithm_version=snapshot.probe_algorithm_version,
        terms=_terms(),
        descriptors=tuple(descriptors),
        relations=tuple(relations),
    )


__all__ = ()
