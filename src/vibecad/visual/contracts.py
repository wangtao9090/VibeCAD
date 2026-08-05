"""Strict, provider-neutral contracts for sealed visual inputs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

VISUAL_SCHEMA_VERSION = 1
MAX_IMAGE_SET_ITEMS = 16
MAX_IMAGE_SOURCE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_SET_SOURCE_BYTES = 256 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_SET_PIXELS = 400_000_000
MAX_NORMALIZED_LONG_EDGE = 4096
MAX_NORMALIZED_IMAGE_BYTES = 72 * 1024 * 1024
MAX_IMAGE_SET_RECORD_BYTES = 64 * 1024
MAX_IMAGE_SET_PHYSICAL_BYTES = 1536 * 1024 * 1024
MAX_VISUAL_INPUT_STORE_BYTES = 8 * 1024 * 1024 * 1024
MAX_IMAGE_SETS = 1024
MAX_IMAGE_SET_TEMPORARIES = 8
MAX_DIMENSION_HINTS = 32
MAX_CALIBRATION_EVIDENCE = MAX_IMAGE_SET_ITEMS * 2

SOURCE_JPEG_PROFILE = "source-jpeg-v1"
SOURCE_PNG_PROFILE = "source-png-v1"
NORMALIZATION_PROFILE = "vibecad-png-srgb-4096-v1-pillow12.2"

_MAX_TEXT_BYTES = 256
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_SET_ID = re.compile(r"^image_set_[0-9a-f]{32}$")
_VISUAL_INPUT_ID = re.compile(r"^visual_input_[0-9a-f]{32}$")
_CREATE_KEY = re.compile(r"^image_set_create_[0-9a-f]{32}$")
_IMAGE_SET_ID_DOMAIN = b"vibecad-image-set-id-v1\0"
_VISUAL_INPUT_ID_DOMAIN = b"vibecad-visual-input-id-v1\0"
_MANIFEST_DOMAIN = b"vibecad-image-set-manifest-v1\0"


class VisualContractErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_VERSION = "unsupported_version"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTEGRITY_FAILURE = "integrity_failure"


class VisualContractError(ValueError):
    """Bounded error that never reflects rejected image metadata."""

    def __init__(self, code: VisualContractErrorCode, path: str = "") -> None:
        if type(code) is not VisualContractErrorCode:
            raise TypeError("code must be an exact VisualContractErrorCode")
        if type(path) is not str or len(path.encode("utf-8")) > 512:
            raise ValueError("path must be a bounded string")
        self.code = code
        self.path = path
        super().__init__(code.value)


class ViewRole(StrEnum):
    FRONT = "front"
    TOP = "top"
    RIGHT = "right"
    BACK = "back"
    ISOMETRIC = "isometric"
    UNKNOWN = "unknown"


class CalibrationStatus(StrEnum):
    UNKNOWN = "unknown"
    EXPLICIT_SCALE = "explicit_scale"
    CALIBRATED = "calibrated"


class ProcessingAuthorization(StrEnum):
    LOCAL_ONLY = "local_only"
    CLOUD_PROVIDER = "cloud_provider"


class ImageMime(StrEnum):
    JPEG = "image/jpeg"
    PNG = "image/png"


class CalibrationKind(StrEnum):
    SCALE = "scale"
    CAMERA_INTRINSICS = "camera_intrinsics"


def _fail(code: VisualContractErrorCode, path: str = "") -> None:
    raise VisualContractError(code, path)


def _schema(value: object) -> int:
    if type(value) is not int or value != VISUAL_SCHEMA_VERSION:
        _fail(VisualContractErrorCode.UNSUPPORTED_VERSION, "/schema_version")
    return value


def _exact_mapping(value: object, fields: set[str], path: str = "") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(VisualContractErrorCode.INVALID_INPUT, path)
    try:
        result = dict(value)
    except Exception:
        _fail(VisualContractErrorCode.INVALID_INPUT, path)
    if set(result) != fields or any(type(key) is not str for key in result):
        _fail(VisualContractErrorCode.INVALID_INPUT, path)
    return result


def _text(value: object, path: str) -> str:
    if type(value) is not str:
        _fail(VisualContractErrorCode.INVALID_INPUT, path)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        _fail(VisualContractErrorCode.INVALID_INPUT, path)
    if (
        not encoded
        or len(encoded) > _MAX_TEXT_BYTES
        or value.strip() != value
        or not value.isprintable()
        or len(value.splitlines()) != 1
    ):
        _fail(VisualContractErrorCode.INVALID_INPUT, path)
    return value


def _identifier(value: object, pattern: re.Pattern[str], path: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _fail(VisualContractErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    return _identifier(value, _DIGEST, path)


def _positive_int(value: object, maximum: int, path: str) -> int:
    if type(value) is not int or not 0 < value <= maximum:
        _fail(VisualContractErrorCode.INVALID_INPUT, path)
    return value


def _canonical_json(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _fail(VisualContractErrorCode.INVALID_INPUT)
    if len(raw) > MAX_IMAGE_SET_RECORD_BYTES:
        _fail(VisualContractErrorCode.BUDGET_EXCEEDED)
    return raw


def image_set_identity(create_key: object) -> tuple[str, str]:
    canonical = _identifier(create_key, _CREATE_KEY, "/create_key")
    key_digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()
    identifier = (
        "image_set_"
        + hashlib.sha256(_IMAGE_SET_ID_DOMAIN + bytes.fromhex(key_digest)).hexdigest()[:32]
    )
    return identifier, key_digest


def visual_input_identity(create_key: object, index: object, variant: object) -> str:
    image_set_id, _ = image_set_identity(create_key)
    return _visual_input_identity_for_set(image_set_id, index, variant)


def _visual_input_identity_for_set(image_set_id: object, index: object, variant: object) -> str:
    canonical = _identifier(image_set_id, _IMAGE_SET_ID, "/image_set_id")
    if type(index) is not int or not 0 <= index < MAX_IMAGE_SET_ITEMS:
        _fail(VisualContractErrorCode.INVALID_INPUT, "/index")
    if type(variant) is not str or variant not in {"original", "normalized"}:
        _fail(VisualContractErrorCode.INVALID_INPUT, "/variant")
    seed = f"{canonical}:{index}:{variant}".encode("ascii")
    return "visual_input_" + hashlib.sha256(_VISUAL_INPUT_ID_DOMAIN + seed).hexdigest()[:32]


@dataclass(frozen=True, slots=True, kw_only=True)
class DimensionHint:
    name: str
    value_mm: int | float
    source_index: int
    schema_version: int = VISUAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        if type(self.value_mm) not in {int, float} or isinstance(self.value_mm, bool):
            _fail(VisualContractErrorCode.INVALID_INPUT, "/value_mm")
        if not 0 < self.value_mm <= 1_000_000_000 or (
            type(self.value_mm) is float and not math.isfinite(self.value_mm)
        ):
            _fail(VisualContractErrorCode.INVALID_INPUT, "/value_mm")
        if type(self.source_index) is not int or not 0 <= self.source_index < MAX_IMAGE_SET_ITEMS:
            _fail(VisualContractErrorCode.INVALID_INPUT, "/source_index")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "value_mm": self.value_mm,
            "source_index": self.source_index,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _exact_mapping(
            value,
            {"schema_version", "name", "value_mm", "source_index"},
        )
        return cls(**data)


def _optional_number(
    value: object,
    *,
    positive: bool,
    path: str,
) -> int | float | None:
    if value is None:
        return None
    if type(value) not in {int, float} or isinstance(value, bool):
        _fail(VisualContractErrorCode.INVALID_INPUT, path)
    if (
        (value <= 0 if positive else value < 0)
        or value > 1_000_000_000
        or (type(value) is float and not math.isfinite(value))
    ):
        _fail(VisualContractErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class CalibrationEvidence:
    source_index: int
    kind: CalibrationKind
    reference: str
    scale_mm_per_pixel: int | float | None
    focal_length_px: int | float | None
    principal_x_px: int | float | None
    principal_y_px: int | float | None
    schema_version: int = VISUAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        if type(self.source_index) is not int or not 0 <= self.source_index < MAX_IMAGE_SET_ITEMS:
            _fail(VisualContractErrorCode.INVALID_INPUT, "/source_index")
        if type(self.kind) is not CalibrationKind:
            try:
                object.__setattr__(self, "kind", CalibrationKind(self.kind))
            except (TypeError, ValueError):
                _fail(VisualContractErrorCode.INVALID_INPUT, "/kind")
        object.__setattr__(self, "reference", _text(self.reference, "/reference"))
        object.__setattr__(
            self,
            "scale_mm_per_pixel",
            _optional_number(
                self.scale_mm_per_pixel,
                positive=True,
                path="/scale_mm_per_pixel",
            ),
        )
        object.__setattr__(
            self,
            "focal_length_px",
            _optional_number(self.focal_length_px, positive=True, path="/focal_length_px"),
        )
        object.__setattr__(
            self,
            "principal_x_px",
            _optional_number(self.principal_x_px, positive=False, path="/principal_x_px"),
        )
        object.__setattr__(
            self,
            "principal_y_px",
            _optional_number(self.principal_y_px, positive=False, path="/principal_y_px"),
        )
        if self.kind is CalibrationKind.SCALE:
            valid = self.scale_mm_per_pixel is not None and all(
                value is None
                for value in (self.focal_length_px, self.principal_x_px, self.principal_y_px)
            )
        else:
            valid = (
                self.scale_mm_per_pixel is None
                and self.focal_length_px is not None
                and self.principal_x_px is not None
                and self.principal_y_px is not None
            )
        if not valid:
            _fail(VisualContractErrorCode.INVALID_INPUT, "/kind")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_index": self.source_index,
            "kind": self.kind.value,
            "reference": self.reference,
            "scale_mm_per_pixel": self.scale_mm_per_pixel,
            "focal_length_px": self.focal_length_px,
            "principal_x_px": self.principal_x_px,
            "principal_y_px": self.principal_y_px,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _exact_mapping(
            value,
            {
                "schema_version",
                "source_index",
                "kind",
                "reference",
                "scale_mm_per_pixel",
                "focal_length_px",
                "principal_x_px",
                "principal_y_px",
            },
        )
        return cls(**data)


@dataclass(frozen=True, slots=True, kw_only=True)
class ImageRef:
    id: str
    sha256: str
    size_bytes: int
    mime: ImageMime
    width: int
    height: int
    profile: str
    schema_version: int = VISUAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "id", _identifier(self.id, _VISUAL_INPUT_ID, "/id"))
        object.__setattr__(self, "sha256", _digest(self.sha256, "/sha256"))
        object.__setattr__(
            self,
            "size_bytes",
            _positive_int(self.size_bytes, MAX_NORMALIZED_IMAGE_BYTES, "/size_bytes"),
        )
        if type(self.mime) is not ImageMime:
            try:
                object.__setattr__(self, "mime", ImageMime(self.mime))
            except (TypeError, ValueError):
                _fail(VisualContractErrorCode.INVALID_INPUT, "/mime")
        object.__setattr__(self, "width", _positive_int(self.width, MAX_IMAGE_PIXELS, "/width"))
        object.__setattr__(self, "height", _positive_int(self.height, MAX_IMAGE_PIXELS, "/height"))
        if self.width * self.height > MAX_IMAGE_PIXELS:
            _fail(VisualContractErrorCode.BUDGET_EXCEEDED, "/width")
        object.__setattr__(self, "profile", _text(self.profile, "/profile"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "visual_input_id": self.id,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mime": self.mime.value,
            "width": self.width,
            "height": self.height,
            "profile": self.profile,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _exact_mapping(
            value,
            {
                "schema_version",
                "visual_input_id",
                "sha256",
                "size_bytes",
                "mime",
                "width",
                "height",
                "profile",
            },
        )
        return cls(
            schema_version=data["schema_version"],
            id=data["visual_input_id"],
            sha256=data["sha256"],
            size_bytes=data["size_bytes"],
            mime=data["mime"],
            width=data["width"],
            height=data["height"],
            profile=data["profile"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualInput:
    original: ImageRef
    normalized: ImageRef
    view_role: ViewRole
    calibration_status: CalibrationStatus
    schema_version: int = VISUAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        if type(self.original) is not ImageRef or type(self.normalized) is not ImageRef:
            _fail(VisualContractErrorCode.INVALID_INPUT)
        if self.original.id == self.normalized.id:
            _fail(VisualContractErrorCode.INTEGRITY_FAILURE, "/normalized/id")
        if self.original.size_bytes > MAX_IMAGE_SOURCE_BYTES:
            _fail(VisualContractErrorCode.BUDGET_EXCEEDED, "/original/size_bytes")
        if self.normalized.size_bytes > MAX_NORMALIZED_IMAGE_BYTES:
            _fail(VisualContractErrorCode.BUDGET_EXCEEDED, "/normalized/size_bytes")
        if max(self.normalized.width, self.normalized.height) > MAX_NORMALIZED_LONG_EDGE:
            _fail(VisualContractErrorCode.BUDGET_EXCEEDED, "/normalized/width")
        expected_source_profile = (
            SOURCE_JPEG_PROFILE if self.original.mime is ImageMime.JPEG else SOURCE_PNG_PROFILE
        )
        if self.original.profile != expected_source_profile:
            _fail(VisualContractErrorCode.INTEGRITY_FAILURE, "/original/profile")
        if (
            self.normalized.mime is not ImageMime.PNG
            or self.normalized.profile != NORMALIZATION_PROFILE
        ):
            _fail(VisualContractErrorCode.INTEGRITY_FAILURE, "/normalized/profile")
        for field_name, enum_type in (
            ("view_role", ViewRole),
            ("calibration_status", CalibrationStatus),
        ):
            current = getattr(self, field_name)
            if type(current) is not enum_type:
                try:
                    object.__setattr__(self, field_name, enum_type(current))
                except (TypeError, ValueError):
                    _fail(VisualContractErrorCode.INVALID_INPUT, f"/{field_name}")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "original": self.original.to_mapping(),
            "normalized": self.normalized.to_mapping(),
            "view_role": self.view_role.value,
            "calibration_status": self.calibration_status.value,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _exact_mapping(
            value,
            {"schema_version", "original", "normalized", "view_role", "calibration_status"},
        )
        return cls(
            schema_version=data["schema_version"],
            original=ImageRef.from_mapping(data["original"]),
            normalized=ImageRef.from_mapping(data["normalized"]),
            view_role=data["view_role"],
            calibration_status=data["calibration_status"],
        )


def _manifest_body(value: ImageSet) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "image_set_id": value.id,
        "create_key_digest": value.create_key_digest,
        "inputs": [item.to_mapping() for item in value.inputs],
        "unit": value.unit,
        "dimension_hints": [item.to_mapping() for item in value.dimension_hints],
        "calibration_evidence": [item.to_mapping() for item in value.calibration_evidence],
        "same_object": value.same_object,
        "same_state": value.same_state,
        "same_scale": value.same_scale,
        "processing_authorization": value.processing_authorization.value,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class ImageSet:
    id: str
    create_key_digest: str
    inputs: tuple[VisualInput, ...]
    unit: str | None
    dimension_hints: tuple[DimensionHint, ...]
    calibration_evidence: tuple[CalibrationEvidence, ...]
    same_object: bool
    same_state: bool
    same_scale: bool
    processing_authorization: ProcessingAuthorization
    manifest_sha256: str = ""
    schema_version: int = VISUAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "id", _identifier(self.id, _IMAGE_SET_ID, "/id"))
        object.__setattr__(
            self,
            "create_key_digest",
            _digest(self.create_key_digest, "/create_key_digest"),
        )
        expected_id = (
            "image_set_"
            + hashlib.sha256(
                _IMAGE_SET_ID_DOMAIN + bytes.fromhex(self.create_key_digest)
            ).hexdigest()[:32]
        )
        if self.id != expected_id:
            _fail(VisualContractErrorCode.INTEGRITY_FAILURE, "/id")
        if not isinstance(self.inputs, Sequence) or isinstance(self.inputs, (str, bytes)):
            _fail(VisualContractErrorCode.INVALID_INPUT, "/inputs")
        inputs = tuple(self.inputs)
        if not 1 <= len(inputs) <= MAX_IMAGE_SET_ITEMS or any(
            type(item) is not VisualInput for item in inputs
        ):
            _fail(VisualContractErrorCode.INVALID_INPUT, "/inputs")
        ids = tuple(
            identifier for item in inputs for identifier in (item.original.id, item.normalized.id)
        )
        if len(set(ids)) != len(ids):
            _fail(VisualContractErrorCode.INTEGRITY_FAILURE, "/inputs")
        for index, item in enumerate(inputs):
            if item.original.id != _visual_input_identity_for_set(self.id, index, "original"):
                _fail(VisualContractErrorCode.INTEGRITY_FAILURE, f"/inputs/{index}/original/id")
            if item.normalized.id != _visual_input_identity_for_set(self.id, index, "normalized"):
                _fail(VisualContractErrorCode.INTEGRITY_FAILURE, f"/inputs/{index}/normalized/id")
        source_bytes = sum(item.original.size_bytes for item in inputs)
        source_pixels = sum(item.original.width * item.original.height for item in inputs)
        if source_bytes > MAX_IMAGE_SET_SOURCE_BYTES or source_pixels > MAX_IMAGE_SET_PIXELS:
            _fail(VisualContractErrorCode.BUDGET_EXCEEDED, "/inputs")
        physical_bytes = sum(
            item.original.size_bytes + item.normalized.size_bytes for item in inputs
        )
        if physical_bytes > MAX_IMAGE_SET_PHYSICAL_BYTES:
            _fail(VisualContractErrorCode.BUDGET_EXCEEDED, "/inputs")
        object.__setattr__(self, "inputs", inputs)
        if self.unit is not None and (type(self.unit) is not str or self.unit != "mm"):
            _fail(VisualContractErrorCode.INVALID_INPUT, "/unit")
        if not isinstance(self.dimension_hints, Sequence) or isinstance(
            self.dimension_hints, (str, bytes)
        ):
            _fail(VisualContractErrorCode.INVALID_INPUT, "/dimension_hints")
        hints = tuple(self.dimension_hints)
        if len(hints) > MAX_DIMENSION_HINTS or any(
            type(item) is not DimensionHint for item in hints
        ):
            _fail(VisualContractErrorCode.INVALID_INPUT, "/dimension_hints")
        if any(item.source_index >= len(inputs) for item in hints):
            _fail(VisualContractErrorCode.INVALID_INPUT, "/dimension_hints")
        object.__setattr__(self, "dimension_hints", hints)
        if not isinstance(self.calibration_evidence, Sequence) or isinstance(
            self.calibration_evidence, (str, bytes)
        ):
            _fail(VisualContractErrorCode.INVALID_INPUT, "/calibration_evidence")
        evidence = tuple(self.calibration_evidence)
        if len(evidence) > MAX_CALIBRATION_EVIDENCE or any(
            type(item) is not CalibrationEvidence for item in evidence
        ):
            _fail(VisualContractErrorCode.INVALID_INPUT, "/calibration_evidence")
        if any(item.source_index >= len(inputs) for item in evidence):
            _fail(VisualContractErrorCode.INVALID_INPUT, "/calibration_evidence")
        identities = tuple((item.source_index, item.kind) for item in evidence)
        if len(set(identities)) != len(identities):
            _fail(VisualContractErrorCode.INVALID_INPUT, "/calibration_evidence")
        for index, item in enumerate(inputs):
            kinds = {entry.kind for entry in evidence if entry.source_index == index}
            required = {
                CalibrationStatus.UNKNOWN: set(),
                CalibrationStatus.EXPLICIT_SCALE: {CalibrationKind.SCALE},
                CalibrationStatus.CALIBRATED: {CalibrationKind.CAMERA_INTRINSICS},
            }[item.calibration_status]
            if not required.issubset(kinds) or (
                item.calibration_status is CalibrationStatus.UNKNOWN and kinds
            ):
                _fail(
                    VisualContractErrorCode.INVALID_INPUT,
                    f"/calibration_evidence/{index}",
                )
        object.__setattr__(self, "calibration_evidence", evidence)
        for name in ("same_object", "same_state", "same_scale"):
            if type(getattr(self, name)) is not bool:
                _fail(VisualContractErrorCode.INVALID_INPUT, f"/{name}")
        if type(self.processing_authorization) is not ProcessingAuthorization:
            try:
                object.__setattr__(
                    self,
                    "processing_authorization",
                    ProcessingAuthorization(self.processing_authorization),
                )
            except (TypeError, ValueError):
                _fail(VisualContractErrorCode.INVALID_INPUT, "/processing_authorization")
        expected = hashlib.sha256(
            _MANIFEST_DOMAIN + _canonical_json(_manifest_body(self))
        ).hexdigest()
        if self.manifest_sha256 and self.manifest_sha256 != expected:
            _fail(VisualContractErrorCode.INTEGRITY_FAILURE, "/manifest_sha256")
        object.__setattr__(self, "manifest_sha256", expected)

    def to_mapping(self) -> dict[str, object]:
        return _manifest_body(self) | {"manifest_sha256": self.manifest_sha256}

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _exact_mapping(
            value,
            {
                "schema_version",
                "image_set_id",
                "create_key_digest",
                "inputs",
                "unit",
                "dimension_hints",
                "calibration_evidence",
                "same_object",
                "same_state",
                "same_scale",
                "processing_authorization",
                "manifest_sha256",
            },
        )
        inputs = data["inputs"]
        hints = data["dimension_hints"]
        evidence = data["calibration_evidence"]
        if (
            not isinstance(inputs, list)
            or not isinstance(hints, list)
            or not isinstance(evidence, list)
        ):
            _fail(VisualContractErrorCode.INVALID_INPUT)
        return cls(
            schema_version=data["schema_version"],
            id=data["image_set_id"],
            create_key_digest=data["create_key_digest"],
            inputs=tuple(VisualInput.from_mapping(item) for item in inputs),
            unit=data["unit"],
            dimension_hints=tuple(DimensionHint.from_mapping(item) for item in hints),
            calibration_evidence=tuple(CalibrationEvidence.from_mapping(item) for item in evidence),
            same_object=data["same_object"],
            same_state=data["same_state"],
            same_scale=data["same_scale"],
            processing_authorization=data["processing_authorization"],
            manifest_sha256=data["manifest_sha256"],
        )


def encode_image_set(value: ImageSet) -> bytes:
    if type(value) is not ImageSet:
        raise TypeError("value must be an exact ImageSet")
    return _canonical_json(value.to_mapping())


def decode_image_set(raw: object) -> ImageSet:
    if type(raw) is not bytes or not raw or len(raw) > MAX_IMAGE_SET_RECORD_BYTES:
        _fail(VisualContractErrorCode.BUDGET_EXCEEDED)
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        _fail(VisualContractErrorCode.INVALID_INPUT)
    result = ImageSet.from_mapping(value)
    if encode_image_set(result) != raw:
        _fail(VisualContractErrorCode.INTEGRITY_FAILURE)
    return result
