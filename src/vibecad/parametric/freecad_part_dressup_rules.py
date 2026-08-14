"""Trusted FreeCAD rules for the reviewed root-level Part dress-up family.

The backend-neutral plan carries one semantic selection role.  It never carries
``EdgeN``/``FaceN`` or a native property name.  This trusted module resolves the
role against the authenticated live source shape immediately before mutation,
requires exactly one candidate, and only then applies the statically reviewed
FreeCAD 1.1.0 rule inside the shared rollback-proven transaction boundary.
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

PART_DRESSUP_PLAN_SCHEMA_VERSION: Final = 1
PART_DRESSUP_PLAN_MEDIA_TYPE: Final = (
    "application/vnd.vibecad.freecad-part-dressup-plan+json"
)
MAX_PART_DRESSUP_PLAN_BYTES: Final = 32 * 1024
PART_DRESSUP_FREECAD_ENGINE_BUILD_ID: Final = (
    "34a9716668b1ddeb55b914f1c5be644826bdbbbf"
)
MIN_DRESSUP_MAGNITUDE_MM: Final = 0.001
MAX_DRESSUP_MAGNITUDE_MM: Final = 100_000.0

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-part-dressup-plan.v1\0"
_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-part-dressup-rule.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-part-dressup-receipt.v1\0"


class PartDressupOperation(StrEnum):
    EDGE_FILLET = "edge_fillet"
    EDGE_CHAMFER = "edge_chamfer"
    FACE_THICKNESS = "face_thickness"


class PartDressupSelectionRole(StrEnum):
    OUTER_MAX_X_MAX_Y_PARALLEL_Z = "outer_max_x_max_y_parallel_z"
    OUTER_MAX_Z_PLANAR_FACE = "outer_max_z_planar_face"


@dataclass(frozen=True, slots=True)
class _NativeDressupSpec:
    type_id: str
    object_prefix: str
    properties: tuple[str, ...]
    selection_role: PartDressupSelectionRole


_NATIVE_DRESSUP_SPECS: Final = {
    PartDressupOperation.EDGE_FILLET: _NativeDressupSpec(
        "Part::Fillet",
        "Fillet",
        ("Base", "EdgeLinks", "Edges"),
        PartDressupSelectionRole.OUTER_MAX_X_MAX_Y_PARALLEL_Z,
    ),
    PartDressupOperation.EDGE_CHAMFER: _NativeDressupSpec(
        "Part::Chamfer",
        "Chamfer",
        ("Base", "EdgeLinks", "Edges"),
        PartDressupSelectionRole.OUTER_MAX_X_MAX_Y_PARALLEL_Z,
    ),
    PartDressupOperation.FACE_THICKNESS: _NativeDressupSpec(
        "Part::Thickness",
        "Thickness",
        ("Faces", "Intersection", "Join", "Mode", "SelfIntersection", "Value"),
        PartDressupSelectionRole.OUTER_MAX_Z_PLANAR_FACE,
    ),
}

PART_DRESSUP_NATIVE_TYPE_IDS: Final = {
    operation: spec.type_id for operation, spec in _NATIVE_DRESSUP_SPECS.items()
}
PART_DRESSUP_NATIVE_PROPERTIES: Final = {
    operation: spec.properties for operation, spec in _NATIVE_DRESSUP_SPECS.items()
}

PART_DRESSUP_RULE_ID: Final = "freecad.part.dressup-family.v1"
_NATIVE_CONTRACT = (
    f"engine=FreeCAD-1.1.0/{PART_DRESSUP_FREECAD_ENGINE_BUILD_ID};"
    "ops=edge-fillet:Part::Fillet,edge-chamfer:Part::Chamfer,"
    "face-thickness:Part::Thickness;"
    "edge-role=outer-max-x-max-y-parallel-z;"
    "face-role=outer-max-z-planar-face;selection=live-unique-resolve;"
    "fillet=single-edge-constant-radius;chamfer=single-edge-equal-distance;"
    "edge-links=derived-live-LinkSub;"
    "thickness=single-face,Mode:Skin,Join:Arc,Intersection:false,"
    "SelfIntersection:false;ownership=document-root;transaction=shared-rollback"
)
PART_DRESSUP_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN + _NATIVE_CONTRACT.encode("ascii")
).hexdigest()


class PartDressupRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    SELECTION_FAILED = "selection_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class PartDressupRuleError(ValueError):
    """Bounded failure from the reviewed Part dress-up native boundary."""

    def __init__(self, code: PartDressupRuleErrorCode, path: str = "/") -> None:
        if type(code) is not PartDressupRuleErrorCode:
            raise TypeError("code must be an exact PartDressupRuleErrorCode")
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
        super().__init__(f"Part dress-up rule error ({code.value}) at {path}")


def _fail(code: PartDressupRuleErrorCode, path: str) -> None:
    raise PartDressupRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(PartDressupRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(PartDressupRuleErrorCode.INVALID_INPUT, path)
    return value


def _finite(
    value: object,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if type(value) not in {int, float}:
        _fail(PartDressupRuleErrorCode.INVALID_INPUT, path)
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        _fail(PartDressupRuleErrorCode.INVALID_INPUT, path)
    if (
        not math.isfinite(result)
        or (minimum is not None and result < minimum)
        or (maximum is not None and result > maximum)
    ):
        _fail(PartDressupRuleErrorCode.INVALID_INPUT, path)
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
        _fail(PartDressupRuleErrorCode.INVALID_INPUT, "/")
    if not payload or len(payload) > MAX_PART_DRESSUP_PLAN_BYTES:
        _fail(PartDressupRuleErrorCode.INVALID_INPUT, "/")
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
    if type(raw) is not bytes or not raw or len(raw) > MAX_PART_DRESSUP_PLAN_BYTES:
        _fail(PartDressupRuleErrorCode.INVALID_INPUT, "/")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(PartDressupRuleErrorCode.INTEGRITY_FAILURE, "/")
    except (UnicodeError, ValueError, RecursionError):
        _fail(PartDressupRuleErrorCode.INVALID_INPUT, "/")
    if type(value) is not dict or not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(PartDressupRuleErrorCode.INTEGRITY_FAILURE, "/")
    return value


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if (
        type(value) is not dict
        or set(value) != keys
        or any(type(key) is not str for key in value)
    ):
        _fail(PartDressupRuleErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDressupBackendPlan:
    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    manifest_sha256: str
    container_id: str
    source_node_id: str
    source_solid_result_id: str
    source_selection_result_id: str
    semantic_reference_id: str
    target_node_id: str
    target_result_id: str
    operation: PartDressupOperation
    selection_role: PartDressupSelectionRole
    magnitude_mm: float
    schema_version: int = PART_DRESSUP_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(PartDressupRuleErrorCode.INVALID_INPUT, "/schema_version")
        for name in (
            "source_artifact_id",
            "source_graph_id",
            "container_id",
            "source_node_id",
            "source_solid_result_id",
            "source_selection_result_id",
            "semantic_reference_id",
            "target_node_id",
            "target_result_id",
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
        if type(self.operation) is not PartDressupOperation:
            _fail(PartDressupRuleErrorCode.INVALID_INPUT, "/operation")
        if type(self.selection_role) is not PartDressupSelectionRole:
            _fail(PartDressupRuleErrorCode.INVALID_INPUT, "/selection_role")
        if self.selection_role is not _NATIVE_DRESSUP_SPECS[self.operation].selection_role:
            _fail(PartDressupRuleErrorCode.INTEGRITY_FAILURE, "/selection_role")
        object.__setattr__(
            self,
            "magnitude_mm",
            _finite(
                self.magnitude_mm,
                "/magnitude_mm",
                minimum=MIN_DRESSUP_MAGNITUDE_MM,
                maximum=MAX_DRESSUP_MAGNITUDE_MM,
            ),
        )

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
                "engine_build_id": PART_DRESSUP_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": PART_DRESSUP_RULE_ID,
                "rule_contract_sha256": PART_DRESSUP_RULE_CONTRACT_SHA256,
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
                "source_node_id": self.source_node_id,
                "source_solid_result_id": self.source_solid_result_id,
                "source_selection_result_id": self.source_selection_result_id,
                "semantic_reference_id": self.semantic_reference_id,
                "target_node_id": self.target_node_id,
                "target_result_id": self.target_result_id,
                "selection_role": self.selection_role.value,
            },
            "operation": {
                "kind": self.operation.value,
                "magnitude_mm": self.magnitude_mm,
            },
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_PLAN_DIGEST_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> PartDressupBackendPlan:
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
            root["selection"],
            {
                "container_id",
                "source_node_id",
                "source_solid_result_id",
                "source_selection_result_id",
                "semantic_reference_id",
                "target_node_id",
                "target_result_id",
                "selection_role",
            },
            "/selection",
        )
        operation = _exact_fields(root["operation"], {"kind", "magnitude_mm"}, "/operation")
        if (
            root["authority"] != "none"
            or backend
            != {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PART_DRESSUP_FREECAD_ENGINE_BUILD_ID,
            }
            or rule["rule_id"] != PART_DRESSUP_RULE_ID
            or rule["rule_contract_sha256"] != PART_DRESSUP_RULE_CONTRACT_SHA256
        ):
            _fail(PartDressupRuleErrorCode.INTEGRITY_FAILURE, "/contract")
        try:
            kind = PartDressupOperation(operation["kind"])
            role = PartDressupSelectionRole(selection["selection_role"])
        except (TypeError, ValueError):
            _fail(PartDressupRuleErrorCode.INVALID_INPUT, "/operation")
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
            source_node_id=selection["source_node_id"],
            source_solid_result_id=selection["source_solid_result_id"],
            source_selection_result_id=selection["source_selection_result_id"],
            semantic_reference_id=selection["semantic_reference_id"],
            target_node_id=selection["target_node_id"],
            target_result_id=selection["target_result_id"],
            operation=kind,
            selection_role=role,
            magnitude_mm=operation["magnitude_mm"],
        )


def decode_part_dressup_backend_plan(
    raw: bytes,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> PartDressupBackendPlan:
    plan = PartDressupBackendPlan.from_mapping(_decode_mapping(raw))
    if not hmac.compare_digest(raw, plan.canonical_bytes):
        _fail(PartDressupRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(),
        _digest(expected_content_sha256, "/expected_content_sha256"),
    ):
        _fail(PartDressupRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        plan.plan_sha256,
        _digest(expected_plan_sha256, "/expected_plan_sha256"),
    ):
        _fail(PartDressupRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return plan


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDressupExecutionBindings:
    document: object
    container_id: str
    source_node_id: str
    source_solid_result_id: str
    source_object: object

    def __post_init__(self) -> None:
        if self.document is None or self.source_object is None:
            _fail(PartDressupRuleErrorCode.INVALID_INPUT, "/bindings")
        for name in ("container_id", "source_node_id", "source_solid_result_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/bindings/{name}"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDressupConformanceReceipt:
    plan_sha256: str
    operation: PartDressupOperation
    selection_role: PartDressupSelectionRole
    object_name: str
    native_type_id: str
    source_object_name: str
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/plan_sha256"))
        if type(self.operation) is not PartDressupOperation:
            _fail(PartDressupRuleErrorCode.INVALID_INPUT, "/operation")
        if self.selection_role is not _NATIVE_DRESSUP_SPECS[self.operation].selection_role:
            _fail(PartDressupRuleErrorCode.INVALID_INPUT, "/selection_role")
        for name in ("object_name", "source_object_name"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        if self.native_type_id != PART_DRESSUP_NATIVE_TYPE_IDS[self.operation]:
            _fail(PartDressupRuleErrorCode.INVALID_INPUT, "/native_type_id")
        body = {
            "plan_sha256": self.plan_sha256,
            "operation": self.operation.value,
            "selection_role": self.selection_role.value,
            "object_name": self.object_name,
            "native_type_id": self.native_type_id,
            "source_object_name": self.source_object_name,
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


def _shape_signature(item: object) -> tuple[object, ...] | None:
    if "Shape" not in tuple(item.PropertiesList):
        return None
    shape = item.Shape
    if shape.isNull():
        return (True,)
    return (
        False,
        int(shape.hashCode()),
        float(shape.Volume),
        float(shape.Area),
        float(shape.Length),
        len(tuple(shape.Vertexes)),
        len(tuple(shape.Edges)),
        len(tuple(shape.Faces)),
        len(tuple(shape.Solids)),
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
    shapes = tuple((item, _shape_signature(item)) for item in objects)
    return objects, groups, visibility, shapes


def _rollback_matches(document: object, before: object) -> bool:
    objects, groups, visibility, shapes = before
    if not _same_identity_sequence(document.Objects, objects):
        return False
    for item, members in groups:
        if not _same_identity_sequence(item.Group, members):
            return False
    try:
        return all(bool(item.Visibility) is value for item, value in visibility) and all(
            _shape_signature(item) == signature for item, signature in shapes
        )
    except Exception:
        return False


def _selection_tolerance(shape: object) -> float:
    try:
        box = shape.BoundBox
        diagonal = math.sqrt(
            float(box.XLength) ** 2 + float(box.YLength) ** 2 + float(box.ZLength) ** 2
        )
    except Exception:
        _fail(PartDressupRuleErrorCode.PRECONDITION_FAILED, "/source/shape")
    return max(1e-7, diagonal * 1e-9)


def _resolve_edge_role(shape: object) -> int:
    tolerance = _selection_tolerance(shape)
    box = shape.BoundBox
    candidates: list[int] = []
    try:
        for index, edge in enumerate(shape.Edges, 1):
            vertices = tuple(edge.Vertexes)
            if edge.Curve.TypeId != "Part::GeomLine" or len(vertices) != 2:
                continue
            first, second = (item.Point for item in vertices)
            if (
                abs(float(first.x) - float(second.x)) <= tolerance
                and abs(float(first.y) - float(second.y)) <= tolerance
                and abs(float(first.z) - float(second.z)) > tolerance
                and abs(float(first.x) - float(box.XMax)) <= tolerance
                and abs(float(second.x) - float(box.XMax)) <= tolerance
                and abs(float(first.y) - float(box.YMax)) <= tolerance
                and abs(float(second.y) - float(box.YMax)) <= tolerance
            ):
                candidates.append(index)
    except Exception:
        _fail(PartDressupRuleErrorCode.PRECONDITION_FAILED, "/source/shape")
    if len(candidates) != 1:
        _fail(PartDressupRuleErrorCode.SELECTION_FAILED, "/selection/candidates")
    return candidates[0]


def _resolve_face_role(shape: object) -> int:
    tolerance = _selection_tolerance(shape)
    normal_tolerance = 1e-9
    box = shape.BoundBox
    candidates: list[int] = []
    try:
        for index, face in enumerate(shape.Faces, 1):
            vertices = tuple(face.Vertexes)
            if face.Surface.TypeId != "Part::GeomPlane" or len(vertices) < 3:
                continue
            if not all(
                abs(float(vertex.Point.z) - float(box.ZMax)) <= tolerance
                for vertex in vertices
            ):
                continue
            u_min, u_max, v_min, v_max = face.ParameterRange
            normal = face.normalAt((u_min + u_max) / 2.0, (v_min + v_max) / 2.0)
            if (
                abs(float(normal.x)) <= normal_tolerance
                and abs(float(normal.y)) <= normal_tolerance
                and float(normal.z) >= 1.0 - normal_tolerance
            ):
                candidates.append(index)
    except Exception:
        _fail(PartDressupRuleErrorCode.PRECONDITION_FAILED, "/source/shape")
    if len(candidates) != 1:
        _fail(PartDressupRuleErrorCode.SELECTION_FAILED, "/selection/candidates")
    return candidates[0]


def _resolve_semantic_selection(
    source: object,
    role: PartDressupSelectionRole,
) -> int:
    try:
        shape = source.Shape
        if shape.isNull() or not shape.isValid() or len(tuple(shape.Solids)) != 1:
            _fail(PartDressupRuleErrorCode.PRECONDITION_FAILED, "/source/shape")
    except PartDressupRuleError:
        raise
    except Exception:
        _fail(PartDressupRuleErrorCode.PRECONDITION_FAILED, "/source/shape")
    if role is PartDressupSelectionRole.OUTER_MAX_X_MAX_Y_PARALLEL_Z:
        return _resolve_edge_role(shape)
    if role is PartDressupSelectionRole.OUTER_MAX_Z_PLANAR_FACE:
        return _resolve_face_role(shape)
    _fail(PartDressupRuleErrorCode.PRECONDITION_FAILED, "/selection/role")


def _validate_magnitude_for_shape(shape: object, magnitude_mm: float) -> None:
    try:
        lengths = tuple(
            value
            for value in (
                float(shape.BoundBox.XLength),
                float(shape.BoundBox.YLength),
                float(shape.BoundBox.ZLength),
            )
            if value > 1e-9
        )
    except Exception:
        _fail(PartDressupRuleErrorCode.PRECONDITION_FAILED, "/source/shape")
    if not lengths or magnitude_mm >= min(lengths) * 0.45:
        _fail(PartDressupRuleErrorCode.PRECONDITION_FAILED, "/operation/magnitude_mm")


def _validate_root_ownership(feature: object) -> None:
    try:
        parents = feature.getParentGroup()
        if parents not in (None, []) and tuple(parents):
            _fail(PartDressupRuleErrorCode.CONFORMANCE_FAILED, "/result/ownership")
    except PartDressupRuleError:
        raise
    except Exception:
        _fail(PartDressupRuleErrorCode.CONFORMANCE_FAILED, "/result/ownership")


def _validate_native_binding(
    feature: object,
    source: object,
    operation: PartDressupOperation,
    native_index: int,
    magnitude_mm: float,
) -> None:
    try:
        if operation in {
            PartDressupOperation.EDGE_FILLET,
            PartDressupOperation.EDGE_CHAMFER,
        }:
            edges = tuple(feature.Edges)
            edge_link_base, edge_link_names = feature.EdgeLinks
            if (
                feature.Base is not source
                or edge_link_base is not source
                or tuple(edge_link_names) != (f"Edge{native_index}",)
                or len(edges) != 1
                or int(edges[0][0]) != native_index
                or abs(float(edges[0][1]) - magnitude_mm) > 1e-9
                or abs(float(edges[0][2]) - magnitude_mm) > 1e-9
            ):
                _fail(PartDressupRuleErrorCode.CONFORMANCE_FAILED, "/result/binding")
        else:
            base, subelements = feature.Faces
            if (
                base is not source
                or tuple(subelements) != (f"Face{native_index}",)
                or abs(float(feature.Value) - magnitude_mm) > 1e-9
                or feature.Mode != "Skin"
                or feature.Join != "Arc"
                or bool(feature.Intersection)
                or bool(feature.SelfIntersection)
            ):
                _fail(PartDressupRuleErrorCode.CONFORMANCE_FAILED, "/result/binding")
    except PartDressupRuleError:
        raise
    except Exception:
        _fail(PartDressupRuleErrorCode.CONFORMANCE_FAILED, "/result/binding")


def apply_part_dressup_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: PartDressupExecutionBindings,
) -> PartDressupConformanceReceipt:
    """Execute one exact reviewed Part dress-up plan."""

    if type(bindings) is not PartDressupExecutionBindings:
        _fail(PartDressupRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(PartDressupRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != PART_DRESSUP_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(PartDressupRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_part_dressup_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    if (
        bindings.container_id != plan.container_id
        or bindings.source_node_id != plan.source_node_id
        or bindings.source_solid_result_id != plan.source_solid_result_id
    ):
        _fail(PartDressupRuleErrorCode.PRECONDITION_FAILED, "/bindings/identity")
    document = bindings.document
    source = bindings.source_object
    spec = _NATIVE_DRESSUP_SPECS[plan.operation]
    object_name = f"{spec.object_prefix}_{plan.plan_sha256[:16]}"
    try:
        if (
            source.Document is not document
            or not any(item is source for item in document.Objects)
            or document.getObject(object_name) is not None
            or bool(document.HasPendingTransaction)
            or not source.isValid()
            or tuple(source.State) != ("Up-to-date",)
        ):
            _fail(PartDressupRuleErrorCode.PRECONDITION_FAILED, "/document")
        before_objects = tuple(document.Objects)
        source_visibility = (
            bool(source.Visibility) if "Visibility" in tuple(source.PropertiesList) else None
        )
        native_index = _resolve_semantic_selection(source, plan.selection_role)
        _validate_magnitude_for_shape(source.Shape, plan.magnitude_mm)
    except PartDressupRuleError:
        raise
    except Exception:
        _fail(PartDressupRuleErrorCode.PRECONDITION_FAILED, "/document")

    feature: object | None = None

    def apply() -> object:
        nonlocal feature
        if _resolve_semantic_selection(source, plan.selection_role) != native_index:
            _fail(PartDressupRuleErrorCode.PRECONDITION_FAILED, "/selection/drift")
        feature = document.addObject(spec.type_id, object_name)
        if plan.operation in {
            PartDressupOperation.EDGE_FILLET,
            PartDressupOperation.EDGE_CHAMFER,
        }:
            feature.Base = source
            feature.Edges = [(native_index, plan.magnitude_mm, plan.magnitude_mm)]
        else:
            feature.Faces = (source, [f"Face{native_index}"])
            feature.Value = plan.magnitude_mm
            feature.Mode = "Skin"
            feature.Join = "Arc"
            feature.Intersection = False
            feature.SelfIntersection = False
        document.recompute()
        after_objects = tuple(document.Objects)
        try:
            if (
                len(after_objects) != len(before_objects) + 1
                or not _same_identity_sequence(after_objects[:-1], before_objects)
                or after_objects[-1] is not feature
                or document.getObject(object_name) is not feature
                or feature.TypeId != spec.type_id
                or feature.Document is not document
                or not feature.isValid()
                or tuple(feature.State) != ("Up-to-date",)
                or feature.Shape.isNull()
                or not feature.Shape.isValid()
                or len(tuple(feature.Shape.Solids)) != 1
                or abs(float(feature.Shape.Volume) - float(source.Shape.Volume)) <= 1e-9
                or (
                    source_visibility is not None
                    and bool(source.Visibility) is not source_visibility
                )
            ):
                _fail(PartDressupRuleErrorCode.CONFORMANCE_FAILED, "/result")
            _validate_root_ownership(feature)
            _validate_native_binding(
                feature,
                source,
                plan.operation,
                native_index,
                plan.magnitude_mm,
            )
        except PartDressupRuleError:
            raise
        except Exception:
            _fail(PartDressupRuleErrorCode.CONFORMANCE_FAILED, "/result")
        return feature

    try:
        NativeTransactionRunner().run(
            document,
            label="VibeCAD trusted Part dress-up family",
            snapshot=lambda: _snapshot(document),
            apply=apply,
            rollback_matches=lambda before: _rollback_matches(document, before),
        )
    except NativeTransactionError as error:
        _fail(PartDressupRuleErrorCode.TRANSACTION_FAILED, error.path)
    if feature is None:
        _fail(PartDressupRuleErrorCode.TRANSACTION_FAILED, "/result")
    return PartDressupConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        selection_role=plan.selection_role,
        object_name=object_name,
        native_type_id=spec.type_id,
        source_object_name=source.Name,
    )


__all__ = [
    "MAX_DRESSUP_MAGNITUDE_MM",
    "MAX_PART_DRESSUP_PLAN_BYTES",
    "MIN_DRESSUP_MAGNITUDE_MM",
    "PART_DRESSUP_FREECAD_ENGINE_BUILD_ID",
    "PART_DRESSUP_NATIVE_PROPERTIES",
    "PART_DRESSUP_NATIVE_TYPE_IDS",
    "PART_DRESSUP_PLAN_MEDIA_TYPE",
    "PART_DRESSUP_PLAN_SCHEMA_VERSION",
    "PART_DRESSUP_RULE_CONTRACT_SHA256",
    "PART_DRESSUP_RULE_ID",
    "PartDressupBackendPlan",
    "PartDressupConformanceReceipt",
    "PartDressupExecutionBindings",
    "PartDressupOperation",
    "PartDressupRuleError",
    "PartDressupRuleErrorCode",
    "PartDressupSelectionRole",
    "apply_part_dressup_plan",
    "decode_part_dressup_backend_plan",
]
