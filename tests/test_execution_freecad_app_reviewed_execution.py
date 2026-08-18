from __future__ import annotations

import dataclasses
import hashlib
from types import SimpleNamespace

import pytest

import vibecad.execution.freecad_app_reviewed_execution as app_execution
from tests.test_intent_bridge_freecad_app_family_adapter import _graph
from vibecad.execution.freecad_app_reviewed_execution import (
    APP_NO_SOURCE_REVIEWED_FAMILY_SPEC,
    APP_ONE_SOURCE_REVIEWED_FAMILY_SPEC,
    APP_REVIEWED_FAMILY_SPECS,
    APP_REVIEWED_PRODUCT_CONTRACTS,
    APP_REVIEWED_PRODUCT_IDENTITIES,
    AppReviewedProductReceipt,
    AppReviewedResultKind,
    build_app_reviewed_bindings,
    execute_app_reviewed_plan,
    resolve_app_reviewed_operation,
    validate_app_reviewed_plan,
)
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_APP_NO_SOURCE_ROUTES,
    REVIEWED_APP_ONE_SOURCE_ROUTES,
    REVIEWED_APP_ROUTES,
    REVIEWED_PART_PRIMITIVE_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    ReviewedNativeExecutionResult,
    _ReviewedFamilyExecutionContext,
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
from vibecad.intent_bridge.contracts import DocumentRef
from vibecad.intent_bridge.freecad_app_family_adapter import APP_FAMILY_MANIFEST
from vibecad.intent_bridge.reviewed_family_engine import ReviewedPlanReceipt
from vibecad.parametric.freecad_app_family_rules import (
    APP_FAMILY_NATIVE_TYPE_IDS,
    APP_FAMILY_RELATION_KINDS,
    AppFamilyBackendPlan,
    AppFamilyConformanceReceipt,
    AppFamilyExecutionBindings,
    AppFamilyOperation,
    AppFamilyRelationKind,
    encode_app_family_configuration,
)
from vibecad.parametric.freecad_part_core_rules import (
    PartCoreConformanceReceipt,
    PartCoreOperation,
)
from vibecad.validation.contracts import EntityObservation
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1

_PLACEMENT = {
    "position_mm": [3.0, 4.0, 5.0],
    "axis": [0.0, 0.0, 1.0],
    "angle_degrees": 30.0,
}
_CONFIGURATIONS = {
    AppFamilyOperation.TEXT_ANNOTATION: {
        "lines": ["reviewed", "annotation"],
        "position_mm": [1.0, 2.0, 3.0],
    },
    AppFamilyOperation.LEADER_ANNOTATION: {
        "lines": ["reviewed leader"],
        "base_position_mm": [1.0, 2.0, 3.0],
        "text_position_mm": [4.0, 5.0, 6.0],
    },
    AppFamilyOperation.DOCUMENT_GROUP: {},
    AppFamilyOperation.OBJECT_LINK: {"placement": _PLACEMENT},
    AppFamilyOperation.LINK_GROUP: {"placement": _PLACEMENT},
    AppFamilyOperation.MATERIAL_DEFINITION: {
        "name": "Reviewed material",
        "description": "Bounded metadata",
        "density_kg_m3": 2700.0,
    },
    AppFamilyOperation.POSITIONED_PART: {"placement": _PLACEMENT},
    AppFamilyOperation.PLACEMENT_REFERENCE: {"placement": _PLACEMENT},
    AppFamilyOperation.TEXT_DOCUMENT: {"text": "Bounded reviewed text"},
    AppFamilyOperation.SCALAR_VARIABLE_SET: {"value": 12.5},
}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _operation_spec(operation: AppFamilyOperation):
    return next(
        item for item in APP_FAMILY_MANIFEST.operations if item.operation_id == operation.value
    )


def _plan_fixture(
    operation: AppFamilyOperation,
) -> tuple[AppFamilyBackendPlan, DocumentRef, ReviewedPlanReceipt]:
    relation = APP_FAMILY_RELATION_KINDS[operation] is not AppFamilyRelationKind.NONE
    plan = AppFamilyBackendPlan(
        source_artifact_id="artifact_app_product_graph",
        source_graph_id=f"graph_app_{operation.value}",
        source_graph_sha256=_sha(f"graph:{operation.value}"),
        source_content_sha256=_sha(f"content:{operation.value}"),
        lowering_request_sha256=_sha(f"request:{operation.value}"),
        adapter_contract_sha256=APP_FAMILY_MANIFEST.adapter.adapter_contract_sha256,
        manifest_sha256=APP_FAMILY_MANIFEST.manifest_sha256,
        container_id="document_space",
        target_node_id="node_target",
        target_result_id="result_target",
        operation=operation,
        configuration_bytes=encode_app_family_configuration(operation, _CONFIGURATIONS[operation]),
        related_node_id="node_related" if relation else None,
        related_result_id="result_related" if relation else None,
    )
    source_document = DocumentRef(
        artifact_id=plan.source_artifact_id,
        role_term_ref_id=APP_FAMILY_MANIFEST.intent_role_term.term_ref_id,
        schema_term_ref_id=APP_FAMILY_MANIFEST.intent_schema_term.term_ref_id,
        document_id=plan.source_graph_id,
        document_digest=plan.source_graph_sha256,
        content_sha256=plan.source_content_sha256,
        size_bytes=123,
        media_type=APP_FAMILY_MANIFEST.intent_media_type,
    )
    plan_document = APP_FAMILY_MANIFEST.plan_document(plan.canonical_bytes, plan.plan_sha256)
    receipt = ReviewedPlanReceipt(
        manifest_sha256=APP_FAMILY_MANIFEST.manifest_sha256,
        request_digest=plan.lowering_request_sha256,
        adapter=APP_FAMILY_MANIFEST.adapter,
        operation=_operation_spec(operation),
        source_document=source_document,
        plan_document=plan_document,
    )
    return plan, plan_document, receipt


def _reviewed_program(operation: AppFamilyOperation) -> ReviewedIntentProgramV1:
    graph = _graph(operation)
    operation_id, semantic_operation = APP_REVIEWED_PRODUCT_IDENTITIES[
        tuple(AppFamilyOperation).index(operation)
    ]
    return ReviewedIntentProgramV1(
        operation_id=operation_id,
        semantic_operation=semantic_operation,
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


class _Vector:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x = x
        self.y = y
        self.z = z

    def __iter__(self):
        return iter((self.x, self.y, self.z))


class _Placement:
    def __init__(self) -> None:
        self.Base = _Vector(3.0, 4.0, 5.0)
        self.Rotation = SimpleNamespace(Q=(0.0, 0.0, 0.2588190451, 0.9659258263))


class _Shape:
    def __init__(self, brep: str) -> None:
        self.brep = brep
        self.Solids = (object(),)
        self.Volume = 10.0

    def isNull(self) -> bool:  # noqa: N802 - FreeCAD API spelling
        return False

    def isValid(self) -> bool:  # noqa: N802 - FreeCAD API spelling
        return True

    def exportBrepToString(self) -> str:
        return self.brep


class _Object:
    def __init__(self, document: _Document, name: str, type_id: str) -> None:
        self.Document = document
        self.Name = name
        self.TypeId = type_id
        self.State = ("Up-to-date",)
        self.ExpressionEngine = ()
        self.Placement = _Placement()
        self._parent = None

    def isValid(self) -> bool:  # noqa: N802 - FreeCAD API spelling
        return True

    def getParentGroup(self):  # noqa: N802 - FreeCAD API spelling
        return self._parent


class _Document:
    def __init__(self) -> None:
        self.Objects: tuple[_Object, ...] = ()

    def getObject(self, name: str):  # noqa: N802 - FreeCAD API spelling
        return next((item for item in self.Objects if item.Name == name), None)


class _Session:
    def __init__(
        self,
        document: _Document,
        identities: dict[_Object, EntityIdentity],
    ) -> None:
        self.doc = document
        self.identities = identities

    def read_object_identity(self, obj: _Object) -> EntityIdentity:
        return self.identities[obj]


class _Sink:
    def __init__(self) -> None:
        self.items: dict[str, tuple[DocumentRef, bytes]] = {}

    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        self.items[document.artifact_id] = (document, payload)
        return payload

    def read_exact(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        stored, payload = self.items[document.artifact_id]
        assert stored == document and len(payload) <= maximum_bytes
        return payload


def _part_helpers(document: _Document, primary: _Object) -> tuple[_Object, ...]:
    origin = _Object(document, f"{primary.Name}_OriginContainer", "App::Origin")
    helpers = tuple(
        _Object(document, f"{primary.Name}_{role}", type_id)
        for role, type_id in (
            ("X_Axis", "App::Line"),
            ("Y_Axis", "App::Line"),
            ("Z_Axis", "App::Line"),
            ("XY_Plane", "App::Plane"),
            ("XZ_Plane", "App::Plane"),
            ("YZ_Plane", "App::Plane"),
            ("Origin", "App::Point"),
        )
    )
    for helper, role in zip(
        helpers,
        ("X_Axis", "Y_Axis", "Z_Axis", "XY_Plane", "XZ_Plane", "YZ_Plane", "Origin"),
        strict=True,
    ):
        helper.Role = role
        helper._parent = origin
    origin.OriginFeatures = helpers
    origin._parent = primary
    primary.Origin = origin
    return (origin, *helpers)


def _created_closure(
    document: _Document,
    operation: AppFamilyOperation,
    related: _Object | None,
) -> tuple[_Object, ...]:
    config = _CONFIGURATIONS[operation]
    primary = _Object(
        document,
        f"Reviewed_{operation.value}",
        APP_FAMILY_NATIVE_TYPE_IDS[operation],
    )
    if operation is AppFamilyOperation.TEXT_ANNOTATION:
        primary.LabelText = list(config["lines"])
        primary.Position = _Vector(*config["position_mm"])
    elif operation is AppFamilyOperation.LEADER_ANNOTATION:
        primary.LabelText = list(config["lines"])
        primary.BasePosition = _Vector(*config["base_position_mm"])
        primary.TextPosition = _Vector(*config["text_position_mm"])
    elif operation is AppFamilyOperation.DOCUMENT_GROUP:
        primary.Group = (related,)
    elif operation is AppFamilyOperation.OBJECT_LINK:
        primary.LinkedObject = related
        primary.LinkTransform = True
        primary.LinkPlacement = primary.Placement
    elif operation is AppFamilyOperation.LINK_GROUP:
        primary.ElementList = (related,)
        primary.LinkMode = "None"
    elif operation is AppFamilyOperation.MATERIAL_DEFINITION:
        primary.Material = {
            "Name": config["name"],
            "Description": config["description"],
            "Density": "2700 kg/m^3",
        }
    elif operation is AppFamilyOperation.POSITIONED_PART:
        primary.Group = (related,)
        return (primary, *_part_helpers(document, primary))
    elif operation is AppFamilyOperation.TEXT_DOCUMENT:
        primary.Text = config["text"]
    elif operation is AppFamilyOperation.SCALAR_VARIABLE_SET:
        primary.Value = config["value"]
        primary.getGroupOfProperty = lambda name: "Variables"
        primary.getTypeIdOfProperty = lambda name: "App::PropertyFloat"
    return (primary,)


def _source_fixture() -> tuple[_Session, ReviewedNativeExecutionResult]:
    document = _Document()
    route = REVIEWED_PART_PRIMITIVE_ROUTES[0]
    source = _Object(document, "ReviewedSource", route.operation.native_type_id)
    source.Shape = _Shape("source-brep")
    document.Objects = (source,)
    identity = EntityIdentity(
        object_id="object_11111111111111111111111111111111",
        feature_id="feature_11111111111111111111111111111111",
        object_type=source.TypeId,
        semantic_role=SemanticRole.PRIMITIVE,
        provenance=Provenance(
            source=ProvenanceSource.MODEL,
            operation_id="create_reviewed_source",
        ),
    )
    receipt = PartCoreConformanceReceipt(
        plan_sha256="1" * 64,
        operation=PartCoreOperation(route.operation.operation_id),
        object_name=source.Name,
        source_shape_sha256s=(),
        result_shape_sha256=_sha("source-brep"),
    )
    result = ReviewedNativeExecutionResult(
        route=route,
        object=source,
        plan_sha256=receipt.plan_sha256,
        plan_content_sha256="2" * 64,
        native_receipt=receipt,
    )
    return _Session(document, {source: identity}), result


def _context(
    session: _Session,
    source_results: tuple[ReviewedNativeExecutionResult, ...],
) -> _ReviewedFamilyExecutionContext:
    run_token = None
    if source_results:
        run_token = object()
        for source in source_results:
            if type(source) is ReviewedNativeExecutionResult:
                source._retain_for_run(  # noqa: SLF001 - same-run product fixture
                    run_token
                )
    return _ReviewedFamilyExecutionContext(
        session=session,
        document=session.doc,
        source_results=source_results,
        run_token=run_token,
    )


def test_app_product_inventory_freezes_ten_create_only_reference_contracts() -> None:
    no_source = tuple(
        item.value
        for item in AppFamilyOperation
        if APP_FAMILY_RELATION_KINDS[item] is AppFamilyRelationKind.NONE
    )
    one_source = tuple(
        item.value
        for item in AppFamilyOperation
        if APP_FAMILY_RELATION_KINDS[item] is not AppFamilyRelationKind.NONE
    )

    assert len(APP_REVIEWED_PRODUCT_CONTRACTS) == len(APP_REVIEWED_PRODUCT_IDENTITIES) == 10
    assert APP_REVIEWED_FAMILY_SPECS == (
        APP_NO_SOURCE_REVIEWED_FAMILY_SPEC,
        APP_ONE_SOURCE_REVIEWED_FAMILY_SPEC,
    )
    assert APP_NO_SOURCE_REVIEWED_FAMILY_SPEC.operation_ids == no_source
    assert APP_ONE_SOURCE_REVIEWED_FAMILY_SPEC.operation_ids == one_source
    assert (len(no_source), len(one_source)) == (6, 4)
    assert all(
        item.result_kind is AppReviewedResultKind.REFERENCE
        and item.minimum_sources == item.maximum_sources
        and item.ownership == "document-root"
        for item in APP_REVIEWED_PRODUCT_CONTRACTS.values()
    )
    part = APP_REVIEWED_PRODUCT_CONTRACTS[AppFamilyOperation.POSITIONED_PART]
    assert part.owned_type_ids == (
        "App::Part",
        "App::Origin",
        "App::Line",
        "App::Line",
        "App::Line",
        "App::Plane",
        "App::Plane",
        "App::Plane",
        "App::Point",
    )
    assert part.semantic_roles == (SemanticRole.PART, *((SemanticRole.SUPPORT,) * 8))
    assert all(
        "local_coordinate" not in item[0] and "link_element" not in item[0]
        for item in APP_REVIEWED_PRODUCT_IDENTITIES
    )
    formal = tuple(
        item
        for item in current_freecad_intent_capability_specs()
        if item.adapter_id == APP_FAMILY_MANIFEST.adapter.adapter_id
    )
    assert tuple(sorted(item.operation_id for item in formal)) == tuple(
        sorted(item[0] for item in APP_REVIEWED_PRODUCT_IDENTITIES)
    )
    assert {item.native_type_id for item in formal} == set(APP_FAMILY_NATIVE_TYPE_IDS.values())


@pytest.mark.parametrize("operation", tuple(AppFamilyOperation))
def test_app_routes_are_exact_full_identity_reference_products_and_lower(
    operation: AppFamilyOperation,
) -> None:
    program = _reviewed_program(operation)
    route = route_reviewed_intent(program)
    lowered = lower_reviewed_intent(program)
    contract = APP_REVIEWED_PRODUCT_CONTRACTS[operation]
    shared_contract = next(
        item for item in route.family.product_results if item.operation_id == operation.value
    )

    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 81
    assert CURRENT_REVIEWED_INTENT_ROUTES[-13:-3] == REVIEWED_APP_ROUTES
    assert REVIEWED_APP_ROUTES == (
        *REVIEWED_APP_NO_SOURCE_ROUTES,
        *REVIEWED_APP_ONE_SOURCE_ROUTES,
    )
    assert route.operation_id == program.operation_id
    assert route.semantic_operation == route.manifest_semantic_operation
    assert lowered.route is route
    assert lowered.plan.operation is operation
    assert shared_contract.result_kind is _ReviewedProductResultKind.REFERENCE
    assert shared_contract.source_count == contract.minimum_sources
    assert shared_contract.owned_type_ids == contract.owned_type_ids
    assert shared_contract.semantic_roles == contract.semantic_roles
    assert shared_contract.requires_state_sha256 is True
    assert route.family.requires_same_run_sources is (contract.minimum_sources == 1)


@pytest.mark.parametrize("operation", tuple(AppFamilyOperation))
def test_app_product_identity_adapter_and_exact_plan_binding(
    operation: AppFamilyOperation,
) -> None:
    plan, _plan_document, receipt = _plan_fixture(operation)
    operation_id, semantic_operation = APP_REVIEWED_PRODUCT_IDENTITIES[
        tuple(AppFamilyOperation).index(operation)
    ]

    resolved = resolve_app_reviewed_operation(operation_id, semantic_operation)
    adapter = next(
        spec for spec in APP_REVIEWED_FAMILY_SPECS if operation.value in spec.operation_ids
    ).adapter_factory(_Sink())

    assert resolved is _operation_spec(operation)
    assert adapter.manifest is APP_FAMILY_MANIFEST
    validate_app_reviewed_plan(plan, receipt, resolved)
    assert b"App::" not in plan.canonical_bytes
    assert b"python" not in plan.canonical_bytes.lower()
    assert b"expression" not in plan.canonical_bytes.lower()
    assert b"path" not in plan.canonical_bytes.lower()


def test_app_product_identity_and_plan_tamper_fail_closed() -> None:
    plan, _plan_document, receipt = _plan_fixture(AppFamilyOperation.TEXT_DOCUMENT)
    operation_id, semantic_operation = APP_REVIEWED_PRODUCT_IDENTITIES[8]

    assert resolve_app_reviewed_operation(operation_id + ".alias", semantic_operation) is None
    assert resolve_app_reviewed_operation(operation_id, semantic_operation + "0") is None
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        validate_app_reviewed_plan(
            plan,
            dataclasses.replace(receipt, request_digest="f" * 64),
            receipt.operation,
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize("operation", tuple(AppFamilyOperation))
def test_app_product_executes_existing_rule_and_closes_reference_adoption(
    operation: AppFamilyOperation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, plan_document, _receipt = _plan_fixture(operation)
    related = APP_FAMILY_RELATION_KINDS[operation] is not AppFamilyRelationKind.NONE
    if related:
        session, source_result = _source_fixture()
        source_results = (source_result,)
        related_object = source_result.object
    else:
        session = _Session(_Document(), {})
        source_results = ()
        related_object = None
    calls: list[AppFamilyExecutionBindings] = []

    def apply(
        raw: bytes,
        *,
        expected_content_sha256: str,
        expected_plan_sha256: str,
        bindings: AppFamilyExecutionBindings,
    ) -> AppFamilyConformanceReceipt:
        assert raw == plan.canonical_bytes
        assert expected_content_sha256 == plan_document.content_sha256
        assert expected_plan_sha256 == plan_document.document_digest
        calls.append(bindings)
        owned = _created_closure(session.doc, operation, related_object)
        session.doc.Objects = (*session.doc.Objects, *owned)
        return AppFamilyConformanceReceipt(
            plan_sha256=plan.plan_sha256,
            operation=operation,
            object_name=owned[0].Name,
            native_type_id=owned[0].TypeId,
            owned_object_names=tuple(item.Name for item in owned),
            related_object_name=None if related_object is None else related_object.Name,
        )

    monkeypatch.setattr(app_execution, "apply_app_family_plan", apply)

    result = execute_app_reviewed_plan(
        session.doc,
        plan,
        plan.canonical_bytes,
        plan_document,
        _operation_spec(operation),
        _context(session, source_results),
    )

    contract = APP_REVIEWED_PRODUCT_CONTRACTS[operation]
    assert len(calls) == 1
    assert calls[0].related_object is related_object
    assert tuple(item.TypeId for item in result.owned_objects) == contract.owned_type_ids
    assert type(result.receipt) is AppReviewedProductReceipt
    assert result.state_sha256 == result.receipt.state_sha256
    result.receipt.validate_current(session.doc, result.object, result.owned_objects)
    observation = EntityObservation(
        object_id="object_22222222222222222222222222222222",
        feature_id="feature_22222222222222222222222222222222",
        object_type=contract.owned_type_ids[0],
        semantic_role=contract.semantic_roles[0].value,
        provenance={"source": "model", "operation_id": "apply_reviewed_intent"},
        placement=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
    )
    result.receipt.validate_adoption(session.doc, result.object, observation)


@pytest.mark.parametrize("source_count", (0, 2))
def test_relation_binding_requires_exactly_one_authenticated_source(
    source_count: int,
) -> None:
    session, source = _source_fixture()
    plan, _plan_document, _receipt = _plan_fixture(AppFamilyOperation.DOCUMENT_GROUP)
    sources = () if source_count == 0 else (source, source)

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        build_app_reviewed_bindings(
            session.doc,
            plan,
            _operation_spec(plan.operation),
            _context(session, sources),
        )

    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


def test_relation_binding_rejects_a_result_retained_for_another_run() -> None:
    session, source = _source_fixture()
    source._retain_for_run(object())  # noqa: SLF001 - deliberate cross-run fixture
    plan, _plan_document, _receipt = _plan_fixture(AppFamilyOperation.OBJECT_LINK)
    context = _ReviewedFamilyExecutionContext(
        session=session,
        document=session.doc,
        source_results=(source,),
        run_token=object(),
    )

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        build_app_reviewed_bindings(
            session.doc,
            plan,
            _operation_spec(plan.operation),
            context,
        )

    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize(
    "failure",
    ("cross_document", "stale", "wrong_provenance", "unknown", "wrong_route"),
)
def test_invalid_related_product_is_rejected_before_mutation(failure: str) -> None:
    session, source = _source_fixture()
    plan, _plan_document, _receipt = _plan_fixture(AppFamilyOperation.OBJECT_LINK)
    before = tuple(session.doc.Objects)
    if failure == "cross_document":
        source.object.Document = _Document()
    elif failure == "stale":
        source.object.Shape.brep = "mutated-brep"
    elif failure == "wrong_provenance":
        session.identities[source.object] = dataclasses.replace(
            session.identities[source.object],
            provenance=Provenance(source=ProvenanceSource.IMPORTED, operation_id=None),
        )
    elif failure == "unknown":
        del session.identities[source.object]
    else:
        source = SimpleNamespace(
            route=SimpleNamespace(operation=source.route.operation),
            object=source.object,
        )

    with pytest.raises((ReviewedIntentExecutionError, TypeError)) as caught:
        build_app_reviewed_bindings(
            session.doc,
            plan,
            _operation_spec(plan.operation),
            _context(session, (source,)),
        )

    if isinstance(caught.value, ReviewedIntentExecutionError):
        assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert tuple(session.doc.Objects) == before


def test_app_product_receipt_rejects_post_create_state_and_closure_tamper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = AppFamilyOperation.POSITIONED_PART
    session, source = _source_fixture()
    plan, plan_document, _receipt = _plan_fixture(operation)

    def apply(
        raw: bytes,
        *,
        expected_content_sha256: str,
        expected_plan_sha256: str,
        bindings: AppFamilyExecutionBindings,
    ) -> AppFamilyConformanceReceipt:
        owned = _created_closure(session.doc, operation, source.object)
        session.doc.Objects = (*session.doc.Objects, *owned)
        return AppFamilyConformanceReceipt(
            plan_sha256=expected_plan_sha256,
            operation=operation,
            object_name=owned[0].Name,
            native_type_id=owned[0].TypeId,
            owned_object_names=tuple(item.Name for item in owned),
            related_object_name=source.object.Name,
        )

    monkeypatch.setattr(app_execution, "apply_app_family_plan", apply)
    result = execute_app_reviewed_plan(
        session.doc,
        plan,
        plan.canonical_bytes,
        plan_document,
        _operation_spec(operation),
        _context(session, (source,)),
    )
    result.owned_objects[-1].Role = "tampered"

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        result.receipt.validate_current(session.doc, result.object, result.owned_objects)

    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert CURRENT_REVIEWED_INTENT_ROUTES
