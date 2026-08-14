"""Internal complete-index projection and promotion packs for FreeCAD v2.

Discovery is inventory, not authority.  This module classifies every TypeId in
one validated paged snapshot and overlays only explicitly supplied,
content-addressed promotion packs.  A formal executable capability that names a
TypeId is recorded as a relation; it never promotes the native TypeId by
itself.  The result is an internal manifest for semantic lanes, not a public
MCP surface and not an execution registry.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from enum import StrEnum

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
    encode_capability_catalog,
)
from vibecad.execution.capability_index import CapabilityCatalogIndex
from vibecad.execution.freecad_capabilities import (
    FreeCadNativeTypeCategory,
    FreeCadRegisteredType,
    freecad_type_capability_id,
)
from vibecad.execution.freecad_discovery_v2 import (
    FreeCadPagedCapabilityCatalog,
    validate_freecad_capability_page_set,
)

FREECAD_CAPABILITY_PROJECTION_V2_SCHEMA_VERSION = 1
FREECAD_CAPABILITY_PROMOTION_PACK_SCHEMA_VERSION = 1
MAX_FREECAD_CAPABILITY_PROMOTION_ENTRIES = 512
MAX_FREECAD_CAPABILITY_PROMOTION_PACKS = 64
MAX_FREECAD_CAPABILITY_FORMAL_CATALOGS = 64
MAX_FREECAD_CAPABILITY_FORMAL_BINDINGS = 1_024
MAX_FREECAD_CAPABILITY_PROMOTION_PACK_BYTES = 2 * 1024 * 1024
MAX_FREECAD_CAPABILITY_PROJECTION_BYTES = 8 * 1024 * 1024

_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_IDENTIFIER_BYTES = 192
_MAX_VERSION_BYTES = 64
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,191}$")
_PACK_DIGEST_DOMAIN = b"vibecad-freecad-capability-promotion-pack-v1\0"
_PROJECTION_DIGEST_DOMAIN = b"vibecad-freecad-capability-projection-v2\0"
_FORMAL_BINDING_DIGEST_DOMAIN = b"vibecad-freecad-formal-type-binding-v1\0"
_VERIFICATION_BINDING_DIGEST_DOMAIN = b"vibecad-freecad-promotion-verification-v1\0"


def _fail(code: CapabilityCatalogErrorCode, path: str) -> None:
    raise CapabilityCatalogError(code, path)


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _identifier(value: object, path: str) -> str:
    if type(value) is not str:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if (
        not value
        or size > _MAX_IDENTIFIER_BYTES
        or _IDENTIFIER.fullmatch(value) is None
        or ".." in value
        or "//" in value
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _version(value: object, path: str) -> str:
    if type(value) is not str:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if not value or size > _MAX_VERSION_BYTES or not value.isprintable():
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _canonical(value: object, *, maximum: int = MAX_FREECAD_CAPABILITY_PROJECTION_BYTES) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "canonical")
    if not raw or len(raw) > maximum:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "canonical")
    return raw


class FreeCadCapabilitySemanticKind(StrEnum):
    DOCUMENT_OBJECT = "document_object"
    PROPERTY_TYPE = "property_type"
    EXTENSION_TYPE = "extension_type"
    NATIVE_TYPE = "native_type"


_SEMANTIC_KIND = {
    FreeCadNativeTypeCategory.DOCUMENT_OBJECT: FreeCadCapabilitySemanticKind.DOCUMENT_OBJECT,
    FreeCadNativeTypeCategory.PROPERTY_TYPE: FreeCadCapabilitySemanticKind.PROPERTY_TYPE,
    FreeCadNativeTypeCategory.EXTENSION_TYPE: FreeCadCapabilitySemanticKind.EXTENSION_TYPE,
    FreeCadNativeTypeCategory.NATIVE_TYPE: FreeCadCapabilitySemanticKind.NATIVE_TYPE,
}


_CAPABILITY_KIND = {
    FreeCadCapabilitySemanticKind.DOCUMENT_OBJECT: CapabilityKind.DOCUMENT_OBJECT,
    FreeCadCapabilitySemanticKind.PROPERTY_TYPE: CapabilityKind.PROPERTY_TYPE,
    FreeCadCapabilitySemanticKind.EXTENSION_TYPE: CapabilityKind.EXTENSION_TYPE,
    FreeCadCapabilitySemanticKind.NATIVE_TYPE: CapabilityKind.NATIVE_TYPE,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadPromotionVerificationBinding:
    """Exact proof inputs required before one TypeId can become verified."""

    runtime_build_sha256: str
    adapter_contract_sha256: str
    test_contract_sha256: str
    test_receipt_sha256: str
    test_receipt_size_bytes: int
    verifier_id: str
    verifier_version: str

    def __post_init__(self) -> None:
        for path, value in (
            ("runtime_build_sha256", self.runtime_build_sha256),
            ("adapter_contract_sha256", self.adapter_contract_sha256),
            ("test_contract_sha256", self.test_contract_sha256),
            ("test_receipt_sha256", self.test_receipt_sha256),
        ):
            _digest(value, path)
        if (
            type(self.test_receipt_size_bytes) is not int
            or not 0 < self.test_receipt_size_bytes <= _MAX_SAFE_INTEGER
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "test_receipt_size_bytes")
        _identifier(self.verifier_id, "verifier_id")
        _version(self.verifier_version, "verifier_version")

    def _mapping(self) -> dict[str, object]:
        return {
            "adapter_contract_sha256": self.adapter_contract_sha256,
            "runtime_build_sha256": self.runtime_build_sha256,
            "test_contract_sha256": self.test_contract_sha256,
            "test_receipt_sha256": self.test_receipt_sha256,
            "test_receipt_size_bytes": self.test_receipt_size_bytes,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadCapabilityPromotionEntry:
    native_type_id: str
    semantic_kind: FreeCadCapabilitySemanticKind
    target_status: CapabilitySupportStatus
    risk_class: CapabilityRiskClass
    semantic_term_ref_ids: tuple[str, ...]
    facts: tuple[CapabilityFact, ...] = ()
    execution_profiles: tuple[CapabilityExecutionProfile, ...] = ()
    lifecycle_stages: tuple[CapabilityLifecycleStage, ...] = ()
    dependency_ids: tuple[str, ...] = ()
    verification: FreeCadPromotionVerificationBinding | None = None

    def __post_init__(self) -> None:
        _identifier(self.native_type_id, "native_type_id")
        if type(self.semantic_kind) is not FreeCadCapabilitySemanticKind:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "semantic_kind")
        if type(self.target_status) is not CapabilitySupportStatus:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "target_status")
        if self.target_status is CapabilitySupportStatus.DISCOVERED:
            _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "target_status")
        if type(self.risk_class) is not CapabilityRiskClass:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "risk_class")
        if type(self.semantic_term_ref_ids) is not tuple or not self.semantic_term_ref_ids:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "semantic_term_ref_ids")
        terms = tuple(
            _identifier(item, "semantic_term_ref_ids") for item in self.semantic_term_ref_ids
        )
        if len(terms) > 16 or len(set(terms)) != len(terms):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "semantic_term_ref_ids")
        if type(self.facts) is not tuple or not all(
            type(item) is CapabilityFact for item in self.facts
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "facts")
        if len(self.facts) > 64 or len({item.key_term_ref_id for item in self.facts}) != len(
            self.facts
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "facts")
        if type(self.execution_profiles) is not tuple or not all(
            type(item) is CapabilityExecutionProfile for item in self.execution_profiles
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "execution_profiles")
        if type(self.lifecycle_stages) is not tuple or not all(
            type(item) is CapabilityLifecycleStage for item in self.lifecycle_stages
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "lifecycle_stages")
        if len(set(self.execution_profiles)) != len(self.execution_profiles) or len(
            set(self.lifecycle_stages)
        ) != len(self.lifecycle_stages):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "target_status")
        if type(self.dependency_ids) is not tuple:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "dependency_ids")
        dependencies = tuple(_identifier(item, "dependency_ids") for item in self.dependency_ids)
        if len(dependencies) > 32 or len(set(dependencies)) != len(dependencies):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "dependency_ids")
        executable = self.target_status.rank >= CapabilitySupportStatus.EXECUTABLE.rank
        if executable != bool(self.execution_profiles) or executable != bool(self.lifecycle_stages):
            _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "target_status")
        if self.target_status is CapabilitySupportStatus.VERIFIED:
            if type(self.verification) is not FreeCadPromotionVerificationBinding:
                _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "verification")
        elif self.verification is not None:
            _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "verification")
        object.__setattr__(self, "semantic_term_ref_ids", tuple(sorted(terms)))
        object.__setattr__(
            self,
            "facts",
            tuple(sorted(self.facts, key=lambda item: item.key_term_ref_id)),
        )
        object.__setattr__(
            self,
            "execution_profiles",
            tuple(sorted(self.execution_profiles, key=str)),
        )
        object.__setattr__(
            self,
            "lifecycle_stages",
            tuple(sorted(self.lifecycle_stages, key=str)),
        )
        object.__setattr__(self, "dependency_ids", tuple(sorted(dependencies)))

    def _mapping(self) -> dict[str, object]:
        return {
            "execution_profiles": [item.value for item in self.execution_profiles],
            "facts": [_fact_mapping(item) for item in self.facts],
            "lifecycle_stages": [item.value for item in self.lifecycle_stages],
            "native_type_id": self.native_type_id,
            "risk_class": self.risk_class.value,
            "semantic_kind": self.semantic_kind.value,
            "semantic_term_ref_ids": list(self.semantic_term_ref_ids),
            "target_status": self.target_status.value,
            "dependency_ids": list(self.dependency_ids),
            "verification": None if self.verification is None else self.verification._mapping(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadCapabilityPromotionPack:
    schema_version: int
    pack_id: str
    lane_id: str
    adapter_id: str
    adapter_version: str
    adapter_contract_sha256: str
    discovery_snapshot_sha256: str
    discovery_manifest_sha256: str
    backend: CapabilityBackend
    terms: tuple[CapabilityTermRef, ...]
    entries: tuple[FreeCadCapabilityPromotionEntry, ...]
    external_refs: tuple[ExternalCapabilityRef, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != FREECAD_CAPABILITY_PROMOTION_PACK_SCHEMA_VERSION
        ):
            _fail(CapabilityCatalogErrorCode.UNSUPPORTED_VERSION, "schema_version")
        _identifier(self.pack_id, "pack_id")
        _identifier(self.lane_id, "lane_id")
        _identifier(self.adapter_id, "adapter_id")
        _version(self.adapter_version, "adapter_version")
        _digest(self.adapter_contract_sha256, "adapter_contract_sha256")
        _digest(self.discovery_snapshot_sha256, "discovery_snapshot_sha256")
        _digest(self.discovery_manifest_sha256, "discovery_manifest_sha256")
        if type(self.backend) is not CapabilityBackend:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "backend")
        if type(self.terms) is not tuple or not all(
            type(item) is CapabilityTermRef for item in self.terms
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "terms")
        if len(self.terms) > 256:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "terms")
        term_ids = tuple(item.term_ref_id for item in self.terms)
        if len(set(term_ids)) != len(term_ids):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "terms")
        if type(self.entries) is not tuple or not self.entries:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "entries")
        if len(self.entries) > MAX_FREECAD_CAPABILITY_PROMOTION_ENTRIES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "entries")
        if not all(type(item) is FreeCadCapabilityPromotionEntry for item in self.entries):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "entries")
        native_ids = tuple(item.native_type_id for item in self.entries)
        if len(set(native_ids)) != len(native_ids):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "entries")
        if type(self.external_refs) is not tuple or not all(
            type(item) is ExternalCapabilityRef for item in self.external_refs
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "external_refs")
        if len(self.external_refs) > 512:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "external_refs")
        external_ids = tuple(item.capability_id for item in self.external_refs)
        if len(set(external_ids)) != len(external_ids):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "external_refs")
        local_ids = {freecad_type_capability_id(item) for item in native_ids}
        dependency_ids = {
            dependency_id for item in self.entries for dependency_id in item.dependency_ids
        }
        if (
            local_ids & set(external_ids)
            or not dependency_ids <= local_ids | set(external_ids)
            or dependency_ids - local_ids != set(external_ids)
        ):
            _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "entries/dependency_ids")
        if any(
            item.verification is not None
            and (
                item.verification.runtime_build_sha256 != self.backend.build_fingerprint_sha256
                or item.verification.adapter_contract_sha256 != self.adapter_contract_sha256
            )
            for item in self.entries
        ):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "entries/verification")
        referenced_terms = {
            term_ref_id for item in self.entries for term_ref_id in item.semantic_term_ref_ids
        }
        referenced_terms.update(
            ref
            for entry in self.entries
            for fact in entry.facts
            for ref in (
                fact.key_term_ref_id,
                fact.value_term_ref_id,
                fact.unit_term_ref_id,
            )
            if ref is not None
        )
        if not referenced_terms <= set(term_ids):
            _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "entries/semantic_term_ref_ids")
        object.__setattr__(
            self,
            "terms",
            tuple(sorted(self.terms, key=lambda item: item.term_ref_id)),
        )
        object.__setattr__(
            self,
            "entries",
            tuple(sorted(self.entries, key=lambda item: item.native_type_id)),
        )
        object.__setattr__(
            self,
            "external_refs",
            tuple(sorted(self.external_refs, key=lambda item: item.capability_id)),
        )
        _canonical(
            self._mapping(),
            maximum=MAX_FREECAD_CAPABILITY_PROMOTION_PACK_BYTES,
        )

    def _mapping(self) -> dict[str, object]:
        return {
            "adapter_contract_sha256": self.adapter_contract_sha256,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "backend": _backend_mapping(self.backend),
            "discovery_manifest_sha256": self.discovery_manifest_sha256,
            "discovery_snapshot_sha256": self.discovery_snapshot_sha256,
            "entries": [item._mapping() for item in self.entries],
            "external_refs": [_external_ref_mapping(item) for item in self.external_refs],
            "lane_id": self.lane_id,
            "pack_id": self.pack_id,
            "schema_version": self.schema_version,
            "terms": [_term_mapping(item) for item in self.terms],
        }

    @property
    def pack_sha256(self) -> str:
        return hashlib.sha256(
            _PACK_DIGEST_DOMAIN
            + _canonical(
                self._mapping(),
                maximum=MAX_FREECAD_CAPABILITY_PROMOTION_PACK_BYTES,
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadCapabilityLayerRef:
    """One explicit support layer for a native TypeId.

    Missing intermediate layers stay missing; the projection never invents
    representability or verification evidence merely because a later status
    exists.
    """

    status: CapabilitySupportStatus
    descriptor_sha256: str
    catalog_sha256: str
    promotion_pack_sha256: str | None

    def __post_init__(self) -> None:
        if type(self.status) is not CapabilitySupportStatus:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "status")
        _digest(self.descriptor_sha256, "descriptor_sha256")
        _digest(self.catalog_sha256, "catalog_sha256")
        if self.status is CapabilitySupportStatus.DISCOVERED:
            if self.promotion_pack_sha256 is not None:
                _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "promotion_pack_sha256")
        else:
            _digest(self.promotion_pack_sha256, "promotion_pack_sha256")

    def _mapping(self) -> dict[str, object]:
        return {
            "catalog_sha256": self.catalog_sha256,
            "descriptor_sha256": self.descriptor_sha256,
            "promotion_pack_sha256": self.promotion_pack_sha256,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadCapabilityIndexEntry:
    native_type_id: str
    capability_id: str
    declaring_module: str
    semantic_kind: FreeCadCapabilitySemanticKind
    parent_native_type_id: str | None
    inheritance_family_native_type_id: str
    layers: tuple[FreeCadCapabilityLayerRef, ...]

    def __post_init__(self) -> None:
        _identifier(self.native_type_id, "native_type_id")
        _identifier(self.capability_id, "capability_id")
        if self.capability_id != freecad_type_capability_id(self.native_type_id):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "capability_id")
        _identifier(self.declaring_module, "declaring_module")
        if type(self.semantic_kind) is not FreeCadCapabilitySemanticKind:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "semantic_kind")
        if self.parent_native_type_id is not None:
            _identifier(self.parent_native_type_id, "parent_native_type_id")
        _identifier(
            self.inheritance_family_native_type_id,
            "inheritance_family_native_type_id",
        )
        if (
            type(self.layers) is not tuple
            or not self.layers
            or not all(type(item) is FreeCadCapabilityLayerRef for item in self.layers)
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "layers")
        statuses = tuple(item.status for item in self.layers)
        if statuses[0] is not CapabilitySupportStatus.DISCOVERED or len(set(statuses)) != len(
            statuses
        ):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "layers")
        object.__setattr__(
            self,
            "layers",
            tuple(sorted(self.layers, key=lambda item: item.status.rank)),
        )

    @property
    def status(self) -> CapabilitySupportStatus:
        return self.layers[-1].status

    @property
    def descriptor_sha256(self) -> str:
        return self.layers[-1].descriptor_sha256

    def layer(self, status: CapabilitySupportStatus) -> FreeCadCapabilityLayerRef | None:
        if type(status) is not CapabilitySupportStatus:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "status")
        return next((item for item in self.layers if item.status is status), None)

    def _mapping(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "declaring_module": self.declaring_module,
            "active_descriptor_sha256": self.descriptor_sha256,
            "active_status": self.status.value,
            "inheritance_family_native_type_id": self.inheritance_family_native_type_id,
            "layers": {
                status.value: (
                    None if self.layer(status) is None else self.layer(status)._mapping()
                )
                for status in CapabilitySupportStatus
            },
            "native_type_id": self.native_type_id,
            "parent_native_type_id": self.parent_native_type_id,
            "semantic_kind": self.semantic_kind.value,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadFormalTypeBinding:
    formal_capability_id: str
    formal_catalog_sha256: str
    formal_descriptor_sha256: str
    formal_status: CapabilitySupportStatus
    native_type_id: str
    native_capability_id: str
    native_descriptor_sha256: str
    binding_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.formal_capability_id, "formal_capability_id")
        _digest(self.formal_catalog_sha256, "formal_catalog_sha256")
        _digest(self.formal_descriptor_sha256, "formal_descriptor_sha256")
        if (
            type(self.formal_status) is not CapabilitySupportStatus
            or self.formal_status.rank < CapabilitySupportStatus.EXECUTABLE.rank
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "formal_status")
        _identifier(self.native_type_id, "native_type_id")
        _identifier(self.native_capability_id, "native_capability_id")
        if self.native_capability_id != freecad_type_capability_id(self.native_type_id):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "native_capability_id")
        _digest(self.native_descriptor_sha256, "native_descriptor_sha256")
        _digest(self.binding_sha256, "binding_sha256")
        expected = hashlib.sha256(
            _FORMAL_BINDING_DIGEST_DOMAIN + _canonical(self._body_mapping())
        ).hexdigest()
        if not hmac.compare_digest(expected, self.binding_sha256):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "binding_sha256")

    def _body_mapping(self) -> dict[str, object]:
        return {
            "formal_capability_id": self.formal_capability_id,
            "formal_catalog_sha256": self.formal_catalog_sha256,
            "formal_descriptor_sha256": self.formal_descriptor_sha256,
            "formal_status": self.formal_status.value,
            "native_capability_id": self.native_capability_id,
            "native_descriptor_sha256": self.native_descriptor_sha256,
            "native_type_id": self.native_type_id,
        }

    def _mapping(self) -> dict[str, object]:
        return {**self._body_mapping(), "binding_sha256": self.binding_sha256}


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadCapabilityProjectionManifestV2:
    schema_version: int
    backend: CapabilityBackend
    discovery_snapshot_sha256: str
    discovery_manifest_sha256: str
    promotion_pack_sha256: tuple[str, ...]
    formal_catalog_sha256: tuple[str, ...]
    entries: tuple[FreeCadCapabilityIndexEntry, ...]
    formal_bindings: tuple[FreeCadFormalTypeBinding, ...]
    module_index: tuple[tuple[str, tuple[str, ...]], ...]
    semantic_kind_index: tuple[tuple[str, tuple[str, ...]], ...]
    inheritance_family_index: tuple[tuple[str, tuple[str, ...]], ...]
    status_index: tuple[tuple[str, tuple[str, ...]], ...]
    layer_status_index: tuple[tuple[str, tuple[str, ...]], ...]

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != FREECAD_CAPABILITY_PROJECTION_V2_SCHEMA_VERSION
        ):
            _fail(CapabilityCatalogErrorCode.UNSUPPORTED_VERSION, "schema_version")
        if type(self.backend) is not CapabilityBackend:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "backend")
        _digest(self.discovery_snapshot_sha256, "discovery_snapshot_sha256")
        _digest(self.discovery_manifest_sha256, "discovery_manifest_sha256")
        for path, values, item_type in (
            ("promotion_pack_sha256", self.promotion_pack_sha256, str),
            ("formal_catalog_sha256", self.formal_catalog_sha256, str),
            ("entries", self.entries, FreeCadCapabilityIndexEntry),
            ("formal_bindings", self.formal_bindings, FreeCadFormalTypeBinding),
        ):
            if type(values) is not tuple or not all(type(item) is item_type for item in values):
                _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
        for path, values in (
            ("promotion_pack_sha256", self.promotion_pack_sha256),
            ("formal_catalog_sha256", self.formal_catalog_sha256),
        ):
            if len(set(values)) != len(values) or values != tuple(sorted(values)):
                _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
            for value in values:
                _digest(value, path)
        if self.entries != tuple(sorted(self.entries, key=lambda item: item.native_type_id)):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "entries")
        if len({item.native_type_id for item in self.entries}) != len(self.entries):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "entries")
        if self.formal_bindings != tuple(
            sorted(self.formal_bindings, key=lambda item: item.formal_capability_id)
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "formal_bindings")
        if len({item.formal_capability_id for item in self.formal_bindings}) != len(
            self.formal_bindings
        ):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "formal_bindings")
        if any(
            layer.promotion_pack_sha256 not in self.promotion_pack_sha256
            for item in self.entries
            for layer in item.layers
            if layer.promotion_pack_sha256 is not None
        ):
            _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "entries/layers")
        if any(
            item.formal_catalog_sha256 not in self.formal_catalog_sha256
            for item in self.formal_bindings
        ):
            _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "formal_bindings")
        expected_indexes = (
            _index_rows(self.entries, "declaring_module"),
            _index_rows(self.entries, "semantic_kind"),
            _index_rows(self.entries, "inheritance_family_native_type_id"),
            _index_rows(self.entries, "status"),
            _layer_index_rows(self.entries),
        )
        if expected_indexes != (
            self.module_index,
            self.semantic_kind_index,
            self.inheritance_family_index,
            self.status_index,
            self.layer_status_index,
        ):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "indexes")
        _canonical(self._body_mapping())

    def _body_mapping(self) -> dict[str, object]:
        def indexes(values: tuple[tuple[str, tuple[str, ...]], ...]) -> list[dict[str, object]]:
            return [{"key": key, "native_type_ids": list(native_ids)} for key, native_ids in values]

        return {
            "backend": _backend_mapping(self.backend),
            "discovery_manifest_sha256": self.discovery_manifest_sha256,
            "discovery_snapshot_sha256": self.discovery_snapshot_sha256,
            "entries": [item._mapping() for item in self.entries],
            "formal_bindings": [item._mapping() for item in self.formal_bindings],
            "formal_catalog_sha256": list(self.formal_catalog_sha256),
            "inheritance_family_index": indexes(self.inheritance_family_index),
            "layer_status_index": indexes(self.layer_status_index),
            "module_index": indexes(self.module_index),
            "promotion_pack_sha256": list(self.promotion_pack_sha256),
            "schema_version": self.schema_version,
            "semantic_kind_index": indexes(self.semantic_kind_index),
            "status_index": indexes(self.status_index),
        }

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(
            _PROJECTION_DIGEST_DOMAIN + _canonical(self._body_mapping())
        ).hexdigest()

    @property
    def coverage(self) -> tuple[tuple[CapabilitySupportStatus, int], ...]:
        return tuple(
            (status, sum(item.status is status for item in self.entries))
            for status in CapabilitySupportStatus
        )

    @property
    def layer_coverage(self) -> tuple[tuple[CapabilitySupportStatus, int], ...]:
        return tuple(
            (status, sum(item.layer(status) is not None for item in self.entries))
            for status in CapabilitySupportStatus
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadCapabilityProjectionV2:
    manifest: FreeCadCapabilityProjectionManifestV2
    discovery_pages: tuple[CapabilityCatalogSegment, ...]
    promotion_segments: tuple[CapabilityCatalogSegment, ...]
    formal_catalogs: tuple[CapabilityCatalogSegment, ...]
    index: CapabilityCatalogIndex

    def __post_init__(self) -> None:
        if type(self.manifest) is not FreeCadCapabilityProjectionManifestV2:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "manifest")
        for path, values in (
            ("discovery_pages", self.discovery_pages),
            ("promotion_segments", self.promotion_segments),
            ("formal_catalogs", self.formal_catalogs),
        ):
            if type(values) is not tuple or not all(
                type(item) is CapabilityCatalogSegment for item in values
            ):
                _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
        if type(self.index) is not CapabilityCatalogIndex:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "index")
        if self.index.backend != self.manifest.backend:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "index/backend")
        discovered_catalogs = {item.catalog_sha256 for item in self.discovery_pages}
        promotion_catalog_to_pack = {
            item.catalog_sha256: item.discovery_receipt_sha256 for item in self.promotion_segments
        }
        catalog_by_digest = {
            item.catalog_sha256: item for item in (*self.discovery_pages, *self.promotion_segments)
        }
        if tuple(sorted(promotion_catalog_to_pack.values())) != (
            self.manifest.promotion_pack_sha256
        ) or tuple(sorted(item.catalog_sha256 for item in self.formal_catalogs)) != (
            self.manifest.formal_catalog_sha256
        ):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "manifest/catalogs")
        for entry in self.manifest.entries:
            for layer in entry.layers:
                if layer.status is CapabilitySupportStatus.DISCOVERED:
                    if layer.catalog_sha256 not in discovered_catalogs:
                        _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "entries/layers")
                else:
                    pack_digest = promotion_catalog_to_pack.get(layer.catalog_sha256)
                    if pack_digest is None or not hmac.compare_digest(
                        pack_digest,
                        layer.promotion_pack_sha256,
                    ):
                        _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "entries/layers")
                source = catalog_by_digest.get(layer.catalog_sha256)
                if source is None:
                    _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "entries/layers")
                matches = tuple(
                    descriptor
                    for descriptor in source.descriptors
                    if descriptor.capability_id == entry.capability_id
                )
                if (
                    len(matches) != 1
                    or matches[0].status is not layer.status
                    or not hmac.compare_digest(
                        matches[0].descriptor_sha256,
                        layer.descriptor_sha256,
                    )
                ):
                    _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "entries/layers")
        expected = CapabilityCatalogIndex(
            (*self.discovery_pages, *self.promotion_segments, *self.formal_catalogs)
        )
        if not hmac.compare_digest(expected.catalog_sha256, self.index.catalog_sha256):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "index")
        for entry in self.manifest.entries:
            active = expected.lookup(entry.capability_id)
            if active.status is not entry.status or not hmac.compare_digest(
                active.descriptor_sha256, entry.descriptor_sha256
            ):
                _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "entries")


def encode_freecad_capability_promotion_pack(
    value: FreeCadCapabilityPromotionPack,
) -> bytes:
    """Return a bounded self-authenticating canonical promotion-pack envelope."""

    if type(value) is not FreeCadCapabilityPromotionPack:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "promotion_pack")
    return _canonical(
        {**value._mapping(), "pack_sha256": value.pack_sha256},
        maximum=MAX_FREECAD_CAPABILITY_PROMOTION_PACK_BYTES,
    )


def encode_freecad_capability_projection_manifest_v2(
    value: FreeCadCapabilityProjectionManifestV2,
) -> bytes:
    """Return the bounded canonical internal projection manifest."""

    if type(value) is not FreeCadCapabilityProjectionManifestV2:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "manifest")
    return _canonical({**value._body_mapping(), "manifest_sha256": value.manifest_sha256})


def _backend_mapping(value: CapabilityBackend) -> dict[str, object]:
    return {
        "backend_id": value.backend_id,
        "backend_version": list(value.backend_version),
        "build_fingerprint_sha256": value.build_fingerprint_sha256,
        "discovery_profile": value.discovery_profile.value,
        "platform_id": value.platform_id,
    }


def _term_mapping(value: CapabilityTermRef) -> dict[str, object]:
    return {
        "namespace": value.namespace,
        "term_definition_sha256": value.term_definition_sha256,
        "term_id": value.term_id,
        "term_ref_id": value.term_ref_id,
        "vocabulary_version": value.vocabulary_version,
    }


def _fact_mapping(value: CapabilityFact) -> dict[str, object]:
    return {
        "key_term_ref_id": value.key_term_ref_id,
        "unit_term_ref_id": value.unit_term_ref_id,
        "value": value.decoded_value,
        "value_term_ref_id": value.value_term_ref_id,
    }


def _external_ref_mapping(value: ExternalCapabilityRef) -> dict[str, object]:
    return {
        "capability_id": value.capability_id,
        "descriptor_sha256": value.descriptor_sha256,
    }


def _verification_ref(
    *,
    pack: FreeCadCapabilityPromotionPack,
    entry: FreeCadCapabilityPromotionEntry,
) -> CapabilityVerificationRef | None:
    binding = entry.verification
    if binding is None:
        return None
    body = {
        "adapter_contract_sha256": pack.adapter_contract_sha256,
        "adapter_id": pack.adapter_id,
        "adapter_version": pack.adapter_version,
        "backend": _backend_mapping(pack.backend),
        "discovery_manifest_sha256": pack.discovery_manifest_sha256,
        "discovery_snapshot_sha256": pack.discovery_snapshot_sha256,
        "entry": entry._mapping(),
        "lane_id": pack.lane_id,
        "pack_id": pack.pack_id,
        "pack_sha256": pack.pack_sha256,
        "schema_version": FREECAD_CAPABILITY_PROMOTION_PACK_SCHEMA_VERSION,
    }
    raw = _canonical(body, maximum=MAX_FREECAD_CAPABILITY_PROMOTION_PACK_BYTES)
    return CapabilityVerificationRef(
        receipt_sha256=hashlib.sha256(_VERIFICATION_BINDING_DIGEST_DOMAIN + raw).hexdigest(),
        receipt_size_bytes=len(raw),
        verifier_id=binding.verifier_id,
        verifier_version=binding.verifier_version,
    )


def _family(
    value: FreeCadRegisteredType,
    by_native: dict[str, FreeCadRegisteredType],
) -> str:
    chain: list[str] = []
    seen: set[str] = set()
    cursor: FreeCadRegisteredType | None = value
    while cursor is not None:
        if cursor.native_type_id in seen:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "inheritance")
        seen.add(cursor.native_type_id)
        chain.append(cursor.native_type_id)
        if cursor.parent_native_type_id is None:
            cursor = None
            continue
        cursor = by_native.get(cursor.parent_native_type_id)
        if cursor is None:
            _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "inheritance")
    chain.reverse()
    category_root = {
        FreeCadNativeTypeCategory.DOCUMENT_OBJECT: "App::DocumentObject",
        FreeCadNativeTypeCategory.PROPERTY_TYPE: "App::Property",
        FreeCadNativeTypeCategory.EXTENSION_TYPE: "App::Extension",
    }.get(value.category)
    if category_root is not None and category_root in chain:
        root_index = chain.index(category_root)
        return chain[root_index + 1] if root_index + 1 < len(chain) else category_root
    return chain[0]


def _promotion_segment(
    *,
    pack: FreeCadCapabilityPromotionPack,
    discovery_index: CapabilityCatalogIndex,
) -> CapabilityCatalogSegment:
    descriptors: list[CapabilityDescriptor] = []
    external = {item.capability_id: item for item in pack.external_refs}
    terms = {item.term_ref_id: item for item in pack.terms}
    discovery_terms = {
        item.term_ref_id: item for segment in discovery_index.segments for item in segment.terms
    }
    identities = {
        (item.namespace, item.vocabulary_version, item.term_id): item
        for item in discovery_terms.values()
    }
    for term_ref_id, term in terms.items():
        prior = discovery_terms.get(term_ref_id)
        if prior is not None and prior != term:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "pack/terms")
        identity = (term.namespace, term.vocabulary_version, term.term_id)
        lexical_prior = identities.get(identity)
        if lexical_prior is not None and lexical_prior != term:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "pack/terms")
        identities[identity] = term
    for entry in pack.entries:
        values = discovery_index.lookup_native(entry.native_type_id)
        if len(values) != 1:
            _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "pack/entries")
        base = values[0]
        if (
            base.kind is not _CAPABILITY_KIND[entry.semantic_kind]
            or base.status is not CapabilitySupportStatus.DISCOVERED
        ):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "pack/entries")
        required_discovery_ids = (base.declaring_module_id, *base.dependency_ids)
        for capability_id in required_discovery_ids:
            dependency = discovery_index.lookup(capability_id)
            reference = ExternalCapabilityRef(
                capability_id=dependency.capability_id,
                descriptor_sha256=dependency.descriptor_sha256,
            )
            prior = external.get(reference.capability_id)
            if prior is not None and prior != reference:
                _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "pack/external_refs")
            external[reference.capability_id] = reference
        for term_ref_id in base.semantic_term_ref_ids:
            term = discovery_terms.get(term_ref_id)
            if term is None:
                _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "pack/entries/terms")
            terms[term.term_ref_id] = term
        descriptors.append(
            CapabilityDescriptor(
                capability_id=base.capability_id,
                kind=base.kind,
                native_identifier=base.native_identifier,
                declaring_module_id=base.declaring_module_id,
                status=entry.target_status,
                risk_class=entry.risk_class,
                semantic_term_ref_ids=tuple(
                    sorted(set(base.semantic_term_ref_ids) | set(entry.semantic_term_ref_ids))
                ),
                facts=(*base.facts, *entry.facts),
                execution_profiles=entry.execution_profiles,
                lifecycle_stages=entry.lifecycle_stages,
                dependency_ids=tuple(sorted(set(base.dependency_ids) | set(entry.dependency_ids))),
                verification=_verification_ref(pack=pack, entry=entry),
            )
        )
    segment = CapabilityCatalogSegment(
        schema_version=1,
        segment_id=f"freecad.promotion.{pack.pack_sha256[:32]}",
        backend=pack.backend,
        discovery_receipt_sha256=pack.pack_sha256,
        discovery_algorithm_id="vcad.freecad.capability.promotion-pack",
        discovery_algorithm_version="1.0",
        terms=tuple(terms.values()),
        descriptors=tuple(descriptors),
        external_refs=tuple(external.values()),
    )
    encode_capability_catalog(segment)
    return segment


def _index_rows(
    values: tuple[FreeCadCapabilityIndexEntry, ...],
    field: str,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    grouped: dict[str, list[str]] = {}
    for item in values:
        key = getattr(item, field)
        grouped.setdefault(key.value if isinstance(key, StrEnum) else key, []).append(
            item.native_type_id
        )
    return tuple((key, tuple(sorted(native_ids))) for key, native_ids in sorted(grouped.items()))


def _layer_index_rows(
    values: tuple[FreeCadCapabilityIndexEntry, ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    grouped = {
        status.value: tuple(
            item.native_type_id for item in values if item.layer(status) is not None
        )
        for status in CapabilitySupportStatus
    }
    return tuple((key, tuple(sorted(native_ids))) for key, native_ids in sorted(grouped.items()))


def _validate_term_identities(
    segments: tuple[CapabilityCatalogSegment, ...],
    promotion_packs: tuple[FreeCadCapabilityPromotionPack, ...],
) -> None:
    by_ref: dict[str, CapabilityTermRef] = {}
    by_identity: dict[tuple[str, str, str], CapabilityTermRef] = {}
    terms = (
        *(term for segment in segments for term in segment.terms),
        *(term for pack in promotion_packs for term in pack.terms),
    )
    for term in terms:
        prior_ref = by_ref.get(term.term_ref_id)
        identity = (term.namespace, term.vocabulary_version, term.term_id)
        prior_identity = by_identity.get(identity)
        if (prior_ref is not None and prior_ref != term) or (
            prior_identity is not None and prior_identity != term
        ):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "terms")
        by_ref[term.term_ref_id] = term
        by_identity[identity] = term


def _formal_bindings(
    *,
    formal_catalogs: tuple[CapabilityCatalogSegment, ...],
    discovery_index: CapabilityCatalogIndex,
    aggregate_index: CapabilityCatalogIndex,
) -> tuple[FreeCadFormalTypeBinding, ...]:
    result: list[FreeCadFormalTypeBinding] = []
    candidates: dict[str, CapabilityDescriptor] = {}
    source_by_descriptor: dict[str, str] = {}
    for catalog in formal_catalogs:
        for formal in catalog.descriptors:
            if formal.kind in {
                CapabilityKind.MODULE,
                CapabilityKind.NATIVE_TYPE,
                CapabilityKind.DOCUMENT_OBJECT,
                CapabilityKind.PROPERTY_TYPE,
                CapabilityKind.EXTENSION_TYPE,
            }:
                continue
            prior_source = source_by_descriptor.get(formal.descriptor_sha256)
            if prior_source is not None and prior_source != catalog.catalog_sha256:
                _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "formal_catalogs")
            source_by_descriptor[formal.descriptor_sha256] = catalog.catalog_sha256
            candidates[formal.capability_id] = aggregate_index.lookup(formal.capability_id)
    for capability_id, formal in sorted(candidates.items()):
        native_matches = discovery_index.lookup_native(formal.native_identifier)
        if not native_matches:
            if "::" in formal.native_identifier:
                _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "formal_catalogs")
            continue
        if formal.status.rank < CapabilitySupportStatus.EXECUTABLE.rank:
            _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "formal_catalogs")
        if len(native_matches) != 1:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "formal_catalogs")
        catalog_sha256 = source_by_descriptor.get(formal.descriptor_sha256)
        if catalog_sha256 is None:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "formal_catalogs")
        native = native_matches[0]
        body = {
            "formal_capability_id": capability_id,
            "formal_catalog_sha256": catalog_sha256,
            "formal_descriptor_sha256": formal.descriptor_sha256,
            "formal_status": formal.status.value,
            "native_capability_id": native.capability_id,
            "native_descriptor_sha256": native.descriptor_sha256,
            "native_type_id": native.native_identifier,
        }
        result.append(
            FreeCadFormalTypeBinding(
                formal_capability_id=capability_id,
                formal_catalog_sha256=catalog_sha256,
                formal_descriptor_sha256=formal.descriptor_sha256,
                formal_status=formal.status,
                native_type_id=native.native_identifier,
                native_capability_id=native.capability_id,
                native_descriptor_sha256=native.descriptor_sha256,
                binding_sha256=hashlib.sha256(
                    _FORMAL_BINDING_DIGEST_DOMAIN + _canonical(body)
                ).hexdigest(),
            )
        )
    if len(result) > MAX_FREECAD_CAPABILITY_FORMAL_BINDINGS:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "formal_catalogs")
    return tuple(sorted(result, key=lambda item: item.formal_capability_id))


def _validate_formal_catalogs(
    *,
    formal_catalogs: tuple[CapabilityCatalogSegment, ...],
    discovery_index: CapabilityCatalogIndex,
) -> None:
    type_kinds = {
        CapabilityKind.NATIVE_TYPE,
        CapabilityKind.DOCUMENT_OBJECT,
        CapabilityKind.PROPERTY_TYPE,
        CapabilityKind.EXTENSION_TYPE,
    }
    discovered_ids = set(discovery_index.descriptors)
    for catalog in formal_catalogs:
        for descriptor in catalog.descriptors:
            if descriptor.kind in type_kinds or descriptor.capability_id in discovered_ids:
                _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "formal_catalogs")
            if descriptor.kind is CapabilityKind.MODULE:
                continue
            matches = discovery_index.lookup_native(descriptor.native_identifier)
            if not matches and "::" in descriptor.native_identifier:
                _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "formal_catalogs")
            if matches and descriptor.status.rank < CapabilitySupportStatus.EXECUTABLE.rank:
                _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "formal_catalogs")


def build_freecad_capability_projection_v2(
    *,
    discovery: FreeCadPagedCapabilityCatalog,
    promotion_packs: tuple[FreeCadCapabilityPromotionPack, ...] = (),
    formal_catalogs: tuple[CapabilityCatalogSegment, ...] = (),
) -> FreeCadCapabilityProjectionV2:
    """Build one stable complete TypeId index and monotonic promotion overlay."""

    if type(discovery) is not FreeCadPagedCapabilityCatalog:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "discovery")
    validate_freecad_capability_page_set(discovery.manifest, discovery.pages)
    if type(promotion_packs) is not tuple:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "promotion_packs")
    if len(promotion_packs) > MAX_FREECAD_CAPABILITY_PROMOTION_PACKS:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "promotion_packs")
    if not all(type(item) is FreeCadCapabilityPromotionPack for item in promotion_packs):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "promotion_packs")
    if type(formal_catalogs) is not tuple:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "formal_catalogs")
    if len(formal_catalogs) > MAX_FREECAD_CAPABILITY_FORMAL_CATALOGS:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "formal_catalogs")
    if not all(type(item) is CapabilityCatalogSegment for item in formal_catalogs):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "formal_catalogs")
    backend = discovery.snapshot.backend
    if any(
        pack.backend != backend
        or not hmac.compare_digest(
            pack.discovery_snapshot_sha256,
            discovery.snapshot.snapshot_sha256,
        )
        or not hmac.compare_digest(
            pack.discovery_manifest_sha256,
            discovery.manifest.manifest_sha256,
        )
        for pack in promotion_packs
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "promotion_packs")
    if any(catalog.backend != backend for catalog in formal_catalogs):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "formal_catalogs/backend")
    pack_digests = tuple(pack.pack_sha256 for pack in promotion_packs)
    pack_ids = tuple(pack.pack_id for pack in promotion_packs)
    if len(set(pack_digests)) != len(pack_digests) or len(set(pack_ids)) != len(pack_ids):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "promotion_packs")
    formal_digests = tuple(catalog.catalog_sha256 for catalog in formal_catalogs)
    if len(set(formal_digests)) != len(formal_digests):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "formal_catalogs")
    ordered_packs = tuple(sorted(promotion_packs, key=lambda item: item.pack_sha256))
    ordered_formals = tuple(sorted(formal_catalogs, key=lambda item: item.catalog_sha256))
    _validate_term_identities((*discovery.pages, *ordered_formals), ordered_packs)
    discovery_index = CapabilityCatalogIndex(discovery.pages)
    _validate_formal_catalogs(
        formal_catalogs=ordered_formals,
        discovery_index=discovery_index,
    )
    seen_promotion_layers: set[tuple[str, CapabilitySupportStatus]] = set()
    for pack in ordered_packs:
        for entry in pack.entries:
            key = (entry.native_type_id, entry.target_status)
            if key in seen_promotion_layers:
                _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "promotion_packs/entries")
            seen_promotion_layers.add(key)
    promotion_segments = tuple(
        _promotion_segment(pack=pack, discovery_index=discovery_index) for pack in ordered_packs
    )
    index = CapabilityCatalogIndex((*discovery.pages, *promotion_segments, *ordered_formals))
    by_native = {item.native_type_id: item for item in discovery.snapshot.registered_types}
    type_capability_ids = {freecad_type_capability_id(item) for item in by_native}
    discovery_catalog_by_capability: dict[str, str] = {}
    for page in discovery.pages:
        for descriptor in page.descriptors:
            if descriptor.capability_id not in type_capability_ids:
                continue
            if descriptor.capability_id in discovery_catalog_by_capability:
                _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "discovery/pages")
            discovery_catalog_by_capability[descriptor.capability_id] = page.catalog_sha256
    promotion_layers: dict[str, list[FreeCadCapabilityLayerRef]] = {}
    for pack, segment in zip(ordered_packs, promotion_segments, strict=True):
        descriptor_by_native = {item.native_identifier: item for item in segment.descriptors}
        for entry in pack.entries:
            descriptor = descriptor_by_native.get(entry.native_type_id)
            if descriptor is None or descriptor.status is not entry.target_status:
                _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "promotion_segments")
            promotion_layers.setdefault(entry.native_type_id, []).append(
                FreeCadCapabilityLayerRef(
                    status=descriptor.status,
                    descriptor_sha256=descriptor.descriptor_sha256,
                    catalog_sha256=segment.catalog_sha256,
                    promotion_pack_sha256=pack.pack_sha256,
                )
            )
    entries_list: list[FreeCadCapabilityIndexEntry] = []
    for item in sorted(
        discovery.snapshot.registered_types,
        key=lambda value: value.native_type_id,
    ):
        capability_id = freecad_type_capability_id(item.native_type_id)
        discovered_descriptor = discovery_index.lookup(capability_id)
        source_catalog = discovery_catalog_by_capability.get(capability_id)
        if source_catalog is None:
            _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "discovery/pages")
        layers = (
            FreeCadCapabilityLayerRef(
                status=CapabilitySupportStatus.DISCOVERED,
                descriptor_sha256=discovered_descriptor.descriptor_sha256,
                catalog_sha256=source_catalog,
                promotion_pack_sha256=None,
            ),
            *promotion_layers.get(item.native_type_id, ()),
        )
        projected = FreeCadCapabilityIndexEntry(
            native_type_id=item.native_type_id,
            capability_id=capability_id,
            declaring_module=item.declaring_module,
            semantic_kind=_SEMANTIC_KIND[item.category],
            parent_native_type_id=item.parent_native_type_id,
            inheritance_family_native_type_id=_family(item, by_native),
            layers=layers,
        )
        active = index.lookup(capability_id)
        if projected.status is not active.status or not hmac.compare_digest(
            projected.descriptor_sha256, active.descriptor_sha256
        ):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "entries/layers")
        entries_list.append(projected)
    entries = tuple(entries_list)
    bindings = _formal_bindings(
        formal_catalogs=ordered_formals,
        discovery_index=discovery_index,
        aggregate_index=index,
    )
    manifest = FreeCadCapabilityProjectionManifestV2(
        schema_version=FREECAD_CAPABILITY_PROJECTION_V2_SCHEMA_VERSION,
        backend=backend,
        discovery_snapshot_sha256=discovery.snapshot.snapshot_sha256,
        discovery_manifest_sha256=discovery.manifest.manifest_sha256,
        promotion_pack_sha256=tuple(sorted(pack_digests)),
        formal_catalog_sha256=tuple(sorted(formal_digests)),
        entries=entries,
        formal_bindings=bindings,
        module_index=_index_rows(entries, "declaring_module"),
        semantic_kind_index=_index_rows(entries, "semantic_kind"),
        inheritance_family_index=_index_rows(entries, "inheritance_family_native_type_id"),
        status_index=_index_rows(entries, "status"),
        layer_status_index=_layer_index_rows(entries),
    )
    _canonical(manifest._body_mapping())
    return FreeCadCapabilityProjectionV2(
        manifest=manifest,
        discovery_pages=discovery.pages,
        promotion_segments=promotion_segments,
        formal_catalogs=ordered_formals,
        index=index,
    )


__all__ = ()
