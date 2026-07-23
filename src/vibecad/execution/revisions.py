"""Immutable local CAD revision persistence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import secrets
import signal
import stat
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER
from vibecad.workflow.lease import (
    LeaseError,
    LeaseErrorCode,
    ProjectWriteLease,
    ResourceLeaseManager,
)

__all__ = (
    "CommitJournal",
    "CommitJournalState",
    "LocalRevisionStore",
    "ProjectHead",
    "ProjectSnapshotEntry",
    "ReconciliationResult",
    "ReconciliationStatus",
    "RevisionAncestrySnapshot",
    "RevisionArtifactRef",
    "RevisionCopyCursor",
    "RevisionRef",
    "RevisionSnapshotEntry",
    "RevisionSourceBinding",
    "RevisionSourceObservation",
    "RevisionStoreError",
    "RevisionStoreErrorCode",
    "RevisionStoreRootTrust",
)

_SCHEMA_VERSION = 1
_MAX_HEAD_BYTES = 16384
_MAX_JOURNAL_BYTES = 32768
_MAX_MANIFEST_BYTES = 262144
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 4096
_MAX_JSON_STRING_BYTES = 4096
_MAX_FILE_BYTES = 536870912
_MAX_REVISION_BYTES = 1073741824
_MAX_STORE_BYTES = 17179869184
_MAX_PROJECTS = 4096
_MAX_REVISIONS = 8192
_MAX_CANDIDATES_AND_RESERVATIONS = 1024
_MAX_ORDINARY_FILES = 65536
_MAX_CANDIDATE_FILE_BYTES = 536870912
_GENERATION_ZERO_RESERVATION_BYTES = 1074790400
_CANDIDATE_RESERVATION_BYTES = 2151677952
_COPY_CHUNK_BYTES = 65536
_MAX_RECORD_OPEN_ATTEMPTS = 3

_PROJECT_PATH_DOMAIN = b"vibecad-revision-project-path-v1\0"
_REVISION_PATH_DOMAIN = b"vibecad-revision-content-path-v1\0"
_CANDIDATE_PATH_DOMAIN = b"vibecad-revision-candidate-path-v1\0"
_MANIFEST_CHECKSUM_DOMAIN = b"vibecad-revision-manifest-v1\0"
_HEAD_CHECKSUM_DOMAIN = b"vibecad-project-head-v1\0"
_JOURNAL_CHECKSUM_DOMAIN = b"vibecad-commit-journal-v1\0"
_RESERVATION_CHECKSUM_DOMAIN = b"vibecad-revision-reservation-v1\0"
_RESERVATION_KEY_DOMAIN = b"vibecad-revision-reservation-key-v1\0"
_SEED_INTENT_CHECKSUM_DOMAIN = b"vibecad-revision-seed-intent-v1\0"
_SEED_BINDING_CHECKSUM_DOMAIN = b"vibecad-revision-seed-binding-v1\0"
_QUOTA_RESOURCE_ID = "vibecad-revision-quota-v1"
_QUOTA_DIRECTORY = ".revision-quota"
_RESERVATIONS_DIRECTORY = "reservations"
_RESERVATION_RECORD = "reservation.json"
_SEED_INTENT_RECORD = "seed-intent.json"
_SEED_BINDING_RECORD = "seed-binding.json"
_QUOTA_OWNER_CONFLICT = ("conflicting_reservation_owner",)
_CAD_FILE_LIMIT_RESOURCE = "vibecad-candidate-file-limit-v1"
_DISCOVERY_NAMESPACE_DOMAIN = b"vibecad-revision-discovery-namespace-v1\0"
_DISCOVERY_PROJECT_STATE_DOMAIN = b"vibecad-revision-project-state-v1\0"

_PROJECT_PATTERN = r"project_[0-9a-f]{32}"
_REVISION_PATTERN = r"revision_[0-9a-f]{32}"
_ARTIFACT_PATTERN = r"artifact_[0-9a-f]{32}"
_TRANSACTION_PATTERN = r"transaction_[0-9a-f]{32}"
_DIGEST_PATTERN = r"[0-9a-f]{64}"
_ARTIFACT_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}"
_SOURCE_NAME_PATTERN = r"[A-Za-z0-9._-]{1,255}"


class RevisionStoreRootTrust(StrEnum):
    TRUSTED_LOCAL = "trusted_local"


class CommitJournalState(StrEnum):
    STAGING = "staging"
    PREPARED = "prepared"
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"


class ReconciliationStatus(StrEnum):
    CLEAN = "clean"
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    CLEANUP_REQUIRED = "cleanup_required"


class RevisionStoreErrorCode(StrEnum):
    INVALID_IDENTIFIER = "invalid_identifier"
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    CONFLICT = "conflict"
    CORRUPT_RECORD = "corrupt_record"
    CORRUPT_CONTENT = "corrupt_content"
    BUDGET_EXCEEDED = "budget_exceeded"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    UNSAFE_STORE = "unsafe_store"
    INVALID_LEASE = "invalid_lease"
    IO_ERROR = "io_error"
    DURABILITY_UNCERTAIN = "durability_uncertain"
    RECOVERY_REQUIRED = "recovery_required"
    CLEANUP_REQUIRED = "cleanup_required"


class RevisionStoreError(ValueError):
    __slots__ = ("code", "message", "head_committed")

    def __init__(self, code, head_committed=None):
        if type(code) is not RevisionStoreErrorCode:
            raise TypeError("code must be a RevisionStoreErrorCode")
        if code is RevisionStoreErrorCode.DURABILITY_UNCERTAIN:
            if type(head_committed) is not bool:
                raise ValueError("head_committed must be a bool")
        elif head_committed is not None:
            raise ValueError("metadata is only valid for durability uncertainty")
        message = _error_message(code)
        self.code = code
        self.message = message
        if code is RevisionStoreErrorCode.DURABILITY_UNCERTAIN:
            self.head_committed = head_committed
        self.args = (message,)


def _install_candidate_file_signal_policy():
    try:
        current = signal.getsignal(signal.SIGXFSZ)
        if current is not signal.SIG_IGN:
            if threading.current_thread() is not threading.main_thread():
                return False
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        return signal.getsignal(signal.SIGXFSZ) is signal.SIG_IGN
    except (OSError, RuntimeError, ValueError):
        return False


class _CandidateFileLimitRuntime:
    __slots__ = ()

    _initialized_pid = None
    _poisoned_pid = None
    _gate = None


def _initialize_candidate_file_limit_runtime():
    pid = os.getpid()
    if _CandidateFileLimitRuntime._poisoned_pid == pid:
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    if _CandidateFileLimitRuntime._initialized_pid == pid:
        return
    if threading.current_thread() is not threading.main_thread():
        raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
    if not _install_candidate_file_signal_policy():
        raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
    _CandidateFileLimitRuntime._gate = threading.RLock()
    _CandidateFileLimitRuntime._initialized_pid = pid


class _CandidateFileLimit:
    __slots__ = ("_active", "_gate", "_previous", "_store")

    def __init__(self, store):
        self._store = store
        self._gate = None
        self._previous = None
        self._active = False

    def __enter__(self):
        pid = os.getpid()
        if _CandidateFileLimitRuntime._poisoned_pid == pid:
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        if _CandidateFileLimitRuntime._initialized_pid != pid:
            raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
        if not _install_candidate_file_signal_policy():
            raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
        if os.getpid() != self._store._pid:
            raise RevisionStoreError(RevisionStoreErrorCode.INVALID_LEASE)
        gate = _CandidateFileLimitRuntime._gate
        failure_code = None
        acquired = False
        try:
            if gate is None or not gate.acquire(timeout=5.0):
                raise RuntimeError("candidate file limit gate unavailable")
            acquired = True
            if _CandidateFileLimitRuntime._poisoned_pid == pid:
                raise RuntimeError("candidate file limit runtime is poisoned")
            previous = resource.getrlimit(resource.RLIMIT_FSIZE)
            previous_soft = previous[0]
            hard = previous[1]
            effective = _MAX_CANDIDATE_FILE_BYTES
            if previous_soft != resource.RLIM_INFINITY:
                effective = min(previous_soft, _MAX_CANDIDATE_FILE_BYTES)
            if hard != resource.RLIM_INFINITY and effective > hard:
                raise ValueError("invalid file-size limit")
            resource.setrlimit(resource.RLIMIT_FSIZE, (effective, hard))
        except (OSError, RuntimeError, ValueError):
            release_failed = False
            if acquired:
                try:
                    gate.release()
                except RuntimeError:
                    release_failed = True
            if release_failed:
                _CandidateFileLimitRuntime._poisoned_pid = pid
                failure_code = RevisionStoreErrorCode.RECOVERY_REQUIRED
            elif _CandidateFileLimitRuntime._poisoned_pid == pid:
                failure_code = RevisionStoreErrorCode.RECOVERY_REQUIRED
            else:
                failure_code = RevisionStoreErrorCode.IO_ERROR
        if failure_code is not None:
            raise RevisionStoreError(failure_code)
        self._gate = gate
        self._previous = previous
        self._active = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        failed = False
        try:
            if self._active and self._previous is not None:
                resource.setrlimit(resource.RLIMIT_FSIZE, self._previous)
        except (OSError, ValueError):
            failed = True
        if failed:
            _CandidateFileLimitRuntime._poisoned_pid = os.getpid()
        try:
            if self._gate is not None:
                self._gate.release()
        except RuntimeError:
            failed = True
            _CandidateFileLimitRuntime._poisoned_pid = os.getpid()
        self._active = False
        if failed:
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        return False


def _candidate_file_limit(store):
    return _CandidateFileLimit(store)


@dataclass(frozen=True, kw_only=True, slots=True)
class RevisionCopyCursor:
    """Exact verified destination prefix for one revision artifact."""

    name: str
    size_bytes: int
    sha256: str

    def __post_init__(self):
        if type(self.name) is not str:
            raise TypeError("cursor name must be an exact string")
        if type(self.size_bytes) is not int:
            raise TypeError("cursor size must be an exact integer")
        if type(self.sha256) is not str:
            raise TypeError("cursor digest must be an exact string")
        if (
            re.fullmatch(_ARTIFACT_NAME_PATTERN, self.name) is None
            or self.size_bytes < 0
            or self.size_bytes > _MAX_FILE_BYTES
            or re.fullmatch(_DIGEST_PATTERN, self.sha256) is None
        ):
            raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)


@dataclass(frozen=True, kw_only=True, slots=True)
class RevisionSourceBinding:
    """Exact descriptor-relative identity for one trusted import source."""

    dev: int
    ino: int
    mode: int
    uid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int

    def __post_init__(self):
        if (
            type(self.dev) is not int
            or type(self.ino) is not int
            or type(self.mode) is not int
            or type(self.uid) is not int
            or type(self.nlink) is not int
            or type(self.size) is not int
            or type(self.mtime_ns) is not int
            or type(self.ctime_ns) is not int
        ):
            raise TypeError("source binding fields must be exact integers")
        if (
            self.dev < 0
            or self.ino < 0
            or self.mode < 0
            or not stat.S_ISREG(self.mode)
            or stat.S_IMODE(self.mode) != 384
            or self.uid != os.geteuid()
            or self.nlink != 1
            or self.size <= 0
            or self.mtime_ns < 0
            or self.ctime_ns < 0
        ):
            raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)


@dataclass(frozen=True, kw_only=True, slots=True)
class RevisionSourceObservation:
    """One coherent, descriptor-verified model source observation."""

    head: ProjectHead
    revision: RevisionRef
    model_path: Path
    model_binding: RevisionSourceBinding

    def __post_init__(self):
        if (
            type(self.head) is not ProjectHead
            or type(self.revision) is not RevisionRef
            or type(self.model_path) is not type(Path("/"))
            or type(self.model_binding) is not RevisionSourceBinding
        ):
            raise TypeError("source observation fields have invalid types")
        if (
            self.revision.model is None
            or self.head.project_id != self.revision.project_id
            or self.model_path.name != self.revision.model.name
            or self.model_binding.size != self.revision.model.size_bytes
        ):
            raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)


@dataclass(frozen=True, kw_only=True, slots=True)
class RevisionArtifactRef:
    schema_version: int = 1
    id: str
    name: str
    format: str
    sha256: str
    size_bytes: int

    def __post_init__(self):
        code = _validate_artifact(self)
        if code is not None:
            raise RevisionStoreError(code)

    @staticmethod
    def from_mapping(mapping):
        return _artifact_from_mapping(mapping)

    def to_mapping(self):
        return _artifact_mapping(self)


@dataclass(frozen=True, kw_only=True, slots=True)
class RevisionRef:
    schema_version: int = 1
    id: str
    project_id: str
    base_revision: str | None
    manifest_sha256: str
    model: RevisionArtifactRef | None
    artifacts: object

    def __post_init__(self):
        code = _validate_revision(self)
        if code is not None:
            raise RevisionStoreError(code)

    @staticmethod
    def from_mapping(mapping):
        return _revision_from_mapping(mapping)

    def to_mapping(self):
        return _revision_mapping(self)


@dataclass(frozen=True, kw_only=True, slots=True)
class ProjectHead:
    schema_version: int = 1
    project_id: str
    generation: int
    revision_id: str
    manifest_sha256: str

    def __post_init__(self):
        code = _validate_head(self)
        if code is not None:
            raise RevisionStoreError(code)

    @staticmethod
    def from_mapping(mapping):
        return _head_from_mapping(mapping)

    def to_mapping(self):
        return _head_mapping(self)


@dataclass(frozen=True, kw_only=True, slots=True)
class ProjectSnapshotEntry:
    """One validated project HEAD plus its complete committed-state digest."""

    project_id: str
    generation: int
    revision_id: str
    manifest_sha256: str
    state_sha256: str

    def __post_init__(self):
        if (
            type(self.project_id) is not str
            or re.fullmatch(_PROJECT_PATTERN, self.project_id) is None
            or type(self.generation) is not int
            or self.generation < 0
            or self.generation > MAX_SAFE_JSON_INTEGER
            or type(self.revision_id) is not str
            or re.fullmatch(_REVISION_PATTERN, self.revision_id) is None
            or type(self.manifest_sha256) is not str
            or re.fullmatch(_DIGEST_PATTERN, self.manifest_sha256) is None
            or type(self.state_sha256) is not str
            or re.fullmatch(_DIGEST_PATTERN, self.state_sha256) is None
        ):
            raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)


@dataclass(frozen=True, kw_only=True, slots=True)
class RevisionSnapshotEntry:
    """Path-free metadata for one committed revision in current HEAD ancestry."""

    id: str
    project_id: str
    base_revision: str | None
    manifest_sha256: str

    def __post_init__(self):
        if (
            type(self.id) is not str
            or re.fullmatch(_REVISION_PATTERN, self.id) is None
            or type(self.project_id) is not str
            or re.fullmatch(_PROJECT_PATTERN, self.project_id) is None
            or type(self.manifest_sha256) is not str
            or re.fullmatch(_DIGEST_PATTERN, self.manifest_sha256) is None
        ):
            raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
        if self.base_revision is not None and (
            type(self.base_revision) is not str
            or re.fullmatch(_REVISION_PATTERN, self.base_revision) is None
        ):
            raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)


@dataclass(frozen=True, kw_only=True, slots=True)
class RevisionAncestrySnapshot:
    """Validated current HEAD ancestry and digest of all sealed project state."""

    project_id: str
    head: ProjectHead
    revisions: tuple[RevisionSnapshotEntry, ...]
    state_sha256: str

    def __post_init__(self):
        revisions_value = self.revisions
        if type(revisions_value) is not type(()):
            raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
        if (
            type(self.project_id) is not str
            or re.fullmatch(_PROJECT_PATTERN, self.project_id) is None
            or type(self.head) is not ProjectHead
            or self.head.project_id != self.project_id
            or type(self.state_sha256) is not str
            or re.fullmatch(_DIGEST_PATTERN, self.state_sha256) is None
        ):
            raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
        previous = None
        identifiers = {}
        for entry in revisions_value:
            if (
                type(entry) is not RevisionSnapshotEntry
                or entry.project_id != self.project_id
                or entry.id in identifiers
                or (previous is not None and entry.id <= previous)
            ):
                raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
            identifiers[entry.id] = True
            previous = entry.id
        if self.head.revision_id not in identifiers:
            raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)


@dataclass(frozen=True, kw_only=True, slots=True)
class CommitJournal:
    schema_version: int = 1
    id: str
    project_id: str
    expected_head: ProjectHead
    candidate_revision: str
    manifest_sha256: str | None
    state: CommitJournalState

    def __post_init__(self):
        code = _validate_journal(self)
        if code is not None:
            raise RevisionStoreError(code)

    @staticmethod
    def from_mapping(mapping):
        return _journal_from_mapping(mapping)

    def to_mapping(self):
        return _journal_mapping(self)


@dataclass(frozen=True, kw_only=True, slots=True)
class ReconciliationResult:
    schema_version: int = 1
    project_id: str
    status: ReconciliationStatus
    head: ProjectHead
    journal: CommitJournal | None

    def __post_init__(self):
        code = _validate_reconciliation(self)
        if code is not None:
            raise RevisionStoreError(code)

    @staticmethod
    def from_mapping(mapping):
        return _reconciliation_from_mapping(mapping)

    def to_mapping(self):
        return _reconciliation_mapping(self)


def _error_message(code):
    if code is RevisionStoreErrorCode.INVALID_IDENTIFIER:
        return "The identifier is invalid."
    if code is RevisionStoreErrorCode.INVALID_INPUT:
        return "The revision input is invalid."
    if code is RevisionStoreErrorCode.NOT_FOUND:
        return "The revision resource was not found."
    if code is RevisionStoreErrorCode.ALREADY_EXISTS:
        return "The project already exists."
    if code is RevisionStoreErrorCode.CONFLICT:
        return "The revision operation conflicts with durable state."
    if code is RevisionStoreErrorCode.CORRUPT_RECORD:
        return "The revision record is corrupt."
    if code is RevisionStoreErrorCode.CORRUPT_CONTENT:
        return "The revision content is corrupt."
    if code is RevisionStoreErrorCode.BUDGET_EXCEEDED:
        return "The revision storage budget was exceeded."
    if code is RevisionStoreErrorCode.RESOURCE_EXHAUSTED:
        return "Revision storage capacity is exhausted."
    if code is RevisionStoreErrorCode.UNSAFE_STORE:
        return "The revision store is unsafe."
    if code is RevisionStoreErrorCode.INVALID_LEASE:
        return "The project write lease is invalid."
    if code is RevisionStoreErrorCode.IO_ERROR:
        return "The revision operation failed because of an I/O error."
    if code is RevisionStoreErrorCode.DURABILITY_UNCERTAIN:
        return "Revision durability is uncertain."
    if code is RevisionStoreErrorCode.RECOVERY_REQUIRED:
        return "The revision store requires explicit recovery."
    return "Revision cleanup is required."


def _identifier_code(value, pattern):
    if type(value) is not str:
        return RevisionStoreErrorCode.INVALID_IDENTIFIER
    if re.fullmatch(pattern, value) is None:
        return RevisionStoreErrorCode.INVALID_IDENTIFIER
    return None


def _digest_code(value):
    if type(value) is not str:
        return RevisionStoreErrorCode.INVALID_INPUT
    if re.fullmatch(_DIGEST_PATTERN, value) is None:
        return RevisionStoreErrorCode.INVALID_INPUT
    return None


def _validate_schema(value):
    if type(value) is not int or value != _SCHEMA_VERSION:
        return RevisionStoreErrorCode.INVALID_INPUT
    return None


def _validate_artifact(value):
    if _validate_schema(value.schema_version) is not None:
        return RevisionStoreErrorCode.INVALID_INPUT
    code = _identifier_code(value.id, _ARTIFACT_PATTERN)
    if code is not None:
        return code
    if type(value.name) is not str or type(value.format) is not str:
        return RevisionStoreErrorCode.INVALID_INPUT
    if re.fullmatch(_ARTIFACT_NAME_PATTERN, value.name) is None:
        return RevisionStoreErrorCode.INVALID_INPUT
    if value.format != "fcstd" and value.format != "step":
        return RevisionStoreErrorCode.INVALID_INPUT
    if _digest_code(value.sha256) is not None:
        return RevisionStoreErrorCode.INVALID_INPUT
    if type(value.size_bytes) is not int or value.size_bytes <= 0:
        return RevisionStoreErrorCode.INVALID_INPUT
    if value.size_bytes > MAX_SAFE_JSON_INTEGER or value.size_bytes > _MAX_FILE_BYTES:
        return RevisionStoreErrorCode.BUDGET_EXCEEDED
    return None


def _validate_revision(value):
    if _validate_schema(value.schema_version) is not None:
        return RevisionStoreErrorCode.INVALID_INPUT
    code = _identifier_code(value.id, _REVISION_PATTERN)
    if code is not None:
        return code
    code = _identifier_code(value.project_id, _PROJECT_PATTERN)
    if code is not None:
        return code
    if value.base_revision is not None:
        code = _identifier_code(value.base_revision, _REVISION_PATTERN)
        if code is not None:
            return code
        if value.base_revision == value.id:
            return RevisionStoreErrorCode.INVALID_INPUT
    if _digest_code(value.manifest_sha256) is not None:
        return RevisionStoreErrorCode.INVALID_INPUT
    if value.model is not None and type(value.model) is not RevisionArtifactRef:
        return RevisionStoreErrorCode.INVALID_INPUT
    artifacts_value = value.artifacts
    if type(artifacts_value) is not type(()):
        return RevisionStoreErrorCode.INVALID_INPUT
    artifact_count = 0
    if type(artifacts_value) is not type(()):
        return RevisionStoreErrorCode.INVALID_INPUT
    for artifact_item in artifacts_value:
        artifact_count += 1
        if type(artifact_item) is not RevisionArtifactRef:
            return RevisionStoreErrorCode.INVALID_INPUT
    if value.base_revision is None:
        if artifact_count != 0:
            return RevisionStoreErrorCode.INVALID_INPUT
        if value.model is not None:
            if value.model.name != "model.FCStd" or value.model.format != "fcstd":
                return RevisionStoreErrorCode.INVALID_INPUT
        return None
    if value.model is None:
        return RevisionStoreErrorCode.INVALID_INPUT
    if value.model.name != "model.FCStd" or value.model.format != "fcstd":
        return RevisionStoreErrorCode.INVALID_INPUT
    if artifact_count != 1:
        return RevisionStoreErrorCode.INVALID_INPUT
    step_item = artifacts_value[0]
    if step_item.name != "model.step" or step_item.format != "step":
        return RevisionStoreErrorCode.INVALID_INPUT
    if step_item.id == value.model.id:
        return RevisionStoreErrorCode.INVALID_INPUT
    if value.model.size_bytes + step_item.size_bytes > _MAX_REVISION_BYTES:
        return RevisionStoreErrorCode.BUDGET_EXCEEDED
    return None


def _validate_head(value):
    if _validate_schema(value.schema_version) is not None:
        return RevisionStoreErrorCode.INVALID_INPUT
    code = _identifier_code(value.project_id, _PROJECT_PATTERN)
    if code is not None:
        return code
    code = _identifier_code(value.revision_id, _REVISION_PATTERN)
    if code is not None:
        return code
    if type(value.generation) is not int or value.generation < 0:
        return RevisionStoreErrorCode.INVALID_INPUT
    if value.generation > MAX_SAFE_JSON_INTEGER:
        return RevisionStoreErrorCode.INVALID_INPUT
    if _digest_code(value.manifest_sha256) is not None:
        return RevisionStoreErrorCode.INVALID_INPUT
    return None


def _validate_journal(value):
    if _validate_schema(value.schema_version) is not None:
        return RevisionStoreErrorCode.INVALID_INPUT
    code = _identifier_code(value.id, _TRANSACTION_PATTERN)
    if code is not None:
        return code
    code = _identifier_code(value.project_id, _PROJECT_PATTERN)
    if code is not None:
        return code
    if type(value.expected_head) is not ProjectHead:
        return RevisionStoreErrorCode.INVALID_INPUT
    if value.expected_head.project_id != value.project_id:
        return RevisionStoreErrorCode.INVALID_INPUT
    code = _identifier_code(value.candidate_revision, _REVISION_PATTERN)
    if code is not None:
        return code
    if value.candidate_revision == value.expected_head.revision_id:
        return RevisionStoreErrorCode.INVALID_INPUT
    if type(value.state) is not CommitJournalState:
        return RevisionStoreErrorCode.INVALID_INPUT
    if value.state is CommitJournalState.STAGING:
        if value.manifest_sha256 is not None:
            return RevisionStoreErrorCode.INVALID_INPUT
    else:
        if _digest_code(value.manifest_sha256) is not None:
            return RevisionStoreErrorCode.INVALID_INPUT
    return None


def _validate_reconciliation(value):
    if _validate_schema(value.schema_version) is not None:
        return RevisionStoreErrorCode.INVALID_INPUT
    code = _identifier_code(value.project_id, _PROJECT_PATTERN)
    if code is not None:
        return code
    if type(value.status) is not ReconciliationStatus:
        return RevisionStoreErrorCode.INVALID_INPUT
    if type(value.head) is not ProjectHead or value.head.project_id != value.project_id:
        return RevisionStoreErrorCode.INVALID_INPUT
    if value.status is ReconciliationStatus.CLEAN:
        if value.journal is not None:
            return RevisionStoreErrorCode.INVALID_INPUT
        return None
    if type(value.journal) is not CommitJournal:
        return RevisionStoreErrorCode.INVALID_INPUT
    if value.journal.project_id != value.project_id:
        return RevisionStoreErrorCode.INVALID_INPUT
    if value.status is ReconciliationStatus.COMMITTED:
        if value.journal.state is not CommitJournalState.COMMITTED:
            return RevisionStoreErrorCode.INVALID_INPUT
        if value.head.generation != value.journal.expected_head.generation + 1:
            return RevisionStoreErrorCode.INVALID_INPUT
        if value.head.revision_id != value.journal.candidate_revision:
            return RevisionStoreErrorCode.INVALID_INPUT
        if value.head.manifest_sha256 != value.journal.manifest_sha256:
            return RevisionStoreErrorCode.INVALID_INPUT
        return None
    if value.journal.state is not CommitJournalState.NOT_COMMITTED:
        return RevisionStoreErrorCode.INVALID_INPUT
    if value.head != value.journal.expected_head:
        return RevisionStoreErrorCode.INVALID_INPUT
    return None


def _mapping_has_exact(mapping, expected):
    if type(mapping) is not dict:
        return False
    mapping_count = 0
    if type(mapping) is not dict:
        return False
    for mapping_key in mapping:
        mapping_count += 1
        if type(mapping_key) is not str or mapping_key not in expected:
            return False
    expected_count = 0
    if type(expected) is not type(()):
        return False
    for _expected_key in expected:
        expected_count += 1
    return mapping_count == expected_count


def _artifact_mapping(value):
    return {
        "schema_version": value.schema_version,
        "id": value.id,
        "name": value.name,
        "format": value.format,
        "sha256": value.sha256,
        "size_bytes": value.size_bytes,
    }


def _artifact_from_mapping(mapping):
    expected = (
        "schema_version",
        "id",
        "name",
        "format",
        "sha256",
        "size_bytes",
    )
    if not _mapping_has_exact(mapping, expected):
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    return RevisionArtifactRef(
        schema_version=mapping["schema_version"],
        id=mapping["id"],
        name=mapping["name"],
        format=mapping["format"],
        sha256=mapping["sha256"],
        size_bytes=mapping["size_bytes"],
    )


def _artifact_list_mapping(values):
    result = []
    if type(values) is not type(()):
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    for artifact_value in values:
        result = result + [_artifact_mapping(artifact_value)]
    return result


def _artifact_tuple_from_list(values):
    if type(values) is not list:
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    result = ()
    if type(values) is not list:
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    for artifact_mapping_value in values:
        result = result + (_artifact_from_mapping(artifact_mapping_value),)
    return result


def _revision_mapping(value):
    model_mapping = None
    if value.model is not None:
        model_mapping = _artifact_mapping(value.model)
    return {
        "schema_version": value.schema_version,
        "id": value.id,
        "project_id": value.project_id,
        "base_revision": value.base_revision,
        "manifest_sha256": value.manifest_sha256,
        "model": model_mapping,
        "artifacts": _artifact_list_mapping(value.artifacts),
    }


def _revision_from_mapping(mapping):
    expected = (
        "schema_version",
        "id",
        "project_id",
        "base_revision",
        "manifest_sha256",
        "model",
        "artifacts",
    )
    if not _mapping_has_exact(mapping, expected):
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    model_value = None
    if mapping["model"] is not None:
        model_value = _artifact_from_mapping(mapping["model"])
    artifact_values = _artifact_tuple_from_list(mapping["artifacts"])
    return RevisionRef(
        schema_version=mapping["schema_version"],
        id=mapping["id"],
        project_id=mapping["project_id"],
        base_revision=mapping["base_revision"],
        manifest_sha256=mapping["manifest_sha256"],
        model=model_value,
        artifacts=artifact_values,
    )


def _head_mapping(value):
    return {
        "schema_version": value.schema_version,
        "project_id": value.project_id,
        "generation": value.generation,
        "revision_id": value.revision_id,
        "manifest_sha256": value.manifest_sha256,
    }


def _head_from_mapping(mapping):
    expected = (
        "schema_version",
        "project_id",
        "generation",
        "revision_id",
        "manifest_sha256",
    )
    if not _mapping_has_exact(mapping, expected):
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    return ProjectHead(
        schema_version=mapping["schema_version"],
        project_id=mapping["project_id"],
        generation=mapping["generation"],
        revision_id=mapping["revision_id"],
        manifest_sha256=mapping["manifest_sha256"],
    )


def _journal_state_from_value(value):
    if value == "staging":
        return CommitJournalState.STAGING
    if value == "prepared":
        return CommitJournalState.PREPARED
    if value == "committed":
        return CommitJournalState.COMMITTED
    if value == "not_committed":
        return CommitJournalState.NOT_COMMITTED
    raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)


def _journal_mapping(value):
    return {
        "schema_version": value.schema_version,
        "id": value.id,
        "project_id": value.project_id,
        "expected_head": _head_mapping(value.expected_head),
        "candidate_revision": value.candidate_revision,
        "manifest_sha256": value.manifest_sha256,
        "state": value.state.value,
    }


def _journal_from_mapping(mapping):
    expected = (
        "schema_version",
        "id",
        "project_id",
        "expected_head",
        "candidate_revision",
        "manifest_sha256",
        "state",
    )
    if not _mapping_has_exact(mapping, expected):
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    if type(mapping["state"]) is not str:
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    return CommitJournal(
        schema_version=mapping["schema_version"],
        id=mapping["id"],
        project_id=mapping["project_id"],
        expected_head=_head_from_mapping(mapping["expected_head"]),
        candidate_revision=mapping["candidate_revision"],
        manifest_sha256=mapping["manifest_sha256"],
        state=_journal_state_from_value(mapping["state"]),
    )


def _reconciliation_status_from_value(value):
    if value == "clean":
        return ReconciliationStatus.CLEAN
    if value == "committed":
        return ReconciliationStatus.COMMITTED
    if value == "not_committed":
        return ReconciliationStatus.NOT_COMMITTED
    if value == "cleanup_required":
        return ReconciliationStatus.CLEANUP_REQUIRED
    raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)


def _reconciliation_mapping(value):
    journal_mapping = None
    if value.journal is not None:
        journal_mapping = _journal_mapping(value.journal)
    return {
        "schema_version": value.schema_version,
        "project_id": value.project_id,
        "status": value.status.value,
        "head": _head_mapping(value.head),
        "journal": journal_mapping,
    }


def _reconciliation_from_mapping(mapping):
    expected = (
        "schema_version",
        "project_id",
        "status",
        "head",
        "journal",
    )
    if not _mapping_has_exact(mapping, expected):
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    if type(mapping["status"]) is not str:
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    journal_value = None
    if mapping["journal"] is not None:
        journal_value = _journal_from_mapping(mapping["journal"])
    return ReconciliationResult(
        schema_version=mapping["schema_version"],
        project_id=mapping["project_id"],
        status=_reconciliation_status_from_value(mapping["status"]),
        head=_head_from_mapping(mapping["head"]),
        journal=journal_value,
    )


def _canonical_bytes(value):
    text = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return bytes(text, "utf-8")


def _checked_record_bytes(body, domain):
    body_raw = _canonical_bytes(body)
    checksum = hashlib.sha256(domain + body_raw).hexdigest()
    record = {}
    body_keys = body
    if type(body_keys) is not dict:
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    for body_key in body_keys:
        record[body_key] = body[body_key]
    record["checksum"] = checksum
    return _canonical_bytes(record)


def _json_object_pairs(pairs):
    duplicate = False
    result = {}
    if type(pairs) is not list:
        return (True, result)
    for pair_item in pairs:
        if type(pair_item) is not type(()) or pair_item[0] in result:
            duplicate = True
        else:
            result[pair_item[0]] = pair_item[1]
    return (duplicate, result)


def _unwrap_json(value):
    if type(value) is type(()) and type(value[0]) is bool and type(value[1]) is dict:
        if value[0]:
            return (None, True)
        return _unwrap_json_mapping(value[1])
    if type(value) is list:
        return _unwrap_json_list(value)
    return (value, False)


def _unwrap_json_mapping(source_mapping):
    result_mapping = {}
    if type(source_mapping) is not dict:
        return (None, True)
    for unwrap_key in source_mapping:
        unwrapped_value = _unwrap_json(source_mapping[unwrap_key])
        if unwrapped_value[1]:
            return (None, True)
        result_mapping[unwrap_key] = unwrapped_value[0]
    return (result_mapping, False)


def _unwrap_json_list(source_list):
    result_list = []
    if type(source_list) is not list:
        return (None, True)
    for unwrap_item in source_list:
        unwrapped_item = _unwrap_json(unwrap_item)
        if unwrapped_item[1]:
            return (None, True)
        result_list = result_list + [unwrapped_item[0]]
    return (result_list, False)


def _json_depth_is_safe(raw):
    if type(raw) is not bytes:
        return False
    depth = 0
    quoted = False
    escaped = False
    if type(raw) is not bytes:
        return False
    for depth_byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif depth_byte == 92:
                escaped = True
            elif depth_byte == 34:
                quoted = False
        elif depth_byte == 34:
            quoted = True
        elif depth_byte == 91 or depth_byte == 123:
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                return False
        elif depth_byte == 93 or depth_byte == 125:
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not quoted


def _integer_tokens_are_safe(raw):
    if type(raw) is not bytes:
        return False
    quoted = False
    escaped = False
    digits = 0
    if type(raw) is not bytes:
        return False
    for token_byte in raw:
        if quoted:
            digits = 0
            if escaped:
                escaped = False
            elif token_byte == 92:
                escaped = True
            elif token_byte == 34:
                quoted = False
        elif token_byte == 34:
            quoted = True
            digits = 0
        elif token_byte >= 48 and token_byte <= 57:
            digits += 1
            if digits > 128:
                return False
        else:
            digits = 0
    return True


def _utf8_size_is_safe(value):
    encoded_value = bytes(value, "utf-8")
    size = 0
    if type(encoded_value) is not bytes:
        return False
    for _string_byte in encoded_value:
        size += 1
        if size > _MAX_JSON_STRING_BYTES:
            return False
    return True


def _json_resource_walk(value, depth, count):
    count += 1
    if count > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
        return (count, True)
    if value is None or type(value) is bool:
        return (count, False)
    if type(value) is int:
        if value > MAX_SAFE_JSON_INTEGER or value < -MAX_SAFE_JSON_INTEGER:
            return (count, True)
        return (count, False)
    if type(value) is str:
        return (count, not _utf8_size_is_safe(value))
    if type(value) is list:
        return _json_resource_list(value, depth, count)
    if type(value) is dict:
        return _json_resource_mapping(value, depth, count)
    return (count, True)


def _json_resource_list(resource_list, depth, count):
    if type(resource_list) is not list:
        return (count, True)
    for resource_item in resource_list:
        walked_item = _json_resource_walk(resource_item, depth + 1, count)
        count = walked_item[0]
        if walked_item[1]:
            return (count, True)
    return (count, False)


def _json_resource_mapping(resource_mapping, depth, count):
    if type(resource_mapping) is not dict:
        return (count, True)
    for resource_key in resource_mapping:
        if type(resource_key) is not str or not _utf8_size_is_safe(resource_key):
            return (count, True)
        count += 1
        if count > _MAX_JSON_NODES:
            return (count, True)
        walked_value = _json_resource_walk(resource_mapping[resource_key], depth + 1, count)
        count = walked_value[0]
        if walked_value[1]:
            return (count, True)
    return (count, False)


def _validate_json_resources(value):
    walked = _json_resource_walk(value, 0, 0)
    if walked[1]:
        raise RevisionStoreError(RevisionStoreErrorCode.CORRUPT_RECORD)


def _parse_checked_record(raw, domain, maximum):
    raw_size = 0
    if type(raw) is not bytes:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    for _raw_byte in raw:
        raw_size += 1
        if raw_size > maximum:
            return (None, RevisionStoreErrorCode.BUDGET_EXCEEDED)
    if not _json_depth_is_safe(raw) or not _integer_tokens_are_safe(raw):
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    decode_failed = False
    text = None
    try:
        text = str(raw, "utf-8")
    except UnicodeDecodeError:
        decode_failed = True
    if decode_failed or text is None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    parse_failed = False
    parsed = None
    try:
        parsed = json.loads(text, object_pairs_hook=_json_object_pairs)
    except json.JSONDecodeError:
        parse_failed = True
    if parse_failed:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    unwrapped = _unwrap_json(parsed)
    if unwrapped[1] or type(unwrapped[0]) is not dict:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    record = unwrapped[0]
    walked = _json_resource_walk(record, 0, 0)
    if walked[1]:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if "checksum" not in record or type(record["checksum"]) is not str:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    checksum = record["checksum"]
    if re.fullmatch(_DIGEST_PATTERN, checksum) is None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    body = {}
    record_keys = record
    if type(record_keys) is not dict:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    for record_key in record_keys:
        if record_key != "checksum":
            body[record_key] = record[record_key]
    expected = hashlib.sha256(domain + _canonical_bytes(body)).hexdigest()
    if checksum != expected or _canonical_bytes(record) != raw:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    return (body, None)


def _new_revision_id():
    return "revision_" + secrets.token_hex(16)


def _new_transaction_id():
    return "transaction_" + secrets.token_hex(16)


def _new_artifact_id():
    return "artifact_" + secrets.token_hex(16)


def _path_key(domain, identifier):
    return hashlib.sha256(domain + bytes(identifier, "utf-8")).hexdigest()


def _project_key(project_id):
    return _path_key(_PROJECT_PATH_DOMAIN, project_id)


def _revision_key(revision_id):
    return _path_key(_REVISION_PATH_DOMAIN, revision_id)


def _candidate_key(revision_id):
    return _path_key(_CANDIDATE_PATH_DOMAIN, revision_id)


def _coerce_path(value):
    if type(value) is str:
        path_value = Path(value)
    elif type(value) is type(Path("/")):
        path_value = value
    else:
        return (None, RevisionStoreErrorCode.UNSAFE_STORE)
    parts_value = path_value.parts
    part_count = 0
    if type(parts_value) is not type(()):
        return (None, RevisionStoreErrorCode.UNSAFE_STORE)
    for path_part in parts_value:
        part_count += 1
        if path_part == "..":
            return (None, RevisionStoreErrorCode.UNSAFE_STORE)
    if part_count == 0 or parts_value[0] != "/":
        return (None, RevisionStoreErrorCode.UNSAFE_STORE)
    return ((path_value, parts_value), None)


def _root_flags():
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _read_flags():
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _create_flags():
    return os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _write_flags():
    return os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC


def _replace_create_flags():
    return os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _close_fd(fd):
    failed = False
    try:
        os.close(fd)
    except OSError:
        failed = True
    return failed


def _close_owned_fd(fd):
    return _close_fd(fd)


def _close_two(first_fd, second_fd):
    first_failed = _close_fd(first_fd)
    second_failed = _close_fd(second_fd)
    return first_failed or second_failed


def _open_next_directory(current_fd, component):
    next_fd = None
    open_errno = None
    try:
        next_fd = os.open(component, _root_flags(), dir_fd=current_fd)
    except OSError as open_error:
        open_errno = open_error.errno
    if open_errno is not None or next_fd is None:
        current_close_failed = _close_owned_fd(current_fd)
        if current_close_failed:
            return (None, RevisionStoreErrorCode.IO_ERROR)
        if open_errno == 2:
            return (None, RevisionStoreErrorCode.NOT_FOUND)
        return (None, RevisionStoreErrorCode.IO_ERROR)
    current_close_failed = _close_owned_fd(current_fd)
    if current_close_failed:
        _close_owned_fd(next_fd)
        return (None, RevisionStoreErrorCode.IO_ERROR)
    return (next_fd, None)


def _safe_root_stat(root_stat):
    if not stat.S_ISDIR(root_stat.st_mode):
        return False
    if root_stat.st_uid != os.geteuid():
        return False
    if stat.S_IMODE(root_stat.st_mode) != 448:
        return False
    return True


def _safe_directory_stat(directory_stat, root_device):
    if not stat.S_ISDIR(directory_stat.st_mode):
        return False
    if directory_stat.st_uid != os.geteuid():
        return False
    if stat.S_IMODE(directory_stat.st_mode) != 448:
        return False
    if directory_stat.st_dev != root_device:
        return False
    return True


def _safe_immutable_stat(file_stat, root_device):
    if not stat.S_ISREG(file_stat.st_mode):
        return False
    if file_stat.st_uid != os.geteuid():
        return False
    if stat.S_IMODE(file_stat.st_mode) != 384:
        return False
    if file_stat.st_nlink != 1 or file_stat.st_dev != root_device:
        return False
    return True


def _safe_unlinked_replaceable_stat(file_stat, root_device):
    if not stat.S_ISREG(file_stat.st_mode):
        return False
    if file_stat.st_uid != os.geteuid():
        return False
    if stat.S_IMODE(file_stat.st_mode) != 384:
        return False
    if file_stat.st_nlink != 0 or file_stat.st_dev != root_device:
        return False
    return True


def _safe_candidate_stat(file_stat, root_device):
    if not stat.S_ISREG(file_stat.st_mode):
        return False
    if file_stat.st_uid != os.geteuid():
        return False
    if file_stat.st_nlink != 1 or file_stat.st_dev != root_device:
        return False
    return True


def _open_root(parts_value, expected_identity):
    current_fd = None
    tail_parts = parts_value[1:]
    if type(tail_parts) is not type(()):
        return (None, None, RevisionStoreErrorCode.UNSAFE_STORE)
    open_failed = False
    try:
        current_fd = os.open("/", _root_flags())
    except OSError:
        open_failed = True
    if open_failed or current_fd is None:
        return (None, None, RevisionStoreErrorCode.UNSAFE_STORE)
    for root_part in tail_parts:
        advanced = _open_next_directory(current_fd, root_part)
        if advanced[1] is not None:
            return (None, None, RevisionStoreErrorCode.UNSAFE_STORE)
        current_fd = advanced[0]
    stat_failed = False
    root_stat = None
    try:
        root_stat = os.fstat(current_fd)
    except OSError:
        stat_failed = True
    if stat_failed or root_stat is None or not _safe_root_stat(root_stat):
        _close_fd(current_fd)
        return (None, None, RevisionStoreErrorCode.UNSAFE_STORE)
    if expected_identity is not None:
        if root_stat.st_dev != expected_identity[0] or root_stat.st_ino != expected_identity[1]:
            _close_fd(current_fd)
            return (None, None, RevisionStoreErrorCode.UNSAFE_STORE)
    inheritable_failed = False
    inheritable = True
    try:
        inheritable = os.get_inheritable(current_fd)
    except OSError:
        inheritable_failed = True
    if inheritable_failed or inheritable:
        _close_fd(current_fd)
        return (None, None, RevisionStoreErrorCode.UNSAFE_STORE)
    return (current_fd, root_stat, None)


def _entry_stat(parent_fd, name):
    native_errno = None
    entry_stat = None
    stat_failed = False
    try:
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as stat_error:
        stat_failed = True
        native_errno = stat_error.errno
    if not stat_failed and entry_stat is not None:
        return (entry_stat, True, None)
    if native_errno == 2:
        return (None, False, None)
    return (None, False, RevisionStoreErrorCode.IO_ERROR)


def _open_safe_directory(parent_fd, name, root_device, missing_code):
    before = _entry_stat(parent_fd, name)
    if before[2] is not None:
        return (None, before[2])
    if not before[1]:
        return (None, missing_code)
    if not _safe_directory_stat(before[0], root_device):
        return (None, RevisionStoreErrorCode.UNSAFE_STORE)
    directory_fd = None
    handed_off = False
    try:
        try:
            directory_fd = os.open(name, _root_flags(), dir_fd=parent_fd)
        except OSError:
            return (None, RevisionStoreErrorCode.UNSAFE_STORE)
        opened_stat = None
        try:
            opened_stat = os.fstat(directory_fd)
        except OSError:
            return (None, RevisionStoreErrorCode.UNSAFE_STORE)
        if opened_stat is None:
            return (None, RevisionStoreErrorCode.UNSAFE_STORE)
        if not _safe_directory_stat(opened_stat, root_device):
            return (None, RevisionStoreErrorCode.UNSAFE_STORE)
        if opened_stat.st_dev != before[0].st_dev or opened_stat.st_ino != before[0].st_ino:
            return (None, RevisionStoreErrorCode.UNSAFE_STORE)
        handed_off = True
        return (directory_fd, None)
    finally:
        if directory_fd is not None and not handed_off:
            _close_fd(directory_fd)


def _open_project(root_fd, root_device, project_id):
    project_fd = None
    revisions_fd = None
    candidates_fd = None
    handed_off = False
    try:
        project_open = _open_safe_directory(
            root_fd,
            _project_key(project_id),
            root_device,
            RevisionStoreErrorCode.NOT_FOUND,
        )
        if project_open[1] is not None:
            return (None, None, None, project_open[1])
        project_fd = project_open[0]
        revisions_open = _open_safe_directory(
            project_fd,
            "revisions",
            root_device,
            RevisionStoreErrorCode.UNSAFE_STORE,
        )
        if revisions_open[1] is not None:
            return (None, None, None, revisions_open[1])
        revisions_fd = revisions_open[0]
        candidates_open = _open_safe_directory(
            project_fd,
            "candidates",
            root_device,
            RevisionStoreErrorCode.UNSAFE_STORE,
        )
        if candidates_open[1] is not None:
            return (None, None, None, candidates_open[1])
        candidates_fd = candidates_open[0]
        handed_off = True
        return (project_fd, revisions_fd, candidates_fd, None)
    finally:
        if not handed_off:
            if candidates_fd is not None:
                _close_fd(candidates_fd)
            if revisions_fd is not None:
                _close_fd(revisions_fd)
            if project_fd is not None:
                _close_fd(project_fd)


def _open_checked_file(parent_fd, name, root_device, maximum, missing_code, replaceable):
    attempt = 0
    while attempt < _MAX_RECORD_OPEN_ATTEMPTS:
        attempt += 1
        before = _entry_stat(parent_fd, name)
        if before[2] is not None:
            return (None, None, before[2])
        if not before[1]:
            return (None, None, missing_code)
        before_unlinked = False
        if replaceable:
            before_unlinked = _safe_unlinked_replaceable_stat(
                before[0],
                root_device,
            )
        if before_unlinked:
            continue
        if not _safe_immutable_stat(before[0], root_device):
            return (None, None, RevisionStoreErrorCode.UNSAFE_STORE)
        if before[0].st_size > maximum:
            return (None, None, RevisionStoreErrorCode.BUDGET_EXCEEDED)
        file_fd = None
        open_failed = False
        try:
            file_fd = os.open(name, _read_flags(), dir_fd=parent_fd)
        except OSError:
            open_failed = True
        if open_failed or file_fd is None:
            if replaceable:
                continue
            return (None, None, RevisionStoreErrorCode.IO_ERROR)
        handed_off = False
        try:
            opened_stat = None
            try:
                opened_stat = os.fstat(file_fd)
            except OSError:
                return (None, None, RevisionStoreErrorCode.IO_ERROR)
            if opened_stat is None:
                return (None, None, RevisionStoreErrorCode.IO_ERROR)
            replaceable_unlinked = False
            if replaceable:
                replaceable_unlinked = _safe_unlinked_replaceable_stat(
                    opened_stat,
                    root_device,
                )
            if replaceable_unlinked:
                close_failed = _close_fd(file_fd)
                file_fd = None
                if close_failed:
                    return (None, None, RevisionStoreErrorCode.IO_ERROR)
                continue
            if not _safe_immutable_stat(opened_stat, root_device):
                return (None, None, RevisionStoreErrorCode.UNSAFE_STORE)
            if opened_stat.st_dev == before[0].st_dev and opened_stat.st_ino == before[0].st_ino:
                handed_off = True
                return (file_fd, opened_stat, None)
            close_failed = _close_fd(file_fd)
            file_fd = None
            if close_failed:
                return (None, None, RevisionStoreErrorCode.IO_ERROR)
            if not replaceable:
                return (None, None, RevisionStoreErrorCode.UNSAFE_STORE)
        finally:
            if file_fd is not None and not handed_off:
                _close_fd(file_fd)
    return (None, None, RevisionStoreErrorCode.IO_ERROR)


def _read_bounded_file(parent_fd, name, root_device, maximum, missing_code):
    replaceable = name == "HEAD.json" or name == "journal.json"
    opened = _open_checked_file(
        parent_fd,
        name,
        root_device,
        maximum,
        missing_code,
        replaceable,
    )
    if opened[2] is not None:
        return (None, opened[2])
    file_fd = opened[0]
    opened_stat = opened[1]
    remaining = opened_stat.st_size
    raw = b""
    read_failed = False
    after_stat = None
    stat_failed = False
    close_failed = False
    try:
        while remaining > 0 and not read_failed:
            chunk = None
            try:
                chunk = os.read(file_fd, _COPY_CHUNK_BYTES)
            except OSError:
                read_failed = True
            if not read_failed:
                chunk_size = _byte_count(chunk, _COPY_CHUNK_BYTES)
                if chunk_size < 0:
                    read_failed = True
                if chunk_size == 0 or chunk_size > remaining:
                    read_failed = True
                else:
                    raw = raw + chunk
                    remaining -= chunk_size
        try:
            after_stat = os.fstat(file_fd)
        except OSError:
            stat_failed = True
    finally:
        close_failed = _close_fd(file_fd)
    if read_failed or stat_failed or after_stat is None or close_failed:
        return (None, RevisionStoreErrorCode.IO_ERROR)
    replaceable_unlinked = False
    if replaceable:
        replaceable_unlinked = _safe_unlinked_replaceable_stat(
            after_stat,
            root_device,
        )
    if not _safe_immutable_stat(after_stat, root_device) and not replaceable_unlinked:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if after_stat.st_dev != opened_stat.st_dev or after_stat.st_ino != opened_stat.st_ino:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if after_stat.st_size != opened_stat.st_size:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if after_stat.st_mtime_ns != opened_stat.st_mtime_ns:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if after_stat.st_nlink != opened_stat.st_nlink and not replaceable_unlinked:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if after_stat.st_ctime_ns != opened_stat.st_ctime_ns and not replaceable_unlinked:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    return (raw, None)


def _byte_count(raw, maximum):
    count = 0
    if type(raw) is not bytes:
        return -1
    for _count_byte in raw:
        count += 1
        if count > maximum:
            return -1
    return count


def _write_all(file_fd, raw):
    offset = 0
    total = 0
    if type(raw) is not bytes:
        return False
    for _write_byte in raw:
        total += 1
    while offset < total:
        failed = False
        written = 0
        try:
            written = os.write(file_fd, raw[offset:])
        except OSError:
            failed = True
        if failed or type(written) is not int or written <= 0:
            return False
        offset += written
    return True


def _create_durable_file(parent_fd, name, raw, precreated=False):
    failed = False
    file_fd = None
    try:
        flags = _create_flags()
        if precreated:
            flags = _write_flags()
        file_fd = os.open(name, flags, 384, dir_fd=parent_fd)
    except OSError:
        failed = True
    if failed or file_fd is None:
        return RevisionStoreErrorCode.IO_ERROR
    write_ok = _write_all(file_fd, raw)
    sync_failed = False
    if write_ok:
        try:
            os.fchmod(file_fd, 384)
            os.fsync(file_fd)
        except OSError:
            sync_failed = True
    close_failed = _close_fd(file_fd)
    if not write_ok or sync_failed or close_failed:
        return RevisionStoreErrorCode.IO_ERROR
    return None


def _create_empty_file(parent_fd, name):
    file_fd = None
    try:
        file_fd = os.open(name, _create_flags(), 384, dir_fd=parent_fd)
        os.fchmod(file_fd, 384)
        os.fsync(file_fd)
    except OSError:
        if file_fd is not None:
            _close_fd(file_fd)
        return RevisionStoreErrorCode.IO_ERROR
    if _close_fd(file_fd):
        return RevisionStoreErrorCode.IO_ERROR
    return None


def _replace_durable_record(parent_fd, filename, raw, token, uncertainty):
    temp_name = "." + filename + "." + token + ".tmp"
    code = _create_durable_file(parent_fd, temp_name, raw)
    if code is not None:
        return code
    replace_failed = False
    try:
        os.replace(temp_name, filename, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except OSError:
        replace_failed = True
    if replace_failed:
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except OSError:
            pass
        return RevisionStoreErrorCode.IO_ERROR
    sync_failed = False
    try:
        os.fsync(parent_fd)
    except OSError:
        sync_failed = True
    if sync_failed:
        return uncertainty
    return None


def _artifact_record_code(mapping):
    expected = (
        "schema_version",
        "id",
        "name",
        "format",
        "sha256",
        "size_bytes",
    )
    if not _mapping_has_exact(mapping, expected):
        return RevisionStoreErrorCode.CORRUPT_RECORD
    if type(mapping["schema_version"]) is not int or mapping["schema_version"] != 1:
        return RevisionStoreErrorCode.CORRUPT_RECORD
    if _identifier_code(mapping["id"], _ARTIFACT_PATTERN) is not None:
        return RevisionStoreErrorCode.CORRUPT_RECORD
    if type(mapping["name"]) is not str or type(mapping["format"]) is not str:
        return RevisionStoreErrorCode.CORRUPT_RECORD
    pair_ok = False
    if mapping["name"] == "model.FCStd" and mapping["format"] == "fcstd":
        pair_ok = True
    if mapping["name"] == "model.step" and mapping["format"] == "step":
        pair_ok = True
    if not pair_ok or _digest_code(mapping["sha256"]) is not None:
        return RevisionStoreErrorCode.CORRUPT_RECORD
    size_value = mapping["size_bytes"]
    if type(size_value) is not int or size_value <= 0:
        return RevisionStoreErrorCode.CORRUPT_RECORD
    if size_value > _MAX_FILE_BYTES:
        return RevisionStoreErrorCode.BUDGET_EXCEEDED
    return None


def _artifact_from_record(mapping):
    code = _artifact_record_code(mapping)
    if code is not None:
        return (None, code)
    value = RevisionArtifactRef(
        schema_version=mapping["schema_version"],
        id=mapping["id"],
        name=mapping["name"],
        format=mapping["format"],
        sha256=mapping["sha256"],
        size_bytes=mapping["size_bytes"],
    )
    return (value, None)


def _artifacts_from_record(values):
    if type(values) is not list:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    result = ()
    if type(values) is not list:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    for artifact_record in values:
        artifact_result = _artifact_from_record(artifact_record)
        if artifact_result[1] is not None:
            return (None, artifact_result[1])
        result = result + (artifact_result[0],)
    return (result, None)


def _manifest_body(project_id, revision_id, base_revision, model, artifacts):
    model_mapping = None
    if model is not None:
        model_mapping = _artifact_mapping(model)
    return {
        "schema_version": 1,
        "project_id": project_id,
        "revision_id": revision_id,
        "base_revision": base_revision,
        "model": model_mapping,
        "artifacts": _artifact_list_mapping(artifacts),
    }


def _revision_from_manifest(body, raw):
    expected = (
        "schema_version",
        "project_id",
        "revision_id",
        "base_revision",
        "model",
        "artifacts",
    )
    if not _mapping_has_exact(body, expected):
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if type(body["schema_version"]) is not int or body["schema_version"] != 1:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if _identifier_code(body["project_id"], _PROJECT_PATTERN) is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if _identifier_code(body["revision_id"], _REVISION_PATTERN) is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    base_value = body["base_revision"]
    if base_value is not None:
        if _identifier_code(base_value, _REVISION_PATTERN) is not None:
            return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
        if base_value == body["revision_id"]:
            return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    model_value = None
    if body["model"] is not None:
        model_result = _artifact_from_record(body["model"])
        if model_result[1] is not None:
            return (None, model_result[1])
        model_value = model_result[0]
    artifacts_result = _artifacts_from_record(body["artifacts"])
    if artifacts_result[1] is not None:
        return (None, artifacts_result[1])
    digest = hashlib.sha256(raw).hexdigest()
    provisional = _revision_record_invariants(
        base_value,
        model_value,
        artifacts_result[0],
    )
    if provisional is not None:
        return (None, provisional)
    value = RevisionRef(
        id=body["revision_id"],
        project_id=body["project_id"],
        base_revision=base_value,
        manifest_sha256=digest,
        model=model_value,
        artifacts=artifacts_result[0],
    )
    return (value, None)


def _revision_record_invariants(base_value, model_value, artifacts_value):
    artifact_count = 0
    if type(artifacts_value) is not type(()):
        return RevisionStoreErrorCode.CORRUPT_RECORD
    for _invariant_artifact in artifacts_value:
        artifact_count += 1
    if base_value is None:
        if artifact_count != 0:
            return RevisionStoreErrorCode.CORRUPT_RECORD
        if model_value is not None:
            if model_value.name != "model.FCStd" or model_value.format != "fcstd":
                return RevisionStoreErrorCode.CORRUPT_RECORD
        return None
    if model_value is None or artifact_count != 1:
        return RevisionStoreErrorCode.CORRUPT_RECORD
    if model_value.name != "model.FCStd" or model_value.format != "fcstd":
        return RevisionStoreErrorCode.CORRUPT_RECORD
    step_value = artifacts_value[0]
    if step_value.name != "model.step" or step_value.format != "step":
        return RevisionStoreErrorCode.CORRUPT_RECORD
    if step_value.id == model_value.id:
        return RevisionStoreErrorCode.CORRUPT_RECORD
    if step_value.size_bytes + model_value.size_bytes > _MAX_REVISION_BYTES:
        return RevisionStoreErrorCode.BUDGET_EXCEEDED
    return None


def _head_from_record(body):
    expected = (
        "schema_version",
        "project_id",
        "generation",
        "revision_id",
        "manifest_sha256",
    )
    if not _mapping_has_exact(body, expected):
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if type(body["schema_version"]) is not int or body["schema_version"] != 1:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if _identifier_code(body["project_id"], _PROJECT_PATTERN) is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if _identifier_code(body["revision_id"], _REVISION_PATTERN) is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if type(body["generation"]) is not int or body["generation"] < 0:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if body["generation"] > MAX_SAFE_JSON_INTEGER:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if _digest_code(body["manifest_sha256"]) is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    value = ProjectHead(
        schema_version=body["schema_version"],
        project_id=body["project_id"],
        generation=body["generation"],
        revision_id=body["revision_id"],
        manifest_sha256=body["manifest_sha256"],
    )
    return (value, None)


def _journal_from_record(body):
    expected = (
        "schema_version",
        "id",
        "project_id",
        "expected_head",
        "candidate_revision",
        "manifest_sha256",
        "state",
    )
    if not _mapping_has_exact(body, expected):
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if type(body["schema_version"]) is not int or body["schema_version"] != 1:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if _identifier_code(body["id"], _TRANSACTION_PATTERN) is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if _identifier_code(body["project_id"], _PROJECT_PATTERN) is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if _identifier_code(body["candidate_revision"], _REVISION_PATTERN) is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    expected_head_result = _head_from_record(body["expected_head"])
    if expected_head_result[1] is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if expected_head_result[0].project_id != body["project_id"]:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if body["candidate_revision"] == expected_head_result[0].revision_id:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if type(body["state"]) is not str:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    state_value = None
    if body["state"] == "staging":
        state_value = CommitJournalState.STAGING
    elif body["state"] == "prepared":
        state_value = CommitJournalState.PREPARED
    elif body["state"] == "committed":
        state_value = CommitJournalState.COMMITTED
    elif body["state"] == "not_committed":
        state_value = CommitJournalState.NOT_COMMITTED
    else:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if state_value is CommitJournalState.STAGING:
        if body["manifest_sha256"] is not None:
            return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    elif _digest_code(body["manifest_sha256"]) is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    value = CommitJournal(
        schema_version=body["schema_version"],
        id=body["id"],
        project_id=body["project_id"],
        expected_head=expected_head_result[0],
        candidate_revision=body["candidate_revision"],
        manifest_sha256=body["manifest_sha256"],
        state=state_value,
    )
    return (value, None)


def _open_revision_directory(revisions_fd, root_device, revision_id):
    return _open_safe_directory(
        revisions_fd,
        _revision_key(revision_id),
        root_device,
        RevisionStoreErrorCode.NOT_FOUND,
    )


def _revision_source_binding(source_stat):
    try:
        return (
            RevisionSourceBinding(
                dev=source_stat.st_dev,
                ino=source_stat.st_ino,
                mode=source_stat.st_mode,
                uid=source_stat.st_uid,
                nlink=source_stat.st_nlink,
                size=source_stat.st_size,
                mtime_ns=source_stat.st_mtime_ns,
                ctime_ns=source_stat.st_ctime_ns,
            ),
            None,
        )
    except RevisionStoreError:
        return (None, RevisionStoreErrorCode.CORRUPT_CONTENT)


def _observe_model_binding_fd(revision_fd, root_device, model):
    opened = _open_checked_file(
        revision_fd,
        model.name,
        root_device,
        _MAX_FILE_BYTES,
        RevisionStoreErrorCode.CORRUPT_CONTENT,
        False,
    )
    if opened[2] is not None:
        return (None, opened[2])
    model_fd = opened[0]
    binding = _revision_source_binding(opened[1])
    code = binding[1]
    if code is None and opened[1].st_size != model.size_bytes:
        code = RevisionStoreErrorCode.CORRUPT_CONTENT
    entry = None
    if code is None:
        entry = _entry_stat(revision_fd, model.name)
        code = entry[2]
        if code is None and not entry[1]:
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
        if code is None and not _source_matches_binding(entry[0], binding[0]):
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
    final_stat = None
    if code is None:
        try:
            final_stat = os.fstat(model_fd)
        except OSError:
            code = RevisionStoreErrorCode.IO_ERROR
        if code is None and not _source_matches_binding(final_stat, binding[0]):
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
    if _close_fd(model_fd) and code is None:
        code = RevisionStoreErrorCode.IO_ERROR
    if code is not None:
        return (None, code)
    return binding


def _load_revision_fd(
    revisions_fd,
    root_device,
    project_id,
    revision_id,
    observe_model=False,
):
    opened = _open_revision_directory(revisions_fd, root_device, revision_id)
    if opened[1] is not None:
        return (None, opened[1])
    revision_fd = opened[0]
    revision_stat = None
    if observe_model:
        try:
            revision_stat = os.fstat(revision_fd)
        except OSError:
            _close_fd(revision_fd)
            return (None, RevisionStoreErrorCode.IO_ERROR)
    manifest_read = _read_bounded_file(
        revision_fd,
        "manifest.json",
        root_device,
        _MAX_MANIFEST_BYTES,
        RevisionStoreErrorCode.CORRUPT_RECORD,
    )
    if manifest_read[1] is not None:
        _close_fd(revision_fd)
        code = manifest_read[1]
        if code is RevisionStoreErrorCode.NOT_FOUND:
            code = RevisionStoreErrorCode.CORRUPT_RECORD
        return (None, code)
    parsed = _parse_checked_record(
        manifest_read[0],
        _MANIFEST_CHECKSUM_DOMAIN,
        _MAX_MANIFEST_BYTES,
    )
    if parsed[1] is not None:
        _close_fd(revision_fd)
        return (None, parsed[1])
    revision_result = _revision_from_manifest(parsed[0], manifest_read[0])
    if revision_result[1] is not None:
        _close_fd(revision_fd)
        return revision_result
    revision_value = revision_result[0]
    if revision_value.project_id != project_id or revision_value.id != revision_id:
        _close_fd(revision_fd)
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    source_before = (None, None)
    if observe_model:
        if revision_value.model is None:
            _close_fd(revision_fd)
            return (None, RevisionStoreErrorCode.NOT_FOUND)
        source_before = _observe_model_binding_fd(
            revision_fd,
            root_device,
            revision_value.model,
        )
        if source_before[1] is not None:
            _close_fd(revision_fd)
            return (None, source_before[1])
    content_result = _validate_revision_content(revision_fd, root_device, revision_value)
    content_code = content_result[0]
    hashed_model_binding = content_result[1]
    if content_code is None:
        manifest_after = _read_bounded_file(
            revision_fd,
            "manifest.json",
            root_device,
            _MAX_MANIFEST_BYTES,
            RevisionStoreErrorCode.CORRUPT_RECORD,
        )
        if manifest_after[1] is not None:
            content_code = manifest_after[1]
            if content_code is RevisionStoreErrorCode.NOT_FOUND:
                content_code = RevisionStoreErrorCode.CORRUPT_RECORD
        elif manifest_after[0] != manifest_read[0]:
            content_code = RevisionStoreErrorCode.CORRUPT_RECORD
    source_after = (None, None)
    if content_code is None and observe_model:
        if source_before[0] != hashed_model_binding:
            content_code = RevisionStoreErrorCode.CORRUPT_CONTENT
    if content_code is None and observe_model:
        source_after = _observe_model_binding_fd(
            revision_fd,
            root_device,
            revision_value.model,
        )
        if source_after[1] is not None:
            content_code = source_after[1]
        elif source_before[0] != source_after[0]:
            content_code = RevisionStoreErrorCode.CORRUPT_CONTENT
    if content_code is None and observe_model:
        revision_after = None
        try:
            revision_after = os.fstat(revision_fd)
        except OSError:
            content_code = RevisionStoreErrorCode.IO_ERROR
        if content_code is None and not _same_source_parent(revision_after, revision_stat):
            content_code = RevisionStoreErrorCode.CORRUPT_CONTENT
    if content_code is None and observe_model:
        revision_entry = _entry_stat(revisions_fd, _revision_key(revision_id))
        if revision_entry[2] is not None:
            content_code = revision_entry[2]
        elif not revision_entry[1]:
            content_code = RevisionStoreErrorCode.CORRUPT_CONTENT
        elif not _safe_directory_stat(revision_entry[0], root_device):
            content_code = RevisionStoreErrorCode.CORRUPT_CONTENT
        elif not _same_source_parent(revision_entry[0], revision_stat):
            content_code = RevisionStoreErrorCode.CORRUPT_CONTENT
    close_failed = _close_fd(revision_fd)
    if content_code is not None:
        return (None, content_code)
    if close_failed:
        return (None, RevisionStoreErrorCode.IO_ERROR)
    if observe_model:
        return ((revision_value, source_after[0]), None)
    return (revision_value, None)


def _validate_revision_content(revision_fd, root_device, revision_value):
    model_binding = None
    if revision_value.model is not None:
        model_result = _validate_content_file(
            revision_fd,
            root_device,
            revision_value.model,
        )
        if model_result[0] is not None:
            return (model_result[0], None)
        model_binding = model_result[1]
    content_artifacts = revision_value.artifacts
    if type(content_artifacts) is not type(()):
        return (RevisionStoreErrorCode.CORRUPT_RECORD, None)
    for content_artifact in content_artifacts:
        artifact_result = _validate_content_file(
            revision_fd,
            root_device,
            content_artifact,
        )
        if artifact_result[0] is not None:
            return (artifact_result[0], None)
    return (None, model_binding)


def _validate_content_file(revision_fd, root_device, reference):
    opened = _open_checked_file(
        revision_fd,
        reference.name,
        root_device,
        _MAX_FILE_BYTES,
        RevisionStoreErrorCode.CORRUPT_CONTENT,
        False,
    )
    if opened[2] is not None:
        return (opened[2], None)
    content_fd = opened[0]
    content_stat = opened[1]
    if content_stat.st_size != reference.size_bytes:
        _close_fd(content_fd)
        return (RevisionStoreErrorCode.CORRUPT_CONTENT, None)
    binding = _revision_source_binding(content_stat)
    if binding[1] is not None:
        _close_fd(content_fd)
        return (binding[1], None)
    remaining = content_stat.st_size
    content_hash_state = hashlib.sha256()
    read_failed = False
    while remaining > 0 and not read_failed:
        chunk = None
        try:
            chunk = os.read(content_fd, _COPY_CHUNK_BYTES)
        except OSError:
            read_failed = True
        if not read_failed:
            chunk_size = _byte_count(chunk, _COPY_CHUNK_BYTES)
            if chunk_size <= 0 or chunk_size > remaining:
                read_failed = True
            else:
                content_hash_state.update(chunk)
                remaining -= chunk_size
    after_stat = None
    stat_failed = False
    try:
        after_stat = os.fstat(content_fd)
    except OSError:
        stat_failed = True
    close_failed = _close_fd(content_fd)
    if read_failed or stat_failed or after_stat is None or close_failed:
        return (RevisionStoreErrorCode.IO_ERROR, None)
    if not _source_matches_binding(after_stat, binding[0]):
        return (RevisionStoreErrorCode.CORRUPT_CONTENT, None)
    actual_digest = content_hash_state.hexdigest()
    if actual_digest != reference.sha256:
        return (RevisionStoreErrorCode.CORRUPT_CONTENT, None)
    return (None, binding[0])


def _load_head_record_fd(project_fd, root_device, project_id):
    head_read = _read_bounded_file(
        project_fd,
        "HEAD.json",
        root_device,
        _MAX_HEAD_BYTES,
        RevisionStoreErrorCode.CORRUPT_RECORD,
    )
    if head_read[1] is not None:
        return (None, head_read[1])
    parsed = _parse_checked_record(head_read[0], _HEAD_CHECKSUM_DOMAIN, _MAX_HEAD_BYTES)
    if parsed[1] is not None:
        return (None, parsed[1])
    head_result = _head_from_record(parsed[0])
    if head_result[1] is not None:
        return head_result
    head_value = head_result[0]
    if head_value.project_id != project_id:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    return (head_value, None)


def _load_head_fd(project_fd, revisions_fd, root_device, project_id):
    head_result = _load_head_record_fd(project_fd, root_device, project_id)
    if head_result[1] is not None:
        return head_result
    head_value = head_result[0]
    revision_result = _load_revision_fd(
        revisions_fd,
        root_device,
        project_id,
        head_value.revision_id,
    )
    if revision_result[1] is not None:
        return (None, revision_result[1])
    if revision_result[0].manifest_sha256 != head_value.manifest_sha256:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    return (head_value, None)


def _load_journal_fd(project_fd, root_device):
    entry = _entry_stat(project_fd, "journal.json")
    if entry[2] is not None:
        return (None, entry[2])
    if not entry[1]:
        return (None, None)
    journal_read = _read_bounded_file(
        project_fd,
        "journal.json",
        root_device,
        _MAX_JOURNAL_BYTES,
        RevisionStoreErrorCode.CORRUPT_RECORD,
    )
    if journal_read[1] is not None:
        return (None, journal_read[1])
    parsed = _parse_checked_record(
        journal_read[0],
        _JOURNAL_CHECKSUM_DOMAIN,
        _MAX_JOURNAL_BYTES,
    )
    if parsed[1] is not None:
        return (None, parsed[1])
    return _journal_from_record(parsed[0])


def _safe_source_parent_stat(parent_stat):
    if not stat.S_ISDIR(parent_stat.st_mode):
        return False
    if parent_stat.st_uid != os.geteuid():
        return False
    if stat.S_IMODE(parent_stat.st_mode) != 448:
        return False
    return True


def _same_source_parent(left, right):
    if left.st_dev != right.st_dev or left.st_ino != right.st_ino:
        return False
    if left.st_mode != right.st_mode or left.st_uid != right.st_uid:
        return False
    return True


def _safe_external_source_stat(source_stat):
    if not stat.S_ISREG(source_stat.st_mode):
        return False
    if source_stat.st_uid != os.geteuid():
        return False
    if stat.S_IMODE(source_stat.st_mode) != 384:
        return False
    if source_stat.st_nlink != 1:
        return False
    if source_stat.st_size <= 0 or source_stat.st_size > _MAX_FILE_BYTES:
        return False
    return True


def _source_matches_binding(source_stat, expected_binding):
    if source_stat.st_dev != expected_binding.dev:
        return False
    if source_stat.st_ino != expected_binding.ino:
        return False
    if source_stat.st_mode != expected_binding.mode:
        return False
    if source_stat.st_uid != expected_binding.uid:
        return False
    if source_stat.st_nlink != expected_binding.nlink:
        return False
    if source_stat.st_size != expected_binding.size:
        return False
    if source_stat.st_mtime_ns != expected_binding.mtime_ns:
        return False
    if source_stat.st_ctime_ns != expected_binding.ctime_ns:
        return False
    return True


def _source_parent_after_code(parent_fd, expected_parent):
    current = None
    try:
        current = os.fstat(parent_fd)
    except OSError:
        return RevisionStoreErrorCode.IO_ERROR
    if not _safe_source_parent_stat(current):
        return RevisionStoreErrorCode.UNSAFE_STORE
    if not _same_source_parent(current, expected_parent):
        return RevisionStoreErrorCode.UNSAFE_STORE
    return None


def _open_external_source_at(source_parent_fd, source_name, expected_binding):
    if type(source_parent_fd) is not int or source_parent_fd < 0:
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT, None)
    if (
        type(source_name) is not str
        or source_name in {".", ".."}
        or re.fullmatch(_SOURCE_NAME_PATTERN, source_name) is None
    ):
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT, None)
    if type(expected_binding) is not RevisionSourceBinding:
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT, None)
    parent_before = None
    try:
        parent_before = os.fstat(source_parent_fd)
    except OSError:
        return (None, None, None, None, RevisionStoreErrorCode.UNSAFE_STORE, None)
    if not _safe_source_parent_stat(parent_before):
        return (None, None, None, None, RevisionStoreErrorCode.UNSAFE_STORE, None)
    parent_fd = None
    source_fd = None
    source_stat = None
    code = None
    try:
        parent_fd = os.dup(source_parent_fd)
    except OSError:
        code = RevisionStoreErrorCode.IO_ERROR
    duplicated_parent = None
    inheritable = True
    if code is None:
        try:
            duplicated_parent = os.fstat(parent_fd)
            inheritable = os.get_inheritable(parent_fd)
        except OSError:
            code = RevisionStoreErrorCode.IO_ERROR
    if code is None and (
        inheritable
        or duplicated_parent is None
        or not _safe_source_parent_stat(duplicated_parent)
        or not _same_source_parent(duplicated_parent, parent_before)
    ):
        code = RevisionStoreErrorCode.UNSAFE_STORE
    before = None
    if code is None:
        before = _entry_stat(parent_fd, source_name)
        if before[2] is not None:
            code = before[2]
        elif not before[1]:
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
        elif not _safe_external_source_stat(before[0]):
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
        elif not _source_matches_binding(before[0], expected_binding):
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
    if code is None:
        try:
            source_fd = os.open(source_name, _read_flags(), dir_fd=parent_fd)
        except OSError:
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
    if code is None:
        try:
            source_stat = os.fstat(source_fd)
        except OSError:
            code = RevisionStoreErrorCode.IO_ERROR
    if code is None and (
        source_stat is None
        or not _safe_external_source_stat(source_stat)
        or not _source_matches_binding(source_stat, expected_binding)
    ):
        code = RevisionStoreErrorCode.CORRUPT_CONTENT
    if code is None:
        after_open = _entry_stat(parent_fd, source_name)
        if after_open[2] is not None:
            code = after_open[2]
        elif not after_open[1]:
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
        elif not _safe_external_source_stat(after_open[0]):
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
        elif not _source_matches_binding(after_open[0], expected_binding):
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
    if code is None:
        code = _source_parent_after_code(parent_fd, parent_before)
    if code is not None:
        close_failed = False
        if source_fd is not None:
            close_failed = _close_fd(source_fd) or close_failed
        if parent_fd is not None:
            close_failed = _close_fd(parent_fd) or close_failed
        if close_failed:
            code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        return (None, None, None, None, code, None)
    return (parent_fd, source_fd, source_stat, source_name, None, parent_before)


def _open_external_source(source):
    coerced = _coerce_path(source)
    if coerced[1] is not None:
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    parts_value = coerced[0][1]
    part_count = 0
    if type(parts_value) is not type(()):
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    for _source_part_count in parts_value:
        part_count += 1
    if part_count < 2:
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    parent_parts = parts_value[1:-1]
    if type(parent_parts) is not type(()):
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    parent_fd = None
    open_failed = False
    try:
        parent_fd = os.open("/", _root_flags())
    except OSError:
        open_failed = True
    if open_failed or parent_fd is None:
        return (None, None, None, None, RevisionStoreErrorCode.NOT_FOUND)
    for source_parent_part in parent_parts:
        advanced_parent = _open_next_directory(parent_fd, source_parent_part)
        if advanced_parent[1] is not None:
            return (None, None, None, None, advanced_parent[1])
        parent_fd = advanced_parent[0]
    filename = parts_value[part_count - 1]
    before = _entry_stat(parent_fd, filename)
    if before[2] is not None:
        _close_fd(parent_fd)
        return (None, None, None, None, before[2])
    if not before[1]:
        _close_fd(parent_fd)
        return (None, None, None, None, RevisionStoreErrorCode.NOT_FOUND)
    source_stat = before[0]
    if not stat.S_ISREG(source_stat.st_mode):
        _close_fd(parent_fd)
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    if source_stat.st_uid != os.geteuid() or source_stat.st_nlink != 1:
        _close_fd(parent_fd)
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    if source_stat.st_size <= 0:
        _close_fd(parent_fd)
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    if source_stat.st_size > _MAX_FILE_BYTES:
        _close_fd(parent_fd)
        return (None, None, None, None, RevisionStoreErrorCode.BUDGET_EXCEEDED)
    source_fd = None
    open_failed = False
    try:
        source_fd = os.open(coerced[0][0], _read_flags())
    except OSError:
        open_failed = True
    if open_failed or source_fd is None:
        _close_fd(parent_fd)
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    opened_stat = None
    stat_failed = False
    try:
        opened_stat = os.fstat(source_fd)
    except OSError:
        stat_failed = True
    safe = True
    if stat_failed or opened_stat is None:
        safe = False
    elif not stat.S_ISREG(opened_stat.st_mode):
        safe = False
    elif opened_stat.st_uid != os.geteuid() or opened_stat.st_nlink != 1:
        safe = False
    elif opened_stat.st_dev != source_stat.st_dev or opened_stat.st_ino != source_stat.st_ino:
        safe = False
    elif opened_stat.st_size != source_stat.st_size:
        safe = False
    if not safe:
        _close_fd(source_fd)
        _close_fd(parent_fd)
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    return (parent_fd, source_fd, opened_stat, filename, None)


def _open_candidate_source(candidate_fd, root_device, filename):
    before = _entry_stat(candidate_fd, filename)
    if before[2] is not None:
        return (None, None, before[2])
    if not before[1]:
        return (None, None, RevisionStoreErrorCode.NOT_FOUND)
    if not _safe_candidate_stat(before[0], root_device):
        return (None, None, RevisionStoreErrorCode.INVALID_INPUT)
    if before[0].st_size <= 0:
        return (None, None, RevisionStoreErrorCode.INVALID_INPUT)
    if before[0].st_size > _MAX_FILE_BYTES:
        return (None, None, RevisionStoreErrorCode.BUDGET_EXCEEDED)
    source_fd = None
    failed = False
    try:
        source_fd = os.open(filename, _read_flags(), dir_fd=candidate_fd)
    except OSError:
        failed = True
    if failed or source_fd is None:
        return (None, None, RevisionStoreErrorCode.INVALID_INPUT)
    opened_stat = None
    stat_failed = False
    try:
        opened_stat = os.fstat(source_fd)
    except OSError:
        stat_failed = True
    if stat_failed or opened_stat is None:
        _close_fd(source_fd)
        return (None, None, RevisionStoreErrorCode.INVALID_INPUT)
    if not _safe_candidate_stat(opened_stat, root_device):
        _close_fd(source_fd)
        return (None, None, RevisionStoreErrorCode.INVALID_INPUT)
    if opened_stat.st_dev != before[0].st_dev or opened_stat.st_ino != before[0].st_ino:
        _close_fd(source_fd)
        return (None, None, RevisionStoreErrorCode.INVALID_INPUT)
    return (source_fd, opened_stat, None)


def _copy_open_file(
    source_parent_fd,
    source_fd,
    source_stat,
    source_name,
    target_fd,
    target_name,
    precreated=False,
):
    destination_fd = None
    open_failed = False
    try:
        flags = _create_flags()
        if precreated:
            flags = _write_flags()
        destination_fd = os.open(target_name, flags, 384, dir_fd=target_fd)
    except OSError:
        open_failed = True
    if open_failed or destination_fd is None:
        return (None, None, RevisionStoreErrorCode.IO_ERROR)
    remaining = source_stat.st_size
    total = 0
    hash_copy_state = hashlib.sha256()
    copy_failed = False
    while remaining > 0 and not copy_failed:
        chunk = None
        try:
            chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
        except OSError:
            copy_failed = True
        if not copy_failed:
            chunk_size = _byte_count(chunk, _COPY_CHUNK_BYTES)
            if chunk_size <= 0 or chunk_size > remaining:
                copy_failed = True
            elif not _write_all(destination_fd, chunk):
                copy_failed = True
            else:
                hash_copy_state.update(chunk)
                total += chunk_size
                remaining -= chunk_size
    sync_failed = False
    if not copy_failed:
        try:
            os.fchmod(destination_fd, 384)
            os.fsync(destination_fd)
        except OSError:
            sync_failed = True
    destination_close_failed = _close_fd(destination_fd)
    after_fd_stat = None
    fd_stat_failed = False
    try:
        after_fd_stat = os.fstat(source_fd)
    except OSError:
        fd_stat_failed = True
    after_entry = _entry_stat(source_parent_fd, source_name)
    mutated = False
    if after_entry[2] is not None:
        return (None, None, after_entry[2])
    if fd_stat_failed or after_fd_stat is None or not after_entry[1]:
        mutated = True
    elif after_fd_stat.st_dev != source_stat.st_dev or after_fd_stat.st_ino != source_stat.st_ino:
        mutated = True
    elif after_entry[0].st_dev != source_stat.st_dev or after_entry[0].st_ino != source_stat.st_ino:
        mutated = True
    elif after_fd_stat.st_mode != source_stat.st_mode:
        mutated = True
    elif after_entry[0].st_mode != source_stat.st_mode:
        mutated = True
    elif after_fd_stat.st_uid != source_stat.st_uid:
        mutated = True
    elif after_entry[0].st_uid != source_stat.st_uid:
        mutated = True
    elif after_fd_stat.st_nlink != source_stat.st_nlink:
        mutated = True
    elif after_entry[0].st_nlink != source_stat.st_nlink:
        mutated = True
    elif (
        after_fd_stat.st_size != source_stat.st_size
        or after_entry[0].st_size != source_stat.st_size
    ):
        mutated = True
    elif after_fd_stat.st_mtime_ns != source_stat.st_mtime_ns:
        mutated = True
    elif after_fd_stat.st_ctime_ns != source_stat.st_ctime_ns:
        mutated = True
    elif after_entry[0].st_mtime_ns != source_stat.st_mtime_ns:
        mutated = True
    elif after_entry[0].st_ctime_ns != source_stat.st_ctime_ns:
        mutated = True
    if copy_failed or sync_failed or destination_close_failed:
        return (None, None, RevisionStoreErrorCode.IO_ERROR)
    if mutated:
        return (None, None, RevisionStoreErrorCode.CORRUPT_CONTENT)
    return (hash_copy_state.hexdigest(), total, None)


def _best_unlink(parent_fd, name):
    entry = _entry_stat(parent_fd, name)
    if entry[2] is not None:
        return True
    if not entry[1]:
        return False
    failed = False
    try:
        os.unlink(name, dir_fd=parent_fd)
    except OSError:
        failed = True
    return failed


def _best_rmdir(parent_fd, name):
    failed = False
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError:
        failed = True
    return failed


def _cleanup_initial(root_fd, temp_name, revision_name):
    project_fd = None
    failed = False
    try:
        project_fd = os.open(temp_name, _root_flags(), dir_fd=root_fd)
    except OSError:
        failed = True
    if failed or project_fd is None:
        return _best_rmdir(root_fd, temp_name)
    revisions_fd = None
    candidates_fd = None
    try:
        revisions_fd = os.open("revisions", _root_flags(), dir_fd=project_fd)
    except OSError:
        pass
    try:
        candidates_fd = os.open("candidates", _root_flags(), dir_fd=project_fd)
    except OSError:
        pass
    if revisions_fd is not None:
        revision_fd = None
        try:
            revision_fd = os.open(revision_name, _root_flags(), dir_fd=revisions_fd)
        except OSError:
            pass
        if revision_fd is not None:
            failed = _best_unlink(revision_fd, "model.FCStd") or failed
            failed = _best_unlink(revision_fd, "manifest.json") or failed
            failed = _close_fd(revision_fd) or failed
            failed = _best_rmdir(revisions_fd, revision_name) or failed
        failed = _close_fd(revisions_fd) or failed
        failed = _best_rmdir(project_fd, "revisions") or failed
    if candidates_fd is not None:
        failed = _close_fd(candidates_fd) or failed
        failed = _best_rmdir(project_fd, "candidates") or failed
    failed = _best_unlink(project_fd, "HEAD.json") or failed
    failed = _close_fd(project_fd) or failed
    failed = _best_rmdir(root_fd, temp_name) or failed
    try:
        os.fsync(root_fd)
    except OSError:
        failed = True
    return failed


def _require_mutation(store, project_id, lease):
    code = _identifier_code(project_id, _PROJECT_PATTERN)
    if code is not None:
        return code
    if os.getpid() != store._pid:
        return RevisionStoreErrorCode.INVALID_LEASE
    if type(lease) is not ProjectWriteLease:
        return RevisionStoreErrorCode.INVALID_LEASE
    if lease._issuer is not store._lease_manager:
        return RevisionStoreErrorCode.INVALID_LEASE
    if lease._seal is not store._lease_manager._seal:
        return RevisionStoreErrorCode.INVALID_LEASE
    if lease.project_id != project_id or type(lease.released) is not bool or lease.released:
        return RevisionStoreErrorCode.INVALID_LEASE
    return None


def _open_store_root(store):
    return _open_root(store._parts, store._identity)


def _reservation_key_digest(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) is None:
        return (None, RevisionStoreErrorCode.INVALID_INPUT)
    encoded = bytes(value, "utf-8")
    return (hashlib.sha256(_RESERVATION_KEY_DOMAIN + encoded).hexdigest(), None)


def _reservation_body(
    kind,
    project_id,
    expected_head,
    revision_id,
    key_sha256,
    ceiling_files,
    state,
    project_temp,
    revision_temp,
):
    head_value = None
    if expected_head is not None:
        head_value = _head_mapping(expected_head)
    ceiling = _CANDIDATE_RESERVATION_BYTES
    if kind == "generation_zero":
        ceiling = _GENERATION_ZERO_RESERVATION_BYTES
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": kind,
        "project_id": project_id,
        "expected_head": head_value,
        "revision_id": revision_id,
        "key_sha256": key_sha256,
        "ceiling_bytes": ceiling,
        "ceiling_files": ceiling_files,
        "state": state,
        "project_temp": project_temp,
        "revision_temp": revision_temp,
    }


def _parse_reservation_body(body):
    expected = (
        "schema_version",
        "kind",
        "project_id",
        "expected_head",
        "revision_id",
        "key_sha256",
        "ceiling_bytes",
        "ceiling_files",
        "state",
        "project_temp",
        "revision_temp",
    )
    if not _mapping_has_exact(body, expected):
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if body["schema_version"] != _SCHEMA_VERSION:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    kind = body["kind"]
    if kind != "generation_zero" and kind != "candidate":
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if _identifier_code(body["project_id"], _PROJECT_PATTERN) is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if _identifier_code(body["revision_id"], _REVISION_PATTERN) is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if _digest_code(body["key_sha256"]) is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    expected_head = None
    if body["expected_head"] is not None:
        try:
            expected_head = _head_from_mapping(body["expected_head"])
        except RevisionStoreError:
            return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    ceiling = body["ceiling_bytes"]
    ceiling_files = body["ceiling_files"]
    if kind == "generation_zero":
        if (
            expected_head is not None
            or ceiling != _GENERATION_ZERO_RESERVATION_BYTES
            or ceiling_files not in {4, 5}
        ):
            return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    elif (
        expected_head is None
        or expected_head.project_id != body["project_id"]
        or ceiling != _CANDIDATE_RESERVATION_BYTES
        or ceiling_files not in {8, 9}
    ):
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if body["state"] not in {"reserved", "staged", "publishing", "published"}:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    for temp_value in (body["project_temp"], body["revision_temp"]):
        if temp_value is not None:
            if type(temp_value) is not str:
                return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
            if re.fullmatch(r"\.(?:project|revision)\.[0-9a-f]{32}\.tmp", temp_value) is None:
                return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if kind == "generation_zero":
        if body["project_temp"] is None or body["revision_temp"] is not None:
            return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    else:
        if body["project_temp"] is not None:
            return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
        if body["state"] == "publishing" and body["revision_temp"] is None:
            return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
        if body["state"] != "publishing" and body["revision_temp"] is not None:
            return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    parsed = dict(body)
    parsed["expected_head"] = expected_head
    return (parsed, None)


def _quota_entry_allowed(relative, name, is_directory):
    depth = len(relative)
    if depth == 0:
        if is_directory and name == _QUOTA_DIRECTORY:
            return True
        if is_directory and re.fullmatch(r"[0-9a-f]{64}", name) is not None:
            return True
        return bool(
            is_directory and re.fullmatch(r"\.project\.[0-9a-f]{32}\.tmp", name) is not None
        )
    if relative == (_QUOTA_DIRECTORY,):
        return is_directory and name == _RESERVATIONS_DIRECTORY
    if relative == (_QUOTA_DIRECTORY, _RESERVATIONS_DIRECTORY):
        return is_directory and re.fullmatch(r"[0-9a-f]{64}", name) is not None
    if len(relative) == 3 and relative[:2] == (
        _QUOTA_DIRECTORY,
        _RESERVATIONS_DIRECTORY,
    ):
        if is_directory:
            return False
        if name == _RESERVATION_RECORD:
            return True
        return re.fullmatch(r"\.reservation\.json\.[0-9a-f]{32}\.tmp", name) is not None
    top = relative[0]
    project_like = re.fullmatch(r"[0-9a-f]{64}", top) is not None
    project_temp = re.fullmatch(r"\.project\.[0-9a-f]{32}\.tmp", top) is not None
    if not project_like and not project_temp:
        return False
    if depth == 1:
        if is_directory:
            return name == "revisions" or name == "candidates"
        if name == "HEAD.json" or name == "journal.json":
            return True
        return re.fullmatch(r"\.(?:HEAD|journal)\.json\.[0-9a-f]{32}\.tmp", name) is not None
    if depth == 2 and relative[1] == "revisions":
        if not is_directory:
            return False
        if re.fullmatch(r"[0-9a-f]{64}", name) is not None:
            return True
        return re.fullmatch(r"\.revision\.[0-9a-f]{32}\.tmp", name) is not None
    if depth == 2 and relative[1] == "candidates":
        return is_directory and re.fullmatch(r"[0-9a-f]{64}", name) is not None
    if depth == 3 and relative[1] == "revisions":
        return not is_directory and name in {"model.FCStd", "model.step", "manifest.json"}
    if depth == 3 and relative[1] == "candidates":
        return not is_directory and name in {
            "model.FCStd",
            "model.step",
            _SEED_INTENT_RECORD,
            _SEED_BINDING_RECORD,
        }
    return False


def _quota_path_owner(relative, prefix_owner, journal_owner):
    owner = None
    prefix_length = 1
    while prefix_length <= len(relative):
        revision_id = prefix_owner.get(relative[:prefix_length])
        if revision_id is not None:
            if owner is not None and owner != revision_id:
                return _QUOTA_OWNER_CONFLICT
            owner = revision_id
        prefix_length += 1
    if len(relative) == 2 and relative[0] in journal_owner:
        name = relative[1]
        if (
            name == "journal.json"
            or re.fullmatch(
                r"\.journal\.json\.[0-9a-f]{32}\.tmp",
                name,
            )
            is not None
        ):
            journal_revision = journal_owner[relative[0]]
            if owner is not None and owner != journal_revision:
                return _QUOTA_OWNER_CONFLICT
            owner = journal_revision
    return owner


def _quota_temporary_path(relative):
    if not relative:
        return False
    name = relative[-1]
    if re.fullmatch(r"\.(?:project|revision)\.[0-9a-f]{32}\.tmp", name) is not None:
        return True
    return (
        re.fullmatch(
            r"\.(?:reservation|HEAD|journal)\.json\.[0-9a-f]{32}\.tmp",
            name,
        )
        is not None
    )


def _scan_quota_tree(
    directory_fd,
    root_device,
    relative,
    snapshot,
):
    if len(relative) > 4:
        return RevisionStoreErrorCode.UNSAFE_STORE
    iterator = None
    code = None
    try:
        try:
            iterator = os.scandir(directory_fd)
            for entry in iterator:
                name = entry.name
                if type(name) is not str or name == "." or name == "..":
                    code = RevisionStoreErrorCode.UNSAFE_STORE
                    break
                child_relative = relative + (name,)
                entry_stat = entry.stat(follow_symlinks=False)
                is_directory = stat.S_ISDIR(entry_stat.st_mode)
                is_file = stat.S_ISREG(entry_stat.st_mode)
                if not is_directory and not is_file:
                    code = RevisionStoreErrorCode.UNSAFE_STORE
                    break
                if entry_stat.st_uid != os.geteuid() or entry_stat.st_dev != root_device:
                    code = RevisionStoreErrorCode.UNSAFE_STORE
                    break
                if is_directory and stat.S_IMODE(entry_stat.st_mode) != 448:
                    code = RevisionStoreErrorCode.UNSAFE_STORE
                    break
                if not _quota_entry_allowed(relative, name, is_directory):
                    code = RevisionStoreErrorCode.UNSAFE_STORE
                    break
                if is_file:
                    candidate_file = len(relative) == 3 and relative[1] == "candidates"
                    if entry_stat.st_nlink != 1:
                        code = RevisionStoreErrorCode.UNSAFE_STORE
                        break
                    if candidate_file:
                        if stat.S_IMODE(entry_stat.st_mode) & 18:
                            code = RevisionStoreErrorCode.UNSAFE_STORE
                            break
                    elif stat.S_IMODE(entry_stat.st_mode) != 384:
                        code = RevisionStoreErrorCode.UNSAFE_STORE
                        break
                    if entry_stat.st_size < 0:
                        code = RevisionStoreErrorCode.UNSAFE_STORE
                        break
                    snapshot["bytes"] += entry_stat.st_size
                    snapshot["files"] += 1
                    snapshot["file_sizes"][child_relative] = entry_stat.st_size
                    if _quota_temporary_path(child_relative):
                        snapshot["temporary_entries"][child_relative] = True
                    if snapshot["files"] > _MAX_ORDINARY_FILES:
                        snapshot["over_limit"] = True
                else:
                    category = None
                    if len(relative) == 0 and (
                        re.fullmatch(r"[0-9a-f]{64}", name) is not None
                        or re.fullmatch(r"\.project\.[0-9a-f]{32}\.tmp", name) is not None
                    ):
                        category = "projects"
                        snapshot["projects"] += 1
                    if len(relative) == 2 and relative[1] == "revisions":
                        if (
                            re.fullmatch(r"[0-9a-f]{64}", name) is not None
                            or re.fullmatch(r"\.revision\.[0-9a-f]{32}\.tmp", name) is not None
                        ):
                            category = "revisions"
                            snapshot["revisions"] += 1
                    if (
                        len(relative) == 2
                        and relative[1] == "candidates"
                        and re.fullmatch(r"[0-9a-f]{64}", name) is not None
                    ) or relative == (_QUOTA_DIRECTORY, _RESERVATIONS_DIRECTORY):
                        category = "candidate_reservations"
                        snapshot["candidate_reservations"] += 1
                    if category is not None:
                        snapshot["directory_categories"][child_relative] = category
                    if _quota_temporary_path(child_relative):
                        snapshot["temporary_entries"][child_relative] = True
                    child_fd = None
                    close_failed = False
                    try:
                        child_fd = os.open(name, _root_flags(), dir_fd=directory_fd)
                        child_code = _discovery_directory_pin_code(
                            directory_fd,
                            name,
                            child_fd,
                            entry_stat,
                            root_device,
                        )
                        if child_code is None:
                            child_code = _scan_quota_tree(
                                child_fd,
                                root_device,
                                child_relative,
                                snapshot,
                            )
                        if child_code is None:
                            child_code = _discovery_directory_pin_code(
                                directory_fd,
                                name,
                                child_fd,
                                entry_stat,
                                root_device,
                            )
                    except OSError:
                        child_code = RevisionStoreErrorCode.UNSAFE_STORE
                    finally:
                        if child_fd is not None:
                            close_failed = _close_fd(child_fd)
                    if child_code is not None:
                        code = child_code
                        break
                    if close_failed:
                        code = RevisionStoreErrorCode.IO_ERROR
                        break
        except OSError:
            code = RevisionStoreErrorCode.IO_ERROR
    finally:
        if iterator is not None:
            try:
                iterator.close()
            except OSError:
                if code is None:
                    code = RevisionStoreErrorCode.IO_ERROR
    if code is not None:
        return code
    if snapshot["bytes"] > _MAX_STORE_BYTES:
        snapshot["over_limit"] = True
    if snapshot["projects"] > _MAX_PROJECTS:
        snapshot["over_limit"] = True
    if snapshot["revisions"] > _MAX_REVISIONS:
        snapshot["over_limit"] = True
    if snapshot["candidate_reservations"] > _MAX_CANDIDATES_AND_RESERVATIONS:
        snapshot["over_limit"] = True
    return None


def _open_quota_directories(root_fd, root_device, create):
    quota_fd = None
    reservations_fd = None
    handed_off = False
    try:
        quota_stat = _entry_stat(root_fd, _QUOTA_DIRECTORY)
        if quota_stat[2] is not None:
            return (None, None, quota_stat[2])
        if not quota_stat[1]:
            if not create:
                return (None, None, None)
            try:
                os.mkdir(_QUOTA_DIRECTORY, 448, dir_fd=root_fd)
                os.fsync(root_fd)
            except OSError:
                return (None, None, RevisionStoreErrorCode.IO_ERROR)
        quota_open = _open_safe_directory(
            root_fd,
            _QUOTA_DIRECTORY,
            root_device,
            RevisionStoreErrorCode.UNSAFE_STORE,
        )
        if quota_open[1] is not None:
            return (None, None, quota_open[1])
        quota_fd = quota_open[0]
        reservations_stat = _entry_stat(quota_fd, _RESERVATIONS_DIRECTORY)
        if reservations_stat[2] is not None:
            return (None, None, reservations_stat[2])
        if not reservations_stat[1]:
            if not create:
                return (None, None, RevisionStoreErrorCode.UNSAFE_STORE)
            try:
                os.mkdir(_RESERVATIONS_DIRECTORY, 448, dir_fd=quota_fd)
                os.fsync(quota_fd)
            except OSError:
                return (None, None, RevisionStoreErrorCode.IO_ERROR)
        reservations_open = _open_safe_directory(
            quota_fd,
            _RESERVATIONS_DIRECTORY,
            root_device,
            RevisionStoreErrorCode.UNSAFE_STORE,
        )
        if reservations_open[1] is not None:
            return (None, None, reservations_open[1])
        reservations_fd = reservations_open[0]
        handed_off = True
        return (quota_fd, reservations_fd, None)
    finally:
        if not handed_off:
            if reservations_fd is not None:
                _close_fd(reservations_fd)
            if quota_fd is not None:
                _close_fd(quota_fd)


def _load_reservations(root_fd, root_device):
    opened = _open_quota_directories(root_fd, root_device, False)
    if opened[2] is not None:
        return (None, opened[2])
    if opened[0] is None:
        return ((), None)
    quota_fd = opened[0]
    reservations_fd = opened[1]
    quota_initial = _discovery_directory_stat(quota_fd, root_device)
    reservations_initial = _discovery_directory_stat(reservations_fd, root_device)
    if quota_initial[1] is not None or reservations_initial[1] is not None:
        close_failed = _close_two(reservations_fd, quota_fd)
        code = quota_initial[1]
        if code is None:
            code = reservations_initial[1]
        if close_failed and code is None:
            code = RevisionStoreErrorCode.IO_ERROR
        return (None, code)
    values = ()
    iterator = None
    code = None
    close_failed = False
    try:
        try:
            iterator = os.scandir(reservations_fd)
            for entry in iterator:
                name = entry.name
                entry_stat = entry.stat(follow_symlinks=False)
                if (
                    type(name) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", name) is None
                    or not _safe_directory_stat(entry_stat, root_device)
                ):
                    code = RevisionStoreErrorCode.UNSAFE_STORE
                    break
                reservation_open = _open_safe_directory(
                    reservations_fd,
                    name,
                    root_device,
                    RevisionStoreErrorCode.CORRUPT_RECORD,
                )
                if reservation_open[1] is not None:
                    code = reservation_open[1]
                    break
                reservation_close_failed = False
                try:
                    raw = _read_bounded_file(
                        reservation_open[0],
                        _RESERVATION_RECORD,
                        root_device,
                        _MAX_JOURNAL_BYTES,
                        RevisionStoreErrorCode.CORRUPT_RECORD,
                    )
                    if raw[1] is None:
                        parsed_record = _parse_checked_record(
                            raw[0],
                            _RESERVATION_CHECKSUM_DOMAIN,
                            _MAX_JOURNAL_BYTES,
                        )
                        if parsed_record[1] is None:
                            parsed = _parse_reservation_body(parsed_record[0])
                            if parsed[1] is None:
                                if _revision_key(parsed[0]["revision_id"]) != name:
                                    code = RevisionStoreErrorCode.CORRUPT_RECORD
                                else:
                                    values = values + (parsed[0],)
                            else:
                                code = parsed[1]
                        else:
                            code = parsed_record[1]
                    else:
                        code = raw[1]
                    if code is None:
                        code = _discovery_directory_pin_code(
                            reservations_fd,
                            name,
                            reservation_open[0],
                            entry_stat,
                            root_device,
                        )
                finally:
                    reservation_close_failed = _close_fd(reservation_open[0])
                if code is not None or reservation_close_failed:
                    if code is None:
                        code = RevisionStoreErrorCode.IO_ERROR
                    break
        except OSError:
            code = RevisionStoreErrorCode.IO_ERROR
    finally:
        try:
            if iterator is not None:
                try:
                    iterator.close()
                except OSError:
                    if code is None:
                        code = RevisionStoreErrorCode.IO_ERROR
        finally:
            if code is None:
                code = _discovery_directory_pin_code(
                    quota_fd,
                    _RESERVATIONS_DIRECTORY,
                    reservations_fd,
                    reservations_initial[0],
                    root_device,
                )
            if code is None:
                code = _discovery_directory_pin_code(
                    root_fd,
                    _QUOTA_DIRECTORY,
                    quota_fd,
                    quota_initial[0],
                    root_device,
                )
            close_failed = _close_two(reservations_fd, quota_fd)
    if code is not None:
        return (None, code)
    if close_failed:
        return (None, RevisionStoreErrorCode.IO_ERROR)
    return (values, None)


def _reservation_prefixes(reservation):
    revision_key = _revision_key(reservation["revision_id"])
    prefixes = ((_QUOTA_DIRECTORY, _RESERVATIONS_DIRECTORY, revision_key),)
    if reservation["kind"] == "generation_zero":
        prefixes = prefixes + ((reservation["project_temp"],),)
        if reservation["state"] in {"publishing", "published"}:
            prefixes = prefixes + ((_project_key(reservation["project_id"]),),)
    else:
        project_key = _project_key(reservation["project_id"])
        prefixes = prefixes + (
            (project_key, "candidates", _candidate_key(reservation["revision_id"])),
        )
        if reservation["revision_temp"] is not None:
            prefixes = prefixes + ((project_key, "revisions", reservation["revision_temp"]),)
        if reservation["state"] in {"publishing", "published"}:
            prefixes = prefixes + ((project_key, "revisions", revision_key),)
    return prefixes


def _quota_snapshot(root_fd, root_device, reservations):
    prefix_owner = {}
    journal_owner = {}
    for reservation in reservations:
        revision_id = reservation["revision_id"]
        for prefix in _reservation_prefixes(reservation):
            if prefix in prefix_owner:
                return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
            prefix_owner[prefix] = revision_id
        if reservation["kind"] == "candidate":
            project_key = _project_key(reservation["project_id"])
            if project_key in journal_owner:
                return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
            journal_owner[project_key] = revision_id
    snapshot = {
        "bytes": 0,
        "files": 0,
        "projects": 0,
        "revisions": 0,
        "candidate_reservations": 0,
        "file_sizes": {},
        "directory_categories": {},
        "temporary_entries": {},
        "over_limit": False,
    }
    code = _scan_quota_tree(
        root_fd,
        root_device,
        (),
        snapshot,
    )
    if code is not None:
        return (None, code)
    observed_bytes = {}
    observed_files = {}
    observed_directories = {}
    for relative, size in snapshot["file_sizes"].items():
        owner = _quota_path_owner(relative, prefix_owner, journal_owner)
        if owner == _QUOTA_OWNER_CONFLICT:
            return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
        if owner is not None:
            observed_bytes[owner] = observed_bytes.get(owner, 0) + size
            observed_files[owner] = observed_files.get(owner, 0) + 1
    for relative, category in snapshot["directory_categories"].items():
        owner = _quota_path_owner(relative, prefix_owner, journal_owner)
        if owner == _QUOTA_OWNER_CONFLICT:
            return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
        if owner is not None:
            owned = observed_directories.get(owner)
            if owned is None:
                owned = {
                    "projects": 0,
                    "revisions": 0,
                    "candidate_reservations": 0,
                }
                observed_directories[owner] = owned
            owned[category] += 1
    observed_temporary_entries = {}
    unowned_temporary_entries = 0
    for relative in snapshot["temporary_entries"]:
        owner = _quota_path_owner(relative, prefix_owner, journal_owner)
        if owner == _QUOTA_OWNER_CONFLICT:
            return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
        if owner is None:
            unowned_temporary_entries += 1
        else:
            observed_temporary_entries[owner] = observed_temporary_entries.get(owner, 0) + 1
    snapshot["observed_bytes"] = observed_bytes
    snapshot["observed_files"] = observed_files
    snapshot["observed_directories"] = observed_directories
    snapshot["observed_temporary_entries"] = observed_temporary_entries
    snapshot["unowned_temporary_entries"] = unowned_temporary_entries
    return (snapshot, None)


def _acquire_quota_lease(store):
    attempt = 0
    while attempt < 250:
        attempt += 1
        try:
            return (store._lease_manager.acquire(_QUOTA_RESOURCE_ID), None)
        except LeaseError as error:
            if error.code is not LeaseErrorCode.CONTENDED:
                return (None, RevisionStoreErrorCode.IO_ERROR)
        time.sleep(0.001)
    return (None, RevisionStoreErrorCode.IO_ERROR)


def _release_quota_lease(quota_lease):
    try:
        quota_lease.release(owner_token=quota_lease.owner_token)
    except LeaseError:
        return RevisionStoreErrorCode.IO_ERROR
    return None


def _quota_create_initial_namespace(
    store,
    root_fd,
    temp_name,
    revision_name,
    with_model,
):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return quota[1]
    quota_lease = quota[0]
    project_fd = None
    revisions_fd = None
    candidates_fd = None
    revision_fd = None
    code = None
    close_failed = False
    release_code = None
    try:
        try:
            os.mkdir(temp_name, 448, dir_fd=root_fd)
            project_fd = os.open(temp_name, _root_flags(), dir_fd=root_fd)
            os.mkdir("revisions", 448, dir_fd=project_fd)
            os.mkdir("candidates", 448, dir_fd=project_fd)
            revisions_fd = os.open("revisions", _root_flags(), dir_fd=project_fd)
            candidates_fd = os.open("candidates", _root_flags(), dir_fd=project_fd)
            os.mkdir(revision_name, 448, dir_fd=revisions_fd)
            revision_fd = os.open(revision_name, _root_flags(), dir_fd=revisions_fd)
        except OSError:
            code = RevisionStoreErrorCode.IO_ERROR
        if code is None and with_model:
            code = _create_empty_file(revision_fd, "model.FCStd")
        if code is None:
            code = _create_empty_file(revision_fd, "manifest.json")
        if code is None:
            code = _create_empty_file(project_fd, "HEAD.json")
        if code is None:
            try:
                os.fsync(revision_fd)
                os.fsync(revisions_fd)
                os.fsync(candidates_fd)
                os.fsync(project_fd)
                os.fsync(root_fd)
            except OSError:
                code = RevisionStoreErrorCode.IO_ERROR
    finally:
        if revision_fd is not None:
            close_failed = _close_fd(revision_fd) or close_failed
        if candidates_fd is not None:
            close_failed = _close_fd(candidates_fd) or close_failed
        if revisions_fd is not None:
            close_failed = _close_fd(revisions_fd) or close_failed
        if project_fd is not None:
            close_failed = _close_fd(project_fd) or close_failed
        release_code = _release_quota_lease(quota_lease)
    if code is None and close_failed:
        code = RevisionStoreErrorCode.IO_ERROR
    if release_code is not None:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    return code


def _quota_create_candidate_namespace(
    store,
    candidates_fd,
    candidate_name,
    seed_intent_raw,
):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return quota[1]
    quota_lease = quota[0]
    candidate_fd = None
    code = None
    close_failed = False
    release_code = None
    try:
        try:
            os.mkdir(candidate_name, 448, dir_fd=candidates_fd)
            candidate_fd = os.open(candidate_name, _root_flags(), dir_fd=candidates_fd)
        except OSError:
            code = RevisionStoreErrorCode.IO_ERROR
        if code is None:
            code = _create_empty_file(candidate_fd, "model.FCStd")
        if code is None:
            code = _create_empty_file(candidate_fd, "model.step")
        if code is None and seed_intent_raw is not None:
            code = _create_durable_file(
                candidate_fd,
                _SEED_INTENT_RECORD,
                seed_intent_raw,
            )
        if code is None:
            try:
                os.fsync(candidate_fd)
                os.fsync(candidates_fd)
            except OSError:
                code = RevisionStoreErrorCode.IO_ERROR
    finally:
        if candidate_fd is not None:
            close_failed = _close_fd(candidate_fd)
        release_code = _release_quota_lease(quota_lease)
    if close_failed and code is None:
        code = RevisionStoreErrorCode.IO_ERROR
    if release_code is not None:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    return code


def _quota_create_revision_namespace(store, revisions_fd, temp_name):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return quota[1]
    quota_lease = quota[0]
    revision_fd = None
    code = None
    close_failed = False
    release_code = None
    try:
        try:
            os.mkdir(temp_name, 448, dir_fd=revisions_fd)
            revision_fd = os.open(temp_name, _root_flags(), dir_fd=revisions_fd)
        except OSError:
            code = RevisionStoreErrorCode.IO_ERROR
        if code is None:
            code = _create_empty_file(revision_fd, "model.FCStd")
        if code is None:
            code = _create_empty_file(revision_fd, "model.step")
        if code is None:
            code = _create_empty_file(revision_fd, "manifest.json")
        if code is None:
            try:
                os.fsync(revision_fd)
                os.fsync(revisions_fd)
            except OSError:
                code = RevisionStoreErrorCode.IO_ERROR
    finally:
        if revision_fd is not None:
            close_failed = _close_fd(revision_fd)
        release_code = _release_quota_lease(quota_lease)
    if close_failed and code is None:
        code = RevisionStoreErrorCode.IO_ERROR
    if release_code is not None:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    return code


def _quota_replace_record(store, parent_fd, filename, raw, token, uncertainty):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return quota[1]
    code = None
    release_code = None
    try:
        code = _replace_durable_record(parent_fd, filename, raw, token, uncertainty)
    finally:
        release_code = _release_quota_lease(quota[0])
    if release_code is not None:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    return code


def _quota_unlink_file(store, parent_fd, name):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return True
    failed = False
    release_code = None
    try:
        failed = _best_unlink(parent_fd, name)
        if not failed:
            try:
                os.fsync(parent_fd)
            except OSError:
                failed = True
    finally:
        release_code = _release_quota_lease(quota[0])
    return failed or release_code is not None


def _quota_rename_directory(
    store,
    parent_fd,
    source_name,
    target_name,
    uncertainty,
):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return (quota[1], False)
    code = None
    renamed = False
    release_code = None
    try:
        try:
            os.rename(source_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            renamed = True
            os.fsync(parent_fd)
        except OSError:
            code = RevisionStoreErrorCode.IO_ERROR
            if renamed:
                code = uncertainty
    finally:
        release_code = _release_quota_lease(quota[0])
    if release_code is not None:
        code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    return (code, renamed)


def _quota_cleanup_initial(store, root_fd, temp_name, revision_name):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return True
    failed = False
    release_code = None
    try:
        failed = _cleanup_initial(root_fd, temp_name, revision_name)
    finally:
        release_code = _release_quota_lease(quota[0])
    return failed or release_code is not None


def _quota_cleanup_candidate(store, candidates_fd, candidate_name, root_device):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return True
    failed = False
    release_code = None
    try:
        failed = _cleanup_candidate_dir(candidates_fd, candidate_name, root_device)
    finally:
        release_code = _release_quota_lease(quota[0])
    return failed or release_code is not None


def _quota_cleanup_revision(store, revisions_fd, temp_name):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return True
    failed = False
    release_code = None
    try:
        failed = _cleanup_revision_temp(revisions_fd, temp_name)
    finally:
        release_code = _release_quota_lease(quota[0])
    return failed or release_code is not None


def _reservation_admission_code(snapshot, reservations, kind, new_files):
    if snapshot["over_limit"]:
        return RevisionStoreErrorCode.RESOURCE_EXHAUSTED
    unreserved_bytes = snapshot["bytes"]
    active_ceilings = 0
    unreserved_files = snapshot["files"]
    future_files = 0
    unreserved_projects = snapshot["projects"]
    active_projects = 0
    unreserved_revisions = snapshot["revisions"]
    active_revisions = 0
    unreserved_candidate_dirs = snapshot["candidate_reservations"]
    active_candidate_dirs = 0
    for reservation in reservations:
        revision_id = reservation["revision_id"]
        observed_bytes = snapshot["observed_bytes"].get(revision_id, 0)
        observed_files = snapshot["observed_files"].get(revision_id, 0)
        if observed_bytes > reservation["ceiling_bytes"]:
            return RevisionStoreErrorCode.RESOURCE_EXHAUSTED
        if observed_files > reservation["ceiling_files"]:
            return RevisionStoreErrorCode.RESOURCE_EXHAUSTED
        unreserved_bytes -= observed_bytes
        active_ceilings += reservation["ceiling_bytes"]
        unreserved_files -= observed_files
        observed_directories = snapshot["observed_directories"].get(revision_id, {})
        unreserved_projects -= observed_directories.get("projects", 0)
        unreserved_revisions -= observed_directories.get("revisions", 0)
        unreserved_candidate_dirs -= observed_directories.get(
            "candidate_reservations",
            0,
        )
        maximum_files = reservation["ceiling_files"]
        if reservation["kind"] == "generation_zero":
            active_projects += 1
            active_candidate_dirs += 1
        else:
            active_candidate_dirs += 2
        active_revisions += 1
        future_files += maximum_files
    new_ceiling = 0
    new_candidate_dirs = 0
    new_projects = 0
    new_revisions = 0
    if kind == "generation_zero":
        new_ceiling = _GENERATION_ZERO_RESERVATION_BYTES
        new_candidate_dirs = 1
        new_projects = 1
        new_revisions = 1
    elif kind == "candidate":
        new_ceiling = _CANDIDATE_RESERVATION_BYTES
        new_candidate_dirs = 2
        new_revisions = 1
    if unreserved_bytes + active_ceilings + new_ceiling > _MAX_STORE_BYTES:
        return RevisionStoreErrorCode.RESOURCE_EXHAUSTED
    if unreserved_projects + active_projects + new_projects > _MAX_PROJECTS:
        return RevisionStoreErrorCode.RESOURCE_EXHAUSTED
    if unreserved_revisions + active_revisions + new_revisions > _MAX_REVISIONS:
        return RevisionStoreErrorCode.RESOURCE_EXHAUSTED
    if (
        unreserved_candidate_dirs + active_candidate_dirs + new_candidate_dirs
        > _MAX_CANDIDATES_AND_RESERVATIONS
    ):
        return RevisionStoreErrorCode.RESOURCE_EXHAUSTED
    if unreserved_files + future_files + new_files > _MAX_ORDINARY_FILES:
        return RevisionStoreErrorCode.RESOURCE_EXHAUSTED
    return None


def _reservation_release_code(snapshot, reservations, reservation):
    code = _reservation_admission_code(snapshot, reservations, None, 0)
    if code is not None:
        return code
    revision_id = reservation["revision_id"]
    if snapshot["unowned_temporary_entries"] != 0:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    if snapshot["observed_temporary_entries"].get(revision_id, 0) != 0:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    owned = snapshot["observed_directories"].get(revision_id, {})
    if owned.get("candidate_reservations", 0) != 1:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    if reservation["kind"] == "generation_zero":
        if reservation["state"] == "published":
            if owned.get("projects", 0) != 1 or owned.get("revisions", 0) != 1:
                return RevisionStoreErrorCode.RECOVERY_REQUIRED
        elif owned.get("projects", 0) != 0 or owned.get("revisions", 0) != 0:
            return RevisionStoreErrorCode.RECOVERY_REQUIRED
    elif reservation["state"] == "published":
        if owned.get("revisions", 0) != 1:
            return RevisionStoreErrorCode.RECOVERY_REQUIRED
    elif reservation["state"] == "publishing":
        if owned.get("revisions", 0) not in {0, 1}:
            return RevisionStoreErrorCode.RECOVERY_REQUIRED
    elif owned.get("revisions", 0) != 0:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    return None


def _create_reservation_record(root_fd, root_device, reservation):
    quota_fd = None
    reservations_fd = None
    reservation_fd = None
    reservation_name = None
    code = None
    cleanup_failed = False
    close_failed = False
    try:
        opened = _open_quota_directories(root_fd, root_device, True)
        code = opened[2]
        if code is None:
            quota_fd = opened[0]
            reservations_fd = opened[1]
            reservation_name = _revision_key(reservation["revision_id"])
        if code is None:
            try:
                os.mkdir(reservation_name, 448, dir_fd=reservations_fd)
                os.fsync(reservations_fd)
            except OSError:
                code = RevisionStoreErrorCode.IO_ERROR
        if code is None:
            reservation_open = _open_safe_directory(
                reservations_fd,
                reservation_name,
                root_device,
                RevisionStoreErrorCode.IO_ERROR,
            )
            code = reservation_open[1]
            reservation_fd = reservation_open[0]
        if code is None:
            raw = _checked_record_bytes(reservation, _RESERVATION_CHECKSUM_DOMAIN)
            code = _create_durable_file(reservation_fd, _RESERVATION_RECORD, raw)
        if code is None:
            try:
                os.fsync(reservation_fd)
                os.fsync(reservations_fd)
            except OSError:
                code = RevisionStoreErrorCode.IO_ERROR
        if code is not None and reservation_fd is not None:
            cleanup_failed = _best_unlink(
                reservation_fd,
                _RESERVATION_RECORD,
            )
            try:
                os.fsync(reservation_fd)
            except OSError:
                cleanup_failed = True
        if reservation_fd is not None:
            close_failed = _close_fd(reservation_fd)
            reservation_fd = None
        if code is not None and reservations_fd is not None and reservation_name is not None:
            cleanup_failed = _best_rmdir(reservations_fd, reservation_name) or cleanup_failed
            try:
                os.fsync(reservations_fd)
            except OSError:
                cleanup_failed = True
    finally:
        if reservation_fd is not None:
            close_failed = _close_fd(reservation_fd) or close_failed
        if reservations_fd is not None:
            close_failed = _close_fd(reservations_fd) or close_failed
        if quota_fd is not None:
            close_failed = _close_fd(quota_fd) or close_failed
    if code is not None and cleanup_failed:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    if code is None and close_failed:
        code = RevisionStoreErrorCode.IO_ERROR
    return code


def _reserve_quota(
    store,
    kind,
    project_id,
    expected_head,
    revision_id,
    reservation_key,
    project_temp,
    ceiling_files,
):
    key_result = _reservation_key_digest(reservation_key)
    if key_result[1] is not None:
        return (None, False, key_result[1])
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return (None, False, quota[1])
    quota_lease = quota[0]
    root_fd = None
    root_device = None
    code = None
    reservations = None
    existing = None
    close_failed = False
    release_code = None
    try:
        root_open = _open_store_root(store)
        code = root_open[2]
        if code is None:
            root_fd = root_open[0]
            root_device = root_open[1].st_dev
            reservations_result = _load_reservations(root_fd, root_device)
            code = reservations_result[1]
            reservations = reservations_result[0]
        if code is None:
            for reservation in reservations:
                if reservation["project_id"] == project_id:
                    if (
                        reservation["kind"] == kind
                        and reservation["key_sha256"] == key_result[0]
                        and reservation["expected_head"] == expected_head
                        and reservation["ceiling_files"] == ceiling_files
                    ):
                        existing = reservation
                    else:
                        code = RevisionStoreErrorCode.CONFLICT
                    break
        if code is None and existing is None:
            snapshot_result = _quota_snapshot(
                root_fd,
                root_device,
                reservations,
            )
            code = snapshot_result[1]
            if code is None:
                code = _reservation_admission_code(
                    snapshot_result[0],
                    reservations,
                    kind,
                    ceiling_files,
                )
        if code is None and existing is None:
            reservation = _reservation_body(
                kind,
                project_id,
                expected_head,
                revision_id,
                key_result[0],
                ceiling_files,
                "reserved",
                project_temp,
                None,
            )
            code = _create_reservation_record(
                root_fd,
                root_device,
                reservation,
            )
            if code is None:
                existing = reservation
    finally:
        if root_fd is not None:
            close_failed = _close_fd(root_fd)
        release_code = _release_quota_lease(quota_lease)
    if code is None and (close_failed or release_code is not None):
        code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    if code is not None:
        return (None, False, code)
    return (existing, existing["revision_id"] != revision_id, None)


def _read_reservation_for_update(root_fd, root_device, revision_id):
    quota_fd = None
    reservations_fd = None
    reservation_fd = None
    handed_off = False
    try:
        opened = _open_quota_directories(root_fd, root_device, False)
        if opened[2] is not None or opened[0] is None:
            code = opened[2]
            if code is None:
                code = RevisionStoreErrorCode.NOT_FOUND
            return (None, None, None, code)
        quota_fd = opened[0]
        reservations_fd = opened[1]
        reservation_open = _open_safe_directory(
            reservations_fd,
            _revision_key(revision_id),
            root_device,
            RevisionStoreErrorCode.NOT_FOUND,
        )
        if reservation_open[1] is not None:
            return (None, None, None, reservation_open[1])
        reservation_fd = reservation_open[0]
        raw = _read_bounded_file(
            reservation_fd,
            _RESERVATION_RECORD,
            root_device,
            _MAX_JOURNAL_BYTES,
            RevisionStoreErrorCode.RECOVERY_REQUIRED,
        )
        parsed = None
        code = raw[1]
        if code is None:
            checked = _parse_checked_record(
                raw[0],
                _RESERVATION_CHECKSUM_DOMAIN,
                _MAX_JOURNAL_BYTES,
            )
            code = checked[1]
            if code is None:
                parsed_result = _parse_reservation_body(checked[0])
                parsed = parsed_result[0]
                code = parsed_result[1]
        if code is None and parsed["revision_id"] != revision_id:
            code = RevisionStoreErrorCode.CORRUPT_RECORD
        if code is not None:
            return (None, None, None, code)
        handed_off = True
        return (
            parsed,
            (quota_fd, reservations_fd, reservation_fd),
            raw[0],
            None,
        )
    finally:
        if not handed_off:
            if reservation_fd is not None:
                _close_fd(reservation_fd)
            if reservations_fd is not None:
                _close_fd(reservations_fd)
            if quota_fd is not None:
                _close_fd(quota_fd)


def _reservation_binding_code(
    reservation,
    kind,
    project_id,
    expected_head,
    reservation_key,
):
    key_result = _reservation_key_digest(reservation_key)
    if key_result[1] is not None:
        return key_result[1]
    if (
        reservation["kind"] != kind
        or reservation["project_id"] != project_id
        or reservation["expected_head"] != expected_head
        or reservation["key_sha256"] != key_result[0]
    ):
        return RevisionStoreErrorCode.CONFLICT
    return None


def _replace_reservation(store, revision_id, transform):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return (None, quota[1])
    quota_lease = quota[0]
    root_fd = None
    root_device = None
    opened = None
    code = None
    replacement = None
    replacement_body = None
    close_failed = False
    release_code = None
    try:
        root_open = _open_store_root(store)
        code = root_open[2]
        if code is None:
            root_fd = root_open[0]
            root_device = root_open[1].st_dev
            opened = _read_reservation_for_update(
                root_fd,
                root_device,
                revision_id,
            )
            code = opened[3]
        if code is None:
            replacement_body = transform(opened[0])
            parsed = _parse_reservation_body(replacement_body)
            if parsed[1] is not None:
                code = RevisionStoreErrorCode.INVALID_INPUT
            else:
                replacement = parsed[0]
        if code is None:
            raw = _checked_record_bytes(replacement_body, _RESERVATION_CHECKSUM_DOMAIN)
            code = _replace_durable_record(
                opened[1][2],
                _RESERVATION_RECORD,
                raw,
                secrets.token_hex(16),
                RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            )
    finally:
        if opened is not None and opened[1] is not None:
            close_failed = _close_fd(opened[1][2])
            close_failed = _close_two(opened[1][1], opened[1][0]) or close_failed
        if root_fd is not None:
            close_failed = _close_fd(root_fd) or close_failed
        release_code = _release_quota_lease(quota_lease)
    if code is RevisionStoreErrorCode.DURABILITY_UNCERTAIN:
        code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    if code is None and (close_failed or release_code is not None):
        code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    return (replacement, code)


def _set_reservation_phase(
    store,
    revision_id,
    kind,
    project_id,
    expected_head,
    reservation_key,
    state,
    revision_temp,
):
    def transform(current):
        binding_code = _reservation_binding_code(
            current,
            kind,
            project_id,
            expected_head,
            reservation_key,
        )
        if binding_code is not None:
            raise RevisionStoreError(binding_code)
        return _reservation_body(
            current["kind"],
            current["project_id"],
            current["expected_head"],
            current["revision_id"],
            current["key_sha256"],
            current["ceiling_files"],
            state,
            current["project_temp"],
            revision_temp,
        )

    try:
        return _replace_reservation(store, revision_id, transform)
    except RevisionStoreError as error:
        return (None, error.code)


def _release_reservation(
    store,
    revision_id,
    kind,
    project_id,
    expected_head,
    reservation_key,
):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return quota[1]
    quota_lease = quota[0]
    root_fd = None
    root_device = None
    quota_fd = None
    reservations_fd = None
    reservation_fd = None
    reservation = None
    code = None
    close_failed = False
    release_code = None
    try:
        root_open = _open_store_root(store)
        code = root_open[2]
        if code is None:
            root_fd = root_open[0]
            root_device = root_open[1].st_dev
            opened = _read_reservation_for_update(
                root_fd,
                root_device,
                revision_id,
            )
            code = opened[3]
            reservation = opened[0]
            if opened[1] is not None:
                quota_fd = opened[1][0]
                reservations_fd = opened[1][1]
                reservation_fd = opened[1][2]
        if code is None:
            code = _reservation_binding_code(
                reservation,
                kind,
                project_id,
                expected_head,
                reservation_key,
            )
        if code is None:
            reservations_result = _load_reservations(root_fd, root_device)
            code = reservations_result[1]
            if code is None:
                snapshot_result = _quota_snapshot(
                    root_fd,
                    root_device,
                    reservations_result[0],
                )
                code = snapshot_result[1]
                if code is None:
                    code = _reservation_release_code(
                        snapshot_result[0],
                        reservations_result[0],
                        reservation,
                    )
        if code is None:
            try:
                os.unlink(_RESERVATION_RECORD, dir_fd=reservation_fd)
                os.fsync(reservation_fd)
            except OSError:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        if reservation_fd is not None:
            close_failed = _close_fd(reservation_fd) or close_failed
            reservation_fd = None
        if code is None:
            try:
                os.rmdir(_revision_key(revision_id), dir_fd=reservations_fd)
                os.fsync(reservations_fd)
            except OSError:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    finally:
        if reservation_fd is not None:
            close_failed = _close_fd(reservation_fd) or close_failed
        if reservations_fd is not None:
            close_failed = _close_fd(reservations_fd) or close_failed
        if quota_fd is not None:
            close_failed = _close_fd(quota_fd) or close_failed
        if root_fd is not None:
            close_failed = _close_fd(root_fd) or close_failed
        release_code = _release_quota_lease(quota_lease)
    if code is None and (close_failed or release_code is not None):
        code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    return code


def _reservation_value(
    store,
    revision_id,
    kind,
    project_id,
    expected_head,
    reservation_key,
):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return (None, quota[1])
    quota_lease = quota[0]
    root_fd = None
    opened = None
    reservation = None
    code = None
    close_failed = False
    release_code = None
    try:
        root_open = _open_store_root(store)
        code = root_open[2]
        if code is None:
            root_fd = root_open[0]
            opened = _read_reservation_for_update(
                root_fd,
                root_open[1].st_dev,
                revision_id,
            )
            code = opened[3]
            reservation = opened[0]
        if code is None:
            code = _reservation_binding_code(
                reservation,
                kind,
                project_id,
                expected_head,
                reservation_key,
            )
    finally:
        if opened is not None and opened[1] is not None:
            close_failed = _close_fd(opened[1][2])
            close_failed = _close_two(opened[1][1], opened[1][0]) or close_failed
        if root_fd is not None:
            close_failed = _close_fd(root_fd) or close_failed
        release_code = _release_quota_lease(quota_lease)
    if code is None and (close_failed or release_code is not None):
        code = RevisionStoreErrorCode.IO_ERROR
    return (reservation, code)


def _release_reservation_by_record(store, project_id, revision_id):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return quota[1]
    quota_lease = quota[0]
    root_fd = None
    root_device = None
    quota_fd = None
    reservations_fd = None
    reservation_fd = None
    reservation = None
    code = None
    close_failed = False
    release_code = None
    try:
        root_open = _open_store_root(store)
        code = root_open[2]
        if code is None:
            root_fd = root_open[0]
            root_device = root_open[1].st_dev
            opened = _read_reservation_for_update(
                root_fd,
                root_device,
                revision_id,
            )
            code = opened[3]
            reservation = opened[0]
            if opened[1] is not None:
                quota_fd = opened[1][0]
                reservations_fd = opened[1][1]
                reservation_fd = opened[1][2]
        if code is None and reservation["project_id"] != project_id:
            code = RevisionStoreErrorCode.CONFLICT
        if code is None:
            reservations_result = _load_reservations(root_fd, root_device)
            code = reservations_result[1]
            if code is None:
                snapshot_result = _quota_snapshot(
                    root_fd,
                    root_device,
                    reservations_result[0],
                )
                code = snapshot_result[1]
                if code is None:
                    code = _reservation_release_code(
                        snapshot_result[0],
                        reservations_result[0],
                        reservation,
                    )
        if code is None:
            try:
                os.unlink(_RESERVATION_RECORD, dir_fd=reservation_fd)
                os.fsync(reservation_fd)
            except OSError:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        if reservation_fd is not None:
            close_failed = _close_fd(reservation_fd) or close_failed
            reservation_fd = None
        if code is None:
            try:
                os.rmdir(_revision_key(revision_id), dir_fd=reservations_fd)
                os.fsync(reservations_fd)
            except OSError:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    finally:
        if reservation_fd is not None:
            close_failed = _close_fd(reservation_fd) or close_failed
        if reservations_fd is not None:
            close_failed = _close_fd(reservations_fd) or close_failed
        if quota_fd is not None:
            close_failed = _close_fd(quota_fd) or close_failed
        if root_fd is not None:
            close_failed = _close_fd(root_fd) or close_failed
        release_code = _release_quota_lease(quota_lease)
    if code is None and (close_failed or release_code is not None):
        code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    return code


def _reservation_by_record(store, project_id, revision_id):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return (None, quota[1])
    quota_lease = quota[0]
    root_fd = None
    opened = None
    reservation = None
    code = None
    close_failed = False
    release_code = None
    try:
        root_open = _open_store_root(store)
        code = root_open[2]
        if code is None:
            root_fd = root_open[0]
            opened = _read_reservation_for_update(
                root_fd,
                root_open[1].st_dev,
                revision_id,
            )
            code = opened[3]
            reservation = opened[0]
        if code is None and reservation["project_id"] != project_id:
            code = RevisionStoreErrorCode.CONFLICT
    finally:
        if opened is not None and opened[1] is not None:
            close_failed = _close_fd(opened[1][2])
            close_failed = _close_two(opened[1][1], opened[1][0]) or close_failed
        if root_fd is not None:
            close_failed = _close_fd(root_fd) or close_failed
        release_code = _release_quota_lease(quota_lease)
    if code is None and (close_failed or release_code is not None):
        code = RevisionStoreErrorCode.IO_ERROR
    return (reservation, code)


def _set_reservation_phase_by_record(
    store,
    project_id,
    revision_id,
    state,
    revision_temp,
):
    def transform(current):
        if current["project_id"] != project_id or current["revision_id"] != revision_id:
            raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)
        return _reservation_body(
            current["kind"],
            current["project_id"],
            current["expected_head"],
            current["revision_id"],
            current["key_sha256"],
            current["ceiling_files"],
            state,
            current["project_temp"],
            revision_temp,
        )

    try:
        return _replace_reservation(store, revision_id, transform)
    except RevisionStoreError as error:
        return (None, error.code)


def _validate_candidate_reservation(
    store,
    project_id,
    expected_head,
    revision_id,
    reservation_key,
    lease,
):
    mutation_code = _require_mutation(store, project_id, lease)
    if mutation_code is not None:
        return mutation_code
    reservation = _reservation_value(
        store,
        revision_id,
        "candidate",
        project_id,
        expected_head,
        reservation_key,
    )
    if reservation[1] is not None:
        return reservation[1]
    if reservation[0]["state"] != "staged":
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    authority = _candidate_authority(store, project_id, revision_id, lease)
    if authority[2] is not None:
        return authority[2]
    close_failed = _close_project_fds(authority[1])
    close_failed = _close_fd(authority[0][0]) or close_failed
    if close_failed:
        return RevisionStoreErrorCode.IO_ERROR
    return None


def _converge_generation_zero_publication(
    store,
    root_fd,
    root_device,
    project_id,
    expected_sha256,
    expected_size,
    reservation_key,
    ceiling_files,
):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return quota[1]
    quota_lease = quota[0]
    code = None
    reservation = None
    key_result = _reservation_key_digest(reservation_key)
    project_open = None
    head = None
    revision = None
    close_failed = False
    release_code = None
    try:
        reservations_result = _load_reservations(root_fd, root_device)
        code = reservations_result[1]
        if code is None:
            for candidate in reservations_result[0]:
                if candidate["project_id"] == project_id:
                    reservation = candidate
        if code is None and reservation is None:
            code = RevisionStoreErrorCode.ALREADY_EXISTS
        if code is None:
            if (
                reservation["kind"] != "generation_zero"
                or reservation["expected_head"] is not None
                or reservation["key_sha256"] != key_result[0]
                or reservation["ceiling_files"] != ceiling_files
                or reservation["state"] not in {"publishing", "published"}
            ):
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        if code is None:
            temporary = _entry_stat(root_fd, reservation["project_temp"])
            if temporary[2] is not None or temporary[1]:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        if code is None:
            project_open = _open_project(root_fd, root_device, project_id)
            if project_open[3] is not None:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        if code is None:
            head_result = _load_head_fd(
                project_open[0],
                project_open[1],
                root_device,
                project_id,
            )
            if head_result[1] is not None:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
            else:
                head = head_result[0]
        if code is None:
            revision_result = _load_revision_fd(
                project_open[1],
                root_device,
                project_id,
                reservation["revision_id"],
            )
            if revision_result[1] is not None:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
            else:
                revision = revision_result[0]
        if code is None:
            if (
                head.generation != 0
                or head.revision_id != reservation["revision_id"]
                or revision.id != reservation["revision_id"]
                or revision.base_revision is not None
                or head.manifest_sha256 != revision.manifest_sha256
                or revision.artifacts != ()
            ):
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        if code is None and expected_sha256 is None:
            if revision.model is not None:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        if code is None and expected_sha256 is not None:
            if (
                revision.model is None
                or revision.model.name != "model.FCStd"
                or revision.model.format != "fcstd"
                or revision.model.sha256 != expected_sha256
                or revision.model.size_bytes != expected_size
            ):
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    finally:
        if project_open is not None:
            close_failed = _close_project_fds(project_open)
        release_code = _release_quota_lease(quota_lease)
    if code is None and (close_failed or release_code is not None):
        code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    if code is not None:
        return code
    if reservation["state"] == "publishing":
        phase = _set_reservation_phase(
            store,
            reservation["revision_id"],
            "generation_zero",
            project_id,
            None,
            reservation_key,
            "published",
            None,
        )
        if phase[1] is not None:
            return RevisionStoreErrorCode.RECOVERY_REQUIRED
    released = _release_reservation(
        store,
        reservation["revision_id"],
        "generation_zero",
        project_id,
        None,
        reservation_key,
    )
    if released is not None:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    return RevisionStoreErrorCode.ALREADY_EXISTS


def _initialize_project(
    store,
    project_id,
    source,
    expected_sha256,
    expected_size,
    lease,
    source_at=None,
):
    mutation_code = _require_mutation(store, project_id, lease)
    if mutation_code is not None:
        raise RevisionStoreError(mutation_code)
    source_open = None
    if source is not None and source_at is not None:
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    if source is not None or source_at is not None:
        if (
            type(expected_sha256) is not str
            or re.fullmatch(_DIGEST_PATTERN, expected_sha256) is None
            or type(expected_size) is not int
            or expected_size <= 0
        ):
            raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
        if expected_size > _MAX_FILE_BYTES:
            raise RevisionStoreError(RevisionStoreErrorCode.BUDGET_EXCEEDED)
        if source_at is None:
            source_open = _open_external_source(source)
        else:
            if type(source_at) is not tuple or len(source_at) != 3:
                raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
            if type(source_at[2]) is not RevisionSourceBinding:
                raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
            source_open = _open_external_source_at(
                source_at[0],
                source_at[1],
                source_at[2],
            )
        if source_open[4] is not None:
            raise RevisionStoreError(source_open[4])
    elif expected_sha256 is not None or expected_size is not None:
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    root_open = _open_store_root(store)
    if root_open[2] is not None:
        source_close_failed = False
        if source_open is not None:
            source_close_failed = _close_two(source_open[1], source_open[0])
        if source_close_failed:
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        raise RevisionStoreError(root_open[2])
    root_fd = root_open[0]
    final_name = _project_key(project_id)
    existing = _entry_stat(root_fd, final_name)
    if existing[2] is not None:
        source_close_failed = False
        if source_open is not None:
            source_close_failed = _close_two(source_open[1], source_open[0])
        _close_fd(root_fd)
        if source_close_failed:
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        raise RevisionStoreError(existing[2])
    if existing[1]:
        convergence = RevisionStoreErrorCode.UNSAFE_STORE
        if _safe_directory_stat(existing[0], root_open[1].st_dev):
            convergence = _converge_generation_zero_publication(
                store,
                root_fd,
                root_open[1].st_dev,
                project_id,
                expected_sha256,
                expected_size,
                "generation-zero:" + project_id,
                5 if source_open is not None else 4,
            )
        source_close_failed = False
        if source_open is not None:
            source_close_failed = _close_two(source_open[1], source_open[0])
        _close_fd(root_fd)
        if source_close_failed:
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        raise RevisionStoreError(convergence)
    revision_id = _new_revision_id()
    if _identifier_code(revision_id, _REVISION_PATTERN) is not None:
        source_close_failed = False
        if source_open is not None:
            source_close_failed = _close_two(source_open[1], source_open[0])
        _close_fd(root_fd)
        if source_close_failed:
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_IDENTIFIER)
    revision_name = _revision_key(revision_id)
    temp_name = ".project." + secrets.token_hex(16) + ".tmp"
    reservation_key = "generation-zero:" + project_id
    reservation_returned = False
    try:
        reserved = _reserve_quota(
            store,
            "generation_zero",
            project_id,
            None,
            revision_id,
            reservation_key,
            temp_name,
            5 if source_open is not None else 4,
        )
        reservation_returned = True
    finally:
        if not reservation_returned:
            source_close_failed = False
            if source_open is not None:
                source_close_failed = _close_two(source_open[1], source_open[0])
            _close_fd(root_fd)
            if source_close_failed:
                raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    if reserved[2] is not None:
        source_close_failed = False
        if source_open is not None:
            source_close_failed = _close_two(source_open[1], source_open[0])
        _close_fd(root_fd)
        if source_close_failed:
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        raise RevisionStoreError(reserved[2])
    reservation = reserved[0]
    revision_id = reservation["revision_id"]
    revision_name = _revision_key(revision_id)
    temp_name = reservation["project_temp"]
    if reserved[1]:
        if reservation["state"] == "published":
            source_close_failed = False
            if source_open is not None:
                source_close_failed = _close_two(source_open[1], source_open[0])
            _close_fd(root_fd)
            if source_close_failed:
                raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        if _quota_cleanup_initial(store, root_fd, temp_name, revision_name):
            source_close_failed = False
            if source_open is not None:
                source_close_failed = _close_two(source_open[1], source_open[0])
            _close_fd(root_fd)
            if source_close_failed:
                raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    creation_code = _quota_create_initial_namespace(
        store,
        root_fd,
        temp_name,
        revision_name,
        source_open is not None,
    )
    if creation_code is not None:
        source_close_failed = False
        if source_open is not None:
            source_close_failed = _close_two(source_open[1], source_open[0])
        cleanup_failed = _quota_cleanup_initial(store, root_fd, temp_name, revision_name)
        release_code = None
        if not cleanup_failed:
            release_code = _release_reservation(
                store,
                revision_id,
                "generation_zero",
                project_id,
                None,
                reservation_key,
            )
        _close_fd(root_fd)
        if source_close_failed or cleanup_failed or release_code is not None:
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        raise RevisionStoreError(creation_code)
    project_fd = None
    revisions_fd = None
    candidates_fd = None
    revision_fd = None
    code = None
    try:
        project_fd = os.open(temp_name, _root_flags(), dir_fd=root_fd)
        revisions_fd = os.open("revisions", _root_flags(), dir_fd=project_fd)
        candidates_fd = os.open("candidates", _root_flags(), dir_fd=project_fd)
        revision_fd = os.open(revision_name, _root_flags(), dir_fd=revisions_fd)
    except OSError:
        code = RevisionStoreErrorCode.IO_ERROR
    model = None
    if code is None and source_open is not None:
        copied = _copy_open_file(
            source_open[0],
            source_open[1],
            source_open[2],
            source_open[3],
            revision_fd,
            "model.FCStd",
            True,
        )
        if copied[2] is not None:
            code = copied[2]
        elif copied[0] != expected_sha256 or copied[1] != expected_size:
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
        else:
            model = RevisionArtifactRef(
                id=_new_artifact_id(),
                name="model.FCStd",
                format="fcstd",
                sha256=copied[0],
                size_bytes=copied[1],
            )
    if source_at is not None and source_open is not None:
        parent_code = _source_parent_after_code(source_open[0], source_open[5])
        if parent_code is not None and code is None:
            code = parent_code
    if source_open is not None:
        if _close_two(source_open[1], source_open[0]):
            code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    artifacts = ()
    if code is None:
        manifest_body = _manifest_body(project_id, revision_id, None, model, artifacts)
        manifest_raw = _checked_record_bytes(manifest_body, _MANIFEST_CHECKSUM_DOMAIN)
        code = _create_durable_file(revision_fd, "manifest.json", manifest_raw, True)
    if code is None:
        manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
        head = ProjectHead(
            project_id=project_id,
            generation=0,
            revision_id=revision_id,
            manifest_sha256=manifest_digest,
        )
        head_raw = _checked_record_bytes(_head_mapping(head), _HEAD_CHECKSUM_DOMAIN)
        code = _create_durable_file(project_fd, "HEAD.json", head_raw, True)
    if code is None:
        sync_failed = False
        try:
            os.fsync(revision_fd)
            os.fsync(revisions_fd)
            os.fsync(candidates_fd)
            os.fsync(project_fd)
        except OSError:
            sync_failed = True
        if sync_failed:
            code = RevisionStoreErrorCode.IO_ERROR
    close_failed = False
    if revision_fd is not None:
        close_failed = _close_fd(revision_fd) or close_failed
    if candidates_fd is not None:
        close_failed = _close_fd(candidates_fd) or close_failed
    if revisions_fd is not None:
        close_failed = _close_fd(revisions_fd) or close_failed
    if project_fd is not None:
        close_failed = _close_fd(project_fd) or close_failed
    if close_failed and code is None:
        code = RevisionStoreErrorCode.IO_ERROR
    if code is not None:
        cleanup_failed = _quota_cleanup_initial(store, root_fd, temp_name, revision_name)
        release_code = None
        if not cleanup_failed:
            release_code = _release_reservation(
                store,
                revision_id,
                "generation_zero",
                project_id,
                None,
                reservation_key,
            )
        _close_fd(root_fd)
        if cleanup_failed or release_code is not None:
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        raise RevisionStoreError(code)
    publishing = _set_reservation_phase(
        store,
        revision_id,
        "generation_zero",
        project_id,
        None,
        reservation_key,
        "publishing",
        None,
    )
    if publishing[1] is not None:
        _close_fd(root_fd)
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    renamed = _quota_rename_directory(
        store,
        root_fd,
        temp_name,
        final_name,
        RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
    )
    rename_code = renamed[0]
    if rename_code is not None and renamed[1]:
        _close_fd(root_fd)
        raise RevisionStoreError(
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            head_committed=True,
        )
    if rename_code is not None:
        cleanup_failed = _quota_cleanup_initial(store, root_fd, temp_name, revision_name)
        release_code = None
        if not cleanup_failed:
            release_code = _release_reservation(
                store,
                revision_id,
                "generation_zero",
                project_id,
                None,
                reservation_key,
            )
        _close_fd(root_fd)
        if cleanup_failed or release_code is not None:
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        raise RevisionStoreError(rename_code)
    phase_result = _set_reservation_phase(
        store,
        revision_id,
        "generation_zero",
        project_id,
        None,
        reservation_key,
        "published",
        None,
    )
    release_code = None
    if phase_result[1] is None:
        release_code = _release_reservation(
            store,
            revision_id,
            "generation_zero",
            project_id,
            None,
            reservation_key,
        )
    root_close_failed = _close_fd(root_fd)
    if phase_result[1] is not None or release_code is not None or root_close_failed:
        raise RevisionStoreError(
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            head_committed=True,
        )
    return head


def _close_project_fds(project_open):
    failed = False
    if project_open[2] is not None:
        failed = _close_fd(project_open[2]) or failed
    if project_open[1] is not None:
        failed = _close_fd(project_open[1]) or failed
    if project_open[0] is not None:
        failed = _close_fd(project_open[0]) or failed
    return failed


def _load_store_project(store, project_id):
    root_open = _open_store_root(store)
    if root_open[2] is not None:
        return (None, None, root_open[2])
    project_open = _open_project(root_open[0], root_open[1].st_dev, project_id)
    if project_open[3] is not None:
        _close_fd(root_open[0])
        return (None, None, project_open[3])
    return (root_open, project_open, None)


def _directory_binding_code(parent_fd, name, directory_fd, root_device):
    try:
        opened = os.fstat(directory_fd)
    except OSError:
        return RevisionStoreErrorCode.IO_ERROR
    if not _safe_directory_stat(opened, root_device):
        return RevisionStoreErrorCode.UNSAFE_STORE
    entry = _entry_stat(parent_fd, name)
    if entry[2] is not None:
        return entry[2]
    if not entry[1]:
        return RevisionStoreErrorCode.UNSAFE_STORE
    if not _safe_directory_stat(entry[0], root_device):
        return RevisionStoreErrorCode.UNSAFE_STORE
    if not _same_source_parent(entry[0], opened):
        return RevisionStoreErrorCode.UNSAFE_STORE
    return None


def _terminal_journal_matches(head, journal):
    if journal.state is CommitJournalState.NOT_COMMITTED:
        return head == journal.expected_head
    if journal.state is CommitJournalState.COMMITTED:
        if head.generation != journal.expected_head.generation + 1:
            return False
        if head.revision_id != journal.candidate_revision:
            return False
        if head.manifest_sha256 != journal.manifest_sha256:
            return False
        return True
    return False


def _cleanup_candidate_dir(candidates_fd, candidate_name, root_device):
    opened = _open_safe_directory(
        candidates_fd,
        candidate_name,
        root_device,
        RevisionStoreErrorCode.NOT_FOUND,
    )
    if opened[1] is RevisionStoreErrorCode.NOT_FOUND:
        return False
    if opened[1] is not None:
        return True
    candidate_fd = opened[0]
    failed = _best_unlink(candidate_fd, "model.FCStd")
    failed = _best_unlink(candidate_fd, "model.step") or failed
    failed = _best_unlink(candidate_fd, _SEED_INTENT_RECORD) or failed
    failed = _best_unlink(candidate_fd, _SEED_BINDING_RECORD) or failed
    failed = _close_fd(candidate_fd) or failed
    failed = _best_rmdir(candidates_fd, candidate_name) or failed
    sync_failed = False
    try:
        os.fsync(candidates_fd)
    except OSError:
        sync_failed = True
    return failed or sync_failed


def _reserve_candidate_revision(store, project_id, expected_head, reservation_key, lease):
    mutation_code = _require_mutation(store, project_id, lease)
    if mutation_code is not None:
        raise RevisionStoreError(mutation_code)
    if type(expected_head) is not ProjectHead or expected_head.project_id != project_id:
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    loaded = _load_store_project(store, project_id)
    if loaded[2] is not None:
        raise RevisionStoreError(loaded[2])
    root_open = loaded[0]
    project_open = loaded[1]
    project_fd = project_open[0]
    revisions_fd = project_open[1]
    candidates_fd = project_open[2]
    head_result = _load_head_fd(project_fd, revisions_fd, root_open[1].st_dev, project_id)
    if head_result[1] is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(head_result[1])
    head = head_result[0]
    if head != expected_head:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)
    journal_result = _load_journal_fd(project_fd, root_open[1].st_dev)
    if journal_result[1] is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    journal = journal_result[0]
    if journal is not None:
        if journal.state is CommitJournalState.STAGING:
            reservation = _reservation_value(
                store,
                journal.candidate_revision,
                "candidate",
                project_id,
                expected_head,
                reservation_key,
            )
            if reservation[1] is None and reservation[0]["state"] in {
                "reserved",
                "staged",
            }:
                candidate_open = _open_safe_directory(
                    candidates_fd,
                    _candidate_key(journal.candidate_revision),
                    root_open[1].st_dev,
                    RevisionStoreErrorCode.RECOVERY_REQUIRED,
                )
                if candidate_open[1] is None:
                    phase_code = None
                    if reservation[0]["state"] == "reserved":
                        phase = _set_reservation_phase(
                            store,
                            journal.candidate_revision,
                            "candidate",
                            project_id,
                            expected_head,
                            reservation_key,
                            "staged",
                            None,
                        )
                        phase_code = phase[1]
                    candidate_close_failed = _close_fd(candidate_open[0])
                    close_failed = _close_project_fds(project_open)
                    close_failed = _close_fd(root_open[0]) or close_failed
                    if phase_code is not None:
                        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
                    if candidate_close_failed or close_failed:
                        raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
                    return journal.candidate_revision
            _close_project_fds(project_open)
            _close_fd(root_open[0])
            if reservation[1] is RevisionStoreErrorCode.CONFLICT:
                raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        if not _terminal_journal_matches(head, journal):
            _close_project_fds(project_open)
            _close_fd(root_open[0])
            raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)
        if journal.state is CommitJournalState.COMMITTED:
            terminal_revision = _load_revision_fd(
                revisions_fd,
                root_open[1].st_dev,
                project_id,
                journal.candidate_revision,
            )
            if terminal_revision[1] is not None:
                _close_project_fds(project_open)
                _close_fd(root_open[0])
                raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
            if (
                terminal_revision[0].manifest_sha256 != journal.manifest_sha256
                or terminal_revision[0].base_revision != journal.expected_head.revision_id
            ):
                _close_project_fds(project_open)
                _close_fd(root_open[0])
                raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        unlink_failed = _quota_unlink_file(store, project_fd, "journal.json")
        if unlink_failed:
            _close_project_fds(project_open)
            _close_fd(root_open[0])
            raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
    revision_id = _new_revision_id()
    transaction_id = _new_transaction_id()
    if _identifier_code(revision_id, _REVISION_PATTERN) is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_IDENTIFIER)
    if _identifier_code(transaction_id, _TRANSACTION_PATTERN) is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_IDENTIFIER)
    candidate_name = _candidate_key(revision_id)
    seeded_reservation = bool(
        type(reservation_key) is str
        and re.fullmatch(r"revert:[0-9a-f]{64}", reservation_key) is not None
    )
    ceiling_files = 8
    if seeded_reservation:
        ceiling_files = 9
    reservation_returned = False
    try:
        reserved = _reserve_quota(
            store,
            "candidate",
            project_id,
            expected_head,
            revision_id,
            reservation_key,
            None,
            ceiling_files,
        )
        reservation_returned = True
    finally:
        if not reservation_returned:
            _close_project_fds(project_open)
            _close_fd(root_open[0])
    if reserved[2] is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(reserved[2])
    revision_id = reserved[0]["revision_id"]
    candidate_name = _candidate_key(revision_id)
    seed_intent_raw = None
    if seeded_reservation:
        seed_key_result = _reservation_key_digest(reservation_key)
        if seed_key_result[1] is None:
            seed_intent_raw = _checked_record_bytes(
                _seed_intent_body(
                    project_id,
                    revision_id,
                    expected_head,
                    seed_key_result[0],
                ),
                _SEED_INTENT_CHECKSUM_DOMAIN,
            )
    if reserved[1]:
        if reserved[0]["state"] == "published":
            _close_project_fds(project_open)
            _close_fd(root_open[0])
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        if _quota_cleanup_candidate(
            store,
            candidates_fd,
            candidate_name,
            root_open[1].st_dev,
        ):
            _close_project_fds(project_open)
            _close_fd(root_open[0])
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    namespace_code = _quota_create_candidate_namespace(
        store,
        candidates_fd,
        candidate_name,
        seed_intent_raw,
    )
    if namespace_code is not None:
        cleanup_failed = _quota_cleanup_candidate(
            store,
            candidates_fd,
            candidate_name,
            root_open[1].st_dev,
        )
        release_code = _release_reservation(
            store,
            revision_id,
            "candidate",
            project_id,
            expected_head,
            reservation_key,
        )
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        if cleanup_failed or release_code is not None:
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        raise RevisionStoreError(namespace_code)
    candidate_open = _open_safe_directory(
        candidates_fd,
        candidate_name,
        root_open[1].st_dev,
        RevisionStoreErrorCode.IO_ERROR,
    )
    code = candidate_open[1]
    candidate_fd = candidate_open[0]
    base_revision = None
    if code is None:
        base_result = _load_revision_fd(
            revisions_fd,
            root_open[1].st_dev,
            project_id,
            head.revision_id,
        )
        if base_result[1] is not None:
            code = base_result[1]
        else:
            base_revision = base_result[0]
    if code is None and base_revision.model is not None:
        base_open = _open_revision_directory(
            revisions_fd,
            root_open[1].st_dev,
            head.revision_id,
        )
        if base_open[1] is not None:
            code = base_open[1]
        else:
            base_model_open = _open_checked_file(
                base_open[0],
                "model.FCStd",
                root_open[1].st_dev,
                _MAX_FILE_BYTES,
                RevisionStoreErrorCode.CORRUPT_CONTENT,
                False,
            )
            if base_model_open[2] is not None:
                code = base_model_open[2]
            else:
                copied_base = _copy_open_file(
                    base_open[0],
                    base_model_open[0],
                    base_model_open[1],
                    "model.FCStd",
                    candidate_fd,
                    "model.FCStd",
                    True,
                )
                code = copied_base[2]
                if _close_fd(base_model_open[0]) and code is None:
                    code = RevisionStoreErrorCode.IO_ERROR
                if code is None:
                    if copied_base[0] != base_revision.model.sha256:
                        code = RevisionStoreErrorCode.CORRUPT_CONTENT
                    elif copied_base[1] != base_revision.model.size_bytes:
                        code = RevisionStoreErrorCode.CORRUPT_CONTENT
            if _close_fd(base_open[0]) and code is None:
                code = RevisionStoreErrorCode.IO_ERROR
    if code is None:
        sync_failed = False
        try:
            os.fsync(candidate_fd)
            os.fsync(candidates_fd)
        except OSError:
            sync_failed = True
        if sync_failed:
            code = RevisionStoreErrorCode.IO_ERROR
    if candidate_fd is not None and _close_fd(candidate_fd) and code is None:
        code = RevisionStoreErrorCode.IO_ERROR
    staging = None
    if code is None:
        staging = CommitJournal(
            id=transaction_id,
            project_id=project_id,
            expected_head=head,
            candidate_revision=revision_id,
            manifest_sha256=None,
            state=CommitJournalState.STAGING,
        )
        journal_raw = _checked_record_bytes(_journal_mapping(staging), _JOURNAL_CHECKSUM_DOMAIN)
        code = _quota_replace_record(
            store,
            project_fd,
            "journal.json",
            journal_raw,
            secrets.token_hex(16),
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
        )
    cleanup_failed = False
    release_code = None
    if code is not None and staging is None:
        cleanup_failed = _quota_cleanup_candidate(
            store,
            candidates_fd,
            candidate_name,
            root_open[1].st_dev,
        )
        if not cleanup_failed:
            release_code = _release_reservation(
                store,
                revision_id,
                "candidate",
                project_id,
                expected_head,
                reservation_key,
            )
    phase_code = None
    if code is None:
        phase_result = _set_reservation_phase(
            store,
            revision_id,
            "candidate",
            project_id,
            expected_head,
            reservation_key,
            "staged",
            None,
        )
        phase_code = phase_result[1]
    close_failed = _close_project_fds(project_open)
    close_failed = _close_fd(root_open[0]) or close_failed
    if code is RevisionStoreErrorCode.DURABILITY_UNCERTAIN:
        raise RevisionStoreError(code, head_committed=False)
    if code is not None:
        if cleanup_failed or release_code is not None:
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        raise RevisionStoreError(code)
    if phase_code is not None:
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    if close_failed:
        raise RevisionStoreError(
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            head_committed=False,
        )
    return revision_id


def _begin_revision(store, project_id, expected_head, lease):
    return _reserve_candidate_revision(
        store,
        project_id,
        expected_head,
        "legacy:" + secrets.token_hex(16),
        lease,
    )


def _prepare_revision(
    store,
    project_id,
    expected_head,
    revision_id,
    manifest_sha256,
    lease,
):
    mutation_code = _require_mutation(store, project_id, lease)
    if mutation_code is not None:
        raise RevisionStoreError(mutation_code)
    revision_code = _identifier_code(revision_id, _REVISION_PATTERN)
    if revision_code is not None:
        raise RevisionStoreError(revision_code)
    if type(expected_head) is not ProjectHead or expected_head.project_id != project_id:
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    if _digest_code(manifest_sha256) is not None:
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    loaded = _load_store_project(store, project_id)
    if loaded[2] is not None:
        raise RevisionStoreError(loaded[2])
    root_open = loaded[0]
    project_open = loaded[1]
    project_fd = project_open[0]
    revisions_fd = project_open[1]
    root_device = root_open[1].st_dev
    head_result = _load_head_fd(
        project_fd,
        revisions_fd,
        root_device,
        project_id,
    )
    if head_result[1] is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(head_result[1])
    head = head_result[0]
    if head != expected_head:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)
    journal_result = _load_journal_fd(project_fd, root_device)
    if journal_result[1] is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    journal = journal_result[0]
    if journal is not None:
        if journal.state not in {
            CommitJournalState.COMMITTED,
            CommitJournalState.NOT_COMMITTED,
        }:
            _close_project_fds(project_open)
            _close_fd(root_open[0])
            raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)
        if not _terminal_journal_matches(head, journal):
            _close_project_fds(project_open)
            _close_fd(root_open[0])
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
        if journal.state is CommitJournalState.COMMITTED:
            terminal_revision = _load_revision_fd(
                revisions_fd,
                root_device,
                project_id,
                journal.candidate_revision,
            )
            if terminal_revision[1] is not None:
                _close_project_fds(project_open)
                _close_fd(root_open[0])
                raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
            if (
                terminal_revision[0].manifest_sha256 != journal.manifest_sha256
                or terminal_revision[0].base_revision != journal.expected_head.revision_id
            ):
                _close_project_fds(project_open)
                _close_fd(root_open[0])
                raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    sealed_result = _load_revision_fd(
        revisions_fd,
        root_device,
        project_id,
        revision_id,
    )
    if sealed_result[1] is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(sealed_result[1])
    sealed = sealed_result[0]
    if (
        sealed.base_revision != expected_head.revision_id
        or sealed.manifest_sha256 != manifest_sha256
    ):
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)
    if (
        journal is not None
        and journal.state is CommitJournalState.NOT_COMMITTED
        and journal.candidate_revision == revision_id
        and journal.manifest_sha256 != sealed.manifest_sha256
    ):
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    transaction_id = _new_transaction_id()
    if _identifier_code(transaction_id, _TRANSACTION_PATTERN) is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_IDENTIFIER)
    if journal is not None and transaction_id == journal.id:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)
    prepared = CommitJournal(
        id=transaction_id,
        project_id=project_id,
        expected_head=expected_head,
        candidate_revision=revision_id,
        manifest_sha256=sealed.manifest_sha256,
        state=CommitJournalState.PREPARED,
    )
    prepared_raw = _checked_record_bytes(
        _journal_mapping(prepared),
        _JOURNAL_CHECKSUM_DOMAIN,
    )
    code = _quota_replace_record(
        store,
        project_fd,
        "journal.json",
        prepared_raw,
        secrets.token_hex(16),
        RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
    )
    close_failed = _close_project_fds(project_open)
    close_failed = _close_fd(root_open[0]) or close_failed
    if code is RevisionStoreErrorCode.DURABILITY_UNCERTAIN:
        raise RevisionStoreError(code, head_committed=False)
    if code is not None:
        raise RevisionStoreError(code)
    if close_failed:
        raise RevisionStoreError(
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            head_committed=False,
        )
    return sealed


def _candidate_authority(store, project_id, revision_id, lease):
    mutation_code = _require_mutation(store, project_id, lease)
    if mutation_code is not None:
        return (None, None, mutation_code)
    revision_code = _identifier_code(revision_id, _REVISION_PATTERN)
    if revision_code is not None:
        return (None, None, revision_code)
    loaded = _load_store_project(store, project_id)
    if loaded[2] is not None:
        return (None, None, loaded[2])
    root_open = loaded[0]
    project_open = loaded[1]
    journal_result = _load_journal_fd(project_open[0], root_open[1].st_dev)
    if journal_result[1] is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        return (None, None, RevisionStoreErrorCode.RECOVERY_REQUIRED)
    journal = journal_result[0]
    if journal is None or journal.state is not CommitJournalState.STAGING:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        return (None, None, RevisionStoreErrorCode.CONFLICT)
    if journal.project_id != project_id or journal.candidate_revision != revision_id:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        return (None, None, RevisionStoreErrorCode.CONFLICT)
    head_result = _load_head_fd(
        project_open[0],
        project_open[1],
        root_open[1].st_dev,
        project_id,
    )
    if head_result[1] is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        return (None, None, head_result[1])
    if head_result[0] != journal.expected_head:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        return (None, None, RevisionStoreErrorCode.CONFLICT)
    candidate_open = _open_safe_directory(
        project_open[2],
        _candidate_key(revision_id),
        root_open[1].st_dev,
        RevisionStoreErrorCode.NOT_FOUND,
    )
    if candidate_open[1] is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        return (None, None, candidate_open[1])
    candidate_close_failed = _close_fd(candidate_open[0])
    if candidate_close_failed:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        return (None, None, RevisionStoreErrorCode.IO_ERROR)
    return (root_open, project_open, None)


def _open_worker_candidate_staging(
    store,
    *,
    expected_head,
    revision_id,
    lease,
):
    """Atomically pin one live store reservation for the Worker parent proxy."""

    if (
        type(store) is not LocalRevisionStore
        or type(expected_head) is not ProjectHead
        or expected_head.project_id == ""
        or _identifier_code(revision_id, _REVISION_PATTERN) is not None
    ):
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    project_id = expected_head.project_id
    authority = _candidate_authority(store, project_id, revision_id, lease)
    if authority[2] is not None:
        raise RevisionStoreError(authority[2])
    root_open = authority[0]
    project_open = authority[1]
    root_device = root_open[1].st_dev
    candidate_fd = None
    candidates_copy = None
    result = None
    code = None
    close_failed = False
    try:
        journal_result = _load_journal_fd(project_open[0], root_device)
        if journal_result[1] is not None or journal_result[0] is None:
            code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        else:
            journal = journal_result[0]
            if (
                journal.state is not CommitJournalState.STAGING
                or journal.project_id != project_id
                or journal.expected_head != expected_head
                or journal.candidate_revision != revision_id
            ):
                code = RevisionStoreErrorCode.CONFLICT
        reservation = None
        if code is None:
            reservation_result = _reservation_by_record(
                store,
                project_id,
                revision_id,
            )
            reservation = reservation_result[0]
            code = reservation_result[1]
        if code is None and (
            reservation["kind"] != "candidate"
            or reservation["project_id"] != project_id
            or reservation["expected_head"] != expected_head
            or reservation["revision_id"] != revision_id
            or reservation["state"] != "staged"
        ):
            code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        candidate_name = _candidate_key(revision_id)
        if code is None:
            opened = _open_safe_directory(
                project_open[2],
                candidate_name,
                root_device,
                RevisionStoreErrorCode.NOT_FOUND,
            )
            candidate_fd = opened[0]
            code = opened[1]
        if code is None:
            entries_result = _discovery_entries(candidate_fd)
            entries = entries_result[0]
            code = entries_result[1]
        if code is None:
            entry_count = len(entries)
            names = ()
            if entry_count >= 1:
                names = names + (entries[0][0],)
            if entry_count >= 2:
                names = names + (entries[1][0],)
            if entry_count >= 3:
                names = names + (entries[2][0],)
            if entry_count == 4:
                names = names + (entries[3][0],)
            if entry_count > 4 or names not in (
                ("model.FCStd", "model.step"),
                ("model.FCStd", "model.step", _SEED_BINDING_RECORD),
                ("model.FCStd", "model.step", _SEED_INTENT_RECORD),
                (
                    "model.FCStd",
                    "model.step",
                    _SEED_BINDING_RECORD,
                    _SEED_INTENT_RECORD,
                ),
            ):
                code = RevisionStoreErrorCode.UNSAFE_STORE
            if code is None and not _safe_immutable_stat(entries[0][1], root_device):
                code = RevisionStoreErrorCode.UNSAFE_STORE
            if code is None and not _safe_immutable_stat(entries[1][1], root_device):
                code = RevisionStoreErrorCode.UNSAFE_STORE
            if (
                code is None
                and entry_count >= 3
                and not _safe_immutable_stat(entries[2][1], root_device)
            ):
                code = RevisionStoreErrorCode.UNSAFE_STORE
            if (
                code is None
                and entry_count == 4
                and not _safe_immutable_stat(entries[3][1], root_device)
            ):
                code = RevisionStoreErrorCode.UNSAFE_STORE
        if code is None:
            try:
                candidates_copy = os.dup(project_open[2])
                parent_stat = os.fstat(project_open[2])
                copied_stat = os.fstat(candidates_copy)
                directory_stat = os.fstat(candidate_fd)
                live_stat = os.stat(
                    candidate_name,
                    dir_fd=candidates_copy,
                    follow_symlinks=False,
                )
                inheritable = os.get_inheritable(candidates_copy) or os.get_inheritable(
                    candidate_fd
                )
            except OSError:
                code = RevisionStoreErrorCode.IO_ERROR
            else:
                if (
                    inheritable
                    or not _safe_directory_stat(copied_stat, root_device)
                    or not _safe_directory_stat(directory_stat, root_device)
                    or not _same_source_parent(parent_stat, copied_stat)
                    or not _same_source_parent(directory_stat, live_stat)
                ):
                    code = RevisionStoreErrorCode.UNSAFE_STORE
        if code is None:
            result = (
                candidates_copy,
                candidate_fd,
                candidate_name,
                root_device,
            )
            candidates_copy = None
            candidate_fd = None
    finally:
        if candidate_fd is not None:
            close_failed = _close_fd(candidate_fd) or close_failed
        if candidates_copy is not None:
            close_failed = _close_fd(candidates_copy) or close_failed
        close_failed = _close_project_fds(project_open) or close_failed
        close_failed = _close_fd(root_open[0]) or close_failed
    if code is None and close_failed:
        code = RevisionStoreErrorCode.IO_ERROR
    if code is not None:
        if result is not None:
            close_failed = _close_fd(result[1])
            close_failed = _close_fd(result[0]) or close_failed
            if close_failed:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        raise RevisionStoreError(code)
    return result


def _seed_reservation_key_digest(value):
    if type(value) is not str or re.fullmatch(r"revert:[0-9a-f]{64}", value) is None:
        return (None, RevisionStoreErrorCode.INVALID_INPUT)
    return _reservation_key_digest(value)


def _seed_intent_body(project_id, revision_id, expected_head, key_sha256):
    return {
        "schema_version": _SCHEMA_VERSION,
        "project_id": project_id,
        "candidate_revision": revision_id,
        "expected_head": _head_mapping(expected_head),
        "key_sha256": key_sha256,
    }


def _seed_binding_body(
    project_id,
    revision_id,
    expected_head,
    expected_source,
    key_sha256,
):
    return {
        "schema_version": _SCHEMA_VERSION,
        "project_id": project_id,
        "candidate_revision": revision_id,
        "expected_head": _head_mapping(expected_head),
        "source_revision": _revision_mapping(expected_source),
        "key_sha256": key_sha256,
    }


def _seed_intent_from_body(body):
    expected = (
        "schema_version",
        "project_id",
        "candidate_revision",
        "expected_head",
        "key_sha256",
    )
    if not _mapping_has_exact(body, expected):
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if type(body["schema_version"]) is not int or body["schema_version"] != _SCHEMA_VERSION:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if _identifier_code(body["project_id"], _PROJECT_PATTERN) is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if _identifier_code(body["candidate_revision"], _REVISION_PATTERN) is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if _digest_code(body["key_sha256"]) is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    head_result = _head_from_record(body["expected_head"])
    if head_result[1] is not None:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if (
        head_result[0].project_id != body["project_id"]
        or head_result[0].revision_id == body["candidate_revision"]
    ):
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    return (
        {
            "project_id": body["project_id"],
            "candidate_revision": body["candidate_revision"],
            "expected_head": head_result[0],
            "key_sha256": body["key_sha256"],
        },
        None,
    )


def _seed_binding_from_body(body):
    expected = (
        "schema_version",
        "project_id",
        "candidate_revision",
        "expected_head",
        "source_revision",
        "key_sha256",
    )
    if not _mapping_has_exact(body, expected):
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    intent_result = _seed_intent_from_body(
        {
            "schema_version": body["schema_version"],
            "project_id": body["project_id"],
            "candidate_revision": body["candidate_revision"],
            "expected_head": body["expected_head"],
            "key_sha256": body["key_sha256"],
        }
    )
    if intent_result[1] is not None:
        return (None, intent_result[1])
    source = None
    try:
        source = _revision_from_mapping(body["source_revision"])
    except RevisionStoreError:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    if (
        _seed_source_code(body["project_id"], source) is not None
        or source.id == intent_result[0]["expected_head"].revision_id
    ):
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    value = dict(intent_result[0])
    value["source_revision"] = source
    return (value, None)


def _read_seed_control(candidate_fd, root_device, name, domain, bound):
    raw = _read_bounded_file(
        candidate_fd,
        name,
        root_device,
        _MAX_JOURNAL_BYTES,
        RevisionStoreErrorCode.NOT_FOUND,
    )
    if raw[1] is RevisionStoreErrorCode.NOT_FOUND:
        return (None, RevisionStoreErrorCode.NOT_FOUND)
    if raw[1] is not None:
        return (None, RevisionStoreErrorCode.RECOVERY_REQUIRED)
    checked = _parse_checked_record(raw[0], domain, _MAX_JOURNAL_BYTES)
    if checked[1] is not None:
        return (None, RevisionStoreErrorCode.RECOVERY_REQUIRED)
    parsed = _seed_intent_from_body(checked[0])
    if bound:
        parsed = _seed_binding_from_body(checked[0])
    if parsed[1] is not None:
        return (None, RevisionStoreErrorCode.RECOVERY_REQUIRED)
    return parsed


def _seed_control_binding_code(
    value,
    project_id,
    revision_id,
    expected_head,
    key_sha256,
):
    if (
        value["project_id"] != project_id
        or value["candidate_revision"] != revision_id
        or value["expected_head"] != expected_head
        or value["key_sha256"] != key_sha256
    ):
        return RevisionStoreErrorCode.CONFLICT
    return None


def _quota_create_seed_binding(store, candidate_fd, raw):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return quota[1]
    code = None
    release_code = None
    try:
        code = _create_durable_file(candidate_fd, _SEED_BINDING_RECORD, raw)
        if code is None:
            try:
                os.fsync(candidate_fd)
            except OSError:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    finally:
        release_code = _release_quota_lease(quota[0])
    if code is not None or release_code is not None:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    return None


def _bind_seed_source(
    store,
    candidate_fd,
    root_device,
    project_id,
    revision_id,
    expected_head,
    expected_source,
    key_sha256,
):
    binding = _read_seed_control(
        candidate_fd,
        root_device,
        _SEED_BINDING_RECORD,
        _SEED_BINDING_CHECKSUM_DOMAIN,
        True,
    )
    if binding[1] is not None and binding[1] is not RevisionStoreErrorCode.NOT_FOUND:
        return binding[1]
    intent = _read_seed_control(
        candidate_fd,
        root_device,
        _SEED_INTENT_RECORD,
        _SEED_INTENT_CHECKSUM_DOMAIN,
        False,
    )
    if intent[1] is not None and intent[1] is not RevisionStoreErrorCode.NOT_FOUND:
        return intent[1]
    if binding[1] is None:
        code = _seed_control_binding_code(
            binding[0],
            project_id,
            revision_id,
            expected_head,
            key_sha256,
        )
        if code is None and binding[0]["source_revision"] != expected_source:
            code = RevisionStoreErrorCode.CONFLICT
        if code is not None:
            return code
        if intent[1] is None:
            code = _seed_control_binding_code(
                intent[0],
                project_id,
                revision_id,
                expected_head,
                key_sha256,
            )
            if code is not None:
                return code
            if _quota_unlink_file(store, candidate_fd, _SEED_INTENT_RECORD):
                return RevisionStoreErrorCode.RECOVERY_REQUIRED
        return None
    if intent[1] is RevisionStoreErrorCode.NOT_FOUND:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    code = _seed_control_binding_code(
        intent[0],
        project_id,
        revision_id,
        expected_head,
        key_sha256,
    )
    if code is not None:
        return code
    raw = _checked_record_bytes(
        _seed_binding_body(
            project_id,
            revision_id,
            expected_head,
            expected_source,
            key_sha256,
        ),
        _SEED_BINDING_CHECKSUM_DOMAIN,
    )
    code = _quota_create_seed_binding(store, candidate_fd, raw)
    if code is not None:
        return code
    binding = _read_seed_control(
        candidate_fd,
        root_device,
        _SEED_BINDING_RECORD,
        _SEED_BINDING_CHECKSUM_DOMAIN,
        True,
    )
    if binding[1] is not None:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    code = _seed_control_binding_code(
        binding[0],
        project_id,
        revision_id,
        expected_head,
        key_sha256,
    )
    if code is None and binding[0]["source_revision"] != expected_source:
        code = RevisionStoreErrorCode.CONFLICT
    if code is not None:
        return code
    if _quota_unlink_file(store, candidate_fd, _SEED_INTENT_RECORD):
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    return None


def _validate_seed_binding(
    candidate_fd,
    root_device,
    project_id,
    revision_id,
    expected_head,
    expected_source,
    key_sha256,
):
    binding = _read_seed_control(
        candidate_fd,
        root_device,
        _SEED_BINDING_RECORD,
        _SEED_BINDING_CHECKSUM_DOMAIN,
        True,
    )
    if binding[1] is not None:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    intent = _read_seed_control(
        candidate_fd,
        root_device,
        _SEED_INTENT_RECORD,
        _SEED_INTENT_CHECKSUM_DOMAIN,
        False,
    )
    if intent[1] is not RevisionStoreErrorCode.NOT_FOUND:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    code = _seed_control_binding_code(
        binding[0],
        project_id,
        revision_id,
        expected_head,
        key_sha256,
    )
    if code is None and binding[0]["source_revision"] != expected_source:
        code = RevisionStoreErrorCode.CONFLICT
    return code


def _seed_seal_source(
    candidate_fd,
    revisions_fd,
    root_device,
    project_id,
    revision_id,
    expected_head,
    key_sha256,
    seeded_required,
):
    intent = _read_seed_control(
        candidate_fd,
        root_device,
        _SEED_INTENT_RECORD,
        _SEED_INTENT_CHECKSUM_DOMAIN,
        False,
    )
    binding = _read_seed_control(
        candidate_fd,
        root_device,
        _SEED_BINDING_RECORD,
        _SEED_BINDING_CHECKSUM_DOMAIN,
        True,
    )
    if (
        intent[1] is RevisionStoreErrorCode.NOT_FOUND
        and binding[1] is RevisionStoreErrorCode.NOT_FOUND
    ):
        if seeded_required:
            return (None, RevisionStoreErrorCode.RECOVERY_REQUIRED)
        return (None, None)
    if intent[1] is not RevisionStoreErrorCode.NOT_FOUND or binding[1] is not None:
        return (None, RevisionStoreErrorCode.RECOVERY_REQUIRED)
    code = _seed_control_binding_code(
        binding[0],
        project_id,
        revision_id,
        expected_head,
        key_sha256,
    )
    if code is None:
        code = _strict_source_ancestry_code(
            revisions_fd,
            root_device,
            project_id,
            expected_head,
            binding[0]["source_revision"],
            True,
        )
    payload = None
    if code is None:
        payload_result = _open_candidate_payload_readonly(candidate_fd, root_device)
        payload = payload_result[0]
        code = payload_result[1]
    if code is None:
        code = _validate_open_candidate_payload(
            candidate_fd,
            payload,
            binding[0]["source_revision"],
        )
    close_failed = _close_candidate_payload(payload)
    if code is None and close_failed:
        code = RevisionStoreErrorCode.IO_ERROR
    if code is not None:
        return (None, code)
    return (binding[0]["source_revision"], None)


def _source_integrity_code(code):
    if (
        code is RevisionStoreErrorCode.NOT_FOUND
        or code is RevisionStoreErrorCode.CORRUPT_RECORD
        or code is RevisionStoreErrorCode.CORRUPT_CONTENT
        or code is RevisionStoreErrorCode.UNSAFE_STORE
    ):
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    return code


def _strict_source_ancestry_code(
    revisions_fd,
    root_device,
    project_id,
    expected_head,
    expected_source,
    bound_source,
):
    head_result = _load_revision_fd(
        revisions_fd,
        root_device,
        project_id,
        expected_head.revision_id,
    )
    if head_result[1] is not None:
        return _source_integrity_code(head_result[1])
    if head_result[0].manifest_sha256 != expected_head.manifest_sha256:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    current = head_result[0].base_revision
    traversed = 0
    while current is not None:
        traversed += 1
        if traversed > _MAX_REVISIONS:
            return RevisionStoreErrorCode.RECOVERY_REQUIRED
        loaded = _load_revision_fd(
            revisions_fd,
            root_device,
            project_id,
            current,
        )
        if loaded[1] is not None:
            return _source_integrity_code(loaded[1])
        if loaded[0].id == expected_source.id:
            if loaded[0] != expected_source:
                if bound_source:
                    return RevisionStoreErrorCode.RECOVERY_REQUIRED
                return RevisionStoreErrorCode.CONFLICT
            return None
        current = loaded[0].base_revision
    if bound_source:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    return RevisionStoreErrorCode.CONFLICT


def _seed_source_code(project_id, expected_source):
    if type(expected_source) is not RevisionRef:
        return RevisionStoreErrorCode.INVALID_INPUT
    if expected_source.project_id != project_id:
        return RevisionStoreErrorCode.INVALID_INPUT
    if expected_source.base_revision is None or expected_source.model is None:
        return RevisionStoreErrorCode.INVALID_INPUT
    if type(expected_source.artifacts) is not type(()) or len(expected_source.artifacts) != 1:
        return RevisionStoreErrorCode.INVALID_INPUT
    if expected_source.model.name != "model.FCStd" or expected_source.model.format != "fcstd":
        return RevisionStoreErrorCode.INVALID_INPUT
    step = expected_source.artifacts[0]
    if step.name != "model.step" or step.format != "step":
        return RevisionStoreErrorCode.INVALID_INPUT
    return None


def _close_source_payload(source):
    failed = False
    if source is None:
        return failed
    failed = _close_fd(source[4]) or failed
    failed = _close_fd(source[2]) or failed
    failed = _close_fd(source[0]) or failed
    return failed


def _open_expected_source_payload(
    revisions_fd,
    root_device,
    project_id,
    expected_source,
):
    loaded = _load_revision_fd(
        revisions_fd,
        root_device,
        project_id,
        expected_source.id,
    )
    if loaded[1] is not None:
        return (None, loaded[1])
    if loaded[0] != expected_source:
        return (None, RevisionStoreErrorCode.CONFLICT)
    opened = _open_revision_directory(
        revisions_fd,
        root_device,
        expected_source.id,
    )
    if opened[1] is not None:
        return (None, opened[1])
    source_fd = opened[0]
    source_stat = None
    try:
        source_stat = os.fstat(source_fd)
    except OSError:
        _close_fd(source_fd)
        return (None, RevisionStoreErrorCode.IO_ERROR)
    if not _safe_directory_stat(source_stat, root_device):
        _close_fd(source_fd)
        return (None, RevisionStoreErrorCode.UNSAFE_STORE)
    model_open = _open_checked_file(
        source_fd,
        "model.FCStd",
        root_device,
        _MAX_FILE_BYTES,
        RevisionStoreErrorCode.CORRUPT_CONTENT,
        False,
    )
    if model_open[2] is not None:
        _close_fd(source_fd)
        return (None, model_open[2])
    step_open = _open_checked_file(
        source_fd,
        "model.step",
        root_device,
        _MAX_FILE_BYTES,
        RevisionStoreErrorCode.CORRUPT_CONTENT,
        False,
    )
    if step_open[2] is not None:
        close_failed = _close_fd(model_open[0])
        close_failed = _close_fd(source_fd) or close_failed
        if close_failed:
            return (None, RevisionStoreErrorCode.IO_ERROR)
        return (None, step_open[2])
    source = (
        source_fd,
        source_stat,
        model_open[0],
        model_open[1],
        step_open[0],
        step_open[1],
    )
    code = _hash_pinned_file(
        source_fd,
        "model.FCStd",
        source[2],
        source[3],
        expected_source.model.size_bytes,
        expected_source.model.sha256,
        _COPY_CHUNK_BYTES,
    )
    if code is None:
        code = _hash_pinned_file(
            source_fd,
            "model.step",
            source[4],
            source[5],
            expected_source.artifacts[0].size_bytes,
            expected_source.artifacts[0].sha256,
            _COPY_CHUNK_BYTES,
        )
    if code is not None:
        _close_source_payload(source)
        return (None, code)
    return (source, None)


def _validate_expected_source_payload(
    revisions_fd,
    root_device,
    project_id,
    expected_source,
    source,
):
    code = _hash_pinned_file(
        source[0],
        "model.FCStd",
        source[2],
        source[3],
        expected_source.model.size_bytes,
        expected_source.model.sha256,
        _COPY_CHUNK_BYTES,
    )
    if code is not None:
        return code
    code = _hash_pinned_file(
        source[0],
        "model.step",
        source[4],
        source[5],
        expected_source.artifacts[0].size_bytes,
        expected_source.artifacts[0].sha256,
        _COPY_CHUNK_BYTES,
    )
    if code is not None:
        return code
    directory_code = _discovery_directory_pin_code(
        revisions_fd,
        _revision_key(expected_source.id),
        source[0],
        source[1],
        root_device,
    )
    if directory_code is not None:
        return directory_code
    loaded = _load_revision_fd(
        revisions_fd,
        root_device,
        project_id,
        expected_source.id,
    )
    if loaded[1] is not None:
        return loaded[1]
    if loaded[0] != expected_source:
        return RevisionStoreErrorCode.CONFLICT
    return None


def _open_seed_destination(candidate_fd, root_device, name):
    before = _entry_stat(candidate_fd, name)
    if before[2] is not None:
        return (None, None, before[2])
    if not before[1]:
        return (None, None, RevisionStoreErrorCode.NOT_FOUND)
    if not _safe_immutable_stat(before[0], root_device):
        return (None, None, RevisionStoreErrorCode.UNSAFE_STORE)
    file_fd = None
    try:
        file_fd = os.open(
            name,
            os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=candidate_fd,
        )
    except OSError:
        return (None, None, RevisionStoreErrorCode.IO_ERROR)
    try:
        opened_stat = os.fstat(file_fd)
    except OSError:
        _close_fd(file_fd)
        return (None, None, RevisionStoreErrorCode.IO_ERROR)
    if not _safe_immutable_stat(opened_stat, root_device):
        _close_fd(file_fd)
        return (None, None, RevisionStoreErrorCode.UNSAFE_STORE)
    if not _same_copy_file_stat(before[0], opened_stat):
        _close_fd(file_fd)
        return (None, None, RevisionStoreErrorCode.UNSAFE_STORE)
    return (file_fd, opened_stat, None)


def _copy_source_to_seed_destination(
    source_parent_fd,
    source_name,
    source_fd,
    source_stat,
    destination_parent_fd,
    destination_name,
    destination_fd,
    reference,
    root_device,
):
    failed = False
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
        os.ftruncate(destination_fd, 0)
        os.lseek(destination_fd, 0, os.SEEK_SET)
    except OSError:
        failed = True
    remaining = reference.size_bytes
    copied_hash = hashlib.sha256()
    while remaining > 0 and not failed:
        maximum = min(_COPY_CHUNK_BYTES, remaining)
        chunk = None
        try:
            chunk = os.read(source_fd, maximum)
        except OSError:
            failed = True
        if not failed:
            chunk_size = _byte_count(chunk, maximum)
            if chunk_size <= 0 or chunk_size > remaining:
                failed = True
            elif not _write_all(destination_fd, chunk):
                failed = True
            else:
                copied_hash.update(chunk)
                remaining -= chunk_size
    if not failed:
        try:
            os.fchmod(destination_fd, 384)
            os.fsync(destination_fd)
        except OSError:
            failed = True
    if failed:
        return RevisionStoreErrorCode.IO_ERROR
    if copied_hash.hexdigest() != reference.sha256:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    source_code = _hash_pinned_file(
        source_parent_fd,
        source_name,
        source_fd,
        source_stat,
        reference.size_bytes,
        reference.sha256,
        _COPY_CHUNK_BYTES,
    )
    if source_code is not None:
        return _source_integrity_code(source_code)
    try:
        destination_stat = os.fstat(destination_fd)
    except OSError:
        return RevisionStoreErrorCode.IO_ERROR
    if not _safe_immutable_stat(destination_stat, root_device):
        return RevisionStoreErrorCode.UNSAFE_STORE
    return _hash_pinned_file(
        destination_parent_fd,
        destination_name,
        destination_fd,
        destination_stat,
        reference.size_bytes,
        reference.sha256,
        _COPY_CHUNK_BYTES,
    )


def _open_candidate_payload(candidate_fd, root_device):
    model = _open_seed_destination(candidate_fd, root_device, "model.FCStd")
    if model[2] is not None:
        return (None, model[2])
    step = _open_seed_destination(candidate_fd, root_device, "model.step")
    if step[2] is not None:
        close_failed = _close_fd(model[0])
        if close_failed:
            return (None, RevisionStoreErrorCode.IO_ERROR)
        return (None, step[2])
    return ((model[0], model[1], step[0], step[1]), None)


def _open_candidate_payload_readonly(candidate_fd, root_device):
    model = _open_checked_file(
        candidate_fd,
        "model.FCStd",
        root_device,
        _MAX_FILE_BYTES,
        RevisionStoreErrorCode.CORRUPT_CONTENT,
        False,
    )
    if model[2] is not None:
        return (None, model[2])
    step = _open_checked_file(
        candidate_fd,
        "model.step",
        root_device,
        _MAX_FILE_BYTES,
        RevisionStoreErrorCode.CORRUPT_CONTENT,
        False,
    )
    if step[2] is not None:
        close_failed = _close_fd(model[0])
        if close_failed:
            return (None, RevisionStoreErrorCode.IO_ERROR)
        return (None, step[2])
    return ((model[0], model[1], step[0], step[1]), None)


def _close_candidate_payload(payload):
    if payload is None:
        return False
    failed = _close_fd(payload[2])
    failed = _close_fd(payload[0]) or failed
    return failed


def _validate_open_candidate_payload(
    candidate_fd,
    payload,
    expected_source,
):
    try:
        model_stat = os.fstat(payload[0])
    except OSError:
        return RevisionStoreErrorCode.IO_ERROR
    code = _hash_pinned_file(
        candidate_fd,
        "model.FCStd",
        payload[0],
        model_stat,
        expected_source.model.size_bytes,
        expected_source.model.sha256,
        _COPY_CHUNK_BYTES,
    )
    if code is not None:
        return code
    try:
        step_stat = os.fstat(payload[2])
    except OSError:
        return RevisionStoreErrorCode.IO_ERROR
    code = _hash_pinned_file(
        candidate_fd,
        "model.step",
        payload[2],
        step_stat,
        expected_source.artifacts[0].size_bytes,
        expected_source.artifacts[0].sha256,
        _COPY_CHUNK_BYTES,
    )
    if code is not None:
        return code
    return None


def _seed_candidate_from_revision(
    store,
    project_id,
    expected_head,
    revision_id,
    expected_source,
    reservation_key,
    lease,
):
    project_code = _identifier_code(project_id, _PROJECT_PATTERN)
    revision_code = _identifier_code(revision_id, _REVISION_PATTERN)
    source_code = _seed_source_code(project_id, expected_source)
    key_result = _seed_reservation_key_digest(reservation_key)
    if project_code is not None:
        raise RevisionStoreError(project_code)
    if revision_code is not None:
        raise RevisionStoreError(revision_code)
    if type(expected_head) is not ProjectHead or expected_head.project_id != project_id:
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    if source_code is not None:
        raise RevisionStoreError(source_code)
    if key_result[1] is not None:
        raise RevisionStoreError(key_result[1])
    reservation_code = _validate_candidate_reservation(
        store,
        project_id,
        expected_head,
        revision_id,
        reservation_key,
        lease,
    )
    if reservation_code is not None:
        raise RevisionStoreError(reservation_code)
    authority = _candidate_authority(store, project_id, revision_id, lease)
    if authority[2] is not None:
        raise RevisionStoreError(authority[2])
    root_open = authority[0]
    project_open = authority[1]
    root_device = root_open[1].st_dev
    source = None
    candidate_fd = None
    candidate_stat = None
    payload = None
    code = None
    try:
        code = _strict_source_ancestry_code(
            project_open[1],
            root_device,
            project_id,
            expected_head,
            expected_source,
            False,
        )
        if code is None:
            candidate_open = _open_safe_directory(
                project_open[2],
                _candidate_key(revision_id),
                root_device,
                RevisionStoreErrorCode.NOT_FOUND,
            )
            candidate_fd = candidate_open[0]
            code = candidate_open[1]
        if code is None:
            code = _bind_seed_source(
                store,
                candidate_fd,
                root_device,
                project_id,
                revision_id,
                expected_head,
                expected_source,
                key_result[0],
            )
        if code is None:
            try:
                candidate_stat = os.fstat(candidate_fd)
            except OSError:
                code = RevisionStoreErrorCode.IO_ERROR
        if code is None and not _safe_directory_stat(candidate_stat, root_device):
            code = RevisionStoreErrorCode.UNSAFE_STORE
        if code is None:
            source_result = _open_expected_source_payload(
                project_open[1],
                root_device,
                project_id,
                expected_source,
            )
            source = source_result[0]
            code = _source_integrity_code(source_result[1])
        if code is None:
            payload_result = _open_candidate_payload(candidate_fd, root_device)
            payload = payload_result[0]
            code = payload_result[1]
        if code is None:
            code = _copy_source_to_seed_destination(
                source[0],
                "model.FCStd",
                source[2],
                source[3],
                candidate_fd,
                "model.FCStd",
                payload[0],
                expected_source.model,
                root_device,
            )
        if code is None:
            code = _copy_source_to_seed_destination(
                source[0],
                "model.step",
                source[4],
                source[5],
                candidate_fd,
                "model.step",
                payload[2],
                expected_source.artifacts[0],
                root_device,
            )
        if code is None:
            code = _validate_open_candidate_payload(
                candidate_fd,
                payload,
                expected_source,
            )
        if code is None:
            code = _source_integrity_code(
                _validate_expected_source_payload(
                    project_open[1],
                    root_device,
                    project_id,
                    expected_source,
                    source,
                )
            )
        if code is None:
            code = _discovery_directory_pin_code(
                project_open[2],
                _candidate_key(revision_id),
                candidate_fd,
                candidate_stat,
                root_device,
            )
        if code is None:
            try:
                os.fsync(candidate_fd)
                os.fsync(project_open[2])
            except OSError:
                code = RevisionStoreErrorCode.IO_ERROR
    finally:
        close_failed = _close_candidate_payload(payload)
        if candidate_fd is not None:
            close_failed = _close_fd(candidate_fd) or close_failed
        close_failed = _close_source_payload(source) or close_failed
        close_failed = _close_project_fds(project_open) or close_failed
        close_failed = _close_fd(root_open[0]) or close_failed
    if code is None and close_failed:
        code = RevisionStoreErrorCode.IO_ERROR
    if code is not None:
        raise RevisionStoreError(code)
    return None


def _validate_candidate_payload(
    store,
    project_id,
    revision_id,
    expected_source,
    lease,
):
    project_code = _identifier_code(project_id, _PROJECT_PATTERN)
    revision_code = _identifier_code(revision_id, _REVISION_PATTERN)
    source_code = _seed_source_code(project_id, expected_source)
    if project_code is not None:
        raise RevisionStoreError(project_code)
    if revision_code is not None:
        raise RevisionStoreError(revision_code)
    if source_code is not None:
        raise RevisionStoreError(source_code)
    authority = _candidate_authority(store, project_id, revision_id, lease)
    if authority[2] is not None:
        raise RevisionStoreError(authority[2])
    root_open = authority[0]
    project_open = authority[1]
    root_device = root_open[1].st_dev
    journal_result = _load_journal_fd(project_open[0], root_device)
    if journal_result[1] is not None or journal_result[0] is None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    journal = journal_result[0]
    reservation = _reservation_by_record(store, project_id, revision_id)
    if reservation[1] is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    source = None
    candidate_fd = None
    candidate_stat = None
    payload = None
    code = None
    try:
        candidate_open = _open_safe_directory(
            project_open[2],
            _candidate_key(revision_id),
            root_device,
            RevisionStoreErrorCode.NOT_FOUND,
        )
        candidate_fd = candidate_open[0]
        code = candidate_open[1]
        if code is None:
            code = _validate_seed_binding(
                candidate_fd,
                root_device,
                project_id,
                revision_id,
                journal.expected_head,
                expected_source,
                reservation[0]["key_sha256"],
            )
        if code is None:
            code = _strict_source_ancestry_code(
                project_open[1],
                root_device,
                project_id,
                journal.expected_head,
                expected_source,
                True,
            )
        if code is None:
            try:
                candidate_stat = os.fstat(candidate_fd)
            except OSError:
                code = RevisionStoreErrorCode.IO_ERROR
        if code is None and not _safe_directory_stat(candidate_stat, root_device):
            code = RevisionStoreErrorCode.UNSAFE_STORE
        if code is None:
            source_result = _open_expected_source_payload(
                project_open[1],
                root_device,
                project_id,
                expected_source,
            )
            source = source_result[0]
            code = _source_integrity_code(source_result[1])
        if code is None:
            payload_result = _open_candidate_payload_readonly(
                candidate_fd,
                root_device,
            )
            payload = payload_result[0]
            code = payload_result[1]
        if code is None:
            code = _validate_open_candidate_payload(
                candidate_fd,
                payload,
                expected_source,
            )
        if code is None:
            code = _source_integrity_code(
                _validate_expected_source_payload(
                    project_open[1],
                    root_device,
                    project_id,
                    expected_source,
                    source,
                )
            )
        if code is None:
            code = _discovery_directory_pin_code(
                project_open[2],
                _candidate_key(revision_id),
                candidate_fd,
                candidate_stat,
                root_device,
            )
    finally:
        close_failed = _close_candidate_payload(payload)
        if candidate_fd is not None:
            close_failed = _close_fd(candidate_fd) or close_failed
        close_failed = _close_source_payload(source) or close_failed
        close_failed = _close_project_fds(project_open) or close_failed
        close_failed = _close_fd(root_open[0]) or close_failed
    if code is None and close_failed:
        code = RevisionStoreErrorCode.IO_ERROR
    if code is not None:
        raise RevisionStoreError(code)
    return None


def _discovery_stat_identity(value):
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


def _discovery_directory_stat(directory_fd, root_device):
    try:
        value = os.fstat(directory_fd)
    except OSError:
        return (None, RevisionStoreErrorCode.IO_ERROR)
    if not _safe_directory_stat(value, root_device):
        return (None, RevisionStoreErrorCode.UNSAFE_STORE)
    return (value, None)


def _discovery_directory_pin_code(
    parent_fd,
    name,
    directory_fd,
    opened_stat,
    root_device,
):
    after = _discovery_directory_stat(directory_fd, root_device)
    if after[1] is not None:
        return after[1]
    current = after[0]
    opened_identity = _discovery_stat_identity(opened_stat)
    if _discovery_stat_identity(current) != opened_identity:
        return RevisionStoreErrorCode.UNSAFE_STORE
    entry = _entry_stat(parent_fd, name)
    if entry[2] is not None:
        return entry[2]
    if not entry[1] or not _safe_directory_stat(entry[0], root_device):
        return RevisionStoreErrorCode.UNSAFE_STORE
    if _discovery_stat_identity(entry[0]) != opened_identity:
        return RevisionStoreErrorCode.UNSAFE_STORE
    return None


def _discovery_pair_name(value):
    return value[0]


def _discovery_revision_id(value):
    return value.id


def _discovery_project_id(value):
    return value.project_id


def _discovery_entries(directory_fd):
    iterator = None
    values = []
    code = None
    try:
        try:
            iterator = os.scandir(directory_fd)
            for entry in iterator:
                name = entry.name
                if type(name) is not str or name in {".", ".."}:
                    code = RevisionStoreErrorCode.UNSAFE_STORE
                    break
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    code = RevisionStoreErrorCode.IO_ERROR
                    break
                values.append((name, entry_stat))
        except OSError:
            code = RevisionStoreErrorCode.IO_ERROR
    finally:
        if iterator is not None:
            try:
                iterator.close()
            except OSError:
                if code is None:
                    code = RevisionStoreErrorCode.IO_ERROR
    if code is not None:
        return (None, code)
    return (tuple(sorted(values, key=_discovery_pair_name)), None)


def _discovery_record(
    parent_fd,
    name,
    root_device,
    maximum,
    missing_code,
    replaceable,
):
    opened = _open_checked_file(
        parent_fd,
        name,
        root_device,
        maximum,
        missing_code,
        replaceable,
    )
    if opened[2] is not None:
        return (None, None, opened[2])
    file_fd = opened[0]
    opened_stat = opened[1]
    raw = _read_pinned_record(
        parent_fd,
        name,
        file_fd,
        opened_stat,
        maximum,
        _COPY_CHUNK_BYTES,
    )
    close_failed = _close_fd(file_fd)
    if raw[1] is not None:
        return (None, None, raw[1])
    if close_failed:
        return (None, None, RevisionStoreErrorCode.IO_ERROR)
    return (raw[0], opened_stat, None)


def _discovery_content_stat(
    parent_fd,
    name,
    root_device,
    expected_size,
):
    opened = _open_checked_file(
        parent_fd,
        name,
        root_device,
        _MAX_FILE_BYTES,
        RevisionStoreErrorCode.CORRUPT_CONTENT,
        False,
    )
    if opened[2] is not None:
        return (None, opened[2])
    file_fd = opened[0]
    before = opened[1]
    code = None
    after = None
    if before.st_size != expected_size:
        code = RevisionStoreErrorCode.CORRUPT_CONTENT
    if code is None:
        try:
            after = os.fstat(file_fd)
        except OSError:
            code = RevisionStoreErrorCode.IO_ERROR
    if code is None and not _same_copy_file_stat(before, after):
        code = RevisionStoreErrorCode.CORRUPT_CONTENT
    if code is None:
        code = _pinned_file_entry_code(
            parent_fd,
            name,
            before,
            RevisionStoreErrorCode.CORRUPT_CONTENT,
        )
    close_failed = _close_fd(file_fd)
    if code is None and close_failed:
        code = RevisionStoreErrorCode.IO_ERROR
    if code is not None:
        return (None, code)
    return (_discovery_stat_identity(before), None)


def _discovery_manifest(
    revisions_fd,
    root_device,
    project_id,
    physical_name,
):
    revision_open = _open_safe_directory(
        revisions_fd,
        physical_name,
        root_device,
        RevisionStoreErrorCode.CORRUPT_RECORD,
    )
    if revision_open[1] is not None:
        return (None, None, revision_open[1])
    revision_fd = revision_open[0]
    revision_initial = _discovery_directory_stat(revision_fd, root_device)
    if revision_initial[1] is not None:
        close_failed = _close_fd(revision_fd)
        code = revision_initial[1]
        if close_failed and code is None:
            code = RevisionStoreErrorCode.IO_ERROR
        return (None, None, code)
    revision = None
    identity = None
    code = None
    try:
        entries = _discovery_entries(revision_fd)
        code = entries[1]
        manifest = None
        manifest_stat = None
        if code is None:
            manifest = _discovery_record(
                revision_fd,
                "manifest.json",
                root_device,
                _MAX_MANIFEST_BYTES,
                RevisionStoreErrorCode.CORRUPT_RECORD,
                False,
            )
            code = manifest[2]
            manifest_stat = manifest[1]
        if code is None:
            parsed = _parse_checked_record(
                manifest[0],
                _MANIFEST_CHECKSUM_DOMAIN,
                _MAX_MANIFEST_BYTES,
            )
            code = parsed[1]
        if code is None:
            revision_result = _revision_from_manifest(parsed[0], manifest[0])
            revision = revision_result[0]
            code = revision_result[1]
        if code is None and (
            revision.project_id != project_id or _revision_key(revision.id) != physical_name
        ):
            code = RevisionStoreErrorCode.CORRUPT_RECORD
        expected_names = ("manifest.json",)
        references = ()
        if code is None and revision.model is not None:
            expected_names = expected_names + (revision.model.name,)
            references = references + (revision.model,)
        if code is None:
            for artifact in revision.artifacts:
                expected_names = expected_names + (artifact.name,)
                references = references + (artifact,)
        if code is None:
            if len(entries[0]) != len(expected_names):
                code = RevisionStoreErrorCode.CORRUPT_RECORD
            else:
                for item in entries[0]:
                    if item[0] not in expected_names:
                        code = RevisionStoreErrorCode.CORRUPT_RECORD
                        break
        content_identities = []
        if code is None:
            for reference in references:
                content = _discovery_content_stat(
                    revision_fd,
                    reference.name,
                    root_device,
                    reference.size_bytes,
                )
                if content[1] is not None:
                    code = content[1]
                    break
                content_identities.append((reference.name, content[0]))
        if code is None:
            try:
                directory_stat = os.fstat(revision_fd)
            except OSError:
                code = RevisionStoreErrorCode.IO_ERROR
        if code is None and not _safe_directory_stat(directory_stat, root_device):
            code = RevisionStoreErrorCode.UNSAFE_STORE
        if code is None:
            identity = (
                revision.id,
                hashlib.sha256(manifest[0]).hexdigest(),
                _discovery_stat_identity(manifest_stat),
                _discovery_stat_identity(directory_stat),
                tuple(content_identities),
            )
    finally:
        if code is None:
            code = _discovery_directory_pin_code(
                revisions_fd,
                physical_name,
                revision_fd,
                revision_initial[0],
                root_device,
            )
        close_failed = _close_fd(revision_fd)
    if code is None and close_failed:
        code = RevisionStoreErrorCode.IO_ERROR
    if code is not None:
        return (None, None, code)
    return (revision, identity, None)


def _discovery_candidate(
    candidates_fd,
    root_device,
    revision_id,
    reservation,
):
    candidate_open = _open_safe_directory(
        candidates_fd,
        _candidate_key(revision_id),
        root_device,
        RevisionStoreErrorCode.RECOVERY_REQUIRED,
    )
    if candidate_open[1] is not None:
        return candidate_open[1]
    candidate_fd = candidate_open[0]
    candidate_initial = _discovery_directory_stat(candidate_fd, root_device)
    if candidate_initial[1] is not None:
        close_failed = _close_fd(candidate_fd)
        code = candidate_initial[1]
        if close_failed and code is None:
            code = RevisionStoreErrorCode.IO_ERROR
        return code
    code = None
    try:
        entries = _discovery_entries(candidate_fd)
        code = entries[1]
        has_model = False
        has_step = False
        has_intent = False
        has_binding = False
        if code is None:
            for item in entries[0]:
                if item[0] == "model.FCStd":
                    has_model = True
                elif item[0] == "model.step":
                    has_step = True
                elif item[0] == _SEED_INTENT_RECORD:
                    has_intent = True
                elif item[0] == _SEED_BINDING_RECORD:
                    has_binding = True
                else:
                    code = RevisionStoreErrorCode.RECOVERY_REQUIRED
                    break
        seeded_required = reservation["ceiling_files"] == 9
        if code is None and (not has_model or not has_step):
            code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        if code is None and seeded_required:
            if len(entries[0]) != 3 or has_intent == has_binding:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        if code is None and not seeded_required:
            if len(entries[0]) != 2 or has_intent or has_binding:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        if code is None:
            intent = _read_seed_control(
                candidate_fd,
                root_device,
                _SEED_INTENT_RECORD,
                _SEED_INTENT_CHECKSUM_DOMAIN,
                False,
            )
            binding = _read_seed_control(
                candidate_fd,
                root_device,
                _SEED_BINDING_RECORD,
                _SEED_BINDING_CHECKSUM_DOMAIN,
                True,
            )
            if intent[1] is not None and intent[1] is not RevisionStoreErrorCode.NOT_FOUND:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
            elif binding[1] is not None and binding[1] is not RevisionStoreErrorCode.NOT_FOUND:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
            elif intent[1] is None and binding[1] is None:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
            elif intent[1] is None:
                code = _seed_control_binding_code(
                    intent[0],
                    reservation["project_id"],
                    reservation["revision_id"],
                    reservation["expected_head"],
                    reservation["key_sha256"],
                )
            elif binding[1] is None:
                code = _seed_control_binding_code(
                    binding[0],
                    reservation["project_id"],
                    reservation["revision_id"],
                    reservation["expected_head"],
                    reservation["key_sha256"],
                )
        total_size = 0
        if code is None:
            for _name, entry_stat in entries[0]:
                if (
                    not _safe_candidate_stat(entry_stat, root_device)
                    or stat.S_IMODE(entry_stat.st_mode) & 0o022
                    or entry_stat.st_size < 0
                    or entry_stat.st_size > _MAX_FILE_BYTES
                ):
                    code = RevisionStoreErrorCode.UNSAFE_STORE
                    break
                if _name == "model.FCStd" or _name == "model.step":
                    total_size += entry_stat.st_size
        if code is None and total_size > _MAX_REVISION_BYTES:
            code = RevisionStoreErrorCode.BUDGET_EXCEEDED
    finally:
        if code is None:
            code = _discovery_directory_pin_code(
                candidates_fd,
                _candidate_key(revision_id),
                candidate_fd,
                candidate_initial[0],
                root_device,
            )
        close_failed = _close_fd(candidate_fd)
    if code is None and close_failed:
        code = RevisionStoreErrorCode.IO_ERROR
    return code


def _discovery_depths(revisions):
    roots = []
    for revision_id, revision in revisions.items():
        if revision.base_revision is None:
            roots.append(revision_id)
    if len(roots) != 1:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    depths = {}
    for revision_id in revisions:
        trail = []
        active = {}
        current = revision_id
        while current not in depths:
            if current in active:
                return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
            active[current] = True
            trail.append(current)
            revision = revisions.get(current)
            if revision is None:
                return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
            if revision.base_revision is None:
                depth = 0
                break
            current = revision.base_revision
        else:
            depth = depths[current]
        while trail:
            item = trail.pop()
            if revisions[item].base_revision is None:
                depths[item] = 0
                depth = 0
            else:
                depth += 1
                depths[item] = depth
    for revision_id in revisions:
        if depths[revision_id] < 0:
            return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    return (depths, None)


def _head_matches_discovery_graph(head, revisions, depths):
    revision = revisions.get(head.revision_id)
    return bool(
        revision is not None
        and revision.manifest_sha256 == head.manifest_sha256
        and depths.get(head.revision_id) == head.generation
    )


def _discovery_journal_code(
    *,
    project_id,
    head,
    journal,
    revisions,
    depths,
    reservations,
    candidate_names,
    candidates_fd,
    root_device,
):
    if journal is None:
        if reservations or candidate_names:
            return RevisionStoreErrorCode.RECOVERY_REQUIRED
        return None
    if journal.project_id != project_id or not _head_matches_discovery_graph(
        journal.expected_head, revisions, depths
    ):
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    if journal.state is CommitJournalState.STAGING:
        if (
            head != journal.expected_head
            or journal.candidate_revision in revisions
            or len(reservations) != 1
            or candidate_names != (_candidate_key(journal.candidate_revision),)
        ):
            return RevisionStoreErrorCode.RECOVERY_REQUIRED
        reservation = reservations[0]
        if (
            reservation["kind"] != "candidate"
            or reservation["project_id"] != project_id
            or reservation["revision_id"] != journal.candidate_revision
            or reservation["expected_head"] != journal.expected_head
            or reservation["state"] != "staged"
            or reservation["project_temp"] is not None
            or reservation["revision_temp"] is not None
        ):
            return RevisionStoreErrorCode.RECOVERY_REQUIRED
        return _discovery_candidate(
            candidates_fd,
            root_device,
            journal.candidate_revision,
            reservation,
        )
    if reservations or candidate_names:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    sealed = revisions.get(journal.candidate_revision)
    if journal.state is CommitJournalState.PREPARED:
        if (
            sealed is None
            or sealed.base_revision != journal.expected_head.revision_id
            or sealed.manifest_sha256 != journal.manifest_sha256
        ):
            return RevisionStoreErrorCode.RECOVERY_REQUIRED
        if head != journal.expected_head and not _new_head_matches(head, journal):
            return RevisionStoreErrorCode.RECOVERY_REQUIRED
        return None
    if journal.state is CommitJournalState.COMMITTED:
        if (
            not _new_head_matches(head, journal)
            or sealed is None
            or sealed.base_revision != journal.expected_head.revision_id
            or sealed.manifest_sha256 != journal.manifest_sha256
        ):
            return RevisionStoreErrorCode.RECOVERY_REQUIRED
        return None
    if journal.state is CommitJournalState.NOT_COMMITTED:
        if head != journal.expected_head:
            return RevisionStoreErrorCode.RECOVERY_REQUIRED
        if sealed is None:
            if journal.manifest_sha256 != journal.expected_head.manifest_sha256:
                return RevisionStoreErrorCode.RECOVERY_REQUIRED
        elif (
            sealed.base_revision != journal.expected_head.revision_id
            or sealed.manifest_sha256 != journal.manifest_sha256
        ):
            return RevisionStoreErrorCode.RECOVERY_REQUIRED
        return None
    return RevisionStoreErrorCode.RECOVERY_REQUIRED


def _discovery_reservation_index(reservations):
    grouped = {}
    for reservation in reservations:
        project_id = reservation["project_id"]
        values = grouped.get(project_id)
        if values is None:
            values = []
            grouped[project_id] = values
        values.append(reservation)
    frozen = {}
    for project_id, values in grouped.items():
        frozen[project_id] = tuple(values)
    return frozen


def _discovery_reservations_for_project(index, project_id):
    return index.get(project_id, ())


def _discovery_project(
    root_fd,
    root_device,
    physical_name,
    reservation_index,
):
    project_open = _open_safe_directory(
        root_fd,
        physical_name,
        root_device,
        RevisionStoreErrorCode.CORRUPT_RECORD,
    )
    if project_open[1] is not None:
        return (None, project_open[1])
    project_fd = project_open[0]
    project_initial = _discovery_directory_stat(project_fd, root_device)
    if project_initial[1] is not None:
        close_failed = _close_fd(project_fd)
        code = project_initial[1]
        if close_failed and code is None:
            code = RevisionStoreErrorCode.IO_ERROR
        return (None, code)
    revisions_fd = None
    candidates_fd = None
    revisions_initial = None
    candidates_initial = None
    result = None
    code = None
    try:
        entries = _discovery_entries(project_fd)
        code = entries[1]
        entry_names = ()
        if code is None:
            for item in entries[0]:
                entry_names = entry_names + (item[0],)
                if item[0] not in (
                    "HEAD.json",
                    "journal.json",
                    "revisions",
                    "candidates",
                ):
                    code = RevisionStoreErrorCode.UNSAFE_STORE
                    break
        head_record = None
        if code is None:
            head_record = _discovery_record(
                project_fd,
                "HEAD.json",
                root_device,
                _MAX_HEAD_BYTES,
                RevisionStoreErrorCode.CORRUPT_RECORD,
                True,
            )
            code = head_record[2]
        if code is None:
            parsed_head = _parse_checked_record(
                head_record[0],
                _HEAD_CHECKSUM_DOMAIN,
                _MAX_HEAD_BYTES,
            )
            code = parsed_head[1]
        if code is None:
            head_result = _head_from_record(parsed_head[0])
            head = head_result[0]
            code = head_result[1]
        if code is None and _project_key(head.project_id) != physical_name:
            code = RevisionStoreErrorCode.CORRUPT_RECORD
        journal = None
        journal_identity = None
        if code is None and "journal.json" in entry_names:
            journal_record = _discovery_record(
                project_fd,
                "journal.json",
                root_device,
                _MAX_JOURNAL_BYTES,
                RevisionStoreErrorCode.CORRUPT_RECORD,
                True,
            )
            code = journal_record[2]
            if code is None:
                parsed_journal = _parse_checked_record(
                    journal_record[0],
                    _JOURNAL_CHECKSUM_DOMAIN,
                    _MAX_JOURNAL_BYTES,
                )
                code = parsed_journal[1]
            if code is None:
                journal_result = _journal_from_record(parsed_journal[0])
                journal = journal_result[0]
                code = journal_result[1]
            if code is None:
                journal_identity = (
                    hashlib.sha256(journal_record[0]).hexdigest(),
                    _discovery_stat_identity(journal_record[1]),
                )
        expected_entries = ("HEAD.json", "revisions", "candidates")
        if journal is not None:
            expected_entries = expected_entries + ("journal.json",)
        if code is None:
            if len(entry_names) != len(expected_entries):
                code = RevisionStoreErrorCode.CORRUPT_RECORD
            else:
                for name in entry_names:
                    if name not in expected_entries:
                        code = RevisionStoreErrorCode.CORRUPT_RECORD
                        break
        if code is None:
            revisions_open = _open_safe_directory(
                project_fd,
                "revisions",
                root_device,
                RevisionStoreErrorCode.UNSAFE_STORE,
            )
            code = revisions_open[1]
            revisions_fd = revisions_open[0]
            if code is None:
                revisions_initial = _discovery_directory_stat(
                    revisions_fd,
                    root_device,
                )
                code = revisions_initial[1]
        if code is None:
            candidates_open = _open_safe_directory(
                project_fd,
                "candidates",
                root_device,
                RevisionStoreErrorCode.UNSAFE_STORE,
            )
            code = candidates_open[1]
            candidates_fd = candidates_open[0]
            if code is None:
                candidates_initial = _discovery_directory_stat(
                    candidates_fd,
                    root_device,
                )
                code = candidates_initial[1]
        revision_entries = None
        if code is None:
            revision_entries = _discovery_entries(revisions_fd)
            code = revision_entries[1]
        revisions = {}
        revision_identities = []
        if code is None:
            for name, entry_stat in revision_entries[0]:
                if re.fullmatch(r"[0-9a-f]{64}", name) is None or not _safe_directory_stat(
                    entry_stat, root_device
                ):
                    code = RevisionStoreErrorCode.UNSAFE_STORE
                    break
                loaded = _discovery_manifest(
                    revisions_fd,
                    root_device,
                    head.project_id,
                    name,
                )
                if loaded[2] is not None:
                    code = loaded[2]
                    break
                if loaded[0].id in revisions:
                    code = RevisionStoreErrorCode.CORRUPT_RECORD
                    break
                revisions[loaded[0].id] = loaded[0]
                revision_identities.append(loaded[1])
        if code is None and not revisions:
            code = RevisionStoreErrorCode.CORRUPT_RECORD
        depths = None
        if code is None:
            depth_result = _discovery_depths(revisions)
            depths = depth_result[0]
            code = depth_result[1]
        if code is None and not _head_matches_discovery_graph(head, revisions, depths):
            code = RevisionStoreErrorCode.CORRUPT_RECORD
        candidate_entries = None
        if code is None:
            candidate_entries = _discovery_entries(candidates_fd)
            code = candidate_entries[1]
        candidate_names = []
        if code is None:
            for name, entry_stat in candidate_entries[0]:
                if re.fullmatch(r"[0-9a-f]{64}", name) is None or not _safe_directory_stat(
                    entry_stat, root_device
                ):
                    code = RevisionStoreErrorCode.UNSAFE_STORE
                    break
                candidate_names.append(name)
        project_reservations = ()
        if code is None:
            project_reservations = _discovery_reservations_for_project(
                reservation_index,
                head.project_id,
            )
        if code is None:
            code = _discovery_journal_code(
                project_id=head.project_id,
                head=head,
                journal=journal,
                revisions=revisions,
                depths=depths,
                reservations=project_reservations,
                candidate_names=tuple(candidate_names),
                candidates_fd=candidates_fd,
                root_device=root_device,
            )
        ancestry = []
        if code is None:
            current = head.revision_id
            seen = {}
            while current is not None:
                if current in seen or current not in revisions:
                    code = RevisionStoreErrorCode.CORRUPT_RECORD
                    break
                seen[current] = True
                revision = revisions[current]
                ancestry.append(
                    RevisionSnapshotEntry(
                        id=revision.id,
                        project_id=revision.project_id,
                        base_revision=revision.base_revision,
                        manifest_sha256=revision.manifest_sha256,
                    )
                )
                current = revision.base_revision
        if code is None and len(ancestry) != head.generation + 1:
            code = RevisionStoreErrorCode.CORRUPT_RECORD
        try:
            project_stat = os.fstat(project_fd)
        except OSError:
            project_stat = None
            if code is None:
                code = RevisionStoreErrorCode.IO_ERROR
        reservation_identities = []
        if code is None:
            for reservation in project_reservations:
                reservation_body = _reservation_body(
                    reservation["kind"],
                    reservation["project_id"],
                    reservation["expected_head"],
                    reservation["revision_id"],
                    reservation["key_sha256"],
                    reservation["ceiling_files"],
                    reservation["state"],
                    reservation["project_temp"],
                    reservation["revision_temp"],
                )
                reservation_identities.append(
                    hashlib.sha256(_canonical_bytes(reservation_body)).hexdigest()
                )
            ordered_revision_identities = tuple(
                sorted(revision_identities, key=_discovery_pair_name)
            )
            state_body = (
                head.project_id,
                hashlib.sha256(head_record[0]).hexdigest(),
                _discovery_stat_identity(head_record[1]),
                _discovery_stat_identity(project_stat),
                journal_identity,
                ordered_revision_identities,
                tuple(reservation_identities),
                tuple(candidate_names),
            )
            state_sha256 = hashlib.sha256(
                _DISCOVERY_PROJECT_STATE_DOMAIN + _canonical_bytes(state_body)
            ).hexdigest()
            ordered = tuple(sorted(ancestry, key=_discovery_revision_id))
            result = RevisionAncestrySnapshot(
                project_id=head.project_id,
                head=head,
                revisions=ordered,
                state_sha256=state_sha256,
            )
    finally:
        close_failed = False
        if code is None and candidates_fd is not None:
            code = _discovery_directory_pin_code(
                project_fd,
                "candidates",
                candidates_fd,
                candidates_initial[0],
                root_device,
            )
        if code is None and revisions_fd is not None:
            code = _discovery_directory_pin_code(
                project_fd,
                "revisions",
                revisions_fd,
                revisions_initial[0],
                root_device,
            )
        if code is None:
            code = _discovery_directory_pin_code(
                root_fd,
                physical_name,
                project_fd,
                project_initial[0],
                root_device,
            )
        if candidates_fd is not None:
            close_failed = _close_fd(candidates_fd) or close_failed
        if revisions_fd is not None:
            close_failed = _close_fd(revisions_fd) or close_failed
        close_failed = _close_fd(project_fd) or close_failed
    if code is None and close_failed:
        code = RevisionStoreErrorCode.IO_ERROR
    if code is not None:
        return (None, code)
    return (result, None)


def _discovery_quota_binding(root_fd, root_device):
    opened = _open_quota_directories(root_fd, root_device, False)
    if opened[2] is not None:
        return (None, opened[2])
    if opened[0] is None:
        return (None, None)
    quota_fd = opened[0]
    reservations_fd = opened[1]
    quota_stat = _discovery_directory_stat(quota_fd, root_device)
    reservations_stat = _discovery_directory_stat(reservations_fd, root_device)
    code = quota_stat[1]
    if code is None:
        code = reservations_stat[1]
    if code is None:
        code = _discovery_directory_pin_code(
            quota_fd,
            _RESERVATIONS_DIRECTORY,
            reservations_fd,
            reservations_stat[0],
            root_device,
        )
    if code is None:
        code = _discovery_directory_pin_code(
            root_fd,
            _QUOTA_DIRECTORY,
            quota_fd,
            quota_stat[0],
            root_device,
        )
    close_failed = _close_two(reservations_fd, quota_fd)
    if code is None and close_failed:
        code = RevisionStoreErrorCode.IO_ERROR
    if code is not None:
        return (None, code)
    return (
        (
            _discovery_stat_identity(quota_stat[0]),
            _discovery_stat_identity(reservations_stat[0]),
        ),
        None,
    )


def _discovery_store_snapshot(store):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        raise RevisionStoreError(quota[1])
    quota_lease = quota[0]
    root_fd = None
    failure = None
    projects = None
    ancestries = None
    root_initial = None
    try:
        root_open = _open_store_root(store)
        failure = root_open[2]
        if failure is None:
            root_fd = root_open[0]
            root_device = root_open[1].st_dev
            root_initial = root_open[1]
        quota_binding = None
        if failure is None:
            quota_binding_result = _discovery_quota_binding(
                root_fd,
                root_device,
            )
            quota_binding = quota_binding_result[0]
            failure = quota_binding_result[1]
        reservations = None
        if failure is None:
            reservation_result = _load_reservations(root_fd, root_device)
            reservations = reservation_result[0]
            failure = reservation_result[1]
        reservation_index = None
        if failure is None:
            reservation_index = _discovery_reservation_index(reservations)
        quota_snapshot = None
        if failure is None:
            snapshot_result = _quota_snapshot(
                root_fd,
                root_device,
                reservations,
            )
            quota_snapshot = snapshot_result[0]
            failure = snapshot_result[1]
        if failure is None and quota_snapshot["over_limit"]:
            failure = RevisionStoreErrorCode.RESOURCE_EXHAUSTED
        if failure is None and quota_snapshot["temporary_entries"]:
            failure = RevisionStoreErrorCode.RECOVERY_REQUIRED
        root_entries = None
        if failure is None:
            root_entries = _discovery_entries(root_fd)
            failure = root_entries[1]
        physical_projects = []
        if failure is None:
            for name, entry_stat in root_entries[0]:
                if name == _QUOTA_DIRECTORY:
                    continue
                if re.fullmatch(r"[0-9a-f]{64}", name) is None or not _safe_directory_stat(
                    entry_stat, root_device
                ):
                    failure = RevisionStoreErrorCode.UNSAFE_STORE
                    break
                physical_projects.append(name)
        discovered = []
        project_ids = {}
        if failure is None:
            for name in physical_projects:
                scanned = _discovery_project(
                    root_fd,
                    root_device,
                    name,
                    reservation_index,
                )
                if scanned[1] is not None:
                    failure = scanned[1]
                    break
                if scanned[0].project_id in project_ids:
                    failure = RevisionStoreErrorCode.CORRUPT_RECORD
                    break
                project_ids[scanned[0].project_id] = True
                discovered.append(scanned[0])
        if failure is None:
            for reservation in reservations:
                if (
                    reservation["kind"] != "candidate"
                    or reservation["project_id"] not in project_ids
                ):
                    failure = RevisionStoreErrorCode.RECOVERY_REQUIRED
                    break
        if failure is None:
            discovered = sorted(discovered, key=_discovery_project_id)
            project_values = []
            ancestries = {}
            for item in discovered:
                project_values.append(
                    ProjectSnapshotEntry(
                        project_id=item.project_id,
                        generation=item.head.generation,
                        revision_id=item.head.revision_id,
                        manifest_sha256=item.head.manifest_sha256,
                        state_sha256=item.state_sha256,
                    )
                )
                ancestries[item.project_id] = item
            projects = tuple(project_values)
        if failure is None:
            final_quota_binding = _discovery_quota_binding(
                root_fd,
                root_device,
            )
            failure = final_quota_binding[1]
            if failure is None and final_quota_binding[0] != quota_binding:
                failure = RevisionStoreErrorCode.UNSAFE_STORE
        if failure is None:
            root_verification = _open_store_root(store)
            failure = root_verification[2]
            if failure is None:
                if _discovery_stat_identity(root_verification[1]) != _discovery_stat_identity(
                    root_initial
                ):
                    failure = RevisionStoreErrorCode.UNSAFE_STORE
                if _close_fd(root_verification[0]) and failure is None:
                    failure = RevisionStoreErrorCode.IO_ERROR
    finally:
        close_failed = root_fd is not None and _close_fd(root_fd)
        release_code = _release_quota_lease(quota_lease)
    if failure is not None:
        raise RevisionStoreError(failure)
    if close_failed or release_code is not None or projects is None or ancestries is None:
        raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
    return (projects, ancestries)


class LocalRevisionStore:
    __slots__ = ("_identity", "_lease_manager", "_parts", "_pid", "_root")

    def __init__(self, root, lease_manager, *, trust):
        if type(lease_manager) is not ResourceLeaseManager:
            raise TypeError("lease_manager must be a ResourceLeaseManager")
        if type(trust) is not RevisionStoreRootTrust:
            raise RevisionStoreError(RevisionStoreErrorCode.UNSAFE_STORE)
        if trust is not RevisionStoreRootTrust.TRUSTED_LOCAL:
            raise RevisionStoreError(RevisionStoreErrorCode.UNSAFE_STORE)
        coerced = _coerce_path(root)
        if coerced[1] is not None:
            raise RevisionStoreError(coerced[1])
        opened = _open_root(coerced[0][1], None)
        if opened[2] is not None:
            raise RevisionStoreError(opened[2])
        identity = (opened[1].st_dev, opened[1].st_ino)
        if _close_fd(opened[0]):
            raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
        self._root = coerced[0][0]
        self._parts = coerced[0][1]
        self._identity = identity
        self._lease_manager = lease_manager
        self._pid = os.getpid()

    def begin_revision(self, project_id, expected_head, lease):
        return _begin_revision(self, project_id, expected_head, lease)

    def discovery_namespace(self):
        device = self._identity[0]
        inode = self._identity[1]
        return hashlib.sha256(
            _DISCOVERY_NAMESPACE_DOMAIN
            + bytes(str(device), "utf-8")
            + b":"
            + bytes(str(inode), "utf-8")
        ).digest()

    def candidate_artifact_path(self, project_id, revision_id, format, lease):
        if type(format) is not str or format != "step":
            raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
        authority = _candidate_authority(self, project_id, revision_id, lease)
        if authority[2] is not None:
            raise RevisionStoreError(authority[2])
        close_failed = _close_project_fds(authority[1])
        close_failed = _close_fd(authority[0][0]) or close_failed
        if close_failed:
            raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
        return (
            self._root
            / _project_key(project_id)
            / "candidates"
            / _candidate_key(revision_id)
            / "model.step"
        )

    def candidate_model_path(self, project_id, revision_id, lease):
        authority = _candidate_authority(self, project_id, revision_id, lease)
        if authority[2] is not None:
            raise RevisionStoreError(authority[2])
        close_failed = _close_project_fds(authority[1])
        close_failed = _close_fd(authority[0][0]) or close_failed
        if close_failed:
            raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
        return (
            self._root
            / _project_key(project_id)
            / "candidates"
            / _candidate_key(revision_id)
            / "model.FCStd"
        )

    def seed_candidate_from_revision(
        self,
        project_id,
        expected_head,
        revision_id,
        expected_source,
        reservation_key,
        lease,
    ):
        return _seed_candidate_from_revision(
            self,
            project_id,
            expected_head,
            revision_id,
            expected_source,
            reservation_key,
            lease,
        )

    def validate_candidate_payload(
        self,
        project_id,
        revision_id,
        expected_source,
        lease,
    ):
        return _validate_candidate_payload(
            self,
            project_id,
            revision_id,
            expected_source,
            lease,
        )

    def commit_revision(self, project_id, expected_head, revision_id, lease):
        return _commit_revision(self, project_id, expected_head, revision_id, lease)

    def copy_revision_artifacts_at(
        self,
        *,
        expected_revision: RevisionRef,
        destination_directory_fd: int,
        cursors: tuple[RevisionCopyCursor, ...],
        chunk_bytes: int,
    ) -> None:
        return _copy_revision_artifacts_at(
            self,
            expected_revision,
            destination_directory_fd,
            cursors,
            chunk_bytes,
        )

    def import_trusted_fcstd(
        self,
        project_id,
        source,
        expected_sha256,
        expected_size,
        lease,
    ):
        return _initialize_project(
            self,
            project_id,
            source,
            expected_sha256,
            expected_size,
            lease,
        )

    def import_trusted_fcstd_at(
        self,
        project_id,
        *,
        source_parent_fd,
        source_name,
        expected_binding,
        expected_sha256,
        expected_size,
        lease,
    ):
        return _initialize_project(
            self,
            project_id,
            None,
            expected_sha256,
            expected_size,
            lease,
            (source_parent_fd, source_name, expected_binding),
        )

    def initialize_empty_project(self, project_id, lease):
        return _initialize_project(self, project_id, None, None, None, lease)

    def load_head(self, project_id):
        return _load_head(self, project_id)

    def load_revision(self, project_id, revision_id):
        return _load_revision(self, project_id, revision_id)

    def observe_model_source(self, project_id, revision_id):
        return _observe_model_source(self, project_id, revision_id)

    def snapshot_projects(self):
        return _discovery_store_snapshot(self)[0]

    def snapshot_revisions(self, project_id):
        code = _identifier_code(project_id, _PROJECT_PATTERN)
        if code is not None:
            raise RevisionStoreError(code)
        snapshot = _discovery_store_snapshot(self)[1].get(project_id)
        if snapshot is None:
            raise RevisionStoreError(RevisionStoreErrorCode.NOT_FOUND)
        return snapshot

    def prepare_revision(
        self,
        project_id,
        expected_head,
        revision_id,
        manifest_sha256,
        lease,
    ):
        return _prepare_revision(
            self,
            project_id,
            expected_head,
            revision_id,
            manifest_sha256,
            lease,
        )

    def reconcile(self, project_id, lease):
        return _reconcile(self, project_id, lease)

    def revision_artifact_path(self, project_id, revision_id, artifact_id):
        return _revision_artifact_path(self, project_id, revision_id, artifact_id)

    def revision_model_path(self, project_id, revision_id):
        return _revision_model_path(self, project_id, revision_id)

    def rollback_revision(self, project_id, revision_id, lease):
        return _rollback_revision(self, project_id, revision_id, lease)

    def seal_revision(self, project_id, revision_id, lease):
        return _seal_revision(self, project_id, revision_id, lease)

    def validate_project_write_lease(self, project_id, lease):
        code = _require_mutation(self, project_id, lease)
        if code is not None:
            raise RevisionStoreError(code)
        return None


def _load_head(store, project_id):
    code = _identifier_code(project_id, _PROJECT_PATTERN)
    if code is not None:
        raise RevisionStoreError(code)
    loaded = _load_store_project(store, project_id)
    if loaded[2] is not None:
        raise RevisionStoreError(loaded[2])
    root_open = loaded[0]
    project_open = loaded[1]
    result = _load_head_fd(
        project_open[0],
        project_open[1],
        root_open[1].st_dev,
        project_id,
    )
    close_failed = _close_project_fds(project_open)
    close_failed = _close_fd(root_open[0]) or close_failed
    if result[1] is not None:
        raise RevisionStoreError(result[1])
    if close_failed:
        raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
    return result[0]


def _load_revision(store, project_id, revision_id):
    project_code = _identifier_code(project_id, _PROJECT_PATTERN)
    if project_code is not None:
        raise RevisionStoreError(project_code)
    revision_code = _identifier_code(revision_id, _REVISION_PATTERN)
    if revision_code is not None:
        raise RevisionStoreError(revision_code)
    loaded = _load_store_project(store, project_id)
    if loaded[2] is not None:
        raise RevisionStoreError(loaded[2])
    result = _load_revision_fd(
        loaded[1][1],
        loaded[0][1].st_dev,
        project_id,
        revision_id,
    )
    close_failed = _close_project_fds(loaded[1])
    close_failed = _close_fd(loaded[0][0]) or close_failed
    if result[1] is not None:
        raise RevisionStoreError(result[1])
    if close_failed:
        raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
    return result[0]


def _observe_model_source(store, project_id, revision_id):
    project_code = _identifier_code(project_id, _PROJECT_PATTERN)
    if project_code is not None:
        raise RevisionStoreError(project_code)
    if revision_id is not None:
        revision_code = _identifier_code(revision_id, _REVISION_PATTERN)
        if revision_code is not None:
            raise RevisionStoreError(revision_code)
    attempt = 0
    while attempt < _MAX_RECORD_OPEN_ATTEMPTS:
        attempt += 1
        loaded = _load_store_project(store, project_id)
        if loaded[2] is not None:
            raise RevisionStoreError(loaded[2])
        root_open = loaded[0]
        project_open = loaded[1]
        code = None
        observation = None
        retry = False
        head_before = _load_head_record_fd(
            project_open[0],
            root_open[1].st_dev,
            project_id,
        )
        if head_before[1] is not None:
            code = head_before[1]
        target_revision_id = revision_id
        if code is None and target_revision_id is None:
            target_revision_id = head_before[0].revision_id
        head_revision = None
        target_revision = None
        target_binding = None
        if code is None and target_revision_id == head_before[0].revision_id:
            target_result = _load_revision_fd(
                project_open[1],
                root_open[1].st_dev,
                project_id,
                target_revision_id,
                True,
            )
            if target_result[1] is not None:
                code = target_result[1]
            else:
                target_revision = target_result[0][0]
                target_binding = target_result[0][1]
                head_revision = target_revision
        elif code is None:
            head_result = _load_revision_fd(
                project_open[1],
                root_open[1].st_dev,
                project_id,
                head_before[0].revision_id,
            )
            if head_result[1] is not None:
                code = head_result[1]
            else:
                head_revision = head_result[0]
            if code is None:
                target_result = _load_revision_fd(
                    project_open[1],
                    root_open[1].st_dev,
                    project_id,
                    target_revision_id,
                    True,
                )
                if target_result[1] is not None:
                    code = target_result[1]
                else:
                    target_revision = target_result[0][0]
                    target_binding = target_result[0][1]
        if code is None and (
            head_revision.manifest_sha256 != head_before[0].manifest_sha256
            or head_revision.project_id != project_id
            or head_revision.id != head_before[0].revision_id
        ):
            code = RevisionStoreErrorCode.CORRUPT_RECORD
        head_after = (None, None)
        if code is None:
            head_after = _load_head_record_fd(
                project_open[0],
                root_open[1].st_dev,
                project_id,
            )
            if head_after[1] is not None:
                code = head_after[1]
            elif head_after[0] != head_before[0]:
                retry = True
        if code is None and not retry:
            code = _directory_binding_code(
                root_open[0],
                _project_key(project_id),
                project_open[0],
                root_open[1].st_dev,
            )
        if code is None and not retry:
            code = _directory_binding_code(
                project_open[0],
                "revisions",
                project_open[1],
                root_open[1].st_dev,
            )
        if code is None and not retry:
            root_verification = _open_store_root(store)
            if root_verification[2] is not None:
                code = root_verification[2]
            else:
                if not _same_source_parent(root_verification[1], root_open[1]):
                    code = RevisionStoreErrorCode.UNSAFE_STORE
                if _close_fd(root_verification[0]) and code is None:
                    code = RevisionStoreErrorCode.IO_ERROR
        if code is None and not retry:
            model_path = (
                store._root
                / _project_key(project_id)
                / "revisions"
                / _revision_key(target_revision_id)
                / target_revision.model.name
            )
            if (
                type(head_after[0]) is not ProjectHead
                or type(target_revision) is not RevisionRef
                or type(target_binding) is not RevisionSourceBinding
            ):
                code = RevisionStoreErrorCode.CORRUPT_RECORD
            else:
                observation = RevisionSourceObservation(
                    head=head_after[0],
                    revision=target_revision,
                    model_path=model_path,
                    model_binding=target_binding,
                )
        close_failed = _close_project_fds(project_open)
        close_failed = _close_fd(root_open[0]) or close_failed
        if close_failed:
            raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
        if code is not None:
            raise RevisionStoreError(code)
        if retry:
            continue
        return observation
    raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)


def _same_copy_file_stat(left, right):
    if left.st_dev != right.st_dev or left.st_ino != right.st_ino:
        return False
    if left.st_mode != right.st_mode or left.st_uid != right.st_uid:
        return False
    if left.st_nlink != right.st_nlink or left.st_size != right.st_size:
        return False
    if left.st_mtime_ns != right.st_mtime_ns:
        return False
    if left.st_ctime_ns != right.st_ctime_ns:
        return False
    return True


def _pinned_file_entry_code(parent_fd, name, expected_stat, missing_code):
    entry = _entry_stat(parent_fd, name)
    if entry[2] is not None:
        return entry[2]
    if not entry[1]:
        return missing_code
    if not _same_copy_file_stat(entry[0], expected_stat):
        return RevisionStoreErrorCode.CORRUPT_CONTENT
    return None


def _read_pinned_record(parent_fd, name, file_fd, opened_stat, maximum, chunk_bytes):
    if opened_stat.st_size <= 0 or opened_stat.st_size > maximum:
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    failed = False
    try:
        os.lseek(file_fd, 0, os.SEEK_SET)
    except OSError:
        failed = True
    remaining = opened_stat.st_size
    raw = b""
    while remaining > 0 and not failed:
        chunk = None
        try:
            chunk = os.read(file_fd, min(chunk_bytes, remaining))
        except OSError:
            failed = True
        if not failed:
            chunk_size = _byte_count(chunk, min(chunk_bytes, remaining))
            if chunk_size <= 0 or chunk_size > remaining:
                failed = True
            else:
                raw = raw + chunk
                remaining -= chunk_size
    after_stat = None
    if not failed:
        try:
            after_stat = os.fstat(file_fd)
            os.lseek(file_fd, 0, os.SEEK_SET)
        except OSError:
            failed = True
    if failed or after_stat is None:
        return (None, RevisionStoreErrorCode.IO_ERROR)
    if not _same_copy_file_stat(after_stat, opened_stat):
        return (None, RevisionStoreErrorCode.CORRUPT_RECORD)
    entry_code = _pinned_file_entry_code(
        parent_fd,
        name,
        opened_stat,
        RevisionStoreErrorCode.CORRUPT_RECORD,
    )
    if entry_code is not None:
        if entry_code is RevisionStoreErrorCode.CORRUPT_CONTENT:
            entry_code = RevisionStoreErrorCode.CORRUPT_RECORD
        return (None, entry_code)
    return (raw, None)


def _hash_pinned_file(
    parent_fd,
    name,
    file_fd,
    opened_stat,
    expected_size,
    expected_sha256,
    chunk_bytes,
):
    if opened_stat.st_size != expected_size:
        return RevisionStoreErrorCode.CORRUPT_CONTENT
    failed = False
    try:
        os.lseek(file_fd, 0, os.SEEK_SET)
    except OSError:
        failed = True
    remaining = expected_size
    pinned_file_hash_state = hashlib.sha256()
    while remaining > 0 and not failed:
        maximum = min(chunk_bytes, remaining)
        chunk = None
        try:
            chunk = os.read(file_fd, maximum)
        except OSError:
            failed = True
        if not failed:
            chunk_size = _byte_count(chunk, maximum)
            if chunk_size <= 0 or chunk_size > remaining:
                failed = True
            else:
                pinned_file_hash_state.update(chunk)
                remaining -= chunk_size
    after_stat = None
    if not failed:
        try:
            after_stat = os.fstat(file_fd)
            os.lseek(file_fd, 0, os.SEEK_SET)
        except OSError:
            failed = True
    if failed or after_stat is None:
        return RevisionStoreErrorCode.IO_ERROR
    if not _same_copy_file_stat(after_stat, opened_stat):
        return RevisionStoreErrorCode.CORRUPT_CONTENT
    entry_code = _pinned_file_entry_code(
        parent_fd,
        name,
        opened_stat,
        RevisionStoreErrorCode.CORRUPT_CONTENT,
    )
    if entry_code is not None:
        return entry_code
    if pinned_file_hash_state.hexdigest() != expected_sha256:
        return RevisionStoreErrorCode.CORRUPT_CONTENT
    return None


def _hash_pinned_prefix(file_fd, opened_stat, prefix_size, chunk_bytes):
    if prefix_size < 0 or prefix_size > opened_stat.st_size:
        return (None, RevisionStoreErrorCode.INVALID_INPUT)
    failed = False
    try:
        os.lseek(file_fd, 0, os.SEEK_SET)
    except OSError:
        failed = True
    remaining = prefix_size
    pinned_prefix_hash_state = hashlib.sha256()
    while remaining > 0 and not failed:
        maximum = min(chunk_bytes, remaining)
        chunk = None
        try:
            chunk = os.read(file_fd, maximum)
        except OSError:
            failed = True
        if not failed:
            chunk_size = _byte_count(chunk, maximum)
            if chunk_size <= 0 or chunk_size > remaining:
                failed = True
            else:
                pinned_prefix_hash_state.update(chunk)
                remaining -= chunk_size
    after_stat = None
    if not failed:
        try:
            after_stat = os.fstat(file_fd)
            os.lseek(file_fd, 0, os.SEEK_SET)
        except OSError:
            failed = True
    if failed or after_stat is None:
        return (None, RevisionStoreErrorCode.IO_ERROR)
    if not _same_copy_file_stat(after_stat, opened_stat):
        return (None, RevisionStoreErrorCode.CORRUPT_CONTENT)
    return (pinned_prefix_hash_state.hexdigest(), None)


def _copy_request_parts(expected_revision, destination_directory_fd, cursors, chunk_bytes):
    if type(expected_revision) is not RevisionRef:
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    if type(destination_directory_fd) is not int or destination_directory_fd < 0:
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    if type(cursors) is not type(()):
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    if type(chunk_bytes) is not int or chunk_bytes <= 0 or chunk_bytes > _COPY_CHUNK_BYTES:
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    if expected_revision.base_revision is None or expected_revision.model is None:
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    artifacts = expected_revision.artifacts
    if type(artifacts) is not type(()) or len(artifacts) != 1:
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    model = expected_revision.model
    step = artifacts[0]
    if (
        type(step) is not RevisionArtifactRef
        or model.name != "model.FCStd"
        or model.format != "fcstd"
        or step.name != "model.step"
        or step.format != "step"
    ):
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    cursor_count = len(cursors)
    if cursor_count > 2:
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    for cursor in cursors:
        if type(cursor) is not RevisionCopyCursor:
            return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    if cursor_count >= 1 and cursors[0].name != model.name:
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    if cursor_count == 2 and cursors[1].name != step.name:
        return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    model_cursor = None
    step_cursor = None
    if cursor_count >= 1:
        model_cursor = cursors[0]
        if model_cursor.size_bytes > model.size_bytes:
            return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
        if model_cursor.size_bytes == model.size_bytes and model_cursor.sha256 != model.sha256:
            return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    if cursor_count == 2:
        step_cursor = cursors[1]
        if model_cursor.size_bytes != model.size_bytes:
            return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
        if step_cursor.size_bytes > step.size_bytes:
            return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
        if step_cursor.size_bytes == step.size_bytes and step_cursor.sha256 != step.sha256:
            return (None, None, None, None, RevisionStoreErrorCode.INVALID_INPUT)
    return (model, step, model_cursor, step_cursor, None)


def _copy_destination_names(directory_fd):
    iterator = None
    names = ()
    code = None
    entry_count = 0
    try:
        try:
            iterator = os.scandir(directory_fd)
            for entry in iterator:
                entry_count += 1
                if entry_count > 2:
                    code = RevisionStoreErrorCode.CORRUPT_CONTENT
                    break
                name = entry.name
                if type(name) is not str or name not in {"model.FCStd", "model.step"}:
                    code = RevisionStoreErrorCode.CORRUPT_CONTENT
                    break
                names = names + (name,)
        except OSError:
            code = RevisionStoreErrorCode.IO_ERROR
    finally:
        if iterator is not None:
            try:
                iterator.close()
            except OSError:
                if code is None:
                    code = RevisionStoreErrorCode.IO_ERROR
    return (names, code)


def _destination_write_flags():
    return os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _destination_create_flags():
    return os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _open_destination_cursor(directory_fd, root_device, cursor, chunk_bytes):
    before = _entry_stat(directory_fd, cursor.name)
    if before[2] is not None:
        return (None, None, before[2])
    if not before[1]:
        return (None, None, RevisionStoreErrorCode.CORRUPT_CONTENT)
    if not _safe_immutable_stat(before[0], root_device) or before[0].st_size != cursor.size_bytes:
        return (None, None, RevisionStoreErrorCode.CORRUPT_CONTENT)
    file_fd = None
    try:
        file_fd = os.open(
            cursor.name,
            _destination_write_flags(),
            dir_fd=directory_fd,
        )
    except OSError:
        return (None, None, RevisionStoreErrorCode.CORRUPT_CONTENT)
    opened_stat = None
    try:
        opened_stat = os.fstat(file_fd)
    except OSError:
        if _close_fd(file_fd):
            return (None, None, RevisionStoreErrorCode.RECOVERY_REQUIRED)
        return (None, None, RevisionStoreErrorCode.IO_ERROR)
    if not _safe_immutable_stat(opened_stat, root_device) or not _same_copy_file_stat(
        opened_stat, before[0]
    ):
        if _close_fd(file_fd):
            return (None, None, RevisionStoreErrorCode.RECOVERY_REQUIRED)
        return (None, None, RevisionStoreErrorCode.CORRUPT_CONTENT)
    code = _hash_pinned_file(
        directory_fd,
        cursor.name,
        file_fd,
        opened_stat,
        cursor.size_bytes,
        cursor.sha256,
        chunk_bytes,
    )
    if code is not None:
        if _close_fd(file_fd):
            code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        return (None, None, code)
    return (file_fd, opened_stat, None)


def _create_destination_file(directory_fd, root_device, name):
    file_fd = None
    try:
        file_fd = os.open(name, _destination_create_flags(), 384, dir_fd=directory_fd)
    except OSError:
        return (None, None, RevisionStoreErrorCode.IO_ERROR)
    opened_stat = None
    code = None
    try:
        os.fchmod(file_fd, 384)
        opened_stat = os.fstat(file_fd)
        if not _safe_immutable_stat(opened_stat, root_device) or opened_stat.st_size != 0:
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
        if code is None:
            os.fsync(file_fd)
            os.fsync(directory_fd)
    except OSError:
        code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    if code is not None:
        if _close_fd(file_fd):
            code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        return (None, None, code)
    return (file_fd, opened_stat, None)


def _copy_file_suffix(source_fd, destination_fd, offset, total_size, chunk_bytes):
    try:
        os.lseek(source_fd, offset, os.SEEK_SET)
        os.lseek(destination_fd, offset, os.SEEK_SET)
    except OSError:
        return RevisionStoreErrorCode.IO_ERROR
    remaining = total_size - offset
    while remaining > 0:
        maximum = min(chunk_bytes, remaining)
        chunk = None
        try:
            chunk = os.read(source_fd, maximum)
        except OSError:
            return RevisionStoreErrorCode.IO_ERROR
        chunk_size = _byte_count(chunk, maximum)
        if chunk_size <= 0 or chunk_size > remaining:
            return RevisionStoreErrorCode.CORRUPT_CONTENT
        if not _write_all(destination_fd, chunk):
            return RevisionStoreErrorCode.IO_ERROR
        remaining -= chunk_size
    return None


def _sync_destination_file(directory_fd, file_fd):
    try:
        os.fchmod(file_fd, 384)
        os.fsync(file_fd)
        os.fsync(directory_fd)
    except OSError:
        return RevisionStoreErrorCode.RECOVERY_REQUIRED
    return None


def _copy_revision_artifacts_at(
    store,
    expected_revision,
    destination_directory_fd,
    cursors,
    chunk_bytes,
):
    request = _copy_request_parts(
        expected_revision,
        destination_directory_fd,
        cursors,
        chunk_bytes,
    )
    if request[4] is not None:
        raise RevisionStoreError(request[4])
    model = request[0]
    step = request[1]
    model_cursor = request[2]
    step_cursor = request[3]
    root_open = None
    project_open = None
    revision_fd = None
    manifest_fd = None
    manifest_stat = None
    manifest_raw = None
    model_fd = None
    model_stat = None
    step_fd = None
    step_stat = None
    destination_fd = None
    destination_stat = None
    destination_model_fd = None
    destination_step_fd = None
    code = None
    borrowed_destination_stat = None
    try:
        borrowed_destination_stat = os.fstat(destination_directory_fd)
    except OSError:
        code = RevisionStoreErrorCode.UNSAFE_STORE
    if code is None and (
        borrowed_destination_stat is None or not _safe_source_parent_stat(borrowed_destination_stat)
    ):
        code = RevisionStoreErrorCode.UNSAFE_STORE
    if code is None:
        try:
            destination_fd = os.dup(destination_directory_fd)
        except OSError:
            code = RevisionStoreErrorCode.IO_ERROR
    destination_inheritable = True
    if code is None:
        try:
            destination_stat = os.fstat(destination_fd)
            destination_inheritable = os.get_inheritable(destination_fd)
        except OSError:
            code = RevisionStoreErrorCode.IO_ERROR
    if code is None and (
        destination_inheritable
        or destination_stat is None
        or not _safe_source_parent_stat(destination_stat)
        or not _same_source_parent(destination_stat, borrowed_destination_stat)
    ):
        code = RevisionStoreErrorCode.UNSAFE_STORE
    if code is None:
        root_open = _open_store_root(store)
        if root_open[2] is not None:
            code = root_open[2]
    if code is None:
        project_open = _open_project(
            root_open[0],
            root_open[1].st_dev,
            expected_revision.project_id,
        )
        if project_open[3] is not None:
            code = project_open[3]
    revision_name = _revision_key(expected_revision.id)
    revision_stat = None
    if code is None:
        revision_open = _open_safe_directory(
            project_open[1],
            revision_name,
            root_open[1].st_dev,
            RevisionStoreErrorCode.NOT_FOUND,
        )
        revision_fd = revision_open[0]
        code = revision_open[1]
    if code is None:
        try:
            revision_stat = os.fstat(revision_fd)
        except OSError:
            code = RevisionStoreErrorCode.IO_ERROR
    if code is None:
        manifest_open = _open_checked_file(
            revision_fd,
            "manifest.json",
            root_open[1].st_dev,
            _MAX_MANIFEST_BYTES,
            RevisionStoreErrorCode.CORRUPT_RECORD,
            False,
        )
        manifest_fd = manifest_open[0]
        manifest_stat = manifest_open[1]
        code = manifest_open[2]
    if code is None:
        manifest_read = _read_pinned_record(
            revision_fd,
            "manifest.json",
            manifest_fd,
            manifest_stat,
            _MAX_MANIFEST_BYTES,
            chunk_bytes,
        )
        manifest_raw = manifest_read[0]
        code = manifest_read[1]
    if code is None:
        parsed = _parse_checked_record(
            manifest_raw,
            _MANIFEST_CHECKSUM_DOMAIN,
            _MAX_MANIFEST_BYTES,
        )
        if parsed[1] is not None:
            code = parsed[1]
        else:
            revision_result = _revision_from_manifest(parsed[0], manifest_raw)
            if revision_result[1] is not None:
                code = revision_result[1]
            elif revision_result[0] != expected_revision:
                code = RevisionStoreErrorCode.CONFLICT
    if code is None:
        model_open = _open_checked_file(
            revision_fd,
            model.name,
            root_open[1].st_dev,
            _MAX_FILE_BYTES,
            RevisionStoreErrorCode.CORRUPT_CONTENT,
            False,
        )
        model_fd = model_open[0]
        model_stat = model_open[1]
        code = model_open[2]
    if code is None:
        step_open = _open_checked_file(
            revision_fd,
            step.name,
            root_open[1].st_dev,
            _MAX_FILE_BYTES,
            RevisionStoreErrorCode.CORRUPT_CONTENT,
            False,
        )
        step_fd = step_open[0]
        step_stat = step_open[1]
        code = step_open[2]
    if code is None:
        code = _hash_pinned_file(
            revision_fd,
            model.name,
            model_fd,
            model_stat,
            model.size_bytes,
            model.sha256,
            chunk_bytes,
        )
    if code is None:
        code = _hash_pinned_file(
            revision_fd,
            step.name,
            step_fd,
            step_stat,
            step.size_bytes,
            step.sha256,
            chunk_bytes,
        )
    if code is None and model_cursor is not None:
        prefix = _hash_pinned_prefix(
            model_fd,
            model_stat,
            model_cursor.size_bytes,
            chunk_bytes,
        )
        if prefix[1] is not None:
            code = prefix[1]
        elif prefix[0] != model_cursor.sha256:
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
    if code is None and step_cursor is not None:
        prefix = _hash_pinned_prefix(
            step_fd,
            step_stat,
            step_cursor.size_bytes,
            chunk_bytes,
        )
        if prefix[1] is not None:
            code = prefix[1]
        elif prefix[0] != step_cursor.sha256:
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
    destination_names = None
    if code is None:
        names_result = _copy_destination_names(destination_fd)
        destination_names = names_result[0]
        code = names_result[1]
    if code is None:
        if len(cursors) == 0 and destination_names != ():
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
        elif len(cursors) == 1 and destination_names != (model.name,):
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
        elif len(cursors) == 2 and (
            len(destination_names) != 2
            or model.name not in destination_names
            or step.name not in destination_names
        ):
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
    if code is None and model_cursor is not None:
        opened_destination = _open_destination_cursor(
            destination_fd,
            destination_stat.st_dev,
            model_cursor,
            chunk_bytes,
        )
        destination_model_fd = opened_destination[0]
        code = opened_destination[2]
    if code is None and step_cursor is not None:
        opened_destination = _open_destination_cursor(
            destination_fd,
            destination_stat.st_dev,
            step_cursor,
            chunk_bytes,
        )
        destination_step_fd = opened_destination[0]
        code = opened_destination[2]
    if code is None and destination_model_fd is None:
        created = _create_destination_file(
            destination_fd,
            destination_stat.st_dev,
            model.name,
        )
        destination_model_fd = created[0]
        code = created[2]
    model_offset = 0 if model_cursor is None else model_cursor.size_bytes
    if code is None and model_offset < model.size_bytes:
        code = _copy_file_suffix(
            model_fd,
            destination_model_fd,
            model_offset,
            model.size_bytes,
            chunk_bytes,
        )
        if code is None:
            current_model_stat = None
            try:
                current_model_stat = os.fstat(destination_model_fd)
            except OSError:
                code = RevisionStoreErrorCode.IO_ERROR
            if code is None and not _safe_immutable_stat(
                current_model_stat,
                destination_stat.st_dev,
            ):
                code = RevisionStoreErrorCode.CORRUPT_CONTENT
            if code is None:
                code = _hash_pinned_file(
                    destination_fd,
                    model.name,
                    destination_model_fd,
                    current_model_stat,
                    model.size_bytes,
                    model.sha256,
                    chunk_bytes,
                )
        sync_code = _sync_destination_file(destination_fd, destination_model_fd)
        if sync_code is not None:
            code = sync_code
    if code is None and destination_step_fd is None:
        created = _create_destination_file(
            destination_fd,
            destination_stat.st_dev,
            step.name,
        )
        destination_step_fd = created[0]
        code = created[2]
    step_offset = 0 if step_cursor is None else step_cursor.size_bytes
    if code is None and step_offset < step.size_bytes:
        code = _copy_file_suffix(
            step_fd,
            destination_step_fd,
            step_offset,
            step.size_bytes,
            chunk_bytes,
        )
        if code is None:
            current_step_stat = None
            try:
                current_step_stat = os.fstat(destination_step_fd)
            except OSError:
                code = RevisionStoreErrorCode.IO_ERROR
            if code is None and not _safe_immutable_stat(
                current_step_stat,
                destination_stat.st_dev,
            ):
                code = RevisionStoreErrorCode.CORRUPT_CONTENT
            if code is None:
                code = _hash_pinned_file(
                    destination_fd,
                    step.name,
                    destination_step_fd,
                    current_step_stat,
                    step.size_bytes,
                    step.sha256,
                    chunk_bytes,
                )
        sync_code = _sync_destination_file(destination_fd, destination_step_fd)
        if sync_code is not None:
            code = sync_code
    if code is None:
        final_manifest = _read_pinned_record(
            revision_fd,
            "manifest.json",
            manifest_fd,
            manifest_stat,
            _MAX_MANIFEST_BYTES,
            chunk_bytes,
        )
        if final_manifest[1] is not None:
            code = final_manifest[1]
        elif final_manifest[0] != manifest_raw:
            code = RevisionStoreErrorCode.CORRUPT_RECORD
    if code is None:
        code = _hash_pinned_file(
            revision_fd,
            model.name,
            model_fd,
            model_stat,
            model.size_bytes,
            model.sha256,
            chunk_bytes,
        )
    if code is None:
        code = _hash_pinned_file(
            revision_fd,
            step.name,
            step_fd,
            step_stat,
            step.size_bytes,
            step.sha256,
            chunk_bytes,
        )
    if code is None:
        revision_after = None
        try:
            revision_after = os.fstat(revision_fd)
        except OSError:
            code = RevisionStoreErrorCode.IO_ERROR
        if code is None and not _same_source_parent(revision_after, revision_stat):
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
    if code is None:
        revision_entry = _entry_stat(project_open[1], revision_name)
        if revision_entry[2] is not None:
            code = revision_entry[2]
        elif not revision_entry[1]:
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
        elif not _safe_directory_stat(revision_entry[0], root_open[1].st_dev):
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
        elif not _same_source_parent(revision_entry[0], revision_stat):
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
    if code is None:
        code = _source_parent_after_code(destination_fd, destination_stat)
    close_failed = False
    if destination_step_fd is not None:
        close_failed = _close_fd(destination_step_fd) or close_failed
    if destination_model_fd is not None:
        close_failed = _close_fd(destination_model_fd) or close_failed
    if destination_fd is not None:
        close_failed = _close_fd(destination_fd) or close_failed
    if step_fd is not None:
        close_failed = _close_fd(step_fd) or close_failed
    if model_fd is not None:
        close_failed = _close_fd(model_fd) or close_failed
    if manifest_fd is not None:
        close_failed = _close_fd(manifest_fd) or close_failed
    if revision_fd is not None:
        close_failed = _close_fd(revision_fd) or close_failed
    if project_open is not None:
        close_failed = _close_project_fds(project_open) or close_failed
    if root_open is not None and root_open[0] is not None:
        close_failed = _close_fd(root_open[0]) or close_failed
    if close_failed:
        code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    if code is not None:
        raise RevisionStoreError(code)
    return None


def _revision_model_path(store, project_id, revision_id):
    revision = _load_revision(store, project_id, revision_id)
    if revision.model is None:
        raise RevisionStoreError(RevisionStoreErrorCode.NOT_FOUND)
    return (
        store._root
        / _project_key(project_id)
        / "revisions"
        / _revision_key(revision_id)
        / "model.FCStd"
    )


def _revision_artifact_path(store, project_id, revision_id, artifact_id):
    artifact_code = _identifier_code(artifact_id, _ARTIFACT_PATTERN)
    if artifact_code is not None:
        raise RevisionStoreError(artifact_code)
    revision = _load_revision(store, project_id, revision_id)
    found = None
    path_artifacts = revision.artifacts
    if type(path_artifacts) is not type(()):
        raise RevisionStoreError(RevisionStoreErrorCode.CORRUPT_RECORD)
    for path_artifact in path_artifacts:
        if path_artifact.id == artifact_id:
            found = path_artifact
    if found is None:
        raise RevisionStoreError(RevisionStoreErrorCode.NOT_FOUND)
    return (
        store._root
        / _project_key(project_id)
        / "revisions"
        / _revision_key(revision_id)
        / found.name
    )


def _cleanup_revision_temp(revisions_fd, temp_name):
    revision_fd = None
    failed = False
    try:
        revision_fd = os.open(temp_name, _root_flags(), dir_fd=revisions_fd)
    except OSError:
        failed = True
    if failed or revision_fd is None:
        entry = _entry_stat(revisions_fd, temp_name)
        if entry[2] is not None:
            return True
        if not entry[1]:
            return False
        return _best_rmdir(revisions_fd, temp_name)
    failed = _best_unlink(revision_fd, "model.FCStd") or failed
    failed = _best_unlink(revision_fd, "model.step") or failed
    failed = _best_unlink(revision_fd, "manifest.json") or failed
    failed = _close_fd(revision_fd) or failed
    failed = _best_rmdir(revisions_fd, temp_name) or failed
    try:
        os.fsync(revisions_fd)
    except OSError:
        failed = True
    return failed


def _seal_revision(store, project_id, revision_id, lease):
    authority = _candidate_authority(store, project_id, revision_id, lease)
    if authority[2] is not None:
        raise RevisionStoreError(authority[2])
    root_open = authority[0]
    project_open = authority[1]
    project_fd = project_open[0]
    revisions_fd = project_open[1]
    candidates_fd = project_open[2]
    journal_result = _load_journal_fd(project_fd, root_open[1].st_dev)
    if journal_result[1] is not None or journal_result[0] is None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    journal = journal_result[0]
    reservation = _reservation_by_record(store, project_id, revision_id)
    reservation_code = reservation[1]
    if reservation_code is None:
        if (
            reservation[0]["kind"] != "candidate"
            or reservation[0]["expected_head"] != journal.expected_head
        ):
            reservation_code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    if reservation_code is None and reservation[0]["state"] == "publishing":
        bound_temp = reservation[0]["revision_temp"]
        bound_entry = _entry_stat(revisions_fd, bound_temp)
        final_entry = _entry_stat(revisions_fd, _revision_key(revision_id))
        if bound_entry[2] is not None or final_entry[2] is not None or final_entry[1]:
            reservation_code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        elif bound_entry[1] and _quota_cleanup_revision(store, revisions_fd, bound_temp):
            reservation_code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        if reservation_code is None:
            restaged = _set_reservation_phase_by_record(
                store,
                project_id,
                revision_id,
                "staged",
                None,
            )
            reservation_code = restaged[1]
    elif reservation_code is None and reservation[0]["state"] != "staged":
        reservation_code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    if reservation_code is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    candidate_name = _candidate_key(revision_id)
    candidate_open = _open_safe_directory(
        candidates_fd,
        candidate_name,
        root_open[1].st_dev,
        RevisionStoreErrorCode.NOT_FOUND,
    )
    if candidate_open[1] is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(candidate_open[1])
    candidate_fd = candidate_open[0]
    seed_result = _seed_seal_source(
        candidate_fd,
        revisions_fd,
        root_open[1].st_dev,
        project_id,
        revision_id,
        journal.expected_head,
        reservation[0]["key_sha256"],
        reservation[0]["ceiling_files"] == 9,
    )
    seed_source = seed_result[0]
    if seed_result[1] is not None:
        _close_fd(candidate_fd)
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(seed_result[1])
    model_source = _open_candidate_source(candidate_fd, root_open[1].st_dev, "model.FCStd")
    if model_source[2] is not None:
        _close_fd(candidate_fd)
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(model_source[2])
    step_source = _open_candidate_source(candidate_fd, root_open[1].st_dev, "model.step")
    if step_source[2] is not None:
        _close_fd(model_source[0])
        _close_fd(candidate_fd)
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(step_source[2])
    if model_source[1].st_size + step_source[1].st_size > _MAX_REVISION_BYTES:
        _close_fd(model_source[0])
        _close_fd(step_source[0])
        _close_fd(candidate_fd)
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.BUDGET_EXCEEDED)
    final_name = _revision_key(revision_id)
    existing = _entry_stat(revisions_fd, final_name)
    if existing[2] is not None:
        _close_fd(model_source[0])
        _close_fd(step_source[0])
        _close_fd(candidate_fd)
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(existing[2])
    if existing[1]:
        _close_fd(model_source[0])
        _close_fd(step_source[0])
        _close_fd(candidate_fd)
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)
    temp_name = ".revision." + secrets.token_hex(16) + ".tmp"
    phase_result = _set_reservation_phase_by_record(
        store,
        project_id,
        revision_id,
        "publishing",
        temp_name,
    )
    if phase_result[1] is not None:
        _close_fd(model_source[0])
        _close_fd(step_source[0])
        _close_fd(candidate_fd)
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    namespace_code = _quota_create_revision_namespace(store, revisions_fd, temp_name)
    if namespace_code is not None:
        _close_fd(model_source[0])
        _close_fd(step_source[0])
        _close_fd(candidate_fd)
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(namespace_code)
    revision_open = _open_safe_directory(
        revisions_fd,
        temp_name,
        root_open[1].st_dev,
        RevisionStoreErrorCode.IO_ERROR,
    )
    revision_fd = revision_open[0]
    code = revision_open[1]
    copied_model = None
    copied_step = None
    if code is None:
        copied_model = _copy_open_file(
            candidate_fd,
            model_source[0],
            model_source[1],
            "model.FCStd",
            revision_fd,
            "model.FCStd",
            True,
        )
        code = copied_model[2]
    if _close_fd(model_source[0]) and code is None:
        code = RevisionStoreErrorCode.IO_ERROR
    if code is None:
        copied_step = _copy_open_file(
            candidate_fd,
            step_source[0],
            step_source[1],
            "model.step",
            revision_fd,
            "model.step",
            True,
        )
        code = copied_step[2]
    if _close_fd(step_source[0]) and code is None:
        code = RevisionStoreErrorCode.IO_ERROR
    if code is None and seed_source is not None:
        if (
            copied_model[0] != seed_source.model.sha256
            or copied_model[1] != seed_source.model.size_bytes
            or copied_step[0] != seed_source.artifacts[0].sha256
            or copied_step[1] != seed_source.artifacts[0].size_bytes
        ):
            code = RevisionStoreErrorCode.CORRUPT_CONTENT
    model = None
    step = None
    if code is None:
        model_id = _new_artifact_id()
        step_id = _new_artifact_id()
        if _identifier_code(model_id, _ARTIFACT_PATTERN) is not None:
            code = RevisionStoreErrorCode.INVALID_IDENTIFIER
        elif _identifier_code(step_id, _ARTIFACT_PATTERN) is not None:
            code = RevisionStoreErrorCode.INVALID_IDENTIFIER
        elif model_id == step_id:
            code = RevisionStoreErrorCode.INVALID_INPUT
        else:
            model = RevisionArtifactRef(
                id=model_id,
                name="model.FCStd",
                format="fcstd",
                sha256=copied_model[0],
                size_bytes=copied_model[1],
            )
            step = RevisionArtifactRef(
                id=step_id,
                name="model.step",
                format="step",
                sha256=copied_step[0],
                size_bytes=copied_step[1],
            )
    if code is None:
        manifest_body = _manifest_body(
            project_id,
            revision_id,
            journal.expected_head.revision_id,
            model,
            (step,),
        )
        manifest_raw = _checked_record_bytes(manifest_body, _MANIFEST_CHECKSUM_DOMAIN)
        code = _create_durable_file(revision_fd, "manifest.json", manifest_raw, True)
    if code is None:
        sync_failed = False
        try:
            os.fsync(revision_fd)
        except OSError:
            sync_failed = True
        if sync_failed:
            code = RevisionStoreErrorCode.IO_ERROR
    if revision_fd is not None and _close_fd(revision_fd) and code is None:
        code = RevisionStoreErrorCode.IO_ERROR
    published = False
    if code is None:
        renamed = _quota_rename_directory(
            store,
            revisions_fd,
            temp_name,
            final_name,
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
        )
        code = renamed[0]
        published = renamed[1]
    sealed = None
    if published:
        manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
        sealed = RevisionRef(
            id=revision_id,
            project_id=project_id,
            base_revision=journal.expected_head.revision_id,
            manifest_sha256=manifest_digest,
            model=model,
            artifacts=(step,),
        )
    if code is None:
        readback = _load_revision_fd(
            revisions_fd,
            root_open[1].st_dev,
            project_id,
            revision_id,
        )
        if readback[1] is not None or readback[0] != sealed:
            code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    if code is None:
        prepared = CommitJournal(
            id=journal.id,
            project_id=project_id,
            expected_head=journal.expected_head,
            candidate_revision=revision_id,
            manifest_sha256=sealed.manifest_sha256,
            state=CommitJournalState.PREPARED,
        )
        prepared_raw = _checked_record_bytes(
            _journal_mapping(prepared),
            _JOURNAL_CHECKSUM_DOMAIN,
        )
        code = _quota_replace_record(
            store,
            project_fd,
            "journal.json",
            prepared_raw,
            secrets.token_hex(16),
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
        )
    candidate_close_failed = _close_fd(candidate_fd)
    cleanup_failed = False
    if code is None:
        cleanup_failed = _quota_cleanup_candidate(
            store,
            candidates_fd,
            candidate_name,
            root_open[1].st_dev,
        )
    reservation_code = None
    if code is None and not cleanup_failed:
        phase_result = _set_reservation_phase_by_record(
            store,
            project_id,
            revision_id,
            "published",
            None,
        )
        reservation_code = phase_result[1]
        if reservation_code is None:
            reservation_code = _release_reservation_by_record(
                store,
                project_id,
                revision_id,
            )
    if not published:
        _quota_cleanup_revision(store, revisions_fd, temp_name)
    close_failed = _close_project_fds(project_open)
    close_failed = _close_fd(root_open[0]) or close_failed
    if code is RevisionStoreErrorCode.DURABILITY_UNCERTAIN:
        raise RevisionStoreError(code, head_committed=False)
    if code is not None:
        raise RevisionStoreError(code)
    if reservation_code is not None:
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    if candidate_close_failed or cleanup_failed:
        raise RevisionStoreError(RevisionStoreErrorCode.CLEANUP_REQUIRED)
    if close_failed:
        raise RevisionStoreError(
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            head_committed=False,
        )
    return sealed


def _replace_head_record(project_fd, raw, token):
    temp_name = ".HEAD.json." + token + ".tmp"
    code = _create_durable_file(project_fd, temp_name, raw)
    if code is not None:
        return (code, False)
    replace_failed = False
    try:
        os.replace(temp_name, "HEAD.json", src_dir_fd=project_fd, dst_dir_fd=project_fd)
    except OSError:
        replace_failed = True
    if replace_failed:
        _best_unlink(project_fd, temp_name)
        return (RevisionStoreErrorCode.IO_ERROR, False)
    sync_failed = False
    try:
        os.fsync(project_fd)
    except OSError:
        sync_failed = True
    if sync_failed:
        return (RevisionStoreErrorCode.DURABILITY_UNCERTAIN, True)
    return (None, True)


def _quota_replace_head_record(store, project_fd, raw, token):
    quota = _acquire_quota_lease(store)
    if quota[1] is not None:
        return (quota[1], False)
    replaced = None
    release_code = None
    try:
        replaced = _replace_head_record(project_fd, raw, token)
    finally:
        release_code = _release_quota_lease(quota[0])
    if release_code is not None:
        return (RevisionStoreErrorCode.RECOVERY_REQUIRED, replaced[1])
    return replaced


def _commit_revision(store, project_id, expected_head, revision_id, lease):
    mutation_code = _require_mutation(store, project_id, lease)
    if mutation_code is not None:
        raise RevisionStoreError(mutation_code)
    revision_code = _identifier_code(revision_id, _REVISION_PATTERN)
    if revision_code is not None:
        raise RevisionStoreError(revision_code)
    if type(expected_head) is not ProjectHead or expected_head.project_id != project_id:
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_INPUT)
    loaded = _load_store_project(store, project_id)
    if loaded[2] is not None:
        raise RevisionStoreError(loaded[2])
    root_open = loaded[0]
    project_open = loaded[1]
    current_result = _load_head_fd(
        project_open[0],
        project_open[1],
        root_open[1].st_dev,
        project_id,
    )
    if current_result[1] is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(current_result[1])
    if current_result[0] != expected_head:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)
    journal_result = _load_journal_fd(project_open[0], root_open[1].st_dev)
    if journal_result[1] is not None or journal_result[0] is None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    journal = journal_result[0]
    if journal.state is not CommitJournalState.PREPARED:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)
    if journal.candidate_revision != revision_id or journal.expected_head != expected_head:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)
    sealed_result = _load_revision_fd(
        project_open[1],
        root_open[1].st_dev,
        project_id,
        revision_id,
    )
    if sealed_result[1] is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(sealed_result[1])
    sealed = sealed_result[0]
    if sealed.manifest_sha256 != journal.manifest_sha256:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.CORRUPT_RECORD)
    if sealed.base_revision != expected_head.revision_id:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.CORRUPT_RECORD)
    new_head = ProjectHead(
        project_id=project_id,
        generation=expected_head.generation + 1,
        revision_id=revision_id,
        manifest_sha256=sealed.manifest_sha256,
    )
    head_raw = _checked_record_bytes(_head_mapping(new_head), _HEAD_CHECKSUM_DOMAIN)
    replaced = _quota_replace_head_record(
        store,
        project_open[0],
        head_raw,
        secrets.token_hex(16),
    )
    if replaced[0] is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        if replaced[1]:
            raise RevisionStoreError(
                RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
                head_committed=True,
            )
        raise RevisionStoreError(replaced[0])
    committed = CommitJournal(
        id=journal.id,
        project_id=project_id,
        expected_head=journal.expected_head,
        candidate_revision=revision_id,
        manifest_sha256=journal.manifest_sha256,
        state=CommitJournalState.COMMITTED,
    )
    committed_raw = _checked_record_bytes(
        _journal_mapping(committed),
        _JOURNAL_CHECKSUM_DOMAIN,
    )
    journal_code = _quota_replace_record(
        store,
        project_open[0],
        "journal.json",
        committed_raw,
        secrets.token_hex(16),
        RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
    )
    close_failed = _close_project_fds(project_open)
    close_failed = _close_fd(root_open[0]) or close_failed
    if journal_code is not None or close_failed:
        raise RevisionStoreError(
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            head_committed=True,
        )
    return new_head


def _terminal_journal(journal, state):
    manifest_digest = journal.manifest_sha256
    if state is CommitJournalState.NOT_COMMITTED and manifest_digest is None:
        manifest_digest = journal.expected_head.manifest_sha256
    return CommitJournal(
        id=journal.id,
        project_id=journal.project_id,
        expected_head=journal.expected_head,
        candidate_revision=journal.candidate_revision,
        manifest_sha256=manifest_digest,
        state=state,
    )


def _persist_journal(store, project_fd, journal):
    raw = _checked_record_bytes(_journal_mapping(journal), _JOURNAL_CHECKSUM_DOMAIN)
    return _quota_replace_record(
        store,
        project_fd,
        "journal.json",
        raw,
        secrets.token_hex(16),
        RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
    )


def _new_head_matches(head, journal):
    if head.generation != journal.expected_head.generation + 1:
        return False
    if head.project_id != journal.project_id:
        return False
    if head.revision_id != journal.candidate_revision:
        return False
    if head.manifest_sha256 != journal.manifest_sha256:
        return False
    return True


def _reconcile(store, project_id, lease):
    mutation_code = _require_mutation(store, project_id, lease)
    if mutation_code is not None:
        raise RevisionStoreError(mutation_code)
    loaded = _load_store_project(store, project_id)
    if loaded[2] is not None:
        raise RevisionStoreError(loaded[2])
    root_open = loaded[0]
    project_open = loaded[1]
    head_result = _load_head_fd(
        project_open[0],
        project_open[1],
        root_open[1].st_dev,
        project_id,
    )
    if head_result[1] is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    head = head_result[0]
    journal_result = _load_journal_fd(project_open[0], root_open[1].st_dev)
    if journal_result[1] is not None:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    journal = journal_result[0]
    if journal is None:
        close_failed = _close_project_fds(project_open)
        close_failed = _close_fd(root_open[0]) or close_failed
        if close_failed:
            raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
        return ReconciliationResult(
            project_id=project_id,
            status=ReconciliationStatus.CLEAN,
            head=head,
            journal=None,
        )
    if journal.project_id != project_id:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    old_match = head == journal.expected_head
    new_match = _new_head_matches(head, journal)
    result_status = None
    result_journal = journal
    code = None
    cleanup = False
    if old_match:
        if journal.state is CommitJournalState.COMMITTED:
            code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        elif journal.state is CommitJournalState.PREPARED:
            sealed_result = _load_revision_fd(
                project_open[1],
                root_open[1].st_dev,
                project_id,
                journal.candidate_revision,
            )
            if sealed_result[1] is not None:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
            elif sealed_result[0].manifest_sha256 != journal.manifest_sha256:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
            elif sealed_result[0].base_revision != journal.expected_head.revision_id:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
        if code is None:
            if journal.state is not CommitJournalState.NOT_COMMITTED:
                result_journal = _terminal_journal(journal, CommitJournalState.NOT_COMMITTED)
                journal_code = _persist_journal(store, project_open[0], result_journal)
                if journal_code is not None:
                    code = RevisionStoreErrorCode.RECOVERY_REQUIRED
            if code is None:
                cleanup = True
                result_status = ReconciliationStatus.NOT_COMMITTED
    elif new_match:
        if (
            journal.state is CommitJournalState.PREPARED
            or journal.state is CommitJournalState.COMMITTED
        ):
            sealed_result = _load_revision_fd(
                project_open[1],
                root_open[1].st_dev,
                project_id,
                journal.candidate_revision,
            )
            if sealed_result[1] is not None:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
            elif sealed_result[0].manifest_sha256 != journal.manifest_sha256:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
            elif sealed_result[0].base_revision != journal.expected_head.revision_id:
                code = RevisionStoreErrorCode.RECOVERY_REQUIRED
            elif journal.state is CommitJournalState.PREPARED:
                result_journal = _terminal_journal(journal, CommitJournalState.COMMITTED)
                journal_code = _persist_journal(store, project_open[0], result_journal)
                if journal_code is not None:
                    code = RevisionStoreErrorCode.RECOVERY_REQUIRED
            if code is None:
                result_status = ReconciliationStatus.COMMITTED
        else:
            code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    else:
        code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    if cleanup and code is None:
        cleanup_failed = _quota_cleanup_candidate(
            store,
            project_open[2],
            _candidate_key(journal.candidate_revision),
            root_open[1].st_dev,
        )
        reservation = _reservation_by_record(
            store,
            project_id,
            journal.candidate_revision,
        )
        if reservation[1] is None:
            if reservation[0]["revision_temp"] is not None:
                cleanup_failed = (
                    _quota_cleanup_revision(
                        store,
                        project_open[1],
                        reservation[0]["revision_temp"],
                    )
                    or cleanup_failed
                )
        elif reservation[1] is not RevisionStoreErrorCode.NOT_FOUND:
            cleanup_failed = True
        if not cleanup_failed and reservation[1] is None:
            reservation_code = _release_reservation_by_record(
                store,
                project_id,
                journal.candidate_revision,
            )
            cleanup_failed = reservation_code is not None
        if cleanup_failed:
            result_status = ReconciliationStatus.CLEANUP_REQUIRED
    close_failed = _close_project_fds(project_open)
    close_failed = _close_fd(root_open[0]) or close_failed
    if code is not None:
        raise RevisionStoreError(code)
    if close_failed:
        raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
    return ReconciliationResult(
        project_id=project_id,
        status=result_status,
        head=head,
        journal=result_journal,
    )


def _rollback_revision(store, project_id, revision_id, lease):
    mutation_code = _require_mutation(store, project_id, lease)
    if mutation_code is not None:
        raise RevisionStoreError(mutation_code)
    revision_code = _identifier_code(revision_id, _REVISION_PATTERN)
    if revision_code is not None:
        raise RevisionStoreError(revision_code)
    loaded = _load_store_project(store, project_id)
    if loaded[2] is not None:
        raise RevisionStoreError(loaded[2])
    journal_result = _load_journal_fd(loaded[1][0], loaded[0][1].st_dev)
    close_failed = _close_project_fds(loaded[1])
    close_failed = _close_fd(loaded[0][0]) or close_failed
    if journal_result[1] is not None:
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
    if journal_result[0] is None:
        raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)
    if journal_result[0].candidate_revision != revision_id:
        raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)
    if close_failed:
        raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
    return _reconcile(store, project_id, lease)
