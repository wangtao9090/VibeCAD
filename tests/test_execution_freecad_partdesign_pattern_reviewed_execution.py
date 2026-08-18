from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass

import pytest

import tests.test_intent_bridge_freecad_partdesign_pattern_adapter as adapter_cases
import vibecad.execution.freecad_partdesign_pattern_reviewed_execution as pattern_execution
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_partdesign_pattern_reviewed_execution import (
    PARTDESIGN_PATTERN_MANIFEST,
    PARTDESIGN_PATTERN_REQUIRED_SOURCE_ROLES,
    PARTDESIGN_PATTERN_REVIEWED_FAMILY_SPEC,
    PARTDESIGN_PATTERN_REVIEWED_OPERATIONS,
    PARTDESIGN_PATTERN_REVIEWED_PRODUCT_IDENTITIES,
    PartDesignPatternOwnershipClosure,
    PartDesignPatternSourceRole,
    execute_partdesign_pattern_reviewed_plan_with_sources,
    partdesign_pattern_reviewed_adapter_factory,
    resolve_partdesign_pattern_reviewed_operation,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PARTDESIGN_PATTERN_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    ReviewedNativeExecutionResult,
    _ReviewedFamilyExecutionContext,
    lower_reviewed_intent,
    route_reviewed_intent,
)
from vibecad.execution.selectors import (
    EntityIdentity,
    Provenance,
    ProvenanceSource,
    SemanticRole,
)
from vibecad.intent_bridge.contracts import (
    BackendLoweringRequest,
    BridgeBudget,
    IntentBridgeError,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import ParametricFeatureGraphV2Codec
from vibecad.intent_bridge.ports import TrustedCodecRegistry
from vibecad.intent_bridge.reviewed_family_engine import ExactReviewedFamilyAdapter
from vibecad.parametric.freecad_partdesign_pattern_rules import (
    MAX_PARTDESIGN_PATTERN_OCCURRENCES,
    PartDesignPatternBackendPlan,
    PartDesignPatternConformanceReceipt,
    PartDesignPatternOperation,
    PartDesignPatternRuleError,
    PatternOriginAxis,
    PatternOriginPlane,
)
from vibecad.validation import EntityObservation
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _request(operation: PartDesignPatternOperation):
    original, reader, policy = adapter_cases._request(adapter_cases._graph(operation))  # noqa: SLF001
    intent = next(
        item for item in original.documents if item.artifact_id in original.intent_artifact_ids
    )
    old_capability = next(
        item for item in original.documents if item.artifact_id in original.capability_artifact_ids
    )
    capability, payload = PARTDESIGN_PATTERN_MANIFEST.capability_document(
        artifact_id=old_capability.artifact_id
    )
    reader.payloads[capability.artifact_id] = payload
    request = BackendLoweringRequest(
        adapter=original.adapter,
        terms=original.terms,
        documents=(intent, capability),
        intent_artifact_ids=(intent.artifact_id,),
        capability_artifact_ids=(capability.artifact_id,),
        proof_bundle=original.proof_bundle,
        budget=BridgeBudget(
            max_input_bytes=intent.size_bytes + len(payload),
            max_output_bytes=original.budget.max_output_bytes,
            max_subject_lookups=1,
            max_rule_applications=1,
        ),
    )
    return request, reader, policy


def _lower(operation: PartDesignPatternOperation):
    request, reader, policy = _request(operation)
    sink = adapter_cases._MemoryPlanSink()  # noqa: SLF001
    adapter = partdesign_pattern_reviewed_adapter_factory(sink)
    result, receipt = adapter.lower_with_receipt(
        request,
        artifacts=reader,
        codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
        proof_policy=policy,
    )
    plan, payload = adapter.read_plan(receipt)
    return adapter, result, receipt, plan, payload


def _program(operation: PartDesignPatternOperation) -> ReviewedIntentProgramV1:
    graph = adapter_cases._graph(operation)  # noqa: SLF001
    route = next(
        item
        for item in REVIEWED_PARTDESIGN_PATTERN_ROUTES
        if item.operation.operation_id == operation.value
    )
    return ReviewedIntentProgramV1(
        operation_id=route.operation_id,
        semantic_operation=route.semantic_operation,
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


def test_pattern_family_has_three_exact_routes_and_ordered_source_roles() -> None:
    routes = REVIEWED_PARTDESIGN_PATTERN_ROUTES
    family = routes[0].family

    assert family.manifest is PARTDESIGN_PATTERN_MANIFEST
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 99
    assert CURRENT_REVIEWED_INTENT_ROUTES[55:58] == routes
    assert len(routes) == len(PARTDESIGN_PATTERN_REVIEWED_PRODUCT_IDENTITIES) == 3
    assert PARTDESIGN_PATTERN_REVIEWED_OPERATIONS == tuple(PartDesignPatternOperation)
    assert PARTDESIGN_PATTERN_REVIEWED_FAMILY_SPEC.minimum_sources == 2
    assert PARTDESIGN_PATTERN_REVIEWED_FAMILY_SPEC.maximum_sources == 2
    assert all(
        PARTDESIGN_PATTERN_REQUIRED_SOURCE_ROLES[operation.value]
        == (PartDesignPatternSourceRole.BASE, PartDesignPatternSourceRole.SOURCE_FEATURE)
        for operation in PartDesignPatternOperation
    )
    assert tuple(route.operation.operation_id for route in routes) == (
        "linear_pattern",
        "polar_pattern",
        "mirrored",
    )
    for identity, operation in zip(
        PARTDESIGN_PATTERN_REVIEWED_PRODUCT_IDENTITIES,
        PartDesignPatternOperation,
        strict=True,
    ):
        resolved = resolve_partdesign_pattern_reviewed_operation(*identity)
        assert resolved is not None and resolved.operation_id == operation.value
        assert (
            resolve_partdesign_pattern_reviewed_operation(identity[0] + ".future", identity[1])
            is None
        )
    formal = current_freecad_intent_capability_specs()
    for route in routes:
        matching = tuple(item for item in formal if item.operation_id == route.operation_id)
        assert len(matching) == 1
        assert route.semantic_operation == matching[0].semantic_operation
        assert route.semantic_operation == route.operation.semantic_term.term_id
        assert route.manifest_semantic_operation != route.semantic_operation
        assert (
            resolve_partdesign_pattern_reviewed_operation(
                route.operation_id,
                route.manifest_semantic_operation,
            )
            is None
        )
        result = route.family.product_result(route.operation)
        assert result.result_kind.value == "solid"
        assert result.semantic_roles == (SemanticRole.FEATURE,)
        assert route.family.minimum_sources == route.family.maximum_sources == 2


@pytest.mark.parametrize("operation", tuple(PartDesignPatternOperation))
def test_pattern_public_route_and_lower_use_only_exact_legacy_identity(
    operation: PartDesignPatternOperation,
) -> None:
    program = _program(operation)
    route = route_reviewed_intent(program)
    lowered = lower_reviewed_intent(program)

    assert route in REVIEWED_PARTDESIGN_PATTERN_ROUTES
    assert lowered.route is route
    assert lowered.plan.operation is operation
    assert lowered.receipt.operation is route.operation

    rebound = dataclasses.replace(
        program,
        semantic_operation=route.manifest_semantic_operation,
    )
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        route_reviewed_intent(rebound)
    assert caught.value.code is ReviewedIntentExecutionErrorCode.UNKNOWN_ROUTE


@pytest.mark.parametrize("operation", tuple(PartDesignPatternOperation))
def test_pattern_exact_adapter_lowers_content_bound_origin_locators(
    operation: PartDesignPatternOperation,
) -> None:
    program = _program(operation)
    adapter, result, receipt, plan, payload = _lower(operation)
    reference = program.intent_graph.references[0]
    graph_locator = next(
        item
        for item in program.intent_graph.terms
        if item.term_ref_id == reference.locator_term_ref_id
    )
    manifest_locator = next(
        item
        for item in PARTDESIGN_PATTERN_MANIFEST.request_terms
        if item.term_ref_id == reference.locator_term_ref_id
    )

    assert type(adapter) is ExactReviewedFamilyAdapter
    assert type(plan) is PartDesignPatternBackendPlan
    assert plan.operation is operation
    assert plan.reference_id == "reference_origin"
    assert payload == plan.canonical_bytes
    assert receipt.operation.operation_id == operation.value
    assert result.plan_document.document_digest == plan.plan_sha256
    assert (
        graph_locator.namespace,
        graph_locator.vocabulary_version,
        graph_locator.term_id,
        graph_locator.term_definition_sha256,
    ) == manifest_locator.semantic_identity
    if operation is PartDesignPatternOperation.LINEAR_PATTERN:
        assert plan.axis is PatternOriginAxis.X and plan.span_mm == 30.0
    elif operation is PartDesignPatternOperation.POLAR_PATTERN:
        assert plan.axis is PatternOriginAxis.Z and plan.angle_degrees == 180.0
    else:
        assert plan.plane is PatternOriginPlane.YZ and plan.occurrences is None


def test_pattern_locator_identity_tamper_remains_inert() -> None:
    graph = adapter_cases._graph(  # noqa: SLF001
        PartDesignPatternOperation.LINEAR_PATTERN,
        locator_definition="f" * 64,
    )
    request, reader, policy = adapter_cases._request(graph)  # noqa: SLF001
    with pytest.raises(IntentBridgeError):
        adapter_cases._lower(  # noqa: SLF001
            adapter_cases.FreeCADPartDesignPatternAdapter(
                adapter_cases._MemoryPlanSink()  # noqa: SLF001
            ),
            request,
            reader,
            policy,
        )


def test_pattern_exact_parameter_bounds_are_closed() -> None:
    _, _, _, linear, _ = _lower(PartDesignPatternOperation.LINEAR_PATTERN)
    _, _, _, polar, _ = _lower(PartDesignPatternOperation.POLAR_PATTERN)
    _, _, _, mirrored, _ = _lower(PartDesignPatternOperation.MIRRORED)

    assert linear.occurrences <= MAX_PARTDESIGN_PATTERN_OCCURRENCES
    with pytest.raises(PartDesignPatternRuleError):
        dataclasses.replace(linear, occurrences=MAX_PARTDESIGN_PATTERN_OCCURRENCES + 1)
    with pytest.raises(PartDesignPatternRuleError):
        dataclasses.replace(linear, span_mm=0.0)
    with pytest.raises(PartDesignPatternRuleError):
        dataclasses.replace(polar, angle_degrees=360.0001)
    with pytest.raises(PartDesignPatternRuleError):
        dataclasses.replace(mirrored, reversed=True)


class _Shape:
    def __init__(self, value: str, volume: float) -> None:
        self.value = value
        self.Volume = volume
        self.Solids = (object(),)

    def exportBrepToString(self) -> str:
        return self.value

    def isNull(self) -> bool:
        return False

    def isValid(self) -> bool:
        return True


class _Feature:
    def __init__(self, document: _Document, name: str, type_id: str, volume: float) -> None:
        self.Document = document
        self.Name = name
        self.TypeId = type_id
        self.State = ("Up-to-date",)
        self.Shape = _Shape(name, volume)
        self.BaseFeature = None
        self.Originals: list[object] = []

    def isValid(self) -> bool:
        return True


class _Body:
    TypeId = "PartDesign::Body"

    def __init__(self, document: _Document, name: str) -> None:
        self.Document = document
        self.Name = name
        self.Group: list[object] = []
        self.Tip: object | None = None


class _Document:
    def __init__(self) -> None:
        self.Objects: list[object] = []

    def getObject(self, name: str):
        return next((item for item in self.Objects if getattr(item, "Name", None) == name), None)


class _Session:
    def __init__(self, document: _Document, identities: dict[object, EntityIdentity]) -> None:
        self.doc = document
        self.identities = identities

    def read_object_identity(self, item: object) -> EntityIdentity:
        return self.identities[item]


@dataclass(frozen=True)
class _Fixture:
    document: _Document
    body: _Body
    session: _Session
    plan: PartDesignPatternBackendPlan
    payload: bytes
    plan_document: object
    operation: object
    source_results: tuple[ReviewedNativeExecutionResult, ...]


def _identity(item: _Feature) -> EntityIdentity:
    return EntityIdentity(
        object_id="object_" + hashlib.sha256(item.Name.encode()).hexdigest()[:32],
        feature_id="feature_" + hashlib.sha256((item.Name + "-feature").encode()).hexdigest()[:32],
        object_type=item.TypeId,
        semantic_role=SemanticRole.FEATURE,
        provenance=Provenance(
            source=ProvenanceSource.MODEL,
            operation_id="apply_reviewed_intent",
        ),
    )


def _source_result(route, item: _Feature, *, body_id: str, node_id: str, result_id: str):
    native = PartDesignPatternConformanceReceipt(
        plan_sha256=_sha("source-plan-" + item.Name),
        operation=PartDesignPatternOperation(route.operation.operation_id),
        object_name=item.Name,
        before_volume_mm3=1.0,
        after_volume_mm3=float(item.Shape.Volume),
    )
    content_sha256 = _sha("source-content-" + item.Name)
    closure = PartDesignPatternOwnershipClosure(
        native_receipt=native,
        body_id=body_id,
        node_id=node_id,
        result_id=result_id,
        native_type_id=item.TypeId,
        plan_content_sha256=content_sha256,
        result_shape_sha256=hashlib.sha256(item.Shape.exportBrepToString().encode()).hexdigest(),
    )
    return ReviewedNativeExecutionResult(
        route=route,
        object=item,
        plan_sha256=closure.plan_sha256,
        plan_content_sha256=content_sha256,
        native_receipt=closure,
    )


def _separate_sources(plan: PartDesignPatternBackendPlan) -> PartDesignPatternBackendPlan:
    return dataclasses.replace(
        plan,
        base=dataclasses.replace(plan.base, node_id="node_base", result_id="result_base"),
        source_feature=dataclasses.replace(
            plan.source_feature,
            node_id="node_source",
            result_id="result_source",
        ),
    )


def _fixture(*, separate_sources: bool = True) -> _Fixture:
    routes = REVIEWED_PARTDESIGN_PATTERN_ROUTES
    _, _, _, original_plan, _ = _lower(PartDesignPatternOperation.LINEAR_PATTERN)
    plan = _separate_sources(original_plan) if separate_sources else original_plan
    plan_document = PARTDESIGN_PATTERN_MANIFEST.plan_document(
        plan.canonical_bytes,
        plan.plan_sha256,
    )
    document = _Document()
    body = _Body(document, "Body")
    base = _Feature(document, "Base", routes[0].operation.native_type_id, 10.0)
    source = (
        _Feature(document, "Source", routes[0].operation.native_type_id, 9.0)
        if separate_sources
        else base
    )
    body.Group = [source, base] if separate_sources else [base]
    body.Tip = base
    document.Objects = [body, source, base] if separate_sources else [body, base]
    session = _Session(document, {base: _identity(base), source: _identity(source)})
    base_result = _source_result(
        routes[0],
        base,
        body_id=plan.body_id,
        node_id=plan.base.node_id,
        result_id=plan.base.result_id,
    )
    results = (
        base_result,
        _source_result(
            routes[0],
            source,
            body_id=plan.body_id,
            node_id=plan.source_feature.node_id,
            result_id=plan.source_feature.result_id,
        )
        if separate_sources
        else base_result,
    )
    return _Fixture(
        document=document,
        body=body,
        session=session,
        plan=plan,
        payload=plan.canonical_bytes,
        plan_document=plan_document,
        operation=routes[0].operation,
        source_results=results,
    )


def test_pattern_accepts_one_sealed_feature_as_both_base_and_source() -> None:
    fixture = _fixture(separate_sources=False)

    bindings = pattern_execution._authenticated_bindings(  # noqa: SLF001
        fixture.document,
        fixture.plan,
        fixture.source_results,
        session=fixture.session,
    )

    assert bindings.base.object is bindings.source_feature.object
    assert bindings.body is fixture.body


def test_pattern_executes_and_returns_solid_feature_adoption_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()

    def apply(raw, *, expected_content_sha256, expected_plan_sha256, bindings):
        assert raw == fixture.payload
        assert expected_content_sha256 == fixture.plan_document.content_sha256
        assert expected_plan_sha256 == fixture.plan.plan_sha256
        result = _Feature(
            fixture.document,
            "LinearResult",
            fixture.operation.native_type_id,
            18.0,
        )
        result.BaseFeature = bindings.base.object
        result.Originals = [bindings.source_feature.object]
        fixture.body.Group.append(result)
        fixture.body.Tip = result
        fixture.document.Objects.append(result)
        return PartDesignPatternConformanceReceipt(
            plan_sha256=expected_plan_sha256,
            operation=PartDesignPatternOperation.LINEAR_PATTERN,
            object_name=result.Name,
            before_volume_mm3=10.0,
            after_volume_mm3=18.0,
        )

    monkeypatch.setattr(pattern_execution, "apply_partdesign_pattern_plan", apply)
    route = REVIEWED_PARTDESIGN_PATTERN_ROUTES[0]
    native = route.family.apply_plan(
        fixture.document,
        fixture.plan,
        fixture.payload,
        fixture.plan_document,
        fixture.operation,
        _ReviewedFamilyExecutionContext(
            document=fixture.document,
            source_results=fixture.source_results,
            session=fixture.session,
        ),
    )

    assert native.object is fixture.document.Objects[-1]
    assert fixture.body.Tip is native.object
    assert type(native.receipt) is PartDesignPatternOwnershipClosure
    assert native.receipt.body_id == fixture.plan.body_id
    assert native.receipt.node_id == fixture.plan.node_id
    assert native.receipt.result_id == fixture.plan.result_id
    assert native.receipt.native_type_id == fixture.operation.native_type_id
    assert native.receipt.plan_content_sha256 == fixture.plan_document.content_sha256
    assert native.receipt.native_receipt.operation is PartDesignPatternOperation.LINEAR_PATTERN
    assert native.receipt.native_receipt.object_name == native.object.Name
    assert (
        native.receipt.result_shape_sha256
        == hashlib.sha256(native.object.Shape.exportBrepToString().encode()).hexdigest()
    )
    observation = EntityObservation(
        object_id="object_" + "a" * 32,
        feature_id="feature_" + "b" * 32,
        object_type=fixture.operation.native_type_id,
        semantic_role="feature",
        provenance={"source": "model", "operation_id": "apply_reviewed_intent"},
        placement=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
        volume_mm3=18.0,
        area_mm2=1.0,
        bbox_mm=(1.0, 1.0, 1.0),
        center_of_mass_mm=(0.0, 0.0, 0.0),
        valid_shape=True,
        solid_count=1,
    )
    native.receipt.validate_adoption(fixture.document, native.object, observation)


@pytest.mark.parametrize(
    "failure",
    (
        "n_minus_1",
        "n_plus_1",
        "order",
        "role",
        "stale",
        "cross_body",
        "selection",
        "content",
        "plan_document",
    ),
)
def test_pattern_rejects_unsealed_sources_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    fixture = _fixture()
    sources = fixture.source_results
    plan_document = fixture.plan_document
    if failure == "n_minus_1":
        sources = sources[:-1]
    elif failure == "n_plus_1":
        sources = (*sources, sources[-1])
    elif failure == "order":
        sources = tuple(reversed(sources))
    elif failure == "role":
        item = sources[1].object
        fixture.session.identities[item] = dataclasses.replace(
            fixture.session.identities[item],
            semantic_role=SemanticRole.PRIMITIVE,
        )
    elif failure == "stale":
        sources[1].object.Shape.value += "-stale"
    elif failure == "cross_body":
        other = _Body(fixture.document, "OtherBody")
        fixture.document.Objects.append(other)
        fixture.body.Group.remove(sources[1].object)
        other.Group.append(sources[1].object)
    elif failure == "selection":
        receipt = dataclasses.replace(sources[1].native_receipt, node_id="node_wrong")
        sources = (
            sources[0],
            ReviewedNativeExecutionResult(
                route=sources[1].route,
                object=sources[1].object,
                plan_sha256=sources[1].plan_sha256,
                plan_content_sha256=sources[1].plan_content_sha256,
                native_receipt=receipt,
            ),
        )
    elif failure == "content":
        sources = (
            sources[0],
            ReviewedNativeExecutionResult(
                route=sources[1].route,
                object=sources[1].object,
                plan_sha256=sources[1].plan_sha256,
                plan_content_sha256="f" * 64,
                native_receipt=sources[1].native_receipt,
            ),
        )
    else:
        plan_document = dataclasses.replace(plan_document, content_sha256="f" * 64)
    before = tuple(fixture.document.Objects)
    called = False

    def apply(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid sources must remain pre-mutation")

    monkeypatch.setattr(pattern_execution, "apply_partdesign_pattern_plan", apply)
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_partdesign_pattern_reviewed_plan_with_sources(
            fixture.document,
            fixture.plan,
            fixture.payload,
            plan_document,
            fixture.operation,
            sources,
            session=fixture.session,
        )

    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert called is False
    assert tuple(fixture.document.Objects) == before
