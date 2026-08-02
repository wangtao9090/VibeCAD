"""Bounded stdio bridge between a thin FreeCAD addon and the local Task Kernel."""

from __future__ import annotations

import json
import math
import secrets
import sys
from collections.abc import Callable
from enum import Enum
from typing import BinaryIO

from vibecad import __version__

BRIDGE_PROTOCOL = "vibecad-freecad-bridge"
BRIDGE_PROTOCOL_VERSION = 1
MAX_BRIDGE_FRAME_BYTES = 1_048_576

_MAX_DEPTH = 12
_MAX_NODES = 10_000
_MAX_STRING = 4096
_MAX_KEY = 128
_MAX_CONTAINER = 1000
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_DAEMON_PREFIX = "daemon_"
_ERROR_CODES = frozenset(
    {
        "invalid_input",
        "unavailable",
        "internal_error",
        "closed",
        "wrong_process",
        "incompatible_kernel",
    }
)
_METHOD_KEYS = {
    "ping": frozenset(),
    "list_projects": frozenset({"request"}),
    "list_tasks": frozenset({"request"}),
    "get_project": frozenset({"request"}),
    "get_task": frozenset({"request"}),
    "accept_draft": frozenset({"request"}),
    "reject_draft": frozenset({"request"}),
    "open_checkout": frozenset({"open_key", "source"}),
    "get_checkout": frozenset({"checkout_id"}),
    "checkpoint_checkout": frozenset({"checkpoint_key", "checkout_id"}),
    "close_checkout": frozenset({"checkout_id"}),
    "claim_file_grant": frozenset({"grant_id"}),
    "resolve_selector": frozenset({"request"}),
    "close": frozenset(),
}


class _IdentityRecord:
    __slots__ = (
        "TypeId",
        "VibeCADFeatureId",
        "VibeCADObjectId",
        "VibeCADProvenance",
        "VibeCADSemanticRole",
    )

    def __init__(self, value: dict[str, object]) -> None:
        self.VibeCADObjectId = value["object_id"]
        self.VibeCADFeatureId = value["feature_id"]
        self.TypeId = value["object_type"]
        self.VibeCADSemanticRole = value["semantic_role"]
        self.VibeCADProvenance = value["provenance"]


class BridgeProtocolError(ValueError):
    """Fixed bridge framing or schema rejection."""


def _plain(value: object, *, depth: int = 0, budget: list[int] | None = None) -> object:
    if budget is None:
        budget = [_MAX_NODES]
    budget[0] -= 1
    if budget[0] < 0 or depth > _MAX_DEPTH:
        raise BridgeProtocolError("invalid bridge value")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
            raise BridgeProtocolError("invalid bridge value")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise BridgeProtocolError("invalid bridge value")
        return value
    if type(value) is str:
        if len(value.encode("utf-8")) > _MAX_STRING:
            raise BridgeProtocolError("invalid bridge value")
        return value
    if type(value) is list:
        if len(value) > _MAX_CONTAINER:
            raise BridgeProtocolError("invalid bridge value")
        return [_plain(item, depth=depth + 1, budget=budget) for item in value]
    if type(value) is dict:
        if len(value) > _MAX_CONTAINER or any(
            type(key) is not str or len(key.encode("utf-8")) > _MAX_KEY for key in value
        ):
            raise BridgeProtocolError("invalid bridge value")
        return {key: _plain(item, depth=depth + 1, budget=budget) for key, item in value.items()}
    raise BridgeProtocolError("invalid bridge value")


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise BridgeProtocolError("invalid bridge JSON")
        result[key] = value
    return result


def encode_bridge_frame(value: object) -> bytes:
    copied = _plain(value)
    if type(copied) is not dict:
        raise BridgeProtocolError("bridge frame must be a mapping")
    try:
        payload = json.dumps(
            copied,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, UnicodeError, ValueError):
        raise BridgeProtocolError("invalid bridge JSON") from None
    if not payload or len(payload) > MAX_BRIDGE_FRAME_BYTES:
        raise BridgeProtocolError("invalid bridge frame size")
    return len(payload).to_bytes(4, "big") + payload


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = stream.read(size - len(result))
        if not chunk:
            raise BridgeProtocolError("bridge stream closed")
        result.extend(chunk)
    return bytes(result)


def read_bridge_frame(stream: BinaryIO) -> dict[str, object]:
    header = _read_exact(stream, 4)
    size = int.from_bytes(header, "big")
    if not 0 < size <= MAX_BRIDGE_FRAME_BYTES:
        raise BridgeProtocolError("invalid bridge frame size")
    payload = _read_exact(stream, size)
    try:
        value = json.loads(payload.decode("ascii"), object_pairs_hook=_json_object)
    except (BridgeProtocolError, UnicodeError, ValueError, json.JSONDecodeError):
        raise BridgeProtocolError("invalid bridge JSON") from None
    copied = _plain(value)
    if type(copied) is not dict:
        raise BridgeProtocolError("bridge frame must be a mapping")
    return copied


def _write_bridge_frame(stream: BinaryIO, value: object) -> None:
    stream.write(encode_bridge_frame(value))
    stream.flush()


def _identifier(value: object, prefix: str) -> str:
    if (
        type(value) is not str
        or len(value) != len(prefix) + 32
        or not value.startswith(prefix)
        or any(character not in "0123456789abcdef" for character in value[len(prefix) :])
    ):
        raise BridgeProtocolError("invalid bridge identifier")
    return value


def _known_error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, Enum) and type(code.value) is str and code.value in _ERROR_CODES:
        return code.value
    return "internal_error"


def _response(
    request_id: int,
    *,
    result: dict[str, object] | None = None,
    error: str | None = None,
) -> dict[str, object]:
    if (result is None) == (error is None):
        raise RuntimeError("invalid bridge response")
    if error is not None:
        return {
            "schema_version": 1,
            "kind": "response",
            "request_id": request_id,
            "ok": False,
            "error": {"code": error},
        }
    return {
        "schema_version": 1,
        "kind": "response",
        "request_id": request_id,
        "ok": True,
        "result": result,
    }


def _ready(value: object, *, nonce: str) -> None:
    expected = {
        "schema_version": 1,
        "kind": "ready",
        "protocol": BRIDGE_PROTOCOL,
        "protocol_version": BRIDGE_PROTOCOL_VERSION,
        "nonce": nonce,
    }
    if value != expected:
        raise BridgeProtocolError("invalid bridge handshake")


def _default_nonce() -> str:
    return secrets.token_hex(16)


def _request(value: object, *, highwater: int) -> tuple[int, str, dict[str, object]]:
    if type(value) is not dict or set(value) != {
        "schema_version",
        "kind",
        "request_id",
        "method",
        "params",
    }:
        raise BridgeProtocolError("invalid bridge request")
    request_id = value.get("request_id")
    method = value.get("method")
    params = value.get("params")
    if (
        value.get("schema_version") != 1
        or value.get("kind") != "request"
        or type(request_id) is not int
        or not highwater < request_id <= _MAX_SAFE_INTEGER
        or type(method) is not str
        or type(params) is not dict
    ):
        raise BridgeProtocolError("invalid bridge request")
    return request_id, method, params


def _dispatch(client: object, method: str, params: dict[str, object]) -> dict[str, object]:
    keys = _METHOD_KEYS.get(method)
    if keys is None or set(params) != keys:
        raise BridgeProtocolError("invalid bridge method")
    if method == "resolve_selector":
        result = _resolve_selector(params["request"])
    elif method == "ping":
        result = client.ping()
    elif method in {
        "list_projects",
        "list_tasks",
        "get_project",
        "get_task",
        "accept_draft",
        "reject_draft",
    }:
        result = getattr(client, f"{method}_request")(params["request"])
    elif method == "open_checkout":
        result = client.open_checkout(open_key=params["open_key"], source=params["source"])
    elif method == "checkpoint_checkout":
        result = client.checkpoint_checkout(
            checkpoint_key=params["checkpoint_key"],
            checkout_id=params["checkout_id"],
        )
    elif method in {"get_checkout", "close_checkout"}:
        result = getattr(client, method)(checkout_id=params["checkout_id"])
    elif method == "claim_file_grant":
        result = client.claim_file_grant(grant_id=params["grant_id"])
    else:
        assert method == "close"
        client.close()
        result = {}
    copied = _plain(result)
    if type(copied) is not dict:
        raise BridgeProtocolError("invalid bridge result")
    return copied


def _resolve_selector(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != {
        "schema_version",
        "project_id",
        "revision_id",
        "selected_index",
        "objects",
    }:
        raise BridgeProtocolError("invalid selector request")
    project_id = value["project_id"]
    revision_id = value["revision_id"]
    selected_index = value["selected_index"]
    objects = value["objects"]
    if (
        value["schema_version"] != 1
        or type(project_id) is not str
        or type(revision_id) is not str
        or type(selected_index) is not int
        or type(objects) is not list
        or not 0 < len(objects) <= _MAX_CONTAINER
        or not 0 <= selected_index < len(objects)
    ):
        raise BridgeProtocolError("invalid selector request")
    keys = {"object_id", "feature_id", "object_type", "semantic_role", "provenance"}
    if any(type(item) is not dict or set(item) != keys for item in objects):
        raise BridgeProtocolError("invalid selector request")
    try:
        from vibecad.execution.selectors import (
            EntityKind,
            SelectorError,
            parse_entity_identity,
            resolve_selector,
        )

        records = tuple(_IdentityRecord(item) for item in objects)
        selected = records[selected_index]
        identity = parse_entity_identity(selected)
        kind = EntityKind.FEATURE if identity.feature_id is not None else EntityKind.OBJECT
        selector = identity.to_selector(
            project_id=project_id,
            revision_id=revision_id,
            entity_kind=kind,
        )
        if (
            resolve_selector(
                selector,
                records,
                project_id=project_id,
                revision_id=revision_id,
            )
            is not selected
        ):
            raise BridgeProtocolError("invalid selector resolution")
    except SelectorError:
        raise BridgeProtocolError("invalid selector request") from None
    mapping = selector.to_mapping()
    text = json.dumps(
        mapping,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return {"schema_version": 1, "selector": mapping, "text": text}


def serve_bridge(
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    *,
    client_factory: Callable[[], object] | None = None,
    nonce_factory: Callable[[], str] | None = None,
) -> int:
    """Serve one process-bound client until an authenticated close request."""

    if client_factory is None:
        from vibecad.daemon import LocalAgentClient

        client_factory = LocalAgentClient.open
    if nonce_factory is None:
        nonce_factory = _default_nonce
    client = client_factory()
    client_closed = False
    try:
        daemon_id = _identifier(getattr(client, "daemon_id", None), _DAEMON_PREFIX)
        nonce = nonce_factory()
        if (
            type(nonce) is not str
            or len(nonce) != 32
            or any(character not in "0123456789abcdef" for character in nonce)
        ):
            raise BridgeProtocolError("invalid bridge nonce")
        _write_bridge_frame(
            output_stream,
            {
                "schema_version": 1,
                "kind": "hello",
                "protocol": BRIDGE_PROTOCOL,
                "protocol_version": BRIDGE_PROTOCOL_VERSION,
                "package_version": __version__,
                "daemon_id": daemon_id,
                "nonce": nonce,
            },
        )
        _ready(read_bridge_frame(input_stream), nonce=nonce)
        _write_bridge_frame(
            output_stream,
            {
                "schema_version": 1,
                "kind": "ready",
                "protocol": BRIDGE_PROTOCOL,
                "protocol_version": BRIDGE_PROTOCOL_VERSION,
            },
        )
        highwater = -1
        while True:
            value = read_bridge_frame(input_stream)
            request_id, method, params = _request(value, highwater=highwater)
            highwater = request_id
            try:
                result = _dispatch(client, method, params)
            except BridgeProtocolError:
                response = _response(request_id, error="invalid_input")
            except BaseException as error:
                response = _response(request_id, error=_known_error_code(error))
            else:
                response = _response(request_id, result=result)
            _write_bridge_frame(output_stream, response)
            if method == "close":
                client_closed = response["ok"] is True
                return 0 if client_closed else 1
    finally:
        if not client_closed:
            try:
                client.close()
            except BaseException:
                pass


def main() -> int:
    try:
        return serve_bridge(sys.stdin.buffer, sys.stdout.buffer)
    except BaseException:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "BRIDGE_PROTOCOL",
    "BRIDGE_PROTOCOL_VERSION",
    "MAX_BRIDGE_FRAME_BYTES",
    "BridgeProtocolError",
    "encode_bridge_frame",
    "main",
    "read_bridge_frame",
    "serve_bridge",
)
