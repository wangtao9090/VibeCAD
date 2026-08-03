"""Pure, bounded task discovery over the durable task catalog."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum

from vibecad.workflow.catalog import (
    TaskCatalogError,
    TaskCatalogErrorCode,
    TaskCatalogService,
)
from vibecad.workflow.store import TaskSnapshotEntry

__all__ = (
    "TaskDiscoveryError",
    "TaskDiscoveryErrorCode",
    "TaskDiscoveryService",
)

_LIST_CURSOR = re.compile(r"^task_list_cursor_[0-9a-f]{64}$")
_EVENT_CURSOR = re.compile(r"^task_event_cursor_[0-9a-f]{64}$")
_TASK_ID = re.compile(r"^task_[0-9a-f]{32}$")
_LIST_DOMAIN = b"vibecad-task-list-cursor-v1\0"
_EVENT_DOMAIN = b"vibecad-task-event-cursor-v1\0"
_SNAPSHOT_DOMAIN = b"vibecad-task-list-snapshot-v1\0"
_MAX_TASKS = 1024
_MAX_EVENTS = 128


class TaskDiscoveryErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    STORE_FAILURE = "store_failure"


class TaskDiscoveryError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: TaskDiscoveryErrorCode) -> None:
        if type(code) is not TaskDiscoveryErrorCode:
            raise TypeError("code must be an exact TaskDiscoveryErrorCode")
        self.code = code
        super().__init__(code.value)


def _raise(code: TaskDiscoveryErrorCode) -> None:
    raise TaskDiscoveryError(code)


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
        _raise(TaskDiscoveryErrorCode.INVALID_INPUT)
    return value


def _snapshot_digest(
    namespace: bytes,
    records: tuple[TaskSnapshotEntry, ...],
) -> bytes:
    digest = hashlib.sha256(_SNAPSHOT_DOMAIN + namespace)
    for entry in records:
        raw = _canonical((entry.task_id, entry.generation, entry.record_sha256))
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.digest()


def _cursor(domain: bytes, snapshot_digest: bytes, offset: int, prefix: str) -> str:
    token = hashlib.sha256(domain + snapshot_digest + offset.to_bytes(8, "big")).hexdigest()
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
        _raise(TaskDiscoveryErrorCode.INVALID_INPUT)
    for candidate in range(1, count + 1):
        if _cursor(domain, digest, candidate, prefix) == cursor:
            return candidate
    _raise(TaskDiscoveryErrorCode.CONFLICT)


def _catalog_error(error: TaskCatalogError) -> None:
    mapping = {
        TaskCatalogErrorCode.INVALID_INPUT: TaskDiscoveryErrorCode.INVALID_INPUT,
        TaskCatalogErrorCode.NOT_FOUND: TaskDiscoveryErrorCode.NOT_FOUND,
        TaskCatalogErrorCode.CONFLICT: TaskDiscoveryErrorCode.CONFLICT,
        TaskCatalogErrorCode.RESOURCE_EXHAUSTED: (TaskDiscoveryErrorCode.RESOURCE_EXHAUSTED),
    }
    _raise(mapping.get(error.code, TaskDiscoveryErrorCode.STORE_FAILURE))


def _summary(entry: TaskSnapshotEntry) -> dict[str, object]:
    return {
        "task_id": entry.task_id,
        "project_id": entry.project_id,
        "generation": entry.generation,
        "base_revision": entry.base_revision,
        "reasoning_owner": entry.reasoning_owner,
        "review_policy": entry.review_policy,
        "status": entry.status,
        "next_action": entry.next_action,
        "candidate_revision": entry.candidate_revision,
        "committed_revision": entry.committed_revision,
        "draft_id": entry.draft_id,
    }


class TaskDiscoveryService:
    __slots__ = ("_catalog",)

    def __init__(self, *, catalog: TaskCatalogService) -> None:
        if type(catalog) is not TaskCatalogService:
            _raise(TaskDiscoveryErrorCode.INVALID_INPUT)
        self._catalog = catalog

    def list_tasks(self, *, limit: object = 50, cursor: object = None) -> dict[str, object]:
        selected_limit = _limit(limit)
        try:
            records = self._catalog.snapshot_tasks()
            namespace = self._catalog.discovery_namespace()
        except TaskCatalogError as error:
            _catalog_error(error)
        if len(records) > _MAX_TASKS:
            _raise(TaskDiscoveryErrorCode.RESOURCE_EXHAUSTED)
        digest = _snapshot_digest(namespace, records)
        start = _offset(
            cursor=cursor,
            pattern=_LIST_CURSOR,
            prefix="task_list_cursor_",
            domain=_LIST_DOMAIN,
            digest=digest,
            count=len(records),
        )
        end = min(start + selected_limit, len(records))
        next_cursor = None
        if end < len(records):
            next_cursor = _cursor(
                _LIST_DOMAIN,
                digest,
                end,
                "task_list_cursor_",
            )
        return {
            "tasks": [_summary(entry) for entry in records[start:end]],
            "next_cursor": next_cursor,
        }

    def get_task_events(
        self,
        *,
        task_id: object,
        limit: object = 50,
        cursor: object = None,
    ) -> dict[str, object]:
        selected_limit = _limit(limit)
        if type(task_id) is not str or _TASK_ID.fullmatch(task_id) is None:
            _raise(TaskDiscoveryErrorCode.INVALID_INPUT)
        try:
            stored = self._catalog.get_task(task_id=task_id)
            namespace = self._catalog.discovery_namespace()
        except TaskCatalogError as error:
            _catalog_error(error)
        transitions = stored.task_run.transitions
        if len(transitions) > _MAX_EVENTS:
            _raise(TaskDiscoveryErrorCode.RESOURCE_EXHAUSTED)
        digest = hashlib.sha256(
            _EVENT_DOMAIN
            + namespace
            + task_id.encode("ascii")
            + stored.generation.to_bytes(8, "big")
            + _canonical([item.to_mapping() for item in transitions])
        ).digest()
        start = _offset(
            cursor=cursor,
            pattern=_EVENT_CURSOR,
            prefix="task_event_cursor_",
            domain=_EVENT_DOMAIN,
            digest=digest,
            count=len(transitions),
        )
        end = min(start + selected_limit, len(transitions))
        next_cursor = None
        if end < len(transitions):
            next_cursor = _cursor(
                _EVENT_DOMAIN,
                digest,
                end,
                "task_event_cursor_",
            )
        return {
            "task_id": task_id,
            "generation": stored.generation,
            "transitions": [item.to_mapping() for item in transitions[start:end]],
            "next_cursor": next_cursor,
        }
