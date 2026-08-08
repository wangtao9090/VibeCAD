"""Focused tests for private, overview-only visual coordinate evidence."""

from __future__ import annotations

import dataclasses
import hashlib
import io

import pytest
from PIL import Image

from vibecad.visual.contracts import (
    NORMALIZATION_PROFILE,
    SOURCE_PNG_PROFILE,
    CalibrationStatus,
    ImageMime,
    ImageRef,
    ImageSet,
    ProcessingAuthorization,
    ViewRole,
    VisualInput,
    image_set_identity,
    visual_input_identity,
)
from vibecad.visual.evidence import (
    MAX_EVIDENCE_FEATURES,
    MAX_EVIDENCE_POINTS_PER_FEATURE,
    EvidenceCoordinateSpace,
    NormalizedEvidencePoint,
    ProviderFeatureEvidence,
    VisualEvidenceError,
    VisualEvidenceErrorCode,
    bind_visual_evidence,
)
from vibecad.visual.geometry_fit import PrimitiveFamily
from vibecad.visual.provider_images import (
    ProviderDetailCrop,
    ProviderImageDetail,
    VisualProviderCapabilityProfile,
    prepare_provider_image_batch,
)
from vibecad.visual.reconstruction import (
    VisualClaim,
    VisualClaimStatus,
    VisualObservation,
    reconstruction_identity,
    visual_invocation_identity,
)

_IMAGE_CREATE_KEY = "image_set_create_abcdefabcdefabcdefabcdefabcdefab"
_RECONSTRUCTION_CREATE_KEY = "reconstruction_create_11111111111111111111111111111111"


def _png(index: int, *, size: tuple[int, int]) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", size, (20 + index, 80, 140)).save(stream, format="PNG")
    return stream.getvalue()


def _image_set(*sizes: tuple[int, int]) -> tuple[ImageSet, tuple[bytes, ...]]:
    image_set_id, create_digest = image_set_identity(_IMAGE_CREATE_KEY)
    raws = tuple(_png(index, size=size) for index, size in enumerate(sizes))
    inputs = tuple(
        VisualInput(
            original=ImageRef(
                id=visual_input_identity(_IMAGE_CREATE_KEY, index, "original"),
                sha256=hashlib.sha256(b"source" + bytes([index])).hexdigest(),
                size_bytes=128,
                mime=ImageMime.PNG,
                width=size[0],
                height=size[1],
                profile=SOURCE_PNG_PROFILE,
            ),
            normalized=ImageRef(
                id=visual_input_identity(_IMAGE_CREATE_KEY, index, "normalized"),
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                mime=ImageMime.PNG,
                width=size[0],
                height=size[1],
                profile=NORMALIZATION_PROFILE,
            ),
            view_role=ViewRole.FRONT if index == 0 else ViewRole.TOP,
            calibration_status=CalibrationStatus.UNKNOWN,
        )
        for index, (size, raw) in enumerate(zip(sizes, raws, strict=True))
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
            processing_authorization=ProcessingAuthorization.CLOUD_PROVIDER,
        ),
        raws,
    )


def _profile(*, preferred_long_edge: int = 1024) -> VisualProviderCapabilityProfile:
    return VisualProviderCapabilityProfile(
        provider="candidate",
        model="vision-model",
        model_version="2026-08-08",
        data_policy_profile="personal-default",
        max_source_images=16,
        max_image_parts=20,
        max_image_bytes=2 * 1024 * 1024,
        max_batch_image_bytes=20 * 1024 * 1024,
        preferred_long_edge=preferred_long_edge,
        max_long_edge=2048,
        detail=ProviderImageDetail.HIGH,
        supports_detail_crops=True,
        transport_timeout_ms=120_000,
    )


def _observation(image_set: ImageSet, *, source_indices: tuple[int, ...] = (0,)):
    claim = VisualClaim(
        name="outer_profile",
        status=(
            VisualClaimStatus.CROSS_VIEW_DERIVED
            if len(source_indices) > 1
            else VisualClaimStatus.CONFIRMED
        ),
        source_indices=source_indices,
        value=True,
        unit=None,
        description="Visible profile evidence",
    )
    reconstruction_id, _digest = reconstruction_identity(_RECONSTRUCTION_CREATE_KEY)
    generation = 1
    return (
        VisualObservation(
            reconstruction_id=reconstruction_id,
            generation=generation,
            image_set_id=image_set.id,
            image_set_manifest_sha256=image_set.manifest_sha256,
            invocation_id=visual_invocation_identity(
                reconstruction_id,
                generation,
                image_set.id,
                image_set.manifest_sha256,
            ),
            claims=(claim,),
        ),
        claim,
    )


def _feature(part_id: str, claim_id: str, *, source_index: int = 0):
    return ProviderFeatureEvidence(
        local_feature_id="outer.edge",
        source_index=source_index,
        provider_image_id=part_id,
        family=PrimitiveFamily.LINE,
        points=(
            NormalizedEvidencePoint(x=0.0, y=0.0),
            NormalizedEvidencePoint(x=0.5, y=0.5),
            NormalizedEvidencePoint(x=1.0, y=1.0),
        ),
        localization_uncertainty_norm=0.01,
        claim_ids=(claim_id,),
    )


def test_overview_coordinates_bind_to_exact_source_pixels_and_observation() -> None:
    image_set, raws = _image_set((96, 64))
    batch = prepare_provider_image_batch(
        image_set=image_set,
        normalized_images=raws,
        profile=_profile(),
    )
    observation, claim = _observation(image_set)

    result = bind_visual_evidence(
        observation=observation,
        image_set=image_set,
        image_batch=batch,
        features=(_feature(batch.parts[0].id, claim.id),),
    )

    assert result.observation_id == observation.id
    assert result.observation_digest == observation.digest
    assert result.image_batch_manifest_sha256 == batch.manifest_sha256
    assert result.features[0].claim_ids == (claim.id,)
    assert tuple((point.x_px, point.y_px) for point in result.features[0].pixel_points) == (
        (0.0, 0.0),
        (47.5, 31.5),
        (95.0, 63.0),
    )
    assert all(
        point.uncertainty_px == pytest.approx(0.95) for point in result.features[0].pixel_points
    )


def test_downsampled_overview_enforces_half_provider_pixel_uncertainty_floor() -> None:
    image_set, raws = _image_set((400, 200))
    batch = prepare_provider_image_batch(
        image_set=image_set,
        normalized_images=raws,
        profile=_profile(preferred_long_edge=256),
    )
    observation, claim = _observation(image_set)
    feature = dataclasses.replace(
        _feature(batch.parts[0].id, claim.id),
        localization_uncertainty_norm=1e-9,
    )

    result = bind_visual_evidence(
        observation=observation,
        image_set=image_set,
        image_batch=batch,
        features=(feature,),
    )

    expected_floor = 0.5 * max(399 / 255, 199 / 127)
    assert batch.parts[0].width == 256
    assert result.features[0].pixel_points[0].uncertainty_px == pytest.approx(expected_floor)
    assert result.features[0].pixel_points[0].uncertainty_px > 0.5


def test_detail_crop_cannot_become_coordinate_evidence_without_transform() -> None:
    image_set, raws = _image_set((96, 64))
    batch = prepare_provider_image_batch(
        image_set=image_set,
        normalized_images=raws,
        profile=_profile(),
        detail_crops=(
            ProviderDetailCrop(
                source_index=0,
                left=0.25,
                top=0.25,
                right=0.75,
                bottom=0.75,
                label="detail",
            ),
        ),
    )
    observation, claim = _observation(image_set)

    with pytest.raises(VisualEvidenceError) as caught:
        bind_visual_evidence(
            observation=observation,
            image_set=image_set,
            image_batch=batch,
            features=(_feature(batch.parts[-1].id, claim.id),),
        )

    assert caught.value.code is VisualEvidenceErrorCode.UNSAFE_DERIVATIVE


def test_claim_source_part_and_manifest_bindings_fail_closed() -> None:
    image_set, raws = _image_set((96, 64), (96, 64))
    batch = prepare_provider_image_batch(
        image_set=image_set,
        normalized_images=raws,
        profile=_profile(),
    )
    observation, claim = _observation(image_set, source_indices=(1,))

    cases = (
        (
            _feature(batch.parts[0].id, claim.id),
            VisualEvidenceErrorCode.BINDING_MISMATCH,
        ),
        (
            _feature("provider_image_" + "f" * 32, claim.id),
            VisualEvidenceErrorCode.UNKNOWN_REFERENCE,
        ),
        (
            _feature(batch.parts[1].id, claim.id, source_index=0),
            VisualEvidenceErrorCode.BINDING_MISMATCH,
        ),
        (
            dataclasses.replace(
                _feature(batch.parts[0].id, claim.id),
                claim_ids=("visual_claim_" + "f" * 32,),
            ),
            VisualEvidenceErrorCode.UNKNOWN_REFERENCE,
        ),
    )
    for feature, expected in cases:
        with pytest.raises(VisualEvidenceError) as caught:
            bind_visual_evidence(
                observation=observation,
                image_set=image_set,
                image_batch=batch,
                features=(feature,),
            )
        assert caught.value.code is expected

    other_image_set, _other_raws = _image_set((97, 64), (96, 64))
    changed_observation, _other_claim = _observation(other_image_set, source_indices=(1,))
    with pytest.raises(VisualEvidenceError) as manifest:
        bind_visual_evidence(
            observation=changed_observation,
            image_set=image_set,
            image_batch=batch,
            features=(),
        )
    assert manifest.value.code is VisualEvidenceErrorCode.BINDING_MISMATCH


def test_contract_rejects_bad_coordinate_space_uncertainty_and_containers() -> None:
    with pytest.raises(VisualEvidenceError) as coordinate:
        NormalizedEvidencePoint(x=-0.01, y=0.5)
    assert coordinate.value.code is VisualEvidenceErrorCode.INVALID_INPUT

    base = ProviderFeatureEvidence(
        local_feature_id="edge",
        source_index=0,
        provider_image_id="provider_image_" + "1" * 32,
        family=PrimitiveFamily.LINE,
        points=(NormalizedEvidencePoint(x=0.0, y=0.0),),
        localization_uncertainty_norm=0.1,
        claim_ids=("visual_claim_" + "2" * 32,),
    )
    with pytest.raises(VisualEvidenceError) as zero_uncertainty:
        dataclasses.replace(base, localization_uncertainty_norm=0.0)
    assert zero_uncertainty.value.code is VisualEvidenceErrorCode.INVALID_INPUT
    with pytest.raises(VisualEvidenceError) as coordinates:
        dataclasses.replace(base, coordinate_space="detail_crop_normalized")
    assert coordinates.value.code is VisualEvidenceErrorCode.INVALID_INPUT
    with pytest.raises(VisualEvidenceError) as list_points:
        dataclasses.replace(base, points=list(base.points))  # type: ignore[arg-type]
    assert list_points.value.code is VisualEvidenceErrorCode.INVALID_INPUT


def test_feature_and_total_point_budgets_precede_binding_work(monkeypatch) -> None:
    image_set, raws = _image_set((96, 64))
    batch = prepare_provider_image_batch(
        image_set=image_set,
        normalized_images=raws,
        profile=_profile(),
    )
    observation, claim = _observation(image_set)
    base = _feature(batch.parts[0].id, claim.id)

    with pytest.raises(VisualEvidenceError) as per_feature:
        dataclasses.replace(
            base,
            points=tuple(
                NormalizedEvidencePoint(x=0.5, y=0.5)
                for _ in range(MAX_EVIDENCE_POINTS_PER_FEATURE + 1)
            ),
        )
    assert per_feature.value.code is VisualEvidenceErrorCode.BUDGET_EXCEEDED

    excessive = tuple(base for _ in range(MAX_EVIDENCE_FEATURES + 1))

    def must_not_index(_batch):
        raise AssertionError("over-budget features reached binding")

    monkeypatch.setattr("vibecad.visual.evidence._part_by_id", must_not_index)
    with pytest.raises(VisualEvidenceError) as feature_count:
        bind_visual_evidence(
            observation=observation,
            image_set=image_set,
            image_batch=batch,
            features=excessive,
        )
    assert feature_count.value.code is VisualEvidenceErrorCode.BUDGET_EXCEEDED

    many_points = tuple(NormalizedEvidencePoint(x=0.5, y=0.5) for _ in range(64))
    total_over_budget = tuple(
        dataclasses.replace(
            base,
            local_feature_id=f"edge.{index}",
            points=many_points,
        )
        for index in range(9)
    )
    with pytest.raises(VisualEvidenceError) as total_points:
        bind_visual_evidence(
            observation=observation,
            image_set=image_set,
            image_batch=batch,
            features=total_over_budget,
        )
    assert total_points.value.code is VisualEvidenceErrorCode.BUDGET_EXCEEDED


def test_duplicate_local_feature_key_is_rejected_before_binding() -> None:
    image_set, raws = _image_set((96, 64))
    batch = prepare_provider_image_batch(
        image_set=image_set,
        normalized_images=raws,
        profile=_profile(),
    )
    observation, claim = _observation(image_set)
    feature = _feature(batch.parts[0].id, claim.id)

    with pytest.raises(VisualEvidenceError) as duplicate:
        bind_visual_evidence(
            observation=observation,
            image_set=image_set,
            image_batch=batch,
            features=(feature, feature),
        )

    assert duplicate.value.code is VisualEvidenceErrorCode.INVALID_INPUT


def test_empty_evidence_is_a_valid_safe_outcome() -> None:
    image_set, raws = _image_set((96, 64))
    batch = prepare_provider_image_batch(
        image_set=image_set,
        normalized_images=raws,
        profile=_profile(),
    )
    observation, _claim = _observation(image_set)

    result = bind_visual_evidence(
        observation=observation,
        image_set=image_set,
        image_batch=batch,
        features=(),
    )

    assert result.features == ()
    assert result.schema_version == 1
    assert EvidenceCoordinateSpace.OVERVIEW_NORMALIZED.value == "overview_normalized"
