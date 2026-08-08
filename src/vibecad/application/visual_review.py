"""Application-owned publication and deletion of advisory review overlays."""

from __future__ import annotations

from dataclasses import dataclass

from vibecad.visual.drafts import ReconstructionDraft
from vibecad.visual.review_store import VisualReviewArtifactStore
from vibecad.visual.review_workflow import render_visual_review_artifacts
from vibecad.visual.service import VisualReconstructionService


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationVisualReviewPort:
    """Bridge process-local evidence to the immutable application review store."""

    store: VisualReviewArtifactStore

    def __post_init__(self) -> None:
        if type(self.store) is not VisualReviewArtifactStore:
            raise TypeError("store must be an exact VisualReviewArtifactStore")

    def delete_observation_exact(
        self,
        observation_id: str,
        observation_digest: str,
    ) -> int:
        return self.store.delete_observation_exact(observation_id, observation_digest)

    def publish_process_local_evidence(
        self,
        *,
        service: VisualReconstructionService,
        draft: ReconstructionDraft,
    ) -> None:
        """Publish available evidence once; absence after restart is not retried."""

        if (
            type(service) is not VisualReconstructionService
            or type(draft) is not ReconstructionDraft
        ):
            raise TypeError("invalid visual review publication request")
        value = service.load_process_local_review_input(draft)
        if value is None:
            return
        for artifact in render_visual_review_artifacts(
            reconstruction_id=draft.reconstruction_id,
            generation=draft.generation,
            value=value,
        ):
            self.store.publish(artifact)


__all__ = ["ApplicationVisualReviewPort"]
