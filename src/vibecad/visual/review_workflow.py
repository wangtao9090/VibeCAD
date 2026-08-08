"""Private render and cleanup contracts for advisory visual-review artifacts.

The render input exists only long enough to turn already bound provider
evidence and exact normalized source bytes into immutable PNG records.  It
does not make process-local evidence durable, replay a provider, or grant CAD
authority.  The cleanup port lets the reconstruction lifecycle tombstone those
records before deleting their sealed source images without exposing a store to
the provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from vibecad.visual.contracts import MAX_IMAGE_SET_ITEMS, ImageSet
from vibecad.visual.evidence import BoundVisualEvidence
from vibecad.visual.overlay import build_evidence_overlay
from vibecad.visual.overlay_render import render_evidence_overlay
from vibecad.visual.review_artifacts import VisualReviewArtifact
from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER


@runtime_checkable
class VisualReviewCleanupPort(Protocol):
    """Application-owned deletion effect with no Provider or CAD authority."""

    def delete_observation_exact(
        self,
        observation_id: str,
        observation_digest: str,
    ) -> int: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualReviewRenderInput:
    """Exact in-process evidence plus sealed normalized image bytes."""

    evidence: BoundVisualEvidence
    image_set: ImageSet
    normalized_images: tuple[bytes, ...]

    def __post_init__(self) -> None:
        if (
            type(self.evidence) is not BoundVisualEvidence
            or type(self.image_set) is not ImageSet
            or type(self.normalized_images) is not tuple
            or len(self.normalized_images) != len(self.image_set.inputs)
            or not 0 < len(self.normalized_images) <= MAX_IMAGE_SET_ITEMS
            or any(type(item) is not bytes or not item for item in self.normalized_images)
            or self.evidence.image_set_id != self.image_set.id
            or self.evidence.image_set_manifest_sha256 != self.image_set.manifest_sha256
        ):
            raise ValueError("invalid visual review render input")


def render_visual_review_artifacts(
    *,
    reconstruction_id: str,
    generation: int,
    value: VisualReviewRenderInput,
) -> tuple[VisualReviewArtifact, ...]:
    """Render one deterministic artifact for every source with evidence."""

    if (
        type(value) is not VisualReviewRenderInput
        or type(generation) is not int
        or not 0 < generation <= MAX_SAFE_JSON_INTEGER
    ):
        raise ValueError("invalid visual review render request")
    plan = build_evidence_overlay(value.evidence, value.image_set)
    source_indices = tuple(sorted({item.source_index for item in plan.items}))
    return tuple(
        VisualReviewArtifact(
            reconstruction_id=reconstruction_id,
            generation=generation,
            overlay=render_evidence_overlay(
                plan=plan,
                image_set=value.image_set,
                source_index=source_index,
                normalized_png=value.normalized_images[source_index],
            ),
        )
        for source_index in source_indices
    )


__all__ = [
    "VisualReviewCleanupPort",
    "VisualReviewRenderInput",
    "render_visual_review_artifacts",
]
