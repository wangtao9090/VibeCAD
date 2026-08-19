"""Immutable backend-neutral graph for editable sketch intent.

This contract describes geometry, stable anchors, and constraints without
choosing a CAD backend.  Unknown ontology terms remain serializable but inert.
Catalog matching here establishes structural resolution only; execution trust
belongs to a separate adapter policy boundary.

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
    SketchElementKind,
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
MAX_SKETCH_INTENT_RESULTS = 1_024
MAX_SKETCH_PROPERTIES_PER_NODE = 64
MAX_SKETCH_ANCHORS_PER_NODE = 16
MAX_SKETCH_RESULTS_PER_NODE = 16
MAX_SKETCH_VECTOR_COMPONENTS = 16
MAX_SKETCH_MATRIX_ROWS = 16
MAX_SKETCH_MATRIX_COLUMNS = 16
MAX_SKETCH_VALUE_ITEMS = 256
MAX_SKETCH_VALUE_NODES = 4_096
MAX_SKETCH_VALUE_DEPTH = 16
MAX_SKETCH_VALUE_BYTES = 64 * 1024
MAX_SKETCH_TEXT_BYTES = 1_024
MAX_SKETCH_INTENT_BYTES = 512 * 1024

_SAFE_INTEGER = 2**53 - 1
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,127}$")
_GRAPH_DIGEST_DOMAIN = b"vibecad-sketch-intent-graph-v1\0"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")


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


def _canonical(value: object, *, maximum: int | None = None) -> bytes:
    if maximum is None:
        maximum = MAX_SKETCH_INTENT_BYTES
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


def _value_tree(value: object, *, depth: int, remaining: list[int]) -> None:
    remaining[0] -= 1
    if remaining[0] < 0 or depth > MAX_SKETCH_VALUE_DEPTH:
        _fail(SketchIntentErrorCode.BUDGET_EXCEEDED, "value")
    if value is None or type(value) is bool:
        return
    if type(value) in {int, float}:
        _number(value, "value")
        return
    if type(value) is str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
        if size > MAX_SKETCH_TEXT_BYTES:
            _fail(SketchIntentErrorCode.BUDGET_EXCEEDED, "value")
        return
    if type(value) is list:
        if len(value) > MAX_SKETCH_VALUE_ITEMS:
            _fail(SketchIntentErrorCode.BUDGET_EXCEEDED, "value")
        for item in value:
            _value_tree(item, depth=depth + 1, remaining=remaining)
        return
    if type(value) is dict:
        if len(value) > MAX_SKETCH_VALUE_ITEMS:
            _fail(SketchIntentErrorCode.BUDGET_EXCEEDED, "value")
        for key, item in value.items():
            _identifier(key, "value")
            _value_tree(item, depth=depth + 1, remaining=remaining)
        return
    _fail(SketchIntentErrorCode.INVALID_INPUT, "value")


def _canonical_value(value: object) -> bytes:
    _value_tree(value, depth=0, remaining=[MAX_SKETCH_VALUE_NODES])
    return _canonical(value, maximum=MAX_SKETCH_VALUE_BYTES)


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
class SketchElementRef:
    element_id: str
    element_kind: SketchElementKind

    def __post_init__(self) -> None:
        _identifier(self.element_id, "element_id")
        if type(self.element_kind) is not SketchElementKind:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "element_kind")

    def to_mapping(self) -> dict[str, str]:
        return {"element_id": self.element_id, "element_kind": self.element_kind.value}

    @classmethod
    def from_mapping(cls, value: object) -> SketchElementRef:
        item = _strict_mapping(value, {"element_id", "element_kind"}, "element_ref")
        try:
            kind = SketchElementKind(item["element_kind"])
        except (TypeError, ValueError):
            _fail(SketchIntentErrorCode.INVALID_INPUT, "element_kind")
        return cls(element_id=item["element_id"], element_kind=kind)


def _normalize_typed_value(kind: SketchValueKind, value: object) -> object:
    if kind is SketchValueKind.BOOLEAN:
        if type(value) is not bool:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
        return value
    if kind is SketchValueKind.INTEGER:
        if type(value) is not int or abs(value) > _SAFE_INTEGER:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
        return value
    if kind is SketchValueKind.NUMBER:
        return _number(value, "value")
    if kind in {SketchValueKind.TEXT, SketchValueKind.EXPRESSION}:
        if type(value) is not str or not value or not value.isprintable():
            _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
        return value
    if kind is SketchValueKind.VECTOR:
        if type(value) not in {list, tuple}:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
        if not 1 <= len(value) <= MAX_SKETCH_VECTOR_COMPONENTS:
            _fail(
                SketchIntentErrorCode.BUDGET_EXCEEDED
                if len(value) > MAX_SKETCH_VECTOR_COMPONENTS
                else SketchIntentErrorCode.INVALID_INPUT,
                "value",
            )
        return [_number(item, "value") for item in value]
    if kind is SketchValueKind.MATRIX:
        if type(value) not in {list, tuple} or not 1 <= len(value) <= MAX_SKETCH_MATRIX_ROWS:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
        rows: list[list[int | float]] = []
        width: int | None = None
        for row in value:
            if type(row) not in {list, tuple} or not 1 <= len(row) <= MAX_SKETCH_MATRIX_COLUMNS:
                _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
            if width is None:
                width = len(row)
            elif len(row) != width:
                _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
            rows.append([_number(item, "value") for item in row])
        return rows
    if kind is SketchValueKind.PLACEMENT:
        item = _strict_mapping(
            value,
            {"translation", "rotation_quaternion"},
            "value",
        )
        translation = item["translation"]
        rotation = item["rotation_quaternion"]
        if type(translation) not in {list, tuple} or len(translation) != 3:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
        if type(rotation) not in {list, tuple} or len(rotation) != 4:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
        return {
            "translation": [_number(item, "value") for item in translation],
            "rotation_quaternion": [_number(item, "value") for item in rotation],
        }
    if kind is SketchValueKind.CONTENT_REF:
        item = _strict_mapping(
            value,
            {"sha256", "size_bytes", "media_type", "schema_term_ref_id"},
            "value",
        )
        _digest(item["sha256"], "value")
        if type(item["size_bytes"]) is not int or not 0 <= item["size_bytes"] <= _SAFE_INTEGER:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
        if type(item["media_type"]) is not str or _MEDIA_TYPE.fullmatch(item["media_type"]) is None:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
        if item["schema_term_ref_id"] is not None:
            _identifier(item["schema_term_ref_id"], "value")
        return dict(item)
    if kind is SketchValueKind.TERM_REF:
        return _identifier(value, "value")
    if kind is SketchValueKind.ELEMENT_REF:
        element = value if type(value) is SketchElementRef else SketchElementRef.from_mapping(value)
        return element.to_mapping()
    if kind is SketchValueKind.LIST:
        if type(value) not in {list, tuple}:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
        return list(value)
    if kind is SketchValueKind.RECORD:
        if type(value) is not dict:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
        return dict(value)
    _fail(SketchIntentErrorCode.INVALID_INPUT, "value_kind")


def _decode_value_bytes(raw: bytes) -> object:
    if not raw or len(raw) > MAX_SKETCH_VALUE_BYTES:
        _fail(SketchIntentErrorCode.BUDGET_EXCEEDED, "value")
    try:
        value = json.loads(raw, object_pairs_hook=_json_pairs, parse_constant=_reject_constant)
    except SketchIntentError:
        raise
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError, RecursionError):
        _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
    if _canonical_value(value) != raw:
        _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchTypedValue:
    value_type_term_ref_id: str
    value_kind: SketchValueKind
    value: object

    def __post_init__(self) -> None:
        _identifier(self.value_type_term_ref_id, "value_type_term_ref_id")
        if type(self.value_kind) is not SketchValueKind:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "value_kind")
        decoded = _decode_value_bytes(self.value) if type(self.value) is bytes else self.value
        normalized = _normalize_typed_value(self.value_kind, decoded)
        object.__setattr__(self, "value", _canonical_value(normalized))

    @property
    def decoded_value(self) -> object:
        return _decode_value_bytes(self.value)

    @property
    def element_ref(self) -> SketchElementRef | None:
        if self.value_kind is not SketchValueKind.ELEMENT_REF:
            return None
        return SketchElementRef.from_mapping(self.decoded_value)

    def referenced_term_ids(self) -> tuple[str, ...]:
        result = [self.value_type_term_ref_id]
        decoded = self.decoded_value
        if self.value_kind is SketchValueKind.TERM_REF:
            result.append(decoded)
        elif self.value_kind is SketchValueKind.CONTENT_REF and decoded["schema_term_ref_id"]:
            result.append(decoded["schema_term_ref_id"])
        return tuple(result)

    def to_mapping(self) -> dict[str, object]:
        return {
            "value_type_term_ref_id": self.value_type_term_ref_id,
            "value_kind": self.value_kind.value,
            "value": self.decoded_value,
        }

    @classmethod
    def from_mapping(cls, value: object) -> SketchTypedValue:
        item = _strict_mapping(
            value,
            {"value_type_term_ref_id", "value_kind", "value"},
            "typed_value",
        )
        try:
            kind = SketchValueKind(item["value_kind"])
        except (TypeError, ValueError):
            _fail(SketchIntentErrorCode.INVALID_INPUT, "value_kind")
        return cls(
            value_type_term_ref_id=item["value_type_term_ref_id"],
            value_kind=kind,
            value=item["value"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchProperty:
    property_term_ref_id: str
    typed_value: SketchTypedValue
    unit_term_ref_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.property_term_ref_id, "property_term_ref_id")
        if type(self.typed_value) is not SketchTypedValue:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "typed_value")
        if self.unit_term_ref_id is not None:
            _identifier(self.unit_term_ref_id, "unit_term_ref_id")

    def to_mapping(self) -> dict[str, object]:
        return {
            "property_term_ref_id": self.property_term_ref_id,
            "typed_value": self.typed_value.to_mapping(),
            "unit_term_ref_id": self.unit_term_ref_id,
        }

    @classmethod
    def from_mapping(cls, value: object) -> SketchProperty:
        item = _strict_mapping(
            value,
            {"property_term_ref_id", "typed_value", "unit_term_ref_id"},
        )
        return cls(
            property_term_ref_id=item["property_term_ref_id"],
            typed_value=SketchTypedValue.from_mapping(item["typed_value"]),
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


def _result_ids(value: object, path: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        _fail(SketchIntentErrorCode.INVALID_INPUT, path)
    if len(value) > MAX_SKETCH_RESULTS_PER_NODE:
        _fail(SketchIntentErrorCode.BUDGET_EXCEEDED, path)
    result = tuple(_identifier(item, path) for item in value)
    if len(set(result)) != len(result):
        _fail(SketchIntentErrorCode.DUPLICATE_ID, path)
    return tuple(sorted(result))


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
class SketchResultPort:
    result_id: str
    producer_id: str
    port_id: str
    value_type_term_ref_id: str
    value: SketchTypedValue | None = None

    def __post_init__(self) -> None:
        _identifier(self.result_id, "result_id")
        _identifier(self.producer_id, "producer_id")
        _identifier(self.port_id, "port_id")
        _identifier(self.value_type_term_ref_id, "value_type_term_ref_id")
        if self.value is not None:
            if type(self.value) is not SketchTypedValue:
                _fail(SketchIntentErrorCode.INVALID_INPUT, "value")
            if self.value.value_type_term_ref_id != self.value_type_term_ref_id:
                _fail(SketchIntentErrorCode.BINDING_MISMATCH, "value")

    def to_mapping(self) -> dict[str, object]:
        return {
            "result_id": self.result_id,
            "producer_id": self.producer_id,
            "port_id": self.port_id,
            "value_type_term_ref_id": self.value_type_term_ref_id,
            "value": None if self.value is None else self.value.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> SketchResultPort:
        item = _strict_mapping(
            value,
            {"result_id", "producer_id", "port_id", "value_type_term_ref_id", "value"},
        )
        return cls(
            result_id=item["result_id"],
            producer_id=item["producer_id"],
            port_id=item["port_id"],
            value_type_term_ref_id=item["value_type_term_ref_id"],
            value=None if item["value"] is None else SketchTypedValue.from_mapping(item["value"]),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchGeometryNode:
    geometry_id: str
    geometry_term_ref_id: str
    properties: tuple[SketchProperty, ...] = ()
    anchor_ids: tuple[str, ...] = ()
    result_ids: tuple[str, ...] = ()
    construction: bool = False

    def __post_init__(self) -> None:
        _identifier(self.geometry_id, "geometry_id")
        _identifier(self.geometry_term_ref_id, "geometry_term_ref_id")
        object.__setattr__(self, "properties", _properties(self.properties, "properties"))
        object.__setattr__(self, "anchor_ids", _anchor_ids(self.anchor_ids, "anchor_ids"))
        object.__setattr__(self, "result_ids", _result_ids(self.result_ids, "result_ids"))
        if type(self.construction) is not bool:
            _fail(SketchIntentErrorCode.INVALID_INPUT, "construction")

    def to_mapping(self) -> dict[str, object]:
        return {
            "geometry_id": self.geometry_id,
            "geometry_term_ref_id": self.geometry_term_ref_id,
            "properties": [item.to_mapping() for item in self.properties],
            "anchor_ids": list(self.anchor_ids),
            "result_ids": list(self.result_ids),
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
                "result_ids",
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
            result_ids=tuple(
                _sequence(
                    item["result_ids"],
                    "result_ids",
                    maximum=MAX_SKETCH_RESULTS_PER_NODE,
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
    result_ids: tuple[str, ...] = ()
    mode: SketchConstraintMode = SketchConstraintMode.DRIVING
    enabled: bool = True

    def __post_init__(self) -> None:
        _identifier(self.constraint_id, "constraint_id")
        _identifier(self.constraint_term_ref_id, "constraint_term_ref_id")
        object.__setattr__(self, "anchor_ids", _anchor_ids(self.anchor_ids, "anchor_ids"))
        object.__setattr__(self, "properties", _properties(self.properties, "properties"))
        object.__setattr__(self, "result_ids", _result_ids(self.result_ids, "result_ids"))
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
            "result_ids": list(self.result_ids),
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
                "result_ids",
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
            result_ids=tuple(
                _sequence(
                    item["result_ids"],
                    "result_ids",
                    maximum=MAX_SKETCH_RESULTS_PER_NODE,
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
    results: tuple[SketchResultPort, ...] = ()

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
        results = _ordered_objects(
            self.results,
            SketchResultPort,
            "results",
            maximum=MAX_SKETCH_INTENT_RESULTS,
            key=lambda item: item.result_id,
        )
        element_ids = (
            {self.graph_id, self.sketch_id}
            | {item.geometry_id for item in geometries}
            | {item.anchor_id for item in anchors}
            | {item.constraint_id for item in constraints}
            | {item.result_id for item in results}
        )
        expected_count = 2 + len(geometries) + len(anchors) + len(constraints) + len(results)
        if len(element_ids) != expected_count:
            _fail(SketchIntentErrorCode.DUPLICATE_ID)
        term_ids = {item.term_ref_id for item in terms}
        term_identities = tuple(item.semantic_identity for item in terms)
        if len(set(term_identities)) != len(term_identities):
            _fail(SketchIntentErrorCode.DUPLICATE_ID, "terms")
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
            for prop in node.properties:
                refs.update(prop.typed_value.referenced_term_ids())
        refs.update(item.value_type_term_ref_id for item in results)
        for item in results:
            if item.value is not None:
                refs.update(item.value.referenced_term_ids())
        if not refs <= term_ids:
            _fail(SketchIntentErrorCode.UNKNOWN_REFERENCE, "terms")
        geometry_ids = {item.geometry_id for item in geometries}
        constraint_ids = {item.constraint_id for item in constraints}
        result_ids = {item.result_id for item in results}
        element_type_by_ref = {
            **{item.geometry_id: item.geometry_term_ref_id for item in geometries},
            **{item.constraint_id: item.constraint_term_ref_id for item in constraints},
            **{item.result_id: item.value_type_term_ref_id for item in results},
        }
        producer_ids = geometry_ids | constraint_ids
        results_by_producer: dict[str, set[str]] = {}
        for index, result in enumerate(results):
            if result.producer_id not in producer_ids:
                _fail(SketchIntentErrorCode.UNKNOWN_REFERENCE, f"results/{index}/producer_id")
            results_by_producer.setdefault(result.producer_id, set()).add(result.result_id)
        for collection_name, nodes in (("geometries", geometries), ("constraints", constraints)):
            for index, node in enumerate(nodes):
                if set(node.result_ids) != results_by_producer.get(
                    node.geometry_id if type(node) is SketchGeometryNode else node.constraint_id,
                    set(),
                ):
                    _fail(
                        SketchIntentErrorCode.BINDING_MISMATCH,
                        f"{collection_name}/{index}/result_ids",
                    )
        anchor_ids = {item.anchor_id for item in anchors}

        def require_element_ref(value: SketchTypedValue, path: str) -> None:
            ref = value.element_ref
            if ref is None:
                return
            known = {
                SketchElementKind.GEOMETRY: geometry_ids,
                SketchElementKind.CONSTRAINT: constraint_ids,
                SketchElementKind.RESULT: result_ids,
            }[ref.element_kind]
            if ref.element_id not in known:
                _fail(SketchIntentErrorCode.UNKNOWN_REFERENCE, path)
            if element_type_by_ref[ref.element_id] != value.value_type_term_ref_id:
                _fail(SketchIntentErrorCode.BINDING_MISMATCH, path)

        for index, result in enumerate(results):
            if result.value is not None:
                require_element_ref(result.value, f"results/{index}/value")
        for index, anchor in enumerate(anchors):
            if anchor.target_kind is SketchAnchorTargetKind.SKETCH:
                valid_target = anchor.target_id == self.sketch_id
            elif anchor.target_kind is SketchAnchorTargetKind.GEOMETRY:
                valid_target = anchor.target_id in geometry_ids
            elif anchor.target_kind is SketchAnchorTargetKind.RESULT:
                valid_target = anchor.target_id in result_ids
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
                for prop in node.properties:
                    require_element_ref(
                        prop.typed_value,
                        f"{collection_name}/{index}/properties",
                    )
        object.__setattr__(self, "terms", terms)
        object.__setattr__(self, "geometries", geometries)
        object.__setattr__(self, "anchors", anchors)
        object.__setattr__(self, "constraints", constraints)
        object.__setattr__(self, "results", results)
        envelope = self.to_mapping()
        envelope["graph_sha256"] = self.graph_sha256
        _canonical(envelope)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "graph_id": self.graph_id,
            "sketch_id": self.sketch_id,
            "terms": [item.to_mapping() for item in self.terms],
            "geometries": [item.to_mapping() for item in self.geometries],
            "anchors": [item.to_mapping() for item in self.anchors],
            "constraints": [item.to_mapping() for item in self.constraints],
            "results": [item.to_mapping() for item in self.results],
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
                "results",
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
            results=tuple(
                SketchResultPort.from_mapping(raw)
                for raw in _sequence(
                    item["results"],
                    "results",
                    maximum=MAX_SKETCH_INTENT_RESULTS,
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
            "results",
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
    structurally_resolved_geometry_ids: tuple[str, ...]
    structurally_resolved_constraint_ids: tuple[str, ...]
    inert_geometry_ids: tuple[str, ...]
    inert_constraint_ids: tuple[str, ...]
    unresolved_term_ref_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _digest(self.graph_sha256, "graph_sha256")
        _digest(self.ontology_sha256, "ontology_sha256")
        for name in (
            "structurally_resolved_geometry_ids",
            "structurally_resolved_constraint_ids",
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
        if (
            value.typed_value.value_kind not in signature.value_kinds
            or value.typed_value.value_type_term_ref_id not in signature.value_type_term_ref_ids
        ):
            _fail(SketchIntentErrorCode.BINDING_MISMATCH, "properties")
        ref = value.typed_value.element_ref
        if ref is not None and ref.element_kind not in signature.element_kinds:
            _fail(SketchIntentErrorCode.BINDING_MISMATCH, "properties")
        if signature.unit_term_ref_ids:
            if value.unit_term_ref_id not in signature.unit_term_ref_ids:
                _fail(SketchIntentErrorCode.BINDING_MISMATCH, "properties")
        elif value.unit_term_ref_id is not None:
            _fail(SketchIntentErrorCode.BINDING_MISMATCH, "properties")


def _has_unresolved_node_terms(
    node: SketchGeometryNode | SketchConstraintNode,
    anchors: dict[str, SketchAnchor],
    results: dict[str, SketchResultPort],
    definitions: dict[str, SketchOntologyTermDefinition],
) -> bool:
    refs = {
        *(item.property_term_ref_id for item in node.properties),
        *(item.typed_value.value_type_term_ref_id for item in node.properties),
        *(item.unit_term_ref_id for item in node.properties if item.unit_term_ref_id is not None),
        *(anchors[anchor_id].role_term_ref_id for anchor_id in node.anchor_ids),
        *(results[result_id].value_type_term_ref_id for result_id in node.result_ids),
    }
    for item in node.properties:
        refs.update(item.typed_value.referenced_term_ids())
    for result_id in node.result_ids:
        value = results[result_id].value
        if value is not None:
            refs.update(value.referenced_term_ids())
    return not refs <= definitions.keys()


def _match_results(
    node: SketchGeometryNode | SketchConstraintNode,
    definition: SketchOntologyTermDefinition,
    results: dict[str, SketchResultPort],
) -> None:
    expected = {item.port_id: item for item in definition.result_ports}
    actual = {results[result_id].port_id: results[result_id] for result_id in node.result_ids}
    if len(actual) != len(node.result_ids):
        _fail(SketchIntentErrorCode.BINDING_MISMATCH, "results")
    required = {item.port_id for item in definition.result_ports if item.required}
    if not required <= set(actual) or not set(actual) <= set(expected):
        _fail(SketchIntentErrorCode.BINDING_MISMATCH, "results")
    for port_id, result in actual.items():
        if result.value_type_term_ref_id not in expected[port_id].value_type_term_ref_ids:
            _fail(SketchIntentErrorCode.BINDING_MISMATCH, "results")


def _match_anchor_slots(
    anchor_ids: tuple[str, ...],
    slots: tuple[SketchAnchorSlotSignature, ...],
    anchors: dict[str, SketchAnchor],
    results: dict[str, SketchResultPort],
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
        if (
            anchor.target_kind is SketchAnchorTargetKind.RESULT
            and results[anchor.target_id].value_type_term_ref_id
            not in slot.result_type_term_ref_ids
        ):
            _fail(SketchIntentErrorCode.BINDING_MISMATCH, "anchor_ids")
        selected.append(anchor)
    return tuple(selected)


def _structural_dependencies(
    node: SketchGeometryNode | SketchConstraintNode,
    selected: tuple[SketchAnchor, ...],
    results: dict[str, SketchResultPort],
) -> tuple[frozenset[str], bool]:
    """Return local producer dependencies and whether an external target is present."""

    dependencies: set[str] = set()
    has_external = False
    for anchor in selected:
        if anchor.target_kind is SketchAnchorTargetKind.GEOMETRY:
            dependencies.add(anchor.target_id)
        elif anchor.target_kind is SketchAnchorTargetKind.RESULT:
            dependencies.add(results[anchor.target_id].producer_id)
        elif anchor.target_kind is SketchAnchorTargetKind.EXTERNAL:
            has_external = True
    values = [item.typed_value for item in node.properties]
    values.extend(
        result.value
        for result_id in node.result_ids
        if (result := results[result_id]).value is not None
    )
    for value in values:
        ref = value.element_ref
        if ref is None:
            continue
        if ref.element_kind is SketchElementKind.RESULT:
            dependencies.add(results[ref.element_id].producer_id)
        else:
            dependencies.add(ref.element_id)
    return frozenset(dependencies), has_external


def resolve_sketch_intent(
    graph: object,
    ontology: object,
) -> SketchIntentResolution:
    """Resolve graph structure only; this function never grants execution authority."""

    if type(graph) is not SketchIntentGraph or type(ontology) is not SketchOntologyCatalog:
        _fail(SketchIntentErrorCode.INVALID_INPUT)
    graph_terms = {item.term_ref_id: item for item in graph.terms}
    definitions = ontology.by_id
    for term_id in set(graph_terms) & set(definitions):
        if graph_terms[term_id] != definitions[term_id].reference:
            _fail(SketchIntentErrorCode.INTEGRITY_FAILURE, "terms")
    unresolved_terms = tuple(sorted(set(graph_terms) - set(definitions)))
    anchors = {item.anchor_id: item for item in graph.anchors}
    results = {item.result_id: item for item in graph.results}

    candidates: dict[str, tuple[frozenset[str], bool]] = {}
    for collection_name, nodes, expected_kind in (
        ("geometries", graph.geometries, SketchTermKind.GEOMETRY),
        ("constraints", graph.constraints, SketchTermKind.CONSTRAINT),
    ):
        for node in nodes:
            term_ref_id = (
                node.geometry_term_ref_id
                if type(node) is SketchGeometryNode
                else node.constraint_term_ref_id
            )
            node_id = node.geometry_id if type(node) is SketchGeometryNode else node.constraint_id
            definition = definitions.get(term_ref_id)
            if definition is None:
                continue
            if definition.kind is not expected_kind:
                _fail(SketchIntentErrorCode.BINDING_MISMATCH, collection_name)
            if _has_unresolved_node_terms(node, anchors, results, definitions):
                continue
            _match_properties(node, definition)
            _match_results(node, definition, results)
            selected = _match_anchor_slots(
                node.anchor_ids,
                definition.anchor_slots,
                anchors,
                results,
            )
            candidates[node_id] = _structural_dependencies(node, selected, results)

    structurally_resolved: set[str] = set()
    while True:
        promoted = {
            node_id
            for node_id, (dependencies, has_external) in candidates.items()
            if node_id not in structurally_resolved
            and not has_external
            and dependencies <= structurally_resolved
        }
        if not promoted:
            break
        structurally_resolved.update(promoted)

    all_geometry_ids = {item.geometry_id for item in graph.geometries}
    all_constraint_ids = {item.constraint_id for item in graph.constraints}
    structurally_resolved_geometries = structurally_resolved & all_geometry_ids
    structurally_resolved_constraints = structurally_resolved & all_constraint_ids
    return SketchIntentResolution(
        graph_sha256=graph.graph_sha256,
        ontology_sha256=ontology.catalog_sha256,
        structurally_resolved_geometry_ids=tuple(structurally_resolved_geometries),
        structurally_resolved_constraint_ids=tuple(structurally_resolved_constraints),
        inert_geometry_ids=tuple(all_geometry_ids - structurally_resolved_geometries),
        inert_constraint_ids=tuple(all_constraint_ids - structurally_resolved_constraints),
        unresolved_term_ref_ids=unresolved_terms,
    )


__all__ = [
    "MAX_SKETCH_ANCHORS_PER_NODE",
    "MAX_SKETCH_INTENT_ANCHORS",
    "MAX_SKETCH_INTENT_BYTES",
    "MAX_SKETCH_INTENT_CONSTRAINTS",
    "MAX_SKETCH_INTENT_GEOMETRIES",
    "MAX_SKETCH_INTENT_RESULTS",
    "MAX_SKETCH_INTENT_TERMS",
    "MAX_SKETCH_PROPERTIES_PER_NODE",
    "SKETCH_INTENT_SCHEMA_VERSION",
    "SketchAnchor",
    "SketchConstraintMode",
    "SketchConstraintNode",
    "SketchElementRef",
    "SketchGeometryNode",
    "SketchIntentError",
    "SketchIntentErrorCode",
    "SketchIntentGraph",
    "SketchIntentResolution",
    "SketchProperty",
    "SketchResultPort",
    "SketchTypedValue",
    "decode_sketch_intent_graph",
    "encode_sketch_intent_graph",
    "resolve_sketch_intent",
]
