from __future__ import annotations

import dataclasses
import hashlib
import math
import sys
from types import ModuleType, SimpleNamespace

import pytest

import vibecad.execution.freecad_part_datum_reviewed_execution as datum_execution
import vibecad.execution.freecad_reviewed_intent_execution as shared_execution
import vibecad.parametric.freecad_part_datum_rules as datum_rules
from test_intent_bridge_freecad_part_datum_adapter import _graph
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_part_datum_reviewed_execution import (
    PART_DATUM_REVIEWED_FAMILY_SPEC,
    PART_DATUM_REVIEWED_PRODUCT_IDENTITIES,
    PART_DATUM_REVIEWED_PRODUCT_OPERATIONS,
    execute_part_datum_reviewed_plan,
    part_datum_reviewed_adapter_factory,
    resolve_part_datum_reviewed_operation,
    validate_part_datum_reviewed_plan,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PART_DATUM_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    execute_reviewed_intent_native,
    lower_reviewed_intent,
    route_reviewed_intent,
)
from vibecad.intent_bridge.contracts import DocumentRef
from vibecad.intent_bridge.freecad_part_datum_adapter import (
    PART_DATUM_MANIFEST,
    FreeCADPartDatumAdapter,
)
from vibecad.parametric.freecad_part_datum_rules import (
    PART_DATUM_FREECAD_ENGINE_BUILD_ID,
    PART_DATUM_NATIVE_TYPE_IDS,
    PartDatumBackendPlan,
    PartDatumConformanceReceipt,
    PartDatumOperation,
    PartDatumRuleError,
    PartDatumRuleErrorCode,
)
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1


def _reviewed_operation(operation: PartDatumOperation):
    return next(
        item for item in PART_DATUM_MANIFEST.operations if item.operation_id == operation.value
    )


def _program(operation: PartDatumOperation) -> ReviewedIntentProgramV1:
    graph = _graph(operation)
    reviewed = _reviewed_operation(operation)
    namespace, version, term_id, digest = reviewed.semantic_term.semantic_identity
    return ReviewedIntentProgramV1(
        operation_id=f"{PART_DATUM_MANIFEST.family_id}.{operation.value}",
        semantic_operation=f"{namespace}/{version}/{term_id}@{digest}",
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


class _Sink:
    def __init__(self) -> None:
        self.items: dict[str, tuple[DocumentRef, bytes]] = {}

    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        self.items[document.artifact_id] = (document, payload)
        return payload

    def read_exact(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        expected, payload = self.items[document.artifact_id]
        assert expected == document and len(payload) <= maximum_bytes
        return payload


class _Body:
    TypeId = "PartDesign::Body"

    def __init__(self) -> None:
        self.Tip = object()


class _Feature:
    def __init__(self, document: _Document, name: str, type_id: str) -> None:
        self.Document = document
        self.Name = name
        self.TypeId = type_id
        self.State = ("Up-to-date",)
        self.OriginFeatures: tuple[object, ...] = ()

    def isValid(self) -> bool:  # noqa: N802 - FreeCAD API spelling
        return True


class _Document:
    def __init__(self) -> None:
        self.body = _Body()
        self.Objects: tuple[object, ...] = (self.body,)

    def getObject(self, name: str):
        return next((item for item in self.Objects if getattr(item, "Name", None) == name), None)


def _install_native_apply(
    monkeypatch: pytest.MonkeyPatch,
    *,
    document: _Document,
    plan: PartDatumBackendPlan,
    mutate_tip: bool = False,
) -> None:
    operation = plan.operation
    native_type_id = PART_DATUM_NATIVE_TYPE_IDS[operation]

    def apply(raw, *, expected_content_sha256, expected_plan_sha256, bindings):
        assert raw == plan.canonical_bytes
        assert expected_content_sha256 == hashlib.sha256(raw).hexdigest()
        assert expected_plan_sha256 == plan.plan_sha256
        assert bindings.document is document
        assert bindings.container_id == plan.container_id
        if mutate_tip:
            document.body.Tip = object()
        primary = _Feature(
            document,
            f"VcPartDatum_{operation.value}_{plan.plan_sha256[:16]}",
            native_type_id,
        )
        owned: tuple[_Feature, ...] = (primary,)
        if operation is PartDatumOperation.LOCAL_COORDINATE_SYSTEM:
            helper_types = (
                "App::Line",
                "App::Line",
                "App::Line",
                "App::Plane",
                "App::Plane",
                "App::Plane",
                "App::Point",
            )
            helpers = tuple(
                _Feature(document, f"{primary.Name}_Helper{index}", type_id)
                for index, type_id in enumerate(helper_types)
            )
            primary.OriginFeatures = helpers
            owned = (primary, *helpers)
        document.Objects = (*document.Objects, *owned)
        return PartDatumConformanceReceipt(
            plan_sha256=plan.plan_sha256,
            operation=operation,
            object_name=primary.Name,
            native_type_id=native_type_id,
            owned_object_names=tuple(item.Name for item in owned),
        )

    monkeypatch.setattr(datum_execution, "apply_part_datum_plan", apply)


def test_datum_family_spec_is_exactly_four_static_product_routes() -> None:
    assert PART_DATUM_REVIEWED_PRODUCT_OPERATIONS == tuple(PartDatumOperation)
    assert PART_DATUM_REVIEWED_FAMILY_SPEC.operation_ids == tuple(
        item.value for item in PartDatumOperation
    )
    assert (
        tuple(
            (route.operation_id, route.semantic_operation) for route in REVIEWED_PART_DATUM_ROUTES
        )
        == PART_DATUM_REVIEWED_PRODUCT_IDENTITIES
    )
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 33
    assert {route.operation.native_type_id for route in REVIEWED_PART_DATUM_ROUTES} == {
        "Part::DatumLine",
        "Part::DatumPlane",
        "Part::DatumPoint",
        "Part::LocalCoordinateSystem",
    }
    contracts = tuple(
        route.family.product_result(route.operation) for route in REVIEWED_PART_DATUM_ROUTES
    )
    assert {item.result_kind.value for item in contracts} == {"reference"}
    assert tuple(len(item.owned_type_ids) for item in contracts) == (1, 1, 1, 8)
    assert all(
        tuple(role.value for role in item.semantic_roles) == ("support",) * len(item.owned_type_ids)
        for item in contracts
    )
    assert contracts[-1].owned_type_ids == (
        "Part::LocalCoordinateSystem",
        "App::Line",
        "App::Line",
        "App::Line",
        "App::Plane",
        "App::Plane",
        "App::Plane",
        "App::Point",
    )
    formal = current_freecad_intent_capability_specs()
    for identity, route in zip(
        PART_DATUM_REVIEWED_PRODUCT_IDENTITIES,
        REVIEWED_PART_DATUM_ROUTES,
        strict=True,
    ):
        assert resolve_part_datum_reviewed_operation(*identity) is route.operation
        matching = tuple(item for item in formal if item.operation_id == identity[0])
        assert len(matching) == 1
        assert matching[0].semantic_operation == identity[1]
        assert matching[0].native_type_id == route.operation.native_type_id
    adapter = part_datum_reviewed_adapter_factory(_Sink())
    assert type(adapter) is FreeCADPartDatumAdapter
    assert adapter.manifest is PART_DATUM_MANIFEST


def test_datum_identity_router_rejects_partial_and_tampered_identities() -> None:
    operation_id, semantic_operation = PART_DATUM_REVIEWED_PRODUCT_IDENTITIES[0]
    assert resolve_part_datum_reviewed_operation(operation_id, semantic_operation) is not None
    assert (
        resolve_part_datum_reviewed_operation(operation_id + "_alias", semantic_operation) is None
    )
    assert (
        resolve_part_datum_reviewed_operation(operation_id, semantic_operation[:-1] + "0") is None
    )
    assert resolve_part_datum_reviewed_operation(None, semantic_operation) is None


@pytest.mark.parametrize("operation", PART_DATUM_REVIEWED_PRODUCT_OPERATIONS)
def test_shared_product_bridge_routes_and_lowers_all_four_datums(
    operation: PartDatumOperation,
) -> None:
    program = _program(operation)

    route = route_reviewed_intent(program)
    lowered = lower_reviewed_intent(program)

    assert route in REVIEWED_PART_DATUM_ROUTES
    assert lowered.route is route
    assert type(lowered.plan) is PartDatumBackendPlan
    assert lowered.plan.operation is operation
    assert lowered.plan.placement.position_mm == (10.0, 20.0, 30.0)
    assert lowered.receipt.operation is route.operation
    assert lowered.result.plan_document.document_digest == lowered.plan.plan_sha256


def test_datum_plan_validator_rejects_rebound_receipt() -> None:
    lowered = lower_reviewed_intent(_program(PartDatumOperation.DATUM_PLANE))
    rebound = dataclasses.replace(lowered.receipt, request_digest="f" * 64)

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        validate_part_datum_reviewed_plan(lowered.plan, rebound, lowered.route.operation)

    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize("operation", PART_DATUM_REVIEWED_PRODUCT_OPERATIONS)
def test_shared_native_execution_accepts_exact_datum_ownership_closure(
    monkeypatch: pytest.MonkeyPatch,
    operation: PartDatumOperation,
) -> None:
    program = _program(operation)
    lowered = lower_reviewed_intent(program)
    document = _Document()
    original_tip = document.body.Tip
    _install_native_apply(monkeypatch, document=document, plan=lowered.plan)
    monkeypatch.setattr(
        shared_execution,
        "require_reviewed_route_verified",
        lambda route, *, freecad: None,
    )
    monkeypatch.setitem(sys.modules, "FreeCAD", ModuleType("FreeCAD"))

    result = execute_reviewed_intent_native(type("Session", (), {"doc": document})(), program)

    expected_owned = 8 if operation is PartDatumOperation.LOCAL_COORDINATE_SYSTEM else 1
    assert result.object is document.Objects[1]
    assert result.object.TypeId == PART_DATUM_NATIVE_TYPE_IDS[operation]
    assert len(result.native_receipt.owned_object_names) == expected_owned
    assert len(document.Objects) == 1 + expected_owned
    assert document.body.Tip is original_tip


def test_datum_callback_rejects_body_tip_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lowered = lower_reviewed_intent(_program(PartDatumOperation.DATUM_POINT))
    document = _Document()
    _install_native_apply(
        monkeypatch,
        document=document,
        plan=lowered.plan,
        mutate_tip=True,
    )

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_part_datum_reviewed_plan(
            document,
            lowered.plan,
            lowered.payload,
            lowered.result.plan_document,
            lowered.route.operation,
            shared_execution._ReviewedFamilyExecutionContext(
                session=SimpleNamespace(doc=document),
                document=document,
                source_results=(),
            ),
        )

    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


class _RuleVector:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x = x
        self.y = y
        self.z = z


class _RuleRotation:
    def __init__(self, axis: _RuleVector, angle_degrees: float) -> None:
        half_angle = math.radians(angle_degrees) / 2.0
        sine = math.sin(half_angle)
        self.Q = (
            axis.x * sine,
            axis.y * sine,
            axis.z * sine,
            math.cos(half_angle),
        )


class _RulePlacement:
    def __init__(self, base: _RuleVector, rotation: _RuleRotation) -> None:
        self.Base = base
        self.Rotation = rotation


class _RuleBody:
    TypeId = "PartDesign::Body"
    PropertiesList: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.Tip = object()


class _RuleFeature:
    PropertiesList: tuple[str, ...] = ()

    def __init__(self, document: _RuleDocument, name: str, type_id: str) -> None:
        self.Document = document
        self.Name = name
        self.TypeId = type_id
        self.State = ("Up-to-date",)
        self.MapMode = "Deactivated"
        self.AttachmentSupport: list[object] = []
        self.Group: tuple[object, ...] = ()
        self.OriginFeatures: tuple[_RuleFeature, ...] = ()
        self.InList: tuple[object, ...] = ()
        self.Role = ""
        self.Placement = _RulePlacement(
            _RuleVector(0.0, 0.0, 0.0),
            _RuleRotation(_RuleVector(0.0, 0.0, 1.0), 0.0),
        )

    def getParentGroup(self):  # noqa: N802 - FreeCAD API spelling
        return None

    def isValid(self) -> bool:  # noqa: N802 - FreeCAD API spelling
        return True


class _RuleDocument:
    def __init__(
        self,
        operation: PartDatumOperation,
        *,
        helper_count: int = 7,
        mutate_tip: bool = False,
    ) -> None:
        self.operation = operation
        self.helper_count = helper_count
        self.mutate_tip = mutate_tip
        self.body = _RuleBody()
        self.Objects: tuple[object, ...] = (self.body,)
        self.HasPendingTransaction = False
        self._before: tuple[tuple[object, ...], object] | None = None

    def getObject(self, name: str):  # noqa: N802 - FreeCAD API spelling
        return next((item for item in self.Objects if getattr(item, "Name", None) == name), None)

    def addObject(self, type_id: str, name: str):  # noqa: N802 - FreeCAD API spelling
        feature = _RuleFeature(self, name, type_id)
        created: tuple[_RuleFeature, ...] = (feature,)
        if self.operation is PartDatumOperation.LOCAL_COORDINATE_SYSTEM:
            helper_specs = (
                ("X_Axis", "App::Line"),
                ("Y_Axis", "App::Line"),
                ("Z_Axis", "App::Line"),
                ("XY_Plane", "App::Plane"),
                ("XZ_Plane", "App::Plane"),
                ("YZ_Plane", "App::Plane"),
                ("Origin", "App::Point"),
                ("Extra", "App::Point"),
            )[: self.helper_count]
            helpers = tuple(
                _RuleFeature(self, f"{name}_{role}", helper_type)
                for role, helper_type in helper_specs
            )
            for helper, (role, _helper_type) in zip(helpers, helper_specs, strict=True):
                helper.Role = role
                helper.InList = (feature,)
            feature.OriginFeatures = helpers
            created = (feature, *helpers)
        if self.mutate_tip:
            self.body.Tip = object()
        self.Objects = (*self.Objects, *created)
        return feature

    def openTransaction(self, _label: str) -> None:  # noqa: N802 - FreeCAD API spelling
        self._before = (tuple(self.Objects), self.body.Tip)

    def commitTransaction(self) -> None:  # noqa: N802 - FreeCAD API spelling
        self._before = None

    def abortTransaction(self) -> None:  # noqa: N802 - FreeCAD API spelling
        assert self._before is not None
        self.Objects, self.body.Tip = self._before
        self._before = None

    def recompute(self) -> None:
        return None


@pytest.mark.parametrize(
    ("operation", "helper_count", "mutate_tip"),
    (
        (PartDatumOperation.LOCAL_COORDINATE_SYSTEM, 6, False),
        (PartDatumOperation.LOCAL_COORDINATE_SYSTEM, 8, False),
        (PartDatumOperation.DATUM_POINT, 7, True),
    ),
)
def test_datum_rule_rolls_back_missing_extra_helpers_and_body_tip_drift(
    monkeypatch: pytest.MonkeyPatch,
    operation: PartDatumOperation,
    helper_count: int,
    mutate_tip: bool,
) -> None:
    lowered = lower_reviewed_intent(_program(operation))
    document = _RuleDocument(
        operation,
        helper_count=helper_count,
        mutate_tip=mutate_tip,
    )
    before_objects = tuple(document.Objects)
    before_tip = document.body.Tip
    freecad = ModuleType("FreeCAD")
    freecad.Version = lambda: (  # type: ignore[attr-defined]
        "1",
        "1",
        "0",
        "",
        "",
        "",
        "",
        PART_DATUM_FREECAD_ENGINE_BUILD_ID,
    )
    freecad.Vector = _RuleVector  # type: ignore[attr-defined]
    freecad.Rotation = _RuleRotation  # type: ignore[attr-defined]
    freecad.Placement = _RulePlacement  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "FreeCAD", freecad)

    with pytest.raises(PartDatumRuleError) as caught:
        datum_rules.apply_part_datum_plan(
            lowered.payload,
            expected_content_sha256=lowered.result.plan_document.content_sha256,
            expected_plan_sha256=lowered.result.plan_document.document_digest,
            bindings=datum_rules.PartDatumExecutionBindings(
                document=document,
                container_id=lowered.plan.container_id,
            ),
        )

    assert caught.value.code is PartDatumRuleErrorCode.TRANSACTION_FAILED
    assert tuple(document.Objects) == before_objects
    assert document.body.Tip is before_tip
    assert document.HasPendingTransaction is False
