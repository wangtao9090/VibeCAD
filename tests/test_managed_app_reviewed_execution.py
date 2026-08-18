"""Managed fake E2E gates for the Reviewed App product integration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import vibecad.execution.executor as executor_module
import vibecad.execution.freecad_app_reviewed_execution as app_execution
from tests.test_execution_freecad_app_reviewed_execution import (
    _created_closure,
    _reviewed_program,
)
from tests.test_program_executor import (
    BASE_REVISION,
    _active,
    _command,
    _FakeDocument,
    _FakeSession,
    _store,
)
from vibecad.execution.freecad_app_reviewed_execution import (
    APP_REVIEWED_PRODUCT_CONTRACTS,
    AppReviewedProductReceipt,
    build_app_reviewed_bindings,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    REVIEWED_APP_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    ReviewedNativeExecutionResult,
    _ReviewedFamilyExecutionContext,
    lower_reviewed_intent,
    route_reviewed_intent,
)
from vibecad.execution.selectors import ProvenanceSource, SemanticRole
from vibecad.parametric.freecad_app_family_rules import (
    APP_FAMILY_NATIVE_TYPE_IDS,
    APP_FAMILY_RELATION_KINDS,
    AppFamilyConformanceReceipt,
    AppFamilyOperation,
    AppFamilyRelationKind,
)
from vibecad.workflow.contracts import AcceptanceSpec, ModelProgram, ValueSource
from vibecad.workflow.program import validate_model_program


def _install_fake_app_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> list[AppFamilyOperation]:
    created_operations: list[AppFamilyOperation] = []

    def execute(
        session: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...] = (),
        _reviewed_run_token: object | None = None,
    ) -> ReviewedNativeExecutionResult:
        route = route_reviewed_intent(value)
        assert route in REVIEWED_APP_ROUTES
        operation = AppFamilyOperation(route.operation.operation_id)
        lowered = lower_reviewed_intent(value)
        context = _ReviewedFamilyExecutionContext(
            session=session,
            document=session.doc,
            source_results=source_results,
            run_token=_reviewed_run_token,
        )
        bindings = build_app_reviewed_bindings(
            session.doc,
            lowered.plan,
            route.operation,
            context,
        )
        related = bindings.related_object
        expected_relation = APP_FAMILY_RELATION_KINDS[operation] is not AppFamilyRelationKind.NONE
        assert (related is not None) is expected_relation
        primary_name = f"Reviewed_{operation.value}"
        if session.doc.getObject(primary_name) is not None:
            raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.EXECUTION_FAILED)
        owned = _created_closure(session.doc, operation, related)
        session.doc.Objects = (*session.doc.Objects, *owned)
        created_operations.append(operation)
        native_receipt = AppFamilyConformanceReceipt(
            plan_sha256=lowered.plan.plan_sha256,
            operation=operation,
            object_name=owned[0].Name,
            native_type_id=APP_FAMILY_NATIVE_TYPE_IDS[operation],
            owned_object_names=tuple(item.Name for item in owned),
            related_object_name=None if related is None else related.Name,
        )
        contract = APP_REVIEWED_PRODUCT_CONTRACTS[operation]
        receipt = AppReviewedProductReceipt(
            native_receipt=native_receipt,
            state_sha256=app_execution._state_sha256(  # noqa: SLF001 - exact family state
                session.doc,
                owned[0],
                owned,
                operation,
            ),
            owned_type_ids=contract.owned_type_ids,
            semantic_roles=contract.semantic_roles,
        )
        return ReviewedNativeExecutionResult(
            route=route,
            object=owned[0],
            plan_sha256=lowered.result.plan_document.document_digest,
            plan_content_sha256=lowered.result.plan_document.content_sha256,
            native_receipt=receipt,
            owned_objects=owned,
            state_sha256=receipt.state_sha256,
            _verified_execution_context=context,
        )

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute)
    return created_operations


def _program(
    task_id: str,
    operations: tuple[object, ...],
) -> object:
    return validate_model_program(
        ModelProgram(
            task_id=task_id,
            base_revision=BASE_REVISION,
            operations=operations,
            acceptance=AcceptanceSpec(id=f"accept-{task_id}", criteria=()),
        )
    )


def _direct_apply(
    session: _FakeSession,
    state: executor_module._ReviewedProductRunState,
    operation_id: str,
    operation: AppFamilyOperation,
    *,
    sources: tuple[str, ...] = (),
) -> dict[str, object]:
    return executor_module._managed_apply_reviewed_intent(
        session,
        executor_module._InvocationContext(
            operation_id=operation_id,
            operation="apply_reviewed_intent",
            preserve=(),
            source=ValueSource.MODEL,
        ),
        execution_leaf=executor_module._execute_reviewed_intent_native,
        reviewed_products=state,
        intent=_reviewed_program(operation).to_mapping(),
        sources=sources,
    )


def test_managed_app_source_zero_singleton_adopts_and_duplicate_is_inert(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created = _install_fake_app_leaf(monkeypatch)
    reviewed = _reviewed_program(AppFamilyOperation.TEXT_DOCUMENT)
    program = _program(
        "task-managed-app-text",
        (
            _command(
                "app_text",
                "apply_reviewed_intent",
                args={"intent": reviewed.to_mapping()},
            ),
        ),
    )
    session = _FakeSession()
    executor = executor_module.InProcessCadExecutor(store=_store())

    first = executor.execute_program(program=program, candidate=_active(session, tmp_path))
    objects_after_first = tuple(session.doc.Objects)
    identities_after_first = tuple(session.attached_identities)
    duplicate = executor.execute_program(program=program, candidate=_active(session, tmp_path))

    assert len(first) == 1 and first[0].result.ok is True
    assert len(duplicate) == 1 and duplicate[0].result.ok is False
    assert tuple(session.doc.Objects) == objects_after_first
    assert tuple(session.attached_identities) == identities_after_first
    assert len(objects_after_first) == len(identities_after_first) == 1
    identity = identities_after_first[0][1]
    assert identity.object_type == "App::TextDocument"
    assert identity.semantic_role is SemanticRole.SUPPORT
    assert identity.provenance.source is ProvenanceSource.MODEL
    assert first[0].result.value["after"]["valid_shape"] is None
    assert first[0].result.value["after"]["solid_count"] is None
    assert session.result_object is None
    assert created == [AppFamilyOperation.TEXT_DOCUMENT]


@pytest.mark.parametrize("failure", ("missing_state", "rebound_state", "missing_validator"))
def test_app_reference_contract_requires_exact_family_state_authority(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    _install_fake_app_leaf(monkeypatch)
    session = _FakeSession()
    state = executor_module._ReviewedProductRunState()
    outcome = _direct_apply(
        session,
        state,
        "source",
        AppFamilyOperation.TEXT_DOCUMENT,
    )
    original = state.resolve(
        (str(outcome["object_id"]),),
        read_identity=session.read_object_identity,
    )[0]
    context = _ReviewedFamilyExecutionContext(
        session=session,
        document=session.doc,
        source_results=(),
    )
    state_sha256 = original.state_sha256
    receipt = original.native_receipt
    if failure == "missing_state":
        state_sha256 = None
    elif failure == "rebound_state":
        state_sha256 = "f" * 64
    else:
        receipt = SimpleNamespace(
            plan_sha256=original.plan_sha256,
            state_sha256=original.state_sha256,
            receipt_sha256=original.native_receipt.receipt_sha256,
        )

    with pytest.raises(ReviewedIntentExecutionError) as captured:
        ReviewedNativeExecutionResult(
            route=original.route,
            object=original.object,
            plan_sha256=original.plan_sha256,
            plan_content_sha256=original.plan_content_sha256,
            native_receipt=receipt,
            owned_objects=original.owned_objects,
            state_sha256=state_sha256,
            _verified_execution_context=context,
        )

    assert captured.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


def test_managed_app_source_one_group_and_link_use_same_run_product(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created = _install_fake_app_leaf(monkeypatch)
    source = _reviewed_program(AppFamilyOperation.TEXT_DOCUMENT)
    group = _reviewed_program(AppFamilyOperation.DOCUMENT_GROUP)
    link = _reviewed_program(AppFamilyOperation.OBJECT_LINK)
    source_ref = ({"command_id": "source", "slot": "object"},)
    program = _program(
        "task-managed-app-relations",
        (
            _command(
                "source",
                "apply_reviewed_intent",
                args={"intent": source.to_mapping()},
            ),
            _command(
                "group",
                "apply_reviewed_intent",
                args={"intent": group.to_mapping(), "sources": source_ref},
                depends_on=("source",),
            ),
            _command(
                "link",
                "apply_reviewed_intent",
                args={"intent": link.to_mapping(), "sources": source_ref},
                depends_on=("source",),
            ),
        ),
    )
    session = _FakeSession()

    outcomes = executor_module.InProcessCadExecutor(store=_store()).execute_program(
        program=program,
        candidate=_active(session, tmp_path),
    )

    assert tuple(item.result.ok for item in outcomes) == (True, True, True)
    assert created == [
        AppFamilyOperation.TEXT_DOCUMENT,
        AppFamilyOperation.DOCUMENT_GROUP,
        AppFamilyOperation.OBJECT_LINK,
    ]
    assert tuple(item.TypeId for item in session.doc.Objects) == (
        "App::TextDocument",
        "App::DocumentObjectGroup",
        "App::Link",
    )
    assert len(session.attached_identities) == 3
    assert all(
        identity.semantic_role is SemanticRole.SUPPORT
        for _, identity in session.attached_identities
    )
    assert session.result_object is None


def test_managed_app_part_adopts_exact_nine_closure_and_rejects_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created = _install_fake_app_leaf(monkeypatch)
    source = _reviewed_program(AppFamilyOperation.TEXT_DOCUMENT)
    part = _reviewed_program(AppFamilyOperation.POSITIONED_PART)
    source_ref = ({"command_id": "source", "slot": "object"},)
    program = _program(
        "task-managed-app-part",
        (
            _command(
                "source",
                "apply_reviewed_intent",
                args={"intent": source.to_mapping()},
            ),
            _command(
                "part",
                "apply_reviewed_intent",
                args={"intent": part.to_mapping(), "sources": source_ref},
                depends_on=("source",),
            ),
            _command(
                "part_duplicate",
                "apply_reviewed_intent",
                args={"intent": part.to_mapping(), "sources": source_ref},
                depends_on=("source",),
            ),
        ),
    )
    session = _FakeSession()

    outcomes = executor_module.InProcessCadExecutor(store=_store()).execute_program(
        program=program,
        candidate=_active(session, tmp_path),
    )

    assert tuple(item.result.ok for item in outcomes) == (True, True, False)
    assert created == [AppFamilyOperation.TEXT_DOCUMENT, AppFamilyOperation.POSITIONED_PART]
    assert tuple(item.TypeId for item in session.doc.Objects[1:]) == (
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
    assert len(session.doc.Objects) == len(session.attached_identities) == 10
    assert tuple(identity.semantic_role for _, identity in session.attached_identities[1:]) == (
        SemanticRole.PART,
        *(SemanticRole.SUPPORT,) * 8,
    )
    assert session.result_object is None
    assert outcomes[1].result.value["after"]["valid_shape"] is None
    assert outcomes[1].result.value["after"]["solid_count"] is None


def test_managed_app_part_late_adoption_failure_rolls_back_full_closure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_app_leaf(monkeypatch)

    class FailingAttachSession(_FakeSession):
        def attach_object_identity(self, obj: object, identity: object) -> object:
            if len(self.attached_identities) == 4:
                raise RuntimeError("bounded App closure adoption failure")
            return super().attach_object_identity(obj, identity)

    source = _reviewed_program(AppFamilyOperation.TEXT_DOCUMENT)
    part = _reviewed_program(AppFamilyOperation.POSITIONED_PART)
    program = _program(
        "task-managed-app-part-rollback",
        (
            _command(
                "source",
                "apply_reviewed_intent",
                args={"intent": source.to_mapping()},
            ),
            _command(
                "part",
                "apply_reviewed_intent",
                args={
                    "intent": part.to_mapping(),
                    "sources": ({"command_id": "source", "slot": "object"},),
                },
                depends_on=("source",),
            ),
        ),
    )
    session = FailingAttachSession()

    outcomes = executor_module.InProcessCadExecutor(store=_store()).execute_program(
        program=program,
        candidate=_active(session, tmp_path),
    )

    assert tuple(item.result.ok for item in outcomes) == (True, False)
    assert tuple(item.TypeId for item in session.doc.Objects) == ("App::TextDocument",)
    assert len(session.attached_identities) == 1
    assert session.attached_identities[0][0] is session.doc.Objects[0]


@pytest.mark.parametrize("failure", ("unknown", "cross_document", "stale"))
def test_managed_app_invalid_source_rejects_before_dependent_mutation(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    created = _install_fake_app_leaf(monkeypatch)
    session = _FakeSession()
    state = executor_module._ReviewedProductRunState()
    source_outcome = _direct_apply(
        session,
        state,
        "source",
        AppFamilyOperation.TEXT_DOCUMENT,
    )
    source = session.doc.Objects[0]
    source_id = str(source_outcome["object_id"])
    if failure == "unknown":
        selected_id = "object_ffffffffffffffffffffffffffffffff"
    else:
        selected_id = source_id
        if failure == "cross_document":
            source.Document = _FakeDocument()
        else:
            source.Text = "tampered after retention"
    before = tuple(session.doc.Objects)

    with pytest.raises(RuntimeError):
        _direct_apply(
            session,
            state,
            "dependent",
            AppFamilyOperation.DOCUMENT_GROUP,
            sources=(selected_id,),
        )

    assert tuple(session.doc.Objects) == before
    assert created == [AppFamilyOperation.TEXT_DOCUMENT]
