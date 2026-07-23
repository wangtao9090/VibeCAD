"""Pure, bounded project and committed-revision discovery."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum

from vibecad.execution.revisions import (
    LocalRevisionStore,
    ProjectSnapshotEntry,
    RevisionAncestrySnapshot,
    RevisionSnapshotEntry,
    RevisionStoreError,
    RevisionStoreErrorCode,
)

__all__ = (
    "RevisionDiscoveryError",
    "RevisionDiscoveryErrorCode",
    "RevisionDiscoveryService",
)

_PROJECT_CURSOR = re.compile(r"^project_list_cursor_[0-9a-f]{64}$")
_REVISION_CURSOR = re.compile(r"^revision_list_cursor_[0-9a-f]{64}$")
_PROJECT_ID = re.compile(r"^project_[0-9a-f]{32}$")
_PROJECT_CURSOR_DOMAIN = b"vibecad-project-list-cursor-v1\0"
_REVISION_CURSOR_DOMAIN = b"vibecad-revision-list-cursor-v1\0"
_PROJECT_SNAPSHOT_DOMAIN = b"vibecad-project-list-snapshot-v1\0"
_REVISION_SNAPSHOT_DOMAIN = b"vibecad-revision-list-snapshot-v1\0"
_MAX_PROJECTS = 4096
_MAX_REVISIONS = 8192


class RevisionDiscoveryErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INTEGRITY_FAILURE = "integrity_failure"
    STORE_FAILURE = "store_failure"
    RECOVERY_REQUIRED = "recovery_required"


class RevisionDiscoveryError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: RevisionDiscoveryErrorCode) -> None:
        if type(code) is not RevisionDiscoveryErrorCode:
            raise TypeError("code must be an exact RevisionDiscoveryErrorCode")
        self.code = code
        super().__init__(code.value)


def _raise(code: RevisionDiscoveryErrorCode) -> None:
    raise RevisionDiscoveryError(code)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _limit(value: object) -> int:
    if type(value) is not int or value < 1 or value > 100:
        _raise(RevisionDiscoveryErrorCode.INVALID_INPUT)
    return value


def _cursor(domain: bytes, digest: bytes, offset: int, prefix: str) -> str:
    token = hashlib.sha256(domain + digest + offset.to_bytes(8, "big")).hexdigest()
    return f"{prefix}{token}"


def _offset(
    *,
    cursor: object,
    pattern: re.Pattern[str],
    prefix: str,
    domain: bytes,
    digest: bytes,
    count: int,
) -> int:
    if cursor is None:
        return 0
    if type(cursor) is not str or pattern.fullmatch(cursor) is None:
        _raise(RevisionDiscoveryErrorCode.INVALID_INPUT)
    for candidate in range(1, count + 1):
        if _cursor(domain, digest, candidate, prefix) == cursor:
            return candidate
    _raise(RevisionDiscoveryErrorCode.CONFLICT)


def _store_error(error: RevisionStoreError) -> None:
    mapping = {
        RevisionStoreErrorCode.INVALID_IDENTIFIER: (RevisionDiscoveryErrorCode.INVALID_INPUT),
        RevisionStoreErrorCode.INVALID_INPUT: RevisionDiscoveryErrorCode.INVALID_INPUT,
        RevisionStoreErrorCode.NOT_FOUND: RevisionDiscoveryErrorCode.NOT_FOUND,
        RevisionStoreErrorCode.CONFLICT: RevisionDiscoveryErrorCode.CONFLICT,
        RevisionStoreErrorCode.CORRUPT_RECORD: (RevisionDiscoveryErrorCode.INTEGRITY_FAILURE),
        RevisionStoreErrorCode.CORRUPT_CONTENT: (RevisionDiscoveryErrorCode.INTEGRITY_FAILURE),
        RevisionStoreErrorCode.BUDGET_EXCEEDED: (RevisionDiscoveryErrorCode.RESOURCE_EXHAUSTED),
        RevisionStoreErrorCode.RESOURCE_EXHAUSTED: (RevisionDiscoveryErrorCode.RESOURCE_EXHAUSTED),
        RevisionStoreErrorCode.RECOVERY_REQUIRED: (RevisionDiscoveryErrorCode.RECOVERY_REQUIRED),
        RevisionStoreErrorCode.CLEANUP_REQUIRED: (RevisionDiscoveryErrorCode.RECOVERY_REQUIRED),
        RevisionStoreErrorCode.DURABILITY_UNCERTAIN: (RevisionDiscoveryErrorCode.RECOVERY_REQUIRED),
    }
    _raise(mapping.get(error.code, RevisionDiscoveryErrorCode.STORE_FAILURE))


def _project_summary(entry: ProjectSnapshotEntry) -> dict[str, object]:
    return {
        "project_id": entry.project_id,
        "generation": entry.generation,
        "revision_id": entry.revision_id,
        "manifest_sha256": entry.manifest_sha256,
    }


def _revision_summary(entry: RevisionSnapshotEntry) -> dict[str, object]:
    return {
        "id": entry.id,
        "project_id": entry.project_id,
        "base_revision": entry.base_revision,
        "manifest_sha256": entry.manifest_sha256,
    }


def _head_summary(snapshot: RevisionAncestrySnapshot) -> dict[str, object]:
    return {
        "project_id": snapshot.head.project_id,
        "generation": snapshot.head.generation,
        "revision_id": snapshot.head.revision_id,
        "manifest_sha256": snapshot.head.manifest_sha256,
    }


def _project_digest(
    namespace: bytes,
    records: tuple[ProjectSnapshotEntry, ...],
) -> bytes:
    digest = hashlib.sha256(_PROJECT_SNAPSHOT_DOMAIN + namespace)
    for entry in records:
        raw = _canonical(
            (
                entry.project_id,
                entry.generation,
                entry.revision_id,
                entry.manifest_sha256,
                entry.state_sha256,
            )
        )
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.digest()


def _revision_digest(
    namespace: bytes,
    snapshot: RevisionAncestrySnapshot,
) -> bytes:
    digest = hashlib.sha256(
        _REVISION_SNAPSHOT_DOMAIN
        + namespace
        + snapshot.project_id.encode("ascii")
        + bytes.fromhex(snapshot.state_sha256)
    )
    digest.update(_canonical(_head_summary(snapshot)))
    for entry in snapshot.revisions:
        raw = _canonical(_revision_summary(entry))
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.digest()


class RevisionDiscoveryService:
    __slots__ = ("_store",)

    def __init__(self, *, store: LocalRevisionStore) -> None:
        if type(store) is not LocalRevisionStore:
            _raise(RevisionDiscoveryErrorCode.INVALID_INPUT)
        self._store = store

    def list_projects(
        self,
        *,
        limit: object = 50,
        cursor: object = None,
    ) -> dict[str, object]:
        selected_limit = _limit(limit)
        try:
            records = self._store.snapshot_projects()
            namespace = self._store.discovery_namespace()
        except RevisionStoreError as error:
            _store_error(error)
        except Exception:
            _raise(RevisionDiscoveryErrorCode.STORE_FAILURE)
        if (
            type(records) is not tuple
            or len(records) > _MAX_PROJECTS
            or not all(type(item) is ProjectSnapshotEntry for item in records)
        ):
            _raise(RevisionDiscoveryErrorCode.STORE_FAILURE)
        if type(namespace) is not bytes or len(namespace) != 32:
            _raise(RevisionDiscoveryErrorCode.STORE_FAILURE)
        digest = _project_digest(namespace, records)
        start = _offset(
            cursor=cursor,
            pattern=_PROJECT_CURSOR,
            prefix="project_list_cursor_",
            domain=_PROJECT_CURSOR_DOMAIN,
            digest=digest,
            count=len(records),
        )
        end = min(start + selected_limit, len(records))
        next_cursor = None
        if end < len(records):
            next_cursor = _cursor(
                _PROJECT_CURSOR_DOMAIN,
                digest,
                end,
                "project_list_cursor_",
            )
        return {
            "projects": [_project_summary(item) for item in records[start:end]],
            "next_cursor": next_cursor,
        }

    def list_revisions(
        self,
        *,
        project_id: object,
        limit: object = 50,
        cursor: object = None,
    ) -> dict[str, object]:
        selected_limit = _limit(limit)
        if type(project_id) is not str or _PROJECT_ID.fullmatch(project_id) is None:
            _raise(RevisionDiscoveryErrorCode.INVALID_INPUT)
        try:
            snapshot = self._store.snapshot_revisions(project_id)
            namespace = self._store.discovery_namespace()
        except RevisionStoreError as error:
            _store_error(error)
        except Exception:
            _raise(RevisionDiscoveryErrorCode.STORE_FAILURE)
        if (
            type(snapshot) is not RevisionAncestrySnapshot
            or len(snapshot.revisions) > _MAX_REVISIONS
            or type(namespace) is not bytes
            or len(namespace) != 32
        ):
            _raise(RevisionDiscoveryErrorCode.STORE_FAILURE)
        digest = _revision_digest(namespace, snapshot)
        start = _offset(
            cursor=cursor,
            pattern=_REVISION_CURSOR,
            prefix="revision_list_cursor_",
            domain=_REVISION_CURSOR_DOMAIN,
            digest=digest,
            count=len(snapshot.revisions),
        )
        end = min(start + selected_limit, len(snapshot.revisions))
        next_cursor = None
        if end < len(snapshot.revisions):
            next_cursor = _cursor(
                _REVISION_CURSOR_DOMAIN,
                digest,
                end,
                "revision_list_cursor_",
            )
        return {
            "project_id": project_id,
            "head": _head_summary(snapshot),
            "revisions": [_revision_summary(item) for item in snapshot.revisions[start:end]],
            "next_cursor": next_cursor,
        }
