"""Authority-free planar metrology for visual landmarks.

Vision providers may propose only bounded pixel/plane landmarks and point
locations.  This module owns the numerical calibration, explicit calibration
domains, projection, uncertainty propagation, and multi-view interval
decision.  Calibrations with landmark uncertainty or residual-only evidence
remain advisory and cannot create conservative intervals or conflicts.  The
module has no access to Task, Revision, adoption, or durable-observation
authority.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray

MAX_CALIBRATION_LANDMARKS = 64
MAX_MULTIVIEW_MEASUREMENTS = 16
MAX_ABS_PIXEL_COORDINATE = 10_000_000.0
MAX_ABS_PLANE_COORDINATE_MM = 1_000_000_000.0
MAX_PIXEL_UNCERTAINTY_PX = 1_000_000.0
MAX_PLANE_UNCERTAINTY_MM = 100_000_000.0
MAX_HOMOGRAPHY_CONDITION = 1_000_000_000_000.0

_MIN_GEOMETRY_SPAN = 1e-12
_MIN_AXIS_RATIO = 1e-7
_MIN_RANK_RATIO = 1e-12
_PROJECTION_EPSILON = 1e-12
_ELIGIBILITY_EPSILON_FACTOR = 4096.0
_DOMAIN_EPSILON_FACTOR = 256.0
_VIEW_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

type FloatMatrix = tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]
type PointDomain = tuple[tuple[float, float], ...]


class MetrologyErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    DEGENERATE_GEOMETRY = "degenerate_geometry"
    INCONSISTENT_CALIBRATION = "inconsistent_calibration"
    NUMERICAL_FAILURE = "numerical_failure"
    OUTSIDE_CALIBRATION_DOMAIN = "outside_calibration_domain"
    CALIBRATION_NOT_DECISION_ELIGIBLE = "calibration_not_decision_eligible"


class MetrologyError(ValueError):
    """Bounded failure that does not reflect rejected provider data."""

    def __init__(self, code: MetrologyErrorCode, path: str = "") -> None:
        if type(code) is not MetrologyErrorCode:
            raise TypeError("code must be an exact MetrologyErrorCode")
        if type(path) is not str or len(path.encode("utf-8")) > 256:
            raise ValueError("path must be a bounded string")
        self.code = code
        self.path = path
        super().__init__(code.value)


def _fail(code: MetrologyErrorCode, path: str = "") -> None:
    raise MetrologyError(code, path)


def _finite_number(
    value: object,
    *,
    maximum: float,
    path: str,
    nonnegative: bool = False,
) -> float:
    if type(value) not in {int, float}:
        _fail(MetrologyErrorCode.INVALID_INPUT, path)
    converted = float(value)
    if (
        not math.isfinite(converted)
        or abs(converted) > maximum
        or (nonnegative and converted < 0.0)
    ):
        _fail(MetrologyErrorCode.INVALID_INPUT, path)
    return converted


def _output_number(value: float, *, maximum: float, path: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or abs(converted) > maximum:
        _fail(MetrologyErrorCode.NUMERICAL_FAILURE, path)
    return converted


@dataclass(frozen=True, slots=True, kw_only=True)
class PixelPoint:
    x_px: int | float
    y_px: int | float
    uncertainty_px: int | float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "x_px",
            _finite_number(
                self.x_px,
                maximum=MAX_ABS_PIXEL_COORDINATE,
                path="/x_px",
            ),
        )
        object.__setattr__(
            self,
            "y_px",
            _finite_number(
                self.y_px,
                maximum=MAX_ABS_PIXEL_COORDINATE,
                path="/y_px",
            ),
        )
        object.__setattr__(
            self,
            "uncertainty_px",
            _finite_number(
                self.uncertainty_px,
                maximum=MAX_PIXEL_UNCERTAINTY_PX,
                path="/uncertainty_px",
                nonnegative=True,
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanePoint:
    x_mm: int | float
    y_mm: int | float
    uncertainty_mm: int | float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "x_mm",
            _finite_number(
                self.x_mm,
                maximum=MAX_ABS_PLANE_COORDINATE_MM,
                path="/x_mm",
            ),
        )
        object.__setattr__(
            self,
            "y_mm",
            _finite_number(
                self.y_mm,
                maximum=MAX_ABS_PLANE_COORDINATE_MM,
                path="/y_mm",
            ),
        )
        object.__setattr__(
            self,
            "uncertainty_mm",
            _finite_number(
                self.uncertainty_mm,
                maximum=MAX_PLANE_UNCERTAINTY_MM,
                path="/uncertainty_mm",
                nonnegative=True,
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanarLandmark:
    """One provider-proposed correspondence; it carries no decision authority."""

    pixel: PixelPoint
    plane: PlanePoint

    def __post_init__(self) -> None:
        if type(self.pixel) is not PixelPoint:
            _fail(MetrologyErrorCode.INVALID_INPUT, "/pixel")
        if type(self.plane) is not PlanePoint:
            _fail(MetrologyErrorCode.INVALID_INPUT, "/plane")


def _matrix3(value: object, path: str) -> FloatMatrix:
    if type(value) not in {tuple, list} or len(value) != 3:
        _fail(MetrologyErrorCode.INVALID_INPUT, path)
    rows: list[tuple[float, float, float]] = []
    for row_index, row in enumerate(value):
        if type(row) not in {tuple, list} or len(row) != 3:
            _fail(MetrologyErrorCode.INVALID_INPUT, f"{path}/{row_index}")
        numbers = tuple(
            _finite_number(
                item,
                maximum=MAX_ABS_PLANE_COORDINATE_MM,
                path=f"{path}/{row_index}/{column_index}",
            )
            for column_index, item in enumerate(row)
        )
        rows.append(numbers)  # type: ignore[arg-type]
    return (rows[0], rows[1], rows[2])


def _as_array(matrix: FloatMatrix) -> NDArray[np.float64]:
    return np.asarray(matrix, dtype=np.float64)


def _cross(
    origin: tuple[float, float],
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    return (first[0] - origin[0]) * (second[1] - origin[1]) - (first[1] - origin[1]) * (
        second[0] - origin[0]
    )


def _convex_hull(points: NDArray[np.float64]) -> PointDomain:
    """Return the strict counter-clockwise hull of bounded finite points."""

    ordered = sorted({(float(point[0]), float(point[1])) for point in points})
    if len(ordered) < 3:
        _fail(MetrologyErrorCode.DEGENERATE_GEOMETRY)

    lower: list[tuple[float, float]] = []
    for point in ordered:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], point) <= 0.0:
            lower.pop()
        lower.append(point)
    upper: list[tuple[float, float]] = []
    for point in reversed(ordered):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], point) <= 0.0:
            upper.pop()
        upper.append(point)
    hull = tuple(lower[:-1] + upper[:-1])
    if len(hull) < 3:
        _fail(MetrologyErrorCode.DEGENERATE_GEOMETRY)
    return hull


def _checked_domain(value: object, *, maximum: float, path: str) -> PointDomain:
    if type(value) is not tuple or not 3 <= len(value) <= MAX_CALIBRATION_LANDMARKS:
        _fail(MetrologyErrorCode.INVALID_INPUT, path)
    vertices: list[tuple[float, float]] = []
    for index, vertex in enumerate(value):
        if type(vertex) is not tuple or len(vertex) != 2:
            _fail(MetrologyErrorCode.INVALID_INPUT, f"{path}/{index}")
        vertices.append(
            (
                _finite_number(vertex[0], maximum=maximum, path=f"{path}/{index}/0"),
                _finite_number(vertex[1], maximum=maximum, path=f"{path}/{index}/1"),
            )
        )
    domain = tuple(vertices)
    if len(set(domain)) != len(domain):
        _fail(MetrologyErrorCode.INVALID_INPUT, path)
    for index in range(len(domain)):
        if _cross(domain[index - 1], domain[index], domain[(index + 1) % len(domain)]) <= 0.0:
            _fail(MetrologyErrorCode.INVALID_INPUT, path)
    return domain


def _require_domain_contains(
    domain: PointDomain,
    *,
    x: float,
    y: float,
    uncertainty: float,
    path: str,
) -> None:
    """Require the complete circular uncertainty region to lie in a convex domain."""

    point = (x, y)
    coordinate_span = max(
        1.0,
        max(vertex[0] for vertex in domain) - min(vertex[0] for vertex in domain),
        max(vertex[1] for vertex in domain) - min(vertex[1] for vertex in domain),
    )
    for index, start in enumerate(domain):
        end = domain[(index + 1) % len(domain)]
        edge_length = math.hypot(end[0] - start[0], end[1] - start[1])
        tolerance = (
            _DOMAIN_EPSILON_FACTOR
            * np.finfo(np.float64).eps
            * max(1.0, edge_length * coordinate_span)
        )
        if _cross(start, end, point) + tolerance < uncertainty * edge_length:
            _fail(MetrologyErrorCode.OUTSIDE_CALIBRATION_DOMAIN, path)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanarCalibration:
    """A fitted homography plus the bounded domains and decision eligibility."""

    pixel_to_plane: FloatMatrix
    plane_to_pixel: FloatMatrix
    valid_pixel_domain: PointDomain
    valid_plane_domain: PointDomain
    landmark_count: int
    rms_error_mm: int | float
    max_error_mm: int | float
    fit_error_indicator_mm: int | float
    condition_number: int | float
    decision_eligible: bool

    def __post_init__(self) -> None:
        pixel_to_plane = _matrix3(self.pixel_to_plane, "/pixel_to_plane")
        plane_to_pixel = _matrix3(self.plane_to_pixel, "/plane_to_pixel")
        valid_pixel_domain = _checked_domain(
            self.valid_pixel_domain,
            maximum=MAX_ABS_PIXEL_COORDINATE,
            path="/valid_pixel_domain",
        )
        valid_plane_domain = _checked_domain(
            self.valid_plane_domain,
            maximum=MAX_ABS_PLANE_COORDINATE_MM,
            path="/valid_plane_domain",
        )
        if (
            type(self.landmark_count) is not int
            or not 4 <= self.landmark_count <= MAX_CALIBRATION_LANDMARKS
        ):
            _fail(MetrologyErrorCode.INVALID_INPUT, "/landmark_count")
        rms = _finite_number(
            self.rms_error_mm,
            maximum=MAX_PLANE_UNCERTAINTY_MM,
            path="/rms_error_mm",
            nonnegative=True,
        )
        maximum = _finite_number(
            self.max_error_mm,
            maximum=MAX_PLANE_UNCERTAINTY_MM,
            path="/max_error_mm",
            nonnegative=True,
        )
        indicator = _finite_number(
            self.fit_error_indicator_mm,
            maximum=MAX_PLANE_UNCERTAINTY_MM,
            path="/fit_error_indicator_mm",
            nonnegative=True,
        )
        condition = _finite_number(
            self.condition_number,
            maximum=MAX_HOMOGRAPHY_CONDITION,
            path="/condition_number",
            nonnegative=True,
        )
        if type(self.decision_eligible) is not bool:
            _fail(MetrologyErrorCode.INVALID_INPUT, "/decision_eligible")
        if condition < 1.0 or rms > maximum or maximum > indicator:
            _fail(MetrologyErrorCode.INVALID_INPUT)
        try:
            actual_condition = float(np.linalg.cond(_as_array(pixel_to_plane)))
        except np.linalg.LinAlgError:
            _fail(MetrologyErrorCode.INVALID_INPUT)
        if (
            not math.isfinite(actual_condition)
            or actual_condition > MAX_HOMOGRAPHY_CONDITION
            or not math.isclose(condition, actual_condition, rel_tol=1e-10, abs_tol=1e-10)
        ):
            _fail(MetrologyErrorCode.INVALID_INPUT)
        product = _as_array(pixel_to_plane) @ _as_array(plane_to_pixel)
        if not np.all(np.isfinite(product)) or not np.allclose(
            product,
            np.eye(3, dtype=np.float64),
            rtol=1e-8,
            atol=1e-8,
        ):
            _fail(MetrologyErrorCode.INVALID_INPUT)
        object.__setattr__(self, "pixel_to_plane", pixel_to_plane)
        object.__setattr__(self, "plane_to_pixel", plane_to_pixel)
        object.__setattr__(self, "valid_pixel_domain", valid_pixel_domain)
        object.__setattr__(self, "valid_plane_domain", valid_plane_domain)
        object.__setattr__(self, "rms_error_mm", rms)
        object.__setattr__(self, "max_error_mm", maximum)
        object.__setattr__(self, "fit_error_indicator_mm", indicator)
        object.__setattr__(self, "condition_number", condition)


@dataclass(frozen=True, slots=True, kw_only=True)
class DimensionEstimate:
    """A point estimate with an interval only when decision eligible."""

    value_mm: int | float
    lower_bound_mm: int | float | None
    upper_bound_mm: int | float | None
    calibration_error_bound_mm: int | float | None
    point_error_bound_mm: int | float
    error_bound_mm: int | float | None
    decision_eligible: bool

    def __post_init__(self) -> None:
        value = _finite_number(
            self.value_mm,
            maximum=MAX_ABS_PLANE_COORDINATE_MM,
            path="/value_mm",
            nonnegative=True,
        )
        point_error = _finite_number(
            self.point_error_bound_mm,
            maximum=MAX_ABS_PLANE_COORDINATE_MM,
            path="/point_error_bound_mm",
            nonnegative=True,
        )
        if value <= 0.0 or type(self.decision_eligible) is not bool:
            _fail(MetrologyErrorCode.INVALID_INPUT)
        object.__setattr__(self, "value_mm", value)
        object.__setattr__(self, "point_error_bound_mm", point_error)
        if not self.decision_eligible:
            if any(
                item is not None
                for item in (
                    self.lower_bound_mm,
                    self.upper_bound_mm,
                    self.calibration_error_bound_mm,
                    self.error_bound_mm,
                )
            ):
                _fail(MetrologyErrorCode.INVALID_INPUT)
            return

        values: dict[str, float] = {}
        for field_name in (
            "lower_bound_mm",
            "upper_bound_mm",
            "calibration_error_bound_mm",
            "error_bound_mm",
        ):
            field_value = getattr(self, field_name)
            if field_value is None:
                _fail(MetrologyErrorCode.INVALID_INPUT, f"/{field_name}")
            values[field_name] = _finite_number(
                field_value,
                maximum=MAX_ABS_PLANE_COORDINATE_MM,
                path=f"/{field_name}",
                nonnegative=True,
            )
            object.__setattr__(self, field_name, values[field_name])
        if (
            values["lower_bound_mm"] > value
            or values["upper_bound_mm"] < value
            or not math.isclose(
                values["calibration_error_bound_mm"] + point_error,
                values["error_bound_mm"],
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
            or not math.isclose(
                max(0.0, value - values["error_bound_mm"]),
                values["lower_bound_mm"],
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
            or not math.isclose(
                value + values["error_bound_mm"],
                values["upper_bound_mm"],
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        ):
            _fail(MetrologyErrorCode.INVALID_INPUT)


@dataclass(frozen=True, slots=True, kw_only=True)
class ViewDimension:
    view_id: str
    estimate: DimensionEstimate | None

    def __post_init__(self) -> None:
        if type(self.view_id) is not str or _VIEW_ID.fullmatch(self.view_id) is None:
            _fail(MetrologyErrorCode.INVALID_INPUT, "/view_id")
        if self.estimate is not None and type(self.estimate) is not DimensionEstimate:
            _fail(MetrologyErrorCode.INVALID_INPUT, "/estimate")


class MultiViewStatus(StrEnum):
    CONSISTENT = "consistent"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class MultiViewDecision:
    status: MultiViewStatus
    intersection_lower_mm: float | None
    intersection_upper_mm: float | None
    conflict_gap_mm: float | None
    contributing_views: tuple[str, ...]
    unknown_views: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.status) is not MultiViewStatus:
            _fail(MetrologyErrorCode.INVALID_INPUT, "/status")
        if (
            type(self.contributing_views) is not tuple
            or type(self.unknown_views) is not tuple
            or any(
                type(item) is not str or _VIEW_ID.fullmatch(item) is None
                for item in self.contributing_views
            )
            or any(
                type(item) is not str or _VIEW_ID.fullmatch(item) is None
                for item in self.unknown_views
            )
            or len(set(self.contributing_views + self.unknown_views))
            != len(self.contributing_views) + len(self.unknown_views)
            or len(self.contributing_views) + len(self.unknown_views) > MAX_MULTIVIEW_MEASUREMENTS
        ):
            _fail(MetrologyErrorCode.INVALID_INPUT)
        if self.status is MultiViewStatus.CONSISTENT:
            if (
                len(self.contributing_views) < 2
                or self.intersection_lower_mm is None
                or self.intersection_upper_mm is None
                or self.conflict_gap_mm is not None
                or self.intersection_lower_mm > self.intersection_upper_mm
            ):
                _fail(MetrologyErrorCode.INVALID_INPUT)
            lower = _finite_number(
                self.intersection_lower_mm,
                maximum=MAX_ABS_PLANE_COORDINATE_MM,
                path="/intersection_lower_mm",
                nonnegative=True,
            )
            upper = _finite_number(
                self.intersection_upper_mm,
                maximum=MAX_ABS_PLANE_COORDINATE_MM,
                path="/intersection_upper_mm",
                nonnegative=True,
            )
            object.__setattr__(self, "intersection_lower_mm", lower)
            object.__setattr__(self, "intersection_upper_mm", upper)
        elif self.status is MultiViewStatus.CONFLICT:
            if (
                len(self.contributing_views) < 2
                or self.intersection_lower_mm is not None
                or self.intersection_upper_mm is not None
                or self.conflict_gap_mm is None
                or self.conflict_gap_mm <= 0.0
            ):
                _fail(MetrologyErrorCode.INVALID_INPUT)
            gap = _finite_number(
                self.conflict_gap_mm,
                maximum=MAX_ABS_PLANE_COORDINATE_MM,
                path="/conflict_gap_mm",
                nonnegative=True,
            )
            object.__setattr__(self, "conflict_gap_mm", gap)
        elif (
            self.intersection_lower_mm is not None
            or self.intersection_upper_mm is not None
            or self.conflict_gap_mm is not None
            or len(self.contributing_views) >= 2
        ):
            _fail(MetrologyErrorCode.INVALID_INPUT)


def _bounded_landmarks(value: object) -> tuple[PlanarLandmark, ...]:
    if type(value) not in {tuple, list}:
        _fail(MetrologyErrorCode.INVALID_INPUT, "/landmarks")
    try:
        count = len(value)
    except Exception:
        _fail(MetrologyErrorCode.INVALID_INPUT, "/landmarks")
    if count > MAX_CALIBRATION_LANDMARKS:
        _fail(MetrologyErrorCode.BUDGET_EXCEEDED, "/landmarks")
    if count < 4:
        _fail(MetrologyErrorCode.INVALID_INPUT, "/landmarks")
    try:
        landmarks = tuple(value)
    except Exception:
        _fail(MetrologyErrorCode.INVALID_INPUT, "/landmarks")
    if len(landmarks) != count or any(type(item) is not PlanarLandmark for item in landmarks):
        _fail(MetrologyErrorCode.INVALID_INPUT, "/landmarks")
    return landmarks


def _normalize_points(
    points: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    center = np.mean(points, axis=0)
    centered = points - center
    distances = np.linalg.norm(centered, axis=1)
    mean_distance = float(np.mean(distances))
    if not math.isfinite(mean_distance) or mean_distance <= _MIN_GEOMETRY_SPAN:
        _fail(MetrologyErrorCode.DEGENERATE_GEOMETRY)
    try:
        singular = np.linalg.svd(centered, compute_uv=False)
    except np.linalg.LinAlgError:
        _fail(MetrologyErrorCode.NUMERICAL_FAILURE)
    if (
        singular.shape != (2,)
        or singular[0] <= _MIN_GEOMETRY_SPAN
        or singular[1] / singular[0] <= _MIN_AXIS_RATIO
    ):
        _fail(MetrologyErrorCode.DEGENERATE_GEOMETRY)
    scale = math.sqrt(2.0) / mean_distance
    transform = np.array(
        (
            (scale, 0.0, -scale * center[0]),
            (0.0, scale, -scale * center[1]),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )
    homogeneous = np.column_stack((points, np.ones(points.shape[0], dtype=np.float64)))
    normalized = (transform @ homogeneous.T).T[:, :2]
    if not np.all(np.isfinite(normalized)):
        _fail(MetrologyErrorCode.NUMERICAL_FAILURE)
    return normalized, transform


def _project(
    matrix: NDArray[np.float64],
    x: float,
    y: float,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    numerator_x = matrix[0, 0] * x + matrix[0, 1] * y + matrix[0, 2]
    numerator_y = matrix[1, 0] * x + matrix[1, 1] * y + matrix[1, 2]
    denominator = matrix[2, 0] * x + matrix[2, 1] * y + matrix[2, 2]
    denominator_scale = abs(matrix[2, 0] * x) + abs(matrix[2, 1] * y) + abs(matrix[2, 2])
    if not math.isfinite(float(denominator)) or abs(denominator) <= _PROJECTION_EPSILON * max(
        1.0, denominator_scale
    ):
        _fail(MetrologyErrorCode.NUMERICAL_FAILURE)
    output_x = numerator_x / denominator
    output_y = numerator_y / denominator
    output = np.array((output_x, output_y), dtype=np.float64)
    jacobian = np.array(
        (
            (
                (matrix[0, 0] - output_x * matrix[2, 0]) / denominator,
                (matrix[0, 1] - output_x * matrix[2, 1]) / denominator,
            ),
            (
                (matrix[1, 0] - output_y * matrix[2, 0]) / denominator,
                (matrix[1, 1] - output_y * matrix[2, 1]) / denominator,
            ),
        ),
        dtype=np.float64,
    )
    if not np.all(np.isfinite(output)) or not np.all(np.isfinite(jacobian)):
        _fail(MetrologyErrorCode.NUMERICAL_FAILURE)
    return output, jacobian


def _operator_norm(jacobian: NDArray[np.float64]) -> float:
    try:
        result = float(np.linalg.norm(jacobian, ord=2))
    except np.linalg.LinAlgError:
        _fail(MetrologyErrorCode.NUMERICAL_FAILURE)
    if not math.isfinite(result):
        _fail(MetrologyErrorCode.NUMERICAL_FAILURE)
    return result


def _projected_uncertainty_bound(
    matrix: NDArray[np.float64],
    x: float,
    y: float,
    uncertainty: float,
    jacobian: NDArray[np.float64],
) -> float:
    """Bound projective displacement for a circular source-point error."""

    if uncertainty == 0.0:
        return 0.0
    denominator = float(matrix[2, 0] * x + matrix[2, 1] * y + matrix[2, 2])
    denominator_variation = float(np.linalg.norm(matrix[2, :2])) * uncertainty
    margin = abs(denominator) - denominator_variation
    if margin <= _PROJECTION_EPSILON * max(1.0, abs(denominator)):
        _fail(MetrologyErrorCode.NUMERICAL_FAILURE)
    result = _operator_norm(jacobian) * uncertainty * abs(denominator) / margin
    if not math.isfinite(result):
        _fail(MetrologyErrorCode.NUMERICAL_FAILURE)
    return result


def calibrate_planar_homography(landmarks: object) -> PlanarCalibration:
    """Fit a pixel-to-millimetre homography using normalized DLT."""

    checked = _bounded_landmarks(landmarks)
    pixels = np.array(
        tuple((item.pixel.x_px, item.pixel.y_px) for item in checked),
        dtype=np.float64,
    )
    planes = np.array(
        tuple((item.plane.x_mm, item.plane.y_mm) for item in checked),
        dtype=np.float64,
    )
    normalized_pixels, pixel_transform = _normalize_points(pixels)
    normalized_planes, plane_transform = _normalize_points(planes)

    design = np.zeros((2 * len(checked), 9), dtype=np.float64)
    for index, ((pixel_x, pixel_y), (plane_x, plane_y)) in enumerate(
        zip(normalized_pixels, normalized_planes, strict=True)
    ):
        design[2 * index] = (
            -pixel_x,
            -pixel_y,
            -1.0,
            0.0,
            0.0,
            0.0,
            plane_x * pixel_x,
            plane_x * pixel_y,
            plane_x,
        )
        design[2 * index + 1] = (
            0.0,
            0.0,
            0.0,
            -pixel_x,
            -pixel_y,
            -1.0,
            plane_y * pixel_x,
            plane_y * pixel_y,
            plane_y,
        )
    try:
        _, singular, right_vectors = np.linalg.svd(design, full_matrices=True)
    except np.linalg.LinAlgError:
        _fail(MetrologyErrorCode.NUMERICAL_FAILURE)
    if (
        singular.size < 8
        or singular[0] <= _MIN_GEOMETRY_SPAN
        or singular[7] / singular[0] <= _MIN_RANK_RATIO
    ):
        _fail(MetrologyErrorCode.DEGENERATE_GEOMETRY)
    normalized_homography = right_vectors[-1].reshape(3, 3)
    try:
        homography = np.linalg.inv(plane_transform) @ normalized_homography @ pixel_transform
    except np.linalg.LinAlgError:
        _fail(MetrologyErrorCode.NUMERICAL_FAILURE)
    scale = float(np.linalg.norm(homography))
    if not math.isfinite(scale) or scale <= _MIN_GEOMETRY_SPAN:
        _fail(MetrologyErrorCode.NUMERICAL_FAILURE)
    homography /= scale
    largest = int(np.argmax(np.abs(homography)))
    if homography.flat[largest] < 0.0:
        homography *= -1.0
    try:
        inverse = np.linalg.inv(homography)
        condition = float(np.linalg.cond(homography))
    except np.linalg.LinAlgError:
        _fail(MetrologyErrorCode.NUMERICAL_FAILURE)
    if not math.isfinite(condition) or condition > MAX_HOMOGRAPHY_CONDITION:
        _fail(MetrologyErrorCode.NUMERICAL_FAILURE)

    residuals: list[float] = []
    error_bounds: list[float] = []
    plane_scale = max(1.0, float(np.max(np.abs(planes))))
    numerical_slack = 1e-9 * plane_scale
    for landmark in checked:
        projected, jacobian = _project(
            homography,
            landmark.pixel.x_px,
            landmark.pixel.y_px,
        )
        residual = float(
            np.linalg.norm(projected - np.array((landmark.plane.x_mm, landmark.plane.y_mm)))
        )
        allowed = landmark.plane.uncertainty_mm + _projected_uncertainty_bound(
            homography,
            landmark.pixel.x_px,
            landmark.pixel.y_px,
            landmark.pixel.uncertainty_px,
            jacobian,
        )
        if not math.isfinite(residual) or residual > allowed + numerical_slack:
            _fail(MetrologyErrorCode.INCONSISTENT_CALIBRATION)
        residuals.append(residual)
        error_bounds.append(residual + allowed + numerical_slack)

    rms = math.sqrt(sum(item * item for item in residuals) / len(residuals))
    maximum = max(residuals)
    fit_error_indicator = max(error_bounds)
    for value, path in (
        (rms, "/rms_error_mm"),
        (maximum, "/max_error_mm"),
        (fit_error_indicator, "/fit_error_indicator_mm"),
    ):
        _output_number(value, maximum=MAX_PLANE_UNCERTAINTY_MM, path=path)
    plane_span = max(1.0, float(np.max(np.ptp(planes, axis=0))))
    eligibility_tolerance = _ELIGIBILITY_EPSILON_FACTOR * np.finfo(np.float64).eps * plane_span
    has_declared_uncertainty = any(
        landmark.pixel.uncertainty_px > 0.0 or landmark.plane.uncertainty_mm > 0.0
        for landmark in checked
    )
    decision_eligible = bool(not has_declared_uncertainty and maximum <= eligibility_tolerance)
    matrix = tuple(tuple(float(item) for item in row) for row in homography)
    inverse_matrix = tuple(tuple(float(item) for item in row) for row in inverse)
    return PlanarCalibration(
        pixel_to_plane=matrix,  # type: ignore[arg-type]
        plane_to_pixel=inverse_matrix,  # type: ignore[arg-type]
        valid_pixel_domain=_convex_hull(pixels),
        valid_plane_domain=_convex_hull(planes),
        landmark_count=len(checked),
        rms_error_mm=rms,
        max_error_mm=maximum,
        fit_error_indicator_mm=fit_error_indicator,
        condition_number=condition,
        decision_eligible=decision_eligible,
    )


def _checked_calibration(value: object) -> PlanarCalibration:
    if type(value) is not PlanarCalibration:
        _fail(MetrologyErrorCode.INVALID_INPUT, "/calibration")
    return value


def _require_decision_eligible(calibration: PlanarCalibration) -> None:
    if not calibration.decision_eligible:
        _fail(MetrologyErrorCode.CALIBRATION_NOT_DECISION_ELIGIBLE, "/calibration")


def map_pixel_to_plane(calibration: object, point: object) -> PlanePoint:
    checked = _checked_calibration(calibration)
    if type(point) is not PixelPoint:
        _fail(MetrologyErrorCode.INVALID_INPUT, "/point")
    _require_domain_contains(
        checked.valid_pixel_domain,
        x=point.x_px,
        y=point.y_px,
        uncertainty=point.uncertainty_px,
        path="/point",
    )
    _require_decision_eligible(checked)
    projected, jacobian = _project(
        _as_array(checked.pixel_to_plane),
        point.x_px,
        point.y_px,
    )
    uncertainty = _projected_uncertainty_bound(
        _as_array(checked.pixel_to_plane),
        point.x_px,
        point.y_px,
        point.uncertainty_px,
        jacobian,
    )
    return PlanePoint(
        x_mm=_output_number(
            projected[0],
            maximum=MAX_ABS_PLANE_COORDINATE_MM,
            path="/x_mm",
        ),
        y_mm=_output_number(
            projected[1],
            maximum=MAX_ABS_PLANE_COORDINATE_MM,
            path="/y_mm",
        ),
        uncertainty_mm=_output_number(
            uncertainty,
            maximum=MAX_PLANE_UNCERTAINTY_MM,
            path="/uncertainty_mm",
        ),
    )


def map_plane_to_pixel(calibration: object, point: object) -> PixelPoint:
    checked = _checked_calibration(calibration)
    if type(point) is not PlanePoint:
        _fail(MetrologyErrorCode.INVALID_INPUT, "/point")
    _require_domain_contains(
        checked.valid_plane_domain,
        x=point.x_mm,
        y=point.y_mm,
        uncertainty=point.uncertainty_mm,
        path="/point",
    )
    _require_decision_eligible(checked)
    projected, jacobian = _project(
        _as_array(checked.plane_to_pixel),
        point.x_mm,
        point.y_mm,
    )
    uncertainty = _projected_uncertainty_bound(
        _as_array(checked.plane_to_pixel),
        point.x_mm,
        point.y_mm,
        point.uncertainty_mm,
        jacobian,
    )
    return PixelPoint(
        x_px=_output_number(
            projected[0],
            maximum=MAX_ABS_PIXEL_COORDINATE,
            path="/x_px",
        ),
        y_px=_output_number(
            projected[1],
            maximum=MAX_ABS_PIXEL_COORDINATE,
            path="/y_px",
        ),
        uncertainty_px=_output_number(
            uncertainty,
            maximum=MAX_PIXEL_UNCERTAINTY_PX,
            path="/uncertainty_px",
        ),
    )


def measure_two_point_dimension(
    calibration: object,
    start: object,
    end: object,
) -> DimensionEstimate:
    """Measure a planar distance; return an interval only for eligible calibration."""

    checked = _checked_calibration(calibration)
    if type(start) is not PixelPoint:
        _fail(MetrologyErrorCode.INVALID_INPUT, "/start")
    if type(end) is not PixelPoint:
        _fail(MetrologyErrorCode.INVALID_INPUT, "/end")
    _require_domain_contains(
        checked.valid_pixel_domain,
        x=start.x_px,
        y=start.y_px,
        uncertainty=start.uncertainty_px,
        path="/start",
    )
    _require_domain_contains(
        checked.valid_pixel_domain,
        x=end.x_px,
        y=end.y_px,
        uncertainty=end.uncertainty_px,
        path="/end",
    )
    matrix = _as_array(checked.pixel_to_plane)
    start_plane, start_jacobian = _project(matrix, start.x_px, start.y_px)
    end_plane, end_jacobian = _project(matrix, end.x_px, end.y_px)
    value = float(np.linalg.norm(end_plane - start_plane))
    if not math.isfinite(value):
        _fail(MetrologyErrorCode.NUMERICAL_FAILURE)
    if value <= _MIN_GEOMETRY_SPAN:
        _fail(MetrologyErrorCode.DEGENERATE_GEOMETRY)
    point_error = _projected_uncertainty_bound(
        matrix,
        start.x_px,
        start.y_px,
        start.uncertainty_px,
        start_jacobian,
    ) + _projected_uncertainty_bound(
        matrix,
        end.x_px,
        end.y_px,
        end.uncertainty_px,
        end_jacobian,
    )
    if not checked.decision_eligible:
        return DimensionEstimate(
            value_mm=value,
            lower_bound_mm=None,
            upper_bound_mm=None,
            calibration_error_bound_mm=None,
            point_error_bound_mm=point_error,
            error_bound_mm=None,
            decision_eligible=False,
        )
    calibration_error = 0.0
    total_error = calibration_error + point_error
    upper = value + total_error
    for output, path in (
        (value, "/value_mm"),
        (point_error, "/point_error_bound_mm"),
        (calibration_error, "/calibration_error_bound_mm"),
        (total_error, "/error_bound_mm"),
        (upper, "/upper_bound_mm"),
    ):
        _output_number(output, maximum=MAX_ABS_PLANE_COORDINATE_MM, path=path)
    return DimensionEstimate(
        value_mm=value,
        lower_bound_mm=max(0.0, value - total_error),
        upper_bound_mm=upper,
        calibration_error_bound_mm=calibration_error,
        point_error_bound_mm=point_error,
        error_bound_mm=total_error,
        decision_eligible=True,
    )


def _bounded_views(value: object) -> tuple[ViewDimension, ...]:
    if type(value) not in {tuple, list}:
        _fail(MetrologyErrorCode.INVALID_INPUT, "/measurements")
    try:
        count = len(value)
    except Exception:
        _fail(MetrologyErrorCode.INVALID_INPUT, "/measurements")
    if count > MAX_MULTIVIEW_MEASUREMENTS:
        _fail(MetrologyErrorCode.BUDGET_EXCEEDED, "/measurements")
    try:
        measurements = tuple(value)
    except Exception:
        _fail(MetrologyErrorCode.INVALID_INPUT, "/measurements")
    if len(measurements) != count or any(type(item) is not ViewDimension for item in measurements):
        _fail(MetrologyErrorCode.INVALID_INPUT, "/measurements")
    identifiers = tuple(item.view_id for item in measurements)
    if len(set(identifiers)) != len(identifiers):
        _fail(MetrologyErrorCode.INVALID_INPUT, "/measurements")
    return measurements


def reconcile_multiview_dimensions(measurements: object) -> MultiViewDecision:
    """Decide eligible interval overlap; advisory estimates remain unknown."""

    checked = _bounded_views(measurements)
    available = tuple(
        item for item in checked if item.estimate is not None and item.estimate.decision_eligible
    )
    unknown = tuple(
        item.view_id
        for item in checked
        if item.estimate is None or not item.estimate.decision_eligible
    )
    contributing = tuple(item.view_id for item in available)
    if len(available) < 2:
        return MultiViewDecision(
            status=MultiViewStatus.UNKNOWN,
            intersection_lower_mm=None,
            intersection_upper_mm=None,
            conflict_gap_mm=None,
            contributing_views=contributing,
            unknown_views=unknown,
        )
    lower = max(
        float(item.estimate.lower_bound_mm)
        for item in available
        if item.estimate is not None and item.estimate.lower_bound_mm is not None
    )
    upper = min(
        float(item.estimate.upper_bound_mm)
        for item in available
        if item.estimate is not None and item.estimate.upper_bound_mm is not None
    )
    if lower <= upper:
        return MultiViewDecision(
            status=MultiViewStatus.CONSISTENT,
            intersection_lower_mm=lower,
            intersection_upper_mm=upper,
            conflict_gap_mm=None,
            contributing_views=contributing,
            unknown_views=unknown,
        )
    return MultiViewDecision(
        status=MultiViewStatus.CONFLICT,
        intersection_lower_mm=None,
        intersection_upper_mm=None,
        conflict_gap_mm=lower - upper,
        contributing_views=contributing,
        unknown_views=unknown,
    )
