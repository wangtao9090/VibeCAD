"""Verified, path-free materialization of immutable task CAD artifacts.

This module owns the transport-neutral export contract and its local durable
request catalog.  Revision bytes remain authoritative behind an injected
descriptor-copy port: callers never provide an output pathname and this layer
never asks the source port for one.
"""

from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import sys
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionArtifactRef,
    RevisionCopyCursor,
    RevisionRef,
    RevisionStoreError,
    RevisionStoreErrorCode,
)
from vibecad.interaction.cad import CadExecutionPort, ValidatedMaterializationEvidence
from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER, SCHEMA_VERSION
from vibecad.workflow.lease import (
    LeaseError,
    LeaseErrorCode,
    ResourceLease,
    ResourceLeaseManager,
)
from vibecad.workflow.state import TaskArtifactRef, TaskRun, TaskStatus
from vibecad.workflow.store import (
    StoredTaskRun,
    TaskRunStore,
    TaskStoreError,
    TaskStoreErrorCode,
)

MAX_ARTIFACT_SOURCE_BYTES = 512 * 1024 * 1024
MAX_ARTIFACT_PAIR_BYTES = 1024 * 1024 * 1024
MAX_ARTIFACT_STORE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARTIFACT_REQUESTS = 4096
MAX_ARTIFACT_MATERIALIZATIONS = 4096
MAX_ARTIFACT_TEMPORARIES = 8
MAX_ARTIFACT_RECORD_BYTES = 64 * 1024
MAX_ARTIFACT_RESOURCE_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_RESOURCE_BASE64_BYTES = 89_478_488
MAX_ARTIFACT_RESOURCE_INCREMENTAL_BYTES = 402_653_184
ARTIFACT_COPY_CHUNK_BYTES = 64 * 1024
ABANDONED_ARTIFACT_TEMP_TTL_SECONDS = 86_400
_RESOURCE_ALLOCATION_OVERHEAD_BYTES = 32 * 1024 * 1024

_MAX_REQUEST_BYTES = 8 * 1024
_MAX_REQUEST_DEPTH = 16
_MAX_REQUEST_NODES = 256
_MAX_REQUEST_STRING_BYTES = 4096
_MAX_JSON_KEY_BYTES = 256
_MAX_ERROR_PATH_BYTES = 256
_LOCK_TIMEOUT_SECONDS = 5.0

_EXPORT_KEY = re.compile(r"^export_[0-9a-f]{32}$")
_TASK_ID = re.compile(r"^task_[0-9a-f]{32}$")
_PROJECT_ID = re.compile(r"^project_[0-9a-f]{32}$")
_REVISION_ID = re.compile(r"^revision_[0-9a-f]{32}$")
_DRAFT_ID = re.compile(r"^draft_[0-9a-f]{32}$")
_ARTIFACT_ID = re.compile(r"^artifact_[0-9a-f]{32}$")
_VERIFICATION_ID = re.compile(r"^verification_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_NAME = re.compile(r"^[0-9a-f]{64}\.json$")
_REQUEST_TEMP_NAME = re.compile(r"^\.[0-9a-f]{64}\.json\.[0-9a-f]{32}\.tmp$")
_CLEANUP_RECEIPT_NAME = re.compile(r"^cleanup_[0-9a-f]{64}\.json$")
_CLEANUP_RECEIPT_TEMP_NAME = re.compile(r"^\.cleanup_[0-9a-f]{64}\.json\.[0-9a-f]{32}\.tmp$")
_MATERIALIZATION_NAME = re.compile(r"^materialization_[0-9a-f]{64}$")
_TEMPORARY_NAME = re.compile(r"^\.materialization_[0-9a-f]{64}\.[0-9a-f]{32}\.tmp$")
_RESOURCE_URI = re.compile(
    r"^vibecad://artifact/(materialization_[0-9a-f]{64})/"
    r"(artifact_[0-9a-f]{32})$"
)

_REQUEST_PATH_DOMAIN = b"vibecad-artifact-request-path-v1\0"
_REQUEST_INTENT_DOMAIN = b"vibecad-artifact-request-intent-v1\0"
_REQUEST_CHECKSUM_DOMAIN = b"vibecad-artifact-request-record-v1\0"
_SOURCE_DESCRIPTOR_DOMAIN = b"vibecad-artifact-source-descriptor-v1\0"
_DELIVERY_MANIFEST_DOMAIN = b"vibecad-artifact-delivery-manifest-v1\0"
_TEMP_CREATION_DOMAIN = b"vibecad-artifact-temp-creation-v1\0"
_CLEANUP_RECEIPT_PATH_DOMAIN = b"vibecad-artifact-cleanup-path-v1\0"
_CLEANUP_RECEIPT_CHECKSUM_DOMAIN = b"vibecad-artifact-cleanup-receipt-v1\0"
_LOCK_NAME = ".artifact-mutation.lock"
_CREATION_MARKER_NAME = ".creation.json"
_ORPHAN_CLEANUP_PHASE = "ORPHAN_TEMP_CLEANUP"


class ArtifactSourceKind(StrEnum):
    """The immutable task authority used for one delivery."""

    COMMITTED = "committed"
    DRAFT = "draft"


class ArtifactRequestPhase(StrEnum):
    """Durable export-request phases."""

    RESERVED = "RESERVED"
    STAGING = "STAGING"
    COPIED = "COPIED"
    VALIDATED = "VALIDATED"
    MATERIALIZED = "MATERIALIZED"
    PUBLISHED = "PUBLISHED"
    CLEANUP_REQUIRED = "CLEANUP_REQUIRED"
    REJECTED = "REJECTED"


class ArtifactApiErrorCode(StrEnum):
    """Closed public export failure taxonomy."""

    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    UNSUPPORTED_VERSION = "unsupported_version"
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"
    BUDGET_EXCEEDED = "budget_exceeded"
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    INVALID_STATE = "invalid_state"
    CONFLICT = "conflict"
    LEASE_UNAVAILABLE = "lease_unavailable"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INTEGRITY_FAILURE = "integrity_failure"
    CAD_FAILURE = "cad_failure"
    STORE_FAILURE = "store_failure"
    RECOVERY_REQUIRED = "recovery_required"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    INTERNAL_ERROR = "internal_error"


class ArtifactServiceErrorCode(StrEnum):
    """Path-free errors returned by the materialization service."""

    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    INVALID_STATE = "invalid_state"
    CONFLICT = "conflict"
    LEASE_UNAVAILABLE = "lease_unavailable"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INTEGRITY_FAILURE = "integrity_failure"
    CAD_FAILURE = "cad_failure"
    STORE_FAILURE = "store_failure"
    RECOVERY_REQUIRED = "recovery_required"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    INTERNAL_ERROR = "internal_error"


class ArtifactDependencyErrorCode(StrEnum):
    """Fixed failures accepted from task/revision dependency ports."""

    NOT_FOUND = "not_found"
    INVALID_STATE = "invalid_state"
    CONFLICT = "conflict"
    LEASE_UNAVAILABLE = "lease_unavailable"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INTEGRITY_FAILURE = "integrity_failure"
    CAD_FAILURE = "cad_failure"
    STORE_FAILURE = "store_failure"
    RECOVERY_REQUIRED = "recovery_required"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    INTERNAL_ERROR = "internal_error"


class ArtifactStoreErrorCode(StrEnum):
    """Internal durable-store failure classes."""

    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INVALID_STATE = "invalid_state"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INTEGRITY_FAILURE = "integrity_failure"
    IO_ERROR = "io_error"
    RECOVERY_REQUIRED = "recovery_required"


class ArtifactResourceErrorCode(StrEnum):
    """Sanitized low-level artifact resource errors."""

    INVALID_IDENTIFIER = "invalid_identifier"
    UNAVAILABLE = "unavailable"
    READ_LIMIT = "read_limit"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    INTERNAL_ERROR = "internal_error"


_API_MESSAGES = {
    ArtifactApiErrorCode.MISSING_FIELD: "A required request field is missing.",
    ArtifactApiErrorCode.UNKNOWN_FIELD: "The request contains an unknown field.",
    ArtifactApiErrorCode.UNSUPPORTED_VERSION: "The request schema version is not supported.",
    ArtifactApiErrorCode.INVALID_TYPE: "A request value has an invalid type.",
    ArtifactApiErrorCode.INVALID_VALUE: "A request value is invalid.",
    ArtifactApiErrorCode.BUDGET_EXCEEDED: "The request exceeds a resource budget.",
    ArtifactApiErrorCode.INVALID_INPUT: "The artifact request is invalid.",
    ArtifactApiErrorCode.NOT_FOUND: "The task or revision was not found.",
    ArtifactApiErrorCode.INVALID_STATE: "The task is not eligible for artifact export.",
    ArtifactApiErrorCode.CONFLICT: "The artifact request conflicts with durable state.",
    ArtifactApiErrorCode.LEASE_UNAVAILABLE: "The artifact export gate is unavailable.",
    ArtifactApiErrorCode.RESOURCE_EXHAUSTED: "The artifact capacity is exhausted.",
    ArtifactApiErrorCode.INTEGRITY_FAILURE: "The artifact integrity check failed.",
    ArtifactApiErrorCode.CAD_FAILURE: "The CAD artifact validation failed.",
    ArtifactApiErrorCode.STORE_FAILURE: "The artifact record operation failed.",
    ArtifactApiErrorCode.RECOVERY_REQUIRED: "The artifact request requires recovery.",
    ArtifactApiErrorCode.RUNTIME_UNAVAILABLE: "The managed CAD runtime is not active.",
    ArtifactApiErrorCode.INTERNAL_ERROR: "The request could not be completed.",
}

_RESOURCE_MESSAGES = {
    ArtifactResourceErrorCode.INVALID_IDENTIFIER: "Artifact resource identifier is invalid.",
    ArtifactResourceErrorCode.UNAVAILABLE: "Artifact resource is unavailable.",
    ArtifactResourceErrorCode.READ_LIMIT: "Artifact resource exceeds the read limit.",
    ArtifactResourceErrorCode.RUNTIME_UNAVAILABLE: "The managed CAD runtime is not active.",
    ArtifactResourceErrorCode.INTERNAL_ERROR: "Artifact resource could not be read.",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactServicePortFailure:
    """A neutral service failure carrying no exception or path text."""

    code: ArtifactServiceErrorCode

    def __post_init__(self) -> None:
        if type(self.code) is not ArtifactServiceErrorCode:
            raise TypeError("code must be an exact ArtifactServiceErrorCode")


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactDependencyFailure:
    """A neutral failure value accepted from injected dependency ports."""

    code: ArtifactDependencyErrorCode

    def __post_init__(self) -> None:
        if type(self.code) is not ArtifactDependencyErrorCode:
            raise TypeError("code must be an exact ArtifactDependencyErrorCode")


class ArtifactDependencyError(RuntimeError):
    """Fixed exception form for context-manager entry/exit failures."""

    __slots__ = ("code",)

    def __init__(self, code: ArtifactDependencyErrorCode) -> None:
        if type(code) is not ArtifactDependencyErrorCode:
            raise TypeError("code must be an exact ArtifactDependencyErrorCode")
        self.code = code
        self.args = (code.value,)


class ArtifactStoreError(ValueError):
    """Fixed, non-reflective durable artifact-store failure."""

    __slots__ = ("code",)

    def __init__(self, code: ArtifactStoreErrorCode) -> None:
        if type(code) is not ArtifactStoreErrorCode:
            raise TypeError("code must be an exact ArtifactStoreErrorCode")
        self.code = code
        self.args = (code.value,)


class ArtifactResourceError(ValueError):
    """Fixed resource failure which never reflects the submitted URI."""

    __slots__ = ("code", "message")

    def __init__(self, code: ArtifactResourceErrorCode) -> None:
        if type(code) is not ArtifactResourceErrorCode:
            raise TypeError("code must be an exact ArtifactResourceErrorCode")
        self.code = code
        self.message = _RESOURCE_MESSAGES[code]
        self.args = (self.message,)


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactExportRequest:
    """Strict, path-free export intent passed to the service core."""

    export_key: str
    task_id: str
    expected_generation: int
    revision_id: str
    draft_id: str | None

    def __post_init__(self) -> None:
        if type(self.export_key) is not str or _EXPORT_KEY.fullmatch(self.export_key) is None:
            raise ValueError("invalid export key")
        if type(self.task_id) is not str or _TASK_ID.fullmatch(self.task_id) is None:
            raise ValueError("invalid task id")
        if (
            type(self.expected_generation) is not int
            or self.expected_generation < 0
            or self.expected_generation > MAX_SAFE_JSON_INTEGER
        ):
            raise ValueError("invalid expected generation")
        if type(self.revision_id) is not str or _REVISION_ID.fullmatch(self.revision_id) is None:
            raise ValueError("invalid revision id")
        if self.draft_id is not None and (
            type(self.draft_id) is not str or _DRAFT_ID.fullmatch(self.draft_id) is None
        ):
            raise ValueError("invalid draft id")


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactManifestRequest:
    """Strict, path-free intent for observing one verified artifact manifest."""

    task_id: str
    expected_generation: int
    revision_id: str
    draft_id: str | None

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or _TASK_ID.fullmatch(self.task_id) is None:
            raise ValueError("invalid task id")
        if (
            type(self.expected_generation) is not int
            or self.expected_generation < 0
            or self.expected_generation > MAX_SAFE_JSON_INTEGER
        ):
            raise ValueError("invalid expected generation")
        if type(self.revision_id) is not str or _REVISION_ID.fullmatch(self.revision_id) is None:
            raise ValueError("invalid revision id")
        if self.draft_id is not None and (
            type(self.draft_id) is not str or _DRAFT_ID.fullmatch(self.draft_id) is None
        ):
            raise ValueError("invalid draft id")


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactEligibility:
    """One exact immutable task/revision authority snapshot."""

    source_kind: ArtifactSourceKind
    task_id: str
    task_generation: int
    project_id: str
    revision_id: str
    manifest_sha256: str
    draft_id: str | None
    artifacts: tuple[RevisionArtifactRef, RevisionArtifactRef]

    def __post_init__(self) -> None:
        if type(self.source_kind) is not ArtifactSourceKind:
            raise TypeError("source_kind must be exact")
        if type(self.task_id) is not str or _TASK_ID.fullmatch(self.task_id) is None:
            raise ValueError("task_id is invalid")
        if (
            type(self.task_generation) is not int
            or self.task_generation < 0
            or self.task_generation > MAX_SAFE_JSON_INTEGER
        ):
            raise ValueError("task_generation is invalid")
        if type(self.project_id) is not str or _PROJECT_ID.fullmatch(self.project_id) is None:
            raise ValueError("project_id is invalid")
        if type(self.revision_id) is not str or _REVISION_ID.fullmatch(self.revision_id) is None:
            raise ValueError("revision_id is invalid")
        if type(self.manifest_sha256) is not str or _DIGEST.fullmatch(self.manifest_sha256) is None:
            raise ValueError("manifest digest is invalid")
        if self.source_kind is ArtifactSourceKind.COMMITTED:
            if self.draft_id is not None:
                raise ValueError("committed authority cannot bind a draft")
        elif type(self.draft_id) is not str or _DRAFT_ID.fullmatch(self.draft_id) is None:
            raise ValueError("draft authority requires a draft id")
        if type(self.artifacts) is not tuple or len(self.artifacts) != 2:
            raise ValueError("artifacts must be the exact FCStd/STEP pair")
        if not all(type(item) is RevisionArtifactRef for item in self.artifacts):
            raise TypeError("artifact refs must be exact RevisionArtifactRef values")
        model, step = self.artifacts
        if (model.name, model.format, step.name, step.format) != (
            "model.FCStd",
            "fcstd",
            "model.step",
            "step",
        ):
            raise ValueError("artifact layout is invalid")
        if model.id == step.id:
            raise ValueError("artifact identifiers must be distinct")
        if (
            any(
                type(item.size_bytes) is not int
                or item.size_bytes <= 0
                or item.size_bytes > MAX_ARTIFACT_SOURCE_BYTES
                for item in self.artifacts
            )
            or model.size_bytes + step.size_bytes > MAX_ARTIFACT_PAIR_BYTES
        ):
            raise ValueError("artifact byte budget is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class MaterializedArtifactRef:
    """Path-free immutable delivery artifact."""

    id: str
    name: str
    format: str
    sha256: str
    size_bytes: int
    resource_uri: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactExportResult:
    """Frozen successful export result persisted in a PUBLISHED request."""

    export_key: str
    materialization_id: str
    source_kind: ArtifactSourceKind
    task_id: str
    task_generation: int
    project_id: str
    revision_id: str
    manifest_sha256: str
    authoritative: bool
    artifacts: tuple[MaterializedArtifactRef, MaterializedArtifactRef]


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactCopyCursor:
    """Exact destination prefix available to a descriptor-bound copy port."""

    name: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactResourceContent:
    """Bounded base64 resource payload independent of an MCP SDK type."""

    uri: str
    blob: str
    mime_type: str


class ArtifactServicePort(Protocol):
    """Transport-neutral verified artifact service."""

    def export_task_artifacts(
        self,
        *,
        request: ArtifactExportRequest,
    ) -> ArtifactExportResult | ArtifactServicePortFailure: ...

    def get_artifact_manifest(
        self,
        *,
        request: ArtifactManifestRequest,
    ) -> dict[str, object] | ArtifactServicePortFailure: ...


class ArtifactAuthorityPort(Protocol):
    """Task, gate, revision, and descriptor-copy capabilities."""

    def task_exists(self, *, task_id: str) -> bool | ArtifactDependencyFailure: ...

    def acquire_export_gate(self, *, task_id: str) -> AbstractContextManager[None]: ...

    def load_task(self, *, task_id: str) -> StoredTaskRun | ArtifactDependencyFailure: ...

    def load_revision(
        self, *, project_id: str, revision_id: str
    ) -> RevisionRef | ArtifactDependencyFailure: ...

    def copy_authoritative(
        self,
        *,
        eligibility: ArtifactEligibility,
        destination_directory_fd: int,
        cursors: tuple[ArtifactCopyCursor, ...],
        chunk_bytes: int,
    ) -> None | ArtifactDependencyFailure: ...


class _ApiFailure(Exception):
    __slots__ = ("code", "path")

    def __init__(self, code: ArtifactApiErrorCode, path: str = "") -> None:
        self.code = code
        self.path = path
        self.args = (code.value,)


def _api_raise(code: ArtifactApiErrorCode, path: str = "") -> None:
    raise _ApiFailure(code, path)


def _utf8_size(value: str, *, api: bool = False, path: str = "") -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError:
        if api:
            _api_raise(ArtifactApiErrorCode.INVALID_VALUE, path)
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None


def _bounded_pointer(token: str) -> str:
    escaped = token.replace("~", "~0").replace("/", "~1")
    candidate = f"/{escaped}"
    try:
        if len(candidate.encode("utf-8")) <= _MAX_ERROR_PATH_BYTES:
            return candidate
    except UnicodeError:
        pass
    return "/_truncated"


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None


def _request_mapping(value: ArtifactExportRequest) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "export_key": value.export_key,
        "task_id": value.task_id,
        "expected_generation": value.expected_generation,
        "revision_id": value.revision_id,
        "draft_id": value.draft_id,
    }


def _request_digest(value: ArtifactExportRequest) -> str:
    return hashlib.sha256(
        _REQUEST_INTENT_DOMAIN + _canonical_json(_request_mapping(value))
    ).hexdigest()


def _artifact_mapping(value: RevisionArtifactRef) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": value.id,
        "name": value.name,
        "format": value.format,
        "sha256": value.sha256,
        "size_bytes": value.size_bytes,
    }


def _descriptor_mapping(value: ArtifactEligibility) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source_kind": value.source_kind.value,
        "task_id": value.task_id,
        "task_generation": value.task_generation,
        "project_id": value.project_id,
        "revision_id": value.revision_id,
        "manifest_sha256": value.manifest_sha256,
        "draft_id": value.draft_id,
        "artifacts": [_artifact_mapping(item) for item in value.artifacts],
    }


def _materialization_id(value: ArtifactEligibility) -> str:
    suffix = hashlib.sha256(
        _SOURCE_DESCRIPTOR_DOMAIN + _canonical_json(_descriptor_mapping(value))
    ).hexdigest()
    return f"materialization_{suffix}"


def _delivery_manifest_body(value: ArtifactEligibility) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "materialization_id": _materialization_id(value),
        "source": _descriptor_mapping(value),
        "artifacts": [_artifact_mapping(item) for item in value.artifacts],
    }


def _delivery_manifest_digest(value: ArtifactEligibility) -> str:
    return hashlib.sha256(
        _DELIVERY_MANIFEST_DOMAIN + _canonical_json(_delivery_manifest_body(value))
    ).hexdigest()


def _resource_uri(materialization_id: str, artifact_id: str) -> str:
    return f"vibecad://artifact/{materialization_id}/{artifact_id}"


def _resource_incremental_allocation_bound(size_bytes: int) -> int:
    if type(size_bytes) is not int or size_bytes < 0:
        raise ArtifactResourceError(ArtifactResourceErrorCode.INTERNAL_ERROR)
    if size_bytes > MAX_ARTIFACT_RESOURCE_BYTES:
        raise ArtifactResourceError(ArtifactResourceErrorCode.READ_LIMIT)
    encoded_bytes = 4 * ((size_bytes + 2) // 3)
    if encoded_bytes > MAX_ARTIFACT_RESOURCE_BASE64_BYTES:
        raise ArtifactResourceError(ArtifactResourceErrorCode.READ_LIMIT)
    return size_bytes + 3 * encoded_bytes + _RESOURCE_ALLOCATION_OVERHEAD_BYTES


def _result(value: ArtifactExportRequest, eligibility: ArtifactEligibility) -> ArtifactExportResult:
    materialization_id = _materialization_id(eligibility)
    artifacts = tuple(
        MaterializedArtifactRef(
            id=item.id,
            name=item.name,
            format=item.format,
            sha256=item.sha256,
            size_bytes=item.size_bytes,
            resource_uri=_resource_uri(materialization_id, item.id),
        )
        for item in eligibility.artifacts
    )
    return ArtifactExportResult(
        export_key=value.export_key,
        materialization_id=materialization_id,
        source_kind=eligibility.source_kind,
        task_id=eligibility.task_id,
        task_generation=eligibility.task_generation,
        project_id=eligibility.project_id,
        revision_id=eligibility.revision_id,
        manifest_sha256=eligibility.manifest_sha256,
        authoritative=False,
        artifacts=artifacts,  # type: ignore[arg-type]
    )


def _reservation_ceiling(record: _RequestRecord) -> int:
    return (
        sum(item.size_bytes for item in record.eligibility.artifacts)
        + 2 * MAX_ARTIFACT_RECORD_BYTES
    )


def _materialized_projection(value: MaterializedArtifactRef) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": value.id,
        "name": value.name,
        "format": value.format,
        "sha256": value.sha256,
        "size_bytes": value.size_bytes,
        "resource_uri": value.resource_uri,
    }


def _result_projection(value: ArtifactExportResult) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "export_key": value.export_key,
        "materialization_id": value.materialization_id,
        "source_kind": value.source_kind.value,
        "task_id": value.task_id,
        "task_generation": value.task_generation,
        "project_id": value.project_id,
        "revision_id": value.revision_id,
        "manifest_sha256": value.manifest_sha256,
        "authoritative": value.authoritative,
        "artifacts": [_materialized_projection(item) for item in value.artifacts],
    }


def _valid_result(value: object, request: ArtifactExportRequest) -> bool:
    if type(value) is not ArtifactExportResult:
        return False
    if (
        type(value.export_key) is not str
        or _EXPORT_KEY.fullmatch(value.export_key) is None
        or type(value.materialization_id) is not str
        or _MATERIALIZATION_NAME.fullmatch(value.materialization_id) is None
        or type(value.source_kind) is not ArtifactSourceKind
        or type(value.task_id) is not str
        or _TASK_ID.fullmatch(value.task_id) is None
        or type(value.task_generation) is not int
        or not 0 <= value.task_generation <= MAX_SAFE_JSON_INTEGER
        or type(value.project_id) is not str
        or _PROJECT_ID.fullmatch(value.project_id) is None
        or type(value.revision_id) is not str
        or _REVISION_ID.fullmatch(value.revision_id) is None
        or type(value.manifest_sha256) is not str
        or _DIGEST.fullmatch(value.manifest_sha256) is None
        or type(value.authoritative) is not bool
        or value.authoritative is not False
        or type(value.artifacts) is not tuple
        or len(value.artifacts) != 2
    ):
        return False
    for item in value.artifacts:
        if (
            type(item) is not MaterializedArtifactRef
            or type(item.id) is not str
            or _ARTIFACT_ID.fullmatch(item.id) is None
            or type(item.name) is not str
            or type(item.format) is not str
            or type(item.sha256) is not str
            or _DIGEST.fullmatch(item.sha256) is None
            or type(item.size_bytes) is not int
            or not 0 < item.size_bytes <= MAX_ARTIFACT_SOURCE_BYTES
            or type(item.resource_uri) is not str
            or _RESOURCE_URI.fullmatch(item.resource_uri) is None
        ):
            return False
    if not (
        value.export_key == request.export_key
        and value.task_id == request.task_id
        and value.task_generation == request.expected_generation
        and value.revision_id == request.revision_id
    ):
        return False
    expected_draft = value.source_kind is ArtifactSourceKind.DRAFT
    if expected_draft != (request.draft_id is not None):
        return False
    for index, item in enumerate(value.artifacts):
        expected = ("model.FCStd", "fcstd") if index == 0 else ("model.step", "step")
        if (item.name, item.format) != expected or item.resource_uri != _resource_uri(
            value.materialization_id, item.id
        ):
            return False
    return (
        value.artifacts[0].id != value.artifacts[1].id
        and value.artifacts[0].size_bytes + value.artifacts[1].size_bytes <= MAX_ARTIFACT_PAIR_BYTES
    )


def _validate_api_mapping(
    value: object,
    *,
    required: frozenset[str],
) -> dict[str, object]:
    if type(value) is not dict:
        _api_raise(ArtifactApiErrorCode.INVALID_TYPE)
    keys = tuple(value)
    for key in keys:
        if type(key) is not str:
            _api_raise(ArtifactApiErrorCode.INVALID_TYPE)
        if _utf8_size(key, api=True) > _MAX_JSON_KEY_BYTES:
            _api_raise(ArtifactApiErrorCode.BUDGET_EXCEEDED)
    key_set = set(keys)
    unknown = sorted(key_set - required)
    if unknown:
        _api_raise(ArtifactApiErrorCode.UNKNOWN_FIELD, _bounded_pointer(unknown[0]))
    missing = sorted(required - key_set)
    if missing:
        _api_raise(ArtifactApiErrorCode.MISSING_FIELD, _bounded_pointer(missing[0]))

    count = 0
    seen: set[int] = set()
    stack: list[tuple[object, int, str]] = [(value, 0, "")]
    while stack:
        current, depth, path = stack.pop()
        count += 1
        if count > _MAX_REQUEST_NODES or depth > _MAX_REQUEST_DEPTH:
            _api_raise(ArtifactApiErrorCode.BUDGET_EXCEEDED, path)
        if current is None or type(current) is bool:
            continue
        if type(current) is int:
            if abs(current) > MAX_SAFE_JSON_INTEGER:
                _api_raise(ArtifactApiErrorCode.INVALID_VALUE, path)
            continue
        if type(current) is float:
            if not math.isfinite(current):
                _api_raise(ArtifactApiErrorCode.INVALID_VALUE, path)
            continue
        if type(current) is str:
            if _utf8_size(current, api=True, path=path) > _MAX_REQUEST_STRING_BYTES:
                _api_raise(ArtifactApiErrorCode.BUDGET_EXCEEDED, path)
            continue
        if type(current) not in {dict, list}:
            _api_raise(ArtifactApiErrorCode.INVALID_TYPE, path)
        identity = id(current)
        if identity in seen:
            _api_raise(ArtifactApiErrorCode.INVALID_VALUE, path)
        seen.add(identity)
        if type(current) is list:
            stack.extend((item, depth + 1, path) for item in reversed(current))
        else:
            for key, item in reversed(tuple(current.items())):
                if type(key) is not str:
                    _api_raise(ArtifactApiErrorCode.INVALID_TYPE, path)
                if _utf8_size(key, api=True, path=path) > _MAX_JSON_KEY_BYTES:
                    _api_raise(ArtifactApiErrorCode.BUDGET_EXCEEDED, path)
                stack.append((item, depth + 1, _bounded_pointer(key)))
    try:
        if len(_canonical_json(value)) > _MAX_REQUEST_BYTES:
            _api_raise(ArtifactApiErrorCode.BUDGET_EXCEEDED)
    except ArtifactStoreError:
        _api_raise(ArtifactApiErrorCode.INVALID_VALUE)

    version = value["schema_version"]
    if type(version) is not int:
        _api_raise(ArtifactApiErrorCode.INVALID_TYPE, "/schema_version")
    if version != SCHEMA_VERSION:
        _api_raise(ArtifactApiErrorCode.UNSUPPORTED_VERSION, "/schema_version")
    return value


def _validate_api_request(value: object) -> ArtifactExportRequest:
    data = _validate_api_mapping(
        value,
        required=frozenset(
            {
                "schema_version",
                "export_key",
                "task_id",
                "expected_generation",
                "revision_id",
                "draft_id",
            }
        ),
    )
    fields = (
        ("export_key", _EXPORT_KEY),
        ("task_id", _TASK_ID),
        ("revision_id", _REVISION_ID),
    )
    for name, pattern in fields:
        item = data[name]
        if type(item) is not str:
            _api_raise(ArtifactApiErrorCode.INVALID_TYPE, f"/{name}")
        if pattern.fullmatch(item) is None:
            _api_raise(ArtifactApiErrorCode.INVALID_VALUE, f"/{name}")
    generation = data["expected_generation"]
    if type(generation) is not int:
        _api_raise(ArtifactApiErrorCode.INVALID_TYPE, "/expected_generation")
    if generation < 0 or generation > MAX_SAFE_JSON_INTEGER:
        _api_raise(ArtifactApiErrorCode.INVALID_VALUE, "/expected_generation")
    draft = data["draft_id"]
    if draft is not None:
        if type(draft) is not str:
            _api_raise(ArtifactApiErrorCode.INVALID_TYPE, "/draft_id")
        if _DRAFT_ID.fullmatch(draft) is None:
            _api_raise(ArtifactApiErrorCode.INVALID_VALUE, "/draft_id")
    return ArtifactExportRequest(
        export_key=data["export_key"],
        task_id=data["task_id"],
        expected_generation=generation,
        revision_id=data["revision_id"],
        draft_id=draft,
    )


def _validate_manifest_api_request(value: object) -> ArtifactManifestRequest:
    data = _validate_api_mapping(
        value,
        required=frozenset(
            {
                "schema_version",
                "task_id",
                "expected_generation",
                "revision_id",
                "draft_id",
            }
        ),
    )
    for name, pattern in (("task_id", _TASK_ID), ("revision_id", _REVISION_ID)):
        item = data[name]
        if type(item) is not str:
            _api_raise(ArtifactApiErrorCode.INVALID_TYPE, f"/{name}")
        if pattern.fullmatch(item) is None:
            _api_raise(ArtifactApiErrorCode.INVALID_VALUE, f"/{name}")
    generation = data["expected_generation"]
    if type(generation) is not int:
        _api_raise(ArtifactApiErrorCode.INVALID_TYPE, "/expected_generation")
    if generation < 0 or generation > MAX_SAFE_JSON_INTEGER:
        _api_raise(ArtifactApiErrorCode.INVALID_VALUE, "/expected_generation")
    draft = data["draft_id"]
    if draft is not None:
        if type(draft) is not str:
            _api_raise(ArtifactApiErrorCode.INVALID_TYPE, "/draft_id")
        if _DRAFT_ID.fullmatch(draft) is None:
            _api_raise(ArtifactApiErrorCode.INVALID_VALUE, "/draft_id")
    return ArtifactManifestRequest(
        task_id=data["task_id"],
        expected_generation=generation,
        revision_id=data["revision_id"],
        draft_id=draft,
    )


def _valid_manifest_artifact(
    value: object,
    *,
    index: int,
    materialized: bool,
    materialization_id: str | None,
) -> bool:
    if type(value) is not dict or set(value) != {
        "schema_version",
        "id",
        "name",
        "format",
        "sha256",
        "size_bytes",
        "resource_uri",
    }:
        return False
    expected_name, expected_format = (
        ("model.FCStd", "fcstd") if index == 0 else ("model.step", "step")
    )
    resource_uri = value["resource_uri"]
    if materialized:
        if (
            type(resource_uri) is not str
            or type(materialization_id) is not str
            or resource_uri
            != _resource_uri(
                materialization_id,
                value["id"] if type(value["id"]) is str else "",
            )
        ):
            return False
    elif resource_uri is not None:
        return False
    return (
        type(value["schema_version"]) is int
        and value["schema_version"] == SCHEMA_VERSION
        and type(value["id"]) is str
        and _ARTIFACT_ID.fullmatch(value["id"]) is not None
        and type(value["name"]) is str
        and value["name"] == expected_name
        and type(value["format"]) is str
        and value["format"] == expected_format
        and type(value["sha256"]) is str
        and _DIGEST.fullmatch(value["sha256"]) is not None
        and type(value["size_bytes"]) is int
        and 0 < value["size_bytes"] <= MAX_ARTIFACT_SOURCE_BYTES
    )


def _valid_manifest_result(value: object, request: ArtifactManifestRequest) -> bool:
    if type(value) is not dict or set(value) != {
        "source_kind",
        "task_id",
        "task_generation",
        "project_id",
        "revision_id",
        "draft_id",
        "manifest_sha256",
        "verification_id",
        "acceptance_id",
        "verification_digest",
        "observation_digest",
        "materialized",
        "materialization_id",
        "delivery_manifest_sha256",
        "artifacts",
    }:
        return False
    source_kind = value["source_kind"]
    materialized = value["materialized"]
    materialization_id = value["materialization_id"]
    delivery_digest = value["delivery_manifest_sha256"]
    acceptance_id = value["acceptance_id"]
    artifacts = value["artifacts"]
    if not (
        source_kind in {ArtifactSourceKind.COMMITTED.value, ArtifactSourceKind.DRAFT.value}
        and type(value["task_id"]) is str
        and _TASK_ID.fullmatch(value["task_id"]) is not None
        and value["task_id"] == request.task_id
        and type(value["task_generation"]) is int
        and value["task_generation"] == request.expected_generation
        and type(value["project_id"]) is str
        and _PROJECT_ID.fullmatch(value["project_id"]) is not None
        and type(value["revision_id"]) is str
        and _REVISION_ID.fullmatch(value["revision_id"]) is not None
        and value["revision_id"] == request.revision_id
        and (
            value["draft_id"] is None
            or (
                type(value["draft_id"]) is str
                and _DRAFT_ID.fullmatch(value["draft_id"]) is not None
            )
        )
        and value["draft_id"] == request.draft_id
        and (source_kind == ArtifactSourceKind.DRAFT.value) == (request.draft_id is not None)
        and type(value["manifest_sha256"]) is str
        and _DIGEST.fullmatch(value["manifest_sha256"]) is not None
        and type(value["verification_id"]) is str
        and _VERIFICATION_ID.fullmatch(value["verification_id"]) is not None
        and type(acceptance_id) is str
        and 0 < len(acceptance_id) <= 256
        and bool(acceptance_id.strip())
        and acceptance_id.isprintable()
        and len(acceptance_id.splitlines()) == 1
        and type(value["verification_digest"]) is str
        and _DIGEST.fullmatch(value["verification_digest"]) is not None
        and type(value["observation_digest"]) is str
        and _DIGEST.fullmatch(value["observation_digest"]) is not None
        and type(materialized) is bool
        and type(artifacts) is list
        and len(artifacts) == 2
    ):
        return False
    if materialized:
        if not (
            type(materialization_id) is str
            and _MATERIALIZATION_NAME.fullmatch(materialization_id) is not None
            and type(delivery_digest) is str
            and _DIGEST.fullmatch(delivery_digest) is not None
        ):
            return False
    elif materialization_id is not None or delivery_digest is not None:
        return False
    if not all(
        _valid_manifest_artifact(
            item,
            index=index,
            materialized=materialized,
            materialization_id=materialization_id,
        )
        for index, item in enumerate(artifacts)
    ):
        return False
    return (
        artifacts[0]["id"] != artifacts[1]["id"]
        and artifacts[0]["size_bytes"] + artifacts[1]["size_bytes"] <= MAX_ARTIFACT_PAIR_BYTES
    )


def _manifest_result_projection(value: dict[str, object]) -> dict[str, object]:
    return {"schema_version": SCHEMA_VERSION, **value}


class ArtifactApi:
    """Strict public envelope adapter over one verified artifact service."""

    __slots__ = ("_port",)

    def __init__(self, *, port: ArtifactServicePort) -> None:
        self._port = port

    def export_task_artifacts(self, request: object) -> dict[str, object]:
        try:
            parsed = _validate_api_request(request)
            try:
                value = self._port.export_task_artifacts(request=parsed)
            except Exception:
                _api_raise(ArtifactApiErrorCode.INTERNAL_ERROR)
            if type(value) is ArtifactServicePortFailure:
                if type(value.code) is not ArtifactServiceErrorCode:
                    _api_raise(ArtifactApiErrorCode.INTERNAL_ERROR)
                _api_raise(ArtifactApiErrorCode(value.code.value))
            if not _valid_result(value, parsed):
                _api_raise(ArtifactApiErrorCode.INTERNAL_ERROR)
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "result": _result_projection(value),
                "error": None,
            }
        except _ApiFailure as error:
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "result": None,
                "error": {
                    "schema_version": SCHEMA_VERSION,
                    "code": error.code.value,
                    "path": error.path,
                    "message": _API_MESSAGES[error.code],
                },
            }
        except Exception:
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "result": None,
                "error": {
                    "schema_version": SCHEMA_VERSION,
                    "code": ArtifactApiErrorCode.INTERNAL_ERROR.value,
                    "path": "",
                    "message": _API_MESSAGES[ArtifactApiErrorCode.INTERNAL_ERROR],
                },
            }

    def get_artifact_manifest(self, request: object) -> dict[str, object]:
        try:
            parsed = _validate_manifest_api_request(request)
            try:
                value = self._port.get_artifact_manifest(request=parsed)
            except Exception:
                _api_raise(ArtifactApiErrorCode.INTERNAL_ERROR)
            if type(value) is ArtifactServicePortFailure:
                if type(value.code) is not ArtifactServiceErrorCode:
                    _api_raise(ArtifactApiErrorCode.INTERNAL_ERROR)
                _api_raise(ArtifactApiErrorCode(value.code.value))
            if not _valid_manifest_result(value, parsed):
                _api_raise(ArtifactApiErrorCode.INTERNAL_ERROR)
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "result": _manifest_result_projection(value),
                "error": None,
            }
        except _ApiFailure as error:
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "result": None,
                "error": {
                    "schema_version": SCHEMA_VERSION,
                    "code": error.code.value,
                    "path": error.path,
                    "message": _API_MESSAGES[error.code],
                },
            }
        except Exception:
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "result": None,
                "error": {
                    "schema_version": SCHEMA_VERSION,
                    "code": ArtifactApiErrorCode.INTERNAL_ERROR.value,
                    "path": "",
                    "message": _API_MESSAGES[ArtifactApiErrorCode.INTERNAL_ERROR],
                },
            }


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    dev: int
    ino: int
    uid: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class _DirectoryBinding:
    dev: int
    ino: int
    uid: int
    mode: int
    nlink: int


@dataclass(frozen=True, slots=True)
class _CleanupReceipt:
    phase: str
    temporary_name: str
    directory: _DirectoryBinding
    marker_sha256: str
    created_ns: int


@dataclass(frozen=True, slots=True)
class _RequestRecord:
    phase: ArtifactRequestPhase
    export_key: str
    request_digest: str
    eligibility: ArtifactEligibility
    materialization_id: str
    delivery_manifest_sha256: str
    temporary_name: str
    temporary_identity: _FileIdentity | None = None
    copied: tuple[_FileIdentity, _FileIdentity] | None = None
    validation: ValidatedMaterializationEvidence | None = None
    materialized_identity: _FileIdentity | None = None
    response: ArtifactExportResult | None = None
    failure_code: ArtifactServiceErrorCode | None = None


@dataclass(frozen=True, slots=True)
class _StoreInventory:
    ordinary_bytes: int
    committed_bytes: int
    requests: int
    materializations: int
    temporaries: int


_thread_lock_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _thread_lock_guard:
        return _thread_locks.setdefault(key, threading.RLock())


def _identity(value: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        dev=value.st_dev,
        ino=value.st_ino,
        uid=value.st_uid,
        mode=stat.S_IMODE(value.st_mode),
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        ctime_ns=value.st_ctime_ns,
    )


def _directory_binding(value: os.stat_result) -> _DirectoryBinding:
    return _DirectoryBinding(
        dev=value.st_dev,
        ino=value.st_ino,
        uid=value.st_uid,
        mode=stat.S_IMODE(value.st_mode),
        nlink=value.st_nlink,
    )


def _same_cleanup_binding(value: os.stat_result, expected: _DirectoryBinding) -> bool:
    return _directory_binding(value) == expected


def _same_cleanup_object(value: os.stat_result, expected: _DirectoryBinding) -> bool:
    return (
        value.st_dev == expected.dev
        and value.st_ino == expected.ino
        and value.st_uid == expected.uid
        and stat.S_IMODE(value.st_mode) == expected.mode
    )


def _same_cleanup_empty_binding(
    value: os.stat_result,
    expected: _DirectoryBinding,
) -> bool:
    return _same_cleanup_object(value, expected) and value.st_nlink in {
        expected.nlink,
        expected.nlink - 1,
    }


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return _identity(left) == _identity(right)


def _same_directory_binding(value: os.stat_result, expected: _FileIdentity) -> bool:
    return (
        value.st_dev == expected.dev
        and value.st_ino == expected.ino
        and value.st_uid == expected.uid
        and stat.S_IMODE(value.st_mode) == expected.mode
    )


def _private_directory(value: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(value.st_mode)
        and value.st_uid == os.geteuid()
        and stat.S_IMODE(value.st_mode) == 0o700
    )


def _private_file(value: os.stat_result, *, allow_empty: bool = True) -> bool:
    return (
        stat.S_ISREG(value.st_mode)
        and value.st_uid == os.geteuid()
        and value.st_nlink == 1
        and stat.S_IMODE(value.st_mode) == 0o600
        and (allow_empty or value.st_size > 0)
    )


def _fsync_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError:
        raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None


def _rename_directory_noreplace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    """Atomically rename one directory without ever replacing a destination."""

    try:
        import ctypes  # noqa: PLC0415

        source = source_name.encode("ascii")
        destination = destination_name.encode("ascii")
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
                source_parent_fd,
                source,
                destination_parent_fd,
                destination,
                0x00000004,  # RENAME_EXCL
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
                source_parent_fd,
                source,
                destination_parent_fd,
                destination,
                0x00000001,  # RENAME_NOREPLACE
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


def _darwin_thread_fchdir(fd: int) -> None:
    """Set or clear only the calling thread's Darwin working directory."""

    if sys.platform != "darwin" or type(fd) is not int:
        raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
    try:
        import ctypes  # noqa: PLC0415

        library = ctypes.CDLL(None, use_errno=True)
        operation = library.pthread_fchdir_np
        operation.argtypes = [ctypes.c_int]
        operation.restype = ctypes.c_int
        ctypes.set_errno(0)
        if operation(fd) != 0:
            raise OSError
    except (AttributeError, OSError):
        raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None


def _current_directory_matches(
    expected: _FileIdentity,
    poison: Callable[[], None],
) -> bool:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        owner = _OwnedWorkingFd(os.open(".", flags), poison)
        with owner as fd:
            current = os.fstat(fd)
            return _same_directory_binding(current, expected) and _private_directory(current)
    except OSError:
        return False


def _validate_cad_from_pinned_directory(
    cad: CadExecutionPort,
    directory_fd: int,
    expected: _FileIdentity,
    poison: Callable[[], None],
) -> ValidatedMaterializationEvidence:
    """Call CAD with fixed relative names bound to one open private directory."""

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    previous_owner: _OwnedWorkingFd | None = None
    previous_fd = -1
    previous_identity: _FileIdentity | None = None
    entered = False
    primary: BaseException | None = None
    result: ValidatedMaterializationEvidence | None = None
    restore_failed = False
    try:
        if not _same_directory_binding(os.fstat(directory_fd), expected):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        previous_owner = _OwnedWorkingFd(os.open(".", flags), poison)
        previous_fd = previous_owner.__enter__()
        previous_identity = _identity(os.fstat(previous_fd))
        _darwin_thread_fchdir(directory_fd)
        entered = True
        if not _current_directory_matches(expected, poison):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        try:
            result = cad.validate_materialization(
                fcstd=Path("model.FCStd"),
                step=Path("model.step"),
            )
        except BaseException as error:
            primary = error
        if primary is None and (
            not _current_directory_matches(expected, poison)
            or not _same_directory_binding(os.fstat(directory_fd), expected)
        ):
            primary = ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    except BaseException as error:
        primary = error
    finally:
        if entered:
            try:
                _darwin_thread_fchdir(-1)
            except ArtifactStoreError:
                restore_failed = True
        close_failed = False
        if previous_owner is not None:
            try:
                current_owner = _OwnedWorkingFd(os.open(".", flags), poison)
                with current_owner as current_fd:
                    current = _identity(os.fstat(current_fd))
                    if current != previous_identity:
                        restore_failed = True
            except ArtifactStoreError:
                close_failed = True
            except OSError:
                restore_failed = True
            try:
                previous_owner.close(primary)
            except ArtifactStoreError:
                close_failed = True
        if close_failed:
            restore_failed = True
    if restore_failed:
        raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from primary
    if primary is not None:
        raise primary.with_traceback(primary.__traceback__)
    if type(result) is not ValidatedMaterializationEvidence:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    return result


def _parse_json(raw: bytes) -> object:
    def duplicate_checked(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def integer(value: str) -> int:
        digits = value.removeprefix("-")
        if len(digits) > 16:
            raise ValueError
        return int(value)

    try:
        return json.loads(
            raw,
            object_pairs_hook=duplicate_checked,
            parse_int=integer,
            parse_float=lambda value: (_ for _ in ()).throw(ValueError()),
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, RecursionError, json.JSONDecodeError):
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None


def _record_envelope(record: _RequestRecord) -> bytes:
    body = _record_body(record)
    body_bytes = _canonical_json(body)
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "body": body,
        "body_sha256": hashlib.sha256(_REQUEST_CHECKSUM_DOMAIN + body_bytes).hexdigest(),
    }
    raw = _canonical_json(envelope)
    if len(raw) > MAX_ARTIFACT_RECORD_BYTES:
        raise ArtifactStoreError(ArtifactStoreErrorCode.RESOURCE_EXHAUSTED)
    return raw


def _identity_mapping(value: _FileIdentity | None) -> object:
    if value is None:
        return None
    return {
        "dev": str(value.dev),
        "ino": str(value.ino),
        "uid": str(value.uid),
        "mode": str(value.mode),
        "size": str(value.size),
        "mtime_ns": str(value.mtime_ns),
        "ctime_ns": str(value.ctime_ns),
    }


def _validation_mapping(value: ValidatedMaterializationEvidence | None) -> object:
    if value is None:
        return None
    return {
        "fcstd_sha256": value.fcstd_sha256,
        "fcstd_size_bytes": value.fcstd_size_bytes,
        "step_sha256": value.step_sha256,
        "step_size_bytes": value.step_size_bytes,
    }


def _record_body(record: _RequestRecord) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "phase": record.phase.value,
        "export_key": record.export_key,
        "request_digest": record.request_digest,
        "source": _descriptor_mapping(record.eligibility),
        "materialization_id": record.materialization_id,
        "delivery_manifest_sha256": record.delivery_manifest_sha256,
        "temporary_name": record.temporary_name,
        "temporary_identity": _identity_mapping(record.temporary_identity),
        "copied": None
        if record.copied is None
        else [_identity_mapping(item) for item in record.copied],
        "validation": _validation_mapping(record.validation),
        "materialized_identity": _identity_mapping(record.materialized_identity),
        "response": None if record.response is None else _result_projection(record.response),
        "failure_code": None if record.failure_code is None else record.failure_code.value,
    }


def _exact_keys(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    return value


def _creation_marker_envelope(
    temporary_name: str,
    created_ns: int,
    identity: _FileIdentity,
) -> bytes:
    body = {
        "schema_version": SCHEMA_VERSION,
        "temporary_name": temporary_name,
        "created_ns": str(created_ns),
        "identity": {
            "dev": str(identity.dev),
            "ino": str(identity.ino),
            "uid": str(identity.uid),
            "mode": str(identity.mode),
        },
    }
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "body": body,
        "body_sha256": hashlib.sha256(_TEMP_CREATION_DOMAIN + _canonical_json(body)).hexdigest(),
    }
    raw = _canonical_json(envelope)
    if len(raw) > MAX_ARTIFACT_RECORD_BYTES:
        raise ArtifactStoreError(ArtifactStoreErrorCode.RESOURCE_EXHAUSTED)
    return raw


def _parse_creation_marker(
    raw: bytes,
    temporary_name: str,
    identity: _FileIdentity,
) -> int:
    envelope = _exact_keys(_parse_json(raw), {"schema_version", "body", "body_sha256"})
    body = _exact_keys(
        envelope["body"],
        {"schema_version", "temporary_name", "created_ns", "identity"},
    )
    stable = _exact_keys(body["identity"], {"dev", "ino", "uid", "mode"})
    values = tuple(stable[name] for name in ("dev", "ino", "uid", "mode"))
    created = body["created_ns"]
    if (
        envelope["schema_version"] != SCHEMA_VERSION
        or body["schema_version"] != SCHEMA_VERSION
        or body["temporary_name"] != temporary_name
        or type(created) is not str
        or not created.isascii()
        or not created.isdecimal()
        or len(created) > 20
        or any(
            type(item) is not str or not item.isascii() or not item.isdecimal() or len(item) > 20
            for item in values
        )
        or tuple(int(item) for item in values)
        != (identity.dev, identity.ino, identity.uid, identity.mode)
        or type(envelope["body_sha256"]) is not str
        or envelope["body_sha256"]
        != hashlib.sha256(_TEMP_CREATION_DOMAIN + _canonical_json(body)).hexdigest()
    ):
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    return int(created)


def _cleanup_receipt_name(temporary_name: str) -> str:
    suffix = hashlib.sha256(
        _CLEANUP_RECEIPT_PATH_DOMAIN + temporary_name.encode("ascii")
    ).hexdigest()
    return f"cleanup_{suffix}.json"


def _cleanup_receipt_target_name(temporary_name: str) -> str:
    if _CLEANUP_RECEIPT_TEMP_NAME.fullmatch(temporary_name) is None:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    return temporary_name[1:].rsplit(".", 2)[0]


def _cleanup_receipt_envelope(receipt: _CleanupReceipt) -> bytes:
    body = {
        "schema_version": SCHEMA_VERSION,
        "phase": receipt.phase,
        "temporary_name": receipt.temporary_name,
        "directory": {
            "dev": str(receipt.directory.dev),
            "ino": str(receipt.directory.ino),
            "uid": str(receipt.directory.uid),
            "mode": str(receipt.directory.mode),
            "nlink": str(receipt.directory.nlink),
        },
        "marker_sha256": receipt.marker_sha256,
        "created_ns": str(receipt.created_ns),
    }
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "body": body,
        "body_sha256": hashlib.sha256(
            _CLEANUP_RECEIPT_CHECKSUM_DOMAIN + _canonical_json(body)
        ).hexdigest(),
    }
    raw = _canonical_json(envelope)
    if len(raw) > MAX_ARTIFACT_RECORD_BYTES:
        raise ArtifactStoreError(ArtifactStoreErrorCode.RESOURCE_EXHAUSTED)
    return raw


def _parse_cleanup_receipt(raw: bytes) -> _CleanupReceipt:
    envelope = _exact_keys(_parse_json(raw), {"schema_version", "body", "body_sha256"})
    body = _exact_keys(
        envelope["body"],
        {
            "schema_version",
            "phase",
            "temporary_name",
            "directory",
            "marker_sha256",
            "created_ns",
        },
    )
    directory = _exact_keys(body["directory"], {"dev", "ino", "uid", "mode", "nlink"})
    raw_directory = tuple(directory[name] for name in ("dev", "ino", "uid", "mode", "nlink"))
    created = body["created_ns"]
    if (
        envelope["schema_version"] != SCHEMA_VERSION
        or body["schema_version"] != SCHEMA_VERSION
        or body["phase"] != _ORPHAN_CLEANUP_PHASE
        or type(body["temporary_name"]) is not str
        or _TEMPORARY_NAME.fullmatch(body["temporary_name"]) is None
        or type(body["marker_sha256"]) is not str
        or _DIGEST.fullmatch(body["marker_sha256"]) is None
        or type(created) is not str
        or not created.isascii()
        or not created.isdecimal()
        or len(created) > 20
        or any(
            type(item) is not str or not item.isascii() or not item.isdecimal() or len(item) > 20
            for item in raw_directory
        )
        or type(envelope["body_sha256"]) is not str
        or envelope["body_sha256"]
        != hashlib.sha256(_CLEANUP_RECEIPT_CHECKSUM_DOMAIN + _canonical_json(body)).hexdigest()
    ):
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    values = tuple(int(item) for item in raw_directory)
    if int(created) <= 0 or values[2] != os.geteuid() or values[3] != 0o700 or values[4] < 1:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    return _CleanupReceipt(
        phase=_ORPHAN_CLEANUP_PHASE,
        temporary_name=body["temporary_name"],
        directory=_DirectoryBinding(*values),
        marker_sha256=body["marker_sha256"],
        created_ns=int(created),
    )


def _parse_identity(value: object, *, optional: bool) -> _FileIdentity | None:
    if value is None and optional:
        return None
    data = _exact_keys(value, {"dev", "ino", "uid", "mode", "size", "mtime_ns", "ctime_ns"})
    raw_values = tuple(
        data[name] for name in ("dev", "ino", "uid", "mode", "size", "mtime_ns", "ctime_ns")
    )
    if any(
        type(item) is not str or not item.isascii() or not item.isdecimal() or len(item) > 20
        for item in raw_values
    ):
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    values = tuple(int(item) for item in raw_values)
    return _FileIdentity(*values)


def _parse_artifact(value: object) -> RevisionArtifactRef:
    data = _exact_keys(
        value,
        {"schema_version", "id", "name", "format", "sha256", "size_bytes"},
    )
    try:
        result = RevisionArtifactRef(**data)
    except Exception:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None
    return result


def _parse_eligibility(value: object) -> ArtifactEligibility:
    data = _exact_keys(
        value,
        {
            "schema_version",
            "source_kind",
            "task_id",
            "task_generation",
            "project_id",
            "revision_id",
            "manifest_sha256",
            "draft_id",
            "artifacts",
        },
    )
    if data["schema_version"] != SCHEMA_VERSION or type(data["artifacts"]) is not list:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    try:
        artifacts = tuple(_parse_artifact(item) for item in data["artifacts"])
        return ArtifactEligibility(
            source_kind=ArtifactSourceKind(data["source_kind"]),
            task_id=data["task_id"],
            task_generation=data["task_generation"],
            project_id=data["project_id"],
            revision_id=data["revision_id"],
            manifest_sha256=data["manifest_sha256"],
            draft_id=data["draft_id"],
            artifacts=artifacts,  # type: ignore[arg-type]
        )
    except Exception:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None


def _parse_materialized(value: object) -> MaterializedArtifactRef:
    data = _exact_keys(
        value,
        {"schema_version", "id", "name", "format", "sha256", "size_bytes", "resource_uri"},
    )
    if data.pop("schema_version") != SCHEMA_VERSION:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    try:
        return MaterializedArtifactRef(**data)
    except Exception:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None


def _parse_result(value: object) -> ArtifactExportResult:
    data = _exact_keys(
        value,
        {
            "schema_version",
            "export_key",
            "materialization_id",
            "source_kind",
            "task_id",
            "task_generation",
            "project_id",
            "revision_id",
            "manifest_sha256",
            "authoritative",
            "artifacts",
        },
    )
    if data["schema_version"] != SCHEMA_VERSION or type(data["artifacts"]) is not list:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    try:
        artifacts = tuple(_parse_materialized(item) for item in data["artifacts"])
        return ArtifactExportResult(
            export_key=data["export_key"],
            materialization_id=data["materialization_id"],
            source_kind=ArtifactSourceKind(data["source_kind"]),
            task_id=data["task_id"],
            task_generation=data["task_generation"],
            project_id=data["project_id"],
            revision_id=data["revision_id"],
            manifest_sha256=data["manifest_sha256"],
            authoritative=data["authoritative"],
            artifacts=artifacts,  # type: ignore[arg-type]
        )
    except Exception:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None


def _parse_validation(value: object) -> ValidatedMaterializationEvidence | None:
    if value is None:
        return None
    data = _exact_keys(
        value,
        {"fcstd_sha256", "fcstd_size_bytes", "step_sha256", "step_size_bytes"},
    )
    try:
        return ValidatedMaterializationEvidence(**data)
    except Exception:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None


def _parse_record(raw: bytes) -> _RequestRecord:
    if not raw or len(raw) > MAX_ARTIFACT_RECORD_BYTES:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    envelope = _exact_keys(_parse_json(raw), {"schema_version", "body", "body_sha256"})
    if envelope["schema_version"] != SCHEMA_VERSION:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    body = _exact_keys(
        envelope["body"],
        {
            "schema_version",
            "phase",
            "export_key",
            "request_digest",
            "source",
            "materialization_id",
            "delivery_manifest_sha256",
            "temporary_name",
            "temporary_identity",
            "copied",
            "validation",
            "materialized_identity",
            "response",
            "failure_code",
        },
    )
    expected = hashlib.sha256(_REQUEST_CHECKSUM_DOMAIN + _canonical_json(body)).hexdigest()
    if envelope["body_sha256"] != expected or body["schema_version"] != SCHEMA_VERSION:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    copied_raw = body["copied"]
    if copied_raw is None:
        copied = None
    elif type(copied_raw) is list and len(copied_raw) == 2:
        copied = tuple(_parse_identity(item, optional=False) for item in copied_raw)
    else:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    try:
        record = _RequestRecord(
            phase=ArtifactRequestPhase(body["phase"]),
            export_key=body["export_key"],
            request_digest=body["request_digest"],
            eligibility=_parse_eligibility(body["source"]),
            materialization_id=body["materialization_id"],
            delivery_manifest_sha256=body["delivery_manifest_sha256"],
            temporary_name=body["temporary_name"],
            temporary_identity=_parse_identity(body["temporary_identity"], optional=True),
            copied=copied,  # type: ignore[arg-type]
            validation=_parse_validation(body["validation"]),
            materialized_identity=_parse_identity(body["materialized_identity"], optional=True),
            response=None if body["response"] is None else _parse_result(body["response"]),
            failure_code=None
            if body["failure_code"] is None
            else ArtifactServiceErrorCode(body["failure_code"]),
        )
    except ArtifactStoreError:
        raise
    except Exception:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None
    if not (
        _EXPORT_KEY.fullmatch(record.export_key)
        and _DIGEST.fullmatch(record.request_digest)
        and _MATERIALIZATION_NAME.fullmatch(record.materialization_id)
        and _DIGEST.fullmatch(record.delivery_manifest_sha256)
        and _TEMPORARY_NAME.fullmatch(record.temporary_name)
        and record.materialization_id == _materialization_id(record.eligibility)
        and record.delivery_manifest_sha256 == _delivery_manifest_digest(record.eligibility)
    ):
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    phase = record.phase
    if phase is ArtifactRequestPhase.RESERVED and any(
        item is not None
        for item in (
            record.temporary_identity,
            record.copied,
            record.validation,
            record.materialized_identity,
            record.response,
            record.failure_code,
        )
    ):
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    if phase in {
        ArtifactRequestPhase.STAGING,
        ArtifactRequestPhase.COPIED,
        ArtifactRequestPhase.VALIDATED,
    }:
        if record.temporary_identity is None:
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    if (
        phase in {ArtifactRequestPhase.COPIED, ArtifactRequestPhase.VALIDATED}
        and record.copied is None
    ):
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    if phase is ArtifactRequestPhase.VALIDATED and record.validation is None:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    if phase in {ArtifactRequestPhase.MATERIALIZED, ArtifactRequestPhase.PUBLISHED} and (
        record.materialized_identity is None
    ):
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    if phase is ArtifactRequestPhase.PUBLISHED and record.response is None:
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    if phase in {ArtifactRequestPhase.CLEANUP_REQUIRED, ArtifactRequestPhase.REJECTED} and (
        record.failure_code is None
    ):
        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
    return record


class _MutationLock:
    __slots__ = ("_fd", "_lock", "_poison", "_root_check", "_root_fd")

    def __init__(
        self,
        root_fd: int,
        lock_key: str,
        root_check: Callable[[], None],
        poison: Callable[[], None],
    ) -> None:
        self._root_fd = root_fd
        self._root_check = root_check
        self._poison = poison
        self._lock = _thread_lock(Path(lock_key))
        self._fd = -1

    def __enter__(self) -> _MutationLock:
        if not self._lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
        try:
            self._root_check()
            flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            before = os.stat(_LOCK_NAME, dir_fd=self._root_fd, follow_symlinks=False)
            self._fd = os.open(_LOCK_NAME, flags, dir_fd=self._root_fd)
            opened = os.fstat(self._fd)
            if not _private_file(opened) or not _same_identity(before, opened):
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None
                    time.sleep(0.01)
            self._root_check()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, traceback) -> bool:
        failed = False
        if self._fd >= 0:
            fd = self._fd
            self._fd = -1
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                failed = True
            try:
                os.close(fd)
            except OSError:
                failed = True
        try:
            self._lock.release()
        except RuntimeError:
            failed = True
        if failed:
            self._poison()
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from exc
        return False


class _OwnedWorkingFd:
    """Retire one process-local working descriptor exactly once."""

    __slots__ = ("_fd", "_poison")

    def __init__(self, fd: int, poison: Callable[[], None]) -> None:
        self._fd = fd
        self._poison = poison

    def __enter__(self) -> int:
        return self._fd

    def release(self) -> int:
        fd = self._fd
        self._fd = -1
        return fd

    def close(self, primary: BaseException | None = None) -> None:
        fd = self._fd
        self._fd = -1
        if fd < 0:
            return
        try:
            os.close(fd)
        except OSError:
            self._poison()
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from primary

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close(exc)
        return False


class ArtifactStore:
    """Checksummed durable export catalog and immutable copy-out store."""

    __slots__ = (
        "_root",
        "_requests",
        "_materializations",
        "_lock_path",
        "_root_fd",
        "_requests_fd",
        "_materializations_fd",
        "_root_identity",
        "_requests_identity",
        "_materializations_identity",
        "_pid",
        "_poisoned",
        "_closed",
        "_close_failed",
    )

    def __init__(
        self,
        *,
        root: Path,
        expected_root_identity: tuple[int, int] | None = None,
    ) -> None:
        if (
            not isinstance(root, Path)
            or not root.is_absolute()
            or (
                expected_root_identity is not None
                and (
                    type(expected_root_identity) is not tuple
                    or len(expected_root_identity) != 2
                    or not all(type(item) is int and item >= 0 for item in expected_root_identity)
                )
            )
        ):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INVALID_INPUT)
        self._root = root
        self._requests = root / "requests"
        self._materializations = root / "materializations"
        self._lock_path = root / _LOCK_NAME
        self._pid = os.getpid()
        self._poisoned = False
        self._closed = False
        self._close_failed = False
        self._root_fd = -1
        self._requests_fd = -1
        self._materializations_fd = -1
        try:
            if expected_root_identity is None:
                created = not root.exists()
                root.mkdir(mode=0o700, parents=True, exist_ok=True)
                if created:
                    os.chmod(root, 0o700)
            root_value = os.lstat(root)
            if not _private_directory(root_value):
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_DIRECTORY", 0)
            )
            self._root_fd = os.open(root, directory_flags)
            opened_root = os.fstat(self._root_fd)
            if not _same_identity(root_value, opened_root) or (
                expected_root_identity is not None
                and (opened_root.st_dev, opened_root.st_ino) != expected_root_identity
            ):
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            for name in ("requests", "materializations"):
                try:
                    os.mkdir(name, 0o700, dir_fd=self._root_fd)
                except FileExistsError:
                    pass
                value = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
                if not _private_directory(value):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            self._requests_fd = os.open(
                "requests",
                directory_flags,
                dir_fd=self._root_fd,
            )
            self._materializations_fd = os.open(
                "materializations",
                directory_flags,
                dir_fd=self._root_fd,
            )
            flags = (
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            owner = _OwnedWorkingFd(
                os.open(_LOCK_NAME, flags, 0o600, dir_fd=self._root_fd),
                self._poison,
            )
            with owner as fd:
                os.fchmod(fd, 0o600)
                os.fsync(fd)
            _fsync_fd(self._root_fd)
            self._root_identity = _identity(os.fstat(self._root_fd))
            self._requests_identity = _identity(os.fstat(self._requests_fd))
            self._materializations_identity = _identity(os.fstat(self._materializations_fd))
            self._check_roots()
        except BaseException as error:
            cleanup_failed = self._close_owned_descriptors()
            self._closed = True
            self._close_failed = cleanup_failed
            if cleanup_failed:
                self._poison()
                if isinstance(error, Exception):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from error
            if isinstance(error, ArtifactStoreError):
                raise
            if isinstance(error, OSError):
                raise ArtifactStoreError(ArtifactStoreErrorCode.IO_ERROR) from None
            raise

    @property
    def root(self) -> Path:
        """Return the private store root for composition, never for public projection."""

        return self._root

    def _close_owned_descriptors(self) -> bool:
        failed = False
        for attribute in ("_materializations_fd", "_requests_fd", "_root_fd"):
            fd = getattr(self, attribute)
            if fd < 0:
                continue
            # A failed close has indeterminate ownership.  Retire the numeric
            # descriptor before the syscall and never retry it after another
            # thread could have reused that number.
            setattr(self, attribute, -1)
            try:
                os.close(fd)
            except OSError:
                failed = True
        return failed

    def close(self) -> None:
        """Close this process-owned store exactly once.

        Successful close is idempotent.  A wrong-process call or any uncertain
        descriptor/lock release poisons the instance and reports recovery
        rather than retrying an indeterminate numeric descriptor.
        """

        if os.getpid() != self._pid:
            self._poison()
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
        lock = _thread_lock(self._lock_path)
        if not lock.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            self._poison()
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
        failed = False
        try:
            if self._closed:
                failed = self._close_failed
            else:
                self._closed = True
                failed = self._close_owned_descriptors()
        finally:
            try:
                lock.release()
            except RuntimeError:
                failed = True
        if failed:
            self._close_failed = True
            self._poison()
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)

    def _check_roots(self) -> None:
        if os.getpid() != self._pid or self._poisoned or self._closed:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
        try:
            values = (
                (os.lstat(self._root), os.fstat(self._root_fd), self._root_identity),
                (
                    os.lstat(self._requests),
                    os.fstat(self._requests_fd),
                    self._requests_identity,
                ),
                (
                    os.lstat(self._materializations),
                    os.fstat(self._materializations_fd),
                    self._materializations_identity,
                ),
            )
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None
        if any(
            not _private_directory(alias)
            or not _private_directory(opened)
            or not _same_directory_binding(alias, expected)
            or not _same_directory_binding(opened, expected)
            for alias, opened, expected in values
        ):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)

    def _poison(self) -> None:
        self._poisoned = True

    def _lock(self) -> _MutationLock:
        return _MutationLock(
            self._root_fd,
            str(self._lock_path),
            self._check_roots,
            self._poison,
        )

    def _request_name(self, export_key: str) -> str:
        if type(export_key) is not str or _EXPORT_KEY.fullmatch(export_key) is None:
            raise ArtifactStoreError(ArtifactStoreErrorCode.INVALID_INPUT)
        name = hashlib.sha256(_REQUEST_PATH_DOMAIN + export_key.encode("ascii")).hexdigest()
        return f"{name}.json"

    def _read_file_at(self, directory_fd: int, name: str, limit: int) -> bytes:
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not _private_file(before) or before.st_size <= 0 or before.st_size > limit:
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            owner = _OwnedWorkingFd(
                os.open(name, flags, dir_fd=directory_fd),
                self._poison,
            )
            with owner as fd:
                opened = os.fstat(fd)
                if not _same_identity(before, opened):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                chunks = []
                remaining = opened.st_size
                while remaining:
                    chunk = os.read(fd, min(ARTIFACT_COPY_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if not _same_identity(opened, os.fstat(fd)):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            if not _same_identity(
                before,
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False),
            ):
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            return b"".join(chunks)
        except ArtifactStoreError:
            raise
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.IO_ERROR) from None

    def _read_record_name(self, name: str) -> _RequestRecord:
        return _parse_record(self._read_file_at(self._requests_fd, name, MAX_ARTIFACT_RECORD_BYTES))

    def _read_record(self, export_key: str) -> _RequestRecord | None:
        name = self._request_name(export_key)
        try:
            return self._read_record_name(name)
        except ArtifactStoreError as error:
            if error.code is not ArtifactStoreErrorCode.IO_ERROR:
                raise
            try:
                os.stat(name, dir_fd=self._requests_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            except OSError:
                pass
            raise

    def _validate_request_remnant(
        self,
        name: str,
        expected: _FileIdentity,
        record: _RequestRecord,
        now_ns: int,
    ) -> None:
        target = name[1 : 1 + len(self._request_name(record.export_key))]
        if target != self._request_name(record.export_key):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        if expected.mtime_ns > now_ns:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
        if now_ns - expected.mtime_ns <= ABANDONED_ARTIFACT_TEMP_TTL_SECONDS * 1_000_000_000:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
        current = os.stat(name, dir_fd=self._requests_fd, follow_symlinks=False)
        if _identity(current) != expected:
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)

    def _cleanup_request_remnant(
        self,
        name: str,
        expected: _FileIdentity,
        record: _RequestRecord,
        now_ns: int,
    ) -> None:
        self._validate_request_remnant(name, expected, record, now_ns)
        os.unlink(name, dir_fd=self._requests_fd)
        _fsync_fd(self._requests_fd)

    def _inspect_unbound_temporary(
        self,
        name: str,
        expected: _FileIdentity,
        now_ns: int,
    ) -> tuple[_CleanupReceipt, int]:
        owner = _OwnedWorkingFd(
            self._open_named_directory(self._root_fd, name, expected),
            self._poison,
        )
        with owner as directory_fd:
            names = {entry.name for entry in os.scandir(directory_fd)}
            if names != {_CREATION_MARKER_NAME}:
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            raw = self._read_file_at(
                directory_fd,
                _CREATION_MARKER_NAME,
                MAX_ARTIFACT_RECORD_BYTES,
            )
            created_ns = _parse_creation_marker(raw, name, expected)
            if created_ns > now_ns:
                raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
            if now_ns - created_ns <= ABANDONED_ARTIFACT_TEMP_TTL_SECONDS * 1_000_000_000:
                raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
            marker = os.stat(
                _CREATION_MARKER_NAME,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if not _private_file(marker, allow_empty=False):
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        current = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        if not _same_directory_binding(current, expected) or not _private_directory(current):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        return (
            _CleanupReceipt(
                phase=_ORPHAN_CLEANUP_PHASE,
                temporary_name=name,
                directory=_directory_binding(current),
                marker_sha256=hashlib.sha256(raw).hexdigest(),
                created_ns=created_ns,
            ),
            len(raw),
        )

    def _read_cleanup_receipt_name(self, name: str) -> _CleanupReceipt:
        receipt = _parse_cleanup_receipt(
            self._read_file_at(self._root_fd, name, MAX_ARTIFACT_RECORD_BYTES)
        )
        if name != _cleanup_receipt_name(receipt.temporary_name):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        return receipt

    def _delete_root_file_exact(
        self,
        name: str,
        expected: _FileIdentity,
        *,
        allow_empty: bool = False,
    ) -> None:
        try:
            current = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
            if _identity(current) != expected or not _private_file(
                current,
                allow_empty=allow_empty,
            ):
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            os.unlink(name, dir_fd=self._root_fd)
            _fsync_fd(self._root_fd)
        except ArtifactStoreError:
            raise
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None

    def _inspect_receipt_bound_temporary(
        self,
        name: str,
        expected: _FileIdentity,
        receipt: _CleanupReceipt,
    ) -> int:
        current = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        if (
            not _private_directory(current)
            or not _same_directory_binding(current, expected)
            or not _same_cleanup_object(current, receipt.directory)
        ):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        owner = _OwnedWorkingFd(
            self._open_named_directory(self._root_fd, name, expected),
            self._poison,
        )
        marker_size = 0
        with owner as directory_fd:
            opened = os.fstat(directory_fd)
            names = {entry.name for entry in os.scandir(directory_fd)}
            if names == {_CREATION_MARKER_NAME}:
                if not _same_cleanup_binding(opened, receipt.directory):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                raw = self._read_file_at(
                    directory_fd,
                    _CREATION_MARKER_NAME,
                    MAX_ARTIFACT_RECORD_BYTES,
                )
                if (
                    hashlib.sha256(raw).hexdigest() != receipt.marker_sha256
                    or _parse_creation_marker(raw, name, expected) != receipt.created_ns
                ):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                marker_size = len(raw)
            elif names:
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            elif not _same_cleanup_empty_binding(opened, receipt.directory):
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        current = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        if marker_size:
            valid_binding = _same_cleanup_binding(current, receipt.directory)
        else:
            valid_binding = _same_cleanup_empty_binding(current, receipt.directory)
        if not valid_binding:
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        return marker_size

    def _publish_cleanup_receipt(self, receipt: _CleanupReceipt) -> str:
        name = _cleanup_receipt_name(receipt.temporary_name)
        try:
            existing = self._read_cleanup_receipt_name(name)
        except ArtifactStoreError as error:
            if error.code is not ArtifactStoreErrorCode.IO_ERROR:
                raise
            try:
                os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise
        else:
            if existing != receipt:
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            return name
        raw = _cleanup_receipt_envelope(receipt)
        temporary = f".{name}.{secrets.token_hex(16)}.tmp"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            owner = _OwnedWorkingFd(
                os.open(temporary, flags, 0o600, dir_fd=self._root_fd),
                self._poison,
            )
            with owner as fd:
                os.fchmod(fd, 0o600)
                offset = 0
                while offset < len(raw):
                    written = os.write(fd, raw[offset:])
                    if written <= 0:
                        raise OSError
                    offset += written
                os.fsync(fd)
            temporary_identity = _identity(
                os.stat(temporary, dir_fd=self._root_fd, follow_symlinks=False)
            )
            try:
                _rename_directory_noreplace(
                    self._root_fd,
                    temporary,
                    self._root_fd,
                    name,
                )
            except FileExistsError:
                if self._read_cleanup_receipt_name(name) != receipt:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None
                self._delete_root_file_exact(temporary, temporary_identity)
            _fsync_fd(self._root_fd)
            return name
        except ArtifactStoreError:
            raise
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None

    def _resume_cleanup_receipt(
        self,
        name: str,
        expected: _FileIdentity,
        receipt: _CleanupReceipt,
        now_ns: int,
    ) -> None:
        current_receipt = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        if _identity(current_receipt) != expected or not _private_file(
            current_receipt,
            allow_empty=False,
        ):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        if self._read_cleanup_receipt_name(name) != receipt:
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        if _identity(os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)) != expected:
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        if receipt.created_ns > now_ns or (
            now_ns - receipt.created_ns <= ABANDONED_ARTIFACT_TEMP_TTL_SECONDS * 1_000_000_000
        ):
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
        try:
            current = os.stat(
                receipt.temporary_name,
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            current = None
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None
        if current is not None:
            if not _private_directory(current) or not _same_cleanup_object(
                current,
                receipt.directory,
            ):
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            owner = _OwnedWorkingFd(
                self._open_named_directory(
                    self._root_fd,
                    receipt.temporary_name,
                    _identity(current),
                ),
                self._poison,
            )
            try:
                with owner as directory_fd:
                    opened = os.fstat(directory_fd)
                    names = {entry.name for entry in os.scandir(directory_fd)}
                    if names == {_CREATION_MARKER_NAME}:
                        if not _same_cleanup_binding(opened, receipt.directory):
                            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                        raw = self._read_file_at(
                            directory_fd,
                            _CREATION_MARKER_NAME,
                            MAX_ARTIFACT_RECORD_BYTES,
                        )
                        if (
                            hashlib.sha256(raw).hexdigest() != receipt.marker_sha256
                            or _parse_creation_marker(
                                raw,
                                receipt.temporary_name,
                                _identity(current),
                            )
                            != receipt.created_ns
                        ):
                            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                        os.unlink(_CREATION_MARKER_NAME, dir_fd=directory_fd)
                        _fsync_fd(directory_fd)
                    elif names:
                        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                    elif not _same_cleanup_empty_binding(opened, receipt.directory):
                        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                current = os.stat(
                    receipt.temporary_name,
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
                if not _same_cleanup_empty_binding(current, receipt.directory):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                os.rmdir(receipt.temporary_name, dir_fd=self._root_fd)
                _fsync_fd(self._root_fd)
            except ArtifactStoreError:
                raise
            except OSError:
                raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None
        else:
            _fsync_fd(self._root_fd)
        self._delete_root_file_exact(name, expected)

    def _recover_cleanup_receipt_temporary(
        self,
        name: str,
        expected: _FileIdentity,
        receipt: _CleanupReceipt,
        published: dict[str, tuple[_FileIdentity, _CleanupReceipt]],
        now_ns: int,
    ) -> None:
        final_name = _cleanup_receipt_name(receipt.temporary_name)
        if not name.startswith(f".{final_name}."):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        existing = published.get(final_name)
        if existing is not None:
            if existing[1] != receipt:
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            self._delete_root_file_exact(name, expected)
            return
        if receipt.created_ns > now_ns or (
            now_ns - receipt.created_ns <= ABANDONED_ARTIFACT_TEMP_TTL_SECONDS * 1_000_000_000
        ):
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
        try:
            current = os.stat(
                receipt.temporary_name,
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None
        if not _private_directory(current) or not _same_cleanup_binding(current, receipt.directory):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        owner = _OwnedWorkingFd(
            self._open_named_directory(
                self._root_fd,
                receipt.temporary_name,
                _identity(current),
            ),
            self._poison,
        )
        with owner as directory_fd:
            names = {entry.name for entry in os.scandir(directory_fd)}
            if names != {_CREATION_MARKER_NAME}:
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            raw = self._read_file_at(
                directory_fd,
                _CREATION_MARKER_NAME,
                MAX_ARTIFACT_RECORD_BYTES,
            )
            if (
                hashlib.sha256(raw).hexdigest() != receipt.marker_sha256
                or _parse_creation_marker(
                    raw,
                    receipt.temporary_name,
                    _identity(current),
                )
                != receipt.created_ns
            ):
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        self._delete_root_file_exact(name, expected)

    @staticmethod
    def _inventory_from_scan(
        *,
        ordinary_bytes: int,
        requests: int,
        materializations: int,
        temporaries: int,
        records: list[_RequestRecord],
        record_sizes: dict[str, int],
        temporary_sizes: dict[str, int],
        materialization_sizes: dict[str, int],
    ) -> _StoreInventory:
        unreserved_bytes = ordinary_bytes
        active_ceilings = 0
        published_materializations = {
            record.materialization_id
            for record in records
            if record.phase is ArtifactRequestPhase.PUBLISHED
        }
        claimed_materializations: set[str] = set()
        for record in records:
            if record.phase in {ArtifactRequestPhase.PUBLISHED, ArtifactRequestPhase.REJECTED}:
                continue
            ceiling = _reservation_ceiling(record)
            temporary_observed = temporary_sizes.get(record.temporary_name, 0)
            observed = record_sizes[record.export_key] + temporary_observed
            if (
                temporary_observed == 0
                and record.materialization_id not in published_materializations
                and record.materialization_id not in claimed_materializations
            ):
                materialized_observed = materialization_sizes.get(record.materialization_id, 0)
                observed += materialized_observed
                if materialized_observed:
                    claimed_materializations.add(record.materialization_id)
            if observed > ceiling:
                raise ArtifactStoreError(ArtifactStoreErrorCode.RESOURCE_EXHAUSTED)
            unreserved_bytes -= observed
            active_ceilings += ceiling
        if unreserved_bytes < 0:
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        inventory = _StoreInventory(
            ordinary_bytes=ordinary_bytes,
            committed_bytes=unreserved_bytes + active_ceilings,
            requests=requests,
            materializations=materializations,
            temporaries=temporaries,
        )
        if (
            inventory.ordinary_bytes > MAX_ARTIFACT_STORE_BYTES
            or inventory.committed_bytes > MAX_ARTIFACT_STORE_BYTES
            or inventory.requests > MAX_ARTIFACT_REQUESTS
            or inventory.materializations > MAX_ARTIFACT_MATERIALIZATIONS
            or inventory.temporaries > MAX_ARTIFACT_TEMPORARIES
        ):
            raise ArtifactStoreError(ArtifactStoreErrorCode.RESOURCE_EXHAUSTED)
        return inventory

    def _scan_locked(self) -> _StoreInventory:
        self._check_roots()
        ordinary_bytes = 0
        requests = 0
        materializations = 0
        temporaries = 0
        records: list[_RequestRecord] = []
        record_sizes: dict[str, int] = {}
        remnants: list[tuple[str, _FileIdentity, _RequestRecord]] = []
        temporary_sizes: dict[str, int] = {}
        materialization_sizes: dict[str, int] = {}
        cleanup_receipts: dict[str, tuple[_FileIdentity, _CleanupReceipt]] = {}
        cleanup_receipt_temporaries: list[tuple[str, _FileIdentity, _CleanupReceipt]] = []
        partial_cleanup_receipt_temporaries: list[tuple[str, _FileIdentity, str]] = []
        unbound_temporaries: dict[str, tuple[_FileIdentity, _CleanupReceipt]] = {}
        cleanup_receipt_directory_sizes: dict[str, int] = {}
        unbound_temporary_sizes: dict[str, int] = {}
        now_ns = time.time_ns()
        try:
            request_entries = tuple(os.scandir(self._requests_fd))
            for entry in request_entries:
                if entry.is_symlink():
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                value = entry.stat(follow_symlinks=False)
                if not _private_file(value):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                if _REQUEST_NAME.fullmatch(entry.name):
                    requests += 1
                    record = self._read_record_name(entry.name)
                    if entry.name != self._request_name(record.export_key):
                        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                    records.append(record)
                    record_sizes[record.export_key] = value.st_size
                    ordinary_bytes += value.st_size
                elif _REQUEST_TEMP_NAME.fullmatch(entry.name):
                    raw = self._read_file_at(
                        self._requests_fd,
                        entry.name,
                        MAX_ARTIFACT_RECORD_BYTES,
                    )
                    remnants.append((entry.name, _identity(value), _parse_record(raw)))
                    ordinary_bytes += value.st_size
                    temporaries += 1
                else:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            temporary_names = [record.temporary_name for record in records]
            if len(temporary_names) != len(set(temporary_names)):
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            for name, identity, record in remnants:
                self._validate_request_remnant(name, identity, record, now_ns)
            for entry in os.scandir(self._materializations_fd):
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                value = entry.stat(follow_symlinks=False)
                if (
                    not _private_directory(value)
                    or _MATERIALIZATION_NAME.fullmatch(entry.name) is None
                ):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                materializations += 1
                owner = _OwnedWorkingFd(
                    self._open_named_directory(
                        self._materializations_fd,
                        entry.name,
                        _identity(value),
                    ),
                    self._poison,
                )
                with owner as directory_fd:
                    size = self._scan_artifact_directory(
                        directory_fd,
                        temporary=False,
                    )
                materialization_sizes[entry.name] = size
                ordinary_bytes += size
            bound_temporaries = set(temporary_names)
            root_directories: list[tuple[str, _FileIdentity]] = []
            for entry in tuple(os.scandir(self._root_fd)):
                if entry.name in {"requests", "materializations", _LOCK_NAME}:
                    if entry.name == _LOCK_NAME:
                        value = entry.stat(follow_symlinks=False)
                        if entry.is_symlink() or not _private_file(value):
                            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                        ordinary_bytes += value.st_size
                    continue
                value = entry.stat(follow_symlinks=False)
                identity = _identity(value)
                if _CLEANUP_RECEIPT_NAME.fullmatch(entry.name) is not None:
                    if entry.is_symlink() or not _private_file(value, allow_empty=False):
                        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                    receipt = self._read_cleanup_receipt_name(entry.name)
                    if (
                        _identity(
                            os.stat(
                                entry.name,
                                dir_fd=self._root_fd,
                                follow_symlinks=False,
                            )
                        )
                        != identity
                    ):
                        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                    cleanup_receipts[entry.name] = (identity, receipt)
                    ordinary_bytes += value.st_size
                    temporaries += 1
                    continue
                if _CLEANUP_RECEIPT_TEMP_NAME.fullmatch(entry.name) is not None:
                    if (
                        entry.is_symlink()
                        or not _private_file(value)
                        or value.st_size > MAX_ARTIFACT_RECORD_BYTES
                    ):
                        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                    target = _cleanup_receipt_target_name(entry.name)
                    ordinary_bytes += value.st_size
                    temporaries += 1
                    if value.st_size == 0:
                        partial_cleanup_receipt_temporaries.append((entry.name, identity, target))
                        continue
                    raw = self._read_file_at(
                        self._root_fd,
                        entry.name,
                        MAX_ARTIFACT_RECORD_BYTES,
                    )
                    if (
                        _identity(
                            os.stat(
                                entry.name,
                                dir_fd=self._root_fd,
                                follow_symlinks=False,
                            )
                        )
                        != identity
                    ):
                        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                    try:
                        receipt = _parse_cleanup_receipt(raw)
                    except ArtifactStoreError as error:
                        if error.code is not ArtifactStoreErrorCode.INTEGRITY_FAILURE:
                            raise
                        partial_cleanup_receipt_temporaries.append((entry.name, identity, target))
                    else:
                        cleanup_receipt_temporaries.append((entry.name, identity, receipt))
                    continue
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                if not _private_directory(value) or _TEMPORARY_NAME.fullmatch(entry.name) is None:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                root_directories.append((entry.name, identity))

            receipts_by_temporary: dict[str, tuple[str, _FileIdentity, _CleanupReceipt]] = {}
            for receipt_name, (identity, receipt) in cleanup_receipts.items():
                if receipt.temporary_name in receipts_by_temporary:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                if receipt.created_ns > now_ns or (
                    now_ns - receipt.created_ns
                    <= ABANDONED_ARTIFACT_TEMP_TTL_SECONDS * 1_000_000_000
                ):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
                receipts_by_temporary[receipt.temporary_name] = (
                    receipt_name,
                    identity,
                    receipt,
                )
            receipt_temporary_bodies: dict[str, _CleanupReceipt] = {}
            for name, _identity_value, receipt in cleanup_receipt_temporaries:
                target = _cleanup_receipt_target_name(name)
                if target != _cleanup_receipt_name(receipt.temporary_name):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                if receipt.created_ns > now_ns or (
                    now_ns - receipt.created_ns
                    <= ABANDONED_ARTIFACT_TEMP_TTL_SECONDS * 1_000_000_000
                ):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
                prior = receipt_temporary_bodies.get(target)
                if prior is not None and prior != receipt:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                published = cleanup_receipts.get(target)
                if published is not None and published[1] != receipt:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                receipt_temporary_bodies[target] = receipt

            partial_targets = {
                target for _name, _identity_value, target in partial_cleanup_receipt_temporaries
            }
            for name, identity in root_directories:
                cleanup_name = _cleanup_receipt_name(name)
                if name not in bound_temporaries:
                    published = cleanup_receipts.get(cleanup_name)
                    if published is not None:
                        if published[1].temporary_name != name:
                            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                        marker_size = self._inspect_receipt_bound_temporary(
                            name,
                            identity,
                            published[1],
                        )
                        cleanup_receipt_directory_sizes[name] = marker_size
                        ordinary_bytes += marker_size
                        temporaries += 1
                        continue
                    receipt, size = self._inspect_unbound_temporary(
                        name,
                        identity,
                        now_ns,
                    )
                    staged_receipt = receipt_temporary_bodies.get(cleanup_name)
                    if staged_receipt is not None and staged_receipt != receipt:
                        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                    unbound_temporaries[name] = (identity, receipt)
                    unbound_temporary_sizes[name] = size
                    ordinary_bytes += size
                    temporaries += 1
                    continue
                if (
                    cleanup_name in cleanup_receipts
                    or cleanup_name in receipt_temporary_bodies
                    or cleanup_name in partial_targets
                ):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                temporaries += 1
                owner = _OwnedWorkingFd(
                    self._open_named_directory(
                        self._root_fd,
                        name,
                        identity,
                    ),
                    self._poison,
                )
                with owner as directory_fd:
                    size = self._scan_artifact_directory(
                        directory_fd,
                        temporary=True,
                    )
                temporary_sizes[name] = size
                ordinary_bytes += size

            for target, receipt in receipt_temporary_bodies.items():
                if target not in cleanup_receipts and (
                    receipt.temporary_name not in unbound_temporaries
                ):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            for _name, _identity_value, target in partial_cleanup_receipt_temporaries:
                if target not in cleanup_receipts and not any(
                    _cleanup_receipt_name(name) == target for name in unbound_temporaries
                ):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)

            inventory = self._inventory_from_scan(
                ordinary_bytes=ordinary_bytes,
                requests=requests,
                materializations=materializations,
                temporaries=temporaries,
                records=records,
                record_sizes=record_sizes,
                temporary_sizes=temporary_sizes,
                materialization_sizes=materialization_sizes,
            )
            admission_bytes = inventory.ordinary_bytes
            admission_committed = inventory.committed_bytes
            admission_temporaries = inventory.temporaries
            reclaimed_bytes = (
                sum(identity.size for _name, identity, _record in remnants)
                + sum(identity.size for _name, identity, _receipt in cleanup_receipt_temporaries)
                + sum(
                    identity.size
                    for _name, identity, _target in partial_cleanup_receipt_temporaries
                )
                + sum(identity.size for identity, _receipt in cleanup_receipts.values())
                + sum(cleanup_receipt_directory_sizes.values())
            )
            reclaimed_temporaries = (
                len(remnants)
                + len(cleanup_receipt_temporaries)
                + len(partial_cleanup_receipt_temporaries)
                + len(cleanup_receipts)
                + len(cleanup_receipt_directory_sizes)
            )
            admission_bytes -= reclaimed_bytes
            admission_committed -= reclaimed_bytes
            admission_temporaries -= reclaimed_temporaries
            if admission_bytes < 0 or admission_committed < 0 or admission_temporaries < 0:
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            for temporary_name, (_identity_value, receipt) in unbound_temporaries.items():
                receipt_size = len(_cleanup_receipt_envelope(receipt))
                race_peak_bytes = admission_bytes + 2 * receipt_size
                race_peak_committed = admission_committed + 2 * receipt_size
                race_peak_temporaries = admission_temporaries + 2
                if (
                    race_peak_bytes > MAX_ARTIFACT_STORE_BYTES
                    or race_peak_committed > MAX_ARTIFACT_STORE_BYTES
                    or race_peak_temporaries > MAX_ARTIFACT_TEMPORARIES
                ):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.RESOURCE_EXHAUSTED)
                admission_bytes -= unbound_temporary_sizes[temporary_name]
                admission_committed -= unbound_temporary_sizes[temporary_name]
                admission_temporaries -= 1

            cleanup_performed = False
            for name, identity, record in remnants:
                self._cleanup_request_remnant(name, identity, record, now_ns)
                cleanup_performed = True
            for name, identity, receipt in cleanup_receipt_temporaries:
                self._recover_cleanup_receipt_temporary(
                    name,
                    identity,
                    receipt,
                    cleanup_receipts,
                    now_ns,
                )
                cleanup_performed = True
            for name, identity, _target in partial_cleanup_receipt_temporaries:
                self._delete_root_file_exact(name, identity, allow_empty=True)
                cleanup_performed = True
            for name, (identity, receipt) in cleanup_receipts.items():
                self._resume_cleanup_receipt(name, identity, receipt, now_ns)
                cleanup_performed = True
            for _temporary_name, (_identity_value, receipt) in unbound_temporaries.items():
                receipt_name = self._publish_cleanup_receipt(receipt)
                try:
                    receipt_value = os.stat(
                        receipt_name,
                        dir_fd=self._root_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None
                if not _private_file(receipt_value, allow_empty=False):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                receipt_identity = _identity(receipt_value)
                if self._read_cleanup_receipt_name(receipt_name) != receipt:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                self._resume_cleanup_receipt(
                    receipt_name,
                    receipt_identity,
                    receipt,
                    now_ns,
                )
                cleanup_performed = True
            if cleanup_performed:
                return self._scan_locked()
        except ArtifactStoreError:
            raise
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.IO_ERROR) from None
        return inventory

    def _open_named_directory(
        self,
        parent_fd: int,
        name: str,
        expected: _FileIdentity,
    ) -> int:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            owner = _OwnedWorkingFd(
                os.open(name, flags, dir_fd=parent_fd),
                self._poison,
            )
            fd = owner.__enter__()
            try:
                opened = os.fstat(fd)
                if (
                    not _private_directory(opened)
                    or not _same_directory_binding(before, expected)
                    or not _same_directory_binding(opened, expected)
                ):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            except BaseException as error:
                owner.close(error)
                raise
            return owner.release()
        except ArtifactStoreError:
            raise
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None

    @staticmethod
    def _scan_artifact_directory(directory_fd: int, *, temporary: bool) -> int:
        total = 0
        try:
            for entry in os.scandir(directory_fd):
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                value = entry.stat(follow_symlinks=False)
                if not _private_file(value) or entry.name not in {
                    "model.FCStd",
                    "model.step",
                    "manifest.json",
                    *({_CREATION_MARKER_NAME} if temporary else set()),
                }:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                limit = (
                    MAX_ARTIFACT_RECORD_BYTES
                    if entry.name in {"manifest.json", _CREATION_MARKER_NAME}
                    else MAX_ARTIFACT_SOURCE_BYTES
                )
                if (not temporary and value.st_size <= 0) or value.st_size > limit:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                total += value.st_size
        except ArtifactStoreError:
            raise
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.IO_ERROR) from None
        return total

    def _write_record(self, record: _RequestRecord) -> _RequestRecord:
        raw = _record_envelope(record)
        name = self._request_name(record.export_key)
        inventory = self._scan_locked()
        existing = self._read_record(record.export_key)
        has_reserved_record_peak = existing is not None and existing.phase not in {
            ArtifactRequestPhase.PUBLISHED,
            ArtifactRequestPhase.REJECTED,
        }
        additional_peak = 0 if has_reserved_record_peak else len(raw)
        if inventory.committed_bytes + additional_peak > MAX_ARTIFACT_STORE_BYTES:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RESOURCE_EXHAUSTED)
        temporary = f".{name}.{secrets.token_hex(16)}.tmp"
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            owner = _OwnedWorkingFd(
                os.open(temporary, flags, 0o600, dir_fd=self._requests_fd),
                self._poison,
            )
            with owner as fd:
                os.fchmod(fd, 0o600)
                offset = 0
                while offset < len(raw):
                    written = os.write(fd, raw[offset : offset + ARTIFACT_COPY_CHUNK_BYTES])
                    if written <= 0:
                        raise OSError
                    offset += written
                os.fsync(fd)
            os.replace(
                temporary,
                name,
                src_dir_fd=self._requests_fd,
                dst_dir_fd=self._requests_fd,
            )
            _fsync_fd(self._requests_fd)
            self._check_roots()
            persisted = self._read_record_name(name)
            if persisted != record:
                raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
            return persisted
        except ArtifactStoreError:
            raise
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None
        finally:
            try:
                os.unlink(temporary, dir_fd=self._requests_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def lookup_terminal(
        self,
        *,
        request: ArtifactExportRequest,
    ) -> ArtifactExportResult | ArtifactServicePortFailure | None:
        if type(request) is not ArtifactExportRequest:
            raise ArtifactStoreError(ArtifactStoreErrorCode.INVALID_INPUT)
        with self._lock():
            self._scan_locked()
            record = self._read_record(request.export_key)
            if record is None:
                return None
            if record.request_digest != _request_digest(request):
                return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.CONFLICT)
            if record.phase is ArtifactRequestPhase.PUBLISHED:
                assert record.response is not None
                self._validate_materialized(record)
                return record.response
            if record.phase is ArtifactRequestPhase.REJECTED:
                assert record.failure_code is not None
                return ArtifactServicePortFailure(code=record.failure_code)
            return None

    def _reserve(
        self,
        request: ArtifactExportRequest,
        eligibility: ArtifactEligibility,
    ) -> _RequestRecord:
        existing = self._read_record(request.export_key)
        digest = _request_digest(request)
        if existing is not None:
            if existing.request_digest != digest or existing.eligibility != eligibility:
                raise ArtifactStoreError(ArtifactStoreErrorCode.CONFLICT)
            return existing
        inventory = self._scan_locked()
        if inventory.requests >= MAX_ARTIFACT_REQUESTS:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RESOURCE_EXHAUSTED)
        materialization_id = _materialization_id(eligibility)
        if inventory.temporaries >= MAX_ARTIFACT_TEMPORARIES:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RESOURCE_EXHAUSTED)
        try:
            existing_materialization = os.stat(
                materialization_id,
                dir_fd=self._materializations_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if inventory.materializations >= MAX_ARTIFACT_MATERIALIZATIONS:
                raise ArtifactStoreError(ArtifactStoreErrorCode.RESOURCE_EXHAUSTED) from None
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.IO_ERROR) from None
        else:
            if not _private_directory(existing_materialization):
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        record = _RequestRecord(
            phase=ArtifactRequestPhase.RESERVED,
            export_key=request.export_key,
            request_digest=digest,
            eligibility=eligibility,
            materialization_id=materialization_id,
            delivery_manifest_sha256=_delivery_manifest_digest(eligibility),
            temporary_name=f".{materialization_id}.{secrets.token_hex(16)}.tmp",
        )
        reservation_peak = _reservation_ceiling(record)
        if inventory.committed_bytes + reservation_peak > MAX_ARTIFACT_STORE_BYTES:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RESOURCE_EXHAUSTED)
        return self._write_record(record)

    def _write_creation_marker(
        self,
        directory_fd: int,
        record: _RequestRecord,
        identity: _FileIdentity,
    ) -> None:
        raw = _creation_marker_envelope(record.temporary_name, time.time_ns(), identity)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            owner = _OwnedWorkingFd(
                os.open(_CREATION_MARKER_NAME, flags, 0o600, dir_fd=directory_fd),
                self._poison,
            )
            with owner as fd:
                os.fchmod(fd, 0o600)
                offset = 0
                while offset < len(raw):
                    written = os.write(fd, raw[offset:])
                    if written <= 0:
                        raise OSError
                    offset += written
                os.fsync(fd)
            _fsync_fd(directory_fd)
        except ArtifactStoreError:
            raise
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None

    def _inspect_reserved_temporary(
        self,
        record: _RequestRecord,
    ) -> tuple[_FileIdentity, bool] | None:
        try:
            value = os.stat(
                record.temporary_name,
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None
        identity = _identity(value)
        if not _private_directory(value):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        owner = _OwnedWorkingFd(
            self._open_named_directory(self._root_fd, record.temporary_name, identity),
            self._poison,
        )
        with owner as directory_fd:
            names = {entry.name for entry in os.scandir(directory_fd)}
            if not names <= {_CREATION_MARKER_NAME}:
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            has_marker = _CREATION_MARKER_NAME in names
            if has_marker:
                raw = self._read_file_at(
                    directory_fd,
                    _CREATION_MARKER_NAME,
                    MAX_ARTIFACT_RECORD_BYTES,
                )
                created_ns = _parse_creation_marker(raw, record.temporary_name, identity)
                if created_ns > time.time_ns():
                    raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
        return identity, has_marker

    def _remove_creation_marker(self, record: _RequestRecord) -> None:
        assert record.temporary_identity is not None
        owner = _OwnedWorkingFd(
            self._open_named_directory(
                self._root_fd,
                record.temporary_name,
                record.temporary_identity,
            ),
            self._poison,
        )
        with owner as directory_fd:
            try:
                os.stat(
                    _CREATION_MARKER_NAME,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            raw = self._read_file_at(
                directory_fd,
                _CREATION_MARKER_NAME,
                MAX_ARTIFACT_RECORD_BYTES,
            )
            _parse_creation_marker(raw, record.temporary_name, record.temporary_identity)
            os.unlink(_CREATION_MARKER_NAME, dir_fd=directory_fd)
            _fsync_fd(directory_fd)

    def _ensure_staging(self, record: _RequestRecord) -> _RequestRecord:
        if record.phase is ArtifactRequestPhase.STAGING:
            self._remove_creation_marker(record)
            return record
        if record.phase is not ArtifactRequestPhase.RESERVED:
            return record
        existing = self._inspect_reserved_temporary(record)
        if existing is None:
            inventory = self._scan_locked()
            if inventory.temporaries >= MAX_ARTIFACT_TEMPORARIES:
                raise ArtifactStoreError(ArtifactStoreErrorCode.RESOURCE_EXHAUSTED)
            try:
                os.mkdir(record.temporary_name, 0o700, dir_fd=self._root_fd)
                _fsync_fd(self._root_fd)
                value = os.stat(
                    record.temporary_name,
                    dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
                identity = _identity(value)
                if not _private_directory(value):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                owner = _OwnedWorkingFd(
                    self._open_named_directory(
                        self._root_fd,
                        record.temporary_name,
                        identity,
                    ),
                    self._poison,
                )
                with owner as directory_fd:
                    self._write_creation_marker(directory_fd, record, identity)
            except ArtifactStoreError:
                raise
            except OSError:
                raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None
            has_marker = True
        else:
            identity, has_marker = existing
        staged = self._write_record(
            replace(
                record,
                phase=ArtifactRequestPhase.STAGING,
                temporary_identity=identity,
            )
        )
        if has_marker:
            self._remove_creation_marker(staged)
        return staged

    def _hash_open_file(
        self,
        directory_fd: int,
        name: str,
        maximum: int,
    ) -> tuple[_FileIdentity, str]:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not _private_file(before) or before.st_size > maximum:
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            owner = _OwnedWorkingFd(
                os.open(name, flags, dir_fd=directory_fd),
                self._poison,
            )
            digest = hashlib.sha256()
            with owner as fd:
                opened = os.fstat(fd)
                if not _same_identity(before, opened):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                remaining = opened.st_size
                while remaining:
                    chunk = os.read(fd, min(ARTIFACT_COPY_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                    digest.update(chunk)
                    remaining -= len(chunk)
                if not _same_identity(opened, os.fstat(fd)):
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not _same_identity(before, after):
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            return _identity(before), digest.hexdigest()
        except ArtifactStoreError:
            raise
        except (FileNotFoundError, OSError):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None

    def _copy(
        self,
        record: _RequestRecord,
        authority: ArtifactAuthorityPort,
    ) -> _RequestRecord | ArtifactDependencyFailure:
        if record.phase is not ArtifactRequestPhase.STAGING:
            return record
        assert record.temporary_identity is not None
        owner = _OwnedWorkingFd(
            self._open_named_directory(
                self._root_fd,
                record.temporary_name,
                record.temporary_identity,
            ),
            self._poison,
        )
        with owner as directory_fd:
            try:
                cursors = []
                names = {entry.name for entry in os.scandir(directory_fd)}
                if not names <= {"model.FCStd", "model.step"}:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                for expected in record.eligibility.artifacts:
                    if expected.name in names:
                        identity, digest = self._hash_open_file(
                            directory_fd,
                            expected.name,
                            expected.size_bytes,
                        )
                        cursors.append(
                            ArtifactCopyCursor(
                                name=expected.name,
                                size_bytes=identity.size,
                                sha256=digest,
                            )
                        )
                try:
                    outcome = authority.copy_authoritative(
                        eligibility=record.eligibility,
                        destination_directory_fd=directory_fd,
                        cursors=tuple(cursors),
                        chunk_bytes=ARTIFACT_COPY_CHUNK_BYTES,
                    )
                except Exception:
                    return ArtifactDependencyFailure(
                        code=ArtifactDependencyErrorCode.INTERNAL_ERROR
                    )
                if type(outcome) is ArtifactDependencyFailure:
                    return outcome
                if outcome is not None:
                    return ArtifactDependencyFailure(
                        code=ArtifactDependencyErrorCode.INTERNAL_ERROR
                    )
                identities = []
                names = {entry.name for entry in os.scandir(directory_fd)}
                if names != {"model.FCStd", "model.step"}:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                for expected in record.eligibility.artifacts:
                    identity, digest = self._hash_open_file(
                        directory_fd,
                        expected.name,
                        expected.size_bytes,
                    )
                    if identity.size != expected.size_bytes or digest != expected.sha256:
                        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                    identities.append(identity)
                os.fsync(directory_fd)
            except ArtifactStoreError:
                raise
            except OSError:
                raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None
        return self._write_record(
            replace(
                record,
                phase=ArtifactRequestPhase.COPIED,
                copied=tuple(identities),  # type: ignore[arg-type]
            )
        )

    def _validate_copy(self, record: _RequestRecord) -> tuple[_FileIdentity, _FileIdentity]:
        assert record.temporary_identity is not None
        owner = _OwnedWorkingFd(
            self._open_named_directory(
                self._root_fd,
                record.temporary_name,
                record.temporary_identity,
            ),
            self._poison,
        )
        with owner as directory_fd:
            names = {entry.name for entry in os.scandir(directory_fd)}
            if names not in (
                {"model.FCStd", "model.step"},
                {"model.FCStd", "model.step", "manifest.json"},
            ):
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            identities = []
            for expected in record.eligibility.artifacts:
                identity, digest = self._hash_open_file(
                    directory_fd, expected.name, expected.size_bytes
                )
                if identity.size != expected.size_bytes or digest != expected.sha256:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                identities.append(identity)
            return tuple(identities)  # type: ignore[return-value]

    def _validate_with_cad(
        self,
        record: _RequestRecord,
        cad: CadExecutionPort,
    ) -> _RequestRecord | ArtifactServicePortFailure:
        if record.phase is not ArtifactRequestPhase.COPIED:
            return record
        before = self._validate_copy(record)
        assert record.temporary_identity is not None
        owner = _OwnedWorkingFd(
            self._open_named_directory(
                self._root_fd,
                record.temporary_name,
                record.temporary_identity,
            ),
            self._poison,
        )
        with owner as directory_fd:
            try:
                evidence = _validate_cad_from_pinned_directory(
                    cad,
                    directory_fd,
                    record.temporary_identity,
                    self._poison,
                )
            except ArtifactStoreError:
                raise
            except Exception as error:
                from vibecad.execution.executor import (  # noqa: PLC0415
                    ExecutorError,
                    ExecutorErrorCode,
                )

                if type(error) is ExecutorError:
                    if error.code is ExecutorErrorCode.CAD_FAILURE:
                        return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.CAD_FAILURE)
                    if error.code in {
                        ExecutorErrorCode.INVALID_INPUT,
                        ExecutorErrorCode.ARTIFACT_FAILURE,
                        ExecutorErrorCode.INTEGRITY_FAILURE,
                    }:
                        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None
                return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INTERNAL_ERROR)
        if type(evidence) is not ValidatedMaterializationEvidence:
            return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INTERNAL_ERROR)
        expected = record.eligibility.artifacts
        if (
            evidence.fcstd_sha256 != expected[0].sha256
            or evidence.fcstd_size_bytes != expected[0].size_bytes
            or evidence.step_sha256 != expected[1].sha256
            or evidence.step_size_bytes != expected[1].size_bytes
            or self._validate_copy(record) != before
        ):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        return self._write_record(
            replace(
                record,
                phase=ArtifactRequestPhase.VALIDATED,
                validation=evidence,
            )
        )

    def _write_manifest(self, record: _RequestRecord) -> None:
        assert record.temporary_identity is not None
        owner = _OwnedWorkingFd(
            self._open_named_directory(
                self._root_fd,
                record.temporary_name,
                record.temporary_identity,
            ),
            self._poison,
        )
        body = _delivery_manifest_body(record.eligibility)
        raw = _canonical_json(
            {
                "schema_version": SCHEMA_VERSION,
                "body": body,
                "body_sha256": record.delivery_manifest_sha256,
            }
        )
        if len(raw) > MAX_ARTIFACT_RECORD_BYTES:
            owner.close()
            raise ArtifactStoreError(ArtifactStoreErrorCode.RESOURCE_EXHAUSTED)
        with owner as directory_fd:
            try:
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    file_owner = _OwnedWorkingFd(
                        os.open("manifest.json", flags, 0o600, dir_fd=directory_fd),
                        self._poison,
                    )
                except FileExistsError:
                    existing = self._read_file_at(
                        directory_fd,
                        "manifest.json",
                        MAX_ARTIFACT_RECORD_BYTES,
                    )
                    if existing != raw:
                        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None
                else:
                    with file_owner as fd:
                        os.fchmod(fd, 0o600)
                        offset = 0
                        while offset < len(raw):
                            written = os.write(fd, raw[offset:])
                            if written <= 0:
                                raise OSError
                            offset += written
                        os.fsync(fd)
                os.fsync(directory_fd)
            except ArtifactStoreError:
                raise
            except OSError:
                raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None

    def _validate_manifest(self, directory_fd: int, record: _RequestRecord) -> None:
        raw = self._read_file_at(
            directory_fd,
            "manifest.json",
            MAX_ARTIFACT_RECORD_BYTES,
        )
        data = _exact_keys(_parse_json(raw), {"schema_version", "body", "body_sha256"})
        if (
            data["schema_version"] != SCHEMA_VERSION
            or data["body"] != _delivery_manifest_body(record.eligibility)
            or data["body_sha256"] != record.delivery_manifest_sha256
            or hashlib.sha256(_DELIVERY_MANIFEST_DOMAIN + _canonical_json(data["body"])).hexdigest()
            != record.delivery_manifest_sha256
        ):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)

    def _materialize(self, record: _RequestRecord) -> _RequestRecord:
        if record.phase is ArtifactRequestPhase.MATERIALIZED:
            self._validate_materialized(record)
            return record
        if record.phase is not ArtifactRequestPhase.VALIDATED:
            return record
        self._validate_copy(record)
        self._write_manifest(record)
        try:
            os.stat(
                record.materialization_id,
                dir_fd=self._materializations_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            try:
                _rename_directory_noreplace(
                    self._root_fd,
                    record.temporary_name,
                    self._materializations_fd,
                    record.materialization_id,
                )
            except FileExistsError:
                try:
                    self._validate_materialization_directory(record)
                except FileNotFoundError:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None
                self._delete_temporary(record)
            except OSError:
                raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None
            else:
                _fsync_fd(self._root_fd)
                _fsync_fd(self._materializations_fd)
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None
        else:
            try:
                self._validate_materialization_directory(record)
            except FileNotFoundError:
                raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None
            self._delete_temporary(record)
        identity = os.stat(
            record.materialization_id,
            dir_fd=self._materializations_fd,
            follow_symlinks=False,
        )
        if not _private_directory(identity):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        updated = self._write_record(
            replace(
                record,
                phase=ArtifactRequestPhase.MATERIALIZED,
                materialized_identity=_identity(identity),
            )
        )
        self._validate_materialized(updated)
        return updated

    def _validate_materialization_directory(self, record: _RequestRecord) -> None:
        value = os.stat(
            record.materialization_id,
            dir_fd=self._materializations_fd,
            follow_symlinks=False,
        )
        if not _private_directory(value):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        owner = _OwnedWorkingFd(
            self._open_named_directory(
                self._materializations_fd,
                record.materialization_id,
                _identity(value),
            ),
            self._poison,
        )
        with owner as directory_fd:
            names = {entry.name for entry in os.scandir(directory_fd)}
            if names != {"model.FCStd", "model.step", "manifest.json"}:
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            for expected in record.eligibility.artifacts:
                identity, digest = self._hash_open_file(
                    directory_fd, expected.name, expected.size_bytes
                )
                if identity.size != expected.size_bytes or digest != expected.sha256:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            self._validate_manifest(directory_fd, record)

    def _validate_materialized(self, record: _RequestRecord) -> None:
        if record.materialized_identity is None:
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        try:
            value = os.stat(
                record.materialization_id,
                dir_fd=self._materializations_fd,
                follow_symlinks=False,
            )
            if not _same_directory_binding(value, record.materialized_identity):
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
            self._validate_materialization_directory(record)
        except ArtifactStoreError:
            raise
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE) from None
        if record.response is not None:
            expected = _result(
                ArtifactExportRequest(
                    export_key=record.export_key,
                    task_id=record.eligibility.task_id,
                    expected_generation=record.eligibility.task_generation,
                    revision_id=record.eligibility.revision_id,
                    draft_id=record.eligibility.draft_id,
                ),
                record.eligibility,
            )
            if record.response != expected:
                raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)

    def _delete_temporary(self, record: _RequestRecord) -> None:
        try:
            value = os.stat(
                record.temporary_name,
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None
        if record.temporary_identity is None or not _same_directory_binding(
            value,
            record.temporary_identity,
        ):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        if not _private_directory(value):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        owner = _OwnedWorkingFd(
            self._open_named_directory(
                self._root_fd,
                record.temporary_name,
                record.temporary_identity,
            ),
            self._poison,
        )
        try:
            with owner as directory_fd:
                names = {entry.name for entry in os.scandir(directory_fd)}
                if not names <= {"model.FCStd", "model.step", "manifest.json"}:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                for name in sorted(names):
                    item = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if not _private_file(item):
                        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                    os.unlink(name, dir_fd=directory_fd)
                _fsync_fd(directory_fd)
            os.rmdir(record.temporary_name, dir_fd=self._root_fd)
            _fsync_fd(self._root_fd)
        except ArtifactStoreError:
            raise
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None

    def _has_published_share(self, record: _RequestRecord) -> bool:
        for entry in os.scandir(self._requests_fd):
            if _REQUEST_NAME.fullmatch(entry.name) is None:
                continue
            other = self._read_record_name(entry.name)
            if (
                other.export_key != record.export_key
                and other.phase is ArtifactRequestPhase.PUBLISHED
                and other.materialization_id == record.materialization_id
                and other.eligibility == record.eligibility
            ):
                return True
        return False

    def _delete_materialized(self, record: _RequestRecord) -> None:
        if record.materialized_identity is None or self._has_published_share(record):
            return
        try:
            value = os.stat(
                record.materialization_id,
                dir_fd=self._materializations_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None
        if not _same_directory_binding(
            value, record.materialized_identity
        ) or not _private_directory(value):
            raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
        self._validate_materialization_directory(record)
        owner = _OwnedWorkingFd(
            self._open_named_directory(
                self._materializations_fd,
                record.materialization_id,
                record.materialized_identity,
            ),
            self._poison,
        )
        try:
            with owner as directory_fd:
                names = {entry.name for entry in os.scandir(directory_fd)}
                if names != {"manifest.json", "model.FCStd", "model.step"}:
                    raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                for name in sorted(names):
                    value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if not _private_file(value):
                        raise ArtifactStoreError(ArtifactStoreErrorCode.INTEGRITY_FAILURE)
                    os.unlink(name, dir_fd=directory_fd)
                _fsync_fd(directory_fd)
            os.rmdir(record.materialization_id, dir_fd=self._materializations_fd)
            _fsync_fd(self._materializations_fd)
        except ArtifactStoreError:
            raise
        except OSError:
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED) from None

    def _reject(
        self,
        record: _RequestRecord,
        code: ArtifactServiceErrorCode,
    ) -> ArtifactServicePortFailure:
        if record.phase is ArtifactRequestPhase.REJECTED:
            assert record.failure_code is not None
            return ArtifactServicePortFailure(code=record.failure_code)
        cleanup = self._write_record(
            replace(
                record,
                phase=ArtifactRequestPhase.CLEANUP_REQUIRED,
                failure_code=code,
                response=None,
            )
        )
        try:
            self._delete_temporary(cleanup)
            self._delete_materialized(cleanup)
        except ArtifactStoreError:
            return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.RECOVERY_REQUIRED)
        terminal = self._write_record(replace(cleanup, phase=ArtifactRequestPhase.REJECTED))
        return ArtifactServicePortFailure(code=terminal.failure_code or code)

    def materialize(
        self,
        *,
        request: ArtifactExportRequest,
        eligibility: ArtifactEligibility,
        authority: ArtifactAuthorityPort,
        cad: CadExecutionPort,
        reload_eligibility: Callable[[], ArtifactEligibility | ArtifactServicePortFailure],
    ) -> ArtifactExportResult | ArtifactServicePortFailure:
        with self._lock():
            self._scan_locked()
            record = self._reserve(request, eligibility)
            if record.phase is ArtifactRequestPhase.PUBLISHED:
                assert record.response is not None
                self._validate_materialized(record)
                return record.response
            if record.phase is ArtifactRequestPhase.REJECTED:
                assert record.failure_code is not None
                return ArtifactServicePortFailure(code=record.failure_code)
            if record.phase is ArtifactRequestPhase.CLEANUP_REQUIRED:
                assert record.failure_code is not None
                return self._reject(record, record.failure_code)
            record = self._ensure_staging(record)
            copied = self._copy(record, authority)
            if type(copied) is ArtifactDependencyFailure:
                code = ArtifactServiceErrorCode(copied.code.value)
                if code in {
                    ArtifactServiceErrorCode.INVALID_STATE,
                    ArtifactServiceErrorCode.CONFLICT,
                    ArtifactServiceErrorCode.INTEGRITY_FAILURE,
                }:
                    return self._reject(record, code)
                return ArtifactServicePortFailure(code=code)
            record = copied
            validated = self._validate_with_cad(record, cad)
            if type(validated) is ArtifactServicePortFailure:
                return validated
            record = validated
            try:
                current = reload_eligibility()
            except Exception:
                return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INTERNAL_ERROR)
            if type(current) is ArtifactServicePortFailure:
                if current.code in {
                    ArtifactServiceErrorCode.INVALID_STATE,
                    ArtifactServiceErrorCode.CONFLICT,
                }:
                    return self._reject(record, current.code)
                return current
            if type(current) is not ArtifactEligibility or current != eligibility:
                return self._reject(record, ArtifactServiceErrorCode.CONFLICT)
            record = self._materialize(record)
            try:
                current = reload_eligibility()
            except Exception:
                return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INTERNAL_ERROR)
            if type(current) is ArtifactServicePortFailure:
                if current.code in {
                    ArtifactServiceErrorCode.INVALID_STATE,
                    ArtifactServiceErrorCode.CONFLICT,
                }:
                    return self._reject(record, current.code)
                return current
            if type(current) is not ArtifactEligibility or current != eligibility:
                return self._reject(record, ArtifactServiceErrorCode.CONFLICT)
            response = _result(request, eligibility)
            record = self._write_record(
                replace(
                    record,
                    phase=ArtifactRequestPhase.PUBLISHED,
                    response=response,
                )
            )
            self._validate_materialized(record)
            return record.response or response

    def read_resource(self, uri: object) -> ArtifactResourceContent:
        if type(uri) is not str:
            raise ArtifactResourceError(ArtifactResourceErrorCode.INVALID_IDENTIFIER)
        try:
            raw_uri = uri.encode("ascii")
        except UnicodeError:
            raise ArtifactResourceError(ArtifactResourceErrorCode.INVALID_IDENTIFIER) from None
        match = _RESOURCE_URI.fullmatch(uri)
        if len(raw_uri) != 141 or match is None or "%" in uri or "?" in uri or "#" in uri:
            raise ArtifactResourceError(ArtifactResourceErrorCode.INVALID_IDENTIFIER)
        materialization_id, artifact_id = match.groups()
        try:
            with self._lock():
                self._scan_locked()
                binding: tuple[_RequestRecord, MaterializedArtifactRef] | None = None
                for entry in os.scandir(self._requests_fd):
                    if _REQUEST_NAME.fullmatch(entry.name) is None:
                        continue
                    record = self._read_record_name(entry.name)
                    if record.phase is not ArtifactRequestPhase.PUBLISHED:
                        continue
                    assert record.response is not None
                    if record.response.materialization_id != materialization_id:
                        continue
                    matches = tuple(
                        item for item in record.response.artifacts if item.id == artifact_id
                    )
                    if len(matches) == 1:
                        binding = (record, matches[0])
                        break
                if binding is None:
                    raise ArtifactResourceError(ArtifactResourceErrorCode.UNAVAILABLE)
                record, artifact = binding
                self._validate_materialized(record)
                if artifact.size_bytes > MAX_ARTIFACT_RESOURCE_BYTES:
                    raise ArtifactResourceError(ArtifactResourceErrorCode.READ_LIMIT)
                if (
                    _resource_incremental_allocation_bound(artifact.size_bytes)
                    > MAX_ARTIFACT_RESOURCE_INCREMENTAL_BYTES
                ):
                    raise ArtifactResourceError(ArtifactResourceErrorCode.READ_LIMIT)
                assert record.materialized_identity is not None
                directory_owner = _OwnedWorkingFd(
                    self._open_named_directory(
                        self._materializations_fd,
                        materialization_id,
                        record.materialized_identity,
                    ),
                    self._poison,
                )
                with directory_owner as directory_fd:
                    before = os.stat(
                        artifact.name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not _private_file(before)
                        or before.st_size != artifact.size_bytes
                        or before.st_size > MAX_ARTIFACT_RESOURCE_BYTES
                    ):
                        raise ArtifactResourceError(ArtifactResourceErrorCode.UNAVAILABLE)
                    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                    file_owner = _OwnedWorkingFd(
                        os.open(artifact.name, flags, dir_fd=directory_fd),
                        self._poison,
                    )
                    with file_owner as fd:
                        opened = os.fstat(fd)
                        if not _same_identity(before, opened):
                            raise ArtifactResourceError(ArtifactResourceErrorCode.UNAVAILABLE)
                        chunks = []
                        digest = hashlib.sha256()
                        remaining = artifact.size_bytes
                        while remaining:
                            chunk = os.read(fd, min(ARTIFACT_COPY_CHUNK_BYTES, remaining))
                            if not chunk:
                                raise ArtifactResourceError(ArtifactResourceErrorCode.UNAVAILABLE)
                            chunks.append(chunk)
                            digest.update(chunk)
                            remaining -= len(chunk)
                        if not _same_identity(opened, os.fstat(fd)):
                            raise ArtifactResourceError(ArtifactResourceErrorCode.UNAVAILABLE)
                    after = os.stat(
                        artifact.name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    if not _same_identity(before, after) or digest.hexdigest() != artifact.sha256:
                        raise ArtifactResourceError(ArtifactResourceErrorCode.UNAVAILABLE)
                content = b"".join(chunks)
                blob = base64.b64encode(content).decode("ascii")
                canonical_uri = _resource_uri(materialization_id, artifact_id)
                mime = (
                    "application/vnd.freecad.fcstd" if artifact.format == "fcstd" else "model/step"
                )
                return ArtifactResourceContent(uri=canonical_uri, blob=blob, mime_type=mime)
        except ArtifactResourceError:
            raise
        except ArtifactStoreError:
            raise ArtifactResourceError(ArtifactResourceErrorCode.UNAVAILABLE) from None
        except Exception:
            raise ArtifactResourceError(ArtifactResourceErrorCode.INTERNAL_ERROR) from None


def _task_artifact_matches(task_ref: TaskArtifactRef, revision_ref: RevisionArtifactRef) -> bool:
    return (
        type(task_ref) is TaskArtifactRef
        and task_ref.id == revision_ref.id
        and task_ref.name == revision_ref.name
        and task_ref.format == revision_ref.format
        and task_ref.sha256 == revision_ref.sha256
        and task_ref.size_bytes == revision_ref.size_bytes
    )


_DEPENDENCY_TO_SERVICE = {
    code: ArtifactServiceErrorCode(code.value) for code in ArtifactDependencyErrorCode
}


class ArtifactMaterializationService:
    """Task-gated orchestration over the durable artifact store."""

    __slots__ = ("_authority", "_cad", "_store")

    def __init__(
        self,
        *,
        store: ArtifactStore,
        authority: ArtifactAuthorityPort,
        cad: CadExecutionPort,
    ) -> None:
        if type(store) is not ArtifactStore or not isinstance(cad, CadExecutionPort):
            raise TypeError("invalid artifact service composition")
        self._store = store
        self._authority = authority
        self._cad = cad

    @staticmethod
    def _dependency(value: object) -> ArtifactServicePortFailure | None:
        if type(value) is not ArtifactDependencyFailure:
            return None
        return ArtifactServicePortFailure(code=_DEPENDENCY_TO_SERVICE[value.code])

    def _eligibility(
        self,
        request: ArtifactExportRequest,
    ) -> ArtifactEligibility | ArtifactServicePortFailure:
        try:
            stored = self._authority.load_task(task_id=request.task_id)
        except Exception:
            return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INTERNAL_ERROR)
        failure = self._dependency(stored)
        if failure is not None:
            return failure
        if type(stored) is not StoredTaskRun:
            return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INTERNAL_ERROR)
        task = stored.task_run
        if stored.generation != request.expected_generation or task.id != request.task_id:
            return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.CONFLICT)
        if request.draft_id is None:
            if (
                task.status is not TaskStatus.SUCCEEDED
                or task.committed_revision != request.revision_id
            ):
                return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INVALID_STATE)
            source_kind = ArtifactSourceKind.COMMITTED
        else:
            draft = task.draft
            if (
                task.status is not TaskStatus.AWAITING_USER_REVIEW
                or draft is None
                or draft.id != request.draft_id
                or draft.revision_id != request.revision_id
                or draft.task_id != task.id
                or draft.project_id != task.project_id
            ):
                return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INVALID_STATE)
            source_kind = ArtifactSourceKind.DRAFT
        try:
            revision = self._authority.load_revision(
                project_id=task.project_id,
                revision_id=request.revision_id,
            )
        except Exception:
            return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INTERNAL_ERROR)
        failure = self._dependency(revision)
        if failure is not None:
            return failure
        if (
            type(revision) is not RevisionRef
            or revision.id != request.revision_id
            or revision.project_id != task.project_id
            or revision.model is None
            or type(revision.artifacts) is not tuple
            or len(revision.artifacts) != 1
        ):
            return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INTEGRITY_FAILURE)
        model, step = revision.model, revision.artifacts[0]
        if (
            (model.name, model.format, step.name, step.format)
            != ("model.FCStd", "fcstd", "model.step", "step")
            or len(task.artifacts) != 2
            or not _task_artifact_matches(task.artifacts[0], model)
            or not _task_artifact_matches(task.artifacts[1], step)
            or any(item.candidate_revision != request.revision_id for item in task.artifacts)
        ):
            return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INTEGRITY_FAILURE)
        if source_kind is ArtifactSourceKind.DRAFT:
            assert task.draft is not None
            if task.draft.manifest_sha256 != revision.manifest_sha256:
                return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INTEGRITY_FAILURE)
        try:
            return ArtifactEligibility(
                source_kind=source_kind,
                task_id=task.id,
                task_generation=stored.generation,
                project_id=task.project_id,
                revision_id=revision.id,
                manifest_sha256=revision.manifest_sha256,
                draft_id=request.draft_id,
                artifacts=(model, step),
            )
        except Exception:
            return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INTEGRITY_FAILURE)

    def export_task_artifacts(
        self,
        *,
        request: ArtifactExportRequest,
    ) -> ArtifactExportResult | ArtifactServicePortFailure:
        if type(request) is not ArtifactExportRequest:
            return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INVALID_INPUT)
        try:
            terminal = self._store.lookup_terminal(request=request)
        except ArtifactStoreError as error:
            return ArtifactServicePortFailure(code=_store_service_code(error.code))
        if terminal is not None:
            return terminal
        try:
            exists = self._authority.task_exists(task_id=request.task_id)
        except Exception:
            return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INTERNAL_ERROR)
        failure = self._dependency(exists)
        if failure is not None:
            return failure
        if type(exists) is not bool:
            return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INTERNAL_ERROR)
        if not exists:
            return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.NOT_FOUND)
        try:
            gate = self._authority.acquire_export_gate(task_id=request.task_id)
            with gate:
                terminal = self._store.lookup_terminal(request=request)
                if terminal is not None:
                    return terminal
                eligibility = self._eligibility(request)
                if type(eligibility) is ArtifactServicePortFailure:
                    return eligibility
                return self._store.materialize(
                    request=request,
                    eligibility=eligibility,
                    authority=self._authority,
                    cad=self._cad,
                    reload_eligibility=lambda: self._eligibility(request),
                )
        except ArtifactDependencyError as error:
            return ArtifactServicePortFailure(code=_DEPENDENCY_TO_SERVICE[error.code])
        except ArtifactStoreError as error:
            return ArtifactServicePortFailure(code=_store_service_code(error.code))
        except Exception:
            return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INTERNAL_ERROR)


def _store_service_code(code: ArtifactStoreErrorCode) -> ArtifactServiceErrorCode:
    return {
        ArtifactStoreErrorCode.INVALID_INPUT: ArtifactServiceErrorCode.INVALID_INPUT,
        ArtifactStoreErrorCode.NOT_FOUND: ArtifactServiceErrorCode.NOT_FOUND,
        ArtifactStoreErrorCode.CONFLICT: ArtifactServiceErrorCode.CONFLICT,
        ArtifactStoreErrorCode.INVALID_STATE: ArtifactServiceErrorCode.INVALID_STATE,
        ArtifactStoreErrorCode.RESOURCE_EXHAUSTED: ArtifactServiceErrorCode.RESOURCE_EXHAUSTED,
        ArtifactStoreErrorCode.INTEGRITY_FAILURE: ArtifactServiceErrorCode.INTEGRITY_FAILURE,
        ArtifactStoreErrorCode.IO_ERROR: ArtifactServiceErrorCode.STORE_FAILURE,
        ArtifactStoreErrorCode.RECOVERY_REQUIRED: ArtifactServiceErrorCode.RECOVERY_REQUIRED,
    }[code]


_AUTHORITY_TASK_ID = re.compile(r"task_[0-9a-f]{32}\Z")
_AUTHORITY_PROJECT_ID = re.compile(r"project_[0-9a-f]{32}\Z")
_AUTHORITY_REVISION_ID = re.compile(r"revision_[0-9a-f]{32}\Z")
_AUTHORITY_DRAFT_ID = re.compile(r"draft_[0-9a-f]{32}\Z")
_AUTHORITY_ARTIFACT_ID = re.compile(r"artifact_[0-9a-f]{32}\Z")
_AUTHORITY_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_AUTHORITY_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

_AUTHORITY_TASK_ERRORS = MappingProxyType(
    {
        TaskStoreErrorCode.INVALID_ID: ArtifactDependencyErrorCode.INTERNAL_ERROR,
        TaskStoreErrorCode.NOT_FOUND: ArtifactDependencyErrorCode.NOT_FOUND,
        TaskStoreErrorCode.ALREADY_EXISTS: ArtifactDependencyErrorCode.INTERNAL_ERROR,
        TaskStoreErrorCode.CONFLICT: ArtifactDependencyErrorCode.INTERNAL_ERROR,
        TaskStoreErrorCode.CORRUPT_RECORD: ArtifactDependencyErrorCode.INTEGRITY_FAILURE,
        TaskStoreErrorCode.RECORD_TOO_LARGE: ArtifactDependencyErrorCode.INTEGRITY_FAILURE,
        TaskStoreErrorCode.UNSAFE_STORE: ArtifactDependencyErrorCode.STORE_FAILURE,
        TaskStoreErrorCode.LOCK_UNAVAILABLE: ArtifactDependencyErrorCode.LEASE_UNAVAILABLE,
        TaskStoreErrorCode.IO_ERROR: ArtifactDependencyErrorCode.STORE_FAILURE,
        TaskStoreErrorCode.DURABILITY_UNCERTAIN: ArtifactDependencyErrorCode.RECOVERY_REQUIRED,
        TaskStoreErrorCode.RESOURCE_EXHAUSTED: ArtifactDependencyErrorCode.RESOURCE_EXHAUSTED,
    }
)

_AUTHORITY_REVISION_ERRORS = MappingProxyType(
    {
        RevisionStoreErrorCode.INVALID_IDENTIFIER: ArtifactDependencyErrorCode.INTERNAL_ERROR,
        RevisionStoreErrorCode.INVALID_INPUT: ArtifactDependencyErrorCode.INTERNAL_ERROR,
        RevisionStoreErrorCode.NOT_FOUND: ArtifactDependencyErrorCode.NOT_FOUND,
        RevisionStoreErrorCode.ALREADY_EXISTS: ArtifactDependencyErrorCode.INTERNAL_ERROR,
        RevisionStoreErrorCode.CONFLICT: ArtifactDependencyErrorCode.INTEGRITY_FAILURE,
        RevisionStoreErrorCode.CORRUPT_RECORD: ArtifactDependencyErrorCode.INTEGRITY_FAILURE,
        RevisionStoreErrorCode.CORRUPT_CONTENT: ArtifactDependencyErrorCode.INTEGRITY_FAILURE,
        RevisionStoreErrorCode.BUDGET_EXCEEDED: ArtifactDependencyErrorCode.RESOURCE_EXHAUSTED,
        RevisionStoreErrorCode.RESOURCE_EXHAUSTED: ArtifactDependencyErrorCode.RESOURCE_EXHAUSTED,
        RevisionStoreErrorCode.UNSAFE_STORE: ArtifactDependencyErrorCode.STORE_FAILURE,
        RevisionStoreErrorCode.INVALID_LEASE: ArtifactDependencyErrorCode.INTERNAL_ERROR,
        RevisionStoreErrorCode.IO_ERROR: ArtifactDependencyErrorCode.STORE_FAILURE,
        RevisionStoreErrorCode.DURABILITY_UNCERTAIN: ArtifactDependencyErrorCode.RECOVERY_REQUIRED,
        RevisionStoreErrorCode.RECOVERY_REQUIRED: ArtifactDependencyErrorCode.RECOVERY_REQUIRED,
        RevisionStoreErrorCode.CLEANUP_REQUIRED: ArtifactDependencyErrorCode.RECOVERY_REQUIRED,
    }
)

_AUTHORITY_LEASE_STORE_ERRORS = frozenset(
    {
        LeaseErrorCode.UNTRUSTED_ROOT,
        LeaseErrorCode.UNSAFE_ROOT,
        LeaseErrorCode.UNSAFE_LOCK_ENTRY,
        LeaseErrorCode.UNSUPPORTED_PLATFORM,
        LeaseErrorCode.LOCK_UNAVAILABLE,
        LeaseErrorCode.IO_ERROR,
    }
)


def _authority_failure(code: ArtifactDependencyErrorCode) -> ArtifactDependencyFailure:
    return ArtifactDependencyFailure(code=code)


def _authority_task_failure(error: TaskStoreError) -> ArtifactDependencyFailure:
    if type(error) is not TaskStoreError or type(error.code) is not TaskStoreErrorCode:
        return _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
    code = _AUTHORITY_TASK_ERRORS.get(error.code, ArtifactDependencyErrorCode.INTERNAL_ERROR)
    return _authority_failure(code)


def _authority_revision_failure(error: RevisionStoreError) -> ArtifactDependencyFailure:
    if type(error) is not RevisionStoreError or type(error.code) is not RevisionStoreErrorCode:
        return _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
    code = _AUTHORITY_REVISION_ERRORS.get(error.code, ArtifactDependencyErrorCode.INTERNAL_ERROR)
    return _authority_failure(code)


def _authority_lease_code(error: LeaseError) -> ArtifactDependencyErrorCode:
    if type(error) is not LeaseError or type(error.code) is not LeaseErrorCode:
        return ArtifactDependencyErrorCode.INTERNAL_ERROR
    if error.code is LeaseErrorCode.CONTENDED:
        return ArtifactDependencyErrorCode.LEASE_UNAVAILABLE
    if error.code in _AUTHORITY_LEASE_STORE_ERRORS:
        return ArtifactDependencyErrorCode.STORE_FAILURE
    return ArtifactDependencyErrorCode.INTERNAL_ERROR


def _authority_valid_identifier(value: object, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _authority_canonical_artifact(value: object) -> RevisionArtifactRef | None:
    if type(value) is not RevisionArtifactRef:
        return None
    if (
        type(value.schema_version) is not int
        or type(value.id) is not str
        or _AUTHORITY_ARTIFACT_ID.fullmatch(value.id) is None
        or type(value.name) is not str
        or type(value.format) is not str
        or type(value.sha256) is not str
        or _AUTHORITY_DIGEST.fullmatch(value.sha256) is None
        or type(value.size_bytes) is not int
        or not 0 < value.size_bytes <= MAX_ARTIFACT_SOURCE_BYTES
    ):
        return None
    try:
        return RevisionArtifactRef(
            schema_version=value.schema_version,
            id=value.id,
            name=value.name,
            format=value.format,
            sha256=value.sha256,
            size_bytes=value.size_bytes,
        )
    except BaseException:
        return None


def _authority_canonical_stored_task(value: object, task_id: str) -> StoredTaskRun | None:
    if (
        type(value) is not StoredTaskRun
        or type(value.generation) is not int
        or not 0 <= value.generation <= MAX_SAFE_JSON_INTEGER
        or type(value.task_run) is not TaskRun
    ):
        return None
    try:
        mapping = value.task_run.to_mapping()
        if type(mapping) is not dict:
            return None
        task = TaskRun.from_mapping(mapping)
        if task.id != task_id:
            return None
        return StoredTaskRun(generation=value.generation, task_run=task)
    except BaseException:
        return None


def _authority_valid_revision(value: object) -> bool:
    if type(value) is not RevisionRef:
        return False
    if (
        type(value.schema_version) is not int
        or not _authority_valid_identifier(value.id, _AUTHORITY_REVISION_ID)
        or not _authority_valid_identifier(value.project_id, _AUTHORITY_PROJECT_ID)
        or (
            value.base_revision is not None
            and not _authority_valid_identifier(value.base_revision, _AUTHORITY_REVISION_ID)
        )
        or type(value.manifest_sha256) is not str
        or _AUTHORITY_DIGEST.fullmatch(value.manifest_sha256) is None
        or (value.model is not None and type(value.model) is not RevisionArtifactRef)
        or type(value.artifacts) is not tuple
        or len(value.artifacts) > 1
        or any(type(item) is not RevisionArtifactRef for item in value.artifacts)
    ):
        return False
    model = None if value.model is None else _authority_canonical_artifact(value.model)
    artifacts = tuple(_authority_canonical_artifact(item) for item in value.artifacts)
    if (value.model is not None and model is None) or any(item is None for item in artifacts):
        return False
    try:
        RevisionRef(
            schema_version=value.schema_version,
            id=value.id,
            project_id=value.project_id,
            base_revision=value.base_revision,
            manifest_sha256=value.manifest_sha256,
            model=model,
            artifacts=artifacts,
        )
    except BaseException:
        return False
    return True


def _authority_canonical_eligibility(value: object) -> ArtifactEligibility | None:
    if type(value) is not ArtifactEligibility:
        return None
    if (
        type(value.source_kind) is not ArtifactSourceKind
        or type(value.task_generation) is not int
        or not 0 <= value.task_generation <= MAX_SAFE_JSON_INTEGER
        or not _authority_valid_identifier(value.task_id, _AUTHORITY_TASK_ID)
        or not _authority_valid_identifier(value.project_id, _AUTHORITY_PROJECT_ID)
        or not _authority_valid_identifier(value.revision_id, _AUTHORITY_REVISION_ID)
        or type(value.manifest_sha256) is not str
        or _AUTHORITY_DIGEST.fullmatch(value.manifest_sha256) is None
        or type(value.artifacts) is not tuple
        or len(value.artifacts) != 2
        or (value.source_kind is ArtifactSourceKind.COMMITTED and value.draft_id is not None)
        or (
            value.source_kind is ArtifactSourceKind.DRAFT
            and not _authority_valid_identifier(value.draft_id, _AUTHORITY_DRAFT_ID)
        )
    ):
        return None
    model = _authority_canonical_artifact(value.artifacts[0])
    step = _authority_canonical_artifact(value.artifacts[1])
    if (
        model is None
        or step is None
        or (model.name, model.format, step.name, step.format)
        != ("model.FCStd", "fcstd", "model.step", "step")
        or model.id == step.id
        or model.size_bytes + step.size_bytes > MAX_ARTIFACT_PAIR_BYTES
    ):
        return None
    try:
        return ArtifactEligibility(
            source_kind=value.source_kind,
            task_id=value.task_id,
            task_generation=value.task_generation,
            project_id=value.project_id,
            revision_id=value.revision_id,
            manifest_sha256=value.manifest_sha256,
            draft_id=value.draft_id,
            artifacts=(model, step),
        )
    except BaseException:
        return None


def _authority_canonical_cursors(
    value: object,
    artifacts: tuple[RevisionArtifactRef, RevisionArtifactRef],
) -> tuple[RevisionCopyCursor, ...] | None:
    if type(value) is not tuple or len(value) > 2:
        return None
    if any(type(item) is not ArtifactCopyCursor for item in value):
        return None
    result: list[RevisionCopyCursor] = []
    for index, item in enumerate(value):
        expected = artifacts[index]
        if (
            type(item.name) is not str
            or item.name != expected.name
            or type(item.size_bytes) is not int
            or not 0 <= item.size_bytes <= expected.size_bytes
            or type(item.sha256) is not str
            or _AUTHORITY_DIGEST.fullmatch(item.sha256) is None
            or (item.size_bytes == 0 and item.sha256 != _AUTHORITY_EMPTY_SHA256)
            or (item.size_bytes == expected.size_bytes and item.sha256 != expected.sha256)
        ):
            return None
        try:
            result.append(
                RevisionCopyCursor(
                    name=item.name,
                    size_bytes=item.size_bytes,
                    sha256=item.sha256,
                )
            )
        except BaseException:
            return None
    if len(result) == 2:
        model_cursor = result[0]
        model = artifacts[0]
        if model_cursor.size_bytes != model.size_bytes or model_cursor.sha256 != model.sha256:
            return None
    return tuple(result)


def _authority_revision_matches(value: object, expected: ArtifactEligibility) -> bool:
    if not _authority_valid_revision(value):
        return False
    assert type(value) is RevisionRef
    if (
        value.id != expected.revision_id
        or value.project_id != expected.project_id
        or value.base_revision is None
        or value.manifest_sha256 != expected.manifest_sha256
        or type(value.model) is not RevisionArtifactRef
        or type(value.artifacts) is not tuple
        or len(value.artifacts) != 1
        or type(value.artifacts[0]) is not RevisionArtifactRef
    ):
        return False
    return value.model == expected.artifacts[0] and value.artifacts[0] == expected.artifacts[1]


class _ArtifactExportGate(AbstractContextManager[None]):
    __slots__ = ("_authority", "_entered", "_lease", "_task_id")

    def __init__(self, authority: LocalArtifactAuthority, task_id: str) -> None:
        self._authority = authority
        self._task_id = task_id
        self._lease: ResourceLease | None = None
        self._entered = False

    def __enter__(self) -> None:
        if self._entered or self._lease is not None:
            raise ArtifactDependencyError(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        exists = self._authority.task_exists(task_id=self._task_id)
        if type(exists) is ArtifactDependencyFailure:
            code = exists.code
            if type(code) is not ArtifactDependencyErrorCode:
                code = ArtifactDependencyErrorCode.INTERNAL_ERROR
            raise ArtifactDependencyError(code)
        if type(exists) is not bool:
            raise ArtifactDependencyError(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        if not exists:
            raise ArtifactDependencyError(ArtifactDependencyErrorCode.NOT_FOUND)
        try:
            lease = self._authority._lease_manager.acquire(f"artifact-export:{self._task_id}")
        except LeaseError as error:
            raise ArtifactDependencyError(_authority_lease_code(error)) from None
        except BaseException:
            raise ArtifactDependencyError(ArtifactDependencyErrorCode.INTERNAL_ERROR) from None
        if type(lease) is not ResourceLease:
            raise ArtifactDependencyError(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        self._lease = lease
        self._entered = True
        return None

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback
        lease = self._lease
        if not self._entered or type(lease) is not ResourceLease:
            raise ArtifactDependencyError(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        self._entered = False
        self._lease = None
        try:
            lease.release(owner_token=lease.owner_token)
        except LeaseError as error:
            raise ArtifactDependencyError(_authority_lease_code(error)) from None
        except BaseException:
            raise ArtifactDependencyError(ArtifactDependencyErrorCode.INTERNAL_ERROR) from None


class LocalArtifactAuthority(ArtifactAuthorityPort):
    """Compose the exact durable stores and cross-process artifact gate."""

    __slots__ = ("_lease_manager", "_revision_store", "_task_store")

    def __init__(
        self,
        *,
        task_store: TaskRunStore,
        revision_store: LocalRevisionStore,
        lease_manager: ResourceLeaseManager,
    ) -> None:
        if (
            type(task_store) is not TaskRunStore
            or type(revision_store) is not LocalRevisionStore
            or type(lease_manager) is not ResourceLeaseManager
        ):
            raise TypeError("invalid local artifact authority composition")
        if (
            task_store._lease_manager is not lease_manager
            or revision_store._lease_manager is not lease_manager
        ):
            raise TypeError("artifact authority dependencies must share one lease manager")
        self._task_store = task_store
        self._revision_store = revision_store
        self._lease_manager = lease_manager

    def task_exists(self, *, task_id: str) -> bool | ArtifactDependencyFailure:
        if not _authority_valid_identifier(task_id, _AUTHORITY_TASK_ID):
            return _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        try:
            stored = self._task_store.load(task_id)
        except TaskStoreError as error:
            if (
                type(error) is TaskStoreError
                and type(error.code) is TaskStoreErrorCode
                and error.code is TaskStoreErrorCode.NOT_FOUND
            ):
                return False
            return _authority_task_failure(error)
        except BaseException:
            return _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        canonical = _authority_canonical_stored_task(stored, task_id)
        if canonical is None:
            return _authority_failure(ArtifactDependencyErrorCode.INTEGRITY_FAILURE)
        return True

    def acquire_export_gate(self, *, task_id: str) -> AbstractContextManager[None]:
        if not _authority_valid_identifier(task_id, _AUTHORITY_TASK_ID):
            raise ArtifactDependencyError(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        return _ArtifactExportGate(self, task_id)

    def load_task(self, *, task_id: str) -> StoredTaskRun | ArtifactDependencyFailure:
        if not _authority_valid_identifier(task_id, _AUTHORITY_TASK_ID):
            return _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        try:
            stored = self._task_store.load(task_id)
        except TaskStoreError as error:
            return _authority_task_failure(error)
        except BaseException:
            return _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        canonical = _authority_canonical_stored_task(stored, task_id)
        if canonical is None:
            return _authority_failure(ArtifactDependencyErrorCode.INTEGRITY_FAILURE)
        return canonical

    def load_revision(
        self,
        *,
        project_id: str,
        revision_id: str,
    ) -> RevisionRef | ArtifactDependencyFailure:
        if not _authority_valid_identifier(
            project_id, _AUTHORITY_PROJECT_ID
        ) or not _authority_valid_identifier(revision_id, _AUTHORITY_REVISION_ID):
            return _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        try:
            revision = self._revision_store.load_revision(project_id, revision_id)
        except RevisionStoreError as error:
            return _authority_revision_failure(error)
        except BaseException:
            return _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        if not _authority_valid_revision(revision):
            return _authority_failure(ArtifactDependencyErrorCode.INTEGRITY_FAILURE)
        assert type(revision) is RevisionRef
        if revision.project_id != project_id or revision.id != revision_id:
            return _authority_failure(ArtifactDependencyErrorCode.INTEGRITY_FAILURE)
        return revision

    def copy_authoritative(
        self,
        *,
        eligibility: ArtifactEligibility,
        destination_directory_fd: int,
        cursors: tuple[ArtifactCopyCursor, ...],
        chunk_bytes: int,
    ) -> None | ArtifactDependencyFailure:
        if type(eligibility) is not ArtifactEligibility:
            return _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        if (
            type(destination_directory_fd) is not int
            or destination_directory_fd < 0
            or type(cursors) is not tuple
            or len(cursors) > 2
            or any(type(item) is not ArtifactCopyCursor for item in cursors)
            or type(chunk_bytes) is not int
        ):
            return _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        canonical = _authority_canonical_eligibility(eligibility)
        if canonical is None:
            return _authority_failure(ArtifactDependencyErrorCode.INTEGRITY_FAILURE)
        if chunk_bytes != ARTIFACT_COPY_CHUNK_BYTES:
            return _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        converted = _authority_canonical_cursors(cursors, canonical.artifacts)
        if converted is None:
            return _authority_failure(ArtifactDependencyErrorCode.INTEGRITY_FAILURE)
        try:
            revision = self._revision_store.load_revision(
                canonical.project_id,
                canonical.revision_id,
            )
        except RevisionStoreError as error:
            return _authority_revision_failure(error)
        except BaseException:
            return _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        if not _authority_revision_matches(revision, canonical):
            return _authority_failure(ArtifactDependencyErrorCode.INTEGRITY_FAILURE)
        assert type(revision) is RevisionRef
        try:
            outcome = self._revision_store.copy_revision_artifacts_at(
                expected_revision=revision,
                destination_directory_fd=destination_directory_fd,
                cursors=converted,
                chunk_bytes=chunk_bytes,
            )
        except RevisionStoreError as error:
            return _authority_revision_failure(error)
        except BaseException:
            return _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        if outcome is not None:
            return _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        return None


__all__ = (
    "ARTIFACT_COPY_CHUNK_BYTES",
    "MAX_ARTIFACT_MATERIALIZATIONS",
    "MAX_ARTIFACT_PAIR_BYTES",
    "MAX_ARTIFACT_RECORD_BYTES",
    "MAX_ARTIFACT_REQUESTS",
    "MAX_ARTIFACT_RESOURCE_BASE64_BYTES",
    "MAX_ARTIFACT_RESOURCE_BYTES",
    "MAX_ARTIFACT_RESOURCE_INCREMENTAL_BYTES",
    "MAX_ARTIFACT_SOURCE_BYTES",
    "MAX_ARTIFACT_STORE_BYTES",
    "MAX_ARTIFACT_TEMPORARIES",
    "ArtifactApi",
    "ArtifactApiErrorCode",
    "ArtifactAuthorityPort",
    "ArtifactCopyCursor",
    "ArtifactDependencyError",
    "ArtifactDependencyErrorCode",
    "ArtifactDependencyFailure",
    "ArtifactEligibility",
    "ArtifactExportRequest",
    "ArtifactExportResult",
    "ArtifactMaterializationService",
    "ArtifactRequestPhase",
    "ArtifactResourceContent",
    "ArtifactResourceError",
    "ArtifactResourceErrorCode",
    "ArtifactServiceErrorCode",
    "ArtifactServicePort",
    "ArtifactServicePortFailure",
    "ArtifactSourceKind",
    "ArtifactStore",
    "ArtifactStoreError",
    "ArtifactStoreErrorCode",
    "LocalArtifactAuthority",
    "MaterializedArtifactRef",
)
