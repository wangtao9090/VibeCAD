"""Provider-neutral preparation of sealed images for cloud vision adapters.

The values in this module describe the usable VibeCAD subset of a provider's
current image limits.  They are deliberately independent of any provider SDK
or HTTP transport.  Original images remain in the sealed local store; only
metadata-stripped PNG derivatives produced here are eligible for transport.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from PIL import Image, UnidentifiedImageError

from vibecad.visual.contracts import (
    MAX_IMAGE_SET_ITEMS,
    MAX_NORMALIZED_IMAGE_BYTES,
    ImageMime,
    ImageSet,
    ProcessingAuthorization,
    ViewRole,
)

PROVIDER_IMAGE_SCHEMA_VERSION = 1
MAX_PROVIDER_IMAGE_PARTS = 32
MAX_PROVIDER_IMAGE_BYTES = 64 * 1024 * 1024
MAX_PROVIDER_BATCH_IMAGE_BYTES = 512 * 1024 * 1024
MAX_PROVIDER_LONG_EDGE = 8192
MAX_PROVIDER_TRANSPORT_TIMEOUT_MS = 10 * 60 * 1000

_PROFILE_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_DERIVATIVE_ID = re.compile(r"^provider_image_[0-9a-f]{32}$")
_DERIVATIVE_DOMAIN = b"vibecad-provider-image-derivative-v1\0"
_BATCH_DOMAIN = b"vibecad-provider-image-batch-v1\0"
_MIN_RENDER_LONG_EDGE = 256
_MAX_RENDER_PASSES = 12
_MAX_MANIFEST_BYTES = 64 * 1024


class ProviderImageErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTEGRITY_FAILURE = "integrity_failure"


class ProviderImageError(ValueError):
    """Bounded preparation failure that never reflects image metadata."""

    def __init__(self, code: ProviderImageErrorCode) -> None:
        if type(code) is not ProviderImageErrorCode:
            raise TypeError("code must be an exact ProviderImageErrorCode")
        self.code = code
        super().__init__(code.value)


def _fail(code: ProviderImageErrorCode) -> None:
    raise ProviderImageError(code)


def _text(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _fail(ProviderImageErrorCode.INVALID_INPUT)
    return value


def _positive_int(value: object, maximum: int) -> int:
    if type(value) is not int or not 0 < value <= maximum:
        _fail(ProviderImageErrorCode.INVALID_INPUT)
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
        _fail(ProviderImageErrorCode.INVALID_INPUT)
    if len(raw) > _MAX_MANIFEST_BYTES:
        _fail(ProviderImageErrorCode.BUDGET_EXCEEDED)
    return raw


class ProviderImageDetail(StrEnum):
    AUTO = "auto"
    LOW = "low"
    HIGH = "high"
    ORIGINAL = "original"


class ProviderImagePartKind(StrEnum):
    OVERVIEW = "overview"
    DETAIL_CROP = "detail_crop"


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualProviderCapabilityProfile:
    """A bounded, versioned VibeCAD view of one provider/model capability."""

    provider: str
    model: str
    model_version: str
    data_policy_profile: str
    max_source_images: int
    max_image_parts: int
    max_image_bytes: int
    max_batch_image_bytes: int
    preferred_long_edge: int
    max_long_edge: int
    detail: ProviderImageDetail
    supports_detail_crops: bool
    transport_timeout_ms: int
    schema_version: int = PROVIDER_IMAGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != PROVIDER_IMAGE_SCHEMA_VERSION
        ):
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        object.__setattr__(self, "provider", _text(self.provider, _PROFILE_NAME))
        object.__setattr__(self, "model", _text(self.model, _PROFILE_NAME))
        object.__setattr__(self, "model_version", _text(self.model_version, _VERSION))
        object.__setattr__(
            self,
            "data_policy_profile",
            _text(self.data_policy_profile, _PROFILE_NAME),
        )
        source_count = _positive_int(self.max_source_images, MAX_IMAGE_SET_ITEMS)
        part_count = _positive_int(self.max_image_parts, MAX_PROVIDER_IMAGE_PARTS)
        if part_count < source_count:
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        object.__setattr__(self, "max_source_images", source_count)
        object.__setattr__(self, "max_image_parts", part_count)
        image_bytes = _positive_int(self.max_image_bytes, MAX_PROVIDER_IMAGE_BYTES)
        batch_bytes = _positive_int(
            self.max_batch_image_bytes,
            MAX_PROVIDER_BATCH_IMAGE_BYTES,
        )
        if batch_bytes < image_bytes:
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        object.__setattr__(self, "max_image_bytes", image_bytes)
        object.__setattr__(self, "max_batch_image_bytes", batch_bytes)
        preferred = _positive_int(self.preferred_long_edge, MAX_PROVIDER_LONG_EDGE)
        maximum = _positive_int(self.max_long_edge, MAX_PROVIDER_LONG_EDGE)
        if preferred > maximum or preferred < _MIN_RENDER_LONG_EDGE:
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        object.__setattr__(self, "preferred_long_edge", preferred)
        object.__setattr__(self, "max_long_edge", maximum)
        if type(self.detail) is not ProviderImageDetail:
            try:
                object.__setattr__(self, "detail", ProviderImageDetail(self.detail))
            except (TypeError, ValueError):
                _fail(ProviderImageErrorCode.INVALID_INPUT)
        if type(self.supports_detail_crops) is not bool:
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        object.__setattr__(
            self,
            "transport_timeout_ms",
            _positive_int(
                self.transport_timeout_ms,
                MAX_PROVIDER_TRANSPORT_TIMEOUT_MS,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "model": self.model,
            "model_version": self.model_version,
            "data_policy_profile": self.data_policy_profile,
            "max_source_images": self.max_source_images,
            "max_image_parts": self.max_image_parts,
            "max_image_bytes": self.max_image_bytes,
            "max_batch_image_bytes": self.max_batch_image_bytes,
            "preferred_long_edge": self.preferred_long_edge,
            "max_long_edge": self.max_long_edge,
            "detail": self.detail.value,
            "supports_detail_crops": self.supports_detail_crops,
            "transport_timeout_ms": self.transport_timeout_ms,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderDetailCrop:
    """One normalized crop requested for small, reconstruction-relevant detail."""

    source_index: int
    left: int | float
    top: int | float
    right: int | float
    bottom: int | float
    label: str
    schema_version: int = PROVIDER_IMAGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != PROVIDER_IMAGE_SCHEMA_VERSION
        ):
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        if type(self.source_index) is not int or not 0 <= self.source_index < MAX_IMAGE_SET_ITEMS:
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        coordinates: list[float] = []
        for name in ("left", "top", "right", "bottom"):
            value = getattr(self, name)
            if type(value) not in {int, float} or isinstance(value, bool):
                _fail(ProviderImageErrorCode.INVALID_INPUT)
            converted = float(value)
            if not math.isfinite(converted) or not 0 <= converted <= 1:
                _fail(ProviderImageErrorCode.INVALID_INPUT)
            object.__setattr__(self, name, converted)
            coordinates.append(converted)
        if coordinates[2] <= coordinates[0] or coordinates[3] <= coordinates[1]:
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        object.__setattr__(self, "label", _text(self.label, _PROFILE_NAME))


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderImagePart:
    id: str
    source_index: int
    source_sha256: str
    kind: ProviderImagePartKind
    label: str | None
    width: int
    height: int
    size_bytes: int
    sha256: str
    detail: ProviderImageDetail
    view_role: ViewRole
    data: bytes = field(repr=False, compare=False)
    mime: ImageMime = ImageMime.PNG
    schema_version: int = PROVIDER_IMAGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != PROVIDER_IMAGE_SCHEMA_VERSION
        ):
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        if type(self.id) is not str or _DERIVATIVE_ID.fullmatch(self.id) is None:
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        if type(self.source_index) is not int or not 0 <= self.source_index < MAX_IMAGE_SET_ITEMS:
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        if type(self.source_sha256) is not str or _DIGEST.fullmatch(self.source_sha256) is None:
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        if type(self.kind) is not ProviderImagePartKind:
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        if self.label is not None:
            object.__setattr__(self, "label", _text(self.label, _PROFILE_NAME))
        if (self.kind is ProviderImagePartKind.OVERVIEW) != (self.label is None):
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        object.__setattr__(self, "width", _positive_int(self.width, MAX_PROVIDER_LONG_EDGE))
        object.__setattr__(self, "height", _positive_int(self.height, MAX_PROVIDER_LONG_EDGE))
        object.__setattr__(
            self,
            "size_bytes",
            _positive_int(self.size_bytes, MAX_PROVIDER_IMAGE_BYTES),
        )
        if type(self.sha256) is not str or _DIGEST.fullmatch(self.sha256) is None:
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        if type(self.data) is not bytes or len(self.data) != self.size_bytes:
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        if not hmac.compare_digest(hashlib.sha256(self.data).hexdigest(), self.sha256):
            _fail(ProviderImageErrorCode.INTEGRITY_FAILURE)
        expected_id = (
            "provider_image_"
            + hashlib.sha256(
                _DERIVATIVE_DOMAIN
                + bytes.fromhex(self.source_sha256)
                + bytes.fromhex(self.sha256)
                + f"{self.source_index}:{self.kind.value}:{self.label or ''}".encode("ascii")
            ).hexdigest()[:32]
        )
        if self.id != expected_id:
            _fail(ProviderImageErrorCode.INTEGRITY_FAILURE)
        if type(self.detail) is not ProviderImageDetail or type(self.view_role) is not ViewRole:
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        if self.mime is not ImageMime.PNG or not self.data.startswith(b"\x89PNG\r\n\x1a\n"):
            _fail(ProviderImageErrorCode.INTEGRITY_FAILURE)

    def to_manifest_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_image_id": self.id,
            "source_index": self.source_index,
            "source_sha256": self.source_sha256,
            "kind": self.kind.value,
            "label": self.label,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "mime": self.mime.value,
            "detail": self.detail.value,
            "view_role": self.view_role.value,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderImageBatch:
    image_set_id: str
    image_set_manifest_sha256: str
    profile: VisualProviderCapabilityProfile
    parts: tuple[ProviderImagePart, ...]
    total_bytes: int
    manifest_sha256: str = ""
    schema_version: int = PROVIDER_IMAGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != PROVIDER_IMAGE_SCHEMA_VERSION
        ):
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        if type(self.image_set_id) is not str or not self.image_set_id.startswith("image_set_"):
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        if (
            type(self.image_set_manifest_sha256) is not str
            or _DIGEST.fullmatch(self.image_set_manifest_sha256) is None
            or type(self.profile) is not VisualProviderCapabilityProfile
            or type(self.parts) is not tuple
            or not 1 <= len(self.parts) <= self.profile.max_image_parts
            or any(type(item) is not ProviderImagePart for item in self.parts)
        ):
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        if len({item.id for item in self.parts}) != len(self.parts):
            _fail(ProviderImageErrorCode.INTEGRITY_FAILURE)
        overviews = tuple(
            item for item in self.parts if item.kind is ProviderImagePartKind.OVERVIEW
        )
        overview_indices = tuple(sorted(item.source_index for item in overviews))
        if (
            not overviews
            or len(overviews) > self.profile.max_source_images
            or overview_indices != tuple(range(len(overviews)))
            or any(
                item.source_index >= len(overviews)
                or item.detail is not self.profile.detail
                or item.size_bytes > self.profile.max_image_bytes
                or max(item.width, item.height) > self.profile.max_long_edge
                for item in self.parts
            )
        ):
            _fail(ProviderImageErrorCode.INTEGRITY_FAILURE)
        crop_keys = tuple(
            (item.source_index, item.label)
            for item in self.parts
            if item.kind is ProviderImagePartKind.DETAIL_CROP
        )
        if len(crop_keys) != len(set(crop_keys)):
            _fail(ProviderImageErrorCode.INTEGRITY_FAILURE)
        total = sum(item.size_bytes for item in self.parts)
        if self.total_bytes != total or total > self.profile.max_batch_image_bytes:
            _fail(ProviderImageErrorCode.BUDGET_EXCEEDED)
        body = self._body_mapping()
        expected = hashlib.sha256(_BATCH_DOMAIN + _canonical_json(body)).hexdigest()
        if self.manifest_sha256 and not hmac.compare_digest(self.manifest_sha256, expected):
            _fail(ProviderImageErrorCode.INTEGRITY_FAILURE)
        object.__setattr__(self, "manifest_sha256", expected)

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "image_set_id": self.image_set_id,
            "image_set_manifest_sha256": self.image_set_manifest_sha256,
            "profile": self.profile.to_mapping(),
            "parts": [item.to_manifest_mapping() for item in self.parts],
            "total_bytes": self.total_bytes,
        }

    def to_manifest_mapping(self) -> dict[str, object]:
        return self._body_mapping() | {"manifest_sha256": self.manifest_sha256}


def _checked_sources(image_set: ImageSet, normalized_images: object) -> tuple[bytes, ...]:
    if type(normalized_images) is not tuple or len(normalized_images) != len(image_set.inputs):
        _fail(ProviderImageErrorCode.INVALID_INPUT)
    result: list[bytes] = []
    for item, raw in zip(image_set.inputs, normalized_images, strict=True):
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_NORMALIZED_IMAGE_BYTES:
            _fail(ProviderImageErrorCode.INVALID_INPUT)
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), item.normalized.sha256):
            _fail(ProviderImageErrorCode.INTEGRITY_FAILURE)
        result.append(raw)
    return tuple(result)


def _crop_box(image: Image.Image, crop: ProviderDetailCrop | None) -> tuple[int, int, int, int]:
    if crop is None:
        return (0, 0, image.width, image.height)
    left = max(0, min(image.width - 1, math.floor(crop.left * image.width)))
    top = max(0, min(image.height - 1, math.floor(crop.top * image.height)))
    right = max(left + 1, min(image.width, math.ceil(crop.right * image.width)))
    bottom = max(top + 1, min(image.height, math.ceil(crop.bottom * image.height)))
    return (left, top, right, bottom)


def _render_png(
    raw: bytes,
    *,
    crop: ProviderDetailCrop | None,
    target_long_edge: int,
    max_image_bytes: int,
) -> tuple[bytes, int, int]:
    current_target = target_long_edge
    for _ in range(_MAX_RENDER_PASSES):
        try:
            with Image.open(io.BytesIO(raw)) as source:
                if source.format != "PNG" or getattr(source, "n_frames", 1) != 1:
                    _fail(ProviderImageErrorCode.INTEGRITY_FAILURE)
                box = _crop_box(source, crop)
                image = source.crop(box).convert("RGBA" if "A" in source.getbands() else "RGB")
        except ProviderImageError:
            raise
        except (OSError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError):
            _fail(ProviderImageErrorCode.INTEGRITY_FAILURE)
        long_edge = max(image.size)
        if long_edge > current_target:
            scale = current_target / long_edge
            image = image.resize(
                (
                    max(1, round(image.width * scale)),
                    max(1, round(image.height * scale)),
                ),
                Image.Resampling.LANCZOS,
            )
        stream = io.BytesIO()
        image.save(stream, format="PNG", optimize=False, compress_level=6)
        encoded = stream.getvalue()
        if len(encoded) <= max_image_bytes:
            return encoded, image.width, image.height
        if current_target <= _MIN_RENDER_LONG_EDGE:
            break
        current_target = max(_MIN_RENDER_LONG_EDGE, math.floor(current_target * 0.75))
    _fail(ProviderImageErrorCode.BUDGET_EXCEEDED)


def _part(
    *,
    image_set: ImageSet,
    raw: bytes,
    source_index: int,
    crop: ProviderDetailCrop | None,
    profile: VisualProviderCapabilityProfile,
    target_long_edge: int,
) -> ProviderImagePart:
    encoded, width, height = _render_png(
        raw,
        crop=crop,
        target_long_edge=min(target_long_edge, profile.max_long_edge),
        max_image_bytes=profile.max_image_bytes,
    )
    digest = hashlib.sha256(encoded).hexdigest()
    source_digest = image_set.inputs[source_index].original.sha256
    kind = ProviderImagePartKind.OVERVIEW if crop is None else ProviderImagePartKind.DETAIL_CROP
    label = None if crop is None else crop.label
    identifier = (
        "provider_image_"
        + hashlib.sha256(
            _DERIVATIVE_DOMAIN
            + bytes.fromhex(source_digest)
            + bytes.fromhex(digest)
            + f"{source_index}:{kind.value}:{label or ''}".encode("ascii")
        ).hexdigest()[:32]
    )
    return ProviderImagePart(
        id=identifier,
        source_index=source_index,
        source_sha256=source_digest,
        kind=kind,
        label=label,
        width=width,
        height=height,
        size_bytes=len(encoded),
        sha256=digest,
        detail=profile.detail,
        view_role=image_set.inputs[source_index].view_role,
        data=encoded,
    )


def prepare_provider_image_batch(
    *,
    image_set: ImageSet,
    normalized_images: tuple[bytes, ...],
    profile: VisualProviderCapabilityProfile,
    detail_crops: Sequence[ProviderDetailCrop] = (),
) -> ProviderImageBatch:
    """Create one bounded derivative batch without dropping source views."""

    if type(image_set) is not ImageSet or type(profile) is not VisualProviderCapabilityProfile:
        _fail(ProviderImageErrorCode.INVALID_INPUT)
    if image_set.processing_authorization is not ProcessingAuthorization.CLOUD_PROVIDER:
        _fail(ProviderImageErrorCode.INVALID_INPUT)
    sources = _checked_sources(image_set, normalized_images)
    if len(sources) > profile.max_source_images:
        _fail(ProviderImageErrorCode.BUDGET_EXCEEDED)
    if isinstance(detail_crops, (str, bytes)) or not isinstance(detail_crops, Sequence):
        _fail(ProviderImageErrorCode.INVALID_INPUT)
    crops = tuple(detail_crops)
    if any(type(item) is not ProviderDetailCrop for item in crops):
        _fail(ProviderImageErrorCode.INVALID_INPUT)
    if crops and not profile.supports_detail_crops:
        _fail(ProviderImageErrorCode.INVALID_INPUT)
    if any(item.source_index >= len(sources) for item in crops):
        _fail(ProviderImageErrorCode.INVALID_INPUT)
    crop_keys = tuple((item.source_index, item.label) for item in crops)
    if len(crop_keys) != len(set(crop_keys)):
        _fail(ProviderImageErrorCode.INVALID_INPUT)
    if len(sources) + len(crops) > profile.max_image_parts:
        _fail(ProviderImageErrorCode.BUDGET_EXCEEDED)

    target = profile.preferred_long_edge
    for _ in range(_MAX_RENDER_PASSES):
        parts = tuple(
            _part(
                image_set=image_set,
                raw=raw,
                source_index=index,
                crop=None,
                profile=profile,
                target_long_edge=target,
            )
            for index, raw in enumerate(sources)
        ) + tuple(
            _part(
                image_set=image_set,
                raw=sources[crop.source_index],
                source_index=crop.source_index,
                crop=crop,
                profile=profile,
                target_long_edge=target,
            )
            for crop in crops
        )
        total = sum(item.size_bytes for item in parts)
        if total <= profile.max_batch_image_bytes:
            return ProviderImageBatch(
                image_set_id=image_set.id,
                image_set_manifest_sha256=image_set.manifest_sha256,
                profile=profile,
                parts=parts,
                total_bytes=total,
            )
        if target <= _MIN_RENDER_LONG_EDGE:
            break
        ratio = math.sqrt(profile.max_batch_image_bytes / total)
        target = max(
            _MIN_RENDER_LONG_EDGE,
            min(target - 1, math.floor(target * ratio * 0.9)),
        )
    _fail(ProviderImageErrorCode.BUDGET_EXCEEDED)


__all__ = [
    "MAX_PROVIDER_BATCH_IMAGE_BYTES",
    "MAX_PROVIDER_IMAGE_BYTES",
    "MAX_PROVIDER_IMAGE_PARTS",
    "MAX_PROVIDER_LONG_EDGE",
    "MAX_PROVIDER_TRANSPORT_TIMEOUT_MS",
    "PROVIDER_IMAGE_SCHEMA_VERSION",
    "ProviderDetailCrop",
    "ProviderImageBatch",
    "ProviderImageDetail",
    "ProviderImageError",
    "ProviderImageErrorCode",
    "ProviderImagePart",
    "ProviderImagePartKind",
    "VisualProviderCapabilityProfile",
    "prepare_provider_image_batch",
]
