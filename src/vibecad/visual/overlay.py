"""Private vector overlay plans for bound visual coordinate evidence.

The plan contains normalized vectors only; it does not decode images, render a
canvas, or grant reconstruction authority.  Unordered line/circle evidence is
shown as landmarks.  Arc and rectangle evidence may connect points because
their order is already semantic in the evidence contract.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum

from vibecad.visual.contracts import MAX_IMAGE_SET_ITEMS, ImageSet
from vibecad.visual.evidence import (
    MAX_EVIDENCE_CLAIMS_PER_FEATURE,
    MAX_EVIDENCE_FEATURES,
    MAX_EVIDENCE_POINTS_PER_FEATURE,
    MAX_EVIDENCE_TOTAL_POINTS,
    BoundFeatureEvidence,
    BoundVisualEvidence,
    NormalizedEvidencePoint,
)
from vibecad.visual.geometry_fit import PrimitiveFamily

VISUAL_OVERLAY_SCHEMA_VERSION = 1
MAX_OVERLAY_ITEMS = MAX_EVIDENCE_FEATURES
MAX_OVERLAY_TOTAL_POINTS = MAX_EVIDENCE_TOTAL_POINTS

_LOCAL_FEATURE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CLAIM_ID = re.compile(r"^visual_claim_[0-9a-f]{32}$")
_IMAGE_SET_ID = re.compile(r"^image_set_[0-9a-f]{32}$")
_OBSERVATION_ID = re.compile(r"^visual_observation_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class VisualOverlayErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    BINDING_MISMATCH = "binding_mismatch"
    NUMERICAL_FAILURE = "numerical_failure"


class VisualOverlayError(ValueError):
    """Bounded overlay failure with no reflected provider values."""

    def __init__(self, code: VisualOverlayErrorCode, path: str = "") -> None:
        if type(code) is not VisualOverlayErrorCode:
            raise TypeError("code must be an exact VisualOverlayErrorCode")
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


def _fail(code: VisualOverlayErrorCode, path: str = "") -> None:
    raise VisualOverlayError(code, path)


class OverlayGeometryKind(StrEnum):
    LANDMARK_POINTS = "landmark_points"
    ORDERED_POLYLINE = "ordered_polyline"
    CLOSED_POLYGON = "closed_polygon"


class OverlayEvidenceStatus(StrEnum):
    PROVIDER_PROPOSED = "provider_proposed"


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceOverlayItem:
    source_index: int
    local_feature_id: str
    family: PrimitiveFamily
    geometry_kind: OverlayGeometryKind
    evidence_status: OverlayEvidenceStatus
    claim_ids: tuple[str, ...]
    points: tuple[NormalizedEvidencePoint, ...]
    uncertainty_radius_norm: float

    def __post_init__(self) -> None:
        if type(self.source_index) is not int or not 0 <= self.source_index < MAX_IMAGE_SET_ITEMS:
            _fail(VisualOverlayErrorCode.INVALID_INPUT, "/source_index")
        if (
            type(self.local_feature_id) is not str
            or _LOCAL_FEATURE_ID.fullmatch(self.local_feature_id) is None
        ):
            _fail(VisualOverlayErrorCode.INVALID_INPUT, "/local_feature_id")
        if type(self.family) is not PrimitiveFamily:
            _fail(VisualOverlayErrorCode.INVALID_INPUT, "/family")
        if (
            type(self.geometry_kind) is not OverlayGeometryKind
            or self.geometry_kind is not _geometry_kind(self.family)
            or self.evidence_status is not OverlayEvidenceStatus.PROVIDER_PROPOSED
        ):
            _fail(VisualOverlayErrorCode.INVALID_INPUT, "/geometry_kind")
        if (
            type(self.claim_ids) is not tuple
            or not 1 <= len(self.claim_ids) <= MAX_EVIDENCE_CLAIMS_PER_FEATURE
            or len(set(self.claim_ids)) != len(self.claim_ids)
            or any(
                type(item) is not str or _CLAIM_ID.fullmatch(item) is None
                for item in self.claim_ids
            )
        ):
            _fail(VisualOverlayErrorCode.INVALID_INPUT, "/claim_ids")
        object.__setattr__(self, "claim_ids", tuple(sorted(self.claim_ids)))
        if (
            type(self.points) is not tuple
            or not 1 <= len(self.points) <= MAX_EVIDENCE_POINTS_PER_FEATURE
            or any(type(item) is not NormalizedEvidencePoint for item in self.points)
        ):
            _fail(VisualOverlayErrorCode.INVALID_INPUT, "/points")
        if type(self.uncertainty_radius_norm) not in {int, float}:
            _fail(VisualOverlayErrorCode.INVALID_INPUT, "/uncertainty_radius_norm")
        uncertainty = float(self.uncertainty_radius_norm)
        if not math.isfinite(uncertainty) or not 0.0 < uncertainty <= 1.0:
            _fail(VisualOverlayErrorCode.INVALID_INPUT, "/uncertainty_radius_norm")
        object.__setattr__(self, "uncertainty_radius_norm", uncertainty)


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualOverlayPlan:
    image_set_id: str
    image_set_manifest_sha256: str
    image_batch_manifest_sha256: str
    observation_id: str
    observation_digest: str
    items: tuple[EvidenceOverlayItem, ...]
    schema_version: int = VISUAL_OVERLAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != VISUAL_OVERLAY_SCHEMA_VERSION
        ):
            _fail(VisualOverlayErrorCode.INVALID_INPUT, "/schema_version")
        if type(self.image_set_id) is not str or _IMAGE_SET_ID.fullmatch(self.image_set_id) is None:
            _fail(VisualOverlayErrorCode.INVALID_INPUT, "/image_set_id")
        if (
            type(self.observation_id) is not str
            or _OBSERVATION_ID.fullmatch(self.observation_id) is None
        ):
            _fail(VisualOverlayErrorCode.INVALID_INPUT, "/observation_id")
        for name in (
            "image_set_manifest_sha256",
            "image_batch_manifest_sha256",
            "observation_digest",
        ):
            value = getattr(self, name)
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                _fail(VisualOverlayErrorCode.INVALID_INPUT, f"/{name}")
        if (
            type(self.items) is not tuple
            or len(self.items) > MAX_OVERLAY_ITEMS
            or any(type(item) is not EvidenceOverlayItem for item in self.items)
            or sum(len(item.points) for item in self.items) > MAX_OVERLAY_TOTAL_POINTS
        ):
            _fail(VisualOverlayErrorCode.INVALID_INPUT, "/items")
        keys = tuple((item.source_index, item.local_feature_id) for item in self.items)
        if len(set(keys)) != len(keys):
            _fail(VisualOverlayErrorCode.INVALID_INPUT, "/items")
        object.__setattr__(
            self,
            "items",
            tuple(sorted(self.items, key=lambda item: (item.source_index, item.local_feature_id))),
        )


def _geometry_kind(family: PrimitiveFamily) -> OverlayGeometryKind:
    if family in {PrimitiveFamily.LINE, PrimitiveFamily.CIRCLE}:
        return OverlayGeometryKind.LANDMARK_POINTS
    if family is PrimitiveFamily.ARC:
        return OverlayGeometryKind.ORDERED_POLYLINE
    return OverlayGeometryKind.CLOSED_POLYGON


def _overlay_item(feature: BoundFeatureEvidence, image_set: ImageSet) -> EvidenceOverlayItem:
    if feature.source_index >= len(image_set.inputs):
        _fail(VisualOverlayErrorCode.BINDING_MISMATCH, "/source_index")
    source = image_set.inputs[feature.source_index].normalized
    span = max(source.width - 1, source.height - 1)
    if span <= 0 or len(feature.normalized_points) != len(feature.pixel_points):
        _fail(VisualOverlayErrorCode.BINDING_MISMATCH)
    uncertainty = max(point.uncertainty_px for point in feature.pixel_points) / span
    if not math.isfinite(uncertainty) or not 0.0 < uncertainty <= 1.0:
        _fail(VisualOverlayErrorCode.NUMERICAL_FAILURE)
    return EvidenceOverlayItem(
        source_index=feature.source_index,
        local_feature_id=feature.local_feature_id,
        family=feature.family,
        geometry_kind=_geometry_kind(feature.family),
        evidence_status=OverlayEvidenceStatus.PROVIDER_PROPOSED,
        claim_ids=feature.claim_ids,
        points=feature.normalized_points,
        uncertainty_radius_norm=uncertainty,
    )


def build_evidence_overlay(
    evidence: BoundVisualEvidence,
    image_set: ImageSet,
) -> VisualOverlayPlan:
    """Build one bounded source-normalized overlay from exact bound evidence."""

    if type(evidence) is not BoundVisualEvidence or type(image_set) is not ImageSet:
        _fail(VisualOverlayErrorCode.INVALID_INPUT)
    if len(evidence.features) > MAX_OVERLAY_ITEMS:
        _fail(VisualOverlayErrorCode.BUDGET_EXCEEDED, "/features")
    if any(type(feature) is not BoundFeatureEvidence for feature in evidence.features):
        _fail(VisualOverlayErrorCode.INVALID_INPUT, "/features")
    if (
        sum(len(feature.normalized_points) for feature in evidence.features)
        > MAX_OVERLAY_TOTAL_POINTS
    ):
        _fail(VisualOverlayErrorCode.BUDGET_EXCEEDED, "/features")
    if (
        evidence.image_set_id != image_set.id
        or evidence.image_set_manifest_sha256 != image_set.manifest_sha256
    ):
        _fail(VisualOverlayErrorCode.BINDING_MISMATCH)
    items = tuple(_overlay_item(feature, image_set) for feature in evidence.features)
    return VisualOverlayPlan(
        image_set_id=image_set.id,
        image_set_manifest_sha256=image_set.manifest_sha256,
        image_batch_manifest_sha256=evidence.image_batch_manifest_sha256,
        observation_id=evidence.observation_id,
        observation_digest=evidence.observation_digest,
        items=items,
    )


__all__ = [
    "MAX_OVERLAY_ITEMS",
    "MAX_OVERLAY_TOTAL_POINTS",
    "VISUAL_OVERLAY_SCHEMA_VERSION",
    "EvidenceOverlayItem",
    "OverlayEvidenceStatus",
    "OverlayGeometryKind",
    "VisualOverlayError",
    "VisualOverlayErrorCode",
    "VisualOverlayPlan",
    "build_evidence_overlay",
]
