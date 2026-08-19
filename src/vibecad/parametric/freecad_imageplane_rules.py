"""Reviewed FreeCAD rules for authenticated editable reference-image planes.

Plans carry only a content-bound image reference plus bounded dimensions and
placement.  Artifact bytes cross the trusted ``ArtifactReader`` boundary, are
validated against their exact media contract, and are written to a private
0600 staging file.  ``Image::ImagePlane`` then copies those bytes into the
Session-owned ``Document.TransientDir`` through ``App::PropertyFileIncluded``.

The retained copy is deliberately *not* described as detached: its
content-addressed filename and digest are revalidated, FCStd embeds it on save,
and ``DocumentAssetWorkspace`` owns its live-document extraction lifetime.
Neither plans nor receipts contain a host path or grant execution authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
import struct
import tempfile
import zlib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Self

from vibecad.engine.document_assets import (
    DocumentAssetWorkspace,
    DocumentAssetWorkspaceError,
)
from vibecad.intent_bridge.contracts import DocumentRef, IntentBridgeError
from vibecad.intent_bridge.ports import ArtifactReader, read_verified_document
from vibecad.parametric.freecad_reviewed_transaction import (
    NativeTransactionError,
    NativeTransactionErrorCode,
    NativeTransactionRunner,
)

IMAGEPLANE_PLAN_SCHEMA_VERSION: Final = 1
IMAGEPLANE_PLAN_MEDIA_TYPE: Final = "application/vnd.vibecad.freecad-imageplane-plan+json"
MAX_IMAGEPLANE_PLAN_BYTES: Final = 64 * 1024
# Keep the private rule within the existing ArtifactReader wire budget.  The
# ingestion layer is expected to normalize large originals into a bounded
# reference-image artifact before constructing this graph.
MAX_IMAGEPLANE_ARTIFACT_BYTES: Final = 4 * 1024 * 1024
MAX_IMAGE_DIMENSION_PIXELS: Final = 100_000
IMAGEPLANE_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"
IMAGEPLANE_RULE_ID: Final = "freecad.imageplane.reviewed.v1"

_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-imageplane.rule-contract.v1\0"
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-imageplane.plan.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-imageplane.receipt.v1\0"
_BINDING_DIGEST_DOMAIN = b"vibecad.freecad-imageplane.binding.v1\0"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_KEY_PROPERTY = "VibeCADImagePlaneKey"
_IMAGE_GRAPH_PROPERTY = "VibeCADImagePlaneGraphId"
_IMAGE_NODE_PROPERTY = "VibeCADImagePlaneNodeId"
_BINDING_PROPERTIES = (_IMAGE_KEY_PROPERTY, _IMAGE_GRAPH_PROPERTY, _IMAGE_NODE_PROPERTY)
_MAX_WORKSPACE_ENTRIES = 4096
_MAX_WORKSPACE_DEPTH = 16
_MAX_WORKSPACE_FILE_BYTES = 64 * 1024 * 1024
_MAX_WORKSPACE_TOTAL_BYTES = 256 * 1024 * 1024


class ImagePlaneRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"
    ROLLBACK_FAILED = "rollback_failed"
    STAGING_FAILED = "staging_failed"


class ImagePlaneRuleError(ValueError):
    """Bounded stable failure at the reviewed ImagePlane boundary."""

    def __init__(self, code: ImagePlaneRuleErrorCode, path: str = "/") -> None:
        if type(code) is not ImagePlaneRuleErrorCode:
            raise TypeError("code must be an ImagePlaneRuleErrorCode")
        try:
            size = len(path.encode("utf-8")) if type(path) is str else 0
        except UnicodeError:
            size = 385
        if (
            type(path) is not str
            or not path.startswith("/")
            or not path.isprintable()
            or len(path.splitlines()) != 1
            or size > 384
        ):
            path = "/"
        self.code = code
        self.path = path
        super().__init__(f"ImagePlane rule error ({code.value}) at {path}")


def _fail(code: ImagePlaneRuleErrorCode, path: str = "/") -> None:
    raise ImagePlaneRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or len(value) > 128 or _IDENTIFIER.fullmatch(value) is None:
        _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, path)
    return value


def _number(
    value: object,
    path: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if type(value) not in (int, float):
        _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, path)
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, path)
    return result


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, path)
    return value


def _canonical_json(value: object, *, maximum: int = MAX_IMAGEPLANE_PLAN_BYTES) -> bytes:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/")
    if not payload or len(payload) > maximum:
        _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/")
    return payload


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _decode_mapping(raw: object, *, maximum: int) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/")
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/")
    if type(value) is not dict or not hmac.compare_digest(
        raw,
        _canonical_json(value, maximum=maximum),
    ):
        _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/")
    return value


@dataclass(frozen=True, slots=True)
class ImagePlaneArtifactSpec:
    media_type: str
    schema_term_ref_id: str
    suffix: str


IMAGEPLANE_ARTIFACT_SPECS: Final = MappingProxyType(
    {
        "image/jpeg": ImagePlaneArtifactSpec(
            media_type="image/jpeg",
            schema_term_ref_id="schema_imageplane_jpeg_artifact_v1",
            suffix=".jpg",
        ),
        "image/png": ImagePlaneArtifactSpec(
            media_type="image/png",
            schema_term_ref_id="schema_imageplane_png_artifact_v1",
            suffix=".png",
        ),
    }
)
IMAGEPLANE_ARTIFACT_ROLE_TERM_REF_ID: Final = "role_imageplane_artifact"
IMAGEPLANE_ARTIFACT_VALUE_TYPE_TERM_REF_ID: Final = "type_imageplane_raster_artifact"


def _validated_configuration(value: object) -> dict[str, object]:
    fields = _exact_fields(
        value,
        {"media_type", "x_size_mm", "y_size_mm", "placement"},
        "/configuration",
    )
    media_type = fields["media_type"]
    if type(media_type) is not str or media_type not in IMAGEPLANE_ARTIFACT_SPECS:
        _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/configuration/media_type")
    placement = _exact_fields(
        fields["placement"],
        {"position_mm", "axis", "angle_degrees"},
        "/configuration/placement",
    )

    def vector(raw: object, path: str, *, bound: float) -> tuple[float, float, float]:
        if type(raw) is not list or len(raw) != 3:
            _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, path)
        return tuple(
            _number(item, f"{path}/{index}", minimum=-bound, maximum=bound)
            for index, item in enumerate(raw)
        )

    position = vector(
        placement["position_mm"],
        "/configuration/placement/position_mm",
        bound=1e9,
    )
    axis = vector(placement["axis"], "/configuration/placement/axis", bound=1.0)
    norm = math.sqrt(sum(item * item for item in axis))
    if not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1e-9):
        _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/configuration/placement/axis")
    return {
        "media_type": media_type,
        "x_size_mm": _number(
            fields["x_size_mm"],
            "/configuration/x_size_mm",
            minimum=1e-6,
            maximum=1e9,
        ),
        "y_size_mm": _number(
            fields["y_size_mm"],
            "/configuration/y_size_mm",
            minimum=1e-6,
            maximum=1e9,
        ),
        "placement": {
            "position_mm": list(position),
            "axis": list(axis),
            "angle_degrees": _number(
                placement["angle_degrees"],
                "/configuration/placement/angle_degrees",
                minimum=-360.0,
                maximum=360.0,
            ),
        },
    }


def encode_imageplane_configuration(value: object) -> bytes:
    return _canonical_json(_validated_configuration(value), maximum=16 * 1024)


def _contract_mapping() -> dict[str, object]:
    return {
        "engine": {
            "name": "FreeCAD",
            "version": "1.1.0",
            "build_id": IMAGEPLANE_FREECAD_ENGINE_BUILD_ID,
        },
        "operation": {
            "semantic": "place-or-edit-authenticated-reference-image-plane",
            "type_id": "Image::ImagePlane",
            "properties": ["ImageFile", "Placement", "XSize", "YSize"],
            "binding_properties": list(_BINDING_PROPERTIES),
        },
        "artifacts": [
            {
                "media_type": item.media_type,
                "schema_term_ref_id": item.schema_term_ref_id,
                "suffix": item.suffix,
                "maximum_bytes": MAX_IMAGEPLANE_ARTIFACT_BYTES,
            }
            for item in IMAGEPLANE_ARTIFACT_SPECS.values()
        ],
        "fixed": {
            "artifact_resolution": "ArtifactReader-exact-sha256-media",
            "staging": "host-owned-private-0700-file-0600-O_NOFOLLOW",
            "retention": "content-addressed-App::PropertyFileIncluded-in-Document.TransientDir",
            "durability": "FCStd-embedded;not-detached",
            "transaction": "shared-native-transaction-exact-rollback",
            "native_selection": "static-reviewed-Image::ImagePlane",
        },
    }


IMAGEPLANE_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN + _canonical_json(_contract_mapping())
).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class ImagePlaneBackendPlan:
    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    manifest_sha256: str
    operation_specification_sha256: str
    container_id: str
    node_id: str
    result_id: str
    artifact_id: str
    artifact_content_sha256: str
    artifact_schema_term_ref_id: str
    artifact_media_type: str
    configuration_bytes: bytes
    schema_version: int = IMAGEPLANE_PLAN_SCHEMA_VERSION
    canonical_bytes: bytes = field(init=False, repr=False)
    plan_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != IMAGEPLANE_PLAN_SCHEMA_VERSION
        ):
            _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/schema_version")
        for name in (
            "source_artifact_id",
            "source_graph_id",
            "container_id",
            "node_id",
            "result_id",
            "artifact_id",
            "artifact_schema_term_ref_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in (
            "source_graph_sha256",
            "source_content_sha256",
            "lowering_request_sha256",
            "adapter_contract_sha256",
            "manifest_sha256",
            "operation_specification_sha256",
            "artifact_content_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if type(self.configuration_bytes) is not bytes:
            _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/configuration")
        canonical_configuration = encode_imageplane_configuration(self.configuration)
        if not hmac.compare_digest(canonical_configuration, self.configuration_bytes):
            _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/configuration")
        spec = IMAGEPLANE_ARTIFACT_SPECS[self.configuration["media_type"]]
        if (
            self.artifact_media_type != spec.media_type
            or self.artifact_schema_term_ref_id != spec.schema_term_ref_id
        ):
            _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/contract")
        payload = _canonical_json(self.to_mapping())
        object.__setattr__(self, "canonical_bytes", payload)
        object.__setattr__(
            self,
            "plan_sha256",
            hashlib.sha256(_PLAN_DIGEST_DOMAIN + payload).hexdigest(),
        )

    @property
    def configuration(self) -> dict[str, object]:
        return _validated_configuration(
            _decode_mapping(self.configuration_bytes, maximum=16 * 1024)
        )

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority": "none",
            "source": {
                "artifact_id": self.source_artifact_id,
                "graph_id": self.source_graph_id,
                "graph_sha256": self.source_graph_sha256,
                "content_sha256": self.source_content_sha256,
            },
            "lowering_request_sha256": self.lowering_request_sha256,
            "adapter_contract_sha256": self.adapter_contract_sha256,
            "manifest_sha256": self.manifest_sha256,
            "operation_specification_sha256": self.operation_specification_sha256,
            "target": {
                "container_id": self.container_id,
                "node_id": self.node_id,
                "result_id": self.result_id,
            },
            "artifact": {
                "artifact_id": self.artifact_id,
                "content_sha256": self.artifact_content_sha256,
                "role_term_ref_id": IMAGEPLANE_ARTIFACT_ROLE_TERM_REF_ID,
                "schema_term_ref_id": self.artifact_schema_term_ref_id,
                "value_type_term_ref_id": IMAGEPLANE_ARTIFACT_VALUE_TYPE_TERM_REF_ID,
                "media_type": self.artifact_media_type,
            },
            "operation": {
                "kind": "place_or_edit_image_plane",
                "configuration": self.configuration,
            },
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        fields = _exact_fields(
            value,
            {
                "schema_version",
                "authority",
                "source",
                "lowering_request_sha256",
                "adapter_contract_sha256",
                "manifest_sha256",
                "operation_specification_sha256",
                "target",
                "artifact",
                "operation",
            },
            "/",
        )
        if fields["authority"] != "none":
            _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/authority")
        source = _exact_fields(
            fields["source"],
            {"artifact_id", "graph_id", "graph_sha256", "content_sha256"},
            "/source",
        )
        target = _exact_fields(
            fields["target"],
            {"container_id", "node_id", "result_id"},
            "/target",
        )
        artifact = _exact_fields(
            fields["artifact"],
            {
                "artifact_id",
                "content_sha256",
                "role_term_ref_id",
                "schema_term_ref_id",
                "value_type_term_ref_id",
                "media_type",
            },
            "/artifact",
        )
        operation = _exact_fields(fields["operation"], {"kind", "configuration"}, "/operation")
        if (
            operation["kind"] != "place_or_edit_image_plane"
            or artifact["role_term_ref_id"] != IMAGEPLANE_ARTIFACT_ROLE_TERM_REF_ID
            or artifact["value_type_term_ref_id"] != IMAGEPLANE_ARTIFACT_VALUE_TYPE_TERM_REF_ID
        ):
            _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/contract")
        return cls(
            schema_version=fields["schema_version"],
            source_artifact_id=source["artifact_id"],
            source_graph_id=source["graph_id"],
            source_graph_sha256=source["graph_sha256"],
            source_content_sha256=source["content_sha256"],
            lowering_request_sha256=fields["lowering_request_sha256"],
            adapter_contract_sha256=fields["adapter_contract_sha256"],
            manifest_sha256=fields["manifest_sha256"],
            operation_specification_sha256=fields["operation_specification_sha256"],
            container_id=target["container_id"],
            node_id=target["node_id"],
            result_id=target["result_id"],
            artifact_id=artifact["artifact_id"],
            artifact_content_sha256=artifact["content_sha256"],
            artifact_schema_term_ref_id=artifact["schema_term_ref_id"],
            artifact_media_type=artifact["media_type"],
            configuration_bytes=encode_imageplane_configuration(operation["configuration"]),
        )


def decode_imageplane_backend_plan(
    raw: bytes,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> ImagePlaneBackendPlan:
    plan = ImagePlaneBackendPlan.from_mapping(
        _decode_mapping(raw, maximum=MAX_IMAGEPLANE_PLAN_BYTES)
    )
    if not hmac.compare_digest(raw, plan.canonical_bytes):
        _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(),
        _digest(expected_content_sha256, "/expected_content_sha256"),
    ):
        _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        plan.plan_sha256,
        _digest(expected_plan_sha256, "/expected_plan_sha256"),
    ):
        _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return plan


def _validate_png(payload: bytes) -> None:
    if len(payload) < 45 or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")
    index = 8
    seen_header = False
    seen_image_data = False
    while index < len(payload):
        if index + 12 > len(payload):
            _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")
        length = struct.unpack(">I", payload[index : index + 4])[0]
        chunk_type = payload[index + 4 : index + 8]
        end = index + 12 + length
        if end > len(payload) or any(
            not (65 <= item <= 90 or 97 <= item <= 122) for item in chunk_type
        ):
            _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")
        chunk = payload[index + 8 : index + 8 + length]
        crc = struct.unpack(">I", payload[index + 8 + length : end])[0]
        if zlib.crc32(chunk_type + chunk) & 0xFFFFFFFF != crc:
            _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")
        if not seen_header:
            if chunk_type != b"IHDR" or length != 13:
                _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")
            width, height = struct.unpack(">II", chunk[:8])
            if (
                not 1 <= width <= MAX_IMAGE_DIMENSION_PIXELS
                or not 1 <= height <= MAX_IMAGE_DIMENSION_PIXELS
                or chunk[10] != 0
                or chunk[11] != 0
                or chunk[12] not in (0, 1)
            ):
                _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")
            seen_header = True
        elif chunk_type == b"IHDR":
            _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")
        if chunk_type == b"IDAT":
            if not length:
                _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")
            seen_image_data = True
        if chunk_type == b"IEND":
            if length or not seen_image_data or end != len(payload):
                _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")
            return
        index = end
    _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")


_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _validate_jpeg(payload: bytes) -> None:
    if len(payload) < 12 or payload[:2] != b"\xff\xd8" or payload[-2:] != b"\xff\xd9":
        _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")
    index = 2
    found_sof = False
    while index < len(payload) - 2:
        if payload[index] != 0xFF:
            _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")
        while index < len(payload) and payload[index] == 0xFF:
            index += 1
        if index >= len(payload):
            break
        marker = payload[index]
        index += 1
        if marker in {0x01, *range(0xD0, 0xD9)}:
            continue
        if marker == 0xD9:
            break
        if index + 2 > len(payload):
            _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")
        segment_length = struct.unpack(">H", payload[index : index + 2])[0]
        if segment_length < 2 or index + segment_length > len(payload):
            _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")
        if marker in _JPEG_SOF_MARKERS:
            segment = payload[index + 2 : index + segment_length]
            if len(segment) < 6:
                _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")
            height, width = struct.unpack(">HH", segment[1:5])
            if (
                not 1 <= width <= MAX_IMAGE_DIMENSION_PIXELS
                or not 1 <= height <= MAX_IMAGE_DIMENSION_PIXELS
                or not 1 <= segment[5] <= 4
            ):
                _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")
            found_sof = True
        if marker == 0xDA:
            break
        index += segment_length
    if not found_sof:
        _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/artifact/media")


def validate_imageplane_artifact_payload(payload: bytes, media_type: str) -> None:
    if type(payload) is not bytes or not 1 <= len(payload) <= MAX_IMAGEPLANE_ARTIFACT_BYTES:
        _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/artifact/payload")
    if media_type == "image/png":
        _validate_png(payload)
    elif media_type == "image/jpeg":
        _validate_jpeg(payload)
    else:
        _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/artifact/media_type")


def _private_root_identity(path: object) -> tuple[Path, int, int]:
    if not isinstance(path, Path) or not path.is_absolute():
        _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/stager/root")
    try:
        info = path.lstat()
    except (OSError, ValueError, RuntimeError):
        _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/stager/root")
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/stager/root")
    return path, info.st_dev, info.st_ino


class _StagedImageLease:
    __slots__ = ("_active", "_directory", "_path")

    def __init__(self, directory: Path, path: Path) -> None:
        self._active = True
        self._directory = directory
        self._path = path

    @property
    def path(self) -> Path:
        if not self._active:
            _fail(ImagePlaneRuleErrorCode.STAGING_FAILED, "/stager/lease")
        return self._path

    def verify(self) -> None:
        if not self._active:
            _fail(ImagePlaneRuleErrorCode.STAGING_FAILED, "/stager/lease")
        try:
            directory_info = self._directory.lstat()
            info = self._path.lstat()
        except (OSError, ValueError, RuntimeError):
            _fail(ImagePlaneRuleErrorCode.STAGING_FAILED, "/stager/lease")
        if (
            stat.S_ISLNK(directory_info.st_mode)
            or not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.geteuid()
            or stat.S_IMODE(directory_info.st_mode) != 0o700
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or self._path.parent != self._directory
        ):
            _fail(ImagePlaneRuleErrorCode.STAGING_FAILED, "/stager/lease")

    def close(self) -> None:
        if not self._active:
            return
        self.verify()
        try:
            self._path.unlink()
            self._directory.rmdir()
        except OSError:
            _fail(ImagePlaneRuleErrorCode.STAGING_FAILED, "/stager/cleanup")
        self._active = False
        if self._path.exists() or self._directory.exists():
            _fail(ImagePlaneRuleErrorCode.STAGING_FAILED, "/stager/cleanup")

    def __enter__(self) -> Self:
        self.verify()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        del exc_type, exc, traceback
        if self._active:
            self.close()
        return False


class HostOwnedImageStager:
    """Trusted private source staging; graph and plan never select its root."""

    __slots__ = ("_device", "_inode", "_root")

    def __init__(self, root: Path) -> None:
        checked, device, inode = _private_root_identity(root)
        self._root = checked
        self._device = device
        self._inode = inode

    def require_root(self) -> Path:
        root, device, inode = _private_root_identity(self._root)
        if device != self._device or inode != self._inode:
            _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/stager/root")
        return root

    def stage_exact(
        self,
        payload: bytes,
        *,
        suffix: str,
        expected_content_sha256: str,
    ) -> _StagedImageLease:
        if (
            type(payload) is not bytes
            or not 1 <= len(payload) <= MAX_IMAGEPLANE_ARTIFACT_BYTES
            or suffix not in {item.suffix for item in IMAGEPLANE_ARTIFACT_SPECS.values()}
        ):
            _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/stager/payload")
        expected = _digest(expected_content_sha256, "/stager/content_sha256")
        if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), expected):
            _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/stager/content_sha256")
        root = self.require_root()
        directory: Path | None = None
        staged: Path | None = None
        descriptor: int | None = None
        transferred = False
        try:
            directory = Path(tempfile.mkdtemp(prefix=".vibecad-image-stage-", dir=root))
            os.chmod(directory, 0o700)
            staged = directory / f"source{suffix}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(staged, flags, 0o600)
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short staging write")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            lease = _StagedImageLease(directory, staged)
            lease.verify()
            transferred = True
            return lease
        except ImagePlaneRuleError:
            raise
        except (OSError, ValueError, RuntimeError, SystemExit):
            _fail(ImagePlaneRuleErrorCode.STAGING_FAILED, "/stager/write")
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if not transferred and staged is not None:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    pass
            if not transferred and directory is not None:
                try:
                    directory.rmdir()
                except OSError:
                    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class ImagePlaneExecutionBindings:
    document: object
    document_assets: DocumentAssetWorkspace
    artifact_document: DocumentRef
    artifacts: ArtifactReader
    stager: HostOwnedImageStager
    container_id: str
    expected_adapter_contract_sha256: str
    expected_manifest_sha256: str
    expected_operation_specification_sha256: str

    def __post_init__(self) -> None:
        if self.document is None:
            _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/bindings/document")
        if type(self.document_assets) is not DocumentAssetWorkspace:
            _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/bindings/document_assets")
        if type(self.artifact_document) is not DocumentRef or not isinstance(
            self.artifacts,
            ArtifactReader,
        ):
            _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/bindings/artifact")
        if type(self.stager) is not HostOwnedImageStager:
            _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/bindings/stager")
        object.__setattr__(
            self,
            "container_id",
            _identifier(self.container_id, "/bindings/container"),
        )
        for name in (
            "expected_adapter_contract_sha256",
            "expected_manifest_sha256",
            "expected_operation_specification_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/bindings/{name}"))


@dataclass(frozen=True, slots=True, kw_only=True)
class ImagePlaneConformanceReceipt:
    plan_sha256: str
    disposition: str
    object_name: str
    binding_sha256: str
    artifact_id: str
    artifact_content_sha256: str
    artifact_media_type: str
    retained_alias: str
    x_size_mm: float
    y_size_mm: float
    position_mm: tuple[float, float, float]
    rotation_quaternion: tuple[float, float, float, float]
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/receipt/plan"))
        if self.disposition not in {"created", "updated"}:
            _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/receipt/disposition")
        object.__setattr__(self, "object_name", _identifier(self.object_name, "/receipt/object"))
        object.__setattr__(
            self,
            "binding_sha256",
            _digest(self.binding_sha256, "/receipt/binding_sha256"),
        )
        object.__setattr__(self, "artifact_id", _identifier(self.artifact_id, "/receipt/artifact"))
        object.__setattr__(
            self,
            "artifact_content_sha256",
            _digest(self.artifact_content_sha256, "/receipt/artifact_sha256"),
        )
        spec = IMAGEPLANE_ARTIFACT_SPECS.get(self.artifact_media_type)
        if spec is None or self.retained_alias != self.artifact_content_sha256 + spec.suffix:
            _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/receipt/retained_alias")
        if (
            type(self.x_size_mm) not in (int, float)
            or type(self.y_size_mm) not in (int, float)
            or not math.isfinite(float(self.x_size_mm))
            or not math.isfinite(float(self.y_size_mm))
            or float(self.x_size_mm) <= 0.0
            or float(self.y_size_mm) <= 0.0
            or type(self.position_mm) is not tuple
            or len(self.position_mm) != 3
            or type(self.rotation_quaternion) is not tuple
            or len(self.rotation_quaternion) != 4
            or any(
                not math.isfinite(item) for item in (*self.position_mm, *self.rotation_quaternion)
            )
        ):
            _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/receipt/placement")
        body = {
            "authority": "none",
            "plan_sha256": self.plan_sha256,
            "disposition": self.disposition,
            "object_name": self.object_name,
            "binding_sha256": self.binding_sha256,
            "artifact": {
                "artifact_id": self.artifact_id,
                "content_sha256": self.artifact_content_sha256,
                "media_type": self.artifact_media_type,
                "retained_alias": self.retained_alias,
            },
            "dimensions_mm": {"x": self.x_size_mm, "y": self.y_size_mm},
            "placement": {
                "position_mm": list(self.position_mm),
                "rotation_quaternion": list(self.rotation_quaternion),
            },
        }
        object.__setattr__(
            self,
            "receipt_sha256",
            hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest(),
        )

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _binding_sha256(plan: ImagePlaneBackendPlan) -> str:
    return hashlib.sha256(
        b"\0".join(
            (
                _BINDING_DIGEST_DOMAIN,
                plan.source_graph_id.encode("ascii"),
                plan.node_id.encode("ascii"),
            )
        )
    ).hexdigest()


def _object_name(plan: ImagePlaneBackendPlan) -> str:
    return f"ImagePlane_{_binding_sha256(plan)[:16]}"


def _placement_signature(placement: object) -> tuple[float, ...]:
    return (
        float(placement.Base.x),
        float(placement.Base.y),
        float(placement.Base.z),
        *(float(item) for item in placement.Rotation.Q),
    )


def _expected_quaternion(config: dict[str, object]) -> tuple[float, float, float, float]:
    placement = config["placement"]
    axis = placement["axis"]
    half_angle = math.radians(placement["angle_degrees"]) / 2.0
    sine = math.sin(half_angle)
    return (
        axis[0] * sine,
        axis[1] * sine,
        axis[2] * sine,
        math.cos(half_angle),
    )


def _placement_matches(placement: object, config: dict[str, object]) -> bool:
    try:
        signature = _placement_signature(placement)
        expected_position = tuple(config["placement"]["position_mm"])
        expected_q = _expected_quaternion(config)
    except (Exception, SystemExit):
        return False
    same = all(
        math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9)
        for actual, expected in zip(signature[3:], expected_q, strict=True)
    )
    negated = all(
        math.isclose(actual, -expected, rel_tol=0.0, abs_tol=1e-9)
        for actual, expected in zip(signature[3:], expected_q, strict=True)
    )
    return all(
        math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9)
        for actual, expected in zip(signature[:3], expected_position, strict=True)
    ) and (same or negated)


def _read_regular_digest(
    path: Path,
    *,
    maximum: int,
    minimum: int = 1,
) -> tuple[int, str]:
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_mode & 0o077
            or not minimum <= before.st_size <= maximum
        ):
            _fail(ImagePlaneRuleErrorCode.CONFORMANCE_FAILED, "/result/image_file")
        remaining = before.st_size
        digest = hashlib.sha256()
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                _fail(ImagePlaneRuleErrorCode.CONFORMANCE_FAILED, "/result/image_file")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _fail(ImagePlaneRuleErrorCode.CONFORMANCE_FAILED, "/result/image_file")
        after = os.fstat(descriptor)
        live = path.lstat()
        identity = lambda item: (  # noqa: E731
            item.st_dev,
            item.st_ino,
            item.st_size,
            item.st_mtime_ns,
            stat.S_IFMT(item.st_mode),
        )
        if identity(before) != identity(after) or identity(after) != identity(live):
            _fail(ImagePlaneRuleErrorCode.CONFORMANCE_FAILED, "/result/image_file")
        return before.st_size, digest.hexdigest()
    except ImagePlaneRuleError:
        raise
    except (OSError, ValueError, RuntimeError, SystemExit):
        _fail(ImagePlaneRuleErrorCode.CONFORMANCE_FAILED, "/result/image_file")
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _retained_signature(
    feature: object,
    workspace: Path,
    *,
    alias: str,
    content_sha256: str,
) -> tuple[int, str]:
    try:
        path = Path(str(feature.ImageFile))
    except (Exception, SystemExit):
        _fail(ImagePlaneRuleErrorCode.CONFORMANCE_FAILED, "/result/image_file")
    if path.parent != workspace or path.name != alias:
        _fail(ImagePlaneRuleErrorCode.CONFORMANCE_FAILED, "/result/image_file")
    size, digest = _read_regular_digest(path, maximum=MAX_IMAGEPLANE_ARTIFACT_BYTES)
    if not hmac.compare_digest(digest, content_sha256):
        _fail(ImagePlaneRuleErrorCode.CONFORMANCE_FAILED, "/result/image_file")
    return size, digest


def _workspace_manifest(root: Path) -> tuple[tuple[str, str, int, str], ...]:
    result: list[tuple[str, str, int, str]] = []
    total_bytes = 0

    def visit(directory: Path, depth: int) -> None:
        nonlocal total_bytes
        if depth > _MAX_WORKSPACE_DEPTH:
            _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/document/workspace")
        try:
            entries = tuple(sorted(os.scandir(directory), key=lambda item: item.name))
        except OSError:
            _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/document/workspace")
        for entry in entries:
            if len(result) >= _MAX_WORKSPACE_ENTRIES:
                _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/document/workspace")
            path = Path(entry.path)
            try:
                info = path.lstat()
                relative = str(path.relative_to(root))
            except (OSError, ValueError, RuntimeError):
                _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/document/workspace")
            if stat.S_ISLNK(info.st_mode):
                _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/document/workspace")
            if stat.S_ISDIR(info.st_mode):
                result.append((relative, "directory", 0, ""))
                visit(path, depth + 1)
            elif stat.S_ISREG(info.st_mode):
                size, digest = _read_regular_digest(
                    path,
                    maximum=_MAX_WORKSPACE_FILE_BYTES,
                    minimum=0,
                )
                total_bytes += size
                if total_bytes > _MAX_WORKSPACE_TOTAL_BYTES:
                    _fail(
                        ImagePlaneRuleErrorCode.PRECONDITION_FAILED,
                        "/document/workspace",
                    )
                result.append((relative, "file", size, digest))
            else:
                _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/document/workspace")

    visit(root, 0)
    return tuple(result)


def _binding_values(feature: object) -> tuple[str, str, str]:
    try:
        properties = tuple(feature.PropertiesList)
        if any(name not in properties for name in _BINDING_PROPERTIES):
            _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/result/binding")
        for name in _BINDING_PROPERTIES:
            if (
                feature.getTypeIdOfProperty(name) != "App::PropertyString"
                or not {"ReadOnly", "Hidden"} <= set(feature.getEditorMode(name))
                or "LockDynamic" not in set(feature.getPropertyStatus(name))
            ):
                _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/result/binding")
        return tuple(str(getattr(feature, name)) for name in _BINDING_PROPERTIES)
    except ImagePlaneRuleError:
        raise
    except (Exception, SystemExit):
        _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/result/binding")


def _add_binding_properties(feature: object, plan: ImagePlaneBackendPlan) -> None:
    values = (_binding_sha256(plan), plan.source_graph_id, plan.node_id)
    try:
        for name, value in zip(_BINDING_PROPERTIES, values, strict=True):
            feature.addProperty(
                "App::PropertyString",
                name,
                "VibeCAD",
                "Persistent reviewed ImagePlane semantic binding",
                0,
                True,
                True,
                True,
            )
            setattr(feature, name, value)
    except (Exception, SystemExit):
        _fail(ImagePlaneRuleErrorCode.CONFORMANCE_FAILED, "/result/binding")
    if _binding_values(feature) != values:
        _fail(ImagePlaneRuleErrorCode.CONFORMANCE_FAILED, "/result/binding")


def _validate_bound_feature(feature: object, plan: ImagePlaneBackendPlan) -> None:
    expected = (_binding_sha256(plan), plan.source_graph_id, plan.node_id)
    try:
        if (
            feature.TypeId != "Image::ImagePlane"
            or feature.getParentGroup() is not None
            or tuple(feature.ExpressionEngine)
            or not feature.isValid()
            or tuple(feature.State) != ("Up-to-date",)
        ):
            _fail(ImagePlaneRuleErrorCode.CONFORMANCE_FAILED, "/result/state")
    except ImagePlaneRuleError:
        raise
    except (Exception, SystemExit):
        _fail(ImagePlaneRuleErrorCode.CONFORMANCE_FAILED, "/result/state")
    if _binding_values(feature) != expected:
        _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/result/binding")


def _validate_configuration(feature: object, config: dict[str, object]) -> None:
    try:
        if (
            not math.isclose(float(feature.XSize), config["x_size_mm"], abs_tol=1e-9)
            or not math.isclose(float(feature.YSize), config["y_size_mm"], abs_tol=1e-9)
            or not _placement_matches(feature.Placement, config)
        ):
            _fail(ImagePlaneRuleErrorCode.CONFORMANCE_FAILED, "/result/configuration")
    except ImagePlaneRuleError:
        raise
    except (Exception, SystemExit, TypeError):
        _fail(ImagePlaneRuleErrorCode.CONFORMANCE_FAILED, "/result/configuration")


def _verified_artifact_payload(
    plan: ImagePlaneBackendPlan,
    bindings: ImagePlaneExecutionBindings,
) -> bytes:
    document = bindings.artifact_document
    spec = IMAGEPLANE_ARTIFACT_SPECS[plan.artifact_media_type]
    if (
        document.artifact_id != plan.artifact_id
        or not hmac.compare_digest(document.content_sha256, plan.artifact_content_sha256)
        or not hmac.compare_digest(document.document_digest, plan.artifact_content_sha256)
        or document.document_id != f"imageplane_{plan.artifact_content_sha256[:32]}"
        or document.role_term_ref_id != IMAGEPLANE_ARTIFACT_ROLE_TERM_REF_ID
        or document.schema_term_ref_id != spec.schema_term_ref_id
        or document.media_type != spec.media_type
        or document.size_bytes > MAX_IMAGEPLANE_ARTIFACT_BYTES
    ):
        _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/bindings/artifact")
    try:
        payload = read_verified_document(
            bindings.artifacts,
            document,
            maximum_bytes=MAX_IMAGEPLANE_ARTIFACT_BYTES,
        )
    except (IntentBridgeError, SystemExit):
        _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/bindings/artifact/payload")
    validate_imageplane_artifact_payload(payload, spec.media_type)
    return payload


def _validate_execution_bindings(
    plan: ImagePlaneBackendPlan,
    bindings: ImagePlaneExecutionBindings,
) -> tuple[object, Path, bytes]:
    if (
        bindings.container_id != plan.container_id
        or not hmac.compare_digest(
            plan.adapter_contract_sha256,
            bindings.expected_adapter_contract_sha256,
        )
        or not hmac.compare_digest(plan.manifest_sha256, bindings.expected_manifest_sha256)
        or not hmac.compare_digest(
            plan.operation_specification_sha256,
            bindings.expected_operation_specification_sha256,
        )
    ):
        _fail(ImagePlaneRuleErrorCode.INTEGRITY_FAILURE, "/bindings")
    document = bindings.document
    try:
        workspace = bindings.document_assets.require_attached(document)
    except DocumentAssetWorkspaceError:
        _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/bindings/document_assets")
    staging_root = bindings.stager.require_root()
    try:
        if (
            workspace == staging_root
            or workspace.is_relative_to(staging_root)
            or staging_root.is_relative_to(workspace)
        ):
            _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/bindings/stager")
        if getattr(document, "UndoMode", 0) != 1 or bool(document.HasPendingTransaction):
            _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/bindings/document")
    except ImagePlaneRuleError:
        raise
    except (Exception, SystemExit):
        _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/bindings/document")
    return document, workspace, _verified_artifact_payload(plan, bindings)


def apply_imageplane_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: ImagePlaneExecutionBindings,
) -> ImagePlaneConformanceReceipt:
    """Atomically create or edit one stable authenticated ImagePlane."""

    if type(bindings) is not ImagePlaneExecutionBindings:
        _fail(ImagePlaneRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != IMAGEPLANE_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_imageplane_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    document, workspace, artifact_payload = _validate_execution_bindings(plan, bindings)
    config = plan.configuration
    spec = IMAGEPLANE_ARTIFACT_SPECS[plan.artifact_media_type]
    alias = plan.artifact_content_sha256 + spec.suffix
    object_name = _object_name(plan)
    binding_sha256 = _binding_sha256(plan)
    try:
        existing = document.getObject(object_name)
        matches = tuple(
            item
            for item in document.Objects
            if _IMAGE_KEY_PROPERTY in tuple(item.PropertiesList)
            and str(getattr(item, _IMAGE_KEY_PROPERTY)) == binding_sha256
        )
        if existing is None:
            if matches:
                _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/document/binding")
        elif matches != (existing,):
            _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/document/binding")
        if existing is not None:
            _validate_bound_feature(existing, plan)
        before_objects = tuple(document.Objects)
        before_workspace = _workspace_manifest(workspace)
        before_existing = (
            None
            if existing is None
            else (
                float(existing.XSize),
                float(existing.YSize),
                _placement_signature(existing.Placement),
                str(existing.ImageFile),
                _read_regular_digest(
                    Path(str(existing.ImageFile)),
                    maximum=MAX_IMAGEPLANE_ARTIFACT_BYTES,
                ),
                _binding_values(existing),
            )
        )
    except ImagePlaneRuleError:
        raise
    except (Exception, SystemExit):
        _fail(ImagePlaneRuleErrorCode.PRECONDITION_FAILED, "/document")

    holder: list[tuple[object, str]] = []

    def snapshot() -> object:
        return before_objects, before_workspace, before_existing

    def rollback_matches(before: object) -> bool:
        try:
            objects, workspace_manifest, existing_state = before
            live_objects = tuple(document.Objects)
            if (
                len(live_objects) != len(objects)
                or any(
                    actual is not expected
                    for actual, expected in zip(live_objects, objects, strict=True)
                )
                or _workspace_manifest(workspace) != workspace_manifest
            ):
                return False
            restored = document.getObject(object_name)
            if existing_state is None:
                return restored is None
            return (
                restored is existing
                and math.isclose(float(restored.XSize), existing_state[0], abs_tol=1e-9)
                and math.isclose(float(restored.YSize), existing_state[1], abs_tol=1e-9)
                and _placement_signature(restored.Placement) == existing_state[2]
                and str(restored.ImageFile) == existing_state[3]
                and _read_regular_digest(
                    Path(str(restored.ImageFile)),
                    maximum=MAX_IMAGEPLANE_ARTIFACT_BYTES,
                )
                == existing_state[4]
                and _binding_values(restored) == existing_state[5]
            )
        except (Exception, SystemExit):
            return False

    with bindings.stager.stage_exact(
        artifact_payload,
        suffix=spec.suffix,
        expected_content_sha256=plan.artifact_content_sha256,
    ) as lease:

        def apply() -> object:
            feature = existing
            disposition = "updated"
            if feature is None:
                feature = document.addObject("Image::ImagePlane", object_name)
                _add_binding_properties(feature, plan)
                disposition = "created"
            else:
                _validate_bound_feature(feature, plan)
            current_digest = None
            try:
                current_path = Path(str(feature.ImageFile))
                if current_path.parent == workspace and current_path.is_file():
                    current_digest = _read_regular_digest(
                        current_path,
                        maximum=MAX_IMAGEPLANE_ARTIFACT_BYTES,
                    )[1]
            except (ImagePlaneRuleError, OSError, ValueError):
                current_digest = None
            if not hmac.compare_digest(current_digest or "", plan.artifact_content_sha256):
                lease.verify()
                feature.ImageFile = (str(lease.path), alias)
            feature.XSize = config["x_size_mm"]
            feature.YSize = config["y_size_mm"]
            placement = config["placement"]
            feature.Placement = FreeCAD.Placement(
                FreeCAD.Vector(*placement["position_mm"]),
                FreeCAD.Rotation(
                    FreeCAD.Vector(*placement["axis"]),
                    placement["angle_degrees"],
                ),
            )
            document.recompute()
            _validate_bound_feature(feature, plan)
            _validate_configuration(feature, config)
            _retained_signature(
                feature,
                workspace,
                alias=alias,
                content_sha256=plan.artifact_content_sha256,
            )
            lease.close()
            _retained_signature(
                feature,
                workspace,
                alias=alias,
                content_sha256=plan.artifact_content_sha256,
            )
            holder.append((feature, disposition))
            return feature

        try:
            NativeTransactionRunner().run(
                document,
                label="VibeCAD reviewed ImagePlane",
                snapshot=snapshot,
                apply=apply,
                rollback_matches=rollback_matches,
            )
        except NativeTransactionError as error:
            code = (
                ImagePlaneRuleErrorCode.ROLLBACK_FAILED
                if error.code is NativeTransactionErrorCode.ROLLBACK_FAILED
                else ImagePlaneRuleErrorCode.TRANSACTION_FAILED
            )
            _fail(code, "/document/transaction")

    if len(holder) != 1:
        _fail(ImagePlaneRuleErrorCode.CONFORMANCE_FAILED, "/result")
    feature, disposition = holder[0]
    _validate_bound_feature(feature, plan)
    _validate_configuration(feature, config)
    _retained_signature(
        feature,
        workspace,
        alias=alias,
        content_sha256=plan.artifact_content_sha256,
    )
    signature = _placement_signature(feature.Placement)
    return ImagePlaneConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        disposition=disposition,
        object_name=object_name,
        binding_sha256=binding_sha256,
        artifact_id=plan.artifact_id,
        artifact_content_sha256=plan.artifact_content_sha256,
        artifact_media_type=plan.artifact_media_type,
        retained_alias=alias,
        x_size_mm=float(feature.XSize),
        y_size_mm=float(feature.YSize),
        position_mm=signature[:3],
        rotation_quaternion=signature[3:],
    )


__all__ = [
    "IMAGEPLANE_ARTIFACT_ROLE_TERM_REF_ID",
    "IMAGEPLANE_ARTIFACT_SPECS",
    "IMAGEPLANE_ARTIFACT_VALUE_TYPE_TERM_REF_ID",
    "IMAGEPLANE_FREECAD_ENGINE_BUILD_ID",
    "IMAGEPLANE_PLAN_MEDIA_TYPE",
    "IMAGEPLANE_RULE_CONTRACT_SHA256",
    "IMAGEPLANE_RULE_ID",
    "HostOwnedImageStager",
    "ImagePlaneArtifactSpec",
    "ImagePlaneBackendPlan",
    "ImagePlaneConformanceReceipt",
    "ImagePlaneExecutionBindings",
    "ImagePlaneRuleError",
    "ImagePlaneRuleErrorCode",
    "MAX_IMAGEPLANE_ARTIFACT_BYTES",
    "MAX_IMAGEPLANE_PLAN_BYTES",
    "apply_imageplane_plan",
    "decode_imageplane_backend_plan",
    "encode_imageplane_configuration",
    "validate_imageplane_artifact_payload",
]
