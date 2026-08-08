"""Focused tests for process-local visual-review rendering workflow."""

from __future__ import annotations

import dataclasses

from tests.test_visual_evidence import _image_set
from tests.test_visual_overlay import _evidence, _feature
from vibecad.visual.geometry_fit import PrimitiveFamily
from vibecad.visual.review_workflow import (
    VisualReviewRenderInput,
    render_visual_review_artifacts,
)


def test_review_workflow_renders_only_sources_with_bound_evidence() -> None:
    image_set, raws = _image_set((96, 64), (96, 64))
    first = _feature(PrimitiveFamily.LINE, "first.edge")
    second = dataclasses.replace(
        _feature(PrimitiveFamily.ARC, "second.arc"),
        source_index=1,
    )
    value = VisualReviewRenderInput(
        evidence=_evidence(image_set, (first, second)),
        image_set=image_set,
        normalized_images=raws,
    )

    artifacts = render_visual_review_artifacts(
        reconstruction_id=value.evidence.reconstruction_id,
        generation=3,
        value=value,
    )
    replay = render_visual_review_artifacts(
        reconstruction_id=value.evidence.reconstruction_id,
        generation=3,
        value=value,
    )

    assert tuple(item.source_index for item in artifacts) == (0, 1)
    assert tuple(item.overlay.item_count for item in artifacts) == (1, 1)
    assert tuple(item.record_sha256 for item in artifacts) == tuple(
        item.record_sha256 for item in replay
    )
    assert all(item.observation_id == value.evidence.observation_id for item in artifacts)


def test_review_workflow_does_not_render_sources_without_evidence() -> None:
    image_set, raws = _image_set((96, 64), (80, 60))
    value = VisualReviewRenderInput(
        evidence=_evidence(image_set, ()),
        image_set=image_set,
        normalized_images=raws,
    )

    assert (
        render_visual_review_artifacts(
            reconstruction_id=value.evidence.reconstruction_id,
            generation=3,
            value=value,
        )
        == ()
    )
