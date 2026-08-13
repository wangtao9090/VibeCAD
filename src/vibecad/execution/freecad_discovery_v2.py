"""Bounded, module-scoped paging for discovered FreeCAD TypeIds.

This module is a pure projection layer.  It accepts an exact, already-collected
registry snapshot and emits ordinary capability-catalog segments plus a small
content-addressed manifest.  No discovered descriptor is executable, no
FreeCAD module is imported, and no durable or public transport contract is
changed here.

Descriptors have one owning page.  A page may refer to its declaring module or
its immediate parent type in another page through ``ExternalCapabilityRef``.
The aggregate index validates the complete parent chain, keeping deep native
inheritance bounded without duplicating every ancestor into each leaf page.
A standalone page is only a transport segment, not a closed capability view;
consumers must pass the manifest's ordered, complete page set through
``validate_freecad_capability_page_set`` before treating external references
or transitive ancestry as resolved.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass

from vibecad.execution.capabilities import (
    CAPABILITY_CATALOG_SCHEMA_VERSION,
    MAX_CAPABILITY_CATALOG_BYTES,
    MAX_CAPABILITY_DESCRIPTORS,
    MAX_CAPABILITY_EXTERNAL_REFS,
    MAX_CAPABILITY_RELATIONS,
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
    ExternalCapabilityRef,
    encode_capability_catalog,
)
from vibecad.execution.capability_index import CapabilityCatalogIndex
from vibecad.execution.freecad_capabilities import (
    FreeCadNativeTypeCategory,
    FreeCadRegisteredType,
    freecad_module_capability_id,
    freecad_type_capability_id,
)

FREECAD_DISCOVERY_V2_SCHEMA_VERSION = 2
FREECAD_DISCOVERY_V2_ALGORITHM_ID = "vcad.freecad.typeid.registry.paged"
FREECAD_DISCOVERY_V2_ALGORITHM_VERSION = "2.0"
MAX_FREECAD_DISCOVERY_V2_TYPES = 8_192
MAX_FREECAD_DISCOVERY_V2_MODULES = 64
MAX_FREECAD_DISCOVERY_V2_DESCRIPTORS = 8_192
MAX_FREECAD_DISCOVERY_V2_PAGES = 64
MAX_FREECAD_DISCOVERY_V2_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_FREECAD_DISCOVERY_V2_MANIFEST_BYTES = 256 * 1024
DEFAULT_FREECAD_DISCOVERY_V2_PAGE_DESCRIPTORS = 192

_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_JSON_DEPTH = 24
_MAX_JSON_NODES = 131_072
_MAX_JSON_STRING_BYTES = 4_096
_MAX_NAME_BYTES = 192
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,191}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SNAPSHOT_DIGEST_DOMAIN = b"vibecad-freecad-discovery-snapshot-v2\0"
_PAGE_RECEIPT_DOMAIN = b"vibecad-freecad-discovery-page-receipt-v2\0"
_MANIFEST_DIGEST_DOMAIN = b"vibecad-freecad-discovery-manifest-v2\0"
_RELATION_ID_DOMAIN = b"vibecad-freecad-type-relation-id-v1\0"

_TERM_SPECS = {
    "relation.native.inherits": "relation/native-inherits",
    "semantic.freecad.document_object": "semantic/freecad-document-object",
    "semantic.freecad.extension_type": "semantic/freecad-extension-type",
    "semantic.freecad.module": "semantic/freecad-module",
    "semantic.freecad.native_type": "semantic/freecad-native-type",
    "semantic.freecad.property_type": "semantic/freecad-property-type",
}


def _fail(code: CapabilityCatalogErrorCode, path: str = "") -> None:
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
            _json_tree(key, f"{path}/key", depth + 1, remaining)
            _json_tree(item, f"{path}/field", depth + 1, remaining)
        return
    _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)


def _canonical(value: object, *, maximum: int) -> bytes:
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
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT)
    if not raw or len(raw) > maximum:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED)
    return raw


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if type(key) is not str or key in result:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT)
        result[key] = value
    return result


def _constant(_value: str) -> object:
    _fail(CapabilityCatalogErrorCode.INVALID_INPUT)


def _decode_json(raw: object, *, maximum: int) -> object:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT)
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except CapabilityCatalogError:
        raise
    except (
        json.JSONDecodeError,
        UnicodeError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT)
    _json_tree(value, "", 0, [_MAX_JSON_NODES])
    if _canonical(value, maximum=maximum) != raw:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT)
    return value


def _exact(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _type_mapping(value: FreeCadRegisteredType) -> dict[str, object]:
    return {
        "category": value.category.value,
        "declaring_module": value.declaring_module,
        "native_type_id": value.native_type_id,
        "parent_native_type_id": value.parent_native_type_id,
    }


def _backend_mapping(value: CapabilityBackend) -> dict[str, object]:
    return {
        "backend_id": value.backend_id,
        "backend_version": list(value.backend_version),
        "build_fingerprint_sha256": value.build_fingerprint_sha256,
        "discovery_profile": value.discovery_profile.value,
        "platform_id": value.platform_id,
    }


def _verify_parent_graph(values: tuple[FreeCadRegisteredType, ...]) -> None:
    parents = {item.native_type_id: item.parent_native_type_id for item in values}
    complete: set[str] = set()
    for start in parents:
        if start in complete:
            continue
        trail: set[str] = set()
        cursor: str | None = start
        while cursor is not None and cursor not in complete:
            if cursor in trail:
                _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "registered_types")
            trail.add(cursor)
            cursor = parents[cursor]
        complete.update(trail)


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadDiscoverySnapshotV2:
    """Exact registry snapshot with a larger bounded envelope than v1."""

    schema_version: int
    backend_version: tuple[int, ...]
    build_fingerprint_sha256: str
    platform_id: str
    probe_profile: CapabilityExecutionProfile
    probe_modules: tuple[str, ...]
    registered_types: tuple[FreeCadRegisteredType, ...]
    probe_algorithm_version: str = "2.0"

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != FREECAD_DISCOVERY_V2_SCHEMA_VERSION
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
        if len(self.probe_modules) > MAX_FREECAD_DISCOVERY_V2_MODULES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "probe_modules")
        modules = tuple(_name(item, "probe_modules") for item in self.probe_modules)
        if len(set(modules)) != len(modules):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "probe_modules")
        if type(self.registered_types) is not tuple:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "registered_types")
        if len(self.registered_types) > MAX_FREECAD_DISCOVERY_V2_TYPES:
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
        scope_modules = set(modules) | {item.declaring_module for item in self.registered_types}
        if len(scope_modules) > MAX_FREECAD_DISCOVERY_V2_MODULES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "registered_types")
        if len(scope_modules) + len(self.registered_types) > MAX_FREECAD_DISCOVERY_V2_DESCRIPTORS:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "registered_types")
        _name(self.probe_algorithm_version, "probe_algorithm_version")
        object.__setattr__(self, "probe_modules", tuple(sorted(modules)))
        object.__setattr__(
            self,
            "registered_types",
            tuple(sorted(self.registered_types, key=lambda item: item.native_type_id)),
        )
        _canonical(self._mapping(), maximum=MAX_FREECAD_DISCOVERY_V2_SNAPSHOT_BYTES)

    @property
    def backend(self) -> CapabilityBackend:
        return CapabilityBackend(
            backend_id="freecad",
            backend_version=self.backend_version,
            build_fingerprint_sha256=self.build_fingerprint_sha256,
            platform_id=self.platform_id,
            discovery_profile=self.probe_profile,
        )

    @property
    def scope_modules(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                set(self.probe_modules) | {item.declaring_module for item in self.registered_types}
            )
        )

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
    def snapshot_sha256(self) -> str:
        raw = _canonical(self._mapping(), maximum=MAX_FREECAD_DISCOVERY_V2_SNAPSHOT_BYTES)
        return hashlib.sha256(_SNAPSHOT_DIGEST_DOMAIN + raw).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadCapabilityPageDescriptor:
    page_index: int
    scope_module: str
    segment_id: str
    catalog_id: str
    catalog_sha256: str
    catalog_size_bytes: int
    descriptor_count: int
    external_ref_count: int
    relation_count: int
    first_capability_id: str
    last_capability_id: str

    def __post_init__(self) -> None:
        if (
            type(self.page_index) is not int
            or not 0 <= self.page_index < MAX_FREECAD_DISCOVERY_V2_PAGES
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "page_index")
        _name(self.scope_module, "scope_module")
        _name(self.segment_id, "segment_id")
        _name(self.catalog_id, "catalog_id")
        _digest(self.catalog_sha256, "catalog_sha256")
        if (
            type(self.catalog_size_bytes) is not int
            or not 0 < self.catalog_size_bytes <= MAX_CAPABILITY_CATALOG_BYTES
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "catalog_size_bytes")
        for path, value, maximum in (
            ("descriptor_count", self.descriptor_count, MAX_CAPABILITY_DESCRIPTORS),
            (
                "external_ref_count",
                self.external_ref_count,
                MAX_CAPABILITY_EXTERNAL_REFS,
            ),
            ("relation_count", self.relation_count, MAX_CAPABILITY_RELATIONS),
        ):
            if type(value) is not int or not 0 <= value <= maximum:
                _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
        if self.descriptor_count == 0:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "descriptor_count")
        _name(self.first_capability_id, "first_capability_id")
        _name(self.last_capability_id, "last_capability_id")
        if self.first_capability_id > self.last_capability_id:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "first_capability_id")


def _page_descriptor_mapping(value: FreeCadCapabilityPageDescriptor) -> dict[str, object]:
    return {
        "catalog_id": value.catalog_id,
        "catalog_sha256": value.catalog_sha256,
        "catalog_size_bytes": value.catalog_size_bytes,
        "descriptor_count": value.descriptor_count,
        "external_ref_count": value.external_ref_count,
        "first_capability_id": value.first_capability_id,
        "last_capability_id": value.last_capability_id,
        "page_index": value.page_index,
        "relation_count": value.relation_count,
        "scope_module": value.scope_module,
        "segment_id": value.segment_id,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadCapabilityManifest:
    schema_version: int
    backend: CapabilityBackend
    snapshot_sha256: str
    discovery_algorithm_id: str
    discovery_algorithm_version: str
    module_count: int
    type_count: int
    probe_modules: tuple[str, ...]
    page_descriptor_limit: int
    page_descriptors: tuple[FreeCadCapabilityPageDescriptor, ...]

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != FREECAD_DISCOVERY_V2_SCHEMA_VERSION
        ):
            _fail(CapabilityCatalogErrorCode.UNSUPPORTED_VERSION, "schema_version")
        if type(self.backend) is not CapabilityBackend or self.backend.backend_id != "freecad":
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "backend")
        _digest(self.snapshot_sha256, "snapshot_sha256")
        _name(self.discovery_algorithm_id, "discovery_algorithm_id")
        _name(self.discovery_algorithm_version, "discovery_algorithm_version")
        if (
            self.discovery_algorithm_id != FREECAD_DISCOVERY_V2_ALGORITHM_ID
            or self.discovery_algorithm_version != FREECAD_DISCOVERY_V2_ALGORITHM_VERSION
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "discovery_algorithm_id")
        if (
            type(self.module_count) is not int
            or not 1 <= self.module_count <= MAX_FREECAD_DISCOVERY_V2_MODULES
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "module_count")
        if (
            type(self.type_count) is not int
            or not 0 <= self.type_count <= MAX_FREECAD_DISCOVERY_V2_TYPES
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "type_count")
        if self.module_count + self.type_count > MAX_FREECAD_DISCOVERY_V2_DESCRIPTORS:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "type_count")
        if type(self.probe_modules) is not tuple:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "probe_modules")
        if len(self.probe_modules) > MAX_FREECAD_DISCOVERY_V2_MODULES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "probe_modules")
        probe_modules = tuple(_name(item, "probe_modules") for item in self.probe_modules)
        if len(set(probe_modules)) != len(probe_modules):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "probe_modules")
        probe_modules = tuple(sorted(probe_modules))
        if (
            type(self.page_descriptor_limit) is not int
            or not 1 <= self.page_descriptor_limit <= MAX_CAPABILITY_DESCRIPTORS
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "page_descriptor_limit")
        if type(self.page_descriptors) is not tuple:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "page_descriptors")
        if len(self.page_descriptors) > MAX_FREECAD_DISCOVERY_V2_PAGES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "page_descriptors")
        if not all(type(item) is FreeCadCapabilityPageDescriptor for item in self.page_descriptors):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "page_descriptors")
        pages = tuple(sorted(self.page_descriptors, key=lambda item: item.page_index))
        if tuple(item.page_index for item in pages) != tuple(range(len(pages))):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "page_descriptors")
        scopes = tuple(item.scope_module for item in pages)
        if scopes != tuple(sorted(scopes)):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "page_descriptors")
        paged_modules = set(scopes)
        if len(set(probe_modules) | paged_modules) != self.module_count:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "module_count")
        if any(item.descriptor_count > self.page_descriptor_limit for item in pages):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "page_descriptors")
        if len({item.catalog_sha256 for item in pages}) != len(pages):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "page_descriptors")
        if len({item.segment_id for item in pages}) != len(pages):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "page_descriptors")
        if sum(item.descriptor_count for item in pages) != len(paged_modules) + self.type_count:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "page_descriptors")
        object.__setattr__(self, "probe_modules", probe_modules)
        object.__setattr__(self, "page_descriptors", pages)
        _canonical(self._body_mapping(), maximum=MAX_FREECAD_DISCOVERY_V2_MANIFEST_BYTES)

    def _body_mapping(self) -> dict[str, object]:
        return {
            "backend": _backend_mapping(self.backend),
            "discovery_algorithm_id": self.discovery_algorithm_id,
            "discovery_algorithm_version": self.discovery_algorithm_version,
            "module_count": self.module_count,
            "page_descriptor_limit": self.page_descriptor_limit,
            "page_descriptors": [_page_descriptor_mapping(item) for item in self.page_descriptors],
            "probe_modules": list(self.probe_modules),
            "schema_version": self.schema_version,
            "snapshot_sha256": self.snapshot_sha256,
            "type_count": self.type_count,
        }

    @property
    def manifest_sha256(self) -> str:
        raw = _canonical(self._body_mapping(), maximum=MAX_FREECAD_DISCOVERY_V2_MANIFEST_BYTES)
        return hashlib.sha256(_MANIFEST_DIGEST_DOMAIN + raw).hexdigest()


def encode_freecad_capability_manifest(value: object) -> bytes:
    if type(value) is not FreeCadCapabilityManifest:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT)
    envelope = value._body_mapping()
    envelope["manifest_sha256"] = value.manifest_sha256
    return _canonical(envelope, maximum=MAX_FREECAD_DISCOVERY_V2_MANIFEST_BYTES)


def _page_descriptor_from(value: object, path: str) -> FreeCadCapabilityPageDescriptor:
    item = _exact(
        value,
        {
            "catalog_id",
            "catalog_sha256",
            "catalog_size_bytes",
            "descriptor_count",
            "external_ref_count",
            "first_capability_id",
            "last_capability_id",
            "page_index",
            "relation_count",
            "scope_module",
            "segment_id",
        },
        path,
    )
    return FreeCadCapabilityPageDescriptor(**item)


def decode_freecad_capability_manifest(raw: object) -> FreeCadCapabilityManifest:
    try:
        return _decode_freecad_capability_manifest(raw)
    except CapabilityCatalogError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT)


def _decode_freecad_capability_manifest(raw: object) -> FreeCadCapabilityManifest:
    value = _decode_json(raw, maximum=MAX_FREECAD_DISCOVERY_V2_MANIFEST_BYTES)
    item = _exact(
        value,
        {
            "backend",
            "discovery_algorithm_id",
            "discovery_algorithm_version",
            "manifest_sha256",
            "module_count",
            "page_descriptor_limit",
            "page_descriptors",
            "probe_modules",
            "schema_version",
            "snapshot_sha256",
            "type_count",
        },
        "",
    )
    backend = _exact(
        item["backend"],
        {
            "backend_id",
            "backend_version",
            "build_fingerprint_sha256",
            "discovery_profile",
            "platform_id",
        },
        "backend",
    )
    try:
        parsed_backend = CapabilityBackend(
            backend_id=backend["backend_id"],
            backend_version=tuple(backend["backend_version"])
            if type(backend["backend_version"]) is list
            else backend["backend_version"],
            build_fingerprint_sha256=backend["build_fingerprint_sha256"],
            discovery_profile=CapabilityExecutionProfile(backend["discovery_profile"]),
            platform_id=backend["platform_id"],
        )
    except (TypeError, ValueError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "backend")
    raw_pages = item["page_descriptors"]
    if type(raw_pages) is not list:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "page_descriptors")
    raw_probe_modules = item["probe_modules"]
    if type(raw_probe_modules) is not list:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "probe_modules")
    manifest = FreeCadCapabilityManifest(
        schema_version=item["schema_version"],
        backend=parsed_backend,
        snapshot_sha256=item["snapshot_sha256"],
        discovery_algorithm_id=item["discovery_algorithm_id"],
        discovery_algorithm_version=item["discovery_algorithm_version"],
        module_count=item["module_count"],
        type_count=item["type_count"],
        probe_modules=tuple(raw_probe_modules),
        page_descriptor_limit=item["page_descriptor_limit"],
        page_descriptors=tuple(
            _page_descriptor_from(page, f"page_descriptors/{index}")
            for index, page in enumerate(raw_pages)
        ),
    )
    supplied_digest = _digest(item["manifest_sha256"], "manifest_sha256")
    if not hmac.compare_digest(supplied_digest, manifest.manifest_sha256):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "manifest_sha256")
    if encode_freecad_capability_manifest(manifest) != raw:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT)
    return manifest


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


def _module_descriptor(native_module: str) -> CapabilityDescriptor:
    capability_id = freecad_module_capability_id(native_module)
    return CapabilityDescriptor(
        capability_id=capability_id,
        kind=CapabilityKind.MODULE,
        native_identifier=native_module,
        declaring_module_id=capability_id,
        status=CapabilitySupportStatus.DISCOVERED,
        risk_class=CapabilityRiskClass.UNKNOWN,
        semantic_term_ref_ids=("semantic.freecad.module",),
    )


def _type_descriptor(value: FreeCadRegisteredType) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=freecad_type_capability_id(value.native_type_id),
        kind=_kind(value.category),
        native_identifier=value.native_type_id,
        declaring_module_id=freecad_module_capability_id(value.declaring_module),
        status=CapabilitySupportStatus.DISCOVERED,
        risk_class=CapabilityRiskClass.UNKNOWN,
        semantic_term_ref_ids=(_semantic_term(value.category),),
    )


def _relation(value: FreeCadRegisteredType) -> CapabilityRelation | None:
    parent = value.parent_native_type_id
    if parent is None:
        return None
    digest = hashlib.sha256(
        _RELATION_ID_DOMAIN + value.native_type_id.encode("utf-8") + b"\0" + parent.encode("utf-8")
    ).hexdigest()
    return CapabilityRelation(
        relation_id=f"freecad.relation.inherits.{digest[:32]}",
        relation_term_ref_id="relation.native.inherits",
        source_capability_id=freecad_type_capability_id(value.native_type_id),
        target_capability_ids=(freecad_type_capability_id(parent),),
    )


def _page_receipt(
    *,
    snapshot: FreeCadDiscoverySnapshotV2,
    scope_module: str,
    descriptors: tuple[CapabilityDescriptor, ...],
    native_by_capability: dict[str, FreeCadRegisteredType],
) -> str:
    native_sources = tuple(
        native_by_capability[item.capability_id]
        for item in descriptors
        if item.capability_id in native_by_capability
    )
    body = {
        "algorithm_id": FREECAD_DISCOVERY_V2_ALGORITHM_ID,
        "algorithm_version": FREECAD_DISCOVERY_V2_ALGORITHM_VERSION,
        "backend": _backend_mapping(snapshot.backend),
        "module_was_probed": scope_module in snapshot.probe_modules,
        "owned_capability_ids": sorted(item.capability_id for item in descriptors),
        "owned_registered_types": [
            _type_mapping(item)
            for item in sorted(native_sources, key=lambda item: item.native_type_id)
        ],
        "probe_algorithm_version": snapshot.probe_algorithm_version,
        "scope_module": scope_module,
    }
    raw = _canonical(body, maximum=MAX_CAPABILITY_CATALOG_BYTES)
    return hashlib.sha256(_PAGE_RECEIPT_DOMAIN + raw).hexdigest()


def _build_page(
    *,
    snapshot: FreeCadDiscoverySnapshotV2,
    scope_module: str,
    owned_descriptors: tuple[CapabilityDescriptor, ...],
    native_by_capability: dict[str, FreeCadRegisteredType],
    descriptors_by_id: dict[str, CapabilityDescriptor],
) -> CapabilityCatalogSegment:
    local_ids = {item.capability_id for item in owned_descriptors}
    external_ids: set[str] = set()
    relations: list[CapabilityRelation] = []
    for descriptor in owned_descriptors:
        if descriptor.declaring_module_id not in local_ids:
            external_ids.add(descriptor.declaring_module_id)
        native_type = native_by_capability.get(descriptor.capability_id)
        if native_type is None:
            continue
        parent = native_type.parent_native_type_id
        if parent is not None:
            parent_id = freecad_type_capability_id(parent)
            if parent_id not in local_ids:
                external_ids.add(parent_id)
        relation = _relation(native_type)
        if relation is not None:
            relations.append(relation)
    external_refs = tuple(
        ExternalCapabilityRef(
            capability_id=capability_id,
            descriptor_sha256=descriptors_by_id[capability_id].descriptor_sha256,
        )
        for capability_id in sorted(external_ids)
    )
    receipt = _page_receipt(
        snapshot=snapshot,
        scope_module=scope_module,
        descriptors=owned_descriptors,
        native_by_capability=native_by_capability,
    )
    return CapabilityCatalogSegment(
        schema_version=CAPABILITY_CATALOG_SCHEMA_VERSION,
        segment_id=f"freecad.types.v2.{receipt[:32]}",
        backend=snapshot.backend,
        discovery_receipt_sha256=receipt,
        discovery_algorithm_id=FREECAD_DISCOVERY_V2_ALGORITHM_ID,
        discovery_algorithm_version=FREECAD_DISCOVERY_V2_ALGORITHM_VERSION,
        terms=_terms(),
        descriptors=owned_descriptors,
        external_refs=external_refs,
        relations=tuple(relations),
    )


def _page_descriptor(
    *, page_index: int, scope_module: str, page: CapabilityCatalogSegment
) -> FreeCadCapabilityPageDescriptor:
    encoded = encode_capability_catalog(page)
    capability_ids = tuple(item.capability_id for item in page.descriptors)
    return FreeCadCapabilityPageDescriptor(
        page_index=page_index,
        scope_module=scope_module,
        segment_id=page.segment_id,
        catalog_id=page.catalog_id,
        catalog_sha256=page.catalog_sha256,
        catalog_size_bytes=len(encoded),
        descriptor_count=len(page.descriptors),
        external_ref_count=len(page.external_refs),
        relation_count=len(page.relations),
        first_capability_id=min(capability_ids),
        last_capability_id=max(capability_ids),
    )


def _project_pages(
    snapshot: FreeCadDiscoverySnapshotV2,
    *,
    max_descriptors_per_page: int,
) -> tuple[tuple[str, CapabilityCatalogSegment], ...]:
    paged_modules = tuple(sorted({item.declaring_module for item in snapshot.registered_types}))
    module_descriptors = {module: _module_descriptor(module) for module in paged_modules}
    type_descriptors = {
        item.native_type_id: _type_descriptor(item) for item in snapshot.registered_types
    }
    descriptors_by_id = {
        item.capability_id: item
        for item in tuple(module_descriptors.values()) + tuple(type_descriptors.values())
    }
    native_by_capability = {
        type_descriptors[item.native_type_id].capability_id: item
        for item in snapshot.registered_types
    }
    scoped_pages: list[tuple[str, CapabilityCatalogSegment]] = []
    for module in paged_modules:
        module_types = tuple(
            sorted(
                (item for item in snapshot.registered_types if item.declaring_module == module),
                key=lambda item: item.native_type_id,
            )
        )
        owned = (module_descriptors[module],) + tuple(
            type_descriptors[item.native_type_id] for item in module_types
        )
        offset = 0
        while offset < len(owned):
            count = min(max_descriptors_per_page, len(owned) - offset)
            page: CapabilityCatalogSegment | None = None
            while count > 0:
                try:
                    candidate = _build_page(
                        snapshot=snapshot,
                        scope_module=module,
                        owned_descriptors=owned[offset : offset + count],
                        native_by_capability=native_by_capability,
                        descriptors_by_id=descriptors_by_id,
                    )
                    encode_capability_catalog(candidate)
                except CapabilityCatalogError as error:
                    if error.code is not CapabilityCatalogErrorCode.BUDGET_EXCEEDED:
                        raise
                    count -= 1
                    continue
                page = candidate
                break
            if page is None:
                _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "pages")
            scoped_pages.append((module, page))
            if len(scoped_pages) > MAX_FREECAD_DISCOVERY_V2_PAGES:
                _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "pages")
            offset += count
    return tuple(scoped_pages)


def validate_freecad_capability_page_set(
    manifest: object,
    pages: object,
) -> tuple[CapabilityCatalogSegment, ...]:
    """Validate one ordered, complete page set against its trusted manifest.

    A manifest digest is expected to be authenticated by the caller.  This
    function proves that every supplied canonical page matches the manifest,
    that no page is missing, substituted, reordered, or duplicated, and that
    all cross-page references close in the aggregate index.  Probe-only empty
    modules remain bound by ``manifest.probe_modules`` without consuming a
    catalog page.
    """

    if type(manifest) is not FreeCadCapabilityManifest:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "manifest")
    if type(pages) is not tuple:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "pages")
    if len(pages) > MAX_FREECAD_DISCOVERY_V2_PAGES:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "pages")
    if not all(type(item) is CapabilityCatalogSegment for item in pages):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "pages")
    if len(pages) != len(manifest.page_descriptors):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "pages")

    expected_terms = _terms()
    module_descriptor_ids: set[str] = set()
    type_descriptor_count = 0
    type_descriptors_by_id: dict[str, CapabilityDescriptor] = {}
    parent_by_source: dict[str, str] = {}
    relation_id_by_source: dict[str, str] = {}
    for page_index, (metadata, page) in enumerate(
        zip(manifest.page_descriptors, pages, strict=True)
    ):
        expected_metadata = _page_descriptor(
            page_index=page_index,
            scope_module=metadata.scope_module,
            page=page,
        )
        if metadata != expected_metadata:
            _fail(
                CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                f"pages/{page_index}",
            )
        if (
            page.backend != manifest.backend
            or page.discovery_algorithm_id != FREECAD_DISCOVERY_V2_ALGORITHM_ID
            or page.discovery_algorithm_version != FREECAD_DISCOVERY_V2_ALGORITHM_VERSION
            or page.terms != expected_terms
        ):
            _fail(
                CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                f"pages/{page_index}",
            )
        expected_module_id = freecad_module_capability_id(metadata.scope_module)
        local_descriptor_ids = {item.capability_id for item in page.descriptors}
        expected_external_ids: set[str] = set()
        for descriptor in page.descriptors:
            if (
                descriptor.declaring_module_id != expected_module_id
                or descriptor.status is not CapabilitySupportStatus.DISCOVERED
                or descriptor.risk_class is not CapabilityRiskClass.UNKNOWN
            ):
                _fail(
                    CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                    f"pages/{page_index}/descriptors",
                )
            if descriptor.kind is CapabilityKind.MODULE:
                if (
                    descriptor != _module_descriptor(metadata.scope_module)
                    or descriptor.capability_id in module_descriptor_ids
                ):
                    _fail(
                        CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                        f"pages/{page_index}/descriptors",
                    )
                module_descriptor_ids.add(descriptor.capability_id)
            elif descriptor.kind not in {
                CapabilityKind.NATIVE_TYPE,
                CapabilityKind.DOCUMENT_OBJECT,
                CapabilityKind.PROPERTY_TYPE,
                CapabilityKind.EXTENSION_TYPE,
            }:
                _fail(
                    CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                    f"pages/{page_index}/descriptors",
                )
            else:
                expected_semantic_term = {
                    CapabilityKind.NATIVE_TYPE: "semantic.freecad.native_type",
                    CapabilityKind.DOCUMENT_OBJECT: "semantic.freecad.document_object",
                    CapabilityKind.PROPERTY_TYPE: "semantic.freecad.property_type",
                    CapabilityKind.EXTENSION_TYPE: "semantic.freecad.extension_type",
                }[descriptor.kind]
                if (
                    descriptor.capability_id
                    != freecad_type_capability_id(descriptor.native_identifier)
                    or descriptor.semantic_term_ref_ids != (expected_semantic_term,)
                    or descriptor.facts
                    or descriptor.execution_profiles
                    or descriptor.lifecycle_stages
                    or descriptor.dependency_ids
                    or descriptor.verification is not None
                ):
                    _fail(
                        CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                        f"pages/{page_index}/descriptors",
                    )
                type_descriptors_by_id[descriptor.capability_id] = descriptor
                type_descriptor_count += 1
            if descriptor.declaring_module_id not in local_descriptor_ids:
                expected_external_ids.add(descriptor.declaring_module_id)
        for relation in page.relations:
            if (
                relation.relation_term_ref_id != "relation.native.inherits"
                or len(relation.target_capability_ids) != 1
                or relation.source_capability_id not in local_descriptor_ids
                or relation.source_capability_id in parent_by_source
                or relation.facts
            ):
                _fail(
                    CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                    f"pages/{page_index}/relations",
                )
            parent_by_source[relation.source_capability_id] = relation.target_capability_ids[0]
            relation_id_by_source[relation.source_capability_id] = relation.relation_id
            expected_external_ids.update(
                target_id
                for target_id in relation.target_capability_ids
                if target_id not in local_descriptor_ids
            )
        if {item.capability_id for item in page.external_refs} != expected_external_ids:
            _fail(
                CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                f"pages/{page_index}/external_refs",
            )

    paged_modules = {item.scope_module for item in manifest.page_descriptors}
    if (
        len(module_descriptor_ids) != len(paged_modules)
        or type_descriptor_count != manifest.type_count
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "pages/descriptors")
    if pages:
        index = CapabilityCatalogIndex(pages)
        if len(index.descriptors) != len(module_descriptor_ids) + type_descriptor_count:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "pages/descriptors")
        for source_id, target_id in parent_by_source.items():
            source = type_descriptors_by_id.get(source_id)
            target = type_descriptors_by_id.get(target_id)
            if source is None or target is None or source_id == target_id:
                _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "pages/relations")
            expected_relation = _relation(
                FreeCadRegisteredType(
                    native_type_id=source.native_identifier,
                    declaring_module="Validator",
                    parent_native_type_id=target.native_identifier,
                    category=FreeCadNativeTypeCategory.NATIVE_TYPE,
                )
            )
            if (
                expected_relation is None
                or expected_relation.relation_id != relation_id_by_source[source_id]
            ):
                _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "pages/relations")
        trail_complete: set[str] = set()
        for start in parent_by_source:
            trail: set[str] = set()
            cursor = start
            while cursor in parent_by_source and cursor not in trail_complete:
                if cursor in trail:
                    _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "pages/relations")
                trail.add(cursor)
                cursor = parent_by_source[cursor]
            trail_complete.update(trail)
    return pages


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadPagedCapabilityCatalog:
    snapshot: FreeCadDiscoverySnapshotV2
    manifest: FreeCadCapabilityManifest
    pages: tuple[CapabilityCatalogSegment, ...]

    def __post_init__(self) -> None:
        if type(self.snapshot) is not FreeCadDiscoverySnapshotV2:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "snapshot")
        if type(self.manifest) is not FreeCadCapabilityManifest:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "manifest")
        if type(self.pages) is not tuple:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "pages")
        if len(self.pages) > MAX_FREECAD_DISCOVERY_V2_PAGES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "pages")
        if not all(type(item) is CapabilityCatalogSegment for item in self.pages):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "pages")
        validate_freecad_capability_page_set(self.manifest, self.pages)
        expected_scoped_pages = _project_pages(
            self.snapshot,
            max_descriptors_per_page=self.manifest.page_descriptor_limit,
        )
        expected_pages = tuple(page for _scope, page in expected_scoped_pages)
        if self.pages != expected_pages:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "pages")
        index = CapabilityCatalogIndex(self.pages) if self.pages else None
        declaring_modules = {item.declaring_module for item in self.snapshot.registered_types}
        expected_capability_ids = {
            *(freecad_module_capability_id(module) for module in declaring_modules),
            *(
                freecad_type_capability_id(item.native_type_id)
                for item in self.snapshot.registered_types
            ),
        }
        actual_capability_ids = set() if index is None else set(index.descriptors)
        if actual_capability_ids != expected_capability_ids:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "pages")
        actual_parent_edges = {
            (relation.source_capability_id, relation.target_capability_ids)
            for page in self.pages
            for relation in page.relations
            if relation.relation_term_ref_id == "relation.native.inherits"
        }
        expected_parent_edges = {
            (
                freecad_type_capability_id(item.native_type_id),
                (freecad_type_capability_id(item.parent_native_type_id),),
            )
            for item in self.snapshot.registered_types
            if item.parent_native_type_id is not None
        }
        if actual_parent_edges != expected_parent_edges:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "pages/relations")
        expected_page_descriptors = tuple(
            _page_descriptor(page_index=index, scope_module=scope, page=page)
            for index, (scope, page) in enumerate(expected_scoped_pages)
        )
        expected_manifest = FreeCadCapabilityManifest(
            schema_version=FREECAD_DISCOVERY_V2_SCHEMA_VERSION,
            backend=self.snapshot.backend,
            snapshot_sha256=self.snapshot.snapshot_sha256,
            discovery_algorithm_id=FREECAD_DISCOVERY_V2_ALGORITHM_ID,
            discovery_algorithm_version=FREECAD_DISCOVERY_V2_ALGORITHM_VERSION,
            module_count=len(self.snapshot.scope_modules),
            type_count=len(self.snapshot.registered_types),
            probe_modules=self.snapshot.probe_modules,
            page_descriptor_limit=self.manifest.page_descriptor_limit,
            page_descriptors=expected_page_descriptors,
        )
        if self.manifest != expected_manifest:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "manifest")


def build_paged_freecad_type_catalog(
    snapshot: FreeCadDiscoverySnapshotV2,
    *,
    max_descriptors_per_page: int = DEFAULT_FREECAD_DISCOVERY_V2_PAGE_DESCRIPTORS,
) -> FreeCadPagedCapabilityCatalog:
    """Build deterministic module-scoped pages under the v1 segment budget."""

    if type(snapshot) is not FreeCadDiscoverySnapshotV2:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "snapshot")
    if (
        type(max_descriptors_per_page) is not int
        or not 1 <= max_descriptors_per_page <= MAX_CAPABILITY_DESCRIPTORS
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "max_descriptors_per_page")
    if not snapshot.scope_modules:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "snapshot")
    scoped_pages = _project_pages(snapshot, max_descriptors_per_page=max_descriptors_per_page)
    pages = tuple(page for _scope, page in scoped_pages)
    page_descriptors = tuple(
        _page_descriptor(page_index=index, scope_module=scope, page=page)
        for index, (scope, page) in enumerate(scoped_pages)
    )
    manifest = FreeCadCapabilityManifest(
        schema_version=FREECAD_DISCOVERY_V2_SCHEMA_VERSION,
        backend=snapshot.backend,
        snapshot_sha256=snapshot.snapshot_sha256,
        discovery_algorithm_id=FREECAD_DISCOVERY_V2_ALGORITHM_ID,
        discovery_algorithm_version=FREECAD_DISCOVERY_V2_ALGORITHM_VERSION,
        module_count=len(snapshot.scope_modules),
        type_count=len(snapshot.registered_types),
        probe_modules=snapshot.probe_modules,
        page_descriptor_limit=max_descriptors_per_page,
        page_descriptors=page_descriptors,
    )
    return FreeCadPagedCapabilityCatalog(
        snapshot=snapshot,
        manifest=manifest,
        pages=pages,
    )


__all__ = ()
