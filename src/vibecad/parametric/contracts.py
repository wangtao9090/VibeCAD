"""Immutable, runtime-neutral contracts for editable parametric design intent.

This module deliberately contains no FreeCAD, task, revision, MCP, or provider
integration.  It defines the closed v1 value graph that a later compiler can
translate into one reviewed ``ModelProgram`` operation.  Structural validity
does not prove profile closure or solver success; the compiler must solve every
sketch and recompute every feature before producing a candidate.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Self

from vibecad.workflow.errors import (
    MAX_SAFE_JSON_INTEGER,
    is_canonical_json_pointer,
    join_json_pointer,
)

PARAMETRIC_SCHEMA_VERSION = 1
MAX_DESIGN_EVIDENCE = 128
MAX_DESIGN_PARAMETERS = 128
MAX_DATUM_PLANES = 4
MAX_DESIGN_SKETCHES = 8
MAX_SKETCH_GEOMETRIES = 128
MAX_SKETCH_CONSTRAINTS = 256
MAX_DESIGN_FEATURES = 8
MAX_PARAMETRIC_IR_BYTES = 256 * 1024

_MAX_TOTAL_GEOMETRIES = 256
_MAX_TOTAL_CONSTRAINTS = 512
_MAX_PARAMETRIC_IR_NODES = 8_192
_MAX_EVIDENCE_REFS = 8
_MAX_SOURCE_REFS = 8

_MAX_TEXT_BYTES = 256
_MAX_ERROR_PATH_LENGTH = 512
_MAX_ABSOLUTE_VALUE = 1_000_000_000_000
_DIGEST_DOMAIN = b"vibecad-parametric-design-ir-v1\0"
_LOCAL_ID = re.compile(
    r"^ir_(?:design|body|evidence|parameter|datum|sketch|geometry|constraint|feature)_"
    r"[0-9a-f]{32}$"
)


class ParametricErrorCode(StrEnum):
    """Stable fail-closed rejection reasons for the v1 contract."""

    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    UNSUPPORTED_VERSION = "unsupported_version"
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"
    BUDGET_EXCEEDED = "budget_exceeded"
    DUPLICATE_ID = "duplicate_id"
    UNKNOWN_REFERENCE = "unknown_reference"
    INVALID_ORDER = "invalid_order"


_ERROR_MESSAGES = {
    ParametricErrorCode.MISSING_FIELD: "A required field is missing.",
    ParametricErrorCode.UNKNOWN_FIELD: "The field is not supported.",
    ParametricErrorCode.UNSUPPORTED_VERSION: "The schema version is not supported.",
    ParametricErrorCode.INVALID_TYPE: "The value has an invalid type.",
    ParametricErrorCode.INVALID_VALUE: "The value is invalid.",
    ParametricErrorCode.BUDGET_EXCEEDED: "The parametric design exceeds its resource budget.",
    ParametricErrorCode.DUPLICATE_ID: "Identifiers must be unique within the design.",
    ParametricErrorCode.UNKNOWN_REFERENCE: "The referenced design value does not exist.",
    ParametricErrorCode.INVALID_ORDER: "The feature order cannot produce one supported body.",
}


class ParametricContractError(ValueError):
    """Bounded error envelope that does not reflect rejected input values."""

    def __init__(self, code: ParametricErrorCode, path: str = "") -> None:
        if type(code) is not ParametricErrorCode:
            raise TypeError("code must be a ParametricErrorCode")
        if (
            type(path) is not str
            or len(path) > _MAX_ERROR_PATH_LENGTH
            or not is_canonical_json_pointer(path)
        ):
            raise ValueError("path must be a bounded canonical JSON Pointer")
        self.schema_version = PARAMETRIC_SCHEMA_VERSION
        self.code = code
        self.path = path
        self.message = _ERROR_MESSAGES[code]
        super().__init__(self.message)

    def to_mapping(self) -> dict[str, int | str]:
        return {
            "schema_version": self.schema_version,
            "code": self.code.value,
            "path": self.path,
            "message": self.message,
        }


def _raise(code: ParametricErrorCode, path: str = "") -> None:
    raise ParametricContractError(code, path)


def _safe_path(parent: str, name: str) -> str:
    if (
        len(name) > 128
        or not name.isprintable()
        or len(name.splitlines()) != 1
        or len(parent) + len(name) + 1 > _MAX_ERROR_PATH_LENGTH
    ):
        name = "__unknown__"
    return join_json_pointer(parent, name)


def _fields(
    value: object,
    *,
    allowed: set[str],
    required: set[str],
    path: str = "",
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _raise(ParametricErrorCode.INVALID_TYPE, path)
    try:
        keys = tuple(value)
    except Exception:
        _raise(ParametricErrorCode.INVALID_VALUE, path)
    if not all(type(key) is str for key in keys):
        _raise(ParametricErrorCode.INVALID_TYPE, path)
    unknown = sorted(key for key in keys if key not in allowed)
    if unknown:
        _raise(ParametricErrorCode.UNKNOWN_FIELD, _safe_path(path, unknown[0]))
    missing = sorted(required - set(keys))
    if missing:
        _raise(ParametricErrorCode.MISSING_FIELD, join_json_pointer(path, missing[0]))
    try:
        return {key: value[key] for key in keys}
    except Exception:
        _raise(ParametricErrorCode.INVALID_VALUE, path)


def _schema(value: object, path: str = "/schema_version") -> int:
    if type(value) is not int:
        _raise(ParametricErrorCode.INVALID_TYPE, path)
    if value != PARAMETRIC_SCHEMA_VERSION:
        _raise(ParametricErrorCode.UNSUPPORTED_VERSION, path)
    return value


def _text(value: object, path: str) -> str:
    if type(value) is not str:
        _raise(ParametricErrorCode.INVALID_TYPE, path)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        _raise(ParametricErrorCode.INVALID_VALUE, path)
    if (
        not value.strip()
        or value != value.strip()
        or not value.isprintable()
        or len(value.splitlines()) != 1
        or len(encoded) > _MAX_TEXT_BYTES
    ):
        _raise(ParametricErrorCode.INVALID_VALUE, path)
    return value


def _local_id(value: object, path: str, prefix: str) -> str:
    result = _text(value, path)
    if _LOCAL_ID.fullmatch(result) is None or not result.startswith(f"ir_{prefix}_"):
        _raise(ParametricErrorCode.INVALID_VALUE, path)
    return result


def _number(value: object, path: str, *, optional: bool = False) -> int | float | None:
    if value is None and optional:
        return None
    if type(value) not in {int, float}:
        _raise(ParametricErrorCode.INVALID_TYPE, path)
    if type(value) is int and abs(value) > MAX_SAFE_JSON_INTEGER:
        _raise(ParametricErrorCode.INVALID_VALUE, path)
    if type(value) is float and not math.isfinite(value):
        _raise(ParametricErrorCode.INVALID_VALUE, path)
    if abs(value) > _MAX_ABSOLUTE_VALUE:
        _raise(ParametricErrorCode.INVALID_VALUE, path)
    if value == 0:
        return 0
    if type(value) is float and value.is_integer() and abs(value) <= MAX_SAFE_JSON_INTEGER:
        return int(value)
    return value


def _integer(value: object, path: str, *, minimum: int = 0) -> int:
    if type(value) is not int:
        _raise(ParametricErrorCode.INVALID_TYPE, path)
    if not minimum <= value <= MAX_SAFE_JSON_INTEGER:
        _raise(ParametricErrorCode.INVALID_VALUE, path)
    return value


def _boolean(value: object, path: str) -> bool:
    if type(value) is not bool:
        _raise(ParametricErrorCode.INVALID_TYPE, path)
    return value


def _enum[EnumT: StrEnum](value: object, enum_type: type[EnumT], path: str) -> EnumT:
    if isinstance(value, enum_type):
        return value
    if type(value) is not str:
        _raise(ParametricErrorCode.INVALID_TYPE, path)
    try:
        return enum_type(value)
    except ValueError:
        _raise(ParametricErrorCode.INVALID_VALUE, path)


def _sequence(value: object, path: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, (list, tuple)):
        _raise(ParametricErrorCode.INVALID_TYPE, path)
    return value


def _contract_tuple[ContractT](
    value: object,
    contract_type: type[ContractT],
    path: str,
    *,
    maximum: int,
) -> tuple[ContractT, ...]:
    values = _sequence(value, path)
    if len(values) > maximum:
        _raise(ParametricErrorCode.BUDGET_EXCEEDED, path)
    result: list[ContractT] = []
    for index, item in enumerate(values):
        if not isinstance(item, contract_type):
            _raise(ParametricErrorCode.INVALID_TYPE, join_json_pointer(path, str(index)))
        result.append(item)
    return tuple(result)


def _parse_list[ContractT](
    value: object,
    contract_type: type[ContractT],
    path: str,
    *,
    maximum: int,
) -> tuple[ContractT, ...]:
    values = _sequence(value, path)
    if len(values) > maximum:
        _raise(ParametricErrorCode.BUDGET_EXCEEDED, path)
    parsed: list[ContractT] = []
    for index, item in enumerate(values):
        item_path = join_json_pointer(path, str(index))
        if not isinstance(item, Mapping):
            _raise(ParametricErrorCode.INVALID_TYPE, item_path)
        try:
            parsed.append(contract_type.from_mapping(item))  # type: ignore[attr-defined]
        except ParametricContractError as exc:
            raise ParametricContractError(exc.code, f"{item_path}{exc.path}") from exc
    return tuple(parsed)


def _parse_nested[ContractT](
    value: object,
    contract_type: type[ContractT],
    path: str,
) -> ContractT:
    if not isinstance(value, Mapping):
        _raise(ParametricErrorCode.INVALID_TYPE, path)
    try:
        return contract_type.from_mapping(value)  # type: ignore[attr-defined,no-any-return]
    except ParametricContractError as exc:
        raise ParametricContractError(exc.code, f"{path}{exc.path}") from exc


def _text_tuple(value: object, path: str, *, maximum: int = 128) -> tuple[str, ...]:
    values = _sequence(value, path)
    if len(values) > maximum:
        _raise(ParametricErrorCode.BUDGET_EXCEEDED, path)
    return tuple(
        _text(item, join_json_pointer(path, str(index))) for index, item in enumerate(values)
    )


class _CanonicalIdTuple(tuple[str, ...]):
    """Canonical values plus their original wire indexes for precise errors."""

    source_indexes: Mapping[str, int]

    def __new__(
        cls,
        values: tuple[str, ...],
        source_indexes: Mapping[str, int],
    ) -> _CanonicalIdTuple:
        instance = super().__new__(cls, values)
        instance.source_indexes = MappingProxyType(dict(source_indexes))
        return instance


def _local_id_tuple(
    value: object,
    path: str,
    prefix: str,
    *,
    maximum: int,
    required: bool = False,
) -> tuple[str, ...]:
    values = _text_tuple(value, path, maximum=maximum)
    result = tuple(
        _local_id(item, join_json_pointer(path, str(index)), prefix)
        for index, item in enumerate(values)
    )
    if required and not result:
        _raise(ParametricErrorCode.INVALID_VALUE, path)
    if len(set(result)) != len(result):
        _raise(ParametricErrorCode.DUPLICATE_ID, path)
    return _CanonicalIdTuple(
        tuple(sorted(result)),
        {item: index for index, item in enumerate(result)},
    )


def _source_indexed_ids(values: tuple[str, ...]) -> tuple[tuple[int, str], ...]:
    if isinstance(values, _CanonicalIdTuple):
        return tuple(sorted((values.source_indexes[item], item) for item in values))
    return tuple(enumerate(values))


def _number_tuple(value: object, path: str, *, length: int) -> tuple[int | float, ...]:
    values = _sequence(value, path)
    if len(values) != length:
        _raise(ParametricErrorCode.INVALID_VALUE, path)
    return tuple(
        _number(item, join_json_pointer(path, str(index)))  # type: ignore[arg-type]
        for index, item in enumerate(values)
    )


class DesignUnit(StrEnum):
    MM = "mm"
    DEG = "deg"


class DesignEvidenceStatus(StrEnum):
    """Evidence states that may enter executable design intent."""

    CONFIRMED = "confirmed"
    CALIBRATED = "calibrated"
    CROSS_VIEW_DERIVED = "cross_view_derived"


class DesignEvidenceOrigin(StrEnum):
    """Where an executable design fact came from."""

    USER = "user"
    IMAGE = "image"
    MULTI_VIEW = "multi_view"
    IMPORTED = "imported"
    SYSTEM = "system"


class ParameterKind(StrEnum):
    LENGTH = "length"
    ANGLE = "angle"


class PlaneKind(StrEnum):
    ORIGIN = "origin"
    DATUM = "datum"


class OriginPlane(StrEnum):
    XY = "xy"
    XZ = "xz"
    YZ = "yz"


class GeometryKind(StrEnum):
    POINT = "point"
    LINE = "line"
    CIRCLE = "circle"
    ARC = "arc"
    SLOT = "slot"


class SketchRole(StrEnum):
    PROFILE = "profile"
    HOLE_LOCATIONS = "hole_locations"
    REFERENCE = "reference"


class ReferencePoint(StrEnum):
    WHOLE = "whole"
    START = "start"
    END = "end"
    CENTER = "center"


class ConstraintKind(StrEnum):
    COINCIDENT = "coincident"
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"
    PARALLEL = "parallel"
    PERPENDICULAR = "perpendicular"
    TANGENT = "tangent"
    EQUAL = "equal"
    SYMMETRIC = "symmetric"
    DISTANCE = "distance"
    DISTANCE_X = "distance_x"
    DISTANCE_Y = "distance_y"
    LENGTH = "length"
    RADIUS = "radius"
    DIAMETER = "diameter"
    ANGLE = "angle"


class FeatureKind(StrEnum):
    PAD = "pad"
    POCKET = "pocket"
    REVOLVE = "revolve"
    HOLE = "hole"


class FeatureExtent(StrEnum):
    LENGTH = "length"
    THROUGH_ALL = "through_all"


@dataclass(frozen=True, slots=True, kw_only=True)
class UnitSystem:
    """The deliberately narrow v1 unit system."""

    length: DesignUnit = DesignUnit.MM
    angle: DesignUnit = DesignUnit.DEG
    schema_version: int = PARAMETRIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "length", _enum(self.length, DesignUnit, "/length"))
        object.__setattr__(self, "angle", _enum(self.angle, DesignUnit, "/angle"))
        if self.length is not DesignUnit.MM:
            _raise(ParametricErrorCode.INVALID_VALUE, "/length")
        if self.angle is not DesignUnit.DEG:
            _raise(ParametricErrorCode.INVALID_VALUE, "/angle")

    def to_mapping(self) -> dict[str, int | str]:
        return {
            "schema_version": self.schema_version,
            "length": self.length.value,
            "angle": self.angle.value,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        keys = {"schema_version", "length", "angle"}
        data = _fields(value, allowed=keys, required=keys)
        return cls(
            schema_version=_schema(data["schema_version"]),
            length=data["length"],
            angle=data["angle"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class BodyDefinition:
    """The single authoritative PartDesign body declared by v1."""

    id: str
    name: str
    schema_version: int = PARAMETRIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "id", _local_id(self.id, "/id", "body"))
        object.__setattr__(self, "name", _text(self.name, "/name"))

    def to_mapping(self) -> dict[str, int | str]:
        return {"schema_version": self.schema_version, "id": self.id, "name": self.name}

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        keys = {"schema_version", "id", "name"}
        data = _fields(value, allowed=keys, required=keys)
        return cls(
            schema_version=_schema(data["schema_version"]),
            id=data["id"],
            name=data["name"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DesignEvidence:
    """A confirmed provenance record that may support executable intent."""

    id: str
    status: DesignEvidenceStatus
    origin: DesignEvidenceOrigin
    source_refs: tuple[str, ...]
    description: str | None = None
    schema_version: int = PARAMETRIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "id", _local_id(self.id, "/id", "evidence"))
        object.__setattr__(
            self,
            "status",
            _enum(self.status, DesignEvidenceStatus, "/status"),
        )
        object.__setattr__(
            self,
            "origin",
            _enum(self.origin, DesignEvidenceOrigin, "/origin"),
        )
        refs = _text_tuple(self.source_refs, "/source_refs", maximum=_MAX_SOURCE_REFS)
        if not refs:
            _raise(ParametricErrorCode.INVALID_VALUE, "/source_refs")
        if len(set(refs)) != len(refs):
            _raise(ParametricErrorCode.DUPLICATE_ID, "/source_refs")
        object.__setattr__(self, "source_refs", tuple(sorted(refs)))
        if self.description is not None:
            object.__setattr__(self, "description", _text(self.description, "/description"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "status": self.status.value,
            "origin": self.origin.value,
            "source_refs": list(self.source_refs),
            "description": self.description,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        keys = {"schema_version", "id", "status", "origin", "source_refs", "description"}
        data = _fields(value, allowed=keys, required=keys)
        description = data["description"]
        if description is not None and type(description) is not str:
            _raise(ParametricErrorCode.INVALID_TYPE, "/description")
        return cls(
            schema_version=_schema(data["schema_version"]),
            id=data["id"],
            status=data["status"],
            origin=data["origin"],
            source_refs=_text_tuple(data["source_refs"], "/source_refs", maximum=_MAX_SOURCE_REFS),
            description=description,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DesignParameter:
    """A named, bounded parameter whose value is evidence-backed."""

    id: str
    name: str
    kind: ParameterKind
    value: int | float
    unit: DesignUnit
    evidence_ids: tuple[str, ...]
    minimum: int | float | None = None
    maximum: int | float | None = None
    public: bool = True
    schema_version: int = PARAMETRIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "id", _local_id(self.id, "/id", "parameter"))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        object.__setattr__(self, "kind", _enum(self.kind, ParameterKind, "/kind"))
        object.__setattr__(self, "value", _number(self.value, "/value"))
        object.__setattr__(self, "unit", _enum(self.unit, DesignUnit, "/unit"))
        expected_unit = DesignUnit.MM if self.kind is ParameterKind.LENGTH else DesignUnit.DEG
        if self.unit is not expected_unit:
            _raise(ParametricErrorCode.INVALID_VALUE, "/unit")
        object.__setattr__(
            self,
            "evidence_ids",
            _local_id_tuple(
                self.evidence_ids,
                "/evidence_ids",
                "evidence",
                maximum=_MAX_EVIDENCE_REFS,
                required=True,
            ),
        )
        object.__setattr__(self, "minimum", _number(self.minimum, "/minimum", optional=True))
        object.__setattr__(self, "maximum", _number(self.maximum, "/maximum", optional=True))
        object.__setattr__(self, "public", _boolean(self.public, "/public"))
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            _raise(ParametricErrorCode.INVALID_VALUE, "/minimum")
        if self.minimum is not None and self.value < self.minimum:
            _raise(ParametricErrorCode.INVALID_VALUE, "/value")
        if self.maximum is not None and self.value > self.maximum:
            _raise(ParametricErrorCode.INVALID_VALUE, "/value")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "kind": self.kind.value,
            "value": self.value,
            "unit": self.unit.value,
            "evidence_ids": list(self.evidence_ids),
            "minimum": self.minimum,
            "maximum": self.maximum,
            "public": self.public,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        keys = {
            "schema_version",
            "id",
            "name",
            "kind",
            "value",
            "unit",
            "evidence_ids",
            "minimum",
            "maximum",
            "public",
        }
        data = _fields(value, allowed=keys, required=keys)
        return cls(
            schema_version=_schema(data["schema_version"]),
            id=data["id"],
            name=data["name"],
            kind=data["kind"],
            value=data["value"],
            unit=data["unit"],
            evidence_ids=data["evidence_ids"],
            minimum=data["minimum"],
            maximum=data["maximum"],
            public=data["public"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DatumPlane:
    """An explicit orthonormal local frame, never a generated face attachment."""

    id: str
    name: str
    origin_mm: tuple[int | float, int | float, int | float]
    normal: tuple[int | float, int | float, int | float]
    x_axis: tuple[int | float, int | float, int | float]
    evidence_ids: tuple[str, ...]
    schema_version: int = PARAMETRIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "id", _local_id(self.id, "/id", "datum"))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        origin = _number_tuple(self.origin_mm, "/origin_mm", length=3)
        normal = _number_tuple(self.normal, "/normal", length=3)
        x_axis = _number_tuple(self.x_axis, "/x_axis", length=3)
        if any(abs(float(item)) > 1_000_000 for item in origin):
            _raise(ParametricErrorCode.INVALID_VALUE, "/origin_mm")
        normal_norm = math.sqrt(sum(float(item) ** 2 for item in normal))
        x_axis_norm = math.sqrt(sum(float(item) ** 2 for item in x_axis))
        dot = sum(float(left) * float(right) for left, right in zip(normal, x_axis, strict=True))
        if not math.isclose(normal_norm, 1.0, rel_tol=0.0, abs_tol=1e-9):
            _raise(ParametricErrorCode.INVALID_VALUE, "/normal")
        if not math.isclose(x_axis_norm, 1.0, rel_tol=0.0, abs_tol=1e-9):
            _raise(ParametricErrorCode.INVALID_VALUE, "/x_axis")
        if not math.isclose(dot, 0.0, rel_tol=0.0, abs_tol=1e-9):
            _raise(ParametricErrorCode.INVALID_VALUE, "/x_axis")
        object.__setattr__(self, "origin_mm", origin)
        object.__setattr__(self, "normal", normal)
        object.__setattr__(self, "x_axis", x_axis)
        object.__setattr__(
            self,
            "evidence_ids",
            _local_id_tuple(
                self.evidence_ids,
                "/evidence_ids",
                "evidence",
                maximum=_MAX_EVIDENCE_REFS,
                required=True,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "origin_mm": list(self.origin_mm),
            "normal": list(self.normal),
            "x_axis": list(self.x_axis),
            "evidence_ids": list(self.evidence_ids),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        keys = {
            "schema_version",
            "id",
            "name",
            "origin_mm",
            "normal",
            "x_axis",
            "evidence_ids",
        }
        data = _fields(value, allowed=keys, required=keys)
        return cls(
            schema_version=_schema(data["schema_version"]),
            id=data["id"],
            name=data["name"],
            origin_mm=_number_tuple(data["origin_mm"], "/origin_mm", length=3),
            normal=_number_tuple(data["normal"], "/normal", length=3),
            x_axis=_number_tuple(data["x_axis"], "/x_axis", length=3),
            evidence_ids=data["evidence_ids"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchPlane:
    """A strict origin-plane or explicit-datum reference."""

    kind: PlaneKind
    origin: OriginPlane | None = None
    datum_id: str | None = None
    schema_version: int = PARAMETRIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "kind", _enum(self.kind, PlaneKind, "/kind"))
        if self.kind is PlaneKind.ORIGIN:
            if self.origin is None or self.datum_id is not None:
                _raise(ParametricErrorCode.INVALID_VALUE)
            object.__setattr__(self, "origin", _enum(self.origin, OriginPlane, "/origin"))
        else:
            if self.origin is not None or self.datum_id is None:
                _raise(ParametricErrorCode.INVALID_VALUE)
            object.__setattr__(
                self,
                "datum_id",
                _local_id(self.datum_id, "/datum_id", "datum"),
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "origin": None if self.origin is None else self.origin.value,
            "datum_id": self.datum_id,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        keys = {"schema_version", "kind", "origin", "datum_id"}
        data = _fields(value, allowed=keys, required=keys)
        return cls(
            schema_version=_schema(data["schema_version"]),
            kind=data["kind"],
            origin=data["origin"],
            datum_id=data["datum_id"],
        )


_GEOMETRY_DIMENSIONS: dict[GeometryKind, tuple[str, ...]] = {
    GeometryKind.POINT: ("x_mm", "y_mm"),
    GeometryKind.LINE: ("x1_mm", "y1_mm", "x2_mm", "y2_mm"),
    GeometryKind.CIRCLE: ("cx_mm", "cy_mm", "radius_mm"),
    GeometryKind.ARC: (
        "cx_mm",
        "cy_mm",
        "radius_mm",
        "start_angle_deg",
        "sweep_angle_deg",
    ),
    GeometryKind.SLOT: ("x1_mm", "y1_mm", "x2_mm", "y2_mm", "width_mm"),
}
_POSITIVE_DIMENSIONS = frozenset({"radius_mm", "width_mm"})


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchGeometry:
    """One closed-shape sketch primitive with stable IR-local identity."""

    id: str
    kind: GeometryKind
    dimensions: Mapping[str, int | float]
    construction: bool = False
    evidence_ids: tuple[str, ...] = ()
    schema_version: int = PARAMETRIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "id", _local_id(self.id, "/id", "geometry"))
        object.__setattr__(self, "kind", _enum(self.kind, GeometryKind, "/kind"))
        if not isinstance(self.dimensions, Mapping):
            _raise(ParametricErrorCode.INVALID_TYPE, "/dimensions")
        try:
            dimensions = dict(self.dimensions)
        except Exception:
            _raise(ParametricErrorCode.INVALID_VALUE, "/dimensions")
        expected = set(_GEOMETRY_DIMENSIONS[self.kind])
        if set(dimensions) != expected or not all(type(key) is str for key in dimensions):
            _raise(ParametricErrorCode.INVALID_VALUE, "/dimensions")
        frozen: dict[str, int | float] = {}
        for name in _GEOMETRY_DIMENSIONS[self.kind]:
            path = join_json_pointer("/dimensions", name)
            number = _number(dimensions[name], path)
            assert number is not None
            if name in _POSITIVE_DIMENSIONS and number <= 0:
                _raise(ParametricErrorCode.INVALID_VALUE, path)
            if name == "sweep_angle_deg" and not 0 < number < 360:
                _raise(ParametricErrorCode.INVALID_VALUE, join_json_pointer("/dimensions", name))
            if name.endswith("_mm") and abs(number) > 1_000_000:
                _raise(ParametricErrorCode.INVALID_VALUE, path)
            frozen[name] = number
        object.__setattr__(self, "dimensions", MappingProxyType(frozen))
        if self.kind is GeometryKind.LINE and (
            frozen["x1_mm"],
            frozen["y1_mm"],
        ) == (frozen["x2_mm"], frozen["y2_mm"]):
            _raise(ParametricErrorCode.INVALID_VALUE, "/dimensions")
        if self.kind is GeometryKind.SLOT and (
            frozen["x1_mm"],
            frozen["y1_mm"],
        ) == (frozen["x2_mm"], frozen["y2_mm"]):
            _raise(ParametricErrorCode.INVALID_VALUE, "/dimensions")
        if self.kind is GeometryKind.ARC and not 0 <= frozen["start_angle_deg"] < 360:
            _raise(ParametricErrorCode.INVALID_VALUE, "/dimensions/start_angle_deg")
        object.__setattr__(self, "construction", _boolean(self.construction, "/construction"))
        object.__setattr__(
            self,
            "evidence_ids",
            _local_id_tuple(
                self.evidence_ids,
                "/evidence_ids",
                "evidence",
                maximum=_MAX_EVIDENCE_REFS,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": self.kind.value,
            "dimensions": dict(self.dimensions),
            "construction": self.construction,
            "evidence_ids": list(self.evidence_ids),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        keys = {
            "schema_version",
            "id",
            "kind",
            "dimensions",
            "construction",
            "evidence_ids",
        }
        data = _fields(value, allowed=keys, required=keys)
        dimensions_value = data["dimensions"]
        if not isinstance(dimensions_value, Mapping):
            _raise(ParametricErrorCode.INVALID_TYPE, "/dimensions")
        dimensions: dict[str, int | float] = {}
        try:
            names = tuple(dimensions_value)
        except Exception:
            _raise(ParametricErrorCode.INVALID_VALUE, "/dimensions")
        if not all(type(name) is str for name in names):
            _raise(ParametricErrorCode.INVALID_TYPE, "/dimensions")
        for name in names:
            path = _safe_path("/dimensions", name)
            try:
                item = dimensions_value[name]
            except Exception:
                _raise(ParametricErrorCode.INVALID_VALUE, path)
            number = _number(item, path)
            assert number is not None
            dimensions[name] = number
        return cls(
            schema_version=_schema(data["schema_version"]),
            id=data["id"],
            kind=data["kind"],
            dimensions=dimensions,
            construction=data["construction"],
            evidence_ids=data["evidence_ids"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchReference:
    """A typed reference to sketch geometry or one of three local axes."""

    target: str
    point: ReferencePoint
    schema_version: int = PARAMETRIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        target = _text(self.target, "/target")
        if target not in {"@origin", "@x_axis", "@y_axis"}:
            target = _local_id(target, "/target", "geometry")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "point", _enum(self.point, ReferencePoint, "/point"))
        if target in {"@x_axis", "@y_axis"} and self.point is not ReferencePoint.WHOLE:
            _raise(ParametricErrorCode.INVALID_VALUE, "/point")
        if target == "@origin" and self.point not in {ReferencePoint.CENTER, ReferencePoint.WHOLE}:
            _raise(ParametricErrorCode.INVALID_VALUE, "/point")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "target": self.target,
            "point": self.point.value,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        keys = {"schema_version", "target", "point"}
        data = _fields(value, allowed=keys, required=keys)
        return cls(
            schema_version=_schema(data["schema_version"]),
            target=data["target"],
            point=data["point"],
        )


_CONSTRAINT_CARDINALITY: dict[ConstraintKind, int] = {
    ConstraintKind.COINCIDENT: 2,
    ConstraintKind.HORIZONTAL: 1,
    ConstraintKind.VERTICAL: 1,
    ConstraintKind.PARALLEL: 2,
    ConstraintKind.PERPENDICULAR: 2,
    ConstraintKind.TANGENT: 2,
    ConstraintKind.EQUAL: 2,
    ConstraintKind.SYMMETRIC: 3,
    ConstraintKind.DISTANCE: 2,
    ConstraintKind.DISTANCE_X: 2,
    ConstraintKind.DISTANCE_Y: 2,
    ConstraintKind.LENGTH: 1,
    ConstraintKind.RADIUS: 1,
    ConstraintKind.DIAMETER: 1,
    ConstraintKind.ANGLE: 2,
}
_DIMENSIONAL_CONSTRAINT_UNITS: dict[ConstraintKind, DesignUnit] = {
    ConstraintKind.DISTANCE: DesignUnit.MM,
    ConstraintKind.DISTANCE_X: DesignUnit.MM,
    ConstraintKind.DISTANCE_Y: DesignUnit.MM,
    ConstraintKind.LENGTH: DesignUnit.MM,
    ConstraintKind.RADIUS: DesignUnit.MM,
    ConstraintKind.DIAMETER: DesignUnit.MM,
    ConstraintKind.ANGLE: DesignUnit.DEG,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchConstraint:
    """A closed sketch constraint with explicit reference cardinality."""

    id: str
    kind: ConstraintKind
    references: tuple[SketchReference, ...]
    parameter_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    schema_version: int = PARAMETRIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "id", _local_id(self.id, "/id", "constraint"))
        object.__setattr__(self, "kind", _enum(self.kind, ConstraintKind, "/kind"))
        references = _contract_tuple(
            self.references,
            SketchReference,
            "/references",
            maximum=3,
        )
        object.__setattr__(self, "references", references)
        if len(references) != _CONSTRAINT_CARDINALITY[self.kind]:
            _raise(ParametricErrorCode.INVALID_VALUE, "/references")
        dimensional = self.kind in _DIMENSIONAL_CONSTRAINT_UNITS
        if dimensional and self.parameter_id is None:
            _raise(ParametricErrorCode.INVALID_VALUE, "/parameter_id")
        if not dimensional and self.parameter_id is not None:
            _raise(ParametricErrorCode.INVALID_VALUE, "/parameter_id")
        if self.parameter_id is not None:
            object.__setattr__(
                self,
                "parameter_id",
                _local_id(self.parameter_id, "/parameter_id", "parameter"),
            )
        object.__setattr__(
            self,
            "evidence_ids",
            _local_id_tuple(
                self.evidence_ids,
                "/evidence_ids",
                "evidence",
                maximum=_MAX_EVIDENCE_REFS,
                required=dimensional,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": self.kind.value,
            "references": [item.to_mapping() for item in self.references],
            "parameter_id": self.parameter_id,
            "evidence_ids": list(self.evidence_ids),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        keys = {
            "schema_version",
            "id",
            "kind",
            "references",
            "parameter_id",
            "evidence_ids",
        }
        data = _fields(value, allowed=keys, required=keys)
        return cls(
            schema_version=_schema(data["schema_version"]),
            id=data["id"],
            kind=data["kind"],
            references=_parse_list(
                data["references"],
                SketchReference,
                "/references",
                maximum=3,
            ),
            parameter_id=data["parameter_id"],
            evidence_ids=data["evidence_ids"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ParametricSketch:
    """An origin-plane sketch with bounded geometry and constraints."""

    id: str
    name: str
    role: SketchRole
    plane: SketchPlane
    geometries: tuple[SketchGeometry, ...]
    constraints: tuple[SketchConstraint, ...]
    evidence_ids: tuple[str, ...] = ()
    schema_version: int = PARAMETRIC_SCHEMA_VERSION
    _geometry_source_indexes: Mapping[str, int] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _constraint_source_indexes: Mapping[str, int] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "id", _local_id(self.id, "/id", "sketch"))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        object.__setattr__(self, "role", _enum(self.role, SketchRole, "/role"))
        if not isinstance(self.plane, SketchPlane):
            _raise(ParametricErrorCode.INVALID_TYPE, "/plane")
        geometries = _contract_tuple(
            self.geometries,
            SketchGeometry,
            "/geometries",
            maximum=MAX_SKETCH_GEOMETRIES,
        )
        if not geometries:
            _raise(ParametricErrorCode.INVALID_VALUE, "/geometries")
        constraints = _contract_tuple(
            self.constraints,
            SketchConstraint,
            "/constraints",
            maximum=MAX_SKETCH_CONSTRAINTS,
        )
        seen: set[str] = set()
        for index, geometry in enumerate(geometries):
            if geometry.id in seen:
                _raise(
                    ParametricErrorCode.DUPLICATE_ID,
                    f"/geometries/{index}/id",
                )
            seen.add(geometry.id)
        geometry_by_id = {item.id: item for item in geometries}
        for index, constraint in enumerate(constraints):
            if constraint.id in seen:
                _raise(
                    ParametricErrorCode.DUPLICATE_ID,
                    f"/constraints/{index}/id",
                )
            seen.add(constraint.id)
            for ref_index, reference in enumerate(constraint.references):
                if reference.target.startswith("@"):
                    continue
                geometry = geometry_by_id.get(reference.target)
                if geometry is None:
                    _raise(
                        ParametricErrorCode.UNKNOWN_REFERENCE,
                        f"/constraints/{index}/references/{ref_index}/target",
                    )
                allowed_points = {
                    GeometryKind.POINT: {ReferencePoint.WHOLE, ReferencePoint.CENTER},
                    GeometryKind.LINE: {
                        ReferencePoint.WHOLE,
                        ReferencePoint.START,
                        ReferencePoint.END,
                    },
                    GeometryKind.CIRCLE: {ReferencePoint.WHOLE, ReferencePoint.CENTER},
                    GeometryKind.ARC: {
                        ReferencePoint.WHOLE,
                        ReferencePoint.START,
                        ReferencePoint.END,
                        ReferencePoint.CENTER,
                    },
                    GeometryKind.SLOT: {ReferencePoint.WHOLE, ReferencePoint.CENTER},
                }[geometry.kind]
                if reference.point not in allowed_points:
                    _raise(
                        ParametricErrorCode.INVALID_VALUE,
                        f"/constraints/{index}/references/{ref_index}/point",
                    )
            if constraint.kind in {ConstraintKind.HORIZONTAL, ConstraintKind.VERTICAL}:
                target = geometry_by_id.get(constraint.references[0].target)
                if target is None or target.kind is not GeometryKind.LINE:
                    _raise(
                        ParametricErrorCode.INVALID_VALUE,
                        f"/constraints/{index}/references/0/target",
                    )
                if constraint.references[0].point is not ReferencePoint.WHOLE:
                    _raise(
                        ParametricErrorCode.INVALID_VALUE,
                        f"/constraints/{index}/references/0/point",
                    )
            if constraint.kind is ConstraintKind.LENGTH:
                target = geometry_by_id.get(constraint.references[0].target)
                if (
                    target is None
                    or target.kind is not GeometryKind.LINE
                    or constraint.references[0].point is not ReferencePoint.WHOLE
                ):
                    _raise(
                        ParametricErrorCode.INVALID_VALUE,
                        f"/constraints/{index}/references/0/target",
                    )
            if constraint.kind in {ConstraintKind.RADIUS, ConstraintKind.DIAMETER}:
                target = geometry_by_id.get(constraint.references[0].target)
                if target is None or target.kind not in {GeometryKind.CIRCLE, GeometryKind.ARC}:
                    _raise(
                        ParametricErrorCode.INVALID_VALUE,
                        f"/constraints/{index}/references/0/target",
                    )
                if constraint.references[0].point is not ReferencePoint.WHOLE:
                    _raise(
                        ParametricErrorCode.INVALID_VALUE,
                        f"/constraints/{index}/references/0/point",
                    )
            if constraint.kind is ConstraintKind.COINCIDENT and any(
                item.point is ReferencePoint.WHOLE for item in constraint.references
            ):
                _raise(ParametricErrorCode.INVALID_VALUE, f"/constraints/{index}/references")
            if constraint.kind in {
                ConstraintKind.DISTANCE,
                ConstraintKind.DISTANCE_X,
                ConstraintKind.DISTANCE_Y,
            } and any(item.point is ReferencePoint.WHOLE for item in constraint.references):
                _raise(ParametricErrorCode.INVALID_VALUE, f"/constraints/{index}/references")
            if constraint.kind is ConstraintKind.ANGLE:
                for ref_index, reference in enumerate(constraint.references):
                    target = geometry_by_id.get(reference.target)
                    if (
                        target is None
                        or target.kind is not GeometryKind.LINE
                        or reference.point is not ReferencePoint.WHOLE
                    ):
                        _raise(
                            ParametricErrorCode.INVALID_VALUE,
                            f"/constraints/{index}/references/{ref_index}/target",
                        )
            if constraint.kind in {
                ConstraintKind.PARALLEL,
                ConstraintKind.PERPENDICULAR,
            }:
                for ref_index, reference in enumerate(constraint.references):
                    target = geometry_by_id.get(reference.target)
                    if (
                        target is None
                        or target.kind is not GeometryKind.LINE
                        or reference.point is not ReferencePoint.WHOLE
                    ):
                        _raise(
                            ParametricErrorCode.INVALID_VALUE,
                            f"/constraints/{index}/references/{ref_index}/target",
                        )
            if constraint.kind is ConstraintKind.TANGENT:
                targets = [
                    geometry_by_id.get(reference.target) for reference in constraint.references
                ]
                for ref_index, (reference, target) in enumerate(
                    zip(constraint.references, targets, strict=True)
                ):
                    if (
                        target is None
                        or target.kind
                        not in {GeometryKind.LINE, GeometryKind.CIRCLE, GeometryKind.ARC}
                        or reference.point is not ReferencePoint.WHOLE
                    ):
                        _raise(
                            ParametricErrorCode.INVALID_VALUE,
                            f"/constraints/{index}/references/{ref_index}/target",
                        )
                if all(target.kind is GeometryKind.LINE for target in targets if target):
                    _raise(
                        ParametricErrorCode.INVALID_VALUE,
                        f"/constraints/{index}/references",
                    )
            if constraint.kind is ConstraintKind.EQUAL:
                targets = [
                    geometry_by_id.get(reference.target) for reference in constraint.references
                ]
                for ref_index, (reference, target) in enumerate(
                    zip(constraint.references, targets, strict=True)
                ):
                    if target is None or reference.point is not ReferencePoint.WHOLE:
                        _raise(
                            ParametricErrorCode.INVALID_VALUE,
                            f"/constraints/{index}/references/{ref_index}/target",
                        )
                kinds = {target.kind for target in targets if target is not None}
                if kinds != {GeometryKind.LINE} and not kinds <= {
                    GeometryKind.CIRCLE,
                    GeometryKind.ARC,
                }:
                    _raise(
                        ParametricErrorCode.INVALID_VALUE,
                        f"/constraints/{index}/references",
                    )
            if constraint.kind is ConstraintKind.SYMMETRIC:
                for ref_index, reference in enumerate(constraint.references[:2]):
                    target = geometry_by_id.get(reference.target)
                    point_anchor = reference.target == "@origin" or (
                        target is not None
                        and (
                            target.kind is GeometryKind.POINT
                            or reference.point is not ReferencePoint.WHOLE
                        )
                    )
                    if not point_anchor or reference.target in {"@x_axis", "@y_axis"}:
                        _raise(
                            ParametricErrorCode.INVALID_VALUE,
                            f"/constraints/{index}/references/{ref_index}/target",
                        )
                axis_reference = constraint.references[2]
                axis_geometry = geometry_by_id.get(axis_reference.target)
                valid_axis = axis_reference.target in {"@x_axis", "@y_axis"} or (
                    axis_geometry is not None
                    and axis_geometry.kind is GeometryKind.LINE
                    and axis_geometry.construction
                )
                if not valid_axis or axis_reference.point is not ReferencePoint.WHOLE:
                    _raise(
                        ParametricErrorCode.INVALID_VALUE,
                        f"/constraints/{index}/references/2/target",
                    )
        if self.role is SketchRole.PROFILE and not any(
            not item.construction for item in geometries
        ):
            _raise(ParametricErrorCode.INVALID_VALUE, "/geometries")
        object.__setattr__(
            self,
            "_geometry_source_indexes",
            MappingProxyType({item.id: index for index, item in enumerate(geometries)}),
        )
        object.__setattr__(
            self,
            "_constraint_source_indexes",
            MappingProxyType({item.id: index for index, item in enumerate(constraints)}),
        )
        object.__setattr__(self, "geometries", tuple(sorted(geometries, key=lambda item: item.id)))
        object.__setattr__(
            self,
            "constraints",
            tuple(sorted(constraints, key=lambda item: item.id)),
        )
        object.__setattr__(
            self,
            "evidence_ids",
            _local_id_tuple(
                self.evidence_ids,
                "/evidence_ids",
                "evidence",
                maximum=_MAX_EVIDENCE_REFS,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "role": self.role.value,
            "plane": self.plane.to_mapping(),
            "geometries": [item.to_mapping() for item in self.geometries],
            "constraints": [item.to_mapping() for item in self.constraints],
            "evidence_ids": list(self.evidence_ids),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        keys = {
            "schema_version",
            "id",
            "name",
            "role",
            "plane",
            "geometries",
            "constraints",
            "evidence_ids",
        }
        data = _fields(value, allowed=keys, required=keys)
        return cls(
            schema_version=_schema(data["schema_version"]),
            id=data["id"],
            name=data["name"],
            role=data["role"],
            plane=_parse_nested(data["plane"], SketchPlane, "/plane"),
            geometries=_parse_list(
                data["geometries"],
                SketchGeometry,
                "/geometries",
                maximum=MAX_SKETCH_GEOMETRIES,
            ),
            constraints=_parse_list(
                data["constraints"],
                SketchConstraint,
                "/constraints",
                maximum=MAX_SKETCH_CONSTRAINTS,
            ),
            evidence_ids=data["evidence_ids"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignFeature:
    """A bounded single-body PartDesign feature declaration."""

    id: str
    name: str
    kind: FeatureKind
    sketch_id: str
    base_feature_id: str | None
    parameters: Mapping[str, str]
    evidence_ids: tuple[str, ...]
    extent: FeatureExtent | None = None
    axis: str | None = None
    location_geometry_ids: tuple[str, ...] = ()
    reversed: bool = False
    symmetric: bool = False
    schema_version: int = PARAMETRIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "id", _local_id(self.id, "/id", "feature"))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        object.__setattr__(self, "kind", _enum(self.kind, FeatureKind, "/kind"))
        object.__setattr__(self, "sketch_id", _local_id(self.sketch_id, "/sketch_id", "sketch"))
        if self.base_feature_id is not None:
            object.__setattr__(
                self,
                "base_feature_id",
                _local_id(self.base_feature_id, "/base_feature_id", "feature"),
            )
        if not isinstance(self.parameters, Mapping):
            _raise(ParametricErrorCode.INVALID_TYPE, "/parameters")
        try:
            parameters = dict(self.parameters)
        except Exception:
            _raise(ParametricErrorCode.INVALID_VALUE, "/parameters")
        extent = None
        if self.extent is not None:
            extent = _enum(self.extent, FeatureExtent, "/extent")
        expected_parameters: dict[str, DesignUnit]
        if self.kind is FeatureKind.PAD:
            expected_parameters = {"length": DesignUnit.MM}
            if extent is not FeatureExtent.LENGTH:
                _raise(ParametricErrorCode.INVALID_VALUE, "/extent")
        elif self.kind is FeatureKind.POCKET:
            if extent is None:
                _raise(ParametricErrorCode.INVALID_VALUE, "/extent")
            expected_parameters = (
                {"length": DesignUnit.MM} if extent is FeatureExtent.LENGTH else {}
            )
        elif self.kind is FeatureKind.HOLE:
            if extent is None:
                _raise(ParametricErrorCode.INVALID_VALUE, "/extent")
            expected_parameters = {"diameter": DesignUnit.MM}
            if extent is FeatureExtent.LENGTH:
                expected_parameters["depth"] = DesignUnit.MM
        else:
            expected_parameters = {"angle": DesignUnit.DEG}
            if extent is not None:
                _raise(ParametricErrorCode.INVALID_VALUE, "/extent")
        if set(parameters) != set(expected_parameters):
            _raise(ParametricErrorCode.INVALID_VALUE, "/parameters")
        frozen: dict[str, str] = {}
        for name in expected_parameters:
            if type(name) is not str or type(parameters[name]) is not str:
                _raise(ParametricErrorCode.INVALID_TYPE, _safe_path("/parameters", name))
            frozen[name] = _local_id(parameters[name], _safe_path("/parameters", name), "parameter")
        object.__setattr__(self, "parameters", MappingProxyType(frozen))
        object.__setattr__(self, "extent", extent)
        if self.kind is FeatureKind.REVOLVE:
            if self.axis not in {"@sketch_x", "@sketch_y"}:
                if self.axis is None:
                    _raise(ParametricErrorCode.INVALID_VALUE, "/axis")
                object.__setattr__(self, "axis", _local_id(self.axis, "/axis", "geometry"))
        elif self.axis is not None:
            _raise(ParametricErrorCode.INVALID_VALUE, "/axis")
        locations = _local_id_tuple(
            self.location_geometry_ids,
            "/location_geometry_ids",
            "geometry",
            maximum=128,
            required=self.kind is FeatureKind.HOLE,
        )
        if self.kind is not FeatureKind.HOLE and locations:
            _raise(ParametricErrorCode.INVALID_VALUE, "/location_geometry_ids")
        object.__setattr__(self, "location_geometry_ids", locations)
        object.__setattr__(
            self,
            "evidence_ids",
            _local_id_tuple(
                self.evidence_ids,
                "/evidence_ids",
                "evidence",
                maximum=_MAX_EVIDENCE_REFS,
                required=True,
            ),
        )
        object.__setattr__(self, "reversed", _boolean(self.reversed, "/reversed"))
        object.__setattr__(self, "symmetric", _boolean(self.symmetric, "/symmetric"))
        if self.kind is FeatureKind.HOLE and self.symmetric:
            _raise(ParametricErrorCode.INVALID_VALUE, "/symmetric")
        if self.kind is FeatureKind.REVOLVE and self.axis is None:
            _raise(ParametricErrorCode.INVALID_VALUE, "/axis")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "kind": self.kind.value,
            "sketch_id": self.sketch_id,
            "base_feature_id": self.base_feature_id,
            "parameters": dict(self.parameters),
            "evidence_ids": list(self.evidence_ids),
            "extent": None if self.extent is None else self.extent.value,
            "axis": self.axis,
            "location_geometry_ids": list(self.location_geometry_ids),
            "reversed": self.reversed,
            "symmetric": self.symmetric,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        keys = {
            "schema_version",
            "id",
            "name",
            "kind",
            "sketch_id",
            "base_feature_id",
            "parameters",
            "evidence_ids",
            "extent",
            "axis",
            "location_geometry_ids",
            "reversed",
            "symmetric",
        }
        data = _fields(value, allowed=keys, required=keys)
        parameters = data["parameters"]
        if not isinstance(parameters, Mapping):
            _raise(ParametricErrorCode.INVALID_TYPE, "/parameters")
        try:
            copied_parameters = dict(parameters)
        except Exception:
            _raise(ParametricErrorCode.INVALID_VALUE, "/parameters")
        return cls(
            schema_version=_schema(data["schema_version"]),
            id=data["id"],
            name=data["name"],
            kind=data["kind"],
            sketch_id=data["sketch_id"],
            base_feature_id=data["base_feature_id"],
            parameters=copied_parameters,
            evidence_ids=data["evidence_ids"],
            extent=data["extent"],
            axis=data["axis"],
            location_geometry_ids=data["location_geometry_ids"],
            reversed=data["reversed"],
            symmetric=data["symmetric"],
        )


def _json_node_count(value: object) -> int:
    if isinstance(value, Mapping):
        return 1 + sum(1 + _json_node_count(item) for item in value.values())
    if isinstance(value, list):
        return 1 + sum(_json_node_count(item) for item in value)
    return 1


@dataclass(frozen=True, slots=True, kw_only=True)
class ParametricDesignIR:
    """Closed v1 editable intent; executable geometry remains a compiler invariant."""

    id: str
    name: str
    units: UnitSystem
    body: BodyDefinition
    evidence: tuple[DesignEvidence, ...]
    parameters: tuple[DesignParameter, ...]
    datum_planes: tuple[DatumPlane, ...]
    sketches: tuple[ParametricSketch, ...]
    features: tuple[PartDesignFeature, ...]
    schema_version: int = PARAMETRIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "id", _local_id(self.id, "/id", "design"))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        if not isinstance(self.units, UnitSystem):
            _raise(ParametricErrorCode.INVALID_TYPE, "/units")
        if not isinstance(self.body, BodyDefinition):
            _raise(ParametricErrorCode.INVALID_TYPE, "/body")
        evidence = _contract_tuple(
            self.evidence,
            DesignEvidence,
            "/evidence",
            maximum=MAX_DESIGN_EVIDENCE,
        )
        parameters = _contract_tuple(
            self.parameters,
            DesignParameter,
            "/parameters",
            maximum=MAX_DESIGN_PARAMETERS,
        )
        datum_planes = _contract_tuple(
            self.datum_planes,
            DatumPlane,
            "/datum_planes",
            maximum=MAX_DATUM_PLANES,
        )
        sketches = _contract_tuple(
            self.sketches,
            ParametricSketch,
            "/sketches",
            maximum=MAX_DESIGN_SKETCHES,
        )
        features = _contract_tuple(
            self.features,
            PartDesignFeature,
            "/features",
            maximum=MAX_DESIGN_FEATURES,
        )
        if not evidence:
            _raise(ParametricErrorCode.INVALID_VALUE, "/evidence")
        if not parameters:
            _raise(ParametricErrorCode.INVALID_VALUE, "/parameters")
        if not sketches:
            _raise(ParametricErrorCode.INVALID_VALUE, "/sketches")
        if not features:
            _raise(ParametricErrorCode.INVALID_VALUE, "/features")
        object.__setattr__(self, "evidence", tuple(sorted(evidence, key=lambda item: item.id)))
        object.__setattr__(
            self,
            "parameters",
            tuple(sorted(parameters, key=lambda item: item.id)),
        )
        object.__setattr__(
            self,
            "datum_planes",
            tuple(sorted(datum_planes, key=lambda item: item.id)),
        )
        object.__setattr__(self, "sketches", tuple(sorted(sketches, key=lambda item: item.id)))
        object.__setattr__(self, "features", features)

        seen: set[str] = {self.id, self.body.id}
        evidence_ids: set[str] = set()
        for index, item in enumerate(evidence):
            if item.id in seen:
                _raise(ParametricErrorCode.DUPLICATE_ID, f"/evidence/{index}/id")
            seen.add(item.id)
            evidence_ids.add(item.id)

        def require_evidence(references: tuple[str, ...], path: str) -> None:
            for index, reference in _source_indexed_ids(references):
                if reference not in evidence_ids:
                    _raise(
                        ParametricErrorCode.UNKNOWN_REFERENCE,
                        f"{path}/{index}",
                    )

        parameter_by_id: dict[str, DesignParameter] = {}
        public_names: set[str] = set()
        for index, parameter in enumerate(parameters):
            if parameter.id in seen:
                _raise(ParametricErrorCode.DUPLICATE_ID, f"/parameters/{index}/id")
            seen.add(parameter.id)
            parameter_by_id[parameter.id] = parameter
            require_evidence(parameter.evidence_ids, f"/parameters/{index}/evidence_ids")
            if parameter.public:
                canonical_name = parameter.name.casefold()
                if canonical_name in public_names:
                    _raise(ParametricErrorCode.DUPLICATE_ID, f"/parameters/{index}/name")
                public_names.add(canonical_name)

        datum_by_id: dict[str, DatumPlane] = {}
        for index, datum in enumerate(datum_planes):
            if datum.id in seen:
                _raise(ParametricErrorCode.DUPLICATE_ID, f"/datum_planes/{index}/id")
            seen.add(datum.id)
            datum_by_id[datum.id] = datum
            require_evidence(datum.evidence_ids, f"/datum_planes/{index}/evidence_ids")

        sketch_by_id: dict[str, ParametricSketch] = {}
        total_geometries = 0
        total_constraints = 0
        for sketch_index, sketch in enumerate(sketches):
            if sketch.id in seen:
                _raise(ParametricErrorCode.DUPLICATE_ID, f"/sketches/{sketch_index}/id")
            seen.add(sketch.id)
            sketch_by_id[sketch.id] = sketch
            require_evidence(sketch.evidence_ids, f"/sketches/{sketch_index}/evidence_ids")
            if sketch.plane.kind is PlaneKind.DATUM and sketch.plane.datum_id not in datum_by_id:
                _raise(
                    ParametricErrorCode.UNKNOWN_REFERENCE,
                    f"/sketches/{sketch_index}/plane/datum_id",
                )
            total_geometries += len(sketch.geometries)
            total_constraints += len(sketch.constraints)
            for geometry in sketch.geometries:
                geometry_index = sketch._geometry_source_indexes[geometry.id]
                if geometry.id in seen:
                    _raise(
                        ParametricErrorCode.DUPLICATE_ID,
                        f"/sketches/{sketch_index}/geometries/{geometry_index}/id",
                    )
                seen.add(geometry.id)
                require_evidence(
                    geometry.evidence_ids,
                    f"/sketches/{sketch_index}/geometries/{geometry_index}/evidence_ids",
                )
            for constraint in sketch.constraints:
                constraint_index = sketch._constraint_source_indexes[constraint.id]
                if constraint.id in seen:
                    _raise(
                        ParametricErrorCode.DUPLICATE_ID,
                        f"/sketches/{sketch_index}/constraints/{constraint_index}/id",
                    )
                seen.add(constraint.id)
                require_evidence(
                    constraint.evidence_ids,
                    f"/sketches/{sketch_index}/constraints/{constraint_index}/evidence_ids",
                )
                if constraint.parameter_id is not None:
                    path = f"/sketches/{sketch_index}/constraints/{constraint_index}/parameter_id"
                    parameter = parameter_by_id.get(constraint.parameter_id)
                    if parameter is None:
                        _raise(ParametricErrorCode.UNKNOWN_REFERENCE, path)
                    if parameter.unit is not _DIMENSIONAL_CONSTRAINT_UNITS[constraint.kind]:
                        _raise(ParametricErrorCode.INVALID_VALUE, path)
                    if (
                        constraint.kind
                        in {
                            ConstraintKind.DISTANCE,
                            ConstraintKind.LENGTH,
                            ConstraintKind.RADIUS,
                            ConstraintKind.DIAMETER,
                        }
                        and parameter.value <= 0
                    ):
                        _raise(ParametricErrorCode.INVALID_VALUE, path)
                    if constraint.kind is ConstraintKind.ANGLE and not (0 < parameter.value < 360):
                        _raise(ParametricErrorCode.INVALID_VALUE, path)
        if total_geometries > _MAX_TOTAL_GEOMETRIES:
            _raise(ParametricErrorCode.BUDGET_EXCEEDED, "/sketches")
        if total_constraints > _MAX_TOTAL_CONSTRAINTS:
            _raise(ParametricErrorCode.BUDGET_EXCEEDED, "/sketches")

        if features[0].kind not in {FeatureKind.PAD, FeatureKind.REVOLVE}:
            _raise(ParametricErrorCode.INVALID_ORDER, "/features/0/kind")
        consumed_sketches: set[str] = set()
        for index, feature in enumerate(features):
            if feature.id in seen:
                _raise(ParametricErrorCode.DUPLICATE_ID, f"/features/{index}/id")
            seen.add(feature.id)
            expected_base = None if index == 0 else features[index - 1].id
            if feature.base_feature_id != expected_base:
                _raise(ParametricErrorCode.INVALID_ORDER, f"/features/{index}/base_feature_id")
            sketch = sketch_by_id.get(feature.sketch_id)
            if sketch is None:
                _raise(ParametricErrorCode.UNKNOWN_REFERENCE, f"/features/{index}/sketch_id")
            expected_role = (
                SketchRole.HOLE_LOCATIONS
                if feature.kind is FeatureKind.HOLE
                else SketchRole.PROFILE
            )
            if sketch.role is not expected_role:
                _raise(ParametricErrorCode.INVALID_VALUE, f"/features/{index}/sketch_id")
            if sketch.id in consumed_sketches:
                _raise(ParametricErrorCode.INVALID_VALUE, f"/features/{index}/sketch_id")
            consumed_sketches.add(sketch.id)
            require_evidence(feature.evidence_ids, f"/features/{index}/evidence_ids")
            for name, parameter_id in feature.parameters.items():
                parameter = parameter_by_id.get(parameter_id)
                path = f"/features/{index}/parameters/{name}"
                if parameter is None:
                    _raise(ParametricErrorCode.UNKNOWN_REFERENCE, path)
                expected_unit = DesignUnit.DEG if name == "angle" else DesignUnit.MM
                if parameter.unit is not expected_unit:
                    _raise(ParametricErrorCode.INVALID_VALUE, path)
                if parameter.value <= 0:
                    _raise(ParametricErrorCode.INVALID_VALUE, path)
                if name == "angle" and parameter.value > 360:
                    _raise(ParametricErrorCode.INVALID_VALUE, path)
            geometry_by_id = {item.id: item for item in sketch.geometries}
            if feature.kind is FeatureKind.HOLE:
                for geometry_index, geometry_id in _source_indexed_ids(
                    feature.location_geometry_ids
                ):
                    geometry = geometry_by_id.get(geometry_id)
                    if geometry is None:
                        _raise(
                            ParametricErrorCode.UNKNOWN_REFERENCE,
                            f"/features/{index}/location_geometry_ids/{geometry_index}",
                        )
                    if geometry.kind is not GeometryKind.CIRCLE:
                        _raise(
                            ParametricErrorCode.INVALID_VALUE,
                            f"/features/{index}/location_geometry_ids/{geometry_index}",
                        )
                hole_geometry_ids = {
                    geometry.id
                    for geometry in sketch.geometries
                    if geometry.kind is GeometryKind.CIRCLE and not geometry.construction
                }
                if set(feature.location_geometry_ids) != hole_geometry_ids:
                    _raise(
                        ParametricErrorCode.INVALID_VALUE,
                        f"/features/{index}/location_geometry_ids",
                    )
            if feature.kind is FeatureKind.REVOLVE and feature.axis not in {
                "@sketch_x",
                "@sketch_y",
            }:
                axis_geometry = geometry_by_id.get(feature.axis or "")
                if (
                    axis_geometry is None
                    or axis_geometry.kind is not GeometryKind.LINE
                    or not axis_geometry.construction
                ):
                    _raise(ParametricErrorCode.INVALID_VALUE, f"/features/{index}/axis")

        mapping = self.to_mapping()
        if _json_node_count(mapping) > _MAX_PARAMETRIC_IR_NODES:
            _raise(ParametricErrorCode.BUDGET_EXCEEDED)
        if len(self.canonical_bytes) > MAX_PARAMETRIC_IR_BYTES:
            _raise(ParametricErrorCode.BUDGET_EXCEEDED)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "units": self.units.to_mapping(),
            "body": self.body.to_mapping(),
            "evidence": [item.to_mapping() for item in self.evidence],
            "parameters": [item.to_mapping() for item in self.parameters],
            "datum_planes": [item.to_mapping() for item in self.datum_planes],
            "sketches": [item.to_mapping() for item in self.sketches],
            "features": [item.to_mapping() for item in self.features],
        }

    @property
    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_mapping(),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")

    @property
    def digest(self) -> str:
        return hashlib.sha256(_DIGEST_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        keys = {
            "schema_version",
            "id",
            "name",
            "units",
            "body",
            "evidence",
            "parameters",
            "datum_planes",
            "sketches",
            "features",
        }
        data = _fields(value, allowed=keys, required=keys)
        return cls(
            schema_version=_schema(data["schema_version"]),
            id=data["id"],
            name=data["name"],
            units=_parse_nested(data["units"], UnitSystem, "/units"),
            body=_parse_nested(data["body"], BodyDefinition, "/body"),
            evidence=_parse_list(
                data["evidence"],
                DesignEvidence,
                "/evidence",
                maximum=MAX_DESIGN_EVIDENCE,
            ),
            parameters=_parse_list(
                data["parameters"],
                DesignParameter,
                "/parameters",
                maximum=MAX_DESIGN_PARAMETERS,
            ),
            datum_planes=_parse_list(
                data["datum_planes"],
                DatumPlane,
                "/datum_planes",
                maximum=MAX_DATUM_PLANES,
            ),
            sketches=_parse_list(
                data["sketches"],
                ParametricSketch,
                "/sketches",
                maximum=MAX_DESIGN_SKETCHES,
            ),
            features=_parse_list(
                data["features"],
                PartDesignFeature,
                "/features",
                maximum=MAX_DESIGN_FEATURES,
            ),
        )
