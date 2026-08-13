"""Immutable backend-neutral graph for editable sketch intent.

This contract describes geometry, stable anchors, and constraints without
choosing a CAD backend.  Unknown ontology terms remain serializable but inert;
only an exact trusted ontology signature may classify a node as executable.

Recipes and visual hypotheses intentionally stay outside this first contract:
recipes normalize into ordinary graph nodes, while hypotheses remain in the
evidence layer until one alternative is adopted as design intent.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum

from vibecad.sketch.ontology import (
    SketchAnchorSlotSignature,
    SketchAnchorTargetKind,
    SketchOntologyCatalog,
    SketchOntologyTermDefinition,
    SketchOntologyTermRef,
    SketchTermKind,
    SketchValueKind,
)

SKETCH_INTENT_SCHEMA_VERSION = 1
MAX_SKETCH_INTENT_TERMS = 512
MAX_SKETCH_INTENT_GEOMETRIES = 256
MAX_SKETCH_INTENT_ANCHORS = 2_048
MAX_SKETCH_INTENT_CONSTRAINTS = 512
MAX_SKETCH_PROPERTIES_PER_NODE = 64
MAX_SKETCH_ANCHORS_PER_NODE = 16
MAX_SKETCH_VECTOR_COMPONENTS = 16
MAX_SKETCH_TEXT_BYTES = 1_024
MAX_SKETCH_INTENT_BYTES = 512 * 1024

_SAFE_INTEGER = 2**53 - 1
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,127}$")
_GRAPH_DIGEST_DOMAIN = b"vibecad-sketch-intent-graph-v1\0"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class SketchIntentErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_VERSION = "unsupported_version"
    BUDGET_EXCEEDED = "budget_exceeded"
    DUPLICATE_ID = "duplicate_id"
    UNKNOWN_REFERENCE = "unknown_reference"
    BINDING_MISMATCH = "binding_mismatch"
    INTEGRITY_FAILURE = "integrity_failure"


class SketchIntentError(ValueError):
    """Bounded, non-reflective sketch-intent failure."""

    def __init__(self, code: SketchIntentErrorCode, path: str = "") -> None:
        if type(code) is not SketchIntentErrorCode:
            raise TypeError("code must be an exact SketchIntentErrorCode")
        if type(path) is not str or len(path.encode("utf-8")) > 256:
            raise ValueError("path must be bounded")
        self.code = code
        self.path = path
        super().__init__(code.value)


def _fail(code: SketchIntentErrorCode, path: str = "") -> None:
    raise SketchIntentError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(SketchIntentErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(SketchIntentErrorCode.INVALID_INPUT, path)
    return value


def _number(value: object, path: str) -> int | float:
    if type(value) not in {int, float}:
        _fail(SketchIntentErrorCode.INVALID_INPUT, path)
    if type(value) is int:
        if abs(value) > _SAFE_INTEGER:
            _fail(SketchIntentErrorCode.INVALID_INPUT, path)
        return value
    if not math.isfinite(value):
        _fail(SketchIntentErrorCode.INVALID_INPUT, path)
    if value == 0:
        return 0
    if value.is_integer() and abs(value) <= _SAFE_INTEGER:
        return int(value)
    return value


def _canonical(value: object, *, maximum: int = MAX_SKETCH_INTENT_BYTES) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(SketchIntentErrorCode.INVALID_INPUT)
    if not raw or len(raw) > maximum:
        _fail(SketchIntentErrorCode.BUDGET_EXCEEDED)
    return raw


def _strict_mapping(value: object, keys: set[str], path: str = "") -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _fail(SketchIntentErrorCode.INVALID_INPUT, path)
    return value


def _sequence(value: object, path: str, *, maximum: int) -> list[object]:
    if type(value) is not list:
        _fail(SketchIntentErrorCode.INVALID_INPUT, path)
    if len(value) > maximum:
        _fail(SketchIntentErrorCode.BUDGET_EXCEEDED, path)
    return value


def _ordered_objects[ItemT](
    value: object,
    item_type: type[ItemT],
    path: str,
    *,
    maximum: int,
    key,
) -> tuple[ItemT, ...]:
    if type(value) is not tuple or not all(type(item) is item_type for item in value):
        _fail(SketchIntentErrorCode.INVALID_INPUT, path)
    if len(value) > maximum:
        _fail(SketchIntentErrorCode.BUDGET_EXCEEDED, path)
    ids = tuple(key(item) for item in value)
    if len(set(ids)) != len(ids):
        _fail(SketchIntentErrorCode.DUPLICATE_ID, path)
    return tuple(sorted(value, key=key))


class SketchConstraintMode(StrEnum):
    DRIVING = "driving"
    REFERENCE = "reference"


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchProperty:
    property_term_ref_id: str
    value_kind: SketchValueKind
    value: bool | int | float | str | tuple[int | float, ...]
    unit_term_ref_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.property_term_ref_id, "property_term_ref_id")
        if type(self.value_kind) is not SketchValueKind:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "value_kind")
        value = self.value
        if self.value_kind is SketchValueKind.BOOLEAN:
            if type(value) is not bool:
                _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
        elif self.value_kind is SketchValueKind.INTEGER:
            if type(value) is not int or abs(value) > _SAFE_INTEGER:
                _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
        elif self.value_kind is SketchValueKind.NUMBER:
            object.__setattr__(self, "value", _number(value, "value"))
        elif self.value_kind is SketchValueKind.TEXT:
            if type(value) is not str:
                _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
            try:
                size = len(value.encode("utf-8"))
            except UnicodeError:
                _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
            if not value or size > MAX_SKETCH_TEXT_BYTES or not value.isprintable():
                _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
        elif self.value_kind is SketchValueKind.VECTOR:
            if type(value) is not tuple or not 1 <= len(value) <= MAX_SKETCH_VECTOR_COMPONENTS:
                _fail(
                    SketchIntentErrorCode.BUDGET_EXCEEDED
                    if type(value) is tuple and len(value) > MAX_SKETCH_VECTOR_COMPONENTS
                    else SketchIntentErrorCode.INVALID_INPUT,
                    "value",
                )
            object.__setattr__(
                self,
                "value",
                tuple(_number(item, "value") for item in value),
            )
        else:
            _identifier(value, "value")
        if self.unit_term_ref_id is not None:
            _identifier(self.unit_term_ref_id, "unit_term_ref_id")

    def to_mapping(self) -> dict[str, object]:
        return {
            "property_term_ref_id": self.property_term_ref_id,
            "value_kind": self.value_kind.value,
            "value": list(self.value) if type(self.value) is tuple else self.value,
            "unit_term_ref_id": self.unit_term_ref_id,
        }

    @classmethod
    def from_mapping(cls, value: object) -> SketchProperty:
        item = _strict_mapping(
            value,
            {"property_term_ref_id", "value_kind", "value", "unit_term_ref_id"},
        )
        try:
            kind = SketchValueKind(item["value_kind"])
        except (TypeError, ValueError):
            _fail(SketchIntentErrorCode.INVALID_INPUT, "value_kind")
        raw_value = item["value"]
        if kind is SketchValueKind.VECTOR:
            raw_value = tuple(_sequence(raw_value, "value", maximum=MAX_SKETCH_VECTOR_COMPONENTS))
        return cls(
            property_term_ref_id=item["property_term_ref_id"],
            value_kind=kind,
            value=raw_value,
            unit_term_ref_id=item["unit_term_ref_id"],
        )


def _properties(value: object, path: str) -> tuple[SketchProperty, ...]:
    return _ordered_objects(
        value,
        SketchProperty,
        path,
        maximum=MAX_SKETCH_PROPERTIES_PER_NODE,
        key=lambda item: item.property_term_ref_id,
    )


def _anchor_ids(value: object, path: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        _fail(SketchIntentErrorCode.INVALID_INPUT, path)
    if len(value) > MAX_SKETCH_ANCHORS_PER_NODE:
        _fail(SketchIntentErrorCode.BUDGET_EXCEEDED, path)
    result = tuple(_identifier(item, path) for item in value)
    if len(set(result)) != len(result):
        _fail(SketchIntentErrorCode.DUPLICATE_ID, path)
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchAnchor:
    anchor_id: str
    target_kind: SketchAnchorTargetKind
    target_id: str
    role_term_ref_id: str
    ordinal: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.anchor_id, "anchor_id")
        if type(self.target_kind) is not SketchAnchorTargetKind:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "target_kind")
        _identifier(self.target_id, "target_id")
        _identifier(self.role_term_ref_id, "role_term_ref_id")
        if self.ordinal is not None and (
            type(self.ordinal) is not int or not 0 <= self.ordinal <= _SAFE_INTEGER
        ):
            _fail(SketchIntentErrorCode.INVALID_INPUT, "ordinal")

    def to_mapping(self) -> dict[str, object]:
        return {
            "anchor_id": self.anchor_id,
            "target_kind": self.target_kind.value,
            "target_id": self.target_id,
            "role_term_ref_id": self.role_term_ref_id,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_mapping(cls, value: object) -> SketchAnchor:
        item = _strict_mapping(
            value,
            {"anchor_id", "target_kind", "target_id", "role_term_ref_id", "ordinal"},
        )
        try:
            target_kind = SketchAnchorTargetKind(item["target_kind"])
        except (TypeError, ValueError):
            _fail(SketchIntentErrorCode.INVALID_INPUT, "target_kind")
        return cls(
            anchor_id=item["anchor_id"],
            target_kind=target_kind,
            target_id=item["target_id"],
            role_term_ref_id=item["role_term_ref_id"],
            ordinal=item["ordinal"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchGeometryNode:
    geometry_id: str
    geometry_term_ref_id: str
    properties: tuple[SketchProperty, ...] = ()
    anchor_ids: tuple[str, ...] = ()
    construction: bool = False

    def __post_init__(self) -> None:
        _identifier(self.geometry_id, "geometry_id")
        _identifier(self.geometry_term_ref_id, "geometry_term_ref_id")
        object.__setattr__(self, "properties", _properties(self.properties, "properties"))
        object.__setattr__(self, "anchor_ids", _anchor_ids(self.anchor_ids, "anchor_ids"))
        if type(self.construction) is not bool:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "construction")

    def to_mapping(self) -> dict[str, object]:
        return {
            "geometry_id": self.geometry_id,
            "geometry_term_ref_id": self.geometry_term_ref_id,
            "properties": [item.to_mapping() for item in self.properties],
            "anchor_ids": list(self.anchor_ids),
            "construction": self.construction,
        }

    @classmethod
    def from_mapping(cls, value: object) -> SketchGeometryNode:
        item = _strict_mapping(
            value,
            {
                "geometry_id",
                "geometry_term_ref_id",
                "properties",
                "anchor_ids",
                "construction",
            },
        )
        return cls(
            geometry_id=item["geometry_id"],
            geometry_term_ref_id=item["geometry_term_ref_id"],
            properties=tuple(
                SketchProperty.from_mapping(raw)
                for raw in _sequence(
                    item["properties"],
                    "properties",
                    maximum=MAX_SKETCH_PROPERTIES_PER_NODE,
                )
            ),
            anchor_ids=tuple(
                _sequence(
                    item["anchor_ids"],
                    "anchor_ids",
                    maximum=MAX_SKETCH_ANCHORS_PER_NODE,
                )
            ),
            construction=item["construction"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchConstraintNode:
    constraint_id: str
    constraint_term_ref_id: str
    anchor_ids: tuple[str, ...]
    properties: tuple[SketchProperty, ...] = ()
    mode: SketchConstraintMode = SketchConstraintMode.DRIVING
    enabled: bool = True

    def __post_init__(self) -> None:
        _identifier(self.constraint_id, "constraint_id")
        _identifier(self.constraint_term_ref_id, "constraint_term_ref_id")
        object.__setattr__(self, "anchor_ids", _anchor_ids(self.anchor_ids, "anchor_ids"))
        object.__setattr__(self, "properties", _properties(self.properties, "properties"))
        if type(self.mode) is not SketchConstraintMode:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "mode")
        if type(self.enabled) is not bool:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "enabled")

    def to_mapping(self) -> dict[str, object]:
        return {
            "constraint_id": self.constraint_id,
            "constraint_term_ref_id": self.constraint_term_ref_id,
            "anchor_ids": list(self.anchor_ids),
            "properties": [item.to_mapping() for item in self.properties],
            "mode": self.mode.value,
            "enabled": self.enabled,
        }

    @classmethod
    def from_mapping(cls, value: object) -> SketchConstraintNode:
        item = _strict_mapping(
            value,
            {
                "constraint_id",
                "constraint_term_ref_id",
                "anchor_ids",
                "properties",
                "mode",
                "enabled",
            },
        )
        try:
            mode = SketchConstraintMode(item["mode"])
        except (TypeError, ValueError):
            _fail(SketchIntentErrorCode.INVALID_INPUT, "mode")
        return cls(
            constraint_id=item["constraint_id"],
            constraint_term_ref_id=item["constraint_term_ref_id"],
            anchor_ids=tuple(
                _sequence(
                    item["anchor_ids"],
                    "anchor_ids",
                    maximum=MAX_SKETCH_ANCHORS_PER_NODE,
                )
            ),
            properties=tuple(
                SketchProperty.from_mapping(raw)
                for raw in _sequence(
                    item["properties"],
                    "properties",
                    maximum=MAX_SKETCH_PROPERTIES_PER_NODE,
                )
            ),
            mode=mode,
            enabled=item["enabled"],
        )


def _term_ref_from_mapping(value: object) -> SketchOntologyTermRef:
    try:
        return SketchOntologyTermRef.from_mapping(value)
    except Exception:
        _fail(SketchIntentErrorCode.INVALID_INPUT, "terms")


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchIntentGraph:
    schema_version: int
    graph_id: str
    sketch_id: str
    terms: tuple[SketchOntologyTermRef, ...]
    geometries: tuple[SketchGeometryNode, ...]
    anchors: tuple[SketchAnchor, ...]
    constraints: tuple[SketchConstraintNode, ...]

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != SKETCH_INTENT_SCHEMA_VERSION
        ):
            _fail(SketchIntentErrorCode.UNSUPPORTED_VERSION, "schema_version")
        _identifier(self.graph_id, "graph_id")
        _identifier(self.sketch_id, "sketch_id")
        terms = _ordered_objects(
            self.terms,
            SketchOntologyTermRef,
            "terms",
            maximum=MAX_SKETCH_INTENT_TERMS,
            key=lambda item: item.term_ref_id,
        )
        geometries = _ordered_objects(
            self.geometries,
            SketchGeometryNode,
            "geometries",
            maximum=MAX_SKETCH_INTENT_GEOMETRIES,
            key=lambda item: item.geometry_id,
        )
        anchors = _ordered_objects(
            self.anchors,
            SketchAnchor,
            "anchors",
            maximum=MAX_SKETCH_INTENT_ANCHORS,
            key=lambda item: item.anchor_id,
        )
        constraints = _ordered_objects(
            self.constraints,
            SketchConstraintNode,
            "constraints",
            maximum=MAX_SKETCH_INTENT_CONSTRAINTS,
            key=lambda item: item.constraint_id,
        )
        element_ids = (
            {self.graph_id, self.sketch_id}
            | {item.geometry_id for item in geometries}
            | {item.anchor_id for item in anchors}
            | {item.constraint_id for item in constraints}
        )
        expected_count = 2 + len(geometries) + len(anchors) + len(constraints)
        if len(element_ids) != expected_count:
            _fail(SketchIntentErrorCode.DUPLICATE_ID)
        term_ids = {item.term_ref_id for item in terms}
        refs = {
            *(item.geometry_term_ref_id for item in geometries),
            *(item.constraint_term_ref_id for item in constraints),
            *(item.role_term_ref_id for item in anchors),
        }
        for node in (*geometries, *constraints):
            refs.update(item.property_term_ref_id for item in node.properties)
            refs.update(
                item.unit_term_ref_id
                for item in node.properties
                if item.unit_term_ref_id is not None
            )
            refs.update(
                item.value
                for item in node.properties
                if item.value_kind is SketchValueKind.TERM_REF
            )
        if not refs <= term_ids:
            _fail(SketchIntentErrorCode.UNKNOWN_REFERENCE, "terms")
        geometry_ids = {item.geometry_id for item in geometries}
        anchor_ids = {item.anchor_id for item in anchors}
        for index, anchor in enumerate(anchors):
            if anchor.target_kind is SketchAnchorTargetKind.SKETCH:
                valid_target = anchor.target_id == self.sketch_id
            elif anchor.target_kind is SketchAnchorTargetKind.GEOMETRY:
                valid_target = anchor.target_id in geometry_ids
            else:
                valid_target = True
            if not valid_target:
                _fail(SketchIntentErrorCode.UNKNOWN_REFERENCE, f"anchors/{index}/target_id")
        for collection_name, nodes in (("geometries", geometries), ("constraints", constraints)):
            for index, node in enumerate(nodes):
                if not set(node.anchor_ids) <= anchor_ids:
                    _fail(
                        SketchIntentErrorCode.UNKNOWN_REFERENCE,
                        f"{collection_name}/{index}/anchor_ids",
                    )
        object.__setattr__(self, "terms", terms)
        object.__setattr__(self, "geometries", geometries)
        object.__setattr__(self, "anchors", anchors)
        object.__setattr__(self, "constraints", constraints)
        _canonical(self.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "graph_id": self.graph_id,
            "sketch_id": self.sketch_id,
            "terms": [item.to_mapping() for item in self.terms],
            "geometries": [item.to_mapping() for item in self.geometries],
            "anchors": [item.to_mapping() for item in self.anchors],
            "constraints": [item.to_mapping() for item in self.constraints],
        }

    @property
    def graph_sha256(self) -> str:
        return hashlib.sha256(_GRAPH_DIGEST_DOMAIN + _canonical(self.to_mapping())).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> SketchIntentGraph:
        item = _strict_mapping(
            value,
            {
                "schema_version",
                "graph_id",
                "sketch_id",
                "terms",
                "geometries",
                "anchors",
                "constraints",
            },
        )
        return cls(
            schema_version=item["schema_version"],
            graph_id=item["graph_id"],
            sketch_id=item["sketch_id"],
            terms=tuple(
                _term_ref_from_mapping(raw)
                for raw in _sequence(
                    item["terms"],
                    "terms",
                    maximum=MAX_SKETCH_INTENT_TERMS,
                )
            ),
            geometries=tuple(
                SketchGeometryNode.from_mapping(raw)
                for raw in _sequence(
                    item["geometries"],
                    "geometries",
                    maximum=MAX_SKETCH_INTENT_GEOMETRIES,
                )
            ),
            anchors=tuple(
                SketchAnchor.from_mapping(raw)
                for raw in _sequence(
                    item["anchors"],
                    "anchors",
                    maximum=MAX_SKETCH_INTENT_ANCHORS,
                )
            ),
            constraints=tuple(
                SketchConstraintNode.from_mapping(raw)
                for raw in _sequence(
                    item["constraints"],
                    "constraints",
                    maximum=MAX_SKETCH_INTENT_CONSTRAINTS,
                )
            ),
        )


def encode_sketch_intent_graph(value: object) -> bytes:
    if type(value) is not SketchIntentGraph:
        _fail(SketchIntentErrorCode.INVALID_INPUT)
    envelope = value.to_mapping()
    envelope["graph_sha256"] = value.graph_sha256
    return _canonical(envelope)


def _json_pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            _fail(SketchIntentErrorCode.INVALID_INPUT)
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    _fail(SketchIntentErrorCode.INVALID_INPUT)


def decode_sketch_intent_graph(raw: object) -> SketchIntentGraph:
    if type(raw) is not bytes or not raw:
        _fail(SketchIntentErrorCode.INVALID_INPUT)
    if len(raw) > MAX_SKETCH_INTENT_BYTES:
        _fail(SketchIntentErrorCode.BUDGET_EXCEEDED)
    try:
        value = json.loads(raw, object_pairs_hook=_json_pairs, parse_constant=_reject_constant)
    except SketchIntentError:
        raise
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError, RecursionError):
        _fail(SketchIntentErrorCode.INVALID_INPUT)
    item = _strict_mapping(
        value,
        {
            "schema_version",
            "graph_id",
            "sketch_id",
            "terms",
            "geometries",
            "anchors",
            "constraints",
            "graph_sha256",
        },
    )
    claimed = _digest(item.pop("graph_sha256"), "graph_sha256")
    graph = SketchIntentGraph.from_mapping(item)
    if claimed != graph.graph_sha256:
        _fail(SketchIntentErrorCode.INTEGRITY_FAILURE, "graph_sha256")
    if encode_sketch_intent_graph(graph) != raw:
        _fail(SketchIntentErrorCode.INVALID_INPUT)
    return graph


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchIntentResolution:
    graph_sha256: str
    ontology_sha256: str
    executable_geometry_ids: tuple[str, ...]
    executable_constraint_ids: tuple[str, ...]
    inert_geometry_ids: tuple[str, ...]
    inert_constraint_ids: tuple[str, ...]
    unresolved_term_ref_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _digest(self.graph_sha256, "graph_sha256")
        _digest(self.ontology_sha256, "ontology_sha256")
        for name in (
            "executable_geometry_ids",
            "executable_constraint_ids",
            "inert_geometry_ids",
            "inert_constraint_ids",
            "unresolved_term_ref_ids",
        ):
            value = getattr(self, name)
            if type(value) is not tuple:
                _fail(SketchIntentErrorCode.INVALID_INPUT, name)
            checked = tuple(_identifier(item, name) for item in value)
            if len(set(checked)) != len(checked):
                _fail(SketchIntentErrorCode.DUPLICATE_ID, name)
            object.__setattr__(self, name, tuple(sorted(checked)))


def _match_properties(
    node: SketchGeometryNode | SketchConstraintNode,
    definition: SketchOntologyTermDefinition,
) -> None:
    actual = {item.property_term_ref_id: item for item in node.properties}
    expected = {item.property_term_ref_id: item for item in definition.properties}
    required = {item.property_term_ref_id for item in definition.properties if item.required}
    if not required <= set(actual) or not set(actual) <= set(expected):
        _fail(SketchIntentErrorCode.BINDING_MISMATCH, "properties")
    for property_id, value in actual.items():
        signature = expected[property_id]
        if value.value_kind not in signature.value_kinds:
            _fail(SketchIntentErrorCode.BINDING_MISMATCH, "properties")
        if signature.unit_term_ref_ids:
            if value.unit_term_ref_id not in signature.unit_term_ref_ids:
                _fail(SketchIntentErrorCode.BINDING_MISMATCH, "properties")
        elif value.unit_term_ref_id is not None:
            _fail(SketchIntentErrorCode.BINDING_MISMATCH, "properties")


def _has_unresolved_term_value(
    node: SketchGeometryNode | SketchConstraintNode,
    definitions: dict[str, SketchOntologyTermDefinition],
) -> bool:
    return any(
        item.value_kind is SketchValueKind.TERM_REF and item.value not in definitions
        for item in node.properties
    )


def _match_anchor_slots(
    anchor_ids: tuple[str, ...],
    slots: tuple[SketchAnchorSlotSignature, ...],
    anchors: dict[str, SketchAnchor],
) -> tuple[SketchAnchor, ...]:
    if not slots:
        if anchor_ids:
            _fail(SketchIntentErrorCode.BINDING_MISMATCH, "anchor_ids")
        return ()
    minimum = sum(item.minimum_occurrences for item in slots)
    maximum = sum(item.maximum_occurrences for item in slots)
    if not minimum <= len(anchor_ids) <= maximum:
        _fail(SketchIntentErrorCode.BINDING_MISMATCH, "anchor_ids")
    expanded = list(slots[:-1])
    expanded.extend([slots[-1]] * (len(anchor_ids) - len(slots[:-1])))
    selected: list[SketchAnchor] = []
    for anchor_id, slot in zip(anchor_ids, expanded, strict=True):
        anchor = anchors[anchor_id]
        if (
            anchor.target_kind not in slot.target_kinds
            or anchor.role_term_ref_id not in slot.role_term_ref_ids
        ):
            _fail(SketchIntentErrorCode.BINDING_MISMATCH, "anchor_ids")
        selected.append(anchor)
    return tuple(selected)


def resolve_sketch_intent(
    graph: object,
    ontology: object,
) -> SketchIntentResolution:
    """Resolve exact known terms without treating unknown terms as commands."""

    if type(graph) is not SketchIntentGraph or type(ontology) is not SketchOntologyCatalog:
        _fail(SketchIntentErrorCode.INVALID_INPUT)
    graph_terms = {item.term_ref_id: item for item in graph.terms}
    definitions = ontology.by_id
    for term_id in set(graph_terms) & set(definitions):
        if graph_terms[term_id] != definitions[term_id].reference:
            _fail(SketchIntentErrorCode.INTEGRITY_FAILURE, "terms")
    unresolved_terms = tuple(sorted(set(graph_terms) - set(definitions)))
    anchors = {item.anchor_id: item for item in graph.anchors}

    structurally_known: dict[str, tuple[SketchGeometryNode, tuple[SketchAnchor, ...]]] = {}
    for node in graph.geometries:
        definition = definitions.get(node.geometry_term_ref_id)
        if definition is None:
            continue
        if definition.kind is not SketchTermKind.GEOMETRY:
            _fail(SketchIntentErrorCode.BINDING_MISMATCH, "geometries")
        _match_properties(node, definition)
        if _has_unresolved_term_value(node, definitions):
            continue
        selected = _match_anchor_slots(node.anchor_ids, definition.anchor_slots, anchors)
        structurally_known[node.geometry_id] = (node, selected)

    executable_geometries: set[str] = set()
    while True:
        promoted = {
            geometry_id
            for geometry_id, (_node, selected) in structurally_known.items()
            if geometry_id not in executable_geometries
            and all(
                anchor.target_kind is SketchAnchorTargetKind.SKETCH
                or (
                    anchor.target_kind is SketchAnchorTargetKind.GEOMETRY
                    and anchor.target_id in executable_geometries
                )
                for anchor in selected
            )
        }
        if not promoted:
            break
        executable_geometries.update(promoted)

    executable_constraints: set[str] = set()
    for node in graph.constraints:
        definition = definitions.get(node.constraint_term_ref_id)
        if definition is None:
            continue
        if definition.kind is not SketchTermKind.CONSTRAINT:
            _fail(SketchIntentErrorCode.BINDING_MISMATCH, "constraints")
        _match_properties(node, definition)
        if _has_unresolved_term_value(node, definitions):
            continue
        selected = _match_anchor_slots(node.anchor_ids, definition.anchor_slots, anchors)
        if all(
            anchor.target_kind is SketchAnchorTargetKind.SKETCH
            or (
                anchor.target_kind is SketchAnchorTargetKind.GEOMETRY
                and anchor.target_id in executable_geometries
            )
            for anchor in selected
        ):
            executable_constraints.add(node.constraint_id)

    all_geometry_ids = {item.geometry_id for item in graph.geometries}
    all_constraint_ids = {item.constraint_id for item in graph.constraints}
    return SketchIntentResolution(
        graph_sha256=graph.graph_sha256,
        ontology_sha256=ontology.catalog_sha256,
        executable_geometry_ids=tuple(executable_geometries),
        executable_constraint_ids=tuple(executable_constraints),
        inert_geometry_ids=tuple(all_geometry_ids - executable_geometries),
        inert_constraint_ids=tuple(all_constraint_ids - executable_constraints),
        unresolved_term_ref_ids=unresolved_terms,
    )


__all__ = [
    "MAX_SKETCH_ANCHORS_PER_NODE",
    "MAX_SKETCH_INTENT_ANCHORS",
    "MAX_SKETCH_INTENT_BYTES",
    "MAX_SKETCH_INTENT_CONSTRAINTS",
    "MAX_SKETCH_INTENT_GEOMETRIES",
    "MAX_SKETCH_INTENT_TERMS",
    "MAX_SKETCH_PROPERTIES_PER_NODE",
    "SKETCH_INTENT_SCHEMA_VERSION",
    "SketchAnchor",
    "SketchConstraintMode",
    "SketchConstraintNode",
    "SketchGeometryNode",
    "SketchIntentError",
    "SketchIntentErrorCode",
    "SketchIntentGraph",
    "SketchIntentResolution",
    "SketchProperty",
    "decode_sketch_intent_graph",
    "encode_sketch_intent_graph",
    "resolve_sketch_intent",
]
