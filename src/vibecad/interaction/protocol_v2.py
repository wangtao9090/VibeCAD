"""Authenticated, framed local interaction protocol v2.

This module defines transport values and a connection-local authentication
state machine.  It deliberately does not create sockets, discover a daemon,
construct an application, or dispatch through Python names supplied on the
wire.  The runnable daemon composes these primitives in P0B-C09.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

__all__ = (
    "MAX_V2_CONNECTIONS",
    "MAX_V2_DEPTH",
    "MAX_V2_FRAME_PAYLOAD_BYTES",
    "MAX_V2_FRAMES_PER_FEED",
    "MAX_V2_IN_FLIGHT",
    "MAX_V2_KEY_BYTES",
    "MAX_V2_NODES",
    "MAX_V2_STRING_BYTES",
    "StaticV2Dispatcher",
    "V2ClientConnection",
    "V2ConnectionState",
    "V2ErrorCode",
    "V2FrameDecoder",
    "V2ProtocolError",
    "V2Request",
    "V2Response",
    "V2_FRAME_HEADER_BYTES",
    "V2_HANDSHAKE_TIMEOUT_SECONDS",
    "V2_IDLE_TIMEOUT_SECONDS",
    "V2_PROTOCOL",
    "V2_VERSION",
    "V2ServerConnection",
    "bind_v2_import_locator",
    "decode_v2_frame",
    "encode_v2_frame",
)

V2_PROTOCOL = "vibecad.local"
V2_VERSION = (2, 0)
V2_FRAME_HEADER_BYTES = 4
MAX_V2_FRAME_PAYLOAD_BYTES = 1_048_576
MAX_V2_CONNECTIONS = 8
MAX_V2_IN_FLIGHT = 8
MAX_V2_FRAMES_PER_FEED = MAX_V2_IN_FLIGHT
V2_HANDSHAKE_TIMEOUT_SECONDS = 5.0
V2_IDLE_TIMEOUT_SECONDS = 30.0

MAX_V2_DEPTH = 72
MAX_V2_NODES = 10_240
MAX_V2_STRING_BYTES = 524_288
MAX_V2_KEY_BYTES = 256
_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_SESSION_REQUESTS = 65_536

_DAEMON_RE = re.compile(r"daemon_[0-9a-f]{32}\Z")
_SESSION_RE = re.compile(r"session_[0-9a-f]{32}\Z")
_REQUEST_RE = re.compile(r"request_[0-9a-f]{32}\Z")
_OPEN_KEY_RE = re.compile(r"checkout_open_[0-9a-f]{32}\Z")
_CHECKOUT_RE = re.compile(r"checkout_[0-9a-f]{32}\Z")
_FILE_GRANT_RE = re.compile(r"file_grant_[0-9a-f]{32}\Z")
_PROJECT_RE = re.compile(r"project_[0-9a-f]{32}\Z")
_PROJECT_CREATE_RE = re.compile(r"project_create_[0-9a-f]{32}\Z")
_TASK_RE = re.compile(r"task_[0-9a-f]{32}\Z")
_DRAFT_RE = re.compile(r"draft_[0-9a-f]{32}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_OPERATION_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_TIMESTAMP_RE = re.compile(r"-?[0-9]{1,20}\Z")

_CLIENT_AUTH_DOMAIN = b"vibecad-local-v2-client-auth\0"
_SERVER_AUTH_DOMAIN = b"vibecad-local-v2-server-auth\0"
_SESSION_KEY_DOMAIN = b"vibecad-local-v2-session-key\0"
_REQUEST_DOMAIN = b"vibecad-local-v2-request\0"
_RESPONSE_DOMAIN = b"vibecad-local-v2-response\0"
_IMPORT_LOCATOR_DOMAIN = b"vibecad-local-v2-import-locator-v1\0"

_FORBIDDEN_CAPABILITY_KEYS = frozenset(
    {
        "callable",
        "env",
        "environment",
        "internal_root",
        "local_path",
        "python_name",
        "source_path",
    }
)
_METHODS = (
    "kernel.ping",
    "kernel.retire",
    "application.call",
    "project.import",
    "checkout.open",
    "checkout.get",
    "checkout.close",
    "file_grant.claim",
)
_KERNEL_RETIRE_REASONS = frozenset(
    {
        "incompatible_build",
        "runtime_uninstall",
        "runtime_upgrade",
    }
)
_FILE_GRANT_PURPOSE = "open_managed_checkout"
_FILE_GRANT_TTL_MS = 30_000
_MAX_LOCAL_PATH_BYTES = 4096
_MAX_IMPORT_BYTES = 512 * 1024 * 1024


class V2ErrorCode(StrEnum):
    MALFORMED_FRAME = "malformed_frame"
    TRUNCATED_FRAME = "truncated_frame"
    FRAME_TOO_LARGE = "frame_too_large"
    MALFORMED_MESSAGE = "malformed_message"
    UNSUPPORTED_VERSION = "unsupported_version"
    AUTHENTICATION_FAILED = "authentication_failed"
    INVALID_SESSION = "invalid_session"
    REPLAYED_MESSAGE = "replayed_message"
    DUPLICATE_REQUEST = "duplicate_request"
    UNKNOWN_METHOD = "unknown_method"
    INVALID_REQUEST = "invalid_request"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    UNAVAILABLE = "unavailable"
    INTERNAL_ERROR = "internal_error"
    INVALID_STATE = "invalid_state"


_ERROR_MESSAGES = {
    V2ErrorCode.MALFORMED_FRAME: "The local protocol frame is malformed.",
    V2ErrorCode.TRUNCATED_FRAME: "The local protocol frame is truncated.",
    V2ErrorCode.FRAME_TOO_LARGE: "The local protocol frame exceeds its fixed budget.",
    V2ErrorCode.MALFORMED_MESSAGE: "The local protocol message is malformed.",
    V2ErrorCode.UNSUPPORTED_VERSION: "The local protocol version is unsupported.",
    V2ErrorCode.AUTHENTICATION_FAILED: "Local protocol authentication failed.",
    V2ErrorCode.INVALID_SESSION: "The local protocol session is invalid.",
    V2ErrorCode.REPLAYED_MESSAGE: "The local protocol message was replayed or reordered.",
    V2ErrorCode.DUPLICATE_REQUEST: "The local protocol request identifier was reused.",
    V2ErrorCode.UNKNOWN_METHOD: "The local protocol method is unknown.",
    V2ErrorCode.INVALID_REQUEST: "The local protocol request is invalid.",
    V2ErrorCode.RESOURCE_EXHAUSTED: "The local protocol resource budget is exhausted.",
    V2ErrorCode.UNAVAILABLE: "The local protocol method is unavailable.",
    V2ErrorCode.INTERNAL_ERROR: "The local protocol operation failed.",
    V2ErrorCode.INVALID_STATE: "The local protocol connection state is invalid.",
}
_PUBLIC_FAILURE_CODES = frozenset(
    {
        V2ErrorCode.UNKNOWN_METHOD,
        V2ErrorCode.INVALID_REQUEST,
        V2ErrorCode.RESOURCE_EXHAUSTED,
        V2ErrorCode.UNAVAILABLE,
        V2ErrorCode.INTERNAL_ERROR,
    }
)


class V2ProtocolError(ValueError):
    __slots__ = ("code", "message")

    def __init__(self, code: V2ErrorCode) -> None:
        if type(code) is not V2ErrorCode:
            raise TypeError("code must be a V2ErrorCode")
        self.code = code
        self.message = _ERROR_MESSAGES[code]
        super().__init__(self.message)


class V2ConnectionState(StrEnum):
    NEW = "new"
    CHALLENGE_SENT = "challenge_sent"
    AUTH_SENT = "auth_sent"
    READY = "ready"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class V2Request:
    request_id: str
    sequence: int
    method: str
    params: dict[str, object]

    def __post_init__(self) -> None:
        _identifier(self.request_id, _REQUEST_RE)
        _positive_integer(self.sequence)
        _bounded_ascii(self.method, 64)
        if type(self.params) is not dict:
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)


@dataclass(frozen=True, slots=True)
class V2Response:
    request_id: str
    sequence: int
    result: dict[str, object] | None
    error: dict[str, str] | None

    def __post_init__(self) -> None:
        _identifier(self.request_id, _REQUEST_RE)
        _positive_integer(self.sequence)
        if (self.result is None) == (self.error is None):
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)


class _JsonFailure(Exception):
    pass


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _JsonFailure
        result[key] = value
    return result


def _integer(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:  # pragma: no cover - json limits this path
        raise _JsonFailure from exc
    if value < -_MAX_SAFE_INTEGER or value > _MAX_SAFE_INTEGER:
        raise _JsonFailure
    return value


def _constant(_raw: str) -> object:
    raise _JsonFailure


def _walk(
    value: object,
    *,
    code: V2ErrorCode,
    forbid_capabilities: bool = False,
) -> None:
    stack: list[tuple[bool, object, int]] = [(False, value, 1)]
    active: set[int] = set()
    nodes = 0
    while stack:
        exiting, item, depth = stack.pop()
        if exiting:
            active.remove(id(item))
            continue
        if depth > MAX_V2_DEPTH:
            raise V2ProtocolError(code)
        nodes += 1
        if nodes > MAX_V2_NODES:
            raise V2ProtocolError(code)
        if type(item) is dict:
            identity = id(item)
            if identity in active:
                raise V2ProtocolError(code)
            active.add(identity)
            stack.append((True, item, depth))
            for key, child in reversed(tuple(item.items())):
                if type(key) is not str:
                    raise V2ProtocolError(code)
                try:
                    key_size = len(key.encode("utf-8"))
                except UnicodeEncodeError:
                    raise V2ProtocolError(code) from None
                if key_size > MAX_V2_KEY_BYTES:
                    raise V2ProtocolError(code)
                if forbid_capabilities and key in _FORBIDDEN_CAPABILITY_KEYS:
                    raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
                stack.append((False, child, depth + 1))
        elif type(item) is list:
            identity = id(item)
            if identity in active:
                raise V2ProtocolError(code)
            active.add(identity)
            stack.append((True, item, depth))
            for child in reversed(item):
                stack.append((False, child, depth + 1))
        elif type(item) is str:
            try:
                size = len(item.encode("utf-8"))
            except UnicodeEncodeError:
                raise V2ProtocolError(code) from None
            if size > MAX_V2_STRING_BYTES:
                raise V2ProtocolError(code)
        elif type(item) is int and not -_MAX_SAFE_INTEGER <= item <= _MAX_SAFE_INTEGER:
            raise V2ProtocolError(code)
        elif type(item) is float and not math.isfinite(item):
            raise V2ProtocolError(code)
        elif item is not None and type(item) not in (bool, int, float):
            raise V2ProtocolError(code)


def _decode(raw: object) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_V2_FRAME_PAYLOAD_BYTES:
        raise V2ProtocolError(V2ErrorCode.MALFORMED_MESSAGE)
    if raw.startswith(b"\xef\xbb\xbf"):
        raise V2ProtocolError(V2ErrorCode.MALFORMED_MESSAGE)
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_int=_integer,
            parse_constant=_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _JsonFailure, RecursionError):
        raise V2ProtocolError(V2ErrorCode.MALFORMED_MESSAGE) from None
    _walk(value, code=V2ErrorCode.MALFORMED_MESSAGE)
    if type(value) is not dict:
        raise V2ProtocolError(V2ErrorCode.MALFORMED_MESSAGE)
    return value


def _encode(value: dict[str, object]) -> bytes:
    _walk(value, code=V2ErrorCode.INVALID_REQUEST)
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST) from None
    if not raw or len(raw) > MAX_V2_FRAME_PAYLOAD_BYTES:
        raise V2ProtocolError(V2ErrorCode.FRAME_TOO_LARGE)
    return raw


def _exact(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    return value


def _identifier(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    return value


def _bounded_ascii(value: object, maximum: int) -> str:
    if type(value) is not str:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST) from None
    if not 1 <= len(encoded) <= maximum or any(char < 0x20 or char > 0x7E for char in encoded):
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    return value


def _positive_integer(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_SAFE_INTEGER:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    return value


def _nonnegative_integer(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SAFE_INTEGER:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    return value


def _nonce(value: object) -> str:
    return _identifier(value, _DIGEST_RE)


def _proof(value: object) -> str:
    return _identifier(value, _DIGEST_RE)


def _header(value: dict[str, object], kind: str) -> None:
    if value.get("protocol") != V2_PROTOCOL:
        raise V2ProtocolError(V2ErrorCode.UNSUPPORTED_VERSION)
    version = value.get("version")
    if (
        type(version) is not dict
        or set(version) != {"major", "minor"}
        or type(version["major"]) is not int
        or type(version["minor"]) is not int
        or (version["major"], version["minor"]) != V2_VERSION
    ):
        raise V2ProtocolError(V2ErrorCode.UNSUPPORTED_VERSION)
    if value.get("type") != kind:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)


def _base(kind: str) -> dict[str, object]:
    return {
        "protocol": V2_PROTOCOL,
        "version": {"major": V2_VERSION[0], "minor": V2_VERSION[1]},
        "type": kind,
    }


def _mac(secret: bytes, domain: bytes, *values: dict[str, object]) -> str:
    digest = hmac.new(secret, domain, hashlib.sha256)
    for value in values:
        encoded = _encode(value)
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _session_key(
    secret: bytes,
    *,
    daemon_id: str,
    server_nonce: str,
    client_nonce: str,
    session_id: str,
) -> bytes:
    transcript = "\0".join((daemon_id, server_nonce, client_nonce, session_id)).encode("ascii")
    return hmac.new(secret, _SESSION_KEY_DOMAIN + transcript, hashlib.sha256).digest()


def _secret(value: object) -> bytes:
    if type(value) is not bytes or len(value) != hashlib.sha256().digest_size:
        raise V2ProtocolError(V2ErrorCode.AUTHENTICATION_FAILED)
    return value


def encode_v2_frame(payload: object) -> bytes:
    if type(payload) is not bytes or not payload:
        raise V2ProtocolError(V2ErrorCode.MALFORMED_FRAME)
    if len(payload) > MAX_V2_FRAME_PAYLOAD_BYTES:
        raise V2ProtocolError(V2ErrorCode.FRAME_TOO_LARGE)
    return len(payload).to_bytes(V2_FRAME_HEADER_BYTES, "big") + payload


def decode_v2_frame(frame: object) -> bytes:
    if type(frame) is not bytes:
        raise V2ProtocolError(V2ErrorCode.MALFORMED_FRAME)
    if len(frame) < V2_FRAME_HEADER_BYTES:
        raise V2ProtocolError(V2ErrorCode.TRUNCATED_FRAME)
    declared = int.from_bytes(frame[:V2_FRAME_HEADER_BYTES], "big")
    if declared == 0:
        raise V2ProtocolError(V2ErrorCode.MALFORMED_FRAME)
    if declared > MAX_V2_FRAME_PAYLOAD_BYTES:
        raise V2ProtocolError(V2ErrorCode.FRAME_TOO_LARGE)
    actual = len(frame) - V2_FRAME_HEADER_BYTES
    if actual < declared:
        raise V2ProtocolError(V2ErrorCode.TRUNCATED_FRAME)
    if actual > declared:
        raise V2ProtocolError(V2ErrorCode.MALFORMED_FRAME)
    return frame[V2_FRAME_HEADER_BYTES:]


class V2FrameDecoder:
    """Incremental decoder retaining at most one declared payload plus a header."""

    __slots__ = ("_buffer", "_expected", "_failed", "_finished")

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._expected: int | None = None
        self._failed = False
        self._finished = False

    def _fail(self, code: V2ErrorCode) -> None:
        self._buffer.clear()
        self._expected = None
        self._failed = True
        raise V2ProtocolError(code)

    def feed(self, fragment: object) -> tuple[bytes, ...]:
        if self._failed or self._finished:
            raise V2ProtocolError(V2ErrorCode.INVALID_STATE)
        if type(fragment) is not bytes:
            self._fail(V2ErrorCode.MALFORMED_FRAME)
        if (
            len(fragment) > MAX_V2_FRAME_PAYLOAD_BYTES + V2_FRAME_HEADER_BYTES
            or len(self._buffer) + len(fragment)
            > MAX_V2_FRAME_PAYLOAD_BYTES + V2_FRAME_HEADER_BYTES
        ):
            self._fail(V2ErrorCode.RESOURCE_EXHAUSTED)
        self._buffer.extend(fragment)
        frames: list[bytes] = []
        while True:
            if self._expected is None:
                if len(self._buffer) < V2_FRAME_HEADER_BYTES:
                    break
                declared = int.from_bytes(self._buffer[:V2_FRAME_HEADER_BYTES], "big")
                del self._buffer[:V2_FRAME_HEADER_BYTES]
                if declared == 0:
                    self._fail(V2ErrorCode.MALFORMED_FRAME)
                if declared > MAX_V2_FRAME_PAYLOAD_BYTES:
                    self._fail(V2ErrorCode.FRAME_TOO_LARGE)
                self._expected = declared
            if len(self._buffer) < self._expected:
                break
            if len(frames) >= MAX_V2_FRAMES_PER_FEED:
                self._fail(V2ErrorCode.RESOURCE_EXHAUSTED)
            frames.append(bytes(self._buffer[: self._expected]))
            del self._buffer[: self._expected]
            self._expected = None
        return tuple(frames)

    def finish(self) -> None:
        if self._failed or self._finished:
            raise V2ProtocolError(V2ErrorCode.INVALID_STATE)
        if self._expected is not None or self._buffer:
            self._fail(V2ErrorCode.TRUNCATED_FRAME)
        self._finished = True


def _challenge(raw: object) -> dict[str, object]:
    value = _exact(
        _decode(raw),
        {"protocol", "version", "type", "daemon_id", "server_nonce"},
    )
    _header(value, "challenge")
    _identifier(value["daemon_id"], _DAEMON_RE)
    _nonce(value["server_nonce"])
    return value


def _authentication(raw: object) -> dict[str, object]:
    value = _exact(
        _decode(raw),
        {
            "protocol",
            "version",
            "type",
            "daemon_id",
            "server_nonce",
            "client_nonce",
            "client_name",
            "client_version",
            "client_proof",
        },
    )
    _header(value, "authenticate")
    _identifier(value["daemon_id"], _DAEMON_RE)
    _nonce(value["server_nonce"])
    _nonce(value["client_nonce"])
    _bounded_ascii(value["client_name"], 64)
    _bounded_ascii(value["client_version"], 32)
    _proof(value["client_proof"])
    return value


def _authenticated(raw: object) -> dict[str, object]:
    value = _exact(
        _decode(raw),
        {"protocol", "version", "type", "daemon_id", "session_id", "server_proof"},
    )
    _header(value, "authenticated")
    _identifier(value["daemon_id"], _DAEMON_RE)
    _identifier(value["session_id"], _SESSION_RE)
    _proof(value["server_proof"])
    return value


def _request(raw: object) -> dict[str, object]:
    value = _exact(
        _decode(raw),
        {
            "protocol",
            "version",
            "type",
            "session_id",
            "sequence",
            "request_id",
            "method",
            "params",
            "proof",
        },
    )
    _header(value, "request")
    _identifier(value["session_id"], _SESSION_RE)
    _positive_integer(value["sequence"])
    _identifier(value["request_id"], _REQUEST_RE)
    _bounded_ascii(value["method"], 64)
    if type(value["params"]) is not dict:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    _proof(value["proof"])
    return value


def _response(raw: object) -> dict[str, object]:
    value = _exact(
        _decode(raw),
        {
            "protocol",
            "version",
            "type",
            "session_id",
            "sequence",
            "request_id",
            "result",
            "error",
            "proof",
        },
    )
    _header(value, "response")
    _identifier(value["session_id"], _SESSION_RE)
    _positive_integer(value["sequence"])
    _identifier(value["request_id"], _REQUEST_RE)
    _proof(value["proof"])
    return value


def _without(value: dict[str, object], key: str) -> dict[str, object]:
    return {name: item for name, item in value.items() if name != key}


_IMPORT_REQUEST_KEYS = {"schema_version", "create_key", "kind"}
_IMPORT_LOCATOR_KEYS = {
    "schema_version",
    "dev",
    "ino",
    "mode",
    "uid",
    "nlink",
    "size",
    "mtime_ns",
    "ctime_ns",
    "digest",
}


def _validate_import_request(value: object) -> dict[str, object]:
    request = _exact(value, _IMPORT_REQUEST_KEYS)
    if _positive_integer(request["schema_version"]) != 1 or request["kind"] != "import_fcstd":
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    _identifier(request["create_key"], _PROJECT_CREATE_RE)
    return request


def _import_locator_body(value: object) -> dict[str, object]:
    locator = _exact(value, _IMPORT_LOCATOR_KEYS)
    if _positive_integer(locator["schema_version"]) != 1:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    for key in ("dev", "ino", "mode", "uid"):
        _nonnegative_integer(locator[key])
    if _positive_integer(locator["nlink"]) != 1:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    size = _positive_integer(locator["size"])
    if size > _MAX_IMPORT_BYTES:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    for key in ("mtime_ns", "ctime_ns"):
        if type(locator[key]) is not str or _TIMESTAMP_RE.fullmatch(locator[key]) is None:
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    _identifier(locator["digest"], _DIGEST_RE)
    if not stat.S_ISREG(int(locator["mode"])) or int(locator["uid"]) != os.geteuid():
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    return _without(locator, "digest")


def _validate_import_locator(
    request: object,
    value: object,
) -> dict[str, object]:
    canonical_request = _validate_import_request(request)
    body = _import_locator_body(value)
    locator = value
    expected = hashlib.sha256(
        _IMPORT_LOCATOR_DOMAIN + _encode({"request": canonical_request, "identity": body})
    ).hexdigest()
    if not hmac.compare_digest(str(locator["digest"]), expected):
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    return dict(locator)


def bind_v2_import_locator(
    request: object,
    source: object,
) -> dict[str, object]:
    """Bind one admitted import request to one exact regular-file identity."""

    canonical_request = _validate_import_request(request)
    if type(source) is not os.stat_result:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    body: dict[str, object] = {
        "schema_version": 1,
        "dev": source.st_dev,
        "ino": source.st_ino,
        "mode": source.st_mode,
        "uid": source.st_uid,
        "nlink": source.st_nlink,
        "size": source.st_size,
        "mtime_ns": str(source.st_mtime_ns),
        "ctime_ns": str(source.st_ctime_ns),
    }
    _import_locator_body(body | {"digest": "0" * 64})
    digest = hashlib.sha256(
        _IMPORT_LOCATOR_DOMAIN + _encode({"request": canonical_request, "identity": body})
    ).hexdigest()
    return body | {"digest": digest}


def _validate_source(value: object) -> None:
    if type(value) is not dict:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    kind = value.get("kind")
    if kind == "head":
        source = _exact(value, {"kind", "project_id"})
        _identifier(source["project_id"], _PROJECT_RE)
        return
    if kind == "draft":
        source = _exact(value, {"kind", "task_id", "draft_id", "expected_generation"})
        _identifier(source["task_id"], _TASK_RE)
        _identifier(source["draft_id"], _DRAFT_RE)
        _nonnegative_integer(source["expected_generation"])
        return
    raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)


def _validate_dispatch_params(method: str, params: dict[str, object]) -> None:
    _walk(
        params,
        code=V2ErrorCode.INVALID_REQUEST,
        forbid_capabilities=True,
    )
    if method == "kernel.ping":
        _exact(params, set())
        return
    if method == "kernel.retire":
        value = _exact(params, {"daemon_id", "reason"})
        _identifier(value["daemon_id"], _DAEMON_RE)
        if value["reason"] not in _KERNEL_RETIRE_REASONS:
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
        return
    if method == "application.call":
        value = _exact(params, {"operation", "request"})
        operation = value["operation"]
        if type(operation) is not str or _OPERATION_RE.fullmatch(operation) is None:
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
        if type(value["request"]) is not dict:
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
        return
    if method == "project.import":
        value = _exact(params, {"request", "locator"})
        _validate_import_locator(value["request"], value["locator"])
        return
    if method == "checkout.open":
        value = _exact(params, {"open_key", "source"})
        _identifier(value["open_key"], _OPEN_KEY_RE)
        _validate_source(value["source"])
        return
    if method in {"checkout.get", "checkout.close"}:
        value = _exact(params, {"checkout_id"})
        _identifier(value["checkout_id"], _CHECKOUT_RE)
        return
    if method == "file_grant.claim":
        value = _exact(params, {"grant_id"})
        _identifier(value["grant_id"], _FILE_GRANT_RE)
        return
    raise V2ProtocolError(V2ErrorCode.UNKNOWN_METHOD)


def _claim_local_path(value: object, *, checkout_id: str) -> str:
    if type(value) is not str:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST) from None
    if not 1 <= len(raw) <= _MAX_LOCAL_PATH_BYTES or any(
        not character.isprintable() for character in value
    ):
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or path.root != "/"
        or str(path) != value
        or ".." in path.parts
        or path.name != "model.FCStd"
        or path.parent.name != checkout_id
    ):
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    return value


def _validate_file_grant_descriptor(value: object) -> None:
    grant = _exact(
        value,
        {
            "schema_version",
            "grant_id",
            "purpose",
            "expires_in_ms",
        },
    )
    if type(grant["schema_version"]) is not int or grant["schema_version"] != 1:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    _identifier(grant["grant_id"], _FILE_GRANT_RE)
    if (
        grant["purpose"] != _FILE_GRANT_PURPOSE
        or type(grant["expires_in_ms"]) is not int
        or grant["expires_in_ms"] != _FILE_GRANT_TTL_MS
    ):
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)


def _validate_success_result(
    method: str,
    result: object,
    *,
    expected_grant_id: str | None,
) -> dict[str, object]:
    if type(result) is not dict:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    if method != "file_grant.claim":
        _walk(
            result,
            code=V2ErrorCode.INVALID_REQUEST,
            forbid_capabilities=True,
        )
        if method == "checkout.open":
            _validate_file_grant_descriptor(result.get("file_grant"))
        return result
    _walk(result, code=V2ErrorCode.INVALID_REQUEST)
    value = _exact(
        result,
        {
            "schema_version",
            "grant_id",
            "checkout_id",
            "purpose",
            "local_path",
            "current_model_sha256",
            "current_size_bytes",
        },
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    grant_id = _identifier(value["grant_id"], _FILE_GRANT_RE)
    if expected_grant_id is None or grant_id != expected_grant_id:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    checkout_id = _identifier(value["checkout_id"], _CHECKOUT_RE)
    if value["purpose"] != _FILE_GRANT_PURPOSE:
        raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    _claim_local_path(value["local_path"], checkout_id=checkout_id)
    _identifier(value["current_model_sha256"], _DIGEST_RE)
    _nonnegative_integer(value["current_size_bytes"])
    return value


_Handler = Callable[[dict[str, object]], object]
_DescriptorHandler = Callable[[dict[str, object], int], object]


@dataclass(frozen=True, slots=True, init=False)
class StaticV2Dispatcher:
    """Closed dispatcher whose handlers are installed explicitly in code."""

    _kernel_ping: _Handler | None
    _kernel_retire: _Handler | None
    _application_call: _Handler | None
    _project_import: _DescriptorHandler | None
    _checkout_open: _Handler | None
    _checkout_get: _Handler | None
    _checkout_close: _Handler | None
    _file_grant_claim: _Handler | None
    _allowed_application_operations: frozenset[str]

    def __init__(
        self,
        *,
        kernel_ping: _Handler | None = None,
        kernel_retire: _Handler | None = None,
        application_call: _Handler | None = None,
        project_import: _DescriptorHandler | None = None,
        checkout_open: _Handler | None = None,
        checkout_get: _Handler | None = None,
        checkout_close: _Handler | None = None,
        file_grant_claim: _Handler | None = None,
        allowed_application_operations: frozenset[str] = frozenset(),
    ) -> None:
        handlers = (
            kernel_ping,
            kernel_retire,
            application_call,
            project_import,
            checkout_open,
            checkout_get,
            checkout_close,
            file_grant_claim,
        )
        if any(handler is not None and not callable(handler) for handler in handlers):
            raise TypeError("dispatcher handlers must be callable or None")
        if (
            type(allowed_application_operations) is not frozenset
            or len(allowed_application_operations) > 128
            or any(
                type(operation) is not str or _OPERATION_RE.fullmatch(operation) is None
                for operation in allowed_application_operations
            )
        ):
            raise TypeError("allowed application operations must be a bounded frozenset")
        object.__setattr__(self, "_kernel_ping", kernel_ping)
        object.__setattr__(self, "_kernel_retire", kernel_retire)
        object.__setattr__(self, "_application_call", application_call)
        object.__setattr__(self, "_project_import", project_import)
        object.__setattr__(self, "_checkout_open", checkout_open)
        object.__setattr__(self, "_checkout_get", checkout_get)
        object.__setattr__(self, "_checkout_close", checkout_close)
        object.__setattr__(self, "_file_grant_claim", file_grant_claim)
        object.__setattr__(
            self,
            "_allowed_application_operations",
            allowed_application_operations,
        )

    def dispatch(
        self,
        request: object,
        *,
        descriptor: int | None = None,
    ) -> object:
        if type(request) is not V2Request:
            raise TypeError("request must be a V2Request")
        _validate_dispatch_params(request.method, request.params)
        if descriptor is not None and (type(descriptor) is not int or descriptor < 0):
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
        if request.method == "project.import":
            if descriptor is None or self._project_import is None:
                raise V2ProtocolError(
                    V2ErrorCode.INVALID_REQUEST if descriptor is None else V2ErrorCode.UNAVAILABLE
                )
            return self._project_import(dict(request.params), descriptor)
        if descriptor is not None:
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
        if request.method == "kernel.ping":
            handler = self._kernel_ping
        elif request.method == "kernel.retire":
            handler = self._kernel_retire
        elif request.method == "application.call":
            if request.params["operation"] not in self._allowed_application_operations:
                raise V2ProtocolError(V2ErrorCode.UNKNOWN_METHOD)
            handler = self._application_call
        elif request.method == "checkout.open":
            handler = self._checkout_open
        elif request.method == "checkout.get":
            handler = self._checkout_get
        elif request.method == "checkout.close":
            handler = self._checkout_close
        elif request.method == "file_grant.claim":
            handler = self._file_grant_claim
        else:  # _validate_dispatch_params has already rejected unknown methods
            raise V2ProtocolError(V2ErrorCode.UNKNOWN_METHOD)
        if handler is None:
            raise V2ProtocolError(V2ErrorCode.UNAVAILABLE)
        return handler(dict(request.params))


class V2ServerConnection:
    """One server-side, connection-bound authentication and replay boundary."""

    __slots__ = (
        "_active",
        "_authentication",
        "_boot_secret",
        "_challenge",
        "_daemon_id",
        "_next_sequence",
        "_seen_request_ids",
        "_session_id",
        "_session_key",
        "state",
    )

    def __init__(self, boot_secret: object, *, daemon_id: object) -> None:
        self._boot_secret = _secret(boot_secret)
        self._daemon_id = _identifier(daemon_id, _DAEMON_RE)
        self._challenge: dict[str, object] | None = None
        self._authentication: dict[str, object] | None = None
        self._session_id: str | None = None
        self._session_key: bytes | None = None
        self._next_sequence = 1
        self._active: dict[int, V2Request] = {}
        self._seen_request_ids: set[str] = set()
        self.state = V2ConnectionState.NEW

    @property
    def session_id(self) -> str:
        if self.state is not V2ConnectionState.READY or self._session_id is None:
            raise V2ProtocolError(V2ErrorCode.INVALID_STATE)
        return self._session_id

    def _terminal(self, code: V2ErrorCode) -> None:
        self._boot_secret = b""
        self._session_key = None
        self._active.clear()
        self.state = V2ConnectionState.FAILED
        raise V2ProtocolError(code)

    def start(self) -> bytes:
        if self.state is not V2ConnectionState.NEW:
            self._terminal(V2ErrorCode.INVALID_STATE)
        challenge = _base("challenge") | {
            "daemon_id": self._daemon_id,
            "server_nonce": secrets.token_hex(32),
        }
        self._challenge = challenge
        self.state = V2ConnectionState.CHALLENGE_SENT
        return _encode(challenge)

    def accept_auth(self, payload: object) -> bytes:
        if self.state is not V2ConnectionState.CHALLENGE_SENT or self._challenge is None:
            self._terminal(V2ErrorCode.INVALID_STATE)
        try:
            authentication = _authentication(payload)
        except V2ProtocolError as exc:
            self._terminal(exc.code)
        if (
            authentication["daemon_id"] != self._daemon_id
            or authentication["server_nonce"] != self._challenge["server_nonce"]
        ):
            self._terminal(V2ErrorCode.AUTHENTICATION_FAILED)
        auth_body = _without(authentication, "client_proof")
        expected = _mac(
            self._boot_secret,
            _CLIENT_AUTH_DOMAIN,
            self._challenge,
            auth_body,
        )
        if not hmac.compare_digest(str(authentication["client_proof"]), expected):
            self._terminal(V2ErrorCode.AUTHENTICATION_FAILED)
        session_id = "session_" + secrets.token_hex(16)
        ready_body = _base("authenticated") | {
            "daemon_id": self._daemon_id,
            "session_id": session_id,
        }
        server_proof = _mac(
            self._boot_secret,
            _SERVER_AUTH_DOMAIN,
            self._challenge,
            auth_body,
            ready_body,
        )
        self._session_key = _session_key(
            self._boot_secret,
            daemon_id=self._daemon_id,
            server_nonce=str(self._challenge["server_nonce"]),
            client_nonce=str(authentication["client_nonce"]),
            session_id=session_id,
        )
        self._authentication = auth_body
        self._session_id = session_id
        self._boot_secret = b""
        self.state = V2ConnectionState.READY
        return _encode(ready_body | {"server_proof": server_proof})

    def admit_request(self, payload: object) -> V2Request:
        if (
            self.state is not V2ConnectionState.READY
            or self._session_id is None
            or self._session_key is None
        ):
            raise V2ProtocolError(V2ErrorCode.INVALID_STATE)
        try:
            value = _request(payload)
        except V2ProtocolError as exc:
            self._terminal(exc.code)
        if value["session_id"] != self._session_id:
            self._terminal(V2ErrorCode.INVALID_SESSION)
        body = _without(value, "proof")
        expected = _mac(self._session_key, _REQUEST_DOMAIN, body)
        if not hmac.compare_digest(str(value["proof"]), expected):
            self._terminal(V2ErrorCode.AUTHENTICATION_FAILED)
        sequence = int(value["sequence"])
        if sequence != self._next_sequence:
            self._terminal(V2ErrorCode.REPLAYED_MESSAGE)
        request_id = str(value["request_id"])
        if request_id in self._seen_request_ids:
            self._terminal(V2ErrorCode.DUPLICATE_REQUEST)
        if (
            len(self._active) >= MAX_V2_IN_FLIGHT
            or len(self._seen_request_ids) >= _MAX_SESSION_REQUESTS
        ):
            self._terminal(V2ErrorCode.RESOURCE_EXHAUSTED)
        request = V2Request(
            request_id=request_id,
            sequence=sequence,
            method=str(value["method"]),
            params=dict(value["params"]),
        )
        self._active[sequence] = request
        self._seen_request_ids.add(request_id)
        self._next_sequence += 1
        return request

    def _claim_response(self, request: object) -> V2Request:
        if (
            self.state is not V2ConnectionState.READY
            or self._session_id is None
            or self._session_key is None
        ):
            raise V2ProtocolError(V2ErrorCode.INVALID_STATE)
        if type(request) is not V2Request:
            raise TypeError("request must be a V2Request")
        if self._active.get(request.sequence) is not request:
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
        return request

    def _encode_response(
        self,
        request: V2Request,
        *,
        result: dict[str, object] | None,
        error: dict[str, str] | None,
    ) -> bytes:
        body = _base("response") | {
            "session_id": self._session_id,
            "sequence": request.sequence,
            "request_id": request.request_id,
            "result": result,
            "error": error,
        }
        proof = _mac(self._session_key or b"", _RESPONSE_DOMAIN, body)
        encoded = _encode(body | {"proof": proof})
        del self._active[request.sequence]
        return encoded

    def encode_success(self, request: object, result: object) -> bytes:
        canonical = self._claim_response(request)
        expected_grant_id = (
            canonical.params.get("grant_id")
            if canonical.method == "file_grant.claim"
            and set(canonical.params) == {"grant_id"}
            and type(canonical.params.get("grant_id")) is str
            else None
        )
        canonical_result = _validate_success_result(
            canonical.method,
            result,
            expected_grant_id=expected_grant_id,
        )
        return self._encode_response(
            canonical,
            result=dict(canonical_result),
            error=None,
        )

    def encode_failure(self, request: object, code: object) -> bytes:
        canonical = self._claim_response(request)
        if type(code) is not V2ErrorCode or code not in _PUBLIC_FAILURE_CODES:
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
        return self._encode_response(
            canonical,
            result=None,
            error={"code": code.value, "message": _ERROR_MESSAGES[code]},
        )

    def dispatch_and_encode(
        self,
        payload: object,
        dispatcher: object,
        *,
        descriptor: int | None = None,
    ) -> bytes:
        if type(dispatcher) is not StaticV2Dispatcher:
            raise TypeError("dispatcher must be a StaticV2Dispatcher")
        request = self.admit_request(payload)
        try:
            result = dispatcher.dispatch(request, descriptor=descriptor)
        except V2ProtocolError as exc:
            code = exc.code if exc.code in _PUBLIC_FAILURE_CODES else V2ErrorCode.INTERNAL_ERROR
            return self.encode_failure(request, code)
        except Exception:
            return self.encode_failure(request, V2ErrorCode.INTERNAL_ERROR)
        try:
            return self.encode_success(request, result)
        except V2ProtocolError:
            return self.encode_failure(request, V2ErrorCode.INTERNAL_ERROR)

    def close(self) -> None:
        self._boot_secret = b""
        self._session_key = None
        self._active.clear()
        self.state = V2ConnectionState.CLOSED


class V2ClientConnection:
    """One client-side mutual-authentication and response-correlation boundary."""

    __slots__ = (
        "_active",
        "_authentication",
        "_boot_secret",
        "_challenge",
        "_client_name",
        "_client_version",
        "_daemon_id",
        "_expected_daemon_id",
        "_next_sequence",
        "_seen_request_ids",
        "_session_id",
        "_session_key",
        "state",
    )

    def __init__(
        self,
        boot_secret: object,
        *,
        expected_daemon_id: object,
        client_name: object = "vibecad-client",
        client_version: object = "0",
    ) -> None:
        self._boot_secret = _secret(boot_secret)
        self._expected_daemon_id = _identifier(expected_daemon_id, _DAEMON_RE)
        self._client_name = _bounded_ascii(client_name, 64)
        self._client_version = _bounded_ascii(client_version, 32)
        self._challenge: dict[str, object] | None = None
        self._authentication: dict[str, object] | None = None
        self._daemon_id: str | None = None
        self._session_id: str | None = None
        self._session_key: bytes | None = None
        self._next_sequence = 1
        self._active: dict[int, tuple[str, str, str | None]] = {}
        self._seen_request_ids: set[str] = set()
        self.state = V2ConnectionState.NEW

    def _terminal(self, code: V2ErrorCode) -> None:
        self._boot_secret = b""
        self._session_key = None
        self._active.clear()
        self.state = V2ConnectionState.FAILED
        raise V2ProtocolError(code)

    def answer_challenge(self, payload: object) -> bytes:
        if self.state is not V2ConnectionState.NEW:
            self._terminal(V2ErrorCode.INVALID_STATE)
        try:
            challenge = _challenge(payload)
        except V2ProtocolError as exc:
            self._terminal(exc.code)
        if challenge["daemon_id"] != self._expected_daemon_id:
            self._terminal(V2ErrorCode.AUTHENTICATION_FAILED)
        auth_body = _base("authenticate") | {
            "daemon_id": challenge["daemon_id"],
            "server_nonce": challenge["server_nonce"],
            "client_nonce": secrets.token_hex(32),
            "client_name": self._client_name,
            "client_version": self._client_version,
        }
        client_proof = _mac(
            self._boot_secret,
            _CLIENT_AUTH_DOMAIN,
            challenge,
            auth_body,
        )
        self._challenge = challenge
        self._authentication = auth_body
        self._daemon_id = str(challenge["daemon_id"])
        self.state = V2ConnectionState.AUTH_SENT
        return _encode(auth_body | {"client_proof": client_proof})

    def accept_authenticated(self, payload: object) -> None:
        if (
            self.state is not V2ConnectionState.AUTH_SENT
            or self._challenge is None
            or self._authentication is None
            or self._daemon_id is None
        ):
            self._terminal(V2ErrorCode.INVALID_STATE)
        try:
            value = _authenticated(payload)
        except V2ProtocolError as exc:
            self._terminal(exc.code)
        if value["daemon_id"] != self._daemon_id:
            self._terminal(V2ErrorCode.AUTHENTICATION_FAILED)
        ready_body = _without(value, "server_proof")
        expected = _mac(
            self._boot_secret,
            _SERVER_AUTH_DOMAIN,
            self._challenge,
            self._authentication,
            ready_body,
        )
        if not hmac.compare_digest(str(value["server_proof"]), expected):
            self._terminal(V2ErrorCode.AUTHENTICATION_FAILED)
        session_id = str(value["session_id"])
        self._session_key = _session_key(
            self._boot_secret,
            daemon_id=self._daemon_id,
            server_nonce=str(self._challenge["server_nonce"]),
            client_nonce=str(self._authentication["client_nonce"]),
            session_id=session_id,
        )
        self._session_id = session_id
        self._boot_secret = b""
        self.state = V2ConnectionState.READY

    def encode_request(
        self,
        method: object,
        params: object,
        *,
        request_id: object,
    ) -> bytes:
        if (
            self.state is not V2ConnectionState.READY
            or self._session_id is None
            or self._session_key is None
        ):
            raise V2ProtocolError(V2ErrorCode.INVALID_STATE)
        canonical_method = _bounded_ascii(method, 64)
        canonical_id = _identifier(request_id, _REQUEST_RE)
        if type(params) is not dict:
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
        _walk(params, code=V2ErrorCode.INVALID_REQUEST)
        if canonical_id in self._seen_request_ids:
            raise V2ProtocolError(V2ErrorCode.DUPLICATE_REQUEST)
        if (
            len(self._active) >= MAX_V2_IN_FLIGHT
            or len(self._seen_request_ids) >= _MAX_SESSION_REQUESTS
        ):
            raise V2ProtocolError(V2ErrorCode.RESOURCE_EXHAUSTED)
        sequence = self._next_sequence
        body = _base("request") | {
            "session_id": self._session_id,
            "sequence": sequence,
            "request_id": canonical_id,
            "method": canonical_method,
            "params": dict(params),
        }
        proof = _mac(self._session_key, _REQUEST_DOMAIN, body)
        encoded = _encode(body | {"proof": proof})
        expected_grant_id = (
            params.get("grant_id")
            if canonical_method == "file_grant.claim"
            and set(params) == {"grant_id"}
            and type(params.get("grant_id")) is str
            else None
        )
        self._active[sequence] = (
            canonical_id,
            canonical_method,
            expected_grant_id,
        )
        self._seen_request_ids.add(canonical_id)
        self._next_sequence += 1
        return encoded

    def decode_response(self, payload: object) -> V2Response:
        if (
            self.state is not V2ConnectionState.READY
            or self._session_id is None
            or self._session_key is None
        ):
            raise V2ProtocolError(V2ErrorCode.INVALID_STATE)
        try:
            value = _response(payload)
        except V2ProtocolError as exc:
            self._terminal(exc.code)
        if value["session_id"] != self._session_id:
            self._terminal(V2ErrorCode.INVALID_SESSION)
        body = _without(value, "proof")
        expected = _mac(self._session_key, _RESPONSE_DOMAIN, body)
        if not hmac.compare_digest(str(value["proof"]), expected):
            self._terminal(V2ErrorCode.AUTHENTICATION_FAILED)
        sequence = int(value["sequence"])
        request_id = str(value["request_id"])
        active = self._active.get(sequence)
        if active is None:
            self._terminal(V2ErrorCode.REPLAYED_MESSAGE)
        if active[0] != request_id:
            self._terminal(V2ErrorCode.INVALID_SESSION)
        result = value["result"]
        error = value["error"]
        if (result is None) == (error is None):
            self._terminal(V2ErrorCode.INVALID_REQUEST)
        if result is not None:
            try:
                validated = _validate_success_result(
                    active[1],
                    result,
                    expected_grant_id=active[2],
                )
            except V2ProtocolError as exc:
                self._terminal(exc.code)
            canonical_result: dict[str, object] | None = dict(validated)
            canonical_error: dict[str, str] | None = None
        else:
            try:
                error_map = _exact(error, {"code", "message"})
                code = V2ErrorCode(error_map["code"])
            except (TypeError, ValueError, V2ProtocolError):
                self._terminal(V2ErrorCode.INVALID_REQUEST)
            if code not in _PUBLIC_FAILURE_CODES or error_map["message"] != _ERROR_MESSAGES[code]:
                self._terminal(V2ErrorCode.INVALID_REQUEST)
            canonical_result = None
            canonical_error = {"code": code.value, "message": _ERROR_MESSAGES[code]}
        del self._active[sequence]
        return V2Response(
            request_id=request_id,
            sequence=sequence,
            result=canonical_result,
            error=canonical_error,
        )

    def close(self) -> None:
        self._boot_secret = b""
        self._session_key = None
        self._active.clear()
        self.state = V2ConnectionState.CLOSED
