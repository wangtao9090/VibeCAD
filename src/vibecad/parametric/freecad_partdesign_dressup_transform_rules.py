"""Trusted FreeCAD rules for one reviewed dress-up/transform batch.

The canonical plan contains backend-neutral operations, typed parameters and
semantic axis-aligned edge/face roles.  Native ``TypeId`` and property names
exist only in the static table below.  Durable plans never contain ``EdgeN``
or ``FaceN``; the trusted runtime resolves a unique live sub-element and fails
closed before opening a transaction when resolution is absent or ambiguous.

Plans and receipts are content-addressed evidence and grant no authority.
Mutation happens only through the explicit trusted-host apply function.
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

PARTDESIGN_DRESSUP_TRANSFORM_PLAN_SCHEMA_VERSION: Final = 1
PARTDESIGN_DRESSUP_TRANSFORM_PLAN_MEDIA_TYPE: Final = (
    "application/vnd.vibecad.freecad-partdesign-dressup-transform-plan+json"
)
MAX_PARTDESIGN_DRESSUP_TRANSFORM_PLAN_BYTES: Final = 32 * 1024
PARTDESIGN_DRESSUP_TRANSFORM_FREECAD_ENGINE_BUILD_ID: Final = (
    "34a9716668b1ddeb55b914f1c5be644826bdbbbf"
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-dressup-transform-plan.v1\0"
_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-partdesign-dressup-transform-rule.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-dressup-transform-receipt.v1\0"


class PartDesignDressupTransformOperation(StrEnum):
    SCALED = "scaled"
    MULTI_TRANSFORM = "multi_transform"
    FILLET = "fillet"
    CHAMFER = "chamfer"
    DRAFT = "draft"
    THICKNESS = "thickness"


class Axis(StrEnum):
    X = "x"
    Y = "y"
    Z = "z"


class Side(StrEnum):
    MINIMUM = "minimum"
    MAXIMUM = "maximum"


class OriginPlane(StrEnum):
    XY = "xy"
    XZ = "xz"
    YZ = "yz"


class OriginAxis(StrEnum):
    X = "x"
    Y = "y"
    Z = "z"


class MultiTransformStepKind(StrEnum):
    SCALED = "scaled"
    MIRRORED = "mirrored"


@dataclass(frozen=True, slots=True)
class _NativeSpec:
    type_id: str
    object_prefix: str
    properties: tuple[str, ...]


# The sole semantic-operation -> native-code table.  Graph/plan strings can
# select only a validated enum above, never a native TypeId or property name.
_NATIVE_SPECS: Final = {
    PartDesignDressupTransformOperation.SCALED: _NativeSpec(
        "PartDesign::Scaled",
        "Scaled",
        ("Originals", "Factor", "Occurrences", "TransformMode", "Refine"),
    ),
    PartDesignDressupTransformOperation.MULTI_TRANSFORM: _NativeSpec(
        "PartDesign::MultiTransform",
        "MultiTransform",
        ("Originals", "Transformations", "TransformMode", "Refine", "Shape"),
    ),
    PartDesignDressupTransformOperation.FILLET: _NativeSpec(
        "PartDesign::Fillet",
        "Fillet",
        ("Base", "Radius", "UseAllEdges", "SupportTransform", "Refine"),
    ),
    PartDesignDressupTransformOperation.CHAMFER: _NativeSpec(
        "PartDesign::Chamfer",
        "Chamfer",
        (
            "Base",
            "ChamferType",
            "Size",
            "UseAllEdges",
            "SupportTransform",
            "Refine",
        ),
    ),
    PartDesignDressupTransformOperation.DRAFT: _NativeSpec(
        "PartDesign::Draft",
        "Draft",
        (
            "Base",
            "NeutralPlane",
            "PullDirection",
            "Angle",
            "Reversed",
            "SupportTransform",
            "Refine",
        ),
    ),
    PartDesignDressupTransformOperation.THICKNESS: _NativeSpec(
        "PartDesign::Thickness",
        "Thickness",
        (
            "Base",
            "Value",
            "Mode",
            "Join",
            "Reversed",
            "Intersection",
            "SupportTransform",
            "Refine",
        ),
    ),
}

_NATIVE_STEP_SPECS: Final = {
    MultiTransformStepKind.SCALED: _NativeSpec(
        "PartDesign::Scaled",
        "ScaledStep",
        ("Factor", "Occurrences", "TransformMode", "Refine"),
    ),
    MultiTransformStepKind.MIRRORED: _NativeSpec(
        "PartDesign::Mirrored",
        "MirroredStep",
        ("MirrorPlane", "TransformMode", "Refine"),
    ),
}

PARTDESIGN_DRESSUP_TRANSFORM_RULE_ID: Final = (
    "freecad.partdesign.dressup-transform.axis-aligned.v1"
)
_NATIVE_CONTRACT = {
    "engine": {
        "name": "FreeCAD",
        "version": "1.1.0",
        "build_id": PARTDESIGN_DRESSUP_TRANSFORM_FREECAD_ENGINE_BUILD_ID,
    },
    "common": {
        "single_solid": True,
        "transform_mode": "Features",
        "refine": True,
        "transaction": "rollback",
        "subelement_resolution": "unique-live-axis-aligned-role",
        "durable_native_subelement_names": False,
    },
    "operations": [
        {
            "operation": operation.value,
            "type_id": spec.type_id,
            "properties": list(spec.properties),
        }
        for operation, spec in _NATIVE_SPECS.items()
    ],
    "multi_transform_steps": [
        {
            "kind": kind.value,
            "type_id": spec.type_id,
            "properties": list(spec.properties),
        }
        for kind, spec in _NATIVE_STEP_SPECS.items()
    ],
}
PARTDESIGN_DRESSUP_TRANSFORM_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN
    + json.dumps(
        _NATIVE_CONTRACT,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
).hexdigest()


class PartDesignDressupTransformRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    RESOLUTION_FAILED = "resolution_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class PartDesignDressupTransformRuleError(ValueError):
    """Bounded, non-reflective failure at the trusted native boundary."""

    def __init__(
        self, code: PartDesignDressupTransformRuleErrorCode, path: str = "/"
    ) -> None:
        if type(code) is not PartDesignDressupTransformRuleErrorCode:
            raise TypeError("code must be a PartDesignDressupTransformRuleErrorCode")
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
        super().__init__(f"PartDesign dress-up/transform rule error ({code.value}) at {path}")


def _fail(code: PartDesignDressupTransformRuleErrorCode, path: str = "/") -> None:
    raise PartDesignDressupTransformRuleError(code, path)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/")


def _exact_fields(value: object, expected: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        _fail(PartDesignDressupTransformRuleErrorCode.INTEGRITY_FAILURE, path)
    if set(value) != expected:
        _fail(PartDesignDressupTransformRuleErrorCode.INTEGRITY_FAILURE, path)
    return value


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, path)
    return value


def _finite(value: object, path: str) -> float:
    if type(value) not in (int, float):
        _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, path)
    try:
        converted = float(value)
    except (ValueError, TypeError, OverflowError):
        _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, path)
    if not math.isfinite(converted):
        _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, path)
    return converted


def _integer(value: object, path: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, path)
    return value


def _enum(enum_type, value: object, path: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, path)


def _decode_mapping(raw: object) -> dict[str, object]:
    if type(raw) is not bytes or len(raw) > MAX_PARTDESIGN_DRESSUP_TRANSFORM_PLAN_BYTES:
        _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/")
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, MemoryError):
        _fail(PartDesignDressupTransformRuleErrorCode.INTEGRITY_FAILURE, "/")
    if type(value) is not dict:
        _fail(PartDesignDressupTransformRuleErrorCode.INTEGRITY_FAILURE, "/")
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
        fields = _exact_fields(value, {"node_id", "result_id"}, path)
        return cls(node_id=fields["node_id"], result_id=fields["result_id"])


@dataclass(frozen=True, slots=True, kw_only=True)
class AxisAlignedEdgeRole:
    axis: Axis
    first_side: Side
    second_side: Side

    def __post_init__(self) -> None:
        if type(self.axis) is not Axis or type(self.first_side) is not Side or type(
            self.second_side
        ) is not Side:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/edge_role")

    def to_mapping(self) -> dict[str, str]:
        return {
            "axis": self.axis.value,
            "first_side": self.first_side.value,
            "second_side": self.second_side.value,
        }

    @classmethod
    def from_mapping(cls, value: object) -> AxisAlignedEdgeRole:
        fields = _exact_fields(
            value, {"axis", "first_side", "second_side"}, "/parameters/edge_role"
        )
        return cls(
            axis=_enum(Axis, fields["axis"], "/parameters/edge_role/axis"),
            first_side=_enum(Side, fields["first_side"], "/parameters/edge_role/first_side"),
            second_side=_enum(
                Side, fields["second_side"], "/parameters/edge_role/second_side"
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AxisAlignedFaceRole:
    axis: Axis
    side: Side

    def __post_init__(self) -> None:
        if type(self.axis) is not Axis or type(self.side) is not Side:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/face_role")

    def to_mapping(self) -> dict[str, str]:
        return {"axis": self.axis.value, "side": self.side.value}

    @classmethod
    def from_mapping(cls, value: object) -> AxisAlignedFaceRole:
        fields = _exact_fields(value, {"axis", "side"}, "/parameters/face_role")
        return cls(
            axis=_enum(Axis, fields["axis"], "/parameters/face_role/axis"),
            side=_enum(Side, fields["side"], "/parameters/face_role/side"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ScaledParameters:
    factor: float
    occurrences: int

    def __post_init__(self) -> None:
        factor = _finite(self.factor, "/parameters/factor")
        if not 1.0 <= factor <= 10.0:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/parameters/factor")
        object.__setattr__(self, "factor", factor)
        object.__setattr__(
            self,
            "occurrences",
            _integer(self.occurrences, "/parameters/occurrences", 2, 16),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"factor": self.factor, "occurrences": self.occurrences}

    @classmethod
    def from_mapping(cls, value: object, path: str = "/parameters") -> ScaledParameters:
        fields = _exact_fields(value, {"factor", "occurrences"}, path)
        return cls(factor=fields["factor"], occurrences=fields["occurrences"])


@dataclass(frozen=True, slots=True, kw_only=True)
class MultiTransformStep:
    step_id: str
    kind: MultiTransformStepKind
    factor: float | None = None
    occurrences: int | None = None
    mirror_plane: OriginPlane | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _identifier(self.step_id, "/parameters/steps/id"))
        if type(self.kind) is not MultiTransformStepKind:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/parameters/steps/kind")
        if self.kind is MultiTransformStepKind.SCALED:
            if self.mirror_plane is not None:
                _fail(
                    PartDesignDressupTransformRuleErrorCode.INVALID_INPUT,
                    "/parameters/steps/mirror_plane",
                )
            scaled = ScaledParameters(factor=self.factor, occurrences=self.occurrences)
            object.__setattr__(self, "factor", scaled.factor)
            object.__setattr__(self, "occurrences", scaled.occurrences)
        elif (
            type(self.mirror_plane) is not OriginPlane
            or self.factor is not None
            or self.occurrences is not None
        ):
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/parameters/steps")

    def to_mapping(self) -> dict[str, object]:
        if self.kind is MultiTransformStepKind.SCALED:
            parameters: dict[str, object] = {
                "factor": self.factor,
                "occurrences": self.occurrences,
            }
        else:
            parameters = {"mirror_plane": self.mirror_plane.value}
        return {"step_id": self.step_id, "kind": self.kind.value, "parameters": parameters}

    @classmethod
    def from_mapping(cls, value: object, index: int) -> MultiTransformStep:
        path = f"/parameters/steps/{index}"
        fields = _exact_fields(value, {"step_id", "kind", "parameters"}, path)
        kind = _enum(MultiTransformStepKind, fields["kind"], f"{path}/kind")
        if kind is MultiTransformStepKind.SCALED:
            parameters = _exact_fields(
                fields["parameters"], {"factor", "occurrences"}, f"{path}/parameters"
            )
            return cls(
                step_id=fields["step_id"],
                kind=kind,
                factor=parameters["factor"],
                occurrences=parameters["occurrences"],
            )
        parameters = _exact_fields(
            fields["parameters"], {"mirror_plane"}, f"{path}/parameters"
        )
        return cls(
            step_id=fields["step_id"],
            kind=kind,
            mirror_plane=_enum(
                OriginPlane, parameters["mirror_plane"], f"{path}/parameters/mirror_plane"
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MultiTransformParameters:
    steps: tuple[MultiTransformStep, ...]

    def __post_init__(self) -> None:
        if (
            type(self.steps) is not tuple
            or not 2 <= len(self.steps) <= 8
            or any(type(step) is not MultiTransformStep for step in self.steps)
            or len({step.step_id for step in self.steps}) != len(self.steps)
        ):
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/parameters/steps")

    def to_mapping(self) -> dict[str, object]:
        return {"steps": [step.to_mapping() for step in self.steps]}

    @classmethod
    def from_mapping(cls, value: object) -> MultiTransformParameters:
        fields = _exact_fields(value, {"steps"}, "/parameters")
        values = fields["steps"]
        if type(values) is not list or len(values) > 8:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/parameters/steps")
        return cls(
            steps=tuple(
                MultiTransformStep.from_mapping(item, i) for i, item in enumerate(values)
            )
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FilletParameters:
    edge_role: AxisAlignedEdgeRole
    radius_mm: float

    def __post_init__(self) -> None:
        if type(self.edge_role) is not AxisAlignedEdgeRole:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/parameters/edge_role")
        value = _finite(self.radius_mm, "/parameters/radius_mm")
        if not 0.01 <= value <= 1_000_000.0:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/parameters/radius_mm")
        object.__setattr__(self, "radius_mm", value)

    def to_mapping(self) -> dict[str, object]:
        return {"edge_role": self.edge_role.to_mapping(), "radius_mm": self.radius_mm}

    @classmethod
    def from_mapping(cls, value: object) -> FilletParameters:
        fields = _exact_fields(value, {"edge_role", "radius_mm"}, "/parameters")
        return cls(
            edge_role=AxisAlignedEdgeRole.from_mapping(fields["edge_role"]),
            radius_mm=fields["radius_mm"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChamferParameters:
    edge_role: AxisAlignedEdgeRole
    size_mm: float

    def __post_init__(self) -> None:
        if type(self.edge_role) is not AxisAlignedEdgeRole:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/parameters/edge_role")
        value = _finite(self.size_mm, "/parameters/size_mm")
        if not 0.01 <= value <= 1_000_000.0:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/parameters/size_mm")
        object.__setattr__(self, "size_mm", value)

    def to_mapping(self) -> dict[str, object]:
        return {"edge_role": self.edge_role.to_mapping(), "size_mm": self.size_mm}

    @classmethod
    def from_mapping(cls, value: object) -> ChamferParameters:
        fields = _exact_fields(value, {"edge_role", "size_mm"}, "/parameters")
        return cls(
            edge_role=AxisAlignedEdgeRole.from_mapping(fields["edge_role"]),
            size_mm=fields["size_mm"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DraftParameters:
    face_role: AxisAlignedFaceRole
    neutral_plane: OriginPlane
    pull_direction: OriginAxis
    angle_degrees: float
    reversed: bool

    def __post_init__(self) -> None:
        if (
            type(self.face_role) is not AxisAlignedFaceRole
            or type(self.neutral_plane) is not OriginPlane
            or type(self.pull_direction) is not OriginAxis
            or type(self.reversed) is not bool
        ):
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/parameters")
        value = _finite(self.angle_degrees, "/parameters/angle_degrees")
        if not 0.0 <= value <= 89.0:
            _fail(
                PartDesignDressupTransformRuleErrorCode.INVALID_INPUT,
                "/parameters/angle_degrees",
            )
        object.__setattr__(self, "angle_degrees", value)

    def to_mapping(self) -> dict[str, object]:
        return {
            "face_role": self.face_role.to_mapping(),
            "neutral_plane": self.neutral_plane.value,
            "pull_direction": self.pull_direction.value,
            "angle_degrees": self.angle_degrees,
            "reversed": self.reversed,
        }

    @classmethod
    def from_mapping(cls, value: object) -> DraftParameters:
        fields = _exact_fields(
            value,
            {"face_role", "neutral_plane", "pull_direction", "angle_degrees", "reversed"},
            "/parameters",
        )
        return cls(
            face_role=AxisAlignedFaceRole.from_mapping(fields["face_role"]),
            neutral_plane=_enum(
                OriginPlane, fields["neutral_plane"], "/parameters/neutral_plane"
            ),
            pull_direction=_enum(
                OriginAxis, fields["pull_direction"], "/parameters/pull_direction"
            ),
            angle_degrees=fields["angle_degrees"],
            reversed=fields["reversed"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ThicknessParameters:
    face_role: AxisAlignedFaceRole
    value_mm: float

    def __post_init__(self) -> None:
        if type(self.face_role) is not AxisAlignedFaceRole:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/parameters/face_role")
        value = _finite(self.value_mm, "/parameters/value_mm")
        if not 0.01 <= value <= 1_000_000.0:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/parameters/value_mm")
        object.__setattr__(self, "value_mm", value)

    def to_mapping(self) -> dict[str, object]:
        return {"face_role": self.face_role.to_mapping(), "value_mm": self.value_mm}

    @classmethod
    def from_mapping(cls, value: object) -> ThicknessParameters:
        fields = _exact_fields(value, {"face_role", "value_mm"}, "/parameters")
        return cls(
            face_role=AxisAlignedFaceRole.from_mapping(fields["face_role"]),
            value_mm=fields["value_mm"],
        )


OperationParameters = (
    ScaledParameters
    | MultiTransformParameters
    | FilletParameters
    | ChamferParameters
    | DraftParameters
    | ThicknessParameters
)


def operation_parameters_from_value(
    operation: PartDesignDressupTransformOperation, value: object
) -> OperationParameters:
    if operation is PartDesignDressupTransformOperation.SCALED:
        return ScaledParameters.from_mapping(value)
    if operation is PartDesignDressupTransformOperation.MULTI_TRANSFORM:
        return MultiTransformParameters.from_mapping(value)
    if operation is PartDesignDressupTransformOperation.FILLET:
        return FilletParameters.from_mapping(value)
    if operation is PartDesignDressupTransformOperation.CHAMFER:
        return ChamferParameters.from_mapping(value)
    if operation is PartDesignDressupTransformOperation.DRAFT:
        return DraftParameters.from_mapping(value)
    if operation is PartDesignDressupTransformOperation.THICKNESS:
        return ThicknessParameters.from_mapping(value)
    _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/operation/id")


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignDressupTransformBackendPlan:
    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    body_id: str
    node_id: str
    result_id: str
    operation: PartDesignDressupTransformOperation
    base: SemanticObjectSelection
    parameter_id: str
    value_id: str
    parameters: OperationParameters
    schema_version: int = PARTDESIGN_DRESSUP_TRANSFORM_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/schema_version")
        if type(self.operation) is not PartDesignDressupTransformOperation:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/operation/id")
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
        if type(self.base) is not SemanticObjectSelection:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/selection/base")
        if self.base.node_id == self.node_id or self.base.result_id == self.result_id:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/selection")
        expected_type = {
            PartDesignDressupTransformOperation.SCALED: ScaledParameters,
            PartDesignDressupTransformOperation.MULTI_TRANSFORM: MultiTransformParameters,
            PartDesignDressupTransformOperation.FILLET: FilletParameters,
            PartDesignDressupTransformOperation.CHAMFER: ChamferParameters,
            PartDesignDressupTransformOperation.DRAFT: DraftParameters,
            PartDesignDressupTransformOperation.THICKNESS: ThicknessParameters,
        }[self.operation]
        if type(self.parameters) is not expected_type:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/parameters")

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
                "engine_build_id": PARTDESIGN_DRESSUP_TRANSFORM_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": PARTDESIGN_DRESSUP_TRANSFORM_RULE_ID,
                "rule_contract_sha256": PARTDESIGN_DRESSUP_TRANSFORM_RULE_CONTRACT_SHA256,
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
                "base": self.base.to_mapping(),
                "parameter_id": self.parameter_id,
                "value_id": self.value_id,
            },
            "operation": {"id": self.operation.value, "parameters": self.parameters.to_mapping()},
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_PLAN_DIGEST_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> PartDesignDressupTransformBackendPlan:
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
        operation = _exact_fields(root["operation"], {"id", "parameters"}, "/operation")
        operation_id = _enum(
            PartDesignDressupTransformOperation, operation["id"], "/operation/id"
        )
        if (
            root["authority"] != "none"
            or backend
            != {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PARTDESIGN_DRESSUP_TRANSFORM_FREECAD_ENGINE_BUILD_ID,
            }
            or rule
            != {
                "rule_id": PARTDESIGN_DRESSUP_TRANSFORM_RULE_ID,
                "rule_contract_sha256": PARTDESIGN_DRESSUP_TRANSFORM_RULE_CONTRACT_SHA256,
            }
        ):
            _fail(PartDesignDressupTransformRuleErrorCode.INTEGRITY_FAILURE, "/contract")
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
            base=SemanticObjectSelection.from_mapping(selection["base"], "/selection/base"),
            parameter_id=selection["parameter_id"],
            value_id=selection["value_id"],
            parameters=operation_parameters_from_value(operation_id, operation["parameters"]),
        )


def decode_partdesign_dressup_transform_backend_plan(
    raw: object,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> PartDesignDressupTransformBackendPlan:
    if expected_content_sha256 is not None:
        expected_content_sha256 = _digest(expected_content_sha256, "/expected_content_sha256")
    if expected_plan_sha256 is not None:
        expected_plan_sha256 = _digest(expected_plan_sha256, "/expected_plan_sha256")
    result = PartDesignDressupTransformBackendPlan.from_mapping(_decode_mapping(raw))
    if type(raw) is not bytes or not hmac.compare_digest(raw, result.canonical_bytes):
        _fail(PartDesignDressupTransformRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_content_sha256
    ):
        _fail(PartDesignDressupTransformRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        result.plan_sha256, expected_plan_sha256
    ):
        _fail(PartDesignDressupTransformRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticatedDressupTransformObject:
    object: object
    node_id: str
    result_id: str

    def __post_init__(self) -> None:
        if self.object is None:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/bindings/object")
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/bindings/node_id"))
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/bindings/result_id"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignDressupTransformExecutionBindings:
    document: object
    body: object
    body_id: str
    base: AuthenticatedDressupTransformObject

    def __post_init__(self) -> None:
        if self.document is None or self.body is None:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/bindings")
        object.__setattr__(self, "body_id", _identifier(self.body_id, "/bindings/body_id"))
        if type(self.base) is not AuthenticatedDressupTransformObject:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/bindings/base")


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignDressupTransformConformanceReceipt:
    plan_sha256: str
    operation: PartDesignDressupTransformOperation
    object_names: tuple[str, ...]
    before_volume_mm3: float
    after_volume_mm3: float
    receipt_id: str = field(init=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/plan_sha256"))
        if type(self.operation) is not PartDesignDressupTransformOperation:
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/operation")
        if (
            type(self.object_names) is not tuple
            or not 1 <= len(self.object_names) <= 9
            or any(
                type(item) is not str or _IDENTIFIER.fullmatch(item) is None
                for item in self.object_names
            )
            or len(set(self.object_names)) != len(self.object_names)
        ):
            _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/object_names")
        before = _finite(self.before_volume_mm3, "/receipt/before_volume_mm3")
        after = _finite(self.after_volume_mm3, "/receipt/after_volume_mm3")
        if before <= 0.0 or after <= 0.0 or math.isclose(before, after, rel_tol=0.0, abs_tol=1e-9):
            _fail(PartDesignDressupTransformRuleErrorCode.CONFORMANCE_FAILED, "/receipt")
        body = {
            "authority": "none",
            "plan_sha256": self.plan_sha256,
            "operation": self.operation.value,
            "object_names": list(self.object_names),
            "before_volume_mm3": before,
            "after_volume_mm3": after,
        }
        digest = hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()
        object.__setattr__(self, "receipt_sha256", digest)
        object.__setattr__(self, "receipt_id", f"partdesign_dressup_transform_{digest[:32]}")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _shape_volume(shape: object, path: str) -> float:
    try:
        if shape is None or shape.isNull() or not shape.isValid() or len(shape.Solids) != 1:
            _fail(PartDesignDressupTransformRuleErrorCode.PRECONDITION_FAILED, path)
        volume = float(shape.Volume)
    except PartDesignDressupTransformRuleError:
        raise
    except Exception:
        _fail(PartDesignDressupTransformRuleErrorCode.PRECONDITION_FAILED, path)
    if not math.isfinite(volume) or volume <= 0.0:
        _fail(PartDesignDressupTransformRuleErrorCode.PRECONDITION_FAILED, path)
    return volume


def _same_selection(
    semantic: SemanticObjectSelection, authenticated: AuthenticatedDressupTransformObject
) -> bool:
    return (
        semantic.node_id == authenticated.node_id
        and semantic.result_id == authenticated.result_id
    )


def _validate_bindings(
    plan: PartDesignDressupTransformBackendPlan,
    bindings: PartDesignDressupTransformExecutionBindings,
) -> tuple[float, tuple[object, ...], object]:
    if bindings.body_id != plan.body_id or not _same_selection(plan.base, bindings.base):
        _fail(PartDesignDressupTransformRuleErrorCode.INTEGRITY_FAILURE, "/bindings/identity")
    document, body, base = bindings.document, bindings.body, bindings.base.object
    try:
        if (
            getattr(document, "UndoMode", 0) != 1
            or bool(document.HasPendingTransaction)
            or body.Document is not document
            or body.TypeId != "PartDesign::Body"
            or tuple(body.Group) != (base,)
            or body.Tip is not base
            or base.Document is not document
            or base.getParentGeoFeatureGroup() is not body
        ):
            _fail(PartDesignDressupTransformRuleErrorCode.PRECONDITION_FAILED, "/bindings")
    except PartDesignDressupTransformRuleError:
        raise
    except Exception:
        _fail(PartDesignDressupTransformRuleErrorCode.PRECONDITION_FAILED, "/bindings")
    return _shape_volume(base.Shape, "/bindings/base"), (base,), base


_AXIS_INDEX: Final = {Axis.X: 0, Axis.Y: 1, Axis.Z: 2}
_ORTHOGONAL: Final = {
    Axis.X: (Axis.Y, Axis.Z),
    Axis.Y: (Axis.X, Axis.Z),
    Axis.Z: (Axis.X, Axis.Y),
}


def _coordinates(vector: object) -> tuple[float, float, float]:
    try:
        return (float(vector.x), float(vector.y), float(vector.z))
    except Exception:
        _fail(PartDesignDressupTransformRuleErrorCode.RESOLUTION_FAILED, "/selection")


def _bounds(shape: object) -> tuple[tuple[float, float, float], tuple[float, float, float], float]:
    try:
        box = shape.BoundBox
        minimum = (float(box.XMin), float(box.YMin), float(box.ZMin))
        maximum = (float(box.XMax), float(box.YMax), float(box.ZMax))
        diagonal = float(box.DiagonalLength)
    except Exception:
        _fail(PartDesignDressupTransformRuleErrorCode.RESOLUTION_FAILED, "/selection")
    if not all(math.isfinite(value) for value in (*minimum, *maximum, diagonal)) or diagonal <= 0:
        _fail(PartDesignDressupTransformRuleErrorCode.RESOLUTION_FAILED, "/selection")
    return minimum, maximum, max(1e-8, diagonal * 1e-8)


def _side_value(
    minimum: tuple[float, ...], maximum: tuple[float, ...], axis: Axis, side: Side
) -> float:
    index = _AXIS_INDEX[axis]
    return (minimum if side is Side.MINIMUM else maximum)[index]


def _resolve_edge(shape: object, role: AxisAlignedEdgeRole) -> str:
    minimum, maximum, tolerance = _bounds(shape)
    axis_index = _AXIS_INDEX[role.axis]
    first_axis, second_axis = _ORTHOGONAL[role.axis]
    first_expected = _side_value(minimum, maximum, first_axis, role.first_side)
    second_expected = _side_value(minimum, maximum, second_axis, role.second_side)
    candidates: list[int] = []
    try:
        edges = tuple(shape.Edges)
    except Exception:
        _fail(PartDesignDressupTransformRuleErrorCode.RESOLUTION_FAILED, "/selection/edge")
    if len(edges) > 4096:
        _fail(PartDesignDressupTransformRuleErrorCode.RESOLUTION_FAILED, "/selection/edge")
    for index, edge in enumerate(edges, 1):
        try:
            vertices = tuple(edge.Vertexes)
            curve_kind = type(edge.Curve).__name__
        except Exception:
            continue
        if len(vertices) != 2 or curve_kind != "Line":
            continue
        points = tuple(_coordinates(vertex.Point) for vertex in vertices)
        if (
            abs(points[0][_AXIS_INDEX[first_axis]] - first_expected) > tolerance
            or abs(points[1][_AXIS_INDEX[first_axis]] - first_expected) > tolerance
            or abs(points[0][_AXIS_INDEX[second_axis]] - second_expected) > tolerance
            or abs(points[1][_AXIS_INDEX[second_axis]] - second_expected) > tolerance
            or abs(min(point[axis_index] for point in points) - minimum[axis_index]) > tolerance
            or abs(max(point[axis_index] for point in points) - maximum[axis_index]) > tolerance
        ):
            continue
        candidates.append(index)
    if len(candidates) != 1:
        _fail(PartDesignDressupTransformRuleErrorCode.RESOLUTION_FAILED, "/selection/edge")
    return f"Edge{candidates[0]}"


def _resolve_face(shape: object, role: AxisAlignedFaceRole) -> str:
    minimum, maximum, tolerance = _bounds(shape)
    axis_index = _AXIS_INDEX[role.axis]
    expected = _side_value(minimum, maximum, role.axis, role.side)
    candidates: list[int] = []
    try:
        faces = tuple(shape.Faces)
    except Exception:
        _fail(PartDesignDressupTransformRuleErrorCode.RESOLUTION_FAILED, "/selection/face")
    if len(faces) > 4096:
        _fail(PartDesignDressupTransformRuleErrorCode.RESOLUTION_FAILED, "/selection/face")
    for index, face in enumerate(faces, 1):
        try:
            vertices = tuple(face.Vertexes)
            surface_kind = type(face.Surface).__name__
        except Exception:
            continue
        if not vertices or surface_kind != "Plane":
            continue
        if all(
            abs(_coordinates(vertex.Point)[axis_index] - expected) <= tolerance
            for vertex in vertices
        ):
            candidates.append(index)
    if len(candidates) != 1:
        _fail(PartDesignDressupTransformRuleErrorCode.RESOLUTION_FAILED, "/selection/face")
    return f"Face{candidates[0]}"


_PLANE_ROLE: Final = {
    OriginPlane.XY: "XY_Plane",
    OriginPlane.XZ: "XZ_Plane",
    OriginPlane.YZ: "YZ_Plane",
}
_AXIS_ROLE: Final = {
    OriginAxis.X: "X_Axis",
    OriginAxis.Y: "Y_Axis",
    OriginAxis.Z: "Z_Axis",
}


def _origin_feature(body: object, role: str, expected_type_id: str, path: str) -> object:
    try:
        candidates = tuple(
            item
            for item in body.Origin.OriginFeatures
            if getattr(item, "Role", None) == role
            and item.TypeId == expected_type_id
            and item.getParentGeoFeatureGroup() is body
        )
    except Exception:
        _fail(PartDesignDressupTransformRuleErrorCode.PRECONDITION_FAILED, path)
    if len(candidates) != 1:
        _fail(PartDesignDressupTransformRuleErrorCode.PRECONDITION_FAILED, path)
    return candidates[0]


def _create_multi_step(
    document: object, body: object, step: MultiTransformStep, name: str
) -> object:
    spec = _NATIVE_STEP_SPECS[step.kind]
    child = document.addObject(spec.type_id, name)
    child.TransformMode = "Features"
    child.Refine = True
    if step.kind is MultiTransformStepKind.SCALED:
        child.Factor = step.factor
        child.Occurrences = step.occurrences
    else:
        plane = _origin_feature(
            body, _PLANE_ROLE[step.mirror_plane], "App::Plane", "/parameters/steps/mirror_plane"
        )
        child.MirrorPlane = (plane, [""])
    body.addObject(child)
    return child


def _validate_result(
    operation: PartDesignDressupTransformOperation,
    feature: object,
    base: object,
    body: object,
    before_volume: float,
) -> float:
    after_volume = _shape_volume(feature.Shape, "/result/shape")
    epsilon = max(1e-9, before_volume * 1e-12)
    increases = operation in {
        PartDesignDressupTransformOperation.SCALED,
        PartDesignDressupTransformOperation.MULTI_TRANSFORM,
    }
    decreases = operation in {
        PartDesignDressupTransformOperation.FILLET,
        PartDesignDressupTransformOperation.CHAMFER,
        PartDesignDressupTransformOperation.THICKNESS,
    }
    try:
        valid_common = (
            feature.TypeId == _NATIVE_SPECS[operation].type_id
            and feature.isValid()
            and tuple(feature.State) == ("Up-to-date",)
            and body.Tip is feature
            and feature.BaseFeature is base
            and bool(feature.Refine)
        )
    except Exception:
        valid_common = False
    if (
        not valid_common
        or increases
        and not after_volume > before_volume + epsilon
        or decreases
        and not after_volume < before_volume - epsilon
        or not increases
        and not decreases
        and math.isclose(after_volume, before_volume, rel_tol=0.0, abs_tol=epsilon)
    ):
        _fail(PartDesignDressupTransformRuleErrorCode.CONFORMANCE_FAILED, "/result")
    return after_volume


def apply_partdesign_dressup_transform_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: PartDesignDressupTransformExecutionBindings,
) -> PartDesignDressupTransformConformanceReceipt:
    """Explicit trusted-host action; exact plan validation precedes mutation."""

    if type(bindings) is not PartDesignDressupTransformExecutionBindings:
        _fail(PartDesignDressupTransformRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(PartDesignDressupTransformRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != PARTDESIGN_DRESSUP_TRANSFORM_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(PartDesignDressupTransformRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_partdesign_dressup_transform_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    before_volume, before_group, base = _validate_bindings(plan, bindings)
    document, body = bindings.document, bindings.body
    spec = _NATIVE_SPECS[plan.operation]
    object_name = f"{spec.object_prefix}_{plan.plan_sha256[:16]}"
    try:
        if document.getObject(object_name) is not None:
            _fail(
                PartDesignDressupTransformRuleErrorCode.PRECONDITION_FAILED,
                "/document/object_name",
            )
        before_objects = tuple(document.Objects)
        before_tip = body.Tip
        before_visibilities = tuple(bool(item.Visibility) for item in before_group)
    except PartDesignDressupTransformRuleError:
        raise
    except Exception:
        _fail(PartDesignDressupTransformRuleErrorCode.PRECONDITION_FAILED, "/document")

    # Resolve semantic roles against the authenticated live source.  Native
    # EdgeN/FaceN names are transient locals and never enter the plan.
    edge_name = None
    face_name = None
    if isinstance(plan.parameters, (FilletParameters, ChamferParameters)):
        edge_name = _resolve_edge(base.Shape, plan.parameters.edge_role)
    elif isinstance(plan.parameters, (DraftParameters, ThicknessParameters)):
        face_name = _resolve_face(base.Shape, plan.parameters.face_role)

    transaction_open = False
    created: list[object] = []
    try:
        document.openTransaction("VibeCAD trusted PartDesign dress-up/transform")
        transaction_open = True
        feature = document.addObject(spec.type_id, object_name)
        created.append(feature)
        feature.Refine = True

        if plan.operation is PartDesignDressupTransformOperation.SCALED:
            parameters = plan.parameters
            feature.Originals = [base]
            feature.Factor = parameters.factor
            feature.Occurrences = parameters.occurrences
            feature.TransformMode = "Features"
            body.addObject(feature)
        elif plan.operation is PartDesignDressupTransformOperation.MULTI_TRANSFORM:
            parameters = plan.parameters
            feature.Originals = [base]
            feature.Shape = base.Shape
            feature.TransformMode = "Features"
            body.addObject(feature)
            children = []
            for index, step in enumerate(parameters.steps):
                child_name = (
                    f"{_NATIVE_STEP_SPECS[step.kind].object_prefix}_"
                    f"{plan.plan_sha256[:12]}_{index}"
                )
                if document.getObject(child_name) is not None:
                    _fail(
                        PartDesignDressupTransformRuleErrorCode.PRECONDITION_FAILED,
                        f"/parameters/steps/{index}/name",
                    )
                child = _create_multi_step(document, body, step, child_name)
                children.append(child)
                created.append(child)
            feature.Transformations = children
        elif plan.operation is PartDesignDressupTransformOperation.FILLET:
            parameters = plan.parameters
            feature.Base = (base, [edge_name])
            feature.Radius = parameters.radius_mm
            feature.UseAllEdges = False
            feature.SupportTransform = False
            body.addObject(feature)
        elif plan.operation is PartDesignDressupTransformOperation.CHAMFER:
            parameters = plan.parameters
            feature.Base = (base, [edge_name])
            feature.ChamferType = "Equal distance"
            feature.Size = parameters.size_mm
            feature.UseAllEdges = False
            feature.SupportTransform = False
            body.addObject(feature)
        elif plan.operation is PartDesignDressupTransformOperation.DRAFT:
            parameters = plan.parameters
            plane = _origin_feature(
                body,
                _PLANE_ROLE[parameters.neutral_plane],
                "App::Plane",
                "/parameters/neutral_plane",
            )
            axis = _origin_feature(
                body,
                _AXIS_ROLE[parameters.pull_direction],
                "App::Line",
                "/parameters/pull_direction",
            )
            feature.Base = (base, [face_name])
            feature.NeutralPlane = (plane, [""])
            feature.PullDirection = (axis, [""])
            feature.Angle = parameters.angle_degrees
            feature.Reversed = parameters.reversed
            feature.SupportTransform = False
            body.addObject(feature)
        else:
            parameters = plan.parameters
            feature.Base = (base, [face_name])
            feature.Value = parameters.value_mm
            feature.Mode = "Skin"
            feature.Join = "Arc"
            feature.Reversed = True
            feature.Intersection = False
            feature.SupportTransform = False
            body.addObject(feature)
        body.Tip = feature
        document.recompute()
        after_volume = _validate_result(plan.operation, feature, base, body, before_volume)

        if plan.operation is PartDesignDressupTransformOperation.SCALED:
            if (
                tuple(feature.Originals) != (base,)
                or not math.isclose(float(feature.Factor), plan.parameters.factor, abs_tol=1e-12)
                or int(feature.Occurrences) != plan.parameters.occurrences
                or str(feature.TransformMode) != "Features"
            ):
                _fail(PartDesignDressupTransformRuleErrorCode.CONFORMANCE_FAILED, "/result/links")
        elif plan.operation is PartDesignDressupTransformOperation.MULTI_TRANSFORM:
            children = tuple(created[1:])
            if tuple(feature.Originals) != (base,) or tuple(feature.Transformations) != children:
                _fail(PartDesignDressupTransformRuleErrorCode.CONFORMANCE_FAILED, "/result/links")
            for index, (step, child) in enumerate(
                zip(plan.parameters.steps, children, strict=True)
            ):
                valid_step = (
                    child.TypeId == _NATIVE_STEP_SPECS[step.kind].type_id
                    and str(child.TransformMode) == "Features"
                    and bool(child.Refine)
                )
                if step.kind is MultiTransformStepKind.SCALED:
                    valid_step = (
                        valid_step
                        and math.isclose(
                            float(child.Factor),
                            step.factor,
                            rel_tol=0.0,
                            abs_tol=1e-9,
                        )
                        and int(child.Occurrences) == step.occurrences
                    )
                else:
                    expected_plane = _origin_feature(
                        body,
                        _PLANE_ROLE[step.mirror_plane],
                        "App::Plane",
                        f"/result/steps/{index}/mirror_plane",
                    )
                    valid_step = valid_step and child.MirrorPlane[0] is expected_plane
                if not valid_step:
                    _fail(
                        PartDesignDressupTransformRuleErrorCode.CONFORMANCE_FAILED,
                        f"/result/steps/{index}",
                    )
        elif plan.operation in {
            PartDesignDressupTransformOperation.FILLET,
            PartDesignDressupTransformOperation.CHAMFER,
        }:
            if feature.Base[0] is not base or tuple(feature.Base[1]) != (edge_name,):
                _fail(PartDesignDressupTransformRuleErrorCode.CONFORMANCE_FAILED, "/result/base")
            if plan.operation is PartDesignDressupTransformOperation.FILLET:
                valid_parameters = (
                    math.isclose(
                        float(feature.Radius),
                        plan.parameters.radius_mm,
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                    and not bool(feature.UseAllEdges)
                    and not bool(feature.SupportTransform)
                )
            else:
                valid_parameters = (
                    str(feature.ChamferType) == "Equal distance"
                    and math.isclose(
                        float(feature.Size),
                        plan.parameters.size_mm,
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                    and not bool(feature.UseAllEdges)
                    and not bool(feature.SupportTransform)
                )
            if not valid_parameters:
                _fail(
                    PartDesignDressupTransformRuleErrorCode.CONFORMANCE_FAILED,
                    "/result/parameters",
                )
        else:
            if feature.Base[0] is not base or tuple(feature.Base[1]) != (face_name,):
                _fail(PartDesignDressupTransformRuleErrorCode.CONFORMANCE_FAILED, "/result/base")
            if plan.operation is PartDesignDressupTransformOperation.DRAFT:
                valid_parameters = (
                    feature.NeutralPlane[0] is plane
                    and feature.PullDirection[0] is axis
                    and math.isclose(
                        float(feature.Angle),
                        plan.parameters.angle_degrees,
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                    and bool(feature.Reversed) is plan.parameters.reversed
                    and not bool(feature.SupportTransform)
                )
            else:
                valid_parameters = (
                    math.isclose(
                        float(feature.Value),
                        plan.parameters.value_mm,
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                    and str(feature.Mode) == "Skin"
                    and str(feature.Join) == "Arc"
                    and bool(feature.Reversed)
                    and not bool(feature.Intersection)
                    and not bool(feature.SupportTransform)
                )
            if not valid_parameters:
                _fail(
                    PartDesignDressupTransformRuleErrorCode.CONFORMANCE_FAILED,
                    "/result/parameters",
                )
        document.commitTransaction()
        transaction_open = False
    except BaseException as error:
        if transaction_open:
            try:
                document.abortTransaction()
                document.recompute()
            except BaseException:
                _fail(
                    PartDesignDressupTransformRuleErrorCode.TRANSACTION_FAILED,
                    "/transaction/rollback",
                )
        try:
            after_objects = tuple(document.Objects)
            after_group = tuple(body.Group)
            if (
                after_objects != before_objects
                or after_group != before_group
                or body.Tip is not before_tip
                or tuple(bool(item.Visibility) for item in before_group) != before_visibilities
            ):
                _fail(
                    PartDesignDressupTransformRuleErrorCode.TRANSACTION_FAILED,
                    "/transaction/rollback",
                )
        except PartDesignDressupTransformRuleError:
            raise
        except BaseException:
            _fail(
                PartDesignDressupTransformRuleErrorCode.TRANSACTION_FAILED,
                "/transaction/rollback",
            )
        if isinstance(error, PartDesignDressupTransformRuleError):
            raise error
        _fail(PartDesignDressupTransformRuleErrorCode.CONFORMANCE_FAILED, "/result")

    return PartDesignDressupTransformConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        object_names=tuple(item.Name for item in created),
        before_volume_mm3=before_volume,
        after_volume_mm3=after_volume,
    )
