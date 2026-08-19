"""Trusted native FreeCAD rule for planar-mechanical v1 intent.

The authority-free plan contains only reviewed semantic geometry and stable
intent identifiers.  FreeCAD ``TypeId`` and property spellings live exclusively
in this module and are committed by :data:`PLANAR_MECHANICAL_RULE_CONTRACT_SHA256`.
Importing the module does not import FreeCAD.  Mutation requires an explicit
trusted-host call to :func:`apply_planar_mechanical_plan`.

The PM1 graph models every circular removal as a generic extrusion/remove with
``ThroughAll`` extent.  The reviewed native interpretation is therefore one
``PartDesign::Pocket`` per circle, preserving the graph's feature/result chain.
It is deliberately not reinterpreted as a PartDesign Hole feature.
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

PLANAR_MECHANICAL_PLAN_SCHEMA_VERSION: Final = 1
PLANAR_MECHANICAL_PLAN_MEDIA_TYPE: Final = (
    "application/vnd.vibecad.freecad-planar-mechanical-plan+json"
)
MAX_PLANAR_MECHANICAL_PLAN_BYTES: Final = 128 * 1024
MAX_PLANAR_MECHANICAL_CIRCLES: Final = 16
PLANAR_MECHANICAL_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-planar-mechanical-plan.v1\0"
_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-planar-mechanical-rule.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-planar-mechanical-receipt.v1\0"
_MAX_PROFILE_RESIDUAL_MM = 0.25
_MIN_FEATURE_SIZE_MM = 0.50
_MAX_DEPTH_MM = 100_000.0

PLANAR_MECHANICAL_RULE_ID: Final = "freecad.partdesign.planar-mechanical-v1"
_NATIVE_CONTRACT = (
    f"engine=FreeCAD-1.1.0/{PLANAR_MECHANICAL_FREECAD_ENGINE_BUILD_ID};"
    "body=PartDesign::Body;outer-profile=Sketcher::SketchObject/4xPart::LineSegment;"
    "add=PartDesign::Pad/Profile,Type=Length,Length,Midplane=false,Reversed=false,"
    "Refine=true,AllowMultiFace=false;"
    "inner-profile=Sketcher::SketchObject/Part::Circle/Sketcher::Constraint.Radius;"
    "remove=PartDesign::Pocket/Profile,Type=ThroughAll,SideType=One-side,"
    "AlongSketchNormal=true,UseCustomVector=false,Offset=0,Offset2=0,"
    "TaperAngle=0,TaperAngle2=0,Reversed=true,Refine=true;"
    "feature-chain=one-pocket-per-circle[0..16];single-body=true;single-solid=true;"
    "volume=rectangle*depth-sum(circle*depth);transaction=rollback"
)
PLANAR_MECHANICAL_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN + _NATIVE_CONTRACT.encode("ascii")
).hexdigest()


class PlanarMechanicalRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class PlanarMechanicalRuleError(ValueError):
    """Bounded, non-reflective failure from the trusted native rule."""

    def __init__(self, code: PlanarMechanicalRuleErrorCode, path: str = "/") -> None:
        if type(code) is not PlanarMechanicalRuleErrorCode:
            raise TypeError("code must be an exact PlanarMechanicalRuleErrorCode")
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
        super().__init__(f"planar mechanical rule error ({code.value}) at {path}")


def _fail(code: PlanarMechanicalRuleErrorCode, path: str) -> None:
    raise PlanarMechanicalRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, path)
    return value


def _finite(value: object, path: str, *, positive: bool = False) -> float:
    if type(value) not in {int, float}:
        _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, path)
    try:
        result = float(value)
    except (OverflowError, ValueError):
        _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, path)
    if not math.isfinite(result):
        _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, path)
    if positive and result <= 0.0:
        _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, path)
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
        _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, "/")
    if not raw or len(raw) > MAX_PLANAR_MECHANICAL_PLAN_BYTES:
        _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, "/")
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
    if type(raw) is not bytes or not raw or len(raw) > MAX_PLANAR_MECHANICAL_PLAN_BYTES:
        _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, "/")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(PlanarMechanicalRuleErrorCode.INTEGRITY_FAILURE, "/")
    except (UnicodeError, ValueError, RecursionError):
        _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, "/")
    if type(value) is not dict or not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(PlanarMechanicalRuleErrorCode.INTEGRITY_FAILURE, "/")
    return value


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanarDocumentBinding:
    artifact_id: str
    document_id: str
    document_digest: str
    content_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _identifier(self.artifact_id, "/artifact_id"))
        object.__setattr__(self, "document_id", _identifier(self.document_id, "/document_id"))
        object.__setattr__(
            self, "document_digest", _digest(self.document_digest, "/document_digest")
        )
        object.__setattr__(self, "content_sha256", _digest(self.content_sha256, "/content_sha256"))

    def to_mapping(self) -> dict[str, str]:
        return {
            "artifact_id": self.artifact_id,
            "document_id": self.document_id,
            "document_digest": self.document_digest,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str) -> PlanarDocumentBinding:
        item = _exact_fields(
            value,
            {"artifact_id", "document_id", "document_digest", "content_sha256"},
            path,
        )
        return cls(**item)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanarRectangleProfile:
    geometry_id: str
    profile_result_id: str
    center_x_mm: float
    center_y_mm: float
    half_width_mm: float
    half_height_mm: float
    rotation_radians: float

    def __post_init__(self) -> None:
        for name in ("geometry_id", "profile_result_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in ("center_x_mm", "center_y_mm", "rotation_radians"):
            object.__setattr__(self, name, _finite(getattr(self, name), f"/{name}"))
        for name in ("half_width_mm", "half_height_mm"):
            object.__setattr__(self, name, _finite(getattr(self, name), f"/{name}", positive=True))

    def to_mapping(self) -> dict[str, object]:
        return {
            "geometry_id": self.geometry_id,
            "profile_result_id": self.profile_result_id,
            "center_mm": [self.center_x_mm, self.center_y_mm],
            "half_extents_mm": [self.half_width_mm, self.half_height_mm],
            "rotation_radians": self.rotation_radians,
        }

    @classmethod
    def from_mapping(cls, value: object) -> PlanarRectangleProfile:
        item = _exact_fields(
            value,
            {
                "geometry_id",
                "profile_result_id",
                "center_mm",
                "half_extents_mm",
                "rotation_radians",
            },
            "/rectangle",
        )
        center = item["center_mm"]
        extents = item["half_extents_mm"]
        if type(center) is not list or len(center) != 2:
            _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, "/rectangle/center_mm")
        if type(extents) is not list or len(extents) != 2:
            _fail(
                PlanarMechanicalRuleErrorCode.INVALID_INPUT,
                "/rectangle/half_extents_mm",
            )
        return cls(
            geometry_id=item["geometry_id"],
            profile_result_id=item["profile_result_id"],
            center_x_mm=center[0],
            center_y_mm=center[1],
            half_width_mm=extents[0],
            half_height_mm=extents[1],
            rotation_radians=item["rotation_radians"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanarCircleRemoval:
    geometry_id: str
    profile_result_id: str
    node_id: str
    result_id: str
    base_node_id: str
    base_result_id: str
    center_x_mm: float
    center_y_mm: float
    radius_mm: float

    def __post_init__(self) -> None:
        for name in (
            "geometry_id",
            "profile_result_id",
            "node_id",
            "result_id",
            "base_node_id",
            "base_result_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in ("center_x_mm", "center_y_mm"):
            object.__setattr__(self, name, _finite(getattr(self, name), f"/{name}"))
        object.__setattr__(self, "radius_mm", _finite(self.radius_mm, "/radius_mm", positive=True))

    def to_mapping(self) -> dict[str, object]:
        return {
            "geometry_id": self.geometry_id,
            "profile_result_id": self.profile_result_id,
            "node_id": self.node_id,
            "result_id": self.result_id,
            "base_node_id": self.base_node_id,
            "base_result_id": self.base_result_id,
            "center_mm": [self.center_x_mm, self.center_y_mm],
            "radius_mm": self.radius_mm,
            "extent": "through_all",
        }

    @classmethod
    def from_mapping(cls, value: object, path: str) -> PlanarCircleRemoval:
        item = _exact_fields(
            value,
            {
                "geometry_id",
                "profile_result_id",
                "node_id",
                "result_id",
                "base_node_id",
                "base_result_id",
                "center_mm",
                "radius_mm",
                "extent",
            },
            path,
        )
        center = item["center_mm"]
        if type(center) is not list or len(center) != 2:
            _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, f"{path}/center_mm")
        if item["extent"] != "through_all":
            _fail(PlanarMechanicalRuleErrorCode.INTEGRITY_FAILURE, f"{path}/extent")
        return cls(
            geometry_id=item["geometry_id"],
            profile_result_id=item["profile_result_id"],
            node_id=item["node_id"],
            result_id=item["result_id"],
            base_node_id=item["base_node_id"],
            base_result_id=item["base_result_id"],
            center_x_mm=center[0],
            center_y_mm=center[1],
            radius_mm=item["radius_mm"],
        )


def _local_point(rectangle: PlanarRectangleProfile, x: float, y: float) -> tuple[float, float]:
    dx = x - rectangle.center_x_mm
    dy = y - rectangle.center_y_mm
    cosine = math.cos(rectangle.rotation_radians)
    sine = math.sin(rectangle.rotation_radians)
    return cosine * dx + sine * dy, -sine * dx + cosine * dy


def _validate_circle_layout(
    rectangle: PlanarRectangleProfile,
    circles: tuple[PlanarCircleRemoval, ...],
) -> None:
    for index, circle in enumerate(circles):
        local_x, local_y = _local_point(rectangle, circle.center_x_mm, circle.center_y_mm)
        if (
            circle.radius_mm < _MIN_FEATURE_SIZE_MM
            or abs(local_x) + circle.radius_mm > rectangle.half_width_mm + _MAX_PROFILE_RESIDUAL_MM
            or abs(local_y) + circle.radius_mm > rectangle.half_height_mm + _MAX_PROFILE_RESIDUAL_MM
        ):
            _fail(
                PlanarMechanicalRuleErrorCode.INVALID_INPUT,
                f"/circles/{index}/containment",
            )
        for other_index, other in enumerate(circles[:index]):
            separation = math.hypot(
                circle.center_x_mm - other.center_x_mm,
                circle.center_y_mm - other.center_y_mm,
            )
            if separation < circle.radius_mm + other.radius_mm:
                _fail(
                    PlanarMechanicalRuleErrorCode.INVALID_INPUT,
                    f"/circles/{other_index}/overlap",
                )


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanarMechanicalBackendPlan:
    sketch_document: PlanarDocumentBinding
    parametric_document: PlanarDocumentBinding
    lowering_request_sha256: str
    adapter_contract_sha256: str
    body_id: str
    profiles_node_id: str
    add_node_id: str
    add_result_id: str
    final_node_id: str
    final_result_id: str
    depth_parameter_id: str
    depth_mm: float
    rectangle: PlanarRectangleProfile
    circles: tuple[PlanarCircleRemoval, ...] = ()
    schema_version: int = PLANAR_MECHANICAL_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, "/schema_version")
        if (
            type(self.sketch_document) is not PlanarDocumentBinding
            or type(self.parametric_document) is not PlanarDocumentBinding
            or type(self.rectangle) is not PlanarRectangleProfile
        ):
            _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, "/documents")
        for name in ("lowering_request_sha256", "adapter_contract_sha256"):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        for name in (
            "body_id",
            "profiles_node_id",
            "add_node_id",
            "add_result_id",
            "final_node_id",
            "final_result_id",
            "depth_parameter_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        object.__setattr__(self, "depth_mm", _finite(self.depth_mm, "/depth_mm", positive=True))
        if self.depth_mm > _MAX_DEPTH_MM:
            _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, "/depth_mm")
        if type(self.circles) is not tuple or any(
            type(item) is not PlanarCircleRemoval for item in self.circles
        ):
            _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, "/circles")
        if len(self.circles) > MAX_PLANAR_MECHANICAL_CIRCLES:
            _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, "/circles")
        if len({item.geometry_id for item in self.circles}) != len(self.circles):
            _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, "/circles/geometry_id")
        if len({item.node_id for item in self.circles}) != len(self.circles):
            _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, "/circles/node_id")
        expected_base_node = self.add_node_id
        expected_base_result = self.add_result_id
        for index, circle in enumerate(self.circles):
            if (
                circle.base_node_id != expected_base_node
                or circle.base_result_id != expected_base_result
            ):
                _fail(
                    PlanarMechanicalRuleErrorCode.INVALID_INPUT,
                    f"/circles/{index}/base",
                )
            expected_base_node = circle.node_id
            expected_base_result = circle.result_id
        if self.final_node_id != expected_base_node or self.final_result_id != expected_base_result:
            _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, "/final_result")
        _validate_circle_layout(self.rectangle, self.circles)

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    @property
    def expected_volume_mm3(self) -> float:
        area = 4.0 * self.rectangle.half_width_mm * self.rectangle.half_height_mm
        area -= sum(math.pi * item.radius_mm**2 for item in self.circles)
        return area * self.depth_mm

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority": "none",
            "backend": {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PLANAR_MECHANICAL_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": PLANAR_MECHANICAL_RULE_ID,
                "rule_contract_sha256": PLANAR_MECHANICAL_RULE_CONTRACT_SHA256,
                "native_interpretation": "pad_then_pocket_through_all",
            },
            "source": {
                "sketch": self.sketch_document.to_mapping(),
                "parametric": self.parametric_document.to_mapping(),
            },
            "binding": {
                "lowering_request_sha256": self.lowering_request_sha256,
                "adapter_contract_sha256": self.adapter_contract_sha256,
            },
            "selection": {
                "body_id": self.body_id,
                "profiles_node_id": self.profiles_node_id,
                "add_node_id": self.add_node_id,
                "add_result_id": self.add_result_id,
                "final_node_id": self.final_node_id,
                "final_result_id": self.final_result_id,
                "depth_parameter_id": self.depth_parameter_id,
            },
            "geometry": {
                "depth_mm": self.depth_mm,
                "rectangle": self.rectangle.to_mapping(),
                "circles": [item.to_mapping() for item in self.circles],
            },
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_PLAN_DIGEST_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> PlanarMechanicalBackendPlan:
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
                "geometry",
            },
            "/",
        )
        backend = _exact_fields(
            root["backend"],
            {"engine", "engine_version", "engine_build_id"},
            "/backend",
        )
        rule = _exact_fields(
            root["rule"],
            {"rule_id", "rule_contract_sha256", "native_interpretation"},
            "/rule",
        )
        source = _exact_fields(root["source"], {"sketch", "parametric"}, "/source")
        binding = _exact_fields(
            root["binding"],
            {"lowering_request_sha256", "adapter_contract_sha256"},
            "/binding",
        )
        selection = _exact_fields(
            root["selection"],
            {
                "body_id",
                "profiles_node_id",
                "add_node_id",
                "add_result_id",
                "final_node_id",
                "final_result_id",
                "depth_parameter_id",
            },
            "/selection",
        )
        geometry = _exact_fields(
            root["geometry"],
            {"depth_mm", "rectangle", "circles"},
            "/geometry",
        )
        if (
            root["authority"] != "none"
            or backend
            != {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PLANAR_MECHANICAL_FREECAD_ENGINE_BUILD_ID,
            }
            or rule
            != {
                "rule_id": PLANAR_MECHANICAL_RULE_ID,
                "rule_contract_sha256": PLANAR_MECHANICAL_RULE_CONTRACT_SHA256,
                "native_interpretation": "pad_then_pocket_through_all",
            }
            or type(geometry["circles"]) is not list
            or len(geometry["circles"]) > MAX_PLANAR_MECHANICAL_CIRCLES
        ):
            _fail(PlanarMechanicalRuleErrorCode.INTEGRITY_FAILURE, "/contract")
        return cls(
            schema_version=root["schema_version"],
            sketch_document=PlanarDocumentBinding.from_mapping(source["sketch"], "/source/sketch"),
            parametric_document=PlanarDocumentBinding.from_mapping(
                source["parametric"], "/source/parametric"
            ),
            lowering_request_sha256=binding["lowering_request_sha256"],
            adapter_contract_sha256=binding["adapter_contract_sha256"],
            body_id=selection["body_id"],
            profiles_node_id=selection["profiles_node_id"],
            add_node_id=selection["add_node_id"],
            add_result_id=selection["add_result_id"],
            final_node_id=selection["final_node_id"],
            final_result_id=selection["final_result_id"],
            depth_parameter_id=selection["depth_parameter_id"],
            depth_mm=geometry["depth_mm"],
            rectangle=PlanarRectangleProfile.from_mapping(geometry["rectangle"]),
            circles=tuple(
                PlanarCircleRemoval.from_mapping(item, f"/geometry/circles/{index}")
                for index, item in enumerate(geometry["circles"])
            ),
        )


def decode_planar_mechanical_plan(
    raw: object,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> PlanarMechanicalBackendPlan:
    if expected_content_sha256 is not None:
        expected_content_sha256 = _digest(expected_content_sha256, "/expected_content_sha256")
    if expected_plan_sha256 is not None:
        expected_plan_sha256 = _digest(expected_plan_sha256, "/expected_plan_sha256")
    mapping = _decode_mapping(raw)
    result = PlanarMechanicalBackendPlan.from_mapping(mapping)
    if type(raw) is not bytes or not hmac.compare_digest(raw, result.canonical_bytes):
        _fail(PlanarMechanicalRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_content_sha256
    ):
        _fail(PlanarMechanicalRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        result.plan_sha256, expected_plan_sha256
    ):
        _fail(PlanarMechanicalRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanarMechanicalExecutionBindings:
    """Host-authenticated target for a whole-model native transaction."""

    document: object

    def __post_init__(self) -> None:
        if self.document is None:
            _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, "/bindings/document")


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanarMechanicalConformanceReceipt:
    plan_sha256: str
    body_name: str
    outer_sketch_name: str
    pad_name: str
    circle_sketch_names: tuple[str, ...]
    pocket_names: tuple[str, ...]
    volume_mm3: float
    receipt_id: str = field(init=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/plan_sha256"))
        for name in ("body_name", "outer_sketch_name", "pad_name"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in ("circle_sketch_names", "pocket_names"):
            values = getattr(self, name)
            if type(values) is not tuple or any(type(item) is not str for item in values):
                _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, f"/{name}")
            normalized = tuple(_identifier(item, f"/{name}") for item in values)
            if len(set(normalized)) != len(normalized):
                _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, f"/{name}")
            object.__setattr__(self, name, normalized)
        if len(self.circle_sketch_names) != len(self.pocket_names):
            _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, "/pocket_names")
        object.__setattr__(
            self, "volume_mm3", _finite(self.volume_mm3, "/volume_mm3", positive=True)
        )
        body = {
            "authority": "none",
            "plan_sha256": self.plan_sha256,
            "body_name": self.body_name,
            "outer_sketch_name": self.outer_sketch_name,
            "pad_name": self.pad_name,
            "circle_sketch_names": list(self.circle_sketch_names),
            "pocket_names": list(self.pocket_names),
            "volume_mm3": self.volume_mm3,
        }
        digest = hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()
        object.__setattr__(self, "receipt_sha256", digest)
        object.__setattr__(self, "receipt_id", f"planar_mechanical_{digest[:32]}")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _shape_volume(shape: object, path: str) -> float:
    try:
        if shape is None or shape.isNull() or not shape.isValid() or len(shape.Solids) != 1:
            _fail(PlanarMechanicalRuleErrorCode.CONFORMANCE_FAILED, path)
        volume = float(shape.Volume)
    except PlanarMechanicalRuleError:
        raise
    except Exception:
        _fail(PlanarMechanicalRuleErrorCode.CONFORMANCE_FAILED, path)
    if not math.isfinite(volume) or volume <= 0.0:
        _fail(PlanarMechanicalRuleErrorCode.CONFORMANCE_FAILED, path)
    return volume


def _rectangle_points(plan: PlanarMechanicalBackendPlan) -> tuple[tuple[float, float], ...]:
    item = plan.rectangle
    cosine = math.cos(item.rotation_radians)
    sine = math.sin(item.rotation_radians)
    result = []
    for x, y in (
        (-item.half_width_mm, -item.half_height_mm),
        (item.half_width_mm, -item.half_height_mm),
        (item.half_width_mm, item.half_height_mm),
        (-item.half_width_mm, item.half_height_mm),
    ):
        result.append(
            (
                item.center_x_mm + cosine * x - sine * y,
                item.center_y_mm + sine * x + cosine * y,
            )
        )
    return tuple(result)


def _assert_engine() -> tuple[object, object, object]:
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415
        import Part  # type: ignore[import-not-found]  # noqa: PLC0415
        import Sketcher  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(PlanarMechanicalRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != PLANAR_MECHANICAL_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(PlanarMechanicalRuleErrorCode.PRECONDITION_FAILED, "/engine")
    return FreeCAD, Part, Sketcher


def apply_planar_mechanical_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: PlanarMechanicalExecutionBindings,
) -> PlanarMechanicalConformanceReceipt:
    """Create one editable Body atomically after exact plan revalidation."""

    if type(bindings) is not PlanarMechanicalExecutionBindings:
        _fail(PlanarMechanicalRuleErrorCode.INVALID_INPUT, "/bindings")
    plan = decode_planar_mechanical_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    FreeCAD, Part, Sketcher = _assert_engine()
    document = bindings.document
    token = plan.plan_sha256[:16]
    body_name = f"PM1_Body_{token}"
    outer_name = f"PM1_Outer_{token}"
    pad_name = f"PM1_Pad_{token}"
    circle_names = tuple(f"PM1_Circle_{token}_{index:02d}" for index in range(len(plan.circles)))
    pocket_names = tuple(f"PM1_Pocket_{token}_{index:02d}" for index in range(len(plan.circles)))
    all_names = (body_name, outer_name, pad_name, *circle_names, *pocket_names)
    try:
        if (
            getattr(document, "UndoMode", 0) != 1
            or bool(document.HasPendingTransaction)
            or any(document.getObject(name) is not None for name in all_names)
        ):
            _fail(PlanarMechanicalRuleErrorCode.PRECONDITION_FAILED, "/document")
        before_objects = tuple(document.Objects)
        before_visibility = tuple(bool(item.Visibility) for item in before_objects)
    except PlanarMechanicalRuleError:
        raise
    except Exception:
        _fail(PlanarMechanicalRuleErrorCode.PRECONDITION_FAILED, "/document")

    transaction_open = False
    body = outer = pad = None
    circle_sketches: list[object] = []
    pockets: list[object] = []
    try:
        document.openTransaction("VibeCAD trusted planar mechanical v1")
        transaction_open = True
        body = document.addObject("PartDesign::Body", body_name)
        outer = body.newObject("Sketcher::SketchObject", outer_name)
        points = _rectangle_points(plan)
        for index, start in enumerate(points):
            end = points[(index + 1) % len(points)]
            outer.addGeometry(
                Part.LineSegment(
                    FreeCAD.Vector(start[0], start[1], 0.0),
                    FreeCAD.Vector(end[0], end[1], 0.0),
                ),
                False,
            )
        for index in range(4):
            outer.addConstraint(Sketcher.Constraint("Coincident", index, 2, (index + 1) % 4, 1))
        document.recompute()
        pad = body.newObject("PartDesign::Pad", pad_name)
        pad.Profile = outer
        pad.Type = "Length"
        pad.Length = plan.depth_mm
        pad.Midplane = False
        pad.Reversed = False
        pad.Refine = True
        pad.AllowMultiFace = False
        document.recompute()

        previous = pad
        for index, circle in enumerate(plan.circles):
            sketch = body.newObject("Sketcher::SketchObject", circle_names[index])
            geometry_index = sketch.addGeometry(
                Part.Circle(
                    FreeCAD.Vector(circle.center_x_mm, circle.center_y_mm, 0.0),
                    FreeCAD.Vector(0.0, 0.0, 1.0),
                    circle.radius_mm,
                ),
                False,
            )
            sketch.addConstraint(Sketcher.Constraint("Radius", geometry_index, circle.radius_mm))
            document.recompute()
            pocket = body.newObject("PartDesign::Pocket", pocket_names[index])
            pocket.Profile = sketch
            pocket.Type = "ThroughAll"
            pocket.SideType = "One side"
            pocket.AlongSketchNormal = True
            pocket.UseCustomVector = False
            pocket.Offset = 0.0
            pocket.Offset2 = 0.0
            pocket.TaperAngle = 0.0
            pocket.TaperAngle2 = 0.0
            # PM1 profiles live on the body's XY origin plane while Pad grows
            # along +Z.  FreeCAD Pocket therefore needs its reviewed reversed
            # direction to cross the existing solid; ThroughAll remains the
            # backend-neutral extent selected by the graph.
            pocket.Reversed = True
            pocket.Refine = True
            document.recompute()
            if pocket.BaseFeature is not previous:
                _fail(
                    PlanarMechanicalRuleErrorCode.CONFORMANCE_FAILED,
                    f"/result/pockets/{index}/base",
                )
            circle_sketches.append(sketch)
            pockets.append(pocket)
            previous = pocket

        final_feature = pockets[-1] if pockets else pad
        volume = _shape_volume(final_feature.Shape, "/result/shape")
        tolerance = max(1e-6, plan.expected_volume_mm3 * 1e-8)
        checks = (
            (body.TypeId == "PartDesign::Body", "/result/body/type"),
            (outer.TypeId == "Sketcher::SketchObject", "/result/outer/type"),
            (pad.TypeId == "PartDesign::Pad", "/result/pad/type"),
            (body.Tip is final_feature, "/result/body/tip"),
            (pad.Profile[0] is outer, "/result/pad/profile"),
            (tuple(pad.Profile[1]) == (), "/result/pad/subelements"),
            (pad.Type == "Length", "/result/pad/extent"),
            (abs(float(pad.Length) - plan.depth_mm) <= 1e-9, "/result/pad/depth"),
            (bool(pad.isValid()), "/result/pad/valid"),
            (tuple(pad.State) == ("Up-to-date",), "/result/pad/state"),
            (
                all(item.TypeId == "Sketcher::SketchObject" for item in circle_sketches),
                "/result/circle-sketch/type",
            ),
            (
                all(item.TypeId == "PartDesign::Pocket" for item in pockets),
                "/result/pocket/type",
            ),
            (all(item.Type == "ThroughAll" for item in pockets), "/result/pocket/extent"),
            (all(bool(item.Reversed) for item in pockets), "/result/pocket/direction"),
            (all(item.isValid() for item in pockets), "/result/pocket/valid"),
            (
                all(tuple(item.State) == ("Up-to-date",) for item in pockets),
                "/result/pocket/state",
            ),
            (
                all(
                    item.Profile[0] is sketch
                    for item, sketch in zip(pockets, circle_sketches, strict=True)
                ),
                "/result/pocket/profile",
            ),
            (
                all(tuple(item.Profile[1]) == () for item in pockets),
                "/result/pocket/subelements",
            ),
            (
                abs(volume - plan.expected_volume_mm3) <= tolerance,
                "/result/volume",
            ),
        )
        for accepted, path in checks:
            if not accepted:
                _fail(PlanarMechanicalRuleErrorCode.CONFORMANCE_FAILED, path)
        document.commitTransaction()
        transaction_open = False
    except BaseException as error:
        if transaction_open:
            try:
                document.abortTransaction()
                document.recompute()
            except BaseException:
                _fail(
                    PlanarMechanicalRuleErrorCode.TRANSACTION_FAILED,
                    "/transaction/rollback",
                )
        try:
            after_objects = tuple(document.Objects)
            if (
                len(after_objects) != len(before_objects)
                or any(
                    current is not original
                    for current, original in zip(after_objects, before_objects, strict=True)
                )
                or tuple(bool(item.Visibility) for item in after_objects) != before_visibility
                or bool(document.HasPendingTransaction)
            ):
                _fail(
                    PlanarMechanicalRuleErrorCode.TRANSACTION_FAILED,
                    "/transaction/rollback",
                )
        except PlanarMechanicalRuleError:
            raise
        except Exception:
            _fail(
                PlanarMechanicalRuleErrorCode.TRANSACTION_FAILED,
                "/transaction/rollback",
            )
        if isinstance(error, KeyboardInterrupt):
            raise
        if isinstance(error, PlanarMechanicalRuleError):
            raise error
        _fail(PlanarMechanicalRuleErrorCode.TRANSACTION_FAILED, "/transaction/apply")

    return PlanarMechanicalConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        body_name=body_name,
        outer_sketch_name=outer_name,
        pad_name=pad_name,
        circle_sketch_names=circle_names,
        pocket_names=pocket_names,
        volume_mm3=volume,
    )


__all__ = [
    "MAX_PLANAR_MECHANICAL_CIRCLES",
    "MAX_PLANAR_MECHANICAL_PLAN_BYTES",
    "PLANAR_MECHANICAL_FREECAD_ENGINE_BUILD_ID",
    "PLANAR_MECHANICAL_PLAN_MEDIA_TYPE",
    "PLANAR_MECHANICAL_PLAN_SCHEMA_VERSION",
    "PLANAR_MECHANICAL_RULE_CONTRACT_SHA256",
    "PLANAR_MECHANICAL_RULE_ID",
    "PlanarCircleRemoval",
    "PlanarDocumentBinding",
    "PlanarMechanicalBackendPlan",
    "PlanarMechanicalConformanceReceipt",
    "PlanarMechanicalExecutionBindings",
    "PlanarMechanicalRuleError",
    "PlanarMechanicalRuleErrorCode",
    "PlanarRectangleProfile",
    "apply_planar_mechanical_plan",
    "decode_planar_mechanical_plan",
]
