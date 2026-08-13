"""Backend capability descriptors independent from workflow and CAD intent.

The catalog in this module is metadata only.  It can describe native object
types, properties, constraints, operations, file formats, solvers, workbenches,
commands, and add-ons without making any of them executable.  Discovery and
execution are intentionally separate: only a trusted adapter may promote a
descriptor through ``representable`` and ``executable`` to ``verified``.

Semantics and future backend-specific facts are open but inert.  Every term
and fact key is content-addressed; values are bounded canonical JSON and are
never treated as Python names, import paths, handlers, macros, or commands.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum

CAPABILITY_CATALOG_SCHEMA_VERSION = 1
MAX_CAPABILITY_CATALOG_BYTES = 256 * 1024
MAX_CAPABILITY_DESCRIPTORS = 512
MAX_CAPABILITY_RELATIONS = 1024
MAX_CAPABILITY_TERMS = 256
MAX_CAPABILITY_EXTERNAL_REFS = 512
MAX_CAPABILITY_FACTS_PER_DESCRIPTOR = 64
MAX_CAPABILITY_TERM_REFS_PER_DESCRIPTOR = 16
MAX_CAPABILITY_RELATION_ENDPOINTS = 16
MAX_CAPABILITY_EXECUTION_PROFILES = 3
MAX_CAPABILITY_LIFECYCLE_STAGES = 16
MAX_CAPABILITY_DEPENDENCIES = 32

_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 32_768
_MAX_JSON_STRING_BYTES = 64 * 1024
_MAX_IDENTIFIER_BYTES = 128
_MAX_TERM_BYTES = 192
_MAX_FACT_VALUE_BYTES = 128 * 1024
_MAX_VERSION_COMPONENT = 999_999
_DIGEST_DOMAIN = b"vibecad-capability-catalog-v1\0"
_ID_DOMAIN = b"vibecad-capability-catalog-id-v1\0"
_DESCRIPTOR_DIGEST_DOMAIN = b"vibecad-capability-descriptor-v1\0"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,127}$")
_TERM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,191}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class CapabilityCatalogErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_VERSION = "unsupported_version"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTEGRITY_FAILURE = "integrity_failure"
    UNKNOWN_REFERENCE = "unknown_reference"
    INVALID_STATUS = "invalid_status"


class CapabilityCatalogError(ValueError):
    """Bounded capability-contract failure."""

    def __init__(self, code: CapabilityCatalogErrorCode, path: str = "") -> None:
        if type(code) is not CapabilityCatalogErrorCode:
            raise TypeError("code must be an exact CapabilityCatalogErrorCode")
        try:
            bounded_path = type(path) is str and len(path.encode("utf-8")) <= 256
        except UnicodeError:
            bounded_path = False
        if not bounded_path:
            raise ValueError("path must be bounded")
        self.code = code
        self.path = path
        super().__init__(code.value)


def _fail(code: CapabilityCatalogErrorCode, path: str = "") -> None:
    raise CapabilityCatalogError(code, path)


def _text(value: object, path: str, *, term: bool = False) -> str:
    pattern = _TERM if term else _IDENTIFIER
    maximum = _MAX_TERM_BYTES if term else _MAX_IDENTIFIER_BYTES
    if type(value) is not str:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if not value or size > maximum or pattern.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if ".." in value or "//" in value:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _bounded_tuple(
    value: object,
    path: str,
    *,
    item_type: type,
    maximum: int,
    unique_key=None,
) -> tuple:
    if type(value) is not tuple:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if len(value) > maximum:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, path)
    if not all(type(item) is item_type for item in value):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if unique_key is not None:
        keys = tuple(unique_key(item) for item in value)
        if len(set(keys)) != len(keys):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _json_tree(value: object, path: str, *, depth: int = 0, remaining: list[int]) -> None:
    remaining[0] -= 1
    if remaining[0] < 0:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, path)
    if depth > _MAX_JSON_DEPTH:
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
            _json_tree(item, f"{path}/{index}", depth=depth + 1, remaining=remaining)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
            _json_tree(key, path, depth=depth + 1, remaining=remaining)
            _json_tree(item, f"{path}/{key}", depth=depth + 1, remaining=remaining)
        return
    _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)


def _canonical_json(value: object, *, maximum: int = MAX_CAPABILITY_CATALOG_BYTES) -> bytes:
    _json_tree(value, "", remaining=[_MAX_JSON_NODES])
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


def _freeze_fact_value(value: object, path: str) -> bytes:
    raw = _canonical_json(value, maximum=_MAX_FACT_VALUE_BYTES)
    _decode_json(raw, maximum=_MAX_FACT_VALUE_BYTES)
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
    _json_tree(value, "", remaining=[_MAX_JSON_NODES])
    if _canonical_json(value, maximum=maximum) != raw:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT)
    return value


class CapabilityKind(StrEnum):
    MODULE = "module"
    WORKBENCH = "workbench"
    NATIVE_TYPE = "native_type"
    DOCUMENT_OBJECT = "document_object"
    PROPERTY_TYPE = "property_type"
    EXTENSION_TYPE = "extension_type"
    CONSTRAINT = "constraint"
    OPERATION = "operation"
    IMPORTER = "importer"
    EXPORTER = "exporter"
    SOLVER = "solver"
    COMMAND = "command"
    ADDON = "addon"


class CapabilitySupportStatus(StrEnum):
    DISCOVERED = "discovered"
    REPRESENTABLE = "representable"
    EXECUTABLE = "executable"
    VERIFIED = "verified"

    @property
    def rank(self) -> int:
        return tuple(CapabilitySupportStatus).index(self)


class CapabilityRiskClass(StrEnum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    UNKNOWN = "unknown"


class CapabilityExecutionProfile(StrEnum):
    HEADLESS = "headless"
    OFFSCREEN_GUI = "offscreen_gui"
    INTERACTIVE_GUI = "interactive_gui"


class CapabilityLifecycleStage(StrEnum):
    EXECUTE = "execute"
    INSPECT = "inspect"
    CREATE = "create"
    EDIT = "edit"
    DELETE = "delete"
    RECOMPUTE = "recompute"
    SAVE = "save"
    REOPEN = "reopen"
    IMPORT = "import"
    EXPORT = "export"


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityTermRef:
    term_ref_id: str
    namespace: str
    vocabulary_version: str
    term_id: str
    term_definition_sha256: str

    def __post_init__(self) -> None:
        _text(self.term_ref_id, "term_ref_id")
        _text(self.namespace, "namespace", term=True)
        _text(self.vocabulary_version, "vocabulary_version")
        _text(self.term_id, "term_id", term=True)
        _digest(self.term_definition_sha256, "term_definition_sha256")


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityFact:
    key_term_ref_id: str
    value: object
    value_term_ref_id: str | None = None
    unit_term_ref_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.key_term_ref_id, "key_term_ref_id")
        if self.value_term_ref_id is not None:
            _text(self.value_term_ref_id, "value_term_ref_id")
        if self.unit_term_ref_id is not None:
            _text(self.unit_term_ref_id, "unit_term_ref_id")
        object.__setattr__(self, "value", _freeze_fact_value(self.value, "value"))

    @property
    def decoded_value(self) -> object:
        return _decode_json(self.value, maximum=_MAX_FACT_VALUE_BYTES)


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityBackend:
    backend_id: str
    backend_version: tuple[int, ...]
    build_fingerprint_sha256: str
    platform_id: str
    discovery_profile: CapabilityExecutionProfile

    def __post_init__(self) -> None:
        _text(self.backend_id, "backend_id")
        if (
            type(self.backend_version) is not tuple
            or not 1 <= len(self.backend_version) <= 4
            or not all(
                type(item) is int and 0 <= item <= _MAX_VERSION_COMPONENT
                for item in self.backend_version
            )
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "backend_version")
        _digest(self.build_fingerprint_sha256, "build_fingerprint_sha256")
        _text(self.platform_id, "platform_id")
        if type(self.discovery_profile) is not CapabilityExecutionProfile:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "discovery_profile")


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityVerificationRef:
    receipt_sha256: str
    receipt_size_bytes: int
    verifier_id: str
    verifier_version: str

    def __post_init__(self) -> None:
        _digest(self.receipt_sha256, "receipt_sha256")
        if (
            type(self.receipt_size_bytes) is not int
            or not 0 < self.receipt_size_bytes <= _MAX_SAFE_INTEGER
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "receipt_size_bytes")
        _text(self.verifier_id, "verifier_id")
        _text(self.verifier_version, "verifier_version")


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityDescriptor:
    capability_id: str
    kind: CapabilityKind
    native_identifier: str
    declaring_module_id: str
    status: CapabilitySupportStatus
    risk_class: CapabilityRiskClass = CapabilityRiskClass.UNKNOWN
    semantic_term_ref_ids: tuple[str, ...] = ()
    facts: tuple[CapabilityFact, ...] = ()
    execution_profiles: tuple[CapabilityExecutionProfile, ...] = ()
    lifecycle_stages: tuple[CapabilityLifecycleStage, ...] = ()
    dependency_ids: tuple[str, ...] = ()
    verification: CapabilityVerificationRef | None = None

    def __post_init__(self) -> None:
        _text(self.capability_id, "capability_id")
        if type(self.kind) is not CapabilityKind:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "kind")
        _text(self.native_identifier, "native_identifier", term=True)
        _text(self.declaring_module_id, "declaring_module_id")
        if type(self.status) is not CapabilitySupportStatus:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "status")
        if type(self.risk_class) is not CapabilityRiskClass:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "risk_class")
        terms = _bounded_tuple(
            self.semantic_term_ref_ids,
            "semantic_term_ref_ids",
            item_type=str,
            maximum=MAX_CAPABILITY_TERM_REFS_PER_DESCRIPTOR,
            unique_key=lambda item: item,
        )
        for index, item in enumerate(terms):
            _text(item, f"semantic_term_ref_ids/{index}")
        facts = _bounded_tuple(
            self.facts,
            "facts",
            item_type=CapabilityFact,
            maximum=MAX_CAPABILITY_FACTS_PER_DESCRIPTOR,
            unique_key=lambda item: item.key_term_ref_id,
        )
        profiles = _bounded_tuple(
            self.execution_profiles,
            "execution_profiles",
            item_type=CapabilityExecutionProfile,
            maximum=MAX_CAPABILITY_EXECUTION_PROFILES,
            unique_key=lambda item: item,
        )
        lifecycle = _bounded_tuple(
            self.lifecycle_stages,
            "lifecycle_stages",
            item_type=CapabilityLifecycleStage,
            maximum=MAX_CAPABILITY_LIFECYCLE_STAGES,
            unique_key=lambda item: item,
        )
        dependencies = _bounded_tuple(
            self.dependency_ids,
            "dependency_ids",
            item_type=str,
            maximum=MAX_CAPABILITY_DEPENDENCIES,
            unique_key=lambda item: item,
        )
        for index, item in enumerate(dependencies):
            _text(item, f"dependency_ids/{index}")
        if self.status.rank < CapabilitySupportStatus.EXECUTABLE.rank:
            if profiles or lifecycle or self.verification is not None:
                _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "status")
        elif not profiles or not lifecycle:
            _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "status")
        if self.status is CapabilitySupportStatus.VERIFIED:
            if type(self.verification) is not CapabilityVerificationRef:
                _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "verification")
        elif self.verification is not None:
            _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "verification")
        object.__setattr__(self, "semantic_term_ref_ids", tuple(sorted(terms)))
        object.__setattr__(
            self, "facts", tuple(sorted(facts, key=lambda item: item.key_term_ref_id))
        )
        object.__setattr__(self, "execution_profiles", tuple(sorted(profiles, key=str)))
        object.__setattr__(self, "lifecycle_stages", tuple(sorted(lifecycle, key=str)))
        object.__setattr__(self, "dependency_ids", tuple(sorted(dependencies)))

    @property
    def descriptor_sha256(self) -> str:
        return hashlib.sha256(
            _DESCRIPTOR_DIGEST_DOMAIN + _canonical_json(_descriptor_mapping(self))
        ).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class ExternalCapabilityRef:
    """Content-addressed reference to a descriptor in another catalog segment."""

    capability_id: str
    descriptor_sha256: str

    def __post_init__(self) -> None:
        _text(self.capability_id, "capability_id")
        _digest(self.descriptor_sha256, "descriptor_sha256")


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityRelation:
    relation_id: str
    relation_term_ref_id: str
    source_capability_id: str
    target_capability_ids: tuple[str, ...]
    facts: tuple[CapabilityFact, ...] = ()

    def __post_init__(self) -> None:
        _text(self.relation_id, "relation_id")
        _text(self.relation_term_ref_id, "relation_term_ref_id")
        _text(self.source_capability_id, "source_capability_id")
        targets = _bounded_tuple(
            self.target_capability_ids,
            "target_capability_ids",
            item_type=str,
            maximum=MAX_CAPABILITY_RELATION_ENDPOINTS,
            unique_key=lambda item: item,
        )
        if not targets:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "target_capability_ids")
        for index, item in enumerate(targets):
            _text(item, f"target_capability_ids/{index}")
        facts = _bounded_tuple(
            self.facts,
            "facts",
            item_type=CapabilityFact,
            maximum=MAX_CAPABILITY_FACTS_PER_DESCRIPTOR,
            unique_key=lambda item: item.key_term_ref_id,
        )
        object.__setattr__(self, "target_capability_ids", tuple(sorted(targets)))
        object.__setattr__(
            self, "facts", tuple(sorted(facts, key=lambda item: item.key_term_ref_id))
        )


def _term_mapping(item: CapabilityTermRef) -> dict[str, object]:
    return {
        "namespace": item.namespace,
        "term_definition_sha256": item.term_definition_sha256,
        "term_id": item.term_id,
        "term_ref_id": item.term_ref_id,
        "vocabulary_version": item.vocabulary_version,
    }


def _fact_mapping(item: CapabilityFact) -> dict[str, object]:
    return {
        "key_term_ref_id": item.key_term_ref_id,
        "unit_term_ref_id": item.unit_term_ref_id,
        "value": item.decoded_value,
        "value_term_ref_id": item.value_term_ref_id,
    }


def _verification_mapping(item: CapabilityVerificationRef | None) -> object:
    if item is None:
        return None
    return {
        "receipt_sha256": item.receipt_sha256,
        "receipt_size_bytes": item.receipt_size_bytes,
        "verifier_id": item.verifier_id,
        "verifier_version": item.verifier_version,
    }


def _descriptor_mapping(item: CapabilityDescriptor) -> dict[str, object]:
    return {
        "capability_id": item.capability_id,
        "declaring_module_id": item.declaring_module_id,
        "dependency_ids": list(item.dependency_ids),
        "execution_profiles": [value.value for value in item.execution_profiles],
        "facts": [_fact_mapping(value) for value in item.facts],
        "kind": item.kind.value,
        "lifecycle_stages": [value.value for value in item.lifecycle_stages],
        "native_identifier": item.native_identifier,
        "risk_class": item.risk_class.value,
        "semantic_term_ref_ids": list(item.semantic_term_ref_ids),
        "status": item.status.value,
        "verification": _verification_mapping(item.verification),
    }


def _relation_mapping(item: CapabilityRelation) -> dict[str, object]:
    return {
        "facts": [_fact_mapping(value) for value in item.facts],
        "relation_id": item.relation_id,
        "relation_term_ref_id": item.relation_term_ref_id,
        "source_capability_id": item.source_capability_id,
        "target_capability_ids": list(item.target_capability_ids),
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityCatalogSegment:
    schema_version: int
    segment_id: str
    backend: CapabilityBackend
    discovery_receipt_sha256: str
    discovery_algorithm_id: str
    discovery_algorithm_version: str
    terms: tuple[CapabilityTermRef, ...]
    descriptors: tuple[CapabilityDescriptor, ...]
    external_refs: tuple[ExternalCapabilityRef, ...] = ()
    relations: tuple[CapabilityRelation, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != CAPABILITY_CATALOG_SCHEMA_VERSION
        ):
            _fail(CapabilityCatalogErrorCode.UNSUPPORTED_VERSION, "schema_version")
        _text(self.segment_id, "segment_id")
        if type(self.backend) is not CapabilityBackend:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "backend")
        _digest(self.discovery_receipt_sha256, "discovery_receipt_sha256")
        _text(self.discovery_algorithm_id, "discovery_algorithm_id")
        _text(self.discovery_algorithm_version, "discovery_algorithm_version")
        terms = _bounded_tuple(
            self.terms,
            "terms",
            item_type=CapabilityTermRef,
            maximum=MAX_CAPABILITY_TERMS,
            unique_key=lambda item: item.term_ref_id,
        )
        descriptors = _bounded_tuple(
            self.descriptors,
            "descriptors",
            item_type=CapabilityDescriptor,
            maximum=MAX_CAPABILITY_DESCRIPTORS,
            unique_key=lambda item: item.capability_id,
        )
        external_refs = _bounded_tuple(
            self.external_refs,
            "external_refs",
            item_type=ExternalCapabilityRef,
            maximum=MAX_CAPABILITY_EXTERNAL_REFS,
            unique_key=lambda item: item.capability_id,
        )
        relations = _bounded_tuple(
            self.relations,
            "relations",
            item_type=CapabilityRelation,
            maximum=MAX_CAPABILITY_RELATIONS,
            unique_key=lambda item: item.relation_id,
        )
        term_ids = {item.term_ref_id for item in terms}
        capability_ids = {item.capability_id for item in descriptors}
        external_ids = {item.capability_id for item in external_refs}
        if capability_ids & external_ids:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "external_refs")
        all_capability_ids = capability_ids | external_ids
        for item in descriptors:
            refs = set(item.semantic_term_ref_ids)
            refs.update(fact.key_term_ref_id for fact in item.facts)
            refs.update(
                ref
                for fact in item.facts
                for ref in (fact.value_term_ref_id, fact.unit_term_ref_id)
                if ref is not None
            )
            if not refs <= term_ids:
                _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, item.capability_id)
            if (
                item.declaring_module_id not in all_capability_ids
                or not set(item.dependency_ids) <= all_capability_ids
            ):
                _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, item.capability_id)
        for item in relations:
            if item.relation_term_ref_id not in term_ids:
                _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, item.relation_id)
            if (
                item.source_capability_id not in all_capability_ids
                or not set(item.target_capability_ids) <= all_capability_ids
            ):
                _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, item.relation_id)
            refs = {fact.key_term_ref_id for fact in item.facts}
            refs.update(
                ref
                for fact in item.facts
                for ref in (fact.value_term_ref_id, fact.unit_term_ref_id)
                if ref is not None
            )
            if not refs <= term_ids:
                _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, item.relation_id)
        object.__setattr__(self, "terms", tuple(sorted(terms, key=lambda item: item.term_ref_id)))
        object.__setattr__(
            self, "descriptors", tuple(sorted(descriptors, key=lambda item: item.capability_id))
        )
        object.__setattr__(
            self,
            "external_refs",
            tuple(sorted(external_refs, key=lambda item: item.capability_id)),
        )
        object.__setattr__(
            self, "relations", tuple(sorted(relations, key=lambda item: item.relation_id))
        )
        _canonical_json(self._body_mapping())

    def _body_mapping(self) -> dict[str, object]:
        return {
            "backend": {
                "backend_id": self.backend.backend_id,
                "backend_version": list(self.backend.backend_version),
                "build_fingerprint_sha256": self.backend.build_fingerprint_sha256,
                "discovery_profile": self.backend.discovery_profile.value,
                "platform_id": self.backend.platform_id,
            },
            "descriptors": [_descriptor_mapping(item) for item in self.descriptors],
            "discovery_algorithm_id": self.discovery_algorithm_id,
            "discovery_algorithm_version": self.discovery_algorithm_version,
            "discovery_receipt_sha256": self.discovery_receipt_sha256,
            "external_refs": [
                {
                    "capability_id": item.capability_id,
                    "descriptor_sha256": item.descriptor_sha256,
                }
                for item in self.external_refs
            ],
            "relations": [_relation_mapping(item) for item in self.relations],
            "schema_version": self.schema_version,
            "segment_id": self.segment_id,
            "terms": [_term_mapping(item) for item in self.terms],
        }

    @property
    def catalog_sha256(self) -> str:
        return hashlib.sha256(_DIGEST_DOMAIN + _canonical_json(self._body_mapping())).hexdigest()

    @property
    def catalog_id(self) -> str:
        digest = hashlib.sha256(_ID_DOMAIN + bytes.fromhex(self.catalog_sha256)).hexdigest()
        return f"capability_catalog_{digest[:32]}"

    def support_counts(self) -> tuple[tuple[CapabilitySupportStatus, int], ...]:
        return tuple(
            (status, sum(item.status is status for item in self.descriptors))
            for status in CapabilitySupportStatus
        )

    def lookup(self, capability_id: str) -> CapabilityDescriptor:
        _text(capability_id, "capability_id")
        for item in self.descriptors:
            if hmac.compare_digest(item.capability_id, capability_id):
                return item
        _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "capability_id")


def encode_capability_catalog(value: object) -> bytes:
    if type(value) is not CapabilityCatalogSegment:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT)
    envelope = value._body_mapping()
    envelope["catalog_sha256"] = value.catalog_sha256
    return _canonical_json(envelope)


def _exact(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _term_from(value: object, path: str) -> CapabilityTermRef:
    item = _exact(
        value,
        {"namespace", "term_definition_sha256", "term_id", "term_ref_id", "vocabulary_version"},
        path,
    )
    return CapabilityTermRef(**item)


def _fact_from(value: object, path: str) -> CapabilityFact:
    item = _exact(
        value, {"key_term_ref_id", "unit_term_ref_id", "value", "value_term_ref_id"}, path
    )
    return CapabilityFact(**item)


def _verification_from(value: object, path: str) -> CapabilityVerificationRef | None:
    if value is None:
        return None
    item = _exact(
        value, {"receipt_sha256", "receipt_size_bytes", "verifier_id", "verifier_version"}, path
    )
    return CapabilityVerificationRef(**item)


def _external_ref_from(value: object, path: str) -> ExternalCapabilityRef:
    item = _exact(value, {"capability_id", "descriptor_sha256"}, path)
    return ExternalCapabilityRef(**item)


def _descriptor_from(value: object, path: str) -> CapabilityDescriptor:
    item = _exact(
        value,
        {
            "capability_id",
            "declaring_module_id",
            "dependency_ids",
            "execution_profiles",
            "facts",
            "kind",
            "lifecycle_stages",
            "native_identifier",
            "risk_class",
            "semantic_term_ref_ids",
            "status",
            "verification",
        },
        path,
    )
    return CapabilityDescriptor(
        capability_id=item["capability_id"],
        declaring_module_id=item["declaring_module_id"],
        dependency_ids=tuple(item["dependency_ids"])
        if type(item["dependency_ids"]) is list
        else item["dependency_ids"],
        execution_profiles=tuple(
            CapabilityExecutionProfile(value) for value in item["execution_profiles"]
        )
        if type(item["execution_profiles"]) is list
        else item["execution_profiles"],
        facts=tuple(
            _fact_from(entry, f"{path}/facts/{index}") for index, entry in enumerate(item["facts"])
        )
        if type(item["facts"]) is list
        else item["facts"],
        kind=CapabilityKind(item["kind"]),
        lifecycle_stages=tuple(
            CapabilityLifecycleStage(value) for value in item["lifecycle_stages"]
        )
        if type(item["lifecycle_stages"]) is list
        else item["lifecycle_stages"],
        native_identifier=item["native_identifier"],
        risk_class=CapabilityRiskClass(item["risk_class"]),
        semantic_term_ref_ids=tuple(item["semantic_term_ref_ids"])
        if type(item["semantic_term_ref_ids"]) is list
        else item["semantic_term_ref_ids"],
        status=CapabilitySupportStatus(item["status"]),
        verification=_verification_from(item["verification"], f"{path}/verification"),
    )


def _relation_from(value: object, path: str) -> CapabilityRelation:
    item = _exact(
        value,
        {
            "facts",
            "relation_id",
            "relation_term_ref_id",
            "source_capability_id",
            "target_capability_ids",
        },
        path,
    )
    return CapabilityRelation(
        facts=tuple(
            _fact_from(entry, f"{path}/facts/{index}") for index, entry in enumerate(item["facts"])
        )
        if type(item["facts"]) is list
        else item["facts"],
        relation_id=item["relation_id"],
        relation_term_ref_id=item["relation_term_ref_id"],
        source_capability_id=item["source_capability_id"],
        target_capability_ids=tuple(item["target_capability_ids"])
        if type(item["target_capability_ids"]) is list
        else item["target_capability_ids"],
    )


def decode_capability_catalog(raw: object) -> CapabilityCatalogSegment:
    value = _decode_json(raw, maximum=MAX_CAPABILITY_CATALOG_BYTES)
    item = _exact(
        value,
        {
            "backend",
            "catalog_sha256",
            "descriptors",
            "discovery_algorithm_id",
            "discovery_algorithm_version",
            "discovery_receipt_sha256",
            "external_refs",
            "relations",
            "schema_version",
            "segment_id",
            "terms",
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
        result = CapabilityCatalogSegment(
            schema_version=item["schema_version"],
            segment_id=item["segment_id"],
            backend=CapabilityBackend(
                backend_id=backend["backend_id"],
                backend_version=tuple(backend["backend_version"])
                if type(backend["backend_version"]) is list
                else backend["backend_version"],
                build_fingerprint_sha256=backend["build_fingerprint_sha256"],
                platform_id=backend["platform_id"],
                discovery_profile=CapabilityExecutionProfile(backend["discovery_profile"]),
            ),
            discovery_receipt_sha256=item["discovery_receipt_sha256"],
            discovery_algorithm_id=item["discovery_algorithm_id"],
            discovery_algorithm_version=item["discovery_algorithm_version"],
            external_refs=tuple(
                _external_ref_from(entry, f"external_refs/{index}")
                for index, entry in enumerate(item["external_refs"])
            )
            if type(item["external_refs"]) is list
            else item["external_refs"],
            terms=tuple(
                _term_from(entry, f"terms/{index}") for index, entry in enumerate(item["terms"])
            )
            if type(item["terms"]) is list
            else item["terms"],
            descriptors=tuple(
                _descriptor_from(entry, f"descriptors/{index}")
                for index, entry in enumerate(item["descriptors"])
            )
            if type(item["descriptors"]) is list
            else item["descriptors"],
            relations=tuple(
                _relation_from(entry, f"relations/{index}")
                for index, entry in enumerate(item["relations"])
            )
            if type(item["relations"]) is list
            else item["relations"],
        )
    except ValueError as exc:
        if isinstance(exc, CapabilityCatalogError):
            raise
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT)
    if not hmac.compare_digest(
        result.catalog_sha256, _digest(item["catalog_sha256"], "catalog_sha256")
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "catalog_sha256")
    return result


__all__ = ()
