"""Descriptor-bound, sealed-only storage for local visual inputs."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import hmac
import io
import json
import os
import re
import stat
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

from vibecad.interaction.storage import SafeRoot, StorageFailure
from vibecad.visual.contracts import (
    MAX_DIMENSION_HINTS,
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SET_ITEMS,
    MAX_IMAGE_SET_PHYSICAL_BYTES,
    MAX_IMAGE_SET_PIXELS,
    MAX_IMAGE_SET_RECORD_BYTES,
    MAX_IMAGE_SET_SOURCE_BYTES,
    MAX_IMAGE_SET_TEMPORARIES,
    MAX_IMAGE_SETS,
    MAX_IMAGE_SOURCE_BYTES,
    MAX_NORMALIZED_IMAGE_BYTES,
    MAX_NORMALIZED_LONG_EDGE,
    MAX_VISUAL_INPUT_STORE_BYTES,
    NORMALIZATION_PROFILE,
    SOURCE_JPEG_PROFILE,
    SOURCE_PNG_PROFILE,
    VISUAL_SCHEMA_VERSION,
    CalibrationEvidence,
    CalibrationKind,
    CalibrationStatus,
    DimensionHint,
    ImageMime,
    ImageRef,
    ImageSet,
    ProcessingAuthorization,
    ViewRole,
    VisualContractError,
    VisualContractErrorCode,
    VisualInput,
    decode_image_set,
    encode_image_set,
    image_set_identity,
    visual_input_identity,
)
from vibecad.workflow.lease import LeaseError, LeaseErrorCode, ResourceLeaseManager

_COPY_CHUNK_BYTES = 64 * 1024
_LEASE_WAIT_SECONDS = 3.0
_LEASE_RETRY_SECONDS = 0.02
_CATALOG_RESOURCE = "visual-input-catalog-v1"
_LOCATOR_DOMAIN = b"vibecad-visual-input-locator-v1\0"
_IMAGE_SET_ID = re.compile(r"^image_set_[0-9a-f]{32}$")
_STAGE_NAME = re.compile(r"^\.stage_[0-9a-f]{32}$")
_DELETE_STAGE_NAME = re.compile(r"^\.delete_[0-9a-f]{32}$")
_DELETE_MARKER_NAME = re.compile(r"^\.deleted_[0-9a-f]{32}\.json$")
_DELETE_MARKER_TEMP_NAME = re.compile(r"^\.delete_marker_[0-9a-f]{32}\.tmp$")
_RETIRED_MARKER_NAME = re.compile(r"^\.retired_[0-9a-f]{32}\.json$")
_RETIRED_MARKER_TEMP_NAME = re.compile(r"^\.retire_marker_[0-9a-f]{32}\.tmp$")
_VISUAL_FILE_NAME = re.compile(r"^visual_input_[0-9a-f]{32}\.(?:jpg|png)$")
_DELETE_MARKER_DOMAIN = b"vibecad-visual-input-delete-v1\0"
_RETIRED_MARKER_DOMAIN = b"vibecad-visual-input-retired-v1\0"
_DELETE_MARKER_FIELDS = {
    "schema_version",
    "image_set_id",
    "manifest_sha256",
    "marker_sha256",
}
_RETIRED_MARKER_FIELDS = {"schema_version", "image_set_id", "retired_sha256"}
_LOCATOR_FIELDS = {
    "schema_version",
    "dev",
    "ino",
    "mode",
    "uid",
    "nlink",
    "size",
    "mtime_ns",
    "ctime_ns",
    "digest",
}


class VisualInputStoreErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTEGRITY_FAILURE = "integrity_failure"
    STORE_FAILURE = "store_failure"
    LEASE_UNAVAILABLE = "lease_unavailable"
    RECOVERY_REQUIRED = "recovery_required"


class VisualInputStoreError(RuntimeError):
    """Bounded visual-input failure with no rejected file metadata."""

    def __init__(self, code: VisualInputStoreErrorCode) -> None:
        if type(code) is not VisualInputStoreErrorCode:
            raise TypeError("code must be an exact VisualInputStoreErrorCode")
        self.code = code
        super().__init__(code.value)


def _raise(code: VisualInputStoreErrorCode) -> None:
    raise VisualInputStoreError(code)


def _contract_error(error: VisualContractError) -> VisualInputStoreError:
    if error.code is VisualContractErrorCode.BUDGET_EXCEEDED:
        return VisualInputStoreError(VisualInputStoreErrorCode.BUDGET_EXCEEDED)
    if error.code is VisualContractErrorCode.INTEGRITY_FAILURE:
        return VisualInputStoreError(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
    return VisualInputStoreError(VisualInputStoreErrorCode.INVALID_INPUT)


def _exact_mapping(value: object, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _raise(VisualInputStoreErrorCode.INVALID_INPUT)
    try:
        result = dict(value)
    except Exception:
        _raise(VisualInputStoreErrorCode.INVALID_INPUT)
    if set(result) != fields or any(type(key) is not str for key in result):
        _raise(VisualInputStoreErrorCode.INVALID_INPUT)
    return result


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
        _raise(VisualInputStoreErrorCode.INVALID_INPUT)
    if len(raw) > MAX_IMAGE_SET_RECORD_BYTES:
        _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)
    return raw


def _delete_names(image_set_id: str) -> tuple[str, str]:
    suffix = image_set_id.removeprefix("image_set_")
    return f".deleted_{suffix}.json", f".delete_{suffix}"


def _delete_marker_temp_name(image_set_id: str) -> str:
    return f".delete_marker_{image_set_id.removeprefix('image_set_')}.tmp"


def _delete_marker_raw(image_set_id: str, manifest_sha256: str) -> bytes:
    body = {
        "schema_version": VISUAL_SCHEMA_VERSION,
        "image_set_id": image_set_id,
        "manifest_sha256": manifest_sha256,
    }
    marker_sha256 = hashlib.sha256(_DELETE_MARKER_DOMAIN + _canonical_json(body)).hexdigest()
    return _canonical_json(body | {"marker_sha256": marker_sha256})


def _retired_names(image_set_id: str) -> tuple[str, str]:
    suffix = image_set_id.removeprefix("image_set_")
    return f".retired_{suffix}.json", f".retire_marker_{suffix}.tmp"


def _retired_marker_raw(image_set_id: str) -> bytes:
    body = {"schema_version": VISUAL_SCHEMA_VERSION, "image_set_id": image_set_id}
    retired_sha256 = hashlib.sha256(_RETIRED_MARKER_DOMAIN + _canonical_json(body)).hexdigest()
    return _canonical_json(body | {"retired_sha256": retired_sha256})


@dataclass(frozen=True, slots=True, kw_only=True)
class ImageIngress:
    view_role: ViewRole
    calibration_status: CalibrationStatus
    declared_mime: ImageMime
    schema_version: int = VISUAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != VISUAL_SCHEMA_VERSION:
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        for field_name, enum_type in (
            ("view_role", ViewRole),
            ("calibration_status", CalibrationStatus),
            ("declared_mime", ImageMime),
        ):
            value = getattr(self, field_name)
            if type(value) is not enum_type:
                try:
                    object.__setattr__(self, field_name, enum_type(value))
                except (TypeError, ValueError):
                    _raise(VisualInputStoreErrorCode.INVALID_INPUT)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "view_role": self.view_role.value,
            "calibration_status": self.calibration_status.value,
            "declared_mime": self.declared_mime.value,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SealImageSetRequest:
    create_key: str
    inputs: tuple[ImageIngress, ...]
    unit: str | None
    dimension_hints: tuple[DimensionHint, ...]
    calibration_evidence: tuple[CalibrationEvidence, ...]
    same_object: bool
    same_state: bool
    same_scale: bool
    processing_authorization: ProcessingAuthorization = ProcessingAuthorization.LOCAL_ONLY
    schema_version: int = VISUAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != VISUAL_SCHEMA_VERSION:
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        try:
            image_set_identity(self.create_key)
        except VisualContractError as error:
            raise _contract_error(error) from None
        if not isinstance(self.inputs, Sequence) or isinstance(self.inputs, (str, bytes)):
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        inputs = tuple(self.inputs)
        if not 1 <= len(inputs) <= MAX_IMAGE_SET_ITEMS or any(
            type(item) is not ImageIngress for item in inputs
        ):
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        object.__setattr__(self, "inputs", inputs)
        if self.unit is not None and (type(self.unit) is not str or self.unit != "mm"):
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        if not isinstance(self.dimension_hints, Sequence) or isinstance(
            self.dimension_hints, (str, bytes)
        ):
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        hints = tuple(self.dimension_hints)
        if len(hints) > MAX_DIMENSION_HINTS or any(
            type(item) is not DimensionHint for item in hints
        ):
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        if any(item.source_index >= len(inputs) for item in hints):
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        object.__setattr__(self, "dimension_hints", hints)
        if not isinstance(self.calibration_evidence, Sequence) or isinstance(
            self.calibration_evidence, (str, bytes)
        ):
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        evidence = tuple(self.calibration_evidence)
        if len(evidence) > MAX_IMAGE_SET_ITEMS * 2 or any(
            type(item) is not CalibrationEvidence for item in evidence
        ):
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        if any(item.source_index >= len(inputs) for item in evidence):
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        identities = tuple((item.source_index, item.kind) for item in evidence)
        if len(set(identities)) != len(identities):
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
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
                _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        object.__setattr__(self, "calibration_evidence", evidence)
        for name in ("same_object", "same_state", "same_scale"):
            if type(getattr(self, name)) is not bool:
                _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        if self.processing_authorization is not ProcessingAuthorization.LOCAL_ONLY:
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "create_key": self.create_key,
            "inputs": [item.to_mapping() for item in self.inputs],
            "unit": self.unit,
            "dimension_hints": [item.to_mapping() for item in self.dimension_hints],
            "calibration_evidence": [item.to_mapping() for item in self.calibration_evidence],
            "same_object": self.same_object,
            "same_state": self.same_state,
            "same_scale": self.same_scale,
            "processing_authorization": self.processing_authorization.value,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DescriptorSource:
    fd: int
    locator: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.fd) is not int or self.fd < 0:
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        data = _exact_mapping(self.locator, _LOCATOR_FIELDS)
        object.__setattr__(self, "locator", MappingProxyType(data))


def _identity_body(value: os.stat_result) -> dict[str, object]:
    return {
        "schema_version": VISUAL_SCHEMA_VERSION,
        "dev": value.st_dev,
        "ino": value.st_ino,
        "mode": value.st_mode,
        "uid": value.st_uid,
        "nlink": value.st_nlink,
        "size": value.st_size,
        "mtime_ns": str(value.st_mtime_ns),
        "ctime_ns": str(value.st_ctime_ns),
    }


def _safe_source(value: os.stat_result) -> bool:
    return (
        stat.S_ISREG(value.st_mode)
        and value.st_uid == os.geteuid()
        and value.st_nlink == 1
        and 0 < value.st_size <= MAX_IMAGE_SOURCE_BYTES
    )


def bind_visual_input_locator(
    request: object,
    index: object,
    source: object,
) -> dict[str, object]:
    """Bind one request slot to one exact local regular-file identity."""

    if type(request) is not SealImageSetRequest:
        _raise(VisualInputStoreErrorCode.INVALID_INPUT)
    if type(index) is not int or not 0 <= index < len(request.inputs):
        _raise(VisualInputStoreErrorCode.INVALID_INPUT)
    if type(source) is not os.stat_result or not _safe_source(source):
        _raise(VisualInputStoreErrorCode.INVALID_INPUT)
    body = _identity_body(source)
    digest = hashlib.sha256(
        _LOCATOR_DOMAIN
        + _canonical_json({"request": request.to_mapping(), "index": index, "identity": body})
    ).hexdigest()
    return body | {"digest": digest}


def _source_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _validate_locator(
    request: SealImageSetRequest,
    index: int,
    locator: Mapping[str, object],
) -> tuple[int, int, int, int, int, int, int, int]:
    data = _exact_mapping(locator, _LOCATOR_FIELDS)
    for key in ("schema_version", "dev", "ino", "mode", "uid", "nlink", "size"):
        if type(data[key]) is not int:
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
    if data["schema_version"] != VISUAL_SCHEMA_VERSION:
        _raise(VisualInputStoreErrorCode.INVALID_INPUT)
    for key in ("mtime_ns", "ctime_ns"):
        if type(data[key]) is not str or not data[key].isdigit() or len(data[key]) > 32:
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
    digest = data["digest"]
    if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        _raise(VisualInputStoreErrorCode.INVALID_INPUT)
    body = {key: data[key] for key in _LOCATOR_FIELDS if key != "digest"}
    expected = hashlib.sha256(
        _LOCATOR_DOMAIN
        + _canonical_json({"request": request.to_mapping(), "index": index, "identity": body})
    ).hexdigest()
    if not hmac.compare_digest(digest, expected):
        _raise(VisualInputStoreErrorCode.INVALID_INPUT)
    identity = (
        data["dev"],
        data["ino"],
        data["mode"],
        data["uid"],
        data["nlink"],
        data["size"],
        int(data["mtime_ns"]),
        int(data["ctime_ns"]),
    )
    synthetic = os.stat_result(
        (
            data["mode"],
            data["ino"],
            data["dev"],
            data["nlink"],
            data["uid"],
            0,
            data["size"],
            0,
            0,
            0,
        )
    )
    if not _safe_source(synthetic):
        _raise(VisualInputStoreErrorCode.INVALID_INPUT)
    return identity


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def _rename_directory_noreplace(parent_fd: int, source: str, destination: str) -> None:
    try:
        library = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            operation = library.renameatx_np
            operation.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            arguments = (
                parent_fd,
                source.encode("ascii"),
                parent_fd,
                destination.encode("ascii"),
                4,
            )
        elif sys.platform.startswith("linux"):
            operation = library.renameat2
            operation.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            arguments = (
                parent_fd,
                source.encode("ascii"),
                parent_fd,
                destination.encode("ascii"),
                1,
            )
        else:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
        operation.restype = ctypes.c_int
        ctypes.set_errno(0)
        if operation(*arguments) != 0:
            code = ctypes.get_errno() or errno.EIO
            if code in {errno.EEXIST, errno.ENOTEMPTY}:
                raise FileExistsError(code, "destination exists")
            raise OSError(code, "atomic no-replace rename failed")
    except (AttributeError, UnicodeError):
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable") from None


class VisualInputStore:
    """One identity-pinned catalog of immutable ImageSet directories."""

    __slots__ = ("_lease_manager", "_root")

    def __init__(
        self,
        *,
        root: Path,
        expected_root_identity: tuple[int, int],
        lease_manager: ResourceLeaseManager,
    ) -> None:
        if type(root) is not type(Path("/")):
            raise TypeError("root must be an exact Path")
        if (
            type(expected_root_identity) is not tuple
            or len(expected_root_identity) != 2
            or any(type(item) is not int for item in expected_root_identity)
            or type(lease_manager) is not ResourceLeaseManager
        ):
            raise TypeError("invalid visual-input store composition")
        try:
            selected = SafeRoot(root)
        except StorageFailure:
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        if selected.identity != expected_root_identity:
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        self._root = selected
        self._lease_manager = lease_manager

    def _acquire_catalog(self):
        deadline = time.monotonic() + _LEASE_WAIT_SECONDS
        while True:
            try:
                return self._lease_manager.acquire(_CATALOG_RESOURCE)
            except LeaseError as error:
                if error.code is LeaseErrorCode.CONTENDED and time.monotonic() < deadline:
                    time.sleep(_LEASE_RETRY_SECONDS)
                    continue
                if error.code is LeaseErrorCode.CONTENDED:
                    _raise(VisualInputStoreErrorCode.LEASE_UNAVAILABLE)
                _raise(VisualInputStoreErrorCode.STORE_FAILURE)

    def get(self, image_set_id: object) -> ImageSet:
        if type(image_set_id) is not str or _IMAGE_SET_ID.fullmatch(image_set_id) is None:
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        try:
            root_fd = self._root.open()
            try:
                return self._read_sealed(root_fd, image_set_id)
            finally:
                os.close(root_fd)
        except VisualInputStoreError:
            raise
        except (OSError, StorageFailure):
            _raise(VisualInputStoreErrorCode.STORE_FAILURE)

    def delete_exact(self, image_set_id: object, manifest_sha256: object) -> None:
        """Delete one exact sealed ImageSet and retain in-progress evidence."""

        if type(image_set_id) is not str or _IMAGE_SET_ID.fullmatch(image_set_id) is None:
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        if (
            type(manifest_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
        ):
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        lease = self._acquire_catalog()
        primary: BaseException | None = None
        try:
            self._delete_exact_locked(image_set_id, manifest_sha256)
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                lease.release(owner_token=lease.owner_token)
            except BaseException:
                if primary is None:
                    _raise(VisualInputStoreErrorCode.RECOVERY_REQUIRED)

    def finalize_delete_exact(self, image_set_id: object, manifest_sha256: object) -> None:
        """Retire exact deletion evidence without touching a reappeared ImageSet."""

        if type(image_set_id) is not str or _IMAGE_SET_ID.fullmatch(image_set_id) is None:
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        if (
            type(manifest_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
        ):
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        lease = self._acquire_catalog()
        primary: BaseException | None = None
        try:
            self._finalize_delete_exact_locked(image_set_id, manifest_sha256)
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                lease.release(owner_token=lease.owner_token)
            except BaseException:
                if primary is None:
                    _raise(VisualInputStoreErrorCode.RECOVERY_REQUIRED)

    def _finalize_delete_exact_locked(
        self,
        image_set_id: str,
        manifest_sha256: str,
    ) -> None:
        marker_name, delete_name = _delete_names(image_set_id)
        temporary_name = _delete_marker_temp_name(image_set_id)
        retired_name, retired_temporary_name = _retired_names(image_set_id)
        root_fd = -1
        mutation_started = False
        try:
            root_fd = self._root.open()
            _, _, identity_count = self._inventory(root_fd, recover_stages=False)
            if self._entry_exists(root_fd, image_set_id):
                _raise(VisualInputStoreErrorCode.CONFLICT)
            if self._entry_exists(root_fd, delete_name) or self._entry_exists(
                root_fd, temporary_name
            ):
                _raise(VisualInputStoreErrorCode.RECOVERY_REQUIRED)
            marker = self._read_delete_marker(root_fd, marker_name, missing_ok=True)
            if marker is not None and marker != (image_set_id, manifest_sha256):
                _raise(VisualInputStoreErrorCode.CONFLICT)
            retired = self._read_retired_marker(root_fd, retired_name, missing_ok=True)
            if retired is None and marker is None and identity_count >= MAX_IMAGE_SETS:
                _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)
            if self._entry_exists(root_fd, retired_temporary_name):
                temporary = os.stat(
                    retired_temporary_name,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
                mutation_started = True
                self._remove_delete_marker_temporary(
                    root_fd,
                    retired_temporary_name,
                    temporary,
                )
            if retired is None:
                mutation_started = True
                self._write_retired_marker(root_fd, retired_name, image_set_id)
            elif retired != image_set_id:
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            if marker is not None:
                entry = os.stat(marker_name, dir_fd=root_fd, follow_symlinks=False)
                mutation_started = True
                self._remove_delete_marker(
                    root_fd,
                    marker_name,
                    entry,
                    image_set_id,
                    manifest_sha256,
                )
            os.fsync(root_fd)
        except VisualInputStoreError:
            raise
        except (OSError, StorageFailure):
            _raise(
                VisualInputStoreErrorCode.RECOVERY_REQUIRED
                if mutation_started
                else VisualInputStoreErrorCode.STORE_FAILURE
            )
        finally:
            if root_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(root_fd)

    def _delete_exact_locked(self, image_set_id: str, manifest_sha256: str) -> None:
        marker_name, delete_name = _delete_names(image_set_id)
        retired_name, _ = _retired_names(image_set_id)
        root_fd = -1
        marker_published = False
        try:
            root_fd = self._root.open()
            self._inventory(root_fd, recover_stages=True)
            retired = self._read_retired_marker(root_fd, retired_name, missing_ok=True)
            if retired is not None:
                if self._entry_exists(root_fd, image_set_id) or self._entry_exists(
                    root_fd, delete_name
                ):
                    _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
                os.fsync(root_fd)
                return
            marker = self._read_delete_marker(root_fd, marker_name, missing_ok=True)
            if marker is not None:
                marker_published = True
                if marker != (image_set_id, manifest_sha256):
                    _raise(VisualInputStoreErrorCode.CONFLICT)

            target_exists = self._entry_exists(root_fd, image_set_id)
            delete_exists = self._entry_exists(root_fd, delete_name)
            if target_exists and delete_exists:
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            if not marker_published and not target_exists:
                os.fsync(root_fd)
                return

            if target_exists:
                record = self._read_sealed(root_fd, image_set_id)
                if not hmac.compare_digest(record.manifest_sha256, manifest_sha256):
                    _raise(VisualInputStoreErrorCode.CONFLICT)
                entry = os.stat(image_set_id, dir_fd=root_fd, follow_symlinks=False)
                if not marker_published:
                    if (
                        sum(
                            _DELETE_MARKER_NAME.fullmatch(name) is not None
                            for name in os.listdir(root_fd)
                        )
                        >= MAX_IMAGE_SETS
                    ):
                        _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)
                    marker_published = True
                    self._write_delete_marker(
                        root_fd,
                        marker_name,
                        image_set_id,
                        manifest_sha256,
                    )
                current = os.stat(image_set_id, dir_fd=root_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (entry.st_dev, entry.st_ino):
                    _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
                _rename_directory_noreplace(root_fd, image_set_id, delete_name)
                os.fsync(root_fd)
                delete_exists = True

            if delete_exists:
                record = self._read_delete_stage(
                    root_fd,
                    delete_name,
                    image_set_id,
                    manifest_sha256,
                )
                entry = os.stat(delete_name, dir_fd=root_fd, follow_symlinks=False)
                self._remove_sealed_directory(root_fd, delete_name, entry, record)
            else:
                # The matching marker is durable proof that this exact deletion completed.
                os.fsync(root_fd)
        except VisualInputStoreError:
            raise
        except (OSError, StorageFailure):
            _raise(
                VisualInputStoreErrorCode.RECOVERY_REQUIRED
                if marker_published
                else VisualInputStoreErrorCode.STORE_FAILURE
            )
        finally:
            if root_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(root_fd)

    def seal(
        self,
        request: object,
        sources: object,
    ) -> ImageSet:
        if type(request) is not SealImageSetRequest:
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        selected_sources = tuple(sources)
        if len(selected_sources) != len(request.inputs) or any(
            type(item) is not DescriptorSource for item in selected_sources
        ):
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        identities = tuple(
            _validate_locator(request, index, item.locator)
            for index, item in enumerate(selected_sources)
        )
        locator_bytes = sum(identity[5] for identity in identities)
        if locator_bytes > MAX_IMAGE_SET_SOURCE_BYTES:
            _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)

        lease = self._acquire_catalog()
        primary: BaseException | None = None
        try:
            return self._seal_locked(request, selected_sources, identities)
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                lease.release(owner_token=lease.owner_token)
            except BaseException:
                if primary is None:
                    _raise(VisualInputStoreErrorCode.RECOVERY_REQUIRED)

    def _seal_locked(
        self,
        request: SealImageSetRequest,
        sources: tuple[DescriptorSource, ...],
        identities: tuple[tuple[int, int, int, int, int, int, int, int], ...],
    ) -> ImageSet:
        image_set_id, create_key_digest = image_set_identity(request.create_key)
        stage_name = ".stage_" + image_set_id.removeprefix("image_set_")
        stage_fd = -1
        stage_info: os.stat_result | None = None
        published = False
        root_fd = -1
        try:
            root_fd = self._root.open()
            count, total, identity_count = self._inventory(root_fd, recover_stages=True)
            target_exists = self._entry_exists(root_fd, image_set_id)
            marker_name, _ = _delete_names(image_set_id)
            if self._entry_exists(root_fd, marker_name):
                _raise(VisualInputStoreErrorCode.CONFLICT)
            retired_name, _ = _retired_names(image_set_id)
            if self._entry_exists(root_fd, retired_name):
                _raise(VisualInputStoreErrorCode.CONFLICT)
            if not target_exists and identity_count >= MAX_IMAGE_SETS:
                _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)
            if target_exists:
                existing = self._read_sealed(root_fd, image_set_id)
                self._verify_replay(request, sources, identities, existing)
                return existing
            os.mkdir(stage_name, 0o700, dir_fd=root_fd)
            stage_fd, stage_info = self._root.open_directory_at(root_fd, stage_name)

            visual_inputs: list[VisualInput] = []
            physical_bytes = 0
            source_bytes = 0
            source_pixels = 0
            for index, (metadata, source, expected) in enumerate(
                zip(request.inputs, sources, identities, strict=True)
            ):
                original_name = visual_input_identity(request.create_key, index, "original") + (
                    ".jpg" if metadata.declared_mime is ImageMime.JPEG else ".png"
                )
                original_digest, original_size = self._copy_descriptor(
                    stage_fd,
                    original_name,
                    source.fd,
                    expected,
                )
                width, height, normalized = self._normalize(
                    stage_fd,
                    original_name,
                    metadata.declared_mime,
                )
                source_bytes += original_size
                source_pixels += width * height
                if (
                    source_bytes > MAX_IMAGE_SET_SOURCE_BYTES
                    or source_pixels > MAX_IMAGE_SET_PIXELS
                ):
                    _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)
                normalized_id = visual_input_identity(request.create_key, index, "normalized")
                normalized_name = normalized_id + ".png"
                normalized_digest, normalized_size = self._write_normalized(
                    stage_fd,
                    normalized_name,
                    normalized,
                )
                physical_bytes += original_size + normalized_size
                if physical_bytes > MAX_IMAGE_SET_PHYSICAL_BYTES:
                    _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)
                visual_inputs.append(
                    VisualInput(
                        original=ImageRef(
                            id=original_name.rsplit(".", 1)[0],
                            sha256=original_digest,
                            size_bytes=original_size,
                            mime=metadata.declared_mime,
                            width=width,
                            height=height,
                            profile=(
                                SOURCE_JPEG_PROFILE
                                if metadata.declared_mime is ImageMime.JPEG
                                else SOURCE_PNG_PROFILE
                            ),
                        ),
                        normalized=ImageRef(
                            id=normalized_id,
                            sha256=normalized_digest,
                            size_bytes=normalized_size,
                            mime=ImageMime.PNG,
                            width=normalized.width,
                            height=normalized.height,
                            profile=NORMALIZATION_PROFILE,
                        ),
                        view_role=metadata.view_role,
                        calibration_status=metadata.calibration_status,
                    )
                )

            try:
                candidate = ImageSet(
                    id=image_set_id,
                    create_key_digest=create_key_digest,
                    inputs=tuple(visual_inputs),
                    unit=request.unit,
                    dimension_hints=request.dimension_hints,
                    calibration_evidence=request.calibration_evidence,
                    same_object=request.same_object,
                    same_state=request.same_state,
                    same_scale=request.same_scale,
                    processing_authorization=request.processing_authorization,
                )
                manifest = encode_image_set(candidate)
            except VisualContractError as error:
                raise _contract_error(error) from None
            self._write_stage_file(stage_fd, "manifest.json", manifest, MAX_IMAGE_SET_RECORD_BYTES)
            os.fsync(stage_fd)
            if (
                not target_exists
                and total + physical_bytes + len(manifest) > MAX_VISUAL_INPUT_STORE_BYTES
            ):
                _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)

            try:
                _rename_directory_noreplace(root_fd, stage_name, image_set_id)
            except FileExistsError:
                existing = self._read_sealed(root_fd, image_set_id)
                if encode_image_set(existing) != manifest:
                    _raise(VisualInputStoreErrorCode.CONFLICT)
                return existing
            published = True
            os.fsync(root_fd)
            result = self._read_sealed(root_fd, image_set_id)
            if result != candidate:
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            return result
        except VisualInputStoreError:
            raise
        except VisualContractError as error:
            raise _contract_error(error) from None
        except (OSError, StorageFailure, UnidentifiedImageError, Image.DecompressionBombError):
            _raise(
                VisualInputStoreErrorCode.RECOVERY_REQUIRED
                if published
                else VisualInputStoreErrorCode.STORE_FAILURE
            )
        finally:
            unwinding = sys.exc_info()[0] is not None
            cleanup_failed = False
            if stage_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(stage_fd)
            if root_fd >= 0 and not published and stage_info is not None:
                try:
                    self._remove_stage(root_fd, stage_name, stage_info)
                except BaseException:
                    cleanup_failed = True
            if root_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(root_fd)
            if cleanup_failed and not unwinding:
                _raise(VisualInputStoreErrorCode.RECOVERY_REQUIRED)

    def _verify_replay(
        self,
        request: SealImageSetRequest,
        sources: tuple[DescriptorSource, ...],
        identities: tuple[tuple[int, int, int, int, int, int, int, int], ...],
        existing: ImageSet,
    ) -> None:
        semantic_match = (
            existing.unit == request.unit
            and existing.dimension_hints == request.dimension_hints
            and existing.calibration_evidence == request.calibration_evidence
            and existing.same_object is request.same_object
            and existing.same_state is request.same_state
            and existing.same_scale is request.same_scale
            and existing.processing_authorization is request.processing_authorization
            and len(existing.inputs) == len(request.inputs)
            and all(
                item.view_role is metadata.view_role
                and item.calibration_status is metadata.calibration_status
                and item.original.mime is metadata.declared_mime
                for item, metadata in zip(existing.inputs, request.inputs, strict=True)
            )
        )
        if not semantic_match:
            _raise(VisualInputStoreErrorCode.CONFLICT)
        for item, source, expected in zip(existing.inputs, sources, identities, strict=True):
            digest, size = self._hash_descriptor(source.fd, expected)
            if digest != item.original.sha256 or size != item.original.size_bytes:
                _raise(VisualInputStoreErrorCode.CONFLICT)

    @staticmethod
    def _hash_descriptor(
        source_fd: int,
        expected: tuple[int, int, int, int, int, int, int, int],
    ) -> tuple[str, int]:
        duplicate = -1
        try:
            duplicate = os.dup(source_fd)
            os.set_inheritable(duplicate, False)
            before = os.fstat(duplicate)
            if not _safe_source(before) or _source_identity(before) != expected:
                _raise(VisualInputStoreErrorCode.INVALID_INPUT)
            digest = hashlib.sha256()
            remaining = before.st_size
            offset = 0
            while remaining:
                chunk = os.pread(duplicate, min(_COPY_CHUNK_BYTES, remaining), offset)
                if not chunk:
                    _raise(VisualInputStoreErrorCode.INVALID_INPUT)
                digest.update(chunk)
                remaining -= len(chunk)
                offset += len(chunk)
            if _source_identity(os.fstat(duplicate)) != expected:
                _raise(VisualInputStoreErrorCode.INVALID_INPUT)
            return digest.hexdigest(), before.st_size
        finally:
            if duplicate >= 0:
                with contextlib.suppress(OSError):
                    os.close(duplicate)

    def _copy_descriptor(
        self,
        stage_fd: int,
        name: str,
        source_fd: int,
        expected: tuple[int, int, int, int, int, int, int, int],
    ) -> tuple[str, int]:
        duplicate = -1
        target = -1
        try:
            duplicate = os.dup(source_fd)
            os.set_inheritable(duplicate, False)
            before = os.fstat(duplicate)
            if not _safe_source(before) or _source_identity(before) != expected:
                _raise(VisualInputStoreErrorCode.INVALID_INPUT)
            target = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=stage_fd,
            )
            digest = hashlib.sha256()
            remaining = before.st_size
            offset = 0
            while remaining:
                chunk = os.pread(duplicate, min(_COPY_CHUNK_BYTES, remaining), offset)
                if not chunk:
                    _raise(VisualInputStoreErrorCode.INVALID_INPUT)
                digest.update(chunk)
                _write_all(target, chunk)
                remaining -= len(chunk)
                offset += len(chunk)
            after = os.fstat(duplicate)
            if _source_identity(after) != expected:
                _raise(VisualInputStoreErrorCode.INVALID_INPUT)
            os.fsync(target)
            stored = os.fstat(target)
            if not self._root.regular_file(stored, maximum=MAX_IMAGE_SOURCE_BYTES):
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            return digest.hexdigest(), stored.st_size
        finally:
            if target >= 0:
                with contextlib.suppress(OSError):
                    os.close(target)
            if duplicate >= 0:
                with contextlib.suppress(OSError):
                    os.close(duplicate)

    def _normalize(
        self,
        stage_fd: int,
        name: str,
        declared_mime: ImageMime,
    ) -> tuple[int, int, Image.Image]:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=stage_fd)
        try:
            header = os.pread(fd, 16, 0)
            expected_format = "JPEG" if declared_mime is ImageMime.JPEG else "PNG"
            if declared_mime is ImageMime.JPEG:
                magic_ok = header.startswith(b"\xff\xd8\xff")
            else:
                magic_ok = header.startswith(b"\x89PNG\r\n\x1a\n")
            if not magic_ok:
                _raise(VisualInputStoreErrorCode.INVALID_INPUT)
            with os.fdopen(os.dup(fd), "rb") as stream, Image.open(stream) as probe:
                if probe.format != expected_format or getattr(probe, "n_frames", 1) != 1:
                    _raise(VisualInputStoreErrorCode.INVALID_INPUT)
                width, height = probe.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)
                probe.verify()
            with os.fdopen(os.dup(fd), "rb") as stream, Image.open(stream) as decoded:
                if decoded.format != expected_format or getattr(decoded, "n_frames", 1) != 1:
                    _raise(VisualInputStoreErrorCode.INVALID_INPUT)
                if expected_format == "JPEG":
                    decoded.draft("RGB", (MAX_NORMALIZED_LONG_EDGE, MAX_NORMALIZED_LONG_EDGE))
                decoded.load()
                icc_profile = decoded.info.get("icc_profile")
                if max(decoded.size) > MAX_NORMALIZED_LONG_EDGE:
                    decoded.thumbnail(
                        (MAX_NORMALIZED_LONG_EDGE, MAX_NORMALIZED_LONG_EDGE),
                        Image.Resampling.LANCZOS,
                    )
                ImageOps.exif_transpose(decoded, in_place=True)
                has_alpha = decoded.mode in {"RGBA", "LA"} or "transparency" in decoded.info
                alpha = decoded.convert("RGBA").getchannel("A") if has_alpha else None
                if icc_profile is not None:
                    if type(icc_profile) is not bytes or len(icc_profile) > MAX_IMAGE_SOURCE_BYTES:
                        _raise(VisualInputStoreErrorCode.INVALID_INPUT)
                    source_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
                    target_profile = ImageCms.createProfile("sRGB")
                    color_space = source_profile.profile.xcolor_space.strip().upper()
                    compatible_mode = {
                        "RGB": "RGB",
                        "CMYK": "CMYK",
                        "GRAY": "L",
                    }.get(color_space)
                    if compatible_mode is None:
                        _raise(VisualInputStoreErrorCode.INVALID_INPUT)
                    color_source = (
                        decoded
                        if decoded.mode == compatible_mode
                        else decoded.convert(compatible_mode)
                    )
                    rgb = ImageCms.profileToProfile(
                        color_source,
                        source_profile,
                        target_profile,
                        outputMode="RGB",
                    )
                else:
                    rgb = decoded.convert("RGB")
                normalized = rgb
                if alpha is not None:
                    normalized = rgb.convert("RGBA")
                    normalized.putalpha(alpha)
                normalized.info.clear()
                return width, height, normalized
        except VisualInputStoreError:
            raise
        except Image.DecompressionBombError:
            _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)
        except (OSError, SyntaxError, ValueError, UnidentifiedImageError, ImageCms.PyCMSError):
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        finally:
            os.close(fd)

    def _write_normalized(
        self,
        stage_fd: int,
        name: str,
        image: Image.Image,
    ) -> tuple[str, int]:
        output = io.BytesIO()
        try:
            image.save(output, format="PNG", compress_level=9, optimize=False)
        except (OSError, ValueError):
            _raise(VisualInputStoreErrorCode.INVALID_INPUT)
        raw = output.getvalue()
        if not raw or len(raw) > MAX_NORMALIZED_IMAGE_BYTES:
            _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)
        self._write_stage_file(stage_fd, name, raw, MAX_NORMALIZED_IMAGE_BYTES)
        return hashlib.sha256(raw).hexdigest(), len(raw)

    def _write_stage_file(self, stage_fd: int, name: str, raw: bytes, maximum: int) -> None:
        if not raw or len(raw) > maximum:
            _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)
        fd = -1
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=stage_fd,
            )
            _write_all(fd, raw)
            os.fsync(fd)
            if not self._root.regular_file(os.fstat(fd), maximum=maximum):
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)

    @staticmethod
    def _entry_exists(root_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            _raise(VisualInputStoreErrorCode.STORE_FAILURE)
        return True

    def _inventory(self, root_fd: int, *, recover_stages: bool) -> tuple[int, int, int]:
        count = 0
        total = 0
        stages: list[tuple[str, os.stat_result]] = []
        delete_stages: list[tuple[str, os.stat_result]] = []
        marker_temporaries: list[tuple[str, os.stat_result]] = []
        retired_temporaries: list[tuple[str, os.stat_result]] = []
        markers: dict[str, tuple[str, str]] = {}
        retired: set[str] = set()
        sealed_ids: set[str] = set()
        try:
            names = os.listdir(root_fd)
        except OSError:
            _raise(VisualInputStoreErrorCode.STORE_FAILURE)
        if len(names) > MAX_IMAGE_SETS * 2 + MAX_IMAGE_SET_TEMPORARIES * 2:
            _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)
        for name in names:
            try:
                info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except OSError:
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            if _STAGE_NAME.fullmatch(name) is not None:
                stages.append((name, info))
                continue
            if _DELETE_STAGE_NAME.fullmatch(name) is not None:
                delete_stages.append((name, info))
                continue
            if _DELETE_MARKER_NAME.fullmatch(name) is not None:
                marker = self._read_delete_marker(root_fd, name, missing_ok=False)
                if marker is None:
                    _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
                expected_name, _ = _delete_names(marker[0])
                if expected_name != name or marker[0] in markers:
                    _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
                markers[marker[0]] = marker
                total += len(_delete_marker_raw(*marker))
                continue
            if _DELETE_MARKER_TEMP_NAME.fullmatch(name) is not None:
                marker_temporaries.append((name, info))
                continue
            if _RETIRED_MARKER_NAME.fullmatch(name) is not None:
                image_set_id = self._read_retired_marker(root_fd, name, missing_ok=False)
                if image_set_id is None:
                    _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
                expected_name, _ = _retired_names(image_set_id)
                if expected_name != name or image_set_id in retired:
                    _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
                retired.add(image_set_id)
                total += len(_retired_marker_raw(image_set_id))
                continue
            if _RETIRED_MARKER_TEMP_NAME.fullmatch(name) is not None:
                retired_temporaries.append((name, info))
                continue
            if _IMAGE_SET_ID.fullmatch(name) is None:
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            record = self._read_sealed(root_fd, name)
            sealed_ids.add(record.id)
            count += 1
            total += len(encode_image_set(record)) + sum(
                item.original.size_bytes + item.normalized.size_bytes for item in record.inputs
            )
        if (
            len(stages) + len(delete_stages) + len(marker_temporaries) + len(retired_temporaries)
            > MAX_IMAGE_SET_TEMPORARIES
        ):
            _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)
        identities = sealed_ids | set(markers) | retired
        if len(identities) > MAX_IMAGE_SETS:
            _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)
        for name, _ in delete_stages:
            image_set_id = "image_set_" + name.removeprefix(".delete_")
            if image_set_id not in markers:
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            if image_set_id in retired:
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        if sealed_ids & retired:
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        if recover_stages:
            for name, info in stages:
                self._remove_stage(root_fd, name, info)
            for name, info in marker_temporaries:
                image_set_id = "image_set_" + name.removeprefix(".delete_marker_").removesuffix(
                    ".tmp"
                )
                if image_set_id in markers or image_set_id not in sealed_ids:
                    _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
                self._remove_delete_marker_temporary(root_fd, name, info)
            for name, info in retired_temporaries:
                image_set_id = "image_set_" + name.removeprefix(".retire_marker_").removesuffix(
                    ".tmp"
                )
                _, delete_name = _delete_names(image_set_id)
                if image_set_id in sealed_ids or any(
                    stage_name == delete_name for stage_name, _ in delete_stages
                ):
                    _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
                self._remove_delete_marker_temporary(root_fd, name, info)
            for name, info in delete_stages:
                image_set_id = "image_set_" + name.removeprefix(".delete_")
                marker = markers.get(image_set_id)
                if marker is None:
                    _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
                record = self._read_delete_stage(
                    root_fd,
                    name,
                    image_set_id,
                    marker[1],
                )
                self._remove_sealed_directory(root_fd, name, info, record)
        if count > MAX_IMAGE_SETS or total > MAX_VISUAL_INPUT_STORE_BYTES:
            _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)
        return count, total, len(identities)

    def _remove_stage(self, root_fd: int, name: str, expected: os.stat_result) -> None:
        stage_fd, info = self._root.open_directory_at(
            root_fd,
            name,
            expected_identity=(expected.st_dev, expected.st_ino),
        )
        try:
            names = os.listdir(stage_fd)
            if len(names) > MAX_IMAGE_SET_ITEMS * 2 + 1:
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            for child in names:
                if child != "manifest.json" and _VISUAL_FILE_NAME.fullmatch(child) is None:
                    _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
                child_info = os.stat(child, dir_fd=stage_fd, follow_symlinks=False)
                maximum = (
                    MAX_IMAGE_SET_RECORD_BYTES
                    if child == "manifest.json"
                    else MAX_NORMALIZED_IMAGE_BYTES
                )
                if not self._root.regular_file(child_info, maximum=maximum):
                    _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
                os.unlink(child, dir_fd=stage_fd)
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        os.rmdir(name, dir_fd=root_fd)
        os.fsync(root_fd)

    def _write_delete_marker(
        self,
        root_fd: int,
        name: str,
        image_set_id: str,
        manifest_sha256: str,
    ) -> None:
        raw = _delete_marker_raw(image_set_id, manifest_sha256)
        temporary_name = _delete_marker_temp_name(image_set_id)
        fd = -1
        try:
            fd = os.open(
                temporary_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=root_fd,
            )
            _write_all(fd, raw)
            os.fsync(fd)
            info = os.fstat(fd)
            if (
                not self._root.regular_file(info, maximum=MAX_IMAGE_SET_RECORD_BYTES)
                or info.st_size != len(raw)
                or os.pread(fd, len(raw) + 1, 0) != raw
            ):
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            self._root.verify_file_entry(
                root_fd,
                temporary_name,
                expected=info,
                maximum=MAX_IMAGE_SET_RECORD_BYTES,
            )
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        try:
            _rename_directory_noreplace(root_fd, temporary_name, name)
        except FileExistsError:
            existing = self._read_delete_marker(root_fd, name, missing_ok=False)
            temporary = os.stat(temporary_name, dir_fd=root_fd, follow_symlinks=False)
            self._remove_delete_marker_temporary(root_fd, temporary_name, temporary)
            if existing != (image_set_id, manifest_sha256):
                _raise(VisualInputStoreErrorCode.CONFLICT)
        published = self._read_delete_marker(root_fd, name, missing_ok=False)
        if published != (image_set_id, manifest_sha256):
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        os.fsync(root_fd)

    def _write_retired_marker(self, root_fd: int, name: str, image_set_id: str) -> None:
        raw = _retired_marker_raw(image_set_id)
        _, temporary_name = _retired_names(image_set_id)
        fd = -1
        try:
            fd = os.open(
                temporary_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=root_fd,
            )
            _write_all(fd, raw)
            os.fsync(fd)
            info = os.fstat(fd)
            if (
                not self._root.regular_file(info, maximum=MAX_IMAGE_SET_RECORD_BYTES)
                or info.st_size != len(raw)
                or os.pread(fd, len(raw) + 1, 0) != raw
            ):
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            self._root.verify_file_entry(
                root_fd,
                temporary_name,
                expected=info,
                maximum=MAX_IMAGE_SET_RECORD_BYTES,
            )
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        try:
            _rename_directory_noreplace(root_fd, temporary_name, name)
        except FileExistsError:
            existing = self._read_retired_marker(root_fd, name, missing_ok=False)
            temporary = os.stat(temporary_name, dir_fd=root_fd, follow_symlinks=False)
            self._remove_delete_marker_temporary(root_fd, temporary_name, temporary)
            if existing != image_set_id:
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        published = self._read_retired_marker(root_fd, name, missing_ok=False)
        if published != image_set_id:
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        os.fsync(root_fd)

    def _remove_delete_marker_temporary(
        self,
        root_fd: int,
        name: str,
        expected: os.stat_result,
    ) -> None:
        fd = -1
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root_fd,
            )
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (
                expected.st_dev,
                expected.st_ino,
            ) or not self._root.regular_file(
                opened,
                maximum=MAX_IMAGE_SET_RECORD_BYTES,
            ):
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            while os.read(fd, _COPY_CHUNK_BYTES):
                pass
            after = os.fstat(fd)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            self._root.verify_file_entry(
                root_fd,
                name,
                expected=after,
                maximum=MAX_IMAGE_SET_RECORD_BYTES,
            )
        finally:
            if fd >= 0:
                os.close(fd)
        os.unlink(name, dir_fd=root_fd)
        os.fsync(root_fd)

    def _remove_delete_marker(
        self,
        root_fd: int,
        name: str,
        expected: os.stat_result,
        image_set_id: str,
        manifest_sha256: str,
    ) -> None:
        raw = _delete_marker_raw(image_set_id, manifest_sha256)
        fd = -1
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root_fd,
            )
            opened = os.fstat(fd)
            if (
                (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
                or not self._root.regular_file(
                    opened,
                    maximum=MAX_IMAGE_SET_RECORD_BYTES,
                )
                or opened.st_size != len(raw)
                or os.pread(fd, len(raw) + 1, 0) != raw
            ):
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            after = os.fstat(fd)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            self._root.verify_file_entry(
                root_fd,
                name,
                expected=after,
                maximum=MAX_IMAGE_SET_RECORD_BYTES,
            )
        finally:
            if fd >= 0:
                os.close(fd)
        os.unlink(name, dir_fd=root_fd)
        os.fsync(root_fd)

    def _read_delete_marker(
        self,
        root_fd: int,
        name: str,
        *,
        missing_ok: bool,
    ) -> tuple[str, str] | None:
        try:
            os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return None
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        except OSError:
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        try:
            raw, _ = self._root.read_file_at(
                root_fd,
                name,
                maximum=MAX_IMAGE_SET_RECORD_BYTES,
            )
        except (OSError, StorageFailure):
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError):
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        if not isinstance(value, dict) or set(value) != _DELETE_MARKER_FIELDS:
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != VISUAL_SCHEMA_VERSION
            or type(value["image_set_id"]) is not str
            or _IMAGE_SET_ID.fullmatch(value["image_set_id"]) is None
            or type(value["manifest_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value["manifest_sha256"]) is None
            or type(value["marker_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value["marker_sha256"]) is None
        ):
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        expected = _delete_marker_raw(value["image_set_id"], value["manifest_sha256"])
        if raw != expected:
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        return value["image_set_id"], value["manifest_sha256"]

    def _read_retired_marker(
        self,
        root_fd: int,
        name: str,
        *,
        missing_ok: bool,
    ) -> str | None:
        try:
            os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return None
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        except OSError:
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        try:
            raw, _ = self._root.read_file_at(
                root_fd,
                name,
                maximum=MAX_IMAGE_SET_RECORD_BYTES,
            )
        except (OSError, StorageFailure):
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError):
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        if not isinstance(value, dict) or set(value) != _RETIRED_MARKER_FIELDS:
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != VISUAL_SCHEMA_VERSION
            or type(value["image_set_id"]) is not str
            or _IMAGE_SET_ID.fullmatch(value["image_set_id"]) is None
            or type(value["retired_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value["retired_sha256"]) is None
            or raw != _retired_marker_raw(value["image_set_id"])
        ):
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        return value["image_set_id"]

    def _remove_sealed_directory(
        self,
        root_fd: int,
        name: str,
        expected: os.stat_result,
        record: ImageSet | None,
    ) -> None:
        directory_fd, opened = self._root.open_directory_at(
            root_fd,
            name,
            expected_identity=(expected.st_dev, expected.st_ino),
        )
        try:
            expected_files = self._sealed_file_evidence(record) if record is not None else {}
            present = set(os.listdir(directory_fd))
            if not present.issubset(expected_files):
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            for child in present:
                digest, maximum = expected_files[child]
                actual, _, _ = self._root.hash_open_file(
                    directory_fd,
                    child,
                    maximum=maximum,
                )
                if not hmac.compare_digest(actual, digest):
                    _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            for child in sorted(present - {"manifest.json"}):
                os.unlink(child, dir_fd=directory_fd)
            os.fsync(directory_fd)
            if "manifest.json" in present:
                os.unlink("manifest.json", dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if (
            (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISDIR(current.st_mode)
            or current.st_uid != self._root.uid
            or stat.S_IMODE(current.st_mode) != 0o700
        ):
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        os.rmdir(name, dir_fd=root_fd)
        os.fsync(root_fd)

    @staticmethod
    def _sealed_file_evidence(record: ImageSet) -> dict[str, tuple[str, int]]:
        expected_files: dict[str, tuple[str, int]] = {
            "manifest.json": (
                hashlib.sha256(encode_image_set(record)).hexdigest(),
                MAX_IMAGE_SET_RECORD_BYTES,
            )
        }
        for item in record.inputs:
            original_ext = ".jpg" if item.original.mime is ImageMime.JPEG else ".png"
            expected_files[item.original.id + original_ext] = (
                item.original.sha256,
                MAX_IMAGE_SOURCE_BYTES,
            )
            expected_files[item.normalized.id + ".png"] = (
                item.normalized.sha256,
                MAX_NORMALIZED_IMAGE_BYTES,
            )
        return expected_files

    def _read_delete_stage(
        self,
        root_fd: int,
        directory_name: str,
        image_set_id: str,
        manifest_sha256: str,
    ) -> ImageSet | None:
        entry = os.stat(directory_name, dir_fd=root_fd, follow_symlinks=False)
        directory_fd, opened = self._root.open_directory_at(
            root_fd,
            directory_name,
            expected_identity=(entry.st_dev, entry.st_ino),
        )
        try:
            names = set(os.listdir(directory_fd))
            if not names:
                self._root.verify_directory_entry(
                    root_fd,
                    directory_name,
                    expected=opened,
                )
                return None
            if "manifest.json" not in names:
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            raw, _ = self._root.read_file_at(
                directory_fd,
                "manifest.json",
                maximum=MAX_IMAGE_SET_RECORD_BYTES,
            )
            try:
                record = decode_image_set(raw)
            except VisualContractError as error:
                raise _contract_error(error) from None
            if record.id != image_set_id or not hmac.compare_digest(
                record.manifest_sha256, manifest_sha256
            ):
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            expected_files = self._sealed_file_evidence(record)
            if not names.issubset(expected_files):
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            for child in names:
                digest, maximum = expected_files[child]
                actual, _, _ = self._root.hash_open_file(
                    directory_fd,
                    child,
                    maximum=maximum,
                )
                if not hmac.compare_digest(actual, digest):
                    _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            self._root.verify_directory_entry(
                root_fd,
                directory_name,
                expected=opened,
            )
            return record
        except (OSError, StorageFailure):
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        finally:
            os.close(directory_fd)

    def _read_sealed(self, root_fd: int, image_set_id: str) -> ImageSet:
        try:
            entry = os.stat(image_set_id, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            _raise(VisualInputStoreErrorCode.NOT_FOUND)
        except OSError:
            _raise(VisualInputStoreErrorCode.STORE_FAILURE)
        try:
            directory_fd, opened = self._root.open_directory_at(
                root_fd,
                image_set_id,
                expected_identity=(entry.st_dev, entry.st_ino),
            )
        except (
            OSError,
            StorageFailure,
            SyntaxError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
        ):
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        try:
            raw, _ = self._root.read_file_at(
                directory_fd,
                "manifest.json",
                maximum=MAX_IMAGE_SET_RECORD_BYTES,
            )
            try:
                record = decode_image_set(raw)
            except VisualContractError as error:
                raise _contract_error(error) from None
            if record.id != image_set_id:
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            expected_names = {"manifest.json"}
            physical_bytes = 0
            for item in record.inputs:
                original_ext = ".jpg" if item.original.mime is ImageMime.JPEG else ".png"
                original_name = item.original.id + original_ext
                normalized_name = item.normalized.id + ".png"
                expected_names.update((original_name, normalized_name))
                if (
                    item.normalized.mime is not ImageMime.PNG
                    or item.normalized.profile != NORMALIZATION_PROFILE
                ):
                    _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
                expected_source_profile = (
                    SOURCE_JPEG_PROFILE
                    if item.original.mime is ImageMime.JPEG
                    else SOURCE_PNG_PROFILE
                )
                if item.original.profile != expected_source_profile:
                    _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
                for ref, name, maximum, magic in (
                    (
                        item.original,
                        original_name,
                        MAX_IMAGE_SOURCE_BYTES,
                        b"\xff\xd8\xff"
                        if item.original.mime is ImageMime.JPEG
                        else b"\x89PNG\r\n\x1a\n",
                    ),
                    (
                        item.normalized,
                        normalized_name,
                        MAX_NORMALIZED_IMAGE_BYTES,
                        b"\x89PNG\r\n\x1a\n",
                    ),
                ):
                    digest, size, _ = self._root.hash_open_file(
                        directory_fd,
                        name,
                        maximum=maximum,
                    )
                    if digest != ref.sha256 or size != ref.size_bytes:
                        _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
                    fd = os.open(
                        name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=directory_fd,
                    )
                    try:
                        if not os.pread(fd, len(magic), 0).startswith(magic):
                            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
                        with os.fdopen(os.dup(fd), "rb") as stream, Image.open(stream) as probe:
                            expected_format = "JPEG" if ref.mime is ImageMime.JPEG else "PNG"
                            if (
                                probe.format != expected_format
                                or getattr(probe, "n_frames", 1) != 1
                                or probe.size != (ref.width, ref.height)
                            ):
                                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
                    finally:
                        os.close(fd)
                    physical_bytes += size
            if set(os.listdir(directory_fd)) != expected_names:
                _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
            if physical_bytes > MAX_IMAGE_SET_PHYSICAL_BYTES:
                _raise(VisualInputStoreErrorCode.BUDGET_EXCEEDED)
            self._root.verify_directory_entry(
                root_fd,
                image_set_id,
                expected=opened,
            )
            return record
        except (
            OSError,
            StorageFailure,
            SyntaxError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
        ):
            _raise(VisualInputStoreErrorCode.INTEGRITY_FAILURE)
        finally:
            os.close(directory_fd)


__all__ = [
    "DescriptorSource",
    "ImageIngress",
    "SealImageSetRequest",
    "VisualInputStore",
    "VisualInputStoreError",
    "VisualInputStoreErrorCode",
    "bind_visual_input_locator",
]
