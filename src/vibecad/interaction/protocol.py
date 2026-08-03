"""Strict, non-runnable value codec for the future local interaction protocol."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = (
    "MAX_PROTOCOL_DEPTH",
    "MAX_PROTOCOL_KEY_BYTES",
    "MAX_PROTOCOL_NODES",
    "MAX_PROTOCOL_REQUEST_BYTES",
    "MAX_PROTOCOL_RESPONSE_BYTES",
    "MAX_PROTOCOL_STRING_BYTES",
    "ProtocolError",
    "ProtocolErrorCode",
    "ProtocolRequest",
    "ProtocolResponse",
    "decode_request",
    "decode_response",
    "encode_failure",
    "encode_success",
    "unavailable_response",
)

MAX_PROTOCOL_REQUEST_BYTES = 589_824
MAX_PROTOCOL_RESPONSE_BYTES = 1_048_576
MAX_PROTOCOL_DEPTH = 72
MAX_PROTOCOL_NODES = 10_240
MAX_PROTOCOL_STRING_BYTES = 524_288
MAX_PROTOCOL_KEY_BYTES = 256
MAX_SAFE_INTEGER = 2**53 - 1

_PROTOCOL = "vibecad.local"
_VERSION = {"major": 1, "minor": 0}
_REQUEST_RE = re.compile(r"request_[0-9a-f]{32}\Z")
_KERNEL_RE = re.compile(r"kernel_[0-9a-f]{32}\Z")
_SESSION_RE = re.compile(r"session_[0-9a-f]{32}\Z")
_OPEN_KEY_RE = re.compile(r"checkout_open_[0-9a-f]{32}\Z")
_CHECKOUT_RE = re.compile(r"checkout_[0-9a-f]{32}\Z")
_PROJECT_RE = re.compile(r"project_[0-9a-f]{32}\Z")
_TASK_RE = re.compile(r"task_[0-9a-f]{32}\Z")
_DRAFT_RE = re.compile(r"draft_[0-9a-f]{32}\Z")
_REVISION_RE = re.compile(r"revision_[0-9a-f]{32}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")

_METHODS = frozenset(
    {"initialize", "application.call", "checkout.open", "checkout.get", "checkout.close"}
)
_OPERATIONS = frozenset(
    {
        "create_task",
        "get_task",
        "submit_model_program",
        "resume_task",
        "accept_draft",
        "reject_draft",
        "get_capabilities",
    }
)


class ProtocolErrorCode(StrEnum):
    MALFORMED_MESSAGE = "malformed_message"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNKNOWN_METHOD = "unknown_method"
    BUDGET_EXCEEDED = "budget_exceeded"
    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE = "unavailable"
    INTERNAL_ERROR = "internal_error"


_MESSAGES = {
    ProtocolErrorCode.MALFORMED_MESSAGE: "The local interaction message is malformed.",
    ProtocolErrorCode.UNSUPPORTED_VERSION: "The local interaction protocol version is unsupported.",
    ProtocolErrorCode.UNKNOWN_METHOD: "The local interaction method is unknown.",
    ProtocolErrorCode.BUDGET_EXCEEDED: "The local interaction message exceeds a fixed budget.",
    ProtocolErrorCode.INVALID_REQUEST: "The local interaction request is invalid.",
    ProtocolErrorCode.UNAVAILABLE: "The local interaction method is unavailable.",
    ProtocolErrorCode.INTERNAL_ERROR: "The local interaction operation failed.",
}


class ProtocolError(ValueError):
    __slots__ = ("code", "message")

    def __init__(self, code: ProtocolErrorCode) -> None:
        if type(code) is not ProtocolErrorCode:
            raise TypeError("code must be a ProtocolErrorCode")
        self.code = code
        self.message = _MESSAGES[code]
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class ProtocolRequest:
    request_id: str
    method: str
    params: dict[str, object]

    def __post_init__(self) -> None:
        _identifier(self.request_id, _REQUEST_RE)
        if type(self.method) is not str or self.method not in _METHODS:
            raise ProtocolError(ProtocolErrorCode.UNKNOWN_METHOD)
        if type(self.params) is not dict:
            raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
        _validate_params(self.method, self.params)


@dataclass(frozen=True, slots=True)
class ProtocolResponse:
    request_id: str
    result: dict[str, object] | None
    error: dict[str, str] | None

    def __post_init__(self) -> None:
        _identifier(self.request_id, _REQUEST_RE)
        if (self.result is None) == (self.error is None):
            raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)


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
    except ValueError as exc:  # pragma: no cover - json already limits this path
        raise _JsonFailure from exc
    if value < -MAX_SAFE_INTEGER or value > MAX_SAFE_INTEGER:
        raise _JsonFailure
    return value


def _constant(_raw: str) -> object:
    raise _JsonFailure


def _walk(
    value: object,
    *,
    invalid_code: ProtocolErrorCode = ProtocolErrorCode.MALFORMED_MESSAGE,
    forbid_local_path: bool = False,
) -> int:
    stack: list[tuple[bool, object, int]] = [(False, value, 1)]
    active: set[int] = set()
    nodes = 0
    while stack:
        exiting, item, depth = stack.pop()
        if exiting:
            active.remove(id(item))
            continue
        if depth > MAX_PROTOCOL_DEPTH:
            raise ProtocolError(ProtocolErrorCode.BUDGET_EXCEEDED)
        nodes += 1
        if nodes > MAX_PROTOCOL_NODES:
            raise ProtocolError(ProtocolErrorCode.BUDGET_EXCEEDED)
        if type(item) is dict:
            identity = id(item)
            if identity in active:
                raise ProtocolError(invalid_code)
            active.add(identity)
            stack.append((True, item, depth))
            for key, child in reversed(tuple(item.items())):
                if type(key) is not str:
                    raise ProtocolError(invalid_code)
                if forbid_local_path and key == "local_path":
                    raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
                try:
                    key_size = len(key.encode("utf-8"))
                except UnicodeEncodeError:
                    raise ProtocolError(invalid_code) from None
                if key_size > MAX_PROTOCOL_KEY_BYTES:
                    raise ProtocolError(ProtocolErrorCode.BUDGET_EXCEEDED)
                stack.append((False, child, depth + 1))
        elif type(item) is list:
            identity = id(item)
            if identity in active:
                raise ProtocolError(invalid_code)
            active.add(identity)
            stack.append((True, item, depth))
            for child in reversed(item):
                stack.append((False, child, depth + 1))
        elif type(item) is str:
            try:
                string_size = len(item.encode("utf-8"))
            except UnicodeEncodeError:
                raise ProtocolError(invalid_code) from None
            if string_size > MAX_PROTOCOL_STRING_BYTES:
                raise ProtocolError(ProtocolErrorCode.BUDGET_EXCEEDED)
        elif type(item) is int and not -MAX_SAFE_INTEGER <= item <= MAX_SAFE_INTEGER:
            raise ProtocolError(ProtocolErrorCode.BUDGET_EXCEEDED)
        elif type(item) is float and not math.isfinite(item):
            raise ProtocolError(invalid_code)
        elif item is not None and type(item) not in (bool, int, float):
            raise ProtocolError(invalid_code)
    return nodes


def _decode_json(raw: object, maximum: int) -> object:
    if type(raw) is not bytes:
        raise ProtocolError(ProtocolErrorCode.MALFORMED_MESSAGE)
    if len(raw) > maximum:
        raise ProtocolError(ProtocolErrorCode.BUDGET_EXCEEDED)
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ProtocolError(ProtocolErrorCode.MALFORMED_MESSAGE)
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_int=_integer,
            parse_constant=_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _JsonFailure, RecursionError):
        raise ProtocolError(ProtocolErrorCode.MALFORMED_MESSAGE) from None
    _walk(value)
    return value


def _exact(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
    return value


def _identifier(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
    return value


def _bounded_ascii(value: object, maximum: int) -> str:
    if type(value) is not str:
        raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST) from None
    if not 1 <= len(encoded) <= maximum or any(char < 0x20 or char > 0x7E for char in encoded):
        raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
    return value


def _nonnegative_safe_integer(value: object) -> int:
    if type(value) is not int or value < 0 or value > MAX_SAFE_INTEGER:
        raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
    return value


def _validate_version(value: object) -> dict[str, int]:
    if type(value) is not dict or set(value) != {"major", "minor"}:
        raise ProtocolError(ProtocolErrorCode.UNSUPPORTED_VERSION)
    version = value
    if (
        type(version["major"]) is not int
        or type(version["minor"]) is not int
        or version["major"] != 1
        or version["minor"] != 0
    ):
        raise ProtocolError(ProtocolErrorCode.UNSUPPORTED_VERSION)
    return {"major": 1, "minor": 0}


def _validate_source(value: object, *, resolved: bool = False) -> dict[str, object]:
    if type(value) is not dict:
        raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
    if resolved:
        source = _exact(
            value,
            {
                "kind",
                "project_id",
                "revision_id",
                "manifest_sha256",
                "model_sha256",
                "size_bytes",
                "task_id",
                "draft_id",
                "task_generation",
            },
        )
        if source["kind"] not in ("head", "draft"):
            raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
        _identifier(source["project_id"], _PROJECT_RE)
        _identifier(source["revision_id"], _REVISION_RE)
        _identifier(source["manifest_sha256"], _DIGEST_RE)
        _identifier(source["model_sha256"], _DIGEST_RE)
        _nonnegative_safe_integer(source["size_bytes"])
        if source["kind"] == "head":
            if any(source[key] is not None for key in ("task_id", "draft_id", "task_generation")):
                raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
        else:
            _identifier(source["task_id"], _TASK_RE)
            _identifier(source["draft_id"], _DRAFT_RE)
            _nonnegative_safe_integer(source["task_generation"])
        return source
    kind = value.get("kind")
    if kind == "head":
        source = _exact(value, {"kind", "project_id"})
        _identifier(source["project_id"], _PROJECT_RE)
        return source
    if kind == "draft":
        source = _exact(value, {"kind", "task_id", "draft_id", "expected_generation"})
        _identifier(source["task_id"], _TASK_RE)
        _identifier(source["draft_id"], _DRAFT_RE)
        _nonnegative_safe_integer(source["expected_generation"])
        return source
    raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)


def _validate_params(method: str, value: object) -> dict[str, object]:
    if method == "initialize":
        params = _exact(value, {"client_name", "client_version"})
        _bounded_ascii(params["client_name"], 64)
        _bounded_ascii(params["client_version"], 32)
        return params
    if method == "application.call":
        params = _exact(value, {"kernel_id", "session_id", "operation", "request"})
        _identifier(params["kernel_id"], _KERNEL_RE)
        _identifier(params["session_id"], _SESSION_RE)
        if (
            type(params["operation"]) is not str
            or params["operation"] not in _OPERATIONS
            or type(params["request"]) is not dict
        ):
            raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
        return params
    if method == "checkout.open":
        params = _exact(value, {"kernel_id", "session_id", "open_key", "source"})
        _identifier(params["kernel_id"], _KERNEL_RE)
        _identifier(params["session_id"], _SESSION_RE)
        _identifier(params["open_key"], _OPEN_KEY_RE)
        _validate_source(params["source"])
        return params
    params = _exact(value, {"kernel_id", "session_id", "checkout_id"})
    _identifier(params["kernel_id"], _KERNEL_RE)
    _identifier(params["session_id"], _SESSION_RE)
    _identifier(params["checkout_id"], _CHECKOUT_RE)
    return params


def decode_request(raw: object) -> ProtocolRequest:
    value = _decode_json(raw, MAX_PROTOCOL_REQUEST_BYTES)
    outer = _exact(value, {"protocol", "version", "request_id", "method", "params"})
    if outer["protocol"] != _PROTOCOL:
        raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
    _validate_version(outer["version"])
    request_id = _identifier(outer["request_id"], _REQUEST_RE)
    method = outer["method"]
    if type(method) is not str or method not in _METHODS:
        raise ProtocolError(ProtocolErrorCode.UNKNOWN_METHOD)
    params = _validate_params(method, outer["params"])
    return ProtocolRequest(request_id=request_id, method=method, params=dict(params))


def _validate_checkout_descriptor(value: object) -> dict[str, object]:
    descriptor = _exact(
        value,
        {
            "checkout_id",
            "open_key",
            "state",
            "authoritative",
            "dirty",
            "source",
            "initial_model_sha256",
            "current_model_sha256",
            "current_size_bytes",
        },
    )
    _identifier(descriptor["checkout_id"], _CHECKOUT_RE)
    _identifier(descriptor["open_key"], _OPEN_KEY_RE)
    if descriptor["state"] not in ("open", "closed"):
        raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
    if descriptor["authoritative"] is not False or type(descriptor["dirty"]) is not bool:
        raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
    _validate_source(descriptor["source"], resolved=True)
    _identifier(descriptor["initial_model_sha256"], _DIGEST_RE)
    _identifier(descriptor["current_model_sha256"], _DIGEST_RE)
    _nonnegative_safe_integer(descriptor["current_size_bytes"])
    return descriptor


def _validate_result(method: str, value: object) -> dict[str, object]:
    _walk(
        value,
        invalid_code=ProtocolErrorCode.INVALID_REQUEST,
        forbid_local_path=True,
    )
    if method == "initialize":
        result = _exact(value, {"kernel_id", "session_id", "protocol_version", "capabilities"})
        _identifier(result["kernel_id"], _KERNEL_RE)
        _identifier(result["session_id"], _SESSION_RE)
        try:
            _validate_version(result["protocol_version"])
        except ProtocolError:
            raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST) from None
        capabilities = _exact(
            result["capabilities"],
            {
                "application_dispatch",
                "checkout_dispatch",
                "authenticated_transport",
                "local_path_delivery",
            },
        )
        if any(value is not False for value in capabilities.values()):
            raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
        return result
    if method == "application.call":
        result = _exact(value, {"response"})
        response = _exact(result["response"], {"schema_version", "ok", "result", "error"})
        if type(response["schema_version"]) is not int or response["schema_version"] != 1:
            raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
        if type(response["ok"]) is not bool:
            raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
        if response["ok"]:
            if type(response["result"]) is not dict or response["error"] is not None:
                raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
        else:
            if response["result"] is not None:
                raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
            error = _exact(response["error"], {"schema_version", "code", "path", "message"})
            if (
                type(error["schema_version"]) is not int
                or error["schema_version"] != 1
                or type(error["code"]) is not str
                or type(error["path"]) is not str
                or type(error["message"]) is not str
            ):
                raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
        return result
    return _validate_checkout_descriptor(value)


def _encode(value: dict[str, object]) -> bytes:
    _walk(
        value,
        invalid_code=ProtocolErrorCode.INVALID_REQUEST,
        forbid_local_path=True,
    )
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST) from None
    if len(raw) > MAX_PROTOCOL_RESPONSE_BYTES:
        raise ProtocolError(ProtocolErrorCode.BUDGET_EXCEEDED)
    return raw


def encode_success(request: ProtocolRequest, result: object) -> bytes:
    if type(request) is not ProtocolRequest:
        raise TypeError("request must be a ProtocolRequest")
    validated = _validate_result(request.method, result)
    return _encode(
        {
            "protocol": _PROTOCOL,
            "version": dict(_VERSION),
            "request_id": request.request_id,
            "result": validated,
            "error": None,
        }
    )


def encode_failure(request_id: object, code: ProtocolErrorCode) -> bytes:
    canonical_id = _identifier(request_id, _REQUEST_RE)
    if type(code) is not ProtocolErrorCode:
        raise TypeError("code must be a ProtocolErrorCode")
    return _encode(
        {
            "protocol": _PROTOCOL,
            "version": dict(_VERSION),
            "request_id": canonical_id,
            "result": None,
            "error": {"code": code.value, "message": _MESSAGES[code]},
        }
    )


def unavailable_response(request: ProtocolRequest) -> bytes:
    if type(request) is not ProtocolRequest:
        raise TypeError("request must be a ProtocolRequest")
    return encode_failure(request.request_id, ProtocolErrorCode.UNAVAILABLE)


def decode_response(raw: object, *, method: str | None = None) -> ProtocolResponse:
    value = _decode_json(raw, MAX_PROTOCOL_RESPONSE_BYTES)
    outer = _exact(value, {"protocol", "version", "request_id", "result", "error"})
    if outer["protocol"] != _PROTOCOL:
        raise ProtocolError(ProtocolErrorCode.UNSUPPORTED_VERSION)
    _validate_version(outer["version"])
    request_id = _identifier(outer["request_id"], _REQUEST_RE)
    result = outer["result"]
    error = outer["error"]
    if (result is None) == (error is None):
        raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
    if result is not None:
        _walk(
            result,
            invalid_code=ProtocolErrorCode.INVALID_REQUEST,
            forbid_local_path=True,
        )
        if method is None:
            raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
        if method not in _METHODS:
            raise ProtocolError(ProtocolErrorCode.UNKNOWN_METHOD)
        result = _validate_result(method, result)
        return ProtocolResponse(request_id=request_id, result=dict(result), error=None)
    error_map = _exact(error, {"code", "message"})
    try:
        code = ProtocolErrorCode(error_map["code"])
    except (TypeError, ValueError):
        raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST) from None
    if error_map["message"] != _MESSAGES[code]:
        raise ProtocolError(ProtocolErrorCode.INVALID_REQUEST)
    return ProtocolResponse(
        request_id=request_id,
        result=None,
        error={"code": code.value, "message": _MESSAGES[code]},
    )
