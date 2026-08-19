"""Trusted native rules for one bounded PartDesign reference-object family.

The backend-neutral graph never chooses a FreeCAD TypeId, property name, or
topological FaceN spelling. A canonical plan selects only one reviewed
semantic operation and binds an opaque semantic support receipt. The trusted
host supplies the already-authenticated live support object and subelement.

Plans and conformance receipts are evidence only. Native mutation happens
only through apply_partdesign_reference_plan, after exact runtime, content,
semantic identity, and live-object checks.
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

from vibecad.parametric.freecad_partdesign_dressup_transform_rules import (
    Axis,
    AxisAlignedEdgeRole,
    AxisAlignedFaceRole,
    Side,
)
from vibecad.parametric.freecad_partdesign_dressup_transform_rules import (
    _bounds as _axis_aligned_bounds,  # noqa: PLC2701
)
from vibecad.parametric.freecad_partdesign_dressup_transform_rules import (
    _coordinates as _axis_aligned_coordinates,  # noqa: PLC2701
)
from vibecad.parametric.freecad_partdesign_dressup_transform_rules import (
    _resolve_edge as _resolve_axis_aligned_edge,  # noqa: PLC2701
)
from vibecad.parametric.freecad_partdesign_dressup_transform_rules import (
    _resolve_face as _resolve_axis_aligned_face,  # noqa: PLC2701
)

REFERENCE_PLAN_SCHEMA_VERSION: Final = 1
REFERENCE_PLAN_MEDIA_TYPE: Final = "application/vnd.vibecad.freecad-partdesign-reference-plan+json"
MAX_REFERENCE_PLAN_BYTES: Final = 16 * 1024
REFERENCE_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FACE = re.compile(r"Face([1-9][0-9]*)\Z")
_EDGE = re.compile(r"Edge([1-9][0-9]*)\Z")
_VERTEX = re.compile(r"Vertex([1-9][0-9]*)\Z")
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-reference-plan.v1\0"
_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-partdesign-reference-rule.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-reference-conformance-receipt.v1\0"
_SELECTION_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-reference-selection-receipt.v1\0"
_GEOMETRIC_SIGNATURE_DIGEST_DOMAIN = (
    b"vibecad.freecad-partdesign-reference-geometric-signature.v1\0"
)

REFERENCE_RULE_ID: Final = "freecad.partdesign.reference-family.v1"
_NATIVE_CONTRACT = (
    "engine=FreeCAD-1.1.0/"
    f"{REFERENCE_FREECAD_ENGINE_BUILD_ID};"
    "datum-plane=PartDesign::Plane/AttachmentSupport/FlatFace/Face;"
    "datum-line=PartDesign::Line/AttachmentSupport/Tangent/Edge;"
    "datum-point=PartDesign::Point/AttachmentSupport/Vertex/Vertex;"
    "shape-binder=PartDesign::ShapeBinder/Support/whole/TraceSupport=false;"
    "subshape-binder=PartDesign::SubShapeBinder/Support/single-subelement/"
    "BindMode=Synchronized/Relative=true/PartialLoad=false/Fuse=false/"
    "MakeFace=true;"
    "same-document=true;body-group=true;preserve-tip=true;"
    "host-authenticated-support=true;transaction=rollback"
)
REFERENCE_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN + _NATIVE_CONTRACT.encode("ascii")
).hexdigest()

REFERENCE_REVIEWED_SELECTION_RULE_ID: Final = (
    "freecad.partdesign.reference-family.reviewed-selection.v2"
)
_REVIEWED_SELECTION_NATIVE_CONTRACT = (
    _NATIVE_CONTRACT
    + ";same-run-authenticated-target-body=true"
    + ";content-bound-selection-receipt=true"
    + ";unique-semantic-role-resolution=true"
    + ";durable-native-subelement-names=false"
)
REFERENCE_REVIEWED_SELECTION_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN + _REVIEWED_SELECTION_NATIVE_CONTRACT.encode("ascii")
).hexdigest()


class PartDesignReferenceKind(StrEnum):
    DATUM_PLANE = "datum_plane"
    DATUM_LINE = "datum_line"
    DATUM_POINT = "datum_point"
    SHAPE_BINDER = "shape_binder"
    SUBSHAPE_BINDER = "subshape_binder"


class ReviewedSubelementKind(StrEnum):
    FACE = "face"
    EDGE = "edge"
    VERTEX = "vertex"
    WHOLE_OBJECT = "whole_object"


class ReviewedReferenceSemanticRole(StrEnum):
    AXIS_ALIGNED_FACE_Z_POSITIVE = "axis_aligned_face.z.positive"
    AXIS_ALIGNED_EDGE_X_Y_NEGATIVE_Z_POSITIVE = "axis_aligned_edge.x.y_negative.z_positive"
    AXIS_ALIGNED_VERTEX_X_POSITIVE_Y_POSITIVE_Z_POSITIVE = (
        "axis_aligned_vertex.x_positive.y_positive.z_positive"
    )
    WHOLE_OBJECT = "whole_object"


class ReferenceRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class ReferenceRuleError(ValueError):
    """Bounded, non-reflective failure from the trusted native rule."""

    def __init__(self, code: ReferenceRuleErrorCode, path: str = "/") -> None:
        if type(code) is not ReferenceRuleErrorCode:
            raise TypeError("code must be a ReferenceRuleErrorCode")
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
        super().__init__(f"reference rule error ({code.value}) at {path}")


def _fail(code: ReferenceRuleErrorCode, path: str) -> None:
    raise ReferenceRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(ReferenceRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(ReferenceRuleErrorCode.INVALID_INPUT, path)
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
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(ReferenceRuleErrorCode.INVALID_INPUT, "/")
    if not raw or len(raw) > MAX_REFERENCE_PLAN_BYTES:
        _fail(ReferenceRuleErrorCode.INVALID_INPUT, "/")
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
    if type(raw) is not bytes or not raw or len(raw) > MAX_REFERENCE_PLAN_BYTES:
        _fail(ReferenceRuleErrorCode.INVALID_INPUT, "/")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(ReferenceRuleErrorCode.INTEGRITY_FAILURE, "/")
    except (UnicodeError, ValueError, RecursionError):
        _fail(ReferenceRuleErrorCode.INVALID_INPUT, "/")
    if type(value) is not dict or not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(ReferenceRuleErrorCode.INTEGRITY_FAILURE, "/")
    return value


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(ReferenceRuleErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignReferencePlan:
    """Canonical, authority-free plan for one reviewed reference operation."""

    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    body_id: str
    node_id: str
    result_id: str
    support_reference_id: str
    support_reference_sha256: str
    kind: PartDesignReferenceKind
    schema_version: int = REFERENCE_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(ReferenceRuleErrorCode.INVALID_INPUT, "/schema_version")
        for name in (
            "source_artifact_id",
            "source_graph_id",
            "body_id",
            "node_id",
            "result_id",
            "support_reference_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in (
            "source_graph_sha256",
            "source_content_sha256",
            "lowering_request_sha256",
            "adapter_contract_sha256",
            "support_reference_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if type(self.kind) is not PartDesignReferenceKind:
            _fail(ReferenceRuleErrorCode.INVALID_INPUT, "/kind")

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
                "engine_build_id": REFERENCE_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": REFERENCE_RULE_ID,
                "rule_contract_sha256": REFERENCE_RULE_CONTRACT_SHA256,
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
                "support_reference_id": self.support_reference_id,
                "support_reference_sha256": self.support_reference_sha256,
            },
            "operation": {"kind": self.kind.value},
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_PLAN_DIGEST_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> PartDesignReferencePlan:
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
                "support_reference_id",
                "support_reference_sha256",
            },
            "/selection",
        )
        operation = _exact_fields(root["operation"], {"kind"}, "/operation")
        if (
            root["authority"] != "none"
            or backend
            != {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": REFERENCE_FREECAD_ENGINE_BUILD_ID,
            }
            or rule
            != {
                "rule_id": REFERENCE_RULE_ID,
                "rule_contract_sha256": REFERENCE_RULE_CONTRACT_SHA256,
            }
        ):
            _fail(ReferenceRuleErrorCode.INTEGRITY_FAILURE, "/contract")
        try:
            kind = PartDesignReferenceKind(operation["kind"])
        except (TypeError, ValueError):
            _fail(ReferenceRuleErrorCode.INTEGRITY_FAILURE, "/operation/kind")
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
            support_reference_id=selection["support_reference_id"],
            support_reference_sha256=selection["support_reference_sha256"],
            kind=kind,
        )


def decode_partdesign_reference_plan(
    raw: object,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> PartDesignReferencePlan:
    if expected_content_sha256 is not None:
        expected_content_sha256 = _digest(expected_content_sha256, "/expected_content_sha256")
    if expected_plan_sha256 is not None:
        expected_plan_sha256 = _digest(expected_plan_sha256, "/expected_plan_sha256")
    result = PartDesignReferencePlan.from_mapping(_decode_mapping(raw))
    if type(raw) is not bytes or not hmac.compare_digest(raw, result.canonical_bytes):
        _fail(ReferenceRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_content_sha256
    ):
        _fail(ReferenceRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        result.plan_sha256, expected_plan_sha256
    ):
        _fail(ReferenceRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedSubelementSelectionReceipt:
    """Engine-owned proof of one unique live semantic subelement selection."""

    reference_plan_sha256: str
    reference_plan_content_sha256: str
    source_plan_sha256: str
    source_plan_content_sha256: str
    source_native_receipt_sha256: str
    target_body_entity_identity_sha256: str
    support_entity_identity_sha256: str
    source_shape_sha256: str
    subelement_kind: ReviewedSubelementKind
    semantic_role: ReviewedReferenceSemanticRole
    geometric_signature_sha256: str
    support_subname: str = field(repr=False)
    receipt_id: str = field(init=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "reference_plan_sha256",
            "reference_plan_content_sha256",
            "source_plan_sha256",
            "source_plan_content_sha256",
            "source_native_receipt_sha256",
            "target_body_entity_identity_sha256",
            "support_entity_identity_sha256",
            "source_shape_sha256",
            "geometric_signature_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/selection/{name}"))
        if type(self.subelement_kind) is not ReviewedSubelementKind:
            _fail(ReferenceRuleErrorCode.INVALID_INPUT, "/selection/subelement_kind")
        if type(self.semantic_role) is not ReviewedReferenceSemanticRole:
            _fail(ReferenceRuleErrorCode.INVALID_INPUT, "/selection/semantic_role")
        if (
            type(self.support_subname) is not str
            or len(self.support_subname) > 32
            or not self.support_subname.isascii()
            or not self.support_subname.isprintable()
        ):
            _fail(ReferenceRuleErrorCode.INVALID_INPUT, "/selection/support_subname")
        expected_prefix = {
            ReviewedSubelementKind.FACE: "Face",
            ReviewedSubelementKind.EDGE: "Edge",
            ReviewedSubelementKind.VERTEX: "Vertex",
            ReviewedSubelementKind.WHOLE_OBJECT: "",
        }[self.subelement_kind]
        if (
            self.subelement_kind is ReviewedSubelementKind.WHOLE_OBJECT
            and self.support_subname != ""
        ) or (
            self.subelement_kind is not ReviewedSubelementKind.WHOLE_OBJECT
            and not self.support_subname.startswith(expected_prefix)
        ):
            _fail(ReferenceRuleErrorCode.INVALID_INPUT, "/selection/support_subname")
        body = {
            "authority": "none",
            "reference_plan_sha256": self.reference_plan_sha256,
            "reference_plan_content_sha256": self.reference_plan_content_sha256,
            "source_execution": {
                "plan_sha256": self.source_plan_sha256,
                "plan_content_sha256": self.source_plan_content_sha256,
                "native_receipt_sha256": self.source_native_receipt_sha256,
            },
            "entity_identities": {
                "target_body_sha256": self.target_body_entity_identity_sha256,
                "support_sha256": self.support_entity_identity_sha256,
            },
            "source_shape_sha256": self.source_shape_sha256,
            "subelement_kind": self.subelement_kind.value,
            "semantic_role": self.semantic_role.value,
            "geometric_signature_sha256": self.geometric_signature_sha256,
            "transient_support_subname": self.support_subname,
        }
        digest = hashlib.sha256(
            _SELECTION_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)
        ).hexdigest()
        object.__setattr__(self, "receipt_sha256", digest)
        object.__setattr__(self, "receipt_id", f"reference_selection_{digest[:32]}")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferenceExecutionBindings:
    """Host-authenticated semantic support resolved to one live FreeCAD object."""

    document: object
    body: object
    support: object
    body_id: str
    support_reference_id: str
    support_reference_sha256: str
    target_body_entity_identity_sha256: str
    support_entity_identity_sha256: str
    selection_receipt: ReviewedSubelementSelectionReceipt

    def __post_init__(self) -> None:
        object.__setattr__(self, "body_id", _identifier(self.body_id, "/body_id"))
        object.__setattr__(
            self,
            "support_reference_id",
            _identifier(self.support_reference_id, "/support_reference_id"),
        )
        object.__setattr__(
            self,
            "support_reference_sha256",
            _digest(self.support_reference_sha256, "/support_reference_sha256"),
        )
        for name in (
            "target_body_entity_identity_sha256",
            "support_entity_identity_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if self.document is None or self.body is None or self.support is None:
            _fail(ReferenceRuleErrorCode.INVALID_INPUT, "/bindings")
        if type(self.selection_receipt) is not ReviewedSubelementSelectionReceipt:
            _fail(ReferenceRuleErrorCode.INVALID_INPUT, "/selection_receipt")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferenceConformanceReceipt:
    plan_sha256: str
    object_name: str
    kind: PartDesignReferenceKind
    support_subname: str
    selection_receipt_sha256: str
    face_count: int
    edge_count: int
    vertex_count: int
    receipt_id: str = field(init=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/plan_sha256"))
        object.__setattr__(self, "object_name", _identifier(self.object_name, "/object_name"))
        if type(self.kind) is not PartDesignReferenceKind:
            _fail(ReferenceRuleErrorCode.CONFORMANCE_FAILED, "/receipt/kind")
        object.__setattr__(
            self,
            "selection_receipt_sha256",
            _digest(self.selection_receipt_sha256, "/receipt/selection_receipt_sha256"),
        )
        if (
            type(self.support_subname) is not str
            or len(self.support_subname) > 32
            or not self.support_subname.isascii()
            or not self.support_subname.isprintable()
        ):
            _fail(ReferenceRuleErrorCode.CONFORMANCE_FAILED, "/receipt/support_subname")
        for name in ("face_count", "edge_count", "vertex_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0 or value > 1_000_000:
                _fail(ReferenceRuleErrorCode.CONFORMANCE_FAILED, f"/receipt/{name}")
        body = {
            "authority": "none",
            "plan_sha256": self.plan_sha256,
            "object_name": self.object_name,
            "kind": self.kind.value,
            "support_subname": self.support_subname,
            "selection_receipt_sha256": self.selection_receipt_sha256,
            "topology": {
                "faces": self.face_count,
                "edges": self.edge_count,
                "vertices": self.vertex_count,
            },
        }
        digest = hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()
        object.__setattr__(self, "receipt_sha256", digest)
        object.__setattr__(self, "receipt_id", f"reference_conformance_{digest[:32]}")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


_NATIVE_PROFILE: Final = {
    PartDesignReferenceKind.DATUM_PLANE: (
        "PartDesign::Plane",
        "AttachmentSupport",
        "FlatFace",
        _FACE,
    ),
    PartDesignReferenceKind.DATUM_LINE: (
        "PartDesign::Line",
        "AttachmentSupport",
        "Tangent",
        _EDGE,
    ),
    PartDesignReferenceKind.DATUM_POINT: (
        "PartDesign::Point",
        "AttachmentSupport",
        "Vertex",
        _VERTEX,
    ),
    PartDesignReferenceKind.SHAPE_BINDER: (
        "PartDesign::ShapeBinder",
        "Support",
        None,
        None,
    ),
    PartDesignReferenceKind.SUBSHAPE_BINDER: (
        "PartDesign::SubShapeBinder",
        "Support",
        None,
        (_FACE, _EDGE, _VERTEX),
    ),
}

_REFERENCE_SELECTION_PROFILE: Final = {
    PartDesignReferenceKind.DATUM_PLANE: (
        ReviewedSubelementKind.FACE,
        ReviewedReferenceSemanticRole.AXIS_ALIGNED_FACE_Z_POSITIVE,
    ),
    PartDesignReferenceKind.DATUM_LINE: (
        ReviewedSubelementKind.EDGE,
        ReviewedReferenceSemanticRole.AXIS_ALIGNED_EDGE_X_Y_NEGATIVE_Z_POSITIVE,
    ),
    PartDesignReferenceKind.DATUM_POINT: (
        ReviewedSubelementKind.VERTEX,
        ReviewedReferenceSemanticRole.AXIS_ALIGNED_VERTEX_X_POSITIVE_Y_POSITIVE_Z_POSITIVE,
    ),
    PartDesignReferenceKind.SHAPE_BINDER: (
        ReviewedSubelementKind.WHOLE_OBJECT,
        ReviewedReferenceSemanticRole.WHOLE_OBJECT,
    ),
    PartDesignReferenceKind.SUBSHAPE_BINDER: (
        ReviewedSubelementKind.FACE,
        ReviewedReferenceSemanticRole.AXIS_ALIGNED_FACE_Z_POSITIVE,
    ),
}


def _shape_sha256(shape: object, path: str) -> str:
    try:
        raw = shape.exportBrepToString().encode("utf-8")
    except (Exception, SystemExit):
        _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, path)
    if not raw:
        _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, path)
    return hashlib.sha256(raw).hexdigest()


def _resolve_positive_vertex(shape: object) -> str:
    try:
        _minimum, maximum, tolerance = _axis_aligned_bounds(shape)
        vertices = tuple(shape.Vertexes)
        candidates = tuple(
            index
            for index, vertex in enumerate(vertices, start=1)
            if all(
                abs(actual - expected) <= tolerance
                for actual, expected in zip(
                    _axis_aligned_coordinates(vertex.Point), maximum, strict=True
                )
            )
        )
    except (Exception, SystemExit):
        _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, "/selection/semantic_role")
    if len(candidates) != 1:
        _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, "/selection/ambiguous")
    return f"Vertex{candidates[0]}"


def _resolve_semantic_selection(
    shape: object,
    kind: ReviewedSubelementKind,
    role: ReviewedReferenceSemanticRole,
) -> tuple[str, object]:
    try:
        if (
            kind is ReviewedSubelementKind.FACE
            and role is ReviewedReferenceSemanticRole.AXIS_ALIGNED_FACE_Z_POSITIVE
        ):
            subname = _resolve_axis_aligned_face(
                shape,
                AxisAlignedFaceRole(axis=Axis.Z, side=Side.MAXIMUM),
            )
            selected = shape.Faces[int(subname.removeprefix("Face")) - 1]
        elif (
            kind is ReviewedSubelementKind.EDGE
            and role is ReviewedReferenceSemanticRole.AXIS_ALIGNED_EDGE_X_Y_NEGATIVE_Z_POSITIVE
        ):
            subname = _resolve_axis_aligned_edge(
                shape,
                AxisAlignedEdgeRole(
                    axis=Axis.X,
                    first_side=Side.MINIMUM,
                    second_side=Side.MAXIMUM,
                ),
            )
            selected = shape.Edges[int(subname.removeprefix("Edge")) - 1]
        elif (
            kind is ReviewedSubelementKind.VERTEX
            and role
            is ReviewedReferenceSemanticRole.AXIS_ALIGNED_VERTEX_X_POSITIVE_Y_POSITIVE_Z_POSITIVE
        ):
            subname = _resolve_positive_vertex(shape)
            selected = shape.Vertexes[int(subname.removeprefix("Vertex")) - 1]
        elif (
            kind is ReviewedSubelementKind.WHOLE_OBJECT
            and role is ReviewedReferenceSemanticRole.WHOLE_OBJECT
        ):
            subname = ""
            selected = shape
        else:
            _fail(ReferenceRuleErrorCode.INTEGRITY_FAILURE, "/selection/contract")
    except ReferenceRuleError:
        raise
    except (Exception, SystemExit):
        _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, "/selection/semantic_role")
    return subname, selected


def _geometric_signature_sha256(
    *,
    source_shape_sha256: str,
    kind: ReviewedSubelementKind,
    role: ReviewedReferenceSemanticRole,
    selected: object,
) -> str:
    selected_sha256 = _shape_sha256(selected, "/selection/geometric_signature")
    payload = _canonical_json(
        {
            "schema_version": 1,
            "source_shape_sha256": source_shape_sha256,
            "subelement_kind": kind.value,
            "semantic_role": role.value,
            "selected_shape_sha256": selected_sha256,
        }
    )
    return hashlib.sha256(_GEOMETRIC_SIGNATURE_DIGEST_DOMAIN + payload).hexdigest()


def locate_reviewed_reference_subelement(
    *,
    plan: PartDesignReferencePlan,
    reference_plan_content_sha256: str,
    source_shape: object,
    source_plan_sha256: str,
    source_plan_content_sha256: str,
    source_native_receipt_sha256: str,
    target_body_entity_identity_sha256: str,
    support_entity_identity_sha256: str,
) -> ReviewedSubelementSelectionReceipt:
    """Resolve exactly one live semantic role without accepting native names."""

    if type(plan) is not PartDesignReferencePlan or source_shape is None:
        _fail(ReferenceRuleErrorCode.INVALID_INPUT, "/selection")
    reference_plan_content_sha256 = _digest(
        reference_plan_content_sha256, "/selection/reference_plan_content_sha256"
    )
    source_plan_sha256 = _digest(source_plan_sha256, "/selection/source_plan_sha256")
    source_plan_content_sha256 = _digest(
        source_plan_content_sha256, "/selection/source_plan_content_sha256"
    )
    source_native_receipt_sha256 = _digest(
        source_native_receipt_sha256, "/selection/source_native_receipt_sha256"
    )
    target_body_entity_identity_sha256 = _digest(
        target_body_entity_identity_sha256,
        "/selection/target_body_entity_identity_sha256",
    )
    support_entity_identity_sha256 = _digest(
        support_entity_identity_sha256,
        "/selection/support_entity_identity_sha256",
    )
    if not hmac.compare_digest(plan.support_reference_sha256, source_plan_content_sha256):
        _fail(ReferenceRuleErrorCode.INTEGRITY_FAILURE, "/selection/source_execution")
    source_shape_sha256 = _shape_sha256(source_shape, "/selection/source_shape")
    kind, role = _REFERENCE_SELECTION_PROFILE[plan.kind]
    subname, selected = _resolve_semantic_selection(source_shape, kind, role)
    signature = _geometric_signature_sha256(
        source_shape_sha256=source_shape_sha256,
        kind=kind,
        role=role,
        selected=selected,
    )
    return ReviewedSubelementSelectionReceipt(
        reference_plan_sha256=plan.plan_sha256,
        reference_plan_content_sha256=reference_plan_content_sha256,
        source_plan_sha256=source_plan_sha256,
        source_plan_content_sha256=source_plan_content_sha256,
        source_native_receipt_sha256=source_native_receipt_sha256,
        target_body_entity_identity_sha256=target_body_entity_identity_sha256,
        support_entity_identity_sha256=support_entity_identity_sha256,
        source_shape_sha256=source_shape_sha256,
        subelement_kind=kind,
        semantic_role=role,
        geometric_signature_sha256=signature,
        support_subname=subname,
    )


def _shape_topology(shape: object, path: str) -> tuple[int, int, int]:
    try:
        if shape is None or shape.isNull() or not shape.isValid():
            _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, path)
        result = (len(shape.Faces), len(shape.Edges), len(shape.Vertexes))
    except ReferenceRuleError:
        raise
    except Exception:
        _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, path)
    if any(type(item) is not int or item < 0 or item > 1_000_000 for item in result):
        _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, path)
    return result


def _support_index(kind: PartDesignReferenceKind, subname: str, shape: object) -> int | None:
    matcher = _NATIVE_PROFILE[kind][3]
    if matcher is None:
        if subname != "":
            _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, "/bindings/support_subname")
        return None
    match = None
    if isinstance(matcher, tuple):
        for candidate in matcher:
            match = candidate.fullmatch(subname)
            if match is not None:
                break
    else:
        match = matcher.fullmatch(subname)
    if match is None:
        _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, "/bindings/support_subname")
    index = int(match.group(1))
    try:
        if subname.startswith("Face"):
            maximum = len(shape.Faces)
        elif subname.startswith("Edge"):
            maximum = len(shape.Edges)
        else:
            maximum = len(shape.Vertexes)
    except Exception:
        _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, "/bindings/support_subname")
    if index > maximum:
        _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, "/bindings/support_subname")
    return index


def _link_matches(value: object, support: object, subname: str) -> bool:
    try:
        if type(value) not in {list, tuple} or len(value) != 1:
            return False
        item = value[0]
        if type(item) not in {list, tuple} or len(item) != 2 or item[0] is not support:
            return False
        subelements = item[1]
        return type(subelements) in {list, tuple} and tuple(subelements) == (subname,)
    except Exception:
        return False


def _identity_sequence_plus(
    before: tuple[object, ...], after: tuple[object, ...], added: object
) -> bool:
    if len(after) != len(before) + 1 or sum(item is added for item in after) != 1:
        return False
    return all(any(current is original for current in after) for original in before)


def _validate_bindings(
    plan: PartDesignReferencePlan,
    bindings: ReferenceExecutionBindings,
    reference_plan_content_sha256: str,
) -> tuple[object, tuple[int, int, int]]:
    receipt = bindings.selection_receipt
    if (
        bindings.body_id != plan.body_id
        or bindings.support_reference_id != plan.support_reference_id
        or not hmac.compare_digest(bindings.support_reference_sha256, plan.support_reference_sha256)
        or not hmac.compare_digest(receipt.reference_plan_sha256, plan.plan_sha256)
        or not hmac.compare_digest(
            receipt.reference_plan_content_sha256, reference_plan_content_sha256
        )
        or not hmac.compare_digest(
            receipt.source_plan_content_sha256, plan.support_reference_sha256
        )
        or not hmac.compare_digest(
            receipt.target_body_entity_identity_sha256,
            bindings.target_body_entity_identity_sha256,
        )
        or not hmac.compare_digest(
            receipt.support_entity_identity_sha256,
            bindings.support_entity_identity_sha256,
        )
    ):
        _fail(ReferenceRuleErrorCode.INTEGRITY_FAILURE, "/bindings/identity")
    document, body, support = bindings.document, bindings.body, bindings.support
    try:
        support_shape = support.Shape
        if (
            getattr(document, "UndoMode", 0) != 1
            or bool(document.HasPendingTransaction)
            or body.Document is not document
            or support.Document is not document
            or body.TypeId != "PartDesign::Body"
            or support is body
            or not support.isValid()
        ):
            _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, "/bindings")
    except ReferenceRuleError:
        raise
    except Exception:
        _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, "/bindings")
    topology = _shape_topology(support_shape, "/bindings/support")
    expected_kind, expected_role = _REFERENCE_SELECTION_PROFILE[plan.kind]
    support_subname, selected = _resolve_semantic_selection(
        support_shape,
        expected_kind,
        expected_role,
    )
    source_shape_sha256 = _shape_sha256(support_shape, "/bindings/support")
    signature = _geometric_signature_sha256(
        source_shape_sha256=source_shape_sha256,
        kind=expected_kind,
        role=expected_role,
        selected=selected,
    )
    if (
        receipt.subelement_kind is not expected_kind
        or receipt.semantic_role is not expected_role
        or receipt.support_subname != support_subname
        or not hmac.compare_digest(receipt.source_shape_sha256, source_shape_sha256)
        or not hmac.compare_digest(receipt.geometric_signature_sha256, signature)
    ):
        _fail(ReferenceRuleErrorCode.INTEGRITY_FAILURE, "/bindings/selection_receipt")
    _support_index(plan.kind, support_subname, support_shape)
    return support_shape, topology


def _validate_result(
    *,
    plan: PartDesignReferencePlan,
    result: object,
    body: object,
    support: object,
    support_shape: object,
    support_subname: str,
    preserved_tip: object,
) -> tuple[int, int, int]:
    type_id, link_property, map_mode, _matcher = _NATIVE_PROFILE[plan.kind]
    try:
        if (
            result.TypeId != type_id
            or result.Document is not body.Document
            or result not in body.Group
            or body.Tip is not preserved_tip
            or not result.isValid()
            or tuple(result.State) != ("Up-to-date",)
            or not _link_matches(getattr(result, link_property), support, support_subname)
            or (map_mode is not None and result.MapMode != map_mode)
        ):
            _fail(ReferenceRuleErrorCode.CONFORMANCE_FAILED, "/result")
        if plan.kind is PartDesignReferenceKind.SHAPE_BINDER and bool(result.TraceSupport):
            _fail(ReferenceRuleErrorCode.CONFORMANCE_FAILED, "/result/TraceSupport")
        if plan.kind is PartDesignReferenceKind.SUBSHAPE_BINDER and (
            result.BindMode != "Synchronized"
            or bool(result.Relative) is not True
            or bool(result.PartialLoad)
            or bool(result.Fuse)
            or bool(result.MakeFace) is not True
        ):
            _fail(ReferenceRuleErrorCode.CONFORMANCE_FAILED, "/result/options")
        topology = _shape_topology(result.Shape, "/result/shape")
    except ReferenceRuleError:
        raise
    except Exception:
        _fail(ReferenceRuleErrorCode.CONFORMANCE_FAILED, "/result")

    faces, edges, vertices = topology
    if (
        (plan.kind is PartDesignReferenceKind.DATUM_PLANE and faces != 1)
        or (plan.kind is PartDesignReferenceKind.DATUM_LINE and edges != 1)
        or (plan.kind is PartDesignReferenceKind.DATUM_POINT and vertices != 1)
    ):
        _fail(ReferenceRuleErrorCode.CONFORMANCE_FAILED, "/result/topology")
    if plan.kind is PartDesignReferenceKind.SHAPE_BINDER:
        support_topology = _shape_topology(support_shape, "/bindings/support")
        try:
            same_volume = math.isclose(
                float(result.Shape.Volume),
                float(support_shape.Volume),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        except Exception:
            _fail(ReferenceRuleErrorCode.CONFORMANCE_FAILED, "/result/shape")
        if topology != support_topology or not same_volume:
            _fail(ReferenceRuleErrorCode.CONFORMANCE_FAILED, "/result/shape")
    if plan.kind is PartDesignReferenceKind.SUBSHAPE_BINDER:
        if (
            (support_subname.startswith("Face") and faces != 1)
            or (support_subname.startswith("Edge") and edges != 1)
            or (support_subname.startswith("Vertex") and vertices != 1)
        ):
            _fail(ReferenceRuleErrorCode.CONFORMANCE_FAILED, "/result/topology")
    return topology


def apply_partdesign_reference_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: ReferenceExecutionBindings,
) -> ReferenceConformanceReceipt:
    """Explicit trusted-host action; validate exact bytes before native mutation."""

    if type(bindings) is not ReferenceExecutionBindings:
        _fail(ReferenceRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415
        import PartDesign  # type: ignore[import-not-found]  # noqa: F401, PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != REFERENCE_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_partdesign_reference_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    support_shape, _support_topology = _validate_bindings(
        plan,
        bindings,
        expected_content_sha256,
    )
    document, body, support = bindings.document, bindings.body, bindings.support
    selection_receipt = bindings.selection_receipt
    support_subname = selection_receipt.support_subname
    object_name = f"Reference_{plan.plan_sha256[:16]}"
    try:
        if document.getObject(object_name) is not None:
            _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, "/document/object_name")
        before_objects = tuple(document.Objects)
        before_group = tuple(body.Group)
        before_tip = body.Tip
        before_support_visibility = bool(support.Visibility)
    except ReferenceRuleError:
        raise
    except Exception:
        _fail(ReferenceRuleErrorCode.PRECONDITION_FAILED, "/document")

    type_id, link_property, map_mode, _matcher = _NATIVE_PROFILE[plan.kind]
    transaction_open = False
    try:
        document.openTransaction("VibeCAD trusted PartDesign reference")
        transaction_open = True
        result = body.newObject(type_id, object_name)
        setattr(result, link_property, [(support, [support_subname])])
        if map_mode is not None:
            result.MapMode = map_mode
        elif plan.kind is PartDesignReferenceKind.SHAPE_BINDER:
            result.TraceSupport = False
        else:
            result.BindMode = "Synchronized"
            result.Relative = True
            result.PartialLoad = False
            result.Fuse = False
            result.MakeFace = True
        document.recompute()
        topology = _validate_result(
            plan=plan,
            result=result,
            body=body,
            support=support,
            support_shape=support_shape,
            support_subname=support_subname,
            preserved_tip=before_tip,
        )
        if not _identity_sequence_plus(before_objects, tuple(document.Objects), result) or not (
            _identity_sequence_plus(before_group, tuple(body.Group), result)
        ):
            _fail(ReferenceRuleErrorCode.CONFORMANCE_FAILED, "/result/ownership")
        document.commitTransaction()
        transaction_open = False
    except BaseException as error:
        if transaction_open:
            try:
                document.abortTransaction()
                document.recompute()
            except BaseException:
                _fail(
                    ReferenceRuleErrorCode.TRANSACTION_FAILED,
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
                or bool(support.Visibility) is not before_support_visibility
            ):
                _fail(
                    ReferenceRuleErrorCode.TRANSACTION_FAILED,
                    "/transaction/rollback",
                )
        except ReferenceRuleError:
            raise
        except Exception:
            _fail(ReferenceRuleErrorCode.TRANSACTION_FAILED, "/transaction/rollback")
        if isinstance(error, KeyboardInterrupt):
            raise
        if isinstance(error, ReferenceRuleError):
            raise error
        _fail(ReferenceRuleErrorCode.TRANSACTION_FAILED, "/transaction/apply")

    return ReferenceConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        object_name=object_name,
        kind=plan.kind,
        support_subname=support_subname,
        selection_receipt_sha256=selection_receipt.receipt_sha256,
        face_count=topology[0],
        edge_count=topology[1],
        vertex_count=topology[2],
    )


__all__ = [
    "MAX_REFERENCE_PLAN_BYTES",
    "REFERENCE_FREECAD_ENGINE_BUILD_ID",
    "REFERENCE_PLAN_MEDIA_TYPE",
    "REFERENCE_PLAN_SCHEMA_VERSION",
    "REFERENCE_REVIEWED_SELECTION_RULE_CONTRACT_SHA256",
    "REFERENCE_REVIEWED_SELECTION_RULE_ID",
    "REFERENCE_RULE_CONTRACT_SHA256",
    "REFERENCE_RULE_ID",
    "PartDesignReferenceKind",
    "PartDesignReferencePlan",
    "ReferenceConformanceReceipt",
    "ReferenceExecutionBindings",
    "ReviewedReferenceSemanticRole",
    "ReviewedSubelementKind",
    "ReviewedSubelementSelectionReceipt",
    "ReferenceRuleError",
    "ReferenceRuleErrorCode",
    "apply_partdesign_reference_plan",
    "decode_partdesign_reference_plan",
    "locate_reviewed_reference_subelement",
]
