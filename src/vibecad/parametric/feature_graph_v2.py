"""Backend-neutral parametric feature graph contract.

The graph describes design intent, not an executable program.  Feature
families are a closed tagged union while operation, port, result, and topology
semantics are content-bound ontology terms.  A trusted adapter must explicitly
bind those terms before execution.  Namespaced extensions remain immutable,
inert content references and cannot select code, handlers, or backends.
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

MAX_FEATURE_GRAPH_TERMS = 256
MAX_FEATURE_GRAPH_BODIES = 32
MAX_FEATURE_GRAPH_PARAMETERS = 256
MAX_FEATURE_GRAPH_REFERENCES = 512
MAX_FEATURE_GRAPH_NODES = 128
MAX_FEATURE_GRAPH_EXTENSIONS = 64
MAX_DEPENDENCIES_PER_NODE = 32
MAX_REFERENCES_PER_NODE = 64
MAX_PARAMETERS_PER_NODE = 64
MAX_RESULTS_PER_NODE = 16
MAX_EXTENSIONS_PER_ELEMENT = 16
MAX_PARAMETER_EXPRESSION_TERMS = 32
MAX_REFERENCE_QUALIFIERS = 16
MAX_OCCURRENCE_PATH_STEPS = 16
MAX_ENUM_PARAMETER_VALUES = 128

_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_ABS_NUMBER = 1.0e15
_MAX_TEXT_BYTES = 256
_MAX_ERROR_PATH_BYTES = 384
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 32_768
_GRAPH_DIGEST_DOMAIN = b"vibecad-parametric-feature-graph-v2\0"

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
    """Bounded contract error which never reflects rejected input values."""

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


def _integer(value: object, path: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_SAFE_INTEGER:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    return value


def _signed_integer(value: object, path: str) -> int:
    if type(value) is not int or abs(value) > _MAX_SAFE_INTEGER:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    return value


def _number(value: object, path: str) -> float:
    if type(value) not in {int, float}:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    try:
        result = float(value)
    except (OverflowError, ValueError):
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    if not math.isfinite(result) or abs(result) > _MAX_ABS_NUMBER:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    return 0.0 if result == 0.0 else result


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
    if type(value) is not dict:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
    if any(type(key) is not str for key in value):
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


def _json_tree(value: object, path: str, *, depth: int, remaining: list[int]) -> None:
    remaining[0] -= 1
    if remaining[0] < 0 or depth > _MAX_JSON_DEPTH:
        _fail(ParametricFeatureGraphErrorCode.BUDGET_EXCEEDED, path)
    if value is None or type(value) in {bool, str}:
        return
    if type(value) is int:
        if abs(value) > _MAX_SAFE_INTEGER:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _json_tree(item, f"{path}/{index}", depth=depth + 1, remaining=remaining)
        return
    if type(value) is dict:
        for item in value.values():
            _json_tree(item, f"{path}/field", depth=depth + 1, remaining=remaining)
        return
    _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)


def _canonical_json(value: object) -> bytes:
    _json_tree(value, "", depth=0, remaining=[_MAX_JSON_NODES])
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
    if not raw or len(raw) > MAX_PARAMETRIC_FEATURE_GRAPH_BYTES:
        _fail(ParametricFeatureGraphErrorCode.BUDGET_EXCEEDED)
    return raw


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT)
        result[key] = value
    return result


def _decode_json(raw: object) -> object:
    if type(raw) is not bytes or not raw or len(raw) > MAX_PARAMETRIC_FEATURE_GRAPH_BYTES:
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT)
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=lambda _: _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT),
        )
    except ParametricFeatureGraphError:
        raise
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError, RecursionError):
        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT)
    if _canonical_json(value) != raw:
        _fail(ParametricFeatureGraphErrorCode.INTEGRITY_FAILURE)
    return value


class FeatureFamily(StrEnum):
    EXTRUSION = "extrusion"
    REVOLUTION = "revolution"
    LOFT = "loft"
    SWEEP = "sweep"
    HELIX = "helix"
    PRIMITIVE = "primitive"
    HOLE = "hole"
    TRANSFORM = "transform"
    DRESSUP = "dressup"
    BOOLEAN = "boolean"
    REFERENCE = "reference"


class ParameterValueKind(StrEnum):
    SCALAR = "scalar"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    ENUM = "enum"


class SemanticElementKind(StrEnum):
    BODY = "body"
    FEATURE = "feature"
    SKETCH = "sketch"
    POINT = "point"
    AXIS = "axis"
    PLANE = "plane"
    VERTEX = "vertex"
    EDGE = "edge"
    WIRE = "wire"
    FACE = "face"
    SHELL = "shell"
    SOLID = "solid"
    COMPSOLID = "compsolid"
    COMPOUND = "compound"
    CURVE = "curve"
    SURFACE = "surface"
    COORDINATE_SYSTEM = "coordinate_system"


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
        fields = _fields(
            value,
            allowed={
                "term_ref_id",
                "namespace",
                "vocabulary_version",
                "term_id",
                "term_definition_sha256",
            },
            required={
                "term_ref_id",
                "namespace",
                "vocabulary_version",
                "term_id",
                "term_definition_sha256",
            },
            path=path,
        )
        return cls(**fields)


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
class ParameterExpressionTermV2:
    parameter_id: str
    coefficient: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameter_id", _identifier(self.parameter_id, "/parameter_id"))
        coefficient = _number(self.coefficient, "/coefficient")
        if coefficient == 0.0:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/coefficient")
        object.__setattr__(self, "coefficient", coefficient)

    def to_mapping(self) -> dict[str, object]:
        return {"parameter_id": self.parameter_id, "coefficient": self.coefficient}

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        fields = _fields(
            value,
            allowed={"parameter_id", "coefficient"},
            required={"parameter_id", "coefficient"},
            path=path,
        )
        return cls(**fields)


@dataclass(frozen=True, slots=True, kw_only=True)
class AffineParameterExpressionV2:
    terms: tuple[ParameterExpressionTermV2, ...]
    constant: float = 0.0

    def __post_init__(self) -> None:
        terms = _tuple(
            self.terms,
            "/terms",
            item_type=ParameterExpressionTermV2,
            maximum=MAX_PARAMETER_EXPRESSION_TERMS,
            minimum=1,
            key=lambda item: item.parameter_id,
        )
        object.__setattr__(self, "terms", tuple(sorted(terms, key=lambda item: item.parameter_id)))
        object.__setattr__(self, "constant", _number(self.constant, "/constant"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "terms": [item.to_mapping() for item in self.terms],
            "constant": self.constant,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        fields = _fields(
            value,
            allowed={"terms", "constant"},
            required={"terms", "constant"},
            path=path,
        )
        raw_terms = _wire_tuple(
            fields["terms"], f"{path}/terms", maximum=MAX_PARAMETER_EXPRESSION_TERMS
        )
        return cls(
            terms=tuple(
                ParameterExpressionTermV2.from_mapping(item, f"{path}/terms/{index}")
                for index, item in enumerate(raw_terms)
            ),
            constant=fields["constant"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DesignParameterV2:
    parameter_id: str
    name: str
    semantic_role_term_ref_id: str
    value_kind: ParameterValueKind
    value: float | int | bool | str
    unit_term_ref_id: str | None = None
    minimum: float | int | None = None
    maximum: float | int | None = None
    allowed_value_term_ref_ids: tuple[str, ...] = ()
    expression: AffineParameterExpressionV2 | None = None
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameter_id", _identifier(self.parameter_id, "/parameter_id"))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        object.__setattr__(
            self,
            "semantic_role_term_ref_id",
            _identifier(self.semantic_role_term_ref_id, "/semantic_role_term_ref_id"),
        )
        kind = _enum(self.value_kind, ParameterValueKind, "/value_kind")
        object.__setattr__(self, "value_kind", kind)
        if self.unit_term_ref_id is not None:
            object.__setattr__(
                self,
                "unit_term_ref_id",
                _identifier(self.unit_term_ref_id, "/unit_term_ref_id"),
            )
        allowed = _identifier_tuple(
            self.allowed_value_term_ref_ids,
            "/allowed_value_term_ref_ids",
            maximum=MAX_ENUM_PARAMETER_VALUES,
        )
        object.__setattr__(self, "allowed_value_term_ref_ids", allowed)
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_EXTENSIONS_PER_ELEMENT,
            ),
        )

        if kind is ParameterValueKind.SCALAR:
            value = _number(self.value, "/value")
        elif kind is ParameterValueKind.INTEGER:
            value = _signed_integer(self.value, "/value")
        elif kind is ParameterValueKind.BOOLEAN:
            if type(self.value) is not bool:
                _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/value")
            value = self.value
        else:
            value = _identifier(self.value, "/value")
        object.__setattr__(self, "value", value)

        numeric = kind in {ParameterValueKind.SCALAR, ParameterValueKind.INTEGER}
        if not numeric and (
            self.unit_term_ref_id is not None
            or self.minimum is not None
            or self.maximum is not None
            or self.expression is not None
        ):
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/value_kind")
        if kind is ParameterValueKind.ENUM:
            if not allowed or value not in allowed:
                _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/allowed_value_term_ref_ids")
        elif allowed:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/allowed_value_term_ref_ids")
        if self.expression is not None and (
            kind is not ParameterValueKind.SCALAR
            or type(self.expression) is not AffineParameterExpressionV2
        ):
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/expression")
        number_validator = _signed_integer if kind is ParameterValueKind.INTEGER else _number
        minimum = None if self.minimum is None else number_validator(self.minimum, "/minimum")
        maximum = None if self.maximum is None else number_validator(self.maximum, "/maximum")
        if numeric and (
            (minimum is not None and float(value) < minimum)
            or (maximum is not None and float(value) > maximum)
            or (minimum is not None and maximum is not None and minimum > maximum)
        ):
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/value")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)

    def to_mapping(self) -> dict[str, object]:
        return {
            "parameter_id": self.parameter_id,
            "name": self.name,
            "semantic_role_term_ref_id": self.semantic_role_term_ref_id,
            "value_kind": self.value_kind.value,
            "value": self.value,
            "unit_term_ref_id": self.unit_term_ref_id,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "allowed_value_term_ref_ids": list(self.allowed_value_term_ref_ids),
            "expression": None if self.expression is None else self.expression.to_mapping(),
            "extension_ids": list(self.extension_ids),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {
            "parameter_id",
            "name",
            "semantic_role_term_ref_id",
            "value_kind",
            "value",
            "unit_term_ref_id",
            "minimum",
            "maximum",
            "allowed_value_term_ref_ids",
            "expression",
            "extension_ids",
        }
        fields = _fields(value, allowed=keys, required=keys, path=path)
        expression = fields["expression"]
        return cls(
            parameter_id=fields["parameter_id"],
            name=fields["name"],
            semantic_role_term_ref_id=fields["semantic_role_term_ref_id"],
            value_kind=_enum(fields["value_kind"], ParameterValueKind, f"{path}/value_kind"),
            value=fields["value"],
            unit_term_ref_id=fields["unit_term_ref_id"],
            minimum=fields["minimum"],
            maximum=fields["maximum"],
            allowed_value_term_ref_ids=tuple(
                _wire_tuple(
                    fields["allowed_value_term_ref_ids"],
                    f"{path}/allowed_value_term_ref_ids",
                    maximum=MAX_ENUM_PARAMETER_VALUES,
                )
            ),
            expression=(
                None
                if expression is None
                else AffineParameterExpressionV2.from_mapping(expression, f"{path}/expression")
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
    occurrence_index: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "transform_node_id",
            _identifier(self.transform_node_id, "/transform_node_id"),
        )
        object.__setattr__(
            self,
            "occurrence_index",
            _integer(self.occurrence_index, "/occurrence_index"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "transform_node_id": self.transform_node_id,
            "occurrence_index": self.occurrence_index,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        fields = _fields(
            value,
            allowed={"transform_node_id", "occurrence_index"},
            required={"transform_node_id", "occurrence_index"},
            path=path,
        )
        return cls(**fields)


@dataclass(frozen=True, slots=True, kw_only=True)
class SemanticReferenceV2:
    reference_id: str
    scope: SemanticReferenceScope
    element_kind: SemanticElementKind
    semantic_role_term_ref_id: str
    source_node_id: str | None = None
    source_content_sha256: str | None = None
    source_geometry_id: str | None = None
    occurrence_path: tuple[OccurrencePathStepV2, ...] = ()
    qualifier_term_ref_ids: tuple[str, ...] = ()
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference_id", _identifier(self.reference_id, "/reference_id"))
        scope = _enum(self.scope, SemanticReferenceScope, "/scope")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(
            self,
            "element_kind",
            _enum(self.element_kind, SemanticElementKind, "/element_kind"),
        )
        object.__setattr__(
            self,
            "semantic_role_term_ref_id",
            _identifier(self.semantic_role_term_ref_id, "/semantic_role_term_ref_id"),
        )
        if self.source_node_id is not None:
            object.__setattr__(
                self,
                "source_node_id",
                _identifier(self.source_node_id, "/source_node_id"),
            )
        if self.source_content_sha256 is not None:
            object.__setattr__(
                self,
                "source_content_sha256",
                _digest(self.source_content_sha256, "/source_content_sha256"),
            )
        if self.source_geometry_id is not None:
            object.__setattr__(
                self,
                "source_geometry_id",
                _identifier(self.source_geometry_id, "/source_geometry_id"),
            )
        if scope is SemanticReferenceScope.ORIGIN:
            if any(
                value is not None
                for value in (
                    self.source_node_id,
                    self.source_content_sha256,
                    self.source_geometry_id,
                )
            ):
                _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/scope")
        elif scope is SemanticReferenceScope.FEATURE:
            if self.source_node_id is None or self.source_content_sha256 is not None:
                _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/source_node_id")
        elif self.source_node_id is not None or self.source_content_sha256 is None:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/source_content_sha256")
        path = _tuple(
            self.occurrence_path,
            "/occurrence_path",
            item_type=OccurrencePathStepV2,
            maximum=MAX_OCCURRENCE_PATH_STEPS,
            key=lambda item: item.transform_node_id,
        )
        object.__setattr__(self, "occurrence_path", path)
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
            "element_kind": self.element_kind.value,
            "semantic_role_term_ref_id": self.semantic_role_term_ref_id,
            "source_node_id": self.source_node_id,
            "source_content_sha256": self.source_content_sha256,
            "source_geometry_id": self.source_geometry_id,
            "occurrence_path": [item.to_mapping() for item in self.occurrence_path],
            "qualifier_term_ref_ids": list(self.qualifier_term_ref_ids),
            "extension_ids": list(self.extension_ids),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {
            "reference_id",
            "scope",
            "element_kind",
            "semantic_role_term_ref_id",
            "source_node_id",
            "source_content_sha256",
            "source_geometry_id",
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
            element_kind=_enum(fields["element_kind"], SemanticElementKind, f"{path}/element_kind"),
            semantic_role_term_ref_id=fields["semantic_role_term_ref_id"],
            source_node_id=fields["source_node_id"],
            source_content_sha256=fields["source_content_sha256"],
            source_geometry_id=fields["source_geometry_id"],
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
class FeatureDependencyV2:
    dependency_id: str
    role_term_ref_id: str
    upstream_node_id: str
    ordinal: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "dependency_id", _identifier(self.dependency_id, "/dependency_id"))
        object.__setattr__(
            self,
            "role_term_ref_id",
            _identifier(self.role_term_ref_id, "/role_term_ref_id"),
        )
        object.__setattr__(
            self,
            "upstream_node_id",
            _identifier(self.upstream_node_id, "/upstream_node_id"),
        )
        object.__setattr__(self, "ordinal", _integer(self.ordinal, "/ordinal"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "dependency_id": self.dependency_id,
            "role_term_ref_id": self.role_term_ref_id,
            "upstream_node_id": self.upstream_node_id,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        fields = _fields(
            value,
            allowed={"dependency_id", "role_term_ref_id", "upstream_node_id", "ordinal"},
            required={"dependency_id", "role_term_ref_id", "upstream_node_id", "ordinal"},
            path=path,
        )
        return cls(**fields)


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureReferenceBindingV2:
    binding_id: str
    role_term_ref_id: str
    reference_id: str
    ordinal: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "binding_id", _identifier(self.binding_id, "/binding_id"))
        object.__setattr__(
            self,
            "role_term_ref_id",
            _identifier(self.role_term_ref_id, "/role_term_ref_id"),
        )
        object.__setattr__(self, "reference_id", _identifier(self.reference_id, "/reference_id"))
        object.__setattr__(self, "ordinal", _integer(self.ordinal, "/ordinal"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "role_term_ref_id": self.role_term_ref_id,
            "reference_id": self.reference_id,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        fields = _fields(
            value,
            allowed={"binding_id", "role_term_ref_id", "reference_id", "ordinal"},
            required={"binding_id", "role_term_ref_id", "reference_id", "ordinal"},
            path=path,
        )
        return cls(**fields)


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureParameterBindingV2:
    binding_id: str
    role_term_ref_id: str
    parameter_id: str
    ordinal: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "binding_id", _identifier(self.binding_id, "/binding_id"))
        object.__setattr__(
            self,
            "role_term_ref_id",
            _identifier(self.role_term_ref_id, "/role_term_ref_id"),
        )
        object.__setattr__(self, "parameter_id", _identifier(self.parameter_id, "/parameter_id"))
        object.__setattr__(self, "ordinal", _integer(self.ordinal, "/ordinal"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "role_term_ref_id": self.role_term_ref_id,
            "parameter_id": self.parameter_id,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        fields = _fields(
            value,
            allowed={"binding_id", "role_term_ref_id", "parameter_id", "ordinal"},
            required={"binding_id", "role_term_ref_id", "parameter_id", "ordinal"},
            path=path,
        )
        return cls(**fields)


_FAMILY_MINIMUMS: dict[FeatureFamily, tuple[int, int]] = {
    FeatureFamily.EXTRUSION: (0, 1),
    FeatureFamily.REVOLUTION: (0, 2),
    FeatureFamily.LOFT: (0, 2),
    FeatureFamily.SWEEP: (0, 2),
    FeatureFamily.HELIX: (0, 2),
    FeatureFamily.PRIMITIVE: (0, 0),
    FeatureFamily.HOLE: (1, 1),
    FeatureFamily.TRANSFORM: (1, 0),
    FeatureFamily.DRESSUP: (1, 1),
    FeatureFamily.BOOLEAN: (2, 0),
    FeatureFamily.REFERENCE: (0, 0),
}


def _port_order(item: object) -> tuple[str, int, str]:
    return (
        item.role_term_ref_id,
        item.ordinal,
        (  # type: ignore[attr-defined]
            item.dependency_id if type(item) is FeatureDependencyV2 else item.binding_id  # type: ignore[attr-defined]
        ),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureIntentV2:
    family: FeatureFamily
    operation_term_ref_id: str
    dependencies: tuple[FeatureDependencyV2, ...] = ()
    references: tuple[FeatureReferenceBindingV2, ...] = ()
    parameter_bindings: tuple[FeatureParameterBindingV2, ...] = ()
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        family = _enum(self.family, FeatureFamily, "/family")
        object.__setattr__(self, "family", family)
        object.__setattr__(
            self,
            "operation_term_ref_id",
            _identifier(self.operation_term_ref_id, "/operation_term_ref_id"),
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
        minimum_dependencies, minimum_references = _FAMILY_MINIMUMS[family]
        if len(dependencies) < minimum_dependencies:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/dependencies")
        if len(references) < minimum_references:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/references")
        all_port_ids = tuple(
            [item.dependency_id for item in dependencies]
            + [item.binding_id for item in references]
            + [item.binding_id for item in parameters]
        )
        if len(set(all_port_ids)) != len(all_port_ids):
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/bindings")
        for path, values in (
            ("/dependencies", dependencies),
            ("/references", references),
            ("/parameter_bindings", parameters),
        ):
            role_slots = tuple((item.role_term_ref_id, item.ordinal) for item in values)
            if len(set(role_slots)) != len(role_slots):
                _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, path)
        if family is FeatureFamily.BOOLEAN and len(
            {item.upstream_node_id for item in dependencies}
        ) != len(dependencies):
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/dependencies")
        object.__setattr__(self, "dependencies", tuple(sorted(dependencies, key=_port_order)))
        object.__setattr__(self, "references", tuple(sorted(references, key=_port_order)))
        object.__setattr__(
            self,
            "parameter_bindings",
            tuple(sorted(parameters, key=_port_order)),
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
            "family": self.family.value,
            "operation_term_ref_id": self.operation_term_ref_id,
            "dependencies": [item.to_mapping() for item in self.dependencies],
            "references": [item.to_mapping() for item in self.references],
            "parameter_bindings": [item.to_mapping() for item in self.parameter_bindings],
            "extension_ids": list(self.extension_ids),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {
            "family",
            "operation_term_ref_id",
            "dependencies",
            "references",
            "parameter_bindings",
            "extension_ids",
        }
        fields = _fields(value, allowed=keys, required=keys, path=path)
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
            family=_enum(fields["family"], FeatureFamily, f"{path}/family"),
            operation_term_ref_id=fields["operation_term_ref_id"],
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
    result_term_ref_ids: tuple[str, ...]
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/node_id"))
        object.__setattr__(self, "body_id", _identifier(self.body_id, "/body_id"))
        object.__setattr__(self, "name", _text(self.name, "/name"))
        if type(self.intent) is not FeatureIntentV2:
            _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/intent")
        object.__setattr__(
            self,
            "result_term_ref_ids",
            _identifier_tuple(
                self.result_term_ref_ids,
                "/result_term_ref_ids",
                maximum=MAX_RESULTS_PER_NODE,
                minimum=1,
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
            "node_id": self.node_id,
            "body_id": self.body_id,
            "name": self.name,
            "intent": self.intent.to_mapping(),
            "result_term_ref_ids": list(self.result_term_ref_ids),
            "extension_ids": list(self.extension_ids),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "") -> Self:
        keys = {
            "node_id",
            "body_id",
            "name",
            "intent",
            "result_term_ref_ids",
            "extension_ids",
        }
        fields = _fields(value, allowed=keys, required=keys, path=path)
        return cls(
            node_id=fields["node_id"],
            body_id=fields["body_id"],
            name=fields["name"],
            intent=FeatureIntentV2.from_mapping(fields["intent"], f"{path}/intent"),
            result_term_ref_ids=tuple(
                _wire_tuple(
                    fields["result_term_ref_ids"],
                    f"{path}/result_term_ref_ids",
                    maximum=MAX_RESULTS_PER_NODE,
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
        fields = _fields(
            value,
            allowed={"body_id", "name", "extension_ids"},
            required={"body_id", "name", "extension_ids"},
            path=path,
        )
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


def _require_known(values: tuple[str, ...], known: set[str], path: str) -> None:
    if any(item not in known for item in values):
        _fail(ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE, path)


@dataclass(frozen=True, slots=True, kw_only=True)
class ParametricFeatureGraphV2:
    graph_id: str
    name: str
    terms: tuple[SemanticTermRefV2, ...]
    bodies: tuple[FeatureBodyV2, ...]
    parameters: tuple[DesignParameterV2, ...]
    references: tuple[SemanticReferenceV2, ...]
    nodes: tuple[FeatureNodeV2, ...]
    result_node_ids: tuple[str, ...]
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
        extensions = _tuple(
            self.extensions,
            "/extensions",
            item_type=InertExtensionV2,
            maximum=MAX_FEATURE_GRAPH_EXTENSIONS,
            key=lambda item: item.extension_id,
        )
        results = _identifier_tuple(
            self.result_node_ids,
            "/result_node_ids",
            maximum=MAX_FEATURE_GRAPH_NODES,
            minimum=1,
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
            "extensions",
            tuple(sorted(extensions, key=lambda item: item.extension_id)),
        )
        object.__setattr__(self, "result_node_ids", results)

        term_ids = {item.term_ref_id for item in terms}
        body_ids = {item.body_id for item in bodies}
        parameter_by_id = {item.parameter_id: item for item in parameters}
        reference_by_id = {item.reference_id: item for item in references}
        node_by_id = {item.node_id: item for item in nodes}
        extension_ids = {item.extension_id for item in extensions}
        _require_known(results, set(node_by_id), "/result_node_ids")

        for index, body in enumerate(bodies):
            _require_known(body.extension_ids, extension_ids, f"/bodies/{index}/extension_ids")
        if {node.body_id for node in nodes} != body_ids:
            _fail(ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE, "/bodies")

        for index, extension in enumerate(extensions):
            _require_known(
                (extension.schema_term_ref_id,), term_ids, f"/extensions/{index}/schema_term_ref_id"
            )
        for index, parameter in enumerate(parameters):
            term_refs = [parameter.semantic_role_term_ref_id]
            if parameter.unit_term_ref_id is not None:
                term_refs.append(parameter.unit_term_ref_id)
            term_refs.extend(parameter.allowed_value_term_ref_ids)
            _require_known(tuple(term_refs), term_ids, f"/parameters/{index}/terms")
            _require_known(
                parameter.extension_ids, extension_ids, f"/parameters/{index}/extension_ids"
            )
        for index, reference in enumerate(references):
            _require_known(
                (reference.semantic_role_term_ref_id, *reference.qualifier_term_ref_ids),
                term_ids,
                f"/references/{index}/terms",
            )
            _require_known(
                reference.extension_ids, extension_ids, f"/references/{index}/extension_ids"
            )
            if reference.source_node_id is not None and reference.source_node_id not in node_by_id:
                _fail(
                    ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE,
                    f"/references/{index}/source_node_id",
                )
            for step_index, step in enumerate(reference.occurrence_path):
                source = node_by_id.get(step.transform_node_id)
                if source is None:
                    _fail(
                        ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE,
                        f"/references/{index}/occurrence_path/{step_index}/transform_node_id",
                    )
                if source.intent.family is not FeatureFamily.TRANSFORM:
                    _fail(
                        ParametricFeatureGraphErrorCode.INVALID_INPUT,
                        f"/references/{index}/occurrence_path/{step_index}/transform_node_id",
                    )

        dependency_graph: dict[str, tuple[str, ...]] = {}
        for index, node in enumerate(nodes):
            intent = node.intent
            term_refs = [intent.operation_term_ref_id, *node.result_term_ref_ids]
            term_refs.extend(item.role_term_ref_id for item in intent.dependencies)
            term_refs.extend(item.role_term_ref_id for item in intent.references)
            term_refs.extend(item.role_term_ref_id for item in intent.parameter_bindings)
            _require_known(tuple(term_refs), term_ids, f"/nodes/{index}/terms")
            _require_known(
                (*intent.extension_ids, *node.extension_ids),
                extension_ids,
                f"/nodes/{index}/extension_ids",
            )
            upstream = tuple(item.upstream_node_id for item in intent.dependencies)
            _require_known(upstream, set(node_by_id), f"/nodes/{index}/intent/dependencies")
            if node.node_id in upstream:
                _fail(
                    ParametricFeatureGraphErrorCode.CYCLE,
                    f"/nodes/{index}/intent/dependencies",
                )
            dependency_graph[node.node_id] = upstream
            _require_known(
                tuple(item.reference_id for item in intent.references),
                set(reference_by_id),
                f"/nodes/{index}/intent/references",
            )
            _require_known(
                tuple(item.parameter_id for item in intent.parameter_bindings),
                set(parameter_by_id),
                f"/nodes/{index}/intent/parameter_bindings",
            )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            if node_id in visiting:
                _fail(ParametricFeatureGraphErrorCode.CYCLE, "/nodes")
            visiting.add(node_id)
            for upstream_id in dependency_graph[node_id]:
                visit(upstream_id)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in node_by_id:
            visit(node_id)

        closure_cache: dict[str, frozenset[str]] = {}

        def upstream_closure(node_id: str) -> frozenset[str]:
            cached = closure_cache.get(node_id)
            if cached is not None:
                return cached
            result: set[str] = set(dependency_graph[node_id])
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

        parameter_visiting: set[str] = set()
        parameter_values: dict[str, float] = {}

        def evaluate(parameter_id: str) -> float:
            if parameter_id in parameter_values:
                return parameter_values[parameter_id]
            if parameter_id in parameter_visiting:
                _fail(ParametricFeatureGraphErrorCode.CYCLE, "/parameters")
            parameter = parameter_by_id[parameter_id]
            if parameter.value_kind is not ParameterValueKind.SCALAR:
                _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/parameters")
            expression = parameter.expression
            if expression is None:
                result = float(parameter.value)
            else:
                parameter_visiting.add(parameter_id)
                values = [expression.constant]
                for term in expression.terms:
                    source = parameter_by_id.get(term.parameter_id)
                    if source is None:
                        _fail(ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE, "/parameters")
                    if (
                        source.value_kind is not ParameterValueKind.SCALAR
                        or source.unit_term_ref_id != parameter.unit_term_ref_id
                    ):
                        _fail(ParametricFeatureGraphErrorCode.INVALID_INPUT, "/parameters")
                    values.append(term.coefficient * evaluate(term.parameter_id))
                parameter_visiting.remove(parameter_id)
                result = math.fsum(values)
                if not math.isclose(
                    result,
                    float(parameter.value),
                    rel_tol=1e-12,
                    abs_tol=1e-9,
                ):
                    _fail(ParametricFeatureGraphErrorCode.INTEGRITY_FAILURE, "/parameters")
            parameter_values[parameter_id] = result
            return result

        for parameter in parameters:
            if parameter.expression is not None:
                evaluate(parameter.parameter_id)

        _canonical_json(self.to_mapping())

    @property
    def executable(self) -> bool:
        return False

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
            "result_node_ids": list(self.result_node_ids),
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
            "result_node_ids",
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
            result_node_ids=tuple(
                _wire_tuple(
                    fields["result_node_ids"],
                    "/result_node_ids",
                    maximum=MAX_FEATURE_GRAPH_NODES,
                )
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
    "AffineParameterExpressionV2",
    "DesignParameterV2",
    "ExtensionDisposition",
    "FeatureBodyV2",
    "FeatureDependencyV2",
    "FeatureFamily",
    "FeatureIntentV2",
    "FeatureNodeV2",
    "FeatureParameterBindingV2",
    "FeatureReferenceBindingV2",
    "GraphAuthority",
    "InertExtensionV2",
    "MAX_DEPENDENCIES_PER_NODE",
    "MAX_ENUM_PARAMETER_VALUES",
    "MAX_EXTENSIONS_PER_ELEMENT",
    "MAX_FEATURE_GRAPH_BODIES",
    "MAX_FEATURE_GRAPH_EXTENSIONS",
    "MAX_FEATURE_GRAPH_NODES",
    "MAX_FEATURE_GRAPH_PARAMETERS",
    "MAX_FEATURE_GRAPH_REFERENCES",
    "MAX_FEATURE_GRAPH_TERMS",
    "MAX_OCCURRENCE_PATH_STEPS",
    "MAX_PARAMETERS_PER_NODE",
    "MAX_PARAMETER_EXPRESSION_TERMS",
    "MAX_PARAMETRIC_FEATURE_GRAPH_BYTES",
    "MAX_REFERENCES_PER_NODE",
    "MAX_REFERENCE_QUALIFIERS",
    "MAX_RESULTS_PER_NODE",
    "OccurrencePathStepV2",
    "PARAMETRIC_FEATURE_GRAPH_SCHEMA_VERSION",
    "ParameterExpressionTermV2",
    "ParameterValueKind",
    "ParametricFeatureGraphError",
    "ParametricFeatureGraphErrorCode",
    "ParametricFeatureGraphV2",
    "SemanticElementKind",
    "SemanticReferenceScope",
    "SemanticReferenceV2",
    "SemanticTermRefV2",
    "decode_parametric_feature_graph_v2",
    "encode_parametric_feature_graph_v2",
]
