"""Focused tests for deterministic local evidence-overlay rendering."""

from __future__ import annotations

import dataclasses
import hashlib
import io

import pytest
from PIL import Image, ImageChops

from tests.test_visual_evidence import _image_set
from tests.test_visual_overlay import _evidence, _feature
from vibecad.visual.geometry_fit import PrimitiveFamily
from vibecad.visual.overlay import build_evidence_overlay
from vibecad.visual.overlay_render import (
    OVERLAY_RENDER_PROFILE,
    OverlayRenderError,
    OverlayRenderErrorCode,
    RenderedEvidenceOverlay,
    render_evidence_overlay,
)


def _plan_and_images(*sizes: tuple[int, int]):
    image_set, raws = _image_set(*sizes)
    evidence = _evidence(
        image_set,
        (
            _feature(PrimitiveFamily.LINE, "line"),
            _feature(PrimitiveFamily.CIRCLE, "circle"),
            _feature(PrimitiveFamily.ARC, "arc"),
            _feature(PrimitiveFamily.ROTATED_RECTANGLE, "rectangle"),
        ),
    )
    return build_evidence_overlay(evidence, image_set), image_set, raws


def test_rendered_overlay_is_bound_visible_and_repeatable() -> None:
    plan, image_set, raws = _plan_and_images((96, 64))

    first = render_evidence_overlay(
        plan=plan,
        image_set=image_set,
        source_index=0,
        normalized_png=raws[0],
    )
    second = render_evidence_overlay(
        plan=plan,
        image_set=image_set,
        source_index=0,
        normalized_png=raws[0],
    )

    assert first.profile == OVERLAY_RENDER_PROFILE
    assert first.item_count == 4
    assert (first.width, first.height) == (96, 64)
    assert first.visual_input_id == image_set.inputs[0].normalized.id
    assert first.png_bytes == second.png_bytes
    assert first.png_sha256 == hashlib.sha256(first.png_bytes).hexdigest()
    with (
        Image.open(io.BytesIO(first.png_bytes)) as rendered,
        Image.open(io.BytesIO(raws[0])) as source,
    ):
        assert rendered.format == "PNG"
        assert rendered.mode == "RGB"
        assert rendered.size == source.size
        assert ImageChops.difference(rendered, source.convert("RGB")).getbbox() is not None


def test_render_source_without_items_returns_bound_png() -> None:
    plan, image_set, raws = _plan_and_images((96, 64), (80, 60))

    result = render_evidence_overlay(
        plan=plan,
        image_set=image_set,
        source_index=1,
        normalized_png=raws[1],
    )

    assert result.source_index == 1
    assert result.item_count == 0
    assert result.observation_id == plan.observation_id
    with Image.open(io.BytesIO(result.png_bytes)) as rendered:
        assert rendered.size == (80, 60)


def test_render_rejects_plan_image_and_byte_binding_mismatches() -> None:
    plan, image_set, raws = _plan_and_images((96, 64))
    other_image_set, other_raws = _image_set((97, 64))

    with pytest.raises(OverlayRenderError) as set_mismatch:
        render_evidence_overlay(
            plan=plan,
            image_set=other_image_set,
            source_index=0,
            normalized_png=other_raws[0],
        )
    assert set_mismatch.value.code is OverlayRenderErrorCode.BINDING_MISMATCH

    tampered = bytearray(raws[0])
    tampered[-5] ^= 1
    with pytest.raises(OverlayRenderError) as digest_mismatch:
        render_evidence_overlay(
            plan=plan,
            image_set=image_set,
            source_index=0,
            normalized_png=bytes(tampered),
        )
    assert digest_mismatch.value.code is OverlayRenderErrorCode.INTEGRITY_FAILURE

    with pytest.raises(OverlayRenderError) as source_index:
        render_evidence_overlay(
            plan=plan,
            image_set=image_set,
            source_index=1,
            normalized_png=raws[0],
        )
    assert source_index.value.code is OverlayRenderErrorCode.INVALID_INPUT


def test_render_rejects_malformed_and_multiframe_png_after_digest_binding() -> None:
    plan, image_set, _raws = _plan_and_images((96, 64))
    malformed = b"\x89PNG\r\n\x1a\nnot-a-png"
    image_ref = image_set.inputs[0].normalized
    object.__setattr__(image_ref, "sha256", hashlib.sha256(malformed).hexdigest())
    object.__setattr__(image_ref, "size_bytes", len(malformed))

    with pytest.raises(OverlayRenderError) as invalid_png:
        render_evidence_overlay(
            plan=plan,
            image_set=image_set,
            source_index=0,
            normalized_png=malformed,
        )
    assert invalid_png.value.code is OverlayRenderErrorCode.INTEGRITY_FAILURE

    frames = (Image.new("RGB", (96, 64), "red"), Image.new("RGB", (96, 64), "blue"))
    stream = io.BytesIO()
    frames[0].save(stream, format="PNG", save_all=True, append_images=(frames[1],))
    animated = stream.getvalue()
    object.__setattr__(image_ref, "sha256", hashlib.sha256(animated).hexdigest())
    object.__setattr__(image_ref, "size_bytes", len(animated))
    with pytest.raises(OverlayRenderError) as multiple_frames:
        render_evidence_overlay(
            plan=plan,
            image_set=image_set,
            source_index=0,
            normalized_png=animated,
        )
    assert multiple_frames.value.code is OverlayRenderErrorCode.INTEGRITY_FAILURE


def test_render_enforces_input_budget_before_hash_or_decode(monkeypatch) -> None:
    plan, image_set, raws = _plan_and_images((96, 64))
    monkeypatch.setattr("vibecad.visual.overlay_render.MAX_NORMALIZED_IMAGE_BYTES", 8)

    with pytest.raises(OverlayRenderError) as over_budget:
        render_evidence_overlay(
            plan=plan,
            image_set=image_set,
            source_index=0,
            normalized_png=raws[0],
        )
    assert over_budget.value.code is OverlayRenderErrorCode.BUDGET_EXCEEDED


def test_rendered_result_authenticates_its_png_bytes() -> None:
    plan, image_set, raws = _plan_and_images((96, 64))
    result = render_evidence_overlay(
        plan=plan,
        image_set=image_set,
        source_index=0,
        normalized_png=raws[0],
    )

    with pytest.raises(OverlayRenderError) as forged:
        dataclasses.replace(result, png_sha256="0" * 64)
    assert forged.value.code is OverlayRenderErrorCode.INTEGRITY_FAILURE

    with pytest.raises(OverlayRenderError) as wrong_type:
        render_evidence_overlay(
            plan=object(),  # type: ignore[arg-type]
            image_set=image_set,
            source_index=0,
            normalized_png=raws[0],
        )
    assert wrong_type.value.code is OverlayRenderErrorCode.INVALID_INPUT


def test_overlay_renderer_has_no_authority_or_runtime_dependencies() -> None:
    source = __import__("inspect").getsource(
        __import__("vibecad.visual.overlay_render", fromlist=["*"])
    )
    for forbidden in (
        "vibecad.tasks",
        "vibecad.interaction",
        "vibecad.cad",
        "vibecad.worker",
        "vibecad.mcp",
        "vibecad.providers",
        "blender",
        "bpy",
    ):
        assert forbidden not in source


def test_rendered_overlay_rejects_non_exact_png_payload_type() -> None:
    plan, image_set, raws = _plan_and_images((96, 64))
    result = render_evidence_overlay(
        plan=plan,
        image_set=image_set,
        source_index=0,
        normalized_png=raws[0],
    )

    with pytest.raises(OverlayRenderError) as wrong_payload:
        RenderedEvidenceOverlay(
            image_set_id=result.image_set_id,
            image_set_manifest_sha256=result.image_set_manifest_sha256,
            image_batch_manifest_sha256=result.image_batch_manifest_sha256,
            observation_id=result.observation_id,
            observation_digest=result.observation_digest,
            source_index=result.source_index,
            visual_input_id=result.visual_input_id,
            width=result.width,
            height=result.height,
            item_count=result.item_count,
            png_sha256=result.png_sha256,
            png_size_bytes=result.png_size_bytes,
            png_bytes=bytearray(result.png_bytes),  # type: ignore[arg-type]
        )
    assert wrong_payload.value.code is OverlayRenderErrorCode.INVALID_INPUT
