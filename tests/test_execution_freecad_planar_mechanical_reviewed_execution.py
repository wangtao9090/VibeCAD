"""Product-closure tests for the non-routable PM1 reviewed handoff."""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass

import pytest

import vibecad.execution.freecad_planar_mechanical_reviewed_execution as pm_execution
from vibecad.execution.freecad_planar_mechanical_reviewed_execution import (
    PLANAR_MECHANICAL_REVIEWED_OPERATION_SPECS,
    PLANAR_MECHANICAL_REVIEWED_PRODUCT_HANDOFF,
    PlanarMechanicalOwnershipClosure,
    execute_planar_mechanical_reviewed_plan_with_sources,
    resolve_planar_mechanical_product_contract,
    resolve_planar_mechanical_reviewed_operation,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
)
from vibecad.execution.selectors import SemanticRole
from vibecad.intent_bridge.contracts import DocumentRef
from vibecad.intent_bridge.freecad_planar_mechanical_adapter import (
    FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR,
    PLANAR_PLAN_DOCUMENT_ROLE_TERM,
    PLANAR_PLAN_SCHEMA_TERM,
)
from vibecad.parametric.freecad_planar_mechanical_rules import (
    PLANAR_MECHANICAL_PLAN_MEDIA_TYPE,
    PlanarCircleRemoval,
    PlanarDocumentBinding,
    PlanarMechanicalBackendPlan,
    PlanarMechanicalConformanceReceipt,
    PlanarRectangleProfile,
)


def _semantic_operation(operation) -> str:
    namespace, version, term_id, digest = operation.semantic_term.semantic_identity
    return f"{namespace}/{version}/{term_id}@{digest}"


def _operation(suffix: str):
    return next(
        item
        for item in PLANAR_MECHANICAL_REVIEWED_OPERATION_SPECS
        if item.operation_id.endswith(suffix)
    )


def _plan(circle_count: int) -> PlanarMechanicalBackendPlan:
    circles = []
    base_node = "node.add"
    base_result = "result.add.solid"
    for index in range(circle_count):
        suffix = f"{index:03d}"
        node = f"node.remove.{suffix}"
        result = f"result.remove.{suffix}.solid"
        circles.append(
            PlanarCircleRemoval(
                geometry_id=f"geometry.inner.{suffix}",
                profile_result_id=f"result.profiles.inner.{suffix}",
                node_id=node,
                result_id=result,
                base_node_id=base_node,
                base_result_id=base_result,
                center_x_mm=-4.0 + index * 8.0,
                center_y_mm=0.0,
                radius_mm=1.0,
            )
        )
        base_node = node
        base_result = result
    binding = PlanarDocumentBinding(
        artifact_id="artifact.intent",
        document_id="document.intent",
        document_digest="1" * 64,
        content_sha256="2" * 64,
    )
    return PlanarMechanicalBackendPlan(
        sketch_document=binding,
        parametric_document=binding,
        lowering_request_sha256="3" * 64,
        adapter_contract_sha256=(
            FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR.adapter_contract_sha256
        ),
        body_id="body.main",
        profiles_node_id="node.profiles",
        add_node_id="node.add",
        add_result_id="result.add.solid",
        final_node_id=base_node,
        final_result_id=base_result,
        depth_parameter_id="parameter.depth",
        depth_mm=8.0,
        rectangle=PlanarRectangleProfile(
            geometry_id="geometry.outer",
            profile_result_id="result.profiles.outer",
            center_x_mm=0.0,
            center_y_mm=0.0,
            half_width_mm=20.0,
            half_height_mm=10.0,
            rotation_radians=0.0,
        ),
        circles=tuple(circles),
    )


def _plan_document(plan: PlanarMechanicalBackendPlan) -> DocumentRef:
    payload = plan.canonical_bytes
    return DocumentRef(
        artifact_id=f"artifact.plan.{plan.plan_sha256[:16]}",
        role_term_ref_id=PLANAR_PLAN_DOCUMENT_ROLE_TERM.term_ref_id,
        schema_term_ref_id=PLANAR_PLAN_SCHEMA_TERM.term_ref_id,
        document_id=f"document.plan.{plan.plan_sha256[:16]}",
        document_digest=plan.plan_sha256,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        media_type=PLANAR_MECHANICAL_PLAN_MEDIA_TYPE,
    )


class _Shape:
    def __init__(self, token: str, *, volume: float = 0.0, solid: bool = False) -> None:
        self.token = token
        self.Volume = volume
        self.Solids = (object(),) if solid else ()

    def isNull(self) -> bool:
        return False

    def isValid(self) -> bool:
        return True

    def exportBrepToString(self) -> str:
        return self.token


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
        volume: float = 0.0,
        solid: bool = False,
    ) -> None:
        super().__init__(document, name, type_id)
        self.Shape = _Shape(name, volume=volume, solid=solid)


class _Origin(_Object):
    def __init__(self, document: _Document, name: str) -> None:
        super().__init__(document, name, "App::Origin")
        self.Group: tuple[object, ...] = ()
        type_ids = (
            "App::Line",
            "App::Line",
            "App::Line",
            "App::Plane",
            "App::Plane",
            "App::Plane",
            "App::Point",
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
        helpers = []
        for index, (type_id, role) in enumerate(zip(type_ids, roles, strict=True)):
            helper = _Object(document, f"{name}_Helper_{index}", type_id)
            helper.Role = role
            helper.InList = (self,)
            helpers.append(helper)
        self.OriginFeatures = tuple(helpers)


class _Body(_Object):
    def __init__(self, document: _Document, name: str) -> None:
        super().__init__(document, name, "PartDesign::Body")
        self.Origin = _Origin(document, f"{name}_Origin")
        self.Group: tuple[object, ...] = ()
        self.Tip = None


class _Document:
    def __init__(self) -> None:
        self.UndoMode = 1
        self.HasPendingTransaction = False
        self.Objects: list[object] = []

    def getObject(self, name: str):
        return next((item for item in self.Objects if item.Name == name), None)

    def removeObject(self, name: str) -> None:
        item = self.getObject(name)
        if item is None:
            return
        if item.TypeId == "PartDesign::Body":
            owned = getattr(item, "_transaction_objects", ())
            self.Objects[:] = [
                existing
                for existing in self.Objects
                if not any(existing is member for member in owned)
            ]
            return
        self.Objects.remove(item)

    def recompute(self) -> None:
        return None


@dataclass(slots=True)
class _Session:
    doc: _Document


def _fake_apply(
    document: _Document,
    plan: PlanarMechanicalBackendPlan,
    *,
    wrong_order: bool = False,
    bad_visibility: bool = False,
) -> PlanarMechanicalConformanceReceipt:
    token = plan.plan_sha256[:16]
    body = _Body(document, f"PM1_Body_{token}")
    origin = body.Origin
    outer = _Feature(document, f"PM1_Outer_{token}", "Sketcher::SketchObject")
    pad = _Feature(
        document,
        f"PM1_Pad_{token}",
        "PartDesign::Pad",
        volume=plan.expected_volume_mm3,
        solid=True,
    )
    pad.Profile = (outer, ())
    pad.Type = "Length"
    pad.Length = plan.depth_mm
    pad.Midplane = False
    pad.Reversed = False
    pad.Refine = True
    pad.AllowMultiFace = False
    circle_sketches = tuple(
        _Feature(
            document,
            f"PM1_Circle_{token}_{index:02d}",
            "Sketcher::SketchObject",
        )
        for index in range(len(plan.circles))
    )
    pockets = []
    previous = pad
    for index, sketch in enumerate(circle_sketches):
        pocket = _Feature(
            document,
            f"PM1_Pocket_{token}_{index:02d}",
            "PartDesign::Pocket",
            volume=plan.expected_volume_mm3,
            solid=True,
        )
        pocket.Profile = (sketch, ())
        pocket.BaseFeature = previous
        pocket.Type = "ThroughAll"
        pocket.SideType = "One side"
        pocket.AlongSketchNormal = True
        pocket.UseCustomVector = False
        pocket.Offset = 0.0
        pocket.Offset2 = 0.0
        pocket.TaperAngle = 0.0
        pocket.TaperAngle2 = 0.0
        pocket.Reversed = True
        pocket.Refine = True
        pockets.append(pocket)
        previous = pocket
    pockets = tuple(pockets)
    pad.Visibility = not pockets
    for index, pocket in enumerate(pockets):
        pocket.Visibility = index == len(pockets) - 1
    if bad_visibility:
        pad.Visibility = True
    group: list[object] = [outer, pad]
    for sketch, pocket in zip(circle_sketches, pockets, strict=True):
        group.extend((sketch, pocket))
    body.Group = tuple(group)
    body.Tip = pockets[-1] if pockets else pad
    additions: list[object] = [body, origin, *origin.OriginFeatures, *group]
    body._transaction_objects = tuple(additions)
    if wrong_order:
        additions[-2], additions[-1] = additions[-1], additions[-2]
    document.Objects.extend(additions)
    return PlanarMechanicalConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        body_name=body.Name,
        outer_sketch_name=outer.Name,
        pad_name=pad.Name,
        circle_sketch_names=tuple(item.Name for item in circle_sketches),
        pocket_names=tuple(item.Name for item in pockets),
        volume_mm3=float(body.Tip.Shape.Volume),
    )


def _install_fake_apply(
    monkeypatch: pytest.MonkeyPatch,
    plan: PlanarMechanicalBackendPlan,
    *,
    wrong_order: bool = False,
    bad_visibility: bool = False,
) -> None:
    def apply(
        payload: bytes,
        *,
        expected_content_sha256: str,
        expected_plan_sha256: str,
        bindings,
    ) -> PlanarMechanicalConformanceReceipt:
        assert payload == plan.canonical_bytes
        assert expected_content_sha256 == hashlib.sha256(payload).hexdigest()
        assert expected_plan_sha256 == plan.plan_sha256
        return _fake_apply(
            bindings.document,
            plan,
            wrong_order=wrong_order,
            bad_visibility=bad_visibility,
        )

    monkeypatch.setattr(pm_execution, "apply_planar_mechanical_plan", apply)


def test_handoff_is_exact_and_intentionally_not_current() -> None:
    handoff = PLANAR_MECHANICAL_REVIEWED_PRODUCT_HANDOFF
    assert handoff.lowering_ready is False
    assert handoff.minimum_source_results == handoff.maximum_source_results == 0
    assert len(handoff.required_intent_media_types) == 2
    assert len(handoff.required_proof_media_types) == 3
    assert len(handoff.handoff_sha256) == 64
    ids = {item.operation_id for item in PLANAR_MECHANICAL_REVIEWED_OPERATION_SPECS}
    assert ids.isdisjoint(item.operation_id for item in CURRENT_REVIEWED_INTENT_ROUTES)
    for operation in PLANAR_MECHANICAL_REVIEWED_OPERATION_SPECS:
        assert (
            resolve_planar_mechanical_reviewed_operation(
                operation.operation_id,
                _semantic_operation(operation),
            )
            is operation
        )
        assert (
            resolve_planar_mechanical_reviewed_operation(
                operation.operation_id,
                _semantic_operation(operation)[:-1] + "0",
            )
            is None
        )


@pytest.mark.parametrize(
    ("suffix", "circle_count", "primary_type", "primary_role", "owned_count"),
    (
        ("reference-profiles", 0, "Sketcher::SketchObject", SemanticRole.SUPPORT, 11),
        (".add", 2, "PartDesign::Pad", SemanticRole.FEATURE, 15),
        (".remove", 2, "PartDesign::Pocket", SemanticRole.FEATURE, 15),
    ),
)
def test_dynamic_contract_declares_the_complete_transaction(
    suffix: str,
    circle_count: int,
    primary_type: str,
    primary_role: SemanticRole,
    owned_count: int,
) -> None:
    contract = resolve_planar_mechanical_product_contract(
        _plan(circle_count),
        _operation(suffix),
    )
    assert contract.owned_type_ids[0] == primary_type
    assert contract.semantic_roles[0] is primary_role
    assert len(contract.owned_type_ids) == len(contract.semantic_roles) == owned_count
    assert contract.owned_type_ids.count("PartDesign::Body") == 1
    assert contract.owned_type_ids.count("App::Origin") == 1
    assert contract.owned_type_ids.count("PartDesign::Pad") == 1
    assert contract.owned_type_ids.count("PartDesign::Pocket") == circle_count


def test_remove_without_a_circle_is_rejected() -> None:
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        resolve_planar_mechanical_product_contract(_plan(0), _operation(".remove"))
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize(
    ("suffix", "circle_count", "expected_primary"),
    (
        ("reference-profiles", 0, "Sketcher::SketchObject"),
        (".add", 1, "PartDesign::Pad"),
        (".remove", 1, "PartDesign::Pocket"),
    ),
)
def test_native_callback_returns_primary_first_full_closure(
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    circle_count: int,
    expected_primary: str,
) -> None:
    plan = _plan(circle_count)
    document = _Document()
    sentinel = _Object(document, "Existing", "PartDesign::Feature")
    document.Objects.append(sentinel)
    _install_fake_apply(monkeypatch, plan)
    operation = _operation(suffix)
    execution = execute_planar_mechanical_reviewed_plan_with_sources(
        document,
        plan,
        plan.canonical_bytes,
        _plan_document(plan),
        operation,
        (),
        session=_Session(document),
    )
    assert execution.object.TypeId == expected_primary
    assert execution.owned_objects[0] is execution.object
    assert len(execution.owned_objects) == 11 + 2 * circle_count
    assert document.Objects[0] is sentinel
    assert isinstance(execution.receipt, PlanarMechanicalOwnershipClosure)
    execution.receipt.validate_native_result(document, execution.object)
    resolve_planar_mechanical_product_contract(plan, operation).validate(
        operation,
        execution.object,
        execution.owned_objects,
    )


def test_tamper_and_source_cardinality_fail_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(1)
    operation = _operation(".remove")
    document = _Document()
    calls = 0

    def forbidden(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("native mutation must not run")

    monkeypatch.setattr(pm_execution, "apply_planar_mechanical_plan", forbidden)
    with pytest.raises(ReviewedIntentExecutionError) as tampered:
        execute_planar_mechanical_reviewed_plan_with_sources(
            document,
            plan,
            plan.canonical_bytes + b" ",
            _plan_document(plan),
            operation,
            (),
            session=_Session(document),
        )
    assert tampered.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    with pytest.raises(ReviewedIntentExecutionError) as extra_source:
        execute_planar_mechanical_reviewed_plan_with_sources(
            document,
            plan,
            plan.canonical_bytes,
            _plan_document(plan),
            operation,
            (object(),),
            session=_Session(document),
        )
    assert extra_source.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert calls == 0
    assert document.Objects == []


@pytest.mark.parametrize(("wrong_order", "bad_visibility"), ((True, False), (False, True)))
def test_post_native_conformance_failure_restores_exact_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    wrong_order: bool,
    bad_visibility: bool,
) -> None:
    plan = _plan(1)
    document = _Document()
    sentinel = _Object(document, "Existing", "PartDesign::Feature")
    document.Objects.append(sentinel)
    _install_fake_apply(
        monkeypatch,
        plan,
        wrong_order=wrong_order,
        bad_visibility=bad_visibility,
    )
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_planar_mechanical_reviewed_plan_with_sources(
            document,
            plan,
            plan.canonical_bytes,
            _plan_document(plan),
            _operation(".remove"),
            (),
            session=_Session(document),
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.EXECUTION_FAILED
    assert document.Objects == [sentinel]
    assert document.HasPendingTransaction is False


def test_owned_closure_rejects_stale_shape_and_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(2)
    document = _Document()
    _install_fake_apply(monkeypatch, plan)
    execution = execute_planar_mechanical_reviewed_plan_with_sources(
        document,
        plan,
        plan.canonical_bytes,
        _plan_document(plan),
        _operation(".remove"),
        (),
        session=_Session(document),
    )
    ownership = execution.receipt
    ownership.primary.Shape.token += ":stale"
    with pytest.raises(ReviewedIntentExecutionError) as stale:
        ownership.validate_native_result(document, execution.object)
    assert stale.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE

    ownership.primary.Shape.token = ownership.primary.Name
    ownership.pockets[0].Visibility = True
    with pytest.raises(ReviewedIntentExecutionError) as visibility:
        ownership.validate_native_result(document, execution.object)
    assert visibility.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


def test_wrong_operation_contract_is_rejected_pre_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(1)
    document = _Document()
    operation = dataclasses.replace(_operation(".remove"), native_type_id="PartDesign::Pad")
    monkeypatch.setattr(
        pm_execution,
        "apply_planar_mechanical_plan",
        lambda *args, **kwargs: pytest.fail("must not mutate"),
    )
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_planar_mechanical_reviewed_plan_with_sources(
            document,
            plan,
            plan.canonical_bytes,
            _plan_document(plan),
            operation,
            (),
            session=_Session(document),
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert document.Objects == []
