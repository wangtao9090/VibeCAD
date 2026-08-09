"""Private, authority-free construction of sealed planar calibration receipts.

This module closes the forged-homography input path for the first A11 slice:
callers supply exact sealed image/provider bindings plus already-confirmed
landmarks in one declared metric frame.  VibeCAD derives all pixel locations,
fits the homography itself, and binds the complete result with deterministic
digests.

The receipt is deliberately *not* durable or authenticated.  It proves only
in-process integrity, never that the confirmation references are genuine, and
must not be consumed by Task, Revision, HEAD, MCP, or adoption code.  A future
durable/HMAC store must authenticate the confirmation boundary before this
record can participate in any write-authority decision.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum

from vibecad.visual.contracts import MAX_IMAGE_SET_ITEMS, MAX_NORMALIZED_LONG_EDGE, ImageSet
from vibecad.visual.metrology import (
    MAX_ABS_PLANE_COORDINATE_MM,
    MAX_CALIBRATION_LANDMARKS,
    MAX_PLANE_UNCERTAINTY_MM,
    MetrologyError,
    PixelPoint,
    PlanarCalibration,
    PlanarLandmark,
    PlanePoint,
    calibrate_planar_homography,
)
from vibecad.visual.overlay_render import OverlayRenderError, _decode_source
from vibecad.visual.provider_images import (
    MAX_PROVIDER_LONG_EDGE,
    ProviderImageBatch,
    ProviderImagePart,
    ProviderImagePartKind,
)

CALIBRATION_AUTHORITY_SCHEMA_VERSION = 1
MIN_CALIBRATION_LANDMARKS = 4
MAX_CALIBRATION_RECEIPT_BYTES = 128 * 1024
PLANAR_CALIBRATION_ALGORITHM = "normalized-dlt-planar-homography-v1"
IN_MEMORY_AUTHORITY_LEVEL = "in_memory_integrity_only"

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_SET_ID = re.compile(r"^image_set_[0-9a-f]{32}$")
_VISUAL_INPUT_ID = re.compile(r"^visual_input_[0-9a-f]{32}$")
_PROVIDER_IMAGE_ID = re.compile(r"^provider_image_[0-9a-f]{32}$")

_LANDMARK_RECORD_DOMAIN = b"vibecad-calibration-landmarks-v1\0"
_FRAME_RECORD_DOMAIN = b"vibecad-calibration-frame-v1\0"
_CALIBRATION_RECORD_DOMAIN = b"vibecad-calibration-result-v1\0"
_AUTHORITY_BINDING_DOMAIN = b"vibecad-calibration-authority-binding-v1\0"
_RECEIPT_DOMAIN = b"vibecad-in-memory-calibration-receipt-v1\0"


class CalibrationAuthorityErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    BINDING_MISMATCH = "binding_mismatch"
    INTEGRITY_FAILURE = "integrity_failure"
    CALIBRATION_FAILED = "calibration_failed"


class CalibrationAuthorityError(ValueError):
    """Bounded failure that never reflects rejected confirmation contents."""

    def __init__(self, code: CalibrationAuthorityErrorCode, path: str = "") -> None:
        if type(code) is not CalibrationAuthorityErrorCode:
            raise TypeError("code must be an exact CalibrationAuthorityErrorCode")
        if type(path) is not str:
            raise TypeError("path must be a string")
        try:
            encoded = path.encode("utf-8")
        except UnicodeError:
            raise ValueError("path must be bounded") from None
        if len(encoded) > 256:
            raise ValueError("path must be bounded")
        self.code = code
        self.path = path
        super().__init__(code.value)


def _fail(code: CalibrationAuthorityErrorCode, path: str = "") -> None:
    raise CalibrationAuthorityError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, path)
    return value


def _bound_identifier(value: object, pattern: re.Pattern[str], path: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    return _bound_identifier(value, _DIGEST, path)


def _finite_number(
    value: object,
    *,
    minimum: float,
    maximum: float,
    path: str,
) -> float:
    if type(value) not in {int, float}:
        _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, path)
    try:
        converted = float(value)
    except OverflowError:
        _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, path)
    if not math.isfinite(converted) or not minimum <= converted <= maximum:
        _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, path)
    return 0.0 if converted == 0.0 else converted


def _canonical_json(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail(CalibrationAuthorityErrorCode.INVALID_INPUT)
    if len(raw) > MAX_CALIBRATION_RECEIPT_BYTES:
        _fail(CalibrationAuthorityErrorCode.BUDGET_EXCEEDED)
    return raw


def _hash(domain: bytes, value: object) -> str:
    return hashlib.sha256(domain + _canonical_json(value)).hexdigest()


def _pixel_mapping(value: PixelPoint) -> dict[str, float]:
    return {
        "x_px": value.x_px,
        "y_px": value.y_px,
        "uncertainty_px": value.uncertainty_px,
    }


def _plane_mapping(value: PlanePoint) -> dict[str, float]:
    return {
        "x_mm": value.x_mm,
        "y_mm": value.y_mm,
        "uncertainty_mm": value.uncertainty_mm,
    }


def _calibration_mapping(value: PlanarCalibration) -> dict[str, object]:
    return {
        "pixel_to_plane": value.pixel_to_plane,
        "plane_to_pixel": value.plane_to_pixel,
        "valid_pixel_domain": value.valid_pixel_domain,
        "valid_plane_domain": value.valid_plane_domain,
        "landmark_count": value.landmark_count,
        "rms_error_mm": value.rms_error_mm,
        "max_error_mm": value.max_error_mm,
        "fit_error_indicator_mm": value.fit_error_indicator_mm,
        "condition_number": value.condition_number,
        "decision_eligible": value.decision_eligible,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfirmedPlanarLandmark:
    """One upstream-confirmed correspondence, expressed without a matrix."""

    landmark_id: str
    confirmation_id: str
    normalized_x: int | float
    normalized_y: int | float
    localization_uncertainty_norm: int | float
    x_mm: int | float
    y_mm: int | float
    plane_uncertainty_mm: int | float = 0.0
    schema_version: int = CALIBRATION_AUTHORITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != CALIBRATION_AUTHORITY_SCHEMA_VERSION
        ):
            _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/schema_version")
        object.__setattr__(self, "landmark_id", _identifier(self.landmark_id, "/landmark_id"))
        object.__setattr__(
            self,
            "confirmation_id",
            _identifier(self.confirmation_id, "/confirmation_id"),
        )
        for name in ("normalized_x", "normalized_y", "localization_uncertainty_norm"):
            object.__setattr__(
                self,
                name,
                _finite_number(
                    getattr(self, name),
                    minimum=0.0,
                    maximum=1.0,
                    path=f"/{name}",
                ),
            )
        for name in ("x_mm", "y_mm"):
            object.__setattr__(
                self,
                name,
                _finite_number(
                    getattr(self, name),
                    minimum=-MAX_ABS_PLANE_COORDINATE_MM,
                    maximum=MAX_ABS_PLANE_COORDINATE_MM,
                    path=f"/{name}",
                ),
            )
        object.__setattr__(
            self,
            "plane_uncertainty_mm",
            _finite_number(
                self.plane_uncertainty_mm,
                minimum=0.0,
                maximum=MAX_PLANE_UNCERTAINTY_MM,
                path="/plane_uncertainty_mm",
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfirmedPlanarMetricBasis:
    """Confirmed origin and orientation for millimetre plane coordinates."""

    frame_id: str
    confirmation_id: str
    origin_landmark_id: str
    positive_x_landmark_id: str
    positive_y_landmark_id: str
    unit: str = "mm"
    schema_version: int = CALIBRATION_AUTHORITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != CALIBRATION_AUTHORITY_SCHEMA_VERSION
        ):
            _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/schema_version")
        for name in (
            "frame_id",
            "confirmation_id",
            "origin_landmark_id",
            "positive_x_landmark_id",
            "positive_y_landmark_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        if (
            len(
                {
                    self.origin_landmark_id,
                    self.positive_x_landmark_id,
                    self.positive_y_landmark_id,
                }
            )
            != 3
        ):
            _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/metric_basis")
        if type(self.unit) is not str or self.unit != "mm":
            _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/unit")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "frame_id": self.frame_id,
            "confirmation_id": self.confirmation_id,
            "origin_landmark_id": self.origin_landmark_id,
            "positive_x_landmark_id": self.positive_x_landmark_id,
            "positive_y_landmark_id": self.positive_y_landmark_id,
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SealedCalibrationLandmark:
    """Complete derived record for one confirmed landmark."""

    landmark_id: str
    confirmation_id: str
    normalized_x: float
    normalized_y: float
    localization_uncertainty_norm: float
    normalized_pixel: PixelPoint
    provider_pixel: PixelPoint
    plane: PlanePoint

    def __post_init__(self) -> None:
        object.__setattr__(self, "landmark_id", _identifier(self.landmark_id, "/landmark_id"))
        object.__setattr__(
            self,
            "confirmation_id",
            _identifier(self.confirmation_id, "/confirmation_id"),
        )
        for name in ("normalized_x", "normalized_y", "localization_uncertainty_norm"):
            object.__setattr__(
                self,
                name,
                _finite_number(
                    getattr(self, name),
                    minimum=0.0,
                    maximum=1.0,
                    path=f"/{name}",
                ),
            )
        if type(self.normalized_pixel) is not PixelPoint:
            _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/normalized_pixel")
        if type(self.provider_pixel) is not PixelPoint:
            _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/provider_pixel")
        if type(self.plane) is not PlanePoint:
            _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/plane")

    def to_mapping(self) -> dict[str, object]:
        return {
            "landmark_id": self.landmark_id,
            "confirmation_id": self.confirmation_id,
            "normalized_x": self.normalized_x,
            "normalized_y": self.normalized_y,
            "localization_uncertainty_norm": self.localization_uncertainty_norm,
            "normalized_pixel": _pixel_mapping(self.normalized_pixel),
            "provider_pixel": _pixel_mapping(self.provider_pixel),
            "plane": _plane_mapping(self.plane),
        }


def _basis_landmarks(
    basis: ConfirmedPlanarMetricBasis,
    landmarks: tuple[SealedCalibrationLandmark, ...],
) -> None:
    by_id = {item.landmark_id: item for item in landmarks}
    try:
        origin = by_id[basis.origin_landmark_id].plane
        positive_x = by_id[basis.positive_x_landmark_id].plane
        positive_y = by_id[basis.positive_y_landmark_id].plane
    except KeyError:
        _fail(CalibrationAuthorityErrorCode.BINDING_MISMATCH, "/metric_basis")
    if origin.x_mm != 0.0 or origin.y_mm != 0.0:
        _fail(CalibrationAuthorityErrorCode.BINDING_MISMATCH, "/metric_basis/origin")
    if positive_x.x_mm <= 0.0 or positive_x.y_mm != 0.0:
        _fail(CalibrationAuthorityErrorCode.BINDING_MISMATCH, "/metric_basis/positive_x")
    if positive_y.x_mm != 0.0 or positive_y.y_mm <= 0.0:
        _fail(CalibrationAuthorityErrorCode.BINDING_MISMATCH, "/metric_basis/positive_y")


def _derived_pixel(
    *,
    normalized_x: float,
    normalized_y: float,
    localization_uncertainty_norm: float,
    width: int,
    height: int,
) -> PixelPoint:
    if type(width) is not int or type(height) is not int or min(width, height) <= 1:
        _fail(CalibrationAuthorityErrorCode.BINDING_MISMATCH, "/dimensions")
    x_span = width - 1
    y_span = height - 1
    return PixelPoint(
        x_px=normalized_x * x_span,
        y_px=normalized_y * y_span,
        uncertainty_px=localization_uncertainty_norm * max(x_span, y_span),
    )


def _calibrate(landmarks: tuple[SealedCalibrationLandmark, ...]) -> PlanarCalibration:
    try:
        return calibrate_planar_homography(
            tuple(
                PlanarLandmark(pixel=item.normalized_pixel, plane=item.plane) for item in landmarks
            )
        )
    except MetrologyError:
        _fail(CalibrationAuthorityErrorCode.CALIBRATION_FAILED, "/landmarks")


@dataclass(frozen=True, slots=True, kw_only=True)
class InMemoryPlanarCalibrationReceipt:
    """Integrity-only receipt; never a durable or Task-adoption authority."""

    source_index: int
    image_set_id: str
    image_set_manifest_sha256: str
    normalized_visual_input_id: str
    normalized_sha256: str
    normalized_width: int
    normalized_height: int
    provider_batch_manifest_sha256: str
    provider_image_id: str
    provider_image_sha256: str
    provider_width: int
    provider_height: int
    landmarks: tuple[SealedCalibrationLandmark, ...]
    metric_basis: ConfirmedPlanarMetricBasis
    calibration: PlanarCalibration
    landmark_record_sha256: str
    frame_record_sha256: str
    calibration_sha256: str
    authority_binding_sha256: str
    receipt_sha256: str = ""
    schema_version: int = CALIBRATION_AUTHORITY_SCHEMA_VERSION
    algorithm_id: str = field(default=PLANAR_CALIBRATION_ALGORITHM, init=False)
    authority_level: str = field(default=IN_MEMORY_AUTHORITY_LEVEL, init=False)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != CALIBRATION_AUTHORITY_SCHEMA_VERSION
        ):
            _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/schema_version")
        if type(self.source_index) is not int or not 0 <= self.source_index < MAX_IMAGE_SET_ITEMS:
            _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/source_index")
        _bound_identifier(self.image_set_id, _IMAGE_SET_ID, "/image_set_id")
        _digest(self.image_set_manifest_sha256, "/image_set_manifest_sha256")
        _bound_identifier(
            self.normalized_visual_input_id,
            _VISUAL_INPUT_ID,
            "/normalized_visual_input_id",
        )
        _digest(self.normalized_sha256, "/normalized_sha256")
        _digest(self.provider_batch_manifest_sha256, "/provider_batch_manifest_sha256")
        _bound_identifier(self.provider_image_id, _PROVIDER_IMAGE_ID, "/provider_image_id")
        _digest(self.provider_image_sha256, "/provider_image_sha256")
        for name, maximum in (
            ("normalized_width", MAX_NORMALIZED_LONG_EDGE),
            ("normalized_height", MAX_NORMALIZED_LONG_EDGE),
            ("provider_width", MAX_PROVIDER_LONG_EDGE),
            ("provider_height", MAX_PROVIDER_LONG_EDGE),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 < value <= maximum:
                _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, f"/{name}")
        if (
            type(self.landmarks) is not tuple
            or not MIN_CALIBRATION_LANDMARKS <= len(self.landmarks) <= MAX_CALIBRATION_LANDMARKS
            or any(type(item) is not SealedCalibrationLandmark for item in self.landmarks)
        ):
            _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/landmarks")
        if len({item.landmark_id for item in self.landmarks}) != len(self.landmarks):
            _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/landmarks")
        if tuple(sorted(self.landmarks, key=lambda item: item.landmark_id)) != self.landmarks:
            _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/landmarks")
        if type(self.metric_basis) is not ConfirmedPlanarMetricBasis:
            _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/metric_basis")
        if type(self.calibration) is not PlanarCalibration:
            _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/calibration")

        for index, item in enumerate(self.landmarks):
            expected_normalized = _derived_pixel(
                normalized_x=item.normalized_x,
                normalized_y=item.normalized_y,
                localization_uncertainty_norm=item.localization_uncertainty_norm,
                width=self.normalized_width,
                height=self.normalized_height,
            )
            expected_provider = _derived_pixel(
                normalized_x=item.normalized_x,
                normalized_y=item.normalized_y,
                localization_uncertainty_norm=item.localization_uncertainty_norm,
                width=self.provider_width,
                height=self.provider_height,
            )
            if (
                item.normalized_pixel != expected_normalized
                or item.provider_pixel != expected_provider
            ):
                _fail(
                    CalibrationAuthorityErrorCode.INTEGRITY_FAILURE,
                    f"/landmarks/{index}/pixel",
                )

        _basis_landmarks(self.metric_basis, self.landmarks)
        expected_calibration = _calibrate(self.landmarks)
        if _canonical_json(_calibration_mapping(self.calibration)) != _canonical_json(
            _calibration_mapping(expected_calibration)
        ):
            _fail(CalibrationAuthorityErrorCode.INTEGRITY_FAILURE, "/calibration")

        expected_landmark_digest = _hash(
            _LANDMARK_RECORD_DOMAIN,
            [item.to_mapping() for item in self.landmarks],
        )
        expected_frame_digest = _hash(_FRAME_RECORD_DOMAIN, self.metric_basis.to_mapping())
        expected_calibration_digest = _hash(
            _CALIBRATION_RECORD_DOMAIN,
            _calibration_mapping(self.calibration),
        )
        binding = self._binding_mapping(
            landmark_record_sha256=expected_landmark_digest,
            frame_record_sha256=expected_frame_digest,
            calibration_sha256=expected_calibration_digest,
        )
        expected_authority_digest = _hash(_AUTHORITY_BINDING_DOMAIN, binding)
        for name, expected in (
            ("landmark_record_sha256", expected_landmark_digest),
            ("frame_record_sha256", expected_frame_digest),
            ("calibration_sha256", expected_calibration_digest),
            ("authority_binding_sha256", expected_authority_digest),
        ):
            actual = _digest(getattr(self, name), f"/{name}")
            if not hmac.compare_digest(actual, expected):
                _fail(CalibrationAuthorityErrorCode.INTEGRITY_FAILURE, f"/{name}")

        expected_receipt = _hash(_RECEIPT_DOMAIN, self._body_mapping())
        if self.receipt_sha256 and not hmac.compare_digest(self.receipt_sha256, expected_receipt):
            _fail(CalibrationAuthorityErrorCode.INTEGRITY_FAILURE, "/receipt_sha256")
        object.__setattr__(self, "receipt_sha256", expected_receipt)
        _canonical_json(self.to_mapping())

    @property
    def task_adoption_eligible(self) -> bool:
        """This slice cannot grant Task adoption authority."""

        return False

    def _binding_mapping(
        self,
        *,
        landmark_record_sha256: str,
        frame_record_sha256: str,
        calibration_sha256: str,
    ) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "algorithm_id": self.algorithm_id,
            "image_set_id": self.image_set_id,
            "image_set_manifest_sha256": self.image_set_manifest_sha256,
            "source_index": self.source_index,
            "normalized_visual_input_id": self.normalized_visual_input_id,
            "normalized_sha256": self.normalized_sha256,
            "normalized_width": self.normalized_width,
            "normalized_height": self.normalized_height,
            "provider_batch_manifest_sha256": self.provider_batch_manifest_sha256,
            "provider_image_id": self.provider_image_id,
            "provider_image_sha256": self.provider_image_sha256,
            "provider_width": self.provider_width,
            "provider_height": self.provider_height,
            "landmark_record_sha256": landmark_record_sha256,
            "frame_record_sha256": frame_record_sha256,
            "calibration_sha256": calibration_sha256,
        }

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority_level": self.authority_level,
            "binding": self._binding_mapping(
                landmark_record_sha256=self.landmark_record_sha256,
                frame_record_sha256=self.frame_record_sha256,
                calibration_sha256=self.calibration_sha256,
            ),
            "landmarks": [item.to_mapping() for item in self.landmarks],
            "metric_basis": self.metric_basis.to_mapping(),
            "calibration": _calibration_mapping(self.calibration),
            "authority_binding_sha256": self.authority_binding_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return self._body_mapping() | {"receipt_sha256": self.receipt_sha256}


def _aspect_preserved(part: ProviderImagePart, *, source_width: int, source_height: int) -> bool:
    cross_error = abs(part.width * source_height - part.height * source_width)
    return cross_error <= max(source_width, source_height)


def _validate_overview_png(part: ProviderImagePart) -> None:
    try:
        decoded = _decode_source(part.data, width=part.width, height=part.height)
    except OverlayRenderError:
        _fail(CalibrationAuthorityErrorCode.INTEGRITY_FAILURE, "/provider_overview/data")
    decoded.close()


def build_in_memory_planar_calibration_receipt(
    *,
    image_set: object,
    image_batch: object,
    source_index: object,
    landmarks: object,
    metric_basis: object,
) -> InMemoryPlanarCalibrationReceipt:
    """Fit and seal an integrity-only receipt without accepting derived authority."""

    if type(image_set) is not ImageSet:
        _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/image_set")
    if type(image_batch) is not ProviderImageBatch:
        _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/image_batch")
    if type(source_index) is not int or not 0 <= source_index < len(image_set.inputs):
        _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/source_index")
    if type(landmarks) is not tuple:
        _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/landmarks")
    if len(landmarks) > MAX_CALIBRATION_LANDMARKS:
        _fail(CalibrationAuthorityErrorCode.BUDGET_EXCEEDED, "/landmarks")
    if len(landmarks) < MIN_CALIBRATION_LANDMARKS or any(
        type(item) is not ConfirmedPlanarLandmark for item in landmarks
    ):
        _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/landmarks")
    if len({item.landmark_id for item in landmarks}) != len(landmarks):
        _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/landmarks")
    if type(metric_basis) is not ConfirmedPlanarMetricBasis:
        _fail(CalibrationAuthorityErrorCode.INVALID_INPUT, "/metric_basis")
    if image_batch.image_set_id != image_set.id or not hmac.compare_digest(
        image_batch.image_set_manifest_sha256,
        image_set.manifest_sha256,
    ):
        _fail(CalibrationAuthorityErrorCode.BINDING_MISMATCH, "/image_batch")

    overviews = tuple(
        part
        for part in image_batch.parts
        if part.source_index == source_index
        and part.kind is ProviderImagePartKind.OVERVIEW
        and part.label is None
    )
    if len(overviews) != 1:
        _fail(CalibrationAuthorityErrorCode.BINDING_MISMATCH, "/provider_overview")
    overview = overviews[0]
    _validate_overview_png(overview)
    source = image_set.inputs[source_index]
    if (
        overview.source_sha256 != source.original.sha256
        or overview.view_role is not source.view_role
        or not _aspect_preserved(
            overview,
            source_width=source.normalized.width,
            source_height=source.normalized.height,
        )
    ):
        _fail(CalibrationAuthorityErrorCode.BINDING_MISMATCH, "/provider_overview")

    records = tuple(
        sorted(
            (
                SealedCalibrationLandmark(
                    landmark_id=item.landmark_id,
                    confirmation_id=item.confirmation_id,
                    normalized_x=item.normalized_x,
                    normalized_y=item.normalized_y,
                    localization_uncertainty_norm=item.localization_uncertainty_norm,
                    normalized_pixel=_derived_pixel(
                        normalized_x=item.normalized_x,
                        normalized_y=item.normalized_y,
                        localization_uncertainty_norm=item.localization_uncertainty_norm,
                        width=source.normalized.width,
                        height=source.normalized.height,
                    ),
                    provider_pixel=_derived_pixel(
                        normalized_x=item.normalized_x,
                        normalized_y=item.normalized_y,
                        localization_uncertainty_norm=item.localization_uncertainty_norm,
                        width=overview.width,
                        height=overview.height,
                    ),
                    plane=PlanePoint(
                        x_mm=item.x_mm,
                        y_mm=item.y_mm,
                        uncertainty_mm=item.plane_uncertainty_mm,
                    ),
                )
                for item in landmarks
            ),
            key=lambda item: item.landmark_id,
        )
    )
    _basis_landmarks(metric_basis, records)
    calibration = _calibrate(records)
    landmark_digest = _hash(
        _LANDMARK_RECORD_DOMAIN,
        [item.to_mapping() for item in records],
    )
    frame_digest = _hash(_FRAME_RECORD_DOMAIN, metric_basis.to_mapping())
    calibration_digest = _hash(
        _CALIBRATION_RECORD_DOMAIN,
        _calibration_mapping(calibration),
    )
    binding = {
        "schema_version": CALIBRATION_AUTHORITY_SCHEMA_VERSION,
        "algorithm_id": PLANAR_CALIBRATION_ALGORITHM,
        "image_set_id": image_set.id,
        "image_set_manifest_sha256": image_set.manifest_sha256,
        "source_index": source_index,
        "normalized_visual_input_id": source.normalized.id,
        "normalized_sha256": source.normalized.sha256,
        "normalized_width": source.normalized.width,
        "normalized_height": source.normalized.height,
        "provider_batch_manifest_sha256": image_batch.manifest_sha256,
        "provider_image_id": overview.id,
        "provider_image_sha256": overview.sha256,
        "provider_width": overview.width,
        "provider_height": overview.height,
        "landmark_record_sha256": landmark_digest,
        "frame_record_sha256": frame_digest,
        "calibration_sha256": calibration_digest,
    }
    return InMemoryPlanarCalibrationReceipt(
        source_index=source_index,
        image_set_id=image_set.id,
        image_set_manifest_sha256=image_set.manifest_sha256,
        normalized_visual_input_id=source.normalized.id,
        normalized_sha256=source.normalized.sha256,
        normalized_width=source.normalized.width,
        normalized_height=source.normalized.height,
        provider_batch_manifest_sha256=image_batch.manifest_sha256,
        provider_image_id=overview.id,
        provider_image_sha256=overview.sha256,
        provider_width=overview.width,
        provider_height=overview.height,
        landmarks=records,
        metric_basis=metric_basis,
        calibration=calibration,
        landmark_record_sha256=landmark_digest,
        frame_record_sha256=frame_digest,
        calibration_sha256=calibration_digest,
        authority_binding_sha256=_hash(_AUTHORITY_BINDING_DOMAIN, binding),
    )


__all__ = (
    "CALIBRATION_AUTHORITY_SCHEMA_VERSION",
    "IN_MEMORY_AUTHORITY_LEVEL",
    "MAX_CALIBRATION_RECEIPT_BYTES",
    "MIN_CALIBRATION_LANDMARKS",
    "PLANAR_CALIBRATION_ALGORITHM",
    "CalibrationAuthorityError",
    "CalibrationAuthorityErrorCode",
    "ConfirmedPlanarLandmark",
    "ConfirmedPlanarMetricBasis",
    "InMemoryPlanarCalibrationReceipt",
    "SealedCalibrationLandmark",
    "build_in_memory_planar_calibration_receipt",
)
