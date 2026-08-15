"""Reviewed native FreeCAD rule for backend-neutral sketch intent.

The source graph never selects a FreeCAD type, geometry constructor, constraint
spelling, point code, or property.  Those choices live exclusively in the
static tables in this module.  Plans are canonical and authority-free; a
trusted host must explicitly bind and apply them to one managed
``Sketcher::SketchObject``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from vibecad.parametric.freecad_reviewed_transaction import (
    NativeTransactionError,
    NativeTransactionRunner,
)

REVIEWED_SKETCH_PLAN_SCHEMA_VERSION: Final = 1
REVIEWED_SKETCH_PLAN_MEDIA_TYPE: Final = "application/vnd.vibecad.freecad-reviewed-sketch-plan+json"
MAX_REVIEWED_SKETCH_PLAN_BYTES: Final = 96 * 1024
MAX_REVIEWED_SKETCH_METADATA_BYTES: Final = 256 * 1024
REVIEWED_SKETCH_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"
REVIEWED_SKETCH_NATIVE_TYPE_ID: Final = "Sketcher::SketchObject"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-reviewed-sketch-plan.v1\0"
_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-reviewed-sketch-rule.v1\0"
_NODE_DIGEST_DOMAIN = b"vibecad.freecad-reviewed-sketch-node.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-reviewed-sketch-receipt.v1\0"
_GEOMETRY_FINGERPRINT_DOMAIN = b"vibecad.freecad-reviewed-sketch-geometry.v1\0"
_CONSTRAINT_FINGERPRINT_DOMAIN = b"vibecad.freecad-reviewed-sketch-constraint.v1\0"
_METADATA_PROPERTY = "VibeCADReviewedSketchIntent"


class ReviewedSketchOperation(StrEnum):
    POINT = "point"
    LINE = "line"
    CIRCLE = "circle"
    ARC = "arc"
    SLOT = "slot"
    COINCIDENT = "coincident"
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"
    PARALLEL = "parallel"
    PERPENDICULAR = "perpendicular"
    TANGENT = "tangent"
    EQUAL = "equal"
    SYMMETRIC = "symmetric"
    DISTANCE = "distance"
    DISTANCE_X = "distance_x"
    DISTANCE_Y = "distance_y"
    LENGTH = "length"
    RADIUS = "radius"
    DIAMETER = "diameter"
    ANGLE = "angle"


class ReviewedSketchRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class ReviewedSketchRuleError(ValueError):
    """Bounded, non-reflective failure at the reviewed Sketcher boundary."""

    def __init__(self, code: ReviewedSketchRuleErrorCode, path: str = "/") -> None:
        if type(code) is not ReviewedSketchRuleErrorCode:
            raise TypeError("code must be a ReviewedSketchRuleErrorCode")
        try:
            size = len(path.encode("utf-8")) if type(path) is str else 0
        except UnicodeError:
            size = 385
        if (
            type(path) is not str
            or not path.startswith("/")
            or not path.isprintable()
            or len(path.splitlines()) != 1
            or size > 384
        ):
            path = "/"
        self.code = code
        self.path = path
        super().__init__(f"reviewed sketch rule error ({code.value}) at {path}")


def _fail(code: ReviewedSketchRuleErrorCode, path: str) -> None:
    raise ReviewedSketchRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, path)
    return value


def _finite(value: object, path: str, *, maximum: float = 1_000_000.0) -> float:
    if type(value) not in {int, float}:
        _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, path)
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, path)
    if not math.isfinite(result) or abs(result) > maximum:
        _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, path)
    return 0.0 if result == 0.0 else result


def _canonical_json(value: object, *, maximum: int) -> bytes:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/")
    if not raw or len(raw) > maximum:
        _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/")
    return raw


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _decode_mapping(raw: object, *, maximum: int) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/")
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/")
    if type(value) is not dict or not hmac.compare_digest(
        raw, _canonical_json(value, maximum=maximum)
    ):
        _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/")
    return value


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedSketchParameter:
    key: str
    value: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _identifier(self.key, "/parameters/key"))
        maximum = 2.0 * math.pi if self.key.endswith("_rad") else 1_000_000.0
        object.__setattr__(
            self,
            "value",
            _finite(self.value, "/parameters/value", maximum=maximum),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"key": self.key, "value": self.value}

    @classmethod
    def from_mapping(cls, value: object) -> ReviewedSketchParameter:
        item = _exact_fields(value, {"key", "value"}, "/parameters/item")
        return cls(key=item["key"], value=item["value"])


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedSketchResult:
    result_id: str
    port_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/results/result_id"))
        object.__setattr__(self, "port_id", _identifier(self.port_id, "/results/port_id"))

    def to_mapping(self) -> dict[str, str]:
        return {"result_id": self.result_id, "port_id": self.port_id}

    @classmethod
    def from_mapping(cls, value: object) -> ReviewedSketchResult:
        item = _exact_fields(value, {"result_id", "port_id"}, "/results/item")
        return cls(result_id=item["result_id"], port_id=item["port_id"])


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedSketchReference:
    source_kind: str
    target_id: str
    role: str
    producer_geometry_id: str | None = None
    producer_node_sha256: str | None = None
    port_id: str | None = None
    value_type: str | None = None

    def __post_init__(self) -> None:
        if self.source_kind not in {"result", "sketch"}:
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/references/source_kind")
        object.__setattr__(self, "target_id", _identifier(self.target_id, "/references/target_id"))
        object.__setattr__(self, "role", _identifier(self.role, "/references/role"))
        optional = (
            self.producer_geometry_id,
            self.producer_node_sha256,
            self.port_id,
            self.value_type,
        )
        if self.source_kind == "result":
            if any(item is None for item in optional):
                _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/references/result")
            object.__setattr__(
                self,
                "producer_geometry_id",
                _identifier(self.producer_geometry_id, "/references/producer_geometry_id"),
            )
            object.__setattr__(
                self,
                "producer_node_sha256",
                _digest(self.producer_node_sha256, "/references/producer_node_sha256"),
            )
            object.__setattr__(self, "port_id", _identifier(self.port_id, "/references/port_id"))
            object.__setattr__(
                self,
                "value_type",
                _identifier(self.value_type, "/references/value_type"),
            )
        elif any(item is not None for item in optional):
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/references/sketch")

    def to_mapping(self) -> dict[str, object]:
        return {
            "source_kind": self.source_kind,
            "target_id": self.target_id,
            "role": self.role,
            "producer_geometry_id": self.producer_geometry_id,
            "producer_node_sha256": self.producer_node_sha256,
            "port_id": self.port_id,
            "value_type": self.value_type,
        }

    @classmethod
    def from_mapping(cls, value: object) -> ReviewedSketchReference:
        item = _exact_fields(
            value,
            {
                "source_kind",
                "target_id",
                "role",
                "producer_geometry_id",
                "producer_node_sha256",
                "port_id",
                "value_type",
            },
            "/references/item",
        )
        return cls(**item)


_GEOMETRY_OPERATIONS = frozenset(
    {
        ReviewedSketchOperation.POINT,
        ReviewedSketchOperation.LINE,
        ReviewedSketchOperation.CIRCLE,
        ReviewedSketchOperation.ARC,
        ReviewedSketchOperation.SLOT,
    }
)
_DIMENSIONAL_OPERATIONS = frozenset(
    {
        ReviewedSketchOperation.DISTANCE,
        ReviewedSketchOperation.DISTANCE_X,
        ReviewedSketchOperation.DISTANCE_Y,
        ReviewedSketchOperation.LENGTH,
        ReviewedSketchOperation.RADIUS,
        ReviewedSketchOperation.DIAMETER,
        ReviewedSketchOperation.ANGLE,
    }
)
_PARAMETER_KEYS: Final = {
    ReviewedSketchOperation.POINT: ("x_mm", "y_mm"),
    ReviewedSketchOperation.LINE: ("x1_mm", "x2_mm", "y1_mm", "y2_mm"),
    ReviewedSketchOperation.CIRCLE: ("cx_mm", "cy_mm", "radius_mm"),
    ReviewedSketchOperation.ARC: (
        "cx_mm",
        "cy_mm",
        "radius_mm",
        "start_angle_rad",
        "sweep_angle_rad",
    ),
    ReviewedSketchOperation.SLOT: ("width_mm", "x1_mm", "x2_mm", "y1_mm", "y2_mm"),
    ReviewedSketchOperation.DISTANCE: ("value_mm",),
    ReviewedSketchOperation.DISTANCE_X: ("value_mm",),
    ReviewedSketchOperation.DISTANCE_Y: ("value_mm",),
    ReviewedSketchOperation.LENGTH: ("value_mm",),
    ReviewedSketchOperation.RADIUS: ("value_mm",),
    ReviewedSketchOperation.DIAMETER: ("value_mm",),
    ReviewedSketchOperation.ANGLE: ("value_rad",),
}
_RESULT_PORTS: Final = {
    ReviewedSketchOperation.POINT: ("point",),
    ReviewedSketchOperation.LINE: ("curve",),
    ReviewedSketchOperation.CIRCLE: ("curve",),
    ReviewedSketchOperation.ARC: ("curve",),
    ReviewedSketchOperation.SLOT: ("cap_end", "cap_start", "side_a", "side_b"),
}
_REFERENCE_COUNT: Final = {
    ReviewedSketchOperation.COINCIDENT: 2,
    ReviewedSketchOperation.HORIZONTAL: 1,
    ReviewedSketchOperation.VERTICAL: 1,
    ReviewedSketchOperation.PARALLEL: 2,
    ReviewedSketchOperation.PERPENDICULAR: 2,
    ReviewedSketchOperation.TANGENT: 2,
    ReviewedSketchOperation.EQUAL: 2,
    ReviewedSketchOperation.SYMMETRIC: 3,
    ReviewedSketchOperation.DISTANCE: 2,
    ReviewedSketchOperation.DISTANCE_X: 2,
    ReviewedSketchOperation.DISTANCE_Y: 2,
    ReviewedSketchOperation.LENGTH: 1,
    ReviewedSketchOperation.RADIUS: 1,
    ReviewedSketchOperation.DIAMETER: 1,
    ReviewedSketchOperation.ANGLE: 2,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedSketchBackendPlan:
    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    request_digest: str
    adapter_contract_sha256: str
    manifest_sha256: str
    operation_specification_sha256: str
    sketch_id: str
    node_id: str
    node_sha256: str
    operation: ReviewedSketchOperation
    parameters: tuple[ReviewedSketchParameter, ...]
    references: tuple[ReviewedSketchReference, ...]
    results: tuple[ReviewedSketchResult, ...]
    construction: bool | None
    mode: str | None
    enabled: bool | None
    schema_version: int = REVIEWED_SKETCH_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/schema_version")
        for name in ("source_artifact_id", "source_graph_id", "sketch_id", "node_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in (
            "source_graph_sha256",
            "source_content_sha256",
            "request_digest",
            "adapter_contract_sha256",
            "manifest_sha256",
            "operation_specification_sha256",
            "node_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if type(self.operation) is not ReviewedSketchOperation:
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/operation")
        if (
            type(self.parameters) is not tuple
            or type(self.references) is not tuple
            or type(self.results) is not tuple
            or not all(type(item) is ReviewedSketchParameter for item in self.parameters)
            or not all(type(item) is ReviewedSketchReference for item in self.references)
            or not all(type(item) is ReviewedSketchResult for item in self.results)
        ):
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/plan")
        parameters = tuple(sorted(self.parameters, key=lambda item: item.key))
        results = tuple(sorted(self.results, key=lambda item: item.port_id))
        if (
            len({item.key for item in parameters}) != len(parameters)
            or len({item.result_id for item in results}) != len(results)
            or len({item.port_id for item in results}) != len(results)
        ):
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/plan")
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(self, "results", results)
        expected_parameters = _PARAMETER_KEYS.get(self.operation, ())
        if tuple(item.key for item in parameters) != expected_parameters:
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/parameters")
        if self.operation in _GEOMETRY_OPERATIONS:
            if (
                self.references
                or type(self.construction) is not bool
                or self.mode is not None
                or self.enabled is not None
                or tuple(item.port_id for item in results) != _RESULT_PORTS[self.operation]
            ):
                _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/geometry")
        else:
            if (
                len(self.references) != _REFERENCE_COUNT[self.operation]
                or self.construction is not None
                or self.mode != "driving"
                or type(self.enabled) is not bool
                or tuple(item.port_id for item in results) != ("constraint",)
            ):
                _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/constraint")
        self._validate_relations()

    def _validate_relations(self) -> None:
        values = {item.key: item.value for item in self.parameters}
        if self.operation is ReviewedSketchOperation.LINE and (
            values["x1_mm"],
            values["y1_mm"],
        ) == (values["x2_mm"], values["y2_mm"]):
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/parameters")
        if self.operation is ReviewedSketchOperation.SLOT and (
            values["x1_mm"],
            values["y1_mm"],
        ) == (values["x2_mm"], values["y2_mm"]):
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/parameters")
        positive = {"radius_mm", "width_mm"}
        if self.operation in {
            ReviewedSketchOperation.DISTANCE,
            ReviewedSketchOperation.LENGTH,
            ReviewedSketchOperation.RADIUS,
            ReviewedSketchOperation.DIAMETER,
        }:
            positive.add("value_mm")
        if any(values.get(key, 1.0) <= 0.0 for key in positive):
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/parameters")
        if self.operation is ReviewedSketchOperation.ARC and not (
            0.0 <= values["start_angle_rad"] < 2.0 * math.pi
            and 0.0 < values["sweep_angle_rad"] < 2.0 * math.pi
        ):
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/parameters")
        if self.operation is ReviewedSketchOperation.ANGLE and not (
            0.0 < values["value_rad"] < 2.0 * math.pi
        ):
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/parameters")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority": "none",
            "source": {
                "artifact_id": self.source_artifact_id,
                "graph_id": self.source_graph_id,
                "graph_sha256": self.source_graph_sha256,
                "content_sha256": self.source_content_sha256,
            },
            "binding": {
                "request_digest": self.request_digest,
                "adapter_contract_sha256": self.adapter_contract_sha256,
                "manifest_sha256": self.manifest_sha256,
                "operation_specification_sha256": self.operation_specification_sha256,
            },
            "selection": {
                "sketch_id": self.sketch_id,
                "node_id": self.node_id,
                "node_sha256": self.node_sha256,
            },
            "operation": self.operation.value,
            "parameters": [item.to_mapping() for item in self.parameters],
            "references": [item.to_mapping() for item in self.references],
            "results": [item.to_mapping() for item in self.results],
            "construction": self.construction,
            "mode": self.mode,
            "enabled": self.enabled,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping(), maximum=MAX_REVIEWED_SKETCH_PLAN_BYTES)

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_PLAN_DIGEST_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> ReviewedSketchBackendPlan:
        root = _exact_fields(
            value,
            {
                "schema_version",
                "authority",
                "source",
                "binding",
                "selection",
                "operation",
                "parameters",
                "references",
                "results",
                "construction",
                "mode",
                "enabled",
            },
            "/",
        )
        source = _exact_fields(
            root["source"],
            {"artifact_id", "graph_id", "graph_sha256", "content_sha256"},
            "/source",
        )
        binding = _exact_fields(
            root["binding"],
            {
                "request_digest",
                "adapter_contract_sha256",
                "manifest_sha256",
                "operation_specification_sha256",
            },
            "/binding",
        )
        selection = _exact_fields(
            root["selection"],
            {"sketch_id", "node_id", "node_sha256"},
            "/selection",
        )
        if root["authority"] != "none":
            _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/authority")
        try:
            operation = ReviewedSketchOperation(root["operation"])
        except (TypeError, ValueError):
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/operation")
        if (
            type(root["parameters"]) is not list
            or type(root["references"]) is not list
            or type(root["results"]) is not list
        ):
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/plan")
        if len(root["parameters"]) > 8 or len(root["references"]) > 3 or len(root["results"]) > 4:
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/plan")
        return cls(
            schema_version=root["schema_version"],
            source_artifact_id=source["artifact_id"],
            source_graph_id=source["graph_id"],
            source_graph_sha256=source["graph_sha256"],
            source_content_sha256=source["content_sha256"],
            request_digest=binding["request_digest"],
            adapter_contract_sha256=binding["adapter_contract_sha256"],
            manifest_sha256=binding["manifest_sha256"],
            operation_specification_sha256=binding["operation_specification_sha256"],
            sketch_id=selection["sketch_id"],
            node_id=selection["node_id"],
            node_sha256=selection["node_sha256"],
            operation=operation,
            parameters=tuple(
                ReviewedSketchParameter.from_mapping(item) for item in root["parameters"]
            ),
            references=tuple(
                ReviewedSketchReference.from_mapping(item) for item in root["references"]
            ),
            results=tuple(ReviewedSketchResult.from_mapping(item) for item in root["results"]),
            construction=root["construction"],
            mode=root["mode"],
            enabled=root["enabled"],
        )


def decode_reviewed_sketch_backend_plan(
    raw: object,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> ReviewedSketchBackendPlan:
    if expected_content_sha256 is not None:
        expected_content_sha256 = _digest(expected_content_sha256, "/expected_content_sha256")
    if expected_plan_sha256 is not None:
        expected_plan_sha256 = _digest(expected_plan_sha256, "/expected_plan_sha256")
    plan = ReviewedSketchBackendPlan.from_mapping(
        _decode_mapping(raw, maximum=MAX_REVIEWED_SKETCH_PLAN_BYTES)
    )
    if type(raw) is not bytes or not hmac.compare_digest(raw, plan.canonical_bytes):
        _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_content_sha256
    ):
        _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        plan.plan_sha256, expected_plan_sha256
    ):
        _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return plan


_NATIVE_OPERATION: Final = {
    ReviewedSketchOperation.POINT: "addGeometry.Point",
    ReviewedSketchOperation.LINE: "addGeometry.LineSegment",
    ReviewedSketchOperation.CIRCLE: "addGeometry.Circle",
    ReviewedSketchOperation.ARC: "addGeometry.ArcOfCircle",
    ReviewedSketchOperation.SLOT: "addGeometry.SlotCapsule",
    ReviewedSketchOperation.COINCIDENT: "addConstraint.Coincident",
    ReviewedSketchOperation.HORIZONTAL: "addConstraint.Horizontal",
    ReviewedSketchOperation.VERTICAL: "addConstraint.Vertical",
    ReviewedSketchOperation.PARALLEL: "addConstraint.Parallel",
    ReviewedSketchOperation.PERPENDICULAR: "addConstraint.Perpendicular",
    ReviewedSketchOperation.TANGENT: "addConstraint.Tangent",
    ReviewedSketchOperation.EQUAL: "addConstraint.Equal",
    ReviewedSketchOperation.SYMMETRIC: "addConstraint.Symmetric",
    ReviewedSketchOperation.DISTANCE: "addConstraint.Distance",
    ReviewedSketchOperation.DISTANCE_X: "addConstraint.DistanceX",
    ReviewedSketchOperation.DISTANCE_Y: "addConstraint.DistanceY",
    ReviewedSketchOperation.LENGTH: "addConstraint.DistanceLength",
    ReviewedSketchOperation.RADIUS: "addConstraint.Radius",
    ReviewedSketchOperation.DIAMETER: "addConstraint.Diameter",
    ReviewedSketchOperation.ANGLE: "addConstraint.Angle",
}
_NATIVE_CONSTRAINT_TYPE: Final = {
    ReviewedSketchOperation.COINCIDENT: "Coincident",
    ReviewedSketchOperation.HORIZONTAL: "Horizontal",
    ReviewedSketchOperation.VERTICAL: "Vertical",
    ReviewedSketchOperation.PARALLEL: "Parallel",
    ReviewedSketchOperation.PERPENDICULAR: "Perpendicular",
    ReviewedSketchOperation.TANGENT: "Tangent",
    ReviewedSketchOperation.EQUAL: "Equal",
    ReviewedSketchOperation.SYMMETRIC: "Symmetric",
    ReviewedSketchOperation.DISTANCE: "Distance",
    ReviewedSketchOperation.DISTANCE_X: "DistanceX",
    ReviewedSketchOperation.DISTANCE_Y: "DistanceY",
    ReviewedSketchOperation.LENGTH: "Distance",
    ReviewedSketchOperation.RADIUS: "Radius",
    ReviewedSketchOperation.DIAMETER: "Diameter",
    ReviewedSketchOperation.ANGLE: "Angle",
}
_NATIVE_GEOMETRY_TYPES: Final = {
    ReviewedSketchOperation.POINT: ("Part::GeomPoint",),
    ReviewedSketchOperation.LINE: ("Part::GeomLineSegment",),
    ReviewedSketchOperation.CIRCLE: ("Part::GeomCircle",),
    ReviewedSketchOperation.ARC: ("Part::GeomArcOfCircle",),
    ReviewedSketchOperation.SLOT: (
        "Part::GeomArcOfCircle",
        "Part::GeomArcOfCircle",
        "Part::GeomLineSegment",
        "Part::GeomLineSegment",
    ),
}
_PORT_OFFSETS: Final = {
    ReviewedSketchOperation.POINT: {"point": 0},
    ReviewedSketchOperation.LINE: {"curve": 0},
    ReviewedSketchOperation.CIRCLE: {"curve": 0},
    ReviewedSketchOperation.ARC: {"curve": 0},
    ReviewedSketchOperation.SLOT: {
        "cap_end": 0,
        "cap_start": 1,
        "side_a": 2,
        "side_b": 3,
    },
}
_SLOT_INTERNAL_CONSTRAINT_TYPES: Final = (
    "Tangent",
    "Tangent",
    "Tangent",
    "Tangent",
    "Equal",
)

REVIEWED_SKETCH_RULE_ID: Final = "freecad.sketch.reviewed.v1"
_RULE_CONTRACT = {
    "engine": {
        "name": "FreeCAD",
        "version": "1.1.0",
        "build_id": REVIEWED_SKETCH_FREECAD_ENGINE_BUILD_ID,
    },
    "native_type_id": REVIEWED_SKETCH_NATIVE_TYPE_ID,
    "operations": [
        {
            "operation": operation.value,
            "native_operation": _NATIVE_OPERATION[operation],
            "parameter_keys": list(_PARAMETER_KEYS.get(operation, ())),
            "reference_count": _REFERENCE_COUNT.get(operation, 0),
            "result_ports": list(_RESULT_PORTS.get(operation, ("constraint",))),
            "native_geometry_types": list(_NATIVE_GEOMETRY_TYPES.get(operation, ())),
            "native_constraint_type": _NATIVE_CONSTRAINT_TYPE.get(operation),
        }
        for operation in ReviewedSketchOperation
    ],
    "slot": {
        "meaning": "centerline-start-end-plus-total-width",
        "native_geometry_order": "cap-end-cap-start-side-a-side-b",
        "native_admission": "single-geometry-batch-then-single-constraint-batch",
        "tangent_binding": "point-aware-endpoint-tangent",
        "internal_constraints": list(_SLOT_INTERNAL_CONSTRAINT_TYPES),
        "expected_dof": 5,
    },
    "constraint_modes": {
        "driving": "all",
        "reference": "not-admitted-in-v1",
        "inactive": "all",
    },
    "solver": {
        "solve_result": 0,
        "reject": ["conflicting", "redundant", "partially_redundant", "malformed"],
    },
    "persistence": {
        "property": _METADATA_PROPERTY,
        "binding": "geometry-and-constraint-structure",
        "mutable_state": ["geometry-parameters", "dimensional-values"],
        "rollback_state_precision_decimal_places": 9,
    },
    "transaction": "shared-native-runner-exact-rollback",
}
REVIEWED_SKETCH_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN
    + json.dumps(
        _RULE_CONTRACT,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedSketchExecutionBindings:
    document: object
    sketch: object
    sketch_id: str

    def __post_init__(self) -> None:
        if self.document is None or self.sketch is None:
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/bindings")
        object.__setattr__(self, "sketch_id", _identifier(self.sketch_id, "/bindings/sketch_id"))


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedSketchNativeResult:
    result_id: str
    port_id: str
    geometry_index: int
    geometry_type_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/receipt/result_id"))
        object.__setattr__(self, "port_id", _identifier(self.port_id, "/receipt/port_id"))
        if type(self.geometry_index) is not int or not 0 <= self.geometry_index <= 1_000_000:
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/receipt/geometry_index")
        if type(self.geometry_type_id) is not str or self.geometry_type_id not in {
            "Part::GeomPoint",
            "Part::GeomLineSegment",
            "Part::GeomCircle",
            "Part::GeomArcOfCircle",
        }:
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/receipt/geometry_type_id")

    def to_mapping(self) -> dict[str, object]:
        return {
            "result_id": self.result_id,
            "port_id": self.port_id,
            "geometry_index": self.geometry_index,
            "geometry_type_id": self.geometry_type_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedSketchConformanceReceipt:
    plan_sha256: str
    operation: ReviewedSketchOperation
    sketch_object_name: str
    sketch_id: str
    node_id: str
    node_sha256: str
    native_results: tuple[ReviewedSketchNativeResult, ...]
    geometry_indices: tuple[int, ...]
    constraint_indices: tuple[int, ...]
    dof: int
    fully_constrained: bool
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/receipt/plan"))
        if type(self.operation) is not ReviewedSketchOperation:
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/receipt/operation")
        for name in ("sketch_object_name", "sketch_id", "node_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/receipt/{name}"))
        object.__setattr__(self, "node_sha256", _digest(self.node_sha256, "/receipt/node"))
        if (
            type(self.native_results) is not tuple
            or not all(type(item) is ReviewedSketchNativeResult for item in self.native_results)
            or type(self.geometry_indices) is not tuple
            or type(self.constraint_indices) is not tuple
            or not all(
                type(item) is int and 0 <= item <= 1_000_000 for item in self.geometry_indices
            )
            or not all(
                type(item) is int and 0 <= item <= 1_000_000 for item in self.constraint_indices
            )
            or type(self.dof) is not int
            or not -1 <= self.dof <= 4096
            or type(self.fully_constrained) is not bool
        ):
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/receipt")
        if len({item.result_id for item in self.native_results}) != len(self.native_results):
            _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/receipt/results")
        body = {
            "plan_sha256": self.plan_sha256,
            "operation": self.operation.value,
            "sketch_object_name": self.sketch_object_name,
            "sketch_id": self.sketch_id,
            "node_id": self.node_id,
            "node_sha256": self.node_sha256,
            "native_results": [item.to_mapping() for item in self.native_results],
            "geometry_indices": list(self.geometry_indices),
            "constraint_indices": list(self.constraint_indices),
            "dof": self.dof,
            "fully_constrained": self.fully_constrained,
        }
        object.__setattr__(
            self,
            "receipt_sha256",
            hashlib.sha256(
                _RECEIPT_DIGEST_DOMAIN
                + _canonical_json(body, maximum=MAX_REVIEWED_SKETCH_PLAN_BYTES)
            ).hexdigest(),
        )


def reviewed_sketch_node_sha256(value: object) -> str:
    """Content-address one adapter-normalized semantic node."""

    return hashlib.sha256(
        _NODE_DIGEST_DOMAIN + _canonical_json(value, maximum=MAX_REVIEWED_SKETCH_PLAN_BYTES)
    ).hexdigest()


def _empty_metadata(sketch_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "sketch_id": sketch_id,
        "geometries": [],
        "constraints": [],
    }


def _metadata_payload(value: object) -> str:
    return _canonical_json(value, maximum=MAX_REVIEWED_SKETCH_METADATA_BYTES).decode("ascii")


def _read_metadata(sketch: object, sketch_id: str) -> dict[str, object]:
    try:
        properties = tuple(sketch.PropertiesList)
        geometry_count = sketch.GeometryCount
        constraint_count = sketch.ConstraintCount
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.PRECONDITION_FAILED, "/sketch")
    if _METADATA_PROPERTY not in properties:
        if geometry_count != 0 or constraint_count != 0:
            _fail(ReviewedSketchRuleErrorCode.PRECONDITION_FAILED, "/sketch/ownership")
        return _empty_metadata(sketch_id)
    try:
        raw = getattr(sketch, _METADATA_PROPERTY).encode("ascii")
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata")
    value = _decode_mapping(raw, maximum=MAX_REVIEWED_SKETCH_METADATA_BYTES)
    root = _exact_fields(
        value,
        {"schema_version", "sketch_id", "geometries", "constraints"},
        "/sketch/metadata",
    )
    if root["schema_version"] != 1 or root["sketch_id"] != sketch_id:
        _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata")
    if type(root["geometries"]) is not list or type(root["constraints"]) is not list:
        _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata")
    if len(root["geometries"]) > 256 or len(root["constraints"]) > 512:
        _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata")
    return root


def _write_metadata(sketch: object, value: dict[str, object]) -> None:
    try:
        if _METADATA_PROPERTY not in tuple(sketch.PropertiesList):
            sketch.addProperty(
                "App::PropertyString",
                _METADATA_PROPERTY,
                "VibeCAD",
                "Content-bound reviewed SketchIntentGraph bindings",
            )
        setattr(sketch, _METADATA_PROPERTY, _metadata_payload(value))
        sketch.setEditorMode(_METADATA_PROPERTY, 2)
    except ReviewedSketchRuleError:
        raise
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/sketch/metadata")


def _integer_list(value: object, path: str, *, maximum: int) -> tuple[int, ...]:
    if (
        type(value) is not list
        or len(value) > maximum
        or not all(type(item) is int and 0 <= item <= 1_000_000 for item in value)
        or len(set(value)) != len(value)
    ):
        _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, path)
    return tuple(value)


def _fingerprint_number(value: object, path: str) -> float:
    result = round(_finite(value, path), 9)
    return 0.0 if result == 0.0 else result


def _vector_fingerprint(value: object, path: str) -> list[float]:
    try:
        return [
            _fingerprint_number(value.x, path),
            _fingerprint_number(value.y, path),
            _fingerprint_number(value.z, path),
        ]
    except ReviewedSketchRuleError:
        raise
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, path)


def _geometry_fingerprint(sketch: object, indices: tuple[int, ...]) -> str:
    try:
        body = [
            {
                "index": index,
                "type_id": sketch.Geometry[index].TypeId,
                "construction": bool(sketch.getConstruction(index)),
            }
            for index in indices
        ]
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/sketch/geometry")
    return hashlib.sha256(
        _GEOMETRY_FINGERPRINT_DOMAIN
        + b"structure\0"
        + _canonical_json(body, maximum=MAX_REVIEWED_SKETCH_METADATA_BYTES)
    ).hexdigest()


def _geometry_state_fingerprint(sketch: object, indices: tuple[int, ...]) -> str:
    body: list[dict[str, object]] = []
    try:
        for index in indices:
            geometry = sketch.Geometry[index]
            item: dict[str, object] = {
                "index": index,
                "type_id": geometry.TypeId,
                "construction": bool(sketch.getConstruction(index)),
            }
            if geometry.TypeId == "Part::GeomPoint":
                item["point"] = [
                    _fingerprint_number(geometry.X, "/sketch/geometry"),
                    _fingerprint_number(geometry.Y, "/sketch/geometry"),
                    0.0,
                ]
            elif geometry.TypeId == "Part::GeomLineSegment":
                item["start"] = _vector_fingerprint(
                    geometry.StartPoint,
                    "/sketch/geometry",
                )
                item["end"] = _vector_fingerprint(geometry.EndPoint, "/sketch/geometry")
            elif geometry.TypeId in {"Part::GeomCircle", "Part::GeomArcOfCircle"}:
                item["center"] = _vector_fingerprint(geometry.Center, "/sketch/geometry")
                item["axis"] = _vector_fingerprint(geometry.Axis, "/sketch/geometry")
                item["radius"] = _fingerprint_number(geometry.Radius, "/sketch/geometry")
                if geometry.TypeId == "Part::GeomArcOfCircle":
                    item["first_parameter"] = _fingerprint_number(
                        geometry.FirstParameter,
                        "/sketch/geometry",
                    )
                    item["last_parameter"] = _fingerprint_number(
                        geometry.LastParameter,
                        "/sketch/geometry",
                    )
            else:
                _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/sketch/geometry")
            body.append(item)
    except ReviewedSketchRuleError:
        raise
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/sketch/geometry")
    return hashlib.sha256(
        _GEOMETRY_FINGERPRINT_DOMAIN
        + _canonical_json(body, maximum=MAX_REVIEWED_SKETCH_METADATA_BYTES)
    ).hexdigest()


def _constraint_fingerprint(sketch: object, index: int) -> str:
    try:
        constraint = sketch.Constraints[index]
        body = {
            "index": index,
            "type": constraint.Type,
            "name": constraint.Name,
            "first": constraint.First,
            "first_pos": constraint.FirstPos,
            "second": constraint.Second,
            "second_pos": constraint.SecondPos,
            "third": constraint.Third,
            "third_pos": constraint.ThirdPos,
            "active": bool(sketch.getActive(index)),
        }
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/sketch/constraint")
    return hashlib.sha256(
        _CONSTRAINT_FINGERPRINT_DOMAIN + b"structure\0" + _canonical_json(body, maximum=16 * 1024)
    ).hexdigest()


def _constraint_state_fingerprint(sketch: object, index: int) -> str:
    try:
        constraint = sketch.Constraints[index]
        body = {
            "index": index,
            "type": constraint.Type,
            "name": constraint.Name,
            "first": constraint.First,
            "first_pos": constraint.FirstPos,
            "second": constraint.Second,
            "second_pos": constraint.SecondPos,
            "third": constraint.Third,
            "third_pos": constraint.ThirdPos,
            "value": _fingerprint_number(constraint.Value, "/sketch/constraint"),
            "active": bool(sketch.getActive(index)),
        }
    except ReviewedSketchRuleError:
        raise
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/sketch/constraint")
    return hashlib.sha256(
        _CONSTRAINT_FINGERPRINT_DOMAIN + _canonical_json(body, maximum=16 * 1024)
    ).hexdigest()


def _validated_metadata(
    sketch: object,
    sketch_id: str,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    metadata = _read_metadata(sketch, sketch_id)
    try:
        geometry_count = sketch.GeometryCount
        constraint_count = sketch.ConstraintCount
        geometry = tuple(sketch.Geometry)
        constraints = tuple(sketch.Constraints)
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.PRECONDITION_FAILED, "/sketch")
    result_entries: dict[str, dict[str, object]] = {}
    semantic_ids: set[str] = set()
    result_ids: set[str] = set()
    claimed_geometry_indices: set[int] = set()
    claimed_constraint_indices: set[int] = set()
    for index, raw in enumerate(metadata["geometries"]):
        entry = _exact_fields(
            raw,
            {
                "geometry_id",
                "node_sha256",
                "operation",
                "geometry_indices",
                "internal_constraint_indices",
                "native_fingerprint_sha256",
                "results",
            },
            f"/sketch/metadata/geometries/{index}",
        )
        geometry_id = _identifier(entry["geometry_id"], "/sketch/metadata/geometry_id")
        node_sha256 = _digest(entry["node_sha256"], "/sketch/metadata/node_sha256")
        native_fingerprint = _digest(
            entry["native_fingerprint_sha256"],
            "/sketch/metadata/native_fingerprint_sha256",
        )
        try:
            operation = ReviewedSketchOperation(entry["operation"])
        except (TypeError, ValueError):
            _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata/operation")
        if (
            operation not in _GEOMETRY_OPERATIONS
            or geometry_id in semantic_ids
            or geometry_id in result_ids
        ):
            _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata")
        semantic_ids.add(geometry_id)
        indices = _integer_list(
            entry["geometry_indices"],
            "/sketch/metadata/geometry_indices",
            maximum=4,
        )
        internal = _integer_list(
            entry["internal_constraint_indices"],
            "/sketch/metadata/internal_constraint_indices",
            maximum=9,
        )
        if len(indices) != len(_NATIVE_GEOMETRY_TYPES[operation]) or any(
            item >= geometry_count for item in indices
        ):
            _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata/geometry")
        if tuple(geometry[item].TypeId for item in indices) != _NATIVE_GEOMETRY_TYPES[operation]:
            _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata/geometry")
        if not hmac.compare_digest(native_fingerprint, _geometry_fingerprint(sketch, indices)):
            _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata/geometry")
        if any(item >= constraint_count for item in internal):
            _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata/constraint")
        if claimed_geometry_indices.intersection(
            indices
        ) or claimed_constraint_indices.intersection(internal):
            _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata/ownership")
        claimed_geometry_indices.update(indices)
        claimed_constraint_indices.update(internal)
        expected_internal = (
            _SLOT_INTERNAL_CONSTRAINT_TYPES if operation is ReviewedSketchOperation.SLOT else ()
        )
        if tuple(constraints[item].Type for item in internal) != expected_internal:
            _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata/constraint")
        if type(entry["results"]) is not list or len(entry["results"]) != len(
            _RESULT_PORTS[operation]
        ):
            _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata/results")
        for raw_result in entry["results"]:
            result = _exact_fields(
                raw_result,
                {"result_id", "port_id", "geometry_index", "geometry_type_id"},
                "/sketch/metadata/result",
            )
            result_id = _identifier(result["result_id"], "/sketch/metadata/result_id")
            port_id = _identifier(result["port_id"], "/sketch/metadata/port_id")
            geometry_index = result["geometry_index"]
            geometry_type_id = result["geometry_type_id"]
            if (
                result_id in result_ids
                or result_id in semantic_ids
                or port_id not in _PORT_OFFSETS[operation]
                or type(geometry_index) is not int
                or geometry_index != indices[_PORT_OFFSETS[operation][port_id]]
                or geometry_type_id != geometry[geometry_index].TypeId
            ):
                _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata/results")
            result_ids.add(result_id)
            result_entries[result_id] = {
                **result,
                "producer_geometry_id": geometry_id,
                "producer_node_sha256": node_sha256,
            }
    for index, raw in enumerate(metadata["constraints"]):
        entry = _exact_fields(
            raw,
            {
                "constraint_id",
                "node_sha256",
                "operation",
                "constraint_index",
                "result_id",
                "enabled",
                "native_fingerprint_sha256",
            },
            f"/sketch/metadata/constraints/{index}",
        )
        constraint_id = _identifier(entry["constraint_id"], "/sketch/metadata/constraint_id")
        _digest(entry["node_sha256"], "/sketch/metadata/node_sha256")
        try:
            operation = ReviewedSketchOperation(entry["operation"])
        except (TypeError, ValueError):
            _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata/operation")
        constraint_index = entry["constraint_index"]
        result_id = _identifier(entry["result_id"], "/sketch/metadata/result_id")
        native_fingerprint = _digest(
            entry["native_fingerprint_sha256"],
            "/sketch/metadata/native_fingerprint_sha256",
        )
        if (
            operation in _GEOMETRY_OPERATIONS
            or constraint_id in semantic_ids
            or constraint_id in result_ids
            or result_id in result_ids
            or result_id in semantic_ids
            or type(constraint_index) is not int
            or not 0 <= constraint_index < constraint_count
            or constraint_index in claimed_constraint_indices
            or type(entry["enabled"]) is not bool
            or constraints[constraint_index].Type != _NATIVE_CONSTRAINT_TYPE[operation]
        ):
            _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata/constraint")
        try:
            if (
                constraints[constraint_index].Name != constraint_id
                or bool(sketch.getActive(constraint_index)) is not entry["enabled"]
                or not hmac.compare_digest(
                    native_fingerprint,
                    _constraint_fingerprint(sketch, constraint_index),
                )
            ):
                _fail(
                    ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE,
                    "/sketch/metadata/constraint",
                )
        except ReviewedSketchRuleError:
            raise
        except Exception:
            _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata/constraint")
        semantic_ids.add(constraint_id)
        result_ids.add(result_id)
        claimed_constraint_indices.add(constraint_index)
    if claimed_geometry_indices != set(range(geometry_count)) or claimed_constraint_indices != set(
        range(constraint_count)
    ):
        _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata/ownership")
    return metadata, result_entries


def _solver_facts(sketch: object) -> tuple[int, int, bool]:
    try:
        solve_result = sketch.solve()
        dof = sketch.DoF
        fully_constrained = sketch.FullyConstrained
        constraint_count = sketch.ConstraintCount
        diagnostics = (
            tuple(sketch.ConflictingConstraints),
            tuple(sketch.RedundantConstraints),
            tuple(sketch.PartiallyRedundantConstraints),
            tuple(sketch.MalformedConstraints),
        )
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/solver")
    if (
        type(solve_result) is not int
        or type(dof) is not int
        or type(fully_constrained) is not bool
        or type(constraint_count) is not int
        or solve_result != 0
        or not 0 <= dof <= 4096
        or any(values for values in diagnostics)
    ):
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/solver")
    return solve_result, dof, fully_constrained


def _native_state_signature(sketch: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        geometry_count = sketch.GeometryCount
        constraint_count = sketch.ConstraintCount
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/sketch/stability")
    if (
        type(geometry_count) is not int
        or type(constraint_count) is not int
        or not 0 <= geometry_count <= 4096
        or not 0 <= constraint_count <= 8192
    ):
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/sketch/stability")
    return (
        tuple(_geometry_state_fingerprint(sketch, (index,)) for index in range(geometry_count)),
        tuple(_constraint_state_fingerprint(sketch, index) for index in range(constraint_count)),
    )


def _stabilized_solver_facts(document: object, sketch: object) -> tuple[int, int, bool]:
    previous: tuple[tuple[str, ...], tuple[str, ...]] | None = None
    facts: tuple[int, int, bool] | None = None
    for _ in range(8):
        try:
            document.recompute()
        except Exception:
            _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/document/recompute")
        facts = _solver_facts(sketch)
        current = _native_state_signature(sketch)
        if current == previous:
            return facts
        previous = current
    _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/sketch/stability")


def _snapshot(document: object, sketch: object) -> tuple[object, ...]:
    try:
        return (
            tuple(document.Objects),
            tuple(item.TypeId for item in sketch.Geometry),
            tuple((item.Type, getattr(item, "Name", None)) for item in sketch.Constraints),
            (
                None
                if _METADATA_PROPERTY not in tuple(sketch.PropertiesList)
                else getattr(sketch, _METADATA_PROPERTY)
            ),
            sketch.GeometryCount,
            sketch.ConstraintCount,
            sketch.DoF,
            sketch.FullyConstrained,
            _native_state_signature(sketch),
        )
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.PRECONDITION_FAILED, "/document/snapshot")


def _snapshot_matches(
    document: object,
    sketch: object,
    sketch_id: str,
    before: object,
) -> bool:
    try:
        _stabilized_solver_facts(document, sketch)
        exact = _snapshot(document, sketch) == before and not document.HasPendingTransaction
        if exact:
            _validated_metadata(sketch, sketch_id)
        return exact
    except Exception:
        return False


def _geometry_values(
    FreeCAD: object, Part: object, plan: ReviewedSketchBackendPlan
) -> tuple[object, ...]:
    values = {item.key: item.value for item in plan.parameters}
    try:
        vector = FreeCAD.Vector
        if plan.operation is ReviewedSketchOperation.POINT:
            return (Part.Point(vector(values["x_mm"], values["y_mm"], 0.0)),)
        if plan.operation is ReviewedSketchOperation.LINE:
            return (
                Part.LineSegment(
                    vector(values["x1_mm"], values["y1_mm"], 0.0),
                    vector(values["x2_mm"], values["y2_mm"], 0.0),
                ),
            )
        if plan.operation is ReviewedSketchOperation.CIRCLE:
            return (
                Part.Circle(
                    vector(values["cx_mm"], values["cy_mm"], 0.0),
                    vector(0.0, 0.0, 1.0),
                    values["radius_mm"],
                ),
            )
        if plan.operation is ReviewedSketchOperation.ARC:
            cx, cy = values["cx_mm"], values["cy_mm"]
            radius = values["radius_mm"]
            start = values["start_angle_rad"]
            middle = start + values["sweep_angle_rad"] / 2.0
            end = start + values["sweep_angle_rad"]

            def point(angle: float) -> object:
                return vector(cx + radius * math.cos(angle), cy + radius * math.sin(angle), 0.0)

            return (Part.Arc(point(start), point(middle), point(end)),)
        if plan.operation is ReviewedSketchOperation.SLOT:
            x1, y1 = values["x1_mm"], values["y1_mm"]
            x2, y2 = values["x2_mm"], values["y2_mm"]
            radius = values["width_mm"] / 2.0
            dx, dy = x2 - x1, y2 - y1
            length = math.hypot(dx, dy)
            ux, uy = dx / length, dy / length
            nx, ny = -uy, ux
            start_positive = vector(x1 + nx * radius, y1 + ny * radius, 0.0)
            end_positive = vector(x2 + nx * radius, y2 + ny * radius, 0.0)
            end_negative = vector(x2 - nx * radius, y2 - ny * radius, 0.0)
            start_negative = vector(x1 - nx * radius, y1 - ny * radius, 0.0)
            end_middle = vector(x2 + ux * radius, y2 + uy * radius, 0.0)
            start_middle = vector(x1 - ux * radius, y1 - uy * radius, 0.0)
            return (
                Part.Arc(end_positive, end_middle, end_negative),
                Part.Arc(start_negative, start_middle, start_positive),
                Part.LineSegment(end_positive, start_positive),
                Part.LineSegment(end_negative, start_negative),
            )
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/geometry")
    _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/operation")


def _slot_constraints(Sketcher: object, indices: tuple[int, ...]) -> tuple[object, ...]:
    cap_end, cap_start, side_a, side_b = indices
    try:
        make = Sketcher.Constraint
        return (
            make("Tangent", cap_end, 1, side_a, 1),
            make("Tangent", cap_end, 2, side_b, 1),
            make("Tangent", cap_start, 2, side_a, 2),
            make("Tangent", cap_start, 1, side_b, 2),
            make("Equal", cap_end, cap_start),
        )
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/geometry/slot")


def _slot_stabilizing_constraints(
    Sketcher: object,
    indices: tuple[int, ...],
    plan: ReviewedSketchBackendPlan,
) -> tuple[object, ...]:
    values = {item.key: item.value for item in plan.parameters}
    cap_end, cap_start, _, _ = indices
    try:
        make = Sketcher.Constraint
        return (
            make("DistanceX", cap_start, 3, values["x1_mm"]),
            make("DistanceY", cap_start, 3, values["y1_mm"]),
            make("DistanceX", cap_end, 3, values["x2_mm"]),
            make("DistanceY", cap_end, 3, values["y2_mm"]),
            make("Radius", cap_end, values["width_mm"] / 2.0),
        )
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/geometry/slot")


def _slot_matches_plan(
    sketch: object,
    indices: tuple[int, ...],
    plan: ReviewedSketchBackendPlan,
) -> bool:
    values = {item.key: item.value for item in plan.parameters}
    cap_end, cap_start, _, _ = indices
    try:
        expected = (
            values["x1_mm"],
            values["y1_mm"],
            values["x2_mm"],
            values["y2_mm"],
            values["width_mm"] / 2.0,
            values["width_mm"] / 2.0,
        )
        actual = (
            sketch.Geometry[cap_start].Center.x,
            sketch.Geometry[cap_start].Center.y,
            sketch.Geometry[cap_end].Center.x,
            sketch.Geometry[cap_end].Center.y,
            sketch.Geometry[cap_start].Radius,
            sketch.Geometry[cap_end].Radius,
        )
        return all(
            math.isclose(_finite(left, "/geometry/slot"), right, rel_tol=0.0, abs_tol=1e-8)
            for left, right in zip(actual, expected, strict=True)
        )
    except ReviewedSketchRuleError:
        raise
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/geometry/slot")


@dataclass(frozen=True, slots=True)
class _NativeReference:
    index: int
    point: int | None
    value_type: str
    role: str
    identity: tuple[str, str]


def _native_reference(
    reference: ReviewedSketchReference,
    *,
    sketch_id: str,
    result_entries: dict[str, dict[str, object]],
) -> _NativeReference:
    if reference.source_kind == "sketch":
        if reference.target_id != sketch_id or reference.role not in {
            "origin",
            "x_axis",
            "y_axis",
        }:
            _fail(ReviewedSketchRuleErrorCode.PRECONDITION_FAILED, "/references/sketch")
        index = {"origin": -1, "x_axis": -1, "y_axis": -2}[reference.role]
        point = 1 if reference.role == "origin" else None
        return _NativeReference(
            index=index,
            point=point,
            value_type="point" if point == 1 else "line",
            role=reference.role,
            identity=(reference.target_id, reference.role),
        )
    entry = result_entries.get(reference.target_id)
    if (
        entry is None
        or entry["producer_geometry_id"] != reference.producer_geometry_id
        or entry["producer_node_sha256"] != reference.producer_node_sha256
        or entry["port_id"] != reference.port_id
    ):
        _fail(ReviewedSketchRuleErrorCode.PRECONDITION_FAILED, "/references/result")
    native_type = entry["geometry_type_id"]
    value_type = {
        "Part::GeomPoint": "point",
        "Part::GeomLineSegment": "line",
        "Part::GeomCircle": "circle",
        "Part::GeomArcOfCircle": "arc",
    }.get(native_type)
    if value_type is None or value_type != reference.value_type:
        _fail(ReviewedSketchRuleErrorCode.PRECONDITION_FAILED, "/references/value_type")
    point = {
        ("point", "point"): 1,
        ("line", "start"): 1,
        ("line", "end"): 2,
        ("circle", "center"): 3,
        ("arc", "start"): 1,
        ("arc", "end"): 2,
        ("arc", "center"): 3,
    }.get((value_type, reference.role))
    if reference.role == "whole":
        point = None
    elif point is None:
        _fail(ReviewedSketchRuleErrorCode.PRECONDITION_FAILED, "/references/role")
    return _NativeReference(
        index=entry["geometry_index"],
        point=point,
        value_type=value_type,
        role=reference.role,
        identity=(reference.target_id, reference.role),
    )


def reviewed_sketch_native_operation(operation: ReviewedSketchOperation) -> str:
    """Return the reviewed native operation literal for one exact semantic enum."""

    if type(operation) is not ReviewedSketchOperation:
        _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/operation")
    return _NATIVE_OPERATION[operation]


def _validate_reference_contract(
    operation: ReviewedSketchOperation,
    references: tuple[_NativeReference, ...],
) -> None:
    def pointlike(item: _NativeReference) -> bool:
        return item.point is not None and item.value_type in {
            "point",
            "line",
            "circle",
            "arc",
        }

    def line(item: _NativeReference) -> bool:
        return item.value_type == "line" and item.point is None

    def circular(item: _NativeReference) -> bool:
        return item.value_type in {"circle", "arc"} and item.point is None

    if len({item.identity for item in references}) != len(references):
        _fail(ReviewedSketchRuleErrorCode.PRECONDITION_FAILED, "/references/duplicate")
    if operation in {
        ReviewedSketchOperation.COINCIDENT,
        ReviewedSketchOperation.DISTANCE,
        ReviewedSketchOperation.DISTANCE_X,
        ReviewedSketchOperation.DISTANCE_Y,
    }:
        valid = all(pointlike(item) for item in references)
    elif operation in {
        ReviewedSketchOperation.HORIZONTAL,
        ReviewedSketchOperation.VERTICAL,
        ReviewedSketchOperation.LENGTH,
    }:
        valid = len(references) == 1 and line(references[0])
    elif operation in {
        ReviewedSketchOperation.PARALLEL,
        ReviewedSketchOperation.PERPENDICULAR,
        ReviewedSketchOperation.ANGLE,
    }:
        valid = len(references) == 2 and all(line(item) for item in references)
    elif operation is ReviewedSketchOperation.TANGENT:
        valid = (
            len(references) == 2
            and all(line(item) or circular(item) for item in references)
            and not all(line(item) for item in references)
        )
    elif operation is ReviewedSketchOperation.EQUAL:
        valid = len(references) == 2 and (
            all(line(item) for item in references) or all(circular(item) for item in references)
        )
    elif operation is ReviewedSketchOperation.SYMMETRIC:
        valid = (
            len(references) == 3
            and pointlike(references[0])
            and pointlike(references[1])
            and line(references[2])
        )
    elif operation in {ReviewedSketchOperation.RADIUS, ReviewedSketchOperation.DIAMETER}:
        valid = len(references) == 1 and circular(references[0])
    else:
        valid = False
    if not valid:
        _fail(ReviewedSketchRuleErrorCode.PRECONDITION_FAILED, "/references/signature")


def _constraint_value(
    Sketcher: object,
    plan: ReviewedSketchBackendPlan,
    references: tuple[_NativeReference, ...],
) -> object:
    _validate_reference_contract(plan.operation, references)
    values = {item.key: item.value for item in plan.parameters}
    try:
        make = Sketcher.Constraint
        operation = plan.operation
        if operation is ReviewedSketchOperation.COINCIDENT:
            return make(
                "Coincident",
                references[0].index,
                references[0].point,
                references[1].index,
                references[1].point,
            )
        if operation in {ReviewedSketchOperation.HORIZONTAL, ReviewedSketchOperation.VERTICAL}:
            return make(_NATIVE_CONSTRAINT_TYPE[operation], references[0].index)
        if operation in {
            ReviewedSketchOperation.PARALLEL,
            ReviewedSketchOperation.PERPENDICULAR,
            ReviewedSketchOperation.TANGENT,
            ReviewedSketchOperation.EQUAL,
        }:
            return make(
                _NATIVE_CONSTRAINT_TYPE[operation],
                references[0].index,
                references[1].index,
            )
        if operation is ReviewedSketchOperation.SYMMETRIC:
            return make(
                "Symmetric",
                references[0].index,
                references[0].point,
                references[1].index,
                references[1].point,
                references[2].index,
            )
        if operation in {
            ReviewedSketchOperation.DISTANCE,
            ReviewedSketchOperation.DISTANCE_X,
            ReviewedSketchOperation.DISTANCE_Y,
        }:
            return make(
                _NATIVE_CONSTRAINT_TYPE[operation],
                references[0].index,
                references[0].point,
                references[1].index,
                references[1].point,
                values["value_mm"],
            )
        if operation is ReviewedSketchOperation.LENGTH:
            return make("Distance", references[0].index, values["value_mm"])
        if operation in {ReviewedSketchOperation.RADIUS, ReviewedSketchOperation.DIAMETER}:
            return make(
                _NATIVE_CONSTRAINT_TYPE[operation],
                references[0].index,
                values["value_mm"],
            )
        if operation is ReviewedSketchOperation.ANGLE:
            return make(
                "Angle",
                references[0].index,
                references[1].index,
                values["value_rad"],
            )
    except ReviewedSketchRuleError:
        raise
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/constraint/construct")
    _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/operation")


def _validate_bindings(
    plan: ReviewedSketchBackendPlan,
    bindings: ReviewedSketchExecutionBindings,
) -> tuple[dict[str, object], dict[str, dict[str, object]], int]:
    if bindings.sketch_id != plan.sketch_id:
        _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/bindings/identity")
    document, sketch = bindings.document, bindings.sketch
    try:
        if (
            getattr(document, "UndoMode", 0) != 1
            or bool(document.HasPendingTransaction)
            or sketch.Document is not document
            or sketch.TypeId != REVIEWED_SKETCH_NATIVE_TYPE_ID
            or document.getObject(sketch.Name) is not sketch
        ):
            _fail(ReviewedSketchRuleErrorCode.PRECONDITION_FAILED, "/bindings")
    except ReviewedSketchRuleError:
        raise
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.PRECONDITION_FAILED, "/bindings")
    _, dof, _ = _stabilized_solver_facts(document, sketch)
    metadata, results = _validated_metadata(sketch, bindings.sketch_id)
    return metadata, results, dof


def _native_results(
    plan: ReviewedSketchBackendPlan,
    indices: tuple[int, ...],
    sketch: object,
) -> tuple[ReviewedSketchNativeResult, ...]:
    results: list[ReviewedSketchNativeResult] = []
    by_port = {item.port_id: item for item in plan.results}
    for port_id, offset in sorted(_PORT_OFFSETS[plan.operation].items()):
        result = by_port[port_id]
        index = indices[offset]
        try:
            geometry_type_id = sketch.Geometry[index].TypeId
        except Exception:
            _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/geometry/result")
        results.append(
            ReviewedSketchNativeResult(
                result_id=result.result_id,
                port_id=port_id,
                geometry_index=index,
                geometry_type_id=geometry_type_id,
            )
        )
    return tuple(results)


def _append_geometry_metadata(
    metadata: dict[str, object],
    plan: ReviewedSketchBackendPlan,
    indices: tuple[int, ...],
    internal: tuple[int, ...],
    results: tuple[ReviewedSketchNativeResult, ...],
) -> None:
    geometries = metadata["geometries"]
    constraints = metadata["constraints"]
    if type(geometries) is not list or type(constraints) is not list:
        _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata")
    existing_ids = {
        *(item["geometry_id"] for item in geometries),
        *(result["result_id"] for item in geometries for result in item["results"]),
        *(item["constraint_id"] for item in constraints),
        *(item["result_id"] for item in constraints),
    }
    new_ids = {plan.node_id, *(item.result_id for item in results)}
    if existing_ids.intersection(new_ids) or len(new_ids) != 1 + len(results):
        _fail(ReviewedSketchRuleErrorCode.PRECONDITION_FAILED, "/geometry/identity")
    geometries.append(
        {
            "geometry_id": plan.node_id,
            "node_sha256": plan.node_sha256,
            "operation": plan.operation.value,
            "geometry_indices": list(indices),
            "internal_constraint_indices": list(internal),
            "native_fingerprint_sha256": "0" * 64,
            "results": [item.to_mapping() for item in results],
        }
    )


def _append_constraint_metadata(
    metadata: dict[str, object],
    plan: ReviewedSketchBackendPlan,
    index: int,
) -> None:
    constraints = metadata["constraints"]
    geometries = metadata["geometries"]
    if type(geometries) is not list or type(constraints) is not list:
        _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata")
    existing_ids = {
        *(item["geometry_id"] for item in geometries),
        *(result["result_id"] for item in geometries for result in item["results"]),
        *(item["constraint_id"] for item in constraints),
        *(item["result_id"] for item in constraints),
    }
    new_ids = {plan.node_id, plan.results[0].result_id}
    if existing_ids.intersection(new_ids) or len(new_ids) != 2:
        _fail(ReviewedSketchRuleErrorCode.PRECONDITION_FAILED, "/constraint/identity")
    constraints.append(
        {
            "constraint_id": plan.node_id,
            "node_sha256": plan.node_sha256,
            "operation": plan.operation.value,
            "constraint_index": index,
            "result_id": plan.results[0].result_id,
            "enabled": plan.enabled,
            "native_fingerprint_sha256": "0" * 64,
        }
    )


def _refresh_metadata_fingerprints(metadata: dict[str, object], sketch: object) -> None:
    geometries = metadata["geometries"]
    constraints = metadata["constraints"]
    if type(geometries) is not list or type(constraints) is not list:
        _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata")
    for item in geometries:
        indices = _integer_list(
            item["geometry_indices"],
            "/sketch/metadata/geometry_indices",
            maximum=4,
        )
        item["native_fingerprint_sha256"] = _geometry_fingerprint(sketch, indices)
    for item in constraints:
        index = item["constraint_index"]
        if type(index) is not int:
            _fail(ReviewedSketchRuleErrorCode.INTEGRITY_FAILURE, "/sketch/metadata")
        item["native_fingerprint_sha256"] = _constraint_fingerprint(sketch, index)


def _apply_geometry(
    FreeCAD: object,
    Part: object,
    Sketcher: object,
    plan: ReviewedSketchBackendPlan,
    bindings: ReviewedSketchExecutionBindings,
    metadata: dict[str, object],
    before_dof: int,
) -> ReviewedSketchConformanceReceipt:
    document, sketch = bindings.document, bindings.sketch
    values = _geometry_values(FreeCAD, Part, plan)
    try:
        if plan.operation is ReviewedSketchOperation.SLOT:
            added_geometry = sketch.addGeometry(list(values), plan.construction)
            if type(added_geometry) not in {list, tuple} or not all(
                type(item) is int for item in added_geometry
            ):
                _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/geometry/add")
            indices = tuple(added_geometry)
        else:
            indices = tuple(sketch.addGeometry(value, plan.construction) for value in values)
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/geometry/add")
    expected_types = _NATIVE_GEOMETRY_TYPES[plan.operation]
    try:
        if (
            len(indices) != len(expected_types)
            or tuple(sketch.Geometry[index].TypeId for index in indices) != expected_types
            or any(
                bool(sketch.getConstruction(index)) is not plan.construction for index in indices
            )
        ):
            _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/geometry/type")
    except ReviewedSketchRuleError:
        raise
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/geometry/type")
    internal: tuple[int, ...] = ()
    if plan.operation is ReviewedSketchOperation.SLOT:
        try:
            internal_values = _slot_constraints(Sketcher, indices)
            stabilizing_values = _slot_stabilizing_constraints(Sketcher, indices, plan)
            added_constraints = sketch.addConstraint(list((*internal_values, *stabilizing_values)))
            if type(added_constraints) not in {list, tuple} or not all(
                type(item) is int for item in added_constraints
            ):
                _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/geometry/slot")
            internal_count = len(internal_values)
            if len(added_constraints) != internal_count + len(stabilizing_values):
                _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/geometry/slot")
            internal = tuple(added_constraints[:internal_count])
            temporary = tuple(added_constraints[internal_count:])
            for offset, constraint_index in enumerate(internal):
                sketch.renameConstraint(constraint_index, f"slot_{plan.node_id}_{offset}")
            _, temporary_dof, _ = _solver_facts(sketch)
            if temporary_dof != before_dof:
                _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/geometry/slot")
            sketch.delConstraints(list(temporary), False)
        except Exception:
            _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/geometry/slot")
    _, after_dof, fully_constrained = _stabilized_solver_facts(document, sketch)
    expected_delta = {
        ReviewedSketchOperation.POINT: 2,
        ReviewedSketchOperation.LINE: 4,
        ReviewedSketchOperation.CIRCLE: 3,
        ReviewedSketchOperation.ARC: 5,
        ReviewedSketchOperation.SLOT: 5,
    }[plan.operation]
    if after_dof - before_dof != expected_delta or (
        plan.operation is ReviewedSketchOperation.SLOT
        and not _slot_matches_plan(sketch, indices, plan)
    ):
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/geometry/dof")
    native_results = _native_results(plan, indices, sketch)
    _append_geometry_metadata(metadata, plan, indices, internal, native_results)
    _refresh_metadata_fingerprints(metadata, sketch)
    _write_metadata(sketch, metadata)
    return ReviewedSketchConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        sketch_object_name=sketch.Name,
        sketch_id=plan.sketch_id,
        node_id=plan.node_id,
        node_sha256=plan.node_sha256,
        native_results=native_results,
        geometry_indices=indices,
        constraint_indices=internal,
        dof=after_dof,
        fully_constrained=fully_constrained,
    )


def _apply_constraint(
    Sketcher: object,
    plan: ReviewedSketchBackendPlan,
    bindings: ReviewedSketchExecutionBindings,
    metadata: dict[str, object],
    result_entries: dict[str, dict[str, object]],
    before_dof: int,
) -> ReviewedSketchConformanceReceipt:
    document, sketch = bindings.document, bindings.sketch
    references = tuple(
        _native_reference(
            item,
            sketch_id=plan.sketch_id,
            result_entries=result_entries,
        )
        for item in plan.references
    )
    native_constraint = _constraint_value(Sketcher, plan, references)
    try:
        index = sketch.addConstraint(native_constraint)
        sketch.renameConstraint(index, plan.node_id)
        if not plan.enabled:
            sketch.setActive(index, False)
    except Exception:
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/constraint/add")
    _, after_dof, fully_constrained = _stabilized_solver_facts(document, sketch)
    try:
        valid_native = (
            sketch.Constraints[index].Type == _NATIVE_CONSTRAINT_TYPE[plan.operation]
            and sketch.Constraints[index].Name == plan.node_id
            and bool(sketch.getActive(index)) is plan.enabled
        )
    except Exception:
        valid_native = False
    if (
        not valid_native
        or (plan.enabled and after_dof >= before_dof)
        or (not plan.enabled and after_dof != before_dof)
    ):
        _fail(ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED, "/constraint/result")
    _append_constraint_metadata(metadata, plan, index)
    _refresh_metadata_fingerprints(metadata, sketch)
    _write_metadata(sketch, metadata)
    return ReviewedSketchConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        sketch_object_name=sketch.Name,
        sketch_id=plan.sketch_id,
        node_id=plan.node_id,
        node_sha256=plan.node_sha256,
        native_results=(),
        geometry_indices=(),
        constraint_indices=(index,),
        dof=after_dof,
        fully_constrained=fully_constrained,
    )


def apply_reviewed_sketch_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: ReviewedSketchExecutionBindings,
) -> ReviewedSketchConformanceReceipt:
    """Apply one exact reviewed Sketch plan under a rollback-proven transaction."""

    if type(bindings) is not ReviewedSketchExecutionBindings:
        _fail(ReviewedSketchRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415
        import Part  # type: ignore[import-not-found]  # noqa: PLC0415
        import Sketcher  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(ReviewedSketchRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != REVIEWED_SKETCH_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(ReviewedSketchRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_reviewed_sketch_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    metadata, result_entries, before_dof = _validate_bindings(plan, bindings)
    document, sketch = bindings.document, bindings.sketch
    runner = NativeTransactionRunner()

    def apply() -> ReviewedSketchConformanceReceipt:
        if plan.operation in _GEOMETRY_OPERATIONS:
            return _apply_geometry(
                FreeCAD,
                Part,
                Sketcher,
                plan,
                bindings,
                metadata,
                before_dof,
            )
        return _apply_constraint(
            Sketcher,
            plan,
            bindings,
            metadata,
            result_entries,
            before_dof,
        )

    try:
        return runner.run(
            document,
            label=f"VibeCAD reviewed sketch {plan.operation.value}",
            snapshot=lambda: _snapshot(document, sketch),
            apply=apply,
            rollback_matches=lambda before: _snapshot_matches(
                document,
                sketch,
                bindings.sketch_id,
                before,
            ),
        )
    except NativeTransactionError:
        _fail(ReviewedSketchRuleErrorCode.TRANSACTION_FAILED, "/document/transaction")


__all__ = [
    "MAX_REVIEWED_SKETCH_PLAN_BYTES",
    "REVIEWED_SKETCH_FREECAD_ENGINE_BUILD_ID",
    "REVIEWED_SKETCH_NATIVE_TYPE_ID",
    "REVIEWED_SKETCH_PLAN_MEDIA_TYPE",
    "REVIEWED_SKETCH_RULE_CONTRACT_SHA256",
    "REVIEWED_SKETCH_RULE_ID",
    "ReviewedSketchBackendPlan",
    "ReviewedSketchConformanceReceipt",
    "ReviewedSketchExecutionBindings",
    "ReviewedSketchNativeResult",
    "ReviewedSketchOperation",
    "ReviewedSketchParameter",
    "ReviewedSketchReference",
    "ReviewedSketchResult",
    "ReviewedSketchRuleError",
    "ReviewedSketchRuleErrorCode",
    "apply_reviewed_sketch_plan",
    "decode_reviewed_sketch_backend_plan",
    "reviewed_sketch_native_operation",
    "reviewed_sketch_node_sha256",
]
