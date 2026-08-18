"""Focused family-only tests for reviewed Sketch CREATE bootstrap."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import replace
from types import ModuleType

import pytest

import vibecad.execution.freecad_sketch_bootstrap_reviewed_execution as bootstrap_execution
import vibecad.intent_bridge.freecad_sketch_bootstrap_adapter as bootstrap_adapter
from vibecad.execution.freecad_sketch_bootstrap_reviewed_execution import (
    SKETCH_BOOTSTRAP_PRODUCT_CONTRACT,
    SKETCH_BOOTSTRAP_REVIEWED_FAMILY_SPEC,
    SKETCH_BOOTSTRAP_REVIEWED_PRODUCT_IDENTITIES,
    SketchBootstrapExecutionError,
    execute_sketch_bootstrap_reviewed_plan_with_sources,
    resolve_sketch_bootstrap_reviewed_operation,
)
from vibecad.intent_bridge.contracts import DocumentRef, IntentBridgeError
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    ReviewedPlanDraft,
)
from vibecad.parametric import freecad_sketch_intent_rules as sketch_rules
from vibecad.parametric.freecad_sketch_bootstrap_rules import (
    SKETCH_BOOTSTRAP_FREECAD_ENGINE_BUILD_ID,
    SKETCH_BOOTSTRAP_ORIGIN_CLOSURE_TYPE_IDS,
    SKETCH_BOOTSTRAP_PLAN_MEDIA_TYPE,
    SketchBootstrapBackendPlan,
    SketchBootstrapExecutionBindings,
    SketchBootstrapRuleError,
    SketchBootstrapRuleErrorCode,
    SketchBootstrapSemanticIdentity,
    apply_sketch_bootstrap_plan,
    decode_sketch_bootstrap_backend_plan,
)


class _MemoryPlanSink:
    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        self.document = document
        self.payload = payload
        return payload

    def read_exact(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        assert document == self.document
        assert len(self.payload) <= maximum_bytes
        return self.payload


def _semantic(term) -> SketchBootstrapSemanticIdentity:
    return SketchBootstrapSemanticIdentity(
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


def _plan() -> SketchBootstrapBackendPlan:
    return SketchBootstrapBackendPlan(
        source_artifact_id="artifact_sketch_bootstrap",
        source_graph_id="graph_sketch_bootstrap",
        source_graph_sha256="1" * 64,
        source_content_sha256="2" * 64,
        lowering_request_sha256="3" * 64,
        adapter_contract_sha256=(
            bootstrap_adapter.FREECAD_SKETCH_BOOTSTRAP_ADAPTER_DESCRIPTOR.adapter_contract_sha256
        ),
        body_id="body_sketch_bootstrap",
        node_id="node_sketch_bootstrap",
        result_id="result_sketch_bootstrap",
        operation_identity=_semantic(bootstrap_adapter.SKETCH_BOOTSTRAP_CREATE_OPERATION_TERM),
        ownership_identity=_semantic(bootstrap_adapter.SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM),
        plane_identity=_semantic(bootstrap_adapter.SKETCH_BOOTSTRAP_XY_PLANE_TERM),
        profile_identity=_semantic(bootstrap_adapter.SKETCH_BOOTSTRAP_CLOSED_CIRCLE_PROFILE_TERM),
    )


def _plan_document(plan: SketchBootstrapBackendPlan) -> DocumentRef:
    return bootstrap_adapter.SKETCH_BOOTSTRAP_FAMILY_MANIFEST.plan_document(
        plan.canonical_bytes, plan.plan_sha256
    )


def test_manifest_is_one_new_create_and_family_only() -> None:
    manifest = bootstrap_adapter.SKETCH_BOOTSTRAP_FAMILY_MANIFEST
    adapter = bootstrap_adapter.sketch_bootstrap_reviewed_adapter_factory(_MemoryPlanSink())
    assert type(adapter) is ExactReviewedFamilyAdapter
    assert manifest.operations == (bootstrap_adapter.SKETCH_BOOTSTRAP_OPERATION_SPEC,)
    assert manifest.operations[0].operation_id == "create_body_owned_closed_circle"
    assert SKETCH_BOOTSTRAP_REVIEWED_FAMILY_SPEC.shared_registration_ready is False
    assert SKETCH_BOOTSTRAP_REVIEWED_FAMILY_SPEC.operation_ids == (
        "create_body_owned_closed_circle",
    )
    assert bootstrap_adapter.SKETCH_BOOTSTRAP_FORMAL_HANDOFF.blockers == (
        "shared-dispatcher-registration-not-in-family-scope",
        "intel-and-arm-release-attestation-refresh-pending",
    )
    identity = SKETCH_BOOTSTRAP_REVIEWED_PRODUCT_IDENTITIES[0]
    assert resolve_sketch_bootstrap_reviewed_operation(*identity) is manifest.operations[0]
    assert resolve_sketch_bootstrap_reviewed_operation(identity[0], identity[1] + "x") is None


def test_exact_semantics_lower_to_zero_source_full_identity_plan() -> None:
    graph = bootstrap_adapter.build_sketch_bootstrap_intent_graph()
    payload = graph.canonical_bytes
    document = DocumentRef(
        artifact_id="artifact_sketch_bootstrap",
        role_term_ref_id=bootstrap_adapter.SKETCH_BOOTSTRAP_INTENT_ROLE_TERM.term_ref_id,
        schema_term_ref_id=(bootstrap_adapter.PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM.term_ref_id),
        document_id=graph.graph_id,
        document_digest=graph.graph_sha256,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        media_type=bootstrap_adapter.PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    )
    draft = bootstrap_adapter._build_plan(  # noqa: SLF001
        document,
        payload,
        "4" * 64,
        bootstrap_adapter.SKETCH_BOOTSTRAP_FAMILY_MANIFEST,
    )
    assert type(draft) is ReviewedPlanDraft
    plan = decode_sketch_bootstrap_backend_plan(
        draft.payload,
        expected_content_sha256=hashlib.sha256(draft.payload).hexdigest(),
        expected_plan_sha256=draft.semantic_plan_sha256,
    )
    assert plan.source_count == 0
    assert plan.operation_identity == _semantic(
        bootstrap_adapter.SKETCH_BOOTSTRAP_CREATE_OPERATION_TERM
    )
    assert plan.ownership_identity == _semantic(
        bootstrap_adapter.SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM
    )
    assert plan.plane_identity == _semantic(bootstrap_adapter.SKETCH_BOOTSTRAP_XY_PLANE_TERM)
    assert plan.profile_identity == _semantic(
        bootstrap_adapter.SKETCH_BOOTSTRAP_CLOSED_CIRCLE_PROFILE_TERM
    )
    mapping = plan.to_mapping()
    assert mapping["operation"]["source_count"] == 0
    assert mapping["operation"]["support_plane"] == "xy"
    assert mapping["operation"]["profile"] == {
        "kind": "circle",
        "center_mm": [0.0, 0.0],
        "radius_mm": 10.0,
        "closed": True,
    }
    assert "TypeId" not in draft.payload.decode("ascii")
    assert "ReviewedSketch" not in draft.payload.decode("ascii")


def test_extra_node_or_wrong_plane_semantic_is_rejected() -> None:
    graph = bootstrap_adapter.build_sketch_bootstrap_intent_graph()
    extra_reference = replace(graph.references[0], reference_id="reference_origin_xy_plane_extra")
    extra_scope = replace(graph, references=(*graph.references, extra_reference))
    extra_document, extra_payload = _intent_document(extra_scope)
    with pytest.raises(IntentBridgeError):
        bootstrap_adapter._build_plan(  # noqa: SLF001
            extra_document,
            extra_payload,
            "4" * 64,
            bootstrap_adapter.SKETCH_BOOTSTRAP_FAMILY_MANIFEST,
        )
    wrong_plane = replace(
        graph.references[0],
        locator_term_ref_id=bootstrap_adapter.SKETCH_BOOTSTRAP_RESULT_TYPE_TERM.term_ref_id,
    )
    mutated = replace(graph, references=(wrong_plane,))
    document, payload = _intent_document(mutated)
    with pytest.raises(IntentBridgeError):
        bootstrap_adapter._build_plan(  # noqa: SLF001
            document,
            payload,
            "4" * 64,
            bootstrap_adapter.SKETCH_BOOTSTRAP_FAMILY_MANIFEST,
        )


def _intent_document(graph) -> tuple[DocumentRef, bytes]:
    payload = graph.canonical_bytes
    return (
        DocumentRef(
            artifact_id="artifact_sketch_bootstrap",
            role_term_ref_id=bootstrap_adapter.SKETCH_BOOTSTRAP_INTENT_ROLE_TERM.term_ref_id,
            schema_term_ref_id=(
                bootstrap_adapter.PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM.term_ref_id
            ),
            document_id=graph.graph_id,
            document_digest=graph.graph_sha256,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type=bootstrap_adapter.PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
        ),
        payload,
    )


class _Vector:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x = x
        self.y = y
        self.z = z


class _Circle:
    TypeId = "Part::GeomCircle"

    def __init__(self, center: _Vector, axis: _Vector, radius: float) -> None:
        self.Center = center
        self.Axis = axis
        self.Radius = radius


class _Wire:
    def isClosed(self) -> bool:  # noqa: N802 - FreeCAD spelling
        return True


class _Shape:
    Wires = (_Wire(),)
    Edges = (object(),)
    Faces = ()
    Solids = ()

    def isNull(self) -> bool:  # noqa: N802 - FreeCAD spelling
        return False

    def isValid(self) -> bool:  # noqa: N802 - FreeCAD spelling
        return True

    def exportBrepToString(self) -> str:  # noqa: N802 - FreeCAD spelling
        return "reviewed-closed-circle-brep"


class _Object:
    def __init__(self, document: _Document, name: str, type_id: str) -> None:
        self.Document = document
        self.Name = name
        self.TypeId = type_id
        self.Visibility = True


class _Origin(_Object):
    def __init__(self, document: _Document, name: str, features: tuple[_Object, ...]) -> None:
        super().__init__(document, name, "App::Origin")
        self.OriginFeatures = features


class _Sketch(_Object):
    def __init__(self, document: _Document, name: str) -> None:
        super().__init__(document, name, "Sketcher::SketchObject")
        self.AttachmentSupport = ()
        self.MapMode = "Deactivated"
        self.Geometry: list[_Circle] = []
        self.Constraints: list[object] = []
        self.OpenVertices: tuple[object, ...] = ()
        self.Shape = _Shape()
        self.State = ("Up-to-date",)
        self._properties: list[str] = []

    @property
    def GeometryCount(self) -> int:  # noqa: N802 - FreeCAD spelling
        return len(self.Geometry)

    @property
    def ConstraintCount(self) -> int:  # noqa: N802 - FreeCAD spelling
        return len(self.Constraints)

    def addGeometry(self, geometry: _Circle, construction: bool) -> int:  # noqa: N802
        assert construction is False
        self.Geometry.append(geometry)
        return len(self.Geometry) - 1

    def getConstruction(self, index: int) -> bool:  # noqa: N802 - FreeCAD spelling
        assert index == 0
        return False

    @property
    def PropertiesList(self) -> tuple[str, ...]:  # noqa: N802 - FreeCAD spelling
        return tuple(self._properties)

    def addProperty(self, _type_id: str, name: str, _group: str, _description: str) -> None:  # noqa: N802
        self._properties.append(name)

    def setEditorMode(self, name: str, mode: int) -> None:  # noqa: N802
        assert name in self._properties and mode == 2


class _Body(_Object):
    def __init__(self, document: _Document, name: str) -> None:
        super().__init__(document, name, "PartDesign::Body")
        suffix = str(document.body_count)
        features = tuple(
            _Object(document, f"OriginFeature_{suffix}_{index}", type_id)
            for index, type_id in enumerate(SKETCH_BOOTSTRAP_ORIGIN_CLOSURE_TYPE_IDS[1:])
        )
        self.Origin = _Origin(document, f"OriginContainer_{suffix}", features)
        self.Group: list[object] = []
        self.Tip = None
        document.Objects.extend((self, self.Origin, *features))

    def newObject(self, type_id: str, name: str):  # noqa: N802 - FreeCAD spelling
        assert type_id == "Sketcher::SketchObject"
        sketch = _Sketch(self.Document, name)
        self.Document.Objects.append(sketch)
        self.Group.append(sketch)
        return sketch


class _Document:
    UndoMode = 1

    def __init__(self, *, fail_recompute: bool = False) -> None:
        self.Objects: list[object] = []
        self.HasPendingTransaction = False
        self.body_count = 0
        self.fail_recompute = fail_recompute
        self._snapshot = None

    def addObject(self, type_id: str, name: str):  # noqa: N802 - FreeCAD spelling
        assert type_id == "PartDesign::Body"
        self.body_count += 1
        return _Body(self, name)

    def getObject(self, name: str):  # noqa: N802 - FreeCAD spelling
        return next((item for item in self.Objects if item.Name == name), None)

    def removeObject(self, name: str) -> None:  # noqa: N802 - FreeCAD spelling
        item = self.getObject(name)
        if item is None:
            return
        if isinstance(item, _Body):
            owned = (item, item.Origin, *item.Origin.OriginFeatures, *item.Group)
            self.Objects[:] = [
                candidate
                for candidate in self.Objects
                if not any(candidate is value for value in owned)
            ]
        else:
            self.Objects.remove(item)

    def openTransaction(self, _label: str) -> None:  # noqa: N802 - FreeCAD spelling
        assert not self.HasPendingTransaction
        self.HasPendingTransaction = True
        self._snapshot = (
            tuple(self.Objects),
            tuple(
                (
                    item,
                    bool(item.Visibility),
                    tuple(item.Group) if isinstance(item, _Body) else None,
                    item.Tip if isinstance(item, _Body) else None,
                )
                for item in self.Objects
            ),
        )

    def commitTransaction(self) -> None:  # noqa: N802 - FreeCAD spelling
        assert self.HasPendingTransaction
        self.HasPendingTransaction = False
        self._snapshot = None

    def abortTransaction(self) -> None:  # noqa: N802 - FreeCAD spelling
        before, states = self._snapshot
        self.Objects[:] = before
        for item, visibility, group, tip in states:
            item.Visibility = visibility
            if group is not None:
                item.Group[:] = group
                item.Tip = tip
        self.HasPendingTransaction = False
        self._snapshot = None

    def recompute(self) -> None:
        if self.fail_recompute and self.HasPendingTransaction:
            for item in self.Objects:
                item.Visibility = False
                if isinstance(item, _Body) and item.Group:
                    item.Group.reverse()
                    item.Tip = None
            self.fail_recompute = False
            raise RuntimeError("secret recompute detail")


def _install_fake_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    freecad = ModuleType("FreeCAD")
    freecad.Version = lambda: (
        "1",
        "1",
        "0",
        "",
        "",
        "",
        "",
        SKETCH_BOOTSTRAP_FREECAD_ENGINE_BUILD_ID,
    )
    freecad.Vector = _Vector
    part = ModuleType("Part")
    part.Circle = _Circle
    monkeypatch.setitem(sys.modules, "FreeCAD", freecad)
    monkeypatch.setitem(sys.modules, "Part", part)
    monkeypatch.setitem(sys.modules, "Sketcher", ModuleType("Sketcher"))


def _document_with_existing_body() -> tuple[_Document, _Body, _Object]:
    document = _Document()
    existing = document.addObject("PartDesign::Body", "ExistingBody")
    feature = _Object(document, "ExistingFeature", "PartDesign::Feature")
    document.Objects.append(feature)
    existing.Group.append(feature)
    existing.Tip = feature
    feature.Visibility = False
    return document, existing, feature


def test_native_rule_creates_exact_body_origin7_sketch_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_modules(monkeypatch)
    plan = _plan()
    document, existing, feature = _document_with_existing_body()
    before = tuple(document.Objects)
    receipt = apply_sketch_bootstrap_plan(
        plan.canonical_bytes,
        expected_content_sha256=hashlib.sha256(plan.canonical_bytes).hexdigest(),
        expected_plan_sha256=plan.plan_sha256,
        bindings=SketchBootstrapExecutionBindings(document=document, body_id=plan.body_id),
    )
    added = tuple(document.Objects[len(before) :])
    assert len(added) == 10
    body, *origin_and_sketch = added
    sketch = origin_and_sketch[-1]
    assert body.TypeId == "PartDesign::Body"
    assert tuple(item.TypeId for item in added[1:-1]) == SKETCH_BOOTSTRAP_ORIGIN_CLOSURE_TYPE_IDS
    assert sketch.TypeId == "Sketcher::SketchObject"
    assert tuple(body.Group) == (sketch,)
    assert body.Tip is sketch
    assert sketch.AttachmentSupport[0][0] is body.Origin.OriginFeatures[3]
    assert sketch.MapMode == "FlatFace"
    assert sketch.GeometryCount == 1
    assert sketch.ConstraintCount == 0
    assert len(sketch.Shape.Wires) == 1
    assert sketch.Shape.Wires[0].isClosed()
    assert len(sketch.OpenVertices) == 0
    assert receipt.closure_names == tuple(item.Name for item in added)
    assert len(receipt.state_sha256) == 64
    assert len(receipt.shape_sha256) == 64
    assert len(receipt.geometry_sha256) == 64
    assert len(receipt.constraint_sha256) == 64
    assert tuple(document.Objects[: len(before)]) == before
    assert tuple(existing.Group) == (feature,)
    assert existing.Tip is feature
    assert feature.Visibility is False


def test_native_failure_restores_sequence_group_tip_and_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_modules(monkeypatch)
    plan = _plan()
    document, existing, feature = _document_with_existing_body()
    document.fail_recompute = True
    before = tuple(document.Objects)
    before_visibility = tuple(bool(item.Visibility) for item in before)
    with pytest.raises(SketchBootstrapRuleError) as caught:
        apply_sketch_bootstrap_plan(
            plan.canonical_bytes,
            expected_content_sha256=hashlib.sha256(plan.canonical_bytes).hexdigest(),
            expected_plan_sha256=plan.plan_sha256,
            bindings=SketchBootstrapExecutionBindings(document=document, body_id=plan.body_id),
        )
    assert caught.value.code is SketchBootstrapRuleErrorCode.TRANSACTION_FAILED
    assert tuple(document.Objects) == before
    assert tuple(existing.Group) == (feature,)
    assert existing.Tip is feature
    assert tuple(bool(item.Visibility) for item in before) == before_visibility
    assert document.HasPendingTransaction is False


def test_family_execution_requires_zero_sources_and_returns_primary_sketch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_modules(monkeypatch)
    plan = _plan()
    document = _Document()
    plan_document = _plan_document(plan)
    result = execute_sketch_bootstrap_reviewed_plan_with_sources(
        document,
        plan,
        plan.canonical_bytes,
        plan_document,
        bootstrap_adapter.SKETCH_BOOTSTRAP_OPERATION_SPEC,
        (),
    )
    assert result.object.TypeId == "Sketcher::SketchObject"
    assert result.owned_objects[0] is result.object
    assert len(result.owned_objects) == 10
    assert result.receipt.shape_sha256
    assert result.receipt.result_id == plan.result_id
    assert len(result.receipt.profile_node_sha256) == 64
    metadata, results = sketch_rules._validated_metadata(  # noqa: SLF001
        result.object,
        plan.node_id,
    )
    assert metadata["geometries"][0]["geometry_id"] == result.receipt.profile_geometry_id
    assert results[plan.result_id]["producer_node_sha256"] == (result.receipt.profile_node_sha256)
    with pytest.raises(SketchBootstrapExecutionError):
        execute_sketch_bootstrap_reviewed_plan_with_sources(
            _Document(),
            plan,
            plan.canonical_bytes,
            plan_document,
            bootstrap_adapter.SKETCH_BOOTSTRAP_OPERATION_SPEC,
            (result,),
        )
    with pytest.raises(SketchBootstrapExecutionError):
        SKETCH_BOOTSTRAP_PRODUCT_CONTRACT.validate_owned(result.object, (result.object,))


def test_post_native_receipt_mismatch_recovers_entire_created_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_modules(monkeypatch)
    plan = _plan()
    document, existing, feature = _document_with_existing_body()
    before = tuple(document.Objects)
    original = bootstrap_execution.apply_sketch_bootstrap_plan

    def stale_receipt(*args, **kwargs):
        receipt = original(*args, **kwargs)
        document.getObject(receipt.object_name).Geometry[0].Radius = 11.0
        return receipt

    monkeypatch.setattr(bootstrap_execution, "apply_sketch_bootstrap_plan", stale_receipt)
    with pytest.raises(SketchBootstrapExecutionError):
        execute_sketch_bootstrap_reviewed_plan_with_sources(
            document,
            plan,
            plan.canonical_bytes,
            _plan_document(plan),
            bootstrap_adapter.SKETCH_BOOTSTRAP_OPERATION_SPEC,
            (),
        )
    assert tuple(document.Objects) == before
    assert tuple(existing.Group) == (feature,)
    assert existing.Tip is feature
    assert feature.Visibility is False


def test_plan_is_canonical_and_rejects_native_name_injection() -> None:
    plan = _plan()
    decoded = decode_sketch_bootstrap_backend_plan(
        plan.canonical_bytes,
        expected_content_sha256=hashlib.sha256(plan.canonical_bytes).hexdigest(),
        expected_plan_sha256=plan.plan_sha256,
    )
    assert decoded == plan
    payload = plan.canonical_bytes.replace(b'"support_plane":"xy"', b'"support_plane":"XY_Plane"')
    with pytest.raises(SketchBootstrapRuleError):
        decode_sketch_bootstrap_backend_plan(payload)
    assert SKETCH_BOOTSTRAP_PLAN_MEDIA_TYPE == (
        "application/vnd.vibecad.freecad-sketch-bootstrap-plan+json"
    )
