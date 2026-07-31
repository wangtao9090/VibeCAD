"""Required phase, correlation, success-vector, and late-error rules."""

from __future__ import annotations

from typing import NamedTuple

from .journal import AuditDocument

FLW_001 = "A16-FLW-001"
COR_001 = "A16-COR-001"
SUC_001 = "A16-SUC-001"
ERR_001 = "A16-ERR-001"

EXPECTED_PHASES = (
    "preview",
    "preview",
    "refresh",
    "refresh",
    "refresh",
    "refresh",
    "refresh",
    "review",
)
EXPECTED_SUCCESSES = (
    "preview_opened",
    "preview_opened",
    "project",
    "task",
    "preview_refreshed",
    "preview_refreshed",
    "tasks",
    "review",
)
EXPECTED_OPERATIONS = (
    "preview_open",
    "preview_open",
    "refresh_project",
    "refresh_task",
    "preview_refresh",
    "preview_refresh",
    "list_tasks",
    "review",
)


class EventBinding(NamedTuple):
    emitter: object
    role: object
    operation: object
    request_id: object
    callback_id: object
    request_sha256: object
    callback_sha256: object


EXPECTED_BINDINGS = tuple(
    EventBinding(
        "probe_wrapper",
        "gui",
        operation,
        ordinal,
        ordinal,
        "a" * 64,
        "b" * 64,
    )
    for ordinal, operation in enumerate(EXPECTED_OPERATIONS, start=1)
)
EXPECTED_REQUEST_CALLBACKS = tuple(
    (
        binding.operation,
        binding.request_id,
        binding.callback_id,
        binding.request_sha256,
        binding.callback_sha256,
    )
    for binding in EXPECTED_BINDINGS
)


def event_bindings(document: AuditDocument) -> tuple[EventBinding, ...]:
    """Bind every request and callback fact to its exact event position."""
    return tuple(
        EventBinding(
            record["emitter"],
            record["role"],
            record["correlation"].get("operation"),
            record["correlation"].get("request_id"),
            record["correlation"].get("callback_id"),
            record["digests"].get("request_sha256"),
            record["digests"].get("callback_sha256"),
        )
        for record in document.events
    )


def check_flow(document: AuditDocument) -> tuple[str, ...]:
    events = document.events
    bindings = event_bindings(document)
    findings: set[str] = set()
    if tuple(record["phase"] for record in events) != EXPECTED_PHASES:
        findings.add(FLW_001)
    if tuple(record["event"] for record in events) != EXPECTED_SUCCESSES:
        findings.add(SUC_001)
    request_callbacks = tuple(
        (
            binding.operation,
            binding.request_id,
            binding.callback_id,
            binding.request_sha256,
            binding.callback_sha256,
        )
        for binding in bindings
    )
    if request_callbacks != EXPECTED_REQUEST_CALLBACKS:
        findings.add(COR_001)
    if any(record["outcome"] != "success" or record["error_code"] is not None for record in events):
        findings.add(ERR_001)
    return tuple(sorted(findings))
