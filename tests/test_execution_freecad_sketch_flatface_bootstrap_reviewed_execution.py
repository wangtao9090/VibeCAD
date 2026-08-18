from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_FLATFACE_SKETCH_ROUTES,
    ReviewedIntentExecutionError,
    _ReviewedFamilyExecutionContext,
)
from vibecad.execution.freecad_sketch_flatface_bootstrap_reviewed_execution import (
    FLATFACE_SKETCH_PRODUCT_CONTRACT,
    FLATFACE_SKETCH_REGISTRATION_HANDOFF,
    FLATFACE_SKETCH_REVIEWED_FAMILY_SPEC,
    FLATFACE_SKETCH_REVIEWED_PRODUCT_IDENTITIES,
    build_flatface_sketch_reviewed_family_descriptor,
    execute_flatface_sketch_reviewed_plan_with_sources,
    resolve_flatface_sketch_reviewed_operation,
)
from vibecad.intent_bridge.contracts import DocumentRef
from vibecad.intent_bridge.freecad_sketch_flatface_bootstrap_adapter import (
    FLATFACE_SKETCH_BODY_OWNERSHIP_TERM,
    FLATFACE_SKETCH_FAMILY_MANIFEST,
    FLATFACE_SKETCH_FORMAL_HANDOFF,
    FLATFACE_SKETCH_OPERATION_SPEC,
    FLATFACE_SKETCH_PROFILE_TERM,
    FLATFACE_SKETCH_SELECTOR_TERM,
    FREECAD_FLATFACE_SKETCH_ADAPTER_DESCRIPTOR,
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    _build_plan,
    build_flatface_sketch_intent_graph,
)
from vibecad.intent_bridge.reviewed_family_engine import ReviewedPlanDraft
from vibecad.parametric import freecad_sketch_flatface_bootstrap_rules as rules
from vibecad.parametric.freecad_sketch_flatface_bootstrap_rules import (
    FLATFACE_SKETCH_NATIVE_TYPE_ID,
    FlatFaceSketchRuleError,
    FlatFaceSketchRuleErrorCode,
    decode_flatface_sketch_backend_plan,
    select_unique_zmax_planar_face,
)


def _lowered_plan():
    graph = build_flatface_sketch_intent_graph()
    payload = graph.canonical_bytes
    document = DocumentRef(
        artifact_id="artifact_flatface",
        role_term_ref_id=FLATFACE_SKETCH_FAMILY_MANIFEST.intent_role_term.term_ref_id,
        schema_term_ref_id=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM.term_ref_id,
        document_id=graph.graph_id,
        document_digest=graph.graph_sha256,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    )
    draft = _build_plan(
        document,
        payload,
        "4" * 64,
        FLATFACE_SKETCH_FAMILY_MANIFEST,
    )
    return (
        document,
        draft,
        decode_flatface_sketch_backend_plan(
            draft.payload,
            expected_content_sha256=hashlib.sha256(draft.payload).hexdigest(),
            expected_plan_sha256=draft.semantic_plan_sha256,
        ),
    )


def test_manifest_is_independent_exact_one_source_and_handoff_only() -> None:
    assert FLATFACE_SKETCH_FAMILY_MANIFEST.family_id == "freecad_sketch_flatface_bootstrap"
    assert FLATFACE_SKETCH_FAMILY_MANIFEST.operations == (FLATFACE_SKETCH_OPERATION_SPEC,)
    assert FLATFACE_SKETCH_OPERATION_SPEC.operation_id == (
        "create_closed_circle_on_unique_zmax_planar_face"
    )
    assert FLATFACE_SKETCH_PRODUCT_CONTRACT.minimum_sources == 1
    assert FLATFACE_SKETCH_PRODUCT_CONTRACT.maximum_sources == 1
    assert FLATFACE_SKETCH_PRODUCT_CONTRACT.requires_same_run_sources is True
    assert FLATFACE_SKETCH_REVIEWED_FAMILY_SPEC.minimum_sources == 1
    assert FLATFACE_SKETCH_REVIEWED_FAMILY_SPEC.maximum_sources == 1
    assert FLATFACE_SKETCH_FORMAL_HANDOFF.future_formal_operation_count == 126
    assert FLATFACE_SKETCH_FORMAL_HANDOFF.shared_registration_ready is False
    assert FLATFACE_SKETCH_REGISTRATION_HANDOFF.result_kind == "valid_shape"
    assert FLATFACE_SKETCH_REGISTRATION_HANDOFF.requires_same_run_sources is True
    assert FLATFACE_SKETCH_REGISTRATION_HANDOFF.downstream_operation_id == (
        "partdesign_residual.hole"
    )
    assert FLATFACE_SKETCH_REGISTRATION_HANDOFF.downstream_profile_source_index == 1
    assert FLATFACE_SKETCH_REGISTRATION_HANDOFF.downstream_receipt_fields == (
        "object_name",
        "plan_sha256",
        "receipt_sha256",
        "shape_sha256",
    )
    identity = FLATFACE_SKETCH_REVIEWED_PRODUCT_IDENTITIES[0]
    assert resolve_flatface_sketch_reviewed_operation(*identity) is FLATFACE_SKETCH_OPERATION_SPEC
    assert resolve_flatface_sketch_reviewed_operation(identity[0], identity[1] + "x") is None
    assert tuple(
        (route.operation_id, route.semantic_operation) for route in REVIEWED_FLATFACE_SKETCH_ROUTES
    ) == (identity,)
    assert CURRENT_REVIEWED_INTENT_ROUTES[125:] == REVIEWED_FLATFACE_SKETCH_ROUTES


def test_exact_adapter_lowers_dependency_without_native_subelement_authority() -> None:
    _document, draft, plan = _lowered_plan()
    assert type(draft) is ReviewedPlanDraft
    assert plan.source_count == 1
    assert plan.base_node_id == "node_base"
    assert plan.base_result_id == "result_base"
    assert plan.ownership_identity.term_id == FLATFACE_SKETCH_BODY_OWNERSHIP_TERM.term_id
    assert plan.selector_identity.term_id == FLATFACE_SKETCH_SELECTOR_TERM.term_id
    assert plan.profile_identity.term_id == FLATFACE_SKETCH_PROFILE_TERM.term_id
    assert plan.to_mapping()["selection"]["face"] == "unique-z-max-planar-face"
    assert b"Face1" not in plan.canonical_bytes
    assert b"Face2" not in plan.canonical_bytes
    assert b"Face3" not in plan.canonical_bytes


def test_plan_rejects_bare_face_label_and_manifest_rebinding() -> None:
    _document, draft, plan = _lowered_plan()
    mapping = json.loads(draft.payload)
    mapping["selection"]["face"] = "Face6"
    tampered = json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode("ascii")
    with pytest.raises(FlatFaceSketchRuleError) as caught:
        decode_flatface_sketch_backend_plan(tampered)
    assert caught.value.code is FlatFaceSketchRuleErrorCode.INTEGRITY_FAILURE

    rebound = replace(plan, manifest_sha256="f" * 64)
    assert rebound.canonical_bytes != plan.canonical_bytes
    assert rebound.plan_sha256 != plan.plan_sha256


class _Vector:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x, self.y, self.z = x, y, z


class _Bounds:
    def __init__(self, z: float) -> None:
        self.XMin, self.XMax = 0.0, 10.0
        self.YMin, self.YMax = 0.0, 10.0
        self.ZMin = self.ZMax = z


class _Face:
    Area = 100.0
    CenterOfMass = _Vector(5.0, 5.0, 10.0)
    ParameterRange = (0.0, 10.0, 0.0, 10.0)

    def __init__(self, z: float, token: str, *, planar: bool = True) -> None:
        self.BoundBox = _Bounds(z)
        self.Surface = type(
            "Surface",
            (),
            {"TypeId": "Part::GeomPlane" if planar else "Part::GeomCylinder"},
        )()
        self._token = token

    def normalAt(self, _u: float, _v: float) -> _Vector:
        return _Vector(0.0, 0.0, 1.0)

    def exportBrepToString(self) -> str:
        return f"face-brep:{self._token}"


class _Shape:
    def __init__(self, faces: tuple[_Face, ...]) -> None:
        self.Faces = faces
        self.BoundBox = type("ShapeBounds", (), {"ZMax": 10.0})()

    def exportBrepToString(self) -> str:
        return "base-brep"


class _Base:
    def __init__(self, faces: tuple[_Face, ...]) -> None:
        self.Shape = _Shape(faces)


def test_family_selects_one_content_bound_zmax_plane_but_does_not_receipt_face_n() -> None:
    side = _Face(0.0, "side", planar=False)
    top = _Face(10.0, "top")
    _face, native_label, evidence = select_unique_zmax_planar_face(_Base((side, top)))

    assert native_label == "Face2"
    assert evidence.area_mm2 == 100.0
    assert evidence.center_mm == (5.0, 5.0, 10.0)
    assert "Face2" not in repr(evidence)
    assert len(evidence.geometric_signature_sha256) == 64
    assert len(evidence.face_brep_sha256) == 64
    assert len(evidence.base_brep_sha256) == 64


def test_ambiguous_zmax_planes_fail_before_mutation() -> None:
    with pytest.raises(FlatFaceSketchRuleError) as caught:
        select_unique_zmax_planar_face(_Base((_Face(10.0, "a"), _Face(10.0, "b"))))
    assert caught.value.code is FlatFaceSketchRuleErrorCode.PRECONDITION_FAILED
    assert caught.value.path == "/selection/unique_zmax_planar_face"


class _Object:
    def __init__(self, name: str, type_id: str = "PartDesign::Feature") -> None:
        self.Name = name
        self.TypeId = type_id
        self.Visibility = True


class _Body(_Object):
    def __init__(self, base: _Object) -> None:
        super().__init__("Body", "PartDesign::Body")
        self.Group = [base]
        self.Tip = base


class _Document:
    UndoMode = 1
    HasPendingTransaction = False

    def __init__(self, body: _Body, base: _Object) -> None:
        self.Objects = [body, base]
        self.body = body

    def getObject(self, name: str):
        return next((item for item in self.Objects if item.Name == name), None)

    def removeObject(self, name: str) -> None:
        item = self.getObject(name)
        self.Objects.remove(item)
        if item in self.body.Group:
            self.body.Group.remove(item)
        if self.body.Tip is item:
            self.body.Tip = self.body.Group[-1]

    def recompute(self) -> None:
        return None


def test_rollback_restores_sequence_group_tip_and_visibility() -> None:
    base = _Object("Base")
    body = _Body(base)
    document = _Document(body, base)
    before, snapshots = rules._snapshot_document(document)  # noqa: SLF001
    sketch = _Object("Sketch", FLATFACE_SKETCH_NATIVE_TYPE_ID)
    document.Objects.append(sketch)
    body.Group.append(sketch)
    body.Tip = sketch
    base.Visibility = False

    assert rules._restore_document(document, before, snapshots) is True  # noqa: SLF001
    assert tuple(document.Objects) == before
    assert tuple(body.Group) == (base,)
    assert body.Tip is base
    assert base.Visibility is True


def test_private_descriptor_is_exact_one_source_valid_shape() -> None:
    descriptor = build_flatface_sketch_reviewed_family_descriptor()
    assert descriptor.manifest is FLATFACE_SKETCH_FAMILY_MANIFEST
    assert descriptor.minimum_sources == descriptor.maximum_sources == 1
    assert descriptor.requires_same_run_sources is True
    assert descriptor.product_results[0].source_count == 1
    assert descriptor.product_results[0].owned_type_ids == (FLATFACE_SKETCH_NATIVE_TYPE_ID,)
    assert descriptor.product_results[0].requires_state_sha256 is True
    assert descriptor.create_recovery is not None


def test_shared_create_recovery_restores_and_commits_exact_body_state() -> None:
    base = _Object("Base")
    body = _Body(base)
    document = _Document(body, base)
    base.getParentGeoFeatureGroup = lambda: body
    context = _ReviewedFamilyExecutionContext(
        session=object(),
        document=document,
        source_results=(type("Source", (), {"object": base})(),),
        run_token=object(),
    )
    descriptor = build_flatface_sketch_reviewed_family_descriptor().create_recovery
    assert descriptor is not None
    before_sha256, opaque = descriptor.prepare(
        document,
        FLATFACE_SKETCH_OPERATION_SPEC,
        context,
    )
    assert (
        descriptor.verify(
            document,
            opaque,
            FLATFACE_SKETCH_OPERATION_SPEC,
            context,
        )
        == before_sha256
    )

    sketch = _Object("Sketch", FLATFACE_SKETCH_NATIVE_TYPE_ID)
    sketch.MapMode = "FlatFace"
    sketch.AttachmentSupport = [(base, ["Face2"])]
    document.Objects.append(sketch)
    body.Group.append(sketch)
    body.Tip = sketch
    base.Visibility = False
    descriptor.commit(document, opaque, FLATFACE_SKETCH_OPERATION_SPEC, context)

    descriptor.recover(document, opaque, FLATFACE_SKETCH_OPERATION_SPEC, context)
    assert (
        descriptor.verify(
            document,
            opaque,
            FLATFACE_SKETCH_OPERATION_SPEC,
            context,
        )
        == before_sha256
    )
    assert tuple(document.Objects) == (body, base)
    assert tuple(body.Group) == (base,)
    assert body.Tip is base
    assert base.Visibility is True


def test_product_entry_rejects_missing_source_before_native_rule() -> None:
    _source_document, _draft, plan = _lowered_plan()
    plan_document = FLATFACE_SKETCH_FAMILY_MANIFEST.plan_document(
        plan.canonical_bytes,
        plan.plan_sha256,
    )
    with pytest.raises(ReviewedIntentExecutionError):
        execute_flatface_sketch_reviewed_plan_with_sources(
            object(),
            plan,
            plan.canonical_bytes,
            plan_document,
            FLATFACE_SKETCH_OPERATION_SPEC,
            (),
            session=object(),
            run_token=object(),
        )


def test_adapter_identity_and_native_type_are_new_without_reusing_xy_create() -> None:
    assert FREECAD_FLATFACE_SKETCH_ADAPTER_DESCRIPTOR.adapter_id == (
        "freecad_sketch_flatface_bootstrap_adapter"
    )
    assert FLATFACE_SKETCH_OPERATION_SPEC.native_type_id == "Sketcher::SketchObject"
    assert FLATFACE_SKETCH_OPERATION_SPEC.semantic_term.term_id == (
        "operation.sketch.create-flatface-circle"
    )
