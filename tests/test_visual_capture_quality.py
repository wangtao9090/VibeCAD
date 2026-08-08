"""Focused tests for deterministic, authority-free capture-quality advice."""

from __future__ import annotations

import dataclasses
import hashlib
import io

import numpy as np
import pytest
from PIL import Image, ImageFilter

from vibecad.visual.capture_quality import (
    MIN_READABLE_EDGE,
    CaptureQualityDecision,
    CaptureQualityError,
    CaptureQualityErrorCode,
    CaptureQualityIssueCode,
    CaptureQualitySeverity,
    NormalizedCaptureImage,
    assess_capture_quality,
)
from vibecad.visual.contracts import MAX_IMAGE_SET_ITEMS


def _png(image: Image.Image) -> bytes:
    stream = io.BytesIO()
    image.save(stream, format="PNG", optimize=False)
    return stream.getvalue()


def _capture(index: int, image: Image.Image) -> NormalizedCaptureImage:
    rgb = image.convert("RGB")
    data = _png(rgb)
    return NormalizedCaptureImage(
        source_index=index,
        visual_input_id=f"visual_input_{index + 1:032x}",
        width=rgb.width,
        height=rgb.height,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def _checkerboard(size: int = 768, *, low: int = 24, high: int = 232) -> Image.Image:
    y, x = np.indices((size, size))
    cells = ((x // 24 + y // 24) % 2).astype(np.uint8)
    values = np.where(cells == 0, low, high).astype(np.uint8)
    return Image.fromarray(np.repeat(values[:, :, None], 3, axis=2), mode="RGB")


def _codes(report) -> tuple[CaptureQualityIssueCode, ...]:
    return tuple(finding.code for finding in report.findings)


def test_sharp_well_exposed_image_is_ready_and_metrics_are_deterministic() -> None:
    capture = _capture(0, _checkerboard())

    first = assess_capture_quality((capture,))
    second = assess_capture_quality((capture,))

    assert first == second
    assert first.decision is CaptureQualityDecision.READY
    assert first.findings == ()
    assert first.readable_source_indices == (0,)
    assert first.redundant_source_indices == ()
    assert first.metrics[0].contrast_span > 0.7
    assert first.metrics[0].sharpness > 0.01


def test_blur_exposure_contrast_and_resolution_are_advisory_only() -> None:
    blurred = _checkerboard().filter(ImageFilter.GaussianBlur(radius=14))
    dark = _checkerboard(low=1, high=24)
    bright = _checkerboard(low=232, high=254)
    low_contrast = _checkerboard(low=112, high=136)
    low_resolution = _checkerboard(size=256)

    report = assess_capture_quality(
        tuple(
            _capture(index, image)
            for index, image in enumerate((blurred, dark, bright, low_contrast, low_resolution))
        )
    )

    assert report.decision is CaptureQualityDecision.RECAPTURE_RECOMMENDED
    assert report.readable_source_indices == (0, 1, 2, 3, 4)
    codes = set(_codes(report))
    assert CaptureQualityIssueCode.BLUR_RISK in codes
    assert CaptureQualityIssueCode.UNDEREXPOSED in codes
    assert CaptureQualityIssueCode.OVEREXPOSED in codes
    assert CaptureQualityIssueCode.LOW_CONTRAST in codes
    assert CaptureQualityIssueCode.LOW_RESOLUTION in codes
    assert all(finding.severity is CaptureQualitySeverity.ADVISORY for finding in report.findings)


def test_only_mechanically_unreadable_views_stop_the_set() -> None:
    blank = Image.new("RGB", (640, 640), (128, 128, 128))
    tiny = _checkerboard(size=MIN_READABLE_EDGE - 1)

    report = assess_capture_quality((_capture(0, blank), _capture(1, tiny)))

    assert report.decision is CaptureQualityDecision.STOP
    assert report.readable_source_indices == ()
    assert _codes(report) == (
        CaptureQualityIssueCode.NO_VISUAL_SIGNAL,
        CaptureQualityIssueCode.UNREADABLE_DIMENSIONS,
    )
    assert all(finding.severity is CaptureQualitySeverity.UNREADABLE for finding in report.findings)


def test_one_unreadable_view_does_not_block_other_readable_evidence() -> None:
    report = assess_capture_quality(
        (
            _capture(0, Image.new("RGB", (640, 640), "black")),
            _capture(1, _checkerboard()),
        )
    )

    assert report.decision is CaptureQualityDecision.RECAPTURE_RECOMMENDED
    assert report.readable_source_indices == (1,)
    assert _codes(report) == (CaptureQualityIssueCode.NO_VISUAL_SIGNAL,)


def test_near_duplicate_is_order_independent_and_only_later_source_is_redundant() -> None:
    original = _checkerboard()
    shifted = original.point(lambda value: min(255, value + 3))
    distinct_array = np.asarray(original).copy()
    distinct_array[:, : distinct_array.shape[1] // 2] = (30, 180, 80)
    distinct = Image.fromarray(distinct_array, mode="RGB")
    captures = (_capture(7, distinct), _capture(3, shifted), _capture(1, original))

    report = assess_capture_quality(captures)

    assert tuple(metric.source_index for metric in report.metrics) == (1, 3, 7)
    assert report.redundant_source_indices == (3,)
    duplicate = next(
        finding
        for finding in report.findings
        if finding.code is CaptureQualityIssueCode.NEAR_DUPLICATE
    )
    assert duplicate.source_indices == (1, 3)


def test_near_duplicate_prefers_the_stronger_view_not_the_lower_index() -> None:
    high_resolution_image = _checkerboard(size=768)
    low_resolution = _capture(
        1,
        high_resolution_image.resize((256, 256), Image.Resampling.LANCZOS),
    )
    high_resolution = _capture(7, high_resolution_image)

    report = assess_capture_quality((low_resolution, high_resolution))

    assert report.redundant_source_indices == (1,)
    duplicate = next(
        finding
        for finding in report.findings
        if finding.code is CaptureQualityIssueCode.NEAR_DUPLICATE
    )
    assert duplicate.source_indices == (7, 1)


def test_exact_contract_rejects_tamper_bad_png_and_duplicate_identity() -> None:
    first = _capture(0, _checkerboard())

    with pytest.raises(CaptureQualityError) as tampered:
        dataclasses.replace(first, data=first.data + b"x")
    assert tampered.value.code is CaptureQualityErrorCode.INTEGRITY_FAILURE

    with pytest.raises(CaptureQualityError) as wrong_data_type:
        dataclasses.replace(first, data=bytearray(first.data))  # type: ignore[arg-type]
    assert wrong_data_type.value.code is CaptureQualityErrorCode.INVALID_INPUT

    with pytest.raises(CaptureQualityError) as wrong_dimensions:
        assess_capture_quality((dataclasses.replace(first, width=first.width - 1),))
    assert wrong_dimensions.value.code is CaptureQualityErrorCode.INTEGRITY_FAILURE

    raw = b"\x89PNG\r\n\x1a\nnot-a-png"
    forged = NormalizedCaptureImage(
        source_index=1,
        visual_input_id="visual_input_00000000000000000000000000000002",
        width=64,
        height=64,
        sha256=hashlib.sha256(raw).hexdigest(),
        data=raw,
    )
    with pytest.raises(CaptureQualityError) as invalid_png:
        assess_capture_quality((forged,))
    assert invalid_png.value.code is CaptureQualityErrorCode.INTEGRITY_FAILURE

    with pytest.raises(CaptureQualityError) as duplicate:
        assess_capture_quality((first, dataclasses.replace(first, source_index=1)))
    assert duplicate.value.code is CaptureQualityErrorCode.INVALID_INPUT


def test_container_and_count_budgets_fail_before_image_analysis(monkeypatch) -> None:
    capture = _capture(0, _checkerboard())

    with pytest.raises(CaptureQualityError) as non_tuple:
        assess_capture_quality([capture])  # type: ignore[arg-type]
    assert non_tuple.value.code is CaptureQualityErrorCode.INVALID_INPUT

    excessive = tuple(capture for _ in range(MAX_IMAGE_SET_ITEMS + 1))
    with pytest.raises(CaptureQualityError) as over_budget:
        assess_capture_quality(excessive)
    assert over_budget.value.code is CaptureQualityErrorCode.BUDGET_EXCEEDED

    def must_not_decode(*args, **kwargs):
        raise AssertionError("over-budget set reached image decoding")

    monkeypatch.setattr(Image, "open", must_not_decode)
    with pytest.raises(CaptureQualityError) as repeated:
        assess_capture_quality(excessive)
    assert repeated.value.code is CaptureQualityErrorCode.BUDGET_EXCEEDED
