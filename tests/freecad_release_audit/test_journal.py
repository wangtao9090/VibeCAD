from __future__ import annotations

import hashlib
import io
import json
import struct
from pathlib import Path

import pytest

from .ipc import (
    FrameEOF,
    FrameIOError,
    FrameOversize,
    FrameReader,
    canonical_payload,
    encode_frame,
    write_frame,
)
from .journal import (
    MAX_RECORD_BYTES,
    MAX_RECORDS,
    MAX_RUN_BYTES,
    AuditJournal,
    JournalError,
    JournalLimitError,
    JournalStateError,
    Limits,
    canonical_line,
    validate_event,
    validate_terminal,
)

DIGEST = "a" * 64


class Clock:
    def __init__(self) -> None:
        self.value = 100

    def __call__(self) -> int:
        self.value += 1
        return self.value


class Chunks:
    def __init__(self, raw: bytes, sizes: tuple[int, ...] = (1,)) -> None:
        self.raw = raw
        self.sizes = sizes
        self.index = 0

    def __call__(self, maximum: int) -> bytes:
        if not self.raw:
            return b""
        size = min(maximum, self.sizes[self.index % len(self.sizes)], len(self.raw))
        self.index += 1
        result, self.raw = self.raw[:size], self.raw[size:]
        return result


class ShortWrite(io.BytesIO):
    def write(self, raw: bytes) -> int:
        super().write(raw[:-1])
        return len(raw) - 1


class FlushFailure(io.BytesIO):
    def flush(self) -> None:
        raise OSError("flush failed")


def event_kwargs(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "emitter": "probe_wrapper",
        "role": "gui",
        "phase": "preview",
        "event": "request_observed",
        "outcome": "success",
        "correlation": {"request_id": 1, "operation": "preview_open"},
        "identities": {"daemon_id": "daemon_1", "gui_pid": 101},
        "digests": {"request_sha256": DIGEST},
        "counts": {"pending_request_count": 1},
    }
    value.update(updates)
    return value


def terminal_kwargs(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "outcome": "success",
        "error_code": None,
        "identities": {
            "daemon_id": "daemon_1",
            "daemon_pid": 100,
            "gui_pid": 101,
            "project_id": "project_1",
        },
        "elapsed_ns": 10,
        "deadline_ns": 20,
        "planned_exits": {"daemon": 0, "gui": 0},
        "observed_exits": {"daemon": 0, "gui": 0},
        "hashes": {
            "product_before_sha256": DIGEST,
            "product_after_sha256": DIGEST,
            "runtime_before_sha256": DIGEST,
            "runtime_after_sha256": DIGEST,
        },
        "digests": {
            "result_sha256": DIGEST,
            "screenshot_sha256": DIGEST,
            "daemon_stdout_sha256": DIGEST,
            "daemon_stderr_sha256": DIGEST,
            "gui_stdout_sha256": DIGEST,
            "gui_stderr_sha256": DIGEST,
        },
        "resources": {
            "open_checkouts": 0,
            "file_grants": 0,
            "documents": 0,
            "pending_requests": 0,
            "active_daemons": 0,
            "active_sockets": 0,
            "active_gui_processes": 0,
        },
        "dropped_frames": 0,
        "truncated_logs": 0,
    }
    value.update(updates)
    return value


def decoded_lines(stream: io.BytesIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_published_limits_are_exact() -> None:
    assert MAX_RECORD_BYTES == 16_384
    assert MAX_RECORDS == 4_096
    assert MAX_RUN_BYTES == 4 * 1_048_576
    assert Limits() == Limits(16_384, 4_096, 4 * 1_048_576)
    with pytest.raises(ValueError):
        Limits(record_bytes=MAX_RECORD_BYTES + 1)
    with pytest.raises(ValueError):
        Limits(records=MAX_RECORDS + 1)
    with pytest.raises(ValueError):
        Limits(run_bytes=MAX_RUN_BYTES + 1)


def test_canonical_json_bytes_are_sorted_bounded_and_finite() -> None:
    assert canonical_line({"b": 2, "a": 1}) == b'{"a":1,"b":2}\n'
    with pytest.raises(JournalError):
        canonical_line({"value": float("nan")})
    with pytest.raises(JournalLimitError):
        canonical_line({"value": "x" * 200}, maximum=128)
    with pytest.raises(ValueError):
        canonical_line({}, maximum=MAX_RECORD_BYTES + 1)
    assert canonical_payload({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    with pytest.raises(FrameIOError):
        canonical_payload({"value": float("inf")})


def test_event_schema_is_closed_and_scalar_only() -> None:
    stream = io.BytesIO()
    journal = AuditJournal(stream, run_id="run_1", clock=Clock())
    record = journal.append_event(**event_kwargs())
    assert validate_event(record) is record
    assert record["seq"] == 1
    assert record["monotonic_ns"] == 101
    assert decoded_lines(stream) == [record]
    before = stream.getvalue()
    with pytest.raises(JournalError):
        journal.append_event(**event_kwargs(identities={"unknown": "value"}))
    with pytest.raises(JournalError):
        journal.append_event(**event_kwargs(event="UPPER_CASE"))
    with pytest.raises(JournalError):
        journal.append_event(**event_kwargs(outcome="unknown"))
    with pytest.raises(JournalError):
        journal.append_event(**event_kwargs(correlation={"request_id": "1"}))
    assert stream.getvalue() == before


def test_ids_and_codes_use_utf8_byte_bounds() -> None:
    stream = io.BytesIO()
    journal = AuditJournal(stream, run_id="r" * 128, clock=Clock())
    journal.append_event(**event_kwargs(event="e" * 64))
    with pytest.raises(JournalError):
        journal.append_event(**event_kwargs(event="e" * 65))
    with pytest.raises(JournalError):
        AuditJournal(io.BytesIO(), run_id="r" * 129)


def test_controller_clock_must_remain_monotonic() -> None:
    values = iter((20, 19))
    journal = AuditJournal(io.BytesIO(), run_id="run_1", clock=lambda: next(values))
    journal.append_event(**event_kwargs())
    with pytest.raises(JournalError, match="backwards"):
        journal.append_event(**event_kwargs())


def test_record_count_reserves_exactly_one_terminal() -> None:
    stream = io.BytesIO()
    limits = Limits(record_bytes=2_048, records=2, run_bytes=4_096)
    journal = AuditJournal(stream, run_id="run_1", clock=Clock(), limits=limits)
    journal.append_event(**event_kwargs())
    with pytest.raises(JournalLimitError, match="terminal record"):
        journal.append_event(**event_kwargs())
    terminal = journal.append_terminal(**terminal_kwargs())
    assert terminal["seq"] == 2
    assert terminal["record_count"] == 2
    assert journal.record_count == 2


def test_event_bytes_reserve_a_full_terminal_record() -> None:
    stream = io.BytesIO()
    limits = Limits(record_bytes=2_048, records=4, run_bytes=2_200)
    journal = AuditJournal(stream, run_id="run_1", clock=Clock(), limits=limits)
    with pytest.raises(JournalLimitError, match="terminal byte"):
        journal.append_event(**event_kwargs())
    assert stream.getvalue() == b""
    terminal = journal.append_terminal(**terminal_kwargs())
    assert terminal["record_count"] == 1
    assert journal.complete


def test_terminal_is_exactly_once_last_and_binds_prefix() -> None:
    stream = io.BytesIO()
    journal = AuditJournal(stream, run_id="run_1", clock=Clock())
    first = journal.append_event(**event_kwargs())
    second = journal.append_event(
        **event_kwargs(
            event="callback_observed",
            correlation={"request_id": 1, "callback_id": 1},
        )
    )
    prefix = stream.getvalue()
    terminal = journal.append_terminal(**terminal_kwargs())
    assert terminal["last_event_seq"] == 2
    assert terminal["record_count"] == 3
    assert terminal["prefix_sha256"] == hashlib.sha256(prefix).hexdigest()
    assert decoded_lines(stream) == [first, second, terminal]
    assert validate_terminal(terminal) is terminal
    assert journal.complete
    with pytest.raises(JournalStateError, match="terminal"):
        journal.append_terminal(**terminal_kwargs())
    with pytest.raises(JournalStateError, match="terminal"):
        journal.append_event(**event_kwargs())


def test_terminal_schema_rejects_missing_extra_and_unbounded_values() -> None:
    stream = io.BytesIO()
    journal = AuditJournal(stream, run_id="run_1", clock=Clock())
    with pytest.raises(JournalError):
        journal.append_terminal(
            **terminal_kwargs(resources={**terminal_kwargs()["resources"], "unknown": 0})
        )
    assert journal.failed
    assert not journal.terminal_written
    assert stream.getvalue() == b""
    with pytest.raises(JournalStateError):
        journal.append_terminal(**terminal_kwargs())


def test_short_terminal_write_fails_closed_without_retry() -> None:
    stream = ShortWrite()
    journal = AuditJournal(stream, run_id="run_1", clock=Clock())
    with pytest.raises(OSError, match="short journal write"):
        journal.append_terminal(**terminal_kwargs())
    assert journal.failed
    assert not journal.complete
    assert not journal.terminal_written
    with pytest.raises(JournalStateError):
        journal.append_terminal(**terminal_kwargs())


def test_flush_failure_prevents_further_records() -> None:
    journal = AuditJournal(FlushFailure(), run_id="run_1", clock=Clock())
    with pytest.raises(OSError, match="flush failed"):
        journal.append_event(**event_kwargs())
    assert journal.failed
    assert journal.record_count == 0
    with pytest.raises(JournalStateError):
        journal.append_event(**event_kwargs())


def test_close_without_terminal_is_incomplete_and_does_not_fabricate() -> None:
    stream = io.BytesIO()
    journal = AuditJournal(stream, run_id="run_1", clock=Clock())
    journal.append_event(**event_kwargs())
    before = stream.getvalue()
    journal.close()
    journal.close()
    assert stream.getvalue() == before
    assert journal.failed
    assert not journal.complete
    assert not journal.terminal_written
    with pytest.raises(JournalStateError):
        journal.append_event(**event_kwargs())


def test_close_after_terminal_preserves_complete_state_and_can_own_stream() -> None:
    stream = io.BytesIO()
    journal = AuditJournal(
        stream,
        run_id="run_1",
        clock=Clock(),
        close_stream=True,
    )
    journal.append_terminal(**terminal_kwargs())
    assert journal.complete
    journal.close()
    assert stream.closed
    assert journal.complete


def test_frame_encoding_and_partial_read_round_trip() -> None:
    payload = canonical_payload({"event": "request", "request_id": 1})
    frame = encode_frame(payload)
    assert frame[:4] == struct.pack(">I", len(payload))
    reader = FrameReader(Chunks(frame, (1, 2, 1, 3)))
    assert reader.read_frame() == payload
    assert reader.read_frame() is None


def test_frame_reader_distinguishes_clean_and_truncated_eof() -> None:
    assert FrameReader(Chunks(b"")).read_frame() is None
    with pytest.raises(FrameEOF):
        FrameReader(Chunks(b"\x00\x00")).read_frame()
    with pytest.raises(FrameEOF):
        FrameReader(Chunks(struct.pack(">I", 3) + b"ab")).read_frame()


def test_frame_reader_rejects_empty_oversized_and_invalid_reads() -> None:
    with pytest.raises(FrameOversize):
        FrameReader(Chunks(struct.pack(">I", 0))).read_frame()
    with pytest.raises(FrameOversize):
        FrameReader(Chunks(struct.pack(">I", 17)), maximum=16).read_frame()
    with pytest.raises(FrameIOError):
        FrameReader(lambda _size: "not bytes").read_frame()
    with pytest.raises(FrameIOError):
        FrameReader(lambda size: b"x" * (size + 1)).read_frame()


def test_frame_writer_handles_partial_writes_and_rejects_zero() -> None:
    committed = bytearray()

    def partial(raw: bytes) -> int:
        count = min(2, len(raw))
        committed.extend(raw[:count])
        return count

    payload = b"observation"
    assert write_frame(partial, payload) == len(encode_frame(payload))
    assert bytes(committed) == encode_frame(payload)
    with pytest.raises(FrameIOError):
        write_frame(lambda _raw: 0, payload)
    with pytest.raises(FrameOversize):
        encode_frame(b"x" * 17, maximum=16)
    with pytest.raises(ValueError):
        encode_frame(b"x", maximum=16_385)


def test_schemas_are_v1_closed_and_do_not_offer_payload_fields() -> None:
    schema_root = Path(__file__).with_name("schemas") / "v1"
    event = json.loads((schema_root / "event.schema.json").read_bytes())
    terminal = json.loads((schema_root / "terminal.schema.json").read_bytes())
    for schema in (event, terminal):
        assert schema["additionalProperties"] is False
        assert schema["properties"]["schema_version"] == {"const": 1}
        forbidden = {"path", "argv", "env", "credentials", "payload", "model", "document"}
        assert not forbidden & set(schema["properties"])
