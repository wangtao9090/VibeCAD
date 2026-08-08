"""Deterministic, authority-free capture-quality advice for sealed images.

The caller supplies normalized PNG bytes read from the visual-input store.  A
quality report may recommend a recapture or identify redundant views, but it
cannot approve geometry, mutate a Task, or adopt a candidate.  Heuristic
thresholds are deliberately advisory.  The set is stopped only when every
view is mechanically unreadable (too small or without measurable visual
signal); malformed or tampered input fails closed with a bounded error.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray
from PIL import Image, UnidentifiedImageError

from vibecad.visual.contracts import (
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SET_ITEMS,
    MAX_IMAGE_SET_PHYSICAL_BYTES,
    MAX_NORMALIZED_IMAGE_BYTES,
    MAX_NORMALIZED_LONG_EDGE,
)

CAPTURE_QUALITY_SCHEMA_VERSION = 1
MAX_QUALITY_ANALYSIS_LONG_EDGE = 1024
MIN_READABLE_EDGE = 32
RECOMMENDED_MIN_EDGE = 512

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_VISUAL_INPUT_ID = re.compile(r"^visual_input_[0-9a-f]{32}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_SHADOW_LEVEL = 0.03
_HIGHLIGHT_LEVEL = 0.97
_EXPOSURE_MEAN_LOW = 0.12
_EXPOSURE_MEAN_HIGH = 0.88
_EXPOSURE_CLIPPED_FRACTION = 0.50
_NO_SIGNAL_SPAN = 1.0 / 255.0
_NO_SIGNAL_SHARPNESS = 0.002
_LOW_CONTRAST_SPAN = 0.12
_BLUR_MIN_CONTRAST_SPAN = 0.02
_BLUR_RISK_SHARPNESS = 0.010
_DUPLICATE_FINGERPRINT_EDGE = 32
_DUPLICATE_ASPECT_RATIO_DELTA = 0.02
_DUPLICATE_DISTANCE = 0.10


class CaptureQualityErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTEGRITY_FAILURE = "integrity_failure"
    NUMERICAL_FAILURE = "numerical_failure"


class CaptureQualityError(ValueError):
    """Bounded failure that never reflects rejected image metadata."""

    def __init__(self, code: CaptureQualityErrorCode, path: str = "") -> None:
        if type(code) is not CaptureQualityErrorCode:
            raise TypeError("code must be an exact CaptureQualityErrorCode")
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


def _fail(code: CaptureQualityErrorCode, path: str = "") -> None:
    raise CaptureQualityError(code, path)


class CaptureQualityDecision(StrEnum):
    READY = "ready"
    RECAPTURE_RECOMMENDED = "recapture_recommended"
    STOP = "stop"


class CaptureQualitySeverity(StrEnum):
    ADVISORY = "advisory"
    UNREADABLE = "unreadable"


class CaptureQualityIssueCode(StrEnum):
    UNREADABLE_DIMENSIONS = "unreadable_dimensions"
    NO_VISUAL_SIGNAL = "no_visual_signal"
    LOW_RESOLUTION = "low_resolution"
    UNDEREXPOSED = "underexposed"
    OVEREXPOSED = "overexposed"
    LOW_CONTRAST = "low_contrast"
    BLUR_RISK = "blur_risk"
    NEAR_DUPLICATE = "near_duplicate"


@dataclass(frozen=True, slots=True, kw_only=True)
class NormalizedCaptureImage:
    """One exact normalized PNG read from a sealed visual-input generation."""

    source_index: int
    visual_input_id: str
    width: int
    height: int
    sha256: str
    data: bytes = field(repr=False, compare=False)
    schema_version: int = CAPTURE_QUALITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != CAPTURE_QUALITY_SCHEMA_VERSION
        ):
            _fail(CaptureQualityErrorCode.INVALID_INPUT, "/schema_version")
        if type(self.source_index) is not int or not 0 <= self.source_index < MAX_IMAGE_SET_ITEMS:
            _fail(CaptureQualityErrorCode.INVALID_INPUT, "/source_index")
        if (
            type(self.visual_input_id) is not str
            or _VISUAL_INPUT_ID.fullmatch(self.visual_input_id) is None
        ):
            _fail(CaptureQualityErrorCode.INVALID_INPUT, "/visual_input_id")
        for field_name in ("width", "height"):
            value = getattr(self, field_name)
            if type(value) is not int or not 0 < value <= MAX_NORMALIZED_LONG_EDGE:
                _fail(CaptureQualityErrorCode.INVALID_INPUT, f"/{field_name}")
        if self.width * self.height > MAX_IMAGE_PIXELS:
            _fail(CaptureQualityErrorCode.BUDGET_EXCEEDED, "/pixels")
        if type(self.sha256) is not str or _DIGEST.fullmatch(self.sha256) is None:
            _fail(CaptureQualityErrorCode.INVALID_INPUT, "/sha256")
        if type(self.data) is not bytes:
            _fail(CaptureQualityErrorCode.INVALID_INPUT, "/data")
        if not self.data:
            _fail(CaptureQualityErrorCode.INVALID_INPUT, "/data")
        if len(self.data) > MAX_NORMALIZED_IMAGE_BYTES:
            _fail(CaptureQualityErrorCode.BUDGET_EXCEEDED, "/data")
        if not self.data.startswith(_PNG_SIGNATURE):
            _fail(CaptureQualityErrorCode.INTEGRITY_FAILURE, "/data")
        if not hmac.compare_digest(hashlib.sha256(self.data).hexdigest(), self.sha256):
            _fail(CaptureQualityErrorCode.INTEGRITY_FAILURE, "/sha256")


@dataclass(frozen=True, slots=True, kw_only=True)
class CaptureFrameMetrics:
    source_index: int
    width: int
    height: int
    mean_luminance: float
    shadow_fraction: float
    highlight_fraction: float
    contrast_span: float
    sharpness: float


@dataclass(frozen=True, slots=True, kw_only=True)
class CaptureQualityFinding:
    code: CaptureQualityIssueCode
    severity: CaptureQualitySeverity
    source_indices: tuple[int, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class CaptureQualityReport:
    decision: CaptureQualityDecision
    metrics: tuple[CaptureFrameMetrics, ...]
    findings: tuple[CaptureQualityFinding, ...]
    readable_source_indices: tuple[int, ...]
    redundant_source_indices: tuple[int, ...]
    schema_version: int = CAPTURE_QUALITY_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class _Analysis:
    metrics: CaptureFrameMetrics
    fingerprint: NDArray[np.float32] = field(repr=False, compare=False)
    aspect_ratio: float
    unreadable: bool


def _decode_normalized(image: NormalizedCaptureImage, path: str) -> Image.Image:
    try:
        with Image.open(io.BytesIO(image.data)) as decoded:
            if (
                decoded.format != "PNG"
                or getattr(decoded, "n_frames", 1) != 1
                or decoded.size != (image.width, image.height)
                or decoded.mode not in {"RGB", "RGBA"}
            ):
                _fail(CaptureQualityErrorCode.INTEGRITY_FAILURE, path)
            decoded.load()
            if decoded.mode == "RGBA":
                background = Image.new("RGBA", decoded.size, (255, 255, 255, 255))
                rgb = Image.alpha_composite(background, decoded).convert("RGB")
            else:
                rgb = decoded.copy()
    except CaptureQualityError:
        raise
    except (
        Image.DecompressionBombError,
        MemoryError,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        ValueError,
    ):
        _fail(CaptureQualityErrorCode.INTEGRITY_FAILURE, path)
    return rgb


def _analysis_rgb(image: Image.Image) -> Image.Image:
    if max(image.size) <= MAX_QUALITY_ANALYSIS_LONG_EDGE:
        return image
    scale = MAX_QUALITY_ANALYSIS_LONG_EDGE / max(image.size)
    size = (
        max(1, round(image.width * scale)),
        max(1, round(image.height * scale)),
    )
    return image.resize(size, Image.Resampling.LANCZOS)


def _luminance(rgb: Image.Image) -> NDArray[np.float32]:
    values = np.asarray(rgb, dtype=np.float32) / np.float32(255.0)
    if values.ndim != 3 or values.shape[2] != 3:
        _fail(CaptureQualityErrorCode.NUMERICAL_FAILURE)
    result = (
        values[:, :, 0] * np.float32(0.2126)
        + values[:, :, 1] * np.float32(0.7152)
        + values[:, :, 2] * np.float32(0.0722)
    )
    if not np.isfinite(result).all():
        _fail(CaptureQualityErrorCode.NUMERICAL_FAILURE)
    return result


def _sharpness(luminance: NDArray[np.float32]) -> float:
    if min(luminance.shape) < 3:
        return 0.0
    center = luminance[1:-1, 1:-1]
    laplacian = (
        np.float32(4.0) * center
        - luminance[:-2, 1:-1]
        - luminance[2:, 1:-1]
        - luminance[1:-1, :-2]
        - luminance[1:-1, 2:]
    )
    result = float(np.sqrt(np.mean(np.square(laplacian, dtype=np.float32))))
    if not math.isfinite(result):
        _fail(CaptureQualityErrorCode.NUMERICAL_FAILURE)
    return result


def _fingerprint(rgb: Image.Image) -> NDArray[np.float32]:
    small = rgb.resize(
        (_DUPLICATE_FINGERPRINT_EDGE, _DUPLICATE_FINGERPRINT_EDGE),
        Image.Resampling.LANCZOS,
    )
    luminance = _luminance(small)
    mean = float(np.mean(luminance))
    deviation = float(np.std(luminance))
    if not math.isfinite(mean) or not math.isfinite(deviation):
        _fail(CaptureQualityErrorCode.NUMERICAL_FAILURE)
    if deviation <= 1e-6:
        return np.zeros_like(luminance, dtype=np.float32)
    return ((luminance - np.float32(mean)) / np.float32(deviation)).astype(
        np.float32,
        copy=False,
    )


def _analyze(image: NormalizedCaptureImage, path: str) -> _Analysis:
    rgb = _decode_normalized(image, path)
    sampled = _analysis_rgb(rgb)
    luminance = _luminance(sampled)
    try:
        percentiles = np.percentile(luminance, (1.0, 5.0, 95.0, 99.0))
    except (FloatingPointError, MemoryError, ValueError):
        _fail(CaptureQualityErrorCode.NUMERICAL_FAILURE, path)
    p01, p05, p95, p99 = (float(value) for value in percentiles)
    metrics = CaptureFrameMetrics(
        source_index=image.source_index,
        width=image.width,
        height=image.height,
        mean_luminance=float(np.mean(luminance)),
        shadow_fraction=float(np.mean(luminance <= _SHADOW_LEVEL)),
        highlight_fraction=float(np.mean(luminance >= _HIGHLIGHT_LEVEL)),
        contrast_span=p95 - p05,
        sharpness=_sharpness(luminance),
    )
    numeric_values = (
        metrics.mean_luminance,
        metrics.shadow_fraction,
        metrics.highlight_fraction,
        metrics.contrast_span,
        metrics.sharpness,
        p99 - p01,
    )
    if any(not math.isfinite(value) for value in numeric_values):
        _fail(CaptureQualityErrorCode.NUMERICAL_FAILURE, path)
    unreadable = min(image.width, image.height) < MIN_READABLE_EDGE or (
        p99 - p01 <= _NO_SIGNAL_SPAN and metrics.sharpness <= _NO_SIGNAL_SHARPNESS
    )
    return _Analysis(
        metrics=metrics,
        fingerprint=_fingerprint(sampled),
        aspect_ratio=image.width / image.height,
        unreadable=unreadable,
    )


def _finding(
    code: CaptureQualityIssueCode,
    severity: CaptureQualitySeverity,
    *source_indices: int,
) -> CaptureQualityFinding:
    return CaptureQualityFinding(
        code=code,
        severity=severity,
        source_indices=tuple(source_indices),
    )


def _frame_findings(analysis: _Analysis) -> list[CaptureQualityFinding]:
    metrics = analysis.metrics
    source_index = metrics.source_index
    if min(metrics.width, metrics.height) < MIN_READABLE_EDGE:
        return [
            _finding(
                CaptureQualityIssueCode.UNREADABLE_DIMENSIONS,
                CaptureQualitySeverity.UNREADABLE,
                source_index,
            )
        ]
    if analysis.unreadable:
        return [
            _finding(
                CaptureQualityIssueCode.NO_VISUAL_SIGNAL,
                CaptureQualitySeverity.UNREADABLE,
                source_index,
            )
        ]

    findings: list[CaptureQualityFinding] = []
    if min(metrics.width, metrics.height) < RECOMMENDED_MIN_EDGE:
        findings.append(
            _finding(
                CaptureQualityIssueCode.LOW_RESOLUTION,
                CaptureQualitySeverity.ADVISORY,
                source_index,
            )
        )
    if (
        metrics.mean_luminance < _EXPOSURE_MEAN_LOW
        and metrics.shadow_fraction >= _EXPOSURE_CLIPPED_FRACTION
    ):
        findings.append(
            _finding(
                CaptureQualityIssueCode.UNDEREXPOSED,
                CaptureQualitySeverity.ADVISORY,
                source_index,
            )
        )
    if (
        metrics.mean_luminance > _EXPOSURE_MEAN_HIGH
        and metrics.highlight_fraction >= _EXPOSURE_CLIPPED_FRACTION
    ):
        findings.append(
            _finding(
                CaptureQualityIssueCode.OVEREXPOSED,
                CaptureQualitySeverity.ADVISORY,
                source_index,
            )
        )
    if metrics.contrast_span < _LOW_CONTRAST_SPAN:
        findings.append(
            _finding(
                CaptureQualityIssueCode.LOW_CONTRAST,
                CaptureQualitySeverity.ADVISORY,
                source_index,
            )
        )
    if (
        metrics.contrast_span >= _BLUR_MIN_CONTRAST_SPAN
        and metrics.sharpness < _BLUR_RISK_SHARPNESS
    ):
        findings.append(
            _finding(
                CaptureQualityIssueCode.BLUR_RISK,
                CaptureQualitySeverity.ADVISORY,
                source_index,
            )
        )
    return findings


def _near_duplicate(first: _Analysis, second: _Analysis) -> bool:
    aspect_delta = abs(first.aspect_ratio - second.aspect_ratio) / max(
        first.aspect_ratio,
        second.aspect_ratio,
    )
    if aspect_delta > _DUPLICATE_ASPECT_RATIO_DELTA:
        return False
    distance = float(np.sqrt(np.mean(np.square(first.fingerprint - second.fingerprint))))
    if not math.isfinite(distance):
        _fail(CaptureQualityErrorCode.NUMERICAL_FAILURE)
    return distance <= _DUPLICATE_DISTANCE


def _quality_rank(
    analysis: _Analysis,
    frame_findings: tuple[CaptureQualityFinding, ...],
) -> tuple[int, int, int, float, float, float, int]:
    """Rank duplicate candidates without turning the score into authority."""

    metrics = analysis.metrics
    return (
        -len(frame_findings),
        min(metrics.width, metrics.height),
        metrics.width * metrics.height,
        metrics.sharpness,
        metrics.contrast_span,
        -abs(metrics.mean_luminance - 0.5),
        -metrics.source_index,
    )


def assess_capture_quality(images: tuple[NormalizedCaptureImage, ...]) -> CaptureQualityReport:
    """Return deterministic capture advice for a bounded normalized image set."""

    if type(images) is not tuple:
        _fail(CaptureQualityErrorCode.INVALID_INPUT, "/images")
    if not images:
        _fail(CaptureQualityErrorCode.INVALID_INPUT, "/images")
    if len(images) > MAX_IMAGE_SET_ITEMS:
        _fail(CaptureQualityErrorCode.BUDGET_EXCEEDED, "/images")
    if any(type(image) is not NormalizedCaptureImage for image in images):
        _fail(CaptureQualityErrorCode.INVALID_INPUT, "/images")
    source_indices = tuple(image.source_index for image in images)
    image_ids = tuple(image.visual_input_id for image in images)
    if len(set(source_indices)) != len(source_indices) or len(set(image_ids)) != len(image_ids):
        _fail(CaptureQualityErrorCode.INVALID_INPUT, "/images")
    if sum(len(image.data) for image in images) > MAX_IMAGE_SET_PHYSICAL_BYTES:
        _fail(CaptureQualityErrorCode.BUDGET_EXCEEDED, "/images")

    ordered = tuple(sorted(images, key=lambda image: image.source_index))
    analyses = tuple(
        _analyze(image, f"/images/{position}") for position, image in enumerate(ordered)
    )
    findings_by_source = {
        item.metrics.source_index: tuple(_frame_findings(item)) for item in analyses
    }
    findings = [
        finding for item in analyses for finding in findings_by_source[item.metrics.source_index]
    ]
    readable = tuple(item.metrics.source_index for item in analyses if not item.unreadable)

    redundant: set[int] = set()
    duplicate_findings: list[CaptureQualityFinding] = []
    ranked = sorted(
        (item for item in analyses if not item.unreadable),
        key=lambda item: _quality_rank(
            item,
            findings_by_source[item.metrics.source_index],
        ),
        reverse=True,
    )
    retained: list[_Analysis] = []
    for candidate in ranked:
        for first in retained:
            if not _near_duplicate(first, candidate):
                continue
            redundant.add(candidate.metrics.source_index)
            duplicate_findings.append(
                _finding(
                    CaptureQualityIssueCode.NEAR_DUPLICATE,
                    CaptureQualitySeverity.ADVISORY,
                    first.metrics.source_index,
                    candidate.metrics.source_index,
                )
            )
            break
        else:
            retained.append(candidate)
    findings.extend(
        sorted(duplicate_findings, key=lambda finding: tuple(sorted(finding.source_indices)))
    )

    if not readable:
        decision = CaptureQualityDecision.STOP
    elif findings:
        decision = CaptureQualityDecision.RECAPTURE_RECOMMENDED
    else:
        decision = CaptureQualityDecision.READY
    return CaptureQualityReport(
        decision=decision,
        metrics=tuple(item.metrics for item in analyses),
        findings=tuple(findings),
        readable_source_indices=readable,
        redundant_source_indices=tuple(sorted(redundant)),
    )


__all__ = [
    "CAPTURE_QUALITY_SCHEMA_VERSION",
    "MAX_QUALITY_ANALYSIS_LONG_EDGE",
    "MIN_READABLE_EDGE",
    "RECOMMENDED_MIN_EDGE",
    "CaptureFrameMetrics",
    "CaptureQualityDecision",
    "CaptureQualityError",
    "CaptureQualityErrorCode",
    "CaptureQualityFinding",
    "CaptureQualityIssueCode",
    "CaptureQualityReport",
    "CaptureQualitySeverity",
    "NormalizedCaptureImage",
    "assess_capture_quality",
]
