"""Focused tests for the private A11 calibration-authority candidate."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import io
import json

import pytest
from PIL import Image

from vibecad.visual.calibration_authority import (
    IN_MEMORY_AUTHORITY_LEVEL,
    MAX_CALIBRATION_RECEIPT_BYTES,
    PLANAR_CALIBRATION_ALGORITHM,
    CalibrationAuthorityError,
    CalibrationAuthorityErrorCode,
    ConfirmedPlanarLandmark,
    ConfirmedPlanarMetricBasis,
    build_in_memory_planar_calibration_receipt,
)
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
from vibecad.visual.provider_images import (
    ProviderImageBatch,
    ProviderImageDetail,
    VisualProviderCapabilityProfile,
    prepare_provider_image_batch,
)

_CREATE_KEY = "image_set_create_11223344556677889900aabbccddeeff"


def _png() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (101, 101), (24, 52, 80)).save(stream, format="PNG")
    return stream.getvalue()


def _sealed_inputs() -> tuple[ImageSet, ProviderImageBatch]:
    raw = _png()
    image_set_id, create_key_digest = image_set_identity(_CREATE_KEY)
    image_set = ImageSet(
        id=image_set_id,
        create_key_digest=create_key_digest,
        inputs=(
            VisualInput(
                original=ImageRef(
                    id=visual_input_identity(_CREATE_KEY, 0, "original"),
                    sha256=hashlib.sha256(b"sealed-original").hexdigest(),
                    size_bytes=1024,
                    mime=ImageMime.PNG,
                    width=101,
                    height=101,
                    profile=SOURCE_PNG_PROFILE,
                ),
                normalized=ImageRef(
                    id=visual_input_identity(_CREATE_KEY, 0, "normalized"),
                    sha256=hashlib.sha256(raw).hexdigest(),
                    size_bytes=len(raw),
                    mime=ImageMime.PNG,
                    width=101,
                    height=101,
                    profile=NORMALIZATION_PROFILE,
                ),
                view_role=ViewRole.FRONT,
                calibration_status=CalibrationStatus.UNKNOWN,
            ),
        ),
        unit="mm",
        dimension_hints=(),
        calibration_evidence=(),
        same_object=True,
        same_state=True,
        same_scale=True,
        processing_authorization=ProcessingAuthorization.CLOUD_PROVIDER,
    )
    batch = prepare_provider_image_batch(
        image_set=image_set,
        normalized_images=(raw,),
        profile=VisualProviderCapabilityProfile(
            provider="candidate",
            model="vision-model",
            model_version="2026-08-08",
            data_policy_profile="personal-default",
            max_source_images=1,
            max_image_parts=1,
            max_image_bytes=2 * 1024 * 1024,
            max_batch_image_bytes=2 * 1024 * 1024,
            preferred_long_edge=512,
            max_long_edge=512,
            detail=ProviderImageDetail.HIGH,
            supports_detail_crops=False,
            transport_timeout_ms=120_000,
        ),
    )
    return image_set, batch


def _landmarks(*, scale: float = 1.0) -> tuple[ConfirmedPlanarLandmark, ...]:
    points = (
        ("origin", 0.0, 0.0, 0.0, 0.0),
        ("positive-x", 1.0, 0.0, 10.0 * scale, 0.0),
        ("positive-y", 0.0, 1.0, 0.0, 10.0 * scale),
        ("opposite", 1.0, 1.0, 10.0 * scale, 10.0 * scale),
    )
    return tuple(
        ConfirmedPlanarLandmark(
            landmark_id=identifier,
            confirmation_id=f"confirm-{identifier}",
            normalized_x=normalized_x,
            normalized_y=normalized_y,
            localization_uncertainty_norm=0.0,
            x_mm=x_mm,
            y_mm=y_mm,
        )
        for identifier, normalized_x, normalized_y, x_mm, y_mm in points
    )


def _basis(*, confirmation_id: str = "confirm-basis") -> ConfirmedPlanarMetricBasis:
    return ConfirmedPlanarMetricBasis(
        frame_id="front-plane",
        confirmation_id=confirmation_id,
        origin_landmark_id="origin",
        positive_x_landmark_id="positive-x",
        positive_y_landmark_id="positive-y",
    )


def _maximum_landmarks() -> tuple[ConfirmedPlanarLandmark, ...]:
    records = []
    for row in range(8):
        for column in range(8):
            identifier = {
                (0, 0): "origin",
                (0, 7): "positive-x",
                (7, 0): "positive-y",
            }.get((row, column), f"point-{row}-{column}")
            records.append(
                ConfirmedPlanarLandmark(
                    landmark_id=identifier,
                    confirmation_id=f"confirm-{identifier}",
                    normalized_x=column / 7,
                    normalized_y=row / 7,
                    localization_uncertainty_norm=0.0,
                    x_mm=float(column),
                    y_mm=float(row),
                )
            )
    return tuple(records)


def _build(*, scale: float = 1.0, confirmation_id: str = "confirm-basis"):
    image_set, batch = _sealed_inputs()
    return build_in_memory_planar_calibration_receipt(
        image_set=image_set,
        image_batch=batch,
        source_index=0,
        landmarks=_landmarks(scale=scale),
        metric_basis=_basis(confirmation_id=confirmation_id),
    )


def test_builder_derives_complete_stable_receipt_from_exact_sealed_bindings() -> None:
    image_set, batch = _sealed_inputs()

    receipt = build_in_memory_planar_calibration_receipt(
        image_set=image_set,
        image_batch=batch,
        source_index=0,
        landmarks=_landmarks(),
        metric_basis=_basis(),
    )
    replay = _build()

    assert receipt.receipt_sha256 == replay.receipt_sha256
    assert receipt.authority_binding_sha256 == replay.authority_binding_sha256
    assert receipt.image_set_manifest_sha256 == image_set.manifest_sha256
    assert receipt.provider_batch_manifest_sha256 == batch.manifest_sha256
    assert receipt.normalized_sha256 == image_set.inputs[0].normalized.sha256
    assert receipt.provider_image_id == batch.parts[0].id
    assert receipt.algorithm_id == PLANAR_CALIBRATION_ALGORITHM
    assert receipt.authority_level == IN_MEMORY_AUTHORITY_LEVEL
    assert receipt.calibration.landmark_count == 4
    assert receipt.calibration.decision_eligible
    assert receipt.calibration.valid_pixel_domain == (
        (0.0, 0.0),
        (100.0, 0.0),
        (100.0, 100.0),
        (0.0, 100.0),
    )
    assert not receipt.task_adoption_eligible
    raw = json.dumps(
        receipt.to_mapping(),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    assert len(raw) <= MAX_CALIBRATION_RECEIPT_BYTES


def test_sixty_four_landmarks_fit_within_receipt_budget() -> None:
    image_set, batch = _sealed_inputs()

    receipt = build_in_memory_planar_calibration_receipt(
        image_set=image_set,
        image_batch=batch,
        source_index=0,
        landmarks=_maximum_landmarks(),
        metric_basis=_basis(),
    )

    assert receipt.calibration.landmark_count == 64
    assert len(json.dumps(receipt.to_mapping(), separators=(",", ":"))) < (
        MAX_CALIBRATION_RECEIPT_BYTES
    )


def test_builder_has_no_matrix_digest_eligibility_or_provider_part_input_path() -> None:
    parameters = inspect.signature(build_in_memory_planar_calibration_receipt).parameters

    assert tuple(parameters) == (
        "image_set",
        "image_batch",
        "source_index",
        "landmarks",
        "metric_basis",
    )
    assert not {
        "pixel_to_plane",
        "plane_to_pixel",
        "calibration",
        "calibration_sha256",
        "decision_eligible",
        "provider_image_id",
    } & set(parameters)

    image_set, batch = _sealed_inputs()
    with pytest.raises(TypeError):
        build_in_memory_planar_calibration_receipt(
            image_set=image_set,
            image_batch=batch,
            source_index=0,
            landmarks=_landmarks(),
            metric_basis=_basis(),
            pixel_to_plane=((1.0, 0.0, 0.0),) * 3,  # type: ignore[call-arg]
        )


def test_matrix_and_digest_tampering_fail_closed() -> None:
    receipt = _build()
    forged_calibration = _build(scale=2.0).calibration

    with pytest.raises(CalibrationAuthorityError) as matrix_tamper:
        dataclasses.replace(receipt, calibration=forged_calibration)
    with pytest.raises(CalibrationAuthorityError) as digest_tamper:
        dataclasses.replace(receipt, authority_binding_sha256="f" * 64)
    with pytest.raises(CalibrationAuthorityError) as receipt_tamper:
        dataclasses.replace(receipt, receipt_sha256="e" * 64)

    assert matrix_tamper.value.code is CalibrationAuthorityErrorCode.INTEGRITY_FAILURE
    assert digest_tamper.value.code is CalibrationAuthorityErrorCode.INTEGRITY_FAILURE
    assert receipt_tamper.value.code is CalibrationAuthorityErrorCode.INTEGRITY_FAILURE


def test_authority_binding_covers_landmarks_frame_and_algorithm_inputs() -> None:
    receipt = _build()
    changed_landmarks = _build(scale=2.0)
    changed_frame = _build(confirmation_id="confirm-basis-v2")

    assert receipt.landmark_record_sha256 != changed_landmarks.landmark_record_sha256
    assert receipt.calibration_sha256 != changed_landmarks.calibration_sha256
    assert receipt.authority_binding_sha256 != changed_landmarks.authority_binding_sha256
    assert receipt.frame_record_sha256 != changed_frame.frame_record_sha256
    assert receipt.authority_binding_sha256 != changed_frame.authority_binding_sha256


def test_mismatched_batch_and_invalid_metric_basis_fail_closed() -> None:
    image_set, batch = _sealed_inputs()
    mismatched_batch = dataclasses.replace(
        batch,
        image_set_manifest_sha256="0" * 64,
        manifest_sha256="",
    )

    with pytest.raises(CalibrationAuthorityError) as mismatch:
        build_in_memory_planar_calibration_receipt(
            image_set=image_set,
            image_batch=mismatched_batch,
            source_index=0,
            landmarks=_landmarks(),
            metric_basis=_basis(),
        )
    with pytest.raises(CalibrationAuthorityError) as basis:
        build_in_memory_planar_calibration_receipt(
            image_set=image_set,
            image_batch=batch,
            source_index=0,
            landmarks=_landmarks(),
            metric_basis=dataclasses.replace(
                _basis(),
                origin_landmark_id="opposite",
            ),
        )

    assert mismatch.value.code is CalibrationAuthorityErrorCode.BINDING_MISMATCH
    assert basis.value.code is CalibrationAuthorityErrorCode.BINDING_MISMATCH


def test_positive_y_basis_landmark_must_lie_on_the_y_axis() -> None:
    image_set, batch = _sealed_inputs()
    landmarks = tuple(
        dataclasses.replace(item, x_mm=5.0) if item.landmark_id == "positive-y" else item
        for item in _landmarks()
    )

    with pytest.raises(CalibrationAuthorityError) as caught:
        build_in_memory_planar_calibration_receipt(
            image_set=image_set,
            image_batch=batch,
            source_index=0,
            landmarks=landmarks,
            metric_basis=_basis(),
        )

    assert caught.value.code is CalibrationAuthorityErrorCode.BINDING_MISMATCH
    assert caught.value.path == "/metric_basis/positive_y"


def test_overview_declared_dimensions_must_match_bounded_png_decode() -> None:
    image_set, batch = _sealed_inputs()
    forged_part = dataclasses.replace(batch.parts[0], width=201, height=201)
    forged_batch = dataclasses.replace(
        batch,
        parts=(forged_part,),
        manifest_sha256="",
    )

    with pytest.raises(CalibrationAuthorityError) as caught:
        build_in_memory_planar_calibration_receipt(
            image_set=image_set,
            image_batch=forged_batch,
            source_index=0,
            landmarks=_landmarks(),
            metric_basis=_basis(),
        )

    assert forged_part.data == batch.parts[0].data
    assert (forged_part.width, forged_part.height) == (201, 201)
    assert forged_batch.manifest_sha256 != batch.manifest_sha256
    assert caught.value.code is CalibrationAuthorityErrorCode.INTEGRITY_FAILURE
    assert caught.value.path == "/provider_overview/data"


@pytest.mark.parametrize(
    ("landmarks", "expected"),
    (
        (_landmarks()[:3], CalibrationAuthorityErrorCode.INVALID_INPUT),
        (
            tuple(
                ConfirmedPlanarLandmark(
                    landmark_id=f"point-{index}",
                    confirmation_id=f"confirm-{index}",
                    normalized_x=(index % 8) / 7,
                    normalized_y=(index // 8) / 8,
                    localization_uncertainty_norm=0.0,
                    x_mm=float(index % 8),
                    y_mm=float(index // 8),
                )
                for index in range(65)
            ),
            CalibrationAuthorityErrorCode.BUDGET_EXCEEDED,
        ),
    ),
)
def test_landmark_count_is_strictly_bounded(landmarks, expected) -> None:
    image_set, batch = _sealed_inputs()

    with pytest.raises(CalibrationAuthorityError) as caught:
        build_in_memory_planar_calibration_receipt(
            image_set=image_set,
            image_batch=batch,
            source_index=0,
            landmarks=landmarks,
            metric_basis=_basis(),
        )

    assert caught.value.code is expected


def test_nonfinite_and_nonexact_inputs_are_rejected_before_calibration() -> None:
    image_set, batch = _sealed_inputs()

    with pytest.raises(CalibrationAuthorityError) as nonfinite:
        ConfirmedPlanarLandmark(
            landmark_id="bad",
            confirmation_id="confirm-bad",
            normalized_x=float("nan"),
            normalized_y=0.0,
            localization_uncertainty_norm=0.0,
            x_mm=0.0,
            y_mm=0.0,
        )
    with pytest.raises(CalibrationAuthorityError) as nonexact:
        build_in_memory_planar_calibration_receipt(
            image_set=image_set,
            image_batch=batch,
            source_index=0,
            landmarks=list(_landmarks()),
            metric_basis=_basis(),
        )

    assert nonfinite.value.code is CalibrationAuthorityErrorCode.INVALID_INPUT
    assert nonexact.value.code is CalibrationAuthorityErrorCode.INVALID_INPUT


def test_huge_integer_conversion_is_a_bounded_invalid_input() -> None:
    with pytest.raises(CalibrationAuthorityError) as caught:
        ConfirmedPlanarLandmark(
            landmark_id="huge",
            confirmation_id="confirm-huge",
            normalized_x=0.0,
            normalized_y=0.0,
            localization_uncertainty_norm=0.0,
            x_mm=10**10_000,
            y_mm=0.0,
        )

    assert caught.value.code is CalibrationAuthorityErrorCode.INVALID_INPUT
    assert caught.value.path == "/x_mm"
