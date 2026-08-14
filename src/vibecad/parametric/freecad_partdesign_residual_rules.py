"""Trusted native rules for the reviewed PartDesign residual family.

The family intentionally covers only three narrow operations on the pinned
FreeCAD 1.1.0 build: a flat unthreaded Hole, an angle-mode additive
Revolution, and an unattached explicitly placed local coordinate system.
Backend plans are evidence-only and carry no execution authority.  Native
mutation happens only through :func:`apply_partdesign_residual_plan` after the
host has supplied authenticated live objects.
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

PARTDESIGN_RESIDUAL_PLAN_SCHEMA_VERSION: Final = 1
PARTDESIGN_RESIDUAL_PLAN_MEDIA_TYPE: Final = (
    "application/vnd.vibecad.freecad-partdesign-residual-plan+json"
)
MAX_PARTDESIGN_RESIDUAL_PLAN_BYTES: Final = 32 * 1024
PARTDESIGN_RESIDUAL_FREECAD_ENGINE_BUILD_ID: Final = (
    "34a9716668b1ddeb55b914f1c5be644826bdbbbf"
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FACE = re.compile(r"Face([1-9][0-9]{0,3})\Z")
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-residual-plan.v1\0"
_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-partdesign-residual-rule.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-residual-receipt.v1\0"

PARTDESIGN_RESIDUAL_RULE_ID: Final = "freecad.partdesign.residual-family.v1"
_NATIVE_CONTRACT = (
    "engine=FreeCAD-1.1.0/"
    f"{PARTDESIGN_RESIDUAL_FREECAD_ENGINE_BUILD_ID};"
    "hole=PartDesign::Hole/Profile,BaseFeature,DepthType:Dimension|ThroughAll,"
    "Depth,Diameter,HoleCutType=None,HoleCutCustomValues=false,ThreadType=None,"
    "Threaded=false,ModelThread=false,Tapered=false,DrillPoint=Flat,"
    "DrillForDepth=false,UseCustomThreadClearance=false,Midplane=false,"
    "Reversed=false,Refine=true,AllowMultiFace=false;"
    "revolution=PartDesign::Revolution/Profile,BaseFeature:optional,"
    "ReferenceAxis:Sketcher::SketchObject/H_Axis|V_Axis,Type=Angle,Angle,"
    "Angle2=0,Midplane=false,Reversed=false,Refine=true,AllowMultiFace=false;"
    "coordinate-system=PartDesign::CoordinateSystem,MapMode=Deactivated,"
    "AttachmentSupport=empty,Placement=explicit,preserve-body-tip;"
    "same-document=true;body-group=true;single-solid=true;transaction=shared-rollback"
)
PARTDESIGN_RESIDUAL_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN + _NATIVE_CONTRACT.encode("ascii")
).hexdigest()


class PartDesignResidualOperation(StrEnum):
    HOLE = "hole"
    REVOLUTION = "revolution"
    COORDINATE_SYSTEM = "coordinate_system"


class HoleExtent(StrEnum):
    DIMENSION = "dimension"
    THROUGH_ALL = "through_all"


class RevolutionAxis(StrEnum):
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"


PARTDESIGN_RESIDUAL_NATIVE_TYPE_IDS: Final = {
    PartDesignResidualOperation.HOLE: "PartDesign::Hole",
    PartDesignResidualOperation.REVOLUTION: "PartDesign::Revolution",
    PartDesignResidualOperation.COORDINATE_SYSTEM: "PartDesign::CoordinateSystem",
}

PARTDESIGN_RESIDUAL_NATIVE_PROPERTIES: Final = {
    PartDesignResidualOperation.HOLE: (
        "AllowMultiFace",
        "BaseFeature",
        "Depth",
        "DepthType",
        "Diameter",
        "DrillForDepth",
        "DrillPoint",
        "HoleCutCustomValues",
        "HoleCutType",
        "Midplane",
        "ModelThread",
        "Profile",
        "Refine",
        "Reversed",
        "Tapered",
        "ThreadType",
        "Threaded",
        "UseCustomThreadClearance",
    ),
    PartDesignResidualOperation.REVOLUTION: (
        "AllowMultiFace",
        "Angle",
        "Angle2",
        "BaseFeature",
        "Midplane",
        "Profile",
        "ReferenceAxis",
        "Refine",
        "Reversed",
        "Type",
    ),
    PartDesignResidualOperation.COORDINATE_SYSTEM: (
        "AttachmentSupport",
        "MapMode",
        "Placement",
    ),
}


class PartDesignResidualRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class PartDesignResidualRuleError(ValueError):
    """Bounded failure from the family-specific reviewed native boundary."""

    def __init__(
        self,
        code: PartDesignResidualRuleErrorCode,
        path: str = "/",
    ) -> None:
        if type(code) is not PartDesignResidualRuleErrorCode:
            raise TypeError("code must be an exact PartDesignResidualRuleErrorCode")
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
        super().__init__(f"PartDesign residual rule error ({code.value}) at {path}")


def _fail(code: PartDesignResidualRuleErrorCode, path: str) -> None:
    raise PartDesignResidualRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, path)
    return value


def _finite(
    value: object,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, path)
    result = float(value)
    if (minimum is not None and result < minimum) or (
        maximum is not None and result > maximum
    ):
        _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, path)
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
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/")
    if not raw or len(raw) > MAX_PARTDESIGN_RESIDUAL_PLAN_BYTES:
        _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/")
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
    if (
        type(raw) is not bytes
        or not raw
        or len(raw) > MAX_PARTDESIGN_RESIDUAL_PLAN_BYTES
    ):
        _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(PartDesignResidualRuleErrorCode.INTEGRITY_FAILURE, "/")
    except (UnicodeError, ValueError, RecursionError):
        _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/")
    if type(value) is not dict or not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(PartDesignResidualRuleErrorCode.INTEGRITY_FAILURE, "/")
    return value


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if (
        type(value) is not dict
        or set(value) != keys
        or any(type(key) is not str for key in value)
    ):
        _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class SemanticObjectSelection:
    node_id: str
    result_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/node_id"))
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/result_id"))

    def to_mapping(self) -> dict[str, str]:
        return {"node_id": self.node_id, "result_id": self.result_id}

    @classmethod
    def from_mapping(cls, value: object, path: str) -> SemanticObjectSelection:
        fields = _exact_fields(value, {"node_id", "result_id"}, path)
        return cls(node_id=fields["node_id"], result_id=fields["result_id"])


@dataclass(frozen=True, slots=True, kw_only=True)
class ExplicitPlacement:
    position_mm: tuple[float, float, float]
    axis: tuple[float, float, float]
    angle_degrees: float

    def __post_init__(self) -> None:
        if type(self.position_mm) is not tuple or len(self.position_mm) != 3:
            _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/placement/position_mm")
        if type(self.axis) is not tuple or len(self.axis) != 3:
            _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/placement/axis")
        position = tuple(
            _finite(item, f"/placement/position_mm/{index}", minimum=-1e6, maximum=1e6)
            for index, item in enumerate(self.position_mm)
        )
        axis = tuple(
            _finite(item, f"/placement/axis/{index}", minimum=-1.0, maximum=1.0)
            for index, item in enumerate(self.axis)
        )
        norm = math.sqrt(sum(item * item for item in axis))
        if norm <= 1e-12 or abs(norm - 1.0) > 1e-9:
            _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/placement/axis")
        angle = _finite(
            self.angle_degrees,
            "/placement/angle_degrees",
            minimum=-360.0,
            maximum=360.0,
        )
        object.__setattr__(self, "position_mm", position)
        object.__setattr__(self, "axis", axis)
        object.__setattr__(self, "angle_degrees", angle)

    def to_mapping(self) -> dict[str, object]:
        return {
            "position_mm": list(self.position_mm),
            "axis": list(self.axis),
            "angle_degrees": self.angle_degrees,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str) -> ExplicitPlacement:
        fields = _exact_fields(value, {"position_mm", "axis", "angle_degrees"}, path)
        if type(fields["position_mm"]) is not list or type(fields["axis"]) is not list:
            _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, path)
        return cls(
            position_mm=tuple(fields["position_mm"]),
            axis=tuple(fields["axis"]),
            angle_degrees=fields["angle_degrees"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignResidualBackendPlan:
    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    manifest_sha256: str
    body_id: str
    node_id: str
    result_id: str
    operation: PartDesignResidualOperation
    base: SemanticObjectSelection | None = None
    profile: SemanticObjectSelection | None = None
    axis_reference_id: str | None = None
    axis_result_id: str | None = None
    hole_extent: HoleExtent | None = None
    diameter_mm: float | None = None
    depth_mm: float | None = None
    revolution_axis: RevolutionAxis | None = None
    angle_degrees: float | None = None
    placement: ExplicitPlacement | None = None
    schema_version: int = PARTDESIGN_RESIDUAL_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/schema_version")
        for name in ("source_artifact_id", "source_graph_id", "body_id", "node_id", "result_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in (
            "source_graph_sha256",
            "source_content_sha256",
            "lowering_request_sha256",
            "adapter_contract_sha256",
            "manifest_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if type(self.operation) is not PartDesignResidualOperation:
            _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/operation")
        for name in ("base", "profile"):
            value = getattr(self, name)
            if value is not None and type(value) is not SemanticObjectSelection:
                _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, f"/{name}")
        for name in ("axis_reference_id", "axis_result_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _identifier(value, f"/{name}"))
        if self.base is not None and self.profile is not None and self.base == self.profile:
            _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/selection")
        selected_nodes = {
            item.node_id for item in (self.base, self.profile) if item is not None
        }
        if self.node_id in selected_nodes:
            _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/node_id")

        if self.operation is PartDesignResidualOperation.HOLE:
            if (
                self.base is None
                or self.profile is None
                or self.axis_reference_id is not None
                or self.axis_result_id is not None
                or type(self.hole_extent) is not HoleExtent
                or self.revolution_axis is not None
                or self.angle_degrees is not None
                or self.placement is not None
            ):
                _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/operation")
            diameter = _finite(
                self.diameter_mm,
                "/operation/diameter_mm",
                minimum=0.01,
                maximum=1e6,
            )
            if self.hole_extent is HoleExtent.DIMENSION:
                depth = _finite(
                    self.depth_mm,
                    "/operation/depth_mm",
                    minimum=0.01,
                    maximum=1e6,
                )
            elif self.depth_mm is not None:
                _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/operation/depth_mm")
            else:
                depth = None
            object.__setattr__(self, "diameter_mm", diameter)
            object.__setattr__(self, "depth_mm", depth)
        elif self.operation is PartDesignResidualOperation.REVOLUTION:
            if (
                self.profile is None
                or self.axis_reference_id is None
                or self.axis_result_id is None
                or self.hole_extent is not None
                or self.diameter_mm is not None
                or self.depth_mm is not None
                or type(self.revolution_axis) is not RevolutionAxis
                or self.placement is not None
            ):
                _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/operation")
            object.__setattr__(
                self,
                "angle_degrees",
                _finite(
                    self.angle_degrees,
                    "/operation/angle_degrees",
                    minimum=1e-6,
                    maximum=360.0,
                ),
            )
        elif any(
            item is not None
            for item in (
                self.base,
                self.profile,
                self.axis_reference_id,
                self.axis_result_id,
                self.hole_extent,
                self.diameter_mm,
                self.depth_mm,
                self.revolution_axis,
                self.angle_degrees,
            )
        ) or type(self.placement) is not ExplicitPlacement:
            _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/operation")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    def _operation_mapping(self) -> dict[str, object]:
        if self.operation is PartDesignResidualOperation.HOLE:
            return {
                "kind": self.operation.value,
                "extent": self.hole_extent.value,  # type: ignore[union-attr]
                "diameter_mm": self.diameter_mm,
                "depth_mm": self.depth_mm,
            }
        if self.operation is PartDesignResidualOperation.REVOLUTION:
            return {
                "kind": self.operation.value,
                "axis": self.revolution_axis.value,  # type: ignore[union-attr]
                "angle_degrees": self.angle_degrees,
            }
        return {
            "kind": self.operation.value,
            "placement": self.placement.to_mapping(),  # type: ignore[union-attr]
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority": "none",
            "backend": {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PARTDESIGN_RESIDUAL_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": PARTDESIGN_RESIDUAL_RULE_ID,
                "rule_contract_sha256": PARTDESIGN_RESIDUAL_RULE_CONTRACT_SHA256,
                "manifest_sha256": self.manifest_sha256,
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
                "profile": None if self.profile is None else self.profile.to_mapping(),
                "axis_reference_id": self.axis_reference_id,
                "axis_result_id": self.axis_result_id,
            },
            "operation": self._operation_mapping(),
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_PLAN_DIGEST_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> PartDesignResidualBackendPlan:
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
        rule = _exact_fields(
            root["rule"], {"rule_id", "rule_contract_sha256", "manifest_sha256"}, "/rule"
        )
        source = _exact_fields(
            root["source"], {"artifact_id", "graph_id", "graph_sha256", "content_sha256"}, "/source"
        )
        binding = _exact_fields(
            root["binding"], {"lowering_request_sha256", "adapter_contract_sha256"}, "/binding"
        )
        selection = _exact_fields(
            root["selection"],
            {
                "body_id",
                "node_id",
                "result_id",
                "base",
                "profile",
                "axis_reference_id",
                "axis_result_id",
            },
            "/selection",
        )
        if (
            root["authority"] != "none"
            or backend
            != {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PARTDESIGN_RESIDUAL_FREECAD_ENGINE_BUILD_ID,
            }
            or rule["rule_id"] != PARTDESIGN_RESIDUAL_RULE_ID
            or rule["rule_contract_sha256"] != PARTDESIGN_RESIDUAL_RULE_CONTRACT_SHA256
        ):
            _fail(PartDesignResidualRuleErrorCode.INTEGRITY_FAILURE, "/contract")
        if type(root["operation"]) is not dict:
            _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/operation")
        operation = root["operation"]
        try:
            kind = PartDesignResidualOperation(operation.get("kind"))
        except (TypeError, ValueError):
            _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/operation/kind")
        hole_extent = None
        diameter = depth = angle = None
        axis = None
        placement = None
        if kind is PartDesignResidualOperation.HOLE:
            operation = _exact_fields(
                operation, {"kind", "extent", "diameter_mm", "depth_mm"}, "/operation"
            )
            try:
                hole_extent = HoleExtent(operation["extent"])
            except (TypeError, ValueError):
                _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/operation/extent")
            diameter = operation["diameter_mm"]
            depth = operation["depth_mm"]
        elif kind is PartDesignResidualOperation.REVOLUTION:
            operation = _exact_fields(
                operation, {"kind", "axis", "angle_degrees"}, "/operation"
            )
            try:
                axis = RevolutionAxis(operation["axis"])
            except (TypeError, ValueError):
                _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/operation/axis")
            angle = operation["angle_degrees"]
        else:
            operation = _exact_fields(operation, {"kind", "placement"}, "/operation")
            placement = ExplicitPlacement.from_mapping(
                operation["placement"], "/operation/placement"
            )

        def selection_value(name: str) -> SemanticObjectSelection | None:
            raw = selection[name]
            return (
                None
                if raw is None
                else SemanticObjectSelection.from_mapping(raw, f"/selection/{name}")
            )

        return cls(
            schema_version=root["schema_version"],
            source_artifact_id=source["artifact_id"],
            source_graph_id=source["graph_id"],
            source_graph_sha256=source["graph_sha256"],
            source_content_sha256=source["content_sha256"],
            lowering_request_sha256=binding["lowering_request_sha256"],
            adapter_contract_sha256=binding["adapter_contract_sha256"],
            manifest_sha256=rule["manifest_sha256"],
            body_id=selection["body_id"],
            node_id=selection["node_id"],
            result_id=selection["result_id"],
            operation=kind,
            base=selection_value("base"),
            profile=selection_value("profile"),
            axis_reference_id=selection["axis_reference_id"],
            axis_result_id=selection["axis_result_id"],
            hole_extent=hole_extent,
            diameter_mm=diameter,
            depth_mm=depth,
            revolution_axis=axis,
            angle_degrees=angle,
            placement=placement,
        )


def decode_partdesign_residual_backend_plan(
    raw: bytes,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> PartDesignResidualBackendPlan:
    mapping = _decode_mapping(raw)
    plan = PartDesignResidualBackendPlan.from_mapping(mapping)
    if not hmac.compare_digest(raw, plan.canonical_bytes):
        _fail(PartDesignResidualRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(),
        _digest(expected_content_sha256, "/expected_content_sha256"),
    ):
        _fail(PartDesignResidualRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        plan.plan_sha256, _digest(expected_plan_sha256, "/expected_plan_sha256")
    ):
        _fail(PartDesignResidualRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return plan


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticatedResidualObject:
    object: object
    node_id: str
    result_id: str

    def __post_init__(self) -> None:
        if self.object is None:
            _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/bindings/object")
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/bindings/node_id"))
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/bindings/result_id"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignResidualExecutionBindings:
    document: object
    body: object
    body_id: str
    base: AuthenticatedResidualObject | None = None
    profile: AuthenticatedResidualObject | None = None

    def __post_init__(self) -> None:
        if self.document is None or self.body is None:
            _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/bindings")
        object.__setattr__(self, "body_id", _identifier(self.body_id, "/bindings/body_id"))
        for name in ("base", "profile"):
            value = getattr(self, name)
            if value is not None and type(value) is not AuthenticatedResidualObject:
                _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, f"/bindings/{name}")


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignResidualConformanceReceipt:
    plan_sha256: str
    operation: PartDesignResidualOperation
    object_name: str
    native_type_id: str
    before_volume_mm3: float | None = None
    after_volume_mm3: float | None = None
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/plan_sha256"))
        if type(self.operation) is not PartDesignResidualOperation:
            _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/operation")
        object.__setattr__(self, "object_name", _identifier(self.object_name, "/object_name"))
        expected = PARTDESIGN_RESIDUAL_NATIVE_TYPE_IDS[self.operation]
        if self.native_type_id != expected:
            _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/native_type_id")
        for name in ("before_volume_mm3", "after_volume_mm3"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _finite(value, f"/{name}", minimum=0.0))
        if self.operation is PartDesignResidualOperation.COORDINATE_SYSTEM:
            if self.before_volume_mm3 is not None or self.after_volume_mm3 is not None:
                _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/volume")
        elif self.after_volume_mm3 is None:
            _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/after_volume_mm3")
        body = {
            "plan_sha256": self.plan_sha256,
            "operation": self.operation.value,
            "object_name": self.object_name,
            "native_type_id": self.native_type_id,
            "before_volume_mm3": self.before_volume_mm3,
            "after_volume_mm3": self.after_volume_mm3,
        }
        object.__setattr__(
            self,
            "receipt_sha256",
            hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest(),
        )

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _same_sequence(left: object, right: tuple[object, ...]) -> bool:
    try:
        current = tuple(left)
    except Exception:
        return False
    return len(current) == len(right) and all(
        actual is expected for actual, expected in zip(current, right, strict=True)
    )


def _shape_volume(
    shape: object,
    path: str,
    *,
    code: PartDesignResidualRuleErrorCode = (
        PartDesignResidualRuleErrorCode.CONFORMANCE_FAILED
    ),
) -> float:
    try:
        volume = float(shape.Volume)
        valid = bool(shape.isValid())
        null = bool(shape.isNull())
        solids = tuple(shape.Solids)
    except Exception:
        _fail(code, path)
    if not math.isfinite(volume) or volume <= 1e-9 or not valid or null or len(solids) != 1:
        _fail(code, path)
    return volume


def _validate_authenticated(
    selection: SemanticObjectSelection | None,
    authenticated: AuthenticatedResidualObject | None,
    path: str,
) -> object | None:
    if selection is None:
        if authenticated is not None:
            _fail(PartDesignResidualRuleErrorCode.PRECONDITION_FAILED, path)
        return None
    if (
        authenticated is None
        or authenticated.node_id != selection.node_id
        or authenticated.result_id != selection.result_id
    ):
        _fail(PartDesignResidualRuleErrorCode.PRECONDITION_FAILED, path)
    return authenticated.object


def _validate_bindings(
    plan: PartDesignResidualBackendPlan,
    bindings: PartDesignResidualExecutionBindings,
) -> tuple[object | None, object | None, float | None]:
    document, body = bindings.document, bindings.body
    base = _validate_authenticated(plan.base, bindings.base, "/bindings/base")
    profile = _validate_authenticated(plan.profile, bindings.profile, "/bindings/profile")
    try:
        group = tuple(body.Group)
        if (
            bindings.body_id != plan.body_id
            or body.TypeId != "PartDesign::Body"
            or body.Document is not document
            or body not in tuple(document.Objects)
            or any(item is not None and item not in group for item in (base, profile))
        ):
            _fail(PartDesignResidualRuleErrorCode.PRECONDITION_FAILED, "/bindings")
    except PartDesignResidualRuleError:
        raise
    except Exception:
        _fail(PartDesignResidualRuleErrorCode.PRECONDITION_FAILED, "/bindings")

    before_volume = None
    if base is not None:
        try:
            if not str(base.TypeId).startswith("PartDesign::") or base.Document is not document:
                _fail(PartDesignResidualRuleErrorCode.PRECONDITION_FAILED, "/bindings/base")
            before_volume = _shape_volume(
                base.Shape,
                "/bindings/base/shape",
                code=PartDesignResidualRuleErrorCode.PRECONDITION_FAILED,
            )
        except PartDesignResidualRuleError:
            raise
        except Exception:
            _fail(PartDesignResidualRuleErrorCode.PRECONDITION_FAILED, "/bindings/base")
    if profile is not None:
        try:
            if profile.TypeId != "Sketcher::SketchObject" or profile.Document is not document:
                _fail(PartDesignResidualRuleErrorCode.PRECONDITION_FAILED, "/bindings/profile")
            if plan.operation is PartDesignResidualOperation.HOLE:
                support = tuple(profile.AttachmentSupport)
                if (
                    base is None
                    or len(support) != 1
                    or support[0][0] is not base
                    or len(tuple(support[0][1])) != 1
                    or _FACE.fullmatch(tuple(support[0][1])[0]) is None
                    or profile.MapMode != "FlatFace"
                    or int(profile.GeometryCount) != 1
                    or profile.Geometry[0].TypeId != "Part::GeomCircle"
                    or bool(profile.getConstruction(0))
                ):
                    _fail(
                        PartDesignResidualRuleErrorCode.PRECONDITION_FAILED,
                        "/bindings/profile",
                    )
            elif (
                not profile.isValid()
                or len(tuple(profile.Shape.Wires)) != 1
                or not profile.Shape.Wires[0].isClosed()
                or len(tuple(profile.OpenVertices)) != 0
            ):
                _fail(
                    PartDesignResidualRuleErrorCode.PRECONDITION_FAILED,
                    "/bindings/profile",
                )
        except PartDesignResidualRuleError:
            raise
        except Exception:
            _fail(PartDesignResidualRuleErrorCode.PRECONDITION_FAILED, "/bindings/profile")
    return base, profile, before_volume


def _placement_matches(actual: object, expected: ExplicitPlacement) -> bool:
    try:
        position = (float(actual.Base.x), float(actual.Base.y), float(actual.Base.z))
        quaternion = tuple(float(item) for item in actual.Rotation.Q)
        half_angle = math.radians(expected.angle_degrees) / 2.0
        sine = math.sin(half_angle)
        expected_quaternion = (
            expected.axis[0] * sine,
            expected.axis[1] * sine,
            expected.axis[2] * sine,
            math.cos(half_angle),
        )
        same_rotation = all(
            abs(actual_value - expected_value) <= 1e-9
            for actual_value, expected_value in zip(
                quaternion, expected_quaternion, strict=True
            )
        )
        negated_rotation = all(
            abs(actual_value + expected_value) <= 1e-9
            for actual_value, expected_value in zip(
                quaternion, expected_quaternion, strict=True
            )
        )
        return (
            all(
                abs(position[index] - expected.position_mm[index]) <= 1e-9
                for index in range(3)
            )
            and (same_rotation or negated_rotation)
        )
    except Exception:
        return False


def apply_partdesign_residual_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: PartDesignResidualExecutionBindings,
) -> PartDesignResidualConformanceReceipt:
    """Execute one exact reviewed plan in a managed FreeCAD transaction."""

    if type(bindings) is not PartDesignResidualExecutionBindings:
        _fail(PartDesignResidualRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(PartDesignResidualRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != PARTDESIGN_RESIDUAL_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(PartDesignResidualRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_partdesign_residual_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    base, profile, before_volume = _validate_bindings(plan, bindings)
    document, body = bindings.document, bindings.body
    native_type_id = PARTDESIGN_RESIDUAL_NATIVE_TYPE_IDS[plan.operation]
    prefix = {
        PartDesignResidualOperation.HOLE: "Hole",
        PartDesignResidualOperation.REVOLUTION: "Revolution",
        PartDesignResidualOperation.COORDINATE_SYSTEM: "CoordinateSystem",
    }[plan.operation]
    object_name = f"{prefix}_{plan.plan_sha256[:16]}"
    try:
        if document.getObject(object_name) is not None or bool(document.HasPendingTransaction):
            _fail(PartDesignResidualRuleErrorCode.PRECONDITION_FAILED, "/document")
    except PartDesignResidualRuleError:
        raise
    except Exception:
        _fail(PartDesignResidualRuleErrorCode.PRECONDITION_FAILED, "/document")

    def snapshot() -> tuple[tuple[object, ...], tuple[object, ...], object, tuple[bool, ...]]:
        group = tuple(body.Group)
        return (
            tuple(document.Objects),
            group,
            body.Tip,
            tuple(bool(item.Visibility) for item in group),
        )

    def rollback_matches(
        before: tuple[tuple[object, ...], tuple[object, ...], object, tuple[bool, ...]],
    ) -> bool:
        return (
            _same_sequence(document.Objects, before[0])
            and _same_sequence(body.Group, before[1])
            and body.Tip is before[2]
            and tuple(bool(item.Visibility) for item in tuple(body.Group)) == before[3]
        )

    after_volume: float | None = None

    def apply() -> object:
        nonlocal after_volume
        feature = body.newObject(native_type_id, object_name)
        if plan.operation is PartDesignResidualOperation.HOLE:
            assert profile is not None and base is not None and plan.hole_extent is not None
            feature.Profile = profile
            feature.DepthType = (
                "Dimension" if plan.hole_extent is HoleExtent.DIMENSION else "ThroughAll"
            )
            if plan.depth_mm is not None:
                feature.Depth = plan.depth_mm
            feature.Diameter = plan.diameter_mm
            feature.HoleCutType = "None"
            feature.HoleCutCustomValues = False
            feature.ThreadType = "None"
            feature.Threaded = False
            feature.ModelThread = False
            feature.Tapered = False
            feature.DrillPoint = "Flat"
            feature.DrillForDepth = False
            feature.UseCustomThreadClearance = False
            feature.Midplane = False
            feature.Reversed = False
            feature.Refine = True
            feature.AllowMultiFace = False
        elif plan.operation is PartDesignResidualOperation.REVOLUTION:
            assert profile is not None and plan.revolution_axis is not None
            feature.Profile = profile
            feature.ReferenceAxis = (
                profile,
                ["H_Axis" if plan.revolution_axis is RevolutionAxis.HORIZONTAL else "V_Axis"],
            )
            feature.Type = "Angle"
            feature.Angle = plan.angle_degrees
            feature.Angle2 = 0.0
            feature.Midplane = False
            feature.Reversed = False
            feature.Refine = True
            feature.AllowMultiFace = False
        else:
            assert plan.placement is not None
            placement = plan.placement
            feature.MapMode = "Deactivated"
            feature.AttachmentSupport = []
            feature.Placement = FreeCAD.Placement(
                FreeCAD.Vector(*placement.position_mm),
                FreeCAD.Rotation(FreeCAD.Vector(*placement.axis), placement.angle_degrees),
            )
            # Reference objects must not replace the previous solid Body tip.
            body.Tip = snapshot_before[2]
        document.recompute()
        try:
            if (
                feature.TypeId != native_type_id
                or not feature.isValid()
                or tuple(feature.State) != ("Up-to-date",)
                or feature not in tuple(body.Group)
                or document.getObject(object_name) is not feature
            ):
                _fail(PartDesignResidualRuleErrorCode.CONFORMANCE_FAILED, "/result")
            if plan.operation is PartDesignResidualOperation.HOLE:
                assert base is not None and profile is not None and before_volume is not None
                after_volume = _shape_volume(feature.Shape, "/result/shape")
                expected_depth = (
                    "Dimension" if plan.hole_extent is HoleExtent.DIMENSION else "ThroughAll"
                )
                if (
                    body.Tip is not feature
                    or feature.BaseFeature is not base
                    or feature.Profile[0] is not profile
                    or tuple(feature.Profile[1]) != ()
                    or feature.DepthType != expected_depth
                    or abs(float(feature.Diameter) - plan.diameter_mm) > 1e-9
                    or (
                        plan.depth_mm is not None
                        and abs(float(feature.Depth) - plan.depth_mm) > 1e-9
                    )
                    or feature.HoleCutType != "None"
                    or bool(feature.HoleCutCustomValues)
                    or feature.ThreadType != "None"
                    or bool(feature.Threaded)
                    or bool(feature.ModelThread)
                    or bool(feature.Tapered)
                    or feature.DrillPoint != "Flat"
                    or bool(feature.DrillForDepth)
                    or bool(feature.UseCustomThreadClearance)
                    or bool(feature.Midplane)
                    or bool(feature.Reversed)
                    or not bool(feature.Refine)
                    or bool(feature.AllowMultiFace)
                    or not after_volume < before_volume - max(1e-9, before_volume * 1e-12)
                ):
                    _fail(PartDesignResidualRuleErrorCode.CONFORMANCE_FAILED, "/result/hole")
            elif plan.operation is PartDesignResidualOperation.REVOLUTION:
                assert profile is not None
                after_volume = _shape_volume(feature.Shape, "/result/shape")
                axis_token = (
                    "H_Axis"
                    if plan.revolution_axis is RevolutionAxis.HORIZONTAL
                    else "V_Axis"
                )
                if (
                    body.Tip is not feature
                    or feature.BaseFeature is not base
                    or feature.Profile[0] is not profile
                    or tuple(feature.Profile[1]) != ()
                    or feature.ReferenceAxis[0] is not profile
                    or tuple(feature.ReferenceAxis[1]) != (axis_token,)
                    or feature.Type != "Angle"
                    or abs(float(feature.Angle) - plan.angle_degrees) > 1e-9
                    or abs(float(feature.Angle2)) > 1e-9
                    or bool(feature.Midplane)
                    or bool(feature.Reversed)
                    or not bool(feature.Refine)
                    or bool(feature.AllowMultiFace)
                    or (
                        before_volume is not None
                        and after_volume
                        <= before_volume + max(1e-9, before_volume * 1e-12)
                    )
                ):
                    _fail(
                        PartDesignResidualRuleErrorCode.CONFORMANCE_FAILED,
                        "/result/revolution",
                    )
            else:
                assert plan.placement is not None
                if (
                    body.Tip is not snapshot_before[2]
                    or feature.MapMode != "Deactivated"
                    or tuple(feature.AttachmentSupport) != ()
                    or not _placement_matches(feature.Placement, plan.placement)
                ):
                    _fail(
                        PartDesignResidualRuleErrorCode.CONFORMANCE_FAILED,
                        "/result/coordinate_system",
                    )
        except PartDesignResidualRuleError:
            raise
        except Exception:
            _fail(PartDesignResidualRuleErrorCode.CONFORMANCE_FAILED, "/result")
        return feature

    snapshot_before = snapshot()
    try:
        NativeTransactionRunner().run(
            document,
            label="VibeCAD trusted PartDesign residual family",
            snapshot=snapshot,
            apply=apply,
            rollback_matches=rollback_matches,
        )
    except NativeTransactionError as error:
        _fail(PartDesignResidualRuleErrorCode.TRANSACTION_FAILED, error.path)
    return PartDesignResidualConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        object_name=object_name,
        native_type_id=native_type_id,
        before_volume_mm3=before_volume,
        after_volume_mm3=after_volume,
    )


__all__ = [
    "MAX_PARTDESIGN_RESIDUAL_PLAN_BYTES",
    "PARTDESIGN_RESIDUAL_FREECAD_ENGINE_BUILD_ID",
    "PARTDESIGN_RESIDUAL_NATIVE_PROPERTIES",
    "PARTDESIGN_RESIDUAL_NATIVE_TYPE_IDS",
    "PARTDESIGN_RESIDUAL_PLAN_MEDIA_TYPE",
    "PARTDESIGN_RESIDUAL_PLAN_SCHEMA_VERSION",
    "PARTDESIGN_RESIDUAL_RULE_CONTRACT_SHA256",
    "PARTDESIGN_RESIDUAL_RULE_ID",
    "AuthenticatedResidualObject",
    "ExplicitPlacement",
    "HoleExtent",
    "PartDesignResidualBackendPlan",
    "PartDesignResidualConformanceReceipt",
    "PartDesignResidualExecutionBindings",
    "PartDesignResidualOperation",
    "PartDesignResidualRuleError",
    "PartDesignResidualRuleErrorCode",
    "RevolutionAxis",
    "SemanticObjectSelection",
    "apply_partdesign_residual_plan",
    "decode_partdesign_residual_backend_plan",
]
