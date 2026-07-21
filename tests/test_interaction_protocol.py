from __future__ import annotations

import json

import pytest

import vibecad.interaction.protocol as protocol_module
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
