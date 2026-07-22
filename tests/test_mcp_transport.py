"""Focused contract tests for VibeCAD's owned, bounded MCP transport."""

from __future__ import annotations

import importlib
import json
import queue
import threading
import time
import traceback
from typing import Any

import pytest


def _transport():
    try:
        return importlib.import_module("vibecad.mcp_transport")
    except ModuleNotFoundError:
        pytest.fail("the bounded MCP transport contract is not implemented")


def _rejected(module: Any, payload: bytes):
    with pytest.raises(module.TransportProtocolError) as caught:
        module.decode_request_frame(payload)
    return caught.value


def _request(module: Any, *, request_id: int | str, method: str = "ping"):
    return module.prevalidate_client_message(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {},
        }
    )


def test_transport_limits_are_literal_and_not_sdk_defaults() -> None:
    transport = _transport()

    assert transport.READ_CHUNK_BYTES == 65_536
    assert transport.MAX_REQUEST_FRAME_BYTES == 2_097_152
    assert transport.MAX_RESPONSE_FRAME_BYTES == 100_663_296
    assert transport.MAX_JSON_DEPTH == 64
    assert transport.MAX_JSON_NODES == 65_536
    assert transport.MAX_JSON_KEY_BYTES == 256
    assert transport.MAX_JSON_STRING_BYTES == 1_048_576
    assert transport.MAX_SAFE_JSON_INTEGER == 9_007_199_254_740_991
    assert transport.MAX_JSON_INTEGER_DIGITS == 16
    assert transport.MAX_JSON_FLOAT_TOKEN_BYTES == 64
    assert transport.MAX_IN_FLIGHT == 8
    assert transport.MAX_WORKERS == 4
    assert transport.MAX_RESOURCE_READS == 1


def test_line_framer_accepts_n_and_drains_n_plus_one_with_bounded_state() -> None:
    transport = _transport()
    framer = transport.RequestLineFramer()
    n = transport.MAX_REQUEST_FRAME_BYTES

    assert framer.feed(b"x" * n) == ()
    assert framer.buffered_bytes == n
    events = framer.feed(b"\nignored")
    assert len(events) == 1
    assert isinstance(events[0], transport.RequestFrame)
    assert events[0].payload == b"x" * n
    assert framer.buffered_bytes == len(b"ignored")

    over = transport.RequestLineFramer()
    assert over.feed(b"x" * n) == ()
    assert over.feed(b"y") == ()
    assert over.buffered_bytes == 0
    assert over.draining is True
    events = over.feed(b"discarded\nsecond frame must be ignored\n")
    assert len(events) == 1
    assert isinstance(events[0], transport.FrameFailure)
    assert events[0].response == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }
    assert over.closed is True
    assert over.buffered_bytes == 0


def test_line_framer_handles_multiple_chunks_crlf_and_unterminated_eof() -> None:
    transport = _transport()
    framer = transport.RequestLineFramer()

    assert framer.feed(b'{"a":1}') == ()
    events = framer.feed(b'\r\n{"b":2}\n')
    assert [event.payload for event in events] == [b'{"a":1}\r', b'{"b":2}']

    tail = transport.RequestLineFramer()
    assert tail.feed(b'{"secret":"must-not-reflect"}') == ()
    events = tail.finish()
    assert len(events) == 1
    assert isinstance(events[0], transport.FrameFailure)
    assert "must-not-reflect" not in json.dumps(events[0].response)
    assert tail.closed is True
    assert tail.buffered_bytes == 0


@pytest.mark.parametrize(
    "payload",
    [
        b'\xff{"jsonrpc":"2.0"}',
        b'{"jsonrpc":"2.0","jsonrpc":"2.0"}',
        b'{"jsonrpc":',
        b'{"n":NaN}',
        b'{"n":Infinity}',
        b'{"n":01}',
        b'{"n":1.}',
        b'{"n":+1}',
        b'{"n":9007199254740992}',
        b'{"n":-9007199254740992}',
    ],
)
def test_decode_rejects_lexical_and_json_failures_with_one_closed_error(payload: bytes) -> None:
    transport = _transport()

    error = _rejected(transport, payload)

    assert error.close is True
    assert error.response == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }
    assert payload.decode("utf-8", errors="ignore") not in str(error)


def test_decode_enforces_depth_key_string_node_and_number_n_plus_one() -> None:
    transport = _transport()

    depth_n = b"[" * 64 + b"0" + b"]" * 64
    assert transport.decode_request_frame(depth_n) is not None
    _rejected(transport, b"[" * 65 + b"0" + b"]" * 65)

    key_n = "é" * 128
    assert transport.decode_request_frame(
        json.dumps({key_n: 1}, ensure_ascii=False).encode("utf-8")
    ) == {key_n: 1}
    _rejected(
        transport,
        json.dumps({"é" * 129: 1}, ensure_ascii=False).encode("utf-8"),
    )

    string_n = "x" * transport.MAX_JSON_STRING_BYTES
    assert (
        transport.decode_request_frame(json.dumps(string_n, separators=(",", ":")).encode("utf-8"))
        == string_n
    )
    _rejected(
        transport,
        json.dumps(string_n + "x", separators=(",", ":")).encode("utf-8"),
    )

    nodes_n = b"[" + b",".join([b"null"] * 65_535) + b"]"
    assert len(transport.decode_request_frame(nodes_n)) == 65_535
    _rejected(transport, b"[" + b",".join([b"null"] * 65_536) + b"]")

    assert transport.decode_request_frame(b'{"n":9007199254740991}') == {"n": 9_007_199_254_740_991}
    float_n = b"1." + b"0" * 62
    assert transport.decode_request_frame(b'{"n":' + float_n + b"}") == {"n": 1.0}
    _rejected(transport, b'{"n":' + float_n + b"0}")


def test_decode_rejects_wire_n_plus_one_before_object_construction() -> None:
    transport = _transport()
    payload = b" " * transport.MAX_REQUEST_FRAME_BYTES

    error = _rejected(transport, payload + b" ")

    assert error.response["error"]["code"] == -32700
    assert error.close is True


def test_decode_failure_traceback_does_not_retain_secret_input() -> None:
    transport = _transport()
    secret = "unique-transport-secret-7f58"

    error = _rejected(transport, ('{"value":"' + secret + '",}').encode())
    rendered = "".join(traceback.format_exception(error))

    assert secret not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


def test_prevalidator_accepts_only_the_supported_client_union() -> None:
    transport = _transport()
    artifact_uri = "vibecad://artifact/materialization_" + "a" * 64 + "/artifact_" + "b" * 32

    initialize = transport.prevalidate_client_message(
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {
                    "roots": {"listChanged": True},
                    "sampling": {"context": {}, "tools": {}},
                    "elicitation": {"form": {}, "url": {}},
                    "tasks": {
                        "list": {},
                        "cancel": {},
                        "requests": {
                            "sampling": {"createMessage": {}},
                            "elicitation": {"create": {}},
                        },
                    },
                    "experimental": {"vendor.example/capability": {"enabled": True}},
                },
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
    )
    assert initialize.request_id == "init"
    assert initialize.is_notification is False

    listed = transport.prevalidate_client_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {"cursor": "next"},
        }
    )
    assert listed.method == "tools/list"

    called = transport.prevalidate_client_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "ping", "arguments": {}},
        }
    )
    assert called.params["name"] == "ping"

    resource = transport.prevalidate_client_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": artifact_uri},
        }
    )
    assert resource.is_resource_read is True

    cancelled = transport.prevalidate_client_message(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 2, "reason": "client request"},
        }
    )
    assert cancelled.is_cancellation is True
    assert cancelled.cancellation_target == 2

    initialized = transport.prevalidate_client_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert initialized.is_notification is True


def test_every_accepted_protocol_shape_survives_pinned_sdk_typed_validation() -> None:
    transport = _transport()
    from mcp import types

    artifact_uri = "vibecad://artifact/materialization_" + "a" * 64 + "/artifact_" + "b" * 32
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {
                    "roots": {"listChanged": True},
                    "sampling": {"context": {}, "tools": {}},
                    "elicitation": {"form": {}, "url": {}},
                    "tasks": {
                        "list": {},
                        "cancel": {},
                        "requests": {
                            "sampling": {"createMessage": {}},
                            "elicitation": {"create": {}},
                        },
                    },
                    "experimental": {"vendor.example/capability": {"enabled": True}},
                },
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "ping", "arguments": {}},
        },
        {"jsonrpc": "2.0", "id": 5, "method": "resources/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "resources/templates/list",
            "params": {},
        },
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "resources/read",
            "params": {"uri": artifact_uri},
        },
    ]
    notifications = [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 4, "reason": "client request"},
        },
    ]

    for message in requests:
        descriptor = transport.prevalidate_client_message(message)
        types.ClientRequest.model_validate(
            {"method": descriptor.method, "params": dict(descriptor.params)}
        )
    for message in notifications:
        descriptor = transport.prevalidate_client_message(message)
        typed = {"method": descriptor.method}
        if descriptor.params:
            typed["params"] = dict(descriptor.params)
        types.ClientNotification.model_validate(typed)


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "id": 1, "method": "secret-method", "params": {}},
        {"jsonrpc": "2.0", "id": True, "method": "ping", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"secret": 1}},
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}, "secret": 1},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"roots": {"listChanged": "secret"}},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"sampling": {"context": {"secret": True}}},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"roots": {"secret": True}},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {
                    "tasks": {"requests": {"sampling": {"createMessage": {"secret": True}}}}
                },
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"experimental": {"secret": []}},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    ],
)
def test_prevalidator_uses_fixed_non_reflective_invalid_request(message: dict[str, object]) -> None:
    transport = _transport()

    with pytest.raises(transport.TransportProtocolError) as caught:
        transport.prevalidate_client_message(message)

    response = caught.value.response
    assert response["error"] == {"code": -32600, "message": "Invalid Request"}
    assert "secret" not in json.dumps(response)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"uri": 7},
        {"uri": "vibecad://artifact/value", "secret": True},
        {"uri": "not-a-typed-uri"},
    ],
)
def test_resource_parameter_container_failures_use_fixed_invalid_params(params) -> None:
    transport = _transport()

    with pytest.raises(transport.TransportProtocolError) as caught:
        transport.prevalidate_client_message(
            {
                "jsonrpc": "2.0",
                "id": 40,
                "method": "resources/read",
                "params": params,
            }
        )

    assert caught.value.response == {
        "jsonrpc": "2.0",
        "id": 40,
        "error": {"code": -32602, "message": "Invalid request parameters"},
    }
    assert "secret" not in str(caught.value)


def test_typed_non_artifact_uri_survives_prevalidation_for_owned_handler() -> None:
    from mcp import types

    transport = _transport()
    descriptor = transport.prevalidate_client_message(
        {
            "jsonrpc": "2.0",
            "id": 41,
            "method": "resources/read",
            "params": {"uri": "file:///secret/path"},
        }
    )

    typed = types.ClientRequest.model_validate(
        {"method": descriptor.method, "params": dict(descriptor.params)}
    )
    assert typed.root.params.uri.scheme == "file"


def test_malformed_tools_call_container_has_its_own_fixed_error() -> None:
    transport = _transport()

    with pytest.raises(transport.TransportProtocolError) as caught:
        transport.prevalidate_client_message(
            {
                "jsonrpc": "2.0",
                "id": 41,
                "method": "tools/call",
                "params": {"name": "secret", "arguments": [], "secret": "value"},
            }
        )

    assert caught.value.response == {
        "jsonrpc": "2.0",
        "id": 41,
        "error": {"code": -32602, "message": "Tool request is invalid."},
    }


def test_active_request_ids_reject_duplicates_without_type_aliasing() -> None:
    transport = _transport()
    active = transport.ActiveRequestIds()

    assert active.reserve(1) is True
    assert active.reserve(1) is False
    assert active.reserve("1") is True
    assert active.count == 2
    active.release(1)
    assert active.reserve(1) is True
    assert active.count == 2


def test_eight_work_slots_keep_a_separate_single_control_lane() -> None:
    transport = _transport()
    controller = transport.AdmissionController()
    admitted = [controller.try_acquire_work(_request(transport, request_id=i)) for i in range(8)]

    assert all(item.lease is not None for item in admitted)
    assert controller.active_work_count == 8
    ninth = controller.try_acquire_work(_request(transport, request_id=8))
    assert ninth.full is True
    assert ninth.lease is None

    control = controller.try_acquire_control()
    assert control is not None
    assert controller.try_acquire_control() is None

    busy = controller.route_control(_request(transport, request_id=8), control)
    assert busy.kind is transport.ControlDecisionKind.BUSY
    assert busy.response == {
        "jsonrpc": "2.0",
        "id": 8,
        "error": {"code": -32005, "message": "Server is busy."},
    }
    assert controller.active_work_count == 8

    notification = transport.prevalidate_client_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    dropped = controller.route_control(notification, control)
    assert dropped.kind is transport.ControlDecisionKind.DROPPED
    assert dropped.response is None
    assert controller.active_work_count == 8
    controller.release_control(control)
    assert controller.try_acquire_control() is not None


def test_duplicate_and_cancel_do_not_release_capacity_until_cleanup_completion() -> None:
    transport = _transport()
    controller = transport.AdmissionController()
    first = controller.try_acquire_work(_request(transport, request_id="same"))
    assert first.lease is not None

    duplicate = controller.try_acquire_work(_request(transport, request_id="same"))
    assert duplicate.response == {
        "jsonrpc": "2.0",
        "id": "same",
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    assert controller.active_work_count == 1

    for index in range(1, 8):
        assert controller.try_acquire_work(_request(transport, request_id=index)).lease is not None
    control = controller.try_acquire_control()
    assert control is not None
    cancellation = transport.prevalidate_client_message(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "same"},
        }
    )
    marked = controller.route_control(cancellation, control)
    assert marked.kind is transport.ControlDecisionKind.CANCEL_REQUESTED
    assert controller.active_work_count == 8
    assert controller.try_acquire_work(_request(transport, request_id="same")).response == {
        "jsonrpc": "2.0",
        "id": "same",
        "error": {"code": -32600, "message": "Invalid Request"},
    }

    completion = controller.complete_work(first.lease)
    assert completion == {
        "jsonrpc": "2.0",
        "id": "same",
        "error": {"code": -32800, "message": "Request cancelled"},
    }
    assert controller.active_work_count == 8
    assert controller.try_acquire_work(_request(transport, request_id=8)).full is True
    assert controller.try_acquire_work(_request(transport, request_id="same")).response == {
        "jsonrpc": "2.0",
        "id": "same",
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    late = controller.route_control(cancellation, control)
    assert late.kind is transport.ControlDecisionKind.DROPPED
    with pytest.raises(transport.TransportLeaseError):
        controller.complete_work(first.lease)

    controller.acknowledge_work(first.lease)
    assert controller.active_work_count == 7
    assert controller.try_acquire_work(_request(transport, request_id="same")).lease is not None


def test_worker_and_resource_slots_are_bounded_and_block_early_work_release() -> None:
    transport = _transport()
    controller = transport.AdmissionController()
    work = [controller.try_acquire_work(_request(transport, request_id=i)).lease for i in range(5)]
    assert all(lease is not None for lease in work)

    workers = [controller.try_acquire_worker(lease) for lease in work[:4]]
    assert all(worker is not None for worker in workers)
    assert controller.try_acquire_worker(work[4]) is None

    resource = controller.try_acquire_resource(work[0])
    assert resource is not None
    assert controller.try_acquire_resource(work[1]) is None
    with pytest.raises(transport.TransportLeaseError):
        controller.complete_work(work[0])

    controller.release_worker(workers[0])
    assert controller.complete_work(work[0]) is None
    assert controller.active_work_count == 5
    assert controller.try_acquire_resource(work[1]) is None
    with pytest.raises(transport.TransportLeaseError):
        controller.acknowledge_work(work[0])

    controller.release_resource(resource)
    controller.acknowledge_work(work[0])
    assert controller.active_work_count == 4
    assert controller.try_acquire_resource(work[1]) is not None


def test_work_cannot_be_acknowledged_before_cleanup_completion() -> None:
    transport = _transport()
    controller = transport.AdmissionController()
    work = controller.try_acquire_work(_request(transport, request_id=1)).lease
    assert work is not None

    with pytest.raises(transport.TransportLeaseError):
        controller.acknowledge_work(work)

    assert controller.active_work_count == 1
    assert controller.complete_work(work) is None
    assert controller.active_work_count == 1
    controller.acknowledge_work(work)
    assert controller.active_work_count == 0


class _ChunkSource:
    def __init__(self) -> None:
        self._chunks: queue.Queue[bytes] = queue.Queue()

    def send(self, *messages: dict[str, object]) -> None:
        payload = b"".join(
            json.dumps(message, separators=(",", ":")).encode() + b"\n" for message in messages
        )
        self._chunks.put(payload)

    def close(self) -> None:
        self._chunks.put(b"")

    def read(self, maximum: int) -> bytes:
        assert maximum == 65_536
        return self._chunks.get(timeout=5)


class _FrameSink:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self.messages: list[dict[str, object]] = []

    def write(self, frame: bytes) -> None:
        assert frame.endswith(b"\n")
        message = json.loads(frame[:-1])
        with self._condition:
            self.messages.append(message)
            self._condition.notify_all()

    def wait_for(self, count: int) -> list[dict[str, object]]:
        deadline = time.monotonic() + 5
        with self._condition:
            while len(self.messages) < count:
                remaining = deadline - time.monotonic()
                assert remaining > 0, self.messages
                self._condition.wait(remaining)
            return list(self.messages)


def _wire_request(request_id: int | str, method: str = "ping") -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {}}


def _initialize_owned(source: _ChunkSource, sink: _FrameSink, runner) -> None:
    response_count = len(sink.messages) + 1
    source.send(
        {
            "jsonrpc": "2.0",
            "id": "initialize",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
    )
    sink.wait_for(response_count)
    source.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    deadline = time.monotonic() + 5
    while runner.handshake_state != "READY" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.handshake_state == "READY"


def _run_owned(transport, dispatch, source, sink, **kwargs):
    lifecycle = transport.ProcessLifecycle()
    exits: list[int] = []
    runner = transport.OwnedStdioRunner(
        dispatch=dispatch,
        lifecycle=lifecycle,
        close_application=lambda: True,
        uninstall_recovery_response=lambda request_id: transport.rpc_error_response(
            transport.INTERNAL_ERROR,
            request_id=request_id,
        ),
        exit_process=exits.append,
        **kwargs,
    )
    thread = threading.Thread(
        target=runner.run,
        kwargs={"read_chunk": source.read, "write_frame": sink.write},
        daemon=True,
    )
    thread.start()
    return runner, thread, lifecycle, exits


def test_owned_runner_has_four_precreated_workers_and_eight_admitted_slots() -> None:
    transport = _transport()
    source = _ChunkSource()
    sink = _FrameSink()
    release = threading.Event()
    entered = threading.Condition()
    calls: list[tuple[int | str | None, str]] = []

    def dispatch(descriptor):
        if descriptor.method == "initialize":
            return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}
        with entered:
            calls.append((descriptor.request_id, threading.current_thread().name))
            entered.notify_all()
        assert release.wait(5)
        return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}

    runner, thread, _lifecycle, _exits = _run_owned(transport, dispatch, source, sink)
    assert runner.worker_count == 4
    assert len(runner.worker_names) == 4
    _initialize_owned(source, sink, runner)

    source.send(*(_wire_request(index) for index in range(8)), _wire_request(8))
    with entered:
        deadline = time.monotonic() + 5
        while len(calls) < 4:
            remaining = deadline - time.monotonic()
            assert remaining > 0
            entered.wait(remaining)
    assert len(calls) == 4
    busy = sink.wait_for(2)[1]
    assert busy == {
        "jsonrpc": "2.0",
        "id": 8,
        "error": {"code": -32005, "message": "Server is busy."},
    }

    release.set()
    sink.wait_for(10)
    source.close()
    thread.join(5)
    assert not thread.is_alive()
    assert len(calls) == 8
    assert len({name for _, name in calls}) <= 4
    assert all(name in runner.worker_names for _, name in calls)


def test_bounded_control_backlog_preserves_rejections_and_live_cancellation() -> None:
    transport = _transport()
    source = _ChunkSource()
    sink = _FrameSink()
    work_entered = threading.Condition()
    work_count = 0
    release_work = threading.Event()
    busy_write_entered = threading.Event()
    release_busy_write = threading.Event()

    def dispatch(descriptor):
        nonlocal work_count
        if descriptor.method == "initialize":
            return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}
        with work_entered:
            work_count += 1
            work_entered.notify_all()
        assert release_work.wait(5)
        return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}

    def write(frame: bytes) -> None:
        message = json.loads(frame[:-1])
        if message.get("error", {}).get("code") == -32005:
            busy_write_entered.set()
            assert release_busy_write.wait(5)
        sink.write(frame)

    lifecycle = transport.ProcessLifecycle()
    runner = transport.OwnedStdioRunner(
        dispatch=dispatch,
        lifecycle=lifecycle,
        close_application=lambda: True,
        uninstall_recovery_response=lambda request_id: {},
        exit_process=lambda _code: None,
    )
    thread = threading.Thread(
        target=runner.run,
        kwargs={"read_chunk": source.read, "write_frame": write},
        daemon=True,
    )
    thread.start()
    _initialize_owned(source, sink, runner)
    source.send(
        *(_wire_request(index) for index in range(8)),
        _wire_request(8),
        _wire_request(9),
    )
    with work_entered:
        deadline = time.monotonic() + 5
        while work_count < 4:
            remaining = deadline - time.monotonic()
            assert remaining > 0
            work_entered.wait(remaining)
    assert busy_write_entered.wait(5)

    source.send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 0},
        }
    )
    deadline = time.monotonic() + 5
    while not runner.is_cancel_requested(0):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    release_work.set()
    time.sleep(0.05)
    assert runner.active_work_count == 8

    release_busy_write.set()
    messages = sink.wait_for(11)
    source.close()
    thread.join(5)
    by_id = {message.get("id"): message for message in messages[1:]}
    assert by_id[0]["error"] == {"code": -32800, "message": "Request cancelled"}
    assert by_id[8]["error"] == {"code": -32005, "message": "Server is busy."}
    assert by_id[9]["error"] == {"code": -32005, "message": "Server is busy."}


def test_owned_runner_rejects_duplicate_id_and_holds_cancel_until_cleanup() -> None:
    transport = _transport()
    source = _ChunkSource()
    sink = _FrameSink()
    entered = threading.Event()
    release = threading.Event()
    calls: list[int | str | None] = []

    def dispatch(descriptor):
        if descriptor.method == "initialize":
            return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}
        calls.append(descriptor.request_id)
        entered.set()
        assert release.wait(5)
        return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {"unsafe": True}}

    runner, thread, _lifecycle, _exits = _run_owned(transport, dispatch, source, sink)
    _initialize_owned(source, sink, runner)
    source.send(_wire_request("same"), _wire_request("same"))
    assert entered.wait(5)
    assert sink.wait_for(2)[1] == {
        "jsonrpc": "2.0",
        "id": "same",
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    source.send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "same"},
        }
    )
    assert sink.messages[1:] == [
        {
            "jsonrpc": "2.0",
            "id": "same",
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    ]
    deadline = time.monotonic() + 5
    while not runner.is_cancel_requested("same"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    release.set()
    assert sink.wait_for(3)[2] == {
        "jsonrpc": "2.0",
        "id": "same",
        "error": {"code": -32800, "message": "Request cancelled"},
    }
    source.close()
    thread.join(5)
    assert calls == ["same"]


def test_owned_runner_drops_late_cancel_after_cleanup_response_is_frozen() -> None:
    transport = _transport()
    source = _ChunkSource()
    write_entered = threading.Event()
    release_write = threading.Event()
    written: list[dict[str, object]] = []
    sink = _FrameSink()

    def dispatch(descriptor):
        return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {"ok": True}}

    def write(frame: bytes) -> None:
        message = json.loads(frame[:-1])
        if message.get("id") == 1 and "result" in message:
            write_entered.set()
            assert release_write.wait(5)
        written.append(message)
        sink.write(frame)

    lifecycle = transport.ProcessLifecycle()
    runner = transport.OwnedStdioRunner(
        dispatch=dispatch,
        lifecycle=lifecycle,
        close_application=lambda: True,
        uninstall_recovery_response=lambda request_id: {},
        exit_process=lambda _code: None,
    )
    thread = threading.Thread(
        target=runner.run,
        kwargs={"read_chunk": source.read, "write_frame": write},
        daemon=True,
    )
    thread.start()
    _initialize_owned(source, sink, runner)
    source.send(_wire_request(1))
    assert write_entered.wait(5)
    source.send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 1},
        }
    )
    release_write.set()
    deadline = time.monotonic() + 5
    while not written and time.monotonic() < deadline:
        time.sleep(0.01)
    source.close()
    thread.join(5)
    assert written[1:] == [{"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}]


def test_resource_lease_is_held_through_serialization_and_completed_write() -> None:
    transport = _transport()
    source = _ChunkSource()
    first_write = threading.Event()
    release_write = threading.Event()
    second_dispatch = threading.Event()
    sink = _FrameSink()
    calls: list[int | str | None] = []

    def dispatch(descriptor):
        calls.append(descriptor.request_id)
        if descriptor.request_id == 2:
            second_dispatch.set()
        return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}

    def write(frame: bytes) -> None:
        message = json.loads(frame[:-1])
        if message.get("id") == 1:
            first_write.set()
            assert release_write.wait(5)
        sink.write(frame)

    lifecycle = transport.ProcessLifecycle()
    runner = transport.OwnedStdioRunner(
        dispatch=dispatch,
        lifecycle=lifecycle,
        close_application=lambda: True,
        uninstall_recovery_response=lambda request_id: {},
        exit_process=lambda _code: None,
    )
    thread = threading.Thread(
        target=runner.run,
        kwargs={"read_chunk": source.read, "write_frame": write},
        daemon=True,
    )
    thread.start()
    _initialize_owned(source, sink, runner)
    uri = "vibecad://artifact/materialization_" + "a" * 64 + "/artifact_" + "b" * 32
    source.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": uri},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )
    assert first_write.wait(5)
    assert not second_dispatch.wait(0.1)
    release_write.set()
    assert second_dispatch.wait(5)
    sink.wait_for(3)
    source.close()
    thread.join(5)
    assert calls == ["initialize", 1, 2]


def test_swap_latch_drains_all_admitted_responses_before_exact_exit() -> None:
    transport = _transport()
    source = _ChunkSource()
    sink = _FrameSink()
    release = threading.Event()
    lifecycle = transport.ProcessLifecycle()
    exits: list[tuple[int, int, int]] = []
    entered = threading.Condition()
    entered_count = 0

    def dispatch(descriptor):
        nonlocal entered_count
        if descriptor.method == "initialize":
            return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}
        with entered:
            entered_count += 1
            entered.notify_all()
            while entered_count < 2:
                entered.wait(5)
        lifecycle.request_swap()
        assert release.wait(5)
        return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}

    runner = transport.OwnedStdioRunner(
        dispatch=dispatch,
        lifecycle=lifecycle,
        close_application=lambda: True,
        uninstall_recovery_response=lambda request_id: {},
        exit_process=lambda code: exits.append(
            (code, len(sink.messages), runner.active_work_count)
        ),
    )
    thread = threading.Thread(
        target=runner.run,
        kwargs={"read_chunk": source.read, "write_frame": sink.write},
        daemon=True,
    )
    thread.start()
    _initialize_owned(source, sink, runner)
    source.send(_wire_request(1), _wire_request(2))
    deadline = time.monotonic() + 5
    while runner.active_work_count < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    release.set()
    sink.wait_for(3)
    deadline = time.monotonic() + 5
    while not exits and time.monotonic() < deadline:
        time.sleep(0.01)
    source.close()
    thread.join(5)

    assert exits == [(75, 3, 0)]


def test_invalid_confirmed_uninstall_resumes_running_after_schema_response() -> None:
    transport = _transport()
    source = _ChunkSource()
    sink = _FrameSink()
    lifecycle = transport.ProcessLifecycle()

    def dispatch(descriptor):
        if descriptor.params.get("name") == "uninstall_runtime":
            return {
                "jsonrpc": "2.0",
                "id": descriptor.request_id,
                "result": {"schema_error": True},
            }
        return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}

    runner = transport.OwnedStdioRunner(
        dispatch=dispatch,
        lifecycle=lifecycle,
        close_application=lambda: pytest.fail("invalid request must not close"),
        uninstall_recovery_response=lambda request_id: {},
        exit_process=lambda _code: pytest.fail("invalid request must not exit"),
    )
    thread = threading.Thread(
        target=runner.run,
        kwargs={"read_chunk": source.read, "write_frame": sink.write},
        daemon=True,
    )
    thread.start()
    _initialize_owned(source, sink, runner)
    source.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "uninstall_runtime",
                "arguments": {"confirm": True, "unknown": True},
            },
        }
    )
    assert sink.wait_for(2)[1]["result"] == {"schema_error": True}
    assert lifecycle.state is transport.ProcessState.RUNNING

    source.send(_wire_request(2))
    assert sink.wait_for(3)[2] == {"jsonrpc": "2.0", "id": 2, "result": {}}
    source.close()
    thread.join(5)


def test_confirmed_uninstall_drains_peers_closes_then_flushes_and_exits() -> None:
    transport = _transport()
    source = _ChunkSource()
    sink = _FrameSink()
    peer_entered = threading.Event()
    release_peer = threading.Event()
    lifecycle = transport.ProcessLifecycle()
    events: list[str] = []
    exits: list[tuple[int, list[str]]] = []

    def dispatch(descriptor):
        if descriptor.request_id == 1:
            peer_entered.set()
            assert release_peer.wait(5)
            events.append("peer-return")
        elif descriptor.request_id == 2:
            lifecycle.request_uninstall_exit()
            events.append("marked")
        return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}

    runner = transport.OwnedStdioRunner(
        dispatch=dispatch,
        lifecycle=lifecycle,
        close_application=lambda: events.append("closed") or True,
        uninstall_recovery_response=lambda request_id: {},
        exit_process=lambda code: exits.append((code, list(events))),
    )
    thread = threading.Thread(
        target=runner.run,
        kwargs={"read_chunk": source.read, "write_frame": sink.write},
        daemon=True,
    )
    thread.start()
    _initialize_owned(source, sink, runner)
    source.send(
        _wire_request(1),
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "uninstall_runtime", "arguments": {"confirm": True}},
        },
    )
    assert peer_entered.wait(5)
    deadline = time.monotonic() + 5
    while "marked" not in events and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "closed" not in events
    release_peer.set()
    sink.wait_for(3)
    deadline = time.monotonic() + 5
    while not exits and time.monotonic() < deadline:
        time.sleep(0.01)
    source.close()
    thread.join(5)

    assert events.index("peer-return") < events.index("closed")
    assert exits == [(75, ["marked", "peer-return", "closed"])]


def test_uninstall_drain_failure_returns_recovery_and_never_exits() -> None:
    transport = _transport()
    source = _ChunkSource()
    sink = _FrameSink()
    lifecycle = transport.ProcessLifecycle()
    exits: list[int] = []

    def dispatch(descriptor):
        if descriptor.request_id == 1:
            lifecycle.request_uninstall_exit()
        return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}

    runner = transport.OwnedStdioRunner(
        dispatch=dispatch,
        lifecycle=lifecycle,
        close_application=lambda: False,
        uninstall_recovery_response=lambda request_id: {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"recovery_required": True},
        },
        exit_process=exits.append,
    )
    thread = threading.Thread(
        target=runner.run,
        kwargs={"read_chunk": source.read, "write_frame": sink.write},
        daemon=True,
    )
    thread.start()
    _initialize_owned(source, sink, runner)
    source.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "uninstall_runtime", "arguments": {"confirm": True}},
        }
    )
    assert sink.wait_for(2)[1:] == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"recovery_required": True},
        }
    ]
    source.close()
    thread.join(5)

    assert exits == []
    assert lifecycle.state is transport.ProcessState.CLOSED


def test_runner_activates_once_after_construction_and_before_any_ingress() -> None:
    transport = _transport()
    lifecycle = transport.ProcessLifecycle()
    observations: list[tuple[bool, tuple[bool, ...]]] = []
    runner = transport.OwnedStdioRunner(
        dispatch=lambda _descriptor: None,
        lifecycle=lifecycle,
        close_application=lambda: True,
        uninstall_recovery_response=lambda _request_id: {},
        exit_process=lambda _code: None,
    )

    assert lifecycle.accepts_work is False
    assert lifecycle.request_swap() is False
    assert all(not worker.is_alive() for worker in runner._workers)

    runner.run(
        read_chunk=lambda _maximum: b"",
        write_frame=lambda _frame: None,
        before_read=lambda: observations.append(
            (lifecycle.accepts_work, tuple(worker.is_alive() for worker in runner._workers))
        ),
    )

    assert observations == [(True, (True, True, True, True))]
    with pytest.raises(RuntimeError, match="single-use"):
        runner.run(read_chunk=lambda _maximum: b"", write_frame=lambda _frame: None)


def test_owned_runner_rejects_every_preinit_and_duplicate_handshake_frame() -> None:
    transport = _transport()
    source = _ChunkSource()
    sink = _FrameSink()
    dispatched: list[str] = []

    def dispatch(descriptor):
        dispatched.append(descriptor.method)
        return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}

    runner, thread, _lifecycle, _exits = _run_owned(transport, dispatch, source, sink)
    source.send(_wire_request(1))
    assert sink.wait_for(1)[0]["error"] == {
        "code": -32600,
        "message": "Invalid Request",
    }
    assert dispatched == []

    _initialize_owned(source, sink, runner)
    source.send(
        {
            "jsonrpc": "2.0",
            "id": "again",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
    )
    sink.wait_for(3)
    source.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    responses = sink.wait_for(4)
    assert responses[2]["error"] == {"code": -32600, "message": "Invalid Request"}
    assert responses[3]["error"] == {"code": -32600, "message": "Invalid Request"}
    assert dispatched == ["initialize"]
    source.close()
    thread.join(5)


def test_pipelined_initialized_is_rejected_even_while_initialize_write_is_blocked() -> None:
    transport = _transport()
    source = _ChunkSource()
    sink = _FrameSink()
    write_entered = threading.Event()
    release_write = threading.Event()

    def dispatch(descriptor):
        return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}

    def write(frame: bytes) -> None:
        message = json.loads(frame[:-1])
        if message.get("id") == "initialize":
            write_entered.set()
            assert release_write.wait(5)
        sink.write(frame)

    lifecycle = transport.ProcessLifecycle()
    runner = transport.OwnedStdioRunner(
        dispatch=dispatch,
        lifecycle=lifecycle,
        close_application=lambda: True,
        uninstall_recovery_response=lambda _request_id: {},
        exit_process=lambda _code: None,
    )
    thread = threading.Thread(
        target=runner.run,
        kwargs={"read_chunk": source.read, "write_frame": write},
        daemon=True,
    )
    thread.start()
    source.send(
        {
            "jsonrpc": "2.0",
            "id": "initialize",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert write_entered.wait(5)
    release_write.set()
    responses = sink.wait_for(2)
    assert any(
        response.get("error") == {"code": -32600, "message": "Invalid Request"}
        for response in responses
    )
    assert runner.handshake_state == "RESPONDED"
    source.close()
    thread.join(5)


def test_cancellation_cannot_bypass_handshake_or_cancel_initialize() -> None:
    transport = _transport()
    source = _ChunkSource()
    sink = _FrameSink()
    write_entered = threading.Event()
    release_write = threading.Event()
    dispatched: list[str] = []

    def dispatch(descriptor):
        dispatched.append(descriptor.method)
        return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}

    def write(frame: bytes) -> None:
        message = json.loads(frame[:-1])
        if message.get("id") == "initialize" and "result" in message:
            write_entered.set()
            assert release_write.wait(5)
        sink.write(frame)

    lifecycle = transport.ProcessLifecycle()
    runner = transport.OwnedStdioRunner(
        dispatch=dispatch,
        lifecycle=lifecycle,
        close_application=lambda: True,
        uninstall_recovery_response=lambda _request_id: {},
        exit_process=lambda _code: None,
    )
    thread = threading.Thread(
        target=runner.run,
        kwargs={"read_chunk": source.read, "write_frame": write},
        daemon=True,
    )
    thread.start()
    source.send(
        {
            "jsonrpc": "2.0",
            "id": "initialize",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
    )
    assert write_entered.wait(5)
    source.send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "initialize"},
        }
    )
    assert runner.is_cancel_requested("initialize") is False
    release_write.set()

    responses = sink.wait_for(2)
    assert responses[0] == {"jsonrpc": "2.0", "id": "initialize", "result": {}}
    assert responses[1] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    assert runner.handshake_state == "RESPONDED"
    assert runner.is_cancel_requested("initialize") is False
    source.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    deadline = time.monotonic() + 5
    while runner.handshake_state != "READY" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.handshake_state == "READY"
    source.close()
    thread.join(5)
    assert not thread.is_alive()
    assert dispatched == ["initialize"]


def test_owned_runner_rejects_preinit_cancellation_before_control_routing() -> None:
    transport = _transport()
    source = _ChunkSource()
    sink = _FrameSink()
    dispatched: list[str] = []

    def dispatch(descriptor):
        dispatched.append(descriptor.method)
        return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}

    runner, thread, _lifecycle, _exits = _run_owned(transport, dispatch, source, sink)
    source.send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "not-yet-active", "reason": "must-not-be-retained"},
        }
    )
    assert sink.wait_for(1) == [
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    ]
    assert runner.handshake_state == "NEW"
    assert dispatched == []

    _initialize_owned(source, sink, runner)
    source.close()
    thread.join(5)
    assert dispatched == ["initialize"]


def test_worker_exception_and_response_overflow_use_one_fixed_small_response(
    monkeypatch,
) -> None:
    transport = _transport()
    source = _ChunkSource()
    sink = _FrameSink()

    def dispatch(descriptor):
        if descriptor.method == "initialize":
            return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}
        if descriptor.request_id == 1:
            raise RuntimeError("secret-worker-value")
        return {
            "jsonrpc": "2.0",
            "id": descriptor.request_id,
            "result": {"secret": "x" * 1_000},
        }

    runner, thread, _lifecycle, _exits = _run_owned(transport, dispatch, source, sink)
    _initialize_owned(source, sink, runner)
    monkeypatch.setattr(transport, "MAX_RESPONSE_FRAME_BYTES", 120)
    source.send(_wire_request(1), _wire_request(2))
    responses = {item["id"]: item for item in sink.wait_for(3)[1:]}
    assert responses[1]["error"] == {"code": -32603, "message": "Internal error"}
    assert responses[2]["error"] == {"code": -32603, "message": "Internal error"}
    assert "secret" not in json.dumps(responses)
    source.close()
    thread.join(5)


def test_stdin_eof_drains_an_already_admitted_response() -> None:
    transport = _transport()
    entered = threading.Event()
    release = threading.Event()
    reads = iter(
        (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "initialize",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
                separators=(",", ":"),
            ).encode()
            + b"\n",
            b"",
        )
    )
    sink = _FrameSink()

    def dispatch(descriptor):
        entered.set()
        assert release.wait(5)
        return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}

    lifecycle = transport.ProcessLifecycle()
    runner = transport.OwnedStdioRunner(
        dispatch=dispatch,
        lifecycle=lifecycle,
        close_application=lambda: True,
        uninstall_recovery_response=lambda _request_id: {},
        exit_process=lambda _code: None,
    )
    thread = threading.Thread(
        target=runner.run,
        kwargs={"read_chunk": lambda _maximum: next(reads), "write_frame": sink.write},
        daemon=True,
    )
    thread.start()
    assert entered.wait(5)
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert sink.messages == [{"jsonrpc": "2.0", "id": "initialize", "result": {}}]


def test_marker_winner_rejects_late_resource_and_losing_uninstall_cannot_recover() -> None:
    transport = _transport()
    source = _ChunkSource()
    sink = _FrameSink()
    lifecycle = transport.ProcessLifecycle()
    both_entered = threading.Barrier(2)
    winner_marked = threading.Event()
    calls: list[int | str | None] = []
    closes: list[str] = []
    exits: list[int] = []

    def dispatch(descriptor):
        if descriptor.method == "initialize":
            return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}
        calls.append(descriptor.request_id)
        if descriptor.request_id == 1:
            both_entered.wait(5)
            assert lifecycle.request_uninstall_exit() is True
            winner_marked.set()
            return {"jsonrpc": "2.0", "id": 1, "result": {"status": "marked"}}
        if descriptor.request_id == 2:
            both_entered.wait(5)
            assert winner_marked.wait(5)
            assert lifecycle.request_uninstall_exit() is False
            assert lifecycle.recovery_required() is False
            return {"jsonrpc": "2.0", "id": 2, "result": {"status": "recovery"}}
        raise AssertionError("late resource reached dispatch")

    runner = transport.OwnedStdioRunner(
        dispatch=dispatch,
        lifecycle=lifecycle,
        close_application=lambda: closes.append("closed") or True,
        uninstall_recovery_response=lambda request_id: {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"status": "recovery"},
        },
        exit_process=exits.append,
    )
    thread = threading.Thread(
        target=runner.run,
        kwargs={"read_chunk": source.read, "write_frame": sink.write},
        daemon=True,
    )
    thread.start()
    _initialize_owned(source, sink, runner)

    def uninstall(request_id):
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": "uninstall_runtime", "arguments": {"confirm": True}},
        }

    source.send(uninstall(1), uninstall(2))
    assert winner_marked.wait(5)
    uri = "vibecad://artifact/materialization_" + "a" * 64 + "/artifact_" + "b" * 32
    source.send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": uri},
        }
    )
    responses = {item["id"]: item for item in sink.wait_for(4)[1:]}
    assert responses[3]["error"] == {"code": -32005, "message": "Server is busy."}
    deadline = time.monotonic() + 5
    while not exits and time.monotonic() < deadline:
        time.sleep(0.01)
    source.close()
    thread.join(5)
    assert calls == [1, 2] or calls == [2, 1]
    assert closes == ["closed"]
    assert exits == [75]


def test_partial_marked_write_keeps_marker_and_never_appends_recovery(
    tmp_path,
) -> None:
    transport = _transport()
    source = _ChunkSource()
    sink = _FrameSink()
    lifecycle = transport.ProcessLifecycle()
    marker = tmp_path / ".uninstall_requested"
    marked_write = threading.Event()
    attempts: list[dict[str, object]] = []
    exits: list[int] = []

    def dispatch(descriptor):
        if descriptor.method == "initialize":
            return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}
        marker.touch()
        assert lifecycle.request_uninstall_exit() is True
        return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {"status": "marked"}}

    def write(frame: bytes) -> None:
        response = json.loads(frame[:-1])
        attempts.append(response)
        if response.get("id") == 1:
            marked_write.set()
            raise OSError("simulated partial write")
        sink.write(frame)

    runner = transport.OwnedStdioRunner(
        dispatch=dispatch,
        lifecycle=lifecycle,
        close_application=lambda: True,
        uninstall_recovery_response=lambda request_id: {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"status": "recovery"},
        },
        exit_process=exits.append,
    )
    thread = threading.Thread(
        target=runner.run,
        kwargs={"read_chunk": source.read, "write_frame": write},
        daemon=True,
    )
    thread.start()
    _initialize_owned(source, sink, runner)
    source.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "uninstall_runtime", "arguments": {"confirm": True}},
        }
    )
    assert marked_write.wait(5)
    deadline = time.monotonic() + 5
    while lifecycle.state is not transport.ProcessState.RECOVERY_REQUIRED:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    source.close()
    thread.join(5)

    assert marker.exists()
    assert [attempt["id"] for attempt in attempts] == ["initialize", 1]
    assert attempts[-1]["result"] == {"status": "marked"}
    assert exits == []


def test_response_ceiling_uses_fixed_method_failure_without_secret_reflection(
    monkeypatch,
) -> None:
    transport = _transport()
    source = _ChunkSource()
    sink = _FrameSink()
    secret = "secret-response-value-66af"

    def dispatch(descriptor):
        if descriptor.method == "initialize":
            return {"jsonrpc": "2.0", "id": descriptor.request_id, "result": {}}
        return {
            "jsonrpc": "2.0",
            "id": descriptor.request_id,
            "result": {"value": secret * 20},
        }

    def fixed_failure(descriptor):
        return transport.rpc_error_response(
            transport.GENERIC_INTERNAL_ERROR,
            request_id=descriptor.request_id,
        )

    monkeypatch.setattr(transport, "MAX_RESPONSE_FRAME_BYTES", 160)
    lifecycle = transport.ProcessLifecycle()
    runner = transport.OwnedStdioRunner(
        dispatch=dispatch,
        lifecycle=lifecycle,
        close_application=lambda: True,
        uninstall_recovery_response=lambda request_id: {},
        exit_process=lambda _code: None,
        failure_response=fixed_failure,
    )
    thread = threading.Thread(
        target=runner.run,
        kwargs={"read_chunk": source.read, "write_frame": sink.write},
        daemon=True,
    )
    thread.start()
    _initialize_owned(source, sink, runner)
    source.send(_wire_request(1))
    response = sink.wait_for(2)[1]
    source.close()
    thread.join(5)

    assert response == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32603, "message": "Internal error"},
    }
    assert secret not in json.dumps(response)
