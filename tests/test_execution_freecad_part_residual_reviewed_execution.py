from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass

import pytest

import vibecad.execution.freecad_part_residual_reviewed_execution as residual_execution
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_part_a_verification import (
    PART_CORE_REVIEWED_HOST_CASE_MANIFEST,
)
from vibecad.execution.freecad_part_residual_reviewed_execution import (
    PART_RESIDUAL_PRODUCT_CONTRACTS,
    PART_RESIDUAL_REVIEWED_FAMILY_SPEC,
    PART_RESIDUAL_REVIEWED_PRODUCT_IDENTITIES,
    PART_RESIDUAL_REVIEWED_PRODUCT_OPERATIONS,
    PartResidualOwnershipClosure,
    PartResidualProductResultKind,
    build_part_residual_reviewed_bindings,
    build_part_residual_reviewed_family_descriptor,
    execute_part_residual_reviewed_plan,
    resolve_part_residual_reviewed_operation,
    validate_part_residual_bindings_current,
    validate_part_residual_reviewed_plan,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PART_PRIMITIVE_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    ReviewedNativeExecutionResult,
    _ReviewedFamilyExecutionContext,
)
from vibecad.execution.selectors import (
    EntityIdentity,
    Provenance,
    ProvenanceSource,
    SemanticRole,
)
from vibecad.intent_bridge.contracts import DocumentRef
from vibecad.intent_bridge.freecad_part_core_adapter import PART_CORE_MANIFEST
from vibecad.intent_bridge.reviewed_family_engine import ReviewedPlanReceipt
from vibecad.parametric.freecad_part_core_rules import (
    PART_CORE_NATIVE_SPECS,
    PartCoreBackendPlan,
    PartCoreConformanceReceipt,
    PartCoreOperation,
    PartCoreParameterSet,
    PartCoreSelection,
)

_PARAMETERS = {
    PartCoreOperation.SECTION: {},
    PartCoreOperation.MULTI_FUSE: {},
    PartCoreOperation.MULTI_COMMON: {},
    PartCoreOperation.COMPOUND: {},
    PartCoreOperation.MIRROR: {
        "base_point_mm": [0.0, 0.0, 0.0],
        "normal": [1.0, 0.0, 0.0],
    },
    PartCoreOperation.SCALE: {"scale_xyz": [2.0, 2.0, 2.0]},
    PartCoreOperation.REVERSE: {},
    PartCoreOperation.REFINE: {},
}


def _operation(operation: PartCoreOperation):
    return next(
        item for item in PART_CORE_MANIFEST.operations if item.operation_id == operation.value
    )


def _source_document(operation: PartCoreOperation) -> DocumentRef:
    return DocumentRef(
        artifact_id=f"artifact_residual_{operation.value}",
        role_term_ref_id=PART_CORE_MANIFEST.intent_role_term.term_ref_id,
        schema_term_ref_id=PART_CORE_MANIFEST.intent_schema_term.term_ref_id,
        document_id=f"graph_residual_{operation.value}",
        document_digest=hashlib.sha256(f"graph:{operation.value}".encode()).hexdigest(),
        content_sha256=hashlib.sha256(f"content:{operation.value}".encode()).hexdigest(),
        size_bytes=128,
        media_type=PART_CORE_MANIFEST.intent_media_type,
    )


def _plan(
    operation: PartCoreOperation,
    source_count: int | None = None,
) -> tuple[PartCoreBackendPlan, ReviewedPlanReceipt]:
    contract = PART_RESIDUAL_PRODUCT_CONTRACTS[operation]
    count = contract.minimum_sources if source_count is None else source_count
    reviewed = _operation(operation)
    source = _source_document(operation)
    request_sha256 = hashlib.sha256(f"request:{operation.value}:{count}".encode()).hexdigest()
    plan = PartCoreBackendPlan(
        source_artifact_id=source.artifact_id,
        source_graph_id=source.document_id,
        source_graph_sha256=source.document_digest,
        source_content_sha256=source.content_sha256,
        lowering_request_sha256=request_sha256,
        adapter_contract_sha256=PART_CORE_MANIFEST.adapter.adapter_contract_sha256,
        manifest_sha256=PART_CORE_MANIFEST.manifest_sha256,
        operation_specification_sha256=reviewed.specification_sha256,
        body_id="body_main",
        target=PartCoreSelection(node_id="node_target", result_id="result_target"),
        operation=operation,
        sources=tuple(
            PartCoreSelection(node_id=f"node_source_{index}", result_id=f"result_source_{index}")
            for index in range(count)
        ),
        parameters=PartCoreParameterSet.from_value(operation, _PARAMETERS[operation]),
    )
    return plan, ReviewedPlanReceipt(
        manifest_sha256=PART_CORE_MANIFEST.manifest_sha256,
        request_digest=request_sha256,
        adapter=PART_CORE_MANIFEST.adapter,
        operation=reviewed,
        source_document=source,
        plan_document=PART_CORE_MANIFEST.plan_document(plan.canonical_bytes, plan.plan_sha256),
    )


class _Shape:
    def __init__(
        self,
        brep: str,
        *,
        volume: float = 1.0,
        solids: int = 1,
        edges: int = 12,
        shape_type: str = "Solid",
        children: int = 0,
    ) -> None:
        self.brep = brep
        self.Volume = volume
        self.Solids = tuple(object() for _ in range(solids))
        self.Edges = tuple(object() for _ in range(edges))
        self.ShapeType = shape_type
        self._children = tuple(object() for _ in range(children))

    def isNull(self) -> bool:  # noqa: N802
        return False

    def isValid(self) -> bool:  # noqa: N802
        return True

    def exportBrepToString(self) -> str:
        return self.brep

    def childShapes(self) -> tuple[object, ...]:  # noqa: N802
        return self._children


class _Object:
    def __init__(
        self,
        document: _Document,
        *,
        name: str,
        type_id: str,
        shape: _Shape,
    ) -> None:
        self.Document = document
        self.Name = name
        self.TypeId = type_id
        self.Shape = shape
        self.State = ("Up-to-date",)

    def isValid(self) -> bool:  # noqa: N802
        return True


class _Document:
    def __init__(self) -> None:
        self.Objects: tuple[_Object, ...] = ()

    def getObject(self, name: str) -> _Object | None:  # noqa: N802
        return next((item for item in self.Objects if item.Name == name), None)


class _Session:
    def __init__(self, document: _Document, identities: dict[_Object, EntityIdentity]) -> None:
        self.doc = document
        self.identities = identities

    def read_object_identity(self, item: _Object) -> EntityIdentity:
        return self.identities[item]


def _shape_sha256(item: _Object) -> str:
    return hashlib.sha256(item.Shape.exportBrepToString().encode()).hexdigest()


def _identity(index: int, type_id: str) -> EntityIdentity:
    return EntityIdentity(
        object_id=f"object_{index + 1:032x}",
        feature_id=f"feature_{index + 1:032x}",
        object_type=type_id,
        semantic_role=SemanticRole.PRIMITIVE,
        provenance=Provenance(
            source=ProvenanceSource.MODEL,
            operation_id="apply_reviewed_intent",
        ),
    )


@dataclass(frozen=True)
class _SourceFixture:
    session: _Session
    results: tuple[ReviewedNativeExecutionResult, ...]
    token: object

    def context(self, results: tuple[ReviewedNativeExecutionResult, ...] | None = None):
        return _ReviewedFamilyExecutionContext(
            session=self.session,
            document=self.session.doc,
            source_results=self.results if results is None else results,
            run_token=self.token,
        )


def _source_fixture(count: int) -> _SourceFixture:
    document = _Document()
    routes = REVIEWED_PART_PRIMITIVE_ROUTES[:count]
    objects = tuple(
        _Object(
            document,
            name=f"Source{index}",
            type_id=route.operation.native_type_id,
            shape=_Shape(f"source-shape-{index}", volume=float(index + 1)),
        )
        for index, route in enumerate(routes)
    )
    document.Objects = objects
    session = _Session(
        document,
        {item: _identity(index, item.TypeId) for index, item in enumerate(objects)},
    )
    results = tuple(
        ReviewedNativeExecutionResult(
            route=route,
            object=item,
            plan_sha256=hashlib.sha256(f"source-plan:{index}".encode()).hexdigest(),
            plan_content_sha256=hashlib.sha256(f"source-content:{index}".encode()).hexdigest(),
            native_receipt=PartCoreConformanceReceipt(
                plan_sha256=hashlib.sha256(f"source-plan:{index}".encode()).hexdigest(),
                operation=PartCoreOperation(route.operation.operation_id),
                object_name=item.Name,
                source_shape_sha256s=(),
                result_shape_sha256=_shape_sha256(item),
            ),
        )
        for index, (item, route) in enumerate(zip(objects, routes, strict=True))
    )
    token = object()
    for result in results:
        result._retain_for_run(token)
    return _SourceFixture(session=session, results=results, token=token)


def test_residual_family_is_exact_unregistered_and_truthful_about_wire_limit() -> None:
    assert len(PART_RESIDUAL_REVIEWED_PRODUCT_OPERATIONS) == 8
    assert len(PART_RESIDUAL_REVIEWED_PRODUCT_IDENTITIES) == 8
    assert PART_RESIDUAL_REVIEWED_FAMILY_SPEC.manifest is PART_CORE_MANIFEST
    assert PART_RESIDUAL_REVIEWED_FAMILY_SPEC.operation_ids == tuple(
        item.value for item in PART_RESIDUAL_REVIEWED_PRODUCT_OPERATIONS
    )
    assert (
        PART_RESIDUAL_REVIEWED_FAMILY_SPEC.minimum_sources,
        PART_RESIDUAL_REVIEWED_FAMILY_SPEC.maximum_sources,
    ) == (1, 8)
    assert not {
        f"{PART_CORE_MANIFEST.family_id}.{item.value}"
        for item in PART_RESIDUAL_REVIEWED_PRODUCT_OPERATIONS
    }.intersection(item.operation_id for item in CURRENT_REVIEWED_INTENT_ROUTES)

    descriptor = build_part_residual_reviewed_family_descriptor()
    assert descriptor.manifest is PART_CORE_MANIFEST
    assert descriptor.requires_same_run_sources is True
    assert (descriptor.minimum_sources, descriptor.maximum_sources) == (1, 8)
    assert len(descriptor.product_results) == 26
    assert {
        item.operation_id
        for item in descriptor.product_results
        if item.result_kind.value == "valid_shape"
    } == {"section", "compound", "reverse"}
    for identity in PART_RESIDUAL_REVIEWED_PRODUCT_IDENTITIES:
        assert resolve_part_residual_reviewed_operation(*identity) is not None
    assert (
        resolve_part_residual_reviewed_operation("freecad_part_core.box", "operation.part-box")
        is None
    )

    formal = {item.operation_id: item for item in current_freecad_intent_capability_specs()}
    residual_operation_ids = {
        f"{PART_CORE_MANIFEST.family_id}.{item.value}"
        for item in PART_RESIDUAL_REVIEWED_PRODUCT_OPERATIONS
    }
    host_case_operation_ids = {
        item.operation_id for item in PART_CORE_REVIEWED_HOST_CASE_MANIFEST.cases
    }
    assert residual_operation_ids == residual_operation_ids.intersection(formal)
    assert {item.value for item in PART_RESIDUAL_REVIEWED_PRODUCT_OPERATIONS}.issubset(
        host_case_operation_ids
    )
    for operation in PART_RESIDUAL_REVIEWED_PRODUCT_OPERATIONS:
        spec = formal[f"{PART_CORE_MANIFEST.family_id}.{operation.value}"]
        assert spec.native_type_id == PART_CORE_NATIVE_SPECS[operation].type_id
        assert spec.adapter_id == PART_CORE_MANIFEST.adapter.adapter_id
        assert spec.adapter_version == PART_CORE_MANIFEST.adapter.adapter_version
        assert spec.adapter_contract_sha256 == PART_CORE_MANIFEST.adapter.adapter_contract_sha256
        assert spec.rule_id == PART_CORE_MANIFEST.rule_id
        assert spec.rule_contract_sha256 == PART_CORE_MANIFEST.rule_contract_sha256


@pytest.mark.parametrize("operation", PART_RESIDUAL_REVIEWED_PRODUCT_OPERATIONS)
def test_residual_plan_validation_reuses_exact_part_core_contract(
    operation: PartCoreOperation,
) -> None:
    plan, receipt = _plan(operation)

    validate_part_residual_reviewed_plan(plan, receipt, _operation(operation))

    contract = PART_RESIDUAL_PRODUCT_CONTRACTS[operation]
    native = PART_CORE_NATIVE_SPECS[operation]
    assert contract.native_type_id == native.type_id
    assert contract.minimum_sources == native.minimum_sources
    assert contract.maximum_sources == min(native.maximum_sources, 8)
    assert tuple((item.node_id, item.result_id) for item in plan.sources) == tuple(
        (f"node_source_{index}", f"result_source_{index}")
        for index in range(contract.minimum_sources)
    )


@pytest.mark.parametrize(
    "operation",
    (PartCoreOperation.MULTI_FUSE, PartCoreOperation.MULTI_COMMON, PartCoreOperation.COMPOUND),
)
def test_aggregate_n_plus_one_plan_is_rejected_before_product_execution(
    operation: PartCoreOperation,
) -> None:
    plan, receipt = _plan(operation, 9)

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        validate_part_residual_reviewed_plan(plan, receipt, _operation(operation))

    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


def test_ordered_sources_bind_without_sorting_and_include_state_digests() -> None:
    plan, _receipt = _plan(PartCoreOperation.MULTI_FUSE, 3)
    fixture = _source_fixture(3)
    reordered = (fixture.results[2], fixture.results[0], fixture.results[1])

    bindings = build_part_residual_reviewed_bindings(
        fixture.session.doc,
        plan,
        _operation(plan.operation),
        fixture.context(reordered),
    )

    assert tuple(item.object for item in bindings.execution.sources) == tuple(
        item.object for item in reordered
    )
    assert tuple((item.node_id, item.result_id) for item in bindings.execution.sources) == (
        ("node_source_0", "result_source_0"),
        ("node_source_1", "result_source_1"),
        ("node_source_2", "result_source_2"),
    )
    assert bindings.source_result_plan_sha256s == tuple(item.plan_sha256 for item in reordered)
    assert len(set(bindings.source_identity_sha256s)) == 3
    assert len(set(bindings.source_state_sha256s)) == 3


def test_aggregate_wire_maximum_eight_sources_is_executable() -> None:
    plan, receipt = _plan(PartCoreOperation.COMPOUND, 8)
    fixture = _source_fixture(8)

    validate_part_residual_reviewed_plan(plan, receipt, _operation(plan.operation))
    bindings = build_part_residual_reviewed_bindings(
        fixture.session.doc,
        plan,
        _operation(plan.operation),
        fixture.context(),
    )

    assert len(bindings.execution.sources) == 8
    assert tuple(item.object for item in bindings.execution.sources) == tuple(
        item.object for item in fixture.results
    )


@pytest.mark.parametrize(
    "failure",
    (
        "n_minus_one",
        "duplicate",
        "stale",
        "tamper_state",
        "wrong_role",
        "wrong_provenance",
        "cross_run",
    ),
)
def test_invalid_sources_fail_closed_before_mutation(failure: str) -> None:
    plan, _receipt = _plan(PartCoreOperation.SECTION)
    fixture = _source_fixture(2)
    results = fixture.results
    source = results[-1].object
    context = fixture.context()
    before = tuple(fixture.session.doc.Objects)
    if failure == "n_minus_one":
        context = fixture.context(results[:1])
    elif failure == "duplicate":
        context = fixture.context((results[0], results[0]))
    elif failure == "stale":
        source.Shape.brep = "tampered-brep"
    elif failure == "tamper_state":
        source.State = ("Touched",)
    elif failure == "wrong_role":
        fixture.session.identities[source] = dataclasses.replace(
            fixture.session.identities[source], semantic_role=SemanticRole.FEATURE
        )
    elif failure == "wrong_provenance":
        fixture.session.identities[source] = dataclasses.replace(
            fixture.session.identities[source],
            provenance=Provenance(source=ProvenanceSource.IMPORTED, operation_id=None),
        )
    else:
        context = _ReviewedFamilyExecutionContext(
            session=fixture.session,
            document=fixture.session.doc,
            source_results=results,
            run_token=object(),
        )

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        build_part_residual_reviewed_bindings(
            fixture.session.doc,
            plan,
            _operation(plan.operation),
            context,
        )

    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert tuple(fixture.session.doc.Objects) == before


@pytest.mark.parametrize("tamper", ("shape", "state", "identity"))
def test_pre_mutation_revalidation_rejects_post_binding_tamper(tamper: str) -> None:
    plan, _receipt = _plan(PartCoreOperation.MIRROR)
    fixture = _source_fixture(1)
    context = fixture.context()
    bindings = build_part_residual_reviewed_bindings(
        fixture.session.doc, plan, _operation(plan.operation), context
    )
    source = fixture.results[0].object
    before = tuple(fixture.session.doc.Objects)
    if tamper == "shape":
        source.Shape.brep = "changed-after-binding"
    elif tamper == "state":
        source.State = ("Touched",)
    else:
        fixture.session.identities[source] = dataclasses.replace(
            fixture.session.identities[source],
            object_id="object_ffffffffffffffffffffffffffffffff",
        )

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        validate_part_residual_bindings_current(
            fixture.session.doc,
            plan,
            _operation(plan.operation),
            context,
            bindings,
        )

    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert tuple(fixture.session.doc.Objects) == before


def _result_shape(operation: PartCoreOperation, source_count: int) -> _Shape:
    if operation is PartCoreOperation.SECTION:
        return _Shape("section-result", volume=0.0, solids=0, edges=4, shape_type="Compound")
    if operation is PartCoreOperation.COMPOUND:
        return _Shape(
            "compound-result",
            volume=2.0,
            solids=2,
            shape_type="Compound",
            children=source_count,
        )
    if operation is PartCoreOperation.REVERSE:
        return _Shape("reverse-result", volume=-1.0)
    return _Shape(f"{operation.value}-result", volume=5.0)


@pytest.mark.parametrize("operation", PART_RESIDUAL_REVIEWED_PRODUCT_OPERATIONS)
def test_residual_operations_return_exact_singleton_ownership_closure(
    operation: PartCoreOperation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, receipt = _plan(operation)
    count = len(plan.sources)
    fixture = _source_fixture(count)
    calls = []

    def apply(
        raw: bytes,
        *,
        expected_content_sha256: str,
        expected_plan_sha256: str,
        bindings,
    ) -> PartCoreConformanceReceipt:
        assert raw == plan.canonical_bytes
        assert expected_content_sha256 == receipt.plan_document.content_sha256
        assert expected_plan_sha256 == plan.plan_sha256
        calls.append(bindings)
        result = _Object(
            fixture.session.doc,
            name=f"Residual{operation.value}",
            type_id=PART_CORE_NATIVE_SPECS[operation].type_id,
            shape=_result_shape(operation, count),
        )
        fixture.session.doc.Objects = (*fixture.session.doc.Objects, result)
        return PartCoreConformanceReceipt(
            plan_sha256=plan.plan_sha256,
            operation=operation,
            object_name=result.Name,
            source_shape_sha256s=tuple(_shape_sha256(item.object) for item in fixture.results),
            result_shape_sha256=_shape_sha256(result),
        )

    monkeypatch.setattr(residual_execution, "apply_part_core_plan", apply)

    execution = execute_part_residual_reviewed_plan(
        fixture.session.doc,
        plan,
        plan.canonical_bytes,
        receipt.plan_document,
        _operation(operation),
        fixture.context(),
    )

    assert len(calls) == 1
    assert execution.object is fixture.session.doc.Objects[-1]
    assert type(execution.receipt) is PartResidualOwnershipClosure
    assert execution.receipt.operation is operation
    assert execution.receipt.plan_sha256 == plan.plan_sha256
    assert execution.receipt.plan_content_sha256 == receipt.plan_document.content_sha256
    assert execution.receipt.semantic_role is SemanticRole.FEATURE
    assert (
        execution.receipt.invariant.contract.result_kind
        is PART_RESIDUAL_PRODUCT_CONTRACTS[operation].result_kind
    )
    assert (
        operation is PartCoreOperation.REVERSE
        and execution.receipt.invariant.contract.result_kind
        is PartResidualProductResultKind.VALID_SHAPE
    ) or operation is not PartCoreOperation.REVERSE
