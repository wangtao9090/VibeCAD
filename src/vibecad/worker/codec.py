"""Strict private codec for one local FreeCAD Worker generation."""

from __future__ import annotations

import json
import math
import re
from enum import StrEnum
from typing import Any

from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER

MAX_WORKER_REQUEST_BYTES = 1_048_576
MAX_WORKER_RESPONSE_BYTES = 262_144
MAX_WORKER_JSON_DEPTH = 64
MAX_WORKER_JSON_NODES = 16_384
MAX_WORKER_JSON_STRING_BYTES = 524_288

_GENERATION = re.compile(r"worker_generation_[0-9a-f]{32}\Z")
_REQUEST = re.compile(r"worker_request_[0-9a-f]{32}\Z")
_METHODS = frozenset(
    {
        "worker.ready",
        "candidate.bind",
        "candidate.release",
        "session.create_empty",
        "session.load_fcstd",
        "session.checkpoint_fcstd",
        "session.close",
        "program.begin",
        "program.execute_command",
        "session.export_step",
        "worker.shutdown",
    }
)


class WorkerWireErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    INVALID_HANDLE = "invalid_handle"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INVALID_INPUT = "invalid_input"
    INVALID_CANDIDATE = "invalid_candidate"
    CAD_FAILURE = "cad_failure"
    ARTIFACT_FAILURE = "artifact_failure"
    INTEGRITY_FAILURE = "integrity_failure"
    INTERNAL_ERROR = "internal_error"


class WorkerCodecError(ValueError):
    pass


def _fail() -> WorkerCodecError:
    return WorkerCodecError("invalid Worker protocol payload")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise _fail()
        result[key] = value
    return result


def _constant(_value: str) -> object:
    raise _fail()


def _walk_json(value: object) -> None:
    remaining = MAX_WORKER_JSON_NODES

    def visit(item: object, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_WORKER_JSON_DEPTH:
            raise _fail()
        if item is None or type(item) is bool:
            return
        if type(item) is int:
            if abs(item) > MAX_SAFE_JSON_INTEGER:
                raise _fail()
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise _fail()
            return
        if type(item) is str:
            try:
                encoded = item.encode("utf-8")
            except UnicodeError:
                raise _fail() from None
            if len(encoded) > MAX_WORKER_JSON_STRING_BYTES:
                raise _fail()
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise _fail()
                visit(key, depth + 1)
                visit(child, depth + 1)
            return
        raise _fail()

    visit(value, 0)


def _canonical(value: object) -> bytes:
    _walk_json(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise _fail() from None


def _decode(raw: object, *, maximum: int) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        raise _fail()
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except WorkerCodecError:
        raise
    except (
        json.JSONDecodeError,
        UnicodeError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
    ):
        raise _fail() from None
    if type(value) is not dict:
        raise _fail()
    _walk_json(value)
    if _canonical(value) != raw:
        raise _fail()
    return value


def _identifier(value: object, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _validate_request(value: dict[str, object]) -> None:
    if set(value) != {
        "schema_version",
        "generation_id",
        "request_id",
        "method",
        "params",
    }:
        raise _fail()
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not _identifier(value["generation_id"], _GENERATION)
        or not _identifier(value["request_id"], _REQUEST)
        or type(value["method"]) is not str
        or value["method"] not in _METHODS
        or type(value["params"]) is not dict
    ):
        raise _fail()


def encode_worker_request(value: object) -> bytes:
    if type(value) is not dict:
        raise _fail()
    _validate_request(value)
    raw = _canonical(value)
    if not raw or len(raw) > MAX_WORKER_REQUEST_BYTES:
        raise _fail()
    return raw


def decode_worker_request(raw: object) -> dict[str, object]:
    value = _decode(raw, maximum=MAX_WORKER_REQUEST_BYTES)
    _validate_request(value)
    return value


def _validate_response(value: dict[str, object]) -> None:
    common = {
        "schema_version",
        "generation_id",
        "request_id",
        "ok",
    }
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or not _identifier(value.get("generation_id"), _GENERATION)
        or not _identifier(value.get("request_id"), _REQUEST)
        or type(value.get("ok")) is not bool
    ):
        raise _fail()
    if value["ok"] is True:
        if set(value) != common | {"result"} or type(value["result"]) is not dict:
            raise _fail()
        return
    if set(value) != common | {"error"} or type(value["error"]) is not dict:
        raise _fail()
    error = value["error"]
    if (
        set(error) != {"schema_version", "code"}
        or type(error["schema_version"]) is not int
        or error["schema_version"] != 1
        or type(error["code"]) is not str
    ):
        raise _fail()
    try:
        WorkerWireErrorCode(error["code"])
    except ValueError:
        raise _fail() from None


def encode_worker_response(value: object) -> bytes:
    if type(value) is not dict:
        raise _fail()
    _validate_response(value)
    raw = _canonical(value)
    if not raw or len(raw) > MAX_WORKER_RESPONSE_BYTES:
        raise _fail()
    return raw


def decode_worker_response(
    raw: object,
    *,
    expected_generation_id: object,
    expected_request_id: object,
) -> dict[str, object]:
    value = _decode(raw, maximum=MAX_WORKER_RESPONSE_BYTES)
    _validate_response(value)
    if (
        not _identifier(expected_generation_id, _GENERATION)
        or not _identifier(expected_request_id, _REQUEST)
        or value["generation_id"] != expected_generation_id
        or value["request_id"] != expected_request_id
    ):
        raise _fail()
    return value


def success_response(
    *,
    generation_id: str,
    request_id: str,
    result: dict[str, Any],
) -> dict[str, object]:
    value = {
        "schema_version": 1,
        "generation_id": generation_id,
        "request_id": request_id,
        "ok": True,
        "result": result,
    }
    encode_worker_response(value)
    return value


def error_response(
    *,
    generation_id: str,
    request_id: str,
    code: WorkerWireErrorCode,
) -> dict[str, object]:
    if type(code) is not WorkerWireErrorCode:
        raise _fail()
    value = {
        "schema_version": 1,
        "generation_id": generation_id,
        "request_id": request_id,
        "ok": False,
        "error": {
            "schema_version": 1,
            "code": code.value,
        },
    }
    encode_worker_response(value)
    return value


__all__ = (
    "MAX_WORKER_REQUEST_BYTES",
    "MAX_WORKER_RESPONSE_BYTES",
    "WorkerCodecError",
    "WorkerWireErrorCode",
    "decode_worker_request",
    "decode_worker_response",
    "encode_worker_request",
    "encode_worker_response",
)
