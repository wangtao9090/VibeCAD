"""Focused tests for caller-declared, authority-free planar fitting."""

from __future__ import annotations

import math

import pytest

from vibecad.visual.geometry_fit import (
    MAX_FIT_POINTS,
    ArcPrimitive,
    CirclePrimitive,
    GeometryFitError,
    GeometryFitErrorCode,
    GeometryFitRequest,
    GeometryFitStatus,
    GeometryFitUnknownReason,
    LinePrimitive,
    PrimitiveFamily,
    RotatedRectanglePrimitive,
    fit_declared_geometry,
)
from vibecad.visual.metrology import PlanePoint


def _point(x: float, y: float, uncertainty: float = 0.0) -> PlanePoint:
    return PlanePoint(x_mm=x, y_mm=y, uncertainty_mm=uncertainty)


def _request(
    family: PrimitiveFamily,
    points: tuple[PlanePoint, ...],
    tolerance: float = 1e-6,
) -> GeometryFitRequest:
    return GeometryFitRequest(
        family=family,
        points=points,
        residual_tolerance_mm=tolerance,
    )


def test_line_fit_is_order_independent_and_has_canonical_direction() -> None:
    points = (
        _point(-4.0, -1.0),
        _point(0.0, 1.0),
        _point(6.0, 4.0),
        _point(2.0, 2.0),
    )

    forward = fit_declared_geometry(_request(PrimitiveFamily.LINE, points))
    reverse = fit_declared_geometry(_request(PrimitiveFamily.LINE, tuple(reversed(points))))

    assert forward == reverse
    assert forward.status is GeometryFitStatus.FITTED
    assert type(forward.primitive) is LinePrimitive
    assert forward.primitive.direction_x > 0.0
    assert forward.primitive.direction_y / forward.primitive.direction_x == pytest.approx(0.5)
    assert forward.max_residual_mm == pytest.approx(0.0, abs=1e-12)


def test_residual_limit_returns_unknown_and_point_uncertainty_is_conservative() -> None:
    points = (_point(0.0, 0.0), _point(5.0, 0.0), _point(10.0, 1.0))

    rejected = fit_declared_geometry(_request(PrimitiveFamily.LINE, points, tolerance=0.01))
    uncertain = fit_declared_geometry(
        _request(
            PrimitiveFamily.LINE,
            tuple(_point(point.x_mm, point.y_mm, uncertainty=0.5) for point in points),
            tolerance=0.01,
        )
    )

    assert rejected.status is GeometryFitStatus.UNKNOWN
    assert rejected.primitive is None
    assert rejected.unknown_reason is GeometryFitUnknownReason.RESIDUAL_EXCEEDED
    assert rejected.max_excess_residual_mm > 0.01
    assert uncertain.status is GeometryFitStatus.FITTED
    assert uncertain.max_residual_mm > uncertain.max_excess_residual_mm


def test_circle_fit_is_stable_at_large_coordinate_offset() -> None:
    center_x = 1_000_000.0
    center_y = -2_000_000.0
    radius = 35.0
    points = tuple(
        _point(
            center_x + radius * math.cos(angle),
            center_y + radius * math.sin(angle),
        )
        for angle in (0.0, 0.7, 1.9, 3.1, 4.4, 5.6)
    )

    result = fit_declared_geometry(_request(PrimitiveFamily.CIRCLE, points, tolerance=1e-7))

    assert result.status is GeometryFitStatus.FITTED
    assert type(result.primitive) is CirclePrimitive
    assert result.primitive.center_x_mm == pytest.approx(center_x, abs=1e-9)
    assert result.primitive.center_y_mm == pytest.approx(center_y, abs=1e-9)
    assert result.primitive.radius_mm == pytest.approx(radius, abs=1e-9)


def test_collinear_circle_and_insufficient_line_return_unknown() -> None:
    circle = fit_declared_geometry(
        _request(
            PrimitiveFamily.CIRCLE,
            (_point(0.0, 0.0), _point(1.0, 0.0), _point(2.0, 0.0)),
        )
    )
    line = fit_declared_geometry(_request(PrimitiveFamily.LINE, (_point(0.0, 0.0),)))

    assert circle.unknown_reason is GeometryFitUnknownReason.DEGENERATE_GEOMETRY
    assert line.unknown_reason is GeometryFitUnknownReason.INSUFFICIENT_POINTS
    assert circle.rms_residual_mm is None
    assert line.rms_residual_mm is None


def test_ordered_arc_crosses_angle_boundary_with_signed_sweep() -> None:
    center = (4.0, -3.0)
    radius = 8.0
    angles = (math.radians(170), math.radians(190), math.radians(225))
    points = tuple(
        _point(center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle))
        for angle in angles
    )

    result = fit_declared_geometry(_request(PrimitiveFamily.ARC, points))

    assert result.status is GeometryFitStatus.FITTED
    assert type(result.primitive) is ArcPrimitive
    assert result.primitive.center_x_mm == pytest.approx(center[0])
    assert result.primitive.center_y_mm == pytest.approx(center[1])
    assert result.primitive.radius_mm == pytest.approx(radius)
    assert result.primitive.start_angle_rad == pytest.approx(math.radians(170))
    assert result.primitive.sweep_angle_rad == pytest.approx(math.radians(55))


def test_arc_with_reversed_middle_order_is_unknown() -> None:
    points = tuple(
        _point(10.0 * math.cos(angle), 10.0 * math.sin(angle)) for angle in (0.0, 1.0, 0.5, 1.5)
    )

    result = fit_declared_geometry(_request(PrimitiveFamily.ARC, points))

    assert result.status is GeometryFitStatus.UNKNOWN
    assert result.unknown_reason is GeometryFitUnknownReason.AMBIGUOUS_ARC_ORDER


def test_rotated_rectangle_fit_preserves_declared_edge_order() -> None:
    center_x, center_y = 12.0, -7.0
    width, height = 20.0, 8.0
    angle = math.radians(31.0)
    direction_u = (math.cos(angle), math.sin(angle))
    direction_v = (-math.sin(angle), math.cos(angle))
    points = tuple(
        _point(
            center_x + sx * 0.5 * width * direction_u[0] + sy * 0.5 * height * direction_v[0],
            center_y + sx * 0.5 * width * direction_u[1] + sy * 0.5 * height * direction_v[1],
        )
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))
    )

    result = fit_declared_geometry(_request(PrimitiveFamily.ROTATED_RECTANGLE, points))

    assert result.status is GeometryFitStatus.FITTED
    assert type(result.primitive) is RotatedRectanglePrimitive
    assert result.primitive.center_x_mm == pytest.approx(center_x)
    assert result.primitive.center_y_mm == pytest.approx(center_y)
    assert result.primitive.width_mm == pytest.approx(width)
    assert result.primitive.height_mm == pytest.approx(height)
    assert result.primitive.angle_rad == pytest.approx(angle)
    assert result.max_residual_mm == pytest.approx(0.0, abs=1e-12)


def test_rectangle_noise_is_governed_by_explicit_residual_tolerance() -> None:
    points = (
        _point(-5.0, -2.0),
        _point(5.0, -2.0),
        _point(5.04, 2.03),
        _point(-5.0, 2.0),
    )

    accepted = fit_declared_geometry(
        _request(PrimitiveFamily.ROTATED_RECTANGLE, points, tolerance=0.1)
    )
    rejected = fit_declared_geometry(
        _request(PrimitiveFamily.ROTATED_RECTANGLE, points, tolerance=0.001)
    )

    assert accepted.status is GeometryFitStatus.FITTED
    assert accepted.max_residual_mm < 0.1
    assert rejected.status is GeometryFitStatus.UNKNOWN
    assert rejected.unknown_reason is GeometryFitUnknownReason.RESIDUAL_EXCEEDED


def test_rectangle_wrong_count_or_crossed_order_returns_unknown() -> None:
    wrong_count = fit_declared_geometry(
        _request(
            PrimitiveFamily.ROTATED_RECTANGLE,
            (_point(0.0, 0.0), _point(1.0, 0.0), _point(1.0, 1.0)),
        )
    )
    crossed = fit_declared_geometry(
        _request(
            PrimitiveFamily.ROTATED_RECTANGLE,
            (
                _point(0.0, 0.0),
                _point(1.0, 1.0),
                _point(0.0, 1.0),
                _point(1.0, 0.0),
            ),
        )
    )

    assert wrong_count.unknown_reason is GeometryFitUnknownReason.POINT_COUNT_MISMATCH
    assert crossed.unknown_reason is GeometryFitUnknownReason.INVALID_RECTANGLE_ORDER


def test_exact_request_and_point_budgets_fail_closed_before_fitting() -> None:
    with pytest.raises(GeometryFitError) as non_tuple:
        GeometryFitRequest(
            family=PrimitiveFamily.LINE,
            points=[_point(0.0, 0.0), _point(1.0, 1.0)],  # type: ignore[arg-type]
            residual_tolerance_mm=0.1,
        )
    assert non_tuple.value.code is GeometryFitErrorCode.INVALID_INPUT

    with pytest.raises(GeometryFitError) as over_budget:
        GeometryFitRequest(
            family=PrimitiveFamily.LINE,
            points=tuple(_point(float(index), 0.0) for index in range(MAX_FIT_POINTS + 1)),
            residual_tolerance_mm=0.1,
        )
    assert over_budget.value.code is GeometryFitErrorCode.BUDGET_EXCEEDED

    request = _request(PrimitiveFamily.LINE, (_point(0.0, 0.0), _point(1.0, 1.0)))
    with pytest.raises(GeometryFitError) as forged_request:
        fit_declared_geometry(object())  # type: ignore[arg-type]
    assert forged_request.value.code is GeometryFitErrorCode.INVALID_INPUT
    assert fit_declared_geometry(request).status is GeometryFitStatus.FITTED
