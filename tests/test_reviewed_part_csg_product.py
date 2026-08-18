from __future__ import annotations

import dataclasses
import hashlib
import pickle
import sys
from types import SimpleNamespace

import pytest

import vibecad.execution.executor as executor_module
import vibecad.execution.freecad_reviewed_intent_execution as reviewed_execution
import vibecad.execution.freecad_reviewed_part_csg_execution as csg_execution
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PART_CSG_ROUTES,
    REVIEWED_PART_PRIMITIVE_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    ReviewedNativeExecutionResult,
    _ReviewedFamilyExecutionContext,
    execute_reviewed_intent_native,
    lower_reviewed_intent,
    route_reviewed_intent,
)
from vibecad.execution.freecad_reviewed_part_csg_execution import (
    build_part_csg_reviewed_bindings,
)
from vibecad.execution.selectors import (
    EntityIdentity,
    Provenance,
    ProvenanceSource,
    SemanticRole,
)
from vibecad.intent_bridge.freecad_part_core_adapter import (
    PART_CORE_CANONICAL_JSON_TERM,
    PART_CORE_OPERATION_SPECS,
    PART_CORE_OPERATION_TERMS,
    PART_CORE_PARAMETERS_ROLE_TERM,
    PART_CORE_PARAMETERS_TYPE_TERM,
    PART_CORE_PFG_TERMS,
    PART_CORE_RESULT_ROLE_TERM,
    PART_CORE_SHAPE_TYPE_TERM,
    PART_CORE_SOURCE_FAMILY_TERM,
    PART_CORE_SOURCE_OPERATION_TERM,
    PART_CORE_SOURCE_ROLE_TERM,
    PART_CORE_SOURCE_STRUCTURE_TERM,
    PART_CORE_STRUCTURE_TERM,
)
from vibecad.parametric.feature_graph_v2 import (
    DesignParameterV2,
    FeatureBodyV2,
    FeatureDependencyV2,
    FeatureGraphResultV2,
    FeatureInputPortV2,
    FeatureIntentV2,
    FeatureNodeV2,
    FeatureParameterBindingV2,
    FeatureResultV2,
    ParametricFeatureGraphV2,
    TermTypedValueV2,
)
from vibecad.parametric.freecad_part_core_rules import (
    PartCoreConformanceReceipt,
    PartCoreExecutionBindings,
    PartCoreOperation,
)
from vibecad.workflow.contracts import AcceptanceSpec, ModelCommand, ModelProgram, ValueSource
from vibecad.workflow.program import (
    BoundResultRef,
    ProgramErrorCode,
    ProgramValidationError,
    validate_model_program,
)
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1


def _semantic_operation(operation: PartCoreOperation) -> str:
    spec = next(item for item in PART_CORE_OPERATION_SPECS if item.operation_id == operation.value)
    namespace, version, term_id, digest = spec.semantic_term.semantic_identity
    return f"{namespace}/{version}/{term_id}@{digest}"


def _source_node(index: int) -> FeatureNodeV2:
    return FeatureNodeV2(
        node_id=f"node_source_{index}",
        body_id="body_main",
        name=f"Authenticated product source {index}",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=PART_CORE_SOURCE_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=PART_CORE_SOURCE_FAMILY_TERM.term_ref_id,
            operation_term_ref_id=PART_CORE_SOURCE_OPERATION_TERM.term_ref_id,
        ),
        results=(
            FeatureResultV2(
                result_id=f"result_source_{index}",
                semantic_role_term_ref_id=PART_CORE_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=PART_CORE_SHAPE_TYPE_TERM.term_ref_id,
            ),
        ),
    )


def reviewed_csg_program(operation: PartCoreOperation) -> ReviewedIntentProgramV1:
    sources = (_source_node(0), _source_node(1))
    operation_terms = next(
        item for item in PART_CORE_OPERATION_TERMS if item.operation is operation
    )
    parameter = DesignParameterV2(
        parameter_id="parameter_target",
        name="Reviewed CSG parameters",
        semantic_role_term_ref_id=PART_CORE_PARAMETERS_ROLE_TERM.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id="value_target",
            value_type_term_ref_id=PART_CORE_PARAMETERS_TYPE_TERM.term_ref_id,
            encoding_term_ref_id=PART_CORE_CANONICAL_JSON_TERM.term_ref_id,
            value={},
        ),
    )
    target = FeatureNodeV2(
        node_id="node_target",
        body_id="body_main",
        name=f"Reviewed {operation.value}",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=PART_CORE_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=operation_terms.family_term.term_ref_id,
            operation_term_ref_id=operation_terms.operation_term.term_ref_id,
            input_ports=(
                FeatureInputPortV2(
                    port_id="port_sources",
                    semantic_role_term_ref_id=PART_CORE_SOURCE_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=PART_CORE_SHAPE_TYPE_TERM.term_ref_id,
                    minimum_cardinality=2,
                    maximum_cardinality=2,
                    ordered=True,
                ),
                FeatureInputPortV2(
                    port_id="port_parameters",
                    semantic_role_term_ref_id=PART_CORE_PARAMETERS_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=PART_CORE_PARAMETERS_TYPE_TERM.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
            ),
            dependencies=tuple(
                FeatureDependencyV2(
                    dependency_id=f"dependency_source_{index}",
                    port_id="port_sources",
                    upstream_node_id=source.node_id,
                    upstream_result_id=source.results[0].result_id,
                    ordinal=index,
                )
                for index, source in enumerate(sources)
            ),
            parameter_bindings=(
                FeatureParameterBindingV2(
                    binding_id="binding_parameters",
                    port_id="port_parameters",
                    parameter_id=parameter.parameter_id,
                ),
            ),
        ),
        results=(
            FeatureResultV2(
                result_id="result_target",
                semantic_role_term_ref_id=PART_CORE_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=PART_CORE_SHAPE_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    graph = ParametricFeatureGraphV2(
        graph_id=f"graph_reviewed_{operation.value}",
        name=f"Reviewed product {operation.value}",
        terms=PART_CORE_PFG_TERMS,
        bodies=(FeatureBodyV2(body_id="body_main", name="Main body"),),
        parameters=(parameter,),
        references=(),
        nodes=(*sources, target),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection_target",
                node_id=target.node_id,
                result_id=target.results[0].result_id,
            ),
        ),
    )
    return ReviewedIntentProgramV1(
        operation_id=f"freecad_part_core.{operation.value}",
        semantic_operation=_semantic_operation(operation),
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


class _Shape:
    def __init__(self, brep: str) -> None:
        self.brep = brep
        self.Solids = (object(),)
        self.Volume = 1.0

    def isNull(self) -> bool:  # noqa: N802 - FreeCAD API spelling
        return False

    def isValid(self) -> bool:  # noqa: N802 - FreeCAD API spelling
        return True

    def exportBrepToString(self) -> str:
        return self.brep


class _Object:
    def __init__(self, document: _Document, *, name: str, type_id: str, brep: str) -> None:
        self.Document = document
        self.Name = name
        self.TypeId = type_id
        self.Shape = _Shape(brep)


class _Document:
    def __init__(self) -> None:
        self.Objects: tuple[_Object, ...] = ()

    def getObject(self, name: str) -> _Object | None:
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


def _shape_sha256(obj: _Object) -> str:
    return hashlib.sha256(obj.Shape.exportBrepToString().encode()).hexdigest()


def _identity(index: int, type_id: str) -> EntityIdentity:
    return EntityIdentity(
        object_id=f"object_{index + 1:032x}",
        feature_id=f"feature_{index + 1:032x}",
        object_type=type_id,
        semantic_role=SemanticRole.PRIMITIVE,
        provenance=Provenance(
            source=ProvenanceSource.MODEL,
            operation_id=f"reviewed_source_{index}",
        ),
    )


def _source_fixture() -> tuple[
    _Session,
    tuple[ReviewedNativeExecutionResult, ReviewedNativeExecutionResult],
]:
    document = _Document()
    objects = tuple(
        _Object(
            document,
            name=f"Source{index}",
            type_id=route.operation.native_type_id,
            brep=f"shape-{index}",
        )
        for index, route in enumerate(REVIEWED_PART_PRIMITIVE_ROUTES[:2])
    )
    document.Objects = objects
    identities = {obj: _identity(index, obj.TypeId) for index, obj in enumerate(objects)}
    results = tuple(
        ReviewedNativeExecutionResult(
            route=route,
            object=obj,
            plan_sha256=f"{index + 1:x}" * 64,
            plan_content_sha256=f"{index + 3:x}" * 64,
            native_receipt=PartCoreConformanceReceipt(
                plan_sha256=f"{index + 1:x}" * 64,
                operation=PartCoreOperation(route.operation.operation_id),
                object_name=obj.Name,
                source_shape_sha256s=(),
                result_shape_sha256=_shape_sha256(obj),
            ),
        )
        for index, (obj, route) in enumerate(
            zip(objects, REVIEWED_PART_PRIMITIVE_ROUTES[:2], strict=True)
        )
    )
    assert len(results) == 2
    return _Session(document, identities), (results[0], results[1])


@pytest.mark.parametrize(
    "operation",
    (PartCoreOperation.CUT, PartCoreOperation.FUSE, PartCoreOperation.COMMON),
)
def test_reviewed_part_csg_routes_lower_two_exact_pfg_sources(
    operation: PartCoreOperation,
) -> None:
    program = reviewed_csg_program(operation)

    route = route_reviewed_intent(program)
    lowered = lower_reviewed_intent(program)

    assert route.operation.operation_id == operation.value
    assert lowered.route is route
    assert lowered.plan.operation is operation
    assert tuple((item.node_id, item.result_id) for item in lowered.plan.sources) == (
        ("node_source_0", "result_source_0"),
        ("node_source_1", "result_source_1"),
    )
    assert lowered.result.plan_document.document_digest == lowered.plan.plan_sha256


def test_reviewed_part_csg_route_table_is_exact_and_closed() -> None:
    programs = tuple(
        reviewed_csg_program(operation)
        for operation in (PartCoreOperation.CUT, PartCoreOperation.FUSE, PartCoreOperation.COMMON)
    )

    assert tuple(route_reviewed_intent(item) for item in programs) == (REVIEWED_PART_CSG_ROUTES)
    assert CURRENT_REVIEWED_INTENT_ROUTES[17:20] == REVIEWED_PART_CSG_ROUTES
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 24
    assert all(
        route.family.product_result(route.operation).result_kind.value == "solid"
        and route.family.product_result(route.operation).owned_type_ids
        == (route.operation.native_type_id,)
        for route in REVIEWED_PART_CSG_ROUTES
    )
    assert {item.operation.native_type_id for item in REVIEWED_PART_CSG_ROUTES} == {
        "Part::Cut",
        "Part::Fuse",
        "Part::Common",
    }


@pytest.mark.parametrize("source_count", (0, 1))
def test_shared_csg_descriptor_rejects_non_exact_source_count(source_count: int) -> None:
    session, source_results = _source_fixture()

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_reviewed_intent_native(
            session,
            reviewed_csg_program(PartCoreOperation.CUT),
            source_results=source_results[:source_count],
        )

    assert caught.value.code is ReviewedIntentExecutionErrorCode.INVALID_INPUT


def test_reviewed_csg_sources_are_two_optional_object_result_refs() -> None:
    program = reviewed_csg_program(PartCoreOperation.CUT)
    model_program = ModelProgram(
        task_id="task_reviewed_csg_refs",
        base_revision="revision_0123456789abcdef0123456789abcdef",
        operations=(
            ModelCommand(
                id="source_a",
                op="create_box",
                args={"length_mm": 10, "width_mm": 10, "height_mm": 10},
                source=ValueSource.MODEL,
            ),
            ModelCommand(
                id="source_b",
                op="create_box",
                args={"length_mm": 8, "width_mm": 8, "height_mm": 8},
                source=ValueSource.MODEL,
            ),
            ModelCommand(
                id="csg",
                op="apply_reviewed_intent",
                args={
                    "intent": program.to_mapping(),
                    "source_a": {"command_id": "source_a", "slot": "object"},
                    "source_b": {"command_id": "source_b", "slot": "object"},
                },
                depends_on=("source_a", "source_b"),
                source=ValueSource.MODEL,
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance_reviewed_csg_refs", criteria=()),
    )

    validated = validate_model_program(model_program)

    first = validated.commands[2].handler_kwargs["source_a"]
    second = validated.commands[2].handler_kwargs["source_b"]
    assert type(first) is type(second) is BoundResultRef
    assert (first.command_id, first.slot, first.value_shape.value) == (
        "source_a",
        "object",
        "object_id",
    )
    assert (second.command_id, second.slot, second.value_shape.value) == (
        "source_b",
        "object",
        "object_id",
    )

    legacy = dataclasses.replace(
        model_program,
        operations=(
            ModelCommand(
                id="legacy",
                op="apply_reviewed_intent",
                args={"intent": program.to_mapping()},
                source=ValueSource.MODEL,
            ),
        ),
    )
    assert set(validate_model_program(legacy).commands[0].handler_kwargs) == {"intent"}


def test_reviewed_csg_source_cannot_be_a_literal_or_cross_run_object_id() -> None:
    program = reviewed_csg_program(PartCoreOperation.CUT)
    model_program = ModelProgram(
        task_id="task_reviewed_csg_cross_run",
        base_revision="revision_0123456789abcdef0123456789abcdef",
        operations=(
            ModelCommand(
                id="csg",
                op="apply_reviewed_intent",
                args={
                    "intent": program.to_mapping(),
                    "source_a": "object_11111111111111111111111111111111",
                    "source_b": "object_22222222222222222222222222222222",
                },
                source=ValueSource.MODEL,
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance_reviewed_csg_cross_run", criteria=()),
    )

    with pytest.raises(ProgramValidationError) as caught:
        validate_model_program(model_program)

    assert caught.value.code is ProgramErrorCode.INVALID_RESULT_REFERENCE
    assert caught.value.path == "/operations/0/args/source_a"


def test_two_existing_reviewed_products_bind_to_pfg_sources() -> None:
    session, source_results = _source_fixture()
    lowered = lower_reviewed_intent(reviewed_csg_program(PartCoreOperation.CUT))

    bindings = build_part_csg_reviewed_bindings(
        session.doc,
        lowered.plan,
        lowered.route.operation,
        _ReviewedFamilyExecutionContext(
            session=session,
            document=session.doc,
            source_results=source_results,
        ),
    )

    assert type(bindings.execution) is PartCoreExecutionBindings
    assert bindings.execution.document is session.doc
    assert tuple(item.object for item in bindings.execution.sources) == tuple(
        item.object for item in source_results
    )
    assert tuple((item.node_id, item.result_id) for item in bindings.execution.sources) == (
        ("node_source_0", "result_source_0"),
        ("node_source_1", "result_source_1"),
    )
    assert bindings.source_result_shape_sha256s == tuple(
        item.native_receipt.result_shape_sha256 for item in source_results
    )


def test_program_run_state_resolves_ordered_one_to_eight_source_records() -> None:
    session, source_results = _source_fixture()
    state = executor_module._ReviewedProductRunState()
    for result in source_results:
        state.retain(result, session.read_object_identity(result.object))
    source_ids = tuple(
        session.read_object_identity(item.object).object_id for item in source_results
    )

    assert state.resolve(
        (source_ids[0],),
        read_identity=session.read_object_identity,
        minimum=1,
        maximum=8,
    ) == (source_results[0],)
    assert state.resolve(
        tuple(reversed(source_ids)),
        read_identity=session.read_object_identity,
        minimum=1,
        maximum=8,
    ) == tuple(reversed(source_results))

    with pytest.raises(RuntimeError):
        state.resolve(
            (source_ids[0], source_ids[0]),
            read_identity=session.read_object_identity,
            minimum=2,
            maximum=2,
        )
    session.identities[source_results[0].object] = dataclasses.replace(
        session.identities[source_results[0].object],
        provenance=Provenance(source=ProvenanceSource.IMPORTED, operation_id=None),
    )
    with pytest.raises(RuntimeError):
        state.resolve(
            (source_ids[0],),
            read_identity=session.read_object_identity,
            minimum=1,
            maximum=8,
        )
    with pytest.raises(RuntimeError):
        state.resolve(
            tuple(f"object_{index:032x}" for index in range(9)),
            read_identity=session.read_object_identity,
            minimum=1,
            maximum=8,
        )
    with pytest.raises(TypeError):
        pickle.dumps(state)


@pytest.mark.parametrize(
    "failure",
    ("duplicate", "cross_document", "stale", "wrong_provenance", "unknown"),
)
def test_invalid_product_source_binding_is_inert(failure: str) -> None:
    session, source_results = _source_fixture()
    lowered = lower_reviewed_intent(reviewed_csg_program(PartCoreOperation.CUT))
    if failure == "duplicate":
        source_results = (source_results[0], source_results[0])
    source = source_results[1].object
    before = tuple(session.doc.Objects)

    if failure == "duplicate":
        pass
    elif failure == "cross_document":
        source.Document = _Document()
    elif failure == "stale":
        source.Shape.brep = "shape-mutated-after-receipt"
    elif failure == "wrong_provenance":
        session.identities[source] = dataclasses.replace(
            session.identities[source],
            provenance=Provenance(source=ProvenanceSource.IMPORTED, operation_id=None),
        )
    else:
        del session.identities[source]

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        build_part_csg_reviewed_bindings(
            session.doc,
            lowered.plan,
            lowered.route.operation,
            _ReviewedFamilyExecutionContext(
                session=session,
                document=session.doc,
                source_results=source_results,
            ),
        )

    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert tuple(session.doc.Objects) == before


@pytest.mark.parametrize(
    "operation",
    (PartCoreOperation.CUT, PartCoreOperation.FUSE, PartCoreOperation.COMMON),
)
def test_reviewed_part_csg_executes_native_rule_with_authenticated_sources(
    operation: PartCoreOperation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, source_results = _source_fixture()
    calls: list[PartCoreExecutionBindings] = []

    monkeypatch.setitem(sys.modules, "FreeCAD", SimpleNamespace())
    monkeypatch.setattr(
        reviewed_execution,
        "require_reviewed_route_verified",
        lambda route, freecad: None,
    )

    def apply(
        raw: bytes,
        *,
        expected_content_sha256: str,
        expected_plan_sha256: str,
        bindings: PartCoreExecutionBindings,
    ) -> PartCoreConformanceReceipt:
        assert raw
        assert len(expected_content_sha256) == len(expected_plan_sha256) == 64
        calls.append(bindings)
        result = _Object(
            session.doc,
            name=f"Result{operation.value}",
            type_id=f"Part::{operation.value.title()}",
            brep=f"result-{operation.value}",
        )
        session.doc.Objects = (*session.doc.Objects, result)
        return PartCoreConformanceReceipt(
            plan_sha256=expected_plan_sha256,
            operation=operation,
            object_name=result.Name,
            source_shape_sha256s=tuple(_shape_sha256(item.object) for item in bindings.sources),
            result_shape_sha256=_shape_sha256(result),
        )

    monkeypatch.setattr(csg_execution, "apply_part_core_plan", apply)

    result = execute_reviewed_intent_native(
        session,
        reviewed_csg_program(operation),
        source_results=source_results,
    )

    assert result.route.operation.operation_id == operation.value
    assert result.object is session.doc.Objects[-1]
    assert len(calls) == 1
    assert tuple(item.object for item in calls[0].sources) == tuple(
        item.object for item in source_results
    )


__all__ = ["reviewed_csg_program"]
