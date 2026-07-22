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


def _snapshot_projection(head: ProjectHead, revision: RevisionRef) -> dict[str, object]:
    return {
        "head": _head_projection(head),
        "revision": _revision_projection(revision),
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
