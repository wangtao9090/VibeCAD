"""Bounded, runtime-neutral contracts for the first freeform solid slice.

The contract intentionally models one result feature only.  A loft consumes
closed section curves; a sweep consumes one closed section and one open guide.
Task, revision, MCP, and provider authority are outside this package.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self

from vibecad.workflow.errors import is_canonical_json_pointer, join_json_pointer

FREEFORM_SCHEMA_VERSION = 1
MAX_FREEFORM_CURVES = 24
MAX_FREEFORM_SECTIONS = 16
MAX_FREEFORM_GUIDES = 8
MAX_CURVE_CONTROL_POINTS = 64
MAX_TOTAL_CONTROL_POINTS = 512
MAX_FREEFORM_IR_BYTES = 256 * 1024

_MAX_TEXT_BYTES = 256
_MAX_ABSOLUTE_VALUE = 1_000_000_000
_MAX_CURVE_KNOT_VALUES = MAX_CURVE_CONTROL_POINTS + 6
_MAX_JSON_DEPTH = 8
_MAX_JSON_FIELDS = 16
_MAX_JSON_NODES = 8_192
_MAX_JSON_SEQUENCE_ITEMS = MAX_TOTAL_CONTROL_POINTS
_ID = re.compile(r"^freeform_(?:design|curve|feature)_[0-9a-f]{32}$")
_DIGEST_DOMAIN = b"vibecad-freeform-design-v1\0"


class FreeformErrorCode(StrEnum):
    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    UNSUPPORTED_VERSION = "unsupported_version"
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"
    BUDGET_EXCEEDED = "budget_exceeded"
    DUPLICATE_ID = "duplicate_id"
    UNKNOWN_REFERENCE = "unknown_reference"
    INVALID_ROLE = "invalid_role"


_ERROR_MESSAGES = {
    FreeformErrorCode.MISSING_FIELD: "A required field is missing.",
    FreeformErrorCode.UNKNOWN_FIELD: "The field is not supported.",
    FreeformErrorCode.UNSUPPORTED_VERSION: "The schema version is not supported.",
    FreeformErrorCode.INVALID_TYPE: "The value has an invalid type.",
    FreeformErrorCode.INVALID_VALUE: "The value is invalid.",
    FreeformErrorCode.BUDGET_EXCEEDED: "The freeform design exceeds its resource budget.",
    FreeformErrorCode.DUPLICATE_ID: "Identifiers and names must be unique.",
    FreeformErrorCode.UNKNOWN_REFERENCE: "The referenced curve does not exist.",
    FreeformErrorCode.INVALID_ROLE: "The referenced curve has the wrong role.",
}


class FreeformContractError(ValueError):
    """Stable rejection envelope which never reflects rejected values."""

    def __init__(self, code: FreeformErrorCode, path: str = "") -> None:
        if type(code) is not FreeformErrorCode:
            raise TypeError("code must be FreeformErrorCode")
        if type(path) is not str or len(path) > 512 or not is_canonical_json_pointer(path):
            raise ValueError("path must be a bounded JSON Pointer")
        self.code = code
        self.path = path
        self.message = _ERROR_MESSAGES[code]
        super().__init__(self.message)


def _raise(code: FreeformErrorCode, path: str = "") -> None:
    raise FreeformContractError(code, path)


def _safe_path(parent: str, name: str) -> str:
    if (
        len(name) > 128
        or not name.isprintable()
        or len(name.splitlines()) != 1
        or len(parent) + len(name) + 1 > 512
    ):
        name = "__unknown__"
    return join_json_pointer(parent, name)


def _fields(value: object, *, allowed: set[str], required: set[str], path: str) -> dict[str, Any]:
    if type(value) is not dict:
        _raise(FreeformErrorCode.INVALID_TYPE, path)
    if len(value) > _MAX_JSON_FIELDS:
        _raise(FreeformErrorCode.BUDGET_EXCEEDED, path)
    if not all(type(key) is str for key in value):
        _raise(FreeformErrorCode.INVALID_TYPE, path)
    unknown = sorted(set(value) - allowed)
    if unknown:
        _raise(FreeformErrorCode.UNKNOWN_FIELD, _safe_path(path, unknown[0]))
    missing = sorted(required - set(value))
    if missing:
        _raise(FreeformErrorCode.MISSING_FIELD, join_json_pointer(path, missing[0]))
    return dict(value)


def _text(value: object, path: str) -> str:
    if type(value) is not str:
        _raise(FreeformErrorCode.INVALID_TYPE, path)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        _raise(FreeformErrorCode.INVALID_VALUE, path)
    if (
        not value
        or value != value.strip()
        or not value.isprintable()
        or len(value.splitlines()) != 1
        or len(encoded) > _MAX_TEXT_BYTES
    ):
        _raise(FreeformErrorCode.INVALID_VALUE, path)
    return value


def _id(value: object, path: str, prefix: str) -> str:
    result = _text(value, path)
    if _ID.fullmatch(result) is None or not result.startswith(f"freeform_{prefix}_"):
        _raise(FreeformErrorCode.INVALID_VALUE, path)
    return result


def _number(value: object, path: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value):
        _raise(FreeformErrorCode.INVALID_TYPE, path)
    result = float(value)
    if abs(result) > _MAX_ABSOLUTE_VALUE:
        _raise(FreeformErrorCode.INVALID_VALUE, path)
    return 0.0 if result == 0 else result


def _integer(value: object, path: str, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        _raise(FreeformErrorCode.INVALID_TYPE, path)
    if not minimum <= value <= maximum:
        _raise(FreeformErrorCode.INVALID_VALUE, path)
    return value


def _boolean(value: object, path: str) -> bool:
    if type(value) is not bool:
        _raise(FreeformErrorCode.INVALID_TYPE, path)
    return value


def _sequence(value: object, path: str, *, maximum: int) -> Sequence[Any]:
    if type(value) not in {list, tuple}:
        _raise(FreeformErrorCode.INVALID_TYPE, path)
    if len(value) > maximum:
        _raise(FreeformErrorCode.BUDGET_EXCEEDED, path)
    return value


def _validate_exact_json(
    value: object,
    *,
    path: str,
    depth: int,
    active: set[int],
    node_count: list[int],
) -> None:
    node_count[0] += 1
    if node_count[0] > _MAX_JSON_NODES:
        _raise(FreeformErrorCode.BUDGET_EXCEEDED, path)
    if depth > _MAX_JSON_DEPTH:
        _raise(FreeformErrorCode.BUDGET_EXCEEDED, path)
    value_type = type(value)
    if value_type is dict:
        if len(value) > _MAX_JSON_FIELDS:
            _raise(FreeformErrorCode.BUDGET_EXCEEDED, path)
        identity = id(value)
        if identity in active:
            _raise(FreeformErrorCode.INVALID_VALUE, path)
        active.add(identity)
        try:
            for key, item in value.items():
                if type(key) is not str:
                    _raise(FreeformErrorCode.INVALID_TYPE, path)
                child_path = _safe_path(path, key)
                _validate_exact_json(
                    item,
                    path=child_path,
                    depth=depth + 1,
                    active=active,
                    node_count=node_count,
                )
        finally:
            active.remove(identity)
        return
    if value_type in {list, tuple}:
        if len(value) > _MAX_JSON_SEQUENCE_ITEMS:
            _raise(FreeformErrorCode.BUDGET_EXCEEDED, path)
        identity = id(value)
        if identity in active:
            _raise(FreeformErrorCode.INVALID_VALUE, path)
        active.add(identity)
        try:
            for index, item in enumerate(value):
                _validate_exact_json(
                    item,
                    path=f"{path}/{index}",
                    depth=depth + 1,
                    active=active,
                    node_count=node_count,
                )
        finally:
            active.remove(identity)
        return
    if value_type is str:
        if len(value) > MAX_FREEFORM_IR_BYTES:
            _raise(FreeformErrorCode.BUDGET_EXCEEDED, path)
        return
    if value_type in {bool, float, int, type(None)}:
        return
    _raise(FreeformErrorCode.INVALID_TYPE, path)


def _ensure_ir_size_budget(value: object, path: str = "") -> None:
    """Validate exact JSON shape and bound bytes before contract materialization."""

    _validate_exact_json(value, path=path, depth=0, active=set(), node_count=[0])

    encoder = json.JSONEncoder(allow_nan=True, ensure_ascii=False, separators=(",", ":"))
    total = 0
    try:
        for chunk in encoder.iterencode(value):
            total += len(chunk.encode("utf-8"))
            if total > MAX_FREEFORM_IR_BYTES:
                _raise(FreeformErrorCode.BUDGET_EXCEEDED)
    except FreeformContractError:
        raise
    except (RecursionError, TypeError, UnicodeError, ValueError):
        _raise(FreeformErrorCode.INVALID_VALUE, path)


def _preflight_curve_parse_budget(raw_curves: Sequence[Any]) -> None:
    """Reject per-curve and aggregate point budgets before Point3D parsing."""

    total = 0
    for index, raw_curve in enumerate(raw_curves):
        if type(raw_curve) is not dict:
            continue
        raw_points = raw_curve.get("control_points")
        if type(raw_points) not in {list, tuple}:
            continue
        if len(raw_points) > MAX_CURVE_CONTROL_POINTS:
            _raise(FreeformErrorCode.BUDGET_EXCEEDED, f"/curves/{index}/control_points")
        total += len(raw_points)
        if total > MAX_TOTAL_CONTROL_POINTS:
            _raise(FreeformErrorCode.BUDGET_EXCEEDED, "/curves")


class SplineKind(StrEnum):
    BSPLINE = "bspline"
    NURBS = "nurbs"


class CurveRole(StrEnum):
    SECTION = "section"
    GUIDE = "guide"


class FreeformFeatureKind(StrEnum):
    LOFT = "loft"
    SWEEP = "sweep"


def _enum[EnumT: StrEnum](value: object, enum_type: type[EnumT], path: str) -> EnumT:
    if isinstance(value, enum_type):
        return value
    if type(value) is not str:
        _raise(FreeformErrorCode.INVALID_TYPE, path)
    try:
        return enum_type(value)
    except ValueError:
        _raise(FreeformErrorCode.INVALID_VALUE, path)


@dataclass(frozen=True, slots=True)
class Point3D:
    x_mm: float
    y_mm: float
    z_mm: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x_mm", _number(self.x_mm, "/x_mm"))
        object.__setattr__(self, "y_mm", _number(self.y_mm, "/y_mm"))
        object.__setattr__(self, "z_mm", _number(self.z_mm, "/z_mm"))

    def to_mapping(self) -> dict[str, float]:
        return {"x_mm": self.x_mm, "y_mm": self.y_mm, "z_mm": self.z_mm}

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        _ensure_ir_size_budget(value, path)
        fields = _fields(
            value,
            allowed={"x_mm", "y_mm", "z_mm"},
            required={"x_mm", "y_mm", "z_mm"},
            path=path,
        )
        return cls(fields["x_mm"], fields["y_mm"], fields["z_mm"])


def _same_point(left: Point3D, right: Point3D) -> bool:
    return left == right


@dataclass(frozen=True, slots=True)
class SplineCurve:
    id: str
    name: str
    role: CurveRole
    kind: SplineKind
    degree: int
    control_points: tuple[Point3D, ...]
    knots: tuple[float, ...]
    multiplicities: tuple[int, ...]
    weights: tuple[float, ...] = ()
    closed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _id(self.id, "/id", "curve"))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        object.__setattr__(self, "role", _enum(self.role, CurveRole, "/role"))
        object.__setattr__(self, "kind", _enum(self.kind, SplineKind, "/kind"))
        degree = _integer(self.degree, "/degree", 2, 5)
        object.__setattr__(self, "degree", degree)
        points = tuple(self.control_points)
        if not all(type(point) is Point3D for point in points):
            _raise(FreeformErrorCode.INVALID_TYPE, "/control_points")
        if len(points) < degree + 1:
            _raise(FreeformErrorCode.INVALID_VALUE, "/control_points")
        if len(points) > MAX_CURVE_CONTROL_POINTS:
            _raise(FreeformErrorCode.BUDGET_EXCEEDED, "/control_points")
        object.__setattr__(self, "control_points", points)
        knots = tuple(_number(item, f"/knots/{index}") for index, item in enumerate(self.knots))
        if len(knots) < 2 or any(
            right <= left for left, right in zip(knots, knots[1:], strict=False)
        ):
            _raise(FreeformErrorCode.INVALID_VALUE, "/knots")
        object.__setattr__(self, "knots", knots)
        multiplicities = tuple(
            _integer(item, f"/multiplicities/{index}", 1, degree + 1)
            for index, item in enumerate(self.multiplicities)
        )
        if len(multiplicities) != len(knots):
            _raise(FreeformErrorCode.INVALID_VALUE, "/multiplicities")
        if sum(multiplicities) != len(points) + degree + 1:
            _raise(FreeformErrorCode.INVALID_VALUE, "/multiplicities")
        if multiplicities[0] != degree + 1 or multiplicities[-1] != degree + 1:
            _raise(FreeformErrorCode.INVALID_VALUE, "/multiplicities")
        object.__setattr__(self, "multiplicities", multiplicities)
        weights = tuple(
            _number(item, f"/weights/{index}") for index, item in enumerate(self.weights)
        )
        if self.kind is SplineKind.NURBS:
            if len(weights) != len(points) or any(weight <= 0 for weight in weights):
                _raise(FreeformErrorCode.INVALID_VALUE, "/weights")
        elif weights:
            _raise(FreeformErrorCode.INVALID_VALUE, "/weights")
        object.__setattr__(self, "weights", weights)
        object.__setattr__(self, "closed", _boolean(self.closed, "/closed"))
        if self.role is CurveRole.SECTION and not self.closed:
            _raise(FreeformErrorCode.INVALID_ROLE, "/closed")
        if self.role is CurveRole.GUIDE and self.closed:
            _raise(FreeformErrorCode.INVALID_ROLE, "/closed")
        if self.closed and not _same_point(points[0], points[-1]):
            _raise(FreeformErrorCode.INVALID_VALUE, "/control_points")

    @property
    def parameter_range(self) -> tuple[float, float]:
        return self.knots[0], self.knots[-1]

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role.value,
            "kind": self.kind.value,
            "degree": self.degree,
            "control_points": [point.to_mapping() for point in self.control_points],
            "knots": list(self.knots),
            "multiplicities": list(self.multiplicities),
            "weights": list(self.weights),
            "closed": self.closed,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        _ensure_ir_size_budget(value, path)
        fields = _fields(
            value,
            allowed={
                "id",
                "name",
                "role",
                "kind",
                "degree",
                "control_points",
                "knots",
                "multiplicities",
                "weights",
                "closed",
            },
            required={
                "id",
                "name",
                "role",
                "kind",
                "degree",
                "control_points",
                "knots",
                "multiplicities",
                "closed",
            },
            path=path,
        )
        raw_points = _sequence(
            fields["control_points"],
            f"{path}/control_points",
            maximum=MAX_CURVE_CONTROL_POINTS,
        )
        raw_knots = _sequence(fields["knots"], f"{path}/knots", maximum=_MAX_CURVE_KNOT_VALUES)
        raw_mults = _sequence(
            fields["multiplicities"],
            f"{path}/multiplicities",
            maximum=_MAX_CURVE_KNOT_VALUES,
        )
        raw_weights = _sequence(
            fields.get("weights", ()), f"{path}/weights", maximum=MAX_CURVE_CONTROL_POINTS
        )
        return cls(
            fields["id"],
            fields["name"],
            fields["role"],
            fields["kind"],
            fields["degree"],
            tuple(
                Point3D.from_mapping(point, f"{path}/control_points/{index}")
                for index, point in enumerate(raw_points)
            ),
            tuple(raw_knots),
            tuple(raw_mults),
            tuple(raw_weights),
            fields["closed"],
        )


@dataclass(frozen=True, slots=True)
class FreeformFeature:
    id: str
    name: str
    kind: FreeformFeatureKind
    section_ids: tuple[str, ...]
    guide_ids: tuple[str, ...] = ()
    solid: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _id(self.id, "/id", "feature"))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        object.__setattr__(self, "kind", _enum(self.kind, FreeformFeatureKind, "/kind"))
        sections = tuple(
            _id(item, f"/section_ids/{index}", "curve")
            for index, item in enumerate(self.section_ids)
        )
        guides = tuple(
            _id(item, f"/guide_ids/{index}", "curve") for index, item in enumerate(self.guide_ids)
        )
        if len(set(sections)) != len(sections) or len(set(guides)) != len(guides):
            _raise(FreeformErrorCode.DUPLICATE_ID)
        if self.kind is FreeformFeatureKind.LOFT:
            if not 2 <= len(sections) <= MAX_FREEFORM_SECTIONS or guides:
                _raise(FreeformErrorCode.INVALID_VALUE, "/section_ids")
        elif len(sections) != 1 or len(guides) != 1:
            _raise(FreeformErrorCode.INVALID_VALUE, "/guide_ids")
        object.__setattr__(self, "section_ids", sections)
        object.__setattr__(self, "guide_ids", guides)
        object.__setattr__(self, "solid", _boolean(self.solid, "/solid"))
        if not self.solid:
            _raise(FreeformErrorCode.INVALID_VALUE, "/solid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind.value,
            "section_ids": list(self.section_ids),
            "guide_ids": list(self.guide_ids),
            "solid": self.solid,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        _ensure_ir_size_budget(value, path)
        fields = _fields(
            value,
            allowed={"id", "name", "kind", "section_ids", "guide_ids", "solid"},
            required={"id", "name", "kind", "section_ids", "solid"},
            path=path,
        )
        return cls(
            fields["id"],
            fields["name"],
            fields["kind"],
            tuple(
                _sequence(
                    fields["section_ids"],
                    f"{path}/section_ids",
                    maximum=MAX_FREEFORM_SECTIONS,
                )
            ),
            tuple(
                _sequence(
                    fields.get("guide_ids", ()),
                    f"{path}/guide_ids",
                    maximum=MAX_FREEFORM_GUIDES,
                )
            ),
            fields["solid"],
        )


@dataclass(frozen=True, slots=True)
class FreeformDesign:
    id: str
    name: str
    curves: tuple[SplineCurve, ...]
    feature: FreeformFeature
    schema_version: int = field(default=FREEFORM_SCHEMA_VERSION)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int:
            _raise(FreeformErrorCode.INVALID_TYPE, "/schema_version")
        if self.schema_version != FREEFORM_SCHEMA_VERSION:
            _raise(FreeformErrorCode.UNSUPPORTED_VERSION, "/schema_version")
        object.__setattr__(self, "id", _id(self.id, "/id", "design"))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        curves = tuple(self.curves)
        if not 2 <= len(curves) <= MAX_FREEFORM_CURVES:
            _raise(FreeformErrorCode.BUDGET_EXCEEDED, "/curves")
        if not all(type(curve) is SplineCurve for curve in curves):
            _raise(FreeformErrorCode.INVALID_TYPE, "/curves")
        if len({curve.id for curve in curves}) != len(curves):
            _raise(FreeformErrorCode.DUPLICATE_ID, "/curves")
        if len({curve.name for curve in curves}) != len(curves):
            _raise(FreeformErrorCode.DUPLICATE_ID, "/curves")
        if sum(len(curve.control_points) for curve in curves) > MAX_TOTAL_CONTROL_POINTS:
            _raise(FreeformErrorCode.BUDGET_EXCEEDED, "/curves")
        sections = sum(curve.role is CurveRole.SECTION for curve in curves)
        guides = sum(curve.role is CurveRole.GUIDE for curve in curves)
        if sections > MAX_FREEFORM_SECTIONS or guides > MAX_FREEFORM_GUIDES:
            _raise(FreeformErrorCode.BUDGET_EXCEEDED, "/curves")
        if type(self.feature) is not FreeformFeature:
            _raise(FreeformErrorCode.INVALID_TYPE, "/feature")
        by_id = {curve.id: curve for curve in curves}
        for index, curve_id in enumerate(self.feature.section_ids):
            curve = by_id.get(curve_id)
            if curve is None:
                _raise(FreeformErrorCode.UNKNOWN_REFERENCE, f"/feature/section_ids/{index}")
            if curve.role is not CurveRole.SECTION:
                _raise(FreeformErrorCode.INVALID_ROLE, f"/feature/section_ids/{index}")
        for index, curve_id in enumerate(self.feature.guide_ids):
            curve = by_id.get(curve_id)
            if curve is None:
                _raise(FreeformErrorCode.UNKNOWN_REFERENCE, f"/feature/guide_ids/{index}")
            if curve.role is not CurveRole.GUIDE:
                _raise(FreeformErrorCode.INVALID_ROLE, f"/feature/guide_ids/{index}")
        object.__setattr__(self, "curves", curves)
        if len(self.to_canonical_json().encode("utf-8")) > MAX_FREEFORM_IR_BYTES:
            _raise(FreeformErrorCode.BUDGET_EXCEEDED)

    @property
    def curve_by_id(self) -> dict[str, SplineCurve]:
        return {curve.id: curve for curve in self.curves}

    @property
    def digest(self) -> str:
        return hashlib.sha256(_DIGEST_DOMAIN + self.to_canonical_json().encode()).hexdigest()

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "curves": [curve.to_mapping() for curve in self.curves],
            "feature": self.feature.to_mapping(),
        }

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.to_mapping(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        _ensure_ir_size_budget(value)
        fields = _fields(
            value,
            allowed={"schema_version", "id", "name", "curves", "feature"},
            required={"schema_version", "id", "name", "curves", "feature"},
            path="",
        )
        raw_curves = _sequence(fields["curves"], "/curves", maximum=MAX_FREEFORM_CURVES)
        _preflight_curve_parse_budget(raw_curves)
        return cls(
            fields["id"],
            fields["name"],
            tuple(
                SplineCurve.from_mapping(curve, f"/curves/{index}")
                for index, curve in enumerate(raw_curves)
            ),
            FreeformFeature.from_mapping(fields["feature"], "/feature"),
            fields["schema_version"],
        )


__all__ = [
    "FREEFORM_SCHEMA_VERSION",
    "MAX_CURVE_CONTROL_POINTS",
    "MAX_FREEFORM_CURVES",
    "MAX_FREEFORM_GUIDES",
    "MAX_FREEFORM_IR_BYTES",
    "MAX_FREEFORM_SECTIONS",
    "MAX_TOTAL_CONTROL_POINTS",
    "CurveRole",
    "FreeformContractError",
    "FreeformDesign",
    "FreeformErrorCode",
    "FreeformFeature",
    "FreeformFeatureKind",
    "Point3D",
    "SplineCurve",
    "SplineKind",
]
