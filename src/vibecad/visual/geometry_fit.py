"""Bounded fitting for caller-declared planar primitive families.

This module consumes points that have already been mapped into one plane
frame.  It never segments pixels or guesses a primitive family.  A successful
fit is an authority-free candidate with explicit residual evidence; callers
still own reconstruction review and adoption.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray

from vibecad.visual.metrology import MAX_ABS_PLANE_COORDINATE_MM, PlanePoint

GEOMETRY_FIT_SCHEMA_VERSION = 1
MAX_FIT_POINTS = 256
MAX_FIT_TOLERANCE_MM = 100_000_000.0
MAX_FIT_CONDITION = 1_000_000_000_000.0

_GEOMETRY_EPSILON = 1e-12
_ANGLE_EPSILON = 1e-8
_FULL_CIRCLE_GAP = 1e-4


class GeometryFitErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    NUMERICAL_FAILURE = "numerical_failure"


class GeometryFitError(ValueError):
    """Bounded failure that never reflects rejected point values."""

    def __init__(self, code: GeometryFitErrorCode, path: str = "") -> None:
        if type(code) is not GeometryFitErrorCode:
            raise TypeError("code must be an exact GeometryFitErrorCode")
        if type(path) is not str:
            raise TypeError("path must be a string")
        try:
            encoded = path.encode("utf-8")
        except UnicodeError:
            raise ValueError("path must be a bounded string") from None
        if len(encoded) > 256:
            raise ValueError("path must be a bounded string")
        self.code = code
        self.path = path
        super().__init__(code.value)


def _fail(code: GeometryFitErrorCode, path: str = "") -> None:
    raise GeometryFitError(code, path)


class PrimitiveFamily(StrEnum):
    LINE = "line"
    CIRCLE = "circle"
    ARC = "arc"
    ROTATED_RECTANGLE = "rotated_rectangle"


class GeometryFitStatus(StrEnum):
    FITTED = "fitted"
    UNKNOWN = "unknown"


class GeometryFitUnknownReason(StrEnum):
    INSUFFICIENT_POINTS = "insufficient_points"
    POINT_COUNT_MISMATCH = "point_count_mismatch"
    DEGENERATE_GEOMETRY = "degenerate_geometry"
    RESIDUAL_EXCEEDED = "residual_exceeded"
    AMBIGUOUS_ARC_ORDER = "ambiguous_arc_order"
    INVALID_RECTANGLE_ORDER = "invalid_rectangle_order"


@dataclass(frozen=True, slots=True, kw_only=True)
class GeometryFitRequest:
    family: PrimitiveFamily
    points: tuple[PlanePoint, ...]
    residual_tolerance_mm: int | float
    schema_version: int = GEOMETRY_FIT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != GEOMETRY_FIT_SCHEMA_VERSION
        ):
            _fail(GeometryFitErrorCode.INVALID_INPUT, "/schema_version")
        if type(self.family) is not PrimitiveFamily:
            _fail(GeometryFitErrorCode.INVALID_INPUT, "/family")
        if type(self.points) is not tuple:
            _fail(GeometryFitErrorCode.INVALID_INPUT, "/points")
        if len(self.points) > MAX_FIT_POINTS:
            _fail(GeometryFitErrorCode.BUDGET_EXCEEDED, "/points")
        if any(type(point) is not PlanePoint for point in self.points):
            _fail(GeometryFitErrorCode.INVALID_INPUT, "/points")
        if type(self.residual_tolerance_mm) not in {int, float}:
            _fail(GeometryFitErrorCode.INVALID_INPUT, "/residual_tolerance_mm")
        tolerance = float(self.residual_tolerance_mm)
        if not math.isfinite(tolerance) or not 0.0 <= tolerance <= MAX_FIT_TOLERANCE_MM:
            _fail(GeometryFitErrorCode.INVALID_INPUT, "/residual_tolerance_mm")
        object.__setattr__(self, "residual_tolerance_mm", tolerance)


@dataclass(frozen=True, slots=True, kw_only=True)
class LinePrimitive:
    anchor_x_mm: float
    anchor_y_mm: float
    direction_x: float
    direction_y: float


@dataclass(frozen=True, slots=True, kw_only=True)
class CirclePrimitive:
    center_x_mm: float
    center_y_mm: float
    radius_mm: float


@dataclass(frozen=True, slots=True, kw_only=True)
class ArcPrimitive:
    center_x_mm: float
    center_y_mm: float
    radius_mm: float
    start_angle_rad: float
    sweep_angle_rad: float


@dataclass(frozen=True, slots=True, kw_only=True)
class RotatedRectanglePrimitive:
    center_x_mm: float
    center_y_mm: float
    width_mm: float
    height_mm: float
    angle_rad: float


type FittedPrimitive = LinePrimitive | CirclePrimitive | ArcPrimitive | RotatedRectanglePrimitive


@dataclass(frozen=True, slots=True, kw_only=True)
class GeometryFitResult:
    family: PrimitiveFamily
    status: GeometryFitStatus
    primitive: FittedPrimitive | None
    rms_residual_mm: float | None
    max_residual_mm: float | None
    max_excess_residual_mm: float | None
    unknown_reason: GeometryFitUnknownReason | None
    point_count: int
    schema_version: int = GEOMETRY_FIT_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class _Candidate:
    primitive: FittedPrimitive
    residuals: NDArray[np.float64]


def _unknown(
    request: GeometryFitRequest,
    reason: GeometryFitUnknownReason,
    *,
    residuals: NDArray[np.float64] | None = None,
) -> GeometryFitResult:
    rms = maximum = excess = None
    if residuals is not None:
        rms, maximum, excess = _residual_metrics(request, residuals)
    return GeometryFitResult(
        family=request.family,
        status=GeometryFitStatus.UNKNOWN,
        primitive=None,
        rms_residual_mm=rms,
        max_residual_mm=maximum,
        max_excess_residual_mm=excess,
        unknown_reason=reason,
        point_count=len(request.points),
    )


def _point_array(points: tuple[PlanePoint, ...]) -> NDArray[np.float64]:
    result = np.asarray(tuple((point.x_mm, point.y_mm) for point in points), dtype=np.float64)
    if result.ndim != 2 or result.shape[1:] != (2,) or not np.isfinite(result).all():
        _fail(GeometryFitErrorCode.NUMERICAL_FAILURE)
    return result


def _bounded_output(value: float) -> float:
    result = float(value)
    if not math.isfinite(result) or abs(result) > MAX_ABS_PLANE_COORDINATE_MM:
        _fail(GeometryFitErrorCode.NUMERICAL_FAILURE)
    return result


def _residual_metrics(
    request: GeometryFitRequest,
    residuals: NDArray[np.float64],
) -> tuple[float, float, float]:
    if residuals.shape != (len(request.points),) or not np.isfinite(residuals).all():
        _fail(GeometryFitErrorCode.NUMERICAL_FAILURE)
    uncertainty = np.asarray(
        tuple(point.uncertainty_mm for point in request.points),
        dtype=np.float64,
    )
    rms = float(np.sqrt(np.mean(np.square(residuals))))
    maximum = float(np.max(residuals))
    excess = float(np.max(np.maximum(0.0, residuals - uncertainty)))
    if any(not math.isfinite(value) for value in (rms, maximum, excess)):
        _fail(GeometryFitErrorCode.NUMERICAL_FAILURE)
    return rms, maximum, excess


def _finalize(request: GeometryFitRequest, candidate: _Candidate) -> GeometryFitResult:
    rms, maximum, excess = _residual_metrics(request, candidate.residuals)
    if excess > request.residual_tolerance_mm:
        return _unknown(
            request,
            GeometryFitUnknownReason.RESIDUAL_EXCEEDED,
            residuals=candidate.residuals,
        )
    return GeometryFitResult(
        family=request.family,
        status=GeometryFitStatus.FITTED,
        primitive=candidate.primitive,
        rms_residual_mm=rms,
        max_residual_mm=maximum,
        max_excess_residual_mm=excess,
        unknown_reason=None,
        point_count=len(request.points),
    )


def _canonical_direction(direction: NDArray[np.float64]) -> NDArray[np.float64]:
    if abs(float(direction[0])) >= abs(float(direction[1])):
        sign = 1.0 if direction[0] >= 0.0 else -1.0
    else:
        sign = 1.0 if direction[1] >= 0.0 else -1.0
    return direction * sign


def _fit_line(request: GeometryFitRequest) -> _Candidate | GeometryFitUnknownReason:
    if len(request.points) < 2:
        return GeometryFitUnknownReason.INSUFFICIENT_POINTS
    points = _point_array(request.points)
    anchor = np.mean(points, axis=0)
    centered = points - anchor
    try:
        _left, singular, right = np.linalg.svd(centered, full_matrices=False)
    except np.linalg.LinAlgError:
        _fail(GeometryFitErrorCode.NUMERICAL_FAILURE)
    if not singular.size or singular[0] <= _GEOMETRY_EPSILON:
        return GeometryFitUnknownReason.DEGENERATE_GEOMETRY
    direction = _canonical_direction(right[0])
    normal = np.asarray((-direction[1], direction[0]), dtype=np.float64)
    residuals = np.abs(centered @ normal)
    return _Candidate(
        primitive=LinePrimitive(
            anchor_x_mm=_bounded_output(anchor[0]),
            anchor_y_mm=_bounded_output(anchor[1]),
            direction_x=_bounded_output(direction[0]),
            direction_y=_bounded_output(direction[1]),
        ),
        residuals=residuals,
    )


def _circle_candidate(
    request: GeometryFitRequest,
) -> _Candidate | GeometryFitUnknownReason:
    if len(request.points) < 3:
        return GeometryFitUnknownReason.INSUFFICIENT_POINTS
    points = _point_array(request.points)
    origin = np.mean(points, axis=0)
    centered = points - origin
    coefficients = np.float64(2.0) * centered
    target = np.sum(np.square(centered), axis=1)
    try:
        delta, _residuals, rank, singular = np.linalg.lstsq(
            coefficients,
            target,
            rcond=None,
        )
    except np.linalg.LinAlgError:
        _fail(GeometryFitErrorCode.NUMERICAL_FAILURE)
    if rank != 2 or len(singular) != 2 or singular[-1] <= _GEOMETRY_EPSILON:
        return GeometryFitUnknownReason.DEGENERATE_GEOMETRY
    if singular[0] / singular[-1] > MAX_FIT_CONDITION:
        return GeometryFitUnknownReason.DEGENERATE_GEOMETRY
    center = origin + delta
    distances = np.linalg.norm(points - center, axis=1)
    radius = float(np.mean(distances))
    if not math.isfinite(radius) or radius <= _GEOMETRY_EPSILON:
        return GeometryFitUnknownReason.DEGENERATE_GEOMETRY
    residuals = np.abs(distances - radius)
    return _Candidate(
        primitive=CirclePrimitive(
            center_x_mm=_bounded_output(center[0]),
            center_y_mm=_bounded_output(center[1]),
            radius_mm=_bounded_output(radius),
        ),
        residuals=residuals,
    )


def _fit_arc(request: GeometryFitRequest) -> _Candidate | GeometryFitUnknownReason:
    circle = _circle_candidate(request)
    if type(circle) is GeometryFitUnknownReason:
        return circle
    if type(circle.primitive) is not CirclePrimitive:
        _fail(GeometryFitErrorCode.NUMERICAL_FAILURE)
    points = _point_array(request.points)
    center = np.asarray(
        (circle.primitive.center_x_mm, circle.primitive.center_y_mm),
        dtype=np.float64,
    )
    angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
    deltas = (np.diff(angles) + math.pi) % (2.0 * math.pi) - math.pi
    significant = deltas[np.abs(deltas) > _ANGLE_EPSILON]
    if not significant.size:
        return GeometryFitUnknownReason.AMBIGUOUS_ARC_ORDER
    if np.any(significant > 0.0) and np.any(significant < 0.0):
        return GeometryFitUnknownReason.AMBIGUOUS_ARC_ORDER
    sweep = float(np.sum(deltas))
    if abs(sweep) <= _ANGLE_EPSILON or abs(sweep) >= 2.0 * math.pi - _FULL_CIRCLE_GAP:
        return GeometryFitUnknownReason.AMBIGUOUS_ARC_ORDER
    start = float((angles[0] + math.pi) % (2.0 * math.pi) - math.pi)
    return _Candidate(
        primitive=ArcPrimitive(
            center_x_mm=circle.primitive.center_x_mm,
            center_y_mm=circle.primitive.center_y_mm,
            radius_mm=circle.primitive.radius_mm,
            start_angle_rad=start,
            sweep_angle_rad=sweep,
        ),
        residuals=circle.residuals,
    )


def _fit_rectangle(request: GeometryFitRequest) -> _Candidate | GeometryFitUnknownReason:
    if len(request.points) != 4:
        return GeometryFitUnknownReason.POINT_COUNT_MISMATCH
    points = _point_array(request.points)
    edges = np.roll(points, -1, axis=0) - points
    cross = edges[:, 0] * np.roll(edges[:, 1], -1) - edges[:, 1] * np.roll(
        edges[:, 0],
        -1,
    )
    significant = cross[np.abs(cross) > _GEOMETRY_EPSILON]
    if len(significant) != 4 or (np.any(significant > 0.0) and np.any(significant < 0.0)):
        return GeometryFitUnknownReason.INVALID_RECTANGLE_ORDER
    signed_area = 0.5 * float(
        np.sum(points[:, 0] * np.roll(points[:, 1], -1))
        - np.sum(points[:, 1] * np.roll(points[:, 0], -1))
    )
    if abs(signed_area) <= _GEOMETRY_EPSILON:
        return GeometryFitUnknownReason.DEGENERATE_GEOMETRY

    u_seed = edges[0] - edges[2]
    u_length = float(np.linalg.norm(u_seed))
    if u_length <= _GEOMETRY_EPSILON:
        return GeometryFitUnknownReason.DEGENERATE_GEOMETRY
    direction_u = u_seed / u_length
    if float(np.dot(direction_u, edges[0])) < 0.0:
        direction_u = -direction_u
    orientation = 1.0 if signed_area > 0.0 else -1.0
    direction_v = orientation * np.asarray((-direction_u[1], direction_u[0]))
    width = 0.5 * float(np.dot(edges[0], direction_u) - np.dot(edges[2], direction_u))
    height = 0.5 * float(np.dot(edges[1], direction_v) - np.dot(edges[3], direction_v))
    if width <= _GEOMETRY_EPSILON or height <= _GEOMETRY_EPSILON:
        return GeometryFitUnknownReason.DEGENERATE_GEOMETRY

    center = np.mean(points, axis=0)
    expected = np.asarray(
        (
            center - 0.5 * width * direction_u - 0.5 * height * direction_v,
            center + 0.5 * width * direction_u - 0.5 * height * direction_v,
            center + 0.5 * width * direction_u + 0.5 * height * direction_v,
            center - 0.5 * width * direction_u + 0.5 * height * direction_v,
        ),
        dtype=np.float64,
    )
    residuals = np.linalg.norm(points - expected, axis=1)
    angle = float(math.atan2(direction_u[1], direction_u[0]))
    return _Candidate(
        primitive=RotatedRectanglePrimitive(
            center_x_mm=_bounded_output(center[0]),
            center_y_mm=_bounded_output(center[1]),
            width_mm=_bounded_output(width),
            height_mm=_bounded_output(height),
            angle_rad=angle,
        ),
        residuals=residuals,
    )


def fit_declared_geometry(request: GeometryFitRequest) -> GeometryFitResult:
    """Fit exactly the declared primitive family or return bounded UNKNOWN."""

    if type(request) is not GeometryFitRequest:
        _fail(GeometryFitErrorCode.INVALID_INPUT, "/request")
    if request.family in {PrimitiveFamily.LINE, PrimitiveFamily.CIRCLE}:
        ordered_points = tuple(
            sorted(
                request.points,
                key=lambda point: (point.x_mm, point.y_mm, point.uncertainty_mm),
            )
        )
        request = GeometryFitRequest(
            family=request.family,
            points=ordered_points,
            residual_tolerance_mm=request.residual_tolerance_mm,
        )

    if request.family is PrimitiveFamily.LINE:
        candidate = _fit_line(request)
    elif request.family is PrimitiveFamily.CIRCLE:
        candidate = _circle_candidate(request)
    elif request.family is PrimitiveFamily.ARC:
        candidate = _fit_arc(request)
    else:
        candidate = _fit_rectangle(request)
    if type(candidate) is GeometryFitUnknownReason:
        return _unknown(request, candidate)
    return _finalize(request, candidate)


__all__ = [
    "GEOMETRY_FIT_SCHEMA_VERSION",
    "MAX_FIT_CONDITION",
    "MAX_FIT_POINTS",
    "MAX_FIT_TOLERANCE_MM",
    "ArcPrimitive",
    "CirclePrimitive",
    "FittedPrimitive",
    "GeometryFitError",
    "GeometryFitErrorCode",
    "GeometryFitRequest",
    "GeometryFitResult",
    "GeometryFitStatus",
    "GeometryFitUnknownReason",
    "LinePrimitive",
    "PrimitiveFamily",
    "RotatedRectanglePrimitive",
    "fit_declared_geometry",
]
