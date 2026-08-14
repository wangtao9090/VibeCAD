"""Trusted FreeCAD rules for the reviewed PartDesign primitive batch.

The wire plan contains only reviewed operation identities and backend-neutral
semantic parameters.  Native ``TypeId`` and property selection is exclusively
owned by the static table in this module.  Importing the module does not import
FreeCAD and neither plans nor receipts grant execution authority.
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

PARTDESIGN_PRIMITIVE_PLAN_SCHEMA_VERSION: Final = 1
PARTDESIGN_PRIMITIVE_PLAN_MEDIA_TYPE: Final = (
    "application/vnd.vibecad.freecad-partdesign-primitive-plan+json"
)
MAX_PARTDESIGN_PRIMITIVE_PLAN_BYTES: Final = 32 * 1024
PARTDESIGN_PRIMITIVE_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-primitive-plan.v1\0"
_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-partdesign-primitive-rule.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-primitive-receipt.v1\0"


class PartDesignPrimitiveOperation(StrEnum):
    ADDITIVE_BOX = "additive_box"
    SUBTRACTIVE_BOX = "subtractive_box"
    ADDITIVE_CYLINDER = "additive_cylinder"
    SUBTRACTIVE_CYLINDER = "subtractive_cylinder"
    ADDITIVE_SPHERE = "additive_sphere"
    SUBTRACTIVE_SPHERE = "subtractive_sphere"
    ADDITIVE_CONE = "additive_cone"
    SUBTRACTIVE_CONE = "subtractive_cone"
    ADDITIVE_ELLIPSOID = "additive_ellipsoid"
    SUBTRACTIVE_ELLIPSOID = "subtractive_ellipsoid"
    ADDITIVE_PRISM = "additive_prism"
    SUBTRACTIVE_PRISM = "subtractive_prism"
    ADDITIVE_WEDGE = "additive_wedge"
    SUBTRACTIVE_WEDGE = "subtractive_wedge"
    ADDITIVE_TORUS = "additive_torus"
    SUBTRACTIVE_TORUS = "subtractive_torus"


@dataclass(frozen=True, slots=True)
class _NativeParameterSpec:
    semantic_key: str
    property_name: str
    kind: str
    minimum: float
    maximum: float


@dataclass(frozen=True, slots=True)
class _NativePrimitiveSpec:
    type_id: str
    family: str
    additive: bool
    object_prefix: str
    parameters: tuple[_NativeParameterSpec, ...]
    fixed_properties: tuple[tuple[str, float], ...] = ()


def _parameter(
    semantic_key: str,
    property_name: str,
    *,
    kind: str = "float",
    minimum: float = 0.01,
    maximum: float = 1_000_000.0,
) -> _NativeParameterSpec:
    return _NativeParameterSpec(semantic_key, property_name, kind, minimum, maximum)


_BOX = (
    _parameter("size_x_mm", "Length"),
    _parameter("size_y_mm", "Width"),
    _parameter("size_z_mm", "Height"),
)
_CYLINDER = (
    _parameter("radius_mm", "Radius"),
    _parameter("height_mm", "Height"),
    _parameter("sweep_degrees", "Angle", minimum=0.01, maximum=360.0),
)
_SPHERE = (
    _parameter("radius_mm", "Radius"),
    _parameter("latitude_min_degrees", "Angle1", minimum=-90.0, maximum=89.99),
    _parameter("latitude_max_degrees", "Angle2", minimum=-89.99, maximum=90.0),
    _parameter("sweep_degrees", "Angle3", minimum=0.01, maximum=360.0),
)
_CONE = (
    _parameter("base_radius_mm", "Radius1"),
    _parameter("top_radius_mm", "Radius2"),
    _parameter("height_mm", "Height"),
    _parameter("sweep_degrees", "Angle", minimum=0.01, maximum=360.0),
)
_ELLIPSOID = (
    _parameter("radius_x_mm", "Radius1"),
    _parameter("radius_y_mm", "Radius2"),
    _parameter("radius_z_mm", "Radius3"),
    _parameter("latitude_min_degrees", "Angle1", minimum=-90.0, maximum=89.99),
    _parameter("latitude_max_degrees", "Angle2", minimum=-89.99, maximum=90.0),
    _parameter("sweep_degrees", "Angle3", minimum=0.01, maximum=360.0),
)
_PRISM = (
    _parameter("side_count", "Polygon", kind="integer", minimum=3, maximum=64),
    _parameter("circumradius_mm", "Circumradius"),
    _parameter("height_mm", "Height"),
)
_WEDGE = tuple(
    _parameter(key, prop, minimum=-1_000_000.0, maximum=1_000_000.0)
    for key, prop in (
        ("x_min_mm", "Xmin"),
        ("y_min_mm", "Ymin"),
        ("z_min_mm", "Zmin"),
        ("x_inner_min_mm", "X2min"),
        ("z_inner_min_mm", "Z2min"),
        ("x_max_mm", "Xmax"),
        ("y_max_mm", "Ymax"),
        ("z_max_mm", "Zmax"),
        ("x_inner_max_mm", "X2max"),
        ("z_inner_max_mm", "Z2max"),
    )
)
_TORUS = (
    _parameter("major_radius_mm", "Radius1"),
    _parameter("minor_radius_mm", "Radius2"),
    _parameter("latitude_min_degrees", "Angle1", minimum=-180.0, maximum=179.99),
    _parameter("latitude_max_degrees", "Angle2", minimum=-179.99, maximum=180.0),
    _parameter("sweep_degrees", "Angle3", minimum=0.01, maximum=360.0),
)


def _specs_for(
    family: str,
    parameters: tuple[_NativeParameterSpec, ...],
    *,
    fixed_properties: tuple[tuple[str, float], ...] = (),
) -> tuple[tuple[PartDesignPrimitiveOperation, _NativePrimitiveSpec], ...]:
    title = family.title()
    return tuple(
        (
            PartDesignPrimitiveOperation(f"{mode.lower()}_{family}"),
            _NativePrimitiveSpec(
                type_id=f"PartDesign::{mode}{title}",
                family=family,
                additive=mode == "Additive",
                object_prefix=f"{mode}{title}",
                parameters=parameters,
                fixed_properties=fixed_properties,
            ),
        )
        for mode in ("Additive", "Subtractive")
    )


# The sole semantic-operation -> native-code selection table.  No graph or plan
# field is ever interpreted as a TypeId or a native property name.
_NATIVE_PRIMITIVE_SPECS: Final = dict(
    (
        *_specs_for("box", _BOX),
        *_specs_for(
            "cylinder",
            _CYLINDER,
            fixed_properties=(("FirstAngle", 0.0), ("SecondAngle", 0.0)),
        ),
        *_specs_for("sphere", _SPHERE),
        *_specs_for("cone", _CONE),
        *_specs_for("ellipsoid", _ELLIPSOID),
        *_specs_for(
            "prism",
            _PRISM,
            fixed_properties=(("FirstAngle", 0.0), ("SecondAngle", 0.0)),
        ),
        *_specs_for("wedge", _WEDGE),
        *_specs_for("torus", _TORUS),
    )
)

PARTDESIGN_PRIMITIVE_RULE_ID: Final = "freecad.partdesign.primitives.v1"
_NATIVE_CONTRACT = {
    "engine": {
        "name": "FreeCAD",
        "version": "1.1.0",
        "build_id": PARTDESIGN_PRIMITIVE_FREECAD_ENGINE_BUILD_ID,
    },
    "common": {
        "map_mode": "Deactivated",
        "refine": True,
        "single_solid": True,
        "base_feature": "previous-tip-or-none",
        "volume": "strict-direction",
        "transaction": "rollback",
    },
    "operations": [
        {
            "operation": operation.value,
            "type_id": spec.type_id,
            "additive": spec.additive,
            "parameters": [
                {
                    "semantic_key": item.semantic_key,
                    "property_name": item.property_name,
                    "kind": item.kind,
                    "minimum": item.minimum,
                    "maximum": item.maximum,
                }
                for item in spec.parameters
            ],
            "fixed_properties": list(spec.fixed_properties),
        }
        for operation, spec in _NATIVE_PRIMITIVE_SPECS.items()
    ],
}
PARTDESIGN_PRIMITIVE_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN
    + json.dumps(
        _NATIVE_CONTRACT,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
).hexdigest()


class PartDesignPrimitiveRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class PartDesignPrimitiveRuleError(ValueError):
    """Bounded, non-reflective failure at the trusted native boundary."""

    def __init__(self, code: PartDesignPrimitiveRuleErrorCode, path: str = "/") -> None:
        if type(code) is not PartDesignPrimitiveRuleErrorCode:
            raise TypeError("code must be a PartDesignPrimitiveRuleErrorCode")
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
        super().__init__(f"PartDesign primitive rule error ({code.value}) at {path}")


def _fail(code: PartDesignPrimitiveRuleErrorCode, path: str) -> None:
    raise PartDesignPrimitiveRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, path)
    return value


def _finite(value: object, path: str) -> float:
    if type(value) not in {int, float}:
        _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, path)
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, path)
    if not math.isfinite(result):
        _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, path)
    return result


def _canonical_json(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/")
    if not raw or len(raw) > MAX_PARTDESIGN_PRIMITIVE_PLAN_BYTES:
        _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/")
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


def _decode_mapping(raw: object) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_PARTDESIGN_PRIMITIVE_PLAN_BYTES:
        _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(PartDesignPrimitiveRuleErrorCode.INTEGRITY_FAILURE, "/")
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/")
    if type(value) is not dict or not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(PartDesignPrimitiveRuleErrorCode.INTEGRITY_FAILURE, "/")
    return value


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class SemanticObjectSelection:
    node_id: str
    result_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/selection/node_id"))
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/selection/result_id"))

    def to_mapping(self) -> dict[str, str]:
        return {"node_id": self.node_id, "result_id": self.result_id}

    @classmethod
    def from_mapping(cls, value: object, path: str) -> SemanticObjectSelection:
        item = _exact_fields(value, {"node_id", "result_id"}, path)
        return cls(node_id=item["node_id"], result_id=item["result_id"])


@dataclass(frozen=True, slots=True, kw_only=True)
class PrimitiveParameterSet:
    """Immutable validated semantic values; it contains no native names."""

    operation: PartDesignPrimitiveOperation
    shape_values: tuple[int | float, ...]
    translation_mm: tuple[float, float, float]
    rotation_axis: tuple[float, float, float]
    rotation_degrees: float

    def __post_init__(self) -> None:
        if type(self.operation) is not PartDesignPrimitiveOperation:
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/operation/id")
        spec = _NATIVE_PRIMITIVE_SPECS[self.operation]
        if type(self.shape_values) is not tuple or len(self.shape_values) != len(spec.parameters):
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/parameters/shape")
        validated: list[int | float] = []
        for index, (value, parameter) in enumerate(
            zip(self.shape_values, spec.parameters, strict=True)
        ):
            path = f"/parameters/shape/{index}"
            if parameter.kind == "integer":
                if type(value) is not int or not parameter.minimum <= value <= parameter.maximum:
                    _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, path)
                validated.append(value)
            else:
                numeric = _finite(value, path)
                if not parameter.minimum <= numeric <= parameter.maximum:
                    _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, path)
                validated.append(numeric)
        object.__setattr__(self, "shape_values", tuple(validated))
        for name in ("translation_mm", "rotation_axis"):
            raw = getattr(self, name)
            if type(raw) is not tuple or len(raw) != 3:
                _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, f"/parameters/{name}")
            converted = tuple(
                _finite(value, f"/parameters/{name}/{index}") for index, value in enumerate(raw)
            )
            object.__setattr__(self, name, converted)
        if any(abs(value) > 1_000_000.0 for value in self.translation_mm):
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/parameters/translation_mm")
        axis_norm = math.sqrt(sum(value * value for value in self.rotation_axis))
        if not math.isclose(axis_norm, 1.0, rel_tol=0.0, abs_tol=1e-9):
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/parameters/rotation_axis")
        angle = _finite(self.rotation_degrees, "/parameters/rotation_degrees")
        if not -360.0 <= angle <= 360.0:
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/parameters/rotation_degrees")
        object.__setattr__(self, "rotation_degrees", angle)
        self._validate_relations(spec)

    def _validate_relations(self, spec: _NativePrimitiveSpec) -> None:
        values = dict(
            zip((item.semantic_key for item in spec.parameters), self.shape_values, strict=True)
        )
        if spec.family in {"sphere", "ellipsoid", "torus"} and not (
            values["latitude_min_degrees"] < values["latitude_max_degrees"]
        ):
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/parameters/shape")
        if spec.family == "torus" and not values["major_radius_mm"] > values["minor_radius_mm"]:
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/parameters/shape")
        if spec.family == "wedge" and not (
            values["x_min_mm"] < values["x_max_mm"]
            and values["y_min_mm"] < values["y_max_mm"]
            and values["z_min_mm"] < values["z_max_mm"]
            and values["x_min_mm"]
            <= values["x_inner_min_mm"]
            <= values["x_inner_max_mm"]
            <= values["x_max_mm"]
            and values["z_min_mm"]
            <= values["z_inner_min_mm"]
            <= values["z_inner_max_mm"]
            <= values["z_max_mm"]
        ):
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/parameters/shape")

    @classmethod
    def from_value(
        cls,
        operation: PartDesignPrimitiveOperation,
        value: object,
    ) -> PrimitiveParameterSet:
        if type(operation) is not PartDesignPrimitiveOperation:
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/operation/id")
        root = _exact_fields(value, {"shape", "placement"}, "/parameters")
        shape = root["shape"]
        placement = _exact_fields(
            root["placement"],
            {"translation_mm", "rotation_axis", "rotation_degrees"},
            "/parameters/placement",
        )
        spec = _NATIVE_PRIMITIVE_SPECS[operation]
        expected_keys = {item.semantic_key for item in spec.parameters}
        shape = _exact_fields(shape, expected_keys, "/parameters/shape")
        translation = placement["translation_mm"]
        rotation_axis = placement["rotation_axis"]
        if type(translation) is not list or type(rotation_axis) is not list:
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/parameters/placement")
        return cls(
            operation=operation,
            shape_values=tuple(shape[item.semantic_key] for item in spec.parameters),
            translation_mm=tuple(translation),
            rotation_axis=tuple(rotation_axis),
            rotation_degrees=placement["rotation_degrees"],
        )

    def to_value(self) -> dict[str, object]:
        spec = _NATIVE_PRIMITIVE_SPECS[self.operation]
        return {
            "shape": {
                item.semantic_key: value
                for item, value in zip(spec.parameters, self.shape_values, strict=True)
            },
            "placement": {
                "translation_mm": list(self.translation_mm),
                "rotation_axis": list(self.rotation_axis),
                "rotation_degrees": self.rotation_degrees,
            },
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignPrimitiveBackendPlan:
    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    body_id: str
    node_id: str
    result_id: str
    operation: PartDesignPrimitiveOperation
    base: SemanticObjectSelection | None
    parameter_id: str
    value_id: str
    parameters: PrimitiveParameterSet
    schema_version: int = PARTDESIGN_PRIMITIVE_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/schema_version")
        if type(self.operation) is not PartDesignPrimitiveOperation:
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/operation/id")
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
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if self.base is not None and type(self.base) is not SemanticObjectSelection:
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/selection/base")
        spec = _NATIVE_PRIMITIVE_SPECS[self.operation]
        if not spec.additive and self.base is None:
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/selection/base")
        if self.base is not None and (
            self.base.node_id == self.node_id or self.base.result_id == self.result_id
        ):
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/selection")
        if (
            type(self.parameters) is not PrimitiveParameterSet
            or self.parameters.operation is not self.operation
        ):
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/parameters")

    @property
    def additive(self) -> bool:
        return _NATIVE_PRIMITIVE_SPECS[self.operation].additive

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
                "engine_build_id": PARTDESIGN_PRIMITIVE_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": PARTDESIGN_PRIMITIVE_RULE_ID,
                "rule_contract_sha256": PARTDESIGN_PRIMITIVE_RULE_CONTRACT_SHA256,
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
            },
            "selection": {
                "body_id": self.body_id,
                "node_id": self.node_id,
                "result_id": self.result_id,
                "base": None if self.base is None else self.base.to_mapping(),
                "parameter_id": self.parameter_id,
                "value_id": self.value_id,
            },
            "operation": {
                "id": self.operation.value,
                "common": {"map_mode": "deactivated", "refine": True},
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
    def from_mapping(cls, value: object) -> PartDesignPrimitiveBackendPlan:
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
            root["binding"], {"lowering_request_sha256", "adapter_contract_sha256"}, "/binding"
        )
        selection = _exact_fields(
            root["selection"],
            {"body_id", "node_id", "result_id", "base", "parameter_id", "value_id"},
            "/selection",
        )
        operation = _exact_fields(root["operation"], {"id", "common", "parameters"}, "/operation")
        try:
            operation_id = PartDesignPrimitiveOperation(operation["id"])
        except (TypeError, ValueError):
            _fail(PartDesignPrimitiveRuleErrorCode.INTEGRITY_FAILURE, "/operation/id")
        if (
            root["authority"] != "none"
            or backend
            != {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PARTDESIGN_PRIMITIVE_FREECAD_ENGINE_BUILD_ID,
            }
            or rule
            != {
                "rule_id": PARTDESIGN_PRIMITIVE_RULE_ID,
                "rule_contract_sha256": PARTDESIGN_PRIMITIVE_RULE_CONTRACT_SHA256,
            }
            or operation["common"] != {"map_mode": "deactivated", "refine": True}
        ):
            _fail(PartDesignPrimitiveRuleErrorCode.INTEGRITY_FAILURE, "/contract")
        base = (
            None
            if selection["base"] is None
            else SemanticObjectSelection.from_mapping(selection["base"], "/selection/base")
        )
        return cls(
            schema_version=root["schema_version"],
            source_artifact_id=source["artifact_id"],
            source_graph_id=source["graph_id"],
            source_graph_sha256=source["graph_sha256"],
            source_content_sha256=source["content_sha256"],
            lowering_request_sha256=binding["lowering_request_sha256"],
            adapter_contract_sha256=binding["adapter_contract_sha256"],
            body_id=selection["body_id"],
            node_id=selection["node_id"],
            result_id=selection["result_id"],
            operation=operation_id,
            base=base,
            parameter_id=selection["parameter_id"],
            value_id=selection["value_id"],
            parameters=PrimitiveParameterSet.from_value(operation_id, operation["parameters"]),
        )


def decode_partdesign_primitive_backend_plan(
    raw: object,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> PartDesignPrimitiveBackendPlan:
    if expected_content_sha256 is not None:
        expected_content_sha256 = _digest(expected_content_sha256, "/expected_content_sha256")
    if expected_plan_sha256 is not None:
        expected_plan_sha256 = _digest(expected_plan_sha256, "/expected_plan_sha256")
    result = PartDesignPrimitiveBackendPlan.from_mapping(_decode_mapping(raw))
    if type(raw) is not bytes or not hmac.compare_digest(raw, result.canonical_bytes):
        _fail(PartDesignPrimitiveRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_content_sha256
    ):
        _fail(PartDesignPrimitiveRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        result.plan_sha256, expected_plan_sha256
    ):
        _fail(PartDesignPrimitiveRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticatedPrimitiveObject:
    object: object
    node_id: str
    result_id: str

    def __post_init__(self) -> None:
        if self.object is None:
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/bindings/object")
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/bindings/node_id"))
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/bindings/result_id"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignPrimitiveExecutionBindings:
    document: object
    body: object
    body_id: str
    base: AuthenticatedPrimitiveObject | None

    def __post_init__(self) -> None:
        if self.document is None or self.body is None:
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/bindings")
        object.__setattr__(self, "body_id", _identifier(self.body_id, "/bindings/body_id"))
        if self.base is not None and type(self.base) is not AuthenticatedPrimitiveObject:
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/bindings/base")


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignPrimitiveConformanceReceipt:
    plan_sha256: str
    operation: PartDesignPrimitiveOperation
    object_name: str
    before_volume_mm3: float
    after_volume_mm3: float
    receipt_id: str = field(init=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/plan_sha256"))
        if type(self.operation) is not PartDesignPrimitiveOperation:
            _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/operation")
        object.__setattr__(self, "object_name", _identifier(self.object_name, "/object_name"))
        before = _finite(self.before_volume_mm3, "/receipt/before_volume_mm3")
        after = _finite(self.after_volume_mm3, "/receipt/after_volume_mm3")
        spec = _NATIVE_PRIMITIVE_SPECS[self.operation]
        if (
            before < 0.0
            or after <= 0.0
            or (spec.additive and before > 0.0 and not after > before)
            or (not spec.additive and not 0.0 < after < before)
        ):
            _fail(PartDesignPrimitiveRuleErrorCode.CONFORMANCE_FAILED, "/receipt")
        body = {
            "authority": "none",
            "plan_sha256": self.plan_sha256,
            "operation": self.operation.value,
            "object_name": self.object_name,
            "before_volume_mm3": before,
            "after_volume_mm3": after,
        }
        digest = hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()
        object.__setattr__(self, "receipt_sha256", digest)
        object.__setattr__(self, "receipt_id", f"partdesign_primitive_{digest[:32]}")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _shape_volume(shape: object, path: str) -> float:
    try:
        if shape is None or shape.isNull() or not shape.isValid() or len(shape.Solids) != 1:
            _fail(PartDesignPrimitiveRuleErrorCode.PRECONDITION_FAILED, path)
        volume = float(shape.Volume)
    except PartDesignPrimitiveRuleError:
        raise
    except Exception:
        _fail(PartDesignPrimitiveRuleErrorCode.PRECONDITION_FAILED, path)
    if not math.isfinite(volume) or volume <= 0.0:
        _fail(PartDesignPrimitiveRuleErrorCode.PRECONDITION_FAILED, path)
    return volume


def _same_selection(
    semantic: SemanticObjectSelection | None, authenticated: AuthenticatedPrimitiveObject | None
) -> bool:
    return (
        semantic is None
        and authenticated is None
        or semantic is not None
        and authenticated is not None
        and semantic.node_id == authenticated.node_id
        and semantic.result_id == authenticated.result_id
    )


def _validate_bindings(
    plan: PartDesignPrimitiveBackendPlan,
    bindings: PartDesignPrimitiveExecutionBindings,
) -> tuple[float, tuple[object, ...], object | None]:
    if bindings.body_id != plan.body_id or not _same_selection(plan.base, bindings.base):
        _fail(PartDesignPrimitiveRuleErrorCode.INTEGRITY_FAILURE, "/bindings/identity")
    document, body = bindings.document, bindings.body
    base = None if bindings.base is None else bindings.base.object
    expected_group = () if base is None else (base,)
    try:
        if (
            getattr(document, "UndoMode", 0) != 1
            or bool(document.HasPendingTransaction)
            or body.Document is not document
            or body.TypeId != "PartDesign::Body"
            or tuple(body.Group) != expected_group
            or body.Tip is not base
            or (base is not None and base.Document is not document)
        ):
            _fail(PartDesignPrimitiveRuleErrorCode.PRECONDITION_FAILED, "/bindings")
    except PartDesignPrimitiveRuleError:
        raise
    except Exception:
        _fail(PartDesignPrimitiveRuleErrorCode.PRECONDITION_FAILED, "/bindings")
    return (
        0.0 if base is None else _shape_volume(base.Shape, "/bindings/base"),
        expected_group,
        base,
    )


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
        _fail(PartDesignPrimitiveRuleErrorCode.CONFORMANCE_FAILED, "/result/placement")


def apply_partdesign_primitive_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: PartDesignPrimitiveExecutionBindings,
) -> PartDesignPrimitiveConformanceReceipt:
    """Explicit trusted-host action; exact plan validation precedes mutation."""

    if type(bindings) is not PartDesignPrimitiveExecutionBindings:
        _fail(PartDesignPrimitiveRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(PartDesignPrimitiveRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != PARTDESIGN_PRIMITIVE_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(PartDesignPrimitiveRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_partdesign_primitive_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    before_volume, before_group, base = _validate_bindings(plan, bindings)
    document, body = bindings.document, bindings.body
    spec = _NATIVE_PRIMITIVE_SPECS[plan.operation]
    object_name = f"{spec.object_prefix}_{plan.plan_sha256[:16]}"
    try:
        if document.getObject(object_name) is not None:
            _fail(PartDesignPrimitiveRuleErrorCode.PRECONDITION_FAILED, "/document/object_name")
        before_objects = tuple(document.Objects)
        before_tip = body.Tip
        before_visibilities = tuple(bool(item.Visibility) for item in before_group)
    except PartDesignPrimitiveRuleError:
        raise
    except Exception:
        _fail(PartDesignPrimitiveRuleErrorCode.PRECONDITION_FAILED, "/document")

    transaction_open = False
    try:
        document.openTransaction("VibeCAD trusted PartDesign primitive")
        transaction_open = True
        feature = body.newObject(spec.type_id, object_name)
        feature.MapMode = "Deactivated"
        expected_placement = FreeCAD.Placement(
            FreeCAD.Vector(*plan.parameters.translation_mm),
            FreeCAD.Rotation(
                FreeCAD.Vector(*plan.parameters.rotation_axis), plan.parameters.rotation_degrees
            ),
        )
        feature.Placement = expected_placement
        feature.Refine = True
        for parameter, value in zip(spec.parameters, plan.parameters.shape_values, strict=True):
            setattr(feature, parameter.property_name, value)
        for property_name, value in spec.fixed_properties:
            setattr(feature, property_name, value)
        document.recompute()
        after_volume = _shape_volume(feature.Shape, "/result/shape")
        if (
            feature.TypeId != spec.type_id
            or not feature.isValid()
            or tuple(feature.State) != ("Up-to-date",)
            or body.Tip is not feature
            or feature.BaseFeature is not base
            or str(feature.MapMode) != "Deactivated"
            or not bool(feature.Refine)
        ):
            _fail(PartDesignPrimitiveRuleErrorCode.CONFORMANCE_FAILED, "/result")
        for index, (actual, expected) in enumerate(
            zip(_matrix_values(feature.Placement), _matrix_values(expected_placement), strict=True)
        ):
            if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9):
                _fail(
                    PartDesignPrimitiveRuleErrorCode.CONFORMANCE_FAILED,
                    f"/result/placement/{index}",
                )
        for index, (parameter, expected) in enumerate(
            zip(spec.parameters, plan.parameters.shape_values, strict=True)
        ):
            actual = getattr(feature, parameter.property_name)
            if parameter.kind == "integer":
                matches = type(int(actual)) is int and int(actual) == expected
            else:
                matches = math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1e-9)
            if not matches:
                _fail(
                    PartDesignPrimitiveRuleErrorCode.CONFORMANCE_FAILED,
                    f"/result/parameters/{index}",
                )
        for index, (property_name, expected) in enumerate(spec.fixed_properties):
            if not math.isclose(
                float(getattr(feature, property_name)), expected, rel_tol=0.0, abs_tol=1e-9
            ):
                _fail(PartDesignPrimitiveRuleErrorCode.CONFORMANCE_FAILED, f"/result/fixed/{index}")
        epsilon = max(1e-9, before_volume * 1e-12)
        if (
            spec.additive
            and before_volume > 0.0
            and not after_volume > before_volume + epsilon
            or not spec.additive
            and not after_volume < before_volume - epsilon
        ):
            _fail(PartDesignPrimitiveRuleErrorCode.CONFORMANCE_FAILED, "/result/volume")
        document.commitTransaction()
        transaction_open = False
    except BaseException as error:
        if transaction_open:
            try:
                document.abortTransaction()
                document.recompute()
            except BaseException:
                _fail(PartDesignPrimitiveRuleErrorCode.TRANSACTION_FAILED, "/transaction/rollback")
        try:
            after_objects = tuple(document.Objects)
            after_group = tuple(body.Group)
            if (
                len(after_objects) != len(before_objects)
                or any(
                    current is not original
                    for current, original in zip(after_objects, before_objects, strict=True)
                )
                or len(after_group) != len(before_group)
                or any(
                    current is not original
                    for current, original in zip(after_group, before_group, strict=True)
                )
                or body.Tip is not before_tip
                or tuple(bool(item.Visibility) for item in before_group) != before_visibilities
            ):
                _fail(PartDesignPrimitiveRuleErrorCode.TRANSACTION_FAILED, "/transaction/rollback")
        except PartDesignPrimitiveRuleError:
            raise
        except Exception:
            _fail(PartDesignPrimitiveRuleErrorCode.TRANSACTION_FAILED, "/transaction/rollback")
        if isinstance(error, KeyboardInterrupt):
            raise
        if isinstance(error, PartDesignPrimitiveRuleError):
            raise error
        _fail(PartDesignPrimitiveRuleErrorCode.TRANSACTION_FAILED, "/transaction/apply")

    return PartDesignPrimitiveConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        object_name=object_name,
        before_volume_mm3=before_volume,
        after_volume_mm3=after_volume,
    )


__all__ = [
    "MAX_PARTDESIGN_PRIMITIVE_PLAN_BYTES",
    "PARTDESIGN_PRIMITIVE_FREECAD_ENGINE_BUILD_ID",
    "PARTDESIGN_PRIMITIVE_PLAN_MEDIA_TYPE",
    "PARTDESIGN_PRIMITIVE_PLAN_SCHEMA_VERSION",
    "PARTDESIGN_PRIMITIVE_RULE_CONTRACT_SHA256",
    "PARTDESIGN_PRIMITIVE_RULE_ID",
    "AuthenticatedPrimitiveObject",
    "PartDesignPrimitiveBackendPlan",
    "PartDesignPrimitiveConformanceReceipt",
    "PartDesignPrimitiveExecutionBindings",
    "PartDesignPrimitiveOperation",
    "PartDesignPrimitiveRuleError",
    "PartDesignPrimitiveRuleErrorCode",
    "PrimitiveParameterSet",
    "SemanticObjectSelection",
    "apply_partdesign_primitive_plan",
    "decode_partdesign_primitive_backend_plan",
]
