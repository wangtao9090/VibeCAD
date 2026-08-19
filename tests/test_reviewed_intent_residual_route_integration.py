"""Public product gates for the append-only residual route wave."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import vibecad.execution.executor as executor_module
import vibecad.execution.freecad_partdesign_residual_reviewed_execution as pd_residual_execution
from tests.test_intent_bridge_freecad_part_core_adapter import _graph as _part_graph
from tests.test_intent_bridge_freecad_part_dressup_adapter import _graph as _dressup_graph
from tests.test_intent_bridge_freecad_partdesign_reference_adapter import (
    _graph as _reference_graph,
)
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
from vibecad.execution.freecad_partdesign_reference_reviewed_execution import (
    PARTDESIGN_REFERENCE_COMPAT_MANIFEST,
    PARTDESIGN_REFERENCE_REVIEWED_PRODUCT_IDENTITIES,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_FLATFACE_SKETCH_ROUTES,
    REVIEWED_PART_BOX_ROUTE,
    REVIEWED_PART_DRESSUP_ROUTES,
    REVIEWED_PART_RESIDUAL_ROUTES,
    REVIEWED_PARTDESIGN_REFERENCE_ROUTES,
    REVIEWED_PARTDESIGN_RESIDUAL_ROUTES,
    ReviewedNativeExecutionResult,
    _ReviewedFamilyExecutionContext,
    _ReviewedProductExecutionMode,
    _ReviewedProductResultKind,
    lower_reviewed_intent,
    route_reviewed_intent,
)
from vibecad.execution.selectors import SemanticRole
from vibecad.intent_bridge.freecad_sketch_flatface_bootstrap_adapter import (
    build_flatface_sketch_intent_graph,
)
from vibecad.parametric.freecad_part_core_rules import (
    PartCoreConformanceReceipt,
    PartCoreOperation,
)
from vibecad.parametric.freecad_part_dressup_rules import PartDressupOperation
from vibecad.parametric.freecad_partdesign_primitive_rules import (
    PartDesignPrimitiveOperation,
)
from vibecad.parametric.freecad_partdesign_reference_rules import PartDesignReferenceKind
from vibecad.parametric.freecad_partdesign_residual_rules import (
    PartDesignResidualOperation,
)
from vibecad.workflow.contracts import AcceptanceSpec, ModelProgram
from vibecad.workflow.program import ProgramValidationError, validate_model_program
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


@dataclass(frozen=True)
class _FakeStateReceipt:
    plan_sha256: str
    receipt_sha256: str
    state_sha256: str

    def validate_adoption(
        self,
        _document: object,
        _result: object,
        _observation: object,
    ) -> None:
        return None


def _managed_flatface_result(
    session: _FakeSession,
    reviewed: ReviewedIntentProgramV1,
    source_results: tuple[ReviewedNativeExecutionResult, ...],
    run_token: object,
    *,
    append: bool = True,
) -> ReviewedNativeExecutionResult:
    route = REVIEWED_FLATFACE_SKETCH_ROUTES[0]
    assert route_reviewed_intent(reviewed) is route
    assert len(source_results) == 1
    assert source_results[0]._is_retained_for_run(run_token)  # noqa: SLF001
    sketch = type("ManagedFlatFaceSketch", (), {})()
    sketch.Document = session.doc
    sketch.Name = "ManagedFlatFaceSketch"
    sketch.TypeId = "Sketcher::SketchObject"
    sketch.Placement = _FakePlacement(0.0)
    sketch.Shape = _FakeShape(
        volume=0.0,
        area=6.0,
        shape_type="Wire",
        solid_count=0,
        wire_closed=True,
    )
    sketch.State = ("Up-to-date",)
    sketch.isValid = lambda: True
    if append:
        session.doc.Objects = (*session.doc.Objects, sketch)
    plan_sha256 = hashlib.sha256(b"public-flatface-plan").hexdigest()
    state_sha256 = hashlib.sha256(b"public-flatface-state").hexdigest()
    receipt = _FakeStateReceipt(
        plan_sha256=plan_sha256,
        receipt_sha256=hashlib.sha256(b"public-flatface-receipt").hexdigest(),
        state_sha256=state_sha256,
    )
    result = object.__new__(ReviewedNativeExecutionResult)
    for name, value in (
        ("route", route),
        ("object", sketch),
        ("plan_sha256", plan_sha256),
        ("plan_content_sha256", hashlib.sha256(b"public-flatface-content").hexdigest()),
        ("native_receipt", receipt),
        ("owned_objects", (sketch,)),
        ("state_sha256", state_sha256),
        ("_update_recovery", None),
        ("_create_recovery", None),
        ("result_kind", _ReviewedProductResultKind.VALID_SHAPE),
        ("semantic_roles", (SemanticRole.FEATURE,)),
        ("execution_mode", _ReviewedProductExecutionMode.CREATE),
        ("requires_state_sha256", True),
        ("_retained_run_token", None),
    ):
        object.__setattr__(result, name, value)
    return result


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


def test_public_model_program_executes_reference_v2_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tests.test_execution_freecad_partdesign_primitive_reviewed_execution import (
        _program as primitive_program,
    )

    source = primitive_program(PartDesignPrimitiveOperation.ADDITIVE_BOX)
    route = next(
        item
        for item in REVIEWED_PARTDESIGN_REFERENCE_ROUTES
        if item.operation.operation_id == PartDesignReferenceKind.DATUM_PLANE.value
    )
    reference = _reviewed_program(route, _reference_graph(PartDesignReferenceKind.DATUM_PLANE))

    def execute(
        session: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...] = (),
        _reviewed_run_token: object | None = None,
    ) -> ReviewedNativeExecutionResult:
        if value == source:
            return _managed_partdesign_additive_result(session, source)
        assert value == reference
        assert len(source_results) == 1 and _reviewed_run_token is not None
        return _managed_singleton_result(
            session,
            reference,
            source_results,
            _reviewed_run_token,
        )

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute)
    program = ModelProgram(
        task_id="task-public-reference-v2",
        base_revision=BASE_REVISION,
        operations=(
            _command("base", "apply_reviewed_intent", args={"intent": source.to_mapping()}),
            _command(
                "reference",
                "apply_reviewed_intent",
                args={
                    "intent": reference.to_mapping(),
                    "sources": ({"command_id": "base", "slot": "object"},),
                },
                depends_on=("base",),
            ),
        ),
        acceptance=AcceptanceSpec(id="accept-public-reference-v2", criteria=()),
    )
    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(_FakeSession(), tmp_path),
    )

    assert [item.result.ok for item in outcomes] == [True, True], [
        item.result.to_mapping() for item in outcomes
    ]
    assert outcomes[-1].result.value["reviewed_operation_id"] == route.operation_id


@pytest.mark.parametrize("failure", ("bare_face", "wrong_selection"))
def test_public_reference_rejects_native_face_authority_and_wrong_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    from tests.test_execution_freecad_partdesign_primitive_reviewed_execution import (
        _program as primitive_program,
    )

    source = primitive_program(PartDesignPrimitiveOperation.ADDITIVE_BOX)
    route = next(
        item
        for item in REVIEWED_PARTDESIGN_REFERENCE_ROUTES
        if item.operation.operation_id == PartDesignReferenceKind.DATUM_PLANE.value
    )
    graph_kind = (
        PartDesignReferenceKind.SHAPE_BINDER
        if failure == "wrong_selection"
        else PartDesignReferenceKind.DATUM_PLANE
    )
    reference = _reviewed_program(route, _reference_graph(graph_kind))
    reference_calls = 0

    def execute(
        session: _FakeSession,
        value: object,
        **_kwargs: object,
    ) -> ReviewedNativeExecutionResult:
        nonlocal reference_calls
        if value == source:
            return _managed_partdesign_additive_result(session, source)
        reference_calls += 1
        lower_reviewed_intent(value)
        raise AssertionError("wrong reference selection unexpectedly lowered")

    args: dict[str, object] = {
        "intent": reference.to_mapping(),
        "sources": ({"command_id": "base", "slot": "object"},),
    }
    if failure == "bare_face":
        args["selection"] = "Face1"
    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute)
    session = _FakeSession()
    program = ModelProgram(
        task_id=f"task-public-reference-{failure}",
        base_revision=BASE_REVISION,
        operations=(
            _command("base", "apply_reviewed_intent", args={"intent": source.to_mapping()}),
            _command(
                "reference",
                "apply_reviewed_intent",
                args=args,
                depends_on=("base",),
            ),
        ),
        acceptance=AcceptanceSpec(id="accept-public-reference-negative", criteria=()),
    )
    if failure == "bare_face":
        with pytest.raises(ProgramValidationError):
            validate_model_program(program)
        assert reference_calls == 0
        assert session.doc.Objects == ()
        return
    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(session, tmp_path),
    )

    assert [item.result.ok for item in outcomes] == [True, False]
    assert reference_calls == 1
    assert len(session.doc.Objects) == 10


def test_public_model_program_executes_base_flatface_sketch_hole_same_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tests.test_execution_freecad_partdesign_primitive_reviewed_execution import (
        _program as primitive_program,
    )

    base = primitive_program(PartDesignPrimitiveOperation.ADDITIVE_BOX)
    flatface = _reviewed_program(
        REVIEWED_FLATFACE_SKETCH_ROUTES[0],
        build_flatface_sketch_intent_graph(),
    )
    hole_route = next(
        item
        for item in REVIEWED_PARTDESIGN_RESIDUAL_ROUTES
        if item.operation.operation_id == PartDesignResidualOperation.HOLE.value
    )
    hole = _reviewed_program(
        hole_route,
        _partdesign_residual_graph(PartDesignResidualOperation.HOLE),
    )
    seen_source_counts: list[int] = []

    def execute(
        session: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...] = (),
        _reviewed_run_token: object | None = None,
    ) -> ReviewedNativeExecutionResult:
        if value == base:
            return _managed_partdesign_additive_result(session, base)
        assert _reviewed_run_token is not None
        seen_source_counts.append(len(source_results))
        if value == flatface:
            return _managed_flatface_result(
                session,
                flatface,
                source_results,
                _reviewed_run_token,
            )
        assert value == hole and len(source_results) == 2
        return _managed_singleton_result(
            session,
            hole,
            source_results,
            _reviewed_run_token,
        )

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute)
    program = ModelProgram(
        task_id="task-public-flatface-hole-chain",
        base_revision=BASE_REVISION,
        operations=(
            _command("base", "apply_reviewed_intent", args={"intent": base.to_mapping()}),
            _command(
                "flatface",
                "apply_reviewed_intent",
                args={
                    "intent": flatface.to_mapping(),
                    "sources": ({"command_id": "base", "slot": "object"},),
                },
                depends_on=("base",),
            ),
            _command(
                "hole",
                "apply_reviewed_intent",
                args={
                    "intent": hole.to_mapping(),
                    "sources": (
                        {"command_id": "base", "slot": "object"},
                        {"command_id": "flatface", "slot": "object"},
                    ),
                },
                depends_on=("base", "flatface"),
            ),
        ),
        acceptance=AcceptanceSpec(id="accept-public-flatface-hole-chain", criteria=()),
    )
    session = _FakeSession()
    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(session, tmp_path),
    )

    assert [item.result.ok for item in outcomes] == [True, True, True], [
        item.result.to_mapping() for item in outcomes
    ]
    assert seen_source_counts == [1, 2]
    assert outcomes[1].result.value["reviewed_operation_id"] == flatface.operation_id
    assert outcomes[2].result.value["reviewed_operation_id"] == hole.operation_id


@pytest.mark.parametrize(
    "failure",
    ("wrong_face", "stale", "tamper", "noop", "rollback"),
)
def test_public_flatface_route_fails_closed_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    from tests.test_execution_freecad_partdesign_primitive_reviewed_execution import (
        _program as primitive_program,
    )

    base = primitive_program(PartDesignPrimitiveOperation.ADDITIVE_BOX)
    flatface = _reviewed_program(
        REVIEWED_FLATFACE_SKETCH_ROUTES[0],
        build_flatface_sketch_intent_graph(),
    )
    flatface_calls = 0

    def execute(
        session: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...] = (),
        _reviewed_run_token: object | None = None,
    ) -> ReviewedNativeExecutionResult:
        nonlocal flatface_calls
        if value == base:
            return _managed_partdesign_additive_result(session, base)
        flatface_calls += 1
        assert _reviewed_run_token is not None and len(source_results) == 1
        if failure == "stale":
            assert not source_results[0]._is_retained_for_run(object())  # noqa: SLF001
            raise RuntimeError("stale source")
        result = _managed_flatface_result(
            session,
            flatface,
            source_results,
            _reviewed_run_token,
            append=failure != "noop",
        )
        if failure == "rollback":
            raise RuntimeError("late native failure")
        return result

    flatface_mapping = flatface.to_mapping()
    if failure == "tamper":
        flatface_mapping["intent_content_sha256"] = "f" * 64
    args: dict[str, object] = {
        "intent": flatface_mapping,
        "sources": ({"command_id": "base", "slot": "object"},),
    }
    if failure == "wrong_face":
        args["selection"] = "Face1"
    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute)
    session = _FakeSession()
    program = ModelProgram(
        task_id=f"task-public-flatface-{failure}",
        base_revision=BASE_REVISION,
        operations=(
            _command("base", "apply_reviewed_intent", args={"intent": base.to_mapping()}),
            _command(
                "flatface",
                "apply_reviewed_intent",
                args=args,
                depends_on=("base",),
            ),
        ),
        acceptance=AcceptanceSpec(id="accept-public-flatface-negative", criteria=()),
    )
    if failure in {"wrong_face", "tamper"}:
        with pytest.raises(ProgramValidationError):
            validate_model_program(program)
        assert flatface_calls == 0
        assert session.doc.Objects == ()
        return
    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(session, tmp_path),
    )

    assert [item.result.ok for item in outcomes] == [True, False]
    assert flatface_calls == 1
    assert len(session.doc.Objects) == 10


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


def test_reference_v2_routes_are_exactly_registered() -> None:
    assert (
        tuple(
            (route.operation_id, route.semantic_operation)
            for route in REVIEWED_PARTDESIGN_REFERENCE_ROUTES
        )
        == PARTDESIGN_REFERENCE_REVIEWED_PRODUCT_IDENTITIES
    )
    assert all(
        route.manifest is PARTDESIGN_REFERENCE_COMPAT_MANIFEST
        for route in REVIEWED_PARTDESIGN_REFERENCE_ROUTES
    )
    assert CURRENT_REVIEWED_INTENT_ROUTES[120:125] == REVIEWED_PARTDESIGN_REFERENCE_ROUTES


def test_residual_routes_are_append_only_with_exact_order_and_digest() -> None:
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 126
    assert CURRENT_REVIEWED_INTENT_ROUTES[82:96] == (
        *REVIEWED_PART_RESIDUAL_ROUTES,
        *REVIEWED_PART_DRESSUP_ROUTES,
        *REVIEWED_PARTDESIGN_RESIDUAL_ROUTES,
    )
    catalog_sha256 = hashlib.sha256(
        "\n".join(
            f"{route.operation_id}:{route.route_contract_sha256}"
            for route in CURRENT_REVIEWED_INTENT_ROUTES[:96]
        ).encode("ascii")
    ).hexdigest()
    assert catalog_sha256 == "53875bdb39d25f3226ae9db5cdb142654da2dd3ba4c734f98d132d813d4cf1ee"
    legacy99_sha256 = hashlib.sha256(
        "\n".join(
            f"{route.operation_id}:{route.route_contract_sha256}"
            for route in CURRENT_REVIEWED_INTENT_ROUTES[:99]
        ).encode("ascii")
    ).hexdigest()
    assert legacy99_sha256 == "10102d5be3ba03f6ed7f28b2fb4c46e71fe47d8fe51b024dc4a4020695727509"
    assert CURRENT_REVIEWED_INTENT_ROUTES[125:] == REVIEWED_FLATFACE_SKETCH_ROUTES
    current_sha256 = hashlib.sha256(
        "\n".join(
            f"{route.operation_id}:{route.route_contract_sha256}"
            for route in CURRENT_REVIEWED_INTENT_ROUTES
        ).encode("ascii")
    ).hexdigest()
    assert current_sha256 == "db733cab94f9e72f6ce380c3c2509791ecdd94b650249c28d719d1a190c351d3"
