"""Trusted native rules for the first horizontal PartDesign promotion batch.

The six operations in this module share one canonical authority-free plan and
one reviewed native boundary.  Wire data selects an operation only after the
adapter has matched its complete static semantic identity; it can never supply
a FreeCAD ``TypeId``, property name, or sub-element spelling.  Importing this
module does not import FreeCAD.  A trusted host must explicitly call
:func:`apply_partdesign_promotion_plan` with authenticated live-object bindings.
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

PARTDESIGN_PROMOTION_PLAN_SCHEMA_VERSION: Final = 1
PARTDESIGN_PROMOTION_PLAN_MEDIA_TYPE: Final = (
    "application/vnd.vibecad.freecad-partdesign-promotion-plan+json"
)
MAX_PARTDESIGN_PROMOTION_PLAN_BYTES: Final = 32 * 1024
PARTDESIGN_PROMOTION_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-promotion-plan.v1\0"
_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-partdesign-promotion-rule.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-promotion-receipt.v1\0"


class PartDesignPromotionOperation(StrEnum):
    ADDITIVE_LOFT = "additive_loft"
    SUBTRACTIVE_LOFT = "subtractive_loft"
    ADDITIVE_PIPE = "additive_pipe"
    SUBTRACTIVE_PIPE = "subtractive_pipe"
    ADDITIVE_HELIX = "additive_helix"
    SUBTRACTIVE_HELIX = "subtractive_helix"


@dataclass(frozen=True, slots=True)
class _NativeOperationSpec:
    type_id: str
    family: str
    additive: bool
    object_prefix: str


# This is the only operation-to-native-code selection table.  It is code-owned,
# fully committed by the rule digest below, and is never populated from a graph.
_NATIVE_OPERATION_SPECS: Final = {
    PartDesignPromotionOperation.ADDITIVE_LOFT: _NativeOperationSpec(
        "PartDesign::AdditiveLoft", "loft", True, "AdditiveLoft"
    ),
    PartDesignPromotionOperation.SUBTRACTIVE_LOFT: _NativeOperationSpec(
        "PartDesign::SubtractiveLoft", "loft", False, "SubtractiveLoft"
    ),
    PartDesignPromotionOperation.ADDITIVE_PIPE: _NativeOperationSpec(
        "PartDesign::AdditivePipe", "pipe", True, "AdditivePipe"
    ),
    PartDesignPromotionOperation.SUBTRACTIVE_PIPE: _NativeOperationSpec(
        "PartDesign::SubtractivePipe", "pipe", False, "SubtractivePipe"
    ),
    PartDesignPromotionOperation.ADDITIVE_HELIX: _NativeOperationSpec(
        "PartDesign::AdditiveHelix", "helix", True, "AdditiveHelix"
    ),
    PartDesignPromotionOperation.SUBTRACTIVE_HELIX: _NativeOperationSpec(
        "PartDesign::SubtractiveHelix", "helix", False, "SubtractiveHelix"
    ),
}

PARTDESIGN_PROMOTION_RULE_ID: Final = "freecad.partdesign.sketch-promotions.v1"
_NATIVE_CONTRACT = (
    f"engine=FreeCAD-1.1.0/{PARTDESIGN_PROMOTION_FREECAD_ENGINE_BUILD_ID};"
    "ops="
    "additive_loft:PartDesign::AdditiveLoft,"
    "subtractive_loft:PartDesign::SubtractiveLoft,"
    "additive_pipe:PartDesign::AdditivePipe,"
    "subtractive_pipe:PartDesign::SubtractivePipe,"
    "additive_helix:PartDesign::AdditiveHelix,"
    "subtractive_helix:PartDesign::SubtractiveHelix;"
    "common=Profile,BaseFeature,Midplane=false,Reversed=false,Refine=true,"
    "AllowMultiFace=false,Body.Tip=base-or-last-profile,single-solid,strict-volume-direction;"
    "loft=Sections[2..8],Closed=false,Ruled=false;"
    "pipe=Spine/all-continuous-edges,Mode=Standard,Transformation=Constant,"
    "Transition=Transformed,Sections=[],AuxiliarySpine=null,"
    "AuxiliaryCurvilinear=false,tangents=false;"
    "helix=ReferenceAxis:profile/V_Axis,Mode=pitch-height-angle,Angle=0,Growth=0,"
    "LeftHanded=false,Outside=false,turns=[0.25,1000];"
    "additive=base-optional;subtractive=base-required;transaction=rollback"
)
PARTDESIGN_PROMOTION_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN + _NATIVE_CONTRACT.encode("ascii")
).hexdigest()


class PartDesignPromotionRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class PartDesignPromotionRuleError(ValueError):
    """Bounded, non-reflective failure from the trusted native rule."""

    def __init__(self, code: PartDesignPromotionRuleErrorCode, path: str = "/") -> None:
        if type(code) is not PartDesignPromotionRuleErrorCode:
            raise TypeError("code must be a PartDesignPromotionRuleErrorCode")
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
        super().__init__(f"PartDesign promotion rule error ({code.value}) at {path}")


def _fail(code: PartDesignPromotionRuleErrorCode, path: str) -> None:
    raise PartDesignPromotionRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, path)
    return value


def _finite_float(
    value: object,
    path: str,
    *,
    code: PartDesignPromotionRuleErrorCode = PartDesignPromotionRuleErrorCode.INVALID_INPUT,
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
        _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/")
    if not raw or len(raw) > MAX_PARTDESIGN_PROMOTION_PLAN_BYTES:
        _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/")
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
    if type(raw) is not bytes or not raw or len(raw) > MAX_PARTDESIGN_PROMOTION_PLAN_BYTES:
        _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(PartDesignPromotionRuleErrorCode.INTEGRITY_FAILURE, "/")
    except (UnicodeError, ValueError, RecursionError):
        _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/")
    if type(value) is not dict or not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(PartDesignPromotionRuleErrorCode.INTEGRITY_FAILURE, "/")
    return value


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, path)
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


def _optional_selection(value: object, path: str) -> SemanticObjectSelection | None:
    return None if value is None else SemanticObjectSelection.from_mapping(value, path)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignPromotionBackendPlan:
    """Canonical authority-free plan shared by all six promotion operations."""

    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    body_id: str
    node_id: str
    result_id: str
    operation: PartDesignPromotionOperation
    base: SemanticObjectSelection | None
    profiles: tuple[SemanticObjectSelection, ...]
    spine: SemanticObjectSelection | None = None
    axis_reference_id: str | None = None
    axis_result_id: str | None = None
    pitch_mm: float | None = None
    height_mm: float | None = None
    angle_degrees: float | None = None
    schema_version: int = PARTDESIGN_PROMOTION_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/schema_version")
        if type(self.operation) is not PartDesignPromotionOperation:
            _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/operation/id")
        for name in ("source_artifact_id", "source_graph_id", "body_id", "node_id", "result_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in (
            "source_graph_sha256",
            "source_content_sha256",
            "lowering_request_sha256",
            "adapter_contract_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if self.base is not None and type(self.base) is not SemanticObjectSelection:
            _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/selection/base")
        if type(self.profiles) is not tuple or any(
            type(item) is not SemanticObjectSelection for item in self.profiles
        ):
            _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/selection/profiles")
        if self.spine is not None and type(self.spine) is not SemanticObjectSelection:
            _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/selection/spine")
        spec = _NATIVE_OPERATION_SPECS[self.operation]
        if not spec.additive and self.base is None:
            _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/selection/base")
        identities = [(item.node_id, item.result_id) for item in self.profiles]
        if self.base is not None:
            identities.append((self.base.node_id, self.base.result_id))
        if self.spine is not None:
            identities.append((self.spine.node_id, self.spine.result_id))
        if (
            len({item[0] for item in identities}) != len(identities)
            or len({item[1] for item in identities}) != len(identities)
            or self.node_id in {item[0] for item in identities}
            or self.result_id in {item[1] for item in identities}
        ):
            _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/selection")
        if spec.family == "loft":
            if (
                not 2 <= len(self.profiles) <= 8
                or self.spine is not None
                or self.axis_reference_id is not None
                or self.axis_result_id is not None
                or any(
                    value is not None
                    for value in (self.pitch_mm, self.height_mm, self.angle_degrees)
                )
            ):
                _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/operation/loft")
        elif spec.family == "pipe":
            if (
                len(self.profiles) != 1
                or self.spine is None
                or self.axis_reference_id is not None
                or self.axis_result_id is not None
                or any(
                    value is not None
                    for value in (self.pitch_mm, self.height_mm, self.angle_degrees)
                )
            ):
                _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/operation/pipe")
        else:
            if (
                len(self.profiles) != 1
                or self.spine is not None
                or self.axis_reference_id is None
                or self.axis_result_id is None
            ):
                _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/operation/helix")
            object.__setattr__(
                self,
                "axis_reference_id",
                _identifier(self.axis_reference_id, "/selection/axis/reference_id"),
            )
            object.__setattr__(
                self,
                "axis_result_id",
                _identifier(self.axis_result_id, "/selection/axis/result_id"),
            )
            if self.axis_result_id in {item.result_id for item in self.profiles}:
                _fail(
                    PartDesignPromotionRuleErrorCode.INVALID_INPUT,
                    "/selection/axis/result_id",
                )
            for name, minimum, maximum in (
                ("pitch_mm", 0.01, 100_000.0),
                ("height_mm", 0.01, 1_000_000.0),
            ):
                value = _finite_float(getattr(self, name), f"/operation/{name}")
                if not minimum <= value <= maximum:
                    _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, f"/operation/{name}")
                object.__setattr__(self, name, value)
            angle = _finite_float(self.angle_degrees, "/operation/angle_degrees")
            if not math.isclose(angle, 0.0, rel_tol=0.0, abs_tol=1e-12):
                _fail(
                    PartDesignPromotionRuleErrorCode.INVALID_INPUT,
                    "/operation/angle_degrees",
                )
            object.__setattr__(self, "angle_degrees", 0.0)
            if not 0.25 <= self.turns <= 1000.0:
                _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/operation/turns")

    @property
    def family(self) -> str:
        return _NATIVE_OPERATION_SPECS[self.operation].family

    @property
    def additive(self) -> bool:
        return _NATIVE_OPERATION_SPECS[self.operation].additive

    @property
    def turns(self) -> float:
        if self.pitch_mm is None or self.height_mm is None:
            return 0.0
        return self.height_mm / self.pitch_mm

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    def to_mapping(self) -> dict[str, object]:
        loft = None
        pipe = None
        helix = None
        if self.family == "loft":
            loft = {"closed": False, "ruled": False}
        elif self.family == "pipe":
            pipe = {
                "mode": "standard",
                "transformation": "constant",
                "transition": "transformed",
                "auxiliary": False,
                "multisection": False,
            }
        else:
            helix = {
                "mode": "pitch-height-angle",
                "pitch_mm": self.pitch_mm,
                "height_mm": self.height_mm,
                "angle_degrees": self.angle_degrees,
                "growth_mm": 0.0,
                "turns": self.turns,
                "left_handed": False,
                "outside": False,
            }
        return {
            "schema_version": self.schema_version,
            "authority": "none",
            "backend": {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PARTDESIGN_PROMOTION_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": PARTDESIGN_PROMOTION_RULE_ID,
                "rule_contract_sha256": PARTDESIGN_PROMOTION_RULE_CONTRACT_SHA256,
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
                "profiles": [item.to_mapping() for item in self.profiles],
                "spine": None if self.spine is None else self.spine.to_mapping(),
                "axis": (
                    None
                    if self.axis_reference_id is None
                    else {
                        "reference_id": self.axis_reference_id,
                        "result_id": self.axis_result_id,
                    }
                ),
            },
            "operation": {
                "id": self.operation.value,
                "common": {
                    "midplane": False,
                    "reversed": False,
                    "refine": True,
                    "allow_multi_face": False,
                },
                "loft": loft,
                "pipe": pipe,
                "helix": helix,
            },
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_PLAN_DIGEST_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> PartDesignPromotionBackendPlan:
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
            {"body_id", "node_id", "result_id", "base", "profiles", "spine", "axis"},
            "/selection",
        )
        operation = _exact_fields(
            root["operation"], {"id", "common", "loft", "pipe", "helix"}, "/operation"
        )
        common = _exact_fields(
            operation["common"],
            {"midplane", "reversed", "refine", "allow_multi_face"},
            "/operation/common",
        )
        try:
            operation_id = PartDesignPromotionOperation(operation["id"])
        except (TypeError, ValueError):
            _fail(PartDesignPromotionRuleErrorCode.INTEGRITY_FAILURE, "/operation/id")
        spec = _NATIVE_OPERATION_SPECS[operation_id]
        expected_loft = {"closed": False, "ruled": False} if spec.family == "loft" else None
        expected_pipe = (
            {
                "mode": "standard",
                "transformation": "constant",
                "transition": "transformed",
                "auxiliary": False,
                "multisection": False,
            }
            if spec.family == "pipe"
            else None
        )
        helix = operation["helix"]
        if spec.family == "helix":
            helix = _exact_fields(
                helix,
                {
                    "mode",
                    "pitch_mm",
                    "height_mm",
                    "angle_degrees",
                    "growth_mm",
                    "turns",
                    "left_handed",
                    "outside",
                },
                "/operation/helix",
            )
            decoded_turns = _finite_float(
                helix["turns"],
                "/operation/turns",
                code=PartDesignPromotionRuleErrorCode.INTEGRITY_FAILURE,
            )
        if (
            root["authority"] != "none"
            or backend
            != {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PARTDESIGN_PROMOTION_FREECAD_ENGINE_BUILD_ID,
            }
            or rule
            != {
                "rule_id": PARTDESIGN_PROMOTION_RULE_ID,
                "rule_contract_sha256": PARTDESIGN_PROMOTION_RULE_CONTRACT_SHA256,
            }
            or common
            != {
                "midplane": False,
                "reversed": False,
                "refine": True,
                "allow_multi_face": False,
            }
            or operation["loft"] != expected_loft
            or operation["pipe"] != expected_pipe
            or (spec.family != "helix" and operation["helix"] is not None)
            or (
                spec.family == "helix"
                and (
                    helix.get("mode") != "pitch-height-angle"
                    or helix.get("growth_mm") != 0.0
                    or helix.get("left_handed") is not False
                    or helix.get("outside") is not False
                )
            )
        ):
            _fail(PartDesignPromotionRuleErrorCode.INTEGRITY_FAILURE, "/contract")
        profiles = selection["profiles"]
        if type(profiles) is not list:
            _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/selection/profiles")
        axis = selection["axis"]
        if axis is not None:
            axis = _exact_fields(axis, {"reference_id", "result_id"}, "/selection/axis")
        result = cls(
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
            base=_optional_selection(selection["base"], "/selection/base"),
            profiles=tuple(
                SemanticObjectSelection.from_mapping(item, "/selection/profiles/item")
                for item in profiles
            ),
            spine=_optional_selection(selection["spine"], "/selection/spine"),
            axis_reference_id=None if axis is None else axis["reference_id"],
            axis_result_id=None if axis is None else axis["result_id"],
            pitch_mm=None if spec.family != "helix" else helix["pitch_mm"],
            height_mm=None if spec.family != "helix" else helix["height_mm"],
            angle_degrees=None if spec.family != "helix" else helix["angle_degrees"],
        )
        if spec.family == "helix" and not math.isclose(
            result.turns, decoded_turns, rel_tol=0.0, abs_tol=1e-12
        ):
            _fail(PartDesignPromotionRuleErrorCode.INTEGRITY_FAILURE, "/operation/turns")
        return result


def decode_partdesign_promotion_backend_plan(
    raw: object,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> PartDesignPromotionBackendPlan:
    if expected_content_sha256 is not None:
        expected_content_sha256 = _digest(expected_content_sha256, "/expected_content_sha256")
    if expected_plan_sha256 is not None:
        expected_plan_sha256 = _digest(expected_plan_sha256, "/expected_plan_sha256")
    result = PartDesignPromotionBackendPlan.from_mapping(_decode_mapping(raw))
    if type(raw) is not bytes or not hmac.compare_digest(raw, result.canonical_bytes):
        _fail(PartDesignPromotionRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_content_sha256
    ):
        _fail(PartDesignPromotionRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        result.plan_sha256, expected_plan_sha256
    ):
        _fail(PartDesignPromotionRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticatedPromotionObject:
    object: object
    node_id: str
    result_id: str

    def __post_init__(self) -> None:
        if self.object is None:
            _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/bindings/object")
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/bindings/node_id"))
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/bindings/result_id"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignPromotionExecutionBindings:
    """Host-authenticated semantic-id to live-object map for one exact plan."""

    document: object
    body: object
    body_id: str
    base: AuthenticatedPromotionObject | None
    profiles: tuple[AuthenticatedPromotionObject, ...]
    spine: AuthenticatedPromotionObject | None = None

    def __post_init__(self) -> None:
        if self.document is None or self.body is None:
            _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/bindings")
        object.__setattr__(self, "body_id", _identifier(self.body_id, "/bindings/body_id"))
        if self.base is not None and type(self.base) is not AuthenticatedPromotionObject:
            _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/bindings/base")
        if type(self.profiles) is not tuple or any(
            type(item) is not AuthenticatedPromotionObject for item in self.profiles
        ):
            _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/bindings/profiles")
        if self.spine is not None and type(self.spine) is not AuthenticatedPromotionObject:
            _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/bindings/spine")


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignPromotionConformanceReceipt:
    plan_sha256: str
    operation: PartDesignPromotionOperation
    object_name: str
    before_volume_mm3: float
    after_volume_mm3: float
    receipt_id: str = field(init=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/plan_sha256"))
        if type(self.operation) is not PartDesignPromotionOperation:
            _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/operation")
        object.__setattr__(self, "object_name", _identifier(self.object_name, "/object_name"))
        spec = _NATIVE_OPERATION_SPECS[self.operation]
        before = _finite_float(
            self.before_volume_mm3,
            "/receipt/before_volume_mm3",
            code=PartDesignPromotionRuleErrorCode.CONFORMANCE_FAILED,
        )
        after = _finite_float(
            self.after_volume_mm3,
            "/receipt/after_volume_mm3",
            code=PartDesignPromotionRuleErrorCode.CONFORMANCE_FAILED,
        )
        if (
            before < 0.0
            or after <= 0.0
            or (spec.additive and before > 0.0 and not after > before)
            or (not spec.additive and not 0.0 < after < before)
        ):
            _fail(PartDesignPromotionRuleErrorCode.CONFORMANCE_FAILED, "/receipt")
        body = {
            "authority": "none",
            "plan_sha256": self.plan_sha256,
            "operation": self.operation.value,
            "object_name": self.object_name,
            "before_volume_mm3": float(before),
            "after_volume_mm3": float(after),
        }
        digest = hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()
        object.__setattr__(self, "receipt_sha256", digest)
        object.__setattr__(self, "receipt_id", f"partdesign_promotion_{digest[:32]}")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _shape_volume(shape: object, path: str) -> float:
    try:
        if shape is None or shape.isNull() or not shape.isValid() or len(shape.Solids) != 1:
            _fail(PartDesignPromotionRuleErrorCode.PRECONDITION_FAILED, path)
        volume = float(shape.Volume)
    except PartDesignPromotionRuleError:
        raise
    except Exception:
        _fail(PartDesignPromotionRuleErrorCode.PRECONDITION_FAILED, path)
    if not math.isfinite(volume) or volume <= 0.0:
        _fail(PartDesignPromotionRuleErrorCode.PRECONDITION_FAILED, path)
    return volume


def _same_selection(
    semantic: SemanticObjectSelection | None,
    authenticated: AuthenticatedPromotionObject | None,
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
    plan: PartDesignPromotionBackendPlan,
    bindings: PartDesignPromotionExecutionBindings,
) -> tuple[float, tuple[object, ...], object | None]:
    if (
        bindings.body_id != plan.body_id
        or not _same_selection(plan.base, bindings.base)
        or len(bindings.profiles) != len(plan.profiles)
        or any(
            not _same_selection(expected, actual)
            for expected, actual in zip(plan.profiles, bindings.profiles, strict=True)
        )
        or not _same_selection(plan.spine, bindings.spine)
    ):
        _fail(PartDesignPromotionRuleErrorCode.INTEGRITY_FAILURE, "/bindings/identity")
    document, body = bindings.document, bindings.body
    base_object = None if bindings.base is None else bindings.base.object
    profile_objects = tuple(item.object for item in bindings.profiles)
    spine_object = None if bindings.spine is None else bindings.spine.object
    expected_group = tuple(
        item for item in (base_object, *profile_objects, spine_object) if item is not None
    )
    try:
        if (
            getattr(document, "UndoMode", 0) != 1
            or bool(document.HasPendingTransaction)
            or body.Document is not document
            or body.TypeId != "PartDesign::Body"
            or tuple(body.Group) != expected_group
            or body.Tip is not (base_object if base_object is not None else profile_objects[-1])
        ):
            _fail(PartDesignPromotionRuleErrorCode.PRECONDITION_FAILED, "/bindings")
        for profile in profile_objects:
            if (
                profile.Document is not document
                or profile.TypeId != "Sketcher::SketchObject"
                or not profile.isValid()
                or len(profile.Shape.Wires) != 1
                or not profile.Shape.Wires[0].isClosed()
                or len(profile.OpenVertices) != 0
            ):
                _fail(
                    PartDesignPromotionRuleErrorCode.PRECONDITION_FAILED,
                    "/bindings/profiles",
                )
        if spine_object is not None and (
            spine_object.Document is not document
            or spine_object.TypeId != "Sketcher::SketchObject"
            or not spine_object.isValid()
            or len(spine_object.Shape.Wires) != 1
            or len(spine_object.Shape.Edges) < 1
            or len(spine_object.OpenVertices) not in {0, 2}
        ):
            _fail(PartDesignPromotionRuleErrorCode.PRECONDITION_FAILED, "/bindings/spine")
    except PartDesignPromotionRuleError:
        raise
    except Exception:
        _fail(PartDesignPromotionRuleErrorCode.PRECONDITION_FAILED, "/bindings")
    before_volume = (
        0.0 if base_object is None else _shape_volume(base_object.Shape, "/bindings/base")
    )
    return before_volume, expected_group, base_object


def _verify_common_result(
    feature: object,
    *,
    body: object,
    base: object | None,
    profile: object,
) -> None:
    try:
        if (
            not feature.isValid()
            or tuple(feature.State) != ("Up-to-date",)
            or body.Tip is not feature
            or feature.BaseFeature is not base
            or feature.Profile[0] is not profile
            or tuple(feature.Profile[1]) != ()
            or bool(feature.Midplane)
            or bool(feature.Reversed)
            or not bool(feature.Refine)
            or bool(feature.AllowMultiFace)
        ):
            _fail(PartDesignPromotionRuleErrorCode.CONFORMANCE_FAILED, "/result")
    except PartDesignPromotionRuleError:
        raise
    except Exception:
        _fail(PartDesignPromotionRuleErrorCode.CONFORMANCE_FAILED, "/result")


def apply_partdesign_promotion_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: PartDesignPromotionExecutionBindings,
) -> PartDesignPromotionConformanceReceipt:
    """Explicit trusted-host action; validate exact bytes before native mutation."""

    if type(bindings) is not PartDesignPromotionExecutionBindings:
        _fail(PartDesignPromotionRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(PartDesignPromotionRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != PARTDESIGN_PROMOTION_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(PartDesignPromotionRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_partdesign_promotion_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    before_volume, before_group, base = _validate_bindings(plan, bindings)
    document, body = bindings.document, bindings.body
    profiles = tuple(item.object for item in bindings.profiles)
    spine = None if bindings.spine is None else bindings.spine.object
    spec = _NATIVE_OPERATION_SPECS[plan.operation]
    object_name = f"{spec.object_prefix}_{plan.plan_sha256[:16]}"
    try:
        if document.getObject(object_name) is not None:
            _fail(
                PartDesignPromotionRuleErrorCode.PRECONDITION_FAILED,
                "/document/object_name",
            )
        before_objects = tuple(document.Objects)
        before_tip = body.Tip
        before_visibilities = tuple(bool(item.Visibility) for item in before_group)
    except PartDesignPromotionRuleError:
        raise
    except Exception:
        _fail(PartDesignPromotionRuleErrorCode.PRECONDITION_FAILED, "/document")

    transaction_open = False
    try:
        document.openTransaction("VibeCAD trusted PartDesign promotion")
        transaction_open = True
        feature = body.newObject(spec.type_id, object_name)
        feature.Profile = profiles[0]
        feature.Midplane = False
        feature.Reversed = False
        feature.Refine = True
        feature.AllowMultiFace = False
        if spec.family == "loft":
            feature.Sections = list(profiles[1:])
            feature.Closed = False
            feature.Ruled = False
        elif spec.family == "pipe":
            edge_names = [f"Edge{index}" for index in range(1, len(spine.Shape.Edges) + 1)]
            feature.Spine = (spine, edge_names)
            feature.Mode = "Standard"
            feature.Transformation = "Constant"
            feature.Transition = "Transformed"
            feature.Sections = []
            feature.AuxiliaryCurvilinear = False
            feature.SpineTangent = False
            feature.AuxiliarySpineTangent = False
        else:
            feature.ReferenceAxis = (profiles[0], ["V_Axis"])
            feature.Mode = "pitch-height-angle"
            feature.Pitch = plan.pitch_mm
            feature.Height = plan.height_mm
            feature.Angle = plan.angle_degrees
            feature.Growth = 0.0
            feature.LeftHanded = False
            feature.Outside = False
        document.recompute()
        after_volume = _shape_volume(feature.Shape, "/result/shape")
        _verify_common_result(feature, body=body, base=base, profile=profiles[0])
        if spec.family == "loft":
            section_objects = tuple(item[0] for item in feature.Sections)
            section_subs = tuple(tuple(item[1]) for item in feature.Sections)
            if (
                section_objects != profiles[1:]
                or section_subs != (("",),) * (len(profiles) - 1)
                or bool(feature.Closed)
                or bool(feature.Ruled)
            ):
                _fail(PartDesignPromotionRuleErrorCode.CONFORMANCE_FAILED, "/result/loft")
        elif spec.family == "pipe":
            expected_edges = tuple(f"Edge{index}" for index in range(1, len(spine.Shape.Edges) + 1))
            if (
                feature.Spine[0] is not spine
                or tuple(feature.Spine[1]) != expected_edges
                or str(feature.Mode) != "Standard"
                or str(feature.Transformation) != "Constant"
                or str(feature.Transition) != "Transformed"
                or tuple(feature.Sections)
                or feature.AuxiliarySpine is not None
                or bool(feature.AuxiliaryCurvilinear)
                or bool(feature.SpineTangent)
                or bool(feature.AuxiliarySpineTangent)
            ):
                _fail(PartDesignPromotionRuleErrorCode.CONFORMANCE_FAILED, "/result/pipe")
        else:
            if (
                feature.ReferenceAxis[0] is not profiles[0]
                or tuple(feature.ReferenceAxis[1]) != ("V_Axis",)
                or str(feature.Mode) != "pitch-height-angle"
                or abs(float(feature.Pitch) - plan.pitch_mm) > 1e-9
                or abs(float(feature.Height) - plan.height_mm) > 1e-9
                or abs(float(feature.Angle) - plan.angle_degrees) > 1e-9
                or abs(float(feature.Growth)) > 1e-12
                or abs(float(feature.Turns) - plan.turns) > 1e-9
                or bool(feature.LeftHanded)
                or bool(feature.Outside)
            ):
                _fail(PartDesignPromotionRuleErrorCode.CONFORMANCE_FAILED, "/result/helix")
        epsilon = max(1e-9, before_volume * 1e-12)
        if (
            spec.additive
            and before_volume > 0.0
            and not after_volume > before_volume + epsilon
            or not spec.additive
            and not after_volume < before_volume - epsilon
        ):
            _fail(PartDesignPromotionRuleErrorCode.CONFORMANCE_FAILED, "/result/volume")
        document.commitTransaction()
        transaction_open = False
    except BaseException as error:
        if transaction_open:
            try:
                document.abortTransaction()
                document.recompute()
            except BaseException:
                _fail(
                    PartDesignPromotionRuleErrorCode.TRANSACTION_FAILED,
                    "/transaction/rollback",
                )
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
                _fail(
                    PartDesignPromotionRuleErrorCode.TRANSACTION_FAILED,
                    "/transaction/rollback",
                )
        except PartDesignPromotionRuleError:
            raise
        except Exception:
            _fail(
                PartDesignPromotionRuleErrorCode.TRANSACTION_FAILED,
                "/transaction/rollback",
            )
        if isinstance(error, KeyboardInterrupt):
            raise
        if isinstance(error, PartDesignPromotionRuleError):
            raise error
        _fail(PartDesignPromotionRuleErrorCode.TRANSACTION_FAILED, "/transaction/apply")

    return PartDesignPromotionConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        object_name=object_name,
        before_volume_mm3=before_volume,
        after_volume_mm3=after_volume,
    )


__all__ = [
    "MAX_PARTDESIGN_PROMOTION_PLAN_BYTES",
    "PARTDESIGN_PROMOTION_FREECAD_ENGINE_BUILD_ID",
    "PARTDESIGN_PROMOTION_PLAN_MEDIA_TYPE",
    "PARTDESIGN_PROMOTION_PLAN_SCHEMA_VERSION",
    "PARTDESIGN_PROMOTION_RULE_CONTRACT_SHA256",
    "PARTDESIGN_PROMOTION_RULE_ID",
    "AuthenticatedPromotionObject",
    "PartDesignPromotionBackendPlan",
    "PartDesignPromotionConformanceReceipt",
    "PartDesignPromotionExecutionBindings",
    "PartDesignPromotionOperation",
    "PartDesignPromotionRuleError",
    "PartDesignPromotionRuleErrorCode",
    "SemanticObjectSelection",
    "apply_partdesign_promotion_plan",
    "decode_partdesign_promotion_backend_plan",
]
