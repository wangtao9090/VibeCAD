"""Authority-free composition from bound visual points to planar fits.

The caller explicitly supplies one calibration per source view and one fit
policy per local feature.  This module never infers a primitive family,
calibration, tolerance, or cross-view correspondence.  Mapping and fitting
failures become bounded UNKNOWN records; no result can approve or adopt CAD.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum

from vibecad.visual.contracts import MAX_IMAGE_SET_ITEMS
from vibecad.visual.evidence import (
    MAX_EVIDENCE_CLAIMS_PER_FEATURE,
    MAX_EVIDENCE_FEATURES,
    MAX_EVIDENCE_POINTS_PER_FEATURE,
    BoundFeatureEvidence,
    BoundVisualEvidence,
)
from vibecad.visual.geometry_fit import (
    MAX_FIT_TOLERANCE_MM,
    GeometryFitError,
    GeometryFitRequest,
    GeometryFitResult,
    GeometryFitStatus,
    PrimitiveFamily,
    fit_declared_geometry,
)
from vibecad.visual.metrology import (
    MetrologyError,
    MetrologyErrorCode,
    PlanarCalibration,
    PlanePoint,
    map_pixel_to_plane,
)

VISUAL_FIT_PIPELINE_SCHEMA_VERSION = 1
MAX_PIPELINE_CALIBRATIONS = MAX_IMAGE_SET_ITEMS
MAX_PIPELINE_POLICIES = MAX_EVIDENCE_FEATURES

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CLAIM_ID = re.compile(r"^visual_claim_[0-9a-f]{32}$")
_PROVIDER_IMAGE_ID = re.compile(r"^provider_image_[0-9a-f]{32}$")
_RECONSTRUCTION_ID = re.compile(r"^reconstruction_[0-9a-f]{32}$")
_IMAGE_SET_ID = re.compile(r"^image_set_[0-9a-f]{32}$")
_OBSERVATION_ID = re.compile(r"^visual_observation_[0-9a-f]{32}$")
_MAX_SAFE_INTEGER = 2**53 - 1
_CALIBRATION_DIGEST_DOMAIN = b"vibecad-visual-planar-calibration-v1\0"


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail(VisualFitPipelineErrorCode.INVALID_INPUT)


class VisualFitPipelineErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    DUPLICATE_ID = "duplicate_id"
    UNKNOWN_REFERENCE = "unknown_reference"
    BINDING_MISMATCH = "binding_mismatch"


class VisualFitPipelineError(ValueError):
    def __init__(self, code: VisualFitPipelineErrorCode, path: str = "") -> None:
        if type(code) is not VisualFitPipelineErrorCode:
            raise TypeError("code must be an exact VisualFitPipelineErrorCode")
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


def _fail(code: VisualFitPipelineErrorCode, path: str = "") -> None:
    raise VisualFitPipelineError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        _fail(VisualFitPipelineErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class SourcePlanarCalibration:
    source_index: int
    image_set_manifest_sha256: str
    provider_image_id: str
    frame_id: str
    calibration: PlanarCalibration
    calibration_sha256: str = ""

    def __post_init__(self) -> None:
        if type(self.source_index) is not int or not 0 <= self.source_index < MAX_IMAGE_SET_ITEMS:
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/source_index")
        if (
            type(self.image_set_manifest_sha256) is not str
            or _DIGEST.fullmatch(self.image_set_manifest_sha256) is None
        ):
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/image_set_manifest_sha256")
        if (
            type(self.provider_image_id) is not str
            or _PROVIDER_IMAGE_ID.fullmatch(self.provider_image_id) is None
        ):
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/provider_image_id")
        object.__setattr__(self, "frame_id", _identifier(self.frame_id, "/frame_id"))
        if type(self.calibration) is not PlanarCalibration:
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/calibration")
        expected = hashlib.sha256(
            _CALIBRATION_DIGEST_DOMAIN
            + _canonical_json(
                {
                    "pixel_to_plane": self.calibration.pixel_to_plane,
                    "plane_to_pixel": self.calibration.plane_to_pixel,
                    "valid_pixel_domain": self.calibration.valid_pixel_domain,
                    "valid_plane_domain": self.calibration.valid_plane_domain,
                    "landmark_count": self.calibration.landmark_count,
                    "rms_error_mm": self.calibration.rms_error_mm,
                    "max_error_mm": self.calibration.max_error_mm,
                    "fit_error_indicator_mm": self.calibration.fit_error_indicator_mm,
                    "condition_number": self.calibration.condition_number,
                    "decision_eligible": self.calibration.decision_eligible,
                }
            )
        ).hexdigest()
        if self.calibration_sha256 and self.calibration_sha256 != expected:
            _fail(VisualFitPipelineErrorCode.BINDING_MISMATCH, "/calibration_sha256")
        object.__setattr__(self, "calibration_sha256", expected)


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureFitPolicy:
    source_index: int
    local_feature_id: str
    residual_tolerance_mm: int | float

    def __post_init__(self) -> None:
        if type(self.source_index) is not int or not 0 <= self.source_index < MAX_IMAGE_SET_ITEMS:
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/source_index")
        object.__setattr__(
            self,
            "local_feature_id",
            _identifier(self.local_feature_id, "/local_feature_id"),
        )
        if type(self.residual_tolerance_mm) not in {int, float}:
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/residual_tolerance_mm")
        tolerance = float(self.residual_tolerance_mm)
        if not math.isfinite(tolerance) or not 0.0 <= tolerance <= MAX_FIT_TOLERANCE_MM:
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/residual_tolerance_mm")
        object.__setattr__(self, "residual_tolerance_mm", tolerance)


class EvidenceFeatureFitStatus(StrEnum):
    FITTED = "fitted"
    UNKNOWN = "unknown"


class EvidenceFeatureFitUnknownReason(StrEnum):
    NOT_REQUESTED = "not_requested"
    MISSING_CALIBRATION = "missing_calibration"
    OUTSIDE_CALIBRATION_DOMAIN = "outside_calibration_domain"
    CALIBRATION_NOT_DECISION_ELIGIBLE = "calibration_not_decision_eligible"
    MAPPING_FAILURE = "mapping_failure"
    FIT_REJECTED = "fit_rejected"
    FIT_FAILURE = "fit_failure"


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceFeatureFit:
    source_index: int
    provider_image_id: str
    local_feature_id: str
    family: PrimitiveFamily
    claim_ids: tuple[str, ...]
    frame_id: str | None
    calibration_sha256: str | None
    status: EvidenceFeatureFitStatus
    plane_points: tuple[PlanePoint, ...]
    fit_result: GeometryFitResult | None
    unknown_reason: EvidenceFeatureFitUnknownReason | None

    def __post_init__(self) -> None:
        if type(self.source_index) is not int or not 0 <= self.source_index < MAX_IMAGE_SET_ITEMS:
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/source_index")
        if (
            type(self.provider_image_id) is not str
            or _PROVIDER_IMAGE_ID.fullmatch(self.provider_image_id) is None
        ):
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/provider_image_id")
        object.__setattr__(
            self,
            "local_feature_id",
            _identifier(self.local_feature_id, "/local_feature_id"),
        )
        if type(self.family) is not PrimitiveFamily:
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/family")
        if (
            type(self.claim_ids) is not tuple
            or not self.claim_ids
            or len(self.claim_ids) > MAX_EVIDENCE_CLAIMS_PER_FEATURE
            or len(set(self.claim_ids)) != len(self.claim_ids)
            or any(
                type(item) is not str or _CLAIM_ID.fullmatch(item) is None
                for item in self.claim_ids
            )
        ):
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/claim_ids")
        object.__setattr__(self, "claim_ids", tuple(sorted(self.claim_ids)))
        if self.frame_id is not None:
            object.__setattr__(self, "frame_id", _identifier(self.frame_id, "/frame_id"))
        if self.calibration_sha256 is not None and (
            type(self.calibration_sha256) is not str
            or _DIGEST.fullmatch(self.calibration_sha256) is None
        ):
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/calibration_sha256")
        if type(self.status) is not EvidenceFeatureFitStatus:
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/status")
        if (
            type(self.plane_points) is not tuple
            or len(self.plane_points) > MAX_EVIDENCE_POINTS_PER_FEATURE
            or any(type(item) is not PlanePoint for item in self.plane_points)
        ):
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/plane_points")
        if self.status is EvidenceFeatureFitStatus.FITTED:
            if (
                self.frame_id is None
                or self.calibration_sha256 is None
                or not self.plane_points
                or type(self.fit_result) is not GeometryFitResult
                or self.fit_result.status is not GeometryFitStatus.FITTED
                or self.fit_result.family is not self.family
                or self.fit_result.point_count != len(self.plane_points)
                or self.unknown_reason is not None
            ):
                _fail(VisualFitPipelineErrorCode.INVALID_INPUT)
            return
        if type(self.unknown_reason) is not EvidenceFeatureFitUnknownReason:
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/unknown_reason")
        if self.unknown_reason is EvidenceFeatureFitUnknownReason.FIT_REJECTED:
            if (
                self.frame_id is None
                or self.calibration_sha256 is None
                or not self.plane_points
                or type(self.fit_result) is not GeometryFitResult
                or self.fit_result.status is not GeometryFitStatus.UNKNOWN
                or self.fit_result.family is not self.family
                or self.fit_result.point_count != len(self.plane_points)
            ):
                _fail(VisualFitPipelineErrorCode.INVALID_INPUT)
        elif self.unknown_reason is EvidenceFeatureFitUnknownReason.FIT_FAILURE:
            if (
                self.frame_id is None
                or self.calibration_sha256 is None
                or not self.plane_points
                or self.fit_result is not None
            ):
                _fail(VisualFitPipelineErrorCode.INVALID_INPUT)
        elif self.fit_result is not None or self.plane_points:
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT)
        if self.unknown_reason in {
            EvidenceFeatureFitUnknownReason.NOT_REQUESTED,
            EvidenceFeatureFitUnknownReason.MISSING_CALIBRATION,
        }:
            if self.frame_id is not None or self.calibration_sha256 is not None:
                _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/frame_id")
        elif self.frame_id is None or self.calibration_sha256 is None:
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/frame_id")


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualEvidenceFitReport:
    reconstruction_id: str
    generation: int
    image_set_id: str
    image_set_manifest_sha256: str
    image_batch_manifest_sha256: str
    observation_id: str
    observation_digest: str
    feature_fits: tuple[EvidenceFeatureFit, ...]
    schema_version: int = VISUAL_FIT_PIPELINE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != VISUAL_FIT_PIPELINE_SCHEMA_VERSION
        ):
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/schema_version")
        for name, pattern in (
            ("reconstruction_id", _RECONSTRUCTION_ID),
            ("image_set_id", _IMAGE_SET_ID),
            ("observation_id", _OBSERVATION_ID),
        ):
            value = getattr(self, name)
            if type(value) is not str or pattern.fullmatch(value) is None:
                _fail(VisualFitPipelineErrorCode.INVALID_INPUT, f"/{name}")
        if type(self.generation) is not int or not 0 < self.generation <= _MAX_SAFE_INTEGER:
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/generation")
        for name in (
            "image_set_manifest_sha256",
            "image_batch_manifest_sha256",
            "observation_digest",
        ):
            value = getattr(self, name)
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                _fail(VisualFitPipelineErrorCode.INVALID_INPUT, f"/{name}")
        if (
            type(self.feature_fits) is not tuple
            or len(self.feature_fits) > MAX_EVIDENCE_FEATURES
            or any(type(item) is not EvidenceFeatureFit for item in self.feature_fits)
        ):
            _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/feature_fits")
        keys = tuple((item.source_index, item.local_feature_id) for item in self.feature_fits)
        if len(keys) != len(set(keys)):
            _fail(VisualFitPipelineErrorCode.DUPLICATE_ID, "/feature_fits")
        object.__setattr__(
            self,
            "feature_fits",
            tuple(
                sorted(
                    self.feature_fits,
                    key=lambda item: (item.source_index, item.local_feature_id),
                )
            ),
        )


def _mapping_unknown_reason(error: MetrologyError) -> EvidenceFeatureFitUnknownReason:
    if error.code is MetrologyErrorCode.OUTSIDE_CALIBRATION_DOMAIN:
        return EvidenceFeatureFitUnknownReason.OUTSIDE_CALIBRATION_DOMAIN
    if error.code is MetrologyErrorCode.CALIBRATION_NOT_DECISION_ELIGIBLE:
        return EvidenceFeatureFitUnknownReason.CALIBRATION_NOT_DECISION_ELIGIBLE
    return EvidenceFeatureFitUnknownReason.MAPPING_FAILURE


def _unknown_fit(
    feature: BoundFeatureEvidence,
    reason: EvidenceFeatureFitUnknownReason,
    *,
    frame_id: str | None = None,
    calibration_sha256: str | None = None,
) -> EvidenceFeatureFit:
    return EvidenceFeatureFit(
        source_index=feature.source_index,
        provider_image_id=feature.provider_image_id,
        local_feature_id=feature.local_feature_id,
        family=feature.family,
        claim_ids=feature.claim_ids,
        frame_id=frame_id,
        calibration_sha256=calibration_sha256,
        status=EvidenceFeatureFitStatus.UNKNOWN,
        plane_points=(),
        fit_result=None,
        unknown_reason=reason,
    )


def fit_bound_visual_evidence(
    *,
    evidence: BoundVisualEvidence,
    calibrations: tuple[SourcePlanarCalibration, ...],
    policies: tuple[FeatureFitPolicy, ...],
) -> VisualEvidenceFitReport:
    """Map and fit only explicitly requested features, failing closed per feature."""

    if type(evidence) is not BoundVisualEvidence:
        _fail(VisualFitPipelineErrorCode.INVALID_INPUT, "/evidence")
    if type(calibrations) is not tuple or type(policies) is not tuple:
        _fail(VisualFitPipelineErrorCode.INVALID_INPUT)
    if len(calibrations) > MAX_PIPELINE_CALIBRATIONS or len(policies) > MAX_PIPELINE_POLICIES:
        _fail(VisualFitPipelineErrorCode.BUDGET_EXCEEDED)
    if any(type(item) is not SourcePlanarCalibration for item in calibrations) or any(
        type(item) is not FeatureFitPolicy for item in policies
    ):
        _fail(VisualFitPipelineErrorCode.INVALID_INPUT)
    calibration_by_source = {item.source_index: item for item in calibrations}
    if len(calibration_by_source) != len(calibrations):
        _fail(VisualFitPipelineErrorCode.DUPLICATE_ID, "/calibrations")
    policy_by_key = {(item.source_index, item.local_feature_id): item for item in policies}
    if len(policy_by_key) != len(policies):
        _fail(VisualFitPipelineErrorCode.DUPLICATE_ID, "/policies")
    features_by_key = {
        (item.source_index, item.local_feature_id): item for item in evidence.features
    }
    if not set(policy_by_key).issubset(features_by_key):
        _fail(VisualFitPipelineErrorCode.UNKNOWN_REFERENCE, "/policies")
    features_by_source: dict[int, tuple[BoundFeatureEvidence, ...]] = {}
    for source_index in {item.source_index for item in evidence.features}:
        features_by_source[source_index] = tuple(
            item for item in evidence.features if item.source_index == source_index
        )
    for calibration in calibrations:
        source_features = features_by_source.get(calibration.source_index)
        if source_features is None:
            _fail(VisualFitPipelineErrorCode.UNKNOWN_REFERENCE, "/calibrations")
        if calibration.image_set_manifest_sha256 != evidence.image_set_manifest_sha256 or any(
            item.provider_image_id != calibration.provider_image_id for item in source_features
        ):
            _fail(VisualFitPipelineErrorCode.BINDING_MISMATCH, "/calibrations")

    fitted: list[EvidenceFeatureFit] = []
    for feature in evidence.features:
        key = (feature.source_index, feature.local_feature_id)
        policy = policy_by_key.get(key)
        if policy is None:
            fitted.append(_unknown_fit(feature, EvidenceFeatureFitUnknownReason.NOT_REQUESTED))
            continue
        calibration = calibration_by_source.get(feature.source_index)
        if calibration is None:
            fitted.append(
                _unknown_fit(feature, EvidenceFeatureFitUnknownReason.MISSING_CALIBRATION)
            )
            continue
        try:
            plane_points = tuple(
                map_pixel_to_plane(calibration.calibration, point) for point in feature.pixel_points
            )
        except MetrologyError as error:
            fitted.append(
                _unknown_fit(
                    feature,
                    _mapping_unknown_reason(error),
                    frame_id=calibration.frame_id,
                    calibration_sha256=calibration.calibration_sha256,
                )
            )
            continue
        try:
            result = fit_declared_geometry(
                GeometryFitRequest(
                    family=feature.family,
                    points=plane_points,
                    residual_tolerance_mm=policy.residual_tolerance_mm,
                )
            )
        except GeometryFitError:
            fitted.append(
                EvidenceFeatureFit(
                    source_index=feature.source_index,
                    provider_image_id=feature.provider_image_id,
                    local_feature_id=feature.local_feature_id,
                    family=feature.family,
                    claim_ids=feature.claim_ids,
                    frame_id=calibration.frame_id,
                    calibration_sha256=calibration.calibration_sha256,
                    status=EvidenceFeatureFitStatus.UNKNOWN,
                    plane_points=plane_points,
                    fit_result=None,
                    unknown_reason=EvidenceFeatureFitUnknownReason.FIT_FAILURE,
                )
            )
            continue
        if result.status is GeometryFitStatus.FITTED:
            fitted.append(
                EvidenceFeatureFit(
                    source_index=feature.source_index,
                    provider_image_id=feature.provider_image_id,
                    local_feature_id=feature.local_feature_id,
                    family=feature.family,
                    claim_ids=feature.claim_ids,
                    frame_id=calibration.frame_id,
                    calibration_sha256=calibration.calibration_sha256,
                    status=EvidenceFeatureFitStatus.FITTED,
                    plane_points=plane_points,
                    fit_result=result,
                    unknown_reason=None,
                )
            )
        else:
            fitted.append(
                EvidenceFeatureFit(
                    source_index=feature.source_index,
                    provider_image_id=feature.provider_image_id,
                    local_feature_id=feature.local_feature_id,
                    family=feature.family,
                    claim_ids=feature.claim_ids,
                    frame_id=calibration.frame_id,
                    calibration_sha256=calibration.calibration_sha256,
                    status=EvidenceFeatureFitStatus.UNKNOWN,
                    plane_points=plane_points,
                    fit_result=result,
                    unknown_reason=EvidenceFeatureFitUnknownReason.FIT_REJECTED,
                )
            )
    return VisualEvidenceFitReport(
        reconstruction_id=evidence.reconstruction_id,
        generation=evidence.generation,
        image_set_id=evidence.image_set_id,
        image_set_manifest_sha256=evidence.image_set_manifest_sha256,
        image_batch_manifest_sha256=evidence.image_batch_manifest_sha256,
        observation_id=evidence.observation_id,
        observation_digest=evidence.observation_digest,
        feature_fits=tuple(fitted),
    )


__all__ = [
    "MAX_PIPELINE_CALIBRATIONS",
    "MAX_PIPELINE_POLICIES",
    "VISUAL_FIT_PIPELINE_SCHEMA_VERSION",
    "EvidenceFeatureFit",
    "EvidenceFeatureFitStatus",
    "EvidenceFeatureFitUnknownReason",
    "FeatureFitPolicy",
    "SourcePlanarCalibration",
    "VisualEvidenceFitReport",
    "VisualFitPipelineError",
    "VisualFitPipelineErrorCode",
    "fit_bound_visual_evidence",
]
