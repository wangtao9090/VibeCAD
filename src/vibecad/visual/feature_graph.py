"""Backend-neutral visual observation graph.

The graph is an immutable, authority-free snapshot of visual evidence.  It
describes sources, coordinate frames, geometry/topology, semantic entities,
relations, measurements, appearance, provenance, and competing hypotheses.
It deliberately contains no CAD feature, operation, backend capability,
acceptance policy, Task, or adoption decision.

Semantics are open but inert: every semantic value is a bounded
``OntologyTermRef`` whose definition digest is part of the graph.  A trusted
resolver outside this module may interpret those terms later.  Unknown terms
remain data and can never execute code or select an alternative by themselves.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum

VISUAL_FEATURE_GRAPH_SCHEMA_VERSION = 1
MAX_VISUAL_FEATURE_GRAPH_BYTES = 192 * 1024

MAX_GRAPH_SOURCES = 16
MAX_GRAPH_FRAMES = 64
MAX_GRAPH_TRANSFORMS = 64
MAX_GRAPH_PROVENANCE = 512
MAX_GRAPH_ONTOLOGY_TERMS = 128
MAX_GRAPH_EXTENSIONS = 64
MAX_GRAPH_GEOMETRIES = 64
MAX_GRAPH_NODES = 64
MAX_GRAPH_RELATIONS = 128
MAX_GRAPH_EQUIVALENCE_GROUPS = 32
MAX_GRAPH_MEASUREMENTS = 128
MAX_GRAPH_APPEARANCES = 64
MAX_GRAPH_HYPOTHESIS_SETS = 16
MAX_GRAPH_ALTERNATIVES_PER_SET = 8
MAX_GRAPH_TOTAL_ALTERNATIVES = 64
MAX_GRAPH_TERMS_PER_ELEMENT = 8
MAX_GRAPH_TOTAL_TERM_REFS = 256
MAX_GRAPH_PROVENANCE_PER_ELEMENT = 8
MAX_GRAPH_TOTAL_PROVENANCE_REFS = 512
MAX_GRAPH_EXTENSIONS_PER_ELEMENT = 8
MAX_GRAPH_TOTAL_EXTENSION_REFS = 512
MAX_GRAPH_INLINE_SAMPLES_PER_GEOMETRY = 256
MAX_GRAPH_TOTAL_INLINE_SAMPLES = 512
MAX_GRAPH_CELLS_PER_GEOMETRY = 512
MAX_GRAPH_TOTAL_CELLS = 1024
MAX_GRAPH_CELL_VERTICES = 64

_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_ABS_NUMBER = 1.0e15
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 16_384
_MAX_JSON_STRING_BYTES = 64 * 1024
_MAX_ERROR_PATH_BYTES = 256
_MAX_IDENTIFIER_BYTES = 64
_MAX_TERM_BYTES = 128
_MAX_MEDIA_TYPE_BYTES = 128
_MAX_MEASUREMENT_DIMENSION = 16
_MAX_RELATION_ENDPOINTS = 16
_MAX_HYPOTHESIS_SUBJECTS = 64

_GRAPH_DIGEST_DOMAIN = b"vibecad-visual-feature-graph-v1\0"
_GRAPH_ID_DOMAIN = b"vibecad-visual-feature-graph-id-v1\0"

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_TERM_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class VisualFeatureGraphErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_VERSION = "unsupported_version"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTEGRITY_FAILURE = "integrity_failure"
    UNKNOWN_REFERENCE = "unknown_reference"
    BINDING_MISMATCH = "binding_mismatch"
    AUTHORITY_VIOLATION = "authority_violation"


class VisualFeatureGraphError(ValueError):
    """Bounded contract failure that never reflects rejected values."""

    def __init__(self, code: VisualFeatureGraphErrorCode, path: str = "") -> None:
        if type(code) is not VisualFeatureGraphErrorCode:
            raise TypeError("code must be an exact VisualFeatureGraphErrorCode")
        if type(path) is not str:
            raise TypeError("path must be a string")
        try:
            encoded = path.encode("utf-8")
        except UnicodeError:
            raise ValueError("path must be bounded") from None
        if len(encoded) > _MAX_ERROR_PATH_BYTES:
            raise ValueError("path must be bounded")
        self.code = code
        self.path = path
        super().__init__(code.value)


def _fail(code: VisualFeatureGraphErrorCode, path: str = "") -> None:
    raise VisualFeatureGraphError(code, path)


def _bounded_text(
    value: object,
    path: str,
    *,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if type(value) is not str:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    if not value or size > maximum or (pattern is not None and pattern.fullmatch(value) is None):
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    return value


def _identifier(value: object, path: str) -> str:
    return _bounded_text(value, path, maximum=_MAX_IDENTIFIER_BYTES, pattern=_IDENTIFIER)


def _term_text(value: object, path: str) -> str:
    result = _bounded_text(value, path, maximum=_MAX_TERM_BYTES, pattern=_TERM_IDENTIFIER)
    if ".." in result or "//" in result:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    return result


def _version(value: object, path: str) -> str:
    return _bounded_text(value, path, maximum=_MAX_IDENTIFIER_BYTES, pattern=_VERSION)


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    return value


def _safe_integer(value: object, path: str, *, positive: bool = False) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SAFE_INTEGER:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    if positive and value == 0:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    return value


def _finite_number(
    value: object,
    path: str,
    *,
    nonnegative: bool = False,
    maximum: float = _MAX_ABS_NUMBER,
) -> float:
    if type(value) not in {int, float}:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    try:
        result = float(value)
    except (OverflowError, ValueError):
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    if not math.isfinite(result) or abs(result) > maximum or (nonnegative and result < 0.0):
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    return 0.0 if result == 0.0 else result


def _advisory_support(value: object, path: str) -> float | None:
    if value is None:
        return None
    result = _finite_number(value, path, nonnegative=True, maximum=1.0)
    if result > 1.0:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    return result


def _identifier_tuple(
    value: object,
    path: str,
    *,
    maximum: int,
    minimum: int = 0,
    ordered: bool = False,
    unique: bool = True,
) -> tuple[str, ...]:
    if type(value) is not tuple or not minimum <= len(value) <= maximum:
        code = (
            VisualFeatureGraphErrorCode.BUDGET_EXCEEDED
            if type(value) is tuple and len(value) > maximum
            else VisualFeatureGraphErrorCode.INVALID_INPUT
        )
        _fail(code, path)
    result = tuple(_identifier(item, f"{path}/{index}") for index, item in enumerate(value))
    if unique and len(set(result)) != len(result):
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    return result if ordered else tuple(sorted(result))


def _number_tuple(
    value: object,
    path: str,
    *,
    maximum: int,
    minimum: int = 0,
    nonnegative: bool = False,
) -> tuple[float, ...]:
    if type(value) is not tuple or not minimum <= len(value) <= maximum:
        code = (
            VisualFeatureGraphErrorCode.BUDGET_EXCEEDED
            if type(value) is tuple and len(value) > maximum
            else VisualFeatureGraphErrorCode.INVALID_INPUT
        )
        _fail(code, path)
    return tuple(
        _finite_number(item, f"{path}/{index}", nonnegative=nonnegative)
        for index, item in enumerate(value)
    )


def _canonical_json(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT)
    if not raw or len(raw) > MAX_VISUAL_FEATURE_GRAPH_BYTES:
        _fail(VisualFeatureGraphErrorCode.BUDGET_EXCEEDED)
    return raw


class VisualGraphAuthority(StrEnum):
    ADVISORY_ONLY = "advisory_only"


class AssertionState(StrEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    NEGATIVE = "negative"
    UNKNOWN = "unknown"
    CONFLICTED = "conflicted"


class ProvenanceKind(StrEnum):
    SENSOR_CAPTURE = "sensor_capture"
    PROVIDER_OUTPUT = "provider_output"
    DETERMINISTIC_DERIVATION = "deterministic_derivation"
    HUMAN_CONFIRMATION = "human_confirmation"
    IMPORTED_ARTIFACT = "imported_artifact"
    UNKNOWN = "unknown"


class FrameKind(StrEnum):
    OVERVIEW_NORMALIZED = "overview_normalized"
    SOURCE_PIXEL = "source_pixel"
    METRIC_PLANE = "metric_plane"
    METRIC_SPACE = "metric_space"
    GENERIC = "generic"


class Handedness(StrEnum):
    RIGHT_HANDED = "right_handed"
    LEFT_HANDED = "left_handed"
    UNKNOWN = "unknown"


class MetricUncertaintyKind(StrEnum):
    UNKNOWN = "unknown"
    ABSOLUTE_BOUND = "absolute_bound"
    AXIS_BOUNDS = "axis_bounds"
    COVARIANCE = "covariance"
    ARTIFACT = "artifact"


class CellOrientation(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    UNKNOWN = "unknown"


class ClosureState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"
    CONFLICTED = "conflicted"


class EntityLayer(StrEnum):
    SCENE = "scene"
    OBJECT = "object"
    ASSEMBLY = "assembly"
    COMPONENT = "component"
    FEATURE = "feature"
    REGION = "region"
    LANDMARK = "landmark"
    ATTRIBUTE = "attribute"
    UNKNOWN = "unknown"


class GraphElementKind(StrEnum):
    SOURCE = "source"
    FRAME = "frame"
    TRANSFORM = "transform"
    GEOMETRY = "geometry"
    SAMPLE = "sample"
    CELL = "cell"
    NODE = "node"
    RELATION = "relation"
    EQUIVALENCE_GROUP = "equivalence_group"
    MEASUREMENT = "measurement"
    APPEARANCE = "appearance"
    HYPOTHESIS_SET = "hypothesis_set"
    HYPOTHESIS_ALTERNATIVE = "hypothesis_alternative"
    PROVENANCE = "provenance"
    EXTENSION = "extension"


class MeasurementEstimateKind(StrEnum):
    UNKNOWN = "unknown"
    EXACT = "exact"
    INTERVAL = "interval"
    COVARIANCE = "covariance"
    EMPIRICAL = "empirical"
    CONFLICTED = "conflicted"


@dataclass(frozen=True, slots=True, kw_only=True)
class OntologyTermRef:
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
        object.__setattr__(self, "term_id", _term_text(self.term_id, "/term_id"))
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


@dataclass(frozen=True, slots=True, kw_only=True)
class ContentRef:
    sha256: str
    size_bytes: int
    media_type: str
    schema_term_ref_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sha256", _digest(self.sha256, "/sha256"))
        object.__setattr__(self, "size_bytes", _safe_integer(self.size_bytes, "/size_bytes"))
        object.__setattr__(
            self,
            "media_type",
            _bounded_text(
                self.media_type,
                "/media_type",
                maximum=_MAX_MEDIA_TYPE_BYTES,
                pattern=_MEDIA_TYPE,
            ),
        )
        if self.schema_term_ref_id is not None:
            object.__setattr__(
                self,
                "schema_term_ref_id",
                _identifier(self.schema_term_ref_id, "/schema_term_ref_id"),
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "schema_term_ref_id": self.schema_term_ref_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtensionRef:
    extension_id: str
    namespace: str
    vocabulary_version: str
    schema_term_ref_id: str
    payload: ContentRef

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
        if type(self.payload) is not ContentRef:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/payload")

    def to_mapping(self) -> dict[str, object]:
        return {
            "extension_id": self.extension_id,
            "namespace": self.namespace,
            "vocabulary_version": self.vocabulary_version,
            "schema_term_ref_id": self.schema_term_ref_id,
            "payload": self.payload.to_mapping(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ProvenanceRecord:
    provenance_id: str
    kind: ProvenanceKind
    content: ContentRef
    producer_id: str
    producer_version: str
    source_ids: tuple[str, ...] = ()
    parent_provenance_ids: tuple[str, ...] = ()
    term_ref_ids: tuple[str, ...] = ()
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provenance_id",
            _identifier(self.provenance_id, "/provenance_id"),
        )
        if type(self.kind) is not ProvenanceKind:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/kind")
        if type(self.content) is not ContentRef:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/content")
        object.__setattr__(self, "producer_id", _identifier(self.producer_id, "/producer_id"))
        object.__setattr__(
            self,
            "producer_version",
            _version(self.producer_version, "/producer_version"),
        )
        object.__setattr__(
            self,
            "source_ids",
            _identifier_tuple(self.source_ids, "/source_ids", maximum=MAX_GRAPH_SOURCES),
        )
        object.__setattr__(
            self,
            "parent_provenance_ids",
            _identifier_tuple(
                self.parent_provenance_ids,
                "/parent_provenance_ids",
                maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "term_ref_ids",
            _identifier_tuple(
                self.term_ref_ids,
                "/term_ref_ids",
                maximum=MAX_GRAPH_TERMS_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "provenance_id": self.provenance_id,
            "kind": self.kind.value,
            "content": self.content.to_mapping(),
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "source_ids": list(self.source_ids),
            "parent_provenance_ids": list(self.parent_provenance_ids),
            "term_ref_ids": list(self.term_ref_ids),
            "extension_ids": list(self.extension_ids),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceArtifact:
    source_id: str
    content: ContentRef
    modality_term_ref_ids: tuple[str, ...]
    parent_source_id: str | None = None
    sequence_index: int | None = None
    provenance_ids: tuple[str, ...] = ()
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _identifier(self.source_id, "/source_id"))
        if type(self.content) is not ContentRef:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/content")
        object.__setattr__(
            self,
            "modality_term_ref_ids",
            _identifier_tuple(
                self.modality_term_ref_ids,
                "/modality_term_ref_ids",
                maximum=MAX_GRAPH_TERMS_PER_ELEMENT,
                minimum=1,
            ),
        )
        if self.parent_source_id is not None:
            object.__setattr__(
                self,
                "parent_source_id",
                _identifier(self.parent_source_id, "/parent_source_id"),
            )
            if self.parent_source_id == self.source_id:
                _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/parent_source_id")
        if self.sequence_index is not None:
            object.__setattr__(
                self,
                "sequence_index",
                _safe_integer(self.sequence_index, "/sequence_index"),
            )
        object.__setattr__(
            self,
            "provenance_ids",
            _identifier_tuple(
                self.provenance_ids,
                "/provenance_ids",
                maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "content": self.content.to_mapping(),
            "modality_term_ref_ids": list(self.modality_term_ref_ids),
            "parent_source_id": self.parent_source_id,
            "sequence_index": self.sequence_index,
            "provenance_ids": list(self.provenance_ids),
            "extension_ids": list(self.extension_ids),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class OverviewNormalizedFrameBinding:
    collection_id: str
    collection_manifest_sha256: str
    derivation_manifest_sha256: str
    provider_asset_id: str
    provider_asset_sha256: str
    width: int
    height: int
    kind: FrameKind = FrameKind.OVERVIEW_NORMALIZED

    def __post_init__(self) -> None:
        if self.kind is not FrameKind.OVERVIEW_NORMALIZED:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/kind")
        object.__setattr__(
            self,
            "collection_id",
            _identifier(self.collection_id, "/collection_id"),
        )
        for name in (
            "collection_manifest_sha256",
            "derivation_manifest_sha256",
            "provider_asset_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        object.__setattr__(
            self,
            "provider_asset_id",
            _identifier(self.provider_asset_id, "/provider_asset_id"),
        )
        object.__setattr__(self, "width", _safe_integer(self.width, "/width", positive=True))
        object.__setattr__(self, "height", _safe_integer(self.height, "/height", positive=True))

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "collection_id": self.collection_id,
            "collection_manifest_sha256": self.collection_manifest_sha256,
            "derivation_manifest_sha256": self.derivation_manifest_sha256,
            "provider_asset_id": self.provider_asset_id,
            "provider_asset_sha256": self.provider_asset_sha256,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SourcePixelFrameBinding:
    source_sha256: str
    width: int
    height: int
    kind: FrameKind = FrameKind.SOURCE_PIXEL

    def __post_init__(self) -> None:
        if self.kind is not FrameKind.SOURCE_PIXEL:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/kind")
        object.__setattr__(self, "source_sha256", _digest(self.source_sha256, "/source_sha256"))
        object.__setattr__(self, "width", _safe_integer(self.width, "/width", positive=True))
        object.__setattr__(self, "height", _safe_integer(self.height, "/height", positive=True))

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "source_sha256": self.source_sha256,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricPlaneFrameBinding:
    frame_record_sha256: str
    calibration_receipt_sha256: str
    calibration_sha256: str
    unit: str = "mm"
    kind: FrameKind = FrameKind.METRIC_PLANE

    def __post_init__(self) -> None:
        if self.kind is not FrameKind.METRIC_PLANE or self.unit != "mm":
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/kind")
        for name in (
            "frame_record_sha256",
            "calibration_receipt_sha256",
            "calibration_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "frame_record_sha256": self.frame_record_sha256,
            "calibration_receipt_sha256": self.calibration_receipt_sha256,
            "calibration_sha256": self.calibration_sha256,
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricSpaceFrameBinding:
    frame_record_sha256: str
    handedness: Handedness
    axis_term_ref_ids: tuple[str, str, str]
    unit: str = "mm"
    kind: FrameKind = FrameKind.METRIC_SPACE

    def __post_init__(self) -> None:
        if self.kind is not FrameKind.METRIC_SPACE or self.unit != "mm":
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/kind")
        object.__setattr__(
            self,
            "frame_record_sha256",
            _digest(self.frame_record_sha256, "/frame_record_sha256"),
        )
        if type(self.handedness) is not Handedness:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/handedness")
        axes = _identifier_tuple(
            self.axis_term_ref_ids,
            "/axis_term_ref_ids",
            maximum=3,
            minimum=3,
            ordered=True,
        )
        object.__setattr__(self, "axis_term_ref_ids", axes)

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "frame_record_sha256": self.frame_record_sha256,
            "handedness": self.handedness.value,
            "axis_term_ref_ids": list(self.axis_term_ref_ids),
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class GenericFrameBinding:
    frame_record_sha256: str
    dimension: int
    coordinate_system_term_ref_id: str
    axis_term_ref_ids: tuple[str, ...]
    unit_term_ref_ids: tuple[str, ...]
    kind: FrameKind = FrameKind.GENERIC

    def __post_init__(self) -> None:
        if self.kind is not FrameKind.GENERIC:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/kind")
        object.__setattr__(
            self,
            "frame_record_sha256",
            _digest(self.frame_record_sha256, "/frame_record_sha256"),
        )
        dimension = _safe_integer(self.dimension, "/dimension", positive=True)
        if dimension > 4:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/dimension")
        object.__setattr__(self, "dimension", dimension)
        object.__setattr__(
            self,
            "coordinate_system_term_ref_id",
            _identifier(self.coordinate_system_term_ref_id, "/coordinate_system_term_ref_id"),
        )
        object.__setattr__(
            self,
            "axis_term_ref_ids",
            _identifier_tuple(
                self.axis_term_ref_ids,
                "/axis_term_ref_ids",
                maximum=dimension,
                minimum=dimension,
                ordered=True,
            ),
        )
        object.__setattr__(
            self,
            "unit_term_ref_ids",
            _identifier_tuple(
                self.unit_term_ref_ids,
                "/unit_term_ref_ids",
                maximum=dimension,
                minimum=dimension,
                ordered=True,
                unique=False,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "frame_record_sha256": self.frame_record_sha256,
            "dimension": self.dimension,
            "coordinate_system_term_ref_id": self.coordinate_system_term_ref_id,
            "axis_term_ref_ids": list(self.axis_term_ref_ids),
            "unit_term_ref_ids": list(self.unit_term_ref_ids),
        }


type FrameBinding = (
    OverviewNormalizedFrameBinding
    | SourcePixelFrameBinding
    | MetricPlaneFrameBinding
    | MetricSpaceFrameBinding
    | GenericFrameBinding
)


def _frame_dimension(binding: FrameBinding) -> int:
    if type(binding) in {OverviewNormalizedFrameBinding, SourcePixelFrameBinding}:
        return 2
    if type(binding) is MetricPlaneFrameBinding:
        return 2
    if type(binding) is MetricSpaceFrameBinding:
        return 3
    if type(binding) is GenericFrameBinding:
        return binding.dimension
    _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/binding")


@dataclass(frozen=True, slots=True, kw_only=True)
class CoordinateFrame:
    frame_id: str
    binding: FrameBinding
    source_id: str | None = None
    term_ref_ids: tuple[str, ...] = ()
    provenance_ids: tuple[str, ...] = ()
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame_id", _identifier(self.frame_id, "/frame_id"))
        if type(self.binding) not in {
            OverviewNormalizedFrameBinding,
            SourcePixelFrameBinding,
            MetricPlaneFrameBinding,
            MetricSpaceFrameBinding,
            GenericFrameBinding,
        }:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/binding")
        source_required = type(self.binding) in {
            OverviewNormalizedFrameBinding,
            SourcePixelFrameBinding,
            MetricPlaneFrameBinding,
        }
        if source_required and self.source_id is None:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/source_id")
        if self.source_id is not None:
            object.__setattr__(self, "source_id", _identifier(self.source_id, "/source_id"))
        object.__setattr__(
            self,
            "term_ref_ids",
            _identifier_tuple(
                self.term_ref_ids,
                "/term_ref_ids",
                maximum=MAX_GRAPH_TERMS_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "provenance_ids",
            _identifier_tuple(
                self.provenance_ids,
                "/provenance_ids",
                maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
            ),
        )

    @property
    def dimension(self) -> int:
        return _frame_dimension(self.binding)

    def to_mapping(self) -> dict[str, object]:
        return {
            "frame_id": self.frame_id,
            "binding": self.binding.to_mapping(),
            "source_id": self.source_id,
            "term_ref_ids": list(self.term_ref_ids),
            "provenance_ids": list(self.provenance_ids),
            "extension_ids": list(self.extension_ids),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FrameTransformRef:
    transform_id: str
    from_frame_id: str
    to_frame_id: str
    transform_term_ref_id: str
    receipt: ContentRef
    uncertainty_measurement_id: str | None = None
    provenance_ids: tuple[str, ...] = ()
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "transform_id", _identifier(self.transform_id, "/transform_id"))
        object.__setattr__(
            self,
            "from_frame_id",
            _identifier(self.from_frame_id, "/from_frame_id"),
        )
        object.__setattr__(self, "to_frame_id", _identifier(self.to_frame_id, "/to_frame_id"))
        if self.from_frame_id == self.to_frame_id:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/to_frame_id")
        object.__setattr__(
            self,
            "transform_term_ref_id",
            _identifier(self.transform_term_ref_id, "/transform_term_ref_id"),
        )
        if type(self.receipt) is not ContentRef:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/receipt")
        if self.uncertainty_measurement_id is not None:
            object.__setattr__(
                self,
                "uncertainty_measurement_id",
                _identifier(self.uncertainty_measurement_id, "/uncertainty_measurement_id"),
            )
        object.__setattr__(
            self,
            "provenance_ids",
            _identifier_tuple(
                self.provenance_ids,
                "/provenance_ids",
                maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "transform_id": self.transform_id,
            "from_frame_id": self.from_frame_id,
            "to_frame_id": self.to_frame_id,
            "transform_term_ref_id": self.transform_term_ref_id,
            "receipt": self.receipt.to_mapping(),
            "uncertainty_measurement_id": self.uncertainty_measurement_id,
            "provenance_ids": list(self.provenance_ids),
            "extension_ids": list(self.extension_ids),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricUncertainty:
    kind: MetricUncertaintyKind
    bounds: tuple[float, ...] = ()
    covariance: tuple[float, ...] = ()
    artifact: ContentRef | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not MetricUncertaintyKind:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/kind")
        object.__setattr__(
            self,
            "bounds",
            _number_tuple(
                self.bounds,
                "/bounds",
                maximum=4,
                nonnegative=True,
            ),
        )
        object.__setattr__(
            self,
            "covariance",
            _number_tuple(self.covariance, "/covariance", maximum=16),
        )
        if self.artifact is not None and type(self.artifact) is not ContentRef:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/artifact")
        if self.kind is MetricUncertaintyKind.UNKNOWN:
            valid = not self.bounds and not self.covariance and self.artifact is None
        elif self.kind is MetricUncertaintyKind.ABSOLUTE_BOUND:
            valid = len(self.bounds) == 1 and not self.covariance and self.artifact is None
        elif self.kind is MetricUncertaintyKind.AXIS_BOUNDS:
            valid = bool(self.bounds) and not self.covariance and self.artifact is None
        elif self.kind is MetricUncertaintyKind.COVARIANCE:
            valid = bool(self.covariance) and not self.bounds and self.artifact is None
        else:
            valid = not self.bounds and not self.covariance and self.artifact is not None
        if not valid:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/kind")

    def validate_dimension(self, dimension: int, path: str) -> None:
        if self.kind is MetricUncertaintyKind.AXIS_BOUNDS and len(self.bounds) != dimension:
            _fail(VisualFeatureGraphErrorCode.BINDING_MISMATCH, path)
        if self.kind is MetricUncertaintyKind.COVARIANCE:
            _validate_covariance(self.covariance, dimension, path)

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "bounds": list(self.bounds),
            "covariance": list(self.covariance),
            "artifact": None if self.artifact is None else self.artifact.to_mapping(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CoordinateSample:
    sample_id: str
    coordinates: tuple[float, ...]
    uncertainty: MetricUncertainty
    term_ref_ids: tuple[str, ...] = ()
    provenance_ids: tuple[str, ...] = ()
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "sample_id", _identifier(self.sample_id, "/sample_id"))
        object.__setattr__(
            self,
            "coordinates",
            _number_tuple(self.coordinates, "/coordinates", maximum=4, minimum=1),
        )
        if type(self.uncertainty) is not MetricUncertainty:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/uncertainty")
        self.uncertainty.validate_dimension(len(self.coordinates), "/uncertainty")
        object.__setattr__(
            self,
            "term_ref_ids",
            _identifier_tuple(
                self.term_ref_ids,
                "/term_ref_ids",
                maximum=MAX_GRAPH_TERMS_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "provenance_ids",
            _identifier_tuple(
                self.provenance_ids,
                "/provenance_ids",
                maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "coordinates": list(self.coordinates),
            "uncertainty": self.uncertainty.to_mapping(),
            "term_ref_ids": list(self.term_ref_ids),
            "provenance_ids": list(self.provenance_ids),
            "extension_ids": list(self.extension_ids),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class TopologyCell:
    cell_id: str
    cell_term_ref_id: str
    sample_ids: tuple[str, ...]
    orientation: CellOrientation = CellOrientation.UNKNOWN
    provenance_ids: tuple[str, ...] = ()
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "cell_id", _identifier(self.cell_id, "/cell_id"))
        object.__setattr__(
            self,
            "cell_term_ref_id",
            _identifier(self.cell_term_ref_id, "/cell_term_ref_id"),
        )
        object.__setattr__(
            self,
            "sample_ids",
            _identifier_tuple(
                self.sample_ids,
                "/sample_ids",
                maximum=MAX_GRAPH_CELL_VERTICES,
                minimum=1,
                ordered=True,
            ),
        )
        if type(self.orientation) is not CellOrientation:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/orientation")
        object.__setattr__(
            self,
            "provenance_ids",
            _identifier_tuple(
                self.provenance_ids,
                "/provenance_ids",
                maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "cell_id": self.cell_id,
            "cell_term_ref_id": self.cell_term_ref_id,
            "sample_ids": list(self.sample_ids),
            "orientation": self.orientation.value,
            "provenance_ids": list(self.provenance_ids),
            "extension_ids": list(self.extension_ids),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class GeometryRecord:
    geometry_id: str
    frame_id: str
    representation_term_ref_id: str
    intrinsic_dimension: int
    samples: tuple[CoordinateSample, ...] = ()
    cells: tuple[TopologyCell, ...] = ()
    artifact: ContentRef | None = None
    closure: ClosureState = ClosureState.UNKNOWN
    state: AssertionState = AssertionState.UNKNOWN
    term_ref_ids: tuple[str, ...] = ()
    provenance_ids: tuple[str, ...] = ()
    extension_ids: tuple[str, ...] = ()
    advisory_support: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "geometry_id", _identifier(self.geometry_id, "/geometry_id"))
        object.__setattr__(self, "frame_id", _identifier(self.frame_id, "/frame_id"))
        object.__setattr__(
            self,
            "representation_term_ref_id",
            _identifier(self.representation_term_ref_id, "/representation_term_ref_id"),
        )
        dimension = _safe_integer(self.intrinsic_dimension, "/intrinsic_dimension")
        if dimension > 3:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/intrinsic_dimension")
        object.__setattr__(self, "intrinsic_dimension", dimension)
        if (
            type(self.samples) is not tuple
            or len(self.samples) > MAX_GRAPH_INLINE_SAMPLES_PER_GEOMETRY
        ):
            _fail(
                VisualFeatureGraphErrorCode.BUDGET_EXCEEDED
                if type(self.samples) is tuple
                else VisualFeatureGraphErrorCode.INVALID_INPUT,
                "/samples",
            )
        if any(type(item) is not CoordinateSample for item in self.samples):
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/samples")
        samples = tuple(sorted(self.samples, key=lambda item: item.sample_id))
        if len({item.sample_id for item in samples}) != len(samples):
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/samples")
        if samples and len({len(item.coordinates) for item in samples}) != 1:
            _fail(VisualFeatureGraphErrorCode.BINDING_MISMATCH, "/samples")
        object.__setattr__(self, "samples", samples)
        if type(self.cells) is not tuple or len(self.cells) > MAX_GRAPH_CELLS_PER_GEOMETRY:
            _fail(
                VisualFeatureGraphErrorCode.BUDGET_EXCEEDED
                if type(self.cells) is tuple
                else VisualFeatureGraphErrorCode.INVALID_INPUT,
                "/cells",
            )
        if any(type(item) is not TopologyCell for item in self.cells):
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/cells")
        cells = tuple(sorted(self.cells, key=lambda item: item.cell_id))
        if len({item.cell_id for item in cells}) != len(cells):
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/cells")
        sample_ids = {item.sample_id for item in samples}
        if any(not set(item.sample_ids).issubset(sample_ids) for item in cells):
            _fail(VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE, "/cells")
        object.__setattr__(self, "cells", cells)
        if self.artifact is not None and type(self.artifact) is not ContentRef:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/artifact")
        if not samples and self.artifact is None:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/samples")
        if type(self.closure) is not ClosureState:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/closure")
        if type(self.state) is not AssertionState:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/state")
        object.__setattr__(
            self,
            "term_ref_ids",
            _identifier_tuple(
                self.term_ref_ids,
                "/term_ref_ids",
                maximum=MAX_GRAPH_TERMS_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "provenance_ids",
            _identifier_tuple(
                self.provenance_ids,
                "/provenance_ids",
                maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "advisory_support",
            _advisory_support(self.advisory_support, "/advisory_support"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "geometry_id": self.geometry_id,
            "frame_id": self.frame_id,
            "representation_term_ref_id": self.representation_term_ref_id,
            "intrinsic_dimension": self.intrinsic_dimension,
            "samples": [item.to_mapping() for item in self.samples],
            "cells": [item.to_mapping() for item in self.cells],
            "artifact": None if self.artifact is None else self.artifact.to_mapping(),
            "closure": self.closure.value,
            "state": self.state.value,
            "term_ref_ids": list(self.term_ref_ids),
            "provenance_ids": list(self.provenance_ids),
            "extension_ids": list(self.extension_ids),
            "advisory_support": self.advisory_support,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureNode:
    node_id: str
    layer: EntityLayer
    term_ref_ids: tuple[str, ...]
    geometry_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    state: AssertionState = AssertionState.UNKNOWN
    provenance_ids: tuple[str, ...] = ()
    extension_ids: tuple[str, ...] = ()
    advisory_support: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/node_id"))
        if type(self.layer) is not EntityLayer:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/layer")
        object.__setattr__(
            self,
            "term_ref_ids",
            _identifier_tuple(
                self.term_ref_ids,
                "/term_ref_ids",
                maximum=MAX_GRAPH_TERMS_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "geometry_ids",
            _identifier_tuple(
                self.geometry_ids,
                "/geometry_ids",
                maximum=MAX_GRAPH_GEOMETRIES,
            ),
        )
        object.__setattr__(
            self,
            "source_ids",
            _identifier_tuple(self.source_ids, "/source_ids", maximum=MAX_GRAPH_SOURCES),
        )
        if not self.term_ref_ids and not self.geometry_ids:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/term_ref_ids")
        if type(self.state) is not AssertionState:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/state")
        object.__setattr__(
            self,
            "provenance_ids",
            _identifier_tuple(
                self.provenance_ids,
                "/provenance_ids",
                maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "advisory_support",
            _advisory_support(self.advisory_support, "/advisory_support"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "layer": self.layer.value,
            "term_ref_ids": list(self.term_ref_ids),
            "geometry_ids": list(self.geometry_ids),
            "source_ids": list(self.source_ids),
            "state": self.state.value,
            "provenance_ids": list(self.provenance_ids),
            "extension_ids": list(self.extension_ids),
            "advisory_support": self.advisory_support,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class GraphElementRef:
    kind: GraphElementKind
    element_id: str

    def __post_init__(self) -> None:
        if type(self.kind) is not GraphElementKind:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/kind")
        object.__setattr__(self, "element_id", _identifier(self.element_id, "/element_id"))

    def to_mapping(self) -> dict[str, object]:
        return {"kind": self.kind.value, "element_id": self.element_id}


def _element_ref_tuple(
    value: object,
    path: str,
    *,
    maximum: int,
    minimum: int = 0,
    ordered: bool = False,
) -> tuple[GraphElementRef, ...]:
    if type(value) is not tuple or not minimum <= len(value) <= maximum:
        code = (
            VisualFeatureGraphErrorCode.BUDGET_EXCEEDED
            if type(value) is tuple and len(value) > maximum
            else VisualFeatureGraphErrorCode.INVALID_INPUT
        )
        _fail(code, path)
    if any(type(item) is not GraphElementRef for item in value):
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    keys = tuple((item.kind.value, item.element_id) for item in value)
    if len(set(keys)) != len(keys):
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    if ordered:
        return value
    return tuple(sorted(value, key=lambda item: (item.kind.value, item.element_id)))


@dataclass(frozen=True, slots=True, kw_only=True)
class RelationEndpoint:
    ordinal: int
    role_term_ref_id: str
    element: GraphElementRef

    def __post_init__(self) -> None:
        ordinal = _safe_integer(self.ordinal, "/ordinal")
        if ordinal >= _MAX_RELATION_ENDPOINTS:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/ordinal")
        object.__setattr__(self, "ordinal", ordinal)
        object.__setattr__(
            self,
            "role_term_ref_id",
            _identifier(self.role_term_ref_id, "/role_term_ref_id"),
        )
        if type(self.element) is not GraphElementRef:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/element")

    def to_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "role_term_ref_id": self.role_term_ref_id,
            "element": self.element.to_mapping(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureRelation:
    relation_id: str
    relation_term_ref_id: str
    endpoints: tuple[RelationEndpoint, ...]
    state: AssertionState = AssertionState.UNKNOWN
    provenance_ids: tuple[str, ...] = ()
    extension_ids: tuple[str, ...] = ()
    advisory_support: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "relation_id", _identifier(self.relation_id, "/relation_id"))
        object.__setattr__(
            self,
            "relation_term_ref_id",
            _identifier(self.relation_term_ref_id, "/relation_term_ref_id"),
        )
        if type(self.endpoints) is not tuple or len(self.endpoints) < 2:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/endpoints")
        if len(self.endpoints) > _MAX_RELATION_ENDPOINTS:
            _fail(VisualFeatureGraphErrorCode.BUDGET_EXCEEDED, "/endpoints")
        if any(type(item) is not RelationEndpoint for item in self.endpoints):
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/endpoints")
        endpoints = tuple(sorted(self.endpoints, key=lambda item: item.ordinal))
        if tuple(item.ordinal for item in endpoints) != tuple(range(len(endpoints))):
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/endpoints")
        if len({(item.element.kind, item.element.element_id) for item in endpoints}) < 2:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/endpoints")
        object.__setattr__(self, "endpoints", endpoints)
        if type(self.state) is not AssertionState:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/state")
        object.__setattr__(
            self,
            "provenance_ids",
            _identifier_tuple(
                self.provenance_ids,
                "/provenance_ids",
                maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "advisory_support",
            _advisory_support(self.advisory_support, "/advisory_support"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "relation_id": self.relation_id,
            "relation_term_ref_id": self.relation_term_ref_id,
            "endpoints": [item.to_mapping() for item in self.endpoints],
            "state": self.state.value,
            "provenance_ids": list(self.provenance_ids),
            "extension_ids": list(self.extension_ids),
            "advisory_support": self.advisory_support,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class EquivalenceGroup:
    group_id: str
    member_node_ids: tuple[str, ...]
    state: AssertionState = AssertionState.UNKNOWN
    provenance_ids: tuple[str, ...] = ()
    extension_ids: tuple[str, ...] = ()
    advisory_support: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "group_id", _identifier(self.group_id, "/group_id"))
        object.__setattr__(
            self,
            "member_node_ids",
            _identifier_tuple(
                self.member_node_ids,
                "/member_node_ids",
                maximum=MAX_GRAPH_NODES,
                minimum=2,
            ),
        )
        if type(self.state) is not AssertionState:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/state")
        object.__setattr__(
            self,
            "provenance_ids",
            _identifier_tuple(
                self.provenance_ids,
                "/provenance_ids",
                maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "advisory_support",
            _advisory_support(self.advisory_support, "/advisory_support"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "member_node_ids": list(self.member_node_ids),
            "state": self.state.value,
            "provenance_ids": list(self.provenance_ids),
            "extension_ids": list(self.extension_ids),
            "advisory_support": self.advisory_support,
        }


def _validate_covariance(values: tuple[float, ...], dimension: int, path: str) -> None:
    if len(values) != dimension * dimension:
        _fail(VisualFeatureGraphErrorCode.BINDING_MISMATCH, path)
    for row in range(dimension):
        if values[row * dimension + row] < 0.0:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
        for column in range(row + 1, dimension):
            if not math.isclose(
                values[row * dimension + column],
                values[column * dimension + row],
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    scale = max(1.0, *(abs(value) for value in values))
    tolerance = scale * 1e-12
    factor = [[0.0 for _column in range(dimension)] for _row in range(dimension)]
    for row in range(dimension):
        for column in range(row + 1):
            remainder = values[row * dimension + column] - sum(
                factor[row][index] * factor[column][index] for index in range(column)
            )
            if row == column:
                if remainder < -tolerance:
                    _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
                factor[row][column] = math.sqrt(max(0.0, remainder))
            elif factor[column][column] > math.sqrt(tolerance):
                factor[row][column] = remainder / factor[column][column]
            elif abs(remainder) > tolerance:
                _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)


@dataclass(frozen=True, slots=True, kw_only=True)
class MeasurementEstimate:
    kind: MeasurementEstimateKind
    central: tuple[float, ...] = ()
    lower: tuple[float, ...] = ()
    upper: tuple[float, ...] = ()
    covariance: tuple[float, ...] = ()
    artifact: ContentRef | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not MeasurementEstimateKind:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/kind")
        for name in ("central", "lower", "upper"):
            object.__setattr__(
                self,
                name,
                _number_tuple(
                    getattr(self, name),
                    f"/{name}",
                    maximum=_MAX_MEASUREMENT_DIMENSION,
                ),
            )
        object.__setattr__(
            self,
            "covariance",
            _number_tuple(
                self.covariance,
                "/covariance",
                maximum=_MAX_MEASUREMENT_DIMENSION**2,
            ),
        )
        if self.artifact is not None and type(self.artifact) is not ContentRef:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/artifact")
        if self.kind in {MeasurementEstimateKind.UNKNOWN, MeasurementEstimateKind.CONFLICTED}:
            valid = not any((self.central, self.lower, self.upper, self.covariance)) and (
                self.artifact is None
            )
        elif self.kind is MeasurementEstimateKind.EXACT:
            valid = (
                bool(self.central)
                and not any((self.lower, self.upper, self.covariance))
                and (self.artifact is None)
            )
        elif self.kind is MeasurementEstimateKind.INTERVAL:
            valid = (
                bool(self.lower)
                and len(self.lower) == len(self.upper)
                and (not self.central or len(self.central) == len(self.lower))
                and not self.covariance
                and self.artifact is None
            )
            if valid:
                for index, (lower, upper) in enumerate(zip(self.lower, self.upper, strict=True)):
                    if lower > upper:
                        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, f"/lower/{index}")
                    if self.central and not lower <= self.central[index] <= upper:
                        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, f"/central/{index}")
        elif self.kind is MeasurementEstimateKind.COVARIANCE:
            valid = (
                bool(self.central) and not self.lower and not self.upper and self.artifact is None
            )
            if valid:
                _validate_covariance(self.covariance, len(self.central), "/covariance")
        else:
            valid = self.artifact is not None and not any((self.lower, self.upper, self.covariance))
        if not valid:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/kind")

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "central": list(self.central),
            "lower": list(self.lower),
            "upper": list(self.upper),
            "covariance": list(self.covariance),
            "artifact": None if self.artifact is None else self.artifact.to_mapping(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class MeasurementRecord:
    measurement_id: str
    quantity_term_ref_id: str
    unit_term_ref_id: str
    targets: tuple[GraphElementRef, ...]
    estimate: MeasurementEstimate
    frame_ids: tuple[str, ...] = ()
    transform_ids: tuple[str, ...] = ()
    state: AssertionState = AssertionState.UNKNOWN
    provenance_ids: tuple[str, ...] = ()
    extension_ids: tuple[str, ...] = ()
    advisory_support: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "measurement_id",
            _identifier(self.measurement_id, "/measurement_id"),
        )
        object.__setattr__(
            self,
            "quantity_term_ref_id",
            _identifier(self.quantity_term_ref_id, "/quantity_term_ref_id"),
        )
        object.__setattr__(
            self,
            "unit_term_ref_id",
            _identifier(self.unit_term_ref_id, "/unit_term_ref_id"),
        )
        object.__setattr__(
            self,
            "targets",
            _element_ref_tuple(self.targets, "/targets", maximum=16, minimum=1),
        )
        if type(self.estimate) is not MeasurementEstimate:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/estimate")
        object.__setattr__(
            self,
            "frame_ids",
            _identifier_tuple(self.frame_ids, "/frame_ids", maximum=MAX_GRAPH_FRAMES),
        )
        object.__setattr__(
            self,
            "transform_ids",
            _identifier_tuple(
                self.transform_ids,
                "/transform_ids",
                maximum=MAX_GRAPH_TRANSFORMS,
            ),
        )
        if len(self.frame_ids) > 1 and not self.transform_ids:
            _fail(VisualFeatureGraphErrorCode.BINDING_MISMATCH, "/transform_ids")
        if len(self.frame_ids) <= 1 and self.transform_ids:
            _fail(VisualFeatureGraphErrorCode.BINDING_MISMATCH, "/transform_ids")
        if type(self.state) is not AssertionState:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/state")
        if (
            self.state is AssertionState.UNKNOWN
            and self.estimate.kind is not MeasurementEstimateKind.UNKNOWN
        ):
            _fail(VisualFeatureGraphErrorCode.BINDING_MISMATCH, "/estimate")
        if (
            self.state is AssertionState.CONFLICTED
            and self.estimate.kind is not MeasurementEstimateKind.CONFLICTED
        ):
            _fail(VisualFeatureGraphErrorCode.BINDING_MISMATCH, "/estimate")
        object.__setattr__(
            self,
            "provenance_ids",
            _identifier_tuple(
                self.provenance_ids,
                "/provenance_ids",
                maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "advisory_support",
            _advisory_support(self.advisory_support, "/advisory_support"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "measurement_id": self.measurement_id,
            "quantity_term_ref_id": self.quantity_term_ref_id,
            "unit_term_ref_id": self.unit_term_ref_id,
            "targets": [item.to_mapping() for item in self.targets],
            "estimate": self.estimate.to_mapping(),
            "frame_ids": list(self.frame_ids),
            "transform_ids": list(self.transform_ids),
            "state": self.state.value,
            "provenance_ids": list(self.provenance_ids),
            "extension_ids": list(self.extension_ids),
            "advisory_support": self.advisory_support,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AppearanceRecord:
    appearance_id: str
    target_node_id: str
    appearance_term_ref_ids: tuple[str, ...] = ()
    channel_measurement_ids: tuple[str, ...] = ()
    texture_artifacts: tuple[ContentRef, ...] = ()
    source_ids: tuple[str, ...] = ()
    state: AssertionState = AssertionState.UNKNOWN
    provenance_ids: tuple[str, ...] = ()
    extension_ids: tuple[str, ...] = ()
    advisory_support: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "appearance_id",
            _identifier(self.appearance_id, "/appearance_id"),
        )
        object.__setattr__(
            self,
            "target_node_id",
            _identifier(self.target_node_id, "/target_node_id"),
        )
        object.__setattr__(
            self,
            "appearance_term_ref_ids",
            _identifier_tuple(
                self.appearance_term_ref_ids,
                "/appearance_term_ref_ids",
                maximum=MAX_GRAPH_TERMS_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "channel_measurement_ids",
            _identifier_tuple(
                self.channel_measurement_ids,
                "/channel_measurement_ids",
                maximum=16,
            ),
        )
        if type(self.texture_artifacts) is not tuple:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/texture_artifacts")
        if len(self.texture_artifacts) > 8:
            _fail(VisualFeatureGraphErrorCode.BUDGET_EXCEEDED, "/texture_artifacts")
        if any(type(item) is not ContentRef for item in self.texture_artifacts):
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/texture_artifacts")
        texture_keys = tuple(
            (item.sha256, item.size_bytes, item.media_type, item.schema_term_ref_id)
            for item in self.texture_artifacts
        )
        if len(set(texture_keys)) != len(texture_keys):
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/texture_artifacts")
        object.__setattr__(
            self,
            "texture_artifacts",
            tuple(
                sorted(
                    self.texture_artifacts,
                    key=lambda item: (
                        item.sha256,
                        item.size_bytes,
                        item.media_type,
                        item.schema_term_ref_id or "",
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "source_ids",
            _identifier_tuple(self.source_ids, "/source_ids", maximum=MAX_GRAPH_SOURCES),
        )
        if (
            not self.appearance_term_ref_ids
            and not self.channel_measurement_ids
            and not self.texture_artifacts
        ):
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/appearance_term_ref_ids")
        if type(self.state) is not AssertionState:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/state")
        object.__setattr__(
            self,
            "provenance_ids",
            _identifier_tuple(
                self.provenance_ids,
                "/provenance_ids",
                maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "advisory_support",
            _advisory_support(self.advisory_support, "/advisory_support"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "appearance_id": self.appearance_id,
            "target_node_id": self.target_node_id,
            "appearance_term_ref_ids": list(self.appearance_term_ref_ids),
            "channel_measurement_ids": list(self.channel_measurement_ids),
            "texture_artifacts": [item.to_mapping() for item in self.texture_artifacts],
            "source_ids": list(self.source_ids),
            "state": self.state.value,
            "provenance_ids": list(self.provenance_ids),
            "extension_ids": list(self.extension_ids),
            "advisory_support": self.advisory_support,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class HypothesisAlternative:
    alternative_id: str
    member_refs: tuple[GraphElementRef, ...]
    advisory_support: float | None = None
    provenance_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "alternative_id",
            _identifier(self.alternative_id, "/alternative_id"),
        )
        object.__setattr__(
            self,
            "member_refs",
            _element_ref_tuple(self.member_refs, "/member_refs", maximum=64, minimum=1),
        )
        object.__setattr__(
            self,
            "advisory_support",
            _advisory_support(self.advisory_support, "/advisory_support"),
        )
        object.__setattr__(
            self,
            "provenance_ids",
            _identifier_tuple(
                self.provenance_ids,
                "/provenance_ids",
                maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "alternative_id": self.alternative_id,
            "member_refs": [item.to_mapping() for item in self.member_refs],
            "advisory_support": self.advisory_support,
            "provenance_ids": list(self.provenance_ids),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class HypothesisSet:
    hypothesis_set_id: str
    subject_refs: tuple[GraphElementRef, ...]
    alternatives: tuple[HypothesisAlternative, ...]
    term_ref_ids: tuple[str, ...] = ()
    provenance_ids: tuple[str, ...] = ()
    extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "hypothesis_set_id",
            _identifier(self.hypothesis_set_id, "/hypothesis_set_id"),
        )
        object.__setattr__(
            self,
            "subject_refs",
            _element_ref_tuple(
                self.subject_refs,
                "/subject_refs",
                maximum=_MAX_HYPOTHESIS_SUBJECTS,
                minimum=1,
            ),
        )
        if type(self.alternatives) is not tuple or len(self.alternatives) < 2:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/alternatives")
        if len(self.alternatives) > MAX_GRAPH_ALTERNATIVES_PER_SET:
            _fail(VisualFeatureGraphErrorCode.BUDGET_EXCEEDED, "/alternatives")
        if any(type(item) is not HypothesisAlternative for item in self.alternatives):
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/alternatives")
        alternatives = tuple(sorted(self.alternatives, key=lambda item: item.alternative_id))
        if len({item.alternative_id for item in alternatives}) != len(alternatives):
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/alternatives")
        members: set[tuple[GraphElementKind, str]] = set()
        for item in alternatives:
            current = {(ref.kind, ref.element_id) for ref in item.member_refs}
            if members.intersection(current):
                _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/alternatives")
            members.update(current)
        object.__setattr__(self, "alternatives", alternatives)
        object.__setattr__(
            self,
            "term_ref_ids",
            _identifier_tuple(
                self.term_ref_ids,
                "/term_ref_ids",
                maximum=MAX_GRAPH_TERMS_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "provenance_ids",
            _identifier_tuple(
                self.provenance_ids,
                "/provenance_ids",
                maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
            ),
        )
        object.__setattr__(
            self,
            "extension_ids",
            _identifier_tuple(
                self.extension_ids,
                "/extension_ids",
                maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "hypothesis_set_id": self.hypothesis_set_id,
            "subject_refs": [item.to_mapping() for item in self.subject_refs],
            "alternatives": [item.to_mapping() for item in self.alternatives],
            "term_ref_ids": list(self.term_ref_ids),
            "provenance_ids": list(self.provenance_ids),
            "extension_ids": list(self.extension_ids),
        }


def _exact_record_tuple[RecordT](
    value: object,
    record_type: type[RecordT],
    path: str,
    *,
    maximum: int,
    key: str,
) -> tuple[RecordT, ...]:
    if type(value) is not tuple:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    if len(value) > maximum:
        _fail(VisualFeatureGraphErrorCode.BUDGET_EXCEEDED, path)
    if any(type(item) is not record_type for item in value):
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    result = tuple(sorted(value, key=lambda item: getattr(item, key)))
    identifiers = tuple(getattr(item, key) for item in result)
    if len(set(identifiers)) != len(identifiers):
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    return result


def _validate_dag(
    identifiers: set[str],
    parents_by_id: dict[str, tuple[str, ...]],
    path: str,
) -> None:
    children: dict[str, list[str]] = {identifier: [] for identifier in identifiers}
    indegree = {identifier: 0 for identifier in identifiers}
    for identifier, parents in parents_by_id.items():
        for parent in parents:
            if parent not in identifiers:
                _fail(VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE, path)
            children[parent].append(identifier)
            indegree[identifier] += 1
    ready = [identifier for identifier, count in indegree.items() if count == 0]
    visited = 0
    while ready:
        identifier = ready.pop(0)
        visited += 1
        for child in children[identifier]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if visited != len(identifiers):
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualFeatureGraph:
    scope_id: str
    scope_version: int
    source_bundle_sha256: str
    producer_algorithm_id: str
    producer_algorithm_version: str
    producer_contract_sha256: str
    ontology_terms: tuple[OntologyTermRef, ...]
    extensions: tuple[ExtensionRef, ...] = ()
    provenance: tuple[ProvenanceRecord, ...] = ()
    sources: tuple[SourceArtifact, ...] = ()
    frames: tuple[CoordinateFrame, ...] = ()
    transforms: tuple[FrameTransformRef, ...] = ()
    geometries: tuple[GeometryRecord, ...] = ()
    nodes: tuple[FeatureNode, ...] = ()
    relations: tuple[FeatureRelation, ...] = ()
    equivalence_groups: tuple[EquivalenceGroup, ...] = ()
    measurements: tuple[MeasurementRecord, ...] = ()
    appearances: tuple[AppearanceRecord, ...] = ()
    hypothesis_sets: tuple[HypothesisSet, ...] = ()
    authority: VisualGraphAuthority = VisualGraphAuthority.ADVISORY_ONLY
    schema_version: int = VISUAL_FEATURE_GRAPH_SCHEMA_VERSION
    graph_id: str = field(init=False)
    graph_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != VISUAL_FEATURE_GRAPH_SCHEMA_VERSION
        ):
            _fail(VisualFeatureGraphErrorCode.UNSUPPORTED_VERSION, "/schema_version")
        if (
            type(self.authority) is not VisualGraphAuthority
            or self.authority is not VisualGraphAuthority.ADVISORY_ONLY
        ):
            _fail(VisualFeatureGraphErrorCode.AUTHORITY_VIOLATION, "/authority")
        object.__setattr__(self, "scope_id", _identifier(self.scope_id, "/scope_id"))
        object.__setattr__(
            self,
            "scope_version",
            _safe_integer(self.scope_version, "/scope_version", positive=True),
        )
        object.__setattr__(
            self,
            "source_bundle_sha256",
            _digest(self.source_bundle_sha256, "/source_bundle_sha256"),
        )
        object.__setattr__(
            self,
            "producer_algorithm_id",
            _identifier(self.producer_algorithm_id, "/producer_algorithm_id"),
        )
        object.__setattr__(
            self,
            "producer_algorithm_version",
            _version(self.producer_algorithm_version, "/producer_algorithm_version"),
        )
        object.__setattr__(
            self,
            "producer_contract_sha256",
            _digest(self.producer_contract_sha256, "/producer_contract_sha256"),
        )

        object.__setattr__(
            self,
            "ontology_terms",
            _exact_record_tuple(
                self.ontology_terms,
                OntologyTermRef,
                "/ontology_terms",
                maximum=MAX_GRAPH_ONTOLOGY_TERMS,
                key="term_ref_id",
            ),
        )
        term_identities = tuple(
            (item.namespace, item.vocabulary_version, item.term_id) for item in self.ontology_terms
        )
        if len(set(term_identities)) != len(term_identities):
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/ontology_terms")
        object.__setattr__(
            self,
            "extensions",
            _exact_record_tuple(
                self.extensions,
                ExtensionRef,
                "/extensions",
                maximum=MAX_GRAPH_EXTENSIONS,
                key="extension_id",
            ),
        )
        object.__setattr__(
            self,
            "provenance",
            _exact_record_tuple(
                self.provenance,
                ProvenanceRecord,
                "/provenance",
                maximum=MAX_GRAPH_PROVENANCE,
                key="provenance_id",
            ),
        )
        object.__setattr__(
            self,
            "sources",
            _exact_record_tuple(
                self.sources,
                SourceArtifact,
                "/sources",
                maximum=MAX_GRAPH_SOURCES,
                key="source_id",
            ),
        )
        object.__setattr__(
            self,
            "frames",
            _exact_record_tuple(
                self.frames,
                CoordinateFrame,
                "/frames",
                maximum=MAX_GRAPH_FRAMES,
                key="frame_id",
            ),
        )
        object.__setattr__(
            self,
            "transforms",
            _exact_record_tuple(
                self.transforms,
                FrameTransformRef,
                "/transforms",
                maximum=MAX_GRAPH_TRANSFORMS,
                key="transform_id",
            ),
        )
        object.__setattr__(
            self,
            "geometries",
            _exact_record_tuple(
                self.geometries,
                GeometryRecord,
                "/geometries",
                maximum=MAX_GRAPH_GEOMETRIES,
                key="geometry_id",
            ),
        )
        object.__setattr__(
            self,
            "nodes",
            _exact_record_tuple(
                self.nodes,
                FeatureNode,
                "/nodes",
                maximum=MAX_GRAPH_NODES,
                key="node_id",
            ),
        )
        object.__setattr__(
            self,
            "relations",
            _exact_record_tuple(
                self.relations,
                FeatureRelation,
                "/relations",
                maximum=MAX_GRAPH_RELATIONS,
                key="relation_id",
            ),
        )
        object.__setattr__(
            self,
            "equivalence_groups",
            _exact_record_tuple(
                self.equivalence_groups,
                EquivalenceGroup,
                "/equivalence_groups",
                maximum=MAX_GRAPH_EQUIVALENCE_GROUPS,
                key="group_id",
            ),
        )
        object.__setattr__(
            self,
            "measurements",
            _exact_record_tuple(
                self.measurements,
                MeasurementRecord,
                "/measurements",
                maximum=MAX_GRAPH_MEASUREMENTS,
                key="measurement_id",
            ),
        )
        object.__setattr__(
            self,
            "appearances",
            _exact_record_tuple(
                self.appearances,
                AppearanceRecord,
                "/appearances",
                maximum=MAX_GRAPH_APPEARANCES,
                key="appearance_id",
            ),
        )
        object.__setattr__(
            self,
            "hypothesis_sets",
            _exact_record_tuple(
                self.hypothesis_sets,
                HypothesisSet,
                "/hypothesis_sets",
                maximum=MAX_GRAPH_HYPOTHESIS_SETS,
                key="hypothesis_set_id",
            ),
        )

        total_samples = sum(len(item.samples) for item in self.geometries)
        total_cells = sum(len(item.cells) for item in self.geometries)
        total_alternatives = sum(len(item.alternatives) for item in self.hypothesis_sets)
        if total_samples > MAX_GRAPH_TOTAL_INLINE_SAMPLES:
            _fail(VisualFeatureGraphErrorCode.BUDGET_EXCEEDED, "/geometries")
        if total_cells > MAX_GRAPH_TOTAL_CELLS:
            _fail(VisualFeatureGraphErrorCode.BUDGET_EXCEEDED, "/geometries")
        if total_alternatives > MAX_GRAPH_TOTAL_ALTERNATIVES:
            _fail(VisualFeatureGraphErrorCode.BUDGET_EXCEEDED, "/hypothesis_sets")

        term_ids = {item.term_ref_id for item in self.ontology_terms}
        provenance_ids = {item.provenance_id for item in self.provenance}
        extension_ids = {item.extension_id for item in self.extensions}
        source_ids = {item.source_id for item in self.sources}
        frame_ids = {item.frame_id for item in self.frames}
        transform_ids = {item.transform_id for item in self.transforms}
        geometry_ids = {item.geometry_id for item in self.geometries}
        node_ids = {item.node_id for item in self.nodes}
        measurement_ids = {item.measurement_id for item in self.measurements}

        element_index: dict[str, GraphElementKind] = {}

        def register(kind: GraphElementKind, identifier: str, path: str) -> None:
            if identifier in element_index:
                _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
            element_index[identifier] = kind

        for item in self.extensions:
            register(GraphElementKind.EXTENSION, item.extension_id, "/extensions")
        for item in self.provenance:
            register(GraphElementKind.PROVENANCE, item.provenance_id, "/provenance")
        for item in self.sources:
            register(GraphElementKind.SOURCE, item.source_id, "/sources")
        for item in self.frames:
            register(GraphElementKind.FRAME, item.frame_id, "/frames")
        for item in self.transforms:
            register(GraphElementKind.TRANSFORM, item.transform_id, "/transforms")
        for item in self.geometries:
            register(GraphElementKind.GEOMETRY, item.geometry_id, "/geometries")
            for sample in item.samples:
                register(GraphElementKind.SAMPLE, sample.sample_id, "/geometries/samples")
            for cell in item.cells:
                register(GraphElementKind.CELL, cell.cell_id, "/geometries/cells")
        for item in self.nodes:
            register(GraphElementKind.NODE, item.node_id, "/nodes")
        for item in self.relations:
            register(GraphElementKind.RELATION, item.relation_id, "/relations")
        for item in self.equivalence_groups:
            register(GraphElementKind.EQUIVALENCE_GROUP, item.group_id, "/equivalence_groups")
        for item in self.measurements:
            register(GraphElementKind.MEASUREMENT, item.measurement_id, "/measurements")
        for item in self.appearances:
            register(GraphElementKind.APPEARANCE, item.appearance_id, "/appearances")
        for item in self.hypothesis_sets:
            register(GraphElementKind.HYPOTHESIS_SET, item.hypothesis_set_id, "/hypothesis_sets")
            for alternative in item.alternatives:
                register(
                    GraphElementKind.HYPOTHESIS_ALTERNATIVE,
                    alternative.alternative_id,
                    "/hypothesis_sets/alternatives",
                )

        term_ref_count = 0
        provenance_ref_count = 0
        extension_ref_count = 0

        def require_terms(values: tuple[str, ...], path: str) -> None:
            nonlocal term_ref_count
            if any(value not in term_ids for value in values):
                _fail(VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE, path)
            term_ref_count += len(values)

        def require_provenance(values: tuple[str, ...], path: str) -> None:
            nonlocal provenance_ref_count
            if any(value not in provenance_ids for value in values):
                _fail(VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE, path)
            provenance_ref_count += len(values)

        def require_extensions(values: tuple[str, ...], path: str) -> None:
            nonlocal extension_ref_count
            if any(value not in extension_ids for value in values):
                _fail(VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE, path)
            extension_ref_count += len(values)

        def require_content(value: ContentRef, path: str) -> None:
            if value.schema_term_ref_id is not None:
                require_terms((value.schema_term_ref_id,), path)

        def require_element(value: GraphElementRef, path: str) -> None:
            if element_index.get(value.element_id) is not value.kind:
                _fail(VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE, path)

        for item in self.extensions:
            require_terms((item.schema_term_ref_id,), "/extensions/schema_term_ref_id")
            require_content(item.payload, "/extensions/payload")
        for item in self.provenance:
            require_content(item.content, "/provenance/content")
            require_terms(item.term_ref_ids, "/provenance/term_ref_ids")
            require_provenance(item.parent_provenance_ids, "/provenance/parent_provenance_ids")
            require_extensions(item.extension_ids, "/provenance/extension_ids")
            if any(source_id not in source_ids for source_id in item.source_ids):
                _fail(VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE, "/provenance/source_ids")
        for item in self.sources:
            require_content(item.content, "/sources/content")
            require_terms(item.modality_term_ref_ids, "/sources/modality_term_ref_ids")
            require_provenance(item.provenance_ids, "/sources/provenance_ids")
            require_extensions(item.extension_ids, "/sources/extension_ids")
            if item.parent_source_id is not None and item.parent_source_id not in source_ids:
                _fail(VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE, "/sources/parent_source_id")

        _validate_dag(
            provenance_ids,
            {item.provenance_id: item.parent_provenance_ids for item in self.provenance},
            "/provenance/parent_provenance_ids",
        )
        _validate_dag(
            source_ids,
            {
                item.source_id: (() if item.parent_source_id is None else (item.parent_source_id,))
                for item in self.sources
            },
            "/sources/parent_source_id",
        )

        frame_by_id = {item.frame_id: item for item in self.frames}
        transform_by_id = {item.transform_id: item for item in self.transforms}
        for item in self.frames:
            if item.source_id is not None and item.source_id not in source_ids:
                _fail(VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE, "/frames/source_id")
            if type(item.binding) is SourcePixelFrameBinding:
                source = next(value for value in self.sources if value.source_id == item.source_id)
                if item.binding.source_sha256 != source.content.sha256:
                    _fail(VisualFeatureGraphErrorCode.BINDING_MISMATCH, "/frames/binding")
            require_terms(item.term_ref_ids, "/frames/term_ref_ids")
            require_provenance(item.provenance_ids, "/frames/provenance_ids")
            require_extensions(item.extension_ids, "/frames/extension_ids")
            if type(item.binding) is MetricSpaceFrameBinding:
                require_terms(item.binding.axis_term_ref_ids, "/frames/binding/axis_term_ref_ids")
            if type(item.binding) is GenericFrameBinding:
                require_terms(
                    (item.binding.coordinate_system_term_ref_id,)
                    + item.binding.axis_term_ref_ids
                    + item.binding.unit_term_ref_ids,
                    "/frames/binding",
                )
        for item in self.transforms:
            if item.from_frame_id not in frame_ids or item.to_frame_id not in frame_ids:
                _fail(VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE, "/transforms")
            require_terms((item.transform_term_ref_id,), "/transforms/transform_term_ref_id")
            require_content(item.receipt, "/transforms/receipt")
            require_provenance(item.provenance_ids, "/transforms/provenance_ids")
            require_extensions(item.extension_ids, "/transforms/extension_ids")
            if (
                item.uncertainty_measurement_id is not None
                and item.uncertainty_measurement_id not in measurement_ids
            ):
                _fail(
                    VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE,
                    "/transforms/uncertainty_measurement_id",
                )

        for item in self.geometries:
            frame = frame_by_id.get(item.frame_id)
            if frame is None:
                _fail(VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE, "/geometries/frame_id")
            require_terms(
                (item.representation_term_ref_id,) + item.term_ref_ids,
                "/geometries/term_ref_ids",
            )
            require_provenance(item.provenance_ids, "/geometries/provenance_ids")
            require_extensions(item.extension_ids, "/geometries/extension_ids")
            if item.artifact is not None:
                require_content(item.artifact, "/geometries/artifact")
            if item.intrinsic_dimension > frame.dimension:
                _fail(VisualFeatureGraphErrorCode.BINDING_MISMATCH, "/geometries")
            for sample in item.samples:
                if len(sample.coordinates) != frame.dimension:
                    _fail(VisualFeatureGraphErrorCode.BINDING_MISMATCH, "/geometries/samples")
                if type(frame.binding) is OverviewNormalizedFrameBinding and any(
                    not 0.0 <= coordinate <= 1.0 for coordinate in sample.coordinates
                ):
                    _fail(VisualFeatureGraphErrorCode.BINDING_MISMATCH, "/geometries/samples")
                if type(frame.binding) is SourcePixelFrameBinding:
                    x, y = sample.coordinates
                    if not 0.0 <= x <= frame.binding.width - 1 or not (
                        0.0 <= y <= frame.binding.height - 1
                    ):
                        _fail(
                            VisualFeatureGraphErrorCode.BINDING_MISMATCH,
                            "/geometries/samples",
                        )
                require_terms(sample.term_ref_ids, "/geometries/samples/term_ref_ids")
                require_provenance(
                    sample.provenance_ids,
                    "/geometries/samples/provenance_ids",
                )
                require_extensions(sample.extension_ids, "/geometries/samples/extension_ids")
                if sample.uncertainty.artifact is not None:
                    require_content(sample.uncertainty.artifact, "/geometries/samples/uncertainty")
            for cell in item.cells:
                require_terms((cell.cell_term_ref_id,), "/geometries/cells/cell_term_ref_id")
                require_provenance(cell.provenance_ids, "/geometries/cells/provenance_ids")
                require_extensions(cell.extension_ids, "/geometries/cells/extension_ids")

        for item in self.nodes:
            require_terms(item.term_ref_ids, "/nodes/term_ref_ids")
            require_provenance(item.provenance_ids, "/nodes/provenance_ids")
            require_extensions(item.extension_ids, "/nodes/extension_ids")
            if any(value not in geometry_ids for value in item.geometry_ids):
                _fail(VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE, "/nodes/geometry_ids")
            if any(value not in source_ids for value in item.source_ids):
                _fail(VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE, "/nodes/source_ids")

        relation_signatures: set[tuple[object, ...]] = set()
        for item in self.relations:
            require_terms((item.relation_term_ref_id,), "/relations/relation_term_ref_id")
            require_terms(
                tuple(endpoint.role_term_ref_id for endpoint in item.endpoints),
                "/relations/endpoints/role_term_ref_id",
            )
            require_provenance(item.provenance_ids, "/relations/provenance_ids")
            require_extensions(item.extension_ids, "/relations/extension_ids")
            for endpoint in item.endpoints:
                require_element(endpoint.element, "/relations/endpoints/element")
                if endpoint.element.element_id == item.relation_id:
                    _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/relations/endpoints")
            signature = (
                item.relation_term_ref_id,
                tuple(
                    (
                        endpoint.ordinal,
                        endpoint.role_term_ref_id,
                        endpoint.element.kind.value,
                        endpoint.element.element_id,
                    )
                    for endpoint in item.endpoints
                ),
            )
            if signature in relation_signatures:
                _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/relations")
            relation_signatures.add(signature)

        equivalent_nodes: set[str] = set()
        for item in self.equivalence_groups:
            if any(value not in node_ids for value in item.member_node_ids):
                _fail(
                    VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE,
                    "/equivalence_groups/member_node_ids",
                )
            if equivalent_nodes.intersection(item.member_node_ids):
                _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, "/equivalence_groups")
            equivalent_nodes.update(item.member_node_ids)
            require_provenance(item.provenance_ids, "/equivalence_groups/provenance_ids")
            require_extensions(item.extension_ids, "/equivalence_groups/extension_ids")

        for item in self.measurements:
            require_terms(
                (item.quantity_term_ref_id, item.unit_term_ref_id),
                "/measurements",
            )
            require_provenance(item.provenance_ids, "/measurements/provenance_ids")
            require_extensions(item.extension_ids, "/measurements/extension_ids")
            for target in item.targets:
                require_element(target, "/measurements/targets")
            if any(value not in frame_ids for value in item.frame_ids):
                _fail(VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE, "/measurements/frame_ids")
            if any(value not in transform_ids for value in item.transform_ids):
                _fail(
                    VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE,
                    "/measurements/transform_ids",
                )
            if item.estimate.artifact is not None:
                require_content(item.estimate.artifact, "/measurements/estimate/artifact")
            if len(item.frame_ids) > 1:
                adjacency = {frame_id: set() for frame_id in item.frame_ids}
                for transform_id in item.transform_ids:
                    transform = transform_by_id[transform_id]
                    if (
                        transform.from_frame_id not in adjacency
                        or transform.to_frame_id not in adjacency
                    ):
                        _fail(
                            VisualFeatureGraphErrorCode.BINDING_MISMATCH,
                            "/measurements/transform_ids",
                        )
                    adjacency[transform.from_frame_id].add(transform.to_frame_id)
                    adjacency[transform.to_frame_id].add(transform.from_frame_id)
                reached = {item.frame_ids[0]}
                pending = [item.frame_ids[0]]
                while pending:
                    current = pending.pop()
                    for neighbor in sorted(adjacency[current]):
                        if neighbor not in reached:
                            reached.add(neighbor)
                            pending.append(neighbor)
                if reached != set(item.frame_ids):
                    _fail(
                        VisualFeatureGraphErrorCode.BINDING_MISMATCH,
                        "/measurements/transform_ids",
                    )

        for item in self.appearances:
            if item.target_node_id not in node_ids:
                _fail(VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE, "/appearances/target_node_id")
            require_terms(item.appearance_term_ref_ids, "/appearances/appearance_term_ref_ids")
            if any(value not in measurement_ids for value in item.channel_measurement_ids):
                _fail(
                    VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE,
                    "/appearances/channel_measurement_ids",
                )
            if any(value not in source_ids for value in item.source_ids):
                _fail(VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE, "/appearances/source_ids")
            for texture in item.texture_artifacts:
                require_content(texture, "/appearances/texture_artifacts")
            require_provenance(item.provenance_ids, "/appearances/provenance_ids")
            require_extensions(item.extension_ids, "/appearances/extension_ids")

        hypothesis_members: set[tuple[GraphElementKind, str]] = set()
        forbidden_hypothesis_members = {
            GraphElementKind.HYPOTHESIS_SET,
            GraphElementKind.HYPOTHESIS_ALTERNATIVE,
        }
        for item in self.hypothesis_sets:
            require_terms(item.term_ref_ids, "/hypothesis_sets/term_ref_ids")
            require_provenance(item.provenance_ids, "/hypothesis_sets/provenance_ids")
            require_extensions(item.extension_ids, "/hypothesis_sets/extension_ids")
            for subject in item.subject_refs:
                require_element(subject, "/hypothesis_sets/subject_refs")
            for alternative in item.alternatives:
                require_provenance(
                    alternative.provenance_ids,
                    "/hypothesis_sets/alternatives/provenance_ids",
                )
                for member in alternative.member_refs:
                    require_element(member, "/hypothesis_sets/alternatives/member_refs")
                    if member.kind in forbidden_hypothesis_members:
                        _fail(
                            VisualFeatureGraphErrorCode.INVALID_INPUT,
                            "/hypothesis_sets/alternatives/member_refs",
                        )
                    key = (member.kind, member.element_id)
                    if key in hypothesis_members:
                        _fail(
                            VisualFeatureGraphErrorCode.INVALID_INPUT,
                            "/hypothesis_sets/alternatives/member_refs",
                        )
                    hypothesis_members.add(key)

        if term_ref_count > MAX_GRAPH_TOTAL_TERM_REFS:
            _fail(VisualFeatureGraphErrorCode.BUDGET_EXCEEDED, "/ontology_terms")
        if provenance_ref_count > MAX_GRAPH_TOTAL_PROVENANCE_REFS:
            _fail(VisualFeatureGraphErrorCode.BUDGET_EXCEEDED, "/provenance")
        if extension_ref_count > MAX_GRAPH_TOTAL_EXTENSION_REFS:
            _fail(VisualFeatureGraphErrorCode.BUDGET_EXCEEDED, "/extensions")

        body = self._body_mapping()
        digest = hashlib.sha256(_GRAPH_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()
        graph_id = (
            "visual_feature_graph_"
            + hashlib.sha256(_GRAPH_ID_DOMAIN + bytes.fromhex(digest)).hexdigest()[:32]
        )
        object.__setattr__(self, "graph_digest", digest)
        object.__setattr__(self, "graph_id", graph_id)
        _canonical_json(self.to_mapping())

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority": self.authority.value,
            "scope_id": self.scope_id,
            "scope_version": self.scope_version,
            "source_bundle_sha256": self.source_bundle_sha256,
            "producer_algorithm_id": self.producer_algorithm_id,
            "producer_algorithm_version": self.producer_algorithm_version,
            "producer_contract_sha256": self.producer_contract_sha256,
            "ontology_terms": [item.to_mapping() for item in self.ontology_terms],
            "extensions": [item.to_mapping() for item in self.extensions],
            "provenance": [item.to_mapping() for item in self.provenance],
            "sources": [item.to_mapping() for item in self.sources],
            "frames": [item.to_mapping() for item in self.frames],
            "transforms": [item.to_mapping() for item in self.transforms],
            "geometries": [item.to_mapping() for item in self.geometries],
            "nodes": [item.to_mapping() for item in self.nodes],
            "relations": [item.to_mapping() for item in self.relations],
            "equivalence_groups": [item.to_mapping() for item in self.equivalence_groups],
            "measurements": [item.to_mapping() for item in self.measurements],
            "appearances": [item.to_mapping() for item in self.appearances],
            "hypothesis_sets": [item.to_mapping() for item in self.hypothesis_sets],
        }

    def to_mapping(self) -> dict[str, object]:
        return self._body_mapping() | {
            "graph_id": self.graph_id,
            "graph_digest": self.graph_digest,
        }


def encode_visual_feature_graph(value: object) -> bytes:
    if type(value) is not VisualFeatureGraph:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT)
    return _canonical_json(value.to_mapping())


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _parse_int(value: str) -> int:
    result = int(value)
    if not -_MAX_SAFE_INTEGER <= result <= _MAX_SAFE_INTEGER:
        raise ValueError
    return result


def _parse_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError


def _validate_json_tree(value: object) -> None:
    nodes = 0

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            _fail(VisualFeatureGraphErrorCode.BUDGET_EXCEEDED)
        if type(item) is str:
            try:
                size = len(item.encode("utf-8"))
            except UnicodeError:
                _fail(VisualFeatureGraphErrorCode.INVALID_INPUT)
            if size > _MAX_JSON_STRING_BYTES:
                _fail(VisualFeatureGraphErrorCode.BUDGET_EXCEEDED)
        elif type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    _fail(VisualFeatureGraphErrorCode.INVALID_INPUT)
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif type(item) is list:
            for child in item:
                visit(child, depth + 1)
        elif type(item) not in {int, float, bool, type(None)}:
            _fail(VisualFeatureGraphErrorCode.INVALID_INPUT)

    visit(value, 0)


def _decode_json(raw: object) -> dict[str, object]:
    if type(raw) is not bytes or not raw:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT)
    if len(raw) > MAX_VISUAL_FEATURE_GRAPH_BYTES:
        _fail(VisualFeatureGraphErrorCode.BUDGET_EXCEEDED)
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_int=_parse_int,
            parse_float=_parse_float,
            parse_constant=_reject_constant,
        )
    except (
        _DuplicateKeyError,
        UnicodeDecodeError,
        ValueError,
        RecursionError,
        OverflowError,
    ):
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT)
    _validate_json_tree(value)
    if type(value) is not dict:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT)
    return value


def _exact_mapping(value: object, fields: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    return value


def _exact_list(value: object, path: str, *, maximum: int) -> list[object]:
    if type(value) is not list:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    if len(value) > maximum:
        _fail(VisualFeatureGraphErrorCode.BUDGET_EXCEEDED, path)
    return value


def _enum_value[EnumT: StrEnum](
    value: object,
    enum_type: type[EnumT],
    path: str,
) -> EnumT:
    if type(value) is not str:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    try:
        return enum_type(value)
    except ValueError:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)


def _tuple_strings(value: object, path: str, *, maximum: int) -> tuple[str, ...]:
    raw = _exact_list(value, path, maximum=maximum)
    if any(type(item) is not str for item in raw):
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    return tuple(raw)


def _tuple_numbers(value: object, path: str, *, maximum: int) -> tuple[float, ...]:
    raw = _exact_list(value, path, maximum=maximum)
    if any(type(item) not in {int, float} for item in raw):
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    return tuple(raw)


def _decode_content(value: object, path: str) -> ContentRef:
    data = _exact_mapping(
        value,
        {"sha256", "size_bytes", "media_type", "schema_term_ref_id"},
        path,
    )
    return ContentRef(
        sha256=data["sha256"],
        size_bytes=data["size_bytes"],
        media_type=data["media_type"],
        schema_term_ref_id=data["schema_term_ref_id"],
    )


def _decode_optional_content(value: object, path: str) -> ContentRef | None:
    return None if value is None else _decode_content(value, path)


def _decode_term(value: object, path: str) -> OntologyTermRef:
    data = _exact_mapping(
        value,
        {
            "term_ref_id",
            "namespace",
            "vocabulary_version",
            "term_id",
            "term_definition_sha256",
        },
        path,
    )
    return OntologyTermRef(
        term_ref_id=data["term_ref_id"],
        namespace=data["namespace"],
        vocabulary_version=data["vocabulary_version"],
        term_id=data["term_id"],
        term_definition_sha256=data["term_definition_sha256"],
    )


def _decode_extension(value: object, path: str) -> ExtensionRef:
    data = _exact_mapping(
        value,
        {
            "extension_id",
            "namespace",
            "vocabulary_version",
            "schema_term_ref_id",
            "payload",
        },
        path,
    )
    return ExtensionRef(
        extension_id=data["extension_id"],
        namespace=data["namespace"],
        vocabulary_version=data["vocabulary_version"],
        schema_term_ref_id=data["schema_term_ref_id"],
        payload=_decode_content(data["payload"], f"{path}/payload"),
    )


def _decode_provenance(value: object, path: str) -> ProvenanceRecord:
    data = _exact_mapping(
        value,
        {
            "provenance_id",
            "kind",
            "content",
            "producer_id",
            "producer_version",
            "source_ids",
            "parent_provenance_ids",
            "term_ref_ids",
            "extension_ids",
        },
        path,
    )
    return ProvenanceRecord(
        provenance_id=data["provenance_id"],
        kind=_enum_value(data["kind"], ProvenanceKind, f"{path}/kind"),
        content=_decode_content(data["content"], f"{path}/content"),
        producer_id=data["producer_id"],
        producer_version=data["producer_version"],
        source_ids=_tuple_strings(data["source_ids"], f"{path}/source_ids", maximum=16),
        parent_provenance_ids=_tuple_strings(
            data["parent_provenance_ids"],
            f"{path}/parent_provenance_ids",
            maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
        ),
        term_ref_ids=_tuple_strings(
            data["term_ref_ids"],
            f"{path}/term_ref_ids",
            maximum=MAX_GRAPH_TERMS_PER_ELEMENT,
        ),
        extension_ids=_tuple_strings(
            data["extension_ids"],
            f"{path}/extension_ids",
            maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
        ),
    )


def _decode_source(value: object, path: str) -> SourceArtifact:
    data = _exact_mapping(
        value,
        {
            "source_id",
            "content",
            "modality_term_ref_ids",
            "parent_source_id",
            "sequence_index",
            "provenance_ids",
            "extension_ids",
        },
        path,
    )
    return SourceArtifact(
        source_id=data["source_id"],
        content=_decode_content(data["content"], f"{path}/content"),
        modality_term_ref_ids=_tuple_strings(
            data["modality_term_ref_ids"],
            f"{path}/modality_term_ref_ids",
            maximum=MAX_GRAPH_TERMS_PER_ELEMENT,
        ),
        parent_source_id=data["parent_source_id"],
        sequence_index=data["sequence_index"],
        provenance_ids=_tuple_strings(
            data["provenance_ids"],
            f"{path}/provenance_ids",
            maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
        ),
        extension_ids=_tuple_strings(
            data["extension_ids"],
            f"{path}/extension_ids",
            maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
        ),
    )


def _decode_frame_binding(value: object, path: str) -> FrameBinding:
    if type(value) is not dict or type(value.get("kind")) is not str:
        _fail(VisualFeatureGraphErrorCode.INVALID_INPUT, path)
    kind = _enum_value(value["kind"], FrameKind, f"{path}/kind")
    if kind is FrameKind.OVERVIEW_NORMALIZED:
        data = _exact_mapping(
            value,
            {
                "kind",
                "collection_id",
                "collection_manifest_sha256",
                "derivation_manifest_sha256",
                "provider_asset_id",
                "provider_asset_sha256",
                "width",
                "height",
            },
            path,
        )
        return OverviewNormalizedFrameBinding(
            collection_id=data["collection_id"],
            collection_manifest_sha256=data["collection_manifest_sha256"],
            derivation_manifest_sha256=data["derivation_manifest_sha256"],
            provider_asset_id=data["provider_asset_id"],
            provider_asset_sha256=data["provider_asset_sha256"],
            width=data["width"],
            height=data["height"],
            kind=kind,
        )
    if kind is FrameKind.SOURCE_PIXEL:
        data = _exact_mapping(value, {"kind", "source_sha256", "width", "height"}, path)
        return SourcePixelFrameBinding(
            source_sha256=data["source_sha256"],
            width=data["width"],
            height=data["height"],
            kind=kind,
        )
    if kind is FrameKind.METRIC_PLANE:
        data = _exact_mapping(
            value,
            {
                "kind",
                "frame_record_sha256",
                "calibration_receipt_sha256",
                "calibration_sha256",
                "unit",
            },
            path,
        )
        return MetricPlaneFrameBinding(
            frame_record_sha256=data["frame_record_sha256"],
            calibration_receipt_sha256=data["calibration_receipt_sha256"],
            calibration_sha256=data["calibration_sha256"],
            unit=data["unit"],
            kind=kind,
        )
    if kind is FrameKind.METRIC_SPACE:
        data = _exact_mapping(
            value,
            {"kind", "frame_record_sha256", "handedness", "axis_term_ref_ids", "unit"},
            path,
        )
        axes = _tuple_strings(
            data["axis_term_ref_ids"],
            f"{path}/axis_term_ref_ids",
            maximum=3,
        )
        return MetricSpaceFrameBinding(
            frame_record_sha256=data["frame_record_sha256"],
            handedness=_enum_value(data["handedness"], Handedness, f"{path}/handedness"),
            axis_term_ref_ids=axes,
            unit=data["unit"],
            kind=kind,
        )
    data = _exact_mapping(
        value,
        {
            "kind",
            "frame_record_sha256",
            "dimension",
            "coordinate_system_term_ref_id",
            "axis_term_ref_ids",
            "unit_term_ref_ids",
        },
        path,
    )
    return GenericFrameBinding(
        frame_record_sha256=data["frame_record_sha256"],
        dimension=data["dimension"],
        coordinate_system_term_ref_id=data["coordinate_system_term_ref_id"],
        axis_term_ref_ids=_tuple_strings(
            data["axis_term_ref_ids"],
            f"{path}/axis_term_ref_ids",
            maximum=4,
        ),
        unit_term_ref_ids=_tuple_strings(
            data["unit_term_ref_ids"],
            f"{path}/unit_term_ref_ids",
            maximum=4,
        ),
        kind=kind,
    )


def _decode_frame(value: object, path: str) -> CoordinateFrame:
    data = _exact_mapping(
        value,
        {"frame_id", "binding", "source_id", "term_ref_ids", "provenance_ids", "extension_ids"},
        path,
    )
    return CoordinateFrame(
        frame_id=data["frame_id"],
        binding=_decode_frame_binding(data["binding"], f"{path}/binding"),
        source_id=data["source_id"],
        term_ref_ids=_tuple_strings(
            data["term_ref_ids"], f"{path}/term_ref_ids", maximum=MAX_GRAPH_TERMS_PER_ELEMENT
        ),
        provenance_ids=_tuple_strings(
            data["provenance_ids"],
            f"{path}/provenance_ids",
            maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
        ),
        extension_ids=_tuple_strings(
            data["extension_ids"],
            f"{path}/extension_ids",
            maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
        ),
    )


def _decode_transform(value: object, path: str) -> FrameTransformRef:
    data = _exact_mapping(
        value,
        {
            "transform_id",
            "from_frame_id",
            "to_frame_id",
            "transform_term_ref_id",
            "receipt",
            "uncertainty_measurement_id",
            "provenance_ids",
            "extension_ids",
        },
        path,
    )
    return FrameTransformRef(
        transform_id=data["transform_id"],
        from_frame_id=data["from_frame_id"],
        to_frame_id=data["to_frame_id"],
        transform_term_ref_id=data["transform_term_ref_id"],
        receipt=_decode_content(data["receipt"], f"{path}/receipt"),
        uncertainty_measurement_id=data["uncertainty_measurement_id"],
        provenance_ids=_tuple_strings(
            data["provenance_ids"],
            f"{path}/provenance_ids",
            maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
        ),
        extension_ids=_tuple_strings(
            data["extension_ids"],
            f"{path}/extension_ids",
            maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
        ),
    )


def _decode_uncertainty(value: object, path: str) -> MetricUncertainty:
    data = _exact_mapping(value, {"kind", "bounds", "covariance", "artifact"}, path)
    return MetricUncertainty(
        kind=_enum_value(data["kind"], MetricUncertaintyKind, f"{path}/kind"),
        bounds=_tuple_numbers(data["bounds"], f"{path}/bounds", maximum=4),
        covariance=_tuple_numbers(data["covariance"], f"{path}/covariance", maximum=16),
        artifact=_decode_optional_content(data["artifact"], f"{path}/artifact"),
    )


def _decode_sample(value: object, path: str) -> CoordinateSample:
    data = _exact_mapping(
        value,
        {
            "sample_id",
            "coordinates",
            "uncertainty",
            "term_ref_ids",
            "provenance_ids",
            "extension_ids",
        },
        path,
    )
    return CoordinateSample(
        sample_id=data["sample_id"],
        coordinates=_tuple_numbers(data["coordinates"], f"{path}/coordinates", maximum=4),
        uncertainty=_decode_uncertainty(data["uncertainty"], f"{path}/uncertainty"),
        term_ref_ids=_tuple_strings(
            data["term_ref_ids"], f"{path}/term_ref_ids", maximum=MAX_GRAPH_TERMS_PER_ELEMENT
        ),
        provenance_ids=_tuple_strings(
            data["provenance_ids"],
            f"{path}/provenance_ids",
            maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
        ),
        extension_ids=_tuple_strings(
            data["extension_ids"],
            f"{path}/extension_ids",
            maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
        ),
    )


def _decode_cell(value: object, path: str) -> TopologyCell:
    data = _exact_mapping(
        value,
        {
            "cell_id",
            "cell_term_ref_id",
            "sample_ids",
            "orientation",
            "provenance_ids",
            "extension_ids",
        },
        path,
    )
    return TopologyCell(
        cell_id=data["cell_id"],
        cell_term_ref_id=data["cell_term_ref_id"],
        sample_ids=_tuple_strings(
            data["sample_ids"], f"{path}/sample_ids", maximum=MAX_GRAPH_CELL_VERTICES
        ),
        orientation=_enum_value(data["orientation"], CellOrientation, f"{path}/orientation"),
        provenance_ids=_tuple_strings(
            data["provenance_ids"],
            f"{path}/provenance_ids",
            maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
        ),
        extension_ids=_tuple_strings(
            data["extension_ids"],
            f"{path}/extension_ids",
            maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
        ),
    )


def _decode_geometry(value: object, path: str) -> GeometryRecord:
    data = _exact_mapping(
        value,
        {
            "geometry_id",
            "frame_id",
            "representation_term_ref_id",
            "intrinsic_dimension",
            "samples",
            "cells",
            "artifact",
            "closure",
            "state",
            "term_ref_ids",
            "provenance_ids",
            "extension_ids",
            "advisory_support",
        },
        path,
    )
    samples = _exact_list(
        data["samples"], f"{path}/samples", maximum=MAX_GRAPH_INLINE_SAMPLES_PER_GEOMETRY
    )
    cells = _exact_list(data["cells"], f"{path}/cells", maximum=MAX_GRAPH_CELLS_PER_GEOMETRY)
    return GeometryRecord(
        geometry_id=data["geometry_id"],
        frame_id=data["frame_id"],
        representation_term_ref_id=data["representation_term_ref_id"],
        intrinsic_dimension=data["intrinsic_dimension"],
        samples=tuple(
            _decode_sample(item, f"{path}/samples/{index}") for index, item in enumerate(samples)
        ),
        cells=tuple(
            _decode_cell(item, f"{path}/cells/{index}") for index, item in enumerate(cells)
        ),
        artifact=_decode_optional_content(data["artifact"], f"{path}/artifact"),
        closure=_enum_value(data["closure"], ClosureState, f"{path}/closure"),
        state=_enum_value(data["state"], AssertionState, f"{path}/state"),
        term_ref_ids=_tuple_strings(
            data["term_ref_ids"], f"{path}/term_ref_ids", maximum=MAX_GRAPH_TERMS_PER_ELEMENT
        ),
        provenance_ids=_tuple_strings(
            data["provenance_ids"],
            f"{path}/provenance_ids",
            maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
        ),
        extension_ids=_tuple_strings(
            data["extension_ids"],
            f"{path}/extension_ids",
            maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
        ),
        advisory_support=data["advisory_support"],
    )


def _decode_node(value: object, path: str) -> FeatureNode:
    data = _exact_mapping(
        value,
        {
            "node_id",
            "layer",
            "term_ref_ids",
            "geometry_ids",
            "source_ids",
            "state",
            "provenance_ids",
            "extension_ids",
            "advisory_support",
        },
        path,
    )
    return FeatureNode(
        node_id=data["node_id"],
        layer=_enum_value(data["layer"], EntityLayer, f"{path}/layer"),
        term_ref_ids=_tuple_strings(
            data["term_ref_ids"], f"{path}/term_ref_ids", maximum=MAX_GRAPH_TERMS_PER_ELEMENT
        ),
        geometry_ids=_tuple_strings(
            data["geometry_ids"], f"{path}/geometry_ids", maximum=MAX_GRAPH_GEOMETRIES
        ),
        source_ids=_tuple_strings(
            data["source_ids"], f"{path}/source_ids", maximum=MAX_GRAPH_SOURCES
        ),
        state=_enum_value(data["state"], AssertionState, f"{path}/state"),
        provenance_ids=_tuple_strings(
            data["provenance_ids"],
            f"{path}/provenance_ids",
            maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
        ),
        extension_ids=_tuple_strings(
            data["extension_ids"],
            f"{path}/extension_ids",
            maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
        ),
        advisory_support=data["advisory_support"],
    )


def _decode_element_ref(value: object, path: str) -> GraphElementRef:
    data = _exact_mapping(value, {"kind", "element_id"}, path)
    return GraphElementRef(
        kind=_enum_value(data["kind"], GraphElementKind, f"{path}/kind"),
        element_id=data["element_id"],
    )


def _decode_endpoint(value: object, path: str) -> RelationEndpoint:
    data = _exact_mapping(value, {"ordinal", "role_term_ref_id", "element"}, path)
    return RelationEndpoint(
        ordinal=data["ordinal"],
        role_term_ref_id=data["role_term_ref_id"],
        element=_decode_element_ref(data["element"], f"{path}/element"),
    )


def _decode_relation(value: object, path: str) -> FeatureRelation:
    data = _exact_mapping(
        value,
        {
            "relation_id",
            "relation_term_ref_id",
            "endpoints",
            "state",
            "provenance_ids",
            "extension_ids",
            "advisory_support",
        },
        path,
    )
    endpoints = _exact_list(data["endpoints"], f"{path}/endpoints", maximum=16)
    return FeatureRelation(
        relation_id=data["relation_id"],
        relation_term_ref_id=data["relation_term_ref_id"],
        endpoints=tuple(
            _decode_endpoint(item, f"{path}/endpoints/{index}")
            for index, item in enumerate(endpoints)
        ),
        state=_enum_value(data["state"], AssertionState, f"{path}/state"),
        provenance_ids=_tuple_strings(
            data["provenance_ids"],
            f"{path}/provenance_ids",
            maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
        ),
        extension_ids=_tuple_strings(
            data["extension_ids"],
            f"{path}/extension_ids",
            maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
        ),
        advisory_support=data["advisory_support"],
    )


def _decode_equivalence(value: object, path: str) -> EquivalenceGroup:
    data = _exact_mapping(
        value,
        {
            "group_id",
            "member_node_ids",
            "state",
            "provenance_ids",
            "extension_ids",
            "advisory_support",
        },
        path,
    )
    return EquivalenceGroup(
        group_id=data["group_id"],
        member_node_ids=_tuple_strings(
            data["member_node_ids"], f"{path}/member_node_ids", maximum=MAX_GRAPH_NODES
        ),
        state=_enum_value(data["state"], AssertionState, f"{path}/state"),
        provenance_ids=_tuple_strings(
            data["provenance_ids"],
            f"{path}/provenance_ids",
            maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
        ),
        extension_ids=_tuple_strings(
            data["extension_ids"],
            f"{path}/extension_ids",
            maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
        ),
        advisory_support=data["advisory_support"],
    )


def _decode_estimate(value: object, path: str) -> MeasurementEstimate:
    data = _exact_mapping(
        value,
        {"kind", "central", "lower", "upper", "covariance", "artifact"},
        path,
    )
    return MeasurementEstimate(
        kind=_enum_value(data["kind"], MeasurementEstimateKind, f"{path}/kind"),
        central=_tuple_numbers(
            data["central"], f"{path}/central", maximum=_MAX_MEASUREMENT_DIMENSION
        ),
        lower=_tuple_numbers(data["lower"], f"{path}/lower", maximum=_MAX_MEASUREMENT_DIMENSION),
        upper=_tuple_numbers(data["upper"], f"{path}/upper", maximum=_MAX_MEASUREMENT_DIMENSION),
        covariance=_tuple_numbers(
            data["covariance"],
            f"{path}/covariance",
            maximum=_MAX_MEASUREMENT_DIMENSION**2,
        ),
        artifact=_decode_optional_content(data["artifact"], f"{path}/artifact"),
    )


def _decode_measurement(value: object, path: str) -> MeasurementRecord:
    data = _exact_mapping(
        value,
        {
            "measurement_id",
            "quantity_term_ref_id",
            "unit_term_ref_id",
            "targets",
            "estimate",
            "frame_ids",
            "transform_ids",
            "state",
            "provenance_ids",
            "extension_ids",
            "advisory_support",
        },
        path,
    )
    targets = _exact_list(data["targets"], f"{path}/targets", maximum=16)
    return MeasurementRecord(
        measurement_id=data["measurement_id"],
        quantity_term_ref_id=data["quantity_term_ref_id"],
        unit_term_ref_id=data["unit_term_ref_id"],
        targets=tuple(
            _decode_element_ref(item, f"{path}/targets/{index}")
            for index, item in enumerate(targets)
        ),
        estimate=_decode_estimate(data["estimate"], f"{path}/estimate"),
        frame_ids=_tuple_strings(data["frame_ids"], f"{path}/frame_ids", maximum=MAX_GRAPH_FRAMES),
        transform_ids=_tuple_strings(
            data["transform_ids"], f"{path}/transform_ids", maximum=MAX_GRAPH_TRANSFORMS
        ),
        state=_enum_value(data["state"], AssertionState, f"{path}/state"),
        provenance_ids=_tuple_strings(
            data["provenance_ids"],
            f"{path}/provenance_ids",
            maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
        ),
        extension_ids=_tuple_strings(
            data["extension_ids"],
            f"{path}/extension_ids",
            maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
        ),
        advisory_support=data["advisory_support"],
    )


def _decode_appearance(value: object, path: str) -> AppearanceRecord:
    data = _exact_mapping(
        value,
        {
            "appearance_id",
            "target_node_id",
            "appearance_term_ref_ids",
            "channel_measurement_ids",
            "texture_artifacts",
            "source_ids",
            "state",
            "provenance_ids",
            "extension_ids",
            "advisory_support",
        },
        path,
    )
    textures = _exact_list(data["texture_artifacts"], f"{path}/texture_artifacts", maximum=8)
    return AppearanceRecord(
        appearance_id=data["appearance_id"],
        target_node_id=data["target_node_id"],
        appearance_term_ref_ids=_tuple_strings(
            data["appearance_term_ref_ids"],
            f"{path}/appearance_term_ref_ids",
            maximum=MAX_GRAPH_TERMS_PER_ELEMENT,
        ),
        channel_measurement_ids=_tuple_strings(
            data["channel_measurement_ids"],
            f"{path}/channel_measurement_ids",
            maximum=16,
        ),
        texture_artifacts=tuple(
            _decode_content(item, f"{path}/texture_artifacts/{index}")
            for index, item in enumerate(textures)
        ),
        source_ids=_tuple_strings(
            data["source_ids"], f"{path}/source_ids", maximum=MAX_GRAPH_SOURCES
        ),
        state=_enum_value(data["state"], AssertionState, f"{path}/state"),
        provenance_ids=_tuple_strings(
            data["provenance_ids"],
            f"{path}/provenance_ids",
            maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
        ),
        extension_ids=_tuple_strings(
            data["extension_ids"],
            f"{path}/extension_ids",
            maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
        ),
        advisory_support=data["advisory_support"],
    )


def _decode_alternative(value: object, path: str) -> HypothesisAlternative:
    data = _exact_mapping(
        value,
        {"alternative_id", "member_refs", "advisory_support", "provenance_ids"},
        path,
    )
    members = _exact_list(data["member_refs"], f"{path}/member_refs", maximum=64)
    return HypothesisAlternative(
        alternative_id=data["alternative_id"],
        member_refs=tuple(
            _decode_element_ref(item, f"{path}/member_refs/{index}")
            for index, item in enumerate(members)
        ),
        advisory_support=data["advisory_support"],
        provenance_ids=_tuple_strings(
            data["provenance_ids"],
            f"{path}/provenance_ids",
            maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
        ),
    )


def _decode_hypothesis(value: object, path: str) -> HypothesisSet:
    data = _exact_mapping(
        value,
        {
            "hypothesis_set_id",
            "subject_refs",
            "alternatives",
            "term_ref_ids",
            "provenance_ids",
            "extension_ids",
        },
        path,
    )
    subjects = _exact_list(
        data["subject_refs"], f"{path}/subject_refs", maximum=_MAX_HYPOTHESIS_SUBJECTS
    )
    alternatives = _exact_list(
        data["alternatives"],
        f"{path}/alternatives",
        maximum=MAX_GRAPH_ALTERNATIVES_PER_SET,
    )
    return HypothesisSet(
        hypothesis_set_id=data["hypothesis_set_id"],
        subject_refs=tuple(
            _decode_element_ref(item, f"{path}/subject_refs/{index}")
            for index, item in enumerate(subjects)
        ),
        alternatives=tuple(
            _decode_alternative(item, f"{path}/alternatives/{index}")
            for index, item in enumerate(alternatives)
        ),
        term_ref_ids=_tuple_strings(
            data["term_ref_ids"], f"{path}/term_ref_ids", maximum=MAX_GRAPH_TERMS_PER_ELEMENT
        ),
        provenance_ids=_tuple_strings(
            data["provenance_ids"],
            f"{path}/provenance_ids",
            maximum=MAX_GRAPH_PROVENANCE_PER_ELEMENT,
        ),
        extension_ids=_tuple_strings(
            data["extension_ids"],
            f"{path}/extension_ids",
            maximum=MAX_GRAPH_EXTENSIONS_PER_ELEMENT,
        ),
    )


def decode_visual_feature_graph(raw: object) -> VisualFeatureGraph:
    data = _exact_mapping(
        _decode_json(raw),
        {
            "schema_version",
            "authority",
            "scope_id",
            "scope_version",
            "source_bundle_sha256",
            "producer_algorithm_id",
            "producer_algorithm_version",
            "producer_contract_sha256",
            "ontology_terms",
            "extensions",
            "provenance",
            "sources",
            "frames",
            "transforms",
            "geometries",
            "nodes",
            "relations",
            "equivalence_groups",
            "measurements",
            "appearances",
            "hypothesis_sets",
            "graph_id",
            "graph_digest",
        },
        "",
    )
    list_specs = {
        "ontology_terms": MAX_GRAPH_ONTOLOGY_TERMS,
        "extensions": MAX_GRAPH_EXTENSIONS,
        "provenance": MAX_GRAPH_PROVENANCE,
        "sources": MAX_GRAPH_SOURCES,
        "frames": MAX_GRAPH_FRAMES,
        "transforms": MAX_GRAPH_TRANSFORMS,
        "geometries": MAX_GRAPH_GEOMETRIES,
        "nodes": MAX_GRAPH_NODES,
        "relations": MAX_GRAPH_RELATIONS,
        "equivalence_groups": MAX_GRAPH_EQUIVALENCE_GROUPS,
        "measurements": MAX_GRAPH_MEASUREMENTS,
        "appearances": MAX_GRAPH_APPEARANCES,
        "hypothesis_sets": MAX_GRAPH_HYPOTHESIS_SETS,
    }
    lists = {
        name: _exact_list(data[name], f"/{name}", maximum=maximum)
        for name, maximum in list_specs.items()
    }
    result = VisualFeatureGraph(
        schema_version=data["schema_version"],
        authority=_enum_value(data["authority"], VisualGraphAuthority, "/authority"),
        scope_id=data["scope_id"],
        scope_version=data["scope_version"],
        source_bundle_sha256=data["source_bundle_sha256"],
        producer_algorithm_id=data["producer_algorithm_id"],
        producer_algorithm_version=data["producer_algorithm_version"],
        producer_contract_sha256=data["producer_contract_sha256"],
        ontology_terms=tuple(
            _decode_term(item, f"/ontology_terms/{index}")
            for index, item in enumerate(lists["ontology_terms"])
        ),
        extensions=tuple(
            _decode_extension(item, f"/extensions/{index}")
            for index, item in enumerate(lists["extensions"])
        ),
        provenance=tuple(
            _decode_provenance(item, f"/provenance/{index}")
            for index, item in enumerate(lists["provenance"])
        ),
        sources=tuple(
            _decode_source(item, f"/sources/{index}") for index, item in enumerate(lists["sources"])
        ),
        frames=tuple(
            _decode_frame(item, f"/frames/{index}") for index, item in enumerate(lists["frames"])
        ),
        transforms=tuple(
            _decode_transform(item, f"/transforms/{index}")
            for index, item in enumerate(lists["transforms"])
        ),
        geometries=tuple(
            _decode_geometry(item, f"/geometries/{index}")
            for index, item in enumerate(lists["geometries"])
        ),
        nodes=tuple(
            _decode_node(item, f"/nodes/{index}") for index, item in enumerate(lists["nodes"])
        ),
        relations=tuple(
            _decode_relation(item, f"/relations/{index}")
            for index, item in enumerate(lists["relations"])
        ),
        equivalence_groups=tuple(
            _decode_equivalence(item, f"/equivalence_groups/{index}")
            for index, item in enumerate(lists["equivalence_groups"])
        ),
        measurements=tuple(
            _decode_measurement(item, f"/measurements/{index}")
            for index, item in enumerate(lists["measurements"])
        ),
        appearances=tuple(
            _decode_appearance(item, f"/appearances/{index}")
            for index, item in enumerate(lists["appearances"])
        ),
        hypothesis_sets=tuple(
            _decode_hypothesis(item, f"/hypothesis_sets/{index}")
            for index, item in enumerate(lists["hypothesis_sets"])
        ),
    )
    if (
        type(data["graph_id"]) is not str
        or type(data["graph_digest"]) is not str
        or not hmac.compare_digest(data["graph_id"], result.graph_id)
        or not hmac.compare_digest(data["graph_digest"], result.graph_digest)
    ):
        _fail(VisualFeatureGraphErrorCode.INTEGRITY_FAILURE)
    encoded = encode_visual_feature_graph(result)
    if type(raw) is not bytes or not hmac.compare_digest(raw, encoded):
        _fail(VisualFeatureGraphErrorCode.INTEGRITY_FAILURE)
    return result


__all__ = [
    "MAX_GRAPH_ALTERNATIVES_PER_SET",
    "MAX_GRAPH_APPEARANCES",
    "MAX_GRAPH_CELLS_PER_GEOMETRY",
    "MAX_GRAPH_CELL_VERTICES",
    "MAX_GRAPH_EQUIVALENCE_GROUPS",
    "MAX_GRAPH_EXTENSIONS",
    "MAX_GRAPH_EXTENSIONS_PER_ELEMENT",
    "MAX_GRAPH_FRAMES",
    "MAX_GRAPH_GEOMETRIES",
    "MAX_GRAPH_HYPOTHESIS_SETS",
    "MAX_GRAPH_INLINE_SAMPLES_PER_GEOMETRY",
    "MAX_GRAPH_MEASUREMENTS",
    "MAX_GRAPH_NODES",
    "MAX_GRAPH_ONTOLOGY_TERMS",
    "MAX_GRAPH_PROVENANCE",
    "MAX_GRAPH_PROVENANCE_PER_ELEMENT",
    "MAX_GRAPH_RELATIONS",
    "MAX_GRAPH_SOURCES",
    "MAX_GRAPH_TERMS_PER_ELEMENT",
    "MAX_GRAPH_TOTAL_ALTERNATIVES",
    "MAX_GRAPH_TOTAL_CELLS",
    "MAX_GRAPH_TOTAL_EXTENSION_REFS",
    "MAX_GRAPH_TOTAL_INLINE_SAMPLES",
    "MAX_GRAPH_TOTAL_PROVENANCE_REFS",
    "MAX_GRAPH_TOTAL_TERM_REFS",
    "MAX_GRAPH_TRANSFORMS",
    "MAX_VISUAL_FEATURE_GRAPH_BYTES",
    "VISUAL_FEATURE_GRAPH_SCHEMA_VERSION",
    "AppearanceRecord",
    "AssertionState",
    "CellOrientation",
    "ClosureState",
    "ContentRef",
    "CoordinateFrame",
    "CoordinateSample",
    "EntityLayer",
    "EquivalenceGroup",
    "ExtensionRef",
    "FeatureNode",
    "FeatureRelation",
    "FrameKind",
    "FrameBinding",
    "FrameTransformRef",
    "GenericFrameBinding",
    "GeometryRecord",
    "GraphElementKind",
    "GraphElementRef",
    "Handedness",
    "HypothesisAlternative",
    "HypothesisSet",
    "MeasurementEstimate",
    "MeasurementEstimateKind",
    "MeasurementRecord",
    "MetricPlaneFrameBinding",
    "MetricSpaceFrameBinding",
    "MetricUncertainty",
    "MetricUncertaintyKind",
    "OntologyTermRef",
    "OverviewNormalizedFrameBinding",
    "ProvenanceKind",
    "ProvenanceRecord",
    "RelationEndpoint",
    "SourceArtifact",
    "SourcePixelFrameBinding",
    "TopologyCell",
    "VisualFeatureGraph",
    "VisualFeatureGraphError",
    "VisualFeatureGraphErrorCode",
    "VisualGraphAuthority",
    "decode_visual_feature_graph",
    "encode_visual_feature_graph",
]
