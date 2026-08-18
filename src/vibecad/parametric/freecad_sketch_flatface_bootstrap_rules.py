"""Native rule for a reviewed Circle Sketch on a content-selected solid face.

The plan never contains a FreeCAD subelement label.  At execution time this
family resolves the unique planar face at the source solid's maximum Z,
content-binds that face, and uses the transient ``FaceN`` spelling only for the
native ``AttachmentSupport`` assignment required by FreeCAD.
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

FLATFACE_SKETCH_PLAN_SCHEMA_VERSION: Final = 1
FLATFACE_SKETCH_PLAN_MEDIA_TYPE: Final = (
    "application/vnd.vibecad.freecad-sketch-flatface-bootstrap-plan+json"
)
MAX_FLATFACE_SKETCH_PLAN_BYTES: Final = 16 * 1024
FLATFACE_SKETCH_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"
FLATFACE_SKETCH_RULE_ID: Final = "freecad.sketch.flatface-bootstrap.v1"
FLATFACE_SKETCH_NATIVE_TYPE_ID: Final = "Sketcher::SketchObject"
FLATFACE_SKETCH_NATIVE_OPERATION: Final = "CreateClosedCircleOnUniqueZMaxPlanarFace"
FLATFACE_SKETCH_CIRCLE_RADIUS_MM: Final = 1.0

_BODY_TYPE_ID: Final = "PartDesign::Body"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_TERM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,191}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_DOMAIN = b"vibecad.freecad-flatface-sketch-plan.v1\0"
_RULE_DOMAIN = b"vibecad.freecad-flatface-sketch-rule.v1\0"
_RECEIPT_DOMAIN = b"vibecad.freecad-flatface-sketch-receipt.v1\0"
_FACE_DOMAIN = b"vibecad.freecad-flatface-sketch-face.v1\0"
_STATE_DOMAIN = b"vibecad.freecad-flatface-sketch-state.v1\0"

_NATIVE_CONTRACT = (
    f"engine=FreeCAD-1.1.0/{FLATFACE_SKETCH_FREECAD_ENGINE_BUILD_ID};"
    "source-count=1;same-run-source=reviewed-solid;owner=source-body;"
    "source-precondition=body-tip-is-base;selection=unique-z-max-planar-face;"
    "selection-authority=family-owned-content-bound;plan-subelement-label=forbidden;"
    "create=Sketcher::SketchObject;MapMode=FlatFace;profile=closed-circle-radius-1mm;"
    "receipt=base-brep+face-brep+face-geometric-signature+body-group+tip+visibility;"
    "rollback=document-sequence+body-group+body-tip+visibility"
)
FLATFACE_SKETCH_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_DOMAIN + _NATIVE_CONTRACT.encode("ascii")
).hexdigest()


class FlatFaceSketchRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class FlatFaceSketchRuleError(ValueError):
    def __init__(self, code: FlatFaceSketchRuleErrorCode, path: str = "/") -> None:
        self.code = code
        self.path = path if type(path) is str and path.startswith("/") else "/"
        super().__init__(f"flatface sketch rule error ({code.value}) at {self.path}")


def _fail(code: FlatFaceSketchRuleErrorCode, path: str) -> None:
    raise FlatFaceSketchRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(FlatFaceSketchRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(FlatFaceSketchRuleErrorCode.INVALID_INPUT, path)
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
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(FlatFaceSketchRuleErrorCode.INVALID_INPUT, "/")
    if not raw or len(raw) > MAX_FLATFACE_SKETCH_PLAN_BYTES:
        _fail(FlatFaceSketchRuleErrorCode.INVALID_INPUT, "/")
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
    if type(raw) is not bytes or not raw or len(raw) > MAX_FLATFACE_SKETCH_PLAN_BYTES:
        _fail(FlatFaceSketchRuleErrorCode.INVALID_INPUT, "/")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(FlatFaceSketchRuleErrorCode.INTEGRITY_FAILURE, "/")
    except (UnicodeError, ValueError, RecursionError):
        _fail(FlatFaceSketchRuleErrorCode.INVALID_INPUT, "/")
    if type(value) is not dict or not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(FlatFaceSketchRuleErrorCode.INTEGRITY_FAILURE, "/")
    return value


def _exact(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _fail(FlatFaceSketchRuleErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class FlatFaceSketchSemanticIdentity:
    namespace: str
    vocabulary_version: str
    term_id: str
    term_definition_sha256: str

    def __post_init__(self) -> None:
        if type(self.namespace) is not str or _IDENTIFIER.fullmatch(self.namespace) is None:
            _fail(FlatFaceSketchRuleErrorCode.INVALID_INPUT, "/semantic/namespace")
        if (
            type(self.vocabulary_version) is not str
            or _VERSION.fullmatch(self.vocabulary_version) is None
        ):
            _fail(FlatFaceSketchRuleErrorCode.INVALID_INPUT, "/semantic/version")
        if type(self.term_id) is not str or _TERM.fullmatch(self.term_id) is None:
            _fail(FlatFaceSketchRuleErrorCode.INVALID_INPUT, "/semantic/term")
        object.__setattr__(
            self,
            "term_definition_sha256",
            _digest(self.term_definition_sha256, "/semantic/definition"),
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "namespace": self.namespace,
            "vocabulary_version": self.vocabulary_version,
            "term_id": self.term_id,
            "term_definition_sha256": self.term_definition_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> FlatFaceSketchSemanticIdentity:
        return cls(
            **_exact(
                value,
                {"namespace", "vocabulary_version", "term_id", "term_definition_sha256"},
                "/semantic",
            )
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FlatFaceSketchBackendPlan:
    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    manifest_sha256: str
    body_id: str
    base_node_id: str
    base_result_id: str
    node_id: str
    result_id: str
    operation_identity: FlatFaceSketchSemanticIdentity
    ownership_identity: FlatFaceSketchSemanticIdentity
    selector_identity: FlatFaceSketchSemanticIdentity
    profile_identity: FlatFaceSketchSemanticIdentity
    schema_version: int = FLATFACE_SKETCH_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(FlatFaceSketchRuleErrorCode.INVALID_INPUT, "/schema_version")
        for name in (
            "source_artifact_id",
            "source_graph_id",
            "body_id",
            "base_node_id",
            "base_result_id",
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
        if any(
            type(getattr(self, name)) is not FlatFaceSketchSemanticIdentity
            for name in (
                "operation_identity",
                "ownership_identity",
                "selector_identity",
                "profile_identity",
            )
        ):
            _fail(FlatFaceSketchRuleErrorCode.INVALID_INPUT, "/semantic")

    @property
    def source_count(self) -> int:
        return 1

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority": "none",
            "backend": {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": FLATFACE_SKETCH_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": FLATFACE_SKETCH_RULE_ID,
                "rule_contract_sha256": FLATFACE_SKETCH_RULE_CONTRACT_SHA256,
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
                "base_node_id": self.base_node_id,
                "base_result_id": self.base_result_id,
                "node_id": self.node_id,
                "result_id": self.result_id,
                "face": "unique-z-max-planar-face",
            },
            "semantic": {
                "operation": self.operation_identity.to_mapping(),
                "ownership": self.ownership_identity.to_mapping(),
                "selector": self.selector_identity.to_mapping(),
                "profile": self.profile_identity.to_mapping(),
            },
            "operation": {
                "lifecycle": "create",
                "source_count": 1,
                "owner": "source-body",
                "map_mode": "FlatFace",
                "profile": {
                    "kind": "circle",
                    "center_mm": [0.0, 0.0],
                    "radius_mm": FLATFACE_SKETCH_CIRCLE_RADIUS_MM,
                    "closed": True,
                },
            },
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_PLAN_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> FlatFaceSketchBackendPlan:
        root = _exact(
            value,
            {
                "schema_version",
                "authority",
                "backend",
                "rule",
                "source",
                "binding",
                "selection",
                "semantic",
                "operation",
            },
            "/",
        )
        backend = _exact(
            root["backend"], {"engine", "engine_version", "engine_build_id"}, "/backend"
        )
        rule = _exact(root["rule"], {"rule_id", "rule_contract_sha256", "manifest_sha256"}, "/rule")
        source = _exact(
            root["source"], {"artifact_id", "graph_id", "graph_sha256", "content_sha256"}, "/source"
        )
        binding = _exact(
            root["binding"], {"lowering_request_sha256", "adapter_contract_sha256"}, "/binding"
        )
        selection = _exact(
            root["selection"],
            {"body_id", "base_node_id", "base_result_id", "node_id", "result_id", "face"},
            "/selection",
        )
        semantic = _exact(
            root["semantic"], {"operation", "ownership", "selector", "profile"}, "/semantic"
        )
        operation = _exact(
            root["operation"],
            {"lifecycle", "source_count", "owner", "map_mode", "profile"},
            "/operation",
        )
        profile = _exact(
            operation["profile"], {"kind", "center_mm", "radius_mm", "closed"}, "/operation/profile"
        )
        if (
            root["authority"] != "none"
            or backend
            != {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": FLATFACE_SKETCH_FREECAD_ENGINE_BUILD_ID,
            }
            or rule["rule_id"] != FLATFACE_SKETCH_RULE_ID
            or rule["rule_contract_sha256"] != FLATFACE_SKETCH_RULE_CONTRACT_SHA256
            or selection["face"] != "unique-z-max-planar-face"
            or operation
            != {
                "lifecycle": "create",
                "source_count": 1,
                "owner": "source-body",
                "map_mode": "FlatFace",
                "profile": profile,
            }
            or profile
            != {"kind": "circle", "center_mm": [0.0, 0.0], "radius_mm": 1.0, "closed": True}
        ):
            _fail(FlatFaceSketchRuleErrorCode.INTEGRITY_FAILURE, "/contract")
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
            base_node_id=selection["base_node_id"],
            base_result_id=selection["base_result_id"],
            node_id=selection["node_id"],
            result_id=selection["result_id"],
            operation_identity=FlatFaceSketchSemanticIdentity.from_mapping(semantic["operation"]),
            ownership_identity=FlatFaceSketchSemanticIdentity.from_mapping(semantic["ownership"]),
            selector_identity=FlatFaceSketchSemanticIdentity.from_mapping(semantic["selector"]),
            profile_identity=FlatFaceSketchSemanticIdentity.from_mapping(semantic["profile"]),
        )


def decode_flatface_sketch_backend_plan(
    raw: object,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> FlatFaceSketchBackendPlan:
    plan = FlatFaceSketchBackendPlan.from_mapping(_decode_mapping(raw))
    if type(raw) is not bytes or not hmac.compare_digest(raw, plan.canonical_bytes):
        _fail(FlatFaceSketchRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(),
        _digest(expected_content_sha256, "/expected_content_sha256"),
    ):
        _fail(FlatFaceSketchRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        plan.plan_sha256, _digest(expected_plan_sha256, "/expected_plan_sha256")
    ):
        _fail(FlatFaceSketchRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return plan


@dataclass(frozen=True, slots=True, kw_only=True)
class FlatFaceSketchExecutionBindings:
    document: object = field(repr=False, compare=False)
    body: object = field(repr=False, compare=False)
    base: object = field(repr=False, compare=False)
    body_id: str
    base_node_id: str
    base_result_id: str

    def __post_init__(self) -> None:
        if self.document is None or self.body is None or self.base is None:
            _fail(FlatFaceSketchRuleErrorCode.INVALID_INPUT, "/bindings")
        for name in ("body_id", "base_node_id", "base_result_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/bindings/{name}"))


@dataclass(frozen=True, slots=True, kw_only=True)
class FlatFaceSelectionEvidence:
    geometric_signature_sha256: str
    face_brep_sha256: str
    base_brep_sha256: str
    area_mm2: float
    center_mm: tuple[float, float, float]
    normal: tuple[float, float, float]
    bounds_mm: tuple[float, float, float, float, float, float]

    def __post_init__(self) -> None:
        for name in ("geometric_signature_sha256", "face_brep_sha256", "base_brep_sha256"):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/selection/{name}"))
        if (
            type(self.area_mm2) is not float
            or not math.isfinite(self.area_mm2)
            or self.area_mm2 <= 0.0
            or type(self.center_mm) is not tuple
            or len(self.center_mm) != 3
            or type(self.normal) is not tuple
            or len(self.normal) != 3
            or type(self.bounds_mm) is not tuple
            or len(self.bounds_mm) != 6
            or not all(
                math.isfinite(item) for item in (*self.center_mm, *self.normal, *self.bounds_mm)
            )
        ):
            _fail(FlatFaceSketchRuleErrorCode.CONFORMANCE_FAILED, "/selection")


@dataclass(frozen=True, slots=True, kw_only=True)
class FlatFaceSketchConformanceReceipt:
    plan_sha256: str
    object_name: str
    body_name: str
    base_name: str
    prior_tip_name: str
    group_before_names: tuple[str, ...]
    group_after_names: tuple[str, ...]
    state_sha256: str
    shape_sha256: str
    geometry_sha256: str
    selection: FlatFaceSelectionEvidence
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("plan_sha256", "state_sha256", "shape_sha256", "geometry_sha256"):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        for name in ("object_name", "body_name", "base_name", "prior_tip_name"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        if (
            type(self.selection) is not FlatFaceSelectionEvidence
            or type(self.group_before_names) is not tuple
            or type(self.group_after_names) is not tuple
            or self.group_after_names != (*self.group_before_names, self.object_name)
            or self.prior_tip_name != self.base_name
        ):
            _fail(FlatFaceSketchRuleErrorCode.CONFORMANCE_FAILED, "/receipt")
        body = {
            "plan_sha256": self.plan_sha256,
            "object_name": self.object_name,
            "body_name": self.body_name,
            "base_name": self.base_name,
            "prior_tip_name": self.prior_tip_name,
            "group_before_names": list(self.group_before_names),
            "group_after_names": list(self.group_after_names),
            "state_sha256": self.state_sha256,
            "shape_sha256": self.shape_sha256,
            "geometry_sha256": self.geometry_sha256,
            "selection_sha256": self.selection.geometric_signature_sha256,
            "face_brep_sha256": self.selection.face_brep_sha256,
            "base_brep_sha256": self.selection.base_brep_sha256,
        }
        object.__setattr__(
            self,
            "receipt_sha256",
            hashlib.sha256(_RECEIPT_DOMAIN + _canonical_json(body)).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class _ObjectSnapshot:
    object: object = field(repr=False, compare=False)
    visibility: bool
    group: tuple[object, ...] | None = field(default=None, repr=False, compare=False)
    tip: object | None = field(default=None, repr=False, compare=False)


def _snapshot_document(document: object) -> tuple[tuple[object, ...], tuple[_ObjectSnapshot, ...]]:
    try:
        objects = tuple(document.Objects)
        if getattr(document, "UndoMode", 0) != 1 or bool(document.HasPendingTransaction):
            raise ValueError
        snapshots = tuple(
            _ObjectSnapshot(
                object=item,
                visibility=bool(item.Visibility),
                group=tuple(item.Group) if getattr(item, "TypeId", None) == _BODY_TYPE_ID else None,
                tip=item.Tip if getattr(item, "TypeId", None) == _BODY_TYPE_ID else None,
            )
            for item in objects
        )
    except (AttributeError, TypeError, ValueError):
        _fail(FlatFaceSketchRuleErrorCode.PRECONDITION_FAILED, "/document")
    return objects, snapshots


def _same_sequence(left: tuple[object, ...], right: tuple[object, ...]) -> bool:
    return len(left) == len(right) and all(a is b for a, b in zip(left, right, strict=True))


def _restore_document(
    document: object, before: tuple[object, ...], snapshots: tuple[_ObjectSnapshot, ...]
) -> bool:
    try:
        added = tuple(
            item for item in tuple(document.Objects) if not any(item is old for old in before)
        )
        for item in reversed(added):
            if document.getObject(item.Name) is item:
                document.removeObject(item.Name)
        for snapshot in snapshots:
            snapshot.object.Visibility = snapshot.visibility
        document.recompute()
        if not _same_sequence(tuple(document.Objects), before):
            return False
        return all(
            bool(snapshot.object.Visibility) is snapshot.visibility
            and (
                snapshot.group is None
                or (
                    _same_sequence(tuple(snapshot.object.Group), snapshot.group)
                    and snapshot.object.Tip is snapshot.tip
                )
            )
            for snapshot in snapshots
        )
    except (Exception, SystemExit):
        return False


def _brep_sha256(shape: object, path: str) -> str:
    try:
        raw = shape.exportBrepToString().encode("utf-8")
    except (Exception, SystemExit, UnicodeError):
        _fail(FlatFaceSketchRuleErrorCode.CONFORMANCE_FAILED, path)
    if not raw:
        _fail(FlatFaceSketchRuleErrorCode.CONFORMANCE_FAILED, path)
    return hashlib.sha256(raw).hexdigest()


def _face_facts(face: object, base_shape: object) -> tuple[dict[str, object], str, str]:
    try:
        bounds = face.BoundBox
        center = face.CenterOfMass
        parameters = tuple(float(item) for item in face.ParameterRange)
        normal_value = face.normalAt(
            (parameters[0] + parameters[1]) / 2.0,
            (parameters[2] + parameters[3]) / 2.0,
        )
        facts = {
            "surface_type_id": str(face.Surface.TypeId),
            "area_mm2": float(face.Area),
            "center_mm": [float(center.x), float(center.y), float(center.z)],
            "normal": [float(normal_value.x), float(normal_value.y), float(normal_value.z)],
            "bounds_mm": [
                float(bounds.XMin),
                float(bounds.XMax),
                float(bounds.YMin),
                float(bounds.YMax),
                float(bounds.ZMin),
                float(bounds.ZMax),
            ],
        }
    except (Exception, SystemExit, TypeError, ValueError, OverflowError):
        _fail(FlatFaceSketchRuleErrorCode.CONFORMANCE_FAILED, "/selection/face")
    if facts["surface_type_id"] != "Part::GeomPlane" or not all(
        math.isfinite(item)
        for item in (facts["area_mm2"], *facts["center_mm"], *facts["normal"], *facts["bounds_mm"])
    ):
        _fail(FlatFaceSketchRuleErrorCode.CONFORMANCE_FAILED, "/selection/face")
    return (
        facts,
        _brep_sha256(face, "/selection/face_brep"),
        _brep_sha256(base_shape, "/selection/base_brep"),
    )


def select_unique_zmax_planar_face(base: object) -> tuple[object, str, FlatFaceSelectionEvidence]:
    """Resolve by geometry; returned native label is transient and never receipted."""

    try:
        shape = base.Shape
        faces = tuple(shape.Faces)
        z_max = float(shape.BoundBox.ZMax)
        tolerance = max(1e-7, abs(z_max) * 1e-10)
        candidates = tuple(
            (index, face)
            for index, face in enumerate(faces, start=1)
            if str(face.Surface.TypeId) == "Part::GeomPlane"
            and abs(float(face.BoundBox.ZMin) - z_max) <= tolerance
            and abs(float(face.BoundBox.ZMax) - z_max) <= tolerance
        )
    except (Exception, SystemExit, TypeError, ValueError, OverflowError):
        _fail(FlatFaceSketchRuleErrorCode.PRECONDITION_FAILED, "/selection")
    if len(candidates) != 1:
        _fail(FlatFaceSketchRuleErrorCode.PRECONDITION_FAILED, "/selection/unique_zmax_planar_face")
    index, face = candidates[0]
    facts, face_brep, base_brep = _face_facts(face, shape)
    signature = hashlib.sha256(_FACE_DOMAIN + _canonical_json(facts)).hexdigest()
    evidence = FlatFaceSelectionEvidence(
        geometric_signature_sha256=signature,
        face_brep_sha256=face_brep,
        base_brep_sha256=base_brep,
        area_mm2=float(facts["area_mm2"]),
        center_mm=tuple(facts["center_mm"]),
        normal=tuple(facts["normal"]),
        bounds_mm=tuple(facts["bounds_mm"]),
    )
    return face, f"Face{index}", evidence


def _shape_sha256(sketch: object) -> str:
    try:
        shape = sketch.Shape
        wires = tuple(shape.Wires)
        if (
            shape.isNull()
            or not shape.isValid()
            or len(wires) != 1
            or not wires[0].isClosed()
            or tuple(sketch.OpenVertices)
        ):
            raise ValueError
    except (Exception, SystemExit, TypeError, ValueError):
        _fail(FlatFaceSketchRuleErrorCode.CONFORMANCE_FAILED, "/sketch/shape")
    return _brep_sha256(shape, "/sketch/shape")


def _geometry_sha256(sketch: object) -> str:
    try:
        item = sketch.Geometry[0]
        facts = {
            "count": int(sketch.GeometryCount),
            "type_id": str(item.TypeId),
            "center": [float(item.Center.x), float(item.Center.y), float(item.Center.z)],
            "axis": [float(item.Axis.x), float(item.Axis.y), float(item.Axis.z)],
            "radius_mm": float(item.Radius),
            "construction": bool(sketch.getConstruction(0)),
            "constraint_count": int(sketch.ConstraintCount),
            "open_vertex_count": len(tuple(sketch.OpenVertices)),
        }
    except (Exception, SystemExit, TypeError, ValueError, IndexError, OverflowError):
        _fail(FlatFaceSketchRuleErrorCode.CONFORMANCE_FAILED, "/sketch/geometry")
    if facts != {
        "count": 1,
        "type_id": "Part::GeomCircle",
        "center": [0.0, 0.0, 0.0],
        "axis": [0.0, 0.0, 1.0],
        "radius_mm": 1.0,
        "construction": False,
        "constraint_count": 0,
        "open_vertex_count": 0,
    }:
        _fail(FlatFaceSketchRuleErrorCode.CONFORMANCE_FAILED, "/sketch/geometry")
    return hashlib.sha256(_STATE_DOMAIN + _canonical_json(facts)).hexdigest()


def _state_sha256(
    body: object, base: object, sketch: object, evidence: FlatFaceSelectionEvidence
) -> str:
    try:
        group = tuple(body.Group)
        support = tuple(sketch.AttachmentSupport)
        support_object = support[0][0] if len(support) == 1 else None
        facts = {
            "body_name": str(body.Name),
            "base_name": str(base.Name),
            "sketch_name": str(sketch.Name),
            "group_names": [str(item.Name) for item in group],
            "tip_is_sketch": body.Tip is sketch,
            "base_group_index": group.index(base),
            "sketch_group_index": group.index(sketch),
            "support_is_base": support_object is base,
            "map_mode": str(sketch.MapMode),
            "base_visibility": bool(base.Visibility),
            "sketch_visibility": bool(sketch.Visibility),
            "base_brep_sha256": evidence.base_brep_sha256,
            "face_brep_sha256": evidence.face_brep_sha256,
            "face_geometric_signature_sha256": evidence.geometric_signature_sha256,
        }
    except (Exception, SystemExit, TypeError, ValueError):
        _fail(FlatFaceSketchRuleErrorCode.CONFORMANCE_FAILED, "/sketch/state")
    if (
        not facts["tip_is_sketch"]
        or not facts["support_is_base"]
        or facts["map_mode"] != "FlatFace"
    ):
        _fail(FlatFaceSketchRuleErrorCode.CONFORMANCE_FAILED, "/sketch/state")
    return hashlib.sha256(_STATE_DOMAIN + _canonical_json(facts)).hexdigest()


def apply_flatface_sketch_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: FlatFaceSketchExecutionBindings,
) -> FlatFaceSketchConformanceReceipt:
    if type(bindings) is not FlatFaceSketchExecutionBindings:
        _fail(FlatFaceSketchRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415
        import Part  # type: ignore[import-not-found]  # noqa: PLC0415
        import Sketcher  # type: ignore[import-not-found]  # noqa: F401, PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(FlatFaceSketchRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != FLATFACE_SKETCH_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(FlatFaceSketchRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_flatface_sketch_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    if (plan.body_id, plan.base_node_id, plan.base_result_id) != (
        bindings.body_id,
        bindings.base_node_id,
        bindings.base_result_id,
    ):
        _fail(FlatFaceSketchRuleErrorCode.INTEGRITY_FAILURE, "/bindings")
    document, body, base = bindings.document, bindings.body, bindings.base
    before, snapshots = _snapshot_document(document)
    try:
        group_before = tuple(body.Group)
        if (
            body.Document is not document
            or body.TypeId != _BODY_TYPE_ID
            or base.Document is not document
            or body.Tip is not base
            or not any(base is item for item in group_before)
            or base.Shape.isNull()
            or not base.Shape.isValid()
            or len(tuple(base.Shape.Solids)) != 1
        ):
            _fail(FlatFaceSketchRuleErrorCode.PRECONDITION_FAILED, "/source")
    except (AttributeError, TypeError, ValueError):
        _fail(FlatFaceSketchRuleErrorCode.PRECONDITION_FAILED, "/source")
    _face, native_face_label, evidence = select_unique_zmax_planar_face(base)
    sketch_name = f"ReviewedFlatFaceSketch_{plan.plan_sha256[:16]}"
    transaction_open = False
    try:
        if document.getObject(sketch_name) is not None:
            _fail(FlatFaceSketchRuleErrorCode.PRECONDITION_FAILED, "/document/object_name")
        document.openTransaction("VibeCAD trusted FlatFace Sketch bootstrap")
        transaction_open = True
        sketch = body.newObject(FLATFACE_SKETCH_NATIVE_TYPE_ID, sketch_name)
        sketch.AttachmentSupport = [(base, [native_face_label])]
        sketch.MapMode = "FlatFace"
        body.Tip = sketch
        index = sketch.addGeometry(
            Part.Circle(
                FreeCAD.Vector(0.0, 0.0, 0.0),
                FreeCAD.Vector(0.0, 0.0, 1.0),
                FLATFACE_SKETCH_CIRCLE_RADIUS_MM,
            ),
            False,
        )
        if type(index) is not int or index != 0:
            raise ValueError
        base.Visibility = False
        sketch.Visibility = True
        document.recompute()
        after = tuple(document.Objects)
        group_after = tuple(body.Group)
        if (
            len(after) != len(before) + 1
            or any(current is not old for current, old in zip(after[:-1], before, strict=True))
            or after[-1] is not sketch
            or group_after != (*group_before, sketch)
            or body.Tip is not sketch
        ):
            _fail(FlatFaceSketchRuleErrorCode.CONFORMANCE_FAILED, "/closure")
        current_face, _transient_label, current_evidence = select_unique_zmax_planar_face(base)
        if current_face.isSame(_face) is not True or current_evidence != evidence:
            _fail(FlatFaceSketchRuleErrorCode.CONFORMANCE_FAILED, "/selection/readback")
        shape_sha256 = _shape_sha256(sketch)
        geometry_sha256 = _geometry_sha256(sketch)
        state_sha256 = _state_sha256(body, base, sketch, evidence)
        receipt = FlatFaceSketchConformanceReceipt(
            plan_sha256=plan.plan_sha256,
            object_name=sketch.Name,
            body_name=body.Name,
            base_name=base.Name,
            prior_tip_name=base.Name,
            group_before_names=tuple(item.Name for item in group_before),
            group_after_names=tuple(item.Name for item in group_after),
            state_sha256=state_sha256,
            shape_sha256=shape_sha256,
            geometry_sha256=geometry_sha256,
            selection=evidence,
        )
        document.commitTransaction()
        transaction_open = False
        return receipt
    except KeyboardInterrupt:
        if transaction_open:
            try:
                document.abortTransaction()
            except BaseException:
                _fail(FlatFaceSketchRuleErrorCode.TRANSACTION_FAILED, "/rollback")
        if not _restore_document(document, before, snapshots):
            _fail(FlatFaceSketchRuleErrorCode.TRANSACTION_FAILED, "/rollback")
        raise
    except FlatFaceSketchRuleError:
        if transaction_open:
            try:
                document.abortTransaction()
            except BaseException:
                _fail(FlatFaceSketchRuleErrorCode.TRANSACTION_FAILED, "/rollback")
        if not _restore_document(document, before, snapshots):
            _fail(FlatFaceSketchRuleErrorCode.TRANSACTION_FAILED, "/rollback")
        raise
    except BaseException:
        if transaction_open:
            try:
                document.abortTransaction()
            except BaseException:
                _fail(FlatFaceSketchRuleErrorCode.TRANSACTION_FAILED, "/rollback")
        if not _restore_document(document, before, snapshots):
            _fail(FlatFaceSketchRuleErrorCode.TRANSACTION_FAILED, "/rollback")
        _fail(FlatFaceSketchRuleErrorCode.TRANSACTION_FAILED, "/execution")


__all__ = [
    "FLATFACE_SKETCH_CIRCLE_RADIUS_MM",
    "FLATFACE_SKETCH_FREECAD_ENGINE_BUILD_ID",
    "FLATFACE_SKETCH_NATIVE_OPERATION",
    "FLATFACE_SKETCH_NATIVE_TYPE_ID",
    "FLATFACE_SKETCH_PLAN_MEDIA_TYPE",
    "FLATFACE_SKETCH_RULE_CONTRACT_SHA256",
    "FLATFACE_SKETCH_RULE_ID",
    "FlatFaceSelectionEvidence",
    "FlatFaceSketchBackendPlan",
    "FlatFaceSketchConformanceReceipt",
    "FlatFaceSketchExecutionBindings",
    "FlatFaceSketchRuleError",
    "FlatFaceSketchRuleErrorCode",
    "FlatFaceSketchSemanticIdentity",
    "apply_flatface_sketch_plan",
    "decode_flatface_sketch_backend_plan",
    "select_unique_zmax_planar_face",
]
