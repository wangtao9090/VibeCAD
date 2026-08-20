"""Crash-safe immutable storage for advisory visual-review PNG records."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import re
import secrets
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vibecad.interaction.storage import SafeRoot, StorageFailure
from vibecad.interaction.storage import os as _storage_os
from vibecad.visual.review_artifacts import (
    MAX_VISUAL_REVIEW_RECORD_BYTES,
    VisualReviewArtifact,
    VisualReviewArtifactError,
    VisualReviewResource,
    decode_visual_review_artifact,
    encode_visual_review_artifact,
    parse_visual_review_resource_uri,
)
from vibecad.workflow.lease import LeaseError, LeaseErrorCode, ResourceLeaseManager

os = _storage_os

MAX_VISUAL_REVIEW_OBSERVATIONS = 1024
MAX_VISUAL_REVIEW_RECORDS = MAX_VISUAL_REVIEW_OBSERVATIONS * 16
MAX_VISUAL_REVIEW_TEMPORARIES = 16
MAX_VISUAL_REVIEW_STORE_BYTES = 8 * 1024 * 1024 * 1024
MAX_VISUAL_REVIEW_TOMBSTONE_BYTES = 512

_CATALOG_RESOURCE = "visual_review_catalog"
_LEASE_WAIT_SECONDS = 2.0
_LEASE_RETRY_SECONDS = 0.01
_TOMBSTONE_SCHEMA_VERSION = 1
_OBSERVATION_ID = re.compile(r"^visual_observation_([0-9a-f]{32})$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_FINAL = re.compile(r"^review_([0-9a-f]{32})_(0[0-9]|1[0-5])\.bin$")
_STAGE = re.compile(r"^\.stage_([0-9a-f]{32})_(0[0-9]|1[0-5])_([0-9a-f]{32})\.tmp$")
_DELETE = re.compile(r"^\.delete_([0-9a-f]{32})_(0[0-9]|1[0-5])\.bin$")
_TOMBSTONE = re.compile(r"^\.deleted_([0-9a-f]{32})\.json$")
_TOMBSTONE_TEMP = re.compile(r"^\.deleted_([0-9a-f]{32})_([0-9a-f]{32})\.tmp$")


class VisualReviewStoreErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    DELETED = "deleted"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTEGRITY_FAILURE = "integrity_failure"
    STORE_FAILURE = "store_failure"
    LEASE_UNAVAILABLE = "lease_unavailable"
    RECOVERY_REQUIRED = "recovery_required"
    DURABILITY_UNCERTAIN = "durability_uncertain"


class VisualReviewStoreError(RuntimeError):
    """Bounded storage failure without reflected filesystem or image data."""

    def __init__(self, code: VisualReviewStoreErrorCode) -> None:
        if type(code) is not VisualReviewStoreErrorCode:
            raise TypeError("code must be an exact VisualReviewStoreErrorCode")
        self.code = code
        super().__init__(code.value)


def _fail(code: VisualReviewStoreErrorCode) -> None:
    raise VisualReviewStoreError(code)


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualReviewRecoverySummary:
    published_stages: int
    removed_partial_stages: int
    completed_deletions: int
    published_tombstones: int


@dataclass(frozen=True, slots=True)
class _Snapshot:
    finals: tuple[str, ...]
    stages: tuple[str, ...]
    deletes: tuple[str, ...]
    tombstones: tuple[str, ...]
    tombstone_temps: tuple[str, ...]
    committed_bytes: int
    recovery_bytes: int


def _observation_suffix(observation_id: object) -> str:
    if type(observation_id) is not str:
        _fail(VisualReviewStoreErrorCode.INVALID_INPUT)
    matched = _OBSERVATION_ID.fullmatch(observation_id)
    if matched is None:
        _fail(VisualReviewStoreErrorCode.INVALID_INPUT)
    return matched.group(1)


def _observation_id(suffix: str) -> str:
    return "visual_observation_" + suffix


def _source_index(value: object) -> int:
    if type(value) is not int or not 0 <= value < 16:
        _fail(VisualReviewStoreErrorCode.INVALID_INPUT)
    return value


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(VisualReviewStoreErrorCode.INVALID_INPUT)
    return value


def _final_name(observation_id: str, source_index: int) -> str:
    return f"review_{_observation_suffix(observation_id)}_{_source_index(source_index):02d}.bin"


def _delete_name(observation_id: str, source_index: int) -> str:
    return f".delete_{_observation_suffix(observation_id)}_{_source_index(source_index):02d}.bin"


def _tombstone_name(observation_id: str) -> str:
    return f".deleted_{_observation_suffix(observation_id)}.json"


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def _rename_noreplace(root_fd: int, source: str, destination: str) -> None:
    if sys.platform == "win32":
        os.rename(
            source,
            destination,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
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
                root_fd,
                source.encode("ascii"),
                root_fd,
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
                root_fd,
                source.encode("ascii"),
                root_fd,
                destination.encode("ascii"),
                1,
            )
        else:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename unavailable")
        operation.restype = ctypes.c_int
        ctypes.set_errno(0)
        if operation(*arguments) != 0:
            code = ctypes.get_errno() or errno.EIO
            if code in {errno.EEXIST, errno.ENOTEMPTY}:
                raise FileExistsError(code, "destination exists")
            raise OSError(code, "atomic no-replace rename failed")
    except (AttributeError, UnicodeError):
        raise OSError(errno.ENOTSUP, "atomic no-replace rename unavailable") from None


def _tombstone_raw(observation_id: str, observation_digest: str) -> bytes:
    body = {
        "observation_digest": _digest(observation_digest),
        "observation_id": _observation_id(_observation_suffix(observation_id)),
        "schema_version": _TOMBSTONE_SCHEMA_VERSION,
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("ascii")
    if len(raw) > MAX_VISUAL_REVIEW_TOMBSTONE_BYTES:
        _fail(VisualReviewStoreErrorCode.BUDGET_EXCEEDED)
    return raw


def _decode_tombstone(raw: bytes) -> tuple[str, str]:
    try:
        value = json.loads(raw.decode("ascii"))
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
        _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
    if type(value) is not dict or set(value) != {
        "observation_digest",
        "observation_id",
        "schema_version",
    }:
        _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
    try:
        observation_id = _observation_id(_observation_suffix(value["observation_id"]))
        observation_digest = _digest(value["observation_digest"])
    except VisualReviewStoreError:
        _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
    if _tombstone_raw(observation_id, observation_digest) != raw:
        _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
    return observation_id, observation_digest


class VisualReviewArtifactStore:
    """Append-only review records with observation-level permanent deletion."""

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
            raise TypeError("invalid visual-review store composition")
        try:
            selected = SafeRoot(root)
        except StorageFailure:
            _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
        if selected.identity != expected_root_identity:
            _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
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
                    _fail(VisualReviewStoreErrorCode.LEASE_UNAVAILABLE)
                _fail(VisualReviewStoreErrorCode.STORE_FAILURE)

    def _snapshot(self, root_fd: int, *, recovery: bool) -> _Snapshot:
        maximum_entries = (
            MAX_VISUAL_REVIEW_RECORDS
            + MAX_VISUAL_REVIEW_OBSERVATIONS
            + MAX_VISUAL_REVIEW_TEMPORARIES * 3
        )
        names: list[str] = []
        try:
            for name in os.listdir(root_fd):
                names.append(name)
                if len(names) > maximum_entries:
                    _fail(VisualReviewStoreErrorCode.BUDGET_EXCEEDED)
        except VisualReviewStoreError:
            raise
        except OSError:
            _fail(VisualReviewStoreErrorCode.STORE_FAILURE)

        buckets: dict[str, list[str]] = {
            "finals": [],
            "stages": [],
            "deletes": [],
            "tombstones": [],
            "tombstone_temps": [],
        }
        committed_bytes = 0
        recovery_bytes = 0
        for name in names:
            if _FINAL.fullmatch(name) is not None:
                bucket = "finals"
                maximum = MAX_VISUAL_REVIEW_RECORD_BYTES
            elif _STAGE.fullmatch(name) is not None:
                bucket = "stages"
                maximum = MAX_VISUAL_REVIEW_RECORD_BYTES
            elif _DELETE.fullmatch(name) is not None:
                bucket = "deletes"
                maximum = MAX_VISUAL_REVIEW_RECORD_BYTES
            elif _TOMBSTONE.fullmatch(name) is not None:
                bucket = "tombstones"
                maximum = MAX_VISUAL_REVIEW_TOMBSTONE_BYTES
            elif _TOMBSTONE_TEMP.fullmatch(name) is not None:
                bucket = "tombstone_temps"
                maximum = MAX_VISUAL_REVIEW_TOMBSTONE_BYTES
            else:
                _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
            try:
                info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except OSError:
                _fail(VisualReviewStoreErrorCode.STORE_FAILURE)
            if not self._root.regular_file(info, maximum=maximum):
                _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
            buckets[bucket].append(name)
            recovery_bytes += info.st_size
            if bucket in {"finals", "tombstones"}:
                committed_bytes += info.st_size

        if (
            len(buckets["finals"]) > MAX_VISUAL_REVIEW_RECORDS
            or len(buckets["tombstones"]) > MAX_VISUAL_REVIEW_OBSERVATIONS
            or len(buckets["stages"]) > MAX_VISUAL_REVIEW_TEMPORARIES
            or len(buckets["deletes"]) > MAX_VISUAL_REVIEW_TEMPORARIES
            or len(buckets["tombstone_temps"]) > MAX_VISUAL_REVIEW_TEMPORARIES
        ):
            _fail(VisualReviewStoreErrorCode.BUDGET_EXCEEDED)
        recovery_ceiling = (
            MAX_VISUAL_REVIEW_STORE_BYTES
            + (MAX_VISUAL_REVIEW_TEMPORARIES * 2 * MAX_VISUAL_REVIEW_RECORD_BYTES)
            + (MAX_VISUAL_REVIEW_TEMPORARIES * MAX_VISUAL_REVIEW_TOMBSTONE_BYTES)
        )
        if recovery_bytes > recovery_ceiling or (
            not recovery and committed_bytes > MAX_VISUAL_REVIEW_STORE_BYTES
        ):
            _fail(VisualReviewStoreErrorCode.BUDGET_EXCEEDED)
        return _Snapshot(
            finals=tuple(sorted(buckets["finals"])),
            stages=tuple(sorted(buckets["stages"])),
            deletes=tuple(sorted(buckets["deletes"])),
            tombstones=tuple(sorted(buckets["tombstones"])),
            tombstone_temps=tuple(sorted(buckets["tombstone_temps"])),
            committed_bytes=committed_bytes,
            recovery_bytes=recovery_bytes,
        )

    @staticmethod
    def _entry_exists(root_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            _fail(VisualReviewStoreErrorCode.STORE_FAILURE)
        return True

    def _read_record(
        self,
        root_fd: int,
        name: str,
    ) -> tuple[VisualReviewArtifact, bytes, os.stat_result]:
        raw, info = self._read_raw(
            root_fd,
            name,
            maximum=MAX_VISUAL_REVIEW_RECORD_BYTES,
        )
        try:
            return decode_visual_review_artifact(raw), raw, info
        except VisualReviewArtifactError:
            _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)

    def _read_raw(
        self,
        root_fd: int,
        name: str,
        *,
        maximum: int,
    ) -> tuple[bytes, os.stat_result]:
        try:
            return self._root.read_file_at(root_fd, name, maximum=maximum)
        except StorageFailure:
            _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)

    def _read_tombstone(
        self,
        root_fd: int,
        name: str,
    ) -> tuple[str, str, bytes]:
        raw, _ = self._read_raw(
            root_fd,
            name,
            maximum=MAX_VISUAL_REVIEW_TOMBSTONE_BYTES,
        )
        observation_id, observation_digest = _decode_tombstone(raw)
        return observation_id, observation_digest, raw

    def _write_temp(self, root_fd: int, name: str, raw: bytes, maximum: int) -> None:
        if not raw or len(raw) > maximum:
            _fail(VisualReviewStoreErrorCode.BUDGET_EXCEEDED)
        fd = -1
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=root_fd,
            )
            _write_all(fd, raw)
            os.fsync(fd)
            info = os.fstat(fd)
            if not self._root.regular_file(info, maximum=maximum) or info.st_size != len(raw):
                _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
            os.close(fd)
            fd = -1
            self._root.verify_file_entry(
                root_fd,
                name,
                expected=info,
                maximum=maximum,
            )
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)

    @staticmethod
    def _unlink(root_fd: int, name: str) -> None:
        try:
            os.unlink(name, dir_fd=root_fd)
        except FileNotFoundError:
            return
        except OSError:
            _fail(VisualReviewStoreErrorCode.DURABILITY_UNCERTAIN)

    def _move_then_remove(
        self,
        root_fd: int,
        final_name: str,
        artifact: VisualReviewArtifact,
        raw: bytes,
        info: os.stat_result,
    ) -> None:
        delete_name = _delete_name(artifact.observation_id, artifact.source_index)
        try:
            _rename_noreplace(root_fd, final_name, delete_name)
        except FileExistsError:
            _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
        moved, moved_raw, moved_info = self._read_record(root_fd, delete_name)
        if (
            (moved_info.st_dev, moved_info.st_ino) != (info.st_dev, info.st_ino)
            or moved_raw != raw
            or moved != artifact
        ):
            _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
        self._unlink(root_fd, delete_name)

    def _tombstones(self, root_fd: int, snapshot: _Snapshot) -> dict[str, str]:
        result: dict[str, str] = {}
        for name in snapshot.tombstones:
            observation_id, observation_digest, _ = self._read_tombstone(root_fd, name)
            matched = _TOMBSTONE.fullmatch(name)
            if matched is None or matched.group(1) != _observation_suffix(observation_id):
                _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
            if observation_id in result:
                _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
            result[observation_id] = observation_digest
        return result

    def _authenticate_observation_records(
        self,
        root_fd: int,
        snapshot: _Snapshot,
        observation_id: str,
        observation_digest: str,
    ) -> None:
        suffix = _observation_suffix(observation_id)
        for name in snapshot.finals:
            matched = _FINAL.fullmatch(name)
            assert matched is not None
            if matched.group(1) != suffix:
                continue
            artifact, _, _ = self._read_record(root_fd, name)
            if (
                _final_name(artifact.observation_id, artifact.source_index) != name
                or artifact.observation_digest != observation_digest
            ):
                _fail(VisualReviewStoreErrorCode.CONFLICT)
        for name in snapshot.stages:
            matched = _STAGE.fullmatch(name)
            assert matched is not None
            if matched.group(1) != suffix:
                continue
            artifact, _, _ = self._read_record(root_fd, name)
            if (
                _observation_suffix(artifact.observation_id) != matched.group(1)
                or artifact.source_index != int(matched.group(2))
                or artifact.observation_digest != observation_digest
            ):
                _fail(VisualReviewStoreErrorCode.CONFLICT)

    def _recover_locked(self) -> VisualReviewRecoverySummary:
        root_fd = -1
        published_stages = 0
        removed_partial_stages = 0
        completed_deletions = 0
        published_tombstones = 0
        try:
            try:
                root_fd = self._root.open()
            except StorageFailure:
                _fail(VisualReviewStoreErrorCode.STORE_FAILURE)
            snapshot = self._snapshot(root_fd, recovery=True)
            for name in snapshot.tombstone_temps:
                matched = _TOMBSTONE_TEMP.fullmatch(name)
                assert matched is not None
                raw, _ = self._read_raw(
                    root_fd,
                    name,
                    maximum=MAX_VISUAL_REVIEW_TOMBSTONE_BYTES,
                )
                try:
                    observation_id, observation_digest = _decode_tombstone(raw)
                except VisualReviewStoreError as error:
                    if error.code is not VisualReviewStoreErrorCode.INTEGRITY_FAILURE:
                        raise
                    self._unlink(root_fd, name)
                    continue
                if matched.group(1) != _observation_suffix(observation_id):
                    _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
                final = _tombstone_name(observation_id)
                if self._entry_exists(root_fd, final):
                    _, current_digest, current_raw = self._read_tombstone(root_fd, final)
                    if current_digest != observation_digest or current_raw != raw:
                        _fail(VisualReviewStoreErrorCode.CONFLICT)
                    self._unlink(root_fd, name)
                else:
                    self._authenticate_observation_records(
                        root_fd,
                        snapshot,
                        observation_id,
                        observation_digest,
                    )
                    _rename_noreplace(root_fd, name, final)
                    published_tombstones += 1
            os.fsync(root_fd)

            snapshot = self._snapshot(root_fd, recovery=True)
            tombstones = self._tombstones(root_fd, snapshot)
            for name in snapshot.deletes:
                artifact, _, _ = self._read_record(root_fd, name)
                if tombstones.get(artifact.observation_id) != artifact.observation_digest:
                    _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
                self._unlink(root_fd, name)
                completed_deletions += 1

            for name in snapshot.finals:
                artifact, raw, info = self._read_record(root_fd, name)
                expected = _final_name(artifact.observation_id, artifact.source_index)
                if expected != name:
                    _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
                tombstone_digest = tombstones.get(artifact.observation_id)
                if tombstone_digest is not None:
                    if tombstone_digest != artifact.observation_digest:
                        _fail(VisualReviewStoreErrorCode.CONFLICT)
                    self._move_then_remove(root_fd, name, artifact, raw, info)
                    completed_deletions += 1

            stage_snapshot = self._snapshot(root_fd, recovery=True)
            current_final_count = len(stage_snapshot.finals)
            current_committed_bytes = stage_snapshot.committed_bytes
            observation_suffixes = {
                matched.group(1)
                for name in (*stage_snapshot.finals, *stage_snapshot.tombstones)
                if (matched := (_FINAL.fullmatch(name) or _TOMBSTONE.fullmatch(name))) is not None
            }
            for name in stage_snapshot.stages:
                matched = _STAGE.fullmatch(name)
                assert matched is not None
                try:
                    raw, _ = self._read_raw(
                        root_fd,
                        name,
                        maximum=MAX_VISUAL_REVIEW_RECORD_BYTES,
                    )
                    artifact = decode_visual_review_artifact(raw)
                except VisualReviewArtifactError:
                    self._unlink(root_fd, name)
                    removed_partial_stages += 1
                    continue
                if (
                    matched.group(1) != _observation_suffix(artifact.observation_id)
                    or int(matched.group(2)) != artifact.source_index
                ):
                    _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
                tombstone_digest = tombstones.get(artifact.observation_id)
                if tombstone_digest is not None:
                    if tombstone_digest != artifact.observation_digest:
                        _fail(VisualReviewStoreErrorCode.CONFLICT)
                    self._unlink(root_fd, name)
                    removed_partial_stages += 1
                    continue
                final = _final_name(artifact.observation_id, artifact.source_index)
                if self._entry_exists(root_fd, final):
                    current, current_raw, _ = self._read_record(root_fd, final)
                    if current != artifact or current_raw != raw:
                        _fail(VisualReviewStoreErrorCode.CONFLICT)
                    self._unlink(root_fd, name)
                else:
                    suffix = _observation_suffix(artifact.observation_id)
                    if (
                        current_final_count >= MAX_VISUAL_REVIEW_RECORDS
                        or (
                            suffix not in observation_suffixes
                            and len(observation_suffixes) >= MAX_VISUAL_REVIEW_OBSERVATIONS
                        )
                        or current_committed_bytes + len(raw) > MAX_VISUAL_REVIEW_STORE_BYTES
                    ):
                        _fail(VisualReviewStoreErrorCode.BUDGET_EXCEEDED)
                    _rename_noreplace(root_fd, name, final)
                    published_stages += 1
                    current_final_count += 1
                    current_committed_bytes += len(raw)
                    observation_suffixes.add(suffix)
            os.fsync(root_fd)
            self._snapshot(root_fd, recovery=False)
            return VisualReviewRecoverySummary(
                published_stages=published_stages,
                removed_partial_stages=removed_partial_stages,
                completed_deletions=completed_deletions,
                published_tombstones=published_tombstones,
            )
        except VisualReviewStoreError:
            raise
        except (OSError, StorageFailure):
            _fail(VisualReviewStoreErrorCode.DURABILITY_UNCERTAIN)
        finally:
            if root_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(root_fd)

    def recover_pending(self) -> VisualReviewRecoverySummary:
        lease = self._acquire_catalog()
        primary: BaseException | None = None
        try:
            return self._recover_locked()
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                lease.release(owner_token=lease.owner_token)
            except BaseException:
                if primary is None:
                    _fail(VisualReviewStoreErrorCode.RECOVERY_REQUIRED)

    def publish(self, artifact: object) -> VisualReviewArtifact:
        if type(artifact) is not VisualReviewArtifact:
            _fail(VisualReviewStoreErrorCode.INVALID_INPUT)
        try:
            raw = encode_visual_review_artifact(artifact)
        except VisualReviewArtifactError:
            _fail(VisualReviewStoreErrorCode.INVALID_INPUT)
        lease = self._acquire_catalog()
        primary: BaseException | None = None
        mutation_started = False
        root_fd = -1
        stage_name = ""
        try:
            self._recover_locked()
            root_fd = self._root.open()
            tombstone = _tombstone_name(artifact.observation_id)
            if self._entry_exists(root_fd, tombstone):
                _, digest, _ = self._read_tombstone(root_fd, tombstone)
                _fail(
                    VisualReviewStoreErrorCode.DELETED
                    if digest == artifact.observation_digest
                    else VisualReviewStoreErrorCode.CONFLICT
                )
            final = _final_name(artifact.observation_id, artifact.source_index)
            if self._entry_exists(root_fd, final):
                current, current_raw, _ = self._read_record(root_fd, final)
                if current != artifact or current_raw != raw:
                    _fail(VisualReviewStoreErrorCode.CONFLICT)
                return current
            snapshot = self._snapshot(root_fd, recovery=False)
            observations = {
                matched.group(1)
                for name in (*snapshot.finals, *snapshot.tombstones)
                if (matched := (_FINAL.fullmatch(name) or _TOMBSTONE.fullmatch(name))) is not None
            }
            suffix = _observation_suffix(artifact.observation_id)
            if (
                len(snapshot.finals) >= MAX_VISUAL_REVIEW_RECORDS
                or (
                    suffix not in observations
                    and len(observations) >= MAX_VISUAL_REVIEW_OBSERVATIONS
                )
                or snapshot.committed_bytes + len(raw) > MAX_VISUAL_REVIEW_STORE_BYTES
            ):
                _fail(VisualReviewStoreErrorCode.BUDGET_EXCEEDED)
            token = secrets.token_hex(16)
            stage_name = f".stage_{suffix}_{artifact.source_index:02d}_{token}.tmp"
            mutation_started = True
            self._write_temp(root_fd, stage_name, raw, MAX_VISUAL_REVIEW_RECORD_BYTES)
            if self._entry_exists(root_fd, tombstone):
                _fail(VisualReviewStoreErrorCode.DURABILITY_UNCERTAIN)
            _rename_noreplace(root_fd, stage_name, final)
            stage_name = ""
            os.fsync(root_fd)
            current, current_raw, _ = self._read_record(root_fd, final)
            if current != artifact or current_raw != raw:
                _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
            return current
        except VisualReviewStoreError as error:
            primary = error
            raise
        except (OSError, StorageFailure) as error:
            primary = error
            _fail(
                VisualReviewStoreErrorCode.DURABILITY_UNCERTAIN
                if mutation_started
                else VisualReviewStoreErrorCode.STORE_FAILURE
            )
        finally:
            if root_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(root_fd)
            try:
                lease.release(owner_token=lease.owner_token)
            except BaseException:
                if primary is None:
                    _fail(VisualReviewStoreErrorCode.RECOVERY_REQUIRED)

    def load_exact(
        self,
        observation_id: object,
        source_index: object,
    ) -> VisualReviewArtifact:
        selected_observation = _observation_id(_observation_suffix(observation_id))
        selected_source = _source_index(source_index)
        lease = self._acquire_catalog()
        primary: BaseException | None = None
        root_fd = -1
        try:
            self._recover_locked()
            root_fd = self._root.open()
            if self._entry_exists(root_fd, _tombstone_name(selected_observation)):
                _fail(VisualReviewStoreErrorCode.DELETED)
            name = _final_name(selected_observation, selected_source)
            if not self._entry_exists(root_fd, name):
                _fail(VisualReviewStoreErrorCode.NOT_FOUND)
            artifact, _, _ = self._read_record(root_fd, name)
            if (
                artifact.observation_id != selected_observation
                or artifact.source_index != selected_source
            ):
                _fail(VisualReviewStoreErrorCode.INTEGRITY_FAILURE)
            return artifact
        except VisualReviewStoreError as error:
            primary = error
            raise
        except (OSError, StorageFailure) as error:
            primary = error
            _fail(VisualReviewStoreErrorCode.STORE_FAILURE)
        finally:
            if root_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(root_fd)
            try:
                lease.release(owner_token=lease.owner_token)
            except BaseException:
                if primary is None:
                    _fail(VisualReviewStoreErrorCode.RECOVERY_REQUIRED)

    def list_exact(
        self,
        observation_id: object,
        observation_digest: object,
    ) -> tuple[VisualReviewArtifact, ...]:
        selected_observation = _observation_id(_observation_suffix(observation_id))
        selected_digest = _digest(observation_digest)
        result: list[VisualReviewArtifact] = []
        lease = self._acquire_catalog()
        primary: BaseException | None = None
        root_fd = -1
        try:
            self._recover_locked()
            root_fd = self._root.open()
            if self._entry_exists(root_fd, _tombstone_name(selected_observation)):
                _fail(VisualReviewStoreErrorCode.DELETED)
            snapshot = self._snapshot(root_fd, recovery=False)
            suffix = _observation_suffix(selected_observation)
            for name in snapshot.finals:
                matched = _FINAL.fullmatch(name)
                assert matched is not None
                if matched.group(1) != suffix:
                    continue
                artifact, _, _ = self._read_record(root_fd, name)
                if artifact.observation_digest != selected_digest:
                    _fail(VisualReviewStoreErrorCode.CONFLICT)
                result.append(artifact)
            return tuple(sorted(result, key=lambda item: item.source_index))
        except VisualReviewStoreError as error:
            primary = error
            raise
        except (OSError, StorageFailure) as error:
            primary = error
            _fail(VisualReviewStoreErrorCode.STORE_FAILURE)
        finally:
            if root_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(root_fd)
            try:
                lease.release(owner_token=lease.owner_token)
            except BaseException:
                if primary is None:
                    _fail(VisualReviewStoreErrorCode.RECOVERY_REQUIRED)

    def read_resource(self, uri: object) -> VisualReviewResource:
        try:
            observation_id, source_index = parse_visual_review_resource_uri(uri)
        except VisualReviewArtifactError:
            _fail(VisualReviewStoreErrorCode.INVALID_INPUT)
        artifact = self.load_exact(observation_id, source_index)
        assert type(uri) is str
        return VisualReviewResource(uri=uri, data=artifact.overlay.png_bytes)

    def delete_observation_exact(
        self,
        observation_id: object,
        observation_digest: object,
    ) -> int:
        selected_observation = _observation_id(_observation_suffix(observation_id))
        selected_digest = _digest(observation_digest)
        lease = self._acquire_catalog()
        primary: BaseException | None = None
        root_fd = -1
        mutation_started = False
        try:
            self._recover_locked()
            root_fd = self._root.open()
            tombstone_name = _tombstone_name(selected_observation)
            if self._entry_exists(root_fd, tombstone_name):
                _, current_digest, _ = self._read_tombstone(root_fd, tombstone_name)
                if current_digest != selected_digest:
                    _fail(VisualReviewStoreErrorCode.CONFLICT)
                return 0

            # Authenticate every recoverable record before publishing the durable
            # deletion intent.  A caller with a stale or forged digest must not be
            # able to poison a valid observation with an irreversible tombstone.
            snapshot = self._snapshot(root_fd, recovery=True)
            self._authenticate_observation_records(
                root_fd,
                snapshot,
                selected_observation,
                selected_digest,
            )

            raw_tombstone = _tombstone_raw(selected_observation, selected_digest)
            token = secrets.token_hex(16)
            temp_name = f".deleted_{_observation_suffix(selected_observation)}_{token}.tmp"
            mutation_started = True
            self._write_temp(
                root_fd,
                temp_name,
                raw_tombstone,
                MAX_VISUAL_REVIEW_TOMBSTONE_BYTES,
            )
            _rename_noreplace(root_fd, temp_name, tombstone_name)
            os.fsync(root_fd)

            snapshot = self._snapshot(root_fd, recovery=True)
            removed = 0
            suffix = _observation_suffix(selected_observation)
            for name in snapshot.finals:
                matched = _FINAL.fullmatch(name)
                assert matched is not None
                if matched.group(1) != suffix:
                    continue
                artifact, raw, info = self._read_record(root_fd, name)
                if artifact.observation_digest != selected_digest:
                    _fail(VisualReviewStoreErrorCode.CONFLICT)
                self._move_then_remove(root_fd, name, artifact, raw, info)
                removed += 1
            for name in snapshot.stages:
                matched = _STAGE.fullmatch(name)
                assert matched is not None
                if matched.group(1) != suffix:
                    continue
                artifact, _, _ = self._read_record(root_fd, name)
                if artifact.observation_digest != selected_digest:
                    _fail(VisualReviewStoreErrorCode.CONFLICT)
                self._unlink(root_fd, name)
            os.fsync(root_fd)
            self._snapshot(root_fd, recovery=False)
            return removed
        except VisualReviewStoreError as error:
            primary = error
            raise
        except (OSError, StorageFailure) as error:
            primary = error
            _fail(
                VisualReviewStoreErrorCode.DURABILITY_UNCERTAIN
                if mutation_started
                else VisualReviewStoreErrorCode.STORE_FAILURE
            )
        finally:
            if root_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(root_fd)
            try:
                lease.release(owner_token=lease.owner_token)
            except BaseException:
                if primary is None:
                    _fail(VisualReviewStoreErrorCode.RECOVERY_REQUIRED)


__all__ = [
    "MAX_VISUAL_REVIEW_OBSERVATIONS",
    "MAX_VISUAL_REVIEW_RECORDS",
    "MAX_VISUAL_REVIEW_STORE_BYTES",
    "MAX_VISUAL_REVIEW_TEMPORARIES",
    "VisualReviewArtifactStore",
    "VisualReviewRecoverySummary",
    "VisualReviewStoreError",
    "VisualReviewStoreErrorCode",
]
