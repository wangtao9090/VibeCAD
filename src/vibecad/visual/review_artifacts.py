"""Strict persistence records for advisory visual-review PNG resources.

Each record is one self-contained, immutable file: a bounded canonical JSON
header followed by the exact rendered PNG bytes.  It carries no Task, CAD,
Revision, adoption, or HEAD authority.  Storage and MCP exposure are separate
layers so this contract can be verified without filesystem or host effects.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass
from enum import StrEnum

from vibecad.visual.contracts import MAX_IMAGE_SET_ITEMS, MAX_NORMALIZED_IMAGE_BYTES
from vibecad.visual.overlay_render import RenderedEvidenceOverlay

VISUAL_REVIEW_SCHEMA_VERSION = 1
MAX_VISUAL_REVIEW_METADATA_BYTES = 64 * 1024
_HEADER = struct.Struct(">8sI")
_MAGIC = b"VCADVR1\0"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_VISUAL_REVIEW_RECORD_BYTES = (
    _HEADER.size + MAX_VISUAL_REVIEW_METADATA_BYTES + MAX_NORMALIZED_IMAGE_BYTES
)

_RECONSTRUCTION_ID = re.compile(r"^reconstruction_[0-9a-f]{32}$")
_OBSERVATION_ID = re.compile(r"^visual_observation_[0-9a-f]{32}$")
_RESOURCE_URI = re.compile(
    r"^vibecad://visual-review/(visual_observation_[0-9a-f]{32})/"
    r"(0|[1-9]|1[0-5])\.png$"
)
_MAX_SAFE_INTEGER = 2**53 - 1


class VisualReviewAuthority(StrEnum):
    ADVISORY_ONLY = "advisory_only"


class VisualReviewArtifactErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTEGRITY_FAILURE = "integrity_failure"
    UNSUPPORTED_VERSION = "unsupported_version"
    AUTHORITY_VIOLATION = "authority_violation"


class VisualReviewArtifactError(ValueError):
    """Bounded contract failure without reflected metadata or image bytes."""

    def __init__(self, code: VisualReviewArtifactErrorCode) -> None:
        if type(code) is not VisualReviewArtifactErrorCode:
            raise TypeError("code must be an exact VisualReviewArtifactErrorCode")
        self.code = code
        super().__init__(code.value)


def _fail(code: VisualReviewArtifactErrorCode) -> None:
    raise VisualReviewArtifactError(code)


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualReviewArtifact:
    reconstruction_id: str
    generation: int
    overlay: RenderedEvidenceOverlay
    authority: VisualReviewAuthority = VisualReviewAuthority.ADVISORY_ONLY
    schema_version: int = VISUAL_REVIEW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != VISUAL_REVIEW_SCHEMA_VERSION
        ):
            _fail(VisualReviewArtifactErrorCode.UNSUPPORTED_VERSION)
        if (
            type(self.reconstruction_id) is not str
            or _RECONSTRUCTION_ID.fullmatch(self.reconstruction_id) is None
        ):
            _fail(VisualReviewArtifactErrorCode.INVALID_INPUT)
        if type(self.generation) is not int or not 0 < self.generation <= _MAX_SAFE_INTEGER:
            _fail(VisualReviewArtifactErrorCode.INVALID_INPUT)
        if type(self.overlay) is not RenderedEvidenceOverlay:
            _fail(VisualReviewArtifactErrorCode.INVALID_INPUT)
        if (
            type(self.authority) is not VisualReviewAuthority
            or self.authority is not VisualReviewAuthority.ADVISORY_ONLY
        ):
            _fail(VisualReviewArtifactErrorCode.AUTHORITY_VIOLATION)

    @property
    def observation_id(self) -> str:
        return self.overlay.observation_id

    @property
    def observation_digest(self) -> str:
        return self.overlay.observation_digest

    @property
    def source_index(self) -> int:
        return self.overlay.source_index

    @property
    def resource_uri(self) -> str:
        return visual_review_resource_uri(self.observation_id, self.source_index)

    @property
    def record_sha256(self) -> str:
        return hashlib.sha256(encode_visual_review_artifact(self)).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualReviewResource:
    uri: str
    data: bytes
    media_type: str = "image/png"

    def __post_init__(self) -> None:
        parse_visual_review_resource_uri(self.uri)
        if (
            type(self.media_type) is not str
            or self.media_type != "image/png"
            or type(self.data) is not bytes
            or not self.data.startswith(_PNG_SIGNATURE)
        ):
            _fail(VisualReviewArtifactErrorCode.INVALID_INPUT)
        if len(self.data) > MAX_NORMALIZED_IMAGE_BYTES:
            _fail(VisualReviewArtifactErrorCode.BUDGET_EXCEEDED)


def visual_review_resource_uri(observation_id: object, source_index: object) -> str:
    if (
        type(observation_id) is not str
        or _OBSERVATION_ID.fullmatch(observation_id) is None
        or type(source_index) is not int
        or not 0 <= source_index < MAX_IMAGE_SET_ITEMS
    ):
        _fail(VisualReviewArtifactErrorCode.INVALID_INPUT)
    return f"vibecad://visual-review/{observation_id}/{source_index}.png"


def parse_visual_review_resource_uri(uri: object) -> tuple[str, int]:
    if type(uri) is not str:
        _fail(VisualReviewArtifactErrorCode.INVALID_INPUT)
    matched = _RESOURCE_URI.fullmatch(uri)
    if matched is None:
        _fail(VisualReviewArtifactErrorCode.INVALID_INPUT)
    return matched.group(1), int(matched.group(2))


def _metadata(value: VisualReviewArtifact) -> dict[str, object]:
    overlay = value.overlay
    return {
        "authority": value.authority.value,
        "generation": value.generation,
        "height": overlay.height,
        "image_batch_manifest_sha256": overlay.image_batch_manifest_sha256,
        "image_set_id": overlay.image_set_id,
        "image_set_manifest_sha256": overlay.image_set_manifest_sha256,
        "item_count": overlay.item_count,
        "observation_digest": overlay.observation_digest,
        "observation_id": overlay.observation_id,
        "overlay_schema_version": overlay.schema_version,
        "png_sha256": overlay.png_sha256,
        "png_size_bytes": overlay.png_size_bytes,
        "profile": overlay.profile,
        "reconstruction_id": value.reconstruction_id,
        "schema_version": value.schema_version,
        "source_index": overlay.source_index,
        "visual_input_id": overlay.visual_input_id,
        "width": overlay.width,
    }


def _canonical_metadata(value: dict[str, object]) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail(VisualReviewArtifactErrorCode.INVALID_INPUT)
    if not raw or len(raw) > MAX_VISUAL_REVIEW_METADATA_BYTES:
        _fail(VisualReviewArtifactErrorCode.BUDGET_EXCEEDED)
    return raw


def encode_visual_review_artifact(value: object) -> bytes:
    if type(value) is not VisualReviewArtifact:
        _fail(VisualReviewArtifactErrorCode.INVALID_INPUT)
    metadata = _canonical_metadata(_metadata(value))
    raw = _HEADER.pack(_MAGIC, len(metadata)) + metadata + value.overlay.png_bytes
    if len(raw) > MAX_VISUAL_REVIEW_RECORD_BYTES:
        _fail(VisualReviewArtifactErrorCode.BUDGET_EXCEEDED)
    return raw


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _invalid_constant(_value: str) -> object:
    raise ValueError


def _decode_metadata(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
        _fail(VisualReviewArtifactErrorCode.INTEGRITY_FAILURE)
    if type(value) is not dict:
        _fail(VisualReviewArtifactErrorCode.INTEGRITY_FAILURE)
    expected = {
        "authority",
        "generation",
        "height",
        "image_batch_manifest_sha256",
        "image_set_id",
        "image_set_manifest_sha256",
        "item_count",
        "observation_digest",
        "observation_id",
        "overlay_schema_version",
        "png_sha256",
        "png_size_bytes",
        "profile",
        "reconstruction_id",
        "schema_version",
        "source_index",
        "visual_input_id",
        "width",
    }
    if set(value) != expected or any(type(key) is not str for key in value):
        _fail(VisualReviewArtifactErrorCode.INTEGRITY_FAILURE)
    return value


def decode_visual_review_artifact(raw: object) -> VisualReviewArtifact:
    if (
        type(raw) is not bytes
        or len(raw) < _HEADER.size
        or len(raw) > MAX_VISUAL_REVIEW_RECORD_BYTES
    ):
        _fail(VisualReviewArtifactErrorCode.INVALID_INPUT)
    try:
        magic, metadata_size = _HEADER.unpack_from(raw)
    except struct.error:
        _fail(VisualReviewArtifactErrorCode.INTEGRITY_FAILURE)
    if magic != _MAGIC or not 0 < metadata_size <= MAX_VISUAL_REVIEW_METADATA_BYTES:
        _fail(VisualReviewArtifactErrorCode.INTEGRITY_FAILURE)
    boundary = _HEADER.size + metadata_size
    if boundary >= len(raw):
        _fail(VisualReviewArtifactErrorCode.INTEGRITY_FAILURE)
    data = _decode_metadata(raw[_HEADER.size : boundary])
    png = raw[boundary:]
    try:
        if data["authority"] != VisualReviewAuthority.ADVISORY_ONLY.value:
            _fail(VisualReviewArtifactErrorCode.AUTHORITY_VIOLATION)
        overlay = RenderedEvidenceOverlay(
            image_set_id=data["image_set_id"],
            image_set_manifest_sha256=data["image_set_manifest_sha256"],
            image_batch_manifest_sha256=data["image_batch_manifest_sha256"],
            observation_id=data["observation_id"],
            observation_digest=data["observation_digest"],
            source_index=data["source_index"],
            visual_input_id=data["visual_input_id"],
            width=data["width"],
            height=data["height"],
            item_count=data["item_count"],
            png_sha256=data["png_sha256"],
            png_size_bytes=data["png_size_bytes"],
            png_bytes=png,
            profile=data["profile"],
            schema_version=data["overlay_schema_version"],
        )
        result = VisualReviewArtifact(
            reconstruction_id=data["reconstruction_id"],
            generation=data["generation"],
            overlay=overlay,
            authority=VisualReviewAuthority(data["authority"]),
            schema_version=data["schema_version"],
        )
    except VisualReviewArtifactError:
        raise
    except (TypeError, ValueError):
        _fail(VisualReviewArtifactErrorCode.INTEGRITY_FAILURE)
    if encode_visual_review_artifact(result) != raw:
        _fail(VisualReviewArtifactErrorCode.INTEGRITY_FAILURE)
    return result


__all__ = [
    "MAX_VISUAL_REVIEW_METADATA_BYTES",
    "MAX_VISUAL_REVIEW_RECORD_BYTES",
    "VISUAL_REVIEW_SCHEMA_VERSION",
    "VisualReviewArtifact",
    "VisualReviewArtifactError",
    "VisualReviewArtifactErrorCode",
    "VisualReviewAuthority",
    "VisualReviewResource",
    "decode_visual_review_artifact",
    "encode_visual_review_artifact",
    "parse_visual_review_resource_uri",
    "visual_review_resource_uri",
]
