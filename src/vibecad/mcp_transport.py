"""Bounded, SDK-independent machinery for VibeCAD's owned MCP transport.

This module owns stdio framing, admission, worker scheduling, response writes,
and process-lifecycle coordination.  It deliberately does not import
:mod:`mcp` or dispatch application calls, so raw input is validated before the
server converts it to SDK request types:

* newline framing with a fixed request-wire ceiling and drain-on-overflow;
* strict UTF-8, duplicate-aware JSON lexical validation and decoding;
* a closed structural prevalidator for the supported client-message union;
* active JSON-RPC id, work, worker, resource, and overflow-control admission.

All public protocol failures use fixed strings.  Apart from a validated
JSON-RPC id (the protocol-mandated echo), caller data is never copied into an
error response or exception message.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast

READ_CHUNK_BYTES = 65_536
MAX_REQUEST_FRAME_BYTES = 2_097_152
MAX_RESPONSE_FRAME_BYTES = 100_663_296

MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 65_536
MAX_JSON_CONTAINER_ITEMS = 65_536
MAX_JSON_KEY_BYTES = 256
MAX_JSON_STRING_BYTES = 1_048_576
MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991
MAX_JSON_INTEGER_DIGITS = 16
MAX_JSON_FLOAT_TOKEN_BYTES = 64

MAX_IN_FLIGHT = 8
MAX_WORKERS = 4
MAX_RESOURCE_READS = 1
UNINSTALL_DRAIN_SECONDS = 30.0
SWAP_EXIT_CODE = 75

ARTIFACT_RESOURCE_URI_BYTES = 141
_TYPED_RESOURCE_URI = re.compile(
    r"^[A-Za-z][A-Za-z0-9+.-]{0,31}://[!-~]+$",
    re.ASCII,
)


@dataclass(frozen=True, slots=True)
class FixedRpcError:
    """A protocol error whose code and message contain no caller data."""

    code: int
    message: str


PARSE_ERROR = FixedRpcError(-32700, "Parse error")
INVALID_REQUEST = FixedRpcError(-32600, "Invalid Request")
INVALID_REQUEST_PARAMS = FixedRpcError(-32602, "Invalid request parameters")
TOOL_REQUEST_INVALID = FixedRpcError(-32602, "Tool request is invalid.")
TOOL_NAME_UNAVAILABLE = FixedRpcError(-32602, "Tool name is not available.")
INTERNAL_ERROR = FixedRpcError(-32603, "Tool request could not be completed.")
SERVER_BUSY = FixedRpcError(-32005, "Server is busy.")
REQUEST_CANCELLED = FixedRpcError(-32800, "Request cancelled")
GENERIC_INTERNAL_ERROR = FixedRpcError(-32603, "Internal error")


def _request_id_key(value: object) -> tuple[str, int | str]:
    if type(value) is int and abs(value) <= MAX_SAFE_JSON_INTEGER:
        return ("integer", value)
    if type(value) is str and len(value.encode("utf-8")) <= MAX_JSON_STRING_BYTES:
        return ("string", value)
    raise TransportProtocolError(INVALID_REQUEST)


def _is_request_id(value: object) -> bool:
    try:
        _request_id_key(value)
    except (TransportProtocolError, UnicodeEncodeError):
        return False
    return True


def rpc_error_response(
    error: FixedRpcError,
    *,
    request_id: object | None = None,
) -> dict[str, object]:
    """Build one minimal JSON-RPC error without reflecting unvalidated data."""

    safe_id: int | str | None = None
    if request_id is not None and _is_request_id(request_id):
        safe_id = cast("int | str", request_id)
    return {
        "jsonrpc": "2.0",
        "id": safe_id,
        "error": {"code": error.code, "message": error.message},
    }


class TransportProtocolError(ValueError):
    """A sanitized request failure suitable for the owned wire boundary."""

    def __init__(
        self,
        error: FixedRpcError,
        *,
        request_id: object | None = None,
        close: bool = False,
    ) -> None:
        super().__init__(error.message)
        self.error = error
        self.response = rpc_error_response(error, request_id=request_id)
        self.close = close


class TransportLeaseError(RuntimeError):
    """Raised for an invalid or prematurely released admission lease."""

    def __init__(self) -> None:
        super().__init__("transport lease is invalid")


@dataclass(frozen=True, slots=True)
class RequestFrame:
    """One newline-delimited request payload, excluding the newline byte."""

    payload: bytes


@dataclass(frozen=True, slots=True)
class FrameFailure:
    """A fatal framing failure.  The caller writes ``response`` then closes."""

    response: dict[str, object]
    close: bool = True


class RequestLineFramer:
    """Incrementally split bounded newline frames with constant overflow state.

    The caller should feed chunks no larger than :data:`READ_CHUNK_BYTES` in
    production.  The primitive also remains bounded if a larger bytes object
    is supplied: at most ``MAX_REQUEST_FRAME_BYTES`` is retained internally.
    Once a line exceeds the ceiling it is discarded until its newline (or
    EOF), one fixed parse error is produced, and the framer closes.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._draining = False
        self._closed = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def closed(self) -> bool:
        return self._closed

    def feed(self, chunk: bytes) -> tuple[RequestFrame | FrameFailure, ...]:
        if self._closed:
            raise TransportLeaseError
        if type(chunk) is not bytes:
            raise TypeError("request chunks must be bytes")
        events: list[RequestFrame | FrameFailure] = []
        offset = 0
        while offset < len(chunk):
            if self._draining:
                newline = chunk.find(b"\n", offset)
                if newline < 0:
                    return tuple(events)
                self._draining = False
                self._closed = True
                events.append(FrameFailure(rpc_error_response(PARSE_ERROR)))
                return tuple(events)

            newline = chunk.find(b"\n", offset)
            if newline >= 0:
                fragment = chunk[offset:newline]
                if len(self._buffer) + len(fragment) > MAX_REQUEST_FRAME_BYTES:
                    self._buffer.clear()
                    self._closed = True
                    events.append(FrameFailure(rpc_error_response(PARSE_ERROR)))
                    return tuple(events)
                payload = bytes(self._buffer) + fragment
                self._buffer.clear()
                events.append(RequestFrame(payload))
                offset = newline + 1
                continue

            available = MAX_REQUEST_FRAME_BYTES - len(self._buffer)
            remainder = len(chunk) - offset
            if remainder <= available:
                self._buffer.extend(chunk[offset:])
                return tuple(events)
            self._buffer.clear()
            self._draining = True
            return tuple(events)
        return tuple(events)

    def finish(self) -> tuple[FrameFailure, ...]:
        if self._closed:
            return ()
        failed = self._draining or bool(self._buffer)
        self._buffer.clear()
        self._draining = False
        self._closed = True
        if failed:
            return (FrameFailure(rpc_error_response(PARSE_ERROR)),)
        return ()


class _LexicalFailure(ValueError):
    pass


_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_HEX = frozenset("0123456789abcdefABCDEF")
_JSON_WHITESPACE = frozenset(" \t\r\n")


class _JsonLexicalScanner:
    """Validate the bounded JSON grammar before constructing its object tree."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._at = 0
        self._nodes = 0

    def scan(self) -> None:
        self._skip_whitespace()
        self._parse_value(depth=0)
        self._skip_whitespace()
        if self._at != len(self._text):
            raise _LexicalFailure

    def _skip_whitespace(self) -> None:
        while self._at < len(self._text) and self._text[self._at] in _JSON_WHITESPACE:
            self._at += 1

    def _count_node(self) -> None:
        self._nodes += 1
        if self._nodes > MAX_JSON_NODES:
            raise _LexicalFailure

    def _parse_value(self, *, depth: int) -> None:
        if self._at >= len(self._text):
            raise _LexicalFailure
        self._count_node()
        token = self._text[self._at]
        if token == "{":
            self._parse_object(depth=depth + 1)
        elif token == "[":
            self._parse_array(depth=depth + 1)
        elif token == '"':
            self._parse_string(is_key=False)
        elif token == "t":
            self._parse_literal("true")
        elif token == "f":
            self._parse_literal("false")
        elif token == "n":
            self._parse_literal("null")
        elif token == "-" or "0" <= token <= "9":
            self._parse_number()
        else:
            raise _LexicalFailure

    def _check_depth(self, depth: int) -> None:
        if depth > MAX_JSON_DEPTH:
            raise _LexicalFailure

    def _parse_object(self, *, depth: int) -> None:
        self._check_depth(depth)
        self._at += 1
        self._skip_whitespace()
        if self._consume("}"):
            return
        keys: set[str] = set()
        members = 0
        while True:
            if self._at >= len(self._text) or self._text[self._at] != '"':
                raise _LexicalFailure
            self._count_node()
            key = self._parse_string(is_key=True)
            if key in keys:
                raise _LexicalFailure
            keys.add(key)
            members += 1
            if members > MAX_JSON_CONTAINER_ITEMS:
                raise _LexicalFailure
            self._skip_whitespace()
            if not self._consume(":"):
                raise _LexicalFailure
            self._skip_whitespace()
            self._parse_value(depth=depth)
            self._skip_whitespace()
            if self._consume("}"):
                return
            if not self._consume(","):
                raise _LexicalFailure
            self._skip_whitespace()

    def _parse_array(self, *, depth: int) -> None:
        self._check_depth(depth)
        self._at += 1
        self._skip_whitespace()
        if self._consume("]"):
            return
        items = 0
        while True:
            self._parse_value(depth=depth)
            items += 1
            if items > MAX_JSON_CONTAINER_ITEMS:
                raise _LexicalFailure
            self._skip_whitespace()
            if self._consume("]"):
                return
            if not self._consume(","):
                raise _LexicalFailure
            self._skip_whitespace()

    def _parse_string(self, *, is_key: bool) -> str:
        start = self._at
        self._at += 1
        while self._at < len(self._text):
            token = self._text[self._at]
            if token == '"':
                self._at += 1
                raw = self._text[start : self._at]
                try:
                    decoded = json.loads(raw)
                    size = len(decoded.encode("utf-8"))
                except (UnicodeEncodeError, ValueError, TypeError) as exc:
                    raise _LexicalFailure from exc
                limit = MAX_JSON_KEY_BYTES if is_key else MAX_JSON_STRING_BYTES
                if size > limit:
                    raise _LexicalFailure
                return decoded
            if ord(token) < 0x20:
                raise _LexicalFailure
            if token != "\\":
                self._at += 1
                continue
            self._at += 1
            if self._at >= len(self._text):
                raise _LexicalFailure
            escape = self._text[self._at]
            if escape == "u":
                end = self._at + 5
                if end > len(self._text) or any(
                    char not in _HEX for char in self._text[self._at + 1 : end]
                ):
                    raise _LexicalFailure
                self._at = end
            elif escape in {'"', "\\", "/", "b", "f", "n", "r", "t"}:
                self._at += 1
            else:
                raise _LexicalFailure
        raise _LexicalFailure

    def _parse_literal(self, literal: str) -> None:
        if not self._text.startswith(literal, self._at):
            raise _LexicalFailure
        self._at += len(literal)

    def _parse_number(self) -> None:
        match = _NUMBER.match(self._text, self._at)
        if match is None:
            raise _LexicalFailure
        token = match.group(0)
        self._at = match.end()
        if "." in token or "e" in token or "E" in token:
            if len(token) > MAX_JSON_FLOAT_TOKEN_BYTES:
                raise _LexicalFailure
            try:
                value = float(token)
            except ValueError as exc:
                raise _LexicalFailure from exc
            if not math.isfinite(value):
                raise _LexicalFailure
            return
        digits = token[1:] if token.startswith("-") else token
        if len(digits) > MAX_JSON_INTEGER_DIGITS:
            raise _LexicalFailure
        try:
            value = int(token)
        except ValueError as exc:
            raise _LexicalFailure from exc
        if abs(value) > MAX_SAFE_JSON_INTEGER:
            raise _LexicalFailure

    def _consume(self, token: str) -> bool:
        if self._at < len(self._text) and self._text[self._at] == token:
            self._at += 1
            return True
        return False


class _DuplicateKey(ValueError):
    pass


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _finite_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError
    return value


def decode_request_frame(payload: bytes) -> Any:
    """Decode one bounded JSON value or raise one fixed fatal parse error."""

    try:
        if type(payload) is not bytes or len(payload) > MAX_REQUEST_FRAME_BYTES:
            raise _LexicalFailure
        text = payload.decode("utf-8", errors="strict")
        _JsonLexicalScanner(text).scan()
        decoded = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda _token: (_ for _ in ()).throw(ValueError()),
            parse_float=_finite_float,
            parse_int=int,
        )
    except (UnicodeError, ValueError, TypeError, RecursionError):
        # Raw decoder exceptions may retain byte excerpts or caller strings in
        # their repr/traceback.  Leave the exception scope before constructing
        # the public failure so it has neither a cause nor a retained context.
        pass
    else:
        return decoded
    raise TransportProtocolError(PARSE_ERROR, close=True)


_REQUEST_METHODS = frozenset(
    {
        "initialize",
        "ping",
        "tools/list",
        "tools/call",
        "resources/list",
        "resources/templates/list",
        "resources/read",
    }
)
_NOTIFICATION_METHODS = frozenset({"notifications/initialized", "notifications/cancelled"})


@dataclass(frozen=True, slots=True)
class ClientMessageDescriptor:
    """A structurally safe member of the supported client-message union."""

    method: str
    request_id: int | str | None
    params: Mapping[str, Any]
    is_notification: bool
    is_cancellation: bool
    cancellation_target: int | str | None
    is_resource_read: bool


def _plain_object(value: object) -> dict[str, Any] | None:
    if type(value) is not dict:
        return None
    if any(type(key) is not str for key in value):
        return None
    return cast("dict[str, Any]", value)


def _bounded_string(value: object, *, maximum: int = MAX_JSON_STRING_BYTES) -> bool:
    if type(value) is not str:
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeEncodeError:
        return False


def _exact_keys(
    value: dict[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> bool:
    keys = frozenset(value)
    return required <= keys and keys <= required | optional


def _validate_client_info(value: object) -> bool:
    info = _plain_object(value)
    if info is None or not _exact_keys(
        info,
        required=frozenset({"name", "version"}),
        optional=frozenset({"title", "websiteUrl", "icons"}),
    ):
        return False
    if not _bounded_string(info["name"]) or not _bounded_string(info["version"]):
        return False
    for key in ("title", "websiteUrl"):
        if key in info and info[key] is not None and not _bounded_string(info[key]):
            return False
    if "icons" not in info or info["icons"] is None:
        return True
    icons = info["icons"]
    if type(icons) is not list or len(icons) > MAX_JSON_CONTAINER_ITEMS:
        return False
    for raw_icon in icons:
        icon = _plain_object(raw_icon)
        if icon is None or not _exact_keys(
            icon,
            required=frozenset({"src"}),
            optional=frozenset({"mimeType", "sizes"}),
        ):
            return False
        if not _bounded_string(icon["src"]):
            return False
        if "mimeType" in icon and icon["mimeType"] is not None:
            if not _bounded_string(icon["mimeType"]):
                return False
        if "sizes" in icon and icon["sizes"] is not None:
            sizes = icon["sizes"]
            if type(sizes) is not list or any(not _bounded_string(size) for size in sizes):
                return False
    return True


def _validate_capabilities(value: object) -> bool:
    capabilities = _plain_object(value)
    if capabilities is None:
        return False
    allowed = frozenset({"experimental", "sampling", "elicitation", "roots", "tasks"})
    if not frozenset(capabilities) <= allowed:
        return False
    # ``experimental`` is the one intentionally opaque extension point in
    # ClientCapabilities.  MCP 1.27.2 types it as dict[str, dict[str, Any]];
    # the lexical pass has already bounded every nested value.
    if "experimental" in capabilities and capabilities["experimental"] is not None:
        experimental = _plain_object(capabilities["experimental"])
        if experimental is None or any(
            _plain_object(item) is None for item in experimental.values()
        ):
            return False
    if "roots" in capabilities and capabilities["roots"] is not None:
        roots = _plain_object(capabilities["roots"])
        if roots is None or not frozenset(roots) <= frozenset({"listChanged"}):
            return False
        if "listChanged" in roots and roots["listChanged"] is not None:
            if type(roots["listChanged"]) is not bool:
                return False
    for capability, known_fields in (
        ("sampling", frozenset({"context", "tools"})),
        ("elicitation", frozenset({"form", "url"})),
    ):
        if capability not in capabilities or capabilities[capability] is None:
            continue
        detail = _plain_object(capabilities[capability])
        if detail is None or not frozenset(detail) <= known_fields:
            return False
        for field in known_fields & frozenset(detail):
            if detail[field] is not None:
                marker = _plain_object(detail[field])
                if marker is None or marker:
                    return False
    if "tasks" in capabilities and capabilities["tasks"] is not None:
        tasks = _plain_object(capabilities["tasks"])
        task_fields = frozenset({"list", "cancel", "requests"})
        if tasks is None or not frozenset(tasks) <= task_fields:
            return False
        for field in frozenset({"list", "cancel"}) & frozenset(tasks):
            if tasks[field] is not None:
                marker = _plain_object(tasks[field])
                if marker is None or marker:
                    return False
        if "requests" in tasks and tasks["requests"] is not None:
            requests = _plain_object(tasks["requests"])
            request_fields = frozenset({"sampling", "elicitation"})
            if requests is None or not frozenset(requests) <= request_fields:
                return False
            nested_fields = {"sampling": "createMessage", "elicitation": "create"}
            for field in request_fields & frozenset(requests):
                if requests[field] is None:
                    continue
                detail = _plain_object(requests[field])
                allowed_detail = frozenset({nested_fields[field]})
                if detail is None or not frozenset(detail) <= allowed_detail:
                    return False
                marker = detail.get(nested_fields[field])
                if marker is not None:
                    marker_object = _plain_object(marker)
                    if marker_object is None or marker_object:
                        return False
    return all(item is None or _plain_object(item) is not None for item in capabilities.values())


def _invalid(
    *,
    request_id: object | None = None,
    tool_request: bool = False,
    resource_params: bool = False,
) -> None:
    if tool_request:
        error = TOOL_REQUEST_INVALID
    elif resource_params:
        error = INVALID_REQUEST_PARAMS
    else:
        error = INVALID_REQUEST
    raise TransportProtocolError(error, request_id=request_id)


def _validate_params(method: str, params: dict[str, Any], request_id: int | str) -> None:
    if method == "initialize":
        if not _exact_keys(
            params,
            required=frozenset({"protocolVersion", "capabilities", "clientInfo"}),
        ):
            _invalid(request_id=request_id)
        if not _bounded_string(params["protocolVersion"], maximum=64):
            _invalid(request_id=request_id)
        if not _validate_capabilities(params["capabilities"]):
            _invalid(request_id=request_id)
        if not _validate_client_info(params["clientInfo"]):
            _invalid(request_id=request_id)
        return
    if method in {"ping"}:
        if params:
            _invalid(request_id=request_id)
        return
    if method in {"tools/list", "resources/list", "resources/templates/list"}:
        if not _exact_keys(params, required=frozenset(), optional=frozenset({"cursor"})):
            _invalid(request_id=request_id)
        if "cursor" in params and params["cursor"] is not None:
            if not _bounded_string(params["cursor"]):
                _invalid(request_id=request_id)
        return
    if method == "tools/call":
        if not _exact_keys(
            params,
            required=frozenset({"name"}),
            optional=frozenset({"arguments"}),
        ):
            _invalid(request_id=request_id, tool_request=True)
        if not _bounded_string(params["name"], maximum=MAX_JSON_KEY_BYTES):
            _invalid(request_id=request_id, tool_request=True)
        if not params["name"]:
            _invalid(request_id=request_id, tool_request=True)
        if "arguments" in params and params["arguments"] is not None:
            if _plain_object(params["arguments"]) is None:
                _invalid(request_id=request_id, tool_request=True)
        return
    if method == "resources/read":
        if not _exact_keys(params, required=frozenset({"uri"})):
            _invalid(request_id=request_id, resource_params=True)
        uri = params["uri"]
        if not _bounded_string(uri, maximum=ARTIFACT_RESOURCE_URI_BYTES):
            _invalid(request_id=request_id, resource_params=True)
        if _TYPED_RESOURCE_URI.fullmatch(uri) is None:
            _invalid(request_id=request_id, resource_params=True)
        return
    _invalid(request_id=request_id)


def prevalidate_client_message(value: object) -> ClientMessageDescriptor:
    """Validate a closed, non-coercing client request/notification shape.

    Tool-specific ``arguments`` remain the responsibility of the public
    surface's exact schema validator.  This boundary proves that the SDK will
    only see a supported method with a safe top-level container and URI.
    """

    message = _plain_object(value)
    if message is None:
        _invalid()
    has_id = "id" in message
    request_id: int | str | None = None
    if has_id:
        candidate = message["id"]
        if not _is_request_id(candidate):
            _invalid()
        request_id = candidate
    allowed_outer = frozenset({"jsonrpc", "method", "params"}) | (
        frozenset({"id"}) if has_id else frozenset()
    )
    if frozenset(message) != allowed_outer and not (
        "params" not in message and frozenset(message) == allowed_outer - {"params"}
    ):
        _invalid(request_id=request_id)
    if message.get("jsonrpc") != "2.0" or type(message.get("method")) is not str:
        _invalid(request_id=request_id)
    method = message["method"]
    if has_id:
        if method not in _REQUEST_METHODS:
            _invalid(request_id=request_id)
    elif method not in _NOTIFICATION_METHODS:
        _invalid()
    params = _plain_object(message.get("params", {}))
    if params is None:
        _invalid(
            request_id=request_id,
            tool_request=method == "tools/call",
            resource_params=method == "resources/read",
        )

    cancellation_target: int | str | None = None
    if has_id:
        assert request_id is not None
        _validate_params(method, params, request_id)
    elif method == "notifications/initialized":
        if params:
            _invalid()
    else:
        if not _exact_keys(
            params,
            required=frozenset({"requestId"}),
            optional=frozenset({"reason"}),
        ):
            _invalid()
        cancellation_target = params["requestId"]
        if not _is_request_id(cancellation_target):
            _invalid()
        if "reason" in params and params["reason"] is not None:
            if not _bounded_string(params["reason"]):
                _invalid()

    return ClientMessageDescriptor(
        method=method,
        request_id=request_id,
        params=MappingProxyType(dict(params)),
        is_notification=not has_id,
        is_cancellation=method == "notifications/cancelled",
        cancellation_target=cancellation_target,
        is_resource_read=method == "resources/read",
    )


def decode_and_prevalidate(payload: bytes) -> ClientMessageDescriptor:
    """Convenience composition for one already-framed client message."""

    return prevalidate_client_message(decode_request_frame(payload))


class ActiveRequestIds:
    """Thread-safe, type-stable reservation set for active non-null ids."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[tuple[str, int | str]] = set()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._active)

    def reserve(self, request_id: int | str) -> bool:
        key = _request_id_key(request_id)
        with self._lock:
            if key in self._active:
                return False
            self._active.add(key)
            return True

    def release(self, request_id: int | str) -> None:
        key = _request_id_key(request_id)
        with self._lock:
            self._active.discard(key)

    def contains(self, request_id: int | str) -> bool:
        key = _request_id_key(request_id)
        with self._lock:
            return key in self._active


@dataclass(frozen=True, slots=True)
class WorkLease:
    owner: int
    token: int
    request_id: int | str | None


@dataclass(frozen=True, slots=True)
class ControlLease:
    owner: int
    token: int


@dataclass(frozen=True, slots=True)
class WorkerLease:
    owner: int
    token: int
    work_token: int


@dataclass(frozen=True, slots=True)
class ResourceLease:
    owner: int
    token: int
    work_token: int


@dataclass(frozen=True, slots=True)
class WorkAdmission:
    lease: WorkLease | None = None
    full: bool = False
    response: dict[str, object] | None = None


class ControlDecisionKind(StrEnum):
    BUSY = "busy"
    DROPPED = "dropped"
    CANCEL_REQUESTED = "cancel_requested"


@dataclass(frozen=True, slots=True)
class ControlDecision:
    kind: ControlDecisionKind
    response: dict[str, object] | None = None


@dataclass(slots=True)
class _WorkState:
    lease: WorkLease
    id_key: tuple[str, int | str] | None
    cancel_requested: bool = False
    cleanup_complete: bool = False
    cancellation_response: dict[str, object] | None = None


class AdmissionController:
    """Thread-safe finite admission state for a future owned dispatch loop.

    A work slot, its id reservation, and any worker/resource sublease remain
    occupied until :meth:`complete_work` is called after real cleanup.  The
    separate control lease lets a saturated reader mark cancellation or emit
    one busy response without admitting a ninth work item.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._control_available = threading.Condition(self._lock)
        self._owner = id(self)
        self._next_token = 1
        self._work: dict[int, _WorkState] = {}
        self._id_to_work: dict[tuple[str, int | str], int] = {}
        self._control_token: int | None = None
        self._workers: dict[int, WorkerLease] = {}
        self._work_to_worker: dict[int, int] = {}
        self._resource: ResourceLease | None = None

    @property
    def active_work_count(self) -> int:
        with self._lock:
            return len(self._work)

    def is_cancel_requested(self, request_id: int | str) -> bool:
        key = _request_id_key(request_id)
        with self._lock:
            token = self._id_to_work.get(key)
            if token is None:
                return False
            return self._work[token].cancel_requested

    def _new_token(self) -> int:
        token = self._next_token
        self._next_token += 1
        return token

    def try_acquire_work(self, descriptor: ClientMessageDescriptor) -> WorkAdmission:
        if not isinstance(descriptor, ClientMessageDescriptor):
            raise TransportLeaseError
        with self._lock:
            id_key = None
            if descriptor.request_id is not None:
                id_key = _request_id_key(descriptor.request_id)
                if id_key in self._id_to_work:
                    return WorkAdmission(
                        response=rpc_error_response(
                            INVALID_REQUEST,
                            request_id=descriptor.request_id,
                        )
                    )
            if len(self._work) >= MAX_IN_FLIGHT:
                return WorkAdmission(full=True)
            token = self._new_token()
            lease = WorkLease(self._owner, token, descriptor.request_id)
            self._work[token] = _WorkState(lease=lease, id_key=id_key)
            if id_key is not None:
                self._id_to_work[id_key] = token
            return WorkAdmission(lease=lease)

    def try_acquire_control(self) -> ControlLease | None:
        with self._lock:
            if self._control_token is not None:
                return None
            token = self._new_token()
            self._control_token = token
            return ControlLease(self._owner, token)

    def acquire_control(self) -> ControlLease:
        """Wait for the one bounded pending-control slot.

        The owned reader backpressures here instead of silently dropping a
        required rejection.  The dedicated control writer releases this slot
        when it dequeues the response, so one additional response can remain
        pending while the current stdout write is blocked.
        """

        with self._control_available:
            while self._control_token is not None:
                self._control_available.wait()
            token = self._new_token()
            self._control_token = token
            return ControlLease(self._owner, token)

    def release_control(self, lease: ControlLease) -> None:
        with self._control_available:
            self._require_control(lease)
            self._control_token = None
            self._control_available.notify()

    def route_control(
        self,
        descriptor: ClientMessageDescriptor,
        lease: ControlLease,
    ) -> ControlDecision:
        if not isinstance(descriptor, ClientMessageDescriptor):
            raise TransportLeaseError
        with self._lock:
            self._require_control(lease)
            if descriptor.is_cancellation:
                target = descriptor.cancellation_target
                if target is not None:
                    token = self._id_to_work.get(_request_id_key(target))
                    if token is not None:
                        state = self._work[token]
                        if state.cleanup_complete:
                            return ControlDecision(ControlDecisionKind.DROPPED)
                        state.cancel_requested = True
                return ControlDecision(ControlDecisionKind.CANCEL_REQUESTED)
            if descriptor.request_id is not None:
                return ControlDecision(
                    ControlDecisionKind.BUSY,
                    rpc_error_response(SERVER_BUSY, request_id=descriptor.request_id),
                )
            return ControlDecision(ControlDecisionKind.DROPPED)

    def request_cancellation(
        self,
        descriptor: ClientMessageDescriptor,
    ) -> ControlDecision:
        """Mark cancellation without retaining a control-frame or response slot.

        The owned reader uses this path even while the one control responder is
        waiting behind a large stdout write.  It performs the same atomic state
        transition as :meth:`route_control` but allocates and reflects nothing.
        """

        if not isinstance(descriptor, ClientMessageDescriptor) or not descriptor.is_cancellation:
            raise TransportLeaseError
        with self._lock:
            target = descriptor.cancellation_target
            if target is not None:
                token = self._id_to_work.get(_request_id_key(target))
                if token is not None:
                    state = self._work[token]
                    if state.cleanup_complete:
                        return ControlDecision(ControlDecisionKind.DROPPED)
                    state.cancel_requested = True
            return ControlDecision(ControlDecisionKind.CANCEL_REQUESTED)

    def try_acquire_worker(self, work: WorkLease) -> WorkerLease | None:
        with self._lock:
            state = self._require_work(work)
            if state.cleanup_complete:
                raise TransportLeaseError
            if work.token in self._work_to_worker or len(self._workers) >= MAX_WORKERS:
                return None
            token = self._new_token()
            lease = WorkerLease(self._owner, token, work.token)
            self._workers[token] = lease
            self._work_to_worker[work.token] = token
            return lease

    def release_worker(self, lease: WorkerLease) -> None:
        with self._lock:
            if lease.owner != self._owner or self._workers.get(lease.token) != lease:
                raise TransportLeaseError
            self._workers.pop(lease.token)
            self._work_to_worker.pop(lease.work_token, None)

    def try_acquire_resource(self, work: WorkLease) -> ResourceLease | None:
        with self._lock:
            state = self._require_work(work)
            if state.cleanup_complete:
                raise TransportLeaseError
            if self._resource is not None:
                return None
            token = self._new_token()
            self._resource = ResourceLease(self._owner, token, work.token)
            return self._resource

    def release_resource(self, lease: ResourceLease) -> None:
        with self._lock:
            if lease.owner != self._owner or self._resource != lease:
                raise TransportLeaseError
            self._resource = None

    def complete_work(
        self,
        lease: WorkLease,
        *,
        allow_cancellation: bool = True,
    ) -> dict[str, object] | None:
        """Freeze the post-cleanup response decision without releasing capacity.

        The caller must finish writing the selected response, or deliberately
        suppress/drop it after disconnect, and then call
        :meth:`acknowledge_work`.  Until that acknowledgement the work slot and
        JSON-RPC id remain reserved.  A resource sublease may also remain held
        across this phase so the caller can retain it through serialization and
        the completed write.
        """

        with self._lock:
            state = self._require_work(lease)
            if state.cleanup_complete:
                raise TransportLeaseError
            if lease.token in self._work_to_worker:
                raise TransportLeaseError
            state.cleanup_complete = True
            if allow_cancellation and state.cancel_requested and lease.request_id is not None:
                state.cancellation_response = rpc_error_response(
                    REQUEST_CANCELLED,
                    request_id=lease.request_id,
                )
                return state.cancellation_response
            return None

    def acknowledge_work(self, lease: WorkLease) -> None:
        """Release one work/id reservation after response write or drop."""

        with self._lock:
            state = self._require_work(lease)
            if not state.cleanup_complete or lease.token in self._work_to_worker:
                raise TransportLeaseError
            if self._resource is not None and self._resource.work_token == lease.token:
                raise TransportLeaseError
            self._work.pop(lease.token)
            if state.id_key is not None:
                self._id_to_work.pop(state.id_key, None)

    def _require_control(self, lease: ControlLease) -> None:
        if (
            not isinstance(lease, ControlLease)
            or lease.owner != self._owner
            or lease.token != self._control_token
        ):
            raise TransportLeaseError

    def _require_work(self, lease: WorkLease) -> _WorkState:
        if not isinstance(lease, WorkLease) or lease.owner != self._owner:
            raise TransportLeaseError
        state = self._work.get(lease.token)
        if state is None or state.lease != lease:
            raise TransportLeaseError
        return state


class ProcessState(StrEnum):
    """Finite child-process lifecycle."""

    RUNNING = "RUNNING"
    SWAP_PENDING = "SWAP_PENDING"
    DRAINING = "DRAINING"
    UNINSTALL_READY = "UNINSTALL_READY"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    CLOSED = "CLOSED"


class ProcessLifecycle:
    """Atomic admission/swap/uninstall latch shared with the server adapter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = ProcessState.RUNNING
        self._active = False
        self._uninstall_owner: int | None = None
        self._local = threading.local()
        self._listeners: list[Callable[[], None]] = []

    @property
    def state(self) -> ProcessState:
        with self._lock:
            return self._state

    @property
    def accepts_work(self) -> bool:
        with self._lock:
            return self._active and self._state is ProcessState.RUNNING

    @property
    def application_may_enter(self) -> bool:
        with self._lock:
            return not self._active or self._state is ProcessState.RUNNING

    def activate(self) -> None:
        with self._lock:
            if self._active or self._state is not ProcessState.RUNNING:
                raise RuntimeError("process lifecycle is already active")
            self._active = True

    def add_listener(self, listener: Callable[[], None]) -> None:
        if not callable(listener):
            raise TypeError("lifecycle listener must be callable")
        with self._lock:
            self._listeners.append(listener)

    @staticmethod
    def _notify(listeners: tuple[Callable[[], None], ...]) -> None:
        for listener in listeners:
            try:
                listener()
            except BaseException:
                continue

    def admit(self, callback: Callable[[], WorkAdmission]) -> WorkAdmission | None:
        with self._lock:
            if not self._active or self._state is not ProcessState.RUNNING:
                return None
            return callback()

    @contextmanager
    def bind_work(self, lease: WorkLease) -> Iterator[None]:
        if not isinstance(lease, WorkLease):
            raise TransportLeaseError
        previous = getattr(self._local, "work_token", None)
        self._local.work_token = lease.token
        try:
            yield
        finally:
            if previous is None:
                try:
                    del self._local.work_token
                except AttributeError:
                    pass
            else:
                self._local.work_token = previous

    def request_swap(self) -> bool:
        listeners: tuple[Callable[[], None], ...] = ()
        with self._lock:
            if not self._active:
                return False
            if self._state is ProcessState.SWAP_PENDING:
                return True
            if self._state is not ProcessState.RUNNING:
                return False
            self._state = ProcessState.SWAP_PENDING
            listeners = tuple(self._listeners)
        self._notify(listeners)
        return True

    def request_uninstall_exit(self) -> bool:
        work_token = getattr(self._local, "work_token", None)
        if type(work_token) is not int:
            return False
        listeners: tuple[Callable[[], None], ...] = ()
        with self._lock:
            if not self._active:
                return False
            if self._state is ProcessState.DRAINING:
                return self._uninstall_owner == work_token
            if self._state not in {ProcessState.RUNNING, ProcessState.SWAP_PENDING}:
                return False
            self._state = ProcessState.DRAINING
            self._uninstall_owner = work_token
            listeners = tuple(self._listeners)
        self._notify(listeners)
        return True

    def owns_uninstall(self, lease: WorkLease) -> bool:
        with self._lock:
            return (
                self._active
                and self._state is ProcessState.DRAINING
                and self._uninstall_owner == lease.token
            )

    def uninstall_ready(self) -> bool:
        listeners: tuple[Callable[[], None], ...] = ()
        with self._lock:
            if self._state is not ProcessState.DRAINING:
                return False
            self._state = ProcessState.UNINSTALL_READY
            listeners = tuple(self._listeners)
        self._notify(listeners)
        return True

    def recovery_required(self) -> bool:
        work_token = getattr(self._local, "work_token", None)
        listeners: tuple[Callable[[], None], ...] = ()
        with self._lock:
            if self._state is ProcessState.RUNNING and type(work_token) is int:
                self._uninstall_owner = work_token
            elif self._state not in {
                ProcessState.DRAINING,
                ProcessState.UNINSTALL_READY,
                ProcessState.RECOVERY_REQUIRED,
            }:
                return self._state is ProcessState.RECOVERY_REQUIRED
            if (
                self._state is ProcessState.DRAINING
                and work_token is not None
                and work_token != self._uninstall_owner
            ):
                return False
            if self._state is ProcessState.RECOVERY_REQUIRED:
                return True
            self._state = ProcessState.RECOVERY_REQUIRED
            listeners = tuple(self._listeners)
        self._notify(listeners)
        return True

    def close(self) -> None:
        with self._lock:
            self._active = False
            self._state = ProcessState.CLOSED


@dataclass(frozen=True, slots=True)
class _QueuedWork:
    descriptor: ClientMessageDescriptor
    lease: WorkLease


@dataclass(frozen=True, slots=True)
class _ControlWrite:
    response: Mapping[str, object]
    lease: ControlLease
    done: threading.Event


class _HandshakeState(StrEnum):
    NEW = "NEW"
    INITIALIZING = "INITIALIZING"
    RESPONDED = "RESPONDED"
    READY = "READY"
    FAILED = "FAILED"


class OwnedStdioRunner:
    """Own the bounded child-side MCP framing, admission and worker loop.

    ``dispatch`` receives only a prevalidated descriptor and returns one
    already-formed JSON-RPC response mapping (or ``None`` for a notification).
    It is invoked exclusively by the four pre-created worker threads.  The
    response frame and any resource sublease remain owned until the completed
    write (or an explicit disconnect drop), after which the work/id reservation
    is acknowledged.
    """

    def __init__(
        self,
        *,
        dispatch: Callable[[ClientMessageDescriptor], Mapping[str, object] | None],
        lifecycle: ProcessLifecycle,
        close_application: Callable[[], bool],
        uninstall_recovery_response: Callable[[int | str | None], Mapping[str, object]],
        exit_process: Callable[[int], None],
        failure_response: Callable[[ClientMessageDescriptor], Mapping[str, object] | None]
        | None = None,
        uninstall_drain_seconds: float = UNINSTALL_DRAIN_SECONDS,
    ) -> None:
        if not callable(dispatch):
            raise TypeError("transport dispatch must be callable")
        if not isinstance(lifecycle, ProcessLifecycle):
            raise TypeError("process lifecycle is invalid")
        if not callable(close_application):
            raise TypeError("application closer must be callable")
        if not callable(uninstall_recovery_response):
            raise TypeError("uninstall recovery response must be callable")
        if not callable(exit_process):
            raise TypeError("process exit hook must be callable")
        if failure_response is not None and not callable(failure_response):
            raise TypeError("failure response callback is invalid")
        if (
            type(uninstall_drain_seconds) not in {int, float}
            or type(uninstall_drain_seconds) is bool
            or not math.isfinite(float(uninstall_drain_seconds))
            or float(uninstall_drain_seconds) <= 0
        ):
            raise ValueError("uninstall drain timeout is invalid")
        self._dispatch = dispatch
        self._lifecycle = lifecycle
        self._close_application = close_application
        self._uninstall_recovery_response = uninstall_recovery_response
        self._exit_process = exit_process
        self._failure_response = failure_response
        self._uninstall_drain_seconds = float(uninstall_drain_seconds)
        self._admission = AdmissionController()
        self._queue: queue.Queue[_QueuedWork | None] = queue.Queue(maxsize=MAX_IN_FLIGHT)
        self._control_queue: queue.Queue[_ControlWrite | None] = queue.Queue(maxsize=1)
        self._resource_gate = threading.Semaphore(MAX_RESOURCE_READS)
        self._write_lock = threading.Lock()
        self._state_condition = threading.Condition()
        self._handshake_lock = threading.Lock()
        self._handshake = _HandshakeState.NEW
        self._started = False
        self._stopping = False
        self._disconnected = False
        self._exit_called = False
        self._write_frame: Callable[[bytes], None] | None = None
        self._workers = tuple(
            threading.Thread(
                target=self._worker_loop,
                name=f"vibecad-application-{index + 1}",
                daemon=True,
            )
            for index in range(MAX_WORKERS)
        )
        self._control_worker = threading.Thread(
            target=self._control_loop,
            name="vibecad-control",
            daemon=True,
        )
        self._lifecycle.add_listener(self._lifecycle_changed)

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    @property
    def worker_names(self) -> tuple[str, ...]:
        return tuple(worker.name for worker in self._workers)

    @property
    def active_work_count(self) -> int:
        return self._admission.active_work_count

    @property
    def lifecycle(self) -> ProcessLifecycle:
        return self._lifecycle

    @property
    def handshake_state(self) -> str:
        with self._handshake_lock:
            return self._handshake.value

    def is_cancel_requested(self, request_id: int | str) -> bool:
        return self._admission.is_cancel_requested(request_id)

    def request_swap(self) -> bool:
        return self._lifecycle.request_swap()

    def request_uninstall_exit(self) -> bool:
        return self._lifecycle.request_uninstall_exit()

    def request_uninstall_recovery(self) -> bool:
        return self._lifecycle.recovery_required()

    def _lifecycle_changed(self) -> None:
        with self._state_condition:
            self._state_condition.notify_all()
        self._maybe_exit_after_flush()

    @staticmethod
    def _encoded_response(response: Mapping[str, object]) -> bytes:
        try:
            payload = json.dumps(
                dict(response),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if len(payload) > MAX_RESPONSE_FRAME_BYTES:
                raise ValueError
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise TransportProtocolError(GENERIC_INTERNAL_ERROR) from None
        return payload + b"\n"

    def _fixed_failure(
        self,
        descriptor: ClientMessageDescriptor,
    ) -> Mapping[str, object] | None:
        if self._failure_response is not None:
            try:
                response = self._failure_response(descriptor)
                if response is None or isinstance(response, Mapping):
                    return response
            except BaseException:
                pass
        if descriptor.request_id is None:
            return None
        return rpc_error_response(GENERIC_INTERNAL_ERROR, request_id=descriptor.request_id)

    def _prepared_frame(
        self,
        response: Mapping[str, object],
        descriptor: ClientMessageDescriptor | None = None,
    ) -> bytes | None:
        try:
            return self._encoded_response(response)
        except TransportProtocolError:
            fallback = (
                self._fixed_failure(descriptor)
                if descriptor is not None
                else rpc_error_response(GENERIC_INTERNAL_ERROR)
            )
            if fallback is None:
                return None
            try:
                return self._encoded_response(fallback)
            except TransportProtocolError:
                return None

    def _write_response(
        self,
        response: Mapping[str, object],
        descriptor: ClientMessageDescriptor | None = None,
    ) -> bool:
        frame = self._prepared_frame(response, descriptor)
        if frame is None:
            return False
        with self._write_lock:
            if self._disconnected or self._write_frame is None:
                return False
            try:
                self._write_frame(frame)
            except BaseException:
                self._disconnected = True
                return False
        return True

    def _enqueue_control_response(
        self,
        response: Mapping[str, object],
        lease: ControlLease,
        *,
        wait: bool = False,
    ) -> bool:
        done = threading.Event()
        self._control_queue.put(_ControlWrite(response, lease, done))
        if wait:
            done.wait(self._uninstall_drain_seconds)
        return True

    def _emit_control_response(
        self,
        response: Mapping[str, object],
        *,
        wait: bool = False,
    ) -> bool:
        lease = self._admission.acquire_control()
        return self._enqueue_control_response(response, lease, wait=wait)

    def _route_control(self, descriptor: ClientMessageDescriptor) -> None:
        if descriptor.is_cancellation:
            self._admission.request_cancellation(descriptor)
            return
        lease = self._admission.acquire_control()
        decision = self._admission.route_control(descriptor, lease)
        if decision.response is None:
            self._admission.release_control(lease)
            return
        self._enqueue_control_response(decision.response, lease)

    def _handshake_allows(self, descriptor: ClientMessageDescriptor) -> bool:
        if descriptor.method == "initialize":
            with self._handshake_lock:
                if self._handshake is not _HandshakeState.NEW:
                    self._emit_control_response(
                        rpc_error_response(INVALID_REQUEST, request_id=descriptor.request_id)
                    )
                    return False
                self._handshake = _HandshakeState.INITIALIZING
            return True
        if descriptor.method == "notifications/initialized":
            if not self._handshake_lock.acquire(blocking=False):
                self._emit_control_response(rpc_error_response(INVALID_REQUEST))
                return False
            try:
                if self._handshake is not _HandshakeState.RESPONDED:
                    self._emit_control_response(rpc_error_response(INVALID_REQUEST))
                    return False
                self._handshake = _HandshakeState.READY
            finally:
                self._handshake_lock.release()
            return False
        with self._handshake_lock:
            ready = self._handshake is _HandshakeState.READY
        if not ready:
            self._emit_control_response(
                rpc_error_response(INVALID_REQUEST, request_id=descriptor.request_id)
            )
        return ready

    def _accept_descriptor(self, descriptor: ClientMessageDescriptor) -> None:
        if not self._handshake_allows(descriptor):
            return
        if descriptor.is_cancellation:
            self._admission.request_cancellation(descriptor)
            return
        admission = self._lifecycle.admit(lambda: self._admission.try_acquire_work(descriptor))
        if admission is None:
            self._route_control(descriptor)
            return
        if admission.response is not None:
            self._emit_control_response(admission.response)
            return
        if admission.full:
            self._route_control(descriptor)
            return
        lease = admission.lease
        if lease is None:
            raise TransportLeaseError
        try:
            self._queue.put_nowait(_QueuedWork(descriptor, lease))
        except queue.Full:
            self._admission.complete_work(lease)
            self._emit_control_response(
                rpc_error_response(SERVER_BUSY, request_id=descriptor.request_id)
            )
            self._admission.acknowledge_work(lease)

    def _handle_payload(self, payload: bytes) -> bool:
        try:
            descriptor = decode_and_prevalidate(payload)
        except TransportProtocolError as error:
            self._emit_control_response(error.response, wait=error.close)
            return not error.close
        self._accept_descriptor(descriptor)
        return True

    def _read_loop(self, read_chunk: Callable[[int], bytes]) -> None:
        framer = RequestLineFramer()
        keep_reading = True
        reached_eof = False
        while keep_reading and not self._stopping:
            try:
                chunk = read_chunk(READ_CHUNK_BYTES)
            except BaseException:
                chunk = b""
            if type(chunk) is not bytes or not chunk:
                reached_eof = True
                break
            for event in framer.feed(chunk):
                if isinstance(event, FrameFailure):
                    self._emit_control_response(event.response, wait=True)
                    keep_reading = False
                    break
                if not self._handle_payload(event.payload):
                    keep_reading = False
                    break
        if reached_eof:
            for event in framer.finish():
                self._emit_control_response(event.response, wait=True)

    def _control_loop(self) -> None:
        while True:
            item = self._control_queue.get()
            if item is None:
                self._control_queue.task_done()
                return
            self._admission.release_control(item.lease)
            try:
                self._write_response(item.response)
            finally:
                item.done.set()
                self._control_queue.task_done()

    def _wait_for_uninstall_peers(self) -> bool:
        deadline = time.monotonic() + self._uninstall_drain_seconds
        with self._state_condition:
            while self._admission.active_work_count > 1:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._state_condition.wait(remaining)
        return True

    def _recovery_response(self, request_id: int | str | None) -> Mapping[str, object]:
        try:
            response = self._uninstall_recovery_response(request_id)
            if isinstance(response, Mapping):
                return response
        except BaseException:
            pass
        return rpc_error_response(GENERIC_INTERNAL_ERROR, request_id=request_id)

    def _write_marked_or_recovery(
        self,
        response: Mapping[str, object],
        item: _QueuedWork,
    ) -> bool:
        try:
            marked = self._encoded_response(response)
        except TransportProtocolError:
            marked = None
        recovery = self._prepared_frame(
            self._recovery_response(item.descriptor.request_id),
            item.descriptor,
        )
        with self._write_lock:
            if self._disconnected or self._write_frame is None:
                self._lifecycle.recovery_required()
                return False
            if marked is not None:
                try:
                    self._write_frame(marked)
                except BaseException:
                    self._lifecycle.recovery_required()
                    self._disconnected = True
                    return False
                else:
                    return True
            self._lifecycle.recovery_required()
            if recovery is None:
                self._disconnected = True
                return False
            try:
                self._write_frame(recovery)
            except BaseException:
                self._disconnected = True
            return False

    def _dispatch_item(self, item: _QueuedWork) -> Mapping[str, object] | None:
        try:
            with self._lifecycle.bind_work(item.lease):
                response = self._dispatch(item.descriptor)
            if response is not None and not isinstance(response, Mapping):
                raise TypeError
            return response
        except BaseException:
            return self._fixed_failure(item.descriptor)

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            worker: WorkerLease | None = None
            resource: ResourceLease | None = None
            resource_acquired = False
            try:
                worker = self._admission.try_acquire_worker(item.lease)
                if worker is None:
                    raise TransportLeaseError
                if item.descriptor.is_resource_read:
                    self._resource_gate.acquire()
                    resource_acquired = True
                    resource = self._admission.try_acquire_resource(item.lease)
                    if resource is None:
                        raise TransportLeaseError
                response = self._dispatch_item(item)
            except BaseException:
                response = self._fixed_failure(item.descriptor)
            finally:
                if worker is not None:
                    try:
                        self._admission.release_worker(worker)
                    except TransportLeaseError:
                        pass

            uninstall_owner = self._lifecycle.owns_uninstall(item.lease)
            clean_uninstall = False
            if uninstall_owner:
                clean_uninstall = self._wait_for_uninstall_peers()
                if clean_uninstall:
                    try:
                        clean_uninstall = self._close_application() is True
                    except BaseException:
                        clean_uninstall = False
                if not clean_uninstall:
                    self._lifecycle.recovery_required()
                    response = self._recovery_response(item.descriptor.request_id)
            try:
                cancelled = self._admission.complete_work(
                    item.lease,
                    allow_cancellation=not uninstall_owner,
                )
                selected = cancelled if cancelled is not None else response
                written = False
                if item.descriptor.method == "initialize":
                    with self._handshake_lock:
                        if selected is not None:
                            written = self._write_response(selected, item.descriptor)
                        self._handshake = (
                            _HandshakeState.RESPONDED
                            if written and selected is not None and "result" in selected
                            else _HandshakeState.FAILED
                        )
                elif selected is not None:
                    if uninstall_owner and clean_uninstall:
                        written = self._write_marked_or_recovery(selected, item)
                    else:
                        written = self._write_response(selected, item.descriptor)
                if uninstall_owner and clean_uninstall and written:
                    self._lifecycle.uninstall_ready()
            finally:
                if resource is not None:
                    try:
                        self._admission.release_resource(resource)
                    except TransportLeaseError:
                        pass
                if resource_acquired:
                    self._resource_gate.release()
                try:
                    self._admission.acknowledge_work(item.lease)
                finally:
                    with self._state_condition:
                        self._state_condition.notify_all()
                    self._queue.task_done()
            self._maybe_exit_after_flush()

    def _maybe_exit_after_flush(self) -> None:
        state = self._lifecycle.state
        if state not in {ProcessState.SWAP_PENDING, ProcessState.UNINSTALL_READY}:
            return
        with self._state_condition:
            if not self._started or self._exit_called:
                return
            if self._admission.active_work_count != 0:
                return
            if state is ProcessState.UNINSTALL_READY and self._disconnected:
                self._lifecycle.recovery_required()
                return
            self._exit_called = True
        try:
            self._exit_process(SWAP_EXIT_CODE)
        except BaseException:
            if state is ProcessState.UNINSTALL_READY:
                self._lifecycle.recovery_required()

    @staticmethod
    def _read_stdio(maximum: int) -> bytes:
        return os.read(sys.stdin.buffer.fileno(), maximum)

    @staticmethod
    def _write_stdio(frame: bytes) -> None:
        if sys.stdout.buffer.write(frame) != len(frame):
            raise OSError("stdio response write is incomplete")
        sys.stdout.buffer.flush()

    def run(
        self,
        *,
        read_chunk: Callable[[int], bytes] | None = None,
        write_frame: Callable[[bytes], None] | None = None,
        before_read: Callable[[], None] | None = None,
    ) -> None:
        reader = self._read_stdio if read_chunk is None else read_chunk
        writer = self._write_stdio if write_frame is None else write_frame
        if not callable(reader) or not callable(writer):
            raise TypeError("stdio callbacks must be callable")
        if before_read is not None and not callable(before_read):
            raise TypeError("before-read callback must be callable")
        with self._state_condition:
            if self._started:
                raise RuntimeError("owned stdio runner is single-use")
            self._started = True
            self._write_frame = writer
        self._lifecycle.activate()
        for worker in self._workers:
            worker.start()
        self._control_worker.start()
        try:
            if before_read is not None:
                before_read()
            self._maybe_exit_after_flush()
            self._read_loop(reader)
        finally:
            self._stopping = True
            self._queue.join()
            for _worker in self._workers:
                self._queue.put(None)
            self._queue.join()
            for worker in self._workers:
                worker.join()
            self._control_queue.join()
            self._control_queue.put(None)
            self._control_queue.join()
            self._control_worker.join()
            self._lifecycle.close()
