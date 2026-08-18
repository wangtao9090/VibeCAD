from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass

import pytest

import tests.test_intent_bridge_freecad_partdesign_dressup_transform_adapter as adapter_cases
import vibecad.parametric.freecad_partdesign_dressup_transform_rules as dressup_rules
from vibecad.execution import (
    freecad_partdesign_dressup_transform_reviewed_execution as dressup_execution,
)
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_partdesign_dressup_transform_reviewed_execution import (
    PARTDESIGN_DRESSUP_REQUIRED_SOURCE_ROLES,
    PARTDESIGN_DRESSUP_REVIEWED_FAMILY_SPEC,
    PARTDESIGN_DRESSUP_REVIEWED_OPERATIONS,
    PARTDESIGN_DRESSUP_REVIEWED_PRODUCT_IDENTITIES,
    PARTDESIGN_DRESSUP_TRANSFORM_CATALOG_OPERATIONS,
    PARTDESIGN_DRESSUP_TRANSFORM_MANIFEST,
    PartDesignDressupOwnershipClosure,
    execute_partdesign_dressup_reviewed_plan_with_sources,
    partdesign_dressup_reviewed_adapter_factory,
    resolve_partdesign_dressup_reviewed_operation,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PARTDESIGN_DRESSUP_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    ReviewedNativeExecutionResult,
    _ReviewedFamilyExecutionContext,
    _ReviewedFamilyNativeExecution,
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
from vibecad.parametric.freecad_partdesign_dressup_transform_rules import (
    Axis,
    AxisAlignedEdgeRole,
    AxisAlignedFaceRole,
    MultiTransformParameters,
    PartDesignDressupTransformBackendPlan,
    PartDesignDressupTransformConformanceReceipt,
    PartDesignDressupTransformOperation,
    PartDesignDressupTransformRuleError,
    Side,
)
from vibecad.validation import EntityObservation
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _request(operation: PartDesignDressupTransformOperation):
    original, reader, policy = adapter_cases._request(  # noqa: SLF001
        adapter_cases._graph(operation)  # noqa: SLF001
    )
    intent = next(
        item for item in original.documents if item.artifact_id in original.intent_artifact_ids
    )
    old_capability = next(
        item for item in original.documents if item.artifact_id in original.capability_artifact_ids
    )
    capability, payload = PARTDESIGN_DRESSUP_TRANSFORM_MANIFEST.capability_document(
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


def _lower(operation: PartDesignDressupTransformOperation):
    request, reader, policy = _request(operation)
    sink = adapter_cases._MemoryPlanSink()  # noqa: SLF001
    adapter = partdesign_dressup_reviewed_adapter_factory(sink)
    result, receipt = adapter.lower_with_receipt(
        request,
        artifacts=reader,
        codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
        proof_policy=policy,
    )
    plan, payload = adapter.read_plan(receipt)
    return adapter, result, receipt, plan, payload


def _program(operation: PartDesignDressupTransformOperation) -> ReviewedIntentProgramV1:
    graph = adapter_cases._graph(operation)  # noqa: SLF001
    operation_id = f"partdesign.{operation.value}"
    formal = next(
        item
        for item in current_freecad_intent_capability_specs()
        if item.operation_id == operation_id
    )
    return ReviewedIntentProgramV1(
        operation_id=operation_id,
        semantic_operation=formal.semantic_operation,
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


def test_dressup_family_routes_six_operations_with_one_dynamic_closure() -> None:
    routes = REVIEWED_PARTDESIGN_DRESSUP_ROUTES
    family = routes[0].family

    assert family.manifest is PARTDESIGN_DRESSUP_TRANSFORM_MANIFEST
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 68
    assert CURRENT_REVIEWED_INTENT_ROUTES[-7:-1] == routes
    assert PARTDESIGN_DRESSUP_TRANSFORM_CATALOG_OPERATIONS == tuple(
        PartDesignDressupTransformOperation
    )
    assert len(PARTDESIGN_DRESSUP_TRANSFORM_MANIFEST.operations) == 6
    assert len(routes) == len(PARTDESIGN_DRESSUP_REVIEWED_PRODUCT_IDENTITIES) == 6
    assert PartDesignDressupTransformOperation.MULTI_TRANSFORM in (
        PARTDESIGN_DRESSUP_REVIEWED_OPERATIONS
    )
    assert PartDesignDressupTransformOperation.MULTI_TRANSFORM.value in (
        PARTDESIGN_DRESSUP_REVIEWED_FAMILY_SPEC.operation_ids
    )
    assert all(
        PARTDESIGN_DRESSUP_REQUIRED_SOURCE_ROLES[operation.value] == ("base",)
        for operation in PARTDESIGN_DRESSUP_REVIEWED_OPERATIONS
    )
    assert tuple(route.operation.operation_id for route in routes) == (
        "scaled",
        "multi_transform",
        "fillet",
        "chamfer",
        "draft",
        "thickness",
    )
    for identity, operation in zip(
        PARTDESIGN_DRESSUP_REVIEWED_PRODUCT_IDENTITIES,
        PARTDESIGN_DRESSUP_REVIEWED_OPERATIONS,
        strict=True,
    ):
        resolved = resolve_partdesign_dressup_reviewed_operation(*identity)
        assert resolved is not None and resolved.operation_id == operation.value
    formal = current_freecad_intent_capability_specs()
    for route in routes:
        matching = tuple(item for item in formal if item.operation_id == route.operation_id)
        assert len(matching) == 1
        assert route.semantic_operation == matching[0].semantic_operation
        assert route.semantic_operation == route.operation.semantic_term.term_id
        assert route.manifest_semantic_operation != route.semantic_operation
        assert (
            resolve_partdesign_dressup_reviewed_operation(
                route.operation_id,
                route.manifest_semantic_operation,
            )
            is None
        )
        resolver = route.family.dynamic_resolver_for(route.operation)
        if route.operation.operation_id == "multi_transform":
            assert resolver is not None
            assert resolver.operation_ids == ("multi_transform",)
            static_contract_fields = (
                route.operation_id,
                route.semantic_operation,
                route.manifest_semantic_operation,
                route.family.formal_semantic_binding.value,
                route.family.product_execution_mode(route.operation).value,
                route.manifest.manifest_sha256,
                route.manifest.adapter.adapter_id,
                route.manifest.adapter.adapter_version,
                route.manifest.adapter.adapter_contract_sha256,
                route.manifest.rule_id,
                route.manifest.rule_contract_sha256,
                route.operation.specification_sha256,
                *route.subject_type_term.semantic_identity,
            )
            dynamic_contract_fields = (
                *static_contract_fields,
                "dynamic-ownership-resolver-v1",
                resolver.resolver_id,
                resolver.resolver_version,
                resolver.resolver_contract_sha256,
            )
            assert (
                route.route_contract_sha256
                == hashlib.sha256(
                    b"vibecad-reviewed-product-route-v1\0"
                    + "\0".join(dynamic_contract_fields).encode("utf-8")
                ).hexdigest()
            )
            assert (
                route.route_contract_sha256
                != hashlib.sha256(
                    b"vibecad-reviewed-product-route-v1\0"
                    + "\0".join(static_contract_fields).encode("utf-8")
                ).hexdigest()
            )
            with pytest.raises(ReviewedIntentExecutionError):
                route.family.product_result(route.operation)
        else:
            assert resolver is None
            result = route.family.product_result(route.operation)
            assert result.result_kind.value == "solid"
            assert result.semantic_roles == (SemanticRole.FEATURE,)
        assert route.family.minimum_sources == route.family.maximum_sources == 1
    multi_program = _program(PartDesignDressupTransformOperation.MULTI_TRANSFORM)
    assert (
        resolve_partdesign_dressup_reviewed_operation(
            multi_program.operation_id,
            multi_program.semantic_operation,
        )
        is not None
    )
    assert any(route.operation_id == multi_program.operation_id for route in routes)


@pytest.mark.parametrize("operation", PARTDESIGN_DRESSUP_REVIEWED_OPERATIONS)
def test_dressup_public_route_and_lower_use_only_exact_legacy_identity(
    operation: PartDesignDressupTransformOperation,
) -> None:
    program = _program(operation)
    route = route_reviewed_intent(program)
    lowered = lower_reviewed_intent(program)

    assert route in REVIEWED_PARTDESIGN_DRESSUP_ROUTES
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


@pytest.mark.parametrize("operation", PARTDESIGN_DRESSUP_REVIEWED_OPERATIONS)
def test_dressup_exact_adapter_lowers_full_catalog_without_native_subelement_names(
    operation: PartDesignDressupTransformOperation,
) -> None:
    adapter, result, receipt, plan, payload = _lower(operation)

    assert type(adapter) is ExactReviewedFamilyAdapter
    assert type(plan) is PartDesignDressupTransformBackendPlan
    assert plan.operation is operation
    assert plan.base.to_mapping() == {
        "node_id": "node_base",
        "result_id": "result_base",
    }
    assert payload == plan.canonical_bytes
    assert receipt.operation.operation_id == operation.value
    assert result.plan_document.document_digest == plan.plan_sha256
    assert b"Edge" not in payload and b"Face" not in payload
    if operation is PartDesignDressupTransformOperation.MULTI_TRANSFORM:
        assert isinstance(plan.parameters, MultiTransformParameters)
        assert tuple(step.step_id for step in plan.parameters.steps) == (
            "scale_primary",
            "mirror_yz",
        )


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
    base: _Feature
    session: _Session
    plan: PartDesignDressupTransformBackendPlan
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


def _source_result(route, item: _Feature, plan: PartDesignDressupTransformBackendPlan):
    native = PartDesignDressupTransformConformanceReceipt(
        plan_sha256=_sha("source-plan-" + item.Name),
        operation=PartDesignDressupTransformOperation.SCALED,
        object_names=(item.Name,),
        before_volume_mm3=5.0,
        after_volume_mm3=float(item.Shape.Volume),
    )
    content_sha256 = _sha("source-content-" + item.Name)
    closure = PartDesignDressupOwnershipClosure(
        native_receipt=native,
        body_id=plan.body_id,
        node_id=plan.base.node_id,
        result_id=plan.base.result_id,
        plan_content_sha256=content_sha256,
        result_shape_sha256=hashlib.sha256(item.Shape.exportBrepToString().encode()).hexdigest(),
        native_type_id=item.TypeId,
    )
    return ReviewedNativeExecutionResult(
        route=route,
        object=item,
        plan_sha256=closure.plan_sha256,
        plan_content_sha256=content_sha256,
        native_receipt=closure,
    )


def _fixture(
    operation: PartDesignDressupTransformOperation = PartDesignDressupTransformOperation.SCALED,
) -> _Fixture:
    routes = REVIEWED_PARTDESIGN_DRESSUP_ROUTES
    _, _, _, plan, _ = _lower(operation)
    plan_document = PARTDESIGN_DRESSUP_TRANSFORM_MANIFEST.plan_document(
        plan.canonical_bytes,
        plan.plan_sha256,
    )
    document = _Document()
    body = _Body(document, "Body")
    base = _Feature(document, "Base", routes[0].operation.native_type_id, 10.0)
    body.Group = [base]
    body.Tip = base
    document.Objects = [body, base]
    session = _Session(document, {base: _identity(base)})
    source = _source_result(routes[0], base, plan)
    route = next(item for item in routes if item.operation.operation_id == operation.value)
    return _Fixture(
        document=document,
        body=body,
        base=base,
        session=session,
        plan=plan,
        payload=plan.canonical_bytes,
        plan_document=plan_document,
        operation=route.operation,
        source_results=(source,),
    )


def _result_volume(operation: PartDesignDressupTransformOperation) -> float:
    if operation in {
        PartDesignDressupTransformOperation.SCALED,
        PartDesignDressupTransformOperation.MULTI_TRANSFORM,
    }:
        return 15.0
    if operation is PartDesignDressupTransformOperation.DRAFT:
        return 11.0
    return 9.0


@pytest.mark.parametrize(
    "operation",
    tuple(
        operation
        for operation in PARTDESIGN_DRESSUP_REVIEWED_OPERATIONS
        if operation is not PartDesignDressupTransformOperation.MULTI_TRANSFORM
    ),
)
def test_dressup_executes_single_solid_feature_and_validates_adoption(
    monkeypatch: pytest.MonkeyPatch,
    operation: PartDesignDressupTransformOperation,
) -> None:
    fixture = _fixture(operation)

    def apply(raw, *, expected_content_sha256, expected_plan_sha256, bindings):
        assert raw == fixture.payload
        assert expected_content_sha256 == fixture.plan_document.content_sha256
        assert expected_plan_sha256 == fixture.plan.plan_sha256
        result = _Feature(
            fixture.document,
            "DressupResult",
            fixture.operation.native_type_id,
            _result_volume(operation),
        )
        result.BaseFeature = bindings.base.object
        fixture.body.Group.append(result)
        fixture.body.Tip = result
        fixture.document.Objects.append(result)
        return PartDesignDressupTransformConformanceReceipt(
            plan_sha256=expected_plan_sha256,
            operation=operation,
            object_names=(result.Name,),
            before_volume_mm3=10.0,
            after_volume_mm3=_result_volume(operation),
        )

    monkeypatch.setattr(
        dressup_execution,
        "apply_partdesign_dressup_transform_plan",
        apply,
    )
    route = next(
        item
        for item in REVIEWED_PARTDESIGN_DRESSUP_ROUTES
        if item.operation.operation_id == operation.value
    )
    context = _ReviewedFamilyExecutionContext(
        document=fixture.document,
        source_results=fixture.source_results,
        session=fixture.session,
    )
    native = route.family.apply_plan(
        fixture.document,
        fixture.plan,
        fixture.payload,
        fixture.plan_document,
        fixture.operation,
        context,
    )

    assert native.object is fixture.document.Objects[-1]
    assert fixture.body.Tip is native.object
    assert native.object.BaseFeature is fixture.base
    assert type(native.receipt) is PartDesignDressupOwnershipClosure
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


def _multitransform_fixture(step_count: int) -> _Fixture:
    fixture = _fixture(PartDesignDressupTransformOperation.MULTI_TRANSFORM)
    if step_count == 2:
        return fixture
    assert type(fixture.plan.parameters) is MultiTransformParameters
    templates = fixture.plan.parameters.steps
    steps = tuple(
        dataclasses.replace(templates[index % len(templates)], step_id=f"step_{index}")
        for index in range(step_count)
    )
    plan = dataclasses.replace(
        fixture.plan,
        parameters=MultiTransformParameters(steps=steps),
    )
    return dataclasses.replace(
        fixture,
        plan=plan,
        payload=plan.canonical_bytes,
        plan_document=PARTDESIGN_DRESSUP_TRANSFORM_MANIFEST.plan_document(
            plan.canonical_bytes,
            plan.plan_sha256,
        ),
    )


def _install_multitransform_apply(
    monkeypatch: pytest.MonkeyPatch,
    fixture: _Fixture,
) -> tuple[str, ...]:
    assert type(fixture.plan.parameters) is MultiTransformParameters
    expected_type_ids = (
        fixture.operation.native_type_id,
        *(
            dressup_rules._NATIVE_STEP_SPECS[step.kind].type_id  # noqa: SLF001
            for step in fixture.plan.parameters.steps
        ),
    )

    def apply(raw, *, expected_content_sha256, expected_plan_sha256, bindings):
        assert raw == fixture.payload
        assert expected_content_sha256 == fixture.plan_document.content_sha256
        assert expected_plan_sha256 == fixture.plan.plan_sha256
        created = tuple(
            _Feature(
                fixture.document,
                "MultiResult" if index == 0 else f"MultiChild{index}",
                type_id,
                15.0 if index == 0 else 1.0,
            )
            for index, type_id in enumerate(expected_type_ids)
        )
        created[0].BaseFeature = bindings.base.object
        fixture.body.Group.extend(created)
        fixture.body.Tip = created[0]
        fixture.document.Objects.extend(created)
        return PartDesignDressupTransformConformanceReceipt(
            plan_sha256=expected_plan_sha256,
            operation=PartDesignDressupTransformOperation.MULTI_TRANSFORM,
            object_names=tuple(item.Name for item in created),
            before_volume_mm3=10.0,
            after_volume_mm3=15.0,
        )

    monkeypatch.setattr(
        dressup_execution,
        "apply_partdesign_dressup_transform_plan",
        apply,
    )
    return expected_type_ids


@pytest.mark.parametrize("step_count", (2, 8))
def test_multitransform_executes_and_seals_exact_plan_bound_owned_closure(
    monkeypatch: pytest.MonkeyPatch,
    step_count: int,
) -> None:
    fixture = _multitransform_fixture(step_count)
    expected_type_ids = _install_multitransform_apply(monkeypatch, fixture)
    route = next(
        item
        for item in REVIEWED_PARTDESIGN_DRESSUP_ROUTES
        if item.operation.operation_id == "multi_transform"
    )
    context = _ReviewedFamilyExecutionContext(
        document=fixture.document,
        source_results=fixture.source_results,
        session=fixture.session,
    )
    native = route.family.apply_plan(
        fixture.document,
        fixture.plan,
        fixture.payload,
        fixture.plan_document,
        fixture.operation,
        context,
    )
    resolution = route.family.resolve_dynamic_product_result(
        fixture.plan,
        fixture.plan_document,
        fixture.operation,
        native,
    )
    assert resolution is not None
    executed = ReviewedNativeExecutionResult(
        route=route,
        object=native.object,
        plan_sha256=fixture.plan_document.document_digest,
        plan_content_sha256=fixture.plan_document.content_sha256,
        native_receipt=native.receipt,
        owned_objects=native.owned_objects,
        _verified_execution_context=context,
        _verified_dynamic_resolution=resolution,
    )

    assert len(executed.owned_objects) == step_count + 1
    assert tuple(item.TypeId for item in executed.owned_objects) == expected_type_ids
    assert executed.semantic_roles == (
        SemanticRole.FEATURE,
        *(SemanticRole.SUPPORT for _index in range(step_count)),
    )
    assert fixture.body.Group[-(step_count + 1) :] == list(executed.owned_objects)
    assert fixture.body.Tip is executed.object
    assert executed.object.BaseFeature is fixture.base
    assert route.family.minimum_sources == route.family.maximum_sources == 1


@pytest.mark.parametrize(
    "failure",
    ("extra", "missing", "order", "type", "forbidden_kind", "plan_rebound"),
)
def test_multitransform_dynamic_resolution_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    fixture = _multitransform_fixture(2)
    _install_multitransform_apply(monkeypatch, fixture)
    route = next(
        item
        for item in REVIEWED_PARTDESIGN_DRESSUP_ROUTES
        if item.operation.operation_id == "multi_transform"
    )
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
    plan = fixture.plan
    plan_document = fixture.plan_document
    if failure == "extra":
        extra = _Feature(fixture.document, "Extra", "PartDesign::Scaled", 1.0)
        native = _ReviewedFamilyNativeExecution(
            object=native.object,
            receipt=native.receipt,
            owned_objects=(*native.owned_objects, extra),
        )
    elif failure == "missing":
        native = _ReviewedFamilyNativeExecution(
            object=native.object,
            receipt=native.receipt,
            owned_objects=native.owned_objects[:-1],
        )
    elif failure == "order":
        native = _ReviewedFamilyNativeExecution(
            object=native.object,
            receipt=native.receipt,
            owned_objects=(
                native.owned_objects[0],
                native.owned_objects[2],
                native.owned_objects[1],
            ),
        )
    elif failure == "type":
        native.owned_objects[1].TypeId = "PartDesign::Fillet"
    elif failure == "forbidden_kind":
        assert type(plan.parameters) is MultiTransformParameters
        object.__setattr__(plan.parameters.steps[0], "kind", "forbidden")
    else:
        plan_document = dataclasses.replace(plan_document, document_digest="f" * 64)

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        route.family.resolve_dynamic_product_result(
            plan,
            plan_document,
            fixture.operation,
            native,
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize(
    "failure",
    (
        "zero_sources",
        "multiple_sources",
        "role",
        "provenance",
        "stale",
        "wrong_tip",
        "wrong_body",
        "wrong_document",
        "selection",
        "content",
        "plan_document",
        "multisolid",
    ),
)
def test_dressup_rejects_unsealed_base_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    fixture = _fixture()
    sources = fixture.source_results
    plan_document = fixture.plan_document
    if failure == "zero_sources":
        sources = ()
    elif failure == "multiple_sources":
        sources = (*sources, sources[0])
    elif failure == "role":
        fixture.session.identities[fixture.base] = dataclasses.replace(
            fixture.session.identities[fixture.base],
            semantic_role=SemanticRole.PRIMITIVE,
        )
    elif failure == "provenance":
        fixture.session.identities[fixture.base] = dataclasses.replace(
            fixture.session.identities[fixture.base],
            provenance=Provenance(
                source=ProvenanceSource.MODEL,
                operation_id="manual_edit",
            ),
        )
    elif failure == "stale":
        fixture.base.Shape.value += "-stale"
    elif failure == "wrong_tip":
        fixture.body.Tip = None
    elif failure == "wrong_body":
        fixture.body.Group = []
    elif failure == "wrong_document":
        fixture.base.Document = _Document()
    elif failure == "selection":
        receipt = dataclasses.replace(sources[0].native_receipt, node_id="node_wrong")
        sources = (
            ReviewedNativeExecutionResult(
                route=sources[0].route,
                object=sources[0].object,
                plan_sha256=sources[0].plan_sha256,
                plan_content_sha256=sources[0].plan_content_sha256,
                native_receipt=receipt,
            ),
        )
    elif failure == "content":
        sources = (
            ReviewedNativeExecutionResult(
                route=sources[0].route,
                object=sources[0].object,
                plan_sha256=sources[0].plan_sha256,
                plan_content_sha256="f" * 64,
                native_receipt=sources[0].native_receipt,
            ),
        )
    elif failure == "plan_document":
        plan_document = dataclasses.replace(plan_document, content_sha256="f" * 64)
    else:
        fixture.base.Shape.Solids = (object(), object())
    before = tuple(fixture.document.Objects)
    called = False

    def apply(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid source must remain pre-mutation")

    monkeypatch.setattr(
        dressup_execution,
        "apply_partdesign_dressup_transform_plan",
        apply,
    )
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_partdesign_dressup_reviewed_plan_with_sources(
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


class _Vector:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x = x
        self.y = y
        self.z = z


class _Vertex:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.Point = _Vector(x, y, z)


class Line:
    pass


class Plane:
    pass


class _Edge:
    def __init__(self) -> None:
        self.Vertexes = (_Vertex(0.0, 0.0, 0.0), _Vertex(0.0, 0.0, 10.0))
        self.Curve = Line()


class _Face:
    def __init__(self) -> None:
        self.Vertexes = (
            _Vertex(0.0, 0.0, 10.0),
            _Vertex(1.0, 0.0, 10.0),
            _Vertex(1.0, 1.0, 10.0),
        )
        self.Surface = Plane()


class _Bounds:
    XMin = 0.0
    YMin = 0.0
    ZMin = 0.0
    XMax = 1.0
    YMax = 1.0
    ZMax = 10.0
    DiagonalLength = 10.1


class _TopologyShape:
    BoundBox = _Bounds()

    def __init__(self, *, edges: tuple[object, ...], faces: tuple[object, ...]) -> None:
        self.Edges = edges
        self.Faces = faces


@pytest.mark.parametrize("cardinality", (0, 2))
def test_dressup_trusted_topology_resolver_requires_unique_edge_and_face(
    cardinality: int,
) -> None:
    edge_role = AxisAlignedEdgeRole(
        axis=Axis.Z,
        first_side=Side.MINIMUM,
        second_side=Side.MINIMUM,
    )
    face_role = AxisAlignedFaceRole(axis=Axis.Z, side=Side.MAXIMUM)
    unique = _TopologyShape(edges=(_Edge(),), faces=(_Face(),))
    assert dressup_rules._resolve_edge(unique, edge_role) == "Edge1"  # noqa: SLF001
    assert dressup_rules._resolve_face(unique, face_role) == "Face1"  # noqa: SLF001
    ambiguous = _TopologyShape(
        edges=tuple(_Edge() for _ in range(cardinality)),
        faces=tuple(_Face() for _ in range(cardinality)),
    )
    with pytest.raises(PartDesignDressupTransformRuleError):
        dressup_rules._resolve_edge(ambiguous, edge_role)  # noqa: SLF001
    with pytest.raises(PartDesignDressupTransformRuleError):
        dressup_rules._resolve_face(ambiguous, face_role)  # noqa: SLF001


@pytest.mark.parametrize("operation", PARTDESIGN_DRESSUP_REVIEWED_OPERATIONS)
def test_dressup_native_receipt_rejects_noop_effect(
    operation: PartDesignDressupTransformOperation,
) -> None:
    with pytest.raises(PartDesignDressupTransformRuleError):
        PartDesignDressupTransformConformanceReceipt(
            plan_sha256=_sha("plan"),
            operation=operation,
            object_names=("NoOp",),
            before_volume_mm3=10.0,
            after_volume_mm3=10.0,
        )
