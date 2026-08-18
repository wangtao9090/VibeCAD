"""Focused shared tests for static Reviewed UPDATE_PRIMARY execution."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace

import pytest

import vibecad.execution.executor as executor_module
import vibecad.execution.freecad_reviewed_intent_execution as reviewed_execution
from tests.test_program_executor import _FakeSession, _FakeShape
from tests.test_reviewed_intent_program import reviewed_box_program
from vibecad.execution.freecad_reviewed_intent_execution import (
    REVIEWED_PART_BOX_ROUTE,
    ReviewedIntentExecutionError,
    ReviewedIntentRoute,
    ReviewedNativeExecutionResult,
    _ReviewedFamilyExecutionContext,
    _ReviewedFamilyNativeExecution,
    _ReviewedIntentFamilyDescriptor,
    _ReviewedPrimaryUpdateSnapshot,
    _ReviewedProductExecutionMode,
    _ReviewedProductResultContract,
    _ReviewedProductResultKind,
    execute_reviewed_intent_native,
)
from vibecad.execution.selectors import (
    EntityIdentity,
    Provenance,
    ProvenanceSource,
    SemanticRole,
)
from vibecad.parametric.freecad_part_core_rules import (
    PartCoreConformanceReceipt,
    PartCoreOperation,
)
from vibecad.workflow.contracts import ValueSource
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramError, ReviewedIntentProgramV1


def _state_sha256(obj: object) -> str:
    raw = "\0".join(
        (
            repr(obj.Length),  # type: ignore[attr-defined]
            obj.Shape.exportBrepToString(),  # type: ignore[attr-defined]
        )
    ).encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class _UpdateReceipt:
    plan_sha256: str
    fail_adoption: bool = False
    receipt_sha256: str = "d" * 64

    def validate_adoption(self, document: object, obj: object, observation: object) -> None:
        del document, obj, observation
        if self.fail_adoption:
            raise RuntimeError("synthetic late adoption failure")


def _identity() -> EntityIdentity:
    return EntityIdentity(
        object_id="object_0123456789abcdef0123456789abcdef",
        feature_id="feature_0123456789abcdef0123456789abcdef",
        object_type="Part::Box",
        semantic_role=SemanticRole.PRIMITIVE,
        provenance=Provenance(
            source=ProvenanceSource.MODEL,
            operation_id="create_reviewed_box",
        ),
    )


def _source_result(session: _FakeSession) -> ReviewedNativeExecutionResult:
    obj = session.identity_object
    return ReviewedNativeExecutionResult(
        route=REVIEWED_PART_BOX_ROUTE,
        object=obj,
        plan_sha256="a" * 64,
        plan_content_sha256="b" * 64,
        native_receipt=PartCoreConformanceReceipt(
            plan_sha256="a" * 64,
            operation=PartCoreOperation.BOX,
            object_name=obj.Name,
            source_shape_sha256s=(),
            result_shape_sha256=hashlib.sha256(obj.Shape.exportBrepToString().encode()).hexdigest(),
        ),
        state_sha256=_state_sha256(obj),
    )


def _update_route(behavior: dict[str, object]) -> ReviewedIntentRoute:
    base_family = REVIEWED_PART_BOX_ROUTE.family
    operation = REVIEWED_PART_BOX_ROUTE.operation

    def capture(
        document: object,
        selected_operation: object,
        context: _ReviewedFamilyExecutionContext,
    ) -> _ReviewedPrimaryUpdateSnapshot:
        assert selected_operation == operation
        source = context.source_results[0]
        obj = source.object
        return _ReviewedPrimaryUpdateSnapshot(
            primary=obj,
            owned_objects=source.owned_objects,
            state_sha256=_state_sha256(obj),
            rollback_state=(obj.Length, obj.Shape, tuple(document.Objects)),
        )

    def rollback(
        document: object,
        snapshot: _ReviewedPrimaryUpdateSnapshot,
        selected_operation: object,
        context: _ReviewedFamilyExecutionContext,
    ) -> None:
        assert selected_operation == operation
        assert context.source_results[0].object is snapshot.primary
        behavior["rollback_calls"] = int(behavior.get("rollback_calls", 0)) + 1
        if behavior.get("rollback_failure"):
            return
        length, shape, objects = snapshot.rollback_state
        snapshot.primary.Length = length
        snapshot.primary.Shape = shape
        document.Objects = objects

    def execute(
        document: object,
        plan: object,
        payload: bytes,
        plan_document: object,
        selected_operation: object,
        context: _ReviewedFamilyExecutionContext,
    ) -> _ReviewedFamilyNativeExecution:
        del plan, payload
        assert selected_operation == operation
        behavior["execute_calls"] = int(behavior.get("execute_calls", 0)) + 1
        obj = context.source_results[0].object
        selected = behavior.get("mutation", "success")
        if selected != "noop":
            obj.Length = 22.0
            obj.Shape = _FakeShape(
                volume=13_200.0,
                area=3_100.0,
                bbox=(22.0, 20.0, 30.0),
                center=(11.0, 10.0, 15.0),
            )
        if selected == "invalid_result":
            obj.Shape = _FakeShape(volume=0.0)
        if selected == "identity_tamper":
            context.session.attach_object_identity(
                obj,
                EntityIdentity(
                    object_id="object_ffffffffffffffffffffffffffffffff",
                    feature_id="feature_ffffffffffffffffffffffffffffffff",
                    object_type="Part::Box",
                    semantic_role=SemanticRole.PRIMITIVE,
                    provenance=Provenance(
                        source=ProvenanceSource.USER,
                        operation_id="tampered_during_update",
                    ),
                ),
            )
        if selected == "add_object":
            document.Objects = (
                *document.Objects,
                SimpleNamespace(Name="Injected", TypeId="Part::Feature"),
            )
        return _ReviewedFamilyNativeExecution(
            object=obj,
            receipt=_UpdateReceipt(
                plan_sha256=plan_document.document_digest,
                fail_adoption=bool(behavior.get("fail_adoption")),
            ),
            state_sha256=_state_sha256(obj),
        )

    family = _ReviewedIntentFamilyDescriptor(
        manifest=base_family.manifest,
        subject_type_term=base_family.subject_type_term,
        adapter_factory=base_family.adapter_factory,
        validate_plan=base_family.validate_plan,
        execute_plan=execute,
        product_results=(
            _ReviewedProductResultContract(
                operation_id=operation.operation_id,
                result_kind=_ReviewedProductResultKind.SOLID,
                owned_type_ids=(operation.native_type_id,),
                semantic_roles=(SemanticRole.PRIMITIVE,),
                source_count=1,
                execution_mode=_ReviewedProductExecutionMode.UPDATE_PRIMARY,
            ),
        ),
        minimum_sources=1,
        maximum_sources=1,
        capture_update_state=capture,
        rollback_update_state=rollback,
    )
    return ReviewedIntentRoute(
        operation_id=REVIEWED_PART_BOX_ROUTE.operation_id,
        semantic_operation=REVIEWED_PART_BOX_ROUTE.semantic_operation,
        family=family,
        manifest=family.manifest,
        operation=operation,
        subject_type_term=family.subject_type_term,
    )


def _seed() -> tuple[
    _FakeSession,
    executor_module._ReviewedProductRunState,
    ReviewedNativeExecutionResult,
    EntityIdentity,
]:
    session = _FakeSession()
    identity = _identity()
    session.attach_object_identity(session.identity_object, identity)
    session.set_result_object(session.identity_object)
    source = _source_result(session)
    state = executor_module._ReviewedProductRunState()
    state.retain(source, identity)
    return session, state, source, identity


def _install_route(
    monkeypatch: pytest.MonkeyPatch,
    route: ReviewedIntentRoute,
) -> None:
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
    identity: EntityIdentity,
) -> dict[str, object]:
    return executor_module._managed_apply_reviewed_intent(
        session,
        executor_module._InvocationContext(
            operation_id="update_reviewed_box",
            operation="apply_reviewed_intent",
            preserve=(),
            source=ValueSource.MODEL,
        ),
        execution_leaf=execute_reviewed_intent_native,
        reviewed_products=state,
        intent=reviewed_box_program().to_mapping(),
        sources=(identity.object_id,),
    )


def test_update_mode_is_static_route_contract_and_requires_rollback_pair() -> None:
    default_contract = _ReviewedProductResultContract(
        operation_id=REVIEWED_PART_BOX_ROUTE.operation.operation_id,
        result_kind=_ReviewedProductResultKind.SOLID,
        owned_type_ids=("Part::Box",),
        semantic_roles=(SemanticRole.PRIMITIVE,),
    )
    assert default_contract.execution_mode is _ReviewedProductExecutionMode.CREATE

    base = REVIEWED_PART_BOX_ROUTE.family
    with pytest.raises(ReviewedIntentExecutionError):
        _ReviewedIntentFamilyDescriptor(
            manifest=base.manifest,
            subject_type_term=base.subject_type_term,
            adapter_factory=base.adapter_factory,
            validate_plan=base.validate_plan,
            execute_plan=base.execute_plan,
            product_results=(
                _ReviewedProductResultContract(
                    operation_id=REVIEWED_PART_BOX_ROUTE.operation.operation_id,
                    result_kind=_ReviewedProductResultKind.SOLID,
                    owned_type_ids=("Part::Box",),
                    semantic_roles=(SemanticRole.PRIMITIVE,),
                    source_count=1,
                    execution_mode=_ReviewedProductExecutionMode.UPDATE_PRIMARY,
                ),
            ),
            minimum_sources=1,
            maximum_sources=1,
        )

    update = _update_route({})
    assert update.family.product_execution_mode(update.operation) is (
        _ReviewedProductExecutionMode.UPDATE_PRIMARY
    )
    assert update.route_contract_sha256 != REVIEWED_PART_BOX_ROUTE.route_contract_sha256
    with pytest.raises(ReviewedIntentProgramError):
        ReviewedIntentProgramV1.from_mapping(
            {**reviewed_box_program().to_mapping(), "execution_mode": "update_primary"}
        )


def test_managed_update_preserves_identity_and_replaces_same_run_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    behavior: dict[str, object] = {}
    route = _update_route(behavior)
    _install_route(monkeypatch, route)
    session, state, source, identity = _seed()
    before_objects = tuple(session.doc.Objects)
    before_attached = tuple(session.attached_identities)

    outcome = _apply(session, state, identity)

    assert outcome["object_id"] == identity.object_id
    assert outcome["feature_id"] == identity.feature_id
    assert tuple(session.doc.Objects) == before_objects
    assert tuple(session.attached_identities) == before_attached
    assert session.read_object_identity(session.identity_object) == identity
    assert session.identity_object.Length == 22.0
    current = state.resolve(
        (identity.object_id,),
        read_identity=session.read_object_identity,
    )[0]
    assert current is not source
    assert current.object is source.object
    assert current.state_sha256 != source.state_sha256
    assert current._update_recovery is None
    assert behavior == {"execute_calls": 1}


@pytest.mark.parametrize("mutation", ("noop", "add_object", "invalid_result"))
def test_native_update_rejects_noop_or_document_membership_change_and_restores_pre_state(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    behavior: dict[str, object] = {"mutation": mutation}
    route = _update_route(behavior)
    _install_route(monkeypatch, route)
    session, state, source, identity = _seed()
    before_objects = tuple(session.doc.Objects)
    before_length = session.identity_object.Length
    before_shape = session.identity_object.Shape

    with pytest.raises(RuntimeError):
        _apply(session, state, identity)

    assert tuple(session.doc.Objects) == before_objects
    assert session.identity_object.Length == before_length
    assert session.identity_object.Shape is before_shape
    assert state.resolve(
        (identity.object_id,),
        read_identity=session.read_object_identity,
    ) == (source,)
    assert behavior["execute_calls"] == 1
    assert behavior["rollback_calls"] == 1


def test_duplicate_retention_and_unscoped_native_update_are_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    behavior: dict[str, object] = {}
    route = _update_route(behavior)
    _install_route(monkeypatch, route)
    session, state, source, identity = _seed()
    with pytest.raises(RuntimeError):
        state.retain(source, identity)

    with pytest.raises(ReviewedIntentExecutionError):
        execute_reviewed_intent_native(
            session,
            reviewed_box_program(),
            source_results=(source,),
        )
    assert behavior.get("execute_calls", 0) == 0


def test_late_adoption_failure_uses_family_rollback_and_proves_pre_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    behavior: dict[str, object] = {"fail_adoption": True}
    route = _update_route(behavior)
    _install_route(monkeypatch, route)
    session, state, source, identity = _seed()
    before_length = session.identity_object.Length
    before_shape = session.identity_object.Shape

    with pytest.raises(RuntimeError):
        _apply(session, state, identity)

    assert session.identity_object.Length == before_length
    assert session.identity_object.Shape is before_shape
    assert state.resolve(
        (identity.object_id,),
        read_identity=session.read_object_identity,
    ) == (source,)
    assert behavior["rollback_calls"] == 1


def test_stale_state_wrong_provenance_and_cross_run_reject_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    behavior: dict[str, object] = {}
    route = _update_route(behavior)
    _install_route(monkeypatch, route)
    session, state, source, identity = _seed()
    session.identity_object.Length = 19.0

    with pytest.raises(RuntimeError):
        _apply(session, state, identity)
    assert behavior.get("execute_calls", 0) == 0
    assert session.identity_object.Length == 19.0
    assert state.resolve(
        (identity.object_id,),
        read_identity=session.read_object_identity,
    ) == (source,)

    wrong_identity = EntityIdentity(
        object_id=identity.object_id,
        feature_id=identity.feature_id,
        object_type=identity.object_type,
        semantic_role=identity.semantic_role,
        provenance=Provenance(
            source=ProvenanceSource.USER,
            operation_id="tampered_provenance",
        ),
    )
    session.attach_object_identity(session.identity_object, wrong_identity)
    with pytest.raises(RuntimeError):
        _apply(session, state, identity)
    assert behavior.get("execute_calls", 0) == 0

    other_run = executor_module._ReviewedProductRunState()
    with pytest.raises(RuntimeError):
        _apply(session, other_run, wrong_identity)
    assert behavior.get("execute_calls", 0) == 0


def test_unproven_family_rollback_escalates_to_internal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    behavior: dict[str, object] = {
        "fail_adoption": True,
        "rollback_failure": True,
    }
    route = _update_route(behavior)
    _install_route(monkeypatch, route)
    session, state, _source, identity = _seed()

    with pytest.raises(executor_module.ExecutorError) as caught:
        _apply(session, state, identity)

    assert caught.value.code is executor_module.ExecutorErrorCode.INTERNAL_FAILURE
    assert behavior["rollback_calls"] == 1


def test_native_rollback_identity_proof_failure_escalates_to_internal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    behavior: dict[str, object] = {"mutation": "identity_tamper"}
    route = _update_route(behavior)
    _install_route(monkeypatch, route)
    session, state, _source, identity = _seed()

    with pytest.raises(executor_module.ExecutorError) as caught:
        _apply(session, state, identity)

    assert caught.value.code is executor_module.ExecutorErrorCode.INTERNAL_FAILURE
    assert behavior["rollback_calls"] == 1
