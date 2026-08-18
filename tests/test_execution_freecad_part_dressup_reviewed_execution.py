from __future__ import annotations

import dataclasses
import hashlib
from types import SimpleNamespace

import pytest

import vibecad.execution.freecad_part_dressup_reviewed_execution as dressup_execution
import vibecad.parametric.freecad_part_dressup_rules as dressup_rules
from tests.test_intent_bridge_freecad_part_dressup_adapter import (
    _graph,
    _lower,
    _MemoryPlanSink,
    _request,
)
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_part_dressup_reviewed_execution import (
    PART_DRESSUP_REQUIRED_SOURCE_ROLES,
    PART_DRESSUP_RESULT_INVARIANTS,
    PART_DRESSUP_REVIEWED_FAMILY_SPEC,
    PART_DRESSUP_REVIEWED_PRODUCT_IDENTITIES,
    PART_DRESSUP_REVIEWED_PRODUCT_OPERATIONS,
    build_part_dressup_reviewed_family_descriptor,
    execute_part_dressup_reviewed_plan,
    resolve_part_dressup_reviewed_operation,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PART_BOX_ROUTE,
    REVIEWED_PART_DRESSUP_ROUTES,
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
from vibecad.intent_bridge.freecad_part_dressup_adapter import (
    PART_DRESSUP_MANIFEST,
    FreeCADPartDressupAdapter,
)
from vibecad.parametric.freecad_part_core_rules import (
    PartCoreConformanceReceipt,
    PartCoreOperation,
)
from vibecad.parametric.freecad_part_dressup_rules import (
    PART_DRESSUP_NATIVE_TYPE_IDS,
    PartDressupConformanceReceipt,
    PartDressupOperation,
    PartDressupRuleError,
    PartDressupRuleErrorCode,
)
from vibecad.validation import EntityObservation


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _lowered(operation: PartDressupOperation):
    request, reader, policy = _request(_graph(operation))
    adapter = FreeCADPartDressupAdapter(_MemoryPlanSink())
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    return result, receipt, plan, payload


class _Point:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x = x
        self.y = y
        self.z = z


class _Vertex:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.Point = _Point(x, y, z)


class _Edge:
    Curve = SimpleNamespace(TypeId="Part::GeomLine")

    def __init__(self) -> None:
        self.Vertexes = (_Vertex(30.0, 20.0, 0.0), _Vertex(30.0, 20.0, 10.0))


class _Face:
    Surface = SimpleNamespace(TypeId="Part::GeomPlane")
    ParameterRange = (0.0, 1.0, 0.0, 1.0)

    def __init__(self) -> None:
        self.Vertexes = (
            _Vertex(0.0, 0.0, 10.0),
            _Vertex(30.0, 0.0, 10.0),
            _Vertex(30.0, 20.0, 10.0),
            _Vertex(0.0, 20.0, 10.0),
        )

    def normalAt(self, _u: float, _v: float):  # noqa: N802 - FreeCAD API
        return _Point(0.0, 0.0, 1.0)


class _Shape:
    def __init__(
        self,
        value: str,
        volume: float,
        *,
        ambiguous_edge: bool = False,
        ambiguous_face: bool = False,
    ) -> None:
        self._value = value
        self.ShapeType = "Solid"
        self.Volume = volume
        self.Solids = (object(),)
        self.BoundBox = SimpleNamespace(
            XMax=30.0,
            YMax=20.0,
            ZMax=10.0,
            XLength=30.0,
            YLength=20.0,
            ZLength=10.0,
        )
        self.Edges = (_Edge(), _Edge()) if ambiguous_edge else (_Edge(),)
        self.Faces = (_Face(), _Face()) if ambiguous_face else (_Face(),)

    def exportBrepToString(self) -> str:  # noqa: N802 - FreeCAD API
        return self._value

    def isNull(self) -> bool:  # noqa: N802 - FreeCAD API
        return False

    def isValid(self) -> bool:  # noqa: N802 - FreeCAD API
        return True

    def mutate(self) -> None:
        self._value += "-stale"


class _Feature:
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

    def isValid(self) -> bool:  # noqa: N802 - FreeCAD API
        return True

    def getParentGroup(self):  # noqa: N802 - FreeCAD API
        return None


class _Document:
    def __init__(self) -> None:
        self.Objects: list[object] = []

    def getObject(self, name: str):  # noqa: N802 - FreeCAD API
        return next((item for item in self.Objects if getattr(item, "Name", None) == name), None)


class _Session:
    def __init__(self, document: _Document, identities: dict[object, EntityIdentity]) -> None:
        self.doc = document
        self.identities = identities

    def read_object_identity(self, item: object) -> EntityIdentity:
        return self.identities[item]


def _source_result(
    document: _Document,
    *,
    provenance_source: ProvenanceSource = ProvenanceSource.MODEL,
) -> tuple[ReviewedNativeExecutionResult, dict[object, EntityIdentity], object]:
    source = _Feature(
        document,
        name="SourceBox",
        type_id="Part::Box",
        shape=_Shape("source-brep", 100.0),
    )
    document.Objects.append(source)
    plan_sha256 = _sha("source-plan")
    receipt = PartCoreConformanceReceipt(
        plan_sha256=plan_sha256,
        operation=PartCoreOperation.BOX,
        object_name=source.Name,
        source_shape_sha256s=(),
        result_shape_sha256=_sha("source-brep"),
    )
    result = ReviewedNativeExecutionResult(
        route=REVIEWED_PART_BOX_ROUTE,
        object=source,
        plan_sha256=plan_sha256,
        plan_content_sha256=_sha("source-content"),
        native_receipt=receipt,
    )
    identities = {
        source: EntityIdentity(
            object_id="object_" + "1" * 32,
            feature_id="feature_" + "2" * 32,
            object_type="Part::Box",
            semantic_role=SemanticRole.PRIMITIVE,
            provenance=Provenance(
                source=provenance_source,
                operation_id="apply_reviewed_intent",
            ),
        )
    }
    token = object()
    result._retain_for_run(token)  # noqa: SLF001 - exercise the engine-owned run contract
    return result, identities, token


def _native_result(
    document: _Document,
    operation: PartDressupOperation,
    source: _Feature,
    magnitude_mm: float,
) -> _Feature:
    result = _Feature(
        document,
        name=f"Result_{operation.value}",
        type_id=PART_DRESSUP_NATIVE_TYPE_IDS[operation],
        shape=_Shape(f"result-{operation.value}", 80.0),
    )
    if operation in {PartDressupOperation.EDGE_FILLET, PartDressupOperation.EDGE_CHAMFER}:
        result.Base = source
        result.EdgeLinks = (source, ["Edge1"])
        result.Edges = [(1, magnitude_mm, magnitude_mm)]
    else:
        result.Faces = (source, ["Face1"])
        result.Value = magnitude_mm
        result.Mode = "Skin"
        result.Join = "Arc"
        result.Intersection = False
        result.SelfIntersection = False
    return result


def test_part_dressup_family_payload_is_exact_and_registered_append_only() -> None:
    family = build_part_dressup_reviewed_family_descriptor()

    assert PART_DRESSUP_REVIEWED_PRODUCT_OPERATIONS == tuple(PartDressupOperation)
    assert PART_DRESSUP_REVIEWED_FAMILY_SPEC.manifest is PART_DRESSUP_MANIFEST
    assert PART_DRESSUP_REVIEWED_FAMILY_SPEC.operation_ids == tuple(
        item.value for item in PartDressupOperation
    )
    assert PART_DRESSUP_REQUIRED_SOURCE_ROLES == {
        operation.value: ("source_solid",) for operation in PartDressupOperation
    }
    assert PART_DRESSUP_REVIEWED_FAMILY_SPEC.result_invariants is (PART_DRESSUP_RESULT_INVARIANTS)
    assert PART_DRESSUP_REVIEWED_FAMILY_SPEC.requires_same_run_sources is True
    assert family.minimum_sources == family.maximum_sources == 1
    assert family.requires_same_run_sources is True
    assert len(family.product_results) == 3
    assert {item.result_kind.value for item in family.product_results} == {"solid"}
    assert {item.semantic_roles for item in family.product_results} == {(SemanticRole.FEATURE,)}
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 126
    assert CURRENT_REVIEWED_INTENT_ROUTES[90:93] == REVIEWED_PART_DRESSUP_ROUTES
    assert tuple(route.operation_id for route in REVIEWED_PART_DRESSUP_ROUTES) == tuple(
        item[0] for item in PART_DRESSUP_REVIEWED_PRODUCT_IDENTITIES
    )

    formal = current_freecad_intent_capability_specs()
    for operation, identity in zip(
        PART_DRESSUP_REVIEWED_PRODUCT_OPERATIONS,
        PART_DRESSUP_REVIEWED_PRODUCT_IDENTITIES,
        strict=True,
    ):
        reviewed = resolve_part_dressup_reviewed_operation(*identity)
        assert reviewed is not None and reviewed.operation_id == operation.value
        matching = tuple(item for item in formal if item.operation_id == identity[0])
        assert len(matching) == 1
        assert matching[0].semantic_operation == identity[1]
        assert matching[0].native_type_id == PART_DRESSUP_NATIVE_TYPE_IDS[operation]

    adapter = family.build_adapter(_MemoryPlanSink())
    assert type(adapter) is FreeCADPartDressupAdapter
    assert adapter.manifest is PART_DRESSUP_MANIFEST


def test_part_dressup_identity_resolver_rejects_partial_and_tampered_values() -> None:
    operation_id, semantic_operation = PART_DRESSUP_REVIEWED_PRODUCT_IDENTITIES[0]
    assert resolve_part_dressup_reviewed_operation(operation_id, semantic_operation) is not None
    assert (
        resolve_part_dressup_reviewed_operation(operation_id + "_alias", semantic_operation) is None
    )
    assert (
        resolve_part_dressup_reviewed_operation(operation_id, semantic_operation[:-1] + "0") is None
    )
    assert resolve_part_dressup_reviewed_operation(None, semantic_operation) is None


@pytest.mark.parametrize("operation", PART_DRESSUP_REVIEWED_PRODUCT_OPERATIONS)
def test_part_dressup_native_callback_closes_same_run_source_and_solid_result(
    monkeypatch: pytest.MonkeyPatch,
    operation: PartDressupOperation,
) -> None:
    lowering, plan_receipt, plan, payload = _lowered(operation)
    PART_DRESSUP_REVIEWED_FAMILY_SPEC.validate_plan(plan, plan_receipt, plan_receipt.operation)
    document = _Document()
    source_result, identities, token = _source_result(document)
    source = source_result.object
    session = _Session(document, identities)

    def apply(raw, *, expected_content_sha256, expected_plan_sha256, bindings):
        assert raw == payload
        assert expected_content_sha256 == lowering.plan_document.content_sha256
        assert expected_plan_sha256 == plan.plan_sha256
        assert bindings.document is document
        assert bindings.source_object is source
        result = _native_result(document, operation, source, plan.magnitude_mm)
        document.Objects.append(result)
        return PartDressupConformanceReceipt(
            plan_sha256=plan.plan_sha256,
            operation=operation,
            selection_role=plan.selection_role,
            object_name=result.Name,
            native_type_id=result.TypeId,
            source_object_name=source.Name,
        )

    monkeypatch.setattr(dressup_execution, "apply_part_dressup_plan", apply)
    executed = execute_part_dressup_reviewed_plan(
        document,
        plan,
        payload,
        lowering.plan_document,
        plan_receipt.operation,
        _ReviewedFamilyExecutionContext(
            session=session,
            document=document,
            source_results=(source_result,),
            run_token=token,
        ),
    )

    invariant = PART_DRESSUP_RESULT_INVARIANTS[operation]
    assert executed.object is document.Objects[-1]
    assert executed.receipt.invariant is invariant
    assert executed.receipt.native_type_id == PART_DRESSUP_NATIVE_TYPE_IDS[operation]
    assert executed.receipt.source_receipt_sha256 == source_result.native_receipt.receipt_sha256
    observation = EntityObservation(
        object_id="object_" + "a" * 32,
        feature_id="feature_" + "b" * 32,
        object_type=PART_DRESSUP_NATIVE_TYPE_IDS[operation],
        semantic_role="feature",
        provenance={"source": "model", "operation_id": "apply_reviewed_intent"},
        placement=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
        volume_mm3=80.0,
        area_mm2=1.0,
        bbox_mm=(1.0, 1.0, 1.0),
        center_of_mass_mm=(0.0, 0.0, 0.0),
        valid_shape=True,
        solid_count=1,
    )
    executed.receipt.validate_adoption(document, executed.object, observation)
    with pytest.raises(ReviewedIntentExecutionError):
        executed.receipt.validate_adopted_observation(
            dataclasses.replace(observation, solid_count=0)
        )
    executed.object.Shape.mutate()
    with pytest.raises(ReviewedIntentExecutionError):
        executed.receipt.validate_adoption(document, executed.object, observation)


@pytest.mark.parametrize(
    "failure",
    ("not_same_run", "stale_shape", "tampered_identity", "tampered_receipt", "wrong_count"),
)
def test_part_dressup_source_failures_stop_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    operation = PartDressupOperation.EDGE_FILLET
    lowering, plan_receipt, plan, payload = _lowered(operation)
    document = _Document()
    source_result, identities, token = _source_result(document)
    source = source_result.object
    source_results: tuple[object, ...] = (source_result,)
    if failure == "not_same_run":
        token = object()
    elif failure == "stale_shape":
        source.Shape.mutate()
    elif failure == "tampered_identity":
        identities[source] = dataclasses.replace(
            identities[source],
            provenance=Provenance(
                source=ProvenanceSource.USER,
                operation_id="apply_reviewed_intent",
            ),
        )
    elif failure == "tampered_receipt":
        object.__setattr__(source_result.native_receipt, "receipt_sha256", _sha("tampered"))
    else:
        source_results = (source_result, source_result)
    before = tuple(document.Objects)
    called = False

    def apply(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid source reached native mutation")

    monkeypatch.setattr(dressup_execution, "apply_part_dressup_plan", apply)
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_part_dressup_reviewed_plan(
            document,
            plan,
            payload,
            lowering.plan_document,
            plan_receipt.operation,
            _ReviewedFamilyExecutionContext(
                session=_Session(document, identities),
                document=document,
                source_results=source_results,
                run_token=token,
            ),
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert called is False
    assert tuple(document.Objects) == before


@pytest.mark.parametrize(
    ("operation", "shape"),
    (
        (PartDressupOperation.EDGE_CHAMFER, _Shape("ambiguous-edge", 100.0, ambiguous_edge=True)),
        (
            PartDressupOperation.FACE_THICKNESS,
            _Shape("ambiguous-face", 100.0, ambiguous_face=True),
        ),
    ),
)
def test_part_dressup_ambiguous_semantic_topology_fails_before_mutation(
    operation: PartDressupOperation,
    shape: _Shape,
) -> None:
    document = _Document()
    source = _Feature(document, name="Ambiguous", type_id="Part::Feature", shape=shape)
    document.Objects.append(source)
    before = tuple(document.Objects)

    with pytest.raises(PartDressupRuleError) as caught:
        dressup_rules._resolve_semantic_selection(  # noqa: SLF001
            source,
            (
                dressup_execution._EXPECTED_SELECTION_ROLES[operation]  # noqa: SLF001
            ),
        )
    assert caught.value.code is PartDressupRuleErrorCode.SELECTION_FAILED
    assert tuple(document.Objects) == before


def test_part_dressup_tampered_plan_is_rejected_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = PartDressupOperation.FACE_THICKNESS
    lowering, plan_receipt, plan, payload = _lowered(operation)
    document = _Document()
    source_result, identities, token = _source_result(document)
    before = tuple(document.Objects)
    called = False

    def apply(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("tampered plan reached native mutation")

    monkeypatch.setattr(dressup_execution, "apply_part_dressup_plan", apply)
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_part_dressup_reviewed_plan(
            document,
            plan,
            payload + b" ",
            lowering.plan_document,
            plan_receipt.operation,
            _ReviewedFamilyExecutionContext(
                session=_Session(document, identities),
                document=document,
                source_results=(source_result,),
                run_token=token,
            ),
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert called is False
    assert tuple(document.Objects) == before
