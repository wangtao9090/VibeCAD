"""Open, content-addressed vocabulary for backend-neutral sketch intent.

The vocabulary is deliberately data only.  A term definition cannot name a
Python callable, CAD command, native object type, or feature implementation.
Adapters may execute a node only after matching the exact term-definition
digest and its typed anchor/property signature.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

SKETCH_ONTOLOGY_SCHEMA_VERSION = 1
MAX_SKETCH_ONTOLOGY_TERMS = 512
MAX_SKETCH_ANCHOR_SLOTS_PER_TERM = 16
MAX_SKETCH_PROPERTY_SIGNATURES_PER_TERM = 64
MAX_SKETCH_VALUE_KINDS_PER_PROPERTY = 8
MAX_SKETCH_UNITS_PER_PROPERTY = 16
MAX_SKETCH_ROLES_PER_ANCHOR_SLOT = 32
MAX_SKETCH_TARGET_KINDS_PER_ANCHOR_SLOT = 3
MAX_SKETCH_ONTOLOGY_BYTES = 512 * 1024

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,127}$")
_TERM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,191}$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){0,3}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_DEFINITION_DOMAIN = b"vibecad-sketch-ontology-definition-v1\0"
_CATALOG_DOMAIN = b"vibecad-sketch-ontology-catalog-v1\0"


class SketchOntologyErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_VERSION = "unsupported_version"
    BUDGET_EXCEEDED = "budget_exceeded"
    DUPLICATE_ID = "duplicate_id"
    UNKNOWN_REFERENCE = "unknown_reference"
    INTEGRITY_FAILURE = "integrity_failure"


class SketchOntologyError(ValueError):
    """Bounded, non-reflective ontology failure."""

    def __init__(self, code: SketchOntologyErrorCode, path: str = "") -> None:
        if type(code) is not SketchOntologyErrorCode:
            raise TypeError("code must be an exact SketchOntologyErrorCode")
        if type(path) is not str or len(path.encode("utf-8")) > 256:
            raise ValueError("path must be bounded")
        self.code = code
        self.path = path
        super().__init__(code.value)


def _fail(code: SketchOntologyErrorCode, path: str = "") -> None:
    raise SketchOntologyError(code, path)


def _identifier(value: object, path: str, *, term: bool = False) -> str:
    pattern = _TERM if term else _IDENTIFIER
    if type(value) is not str or pattern.fullmatch(value) is None:
        _fail(SketchOntologyErrorCode.INVALID_INPUT, path)
    return value


def _version(value: object, path: str) -> str:
    if type(value) is not str or _VERSION.fullmatch(value) is None:
        _fail(SketchOntologyErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(SketchOntologyErrorCode.INVALID_INPUT, path)
    return value


def _canonical(value: object, *, maximum: int) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(SketchOntologyErrorCode.INVALID_INPUT)
    if not raw or len(raw) > maximum:
        _fail(SketchOntologyErrorCode.BUDGET_EXCEEDED)
    return raw


class SketchTermKind(StrEnum):
    GEOMETRY = "geometry"
    CONSTRAINT = "constraint"
    ANCHOR_ROLE = "anchor_role"
    PROPERTY = "property"
    UNIT = "unit"


class SketchAnchorTargetKind(StrEnum):
    SKETCH = "sketch"
    GEOMETRY = "geometry"
    EXTERNAL = "external"


class SketchValueKind(StrEnum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    TEXT = "text"
    VECTOR = "vector"
    TERM_REF = "term_ref"
    ELEMENT_REF = "element_ref"


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchOntologyTermRef:
    term_ref_id: str
    namespace: str
    vocabulary_version: str
    term_id: str
    term_definition_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.term_ref_id, "term_ref_id")
        _identifier(self.namespace, "namespace")
        _version(self.vocabulary_version, "vocabulary_version")
        _identifier(self.term_id, "term_id", term=True)
        _digest(self.term_definition_sha256, "term_definition_sha256")

    def to_mapping(self) -> dict[str, str]:
        return {
            "term_ref_id": self.term_ref_id,
            "namespace": self.namespace,
            "vocabulary_version": self.vocabulary_version,
            "term_id": self.term_id,
            "term_definition_sha256": self.term_definition_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> SketchOntologyTermRef:
        if type(value) is not dict or set(value) != {
            "term_ref_id",
            "namespace",
            "vocabulary_version",
            "term_id",
            "term_definition_sha256",
        }:
            _fail(SketchOntologyErrorCode.INVALID_INPUT)
        return cls(
            term_ref_id=value["term_ref_id"],
            namespace=value["namespace"],
            vocabulary_version=value["vocabulary_version"],
            term_id=value["term_id"],
            term_definition_sha256=value["term_definition_sha256"],
        )


def _enum_tuple[EnumT: StrEnum](
    value: object,
    enum_type: type[EnumT],
    path: str,
    *,
    maximum: int,
) -> tuple[EnumT, ...]:
    if type(value) is not tuple or not value or len(value) > maximum:
        _fail(
            SketchOntologyErrorCode.BUDGET_EXCEEDED
            if type(value) is tuple and len(value) > maximum
            else SketchOntologyErrorCode.INVALID_INPUT,
            path,
        )
    if not all(type(item) is enum_type for item in value):
        _fail(SketchOntologyErrorCode.INVALID_INPUT, path)
    result = tuple(sorted(set(value), key=lambda item: item.value))
    if len(result) != len(value):
        _fail(SketchOntologyErrorCode.DUPLICATE_ID, path)
    return result


def _identifier_tuple(
    value: object,
    path: str,
    *,
    maximum: int,
    required: bool = False,
) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > maximum or (required and not value):
        _fail(
            SketchOntologyErrorCode.BUDGET_EXCEEDED
            if type(value) is tuple and len(value) > maximum
            else SketchOntologyErrorCode.INVALID_INPUT,
            path,
        )
    result = tuple(_identifier(item, path) for item in value)
    if len(set(result)) != len(result):
        _fail(SketchOntologyErrorCode.DUPLICATE_ID, path)
    return tuple(sorted(result))


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchAnchorSlotSignature:
    """One ordered anchor slot; only the final slot may repeat."""

    slot_id: str
    target_kinds: tuple[SketchAnchorTargetKind, ...]
    role_term_ref_ids: tuple[str, ...]
    minimum_occurrences: int = 1
    maximum_occurrences: int = 1

    def __post_init__(self) -> None:
        _identifier(self.slot_id, "slot_id")
        object.__setattr__(
            self,
            "target_kinds",
            _enum_tuple(
                self.target_kinds,
                SketchAnchorTargetKind,
                "target_kinds",
                maximum=MAX_SKETCH_TARGET_KINDS_PER_ANCHOR_SLOT,
            ),
        )
        object.__setattr__(
            self,
            "role_term_ref_ids",
            _identifier_tuple(
                self.role_term_ref_ids,
                "role_term_ref_ids",
                maximum=MAX_SKETCH_ROLES_PER_ANCHOR_SLOT,
                required=True,
            ),
        )
        if (
            type(self.minimum_occurrences) is not int
            or type(self.maximum_occurrences) is not int
            or not 0 <= self.minimum_occurrences <= self.maximum_occurrences <= 16
            or self.maximum_occurrences == 0
        ):
            _fail(SketchOntologyErrorCode.INVALID_INPUT, "occurrences")

    def to_mapping(self) -> dict[str, object]:
        return {
            "slot_id": self.slot_id,
            "target_kinds": [item.value for item in self.target_kinds],
            "role_term_ref_ids": list(self.role_term_ref_ids),
            "minimum_occurrences": self.minimum_occurrences,
            "maximum_occurrences": self.maximum_occurrences,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchPropertySignature:
    property_term_ref_id: str
    value_kinds: tuple[SketchValueKind, ...]
    unit_term_ref_ids: tuple[str, ...] = ()
    required: bool = True

    def __post_init__(self) -> None:
        _identifier(self.property_term_ref_id, "property_term_ref_id")
        object.__setattr__(
            self,
            "value_kinds",
            _enum_tuple(
                self.value_kinds,
                SketchValueKind,
                "value_kinds",
                maximum=MAX_SKETCH_VALUE_KINDS_PER_PROPERTY,
            ),
        )
        object.__setattr__(
            self,
            "unit_term_ref_ids",
            _identifier_tuple(
                self.unit_term_ref_ids,
                "unit_term_ref_ids",
                maximum=MAX_SKETCH_UNITS_PER_PROPERTY,
            ),
        )
        if type(self.required) is not bool:
            _fail(SketchOntologyErrorCode.INVALID_INPUT, "required")

    def to_mapping(self) -> dict[str, object]:
        return {
            "property_term_ref_id": self.property_term_ref_id,
            "value_kinds": [item.value for item in self.value_kinds],
            "unit_term_ref_ids": list(self.unit_term_ref_ids),
            "required": self.required,
        }


def _definition_body(
    *,
    namespace: str,
    vocabulary_version: str,
    term_id: str,
    kind: SketchTermKind,
    anchor_slots: tuple[SketchAnchorSlotSignature, ...],
    properties: tuple[SketchPropertySignature, ...],
) -> dict[str, object]:
    return {
        "namespace": namespace,
        "vocabulary_version": vocabulary_version,
        "term_id": term_id,
        "kind": kind.value,
        "anchor_slots": [item.to_mapping() for item in anchor_slots],
        "properties": [item.to_mapping() for item in properties],
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchOntologyTermDefinition:
    reference: SketchOntologyTermRef
    kind: SketchTermKind
    anchor_slots: tuple[SketchAnchorSlotSignature, ...] = ()
    properties: tuple[SketchPropertySignature, ...] = ()

    def __post_init__(self) -> None:
        if type(self.reference) is not SketchOntologyTermRef:
            _fail(SketchOntologyErrorCode.INVALID_INPUT, "reference")
        if type(self.kind) is not SketchTermKind:
            _fail(SketchOntologyErrorCode.INVALID_INPUT, "kind")
        if (
            type(self.anchor_slots) is not tuple
            or len(self.anchor_slots) > MAX_SKETCH_ANCHOR_SLOTS_PER_TERM
            or not all(type(item) is SketchAnchorSlotSignature for item in self.anchor_slots)
        ):
            _fail(
                SketchOntologyErrorCode.BUDGET_EXCEEDED
                if type(self.anchor_slots) is tuple
                and len(self.anchor_slots) > MAX_SKETCH_ANCHOR_SLOTS_PER_TERM
                else SketchOntologyErrorCode.INVALID_INPUT,
                "anchor_slots",
            )
        if (
            type(self.properties) is not tuple
            or len(self.properties) > MAX_SKETCH_PROPERTY_SIGNATURES_PER_TERM
            or not all(type(item) is SketchPropertySignature for item in self.properties)
        ):
            _fail(
                SketchOntologyErrorCode.BUDGET_EXCEEDED
                if type(self.properties) is tuple
                and len(self.properties) > MAX_SKETCH_PROPERTY_SIGNATURES_PER_TERM
                else SketchOntologyErrorCode.INVALID_INPUT,
                "properties",
            )
        slot_ids = tuple(item.slot_id for item in self.anchor_slots)
        property_ids = tuple(item.property_term_ref_id for item in self.properties)
        if len(set(slot_ids)) != len(slot_ids) or len(set(property_ids)) != len(property_ids):
            _fail(SketchOntologyErrorCode.DUPLICATE_ID)
        if any(
            item.minimum_occurrences != 1 or item.maximum_occurrences != 1
            for item in self.anchor_slots[:-1]
        ):
            _fail(SketchOntologyErrorCode.INVALID_INPUT, "anchor_slots")
        if self.kind not in {SketchTermKind.GEOMETRY, SketchTermKind.CONSTRAINT} and (
            self.anchor_slots or self.properties
        ):
            _fail(SketchOntologyErrorCode.INVALID_INPUT, "kind")
        object.__setattr__(
            self,
            "properties",
            tuple(sorted(self.properties, key=lambda item: item.property_term_ref_id)),
        )
        expected = hashlib.sha256(
            _DEFINITION_DOMAIN
            + _canonical(
                _definition_body(
                    namespace=self.reference.namespace,
                    vocabulary_version=self.reference.vocabulary_version,
                    term_id=self.reference.term_id,
                    kind=self.kind,
                    anchor_slots=self.anchor_slots,
                    properties=self.properties,
                ),
                maximum=128 * 1024,
            )
        ).hexdigest()
        if expected != self.reference.term_definition_sha256:
            _fail(SketchOntologyErrorCode.INTEGRITY_FAILURE, "reference")

    def to_mapping(self) -> dict[str, object]:
        return {
            "reference": self.reference.to_mapping(),
            "kind": self.kind.value,
            "anchor_slots": [item.to_mapping() for item in self.anchor_slots],
            "properties": [item.to_mapping() for item in self.properties],
        }


def define_sketch_term(
    *,
    term_ref_id: str,
    namespace: str,
    vocabulary_version: str,
    term_id: str,
    kind: SketchTermKind,
    anchor_slots: tuple[SketchAnchorSlotSignature, ...] = (),
    properties: tuple[SketchPropertySignature, ...] = (),
) -> SketchOntologyTermDefinition:
    """Create one self-authenticating ontology term definition."""

    _identifier(term_ref_id, "term_ref_id")
    _identifier(namespace, "namespace")
    _version(vocabulary_version, "vocabulary_version")
    _identifier(term_id, "term_id", term=True)
    if type(kind) is not SketchTermKind:
        _fail(SketchOntologyErrorCode.INVALID_INPUT, "kind")
    if type(anchor_slots) is not tuple or type(properties) is not tuple:
        _fail(SketchOntologyErrorCode.INVALID_INPUT)
    if (
        len(anchor_slots) > MAX_SKETCH_ANCHOR_SLOTS_PER_TERM
        or len(properties) > MAX_SKETCH_PROPERTY_SIGNATURES_PER_TERM
    ):
        _fail(SketchOntologyErrorCode.BUDGET_EXCEEDED)
    if not all(type(item) is SketchAnchorSlotSignature for item in anchor_slots) or not all(
        type(item) is SketchPropertySignature for item in properties
    ):
        _fail(SketchOntologyErrorCode.INVALID_INPUT)
    ordered_properties = tuple(sorted(properties, key=lambda item: item.property_term_ref_id))
    digest = hashlib.sha256(
        _DEFINITION_DOMAIN
        + _canonical(
            _definition_body(
                namespace=namespace,
                vocabulary_version=vocabulary_version,
                term_id=term_id,
                kind=kind,
                anchor_slots=anchor_slots,
                properties=ordered_properties,
            ),
            maximum=128 * 1024,
        )
    ).hexdigest()
    return SketchOntologyTermDefinition(
        reference=SketchOntologyTermRef(
            term_ref_id=term_ref_id,
            namespace=namespace,
            vocabulary_version=vocabulary_version,
            term_id=term_id,
            term_definition_sha256=digest,
        ),
        kind=kind,
        anchor_slots=anchor_slots,
        properties=ordered_properties,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchOntologyCatalog:
    schema_version: int
    ontology_id: str
    terms: tuple[SketchOntologyTermDefinition, ...]

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != SKETCH_ONTOLOGY_SCHEMA_VERSION
        ):
            _fail(SketchOntologyErrorCode.UNSUPPORTED_VERSION, "schema_version")
        _identifier(self.ontology_id, "ontology_id")
        if (
            type(self.terms) is not tuple
            or len(self.terms) > MAX_SKETCH_ONTOLOGY_TERMS
            or not all(type(item) is SketchOntologyTermDefinition for item in self.terms)
        ):
            _fail(
                SketchOntologyErrorCode.BUDGET_EXCEEDED
                if type(self.terms) is tuple and len(self.terms) > MAX_SKETCH_ONTOLOGY_TERMS
                else SketchOntologyErrorCode.INVALID_INPUT,
                "terms",
            )
        ordered = tuple(sorted(self.terms, key=lambda item: item.reference.term_ref_id))
        ids = tuple(item.reference.term_ref_id for item in ordered)
        if len(set(ids)) != len(ids):
            _fail(SketchOntologyErrorCode.DUPLICATE_ID, "terms")
        by_id = {item.reference.term_ref_id: item for item in ordered}
        for index, definition in enumerate(ordered):
            for slot in definition.anchor_slots:
                for role_id in slot.role_term_ref_ids:
                    role = by_id.get(role_id)
                    if role is None:
                        _fail(
                            SketchOntologyErrorCode.UNKNOWN_REFERENCE,
                            f"terms/{index}/anchor_slots",
                        )
                    if role.kind is not SketchTermKind.ANCHOR_ROLE:
                        _fail(SketchOntologyErrorCode.INVALID_INPUT, f"terms/{index}/anchor_slots")
            for signature in definition.properties:
                prop = by_id.get(signature.property_term_ref_id)
                if prop is None:
                    _fail(SketchOntologyErrorCode.UNKNOWN_REFERENCE, f"terms/{index}/properties")
                if prop.kind is not SketchTermKind.PROPERTY:
                    _fail(SketchOntologyErrorCode.INVALID_INPUT, f"terms/{index}/properties")
                for unit_id in signature.unit_term_ref_ids:
                    unit = by_id.get(unit_id)
                    if unit is None:
                        _fail(
                            SketchOntologyErrorCode.UNKNOWN_REFERENCE,
                            f"terms/{index}/properties",
                        )
                    if unit.kind is not SketchTermKind.UNIT:
                        _fail(SketchOntologyErrorCode.INVALID_INPUT, f"terms/{index}/properties")
        object.__setattr__(self, "terms", ordered)
        _canonical(self.to_mapping(), maximum=MAX_SKETCH_ONTOLOGY_BYTES)

    @property
    def by_id(self) -> dict[str, SketchOntologyTermDefinition]:
        return {item.reference.term_ref_id: item for item in self.terms}

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "ontology_id": self.ontology_id,
            "terms": [item.to_mapping() for item in self.terms],
        }

    @property
    def catalog_sha256(self) -> str:
        return hashlib.sha256(
            _CATALOG_DOMAIN + _canonical(self.to_mapping(), maximum=MAX_SKETCH_ONTOLOGY_BYTES)
        ).hexdigest()


__all__ = [
    "MAX_SKETCH_ANCHOR_SLOTS_PER_TERM",
    "MAX_SKETCH_ONTOLOGY_BYTES",
    "MAX_SKETCH_ONTOLOGY_TERMS",
    "MAX_SKETCH_PROPERTY_SIGNATURES_PER_TERM",
    "SKETCH_ONTOLOGY_SCHEMA_VERSION",
    "SketchAnchorSlotSignature",
    "SketchAnchorTargetKind",
    "SketchOntologyCatalog",
    "SketchOntologyError",
    "SketchOntologyErrorCode",
    "SketchOntologyTermDefinition",
    "SketchOntologyTermRef",
    "SketchPropertySignature",
    "SketchTermKind",
    "SketchValueKind",
    "define_sketch_term",
]
