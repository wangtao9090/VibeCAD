"""Read-only observation of verified revision and published delivery manifests."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import vibecad.application.artifacts as _delivery
from vibecad.application.artifacts import (
    ArtifactEligibility,
    ArtifactExportRequest,
    ArtifactRequestPhase,
    ArtifactSourceKind,
    ArtifactStoreError,
    ArtifactStoreErrorCode,
)
from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionAncestrySnapshot,
    RevisionArtifactRef,
    RevisionRef,
    RevisionStoreError,
    RevisionStoreErrorCode,
)
from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER
from vibecad.workflow.state import (
    ReviewPolicy,
    TaskArtifactRef,
    TaskRun,
    TaskStatus,
    VerificationReport,
)
from vibecad.workflow.store import (
    StoredTaskRun,
    TaskRunStore,
    TaskStoreError,
    TaskStoreErrorCode,
)

__all__ = (
    "ArtifactManifestError",
    "ArtifactManifestErrorCode",
    "ArtifactManifestService",
)

_TASK_ID = re.compile(r"^task_[0-9a-f]{32}$")
_REVISION_ID = re.compile(r"^revision_[0-9a-f]{32}$")
_DRAFT_ID = re.compile(r"^draft_[0-9a-f]{32}$")
_VERIFICATION_DOMAIN = b"vibecad-verification-report-v1\0"
_CATALOG_LOCK_SECONDS = 5.0


class ArtifactManifestErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INVALID_STATE = "invalid_state"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INTEGRITY_FAILURE = "integrity_failure"
    STORE_FAILURE = "store_failure"
    RECOVERY_REQUIRED = "recovery_required"


class ArtifactManifestError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: ArtifactManifestErrorCode) -> None:
        if type(code) is not ArtifactManifestErrorCode:
            raise TypeError("code must be an exact ArtifactManifestErrorCode")
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class _DeliveryObservation:
    materialization_id: str
    delivery_manifest_sha256: str
    resource_uris: tuple[str, str]


def _raise(code: ArtifactManifestErrorCode) -> None:
    raise ArtifactManifestError(code)


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
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)


def _identifier(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _raise(ArtifactManifestErrorCode.INVALID_INPUT)
    return value


def _task_error(error: TaskStoreError, *, known: bool = False) -> None:
    mapping = {
        TaskStoreErrorCode.INVALID_ID: ArtifactManifestErrorCode.INVALID_INPUT,
        TaskStoreErrorCode.NOT_FOUND: (
            ArtifactManifestErrorCode.CONFLICT if known else ArtifactManifestErrorCode.NOT_FOUND
        ),
        TaskStoreErrorCode.ALREADY_EXISTS: ArtifactManifestErrorCode.STORE_FAILURE,
        TaskStoreErrorCode.CONFLICT: ArtifactManifestErrorCode.CONFLICT,
        TaskStoreErrorCode.CORRUPT_RECORD: ArtifactManifestErrorCode.INTEGRITY_FAILURE,
        TaskStoreErrorCode.RECORD_TOO_LARGE: ArtifactManifestErrorCode.INTEGRITY_FAILURE,
        TaskStoreErrorCode.UNSAFE_STORE: ArtifactManifestErrorCode.STORE_FAILURE,
        TaskStoreErrorCode.LOCK_UNAVAILABLE: ArtifactManifestErrorCode.RECOVERY_REQUIRED,
        TaskStoreErrorCode.IO_ERROR: ArtifactManifestErrorCode.STORE_FAILURE,
        TaskStoreErrorCode.DURABILITY_UNCERTAIN: ArtifactManifestErrorCode.RECOVERY_REQUIRED,
        TaskStoreErrorCode.RESOURCE_EXHAUSTED: ArtifactManifestErrorCode.RESOURCE_EXHAUSTED,
    }
    _raise(mapping.get(error.code, ArtifactManifestErrorCode.STORE_FAILURE))


def _revision_error(error: RevisionStoreError, *, known: bool = False) -> None:
    mapping = {
        RevisionStoreErrorCode.INVALID_IDENTIFIER: ArtifactManifestErrorCode.INVALID_INPUT,
        RevisionStoreErrorCode.INVALID_INPUT: ArtifactManifestErrorCode.INVALID_INPUT,
        RevisionStoreErrorCode.NOT_FOUND: (
            ArtifactManifestErrorCode.INTEGRITY_FAILURE
            if known
            else ArtifactManifestErrorCode.NOT_FOUND
        ),
        RevisionStoreErrorCode.ALREADY_EXISTS: ArtifactManifestErrorCode.STORE_FAILURE,
        RevisionStoreErrorCode.CONFLICT: ArtifactManifestErrorCode.CONFLICT,
        RevisionStoreErrorCode.CORRUPT_RECORD: ArtifactManifestErrorCode.INTEGRITY_FAILURE,
        RevisionStoreErrorCode.CORRUPT_CONTENT: ArtifactManifestErrorCode.INTEGRITY_FAILURE,
        RevisionStoreErrorCode.BUDGET_EXCEEDED: ArtifactManifestErrorCode.RESOURCE_EXHAUSTED,
        RevisionStoreErrorCode.RESOURCE_EXHAUSTED: (ArtifactManifestErrorCode.RESOURCE_EXHAUSTED),
        RevisionStoreErrorCode.UNSAFE_STORE: ArtifactManifestErrorCode.INTEGRITY_FAILURE,
        RevisionStoreErrorCode.INVALID_LEASE: ArtifactManifestErrorCode.STORE_FAILURE,
        RevisionStoreErrorCode.IO_ERROR: ArtifactManifestErrorCode.STORE_FAILURE,
        RevisionStoreErrorCode.DURABILITY_UNCERTAIN: (ArtifactManifestErrorCode.RECOVERY_REQUIRED),
        RevisionStoreErrorCode.RECOVERY_REQUIRED: ArtifactManifestErrorCode.RECOVERY_REQUIRED,
        RevisionStoreErrorCode.CLEANUP_REQUIRED: ArtifactManifestErrorCode.RECOVERY_REQUIRED,
    }
    _raise(mapping.get(error.code, ArtifactManifestErrorCode.STORE_FAILURE))


def _artifact_store_error(error: ArtifactStoreError) -> None:
    mapping = {
        ArtifactStoreErrorCode.INVALID_INPUT: ArtifactManifestErrorCode.INVALID_INPUT,
        ArtifactStoreErrorCode.NOT_FOUND: ArtifactManifestErrorCode.NOT_FOUND,
        ArtifactStoreErrorCode.CONFLICT: ArtifactManifestErrorCode.CONFLICT,
        ArtifactStoreErrorCode.INVALID_STATE: ArtifactManifestErrorCode.INVALID_STATE,
        ArtifactStoreErrorCode.RESOURCE_EXHAUSTED: (ArtifactManifestErrorCode.RESOURCE_EXHAUSTED),
        ArtifactStoreErrorCode.INTEGRITY_FAILURE: (ArtifactManifestErrorCode.INTEGRITY_FAILURE),
        ArtifactStoreErrorCode.IO_ERROR: ArtifactManifestErrorCode.STORE_FAILURE,
        ArtifactStoreErrorCode.RECOVERY_REQUIRED: ArtifactManifestErrorCode.RECOVERY_REQUIRED,
    }
    _raise(mapping.get(error.code, ArtifactManifestErrorCode.STORE_FAILURE))


def _load_task(store: TaskRunStore, task_id: str, *, known: bool = False) -> StoredTaskRun:
    try:
        value = store.load(task_id)
    except TaskStoreError as error:
        _task_error(error, known=known)
    except Exception:
        _raise(ArtifactManifestErrorCode.STORE_FAILURE)
    if (
        type(value) is not StoredTaskRun
        or type(value.generation) is not int
        or type(value.task_run) is not TaskRun
        or value.task_run.id != task_id
    ):
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
    return value


def _snapshot(
    store: LocalRevisionStore,
    project_id: str,
) -> RevisionAncestrySnapshot:
    try:
        value = store.snapshot_revisions(project_id)
    except RevisionStoreError as error:
        _revision_error(error, known=True)
    except Exception:
        _raise(ArtifactManifestErrorCode.STORE_FAILURE)
    if type(value) is not RevisionAncestrySnapshot or value.project_id != project_id:
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
    return value


def _load_revision(
    store: LocalRevisionStore,
    *,
    project_id: str,
    revision_id: str,
) -> RevisionRef:
    try:
        value = store.load_revision(project_id, revision_id)
    except RevisionStoreError as error:
        _revision_error(error, known=True)
    except Exception:
        _raise(ArtifactManifestErrorCode.STORE_FAILURE)
    if type(value) is not RevisionRef or value.project_id != project_id or value.id != revision_id:
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
    return value


def _artifact_pair(
    task: TaskRun, revision: RevisionRef
) -> tuple[
    RevisionArtifactRef,
    RevisionArtifactRef,
]:
    if (
        type(revision.model) is not RevisionArtifactRef
        or type(revision.artifacts) is not tuple
        or len(revision.artifacts) != 1
        or type(revision.artifacts[0]) is not RevisionArtifactRef
        or len(task.artifacts) != 2
    ):
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
    model = revision.model
    step = revision.artifacts[0]
    if (model.name, model.format, step.name, step.format) != (
        "model.FCStd",
        "fcstd",
        "model.step",
        "step",
    ) or model.id == step.id:
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
    for task_ref, revision_ref in zip(task.artifacts, (model, step), strict=True):
        if not (
            type(task_ref) is TaskArtifactRef
            and task_ref.id == revision_ref.id
            and task_ref.name == revision_ref.name
            and task_ref.format == revision_ref.format
            and task_ref.sha256 == revision_ref.sha256
            and task_ref.size_bytes == revision_ref.size_bytes
            and task_ref.candidate_revision == revision.id
        ):
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
    return model, step


def _source_and_report(
    task: TaskRun,
    *,
    revision: RevisionRef,
    draft_id: str | None,
) -> tuple[ArtifactSourceKind, VerificationReport]:
    if task.program is None or task.candidate_revision != revision.id:
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
    report_id = None
    if draft_id is None:
        if task.status is not TaskStatus.SUCCEEDED or task.committed_revision != revision.id:
            _raise(ArtifactManifestErrorCode.INVALID_STATE)
        source_kind = ArtifactSourceKind.COMMITTED
        if task.review_policy is ReviewPolicy.REQUIRE_REVIEW:
            if task.draft is None or task.draft.revision_id != revision.id:
                _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
            report_id = task.draft.verification_id
    else:
        draft = task.draft
        if (
            task.status is not TaskStatus.AWAITING_USER_REVIEW
            or draft is None
            or draft.id != draft_id
            or draft.task_id != task.id
            or draft.project_id != task.project_id
            or draft.revision_id != revision.id
        ):
            _raise(ArtifactManifestErrorCode.INVALID_STATE)
        if draft.manifest_sha256 != revision.manifest_sha256:
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        source_kind = ArtifactSourceKind.DRAFT
        report_id = draft.verification_id
    candidates = tuple(
        report
        for report in task.verification_reports
        if type(report) is VerificationReport
        and report.passed
        and report.candidate_revision == revision.id
        and report.acceptance_id == task.program.acceptance.id
    )
    if report_id is None:
        if not candidates:
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        report = candidates[-1]
    else:
        matches = tuple(report for report in candidates if report.id == report_id)
        if len(matches) != 1:
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        report = matches[0]
    if report.manifest_sha256 != revision.manifest_sha256:
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
    return source_kind, report


def _validate_requested_source(
    task: TaskRun,
    *,
    revision_id: str,
    draft_id: str | None,
) -> None:
    if task.candidate_revision != revision_id:
        _raise(ArtifactManifestErrorCode.INVALID_STATE)
    if draft_id is None:
        if task.committed_revision != revision_id:
            _raise(ArtifactManifestErrorCode.INVALID_STATE)
        return
    draft = task.draft
    if draft is None or draft.id != draft_id or draft.revision_id != revision_id:
        _raise(ArtifactManifestErrorCode.INVALID_STATE)


def _verification_digest(report: VerificationReport) -> str:
    return hashlib.sha256(_VERIFICATION_DOMAIN + _canonical(report.to_mapping())).hexdigest()


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
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


def _private_directory(value: os.stat_result) -> bool:
    return bool(
        stat.S_ISDIR(value.st_mode)
        and value.st_uid == os.geteuid()
        and stat.S_IMODE(value.st_mode) == 0o700
    )


def _private_file(value: os.stat_result, *, nonempty: bool = False) -> bool:
    return bool(
        stat.S_ISREG(value.st_mode)
        and value.st_uid == os.geteuid()
        and value.st_nlink == 1
        and stat.S_IMODE(value.st_mode) == 0o600
        and (not nonempty or value.st_size > 0)
    )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )


def _close(fd: int) -> bool:
    if fd < 0:
        return False
    try:
        os.close(fd)
    except OSError:
        return True
    return False


def _read_file(
    directory_fd: int,
    name: str,
    *,
    maximum: int,
) -> bytes:
    file_fd = -1
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not _private_file(before, nonempty=True) or before.st_size > maximum:
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        file_fd = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
        opened = os.fstat(file_fd)
        if _identity(opened) != _identity(before):
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        remaining = opened.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(
                file_fd,
                min(_delivery.ARTIFACT_COPY_CHUNK_BYTES, remaining),
            )
            if not chunk or len(chunk) > remaining:
                _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
            chunks.append(chunk)
            remaining -= len(chunk)
        if _identity(os.fstat(file_fd)) != _identity(opened):
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(after) != _identity(before):
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        return b"".join(chunks)
    except ArtifactManifestError:
        raise
    except OSError:
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
    finally:
        if _close(file_fd):
            _raise(ArtifactManifestErrorCode.RECOVERY_REQUIRED)


def _hash_file(
    directory_fd: int,
    reference: RevisionArtifactRef,
) -> None:
    file_fd = -1
    try:
        before = os.stat(
            reference.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not _private_file(before, nonempty=True)
            or before.st_size != reference.size_bytes
            or before.st_size > _delivery.MAX_ARTIFACT_SOURCE_BYTES
        ):
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        file_fd = os.open(
            reference.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
        opened = os.fstat(file_fd)
        if _identity(opened) != _identity(before):
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        remaining = reference.size_bytes
        digest = hashlib.sha256()
        while remaining:
            chunk = os.read(
                file_fd,
                min(_delivery.ARTIFACT_COPY_CHUNK_BYTES, remaining),
            )
            if not chunk or len(chunk) > remaining:
                _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
            digest.update(chunk)
            remaining -= len(chunk)
        if _identity(os.fstat(file_fd)) != _identity(opened):
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        after = os.stat(
            reference.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if _identity(after) != _identity(before) or digest.hexdigest() != reference.sha256:
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
    except ArtifactManifestError:
        raise
    except OSError:
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
    finally:
        if _close(file_fd):
            _raise(ArtifactManifestErrorCode.RECOVERY_REQUIRED)


def _open_directory(
    parent_fd: int,
    name: str,
    *,
    root_device: int,
) -> tuple[int, os.stat_result]:
    directory_fd = -1
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _private_directory(before) or before.st_dev != root_device:
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        directory_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
        opened = os.fstat(directory_fd)
        if _identity(opened) != _identity(before):
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        return directory_fd, opened
    except ArtifactManifestError:
        if _close(directory_fd):
            _raise(ArtifactManifestErrorCode.RECOVERY_REQUIRED)
        raise
    except OSError:
        if _close(directory_fd):
            _raise(ArtifactManifestErrorCode.RECOVERY_REQUIRED)
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)


def _pin_directory(
    parent_fd: int,
    name: str,
    directory_fd: int,
    opened: os.stat_result,
) -> None:
    try:
        current = os.fstat(directory_fd)
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
    if _identity(current) != _identity(opened) or _identity(entry) != _identity(opened):
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)


def _request_name(export_key: str) -> str:
    suffix = hashlib.sha256(_delivery._REQUEST_PATH_DOMAIN + export_key.encode("ascii")).hexdigest()
    return f"{suffix}.json"


def _parse_record(raw: bytes):
    try:
        return _delivery._parse_record(raw)
    except ArtifactStoreError as error:
        _artifact_store_error(error)
    except Exception:
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)


def _validate_delivery_manifest(
    directory_fd: int,
    eligibility: ArtifactEligibility,
    expected_digest: str,
) -> None:
    raw = _read_file(
        directory_fd,
        "manifest.json",
        maximum=_delivery.MAX_ARTIFACT_RECORD_BYTES,
    )
    try:
        data = _delivery._parse_json(raw)
    except ArtifactStoreError as error:
        _artifact_store_error(error)
    except Exception:
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
    if type(data) is not dict or set(data) != {
        "schema_version",
        "body",
        "body_sha256",
    }:
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
    body = _delivery._delivery_manifest_body(eligibility)
    digest = hashlib.sha256(
        _delivery._DELIVERY_MANIFEST_DOMAIN + _delivery._canonical_json(body)
    ).hexdigest()
    if (
        data["schema_version"] != 1
        or data["body"] != body
        or data["body_sha256"] != expected_digest
        or digest != expected_digest
    ):
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)


def _validate_published(
    materializations_fd: int,
    *,
    root_device: int,
    record,
    eligibility: ArtifactEligibility,
) -> _DeliveryObservation:
    expected_request = ArtifactExportRequest(
        export_key=record.export_key,
        task_id=eligibility.task_id,
        expected_generation=eligibility.task_generation,
        revision_id=eligibility.revision_id,
        draft_id=eligibility.draft_id,
    )
    expected_result = _delivery._result(expected_request, eligibility)
    if (
        record.request_digest != _delivery._request_digest(expected_request)
        or record.response != expected_result
    ):
        _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
    directory_fd, opened = _open_directory(
        materializations_fd,
        record.materialization_id,
        root_device=root_device,
    )
    try:
        stored_identity = record.materialized_identity
        if (
            stored_identity is None
            or opened.st_dev != stored_identity.dev
            or opened.st_ino != stored_identity.ino
            or opened.st_uid != stored_identity.uid
            or stat.S_IMODE(opened.st_mode) != stored_identity.mode
        ):
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        try:
            names = tuple(sorted(entry.name for entry in os.scandir(directory_fd)))
        except OSError:
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        if names != ("manifest.json", "model.FCStd", "model.step"):
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        for reference in eligibility.artifacts:
            _hash_file(directory_fd, reference)
        _validate_delivery_manifest(
            directory_fd,
            eligibility,
            record.delivery_manifest_sha256,
        )
        _pin_directory(
            materializations_fd,
            record.materialization_id,
            directory_fd,
            opened,
        )
    finally:
        if _close(directory_fd):
            _raise(ArtifactManifestErrorCode.RECOVERY_REQUIRED)
    return _DeliveryObservation(
        materialization_id=record.materialization_id,
        delivery_manifest_sha256=record.delivery_manifest_sha256,
        resource_uris=tuple(item.resource_uri for item in expected_result.artifacts),
    )


def _published_record_matches(record, eligibility: ArtifactEligibility) -> bool:
    try:
        expected_request = ArtifactExportRequest(
            export_key=record.export_key,
            task_id=eligibility.task_id,
            expected_generation=eligibility.task_generation,
            revision_id=eligibility.revision_id,
            draft_id=eligibility.draft_id,
        )
        return record.request_digest == _delivery._request_digest(
            expected_request
        ) and record.response == _delivery._result(expected_request, eligibility)
    except Exception:
        return False


def _catalog_entries(root_fd: int) -> tuple[str, ...]:
    try:
        return tuple(sorted(entry.name for entry in os.scandir(root_fd)))
    except OSError:
        _raise(ArtifactManifestErrorCode.STORE_FAILURE)


def _root_extra_code(names: tuple[str, ...]) -> None:
    allowed = {"requests", "materializations", _delivery._LOCK_NAME}
    extras = tuple(name for name in names if name not in allowed)
    if not extras:
        return
    recoverable = (
        _delivery._TEMPORARY_NAME,
        _delivery._CLEANUP_RECEIPT_NAME,
        _delivery._CLEANUP_RECEIPT_TEMP_NAME,
    )
    if all(any(pattern.fullmatch(name) for pattern in recoverable) for name in extras):
        _raise(ArtifactManifestErrorCode.RECOVERY_REQUIRED)
    _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)


def _scan_requests(
    requests_fd: int,
) -> tuple[tuple[object, ...], dict[str, int], int]:
    records = []
    record_sizes: dict[str, int] = {}
    total_bytes = 0
    try:
        entries = tuple(sorted(os.scandir(requests_fd), key=lambda entry: entry.name))
    except OSError:
        _raise(ArtifactManifestErrorCode.STORE_FAILURE)
    for entry in entries:
        if _delivery._REQUEST_TEMP_NAME.fullmatch(entry.name):
            _raise(ArtifactManifestErrorCode.RECOVERY_REQUIRED)
        if _delivery._REQUEST_NAME.fullmatch(entry.name) is None:
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        raw = _read_file(
            requests_fd,
            entry.name,
            maximum=_delivery.MAX_ARTIFACT_RECORD_BYTES,
        )
        record = _parse_record(raw)
        if entry.name != _request_name(record.export_key):
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        records.append(record)
        record_sizes[record.export_key] = len(raw)
        total_bytes += len(raw)
        if len(records) > _delivery.MAX_ARTIFACT_REQUESTS:
            _raise(ArtifactManifestErrorCode.RESOURCE_EXHAUSTED)
    return tuple(records), record_sizes, total_bytes


def _validate_materialization_structure(
    materializations_fd: int,
    *,
    root_device: int,
    name: str,
) -> int:
    directory_fd, opened = _open_directory(
        materializations_fd,
        name,
        root_device=root_device,
    )
    total_bytes = 0
    try:
        try:
            entries = tuple(sorted(os.scandir(directory_fd), key=lambda entry: entry.name))
        except OSError:
            _raise(ArtifactManifestErrorCode.STORE_FAILURE)
        for entry in entries:
            try:
                value = entry.stat(follow_symlinks=False)
            except OSError:
                _raise(ArtifactManifestErrorCode.STORE_FAILURE)
            if not _private_file(value, nonempty=True) or entry.name not in {
                "manifest.json",
                "model.FCStd",
                "model.step",
            }:
                _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
            limit = (
                _delivery.MAX_ARTIFACT_RECORD_BYTES
                if entry.name == "manifest.json"
                else _delivery.MAX_ARTIFACT_SOURCE_BYTES
            )
            if value.st_size > limit:
                _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
            total_bytes += value.st_size
        _pin_directory(
            materializations_fd,
            name,
            directory_fd,
            opened,
        )
    finally:
        if _close(directory_fd):
            _raise(ArtifactManifestErrorCode.RECOVERY_REQUIRED)
    return total_bytes


def _scan_materialization_names(
    materializations_fd: int,
    *,
    root_device: int,
) -> tuple[tuple[str, ...], dict[str, int], int]:
    names = []
    materialization_sizes: dict[str, int] = {}
    total_bytes = 0
    try:
        entries = tuple(sorted(os.scandir(materializations_fd), key=lambda entry: entry.name))
    except OSError:
        _raise(ArtifactManifestErrorCode.STORE_FAILURE)
    for entry in entries:
        try:
            value = entry.stat(follow_symlinks=False)
        except OSError:
            _raise(ArtifactManifestErrorCode.STORE_FAILURE)
        if _delivery._MATERIALIZATION_NAME.fullmatch(entry.name) is None or not _private_directory(
            value
        ):
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        size = _validate_materialization_structure(
            materializations_fd,
            root_device=root_device,
            name=entry.name,
        )
        names.append(entry.name)
        materialization_sizes[entry.name] = size
        total_bytes += size
        if len(names) > _delivery.MAX_ARTIFACT_MATERIALIZATIONS:
            _raise(ArtifactManifestErrorCode.RESOURCE_EXHAUSTED)
    return tuple(names), materialization_sizes, total_bytes


def _observe_existing_delivery(
    *,
    root: Path,
    expected_root_identity: tuple[int, int],
    eligibility: ArtifactEligibility,
) -> _DeliveryObservation | None:
    root_fd = -1
    requests_fd = -1
    materializations_fd = -1
    lock_fd = -1
    local_lock = None
    local_acquired = False
    file_locked = False
    result = None
    failure: ArtifactManifestError | None = None
    try:
        try:
            alias = os.lstat(root)
            if (
                not _private_directory(alias)
                or (alias.st_dev, alias.st_ino) != expected_root_identity
            ):
                _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
            root_fd = os.open(root, _directory_flags())
            opened_root = os.fstat(root_fd)
        except ArtifactManifestError:
            raise
        except OSError:
            _raise(ArtifactManifestErrorCode.STORE_FAILURE)
        if _identity(alias) != _identity(opened_root):
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        names = _catalog_entries(root_fd)
        if not names:
            final_root = os.fstat(root_fd)
            final_alias = os.lstat(root)
            if _identity(final_root) != _identity(opened_root) or _identity(
                final_alias
            ) != _identity(opened_root):
                _raise(ArtifactManifestErrorCode.RECOVERY_REQUIRED)
            owned_root = root_fd
            root_fd = -1
            if _close(owned_root):
                _raise(ArtifactManifestErrorCode.RECOVERY_REQUIRED)
            return None
        _root_extra_code(names)
        required = {"requests", "materializations", _delivery._LOCK_NAME}
        if not required.issubset(names):
            _raise(ArtifactManifestErrorCode.RECOVERY_REQUIRED)
        local_lock = _delivery._thread_lock(root / _delivery._LOCK_NAME)
        if not local_lock.acquire(timeout=_CATALOG_LOCK_SECONDS):
            _raise(ArtifactManifestErrorCode.RECOVERY_REQUIRED)
        local_acquired = True
        try:
            lock_before = os.stat(
                _delivery._LOCK_NAME,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            if not _private_file(lock_before):
                _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
            lock_fd = os.open(
                _delivery._LOCK_NAME,
                os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
            if _identity(os.fstat(lock_fd)) != _identity(lock_before):
                _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        except ArtifactManifestError:
            raise
        except OSError:
            _raise(ArtifactManifestErrorCode.STORE_FAILURE)
        deadline = time.monotonic() + _CATALOG_LOCK_SECONDS
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                file_locked = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    _raise(ArtifactManifestErrorCode.RECOVERY_REQUIRED)
                time.sleep(0.01)
            except OSError:
                _raise(ArtifactManifestErrorCode.STORE_FAILURE)
        root_started = os.fstat(root_fd)
        names = _catalog_entries(root_fd)
        _root_extra_code(names)
        if set(names) != required:
            _raise(ArtifactManifestErrorCode.RECOVERY_REQUIRED)
        requests_fd, requests_started = _open_directory(
            root_fd,
            "requests",
            root_device=root_started.st_dev,
        )
        materializations_fd, materializations_started = _open_directory(
            root_fd,
            "materializations",
            root_device=root_started.st_dev,
        )
        records, record_sizes, request_bytes = _scan_requests(requests_fd)
        (
            materialization_names,
            materialization_sizes,
            materialization_bytes,
        ) = _scan_materialization_names(
            materializations_fd,
            root_device=root_started.st_dev,
        )
        try:
            _delivery.ArtifactStore._inventory_from_scan(
                ordinary_bytes=lock_before.st_size + request_bytes + materialization_bytes,
                requests=len(records),
                materializations=len(materialization_names),
                temporaries=0,
                records=list(records),
                record_sizes=record_sizes,
                temporary_sizes={},
                materialization_sizes=materialization_sizes,
            )
        except ArtifactStoreError as error:
            _artifact_store_error(error)
        except Exception:
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        matching = tuple(
            record
            for record in records
            if record.phase is ArtifactRequestPhase.PUBLISHED and record.eligibility == eligibility
        )
        if matching:
            first = matching[0]
            if any(
                record.materialization_id != first.materialization_id
                or record.delivery_manifest_sha256 != first.delivery_manifest_sha256
                or not _published_record_matches(record, eligibility)
                for record in matching
            ):
                _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
            if first.materialization_id not in materialization_names:
                _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
            result = _validate_published(
                materializations_fd,
                root_device=root_started.st_dev,
                record=first,
                eligibility=eligibility,
            )
        _pin_directory(
            root_fd,
            "requests",
            requests_fd,
            requests_started,
        )
        _pin_directory(
            root_fd,
            "materializations",
            materializations_fd,
            materializations_started,
        )
        final_root = os.fstat(root_fd)
        final_alias = os.lstat(root)
        if _identity(final_root) != _identity(root_started) or _identity(final_alias) != _identity(
            root_started
        ):
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
    except ArtifactManifestError as error:
        failure = error
    except OSError:
        failure = ArtifactManifestError(ArtifactManifestErrorCode.STORE_FAILURE)
    finally:
        close_failed = _close(materializations_fd)
        close_failed = _close(requests_fd) or close_failed
        if file_locked:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                close_failed = True
        close_failed = _close(lock_fd) or close_failed
        if local_acquired:
            try:
                local_lock.release()
            except RuntimeError:
                close_failed = True
        close_failed = _close(root_fd) or close_failed
        if close_failed:
            failure = ArtifactManifestError(ArtifactManifestErrorCode.RECOVERY_REQUIRED)
    if failure is not None:
        raise failure
    return result


def _artifact_projection(
    reference: RevisionArtifactRef,
    *,
    resource_uri: str | None,
) -> dict[str, object]:
    return {
        "schema_version": reference.schema_version,
        "id": reference.id,
        "name": reference.name,
        "format": reference.format,
        "sha256": reference.sha256,
        "size_bytes": reference.size_bytes,
        "resource_uri": resource_uri,
    }


class ArtifactManifestService:
    """Resolve one task-bound manifest without exporting or materializing bytes."""

    __slots__ = (
        "_artifact_root",
        "_artifact_root_identity",
        "_revision_store",
        "_task_store",
    )

    def __init__(
        self,
        *,
        task_store: TaskRunStore,
        revision_store: LocalRevisionStore,
        artifact_root: Path,
        expected_artifact_root_identity: tuple[int, int],
    ) -> None:
        if (
            type(task_store) is not TaskRunStore
            or type(revision_store) is not LocalRevisionStore
            or type(artifact_root) is not type(Path("/"))
            or not artifact_root.is_absolute()
            or type(expected_artifact_root_identity) is not tuple
            or len(expected_artifact_root_identity) != 2
            or not all(type(item) is int and item >= 0 for item in expected_artifact_root_identity)
            or task_store._lease_manager is not revision_store._lease_manager
        ):
            _raise(ArtifactManifestErrorCode.INVALID_INPUT)
        self._task_store = task_store
        self._revision_store = revision_store
        self._artifact_root = artifact_root
        self._artifact_root_identity = expected_artifact_root_identity

    def get_artifact_manifest(
        self,
        *,
        task_id: object,
        expected_generation: object,
        revision_id: object,
        draft_id: object,
    ) -> dict[str, object]:
        selected_task = _identifier(task_id, _TASK_ID)
        selected_revision = _identifier(revision_id, _REVISION_ID)
        if (
            type(expected_generation) is not int
            or expected_generation < 0
            or expected_generation > MAX_SAFE_JSON_INTEGER
        ):
            _raise(ArtifactManifestErrorCode.INVALID_INPUT)
        selected_draft = None
        if draft_id is not None:
            selected_draft = _identifier(draft_id, _DRAFT_ID)
        task_before = _load_task(self._task_store, selected_task)
        if task_before.generation != expected_generation:
            _raise(ArtifactManifestErrorCode.CONFLICT)
        task = task_before.task_run
        _validate_requested_source(
            task,
            revision_id=selected_revision,
            draft_id=selected_draft,
        )
        before = _snapshot(self._revision_store, task.project_id)
        revision = _load_revision(
            self._revision_store,
            project_id=task.project_id,
            revision_id=selected_revision,
        )
        source_kind, report = _source_and_report(
            task,
            revision=revision,
            draft_id=selected_draft,
        )
        if source_kind is ArtifactSourceKind.COMMITTED and not any(
            entry.id == revision.id for entry in before.revisions
        ):
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        model, step = _artifact_pair(task, revision)
        try:
            eligibility = ArtifactEligibility(
                source_kind=source_kind,
                task_id=task.id,
                task_generation=task_before.generation,
                project_id=task.project_id,
                revision_id=revision.id,
                manifest_sha256=revision.manifest_sha256,
                draft_id=selected_draft,
                artifacts=(model, step),
            )
        except Exception:
            _raise(ArtifactManifestErrorCode.INTEGRITY_FAILURE)
        delivery = _observe_existing_delivery(
            root=self._artifact_root,
            expected_root_identity=self._artifact_root_identity,
            eligibility=eligibility,
        )
        after = _snapshot(self._revision_store, task.project_id)
        task_after = _load_task(self._task_store, selected_task, known=True)
        if after != before or task_after != task_before:
            _raise(ArtifactManifestErrorCode.CONFLICT)
        resource_uris: tuple[str | None, str | None]
        if delivery is None:
            resource_uris = (None, None)
        else:
            resource_uris = delivery.resource_uris
        return {
            "source_kind": source_kind.value,
            "task_id": task.id,
            "task_generation": task_before.generation,
            "project_id": task.project_id,
            "revision_id": revision.id,
            "draft_id": selected_draft,
            "manifest_sha256": revision.manifest_sha256,
            "verification_id": report.id,
            "acceptance_id": report.acceptance_id,
            "verification_digest": _verification_digest(report),
            "observation_digest": report.observation_digest,
            "materialized": delivery is not None,
            "materialization_id": (None if delivery is None else delivery.materialization_id),
            "delivery_manifest_sha256": (
                None if delivery is None else delivery.delivery_manifest_sha256
            ),
            "artifacts": [
                _artifact_projection(reference, resource_uri=uri)
                for reference, uri in zip(
                    (model, step),
                    resource_uris,
                    strict=True,
                )
            ],
        }
