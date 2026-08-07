"""Focused tests for the authority-free planar metrology kernel."""

from __future__ import annotations

import math

import numpy as np
import pytest

from vibecad.visual.metrology import (
    MAX_CALIBRATION_LANDMARKS,
    MAX_MULTIVIEW_MEASUREMENTS,
    DimensionEstimate,
    MetrologyError,
    MetrologyErrorCode,
    MultiViewStatus,
    PixelPoint,
    PlanarLandmark,
    PlanePoint,
    ViewDimension,
    calibrate_planar_homography,
    map_pixel_to_plane,
    map_plane_to_pixel,
    measure_two_point_dimension,
    reconcile_multiview_dimensions,
)


def _transform(
    matrix: np.ndarray,
    point: tuple[float, float],
) -> tuple[float, float]:
    projected = matrix @ np.array((point[0], point[1], 1.0), dtype=np.float64)
    return float(projected[0] / projected[2]), float(projected[1] / projected[2])


def _projective_landmarks(
    *,
    pixel_uncertainty_px: float = 0.0,
    plane_uncertainty_mm: float = 0.0,
) -> tuple[np.ndarray, tuple[PlanarLandmark, ...]]:
    homography = np.array(
        (
            (0.12, 0.01, -5.0),
            (0.005, 0.15, 3.0),
            (0.0002, -0.0001, 1.0),
        ),
        dtype=np.float64,
    )
    pixels = (
        (100.0, 120.0),
        (600.0, 100.0),
        (650.0, 500.0),
        (80.0, 550.0),
        (320.0, 180.0),
        (500.0, 360.0),
        (220.0, 430.0),
        (360.0, 300.0),
    )
    landmarks = tuple(
        PlanarLandmark(
            pixel=PixelPoint(
                x_px=pixel[0],
                y_px=pixel[1],
                uncertainty_px=pixel_uncertainty_px,
            ),
            plane=PlanePoint(
                x_mm=_transform(homography, pixel)[0],
                y_mm=_transform(homography, pixel)[1],
                uncertainty_mm=plane_uncertainty_mm,
            ),
        )
        for pixel in pixels
    )
    return homography, landmarks


def _affine_landmarks(
    *,
    pixel_uncertainty_px: float = 0.0,
    plane_uncertainty_mm: float = 0.0,
) -> tuple[PlanarLandmark, ...]:
    pixels = ((0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0))
    return tuple(
        PlanarLandmark(
            pixel=PixelPoint(
                x_px=x,
                y_px=y,
                uncertainty_px=pixel_uncertainty_px,
            ),
            plane=PlanePoint(
                x_mm=0.5 * x,
                y_mm=0.5 * y,
                uncertainty_mm=plane_uncertainty_mm,
            ),
        )
        for x, y in pixels
    )


def _estimate(value_mm: float, error_mm: float) -> DimensionEstimate:
    return DimensionEstimate(
        value_mm=value_mm,
        lower_bound_mm=max(0.0, value_mm - error_mm),
        upper_bound_mm=value_mm + error_mm,
        calibration_error_bound_mm=0.0,
        point_error_bound_mm=error_mm,
        error_bound_mm=error_mm,
    )


def test_normalized_dlt_maps_projective_points_and_round_trips() -> None:
    expected_homography, landmarks = _projective_landmarks()

    calibration = calibrate_planar_homography(landmarks)
    pixel = PixelPoint(x_px=410.0, y_px=275.0)
    expected_plane = _transform(expected_homography, (pixel.x_px, pixel.y_px))
    plane = map_pixel_to_plane(calibration, pixel)
    round_trip = map_plane_to_pixel(
        calibration,
        PlanePoint(x_mm=plane.x_mm, y_mm=plane.y_mm),
    )

    assert calibration.landmark_count == 8
    assert calibration.rms_error_mm < 1e-10
    assert plane.x_mm == pytest.approx(expected_plane[0], abs=1e-10)
    assert plane.y_mm == pytest.approx(expected_plane[1], abs=1e-10)
    assert round_trip.x_px == pytest.approx(pixel.x_px, abs=1e-9)
    assert round_trip.y_px == pytest.approx(pixel.y_px, abs=1e-9)


def test_normalized_dlt_remains_stable_with_large_coordinate_offsets() -> None:
    pixels = (
        (100_000.0, 200_000.0),
        (101_000.0, 200_000.0),
        (101_000.0, 201_000.0),
        (100_000.0, 201_000.0),
        (100_250.0, 200_600.0),
        (100_800.0, 200_350.0),
    )
    landmarks = tuple(
        PlanarLandmark(
            pixel=PixelPoint(x_px=x, y_px=y),
            plane=PlanePoint(x_mm=0.01 * x - 1_000.0, y_mm=0.02 * y - 4_000.0),
        )
        for x, y in pixels
    )

    calibration = calibrate_planar_homography(landmarks)
    result = map_pixel_to_plane(
        calibration,
        PixelPoint(x_px=100_400.0, y_px=200_700.0),
    )

    assert result.x_mm == pytest.approx(4.0, abs=1e-8)
    assert result.y_mm == pytest.approx(14.0, abs=1e-8)


def test_two_point_dimension_separates_calibration_and_point_error_bounds() -> None:
    calibration = calibrate_planar_homography(
        _affine_landmarks(pixel_uncertainty_px=0.2, plane_uncertainty_mm=0.05)
    )

    estimate = measure_two_point_dimension(
        calibration,
        PixelPoint(x_px=10.0, y_px=10.0, uncertainty_px=0.1),
        PixelPoint(x_px=16.0, y_px=18.0, uncertainty_px=0.1),
    )

    assert estimate.value_mm == pytest.approx(5.0)
    assert estimate.point_error_bound_mm == pytest.approx(0.1, abs=1e-9)
    assert estimate.calibration_error_bound_mm == pytest.approx(0.3, abs=1e-6)
    assert estimate.error_bound_mm == pytest.approx(0.4, abs=1e-6)
    assert estimate.lower_bound_mm == pytest.approx(estimate.value_mm - estimate.error_bound_mm)
    assert estimate.upper_bound_mm == pytest.approx(estimate.value_mm + estimate.error_bound_mm)


def test_mapping_propagates_source_and_calibration_uncertainty() -> None:
    calibration = calibrate_planar_homography(
        _affine_landmarks(pixel_uncertainty_px=0.2, plane_uncertainty_mm=0.05)
    )

    plane = map_pixel_to_plane(
        calibration,
        PixelPoint(x_px=20.0, y_px=30.0, uncertainty_px=0.4),
    )
    pixel = map_plane_to_pixel(
        calibration,
        PlanePoint(x_mm=plane.x_mm, y_mm=plane.y_mm, uncertainty_mm=0.2),
    )

    assert plane.uncertainty_mm >= calibration.calibration_error_bound_mm + 0.2
    assert pixel.uncertainty_px > 0.0


def test_calibration_accepts_residuals_inside_declared_landmark_uncertainty() -> None:
    _, exact = _projective_landmarks(plane_uncertainty_mm=0.1)
    noisy = tuple(
        PlanarLandmark(
            pixel=item.pixel,
            plane=PlanePoint(
                x_mm=item.plane.x_mm + (0.02 if index % 2 == 0 else -0.02),
                y_mm=item.plane.y_mm + (0.01 if index % 3 == 0 else -0.01),
                uncertainty_mm=item.plane.uncertainty_mm,
            ),
        )
        for index, item in enumerate(exact)
    )

    calibration = calibrate_planar_homography(noisy)

    assert 0.0 < calibration.rms_error_mm < 0.1
    assert calibration.calibration_error_bound_mm >= (calibration.max_error_mm + 0.1)


def test_calibration_rejects_landmarks_that_exceed_declared_uncertainty() -> None:
    landmarks = list(_affine_landmarks())
    landmarks.append(
        PlanarLandmark(
            pixel=PixelPoint(x_px=50.0, y_px=50.0),
            plane=PlanePoint(x_mm=40.0, y_mm=25.0),
        )
    )

    with pytest.raises(MetrologyError) as caught:
        calibrate_planar_homography(landmarks)

    assert caught.value.code is MetrologyErrorCode.INCONSISTENT_CALIBRATION


@pytest.mark.parametrize(
    "landmarks",
    (
        tuple(
            PlanarLandmark(
                pixel=PixelPoint(x_px=float(index), y_px=float(index)),
                plane=PlanePoint(x_mm=float(index), y_mm=float(index)),
            )
            for index in range(4)
        ),
        _affine_landmarks()[:3],
    ),
)
def test_calibration_fails_closed_for_degenerate_or_insufficient_geometry(landmarks) -> None:
    with pytest.raises(MetrologyError) as caught:
        calibrate_planar_homography(landmarks)

    assert caught.value.code in {
        MetrologyErrorCode.DEGENERATE_GEOMETRY,
        MetrologyErrorCode.INVALID_INPUT,
    }


def test_calibration_budget_is_checked_before_landmark_content() -> None:
    with pytest.raises(MetrologyError) as caught:
        calibrate_planar_homography([None] * (MAX_CALIBRATION_LANDMARKS + 1))

    assert caught.value.code is MetrologyErrorCode.BUDGET_EXCEEDED


def test_projection_fails_closed_at_homography_horizon() -> None:
    homography = np.array(
        ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.01, 0.0, 1.0)),
        dtype=np.float64,
    )
    pixels = ((0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0), (50.0, 60.0))
    landmarks = tuple(
        PlanarLandmark(
            pixel=PixelPoint(x_px=x, y_px=y),
            plane=PlanePoint(
                x_mm=_transform(homography, (x, y))[0],
                y_mm=_transform(homography, (x, y))[1],
            ),
        )
        for x, y in pixels
    )
    calibration = calibrate_planar_homography(landmarks)

    with pytest.raises(MetrologyError) as caught:
        map_pixel_to_plane(calibration, PixelPoint(x_px=-100.0, y_px=0.0))
    with pytest.raises(MetrologyError) as uncertain:
        map_pixel_to_plane(
            calibration,
            PixelPoint(x_px=-99.0, y_px=0.0, uncertainty_px=2.0),
        )

    assert caught.value.code is MetrologyErrorCode.NUMERICAL_FAILURE
    assert uncertain.value.code is MetrologyErrorCode.NUMERICAL_FAILURE


def test_points_reject_nonfinite_and_boolean_provider_values() -> None:
    with pytest.raises(MetrologyError) as nonfinite:
        PixelPoint(x_px=math.nan, y_px=0.0)
    with pytest.raises(MetrologyError) as boolean:
        PlanePoint(x_mm=True, y_mm=0.0)

    assert nonfinite.value.code is MetrologyErrorCode.INVALID_INPUT
    assert boolean.value.code is MetrologyErrorCode.INVALID_INPUT


def test_multiview_decides_consistent_intersection_and_retains_unknown_views() -> None:
    decision = reconcile_multiview_dimensions(
        (
            ViewDimension(view_id="front", estimate=_estimate(10.0, 1.0)),
            ViewDimension(view_id="right", estimate=_estimate(11.0, 1.0)),
            ViewDimension(view_id="detail", estimate=None),
        )
    )

    assert decision.status is MultiViewStatus.CONSISTENT
    assert decision.intersection_lower_mm == pytest.approx(10.0)
    assert decision.intersection_upper_mm == pytest.approx(11.0)
    assert decision.conflict_gap_mm is None
    assert decision.contributing_views == ("front", "right")
    assert decision.unknown_views == ("detail",)


def test_multiview_decides_conflict_only_from_disjoint_local_intervals() -> None:
    decision = reconcile_multiview_dimensions(
        (
            ViewDimension(view_id="front", estimate=_estimate(9.0, 0.5)),
            ViewDimension(view_id="right", estimate=_estimate(11.0, 0.5)),
        )
    )

    assert decision.status is MultiViewStatus.CONFLICT
    assert decision.intersection_lower_mm is None
    assert decision.intersection_upper_mm is None
    assert decision.conflict_gap_mm == pytest.approx(1.0)


@pytest.mark.parametrize(
    "measurements",
    (
        (),
        (ViewDimension(view_id="front", estimate=_estimate(10.0, 0.5)),),
        (
            ViewDimension(view_id="front", estimate=None),
            ViewDimension(view_id="right", estimate=None),
        ),
    ),
)
def test_multiview_is_unknown_without_two_measured_intervals(measurements) -> None:
    decision = reconcile_multiview_dimensions(measurements)

    assert decision.status is MultiViewStatus.UNKNOWN
    assert decision.intersection_lower_mm is None
    assert decision.intersection_upper_mm is None
    assert decision.conflict_gap_mm is None


def test_multiview_budget_and_duplicate_view_ids_fail_closed() -> None:
    with pytest.raises(MetrologyError) as budget:
        reconcile_multiview_dimensions(
            tuple(
                ViewDimension(view_id=f"view-{index}", estimate=None)
                for index in range(MAX_MULTIVIEW_MEASUREMENTS + 1)
            )
        )
    with pytest.raises(MetrologyError) as duplicate:
        reconcile_multiview_dimensions(
            (
                ViewDimension(view_id="front", estimate=None),
                ViewDimension(view_id="front", estimate=None),
            )
        )

    assert budget.value.code is MetrologyErrorCode.BUDGET_EXCEEDED
    assert duplicate.value.code is MetrologyErrorCode.INVALID_INPUT
