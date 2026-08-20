"""Crash-safe, identity-pinned persistence for reconstruction drafts."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import re
import secrets
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vibecad import _file_compat
from vibecad.interaction.storage import SafeRoot, StorageFailure
from vibecad.interaction.storage import os as _storage_os
from vibecad.visual.admission_inputs import (
    MAX_VISUAL_ADMISSION_INPUT_BYTES,
    VisualAdmissionInputBundle,
    VisualAdmissionInputError,
    decode_visual_admission_inputs,
    encode_visual_admission_inputs,
)
from vibecad.visual.drafts import (
    MAX_RECONSTRUCTION_DRAFT_MUTATIONS,
    MAX_RECONSTRUCTION_DRAFT_RECORD_BYTES,
    MAX_RECONSTRUCTION_DRAFT_STORE_BYTES,
    MAX_RECONSTRUCTION_DRAFTS,
    MAX_RECONSTRUCTION_PROVIDER_INVOCATIONS,
    ReconstructionDraft,
    ReconstructionDraftError,
    ReconstructionPayload,
    ReconstructionPayloadRef,
    decode_reconstruction_draft,
    encode_reconstruction_draft,
    validate_reconstruction_creation,
    validate_reconstruction_successor,
)
from vibecad.visual.reconstruction import ReconstructionStatus
from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER
from vibecad.workflow.lease import LeaseError, LeaseErrorCode, ResourceLease, ResourceLeaseManager

os = _storage_os

MAX_RECONSTRUCTION_DRAFT_INVOCATIONS = MAX_RECONSTRUCTION_PROVIDER_INVOCATIONS

_MAX_JOURNAL_BYTES = 64 * 1024
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 8192
_MAX_JSON_STRING_BYTES = 64 * 1024
_MAX_PAYLOAD_BYTES = 768 * 1024
_MAX_STORED_RECORD_BYTES = MAX_RECONSTRUCTION_DRAFT_RECORD_BYTES + 8 * 1024
_LEASE_WAIT_SECONDS = 3.0
_LEASE_RETRY_SECONDS = 0.02

_CATALOG_RESOURCE = "reconstruction-draft-store:catalog"
_JOURNAL_CHECKSUM_DOMAIN = b"vibecad-reconstruction-draft-mutation-v1\0"
_STORED_RECORD_CHECKSUM_DOMAIN = b"vibecad-reconstruction-draft-store-record-v2\0"
_ADMISSION_ID_DOMAIN = b"vibecad-reconstruction-admission-inputs-v1\0"

_RECONSTRUCTION_ID = re.compile(r"^reconstruction_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_ID = re.compile(r"^(0|[1-9][0-9]{0,19})$")
_ADMISSION_ID = re.compile(r"^admission_inputs_[0-9a-f]{32}$")
_PAYLOAD_NAME = re.compile(
    r"^(?:visual_observation|reconstruction_proposal|clarification_answer|admission_inputs)_"
    r"[0-9a-f]{32}\.json$"
)
_STAGE_NAME = re.compile(r"^\.stage_[0-9a-f]{32}_[0-9a-f]{32}$")
_RECORD_TEMP_NAME = re.compile(r"^\.record\.[0-9a-f]{32}\.tmp$")
_PAYLOAD_TEMP_NAME = re.compile(
    r"^\.(?:visual_observation|reconstruction_proposal|clarification_answer|admission_inputs)_"
    r"[0-9a-f]{32}\.json\.[0-9a-f]{32}\.tmp$"
)
_JOURNAL_NAME = ".mutation.json"
_RECORD_NAME = "record.json"


class ReconstructionDraftStoreErrorCode(StrEnum):
    INVALID_ID = "invalid_id"
    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    CONFLICT = "conflict"
    CORRUPT_RECORD = "corrupt_record"
    RECORD_TOO_LARGE = "record_too_large"
    UNSAFE_STORE = "unsafe_store"
    LOCK_UNAVAILABLE = "lock_unavailable"
    IO_ERROR = "io_error"
    DURABILITY_UNCERTAIN = "durability_uncertain"
    RESOURCE_EXHAUSTED = "resource_exhausted"


class ReconstructionDraftStoreError(RuntimeError):
    """Bounded store error that never reflects rejected persisted bytes."""

    def __init__(
        self,
        code: ReconstructionDraftStoreErrorCode,
        *,
        committed_generation: int | None = None,
    ) -> None:
        if type(code) is not ReconstructionDraftStoreErrorCode:
            raise TypeError("code must be an exact ReconstructionDraftStoreErrorCode")
        if code is ReconstructionDraftStoreErrorCode.DURABILITY_UNCERTAIN:
            if (
                type(committed_generation) is not int
                or committed_generation < 0
                or committed_generation > MAX_SAFE_JSON_INTEGER
            ):
                raise ValueError("committed_generation is required for uncertain durability")
        elif committed_generation is not None:
            raise ValueError("committed_generation is only valid for uncertain durability")
        self.code = code
        if committed_generation is not None:
            self.committed_generation = committed_generation
        super().__init__(code.value)


def _raise(
    code: ReconstructionDraftStoreErrorCode,
    *,
    committed_generation: int | None = None,
) -> None:
    raise ReconstructionDraftStoreError(code, committed_generation=committed_generation)


def _draft_id(value: object) -> str:
    if type(value) is not str or _RECONSTRUCTION_ID.fullmatch(value) is None:
        _raise(ReconstructionDraftStoreErrorCode.INVALID_ID)
    return value


def _generation(value: object) -> int:
    if type(value) is not int or value < 0 or value > MAX_SAFE_JSON_INTEGER:
        _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
    return value


def _record_directory_name(reconstruction_id: str) -> str:
    return reconstruction_id


def _canonical_json(value: object, *, maximum: int) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
    if len(raw) > maximum:
        _raise(ReconstructionDraftStoreErrorCode.RECORD_TOO_LARGE)
    return raw


def _canonical_ascii_json(value: object, *, maximum: int) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
    if len(raw) > maximum:
        _raise(ReconstructionDraftStoreErrorCode.RECORD_TOO_LARGE)
    return raw


def _duplicate_checked_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("duplicate or non-string key")
        result[key] = value
    return result


def _parse_integer(raw: str) -> int:
    value = int(raw)
    if value < -MAX_SAFE_JSON_INTEGER or value > MAX_SAFE_JSON_INTEGER:
        raise ValueError("integer is outside the safe range")
    return value


def _json_depth_is_safe(raw: bytes) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                in_string = False
            continue
        if byte == 34:
            in_string = True
        elif byte in (91, 123):
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                return False
        elif byte in (93, 125):
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string and not escaped


def _validate_json_resources(value: object) -> None:
    remaining = [value]
    nodes = 0
    while remaining:
        selected = remaining.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        if type(selected) is str:
            if len(selected.encode("utf-8")) > _MAX_JSON_STRING_BYTES:
                _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        elif type(selected) is list:
            remaining.extend(selected)
        elif type(selected) is dict:
            remaining.extend(selected.keys())
            remaining.extend(selected.values())
        elif selected is None or type(selected) in (bool, int, float):
            continue
        else:
            _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)


def _decode_canonical(raw: bytes, *, maximum: int) -> object:
    if type(raw) is not bytes or len(raw) > maximum or not _json_depth_is_safe(raw):
        _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
    try:
        result = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_checked_object,
            parse_float=lambda value: (_ for _ in ()).throw(ValueError(value)),
            parse_int=_parse_integer,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeError, ValueError, RecursionError):
        _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
    _validate_json_resources(result)
    if _canonical_json(result, maximum=maximum) != raw:
        _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
    return result


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        try:
            count = os.write(fd, view)
        except OSError:
            _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
        if count <= 0 or count > len(view):
            _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
        view = view[count:]


def _close(fd: int) -> bool:
    try:
        os.close(fd)
    except OSError:
        return False
    return True


def _safe_regular(info: os.stat_result, root: SafeRoot, *, maximum: int) -> bool:
    return root.regular_file(info, maximum=maximum)


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except PermissionError:
        _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
    except (OSError, StorageFailure):
        _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _stable_file_observation(info: os.stat_result) -> tuple[int, ...]:
    common = (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
    )
    if sys.platform == "win32":
        # CRT fstat and name-based stat derive ChangeTime through different
        # Win32 information classes and can legitimately differ by one clock
        # quantum.  Identity, protected DACL and reparse safety are already
        # validated by the storage adapter on both observations.
        return common
    return common + (info.st_mtime_ns, info.st_ctime_ns)


def _hash_fd(fd: int, expected: os.stat_result, *, size: int) -> str:
    if expected.st_size != size:
        _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        try:
            chunk = _file_compat.pread(fd, min(64 * 1024, size - offset), offset)
        except OSError:
            _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
        if not chunk:
            _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
        digest.update(chunk)
        offset += len(chunk)
    after = os.fstat(fd)
    if _stable_file_observation(after) != _stable_file_observation(expected):
        _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
    return digest.hexdigest()


def _write_exclusive(
    root: SafeRoot,
    parent_fd: int,
    name: str,
    raw: bytes,
    *,
    maximum: int,
) -> tuple[int, int]:
    if not raw or len(raw) > maximum:
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    fd = -1
    created_identity: tuple[int, int] | None = None
    succeeded = False
    failure: ReconstructionDraftStoreError | None = None
    try:
        try:
            fd = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent_fd,
            )
            opened = os.fstat(fd)
            created_identity = _identity(opened)
            if not _safe_regular(opened, root, maximum=maximum) or os.get_inheritable(fd):
                _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
            _write_all(fd, raw)
            os.fsync(fd)
            after = os.fstat(fd)
            current = _stat_at(parent_fd, name)
            if (
                current is None
                or not _safe_regular(after, root, maximum=maximum)
                or not _safe_regular(current, root, maximum=maximum)
                or _identity(after) != created_identity
                or _identity(current) != created_identity
                or after.st_size != len(raw)
                or current.st_size != len(raw)
                or _hash_fd(fd, after, size=len(raw)) != hashlib.sha256(raw).hexdigest()
            ):
                _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
            if _stable_file_observation(current) != _stable_file_observation(after):
                _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
            succeeded = True
        except ReconstructionDraftStoreError as error:
            failure = error
        except OSError:
            failure = ReconstructionDraftStoreError(ReconstructionDraftStoreErrorCode.IO_ERROR)
    except OSError:
        failure = ReconstructionDraftStoreError(ReconstructionDraftStoreErrorCode.IO_ERROR)
    finally:
        close_ok = fd < 0 or _close(fd)
    if succeeded and close_ok:
        assert created_identity is not None
        return created_identity
    cleanup_ok = True
    if created_identity is not None:
        try:
            current = _stat_at(parent_fd, name)
            if current is not None:
                if _identity(current) != created_identity:
                    cleanup_ok = False
                else:
                    _unlink_exact(root, parent_fd, name, created_identity, maximum=maximum)
        except ReconstructionDraftStoreError:
            cleanup_ok = False
    if not close_ok or not cleanup_ok:
        _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
    if failure is not None:
        raise failure
    _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)


def _read_file(
    root: SafeRoot,
    parent_fd: int,
    name: str,
    *,
    maximum: int,
) -> tuple[bytes, os.stat_result]:
    try:
        return root.read_file_at(parent_fd, name, maximum=maximum)
    except StorageFailure:
        _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)


def _unlink_exact(
    root: SafeRoot,
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int],
    *,
    maximum: int,
) -> None:
    current = _stat_at(parent_fd, name)
    if (
        current is None
        or not _safe_regular(current, root, maximum=maximum)
        or _identity(current) != expected_identity
    ):
        _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
    try:
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError:
        _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)


def _rename_directory_noreplace(parent_fd: int, source: str, destination: str) -> None:
    if sys.platform == "win32":
        os.rename(
            source,
            destination,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        return
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


@dataclass(frozen=True, slots=True)
class _AdmissionRef:
    admission_id: str
    reconstruction_id: str
    admitted_generation: int
    base_head_sha256: str
    image_set_id: str
    image_set_manifest_sha256: str
    observation_digest: str
    proposal_digest: str
    bundle_digest: str
    sha256: str
    size_bytes: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.admission_id) is not str
            or _ADMISSION_ID.fullmatch(self.admission_id) is None
            or type(self.reconstruction_id) is not str
            or _RECONSTRUCTION_ID.fullmatch(self.reconstruction_id) is None
            or type(self.admitted_generation) is not int
            or not 0 <= self.admitted_generation <= MAX_SAFE_JSON_INTEGER
            or type(self.image_set_id) is not str
            or not self.image_set_id.startswith("image_set_")
            or any(
                type(value) is not str or _DIGEST.fullmatch(value) is None
                for value in (
                    self.base_head_sha256,
                    self.image_set_manifest_sha256,
                    self.observation_digest,
                    self.proposal_digest,
                    self.bundle_digest,
                    self.sha256,
                )
            )
            or type(self.size_bytes) is not int
            or not 0 < self.size_bytes <= MAX_VISUAL_ADMISSION_INPUT_BYTES
        ):
            _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
        expected_id = (
            "admission_inputs_"
            + hashlib.sha256(
                _ADMISSION_ID_DOMAIN
                + self.reconstruction_id.encode("ascii")
                + b"\0"
                + str(self.admitted_generation).encode("ascii")
                + b"\0"
                + bytes.fromhex(self.bundle_digest)
            ).hexdigest()[:32]
        )
        if not secrets.compare_digest(self.admission_id, expected_id):
            _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)

    @property
    def filename(self) -> str:
        return self.admission_id + ".json"

    def to_mapping(self) -> dict[str, object]:
        return {
            "admission_id": self.admission_id,
            "admitted_generation": self.admitted_generation,
            "base_head_sha256": self.base_head_sha256,
            "bundle_digest": self.bundle_digest,
            "image_set_id": self.image_set_id,
            "image_set_manifest_sha256": self.image_set_manifest_sha256,
            "observation_digest": self.observation_digest,
            "proposal_digest": self.proposal_digest,
            "reconstruction_id": self.reconstruction_id,
            "schema_version": self.schema_version,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_mapping(cls, value: object) -> _AdmissionRef:
        expected = {
            "admission_id",
            "admitted_generation",
            "base_head_sha256",
            "bundle_digest",
            "image_set_id",
            "image_set_manifest_sha256",
            "observation_digest",
            "proposal_digest",
            "reconstruction_id",
            "schema_version",
            "sha256",
            "size_bytes",
        }
        if type(value) is not dict or set(value) != expected:
            _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
        try:
            return cls(**value)
        except TypeError:
            _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)


@dataclass(frozen=True, slots=True)
class _StoredDraftRecord:
    draft: ReconstructionDraft
    admission_ref: _AdmissionRef | None = None

    def __post_init__(self) -> None:
        if type(self.draft) is not ReconstructionDraft:
            raise TypeError("draft must be an exact ReconstructionDraft")
        reference = self.admission_ref
        if reference is None:
            return
        if type(reference) is not _AdmissionRef:
            raise TypeError("admission_ref must be an exact _AdmissionRef or null")
        allowed = self.draft.status in {
            ReconstructionStatus.PROPOSED,
            ReconstructionStatus.ADOPTING,
            ReconstructionStatus.ADOPTED,
        } or (
            self.draft.status is ReconstructionStatus.RECOVERY_REQUIRED
            and self.draft.adoption_key_sha256 is not None
        )
        if (
            not allowed
            or self.draft.base_head is None
            or self.draft.image_set_id is None
            or self.draft.image_set_manifest_sha256 is None
            or self.draft.observation_ref is None
            or self.draft.proposal_ref is None
            or reference.reconstruction_id != self.draft.reconstruction_id
            or reference.admitted_generation > self.draft.generation
            or reference.base_head_sha256 != self.draft.base_head.sha256
            or reference.image_set_id != self.draft.image_set_id
            or reference.image_set_manifest_sha256 != self.draft.image_set_manifest_sha256
            or reference.observation_digest != self.draft.observation_ref.contract_digest
            or reference.proposal_digest != self.draft.proposal_ref.contract_digest
        ):
            _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)


def _admission_ref_for(
    bundle: VisualAdmissionInputBundle,
    *,
    admitted_generation: int,
    raw: bytes,
) -> _AdmissionRef:
    admission_id = (
        "admission_inputs_"
        + hashlib.sha256(
            _ADMISSION_ID_DOMAIN
            + bundle.reconstruction_id.encode("ascii")
            + b"\0"
            + str(admitted_generation).encode("ascii")
            + b"\0"
            + bytes.fromhex(bundle.bundle_digest)
        ).hexdigest()[:32]
    )
    return _AdmissionRef(
        admission_id=admission_id,
        reconstruction_id=bundle.reconstruction_id,
        admitted_generation=admitted_generation,
        base_head_sha256=bundle.base_head_sha256,
        image_set_id=bundle.image_set_ref.image_set_id,
        image_set_manifest_sha256=bundle.image_set_ref.manifest_sha256,
        observation_digest=bundle.observation_ref.contract_digest,
        proposal_digest=bundle.proposal_ref.contract_digest,
        bundle_digest=bundle.bundle_digest,
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
    )


def _successor_admission(
    previous: _StoredDraftRecord,
    successor: ReconstructionDraft,
) -> _AdmissionRef | None:
    reference = previous.admission_ref
    if reference is None:
        return None
    preserved = successor.status in {
        ReconstructionStatus.PROPOSED,
        ReconstructionStatus.ADOPTING,
        ReconstructionStatus.ADOPTED,
    } or (
        successor.status is ReconstructionStatus.RECOVERY_REQUIRED
        and successor.adoption_key_sha256 is not None
    )
    if not preserved:
        return None
    if (
        successor.base_head != previous.draft.base_head
        or successor.image_set_id != previous.draft.image_set_id
        or successor.image_set_manifest_sha256 != previous.draft.image_set_manifest_sha256
        or successor.observation_ref != previous.draft.observation_ref
        or successor.proposal_ref != previous.draft.proposal_ref
    ):
        return None
    return reference


@dataclass(frozen=True, slots=True)
class _FileEvidence:
    name: str
    sha256: str
    size: int
    dev: str
    ino: str
    uid: str
    mode: int = 0o600

    @classmethod
    def capture(
        cls,
        root: SafeRoot,
        parent_fd: int,
        name: str,
        *,
        maximum: int,
    ) -> _FileEvidence:
        raw, info = _read_file(root, parent_fd, name, maximum=maximum)
        return cls(
            name=name,
            sha256=hashlib.sha256(raw).hexdigest(),
            size=len(raw),
            dev=str(info.st_dev),
            ino=str(info.st_ino),
            uid=str(info.st_uid),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "dev": self.dev,
            "ino": self.ino,
            "mode": self.mode,
            "name": self.name,
            "sha256": self.sha256,
            "size": self.size,
            "uid": self.uid,
        }

    @classmethod
    def from_mapping(cls, value: object, *, name_pattern: re.Pattern[str]) -> _FileEvidence:
        expected = {"dev", "ino", "mode", "name", "sha256", "size", "uid"}
        if type(value) is not dict or set(value) != expected:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        if (
            type(value["name"]) is not str
            or name_pattern.fullmatch(value["name"]) is None
            or type(value["sha256"]) is not str
            or _DIGEST.fullmatch(value["sha256"]) is None
            or type(value["size"]) is not int
            or not 0 <= value["size"] <= max(_MAX_STORED_RECORD_BYTES, _MAX_PAYLOAD_BYTES)
            or type(value["mode"]) is not int
            or value["mode"] != 0o600
            or any(
                type(value[key]) is not str or _DECIMAL_ID.fullmatch(value[key]) is None
                for key in ("dev", "ino", "uid")
            )
        ):
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        return cls(
            name=value["name"],
            sha256=value["sha256"],
            size=value["size"],
            dev=value["dev"],
            ino=value["ino"],
            uid=value["uid"],
            mode=value["mode"],
        )


def _journal_line(body: Mapping[str, object]) -> bytes:
    body_bytes = _canonical_json(dict(body), maximum=_MAX_JOURNAL_BYTES)
    digest = hashlib.sha256(_JOURNAL_CHECKSUM_DOMAIN + body_bytes).hexdigest()
    raw = _canonical_json(
        {"body": dict(body), "body_sha256": digest, "schema_version": 1},
        maximum=_MAX_JOURNAL_BYTES,
    )
    if len(raw) + 1 > _MAX_JOURNAL_BYTES:
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    return raw + b"\n"


def _decode_journal_line(raw: bytes) -> dict[str, object]:
    decoded = _decode_canonical(raw, maximum=_MAX_JOURNAL_BYTES)
    if type(decoded) is not dict or set(decoded) != {
        "body",
        "body_sha256",
        "schema_version",
    }:
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    body = decoded["body"]
    checksum = decoded["body_sha256"]
    if (
        type(decoded["schema_version"]) is not int
        or decoded["schema_version"] != 1
        or type(body) is not dict
        or type(checksum) is not str
        or _DIGEST.fullmatch(checksum) is None
    ):
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    expected = hashlib.sha256(
        _JOURNAL_CHECKSUM_DOMAIN + _canonical_json(body, maximum=_MAX_JOURNAL_BYTES)
    ).hexdigest()
    if not secrets.compare_digest(checksum, expected):
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    return body


@dataclass(frozen=True, slots=True)
class _PayloadPlan:
    name: str
    temp_name: str
    sha256: str
    size: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "size": self.size,
            "temp_name": self.temp_name,
        }

    @classmethod
    def from_mapping(cls, value: object) -> _PayloadPlan:
        if type(value) is not dict or set(value) != {"name", "sha256", "size", "temp_name"}:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        if (
            type(value["name"]) is not str
            or _PAYLOAD_NAME.fullmatch(value["name"]) is None
            or type(value["temp_name"]) is not str
            or _PAYLOAD_TEMP_NAME.fullmatch(value["temp_name"]) is None
            or not value["temp_name"].startswith(f".{value['name']}.")
            or type(value["sha256"]) is not str
            or _DIGEST.fullmatch(value["sha256"]) is None
            or type(value["size"]) is not int
            or not 0 < value["size"] <= _MAX_PAYLOAD_BYTES
        ):
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        return cls(
            name=value["name"],
            temp_name=value["temp_name"],
            sha256=value["sha256"],
            size=value["size"],
        )


@dataclass(frozen=True, slots=True)
class _MutationJournal:
    state: str
    reconstruction_id: str
    old_sha256: str
    new_sha256: str
    new_size: int
    record_temp_name: str
    add_payloads: tuple[_PayloadPlan, ...]
    remove_payloads: tuple[_FileEvidence, ...]
    record_temp: _FileEvidence | None = None
    added_payloads: tuple[_FileEvidence, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in {"RESERVED", "STAGED"}:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        if (
            type(self.reconstruction_id) is not str
            or _RECONSTRUCTION_ID.fullmatch(self.reconstruction_id) is None
            or _DIGEST.fullmatch(self.old_sha256) is None
            or _DIGEST.fullmatch(self.new_sha256) is None
            or self.old_sha256 == self.new_sha256
            or type(self.new_size) is not int
            or not 0 < self.new_size <= _MAX_STORED_RECORD_BYTES
            or _RECORD_TEMP_NAME.fullmatch(self.record_temp_name) is None
            or len({item.name for item in self.add_payloads}) != len(self.add_payloads)
            or len({item.name for item in self.remove_payloads}) != len(self.remove_payloads)
            or {item.name for item in self.add_payloads}
            & {item.name for item in self.remove_payloads}
        ):
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        if self.state == "RESERVED":
            if self.record_temp is not None or self.added_payloads:
                _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        elif (
            self.record_temp is None
            or self.record_temp.name != self.record_temp_name
            or self.record_temp.sha256 != self.new_sha256
            or self.record_temp.size != self.new_size
            or tuple(item.name for item in self.added_payloads)
            != tuple(item.name for item in self.add_payloads)
            or any(
                evidence.sha256 != plan.sha256 or evidence.size != plan.size
                for evidence, plan in zip(self.added_payloads, self.add_payloads, strict=True)
            )
        ):
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)

    def to_body(self) -> dict[str, object]:
        body: dict[str, object] = {
            "add_payloads": [item.to_mapping() for item in self.add_payloads],
            "new_sha256": self.new_sha256,
            "new_size": self.new_size,
            "old_sha256": self.old_sha256,
            "reconstruction_id": self.reconstruction_id,
            "record_temp_name": self.record_temp_name,
            "remove_payloads": [item.to_mapping() for item in self.remove_payloads],
            "schema_version": 1,
            "state": self.state,
        }
        if self.state == "STAGED":
            assert self.record_temp is not None
            body["added_payloads"] = [item.to_mapping() for item in self.added_payloads]
            body["record_temp"] = self.record_temp.to_mapping()
        return body

    def to_line(self) -> bytes:
        return _journal_line(self.to_body())

    @classmethod
    def from_body(cls, body: object) -> _MutationJournal:
        common = {
            "add_payloads",
            "new_sha256",
            "new_size",
            "old_sha256",
            "reconstruction_id",
            "record_temp_name",
            "remove_payloads",
            "schema_version",
            "state",
        }
        if type(body) is not dict or body.get("state") not in {"RESERVED", "STAGED"}:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        expected = (
            common
            if body["state"] == "RESERVED"
            else common
            | {
                "added_payloads",
                "record_temp",
            }
        )
        if (
            set(body) != expected
            or type(body["schema_version"]) is not int
            or body["schema_version"] != 1
        ):
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        if type(body["add_payloads"]) is not list or type(body["remove_payloads"]) is not list:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        add_payloads = tuple(_PayloadPlan.from_mapping(item) for item in body["add_payloads"])
        remove_payloads = tuple(
            _FileEvidence.from_mapping(item, name_pattern=_PAYLOAD_NAME)
            for item in body["remove_payloads"]
        )
        record_temp = None
        added_payloads: tuple[_FileEvidence, ...] = ()
        if body["state"] == "STAGED":
            if type(body["added_payloads"]) is not list:
                _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
            record_temp = _FileEvidence.from_mapping(
                body["record_temp"],
                name_pattern=_RECORD_TEMP_NAME,
            )
            added_payloads = tuple(
                _FileEvidence.from_mapping(item, name_pattern=_PAYLOAD_NAME)
                for item in body["added_payloads"]
            )
        for name in ("old_sha256", "new_sha256", "record_temp_name", "reconstruction_id"):
            if type(body[name]) is not str:
                _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        if type(body["new_size"]) is not int:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        return cls(
            state=body["state"],
            reconstruction_id=body["reconstruction_id"],
            old_sha256=body["old_sha256"],
            new_sha256=body["new_sha256"],
            new_size=body["new_size"],
            record_temp_name=body["record_temp_name"],
            add_payloads=add_payloads,
            remove_payloads=remove_payloads,
            record_temp=record_temp,
            added_payloads=added_payloads,
        )


def _read_journal(
    root: SafeRoot,
    draft_fd: int,
) -> tuple[_MutationJournal, _MutationJournal | None, int, bytes, os.stat_result]:
    raw, info = _read_file(root, draft_fd, _JOURNAL_NAME, maximum=_MAX_JOURNAL_BYTES)
    parts = raw.split(b"\n")
    complete = parts[:-1]
    partial = parts[-1]
    if not complete or len(complete) > 2 or any(not item for item in complete):
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    reserved = _MutationJournal.from_body(_decode_journal_line(complete[0]))
    if reserved.state != "RESERVED":
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    staged = None
    if len(complete) == 2:
        if partial:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        staged = _MutationJournal.from_body(_decode_journal_line(complete[1]))
        if staged.state != "STAGED":
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        for field in (
            "reconstruction_id",
            "old_sha256",
            "new_sha256",
            "new_size",
            "record_temp_name",
            "add_payloads",
            "remove_payloads",
        ):
            if getattr(staged, field) != getattr(reserved, field):
                _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    valid_length = sum(len(item) + 1 for item in complete)
    return reserved, staged, valid_length, partial, info


def _create_journal(
    root: SafeRoot,
    draft_fd: int,
    reserved: _MutationJournal,
) -> tuple[int, int]:
    identity = _write_exclusive(
        root,
        draft_fd,
        _JOURNAL_NAME,
        reserved.to_line(),
        maximum=_MAX_JOURNAL_BYTES,
    )
    try:
        os.fsync(draft_fd)
    except OSError:
        _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
    return identity


def _append_staged_journal(
    root: SafeRoot,
    draft_fd: int,
    journal_identity: tuple[int, int],
    reserved: _MutationJournal,
    staged: _MutationJournal,
    *,
    valid_length: int,
    partial: bytes = b"",
) -> None:
    reserved_line = reserved.to_line()
    staged_line = staged.to_line()
    if partial and (len(partial) >= len(staged_line) or staged_line[: len(partial)] != partial):
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    raw, info = _read_file(root, draft_fd, _JOURNAL_NAME, maximum=_MAX_JOURNAL_BYTES)
    if (
        _identity(info) != journal_identity
        or raw != reserved_line + partial
        or valid_length != len(reserved_line)
        or valid_length + len(staged_line) > _MAX_JOURNAL_BYTES
    ):
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    fd = -1
    failure: ReconstructionDraftStoreError | None = None
    try:
        try:
            fd = os.open(
                _JOURNAL_NAME,
                os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=draft_fd,
            )
            opened = os.fstat(fd)
            if (
                not _safe_regular(opened, root, maximum=_MAX_JOURNAL_BYTES)
                or _identity(opened) != journal_identity
                or os.get_inheritable(fd)
            ):
                _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
            if opened.st_size != len(reserved_line) + len(partial):
                _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
            if partial:
                os.ftruncate(fd, valid_length)
                os.fsync(fd)
            _write_all(fd, staged_line)
            os.fsync(fd)
        except ReconstructionDraftStoreError as error:
            failure = error
        except OSError:
            failure = ReconstructionDraftStoreError(ReconstructionDraftStoreErrorCode.IO_ERROR)
    finally:
        close_ok = fd < 0 or _close(fd)
    if failure is not None:
        raise failure
    if not close_ok:
        _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
    current_reserved, current_staged, _, tail, current_info = _read_journal(root, draft_fd)
    if (
        current_reserved != reserved
        or current_staged != staged
        or tail
        or _identity(current_info) != journal_identity
    ):
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)


@dataclass(frozen=True, slots=True)
class _CatalogSnapshot:
    draft_ids: tuple[str, ...]
    stages: tuple[tuple[str, tuple[int, int]], ...]
    journal_draft_id: str | None
    total_bytes: int


def _scan_draft_directory(
    root: SafeRoot,
    draft_fd: int,
    reconstruction_id: str,
) -> tuple[int, bool]:
    try:
        names = os.listdir(draft_fd)
    except OSError:
        _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
    if len(names) > _MAX_JSON_NODES:
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    if len(names) != len(set(names)):
        _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
    total = 0
    journal_present = False
    temp_present = False
    record_present = False
    for name in names:
        if type(name) is not str or name in {"", ".", ".."}:
            _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
        info = _stat_at(draft_fd, name)
        if info is None:
            _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
        if name == _RECORD_NAME:
            maximum = _MAX_STORED_RECORD_BYTES
            record_present = True
        elif _PAYLOAD_NAME.fullmatch(name) is not None:
            maximum = _MAX_PAYLOAD_BYTES
        elif name == _JOURNAL_NAME:
            maximum = _MAX_JOURNAL_BYTES
            journal_present = True
        elif _RECORD_TEMP_NAME.fullmatch(name) is not None:
            maximum = _MAX_STORED_RECORD_BYTES
            temp_present = True
        elif _PAYLOAD_TEMP_NAME.fullmatch(name) is not None:
            maximum = _MAX_PAYLOAD_BYTES
            temp_present = True
        else:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        if not _safe_regular(info, root, maximum=maximum):
            _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
        total += info.st_size
        if total > MAX_RECONSTRUCTION_DRAFT_STORE_BYTES:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    if not record_present or (temp_present and not journal_present):
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    if journal_present:
        journal, staged, _valid, _partial, _info = _read_journal(root, draft_fd)
        selected = staged if staged is not None else journal
        if selected.reconstruction_id != reconstruction_id:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    return total, journal_present


def _scan_stage_directory(
    root: SafeRoot,
    stage_fd: int,
) -> tuple[int, tuple[tuple[str, tuple[int, int], int], ...]]:
    try:
        names = os.listdir(stage_fd)
    except OSError:
        _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
    if len(names) > _MAX_JSON_NODES or len(names) != len(set(names)):
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    total = 0
    inventory: list[tuple[str, tuple[int, int], int]] = []
    for name in names:
        info = _stat_at(stage_fd, name)
        if info is None:
            _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
        if name == _RECORD_NAME:
            maximum = _MAX_STORED_RECORD_BYTES
        elif _PAYLOAD_NAME.fullmatch(name) is not None:
            maximum = _MAX_PAYLOAD_BYTES
        else:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        if not _safe_regular(info, root, maximum=maximum):
            _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
        inventory.append((name, _identity(info), maximum))
        total += info.st_size
        if total > MAX_RECONSTRUCTION_DRAFT_STORE_BYTES:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    return total, tuple(sorted(inventory))


def _scan_catalog(root: SafeRoot, root_fd: int) -> _CatalogSnapshot:
    try:
        names = os.listdir(root_fd)
    except OSError:
        _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
    if len(names) > MAX_RECONSTRUCTION_DRAFTS + MAX_RECONSTRUCTION_DRAFT_MUTATIONS:
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    draft_ids: list[str] = []
    stages: list[tuple[str, tuple[int, int]]] = []
    journal_draft_id = None
    total_bytes = 0
    for name in names:
        if type(name) is not str:
            _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
        entry = _stat_at(root_fd, name)
        if entry is None:
            _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
        if _RECONSTRUCTION_ID.fullmatch(name) is not None:
            draft_fd = -1
            try:
                draft_fd, _opened = root.open_directory_at(
                    root_fd,
                    name,
                    expected_identity=_identity(entry),
                )
                selected_bytes, journal_present = _scan_draft_directory(root, draft_fd, name)
                root.verify_directory_entry(root_fd, name, expected=_opened)
            except StorageFailure:
                _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
            finally:
                if draft_fd >= 0 and not _close(draft_fd):
                    _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
            draft_ids.append(name)
            total_bytes += selected_bytes
            if journal_present:
                if journal_draft_id is not None:
                    _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
                journal_draft_id = name
        elif _STAGE_NAME.fullmatch(name) is not None:
            stage_fd = -1
            try:
                stage_fd, _opened = root.open_directory_at(
                    root_fd,
                    name,
                    expected_identity=_identity(entry),
                )
                selected_bytes, _inventory = _scan_stage_directory(root, stage_fd)
                root.verify_directory_entry(root_fd, name, expected=_opened)
                total_bytes += selected_bytes
            except StorageFailure:
                _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
            finally:
                if stage_fd >= 0 and not _close(stage_fd):
                    _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
            stages.append((name, _identity(entry)))
        else:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        if total_bytes > MAX_RECONSTRUCTION_DRAFT_STORE_BYTES:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    if (
        len(draft_ids) > MAX_RECONSTRUCTION_DRAFTS
        or len(stages) + (1 if journal_draft_id is not None else 0)
        > MAX_RECONSTRUCTION_DRAFT_MUTATIONS
    ):
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    return _CatalogSnapshot(
        draft_ids=tuple(sorted(draft_ids)),
        stages=tuple(sorted(stages)),
        journal_draft_id=journal_draft_id,
        total_bytes=total_bytes,
    )


def _remove_stage(
    root: SafeRoot,
    root_fd: int,
    name: str,
    expected_identity: tuple[int, int],
) -> None:
    stage_fd = -1
    try:
        try:
            stage_fd, opened = root.open_directory_at(
                root_fd,
                name,
                expected_identity=expected_identity,
            )
        except StorageFailure:
            _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
        _total, inventory = _scan_stage_directory(root, stage_fd)
        root.verify_directory_entry(root_fd, name, expected=opened)
        for child, child_identity, maximum in inventory:
            _unlink_exact(root, stage_fd, child, child_identity, maximum=maximum)
        current = _stat_at(root_fd, name)
        if current is None or _identity(current) != _identity(opened):
            _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
        os.rmdir(name, dir_fd=root_fd)
        os.fsync(root_fd)
    except ReconstructionDraftStoreError:
        raise
    except (OSError, StorageFailure):
        _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
    finally:
        if stage_fd >= 0 and not _close(stage_fd) and sys.exception() is None:
            _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)


def _acquire(
    manager: ResourceLeaseManager,
    resource_id: str,
) -> ResourceLease:
    deadline = time.monotonic() + _LEASE_WAIT_SECONDS
    while True:
        try:
            lease = manager.acquire(resource_id)
        except LeaseError as error:
            if error.code is LeaseErrorCode.CONTENDED and time.monotonic() < deadline:
                time.sleep(_LEASE_RETRY_SECONDS)
                continue
            _raise(ReconstructionDraftStoreErrorCode.LOCK_UNAVAILABLE)
        if type(lease) is not ResourceLease:
            _raise(ReconstructionDraftStoreErrorCode.LOCK_UNAVAILABLE)
        return lease


def _release(lease: ResourceLease) -> bool:
    try:
        lease.release(owner_token=lease.owner_token)
    except (LeaseError, OSError):
        return False
    return True


def _translate_draft_error(error: ReconstructionDraftError, *, transition: bool = False) -> None:
    if transition:
        _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
    if error.code.value == "budget_exceeded":
        _raise(ReconstructionDraftStoreErrorCode.RECORD_TOO_LARGE)
    _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)


def _encode_public_draft(draft: ReconstructionDraft) -> bytes:
    try:
        raw = encode_reconstruction_draft(draft)
        decoded = decode_reconstruction_draft(raw)
    except ReconstructionDraftError as error:
        _translate_draft_error(error)
    if len(raw) > MAX_RECONSTRUCTION_DRAFT_RECORD_BYTES or decoded != draft:
        _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
    return raw


def _encode_record(record: _StoredDraftRecord) -> bytes:
    if type(record) is not _StoredDraftRecord:
        raise TypeError("record must be an exact _StoredDraftRecord")
    draft_raw = _encode_public_draft(record.draft)
    if record.admission_ref is None:
        return draft_raw
    draft_mapping = _decode_canonical(
        draft_raw,
        maximum=MAX_RECONSTRUCTION_DRAFT_RECORD_BYTES,
    )
    body = {
        "admission_ref": record.admission_ref.to_mapping(),
        "draft_record": draft_mapping,
    }
    body_raw = _canonical_json(body, maximum=_MAX_STORED_RECORD_BYTES)
    raw = _canonical_json(
        {
            "body": body,
            "body_sha256": hashlib.sha256(_STORED_RECORD_CHECKSUM_DOMAIN + body_raw).hexdigest(),
            "storage_epoch": 2,
        },
        maximum=_MAX_STORED_RECORD_BYTES,
    )
    if _decode_record(raw, record.draft.reconstruction_id) != record:
        _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
    return raw


def _decode_record(raw: bytes, reconstruction_id: str) -> _StoredDraftRecord:
    decoded = _decode_canonical(raw, maximum=_MAX_STORED_RECORD_BYTES)
    if type(decoded) is not dict:
        _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
    if set(decoded) != {"body", "body_sha256", "storage_epoch"}:
        try:
            draft = decode_reconstruction_draft(raw)
        except ReconstructionDraftError as error:
            _translate_draft_error(error)
        if draft.reconstruction_id != reconstruction_id or _encode_public_draft(draft) != raw:
            _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
        return _StoredDraftRecord(draft=draft)
    body = decoded["body"]
    checksum = decoded["body_sha256"]
    if (
        type(decoded["storage_epoch"]) is not int
        or decoded["storage_epoch"] != 2
        or type(body) is not dict
        or set(body) != {"admission_ref", "draft_record"}
        or type(checksum) is not str
        or _DIGEST.fullmatch(checksum) is None
    ):
        _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
    body_raw = _canonical_json(body, maximum=_MAX_STORED_RECORD_BYTES)
    expected = hashlib.sha256(_STORED_RECORD_CHECKSUM_DOMAIN + body_raw).hexdigest()
    if not secrets.compare_digest(checksum, expected):
        _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
    if _canonical_json(decoded, maximum=_MAX_STORED_RECORD_BYTES) != raw:
        _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
    draft_raw = _canonical_ascii_json(
        body["draft_record"],
        maximum=MAX_RECONSTRUCTION_DRAFT_RECORD_BYTES,
    )
    try:
        draft = decode_reconstruction_draft(draft_raw)
    except ReconstructionDraftError as error:
        _translate_draft_error(error)
    record = _StoredDraftRecord(
        draft=draft,
        admission_ref=_AdmissionRef.from_mapping(body["admission_ref"]),
    )
    if draft.reconstruction_id != reconstruction_id:
        _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
    return record


def _normalize_payloads(value: object) -> tuple[ReconstructionPayload, ...]:
    if type(value) is not tuple or any(type(item) is not ReconstructionPayload for item in value):
        raise TypeError("payloads must be an exact tuple of ReconstructionPayload values")
    if len(value) > _MAX_JSON_NODES:
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    by_id = {item.ref.id: item for item in value}
    if len(by_id) != len(value):
        _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
    return tuple(sorted(value, key=lambda item: item.ref.id))


def _refs_by_filename(
    record: _StoredDraftRecord,
) -> dict[str, ReconstructionPayloadRef | _AdmissionRef]:
    references: tuple[ReconstructionPayloadRef | _AdmissionRef, ...] = record.draft.payload_refs + (
        () if record.admission_ref is None else (record.admission_ref,)
    )
    result = {item.filename: item for item in references}
    if len(result) != len(references) or any(
        _PAYLOAD_NAME.fullmatch(name) is None for name in result
    ):
        _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
    return result


def _payloads_by_filename(
    draft: ReconstructionDraft,
    payloads: tuple[ReconstructionPayload, ...],
) -> dict[str, ReconstructionPayload]:
    expected = {item.filename: item for item in draft.payload_refs}
    result: dict[str, ReconstructionPayload] = {}
    for payload in payloads:
        reference = expected.get(payload.ref.filename)
        if reference is None or reference != payload.ref:
            _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
        result[payload.ref.filename] = payload
    return result


def _validate_payload_file(
    root: SafeRoot,
    draft_fd: int,
    reference: ReconstructionPayloadRef | _AdmissionRef,
) -> _FileEvidence:
    raw, info = _read_file(root, draft_fd, reference.filename, maximum=reference.size_bytes)
    if type(reference) is ReconstructionPayloadRef:
        try:
            ReconstructionPayload(ref=reference, raw=raw)
        except ReconstructionDraftError as error:
            _translate_draft_error(error)
    elif type(reference) is _AdmissionRef:
        try:
            bundle = decode_visual_admission_inputs(raw)
        except VisualAdmissionInputError:
            _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
        if (
            _admission_ref_for(
                bundle,
                admitted_generation=reference.admitted_generation,
                raw=raw,
            )
            != reference
        ):
            _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
    else:  # pragma: no cover - closed private union.
        raise TypeError("reference must be an exact stored payload reference")
    if len(raw) != reference.size_bytes:
        _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
    evidence = _FileEvidence(
        name=reference.filename,
        sha256=hashlib.sha256(raw).hexdigest(),
        size=len(raw),
        dev=str(info.st_dev),
        ino=str(info.st_ino),
        uid=str(info.st_uid),
    )
    if evidence.sha256 != reference.sha256:
        _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
    return evidence


def _load_draft_fd(
    root: SafeRoot,
    draft_fd: int,
    reconstruction_id: str,
) -> tuple[_StoredDraftRecord, bytes]:
    _scan_draft_directory(root, draft_fd, reconstruction_id)
    raw, _record_info = _read_file(
        root,
        draft_fd,
        _RECORD_NAME,
        maximum=_MAX_STORED_RECORD_BYTES,
    )
    record = _decode_record(raw, reconstruction_id)
    references = _refs_by_filename(record)
    expected_names = {_RECORD_NAME, *references}
    for reference in references.values():
        _validate_payload_file(root, draft_fd, reference)

    names = set(os.listdir(draft_fd))
    if _JOURNAL_NAME not in names:
        if names != expected_names:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        return record, raw

    reserved, staged, _valid, _partial, _info = _read_journal(root, draft_fd)
    selected = staged if staged is not None else reserved
    allowed = expected_names | {
        _JOURNAL_NAME,
        selected.record_temp_name,
        *(item.name for item in selected.add_payloads),
        *(item.temp_name for item in selected.add_payloads),
        *(item.name for item in selected.remove_payloads),
    }
    if not names.issubset(allowed):
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    return record, raw


def _evidence_matches(
    root: SafeRoot,
    parent_fd: int,
    evidence: _FileEvidence,
    *,
    maximum: int,
) -> bool:
    current = _stat_at(parent_fd, evidence.name)
    if current is None:
        return False
    if not _safe_regular(current, root, maximum=maximum):
        _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
    captured = _FileEvidence.capture(
        root,
        parent_fd,
        evidence.name,
        maximum=maximum,
    )
    if captured != evidence:
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    return True


def _open_verified(
    root: SafeRoot,
    parent_fd: int,
    evidence: _FileEvidence,
    *,
    maximum: int,
) -> tuple[int, tuple[int, int]]:
    if not _evidence_matches(root, parent_fd, evidence, maximum=maximum):
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    fd = -1
    try:
        fd = os.open(
            evidence.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        opened = os.fstat(fd)
        current = _stat_at(parent_fd, evidence.name)
        expected_identity = (int(evidence.dev), int(evidence.ino))
        if (
            current is None
            or not _safe_regular(opened, root, maximum=maximum)
            or not _safe_regular(current, root, maximum=maximum)
            or _identity(opened) != expected_identity
            or _identity(current) != expected_identity
            or opened.st_size != evidence.size
            or current.st_size != evidence.size
            or _hash_fd(fd, opened, size=evidence.size) != evidence.sha256
            or os.get_inheritable(fd)
        ):
            _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
        return fd, expected_identity
    except ReconstructionDraftStoreError:
        if fd >= 0:
            _close(fd)
        raise
    except OSError:
        if fd >= 0:
            _close(fd)
        _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)


def _verify_replaced(
    root: SafeRoot,
    parent_fd: int,
    target_name: str,
    publication_fd: int,
    evidence: _FileEvidence,
    *,
    maximum: int,
) -> None:
    opened = os.fstat(publication_fd)
    current = _stat_at(parent_fd, target_name)
    identity = (int(evidence.dev), int(evidence.ino))
    if (
        current is None
        or not _safe_regular(opened, root, maximum=maximum)
        or not _safe_regular(current, root, maximum=maximum)
        or _identity(opened) != identity
        or _identity(current) != identity
        or opened.st_size != evidence.size
        or current.st_size != evidence.size
        or _hash_fd(publication_fd, opened, size=evidence.size) != evidence.sha256
    ):
        _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)


def _open_draft_at(
    root: SafeRoot,
    root_fd: int,
    reconstruction_id: str,
) -> tuple[int, os.stat_result]:
    selected = _stat_at(root_fd, _record_directory_name(reconstruction_id))
    if selected is None:
        _raise(ReconstructionDraftStoreErrorCode.NOT_FOUND)
    try:
        return root.open_directory_at(
            root_fd,
            reconstruction_id,
            expected_identity=_identity(selected),
        )
    except StorageFailure:
        _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)


def _validate_journal_bindings(
    journal: _MutationJournal,
    previous: _StoredDraftRecord,
    successor: _StoredDraftRecord,
) -> tuple[
    dict[str, ReconstructionPayloadRef | _AdmissionRef],
    dict[str, ReconstructionPayloadRef | _AdmissionRef],
]:
    old_refs = _refs_by_filename(previous)
    new_refs = _refs_by_filename(successor)
    expected_add = tuple(sorted(set(new_refs) - set(old_refs)))
    expected_remove = tuple(sorted(set(old_refs) - set(new_refs)))
    if (
        successor.draft.reconstruction_id != journal.reconstruction_id
        or hashlib.sha256(_encode_record(previous)).hexdigest() != journal.old_sha256
        or hashlib.sha256(_encode_record(successor)).hexdigest() != journal.new_sha256
        or len(_encode_record(successor)) != journal.new_size
        or tuple(item.name for item in journal.add_payloads) != expected_add
        or tuple(item.name for item in journal.remove_payloads) != expected_remove
        or any(
            plan.sha256 != new_refs[plan.name].sha256 or plan.size != new_refs[plan.name].size_bytes
            for plan in journal.add_payloads
        )
        or any(
            evidence.sha256 != old_refs[evidence.name].sha256
            or evidence.size != old_refs[evidence.name].size_bytes
            for evidence in journal.remove_payloads
        )
    ):
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    return old_refs, new_refs


def _validate_committed_journal(
    journal: _MutationJournal,
    successor: _StoredDraftRecord,
) -> dict[str, ReconstructionPayloadRef | _AdmissionRef]:
    new_raw = _encode_record(successor)
    new_refs = _refs_by_filename(successor)
    if (
        successor.draft.reconstruction_id != journal.reconstruction_id
        or hashlib.sha256(new_raw).hexdigest() != journal.new_sha256
        or len(new_raw) != journal.new_size
        or any(
            plan.name not in new_refs
            or plan.sha256 != new_refs[plan.name].sha256
            or plan.size != new_refs[plan.name].size_bytes
            for plan in journal.add_payloads
        )
        or any(evidence.name in new_refs for evidence in journal.remove_payloads)
    ):
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    return new_refs


def _validate_old_record_rollback(
    journal: _MutationJournal,
    current: _StoredDraftRecord,
) -> None:
    """Bind rollback deletion authority to the still-authoritative old record."""

    current_refs = _refs_by_filename(current)
    if (
        current.draft.reconstruction_id != journal.reconstruction_id
        or any(plan.name in current_refs for plan in journal.add_payloads)
        or any(
            evidence.name not in current_refs
            or evidence.sha256 != current_refs[evidence.name].sha256
            or evidence.size != current_refs[evidence.name].size_bytes
            for evidence in journal.remove_payloads
        )
    ):
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)


def _capture_expected_file(
    root: SafeRoot,
    parent_fd: int,
    name: str,
    sha256: str,
    size: int,
    *,
    maximum: int,
) -> _FileEvidence:
    evidence = _FileEvidence.capture(root, parent_fd, name, maximum=maximum)
    if evidence.sha256 != sha256 or evidence.size != size:
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    return evidence


def _remove_if_exact(
    root: SafeRoot,
    parent_fd: int,
    evidence: _FileEvidence,
    *,
    maximum: int,
    missing_ok: bool,
) -> None:
    current = _stat_at(parent_fd, evidence.name)
    if current is None:
        if missing_ok:
            return
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    if not _evidence_matches(root, parent_fd, evidence, maximum=maximum):
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    _unlink_exact(root, parent_fd, evidence.name, _identity(current), maximum=maximum)


def _publish_payload_plan(
    root: SafeRoot,
    draft_fd: int,
    plan: _PayloadPlan,
    reference: ReconstructionPayloadRef | _AdmissionRef,
) -> _FileEvidence:
    if (
        plan.name != reference.filename
        or plan.sha256 != reference.sha256
        or plan.size != reference.size_bytes
    ):
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    final = _stat_at(draft_fd, plan.name)
    if final is not None:
        evidence = _capture_expected_file(
            root,
            draft_fd,
            plan.name,
            plan.sha256,
            plan.size,
            maximum=reference.size_bytes,
        )
        _validate_payload_file(root, draft_fd, reference)
        return evidence
    temp = _stat_at(draft_fd, plan.temp_name)
    if temp is None:
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    _capture_expected_file(
        root,
        draft_fd,
        plan.temp_name,
        plan.sha256,
        plan.size,
        maximum=reference.size_bytes,
    )
    try:
        _rename_directory_noreplace(draft_fd, plan.temp_name, plan.name)
        os.fsync(draft_fd)
    except FileExistsError:
        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
    except OSError:
        _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
    evidence = _capture_expected_file(
        root,
        draft_fd,
        plan.name,
        plan.sha256,
        plan.size,
        maximum=reference.size_bytes,
    )
    _validate_payload_file(root, draft_fd, reference)
    return evidence


class ReconstructionDraftStore:
    """One captured-root catalog of generation-CAS reconstruction drafts."""

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
            or any(type(item) is not int or item < 0 for item in expected_root_identity)
            or type(lease_manager) is not ResourceLeaseManager
        ):
            raise TypeError("invalid reconstruction draft store composition")
        try:
            selected = SafeRoot(root)
        except StorageFailure:
            _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
        if selected.identity != expected_root_identity:
            _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
        self._root = selected
        self._lease_manager = lease_manager

    def _acquire_catalog(self) -> ResourceLease:
        return _acquire(self._lease_manager, _CATALOG_RESOURCE)

    def _acquire_draft(self, reconstruction_id: str) -> ResourceLease:
        return _acquire(
            self._lease_manager,
            f"reconstruction-draft-store:{reconstruction_id}",
        )

    def validate_record(self, draft: object, generation: object) -> None:
        selected_generation = _generation(generation)
        if type(draft) is not ReconstructionDraft:
            raise TypeError("draft must be an exact ReconstructionDraft")
        if draft.generation != selected_generation:
            _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
        if len(draft.provider_invocations) > MAX_RECONSTRUCTION_DRAFT_INVOCATIONS:
            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
        _encode_record(_StoredDraftRecord(draft=draft))

    def load(self, reconstruction_id: object) -> ReconstructionDraft:
        selected_id = _draft_id(reconstruction_id)
        catalog = self._acquire_catalog()
        draft_lease = None
        root_fd = -1
        draft_fd = -1
        result = None
        failure: ReconstructionDraftStoreError | None = None
        try:
            try:
                self._recover_pending_locked()
                root_fd = self._root.open()
                _scan_catalog(self._root, root_fd)
                draft_lease = self._acquire_draft(selected_id)
                draft_fd, _draft_info = _open_draft_at(self._root, root_fd, selected_id)
                record, _raw = _load_draft_fd(self._root, draft_fd, selected_id)
                result = record.draft
                self._root.verify_directory_entry(
                    root_fd,
                    selected_id,
                    expected=os.fstat(draft_fd),
                )
            except ReconstructionDraftStoreError as error:
                failure = error
            except (OSError, StorageFailure):
                failure = ReconstructionDraftStoreError(ReconstructionDraftStoreErrorCode.IO_ERROR)
        finally:
            draft_close_ok = draft_fd < 0 or _close(draft_fd)
            root_close_ok = root_fd < 0 or _close(root_fd)
            draft_release_ok = draft_lease is None or _release(draft_lease)
            catalog_release_ok = _release(catalog)
        if failure is not None:
            raise failure
        if (
            result is None
            or not draft_close_ok
            or not root_close_ok
            or not draft_release_ok
            or not catalog_release_ok
        ):
            _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
        return result

    def load_payload(
        self,
        reconstruction_id: object,
        reference: object,
    ) -> ReconstructionPayload:
        selected_id = _draft_id(reconstruction_id)
        if type(reference) is not ReconstructionPayloadRef:
            raise TypeError("reference must be an exact ReconstructionPayloadRef")
        catalog = self._acquire_catalog()
        draft_lease = None
        root_fd = -1
        draft_fd = -1
        result = None
        failure: ReconstructionDraftStoreError | None = None
        try:
            try:
                self._recover_pending_locked()
                root_fd = self._root.open()
                _scan_catalog(self._root, root_fd)
                draft_lease = self._acquire_draft(selected_id)
                draft_fd, _draft_info = _open_draft_at(self._root, root_fd, selected_id)
                record, _raw = _load_draft_fd(self._root, draft_fd, selected_id)
                current = {item.filename: item for item in record.draft.payload_refs}.get(
                    reference.filename
                )
                if current is None:
                    _raise(ReconstructionDraftStoreErrorCode.NOT_FOUND)
                if current != reference:
                    _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
                payload_raw, _payload_info = _read_file(
                    self._root,
                    draft_fd,
                    reference.filename,
                    maximum=reference.size_bytes,
                )
                try:
                    result = ReconstructionPayload(ref=reference, raw=payload_raw)
                except ReconstructionDraftError as error:
                    _translate_draft_error(error)
                self._root.verify_directory_entry(
                    root_fd,
                    selected_id,
                    expected=os.fstat(draft_fd),
                )
            except ReconstructionDraftStoreError as error:
                failure = error
            except (OSError, StorageFailure):
                failure = ReconstructionDraftStoreError(ReconstructionDraftStoreErrorCode.IO_ERROR)
        finally:
            draft_close_ok = draft_fd < 0 or _close(draft_fd)
            root_close_ok = root_fd < 0 or _close(root_fd)
            draft_release_ok = draft_lease is None or _release(draft_lease)
            catalog_release_ok = _release(catalog)
        if failure is not None:
            raise failure
        if (
            result is None
            or not draft_close_ok
            or not root_close_ok
            or not draft_release_ok
            or not catalog_release_ok
        ):
            _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
        return result

    def _load_admission_exact(
        self,
        reconstruction_id: object,
        *,
        expected_generation: object,
        expected_proposal_ref: object,
    ) -> VisualAdmissionInputBundle:
        """Load only the sidecar bound to the exact current draft snapshot."""

        selected_id = _draft_id(reconstruction_id)
        expected = _generation(expected_generation)
        if type(expected_proposal_ref) is not ReconstructionPayloadRef:
            raise TypeError("expected_proposal_ref must be an exact ReconstructionPayloadRef")
        catalog = self._acquire_catalog()
        draft_lease = None
        root_fd = -1
        draft_fd = -1
        result = None
        failure: ReconstructionDraftStoreError | None = None
        try:
            try:
                self._recover_pending_locked()
                root_fd = self._root.open()
                _scan_catalog(self._root, root_fd)
                draft_lease = self._acquire_draft(selected_id)
                draft_fd, _draft_info = _open_draft_at(self._root, root_fd, selected_id)
                record, _raw = _load_draft_fd(self._root, draft_fd, selected_id)
                if (
                    record.draft.generation != expected
                    or record.draft.proposal_ref != expected_proposal_ref
                ):
                    _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
                reference = record.admission_ref
                if reference is None:
                    _raise(ReconstructionDraftStoreErrorCode.NOT_FOUND)
                payload_raw, _payload_info = _read_file(
                    self._root,
                    draft_fd,
                    reference.filename,
                    maximum=reference.size_bytes,
                )
                try:
                    result = decode_visual_admission_inputs(payload_raw)
                except VisualAdmissionInputError:
                    _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
                if (
                    _admission_ref_for(
                        result,
                        admitted_generation=reference.admitted_generation,
                        raw=payload_raw,
                    )
                    != reference
                ):
                    _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
                self._root.verify_directory_entry(
                    root_fd,
                    selected_id,
                    expected=os.fstat(draft_fd),
                )
            except ReconstructionDraftStoreError as error:
                failure = error
            except (OSError, StorageFailure):
                failure = ReconstructionDraftStoreError(ReconstructionDraftStoreErrorCode.IO_ERROR)
        finally:
            draft_close_ok = draft_fd < 0 or _close(draft_fd)
            root_close_ok = root_fd < 0 or _close(root_fd)
            draft_release_ok = draft_lease is None or _release(draft_lease)
            catalog_release_ok = _release(catalog)
        if failure is not None:
            raise failure
        if (
            result is None
            or not draft_close_ok
            or not root_close_ok
            or not draft_release_ok
            or not catalog_release_ok
        ):
            _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
        return result

    def _attach_admission_exact(
        self,
        reconstruction_id: object,
        *,
        expected_generation: object,
        expected_proposal_ref: object,
        bundle: object,
    ) -> VisualAdmissionInputBundle:
        """Attach one immutable input bundle without advancing draft generation."""

        selected_id = _draft_id(reconstruction_id)
        expected = _generation(expected_generation)
        if type(expected_proposal_ref) is not ReconstructionPayloadRef:
            raise TypeError("expected_proposal_ref must be an exact ReconstructionPayloadRef")
        if type(bundle) is not VisualAdmissionInputBundle:
            raise TypeError("bundle must be an exact VisualAdmissionInputBundle")
        try:
            bundle_raw = encode_visual_admission_inputs(bundle)
            if decode_visual_admission_inputs(bundle_raw) != bundle:
                _raise(ReconstructionDraftStoreErrorCode.CORRUPT_RECORD)
        except VisualAdmissionInputError:
            _raise(ReconstructionDraftStoreErrorCode.CONFLICT)

        def prepare(
            previous: _StoredDraftRecord,
        ) -> tuple[_StoredDraftRecord, Mapping[str, bytes]]:
            draft = previous.draft
            if (
                draft.status is not ReconstructionStatus.PROPOSED
                or draft.base_head is None
                or draft.observation_ref is None
                or draft.proposal_ref != expected_proposal_ref
                or bundle.reconstruction_id != draft.reconstruction_id
                or bundle.base_head_sha256 != draft.base_head.sha256
                or bundle.observation_ref != draft.observation_ref
                or bundle.proposal_ref != draft.proposal_ref
                or bundle.image_set_ref.image_set_id != draft.image_set_id
                or bundle.image_set_ref.manifest_sha256 != draft.image_set_manifest_sha256
            ):
                _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
            reference = _admission_ref_for(
                bundle,
                admitted_generation=expected,
                raw=bundle_raw,
            )
            if previous.admission_ref is not None:
                if previous.admission_ref != reference:
                    _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
                return previous, {}
            return (
                _StoredDraftRecord(draft=draft, admission_ref=reference),
                {reference.filename: bundle_raw},
            )

        self._compare_and_set_record(selected_id, expected, prepare)
        return bundle

    def _compare_and_set_record(
        self,
        reconstruction_id: str,
        expected_generation: int,
        prepare: Callable[
            [_StoredDraftRecord],
            tuple[_StoredDraftRecord, Mapping[str, bytes]],
        ],
    ) -> _StoredDraftRecord:
        """Run the one private payload-first, record-last stored-record CAS."""

        catalog = self._acquire_catalog()
        draft_lease = None
        root_fd = -1
        draft_fd = -1
        publication_fd = -1
        journal_created = False
        replaced = False
        previous: _StoredDraftRecord | None = None
        old_raw: bytes | None = None
        successor: _StoredDraftRecord | None = None
        reserved: _MutationJournal | None = None
        journal_identity: tuple[int, int] | None = None
        created_identities: dict[str, tuple[int, int]] = {}
        result: _StoredDraftRecord | None = None
        failure: ReconstructionDraftStoreError | None = None
        try:
            try:
                self._recover_pending_locked()
                root_fd = self._root.open()
                snapshot = _scan_catalog(self._root, root_fd)
                draft_lease = self._acquire_draft(reconstruction_id)
                draft_fd, _draft_info = _open_draft_at(
                    self._root,
                    root_fd,
                    reconstruction_id,
                )
                previous, old_raw = _load_draft_fd(
                    self._root,
                    draft_fd,
                    reconstruction_id,
                )
                if previous.draft.generation != expected_generation:
                    _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
                successor, supplied = prepare(previous)
                if type(successor) is not _StoredDraftRecord or type(supplied) is not dict:
                    raise TypeError("stored record prepare result must be exact")
                if successor.draft.reconstruction_id != reconstruction_id:
                    _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
                if successor == previous:
                    if supplied:
                        _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
                    result = previous
                else:
                    new_raw = _encode_record(successor)
                    old_refs = _refs_by_filename(previous)
                    new_refs = _refs_by_filename(successor)
                    shared = set(old_refs) & set(new_refs)
                    if any(old_refs[name] != new_refs[name] for name in shared):
                        _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
                    additions = tuple(sorted(set(new_refs) - set(old_refs)))
                    removals = tuple(sorted(set(old_refs) - set(new_refs)))
                    if set(supplied) != set(additions) or any(
                        type(supplied[name]) is not bytes
                        or len(supplied[name]) != new_refs[name].size_bytes
                        or hashlib.sha256(supplied[name]).hexdigest() != new_refs[name].sha256
                        for name in additions
                    ):
                        _raise(ReconstructionDraftStoreErrorCode.CONFLICT)

                    token = secrets.token_hex(16)
                    plans = tuple(
                        _PayloadPlan(
                            name=name,
                            temp_name=f".{name}.{token}.tmp",
                            sha256=new_refs[name].sha256,
                            size=new_refs[name].size_bytes,
                        )
                        for name in additions
                    )
                    removed_evidence = tuple(
                        _validate_payload_file(self._root, draft_fd, old_refs[name])
                        for name in removals
                    )
                    record_temp_name = f".record.{token}.tmp"
                    reserved = _MutationJournal(
                        state="RESERVED",
                        reconstruction_id=reconstruction_id,
                        old_sha256=hashlib.sha256(old_raw).hexdigest(),
                        new_sha256=hashlib.sha256(new_raw).hexdigest(),
                        new_size=len(new_raw),
                        record_temp_name=record_temp_name,
                        add_payloads=plans,
                        remove_payloads=removed_evidence,
                    )
                    peak = (
                        snapshot.total_bytes
                        + len(new_raw)
                        + sum(len(raw) for raw in supplied.values())
                        + _MAX_JOURNAL_BYTES
                    )
                    if peak > MAX_RECONSTRUCTION_DRAFT_STORE_BYTES:
                        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)

                    journal_identity = _create_journal(self._root, draft_fd, reserved)
                    journal_created = True
                    for plan in plans:
                        created_identity = _write_exclusive(
                            self._root,
                            draft_fd,
                            plan.temp_name,
                            supplied[plan.name],
                            maximum=plan.size,
                        )
                        created_identities[plan.temp_name] = created_identity
                        created_identities[plan.name] = created_identity
                    created_identities[record_temp_name] = _write_exclusive(
                        self._root,
                        draft_fd,
                        record_temp_name,
                        new_raw,
                        maximum=_MAX_STORED_RECORD_BYTES,
                    )
                    added_evidence = tuple(
                        _publish_payload_plan(
                            self._root,
                            draft_fd,
                            plan,
                            new_refs[plan.name],
                        )
                        for plan in plans
                    )
                    record_evidence = _capture_expected_file(
                        self._root,
                        draft_fd,
                        record_temp_name,
                        reserved.new_sha256,
                        reserved.new_size,
                        maximum=_MAX_STORED_RECORD_BYTES,
                    )
                    staged = _MutationJournal(
                        state="STAGED",
                        reconstruction_id=reserved.reconstruction_id,
                        old_sha256=reserved.old_sha256,
                        new_sha256=reserved.new_sha256,
                        new_size=reserved.new_size,
                        record_temp_name=reserved.record_temp_name,
                        add_payloads=reserved.add_payloads,
                        remove_payloads=reserved.remove_payloads,
                        record_temp=record_evidence,
                        added_payloads=added_evidence,
                    )
                    _append_staged_journal(
                        self._root,
                        draft_fd,
                        journal_identity,
                        reserved,
                        staged,
                        valid_length=len(reserved.to_line()),
                    )
                    current, current_raw = _load_draft_fd(
                        self._root,
                        draft_fd,
                        reconstruction_id,
                    )
                    if current != previous or current_raw != old_raw:
                        _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
                    publication_fd, _publication_identity = _open_verified(
                        self._root,
                        draft_fd,
                        record_evidence,
                        maximum=_MAX_STORED_RECORD_BYTES,
                    )
                    os.replace(
                        record_temp_name,
                        _RECORD_NAME,
                        src_dir_fd=draft_fd,
                        dst_dir_fd=draft_fd,
                    )
                    replaced = True
                    _verify_replaced(
                        self._root,
                        draft_fd,
                        _RECORD_NAME,
                        publication_fd,
                        record_evidence,
                        maximum=_MAX_STORED_RECORD_BYTES,
                    )
                    os.fsync(draft_fd)
                    result, readback_raw = _load_draft_fd(
                        self._root,
                        draft_fd,
                        reconstruction_id,
                    )
                    if result != successor or readback_raw != new_raw:
                        _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
                    self._finish_committed_cleanup(draft_fd, staged, successor)
                    _unlink_exact(
                        self._root,
                        draft_fd,
                        _JOURNAL_NAME,
                        journal_identity,
                        maximum=_MAX_JOURNAL_BYTES,
                    )
                    journal_created = False
                self._root.verify_directory_entry(
                    root_fd,
                    reconstruction_id,
                    expected=os.fstat(draft_fd),
                )
            except ReconstructionDraftStoreError as error:
                failure = error
            except (OSError, StorageFailure):
                failure = ReconstructionDraftStoreError(ReconstructionDraftStoreErrorCode.IO_ERROR)
            if failure is not None and draft_fd >= 0 and not journal_created:
                journal_created = _stat_at(draft_fd, _JOURNAL_NAME) is not None
            if (
                failure is not None
                and not replaced
                and journal_created
                and draft_fd >= 0
                and previous is not None
                and old_raw is not None
                and reserved is not None
                and journal_identity is not None
                and self._rollback_failed_cas_locked(
                    draft_fd,
                    previous,
                    old_raw,
                    reserved,
                    journal_identity,
                    created_identities,
                )
            ):
                journal_created = False
        finally:
            publication_close_ok = publication_fd < 0 or _close(publication_fd)
            draft_close_ok = draft_fd < 0 or _close(draft_fd)
            root_close_ok = root_fd < 0 or _close(root_fd)
            draft_release_ok = draft_lease is None or _release(draft_lease)
            catalog_release_ok = _release(catalog)
        committed_generation = (
            expected_generation if successor is None else successor.draft.generation
        )
        if failure is not None:
            if replaced or journal_created:
                _raise(
                    ReconstructionDraftStoreErrorCode.DURABILITY_UNCERTAIN,
                    committed_generation=committed_generation,
                )
            raise failure
        if (
            result is None
            or not publication_close_ok
            or not draft_close_ok
            or not root_close_ok
            or not draft_release_ok
            or not catalog_release_ok
        ):
            if replaced:
                _raise(
                    ReconstructionDraftStoreErrorCode.DURABILITY_UNCERTAIN,
                    committed_generation=committed_generation,
                )
            _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
        return result

    def _recover_pending_locked(self) -> None:
        root_fd = -1
        failure: ReconstructionDraftStoreError | None = None
        snapshot = None
        try:
            try:
                root_fd = self._root.open()
                snapshot = _scan_catalog(self._root, root_fd)
                for stage_name, stage_identity in snapshot.stages:
                    _remove_stage(self._root, root_fd, stage_name, stage_identity)
                if snapshot.stages:
                    snapshot = _scan_catalog(self._root, root_fd)
            except ReconstructionDraftStoreError as error:
                failure = error
            except (OSError, StorageFailure):
                failure = ReconstructionDraftStoreError(ReconstructionDraftStoreErrorCode.IO_ERROR)
        finally:
            close_ok = root_fd < 0 or _close(root_fd)
        if failure is not None:
            raise failure
        if not close_ok or snapshot is None:
            _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
        if snapshot.journal_draft_id is None:
            return
        lease = self._acquire_draft(snapshot.journal_draft_id)
        try:
            self._recover_draft_locked(snapshot.journal_draft_id)
        finally:
            release_ok = _release(lease)
        if not release_ok:
            _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)

    def _recover_draft_locked(self, reconstruction_id: str) -> None:
        root_fd = -1
        draft_fd = -1
        publication_fd = -1
        try:
            root_fd = self._root.open()
            draft_fd, _draft_info = _open_draft_at(self._root, root_fd, reconstruction_id)
            reserved, staged, valid_length, partial, journal_info = _read_journal(
                self._root,
                draft_fd,
            )
            journal_identity = _identity(journal_info)
            current, current_raw = _load_draft_fd(self._root, draft_fd, reconstruction_id)
            current_sha256 = hashlib.sha256(current_raw).hexdigest()
            selected = staged if staged is not None else reserved
            record_temp_info = _stat_at(draft_fd, selected.record_temp_name)

            if staged is None and record_temp_info is None:
                if partial:
                    _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
                if current_sha256 == reserved.old_sha256:
                    _validate_old_record_rollback(reserved, current)
                    self._rollback_reserved_payloads(draft_fd, reserved)
                    _unlink_exact(
                        self._root,
                        draft_fd,
                        _JOURNAL_NAME,
                        journal_identity,
                        maximum=_MAX_JOURNAL_BYTES,
                    )
                    return
                if current_sha256 != reserved.new_sha256:
                    _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
                _validate_committed_journal(reserved, current)
                self._finish_committed_cleanup(draft_fd, reserved, current)
                _unlink_exact(
                    self._root,
                    draft_fd,
                    _JOURNAL_NAME,
                    journal_identity,
                    maximum=_MAX_JOURNAL_BYTES,
                )
                return

            if staged is None:
                new_raw, _new_info = _read_file(
                    self._root,
                    draft_fd,
                    reserved.record_temp_name,
                    maximum=_MAX_STORED_RECORD_BYTES,
                )
                if (
                    len(new_raw) != reserved.new_size
                    or hashlib.sha256(new_raw).hexdigest() != reserved.new_sha256
                ):
                    _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
                successor = _decode_record(new_raw, reconstruction_id)
                _validate_journal_bindings(reserved, current, successor)
                if current_sha256 != reserved.old_sha256:
                    _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
                new_refs = _refs_by_filename(successor)
                added = tuple(
                    _publish_payload_plan(
                        self._root,
                        draft_fd,
                        plan,
                        new_refs[plan.name],
                    )
                    for plan in reserved.add_payloads
                )
                record_evidence = _capture_expected_file(
                    self._root,
                    draft_fd,
                    reserved.record_temp_name,
                    reserved.new_sha256,
                    reserved.new_size,
                    maximum=_MAX_STORED_RECORD_BYTES,
                )
                staged = _MutationJournal(
                    state="STAGED",
                    reconstruction_id=reserved.reconstruction_id,
                    old_sha256=reserved.old_sha256,
                    new_sha256=reserved.new_sha256,
                    new_size=reserved.new_size,
                    record_temp_name=reserved.record_temp_name,
                    add_payloads=reserved.add_payloads,
                    remove_payloads=reserved.remove_payloads,
                    record_temp=record_evidence,
                    added_payloads=added,
                )
                _append_staged_journal(
                    self._root,
                    draft_fd,
                    journal_identity,
                    reserved,
                    staged,
                    valid_length=valid_length,
                    partial=partial,
                )
                selected = staged

            assert staged is not None and selected.record_temp is not None
            record_temp_present = _stat_at(draft_fd, selected.record_temp_name) is not None
            if not record_temp_present:
                if current_sha256 == selected.old_sha256:
                    _validate_old_record_rollback(selected, current)
                    self._rollback_staged_payloads(draft_fd, selected)
                    _unlink_exact(
                        self._root,
                        draft_fd,
                        _JOURNAL_NAME,
                        journal_identity,
                        maximum=_MAX_JOURNAL_BYTES,
                    )
                    return
                if current_sha256 != selected.new_sha256:
                    _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
                successor = current
                _validate_committed_journal(selected, successor)
                self._finish_committed_cleanup(draft_fd, selected, successor)
                _unlink_exact(
                    self._root,
                    draft_fd,
                    _JOURNAL_NAME,
                    journal_identity,
                    maximum=_MAX_JOURNAL_BYTES,
                )
                return

            if current_sha256 != selected.old_sha256:
                _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
            new_raw, _new_info = _read_file(
                self._root,
                draft_fd,
                selected.record_temp_name,
                maximum=_MAX_STORED_RECORD_BYTES,
            )
            successor = _decode_record(new_raw, reconstruction_id)
            _validate_journal_bindings(selected, current, successor)
            for evidence in selected.added_payloads:
                if not _evidence_matches(
                    self._root,
                    draft_fd,
                    evidence,
                    maximum=_MAX_PAYLOAD_BYTES,
                ):
                    _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
            for evidence in selected.remove_payloads:
                if not _evidence_matches(
                    self._root,
                    draft_fd,
                    evidence,
                    maximum=_MAX_PAYLOAD_BYTES,
                ):
                    _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
            publication_fd, _publication_identity = _open_verified(
                self._root,
                draft_fd,
                selected.record_temp,
                maximum=_MAX_STORED_RECORD_BYTES,
            )
            os.replace(
                selected.record_temp_name,
                _RECORD_NAME,
                src_dir_fd=draft_fd,
                dst_dir_fd=draft_fd,
            )
            _verify_replaced(
                self._root,
                draft_fd,
                _RECORD_NAME,
                publication_fd,
                selected.record_temp,
                maximum=_MAX_STORED_RECORD_BYTES,
            )
            os.fsync(draft_fd)
            readback, readback_raw = _load_draft_fd(self._root, draft_fd, reconstruction_id)
            if (
                readback != successor
                or hashlib.sha256(readback_raw).hexdigest() != selected.new_sha256
            ):
                _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
            self._finish_committed_cleanup(draft_fd, selected, successor)
            _unlink_exact(
                self._root,
                draft_fd,
                _JOURNAL_NAME,
                journal_identity,
                maximum=_MAX_JOURNAL_BYTES,
            )
        except ReconstructionDraftStoreError:
            raise
        except (OSError, StorageFailure):
            _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
        finally:
            publication_close_ok = publication_fd < 0 or _close(publication_fd)
            draft_close_ok = draft_fd < 0 or _close(draft_fd)
            root_close_ok = root_fd < 0 or _close(root_fd)
            if (
                not (publication_close_ok and draft_close_ok and root_close_ok)
                and sys.exception() is None
            ):
                _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)

    def _rollback_reserved_payloads(
        self,
        draft_fd: int,
        journal: _MutationJournal,
    ) -> None:
        for plan in journal.add_payloads:
            for name in (plan.temp_name, plan.name):
                current = _stat_at(draft_fd, name)
                if current is None:
                    continue
                evidence = _capture_expected_file(
                    self._root,
                    draft_fd,
                    name,
                    plan.sha256,
                    plan.size,
                    maximum=plan.size,
                )
                _remove_if_exact(
                    self._root,
                    draft_fd,
                    evidence,
                    maximum=plan.size,
                    missing_ok=False,
                )

    def _rollback_staged_payloads(
        self,
        draft_fd: int,
        journal: _MutationJournal,
    ) -> None:
        for evidence in journal.added_payloads:
            _remove_if_exact(
                self._root,
                draft_fd,
                evidence,
                maximum=evidence.size,
                missing_ok=False,
            )

    def _rollback_failed_cas_locked(
        self,
        draft_fd: int,
        previous: _StoredDraftRecord,
        old_raw: bytes,
        journal: _MutationJournal,
        journal_identity: tuple[int, int],
        created_identities: Mapping[str, tuple[int, int]],
    ) -> bool:
        """Remove only this attempt's remnants while the old record remains authoritative."""

        try:
            current_raw, _record_info = _read_file(
                self._root,
                draft_fd,
                _RECORD_NAME,
                maximum=_MAX_STORED_RECORD_BYTES,
            )
            if (
                current_raw != old_raw
                or _decode_record(current_raw, previous.draft.reconstruction_id) != previous
            ):
                return False
            _validate_old_record_rollback(journal, previous)
            for reference in _refs_by_filename(previous).values():
                _validate_payload_file(self._root, draft_fd, reference)

            removable_names = [
                *(name for plan in journal.add_payloads for name in (plan.temp_name, plan.name)),
                journal.record_temp_name,
            ]
            for name in removable_names:
                current = _stat_at(draft_fd, name)
                expected_identity = created_identities.get(name)
                if current is None:
                    continue
                if expected_identity is None or _identity(current) != expected_identity:
                    return False
                maximum = (
                    _MAX_STORED_RECORD_BYTES
                    if name == journal.record_temp_name
                    else _MAX_PAYLOAD_BYTES
                )
                _unlink_exact(
                    self._root,
                    draft_fd,
                    name,
                    expected_identity,
                    maximum=maximum,
                )
            current_journal = _stat_at(draft_fd, _JOURNAL_NAME)
            if current_journal is None or _identity(current_journal) != journal_identity:
                return False
            _unlink_exact(
                self._root,
                draft_fd,
                _JOURNAL_NAME,
                journal_identity,
                maximum=_MAX_JOURNAL_BYTES,
            )
            readback, readback_raw = _load_draft_fd(
                self._root,
                draft_fd,
                previous.draft.reconstruction_id,
            )
            return readback == previous and readback_raw == old_raw
        except (OSError, StorageFailure, ReconstructionDraftStoreError):
            return False

    def _finish_committed_cleanup(
        self,
        draft_fd: int,
        journal: _MutationJournal,
        successor: _StoredDraftRecord,
    ) -> None:
        new_refs = _refs_by_filename(successor)
        for plan in journal.add_payloads:
            reference = new_refs.get(plan.name)
            if reference is None:
                _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
            _publish_payload_plan(self._root, draft_fd, plan, reference)
        for evidence in journal.remove_payloads:
            _remove_if_exact(
                self._root,
                draft_fd,
                evidence,
                maximum=evidence.size,
                missing_ok=True,
            )

    def create(
        self,
        draft: object,
        payloads: object = (),
    ) -> ReconstructionDraft:
        if type(draft) is not ReconstructionDraft:
            raise TypeError("draft must be an exact ReconstructionDraft")
        try:
            validate_reconstruction_creation(draft)
        except ReconstructionDraftError as error:
            _translate_draft_error(error, transition=True)
        selected_payloads = _normalize_payloads(payloads)
        if selected_payloads:
            _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
        raw = _encode_record(_StoredDraftRecord(draft=draft))

        catalog = self._acquire_catalog()
        draft_lease = None
        root_fd = -1
        stage_fd = -1
        final_fd = -1
        stage_name = (
            ".stage_"
            + draft.reconstruction_id.removeprefix("reconstruction_")
            + "_"
            + secrets.token_hex(16)
        )
        stage_identity: tuple[int, int] | None = None
        published = False
        result: ReconstructionDraft | None = None
        failure: ReconstructionDraftStoreError | None = None
        try:
            try:
                self._recover_pending_locked()
                root_fd = self._root.open()
                snapshot = _scan_catalog(self._root, root_fd)
                draft_lease = self._acquire_draft(draft.reconstruction_id)
                owners: dict[str, str] = {}
                existing_target: ReconstructionDraft | None = None
                for existing_id in snapshot.draft_ids:
                    existing_fd = -1
                    try:
                        existing_fd, opened = _open_draft_at(
                            self._root,
                            root_fd,
                            existing_id,
                        )
                        existing_record, _existing_raw = _load_draft_fd(
                            self._root,
                            existing_fd,
                            existing_id,
                        )
                        self._root.verify_directory_entry(root_fd, existing_id, expected=opened)
                    except StorageFailure:
                        _raise(ReconstructionDraftStoreErrorCode.UNSAFE_STORE)
                    finally:
                        if existing_fd >= 0 and not _close(existing_fd):
                            _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
                    existing = existing_record.draft
                    owned_image_set = existing.image_set_id
                    if existing.delete_cleanup is not None:
                        owned_image_set = existing.delete_cleanup.image_set_id
                    if owned_image_set is not None:
                        prior_owner = owners.setdefault(owned_image_set, existing_id)
                        if prior_owner != existing_id:
                            _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
                    if existing_id == draft.reconstruction_id:
                        existing_target = existing

                selected_owner = owners.get(draft.image_set_id)
                if selected_owner is not None and selected_owner != draft.reconstruction_id:
                    _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
                if existing_target is not None:
                    if existing_target != draft:
                        _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
                    result = existing_target
                else:
                    if len(snapshot.draft_ids) >= MAX_RECONSTRUCTION_DRAFTS:
                        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
                    if snapshot.total_bytes + len(raw) > MAX_RECONSTRUCTION_DRAFT_STORE_BYTES:
                        _raise(ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED)
                    os.mkdir(stage_name, 0o700, dir_fd=root_fd)
                    stage_entry = _stat_at(root_fd, stage_name)
                    if stage_entry is None:
                        _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
                    stage_identity = _identity(stage_entry)
                    stage_fd, _stage_info = self._root.open_directory_at(
                        root_fd,
                        stage_name,
                        expected_identity=stage_identity,
                    )
                    _write_exclusive(
                        self._root,
                        stage_fd,
                        _RECORD_NAME,
                        raw,
                        maximum=_MAX_STORED_RECORD_BYTES,
                    )
                    os.fsync(stage_fd)
                    _rename_directory_noreplace(
                        root_fd,
                        stage_name,
                        draft.reconstruction_id,
                    )
                    published = True
                    os.fsync(root_fd)
                    if not _close(stage_fd):
                        _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
                    stage_fd = -1
                    final_fd, _opened = _open_draft_at(
                        self._root,
                        root_fd,
                        draft.reconstruction_id,
                    )
                    readback, readback_raw = _load_draft_fd(
                        self._root,
                        final_fd,
                        draft.reconstruction_id,
                    )
                    if readback.draft != draft or readback_raw != raw:
                        _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
                    result = readback.draft
                    self._root.verify_directory_entry(
                        root_fd,
                        draft.reconstruction_id,
                        expected=os.fstat(final_fd),
                    )
            except ReconstructionDraftStoreError as error:
                failure = error
            except (OSError, StorageFailure):
                failure = ReconstructionDraftStoreError(ReconstructionDraftStoreErrorCode.IO_ERROR)
        finally:
            stage_close_ok = stage_fd < 0 or _close(stage_fd)
            final_close_ok = final_fd < 0 or _close(final_fd)
            if (
                not published
                and stage_identity is not None
                and root_fd >= 0
                and _stat_at(root_fd, stage_name) is not None
            ):
                try:
                    _remove_stage(self._root, root_fd, stage_name, stage_identity)
                except ReconstructionDraftStoreError as cleanup_error:
                    if failure is None:
                        failure = cleanup_error
            root_close_ok = root_fd < 0 or _close(root_fd)
            draft_release_ok = draft_lease is None or _release(draft_lease)
            catalog_release_ok = _release(catalog)
        if failure is not None:
            if published:
                _raise(
                    ReconstructionDraftStoreErrorCode.DURABILITY_UNCERTAIN,
                    committed_generation=0,
                )
            raise failure
        if not all(
            (
                stage_close_ok,
                final_close_ok,
                root_close_ok,
                draft_release_ok,
                catalog_release_ok,
            )
        ):
            if published:
                _raise(
                    ReconstructionDraftStoreErrorCode.DURABILITY_UNCERTAIN,
                    committed_generation=0,
                )
            _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
        if result is None:
            _raise(ReconstructionDraftStoreErrorCode.IO_ERROR)
        return result

    def compare_and_set(
        self,
        reconstruction_id: object,
        expected_generation: object,
        successor: object,
        payloads: object = (),
    ) -> ReconstructionDraft:
        selected_id = _draft_id(reconstruction_id)
        expected = _generation(expected_generation)
        if type(successor) is not ReconstructionDraft:
            raise TypeError("successor must be an exact ReconstructionDraft")
        if successor.reconstruction_id != selected_id or expected == MAX_SAFE_JSON_INTEGER:
            _raise(ReconstructionDraftStoreErrorCode.CONFLICT)
        selected_payloads = _normalize_payloads(payloads)
        _encode_public_draft(successor)

        supplied = _payloads_by_filename(successor, selected_payloads)

        def prepare(
            previous: _StoredDraftRecord,
        ) -> tuple[_StoredDraftRecord, Mapping[str, bytes]]:
            try:
                validate_reconstruction_successor(previous.draft, successor)
            except ReconstructionDraftError as error:
                _translate_draft_error(error, transition=True)
            return (
                _StoredDraftRecord(
                    draft=successor,
                    admission_ref=_successor_admission(previous, successor),
                ),
                {name: payload.raw for name, payload in supplied.items()},
            )

        return self._compare_and_set_record(selected_id, expected, prepare).draft
