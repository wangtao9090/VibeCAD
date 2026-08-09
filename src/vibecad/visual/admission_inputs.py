"""Private canonical codec for restartable ordinary-photo admission inputs.

The bundle persists only raw, recomputable inputs and expected digests.  It
does not persist a calibration matrix, fit result, coverage plan, evaluator
decision, or any Task/adoption authority.  A later application boundary must
reload the sealed inputs and deterministically rebuild every expected value.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum

from vibecad.visual.calibration_authority import (
    CALIBRATION_AUTHORITY_SCHEMA_VERSION,
    MIN_CALIBRATION_LANDMARKS,
    PLANAR_CALIBRATION_ALGORITHM,
    ConfirmedPlanarLandmark,
    ConfirmedPlanarMetricBasis,
)
from vibecad.visual.capture_quality import CAPTURE_QUALITY_SCHEMA_VERSION
from vibecad.visual.contracts import MAX_IMAGE_SET_ITEMS
from vibecad.visual.drafts import (
    ReconstructionPayloadKind,
    ReconstructionPayloadRef,
)
from vibecad.visual.evidence import (
    MAX_EVIDENCE_FEATURES,
    MAX_EVIDENCE_TOTAL_POINTS,
    VISUAL_EVIDENCE_SCHEMA_VERSION,
    EvidenceCoordinateSpace,
    NormalizedEvidencePoint,
    ProviderFeatureEvidence,
)
from vibecad.visual.fit_pipeline import VISUAL_FIT_PIPELINE_SCHEMA_VERSION
from vibecad.visual.geometry_fit import GEOMETRY_FIT_SCHEMA_VERSION, PrimitiveFamily
from vibecad.visual.metrology import MAX_CALIBRATION_LANDMARKS
from vibecad.visual.proposal_coverage import PROPOSAL_COVERAGE_SCHEMA_VERSION
from vibecad.visual.provider_images import (
    PROVIDER_IMAGE_SCHEMA_VERSION,
    ProviderImageDetail,
    VisualProviderCapabilityProfile,
)

VISUAL_ADMISSION_INPUTS_SCHEMA_VERSION = 1
VISUAL_ADMISSION_INPUTS_KIND = "ordinary_photo_admission_inputs"
VISUAL_ADMISSION_EVALUATOR_ALGORITHM = "ordinary-photo-evidence-evaluator-v1"
VISUAL_ADMISSION_EVALUATOR_SCHEMA_VERSION = 1
MAX_VISUAL_ADMISSION_INPUT_BYTES = 192 * 1024

_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 8192
_MAX_JSON_STRING_BYTES = 64 * 1024
_MAX_ERROR_PATH_BYTES = 256
_BUNDLE_DIGEST_DOMAIN = b"vibecad-visual-admission-inputs-v1\0"

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RECONSTRUCTION_ID = re.compile(r"^reconstruction_[0-9a-f]{32}$")
_IMAGE_SET_ID = re.compile(r"^image_set_[0-9a-f]{32}$")

_TOP_LEVEL_FIELDS = {
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
_ALGORITHM_FIELDS = {
    "evaluator_algorithm",
    "evaluator_schema_version",
    "provider_image_schema_version",
    "visual_evidence_schema_version",
    "fit_pipeline_schema_version",
    "geometry_fit_schema_version",
    "capture_quality_schema_version",
    "calibration_schema_version",
    "calibration_algorithm",
    "proposal_coverage_schema_version",
}
_PROFILE_FIELDS = {
    "schema_version",
    "provider",
    "model",
    "model_version",
    "data_policy_profile",
    "max_source_images",
    "max_image_parts",
    "max_image_bytes",
    "max_batch_image_bytes",
    "preferred_long_edge",
    "max_long_edge",
    "detail",
    "supports_detail_crops",
    "transport_timeout_ms",
}
_FEATURE_FIELDS = {
    "schema_version",
    "local_feature_id",
    "source_index",
    "provider_image_id",
    "family",
    "points",
    "localization_uncertainty_norm",
    "claim_ids",
    "coordinate_space",
}
_POINT_FIELDS = {"x", "y"}
_LANDMARK_FIELDS = {
    "schema_version",
    "landmark_id",
    "confirmation_id",
    "normalized_x",
    "normalized_y",
    "localization_uncertainty_norm",
    "x_mm",
    "y_mm",
    "plane_uncertainty_mm",
}
_BASIS_FIELDS = {
    "schema_version",
    "frame_id",
    "confirmation_id",
    "origin_landmark_id",
    "positive_x_landmark_id",
    "positive_y_landmark_id",
    "unit",
}
_IMAGE_SET_REF_FIELDS = {"image_set_id", "manifest_sha256"}
_EXPECTED_FIELDS = {
    "provider_batch_manifest_sha256",
    "calibration_receipt_sha256",
    "calibration_authority_binding_sha256",
    "calibration_sha256",
    "capture_quality_sha256",
    "evidence_sha256",
    "fit_report_sha256",
    "evaluation_report_sha256",
    "coverage_plan_sha256",
    "expected_operation_payload_sha256",
}


class VisualAdmissionInputErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNSUPPORTED_ALGORITHM = "unsupported_algorithm"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTEGRITY_FAILURE = "integrity_failure"
    BINDING_MISMATCH = "binding_mismatch"


class VisualAdmissionInputError(ValueError):
    """Bounded failure that never reflects rejected persisted contents."""

    def __init__(self, code: VisualAdmissionInputErrorCode, path: str = "") -> None:
        if type(code) is not VisualAdmissionInputErrorCode:
            raise TypeError("code must be an exact VisualAdmissionInputErrorCode")
        if type(path) is not str:
            raise TypeError("path must be a string")
        try:
            encoded = path.encode("utf-8")
        except UnicodeError:
            raise ValueError("path must be bounded") from None
        if len(encoded) > _MAX_ERROR_PATH_BYTES:
            raise ValueError("path must be bounded")
        self.code = code
        self.path = path
        super().__init__(code.value)


def _fail(code: VisualAdmissionInputErrorCode, path: str = "") -> None:
    raise VisualAdmissionInputError(code, path)


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, path)
    return value


def _identifier(value: object, pattern: re.Pattern[str], path: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, path)
    return value


def _safe_integer(value: object, path: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SAFE_INTEGER:
        _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, path)
    return value


def _exact_mapping(value: object, fields: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, path)
    return value


def _exact_list(value: object, path: str) -> list[object]:
    if type(value) is not list:
        _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, path)
    return value


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
        _fail(VisualAdmissionInputErrorCode.INVALID_INPUT)
    if not raw or len(raw) > MAX_VISUAL_ADMISSION_INPUT_BYTES:
        _fail(VisualAdmissionInputErrorCode.BUDGET_EXCEEDED)
    return raw


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _parse_int(value: str) -> int:
    converted = int(value)
    if not -_MAX_SAFE_INTEGER <= converted <= _MAX_SAFE_INTEGER:
        raise ValueError
    return converted


def _parse_float(value: str) -> float:
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError
    return converted


def _reject_constant(_value: str) -> None:
    raise ValueError


def _validate_json_tree(value: object) -> None:
    nodes = 0

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            _fail(VisualAdmissionInputErrorCode.BUDGET_EXCEEDED)
        if depth > _MAX_JSON_DEPTH:
            _fail(VisualAdmissionInputErrorCode.BUDGET_EXCEEDED)
        if type(item) is str:
            try:
                size = len(item.encode("utf-8"))
            except UnicodeError:
                _fail(VisualAdmissionInputErrorCode.INVALID_INPUT)
            if size > _MAX_JSON_STRING_BYTES:
                _fail(VisualAdmissionInputErrorCode.BUDGET_EXCEEDED)
        elif type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    _fail(VisualAdmissionInputErrorCode.INVALID_INPUT)
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif type(item) is list:
            for child in item:
                visit(child, depth + 1)
        elif type(item) not in {int, float, bool, type(None)}:
            _fail(VisualAdmissionInputErrorCode.INVALID_INPUT)

    visit(value, 0)


def _decode_json(raw: object) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_VISUAL_ADMISSION_INPUT_BYTES:
        code = (
            VisualAdmissionInputErrorCode.BUDGET_EXCEEDED
            if type(raw) is bytes and len(raw) > MAX_VISUAL_ADMISSION_INPUT_BYTES
            else VisualAdmissionInputErrorCode.INVALID_INPUT
        )
        _fail(code)
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_int=_parse_int,
            parse_float=_parse_float,
            parse_constant=_reject_constant,
        )
    except (_DuplicateKeyError, UnicodeDecodeError, ValueError, RecursionError):
        _fail(VisualAdmissionInputErrorCode.INVALID_INPUT)
    _validate_json_tree(value)
    if type(value) is not dict:
        _fail(VisualAdmissionInputErrorCode.INVALID_INPUT)
    return value


def _algorithm_mapping() -> dict[str, object]:
    return {
        "evaluator_algorithm": VISUAL_ADMISSION_EVALUATOR_ALGORITHM,
        "evaluator_schema_version": VISUAL_ADMISSION_EVALUATOR_SCHEMA_VERSION,
        "provider_image_schema_version": PROVIDER_IMAGE_SCHEMA_VERSION,
        "visual_evidence_schema_version": VISUAL_EVIDENCE_SCHEMA_VERSION,
        "fit_pipeline_schema_version": VISUAL_FIT_PIPELINE_SCHEMA_VERSION,
        "geometry_fit_schema_version": GEOMETRY_FIT_SCHEMA_VERSION,
        "capture_quality_schema_version": CAPTURE_QUALITY_SCHEMA_VERSION,
        "calibration_schema_version": CALIBRATION_AUTHORITY_SCHEMA_VERSION,
        "calibration_algorithm": PLANAR_CALIBRATION_ALGORITHM,
        "proposal_coverage_schema_version": PROPOSAL_COVERAGE_SCHEMA_VERSION,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class AdmissionImageSetRef:
    image_set_id: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "image_set_id",
            _identifier(self.image_set_id, _IMAGE_SET_ID, "/image_set_ref/image_set_id"),
        )
        object.__setattr__(
            self,
            "manifest_sha256",
            _digest(self.manifest_sha256, "/image_set_ref/manifest_sha256"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "image_set_id": self.image_set_id,
            "manifest_sha256": self.manifest_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AdmissionExpectedDigests:
    provider_batch_manifest_sha256: str
    calibration_receipt_sha256: str
    calibration_authority_binding_sha256: str
    calibration_sha256: str
    capture_quality_sha256: str
    evidence_sha256: str
    fit_report_sha256: str
    evaluation_report_sha256: str
    coverage_plan_sha256: str
    expected_operation_payload_sha256: str

    def __post_init__(self) -> None:
        for name in _EXPECTED_FIELDS:
            object.__setattr__(self, name, _digest(getattr(self, name), f"/expected/{name}"))

    def to_mapping(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in sorted(_EXPECTED_FIELDS)}


def _feature_mapping(value: ProviderFeatureEvidence) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "local_feature_id": value.local_feature_id,
        "source_index": value.source_index,
        "provider_image_id": value.provider_image_id,
        "family": value.family.value,
        "points": [{"x": point.x, "y": point.y} for point in value.points],
        "localization_uncertainty_norm": value.localization_uncertainty_norm,
        "claim_ids": list(value.claim_ids),
        "coordinate_space": value.coordinate_space.value,
    }


def _landmark_mapping(value: ConfirmedPlanarLandmark) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "landmark_id": value.landmark_id,
        "confirmation_id": value.confirmation_id,
        "normalized_x": value.normalized_x,
        "normalized_y": value.normalized_y,
        "localization_uncertainty_norm": value.localization_uncertainty_norm,
        "x_mm": value.x_mm,
        "y_mm": value.y_mm,
        "plane_uncertainty_mm": value.plane_uncertainty_mm,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualAdmissionInputBundle:
    reconstruction_id: str
    base_head_sha256: str
    observation_ref: ReconstructionPayloadRef
    proposal_ref: ReconstructionPayloadRef
    image_set_ref: AdmissionImageSetRef
    source_index: int
    provider_profile: VisualProviderCapabilityProfile
    provider_features: tuple[ProviderFeatureEvidence, ...]
    calibration_landmarks: tuple[ConfirmedPlanarLandmark, ...]
    metric_basis: ConfirmedPlanarMetricBasis
    expected: AdmissionExpectedDigests
    bundle_digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reconstruction_id",
            _identifier(self.reconstruction_id, _RECONSTRUCTION_ID, "/reconstruction_id"),
        )
        object.__setattr__(
            self,
            "base_head_sha256",
            _digest(self.base_head_sha256, "/base_head_sha256"),
        )
        if (
            type(self.observation_ref) is not ReconstructionPayloadRef
            or self.observation_ref.kind is not ReconstructionPayloadKind.OBSERVATION
        ):
            _fail(VisualAdmissionInputErrorCode.BINDING_MISMATCH, "/observation_ref")
        if (
            type(self.proposal_ref) is not ReconstructionPayloadRef
            or self.proposal_ref.kind is not ReconstructionPayloadKind.PROPOSAL
        ):
            _fail(VisualAdmissionInputErrorCode.BINDING_MISMATCH, "/proposal_ref")
        if type(self.image_set_ref) is not AdmissionImageSetRef:
            _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, "/image_set_ref")
        source_index = _safe_integer(self.source_index, "/source_index")
        if source_index >= MAX_IMAGE_SET_ITEMS:
            _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, "/source_index")
        if type(self.provider_profile) is not VisualProviderCapabilityProfile:
            _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, "/provider_profile")
        if source_index >= self.provider_profile.max_source_images:
            _fail(VisualAdmissionInputErrorCode.BINDING_MISMATCH, "/source_index")
        if (
            type(self.provider_features) is not tuple
            or not self.provider_features
            or len(self.provider_features) > MAX_EVIDENCE_FEATURES
            or any(type(item) is not ProviderFeatureEvidence for item in self.provider_features)
        ):
            _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, "/provider_features")
        features = tuple(
            sorted(
                self.provider_features,
                key=lambda item: (item.source_index, item.local_feature_id),
            )
        )
        feature_keys = tuple((item.source_index, item.local_feature_id) for item in features)
        if len(feature_keys) != len(set(feature_keys)):
            _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, "/provider_features")
        if any(item.source_index != source_index for item in features):
            _fail(VisualAdmissionInputErrorCode.BINDING_MISMATCH, "/provider_features")
        if len({item.provider_image_id for item in features}) != 1:
            _fail(VisualAdmissionInputErrorCode.BINDING_MISMATCH, "/provider_features")
        if sum(len(item.points) for item in features) > MAX_EVIDENCE_TOTAL_POINTS:
            _fail(VisualAdmissionInputErrorCode.BUDGET_EXCEEDED, "/provider_features")
        object.__setattr__(self, "provider_features", features)
        if (
            type(self.calibration_landmarks) is not tuple
            or not MIN_CALIBRATION_LANDMARKS
            <= len(self.calibration_landmarks)
            <= MAX_CALIBRATION_LANDMARKS
            or any(
                type(item) is not ConfirmedPlanarLandmark
                for item in self.calibration_landmarks
            )
        ):
            _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, "/calibration_landmarks")
        landmarks = tuple(sorted(self.calibration_landmarks, key=lambda item: item.landmark_id))
        landmark_ids = tuple(item.landmark_id for item in landmarks)
        if len(landmark_ids) != len(set(landmark_ids)):
            _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, "/calibration_landmarks")
        object.__setattr__(self, "calibration_landmarks", landmarks)
        if type(self.metric_basis) is not ConfirmedPlanarMetricBasis:
            _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, "/metric_basis")
        basis_landmarks = {
            self.metric_basis.origin_landmark_id,
            self.metric_basis.positive_x_landmark_id,
            self.metric_basis.positive_y_landmark_id,
        }
        if not basis_landmarks.issubset(set(landmark_ids)):
            _fail(VisualAdmissionInputErrorCode.BINDING_MISMATCH, "/metric_basis")
        if type(self.expected) is not AdmissionExpectedDigests:
            _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, "/expected")
        digest = hashlib.sha256(
            _BUNDLE_DIGEST_DOMAIN + _canonical_json(self._body_mapping())
        ).hexdigest()
        object.__setattr__(self, "bundle_digest", digest)
        _canonical_json(self.to_mapping())

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": VISUAL_ADMISSION_INPUTS_SCHEMA_VERSION,
            "bundle_kind": VISUAL_ADMISSION_INPUTS_KIND,
            "algorithm_suite": _algorithm_mapping(),
            "reconstruction_id": self.reconstruction_id,
            "base_head_sha256": self.base_head_sha256,
            "observation_ref": self.observation_ref.to_mapping(),
            "proposal_ref": self.proposal_ref.to_mapping(),
            "image_set_ref": self.image_set_ref.to_mapping(),
            "source_index": self.source_index,
            "provider_profile": self.provider_profile.to_mapping(),
            "provider_features": [_feature_mapping(item) for item in self.provider_features],
            "calibration_landmarks": [
                _landmark_mapping(item) for item in self.calibration_landmarks
            ],
            "metric_basis": self.metric_basis.to_mapping(),
            "expected": self.expected.to_mapping(),
        }

    def to_mapping(self) -> dict[str, object]:
        return self._body_mapping() | {"bundle_digest": self.bundle_digest}


def encode_visual_admission_inputs(value: object) -> bytes:
    if type(value) is not VisualAdmissionInputBundle:
        raise TypeError("value must be an exact VisualAdmissionInputBundle")
    return _canonical_json(value.to_mapping())


def _decode_profile(value: object) -> VisualProviderCapabilityProfile:
    data = _exact_mapping(value, _PROFILE_FIELDS, "/provider_profile")
    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != PROVIDER_IMAGE_SCHEMA_VERSION
    ):
        _fail(VisualAdmissionInputErrorCode.UNSUPPORTED_VERSION, "/provider_profile/schema_version")
    try:
        detail = ProviderImageDetail(data["detail"])
        return VisualProviderCapabilityProfile(**(data | {"detail": detail}))
    except (TypeError, ValueError):
        _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, "/provider_profile")


def _decode_feature(value: object, index: int) -> ProviderFeatureEvidence:
    path = f"/provider_features/{index}"
    data = _exact_mapping(value, _FEATURE_FIELDS, path)
    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != VISUAL_EVIDENCE_SCHEMA_VERSION
    ):
        _fail(VisualAdmissionInputErrorCode.UNSUPPORTED_VERSION, path + "/schema_version")
    points = _exact_list(data["points"], path + "/points")
    claims = _exact_list(data["claim_ids"], path + "/claim_ids")
    try:
        decoded_points = tuple(
            NormalizedEvidencePoint(
                **_exact_mapping(point, _POINT_FIELDS, path + f"/points/{point_index}")
            )
            for point_index, point in enumerate(points)
        )
        return ProviderFeatureEvidence(
            schema_version=data["schema_version"],
            local_feature_id=data["local_feature_id"],
            source_index=data["source_index"],
            provider_image_id=data["provider_image_id"],
            family=PrimitiveFamily(data["family"]),
            points=decoded_points,
            localization_uncertainty_norm=data["localization_uncertainty_norm"],
            claim_ids=tuple(claims),
            coordinate_space=EvidenceCoordinateSpace(data["coordinate_space"]),
        )
    except VisualAdmissionInputError:
        raise
    except (TypeError, ValueError):
        _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, path)


def _decode_landmark(value: object, index: int) -> ConfirmedPlanarLandmark:
    path = f"/calibration_landmarks/{index}"
    data = _exact_mapping(value, _LANDMARK_FIELDS, path)
    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != CALIBRATION_AUTHORITY_SCHEMA_VERSION
    ):
        _fail(VisualAdmissionInputErrorCode.UNSUPPORTED_VERSION, path + "/schema_version")
    try:
        return ConfirmedPlanarLandmark(**data)
    except (TypeError, ValueError):
        _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, path)


def _decode_basis(value: object) -> ConfirmedPlanarMetricBasis:
    data = _exact_mapping(value, _BASIS_FIELDS, "/metric_basis")
    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != CALIBRATION_AUTHORITY_SCHEMA_VERSION
    ):
        _fail(VisualAdmissionInputErrorCode.UNSUPPORTED_VERSION, "/metric_basis/schema_version")
    try:
        return ConfirmedPlanarMetricBasis(**data)
    except (TypeError, ValueError):
        _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, "/metric_basis")


def _decode_expected(value: object) -> AdmissionExpectedDigests:
    data = _exact_mapping(value, _EXPECTED_FIELDS, "/expected")
    try:
        return AdmissionExpectedDigests(**data)
    except (TypeError, ValueError):
        _fail(VisualAdmissionInputErrorCode.INVALID_INPUT, "/expected")


def decode_visual_admission_inputs(raw: object) -> VisualAdmissionInputBundle:
    mapping = _exact_mapping(_decode_json(raw), _TOP_LEVEL_FIELDS, "")
    if (
        type(mapping["schema_version"]) is not int
        or mapping["schema_version"] != VISUAL_ADMISSION_INPUTS_SCHEMA_VERSION
    ):
        _fail(VisualAdmissionInputErrorCode.UNSUPPORTED_VERSION, "/schema_version")
    if (
        type(mapping["bundle_kind"]) is not str
        or mapping["bundle_kind"] != VISUAL_ADMISSION_INPUTS_KIND
    ):
        _fail(VisualAdmissionInputErrorCode.UNSUPPORTED_ALGORITHM, "/bundle_kind")
    algorithms = _exact_mapping(mapping["algorithm_suite"], _ALGORITHM_FIELDS, "/algorithm_suite")
    expected_algorithms = _algorithm_mapping()
    if any(
        type(algorithms[name]) is not type(expected_algorithms[name])
        or algorithms[name] != expected_algorithms[name]
        for name in _ALGORITHM_FIELDS
    ):
        _fail(VisualAdmissionInputErrorCode.UNSUPPORTED_ALGORITHM, "/algorithm_suite")
    image_set_data = _exact_mapping(
        mapping["image_set_ref"],
        _IMAGE_SET_REF_FIELDS,
        "/image_set_ref",
    )
    features = _exact_list(mapping["provider_features"], "/provider_features")
    landmarks = _exact_list(mapping["calibration_landmarks"], "/calibration_landmarks")
    try:
        observation_ref = ReconstructionPayloadRef.from_mapping(mapping["observation_ref"])
        proposal_ref = ReconstructionPayloadRef.from_mapping(mapping["proposal_ref"])
        result = VisualAdmissionInputBundle(
            reconstruction_id=mapping["reconstruction_id"],
            base_head_sha256=mapping["base_head_sha256"],
            observation_ref=observation_ref,
            proposal_ref=proposal_ref,
            image_set_ref=AdmissionImageSetRef(**image_set_data),
            source_index=mapping["source_index"],
            provider_profile=_decode_profile(mapping["provider_profile"]),
            provider_features=tuple(
                _decode_feature(item, index) for index, item in enumerate(features)
            ),
            calibration_landmarks=tuple(
                _decode_landmark(item, index) for index, item in enumerate(landmarks)
            ),
            metric_basis=_decode_basis(mapping["metric_basis"]),
            expected=_decode_expected(mapping["expected"]),
        )
    except VisualAdmissionInputError:
        raise
    except (TypeError, ValueError):
        _fail(VisualAdmissionInputErrorCode.INVALID_INPUT)
    supplied_digest = _digest(mapping["bundle_digest"], "/bundle_digest")
    if not hmac.compare_digest(result.bundle_digest, supplied_digest):
        _fail(VisualAdmissionInputErrorCode.INTEGRITY_FAILURE, "/bundle_digest")
    if encode_visual_admission_inputs(result) != raw:
        _fail(VisualAdmissionInputErrorCode.INTEGRITY_FAILURE)
    return result


__all__: tuple[str, ...] = ()
