"""Durable, bounded, non-authoritative copies of immutable CAD revisions."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from vibecad.execution.revisions import (
    LocalRevisionStore,
    ProjectHead,
    RevisionRef,
    RevisionSourceBinding,
    RevisionSourceObservation,
    RevisionStoreError,
    RevisionStoreErrorCode,
)
from vibecad.interaction.storage import CheckoutMutationLock, SafeRoot, StorageFailure
from vibecad.workflow.state import ReviewDraft, ReviewPolicy, TaskStatus
from vibecad.workflow.store import TaskRunStore, TaskStoreError, TaskStoreErrorCode

__all__ = (
    "ABANDONED_TEMP_TTL_SECONDS",
    "CLOSED_TOMBSTONE_TTL_SECONDS",
    "MAX_CHECKOUT_FILE_BYTES",
    "MAX_CHECKOUT_TEMP_ENTRIES",
    "MAX_CHECKOUT_TOTAL_BYTES",
    "MAX_CLOSED_TOMBSTONES",
    "MAX_OPEN_CHECKOUTS",
    "CheckoutDescriptor",
    "CheckoutError",
    "CheckoutErrorCode",
    "CheckoutFileSnapshot",
    "CheckoutSourceLiveness",
    "CheckoutState",
    "CheckoutStoreRootTrust",
    "DraftCheckoutSource",
    "HeadCheckoutSource",
    "ManagedCheckoutStore",
    "ResolvedCheckoutSource",
)

MAX_CHECKOUT_FILE_BYTES = 536_870_912
MAX_CHECKOUT_TOTAL_BYTES = 2_147_483_648
MAX_OPEN_CHECKOUTS = 8
ABANDONED_TEMP_TTL_SECONDS = 86_400
CLOSED_TOMBSTONE_TTL_SECONDS = 2_592_000
MAX_CHECKOUT_TEMP_ENTRIES = 8
MAX_CLOSED_TOMBSTONES = 1_024

_LEGACY_SCHEMA_VERSION = 1
_OPEN_SCHEMA_VERSION = 2
_TOMBSTONE_SCHEMA_VERSION = 2
_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_RECORD_BYTES = 65_536
_COPY_CHUNK_BYTES = 65_536
_RECORD_DOMAIN = b"vibecad-managed-checkout-open-v1\0"
_RECORD_DOMAIN_V2 = b"vibecad-managed-checkout-open-v2\0"
_TOMBSTONE_DOMAIN = b"vibecad-managed-checkout-closed-v1\0"
_TOMBSTONE_DOMAIN_V2 = b"vibecad-managed-checkout-closed-v2\0"
_OPEN_KEY_RE = re.compile(r"checkout_open_[0-9a-f]{32}\Z")
_CHECKOUT_RE = re.compile(r"checkout_[0-9a-f]{32}\Z")
_PROJECT_RE = re.compile(r"project_[0-9a-f]{32}\Z")
_TASK_RE = re.compile(r"task_[0-9a-f]{32}\Z")
_DRAFT_RE = re.compile(r"draft_[0-9a-f]{32}\Z")
_REVISION_RE = re.compile(r"revision_[0-9a-f]{32}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_OPEN_TEMP_RE = re.compile(r"\.checkout_[0-9a-f]{32}\.tmp\Z")
_TOMBSTONE_RE = re.compile(r"closed_checkout_[0-9a-f]{32}\.json\Z")
_TOMBSTONE_TEMP_RE = re.compile(r"\.closed_checkout_[0-9a-f]{32}\.json\.[0-9a-f]{32}\.tmp\Z")


class CheckoutStoreRootTrust(StrEnum):
    TRUSTED_LOCAL = "trusted_local"


class CheckoutState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class CheckoutSourceLiveness(StrEnum):
    LIVE = "live"
    STALE = "stale"
    REVOKED = "revoked"
    RECOVERY_REQUIRED = "recovery_required"


class CheckoutErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INTEGRITY_FAILURE = "integrity_failure"
    UNSAFE_STORE = "unsafe_store"
    IO_ERROR = "io_error"
    DURABILITY_UNCERTAIN = "durability_uncertain"
    CLEANUP_REQUIRED = "cleanup_required"
    WRONG_PROCESS = "wrong_process"
    RECOVERY_REQUIRED = "recovery_required"


_MESSAGES = {
    CheckoutErrorCode.INVALID_INPUT: "The checkout request is invalid.",
    CheckoutErrorCode.NOT_FOUND: "The checkout was not found.",
    CheckoutErrorCode.CONFLICT: "The checkout request conflicts with durable state.",
    CheckoutErrorCode.RESOURCE_EXHAUSTED: "The managed checkout capacity is exhausted.",
    CheckoutErrorCode.INTEGRITY_FAILURE: "The managed checkout failed integrity validation.",
    CheckoutErrorCode.UNSAFE_STORE: "The managed checkout store is unsafe.",
    CheckoutErrorCode.IO_ERROR: "The managed checkout operation failed.",
    CheckoutErrorCode.DURABILITY_UNCERTAIN: "Managed checkout durability is uncertain.",
    CheckoutErrorCode.CLEANUP_REQUIRED: "Managed checkout cleanup is required.",
    CheckoutErrorCode.WRONG_PROCESS: "The managed checkout belongs to another process.",
    CheckoutErrorCode.RECOVERY_REQUIRED: "Checkout source authority requires recovery.",
}


class CheckoutError(ValueError):
    __slots__ = ("code", "descriptor", "message")

    def __init__(
        self,
        code: CheckoutErrorCode,
        *,
        descriptor: CheckoutDescriptor | None = None,
    ) -> None:
        if type(code) is not CheckoutErrorCode:
            raise TypeError("code must be a CheckoutErrorCode")
        if descriptor is not None and code not in (
            CheckoutErrorCode.DURABILITY_UNCERTAIN,
            CheckoutErrorCode.CLEANUP_REQUIRED,
        ):
            raise ValueError("descriptor is invalid for this checkout error")
        self.code = code
        self.message = _MESSAGES[code]
        self.descriptor = descriptor
        super().__init__(self.message)


def _identifier(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise CheckoutError(CheckoutErrorCode.INVALID_INPUT)
    return value


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
    return value


def _size(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_CHECKOUT_FILE_BYTES:
        raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class HeadCheckoutSource:
    project_id: str

    def __post_init__(self) -> None:
        _identifier(self.project_id, _PROJECT_RE)

    def to_mapping(self) -> dict[str, str]:
        return {"kind": "head", "project_id": self.project_id}


@dataclass(frozen=True, slots=True, kw_only=True)
class DraftCheckoutSource:
    task_id: str
    draft_id: str
    expected_generation: int

    def __post_init__(self) -> None:
        _identifier(self.task_id, _TASK_RE)
        _identifier(self.draft_id, _DRAFT_RE)
        if (
            type(self.expected_generation) is not int
            or not 0 <= self.expected_generation <= _MAX_SAFE_INTEGER
        ):
            raise CheckoutError(CheckoutErrorCode.INVALID_INPUT)

    def to_mapping(self) -> dict[str, int | str]:
        return {
            "kind": "draft",
            "task_id": self.task_id,
            "draft_id": self.draft_id,
            "expected_generation": self.expected_generation,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedCheckoutSource:
    kind: str
    project_id: str
    revision_id: str
    manifest_sha256: str
    model_sha256: str
    size_bytes: int
    task_id: str | None = None
    draft_id: str | None = None
    task_generation: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("head", "draft"):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        _identifier(self.project_id, _PROJECT_RE)
        _identifier(self.revision_id, _REVISION_RE)
        _digest(self.manifest_sha256)
        _digest(self.model_sha256)
        _size(self.size_bytes)
        if self.kind == "head":
            if any(
                value is not None for value in (self.task_id, self.draft_id, self.task_generation)
            ):
                raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        else:
            _identifier(self.task_id, _TASK_RE)
            _identifier(self.draft_id, _DRAFT_RE)
            if (
                type(self.task_generation) is not int
                or not 0 <= self.task_generation <= _MAX_SAFE_INTEGER
            ):
                raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)

    def to_mapping(self) -> dict[str, int | str | None]:
        return {
            "kind": self.kind,
            "project_id": self.project_id,
            "revision_id": self.revision_id,
            "manifest_sha256": self.manifest_sha256,
            "model_sha256": self.model_sha256,
            "size_bytes": self.size_bytes,
            "task_id": self.task_id,
            "draft_id": self.draft_id,
            "task_generation": self.task_generation,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckoutDescriptor:
    checkout_id: str
    open_key: str
    state: CheckoutState
    dirty: bool
    source: ResolvedCheckoutSource
    initial_model_sha256: str
    current_model_sha256: str
    current_size_bytes: int
    local_path: Path | None
    source_head: ProjectHead | None
    source_binding: RevisionSourceBinding | None
    source_liveness: CheckoutSourceLiveness
    authoritative: bool = False

    def __post_init__(self) -> None:
        _identifier(self.checkout_id, _CHECKOUT_RE)
        _identifier(self.open_key, _OPEN_KEY_RE)
        if type(self.state) is not CheckoutState or self.authoritative is not False:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        if type(self.dirty) is not bool or type(self.source) is not ResolvedCheckoutSource:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        if type(self.source_liveness) is not CheckoutSourceLiveness:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        if (self.source_head is None) != (self.source_binding is None):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        if self.source_head is None:
            if self.source_liveness is not CheckoutSourceLiveness.RECOVERY_REQUIRED:
                raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        elif (
            type(self.source_head) is not ProjectHead
            or type(self.source_binding) is not RevisionSourceBinding
        ):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        elif self.source_head.project_id != self.source.project_id:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        elif self.source.kind == "head" and (
            self.source_head.revision_id != self.source.revision_id
            or self.source_head.manifest_sha256 != self.source.manifest_sha256
        ):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        _digest(self.initial_model_sha256)
        _digest(self.current_model_sha256)
        _size(self.current_size_bytes)
        if self.state is CheckoutState.OPEN and not isinstance(self.local_path, Path):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        if self.state is CheckoutState.CLOSED and self.local_path is not None:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)

    def to_wire_mapping(self) -> dict[str, object]:
        """Return the frozen protocol-v1 projection."""
        return {
            "checkout_id": self.checkout_id,
            "open_key": self.open_key,
            "state": self.state.value,
            "authoritative": False,
            "dirty": self.dirty,
            "source": self.source.to_mapping(),
            "initial_model_sha256": self.initial_model_sha256,
            "current_model_sha256": self.current_model_sha256,
            "current_size_bytes": self.current_size_bytes,
        }

    def to_local_mapping(self) -> dict[str, object]:
        """Return the path-free local-protocol projection used by protocol v2."""
        return self.to_wire_mapping() | {
            "source_head": (None if self.source_head is None else self.source_head.to_mapping()),
            "source_liveness": self.source_liveness.value,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class _CheckoutEntryBinding:
    dev: int
    ino: int
    mode: int
    uid: int
    gid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int

    def __post_init__(self) -> None:
        values = (
            self.dev,
            self.ino,
            self.mode,
            self.uid,
            self.gid,
            self.nlink,
            self.size,
            self.mtime_ns,
            self.ctime_ns,
        )
        if any(type(value) is not int or value < 0 for value in values) or self.nlink < 1:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)


def _entry_binding(value: os.stat_result) -> _CheckoutEntryBinding:
    try:
        return _CheckoutEntryBinding(
            dev=value.st_dev,
            ino=value.st_ino,
            mode=value.st_mode,
            uid=value.st_uid,
            gid=value.st_gid,
            nlink=value.st_nlink,
            size=value.st_size,
            mtime_ns=value.st_mtime_ns,
            ctime_ns=value.st_ctime_ns,
        )
    except (AttributeError, TypeError, ValueError):
        raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None


def _private_directory_binding(value: _CheckoutEntryBinding) -> bool:
    return (
        stat.S_ISDIR(value.mode) and value.uid == os.geteuid() and stat.S_IMODE(value.mode) == 0o700
    )


def _private_file_binding(value: _CheckoutEntryBinding) -> bool:
    return (
        stat.S_ISREG(value.mode)
        and value.uid == os.geteuid()
        and stat.S_IMODE(value.mode) == 0o600
        and value.nlink == 1
    )


def _same_root_identity(
    left: _CheckoutEntryBinding,
    right: _CheckoutEntryBinding,
) -> bool:
    return (
        left.dev,
        left.ino,
        left.mode,
        left.uid,
        left.gid,
    ) == (
        right.dev,
        right.ino,
        right.mode,
        right.uid,
        right.gid,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckoutFileSnapshot:
    """One live managed file observation suitable for a short-lived grant."""

    descriptor: CheckoutDescriptor
    root_binding: _CheckoutEntryBinding
    directory_binding: _CheckoutEntryBinding
    file_binding: _CheckoutEntryBinding
    model_sha256: str
    size_bytes: int
    path: Path

    def __post_init__(self) -> None:
        if (
            type(self.descriptor) is not CheckoutDescriptor
            or type(self.root_binding) is not _CheckoutEntryBinding
            or type(self.directory_binding) is not _CheckoutEntryBinding
            or type(self.file_binding) is not _CheckoutEntryBinding
            or type(self.path) is not type(Path("/"))
            or not self.path.is_absolute()
            or self.path.name != "model.FCStd"
            or self.path.parent.name != self.descriptor.checkout_id
            or self.descriptor.state is not CheckoutState.OPEN
            or self.descriptor.source_liveness is not CheckoutSourceLiveness.LIVE
            or self.descriptor.local_path != self.path
            or not _private_directory_binding(self.root_binding)
            or not _private_directory_binding(self.directory_binding)
            or not _private_file_binding(self.file_binding)
            or self.root_binding.dev != self.directory_binding.dev
            or self.directory_binding.dev != self.file_binding.dev
        ):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        _digest(self.model_sha256)
        _size(self.size_bytes)
        if (
            self.file_binding.size != self.size_bytes
            or self.descriptor.current_model_sha256 != self.model_sha256
            or self.descriptor.current_size_bytes != self.size_bytes
        ):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)


@dataclass(frozen=True, slots=True)
class _OpenRecord:
    checkout_id: str
    open_key: str
    intent: dict[str, object]
    source: ResolvedCheckoutSource
    initial_model_sha256: str
    directory_identity: tuple[int, int]
    source_binding: RevisionSourceBinding | None
    created_ns: int
    source_head: ProjectHead | None


@dataclass(frozen=True, slots=True)
class _ClosedRecord:
    descriptor: CheckoutDescriptor
    intent: dict[str, object]
    closed_ns: int


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _encode_record(body: dict[str, object], domain: bytes) -> bytes:
    checksum = hashlib.sha256(domain + _canonical(body)).hexdigest()
    return _canonical(body | {"checksum": checksum})


def _strict_json(raw: bytes) -> dict[str, object]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid_constant(_raw):
        raise ValueError

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
    if type(value) is not dict:
        raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
    return value


def _resolved_from_mapping(value: object) -> ResolvedCheckoutSource:
    if type(value) is not dict or set(value) != {
        "kind",
        "project_id",
        "revision_id",
        "manifest_sha256",
        "model_sha256",
        "size_bytes",
        "task_id",
        "draft_id",
        "task_generation",
    }:
        raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
    try:
        return ResolvedCheckoutSource(**value)
    except (CheckoutError, TypeError):
        raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None


def _head_from_mapping(value: object) -> ProjectHead:
    if type(value) is not dict:
        raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
    try:
        return ProjectHead.from_mapping(value)
    except (RevisionStoreError, TypeError, ValueError):
        raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None


def _binding_mapping(value: RevisionSourceBinding) -> dict[str, int]:
    if type(value) is not RevisionSourceBinding:
        raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
    return {
        "dev": value.dev,
        "ino": value.ino,
        "mode": value.mode,
        "uid": value.uid,
        "nlink": value.nlink,
        "size": value.size,
        "mtime_ns": value.mtime_ns,
        "ctime_ns": value.ctime_ns,
    }


def _binding_from_mapping(value: object) -> RevisionSourceBinding:
    if type(value) is not dict or set(value) != {
        "dev",
        "ino",
        "mode",
        "uid",
        "nlink",
        "size",
        "mtime_ns",
        "ctime_ns",
    }:
        raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
    try:
        return RevisionSourceBinding(**value)
    except (RevisionStoreError, TypeError, ValueError):
        raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None


def _intent_mapping(source: object) -> dict[str, object]:
    if type(source) is HeadCheckoutSource:
        return dict(source.to_mapping())
    if type(source) is DraftCheckoutSource:
        return dict(source.to_mapping())
    raise CheckoutError(CheckoutErrorCode.INVALID_INPUT)


def _intent_from_mapping(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
    try:
        kind = value.get("kind")
        if kind == "head" and set(value) == {"kind", "project_id"}:
            HeadCheckoutSource(project_id=value["project_id"])
        elif kind == "draft" and set(value) == {
            "kind",
            "task_id",
            "draft_id",
            "expected_generation",
        }:
            DraftCheckoutSource(
                task_id=value["task_id"],
                draft_id=value["draft_id"],
                expected_generation=value["expected_generation"],
            )
        else:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
    except (CheckoutError, TypeError):
        raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
    return dict(value)


def _new_checkout_id() -> str:
    return "checkout_" + secrets.token_hex(16)


class ManagedCheckoutStore:
    """A restart-safe checkout store. It has deliberately no publish operation."""

    __slots__ = (
        "_creator_pid",
        "_lock",
        "_lock_root",
        "_revision_store",
        "_root",
        "_task_store",
    )

    def __init__(
        self,
        root,
        lock_root,
        revision_store,
        task_store,
        *,
        trust,
    ) -> None:
        if (
            type(trust) is not CheckoutStoreRootTrust
            or trust is not CheckoutStoreRootTrust.TRUSTED_LOCAL
        ):
            raise CheckoutError(CheckoutErrorCode.UNSAFE_STORE)
        if not isinstance(revision_store, LocalRevisionStore):
            raise TypeError("revision_store must be a LocalRevisionStore")
        if not isinstance(task_store, TaskRunStore):
            raise TypeError("task_store must be a TaskRunStore")
        try:
            checkout_root = SafeRoot(Path(root))
            locks = SafeRoot(Path(lock_root))
            if checkout_root.identity == locks.identity:
                raise StorageFailure("checkout and lock roots must be distinct")
            mutation_lock = CheckoutMutationLock(locks)
        except StorageFailure:
            raise CheckoutError(CheckoutErrorCode.UNSAFE_STORE) from None
        self._root = checkout_root
        self._lock_root = locks
        self._lock = mutation_lock
        self._revision_store = revision_store
        self._task_store = task_store
        self._creator_pid = os.getpid()

    @property
    def lock_root(self) -> Path:
        return self._lock_root.path

    @property
    def task_store(self) -> TaskRunStore:
        return self._task_store

    @property
    def root(self) -> Path:
        return self._root.path

    def _ensure_process(self) -> None:
        if os.getpid() != self._creator_pid:
            raise CheckoutError(CheckoutErrorCode.WRONG_PROCESS)

    def _fault(self, stage: str) -> None:
        del stage

    def open(
        self,
        open_key: str,
        source: HeadCheckoutSource | DraftCheckoutSource,
    ) -> CheckoutDescriptor:
        self._ensure_process()
        canonical_key = _identifier(open_key, _OPEN_KEY_RE)
        intent = _intent_mapping(source)
        try:
            with self._lock.hold():
                root_fd = self._root.open()
                try:
                    inventory = self._inventory(root_fd, cleanup=True)
                    replay = self._find_key(inventory, canonical_key)
                    if replay is not None:
                        record_intent, checkout_id, closed_descriptor = replay
                        if record_intent != intent:
                            raise CheckoutError(CheckoutErrorCode.CONFLICT)
                        if closed_descriptor is not None:
                            return self._project_liveness(closed_descriptor)
                        self._require_aggregate_budget(inventory)
                        return self._get_locked(root_fd, checkout_id)
                    resolved, model_path, source_head, source_binding = self._resolve(source)
                    self._admit(inventory, resolved.size_bytes)
                    return self._publish_open(
                        root_fd,
                        canonical_key,
                        intent,
                        resolved,
                        model_path,
                        source_head,
                        source_binding,
                    )
                finally:
                    os.close(root_fd)
        except CheckoutError:
            raise
        except StorageFailure:
            raise CheckoutError(CheckoutErrorCode.UNSAFE_STORE) from None
        except OSError:
            raise CheckoutError(CheckoutErrorCode.IO_ERROR) from None

    def get(self, checkout_id: str) -> CheckoutDescriptor:
        self._ensure_process()
        canonical_id = _identifier(checkout_id, _CHECKOUT_RE)
        try:
            with self._lock.hold():
                root_fd = self._root.open()
                try:
                    inventory = self._inventory(root_fd, cleanup=True)
                    closed = self._load_tombstone(root_fd, canonical_id, required=False)
                    if closed is not None:
                        return self._project_liveness(closed.descriptor)
                    self._require_aggregate_budget(inventory)
                    return self._get_locked(root_fd, canonical_id)
                finally:
                    os.close(root_fd)
        except CheckoutError:
            raise
        except StorageFailure:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
        except OSError:
            raise CheckoutError(CheckoutErrorCode.IO_ERROR) from None

    def close(self, checkout_id: str) -> CheckoutDescriptor:
        self._ensure_process()
        canonical_id = _identifier(checkout_id, _CHECKOUT_RE)
        try:
            with self._lock.hold():
                root_fd = self._root.open()
                try:
                    self._inventory(root_fd, cleanup=True)
                    closed = self._load_tombstone(root_fd, canonical_id, required=False)
                    if closed is not None:
                        closed_descriptor = self._project_liveness(closed.descriptor)
                        if self._entry_exists(root_fd, canonical_id):
                            try:
                                self._delete_checkout_directory(root_fd, canonical_id)
                                os.fsync(root_fd)
                            except (CheckoutError, OSError, StorageFailure):
                                raise CheckoutError(
                                    CheckoutErrorCode.CLEANUP_REQUIRED,
                                    descriptor=closed_descriptor,
                                ) from None
                        return closed_descriptor
                    descriptor = self._get_locked(root_fd, canonical_id)
                    tombstone = _ClosedRecord(
                        descriptor=CheckoutDescriptor(
                            checkout_id=descriptor.checkout_id,
                            open_key=descriptor.open_key,
                            state=CheckoutState.CLOSED,
                            dirty=descriptor.dirty,
                            source=descriptor.source,
                            initial_model_sha256=descriptor.initial_model_sha256,
                            current_model_sha256=descriptor.current_model_sha256,
                            current_size_bytes=descriptor.current_size_bytes,
                            local_path=None,
                            source_head=descriptor.source_head,
                            source_binding=descriptor.source_binding,
                            source_liveness=CheckoutSourceLiveness.RECOVERY_REQUIRED,
                        ),
                        intent=self._load_open(root_fd, canonical_id).intent,
                        closed_ns=time.time_ns(),
                    )
                    self._write_tombstone(root_fd, tombstone)
                    self._fault("after_tombstone_publish")
                    try:
                        self._delete_checkout_directory(root_fd, canonical_id)
                        os.fsync(root_fd)
                        self._fault("after_checkout_delete")
                    except (CheckoutError, OSError, StorageFailure):
                        raise CheckoutError(
                            CheckoutErrorCode.CLEANUP_REQUIRED,
                            descriptor=self._project_liveness(tombstone.descriptor),
                        ) from None
                    return self._project_liveness(tombstone.descriptor)
                finally:
                    os.close(root_fd)
        except CheckoutError:
            raise
        except StorageFailure:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
        except OSError:
            raise CheckoutError(CheckoutErrorCode.IO_ERROR) from None

    @staticmethod
    def _require_live_descriptor(descriptor: CheckoutDescriptor) -> None:
        if descriptor.source_liveness is CheckoutSourceLiveness.RECOVERY_REQUIRED:
            raise CheckoutError(CheckoutErrorCode.RECOVERY_REQUIRED)
        if (
            descriptor.state is not CheckoutState.OPEN
            or descriptor.source_liveness is not CheckoutSourceLiveness.LIVE
        ):
            raise CheckoutError(CheckoutErrorCode.CONFLICT)

    def _capture_live_locked(
        self,
        root_fd: int,
        checkout_id: str,
        inventory: dict[str, object],
    ) -> CheckoutFileSnapshot:
        closed = self._load_tombstone(root_fd, checkout_id, required=False)
        if closed is not None:
            self._require_live_descriptor(self._project_liveness(closed.descriptor))
            raise CheckoutError(CheckoutErrorCode.CONFLICT)
        self._require_aggregate_budget(inventory)
        descriptor, root_binding, directory_binding, file_binding = self._inspect_open_locked(
            root_fd,
            checkout_id,
        )
        self._require_live_descriptor(descriptor)
        assert descriptor.local_path is not None
        return CheckoutFileSnapshot(
            descriptor=descriptor,
            root_binding=root_binding,
            directory_binding=directory_binding,
            file_binding=file_binding,
            model_sha256=descriptor.current_model_sha256,
            size_bytes=descriptor.current_size_bytes,
            path=descriptor.local_path,
        )

    def capture_live_file(self, checkout_id: str) -> CheckoutFileSnapshot:
        """Capture one identity-bound observation without exposing it on the wire."""
        self._ensure_process()
        canonical_id = _identifier(checkout_id, _CHECKOUT_RE)
        try:
            with self._lock.hold():
                root_fd = self._root.open()
                try:
                    inventory = self._inventory(root_fd, cleanup=True)
                    return self._capture_live_locked(root_fd, canonical_id, inventory)
                finally:
                    os.close(root_fd)
        except CheckoutError:
            raise
        except StorageFailure:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
        except OSError:
            raise CheckoutError(CheckoutErrorCode.IO_ERROR) from None

    def require_same_live_file(
        self,
        snapshot: CheckoutFileSnapshot,
    ) -> CheckoutFileSnapshot:
        """Re-observe liveness and require the exact managed file captured earlier."""
        self._ensure_process()
        if type(snapshot) is not CheckoutFileSnapshot:
            raise CheckoutError(CheckoutErrorCode.INVALID_INPUT)
        canonical_id = _identifier(snapshot.descriptor.checkout_id, _CHECKOUT_RE)
        try:
            with self._lock.hold():
                root_fd = self._root.open()
                try:
                    inventory = self._inventory(root_fd, cleanup=True)
                    current = self._capture_live_locked(root_fd, canonical_id, inventory)
                finally:
                    os.close(root_fd)
            if (
                current.descriptor != snapshot.descriptor
                or not _same_root_identity(current.root_binding, snapshot.root_binding)
                or current.directory_binding != snapshot.directory_binding
                or current.file_binding != snapshot.file_binding
                or current.model_sha256 != snapshot.model_sha256
                or current.size_bytes != snapshot.size_bytes
                or current.path != snapshot.path
            ):
                raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
            return current
        except CheckoutError:
            raise
        except StorageFailure:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
        except OSError:
            raise CheckoutError(CheckoutErrorCode.IO_ERROR) from None

    def require_live(self, checkout_id: str) -> CheckoutDescriptor:
        """Recompute liveness and reject a closed or non-live checkout."""
        descriptor = self.get(checkout_id)
        self._require_live_descriptor(descriptor)
        return descriptor

    def require_acceptance(
        self,
        checkout_id: str,
        *,
        task_id: str,
        draft_id: str,
        expected_generation: int,
    ) -> CheckoutDescriptor:
        """Guard a review action; TaskService remains the only commit authority."""
        canonical_task = _identifier(task_id, _TASK_RE)
        canonical_draft = _identifier(draft_id, _DRAFT_RE)
        if (
            type(expected_generation) is not int
            or not 0 <= expected_generation <= _MAX_SAFE_INTEGER
        ):
            raise CheckoutError(CheckoutErrorCode.INVALID_INPUT)
        descriptor = self.require_live(checkout_id)
        source = descriptor.source
        if (
            source.kind != "draft"
            or source.task_id != canonical_task
            or source.draft_id != canonical_draft
            or source.task_generation != expected_generation
            or descriptor.dirty
        ):
            raise CheckoutError(CheckoutErrorCode.CONFLICT)
        return descriptor

    def _inventory(self, root_fd: int, *, cleanup: bool) -> dict[str, object]:
        now_ns = time.time_ns()
        opens: list[_OpenRecord] = []
        closed: list[_ClosedRecord] = []
        temps: list[tuple[str, int]] = []
        expired_tombstones: dict[str, str] = {}
        try:
            names = os.listdir(root_fd)
        except OSError:
            raise CheckoutError(CheckoutErrorCode.IO_ERROR) from None
        for name in names:
            if not _TOMBSTONE_RE.fullmatch(name):
                continue
            item = self._load_tombstone_name(root_fd, name)
            expired = now_ns - item.closed_ns > CLOSED_TOMBSTONE_TTL_SECONDS * 1_000_000_000
            closed.append(item)
            if expired and cleanup:
                expired_tombstones[item.descriptor.checkout_id] = name
        closed_by_id = {record.descriptor.checkout_id: record for record in closed}
        if len(closed_by_id) != len(closed):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        for name in names:
            if _CHECKOUT_RE.fullmatch(name):
                tombstone = closed_by_id.get(name)
                if tombstone is None:
                    opens.append(self._load_open(root_fd, name))
                elif cleanup:
                    try:
                        self._delete_checkout_directory(root_fd, name, partial=True)
                        os.fsync(root_fd)
                    except (CheckoutError, OSError, StorageFailure):
                        raise CheckoutError(
                            CheckoutErrorCode.CLEANUP_REQUIRED,
                            descriptor=self._project_liveness(tombstone.descriptor),
                        ) from None
            elif _TOMBSTONE_RE.fullmatch(name):
                continue
            elif _OPEN_TEMP_RE.fullmatch(name):
                info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                    raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
                expired = now_ns - info.st_mtime_ns > ABANDONED_TEMP_TTL_SECONDS * 1_000_000_000
                if expired and cleanup:
                    self._delete_checkout_directory(root_fd, name, partial=True)
                    os.fsync(root_fd)
                else:
                    temps.append((name, self._temp_model_size(root_fd, name)))
            elif _TOMBSTONE_TEMP_RE.fullmatch(name):
                info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if not self._root.regular_file(info, maximum=_MAX_RECORD_BYTES):
                    raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
                expired = now_ns - info.st_mtime_ns > ABANDONED_TEMP_TTL_SECONDS * 1_000_000_000
                if expired and cleanup:
                    os.unlink(name, dir_fd=root_fd)
                    os.fsync(root_fd)
                else:
                    temps.append((name, 0))
            else:
                raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        if expired_tombstones:
            for name in expired_tombstones.values():
                os.unlink(name, dir_fd=root_fd)
            os.fsync(root_fd)
            closed = [
                record
                for record in closed
                if record.descriptor.checkout_id not in expired_tombstones
            ]
        if len(opens) > MAX_OPEN_CHECKOUTS or len(temps) > MAX_CHECKOUT_TEMP_ENTRIES:
            raise CheckoutError(CheckoutErrorCode.RESOURCE_EXHAUSTED)
        if len(closed) > MAX_CLOSED_TOMBSTONES:
            raise CheckoutError(CheckoutErrorCode.RESOURCE_EXHAUSTED)
        keys = [item.open_key for item in opens] + [item.descriptor.open_key for item in closed]
        ids = [item.checkout_id for item in opens] + [
            item.descriptor.checkout_id for item in closed
        ]
        if len(keys) != len(set(keys)) or len(ids) != len(set(ids)):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        open_sizes = {
            record.checkout_id: self._open_model_size(root_fd, record) for record in opens
        }
        aggregate_bytes = sum(open_sizes.values()) + sum(size for _, size in temps)
        return {
            "open": opens,
            "closed": closed,
            "temp": temps,
            "open_sizes": open_sizes,
            "aggregate_bytes": aggregate_bytes,
            "aggregate_over_budget": aggregate_bytes > MAX_CHECKOUT_TOTAL_BYTES,
        }

    @staticmethod
    def _require_aggregate_budget(inventory) -> None:
        if inventory["aggregate_over_budget"] is True:
            raise CheckoutError(CheckoutErrorCode.RESOURCE_EXHAUSTED)

    @staticmethod
    def _find_key(inventory, open_key: str):
        for record in inventory["closed"]:
            if record.descriptor.open_key == open_key:
                return record.intent, record.descriptor.checkout_id, record.descriptor
        for record in inventory["open"]:
            if record.open_key == open_key:
                return record.intent, record.checkout_id, None
        return None

    def _admit(self, inventory, incoming: int) -> None:
        opens = inventory["open"]
        closed = inventory["closed"]
        temps = inventory["temp"]
        if len(opens) >= MAX_OPEN_CHECKOUTS:
            raise CheckoutError(CheckoutErrorCode.RESOURCE_EXHAUSTED)
        if len(temps) >= MAX_CHECKOUT_TEMP_ENTRIES:
            raise CheckoutError(CheckoutErrorCode.RESOURCE_EXHAUSTED)
        if len(closed) + len(opens) >= MAX_CLOSED_TOMBSTONES:
            raise CheckoutError(CheckoutErrorCode.RESOURCE_EXHAUSTED)
        total = inventory["aggregate_bytes"]
        if incoming > MAX_CHECKOUT_FILE_BYTES or total + incoming > MAX_CHECKOUT_TOTAL_BYTES:
            raise CheckoutError(CheckoutErrorCode.RESOURCE_EXHAUSTED)

    def _load_validated_head(self, project_id: str) -> ProjectHead:
        head = self._revision_store.load_head(project_id)
        if type(head) is not ProjectHead or head.project_id != project_id:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        return head

    @staticmethod
    def _head_for_draft(draft: ReviewDraft) -> ProjectHead:
        try:
            return ProjectHead(
                project_id=draft.project_id,
                generation=draft.base_generation,
                revision_id=draft.base_revision,
                manifest_sha256=draft.base_manifest_sha256,
            )
        except (RevisionStoreError, TypeError, ValueError):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None

    @staticmethod
    def _binding_from_stat(value: os.stat_result) -> RevisionSourceBinding:
        try:
            return RevisionSourceBinding(
                dev=value.st_dev,
                ino=value.st_ino,
                mode=value.st_mode,
                uid=value.st_uid,
                nlink=value.st_nlink,
                size=value.st_size,
                mtime_ns=value.st_mtime_ns,
                ctime_ns=value.st_ctime_ns,
            )
        except (RevisionStoreError, TypeError, ValueError):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None

    @staticmethod
    def _revision_matches_source(
        revision: object,
        source: ResolvedCheckoutSource,
        source_head: ProjectHead,
    ) -> bool:
        if type(revision) is not RevisionRef or revision.model is None:
            return False
        if not (
            revision.project_id == source.project_id
            and revision.id == source.revision_id
            and revision.manifest_sha256 == source.manifest_sha256
            and revision.model.sha256 == source.model_sha256
            and revision.model.size_bytes == source.size_bytes
        ):
            return False
        if source.kind == "head":
            return (
                source_head.revision_id == source.revision_id
                and source_head.manifest_sha256 == source.manifest_sha256
            )
        return revision.base_revision == source_head.revision_id

    def _validate_bound_source(
        self,
        source: ResolvedCheckoutSource,
        source_head: ProjectHead,
        source_binding: RevisionSourceBinding,
    ) -> RevisionSourceObservation:
        observation = self._revision_store.observe_model_source(
            source.project_id,
            source.revision_id,
        )
        if (
            type(observation) is not RevisionSourceObservation
            or observation.model_binding != source_binding
            or not self._revision_matches_source(
                observation.revision,
                source,
                source_head,
            )
        ):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        return observation

    @staticmethod
    def _draft_observation(
        stored: object,
        source: ResolvedCheckoutSource,
        source_head: ProjectHead,
    ) -> tuple[object, ...] | CheckoutSourceLiveness:
        try:
            generation = stored.generation
            task = stored.task_run
            task_id = task.id
            project_id = task.project_id
            base_revision = task.base_revision
            review_policy = task.review_policy
            status = task.status
            draft = task.draft
        except (AttributeError, TypeError):
            return CheckoutSourceLiveness.RECOVERY_REQUIRED
        if type(generation) is not int:
            return CheckoutSourceLiveness.RECOVERY_REQUIRED
        if generation != source.task_generation:
            return CheckoutSourceLiveness.REVOKED
        if status in {TaskStatus.SUCCEEDED, TaskStatus.REJECTED}:
            return CheckoutSourceLiveness.REVOKED
        if type(status) is not TaskStatus or status is not TaskStatus.AWAITING_USER_REVIEW:
            return CheckoutSourceLiveness.RECOVERY_REQUIRED
        if review_policy is not ReviewPolicy.REQUIRE_REVIEW:
            return CheckoutSourceLiveness.RECOVERY_REQUIRED
        if type(draft) is not ReviewDraft:
            return CheckoutSourceLiveness.REVOKED
        if (
            task_id != source.task_id
            or draft.id != source.draft_id
            or draft.task_id != source.task_id
            or draft.revision_id != source.revision_id
            or draft.manifest_sha256 != source.manifest_sha256
        ):
            return CheckoutSourceLiveness.REVOKED
        if (
            project_id != source.project_id
            or draft.project_id != source.project_id
            or base_revision != source_head.revision_id
            or draft.base_revision != source_head.revision_id
            or draft.base_generation != source_head.generation
            or draft.base_manifest_sha256 != source_head.manifest_sha256
        ):
            return CheckoutSourceLiveness.RECOVERY_REQUIRED
        return (
            generation,
            task_id,
            project_id,
            base_revision,
            review_policy,
            status,
            draft,
        )

    @staticmethod
    def _head_liveness(
        current: ProjectHead,
        source_head: ProjectHead,
    ) -> CheckoutSourceLiveness:
        if current == source_head:
            return CheckoutSourceLiveness.LIVE
        if (
            current.project_id == source_head.project_id
            and current.generation > source_head.generation
        ):
            return CheckoutSourceLiveness.STALE
        return CheckoutSourceLiveness.RECOVERY_REQUIRED

    def _evaluate_liveness(
        self,
        source: ResolvedCheckoutSource,
        source_head: ProjectHead | None,
        source_binding: RevisionSourceBinding | None,
    ) -> CheckoutSourceLiveness:
        if source_head is None or source_binding is None:
            return CheckoutSourceLiveness.RECOVERY_REQUIRED
        try:
            if source.kind == "head":
                self._validate_bound_source(source, source_head, source_binding)
                current = self._load_validated_head(source.project_id)
                return self._head_liveness(current, source_head)

            first = self._draft_observation(
                self._task_store.load(source.task_id),
                source,
                source_head,
            )
            if type(first) is CheckoutSourceLiveness:
                return first
            self._validate_bound_source(source, source_head, source_binding)
            second = self._draft_observation(
                self._task_store.load(source.task_id),
                source,
                source_head,
            )
            if type(second) is CheckoutSourceLiveness:
                return second
            current = self._load_validated_head(source.project_id)
            final = self._draft_observation(
                self._task_store.load(source.task_id),
                source,
                source_head,
            )
            if type(final) is CheckoutSourceLiveness:
                return final
            if first != second or second != final:
                return CheckoutSourceLiveness.REVOKED
            return self._head_liveness(current, source_head)
        except Exception:
            return CheckoutSourceLiveness.RECOVERY_REQUIRED

    def _project_liveness(self, descriptor: CheckoutDescriptor) -> CheckoutDescriptor:
        liveness = self._evaluate_liveness(
            descriptor.source,
            descriptor.source_head,
            descriptor.source_binding,
        )
        return replace(descriptor, source_liveness=liveness)

    @staticmethod
    def _require_record_binding(
        intent: dict[str, object],
        source: ResolvedCheckoutSource,
        source_head: ProjectHead | None,
    ) -> None:
        if source.kind == "head":
            if intent != {"kind": "head", "project_id": source.project_id}:
                raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
            if source_head is not None and (
                source_head.project_id != source.project_id
                or source_head.revision_id != source.revision_id
                or source_head.manifest_sha256 != source.manifest_sha256
            ):
                raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
            return
        if intent != {
            "kind": "draft",
            "task_id": source.task_id,
            "draft_id": source.draft_id,
            "expected_generation": source.task_generation,
        }:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        if source_head is not None and source_head.project_id != source.project_id:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)

    def _resolve(self, source):
        try:
            if type(source) is HeadCheckoutSource:
                observation = self._revision_store.observe_model_source(
                    source.project_id,
                    None,
                )
                head = observation.head
                revision = observation.revision
                resolved = self._resolved_revision(revision, kind="head")
                if (
                    resolved.project_id != head.project_id
                    or resolved.revision_id != head.revision_id
                    or resolved.manifest_sha256 != head.manifest_sha256
                ):
                    raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
                return (
                    resolved,
                    observation.model_path,
                    head,
                    observation.model_binding,
                )
            if type(source) is not DraftCheckoutSource:
                raise CheckoutError(CheckoutErrorCode.INVALID_INPUT)
            stored = self._task_store.load(source.task_id)
            if stored.generation != source.expected_generation:
                raise CheckoutError(CheckoutErrorCode.CONFLICT)
            draft = stored.task_run.draft
            if (
                type(draft) is not ReviewDraft
                or draft.id != source.draft_id
                or draft.task_id != source.task_id
                or draft.project_id != stored.task_run.project_id
                or draft.base_revision != stored.task_run.base_revision
                or stored.task_run.review_policy is not ReviewPolicy.REQUIRE_REVIEW
                or stored.task_run.status is not TaskStatus.AWAITING_USER_REVIEW
            ):
                raise CheckoutError(CheckoutErrorCode.CONFLICT)
            source_head = self._head_for_draft(draft)
            source_observation = self._revision_store.observe_model_source(
                draft.project_id,
                draft.revision_id,
            )
            current_head = source_observation.head
            if current_head != source_head:
                raise CheckoutError(CheckoutErrorCode.CONFLICT)
            revision = source_observation.revision
            resolved = self._resolved_revision(
                revision,
                kind="draft",
                task_id=draft.task_id,
                draft_id=draft.id,
                task_generation=stored.generation,
            )
            if (
                resolved.project_id != draft.project_id
                or resolved.revision_id != draft.revision_id
                or resolved.manifest_sha256 != draft.manifest_sha256
                or revision.base_revision != draft.base_revision
            ):
                raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
            observation = self._draft_observation(stored, resolved, source_head)
            final_observation = self._draft_observation(
                self._task_store.load(source.task_id),
                resolved,
                source_head,
            )
            final_head = self._load_validated_head(draft.project_id)
            if (
                type(observation) is CheckoutSourceLiveness
                or type(final_observation) is CheckoutSourceLiveness
                or observation != final_observation
                or final_head != source_head
            ):
                raise CheckoutError(CheckoutErrorCode.CONFLICT)
            return (
                resolved,
                source_observation.model_path,
                source_head,
                source_observation.model_binding,
            )
        except CheckoutError:
            raise
        except RevisionStoreError as error:
            if error.code is RevisionStoreErrorCode.NOT_FOUND:
                raise CheckoutError(CheckoutErrorCode.NOT_FOUND) from None
            if error.code is RevisionStoreErrorCode.BUDGET_EXCEEDED:
                raise CheckoutError(CheckoutErrorCode.RESOURCE_EXHAUSTED) from None
            if error.code in {
                RevisionStoreErrorCode.CORRUPT_RECORD,
                RevisionStoreErrorCode.CORRUPT_CONTENT,
                RevisionStoreErrorCode.UNSAFE_STORE,
            }:
                raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
            if error.code is RevisionStoreErrorCode.CONFLICT:
                raise CheckoutError(CheckoutErrorCode.CONFLICT) from None
            raise CheckoutError(CheckoutErrorCode.IO_ERROR) from None
        except TaskStoreError as error:
            if error.code is TaskStoreErrorCode.NOT_FOUND:
                raise CheckoutError(CheckoutErrorCode.NOT_FOUND) from None
            if error.code in {
                TaskStoreErrorCode.CORRUPT_RECORD,
                TaskStoreErrorCode.UNSAFE_STORE,
            }:
                raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
            if error.code is TaskStoreErrorCode.RECORD_TOO_LARGE:
                raise CheckoutError(CheckoutErrorCode.RESOURCE_EXHAUSTED) from None
            if error.code is TaskStoreErrorCode.CONFLICT:
                raise CheckoutError(CheckoutErrorCode.CONFLICT) from None
            raise CheckoutError(CheckoutErrorCode.IO_ERROR) from None
        except OSError:
            raise CheckoutError(CheckoutErrorCode.IO_ERROR) from None

    @staticmethod
    def _resolved_revision(
        revision: RevisionRef,
        *,
        kind: str,
        task_id: str | None = None,
        draft_id: str | None = None,
        task_generation: int | None = None,
    ) -> ResolvedCheckoutSource:
        if type(revision) is not RevisionRef or revision.model is None:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        if revision.model.size_bytes > MAX_CHECKOUT_FILE_BYTES:
            raise CheckoutError(CheckoutErrorCode.RESOURCE_EXHAUSTED)
        return ResolvedCheckoutSource(
            kind=kind,
            project_id=revision.project_id,
            revision_id=revision.id,
            manifest_sha256=revision.manifest_sha256,
            model_sha256=revision.model.sha256,
            size_bytes=revision.model.size_bytes,
            task_id=task_id,
            draft_id=draft_id,
            task_generation=task_generation,
        )

    def _publish_open(
        self,
        root_fd: int,
        open_key: str,
        intent: dict[str, object],
        resolved: ResolvedCheckoutSource,
        model_path: Path,
        source_head: ProjectHead,
        expected_source_binding: RevisionSourceBinding,
    ) -> CheckoutDescriptor:
        checkout_id = _new_checkout_id()
        temp_name = f".{checkout_id}.tmp"
        temp_fd = -1
        source_fd = -1
        published = False
        try:
            os.mkdir(temp_name, 0o700, dir_fd=root_fd)
            temp_fd, temp_info = self._root.open_directory_at(root_fd, temp_name)
            try:
                source_fd = os.open(
                    model_path,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                )
            except OSError:
                raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
            source_before = os.fstat(source_fd)
            if (
                not stat.S_ISREG(source_before.st_mode)
                or source_before.st_uid != os.geteuid()
                or stat.S_IMODE(source_before.st_mode) != 0o600
                or source_before.st_nlink != 1
                or source_before.st_size != resolved.size_bytes
            ):
                raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
            destination_fd = os.open(
                "model.FCStd",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=temp_fd,
            )
            digest = hashlib.sha256()
            count = 0
            try:
                while True:
                    chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > MAX_CHECKOUT_FILE_BYTES:
                        raise CheckoutError(CheckoutErrorCode.RESOURCE_EXHAUSTED)
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(destination_fd, view)
                        if written <= 0:
                            raise OSError
                        view = view[written:]
                os.fsync(destination_fd)
                self._fault("after_file_fsync")
                destination_info = os.fstat(destination_fd)
            finally:
                os.close(destination_fd)
            source_after = os.fstat(source_fd)
            source_binding = self._binding_from_stat(source_before)
            if (
                source_binding != expected_source_binding
                or source_binding != self._binding_from_stat(source_after)
                or source_binding != self._binding_from_stat(model_path.stat(follow_symlinks=False))
            ):
                raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
            if (
                count != resolved.size_bytes
                or digest.hexdigest() != resolved.model_sha256
                or not self._root.regular_file(destination_info, maximum=MAX_CHECKOUT_FILE_BYTES)
                or destination_info.st_ino == source_before.st_ino
            ):
                raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
            self._fault("after_copy")
            record = _OpenRecord(
                checkout_id=checkout_id,
                open_key=open_key,
                intent=intent,
                source=resolved,
                initial_model_sha256=digest.hexdigest(),
                directory_identity=(temp_info.st_dev, temp_info.st_ino),
                source_binding=source_binding,
                created_ns=time.time_ns(),
                source_head=source_head,
            )
            raw = self._encode_open(record)
            metadata_fd = os.open(
                "metadata.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=temp_fd,
            )
            try:
                view = memoryview(raw)
                while view:
                    written = os.write(metadata_fd, view)
                    if written <= 0:
                        raise OSError
                    view = view[written:]
                os.fsync(metadata_fd)
            finally:
                os.close(metadata_fd)
            self._fault("after_metadata_publish")
            os.fsync(temp_fd)
            self._fault("after_directory_fsync")
            os.rename(temp_name, checkout_id, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            published = True
            os.fsync(root_fd)
            self._fault("after_checkout_publish")
            descriptor = self._get_locked(root_fd, checkout_id)
            return descriptor
        except CheckoutError:
            raise
        except (OSError, StorageFailure) as error:
            if published:
                try:
                    descriptor = self._get_locked(root_fd, checkout_id)
                except (CheckoutError, OSError, StorageFailure):
                    pass
                else:
                    raise CheckoutError(
                        CheckoutErrorCode.DURABILITY_UNCERTAIN,
                        descriptor=descriptor,
                    ) from error
            raise CheckoutError(CheckoutErrorCode.IO_ERROR) from None
        finally:
            if source_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(source_fd)
            if temp_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(temp_fd)
            if not published and self._entry_exists(root_fd, temp_name):
                with contextlib.suppress(OSError, StorageFailure, CheckoutError):
                    self._delete_checkout_directory(root_fd, temp_name, partial=True)
                    os.fsync(root_fd)

    def _inspect_open_locked(
        self,
        root_fd: int,
        checkout_id: str,
    ) -> tuple[
        CheckoutDescriptor,
        _CheckoutEntryBinding,
        _CheckoutEntryBinding,
        _CheckoutEntryBinding,
    ]:
        try:
            root_info = os.fstat(root_fd)
        except OSError:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
        record = self._load_open(root_fd, checkout_id)
        directory_fd, directory_info = self._root.open_directory_at(
            root_fd,
            checkout_id,
            expected_identity=record.directory_identity,
        )
        try:
            digest, size, model_info = self._root.hash_open_file(
                directory_fd,
                "model.FCStd",
                maximum=MAX_CHECKOUT_FILE_BYTES,
            )
            self._root.verify_file_entry(
                directory_fd,
                "model.FCStd",
                expected=model_info,
                maximum=MAX_CHECKOUT_FILE_BYTES,
            )
            self._root.verify_directory_entry(
                root_fd,
                checkout_id,
                expected=directory_info,
            )
            live_root_fd = self._root.open()
            try:
                live_root_info = os.fstat(live_root_fd)
                live_directory_fd, live_directory_info = self._root.open_directory_at(
                    live_root_fd,
                    checkout_id,
                    expected_identity=record.directory_identity,
                )
                try:
                    self._root.verify_file_entry(
                        live_directory_fd,
                        "model.FCStd",
                        expected=model_info,
                        maximum=MAX_CHECKOUT_FILE_BYTES,
                    )
                    self._root.verify_directory_entry(
                        live_root_fd,
                        checkout_id,
                        expected=directory_info,
                    )
                finally:
                    os.close(live_directory_fd)
            finally:
                os.close(live_root_fd)
            root_binding = _entry_binding(root_info)
            live_root_binding = _entry_binding(live_root_info)
            directory_binding = _entry_binding(directory_info)
            live_directory_binding = _entry_binding(live_directory_info)
            file_binding = _entry_binding(model_info)
            if (
                root_binding != live_root_binding
                or directory_binding != live_directory_binding
                or not _private_directory_binding(root_binding)
                or not _private_directory_binding(directory_binding)
                or not _private_file_binding(file_binding)
                or root_binding.dev != directory_binding.dev
                or directory_binding.dev != file_binding.dev
            ):
                raise StorageFailure("managed checkout binding changed")
        except (OSError, StorageFailure):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
        finally:
            os.close(directory_fd)
        descriptor = CheckoutDescriptor(
            checkout_id=record.checkout_id,
            open_key=record.open_key,
            state=CheckoutState.OPEN,
            dirty=digest != record.initial_model_sha256,
            source=record.source,
            initial_model_sha256=record.initial_model_sha256,
            current_model_sha256=digest,
            current_size_bytes=size,
            local_path=self._root.path / checkout_id / "model.FCStd",
            source_head=record.source_head,
            source_binding=record.source_binding,
            source_liveness=CheckoutSourceLiveness.RECOVERY_REQUIRED,
        )
        return (
            self._project_liveness(descriptor),
            live_root_binding,
            live_directory_binding,
            file_binding,
        )

    def _get_locked(self, root_fd: int, checkout_id: str) -> CheckoutDescriptor:
        return self._inspect_open_locked(root_fd, checkout_id)[0]

    def _load_open(self, root_fd: int, checkout_id: str) -> _OpenRecord:
        try:
            directory_fd, directory_info = self._root.open_directory_at(root_fd, checkout_id)
            try:
                raw, metadata_info = self._root.read_file_at(
                    directory_fd,
                    "metadata.json",
                    maximum=_MAX_RECORD_BYTES,
                )
                self._root.verify_file_entry(
                    directory_fd,
                    "metadata.json",
                    expected=metadata_info,
                    maximum=_MAX_RECORD_BYTES,
                )
                self._root.verify_directory_entry(
                    root_fd,
                    checkout_id,
                    expected=directory_info,
                )
            finally:
                os.close(directory_fd)
        except StorageFailure:
            if not self._entry_exists(root_fd, checkout_id):
                raise CheckoutError(CheckoutErrorCode.NOT_FOUND) from None
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
        record = self._decode_open(raw)
        if record.checkout_id != checkout_id or record.directory_identity != (
            directory_info.st_dev,
            directory_info.st_ino,
        ):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        return record

    def _encode_open(self, record: _OpenRecord) -> bytes:
        if record.source_head is None or record.source_binding is None:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        body = {
            "schema_version": _OPEN_SCHEMA_VERSION,
            "checkout_id": record.checkout_id,
            "open_key": record.open_key,
            "intent": record.intent,
            "source": record.source.to_mapping(),
            "source_head": record.source_head.to_mapping(),
            "source_binding": _binding_mapping(record.source_binding),
            "initial_model_sha256": record.initial_model_sha256,
            "directory_device": record.directory_identity[0],
            "directory_inode": record.directory_identity[1],
            "created_ns": record.created_ns,
        }
        return _encode_record(body, _RECORD_DOMAIN_V2)

    def _decode_open(self, raw: bytes) -> _OpenRecord:
        value = _strict_json(raw)
        common = {
            "schema_version",
            "checkout_id",
            "open_key",
            "intent",
            "source",
            "initial_model_sha256",
            "directory_device",
            "directory_inode",
            "created_ns",
            "checksum",
        }
        legacy = common | {
            "source_device",
            "source_inode",
            "source_size",
            "source_mtime_ns",
        }
        current = common | {"source_head", "source_binding"}
        schema = value.get("schema_version")
        if schema == _LEGACY_SCHEMA_VERSION and set(value) == legacy:
            domain = _RECORD_DOMAIN
        elif schema == _OPEN_SCHEMA_VERSION and set(value) == current:
            domain = _RECORD_DOMAIN_V2
        else:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        checksum = value.pop("checksum")
        if type(checksum) is not str or not secrets.compare_digest(
            checksum,
            hashlib.sha256(domain + _canonical(value)).hexdigest(),
        ):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        numbers = (
            value["directory_device"],
            value["directory_inode"],
            value["created_ns"],
        )
        if schema == _LEGACY_SCHEMA_VERSION:
            numbers += (
                value["source_device"],
                value["source_inode"],
                value["source_size"],
                value["source_mtime_ns"],
            )
        if any(type(item) is not int or item < 0 for item in numbers):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        try:
            record = _OpenRecord(
                checkout_id=_identifier(value["checkout_id"], _CHECKOUT_RE),
                open_key=_identifier(value["open_key"], _OPEN_KEY_RE),
                intent=_intent_from_mapping(value["intent"]),
                source=_resolved_from_mapping(value["source"]),
                initial_model_sha256=_digest(value["initial_model_sha256"]),
                directory_identity=(value["directory_device"], value["directory_inode"]),
                source_binding=(
                    None
                    if schema == _LEGACY_SCHEMA_VERSION
                    else _binding_from_mapping(value["source_binding"])
                ),
                created_ns=value["created_ns"],
                source_head=(
                    None
                    if schema == _LEGACY_SCHEMA_VERSION
                    else _head_from_mapping(value["source_head"])
                ),
            )
        except CheckoutError:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
        self._require_record_binding(record.intent, record.source, record.source_head)
        return record

    def _write_tombstone(self, root_fd: int, record: _ClosedRecord) -> None:
        descriptor = record.descriptor
        body = {
            "schema_version": _TOMBSTONE_SCHEMA_VERSION,
            "descriptor": descriptor.to_wire_mapping(),
            "source_head": (
                None if descriptor.source_head is None else descriptor.source_head.to_mapping()
            ),
            "source_binding": (
                None
                if descriptor.source_binding is None
                else _binding_mapping(descriptor.source_binding)
            ),
            "intent": record.intent,
            "closed_ns": record.closed_ns,
        }
        raw = _encode_record(body, _TOMBSTONE_DOMAIN_V2)
        name = f"closed_{descriptor.checkout_id}.json"
        if self._entry_exists(root_fd, name):
            existing = self._load_tombstone_name(root_fd, name)
            if existing != record:
                raise CheckoutError(CheckoutErrorCode.CONFLICT)
            return
        try:
            self._root.atomic_write(root_fd, name, raw, token=secrets.token_hex(16))
        except StorageFailure:
            if self._entry_exists(root_fd, name):
                try:
                    readback = self._load_tombstone_name(root_fd, name)
                except CheckoutError:
                    pass
                else:
                    if readback == record:
                        raise CheckoutError(
                            CheckoutErrorCode.DURABILITY_UNCERTAIN,
                            descriptor=self._project_liveness(record.descriptor),
                        ) from None
            raise CheckoutError(CheckoutErrorCode.IO_ERROR) from None

    def _load_tombstone(
        self,
        root_fd: int,
        checkout_id: str,
        *,
        required: bool,
    ) -> _ClosedRecord | None:
        name = f"closed_{checkout_id}.json"
        if not self._entry_exists(root_fd, name):
            if required:
                raise CheckoutError(CheckoutErrorCode.NOT_FOUND)
            return None
        return self._load_tombstone_name(root_fd, name)

    def _load_tombstone_name(self, root_fd: int, name: str) -> _ClosedRecord:
        try:
            raw, tombstone_info = self._root.read_file_at(
                root_fd,
                name,
                maximum=_MAX_RECORD_BYTES,
            )
            self._root.verify_file_entry(
                root_fd,
                name,
                expected=tombstone_info,
                maximum=_MAX_RECORD_BYTES,
            )
        except StorageFailure:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
        value = _strict_json(raw)
        legacy = {"schema_version", "descriptor", "intent", "closed_ns", "checksum"}
        current = legacy | {"source_head", "source_binding"}
        schema = value.get("schema_version")
        if schema == _LEGACY_SCHEMA_VERSION and set(value) == legacy:
            domain = _TOMBSTONE_DOMAIN
        elif schema == _TOMBSTONE_SCHEMA_VERSION and set(value) == current:
            domain = _TOMBSTONE_DOMAIN_V2
        else:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        checksum = value.pop("checksum")
        if type(checksum) is not str:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        if not secrets.compare_digest(
            checksum,
            hashlib.sha256(domain + _canonical(value)).hexdigest(),
        ):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        descriptor_map = value["descriptor"]
        if type(descriptor_map) is not dict or set(descriptor_map) != {
            "checkout_id",
            "open_key",
            "state",
            "authoritative",
            "dirty",
            "source",
            "initial_model_sha256",
            "current_model_sha256",
            "current_size_bytes",
        }:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        if descriptor_map["state"] != "closed" or descriptor_map["authoritative"] is not False:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        try:
            descriptor = CheckoutDescriptor(
                checkout_id=_identifier(descriptor_map["checkout_id"], _CHECKOUT_RE),
                open_key=_identifier(descriptor_map["open_key"], _OPEN_KEY_RE),
                state=CheckoutState.CLOSED,
                authoritative=False,
                dirty=descriptor_map["dirty"],
                source=_resolved_from_mapping(descriptor_map["source"]),
                initial_model_sha256=_digest(descriptor_map["initial_model_sha256"]),
                current_model_sha256=_digest(descriptor_map["current_model_sha256"]),
                current_size_bytes=_size(descriptor_map["current_size_bytes"]),
                local_path=None,
                source_head=(
                    None
                    if schema == _LEGACY_SCHEMA_VERSION or value["source_head"] is None
                    else _head_from_mapping(value["source_head"])
                ),
                source_binding=(
                    None
                    if schema == _LEGACY_SCHEMA_VERSION or value["source_binding"] is None
                    else _binding_from_mapping(value["source_binding"])
                ),
                source_liveness=CheckoutSourceLiveness.RECOVERY_REQUIRED,
            )
        except CheckoutError:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
        expected_id = name.removeprefix("closed_").removesuffix(".json")
        if descriptor.checkout_id != expected_id:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        if type(value["closed_ns"]) is not int or value["closed_ns"] < 0:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        intent = _intent_from_mapping(value["intent"])
        self._require_record_binding(intent, descriptor.source, descriptor.source_head)
        return _ClosedRecord(
            descriptor,
            intent,
            value["closed_ns"],
        )

    @staticmethod
    def _entry_exists(root_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            raise CheckoutError(CheckoutErrorCode.IO_ERROR) from None
        return True

    def _temp_model_size(self, root_fd: int, name: str) -> int:
        directory_fd, _ = self._root.open_directory_at(root_fd, name)
        try:
            try:
                info = os.stat("model.FCStd", dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return 0
            if not self._root.regular_file(info, maximum=MAX_CHECKOUT_FILE_BYTES):
                raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
            return info.st_size
        finally:
            os.close(directory_fd)

    def _open_model_size(self, root_fd: int, record: _OpenRecord) -> int:
        try:
            directory_fd, _ = self._root.open_directory_at(
                root_fd,
                record.checkout_id,
                expected_identity=record.directory_identity,
            )
            try:
                info = os.stat("model.FCStd", dir_fd=directory_fd, follow_symlinks=False)
            finally:
                os.close(directory_fd)
        except (OSError, StorageFailure):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
        if not self._root.regular_file(info, maximum=MAX_CHECKOUT_FILE_BYTES):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        return info.st_size

    def _delete_checkout_directory(
        self,
        root_fd: int,
        name: str,
        *,
        partial: bool = False,
    ) -> None:
        directory_fd, _ = self._root.open_directory_at(root_fd, name)
        try:
            entries = os.listdir(directory_fd)
            allowed = {"model.FCStd", "metadata.json"}
            if any(entry not in allowed for entry in entries):
                raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
            for entry in entries:
                info = os.stat(entry, dir_fd=directory_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                ):
                    raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
                if not partial and stat.S_IMODE(info.st_mode) != 0o600:
                    raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
                os.unlink(entry, dir_fd=directory_fd)
                self._fault("after_checkout_entry_unlink")
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.rmdir(name, dir_fd=root_fd)
