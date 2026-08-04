"""Focused contracts for provider-specific visual derivatives."""

from __future__ import annotations

import dataclasses
import hashlib
import io

import pytest
from PIL import Image

from vibecad.visual import (
    MAX_IMAGE_SET_ITEMS,
    NORMALIZATION_PROFILE,
    SOURCE_PNG_PROFILE,
    CalibrationStatus,
    ImageMime,
    ImageRef,
    ImageSet,
    ProcessingAuthorization,
    ProviderDetailCrop,
    ProviderImageBatch,
    ProviderImageDetail,
    ProviderImageError,
    ProviderImageErrorCode,
    ProviderImagePartKind,
    ViewRole,
    VisualInput,
    VisualProviderCapabilityProfile,
    image_set_identity,
    prepare_provider_image_batch,
    visual_input_identity,
)

_CREATE_KEY = "image_set_create_abcdefabcdefabcdefabcdefabcdefab"


def _png(index: int, *, size: tuple[int, int] = (96, 64)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", size, (20 + index, 80, 140)).save(
        stream,
        format="PNG",
        pnginfo=None,
    )
    return stream.getvalue()


def _image_set(
    count: int,
    *,
    authorization: ProcessingAuthorization = ProcessingAuthorization.CLOUD_PROVIDER,
) -> tuple[ImageSet, tuple[bytes, ...]]:
    image_set_id, create_digest = image_set_identity(_CREATE_KEY)
    raws = tuple(_png(index) for index in range(count))
    roles = tuple(ViewRole)
    inputs = tuple(
        VisualInput(
            original=ImageRef(
                id=visual_input_identity(_CREATE_KEY, index, "original"),
                sha256=hashlib.sha256(b"source" + bytes([index])).hexdigest(),
                size_bytes=128,
                mime=ImageMime.PNG,
                width=96,
                height=64,
                profile=SOURCE_PNG_PROFILE,
            ),
            normalized=ImageRef(
                id=visual_input_identity(_CREATE_KEY, index, "normalized"),
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                mime=ImageMime.PNG,
                width=96,
                height=64,
                profile=NORMALIZATION_PROFILE,
            ),
            view_role=roles[index % len(roles)],
            calibration_status=CalibrationStatus.UNKNOWN,
        )
        for index, raw in enumerate(raws)
    )
    return (
        ImageSet(
            id=image_set_id,
            create_key_digest=create_digest,
            inputs=inputs,
            unit="mm",
            dimension_hints=(),
            calibration_evidence=(),
            same_object=True,
            same_state=True,
            same_scale=True,
            processing_authorization=authorization,
        ),
        raws,
    )


def _profile(**changes) -> VisualProviderCapabilityProfile:
    values = {
        "provider": "candidate",
        "model": "vision-model",
        "model_version": "2026-08-04",
        "data_policy_profile": "personal-default",
        "max_source_images": MAX_IMAGE_SET_ITEMS,
        "max_image_parts": 20,
        "max_image_bytes": 2 * 1024 * 1024,
        "max_batch_image_bytes": 20 * 1024 * 1024,
        "preferred_long_edge": 1568,
        "max_long_edge": 2000,
        "detail": ProviderImageDetail.HIGH,
        "supports_detail_crops": True,
        "transport_timeout_ms": 120_000,
    }
    values.update(changes)
    return VisualProviderCapabilityProfile(**values)


def test_sixteen_source_images_prepare_without_paths_or_original_bytes() -> None:
    image_set, raws = _image_set(MAX_IMAGE_SET_ITEMS)

    batch = prepare_provider_image_batch(
        image_set=image_set,
        normalized_images=raws,
        profile=_profile(),
    )

    assert len(batch.parts) == 16
    assert all(item.kind is ProviderImagePartKind.OVERVIEW for item in batch.parts)
    assert tuple(item.source_index for item in batch.parts) == tuple(range(16))
    assert batch.total_bytes == sum(item.size_bytes for item in batch.parts)
    assert batch.to_manifest_mapping()["manifest_sha256"] == batch.manifest_sha256
    assert "data" not in batch.to_manifest_mapping()["parts"][0]
    assert all(item.data.startswith(b"\x89PNG\r\n\x1a\n") for item in batch.parts)


def test_detail_crop_is_explicit_and_bound_to_its_source() -> None:
    image_set, raws = _image_set(2)
    crop = ProviderDetailCrop(
        source_index=1,
        left=0.25,
        top=0.25,
        right=0.75,
        bottom=0.75,
        label="dimension-callout",
    )

    batch = prepare_provider_image_batch(
        image_set=image_set,
        normalized_images=raws,
        profile=_profile(),
        detail_crops=(crop,),
    )

    detail = batch.parts[-1]
    assert detail.kind is ProviderImagePartKind.DETAIL_CROP
    assert detail.source_index == 1
    assert detail.label == "dimension-callout"
    assert (detail.width, detail.height) == (48, 32)
    assert detail.id != batch.parts[1].id


@pytest.mark.parametrize(
    ("case", "expected"),
    (
        ("local-only", ProviderImageErrorCode.INVALID_INPUT),
        ("provider-count", ProviderImageErrorCode.BUDGET_EXCEEDED),
        ("tampered", ProviderImageErrorCode.INTEGRITY_FAILURE),
        ("unsupported-crop", ProviderImageErrorCode.INVALID_INPUT),
    ),
)
def test_preparation_fails_closed_without_silent_source_drops(case: str, expected) -> None:
    authorization = (
        ProcessingAuthorization.LOCAL_ONLY
        if case == "local-only"
        else ProcessingAuthorization.CLOUD_PROVIDER
    )
    image_set, raws = _image_set(2, authorization=authorization)
    profile = _profile(
        max_source_images=1 if case == "provider-count" else 16,
        supports_detail_crops=case != "unsupported-crop",
    )
    crops = (
        ProviderDetailCrop(
            source_index=0,
            left=0,
            top=0,
            right=0.5,
            bottom=0.5,
            label="small-hole",
        ),
    )
    if case == "tampered":
        raws = (raws[0] + b"tamper", raws[1])

    with pytest.raises(ProviderImageError) as caught:
        prepare_provider_image_batch(
            image_set=image_set,
            normalized_images=raws,
            profile=profile,
            detail_crops=crops if case == "unsupported-crop" else (),
        )

    assert caught.value.code is expected


def test_profile_rejects_unbounded_or_internally_inconsistent_limits() -> None:
    with pytest.raises(ProviderImageError):
        _profile(max_source_images=17)
    with pytest.raises(ProviderImageError):
        _profile(max_source_images=16, max_image_parts=15)
    with pytest.raises(ProviderImageError):
        _profile(preferred_long_edge=2048, max_long_edge=1024)
    with pytest.raises(ProviderImageError):
        _profile(transport_timeout_ms=0)


def test_batch_rejects_silently_missing_sources_and_profile_mismatch() -> None:
    image_set, raws = _image_set(2)
    profile = _profile()
    batch = prepare_provider_image_batch(
        image_set=image_set,
        normalized_images=raws,
        profile=profile,
    )

    with pytest.raises(ProviderImageError) as missing:
        ProviderImageBatch(
            image_set_id=batch.image_set_id,
            image_set_manifest_sha256=batch.image_set_manifest_sha256,
            profile=profile,
            parts=batch.parts[1:],
            total_bytes=sum(item.size_bytes for item in batch.parts[1:]),
        )
    assert missing.value.code is ProviderImageErrorCode.INTEGRITY_FAILURE

    mismatched = (dataclasses.replace(batch.parts[0], detail=ProviderImageDetail.LOW),) + (
        batch.parts[1],
    )
    with pytest.raises(ProviderImageError) as detail:
        ProviderImageBatch(
            image_set_id=batch.image_set_id,
            image_set_manifest_sha256=batch.image_set_manifest_sha256,
            profile=profile,
            parts=mismatched,
            total_bytes=sum(item.size_bytes for item in mismatched),
        )
    assert detail.value.code is ProviderImageErrorCode.INTEGRITY_FAILURE
