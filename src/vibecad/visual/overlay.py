"""Private vector overlay plans for bound visual coordinate evidence.

The plan contains normalized vectors only; it does not decode images, render a
canvas, or grant reconstruction authority.  Unordered line/circle evidence is
shown as landmarks.  Arc and rectangle evidence may connect points because
their order is already semantic in the evidence contract.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from vibecad.visual.contracts import ImageSet
from vibecad.visual.evidence import (
    MAX_EVIDENCE_FEATURES,
    MAX_EVIDENCE_TOTAL_POINTS,
    BoundFeatureEvidence,
    BoundVisualEvidence,
    NormalizedEvidencePoint,
)
from vibecad.visual.geometry_fit import PrimitiveFamily

VISUAL_OVERLAY_SCHEMA_VERSION = 1
MAX_OVERLAY_ITEMS = MAX_EVIDENCE_FEATURES
MAX_OVERLAY_TOTAL_POINTS = MAX_EVIDENCE_TOTAL_POINTS


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


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualOverlayPlan:
    image_set_id: str
    image_set_manifest_sha256: str
    image_batch_manifest_sha256: str
    observation_id: str
    observation_digest: str
    items: tuple[EvidenceOverlayItem, ...]
    schema_version: int = VISUAL_OVERLAY_SCHEMA_VERSION


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
