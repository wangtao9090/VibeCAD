"""Curve-level continuity evidence for bounded freeform inputs.

``check_join`` evaluates the endpoint jets of two curves and provides a
sufficient curve-join test for G0/G1/G2. ``check_paired_boundaries`` samples
two representations of the same intended boundary.  These checks are useful
compiler evidence; they do not claim to prove continuity of arbitrary surface
patches without the corresponding surface cross-derivative data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from vibecad.freeform.contracts import Point3D, SplineCurve, SplineKind

MIN_CONTINUITY_SAMPLES = 3
MAX_CONTINUITY_SAMPLES = 65


class ContinuityOrder(StrEnum):
    G0 = "G0"
    G1 = "G1"
    G2 = "G2"


@dataclass(frozen=True, slots=True)
class ContinuityTolerance:
    position_mm: float = 1e-5
    tangent_angle_deg: float = 0.1
    curvature_per_mm: float = 1e-4

    def __post_init__(self) -> None:
        for value in (self.position_mm, self.tangent_angle_deg, self.curvature_per_mm):
            if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                raise ValueError("continuity tolerances must be finite and non-negative")
        if (
            self.position_mm > 1_000
            or self.tangent_angle_deg > 180
            or self.curvature_per_mm > 1_000
        ):
            raise ValueError("continuity tolerance exceeds the bounded range")


@dataclass(frozen=True, slots=True)
class CurveJet:
    position: Point3D
    tangent: tuple[float, float, float]
    curvature: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class CurveJoinCheck:
    requested_order: ContinuityOrder
    achieved_order: ContinuityOrder | None
    passed: bool
    position_gap_mm: float
    tangent_angle_deg: float | None
    curvature_gap_per_mm: float | None
    left_jet: CurveJet
    right_jet: CurveJet


@dataclass(frozen=True, slots=True)
class BoundarySample:
    parameter_fraction: float
    position_gap_mm: float
    tangent_angle_deg: float | None
    curvature_gap_per_mm: float | None


@dataclass(frozen=True, slots=True)
class SampledBoundaryCheck:
    requested_order: ContinuityOrder
    passed: bool
    reversed: bool
    samples: tuple[BoundarySample, ...]
    max_position_gap_mm: float
    max_tangent_angle_deg: float | None
    max_curvature_gap_per_mm: float | None


Vector = tuple[float, float, float]


def _vector(point: Point3D) -> Vector:
    return point.x_mm, point.y_mm, point.z_mm


def _point(value: Vector) -> Point3D:
    return Point3D(*value)


def _add(left: Vector, right: Vector) -> Vector:
    return tuple(a + b for a, b in zip(left, right, strict=True))  # type: ignore[return-value]


def _sub(left: Vector, right: Vector) -> Vector:
    return tuple(a - b for a, b in zip(left, right, strict=True))  # type: ignore[return-value]


def _scale(value: Vector, factor: float) -> Vector:
    return tuple(item * factor for item in value)  # type: ignore[return-value]


def _dot(left: Vector, right: Vector) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _norm(value: Vector) -> float:
    return math.sqrt(_dot(value, value))


def _unit(value: Vector) -> Vector | None:
    length = _norm(value)
    if length <= 1e-14:
        return None
    return _scale(value, 1.0 / length)


def _distance(left: Vector, right: Vector) -> float:
    return _norm(_sub(left, right))


def _angle(left: Vector, right: Vector) -> float | None:
    left_unit = _unit(left)
    right_unit = _unit(right)
    if left_unit is None or right_unit is None:
        return None
    cosine = max(-1.0, min(1.0, _dot(left_unit, right_unit)))
    return math.degrees(math.acos(cosine))


def _expanded_knots(curve: SplineCurve) -> tuple[float, ...]:
    return tuple(
        knot
        for knot, multiplicity in zip(curve.knots, curve.multiplicities, strict=True)
        for _ in range(multiplicity)
    )


def _basis_functions(
    span: int, parameter: float, degree: int, knots: tuple[float, ...]
) -> list[float]:
    values = [0.0] * (degree + 1)
    left = [0.0] * (degree + 1)
    right = [0.0] * (degree + 1)
    values[0] = 1.0
    for column in range(1, degree + 1):
        left[column] = parameter - knots[span + 1 - column]
        right[column] = knots[span + column] - parameter
        saved = 0.0
        for row in range(column):
            denominator = right[row + 1] + left[column - row]
            term = 0.0 if denominator == 0 else values[row] / denominator
            values[row] = saved + right[row + 1] * term
            saved = left[column - row] * term
        values[column] = saved
    return values


def evaluate_curve(curve: SplineCurve, parameter_fraction: float) -> Point3D:
    """Evaluate a validated non-periodic B-spline/NURBS at a [0, 1] fraction."""

    if type(curve) is not SplineCurve:
        raise TypeError("curve must be SplineCurve")
    if type(parameter_fraction) not in {int, float} or not math.isfinite(parameter_fraction):
        raise TypeError("parameter_fraction must be finite")
    if not 0 <= parameter_fraction <= 1:
        raise ValueError("parameter_fraction must be in [0, 1]")
    knots = _expanded_knots(curve)
    degree = curve.degree
    last_pole = len(curve.control_points) - 1
    start, end = curve.parameter_range
    parameter = start + float(parameter_fraction) * (end - start)
    if parameter_fraction == 1:
        span = last_pole
    else:
        low = degree
        high = last_pole + 1
        span = (low + high) // 2
        while parameter < knots[span] or parameter >= knots[span + 1]:
            if parameter < knots[span]:
                high = span
            else:
                low = span
            span = (low + high) // 2
    basis = _basis_functions(span, parameter, degree, knots)
    numerator = (0.0, 0.0, 0.0)
    denominator = 0.0
    for local_index, basis_value in enumerate(basis):
        pole_index = span - degree + local_index
        weight = curve.weights[pole_index] if curve.kind is SplineKind.NURBS else 1.0
        coefficient = basis_value * weight
        numerator = _add(numerator, _scale(_vector(curve.control_points[pole_index]), coefficient))
        denominator += coefficient
    if denominator <= 1e-14:
        raise ValueError("curve evaluation produced a zero rational denominator")
    return _point(_scale(numerator, 1.0 / denominator))


def _derivatives(curve: SplineCurve, fraction: float) -> tuple[Vector, Vector, Vector]:
    step = 1e-4
    value = _vector(evaluate_curve(curve, fraction))
    if fraction <= step:
        p1 = _vector(evaluate_curve(curve, fraction + step))
        p2 = _vector(evaluate_curve(curve, fraction + 2 * step))
        p3 = _vector(evaluate_curve(curve, fraction + 3 * step))
        first = _scale(_add(_add(_scale(value, -3), _scale(p1, 4)), _scale(p2, -1)), 1 / (2 * step))
        second = _scale(
            _add(_add(_add(_scale(value, 2), _scale(p1, -5)), _scale(p2, 4)), _scale(p3, -1)),
            1 / (step * step),
        )
    elif fraction >= 1 - step:
        p1 = _vector(evaluate_curve(curve, fraction - step))
        p2 = _vector(evaluate_curve(curve, fraction - 2 * step))
        p3 = _vector(evaluate_curve(curve, fraction - 3 * step))
        first = _scale(_add(_add(_scale(value, 3), _scale(p1, -4)), p2), 1 / (2 * step))
        second = _scale(
            _add(_add(_add(_scale(value, 2), _scale(p1, -5)), _scale(p2, 4)), _scale(p3, -1)),
            1 / (step * step),
        )
    else:
        before = _vector(evaluate_curve(curve, fraction - step))
        after = _vector(evaluate_curve(curve, fraction + step))
        first = _scale(_sub(after, before), 1 / (2 * step))
        second = _scale(_add(_sub(after, _scale(value, 2)), before), 1 / (step * step))
    return value, first, second


def curve_jet(curve: SplineCurve, parameter_fraction: float) -> CurveJet:
    position, first, second = _derivatives(curve, parameter_fraction)
    tangent = _unit(first)
    if tangent is None:
        raise ValueError("curve has a degenerate tangent")
    speed_squared = _dot(first, first)
    normal_second = _sub(second, _scale(tangent, _dot(second, tangent)))
    curvature = _scale(normal_second, 1.0 / speed_squared)
    return CurveJet(_point(position), tangent, curvature)


def _passes(
    order: ContinuityOrder,
    position_gap: float,
    tangent_angle: float | None,
    curvature_gap: float | None,
    tolerance: ContinuityTolerance,
) -> bool:
    if position_gap > tolerance.position_mm:
        return False
    if order in {ContinuityOrder.G1, ContinuityOrder.G2} and (
        tangent_angle is None or tangent_angle > tolerance.tangent_angle_deg
    ):
        return False
    return not (
        order is ContinuityOrder.G2
        and (curvature_gap is None or curvature_gap > tolerance.curvature_per_mm)
    )


def check_join(
    left: SplineCurve,
    right: SplineCurve,
    requested_order: ContinuityOrder = ContinuityOrder.G2,
    tolerance: ContinuityTolerance | None = None,
) -> CurveJoinCheck:
    """Check the end of ``left`` against the start of ``right``."""

    if not isinstance(requested_order, ContinuityOrder):
        raise TypeError("requested_order must be ContinuityOrder")
    if tolerance is None:
        tolerance = ContinuityTolerance()
    if type(tolerance) is not ContinuityTolerance:
        raise TypeError("tolerance must be ContinuityTolerance")
    left_jet = curve_jet(left, 1.0)
    right_jet = curve_jet(right, 0.0)
    position_gap = _distance(_vector(left_jet.position), _vector(right_jet.position))
    tangent_angle = _angle(left_jet.tangent, right_jet.tangent)
    curvature_gap = _distance(left_jet.curvature, right_jet.curvature)
    achieved: ContinuityOrder | None = None
    for order in (ContinuityOrder.G0, ContinuityOrder.G1, ContinuityOrder.G2):
        if _passes(order, position_gap, tangent_angle, curvature_gap, tolerance):
            achieved = order
        else:
            break
    return CurveJoinCheck(
        requested_order,
        achieved,
        _passes(requested_order, position_gap, tangent_angle, curvature_gap, tolerance),
        position_gap,
        tangent_angle,
        curvature_gap,
        left_jet,
        right_jet,
    )


def check_paired_boundaries(
    left: SplineCurve,
    right: SplineCurve,
    requested_order: ContinuityOrder = ContinuityOrder.G2,
    tolerance: ContinuityTolerance | None = None,
    *,
    sample_count: int = 9,
    reversed: bool = False,
) -> SampledBoundaryCheck:
    """Sample two curve representations of the same intended patch boundary."""

    if not isinstance(requested_order, ContinuityOrder):
        raise TypeError("requested_order must be ContinuityOrder")
    if tolerance is None:
        tolerance = ContinuityTolerance()
    if type(tolerance) is not ContinuityTolerance:
        raise TypeError("tolerance must be ContinuityTolerance")
    if type(sample_count) is not int or not (
        MIN_CONTINUITY_SAMPLES <= sample_count <= MAX_CONTINUITY_SAMPLES
    ):
        raise ValueError("sample_count is outside the bounded range")
    if type(reversed) is not bool:
        raise TypeError("reversed must be bool")
    samples: list[BoundarySample] = []
    for index in range(sample_count):
        fraction = index / (sample_count - 1)
        right_fraction = 1 - fraction if reversed else fraction
        left_jet = curve_jet(left, fraction)
        right_jet = curve_jet(right, right_fraction)
        right_tangent = _scale(right_jet.tangent, -1) if reversed else right_jet.tangent
        position_gap = _distance(_vector(left_jet.position), _vector(right_jet.position))
        tangent_angle = _angle(left_jet.tangent, right_tangent)
        curvature_gap = _distance(left_jet.curvature, right_jet.curvature)
        samples.append(BoundarySample(fraction, position_gap, tangent_angle, curvature_gap))
    max_position = max(sample.position_gap_mm for sample in samples)
    tangent_values = [
        sample.tangent_angle_deg for sample in samples if sample.tangent_angle_deg is not None
    ]
    curvature_values = [
        sample.curvature_gap_per_mm for sample in samples if sample.curvature_gap_per_mm is not None
    ]
    max_tangent = max(tangent_values) if len(tangent_values) == len(samples) else None
    max_curvature = max(curvature_values) if len(curvature_values) == len(samples) else None
    return SampledBoundaryCheck(
        requested_order,
        _passes(requested_order, max_position, max_tangent, max_curvature, tolerance),
        reversed,
        tuple(samples),
        max_position,
        max_tangent,
        max_curvature,
    )


__all__ = [
    "MAX_CONTINUITY_SAMPLES",
    "MIN_CONTINUITY_SAMPLES",
    "BoundarySample",
    "ContinuityOrder",
    "ContinuityTolerance",
    "CurveJet",
    "CurveJoinCheck",
    "SampledBoundaryCheck",
    "check_join",
    "check_paired_boundaries",
    "curve_jet",
    "evaluate_curve",
]
