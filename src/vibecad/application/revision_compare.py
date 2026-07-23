"""Verified, read-only comparison of committed CAD revision artifacts."""

from __future__ import annotations

import re
from enum import StrEnum

from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionAncestrySnapshot,
    RevisionArtifactRef,
    RevisionRef,
    RevisionSnapshotEntry,
    RevisionStoreError,
    RevisionStoreErrorCode,
)

__all__ = (
    "RevisionCompareError",
    "RevisionCompareErrorCode",
    "RevisionCompareService",
)

_PROJECT_ID = re.compile(r"^project_[0-9a-f]{32}$")
_REVISION_ID = re.compile(r"^revision_[0-9a-f]{32}$")


class RevisionCompareErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INTEGRITY_FAILURE = "integrity_failure"
    STORE_FAILURE = "store_failure"
    RECOVERY_REQUIRED = "recovery_required"


class RevisionCompareError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: RevisionCompareErrorCode) -> None:
        if type(code) is not RevisionCompareErrorCode:
            raise TypeError("code must be an exact RevisionCompareErrorCode")
        self.code = code
        super().__init__(code.value)


def _raise(code: RevisionCompareErrorCode) -> None:
    raise RevisionCompareError(code)


def _identifier(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _raise(RevisionCompareErrorCode.INVALID_INPUT)
    return value


def _store_failure(error: RevisionStoreError, *, known_revision: bool = False) -> None:
    if type(error) is not RevisionStoreError or type(error.code) is not RevisionStoreErrorCode:
        _raise(RevisionCompareErrorCode.STORE_FAILURE)
    mapping = {
        RevisionStoreErrorCode.INVALID_IDENTIFIER: RevisionCompareErrorCode.INVALID_INPUT,
        RevisionStoreErrorCode.INVALID_INPUT: RevisionCompareErrorCode.INVALID_INPUT,
        RevisionStoreErrorCode.NOT_FOUND: (
            RevisionCompareErrorCode.INTEGRITY_FAILURE
            if known_revision
            else RevisionCompareErrorCode.NOT_FOUND
        ),
        RevisionStoreErrorCode.ALREADY_EXISTS: RevisionCompareErrorCode.STORE_FAILURE,
        RevisionStoreErrorCode.CONFLICT: RevisionCompareErrorCode.INTEGRITY_FAILURE,
        RevisionStoreErrorCode.CORRUPT_RECORD: RevisionCompareErrorCode.INTEGRITY_FAILURE,
        RevisionStoreErrorCode.CORRUPT_CONTENT: RevisionCompareErrorCode.INTEGRITY_FAILURE,
        RevisionStoreErrorCode.BUDGET_EXCEEDED: RevisionCompareErrorCode.RESOURCE_EXHAUSTED,
        RevisionStoreErrorCode.RESOURCE_EXHAUSTED: (RevisionCompareErrorCode.RESOURCE_EXHAUSTED),
        RevisionStoreErrorCode.UNSAFE_STORE: RevisionCompareErrorCode.INTEGRITY_FAILURE,
        RevisionStoreErrorCode.INVALID_LEASE: RevisionCompareErrorCode.STORE_FAILURE,
        RevisionStoreErrorCode.IO_ERROR: RevisionCompareErrorCode.STORE_FAILURE,
        RevisionStoreErrorCode.DURABILITY_UNCERTAIN: (RevisionCompareErrorCode.RECOVERY_REQUIRED),
        RevisionStoreErrorCode.RECOVERY_REQUIRED: RevisionCompareErrorCode.RECOVERY_REQUIRED,
        RevisionStoreErrorCode.CLEANUP_REQUIRED: RevisionCompareErrorCode.RECOVERY_REQUIRED,
    }
    _raise(mapping[error.code])


def _snapshot(store: LocalRevisionStore, project_id: str) -> RevisionAncestrySnapshot:
    try:
        value = store.snapshot_revisions(project_id)
    except RevisionStoreError as error:
        _store_failure(error)
    except Exception:
        _raise(RevisionCompareErrorCode.STORE_FAILURE)
    if type(value) is not RevisionAncestrySnapshot or value.project_id != project_id:
        _raise(RevisionCompareErrorCode.STORE_FAILURE)
    return value


def _entry_map(
    snapshot: RevisionAncestrySnapshot,
) -> dict[str, RevisionSnapshotEntry]:
    result: dict[str, RevisionSnapshotEntry] = {}
    for entry in snapshot.revisions:
        if (
            type(entry) is not RevisionSnapshotEntry
            or entry.project_id != snapshot.project_id
            or entry.id in result
        ):
            _raise(RevisionCompareErrorCode.INTEGRITY_FAILURE)
        result[entry.id] = entry
    return result


def _load(
    store: LocalRevisionStore,
    *,
    project_id: str,
    revision_id: str,
    expected: RevisionSnapshotEntry,
) -> RevisionRef:
    try:
        value = store.load_revision(project_id, revision_id)
    except RevisionStoreError as error:
        _store_failure(error, known_revision=True)
    except Exception:
        _raise(RevisionCompareErrorCode.STORE_FAILURE)
    if (
        type(value) is not RevisionRef
        or value.id != expected.id
        or value.project_id != expected.project_id
        or value.base_revision != expected.base_revision
        or value.manifest_sha256 != expected.manifest_sha256
    ):
        _raise(RevisionCompareErrorCode.INTEGRITY_FAILURE)
    return value


def _relation(
    entries: dict[str, RevisionSnapshotEntry],
    from_revision: str,
    to_revision: str,
) -> str:
    if from_revision == to_revision:
        return "same"
    current = to_revision
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        entry = entries.get(current)
        if entry is None or entry.base_revision is None:
            break
        if entry.base_revision == from_revision:
            return "from_ancestor_of_to"
        current = entry.base_revision
    current = from_revision
    seen.clear()
    while current not in seen:
        seen.add(current)
        entry = entries.get(current)
        if entry is None or entry.base_revision is None:
            break
        if entry.base_revision == to_revision:
            return "to_ancestor_of_from"
        current = entry.base_revision
    _raise(RevisionCompareErrorCode.INTEGRITY_FAILURE)


def _slot(
    revision: RevisionRef,
    *,
    name: str,
    format: str,
) -> RevisionArtifactRef | None:
    values = (() if revision.model is None else (revision.model,)) + revision.artifacts
    matches = tuple(item for item in values if (item.name, item.format) == (name, format))
    if len(matches) > 1:
        _raise(RevisionCompareErrorCode.INTEGRITY_FAILURE)
    return None if not matches else matches[0]


def _artifact_change(
    *,
    name: str,
    format: str,
    before: RevisionArtifactRef | None,
    after: RevisionArtifactRef | None,
) -> dict[str, object]:
    if before is None and after is None:
        change = "unchanged"
    elif before is None:
        change = "added"
    elif after is None:
        change = "removed"
    elif before == after:
        change = "unchanged"
    else:
        change = "modified"
    return {
        "name": name,
        "format": format,
        "change": change,
        "from": before,
        "to": after,
    }


class RevisionCompareService:
    """Compare two fully verified revisions from current committed ancestry."""

    __slots__ = ("_store",)

    def __init__(self, *, store: LocalRevisionStore) -> None:
        if type(store) is not LocalRevisionStore:
            _raise(RevisionCompareErrorCode.INVALID_INPUT)
        self._store = store

    def compare_revisions(
        self,
        *,
        project_id: object,
        from_revision: object,
        to_revision: object,
    ) -> dict[str, object]:
        project = _identifier(project_id, _PROJECT_ID)
        left_id = _identifier(from_revision, _REVISION_ID)
        right_id = _identifier(to_revision, _REVISION_ID)
        before = _snapshot(self._store, project)
        entries = _entry_map(before)
        if left_id not in entries or right_id not in entries:
            _raise(RevisionCompareErrorCode.NOT_FOUND)
        left = _load(
            self._store,
            project_id=project,
            revision_id=left_id,
            expected=entries[left_id],
        )
        right = (
            left
            if right_id == left_id
            else _load(
                self._store,
                project_id=project,
                revision_id=right_id,
                expected=entries[right_id],
            )
        )
        after = _snapshot(self._store, project)
        if after != before:
            _raise(RevisionCompareErrorCode.CONFLICT)
        artifact_changes = [
            _artifact_change(
                name=name,
                format=format,
                before=_slot(left, name=name, format=format),
                after=_slot(right, name=name, format=format),
            )
            for name, format in (("model.FCStd", "fcstd"), ("model.step", "step"))
        ]
        return {
            "project_id": project,
            "head": before.head,
            "from_revision": left,
            "to_revision": right,
            "ancestry": {
                "verified": True,
                "relation": _relation(entries, left_id, right_id),
            },
            "base_change": {
                "changed": left.base_revision != right.base_revision,
                "from_base": left.base_revision,
                "to_base": right.base_revision,
            },
            "revision_manifest": {
                "changed": left.manifest_sha256 != right.manifest_sha256,
                "from_sha256": left.manifest_sha256,
                "to_sha256": right.manifest_sha256,
            },
            "artifact_changes": artifact_changes,
            "semantic_diff": {
                "status": "unsupported",
                "scopes": ["geometry", "entity", "parameter"],
            },
        }
