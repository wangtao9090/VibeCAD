"""Public product gates for the append-only residual route wave."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import vibecad.execution.executor as executor_module
import vibecad.execution.freecad_partdesign_residual_reviewed_execution as pd_residual_execution
from tests.test_intent_bridge_freecad_part_core_adapter import _graph as _part_graph
from tests.test_intent_bridge_freecad_part_dressup_adapter import _graph as _dressup_graph
from tests.test_intent_bridge_freecad_partdesign_residual_adapter import (
    _graph as _partdesign_residual_graph,
)
from tests.test_program_executor import (
    BASE_REVISION,
    _active,
    _command,
    _FakePlacement,
    _FakeSession,
    _FakeShape,
    _managed_partdesign_additive_result,
    _store,
)
from tests.test_reviewed_intent_program import reviewed_box_program
from vibecad.execution.executor import InProcessCadExecutor
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_partdesign_reference_reviewed_execution import (
    PARTDESIGN_REFERENCE_COMPAT_MANIFEST,
    PARTDESIGN_REFERENCE_REVIEWED_FAMILY_SPEC,
    PARTDESIGN_REFERENCE_REVIEWED_PRODUCT_IDENTITIES,
    build_partdesign_reference_reviewed_family_descriptor,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PART_BOX_ROUTE,
    REVIEWED_PART_DRESSUP_ROUTES,
    REVIEWED_PART_RESIDUAL_ROUTES,
    REVIEWED_PARTDESIGN_RESIDUAL_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    ReviewedNativeExecutionResult,
    _ReviewedFamilyExecutionContext,
    _routes_for_family,
    lower_reviewed_intent,
    route_reviewed_intent,
)
from vibecad.parametric.freecad_part_core_rules import (
    PartCoreConformanceReceipt,
    PartCoreOperation,
)
from vibecad.parametric.freecad_part_dressup_rules import PartDressupOperation
from vibecad.parametric.freecad_partdesign_primitive_rules import (
    PartDesignPrimitiveOperation,
)
from vibecad.parametric.freecad_partdesign_residual_rules import (
    PartDesignResidualOperation,
)
from vibecad.workflow.contracts import AcceptanceSpec, ModelProgram
from vibecad.workflow.program import validate_model_program
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1


def _reviewed_program(route: object, graph: object) -> ReviewedIntentProgramV1:
    return ReviewedIntentProgramV1(
        operation_id=route.operation_id,
        semantic_operation=route.semantic_operation,
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


def _managed_box_result(
    session: _FakeSession,
    reviewed: ReviewedIntentProgramV1,
) -> ReviewedNativeExecutionResult:
    ordinal = sum(item.TypeId == "Part::Box" for item in session.doc.Objects)
    obj = type("ManagedResidualSource", (), {})()
    obj.Document = session.doc
    obj.Name = f"ResidualSource{ordinal}"
    obj.TypeId = "Part::Box"
    obj.Length = 10.0
    obj.Width = 8.0
    obj.Height = 6.0
    obj.Placement = _FakePlacement(float(ordinal))
    obj.Shape = _FakeShape(
        volume=480.0,
        area=376.0,
        bbox=(10.0, 8.0, 6.0),
        center=(5.0 + float(ordinal), 4.0, 3.0),
    )
    obj.State = ()
    session.doc.Objects = (*session.doc.Objects, obj)
    plan_sha256 = hashlib.sha256(f"box:{ordinal}".encode()).hexdigest()
    return ReviewedNativeExecutionResult(
        route=REVIEWED_PART_BOX_ROUTE,
        object=obj,
        plan_sha256=plan_sha256,
        plan_content_sha256=hashlib.sha256(f"box-content:{ordinal}".encode()).hexdigest(),
        native_receipt=PartCoreConformanceReceipt(
            plan_sha256=plan_sha256,
            operation=PartCoreOperation.BOX,
            object_name=obj.Name,
            source_shape_sha256s=(),
            result_shape_sha256=hashlib.sha256(obj.Shape.exportBrepToString().encode()).hexdigest(),
        ),
    )


def _managed_singleton_result(
    session: _FakeSession,
    reviewed: ReviewedIntentProgramV1,
    source_results: tuple[ReviewedNativeExecutionResult, ...],
    run_token: object,
) -> ReviewedNativeExecutionResult:
    route = route_reviewed_intent(reviewed)
    context = _ReviewedFamilyExecutionContext(
        session=session,
        document=session.doc,
        source_results=source_results,
        run_token=run_token,
    )
    contract = route.family.product_result(route.operation, context=context)
    obj = type("ManagedResidualResult", (), {})()
    obj.Document = session.doc
    obj.Name = f"Managed_{route.operation.operation_id}"
    obj.TypeId = contract.owned_type_ids[0]
    obj.Placement = _FakePlacement(0.0)
    obj.State = ("Up-to-date",)
    obj.isValid = lambda: True
    if contract.result_kind.value != "reference":
        obj.Shape = _FakeShape(volume=240.0, area=300.0, bbox=(8.0, 8.0, 6.0))
    session.doc.Objects = (*session.doc.Objects, obj)
    plan_sha256 = hashlib.sha256(route.operation_id.encode()).hexdigest()
    return ReviewedNativeExecutionResult(
        route=route,
        object=obj,
        plan_sha256=plan_sha256,
        plan_content_sha256=hashlib.sha256(f"content:{route.operation_id}".encode()).hexdigest(),
        native_receipt=SimpleNamespace(
            plan_sha256=plan_sha256,
            receipt_sha256=hashlib.sha256(f"receipt:{route.operation_id}".encode()).hexdigest(),
        ),
        _verified_execution_context=context,
    )


@pytest.mark.parametrize(
    ("route", "graph", "source_count"),
    (
        (
            next(
                item
                for item in REVIEWED_PART_RESIDUAL_ROUTES
                if item.operation.operation_id == PartCoreOperation.MULTI_FUSE.value
            ),
            _part_graph(PartCoreOperation.MULTI_FUSE, source_count=2),
            2,
        ),
        (
            next(
                item
                for item in REVIEWED_PART_DRESSUP_ROUTES
                if item.operation.operation_id == PartDressupOperation.EDGE_FILLET.value
            ),
            _dressup_graph(PartDressupOperation.EDGE_FILLET),
            1,
        ),
    ),
)
def test_public_model_program_executes_part_residual_and_dressup_routes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    route: object,
    graph: object,
    source_count: int,
) -> None:
    source = reviewed_box_program()
    dependent = _reviewed_program(route, graph)
    seen_sources: list[tuple[ReviewedNativeExecutionResult, ...]] = []

    def execute(
        session: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...] = (),
        _reviewed_run_token: object | None = None,
    ) -> ReviewedNativeExecutionResult:
        if value == source:
            assert source_results == () and _reviewed_run_token is None
            return _managed_box_result(session, source)
        assert value == dependent
        assert len(source_results) == source_count
        assert _reviewed_run_token is not None
        seen_sources.append(source_results)
        return _managed_singleton_result(
            session,
            dependent,
            source_results,
            _reviewed_run_token,
        )

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute)
    source_ids = tuple(f"source_{index}" for index in range(source_count))
    program = ModelProgram(
        task_id=f"task-public-{route.operation.operation_id}",
        base_revision=BASE_REVISION,
        operations=(
            *(
                _command(
                    source_id,
                    "apply_reviewed_intent",
                    args={"intent": source.to_mapping()},
                )
                for source_id in source_ids
            ),
            _command(
                "dependent",
                "apply_reviewed_intent",
                args={
                    "intent": dependent.to_mapping(),
                    "sources": tuple(
                        {"command_id": source_id, "slot": "object"} for source_id in source_ids
                    ),
                },
                depends_on=source_ids,
            ),
        ),
        acceptance=AcceptanceSpec(id="accept-public-residual-route", criteria=()),
    )
    session = _FakeSession()

    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(session, tmp_path),
    )

    assert [item.result.ok for item in outcomes] == [True] * (source_count + 1), [
        item.result.to_mapping() for item in outcomes
    ]
    assert len(seen_sources) == 1
    assert outcomes[-1].result.value["reviewed_operation_id"] == route.operation_id
    assert session.result_object.TypeId == route.operation.native_type_id


def test_public_model_program_executes_coordinate_system_and_preserves_body_tip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tests.test_execution_freecad_partdesign_primitive_reviewed_execution import (
        _program as primitive_program,
    )

    source = primitive_program(PartDesignPrimitiveOperation.ADDITIVE_BOX)
    route = next(
        item
        for item in REVIEWED_PARTDESIGN_RESIDUAL_ROUTES
        if item.operation.operation_id == PartDesignResidualOperation.COORDINATE_SYSTEM.value
    )
    dependent = _reviewed_program(
        route,
        _partdesign_residual_graph(PartDesignResidualOperation.COORDINATE_SYSTEM),
    )
    body_tip: object | None = None

    def execute(
        session: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...] = (),
        _reviewed_run_token: object | None = None,
    ) -> ReviewedNativeExecutionResult:
        nonlocal body_tip
        if value == source:
            return _managed_partdesign_additive_result(session, source)
        assert value == dependent
        assert len(source_results) == 1 and _reviewed_run_token is not None
        body = next(item for item in session.doc.Objects if item.TypeId == "PartDesign::Body")
        body_tip = body.Tip
        result = _managed_singleton_result(
            session,
            dependent,
            source_results,
            _reviewed_run_token,
        )
        assert body.Tip is body_tip
        return result

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute)
    program = ModelProgram(
        task_id="task-public-coordinate-system",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "body_anchor",
                "apply_reviewed_intent",
                args={"intent": source.to_mapping()},
            ),
            _command(
                "coordinate_system",
                "apply_reviewed_intent",
                args={
                    "intent": dependent.to_mapping(),
                    "sources": ({"command_id": "body_anchor", "slot": "object"},),
                },
                depends_on=("body_anchor",),
            ),
        ),
        acceptance=AcceptanceSpec(id="accept-public-coordinate-system", criteria=()),
    )
    session = _FakeSession()

    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(session, tmp_path),
    )

    assert [item.result.ok for item in outcomes] == [True, True], [
        item.result.to_mapping() for item in outcomes
    ]
    body = next(item for item in session.doc.Objects if item.TypeId == "PartDesign::Body")
    assert body.Tip is body_tip
    assert session.result_object is body_tip
    coordinate_system = next(
        item for item in session.doc.Objects if item.TypeId == "PartDesign::CoordinateSystem"
    )
    assert session.read_object_identity(coordinate_system).semantic_role.value == "support"


def test_public_hole_rejects_non_flatface_profile_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = reviewed_box_program()
    route = next(
        item
        for item in REVIEWED_PARTDESIGN_RESIDUAL_ROUTES
        if item.operation.operation_id == PartDesignResidualOperation.HOLE.value
    )
    hole = _reviewed_program(
        route,
        _partdesign_residual_graph(PartDesignResidualOperation.HOLE),
    )
    native_called = False

    def forbidden_native(*_args: object, **_kwargs: object) -> object:
        nonlocal native_called
        native_called = True
        raise AssertionError("wrong public profile must not reach native mutation")

    def execute(
        session: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...] = (),
        _reviewed_run_token: object | None = None,
    ) -> ReviewedNativeExecutionResult:
        if value == source:
            return _managed_box_result(session, source)
        assert value == hole
        assert len(source_results) == 2 and _reviewed_run_token is not None
        lowered = lower_reviewed_intent(hole)
        route.family.apply_plan(
            session.doc,
            lowered.plan,
            lowered.payload,
            lowered.result.plan_document,
            route.operation,
            _ReviewedFamilyExecutionContext(
                session=session,
                document=session.doc,
                source_results=source_results,
                run_token=_reviewed_run_token,
            ),
        )
        raise AssertionError("wrong public profile unexpectedly passed family authentication")

    monkeypatch.setattr(
        pd_residual_execution,
        "apply_partdesign_residual_plan",
        forbidden_native,
    )
    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute)
    program = ModelProgram(
        task_id="task-public-hole-wrong-profile",
        base_revision=BASE_REVISION,
        operations=(
            _command("base", "apply_reviewed_intent", args={"intent": source.to_mapping()}),
            _command(
                "wrong_profile",
                "apply_reviewed_intent",
                args={"intent": source.to_mapping()},
            ),
            _command(
                "hole",
                "apply_reviewed_intent",
                args={
                    "intent": hole.to_mapping(),
                    "sources": (
                        {"command_id": "base", "slot": "object"},
                        {"command_id": "wrong_profile", "slot": "object"},
                    ),
                },
                depends_on=("base", "wrong_profile"),
            ),
        ),
        acceptance=AcceptanceSpec(id="accept-public-hole-wrong-profile", criteria=()),
    )
    session = _FakeSession()

    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(session, tmp_path),
    )

    assert [item.result.ok for item in outcomes] == [True, True, False], [
        item.result.to_mapping() for item in outcomes
    ]
    assert native_called is False
    assert len(session.doc.Objects) == 2
    assert len(session.attached_identities) == 2


def test_reference_v2_manifest_remains_fail_closed_against_formal_v1() -> None:
    formal_by_id = {item.operation_id: item for item in current_freecad_intent_capability_specs()}
    assert not {item[0] for item in PARTDESIGN_REFERENCE_REVIEWED_PRODUCT_IDENTITIES}.intersection(
        route.operation_id for route in CURRENT_REVIEWED_INTENT_ROUTES
    )
    assert all(
        formal_by_id[operation_id].adapter_contract_sha256
        != PARTDESIGN_REFERENCE_COMPAT_MANIFEST.adapter.adapter_contract_sha256
        and formal_by_id[operation_id].rule_contract_sha256
        != PARTDESIGN_REFERENCE_COMPAT_MANIFEST.rule_contract_sha256
        for operation_id, _semantic_operation in PARTDESIGN_REFERENCE_REVIEWED_PRODUCT_IDENTITIES
    )

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        _routes_for_family(
            build_partdesign_reference_reviewed_family_descriptor(),
            PARTDESIGN_REFERENCE_REVIEWED_FAMILY_SPEC.operation_ids,
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE

    source = reviewed_box_program()
    blocked = ReviewedIntentProgramV1(
        operation_id=PARTDESIGN_REFERENCE_REVIEWED_PRODUCT_IDENTITIES[0][0],
        semantic_operation=PARTDESIGN_REFERENCE_REVIEWED_PRODUCT_IDENTITIES[0][1],
        intent_graph_sha256=source.intent_graph_sha256,
        intent_content_sha256=source.intent_content_sha256,
        intent_graph=source.intent_graph,
    )
    with pytest.raises(ReviewedIntentExecutionError) as public_error:
        route_reviewed_intent(blocked)
    assert public_error.value.code is ReviewedIntentExecutionErrorCode.UNKNOWN_ROUTE


def test_residual_routes_are_append_only_with_exact_order_and_digest() -> None:
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 96
    assert CURRENT_REVIEWED_INTENT_ROUTES[82:] == (
        *REVIEWED_PART_RESIDUAL_ROUTES,
        *REVIEWED_PART_DRESSUP_ROUTES,
        *REVIEWED_PARTDESIGN_RESIDUAL_ROUTES,
    )
    catalog_sha256 = hashlib.sha256(
        "\n".join(
            f"{route.operation_id}:{route.route_contract_sha256}"
            for route in CURRENT_REVIEWED_INTENT_ROUTES
        ).encode("ascii")
    ).hexdigest()
    assert catalog_sha256 == "6896d4ab9d1d991756c8b92a2538000bbd1201af26206a79131b087bb9a59d9a"
