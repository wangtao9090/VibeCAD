"""Journal framing, schema, ordering, and terminal-binding rules."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..journal import (
    MAX_RECORD_BYTES,
    MAX_RECORDS,
    MAX_RUN_BYTES,
    JournalError,
    canonical_line,
    validate_event,
    validate_terminal,
)

JRN_001 = "A16-JRN-001"
JRN_002 = "A16-JRN-002"
JRN_003 = "A16-JRN-003"
TRM_001 = "A16-TRM-001"

_MAX_JSON_NESTING = 32


@dataclass(frozen=True, slots=True)
class AuditDocument:
    records: tuple[dict[str, object], ...]
    lines: tuple[bytes, ...]
    raw_prefix: bytes
    events: tuple[dict[str, object], ...]
    terminal: dict[str, object] | None


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode(line: str) -> dict[str, object]:
    value = json.loads(
        line,
        object_pairs_hook=_pairs,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
    )
    if type(value) is not dict:
        raise ValueError("record is not an object")
    return value


def _json_nesting_is_bounded(line: str) -> bool:
    """Reject parser work whose nesting exceeds the audit schema's fixed budget."""
    depth = 0
    in_string = False
    escaped = False
    for character in line:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in ("{", "["):
            depth += 1
            if depth > _MAX_JSON_NESTING:
                return False
        elif character in ("}", "]"):
            depth -= 1
    return True


def _has_exact_integer_types(record: dict[str, object], *, terminal: bool) -> bool:
    """Independently close every JSON integer/bool boundary in the v1 schemas."""
    top_level = (
        (
            "schema_version",
            "seq",
            "monotonic_ns",
            "last_event_seq",
            "record_count",
            "elapsed_ns",
            "deadline_ns",
            "dropped_frames",
            "truncated_logs",
        )
        if terminal
        else ("schema_version", "seq", "monotonic_ns")
    )
    if any(type(record.get(name)) is not int for name in top_level):
        return False
    if record["schema_version"] != 1:
        return False

    identities = record.get("identities")
    if type(identities) is not dict:
        return False
    if any(type(value) is not int for name, value in identities.items() if name.endswith("_pid")):
        return False

    if terminal:
        for name in ("planned_exits", "observed_exits"):
            exits = record.get(name)
            if type(exits) is not dict or any(
                value is not None and type(value) is not int for value in exits.values()
            ):
                return False
        resources = record.get("resources")
        return type(resources) is dict and all(type(value) is int for value in resources.values())

    correlation = record.get("correlation")
    digests = record.get("digests")
    counts = record.get("counts")
    if type(correlation) is not dict or type(digests) is not dict or type(counts) is not dict:
        return False
    return all(
        type(value) is int
        for name, value in correlation.items()
        if name in {"request_id", "callback_id"}
    ) and all(type(value) is int for value in counts.values())


def analyze_journal(raw: object) -> tuple[AuditDocument | None, tuple[str, ...]]:
    """Parse one complete JSONL stream and return only its earliest rule class."""
    if (
        type(raw) is not bytes
        or not raw
        or len(raw) > MAX_RUN_BYTES
        or not raw.endswith(b"\n")
        or b"\r" in raw
        or b"\0" in raw
        or b"\xef\xbb\xbf" in raw
    ):
        return None, (JRN_002,)
    record_count = raw.count(b"\n")
    if not 1 <= record_count <= MAX_RECORDS:
        return None, (JRN_002,)

    framed_lines = raw.split(b"\n")
    framed_lines.pop()
    records: list[dict[str, object]] = []
    lines: list[bytes] = []
    events: list[dict[str, object]] = []
    terminals: list[dict[str, object]] = []
    for line in framed_lines:
        if not line or len(line) + 1 > MAX_RECORD_BYTES:
            return None, (JRN_002,)
        try:
            decoded = line.decode("utf-8", errors="strict")
            if not _json_nesting_is_bounded(decoded):
                raise ValueError("JSON nesting exceeds limit")
            record = _decode(decoded)
            if canonical_line(record) != line + b"\n":
                raise ValueError("noncanonical record")
        except (JournalError, OverflowError, RecursionError, UnicodeError, ValueError):
            return None, (JRN_002,)
        try:
            if record.get("type") == "event":
                validate_event(record)
                if not _has_exact_integer_types(record, terminal=False):
                    raise JournalError("event has a non-integer numeric field")
                events.append(record)
            elif record.get("type") == "terminal":
                validate_terminal(record)
                if not _has_exact_integer_types(record, terminal=True):
                    raise JournalError("terminal has a non-integer numeric field")
                terminals.append(record)
            else:
                raise JournalError("unknown record type")
        except (JournalError, KeyError, OverflowError, RecursionError, TypeError):
            return None, (JRN_001,)
        records.append(record)
        lines.append(line)
    raw_prefix = raw[: -(len(lines[-1]) + 1)]
    document = AuditDocument(
        tuple(records),
        tuple(lines),
        raw_prefix,
        tuple(events),
        terminals[0] if terminals else None,
    )
    run_ids = {record["run_id"] for record in records}
    sequences = [record["seq"] for record in records]
    timestamps = [record["monotonic_ns"] for record in records]
    if (
        len(run_ids) != 1
        or sequences != list(range(1, len(records) + 1))
        or timestamps != sorted(timestamps)
    ):
        return document, (JRN_003,)
    if len(terminals) != 1 or records[-1] is not terminals[0]:
        return document, (TRM_001,)
    terminal = terminals[0]
    if (
        terminal["last_event_seq"] != len(events)
        or terminal["record_count"] != len(records)
        or terminal["prefix_sha256"] != hashlib.sha256(document.raw_prefix).hexdigest()
    ):
        return document, (TRM_001,)
    return document, ()
