"""Private FreeCAD PartDesign rules for trusted, content-bound backend plans.

The wire-facing parametric graph never selects a FreeCAD ``TypeId`` or property
name.  This module is the reviewed native-code boundary: it accepts only one
canonical Groove plan whose rule digest commits to every native spelling below.
Importing the module does not import FreeCAD and neither plans nor receipts grant
execution authority.  A trusted host must explicitly call :func:`apply_groove_plan`.
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

GROOVE_PLAN_SCHEMA_VERSION: Final = 1
GROOVE_PLAN_MEDIA_TYPE: Final = "application/vnd.vibecad.freecad-groove-plan+json"
MAX_GROOVE_PLAN_BYTES: Final = 16 * 1024
GROOVE_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-groove-plan.v1\0"
_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-groove-rule.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-groove-conformance-receipt.v1\0"

GROOVE_RULE_ID: Final = "freecad.partdesign.groove.angle.v1"
_NATIVE_CONTRACT = (
    f"engine=FreeCAD-1.1.0/{GROOVE_FREECAD_ENGINE_BUILD_ID};"
    "type_id=PartDesign::Groove;profile=Profile;"
    "axis=ReferenceAxis:Sketcher::SketchObject/V_Axis;base=BaseFeature;"
    "mode=Type:Angle;angle=Angle;angle2=0;midplane=false;reversed=bounded-bool;"
    "refine=true;allow_multi_face=false;body_tip=true;single_solid=true;"
    "strict_volume_decrease=true;transaction=rollback"
)
GROOVE_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN + _NATIVE_CONTRACT.encode("ascii")
).hexdigest()


class GrooveRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class GrooveRuleError(ValueError):
    """Bounded, non-reflective failure from the trusted native rule."""

    def __init__(self, code: GrooveRuleErrorCode, path: str = "/") -> None:
        if type(code) is not GrooveRuleErrorCode:
            raise TypeError("code must be a GrooveRuleErrorCode")
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
        super().__init__(f"groove rule error ({code.value}) at {path}")


def _fail(code: GrooveRuleErrorCode, path: str) -> None:
    raise GrooveRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(GrooveRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(GrooveRuleErrorCode.INVALID_INPUT, path)
    return value


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
        _fail(GrooveRuleErrorCode.INVALID_INPUT, "/")
    if not raw or len(raw) > MAX_GROOVE_PLAN_BYTES:
        _fail(GrooveRuleErrorCode.INVALID_INPUT, "/")
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
    if type(raw) is not bytes or not raw or len(raw) > MAX_GROOVE_PLAN_BYTES:
        _fail(GrooveRuleErrorCode.INVALID_INPUT, "/")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(GrooveRuleErrorCode.INTEGRITY_FAILURE, "/")
    except (UnicodeError, ValueError, RecursionError):
        _fail(GrooveRuleErrorCode.INVALID_INPUT, "/")
    if type(value) is not dict or not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(GrooveRuleErrorCode.INTEGRITY_FAILURE, "/")
    return value


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(GrooveRuleErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class GrooveBackendPlan:
    """Canonical, authority-free input to the single reviewed Groove rule."""

    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    body_id: str
    node_id: str
    result_id: str
    base_node_id: str
    base_result_id: str
    profile_node_id: str
    profile_result_id: str
    axis_reference_id: str
    axis_result_id: str
    angle_degrees: float
    reversed: bool
    schema_version: int = GROOVE_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(GrooveRuleErrorCode.INVALID_INPUT, "/schema_version")
        for name in (
            "source_artifact_id",
            "source_graph_id",
            "body_id",
            "node_id",
            "result_id",
            "base_node_id",
            "base_result_id",
            "profile_node_id",
            "profile_result_id",
            "axis_reference_id",
            "axis_result_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in (
            "source_graph_sha256",
            "source_content_sha256",
            "lowering_request_sha256",
            "adapter_contract_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if (
            type(self.angle_degrees) not in {int, float}
            or not math.isfinite(self.angle_degrees)
            or not 0.0 < float(self.angle_degrees) <= 360.0
        ):
            _fail(GrooveRuleErrorCode.INVALID_INPUT, "/angle_degrees")
        object.__setattr__(self, "angle_degrees", float(self.angle_degrees))
        if type(self.reversed) is not bool:
            _fail(GrooveRuleErrorCode.INVALID_INPUT, "/reversed")
        if self.node_id in {self.base_node_id, self.profile_node_id}:
            _fail(GrooveRuleErrorCode.INVALID_INPUT, "/node_id")

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
                "engine_build_id": GROOVE_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": GROOVE_RULE_ID,
                "rule_contract_sha256": GROOVE_RULE_CONTRACT_SHA256,
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
                "base_node_id": self.base_node_id,
                "base_result_id": self.base_result_id,
                "profile_node_id": self.profile_node_id,
                "profile_result_id": self.profile_result_id,
                "axis_reference_id": self.axis_reference_id,
                "axis_result_id": self.axis_result_id,
            },
            "operation": {
                "type": "Angle",
                "angle_degrees": self.angle_degrees,
                "angle2_degrees": 0.0,
                "axis_locator": "V_Axis",
                "midplane": False,
                "reversed": self.reversed,
                "refine": True,
                "allow_multi_face": False,
            },
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_PLAN_DIGEST_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> GrooveBackendPlan:
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
            root["backend"],
            {"engine", "engine_version", "engine_build_id"},
            "/backend",
        )
        rule = _exact_fields(root["rule"], {"rule_id", "rule_contract_sha256"}, "/rule")
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
            root["selection"],
            {
                "body_id",
                "node_id",
                "result_id",
                "base_node_id",
                "base_result_id",
                "profile_node_id",
                "profile_result_id",
                "axis_reference_id",
                "axis_result_id",
            },
            "/selection",
        )
        operation = _exact_fields(
            root["operation"],
            {
                "type",
                "angle_degrees",
                "angle2_degrees",
                "axis_locator",
                "midplane",
                "reversed",
                "refine",
                "allow_multi_face",
            },
            "/operation",
        )
        if (
            root["authority"] != "none"
            or backend
            != {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": GROOVE_FREECAD_ENGINE_BUILD_ID,
            }
            or rule
            != {"rule_id": GROOVE_RULE_ID, "rule_contract_sha256": GROOVE_RULE_CONTRACT_SHA256}
            or operation.get("type") != "Angle"
            or operation.get("angle2_degrees") != 0.0
            or operation.get("axis_locator") != "V_Axis"
            or operation.get("midplane") is not False
            or operation.get("refine") is not True
            or operation.get("allow_multi_face") is not False
        ):
            _fail(GrooveRuleErrorCode.INTEGRITY_FAILURE, "/contract")
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
            base_node_id=selection["base_node_id"],
            base_result_id=selection["base_result_id"],
            profile_node_id=selection["profile_node_id"],
            profile_result_id=selection["profile_result_id"],
            axis_reference_id=selection["axis_reference_id"],
            axis_result_id=selection["axis_result_id"],
            angle_degrees=operation["angle_degrees"],
            reversed=operation["reversed"],
        )


def decode_groove_backend_plan(
    raw: object,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> GrooveBackendPlan:
    if expected_content_sha256 is not None:
        expected_content_sha256 = _digest(expected_content_sha256, "/expected_content_sha256")
    if expected_plan_sha256 is not None:
        expected_plan_sha256 = _digest(expected_plan_sha256, "/expected_plan_sha256")
    mapping = _decode_mapping(raw)
    result = GrooveBackendPlan.from_mapping(mapping)
    if type(raw) is not bytes or not hmac.compare_digest(raw, result.canonical_bytes):
        _fail(GrooveRuleErrorCode.INTEGRITY_FAILURE, "/")
    content_sha256 = hashlib.sha256(raw).hexdigest()
    if expected_content_sha256 is not None and not hmac.compare_digest(
        content_sha256, expected_content_sha256
    ):
        _fail(GrooveRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        result.plan_sha256, expected_plan_sha256
    ):
        _fail(GrooveRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class GrooveExecutionBindings:
    """Host-authenticated mapping from semantic plan ids to live FreeCAD objects."""

    document: object
    body: object
    base_feature: object
    profile: object
    body_id: str
    base_node_id: str
    base_result_id: str
    profile_node_id: str
    profile_result_id: str

    def __post_init__(self) -> None:
        for name in (
            "body_id",
            "base_node_id",
            "base_result_id",
            "profile_node_id",
            "profile_result_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        if any(
            getattr(self, name) is None
            for name in ("document", "body", "base_feature", "profile")
        ):
            _fail(GrooveRuleErrorCode.INVALID_INPUT, "/bindings")


@dataclass(frozen=True, slots=True, kw_only=True)
class GrooveConformanceReceipt:
    plan_sha256: str
    object_name: str
    before_volume_mm3: float
    after_volume_mm3: float
    reversed: bool
    receipt_id: str = field(init=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/plan_sha256"))
        object.__setattr__(self, "object_name", _identifier(self.object_name, "/object_name"))
        if (
            type(self.before_volume_mm3) not in {int, float}
            or type(self.after_volume_mm3) not in {int, float}
            or not math.isfinite(self.before_volume_mm3)
            or not math.isfinite(self.after_volume_mm3)
            or not 0.0 < self.after_volume_mm3 < self.before_volume_mm3
            or type(self.reversed) is not bool
        ):
            _fail(GrooveRuleErrorCode.CONFORMANCE_FAILED, "/receipt")
        body = {
            "authority": "none",
            "plan_sha256": self.plan_sha256,
            "object_name": self.object_name,
            "before_volume_mm3": float(self.before_volume_mm3),
            "after_volume_mm3": float(self.after_volume_mm3),
            "reversed": self.reversed,
        }
        digest = hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()
        object.__setattr__(self, "receipt_sha256", digest)
        object.__setattr__(self, "receipt_id", f"groove_conformance_{digest[:32]}")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _shape_volume(shape: object, path: str) -> float:
    try:
        if shape is None or shape.isNull() or not shape.isValid() or len(shape.Solids) != 1:
            _fail(GrooveRuleErrorCode.PRECONDITION_FAILED, path)
        volume = float(shape.Volume)
    except GrooveRuleError:
        raise
    except Exception:
        _fail(GrooveRuleErrorCode.PRECONDITION_FAILED, path)
    if not math.isfinite(volume) or volume <= 0.0:
        _fail(GrooveRuleErrorCode.PRECONDITION_FAILED, path)
    return volume


def _validate_bindings(plan: GrooveBackendPlan, bindings: GrooveExecutionBindings) -> float:
    if (
        bindings.body_id != plan.body_id
        or bindings.base_node_id != plan.base_node_id
        or bindings.base_result_id != plan.base_result_id
        or bindings.profile_node_id != plan.profile_node_id
        or bindings.profile_result_id != plan.profile_result_id
    ):
        _fail(GrooveRuleErrorCode.INTEGRITY_FAILURE, "/bindings/identity")
    document, body, base, profile = (
        bindings.document,
        bindings.body,
        bindings.base_feature,
        bindings.profile,
    )
    try:
        if (
            getattr(document, "UndoMode", 0) != 1
            or bool(document.HasPendingTransaction)
            or body.Document is not document
            or base.Document is not document
            or profile.Document is not document
            or body.TypeId != "PartDesign::Body"
            or profile.TypeId != "Sketcher::SketchObject"
            or body.Tip is not base
            or base not in body.Group
            or profile not in body.Group
            or not profile.isValid()
            or len(profile.Shape.Wires) != 1
            or not profile.Shape.Wires[0].isClosed()
            or len(profile.OpenVertices) != 0
        ):
            _fail(GrooveRuleErrorCode.PRECONDITION_FAILED, "/bindings")
    except GrooveRuleError:
        raise
    except Exception:
        _fail(GrooveRuleErrorCode.PRECONDITION_FAILED, "/bindings")
    return _shape_volume(base.Shape, "/bindings/base_feature")


def apply_groove_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: GrooveExecutionBindings,
) -> GrooveConformanceReceipt:
    """Explicit trusted-host action; validate exact bytes before native mutation."""

    if type(bindings) is not GrooveExecutionBindings:
        _fail(GrooveRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(GrooveRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != GROOVE_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(GrooveRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_groove_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    before_volume = _validate_bindings(plan, bindings)
    document, body, base, profile = (
        bindings.document,
        bindings.body,
        bindings.base_feature,
        bindings.profile,
    )
    object_name = f"Groove_{plan.plan_sha256[:16]}"
    try:
        if document.getObject(object_name) is not None:
            _fail(GrooveRuleErrorCode.PRECONDITION_FAILED, "/document/object_name")
        before_objects = tuple(document.Objects)
        before_group = tuple(body.Group)
        before_tip = body.Tip
        before_base_visibility = bool(base.Visibility)
        before_profile_visibility = bool(profile.Visibility)
    except GrooveRuleError:
        raise
    except Exception:
        _fail(GrooveRuleErrorCode.PRECONDITION_FAILED, "/document")

    transaction_open = False
    try:
        document.openTransaction("VibeCAD trusted Groove")
        transaction_open = True
        groove = body.newObject("PartDesign::Groove", object_name)
        groove.Profile = profile
        groove.ReferenceAxis = (profile, ["V_Axis"])
        groove.Type = "Angle"
        groove.Angle = plan.angle_degrees
        groove.Angle2 = 0.0
        groove.Midplane = False
        groove.Reversed = plan.reversed
        groove.Refine = True
        groove.AllowMultiFace = False
        document.recompute()
        after_volume = _shape_volume(groove.Shape, "/result/shape")
        if (
            not groove.isValid()
            or tuple(groove.State) != ("Up-to-date",)
            or body.Tip is not groove
            or groove.BaseFeature is not base
            or groove.Profile[0] is not profile
            or tuple(groove.Profile[1]) != ()
            or groove.ReferenceAxis[0] is not profile
            or tuple(groove.ReferenceAxis[1]) != ("V_Axis",)
            or groove.Type != "Angle"
            or abs(float(groove.Angle) - plan.angle_degrees) > 1e-9
            or bool(groove.Reversed) is not plan.reversed
            or not after_volume < before_volume - 1e-9
        ):
            _fail(GrooveRuleErrorCode.CONFORMANCE_FAILED, "/result")
        document.commitTransaction()
        transaction_open = False
    except BaseException as error:
        if transaction_open:
            try:
                document.abortTransaction()
                document.recompute()
            except BaseException:
                _fail(GrooveRuleErrorCode.TRANSACTION_FAILED, "/transaction/rollback")
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
                or bool(base.Visibility) is not before_base_visibility
                or bool(profile.Visibility) is not before_profile_visibility
            ):
                _fail(GrooveRuleErrorCode.TRANSACTION_FAILED, "/transaction/rollback")
        except GrooveRuleError:
            raise
        except Exception:
            _fail(GrooveRuleErrorCode.TRANSACTION_FAILED, "/transaction/rollback")
        if isinstance(error, KeyboardInterrupt):
            raise
        if isinstance(error, GrooveRuleError):
            raise error
        _fail(GrooveRuleErrorCode.TRANSACTION_FAILED, "/transaction/apply")

    return GrooveConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        object_name=object_name,
        before_volume_mm3=before_volume,
        after_volume_mm3=after_volume,
        reversed=plan.reversed,
    )


__all__ = [
    "GROOVE_PLAN_MEDIA_TYPE",
    "GROOVE_PLAN_SCHEMA_VERSION",
    "GROOVE_FREECAD_ENGINE_BUILD_ID",
    "GROOVE_RULE_CONTRACT_SHA256",
    "GROOVE_RULE_ID",
    "MAX_GROOVE_PLAN_BYTES",
    "GrooveBackendPlan",
    "GrooveConformanceReceipt",
    "GrooveExecutionBindings",
    "GrooveRuleError",
    "GrooveRuleErrorCode",
    "apply_groove_plan",
    "decode_groove_backend_plan",
]
