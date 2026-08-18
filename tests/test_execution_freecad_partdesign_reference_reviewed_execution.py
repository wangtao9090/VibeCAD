"""Internal product gates for content-bound PartDesign reference selection."""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from vibecad.execution import freecad_partdesign_reference_reviewed_execution as reference_execution
from vibecad.execution.freecad_partdesign_primitive_reviewed_execution import (
    PartDesignReviewedBaseBinding,
)
from vibecad.execution.freecad_partdesign_reference_reviewed_execution import (
    PARTDESIGN_REFERENCE_COMPAT_MANIFEST,
    PARTDESIGN_REFERENCE_REQUIRED_SOURCE_ROLES,
    PARTDESIGN_REFERENCE_REVIEWED_OPERATIONS,
    _authenticated_bindings,
    build_partdesign_reference_reviewed_family_descriptor,
    execute_partdesign_reference_reviewed_plan_with_sources,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedNativeExecutionResult,
    _ReviewedProductResultKind,
)
from vibecad.execution.selectors import (
    EntityIdentity,
    Provenance,
    ProvenanceSource,
    SemanticRole,
)
from vibecad.parametric.freecad_partdesign_reference_rules import (
    PartDesignReferenceKind,
    PartDesignReferencePlan,
    ReferenceConformanceReceipt,
    ReferenceExecutionBindings,
    ReferenceRuleError,
    ReviewedReferenceSemanticRole,
    ReviewedSubelementKind,
    _validate_bindings,
    locate_reviewed_reference_subelement,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class _Vector:
    x: float
    y: float
    z: float


class Plane:
    pass


class Line:
    pass


class _Vertex:
    def __init__(self, point: tuple[float, float, float]) -> None:
        self.Point = _Vector(*point)
        self._point = point

    def exportBrepToString(self) -> str:
        return "vertex:" + ",".join(f"{item:.9g}" for item in self._point)


class _Edge:
    Curve = Line()

    def __init__(self, left: _Vertex, right: _Vertex) -> None:
        self.Vertexes = (left, right)

    def exportBrepToString(self) -> str:
        points = sorted(vertex.exportBrepToString() for vertex in self.Vertexes)
        return "edge:" + "|".join(points)


class _Face:
    Surface = Plane()

    def __init__(self, vertices: tuple[_Vertex, ...]) -> None:
        self.Vertexes = vertices

    def exportBrepToString(self) -> str:
        return "face:" + "|".join(sorted(vertex.exportBrepToString() for vertex in self.Vertexes))


@dataclass(frozen=True)
class _BoundBox:
    XMin: float = 0.0
    YMin: float = 0.0
    ZMin: float = 0.0
    XMax: float = 20.0
    YMax: float = 20.0
    ZMax: float = 10.0
    DiagonalLength: float = 30.0


class _Shape:
    def __init__(
        self,
        token: str,
        *,
        reverse_edges: bool = False,
        ambiguous_top: bool = False,
    ) -> None:
        coordinates = (
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 10.0),
            (0.0, 20.0, 0.0),
            (0.0, 20.0, 10.0),
            (20.0, 0.0, 0.0),
            (20.0, 0.0, 10.0),
            (20.0, 20.0, 0.0),
            (20.0, 20.0, 10.0),
        )
        by_point = {point: _Vertex(point) for point in coordinates}
        self.Vertexes = tuple(by_point.values())
        edge_points = (
            ((0.0, 0.0, 0.0), (20.0, 0.0, 0.0)),
            ((0.0, 0.0, 10.0), (20.0, 0.0, 10.0)),
            ((0.0, 20.0, 0.0), (20.0, 20.0, 0.0)),
            ((0.0, 20.0, 10.0), (20.0, 20.0, 10.0)),
            ((0.0, 0.0, 0.0), (0.0, 20.0, 0.0)),
            ((0.0, 0.0, 10.0), (0.0, 20.0, 10.0)),
            ((20.0, 0.0, 0.0), (20.0, 20.0, 0.0)),
            ((20.0, 0.0, 10.0), (20.0, 20.0, 10.0)),
            ((0.0, 0.0, 0.0), (0.0, 0.0, 10.0)),
            ((0.0, 20.0, 0.0), (0.0, 20.0, 10.0)),
            ((20.0, 0.0, 0.0), (20.0, 0.0, 10.0)),
            ((20.0, 20.0, 0.0), (20.0, 20.0, 10.0)),
        )
        edges = tuple(_Edge(by_point[left], by_point[right]) for left, right in edge_points)
        self.Edges = tuple(reversed(edges)) if reverse_edges else edges
        face_points = (
            tuple(point for point in coordinates if point[0] == 0.0),
            tuple(point for point in coordinates if point[0] == 20.0),
            tuple(point for point in coordinates if point[1] == 0.0),
            tuple(point for point in coordinates if point[1] == 20.0),
            tuple(point for point in coordinates if point[2] == 0.0),
            tuple(point for point in coordinates if point[2] == 10.0),
        )
        faces = tuple(_Face(tuple(by_point[point] for point in points)) for points in face_points)
        self.Faces = (*faces, faces[-1]) if ambiguous_top else faces
        self.BoundBox = _BoundBox()
        self.Solids = (object(),)
        self.Volume = 4000.0
        self._token = token

    def exportBrepToString(self) -> str:
        return self._token

    def isNull(self) -> bool:
        return False

    def isValid(self) -> bool:
        return True


class _Feature:
    def __init__(
        self,
        document: _Document,
        type_id: str,
        shape: _Shape,
        *,
        name: str = "Source",
    ) -> None:
        self.Document = document
        self.Name = name
        self.TypeId = type_id
        self.Shape = shape
        self.State = ("Up-to-date",)

    def isValid(self) -> bool:
        return True


class _Body:
    TypeId = "PartDesign::Body"

    def __init__(self, document: _Document) -> None:
        self.Document = document
        self.Group: list[object] = []
        self.Tip: object | None = None


class _Document:
    UndoMode = 1
    HasPendingTransaction = False

    def __init__(self) -> None:
        self.Objects: list[object] = []
        self.mutation_calls = 0

    def getObject(self, name: str):
        return next((item for item in self.Objects if getattr(item, "Name", None) == name), None)


class _Session:
    def __init__(self, document: _Document, identities: dict[object, EntityIdentity]) -> None:
        self.doc = document
        self._identities = identities

    def read_object_identity(self, item: object) -> EntityIdentity:
        return self._identities[item]


def _identity(item: object, role: SemanticRole) -> EntityIdentity:
    return EntityIdentity(
        object_id="object_" + _sha(f"object:{id(item)}")[:32],
        feature_id="feature_" + _sha(f"feature:{id(item)}")[:32],
        object_type=item.TypeId,
        semantic_role=role,
        provenance=Provenance(
            source=ProvenanceSource.MODEL,
            operation_id="apply_reviewed_intent",
        ),
    )


def _plan(kind: PartDesignReferenceKind, support_content_sha256: str) -> PartDesignReferencePlan:
    return PartDesignReferencePlan(
        source_artifact_id="artifact_reference",
        source_graph_id="graph_reference",
        source_graph_sha256=_sha("graph"),
        source_content_sha256=_sha("content"),
        lowering_request_sha256=_sha("request"),
        adapter_contract_sha256=(
            PARTDESIGN_REFERENCE_COMPAT_MANIFEST.adapter.adapter_contract_sha256
        ),
        body_id="body_reference",
        node_id=f"node_{kind.value}",
        result_id=f"result_{kind.value}",
        support_reference_id="reference_support",
        support_reference_sha256=support_content_sha256,
        kind=kind,
    )


def _source_result(
    item: _Feature,
    *,
    content_sha256: str,
    token: object,
    base_binding: PartDesignReviewedBaseBinding | None,
) -> ReviewedNativeExecutionResult:
    route = REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES[0]
    plan_sha256 = _sha(f"source-plan:{id(item)}")
    values = {
        "operation": SimpleNamespace(value=route.operation.operation_id),
        "plan_sha256": plan_sha256,
        "receipt_sha256": _sha(f"source-receipt:{id(item)}"),
        "result_shape_sha256": hashlib.sha256(item.Shape.exportBrepToString().encode()).hexdigest(),
    }
    if base_binding is not None:
        values["partdesign_base_binding"] = base_binding
    result = object.__new__(ReviewedNativeExecutionResult)
    for name, value in (
        ("route", route),
        ("object", item),
        ("plan_sha256", plan_sha256),
        ("plan_content_sha256", content_sha256),
        ("native_receipt", SimpleNamespace(**values)),
        ("semantic_roles", (SemanticRole.FEATURE,)),
        ("result_kind", _ReviewedProductResultKind.SOLID),
        ("_retained_run_token", token),
    ):
        object.__setattr__(result, name, value)
    return result


@dataclass(frozen=True)
class _Fixture:
    document: _Document
    session: _Session
    target: _Feature
    support: _Feature
    plan: PartDesignReferencePlan
    plan_document: object
    sources: tuple[ReviewedNativeExecutionResult, ...]
    token: object


def _fixture(
    kind: PartDesignReferenceKind,
    *,
    ambiguous_support: bool = False,
) -> _Fixture:
    document = _Document()
    body = _Body(document)
    route = REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES[0]
    target = _Feature(document, route.operation.native_type_id, _Shape("target-shape"))
    support = (
        target
        if kind
        in {
            PartDesignReferenceKind.DATUM_PLANE,
            PartDesignReferenceKind.DATUM_LINE,
            PartDesignReferenceKind.DATUM_POINT,
        }
        else _Feature(
            document,
            route.operation.native_type_id,
            _Shape("support-shape", ambiguous_top=ambiguous_support),
        )
    )
    body.Group = [target]
    body.Tip = target
    document.Objects = [body, target] if support is target else [body, target, support]
    identities = {
        body: _identity(body, SemanticRole.PART),
        target: _identity(target, SemanticRole.FEATURE),
    }
    if support is not target:
        identities[support] = _identity(support, SemanticRole.FEATURE)
    session = _Session(document, identities)
    token = object()
    target_content = _sha("target-content")
    support_content = target_content if support is target else _sha("support-content")
    binding = PartDesignReviewedBaseBinding(
        object=target,
        body=body,
        body_closure=(body,),
        result_shape_sha256=hashlib.sha256(target.Shape.exportBrepToString().encode()).hexdigest(),
    )
    target_source = _source_result(
        target,
        content_sha256=target_content,
        token=token,
        base_binding=binding,
    )
    sources = (
        (target_source,)
        if support is target
        else (
            target_source,
            _source_result(
                support,
                content_sha256=support_content,
                token=token,
                base_binding=None,
            ),
        )
    )
    plan = _plan(kind, support_content)
    return _Fixture(
        document=document,
        session=session,
        target=target,
        support=support,
        plan=plan,
        plan_document=PARTDESIGN_REFERENCE_COMPAT_MANIFEST.plan_document(
            plan.canonical_bytes,
            plan.plan_sha256,
        ),
        sources=sources,
        token=token,
    )


@pytest.mark.parametrize(
    ("kind", "expected_kind", "expected_role"),
    (
        (
            PartDesignReferenceKind.DATUM_PLANE,
            ReviewedSubelementKind.FACE,
            ReviewedReferenceSemanticRole.AXIS_ALIGNED_FACE_Z_POSITIVE,
        ),
        (
            PartDesignReferenceKind.DATUM_LINE,
            ReviewedSubelementKind.EDGE,
            ReviewedReferenceSemanticRole.AXIS_ALIGNED_EDGE_X_Y_NEGATIVE_Z_POSITIVE,
        ),
        (
            PartDesignReferenceKind.DATUM_POINT,
            ReviewedSubelementKind.VERTEX,
            ReviewedReferenceSemanticRole.AXIS_ALIGNED_VERTEX_X_POSITIVE_Y_POSITIVE_Z_POSITIVE,
        ),
        (
            PartDesignReferenceKind.SHAPE_BINDER,
            ReviewedSubelementKind.WHOLE_OBJECT,
            ReviewedReferenceSemanticRole.WHOLE_OBJECT,
        ),
        (
            PartDesignReferenceKind.SUBSHAPE_BINDER,
            ReviewedSubelementKind.FACE,
            ReviewedReferenceSemanticRole.AXIS_ALIGNED_FACE_Z_POSITIVE,
        ),
    ),
)
def test_locator_seals_all_five_unique_semantic_roles(
    kind: PartDesignReferenceKind,
    expected_kind: ReviewedSubelementKind,
    expected_role: ReviewedReferenceSemanticRole,
) -> None:
    plan = _plan(kind, _sha("source-content"))
    receipt = locate_reviewed_reference_subelement(
        plan=plan,
        reference_plan_content_sha256=hashlib.sha256(plan.canonical_bytes).hexdigest(),
        source_shape=_Shape("canonical-box"),
        source_plan_sha256=_sha("source-plan"),
        source_plan_content_sha256=plan.support_reference_sha256,
        source_native_receipt_sha256=_sha("source-receipt"),
        target_body_entity_identity_sha256=_sha("target-body-identity"),
        support_entity_identity_sha256=_sha("support-identity"),
    )

    assert receipt.subelement_kind is expected_kind
    assert receipt.semantic_role is expected_role
    assert receipt.executable is False and receipt.grants_execution_authority is False
    assert receipt.reference_plan_sha256 == plan.plan_sha256


def test_locator_geometric_signature_is_independent_of_native_edge_order() -> None:
    plan = _plan(PartDesignReferenceKind.DATUM_LINE, _sha("source-content"))
    kwargs = {
        "plan": plan,
        "reference_plan_content_sha256": hashlib.sha256(plan.canonical_bytes).hexdigest(),
        "source_plan_sha256": _sha("source-plan"),
        "source_plan_content_sha256": plan.support_reference_sha256,
        "source_native_receipt_sha256": _sha("source-receipt"),
        "target_body_entity_identity_sha256": _sha("target-body-identity"),
        "support_entity_identity_sha256": _sha("support-identity"),
    }
    forward = locate_reviewed_reference_subelement(source_shape=_Shape("canonical-box"), **kwargs)
    reversed_order = locate_reviewed_reference_subelement(
        source_shape=_Shape("canonical-box", reverse_edges=True), **kwargs
    )

    assert forward.semantic_role is reversed_order.semantic_role
    assert forward.geometric_signature_sha256 == reversed_order.geometric_signature_sha256
    assert forward.support_subname != reversed_order.support_subname


@pytest.mark.parametrize("kind", tuple(PartDesignReferenceKind))
def test_same_run_binding_is_positive_and_body_is_taken_from_authenticated_binding(
    monkeypatch: pytest.MonkeyPatch,
    kind: PartDesignReferenceKind,
) -> None:
    fixture = _fixture(kind)
    monkeypatch.setattr(PartDesignReviewedBaseBinding, "validate", lambda self, document: None)

    bindings, support, selection = _authenticated_bindings(
        fixture.document,
        fixture.plan,
        fixture.plan_document,
        fixture.sources,
        session=fixture.session,
        run_token=fixture.token,
    )

    assert bindings.body is fixture.target.Document.Objects[0]
    assert support is fixture.support
    assert bindings.selection_receipt is selection
    assert fixture.document.mutation_calls == 0


@pytest.mark.parametrize("kind", tuple(PartDesignReferenceKind))
def test_native_callback_returns_bound_reference_ownership_for_all_five(
    monkeypatch: pytest.MonkeyPatch,
    kind: PartDesignReferenceKind,
) -> None:
    fixture = _fixture(kind)
    operation = next(
        item
        for item in PARTDESIGN_REFERENCE_COMPAT_MANIFEST.operations
        if item.operation_id == kind.value
    )
    monkeypatch.setattr(PartDesignReviewedBaseBinding, "validate", lambda self, document: None)

    def apply_fake(
        raw: bytes,
        *,
        expected_content_sha256: str,
        expected_plan_sha256: str,
        bindings: ReferenceExecutionBindings,
    ) -> ReferenceConformanceReceipt:
        assert raw == fixture.plan.canonical_bytes
        assert expected_content_sha256 == fixture.plan_document.content_sha256
        assert expected_plan_sha256 == fixture.plan.plan_sha256
        fixture.document.mutation_calls += 1
        result = _Feature(
            fixture.document,
            operation.native_type_id,
            _Shape(f"result-{kind.value}"),
            name=f"Reference_{kind.value}",
        )
        fixture.document.Objects.append(result)
        bindings.body.Group.append(result)
        selection = bindings.selection_receipt
        return ReferenceConformanceReceipt(
            plan_sha256=fixture.plan.plan_sha256,
            object_name=result.Name,
            kind=kind,
            support_subname=selection.support_subname,
            selection_receipt_sha256=selection.receipt_sha256,
            face_count=1,
            edge_count=1,
            vertex_count=1,
        )

    monkeypatch.setattr(reference_execution, "apply_partdesign_reference_plan", apply_fake)
    result = execute_partdesign_reference_reviewed_plan_with_sources(
        fixture.document,
        fixture.plan,
        fixture.plan.canonical_bytes,
        fixture.plan_document,
        operation,
        fixture.sources,
        session=fixture.session,
        run_token=fixture.token,
    )

    assert result.object is fixture.document.Objects[-1]
    assert result.receipt.plan_sha256 == fixture.plan.plan_sha256
    assert result.receipt.operation is kind
    assert fixture.document.mutation_calls == 1


@pytest.mark.parametrize(
    ("kind", "delta"),
    (
        (PartDesignReferenceKind.DATUM_PLANE, -1),
        (PartDesignReferenceKind.DATUM_PLANE, 1),
        (PartDesignReferenceKind.SHAPE_BINDER, -1),
        (PartDesignReferenceKind.SHAPE_BINDER, 1),
    ),
)
def test_source_count_n_plus_or_minus_one_fails_before_mutation(
    kind: PartDesignReferenceKind,
    delta: int,
) -> None:
    fixture = _fixture(kind)
    expected = len(PARTDESIGN_REFERENCE_REQUIRED_SOURCE_ROLES[kind.value])
    actual = expected + delta
    sources = tuple((*fixture.sources, *fixture.sources, *fixture.sources)[:actual])

    with pytest.raises(ReviewedIntentExecutionError):
        _authenticated_bindings(
            fixture.document,
            fixture.plan,
            fixture.plan_document,
            sources,
            session=fixture.session,
            run_token=fixture.token,
        )
    assert fixture.document.mutation_calls == 0


def test_binder_source_order_and_stale_run_fail_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(PartDesignReferenceKind.SHAPE_BINDER)
    monkeypatch.setattr(PartDesignReviewedBaseBinding, "validate", lambda self, document: None)

    with pytest.raises(ReviewedIntentExecutionError):
        _authenticated_bindings(
            fixture.document,
            fixture.plan,
            fixture.plan_document,
            tuple(reversed(fixture.sources)),
            session=fixture.session,
            run_token=fixture.token,
        )
    with pytest.raises(ReviewedIntentExecutionError):
        _authenticated_bindings(
            fixture.document,
            fixture.plan,
            fixture.plan_document,
            fixture.sources,
            session=fixture.session,
            run_token=object(),
        )
    assert fixture.document.mutation_calls == 0


def test_stale_shape_tampered_signature_and_ambiguous_role_fail_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(PartDesignReferenceKind.DATUM_PLANE)
    monkeypatch.setattr(PartDesignReviewedBaseBinding, "validate", lambda self, document: None)
    bindings, _support, selection = _authenticated_bindings(
        fixture.document,
        fixture.plan,
        fixture.plan_document,
        fixture.sources,
        session=fixture.session,
        run_token=fixture.token,
    )
    tampered = ReferenceExecutionBindings(
        document=bindings.document,
        body=bindings.body,
        support=bindings.support,
        body_id=bindings.body_id,
        support_reference_id=bindings.support_reference_id,
        support_reference_sha256=bindings.support_reference_sha256,
        target_body_entity_identity_sha256=(bindings.target_body_entity_identity_sha256),
        support_entity_identity_sha256=bindings.support_entity_identity_sha256,
        selection_receipt=dataclasses.replace(
            selection,
            geometric_signature_sha256="f" * 64,
        ),
    )
    with pytest.raises(ReferenceRuleError):
        _validate_bindings(fixture.plan, tampered, fixture.plan_document.content_sha256)

    fixture.support.Shape = _Shape("stale-shape")
    with pytest.raises(ReferenceRuleError):
        _validate_bindings(fixture.plan, bindings, fixture.plan_document.content_sha256)

    ambiguous = _fixture(
        PartDesignReferenceKind.SUBSHAPE_BINDER,
        ambiguous_support=True,
    )
    with pytest.raises((ReviewedIntentExecutionError, ReferenceRuleError)):
        _authenticated_bindings(
            ambiguous.document,
            ambiguous.plan,
            ambiguous.plan_document,
            ambiguous.sources,
            session=ambiguous.session,
            run_token=ambiguous.token,
        )
    assert fixture.document.mutation_calls == 0
    assert ambiguous.document.mutation_calls == 0


def test_private_descriptor_has_five_reference_result_contracts_without_registration() -> None:
    descriptor = build_partdesign_reference_reviewed_family_descriptor()

    assert descriptor.manifest is PARTDESIGN_REFERENCE_COMPAT_MANIFEST
    assert descriptor.requires_same_run_sources is True
    assert tuple(item.operation_id for item in descriptor.product_results) == tuple(
        kind.value for kind in PARTDESIGN_REFERENCE_REVIEWED_OPERATIONS
    )
    assert all(
        item.result_kind is _ReviewedProductResultKind.REFERENCE
        for item in descriptor.product_results
    )
    assert all(
        item.semantic_roles == (SemanticRole.SUPPORT,) for item in descriptor.product_results
    )
