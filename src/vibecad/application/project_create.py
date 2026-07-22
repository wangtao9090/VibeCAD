"""Durable, bounded project-create service core.

The public adapter owns wire validation.  This module owns the replay key,
filesystem transaction, generation-zero receipt, and coherent project reads.
It intentionally has no dependency on a transport or on ``AgentApplication``.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from vibecad.application.project import (
    _current_directory_matches,
    _darwin_thread_fchdir,
)
from vibecad.application.project_api import (
    ProjectCreateResult,
    ProjectCurrentResult,
    ProjectKind,
    ProjectServicePortErrorCode,
    ProjectServicePortFailure,
)
from vibecad.execution.executor import ExecutorError, ExecutorErrorCode
from vibecad.execution.revisions import (
    ProjectHead,
    RevisionArtifactRef,
    RevisionRef,
    RevisionSourceBinding,
    RevisionStoreError,
    RevisionStoreErrorCode,
    _candidate_file_limit,
)
from vibecad.interaction.cad import CadExecutionPort, ValidatedImportEvidence
from vibecad.interaction.storage import SafeRoot, StorageFailure
from vibecad.workflow.lease import LeaseError, LeaseErrorCode

__all__ = ("DurableProjectService",)

_SCHEMA_VERSION = 1
_MAX_RECORD_BYTES = 64 * 1024
_MAX_STORE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_SOURCE_BYTES = 512 * 1024 * 1024
_MAX_ACTIVE = 8
_MAX_QUARANTINES_PER_ROLE = 2
_MAX_RECORDS = 4096
_RECOVERY_ROLES = 3
_RECOVERY_FILES_PER_BINDING = 2
_RECOVERY_FILES_PER_ROLE = _MAX_QUARANTINES_PER_ROLE * _RECOVERY_FILES_PER_BINDING
_MIN_IMPORT_RESERVATION_BYTES = 1 + (2 * _MAX_SOURCE_BYTES)
_MAX_RESERVED_IMPORTS = _MAX_STORE_BYTES // _MIN_IMPORT_RESERVATION_BYTES
_MAX_LIVE_OWNED_FILES = _RECOVERY_ROLES * _MAX_RESERVED_IMPORTS
_CATALOG_TRANSIENT_FILE_HEADROOM = 1
_MANAGED_DIRECTORY_COUNT = 4
# Every admitted record can retain one request plus, for each of the three
# artifact roles, two data tombstones and their two immutable receipts.  The
# byte reservation admits at most one live import, hence at most three owned
# artifact names in addition to those tombstones.  The catalog lease serializes
# request replacement, so one final slot covers its transient file.  Keep this
# derived rather than trading away the frozen 4096-record public capacity.
_MAX_STORE_FILES = (
    _MANAGED_DIRECTORY_COUNT
    + 1
    + _MAX_RECORDS * (1 + _RECOVERY_ROLES * _RECOVERY_FILES_PER_ROLE)
    + _MAX_LIVE_OWNED_FILES
    + _CATALOG_TRANSIENT_FILE_HEADROOM
)
_COPY_CHUNK_BYTES = 1024 * 1024
_LEASE_WAIT_SECONDS = 1.0
_LEASE_RETRY_SECONDS = 0.005

_CREATE_KEY = re.compile(r"project_create_[0-9a-f]{32}\Z")
_PROJECT_ID = re.compile(r"project_[0-9a-f]{32}\Z")
_REVISION_ID = re.compile(r"revision_[0-9a-f]{32}\Z")
_ARTIFACT_ID = re.compile(r"artifact_[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP = re.compile(r"-?[0-9]{1,20}\Z")
_REQUEST_NAME = re.compile(r"request_([0-9a-f]{32})\.json\Z")
_STAGE_NAME = re.compile(r"\.stage\.([0-9a-f]{32})\.FCStd\Z")
_WORK_NAME = re.compile(r"\.work\.([0-9a-f]{32})\.FCStd\Z")
_NORMALIZED_NAME = re.compile(r"\.normalized\.([0-9a-f]{32})\.FCStd\Z")
_QUARANTINE_NAME = re.compile(
    r"\.quarantine\.(stage|work|normalized)\.([0-9a-f]{32})\.([0-9a-f]{64})\.FCStd\Z"
)
_QUARANTINE_RECEIPT_NAME = re.compile(
    r"\.quarantine-receipt\.(stage|work|normalized)\.([0-9a-f]{32})\.([0-9a-f]{64})\.json\Z"
)
_QUARANTINE_RECEIPT_TEMP_NAME = re.compile(
    r"\.quarantine-receipt\.(stage|work|normalized)\.([0-9a-f]{32})\.([0-9a-f]{64})\.tmp\Z"
)
_TEMP_NAME = re.compile(r"\..+\.[0-9a-f]{32}\.tmp\Z")
_LEGACY_STAGE_NAME = re.compile(r"\.import\.[0-9a-f]{32}\.FCStd\Z")
_LEGACY_CLEANUP_NAME = re.compile(r"cleanup_[0-9a-f]{32}\.json\Z")

_KEY_FILE = "hmac-key.json"
_DIRECTORIES = ("requests", "staging", "work", "normalized")
_PHASES = frozenset(
    {
        "RESERVED",
        "STAGED",
        "VALIDATED",
        "CLEANUP_REQUIRED",
        "PUBLISHED",
        "REJECTED",
    }
)
_OUTCOMES = frozenset({"PUBLISHED", "REJECTED"})
_BODY_KEYS = frozenset(
    {
        "schema_version",
        "create_key",
        "key_id",
        "intent_hmac",
        "kind",
        "project_id",
        "phase",
        "source_size",
        "source_identity",
        "reservation_bytes",
        "stage",
        "work",
        "validation_started",
        "work_validated",
        "normalized",
        "outcome",
        "failure_code",
        "generation_zero",
    }
)
_BINDING_KEYS = frozenset(
    {"name", "dev", "ino", "mode", "uid", "nlink", "size", "mtime_ns", "sha256"}
)
_QUARANTINE_RECEIPT_KEYS = frozenset(
    {"schema_version", "binding", "original_name", "quarantine_name"}
)
_SOURCE_IDENTITY_KEYS = frozenset(
    {"dev", "ino", "mode", "uid", "nlink", "size", "mtime_ns", "ctime_ns"}
)
_ENVELOPE_KEYS = frozenset({"schema_version", "body", "body_sha256"})
_KEY_KEYS = frozenset({"schema_version", "key_hex", "key_id"})

_KEY_ID_DOMAIN = b"vibecad-project-create-hmac-key-v1\0"
_INTENT_DOMAIN = b"vibecad-project-create-intent-v1\0"
_RECORD_DOMAIN = b"vibecad-project-create-record-v1\0"
_QUARANTINE_BINDING_DOMAIN = b"vibecad-project-create-quarantine-binding-v1\0"
_QUARANTINE_RECEIPT_DOMAIN = b"vibecad-project-create-quarantine-receipt-v1\0"
_CATALOG_RESOURCE = "vibecad-project-create-catalog-v1"
_PER_KEY_RESOURCE_PREFIX = "vibecad-project-create-request-v1:"
_SLOT_RESOURCE_PREFIX = "vibecad-project-create-slot-v1:"


class _ServiceError(Exception):
    __slots__ = ("code",)

    def __init__(self, code: ProjectServicePortErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Binding:
    name: str
    dev: int
    ino: int
    mode: int
    uid: int
    nlink: int
    size: int
    mtime_ns: str
    sha256: str


@dataclass(frozen=True, slots=True, kw_only=True)
class _QuarantineReceipt:
    original_name: str
    quarantine_name: str
    binding: _Binding


@dataclass(frozen=True, slots=True, kw_only=True)
class _SourceIdentity:
    dev: int
    ino: int
    mode: int
    uid: int
    nlink: int
    size: int
    mtime_ns: str
    ctime_ns: str


@dataclass(frozen=True, slots=True, kw_only=True)
class _Record:
    create_key: str
    key_id: str
    intent_hmac: str
    kind: ProjectKind
    project_id: str
    phase: str
    source_size: int | None
    source_identity: _SourceIdentity | None
    reservation_bytes: int
    stage: _Binding | None
    work: _Binding | None
    validation_started: bool
    work_validated: bool
    normalized: _Binding | None
    outcome: str | None
    failure_code: str | None
    generation_zero: ProjectCreateResult | None


@dataclass(frozen=True, slots=True, kw_only=True)
class _OpenedSource:
    ancestor_fds: tuple[int, ...]
    ancestor_identities: tuple[tuple[int, ...], ...]
    ancestor_names: tuple[str, ...]
    final_name: str
    fd: int
    before: os.stat_result

    def close(self) -> None:
        first = False
        for descriptor in (self.fd, *reversed(self.ancestor_fds)):
            try:
                os.close(descriptor)
            except OSError:
                first = True
        if first:
            raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE) from None


def _strict_json(raw: bytes, *, maximum: int) -> object:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if type(key) is not str or key in result:
                raise ValueError
            if len(key.encode("utf-8")) > 256:
                raise ValueError
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=object_pairs)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE) from None
    nodes = 0
    stack = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > 8192 or depth > 64:
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        if current is None or type(current) is bool:
            continue
        if type(current) is int:
            if abs(current) > 9_007_199_254_740_991:
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
            continue
        if type(current) is str:
            if len(current.encode("utf-8")) > _MAX_RECORD_BYTES:
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
            continue
        if type(current) is list:
            stack.extend((item, depth + 1) for item in current)
            continue
        if type(current) is dict:
            stack.extend((item, depth + 1) for item in current.values())
            continue
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    return value


def _digest(domain: bytes, raw: bytes) -> str:
    return hashlib.sha256(domain + raw).hexdigest()


def _intent_digest(
    key: bytes,
    *,
    create_key: str,
    kind: ProjectKind,
    source_path: str | None,
) -> str:
    raw = _canonical(
        {
            "schema_version": _SCHEMA_VERSION,
            "create_key": create_key,
            "kind": kind.value,
            "source_path": source_path,
        }
    )
    return hmac.new(key, _INTENT_DOMAIN + raw, hashlib.sha256).hexdigest()


def _request_name(create_key: str) -> str:
    return f"request_{create_key.removeprefix('project_create_')}.json"


def _identity(value: os.stat_result) -> tuple[int, ...]:
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


def _binding_mapping(value: _Binding | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "name": value.name,
        "dev": value.dev,
        "ino": value.ino,
        "mode": value.mode,
        "uid": value.uid,
        "nlink": value.nlink,
        "size": value.size,
        "mtime_ns": value.mtime_ns,
        "sha256": value.sha256,
    }


def _source_identity_mapping(
    value: _SourceIdentity | None,
) -> dict[str, object] | None:
    if value is None:
        return None
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


def _artifact_mapping(value: RevisionArtifactRef) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "id": value.id,
        "name": value.name,
        "format": value.format,
        "sha256": value.sha256,
        "size_bytes": value.size_bytes,
    }


def _snapshot_mapping(value: ProjectCreateResult | None) -> dict[str, object] | None:
    if value is None:
        return None
    revision = value.revision
    return {
        "head": {
            "schema_version": value.head.schema_version,
            "project_id": value.head.project_id,
            "generation": value.head.generation,
            "revision_id": value.head.revision_id,
            "manifest_sha256": value.head.manifest_sha256,
        },
        "revision": {
            "schema_version": revision.schema_version,
            "id": revision.id,
            "project_id": revision.project_id,
            "base_revision": revision.base_revision,
            "manifest_sha256": revision.manifest_sha256,
            "model": None if revision.model is None else _artifact_mapping(revision.model),
            "artifacts": [_artifact_mapping(item) for item in revision.artifacts],
        },
    }


def _record_body(value: _Record) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "create_key": value.create_key,
        "key_id": value.key_id,
        "intent_hmac": value.intent_hmac,
        "kind": value.kind.value,
        "project_id": value.project_id,
        "phase": value.phase,
        "source_size": value.source_size,
        "source_identity": _source_identity_mapping(value.source_identity),
        "reservation_bytes": value.reservation_bytes,
        "stage": _binding_mapping(value.stage),
        "work": _binding_mapping(value.work),
        "validation_started": value.validation_started,
        "work_validated": value.work_validated,
        "normalized": _binding_mapping(value.normalized),
        "outcome": value.outcome,
        "failure_code": value.failure_code,
        "generation_zero": _snapshot_mapping(value.generation_zero),
    }


def _record_bytes(value: _Record) -> bytes:
    body = _record_body(value)
    body_raw = _canonical(body)
    raw = _canonical(
        {
            "schema_version": _SCHEMA_VERSION,
            "body": body,
            "body_sha256": _digest(_RECORD_DOMAIN, body_raw),
        }
    )
    if len(raw) > _MAX_RECORD_BYTES:
        raise _ServiceError(ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
    return raw


def _exact_mapping(value: object, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != keys:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    return value


def _exact_int(value: object, *, minimum: int = 0, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    return value


def _exact_string(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    return value


def _require_schema_version(value: object) -> None:
    if type(value) is not int or value != _SCHEMA_VERSION:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)


def _binding_from_mapping(
    value: object,
    pattern: re.Pattern[str],
    *,
    minimum_size: int = 1,
) -> _Binding | None:
    if value is None:
        return None
    mapping = _exact_mapping(value, _BINDING_KEYS)
    name = _exact_string(mapping["name"], pattern)
    digest = _exact_string(mapping["sha256"], _DIGEST)
    return _Binding(
        name=name,
        dev=_exact_int(mapping["dev"], maximum=9_007_199_254_740_991),
        ino=_exact_int(mapping["ino"], maximum=9_007_199_254_740_991),
        mode=_exact_int(mapping["mode"], maximum=9_007_199_254_740_991),
        uid=_exact_int(mapping["uid"], maximum=9_007_199_254_740_991),
        nlink=_exact_int(mapping["nlink"], minimum=1, maximum=9_007_199_254_740_991),
        size=_exact_int(
            mapping["size"],
            minimum=minimum_size,
            maximum=_MAX_SOURCE_BYTES,
        ),
        mtime_ns=_exact_string(mapping["mtime_ns"], _TIMESTAMP),
        sha256=digest,
    )


def _source_identity_from_mapping(value: object) -> _SourceIdentity | None:
    if value is None:
        return None
    mapping = _exact_mapping(value, _SOURCE_IDENTITY_KEYS)
    return _SourceIdentity(
        dev=_exact_int(mapping["dev"], maximum=9_007_199_254_740_991),
        ino=_exact_int(mapping["ino"], maximum=9_007_199_254_740_991),
        mode=_exact_int(mapping["mode"], maximum=9_007_199_254_740_991),
        uid=_exact_int(mapping["uid"], maximum=9_007_199_254_740_991),
        nlink=_exact_int(mapping["nlink"], minimum=1, maximum=9_007_199_254_740_991),
        size=_exact_int(mapping["size"], minimum=1, maximum=_MAX_SOURCE_BYTES),
        mtime_ns=_exact_string(mapping["mtime_ns"], _TIMESTAMP),
        ctime_ns=_exact_string(mapping["ctime_ns"], _TIMESTAMP),
    )


def _artifact_from_mapping(value: object) -> RevisionArtifactRef:
    mapping = _exact_mapping(
        value,
        frozenset({"schema_version", "id", "name", "format", "sha256", "size_bytes"}),
    )
    _require_schema_version(mapping["schema_version"])
    identifier = _exact_string(mapping["id"], _ARTIFACT_ID)
    name = mapping["name"]
    format_name = mapping["format"]
    if (
        type(name) is not str
        or name not in {"model.FCStd", "model.step"}
        or type(format_name) is not str
        or format_name not in {"fcstd", "step"}
    ):
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    return RevisionArtifactRef(
        id=identifier,
        name=name,
        format=format_name,
        sha256=_exact_string(mapping["sha256"], _DIGEST),
        size_bytes=_exact_int(mapping["size_bytes"], minimum=1, maximum=_MAX_SOURCE_BYTES),
    )


def _snapshot_from_mapping(
    value: object,
    *,
    create_key: str,
    kind: ProjectKind,
    project_id: str,
    cleanup_required: bool,
) -> ProjectCreateResult:
    snapshot = _exact_mapping(value, frozenset({"head", "revision"}))
    head_mapping = _exact_mapping(
        snapshot["head"],
        frozenset(
            {
                "schema_version",
                "project_id",
                "generation",
                "revision_id",
                "manifest_sha256",
            }
        ),
    )
    revision_mapping = _exact_mapping(
        snapshot["revision"],
        frozenset(
            {
                "schema_version",
                "id",
                "project_id",
                "base_revision",
                "manifest_sha256",
                "model",
                "artifacts",
            }
        ),
    )
    _require_schema_version(head_mapping["schema_version"])
    _require_schema_version(revision_mapping["schema_version"])
    head_project = _exact_string(head_mapping["project_id"], _PROJECT_ID)
    revision_project = _exact_string(revision_mapping["project_id"], _PROJECT_ID)
    head_revision = _exact_string(head_mapping["revision_id"], _REVISION_ID)
    revision_id = _exact_string(revision_mapping["id"], _REVISION_ID)
    head_digest = _exact_string(head_mapping["manifest_sha256"], _DIGEST)
    revision_digest = _exact_string(revision_mapping["manifest_sha256"], _DIGEST)
    if not (
        project_id == head_project == revision_project
        and head_revision == revision_id
        and head_digest == revision_digest
        and _exact_int(
            head_mapping["generation"],
            maximum=9_007_199_254_740_991,
        )
        == 0
        and revision_mapping["base_revision"] is None
    ):
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    artifacts_value = revision_mapping["artifacts"]
    if type(artifacts_value) is not list or artifacts_value:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    model_value = revision_mapping["model"]
    model = None if model_value is None else _artifact_from_mapping(model_value)
    if kind is ProjectKind.EMPTY:
        if model is not None:
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    elif model is None or model.name != "model.FCStd" or model.format != "fcstd":
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    head = ProjectHead(
        project_id=project_id,
        generation=0,
        revision_id=revision_id,
        manifest_sha256=head_digest,
    )
    revision = RevisionRef(
        id=revision_id,
        project_id=project_id,
        base_revision=None,
        manifest_sha256=revision_digest,
        model=model,
        artifacts=(),
    )
    return ProjectCreateResult(
        create_key=create_key,
        kind=kind,
        cleanup_required=cleanup_required,
        project_id=project_id,
        head=head,
        revision=revision,
    )


def _record_from_bytes(raw: bytes, *, expected_name: str) -> _Record:
    envelope = _exact_mapping(_strict_json(raw, maximum=_MAX_RECORD_BYTES), _ENVELOPE_KEYS)
    _require_schema_version(envelope["schema_version"])
    body = _exact_mapping(envelope["body"], _BODY_KEYS)
    expected_digest = _digest(_RECORD_DOMAIN, _canonical(body))
    if type(envelope["body_sha256"]) is not str or not hmac.compare_digest(
        envelope["body_sha256"], expected_digest
    ):
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    _require_schema_version(body["schema_version"])
    create_key = _exact_string(body["create_key"], _CREATE_KEY)
    if _request_name(create_key) != expected_name:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    key_id = _exact_string(body["key_id"], _DIGEST)
    intent_hmac = _exact_string(body["intent_hmac"], _DIGEST)
    project_id = _exact_string(body["project_id"], _PROJECT_ID)
    try:
        kind = ProjectKind(body["kind"])
    except (TypeError, ValueError):
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE) from None
    phase = body["phase"]
    if type(phase) is not str or phase not in _PHASES:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    source_size_value = body["source_size"]
    source_size = None
    if source_size_value is not None:
        source_size = _exact_int(source_size_value, minimum=1, maximum=_MAX_SOURCE_BYTES)
    reservation = _exact_int(body["reservation_bytes"], maximum=_MAX_STORE_BYTES)
    if type(body["validation_started"]) is not bool:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    if type(body["work_validated"]) is not bool:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    outcome = body["outcome"]
    if outcome is not None and (type(outcome) is not str or outcome not in _OUTCOMES):
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    failure = body["failure_code"]
    if failure is not None and failure != "invalid_input":
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    cleanup_required = phase == "CLEANUP_REQUIRED"
    generation = None
    if body["generation_zero"] is not None:
        generation = _snapshot_from_mapping(
            body["generation_zero"],
            create_key=create_key,
            kind=kind,
            project_id=project_id,
            cleanup_required=cleanup_required,
        )
    record = _Record(
        create_key=create_key,
        key_id=key_id,
        intent_hmac=intent_hmac,
        kind=kind,
        project_id=project_id,
        phase=phase,
        source_size=source_size,
        source_identity=_source_identity_from_mapping(body["source_identity"]),
        reservation_bytes=reservation,
        stage=_binding_from_mapping(body["stage"], _STAGE_NAME),
        work=_binding_from_mapping(body["work"], _WORK_NAME),
        validation_started=body["validation_started"],
        work_validated=body["work_validated"],
        normalized=_binding_from_mapping(body["normalized"], _NORMALIZED_NAME),
        outcome=outcome,
        failure_code=failure,
        generation_zero=generation,
    )
    _validate_record_state(record)
    return record


def _validate_record_state(value: _Record) -> None:
    if (
        type(value) is not _Record
        or type(value.create_key) is not str
        or _CREATE_KEY.fullmatch(value.create_key) is None
        or type(value.key_id) is not str
        or _DIGEST.fullmatch(value.key_id) is None
        or type(value.intent_hmac) is not str
        or _DIGEST.fullmatch(value.intent_hmac) is None
        or type(value.kind) is not ProjectKind
        or type(value.project_id) is not str
        or _PROJECT_ID.fullmatch(value.project_id) is None
        or type(value.phase) is not str
        or value.phase not in _PHASES
        or type(value.reservation_bytes) is not int
        or not 0 <= value.reservation_bytes <= _MAX_STORE_BYTES
        or type(value.validation_started) is not bool
        or type(value.work_validated) is not bool
        or (value.outcome is not None and value.outcome not in _OUTCOMES)
        or (value.failure_code is not None and value.failure_code != "invalid_input")
        or (
            value.generation_zero is not None
            and type(value.generation_zero) is not ProjectCreateResult
        )
    ):
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)

    token = value.intent_hmac[:32]
    expected_names = (
        (value.stage, f".stage.{token}.FCStd"),
        (value.work, f".work.{token}.FCStd"),
        (value.normalized, f".normalized.{token}.FCStd"),
    )
    for binding, expected in expected_names:
        if binding is None:
            continue
        if binding.name != expected or not _binding_is_safe(binding, minimum_size=1):
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)

    if value.kind is ProjectKind.EMPTY:
        if (
            any(
                item is not None
                for item in (
                    value.source_size,
                    value.source_identity,
                    value.stage,
                    value.work,
                    value.normalized,
                )
            )
            or value.validation_started
            or value.work_validated
            or value.reservation_bytes != 0
            or value.phase not in {"RESERVED", "PUBLISHED"}
        ):
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    else:
        if (
            type(value.source_size) is not int
            or not 1 <= value.source_size <= _MAX_SOURCE_BYTES
            or type(value.source_identity) is not _SourceIdentity
            or value.source_identity.size != value.source_size
            or not stat.S_ISREG(value.source_identity.mode)
            or value.source_identity.uid != os.geteuid()
            or value.source_identity.nlink != 1
        ):
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        expected_reservation = value.source_size + (2 * _MAX_SOURCE_BYTES)
        if value.phase in {"PUBLISHED", "REJECTED"}:
            if value.reservation_bytes != 0:
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        elif value.reservation_bytes != expected_reservation:
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        if value.stage is not None and value.stage.size != value.source_size:
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)

    generation = value.generation_zero
    if generation is not None:
        if not (
            generation.create_key == value.create_key
            and generation.kind is value.kind
            and generation.project_id == value.project_id
            and generation.head.project_id == value.project_id
            and generation.head.generation == 0
            and generation.revision.project_id == value.project_id
            and generation.revision.id == generation.head.revision_id
            and generation.revision.manifest_sha256 == generation.head.manifest_sha256
            and generation.revision.base_revision is None
            and generation.revision.artifacts == ()
        ):
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        if value.kind is ProjectKind.EMPTY:
            if generation.revision.model is not None:
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        elif (
            generation.revision.model is None
            or generation.revision.model.name != "model.FCStd"
            or generation.revision.model.format != "fcstd"
        ):
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)

    files = (value.stage, value.work, value.normalized)
    if value.phase == "RESERVED":
        valid = (
            all(item is None for item in files)
            and not value.validation_started
            and not value.work_validated
            and value.outcome is None
            and value.failure_code is None
            and generation is None
        )
    elif value.phase == "STAGED":
        valid = (
            value.kind is ProjectKind.IMPORT_FCSTD
            and value.stage is not None
            and value.normalized is None
            and (not value.validation_started or value.work is not None)
            and (not value.work_validated or value.work is not None)
            and (not value.work_validated or value.validation_started)
            and value.outcome is None
            and value.failure_code is None
            and generation is None
            and (
                value.validation_started
                or value.work is None
                or (value.work.size == value.stage.size and value.work.sha256 == value.stage.sha256)
            )
        )
    elif value.phase == "VALIDATED":
        valid = (
            value.kind is ProjectKind.IMPORT_FCSTD
            and value.stage is not None
            and value.work is None
            and value.validation_started
            and not value.work_validated
            and value.normalized is not None
            and value.outcome is None
            and value.failure_code is None
            and generation is None
        )
    elif value.phase == "CLEANUP_REQUIRED":
        valid = (
            value.kind is ProjectKind.IMPORT_FCSTD
            and value.stage is not None
            and value.validation_started
            and not value.work_validated
            and value.outcome in _OUTCOMES
            and (
                (
                    value.outcome == "PUBLISHED"
                    and value.work is None
                    and value.normalized is not None
                    and value.failure_code is None
                    and generation is not None
                    and generation.cleanup_required
                    and generation.revision.model is not None
                    and generation.revision.model.sha256 == value.normalized.sha256
                    and generation.revision.model.size_bytes == value.normalized.size
                )
                or (
                    value.outcome == "REJECTED"
                    and value.work is not None
                    and value.normalized is None
                    and value.failure_code == "invalid_input"
                    and generation is None
                )
            )
        )
    elif value.phase == "PUBLISHED":
        valid = (
            all(item is None for item in files)
            and not value.validation_started
            and not value.work_validated
            and value.outcome == "PUBLISHED"
            and value.failure_code is None
            and generation is not None
            and not generation.cleanup_required
        )
    else:
        valid = (
            value.kind is ProjectKind.IMPORT_FCSTD
            and all(item is None for item in files)
            and not value.validation_started
            and not value.work_validated
            and value.outcome == "REJECTED"
            and value.failure_code == "invalid_input"
            and generation is None
        )
    if not valid:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)


def _binding(info: os.stat_result, *, name: str, sha256: str) -> _Binding:
    return _Binding(
        name=name,
        dev=info.st_dev,
        ino=info.st_ino,
        mode=info.st_mode,
        uid=info.st_uid,
        nlink=info.st_nlink,
        size=info.st_size,
        mtime_ns=str(info.st_mtime_ns),
        sha256=sha256,
    )


def _binding_identity(value: _Binding) -> tuple[object, ...]:
    return (
        value.dev,
        value.ino,
        value.mode,
        value.uid,
        value.nlink,
        value.size,
        value.mtime_ns,
    )


def _info_binding_identity(value: os.stat_result) -> tuple[object, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_size,
        str(value.st_mtime_ns),
    )


def _binding_is_safe(value: object, *, minimum_size: int) -> bool:
    return (
        type(value) is _Binding
        and type(value.name) is str
        and type(value.dev) is int
        and 0 <= value.dev <= 9_007_199_254_740_991
        and type(value.ino) is int
        and 0 <= value.ino <= 9_007_199_254_740_991
        and type(value.mode) is int
        and stat.S_ISREG(value.mode)
        and stat.S_IMODE(value.mode) == 0o600
        and type(value.uid) is int
        and value.uid == os.geteuid()
        and type(value.nlink) is int
        and value.nlink == 1
        and type(value.size) is int
        and minimum_size <= value.size <= _MAX_SOURCE_BYTES
        and type(value.mtime_ns) is str
        and _TIMESTAMP.fullmatch(value.mtime_ns) is not None
        and type(value.sha256) is str
        and _DIGEST.fullmatch(value.sha256) is not None
    )


def _owned_file_parts(name: str) -> tuple[str, str] | None:
    if type(name) is not str:
        return None
    for role, pattern in (
        ("stage", _STAGE_NAME),
        ("work", _WORK_NAME),
        ("normalized", _NORMALIZED_NAME),
    ):
        match = pattern.fullmatch(name)
        if match is not None:
            return role, match.group(1)
    return None


def _quarantine_binding_digest(value: _Binding) -> str:
    return _digest(
        _QUARANTINE_BINDING_DOMAIN,
        _canonical(_binding_mapping(value)),
    )


def _quarantine_file_name(value: _Binding) -> str:
    parts = _owned_file_parts(value.name)
    if parts is None:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    role, token = parts
    return f".quarantine.{role}.{token}.{_quarantine_binding_digest(value)}.FCStd"


def _quarantine_receipt_name(value: _Binding) -> str:
    parts = _owned_file_parts(value.name)
    if parts is None:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    role, token = parts
    return f".quarantine-receipt.{role}.{token}.{_quarantine_binding_digest(value)}.json"


def _quarantine_receipt_temp_name(value: _Binding) -> str:
    return _quarantine_receipt_name(value).removesuffix(".json") + ".tmp"


def _quarantine_receipt_bytes(value: _QuarantineReceipt) -> bytes:
    body = {
        "schema_version": _SCHEMA_VERSION,
        "binding": _binding_mapping(value.binding),
        "original_name": value.original_name,
        "quarantine_name": value.quarantine_name,
    }
    return _canonical(
        {
            "schema_version": _SCHEMA_VERSION,
            "body": body,
            "body_sha256": _digest(_QUARANTINE_RECEIPT_DOMAIN, _canonical(body)),
        }
    )


def _quarantine_receipt_from_bytes(
    raw: bytes,
    *,
    expected_name: str,
) -> _QuarantineReceipt:
    envelope = _exact_mapping(_strict_json(raw, maximum=8192), _ENVELOPE_KEYS)
    _require_schema_version(envelope["schema_version"])
    body = _exact_mapping(envelope["body"], _QUARANTINE_RECEIPT_KEYS)
    if type(envelope["body_sha256"]) is not str or not hmac.compare_digest(
        envelope["body_sha256"],
        _digest(_QUARANTINE_RECEIPT_DOMAIN, _canonical(body)),
    ):
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    _require_schema_version(body["schema_version"])
    original = body["original_name"]
    if type(original) is not str:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    parts = _owned_file_parts(original)
    if parts is None:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    pattern = {
        "stage": _STAGE_NAME,
        "work": _WORK_NAME,
        "normalized": _NORMALIZED_NAME,
    }[parts[0]]
    binding = _binding_from_mapping(
        body["binding"],
        pattern,
        minimum_size=0,
    )
    if (
        binding is None
        or binding.name != original
        or not _binding_is_safe(
            binding,
            minimum_size=0,
        )
    ):
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    if _quarantine_receipt_name(binding) != expected_name:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    quarantine = body["quarantine_name"]
    if (
        type(quarantine) is not str
        or _QUARANTINE_NAME.fullmatch(quarantine) is None
        or quarantine != _quarantine_file_name(binding)
    ):
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    return _QuarantineReceipt(
        original_name=original,
        quarantine_name=quarantine,
        binding=binding,
    )


def _rename_noreplace(
    root_fd: int,
    source: str,
    destination: str,
    *,
    destination_root_fd: int | None = None,
) -> bool:
    destination_fd = root_fd if destination_root_fd is None else destination_root_fd
    if (
        type(root_fd) is not int
        or root_fd < 0
        or type(destination_fd) is not int
        or destination_fd < 0
        or type(source) is not str
        or type(destination) is not str
        or not source
        or not destination
        or "/" in source
        or "/" in destination
        or "\0" in source
        or "\0" in destination
    ):
        return False
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
            operation.restype = ctypes.c_int
            arguments = (
                root_fd,
                os.fsencode(source),
                destination_fd,
                os.fsencode(destination),
                0x00000004,
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
            operation.restype = ctypes.c_int
            arguments = (
                root_fd,
                os.fsencode(source),
                destination_fd,
                os.fsencode(destination),
                0x00000001,
            )
        else:
            return False
        ctypes.set_errno(0)
        if operation(*arguments) == 0:
            return True
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            return False
        raise OSError(error, os.strerror(error))
    except (AttributeError, UnicodeError):
        return False


def _open_hashed_owned_file(
    root: SafeRoot,
    root_fd: int,
    name: str,
) -> tuple[int, _Binding, os.stat_result] | None:
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            return None
        before = os.fstat(descriptor)
        if not root.regular_file(before, maximum=_MAX_SOURCE_BYTES):
            raise StorageFailure("unsafe quarantine candidate")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_SOURCE_BYTES:
                raise StorageFailure("oversized quarantine candidate")
            digest.update(chunk)
        after = os.fstat(descriptor)
        entry = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if _info_binding_identity(before) != _info_binding_identity(
            after
        ) or _info_binding_identity(entry) != _info_binding_identity(after):
            raise StorageFailure("quarantine candidate changed")
        binding = _binding(after, name=name, sha256=digest.hexdigest())
        result = descriptor, binding, after
        descriptor = -1
        return result
    except OSError:
        raise StorageFailure("unsafe quarantine candidate") from None
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _hash_quarantine_authority(
    root: SafeRoot,
    quarantine_name: str,
    *,
    original_name: str,
) -> _Binding:
    root_fd = -1
    descriptor = -1
    try:
        root_fd = _open_owned(root)
        opened = _open_hashed_owned_file(root, root_fd, quarantine_name)
        if opened is None:
            raise StorageFailure("quarantine authority is missing")
        descriptor, binding, _info = opened
        os.close(descriptor)
        descriptor = -1
        return replace(binding, name=original_name)
    except (OSError, _ServiceError):
        raise StorageFailure("quarantine authority is unsafe") from None
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if root_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(root_fd)


def _read_quarantine_receipt_at(
    root: SafeRoot,
    root_fd: int,
    receipt_name: str,
) -> tuple[_QuarantineReceipt, os.stat_result, bytes] | None:
    try:
        entry = os.stat(receipt_name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise StorageFailure("quarantine receipt is unsafe") from None
    if not root.regular_file(entry, maximum=8192):
        raise StorageFailure("quarantine receipt is unsafe")
    raw, info = root.read_file_at(root_fd, receipt_name, maximum=8192)
    receipt = _quarantine_receipt_from_bytes(raw, expected_name=receipt_name)
    return receipt, info, raw


def _pin_quarantine_receipt_at(
    root: SafeRoot,
    root_fd: int,
    receipt_name: str,
    *,
    expected: _QuarantineReceipt,
) -> tuple[int, os.stat_result, bytes] | None:
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                receipt_name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            return None
        before = os.fstat(descriptor)
        if before.st_size <= 0 or not root.regular_file(before, maximum=8192):
            raise StorageFailure("quarantine receipt is unsafe")
        raw = _read_small_descriptor(descriptor, maximum=8192)
        after = os.fstat(descriptor)
        current = os.stat(receipt_name, dir_fd=root_fd, follow_symlinks=False)
        parsed = _quarantine_receipt_from_bytes(raw, expected_name=receipt_name)
        if (
            _info_binding_identity(before) != _info_binding_identity(after)
            or _info_binding_identity(current) != _info_binding_identity(after)
            or parsed != expected
        ):
            raise StorageFailure("quarantine receipt changed")
        result = descriptor, after, raw
        descriptor = -1
        return result
    except OSError:
        raise StorageFailure("quarantine receipt is unsafe") from None
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _verify_pinned_quarantine_receipt_at(
    root_fd: int,
    receipt_name: str,
    descriptor: int,
    pinned_info: os.stat_result,
    pinned_raw: bytes,
    expected: _QuarantineReceipt,
) -> bool:
    try:
        before = os.fstat(descriptor)
        raw = _read_small_descriptor(descriptor, maximum=8192)
        after = os.fstat(descriptor)
        current = os.stat(receipt_name, dir_fd=root_fd, follow_symlinks=False)
        parsed = _quarantine_receipt_from_bytes(raw, expected_name=receipt_name)
        return (
            _info_binding_identity(before) == _info_binding_identity(pinned_info)
            and _info_binding_identity(after) == _info_binding_identity(pinned_info)
            and _info_binding_identity(current) == _info_binding_identity(pinned_info)
            and raw == pinned_raw
            and parsed == expected
        )
    except (OSError, StorageFailure, _ServiceError):
        return False


def _name_absent_at(root_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _entry_anchor(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
    )


def _read_small_descriptor(descriptor: int, *, maximum: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(_COPY_CHUNK_BYTES, maximum + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise StorageFailure("quarantine receipt exceeds budget")
    return b"".join(chunks)


def _quarantine_entries_at(
    root: SafeRoot,
    root_fd: int,
    original_name: str,
) -> dict[str, os.stat_result]:
    parts = _owned_file_parts(original_name)
    if parts is None:
        raise StorageFailure("invalid quarantine authority")
    role, token = parts
    prefix = f".quarantine.{role}.{token}."
    result: dict[str, os.stat_result] = {}
    try:
        names = os.listdir(root_fd)
        for candidate in names:
            if not candidate.startswith(prefix):
                continue
            match = _QUARANTINE_NAME.fullmatch(candidate)
            if match is None or match.group(1) != role or match.group(2) != token:
                raise StorageFailure("invalid quarantine entry")
            info = os.stat(candidate, dir_fd=root_fd, follow_symlinks=False)
            if not root.regular_file(info, maximum=_MAX_SOURCE_BYTES):
                raise StorageFailure("unsafe quarantine entry")
            result[candidate] = info
    except OSError:
        raise StorageFailure("unsafe quarantine entries") from None
    return result


def _quarantine_receipt_entries_at(
    root: SafeRoot,
    root_fd: int,
    original_name: str,
) -> dict[str, tuple[str, str, os.stat_result]]:
    parts = _owned_file_parts(original_name)
    if parts is None:
        raise StorageFailure("invalid quarantine receipt authority")
    role, token = parts
    prefix = f".quarantine-receipt.{role}.{token}."
    result: dict[str, tuple[str, str, os.stat_result]] = {}
    try:
        names = os.listdir(root_fd)
        for candidate in names:
            if not candidate.startswith(prefix):
                continue
            final_match = _QUARANTINE_RECEIPT_NAME.fullmatch(candidate)
            temp_match = _QUARANTINE_RECEIPT_TEMP_NAME.fullmatch(candidate)
            match = final_match or temp_match
            if match is None or match.group(1) != role or match.group(2) != token:
                raise StorageFailure("invalid quarantine receipt entry")
            info = os.stat(candidate, dir_fd=root_fd, follow_symlinks=False)
            if not root.regular_file(info, maximum=8192):
                raise StorageFailure("unsafe quarantine receipt entry")
            result[candidate] = (
                match.group(3),
                "final" if final_match is not None else "temp",
                info,
            )
    except OSError:
        raise StorageFailure("unsafe quarantine receipt entries") from None
    return result


def _write_quarantine_receipt_at(
    root: SafeRoot,
    root_fd: int,
    receipt: _QuarantineReceipt,
    *,
    quota_admit: Callable[..., tuple[int, int]],
) -> bool:
    receipt_name = _quarantine_receipt_name(receipt.binding)
    temp_name = _quarantine_receipt_temp_name(receipt.binding)
    raw = _quarantine_receipt_bytes(receipt)
    descriptor = -1
    try:
        try:
            os.stat(receipt_name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            final_exists = False
        except OSError:
            return False
        else:
            final_exists = True
        if final_exists:
            try:
                os.stat(temp_name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError:
                return False
            else:
                return False
            loaded = _read_quarantine_receipt_at(root, root_fd, receipt_name)
            return loaded is not None and loaded[0] == receipt and loaded[2] == raw

        created = False
        try:
            descriptor = os.open(
                temp_name,
                os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            quota_admit(extra_bytes=len(raw), extra_files=1)
            try:
                descriptor = os.open(
                    temp_name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=root_fd,
                )
            except FileExistsError:
                return False
            created = True
            os.fsync(root_fd)
        before = os.fstat(descriptor)
        if not root.regular_file(before, maximum=len(raw)):
            return False
        prefix = _read_small_descriptor(descriptor, maximum=len(raw))
        after_read = os.fstat(descriptor)
        current = os.stat(temp_name, dir_fd=root_fd, follow_symlinks=False)
        if not (
            _info_binding_identity(before) == _info_binding_identity(after_read)
            and _info_binding_identity(current) == _info_binding_identity(after_read)
            and raw.startswith(prefix)
        ):
            return False
        remaining = raw[len(prefix) :]
        if remaining:
            if not created:
                quota_admit(extra_bytes=len(remaining), extra_files=0)
            os.lseek(descriptor, 0, os.SEEK_END)
            view = memoryview(remaining)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    return False
                view = view[written:]
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        current = os.stat(temp_name, dir_fd=root_fd, follow_symlinks=False)
        if (
            _entry_anchor(after) != _entry_anchor(before)
            or after.st_size != len(raw)
            or _info_binding_identity(current) != _info_binding_identity(after)
        ):
            return False
        checked_raw = _read_small_descriptor(descriptor, maximum=8192)
        if checked_raw != raw:
            return False
        quota_admit(extra_bytes=0, extra_files=0)
        if not _rename_noreplace(root_fd, temp_name, receipt_name):
            return False
        os.fsync(root_fd)
        final = os.stat(receipt_name, dir_fd=root_fd, follow_symlinks=False)
        return _info_binding_identity(final) == _info_binding_identity(after)
    except (OSError, StorageFailure, _ServiceError):
        return False
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _is_quarantine_tombstone(info: os.stat_result, authority: _Binding) -> bool:
    return info.st_size == 0 and _entry_anchor(info) == (
        authority.dev,
        authority.ino,
        authority.mode,
        authority.uid,
        authority.nlink,
    )


def _zero_quarantine_descriptor(
    root_fd: int,
    descriptor: int,
    quarantine_name: str,
    before: os.stat_result,
) -> bool:
    try:
        os.ftruncate(descriptor, 0)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        current = os.stat(quarantine_name, dir_fd=root_fd, follow_symlinks=False)
        if (
            _entry_anchor(after) != _entry_anchor(before)
            or after.st_size != 0
            or _entry_anchor(current) != _entry_anchor(after)
            or current.st_size != 0
        ):
            return False
        os.fsync(root_fd)
        return True
    except OSError:
        return False


def _quarantine_unlink(
    root: SafeRoot,
    name: str,
    *,
    expected: _Binding | None,
    receipt_required: bool,
    quota_admit: Callable[..., tuple[int, int]],
) -> bool:
    if (
        _owned_file_parts(name) is None
        or (expected is not None and expected.name != name)
        or receipt_required is not True
    ):
        return False
    root_fd = -1
    opened_fd = -1
    receipt_fd = -1
    result = False
    try:
        root_fd = _open_owned(root)
        quarantine_entries = _quarantine_entries_at(root, root_fd, name)
        receipt_entries = _quarantine_receipt_entries_at(root, root_fd, name)
        if len(quarantine_entries) > _MAX_QUARANTINES_PER_ROLE:
            return False
        binding_digests = {
            match.group(3)
            for candidate in quarantine_entries
            if (match := _QUARANTINE_NAME.fullmatch(candidate)) is not None
        }
        receipt_kinds: dict[str, str] = {}
        for digest, kind, _info in receipt_entries.values():
            if digest in receipt_kinds:
                return False
            receipt_kinds[digest] = kind
            binding_digests.add(digest)
        if len(binding_digests) > _MAX_QUARANTINES_PER_ROLE:
            return False

        opened = _open_hashed_owned_file(root, root_fd, name)
        if opened is not None:
            opened_fd, opened_binding, opened_info = opened
        else:
            opened_binding = None
            opened_info = None
        nonzero_quarantines = tuple(
            candidate for candidate, info in quarantine_entries.items() if info.st_size
        )
        if opened_binding is not None and nonzero_quarantines:
            return False

        receipt_name: str
        receipt_info: os.stat_result
        receipt_raw: bytes
        if opened_binding is not None:
            authority = opened_binding if expected is None else expected
            if opened_binding != authority:
                return False
            authority_digest = _quarantine_binding_digest(authority)
            if (
                authority_digest not in binding_digests
                and len(binding_digests) >= _MAX_QUARANTINES_PER_ROLE
            ):
                return False
            quarantine_name = _quarantine_file_name(authority)
            receipt = _QuarantineReceipt(
                original_name=name,
                quarantine_name=quarantine_name,
                binding=authority,
            )
            if not _write_quarantine_receipt_at(
                root,
                root_fd,
                receipt,
                quota_admit=quota_admit,
            ):
                return False
            receipt_name = _quarantine_receipt_name(authority)
            pinned = _pin_quarantine_receipt_at(
                root,
                root_fd,
                receipt_name,
                expected=receipt,
            )
            if pinned is None:
                return False
            receipt_fd, receipt_info, receipt_raw = pinned
        else:
            if not nonzero_quarantines:
                if any(kind == "temp" for _digest, kind, _info in receipt_entries.values()):
                    return False
                completed_bindings: set[_Binding] = set()
                for quarantine_name, quarantine_info in quarantine_entries.items():
                    match = _QUARANTINE_NAME.fullmatch(quarantine_name)
                    if match is None:
                        return False
                    paired_name = (
                        f".quarantine-receipt.{match.group(1)}.{match.group(2)}."
                        f"{match.group(3)}.json"
                    )
                    paired_entry = receipt_entries.get(paired_name)
                    if paired_entry is None or paired_entry[1] != "final":
                        return False
                    loaded = _read_quarantine_receipt_at(root, root_fd, paired_name)
                    if (
                        loaded is None
                        or loaded[0].original_name != name
                        or loaded[0].quarantine_name != quarantine_name
                        or not _is_quarantine_tombstone(
                            quarantine_info,
                            loaded[0].binding,
                        )
                    ):
                        return False
                    completed_bindings.add(loaded[0].binding)
                for candidate, (_digest, kind, _info) in receipt_entries.items():
                    if kind != "final":
                        return False
                    loaded = _read_quarantine_receipt_at(root, root_fd, candidate)
                    if loaded is None:
                        return False
                    completed = quarantine_entries.get(loaded[0].quarantine_name)
                    if completed is None or not _is_quarantine_tombstone(
                        completed,
                        loaded[0].binding,
                    ):
                        return False
                if expected is not None and expected not in completed_bindings:
                    return False
                os.fsync(root_fd)
                return True
            if len(nonzero_quarantines) != 1:
                return False
            quarantine_name = nonzero_quarantines[0]
            match = _QUARANTINE_NAME.fullmatch(quarantine_name)
            if match is None:
                return False
            receipt_name = (
                f".quarantine-receipt.{match.group(1)}.{match.group(2)}.{match.group(3)}.json"
            )
            receipt_entry = receipt_entries.get(receipt_name)
            if receipt_entry is None or receipt_entry[1] != "final":
                return False
            loaded = _read_quarantine_receipt_at(root, root_fd, receipt_name)
            if loaded is None:
                return False
            receipt = loaded[0]
            if (
                receipt.original_name != name
                or receipt.quarantine_name != quarantine_name
                or (expected is not None and receipt.binding != expected)
            ):
                return False
            authority = receipt.binding
            pinned = _pin_quarantine_receipt_at(
                root,
                root_fd,
                receipt_name,
                expected=receipt,
            )
            if pinned is None:
                return False
            receipt_fd, receipt_info, receipt_raw = pinned

        if opened_binding is None:
            quarantined = _open_hashed_owned_file(root, root_fd, quarantine_name)
            if quarantined is None:
                return False
            quarantine_fd, quarantine_binding, quarantine_info = quarantined
            try:
                moved_as_original = replace(quarantine_binding, name=name)
                if moved_as_original != authority:
                    return False
                quota_admit(extra_bytes=0, extra_files=0)
                if not _verify_pinned_quarantine_receipt_at(
                    root_fd,
                    receipt_name,
                    receipt_fd,
                    receipt_info,
                    receipt_raw,
                    receipt,
                ):
                    return False
                current = os.stat(
                    quarantine_name,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
                opened_current = os.fstat(quarantine_fd)
                if _info_binding_identity(current) != _info_binding_identity(opened_current):
                    return False
                if not _name_absent_at(root_fd, name):
                    return False
                if not _zero_quarantine_descriptor(
                    root_fd,
                    quarantine_fd,
                    quarantine_name,
                    quarantine_info,
                ):
                    return False
            finally:
                os.close(quarantine_fd)
            return True

        if quarantine_name in quarantine_entries:
            return False
        authority_digest = _quarantine_binding_digest(authority)
        if (
            authority_digest not in binding_digests
            and len(binding_digests) >= _MAX_QUARANTINES_PER_ROLE
        ):
            return False

        quota_admit(extra_bytes=0, extra_files=0)
        quarantine_entries = _quarantine_entries_at(root, root_fd, name)
        if any(info.st_size for info in quarantine_entries.values()):
            return False
        if (
            quarantine_name not in quarantine_entries
            and len(quarantine_entries) >= _MAX_QUARANTINES_PER_ROLE
        ):
            return False
        current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        opened_current = os.fstat(opened_fd)
        if (
            opened_info is None
            or _info_binding_identity(opened_current) != _info_binding_identity(opened_info)
            or _info_binding_identity(current) != _info_binding_identity(opened_current)
            or not _verify_pinned_quarantine_receipt_at(
                root_fd,
                receipt_name,
                receipt_fd,
                receipt_info,
                receipt_raw,
                receipt,
            )
        ):
            return False
        if not _rename_noreplace(root_fd, name, quarantine_name):
            return False
        os.fsync(root_fd)
        moved = os.stat(quarantine_name, dir_fd=root_fd, follow_symlinks=False)
        after = os.fstat(opened_fd)
        if _info_binding_identity(after) != _info_binding_identity(
            opened_info
        ) or _info_binding_identity(moved) != _info_binding_identity(after):
            return False
        quota_admit(extra_bytes=0, extra_files=0)
        if not _verify_pinned_quarantine_receipt_at(
            root_fd,
            receipt_name,
            receipt_fd,
            receipt_info,
            receipt_raw,
            receipt,
        ):
            return False
        moved = os.stat(
            quarantine_name,
            dir_fd=root_fd,
            follow_symlinks=False,
        )
        after = os.fstat(opened_fd)
        if (
            _info_binding_identity(after) != _info_binding_identity(opened_info)
            or _info_binding_identity(moved) != _info_binding_identity(after)
            or not _name_absent_at(root_fd, name)
        ):
            return False
        if not _zero_quarantine_descriptor(
            root_fd,
            opened_fd,
            quarantine_name,
            opened_info,
        ):
            return False
        result = True
    except (OSError, StorageFailure, _ServiceError):
        result = False
    finally:
        if receipt_fd >= 0:
            try:
                os.close(receipt_fd)
            except OSError:
                result = False
        if opened_fd >= 0:
            try:
                os.close(opened_fd)
            except OSError:
                result = False
        if root_fd >= 0:
            try:
                os.close(root_fd)
            except OSError:
                result = False
    return result


def _quarantine_recovery_present(root: SafeRoot, name: str) -> bool:
    parts = _owned_file_parts(name)
    if parts is None:
        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    role, token = parts
    quarantine_prefix = f".quarantine.{role}.{token}."
    receipt_prefix = f".quarantine-receipt.{role}.{token}."
    root_fd = _open_owned(root)
    try:
        try:
            names = os.listdir(root_fd)
        except OSError:
            raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
        for candidate in names:
            if not candidate.startswith((quarantine_prefix, receipt_prefix)):
                continue
            try:
                info = os.stat(candidate, dir_fd=root_fd, follow_symlinks=False)
            except OSError:
                raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
            if _QUARANTINE_RECEIPT_TEMP_NAME.fullmatch(candidate) is not None:
                return True
            if _QUARANTINE_NAME.fullmatch(candidate) is not None and info.st_size:
                return True
        return False
    finally:
        _close_owned(root_fd)


def _source_identity(value: os.stat_result) -> _SourceIdentity:
    return _SourceIdentity(
        dev=value.st_dev,
        ino=value.st_ino,
        mode=value.st_mode,
        uid=value.st_uid,
        nlink=value.st_nlink,
        size=value.st_size,
        mtime_ns=str(value.st_mtime_ns),
        ctime_ns=str(value.st_ctime_ns),
    )


def _ancestor_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _open_owned(root: SafeRoot) -> int:
    try:
        return root.open()
    except (OSError, StorageFailure):
        raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None


def _close_owned(
    descriptor: int,
    *,
    code: ProjectServicePortErrorCode = ProjectServicePortErrorCode.STORE_FAILURE,
) -> None:
    try:
        os.close(descriptor)
    except OSError:
        raise _ServiceError(code) from None


def _call_cad_from_pinned_root(root: SafeRoot, name: str, action):
    if (
        sys.platform != "darwin"
        or type(name) is not str
        or not (_WORK_NAME.fullmatch(name) or _NORMALIZED_NAME.fullmatch(name))
    ):
        raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root_fd = _open_owned(root)
    previous_fd = -1
    previous_identity = None
    entered = False
    primary = None
    result = None
    restore_failed = False
    try:
        previous_fd = os.open(".", flags)
        previous = os.fstat(previous_fd)
        previous_identity = (previous.st_dev, previous.st_ino)
        _darwin_thread_fchdir(root_fd)
        entered = True
        if not _current_directory_matches(root):
            raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE)
        try:
            result = action(Path(name))
        except BaseException as error:
            primary = error
        if primary is None and not _current_directory_matches(root):
            primary = _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE)
    except BaseException as error:
        primary = error
    finally:
        if entered:
            try:
                _darwin_thread_fchdir(-1)
            except (OSError, StorageFailure):
                restore_failed = True
        current_fd = -1
        try:
            current_fd = os.open(".", flags)
            current = os.fstat(current_fd)
            if (current.st_dev, current.st_ino) != previous_identity:
                restore_failed = True
        except OSError:
            restore_failed = True
        finally:
            if current_fd >= 0:
                try:
                    os.close(current_fd)
                except OSError:
                    restore_failed = True
        if previous_fd >= 0:
            try:
                os.close(previous_fd)
            except OSError:
                restore_failed = True
        try:
            os.close(root_fd)
        except OSError:
            restore_failed = True
    if restore_failed:
        raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED) from primary
    if primary is not None:
        raise primary.with_traceback(primary.__traceback__)
    return result


class DurableProjectService:
    """Durable implementation of the neutral ``ProjectServicePort`` seam."""

    __slots__ = (
        "_bootstrap",
        "_cad_port_factory",
        "_creator_pid",
        "_data_root",
        "_lease_manager",
        "_normalized",
        "_requests",
        "_revision_store",
        "_staging",
        "_work",
    )

    def __init__(
        self,
        *,
        bootstrap_root: Path,
        data_root: Path,
        revision_store: object,
        lease_manager: object,
        cad_port_factory: object,
        expected_bootstrap_identity: tuple[int, int] | None = None,
        expected_data_identity: tuple[int, int] | None = None,
    ) -> None:
        expected_identities = (expected_data_identity, expected_bootstrap_identity)
        if (
            type(bootstrap_root) is not type(Path("/"))
            or type(data_root) is not type(Path("/"))
            or not bootstrap_root.is_absolute()
            or not data_root.is_absolute()
            or bootstrap_root.parent != data_root
            or not callable(cad_port_factory)
            or any(
                identity is not None
                and (
                    type(identity) is not tuple
                    or len(identity) != 2
                    or not all(type(item) is int and item >= 0 for item in identity)
                )
                for identity in expected_identities
            )
        ):
            raise TypeError("invalid durable project service composition")
        self._data_root = SafeRoot(data_root)
        self._bootstrap = SafeRoot(bootstrap_root)
        if (
            self._bootstrap.identity[0] != self._data_root.identity[0]
            or (
                expected_data_identity is not None
                and self._data_root.identity != expected_data_identity
            )
            or (
                expected_bootstrap_identity is not None
                and self._bootstrap.identity != expected_bootstrap_identity
            )
        ):
            raise TypeError("invalid durable project service composition")
        self._create_fixed_directories()
        self._requests = SafeRoot(bootstrap_root / "requests")
        self._staging = SafeRoot(bootstrap_root / "staging")
        self._work = SafeRoot(bootstrap_root / "work")
        self._normalized = SafeRoot(bootstrap_root / "normalized")
        self._revision_store = revision_store
        self._lease_manager = lease_manager
        self._cad_port_factory = cad_port_factory
        self._creator_pid = os.getpid()

    def _create_fixed_directories(self) -> None:
        root_fd = _open_owned(self._bootstrap)
        try:
            for name in _DIRECTORIES:
                try:
                    os.mkdir(name, 0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
                child_fd, _ = self._bootstrap.open_directory_at(root_fd, name)
                os.close(child_fd)
            os.fsync(root_fd)
        except (OSError, StorageFailure):
            raise TypeError("invalid durable project service composition") from None
        finally:
            with contextlib.suppress(OSError):
                os.close(root_fd)

    def _ensure_live(self) -> None:
        if os.getpid() != self._creator_pid:
            raise _ServiceError(ProjectServicePortErrorCode.INTERNAL_ERROR)
        data_fd = -1
        bootstrap_fd = -1
        try:
            data_fd = self._data_root.open()
            bootstrap_fd = self._bootstrap.open()
            current = os.stat("bootstrap", dir_fd=data_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != self._bootstrap.identity
            ):
                raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE)
        except (OSError, StorageFailure):
            raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
        finally:
            if bootstrap_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(bootstrap_fd)
            if data_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(data_fd)

    @staticmethod
    def _failure(code: ProjectServicePortErrorCode) -> ProjectServicePortFailure:
        return ProjectServicePortFailure(code=code)

    def _acquire(self, resource_id: str, *, wait: bool = True):
        deadline = time.monotonic() + _LEASE_WAIT_SECONDS
        while True:
            try:
                return self._lease_manager.acquire(resource_id)
            except LeaseError as error:
                if error.code is LeaseErrorCode.CONTENDED:
                    if not wait or time.monotonic() >= deadline:
                        raise _ServiceError(ProjectServicePortErrorCode.LEASE_UNAVAILABLE) from None
                    time.sleep(_LEASE_RETRY_SECONDS)
                    continue
                if error.code is LeaseErrorCode.WRONG_PROCESS:
                    raise _ServiceError(ProjectServicePortErrorCode.INTERNAL_ERROR) from None
                raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
            except Exception:
                raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None

    @staticmethod
    def _release(lease) -> None:
        try:
            lease.release(owner_token=lease.owner_token)
        except Exception:
            raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED) from None

    def _acquire_slot(self):
        saw_contended = False
        for index in range(_MAX_ACTIVE):
            try:
                return self._acquire(f"{_SLOT_RESOURCE_PREFIX}{index}", wait=False)
            except _ServiceError as error:
                if error.code is ProjectServicePortErrorCode.LEASE_UNAVAILABLE:
                    saw_contended = True
                    continue
                raise
        if saw_contended:
            raise _ServiceError(ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
        raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE)

    def _read_key(self, requests_fd: int, *, records_exist: bool) -> tuple[bytes, str]:
        try:
            raw, _ = self._requests.read_file_at(
                requests_fd,
                _KEY_FILE,
                maximum=1024,
            )
        except StorageFailure:
            try:
                os.stat(_KEY_FILE, dir_fd=requests_fd, follow_symlinks=False)
            except FileNotFoundError:
                if records_exist:
                    raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED) from None
                return self._create_key(requests_fd)
            except OSError:
                pass
            raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
        mapping = _exact_mapping(_strict_json(raw, maximum=1024), _KEY_KEYS)
        _require_schema_version(mapping["schema_version"])
        key_hex = _exact_string(mapping["key_hex"], _DIGEST)
        key_id = _exact_string(mapping["key_id"], _DIGEST)
        key = bytes.fromhex(key_hex)
        if not hmac.compare_digest(key_id, _digest(_KEY_ID_DOMAIN, key)):
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        return key, key_id

    def _create_key(self, requests_fd: int) -> tuple[bytes, str]:
        key = secrets.token_bytes(32)
        key_id = _digest(_KEY_ID_DOMAIN, key)
        raw = _canonical(
            {
                "schema_version": _SCHEMA_VERSION,
                "key_hex": key.hex(),
                "key_id": key_id,
            }
        )
        self._quota_admit(extra_bytes=len(raw), extra_files=1)
        try:
            self._requests.atomic_write(
                requests_fd,
                _KEY_FILE,
                raw,
                token=secrets.token_hex(16),
            )
            checked, _ = self._requests.read_file_at(
                requests_fd,
                _KEY_FILE,
                maximum=1024,
            )
        except StorageFailure:
            raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
        if checked != raw:
            raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
        return key, key_id

    @staticmethod
    def _allowed_entry(relative: tuple[str, ...], name: str, is_directory: bool) -> bool:
        if not relative:
            if is_directory:
                return name in _DIRECTORIES
            return bool(_LEGACY_STAGE_NAME.fullmatch(name) or _LEGACY_CLEANUP_NAME.fullmatch(name))
        if len(relative) != 1 or is_directory:
            return False
        parent = relative[0]
        if parent == "requests":
            return bool(
                name == _KEY_FILE or _REQUEST_NAME.fullmatch(name) or _TEMP_NAME.fullmatch(name)
            )
        pattern = {
            "staging": _STAGE_NAME,
            "work": _WORK_NAME,
            "normalized": _NORMALIZED_NAME,
        }.get(parent)
        role = {"staging": "stage", "work": "work", "normalized": "normalized"}.get(parent)
        if pattern is None or role is None:
            return False
        if pattern.fullmatch(name):
            return True
        for recovery_pattern in (
            _QUARANTINE_NAME,
            _QUARANTINE_RECEIPT_NAME,
            _QUARANTINE_RECEIPT_TEMP_NAME,
        ):
            match = recovery_pattern.fullmatch(name)
            if match is not None:
                return match.group(1) == role
        return False

    def _scan_store_snapshot(self) -> tuple[int, int, int, int]:
        root_fd = _open_owned(self._bootstrap)
        total = 0
        records = 0
        files = 0
        saw_legacy = False
        saw_record_temp = False
        parsed_records: list[_Record] = []
        owned_bytes: dict[str, int] = {}
        owned_infos: dict[tuple[str, str], os.stat_result] = {}
        quarantine_bytes: dict[str, int] = {}
        quarantine_infos: dict[tuple[str, str, str], os.stat_result] = {}
        receipt_bytes: dict[str, int] = {}
        receipt_authorities: dict[tuple[str, str, str], _QuarantineReceipt] = {}
        receipt_temp_tokens: set[str] = set()
        recovery_files: dict[str, int] = {}
        quarantine_roles: dict[tuple[str, str], int] = {}
        binding_roles: dict[tuple[str, str], set[str]] = {}
        receipt_kinds: dict[tuple[str, str, str], str] = {}

        def scan(directory_fd: int, relative: tuple[str, ...]) -> None:
            nonlocal files, records, saw_legacy, saw_record_temp, total
            try:
                entries = os.scandir(directory_fd)
            except OSError:
                raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
            try:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
                    is_directory = stat.S_ISDIR(info.st_mode)
                    if not self._allowed_entry(relative, entry.name, is_directory):
                        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
                    if (
                        info.st_uid != self._bootstrap.uid
                        or info.st_dev != self._bootstrap.identity[0]
                    ):
                        raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE)
                    files += 1
                    if files > _MAX_STORE_FILES:
                        raise _ServiceError(ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
                    if is_directory:
                        if stat.S_IMODE(info.st_mode) != 0o700:
                            raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE)
                        try:
                            child_fd = os.open(
                                entry.name,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=directory_fd,
                            )
                        except OSError:
                            raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
                        try:
                            scan(child_fd, relative + (entry.name,))
                        finally:
                            _close_owned(child_fd)
                        continue
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_nlink != 1
                        or info.st_size < 0
                    ):
                        raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE)
                    total += info.st_size
                    if total > _MAX_STORE_BYTES:
                        raise _ServiceError(ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
                    if not relative and (
                        _LEGACY_STAGE_NAME.fullmatch(entry.name)
                        or _LEGACY_CLEANUP_NAME.fullmatch(entry.name)
                    ):
                        saw_legacy = True
                        continue
                    if relative == ("requests",) and _TEMP_NAME.fullmatch(entry.name):
                        saw_record_temp = True
                    if relative == ("requests",) and _REQUEST_NAME.fullmatch(entry.name):
                        records += 1
                        if records > _MAX_RECORDS:
                            raise _ServiceError(ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
                        try:
                            raw, _ = self._requests.read_file_at(
                                directory_fd,
                                entry.name,
                                maximum=_MAX_RECORD_BYTES,
                            )
                        except StorageFailure:
                            raise _ServiceError(
                                ProjectServicePortErrorCode.INTEGRITY_FAILURE
                            ) from None
                        parsed_records.append(_record_from_bytes(raw, expected_name=entry.name))
                    if relative in {("staging",), ("work",), ("normalized",)}:
                        owned_match = {
                            ("staging",): _STAGE_NAME,
                            ("work",): _WORK_NAME,
                            ("normalized",): _NORMALIZED_NAME,
                        }[relative].fullmatch(entry.name)
                        if owned_match is not None:
                            token = owned_match.group(1)
                            role = {
                                ("staging",): "stage",
                                ("work",): "work",
                                ("normalized",): "normalized",
                            }[relative]
                            owned_bytes[token] = owned_bytes.get(token, 0) + info.st_size
                            owned_infos[token, role] = info
                            continue
                        recovery_match = _QUARANTINE_NAME.fullmatch(entry.name)
                        receipt_match = _QUARANTINE_RECEIPT_NAME.fullmatch(entry.name)
                        receipt_temp_match = _QUARANTINE_RECEIPT_TEMP_NAME.fullmatch(entry.name)
                        if (
                            recovery_match is None
                            and receipt_match is None
                            and receipt_temp_match is None
                        ):
                            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
                        if (
                            receipt_match is not None or receipt_temp_match is not None
                        ) and info.st_size > 8192:
                            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
                        match = recovery_match or receipt_match or receipt_temp_match
                        if match is None:
                            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
                        role, token, binding_digest = match.groups()
                        recovery_files[token] = recovery_files.get(token, 0) + 1
                        if recovery_files[token] > (_RECOVERY_ROLES * _RECOVERY_FILES_PER_ROLE):
                            raise _ServiceError(ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
                        role_key = token, role
                        role_bindings = binding_roles.setdefault(role_key, set())
                        role_bindings.add(binding_digest)
                        if len(role_bindings) > _MAX_QUARANTINES_PER_ROLE:
                            raise _ServiceError(ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
                        if recovery_match is not None:
                            quarantine_roles[role_key] = quarantine_roles.get(role_key, 0) + 1
                            if quarantine_roles[role_key] > _MAX_QUARANTINES_PER_ROLE:
                                raise _ServiceError(ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
                            quarantine_bytes[token] = quarantine_bytes.get(token, 0) + info.st_size
                            quarantine_infos[token, role, binding_digest] = info
                        else:
                            receipt_key = token, role, binding_digest
                            receipt_kind = "final" if receipt_match is not None else "temp"
                            if receipt_key in receipt_kinds:
                                raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
                            receipt_kinds[receipt_key] = receipt_kind
                            receipt_bytes[token] = receipt_bytes.get(token, 0) + info.st_size
                            if receipt_temp_match is not None:
                                receipt_temp_tokens.add(token)
                            else:
                                receipt_root = {
                                    ("staging",): self._staging,
                                    ("work",): self._work,
                                    ("normalized",): self._normalized,
                                }[relative]
                                try:
                                    raw, _ = receipt_root.read_file_at(
                                        directory_fd,
                                        entry.name,
                                        maximum=8192,
                                    )
                                    parsed_receipt = _quarantine_receipt_from_bytes(
                                        raw,
                                        expected_name=entry.name,
                                    )
                                except (StorageFailure, _ServiceError):
                                    raise _ServiceError(
                                        ProjectServicePortErrorCode.RECOVERY_REQUIRED
                                    ) from None
                                receipt_authorities[receipt_key] = parsed_receipt
            except OSError:
                raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
            finally:
                try:
                    entries.close()
                except OSError:
                    raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None

        try:
            scan(root_fd, ())
        finally:
            _close_owned(root_fd)
        if saw_legacy or saw_record_temp:
            raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
        record_tokens: dict[str, _Record] = {}
        reserved = 0
        owned_total = 0
        for record in parsed_records:
            token = record.intent_hmac[:32]
            if token in record_tokens:
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
            record_tokens[token] = record
            if record.phase in {"PUBLISHED", "REJECTED"} and (
                quarantine_bytes.get(token, 0) or token in receipt_temp_tokens
            ):
                raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
            if token in owned_bytes and (
                record.kind is ProjectKind.EMPTY or record.phase in {"PUBLISHED", "REJECTED"}
            ):
                raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
            observed = owned_bytes.get(token, 0) + quarantine_bytes.get(token, 0)
            if observed > record.reservation_bytes:
                raise _ServiceError(ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
            owned_total += observed
            reserved += record.reservation_bytes
            if reserved > _MAX_STORE_BYTES:
                raise _ServiceError(ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
        recovery_tokens = set(quarantine_bytes) | set(receipt_bytes)
        if (set(owned_bytes) | recovery_tokens) - set(record_tokens):
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        for recovery_key in quarantine_infos:
            if (
                receipt_kinds.get(recovery_key) != "final"
                or recovery_key not in receipt_authorities
            ):
                raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
        for (token, role, binding_digest), kind in receipt_kinds.items():
            quarantine_info = quarantine_infos.get((token, role, binding_digest))
            owned_info = owned_infos.get((token, role))
            if kind == "temp":
                if quarantine_info is not None or owned_info is None:
                    raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
                continue
            receipt = receipt_authorities.get((token, role, binding_digest))
            if receipt is None:
                raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
            if quarantine_info is None:
                if owned_info is None or _info_binding_identity(owned_info) != _binding_identity(
                    receipt.binding
                ):
                    raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
                continue
            if quarantine_info.st_size:
                if owned_info is not None or _info_binding_identity(
                    quarantine_info
                ) != _binding_identity(receipt.binding):
                    raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
                quarantine_root = {
                    "stage": self._staging,
                    "work": self._work,
                    "normalized": self._normalized,
                }[role]
                try:
                    quarantine_authority = _hash_quarantine_authority(
                        quarantine_root,
                        receipt.quarantine_name,
                        original_name=receipt.original_name,
                    )
                except StorageFailure:
                    raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED) from None
                if quarantine_authority != receipt.binding:
                    raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
            elif not _is_quarantine_tombstone(quarantine_info, receipt.binding):
                raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
        accounted = total - owned_total + reserved
        if accounted > _MAX_STORE_BYTES:
            raise _ServiceError(ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
        return total, records, accounted, files

    def _scan_store(self) -> tuple[int, int, int]:
        total, records, accounted, _files = self._scan_store_snapshot()
        return total, records, accounted

    def _quota_admit(self, *, extra_bytes: int, extra_files: int) -> tuple[int, int]:
        total, records, accounted, files = self._scan_store_snapshot()
        if (
            type(extra_bytes) is not int
            or extra_bytes < 0
            or accounted + extra_bytes > _MAX_STORE_BYTES
            or type(extra_files) is not int
            or extra_files < 0
            or files + extra_files > _MAX_STORE_FILES
        ):
            raise _ServiceError(ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
        return total, records

    def _load_record(self, create_key: str) -> _Record | None:
        requests_fd = _open_owned(self._requests)
        name = _request_name(create_key)
        try:
            try:
                raw, _ = self._requests.read_file_at(
                    requests_fd,
                    name,
                    maximum=_MAX_RECORD_BYTES,
                )
            except StorageFailure:
                try:
                    os.stat(name, dir_fd=requests_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return None
                except OSError:
                    pass
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE) from None
            return _record_from_bytes(raw, expected_name=name)
        finally:
            _close_owned(requests_fd)

    def _write_record(self, value: _Record, *, creating: bool = False) -> None:
        catalog = self._acquire(_CATALOG_RESOURCE)
        try:
            self._write_record_under_catalog(value, creating=creating)
        finally:
            self._release(catalog)

    def _write_record_under_catalog(
        self,
        value: _Record,
        *,
        creating: bool = False,
    ) -> None:
        _validate_record_state(value)
        raw = _record_bytes(value)
        requests_fd = _open_owned(self._requests)
        try:
            name = _request_name(value.create_key)
            exists = False
            try:
                os.stat(name, dir_fd=requests_fd, follow_symlinks=False)
                exists = True
            except FileNotFoundError:
                pass
            except OSError:
                raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
            if creating != (not exists):
                raise _ServiceError(ProjectServicePortErrorCode.CONFLICT)
            # ``accounted`` already includes the old record.  Adding the full
            # new raw size therefore enforces the physical old+temp peak; the
            # post-replace logical size is necessarily no larger than that peak.
            _, records = self._quota_admit(extra_bytes=len(raw), extra_files=1)
            if creating and records >= _MAX_RECORDS:
                raise _ServiceError(ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
            try:
                self._requests.atomic_write(
                    requests_fd,
                    name,
                    raw,
                    token=secrets.token_hex(16),
                )
                checked, _ = self._requests.read_file_at(
                    requests_fd,
                    name,
                    maximum=_MAX_RECORD_BYTES,
                )
            except StorageFailure:
                raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
            if checked != raw:
                raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
        finally:
            _close_owned(requests_fd)

    def _admit_record(
        self,
        *,
        create_key: str,
        kind: ProjectKind,
        source_path: str | None,
    ) -> tuple[_Record, _OpenedSource | None]:
        catalog = self._acquire(_CATALOG_RESOURCE)
        opened_source = None
        try:
            _, record_count = self._quota_admit(extra_bytes=0, extra_files=0)
            requests_fd = _open_owned(self._requests)
            try:
                key, key_id = self._read_key(requests_fd, records_exist=record_count > 0)
                name = _request_name(create_key)
                try:
                    raw, _ = self._requests.read_file_at(
                        requests_fd,
                        name,
                        maximum=_MAX_RECORD_BYTES,
                    )
                except StorageFailure:
                    try:
                        os.stat(name, dir_fd=requests_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        raw = None
                    except OSError:
                        raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
                    else:
                        raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE) from None
                intent = _intent_digest(
                    key,
                    create_key=create_key,
                    kind=kind,
                    source_path=source_path,
                )
                if raw is not None:
                    record = _record_from_bytes(raw, expected_name=name)
                    if record.key_id != key_id:
                        raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
                    if not hmac.compare_digest(record.intent_hmac, intent):
                        raise _ServiceError(ProjectServicePortErrorCode.CONFLICT)
                    if record.kind is not kind:
                        raise _ServiceError(ProjectServicePortErrorCode.CONFLICT)
                    if record.kind is ProjectKind.IMPORT_FCSTD and record.phase == "RESERVED":
                        opened_source = self._open_source(source_path)
                        if _source_identity(opened_source.before) != record.source_identity:
                            raise _ServiceError(ProjectServicePortErrorCode.CONFLICT)
                    return record, opened_source
                if record_count >= _MAX_RECORDS:
                    raise _ServiceError(ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
                source_size = None
                source_identity = None
                if kind is ProjectKind.IMPORT_FCSTD:
                    opened_source = self._open_source(source_path)
                    source_size = opened_source.before.st_size
                    source_identity = _source_identity(opened_source.before)
                record = _Record(
                    create_key=create_key,
                    key_id=key_id,
                    intent_hmac=intent,
                    kind=kind,
                    project_id=f"project_{secrets.token_hex(16)}",
                    phase="RESERVED",
                    source_size=source_size,
                    source_identity=source_identity,
                    reservation_bytes=(
                        0 if source_size is None else source_size + (2 * _MAX_SOURCE_BYTES)
                    ),
                    stage=None,
                    work=None,
                    validation_started=False,
                    work_validated=False,
                    normalized=None,
                    outcome=None,
                    failure_code=None,
                    generation_zero=None,
                )
                if _PROJECT_ID.fullmatch(record.project_id) is None:
                    raise _ServiceError(ProjectServicePortErrorCode.INTERNAL_ERROR)
                raw = _record_bytes(record)
                self._quota_admit(
                    extra_bytes=len(raw) + record.reservation_bytes,
                    extra_files=1,
                )
                try:
                    self._requests.atomic_write(
                        requests_fd,
                        name,
                        raw,
                        token=secrets.token_hex(16),
                    )
                    checked, _ = self._requests.read_file_at(
                        requests_fd,
                        name,
                        maximum=_MAX_RECORD_BYTES,
                    )
                except StorageFailure:
                    raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
                if checked != raw:
                    raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
                return record, opened_source
            finally:
                _close_owned(requests_fd)
        except BaseException:
            if opened_source is not None:
                with contextlib.suppress(_ServiceError):
                    opened_source.close()
                opened_source = None
            raise
        finally:
            try:
                self._release(catalog)
            except _ServiceError:
                if opened_source is not None:
                    with contextlib.suppress(_ServiceError):
                        opened_source.close()
                raise

    def _open_source(self, source_path: str | None) -> _OpenedSource:
        if type(source_path) is not str or not source_path.startswith("/"):
            raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
        try:
            encoded = source_path.encode("utf-8")
        except UnicodeEncodeError:
            raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT) from None
        if not encoded or len(encoded) > 4096 or b"\0" in encoded:
            raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
        parts = source_path.split("/")[1:]
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        ancestor_fds: list[int] = []
        ancestor_identities: list[tuple[int, ...]] = []
        source_fd = -1
        succeeded = False
        try:
            root_fd = os.open("/", directory_flags)
            ancestor_fds.append(root_fd)
            root_info = os.fstat(root_fd)
            ancestor_identities.append(_ancestor_identity(root_info))
            for part in parts[:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=ancestor_fds[-1])
                ancestor_fds.append(next_fd)
                current = os.fstat(next_fd)
                ancestor_identities.append(_ancestor_identity(current))
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or (current.st_dev, current.st_ino) == self._data_root.identity
                ):
                    raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
            parent_fd = ancestor_fds[-1]
            before = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            if not self._safe_source(before):
                raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
            source_fd = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            opened = os.fstat(source_fd)
            if not self._safe_source(opened) or _identity(opened) != _identity(before):
                raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
            succeeded = True
            return _OpenedSource(
                ancestor_fds=tuple(ancestor_fds),
                ancestor_identities=tuple(ancestor_identities),
                ancestor_names=tuple(parts[:-1]),
                final_name=parts[-1],
                fd=source_fd,
                before=opened,
            )
        except _ServiceError:
            raise
        except (OSError, ValueError):
            raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT) from None
        finally:
            if source_fd >= 0 and not succeeded:
                with contextlib.suppress(OSError):
                    os.close(source_fd)
            if not succeeded:
                for ancestor_fd in reversed(ancestor_fds):
                    with contextlib.suppress(OSError):
                        os.close(ancestor_fd)

    @staticmethod
    def _safe_source(value: os.stat_result) -> bool:
        return (
            stat.S_ISREG(value.st_mode)
            and value.st_uid == os.geteuid()
            and value.st_nlink == 1
            and 0 < value.st_size <= _MAX_SOURCE_BYTES
        )

    def _copy_source_to_stage(
        self,
        opened: _OpenedSource,
        record: _Record,
    ) -> _Record:
        if _source_identity(opened.before) != record.source_identity:
            raise _ServiceError(ProjectServicePortErrorCode.CONFLICT)
        stage_name = f".stage.{record.intent_hmac[:32]}.FCStd"
        if _STAGE_NAME.fullmatch(stage_name) is None:
            raise _ServiceError(ProjectServicePortErrorCode.INTERNAL_ERROR)
        if _quarantine_recovery_present(self._staging, stage_name) and not (
            self._remove_record_owned_partial(self._staging, stage_name)
        ):
            raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
        existing_stage = self._maybe_current_binding(self._staging, stage_name)
        if existing_stage is not None:
            self._verify_source_chain(opened)
            digest = hashlib.sha256()
            remaining = opened.before.st_size
            while remaining:
                try:
                    chunk = os.read(opened.fd, min(_COPY_CHUNK_BYTES, remaining))
                except OSError:
                    raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT) from None
                if not chunk:
                    raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
                digest.update(chunk)
                remaining -= len(chunk)
            self._verify_source_chain(opened)
            if (
                existing_stage.size != opened.before.st_size
                or existing_stage.sha256 != digest.hexdigest()
            ):
                if not self._remove_record_owned_partial(self._staging, stage_name):
                    raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
                try:
                    os.lseek(opened.fd, 0, os.SEEK_SET)
                except OSError:
                    raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT) from None
            else:
                updated = replace(record, phase="STAGED", stage=existing_stage)
                self._write_record(updated)
                return updated
        stage_fd = _open_owned(self._staging)
        target_fd = -1
        created = False
        digest = hashlib.sha256()
        try:
            self._verify_source_chain(opened)
            catalog = self._acquire(_CATALOG_RESOURCE)
            try:
                self._quota_admit(extra_bytes=0, extra_files=1)
                target_fd = os.open(
                    stage_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=stage_fd,
                )
                created = True
            finally:
                self._release(catalog)
            remaining = opened.before.st_size
            while remaining:
                chunk = os.read(opened.fd, min(_COPY_CHUNK_BYTES, remaining))
                if not chunk:
                    raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(target_fd, view)
                    if written <= 0:
                        raise OSError
                    view = view[written:]
                remaining -= len(chunk)
            after_source = os.fstat(opened.fd)
            if _identity(after_source) != _identity(opened.before):
                raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
            self._verify_source_chain(opened)
            os.fsync(target_fd)
            target_info = os.fstat(target_fd)
            if (
                not self._staging.regular_file(target_info, maximum=_MAX_SOURCE_BYTES)
                or target_info.st_size != opened.before.st_size
            ):
                raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE)
            os.close(target_fd)
            target_fd = -1
            os.fsync(stage_fd)
            self._ensure_live()
            staged = _binding(target_info, name=stage_name, sha256=digest.hexdigest())
            updated = replace(record, phase="STAGED", stage=staged)
            self._write_record(updated)
            return updated
        except _ServiceError:
            raise
        except OSError:
            raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
        finally:
            if target_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(target_fd)
            with contextlib.suppress(OSError):
                os.close(stage_fd)
            if created and "updated" not in locals():
                with contextlib.suppress(_ServiceError):
                    self._remove_record_owned_partial(self._staging, stage_name)

    def _verify_source_chain(self, opened: _OpenedSource) -> None:
        if not (
            len(opened.ancestor_fds) == len(opened.ancestor_identities)
            and len(opened.ancestor_names) + 1 == len(opened.ancestor_fds)
        ):
            raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
        try:
            for descriptor, expected in zip(
                opened.ancestor_fds,
                opened.ancestor_identities,
                strict=True,
            ):
                current = os.fstat(descriptor)
                if _ancestor_identity(current) != expected:
                    raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
                if (current.st_dev, current.st_ino) == self._data_root.identity:
                    raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
            for index, name in enumerate(opened.ancestor_names, start=1):
                entry = os.stat(
                    name,
                    dir_fd=opened.ancestor_fds[index - 1],
                    follow_symlinks=False,
                )
                child = os.fstat(opened.ancestor_fds[index])
                if not stat.S_ISDIR(entry.st_mode) or (entry.st_dev, entry.st_ino) != (
                    child.st_dev,
                    child.st_ino,
                ):
                    raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
            final_entry = os.stat(
                opened.final_name,
                dir_fd=opened.ancestor_fds[-1],
                follow_symlinks=False,
            )
            current_source = os.fstat(opened.fd)
            if _identity(final_entry) != _identity(opened.before) or _identity(
                current_source
            ) != _identity(opened.before):
                raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
        except OSError:
            raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT) from None
        self._ensure_live()

    @staticmethod
    def _hash_bound(root: SafeRoot, value: _Binding) -> _Binding:
        root_fd = _open_owned(root)
        try:
            digest, size, info = root.hash_open_file(
                root_fd,
                value.name,
                maximum=_MAX_SOURCE_BYTES,
            )
        except StorageFailure:
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE) from None
        finally:
            _close_owned(root_fd)
        current = _binding(info, name=value.name, sha256=digest)
        if current != value or size != value.size:
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        return current

    @staticmethod
    def _current_binding(root: SafeRoot, name: str) -> _Binding:
        root_fd = _open_owned(root)
        try:
            digest, _, info = root.hash_open_file(
                root_fd,
                name,
                maximum=_MAX_SOURCE_BYTES,
            )
            return _binding(info, name=name, sha256=digest)
        except StorageFailure:
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE) from None
        finally:
            _close_owned(root_fd)

    @staticmethod
    def _maybe_current_binding(root: SafeRoot, name: str) -> _Binding | None:
        root_fd = _open_owned(root)
        try:
            try:
                os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            except OSError:
                raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
        finally:
            _close_owned(root_fd)
        return DurableProjectService._current_binding(root, name)

    def _remove_record_owned_partial(self, root: SafeRoot, name: str) -> bool:
        catalog = self._acquire(_CATALOG_RESOURCE)
        try:
            return _quarantine_unlink(
                root,
                name,
                expected=None,
                receipt_required=True,
                quota_admit=self._quota_admit,
            )
        finally:
            self._release(catalog)

    def _copy_stage_to_work(self, record: _Record) -> _Record:
        if record.stage is None:
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        stage = self._hash_bound(self._staging, record.stage)
        work_name = f".work.{record.intent_hmac[:32]}.FCStd"
        if _quarantine_recovery_present(self._work, work_name) and not (
            self._remove_record_owned_partial(self._work, work_name)
        ):
            raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
        existing_work = self._maybe_current_binding(self._work, work_name)
        if existing_work is not None:
            if existing_work.size != stage.size or existing_work.sha256 != stage.sha256:
                if not self._remove_record_owned_partial(self._work, work_name):
                    raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
            else:
                updated = replace(
                    record,
                    work=existing_work,
                    validation_started=False,
                    work_validated=False,
                )
                self._write_record(updated)
                return updated
        source_root_fd = _open_owned(self._staging)
        destination_root_fd = _open_owned(self._work)
        source_fd = -1
        destination_fd = -1
        created = False
        digest = hashlib.sha256()
        try:
            source_fd = os.open(
                stage.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=source_root_fd,
            )
            source_info = os.fstat(source_fd)
            if _info_binding_identity(source_info) != _binding_identity(stage):
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
            catalog = self._acquire(_CATALOG_RESOURCE)
            try:
                self._quota_admit(extra_bytes=0, extra_files=1)
                destination_fd = os.open(
                    work_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=destination_root_fd,
                )
                created = True
            finally:
                self._release(catalog)
            remaining = stage.size
            while remaining:
                chunk = os.read(source_fd, min(_COPY_CHUNK_BYTES, remaining))
                if not chunk:
                    raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise OSError
                    view = view[written:]
                remaining -= len(chunk)
            after_source = os.fstat(source_fd)
            if _info_binding_identity(after_source) != _binding_identity(stage):
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
            os.fsync(destination_fd)
            work_info = os.fstat(destination_fd)
            if (
                not self._work.regular_file(work_info, maximum=_MAX_SOURCE_BYTES)
                or work_info.st_size != stage.size
                or digest.hexdigest() != stage.sha256
            ):
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
            os.close(destination_fd)
            destination_fd = -1
            os.fsync(destination_root_fd)
            work = _binding(work_info, name=work_name, sha256=digest.hexdigest())
            updated = replace(
                record,
                work=work,
                validation_started=False,
                work_validated=False,
            )
            self._write_record(updated)
            return updated
        except _ServiceError:
            raise
        except OSError:
            raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
        finally:
            for descriptor in (
                source_fd,
                destination_fd,
                source_root_fd,
                destination_root_fd,
            ):
                if descriptor >= 0:
                    with contextlib.suppress(OSError):
                        os.close(descriptor)
            if created and "updated" not in locals():
                with contextlib.suppress(_ServiceError):
                    self._remove_record_owned_partial(self._work, work_name)

    def _discard_work(self, record: _Record) -> _Record:
        if record.work is None:
            return record
        if not self._unlink_bound(self._work, record.work):
            raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
        updated = replace(
            record,
            work=None,
            validation_started=False,
            work_validated=False,
        )
        self._write_record(updated)
        return updated

    def _validate_work(self, record: _Record) -> _Record:
        if record.work is None:
            record = self._copy_stage_to_work(record)
        if record.work is None:
            raise _ServiceError(ProjectServicePortErrorCode.INTERNAL_ERROR)
        if record.validation_started and not record.work_validated:
            return self._revalidate_uncertain_work(record)
        if record.work_validated:
            try:
                self._hash_bound(self._work, record.work)
            except _ServiceError:
                normalized_name = f".normalized.{record.intent_hmac[:32]}.FCStd"
                normalized = self._current_binding(self._normalized, normalized_name)
                if (
                    _binding_identity(normalized) != _binding_identity(record.work)
                    or normalized.sha256 != record.work.sha256
                ):
                    raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE) from None
            return record
        self._hash_bound(self._work, record.work)
        try:
            port = self._cad_port_factory(revision_store=self._revision_store)
            if not isinstance(port, CadExecutionPort):
                raise TypeError
            with _candidate_file_limit(self._revision_store):
                record = replace(record, validation_started=True)
                self._write_record(record)
                evidence = _call_cad_from_pinned_root(
                    self._work,
                    record.work.name,
                    port.validate_import,
                )
            if type(evidence) is not ValidatedImportEvidence:
                raise TypeError
            current = self._current_binding(self._work, record.work.name)
            if current.sha256 != evidence.sha256 or current.size != evidence.size_bytes:
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        except RevisionStoreError as error:
            raise _ServiceError(self._map_revision_error(error)) from None
        except ExecutorError as error:
            if type(error) is ExecutorError and error.code is ExecutorErrorCode.INVALID_INPUT:
                return self._reject_invalid(record)
            raise _ServiceError(self._map_executor_error(error)) from None
        except ValueError as error:
            if type(error) is not ValueError:
                raise _ServiceError(ProjectServicePortErrorCode.CAD_FAILURE) from None
            current_record = record
            with contextlib.suppress(_ServiceError):
                current = self._current_binding(self._work, record.work.name)
                current_record = replace(
                    record,
                    work=current,
                    validation_started=True,
                    work_validated=False,
                )
                self._write_record(current_record)
            return self._reject_invalid(current_record)
        except _ServiceError:
            raise
        except Exception:
            try:
                current = self._current_binding(self._work, record.work.name)
                reset = replace(
                    record,
                    work=current,
                    validation_started=False,
                    work_validated=False,
                )
                self._write_record(reset)
                self._discard_work(reset)
            except _ServiceError as cleanup_error:
                if cleanup_error.code is ProjectServicePortErrorCode.RECOVERY_REQUIRED:
                    raise
            raise _ServiceError(ProjectServicePortErrorCode.CAD_FAILURE) from None
        validated_work = replace(
            record,
            work=current,
            validation_started=True,
            work_validated=True,
        )
        self._write_record(validated_work)
        return validated_work

    def _revalidate_uncertain_work(self, record: _Record) -> _Record:
        if record.work is None or not record.validation_started or record.work_validated:
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        try:
            port = self._cad_port_factory(revision_store=self._revision_store)
            if not isinstance(port, CadExecutionPort):
                raise TypeError
            evidence = _call_cad_from_pinned_root(
                self._work,
                record.work.name,
                port.revalidate_normalized_import,
            )
            if type(evidence) is not ValidatedImportEvidence:
                raise TypeError
            current = self._current_binding(self._work, record.work.name)
            if current.sha256 != evidence.sha256 or current.size != evidence.size_bytes:
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        except RevisionStoreError as error:
            raise _ServiceError(self._map_revision_error(error)) from None
        except ExecutorError as error:
            if type(error) is ExecutorError and error.code is ExecutorErrorCode.INVALID_INPUT:
                return self._reject_invalid(record)
            raise _ServiceError(self._map_executor_error(error)) from None
        except ValueError as error:
            if type(error) is not ValueError:
                raise _ServiceError(ProjectServicePortErrorCode.CAD_FAILURE) from None
            current = self._current_binding(self._work, record.work.name)
            reset = replace(
                record,
                work=current,
                validation_started=False,
                work_validated=False,
            )
            self._write_record(reset)
            reset = self._discard_work(reset)
            return self._validate_work(reset)
        except _ServiceError:
            raise
        except Exception:
            raise _ServiceError(ProjectServicePortErrorCode.CAD_FAILURE) from None
        validated = replace(
            record,
            work=current,
            validation_started=True,
            work_validated=True,
        )
        self._write_record(validated)
        return validated

    def _move_validated_work(self, record: _Record) -> _Record:
        if record.work is None or not record.work_validated:
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        normalized_name = f".normalized.{record.intent_hmac[:32]}.FCStd"
        work_fd = _open_owned(self._work)
        normalized_fd = _open_owned(self._normalized)
        catalog = self._acquire(_CATALOG_RESOURCE)
        try:
            try:
                current = os.stat(record.work.name, dir_fd=work_fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    moved = os.stat(
                        normalized_name,
                        dir_fd=normalized_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED) from None
                if _info_binding_identity(moved) != _binding_identity(record.work):
                    raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE) from None
            else:
                if _info_binding_identity(current) != _binding_identity(record.work):
                    raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
                if not _rename_noreplace(
                    work_fd,
                    record.work.name,
                    normalized_name,
                    destination_root_fd=normalized_fd,
                ):
                    raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
                os.fsync(work_fd)
                os.fsync(normalized_fd)
            normalized = self._current_binding(self._normalized, normalized_name)
            if normalized.sha256 != record.work.sha256 or normalized.size != record.work.size:
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
            updated = replace(
                record,
                phase="VALIDATED",
                work=None,
                validation_started=True,
                work_validated=False,
                normalized=normalized,
            )
            self._write_record_under_catalog(updated)
            return updated
        except OSError:
            raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED) from None
        finally:
            self._release(catalog)
            _close_owned(
                normalized_fd,
                code=ProjectServicePortErrorCode.RECOVERY_REQUIRED,
            )
            _close_owned(
                work_fd,
                code=ProjectServicePortErrorCode.RECOVERY_REQUIRED,
            )

    @staticmethod
    def _map_revision_error(error: RevisionStoreError) -> ProjectServicePortErrorCode:
        code = error.code
        if code in {
            RevisionStoreErrorCode.INVALID_IDENTIFIER,
            RevisionStoreErrorCode.INVALID_INPUT,
            RevisionStoreErrorCode.BUDGET_EXCEEDED,
        }:
            return ProjectServicePortErrorCode.INVALID_INPUT
        if code is RevisionStoreErrorCode.NOT_FOUND:
            return ProjectServicePortErrorCode.NOT_FOUND
        if code in {RevisionStoreErrorCode.ALREADY_EXISTS, RevisionStoreErrorCode.CONFLICT}:
            return ProjectServicePortErrorCode.CONFLICT
        if code is RevisionStoreErrorCode.RESOURCE_EXHAUSTED:
            return ProjectServicePortErrorCode.RESOURCE_EXHAUSTED
        if code in {
            RevisionStoreErrorCode.CORRUPT_RECORD,
            RevisionStoreErrorCode.CORRUPT_CONTENT,
        }:
            return ProjectServicePortErrorCode.INTEGRITY_FAILURE
        if code in {
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            RevisionStoreErrorCode.RECOVERY_REQUIRED,
            RevisionStoreErrorCode.CLEANUP_REQUIRED,
        }:
            return ProjectServicePortErrorCode.RECOVERY_REQUIRED
        return ProjectServicePortErrorCode.STORE_FAILURE

    @staticmethod
    def _map_executor_error(error: ExecutorError) -> ProjectServicePortErrorCode:
        if type(error) is not ExecutorError or type(error.code) is not ExecutorErrorCode:
            return ProjectServicePortErrorCode.INTERNAL_ERROR
        if error.code is ExecutorErrorCode.INVALID_INPUT:
            return ProjectServicePortErrorCode.INVALID_INPUT
        if error.code is ExecutorErrorCode.CAD_FAILURE:
            return ProjectServicePortErrorCode.CAD_FAILURE
        if error.code in {
            ExecutorErrorCode.ARTIFACT_FAILURE,
            ExecutorErrorCode.INTEGRITY_FAILURE,
        }:
            return ProjectServicePortErrorCode.INTEGRITY_FAILURE
        return ProjectServicePortErrorCode.INTERNAL_ERROR

    def _project_lease(self, project_id: str):
        deadline = time.monotonic() + _LEASE_WAIT_SECONDS
        while True:
            try:
                return self._lease_manager.acquire_project_write(project_id)
            except LeaseError as error:
                if error.code is LeaseErrorCode.CONTENDED:
                    if time.monotonic() >= deadline:
                        raise _ServiceError(ProjectServicePortErrorCode.LEASE_UNAVAILABLE) from None
                    time.sleep(_LEASE_RETRY_SECONDS)
                    continue
                if error.code is LeaseErrorCode.WRONG_PROCESS:
                    raise _ServiceError(ProjectServicePortErrorCode.INTERNAL_ERROR) from None
                raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
            except Exception:
                raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None

    def _generation_zero(self, record: _Record) -> ProjectCreateResult:
        try:
            head = self._revision_store.load_head(record.project_id)
            revision = self._revision_store.load_revision(
                record.project_id,
                head.revision_id,
            )
        except RevisionStoreError as error:
            raise _ServiceError(self._map_revision_error(error)) from None
        except Exception:
            raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
        if not (
            type(head) is ProjectHead
            and type(revision) is RevisionRef
            and head.project_id == record.project_id
            and head.generation == 0
            and revision.project_id == record.project_id
            and revision.id == head.revision_id
            and revision.manifest_sha256 == head.manifest_sha256
            and revision.base_revision is None
            and revision.artifacts == ()
        ):
            raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
        if record.kind is ProjectKind.EMPTY:
            if revision.model is not None:
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        else:
            if (
                record.normalized is None
                or revision.model is None
                or revision.model.name != "model.FCStd"
                or revision.model.format != "fcstd"
                or revision.model.sha256 != record.normalized.sha256
                or revision.model.size_bytes != record.normalized.size
            ):
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        return ProjectCreateResult(
            create_key=record.create_key,
            kind=record.kind,
            cleanup_required=False,
            project_id=record.project_id,
            head=head,
            revision=revision,
        )

    def _publish(self, record: _Record) -> ProjectCreateResult:
        source_parent_fd = -1
        source_binding = None
        if record.kind is ProjectKind.IMPORT_FCSTD:
            if record.phase != "VALIDATED" or record.normalized is None:
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
            self._hash_bound(self._normalized, record.normalized)
            source_parent_fd = _open_owned(self._normalized)
            try:
                source_info = os.stat(
                    record.normalized.name,
                    dir_fd=source_parent_fd,
                    follow_symlinks=False,
                )
                if _info_binding_identity(source_info) != _binding_identity(record.normalized):
                    raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
                source_binding = RevisionSourceBinding(
                    dev=source_info.st_dev,
                    ino=source_info.st_ino,
                    mode=source_info.st_mode,
                    uid=source_info.st_uid,
                    nlink=source_info.st_nlink,
                    size=source_info.st_size,
                    mtime_ns=source_info.st_mtime_ns,
                    ctime_ns=source_info.st_ctime_ns,
                )
            except BaseException as error:
                try:
                    _close_owned(
                        source_parent_fd,
                        code=ProjectServicePortErrorCode.RECOVERY_REQUIRED,
                    )
                except _ServiceError as close_error:
                    raise close_error from error
                source_parent_fd = -1
                if type(error) is RevisionStoreError:
                    raise _ServiceError(self._map_revision_error(error)) from None
                if isinstance(error, OSError):
                    raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE) from None
                raise
        elif record.phase != "RESERVED":
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        lease = None
        receipt = None
        try:
            lease = self._project_lease(record.project_id)
            try:
                if record.kind is ProjectKind.EMPTY:
                    self._revision_store.initialize_empty_project(record.project_id, lease)
                else:
                    if source_parent_fd < 0 or type(source_binding) is not RevisionSourceBinding:
                        raise _ServiceError(ProjectServicePortErrorCode.INTERNAL_ERROR)
                    self._revision_store.import_trusted_fcstd_at(
                        record.project_id,
                        source_parent_fd=source_parent_fd,
                        source_name=record.normalized.name,
                        expected_binding=source_binding,
                        expected_sha256=record.normalized.sha256,
                        expected_size=record.normalized.size,
                        lease=lease,
                    )
            except RevisionStoreError as error:
                recoverable = error.code is RevisionStoreErrorCode.ALREADY_EXISTS or (
                    error.code is RevisionStoreErrorCode.DURABILITY_UNCERTAIN
                    and getattr(error, "head_committed", False) is True
                )
                if not recoverable:
                    raise _ServiceError(self._map_revision_error(error)) from None
            result = self._generation_zero(record)
            if record.kind is ProjectKind.EMPTY:
                receipt = replace(
                    record,
                    phase="PUBLISHED",
                    reservation_bytes=0,
                    outcome="PUBLISHED",
                    generation_zero=result,
                )
                self._write_record(receipt)
                return result
            receipt = replace(
                record,
                phase="CLEANUP_REQUIRED",
                outcome="PUBLISHED",
                generation_zero=replace(result, cleanup_required=True),
            )
            self._write_record(receipt)
            return self._converge_cleanup(receipt)
        finally:
            close_failed = False
            if source_parent_fd >= 0:
                try:
                    _close_owned(
                        source_parent_fd,
                        code=ProjectServicePortErrorCode.RECOVERY_REQUIRED,
                    )
                except _ServiceError:
                    close_failed = True
            if lease is not None:
                try:
                    self._release(lease)
                except _ServiceError:
                    if receipt is None or record.kind is ProjectKind.EMPTY:
                        raise
            if close_failed:
                raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)

    def _verify_frozen(self, record: _Record) -> ProjectCreateResult:
        result = record.generation_zero
        if type(result) is not ProjectCreateResult:
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        try:
            revision = self._revision_store.load_revision(
                result.project_id,
                result.revision.id,
            )
        except RevisionStoreError as error:
            raise _ServiceError(self._map_revision_error(error)) from None
        except Exception:
            raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
        if type(revision) is not RevisionRef or revision != result.revision:
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        return replace(
            result,
            cleanup_required=record.phase == "CLEANUP_REQUIRED",
        )

    def _unlink_bound(self, root: SafeRoot, value: _Binding) -> bool:
        catalog = self._acquire(_CATALOG_RESOURCE)
        try:
            return _quarantine_unlink(
                root,
                value.name,
                expected=value,
                receipt_required=True,
                quota_admit=self._quota_admit,
            )
        finally:
            self._release(catalog)

    def _cleanup_record_files(self, record: _Record) -> bool:
        values = (
            (self._staging, record.stage),
            (self._work, record.work),
            (self._normalized, record.normalized),
        )
        succeeded = True
        for root, value in values:
            if value is not None and not self._unlink_bound(root, value):
                succeeded = False
        return succeeded

    def _converge_cleanup(self, record: _Record) -> ProjectCreateResult:
        if record.phase != "CLEANUP_REQUIRED" or record.outcome not in _OUTCOMES:
            raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
        cleaned = self._cleanup_record_files(record)
        if not cleaned:
            if record.outcome == "PUBLISHED":
                return self._verify_frozen(record)
            raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
        terminal_phase = record.outcome
        terminal = replace(
            record,
            phase=terminal_phase,
            reservation_bytes=0,
            stage=None,
            work=None,
            validation_started=False,
            work_validated=False,
            normalized=None,
        )
        if terminal.generation_zero is not None:
            terminal = replace(
                terminal,
                generation_zero=replace(
                    terminal.generation_zero,
                    cleanup_required=False,
                ),
            )
        self._write_record(terminal)
        if terminal_phase == "REJECTED":
            raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
        return self._verify_frozen(terminal)

    def _reject_invalid(self, record: _Record):
        receipt = replace(
            record,
            phase="CLEANUP_REQUIRED",
            outcome="REJECTED",
            failure_code="invalid_input",
            generation_zero=None,
        )
        self._write_record(receipt)
        return self._converge_cleanup(receipt)

    def _resume(self, record: _Record, opened_source: _OpenedSource | None):
        if record.phase == "PUBLISHED":
            return self._verify_frozen(record)
        if record.phase == "REJECTED":
            raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
        if record.phase == "CLEANUP_REQUIRED":
            return self._converge_cleanup(record)
        if record.kind is ProjectKind.EMPTY:
            return self._publish(record)
        if record.phase == "RESERVED":
            if opened_source is None:
                raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
            record = self._copy_source_to_stage(opened_source, record)
            opened_source = None
        if record.phase == "STAGED":
            record = self._validate_work(record)
            if record.phase == "STAGED":
                record = self._move_validated_work(record)
        if record.phase == "VALIDATED":
            return self._publish(record)
        raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)

    def create_project(
        self,
        *,
        create_key: str,
        kind: ProjectKind,
        source_path: str | None,
    ) -> ProjectCreateResult | ProjectServicePortFailure:
        opened_source = None
        key_lease = None
        slot_lease = None
        result = None
        fatal = None
        try:
            self._ensure_live()
            if type(create_key) is not str or _CREATE_KEY.fullmatch(create_key) is None:
                raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
            if type(kind) is not ProjectKind:
                raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
            if (kind is ProjectKind.EMPTY) != (source_path is None):
                raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
            record, opened_source = self._admit_record(
                create_key=create_key,
                kind=kind,
                source_path=source_path,
            )
            key_lease = self._acquire(f"{_PER_KEY_RESOURCE_PREFIX}{create_key}")
            current = self._load_record(create_key)
            if current is None:
                raise _ServiceError(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
            if not (
                current.project_id == record.project_id
                and current.key_id == record.key_id
                and hmac.compare_digest(current.intent_hmac, record.intent_hmac)
                and current.kind is record.kind
            ):
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
            record = current
            if record.phase in {"PUBLISHED", "REJECTED"}:
                result = self._resume(record, opened_source)
            else:
                slot_lease = self._acquire_slot()
                result = self._resume(record, opened_source)
        except _ServiceError as error:
            result = self._failure(error.code)
        except Exception:
            result = self._failure(ProjectServicePortErrorCode.INTERNAL_ERROR)
        except BaseException as error:
            fatal = error
        close_failed = False
        if opened_source is not None:
            try:
                opened_source.close()
            except _ServiceError:
                close_failed = True
        release_failed = False
        for lease in (slot_lease, key_lease):
            if lease is not None and getattr(lease, "released", True) is False:
                try:
                    self._release(lease)
                except _ServiceError:
                    release_failed = True
        if close_failed or release_failed:
            if fatal is not None:
                raise fatal.with_traceback(fatal.__traceback__)
            return self._failure(ProjectServicePortErrorCode.RECOVERY_REQUIRED)
        if fatal is not None:
            raise fatal.with_traceback(fatal.__traceback__)
        if type(result) not in {ProjectCreateResult, ProjectServicePortFailure}:
            return self._failure(ProjectServicePortErrorCode.INTERNAL_ERROR)
        return result

    def get_project(
        self,
        *,
        project_id: str,
    ) -> ProjectCurrentResult | ProjectServicePortFailure:
        try:
            self._ensure_live()
            if type(project_id) is not str or _PROJECT_ID.fullmatch(project_id) is None:
                raise _ServiceError(ProjectServicePortErrorCode.INVALID_INPUT)
            try:
                first = self._revision_store.load_head(project_id)
                revision = self._revision_store.load_revision(project_id, first.revision_id)
                second = self._revision_store.load_head(project_id)
            except RevisionStoreError as error:
                code = self._map_revision_error(error)
                if error.code is RevisionStoreErrorCode.NOT_FOUND:
                    code = ProjectServicePortErrorCode.NOT_FOUND
                raise _ServiceError(code) from None
            except Exception:
                raise _ServiceError(ProjectServicePortErrorCode.STORE_FAILURE) from None
            if type(first) is not ProjectHead or type(second) is not ProjectHead:
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
            if first != second:
                raise _ServiceError(ProjectServicePortErrorCode.CONFLICT)
            if not (
                type(revision) is RevisionRef
                and first.project_id == project_id
                and revision.project_id == project_id
                and first.revision_id == revision.id
                and first.manifest_sha256 == revision.manifest_sha256
            ):
                raise _ServiceError(ProjectServicePortErrorCode.INTEGRITY_FAILURE)
            return ProjectCurrentResult(
                project_id=project_id,
                head=first,
                revision=revision,
            )
        except _ServiceError as error:
            return self._failure(error.code)
        except Exception:
            return self._failure(ProjectServicePortErrorCode.INTERNAL_ERROR)
