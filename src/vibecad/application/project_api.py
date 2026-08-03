"""Bounded, transport-neutral public project API.

The adapter owns public schema validation and response projection.  Durable
project creation, filesystem access, and CAD execution stay behind the
injected :class:`ProjectServicePort`.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from vibecad.execution.revisions import ProjectHead, RevisionArtifactRef, RevisionRef
from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER, SCHEMA_VERSION

__all__ = (
    "ProjectApi",
    "ProjectApiErrorCode",
    "ProjectCreateResult",
    "ProjectCurrentResult",
    "ProjectKind",
    "ProjectServicePort",
    "ProjectServicePortErrorCode",
    "ProjectServicePortFailure",
)

_MAX_REQUEST_BYTES = 8 * 1024
_MAX_SOURCE_PATH_BYTES = 4096
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 8192
_MAX_JSON_KEY_BYTES = 256
_MAX_PUBLIC_ERROR_PATH_BYTES = 256
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_MAX_REVISION_BYTES = 1024 * 1024 * 1024

_CREATE_KEY = re.compile(r"^project_create_[0-9a-f]{32}$")
_PROJECT_ID = re.compile(r"^project_[0-9a-f]{32}$")
_REVISION_ID = re.compile(r"^revision_[0-9a-f]{32}$")
_ARTIFACT_ID = re.compile(r"^artifact_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_PROJECT_LIST_CURSOR = re.compile(r"^project_list_cursor_[0-9a-f]{64}$")
_REVISION_LIST_CURSOR = re.compile(r"^revision_list_cursor_[0-9a-f]{64}$")


class ProjectKind(StrEnum):
    """Supported generation-zero project kinds."""

    EMPTY = "empty"
    IMPORT_FCSTD = "import_fcstd"


class ProjectApiErrorCode(StrEnum):
    """Closed public project failure taxonomy."""

    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    UNSUPPORTED_VERSION = "unsupported_version"
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"
    BUDGET_EXCEEDED = "budget_exceeded"
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    LEASE_UNAVAILABLE = "lease_unavailable"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    INTEGRITY_FAILURE = "integrity_failure"
    CAD_FAILURE = "cad_failure"
    STORE_FAILURE = "store_failure"
    RECOVERY_REQUIRED = "recovery_required"
    INTERNAL_ERROR = "internal_error"


_ERROR_MESSAGES = {
    ProjectApiErrorCode.MISSING_FIELD: "A required request field is missing.",
    ProjectApiErrorCode.UNKNOWN_FIELD: "The request contains an unknown field.",
    ProjectApiErrorCode.UNSUPPORTED_VERSION: ("The request schema version is not supported."),
    ProjectApiErrorCode.INVALID_TYPE: "A request value has an invalid type.",
    ProjectApiErrorCode.INVALID_VALUE: "A request value is invalid.",
    ProjectApiErrorCode.BUDGET_EXCEEDED: "The request exceeds a resource budget.",
    ProjectApiErrorCode.INVALID_INPUT: "The project request is invalid.",
    ProjectApiErrorCode.NOT_FOUND: "The project record was not found.",
    ProjectApiErrorCode.CONFLICT: "The project changed concurrently.",
    ProjectApiErrorCode.LEASE_UNAVAILABLE: "The project write lease is unavailable.",
    ProjectApiErrorCode.RESOURCE_EXHAUSTED: ("The application resource capacity is exhausted."),
    ProjectApiErrorCode.RUNTIME_UNAVAILABLE: ("The managed CAD runtime is not active."),
    ProjectApiErrorCode.INTEGRITY_FAILURE: ("The project record failed integrity validation."),
    ProjectApiErrorCode.CAD_FAILURE: "The CAD operation failed.",
    ProjectApiErrorCode.STORE_FAILURE: "The project record operation failed.",
    ProjectApiErrorCode.RECOVERY_REQUIRED: "The project requires explicit recovery.",
    ProjectApiErrorCode.INTERNAL_ERROR: "The request could not be completed.",
}


class ProjectServicePortErrorCode(StrEnum):
    """Path-free failures accepted from the project-service port."""

    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    LEASE_UNAVAILABLE = "lease_unavailable"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    INTEGRITY_FAILURE = "integrity_failure"
    CAD_FAILURE = "cad_failure"
    STORE_FAILURE = "store_failure"
    RECOVERY_REQUIRED = "recovery_required"
    INTERNAL_ERROR = "internal_error"


_PORT_ERROR_MAP = {code: ProjectApiErrorCode(code.value) for code in ProjectServicePortErrorCode}


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectServicePortFailure:
    """A neutral failure which cannot carry paths or exception text."""

    code: ProjectServicePortErrorCode

    def __post_init__(self) -> None:
        if type(self.code) is not ProjectServicePortErrorCode:
            raise TypeError("code must be an exact ProjectServicePortErrorCode")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectCreateResult:
    """Authoritative immutable result of a durable create request."""

    create_key: str
    kind: ProjectKind
    cleanup_required: bool
    project_id: str
    head: ProjectHead
    revision: RevisionRef

    def __post_init__(self) -> None:
        if type(self.create_key) is not str:
            raise TypeError("create_key must be an exact str")
        if type(self.kind) is not ProjectKind:
            raise TypeError("kind must be an exact ProjectKind")
        if type(self.cleanup_required) is not bool:
            raise TypeError("cleanup_required must be an exact bool")
        if type(self.project_id) is not str:
            raise TypeError("project_id must be an exact str")
        if type(self.head) is not ProjectHead:
            raise TypeError("head must be an exact ProjectHead")
        if type(self.revision) is not RevisionRef:
            raise TypeError("revision must be an exact RevisionRef")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectCurrentResult:
    """One coherent HEAD/revision project snapshot."""

    project_id: str
    head: ProjectHead
    revision: RevisionRef

    def __post_init__(self) -> None:
        if type(self.project_id) is not str:
            raise TypeError("project_id must be an exact str")
        if type(self.head) is not ProjectHead:
            raise TypeError("head must be an exact ProjectHead")
        if type(self.revision) is not RevisionRef:
            raise TypeError("revision must be an exact RevisionRef")


class ProjectServicePort(Protocol):
    """Transport-neutral durable project service seam."""

    def create_project(
        self,
        *,
        create_key: str,
        kind: ProjectKind,
        source_path: str | None,
    ) -> ProjectCreateResult | ProjectServicePortFailure: ...

    def get_project(
        self, *, project_id: str
    ) -> ProjectCurrentResult | ProjectServicePortFailure: ...

    def list_projects(
        self, *, limit: int, cursor: str | None
    ) -> dict[str, object] | ProjectServicePortFailure: ...

    def list_revisions(
        self, *, project_id: str, limit: int, cursor: str | None
    ) -> dict[str, object] | ProjectServicePortFailure: ...

    def compare_revisions(
        self,
        *,
        project_id: str,
        from_revision: str,
        to_revision: str,
    ) -> dict[str, object] | ProjectServicePortFailure: ...


class _ApiFailure(Exception):
    __slots__ = ("code", "path")

    def __init__(self, code: ProjectApiErrorCode, path: str = "") -> None:
        self.code = code
        self.path = path
        super().__init__(code.value)


def _raise(code: ProjectApiErrorCode, path: str = "") -> None:
    raise _ApiFailure(code, path)


def _utf8_length(value: str, path: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        _raise(ProjectApiErrorCode.INVALID_VALUE, path)


def _bounded_pointer(parent: str, token: str) -> str:
    escaped = token.replace("~", "~0").replace("/", "~1")
    candidate = f"{parent}/{escaped}"
    try:
        if len(candidate.encode("utf-8")) <= _MAX_PUBLIC_ERROR_PATH_BYTES:
            return candidate
    except UnicodeEncodeError:
        pass
    return "/_truncated"


def _validate_exact_json(value: object) -> None:
    """Reject non-JSON values and resource abuse without coercion or hooks."""

    count = 0
    seen: set[int] = set()
    stack: list[tuple[object, str, int]] = [(value, "", 0)]
    while stack:
        current, path, depth = stack.pop()
        count += 1
        if count > _MAX_JSON_NODES:
            _raise(ProjectApiErrorCode.BUDGET_EXCEEDED, path)

        if current is None or type(current) is bool:
            continue
        if type(current) is int:
            if abs(current) > MAX_SAFE_JSON_INTEGER:
                _raise(ProjectApiErrorCode.INVALID_VALUE, path)
            continue
        if type(current) is float:
            if not math.isfinite(current):
                _raise(ProjectApiErrorCode.INVALID_VALUE, path)
            continue
        if type(current) is str:
            if len(current) > _MAX_REQUEST_BYTES:
                _raise(ProjectApiErrorCode.BUDGET_EXCEEDED, path)
            if _utf8_length(current, path) > _MAX_REQUEST_BYTES:
                _raise(ProjectApiErrorCode.BUDGET_EXCEEDED, path)
            continue
        if type(current) not in {dict, list}:
            _raise(ProjectApiErrorCode.INVALID_TYPE, path)
        if depth >= _MAX_JSON_DEPTH:
            _raise(ProjectApiErrorCode.BUDGET_EXCEEDED, path)

        identity = id(current)
        if identity in seen:
            _raise(ProjectApiErrorCode.INVALID_VALUE, path)
        seen.add(identity)

        if type(current) is list:
            for index in range(len(current) - 1, -1, -1):
                stack.append((current[index], _bounded_pointer(path, str(index)), depth + 1))
            continue

        items = tuple(current.items())
        for key, item in reversed(items):
            if type(key) is not str:
                _raise(ProjectApiErrorCode.INVALID_TYPE, path)
            if len(key) > _MAX_JSON_KEY_BYTES:
                _raise(ProjectApiErrorCode.BUDGET_EXCEEDED, path)
            if _utf8_length(key, path) > _MAX_JSON_KEY_BYTES:
                _raise(ProjectApiErrorCode.BUDGET_EXCEEDED, path)
            stack.append((item, _bounded_pointer(path, key), depth + 1))


def _canonical_json_size(value: object) -> int:
    total = 0
    try:
        encoder = json.JSONEncoder(
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for chunk in encoder.iterencode(value):
            total += len(chunk.encode("utf-8"))
            if total > _MAX_REQUEST_BYTES:
                return total
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _raise(ProjectApiErrorCode.INVALID_VALUE)
    return total


def _validate_request(
    request: object,
    *,
    required: frozenset[str],
    allowed: frozenset[str],
) -> dict[str, object]:
    if type(request) is not dict:
        _raise(ProjectApiErrorCode.INVALID_TYPE)
    if len(request) > _MAX_JSON_NODES:
        _raise(ProjectApiErrorCode.BUDGET_EXCEEDED)

    _validate_exact_json(request)
    if _canonical_json_size(request) > _MAX_REQUEST_BYTES:
        _raise(ProjectApiErrorCode.BUDGET_EXCEEDED)

    keys = tuple(request)
    unknown = sorted(set(keys) - allowed)
    if unknown:
        _raise(ProjectApiErrorCode.UNKNOWN_FIELD, _bounded_pointer("", unknown[0]))
    missing = sorted(required - set(keys))
    if missing:
        _raise(ProjectApiErrorCode.MISSING_FIELD, _bounded_pointer("", missing[0]))

    version = request["schema_version"]
    if type(version) is not int:
        _raise(ProjectApiErrorCode.INVALID_TYPE, "/schema_version")
    if abs(version) > MAX_SAFE_JSON_INTEGER:
        _raise(ProjectApiErrorCode.INVALID_VALUE, "/schema_version")
    if version != SCHEMA_VERSION:
        _raise(ProjectApiErrorCode.UNSUPPORTED_VERSION, "/schema_version")
    return request


def _identifier(value: object, path: str, pattern: re.Pattern[str]) -> str:
    if type(value) is not str:
        _raise(ProjectApiErrorCode.INVALID_TYPE, path)
    if pattern.fullmatch(value) is None:
        _raise(ProjectApiErrorCode.INVALID_VALUE, path)
    return value


def _project_kind(value: object) -> ProjectKind:
    if type(value) is not str:
        _raise(ProjectApiErrorCode.INVALID_TYPE, "/kind")
    try:
        return ProjectKind(value)
    except ValueError:
        _raise(ProjectApiErrorCode.INVALID_VALUE, "/kind")


def _source_path(value: object) -> str:
    if type(value) is not str:
        _raise(ProjectApiErrorCode.INVALID_TYPE, "/source_path")
    if len(value) > _MAX_SOURCE_PATH_BYTES:
        _raise(ProjectApiErrorCode.BUDGET_EXCEEDED, "/source_path")
    if _utf8_length(value, "/source_path") > _MAX_SOURCE_PATH_BYTES:
        _raise(ProjectApiErrorCode.BUDGET_EXCEEDED, "/source_path")
    if not value.startswith("/") or value == "/" or value.endswith("/"):
        _raise(ProjectApiErrorCode.INVALID_VALUE, "/source_path")
    if "\x00" in value:
        _raise(ProjectApiErrorCode.INVALID_VALUE, "/source_path")
    components = value.split("/")[1:]
    if not components or any(item in {"", ".", ".."} for item in components):
        _raise(ProjectApiErrorCode.INVALID_VALUE, "/source_path")
    return value


def _page_limit(value: object) -> int:
    if type(value) is not int:
        _raise(ProjectApiErrorCode.INVALID_TYPE, "/limit")
    if value < 1 or value > 100:
        _raise(ProjectApiErrorCode.INVALID_VALUE, "/limit")
    return value


def _page_cursor(value: object, pattern: re.Pattern[str]) -> str | None:
    if value is None:
        return None
    return _identifier(value, "/cursor", pattern)


def _valid_exact_string(value: object, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _artifact_is_valid(value: object) -> bool:
    if type(value) is not RevisionArtifactRef:
        return False
    if type(value.schema_version) is not int or value.schema_version != SCHEMA_VERSION:
        return False
    if not _valid_exact_string(value.id, _ARTIFACT_ID):
        return False
    if not _valid_exact_string(value.name, _ARTIFACT_NAME):
        return False
    if type(value.format) is not str or value.format not in {"fcstd", "step"}:
        return False
    if not _valid_exact_string(value.sha256, _DIGEST):
        return False
    return (
        type(value.size_bytes) is int
        and 0 < value.size_bytes <= MAX_SAFE_JSON_INTEGER
        and value.size_bytes <= _MAX_ARTIFACT_BYTES
    )


def _model_is_valid(value: object) -> bool:
    return _artifact_is_valid(value) and value.name == "model.FCStd" and value.format == "fcstd"


def _step_is_valid(value: object) -> bool:
    return _artifact_is_valid(value) and value.name == "model.step" and value.format == "step"


def _revision_is_valid(value: object) -> bool:
    if type(value) is not RevisionRef:
        return False
    if type(value.schema_version) is not int or value.schema_version != SCHEMA_VERSION:
        return False
    if not _valid_exact_string(value.id, _REVISION_ID):
        return False
    if not _valid_exact_string(value.project_id, _PROJECT_ID):
        return False
    if value.base_revision is not None:
        if not _valid_exact_string(value.base_revision, _REVISION_ID):
            return False
        if value.base_revision == value.id:
            return False
    if not _valid_exact_string(value.manifest_sha256, _DIGEST):
        return False
    if value.model is not None and not _model_is_valid(value.model):
        return False
    if type(value.artifacts) is not tuple:
        return False

    if value.base_revision is None:
        return len(value.artifacts) == 0
    if not _model_is_valid(value.model) or len(value.artifacts) != 1:
        return False
    step = value.artifacts[0]
    if not _step_is_valid(step) or step.id == value.model.id:
        return False
    return value.model.size_bytes + step.size_bytes <= _MAX_REVISION_BYTES


def _head_is_valid(value: object) -> bool:
    return (
        type(value) is ProjectHead
        and type(value.schema_version) is int
        and value.schema_version == SCHEMA_VERSION
        and _valid_exact_string(value.project_id, _PROJECT_ID)
        and type(value.generation) is int
        and 0 <= value.generation <= MAX_SAFE_JSON_INTEGER
        and _valid_exact_string(value.revision_id, _REVISION_ID)
        and _valid_exact_string(value.manifest_sha256, _DIGEST)
    )


def _snapshot_is_valid(*, project_id: str, head: object, revision: object) -> bool:
    if not _head_is_valid(head) or not _revision_is_valid(revision):
        return False
    if not (
        head.project_id == project_id
        and revision.project_id == project_id
        and head.revision_id == revision.id
        and head.manifest_sha256 == revision.manifest_sha256
    ):
        return False
    if head.generation == 0:
        return revision.base_revision is None
    return revision.base_revision is not None and revision.model is not None


def _artifact_projection(value: RevisionArtifactRef) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": value.id,
        "name": value.name,
        "format": value.format,
        "sha256": value.sha256,
        "size_bytes": value.size_bytes,
    }


def _revision_projection(value: RevisionRef) -> dict[str, object]:
    model = None if value.model is None else _artifact_projection(value.model)
    artifacts = [_artifact_projection(item) for item in value.artifacts]
    return {
        "schema_version": SCHEMA_VERSION,
        "id": value.id,
        "project_id": value.project_id,
        "base_revision": value.base_revision,
        "manifest_sha256": value.manifest_sha256,
        "model": model,
        "artifacts": artifacts,
    }


def _head_projection(value: ProjectHead) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": value.project_id,
        "generation": value.generation,
        "revision_id": value.revision_id,
        "manifest_sha256": value.manifest_sha256,
    }


def _project_summary_projection(value: object) -> dict[str, object]:
    keys = {
        "project_id",
        "generation",
        "revision_id",
        "manifest_sha256",
    }
    if type(value) is not dict or set(value) != keys:
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)
    project_id = value["project_id"]
    generation = value["generation"]
    revision_id = value["revision_id"]
    manifest_sha256 = value["manifest_sha256"]
    if not (
        _valid_exact_string(project_id, _PROJECT_ID)
        and type(generation) is int
        and 0 <= generation <= MAX_SAFE_JSON_INTEGER
        and _valid_exact_string(revision_id, _REVISION_ID)
        and _valid_exact_string(manifest_sha256, _DIGEST)
    ):
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "generation": generation,
        "revision_id": revision_id,
        "manifest_sha256": manifest_sha256,
    }


def _revision_summary_projection(value: object) -> dict[str, object]:
    keys = {
        "id",
        "project_id",
        "base_revision",
        "manifest_sha256",
    }
    if type(value) is not dict or set(value) != keys:
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)
    revision_id = value["id"]
    project_id = value["project_id"]
    base_revision = value["base_revision"]
    manifest_sha256 = value["manifest_sha256"]
    if not (
        _valid_exact_string(revision_id, _REVISION_ID)
        and _valid_exact_string(project_id, _PROJECT_ID)
        and (
            base_revision is None
            or (_valid_exact_string(base_revision, _REVISION_ID) and base_revision != revision_id)
        )
        and _valid_exact_string(manifest_sha256, _DIGEST)
    ):
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)
    return {
        "schema_version": SCHEMA_VERSION,
        "id": revision_id,
        "project_id": project_id,
        "base_revision": base_revision,
        "manifest_sha256": manifest_sha256,
    }


def _complete_history_is_valid(
    head: dict[str, object],
    revisions: list[dict[str, object]],
) -> bool:
    generation = head["generation"]
    if type(generation) is not int or len(revisions) != generation + 1:
        return False
    by_id = {value["id"]: value for value in revisions}
    current = head["revision_id"]
    visited: set[object] = set()
    while current is not None:
        if current in visited:
            return False
        revision = by_id.get(current)
        if revision is None:
            return False
        if not visited and revision["manifest_sha256"] != head["manifest_sha256"]:
            return False
        visited.add(current)
        current = revision["base_revision"]
    return len(visited) == len(revisions)


def _snapshot_projection(head: ProjectHead, revision: RevisionRef) -> dict[str, object]:
    return {
        "head": _head_projection(head),
        "revision": _revision_projection(revision),
    }


def _comparison_artifact_slot(
    revision: RevisionRef,
    *,
    name: str,
    format: str,
) -> RevisionArtifactRef | None:
    if (name, format) == ("model.FCStd", "fcstd"):
        return revision.model
    if (name, format) == ("model.step", "step"):
        return None if not revision.artifacts else revision.artifacts[0]
    _raise(ProjectApiErrorCode.INTERNAL_ERROR)


def _comparison_artifact_endpoint(
    value: object,
    *,
    expected: RevisionArtifactRef | None,
) -> dict[str, object] | None:
    if expected is None:
        if value is not None:
            _raise(ProjectApiErrorCode.INTERNAL_ERROR)
        return None
    if not _artifact_is_valid(value):
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)
    projected = _artifact_projection(value)
    if projected != _artifact_projection(expected):
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)
    return projected


def _comparison_projection(
    result: dict[str, object],
    *,
    project_id: str,
    from_revision: str,
    to_revision: str,
) -> dict[str, object]:
    if set(result) != {
        "project_id",
        "head",
        "from_revision",
        "to_revision",
        "ancestry",
        "base_change",
        "revision_manifest",
        "artifact_changes",
        "semantic_diff",
    }:
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)
    if type(result["project_id"]) is not str or result["project_id"] != project_id:
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)

    head = result["head"]
    left = result["from_revision"]
    right = result["to_revision"]
    if not (
        _head_is_valid(head)
        and head.project_id == project_id
        and _revision_is_valid(left)
        and left.id == from_revision
        and left.project_id == project_id
        and _revision_is_valid(right)
        and right.id == to_revision
        and right.project_id == project_id
    ):
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)
    if from_revision == to_revision and _revision_projection(left) != _revision_projection(right):
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)
    for revision in (left, right):
        if head.revision_id == revision.id and (
            head.manifest_sha256 != revision.manifest_sha256
            or (head.generation == 0) != (revision.base_revision is None)
        ):
            _raise(ProjectApiErrorCode.INTERNAL_ERROR)
    if head.generation == 0 and (
        from_revision != head.revision_id or to_revision != head.revision_id
    ):
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)

    ancestry = result["ancestry"]
    if type(ancestry) is not dict or set(ancestry) != {"verified", "relation"}:
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)
    verified = ancestry["verified"]
    relation = ancestry["relation"]
    if not (
        type(verified) is bool
        and verified is True
        and type(relation) is str
        and relation
        in {
            "same",
            "from_ancestor_of_to",
            "to_ancestor_of_from",
        }
        and (relation == "same") == (from_revision == to_revision)
    ):
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)

    base_change = result["base_change"]
    if type(base_change) is not dict or set(base_change) != {
        "changed",
        "from_base",
        "to_base",
    }:
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)
    base_changed = base_change["changed"]
    from_base = base_change["from_base"]
    to_base = base_change["to_base"]
    if not (
        type(base_changed) is bool
        and base_changed == (left.base_revision != right.base_revision)
        and (from_base is None or _valid_exact_string(from_base, _REVISION_ID))
        and (to_base is None or _valid_exact_string(to_base, _REVISION_ID))
        and from_base == left.base_revision
        and to_base == right.base_revision
    ):
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)

    revision_manifest = result["revision_manifest"]
    if type(revision_manifest) is not dict or set(revision_manifest) != {
        "changed",
        "from_sha256",
        "to_sha256",
    }:
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)
    manifest_changed = revision_manifest["changed"]
    from_sha256 = revision_manifest["from_sha256"]
    to_sha256 = revision_manifest["to_sha256"]
    if not (
        type(manifest_changed) is bool
        and manifest_changed == (left.manifest_sha256 != right.manifest_sha256)
        and _valid_exact_string(from_sha256, _DIGEST)
        and _valid_exact_string(to_sha256, _DIGEST)
        and from_sha256 == left.manifest_sha256
        and to_sha256 == right.manifest_sha256
    ):
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)

    artifact_changes = result["artifact_changes"]
    if type(artifact_changes) is not list or len(artifact_changes) != 2:
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)
    projected_changes: list[dict[str, object]] = []
    for value, (name, format) in zip(
        artifact_changes,
        (("model.FCStd", "fcstd"), ("model.step", "step")),
        strict=True,
    ):
        if type(value) is not dict or set(value) != {
            "name",
            "format",
            "change",
            "from",
            "to",
        }:
            _raise(ProjectApiErrorCode.INTERNAL_ERROR)
        change = value["change"]
        if not (
            type(value["name"]) is str
            and value["name"] == name
            and type(value["format"]) is str
            and value["format"] == format
            and type(change) is str
            and change in {"unchanged", "added", "removed", "modified"}
        ):
            _raise(ProjectApiErrorCode.INTERNAL_ERROR)
        before = _comparison_artifact_endpoint(
            value["from"],
            expected=_comparison_artifact_slot(left, name=name, format=format),
        )
        after = _comparison_artifact_endpoint(
            value["to"],
            expected=_comparison_artifact_slot(right, name=name, format=format),
        )
        if before is None and after is None:
            expected_change = "unchanged"
        elif before is None:
            expected_change = "added"
        elif after is None:
            expected_change = "removed"
        elif before == after:
            expected_change = "unchanged"
        else:
            expected_change = "modified"
        if change != expected_change:
            _raise(ProjectApiErrorCode.INTERNAL_ERROR)
        projected_changes.append(
            {
                "name": name,
                "format": format,
                "change": change,
                "from": before,
                "to": after,
            }
        )

    semantic_diff = result["semantic_diff"]
    if type(semantic_diff) is not dict or set(semantic_diff) != {"status", "scopes"}:
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)
    status = semantic_diff["status"]
    scopes = semantic_diff["scopes"]
    if not (
        type(status) is str
        and status == "unsupported"
        and type(scopes) is list
        and len(scopes) == 3
        and all(type(value) is str for value in scopes)
        and scopes == ["geometry", "entity", "parameter"]
    ):
        _raise(ProjectApiErrorCode.INTERNAL_ERROR)

    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "head": _head_projection(head),
        "from_revision": _revision_projection(left),
        "to_revision": _revision_projection(right),
        "ancestry": {
            "verified": True,
            "relation": relation,
        },
        "base_change": {
            "changed": base_changed,
            "from_base": from_base,
            "to_base": to_base,
        },
        "revision_manifest": {
            "changed": manifest_changed,
            "from_sha256": from_sha256,
            "to_sha256": to_sha256,
        },
        "artifact_changes": projected_changes,
        "semantic_diff": {
            "status": "unsupported",
            "scopes": ["geometry", "entity", "parameter"],
        },
    }


def _success(result: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "result": result,
        "error": None,
    }


def _failure(error: _ApiFailure) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "result": None,
        "error": {
            "schema_version": SCHEMA_VERSION,
            "code": error.code.value,
            "path": error.path,
            "message": _ERROR_MESSAGES[error.code],
        },
    }


class ProjectApi:
    """Strict public adapter over an injected durable project service."""

    __slots__ = ("_port",)

    def __init__(self, *, port: ProjectServicePort) -> None:
        self._port = port

    @staticmethod
    def _guard(action: Callable[[], dict[str, object]]) -> dict[str, object]:
        try:
            return _success(action())
        except _ApiFailure as error:
            return _failure(error)
        except BaseException:
            return _failure(_ApiFailure(ProjectApiErrorCode.INTERNAL_ERROR))

    @staticmethod
    def _invoke_untrusted(action: Callable[[], object]) -> object:
        try:
            return action()
        except BaseException:
            _raise(ProjectApiErrorCode.INTERNAL_ERROR)

    @staticmethod
    def _port_failure(value: object) -> None:
        if type(value) is not ProjectServicePortFailure:
            return
        if type(value.code) is not ProjectServicePortErrorCode:
            _raise(ProjectApiErrorCode.INTERNAL_ERROR)
        _raise(_PORT_ERROR_MAP[value.code])

    @classmethod
    def _mapping_port_result(cls, value: object) -> dict[str, object]:
        cls._port_failure(value)
        if type(value) is not dict:
            _raise(ProjectApiErrorCode.INTERNAL_ERROR)
        return dict(value)

    @staticmethod
    def _validated_cursor(value: object, pattern: re.Pattern[str]) -> str | None:
        if value is None:
            return None
        if type(value) is not str or pattern.fullmatch(value) is None:
            _raise(ProjectApiErrorCode.INTERNAL_ERROR)
        return value

    def create_project(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _validate_request(
                request,
                required=frozenset({"schema_version", "create_key", "kind"}),
                allowed=frozenset({"schema_version", "create_key", "kind", "source_path"}),
            )
            create_key = _identifier(data["create_key"], "/create_key", _CREATE_KEY)
            kind = _project_kind(data["kind"])
            source_path = None
            if kind is ProjectKind.EMPTY:
                if "source_path" in data:
                    _raise(ProjectApiErrorCode.UNKNOWN_FIELD, "/source_path")
            else:
                if "source_path" not in data:
                    _raise(ProjectApiErrorCode.MISSING_FIELD, "/source_path")
                source_path = _source_path(data["source_path"])

            value = self._invoke_untrusted(
                lambda: self._port.create_project(
                    create_key=create_key,
                    kind=kind,
                    source_path=source_path,
                )
            )
            self._port_failure(value)
            if type(value) is not ProjectCreateResult:
                _raise(ProjectApiErrorCode.INTERNAL_ERROR)
            if not (
                _valid_exact_string(value.create_key, _CREATE_KEY)
                and value.create_key == create_key
                and value.kind is kind
                and type(value.cleanup_required) is bool
                and _valid_exact_string(value.project_id, _PROJECT_ID)
                and _snapshot_is_valid(
                    project_id=value.project_id,
                    head=value.head,
                    revision=value.revision,
                )
                and value.head.generation == 0
                and value.revision.base_revision is None
                and len(value.revision.artifacts) == 0
                and (kind is ProjectKind.IMPORT_FCSTD or value.cleanup_required is False)
                and (
                    (kind is ProjectKind.EMPTY and value.revision.model is None)
                    or (kind is ProjectKind.IMPORT_FCSTD and _model_is_valid(value.revision.model))
                )
            ):
                _raise(ProjectApiErrorCode.INTERNAL_ERROR)
            return {
                "schema_version": SCHEMA_VERSION,
                "create_key": value.create_key,
                "kind": value.kind.value,
                "cleanup_required": value.cleanup_required,
                "project_id": value.project_id,
                "generation_zero": _snapshot_projection(value.head, value.revision),
            }

        return self._guard(action)

    def get_project(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _validate_request(
                request,
                required=frozenset({"schema_version", "project_id"}),
                allowed=frozenset({"schema_version", "project_id"}),
            )
            project_id = _identifier(data["project_id"], "/project_id", _PROJECT_ID)
            value = self._invoke_untrusted(lambda: self._port.get_project(project_id=project_id))
            self._port_failure(value)
            if type(value) is not ProjectCurrentResult:
                _raise(ProjectApiErrorCode.INTERNAL_ERROR)
            if not (
                _valid_exact_string(value.project_id, _PROJECT_ID)
                and value.project_id == project_id
                and _snapshot_is_valid(
                    project_id=value.project_id,
                    head=value.head,
                    revision=value.revision,
                )
            ):
                _raise(ProjectApiErrorCode.INTERNAL_ERROR)
            return {
                "schema_version": SCHEMA_VERSION,
                "project_id": value.project_id,
                "current": _snapshot_projection(value.head, value.revision),
            }

        return self._guard(action)

    def list_projects(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _validate_request(
                request,
                required=frozenset({"schema_version"}),
                allowed=frozenset({"schema_version", "limit", "cursor"}),
            )
            limit = _page_limit(data.get("limit", 50))
            cursor = _page_cursor(data.get("cursor"), _PROJECT_LIST_CURSOR)
            result = self._mapping_port_result(
                self._invoke_untrusted(lambda: self._port.list_projects(limit=limit, cursor=cursor))
            )
            if set(result) != {"projects", "next_cursor"}:
                _raise(ProjectApiErrorCode.INTERNAL_ERROR)
            values = result["projects"]
            if type(values) is not list or len(values) > limit:
                _raise(ProjectApiErrorCode.INTERNAL_ERROR)
            projects = [_project_summary_projection(value) for value in values]
            project_ids = [value["project_id"] for value in projects]
            if project_ids != sorted(project_ids) or len(project_ids) != len(set(project_ids)):
                _raise(ProjectApiErrorCode.INTERNAL_ERROR)
            next_cursor = self._validated_cursor(
                result["next_cursor"],
                _PROJECT_LIST_CURSOR,
            )
            if len(projects) < limit and next_cursor is not None:
                _raise(ProjectApiErrorCode.INTERNAL_ERROR)
            return {
                "schema_version": SCHEMA_VERSION,
                "projects": projects,
                "next_cursor": next_cursor,
            }

        return self._guard(action)

    def list_revisions(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _validate_request(
                request,
                required=frozenset({"schema_version", "project_id"}),
                allowed=frozenset({"schema_version", "project_id", "limit", "cursor"}),
            )
            project_id = _identifier(data["project_id"], "/project_id", _PROJECT_ID)
            limit = _page_limit(data.get("limit", 50))
            cursor = _page_cursor(data.get("cursor"), _REVISION_LIST_CURSOR)
            result = self._mapping_port_result(
                self._invoke_untrusted(
                    lambda: self._port.list_revisions(
                        project_id=project_id,
                        limit=limit,
                        cursor=cursor,
                    )
                )
            )
            if set(result) != {
                "project_id",
                "head",
                "revisions",
                "next_cursor",
            }:
                _raise(ProjectApiErrorCode.INTERNAL_ERROR)
            if type(result["project_id"]) is not str or result["project_id"] != project_id:
                _raise(ProjectApiErrorCode.INTERNAL_ERROR)
            head = _project_summary_projection(result["head"])
            if head["project_id"] != project_id:
                _raise(ProjectApiErrorCode.INTERNAL_ERROR)
            values = result["revisions"]
            if type(values) is not list or len(values) > limit:
                _raise(ProjectApiErrorCode.INTERNAL_ERROR)
            revisions = [_revision_summary_projection(value) for value in values]
            revision_ids = [value["id"] for value in revisions]
            if (
                revision_ids != sorted(revision_ids)
                or len(revision_ids) != len(set(revision_ids))
                or any(value["project_id"] != project_id for value in revisions)
            ):
                _raise(ProjectApiErrorCode.INTERNAL_ERROR)
            for revision in revisions:
                if revision["id"] == head["revision_id"] and (
                    revision["manifest_sha256"] != head["manifest_sha256"]
                ):
                    _raise(ProjectApiErrorCode.INTERNAL_ERROR)
            next_cursor = self._validated_cursor(
                result["next_cursor"],
                _REVISION_LIST_CURSOR,
            )
            if len(revisions) < limit and next_cursor is not None:
                _raise(ProjectApiErrorCode.INTERNAL_ERROR)
            if (
                cursor is None
                and next_cursor is None
                and not _complete_history_is_valid(
                    head,
                    revisions,
                )
            ):
                _raise(ProjectApiErrorCode.INTERNAL_ERROR)
            return {
                "schema_version": SCHEMA_VERSION,
                "project_id": project_id,
                "head": head,
                "revisions": revisions,
                "next_cursor": next_cursor,
            }

        return self._guard(action)

    def compare_revisions(self, request: object) -> dict[str, object]:
        def action() -> dict[str, object]:
            data = _validate_request(
                request,
                required=frozenset(
                    {
                        "schema_version",
                        "project_id",
                        "from_revision",
                        "to_revision",
                    }
                ),
                allowed=frozenset(
                    {
                        "schema_version",
                        "project_id",
                        "from_revision",
                        "to_revision",
                    }
                ),
            )
            project_id = _identifier(data["project_id"], "/project_id", _PROJECT_ID)
            from_revision = _identifier(
                data["from_revision"],
                "/from_revision",
                _REVISION_ID,
            )
            to_revision = _identifier(
                data["to_revision"],
                "/to_revision",
                _REVISION_ID,
            )
            result = self._mapping_port_result(
                self._invoke_untrusted(
                    lambda: self._port.compare_revisions(
                        project_id=project_id,
                        from_revision=from_revision,
                        to_revision=to_revision,
                    )
                )
            )
            return _comparison_projection(
                result,
                project_id=project_id,
                from_revision=from_revision,
                to_revision=to_revision,
            )

        return self._guard(action)
