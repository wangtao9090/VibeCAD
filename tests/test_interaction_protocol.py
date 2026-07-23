from __future__ import annotations

import json

import pytest

import vibecad.interaction.protocol as protocol_module
import vibecad.interaction.protocol_v2 as protocol_v2
from vibecad.interaction.protocol import (
    MAX_PROTOCOL_DEPTH,
    MAX_PROTOCOL_KEY_BYTES,
    MAX_PROTOCOL_NODES,
    MAX_PROTOCOL_REQUEST_BYTES,
    MAX_PROTOCOL_RESPONSE_BYTES,
    MAX_PROTOCOL_STRING_BYTES,
    ProtocolError,
    ProtocolErrorCode,
    ProtocolRequest,
    decode_request,
    decode_response,
    encode_failure,
    encode_success,
    unavailable_response,
)

REQUEST_ID = "request_" + "1" * 32
KERNEL_ID = "kernel_" + "2" * 32
SESSION_ID = "session_" + "3" * 32


def _request(method: str = "initialize", params: object | None = None) -> bytes:
    if params is None:
        params = {"client_name": "pytest", "client_version": "1.0"}
    return json.dumps(
        {
            "protocol": "vibecad.local",
            "version": {"major": 1, "minor": 0},
            "request_id": REQUEST_ID,
            "method": method,
            "params": params,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def test_decode_request_is_exact_and_typed() -> None:
    request = decode_request(_request())

    assert type(request) is ProtocolRequest
    assert request.request_id == REQUEST_ID
    assert request.method == "initialize"
    assert request.params == {"client_name": "pytest", "client_version": "1.0"}


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"\xef\xbb\xbf{}", ProtocolErrorCode.MALFORMED_MESSAGE),
        (
            b'{"protocol":"vibecad.local","protocol":"vibecad.local",'
            b'"version":{"major":1,"minor":0},"request_id":"request_'
            + b"1" * 32
            + b'","method":"initialize","params":{}}',
            ProtocolErrorCode.MALFORMED_MESSAGE,
        ),
        (_request() + b"{}", ProtocolErrorCode.MALFORMED_MESSAGE),
        (_request().replace(b'"minor":0', b'"minor":1'), ProtocolErrorCode.UNSUPPORTED_VERSION),
        (
            _request().replace(b'"params":', b'"extra":null,"params":'),
            ProtocolErrorCode.INVALID_REQUEST,
        ),
    ],
)
def test_decode_request_fails_closed(raw: bytes, code: ProtocolErrorCode) -> None:
    with pytest.raises(ProtocolError) as raised:
        decode_request(raw)

    assert raised.value.code is code
    assert not hasattr(raised.value, "raw")


def test_request_budget_has_exact_n_and_n_plus_one_boundary() -> None:
    raw = _request()
    padded = raw[:-1] + b" " * (MAX_PROTOCOL_REQUEST_BYTES - len(raw)) + b"}"
    assert len(padded) == MAX_PROTOCOL_REQUEST_BYTES
    decode_request(padded)

    with pytest.raises(ProtocolError) as raised:
        decode_request(padded + b" ")
    assert raised.value.code is ProtocolErrorCode.BUDGET_EXCEEDED


def test_method_schemas_reject_unknown_fields_and_paths() -> None:
    with pytest.raises(ProtocolError) as raised:
        decode_request(
            _request(
                "checkout.open",
                {
                    "kernel_id": KERNEL_ID,
                    "session_id": SESSION_ID,
                    "open_key": "checkout_open_" + "4" * 32,
                    "source": {
                        "kind": "head",
                        "project_id": "project_" + "5" * 32,
                    },
                    "local_path": "/tmp/leak.FCStd",
                },
            )
        )

    assert raised.value.code is ProtocolErrorCode.INVALID_REQUEST


def test_success_failure_and_unavailable_envelopes_are_exact() -> None:
    request = decode_request(_request())
    result = {
        "kernel_id": KERNEL_ID,
        "session_id": SESSION_ID,
        "protocol_version": {"major": 1, "minor": 0},
        "capabilities": {
            "application_dispatch": False,
            "checkout_dispatch": False,
            "authenticated_transport": False,
            "local_path_delivery": False,
        },
    }

    success = decode_response(encode_success(request, result), method=request.method)
    assert success.result == result
    assert success.error is None

    failure = decode_response(
        encode_failure(REQUEST_ID, ProtocolErrorCode.UNAVAILABLE),
    )
    assert failure.result is None
    assert failure.error == {
        "code": "unavailable",
        "message": "The local interaction method is unavailable.",
    }

    unavailable = decode_response(unavailable_response(request))
    assert unavailable.error == failure.error


def test_wire_checkout_result_is_path_free() -> None:
    request = decode_request(
        _request(
            "checkout.get",
            {
                "kernel_id": KERNEL_ID,
                "session_id": SESSION_ID,
                "checkout_id": "checkout_" + "6" * 32,
            },
        )
    )
    descriptor = {
        "checkout_id": "checkout_" + "6" * 32,
        "open_key": "checkout_open_" + "4" * 32,
        "state": "open",
        "authoritative": False,
        "dirty": False,
        "source": {
            "kind": "head",
            "project_id": "project_" + "5" * 32,
            "revision_id": "revision_" + "7" * 32,
            "manifest_sha256": "8" * 64,
            "model_sha256": "9" * 64,
            "size_bytes": 5,
            "task_id": None,
            "draft_id": None,
            "task_generation": None,
        },
        "initial_model_sha256": "9" * 64,
        "current_model_sha256": "9" * 64,
        "current_size_bytes": 5,
    }
    encode_success(request, descriptor)

    with pytest.raises(ProtocolError) as raised:
        encode_success(request, descriptor | {"local_path": "/tmp/model.FCStd"})
    assert raised.value.code is ProtocolErrorCode.INVALID_REQUEST


def _application_request(payload: object) -> bytes:
    return _request(
        "application.call",
        {
            "kernel_id": KERNEL_ID,
            "session_id": SESSION_ID,
            "operation": "get_capabilities",
            "request": {"payload": payload},
        },
    )


def test_depth_budget_has_exact_n_and_n_plus_one_boundary() -> None:
    payload: object = None
    # The payload scalar starts at depth four in the frozen request envelope.
    for _ in range(MAX_PROTOCOL_DEPTH - 4):
        payload = [payload]
    decode_request(_application_request(payload))

    with pytest.raises(ProtocolError) as raised:
        decode_request(_application_request([payload]))
    assert raised.value.code is ProtocolErrorCode.BUDGET_EXCEEDED


def test_node_budget_has_exact_n_and_n_plus_one_boundary() -> None:
    # The frozen application.call envelope contributes thirteen nodes.
    payload = [None] * (MAX_PROTOCOL_NODES - 13)
    decode_request(_application_request(payload))

    with pytest.raises(ProtocolError) as raised:
        decode_request(_application_request(payload + [None]))
    assert raised.value.code is ProtocolErrorCode.BUDGET_EXCEEDED


def test_string_and_key_budgets_count_utf8_bytes() -> None:
    decode_request(_application_request("é" * (MAX_PROTOCOL_STRING_BYTES // 2)))
    with pytest.raises(ProtocolError) as raised:
        decode_request(_application_request("é" * (MAX_PROTOCOL_STRING_BYTES // 2) + "x"))
    assert raised.value.code is ProtocolErrorCode.BUDGET_EXCEEDED

    decode_request(_application_request({"k" * MAX_PROTOCOL_KEY_BYTES: None}))
    with pytest.raises(ProtocolError) as raised:
        decode_request(_application_request({"k" * (MAX_PROTOCOL_KEY_BYTES + 1): None}))
    assert raised.value.code is ProtocolErrorCode.BUDGET_EXCEEDED


def test_safe_integer_boundary_is_enforced_before_method_decode() -> None:
    decode_request(_application_request(2**53 - 1))
    with pytest.raises(ProtocolError) as raised:
        decode_request(_application_request(2**53))
    assert raised.value.code is ProtocolErrorCode.MALFORMED_MESSAGE


def test_bool_is_not_accepted_as_an_integer_version() -> None:
    raw = _request().replace(b'"major":1', b'"major":true')
    with pytest.raises(ProtocolError) as raised:
        decode_request(raw)
    assert raised.value.code is ProtocolErrorCode.UNSUPPORTED_VERSION


def test_application_result_preserves_only_exact_task_api_envelope() -> None:
    request = decode_request(
        _request(
            "application.call",
            {
                "kernel_id": KERNEL_ID,
                "session_id": SESSION_ID,
                "operation": "get_capabilities",
                "request": {"schema_version": 1},
            },
        )
    )
    response = {
        "response": {
            "schema_version": 1,
            "ok": True,
            "result": {"registry_schema_version": 1, "operations": []},
            "error": None,
        }
    }
    decoded = decode_response(encode_success(request, response), method=request.method)
    assert decoded.result == response

    with pytest.raises(ProtocolError) as raised:
        encode_success(
            request,
            {"response": response["response"] | {"result": {"local_path": "/tmp/leak.FCStd"}}},
        )
    assert raised.value.code is ProtocolErrorCode.INVALID_REQUEST


def test_success_decode_requires_method_and_never_accepts_local_path() -> None:
    request = decode_request(_request())
    valid_result = {
        "kernel_id": KERNEL_ID,
        "session_id": SESSION_ID,
        "protocol_version": {"major": 1, "minor": 0},
        "capabilities": {
            "application_dispatch": False,
            "checkout_dispatch": False,
            "authenticated_transport": False,
            "local_path_delivery": False,
        },
    }
    with pytest.raises(ProtocolError) as raised:
        decode_response(encode_success(request, valid_result))
    assert raised.value.code is ProtocolErrorCode.INVALID_REQUEST

    malicious = json.dumps(
        {
            "protocol": "vibecad.local",
            "version": {"major": 1, "minor": 0},
            "request_id": REQUEST_ID,
            "result": {"local_path": "/secret"},
            "error": None,
        },
        separators=(",", ":"),
    ).encode()
    with pytest.raises(ProtocolError) as raised:
        decode_response(malicious)
    assert raised.value.code is ProtocolErrorCode.INVALID_REQUEST


@pytest.mark.parametrize("kind", ["string", "depth"])
def test_response_budget_preflight_runs_before_serializer(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    request = decode_request(
        _request(
            "application.call",
            {
                "kernel_id": KERNEL_ID,
                "session_id": SESSION_ID,
                "operation": "get_capabilities",
                "request": {"schema_version": 1},
            },
        )
    )
    if kind == "string":
        payload: object = "x" * (MAX_PROTOCOL_STRING_BYTES + 1)
    else:
        payload = None
        for _ in range(MAX_PROTOCOL_DEPTH):
            payload = [payload]
    response = {
        "response": {
            "schema_version": 1,
            "ok": True,
            "result": {"payload": payload},
            "error": None,
        }
    }

    def forbidden_serializer(*_args, **_kwargs):
        raise AssertionError("serializer ran before response budget admission")

    monkeypatch.setattr(protocol_module.json, "dumps", forbidden_serializer)
    with pytest.raises(ProtocolError) as raised:
        encode_success(request, response)
    assert raised.value.code is ProtocolErrorCode.BUDGET_EXCEEDED


def test_cyclic_response_is_a_closed_protocol_failure() -> None:
    request = decode_request(
        _request(
            "application.call",
            {
                "kernel_id": KERNEL_ID,
                "session_id": SESSION_ID,
                "operation": "get_capabilities",
                "request": {"schema_version": 1},
            },
        )
    )
    cycle: dict[str, object] = {}
    cycle["cycle"] = cycle
    response = {
        "response": {
            "schema_version": 1,
            "ok": True,
            "result": cycle,
            "error": None,
        }
    }

    with pytest.raises(ProtocolError) as raised:
        encode_success(request, response)
    assert raised.value.code is ProtocolErrorCode.INVALID_REQUEST


def test_response_budget_has_exact_n_and_n_plus_one_boundary() -> None:
    raw = encode_failure(REQUEST_ID, ProtocolErrorCode.UNAVAILABLE)
    padded = raw + b" " * (MAX_PROTOCOL_RESPONSE_BYTES - len(raw))
    decode_response(padded)

    with pytest.raises(ProtocolError) as raised:
        decode_response(padded + b" ")
    assert raised.value.code is ProtocolErrorCode.BUDGET_EXCEEDED


V2_BOOT_SECRET = b"s" * 32
V2_DAEMON_ID = "daemon_" + "a" * 32
V2_FILE_GRANT_ID = "file_grant_" + "b" * 32
V2_CHECKOUT_ID = "checkout_" + "c" * 32


def _v2_file_grant_descriptor(
    **changes: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "grant_id": V2_FILE_GRANT_ID,
        "purpose": "open_managed_checkout",
        "expires_in_ms": 30_000,
    }
    value.update(changes)
    return value


def _v2_file_grant_result(
    **changes: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "grant_id": V2_FILE_GRANT_ID,
        "checkout_id": V2_CHECKOUT_ID,
        "purpose": "open_managed_checkout",
        "local_path": f"/private/checkouts/{V2_CHECKOUT_ID}/model.FCStd",
        "current_model_sha256": "d" * 64,
        "current_size_bytes": 4096,
    }
    value.update(changes)
    return value


def _v2_json(raw: bytes) -> dict[str, object]:
    value = json.loads(raw)
    assert type(value) is dict
    return value


def _v2_raw(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _ready_v2_pair(
    *,
    secret: bytes = V2_BOOT_SECRET,
    daemon_id: str = V2_DAEMON_ID,
) -> tuple[protocol_v2.V2ServerConnection, protocol_v2.V2ClientConnection]:
    server = protocol_v2.V2ServerConnection(secret, daemon_id=daemon_id)
    client = protocol_v2.V2ClientConnection(secret, expected_daemon_id=daemon_id)
    challenge = server.start()
    authentication = client.answer_challenge(challenge)
    ready = server.accept_auth(authentication)
    client.accept_authenticated(ready)
    assert server.state is protocol_v2.V2ConnectionState.READY
    assert client.state is protocol_v2.V2ConnectionState.READY
    return server, client


def _v2_request_id(index: int) -> str:
    return f"request_{index:032x}"


def test_v2_contract_budgets_and_v1_export_isolation_are_frozen() -> None:
    import vibecad.interaction as interaction

    assert protocol_v2.V2_PROTOCOL == "vibecad.local"
    assert protocol_v2.V2_VERSION == (2, 0)
    assert protocol_v2.V2_FRAME_HEADER_BYTES == 4
    assert protocol_v2.MAX_V2_FRAME_PAYLOAD_BYTES == 1_048_576
    assert protocol_v2.MAX_V2_CONNECTIONS == 8
    assert protocol_v2.MAX_V2_IN_FLIGHT == 8
    assert protocol_v2.V2_HANDSHAKE_TIMEOUT_SECONDS == 5.0
    assert protocol_v2.V2_IDLE_TIMEOUT_SECONDS == 30.0
    assert interaction.decode_request is decode_request
    assert not hasattr(interaction, "V2ServerConnection")


def test_v2_exact_frame_boundary_and_declared_length_fail_closed() -> None:
    maximum = protocol_v2.MAX_V2_FRAME_PAYLOAD_BYTES
    payload = b"x" * maximum
    framed = protocol_v2.encode_v2_frame(payload)
    assert framed[:4] == maximum.to_bytes(4, "big")
    assert protocol_v2.decode_v2_frame(framed) == payload

    cases = (
        (b"", protocol_v2.V2ErrorCode.TRUNCATED_FRAME),
        (b"\x00\x00\x00", protocol_v2.V2ErrorCode.TRUNCATED_FRAME),
        (b"\x00\x00\x00\x00", protocol_v2.V2ErrorCode.MALFORMED_FRAME),
        (b"\x00\x00\x00\x02x", protocol_v2.V2ErrorCode.TRUNCATED_FRAME),
        (b"\x00\x00\x00\x01xx", protocol_v2.V2ErrorCode.MALFORMED_FRAME),
        ((maximum + 1).to_bytes(4, "big"), protocol_v2.V2ErrorCode.FRAME_TOO_LARGE),
        (b"\xff\xff\xff\xff", protocol_v2.V2ErrorCode.FRAME_TOO_LARGE),
    )
    for raw, code in cases:
        with pytest.raises(protocol_v2.V2ProtocolError) as raised:
            protocol_v2.decode_v2_frame(raw)
        assert raised.value.code is code
        assert not hasattr(raised.value, "raw")

    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        protocol_v2.encode_v2_frame(payload + b"x")
    assert raised.value.code is protocol_v2.V2ErrorCode.FRAME_TOO_LARGE


def test_v2_incremental_frame_decoder_handles_fragmentation_and_multiple_frames() -> None:
    decoder = protocol_v2.V2FrameDecoder()
    first = protocol_v2.encode_v2_frame(b"one")
    second = protocol_v2.encode_v2_frame(b"two")

    assert decoder.feed(first[:2]) == ()
    assert decoder.feed(first[2:] + second) == (b"one", b"two")
    decoder.finish()

    truncated = protocol_v2.V2FrameDecoder()
    assert truncated.feed(first[:-1]) == ()
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        truncated.finish()
    assert raised.value.code is protocol_v2.V2ErrorCode.TRUNCATED_FRAME

    oversized = protocol_v2.V2FrameDecoder()
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        oversized.feed((protocol_v2.MAX_V2_FRAME_PAYLOAD_BYTES + 1).to_bytes(4, "big"))
    assert raised.value.code is protocol_v2.V2ErrorCode.FRAME_TOO_LARGE


def test_v2_mutual_handshake_creates_server_session_without_exposing_secret() -> None:
    server = protocol_v2.V2ServerConnection(V2_BOOT_SECRET, daemon_id=V2_DAEMON_ID)
    client = protocol_v2.V2ClientConnection(
        V2_BOOT_SECRET,
        expected_daemon_id=V2_DAEMON_ID,
    )

    challenge = server.start()
    assert server.state is protocol_v2.V2ConnectionState.CHALLENGE_SENT
    assert V2_BOOT_SECRET not in challenge
    authentication = client.answer_challenge(challenge)
    assert client.state is protocol_v2.V2ConnectionState.AUTH_SENT
    assert V2_BOOT_SECRET not in authentication
    ready = server.accept_auth(authentication)
    client.accept_authenticated(ready)

    ready_value = _v2_json(ready)
    assert ready_value["type"] == "authenticated"
    assert str(ready_value["session_id"]).startswith("session_")
    assert server.state is protocol_v2.V2ConnectionState.READY
    assert client.state is protocol_v2.V2ConnectionState.READY
    assert V2_BOOT_SECRET not in ready


def test_v2_server_session_id_is_readonly_and_ready_only() -> None:
    server = protocol_v2.V2ServerConnection(V2_BOOT_SECRET, daemon_id=V2_DAEMON_ID)
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        _ = server.session_id
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_STATE

    client = protocol_v2.V2ClientConnection(
        V2_BOOT_SECRET,
        expected_daemon_id=V2_DAEMON_ID,
    )
    authentication = client.answer_challenge(server.start())
    ready = server.accept_auth(authentication)
    client.accept_authenticated(ready)
    assert server.session_id == _v2_json(ready)["session_id"]
    with pytest.raises(AttributeError):
        server.session_id = "session_" + "f" * 32

    server.close()
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        _ = server.session_id
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_STATE


def test_v2_handshake_state_machine_and_wrong_secret_fail_closed() -> None:
    server = protocol_v2.V2ServerConnection(V2_BOOT_SECRET, daemon_id=V2_DAEMON_ID)
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        server.accept_auth(b"{}")
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_STATE
    assert server.state is protocol_v2.V2ConnectionState.FAILED

    server = protocol_v2.V2ServerConnection(V2_BOOT_SECRET, daemon_id=V2_DAEMON_ID)
    challenge = server.start()
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        server.start()
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_STATE
    assert server.state is protocol_v2.V2ConnectionState.FAILED

    server = protocol_v2.V2ServerConnection(V2_BOOT_SECRET, daemon_id=V2_DAEMON_ID)
    challenge = server.start()
    client = protocol_v2.V2ClientConnection(
        b"x" * 32,
        expected_daemon_id=V2_DAEMON_ID,
    )
    authentication = client.answer_challenge(challenge)
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        server.accept_auth(authentication)
    assert raised.value.code is protocol_v2.V2ErrorCode.AUTHENTICATION_FAILED
    assert server.state is protocol_v2.V2ConnectionState.FAILED
    assert not hasattr(raised.value, "proof")


def test_v2_authentication_is_connection_bound_and_server_proof_is_verified() -> None:
    first = protocol_v2.V2ServerConnection(V2_BOOT_SECRET, daemon_id=V2_DAEMON_ID)
    second = protocol_v2.V2ServerConnection(V2_BOOT_SECRET, daemon_id=V2_DAEMON_ID)
    client = protocol_v2.V2ClientConnection(
        V2_BOOT_SECRET,
        expected_daemon_id=V2_DAEMON_ID,
    )
    authentication = client.answer_challenge(first.start())
    second.start()

    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        second.accept_auth(authentication)
    assert raised.value.code is protocol_v2.V2ErrorCode.AUTHENTICATION_FAILED
    assert second.state is protocol_v2.V2ConnectionState.FAILED

    server = protocol_v2.V2ServerConnection(V2_BOOT_SECRET, daemon_id=V2_DAEMON_ID)
    verifier = protocol_v2.V2ClientConnection(
        V2_BOOT_SECRET,
        expected_daemon_id=V2_DAEMON_ID,
    )
    authentication = verifier.answer_challenge(server.start())
    ready = _v2_json(server.accept_auth(authentication))
    ready["session_id"] = "session_" + "f" * 32
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        verifier.accept_authenticated(_v2_raw(ready))
    assert raised.value.code is protocol_v2.V2ErrorCode.AUTHENTICATION_FAILED
    assert verifier.state is protocol_v2.V2ConnectionState.FAILED


def test_v2_challenges_and_sessions_are_unique() -> None:
    challenges: set[str] = set()
    sessions: set[str] = set()
    for _ in range(32):
        server = protocol_v2.V2ServerConnection(V2_BOOT_SECRET, daemon_id=V2_DAEMON_ID)
        client = protocol_v2.V2ClientConnection(
            V2_BOOT_SECRET,
            expected_daemon_id=V2_DAEMON_ID,
        )
        challenge = server.start()
        challenges.add(str(_v2_json(challenge)["server_nonce"]))
        ready = server.accept_auth(client.answer_challenge(challenge))
        client.accept_authenticated(ready)
        sessions.add(str(_v2_json(ready)["session_id"]))

    assert len(challenges) == 32
    assert len(sessions) == 32


def test_v2_static_dispatcher_routes_only_six_explicit_methods() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def handler(name: str):
        def invoke(params: dict[str, object]) -> dict[str, object]:
            calls.append((name, params))
            if name == "file_grant.claim":
                return _v2_file_grant_result()
            if name == "checkout.open":
                return {
                    "handled": name,
                    "file_grant": _v2_file_grant_descriptor(),
                }
            return {"handled": name}

        return invoke

    dispatcher = protocol_v2.StaticV2Dispatcher(
        kernel_ping=handler("kernel.ping"),
        application_call=handler("application.call"),
        checkout_open=handler("checkout.open"),
        checkout_get=handler("checkout.get"),
        checkout_close=handler("checkout.close"),
        file_grant_claim=handler("file_grant.claim"),
        allowed_application_operations=frozenset({"get_capabilities"}),
    )
    server, client = _ready_v2_pair()
    requests = (
        ("kernel.ping", {}),
        (
            "application.call",
            {"operation": "get_capabilities", "request": {"schema_version": 1}},
        ),
        (
            "checkout.open",
            {
                "open_key": "checkout_open_" + "1" * 32,
                "source": {"kind": "head", "project_id": "project_" + "2" * 32},
            },
        ),
        ("checkout.get", {"checkout_id": "checkout_" + "3" * 32}),
        ("checkout.close", {"checkout_id": "checkout_" + "3" * 32}),
        ("file_grant.claim", {"grant_id": V2_FILE_GRANT_ID}),
    )
    for index, (method, params) in enumerate(requests, start=1):
        raw = client.encode_request(method, params, request_id=_v2_request_id(index))
        response = client.decode_response(server.dispatch_and_encode(raw, dispatcher))
        if method == "file_grant.claim":
            expected = _v2_file_grant_result()
        elif method == "checkout.open":
            expected = {
                "handled": method,
                "file_grant": _v2_file_grant_descriptor(),
            }
        else:
            expected = {"handled": method}
        assert response.result == expected
        assert response.error is None

    assert [method for method, _params in calls] == [method for method, _params in requests]
    with pytest.raises(AttributeError):
        dispatcher.kernel_ping = handler("replacement")


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"file_grant": {}},
        {"file_grant": _v2_file_grant_descriptor(extra=True)},
        {"file_grant": _v2_file_grant_descriptor(schema_version=2)},
        {"file_grant": _v2_file_grant_descriptor(grant_id="grant_" + "b" * 32)},
        {"file_grant": _v2_file_grant_descriptor(purpose="read_any_path")},
        {"file_grant": _v2_file_grant_descriptor(expires_in_ms=29_999)},
        {"file_grant": _v2_file_grant_descriptor(expires_in_ms=True)},
    ],
)
def test_v2_checkout_open_server_rejects_a_non_exact_grant_descriptor(
    result: dict[str, object],
) -> None:
    server, client = _ready_v2_pair()
    request = server.admit_request(
        client.encode_request(
            "checkout.open",
            {
                "open_key": "checkout_open_" + "1" * 32,
                "source": {"kind": "head", "project_id": "project_" + "2" * 32},
            },
            request_id=_v2_request_id(1),
        )
    )

    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        server.encode_success(request, result)

    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_REQUEST


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"file_grant": _v2_file_grant_descriptor(extra=True)},
        {"file_grant": _v2_file_grant_descriptor(schema_version=2)},
        {"file_grant": _v2_file_grant_descriptor(grant_id="grant_" + "e" * 32)},
        {"file_grant": _v2_file_grant_descriptor(purpose="read_any_path")},
        {"file_grant": _v2_file_grant_descriptor(expires_in_ms=30_001)},
    ],
)
def test_v2_checkout_open_client_rejects_a_signed_non_exact_grant_descriptor(
    result: dict[str, object],
) -> None:
    server, client = _ready_v2_pair()
    request = server.admit_request(
        client.encode_request(
            "checkout.open",
            {
                "open_key": "checkout_open_" + "1" * 32,
                "source": {"kind": "head", "project_id": "project_" + "2" * 32},
            },
            request_id=_v2_request_id(1),
        )
    )
    encoded = server._encode_response(request, result=result, error=None)

    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        client.decode_response(encoded)

    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_REQUEST
    assert client.state is protocol_v2.V2ConnectionState.FAILED


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"grant_id": "grant_" + "b" * 32},
        {"grant_id": "file_grant_" + "b" * 31},
        {"grant_id": V2_FILE_GRANT_ID, "extra": True},
        {"grant_id": V2_FILE_GRANT_ID, "path": "/tmp/model.FCStd"},
        {"grant_id": V2_FILE_GRANT_ID, "local_path": "/tmp/model.FCStd"},
    ],
)
def test_v2_file_grant_claim_request_is_exact_before_handler(
    params: dict[str, object],
) -> None:
    calls = 0

    def claim(_params: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return _v2_file_grant_result()

    dispatcher = protocol_v2.StaticV2Dispatcher(file_grant_claim=claim)
    server, client = _ready_v2_pair()
    raw = client.encode_request(
        "file_grant.claim",
        params,
        request_id=_v2_request_id(1),
    )
    response = client.decode_response(server.dispatch_and_encode(raw, dispatcher))
    assert response.error == {
        "code": "invalid_request",
        "message": "The local protocol request is invalid.",
    }
    assert calls == 0


@pytest.mark.parametrize(
    "result",
    [
        _v2_file_grant_result(extra=True),
        _v2_file_grant_result(schema_version=2),
        _v2_file_grant_result(grant_id="grant_" + "b" * 32),
        _v2_file_grant_result(grant_id="file_grant_" + "e" * 32),
        _v2_file_grant_result(checkout_id="checkout_" + "e" * 32),
        _v2_file_grant_result(purpose="read_any_path"),
        _v2_file_grant_result(local_path="relative/model.FCStd"),
        _v2_file_grant_result(local_path=f"//private/checkouts/{V2_CHECKOUT_ID}/model.FCStd"),
        _v2_file_grant_result(local_path=f"/private/checkouts/./{V2_CHECKOUT_ID}/model.FCStd"),
        _v2_file_grant_result(
            local_path=f"/private/checkouts/{V2_CHECKOUT_ID}/../{V2_CHECKOUT_ID}/model.FCStd"
        ),
        _v2_file_grant_result(local_path=f"/private/checkouts/{V2_CHECKOUT_ID}/other.FCStd"),
        _v2_file_grant_result(
            local_path="/private/checkouts/checkout_" + "e" * 32 + "/model.FCStd"
        ),
        _v2_file_grant_result(
            local_path=f"/private/checkouts/{V2_CHECKOUT_ID}/model.FCStd\nforged"
        ),
        _v2_file_grant_result(
            local_path=f"/private/checkouts/{V2_CHECKOUT_ID}/model.FCStd\u0085forged"
        ),
        _v2_file_grant_result(current_model_sha256="not-a-digest"),
        _v2_file_grant_result(current_size_bytes=-1),
    ],
)
def test_v2_file_grant_claim_server_rejects_non_exact_path_result(
    result: dict[str, object],
) -> None:
    server, client = _ready_v2_pair()
    request = server.admit_request(
        client.encode_request(
            "file_grant.claim",
            {"grant_id": V2_FILE_GRANT_ID},
            request_id=_v2_request_id(1),
        )
    )
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        server.encode_success(request, result)
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_REQUEST


@pytest.mark.parametrize(
    "result",
    [
        _v2_file_grant_result(local_path="relative/model.FCStd"),
        _v2_file_grant_result(grant_id="file_grant_" + "e" * 32),
        _v2_file_grant_result(
            local_path=f"/private/checkouts/{V2_CHECKOUT_ID}/../{V2_CHECKOUT_ID}/model.FCStd"
        ),
        _v2_file_grant_result(
            local_path="/private/checkouts/checkout_" + "e" * 32 + "/model.FCStd"
        ),
        _v2_file_grant_result(extra=True),
    ],
)
def test_v2_file_grant_claim_client_rejects_signed_non_exact_path_result(
    result: dict[str, object],
) -> None:
    server, client = _ready_v2_pair()
    request = server.admit_request(
        client.encode_request(
            "file_grant.claim",
            {"grant_id": V2_FILE_GRANT_ID},
            request_id=_v2_request_id(1),
        )
    )
    forged = server._encode_response(request, result=result, error=None)
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        client.decode_response(forged)
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_REQUEST
    assert client.state is protocol_v2.V2ConnectionState.FAILED


def test_v2_file_grant_claim_is_the_only_result_allowed_to_reveal_local_path() -> None:
    server, client = _ready_v2_pair()
    claim = server.admit_request(
        client.encode_request(
            "file_grant.claim",
            {"grant_id": V2_FILE_GRANT_ID},
            request_id=_v2_request_id(1),
        )
    )
    assert (
        client.decode_response(server.encode_success(claim, _v2_file_grant_result())).result
        == _v2_file_grant_result()
    )

    server, client = _ready_v2_pair()
    ping = server.admit_request(
        client.encode_request("kernel.ping", {}, request_id=_v2_request_id(1))
    )
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        server.encode_success(ping, {"local_path": _v2_file_grant_result()["local_path"]})
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_REQUEST

    server, client = _ready_v2_pair()
    ping = server.admit_request(
        client.encode_request("kernel.ping", {}, request_id=_v2_request_id(1))
    )
    forged = server._encode_response(
        ping,
        result={"local_path": _v2_file_grant_result()["local_path"]},
        error=None,
    )
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        client.decode_response(forged)
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_REQUEST
    assert client.state is protocol_v2.V2ConnectionState.FAILED


def test_v2_unknown_unavailable_and_handler_failure_are_fixed_signed_errors() -> None:
    calls = 0

    def explode(_params: dict[str, object]) -> object:
        nonlocal calls
        calls += 1
        raise RuntimeError("/private/secret should never cross the wire")

    dispatcher = protocol_v2.StaticV2Dispatcher(kernel_ping=explode)
    server, client = _ready_v2_pair()

    unknown = client.encode_request(
        "__getattribute__",
        {},
        request_id=_v2_request_id(1),
    )
    response = client.decode_response(server.dispatch_and_encode(unknown, dispatcher))
    assert response.error == {
        "code": "unknown_method",
        "message": "The local protocol method is unknown.",
    }
    assert calls == 0

    unavailable = client.encode_request(
        "checkout.get",
        {"checkout_id": "checkout_" + "3" * 32},
        request_id=_v2_request_id(2),
    )
    response = client.decode_response(server.dispatch_and_encode(unavailable, dispatcher))
    assert response.error == {
        "code": "unavailable",
        "message": "The local protocol method is unavailable.",
    }
    assert calls == 0

    failing = client.encode_request("kernel.ping", {}, request_id=_v2_request_id(3))
    encoded = server.dispatch_and_encode(failing, dispatcher)
    assert b"/private/secret" not in encoded
    response = client.decode_response(encoded)
    assert response.error == {
        "code": "internal_error",
        "message": "The local protocol operation failed.",
    }
    assert calls == 1


def test_v2_dispatch_rejects_protocol_capabilities_but_preserves_public_source_path() -> None:
    calls: list[dict[str, object]] = []

    def application(params: dict[str, object]) -> dict[str, object]:
        calls.append(params)
        return {"ok": True}

    dispatcher = protocol_v2.StaticV2Dispatcher(
        application_call=application,
        allowed_application_operations=frozenset({"create_project", "get_capabilities"}),
    )
    server, client = _ready_v2_pair()

    for index, forbidden in enumerate(
        ("local_path", "internal_root", "environment", "env", "callable", "python_name"),
        start=1,
    ):
        raw = client.encode_request(
            "application.call",
            {
                "operation": "get_capabilities",
                "request": {"schema_version": 1, forbidden: "forbidden"},
            },
            request_id=_v2_request_id(index),
        )
        response = client.decode_response(server.dispatch_and_encode(raw, dispatcher))
        assert response.error == {
            "code": "invalid_request",
            "message": "The local protocol request is invalid.",
        }
    assert calls == []

    compatible = client.encode_request(
        "application.call",
        {
            "operation": "create_project",
            "request": {
                "schema_version": 1,
                "kind": "import_fcstd",
                "source_path": "/read-only/source.FCStd",
            },
        },
        request_id=_v2_request_id(20),
    )
    response = client.decode_response(server.dispatch_and_encode(compatible, dispatcher))
    assert response.result == {"ok": True}
    assert len(calls) == 1


def test_v2_invalid_handler_result_is_sanitized() -> None:
    dispatcher = protocol_v2.StaticV2Dispatcher(
        kernel_ping=lambda _params: {"local_path": "/private/result.FCStd"}
    )
    server, client = _ready_v2_pair()
    raw = client.encode_request("kernel.ping", {}, request_id=_v2_request_id(1))
    encoded = server.dispatch_and_encode(raw, dispatcher)

    assert b"/private/result" not in encoded
    response = client.decode_response(encoded)
    assert response.error is not None
    assert response.error["code"] == "internal_error"


def test_v2_request_mac_session_and_sequence_replay_fail_before_dispatch() -> None:
    dispatcher_calls = 0

    def ping(_params: dict[str, object]) -> dict[str, object]:
        nonlocal dispatcher_calls
        dispatcher_calls += 1
        return {"ok": True}

    dispatcher = protocol_v2.StaticV2Dispatcher(kernel_ping=ping)
    server, client = _ready_v2_pair()
    first = client.encode_request("kernel.ping", {}, request_id=_v2_request_id(1))
    response = client.decode_response(server.dispatch_and_encode(first, dispatcher))
    assert response.result == {"ok": True}
    assert dispatcher_calls == 1

    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        server.dispatch_and_encode(first, dispatcher)
    assert raised.value.code is protocol_v2.V2ErrorCode.REPLAYED_MESSAGE
    assert server.state is protocol_v2.V2ConnectionState.FAILED
    assert dispatcher_calls == 1

    gap_server, gap_client = _ready_v2_pair(daemon_id="daemon_" + "b" * 32)
    gap_client.encode_request("kernel.ping", {}, request_id=_v2_request_id(2))
    second = gap_client.encode_request("kernel.ping", {}, request_id=_v2_request_id(3))
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        gap_server.admit_request(second)
    assert raised.value.code is protocol_v2.V2ErrorCode.REPLAYED_MESSAGE


def test_v2_cross_session_and_authenticated_tamper_fail_closed() -> None:
    server_a, client_a = _ready_v2_pair(daemon_id="daemon_" + "a" * 32)
    server_b, _client_b = _ready_v2_pair(daemon_id="daemon_" + "b" * 32)
    signed = client_a.encode_request("kernel.ping", {}, request_id=_v2_request_id(1))

    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        server_b.admit_request(signed)
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_SESSION

    server_c, client_c = _ready_v2_pair(daemon_id="daemon_" + "c" * 32)
    tampered = _v2_json(client_c.encode_request("kernel.ping", {}, request_id=_v2_request_id(2)))
    tampered["params"] = {"extra": True}
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        server_c.admit_request(_v2_raw(tampered))
    assert raised.value.code is protocol_v2.V2ErrorCode.AUTHENTICATION_FAILED
    assert server_c.state is protocol_v2.V2ConnectionState.FAILED
    assert server_a.state is protocol_v2.V2ConnectionState.READY


def test_v2_duplicate_request_ids_and_in_flight_budget_are_bounded() -> None:
    server, client = _ready_v2_pair()
    first_id = _v2_request_id(1)
    first = client.encode_request("kernel.ping", {}, request_id=first_id)
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        client.encode_request("kernel.ping", {}, request_id=first_id)
    assert raised.value.code is protocol_v2.V2ErrorCode.DUPLICATE_REQUEST

    requests = [first] + [
        client.encode_request("kernel.ping", {}, request_id=_v2_request_id(index))
        for index in range(2, protocol_v2.MAX_V2_IN_FLIGHT + 1)
    ]
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        client.encode_request(
            "kernel.ping",
            {},
            request_id=_v2_request_id(protocol_v2.MAX_V2_IN_FLIGHT + 1),
        )
    assert raised.value.code is protocol_v2.V2ErrorCode.RESOURCE_EXHAUSTED

    admitted = server.admit_request(requests[0])
    client.decode_response(server.encode_success(admitted, {"ok": True}))
    client.encode_request(
        "kernel.ping",
        {},
        request_id=_v2_request_id(protocol_v2.MAX_V2_IN_FLIGHT + 1),
    )


def test_v2_client_correlates_out_of_order_responses_and_rejects_replay() -> None:
    server, client = _ready_v2_pair()
    first = server.admit_request(
        client.encode_request("kernel.ping", {}, request_id=_v2_request_id(1))
    )
    second = server.admit_request(
        client.encode_request("kernel.ping", {}, request_id=_v2_request_id(2))
    )
    first_response = server.encode_success(first, {"order": 1})
    second_response = server.encode_success(second, {"order": 2})

    assert client.decode_response(second_response).result == {"order": 2}
    assert client.decode_response(first_response).result == {"order": 1}
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        client.decode_response(first_response)
    assert raised.value.code is protocol_v2.V2ErrorCode.REPLAYED_MESSAGE
    assert client.state is protocol_v2.V2ConnectionState.FAILED


def test_v2_duplicate_json_unsupported_version_and_close_are_terminal() -> None:
    server = protocol_v2.V2ServerConnection(V2_BOOT_SECRET, daemon_id=V2_DAEMON_ID)
    client = protocol_v2.V2ClientConnection(
        V2_BOOT_SECRET,
        expected_daemon_id=V2_DAEMON_ID,
    )
    challenge = server.start()
    duplicated = challenge[:-1] + b',"type":"challenge"}'
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        client.answer_challenge(duplicated)
    assert raised.value.code is protocol_v2.V2ErrorCode.MALFORMED_MESSAGE

    server = protocol_v2.V2ServerConnection(V2_BOOT_SECRET, daemon_id=V2_DAEMON_ID)
    client = protocol_v2.V2ClientConnection(
        V2_BOOT_SECRET,
        expected_daemon_id=V2_DAEMON_ID,
    )
    wrong_version = _v2_json(server.start())
    wrong_version["version"] = {"major": 1, "minor": 0}
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        client.answer_challenge(_v2_raw(wrong_version))
    assert raised.value.code is protocol_v2.V2ErrorCode.UNSUPPORTED_VERSION

    ready_server, ready_client = _ready_v2_pair()
    ready_server.close()
    ready_client.close()
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        ready_client.encode_request("kernel.ping", {}, request_id=_v2_request_id(1))
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_STATE


def test_v2_server_response_claim_is_bound_to_the_admitting_connection() -> None:
    server_a, client_a = _ready_v2_pair(daemon_id="daemon_" + "a" * 32)
    server_b, client_b = _ready_v2_pair(daemon_id="daemon_" + "b" * 32)
    request_a = server_a.admit_request(
        client_a.encode_request("kernel.ping", {}, request_id=_v2_request_id(1))
    )
    request_b = server_b.admit_request(
        client_b.encode_request("kernel.ping", {}, request_id=_v2_request_id(1))
    )

    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        server_b.encode_success(request_a, {"wrong_connection": True})
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_REQUEST

    response = client_b.decode_response(server_b.encode_success(request_b, {"correct": True}))
    assert response.result == {"correct": True}


def test_v2_application_dispatch_requires_an_immutable_operation_allowlist() -> None:
    calls: list[str] = []

    def application(params: dict[str, object]) -> dict[str, object]:
        calls.append(str(params["operation"]))
        return {"ok": True}

    dispatcher = protocol_v2.StaticV2Dispatcher(
        application_call=application,
        allowed_application_operations=frozenset({"get_capabilities"}),
    )
    server, client = _ready_v2_pair()
    unknown = client.encode_request(
        "application.call",
        {"operation": "getattr", "request": {"schema_version": 1}},
        request_id=_v2_request_id(1),
    )
    response = client.decode_response(server.dispatch_and_encode(unknown, dispatcher))
    assert response.error == {
        "code": "unknown_method",
        "message": "The local protocol method is unknown.",
    }
    assert calls == []

    allowed = client.encode_request(
        "application.call",
        {"operation": "get_capabilities", "request": {"schema_version": 1}},
        request_id=_v2_request_id(2),
    )
    assert client.decode_response(server.dispatch_and_encode(allowed, dispatcher)).result == {
        "ok": True
    }
    assert calls == ["get_capabilities"]
    with pytest.raises(AttributeError):
        dispatcher._allowed_application_operations = frozenset({"getattr"})


def test_v2_frame_decoder_bounds_per_feed_aggregate_work() -> None:
    decoder = protocol_v2.V2FrameDecoder()
    nine_frames = b"".join(protocol_v2.encode_v2_frame(b"x") for _ in range(9))
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        decoder.feed(nine_frames)
    assert raised.value.code is protocol_v2.V2ErrorCode.RESOURCE_EXHAUSTED

    decoder = protocol_v2.V2FrameDecoder()
    oversized_fragment = b"x" * (
        protocol_v2.MAX_V2_FRAME_PAYLOAD_BYTES + protocol_v2.V2_FRAME_HEADER_BYTES + 1
    )
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        decoder.feed(oversized_fragment)
    assert raised.value.code is protocol_v2.V2ErrorCode.RESOURCE_EXHAUSTED

    decoder = protocol_v2.V2FrameDecoder()
    assert decoder.feed(protocol_v2.encode_v2_frame(b"done")) == (b"done",)
    decoder.finish()
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        decoder.feed(protocol_v2.encode_v2_frame(b"late"))
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_STATE

    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        protocol_v2.decode_v2_frame("not-bytes")
    assert raised.value.code is protocol_v2.V2ErrorCode.MALFORMED_FRAME


def test_v2_client_binds_expected_receipt_daemon_before_sending_proof() -> None:
    server = protocol_v2.V2ServerConnection(
        V2_BOOT_SECRET,
        daemon_id="daemon_" + "b" * 32,
    )
    client = protocol_v2.V2ClientConnection(
        V2_BOOT_SECRET,
        expected_daemon_id="daemon_" + "a" * 32,
    )

    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        client.answer_challenge(server.start())
    assert raised.value.code is protocol_v2.V2ErrorCode.AUTHENTICATION_FAILED
    assert client.state is protocol_v2.V2ConnectionState.FAILED


def test_v2_response_mac_session_and_request_correlation_fail_closed() -> None:
    server, client = _ready_v2_pair()
    request = server.admit_request(
        client.encode_request("kernel.ping", {}, request_id=_v2_request_id(1))
    )
    tampered = _v2_json(server.encode_success(request, {"ok": True}))
    tampered["result"] = {"ok": False}
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        client.decode_response(_v2_raw(tampered))
    assert raised.value.code is protocol_v2.V2ErrorCode.AUTHENTICATION_FAILED
    assert client.state is protocol_v2.V2ConnectionState.FAILED

    server, client = _ready_v2_pair(daemon_id="daemon_" + "b" * 32)
    request = server.admit_request(
        client.encode_request("kernel.ping", {}, request_id=_v2_request_id(2))
    )
    wrong_session = _v2_json(server.encode_success(request, {"ok": True}))
    wrong_session["session_id"] = "session_" + "f" * 32
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        client.decode_response(_v2_raw(wrong_session))
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_SESSION

    server, client = _ready_v2_pair(daemon_id="daemon_" + "c" * 32)
    request = server.admit_request(
        client.encode_request("kernel.ping", {}, request_id=_v2_request_id(3))
    )
    wrong_request = _v2_json(server.encode_success(request, {"ok": True}))
    wrong_request["request_id"] = _v2_request_id(4)
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        client.decode_response(_v2_raw(wrong_request))
    assert raised.value.code is protocol_v2.V2ErrorCode.AUTHENTICATION_FAILED


def test_v2_server_enforces_in_flight_and_duplicate_id_against_signed_clients() -> None:
    server, client = _ready_v2_pair()
    for index in range(1, protocol_v2.MAX_V2_IN_FLIGHT + 1):
        server.admit_request(
            client.encode_request("kernel.ping", {}, request_id=_v2_request_id(index))
        )
    client._active.clear()
    ninth = client.encode_request(
        "kernel.ping",
        {},
        request_id=_v2_request_id(protocol_v2.MAX_V2_IN_FLIGHT + 1),
    )
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        server.admit_request(ninth)
    assert raised.value.code is protocol_v2.V2ErrorCode.RESOURCE_EXHAUSTED
    assert server.state is protocol_v2.V2ConnectionState.FAILED

    server, client = _ready_v2_pair(daemon_id="daemon_" + "d" * 32)
    duplicate_id = _v2_request_id(20)
    server.admit_request(client.encode_request("kernel.ping", {}, request_id=duplicate_id))
    client._seen_request_ids.remove(duplicate_id)
    duplicate = client.encode_request("kernel.ping", {}, request_id=duplicate_id)
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        server.admit_request(duplicate)
    assert raised.value.code is protocol_v2.V2ErrorCode.DUPLICATE_REQUEST
    assert server.state is protocol_v2.V2ConnectionState.FAILED


def test_v2_json_depth_node_key_and_string_budgets_have_exact_boundaries() -> None:
    assert protocol_v2.MAX_V2_DEPTH == 72
    assert protocol_v2.MAX_V2_NODES == 10_240
    assert protocol_v2.MAX_V2_KEY_BYTES == 256
    assert protocol_v2.MAX_V2_STRING_BYTES == 524_288
    _server, client = _ready_v2_pair()

    payload: object = None
    for _ in range(protocol_v2.MAX_V2_DEPTH - 4):
        payload = [payload]
    client.encode_request(
        "application.call",
        {"operation": "get_capabilities", "request": {"payload": payload}},
        request_id=_v2_request_id(1),
    )
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        client.encode_request(
            "application.call",
            {"operation": "get_capabilities", "request": {"payload": [payload]}},
            request_id=_v2_request_id(2),
        )
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_REQUEST

    node_payload = [None] * (protocol_v2.MAX_V2_NODES - 15)
    client.encode_request(
        "application.call",
        {"operation": "get_capabilities", "request": {"payload": node_payload}},
        request_id=_v2_request_id(3),
    )
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        client.encode_request(
            "application.call",
            {
                "operation": "get_capabilities",
                "request": {"payload": node_payload + [None]},
            },
            request_id=_v2_request_id(4),
        )
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_REQUEST

    client.encode_request(
        "application.call",
        {
            "operation": "get_capabilities",
            "request": {"k" * protocol_v2.MAX_V2_KEY_BYTES: None},
        },
        request_id=_v2_request_id(5),
    )
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        client.encode_request(
            "application.call",
            {
                "operation": "get_capabilities",
                "request": {"k" * (protocol_v2.MAX_V2_KEY_BYTES + 1): None},
            },
            request_id=_v2_request_id(6),
        )
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_REQUEST

    client.encode_request(
        "application.call",
        {
            "operation": "get_capabilities",
            "request": {"payload": "x" * protocol_v2.MAX_V2_STRING_BYTES},
        },
        request_id=_v2_request_id(7),
    )
    with pytest.raises(protocol_v2.V2ProtocolError) as raised:
        client.encode_request(
            "application.call",
            {
                "operation": "get_capabilities",
                "request": {"payload": "x" * (protocol_v2.MAX_V2_STRING_BYTES + 1)},
            },
            request_id=_v2_request_id(8),
        )
    assert raised.value.code is protocol_v2.V2ErrorCode.INVALID_REQUEST
