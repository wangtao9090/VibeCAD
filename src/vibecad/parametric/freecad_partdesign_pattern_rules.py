"""Trusted FreeCAD rules for the first PartDesign pattern batch.

The wire plan is backend-neutral and carries only reviewed semantic operation,
reference, and parameter identities.  This module owns the complete static
mapping to FreeCAD ``TypeId`` and origin-object property names.  Importing it
does not import FreeCAD; execution is an explicit trusted-host action.
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

PARTDESIGN_PATTERN_PLAN_SCHEMA_VERSION: Final = 1
PARTDESIGN_PATTERN_PLAN_MEDIA_TYPE: Final = (
    "application/vnd.vibecad.freecad-partdesign-pattern-plan+json"
)
MAX_PARTDESIGN_PATTERN_PLAN_BYTES: Final = 24 * 1024
MAX_PARTDESIGN_PATTERN_OCCURRENCES: Final = 16
PARTDESIGN_PATTERN_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-pattern-plan.v1\0"
_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-partdesign-pattern-rule.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-pattern-receipt.v1\0"


class PartDesignPatternOperation(StrEnum):
    LINEAR_PATTERN = "linear_pattern"
    POLAR_PATTERN = "polar_pattern"
    MIRRORED = "mirrored"


class PatternOriginAxis(StrEnum):
    X = "x_axis"
    Y = "y_axis"
    Z = "z_axis"


class PatternOriginPlane(StrEnum):
    XY = "xy_plane"
    XZ = "xz_plane"
    YZ = "yz_plane"


@dataclass(frozen=True, slots=True)
class _NativePatternSpec:
    type_id: str
    object_prefix: str
    reference_property: str


_NATIVE_PATTERN_SPECS: Final = {
    PartDesignPatternOperation.LINEAR_PATTERN: _NativePatternSpec(
        "PartDesign::LinearPattern", "LinearPattern", "Direction"
    ),
    PartDesignPatternOperation.POLAR_PATTERN: _NativePatternSpec(
        "PartDesign::PolarPattern", "PolarPattern", "Axis"
    ),
    PartDesignPatternOperation.MIRRORED: _NativePatternSpec(
        "PartDesign::Mirrored", "Mirrored", "MirrorPlane"
    ),
}
_NATIVE_AXIS_OBJECTS: Final = {
    PatternOriginAxis.X: "X_Axis",
    PatternOriginAxis.Y: "Y_Axis",
    PatternOriginAxis.Z: "Z_Axis",
}
_NATIVE_PLANE_OBJECTS: Final = {
    PatternOriginPlane.XY: "XY_Plane",
    PatternOriginPlane.XZ: "XZ_Plane",
    PatternOriginPlane.YZ: "YZ_Plane",
}

PARTDESIGN_PATTERN_RULE_ID: Final = "freecad.partdesign.pattern-batch.v1"
_NATIVE_CONTRACT = (
    f"engine=FreeCAD-1.1.0/{PARTDESIGN_PATTERN_FREECAD_ENGINE_BUILD_ID};"
    "ops=linear_pattern:PartDesign::LinearPattern,"
    "polar_pattern:PartDesign::PolarPattern,mirrored:PartDesign::Mirrored;"
    "selection=authenticated-body/base/source-feature;"
    "origin-axis=x:X_Axis,y:Y_Axis,z:Z_Axis;"
    "origin-plane=xy:XY_Plane,xz:XZ_Plane,yz:YZ_Plane;"
    "linear=Originals,BaseFeature,Direction,Occurrences[2,16],Length>0,Reversed;"
    "polar=Originals,BaseFeature,Axis,Occurrences[2,16],Angle(0,360],Reversed;"
    "mirror=Originals,BaseFeature,MirrorPlane;"
    "result=Body.Tip,valid-single-solid,non-noop;transaction=rollback"
)
PARTDESIGN_PATTERN_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN + _NATIVE_CONTRACT.encode("ascii")
).hexdigest()


class PartDesignPatternRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class PartDesignPatternRuleError(ValueError):
    """Bounded, non-reflective failure from the trusted native rule."""

    def __init__(self, code: PartDesignPatternRuleErrorCode, path: str = "/") -> None:
        if type(code) is not PartDesignPatternRuleErrorCode:
            raise TypeError("code must be a PartDesignPatternRuleErrorCode")
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
        super().__init__(f"PartDesign pattern rule error ({code.value}) at {path}")


def _fail(code: PartDesignPatternRuleErrorCode, path: str) -> None:
    raise PartDesignPatternRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, path)
    return value


def _finite_float(
    value: object,
    path: str,
    *,
    code: PartDesignPatternRuleErrorCode = PartDesignPatternRuleErrorCode.INVALID_INPUT,
) -> float:
    if type(value) not in {int, float}:
        _fail(code, path)
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        _fail(code, path)
    if not math.isfinite(result):
        _fail(code, path)
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
    except (TypeError, ValueError, UnicodeError):
        _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/")
    if not raw or len(raw) > MAX_PARTDESIGN_PATTERN_PLAN_BYTES:
        _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/")
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
    if type(raw) is not bytes or not raw or len(raw) > MAX_PARTDESIGN_PATTERN_PLAN_BYTES:
        _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(PartDesignPatternRuleErrorCode.INTEGRITY_FAILURE, "/")
    except (UnicodeError, ValueError, RecursionError):
        _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/")
    if type(value) is not dict or not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(PartDesignPatternRuleErrorCode.INTEGRITY_FAILURE, "/")
    return value


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class PatternObjectSelection:
    node_id: str
    result_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/selection/node_id"))
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/selection/result_id"))

    def to_mapping(self) -> dict[str, str]:
        return {"node_id": self.node_id, "result_id": self.result_id}

    @classmethod
    def from_mapping(cls, value: object, path: str) -> PatternObjectSelection:
        fields = _exact_fields(value, {"node_id", "result_id"}, path)
        return cls(node_id=fields["node_id"], result_id=fields["result_id"])


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignPatternBackendPlan:
    """Canonical authority-free plan shared by all three pattern operations."""

    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    body_id: str
    node_id: str
    result_id: str
    operation: PartDesignPatternOperation
    base: PatternObjectSelection
    source_feature: PatternObjectSelection
    reference_id: str
    axis: PatternOriginAxis | None = None
    plane: PatternOriginPlane | None = None
    occurrences: int | None = None
    span_mm: float | None = None
    angle_degrees: float | None = None
    reversed: bool = False
    schema_version: int = PARTDESIGN_PATTERN_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/schema_version")
        if type(self.operation) is not PartDesignPatternOperation:
            _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/operation/id")
        for name in (
            "source_artifact_id",
            "source_graph_id",
            "body_id",
            "node_id",
            "result_id",
            "reference_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in (
            "source_graph_sha256",
            "source_content_sha256",
            "lowering_request_sha256",
            "adapter_contract_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if type(self.base) is not PatternObjectSelection:
            _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/selection/base")
        if type(self.source_feature) is not PatternObjectSelection:
            _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/selection/source_feature")
        if type(self.reversed) is not bool:
            _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/parameters/reversed")
        if self.operation is PartDesignPatternOperation.LINEAR_PATTERN:
            if (
                type(self.axis) is not PatternOriginAxis
                or self.plane is not None
                or type(self.occurrences) is not int
                or not 2 <= self.occurrences <= MAX_PARTDESIGN_PATTERN_OCCURRENCES
                or self.angle_degrees is not None
            ):
                _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/operation")
            span = _finite_float(self.span_mm, "/parameters/span_mm")
            if not 0.0 < span <= 1_000_000.0:
                _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/parameters/span_mm")
            object.__setattr__(self, "span_mm", span)
        elif self.operation is PartDesignPatternOperation.POLAR_PATTERN:
            if (
                type(self.axis) is not PatternOriginAxis
                or self.plane is not None
                or type(self.occurrences) is not int
                or not 2 <= self.occurrences <= MAX_PARTDESIGN_PATTERN_OCCURRENCES
                or self.span_mm is not None
            ):
                _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/operation")
            angle = _finite_float(self.angle_degrees, "/parameters/angle_degrees")
            if not 0.0 < angle <= 360.0:
                _fail(
                    PartDesignPatternRuleErrorCode.INVALID_INPUT,
                    "/parameters/angle_degrees",
                )
            object.__setattr__(self, "angle_degrees", angle)
        elif (
            self.axis is not None
            or type(self.plane) is not PatternOriginPlane
            or self.occurrences is not None
            or self.span_mm is not None
            or self.angle_degrees is not None
            or self.reversed
        ):
            _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/operation")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source": {
                "artifact_id": self.source_artifact_id,
                "graph_id": self.source_graph_id,
                "graph_sha256": self.source_graph_sha256,
                "content_sha256": self.source_content_sha256,
                "lowering_request_sha256": self.lowering_request_sha256,
                "adapter_contract_sha256": self.adapter_contract_sha256,
            },
            "target": {
                "body_id": self.body_id,
                "node_id": self.node_id,
                "result_id": self.result_id,
            },
            "operation": {
                "id": self.operation.value,
                "rule_id": PARTDESIGN_PATTERN_RULE_ID,
                "rule_contract_sha256": PARTDESIGN_PATTERN_RULE_CONTRACT_SHA256,
            },
            "selection": {
                "base": self.base.to_mapping(),
                "source_feature": self.source_feature.to_mapping(),
            },
            "reference": {
                "reference_id": self.reference_id,
                "axis": None if self.axis is None else self.axis.value,
                "plane": None if self.plane is None else self.plane.value,
            },
            "parameters": {
                "occurrences": self.occurrences,
                "span_mm": self.span_mm,
                "angle_degrees": self.angle_degrees,
                "reversed": self.reversed,
            },
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(
            _PLAN_DIGEST_DOMAIN
            + bytes.fromhex(PARTDESIGN_PATTERN_RULE_CONTRACT_SHA256)
            + self.canonical_bytes
        ).hexdigest()

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    @classmethod
    def from_mapping(cls, value: object) -> PartDesignPatternBackendPlan:
        fields = _exact_fields(
            value,
            {
                "schema_version",
                "source",
                "target",
                "operation",
                "selection",
                "reference",
                "parameters",
            },
            "/",
        )
        source = _exact_fields(
            fields["source"],
            {
                "artifact_id",
                "graph_id",
                "graph_sha256",
                "content_sha256",
                "lowering_request_sha256",
                "adapter_contract_sha256",
            },
            "/source",
        )
        target = _exact_fields(fields["target"], {"body_id", "node_id", "result_id"}, "/target")
        operation = _exact_fields(
            fields["operation"], {"id", "rule_id", "rule_contract_sha256"}, "/operation"
        )
        if (
            operation["rule_id"] != PARTDESIGN_PATTERN_RULE_ID
            or operation["rule_contract_sha256"] != PARTDESIGN_PATTERN_RULE_CONTRACT_SHA256
        ):
            _fail(PartDesignPatternRuleErrorCode.INTEGRITY_FAILURE, "/operation")
        try:
            operation_id = PartDesignPatternOperation(operation["id"])
        except (TypeError, ValueError):
            _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/operation/id")
        selection = _exact_fields(fields["selection"], {"base", "source_feature"}, "/selection")
        reference = _exact_fields(
            fields["reference"], {"reference_id", "axis", "plane"}, "/reference"
        )
        parameters = _exact_fields(
            fields["parameters"],
            {"occurrences", "span_mm", "angle_degrees", "reversed"},
            "/parameters",
        )
        try:
            axis = None if reference["axis"] is None else PatternOriginAxis(reference["axis"])
            plane = None if reference["plane"] is None else PatternOriginPlane(reference["plane"])
        except (TypeError, ValueError):
            _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/reference")
        return cls(
            schema_version=fields["schema_version"],
            source_artifact_id=source["artifact_id"],
            source_graph_id=source["graph_id"],
            source_graph_sha256=source["graph_sha256"],
            source_content_sha256=source["content_sha256"],
            lowering_request_sha256=source["lowering_request_sha256"],
            adapter_contract_sha256=source["adapter_contract_sha256"],
            body_id=target["body_id"],
            node_id=target["node_id"],
            result_id=target["result_id"],
            operation=operation_id,
            base=PatternObjectSelection.from_mapping(selection["base"], "/selection/base"),
            source_feature=PatternObjectSelection.from_mapping(
                selection["source_feature"], "/selection/source_feature"
            ),
            reference_id=reference["reference_id"],
            axis=axis,
            plane=plane,
            occurrences=parameters["occurrences"],
            span_mm=parameters["span_mm"],
            angle_degrees=parameters["angle_degrees"],
            reversed=parameters["reversed"],
        )


def decode_partdesign_pattern_backend_plan(
    raw: object,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> PartDesignPatternBackendPlan:
    if expected_content_sha256 is not None:
        expected_content_sha256 = _digest(expected_content_sha256, "/expected_content_sha256")
    if expected_plan_sha256 is not None:
        expected_plan_sha256 = _digest(expected_plan_sha256, "/expected_plan_sha256")
    result = PartDesignPatternBackendPlan.from_mapping(_decode_mapping(raw))
    if type(raw) is not bytes or not hmac.compare_digest(raw, result.canonical_bytes):
        _fail(PartDesignPatternRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_content_sha256
    ):
        _fail(PartDesignPatternRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        result.plan_sha256, expected_plan_sha256
    ):
        _fail(PartDesignPatternRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticatedPatternObject:
    object: object
    node_id: str
    result_id: str

    def __post_init__(self) -> None:
        if self.object is None:
            _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/bindings/object")
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/bindings/node_id"))
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/bindings/result_id"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignPatternExecutionBindings:
    """Host-authenticated semantic-id to live-object map for one exact plan."""

    document: object
    body: object
    body_id: str
    base: AuthenticatedPatternObject
    source_feature: AuthenticatedPatternObject

    def __post_init__(self) -> None:
        if self.document is None or self.body is None:
            _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/bindings")
        object.__setattr__(self, "body_id", _identifier(self.body_id, "/bindings/body_id"))
        if type(self.base) is not AuthenticatedPatternObject:
            _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/bindings/base")
        if type(self.source_feature) is not AuthenticatedPatternObject:
            _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/bindings/source_feature")


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignPatternConformanceReceipt:
    plan_sha256: str
    operation: PartDesignPatternOperation
    object_name: str
    before_volume_mm3: float
    after_volume_mm3: float
    receipt_id: str = field(init=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/plan_sha256"))
        if type(self.operation) is not PartDesignPatternOperation:
            _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/operation")
        object.__setattr__(self, "object_name", _identifier(self.object_name, "/object_name"))
        before = _finite_float(
            self.before_volume_mm3,
            "/receipt/before_volume_mm3",
            code=PartDesignPatternRuleErrorCode.CONFORMANCE_FAILED,
        )
        after = _finite_float(
            self.after_volume_mm3,
            "/receipt/after_volume_mm3",
            code=PartDesignPatternRuleErrorCode.CONFORMANCE_FAILED,
        )
        epsilon = max(1e-9, before * 1e-12)
        if before <= 0.0 or after <= 0.0 or abs(after - before) <= epsilon:
            _fail(PartDesignPatternRuleErrorCode.CONFORMANCE_FAILED, "/receipt")
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
        object.__setattr__(self, "receipt_id", f"partdesign_pattern_{digest[:32]}")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _shape_volume(shape: object, path: str) -> float:
    try:
        if shape is None or shape.isNull() or not shape.isValid() or len(shape.Solids) != 1:
            _fail(PartDesignPatternRuleErrorCode.PRECONDITION_FAILED, path)
        volume = float(shape.Volume)
    except PartDesignPatternRuleError:
        raise
    except Exception:
        _fail(PartDesignPatternRuleErrorCode.PRECONDITION_FAILED, path)
    if not math.isfinite(volume) or volume <= 0.0:
        _fail(PartDesignPatternRuleErrorCode.PRECONDITION_FAILED, path)
    return volume


def _same_selection(
    semantic: PatternObjectSelection, authenticated: AuthenticatedPatternObject
) -> bool:
    return (
        semantic.node_id == authenticated.node_id and semantic.result_id == authenticated.result_id
    )


def _validate_bindings(
    plan: PartDesignPatternBackendPlan,
    bindings: PartDesignPatternExecutionBindings,
) -> tuple[float, tuple[object, ...], object, object]:
    if (
        bindings.body_id != plan.body_id
        or not _same_selection(plan.base, bindings.base)
        or not _same_selection(plan.source_feature, bindings.source_feature)
    ):
        _fail(PartDesignPatternRuleErrorCode.INTEGRITY_FAILURE, "/bindings/identity")
    document, body = bindings.document, bindings.body
    base, source = bindings.base.object, bindings.source_feature.object
    try:
        group = tuple(body.Group)
        if (
            getattr(document, "UndoMode", 0) != 1
            or bool(document.HasPendingTransaction)
            or body.Document is not document
            or body.TypeId != "PartDesign::Body"
            or base.Document is not document
            or source.Document is not document
            or base not in group
            or source not in group
            or body.Tip is not base
            or not str(source.TypeId).startswith("PartDesign::")
            or not source.isValid()
        ):
            _fail(PartDesignPatternRuleErrorCode.PRECONDITION_FAILED, "/bindings")
    except PartDesignPatternRuleError:
        raise
    except Exception:
        _fail(PartDesignPatternRuleErrorCode.PRECONDITION_FAILED, "/bindings")
    before_volume = _shape_volume(base.Shape, "/bindings/base")
    _shape_volume(source.Shape, "/bindings/source_feature")
    return before_volume, group, base, source


def _origin_object(body: object, token: str) -> object:
    try:
        candidates = tuple(
            item for item in body.Origin.OriginFeatures if str(getattr(item, "Role", "")) == token
        )
    except Exception:
        _fail(PartDesignPatternRuleErrorCode.PRECONDITION_FAILED, "/reference")
    if (
        len(candidates) != 1
        or candidates[0].Document is not body.Document
        or candidates[0].TypeId not in {"App::Line", "App::Plane"}
    ):
        _fail(PartDesignPatternRuleErrorCode.PRECONDITION_FAILED, "/reference")
    return candidates[0]


def _verify_reference(feature: object, property_name: str, expected: object) -> None:
    try:
        reference, subelements = getattr(feature, property_name)
        if reference is not expected or tuple(subelements) not in {(), ("",)}:
            _fail(PartDesignPatternRuleErrorCode.CONFORMANCE_FAILED, "/result/reference")
    except PartDesignPatternRuleError:
        raise
    except Exception:
        _fail(PartDesignPatternRuleErrorCode.CONFORMANCE_FAILED, "/result/reference")


def apply_partdesign_pattern_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: PartDesignPatternExecutionBindings,
) -> PartDesignPatternConformanceReceipt:
    """Explicit trusted-host action; validate exact bytes before native mutation."""

    if type(bindings) is not PartDesignPatternExecutionBindings:
        _fail(PartDesignPatternRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(PartDesignPatternRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != PARTDESIGN_PATTERN_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(PartDesignPatternRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_partdesign_pattern_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    before_volume, before_group, base, source = _validate_bindings(plan, bindings)
    document, body = bindings.document, bindings.body
    spec = _NATIVE_PATTERN_SPECS[plan.operation]
    object_name = f"{spec.object_prefix}_{plan.plan_sha256[:16]}"
    native_token = (
        _NATIVE_PLANE_OBJECTS[plan.plane]
        if plan.operation is PartDesignPatternOperation.MIRRORED
        else _NATIVE_AXIS_OBJECTS[plan.axis]
    )
    origin = _origin_object(body, native_token)
    try:
        if document.getObject(object_name) is not None:
            _fail(PartDesignPatternRuleErrorCode.PRECONDITION_FAILED, "/document/object_name")
        before_objects = tuple(document.Objects)
        before_tip = body.Tip
        before_visibilities = tuple(bool(item.Visibility) for item in before_group)
    except PartDesignPatternRuleError:
        raise
    except Exception:
        _fail(PartDesignPatternRuleErrorCode.PRECONDITION_FAILED, "/document")

    transaction_open = False
    try:
        document.openTransaction("VibeCAD trusted PartDesign pattern batch")
        transaction_open = True
        feature = body.newObject(spec.type_id, object_name)
        feature.Originals = [source]
        setattr(feature, spec.reference_property, (origin, [""]))
        if plan.operation is PartDesignPatternOperation.LINEAR_PATTERN:
            feature.Occurrences = plan.occurrences
            feature.Length = plan.span_mm
            feature.Reversed = plan.reversed
        elif plan.operation is PartDesignPatternOperation.POLAR_PATTERN:
            feature.Occurrences = plan.occurrences
            feature.Angle = plan.angle_degrees
            feature.Reversed = plan.reversed
        body.Tip = feature
        document.recompute()
        after_volume = _shape_volume(feature.Shape, "/result/shape")
        try:
            if (
                feature.TypeId != spec.type_id
                or not feature.isValid()
                or tuple(feature.State) != ("Up-to-date",)
                or body.Tip is not feature
                or feature.BaseFeature is not base
                or tuple(feature.Originals) != (source,)
            ):
                _fail(PartDesignPatternRuleErrorCode.CONFORMANCE_FAILED, "/result")
        except PartDesignPatternRuleError:
            raise
        except Exception:
            _fail(PartDesignPatternRuleErrorCode.CONFORMANCE_FAILED, "/result")
        _verify_reference(feature, spec.reference_property, origin)
        if plan.operation is PartDesignPatternOperation.LINEAR_PATTERN:
            if (
                int(feature.Occurrences) != plan.occurrences
                or abs(float(feature.Length) - plan.span_mm) > 1e-9
                or bool(feature.Reversed) is not plan.reversed
            ):
                _fail(PartDesignPatternRuleErrorCode.CONFORMANCE_FAILED, "/result/parameters")
        elif plan.operation is PartDesignPatternOperation.POLAR_PATTERN:
            if (
                int(feature.Occurrences) != plan.occurrences
                or abs(float(feature.Angle) - plan.angle_degrees) > 1e-9
                or bool(feature.Reversed) is not plan.reversed
            ):
                _fail(PartDesignPatternRuleErrorCode.CONFORMANCE_FAILED, "/result/parameters")
        epsilon = max(1e-9, before_volume * 1e-12)
        if abs(after_volume - before_volume) <= epsilon:
            _fail(PartDesignPatternRuleErrorCode.CONFORMANCE_FAILED, "/result/volume")
        document.commitTransaction()
        transaction_open = False
    except BaseException as error:
        if transaction_open:
            try:
                document.abortTransaction()
                document.recompute()
            except BaseException:
                _fail(PartDesignPatternRuleErrorCode.TRANSACTION_FAILED, "/transaction/rollback")
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
                or bool(document.HasPendingTransaction)
            ):
                _fail(PartDesignPatternRuleErrorCode.TRANSACTION_FAILED, "/transaction/rollback")
        except PartDesignPatternRuleError:
            raise
        except Exception:
            _fail(PartDesignPatternRuleErrorCode.TRANSACTION_FAILED, "/transaction/rollback")
        if isinstance(error, KeyboardInterrupt):
            raise
        if isinstance(error, PartDesignPatternRuleError):
            raise error
        _fail(PartDesignPatternRuleErrorCode.TRANSACTION_FAILED, "/transaction/apply")

    return PartDesignPatternConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        object_name=object_name,
        before_volume_mm3=before_volume,
        after_volume_mm3=after_volume,
    )


__all__ = [
    "MAX_PARTDESIGN_PATTERN_OCCURRENCES",
    "MAX_PARTDESIGN_PATTERN_PLAN_BYTES",
    "PARTDESIGN_PATTERN_FREECAD_ENGINE_BUILD_ID",
    "PARTDESIGN_PATTERN_PLAN_MEDIA_TYPE",
    "PARTDESIGN_PATTERN_PLAN_SCHEMA_VERSION",
    "PARTDESIGN_PATTERN_RULE_CONTRACT_SHA256",
    "PARTDESIGN_PATTERN_RULE_ID",
    "AuthenticatedPatternObject",
    "PartDesignPatternBackendPlan",
    "PartDesignPatternConformanceReceipt",
    "PartDesignPatternExecutionBindings",
    "PartDesignPatternOperation",
    "PartDesignPatternRuleError",
    "PartDesignPatternRuleErrorCode",
    "PatternObjectSelection",
    "PatternOriginAxis",
    "PatternOriginPlane",
    "apply_partdesign_pattern_plan",
    "decode_partdesign_pattern_backend_plan",
]
