"""Trusted FreeCAD rules for reviewed Part curve and path operations.

Plans contain semantic operation identities and backend-neutral values only.
The sole semantic-operation to native ``TypeId``/property mapping is the static
table in this module.  Importing this module does not import FreeCAD, and plan
or conformance receipts never grant execution authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from vibecad.parametric.freecad_reviewed_transaction import (
    NativeTransactionError,
    NativeTransactionErrorCode,
    NativeTransactionRunner,
)

PART_CURVE_PLAN_SCHEMA_VERSION: Final = 1
PART_CURVE_PLAN_MEDIA_TYPE: Final = "application/vnd.vibecad.freecad-part-curve-plan+json"
MAX_PART_CURVE_PLAN_BYTES: Final = 128 * 1024
PART_CURVE_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"

_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-part-curve-rule-contract.v1\0"
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-part-curve-plan.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-part-curve-receipt.v1\0"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_HELIX_TURNS = 256.0
_MAX_SPIRAL_SEGMENTS = 8_192.0


class PartCurveOperation(StrEnum):
    CIRCLE = "circle"
    ELLIPSE = "ellipse"
    HELIX = "helix"
    LINE = "line"
    PLANE = "plane"
    POLYGON = "polygon"
    REGULAR_POLYGON = "regular_polygon"
    SPIRAL = "spiral"
    VERTEX = "vertex"


@dataclass(frozen=True, slots=True)
class NativeCurveParameterSpec:
    semantic_key: str
    property_name: str
    kind: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NativeCurveSpec:
    type_id: str
    object_prefix: str
    parameters: tuple[NativeCurveParameterSpec, ...]
    fixed_properties: tuple[tuple[str, object], ...]
    shape_type: str
    minimum_vertices: int
    minimum_edges: int
    minimum_faces: int


def _float_parameter(
    semantic_key: str,
    property_name: str,
    *,
    minimum: float = -1_000_000.0,
    maximum: float = 1_000_000.0,
) -> NativeCurveParameterSpec:
    return NativeCurveParameterSpec(
        semantic_key,
        property_name,
        "float",
        minimum,
        maximum,
    )


def _integer_parameter(
    semantic_key: str,
    property_name: str,
    *,
    minimum: int,
    maximum: int,
) -> NativeCurveParameterSpec:
    return NativeCurveParameterSpec(
        semantic_key,
        property_name,
        "integer",
        float(minimum),
        float(maximum),
    )


def _bool_parameter(semantic_key: str, property_name: str) -> NativeCurveParameterSpec:
    return NativeCurveParameterSpec(semantic_key, property_name, "boolean")


def _enum_parameter(
    semantic_key: str,
    property_name: str,
    choices: tuple[str, ...],
) -> NativeCurveParameterSpec:
    return NativeCurveParameterSpec(
        semantic_key,
        property_name,
        "enumeration",
        choices=choices,
    )


_ANGLE_PAIR = (
    _float_parameter("start_angle_degrees", "Angle1", minimum=-360.0, maximum=360.0),
    _float_parameter("end_angle_degrees", "Angle2", minimum=-360.0, maximum=360.0),
)
_LINE_COORDINATES = tuple(
    _float_parameter(f"{axis}{endpoint}_mm", f"{axis.upper()}{endpoint}")
    for endpoint in (1, 2)
    for axis in ("x", "y", "z")
)
_VERTEX_COORDINATES = tuple(
    _float_parameter(f"{axis}_mm", axis.upper()) for axis in ("x", "y", "z")
)

PART_CURVE_NATIVE_SPECS: Final = MappingProxyType(
    {
        PartCurveOperation.CIRCLE: NativeCurveSpec(
            "Part::Circle",
            "Circle",
            (_float_parameter("radius_mm", "Radius", minimum=0.001), *_ANGLE_PAIR),
            (),
            "Edge",
            1,
            1,
            0,
        ),
        PartCurveOperation.ELLIPSE: NativeCurveSpec(
            "Part::Ellipse",
            "Ellipse",
            (
                _float_parameter("major_radius_mm", "MajorRadius", minimum=0.001),
                _float_parameter("minor_radius_mm", "MinorRadius", minimum=0.001),
                *_ANGLE_PAIR,
            ),
            (),
            "Edge",
            1,
            1,
            0,
        ),
        PartCurveOperation.HELIX: NativeCurveSpec(
            "Part::Helix",
            "Helix",
            (
                _float_parameter("pitch_mm", "Pitch", minimum=0.001),
                _float_parameter("height_mm", "Height", minimum=0.001),
                _float_parameter("radius_mm", "Radius", minimum=0.001),
                _float_parameter("cone_angle_degrees", "Angle", minimum=-80.0, maximum=80.0),
                _enum_parameter(
                    "handedness",
                    "LocalCoord",
                    ("Right-handed", "Left-handed"),
                ),
            ),
            (("Style", "Old style"), ("SegmentLength", 0.0)),
            "Wire",
            2,
            1,
            0,
        ),
        PartCurveOperation.LINE: NativeCurveSpec(
            "Part::Line", "Line", _LINE_COORDINATES, (), "Edge", 2, 1, 0
        ),
        PartCurveOperation.PLANE: NativeCurveSpec(
            "Part::Plane",
            "Plane",
            (
                _float_parameter("length_mm", "Length", minimum=0.001),
                _float_parameter("width_mm", "Width", minimum=0.001),
            ),
            (),
            "Face",
            4,
            4,
            1,
        ),
        PartCurveOperation.POLYGON: NativeCurveSpec(
            "Part::Polygon",
            "Polygon",
            (
                NativeCurveParameterSpec("points_mm", "Nodes", "points"),
                _bool_parameter("closed", "Close"),
            ),
            (),
            "Wire",
            2,
            1,
            0,
        ),
        PartCurveOperation.REGULAR_POLYGON: NativeCurveSpec(
            "Part::RegularPolygon",
            "RegularPolygon",
            (
                _integer_parameter("side_count", "Polygon", minimum=3, maximum=64),
                _float_parameter("circumradius_mm", "Circumradius", minimum=0.001),
            ),
            (),
            "Wire",
            3,
            3,
            0,
        ),
        PartCurveOperation.SPIRAL: NativeCurveSpec(
            "Part::Spiral",
            "Spiral",
            (
                _float_parameter("growth_mm", "Growth", minimum=0.001),
                _float_parameter("start_radius_mm", "Radius", minimum=0.001),
                _float_parameter("rotations", "Rotations", minimum=0.1, maximum=100.0),
                _float_parameter("segment_length_mm", "SegmentLength", minimum=0.001),
            ),
            (),
            "Wire",
            2,
            1,
            0,
        ),
        PartCurveOperation.VERTEX: NativeCurveSpec(
            "Part::Vertex", "Vertex", _VERTEX_COORDINATES, (), "Vertex", 1, 0, 0
        ),
    }
)

PART_CURVE_RULE_ID: Final = "freecad.part.curves.v1"
_NATIVE_CONTRACT = {
    "engine": {
        "name": "FreeCAD",
        "version": "1.1.0",
        "build_id": PART_CURVE_FREECAD_ENGINE_BUILD_ID,
    },
    "common": {
        "placement": "axis-angle",
        "transaction": "exact-rollback",
        "standalone_part_object": True,
    },
    "operations": [
        {
            "operation": operation.value,
            "type_id": spec.type_id,
            "shape_type": spec.shape_type,
            "parameters": [
                {
                    "semantic_key": item.semantic_key,
                    "property_name": item.property_name,
                    "kind": item.kind,
                    "minimum": item.minimum,
                    "maximum": item.maximum,
                    "choices": list(item.choices),
                }
                for item in spec.parameters
            ],
            "fixed_properties": [list(item) for item in spec.fixed_properties],
        }
        for operation, spec in PART_CURVE_NATIVE_SPECS.items()
    ],
}
PART_CURVE_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN
    + json.dumps(
        _NATIVE_CONTRACT,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
).hexdigest()


class PartCurveRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class PartCurveRuleError(ValueError):
    """Bounded stable failure at the trusted Part native boundary."""

    def __init__(self, code: PartCurveRuleErrorCode, path: str = "/") -> None:
        if type(code) is not PartCurveRuleErrorCode:
            raise TypeError("code must be a PartCurveRuleErrorCode")
        if (
            type(path) is not str
            or not path.startswith("/")
            or not path.isascii()
            or not path.isprintable()
            or len(path) > 192
        ):
            path = "/"
        self.code = code
        self.path = path
        super().__init__(f"Part curve rule error ({code.value}) at {path}")


def _fail(code: PartCurveRuleErrorCode, path: str) -> None:
    raise PartCurveRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or len(value) > 128 or _IDENTIFIER.fullmatch(value) is None:
        _fail(PartCurveRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(PartCurveRuleErrorCode.INVALID_INPUT, path)
    return value


def _finite(value: object, path: str) -> float:
    if type(value) not in {int, float}:
        _fail(PartCurveRuleErrorCode.INVALID_INPUT, path)
    converted = float(value)
    if not math.isfinite(converted):
        _fail(PartCurveRuleErrorCode.INVALID_INPUT, path)
    return converted


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(PartCurveRuleErrorCode.INVALID_INPUT, path)
    return value


def _canonical_json(value: object) -> bytes:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/")
    if not payload or len(payload) > MAX_PART_CURVE_PLAN_BYTES:
        _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/")
    return payload


class _DuplicateKeyError(ValueError):
    pass


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _decode_mapping(raw: object) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_PART_CURVE_PLAN_BYTES:
        _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_pairs)
    except (_DuplicateKeyError, UnicodeError, ValueError, RecursionError):
        _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/")
    if type(value) is not dict or not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(PartCurveRuleErrorCode.INTEGRITY_FAILURE, "/")
    return value


def _point(value: object, path: str) -> tuple[float, float, float]:
    if type(value) not in {list, tuple} or len(value) != 3:
        _fail(PartCurveRuleErrorCode.INVALID_INPUT, path)
    return tuple(_finite(item, f"{path}/{index}") for index, item in enumerate(value))


def _parameter_value(
    value: object,
    parameter: NativeCurveParameterSpec,
    path: str,
) -> object:
    if parameter.kind == "float":
        converted = _finite(value, path)
        if not parameter.minimum <= converted <= parameter.maximum:  # type: ignore[operator]
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, path)
        return converted
    if parameter.kind == "integer":
        if (
            type(value) is not int or not parameter.minimum <= value <= parameter.maximum  # type: ignore[operator]
        ):
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, path)
        return value
    if parameter.kind == "boolean":
        if type(value) is not bool:
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, path)
        return value
    if parameter.kind == "enumeration":
        if type(value) is not str or value not in parameter.choices:
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, path)
        return value
    if parameter.kind == "points":
        if type(value) not in {list, tuple} or not 2 <= len(value) <= 64:
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, path)
        return tuple(_point(item, f"{path}/{index}") for index, item in enumerate(value))
    _fail(PartCurveRuleErrorCode.INTEGRITY_FAILURE, path)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartCurveParameterSet:
    operation: PartCurveOperation
    values: tuple[object, ...]
    translation_mm: tuple[float, float, float]
    rotation_axis: tuple[float, float, float]
    rotation_degrees: float

    def __post_init__(self) -> None:
        if type(self.operation) is not PartCurveOperation:
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/operation")
        spec = PART_CURVE_NATIVE_SPECS[self.operation]
        if type(self.values) is not tuple or len(self.values) != len(spec.parameters):
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/parameters/geometry")
        normalized = tuple(
            _parameter_value(value, parameter, f"/parameters/geometry/{index}")
            for index, (value, parameter) in enumerate(
                zip(self.values, spec.parameters, strict=True)
            )
        )
        object.__setattr__(self, "values", normalized)
        for name in ("translation_mm", "rotation_axis"):
            raw = getattr(self, name)
            if type(raw) is not tuple or len(raw) != 3:
                _fail(PartCurveRuleErrorCode.INVALID_INPUT, f"/parameters/{name}")
            converted = tuple(
                _finite(item, f"/parameters/{name}/{index}") for index, item in enumerate(raw)
            )
            object.__setattr__(self, name, converted)
        if any(abs(item) > 1_000_000.0 for item in self.translation_mm):
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/parameters/translation_mm")
        norm = math.sqrt(sum(item * item for item in self.rotation_axis))
        if not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1e-9):
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/parameters/rotation_axis")
        angle = _finite(self.rotation_degrees, "/parameters/rotation_degrees")
        if not -360.0 <= angle <= 360.0:
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/parameters/rotation_degrees")
        object.__setattr__(self, "rotation_degrees", angle)
        self._validate_relations()

    def _validate_relations(self) -> None:
        spec = PART_CURVE_NATIVE_SPECS[self.operation]
        values = dict(
            zip((item.semantic_key for item in spec.parameters), self.values, strict=True)
        )
        if self.operation in {PartCurveOperation.CIRCLE, PartCurveOperation.ELLIPSE}:
            span = values["end_angle_degrees"] - values["start_angle_degrees"]
            if not 0.01 <= span <= 360.0:
                _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/parameters/geometry/angles")
        if self.operation is PartCurveOperation.ELLIPSE and not (
            values["major_radius_mm"] > values["minor_radius_mm"]
        ):
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/parameters/geometry/radii")
        if self.operation is PartCurveOperation.LINE:
            start = tuple(values[f"{axis}1_mm"] for axis in ("x", "y", "z"))
            end = tuple(values[f"{axis}2_mm"] for axis in ("x", "y", "z"))
            if math.dist(start, end) <= 1e-9:
                _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/parameters/geometry/points")
        if self.operation is PartCurveOperation.HELIX and (
            values["height_mm"] / values["pitch_mm"] > _MAX_HELIX_TURNS
        ):
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/parameters/geometry/complexity")
        if self.operation is PartCurveOperation.POLYGON:
            points = values["points_mm"]
            closed = values["closed"]
            if closed and len(points) < 3:
                _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/parameters/geometry/points_mm")
            right = (*points[1:], points[0]) if closed else points[1:]
            left = points if closed else points[:-1]
            pairs = zip(left, right, strict=True)
            if any(math.dist(left, right) <= 1e-9 for left, right in pairs):
                _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/parameters/geometry/points_mm")
        if self.operation is PartCurveOperation.SPIRAL:
            rotations = values["rotations"]
            maximum_radius = values["start_radius_mm"] + values["growth_mm"] * rotations
            conservative_length = 2.0 * math.pi * rotations * maximum_radius
            conservative_length += values["growth_mm"] * rotations
            if conservative_length / values["segment_length_mm"] > _MAX_SPIRAL_SEGMENTS:
                _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/parameters/geometry/complexity")

    @classmethod
    def from_value(cls, operation: PartCurveOperation, value: object) -> PartCurveParameterSet:
        if type(operation) is not PartCurveOperation:
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/operation")
        root = _exact_fields(value, {"geometry", "placement"}, "/parameters")
        spec = PART_CURVE_NATIVE_SPECS[operation]
        geometry = _exact_fields(
            root["geometry"],
            {item.semantic_key for item in spec.parameters},
            "/parameters/geometry",
        )
        placement = _exact_fields(
            root["placement"],
            {"translation_mm", "rotation_axis", "rotation_degrees"},
            "/parameters/placement",
        )
        translation = placement["translation_mm"]
        rotation_axis = placement["rotation_axis"]
        if type(translation) is not list or type(rotation_axis) is not list:
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/parameters/placement")
        return cls(
            operation=operation,
            values=tuple(geometry[item.semantic_key] for item in spec.parameters),
            translation_mm=tuple(translation),
            rotation_axis=tuple(rotation_axis),
            rotation_degrees=placement["rotation_degrees"],
        )

    def to_value(self) -> dict[str, object]:
        spec = PART_CURVE_NATIVE_SPECS[self.operation]
        geometry: dict[str, object] = {}
        for parameter, value in zip(spec.parameters, self.values, strict=True):
            geometry[parameter.semantic_key] = (
                [list(point) for point in value] if parameter.kind == "points" else value
            )
        return {
            "geometry": geometry,
            "placement": {
                "translation_mm": list(self.translation_mm),
                "rotation_axis": list(self.rotation_axis),
                "rotation_degrees": self.rotation_degrees,
            },
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PartCurveBackendPlan:
    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    manifest_sha256: str
    operation_specification_sha256: str
    body_id: str
    node_id: str
    result_id: str
    parameter_id: str
    value_id: str
    operation: PartCurveOperation
    parameters: PartCurveParameterSet
    schema_version: int = PART_CURVE_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PART_CURVE_PLAN_SCHEMA_VERSION:
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/schema_version")
        for name in (
            "source_artifact_id",
            "source_graph_id",
            "body_id",
            "node_id",
            "result_id",
            "parameter_id",
            "value_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in (
            "source_graph_sha256",
            "source_content_sha256",
            "lowering_request_sha256",
            "adapter_contract_sha256",
            "manifest_sha256",
            "operation_specification_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if (
            type(self.operation) is not PartCurveOperation
            or type(self.parameters) is not PartCurveParameterSet
            or self.parameters.operation is not self.operation
        ):
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/operation")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority": "none",
            "backend": {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PART_CURVE_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": PART_CURVE_RULE_ID,
                "rule_contract_sha256": PART_CURVE_RULE_CONTRACT_SHA256,
            },
            "source": {
                "artifact_id": self.source_artifact_id,
                "graph_id": self.source_graph_id,
                "graph_sha256": self.source_graph_sha256,
                "content_sha256": self.source_content_sha256,
            },
            "binding": {
                "lowering_request_sha256": self.lowering_request_sha256,
                "adapter_contract_sha256": self.adapter_contract_sha256,
                "manifest_sha256": self.manifest_sha256,
                "operation_specification_sha256": self.operation_specification_sha256,
            },
            "selection": {
                "body_id": self.body_id,
                "node_id": self.node_id,
                "result_id": self.result_id,
                "parameter_id": self.parameter_id,
                "value_id": self.value_id,
            },
            "operation": {
                "id": self.operation.value,
                "parameters": self.parameters.to_value(),
            },
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_PLAN_DIGEST_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> PartCurveBackendPlan:
        root = _exact_fields(
            value,
            {
                "schema_version",
                "authority",
                "backend",
                "rule",
                "source",
                "binding",
                "selection",
                "operation",
            },
            "/",
        )
        backend = _exact_fields(
            root["backend"], {"engine", "engine_version", "engine_build_id"}, "/backend"
        )
        rule = _exact_fields(root["rule"], {"rule_id", "rule_contract_sha256"}, "/rule")
        source = _exact_fields(
            root["source"], {"artifact_id", "graph_id", "graph_sha256", "content_sha256"}, "/source"
        )
        binding = _exact_fields(
            root["binding"],
            {
                "lowering_request_sha256",
                "adapter_contract_sha256",
                "manifest_sha256",
                "operation_specification_sha256",
            },
            "/binding",
        )
        selection = _exact_fields(
            root["selection"],
            {"body_id", "node_id", "result_id", "parameter_id", "value_id"},
            "/selection",
        )
        operation = _exact_fields(root["operation"], {"id", "parameters"}, "/operation")
        try:
            operation_id = PartCurveOperation(operation["id"])
        except (TypeError, ValueError):
            _fail(PartCurveRuleErrorCode.INTEGRITY_FAILURE, "/operation/id")
        if (
            root["authority"] != "none"
            or backend
            != {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PART_CURVE_FREECAD_ENGINE_BUILD_ID,
            }
            or rule
            != {
                "rule_id": PART_CURVE_RULE_ID,
                "rule_contract_sha256": PART_CURVE_RULE_CONTRACT_SHA256,
            }
        ):
            _fail(PartCurveRuleErrorCode.INTEGRITY_FAILURE, "/contract")
        return cls(
            schema_version=root["schema_version"],
            source_artifact_id=source["artifact_id"],
            source_graph_id=source["graph_id"],
            source_graph_sha256=source["graph_sha256"],
            source_content_sha256=source["content_sha256"],
            lowering_request_sha256=binding["lowering_request_sha256"],
            adapter_contract_sha256=binding["adapter_contract_sha256"],
            manifest_sha256=binding["manifest_sha256"],
            operation_specification_sha256=binding["operation_specification_sha256"],
            body_id=selection["body_id"],
            node_id=selection["node_id"],
            result_id=selection["result_id"],
            parameter_id=selection["parameter_id"],
            value_id=selection["value_id"],
            operation=operation_id,
            parameters=PartCurveParameterSet.from_value(operation_id, operation["parameters"]),
        )


def decode_part_curve_backend_plan(
    raw: object,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> PartCurveBackendPlan:
    if expected_content_sha256 is not None:
        expected_content_sha256 = _digest(expected_content_sha256, "/expected_content_sha256")
    if expected_plan_sha256 is not None:
        expected_plan_sha256 = _digest(expected_plan_sha256, "/expected_plan_sha256")
    result = PartCurveBackendPlan.from_mapping(_decode_mapping(raw))
    if type(raw) is not bytes or not hmac.compare_digest(raw, result.canonical_bytes):
        _fail(PartCurveRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_content_sha256
    ):
        _fail(PartCurveRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        result.plan_sha256, expected_plan_sha256
    ):
        _fail(PartCurveRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class PartCurveExecutionBindings:
    document: object
    expected_adapter_contract_sha256: str
    expected_manifest_sha256: str
    expected_operation_specification_sha256: str

    def __post_init__(self) -> None:
        if self.document is None:
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/bindings/document")
        for name in (
            "expected_adapter_contract_sha256",
            "expected_manifest_sha256",
            "expected_operation_specification_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/bindings/{name}"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PartCurveShapeSignature:
    shape_type: str
    vertex_count: int
    edge_count: int
    face_count: int
    length_mm: float
    area_mm2: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "shape_type", _identifier(self.shape_type, "/shape_type"))
        for name in ("vertex_count", "edge_count", "face_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                _fail(PartCurveRuleErrorCode.INVALID_INPUT, f"/{name}")
        for name in ("length_mm", "area_mm2"):
            value = _finite(getattr(self, name), f"/{name}")
            if value < 0.0:
                _fail(PartCurveRuleErrorCode.INVALID_INPUT, f"/{name}")
            object.__setattr__(self, name, value)

    def to_mapping(self) -> dict[str, object]:
        return {
            "shape_type": self.shape_type,
            "vertex_count": self.vertex_count,
            "edge_count": self.edge_count,
            "face_count": self.face_count,
            "length_mm": self.length_mm,
            "area_mm2": self.area_mm2,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PartCurveConformanceReceipt:
    plan_sha256: str
    operation: PartCurveOperation
    object_name: str
    shape: PartCurveShapeSignature
    receipt_id: str = field(init=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/plan_sha256"))
        if (
            type(self.operation) is not PartCurveOperation
            or type(self.shape) is not PartCurveShapeSignature
        ):
            _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/receipt")
        object.__setattr__(self, "object_name", _identifier(self.object_name, "/object_name"))
        body = {
            "authority": "none",
            "plan_sha256": self.plan_sha256,
            "operation": self.operation.value,
            "object_name": self.object_name,
            "shape": self.shape.to_mapping(),
        }
        digest = hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()
        object.__setattr__(self, "receipt_sha256", digest)
        object.__setattr__(self, "receipt_id", f"part_curve_receipt_{digest[:32]}")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _matrix_values(placement: object) -> tuple[float, ...]:
    try:
        matrix = placement.toMatrix()
        return tuple(
            float(getattr(matrix, name))
            for name in (
                "A11",
                "A12",
                "A13",
                "A14",
                "A21",
                "A22",
                "A23",
                "A24",
                "A31",
                "A32",
                "A33",
                "A34",
                "A41",
                "A42",
                "A43",
                "A44",
            )
        )
    except Exception:
        _fail(PartCurveRuleErrorCode.CONFORMANCE_FAILED, "/result/placement")


def _property_matches(
    actual: object, expected: object, parameter: NativeCurveParameterSpec
) -> bool:
    if parameter.kind == "float":
        return math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1e-9)
    if parameter.kind == "integer":
        return int(actual) == expected
    if parameter.kind == "boolean":
        return bool(actual) is expected
    if parameter.kind == "enumeration":
        return str(actual) == expected
    if parameter.kind == "points":
        try:
            return len(actual) == len(expected) and all(
                math.isclose(float(vector[index]), float(point[index]), rel_tol=0.0, abs_tol=1e-9)
                for vector, point in zip(actual, expected, strict=True)
                for index in range(3)
            )
        except Exception:
            return False
    return False


def _validate_created(
    document: object,
    feature: object,
    plan: PartCurveBackendPlan,
    expected_placement: object,
) -> PartCurveShapeSignature:
    spec = PART_CURVE_NATIVE_SPECS[plan.operation]
    try:
        shape = feature.Shape
        signature = PartCurveShapeSignature(
            shape_type=shape.ShapeType,
            vertex_count=len(shape.Vertexes),
            edge_count=len(shape.Edges),
            face_count=len(shape.Faces),
            length_mm=float(shape.Length),
            area_mm2=float(shape.Area),
        )
        if (
            feature.Document is not document
            or document.getObject(feature.Name) is not feature
            or feature.TypeId != spec.type_id
            or not feature.isValid()
            or tuple(feature.State) != ("Up-to-date",)
            or shape.isNull()
            or not shape.isValid()
            or signature.shape_type != spec.shape_type
            or signature.vertex_count < spec.minimum_vertices
            or signature.edge_count < spec.minimum_edges
            or signature.face_count < spec.minimum_faces
            or (
                plan.operation is not PartCurveOperation.VERTEX
                and plan.operation is not PartCurveOperation.PLANE
                and signature.length_mm <= 1e-9
            )
            or (plan.operation is PartCurveOperation.PLANE and signature.area_mm2 <= 1e-9)
        ):
            _fail(PartCurveRuleErrorCode.CONFORMANCE_FAILED, "/result/shape")
        values = dict(
            zip(
                (item.semantic_key for item in spec.parameters), plan.parameters.values, strict=True
            )
        )
        if plan.operation is PartCurveOperation.POLYGON:
            expected_edges = (
                len(values["points_mm"]) if values["closed"] else len(values["points_mm"]) - 1
            )
            if signature.edge_count != expected_edges:
                _fail(PartCurveRuleErrorCode.CONFORMANCE_FAILED, "/result/shape/edges")
        if plan.operation is PartCurveOperation.REGULAR_POLYGON and (
            signature.edge_count != values["side_count"]
            or signature.vertex_count != values["side_count"]
        ):
            _fail(PartCurveRuleErrorCode.CONFORMANCE_FAILED, "/result/shape/polygon")
        for index, (parameter, expected) in enumerate(
            zip(spec.parameters, plan.parameters.values, strict=True)
        ):
            if not _property_matches(
                getattr(feature, parameter.property_name), expected, parameter
            ):
                _fail(PartCurveRuleErrorCode.CONFORMANCE_FAILED, f"/result/parameters/{index}")
        for index, (property_name, expected) in enumerate(spec.fixed_properties):
            actual = getattr(feature, property_name)
            matches = (
                str(actual) == expected
                if type(expected) is str
                else math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1e-9)
            )
            if not matches:
                _fail(PartCurveRuleErrorCode.CONFORMANCE_FAILED, f"/result/fixed/{index}")
        if any(
            not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9)
            for actual, expected in zip(
                _matrix_values(feature.Placement), _matrix_values(expected_placement), strict=True
            )
        ):
            _fail(PartCurveRuleErrorCode.CONFORMANCE_FAILED, "/result/placement")
        return signature
    except PartCurveRuleError:
        raise
    except Exception:
        _fail(PartCurveRuleErrorCode.CONFORMANCE_FAILED, "/result")


def apply_part_curve_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: PartCurveExecutionBindings,
) -> PartCurveConformanceReceipt:
    """Explicit trusted-host action; exact plan validation precedes mutation."""

    if type(bindings) is not PartCurveExecutionBindings:
        _fail(PartCurveRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(PartCurveRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != PART_CURVE_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(PartCurveRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_part_curve_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    if (
        not hmac.compare_digest(
            plan.adapter_contract_sha256,
            bindings.expected_adapter_contract_sha256,
        )
        or not hmac.compare_digest(plan.manifest_sha256, bindings.expected_manifest_sha256)
        or not hmac.compare_digest(
            plan.operation_specification_sha256,
            bindings.expected_operation_specification_sha256,
        )
    ):
        _fail(PartCurveRuleErrorCode.INTEGRITY_FAILURE, "/bindings/reviewed_contract")
    document = bindings.document
    spec = PART_CURVE_NATIVE_SPECS[plan.operation]
    object_name = f"{spec.object_prefix}_{plan.plan_sha256[:16]}"
    try:
        if (
            getattr(document, "UndoMode", 0) != 1
            or bool(document.HasPendingTransaction)
            or document.getObject(object_name) is not None
        ):
            _fail(PartCurveRuleErrorCode.PRECONDITION_FAILED, "/document")
        before_objects = tuple(document.Objects)
        before_visibility = tuple(
            (item, bool(item.Visibility)) for item in before_objects if hasattr(item, "Visibility")
        )
    except PartCurveRuleError:
        raise
    except Exception:
        _fail(PartCurveRuleErrorCode.PRECONDITION_FAILED, "/document")

    def snapshot() -> tuple[tuple[object, ...], tuple[tuple[object, bool], ...]]:
        return before_objects, before_visibility

    def rollback_matches(before: object) -> bool:
        objects, visibility = before
        current = tuple(document.Objects)
        return (
            len(current) == len(objects)
            and all(left is right for left, right in zip(current, objects, strict=True))
            and all(bool(item.Visibility) is expected for item, expected in visibility)
        )

    def create() -> PartCurveShapeSignature:
        feature = document.addObject(spec.type_id, object_name)
        expected_placement = FreeCAD.Placement(
            FreeCAD.Vector(*plan.parameters.translation_mm),
            FreeCAD.Rotation(
                FreeCAD.Vector(*plan.parameters.rotation_axis),
                plan.parameters.rotation_degrees,
            ),
        )
        feature.Placement = expected_placement
        for parameter, value in zip(spec.parameters, plan.parameters.values, strict=True):
            native_value = (
                [FreeCAD.Vector(*point) for point in value] if parameter.kind == "points" else value
            )
            setattr(feature, parameter.property_name, native_value)
        for property_name, value in spec.fixed_properties:
            setattr(feature, property_name, value)
        document.recompute()
        return _validate_created(document, feature, plan, expected_placement)

    try:
        signature = NativeTransactionRunner().run(
            document,
            label="VibeCAD trusted Part curve",
            snapshot=snapshot,
            apply=create,
            rollback_matches=rollback_matches,
        )
    except KeyboardInterrupt:
        raise
    except NativeTransactionError as error:
        path = (
            "/transaction/rollback"
            if error.code is NativeTransactionErrorCode.ROLLBACK_FAILED
            else "/transaction/apply"
        )
        _fail(PartCurveRuleErrorCode.TRANSACTION_FAILED, path)
    return PartCurveConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        object_name=object_name,
        shape=signature,
    )


__all__ = [
    "MAX_PART_CURVE_PLAN_BYTES",
    "PART_CURVE_FREECAD_ENGINE_BUILD_ID",
    "PART_CURVE_NATIVE_SPECS",
    "PART_CURVE_PLAN_MEDIA_TYPE",
    "PART_CURVE_PLAN_SCHEMA_VERSION",
    "PART_CURVE_RULE_CONTRACT_SHA256",
    "PART_CURVE_RULE_ID",
    "NativeCurveParameterSpec",
    "NativeCurveSpec",
    "PartCurveBackendPlan",
    "PartCurveConformanceReceipt",
    "PartCurveExecutionBindings",
    "PartCurveOperation",
    "PartCurveParameterSet",
    "PartCurveRuleError",
    "PartCurveRuleErrorCode",
    "PartCurveShapeSignature",
    "apply_part_curve_plan",
    "decode_part_curve_backend_plan",
]
