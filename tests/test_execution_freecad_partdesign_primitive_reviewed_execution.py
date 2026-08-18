"""Focused product-bridge tests for reviewed PartDesign primitives."""

from __future__ import annotations

import dataclasses
import hashlib
import sys
from dataclasses import dataclass
from types import ModuleType

import pytest

import vibecad.execution.freecad_partdesign_primitive_reviewed_execution as pd_execution
import vibecad.execution.freecad_reviewed_intent_execution as reviewed_execution
from tests.test_intent_bridge_freecad_partdesign_primitive_adapter import (
    _graph,
    _MemoryPlanSink,
    _request,
)
from vibecad.execution.freecad_partdesign_primitive_reviewed_execution import (
    PARTDESIGN_PRIMITIVE_MANIFEST,
    PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS,
    PARTDESIGN_PRIMITIVE_REQUEST_TERMS,
    PARTDESIGN_PRIMITIVE_RESULT_INVARIANTS,
    PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC,
    PARTDESIGN_PRIMITIVE_REVIEWED_PRODUCT_IDENTITIES,
    PartDesignPrimitiveOwnershipClosure,
    execute_partdesign_primitive_reviewed_plan_with_sources,
    partdesign_primitive_reviewed_adapter_factory,
    resolve_partdesign_primitive_reviewed_operation,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    ReviewedIntentRoute,
    ReviewedNativeExecutionResult,
    _ReviewedIntentFamilyDescriptor,
    _ReviewedProductResultContract,
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
from vibecad.intent_bridge.contracts import BridgeBudget
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    ParametricFeatureGraphV2Codec,
)
from vibecad.intent_bridge.ports import TrustedCodecRegistry
from vibecad.parametric.freecad_partdesign_primitive_rules import (
    AuthenticatedPrimitiveObject,
    PartDesignPrimitiveConformanceReceipt,
    PartDesignPrimitiveOperation,
    decode_partdesign_primitive_backend_plan,
)
from vibecad.validation import EntityObservation
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1


def _semantic_operation(operation) -> str:
    namespace, version, term_id, digest = operation.semantic_term.semantic_identity
    return f"{namespace}/{version}/{term_id}@{digest}"


@dataclass(frozen=True, slots=True)
class _Lowered:
    plan: object
    payload: bytes
    receipt: object

    @property
    def operation(self):
        return self.receipt.operation

    @property
    def plan_document(self):
        return self.receipt.plan_document


def _lower(
    operation: PartDesignPrimitiveOperation,
    *,
    base_for_additive: bool = False,
) -> _Lowered:
    request, reader, policy = _request(_graph(operation, base_for_additive=base_for_additive))
    documents = {item.artifact_id: item for item in request.documents}
    intent = documents[request.intent_artifact_ids[0]]
    old_capability = documents[request.capability_artifact_ids[0]]
    capability, capability_payload = PARTDESIGN_PRIMITIVE_MANIFEST.capability_document(
        artifact_id=old_capability.artifact_id
    )
    request = dataclasses.replace(
        request,
        terms=tuple(
            {
                item.term_ref_id: item
                for item in (*request.terms, *PARTDESIGN_PRIMITIVE_REQUEST_TERMS)
            }.values()
        ),
        documents=(intent, capability),
        budget=BridgeBudget(
            max_input_bytes=intent.size_bytes + len(capability_payload),
            max_output_bytes=request.budget.max_output_bytes,
            max_subject_lookups=1,
            max_rule_applications=1,
        ),
    )
    reader.payloads[capability.artifact_id] = capability_payload
    adapter = partdesign_primitive_reviewed_adapter_factory(_MemoryPlanSink())
    _result, receipt = adapter.lower_with_receipt(
        request,
        artifacts=reader,
        codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
        proof_policy=policy,
    )
    plan, payload = adapter.read_plan(receipt)
    return _Lowered(plan=plan, payload=payload, receipt=receipt)


def _program(
    operation: PartDesignPrimitiveOperation,
    *,
    base_for_additive: bool = False,
) -> ReviewedIntentProgramV1:
    graph = _graph(operation, base_for_additive=base_for_additive)
    route = next(
        item
        for item in REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES
        if item.operation.operation_id == operation.value
    )
    return ReviewedIntentProgramV1(
        operation_id=route.operation_id,
        semantic_operation=route.semantic_operation,
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


class _Shape:
    def __init__(self, volume: float, token: str) -> None:
        self.Volume = volume
        self.Solids = (object(),)
        self.ShapeType = "Solid"
        self._token = token

    def isNull(self) -> bool:
        return False

    def isValid(self) -> bool:
        return True

    def exportBrepToString(self) -> str:
        return self._token

    def mutate(self) -> None:
        self._token += ":stale"


class _Object:
    def __init__(self, document: _Document, name: str, type_id: str) -> None:
        self.Document = document
        self.Name = name
        self.TypeId = type_id
        self.State = ("Up-to-date",)
        self.Visibility = True

    def isValid(self) -> bool:
        return True


class _Feature(_Object):
    def __init__(
        self,
        document: _Document,
        name: str,
        type_id: str,
        *,
        volume: float,
        token: str,
        base: object | None,
    ) -> None:
        super().__init__(document, name, type_id)
        self.Shape = _Shape(volume, token)
        self.BaseFeature = base


class _Origin(_Object):
    def __init__(self, document: _Document, name: str) -> None:
        super().__init__(document, name, "App::Origin")
        self.Group: tuple[object, ...] = ()
        helper_types = (
            "App::Line",
            "App::Line",
            "App::Line",
            "App::Plane",
            "App::Plane",
            "App::Plane",
            "App::Point",
        )
        self.OriginFeatures = tuple(
            _Object(document, f"{name}_Helper{index}", type_id)
            for index, type_id in enumerate(helper_types)
        )
        roles = (
            "X_Axis",
            "Y_Axis",
            "Z_Axis",
            "XY_Plane",
            "XZ_Plane",
            "YZ_Plane",
            "Origin",
        )
        for helper, role in zip(self.OriginFeatures, roles, strict=True):
            helper.Role = role
            helper.InList = (self,)


class _Body(_Object):
    def __init__(self, document: _Document, name: str) -> None:
        super().__init__(document, name, "PartDesign::Body")
        self.Group: tuple[object, ...] = ()
        self.Tip: object | None = None
        self.Origin = _Origin(document, f"{name}_Origin")


class _Document:
    def __init__(self) -> None:
        self.Objects: list[object] = []
        self.UndoMode = 1
        self.HasPendingTransaction = False

    def getObject(self, name: str):
        return next((item for item in self.Objects if item.Name == name), None)

    def addObject(self, type_id: str, name: str):
        assert type_id == "PartDesign::Body"
        body = _Body(self, name)
        self.Objects.extend((body, body.Origin, *body.Origin.OriginFeatures))
        return body

    def removeObject(self, name: str) -> None:
        item = self.getObject(name)
        if item is None:
            return
        for body in tuple(value for value in self.Objects if isinstance(value, _Body)):
            if item in body.Group:
                body.Group = tuple(value for value in body.Group if value is not item)
                body.Tip = body.Group[-1] if body.Group else None
        self.Objects.remove(item)

    def recompute(self) -> None:
        return None


class _Session:
    def __init__(self, document: _Document, identities: dict[object, EntityIdentity]) -> None:
        self.doc = document
        self.identities = identities

    def read_object_identity(self, item: object) -> EntityIdentity:
        return self.identities[item]


def _identity(index: int, item: object, role: SemanticRole) -> EntityIdentity:
    return EntityIdentity(
        object_id=f"object_{index:032x}",
        feature_id=f"feature_{index:032x}",
        object_type=item.TypeId,
        semantic_role=role,
        provenance=Provenance(
            source=ProvenanceSource.MODEL,
            operation_id="apply_reviewed_intent",
        ),
    )


def _shape_sha256(item: object) -> str:
    return hashlib.sha256(item.Shape.exportBrepToString().encode()).hexdigest()


_SOURCE_FAMILY = _ReviewedIntentFamilyDescriptor(
    manifest=PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC.manifest,
    subject_type_term=PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC.subject_type_term,
    adapter_factory=PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC.adapter_factory,
    validate_plan=PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC.validate_plan,
    execute_plan=PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC.execute_plan,
    product_results=tuple(
        _ReviewedProductResultContract(
            operation_id=operation.value,
            result_kind=_ReviewedProductResultKind.SOLID,
            owned_type_ids=PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS[operation]
            .closure_for_sources(0)
            .owned_type_ids,
            semantic_roles=PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS[operation]
            .closure_for_sources(0)
            .semantic_roles,
        )
        for operation in PartDesignPrimitiveOperation
        if operation.value.startswith("additive_")
    ),
    minimum_sources=0,
    maximum_sources=1,
)


def _source_route() -> ReviewedIntentRoute:
    operation = next(
        item
        for item in PARTDESIGN_PRIMITIVE_MANIFEST.operations
        if item.operation_id == PartDesignPrimitiveOperation.ADDITIVE_BOX.value
    )
    operation_id = f"partdesign.{operation.operation_id}"
    formal = reviewed_execution.current_freecad_intent_capability_specs()
    modern = tuple(
        dataclasses.replace(item, semantic_operation=_semantic_operation(operation))
        if item.operation_id == operation_id
        else item
        for item in formal
    )
    original = reviewed_execution.current_freecad_intent_capability_specs
    reviewed_execution.current_freecad_intent_capability_specs = lambda: modern
    try:
        return ReviewedIntentRoute(
            operation_id=operation_id,
            semantic_operation=_semantic_operation(operation),
            family=_SOURCE_FAMILY,
            manifest=PARTDESIGN_PRIMITIVE_MANIFEST,
            operation=operation,
            subject_type_term=PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC.subject_type_term,
        )
    finally:
        reviewed_execution.current_freecad_intent_capability_specs = original


def _source_fixture() -> tuple[_Session, ReviewedNativeExecutionResult, _Body, _Feature]:
    document = _Document()
    body = document.addObject("PartDesign::Body", "AuthenticatedBody")
    feature = _Feature(
        document,
        "AuthenticatedBase",
        "PartDesign::AdditiveBox",
        volume=100.0,
        token="authenticated-base",
        base=None,
    )
    body.Group = (feature,)
    body.Tip = feature
    document.Objects.append(feature)
    receipt = PartDesignPrimitiveConformanceReceipt(
        plan_sha256=hashlib.sha256(b"source-plan").hexdigest(),
        operation=PartDesignPrimitiveOperation.ADDITIVE_BOX,
        object_name=feature.Name,
        before_volume_mm3=0.0,
        after_volume_mm3=100.0,
    )
    closure = PartDesignPrimitiveOwnershipClosure(
        invariant=PARTDESIGN_PRIMITIVE_RESULT_INVARIANTS[PartDesignPrimitiveOperation.ADDITIVE_BOX],
        native_receipt=receipt,
        object=feature,
        body=body,
        base=None,
        body_closure=(body, body.Origin, *body.Origin.OriginFeatures),
        created_body=True,
        base_shape_sha256=None,
        result_shape_sha256=_shape_sha256(feature),
    )
    owned = closure.owned_objects(feature)
    roles = (SemanticRole.FEATURE, SemanticRole.PART, *(SemanticRole.SUPPORT,) * 8)
    identities = {
        item: _identity(index + 1, item, role)
        for index, (item, role) in enumerate(zip(owned, roles, strict=True))
    }
    result = ReviewedNativeExecutionResult(
        route=_source_route(),
        object=feature,
        plan_sha256=receipt.plan_sha256,
        plan_content_sha256=hashlib.sha256(b"source-content").hexdigest(),
        native_receipt=closure,
        owned_objects=owned,
    )
    return _Session(document, identities), result, body, feature


def _install_native_apply(
    monkeypatch: pytest.MonkeyPatch,
    called: list[PartDesignPrimitiveOperation],
) -> None:
    def apply(
        raw: bytes,
        *,
        expected_content_sha256: str,
        expected_plan_sha256: str,
        bindings: object,
    ) -> PartDesignPrimitiveConformanceReceipt:
        plan = decode_partdesign_primitive_backend_plan(
            raw,
            expected_content_sha256=expected_content_sha256,
            expected_plan_sha256=expected_plan_sha256,
        )
        assert isinstance(bindings.base, (AuthenticatedPrimitiveObject, type(None)))
        base = None if bindings.base is None else bindings.base.object
        before_volume = 0.0 if base is None else float(base.Shape.Volume)
        additive = plan.operation.value.startswith("additive_")
        after_volume = before_volume + 25.0 if additive else before_volume - 25.0
        feature = _Feature(
            bindings.document,
            f"Result_{plan.operation.value}",
            PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS[plan.operation].native_type_id,
            volume=after_volume,
            token=f"result:{plan.operation.value}",
            base=base,
        )
        bindings.body.Group = (*bindings.body.Group, feature)
        bindings.body.Tip = feature
        bindings.document.Objects.append(feature)
        called.append(plan.operation)
        return PartDesignPrimitiveConformanceReceipt(
            plan_sha256=plan.plan_sha256,
            operation=plan.operation,
            object_name=feature.Name,
            before_volume_mm3=before_volume,
            after_volume_mm3=after_volume,
        )

    monkeypatch.setattr(pd_execution, "apply_partdesign_primitive_plan", apply)


def _observation(operation: PartDesignPrimitiveOperation, volume: float) -> EntityObservation:
    return EntityObservation(
        object_id="object_" + "a" * 32,
        feature_id="feature_" + "b" * 32,
        object_type=PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS[operation].native_type_id,
        semantic_role="feature",
        provenance={"source": "model", "operation_id": "apply_reviewed_intent"},
        placement=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
        volume_mm3=volume,
        area_mm2=1.0,
        bbox_mm=(1.0, 1.0, 1.0),
        center_of_mass_mm=(0.0, 0.0, 0.0),
        valid_shape=True,
        solid_count=1,
    )


def test_partdesign_primitive_product_identity_and_source_tables_are_exact() -> None:
    expected = {
        (
            f"partdesign.{operation.value}",
            _semantic_operation(
                next(
                    item
                    for item in PARTDESIGN_PRIMITIVE_MANIFEST.operations
                    if item.operation_id == operation.value
                )
            ),
        )
        for operation in PartDesignPrimitiveOperation
    }

    assert set(PARTDESIGN_PRIMITIVE_REVIEWED_PRODUCT_IDENTITIES) == expected
    assert len(PARTDESIGN_PRIMITIVE_MANIFEST.operations) == 16
    assert tuple(PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC.operation_ids) == tuple(
        item.value for item in PartDesignPrimitiveOperation
    )
    for identity in expected:
        assert resolve_partdesign_primitive_reviewed_operation(*identity) is not None
    assert (
        resolve_partdesign_primitive_reviewed_operation("partdesign.additive_box", "unknown")
        is None
    )


def test_shared_routes_strictly_bind_legacy_formal_to_full_primitive_manifest() -> None:
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 81
    assert CURRENT_REVIEWED_INTENT_ROUTES[-42:-26] == REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES
    assert len(REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES) == 16
    assert len(REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES[0].family.product_results) == 24
    formal = reviewed_execution.current_freecad_intent_capability_specs()
    document = _Document()
    for route in REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES:
        matching = tuple(item for item in formal if item.operation_id == route.operation_id)
        assert len(matching) == 1
        assert route.semantic_operation == matching[0].semantic_operation
        assert route.semantic_operation == route.operation.semantic_term.term_id
        assert "@" not in route.semantic_operation
        assert route.manifest_semantic_operation == _semantic_operation(route.operation)
        assert route.manifest_semantic_operation != route.semantic_operation
        with_one = route.family.product_result(
            route.operation,
            context=reviewed_execution._ReviewedFamilyExecutionContext(
                session=_Session(document, {}),
                document=document,
                source_results=(object(),),
            ),
        )
        assert with_one.source_count == 1
        assert with_one.owned_type_ids == (route.operation.native_type_id,)
        if route.operation.operation_id.startswith("additive_"):
            without_source = route.family.product_result(
                route.operation,
                context=reviewed_execution._ReviewedFamilyExecutionContext(
                    session=_Session(document, {}),
                    document=document,
                    source_results=(),
                ),
            )
            assert without_source.source_count == 0
            assert len(without_source.owned_type_ids) == 10
            assert without_source.owned_type_ids[1:] == (
                "PartDesign::Body",
                "App::Origin",
                "App::Line",
                "App::Line",
                "App::Line",
                "App::Plane",
                "App::Plane",
                "App::Plane",
                "App::Point",
            )
        else:
            with pytest.raises(ReviewedIntentExecutionError) as caught:
                route.family.product_result(
                    route.operation,
                    context=reviewed_execution._ReviewedFamilyExecutionContext(
                        session=_Session(document, {}),
                        document=document,
                        source_results=(),
                    ),
                )
            assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize("operation", tuple(PartDesignPrimitiveOperation))
def test_shared_legacy_routes_lower_all_sixteen(
    operation: PartDesignPrimitiveOperation,
) -> None:
    program = _program(operation)

    route = route_reviewed_intent(program)
    lowered = lower_reviewed_intent(program)

    assert route in REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES
    assert route.operation.operation_id == operation.value
    assert lowered.route is route
    assert lowered.plan.operation is operation
    assert (lowered.plan.base is None) is operation.value.startswith("additive_")


@pytest.mark.parametrize(
    ("operation", "with_base", "expected_owned", "expected_before", "expected_after"),
    (
        (PartDesignPrimitiveOperation.ADDITIVE_BOX, False, 10, 0.0, 25.0),
        (PartDesignPrimitiveOperation.ADDITIVE_CONE, True, 1, 100.0, 125.0),
        (PartDesignPrimitiveOperation.SUBTRACTIVE_BOX, True, 1, 100.0, 75.0),
    ),
)
def test_shared_native_route_executes_first_additive_base_additive_and_subtractive(
    monkeypatch: pytest.MonkeyPatch,
    operation: PartDesignPrimitiveOperation,
    with_base: bool,
    expected_owned: int,
    expected_before: float,
    expected_after: float,
) -> None:
    called: list[PartDesignPrimitiveOperation] = []
    _install_native_apply(monkeypatch, called)
    if with_base:
        session, source, body, base = _source_fixture()
        source_results = (source,)
    else:
        document = _Document()
        session = _Session(document, {})
        source_results = ()
        body = None
        base = None
    monkeypatch.setattr(
        reviewed_execution,
        "require_reviewed_route_verified",
        lambda selected, *, freecad: None,
    )
    monkeypatch.setitem(sys.modules, "FreeCAD", ModuleType("FreeCAD"))

    result = execute_reviewed_intent_native(
        session,
        _program(operation, base_for_additive=with_base),
        source_results=source_results,
    )

    assert result.route in REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES
    assert called == [operation]
    assert len(result.owned_objects) == expected_owned
    assert result.native_receipt.body.Tip is result.object
    assert result.object.BaseFeature is base
    assert result.native_receipt.native_receipt.before_volume_mm3 == expected_before
    assert result.native_receipt.native_receipt.after_volume_mm3 == expected_after
    if body is not None:
        assert body.Group == (base, result.object)


@pytest.mark.parametrize("operation", tuple(PartDesignPrimitiveOperation))
def test_reviewed_adapter_lowers_all_sixteen_to_one_exact_static_route(
    operation: PartDesignPrimitiveOperation,
) -> None:
    lowered = _lower(operation)
    contract = PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS[operation]

    assert lowered.plan.operation is operation
    assert lowered.operation.operation_id == operation.value
    assert lowered.operation.native_type_id == contract.native_type_id
    assert lowered.payload == lowered.plan.canonical_bytes
    assert (lowered.plan.base is None) is operation.value.startswith("additive_")
    assert (contract.minimum_sources, contract.maximum_sources) == (
        (0, 1) if operation.value.startswith("additive_") else (1, 1)
    )
    assert "PartDesign::" not in lowered.payload.decode("ascii")


@pytest.mark.parametrize(
    "operation",
    tuple(item for item in PartDesignPrimitiveOperation if item.value.startswith("additive_")),
)
def test_all_additive_routes_lower_the_optional_authenticated_base_contract(
    operation: PartDesignPrimitiveOperation,
) -> None:
    lowered = _lower(operation, base_for_additive=True)

    assert lowered.plan.base is not None
    assert lowered.plan.base.node_id == "node_base"
    assert tuple(
        item.source_count
        for item in PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS[operation].closure_variants
    ) == (0, 1)


@pytest.mark.parametrize("operation", tuple(PartDesignPrimitiveOperation))
def test_all_sixteen_execute_through_native_rule_with_static_body_closure(
    monkeypatch: pytest.MonkeyPatch,
    operation: PartDesignPrimitiveOperation,
) -> None:
    called: list[PartDesignPrimitiveOperation] = []
    _install_native_apply(monkeypatch, called)
    additive = operation.value.startswith("additive_")
    lowered = _lower(operation)
    if additive:
        document = _Document()
        session = _Session(document, {})
        sources: tuple[ReviewedNativeExecutionResult, ...] = ()
    else:
        session, source, _body, _feature = _source_fixture()
        document = session.doc
        sources = (source,)

    result = execute_partdesign_primitive_reviewed_plan_with_sources(
        document,
        lowered.plan,
        lowered.payload,
        lowered.plan_document,
        lowered.operation,
        sources,
        session=session,
    )

    assert called == [operation]
    assert result.object.TypeId == lowered.operation.native_type_id
    assert result.receipt.operation is operation
    assert result.receipt.body.Tip is result.object
    assert result.object.BaseFeature is (None if additive else sources[0].object)
    expected_owned = 10 if additive else 1
    assert len(result.owned_objects) == expected_owned
    observation = _observation(operation, float(result.object.Shape.Volume))
    result.receipt.validate_adoption(
        document,
        result.object,
        observation,
    )
    with pytest.raises(ReviewedIntentExecutionError):
        result.receipt.validate_adopted_observation(dataclasses.replace(observation, solid_count=0))


@pytest.mark.parametrize(
    "operation",
    tuple(item for item in PartDesignPrimitiveOperation if item.value.startswith("additive_")),
)
def test_additive_routes_execute_with_one_authenticated_base(
    monkeypatch: pytest.MonkeyPatch,
    operation: PartDesignPrimitiveOperation,
) -> None:
    called: list[PartDesignPrimitiveOperation] = []
    _install_native_apply(monkeypatch, called)
    lowered = _lower(operation, base_for_additive=True)
    session, source, body, feature = _source_fixture()

    result = execute_partdesign_primitive_reviewed_plan_with_sources(
        session.doc,
        lowered.plan,
        lowered.payload,
        lowered.plan_document,
        lowered.operation,
        (source,),
        session=session,
    )

    assert called == [operation]
    assert result.owned_objects == (result.object,)
    assert result.object.BaseFeature is feature
    assert body.Group == (feature, result.object)
    assert body.Tip is result.object


@pytest.mark.parametrize(
    "failure",
    (
        "unknown",
        "stale",
        "wrong_body",
        "wrong_base_feature",
        "wrong_provenance",
        "cross_document",
        "tampered_payload",
    ),
)
def test_invalid_base_and_plan_inputs_fail_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    called: list[PartDesignPrimitiveOperation] = []
    _install_native_apply(monkeypatch, called)
    lowered = _lower(PartDesignPrimitiveOperation.SUBTRACTIVE_BOX)
    session, source, body, feature = _source_fixture()
    payload = lowered.payload
    sources: tuple[object, ...] = (source,)
    if failure == "unknown":
        sources = (object(),)
    elif failure == "stale":
        feature.Shape.mutate()
    elif failure == "wrong_body":
        body.Tip = None
    elif failure == "wrong_base_feature":
        feature.BaseFeature = object()
    elif failure == "wrong_provenance":
        identity = session.identities[body]
        session.identities[body] = dataclasses.replace(
            identity,
            provenance=Provenance(
                source=ProvenanceSource.USER,
                operation_id="apply_reviewed_intent",
            ),
        )
    elif failure == "cross_document":
        feature.Document = _Document()
    elif failure == "tampered_payload":
        payload += b" "
    before = tuple(session.doc.Objects)

    with pytest.raises(ReviewedIntentExecutionError):
        execute_partdesign_primitive_reviewed_plan_with_sources(
            session.doc,
            lowered.plan,
            payload,
            lowered.plan_document,
            lowered.operation,
            sources,
            session=session,
        )

    assert called == []
    assert tuple(session.doc.Objects) == before


@pytest.mark.parametrize(
    ("operation", "sources"),
    (
        (PartDesignPrimitiveOperation.ADDITIVE_BOX, (object(),)),
        (PartDesignPrimitiveOperation.SUBTRACTIVE_BOX, ()),
    ),
)
def test_operation_specific_source_cardinality_is_inert(
    monkeypatch: pytest.MonkeyPatch,
    operation: PartDesignPrimitiveOperation,
    sources: tuple[object, ...],
) -> None:
    called: list[PartDesignPrimitiveOperation] = []
    _install_native_apply(monkeypatch, called)
    lowered = _lower(operation)
    document = _Document()

    with pytest.raises(ReviewedIntentExecutionError):
        execute_partdesign_primitive_reviewed_plan_with_sources(
            document,
            lowered.plan,
            lowered.payload,
            lowered.plan_document,
            lowered.operation,
            sources,
            session=_Session(document, {}),
        )

    assert called == []
    assert document.Objects == []


def test_first_additive_native_failure_removes_body_and_origin_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lowered = _lower(PartDesignPrimitiveOperation.ADDITIVE_BOX)
    document = _Document()

    def fail(*args, **kwargs):
        raise RuntimeError("bounded native failure")

    monkeypatch.setattr(pd_execution, "apply_partdesign_primitive_plan", fail)
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_partdesign_primitive_reviewed_plan_with_sources(
            document,
            lowered.plan,
            lowered.payload,
            lowered.plan_document,
            lowered.operation,
            (),
            session=_Session(document, {}),
        )

    assert caught.value.code is ReviewedIntentExecutionErrorCode.EXECUTION_FAILED
    assert document.Objects == []
