"""Trusted native rule for one reviewed, zero-source Sketch CREATE bootstrap.

The rule owns the only mapping from the reviewed bootstrap semantic to FreeCAD
native spellings.  A plan carries complete semantic identities for ownership,
support plane, and profile; it never accepts an object ``Name`` or ``TypeId``.
Execution creates one Body, its exact Origin closure, and one Body-owned Sketch
containing a single closed circle on the Body's XY plane.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

SKETCH_BOOTSTRAP_PLAN_SCHEMA_VERSION: Final = 1
SKETCH_BOOTSTRAP_PLAN_MEDIA_TYPE: Final = (
    "application/vnd.vibecad.freecad-sketch-bootstrap-plan+json"
)
MAX_SKETCH_BOOTSTRAP_PLAN_BYTES: Final = 16 * 1024
SKETCH_BOOTSTRAP_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"

SKETCH_BOOTSTRAP_RULE_ID: Final = "freecad.sketch.bootstrap-create.v1"
SKETCH_BOOTSTRAP_NATIVE_TYPE_ID: Final = "Sketcher::SketchObject"
SKETCH_BOOTSTRAP_NATIVE_OPERATION: Final = "CreateBodyOwnedClosedCircleOnXYPlane"
SKETCH_BOOTSTRAP_CIRCLE_RADIUS_MM: Final = 10.0

_BODY_TYPE_ID: Final = "PartDesign::Body"
_ORIGIN_CLOSURE: Final = (
    ("origin-container", "App::Origin"),
    ("x-axis", "App::Line"),
    ("y-axis", "App::Line"),
    ("z-axis", "App::Line"),
    ("xy-plane", "App::Plane"),
    ("xz-plane", "App::Plane"),
    ("yz-plane", "App::Plane"),
    ("origin-point", "App::Point"),
)
SKETCH_BOOTSTRAP_ORIGIN_CLOSURE_TYPE_IDS: Final = tuple(item[1] for item in _ORIGIN_CLOSURE)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_TERM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,191}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-sketch-bootstrap-plan.v1\0"
_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-sketch-bootstrap-rule.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-sketch-bootstrap-receipt.v1\0"
_STATE_DIGEST_DOMAIN = b"vibecad.freecad-sketch-bootstrap-state.v1\0"
_GEOMETRY_DIGEST_DOMAIN = b"vibecad.freecad-sketch-bootstrap-geometry.v1\0"
_CONSTRAINT_DIGEST_DOMAIN = b"vibecad.freecad-sketch-bootstrap-constraint.v1\0"

_NATIVE_CONTRACT = (
    f"engine=FreeCAD-1.1.0/{SKETCH_BOOTSTRAP_FREECAD_ENGINE_BUILD_ID};"
    "source-count=0;document.addObject=PartDesign::Body;"
    "closure=Body+App::Origin+Origin7+Sketcher::SketchObject;"
    "owner=Body;AttachmentSupport=Body.Origin.XY_Plane;MapMode=FlatFace;"
    "geometry=Part::GeomCircle(center=0,0,0;normal=0,0,1;radius=10mm);"
    "construction=false;geometry-count=1;constraint-count=0;"
    "profile=one-closed-wire/open-vertices-0;tip=Sketch;"
    "rollback=document-sequence/body-group/body-tip/visibility"
)
SKETCH_BOOTSTRAP_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN + _NATIVE_CONTRACT.encode("ascii")
).hexdigest()


class SketchBootstrapRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class SketchBootstrapRuleError(ValueError):
    """Bounded failure from the family-owned native authority."""

    def __init__(self, code: SketchBootstrapRuleErrorCode, path: str = "/") -> None:
        if type(code) is not SketchBootstrapRuleErrorCode:
            raise TypeError("code must be an exact SketchBootstrapRuleErrorCode")
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
        super().__init__(f"sketch bootstrap rule error ({code.value}) at {path}")


def _fail(code: SketchBootstrapRuleErrorCode, path: str) -> None:
    raise SketchBootstrapRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(SketchBootstrapRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(SketchBootstrapRuleErrorCode.INVALID_INPUT, path)
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
        _fail(SketchBootstrapRuleErrorCode.INVALID_INPUT, "/")
    if not raw or len(raw) > MAX_SKETCH_BOOTSTRAP_PLAN_BYTES:
        _fail(SketchBootstrapRuleErrorCode.INVALID_INPUT, "/")
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
    if type(raw) is not bytes or not raw or len(raw) > MAX_SKETCH_BOOTSTRAP_PLAN_BYTES:
        _fail(SketchBootstrapRuleErrorCode.INVALID_INPUT, "/")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(SketchBootstrapRuleErrorCode.INTEGRITY_FAILURE, "/")
    except (UnicodeError, ValueError, RecursionError):
        _fail(SketchBootstrapRuleErrorCode.INVALID_INPUT, "/")
    if type(value) is not dict or not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(SketchBootstrapRuleErrorCode.INTEGRITY_FAILURE, "/")
    return value


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(SketchBootstrapRuleErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchBootstrapSemanticIdentity:
    """Complete semantic identity; local ref ids and native names are excluded."""

    namespace: str
    vocabulary_version: str
    term_id: str
    term_definition_sha256: str

    def __post_init__(self) -> None:
        if type(self.namespace) is not str or _IDENTIFIER.fullmatch(self.namespace) is None:
            _fail(SketchBootstrapRuleErrorCode.INVALID_INPUT, "/semantic/namespace")
        if (
            type(self.vocabulary_version) is not str
            or _VERSION.fullmatch(self.vocabulary_version) is None
        ):
            _fail(SketchBootstrapRuleErrorCode.INVALID_INPUT, "/semantic/vocabulary_version")
        if type(self.term_id) is not str or _TERM.fullmatch(self.term_id) is None:
            _fail(SketchBootstrapRuleErrorCode.INVALID_INPUT, "/semantic/term_id")
        object.__setattr__(
            self,
            "term_definition_sha256",
            _digest(self.term_definition_sha256, "/semantic/term_definition_sha256"),
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "namespace": self.namespace,
            "vocabulary_version": self.vocabulary_version,
            "term_id": self.term_id,
            "term_definition_sha256": self.term_definition_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> SketchBootstrapSemanticIdentity:
        return cls(
            **_exact_fields(
                value,
                {
                    "namespace",
                    "vocabulary_version",
                    "term_id",
                    "term_definition_sha256",
                },
                "/semantic",
            )
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchBootstrapBackendPlan:
    """Canonical authority-free plan for exactly one zero-source CREATE."""

    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    body_id: str
    node_id: str
    result_id: str
    operation_identity: SketchBootstrapSemanticIdentity
    ownership_identity: SketchBootstrapSemanticIdentity
    plane_identity: SketchBootstrapSemanticIdentity
    profile_identity: SketchBootstrapSemanticIdentity
    schema_version: int = SKETCH_BOOTSTRAP_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(SketchBootstrapRuleErrorCode.INVALID_INPUT, "/schema_version")
        for name in ("source_artifact_id", "source_graph_id", "body_id", "node_id", "result_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in (
            "source_graph_sha256",
            "source_content_sha256",
            "lowering_request_sha256",
            "adapter_contract_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if any(
            type(getattr(self, name)) is not SketchBootstrapSemanticIdentity
            for name in (
                "operation_identity",
                "ownership_identity",
                "plane_identity",
                "profile_identity",
            )
        ):
            _fail(SketchBootstrapRuleErrorCode.INVALID_INPUT, "/semantic")

    @property
    def source_count(self) -> int:
        return 0

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
                "engine_build_id": SKETCH_BOOTSTRAP_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": SKETCH_BOOTSTRAP_RULE_ID,
                "rule_contract_sha256": SKETCH_BOOTSTRAP_RULE_CONTRACT_SHA256,
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
            },
            "semantic": {
                "operation": self.operation_identity.to_mapping(),
                "ownership": self.ownership_identity.to_mapping(),
                "plane": self.plane_identity.to_mapping(),
                "profile": self.profile_identity.to_mapping(),
            },
            "operation": {
                "lifecycle": "create",
                "source_count": 0,
                "owner": "body",
                "support_plane": "xy",
                "profile": {
                    "kind": "circle",
                    "center_mm": [0.0, 0.0],
                    "radius_mm": SKETCH_BOOTSTRAP_CIRCLE_RADIUS_MM,
                    "closed": True,
                },
            },
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_PLAN_DIGEST_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> SketchBootstrapBackendPlan:
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
                "semantic",
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
            root["selection"], {"body_id", "node_id", "result_id"}, "/selection"
        )
        semantic = _exact_fields(
            root["semantic"], {"operation", "ownership", "plane", "profile"}, "/semantic"
        )
        operation = _exact_fields(
            root["operation"],
            {"lifecycle", "source_count", "owner", "support_plane", "profile"},
            "/operation",
        )
        profile = _exact_fields(
            operation["profile"],
            {"kind", "center_mm", "radius_mm", "closed"},
            "/operation/profile",
        )
        if (
            root["authority"] != "none"
            or backend
            != {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": SKETCH_BOOTSTRAP_FREECAD_ENGINE_BUILD_ID,
            }
            or rule
            != {
                "rule_id": SKETCH_BOOTSTRAP_RULE_ID,
                "rule_contract_sha256": SKETCH_BOOTSTRAP_RULE_CONTRACT_SHA256,
            }
            or operation["lifecycle"] != "create"
            or operation["source_count"] != 0
            or operation["owner"] != "body"
            or operation["support_plane"] != "xy"
            or profile
            != {
                "kind": "circle",
                "center_mm": [0.0, 0.0],
                "radius_mm": SKETCH_BOOTSTRAP_CIRCLE_RADIUS_MM,
                "closed": True,
            }
        ):
            _fail(SketchBootstrapRuleErrorCode.INTEGRITY_FAILURE, "/contract")
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
            operation_identity=SketchBootstrapSemanticIdentity.from_mapping(semantic["operation"]),
            ownership_identity=SketchBootstrapSemanticIdentity.from_mapping(semantic["ownership"]),
            plane_identity=SketchBootstrapSemanticIdentity.from_mapping(semantic["plane"]),
            profile_identity=SketchBootstrapSemanticIdentity.from_mapping(semantic["profile"]),
        )


def decode_sketch_bootstrap_backend_plan(
    raw: object,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> SketchBootstrapBackendPlan:
    if expected_content_sha256 is not None:
        expected_content_sha256 = _digest(expected_content_sha256, "/expected_content_sha256")
    if expected_plan_sha256 is not None:
        expected_plan_sha256 = _digest(expected_plan_sha256, "/expected_plan_sha256")
    result = SketchBootstrapBackendPlan.from_mapping(_decode_mapping(raw))
    if type(raw) is not bytes or not hmac.compare_digest(raw, result.canonical_bytes):
        _fail(SketchBootstrapRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_content_sha256
    ):
        _fail(SketchBootstrapRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        result.plan_sha256, expected_plan_sha256
    ):
        _fail(SketchBootstrapRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchBootstrapExecutionBindings:
    document: object
    body_id: str

    def __post_init__(self) -> None:
        if self.document is None:
            _fail(SketchBootstrapRuleErrorCode.INVALID_INPUT, "/bindings/document")
        object.__setattr__(self, "body_id", _identifier(self.body_id, "/bindings/body_id"))


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchBootstrapConformanceReceipt:
    plan_sha256: str
    object_name: str
    body_name: str
    closure_names: tuple[str, ...]
    state_sha256: str
    shape_sha256: str
    geometry_sha256: str
    constraint_sha256: str
    receipt_id: str = field(init=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "plan_sha256",
            "state_sha256",
            "shape_sha256",
            "geometry_sha256",
            "constraint_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        object.__setattr__(self, "object_name", _identifier(self.object_name, "/object_name"))
        object.__setattr__(self, "body_name", _identifier(self.body_name, "/body_name"))
        if (
            type(self.closure_names) is not tuple
            or len(self.closure_names) != 10
            or any(
                type(item) is not str or _IDENTIFIER.fullmatch(item) is None
                for item in self.closure_names
            )
            or self.closure_names[-1] != self.object_name
            or self.closure_names[0] != self.body_name
        ):
            _fail(SketchBootstrapRuleErrorCode.CONFORMANCE_FAILED, "/closure_names")
        body = {
            "authority": "none",
            "plan_sha256": self.plan_sha256,
            "object_name": self.object_name,
            "body_name": self.body_name,
            "closure_names": list(self.closure_names),
            "state_sha256": self.state_sha256,
            "shape_sha256": self.shape_sha256,
            "geometry_sha256": self.geometry_sha256,
            "constraint_sha256": self.constraint_sha256,
        }
        digest = hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()
        object.__setattr__(self, "receipt_sha256", digest)
        object.__setattr__(self, "receipt_id", f"sketch_bootstrap_receipt_{digest[:32]}")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class _ObjectSnapshot:
    object: object = field(repr=False, compare=False)
    visibility: bool
    group: tuple[object, ...] | None = field(default=None, repr=False, compare=False)
    tip: object | None = field(default=None, repr=False, compare=False)


def _snapshot_document(document: object) -> tuple[tuple[object, ...], tuple[_ObjectSnapshot, ...]]:
    try:
        objects = tuple(document.Objects)
        if (
            getattr(document, "UndoMode", 0) != 1
            or bool(document.HasPendingTransaction)
            or len({id(item) for item in objects}) != len(objects)
        ):
            raise ValueError
        snapshots = tuple(
            _ObjectSnapshot(
                object=item,
                visibility=bool(item.Visibility),
                group=(
                    tuple(item.Group) if getattr(item, "TypeId", None) == _BODY_TYPE_ID else None
                ),
                tip=(item.Tip if getattr(item, "TypeId", None) == _BODY_TYPE_ID else None),
            )
            for item in objects
        )
    except (AttributeError, TypeError, ValueError):
        _fail(SketchBootstrapRuleErrorCode.PRECONDITION_FAILED, "/document")
    return objects, snapshots


def _same_sequence(left: tuple[object, ...], right: tuple[object, ...]) -> bool:
    return len(left) == len(right) and all(
        item is expected for item, expected in zip(left, right, strict=True)
    )


def _restore_document(
    document: object,
    before: tuple[object, ...],
    snapshots: tuple[_ObjectSnapshot, ...],
) -> bool:
    """Best-effort rollback plus exact readback of all frozen mutable state."""

    try:
        current = tuple(document.Objects)
        added = tuple(item for item in current if not any(item is existing for existing in before))
        bodies = tuple(item for item in added if getattr(item, "TypeId", None) == _BODY_TYPE_ID)
        residual = tuple(item for item in added if not any(item is body for body in bodies))
        for item in (*bodies, *reversed(residual)):
            name = getattr(item, "Name", None)
            if type(name) is str and document.getObject(name) is item:
                document.removeObject(name)
        for snapshot in snapshots:
            snapshot.object.Visibility = snapshot.visibility
        document.recompute()
        current = tuple(document.Objects)
        if not _same_sequence(current, before):
            return False
        for snapshot in snapshots:
            if bool(snapshot.object.Visibility) is not snapshot.visibility:
                return False
            if snapshot.group is not None and (
                not _same_sequence(tuple(snapshot.object.Group), snapshot.group)
                or snapshot.object.Tip is not snapshot.tip
            ):
                return False
        return True
    except (Exception, SystemExit):
        return False


def _origin_closure(body: object) -> tuple[object, ...]:
    try:
        origin = body.Origin
        features = tuple(origin.OriginFeatures)
        closure = (origin, *features)
        if (
            len(features) != 7
            or len(closure) != len(_ORIGIN_CLOSURE)
            or any(
                item.Document is not body.Document or item.TypeId != expected_type
                for item, (_semantic_role, expected_type) in zip(
                    closure, _ORIGIN_CLOSURE, strict=True
                )
            )
        ):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        _fail(SketchBootstrapRuleErrorCode.CONFORMANCE_FAILED, "/body/origin")
    return closure


def _shape_sha256(sketch: object) -> str:
    try:
        shape = sketch.Shape
        wires = tuple(shape.Wires)
        if (
            shape is None
            or shape.isNull()
            or not shape.isValid()
            or len(wires) != 1
            or not wires[0].isClosed()
            or len(tuple(sketch.OpenVertices)) != 0
            or len(tuple(shape.Edges)) != 1
            or tuple(shape.Faces)
            or tuple(shape.Solids)
        ):
            raise ValueError
        raw = shape.exportBrepToString().encode("utf-8")
        if not raw:
            raise ValueError
    except (AttributeError, TypeError, ValueError, UnicodeError):
        _fail(SketchBootstrapRuleErrorCode.CONFORMANCE_FAILED, "/sketch/shape")
    return hashlib.sha256(raw).hexdigest()


def _native_geometry_facts(sketch: object) -> tuple[dict[str, object], ...]:
    try:
        geometry = tuple(sketch.Geometry)
        item = geometry[0]
        center = item.Center
        axis = item.Axis
        facts = (
            {
                "type_id": str(item.TypeId),
                "center": [float(center.x), float(center.y), float(center.z)],
                "axis": [float(axis.x), float(axis.y), float(axis.z)],
                "radius_mm": float(item.Radius),
                "construction": bool(sketch.getConstruction(0)),
            },
        )
        if (
            int(sketch.GeometryCount) != 1
            or len(geometry) != 1
            or facts
            != (
                {
                    "type_id": "Part::GeomCircle",
                    "center": [0.0, 0.0, 0.0],
                    "axis": [0.0, 0.0, 1.0],
                    "radius_mm": SKETCH_BOOTSTRAP_CIRCLE_RADIUS_MM,
                    "construction": False,
                },
            )
        ):
            raise ValueError
    except (AttributeError, IndexError, TypeError, ValueError, OverflowError):
        _fail(SketchBootstrapRuleErrorCode.CONFORMANCE_FAILED, "/sketch/geometry")
    return facts


def _constraint_facts(sketch: object) -> tuple[object, ...]:
    try:
        constraints = tuple(sketch.Constraints)
        if int(sketch.ConstraintCount) != 0 or constraints:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        _fail(SketchBootstrapRuleErrorCode.CONFORMANCE_FAILED, "/sketch/constraints")
    return constraints


def _canonical_digest(domain: bytes, value: object) -> str:
    return hashlib.sha256(domain + _canonical_json(value)).hexdigest()


def _state_digest(body: object, sketch: object, xy_plane: object) -> str:
    def support_matches(value: object) -> bool:
        try:
            raw = tuple(value)
            if len(raw) == 2 and raw[0] is xy_plane:
                return tuple(raw[1]) in {(), ("",)}
            if len(raw) == 1:
                pair = tuple(raw[0])
                return len(pair) == 2 and pair[0] is xy_plane and tuple(pair[1]) in {(), ("",)}
        except (TypeError, ValueError):
            return False
        return False

    try:
        if not (
            support_matches(getattr(sketch, "Support", ()))
            or support_matches(getattr(sketch, "AttachmentSupport", ()))
        ):
            raise ValueError
        state = {
            "state": [str(item) for item in tuple(sketch.State)],
            "owner_group_index": tuple(body.Group).index(sketch),
            "tip_is_sketch": body.Tip is sketch,
            "map_mode": str(sketch.MapMode),
            "support_identity": "body-origin-xy-plane",
            "geometry_count": int(sketch.GeometryCount),
            "constraint_count": int(sketch.ConstraintCount),
            "open_vertex_count": len(tuple(sketch.OpenVertices)),
            "visibility": bool(sketch.Visibility),
        }
    except (AttributeError, TypeError, ValueError):
        _fail(SketchBootstrapRuleErrorCode.CONFORMANCE_FAILED, "/sketch/state")
    if (
        state["owner_group_index"] != 0
        or state["tip_is_sketch"] is not True
        or state["map_mode"] != "FlatFace"
        or state["geometry_count"] != 1
        or state["constraint_count"] != 0
        or state["open_vertex_count"] != 0
    ):
        _fail(SketchBootstrapRuleErrorCode.CONFORMANCE_FAILED, "/sketch/state")
    return _canonical_digest(_STATE_DIGEST_DOMAIN, state)


def _validate_created_closure(
    document: object,
    before: tuple[object, ...],
    body: object,
    sketch: object,
) -> tuple[object, ...]:
    origin = _origin_closure(body)
    expected = (body, *origin, sketch)
    try:
        current = tuple(document.Objects)
        added = tuple(item for item in current if not any(item is existing for existing in before))
        if (
            not _same_sequence(added, expected)
            or body.Document is not document
            or body.TypeId != _BODY_TYPE_ID
            or sketch.Document is not document
            or sketch.TypeId != SKETCH_BOOTSTRAP_NATIVE_TYPE_ID
            or tuple(body.Group) != (sketch,)
            or body.Tip is not sketch
        ):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        _fail(SketchBootstrapRuleErrorCode.CONFORMANCE_FAILED, "/closure")
    return expected


def apply_sketch_bootstrap_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: SketchBootstrapExecutionBindings,
) -> SketchBootstrapConformanceReceipt:
    """Execute the single native CREATE after exact plan/runtime validation."""

    if type(bindings) is not SketchBootstrapExecutionBindings:
        _fail(SketchBootstrapRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415
        import Part  # type: ignore[import-not-found]  # noqa: PLC0415
        import Sketcher  # type: ignore[import-not-found]  # noqa: F401, PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(SketchBootstrapRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != SKETCH_BOOTSTRAP_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(SketchBootstrapRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_sketch_bootstrap_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    if plan.body_id != bindings.body_id:
        _fail(SketchBootstrapRuleErrorCode.INTEGRITY_FAILURE, "/bindings/body_id")
    document = bindings.document
    before, snapshots = _snapshot_document(document)
    body_name = f"ReviewedSketchBody_{plan.plan_sha256[:16]}"
    sketch_name = f"ReviewedSketch_{plan.plan_sha256[:16]}"
    transaction_open = False
    try:
        if document.getObject(body_name) is not None or document.getObject(sketch_name) is not None:
            _fail(SketchBootstrapRuleErrorCode.PRECONDITION_FAILED, "/document/object_name")
        document.openTransaction("VibeCAD trusted Sketch bootstrap CREATE")
        transaction_open = True
        body = document.addObject(_BODY_TYPE_ID, body_name)
        origin = _origin_closure(body)
        xy_plane = origin[4]
        sketch = body.newObject(SKETCH_BOOTSTRAP_NATIVE_TYPE_ID, sketch_name)
        sketch.AttachmentSupport = [(xy_plane, [""])]
        sketch.MapMode = "FlatFace"
        body.Tip = sketch
        index = sketch.addGeometry(
            Part.Circle(
                FreeCAD.Vector(0.0, 0.0, 0.0),
                FreeCAD.Vector(0.0, 0.0, 1.0),
                SKETCH_BOOTSTRAP_CIRCLE_RADIUS_MM,
            ),
            False,
        )
        if type(index) is not int or index != 0:
            raise ValueError
        document.recompute()
        closure = _validate_created_closure(document, before, body, sketch)
        shape_sha256 = _shape_sha256(sketch)
        geometry_sha256 = _canonical_digest(_GEOMETRY_DIGEST_DOMAIN, _native_geometry_facts(sketch))
        constraint_sha256 = _canonical_digest(_CONSTRAINT_DIGEST_DOMAIN, _constraint_facts(sketch))
        state_sha256 = _state_digest(body, sketch, xy_plane)
        receipt = SketchBootstrapConformanceReceipt(
            plan_sha256=plan.plan_sha256,
            object_name=sketch.Name,
            body_name=body.Name,
            closure_names=tuple(item.Name for item in closure),
            state_sha256=state_sha256,
            shape_sha256=shape_sha256,
            geometry_sha256=geometry_sha256,
            constraint_sha256=constraint_sha256,
        )
        document.commitTransaction()
        transaction_open = False
        return receipt
    except KeyboardInterrupt:
        if transaction_open:
            try:
                document.abortTransaction()
            except BaseException:
                _fail(SketchBootstrapRuleErrorCode.TRANSACTION_FAILED, "/rollback")
        if not _restore_document(document, before, snapshots):
            _fail(SketchBootstrapRuleErrorCode.TRANSACTION_FAILED, "/rollback")
        raise
    except SketchBootstrapRuleError:
        if transaction_open:
            try:
                document.abortTransaction()
            except BaseException:
                _fail(SketchBootstrapRuleErrorCode.TRANSACTION_FAILED, "/rollback")
        if not _restore_document(document, before, snapshots):
            _fail(SketchBootstrapRuleErrorCode.TRANSACTION_FAILED, "/rollback")
        raise
    except BaseException:
        if transaction_open:
            try:
                document.abortTransaction()
            except BaseException:
                _fail(SketchBootstrapRuleErrorCode.TRANSACTION_FAILED, "/rollback")
        if not _restore_document(document, before, snapshots):
            _fail(SketchBootstrapRuleErrorCode.TRANSACTION_FAILED, "/rollback")
        _fail(SketchBootstrapRuleErrorCode.TRANSACTION_FAILED, "/execution")


__all__ = [
    "MAX_SKETCH_BOOTSTRAP_PLAN_BYTES",
    "SKETCH_BOOTSTRAP_CIRCLE_RADIUS_MM",
    "SKETCH_BOOTSTRAP_FREECAD_ENGINE_BUILD_ID",
    "SKETCH_BOOTSTRAP_NATIVE_OPERATION",
    "SKETCH_BOOTSTRAP_NATIVE_TYPE_ID",
    "SKETCH_BOOTSTRAP_ORIGIN_CLOSURE_TYPE_IDS",
    "SKETCH_BOOTSTRAP_PLAN_MEDIA_TYPE",
    "SKETCH_BOOTSTRAP_RULE_CONTRACT_SHA256",
    "SKETCH_BOOTSTRAP_RULE_ID",
    "SketchBootstrapBackendPlan",
    "SketchBootstrapConformanceReceipt",
    "SketchBootstrapExecutionBindings",
    "SketchBootstrapRuleError",
    "SketchBootstrapRuleErrorCode",
    "SketchBootstrapSemanticIdentity",
    "apply_sketch_bootstrap_plan",
    "decode_sketch_bootstrap_backend_plan",
]
