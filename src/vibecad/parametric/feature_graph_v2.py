"""Backend-neutral, content-bound parametric feature graph contracts.

This module is deliberately an inert interchange boundary.  It describes a
feature/result graph without naming a CAD backend or claiming that an ontology
term is executable.  Operation families, value types, operators, port roles,
and result semantics are all content-addressed terms.  A trusted adapter must
bind every referenced term and verify its own port contract before execution.

The core wire stays stable when a vocabulary adds a new feature family, value
type, expression operator, or semantic role: unknown terms round-trip as inert
data and never select code by themselves.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn, Self

PARAMETRIC_FEATURE_GRAPH_SCHEMA_VERSION = 2
MAX_PARAMETRIC_FEATURE_GRAPH_BYTES = 256 * 1024

MAX_FEATURE_GRAPH_TERMS = 512
MAX_FEATURE_GRAPH_BODIES = 64
MAX_FEATURE_GRAPH_PARAMETERS = 256
MAX_FEATURE_GRAPH_REFERENCES = 512
MAX_FEATURE_GRAPH_NODES = 128
MAX_FEATURE_GRAPH_EXTENSIONS = 64
MAX_PORTS_PER_NODE = 64
MAX_RESULTS_PER_NODE = 32
MAX_DEPENDENCIES_PER_NODE = 64
MAX_REFERENCES_PER_NODE = 64
MAX_PARAMETERS_PER_NODE = 64
MAX_BINDINGS_PER_PORT = 64
MAX_EXTENSIONS_PER_ELEMENT = 16
MAX_REFERENCE_QUALIFIERS = 16
MAX_OCCURRENCE_PATH_STEPS = 16
MAX_EXPRESSION_NODES = 128
MAX_EXPRESSION_INPUTS_PER_NODE = 32
MAX_TYPED_VALUE_BYTES = 64 * 1024
MAX_TYPED_VALUE_DEPTH = 16
MAX_TYPED_VALUE_NODES = 4_096
MAX_TYPED_VALUE_STRING_BYTES = 64 * 1024

_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_TEXT_BYTES = 256
_MAX_ERROR_PATH_BYTES = 384
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 32_768
_GRAPH_DIGEST_DOMAIN = b"vibecad-parametric-feature-graph-v2\0"
_VALUE_DIGEST_DOMAIN = b"vibecad-parametric-term-value-v2\0"

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9._-]{0,95}$")
_TERM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,191}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ParametricFeatureGraphErrorCode(StrEnum):
    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_VERSION = "unsupported_version"
    BUDGET_EXCEEDED = "budget_exceeded"
    UNKNOWN_REFERENCE = "unknown_reference"
    INVALID_ORDER = "invalid_order"
    CYCLE = "cycle"
    INTEGRITY_FAILURE = "integrity_failure"


class ParametricFeatureGraphError(ValueError):
    """Bounded error which never reflects rejected input values."""

    def __init__(self, code: ParametricFeatureGraphErrorCode, path: str = "") -> None:
        if type(code) is not ParametricFeatureGraphErrorCode:
            raise TypeError("code must be an exact ParametricFeatureGraphErrorCode")
        if type(path) is not str:
            raise TypeError("path must be a string")
        try:
            size = len(path.encode("utf-8"))
        except UnicodeError:
            raise ValueError("path must be bounded") from None
        if size > _MAX_ERROR_PATH_BYTES:
            raise ValueError("path must be bounded")
        self.code = code
        self.path = path
        super().__init__(code.value)


def _fail(code: ParametricFeatureGraphErrorCode, path: str = "") -> NoReturn:
    raise ParametricFeatureGraphError(code, path)


def _text(
    value: object,
    path: str,
    *,
    maximum: int = _MAX_TEXT_BYTES,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if type(value) is not str:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    if (
        not value
        or value != value.strip()
        or not value.isprintable()
        or len(value.splitlines()) != 1
        or size > maximum
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    return value


def _identifier(value: object, path: str) -> str:
    return _text(value, path, maximum=96, pattern=_IDENTIFIER)


def _term(value: object, path: str) -> str:
    result = _text(value, path, maximum=192, pattern=_TERM)
    if ".." in result or "//" in result:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    return result


def _version(value: object, path: str) -> str:
    return _text(value, path, maximum=64, pattern=_VERSION)


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    return value


def _integer(
    value: object, path: str, *, minimum: int = 0, maximum: int = _MAX_SAFE_INTEGER
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    return value


def _enum[EnumT: StrEnum](value: object, enum_type: type[EnumT], path: str) -> EnumT:
    if type(value) is enum_type:
        return value
    if type(value) is not str:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    try:
        return enum_type(value)
    except ValueError:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)


def _tuple(
    value: object,
    path: str,
    *,
    item_type: type,
    maximum: int,
    minimum: int = 0,
    key=None,
) -> tuple:
    if type(value) is not tuple:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    if len(value) > maximum:
        _fail(ParametricFeatureGraphErrorCode.BUDGET_EXCEEDED, path)
    if len(value) < minimum or not all(type(item) is item_type for item in value):
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    if key is not None:
        keys = tuple(key(item) for item in value)
        if len(set(keys)) != len(keys):
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    return value


def _identifier_tuple(
    value: object,
    path: str,
    *,
    maximum: int,
    minimum: int = 0,
    ordered: bool = False,
) -> tuple[str, ...]:
    if type(value) is not tuple:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    if len(value) > maximum:
        _fail(ParametricFeatureGraphErrorCode.BUDGET_EXCEEDED, path)
    if len(value) < minimum:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    result = tuple(_identifier(item, f"{path}/{index}") for index, item in enumerate(value))
    if len(set(result)) != len(result):
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    return result if ordered else tuple(sorted(result))


def _fields(value: object, *, allowed: set[str], required: set[str], path: str) -> dict:
    if type(value) is not dict or any(type(key) is not str for key in value):
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    if any(key not in allowed for key in value):
        _fail(ParametricFeatureGraphErrorCode.UNKNOWN_FIELD, f"{path}/unknown_field")
    missing = sorted(required - set(value))
    if missing:
        _fail(ParametricFeatureGraphErrorCode.MISSING_FIELD, f"{path}/{missing[0]}")
    return dict(value)


def _wire_tuple(value: object, path: str, *, maximum: int) -> tuple[object, ...]:
    if type(value) is not list:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    if len(value) > maximum:
        _fail(ParametricFeatureGraphErrorCode.BUDGET_EXCEEDED, path)
    return tuple(value)


def _json_tree(
    value: object,
    path: str,
    *,
    depth: int,
    remaining: list[int],
    maximum_depth: int,
    maximum_string_bytes: int,
) -> None:
    remaining[0] -= 1
    if remaining[0] < 0 or depth > maximum_depth:
        _fail(ParametricFeatureGraphErrorCode.BUDGET_EXCEEDED, path)
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > _MAX_SAFE_INTEGER:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
        return
    if type(value) is str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
        if size > maximum_string_bytes:
            _fail(ParametricFeatureGraphErrorCode.BUDGET_EXCEEDED, path)
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _json_tree(
                item,
                f"{path}/{index}",
                depth=depth + 1,
                remaining=remaining,
                maximum_depth=maximum_depth,
                maximum_string_bytes=maximum_string_bytes,
            )
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
            _json_tree(
                key,
                f"{path}/key",
                depth=depth + 1,
                remaining=remaining,
                maximum_depth=maximum_depth,
                maximum_string_bytes=maximum_string_bytes,
            )
            _json_tree(
                item,
                f"{path}/field",
                depth=depth + 1,
                remaining=remaining,
                maximum_depth=maximum_depth,
                maximum_string_bytes=maximum_string_bytes,
            )
        return
    _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)


def _canonical_json(
    value: object,
    *,
    maximum: int = MAX_PARAMETRIC_FEATURE_GRAPH_BYTES,
    maximum_depth: int = _MAX_JSON_DEPTH,
    maximum_nodes: int = _MAX_JSON_NODES,
    maximum_string_bytes: int = MAX_TYPED_VALUE_STRING_BYTES,
) -> bytes:
    _json_tree(
        value,
        "",
        depth=0,
        remaining=[maximum_nodes],
        maximum_depth=maximum_depth,
        maximum_string_bytes=maximum_string_bytes,
    )
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT)
    if not raw or len(raw) > maximum:
        _fail(ParametricFeatureGraphErrorCode.BUDGET_EXCEEDED)
    return raw


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if type(key) is not str or key in result:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT)
        result[key] = value
    return result


def _constant(_value: str) -> NoReturn:
    _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT)


def _decode_json(
    raw: object,
    *,
    maximum: int = MAX_PARAMETRIC_FEATURE_GRAPH_BYTES,
    maximum_depth: int = _MAX_JSON_DEPTH,
    maximum_nodes: int = _MAX_JSON_NODES,
) -> object:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT)
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except ParametricFeatureGraphError:
        raise
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError, RecursionError):
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT)
    if (
        _canonical_json(
            value,
            maximum=maximum,
            maximum_depth=maximum_depth,
            maximum_nodes=maximum_nodes,
        )
        != raw
    ):
        _fail(ParametricFeatureGraphErrorCode.INTEGRITY_FAILURE)
    return value


def _typed_value_json(value: object) -> bytes:
    return _canonical_json(
        value,
        maximum=MAX_TYPED_VALUE_BYTES,
        maximum_depth=MAX_TYPED_VALUE_DEPTH,
        maximum_nodes=MAX_TYPED_VALUE_NODES,
    )


class FeatureNodeKind(StrEnum):
    """Only graph structure is closed; feature semantics are ontology terms."""

    FEATURE = "feature"
    REFERENCE = "reference"


class SemanticReferenceScope(StrEnum):
    ORIGIN = "origin"
    FEATURE = "feature"
    EXTERNAL = "external"


class ExtensionDisposition(StrEnum):
    INERT = "inert"


class GraphAuthority(StrEnum):
    TRUSTED_ADAPTER_REQUIRED = "trusted_adapter_required"


@dataclass(frozen=True, slots=True, kw_only=True)
class SemanticTermRefV2:
    term_ref_id: str
    namespace: str
    vocabulary_version: str
    term_id: str
    term_definition_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "term_ref_id", _identifier(self.term_ref_id, "/term_ref_id"))
        object.__setattr__(self, "namespace", _identifier(self.namespace, "/namespace"))
        object.__setattr__(
            self,
            "vocabulary_version",
            _version(self.vocabulary_version, "/vocabulary_version"),
        )
        object.__setattr__(self, "term_id", _term(self.term_id, "/term_id"))
        object.__setattr__(
            self,
            "term_definition_sha256",
            _digest(self.term_definition_sha256, "/term_definition_sha256"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "term_ref_id": self.term_ref_id,
            "namespace": self.namespace,
            "vocabulary_version": self.vocabulary_version,
            "term_id": self.term_id,
            "term_definition_sha256": self.term_definition_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {
            "term_ref_id",
            "namespace",
            "vocabulary_version",
            "term_id",
            "term_definition_sha256",
        }
        return cls(**_fields(value, allowed=keys, required=keys, path=path))


@dataclass(frozen=True, slots=True, kw_only=True)
class InertExtensionV2:
    extension_id: str
    namespace: str
    vocabulary_version: str
    schema_term_ref_id: str
    payload_sha256: str
    payload_size_bytes: int
    media_type: str
    disposition: ExtensionDisposition = ExtensionDisposition.INERT

    def __post_init__(self) -> None:
        object.__setattr__(self, "extension_id", _identifier(self.extension_id, "/extension_id"))
        object.__setattr__(self, "namespace", _identifier(self.namespace, "/namespace"))
        object.__setattr__(
            self,
            "vocabulary_version",
            _version(self.vocabulary_version, "/vocabulary_version"),
        )
        object.__setattr__(
            self,
            "schema_term_ref_id",
            _identifier(self.schema_term_ref_id, "/schema_term_ref_id"),
        )
        object.__setattr__(self, "payload_sha256", _digest(self.payload_sha256, "/payload_sha256"))
        object.__setattr__(
            self,
            "payload_size_bytes",
            _integer(self.payload_size_bytes, "/payload_size_bytes"),
        )
        object.__setattr__(
            self,
            "media_type",
            _text(self.media_type, "/media_type", maximum=128, pattern=_MEDIA_TYPE),
        )
        if self.disposition is not ExtensionDisposition.INERT:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/disposition")

    @property
    def executable(self) -> bool:
        return False

    def to_mapping(self) -> dict[str, object]:
        return {
            "extension_id": self.extension_id,
            "namespace": self.namespace,
            "vocabulary_version": self.vocabulary_version,
            "schema_term_ref_id": self.schema_term_ref_id,
            "payload_sha256": self.payload_sha256,
            "payload_size_bytes": self.payload_size_bytes,
            "media_type": self.media_type,
            "disposition": self.disposition.value,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {
            "extension_id",
            "namespace",
            "vocabulary_version",
            "schema_term_ref_id",
            "payload_sha256",
            "payload_size_bytes",
            "media_type",
            "disposition",
        }
        fields = _fields(value, allowed=keys, required=keys, path=path)
        return cls(
            extension_id=fields["extension_id"],
            namespace=fields["namespace"],
            vocabulary_version=fields["vocabulary_version"],
            schema_term_ref_id=fields["schema_term_ref_id"],
            payload_sha256=fields["payload_sha256"],
            payload_size_bytes=fields["payload_size_bytes"],
            media_type=fields["media_type"],
            disposition=_enum(fields["disposition"], ExtensionDisposition, f"{path}/disposition"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TermTypedValueV2:
    """One immutable canonical JSON value interpreted only by bound terms."""

    value_id: str
    value_type_term_ref_id: str
    encoding_term_ref_id: str
    canonical_value: bytes
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "value_id", _identifier(self.value_id, "/value_id"))
        object.__setattr__(
            self,
            "value_type_term_ref_id",
            _identifier(self.value_type_term_ref_id, "/value_type_term_ref_id"),
        )
        object.__setattr__(
            self,
            "encoding_term_ref_id",
            _identifier(self.encoding_term_ref_id, "/encoding_term_ref_id"),
        )
        if type(self.canonical_value) is not bytes:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/canonical_value")
        decoded = _decode_json(
            self.canonical_value,
            maximum=MAX_TYPED_VALUE_BYTES,
            maximum_depth=MAX_TYPED_VALUE_DEPTH,
            maximum_nodes=MAX_TYPED_VALUE_NODES,
        )
        if _typed_value_json(decoded) != self.canonical_value:
            _fail(ParametricFeatureGraphErrorCode.INTEGRITY_FAILURE, "/canonical_value")
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_EXTENSIONS_PER_ELEMENT,
            ),
        )

    @classmethod
    def from_value(
        cls,
        *,
        value_id: str,
        value_type_term_ref_id: str,
        encoding_term_ref_id: str,
        value: object,
        extension_ids: tuple[str, ...] = (),
    ) -> Self:
        return cls(
            value_id=value_id,
            value_type_term_ref_id=value_type_term_ref_id,
            encoding_term_ref_id=encoding_term_ref_id,
            canonical_value=_typed_value_json(value),
            extension_ids=extension_ids,
        )

    @property
    def value(self) -> object:
        return _decode_json(
            self.canonical_value,
            maximum=MAX_TYPED_VALUE_BYTES,
            maximum_depth=MAX_TYPED_VALUE_DEPTH,
            maximum_nodes=MAX_TYPED_VALUE_NODES,
        )

    @property
    def value_sha256(self) -> str:
        payload = b"\0".join(
            (
                self.value_type_term_ref_id.encode("ascii"),
                self.encoding_term_ref_id.encode("ascii"),
                self.canonical_value,
            )
        )
        return hashlib.sha256(_VALUE_DIGEST_DOMAIN + payload).hexdigest()

    def to_mapping(self) -> dict[str, object]:
        return {
            "value_id": self.value_id,
            "value_type_term_ref_id": self.value_type_term_ref_id,
            "encoding_term_ref_id": self.encoding_term_ref_id,
            "value": self.value,
            "extension_ids": list(self.extension_ids),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {
            "value_id",
            "value_type_term_ref_id",
            "encoding_term_ref_id",
            "value",
            "extension_ids",
        }
        fields = _fields(value, allowed=keys, required=keys, path=path)
        return cls.from_value(
            value_id=fields["value_id"],
            value_type_term_ref_id=fields["value_type_term_ref_id"],
            encoding_term_ref_id=fields["encoding_term_ref_id"],
            value=fields["value"],
            extension_ids=tuple(
                _wire_tuple(
                    fields["extension_ids"],
                    f"{path}/extension_ids",
                    maximum=MAX_EXTENSIONS_PER_ELEMENT,
                )
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpressionInputV2:
    input_id: str
    role_term_ref_id: str
    value_type_term_ref_id: str
    source_id: str
    ordinal: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_id", _identifier(self.input_id, "/input_id"))
        object.__setattr__(
            self,
            "role_term_ref_id",
            _identifier(self.role_term_ref_id, "/role_term_ref_id"),
        )
        object.__setattr__(
            self,
            "value_type_term_ref_id",
            _identifier(self.value_type_term_ref_id, "/value_type_term_ref_id"),
        )
        object.__setattr__(self, "source_id", _identifier(self.source_id, "/source_id"))
        object.__setattr__(
            self,
            "ordinal",
            _integer(self.ordinal, "/ordinal", maximum=MAX_EXPRESSION_INPUTS_PER_NODE - 1),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "input_id": self.input_id,
            "role_term_ref_id": self.role_term_ref_id,
            "value_type_term_ref_id": self.value_type_term_ref_id,
            "source_id": self.source_id,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {
            "input_id",
            "role_term_ref_id",
            "value_type_term_ref_id",
            "source_id",
            "ordinal",
        }
        return cls(**_fields(value, allowed=keys, required=keys, path=path))


def _expression_input_order(item: ExpressionInputV2) -> tuple[str, int, str]:
    return item.role_term_ref_id, item.ordinal, item.input_id


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpressionNodeV2:
    expression_node_id: str
    operator_term_ref_id: str
    result_type_term_ref_id: str
    inputs: tuple[ExpressionInputV2, ...] = ()
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "expression_node_id",
            _identifier(self.expression_node_id, "/expression_node_id"),
        )
        object.__setattr__(
            self,
            "operator_term_ref_id",
            _identifier(self.operator_term_ref_id, "/operator_term_ref_id"),
        )
        object.__setattr__(
            self,
            "result_type_term_ref_id",
            _identifier(self.result_type_term_ref_id, "/result_type_term_ref_id"),
        )
        inputs = _tuple(
            self.inputs,
            "/inputs",
            item_type=ExpressionInputV2,
            maximum=MAX_EXPRESSION_INPUTS_PER_NODE,
            key=lambda item: item.input_id,
        )
        slots = tuple((item.role_term_ref_id, item.ordinal) for item in inputs)
        if len(set(slots)) != len(slots):
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/inputs")
        object.__setattr__(self, "inputs", tuple(sorted(inputs, key=_expression_input_order)))
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_EXTENSIONS_PER_ELEMENT,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "expression_node_id": self.expression_node_id,
            "operator_term_ref_id": self.operator_term_ref_id,
            "result_type_term_ref_id": self.result_type_term_ref_id,
            "inputs": [item.to_mapping() for item in self.inputs],
            "extension_ids": list(self.extension_ids),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {
            "expression_node_id",
            "operator_term_ref_id",
            "result_type_term_ref_id",
            "inputs",
            "extension_ids",
        }
        fields = _fields(value, allowed=keys, required=keys, path=path)
        raw_inputs = _wire_tuple(
            fields["inputs"], f"{path}/inputs", maximum=MAX_EXPRESSION_INPUTS_PER_NODE
        )
        return cls(
            expression_node_id=fields["expression_node_id"],
            operator_term_ref_id=fields["operator_term_ref_id"],
            result_type_term_ref_id=fields["result_type_term_ref_id"],
            inputs=tuple(
                ExpressionInputV2.from_mapping(item, f"{path}/inputs/{index}")
                for index, item in enumerate(raw_inputs)
            ),
            extension_ids=tuple(
                _wire_tuple(
                    fields["extension_ids"],
                    f"{path}/extension_ids",
                    maximum=MAX_EXTENSIONS_PER_ELEMENT,
                )
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TermBoundExpressionV2:
    expression_id: str
    nodes: tuple[ExpressionNodeV2, ...]
    result_node_id: str
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "expression_id", _identifier(self.expression_id, "/expression_id"))
        nodes = _tuple(
            self.nodes,
            "/nodes",
            item_type=ExpressionNodeV2,
            maximum=MAX_EXPRESSION_NODES,
            minimum=1,
            key=lambda item: item.expression_node_id,
        )
        object.__setattr__(
            self, "nodes", tuple(sorted(nodes, key=lambda item: item.expression_node_id))
        )
        object.__setattr__(
            self,
            "result_node_id",
            _identifier(self.result_node_id, "/result_node_id"),
        )
        if self.result_node_id not in {item.expression_node_id for item in nodes}:
            _fail(ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE, "/result_node_id")
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_EXTENSIONS_PER_ELEMENT,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "expression_id": self.expression_id,
            "nodes": [item.to_mapping() for item in self.nodes],
            "result_node_id": self.result_node_id,
            "extension_ids": list(self.extension_ids),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {"expression_id", "nodes", "result_node_id", "extension_ids"}
        fields = _fields(value, allowed=keys, required=keys, path=path)
        raw_nodes = _wire_tuple(fields["nodes"], f"{path}/nodes", maximum=MAX_EXPRESSION_NODES)
        return cls(
            expression_id=fields["expression_id"],
            nodes=tuple(
                ExpressionNodeV2.from_mapping(item, f"{path}/nodes/{index}")
                for index, item in enumerate(raw_nodes)
            ),
            result_node_id=fields["result_node_id"],
            extension_ids=tuple(
                _wire_tuple(
                    fields["extension_ids"],
                    f"{path}/extension_ids",
                    maximum=MAX_EXTENSIONS_PER_ELEMENT,
                )
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DesignParameterV2:
    parameter_id: str
    name: str
    semantic_role_term_ref_id: str
    value: TermTypedValueV2
    expression: TermBoundExpressionV2 | None = None
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameter_id", _identifier(self.parameter_id, "/parameter_id"))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        object.__setattr__(
            self,
            "semantic_role_term_ref_id",
            _identifier(self.semantic_role_term_ref_id, "/semantic_role_term_ref_id"),
        )
        if type(self.value) is not TermTypedValueV2:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/value")
        if self.expression is not None and type(self.expression) is not TermBoundExpressionV2:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/expression")
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_EXTENSIONS_PER_ELEMENT,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "parameter_id": self.parameter_id,
            "name": self.name,
            "semantic_role_term_ref_id": self.semantic_role_term_ref_id,
            "value": self.value.to_mapping(),
            "expression": None if self.expression is None else self.expression.to_mapping(),
            "extension_ids": list(self.extension_ids),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {
            "parameter_id",
            "name",
            "semantic_role_term_ref_id",
            "value",
            "expression",
            "extension_ids",
        }
        fields = _fields(value, allowed=keys, required=keys, path=path)
        expression = fields["expression"]
        return cls(
            parameter_id=fields["parameter_id"],
            name=fields["name"],
            semantic_role_term_ref_id=fields["semantic_role_term_ref_id"],
            value=TermTypedValueV2.from_mapping(fields["value"], f"{path}/value"),
            expression=(
                None
                if expression is None
                else TermBoundExpressionV2.from_mapping(expression, f"{path}/expression")
            ),
            extension_ids=tuple(
                _wire_tuple(
                    fields["extension_ids"],
                    f"{path}/extension_ids",
                    maximum=MAX_EXTENSIONS_PER_ELEMENT,
                )
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class OccurrencePathStepV2:
    transform_node_id: str
    transform_result_id: str
    occurrence_index: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "transform_node_id",
            _identifier(self.transform_node_id, "/transform_node_id"),
        )
        object.__setattr__(
            self,
            "transform_result_id",
            _identifier(self.transform_result_id, "/transform_result_id"),
        )
        object.__setattr__(
            self,
            "occurrence_index",
            _integer(self.occurrence_index, "/occurrence_index"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "transform_node_id": self.transform_node_id,
            "transform_result_id": self.transform_result_id,
            "occurrence_index": self.occurrence_index,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {"transform_node_id", "transform_result_id", "occurrence_index"}
        return cls(**_fields(value, allowed=keys, required=keys, path=path))


@dataclass(frozen=True, slots=True, kw_only=True)
class SemanticReferenceV2:
    reference_id: str
    scope: SemanticReferenceScope
    semantic_role_term_ref_id: str
    value_type_term_ref_id: str
    locator_term_ref_id: str
    source_node_id: str | None = None
    source_geometry_id: str | None = None
    source_content_sha256: str | None = None
    occurrence_path: tuple[OccurrencePathStepV2, ...] = ()
    qualifier_term_ref_ids: tuple[str, ...] = ()
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference_id", _identifier(self.reference_id, "/reference_id"))
        scope = _enum(self.scope, SemanticReferenceScope, "/scope")
        object.__setattr__(self, "scope", scope)
        for field in (
            "semantic_role_term_ref_id",
            "value_type_term_ref_id",
            "locator_term_ref_id",
        ):
            object.__setattr__(self, field, _identifier(getattr(self, field), f"/{field}"))
        if self.source_node_id is not None:
            object.__setattr__(
                self,
                "source_node_id",
                _identifier(self.source_node_id, "/source_node_id"),
            )
        if self.source_geometry_id is not None:
            object.__setattr__(
                self,
                "source_geometry_id",
                _identifier(self.source_geometry_id, "/source_geometry_id"),
            )
        if self.source_content_sha256 is not None:
            object.__setattr__(
                self,
                "source_content_sha256",
                _digest(self.source_content_sha256, "/source_content_sha256"),
            )
        if scope is SemanticReferenceScope.FEATURE:
            if (
                self.source_node_id is None
                or self.source_geometry_id is None
                or self.source_content_sha256 is not None
            ):
                _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/scope")
        elif scope is SemanticReferenceScope.ORIGIN:
            if any(
                item is not None
                for item in (
                    self.source_node_id,
                    self.source_geometry_id,
                    self.source_content_sha256,
                )
            ):
                _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/scope")
        elif any(item is not None for item in (self.source_node_id, self.source_geometry_id)) or (
            self.source_content_sha256 is None
        ):
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/scope")
        occurrence_path = _tuple(
            self.occurrence_path,
            "/occurrence_path",
            item_type=OccurrencePathStepV2,
            maximum=MAX_OCCURRENCE_PATH_STEPS,
            key=lambda item: (item.transform_node_id, item.transform_result_id),
        )
        object.__setattr__(self, "occurrence_path", occurrence_path)
        object.__setattr__(
            self,
            "qualifier_term_ref_ids",
            _identifier_tuple(
                self.qualifier_term_ref_ids,
                "/qualifier_term_ref_ids",
                maximum=MAX_REFERENCE_QUALIFIERS,
            ),
        )
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_EXTENSIONS_PER_ELEMENT,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "reference_id": self.reference_id,
            "scope": self.scope.value,
            "semantic_role_term_ref_id": self.semantic_role_term_ref_id,
            "value_type_term_ref_id": self.value_type_term_ref_id,
            "locator_term_ref_id": self.locator_term_ref_id,
            "source_node_id": self.source_node_id,
            "source_geometry_id": self.source_geometry_id,
            "source_content_sha256": self.source_content_sha256,
            "occurrence_path": [item.to_mapping() for item in self.occurrence_path],
            "qualifier_term_ref_ids": list(self.qualifier_term_ref_ids),
            "extension_ids": list(self.extension_ids),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {
            "reference_id",
            "scope",
            "semantic_role_term_ref_id",
            "value_type_term_ref_id",
            "locator_term_ref_id",
            "source_node_id",
            "source_geometry_id",
            "source_content_sha256",
            "occurrence_path",
            "qualifier_term_ref_ids",
            "extension_ids",
        }
        fields = _fields(value, allowed=keys, required=keys, path=path)
        raw_path = _wire_tuple(
            fields["occurrence_path"],
            f"{path}/occurrence_path",
            maximum=MAX_OCCURRENCE_PATH_STEPS,
        )
        return cls(
            reference_id=fields["reference_id"],
            scope=_enum(fields["scope"], SemanticReferenceScope, f"{path}/scope"),
            semantic_role_term_ref_id=fields["semantic_role_term_ref_id"],
            value_type_term_ref_id=fields["value_type_term_ref_id"],
            locator_term_ref_id=fields["locator_term_ref_id"],
            source_node_id=fields["source_node_id"],
            source_geometry_id=fields["source_geometry_id"],
            source_content_sha256=fields["source_content_sha256"],
            occurrence_path=tuple(
                OccurrencePathStepV2.from_mapping(item, f"{path}/occurrence_path/{index}")
                for index, item in enumerate(raw_path)
            ),
            qualifier_term_ref_ids=tuple(
                _wire_tuple(
                    fields["qualifier_term_ref_ids"],
                    f"{path}/qualifier_term_ref_ids",
                    maximum=MAX_REFERENCE_QUALIFIERS,
                )
            ),
            extension_ids=tuple(
                _wire_tuple(
                    fields["extension_ids"],
                    f"{path}/extension_ids",
                    maximum=MAX_EXTENSIONS_PER_ELEMENT,
                )
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureInputPortV2:
    port_id: str
    semantic_role_term_ref_id: str
    value_type_term_ref_id: str
    minimum_cardinality: int
    maximum_cardinality: int
    ordered: bool
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "port_id", _identifier(self.port_id, "/port_id"))
        object.__setattr__(
            self,
            "semantic_role_term_ref_id",
            _identifier(self.semantic_role_term_ref_id, "/semantic_role_term_ref_id"),
        )
        object.__setattr__(
            self,
            "value_type_term_ref_id",
            _identifier(self.value_type_term_ref_id, "/value_type_term_ref_id"),
        )
        minimum = _integer(
            self.minimum_cardinality,
            "/minimum_cardinality",
            maximum=MAX_BINDINGS_PER_PORT,
        )
        maximum = _integer(
            self.maximum_cardinality,
            "/maximum_cardinality",
            minimum=1,
            maximum=MAX_BINDINGS_PER_PORT,
        )
        if minimum > maximum or type(self.ordered) is not bool:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/cardinality")
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_EXTENSIONS_PER_ELEMENT,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "port_id": self.port_id,
            "semantic_role_term_ref_id": self.semantic_role_term_ref_id,
            "value_type_term_ref_id": self.value_type_term_ref_id,
            "minimum_cardinality": self.minimum_cardinality,
            "maximum_cardinality": self.maximum_cardinality,
            "ordered": self.ordered,
            "extension_ids": list(self.extension_ids),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {
            "port_id",
            "semantic_role_term_ref_id",
            "value_type_term_ref_id",
            "minimum_cardinality",
            "maximum_cardinality",
            "ordered",
            "extension_ids",
        }
        fields = _fields(value, allowed=keys, required=keys, path=path)
        return cls(
            port_id=fields["port_id"],
            semantic_role_term_ref_id=fields["semantic_role_term_ref_id"],
            value_type_term_ref_id=fields["value_type_term_ref_id"],
            minimum_cardinality=fields["minimum_cardinality"],
            maximum_cardinality=fields["maximum_cardinality"],
            ordered=fields["ordered"],
            extension_ids=tuple(
                _wire_tuple(
                    fields["extension_ids"],
                    f"{path}/extension_ids",
                    maximum=MAX_EXTENSIONS_PER_ELEMENT,
                )
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureResultV2:
    result_id: str
    semantic_role_term_ref_id: str
    value_type_term_ref_id: str
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/result_id"))
        object.__setattr__(
            self,
            "semantic_role_term_ref_id",
            _identifier(self.semantic_role_term_ref_id, "/semantic_role_term_ref_id"),
        )
        object.__setattr__(
            self,
            "value_type_term_ref_id",
            _identifier(self.value_type_term_ref_id, "/value_type_term_ref_id"),
        )
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_EXTENSIONS_PER_ELEMENT,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "result_id": self.result_id,
            "semantic_role_term_ref_id": self.semantic_role_term_ref_id,
            "value_type_term_ref_id": self.value_type_term_ref_id,
            "extension_ids": list(self.extension_ids),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {
            "result_id",
            "semantic_role_term_ref_id",
            "value_type_term_ref_id",
            "extension_ids",
        }
        fields = _fields(value, allowed=keys, required=keys, path=path)
        return cls(
            result_id=fields["result_id"],
            semantic_role_term_ref_id=fields["semantic_role_term_ref_id"],
            value_type_term_ref_id=fields["value_type_term_ref_id"],
            extension_ids=tuple(
                _wire_tuple(
                    fields["extension_ids"],
                    f"{path}/extension_ids",
                    maximum=MAX_EXTENSIONS_PER_ELEMENT,
                )
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureDependencyV2:
    dependency_id: str
    port_id: str
    upstream_node_id: str
    upstream_result_id: str
    ordinal: int = 0

    def __post_init__(self) -> None:
        for field in ("dependency_id", "port_id", "upstream_node_id", "upstream_result_id"):
            object.__setattr__(self, field, _identifier(getattr(self, field), f"/{field}"))
        object.__setattr__(
            self,
            "ordinal",
            _integer(self.ordinal, "/ordinal", maximum=MAX_BINDINGS_PER_PORT - 1),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "dependency_id": self.dependency_id,
            "port_id": self.port_id,
            "upstream_node_id": self.upstream_node_id,
            "upstream_result_id": self.upstream_result_id,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {
            "dependency_id",
            "port_id",
            "upstream_node_id",
            "upstream_result_id",
            "ordinal",
        }
        return cls(**_fields(value, allowed=keys, required=keys, path=path))


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureReferenceBindingV2:
    binding_id: str
    port_id: str
    reference_id: str
    ordinal: int = 0

    def __post_init__(self) -> None:
        for field in ("binding_id", "port_id", "reference_id"):
            object.__setattr__(self, field, _identifier(getattr(self, field), f"/{field}"))
        object.__setattr__(
            self,
            "ordinal",
            _integer(self.ordinal, "/ordinal", maximum=MAX_BINDINGS_PER_PORT - 1),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "port_id": self.port_id,
            "reference_id": self.reference_id,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {"binding_id", "port_id", "reference_id", "ordinal"}
        return cls(**_fields(value, allowed=keys, required=keys, path=path))


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureParameterBindingV2:
    binding_id: str
    port_id: str
    parameter_id: str
    ordinal: int = 0

    def __post_init__(self) -> None:
        for field in ("binding_id", "port_id", "parameter_id"):
            object.__setattr__(self, field, _identifier(getattr(self, field), f"/{field}"))
        object.__setattr__(
            self,
            "ordinal",
            _integer(self.ordinal, "/ordinal", maximum=MAX_BINDINGS_PER_PORT - 1),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "port_id": self.port_id,
            "parameter_id": self.parameter_id,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {"binding_id", "port_id", "parameter_id", "ordinal"}
        return cls(**_fields(value, allowed=keys, required=keys, path=path))


def _binding_order(item: object) -> tuple[str, int, str]:
    if type(item) is FeatureDependencyV2:
        return item.port_id, item.ordinal, item.dependency_id
    return item.port_id, item.ordinal, item.binding_id  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureIntentV2:
    node_kind: FeatureNodeKind
    family_term_ref_id: str
    operation_term_ref_id: str
    input_ports: tuple[FeatureInputPortV2, ...] = ()
    dependencies: tuple[FeatureDependencyV2, ...] = ()
    references: tuple[FeatureReferenceBindingV2, ...] = ()
    parameter_bindings: tuple[FeatureParameterBindingV2, ...] = ()
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "node_kind",
            _enum(self.node_kind, FeatureNodeKind, "/node_kind"),
        )
        for field in ("family_term_ref_id", "operation_term_ref_id"):
            object.__setattr__(self, field, _identifier(getattr(self, field), f"/{field}"))
        ports = _tuple(
            self.input_ports,
            "/input_ports",
            item_type=FeatureInputPortV2,
            maximum=MAX_PORTS_PER_NODE,
            key=lambda item: item.port_id,
        )
        dependencies = _tuple(
            self.dependencies,
            "/dependencies",
            item_type=FeatureDependencyV2,
            maximum=MAX_DEPENDENCIES_PER_NODE,
            key=lambda item: item.dependency_id,
        )
        references = _tuple(
            self.references,
            "/references",
            item_type=FeatureReferenceBindingV2,
            maximum=MAX_REFERENCES_PER_NODE,
            key=lambda item: item.binding_id,
        )
        parameters = _tuple(
            self.parameter_bindings,
            "/parameter_bindings",
            item_type=FeatureParameterBindingV2,
            maximum=MAX_PARAMETERS_PER_NODE,
            key=lambda item: item.binding_id,
        )
        binding_ids = tuple(
            [item.dependency_id for item in dependencies]
            + [item.binding_id for item in references]
            + [item.binding_id for item in parameters]
        )
        slots = tuple(
            (item.port_id, item.ordinal) for item in (*dependencies, *references, *parameters)
        )
        if len(set(binding_ids)) != len(binding_ids) or len(set(slots)) != len(slots):
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/bindings")
        port_by_id = {item.port_id: item for item in ports}
        counts = {item.port_id: 0 for item in ports}
        ordinals: dict[str, list[int]] = {item.port_id: [] for item in ports}
        for binding in (*dependencies, *references, *parameters):
            if binding.port_id not in port_by_id:
                _fail(ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE, "/bindings")
            counts[binding.port_id] += 1
            ordinals[binding.port_id].append(binding.ordinal)
        for port in ports:
            count = counts[port.port_id]
            if not port.minimum_cardinality <= count <= port.maximum_cardinality:
                _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/input_ports")
            if tuple(sorted(ordinals[port.port_id])) != tuple(range(count)):
                _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/bindings")
        object.__setattr__(self, "input_ports", tuple(sorted(ports, key=lambda item: item.port_id)))
        object.__setattr__(self, "dependencies", tuple(sorted(dependencies, key=_binding_order)))
        object.__setattr__(self, "references", tuple(sorted(references, key=_binding_order)))
        object.__setattr__(
            self,
            "parameter_bindings",
            tuple(sorted(parameters, key=_binding_order)),
        )
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_EXTENSIONS_PER_ELEMENT,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node_kind": self.node_kind.value,
            "family_term_ref_id": self.family_term_ref_id,
            "operation_term_ref_id": self.operation_term_ref_id,
            "input_ports": [item.to_mapping() for item in self.input_ports],
            "dependencies": [item.to_mapping() for item in self.dependencies],
            "references": [item.to_mapping() for item in self.references],
            "parameter_bindings": [item.to_mapping() for item in self.parameter_bindings],
            "extension_ids": list(self.extension_ids),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {
            "node_kind",
            "family_term_ref_id",
            "operation_term_ref_id",
            "input_ports",
            "dependencies",
            "references",
            "parameter_bindings",
            "extension_ids",
        }
        fields = _fields(value, allowed=keys, required=keys, path=path)
        raw_ports = _wire_tuple(
            fields["input_ports"], f"{path}/input_ports", maximum=MAX_PORTS_PER_NODE
        )
        raw_dependencies = _wire_tuple(
            fields["dependencies"],
            f"{path}/dependencies",
            maximum=MAX_DEPENDENCIES_PER_NODE,
        )
        raw_references = _wire_tuple(
            fields["references"], f"{path}/references", maximum=MAX_REFERENCES_PER_NODE
        )
        raw_parameters = _wire_tuple(
            fields["parameter_bindings"],
            f"{path}/parameter_bindings",
            maximum=MAX_PARAMETERS_PER_NODE,
        )
        return cls(
            node_kind=_enum(fields["node_kind"], FeatureNodeKind, f"{path}/node_kind"),
            family_term_ref_id=fields["family_term_ref_id"],
            operation_term_ref_id=fields["operation_term_ref_id"],
            input_ports=tuple(
                FeatureInputPortV2.from_mapping(item, f"{path}/input_ports/{index}")
                for index, item in enumerate(raw_ports)
            ),
            dependencies=tuple(
                FeatureDependencyV2.from_mapping(item, f"{path}/dependencies/{index}")
                for index, item in enumerate(raw_dependencies)
            ),
            references=tuple(
                FeatureReferenceBindingV2.from_mapping(item, f"{path}/references/{index}")
                for index, item in enumerate(raw_references)
            ),
            parameter_bindings=tuple(
                FeatureParameterBindingV2.from_mapping(item, f"{path}/parameter_bindings/{index}")
                for index, item in enumerate(raw_parameters)
            ),
            extension_ids=tuple(
                _wire_tuple(
                    fields["extension_ids"],
                    f"{path}/extension_ids",
                    maximum=MAX_EXTENSIONS_PER_ELEMENT,
                )
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureNodeV2:
    node_id: str
    body_id: str
    name: str
    intent: FeatureIntentV2
    results: tuple[FeatureResultV2, ...]
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/node_id"))
        object.__setattr__(self, "body_id", _identifier(self.body_id, "/body_id"))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        if type(self.intent) is not FeatureIntentV2:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/intent")
        results = _tuple(
            self.results,
            "/results",
            item_type=FeatureResultV2,
            maximum=MAX_RESULTS_PER_NODE,
            minimum=1,
            key=lambda item: item.result_id,
        )
        object.__setattr__(self, "results", tuple(sorted(results, key=lambda item: item.result_id)))
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_EXTENSIONS_PER_ELEMENT,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "body_id": self.body_id,
            "name": self.name,
            "intent": self.intent.to_mapping(),
            "results": [item.to_mapping() for item in self.results],
            "extension_ids": list(self.extension_ids),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {"node_id", "body_id", "name", "intent", "results", "extension_ids"}
        fields = _fields(value, allowed=keys, required=keys, path=path)
        raw_results = _wire_tuple(
            fields["results"], f"{path}/results", maximum=MAX_RESULTS_PER_NODE
        )
        return cls(
            node_id=fields["node_id"],
            body_id=fields["body_id"],
            name=fields["name"],
            intent=FeatureIntentV2.from_mapping(fields["intent"], f"{path}/intent"),
            results=tuple(
                FeatureResultV2.from_mapping(item, f"{path}/results/{index}")
                for index, item in enumerate(raw_results)
            ),
            extension_ids=tuple(
                _wire_tuple(
                    fields["extension_ids"],
                    f"{path}/extension_ids",
                    maximum=MAX_EXTENSIONS_PER_ELEMENT,
                )
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureBodyV2:
    body_id: str
    name: str
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "body_id", _identifier(self.body_id, "/body_id"))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_EXTENSIONS_PER_ELEMENT,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "body_id": self.body_id,
            "name": self.name,
            "extension_ids": list(self.extension_ids),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {"body_id", "name", "extension_ids"}
        fields = _fields(value, allowed=keys, required=keys, path=path)
        return cls(
            body_id=fields["body_id"],
            name=fields["name"],
            extension_ids=tuple(
                _wire_tuple(
                    fields["extension_ids"],
                    f"{path}/extension_ids",
                    maximum=MAX_EXTENSIONS_PER_ELEMENT,
                )
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureGraphResultV2:
    selection_id: str
    node_id: str
    result_id: str

    def __post_init__(self) -> None:
        for field in ("selection_id", "node_id", "result_id"):
            object.__setattr__(self, field, _identifier(getattr(self, field), f"/{field}"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "selection_id": self.selection_id,
            "node_id": self.node_id,
            "result_id": self.result_id,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {"selection_id", "node_id", "result_id"}
        return cls(**_fields(value, allowed=keys, required=keys, path=path))


def _require_known(values: tuple[str, ...], known: set[str], path: str) -> None:
    if any(item not in known for item in values):
        _fail(ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE, path)


def _visit_acyclic(graph: dict[str, tuple[str, ...]], *, path: str) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visited:
            return
        if identifier in visiting:
            _fail(ParametricFeatureGraphErrorCode.CYCLE, path)
        visiting.add(identifier)
        for dependency in graph[identifier]:
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in graph:
        visit(identifier)


@dataclass(frozen=True, slots=True, kw_only=True)
class ParametricFeatureGraphV2:
    graph_id: str
    name: str
    terms: tuple[SemanticTermRefV2, ...]
    bodies: tuple[FeatureBodyV2, ...]
    parameters: tuple[DesignParameterV2, ...]
    references: tuple[SemanticReferenceV2, ...]
    nodes: tuple[FeatureNodeV2, ...]
    graph_results: tuple[FeatureGraphResultV2, ...]
    extensions: tuple[InertExtensionV2, ...] = ()
    authority: GraphAuthority = GraphAuthority.TRUSTED_ADAPTER_REQUIRED
    schema_version: int = PARAMETRIC_FEATURE_GRAPH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != PARAMETRIC_FEATURE_GRAPH_SCHEMA_VERSION
        ):
            _fail(ParametricFeatureGraphErrorCode.UNSUPPORTED_VERSION, "/schema_version")
        object.__setattr__(self, "graph_id", _identifier(self.graph_id, "/graph_id"))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        if self.authority is not GraphAuthority.TRUSTED_ADAPTER_REQUIRED:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/authority")

        terms = _tuple(
            self.terms,
            "/terms",
            item_type=SemanticTermRefV2,
            maximum=MAX_FEATURE_GRAPH_TERMS,
            minimum=1,
            key=lambda item: item.term_ref_id,
        )
        bodies = _tuple(
            self.bodies,
            "/bodies",
            item_type=FeatureBodyV2,
            maximum=MAX_FEATURE_GRAPH_BODIES,
            minimum=1,
            key=lambda item: item.body_id,
        )
        parameters = _tuple(
            self.parameters,
            "/parameters",
            item_type=DesignParameterV2,
            maximum=MAX_FEATURE_GRAPH_PARAMETERS,
            key=lambda item: item.parameter_id,
        )
        references = _tuple(
            self.references,
            "/references",
            item_type=SemanticReferenceV2,
            maximum=MAX_FEATURE_GRAPH_REFERENCES,
            key=lambda item: item.reference_id,
        )
        nodes = _tuple(
            self.nodes,
            "/nodes",
            item_type=FeatureNodeV2,
            maximum=MAX_FEATURE_GRAPH_NODES,
            minimum=1,
            key=lambda item: item.node_id,
        )
        graph_results = _tuple(
            self.graph_results,
            "/graph_results",
            item_type=FeatureGraphResultV2,
            maximum=MAX_RESULTS_PER_NODE,
            minimum=1,
            key=lambda item: item.selection_id,
        )
        extensions = _tuple(
            self.extensions,
            "/extensions",
            item_type=InertExtensionV2,
            maximum=MAX_FEATURE_GRAPH_EXTENSIONS,
            key=lambda item: item.extension_id,
        )

        object.__setattr__(self, "terms", tuple(sorted(terms, key=lambda item: item.term_ref_id)))
        object.__setattr__(self, "bodies", tuple(sorted(bodies, key=lambda item: item.body_id)))
        object.__setattr__(
            self,
            "parameters",
            tuple(sorted(parameters, key=lambda item: item.parameter_id)),
        )
        object.__setattr__(
            self,
            "references",
            tuple(sorted(references, key=lambda item: item.reference_id)),
        )
        object.__setattr__(self, "nodes", tuple(sorted(nodes, key=lambda item: item.node_id)))
        object.__setattr__(
            self,
            "graph_results",
            tuple(sorted(graph_results, key=lambda item: item.selection_id)),
        )
        object.__setattr__(
            self,
            "extensions",
            tuple(sorted(extensions, key=lambda item: item.extension_id)),
        )

        term_ids = {item.term_ref_id for item in terms}
        body_ids = {item.body_id for item in bodies}
        extension_ids = {item.extension_id for item in extensions}
        parameter_by_id = {item.parameter_id: item for item in parameters}
        reference_by_id = {item.reference_id: item for item in references}
        result_by_node = {
            node.node_id: {result.result_id: result for result in node.results} for node in nodes
        }

        all_result_ids = tuple(result.result_id for node in nodes for result in node.results)
        all_port_ids = tuple(port.port_id for node in nodes for port in node.intent.input_ports)
        all_binding_ids = tuple(
            [item.dependency_id for node in nodes for item in node.intent.dependencies]
            + [item.binding_id for node in nodes for item in node.intent.references]
            + [item.binding_id for node in nodes for item in node.intent.parameter_bindings]
        )
        value_ids = tuple(parameter.value.value_id for parameter in parameters)
        expression_ids = tuple(
            parameter.expression.expression_id
            for parameter in parameters
            if parameter.expression is not None
        )
        if any(
            len(set(values)) != len(values)
            for values in (all_result_ids, all_port_ids, all_binding_ids, value_ids, expression_ids)
        ):
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/identifiers")
        if {node.body_id for node in nodes} != body_ids:
            _fail(ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE, "/bodies")

        for index, body in enumerate(bodies):
            _require_known(body.extension_ids, extension_ids, f"/bodies/{index}/extension_ids")
        for index, extension in enumerate(extensions):
            _require_known(
                (extension.schema_term_ref_id,), term_ids, f"/extensions/{index}/schema_term_ref_id"
            )

        parameter_dependencies: dict[str, tuple[str, ...]] = {}
        for index, parameter in enumerate(parameters):
            value = parameter.value
            _require_known(
                (
                    parameter.semantic_role_term_ref_id,
                    value.value_type_term_ref_id,
                    value.encoding_term_ref_id,
                ),
                term_ids,
                f"/parameters/{index}/terms",
            )
            _require_known(
                (*parameter.extension_ids, *value.extension_ids),
                extension_ids,
                f"/parameters/{index}/extension_ids",
            )
            expression = parameter.expression
            if expression is None:
                parameter_dependencies[parameter.parameter_id] = ()
                continue
            _require_known(
                expression.extension_ids,
                extension_ids,
                f"/parameters/{index}/expression/extension_ids",
            )
            expression_by_id = {node.expression_node_id: node for node in expression.nodes}
            if set(expression_by_id) & set(parameter_by_id):
                _fail(
                    ParametricFeatureGraphErrorCode.INVALID_INPUT, f"/parameters/{index}/expression"
                )
            local_graph: dict[str, tuple[str, ...]] = {}
            external_parameters: set[str] = set()
            for node_index, expression_node in enumerate(expression.nodes):
                _require_known(
                    (
                        expression_node.operator_term_ref_id,
                        expression_node.result_type_term_ref_id,
                        *(
                            term_id
                            for item in expression_node.inputs
                            for term_id in (item.role_term_ref_id, item.value_type_term_ref_id)
                        ),
                    ),
                    term_ids,
                    f"/parameters/{index}/expression/nodes/{node_index}/terms",
                )
                _require_known(
                    expression_node.extension_ids,
                    extension_ids,
                    f"/parameters/{index}/expression/nodes/{node_index}/extension_ids",
                )
                local_dependencies: list[str] = []
                for input_index, expression_input in enumerate(expression_node.inputs):
                    source_expression = expression_by_id.get(expression_input.source_id)
                    source_parameter = parameter_by_id.get(expression_input.source_id)
                    if source_expression is not None:
                        source_type = source_expression.result_type_term_ref_id
                        local_dependencies.append(source_expression.expression_node_id)
                    elif source_parameter is not None:
                        source_type = source_parameter.value.value_type_term_ref_id
                        external_parameters.add(source_parameter.parameter_id)
                    else:
                        _fail(
                            ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE,
                            f"/parameters/{index}/expression/nodes/{node_index}/inputs/{input_index}/source_id",
                        )
                    if source_type != expression_input.value_type_term_ref_id:
                        _fail(
                            ParametricFeatureGraphErrorCode.INVALID_INPUT,
                            f"/parameters/{index}/expression/nodes/{node_index}/inputs/{input_index}/value_type_term_ref_id",
                        )
                local_graph[expression_node.expression_node_id] = tuple(local_dependencies)
            _visit_acyclic(local_graph, path=f"/parameters/{index}/expression/nodes")
            result_node = expression_by_id[expression.result_node_id]
            if result_node.result_type_term_ref_id != value.value_type_term_ref_id:
                _fail(
                    ParametricFeatureGraphErrorCode.INVALID_INPUT,
                    f"/parameters/{index}/expression/result_node_id",
                )
            parameter_dependencies[parameter.parameter_id] = tuple(sorted(external_parameters))
        _visit_acyclic(parameter_dependencies, path="/parameters")

        for index, reference in enumerate(references):
            _require_known(
                (
                    reference.semantic_role_term_ref_id,
                    reference.value_type_term_ref_id,
                    reference.locator_term_ref_id,
                    *reference.qualifier_term_ref_ids,
                ),
                term_ids,
                f"/references/{index}/terms",
            )
            _require_known(
                reference.extension_ids, extension_ids, f"/references/{index}/extension_ids"
            )
            if reference.scope is SemanticReferenceScope.FEATURE:
                source_results = result_by_node.get(reference.source_node_id or "")
                source = (
                    None
                    if source_results is None
                    else source_results.get(reference.source_geometry_id or "")
                )
                if source is None:
                    _fail(
                        ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE,
                        f"/references/{index}/source_geometry_id",
                    )
                if source.value_type_term_ref_id != reference.value_type_term_ref_id:
                    _fail(
                        ParametricFeatureGraphErrorCode.INVALID_INPUT,
                        f"/references/{index}/value_type_term_ref_id",
                    )
            for step_index, step in enumerate(reference.occurrence_path):
                source = result_by_node.get(step.transform_node_id, {}).get(
                    step.transform_result_id
                )
                if source is None:
                    _fail(
                        ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE,
                        f"/references/{index}/occurrence_path/{step_index}/transform_result_id",
                    )

        dependency_graph: dict[str, tuple[str, ...]] = {}
        for index, node in enumerate(nodes):
            intent = node.intent
            port_by_id = {item.port_id: item for item in intent.input_ports}
            term_refs = [intent.family_term_ref_id, intent.operation_term_ref_id]
            term_refs.extend(
                term_id
                for port in intent.input_ports
                for term_id in (port.semantic_role_term_ref_id, port.value_type_term_ref_id)
            )
            term_refs.extend(
                term_id
                for result in node.results
                for term_id in (result.semantic_role_term_ref_id, result.value_type_term_ref_id)
            )
            _require_known(tuple(term_refs), term_ids, f"/nodes/{index}/terms")
            _require_known(
                (
                    *intent.extension_ids,
                    *node.extension_ids,
                    *(extension for port in intent.input_ports for extension in port.extension_ids),
                    *(extension for result in node.results for extension in result.extension_ids),
                ),
                extension_ids,
                f"/nodes/{index}/extension_ids",
            )
            upstream_ids: list[str] = []
            for dependency_index, dependency in enumerate(intent.dependencies):
                source = result_by_node.get(dependency.upstream_node_id, {}).get(
                    dependency.upstream_result_id
                )
                port = port_by_id[dependency.port_id]
                if source is None:
                    _fail(
                        ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE,
                        f"/nodes/{index}/intent/dependencies/{dependency_index}/upstream_result_id",
                    )
                if source.value_type_term_ref_id != port.value_type_term_ref_id:
                    _fail(
                        ParametricFeatureGraphErrorCode.INVALID_INPUT,
                        f"/nodes/{index}/intent/dependencies/{dependency_index}/port_id",
                    )
                upstream_ids.append(dependency.upstream_node_id)
            dependency_graph[node.node_id] = tuple(upstream_ids)
            for binding_index, binding in enumerate(intent.references):
                reference = reference_by_id.get(binding.reference_id)
                if reference is None:
                    _fail(
                        ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE,
                        f"/nodes/{index}/intent/references/{binding_index}/reference_id",
                    )
                if (
                    reference.value_type_term_ref_id
                    != port_by_id[binding.port_id].value_type_term_ref_id
                ):
                    _fail(
                        ParametricFeatureGraphErrorCode.INVALID_INPUT,
                        f"/nodes/{index}/intent/references/{binding_index}/port_id",
                    )
            for binding_index, binding in enumerate(intent.parameter_bindings):
                parameter = parameter_by_id.get(binding.parameter_id)
                if parameter is None:
                    _fail(
                        ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE,
                        f"/nodes/{index}/intent/parameter_bindings/{binding_index}/parameter_id",
                    )
                if (
                    parameter.value.value_type_term_ref_id
                    != port_by_id[binding.port_id].value_type_term_ref_id
                ):
                    _fail(
                        ParametricFeatureGraphErrorCode.INVALID_INPUT,
                        f"/nodes/{index}/intent/parameter_bindings/{binding_index}/port_id",
                    )
        _visit_acyclic(dependency_graph, path="/nodes")

        closure_cache: dict[str, frozenset[str]] = {}

        def upstream_closure(node_id: str) -> frozenset[str]:
            cached = closure_cache.get(node_id)
            if cached is not None:
                return cached
            result = set(dependency_graph[node_id])
            for upstream_id in dependency_graph[node_id]:
                result.update(upstream_closure(upstream_id))
            frozen = frozenset(result)
            closure_cache[node_id] = frozen
            return frozen

        for index, node in enumerate(nodes):
            available = upstream_closure(node.node_id)
            for binding_index, binding in enumerate(node.intent.references):
                reference = reference_by_id[binding.reference_id]
                source_ids = {
                    item
                    for item in (
                        reference.source_node_id,
                        *(step.transform_node_id for step in reference.occurrence_path),
                    )
                    if item is not None
                }
                if not source_ids <= available:
                    _fail(
                        ParametricFeatureGraphErrorCode.INVALID_ORDER,
                        f"/nodes/{index}/intent/references/{binding_index}/reference_id",
                    )

        for index, selection in enumerate(graph_results):
            if selection.result_id not in result_by_node.get(selection.node_id, {}):
                _fail(
                    ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE,
                    f"/graph_results/{index}/result_id",
                )

        _canonical_json(self.to_mapping())

    @property
    def executable(self) -> bool:
        return False

    @property
    def adapter_binding_required(self) -> bool:
        return True

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "graph_id": self.graph_id,
            "name": self.name,
            "authority": self.authority.value,
            "terms": [item.to_mapping() for item in self.terms],
            "bodies": [item.to_mapping() for item in self.bodies],
            "parameters": [item.to_mapping() for item in self.parameters],
            "references": [item.to_mapping() for item in self.references],
            "nodes": [item.to_mapping() for item in self.nodes],
            "graph_results": [item.to_mapping() for item in self.graph_results],
            "extensions": [item.to_mapping() for item in self.extensions],
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @property
    def graph_sha256(self) -> str:
        return hashlib.sha256(_GRAPH_DIGEST_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        keys = {
            "schema_version",
            "graph_id",
            "name",
            "authority",
            "terms",
            "bodies",
            "parameters",
            "references",
            "nodes",
            "graph_results",
            "extensions",
        }
        fields = _fields(value, allowed=keys, required=keys, path="")
        if fields["schema_version"] != PARAMETRIC_FEATURE_GRAPH_SCHEMA_VERSION:
            _fail(ParametricFeatureGraphErrorCode.UNSUPPORTED_VERSION, "/schema_version")
        raw_terms = _wire_tuple(fields["terms"], "/terms", maximum=MAX_FEATURE_GRAPH_TERMS)
        raw_bodies = _wire_tuple(fields["bodies"], "/bodies", maximum=MAX_FEATURE_GRAPH_BODIES)
        raw_parameters = _wire_tuple(
            fields["parameters"], "/parameters", maximum=MAX_FEATURE_GRAPH_PARAMETERS
        )
        raw_references = _wire_tuple(
            fields["references"], "/references", maximum=MAX_FEATURE_GRAPH_REFERENCES
        )
        raw_nodes = _wire_tuple(fields["nodes"], "/nodes", maximum=MAX_FEATURE_GRAPH_NODES)
        raw_graph_results = _wire_tuple(
            fields["graph_results"], "/graph_results", maximum=MAX_RESULTS_PER_NODE
        )
        raw_extensions = _wire_tuple(
            fields["extensions"], "/extensions", maximum=MAX_FEATURE_GRAPH_EXTENSIONS
        )
        return cls(
            schema_version=fields["schema_version"],
            graph_id=fields["graph_id"],
            name=fields["name"],
            authority=_enum(fields["authority"], GraphAuthority, "/authority"),
            terms=tuple(
                SemanticTermRefV2.from_mapping(item, f"/terms/{index}")
                for index, item in enumerate(raw_terms)
            ),
            bodies=tuple(
                FeatureBodyV2.from_mapping(item, f"/bodies/{index}")
                for index, item in enumerate(raw_bodies)
            ),
            parameters=tuple(
                DesignParameterV2.from_mapping(item, f"/parameters/{index}")
                for index, item in enumerate(raw_parameters)
            ),
            references=tuple(
                SemanticReferenceV2.from_mapping(item, f"/references/{index}")
                for index, item in enumerate(raw_references)
            ),
            nodes=tuple(
                FeatureNodeV2.from_mapping(item, f"/nodes/{index}")
                for index, item in enumerate(raw_nodes)
            ),
            graph_results=tuple(
                FeatureGraphResultV2.from_mapping(item, f"/graph_results/{index}")
                for index, item in enumerate(raw_graph_results)
            ),
            extensions=tuple(
                InertExtensionV2.from_mapping(item, f"/extensions/{index}")
                for index, item in enumerate(raw_extensions)
            ),
        )


def encode_parametric_feature_graph_v2(value: object) -> bytes:
    if type(value) is not ParametricFeatureGraphV2:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT)
    return value.canonical_bytes


def decode_parametric_feature_graph_v2(
    raw: object,
    *,
    expected_sha256: str | None = None,
) -> ParametricFeatureGraphV2:
    if expected_sha256 is not None:
        expected_sha256 = _digest(expected_sha256, "/expected_sha256")
    mapping = _decode_json(raw)
    result = ParametricFeatureGraphV2.from_mapping(mapping)
    if expected_sha256 is not None and not hmac.compare_digest(
        result.graph_sha256,
        expected_sha256,
    ):
        _fail(ParametricFeatureGraphErrorCode.INTEGRITY_FAILURE)
    return result


__all__ = [
    "DesignParameterV2",
    "ExpressionInputV2",
    "ExpressionNodeV2",
    "ExtensionDisposition",
    "FeatureBodyV2",
    "FeatureDependencyV2",
    "FeatureGraphResultV2",
    "FeatureInputPortV2",
    "FeatureIntentV2",
    "FeatureNodeKind",
    "FeatureNodeV2",
    "FeatureParameterBindingV2",
    "FeatureReferenceBindingV2",
    "FeatureResultV2",
    "GraphAuthority",
    "InertExtensionV2",
    "MAX_BINDINGS_PER_PORT",
    "MAX_DEPENDENCIES_PER_NODE",
    "MAX_EXPRESSION_INPUTS_PER_NODE",
    "MAX_EXPRESSION_NODES",
    "MAX_EXTENSIONS_PER_ELEMENT",
    "MAX_FEATURE_GRAPH_BODIES",
    "MAX_FEATURE_GRAPH_EXTENSIONS",
    "MAX_FEATURE_GRAPH_NODES",
    "MAX_FEATURE_GRAPH_PARAMETERS",
    "MAX_FEATURE_GRAPH_REFERENCES",
    "MAX_FEATURE_GRAPH_TERMS",
    "MAX_OCCURRENCE_PATH_STEPS",
    "MAX_PARAMETERS_PER_NODE",
    "MAX_PARAMETRIC_FEATURE_GRAPH_BYTES",
    "MAX_PORTS_PER_NODE",
    "MAX_REFERENCES_PER_NODE",
    "MAX_REFERENCE_QUALIFIERS",
    "MAX_RESULTS_PER_NODE",
    "MAX_TYPED_VALUE_BYTES",
    "MAX_TYPED_VALUE_DEPTH",
    "MAX_TYPED_VALUE_NODES",
    "OccurrencePathStepV2",
    "PARAMETRIC_FEATURE_GRAPH_SCHEMA_VERSION",
    "ParametricFeatureGraphError",
    "ParametricFeatureGraphErrorCode",
    "ParametricFeatureGraphV2",
    "SemanticReferenceScope",
    "SemanticReferenceV2",
    "SemanticTermRefV2",
    "TermBoundExpressionV2",
    "TermTypedValueV2",
    "decode_parametric_feature_graph_v2",
    "encode_parametric_feature_graph_v2",
]
