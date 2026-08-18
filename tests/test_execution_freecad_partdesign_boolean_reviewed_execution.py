from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass

import pytest

import tests.test_intent_bridge_freecad_partdesign_boolean_adapter as adapter_cases
import vibecad.execution.freecad_partdesign_boolean_reviewed_execution as boolean_execution
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_partdesign_boolean_reviewed_execution import (
    PARTDESIGN_BOOLEAN_MANIFEST,
    PARTDESIGN_BOOLEAN_REQUIRED_SOURCE_ROLES,
    PARTDESIGN_BOOLEAN_REVIEWED_FAMILY_SPEC,
    PARTDESIGN_BOOLEAN_REVIEWED_OPERATIONS,
    PARTDESIGN_BOOLEAN_REVIEWED_PRODUCT_IDENTITIES,
    PartDesignBooleanOwnershipClosure,
    PartDesignBooleanSourceRole,
    execute_partdesign_boolean_reviewed_plan_with_sources,
    partdesign_boolean_reviewed_adapter_factory,
    resolve_partdesign_boolean_reviewed_operation,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PARTDESIGN_BOOLEAN_ROUTES,
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
from vibecad.intent_bridge.contracts import BackendLoweringRequest, BridgeBudget
from vibecad.intent_bridge.parametric_feature_graph_codec import ParametricFeatureGraphV2Codec
from vibecad.intent_bridge.ports import TrustedCodecRegistry
from vibecad.intent_bridge.reviewed_family_engine import ExactReviewedFamilyAdapter
from vibecad.parametric.freecad_partdesign_boolean_rules import (
    PartDesignBooleanBackendPlan,
    PartDesignBooleanConformanceReceipt,
    PartDesignBooleanOperation,
    PartDesignBooleanRuleError,
)
from vibecad.validation import EntityObservation
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _request(operation: PartDesignBooleanOperation):
    original, reader, policy = adapter_cases._request(  # noqa: SLF001
        adapter_cases._graph(operation)  # noqa: SLF001
    )
    intent = next(
        item for item in original.documents if item.artifact_id in original.intent_artifact_ids
    )
    old_capability = next(
        item for item in original.documents if item.artifact_id in original.capability_artifact_ids
    )
    capability, payload = PARTDESIGN_BOOLEAN_MANIFEST.capability_document(
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


def _lower(operation: PartDesignBooleanOperation):
    request, reader, policy = _request(operation)
    sink = adapter_cases._MemoryPlanSink()  # noqa: SLF001
    adapter = partdesign_boolean_reviewed_adapter_factory(sink)
    result, receipt = adapter.lower_with_receipt(
        request,
        artifacts=reader,
        codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
        proof_policy=policy,
    )
    plan, payload = adapter.read_plan(receipt)
    return adapter, result, receipt, plan, payload


def _program(operation: PartDesignBooleanOperation) -> ReviewedIntentProgramV1:
    graph = adapter_cases._graph(operation)  # noqa: SLF001
    route = next(
        item
        for item in REVIEWED_PARTDESIGN_BOOLEAN_ROUTES
        if item.operation.operation_id == operation.value
    )
    return ReviewedIntentProgramV1(
        operation_id=route.operation_id,
        semantic_operation=route.semantic_operation,
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


def test_boolean_family_has_three_exact_routes_and_ordered_source_roles() -> None:
    routes = REVIEWED_PARTDESIGN_BOOLEAN_ROUTES
    family = routes[0].family

    assert family.manifest is PARTDESIGN_BOOLEAN_MANIFEST
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 78
    assert CURRENT_REVIEWED_INTENT_ROUTES[-20:-17] == routes
    assert len(routes) == len(PARTDESIGN_BOOLEAN_REVIEWED_PRODUCT_IDENTITIES) == 3
    assert PARTDESIGN_BOOLEAN_REVIEWED_OPERATIONS == tuple(PartDesignBooleanOperation)
    assert PARTDESIGN_BOOLEAN_REVIEWED_FAMILY_SPEC.minimum_sources == 2
    assert PARTDESIGN_BOOLEAN_REVIEWED_FAMILY_SPEC.maximum_sources == 2
    assert all(
        PARTDESIGN_BOOLEAN_REQUIRED_SOURCE_ROLES[operation.value]
        == (
            PartDesignBooleanSourceRole.TARGET_BASE,
            PartDesignBooleanSourceRole.EXTERNAL_TOOL,
        )
        for operation in PartDesignBooleanOperation
    )
    assert tuple(route.operation.operation_id for route in routes) == (
        "fuse",
        "cut",
        "common",
    )
    for identity, operation in zip(
        PARTDESIGN_BOOLEAN_REVIEWED_PRODUCT_IDENTITIES,
        PartDesignBooleanOperation,
        strict=True,
    ):
        resolved = resolve_partdesign_boolean_reviewed_operation(*identity)
        assert resolved is not None and resolved.operation_id == operation.value
        assert resolve_partdesign_boolean_reviewed_operation(identity[0], identity[1] + "x") is None
    formal = current_freecad_intent_capability_specs()
    for route in routes:
        matching = tuple(item for item in formal if item.operation_id == route.operation_id)
        assert len(matching) == 1
        assert route.semantic_operation == matching[0].semantic_operation
        assert route.semantic_operation == route.operation.semantic_term.term_id
        assert route.manifest_semantic_operation != route.semantic_operation
        assert (
            resolve_partdesign_boolean_reviewed_operation(
                route.operation_id,
                route.manifest_semantic_operation,
            )
            is None
        )
        result = route.family.product_result(route.operation)
        assert result.result_kind.value == "solid"
        assert result.semantic_roles == (SemanticRole.FEATURE,)
        assert route.family.minimum_sources == route.family.maximum_sources == 2


@pytest.mark.parametrize("operation", tuple(PartDesignBooleanOperation))
def test_boolean_public_route_and_lower_use_only_exact_legacy_identity(
    operation: PartDesignBooleanOperation,
) -> None:
    program = _program(operation)
    route = route_reviewed_intent(program)
    lowered = lower_reviewed_intent(program)

    assert route in REVIEWED_PARTDESIGN_BOOLEAN_ROUTES
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


@pytest.mark.parametrize("operation", tuple(PartDesignBooleanOperation))
def test_boolean_exact_adapter_lowers_two_distinct_body_tip_selections(
    operation: PartDesignBooleanOperation,
) -> None:
    adapter, result, receipt, plan, payload = _lower(operation)

    assert type(adapter) is ExactReviewedFamilyAdapter
    assert type(plan) is PartDesignBooleanBackendPlan
    assert plan.operation is operation
    assert plan.base.to_mapping() == {
        "body_id": "body_main",
        "node_id": "node_base",
        "result_id": "result_base",
    }
    assert tuple(item.to_mapping() for item in plan.tools) == (
        {
            "body_id": "body_tool",
            "node_id": "node_tool",
            "result_id": "result_tool",
        },
    )
    assert payload == plan.canonical_bytes
    assert receipt.operation.operation_id == operation.value
    assert result.plan_document.document_digest == plan.plan_sha256


class _Shape:
    def __init__(self, value: str, volume: float, *, solid_count: int = 1) -> None:
        self.value = value
        self.Volume = volume
        self.Solids = tuple(object() for _ in range(solid_count))

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
        self.Group: list[object] = []

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
    target_body: _Body
    base: _Feature
    tool_body: _Body
    tool: _Feature
    session: _Session
    plan: PartDesignBooleanBackendPlan
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
    result_volume = float(item.Shape.Volume)
    native = PartDesignBooleanConformanceReceipt(
        plan_sha256=_sha("source-plan-" + item.Name),
        operation=PartDesignBooleanOperation.FUSE,
        object_name=item.Name,
        base_volume_mm3=result_volume * 0.6,
        tool_volumes_mm3=(result_volume * 0.5,),
        result_volume_mm3=result_volume,
    )
    content_sha256 = _sha("source-content-" + item.Name)
    closure = PartDesignBooleanOwnershipClosure(
        native_receipt=native,
        body_id=body_id,
        node_id=node_id,
        result_id=result_id,
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


def _fixture(
    operation: PartDesignBooleanOperation = PartDesignBooleanOperation.FUSE,
) -> _Fixture:
    routes = REVIEWED_PARTDESIGN_BOOLEAN_ROUTES
    _, _, _, plan, _ = _lower(operation)
    plan_document = PARTDESIGN_BOOLEAN_MANIFEST.plan_document(
        plan.canonical_bytes,
        plan.plan_sha256,
    )
    document = _Document()
    target_body = _Body(document, "TargetBody")
    base = _Feature(document, "Base", routes[0].operation.native_type_id, 10.0)
    tool_body = _Body(document, "ToolBody")
    tool = _Feature(document, "Tool", routes[0].operation.native_type_id, 7.0)
    target_body.Group = [base]
    target_body.Tip = base
    tool_body.Group = [tool]
    tool_body.Tip = tool
    document.Objects = [target_body, base, tool_body, tool]
    session = _Session(document, {base: _identity(base), tool: _identity(tool)})
    sources = (
        _source_result(
            routes[0],
            base,
            body_id=plan.base.body_id,
            node_id=plan.base.node_id,
            result_id=plan.base.result_id,
        ),
        _source_result(
            routes[0],
            tool,
            body_id=plan.tools[0].body_id,
            node_id=plan.tools[0].node_id,
            result_id=plan.tools[0].result_id,
        ),
    )
    return _Fixture(
        document=document,
        target_body=target_body,
        base=base,
        tool_body=tool_body,
        tool=tool,
        session=session,
        plan=plan,
        payload=plan.canonical_bytes,
        plan_document=plan_document,
        operation=routes[tuple(PartDesignBooleanOperation).index(operation)].operation,
        source_results=sources,
    )


def _result_volume(operation: PartDesignBooleanOperation) -> float:
    return {
        PartDesignBooleanOperation.FUSE: 15.0,
        PartDesignBooleanOperation.CUT: 4.0,
        PartDesignBooleanOperation.COMMON: 3.0,
    }[operation]


def _native_apply(
    fixture: _Fixture,
    operation: PartDesignBooleanOperation,
    *,
    mutate_tool: bool = False,
):
    def apply(raw, *, expected_content_sha256, expected_plan_sha256, bindings):
        assert raw == fixture.payload
        assert expected_content_sha256 == fixture.plan_document.content_sha256
        assert expected_plan_sha256 == fixture.plan.plan_sha256
        result = _Feature(
            fixture.document,
            "BooleanResult",
            fixture.operation.native_type_id,
            _result_volume(operation),
        )
        result.BaseFeature = bindings.base.object
        result.Group = [bindings.tools[0].body]
        fixture.target_body.Group.append(result)
        fixture.target_body.Tip = result
        fixture.document.Objects.append(result)
        if mutate_tool:
            fixture.tool.Shape.value += "-mutated"
        return PartDesignBooleanConformanceReceipt(
            plan_sha256=expected_plan_sha256,
            operation=operation,
            object_name=result.Name,
            base_volume_mm3=10.0,
            tool_volumes_mm3=(7.0,),
            result_volume_mm3=_result_volume(operation),
        )

    return apply


@pytest.mark.parametrize("operation", tuple(PartDesignBooleanOperation))
def test_boolean_executes_effect_and_preserves_external_tool_ownership(
    monkeypatch: pytest.MonkeyPatch,
    operation: PartDesignBooleanOperation,
) -> None:
    fixture = _fixture(operation)
    tool_group = tuple(fixture.tool_body.Group)
    tool_tip = fixture.tool_body.Tip
    tool_shape_sha256 = hashlib.sha256(fixture.tool.Shape.exportBrepToString().encode()).hexdigest()

    monkeypatch.setattr(
        boolean_execution,
        "apply_partdesign_boolean_plan",
        _native_apply(fixture, operation),
    )
    route = REVIEWED_PARTDESIGN_BOOLEAN_ROUTES[tuple(PartDesignBooleanOperation).index(operation)]
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
    assert fixture.target_body.Tip is native.object
    assert native.object.BaseFeature is fixture.base
    assert tuple(native.object.Group) == (fixture.tool_body,)
    assert tuple(fixture.tool_body.Group) == tool_group
    assert fixture.tool_body.Tip is tool_tip is fixture.tool
    assert (
        hashlib.sha256(fixture.tool.Shape.exportBrepToString().encode()).hexdigest()
        == tool_shape_sha256
    )
    assert type(native.receipt) is PartDesignBooleanOwnershipClosure
    assert native.receipt.body_id == fixture.plan.body_id
    assert native.receipt.node_id == fixture.plan.node_id
    assert native.receipt.result_id == fixture.plan.result_id
    assert native.receipt.native_type_id == fixture.operation.native_type_id
    assert native.receipt.plan_content_sha256 == fixture.plan_document.content_sha256
    assert native.receipt.native_receipt.operation is operation
    assert native.receipt.native_receipt.receipt_sha256
    assert native.receipt.receipt_sha256
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
        volume_mm3=_result_volume(operation),
        area_mm2=1.0,
        bbox_mm=(1.0, 1.0, 1.0),
        center_of_mass_mm=(0.0, 0.0, 0.0),
        valid_shape=True,
        solid_count=1,
    )
    native.receipt.validate_adoption(fixture.document, native.object, observation)


def test_boolean_family_rejects_native_tool_shape_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = PartDesignBooleanOperation.FUSE
    fixture = _fixture(operation)
    monkeypatch.setattr(
        boolean_execution,
        "apply_partdesign_boolean_plan",
        _native_apply(fixture, operation, mutate_tool=True),
    )

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        REVIEWED_PARTDESIGN_BOOLEAN_ROUTES[0].family.apply_plan(
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

    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize(
    "failure",
    (
        "n_minus_1",
        "n_plus_1",
        "order",
        "role",
        "provenance",
        "stale",
        "same_body",
        "wrong_tip",
        "wrong_document",
        "selection",
        "content",
        "plan_document",
        "multisolid",
    ),
)
def test_boolean_rejects_unsealed_sources_before_native_mutation(
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
        fixture.session.identities[fixture.tool] = dataclasses.replace(
            fixture.session.identities[fixture.tool],
            semantic_role=SemanticRole.PRIMITIVE,
        )
    elif failure == "provenance":
        fixture.session.identities[fixture.tool] = dataclasses.replace(
            fixture.session.identities[fixture.tool],
            provenance=Provenance(
                source=ProvenanceSource.MODEL,
                operation_id="manual_edit",
            ),
        )
    elif failure == "stale":
        fixture.tool.Shape.value += "-stale"
    elif failure == "same_body":
        fixture.tool_body.Group.remove(fixture.tool)
        fixture.target_body.Group.append(fixture.tool)
    elif failure == "wrong_tip":
        fixture.tool_body.Tip = None
    elif failure == "wrong_document":
        fixture.tool.Document = _Document()
    elif failure == "selection":
        receipt = dataclasses.replace(sources[1].native_receipt, body_id="body_wrong")
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
    elif failure == "plan_document":
        plan_document = dataclasses.replace(plan_document, content_sha256="f" * 64)
    else:
        fixture.tool.Shape.Solids = (object(), object())
    before = tuple(fixture.document.Objects)
    called = False

    def apply(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid sources must remain pre-mutation")

    monkeypatch.setattr(boolean_execution, "apply_partdesign_boolean_plan", apply)
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_partdesign_boolean_reviewed_plan_with_sources(
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


@pytest.mark.parametrize(
    ("operation", "result_volume"),
    (
        (PartDesignBooleanOperation.FUSE, 10.0),
        (PartDesignBooleanOperation.CUT, 10.0),
        (PartDesignBooleanOperation.COMMON, 10.0),
    ),
)
def test_boolean_native_receipt_rejects_noop_effects(
    operation: PartDesignBooleanOperation,
    result_volume: float,
) -> None:
    with pytest.raises(PartDesignBooleanRuleError):
        PartDesignBooleanConformanceReceipt(
            plan_sha256=_sha("plan"),
            operation=operation,
            object_name="NoOp",
            base_volume_mm3=10.0,
            tool_volumes_mm3=(7.0,),
            result_volume_mm3=result_volume,
        )
