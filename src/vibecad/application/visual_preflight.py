"""Private, read-only capture preflight for one sealed visual generation.

This bridge deliberately owns no durable state and exposes no service or MCP
surface.  It composes the existing exact ImageSet reader with the
authority-free capture-quality analyzer so callers cannot accidentally assess
bytes from a different generation.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from vibecad.visual.capture_quality import (
    CaptureQualityReport,
    NormalizedCaptureImage,
    assess_capture_quality,
)
from vibecad.visual.inputs import (
    VisualInputStore,
    VisualInputStoreError,
    VisualInputStoreErrorCode,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SealedCaptureQualityReport:
    """Capture advice bound to one exact immutable ImageSet generation."""

    image_set_id: str
    image_set_manifest_sha256: str
    quality: CaptureQualityReport


def assess_sealed_capture_quality(
    *,
    store: VisualInputStore,
    image_set_id: object,
    image_set_manifest_sha256: object,
) -> SealedCaptureQualityReport:
    """Assess exact cloud-authorized bytes without invoking or mutating a provider."""

    if type(store) is not VisualInputStore:
        raise TypeError("store must be an exact VisualInputStore")
    record, normalized_bytes = store.read_provider_images_exact(
        image_set_id,
        image_set_manifest_sha256,
    )
    if (
        type(image_set_id) is not str
        or type(image_set_manifest_sha256) is not str
        or record.id != image_set_id
        or not hmac.compare_digest(record.manifest_sha256, image_set_manifest_sha256)
        or len(record.inputs) != len(normalized_bytes)
    ):
        raise VisualInputStoreError(VisualInputStoreErrorCode.INTEGRITY_FAILURE)

    captures = tuple(
        NormalizedCaptureImage(
            source_index=index,
            visual_input_id=item.normalized.id,
            width=item.normalized.width,
            height=item.normalized.height,
            sha256=item.normalized.sha256,
            data=normalized_bytes[index],
        )
        for index, item in enumerate(record.inputs)
    )
    return SealedCaptureQualityReport(
        image_set_id=record.id,
        image_set_manifest_sha256=record.manifest_sha256,
        quality=assess_capture_quality(captures),
    )


__all__ = ("SealedCaptureQualityReport", "assess_sealed_capture_quality")
