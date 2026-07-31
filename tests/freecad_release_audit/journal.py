"""Controller-only bounded canonical JSONL writer for release audit v1.

The prefix digest detects inconsistent logs; it is neither authentication nor
crash-durable storage. A close or write failure before a terminal record is a
fail-closed incomplete audit.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import BinaryIO

MAX_RECORD_BYTES = 16_384
MAX_RECORDS = 4_096
MAX_RUN_BYTES = 4 * 1_048_576
MAX_ID_BYTES = 128
MAX_CODE_BYTES = 64

_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_EVENT_OUTCOMES = {"started", "observed", "success", "failure"}
_TERMINAL_OUTCOMES = {"success", "failure"}
_CORRELATION_FIELDS = {"request_id", "callback_id", "operation", "checkout_id", "grant_id"}
_IDENTITY_FIELDS = {
    "daemon_id",
    "daemon_pid",
    "gui_pid",
    "project_id",
    "task_id",
    "draft_id",
    "revision_id",
    "candidate_revision",
    "checkout_id",
    "grant_id",
}
_EVENT_DIGEST_FIELDS = {
    "request_sha256",
    "response_sha256",
    "callback_sha256",
    "result_sha256",
    "screenshot_sha256",
}
_COUNT_FIELDS = {
    "checkout_count",
    "grant_count",
    "document_count",
    "pending_request_count",
    "dropped_frame_count",
    "truncated_log_count",
}
_EVENT_FIELDS = {
    "schema_version",
    "type",
    "run_id",
    "seq",
    "monotonic_ns",
    "emitter",
    "role",
    "phase",
    "event",
    "correlation",
    "identities",
    "outcome",
    "error_code",
    "digests",
    "counts",
}
_TERMINAL_FIELDS = {
    "schema_version",
    "type",
    "run_id",
    "seq",
    "monotonic_ns",
    "emitter",
    "role",
    "phase",
    "event",
    "outcome",
    "error_code",
    "identities",
    "last_event_seq",
    "record_count",
    "prefix_sha256",
    "elapsed_ns",
    "deadline_ns",
    "planned_exits",
    "observed_exits",
    "hashes",
    "digests",
    "resources",
    "dropped_frames",
    "truncated_logs",
}
_EXIT_FIELDS = {"daemon", "gui"}
_HASH_FIELDS = {
    "product_before_sha256",
    "product_after_sha256",
    "runtime_before_sha256",
    "runtime_after_sha256",
}
_TERMINAL_DIGEST_FIELDS = {
    "result_sha256",
    "screenshot_sha256",
    "daemon_stdout_sha256",
    "daemon_stderr_sha256",
    "gui_stdout_sha256",
    "gui_stderr_sha256",
}
_RESOURCE_FIELDS = {
    "open_checkouts",
    "file_grants",
    "documents",
    "pending_requests",
    "active_daemons",
    "active_sockets",
    "active_gui_processes",
}


class JournalError(RuntimeError):
    """Base class for release-audit journal failures."""


class JournalLimitError(JournalError):
    """A record or run would exceed its fixed allocation."""


class JournalStateError(JournalError):
    """A caller attempted an invalid writer state transition."""


@dataclass(frozen=True, slots=True)
class Limits:
    record_bytes: int = MAX_RECORD_BYTES
    records: int = MAX_RECORDS
    run_bytes: int = MAX_RUN_BYTES

    def __post_init__(self) -> None:
        if (
            type(self.record_bytes) is not int
            or not 128 <= self.record_bytes <= MAX_RECORD_BYTES
            or type(self.records) is not int
            or not 1 <= self.records <= MAX_RECORDS
            or type(self.run_bytes) is not int
            or not self.record_bytes <= self.run_bytes <= MAX_RUN_BYTES
        ):
            raise ValueError("invalid journal limits")


def _closed(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise JournalError(f"{label} must have a closed schema")
    return value


def _code(value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or len(value.encode("utf-8")) > MAX_CODE_BYTES:
        raise JournalError("invalid enum code")
    if _CODE.fullmatch(value) is None:
        raise JournalError("invalid enum code")
    return value


def _identifier(value: object) -> str:
    if type(value) is not str or len(value.encode("utf-8")) > MAX_ID_BYTES:
        raise JournalError("invalid identifier")
    if _IDENTIFIER.fullmatch(value) is None:
        raise JournalError("invalid identifier")
    return value


def _integer(value: object, *, optional: bool = False, signed: bool = False) -> int | None:
    if optional and value is None:
        return None
    if type(value) is not int or (not signed and value < 0):
        raise JournalError("invalid integer")
    return value


def _digest(value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise JournalError("invalid SHA-256")
    return value


def _mapping(value: object, fields: set[str], label: str) -> dict[str, object]:
    if value is None:
        return {}
    if type(value) is not dict or not set(value) <= fields:
        raise JournalError(f"{label} has an unknown field")
    return dict(value)


def _validate_identities(value: object) -> dict[str, object]:
    result = _mapping(value, _IDENTITY_FIELDS, "identities")
    for name, item in result.items():
        if name.endswith("_pid"):
            _integer(item)
        else:
            _identifier(item)
    return result


def _validate_correlation(value: object) -> dict[str, object]:
    result = _mapping(value, _CORRELATION_FIELDS, "correlation")
    for name, item in result.items():
        if name.endswith("_id") and name not in {"checkout_id", "grant_id"}:
            _integer(item)
        elif name == "operation":
            _code(item)
        else:
            _identifier(item)
    return result


def _validate_digests(value: object, fields: set[str]) -> dict[str, object]:
    result = _mapping(value, fields, "digests")
    for item in result.values():
        _digest(item)
    return result


def _validate_counts(value: object) -> dict[str, object]:
    result = _mapping(value, _COUNT_FIELDS, "counts")
    for item in result.values():
        _integer(item)
    return result


def canonical_line(record: object, *, maximum: int = MAX_RECORD_BYTES) -> bytes:
    """Serialize one canonical newline-terminated JSON object."""
    if type(maximum) is not int or not 1 <= maximum <= MAX_RECORD_BYTES:
        raise ValueError("invalid record bound")
    if type(record) is not dict:
        raise JournalError("journal record must be an object")
    try:
        raw = (
            json.dumps(
                record,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise JournalError("record is not canonical JSON data") from error
    if len(raw) > maximum:
        raise JournalLimitError("record exceeds byte limit")
    return raw


def validate_event(record: object) -> dict[str, object]:
    """Validate the closed runtime counterpart of event.schema.json."""
    value = _closed(record, _EVENT_FIELDS, "event")
    if value["schema_version"] != 1 or value["type"] != "event":
        raise JournalError("invalid event version or type")
    _identifier(value["run_id"])
    _integer(value["seq"])
    if value["seq"] < 1:
        raise JournalError("event sequence must be positive")
    _integer(value["monotonic_ns"])
    for name in ("emitter", "role", "phase", "event"):
        _code(value[name])
    if value["outcome"] not in _EVENT_OUTCOMES:
        raise JournalError("invalid event outcome")
    _code(value["error_code"], optional=True)
    _validate_correlation(value["correlation"])
    _validate_identities(value["identities"])
    _validate_digests(value["digests"], _EVENT_DIGEST_FIELDS)
    _validate_counts(value["counts"])
    return value


def validate_terminal(record: object) -> dict[str, object]:
    """Validate the closed runtime counterpart of terminal.schema.json."""
    value = _closed(record, _TERMINAL_FIELDS, "terminal")
    fixed = (
        value["schema_version"],
        value["type"],
        value["emitter"],
        value["role"],
        value["phase"],
        value["event"],
    )
    if fixed != (1, "terminal", "controller", "controller", "terminal", "terminal_snapshot"):
        raise JournalError("invalid terminal version or fixed codes")
    _identifier(value["run_id"])
    for name in (
        "seq",
        "monotonic_ns",
        "last_event_seq",
        "record_count",
        "elapsed_ns",
        "deadline_ns",
        "dropped_frames",
        "truncated_logs",
    ):
        _integer(value[name])
    if value["seq"] < 1 or value["record_count"] < 1:
        raise JournalError("terminal sequence and count must be positive")
    if value["outcome"] not in _TERMINAL_OUTCOMES:
        raise JournalError("invalid terminal outcome")
    _code(value["error_code"], optional=True)
    _digest(value["prefix_sha256"])
    _validate_identities(value["identities"])
    for label in ("planned_exits", "observed_exits"):
        exits = _closed(value[label], _EXIT_FIELDS, label)
        for item in exits.values():
            _integer(item, optional=True, signed=True)
    hashes = _closed(value["hashes"], _HASH_FIELDS, "hashes")
    for item in hashes.values():
        _digest(item)
    digests = _closed(value["digests"], _TERMINAL_DIGEST_FIELDS, "digests")
    for item in digests.values():
        _digest(item, optional=True)
    resources = _closed(value["resources"], _RESOURCE_FIELDS, "resources")
    for item in resources.values():
        _integer(item)
    return value


class AuditJournal:
    """Sole controller writer for one v1 audit JSONL stream."""

    def __init__(
        self,
        stream: BinaryIO,
        *,
        run_id: str,
        clock: Callable[[], int] = time.monotonic_ns,
        limits: Limits | None = None,
        close_stream: bool = False,
    ) -> None:
        if not callable(getattr(stream, "write", None)) or not callable(
            getattr(stream, "flush", None)
        ):
            raise TypeError("stream must be a writable binary stream")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._stream = stream
        self._run_id = _identifier(run_id)
        self._clock = clock
        self._limits = Limits() if limits is None else limits
        self._close_stream = close_stream
        self._prefix = hashlib.sha256()
        self._sequence = 0
        self._bytes = 0
        self._last_ns: int | None = None
        self._terminal_attempted = False
        self._terminal_written = False
        self._closed = False
        self._failed = False

    @property
    def record_count(self) -> int:
        return self._sequence

    @property
    def byte_count(self) -> int:
        return self._bytes

    @property
    def terminal_written(self) -> bool:
        return self._terminal_written

    @property
    def complete(self) -> bool:
        return self._terminal_written and not self._failed

    @property
    def failed(self) -> bool:
        return self._failed

    def _open(self) -> None:
        if self._closed or self._failed:
            raise JournalStateError("journal is closed or failed")
        if self._terminal_attempted:
            raise JournalStateError("terminal was already attempted")

    def _timestamp(self) -> int:
        observed = self._clock()
        if type(observed) is not int or observed < 0:
            raise JournalError("clock returned an invalid timestamp")
        if self._last_ns is not None and observed < self._last_ns:
            raise JournalError("clock moved backwards")
        self._last_ns = observed
        return observed

    def _write(self, raw: bytes, *, prefix: bool) -> None:
        try:
            count = self._stream.write(raw)
            if type(count) is not int or count != len(raw):
                raise OSError("short journal write")
            self._stream.flush()
        except BaseException:
            self._failed = True
            raise
        self._bytes += len(raw)
        self._sequence += 1
        if prefix:
            self._prefix.update(raw)

    def append_event(
        self,
        *,
        emitter: str,
        role: str,
        phase: str,
        event: str,
        outcome: str,
        error_code: str | None = None,
        correlation: Mapping[str, object] | None = None,
        identities: Mapping[str, object] | None = None,
        digests: Mapping[str, object] | None = None,
        counts: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Append one event while reserving a full record for the terminal."""
        self._open()
        if self._sequence >= self._limits.records - 1:
            raise JournalLimitError("terminal record allocation is reserved")
        record = {
            "schema_version": 1,
            "type": "event",
            "run_id": self._run_id,
            "seq": self._sequence + 1,
            "monotonic_ns": self._timestamp(),
            "emitter": emitter,
            "role": role,
            "phase": phase,
            "event": event,
            "correlation": dict(correlation or {}),
            "identities": dict(identities or {}),
            "outcome": outcome,
            "error_code": error_code,
            "digests": dict(digests or {}),
            "counts": dict(counts or {}),
        }
        validate_event(record)
        raw = canonical_line(record, maximum=self._limits.record_bytes)
        if self._bytes + len(raw) + self._limits.record_bytes > self._limits.run_bytes:
            raise JournalLimitError("terminal byte allocation is reserved")
        self._write(raw, prefix=True)
        return record

    def append_terminal(
        self,
        *,
        outcome: str,
        error_code: str | None,
        identities: Mapping[str, object],
        elapsed_ns: int,
        deadline_ns: int,
        planned_exits: Mapping[str, object],
        observed_exits: Mapping[str, object],
        hashes: Mapping[str, object],
        digests: Mapping[str, object],
        resources: Mapping[str, object],
        dropped_frames: int,
        truncated_logs: int,
    ) -> dict[str, object]:
        """Attempt the sole terminal record; failure permanently fails closed."""
        self._open()
        self._terminal_attempted = True
        record = {
            "schema_version": 1,
            "type": "terminal",
            "run_id": self._run_id,
            "seq": self._sequence + 1,
            "monotonic_ns": self._timestamp(),
            "emitter": "controller",
            "role": "controller",
            "phase": "terminal",
            "event": "terminal_snapshot",
            "outcome": outcome,
            "error_code": error_code,
            "identities": dict(identities),
            "last_event_seq": self._sequence,
            "record_count": self._sequence + 1,
            "prefix_sha256": self._prefix.hexdigest(),
            "elapsed_ns": elapsed_ns,
            "deadline_ns": deadline_ns,
            "planned_exits": dict(planned_exits),
            "observed_exits": dict(observed_exits),
            "hashes": dict(hashes),
            "digests": dict(digests),
            "resources": dict(resources),
            "dropped_frames": dropped_frames,
            "truncated_logs": truncated_logs,
        }
        try:
            validate_terminal(record)
            raw = canonical_line(record, maximum=self._limits.record_bytes)
            if self._bytes + len(raw) > self._limits.run_bytes:
                raise JournalLimitError("terminal exceeds remaining run allocation")
            self._write(raw, prefix=False)
        except BaseException:
            self._failed = True
            raise
        self._terminal_written = True
        return record

    def close(self) -> None:
        """Close without fabricating a terminal; incompleteness fails closed."""
        if self._closed:
            return
        self._closed = True
        if not self._terminal_written:
            self._failed = True
        if self._close_stream:
            try:
                self._stream.close()
            except BaseException:
                self._failed = True
                raise

    def __enter__(self) -> AuditJournal:
        return self

    def __exit__(self, _kind: object, _error: object, _traceback: object) -> None:
        self.close()
