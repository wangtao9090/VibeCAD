"""Focused tests for the shared family-owned CREATE recovery capsule seam."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from types import ModuleType

import pytest

import vibecad.execution.executor as executor_module
import vibecad.execution.freecad_reviewed_intent_execution as reviewed_execution
from tests.test_program_executor import _FakeSession, _FakeShape
from tests.test_reviewed_intent_program import reviewed_box_program
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PART_BOX_ROUTE,
    REVIEWED_PART_PRIMITIVE_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentRoute,
    ReviewedNativeExecutionResult,
    _commit_reviewed_native_create,
    _ReviewedCreateRecoveryCapsule,
    _ReviewedCreateRecoveryDescriptor,
    _ReviewedFamilyExecutionContext,
    _ReviewedFamilyNativeExecution,
    _ReviewedIntentFamilyDescriptor,
    _rollback_reviewed_native_create,
    execute_reviewed_intent_native,
)
from vibecad.workflow.contracts import ValueSource
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramError, ReviewedIntentProgramV1


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class _CreateReceipt:
    plan_sha256: str
    fail_adoption: bool = False
    receipt_sha256: str = "d" * 64

    def validate_adoption(self, document: object, obj: object, observation: object) -> None:
        del document, obj, observation
        if self.fail_adoption:
            raise RuntimeError("synthetic late adoption failure")


@dataclass(slots=True)
class _FakeExternalWorkspace:
    value: str = "pre-state"
    events: list[str] | None = None
    fail_recover: bool = False
    fail_commit: bool = False
    bad_recovery_proof: bool = False

    def __post_init__(self) -> None:
        if self.events is None:
            self.events = []


def _recovery_route(
    workspace: _FakeExternalWorkspace,
    behavior: dict[str, object] | None = None,
) -> ReviewedIntentRoute:
    behavior = behavior if behavior is not None else {}
    base_family = REVIEWED_PART_BOX_ROUTE.family
    operation = REVIEWED_PART_BOX_ROUTE.operation

    def prepare(
        document: object,
        selected_operation: object,
        context: _ReviewedFamilyExecutionContext,
    ) -> tuple[str, object]:
        assert document is context.document
        assert selected_operation == operation
        workspace.events.append("prepare")  # type: ignore[union-attr]
        snapshot = (workspace.value, tuple(document.Objects))
        return _digest(workspace.value), snapshot

    def recover(
        document: object,
        opaque: object,
        selected_operation: object,
        context: _ReviewedFamilyExecutionContext,
    ) -> None:
        assert document is context.document
        assert selected_operation == operation
        workspace.events.append("recover")  # type: ignore[union-attr]
        value, document_objects = opaque
        if tuple(document.Objects) != document_objects:
            raise RuntimeError("document rollback must precede external recovery")
        if workspace.fail_recover:
            raise RuntimeError("synthetic external recovery failure")
        workspace.value = value

    def verify(
        document: object,
        opaque: object,
        selected_operation: object,
        context: _ReviewedFamilyExecutionContext,
    ) -> str:
        del document, opaque, selected_operation, context
        workspace.events.append("verify")  # type: ignore[union-attr]
        if workspace.bad_recovery_proof and "recover" in workspace.events:
            return _digest("wrong-post-recovery-state")
        return _digest(workspace.value)

    def commit(
        document: object,
        opaque: object,
        selected_operation: object,
        context: _ReviewedFamilyExecutionContext,
    ) -> None:
        del opaque
        assert document is context.document
        assert selected_operation == operation
        workspace.events.append("commit")  # type: ignore[union-attr]
        if workspace.fail_commit:
            raise RuntimeError("synthetic external commit failure")
        workspace.value = "committed"

    def execute(
        document: object,
        plan: object,
        payload: bytes,
        plan_document: object,
        selected_operation: object,
        context: _ReviewedFamilyExecutionContext,
    ) -> _ReviewedFamilyNativeExecution:
        del plan, payload
        assert document is context.document
        assert selected_operation == operation
        workspace.events.append("execute")  # type: ignore[union-attr]
        workspace.value = "native-created"
        obj = context.session.identity_object
        obj.TypeId = (
            "Part::Feature" if behavior.get("invalid_native_type") else operation.native_type_id
        )
        obj.Shape = _FakeShape(volume=480.0)
        document.Objects = (*document.Objects, obj)
        return _ReviewedFamilyNativeExecution(
            object=obj,
            receipt=_CreateReceipt(
                plan_sha256=plan_document.document_digest,
                fail_adoption=bool(behavior.get("fail_adoption")),
            ),
        )

    descriptor = _ReviewedCreateRecoveryDescriptor(
        descriptor_id="synthetic_external_workspace",
        descriptor_version="1.0.0",
        descriptor_contract_sha256="e" * 64,
        operation_ids=(operation.operation_id,),
        prepare=prepare,
        recover=recover,
        verify=verify,
        commit=commit,
    )
    family = _ReviewedIntentFamilyDescriptor(
        manifest=base_family.manifest,
        subject_type_term=base_family.subject_type_term,
        adapter_factory=base_family.adapter_factory,
        validate_plan=base_family.validate_plan,
        execute_plan=execute,
        product_results=base_family.product_results,
        create_recovery=descriptor,
    )
    return ReviewedIntentRoute(
        operation_id=REVIEWED_PART_BOX_ROUTE.operation_id,
        semantic_operation=REVIEWED_PART_BOX_ROUTE.semantic_operation,
        family=family,
        manifest=family.manifest,
        operation=operation,
        subject_type_term=family.subject_type_term,
    )


def _install_route(monkeypatch: pytest.MonkeyPatch, route: ReviewedIntentRoute) -> None:
    monkeypatch.setitem(sys.modules, "FreeCAD", ModuleType("FreeCAD"))
    monkeypatch.setattr(reviewed_execution, "route_reviewed_intent", lambda value: route)
    monkeypatch.setattr(executor_module, "_route_reviewed_intent", lambda value: route)
    monkeypatch.setattr(
        reviewed_execution,
        "require_reviewed_route_verified",
        lambda route, *, freecad: None,
    )


def _apply(
    session: _FakeSession,
    state: executor_module._ReviewedProductRunState,
) -> dict[str, object]:
    return executor_module._managed_apply_reviewed_intent(
        session,
        executor_module._InvocationContext(
            operation_id="create_reviewed_box",
            operation="apply_reviewed_intent",
            preserve=(),
            source=ValueSource.MODEL,
        ),
        execution_leaf=execute_reviewed_intent_native,
        reviewed_products=state,
        intent=reviewed_box_program().to_mapping(),
    )


def test_descriptor_is_allowlisted_and_changes_only_its_route_contract() -> None:
    workspace = _FakeExternalWorkspace()
    route = _recovery_route(workspace)
    base = REVIEWED_PART_BOX_ROUTE.family
    clone = _ReviewedIntentFamilyDescriptor(
        manifest=base.manifest,
        subject_type_term=base.subject_type_term,
        adapter_factory=base.adapter_factory,
        validate_plan=base.validate_plan,
        execute_plan=base.execute_plan,
        product_results=base.product_results,
    )
    unchanged = ReviewedIntentRoute(
        operation_id=REVIEWED_PART_BOX_ROUTE.operation_id,
        semantic_operation=REVIEWED_PART_BOX_ROUTE.semantic_operation,
        family=clone,
        manifest=clone.manifest,
        operation=REVIEWED_PART_BOX_ROUTE.operation,
        subject_type_term=clone.subject_type_term,
    )

    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 82
    assert unchanged.route_contract_sha256 == REVIEWED_PART_BOX_ROUTE.route_contract_sha256
    assert route.route_contract_sha256 != REVIEWED_PART_BOX_ROUTE.route_contract_sha256
    sibling = REVIEWED_PART_PRIMITIVE_ROUTES[1]
    sibling_with_family_descriptor = ReviewedIntentRoute(
        operation_id=sibling.operation_id,
        semantic_operation=sibling.semantic_operation,
        family=route.family,
        manifest=route.family.manifest,
        operation=sibling.operation,
        subject_type_term=route.family.intent_binding.subject_type_for(sibling.operation),
    )
    assert route.family.create_recovery_for(sibling.operation) is None
    assert sibling_with_family_descriptor.route_contract_sha256 == sibling.route_contract_sha256

    descriptor = route.family.create_recovery
    assert descriptor is not None
    with pytest.raises(ReviewedIntentExecutionError):
        _ReviewedCreateRecoveryDescriptor(
            descriptor_id=descriptor.descriptor_id,
            descriptor_version=descriptor.descriptor_version,
            descriptor_contract_sha256=descriptor.descriptor_contract_sha256,
            operation_ids=(route.operation.operation_id, route.operation.operation_id),
            prepare=descriptor.prepare,
            recover=descriptor.recover,
            verify=descriptor.verify,
            commit=descriptor.commit,
        )


def test_success_commits_only_after_managed_adoption_and_clears_capsule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _FakeExternalWorkspace()
    route = _recovery_route(workspace)
    _install_route(monkeypatch, route)
    session = _FakeSession()
    state = executor_module._ReviewedProductRunState()

    outcome = _apply(session, state)

    retained = state.resolve(
        (outcome["object_id"],),
        read_identity=session.read_object_identity,
    )[0]
    assert workspace.value == "committed"
    assert workspace.events == ["prepare", "verify", "execute", "commit"]
    assert retained._create_recovery is None
    assert _commit_reviewed_native_create(retained) is False


@pytest.mark.parametrize("failure", ("adoption", "native_result", "observation"))
def test_late_failure_restores_document_before_exact_external_pre_state(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    behavior = {
        "fail_adoption": failure == "adoption",
        "invalid_native_type": failure == "native_result",
    }
    workspace = _FakeExternalWorkspace()
    route = _recovery_route(workspace, behavior)
    _install_route(monkeypatch, route)
    session = _FakeSession()
    state = executor_module._ReviewedProductRunState()
    if failure == "observation":
        observe = executor_module._entity_observations

        def fail_late_observation(selected_session: object) -> object:
            if tuple(selected_session.doc.Objects):
                raise RuntimeError("synthetic observation failure")
            return observe(selected_session)

        monkeypatch.setattr(executor_module, "_entity_observations", fail_late_observation)

    with pytest.raises(RuntimeError):
        _apply(session, state)

    assert tuple(session.doc.Objects) == ()
    assert workspace.value == "pre-state"
    assert workspace.events[-2:] == ["recover", "verify"]


@pytest.mark.parametrize("failure", ("recover", "proof"))
def test_external_recovery_or_proof_failure_escalates_to_internal_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    workspace = _FakeExternalWorkspace(
        fail_recover=failure == "recover",
        bad_recovery_proof=failure == "proof",
    )
    route = _recovery_route(workspace, {"fail_adoption": True})
    _install_route(monkeypatch, route)
    session = _FakeSession()

    with pytest.raises(executor_module.ExecutorError) as caught:
        _apply(session, executor_module._ReviewedProductRunState())

    assert caught.value.code is executor_module.ExecutorErrorCode.INTERNAL_FAILURE
    assert tuple(session.doc.Objects) == ()
    assert workspace.events.count("recover") == 1


def test_commit_failure_discards_same_run_result_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _FakeExternalWorkspace(fail_commit=True)
    route = _recovery_route(workspace)
    _install_route(monkeypatch, route)
    session = _FakeSession()
    state = executor_module._ReviewedProductRunState()

    with pytest.raises(RuntimeError):
        _apply(session, state)

    assert tuple(session.doc.Objects) == ()
    assert workspace.value == "pre-state"
    assert workspace.events[-3:] == ["commit", "recover", "verify"]
    retained_identity = session.attached_identities[-1][1]
    with pytest.raises(RuntimeError):
        state.resolve(
            (retained_identity.object_id,),
            read_identity=session.read_object_identity,
        )


def test_missing_duplicate_and_tampered_capsules_are_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _FakeExternalWorkspace()
    route = _recovery_route(workspace)
    _install_route(monkeypatch, route)
    session = _FakeSession()

    executed = execute_reviewed_intent_native(session, reviewed_box_program())
    capsule = executed._create_recovery
    assert type(capsule) is _ReviewedCreateRecoveryCapsule
    assert _commit_reviewed_native_create(executed) is True
    assert _commit_reviewed_native_create(executed) is False
    assert _rollback_reviewed_native_create(executed) is False

    session.doc.Objects = ()
    workspace.value = "pre-state"
    workspace.events.clear()
    tampered = execute_reviewed_intent_native(session, reviewed_box_program())
    capsule = tampered._create_recovery
    assert type(capsule) is _ReviewedCreateRecoveryCapsule
    object.__setattr__(capsule, "capsule_sha256", "0" * 64)
    assert _rollback_reviewed_native_create(tampered) is False
    assert workspace.value == "native-created"
    assert workspace.events[-1] == "execute"

    object.__setattr__(tampered, "_create_recovery", None)
    assert _commit_reviewed_native_create(tampered) is False
    with pytest.raises(ReviewedIntentExecutionError):
        _ReviewedCreateRecoveryCapsule(
            family=route.family,
            descriptor=route.family.create_recovery,
            document=session.doc,
            operation=route.operation,
            context=_ReviewedFamilyExecutionContext(
                session=session,
                document=session.doc,
                source_results=(),
            ),
            pre_state_sha256=_digest("pre-state"),
            opaque_state=("pre-state", ()),
            _seal=object(),
        )


def test_family_native_result_cannot_supply_a_recovery_capsule() -> None:
    with pytest.raises(TypeError):
        _ReviewedFamilyNativeExecution(  # type: ignore[call-arg]
            object=object(),
            receipt=object(),
            create_recovery=object(),
        )

    mapping = reviewed_box_program().to_mapping()
    with pytest.raises(ReviewedIntentProgramError):
        ReviewedIntentProgramV1.from_mapping({**mapping, "create_recovery": {"state": "model"}})

    workspace = _FakeExternalWorkspace()
    route = _recovery_route(workspace)
    session = _FakeSession()
    session.doc.Objects = (session.identity_object,)
    with pytest.raises(ReviewedIntentExecutionError):
        ReviewedNativeExecutionResult(
            route=route,
            object=session.identity_object,
            plan_sha256="a" * 64,
            plan_content_sha256="b" * 64,
            native_receipt=_CreateReceipt(plan_sha256="a" * 64),
            _verified_execution_context=_ReviewedFamilyExecutionContext(
                session=session,
                document=session.doc,
                source_results=(),
            ),
        )
