"""Focused tests for the private restartable admission-input codec."""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from vibecad.visual.admission_inputs import (
    MAX_VISUAL_ADMISSION_INPUT_BYTES,
    VISUAL_ADMISSION_INPUTS_KIND,
    VISUAL_ADMISSION_INPUTS_SCHEMA_VERSION,
    AdmissionExpectedDigests,
    AdmissionImageSetRef,
    VisualAdmissionInputBundle,
    VisualAdmissionInputError,
    VisualAdmissionInputErrorCode,
    decode_visual_admission_inputs,
    encode_visual_admission_inputs,
)
from vibecad.visual.calibration_authority import (
    ConfirmedPlanarLandmark,
    ConfirmedPlanarMetricBasis,
)
from vibecad.visual.drafts import ReconstructionPayloadKind, ReconstructionPayloadRef
from vibecad.visual.evidence import NormalizedEvidencePoint, ProviderFeatureEvidence
from vibecad.visual.geometry_fit import PrimitiveFamily
from vibecad.visual.provider_images import (
    ProviderImageDetail,
    VisualProviderCapabilityProfile,
)

_BUNDLE_DIGEST_DOMAIN = b"vibecad-visual-admission-inputs-v1\0"


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _ref(kind: ReconstructionPayloadKind) -> ReconstructionPayloadRef:
    identifier = {
        ReconstructionPayloadKind.OBSERVATION: "visual_observation_" + "1" * 32,
        ReconstructionPayloadKind.PROPOSAL: "reconstruction_proposal_" + "2" * 32,
    }[kind]
    return ReconstructionPayloadRef(
        kind=kind,
        id=identifier,
        contract_digest=_sha(kind.value + "-contract"),
        sha256=_sha(kind.value + "-payload"),
        size_bytes=128,
    )


def _profile() -> VisualProviderCapabilityProfile:
    return VisualProviderCapabilityProfile(
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
    )


def _feature(identifier: str, family: PrimitiveFamily) -> ProviderFeatureEvidence:
    points = {
        PrimitiveFamily.ROTATED_RECTANGLE: ((0.2, 0.2), (0.8, 0.2), (0.8, 0.7), (0.2, 0.7)),
        PrimitiveFamily.CIRCLE: ((0.3, 0.4), (0.4, 0.3), (0.5, 0.4)),
    }[family]
    return ProviderFeatureEvidence(
        local_feature_id=identifier,
        source_index=0,
        provider_image_id="provider_image_" + "3" * 32,
        family=family,
        points=tuple(NormalizedEvidencePoint(x=x, y=y) for x, y in points),
        localization_uncertainty_norm=0.001,
        claim_ids=("visual_claim_" + ("4" if identifier == "rectangle" else "5") * 32,),
    )


def _landmarks() -> tuple[ConfirmedPlanarLandmark, ...]:
    values = (
        ("positive-y", 0.0, 1.0, 0.0, 100.0),
        ("opposite", 1.0, 1.0, 100.0, 100.0),
        ("origin", 0.0, 0.0, 0.0, 0.0),
        ("positive-x", 1.0, 0.0, 100.0, 0.0),
    )
    return tuple(
        ConfirmedPlanarLandmark(
            landmark_id=identifier,
            confirmation_id=f"confirm-{identifier}",
            normalized_x=normalized_x,
            normalized_y=normalized_y,
            localization_uncertainty_norm=0.001,
            x_mm=x_mm,
            y_mm=y_mm,
            plane_uncertainty_mm=0.01,
        )
        for identifier, normalized_x, normalized_y, x_mm, y_mm in values
    )


def _basis(**changes: object) -> ConfirmedPlanarMetricBasis:
    values: dict[str, object] = {
        "frame_id": "front-plane",
        "confirmation_id": "confirm-basis",
        "origin_landmark_id": "origin",
        "positive_x_landmark_id": "positive-x",
        "positive_y_landmark_id": "positive-y",
    }
    values.update(changes)
    return ConfirmedPlanarMetricBasis(**values)


def _expected() -> AdmissionExpectedDigests:
    return AdmissionExpectedDigests(
        **{name: _sha(name) for name in AdmissionExpectedDigests.__dataclass_fields__}
    )


def _bundle(**changes: object) -> VisualAdmissionInputBundle:
    values: dict[str, object] = {
        "reconstruction_id": "reconstruction_" + "6" * 32,
        "base_head_sha256": _sha("base-head"),
        "observation_ref": _ref(ReconstructionPayloadKind.OBSERVATION),
        "proposal_ref": _ref(ReconstructionPayloadKind.PROPOSAL),
        "image_set_ref": AdmissionImageSetRef(
            image_set_id="image_set_" + "7" * 32,
            manifest_sha256=_sha("image-set-manifest"),
        ),
        "source_index": 0,
        "provider_profile": _profile(),
        "provider_features": (
            _feature("rectangle", PrimitiveFamily.ROTATED_RECTANGLE),
            _feature("circle", PrimitiveFamily.CIRCLE),
        ),
        "calibration_landmarks": _landmarks(),
        "metric_basis": _basis(),
        "expected": _expected(),
    }
    values.update(changes)
    return VisualAdmissionInputBundle(**values)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _resign(mapping: dict[str, object]) -> bytes:
    body = dict(mapping)
    body.pop("bundle_digest", None)
    mapping["bundle_digest"] = hashlib.sha256(
        _BUNDLE_DIGEST_DOMAIN + _canonical(body)
    ).hexdigest()
    return _canonical(mapping)


def _assert_error(
    raw: bytes,
    code: VisualAdmissionInputErrorCode,
    path: str | None = None,
) -> None:
    with pytest.raises(VisualAdmissionInputError) as exc_info:
        decode_visual_admission_inputs(raw)
    assert exc_info.value.code is code
    assert len(exc_info.value.path.encode("utf-8")) <= 256
    if path is not None:
        assert exc_info.value.path == path


def test_round_trip_is_canonical_recomputable_and_contains_no_derived_authority() -> None:
    bundle = _bundle()

    raw = encode_visual_admission_inputs(bundle)
    decoded = decode_visual_admission_inputs(raw)
    mapping = decoded.to_mapping()

    assert decoded == bundle
    assert encode_visual_admission_inputs(decoded) == raw
    assert len(raw) < MAX_VISUAL_ADMISSION_INPUT_BYTES
    assert mapping["schema_version"] == VISUAL_ADMISSION_INPUTS_SCHEMA_VERSION
    assert mapping["bundle_kind"] == VISUAL_ADMISSION_INPUTS_KIND
    assert set(mapping) == {
        "schema_version",
        "bundle_kind",
        "algorithm_suite",
        "reconstruction_id",
        "base_head_sha256",
        "observation_ref",
        "proposal_ref",
        "image_set_ref",
        "source_index",
        "provider_profile",
        "provider_features",
        "calibration_landmarks",
        "metric_basis",
        "expected",
        "bundle_digest",
    }
    forbidden = {"matrix", "receipt", "fit", "coverage_plan", "decision", "eligible"}
    assert forbidden.isdisjoint(mapping)


def test_constructor_canonicalizes_sets_but_preserves_feature_point_order() -> None:
    rectangle = _feature("rectangle", PrimitiveFamily.ROTATED_RECTANGLE)
    circle = _feature("circle", PrimitiveFamily.CIRCLE)

    bundle = _bundle(
        provider_features=(rectangle, circle),
        calibration_landmarks=tuple(reversed(_landmarks())),
    )

    assert [item.local_feature_id for item in bundle.provider_features] == ["circle", "rectangle"]
    assert [item.landmark_id for item in bundle.calibration_landmarks] == sorted(
        item.landmark_id for item in _landmarks()
    )
    restored_rectangle = bundle.provider_features[1]
    assert restored_rectangle.points == rectangle.points


@pytest.mark.parametrize(
    ("mutate", "code", "path"),
    [
        (
            lambda value: value | {"schema_version": 2},
            VisualAdmissionInputErrorCode.UNSUPPORTED_VERSION,
            "/schema_version",
        ),
        (
            lambda value: value | {"bundle_kind": "future_admission_inputs"},
            VisualAdmissionInputErrorCode.UNSUPPORTED_ALGORITHM,
            "/bundle_kind",
        ),
        (
            lambda value: value
            | {
                "algorithm_suite": value["algorithm_suite"]
                | {"calibration_algorithm": "future-homography-v2"}
            },
            VisualAdmissionInputErrorCode.UNSUPPORTED_ALGORITHM,
            "/algorithm_suite",
        ),
        (
            lambda value: value
            | {
                "algorithm_suite": value["algorithm_suite"]
                | {"calibration_schema_version": True}
            },
            VisualAdmissionInputErrorCode.UNSUPPORTED_ALGORITHM,
            "/algorithm_suite",
        ),
        (
            lambda value: value | {"unknown": 1},
            VisualAdmissionInputErrorCode.INVALID_INPUT,
            "",
        ),
        (
            lambda value: value
            | {"provider_profile": value["provider_profile"] | {"unknown": 1}},
            VisualAdmissionInputErrorCode.INVALID_INPUT,
            "/provider_profile",
        ),
    ],
)
def test_decode_rejects_unknown_versions_algorithms_and_fields(
    mutate,
    code: VisualAdmissionInputErrorCode,
    path: str,
) -> None:
    mapping = _bundle().to_mapping()

    _assert_error(_resign(mutate(mapping)), code, path)


def test_decode_rejects_duplicate_keys_before_domain_construction() -> None:
    raw = encode_visual_admission_inputs(_bundle())
    duplicated = b'{"schema_version":1,' + raw[1:]

    _assert_error(duplicated, VisualAdmissionInputErrorCode.INVALID_INPUT)


def test_decode_rejects_noncanonical_bytes_and_tampered_digest() -> None:
    raw = encode_visual_admission_inputs(_bundle())
    mapping = json.loads(raw)
    mapping["bundle_digest"] = "0" * 64

    _assert_error(
        _canonical(mapping),
        VisualAdmissionInputErrorCode.INTEGRITY_FAILURE,
        "/bundle_digest",
    )
    _assert_error(raw + b" ", VisualAdmissionInputErrorCode.INTEGRITY_FAILURE)


def test_decode_rejects_budget_nonfinite_unsafe_integer_and_bool_as_int() -> None:
    raw = encode_visual_admission_inputs(_bundle())
    mapping = json.loads(raw)
    mapping["source_index"] = False

    _assert_error(
        b" " * (MAX_VISUAL_ADMISSION_INPUT_BYTES + 1),
        VisualAdmissionInputErrorCode.BUDGET_EXCEEDED,
    )
    _assert_error(
        raw.replace(
            b'"localization_uncertainty_norm":0.001',
            b'"localization_uncertainty_norm":NaN',
            1,
        ),
        VisualAdmissionInputErrorCode.INVALID_INPUT,
    )
    _assert_error(
        raw.replace(b'"source_index":0', b'"source_index":9007199254740992', 1),
        VisualAdmissionInputErrorCode.INVALID_INPUT,
    )
    _assert_error(_resign(mapping), VisualAdmissionInputErrorCode.INVALID_INPUT, "/source_index")


def test_constructor_rejects_wrong_refs_source_binding_and_missing_basis_landmark() -> None:
    bundle = _bundle()
    wrong_source = dataclasses.replace(
        _feature("circle", PrimitiveFamily.CIRCLE),
        source_index=1,
    )
    wrong_provider_image = dataclasses.replace(
        _feature("rectangle", PrimitiveFamily.ROTATED_RECTANGLE),
        provider_image_id="provider_image_" + "8" * 32,
    )

    with pytest.raises(VisualAdmissionInputError) as wrong_ref:
        dataclasses.replace(bundle, observation_ref=bundle.proposal_ref)
    assert wrong_ref.value.code is VisualAdmissionInputErrorCode.BINDING_MISMATCH
    assert wrong_ref.value.path == "/observation_ref"

    with pytest.raises(VisualAdmissionInputError) as source_mismatch:
        dataclasses.replace(bundle, provider_features=(wrong_source,))
    assert source_mismatch.value.code is VisualAdmissionInputErrorCode.BINDING_MISMATCH
    assert source_mismatch.value.path == "/provider_features"

    with pytest.raises(VisualAdmissionInputError) as profile_mismatch:
        dataclasses.replace(bundle, source_index=1, provider_features=(wrong_source,))
    assert profile_mismatch.value.code is VisualAdmissionInputErrorCode.BINDING_MISMATCH
    assert profile_mismatch.value.path == "/source_index"

    with pytest.raises(VisualAdmissionInputError) as provider_image_mismatch:
        dataclasses.replace(
            bundle,
            provider_features=(bundle.provider_features[0], wrong_provider_image),
        )
    assert provider_image_mismatch.value.code is VisualAdmissionInputErrorCode.BINDING_MISMATCH
    assert provider_image_mismatch.value.path == "/provider_features"

    with pytest.raises(VisualAdmissionInputError) as missing_landmark:
        dataclasses.replace(
            bundle,
            metric_basis=_basis(positive_y_landmark_id="not-in-landmarks"),
        )
    assert missing_landmark.value.code is VisualAdmissionInputErrorCode.BINDING_MISMATCH
    assert missing_landmark.value.path == "/metric_basis"


def test_nested_version_and_shape_fail_closed_without_reflecting_values() -> None:
    mapping = _bundle().to_mapping()
    feature = mapping["provider_features"][0]
    feature["schema_version"] = 2

    _assert_error(
        _resign(mapping),
        VisualAdmissionInputErrorCode.UNSUPPORTED_VERSION,
        "/provider_features/0/schema_version",
    )

    mapping = _bundle().to_mapping()
    mapping["calibration_landmarks"][0]["unexpected"] = "secret-rejected-value"
    _assert_error(
        _resign(mapping),
        VisualAdmissionInputErrorCode.INVALID_INPUT,
        "/calibration_landmarks/0",
    )


def test_maximum_domain_payload_remains_within_the_192_kib_codec_budget() -> None:
    claim_ids = tuple(f"visual_claim_{index:032x}" for index in range(8))
    features = tuple(
        ProviderFeatureEvidence(
            local_feature_id=f"line-{index:02d}",
            source_index=0,
            provider_image_id="provider_image_" + "3" * 32,
            family=PrimitiveFamily.LINE,
            points=tuple(
                NormalizedEvidencePoint(x=point / 7, y=index / 63)
                for point in range(8)
            ),
            localization_uncertainty_norm=0.001,
            claim_ids=claim_ids,
        )
        for index in range(64)
    )
    landmarks = tuple(
        ConfirmedPlanarLandmark(
            landmark_id={
                (0, 0): "origin",
                (0, 7): "positive-x",
                (7, 0): "positive-y",
            }.get((row, column), f"landmark-{row}-{column}"),
            confirmation_id=f"confirm-{row}-{column}",
            normalized_x=column / 7,
            normalized_y=row / 7,
            localization_uncertainty_norm=0.001,
            x_mm=float(column),
            y_mm=float(row),
            plane_uncertainty_mm=0.01,
        )
        for row in range(8)
        for column in range(8)
    )

    raw = encode_visual_admission_inputs(
        _bundle(provider_features=features, calibration_landmarks=landmarks)
    )

    assert len(raw) < MAX_VISUAL_ADMISSION_INPUT_BYTES
    assert encode_visual_admission_inputs(decode_visual_admission_inputs(raw)) == raw


def test_encoder_requires_exact_bundle_type() -> None:
    with pytest.raises(TypeError, match="exact VisualAdmissionInputBundle"):
        encode_visual_admission_inputs(object())
