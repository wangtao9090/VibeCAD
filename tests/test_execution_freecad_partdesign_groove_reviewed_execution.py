from __future__ import annotations

import hashlib
import sys
from types import ModuleType, SimpleNamespace

import pytest

import tests.test_intent_bridge_freecad_parametric_adapter as adapter_cases
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_partdesign_groove_reviewed_execution import (
    PARTDESIGN_GROOVE_MANIFEST,
    PARTDESIGN_GROOVE_OPERATION_SPEC,
    PARTDESIGN_GROOVE_RESULT_INVARIANT,
    PARTDESIGN_GROOVE_REVIEWED_FAMILY_SPEC,
    PARTDESIGN_GROOVE_REVIEWED_PRODUCT_IDENTITIES,
    PARTDESIGN_GROOVE_SOURCE_CONTRACT,
    PartDesignGrooveOwnershipClosure,
    execute_partdesign_groove_reviewed_plan_with_sources,
    partdesign_groove_reviewed_adapter_factory,
    resolve_partdesign_groove_reviewed_operation,
    validate_partdesign_groove_reviewed_plan,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PART_CURVE_ROUTES,
    REVIEWED_PARTDESIGN_GROOVE_ROUTES,
    REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    ReviewedIntentRoute,
    ReviewedNativeExecutionResult,
    _ReviewedFamilyNativeExecution,
    _ReviewedProductResultKind,
    execute_reviewed_intent_native,
    lower_reviewed_intent,
    route_reviewed_intent,
)
from vibecad.execution.selectors import (
    EntityIdentity,
    Provenance,
    ProvenanceSource,
    SemanticRole,
)
from vibecad.intent_bridge.contracts import DocumentRef, SubjectRef
from vibecad.intent_bridge.freecad_parametric_adapter import (
    GROOVE_OPERATION_TERM,
    PlanSink,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    ReviewedOperationSpec,
    ReviewedPlanReceipt,
)
from vibecad.parametric.freecad_partdesign_sketch_rules import (
    GrooveBackendPlan,
    GrooveConformanceReceipt,
    GrooveExecutionBindings,
)
from vibecad.validation import EntityObservation
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1


class _MemorySink(PlanSink):
    def __init__(self) -> None:
        self.items: dict[str, tuple[DocumentRef, bytes]] = {}

    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        self.items[document.artifact_id] = (document, payload)
        return payload

    def read_exact(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        expected, payload = self.items[document.artifact_id]
        assert expected == document
        assert len(payload) <= maximum_bytes
        return payload


def _plan(*, angle: float = 270.0, reversed_: bool = True) -> GrooveBackendPlan:
    return GrooveBackendPlan(
        source_artifact_id="artifact_source",
        source_graph_id="graph_source",
        source_graph_sha256="1" * 64,
        source_content_sha256="2" * 64,
        lowering_request_sha256="3" * 64,
        adapter_contract_sha256=PARTDESIGN_GROOVE_MANIFEST.adapter.adapter_contract_sha256,
        body_id="body_main",
        node_id="node_groove",
        result_id="result_groove",
        base_node_id="node_base",
        base_result_id="result_base",
        profile_node_id="node_profile",
        profile_result_id="result_profile",
        axis_reference_id="reference_profile_v_axis",
        axis_result_id="result_profile_axis",
        angle_degrees=angle,
        reversed=reversed_,
    )


def _plan_document(plan: GrooveBackendPlan) -> DocumentRef:
    return PARTDESIGN_GROOVE_MANIFEST.plan_document(plan.canonical_bytes, plan.plan_sha256)


def _source_document() -> DocumentRef:
    return DocumentRef(
        artifact_id="artifact_source",
        role_term_ref_id=PARTDESIGN_GROOVE_MANIFEST.intent_role_term.term_ref_id,
        schema_term_ref_id=PARTDESIGN_GROOVE_MANIFEST.intent_schema_term.term_ref_id,
        document_id="graph_source",
        document_digest="1" * 64,
        content_sha256="2" * 64,
        size_bytes=1,
        media_type=PARTDESIGN_GROOVE_MANIFEST.intent_media_type,
    )


def _receipt(plan: GrooveBackendPlan) -> ReviewedPlanReceipt:
    return ReviewedPlanReceipt(
        manifest_sha256=PARTDESIGN_GROOVE_MANIFEST.manifest_sha256,
        request_digest=plan.lowering_request_sha256,
        adapter=PARTDESIGN_GROOVE_MANIFEST.adapter,
        operation=PARTDESIGN_GROOVE_OPERATION_SPEC,
        source_document=_source_document(),
        plan_document=_plan_document(plan),
    )


def _program(
    *,
    semantic_operation: str | None = None,
    operation_id: str | None = None,
) -> ReviewedIntentProgramV1:
    graph = adapter_cases._graph()  # noqa: SLF001
    route = REVIEWED_PARTDESIGN_GROOVE_ROUTES[0]
    return ReviewedIntentProgramV1(
        operation_id=route.operation_id if operation_id is None else operation_id,
        semantic_operation=(
            route.semantic_operation if semantic_operation is None else semantic_operation
        ),
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


def test_static_manifest_identity_adapter_and_source_contract() -> None:
    assert PARTDESIGN_GROOVE_MANIFEST.family_id == "partdesign.groove"
    assert PARTDESIGN_GROOVE_MANIFEST.operations == (PARTDESIGN_GROOVE_OPERATION_SPEC,)
    assert PARTDESIGN_GROOVE_REVIEWED_FAMILY_SPEC.operation_ids == ("angle",)
    assert PARTDESIGN_GROOVE_OPERATION_SPEC.native_type_id == "PartDesign::Groove"
    assert PARTDESIGN_GROOVE_SOURCE_CONTRACT.minimum == 2
    assert PARTDESIGN_GROOVE_SOURCE_CONTRACT.maximum == 2
    assert tuple(item[0] for item in PARTDESIGN_GROOVE_SOURCE_CONTRACT.selections(_plan())) == (
        "base",
        "profile",
    )

    identity = PARTDESIGN_GROOVE_REVIEWED_PRODUCT_IDENTITIES[0]
    assert identity[0] == "partdesign.groove.angle"
    assert GROOVE_OPERATION_TERM.term_id in identity[1]
    assert resolve_partdesign_groove_reviewed_operation(*identity) is (
        PARTDESIGN_GROOVE_OPERATION_SPEC
    )
    # The formal catalog still publishes the legacy bare term.  This family
    # module intentionally does not accept it as an alternative manifest key;
    # the shared strict dual-binding seam owns that one-way association.
    assert (
        resolve_partdesign_groove_reviewed_operation(identity[0], GROOVE_OPERATION_TERM.term_id)
        is None
    )
    assert (
        resolve_partdesign_groove_reviewed_operation(identity[0], f"{identity[1]}-tamper") is None
    )
    assert resolve_partdesign_groove_reviewed_operation(1, identity[1]) is None

    formal = tuple(
        item
        for item in current_freecad_intent_capability_specs()
        if item.operation_id == identity[0]
    )
    assert len(formal) == 1
    assert formal[0].semantic_operation == GROOVE_OPERATION_TERM.term_id
    assert formal[0].native_type_id == PARTDESIGN_GROOVE_OPERATION_SPEC.native_type_id
    assert formal[0].adapter_id == PARTDESIGN_GROOVE_MANIFEST.adapter.adapter_id
    assert formal[0].adapter_contract_sha256 == (
        PARTDESIGN_GROOVE_MANIFEST.adapter.adapter_contract_sha256
    )
    assert formal[0].rule_id == PARTDESIGN_GROOVE_MANIFEST.rule_id
    assert formal[0].rule_contract_sha256 == PARTDESIGN_GROOVE_MANIFEST.rule_contract_sha256

    adapter = partdesign_groove_reviewed_adapter_factory(_MemorySink())
    assert isinstance(adapter, ExactReviewedFamilyAdapter)
    assert adapter.manifest is PARTDESIGN_GROOVE_MANIFEST


def test_shared_route_strictly_dual_binds_and_public_lower_is_reachable() -> None:
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 81
    assert CURRENT_REVIEWED_INTENT_ROUTES[-14:-13] == REVIEWED_PARTDESIGN_GROOVE_ROUTES
    route = REVIEWED_PARTDESIGN_GROOVE_ROUTES[0]
    assert route.operation_id == "partdesign.groove.angle"
    assert route.semantic_operation == GROOVE_OPERATION_TERM.term_id
    assert "@" not in route.semantic_operation
    assert (
        route.manifest_semantic_operation == (PARTDESIGN_GROOVE_REVIEWED_PRODUCT_IDENTITIES[0][1])
    )
    assert route.manifest_semantic_operation != route.semantic_operation
    assert route.family.minimum_sources == route.family.maximum_sources == 2
    product = route.family.product_result(route.operation)
    assert product.result_kind is _ReviewedProductResultKind.SOLID
    assert product.semantic_roles == (SemanticRole.FEATURE,)
    assert product.owned_type_ids == ("PartDesign::Groove",)

    program = _program()
    assert route_reviewed_intent(program) is route
    lowered = lower_reviewed_intent(program)
    assert lowered.route is route
    assert type(lowered.plan) is GrooveBackendPlan
    assert lowered.plan.angle_degrees == 360.0
    assert lowered.receipt.operation is route.operation

    for rebound in (
        route.manifest_semantic_operation,
        f"{route.semantic_operation}.tampered",
    ):
        with pytest.raises(ReviewedIntentExecutionError) as error:
            route_reviewed_intent(_program(semantic_operation=rebound))
        assert error.value.code is ReviewedIntentExecutionErrorCode.UNKNOWN_ROUTE


def test_plan_binding_angle_edges_and_tamper_are_exact() -> None:
    for angle in (1e-9, 360.0):
        plan = _plan(angle=angle)
        receipt = _receipt(plan)
        validate_partdesign_groove_reviewed_plan(plan, receipt, receipt.operation)

    plan = _plan()
    receipt = _receipt(plan)
    tampered = _plan(reversed_=False)
    object.__setattr__(tampered, "lowering_request_sha256", "4" * 64)
    with pytest.raises(ReviewedIntentExecutionError) as error:
        validate_partdesign_groove_reviewed_plan(tampered, receipt, receipt.operation)
    assert error.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE

    wrong_operation = ReviewedOperationSpec(
        operation_id="other",
        semantic_term=PARTDESIGN_GROOVE_OPERATION_SPEC.semantic_term,
        native_type_id="PartDesign::Groove",
        native_operation="groove_angle",
    )
    with pytest.raises(ReviewedIntentExecutionError):
        validate_partdesign_groove_reviewed_plan(plan, receipt, wrong_operation)


def test_compat_lowering_draft_reuses_exact_legacy_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.execution.freecad_partdesign_groove_reviewed_execution as module

    plan = _plan()
    subject = SubjectRef(
        artifact_id="artifact_source",
        selector_kind_term_ref_id="selector_feature_node",
        selector_id=plan.node_id,
    )
    monkeypatch.setattr(module, "decode_parametric_feature_graph_v2", lambda *_a, **_k: object())
    monkeypatch.setattr(module.groove_adapter, "_build_plan", lambda *_a, **_k: (plan, subject))
    draft = module._reviewed_plan_draft(  # noqa: SLF001
        _source_document(),
        b"{}",
        plan.lowering_request_sha256,
        PARTDESIGN_GROOVE_MANIFEST,
    )
    assert draft.payload == plan.canonical_bytes
    assert draft.semantic_plan_sha256 == plan.plan_sha256
    assert draft.operation_term == PARTDESIGN_GROOVE_OPERATION_SPEC.semantic_term
    assert draft.subjects == (subject,)


class _Wire:
    def __init__(self, closed: bool) -> None:
        self._closed = closed

    def isClosed(self) -> bool:
        return self._closed


class _Shape:
    def __init__(self, token: str, *, kind: str, volume: float = 0.0, closed: bool = True):
        self.token = token
        self.ShapeType = kind
        self.Volume = volume
        self.Solids = (object(),) if kind == "Solid" else ()
        self.Edges = (object(),)
        self.Wires = (_Wire(closed),) if kind != "Solid" else ()

    def isNull(self) -> bool:
        return False

    def isValid(self) -> bool:
        return True

    def exportBrepToString(self) -> str:
        return self.token


class _Object:
    def __init__(
        self,
        name: str,
        type_id: str,
        document: _Document,
        body: _Body,
        shape: _Shape,
        *,
        open_vertices: tuple[object, ...] = (),
    ) -> None:
        self.Name = name
        self.TypeId = type_id
        self.Document = document
        self.Shape = shape
        self.State = ("Up-to-date",)
        self.OpenVertices = open_vertices
        self._body = body

    def isValid(self) -> bool:
        return True

    def getParentGeoFeatureGroup(self) -> _Body:
        return self._body


class _Body:
    def __init__(self, document: _Document) -> None:
        self.Document = document
        self.TypeId = "PartDesign::Body"
        self.Group: tuple[object, ...] = ()
        self.Tip: object | None = None


class _Document:
    def __init__(self) -> None:
        self.Objects: list[object] = []

    def getObject(self, name: str) -> object | None:
        return next((item for item in self.Objects if getattr(item, "Name", None) == name), None)


class _Session:
    def __init__(self, document: _Document, identities: dict[object, EntityIdentity]) -> None:
        self.doc = document
        self._identities = identities

    def read_object_identity(self, item: object) -> EntityIdentity:
        return self._identities[item]


def _identity(token: str, item: _Object, role: SemanticRole) -> EntityIdentity:
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()[:32]
    return EntityIdentity(
        object_id=f"object_{digest}",
        feature_id=f"feature_{digest}",
        object_type=item.TypeId,
        semantic_role=role,
        provenance=Provenance(
            source=ProvenanceSource.MODEL,
            operation_id="apply_reviewed_intent",
        ),
    )


def _forged_route(type_id: str, operation_id: str) -> ReviewedIntentRoute:
    operation = ReviewedOperationSpec(
        operation_id=operation_id,
        semantic_term=PARTDESIGN_GROOVE_OPERATION_SPEC.semantic_term,
        native_type_id=type_id,
        native_operation=operation_id,
    )
    route = object.__new__(ReviewedIntentRoute)
    object.__setattr__(route, "operation", operation)
    object.__setattr__(route, "manifest", SimpleNamespace(operations=(operation,)))
    return route


def _source_result(
    item: _Object,
    *,
    role: SemanticRole,
    kind: _ReviewedProductResultKind,
    digest: str | None = None,
    route: ReviewedIntentRoute | None = None,
) -> ReviewedNativeExecutionResult:
    plan_sha256 = "5" * 64
    receipt = SimpleNamespace(
        plan_sha256=plan_sha256,
        object_name=item.Name,
        result_shape_sha256=(
            hashlib.sha256(item.Shape.exportBrepToString().encode()).hexdigest()
            if digest is None
            else digest
        ),
    )
    result = object.__new__(ReviewedNativeExecutionResult)
    object.__setattr__(
        result,
        "route",
        _forged_route(item.TypeId, f"source_{item.Name}") if route is None else route,
    )
    object.__setattr__(result, "object", item)
    object.__setattr__(result, "plan_sha256", plan_sha256)
    object.__setattr__(result, "native_receipt", receipt)
    object.__setattr__(result, "result_kind", kind)
    object.__setattr__(result, "semantic_roles", (role,))
    return result


def _fixture(*, closed: bool = True):
    document = _Document()
    body = _Body(document)
    base = _Object(
        "Base",
        "PartDesign::AdditiveBox",
        document,
        body,
        _Shape("base-shape", kind="Solid", volume=100.0),
    )
    profile = _Object(
        "Profile",
        "Sketcher::SketchObject",
        document,
        body,
        _Shape("profile-shape", kind="Wire", closed=closed),
        open_vertices=() if closed else (object(), object()),
    )
    document.Objects[:] = [body, base, profile]
    body.Group = (base, profile)
    body.Tip = base
    identities = {
        base: _identity("base", base, SemanticRole.FEATURE),
        profile: _identity("profile", profile, SemanticRole.FEATURE),
    }
    sources = (
        _source_result(base, role=SemanticRole.FEATURE, kind=_ReviewedProductResultKind.SOLID),
        _source_result(
            profile,
            role=SemanticRole.FEATURE,
            kind=_ReviewedProductResultKind.VALID_SHAPE,
        ),
    )
    return document, body, base, profile, _Session(document, identities), sources


@pytest.mark.parametrize(
    "failure",
    (
        "wrong_role",
        "wrong_order",
        "n_minus_1",
        "n_plus_1",
        "duplicate",
        "open",
        "stale",
        "cross_body",
        "stale_tip",
    ),
)
def test_source_rejections_happen_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import vibecad.execution.freecad_partdesign_groove_reviewed_execution as module

    document, body, _base, profile, session, sources = _fixture(closed=failure != "open")
    if failure == "wrong_role":
        session._identities[profile] = _identity("profile-support", profile, SemanticRole.SUPPORT)
    elif failure == "wrong_order":
        sources = tuple(reversed(sources))
    elif failure == "n_minus_1":
        sources = sources[:-1]
    elif failure == "n_plus_1":
        sources = (*sources, sources[-1])
    elif failure == "duplicate":
        sources = (sources[0], sources[0])
    elif failure == "stale":
        sources = (
            sources[0],
            _source_result(
                profile,
                role=SemanticRole.FEATURE,
                kind=_ReviewedProductResultKind.VALID_SHAPE,
                digest="0" * 64,
            ),
        )
    elif failure == "cross_body":
        profile._body = _Body(document)
    elif failure == "stale_tip":
        body.Tip = profile
    import vibecad.execution.freecad_reviewed_intent_execution as shared

    monkeypatch.setattr(
        shared,
        "CURRENT_REVIEWED_INTENT_ROUTES",
        tuple(item.route for item in sources),
    )
    calls = 0

    def forbidden_apply(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("native mutation must not start")

    monkeypatch.setattr(module, "apply_groove_plan", forbidden_apply)
    plan = _plan()
    receipt = _receipt(plan)
    with pytest.raises(ReviewedIntentExecutionError) as error:
        execute_partdesign_groove_reviewed_plan_with_sources(
            document,
            plan,
            plan.canonical_bytes,
            receipt.plan_document,
            receipt.operation,
            sources,
            session=session,
        )
    assert error.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert calls == 0


def test_payload_tamper_rejects_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.execution.freecad_partdesign_groove_reviewed_execution as module

    document, _body, _base, _profile, session, sources = _fixture()
    calls = 0

    def forbidden_apply(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("native mutation must not start")

    monkeypatch.setattr(module, "apply_groove_plan", forbidden_apply)
    plan = _plan()
    receipt = _receipt(plan)
    with pytest.raises(ReviewedIntentExecutionError) as error:
        execute_partdesign_groove_reviewed_plan_with_sources(
            document,
            plan,
            plan.canonical_bytes + b" ",
            receipt.plan_document,
            receipt.operation,
            sources,
            session=session,
        )
    assert error.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert calls == 0


def test_public_execute_rejects_non_sketch_profile_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.execution.freecad_partdesign_groove_reviewed_execution as module
    import vibecad.execution.freecad_reviewed_intent_execution as shared

    document, _body, base, profile, session, _sources = _fixture()
    assert not any(
        route.operation.native_type_id == "Sketcher::SketchObject"
        for route in CURRENT_REVIEWED_INTENT_ROUTES
    )
    base_route = next(
        route
        for route in REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES
        if route.operation.native_type_id == base.TypeId
    )
    profile_route = next(
        route for route in REVIEWED_PART_CURVE_ROUTES if route.operation_id.endswith(".polygon")
    )
    profile.TypeId = profile_route.operation.native_type_id
    session._identities[profile] = _identity("polygon-profile", profile, SemanticRole.PRIMITIVE)
    sources = (
        _source_result(
            base,
            role=SemanticRole.FEATURE,
            kind=_ReviewedProductResultKind.SOLID,
            route=base_route,
        ),
        _source_result(
            profile,
            role=SemanticRole.PRIMITIVE,
            kind=_ReviewedProductResultKind.VALID_SHAPE,
            route=profile_route,
        ),
    )
    calls = 0

    def forbidden_apply(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("native mutation must not start")

    monkeypatch.setattr(module, "apply_groove_plan", forbidden_apply)
    monkeypatch.setattr(shared, "require_reviewed_route_verified", lambda *_a, **_k: None)
    monkeypatch.setitem(sys.modules, "FreeCAD", ModuleType("FreeCAD"))
    with pytest.raises(ReviewedIntentExecutionError) as cardinality_error:
        execute_reviewed_intent_native(session, _program(), source_results=sources[:1])
    assert cardinality_error.value.code is ReviewedIntentExecutionErrorCode.INVALID_INPUT
    with pytest.raises(ReviewedIntentExecutionError) as error:
        execute_reviewed_intent_native(session, _program(), source_results=sources)
    assert error.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert calls == 0


def test_internal_authenticated_execute_and_family_owned_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.execution.freecad_partdesign_groove_reviewed_execution as module

    document, body, base, profile, session, sources = _fixture()
    import vibecad.execution.freecad_reviewed_intent_execution as shared

    monkeypatch.setattr(
        shared,
        "CURRENT_REVIEWED_INTENT_ROUTES",
        tuple(item.route for item in sources),
    )
    plan = _plan(angle=360.0, reversed_=True)
    reviewed_receipt = _receipt(plan)

    def fake_apply(
        raw: bytes,
        *,
        expected_content_sha256: str,
        expected_plan_sha256: str,
        bindings: GrooveExecutionBindings,
    ) -> GrooveConformanceReceipt:
        assert raw == plan.canonical_bytes
        assert expected_content_sha256 == reviewed_receipt.plan_document.content_sha256
        assert expected_plan_sha256 == plan.plan_sha256
        assert bindings.base_feature is base
        assert bindings.profile is profile
        result = _Object(
            "Groove_result",
            "PartDesign::Groove",
            document,
            body,
            _Shape("groove-result-shape", kind="Solid", volume=75.0),
        )
        result.BaseFeature = base
        result.Profile = (profile, ())
        result.ReferenceAxis = (profile, ("V_Axis",))
        result.Type = "Angle"
        result.Angle = 360.0
        result.Angle2 = 0.0
        result.Midplane = False
        result.Reversed = True
        result.Refine = True
        result.AllowMultiFace = False
        document.Objects.append(result)
        body.Group = (*body.Group, result)
        body.Tip = result
        return GrooveConformanceReceipt(
            plan_sha256=plan.plan_sha256,
            object_name=result.Name,
            before_volume_mm3=100.0,
            after_volume_mm3=75.0,
            reversed=True,
        )

    monkeypatch.setattr(module, "apply_groove_plan", fake_apply)
    executed = execute_partdesign_groove_reviewed_plan_with_sources(
        document,
        plan,
        plan.canonical_bytes,
        reviewed_receipt.plan_document,
        reviewed_receipt.operation,
        sources,
        session=session,
    )
    assert type(executed) is _ReviewedFamilyNativeExecution
    assert type(executed.receipt) is PartDesignGrooveOwnershipClosure
    assert executed.object is body.Tip
    observation = EntityObservation(
        object_id="object_0123456789abcdef0123456789abcdef",
        feature_id="feature_0123456789abcdef0123456789abcdef",
        object_type="PartDesign::Groove",
        semantic_role=SemanticRole.FEATURE.value,
        provenance={"source": "model", "operation_id": "apply_reviewed_intent"},
        placement=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
        volume_mm3=75.0,
        area_mm2=1.0,
        bbox_mm=(1.0, 1.0, 1.0),
        center_of_mass_mm=(0.5, 0.5, 0.5),
        valid_shape=True,
        solid_count=1,
    )
    executed.receipt.validate_adoption(document, executed.object, observation)

    executed.object.ReferenceAxis = (profile, ("H_Axis",))
    with pytest.raises(ReviewedIntentExecutionError):
        executed.receipt.validate_adoption(document, executed.object, observation)


def test_result_contract_is_single_solid_feature() -> None:
    assert PARTDESIGN_GROOVE_RESULT_INVARIANT.native_type_id == "PartDesign::Groove"
    assert PARTDESIGN_GROOVE_RESULT_INVARIANT.semantic_role is SemanticRole.FEATURE
