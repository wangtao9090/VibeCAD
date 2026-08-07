from __future__ import annotations

import pytest

from vibecad.freeform.continuity import (
    ContinuityOrder,
    ContinuityTolerance,
    check_join,
    check_paired_boundaries,
    evaluate_curve,
)
from vibecad.freeform.contracts import CurveRole, Point3D, SplineCurve, SplineKind


def _open_curve(suffix: str, points: tuple[Point3D, Point3D, Point3D]) -> SplineCurve:
    return SplineCurve(
        f"freeform_curve_{suffix * 32}",
        f"guide-{suffix}",
        CurveRole.GUIDE,
        SplineKind.BSPLINE,
        2,
        points,
        (0, 1),
        (3, 3),
        (),
        False,
    )


def test_evaluate_quadratic_bspline_endpoints_and_midpoint() -> None:
    curve = _open_curve("a", (Point3D(0, 0, 0), Point3D(1, 0, 0), Point3D(2, 0, 0)))

    assert evaluate_curve(curve, 0) == Point3D(0, 0, 0)
    assert evaluate_curve(curve, 0.5) == Point3D(1, 0, 0)
    assert evaluate_curve(curve, 1) == Point3D(2, 0, 0)


def test_evaluate_rational_nurbs_uses_weights() -> None:
    curve = SplineCurve(
        f"freeform_curve_{'d' * 32}",
        "rational guide",
        CurveRole.GUIDE,
        SplineKind.NURBS,
        2,
        (Point3D(0, 0, 0), Point3D(1, 2, 0), Point3D(2, 0, 0)),
        (0, 1),
        (3, 3),
        (1, 2, 1),
        False,
    )

    midpoint = evaluate_curve(curve, 0.5)

    assert midpoint.x_mm == pytest.approx(1)
    assert midpoint.y_mm == pytest.approx(4 / 3)


def test_straight_join_satisfies_g2_sufficient_condition() -> None:
    left = _open_curve("a", (Point3D(0, 0, 0), Point3D(0.5, 0, 0), Point3D(1, 0, 0)))
    right = _open_curve("b", (Point3D(1, 0, 0), Point3D(1.5, 0, 0), Point3D(2, 0, 0)))

    result = check_join(left, right, ContinuityOrder.G2)

    assert result.passed
    assert result.achieved_order is ContinuityOrder.G2
    assert result.position_gap_mm == pytest.approx(0)
    assert result.tangent_angle_deg == pytest.approx(0)
    assert result.curvature_gap_per_mm == pytest.approx(0, abs=1e-6)


def test_join_reports_g0_when_tangent_direction_breaks() -> None:
    left = _open_curve("a", (Point3D(0, 0, 0), Point3D(0.5, 0, 0), Point3D(1, 0, 0)))
    right = _open_curve("b", (Point3D(1, 0, 0), Point3D(1, 0.5, 0), Point3D(1, 1, 0)))

    result = check_join(left, right, ContinuityOrder.G1)

    assert not result.passed
    assert result.achieved_order is ContinuityOrder.G0
    assert result.tangent_angle_deg == pytest.approx(90, abs=1e-3)


def test_sampled_boundary_check_supports_forward_and_reversed_pairs() -> None:
    forward = _open_curve("a", (Point3D(0, 0, 0), Point3D(1, 1, 0), Point3D(2, 0, 0)))
    same = _open_curve("b", (Point3D(0, 0, 0), Point3D(1, 1, 0), Point3D(2, 0, 0)))
    reverse = _open_curve("c", (Point3D(2, 0, 0), Point3D(1, 1, 0), Point3D(0, 0, 0)))

    direct = check_paired_boundaries(forward, same, sample_count=11)
    reversed_result = check_paired_boundaries(forward, reverse, sample_count=11, reversed=True)

    assert direct.passed
    assert reversed_result.passed
    assert direct.max_position_gap_mm == pytest.approx(0)
    assert reversed_result.max_tangent_angle_deg == pytest.approx(0, abs=1e-6)


def test_sampled_boundary_check_enforces_sample_budget_and_tolerance() -> None:
    left = _open_curve("a", (Point3D(0, 0, 0), Point3D(1, 0, 0), Point3D(2, 0, 0)))
    shifted = _open_curve("b", (Point3D(0, 0.1, 0), Point3D(1, 0.1, 0), Point3D(2, 0.1, 0)))

    result = check_paired_boundaries(
        left,
        shifted,
        ContinuityOrder.G0,
        ContinuityTolerance(position_mm=0.01),
    )
    assert not result.passed
    assert result.max_position_gap_mm == pytest.approx(0.1)

    with pytest.raises(ValueError):
        check_paired_boundaries(left, shifted, sample_count=2)
