"""Focused product-callback tests for the reviewed Sketch family."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from types import SimpleNamespace

import pytest

import vibecad.execution.freecad_reviewed_intent_execution as shared_execution
import vibecad.execution.freecad_sketch_reviewed_execution as execution
import vibecad.intent_bridge.freecad_sketch_intent_adapter as sketch_adapter
from tests.test_intent_bridge_freecad_sketch_intent_adapter import (
    _constraint_graph,
    _geometry_graph,
    _graph_document,
)
from vibecad.execution.freecad_reviewed_intent_execution import ReviewedIntentExecutionError
from vibecad.intent_bridge.contracts import DocumentRef, SubjectRef
from vibecad.intent_bridge.freecad_sketch_intent_adapter import (
    REVIEWED_SKETCH_FAMILY_MANIFEST,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    ReviewedPlanReceipt,
)
from vibecad.intent_bridge.sketch_intent_graph_codec import (
    SKETCH_CONSTRAINT_SELECTOR_TERM,
    SKETCH_GEOMETRY_SELECTOR_TERM,
    SKETCH_ROOT_SEMANTIC_TYPE_TERM,
)
from vibecad.parametric import freecad_sketch_intent_rules as rules
from vibecad.parametric.freecad_sketch_intent_rules import (
    ReviewedSketchBackendPlan,
    ReviewedSketchConformanceReceipt,
    ReviewedSketchNativeResult,
    ReviewedSketchOperation,
    ReviewedSketchParameter,
    ReviewedSketchResult,
    decode_reviewed_sketch_backend_plan,
)
from vibecad.sketch.contracts import SketchIntentGraph, encode_sketch_intent_graph
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1


class _MemorySink:
    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        return payload

    def read_exact(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        raise AssertionError("not needed")


class _Matrix:
    A11 = 1.0
    A12 = 0.0
    A13 = 0.0
    A14 = 0.0
    A21 = 0.0
    A22 = 1.0
    A23 = 0.0
    A24 = 0.0
    A31 = 0.0
    A32 = 0.0
    A33 = 1.0
    A34 = 0.0
    A41 = 0.0
    A42 = 0.0
    A43 = 0.0
    A44 = 1.0


class _Placement:
    def toMatrix(self) -> _Matrix:
        return _Matrix()


@dataclasses.dataclass
class _Vector:
    x: float
    y: float
    z: float = 0.0


@dataclasses.dataclass
class _Circle:
    TypeId: str = "Part::GeomCircle"
    Center: _Vector = dataclasses.field(default_factory=lambda: _Vector(0.0, 0.0))
    Axis: _Vector = dataclasses.field(default_factory=lambda: _Vector(0.0, 0.0, 1.0))
    Radius: float = 5.0

    def copy(self) -> _Circle:
        return dataclasses.replace(
            self,
            Center=dataclasses.replace(self.Center),
            Axis=dataclasses.replace(self.Axis),
        )


class _Wire:
    def isClosed(self) -> bool:
        return True


class _Shape:
    def __init__(self, sketch: _Sketch) -> None:
        self._sketch = sketch

    def isNull(self) -> bool:
        return not self._sketch.Geometry

    def isValid(self) -> bool:
        return bool(self._sketch.Geometry)

    @property
    def ShapeType(self) -> str:
        return "Wire"

    @property
    def Vertexes(self) -> tuple[object, ...]:
        return () if self.isNull() else (object(),)

    @property
    def Edges(self) -> tuple[object, ...]:
        return tuple(object() for _ in self._sketch.Geometry)

    @property
    def Wires(self) -> tuple[_Wire, ...]:
        return () if self.isNull() else (_Wire(),)

    Faces: tuple[object, ...] = ()
    Solids: tuple[object, ...] = ()

    def exportBrepToString(self) -> str:
        return "|".join(f"{item.TypeId}:{item.Radius}" for item in self._sketch.Geometry)


class _Body:
    TypeId = "PartDesign::Body"

    def __init__(self, document: _Document) -> None:
        self.Document = document
        self.Name = "Body"
        self.Group: list[object] = []


class _Sketch:
    TypeId = "Sketcher::SketchObject"
    Name = "Sketch"
    State = ("Up-to-date",)
    MapMode = "Deactivated"
    Support: tuple[object, ...] = ()
    Placement = _Placement()

    def __init__(self, document: _Document, *, body: _Body | None = None) -> None:
        self.Document = document
        self._body = body
        self.Geometry: list[_Circle] = []
        self.Constraints: list[object] = []
        self._construction: list[bool] = []
        self._active: list[bool] = []
        self.OpenVertices: tuple[object, ...] = ()
        self.DoF = 0
        self.FullyConstrained = False
        self.ConflictingConstraints: tuple[int, ...] = ()
        self.RedundantConstraints: tuple[int, ...] = ()
        self.PartiallyRedundantConstraints: tuple[int, ...] = ()
        self.MalformedConstraints: tuple[int, ...] = ()
        self.Shape = _Shape(self)

    @property
    def GeometryCount(self) -> int:
        return len(self.Geometry)

    @property
    def ConstraintCount(self) -> int:
        return len(self.Constraints)

    @property
    def PropertiesList(self) -> tuple[str, ...]:
        return (
            ("VibeCADReviewedSketchIntent",) if hasattr(self, "VibeCADReviewedSketchIntent") else ()
        )

    def isValid(self) -> bool:
        return True

    def solve(self) -> int:
        self.DoF = 3 * len(self.Geometry)
        return 0

    def getParentGeoFeatureGroup(self) -> _Body | None:
        return self._body

    def getConstruction(self, index: int) -> bool:
        return self._construction[index]

    def getActive(self, index: int) -> bool:
        return self._active[index]

    def addGeometry(self, value: _Circle, construction: bool) -> int:
        self.Geometry.append(value.copy())
        self._construction.append(construction)
        return len(self.Geometry) - 1

    def delGeometry(self, index: int) -> None:
        del self.Geometry[index]
        del self._construction[index]

    def addConstraint(self, value: object) -> int:
        self.Constraints.append(value)
        self._active.append(True)
        return len(self.Constraints) - 1

    def delConstraints(self, indices: list[int], update_external: bool) -> None:
        for index in sorted(indices, reverse=True):
            del self.Constraints[index]
            del self._active[index]

    def renameConstraint(self, index: int, name: str) -> None:
        self.Constraints[index].Name = name

    def setActive(self, index: int, active: bool) -> None:
        self._active[index] = active

    def addProperty(self, *args: object) -> None:
        self.VibeCADReviewedSketchIntent = ""

    def removeProperty(self, name: str) -> None:
        delattr(self, name)

    def setEditorMode(self, name: str, mode: int) -> None:
        return None


class _Document:
    UndoMode = 1

    def __init__(self, *, body_owned: bool = False) -> None:
        self.HasPendingTransaction = False
        self.Objects: list[object] = []
        self.body = _Body(self) if body_owned else None
        if self.body is not None:
            self.Objects.append(self.body)
        self.sketch = _Sketch(self, body=self.body)
        self.Objects.append(self.sketch)
        if self.body is not None:
            self.body.Group.append(self.sketch)

    def getObject(self, name: str) -> object | None:
        return next((item for item in self.Objects if item.Name == name), None)

    def recompute(self) -> None:
        self.sketch.solve()

    def openTransaction(self, label: str) -> None:
        assert not self.HasPendingTransaction
        self.HasPendingTransaction = True

    def commitTransaction(self) -> None:
        assert self.HasPendingTransaction
        self.HasPendingTransaction = False

    def abortTransaction(self) -> None:
        self.HasPendingTransaction = False


def _plan() -> tuple[ReviewedSketchBackendPlan, DocumentRef, object]:
    operation = next(
        item
        for item in REVIEWED_SKETCH_FAMILY_MANIFEST.operations
        if item.operation_id == ReviewedSketchOperation.CIRCLE.value
    )
    plan = ReviewedSketchBackendPlan(
        source_artifact_id="artifact_sketch",
        source_graph_id="graph_sketch",
        source_graph_sha256="1" * 64,
        source_content_sha256="2" * 64,
        request_digest="3" * 64,
        adapter_contract_sha256=(REVIEWED_SKETCH_FAMILY_MANIFEST.adapter.adapter_contract_sha256),
        manifest_sha256=REVIEWED_SKETCH_FAMILY_MANIFEST.manifest_sha256,
        operation_specification_sha256=operation.specification_sha256,
        sketch_id="sketch_main",
        node_id="circle_main",
        node_sha256="4" * 64,
        operation=ReviewedSketchOperation.CIRCLE,
        parameters=(
            ReviewedSketchParameter(key="cx_mm", value=0.0),
            ReviewedSketchParameter(key="cy_mm", value=0.0),
            ReviewedSketchParameter(key="radius_mm", value=5.0),
        ),
        references=(),
        results=(ReviewedSketchResult(result_id="circle_result", port_id="curve"),),
        construction=False,
        mode=None,
        enabled=None,
    )
    return (
        plan,
        REVIEWED_SKETCH_FAMILY_MANIFEST.plan_document(
            plan.canonical_bytes,
            plan.plan_sha256,
        ),
        operation,
    )


def _source_document(plan: ReviewedSketchBackendPlan) -> DocumentRef:
    return DocumentRef(
        artifact_id=plan.source_artifact_id,
        role_term_ref_id=REVIEWED_SKETCH_FAMILY_MANIFEST.intent_role_term.term_ref_id,
        schema_term_ref_id=REVIEWED_SKETCH_FAMILY_MANIFEST.intent_schema_term.term_ref_id,
        document_id=plan.source_graph_id,
        document_digest=plan.source_graph_sha256,
        content_sha256=plan.source_content_sha256,
        size_bytes=1,
        media_type=REVIEWED_SKETCH_FAMILY_MANIFEST.intent_media_type,
    )


def _registration_lowered(graph: SketchIntentGraph):
    source_document, source_payload = _graph_document(graph)
    draft = sketch_adapter._build_plan(  # noqa: SLF001
        source_document,
        source_payload,
        "a" * 64,
        execution.REVIEWED_SKETCH_REGISTRATION_MANIFEST,
    )
    plan = decode_reviewed_sketch_backend_plan(
        draft.payload,
        expected_plan_sha256=draft.semantic_plan_sha256,
    )
    plan_document = execution.REVIEWED_SKETCH_REGISTRATION_MANIFEST.plan_document(
        plan.canonical_bytes,
        plan.plan_sha256,
    )
    operation = next(
        item
        for item in execution.REVIEWED_SKETCH_REGISTRATION_MANIFEST.operations
        if item.operation_id == plan.operation.value
    )
    receipt = ReviewedPlanReceipt(
        manifest_sha256=execution.REVIEWED_SKETCH_REGISTRATION_MANIFEST.manifest_sha256,
        request_digest=plan.request_digest,
        adapter=execution.REVIEWED_SKETCH_REGISTRATION_MANIFEST.adapter,
        operation=operation,
        source_document=source_document,
        plan_document=plan_document,
    )
    return plan, plan_document, operation, receipt


def _reviewed_program(graph: SketchIntentGraph, operation: object) -> ReviewedIntentProgramV1:
    namespace, version, term_id, digest = operation.semantic_term.semantic_identity
    payload = encode_sketch_intent_graph(graph)
    return ReviewedIntentProgramV1(
        operation_id=(
            f"{execution.REVIEWED_SKETCH_REGISTRATION_MANIFEST.family_id}.{operation.operation_id}"
        ),
        semantic_operation=f"{namespace}/{version}/{term_id}@{digest}",
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(payload).hexdigest(),
        intent_graph=graph,
    )


def _registration_routes():
    spec = execution.REVIEWED_SKETCH_REGISTRATION_SPEC
    descriptor = shared_execution._ReviewedIntentFamilyDescriptor(  # noqa: SLF001
        manifest=spec.manifest,
        subject_type_term=spec.subject_type_term,
        adapter_factory=spec.adapter_factory,
        validate_plan=spec.validate_plan,
        execute_plan=spec.execute_plan,
        product_results=spec.product_results_factory(),
        intent_binding=spec.intent_binding_factory(),
        minimum_sources=spec.minimum_sources,
        maximum_sources=spec.maximum_sources,
        capture_update_state=spec.capture_update_state,
        rollback_update_state=spec.rollback_update_state,
    )
    return shared_execution._routes_for_family(  # noqa: SLF001
        descriptor,
        spec.operation_ids,
    )


def _install_circle(sketch: _Sketch, plan: ReviewedSketchBackendPlan) -> None:
    index = sketch.addGeometry(_Circle(), False)
    native = ReviewedSketchNativeResult(
        result_id="circle_result",
        port_id="curve",
        geometry_index=index,
        geometry_type_id="Part::GeomCircle",
    )
    metadata = {
        "schema_version": 1,
        "sketch_id": plan.sketch_id,
        "geometries": [
            {
                "geometry_id": plan.node_id,
                "node_sha256": plan.node_sha256,
                "operation": plan.operation.value,
                "geometry_indices": [index],
                "internal_constraint_indices": [],
                "native_fingerprint_sha256": rules._geometry_fingerprint(sketch, (index,)),
                "results": [native.to_mapping()],
            }
        ],
        "constraints": [],
    }
    sketch.VibeCADReviewedSketchIntent = json.dumps(
        metadata,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fake_apply(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: object,
) -> ReviewedSketchConformanceReceipt:
    plan = rules.decode_reviewed_sketch_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    _install_circle(bindings.sketch, plan)
    bindings.document.recompute()
    return ReviewedSketchConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        sketch_object_name=bindings.sketch.Name,
        sketch_id=plan.sketch_id,
        node_id=plan.node_id,
        node_sha256=plan.node_sha256,
        native_results=(
            ReviewedSketchNativeResult(
                result_id="circle_result",
                port_id="curve",
                geometry_index=0,
                geometry_type_id="Part::GeomCircle",
            ),
        ),
        geometry_indices=(0,),
        constraint_indices=(),
        dof=bindings.sketch.DoF,
        fully_constrained=False,
    )


def test_family_freezes_twenty_exact_update_only_routes_and_reports_subject_gap() -> None:
    spec = execution.REVIEWED_SKETCH_FAMILY_SPEC
    assert len(execution.REVIEWED_SKETCH_PRODUCT_IDENTITIES) == 20
    assert set(spec.operation_ids) == {item.value for item in ReviewedSketchOperation}
    assert spec.create_operation_ids == ()
    assert spec.update_primary_operation_ids == spec.operation_ids
    assert (spec.minimum_sources, spec.maximum_sources) == (1, 1)
    assert execution.REVIEWED_SKETCH_SHARED_REGISTRATION_READY is False
    assert len(execution.REVIEWED_SKETCH_SHARED_REGISTRATION_BLOCKERS) == 4
    assert spec.subject_type_term == SKETCH_ROOT_SEMANTIC_TYPE_TERM
    # The current manifest omitted the codec's actual semantic subject type.
    # Registration must add this exact term; substituting a selector/schema
    # term would weaken the proof endpoint.
    assert spec.subject_type_term not in spec.manifest.request_terms
    assert isinstance(spec.adapter_factory(_MemorySink()), ExactReviewedFamilyAdapter)


def test_registration_manifest_adds_only_root_type_without_claiming_attestation() -> None:
    original = REVIEWED_SKETCH_FAMILY_MANIFEST
    manifest = execution.REVIEWED_SKETCH_REGISTRATION_MANIFEST
    spec = execution.REVIEWED_SKETCH_REGISTRATION_SPEC

    assert original.family_version == "1.0.0"
    assert SKETCH_ROOT_SEMANTIC_TYPE_TERM not in original.request_terms
    assert manifest.family_version == "1.0.1"
    assert manifest.manifest_sha256 != original.manifest_sha256
    assert set(manifest.request_terms) == {
        *original.request_terms,
        SKETCH_ROOT_SEMANTIC_TYPE_TERM,
    }
    assert manifest.operations == original.operations
    assert manifest.adapter == original.adapter
    assert manifest.rule_id == original.rule_id
    assert manifest.rule_contract_sha256 == original.rule_contract_sha256
    assert spec.manifest is manifest
    assert spec.subject_type_term == SKETCH_ROOT_SEMANTIC_TYPE_TERM
    assert execution.REVIEWED_SKETCH_REGISTRATION_MATERIAL_READY is True
    assert execution.REVIEWED_SKETCH_REGISTRATION_MANIFEST_HAS_VERIFICATION_RECEIPT is False
    assert spec.compatibility_manifest_has_verification_receipt is False
    assert execution.REVIEWED_SKETCH_PUBLIC_POSITIVE_READY is False
    assert execution.REVIEWED_SKETCH_PUBLIC_POSITIVE_BLOCKERS == (
        "no-reviewed-sketch-object-create-producer",
    )
    assert all(
        route.manifest.family_id != manifest.family_id
        for route in shared_execution.CURRENT_REVIEWED_INTENT_ROUTES
    )


def test_registration_spec_freezes_twenty_full_update_primary_contracts() -> None:
    spec = execution.REVIEWED_SKETCH_REGISTRATION_SPEC
    assert len(execution.REVIEWED_SKETCH_PRODUCT_IDENTITIES) == 20
    assert len(spec.product_results) == 20
    assert len(set(execution.REVIEWED_SKETCH_PRODUCT_IDENTITIES)) == 20
    assert spec.product_identities == execution.REVIEWED_SKETCH_PRODUCT_IDENTITIES
    assert spec.operation_ids == tuple(item.value for item in ReviewedSketchOperation)
    assert spec.create_operation_ids == ()
    assert spec.update_primary_operation_ids == spec.operation_ids
    assert (spec.minimum_sources, spec.maximum_sources) == (1, 1)
    assert spec.capture_update_state is execution.capture_reviewed_sketch_update_state
    assert spec.rollback_update_state is execution.rollback_reviewed_sketch_update_state
    for operation, identity, result in zip(
        execution.REVIEWED_SKETCH_PRODUCT_OPERATIONS,
        execution.REVIEWED_SKETCH_PRODUCT_IDENTITIES,
        spec.product_results,
        strict=True,
    ):
        reviewed = next(
            item for item in spec.manifest.operations if item.operation_id == operation.value
        )
        namespace, version, term_id, digest = reviewed.semantic_term.semantic_identity
        assert identity == (
            f"{spec.manifest.family_id}.{operation.value}",
            f"{namespace}/{version}/{term_id}@{digest}",
        )
        assert execution.resolve_reviewed_sketch_operation(*identity) is reviewed
        assert result.operation_id == operation.value
        assert result.result_kind == "reference"
        assert result.owned_type_ids == ("Sketcher::SketchObject",)
        assert result.semantic_roles == (execution.SemanticRole.FEATURE,)
        assert result.source_count == 1
        assert result.execution_mode == "update_primary"
        assert result.primary_is_source is True

    shared_results = spec.product_results_factory()
    assert len(shared_results) == 20
    assert all(item.result_kind.value == "reference" for item in shared_results)
    assert all(item.execution_mode.value == "update_primary" for item in shared_results)
    assert all(item.source_count == 1 for item in shared_results)

    descriptor = shared_execution._ReviewedIntentFamilyDescriptor(  # noqa: SLF001
        manifest=spec.manifest,
        subject_type_term=spec.subject_type_term,
        adapter_factory=spec.adapter_factory,
        validate_plan=spec.validate_plan,
        execute_plan=spec.execute_plan,
        product_results=shared_results,
        intent_binding=spec.intent_binding_factory(),
        minimum_sources=spec.minimum_sources,
        maximum_sources=spec.maximum_sources,
        capture_update_state=spec.capture_update_state,
        rollback_update_state=spec.rollback_update_state,
    )
    assert descriptor.manifest is spec.manifest
    assert descriptor.product_results == shared_results
    assert descriptor.minimum_sources == descriptor.maximum_sources == 1
    routes = shared_execution._routes_for_family(  # noqa: SLF001
        descriptor,
        spec.operation_ids,
    )
    assert len(routes) == 20
    assert tuple((item.operation_id, item.semantic_operation) for item in routes) == (
        execution.REVIEWED_SKETCH_PRODUCT_IDENTITIES
    )
    assert all(item not in shared_execution.CURRENT_REVIEWED_INTENT_ROUTES for item in routes)


def test_registration_exact_adapter_and_compatibility_plan_binding() -> None:
    spec = execution.REVIEWED_SKETCH_REGISTRATION_SPEC
    adapter = spec.adapter_factory(_MemorySink())
    assert type(adapter) is execution.FreeCADReviewedSketchRegistrationAdapter
    assert isinstance(adapter, ExactReviewedFamilyAdapter)
    assert adapter.manifest is spec.manifest
    assert adapter.descriptor == spec.manifest.adapter

    plan, _, operation, receipt = _registration_lowered(
        _geometry_graph(ReviewedSketchOperation.CIRCLE)
    )
    spec.validate_plan(plan, receipt, operation)
    assert plan.manifest_sha256 == spec.manifest.manifest_sha256
    with pytest.raises(ReviewedIntentExecutionError):
        execution.validate_reviewed_sketch_plan(plan, receipt, operation)
    with pytest.raises(ReviewedIntentExecutionError):
        spec.validate_plan(
            plan,
            dataclasses.replace(
                receipt,
                manifest_sha256=REVIEWED_SKETCH_FAMILY_MANIFEST.manifest_sha256,
            ),
            operation,
        )


@pytest.mark.parametrize(
    ("operation", "graph_factory", "expected_selector"),
    (
        (
            ReviewedSketchOperation.CIRCLE,
            _geometry_graph,
            SKETCH_GEOMETRY_SELECTOR_TERM,
        ),
        (
            ReviewedSketchOperation.HORIZONTAL,
            _constraint_graph,
            SKETCH_CONSTRAINT_SELECTOR_TERM,
        ),
    ),
)
def test_registration_binding_maps_geometry_and_constraint_subjects_exactly(
    operation: ReviewedSketchOperation,
    graph_factory,
    expected_selector,
) -> None:
    spec = execution.REVIEWED_SKETCH_REGISTRATION_SPEC
    graph = graph_factory(operation)
    reviewed = next(
        item for item in spec.manifest.operations if item.operation_id == operation.value
    )
    program = _reviewed_program(graph, reviewed)
    binding = spec.intent_binding_factory()
    selected = binding.materialize(program, reviewed)

    assert binding.root_subject_type_term == SKETCH_ROOT_SEMANTIC_TYPE_TERM
    assert selected.selector_kind_term == expected_selector
    assert selected.subject_type_term == reviewed.semantic_term
    assert selected.selector_id == (
        graph.geometries[-1].geometry_id
        if operation is ReviewedSketchOperation.CIRCLE
        else graph.constraints[0].constraint_id
    )
    payload = encode_sketch_intent_graph(graph)
    document = DocumentRef(
        artifact_id="artifact_registration_binding",
        role_term_ref_id=spec.manifest.intent_role_term.term_ref_id,
        schema_term_ref_id=spec.manifest.intent_schema_term.term_ref_id,
        document_id=graph.graph_id,
        document_digest=graph.graph_sha256,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        media_type=spec.manifest.intent_media_type,
    )
    subject = SubjectRef(
        artifact_id=document.artifact_id,
        selector_kind_term_ref_id=selected.selector_kind_term.term_ref_id,
        selector_id=selected.selector_id,
    )
    resolved = binding.build_codec().resolve_subject(document, payload, subject)
    assert resolved is not None
    assert resolved.semantic_type == reviewed.semantic_term


@pytest.mark.parametrize(
    ("operation", "graph_factory"),
    (
        (ReviewedSketchOperation.CIRCLE, _geometry_graph),
        (ReviewedSketchOperation.HORIZONTAL, _constraint_graph),
    ),
)
def test_unregistered_geometry_and_constraint_routes_lower_with_sketch_codec(
    monkeypatch: pytest.MonkeyPatch,
    operation: ReviewedSketchOperation,
    graph_factory,
) -> None:
    routes = _registration_routes()
    monkeypatch.setattr(shared_execution, "CURRENT_REVIEWED_INTENT_ROUTES", routes)
    monkeypatch.setattr(
        shared_execution,
        "_ROUTES_BY_IDENTITY",
        shared_execution._index_routes(routes),  # noqa: SLF001
    )
    reviewed = next(
        item
        for item in execution.REVIEWED_SKETCH_REGISTRATION_MANIFEST.operations
        if item.operation_id == operation.value
    )
    lowered = shared_execution.lower_reviewed_intent(
        _reviewed_program(graph_factory(operation), reviewed)
    )
    assert lowered.route in routes
    assert lowered.route.operation is reviewed
    assert lowered.plan.operation is operation
    assert lowered.plan.manifest_sha256 == (
        execution.REVIEWED_SKETCH_REGISTRATION_MANIFEST.manifest_sha256
    )
    execution.validate_reviewed_sketch_registration_plan(
        lowered.plan,
        lowered.receipt,
        reviewed,
    )


def test_registration_compat_plan_uses_existing_capture_execute_rollback_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, plan_document, operation, _ = _registration_lowered(
        _geometry_graph(ReviewedSketchOperation.CIRCLE)
    )
    document = _Document(body_owned=True)
    monkeypatch.setattr(execution, "apply_reviewed_sketch_plan", _fake_apply)
    result = execution.execute_reviewed_sketch_plan_on_bound_sketch(
        document,
        plan,
        plan.canonical_bytes,
        plan_document,
        operation,
        document.sketch,
        manifest=execution.REVIEWED_SKETCH_REGISTRATION_MANIFEST,
    )
    assert result.object is document.sketch
    assert result.receipt.closed_profile is True
    assert result.state_sha256 == result.receipt.state_sha256


def test_exact_identity_and_plan_binding_reject_rebound_or_tampered_inputs() -> None:
    plan, plan_document, operation = _plan()
    identity = next(
        item for item in execution.REVIEWED_SKETCH_PRODUCT_IDENTITIES if item[0].endswith(".circle")
    )
    assert execution.resolve_reviewed_sketch_operation(*identity) == operation
    assert execution.resolve_reviewed_sketch_operation(identity[0], identity[1] + "x") is None
    assert (
        execution.resolve_reviewed_sketch_operation("freecad_reviewed_sketch.unknown", "x") is None
    )

    receipt = ReviewedPlanReceipt(
        manifest_sha256=REVIEWED_SKETCH_FAMILY_MANIFEST.manifest_sha256,
        request_digest=plan.request_digest,
        adapter=REVIEWED_SKETCH_FAMILY_MANIFEST.adapter,
        operation=operation,
        source_document=_source_document(plan),
        plan_document=plan_document,
    )
    execution.validate_reviewed_sketch_plan(plan, receipt, operation)
    wrong_operation = next(
        item for item in REVIEWED_SKETCH_FAMILY_MANIFEST.operations if item.operation_id == "line"
    )
    with pytest.raises(ReviewedIntentExecutionError):
        execution.validate_reviewed_sketch_plan(plan, receipt, wrong_operation)
    with pytest.raises(ReviewedIntentExecutionError):
        execution.execute_reviewed_sketch_plan_on_bound_sketch(
            _Document(),
            plan,
            plan.canonical_bytes + b" ",
            plan_document,
            operation,
            _Document().sketch,
        )


def test_capture_binds_solver_geometry_metadata_shape_and_rejects_overconstraint() -> None:
    document = _Document()
    state = execution.capture_reviewed_sketch_native_state(
        document,
        document.sketch,
        sketch_id="sketch_main",
    )
    assert state.geometries == state.constraints == ()
    assert state.owner.kind is execution.ReviewedSketchOwnerKind.DOCUMENT_ROOT
    assert state.metadata_present is False
    assert len(state.state_sha256) == 64

    document.sketch.ConflictingConstraints = (0,)
    with pytest.raises(ReviewedIntentExecutionError):
        execution.capture_reviewed_sketch_native_state(
            document,
            document.sketch,
            sketch_id="sketch_main",
        )


def test_family_update_snapshot_restores_exact_empty_sketch_and_rejects_capsule_tamper() -> None:
    document = _Document()
    source = SimpleNamespace(
        object=document.sketch,
        owned_objects=(document.sketch,),
        native_receipt=SimpleNamespace(sketch_id="sketch_main"),
    )
    context = SimpleNamespace(document=document, source_results=(source,))
    operation = REVIEWED_SKETCH_FAMILY_MANIFEST.operations[0]
    snapshot = execution.capture_reviewed_sketch_update_state(document, operation, context)
    plan, _, _ = _plan()
    _install_circle(document.sketch, plan)
    document.recompute()
    assert document.sketch.GeometryCount == 1

    execution.rollback_reviewed_sketch_update_state(
        document,
        snapshot,
        operation,
        context,
    )
    assert document.sketch.GeometryCount == 0
    restored = execution.capture_reviewed_sketch_native_state(
        document,
        document.sketch,
        sketch_id="sketch_main",
    )
    assert restored.state_sha256 == snapshot.state_sha256

    tampered = SimpleNamespace(
        primary=snapshot.primary,
        owned_objects=snapshot.owned_objects,
        state_sha256="f" * 64,
        rollback_state=snapshot.rollback_state,
    )
    with pytest.raises(ReviewedIntentExecutionError):
        execution.rollback_reviewed_sketch_update_state(
            document,
            tampered,
            operation,
            context,
        )


def test_circle_internal_execute_adopts_live_body_owned_closed_profile(monkeypatch) -> None:
    document = _Document(body_owned=True)
    plan, plan_document, operation = _plan()
    monkeypatch.setattr(execution, "apply_reviewed_sketch_plan", _fake_apply)
    result = execution.execute_reviewed_sketch_plan_on_bound_sketch(
        document,
        plan,
        plan.canonical_bytes,
        plan_document,
        operation,
        document.sketch,
    )
    assert result.object is document.sketch
    assert result.receipt.owner.kind is execution.ReviewedSketchOwnerKind.PARTDESIGN_BODY
    assert result.receipt.closed_profile is True
    assert (
        result.receipt.result_shape_sha256
        == hashlib.sha256(document.sketch.Shape.exportBrepToString().encode()).hexdigest()
    )
    result.receipt.validate_profile_source(document, document.sketch)

    document.sketch.Geometry[0].Radius = 7.0
    with pytest.raises(ReviewedIntentExecutionError):
        result.receipt.validate_native_result(document, document.sketch)


def test_document_root_closed_profile_is_honest_but_not_partdesign_profile(monkeypatch) -> None:
    document = _Document()
    plan, plan_document, operation = _plan()
    monkeypatch.setattr(execution, "apply_reviewed_sketch_plan", _fake_apply)
    result = execution.execute_reviewed_sketch_plan_on_bound_sketch(
        document,
        plan,
        plan.canonical_bytes,
        plan_document,
        operation,
        document.sketch,
    )
    assert result.receipt.closed_profile is True
    assert result.receipt.owner.kind is execution.ReviewedSketchOwnerKind.DOCUMENT_ROOT
    with pytest.raises(ReviewedIntentExecutionError):
        result.receipt.validate_profile_source(document, document.sketch)


def test_public_callback_rejects_wrong_source_before_native_mutation(monkeypatch) -> None:
    document = _Document(body_owned=True)
    plan, plan_document, operation = _plan()
    called = False

    def forbidden_apply(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(execution, "apply_reviewed_sketch_plan", forbidden_apply)
    context = SimpleNamespace(
        document=document,
        source_results=(
            SimpleNamespace(
                object=document.sketch,
                owned_objects=(document.sketch,),
                native_receipt=SimpleNamespace(sketch_id="sketch_main"),
            ),
        ),
    )
    with pytest.raises(ReviewedIntentExecutionError):
        execution.execute_reviewed_sketch_plan(
            document,
            plan,
            plan.canonical_bytes,
            plan_document,
            operation,
            context,
        )
    assert called is False
    assert document.sketch.GeometryCount == 0
