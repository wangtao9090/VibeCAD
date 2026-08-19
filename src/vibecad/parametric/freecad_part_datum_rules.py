"""Trusted FreeCAD rules for four root-level Part datum objects.

The canonical plan carries backend-neutral datum intent and explicit placement.
It grants no execution authority.  This module owns the reviewed mapping to
FreeCAD 1.1.0 native types and executes only against an authenticated document
inside the shared rollback-proven transaction boundary.
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

PART_DATUM_PLAN_SCHEMA_VERSION: Final = 1
PART_DATUM_PLAN_MEDIA_TYPE: Final = "application/vnd.vibecad.freecad-part-datum-plan+json"
MAX_PART_DATUM_PLAN_BYTES: Final = 24 * 1024
PART_DATUM_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-part-datum-plan.v1\0"
_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-part-datum-rule.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-part-datum-receipt.v1\0"


class PartDatumOperation(StrEnum):
    DATUM_LINE = "datum_line"
    DATUM_PLANE = "datum_plane"
    DATUM_POINT = "datum_point"
    LOCAL_COORDINATE_SYSTEM = "local_coordinate_system"


@dataclass(frozen=True, slots=True)
class _NativeDatumSpec:
    type_id: str
    object_prefix: str
    properties: tuple[str, ...]


_NATIVE_DATUM_SPECS: Final = {
    PartDatumOperation.DATUM_LINE: _NativeDatumSpec(
        "Part::DatumLine",
        "DatumLine",
        ("AttachmentSupport", "MapMode", "Placement"),
    ),
    PartDatumOperation.DATUM_PLANE: _NativeDatumSpec(
        "Part::DatumPlane",
        "DatumPlane",
        ("AttachmentSupport", "MapMode", "Placement"),
    ),
    PartDatumOperation.DATUM_POINT: _NativeDatumSpec(
        "Part::DatumPoint",
        "DatumPoint",
        ("AttachmentSupport", "MapMode", "Placement"),
    ),
    PartDatumOperation.LOCAL_COORDINATE_SYSTEM: _NativeDatumSpec(
        "Part::LocalCoordinateSystem",
        "LocalCoordinateSystem",
        ("AttachmentSupport", "Group", "MapMode", "OriginFeatures", "Placement"),
    ),
}

PART_DATUM_NATIVE_TYPE_IDS: Final = {
    operation: spec.type_id for operation, spec in _NATIVE_DATUM_SPECS.items()
}
PART_DATUM_NATIVE_PROPERTIES: Final = {
    operation: spec.properties for operation, spec in _NATIVE_DATUM_SPECS.items()
}

_LCS_HELPERS: Final = (
    ("X_Axis", "App::Line"),
    ("Y_Axis", "App::Line"),
    ("Z_Axis", "App::Line"),
    ("XY_Plane", "App::Plane"),
    ("XZ_Plane", "App::Plane"),
    ("YZ_Plane", "App::Plane"),
    ("Origin", "App::Point"),
)

PART_DATUM_RULE_ID: Final = "freecad.part.datum-family.v1"
_NATIVE_CONTRACT = (
    f"engine=FreeCAD-1.1.0/{PART_DATUM_FREECAD_ENGINE_BUILD_ID};"
    "ops=datum-line:Part::DatumLine,datum-plane:Part::DatumPlane,"
    "datum-point:Part::DatumPoint,local-coordinate-system:Part::LocalCoordinateSystem;"
    "placement=explicit;MapMode=Deactivated;AttachmentSupport=empty;"
    "ownership=document-root;parents=empty;"
    "lcs-origin-features=X_Axis:App::Line,Y_Axis:App::Line,Z_Axis:App::Line,"
    "XY_Plane:App::Plane,XZ_Plane:App::Plane,YZ_Plane:App::Plane,Origin:App::Point;"
    "lcs-helpers=generated-owned-not-capabilities;transaction=shared-rollback"
)
PART_DATUM_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN + _NATIVE_CONTRACT.encode("ascii")
).hexdigest()


class PartDatumRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class PartDatumRuleError(ValueError):
    """Bounded failure from the reviewed Part datum native boundary."""

    def __init__(self, code: PartDatumRuleErrorCode, path: str = "/") -> None:
        if type(code) is not PartDatumRuleErrorCode:
            raise TypeError("code must be an exact PartDatumRuleErrorCode")
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
        super().__init__(f"Part datum rule error ({code.value}) at {path}")


def _fail(code: PartDatumRuleErrorCode, path: str) -> None:
    raise PartDatumRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(PartDatumRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(PartDatumRuleErrorCode.INVALID_INPUT, path)
    return value


def _finite(
    value: object,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if type(value) not in {int, float}:
        _fail(PartDatumRuleErrorCode.INVALID_INPUT, path)
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        _fail(PartDatumRuleErrorCode.INVALID_INPUT, path)
    if (
        not math.isfinite(result)
        or (minimum is not None and result < minimum)
        or (maximum is not None and result > maximum)
    ):
        _fail(PartDatumRuleErrorCode.INVALID_INPUT, path)
    return result


def _canonical_json(value: object) -> bytes:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/")
    if not payload or len(payload) > MAX_PART_DATUM_PLAN_BYTES:
        _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/")
    return payload


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
    if type(raw) is not bytes or not raw or len(raw) > MAX_PART_DATUM_PLAN_BYTES:
        _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(PartDatumRuleErrorCode.INTEGRITY_FAILURE, "/")
    except (UnicodeError, ValueError, RecursionError):
        _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/")
    if type(value) is not dict or not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(PartDatumRuleErrorCode.INTEGRITY_FAILURE, "/")
    return value


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(PartDatumRuleErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class ExplicitDatumPlacement:
    position_mm: tuple[float, float, float]
    axis: tuple[float, float, float]
    angle_degrees: float

    def __post_init__(self) -> None:
        if type(self.position_mm) is not tuple or len(self.position_mm) != 3:
            _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/placement/position_mm")
        if type(self.axis) is not tuple or len(self.axis) != 3:
            _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/placement/axis")
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
            _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/placement/axis")
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
    def from_mapping(cls, value: object, path: str) -> ExplicitDatumPlacement:
        fields = _exact_fields(value, {"position_mm", "axis", "angle_degrees"}, path)
        if type(fields["position_mm"]) is not list or type(fields["axis"]) is not list:
            _fail(PartDatumRuleErrorCode.INVALID_INPUT, path)
        return cls(
            position_mm=tuple(fields["position_mm"]),
            axis=tuple(fields["axis"]),
            angle_degrees=fields["angle_degrees"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDatumBackendPlan:
    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    manifest_sha256: str
    container_id: str
    node_id: str
    result_id: str
    operation: PartDatumOperation
    placement: ExplicitDatumPlacement
    schema_version: int = PART_DATUM_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/schema_version")
        for name in (
            "source_artifact_id",
            "source_graph_id",
            "container_id",
            "node_id",
            "result_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in (
            "source_graph_sha256",
            "source_content_sha256",
            "lowering_request_sha256",
            "adapter_contract_sha256",
            "manifest_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if type(self.operation) is not PartDatumOperation:
            _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/operation")
        if type(self.placement) is not ExplicitDatumPlacement:
            _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/placement")

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
                "engine_build_id": PART_DATUM_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": PART_DATUM_RULE_ID,
                "rule_contract_sha256": PART_DATUM_RULE_CONTRACT_SHA256,
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
                "container_id": self.container_id,
                "node_id": self.node_id,
                "result_id": self.result_id,
            },
            "operation": {
                "kind": self.operation.value,
                "placement": self.placement.to_mapping(),
            },
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_PLAN_DIGEST_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> PartDatumBackendPlan:
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
            root["rule"],
            {"rule_id", "rule_contract_sha256", "manifest_sha256"},
            "/rule",
        )
        source = _exact_fields(
            root["source"],
            {"artifact_id", "graph_id", "graph_sha256", "content_sha256"},
            "/source",
        )
        binding = _exact_fields(
            root["binding"],
            {"lowering_request_sha256", "adapter_contract_sha256"},
            "/binding",
        )
        selection = _exact_fields(
            root["selection"], {"container_id", "node_id", "result_id"}, "/selection"
        )
        operation = _exact_fields(root["operation"], {"kind", "placement"}, "/operation")
        if (
            root["authority"] != "none"
            or backend
            != {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PART_DATUM_FREECAD_ENGINE_BUILD_ID,
            }
            or rule["rule_id"] != PART_DATUM_RULE_ID
            or rule["rule_contract_sha256"] != PART_DATUM_RULE_CONTRACT_SHA256
        ):
            _fail(PartDatumRuleErrorCode.INTEGRITY_FAILURE, "/contract")
        try:
            kind = PartDatumOperation(operation["kind"])
        except (TypeError, ValueError):
            _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/operation/kind")
        return cls(
            schema_version=root["schema_version"],
            source_artifact_id=source["artifact_id"],
            source_graph_id=source["graph_id"],
            source_graph_sha256=source["graph_sha256"],
            source_content_sha256=source["content_sha256"],
            lowering_request_sha256=binding["lowering_request_sha256"],
            adapter_contract_sha256=binding["adapter_contract_sha256"],
            manifest_sha256=rule["manifest_sha256"],
            container_id=selection["container_id"],
            node_id=selection["node_id"],
            result_id=selection["result_id"],
            operation=kind,
            placement=ExplicitDatumPlacement.from_mapping(
                operation["placement"], "/operation/placement"
            ),
        )


def decode_part_datum_backend_plan(
    raw: bytes,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> PartDatumBackendPlan:
    plan = PartDatumBackendPlan.from_mapping(_decode_mapping(raw))
    if not hmac.compare_digest(raw, plan.canonical_bytes):
        _fail(PartDatumRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(),
        _digest(expected_content_sha256, "/expected_content_sha256"),
    ):
        _fail(PartDatumRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        plan.plan_sha256,
        _digest(expected_plan_sha256, "/expected_plan_sha256"),
    ):
        _fail(PartDatumRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return plan


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDatumExecutionBindings:
    document: object
    container_id: str

    def __post_init__(self) -> None:
        if self.document is None:
            _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/bindings/document")
        object.__setattr__(
            self,
            "container_id",
            _identifier(self.container_id, "/bindings/container_id"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDatumConformanceReceipt:
    plan_sha256: str
    operation: PartDatumOperation
    object_name: str
    native_type_id: str
    owned_object_names: tuple[str, ...]
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/plan_sha256"))
        if type(self.operation) is not PartDatumOperation:
            _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/operation")
        object.__setattr__(self, "object_name", _identifier(self.object_name, "/object_name"))
        if self.native_type_id != PART_DATUM_NATIVE_TYPE_IDS[self.operation]:
            _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/native_type_id")
        if (
            type(self.owned_object_names) is not tuple
            or not self.owned_object_names
            or len(self.owned_object_names) > 8
        ):
            _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/owned_object_names")
        checked = tuple(
            _identifier(item, f"/owned_object_names/{index}")
            for index, item in enumerate(self.owned_object_names)
        )
        if len(set(checked)) != len(checked) or checked[0] != self.object_name:
            _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/owned_object_names")
        expected_count = 8 if self.operation is PartDatumOperation.LOCAL_COORDINATE_SYSTEM else 1
        if len(checked) != expected_count:
            _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/owned_object_names")
        object.__setattr__(self, "owned_object_names", checked)
        body = {
            "plan_sha256": self.plan_sha256,
            "operation": self.operation.value,
            "object_name": self.object_name,
            "native_type_id": self.native_type_id,
            "owned_object_names": list(self.owned_object_names),
        }
        object.__setattr__(
            self,
            "receipt_sha256",
            hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest(),
        )

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _same_identity_sequence(left: object, right: tuple[object, ...]) -> bool:
    try:
        values = tuple(left)
    except Exception:
        return False
    return len(values) == len(right) and all(
        actual is expected for actual, expected in zip(values, right, strict=True)
    )


def _snapshot(document: object):
    objects = tuple(document.Objects)
    groups = tuple(
        (item, tuple(item.Group)) for item in objects if "Group" in tuple(item.PropertiesList)
    )
    visibility = tuple(
        (item, bool(item.Visibility))
        for item in objects
        if "Visibility" in tuple(item.PropertiesList)
    )
    body_tips = tuple((item, item.Tip) for item in objects if item.TypeId == "PartDesign::Body")
    return objects, groups, visibility, body_tips


def _rollback_matches(document: object, before: object) -> bool:
    objects, groups, visibility, body_tips = before
    if not _same_identity_sequence(document.Objects, objects):
        return False
    for item, members in groups:
        if not _same_identity_sequence(item.Group, members):
            return False
    try:
        return all(bool(item.Visibility) is value for item, value in visibility) and all(
            item.Tip is tip for item, tip in body_tips
        )
    except Exception:
        return False


def _same_body_tips(
    objects: tuple[object, ...],
    expected: tuple[tuple[object, object], ...],
) -> bool:
    try:
        actual = tuple((item, item.Tip) for item in objects if item.TypeId == "PartDesign::Body")
    except Exception:
        return False
    return len(actual) == len(expected) and all(
        actual_body is expected_body and actual_tip is expected_tip
        for (actual_body, actual_tip), (expected_body, expected_tip) in zip(
            actual, expected, strict=True
        )
    )


def _placement_matches(actual: object, expected: ExplicitDatumPlacement) -> bool:
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
        same = all(
            abs(actual_value - expected_value) <= 1e-9
            for actual_value, expected_value in zip(quaternion, expected_quaternion, strict=True)
        )
        negated = all(
            abs(actual_value + expected_value) <= 1e-9
            for actual_value, expected_value in zip(quaternion, expected_quaternion, strict=True)
        )
        return all(
            abs(position[index] - expected.position_mm[index]) <= 1e-9 for index in range(3)
        ) and (same or negated)
    except Exception:
        return False


def _validate_root_ownership(feature: object) -> None:
    try:
        parents = feature.getParentGroup()
        if parents not in (None, []) and tuple(parents):
            _fail(PartDatumRuleErrorCode.CONFORMANCE_FAILED, "/result/ownership")
    except PartDatumRuleError:
        raise
    except Exception:
        _fail(PartDatumRuleErrorCode.CONFORMANCE_FAILED, "/result/ownership")


def _validate_lcs_helpers(feature: object, created: tuple[object, ...]) -> tuple[object, ...]:
    try:
        helpers = tuple(feature.OriginFeatures)
        if tuple(feature.Group) or len(helpers) != len(_LCS_HELPERS):
            _fail(PartDatumRuleErrorCode.CONFORMANCE_FAILED, "/result/origin_features")
        if (
            len(created) != 8
            or created[0] is not feature
            or not _same_identity_sequence(created[1:], helpers)
        ):
            _fail(PartDatumRuleErrorCode.CONFORMANCE_FAILED, "/result/ownership")
        for helper, (role, type_id) in zip(helpers, _LCS_HELPERS, strict=True):
            if (
                helper.TypeId != type_id
                or helper.Role != role
                or helper.Document is not feature.Document
                or tuple(helper.InList) != (feature,)
                or not helper.isValid()
                or tuple(helper.State) != ("Up-to-date",)
            ):
                _fail(PartDatumRuleErrorCode.CONFORMANCE_FAILED, "/result/origin_features")
            _validate_root_ownership(helper)
        return helpers
    except PartDatumRuleError:
        raise
    except Exception:
        _fail(PartDatumRuleErrorCode.CONFORMANCE_FAILED, "/result/origin_features")


def apply_part_datum_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: PartDatumExecutionBindings,
) -> PartDatumConformanceReceipt:
    """Execute one exact reviewed root-level datum plan."""

    if type(bindings) is not PartDatumExecutionBindings:
        _fail(PartDatumRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(PartDatumRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != PART_DATUM_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(PartDatumRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_part_datum_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    if bindings.container_id != plan.container_id:
        _fail(PartDatumRuleErrorCode.PRECONDITION_FAILED, "/bindings/container_id")
    document = bindings.document
    spec = _NATIVE_DATUM_SPECS[plan.operation]
    object_name = f"{spec.object_prefix}_{plan.plan_sha256[:16]}"
    try:
        if document.getObject(object_name) is not None or bool(document.HasPendingTransaction):
            _fail(PartDatumRuleErrorCode.PRECONDITION_FAILED, "/document")
        before_objects = tuple(document.Objects)
        body_tips = tuple(
            (item, item.Tip) for item in before_objects if item.TypeId == "PartDesign::Body"
        )
    except PartDatumRuleError:
        raise
    except Exception:
        _fail(PartDatumRuleErrorCode.PRECONDITION_FAILED, "/document")

    owned: tuple[object, ...] = ()

    def apply() -> object:
        nonlocal owned
        feature = document.addObject(spec.type_id, object_name)
        feature.MapMode = "Deactivated"
        feature.AttachmentSupport = []
        placement = plan.placement
        feature.Placement = FreeCAD.Placement(
            FreeCAD.Vector(*placement.position_mm),
            FreeCAD.Rotation(FreeCAD.Vector(*placement.axis), placement.angle_degrees),
        )
        document.recompute()
        after_objects = tuple(document.Objects)
        if not _same_identity_sequence(
            after_objects[: len(before_objects)], before_objects
        ) or not _same_body_tips(after_objects, body_tips):
            _fail(PartDatumRuleErrorCode.CONFORMANCE_FAILED, "/result/ownership")
        owned = after_objects[len(before_objects) :]
        try:
            if (
                not owned
                or owned[0] is not feature
                or document.getObject(object_name) is not feature
                or feature.TypeId != spec.type_id
                or feature.Document is not document
                or not feature.isValid()
                or tuple(feature.State) != ("Up-to-date",)
                or feature.MapMode != "Deactivated"
                or tuple(feature.AttachmentSupport) != ()
                or not _placement_matches(feature.Placement, placement)
            ):
                _fail(PartDatumRuleErrorCode.CONFORMANCE_FAILED, "/result")
            _validate_root_ownership(feature)
            if plan.operation is PartDatumOperation.LOCAL_COORDINATE_SYSTEM:
                _validate_lcs_helpers(feature, owned)
            elif len(owned) != 1:
                _fail(PartDatumRuleErrorCode.CONFORMANCE_FAILED, "/result/ownership")
        except PartDatumRuleError:
            raise
        except Exception:
            _fail(PartDatumRuleErrorCode.CONFORMANCE_FAILED, "/result")
        return feature

    try:
        NativeTransactionRunner().run(
            document,
            label="VibeCAD trusted Part datum family",
            snapshot=lambda: _snapshot(document),
            apply=apply,
            rollback_matches=lambda before: _rollback_matches(document, before),
        )
    except NativeTransactionError as error:
        _fail(PartDatumRuleErrorCode.TRANSACTION_FAILED, error.path)
    return PartDatumConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        object_name=object_name,
        native_type_id=spec.type_id,
        owned_object_names=tuple(item.Name for item in owned),
    )


__all__ = [
    "MAX_PART_DATUM_PLAN_BYTES",
    "PART_DATUM_FREECAD_ENGINE_BUILD_ID",
    "PART_DATUM_NATIVE_PROPERTIES",
    "PART_DATUM_NATIVE_TYPE_IDS",
    "PART_DATUM_PLAN_MEDIA_TYPE",
    "PART_DATUM_PLAN_SCHEMA_VERSION",
    "PART_DATUM_RULE_CONTRACT_SHA256",
    "PART_DATUM_RULE_ID",
    "ExplicitDatumPlacement",
    "PartDatumBackendPlan",
    "PartDatumConformanceReceipt",
    "PartDatumExecutionBindings",
    "PartDatumOperation",
    "PartDatumRuleError",
    "PartDatumRuleErrorCode",
    "apply_part_datum_plan",
    "decode_part_datum_backend_plan",
]
