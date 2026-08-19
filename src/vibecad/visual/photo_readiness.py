"""Fail-closed readiness advice for ordinary-photo mechanical reconstruction.

The gate composes existing sealed-input, capture, provider-evidence, and
metric-fit contracts.  It never invokes a provider, infers a feature family or
cross-view relationship, constructs CAD, or grants adoption authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, fields
from enum import StrEnum

from vibecad.visual.capture_quality import (
    CAPTURE_QUALITY_SCHEMA_VERSION,
    CaptureFrameMetrics,
    CaptureQualityDecision,
    CaptureQualityFinding,
    CaptureQualityIssueCode,
    CaptureQualityReport,
    CaptureQualitySeverity,
)
from vibecad.visual.contracts import (
    MAX_IMAGE_SET_ITEMS,
    CalibrationKind,
    CalibrationStatus,
    ImageSet,
)
from vibecad.visual.evidence import (
    MAX_EVIDENCE_FEATURES,
    BoundVisualEvidence,
)
from vibecad.visual.fit_pipeline import (
    EvidenceFeatureFit,
    EvidenceFeatureFitStatus,
    VisualEvidenceFitReport,
)
from vibecad.visual.geometry_fit import (
    ArcPrimitive,
    CirclePrimitive,
    GeometryFitStatus,
    LinePrimitive,
    PrimitiveFamily,
    RotatedRectanglePrimitive,
)
from vibecad.visual.reconstruction import (
    MAX_VISUAL_CLAIMS,
    VisualClaimStatus,
    VisualObservation,
)

PHOTO_READINESS_SCHEMA_VERSION = 1
MAX_PHOTO_READINESS_RECORD_BYTES = 128 * 1024
MAX_PHOTO_READINESS_FINDINGS = (
    MAX_VISUAL_CLAIMS + 2 * MAX_EVIDENCE_FEATURES + MAX_IMAGE_SET_ITEMS + 3
)
MAX_CAPTURE_QUALITY_FINDINGS = 5 * MAX_IMAGE_SET_ITEMS - 1

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_SET_ID = re.compile(r"^image_set_[0-9a-f]{32}$")
_OBSERVATION_ID = re.compile(r"^visual_observation_[0-9a-f]{32}$")
_LOCAL_FEATURE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_READINESS_DIGEST_DOMAIN = b"vibecad-photo-readiness-v1\0"


class PhotoReadinessErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    BINDING_MISMATCH = "binding_mismatch"


class PhotoReadinessError(ValueError):
    """Bounded error for malformed or mutually inconsistent inputs."""

    def __init__(self, code: PhotoReadinessErrorCode, path: str = "") -> None:
        if type(code) is not PhotoReadinessErrorCode:
            raise TypeError("code must be an exact PhotoReadinessErrorCode")
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


def _fail(code: PhotoReadinessErrorCode, path: str = "") -> None:
    raise PhotoReadinessError(code, path)


class PhotoReadinessDecision(StrEnum):
    READY = "ready"
    NEEDS_CAPTURE = "needs_capture"
    OUT_OF_ENVELOPE = "out_of_envelope"
    UNKNOWN = "unknown"


class PhotoReadinessIssueCode(StrEnum):
    CAPTURE_UNREADABLE = "capture_unreadable"
    CROSS_VIEW_OBJECT_UNDECLARED = "cross_view_object_undeclared"
    CROSS_VIEW_STATE_UNDECLARED = "cross_view_state_undeclared"
    AMBIGUOUS_SCALE = "ambiguous_scale"
    MISSING_EXPLICIT_SCALE = "missing_explicit_scale"
    PROVIDER_CONFLICT = "provider_conflict"
    BLOCKING_UNKNOWN = "blocking_unknown"
    MISSING_REQUIRED_EVIDENCE = "missing_required_evidence"
    REQUIRED_FAMILY_MISMATCH = "required_family_mismatch"
    MISSING_REQUIRED_FIT = "missing_required_fit"
    REQUIRED_FIT_UNKNOWN = "required_fit_unknown"


def _finding_key(item: PhotoReadinessFinding) -> tuple[str, int, str, str]:
    return (
        item.code.value,
        -1 if item.source_index is None else item.source_index,
        item.local_feature_id or "",
        item.claim_id or "",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class RequiredPhotoFeature:
    """One caller-declared local feature; no cross-view identity is implied."""

    source_index: int
    local_feature_id: str
    family: PrimitiveFamily

    def __post_init__(self) -> None:
        if type(self.source_index) is not int or not 0 <= self.source_index < MAX_IMAGE_SET_ITEMS:
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/required_features/source_index")
        if (
            type(self.local_feature_id) is not str
            or _LOCAL_FEATURE_ID.fullmatch(self.local_feature_id) is None
        ):
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/required_features/local_feature_id")
        if type(self.family) is not PrimitiveFamily:
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/required_features/family")

    def to_mapping(self) -> dict[str, object]:
        return {
            "source_index": self.source_index,
            "local_feature_id": self.local_feature_id,
            "family": self.family.value,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PhotoReadinessFinding:
    code: PhotoReadinessIssueCode
    source_index: int | None = None
    local_feature_id: str | None = None
    claim_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.code) is not PhotoReadinessIssueCode:
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/findings/code")
        if self.source_index is not None and (
            type(self.source_index) is not int or not 0 <= self.source_index < MAX_IMAGE_SET_ITEMS
        ):
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/findings/source_index")
        if self.local_feature_id is not None and (
            type(self.local_feature_id) is not str
            or _LOCAL_FEATURE_ID.fullmatch(self.local_feature_id) is None
        ):
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/findings/local_feature_id")
        if self.claim_id is not None and (
            type(self.claim_id) is not str
            or re.fullmatch(r"visual_claim_[0-9a-f]{32}", self.claim_id) is None
        ):
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/findings/claim_id")

    def to_mapping(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "source_index": self.source_index,
            "local_feature_id": self.local_feature_id,
            "claim_id": self.claim_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PhotoReadinessReport:
    decision: PhotoReadinessDecision
    findings: tuple[PhotoReadinessFinding, ...]
    required_features: tuple[RequiredPhotoFeature, ...]
    image_set_id: str
    image_set_manifest_sha256: str
    observation_id: str
    observation_digest: str
    image_batch_manifest_sha256: str
    capture_quality_sha256: str
    evidence_sha256: str
    fit_report_sha256: str
    digest: str = ""
    schema_version: int = PHOTO_READINESS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != PHOTO_READINESS_SCHEMA_VERSION
        ):
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/schema_version")
        if type(self.decision) is not PhotoReadinessDecision:
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/decision")
        if type(self.findings) is not tuple or any(
            type(item) is not PhotoReadinessFinding for item in self.findings
        ):
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/findings")
        if len(self.findings) > MAX_PHOTO_READINESS_FINDINGS:
            _fail(PhotoReadinessErrorCode.BUDGET_EXCEEDED, "/findings")
        if len(set(self.findings)) != len(self.findings):
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/findings")
        object.__setattr__(self, "findings", tuple(sorted(self.findings, key=_finding_key)))
        if type(self.required_features) is not tuple or not self.required_features:
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/required_features")
        if len(self.required_features) > MAX_EVIDENCE_FEATURES:
            _fail(PhotoReadinessErrorCode.BUDGET_EXCEEDED, "/required_features")
        if any(type(item) is not RequiredPhotoFeature for item in self.required_features):
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/required_features")
        required_keys = tuple(
            (item.source_index, item.local_feature_id) for item in self.required_features
        )
        if len(required_keys) != len(set(required_keys)):
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/required_features")
        object.__setattr__(
            self,
            "required_features",
            tuple(
                sorted(
                    self.required_features,
                    key=lambda item: (item.source_index, item.local_feature_id),
                )
            ),
        )
        for name, pattern in (
            ("image_set_id", _IMAGE_SET_ID),
            ("observation_id", _OBSERVATION_ID),
        ):
            value = getattr(self, name)
            if type(value) is not str or pattern.fullmatch(value) is None:
                _fail(PhotoReadinessErrorCode.INVALID_INPUT, f"/{name}")
        for name in (
            "image_set_manifest_sha256",
            "observation_digest",
            "image_batch_manifest_sha256",
            "capture_quality_sha256",
            "evidence_sha256",
            "fit_report_sha256",
        ):
            value = getattr(self, name)
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                _fail(PhotoReadinessErrorCode.INVALID_INPUT, f"/{name}")
        if self.decision is not _decision(self.findings):
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/decision")
        body = self._body_mapping()
        expected = hashlib.sha256(_READINESS_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()
        if self.digest and self.digest != expected:
            _fail(PhotoReadinessErrorCode.BINDING_MISMATCH, "/digest")
        object.__setattr__(self, "digest", expected)

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "decision": self.decision.value,
            "findings": [item.to_mapping() for item in self.findings],
            "required_features": [item.to_mapping() for item in self.required_features],
            "image_set_id": self.image_set_id,
            "image_set_manifest_sha256": self.image_set_manifest_sha256,
            "observation_id": self.observation_id,
            "observation_digest": self.observation_digest,
            "image_batch_manifest_sha256": self.image_batch_manifest_sha256,
            "capture_quality_sha256": self.capture_quality_sha256,
            "evidence_sha256": self.evidence_sha256,
            "fit_report_sha256": self.fit_report_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return self._body_mapping() | {"digest": self.digest}


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
        _fail(PhotoReadinessErrorCode.INVALID_INPUT)
    if len(raw) > MAX_PHOTO_READINESS_RECORD_BYTES:
        _fail(PhotoReadinessErrorCode.BUDGET_EXCEEDED)
    return raw


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _validate_quality(report: CaptureQualityReport, image_set: ImageSet) -> dict[str, object]:
    if type(report) is not CaptureQualityReport:
        _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/capture_quality")
    if (
        type(report.schema_version) is not int
        or report.schema_version != CAPTURE_QUALITY_SCHEMA_VERSION
        or type(report.decision) is not CaptureQualityDecision
        or type(report.metrics) is not tuple
        or type(report.findings) is not tuple
        or type(report.readable_source_indices) is not tuple
        or type(report.redundant_source_indices) is not tuple
    ):
        _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/capture_quality")
    if (
        len(report.metrics) > MAX_IMAGE_SET_ITEMS
        or len(report.findings) > MAX_CAPTURE_QUALITY_FINDINGS
    ):
        _fail(PhotoReadinessErrorCode.BUDGET_EXCEEDED, "/capture_quality")
    if any(type(item) is not CaptureFrameMetrics for item in report.metrics) or any(
        type(item) is not CaptureQualityFinding for item in report.findings
    ):
        _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/capture_quality")
    expected_sources = tuple(range(len(image_set.inputs)))
    metric_sources = tuple(item.source_index for item in report.metrics)
    if metric_sources != expected_sources:
        _fail(PhotoReadinessErrorCode.BINDING_MISMATCH, "/capture_quality/metrics")
    metrics: list[dict[str, object]] = []
    for index, metric in enumerate(report.metrics):
        expected = image_set.inputs[index].normalized
        if type(metric.width) is not int or type(metric.height) is not int:
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/capture_quality/metrics")
        if (metric.width, metric.height) != (expected.width, expected.height):
            _fail(PhotoReadinessErrorCode.BINDING_MISMATCH, "/capture_quality/metrics")
        fractions = (
            metric.mean_luminance,
            metric.shadow_fraction,
            metric.highlight_fraction,
            metric.contrast_span,
        )
        numbers = fractions + (metric.sharpness,)
        if any(type(value) not in {int, float} or not math.isfinite(value) for value in numbers):
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/capture_quality/metrics")
        if any(not 0.0 <= value <= 1.0 for value in fractions) or metric.sharpness < 0.0:
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/capture_quality/metrics")
        metrics.append(
            {
                "source_index": metric.source_index,
                "width": metric.width,
                "height": metric.height,
                "mean_luminance": metric.mean_luminance,
                "shadow_fraction": metric.shadow_fraction,
                "highlight_fraction": metric.highlight_fraction,
                "contrast_span": metric.contrast_span,
                "sharpness": metric.sharpness,
            }
        )
    for name, indices in (
        ("readable_source_indices", report.readable_source_indices),
        ("redundant_source_indices", report.redundant_source_indices),
    ):
        if (
            tuple(sorted(indices)) != indices
            or len(set(indices)) != len(indices)
            or any(type(index) is not int or index not in expected_sources for index in indices)
        ):
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, f"/capture_quality/{name}")
    findings: list[dict[str, object]] = []
    for finding in report.findings:
        if (
            type(finding.code) is not CaptureQualityIssueCode
            or type(finding.severity) is not CaptureQualitySeverity
            or type(finding.source_indices) is not tuple
            or not finding.source_indices
            or len(set(finding.source_indices)) != len(finding.source_indices)
            or any(
                type(index) is not int or index not in expected_sources
                for index in finding.source_indices
            )
        ):
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/capture_quality/findings")
        findings.append(
            {
                "code": finding.code.value,
                "severity": finding.severity.value,
                "source_indices": list(finding.source_indices),
            }
        )
    if (report.decision is CaptureQualityDecision.STOP) != (not report.readable_source_indices):
        _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/capture_quality/decision")
    return {
        "schema_version": report.schema_version,
        "decision": report.decision.value,
        "metrics": metrics,
        "findings": findings,
        "readable_source_indices": list(report.readable_source_indices),
        "redundant_source_indices": list(report.redundant_source_indices),
    }


def _validate_bindings(
    image_set: ImageSet,
    observation: VisualObservation,
    evidence: BoundVisualEvidence,
    fit_report: VisualEvidenceFitReport,
) -> None:
    if (
        observation.image_set_id != image_set.id
        or observation.image_set_manifest_sha256 != image_set.manifest_sha256
        or evidence.image_set_id != image_set.id
        or evidence.image_set_manifest_sha256 != image_set.manifest_sha256
        or fit_report.image_set_id != image_set.id
        or fit_report.image_set_manifest_sha256 != image_set.manifest_sha256
        or evidence.reconstruction_id != observation.reconstruction_id
        or evidence.generation != observation.generation
        or evidence.observation_id != observation.id
        or evidence.observation_digest != observation.digest
        or fit_report.reconstruction_id != observation.reconstruction_id
        or fit_report.generation != observation.generation
        or fit_report.observation_id != observation.id
        or fit_report.observation_digest != observation.digest
        or fit_report.image_batch_manifest_sha256 != evidence.image_batch_manifest_sha256
    ):
        _fail(PhotoReadinessErrorCode.BINDING_MISMATCH)


def _evidence_mapping(evidence: BoundVisualEvidence) -> dict[str, object]:
    return {
        "image_batch_manifest_sha256": evidence.image_batch_manifest_sha256,
        "features": [
            {
                "source_index": item.source_index,
                "local_feature_id": item.local_feature_id,
                "provider_image_id": item.provider_image_id,
                "family": item.family.value,
                "claim_ids": list(item.claim_ids),
                "normalized_points": [[point.x, point.y] for point in item.normalized_points],
                "points": [
                    [point.x_px, point.y_px, point.uncertainty_px] for point in item.pixel_points
                ],
            }
            for item in evidence.features
        ],
    }


def _fit_mapping(item: EvidenceFeatureFit) -> dict[str, object]:
    result = item.fit_result
    primitive_mapping = None
    if result is not None:
        numbers = (
            result.rms_residual_mm,
            result.max_residual_mm,
            result.max_excess_residual_mm,
        )
        if any(
            value is not None and (type(value) not in {int, float} or not math.isfinite(value))
            for value in numbers
        ):
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/fit_report/feature_fits")
        if type(result.point_count) is not int or result.point_count < 0:
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/fit_report/feature_fits")
        primitive_types = {
            PrimitiveFamily.LINE: LinePrimitive,
            PrimitiveFamily.CIRCLE: CirclePrimitive,
            PrimitiveFamily.ARC: ArcPrimitive,
            PrimitiveFamily.ROTATED_RECTANGLE: RotatedRectanglePrimitive,
        }
        expected_primitive = primitive_types[result.family]
        if result.status is GeometryFitStatus.FITTED:
            if type(result.primitive) is not expected_primitive:
                _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/fit_report/feature_fits")
            primitive_mapping = {}
            for field in fields(result.primitive):
                value = getattr(result.primitive, field.name)
                if type(value) not in {int, float} or not math.isfinite(value):
                    _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/fit_report/feature_fits")
                primitive_mapping[field.name] = value
        elif result.primitive is not None:
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/fit_report/feature_fits")
    return {
        "source_index": item.source_index,
        "local_feature_id": item.local_feature_id,
        "provider_image_id": item.provider_image_id,
        "family": item.family.value,
        "claim_ids": list(item.claim_ids),
        "frame_id": item.frame_id,
        "calibration_sha256": item.calibration_sha256,
        "status": item.status.value,
        "unknown_reason": None if item.unknown_reason is None else item.unknown_reason.value,
        "plane_points": [
            [point.x_mm, point.y_mm, point.uncertainty_mm] for point in item.plane_points
        ],
        "fit_result": None
        if result is None
        else {
            "family": result.family.value,
            "status": result.status.value,
            "primitive": primitive_mapping,
            "rms_residual_mm": result.rms_residual_mm,
            "max_residual_mm": result.max_residual_mm,
            "max_excess_residual_mm": result.max_excess_residual_mm,
            "unknown_reason": None
            if result.unknown_reason is None
            else result.unknown_reason.value,
            "point_count": result.point_count,
        },
    }


def _decision(findings: tuple[PhotoReadinessFinding, ...]) -> PhotoReadinessDecision:
    codes = {item.code for item in findings}
    if codes & {
        PhotoReadinessIssueCode.CROSS_VIEW_OBJECT_UNDECLARED,
        PhotoReadinessIssueCode.CROSS_VIEW_STATE_UNDECLARED,
        PhotoReadinessIssueCode.PROVIDER_CONFLICT,
        PhotoReadinessIssueCode.REQUIRED_FAMILY_MISMATCH,
    }:
        return PhotoReadinessDecision.OUT_OF_ENVELOPE
    if codes & {
        PhotoReadinessIssueCode.CAPTURE_UNREADABLE,
        PhotoReadinessIssueCode.AMBIGUOUS_SCALE,
        PhotoReadinessIssueCode.MISSING_EXPLICIT_SCALE,
    }:
        return PhotoReadinessDecision.NEEDS_CAPTURE
    if findings:
        return PhotoReadinessDecision.UNKNOWN
    return PhotoReadinessDecision.READY


def assess_photo_readiness(
    *,
    image_set: ImageSet,
    capture_quality: CaptureQualityReport,
    observation: VisualObservation,
    evidence: BoundVisualEvidence,
    fit_report: VisualEvidenceFitReport,
    required_features: tuple[RequiredPhotoFeature, ...],
) -> PhotoReadinessReport:
    """Return deterministic advisory readiness without granting CAD authority."""

    for value, expected, path in (
        (image_set, ImageSet, "/image_set"),
        (observation, VisualObservation, "/observation"),
        (evidence, BoundVisualEvidence, "/evidence"),
        (fit_report, VisualEvidenceFitReport, "/fit_report"),
    ):
        if type(value) is not expected:
            _fail(PhotoReadinessErrorCode.INVALID_INPUT, path)
    if type(required_features) is not tuple or not required_features:
        _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/required_features")
    if len(required_features) > MAX_EVIDENCE_FEATURES:
        _fail(PhotoReadinessErrorCode.BUDGET_EXCEEDED, "/required_features")
    if any(type(item) is not RequiredPhotoFeature for item in required_features):
        _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/required_features")
    if any(item.source_index >= len(image_set.inputs) for item in required_features):
        _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/required_features/source_index")
    required = tuple(
        sorted(required_features, key=lambda item: (item.source_index, item.local_feature_id))
    )
    keys = tuple((item.source_index, item.local_feature_id) for item in required)
    if len(keys) != len(set(keys)):
        _fail(PhotoReadinessErrorCode.INVALID_INPUT, "/required_features")

    quality_mapping = _validate_quality(capture_quality, image_set)
    _validate_bindings(image_set, observation, evidence, fit_report)
    claim_by_id = {item.id: item for item in observation.claims}
    if any(
        source_index >= len(image_set.inputs)
        for claim in observation.claims
        for source_index in claim.source_indices
    ):
        _fail(PhotoReadinessErrorCode.BINDING_MISMATCH, "/observation/claims/source_indices")
    for feature in evidence.features:
        if feature.source_index >= len(image_set.inputs) or any(
            claim_id not in claim_by_id for claim_id in feature.claim_ids
        ):
            _fail(PhotoReadinessErrorCode.BINDING_MISMATCH, "/evidence/features/claim_ids")

    findings: list[PhotoReadinessFinding] = []
    required_sources = tuple(sorted({item.source_index for item in required}))
    readable = set(capture_quality.readable_source_indices)
    for source_index in required_sources:
        if source_index not in readable:
            findings.append(
                PhotoReadinessFinding(
                    code=PhotoReadinessIssueCode.CAPTURE_UNREADABLE,
                    source_index=source_index,
                )
            )
    if len(required_sources) > 1:
        if not image_set.same_object:
            findings.append(
                PhotoReadinessFinding(code=PhotoReadinessIssueCode.CROSS_VIEW_OBJECT_UNDECLARED)
            )
        if not image_set.same_state:
            findings.append(
                PhotoReadinessFinding(code=PhotoReadinessIssueCode.CROSS_VIEW_STATE_UNDECLARED)
            )
        if not image_set.same_scale:
            findings.append(PhotoReadinessFinding(code=PhotoReadinessIssueCode.AMBIGUOUS_SCALE))

    scale_sources = {
        item.source_index
        for item in image_set.calibration_evidence
        if item.kind is CalibrationKind.SCALE and item.scale_mm_per_pixel is not None
    }
    for source_index in required_sources:
        input_item = image_set.inputs[source_index]
        if (
            image_set.unit != "mm"
            or input_item.calibration_status is not CalibrationStatus.EXPLICIT_SCALE
            or source_index not in scale_sources
        ):
            findings.append(
                PhotoReadinessFinding(
                    code=PhotoReadinessIssueCode.MISSING_EXPLICIT_SCALE,
                    source_index=source_index,
                )
            )

    for claim in observation.claims:
        if claim.status is VisualClaimStatus.CONFLICT:
            findings.append(
                PhotoReadinessFinding(
                    code=PhotoReadinessIssueCode.PROVIDER_CONFLICT,
                    claim_id=claim.id,
                )
            )
        elif claim.blocking and claim.status is VisualClaimStatus.UNKNOWN:
            findings.append(
                PhotoReadinessFinding(
                    code=PhotoReadinessIssueCode.BLOCKING_UNKNOWN,
                    claim_id=claim.id,
                )
            )

    evidence_by_key = {
        (item.source_index, item.local_feature_id): item for item in evidence.features
    }
    fit_by_key = {
        (item.source_index, item.local_feature_id): item for item in fit_report.feature_fits
    }
    for key, fitted in fit_by_key.items():
        bound = evidence_by_key.get(key)
        if bound is None or (
            fitted.provider_image_id != bound.provider_image_id
            or fitted.family is not bound.family
            or fitted.claim_ids != bound.claim_ids
        ):
            _fail(PhotoReadinessErrorCode.BINDING_MISMATCH, "/fit_report/feature_fits")
    for requirement in required:
        key = (requirement.source_index, requirement.local_feature_id)
        bound = evidence_by_key.get(key)
        if bound is None:
            findings.append(
                PhotoReadinessFinding(
                    code=PhotoReadinessIssueCode.MISSING_REQUIRED_EVIDENCE,
                    source_index=requirement.source_index,
                    local_feature_id=requirement.local_feature_id,
                )
            )
            continue
        if bound.family is not requirement.family:
            findings.append(
                PhotoReadinessFinding(
                    code=PhotoReadinessIssueCode.REQUIRED_FAMILY_MISMATCH,
                    source_index=requirement.source_index,
                    local_feature_id=requirement.local_feature_id,
                )
            )
            continue
        fitted = fit_by_key.get(key)
        if fitted is None:
            findings.append(
                PhotoReadinessFinding(
                    code=PhotoReadinessIssueCode.MISSING_REQUIRED_FIT,
                    source_index=requirement.source_index,
                    local_feature_id=requirement.local_feature_id,
                )
            )
            continue
        if (
            fitted.family is not requirement.family
            or fitted.provider_image_id != bound.provider_image_id
        ):
            findings.append(
                PhotoReadinessFinding(
                    code=PhotoReadinessIssueCode.REQUIRED_FAMILY_MISMATCH,
                    source_index=requirement.source_index,
                    local_feature_id=requirement.local_feature_id,
                )
            )
            continue
        if (
            fitted.status is not EvidenceFeatureFitStatus.FITTED
            or fitted.fit_result is None
            or fitted.fit_result.status is not GeometryFitStatus.FITTED
            or fitted.calibration_sha256 is None
        ):
            findings.append(
                PhotoReadinessFinding(
                    code=PhotoReadinessIssueCode.REQUIRED_FIT_UNKNOWN,
                    source_index=requirement.source_index,
                    local_feature_id=requirement.local_feature_id,
                )
            )

    ordered_findings = tuple(sorted(findings, key=_finding_key))
    evidence_mapping = _evidence_mapping(evidence)
    fit_mapping = [_fit_mapping(item) for item in fit_report.feature_fits]
    return PhotoReadinessReport(
        decision=_decision(ordered_findings),
        findings=ordered_findings,
        required_features=required,
        image_set_id=image_set.id,
        image_set_manifest_sha256=image_set.manifest_sha256,
        observation_id=observation.id,
        observation_digest=observation.digest,
        image_batch_manifest_sha256=evidence.image_batch_manifest_sha256,
        capture_quality_sha256=_sha256(quality_mapping),
        evidence_sha256=_sha256(evidence_mapping),
        fit_report_sha256=_sha256(fit_mapping),
    )


__all__ = [
    "MAX_CAPTURE_QUALITY_FINDINGS",
    "MAX_PHOTO_READINESS_FINDINGS",
    "MAX_PHOTO_READINESS_RECORD_BYTES",
    "PHOTO_READINESS_SCHEMA_VERSION",
    "PhotoReadinessDecision",
    "PhotoReadinessError",
    "PhotoReadinessErrorCode",
    "PhotoReadinessFinding",
    "PhotoReadinessIssueCode",
    "PhotoReadinessReport",
    "RequiredPhotoFeature",
    "assess_photo_readiness",
]
