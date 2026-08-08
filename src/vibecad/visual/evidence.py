"""Private coordinate evidence bound to exact visual inputs and observations.

Provider proposals use endpoint-inclusive normalized coordinates over a full
overview image: ``(0, 0)`` is the top-left source pixel centre and ``(1, 1)``
is the bottom-right source pixel centre.  Detail crops are intentionally not
accepted because provider-image v1 does not persist their crop transform.

The resulting pixel points carry both a caller-supplied localization
uncertainty and a host-enforced resampling floor.  They remain advisory and
cannot approve geometry, mutate a Task, or adopt a candidate.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum

from vibecad.visual.contracts import MAX_IMAGE_SET_ITEMS, ImageSet
from vibecad.visual.geometry_fit import PrimitiveFamily
from vibecad.visual.metrology import PixelPoint
from vibecad.visual.provider_images import (
    ProviderImageBatch,
    ProviderImagePart,
    ProviderImagePartKind,
)
from vibecad.visual.reconstruction import VisualObservation

VISUAL_EVIDENCE_SCHEMA_VERSION = 1
MAX_EVIDENCE_FEATURES = 64
MAX_EVIDENCE_POINTS_PER_FEATURE = 64
MAX_EVIDENCE_TOTAL_POINTS = 512
MAX_EVIDENCE_CLAIMS_PER_FEATURE = 8

_MAX_SAFE_INTEGER = 2**53 - 1
_LOCAL_FEATURE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PROVIDER_IMAGE_ID = re.compile(r"^provider_image_[0-9a-f]{32}$")
_CLAIM_ID = re.compile(r"^visual_claim_[0-9a-f]{32}$")
_RECONSTRUCTION_ID = re.compile(r"^reconstruction_[0-9a-f]{32}$")
_IMAGE_SET_ID = re.compile(r"^image_set_[0-9a-f]{32}$")
_OBSERVATION_ID = re.compile(r"^visual_observation_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class VisualEvidenceErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    UNKNOWN_REFERENCE = "unknown_reference"
    BINDING_MISMATCH = "binding_mismatch"
    UNSAFE_DERIVATIVE = "unsafe_derivative"
    NUMERICAL_FAILURE = "numerical_failure"


class VisualEvidenceError(ValueError):
    """Bounded failure that never reflects rejected provider identifiers."""

    def __init__(self, code: VisualEvidenceErrorCode, path: str = "") -> None:
        if type(code) is not VisualEvidenceErrorCode:
            raise TypeError("code must be an exact VisualEvidenceErrorCode")
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


def _fail(code: VisualEvidenceErrorCode, path: str = "") -> None:
    raise VisualEvidenceError(code, path)


class EvidenceCoordinateSpace(StrEnum):
    OVERVIEW_NORMALIZED = "overview_normalized"


def _fraction(value: object, path: str, *, positive: bool = False) -> float:
    if type(value) not in {int, float}:
        _fail(VisualEvidenceErrorCode.INVALID_INPUT, path)
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0 or (positive and result <= 0.0):
        _fail(VisualEvidenceErrorCode.INVALID_INPUT, path)
    return result


def _identifier(value: object, pattern: re.Pattern[str], path: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _fail(VisualEvidenceErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class NormalizedEvidencePoint:
    x: int | float
    y: int | float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _fraction(self.x, "/x"))
        object.__setattr__(self, "y", _fraction(self.y, "/y"))


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderFeatureEvidence:
    """One provider-proposed local feature on one full overview image."""

    local_feature_id: str
    source_index: int
    provider_image_id: str
    family: PrimitiveFamily
    points: tuple[NormalizedEvidencePoint, ...]
    localization_uncertainty_norm: int | float
    claim_ids: tuple[str, ...]
    coordinate_space: EvidenceCoordinateSpace = EvidenceCoordinateSpace.OVERVIEW_NORMALIZED
    schema_version: int = VISUAL_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != VISUAL_EVIDENCE_SCHEMA_VERSION
        ):
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/schema_version")
        if (
            type(self.local_feature_id) is not str
            or _LOCAL_FEATURE_ID.fullmatch(self.local_feature_id) is None
        ):
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/local_feature_id")
        if type(self.source_index) is not int or not 0 <= self.source_index < MAX_IMAGE_SET_ITEMS:
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/source_index")
        if (
            type(self.provider_image_id) is not str
            or _PROVIDER_IMAGE_ID.fullmatch(self.provider_image_id) is None
        ):
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/provider_image_id")
        if type(self.family) is not PrimitiveFamily:
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/family")
        if type(self.points) is not tuple or not self.points:
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/points")
        if len(self.points) > MAX_EVIDENCE_POINTS_PER_FEATURE:
            _fail(VisualEvidenceErrorCode.BUDGET_EXCEEDED, "/points")
        if any(type(point) is not NormalizedEvidencePoint for point in self.points):
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/points")
        object.__setattr__(
            self,
            "localization_uncertainty_norm",
            _fraction(
                self.localization_uncertainty_norm,
                "/localization_uncertainty_norm",
                positive=True,
            ),
        )
        if type(self.claim_ids) is not tuple or not self.claim_ids:
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/claim_ids")
        if len(self.claim_ids) > MAX_EVIDENCE_CLAIMS_PER_FEATURE:
            _fail(VisualEvidenceErrorCode.BUDGET_EXCEEDED, "/claim_ids")
        if any(
            type(value) is not str or _CLAIM_ID.fullmatch(value) is None for value in self.claim_ids
        ):
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/claim_ids")
        if len(set(self.claim_ids)) != len(self.claim_ids):
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/claim_ids")
        object.__setattr__(self, "claim_ids", tuple(sorted(self.claim_ids)))
        if type(self.coordinate_space) is not EvidenceCoordinateSpace:
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/coordinate_space")


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundFeatureEvidence:
    local_feature_id: str
    source_index: int
    provider_image_id: str
    family: PrimitiveFamily
    claim_ids: tuple[str, ...]
    normalized_points: tuple[NormalizedEvidencePoint, ...]
    pixel_points: tuple[PixelPoint, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "local_feature_id",
            _identifier(self.local_feature_id, _LOCAL_FEATURE_ID, "/local_feature_id"),
        )
        if type(self.source_index) is not int or not 0 <= self.source_index < MAX_IMAGE_SET_ITEMS:
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/source_index")
        object.__setattr__(
            self,
            "provider_image_id",
            _identifier(self.provider_image_id, _PROVIDER_IMAGE_ID, "/provider_image_id"),
        )
        if type(self.family) is not PrimitiveFamily:
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/family")
        if (
            type(self.claim_ids) is not tuple
            or not self.claim_ids
            or len(self.claim_ids) > MAX_EVIDENCE_CLAIMS_PER_FEATURE
            or any(
                type(value) is not str or _CLAIM_ID.fullmatch(value) is None
                for value in self.claim_ids
            )
            or len(set(self.claim_ids)) != len(self.claim_ids)
        ):
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/claim_ids")
        object.__setattr__(self, "claim_ids", tuple(sorted(self.claim_ids)))
        if (
            type(self.normalized_points) is not tuple
            or not self.normalized_points
            or len(self.normalized_points) > MAX_EVIDENCE_POINTS_PER_FEATURE
            or any(type(point) is not NormalizedEvidencePoint for point in self.normalized_points)
        ):
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/normalized_points")
        if (
            type(self.pixel_points) is not tuple
            or len(self.pixel_points) != len(self.normalized_points)
            or any(type(point) is not PixelPoint for point in self.pixel_points)
        ):
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/pixel_points")


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundVisualEvidence:
    reconstruction_id: str
    generation: int
    image_set_id: str
    image_set_manifest_sha256: str
    image_batch_manifest_sha256: str
    observation_id: str
    observation_digest: str
    features: tuple[BoundFeatureEvidence, ...]
    schema_version: int = VISUAL_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != VISUAL_EVIDENCE_SCHEMA_VERSION
        ):
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/schema_version")
        object.__setattr__(
            self,
            "reconstruction_id",
            _identifier(self.reconstruction_id, _RECONSTRUCTION_ID, "/reconstruction_id"),
        )
        if type(self.generation) is not int or not 0 < self.generation <= _MAX_SAFE_INTEGER:
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/generation")
        object.__setattr__(
            self,
            "image_set_id",
            _identifier(self.image_set_id, _IMAGE_SET_ID, "/image_set_id"),
        )
        for field_name in (
            "image_set_manifest_sha256",
            "image_batch_manifest_sha256",
            "observation_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), _DIGEST, f"/{field_name}"),
            )
        object.__setattr__(
            self,
            "observation_id",
            _identifier(self.observation_id, _OBSERVATION_ID, "/observation_id"),
        )
        if type(self.features) is not tuple:
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/features")
        if len(self.features) > MAX_EVIDENCE_FEATURES:
            _fail(VisualEvidenceErrorCode.BUDGET_EXCEEDED, "/features")
        if any(type(feature) is not BoundFeatureEvidence for feature in self.features):
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/features")
        if (
            sum(len(feature.normalized_points) for feature in self.features)
            > MAX_EVIDENCE_TOTAL_POINTS
        ):
            _fail(VisualEvidenceErrorCode.BUDGET_EXCEEDED, "/features")
        keys = tuple((feature.source_index, feature.local_feature_id) for feature in self.features)
        if len(set(keys)) != len(keys):
            _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/features")
        object.__setattr__(
            self,
            "features",
            tuple(
                sorted(self.features, key=lambda item: (item.source_index, item.local_feature_id))
            ),
        )


def _part_by_id(batch: ProviderImageBatch) -> dict[str, ProviderImagePart]:
    return {part.id: part for part in batch.parts}


def _aspect_preserved(part: ProviderImagePart, *, source_width: int, source_height: int) -> bool:
    cross_error = abs(part.width * source_height - part.height * source_width)
    return cross_error <= max(source_width, source_height)


def _pixel_points(
    feature: ProviderFeatureEvidence,
    *,
    part: ProviderImagePart,
    source_width: int,
    source_height: int,
) -> tuple[PixelPoint, ...]:
    if min(source_width, source_height, part.width, part.height) <= 1:
        _fail(VisualEvidenceErrorCode.BINDING_MISMATCH, "/dimensions")
    source_x_span = source_width - 1
    source_y_span = source_height - 1
    sampling_floor = 0.5 * max(
        source_x_span / (part.width - 1),
        source_y_span / (part.height - 1),
    )
    declared = feature.localization_uncertainty_norm * max(source_x_span, source_y_span)
    uncertainty = max(sampling_floor, declared)
    if not math.isfinite(uncertainty):
        _fail(VisualEvidenceErrorCode.NUMERICAL_FAILURE)
    return tuple(
        PixelPoint(
            x_px=point.x * source_x_span,
            y_px=point.y * source_y_span,
            uncertainty_px=uncertainty,
        )
        for point in feature.points
    )


def bind_visual_evidence(
    *,
    observation: VisualObservation,
    image_set: ImageSet,
    image_batch: ProviderImageBatch,
    features: tuple[ProviderFeatureEvidence, ...],
) -> BoundVisualEvidence:
    """Bind provider features to exact overview parts and source pixel space."""

    if (
        type(observation) is not VisualObservation
        or type(image_set) is not ImageSet
        or type(image_batch) is not ProviderImageBatch
    ):
        _fail(VisualEvidenceErrorCode.INVALID_INPUT)
    if type(features) is not tuple:
        _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/features")
    if len(features) > MAX_EVIDENCE_FEATURES:
        _fail(VisualEvidenceErrorCode.BUDGET_EXCEEDED, "/features")
    if any(type(feature) is not ProviderFeatureEvidence for feature in features):
        _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/features")
    if sum(len(feature.points) for feature in features) > MAX_EVIDENCE_TOTAL_POINTS:
        _fail(VisualEvidenceErrorCode.BUDGET_EXCEEDED, "/features")
    local_keys = tuple((feature.source_index, feature.local_feature_id) for feature in features)
    if len(set(local_keys)) != len(local_keys):
        _fail(VisualEvidenceErrorCode.INVALID_INPUT, "/features")

    if (
        observation.image_set_id != image_set.id
        or observation.image_set_manifest_sha256 != image_set.manifest_sha256
        or image_batch.image_set_id != image_set.id
        or image_batch.image_set_manifest_sha256 != image_set.manifest_sha256
    ):
        _fail(VisualEvidenceErrorCode.BINDING_MISMATCH)
    claims = {claim.id: claim for claim in observation.claims}
    parts = _part_by_id(image_batch)
    bound: list[BoundFeatureEvidence] = []
    for index, feature in enumerate(features):
        path = f"/features/{index}"
        if feature.source_index >= len(image_set.inputs):
            _fail(VisualEvidenceErrorCode.UNKNOWN_REFERENCE, f"{path}/source_index")
        part = parts.get(feature.provider_image_id)
        if part is None:
            _fail(VisualEvidenceErrorCode.UNKNOWN_REFERENCE, f"{path}/provider_image_id")
        if part.kind is not ProviderImagePartKind.OVERVIEW or part.label is not None:
            _fail(VisualEvidenceErrorCode.UNSAFE_DERIVATIVE, f"{path}/provider_image_id")
        if part.source_index != feature.source_index:
            _fail(VisualEvidenceErrorCode.BINDING_MISMATCH, f"{path}/source_index")
        source = image_set.inputs[feature.source_index]
        if part.source_sha256 != source.original.sha256 or not _aspect_preserved(
            part,
            source_width=source.normalized.width,
            source_height=source.normalized.height,
        ):
            _fail(VisualEvidenceErrorCode.BINDING_MISMATCH, f"{path}/provider_image_id")
        for claim_id in feature.claim_ids:
            claim = claims.get(claim_id)
            if claim is None:
                _fail(VisualEvidenceErrorCode.UNKNOWN_REFERENCE, f"{path}/claim_ids")
            if feature.source_index not in claim.source_indices:
                _fail(VisualEvidenceErrorCode.BINDING_MISMATCH, f"{path}/claim_ids")
        bound.append(
            BoundFeatureEvidence(
                local_feature_id=feature.local_feature_id,
                source_index=feature.source_index,
                provider_image_id=feature.provider_image_id,
                family=feature.family,
                claim_ids=feature.claim_ids,
                normalized_points=feature.points,
                pixel_points=_pixel_points(
                    feature,
                    part=part,
                    source_width=source.normalized.width,
                    source_height=source.normalized.height,
                ),
            )
        )
    return BoundVisualEvidence(
        reconstruction_id=observation.reconstruction_id,
        generation=observation.generation,
        image_set_id=image_set.id,
        image_set_manifest_sha256=image_set.manifest_sha256,
        image_batch_manifest_sha256=image_batch.manifest_sha256,
        observation_id=observation.id,
        observation_digest=observation.digest,
        features=tuple(sorted(bound, key=lambda item: (item.source_index, item.local_feature_id))),
    )


__all__ = [
    "MAX_EVIDENCE_CLAIMS_PER_FEATURE",
    "MAX_EVIDENCE_FEATURES",
    "MAX_EVIDENCE_POINTS_PER_FEATURE",
    "MAX_EVIDENCE_TOTAL_POINTS",
    "VISUAL_EVIDENCE_SCHEMA_VERSION",
    "BoundFeatureEvidence",
    "BoundVisualEvidence",
    "EvidenceCoordinateSpace",
    "NormalizedEvidencePoint",
    "ProviderFeatureEvidence",
    "VisualEvidenceError",
    "VisualEvidenceErrorCode",
    "bind_visual_evidence",
]
