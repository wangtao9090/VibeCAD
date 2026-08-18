from __future__ import annotations

import dataclasses
import hashlib
from types import SimpleNamespace

import pytest

import tests.test_intent_bridge_freecad_partdesign_promotion_adapter as adapter_cases
import tests.test_reviewed_intent_program as reviewed_program_cases
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_partdesign_promotion_reviewed_execution import (
    PARTDESIGN_PROMOTION_MANIFEST,
    PARTDESIGN_PROMOTION_RESULT_INVARIANTS,
    PARTDESIGN_PROMOTION_REVIEWED_FAMILY_SPEC,
    PARTDESIGN_PROMOTION_REVIEWED_PRODUCT_IDENTITIES,
    PARTDESIGN_PROMOTION_SOURCE_CONTRACTS,
    PartDesignPromotionOwnershipClosure,
    execute_partdesign_promotion_reviewed_plan_with_sources,
    partdesign_promotion_reviewed_adapter_factory,
    resolve_partdesign_promotion_reviewed_operation,
    validate_partdesign_promotion_reviewed_plan,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PART_BOX_ROUTE,
    REVIEWED_PARTDESIGN_PROMOTION_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    ReviewedIntentRoute,
    ReviewedNativeExecutionResult,
    _ReviewedFamilyNativeExecution,
    _ReviewedProductResultKind,
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
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    ReviewedOperationSpec,
    ReviewedPlanReceipt,
)
from vibecad.parametric.freecad_partdesign_promotion_rules import (
    AuthenticatedPromotionObject,
    PartDesignPromotionBackendPlan,
    PartDesignPromotionConformanceReceipt,
    PartDesignPromotionOperation,
    SemanticObjectSelection,
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


def _selection(kind: str, index: int = 0) -> SemanticObjectSelection:
    return SemanticObjectSelection(
        node_id=f"node_{kind}_{index}",
        result_id=f"result_{kind}_{index}",
    )


def _plan(
    operation: PartDesignPromotionOperation,
    *,
    with_base: bool | None = None,
    profiles: int | None = None,
) -> PartDesignPromotionBackendPlan:
    additive = operation.value.startswith("additive_")
    family = operation.value.rsplit("_", 1)[1]
    include_base = not additive if with_base is None else with_base
    profile_count = profiles if profiles is not None else (2 if family == "loft" else 1)
    return PartDesignPromotionBackendPlan(
        source_artifact_id="artifact_source",
        source_graph_id="graph_source",
        source_graph_sha256="1" * 64,
        source_content_sha256="2" * 64,
        lowering_request_sha256="3" * 64,
        adapter_contract_sha256=(PARTDESIGN_PROMOTION_MANIFEST.adapter.adapter_contract_sha256),
        body_id="body_main",
        node_id="node_target",
        result_id="result_target",
        operation=operation,
        base=_selection("base") if include_base else None,
        profiles=tuple(_selection("profile", index) for index in range(profile_count)),
        spine=_selection("spine") if family == "pipe" else None,
        axis_reference_id="reference_axis" if family == "helix" else None,
        axis_result_id="result_axis" if family == "helix" else None,
        pitch_mm=4.0 if family == "helix" else None,
        height_mm=12.0 if family == "helix" else None,
        angle_degrees=0.0 if family == "helix" else None,
    )


def _plan_document(plan: PartDesignPromotionBackendPlan) -> DocumentRef:
    return PARTDESIGN_PROMOTION_MANIFEST.plan_document(
        plan.canonical_bytes,
        plan.plan_sha256,
    )


def _source_document() -> DocumentRef:
    return DocumentRef(
        artifact_id="artifact_source",
        role_term_ref_id=PARTDESIGN_PROMOTION_MANIFEST.intent_role_term.term_ref_id,
        schema_term_ref_id=PARTDESIGN_PROMOTION_MANIFEST.intent_schema_term.term_ref_id,
        document_id="graph_source",
        document_digest="1" * 64,
        content_sha256="2" * 64,
        size_bytes=1,
        media_type=PARTDESIGN_PROMOTION_MANIFEST.intent_media_type,
    )


def _receipt(plan: PartDesignPromotionBackendPlan) -> ReviewedPlanReceipt:
    operation = next(
        item
        for item in PARTDESIGN_PROMOTION_MANIFEST.operations
        if item.operation_id == plan.operation.value
    )
    return ReviewedPlanReceipt(
        manifest_sha256=PARTDESIGN_PROMOTION_MANIFEST.manifest_sha256,
        request_digest=plan.lowering_request_sha256,
        adapter=PARTDESIGN_PROMOTION_MANIFEST.adapter,
        operation=operation,
        source_document=_source_document(),
        plan_document=_plan_document(plan),
    )


def _program(
    operation: PartDesignPromotionOperation,
    *,
    semantic_operation: str | None = None,
    operation_id: str | None = None,
) -> ReviewedIntentProgramV1:
    graph = adapter_cases._graph(operation)  # noqa: SLF001
    route = next(
        item
        for item in REVIEWED_PARTDESIGN_PROMOTION_ROUTES
        if item.operation.operation_id == operation.value
    )
    return ReviewedIntentProgramV1(
        operation_id=route.operation_id if operation_id is None else operation_id,
        semantic_operation=(
            route.semantic_operation if semantic_operation is None else semantic_operation
        ),
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


def test_static_manifest_exact_identities_and_adapter_factory() -> None:
    assert len(PARTDESIGN_PROMOTION_MANIFEST.operations) == 6
    assert PARTDESIGN_PROMOTION_REVIEWED_FAMILY_SPEC.operation_ids == tuple(
        item.value for item in PartDesignPromotionOperation
    )
    assert len(PARTDESIGN_PROMOTION_REVIEWED_PRODUCT_IDENTITIES) == 6
    assert all(
        identity[0].startswith("partdesign.")
        for identity in (PARTDESIGN_PROMOTION_REVIEWED_PRODUCT_IDENTITIES)
    )
    for identity in PARTDESIGN_PROMOTION_REVIEWED_PRODUCT_IDENTITIES:
        assert resolve_partdesign_promotion_reviewed_operation(*identity) is not None
        assert (
            resolve_partdesign_promotion_reviewed_operation(identity[0], f"{identity[1]}-tampered")
            is None
        )
    assert resolve_partdesign_promotion_reviewed_operation(1, "x") is None
    adapter = partdesign_promotion_reviewed_adapter_factory(_MemorySink())
    assert isinstance(adapter, ExactReviewedFamilyAdapter)
    assert adapter.manifest is PARTDESIGN_PROMOTION_MANIFEST


def test_shared_routes_strictly_dual_bind_legacy_formal_and_full_manifest_identity() -> None:
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 96
    assert CURRENT_REVIEWED_INTENT_ROUTES[33:39] == REVIEWED_PARTDESIGN_PROMOTION_ROUTES
    assert (
        tuple(
            (route.operation_id, route.semantic_operation)
            for route in REVIEWED_PARTDESIGN_PROMOTION_ROUTES
        )
        == PARTDESIGN_PROMOTION_REVIEWED_PRODUCT_IDENTITIES
    )
    formal = current_freecad_intent_capability_specs()
    for route in REVIEWED_PARTDESIGN_PROMOTION_ROUTES:
        matching = tuple(item for item in formal if item.operation_id == route.operation_id)
        assert len(matching) == 1
        assert route.semantic_operation == matching[0].semantic_operation
        assert route.semantic_operation == route.operation.semantic_term.term_id
        assert "@" not in route.semantic_operation
        assert route.manifest_semantic_operation.endswith(
            f"@{route.operation.semantic_term.term_definition_sha256}"
        )
        assert route.manifest_semantic_operation != route.semantic_operation
        assert (
            resolve_partdesign_promotion_reviewed_operation(
                route.operation_id,
                route.manifest_semantic_operation,
            )
            is None
        )
        assert route.family.product_result(route.operation).result_kind.value == "solid"
        assert route.family.product_result(route.operation).semantic_roles == (
            SemanticRole.FEATURE,
        )
        assert route.family.minimum_sources == 1
        assert route.family.maximum_sources == 8


@pytest.mark.parametrize("operation", tuple(PartDesignPromotionOperation))
def test_shared_legacy_route_and_lower_are_reachable(
    operation: PartDesignPromotionOperation,
) -> None:
    program = _program(operation)
    route = route_reviewed_intent(program)
    lowered = lower_reviewed_intent(program)

    assert route in REVIEWED_PARTDESIGN_PROMOTION_ROUTES
    assert lowered.route is route
    assert lowered.plan.operation is operation
    assert lowered.receipt.operation is route.operation
    assert lowered.plan.angle_degrees == (0.0 if "helix" in operation.value else None)


def test_dual_binding_rejects_bare_full_rebound_and_operation_substitution() -> None:
    route = REVIEWED_PARTDESIGN_PROMOTION_ROUTES[0]
    operation = PartDesignPromotionOperation(route.operation.operation_id)
    program = _program(operation)

    full_rebound = dataclasses.replace(
        program,
        semantic_operation=route.manifest_semantic_operation,
    )
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        route_reviewed_intent(full_rebound)
    assert caught.value.code is ReviewedIntentExecutionErrorCode.UNKNOWN_ROUTE

    other = REVIEWED_PARTDESIGN_PROMOTION_ROUTES[1]
    bare_rebound = dataclasses.replace(program, semantic_operation=other.semantic_operation)
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        route_reviewed_intent(bare_rebound)
    assert caught.value.code is ReviewedIntentExecutionErrorCode.UNKNOWN_ROUTE

    substituted = _program(
        operation,
        operation_id=other.operation_id,
        semantic_operation=other.semantic_operation,
    )
    assert route_reviewed_intent(substituted) is other
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        lower_reviewed_intent(substituted)
    assert caught.value.code in {
        ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE,
        ReviewedIntentExecutionErrorCode.LOWERING_FAILED,
    }

    with pytest.raises(ReviewedIntentExecutionError):
        dataclasses.replace(route, semantic_operation=route.manifest_semantic_operation)
    with pytest.raises(ReviewedIntentExecutionError):
        dataclasses.replace(route, operation=other.operation)


def test_modern_route_still_requires_full_identity_and_rejects_bare_term() -> None:
    modern = REVIEWED_PART_BOX_ROUTE
    assert modern.semantic_operation == modern.manifest_semantic_operation
    program = reviewed_program_cases.reviewed_box_program()
    assert route_reviewed_intent(program) is modern

    rebound = dataclasses.replace(
        program,
        semantic_operation=modern.operation.semantic_term.term_id,
    )
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        route_reviewed_intent(rebound)
    assert caught.value.code is ReviewedIntentExecutionErrorCode.UNKNOWN_ROUTE
    with pytest.raises(ReviewedIntentExecutionError):
        dataclasses.replace(
            modern,
            semantic_operation=modern.operation.semantic_term.term_id,
        )


@pytest.mark.parametrize(
    ("operation", "with_base", "profiles", "roles"),
    (
        (PartDesignPromotionOperation.ADDITIVE_LOFT, False, 2, ("profile", "profile")),
        (
            PartDesignPromotionOperation.ADDITIVE_LOFT,
            True,
            7,
            ("base", *("profile" for _ in range(7))),
        ),
        (
            PartDesignPromotionOperation.SUBTRACTIVE_LOFT,
            True,
            2,
            ("base", "profile", "profile"),
        ),
        (
            PartDesignPromotionOperation.ADDITIVE_PIPE,
            False,
            1,
            ("profile", "spine"),
        ),
        (
            PartDesignPromotionOperation.SUBTRACTIVE_PIPE,
            True,
            1,
            ("base", "profile", "spine"),
        ),
        (PartDesignPromotionOperation.ADDITIVE_HELIX, False, 1, ("profile",)),
        (
            PartDesignPromotionOperation.SUBTRACTIVE_HELIX,
            True,
            1,
            ("base", "profile"),
        ),
    ),
)
def test_ordered_source_contract(
    operation: PartDesignPromotionOperation,
    with_base: bool,
    profiles: int,
    roles: tuple[str, ...],
) -> None:
    plan = _plan(operation, with_base=with_base, profiles=profiles)
    assert (
        tuple(role for role, _ in PARTDESIGN_PROMOTION_SOURCE_CONTRACTS[operation].selections(plan))
        == roles
    )


def test_nine_source_loft_and_duplicate_selection_fail_closed() -> None:
    nine = _plan(
        PartDesignPromotionOperation.ADDITIVE_LOFT,
        with_base=True,
        profiles=8,
    )
    with pytest.raises(ReviewedIntentExecutionError) as nine_error:
        PARTDESIGN_PROMOTION_SOURCE_CONTRACTS[nine.operation].selections(nine)
    assert nine_error.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE

    duplicate = _plan(PartDesignPromotionOperation.ADDITIVE_LOFT)
    object.__setattr__(duplicate, "profiles", (duplicate.profiles[0], duplicate.profiles[0]))
    with pytest.raises(ReviewedIntentExecutionError):
        PARTDESIGN_PROMOTION_SOURCE_CONTRACTS[duplicate.operation].selections(duplicate)


def test_plan_binding_and_tamper_are_exact() -> None:
    plan = _plan(PartDesignPromotionOperation.ADDITIVE_HELIX)
    receipt = _receipt(plan)
    validate_partdesign_promotion_reviewed_plan(plan, receipt, receipt.operation)

    wrong_operation = next(
        item
        for item in PARTDESIGN_PROMOTION_MANIFEST.operations
        if item.operation_id == PartDesignPromotionOperation.ADDITIVE_PIPE.value
    )
    with pytest.raises(ReviewedIntentExecutionError) as error:
        validate_partdesign_promotion_reviewed_plan(plan, receipt, wrong_operation)
    assert error.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE

    tampered = dataclasses.replace(plan, angle_degrees=0.0)
    object.__setattr__(tampered, "lowering_request_sha256", "4" * 64)
    with pytest.raises(ReviewedIntentExecutionError):
        validate_partdesign_promotion_reviewed_plan(tampered, receipt, receipt.operation)


def test_compat_lowering_draft_reuses_legacy_plan_and_enforces_source_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.execution.freecad_partdesign_promotion_reviewed_execution as module

    plan = _plan(PartDesignPromotionOperation.ADDITIVE_HELIX)
    subject = SubjectRef(
        artifact_id="artifact_source",
        selector_kind_term_ref_id="selector_feature_node",
        selector_id=plan.node_id,
    )
    source = _source_document()
    monkeypatch.setattr(module, "decode_parametric_feature_graph_v2", lambda *_a, **_k: object())
    monkeypatch.setattr(
        module.promotion_adapter,
        "_build_plan",
        lambda *_a, **_k: (plan, subject),
    )
    draft = module._reviewed_plan_draft(  # noqa: SLF001
        source,
        b"{}",
        plan.lowering_request_sha256,
        PARTDESIGN_PROMOTION_MANIFEST,
    )
    assert draft.payload == plan.canonical_bytes
    assert draft.semantic_plan_sha256 == plan.plan_sha256
    assert draft.operation_term == _receipt(plan).operation.semantic_term
    assert draft.subjects == (subject,)

    nine = _plan(
        PartDesignPromotionOperation.ADDITIVE_LOFT,
        with_base=True,
        profiles=8,
    )
    monkeypatch.setattr(
        module.promotion_adapter,
        "_build_plan",
        lambda *_a, **_k: (nine, subject),
    )
    with pytest.raises(ReviewedIntentExecutionError):
        module._reviewed_plan_draft(  # noqa: SLF001
            source,
            b"{}",
            nine.lowering_request_sha256,
            PARTDESIGN_PROMOTION_MANIFEST,
        )


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


def _identity(
    token: str,
    item: _Object,
    role: SemanticRole,
) -> EntityIdentity:
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
        semantic_term=PARTDESIGN_PROMOTION_MANIFEST.operations[0].semantic_term,
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
    object.__setattr__(result, "route", _forged_route(item.TypeId, f"source_{item.Name}"))
    object.__setattr__(result, "object", item)
    object.__setattr__(result, "plan_sha256", plan_sha256)
    object.__setattr__(result, "native_receipt", receipt)
    object.__setattr__(result, "result_kind", kind)
    object.__setattr__(result, "semantic_roles", (role,))
    return result


def _helix_fixture():
    document = _Document()
    body = _Body(document)
    base = _Object(
        "Base",
        "Part::Box",
        document,
        body,
        _Shape("base-shape", kind="Solid", volume=100.0),
    )
    profile = _Object(
        "Profile",
        "Sketcher::SketchObject",
        document,
        body,
        _Shape("profile-shape", kind="Wire", closed=True),
    )
    document.Objects[:] = [body, base, profile]
    body.Group = (base, profile)
    body.Tip = base
    identities = {
        base: _identity("base", base, SemanticRole.PRIMITIVE),
        profile: _identity("profile", profile, SemanticRole.FEATURE),
    }
    sources = (
        _source_result(
            base,
            role=SemanticRole.PRIMITIVE,
            kind=_ReviewedProductResultKind.SOLID,
        ),
        _source_result(
            profile,
            role=SemanticRole.FEATURE,
            kind=_ReviewedProductResultKind.VALID_SHAPE,
        ),
    )
    return document, body, base, profile, _Session(document, identities), sources


@pytest.mark.parametrize(
    "failure",
    ("wrong_order", "n_minus_1", "n_plus_1", "duplicate", "stale", "cross_body"),
)
def test_wrong_role_order_cardinality_stale_and_cross_body_reject_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import vibecad.execution.freecad_partdesign_promotion_reviewed_execution as module

    document, _body, _base, profile, session, sources = _helix_fixture()
    plan = _plan(PartDesignPromotionOperation.SUBTRACTIVE_HELIX)
    if failure == "wrong_order":
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
    else:
        other_body = _Body(document)
        profile._body = other_body
    calls = 0

    def forbidden_apply(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("native mutation must not start")

    monkeypatch.setattr(module, "apply_partdesign_promotion_plan", forbidden_apply)
    receipt = _receipt(plan)
    with pytest.raises(ReviewedIntentExecutionError) as error:
        execute_partdesign_promotion_reviewed_plan_with_sources(
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


def test_pure_helix_execute_and_family_owned_adoption(monkeypatch: pytest.MonkeyPatch) -> None:
    import vibecad.execution.freecad_partdesign_promotion_reviewed_execution as module

    document, body, base, profile, session, sources = _helix_fixture()
    plan = _plan(PartDesignPromotionOperation.SUBTRACTIVE_HELIX)
    reviewed_receipt = _receipt(plan)

    def fake_apply(
        raw: bytes,
        *,
        expected_content_sha256: str,
        expected_plan_sha256: str,
        bindings,
    ) -> PartDesignPromotionConformanceReceipt:
        assert raw == plan.canonical_bytes
        assert expected_content_sha256 == reviewed_receipt.plan_document.content_sha256
        assert expected_plan_sha256 == plan.plan_sha256
        assert type(bindings.base) is AuthenticatedPromotionObject
        result = _Object(
            "SubtractiveHelix_result",
            "PartDesign::SubtractiveHelix",
            document,
            body,
            _Shape("result-shape", kind="Solid", volume=80.0),
        )
        result.BaseFeature = base
        result.Profile = (profile, ())
        result.Midplane = False
        result.Reversed = False
        result.Refine = True
        result.AllowMultiFace = False
        result.ReferenceAxis = (profile, ("V_Axis",))
        result.Mode = "pitch-height-angle"
        result.Pitch = 4.0
        result.Height = 12.0
        result.Angle = 0.0
        result.Growth = 0.0
        result.Turns = 3.0
        result.LeftHanded = False
        result.Outside = False
        document.Objects.append(result)
        body.Group = (*body.Group, result)
        body.Tip = result
        return PartDesignPromotionConformanceReceipt(
            plan_sha256=plan.plan_sha256,
            operation=plan.operation,
            object_name=result.Name,
            before_volume_mm3=100.0,
            after_volume_mm3=80.0,
        )

    monkeypatch.setattr(module, "apply_partdesign_promotion_plan", fake_apply)
    executed = execute_partdesign_promotion_reviewed_plan_with_sources(
        document,
        plan,
        plan.canonical_bytes,
        reviewed_receipt.plan_document,
        reviewed_receipt.operation,
        sources,
        session=session,
    )
    assert type(executed) is _ReviewedFamilyNativeExecution
    assert type(executed.receipt) is PartDesignPromotionOwnershipClosure
    assert executed.object is body.Tip
    observation = EntityObservation(
        object_id="object_0123456789abcdef0123456789abcdef",
        feature_id="feature_0123456789abcdef0123456789abcdef",
        object_type="PartDesign::SubtractiveHelix",
        semantic_role=SemanticRole.FEATURE.value,
        provenance={"source": "model", "operation_id": "apply_reviewed_intent"},
        placement=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
        volume_mm3=80.0,
        area_mm2=1.0,
        bbox_mm=(1.0, 1.0, 1.0),
        center_of_mass_mm=(0.5, 0.5, 0.5),
        valid_shape=True,
        solid_count=1,
    )
    executed.receipt.validate_adoption(document, executed.object, observation)

    executed.object.Growth = 1.0
    with pytest.raises(ReviewedIntentExecutionError):
        executed.receipt.validate_adoption(document, executed.object, observation)


@pytest.mark.parametrize("operation", tuple(PartDesignPromotionOperation))
def test_result_contract_is_single_solid_feature(
    operation: PartDesignPromotionOperation,
) -> None:
    invariant = PARTDESIGN_PROMOTION_RESULT_INVARIANTS[operation]
    assert invariant.operation is operation
    assert invariant.native_type_id.startswith("PartDesign::")
    assert invariant.semantic_role is SemanticRole.FEATURE
