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
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionRef,
    RevisionStoreError,
    RevisionStoreErrorCode,
)
from vibecad.interaction.storage import CheckoutMutationLock, SafeRoot, StorageFailure
from vibecad.workflow.state import ReviewDraft
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

_SCHEMA_VERSION = 1
_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_RECORD_BYTES = 65_536
_COPY_CHUNK_BYTES = 65_536
_RECORD_DOMAIN = b"vibecad-managed-checkout-open-v1\0"
_TOMBSTONE_DOMAIN = b"vibecad-managed-checkout-closed-v1\0"
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
    authoritative: bool = False

    def __post_init__(self) -> None:
        _identifier(self.checkout_id, _CHECKOUT_RE)
        _identifier(self.open_key, _OPEN_KEY_RE)
        if type(self.state) is not CheckoutState or self.authoritative is not False:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        if type(self.dirty) is not bool or type(self.source) is not ResolvedCheckoutSource:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        _digest(self.initial_model_sha256)
        _digest(self.current_model_sha256)
        _size(self.current_size_bytes)
        if self.state is CheckoutState.OPEN and not isinstance(self.local_path, Path):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        if self.state is CheckoutState.CLOSED and self.local_path is not None:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)

    def to_wire_mapping(self) -> dict[str, object]:
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


@dataclass(frozen=True, slots=True)
class _OpenRecord:
    checkout_id: str
    open_key: str
    intent: dict[str, object]
    source: ResolvedCheckoutSource
    initial_model_sha256: str
    directory_identity: tuple[int, int]
    source_identity: tuple[int, int, int, int]
    created_ns: int


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
                            return closed_descriptor
                        self._require_aggregate_budget(inventory)
                        return self._get_locked(root_fd, checkout_id)
                    resolved, model_path = self._resolve(source)
                    self._admit(inventory, resolved.size_bytes)
                    return self._publish_open(
                        root_fd,
                        canonical_key,
                        intent,
                        resolved,
                        model_path,
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
                        return closed.descriptor
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
                        if self._entry_exists(root_fd, canonical_id):
                            try:
                                self._delete_checkout_directory(root_fd, canonical_id)
                                os.fsync(root_fd)
                            except (CheckoutError, OSError, StorageFailure):
                                raise CheckoutError(
                                    CheckoutErrorCode.CLEANUP_REQUIRED,
                                    descriptor=closed.descriptor,
                                ) from None
                        return closed.descriptor
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
                            descriptor=tombstone.descriptor,
                        ) from None
                    return tombstone.descriptor
                finally:
                    os.close(root_fd)
        except CheckoutError:
            raise
        except StorageFailure:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
        except OSError:
            raise CheckoutError(CheckoutErrorCode.IO_ERROR) from None

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
                            descriptor=tombstone.descriptor,
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

    def _resolve(self, source):
        try:
            if type(source) is HeadCheckoutSource:
                head = self._revision_store.load_head(source.project_id)
                revision = self._revision_store.load_revision(source.project_id, head.revision_id)
                resolved = self._resolved_revision(revision, kind="head")
                if (
                    resolved.project_id != head.project_id
                    or resolved.revision_id != head.revision_id
                    or resolved.manifest_sha256 != head.manifest_sha256
                ):
                    raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
                return resolved, self._revision_store.revision_model_path(
                    source.project_id, head.revision_id
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
            ):
                raise CheckoutError(CheckoutErrorCode.CONFLICT)
            revision = self._revision_store.load_revision(draft.project_id, draft.revision_id)
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
            return resolved, self._revision_store.revision_model_path(
                draft.project_id, draft.revision_id
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
            source_identity = (
                source_before.st_dev,
                source_before.st_ino,
                source_before.st_size,
                source_before.st_mtime_ns,
            )
            if source_identity != (
                source_after.st_dev,
                source_after.st_ino,
                source_after.st_size,
                source_after.st_mtime_ns,
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
                source_identity=source_identity,
                created_ns=time.time_ns(),
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

    def _get_locked(self, root_fd: int, checkout_id: str) -> CheckoutDescriptor:
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
                live_directory_fd, _ = self._root.open_directory_at(
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
        except StorageFailure:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
        finally:
            os.close(directory_fd)
        return CheckoutDescriptor(
            checkout_id=record.checkout_id,
            open_key=record.open_key,
            state=CheckoutState.OPEN,
            dirty=digest != record.initial_model_sha256,
            source=record.source,
            initial_model_sha256=record.initial_model_sha256,
            current_model_sha256=digest,
            current_size_bytes=size,
            local_path=self._root.path / checkout_id / "model.FCStd",
        )

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
        body = {
            "schema_version": _SCHEMA_VERSION,
            "checkout_id": record.checkout_id,
            "open_key": record.open_key,
            "intent": record.intent,
            "source": record.source.to_mapping(),
            "initial_model_sha256": record.initial_model_sha256,
            "directory_device": record.directory_identity[0],
            "directory_inode": record.directory_identity[1],
            "source_device": record.source_identity[0],
            "source_inode": record.source_identity[1],
            "source_size": record.source_identity[2],
            "source_mtime_ns": record.source_identity[3],
            "created_ns": record.created_ns,
        }
        return _encode_record(body, _RECORD_DOMAIN)

    def _decode_open(self, raw: bytes) -> _OpenRecord:
        value = _strict_json(raw)
        expected = {
            "schema_version",
            "checkout_id",
            "open_key",
            "intent",
            "source",
            "initial_model_sha256",
            "directory_device",
            "directory_inode",
            "source_device",
            "source_inode",
            "source_size",
            "source_mtime_ns",
            "created_ns",
            "checksum",
        }
        if set(value) != expected or value["schema_version"] != _SCHEMA_VERSION:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        checksum = value.pop("checksum")
        if type(checksum) is not str or not secrets.compare_digest(
            checksum,
            hashlib.sha256(_RECORD_DOMAIN + _canonical(value)).hexdigest(),
        ):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        numbers = (
            value["directory_device"],
            value["directory_inode"],
            value["source_device"],
            value["source_inode"],
            value["source_size"],
            value["source_mtime_ns"],
            value["created_ns"],
        )
        if any(type(item) is not int or item < 0 for item in numbers):
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        try:
            return _OpenRecord(
                checkout_id=_identifier(value["checkout_id"], _CHECKOUT_RE),
                open_key=_identifier(value["open_key"], _OPEN_KEY_RE),
                intent=_intent_from_mapping(value["intent"]),
                source=_resolved_from_mapping(value["source"]),
                initial_model_sha256=_digest(value["initial_model_sha256"]),
                directory_identity=(value["directory_device"], value["directory_inode"]),
                source_identity=(
                    value["source_device"],
                    value["source_inode"],
                    value["source_size"],
                    value["source_mtime_ns"],
                ),
                created_ns=value["created_ns"],
            )
        except CheckoutError:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None

    def _write_tombstone(self, root_fd: int, record: _ClosedRecord) -> None:
        descriptor = record.descriptor
        body = {
            "schema_version": _SCHEMA_VERSION,
            "descriptor": descriptor.to_wire_mapping(),
            "intent": record.intent,
            "closed_ns": record.closed_ns,
        }
        raw = _encode_record(body, _TOMBSTONE_DOMAIN)
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
                            descriptor=record.descriptor,
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
        if set(value) != {"schema_version", "descriptor", "intent", "closed_ns", "checksum"}:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        checksum = value.pop("checksum")
        if value["schema_version"] != _SCHEMA_VERSION or type(checksum) is not str:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        if not secrets.compare_digest(
            checksum,
            hashlib.sha256(_TOMBSTONE_DOMAIN + _canonical(value)).hexdigest(),
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
            )
        except CheckoutError:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE) from None
        expected_id = name.removeprefix("closed_").removesuffix(".json")
        if descriptor.checkout_id != expected_id:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        if type(value["closed_ns"]) is not int or value["closed_ns"] < 0:
            raise CheckoutError(CheckoutErrorCode.INTEGRITY_FAILURE)
        return _ClosedRecord(
            descriptor,
            _intent_from_mapping(value["intent"]),
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
