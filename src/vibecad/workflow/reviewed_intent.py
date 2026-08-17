"""Closed ModelProgram payload for one Reviewed semantic intent.

The payload carries a backend-neutral Parametric Feature Graph plus the exact
public semantic identity selected by the planner.  It deliberately carries no
FreeCAD ``TypeId``, property name, callable, import path, proof authority, or
native execution selector.  A trusted product router must independently bind
the two public identities to a static Reviewed manifest before lowering.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self

from vibecad.parametric.feature_graph_v2 import (
    ParametricFeatureGraphError,
    ParametricFeatureGraphV2,
)

REVIEWED_INTENT_PROGRAM_SCHEMA_VERSION = 1
MAX_REVIEWED_INTENT_PROGRAM_BYTES = 256 * 1024

_PROGRAM_DIGEST_DOMAIN = b"vibecad-reviewed-intent-program-v1\0"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_ID = re.compile(r"^[a-z][a-z0-9_]*(?:[.-][a-z0-9_]+)+$")
_MAX_OPERATION_ID_BYTES = 256
_MAX_SEMANTIC_OPERATION_BYTES = 512


class ReviewedIntentProgramErrorCode(StrEnum):
    """Closed, non-reflective payload failures."""

    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    BUDGET_EXCEEDED = "budget_exceeded"
    UNSUPPORTED_VERSION = "unsupported_version"


class ReviewedIntentProgramError(ValueError):
    """Fixed Reviewed-intent payload failure."""

    __slots__ = ("code", "path")

    def __init__(self, code: ReviewedIntentProgramErrorCode, path: str = "/") -> None:
        if type(code) is not ReviewedIntentProgramErrorCode:
            raise TypeError("code must be a ReviewedIntentProgramErrorCode")
        if (
            type(path) is not str
            or not path.startswith("/")
            or not path.isprintable()
            or len(path.splitlines()) != 1
            or len(path.encode("utf-8", errors="ignore")) > 384
        ):
            path = "/"
        self.code = code
        self.path = path
        super().__init__(f"reviewed intent program error ({code.value}) at {path}")


def _fail(code: ReviewedIntentProgramErrorCode, path: str = "/") -> None:
    raise ReviewedIntentProgramError(code, path)


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT, path)
    return value


def _bounded_text(value: object, *, maximum: int, path: str) -> str:
    if (
        type(value) is not str
        or not value
        or not value.isprintable()
        or len(value.splitlines()) != 1
    ):
        _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT, path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT, path)
    if size > maximum:
        _fail(ReviewedIntentProgramErrorCode.BUDGET_EXCEEDED, path)
    return value


def _operation_id(value: object) -> str:
    result = _bounded_text(
        value,
        maximum=_MAX_OPERATION_ID_BYTES,
        path="/operation_id",
    )
    if _OPERATION_ID.fullmatch(result) is None:
        _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT, "/operation_id")
    return result


def _semantic_operation(value: object) -> str:
    result = _bounded_text(
        value,
        maximum=_MAX_SEMANTIC_OPERATION_BYTES,
        path="/semantic_operation",
    )
    prefix, separator, digest = result.rpartition("@")
    if (
        separator != "@"
        or not prefix
        or "/" not in prefix
        or any(character.isspace() for character in result)
        or _DIGEST.fullmatch(digest) is None
    ):
        _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT, "/semantic_operation")
    return result


def _canonical(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT)
    if not raw:
        _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT)
    if len(raw) > MAX_REVIEWED_INTENT_PROGRAM_BYTES:
        _fail(ReviewedIntentProgramErrorCode.BUDGET_EXCEEDED)
    return raw


def _exact_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT)
    try:
        keys = tuple(value)
        if any(type(key) is not str for key in keys) or len(set(keys)) != len(keys):
            _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT)
        result = {key: value[key] for key in keys}
    except ReviewedIntentProgramError:
        raise
    except Exception:
        _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT)
    if set(result) != {
        "schema_version",
        "operation_id",
        "semantic_operation",
        "intent_graph_sha256",
        "intent_content_sha256",
        "intent_graph",
    }:
        _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT)
    return result


def _plain_json(value: object, *, depth: int = 0) -> object:
    """Thaw ModelProgram's immutable JSON representation for PFG decoding."""

    if depth > 64:
        _fail(ReviewedIntentProgramErrorCode.BUDGET_EXCEEDED, "/intent_graph")
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if isinstance(value, Mapping):
        try:
            keys = tuple(value)
            if any(type(key) is not str for key in keys) or len(set(keys)) != len(keys):
                _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT, "/intent_graph")
            return {key: _plain_json(value[key], depth=depth + 1) for key in keys}
        except ReviewedIntentProgramError:
            raise
        except Exception:
            _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT, "/intent_graph")
    if type(value) in {list, tuple}:
        return [_plain_json(item, depth=depth + 1) for item in value]
    _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT, "/intent_graph")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedIntentProgramV1:
    """One exact Reviewed semantic request carried by a ModelProgram command."""

    operation_id: str
    semantic_operation: str
    intent_graph_sha256: str
    intent_content_sha256: str
    intent_graph: ParametricFeatureGraphV2
    schema_version: int = REVIEWED_INTENT_PROGRAM_SCHEMA_VERSION
    program_sha256: str = field(init=False)
    canonical_bytes: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != REVIEWED_INTENT_PROGRAM_SCHEMA_VERSION
        ):
            _fail(ReviewedIntentProgramErrorCode.UNSUPPORTED_VERSION, "/schema_version")
        object.__setattr__(self, "operation_id", _operation_id(self.operation_id))
        object.__setattr__(
            self,
            "semantic_operation",
            _semantic_operation(self.semantic_operation),
        )
        object.__setattr__(
            self,
            "intent_graph_sha256",
            _digest(self.intent_graph_sha256, "/intent_graph_sha256"),
        )
        object.__setattr__(
            self,
            "intent_content_sha256",
            _digest(self.intent_content_sha256, "/intent_content_sha256"),
        )
        if type(self.intent_graph) is not ParametricFeatureGraphV2:
            _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT, "/intent_graph")
        try:
            graph = ParametricFeatureGraphV2.from_mapping(self.intent_graph.to_mapping())
        except (ParametricFeatureGraphError, AttributeError, TypeError, ValueError):
            _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT, "/intent_graph")
        if not hmac.compare_digest(
            graph.graph_sha256, self.intent_graph_sha256
        ) or not hmac.compare_digest(
            hashlib.sha256(graph.canonical_bytes).hexdigest(),
            self.intent_content_sha256,
        ):
            _fail(ReviewedIntentProgramErrorCode.INTEGRITY_FAILURE, "/intent_graph")
        object.__setattr__(self, "intent_graph", graph)
        canonical_bytes = _canonical(self.to_mapping())
        object.__setattr__(self, "canonical_bytes", canonical_bytes)
        object.__setattr__(
            self,
            "program_sha256",
            hashlib.sha256(_PROGRAM_DIGEST_DOMAIN + canonical_bytes).hexdigest(),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "semantic_operation": self.semantic_operation,
            "intent_graph_sha256": self.intent_graph_sha256,
            "intent_content_sha256": self.intent_content_sha256,
            "intent_graph": self.intent_graph.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        item = _exact_mapping(value)
        try:
            graph_mapping = _plain_json(item["intent_graph"])
            _canonical(graph_mapping)
            graph = ParametricFeatureGraphV2.from_mapping(graph_mapping)
        except (ParametricFeatureGraphError, TypeError, ValueError):
            _fail(ReviewedIntentProgramErrorCode.INVALID_INPUT, "/intent_graph")
        return cls(
            schema_version=item["schema_version"],
            operation_id=item["operation_id"],
            semantic_operation=item["semantic_operation"],
            intent_graph_sha256=item["intent_graph_sha256"],
            intent_content_sha256=item["intent_content_sha256"],
            intent_graph=graph,
        )


__all__ = [
    "MAX_REVIEWED_INTENT_PROGRAM_BYTES",
    "REVIEWED_INTENT_PROGRAM_SCHEMA_VERSION",
    "ReviewedIntentProgramError",
    "ReviewedIntentProgramErrorCode",
    "ReviewedIntentProgramV1",
]
