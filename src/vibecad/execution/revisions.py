"""Immutable local CAD revision persistence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER
from vibecad.workflow.lease import ProjectWriteLease, ResourceLeaseManager

__all__ = (
    "CommitJournal",
    "CommitJournalState",
    "LocalRevisionStore",
    "ProjectHead",
    "ReconciliationResult",
    "ReconciliationStatus",
    "RevisionArtifactRef",
    "RevisionRef",
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
_COPY_CHUNK_BYTES = 65536
_MAX_RECORD_OPEN_ATTEMPTS = 3

_PROJECT_PATH_DOMAIN = b"vibecad-revision-project-path-v1\0"
_REVISION_PATH_DOMAIN = b"vibecad-revision-content-path-v1\0"
_CANDIDATE_PATH_DOMAIN = b"vibecad-revision-candidate-path-v1\0"
_MANIFEST_CHECKSUM_DOMAIN = b"vibecad-revision-manifest-v1\0"
_HEAD_CHECKSUM_DOMAIN = b"vibecad-project-head-v1\0"
_JOURNAL_CHECKSUM_DOMAIN = b"vibecad-commit-journal-v1\0"

_PROJECT_PATTERN = r"project_[0-9a-f]{32}"
_REVISION_PATTERN = r"revision_[0-9a-f]{32}"
_ARTIFACT_PATTERN = r"artifact_[0-9a-f]{32}"
_TRANSACTION_PATTERN = r"transaction_[0-9a-f]{32}"
_DIGEST_PATTERN = r"[0-9a-f]{64}"
_ARTIFACT_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}"


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
    failed = False
    directory_fd = None
    try:
        directory_fd = os.open(name, _root_flags(), dir_fd=parent_fd)
    except OSError:
        failed = True
    if failed or directory_fd is None:
        return (None, RevisionStoreErrorCode.UNSAFE_STORE)
    stat_failed = False
    opened_stat = None
    try:
        opened_stat = os.fstat(directory_fd)
    except OSError:
        stat_failed = True
    if stat_failed or opened_stat is None:
        _close_fd(directory_fd)
        return (None, RevisionStoreErrorCode.UNSAFE_STORE)
    if not _safe_directory_stat(opened_stat, root_device):
        _close_fd(directory_fd)
        return (None, RevisionStoreErrorCode.UNSAFE_STORE)
    if opened_stat.st_dev != before[0].st_dev or opened_stat.st_ino != before[0].st_ino:
        _close_fd(directory_fd)
        return (None, RevisionStoreErrorCode.UNSAFE_STORE)
    return (directory_fd, None)


def _open_project(root_fd, root_device, project_id):
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
        _close_fd(project_fd)
        return (None, None, None, revisions_open[1])
    candidates_open = _open_safe_directory(
        project_fd,
        "candidates",
        root_device,
        RevisionStoreErrorCode.UNSAFE_STORE,
    )
    if candidates_open[1] is not None:
        _close_fd(revisions_open[0])
        _close_fd(project_fd)
        return (None, None, None, candidates_open[1])
    return (project_fd, revisions_open[0], candidates_open[0], None)


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
        opened_stat = None
        stat_failed = False
        try:
            opened_stat = os.fstat(file_fd)
        except OSError:
            stat_failed = True
        if stat_failed or opened_stat is None:
            _close_fd(file_fd)
            return (None, None, RevisionStoreErrorCode.IO_ERROR)
        replaceable_unlinked = False
        if replaceable:
            replaceable_unlinked = _safe_unlinked_replaceable_stat(
                opened_stat,
                root_device,
            )
        if replaceable_unlinked:
            close_failed = _close_fd(file_fd)
            if close_failed:
                return (None, None, RevisionStoreErrorCode.IO_ERROR)
            continue
        if not _safe_immutable_stat(opened_stat, root_device):
            _close_fd(file_fd)
            return (None, None, RevisionStoreErrorCode.UNSAFE_STORE)
        if opened_stat.st_dev == before[0].st_dev and opened_stat.st_ino == before[0].st_ino:
            return (file_fd, opened_stat, None)
        close_failed = _close_fd(file_fd)
        if close_failed:
            return (None, None, RevisionStoreErrorCode.IO_ERROR)
        if not replaceable:
            return (None, None, RevisionStoreErrorCode.UNSAFE_STORE)
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
    after_stat = None
    stat_failed = False
    try:
        after_stat = os.fstat(file_fd)
    except OSError:
        stat_failed = True
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


def _create_durable_file(parent_fd, name, raw):
    failed = False
    file_fd = None
    try:
        file_fd = os.open(name, _create_flags(), 384, dir_fd=parent_fd)
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


def _load_revision_fd(revisions_fd, root_device, project_id, revision_id):
    opened = _open_revision_directory(revisions_fd, root_device, revision_id)
    if opened[1] is not None:
        return (None, opened[1])
    revision_fd = opened[0]
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
    content_code = _validate_revision_content(revision_fd, root_device, revision_value)
    close_failed = _close_fd(revision_fd)
    if content_code is not None:
        return (None, content_code)
    if close_failed:
        return (None, RevisionStoreErrorCode.IO_ERROR)
    return (revision_value, None)


def _validate_revision_content(revision_fd, root_device, revision_value):
    if revision_value.model is not None:
        model_code = _validate_content_file(revision_fd, root_device, revision_value.model)
        if model_code is not None:
            return model_code
    content_artifacts = revision_value.artifacts
    if type(content_artifacts) is not type(()):
        return RevisionStoreErrorCode.CORRUPT_RECORD
    for content_artifact in content_artifacts:
        artifact_code = _validate_content_file(revision_fd, root_device, content_artifact)
        if artifact_code is not None:
            return artifact_code
    return None


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
        return opened[2]
    content_fd = opened[0]
    content_stat = opened[1]
    if content_stat.st_size != reference.size_bytes:
        _close_fd(content_fd)
        return RevisionStoreErrorCode.CORRUPT_CONTENT
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
        return RevisionStoreErrorCode.IO_ERROR
    if after_stat.st_dev != content_stat.st_dev or after_stat.st_ino != content_stat.st_ino:
        return RevisionStoreErrorCode.CORRUPT_CONTENT
    if after_stat.st_size != content_stat.st_size:
        return RevisionStoreErrorCode.CORRUPT_CONTENT
    if after_stat.st_mtime_ns != content_stat.st_mtime_ns:
        return RevisionStoreErrorCode.CORRUPT_CONTENT
    if after_stat.st_ctime_ns != content_stat.st_ctime_ns:
        return RevisionStoreErrorCode.CORRUPT_CONTENT
    actual_digest = content_hash_state.hexdigest()
    if actual_digest != reference.sha256:
        return RevisionStoreErrorCode.CORRUPT_CONTENT
    return None


def _load_head_fd(project_fd, revisions_fd, root_device, project_id):
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


def _copy_open_file(source_parent_fd, source_fd, source_stat, source_name, target_fd, target_name):
    destination_fd = None
    open_failed = False
    try:
        destination_fd = os.open(target_name, _create_flags(), 384, dir_fd=target_fd)
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
        _best_rmdir(root_fd, temp_name)
        return
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
            _best_unlink(revision_fd, "model.FCStd")
            _best_unlink(revision_fd, "manifest.json")
            _close_fd(revision_fd)
            _best_rmdir(revisions_fd, revision_name)
        _close_fd(revisions_fd)
        _best_rmdir(project_fd, "revisions")
    if candidates_fd is not None:
        _close_fd(candidates_fd)
        _best_rmdir(project_fd, "candidates")
    _best_unlink(project_fd, "HEAD.json")
    _close_fd(project_fd)
    _best_rmdir(root_fd, temp_name)


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


def _initialize_project(store, project_id, source, lease):
    mutation_code = _require_mutation(store, project_id, lease)
    if mutation_code is not None:
        raise RevisionStoreError(mutation_code)
    source_open = None
    if source is not None:
        source_open = _open_external_source(source)
        if source_open[4] is not None:
            raise RevisionStoreError(source_open[4])
    root_open = _open_store_root(store)
    if root_open[2] is not None:
        if source_open is not None:
            _close_fd(source_open[1])
            _close_fd(source_open[0])
        raise RevisionStoreError(root_open[2])
    root_fd = root_open[0]
    final_name = _project_key(project_id)
    existing = _entry_stat(root_fd, final_name)
    if existing[2] is not None:
        if source_open is not None:
            _close_fd(source_open[1])
            _close_fd(source_open[0])
        _close_fd(root_fd)
        raise RevisionStoreError(existing[2])
    if existing[1]:
        if source_open is not None:
            _close_fd(source_open[1])
            _close_fd(source_open[0])
        _close_fd(root_fd)
        if _safe_directory_stat(existing[0], root_open[1].st_dev):
            raise RevisionStoreError(RevisionStoreErrorCode.ALREADY_EXISTS)
        raise RevisionStoreError(RevisionStoreErrorCode.UNSAFE_STORE)
    revision_id = _new_revision_id()
    if _identifier_code(revision_id, _REVISION_PATTERN) is not None:
        if source_open is not None:
            _close_fd(source_open[1])
            _close_fd(source_open[0])
        _close_fd(root_fd)
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_IDENTIFIER)
    revision_name = _revision_key(revision_id)
    temp_name = ".project." + secrets.token_hex(16) + ".tmp"
    creation_failed = False
    try:
        os.mkdir(temp_name, 448, dir_fd=root_fd)
    except OSError:
        creation_failed = True
    if creation_failed:
        if source_open is not None:
            _close_fd(source_open[1])
            _close_fd(source_open[0])
        _close_fd(root_fd)
        raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
    project_fd = None
    revisions_fd = None
    candidates_fd = None
    revision_fd = None
    code = None
    try:
        project_fd = os.open(temp_name, _root_flags(), dir_fd=root_fd)
        os.mkdir("revisions", 448, dir_fd=project_fd)
        os.mkdir("candidates", 448, dir_fd=project_fd)
        revisions_fd = os.open("revisions", _root_flags(), dir_fd=project_fd)
        candidates_fd = os.open("candidates", _root_flags(), dir_fd=project_fd)
        os.mkdir(revision_name, 448, dir_fd=revisions_fd)
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
        )
        if copied[2] is not None:
            code = copied[2]
        else:
            model = RevisionArtifactRef(
                id=_new_artifact_id(),
                name="model.FCStd",
                format="fcstd",
                sha256=copied[0],
                size_bytes=copied[1],
            )
    if source_open is not None:
        if _close_two(source_open[1], source_open[0]):
            if code is None:
                code = RevisionStoreErrorCode.IO_ERROR
    artifacts = ()
    if code is None:
        manifest_body = _manifest_body(project_id, revision_id, None, model, artifacts)
        manifest_raw = _checked_record_bytes(manifest_body, _MANIFEST_CHECKSUM_DOMAIN)
        code = _create_durable_file(revision_fd, "manifest.json", manifest_raw)
    if code is None:
        manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
        head = ProjectHead(
            project_id=project_id,
            generation=0,
            revision_id=revision_id,
            manifest_sha256=manifest_digest,
        )
        head_raw = _checked_record_bytes(_head_mapping(head), _HEAD_CHECKSUM_DOMAIN)
        code = _create_durable_file(project_fd, "HEAD.json", head_raw)
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
        _cleanup_initial(root_fd, temp_name, revision_name)
        _close_fd(root_fd)
        raise RevisionStoreError(code)
    rename_failed = False
    try:
        os.rename(temp_name, final_name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
    except OSError:
        rename_failed = True
    if rename_failed:
        _cleanup_initial(root_fd, temp_name, revision_name)
        _close_fd(root_fd)
        raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
    sync_failed = False
    try:
        os.fsync(root_fd)
    except OSError:
        sync_failed = True
    root_close_failed = _close_fd(root_fd)
    if sync_failed or root_close_failed:
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
    failed = _close_fd(candidate_fd) or failed
    failed = _best_rmdir(candidates_fd, candidate_name) or failed
    sync_failed = False
    try:
        os.fsync(candidates_fd)
    except OSError:
        sync_failed = True
    return failed or sync_failed


def _begin_revision(store, project_id, expected_head, lease):
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
        unlink_failed = _best_unlink(project_fd, "journal.json")
        sync_failed = False
        if not unlink_failed:
            try:
                os.fsync(project_fd)
            except OSError:
                sync_failed = True
        if unlink_failed or sync_failed:
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
    mkdir_failed = False
    try:
        os.mkdir(candidate_name, 448, dir_fd=candidates_fd)
    except OSError:
        mkdir_failed = True
    if mkdir_failed:
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
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
        code = _replace_durable_record(
            project_fd,
            "journal.json",
            journal_raw,
            secrets.token_hex(16),
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
        )
    if code is not None and staging is None:
        _cleanup_candidate_dir(candidates_fd, candidate_name, root_open[1].st_dev)
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
    return revision_id


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
    code = _replace_durable_record(
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

    def commit_revision(self, project_id, expected_head, revision_id, lease):
        return _commit_revision(self, project_id, expected_head, revision_id, lease)

    def import_trusted_fcstd(self, project_id, source, lease):
        return _initialize_project(self, project_id, source, lease)

    def initialize_empty_project(self, project_id, lease):
        return _initialize_project(self, project_id, None, lease)

    def load_head(self, project_id):
        return _load_head(self, project_id)

    def load_revision(self, project_id, revision_id):
        return _load_revision(self, project_id, revision_id)

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
        _best_rmdir(revisions_fd, temp_name)
        return
    _best_unlink(revision_fd, "model.FCStd")
    _best_unlink(revision_fd, "model.step")
    _best_unlink(revision_fd, "manifest.json")
    _close_fd(revision_fd)
    _best_rmdir(revisions_fd, temp_name)


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
    mkdir_failed = False
    try:
        os.mkdir(temp_name, 448, dir_fd=revisions_fd)
    except OSError:
        mkdir_failed = True
    if mkdir_failed:
        _close_fd(model_source[0])
        _close_fd(step_source[0])
        _close_fd(candidate_fd)
        _close_project_fds(project_open)
        _close_fd(root_open[0])
        raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
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
        )
        code = copied_step[2]
    if _close_fd(step_source[0]) and code is None:
        code = RevisionStoreErrorCode.IO_ERROR
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
        code = _create_durable_file(revision_fd, "manifest.json", manifest_raw)
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
        rename_failed = False
        try:
            os.rename(temp_name, final_name, src_dir_fd=revisions_fd, dst_dir_fd=revisions_fd)
        except OSError:
            rename_failed = True
        if rename_failed:
            code = RevisionStoreErrorCode.IO_ERROR
        else:
            published = True
    if code is None:
        sync_failed = False
        try:
            os.fsync(revisions_fd)
        except OSError:
            sync_failed = True
        if sync_failed:
            code = RevisionStoreErrorCode.DURABILITY_UNCERTAIN
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
        code = _replace_durable_record(
            project_fd,
            "journal.json",
            prepared_raw,
            secrets.token_hex(16),
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
        )
    candidate_close_failed = _close_fd(candidate_fd)
    cleanup_failed = False
    if code is None:
        cleanup_failed = _cleanup_candidate_dir(
            candidates_fd,
            candidate_name,
            root_open[1].st_dev,
        )
    if not published:
        _cleanup_revision_temp(revisions_fd, temp_name)
    close_failed = _close_project_fds(project_open)
    close_failed = _close_fd(root_open[0]) or close_failed
    if code is RevisionStoreErrorCode.DURABILITY_UNCERTAIN:
        raise RevisionStoreError(code, head_committed=False)
    if code is not None:
        raise RevisionStoreError(code)
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
    replaced = _replace_head_record(project_open[0], head_raw, secrets.token_hex(16))
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
    journal_code = _replace_durable_record(
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


def _persist_journal(project_fd, journal):
    raw = _checked_record_bytes(_journal_mapping(journal), _JOURNAL_CHECKSUM_DOMAIN)
    return _replace_durable_record(
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
                journal_code = _persist_journal(project_open[0], result_journal)
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
                journal_code = _persist_journal(project_open[0], result_journal)
                if journal_code is not None:
                    code = RevisionStoreErrorCode.RECOVERY_REQUIRED
            if code is None:
                result_status = ReconciliationStatus.COMMITTED
        else:
            code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    else:
        code = RevisionStoreErrorCode.RECOVERY_REQUIRED
    if cleanup and code is None:
        cleanup_failed = _cleanup_candidate_dir(
            project_open[2],
            _candidate_key(journal.candidate_revision),
            root_open[1].st_dev,
        )
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
