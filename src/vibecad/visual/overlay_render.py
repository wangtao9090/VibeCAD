"""Deterministic local PNG rendering for bound visual evidence overlays.

The renderer consumes an already validated :class:`VisualOverlayPlan` and the
exact normalized PNG sealed by an :class:`ImageSet`.  It adds vector evidence
for one source image and returns bytes suitable for a host-local resource.  It
does not persist data, expose an MCP resource, approve geometry, or mutate a
Task.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import math
import re
import warnings
from dataclasses import dataclass, field
from enum import StrEnum

from PIL import Image, ImageDraw, UnidentifiedImageError

from vibecad.visual.contracts import (
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SET_ITEMS,
    MAX_NORMALIZED_IMAGE_BYTES,
    MAX_NORMALIZED_LONG_EDGE,
    ImageSet,
)
from vibecad.visual.geometry_fit import PrimitiveFamily
from vibecad.visual.overlay import (
    MAX_OVERLAY_ITEMS,
    EvidenceOverlayItem,
    OverlayGeometryKind,
    VisualOverlayPlan,
)

OVERLAY_RENDER_SCHEMA_VERSION = 1
OVERLAY_RENDER_PROFILE = "vibecad-evidence-overlay-png-v1-pillow12.2"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_SET_ID = re.compile(r"^image_set_[0-9a-f]{32}$")
_VISUAL_INPUT_ID = re.compile(r"^visual_input_[0-9a-f]{32}$")
_OBSERVATION_ID = re.compile(r"^visual_observation_[0-9a-f]{32}$")

_FAMILY_COLORS: dict[PrimitiveFamily, tuple[int, int, int, int]] = {
    PrimitiveFamily.LINE: (0, 220, 255, 224),
    PrimitiveFamily.CIRCLE: (255, 196, 0, 224),
    PrimitiveFamily.ARC: (255, 92, 184, 224),
    PrimitiveFamily.ROTATED_RECTANGLE: (104, 240, 104, 224),
}


class OverlayRenderErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    BINDING_MISMATCH = "binding_mismatch"
    INTEGRITY_FAILURE = "integrity_failure"
    RENDER_FAILURE = "render_failure"


class OverlayRenderError(ValueError):
    """Bounded rendering failure that never reflects image metadata."""

    def __init__(self, code: OverlayRenderErrorCode, path: str = "") -> None:
        if type(code) is not OverlayRenderErrorCode:
            raise TypeError("code must be an exact OverlayRenderErrorCode")
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


def _fail(code: OverlayRenderErrorCode, path: str = "") -> None:
    raise OverlayRenderError(code, path)


@dataclass(frozen=True, slots=True, kw_only=True)
class RenderedEvidenceOverlay:
    image_set_id: str
    image_set_manifest_sha256: str
    image_batch_manifest_sha256: str
    observation_id: str
    observation_digest: str
    source_index: int
    visual_input_id: str
    width: int
    height: int
    item_count: int
    png_sha256: str
    png_size_bytes: int
    png_bytes: bytes = field(repr=False, compare=False)
    profile: str = OVERLAY_RENDER_PROFILE
    schema_version: int = OVERLAY_RENDER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != OVERLAY_RENDER_SCHEMA_VERSION
        ):
            _fail(OverlayRenderErrorCode.INVALID_INPUT, "/schema_version")
        if type(self.profile) is not str or self.profile != OVERLAY_RENDER_PROFILE:
            _fail(OverlayRenderErrorCode.INVALID_INPUT, "/profile")
        if type(self.image_set_id) is not str or _IMAGE_SET_ID.fullmatch(self.image_set_id) is None:
            _fail(OverlayRenderErrorCode.INVALID_INPUT, "/image_set_id")
        if (
            type(self.visual_input_id) is not str
            or _VISUAL_INPUT_ID.fullmatch(self.visual_input_id) is None
        ):
            _fail(OverlayRenderErrorCode.INVALID_INPUT, "/visual_input_id")
        if (
            type(self.observation_id) is not str
            or _OBSERVATION_ID.fullmatch(self.observation_id) is None
        ):
            _fail(OverlayRenderErrorCode.INVALID_INPUT, "/observation_id")
        for name in (
            "image_set_manifest_sha256",
            "image_batch_manifest_sha256",
            "observation_digest",
            "png_sha256",
        ):
            value = getattr(self, name)
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                _fail(OverlayRenderErrorCode.INVALID_INPUT, f"/{name}")
        if type(self.source_index) is not int or not 0 <= self.source_index < MAX_IMAGE_SET_ITEMS:
            _fail(OverlayRenderErrorCode.INVALID_INPUT, "/source_index")
        if (
            type(self.width) is not int
            or type(self.height) is not int
            or not 0 < self.width <= MAX_NORMALIZED_LONG_EDGE
            or not 0 < self.height <= MAX_NORMALIZED_LONG_EDGE
            or self.width * self.height > MAX_IMAGE_PIXELS
        ):
            _fail(OverlayRenderErrorCode.INVALID_INPUT, "/dimensions")
        if type(self.item_count) is not int or not 0 <= self.item_count <= MAX_OVERLAY_ITEMS:
            _fail(OverlayRenderErrorCode.INVALID_INPUT, "/item_count")
        if type(self.png_bytes) is not bytes or not self.png_bytes.startswith(_PNG_SIGNATURE):
            _fail(OverlayRenderErrorCode.INVALID_INPUT, "/png_bytes")
        if (
            type(self.png_size_bytes) is not int
            or self.png_size_bytes != len(self.png_bytes)
            or not 0 < self.png_size_bytes <= MAX_NORMALIZED_IMAGE_BYTES
        ):
            _fail(OverlayRenderErrorCode.INVALID_INPUT, "/png_size_bytes")
        if not hmac.compare_digest(hashlib.sha256(self.png_bytes).hexdigest(), self.png_sha256):
            _fail(OverlayRenderErrorCode.INTEGRITY_FAILURE, "/png_sha256")


def _decode_source(raw: bytes, *, width: int, height: int) -> Image.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as decoded:
                if (
                    decoded.format != "PNG"
                    or getattr(decoded, "n_frames", 1) != 1
                    or decoded.size != (width, height)
                    or decoded.mode not in {"RGB", "RGBA"}
                ):
                    _fail(OverlayRenderErrorCode.INTEGRITY_FAILURE, "/normalized_png")
                decoded.load()
                if decoded.mode == "RGBA":
                    background = Image.new("RGBA", decoded.size, (255, 255, 255, 255))
                    result = Image.alpha_composite(background, decoded).convert("RGB")
                else:
                    result = decoded.copy()
                result.info.clear()
                return result
    except OverlayRenderError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        MemoryError,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        ValueError,
    ):
        _fail(OverlayRenderErrorCode.INTEGRITY_FAILURE, "/normalized_png")


def _pixel_points(
    item: EvidenceOverlayItem,
    *,
    width: int,
    height: int,
) -> tuple[tuple[int, int], ...]:
    return tuple(
        (
            round(point.x * (width - 1)),
            round(point.y * (height - 1)),
        )
        for point in item.points
    )


def _draw_item(
    draw: ImageDraw.ImageDraw,
    item: EvidenceOverlayItem,
    *,
    width: int,
    height: int,
) -> None:
    points = _pixel_points(item, width=width, height=height)
    color = _FAMILY_COLORS[item.family]
    span = max(width - 1, height - 1)
    stroke_width = max(1, min(8, round(max(width, height) / 512)))
    marker_radius = max(2, min(10, stroke_width * 2))
    uncertainty_radius = max(1, math.ceil(item.uncertainty_radius_norm * span))

    if item.geometry_kind is OverlayGeometryKind.ORDERED_POLYLINE and len(points) > 1:
        draw.line(points, fill=color, width=stroke_width, joint="curve")
    elif item.geometry_kind is OverlayGeometryKind.CLOSED_POLYGON and len(points) > 1:
        draw.line((*points, points[0]), fill=color, width=stroke_width, joint="curve")

    ring_color = (*color[:3], 144)
    marker_color = (*color[:3], 255)
    for x, y in points:
        draw.ellipse(
            (
                x - uncertainty_radius,
                y - uncertainty_radius,
                x + uncertainty_radius,
                y + uncertainty_radius,
            ),
            outline=ring_color,
            width=stroke_width,
        )
        draw.ellipse(
            (
                x - marker_radius,
                y - marker_radius,
                x + marker_radius,
                y + marker_radius,
            ),
            fill=marker_color,
        )


def render_evidence_overlay(
    *,
    plan: VisualOverlayPlan,
    image_set: ImageSet,
    source_index: int,
    normalized_png: bytes,
) -> RenderedEvidenceOverlay:
    """Render one exact source image with provider-proposed evidence vectors."""

    if type(plan) is not VisualOverlayPlan or type(image_set) is not ImageSet:
        _fail(OverlayRenderErrorCode.INVALID_INPUT)
    if (
        type(source_index) is not int
        or not 0 <= source_index < MAX_IMAGE_SET_ITEMS
        or source_index >= len(image_set.inputs)
    ):
        _fail(OverlayRenderErrorCode.INVALID_INPUT, "/source_index")
    if type(normalized_png) is not bytes or not normalized_png:
        _fail(OverlayRenderErrorCode.INVALID_INPUT, "/normalized_png")
    if len(normalized_png) > MAX_NORMALIZED_IMAGE_BYTES:
        _fail(OverlayRenderErrorCode.BUDGET_EXCEEDED, "/normalized_png")
    if (
        plan.image_set_id != image_set.id
        or plan.image_set_manifest_sha256 != image_set.manifest_sha256
    ):
        _fail(OverlayRenderErrorCode.BINDING_MISMATCH)

    image_ref = image_set.inputs[source_index].normalized
    if len(normalized_png) != image_ref.size_bytes or not hmac.compare_digest(
        hashlib.sha256(normalized_png).hexdigest(), image_ref.sha256
    ):
        _fail(OverlayRenderErrorCode.INTEGRITY_FAILURE, "/normalized_png")
    source = _decode_source(normalized_png, width=image_ref.width, height=image_ref.height)
    items = tuple(item for item in plan.items if item.source_index == source_index)

    try:
        overlay = Image.new("RGBA", source.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay, "RGBA")
        for item in items:
            _draw_item(draw, item, width=source.width, height=source.height)
        rendered = Image.alpha_composite(source.convert("RGBA"), overlay).convert("RGB")
        rendered.info.clear()
        output = io.BytesIO()
        rendered.save(output, format="PNG", compress_level=6, optimize=False)
        raw = output.getvalue()
    except (MemoryError, OSError, ValueError):
        _fail(OverlayRenderErrorCode.RENDER_FAILURE)
    if not raw or len(raw) > MAX_NORMALIZED_IMAGE_BYTES:
        _fail(OverlayRenderErrorCode.BUDGET_EXCEEDED, "/rendered_png")

    return RenderedEvidenceOverlay(
        image_set_id=plan.image_set_id,
        image_set_manifest_sha256=plan.image_set_manifest_sha256,
        image_batch_manifest_sha256=plan.image_batch_manifest_sha256,
        observation_id=plan.observation_id,
        observation_digest=plan.observation_digest,
        source_index=source_index,
        visual_input_id=image_ref.id,
        width=source.width,
        height=source.height,
        item_count=len(items),
        png_sha256=hashlib.sha256(raw).hexdigest(),
        png_size_bytes=len(raw),
        png_bytes=raw,
    )


__all__ = [
    "OVERLAY_RENDER_PROFILE",
    "OVERLAY_RENDER_SCHEMA_VERSION",
    "OverlayRenderError",
    "OverlayRenderErrorCode",
    "RenderedEvidenceOverlay",
    "render_evidence_overlay",
]
