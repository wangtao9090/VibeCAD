from __future__ import annotations

import dataclasses
import hashlib

import pytest

import tests.test_intent_bridge_freecad_part_offset_projection_adapter as adapter_cases
import vibecad.execution.freecad_part_offset_projection_reviewed_execution as offset_execution
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_part_offset_projection_reviewed_execution import (
    PART_OFFSET_REQUIRED_SOURCE_ROLES,
    PART_OFFSET_RESULT_INVARIANTS,
    PART_OFFSET_REVIEWED_FAMILY_SPEC,
    PART_OFFSET_REVIEWED_PRODUCT_IDENTITIES,
    PART_OFFSET_REVIEWED_PRODUCT_OPERATIONS,
    execute_part_offset_reviewed_plan,
    execute_part_offset_reviewed_plan_with_sources,
    resolve_part_offset_reviewed_operation,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PART_BOX_ROUTE,
    REVIEWED_PART_CURVE_ROUTES,
    REVIEWED_PART_OFFSET_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    ReviewedNativeExecutionResult,
    _ReviewedFamilyExecutionContext,
    lower_reviewed_intent,
)
from vibecad.execution.selectors import (
    EntityIdentity,
    Provenance,
    ProvenanceSource,
    SemanticRole,
)
from vibecad.intent_bridge.freecad_part_offset_projection_adapter import (
    PART_OFFSET_MANIFEST,
)
from vibecad.parametric.freecad_part_core_rules import (
    PartCoreConformanceReceipt,
    PartCoreOperation,
)
from vibecad.parametric.freecad_part_curve_rules import (
    PartCurveConformanceReceipt,
    PartCurveOperation,
    PartCurveShapeSignature,
)
from vibecad.parametric.freecad_part_offset_projection_rules import (
    PART_OFFSET_NATIVE_TYPE_IDS,
    PART_OFFSET_SOURCE_ROLES,
    PartOffsetBackendPlan,
    PartOffsetConformanceReceipt,
    PartOffsetOperation,
    PartOffsetSourceRole,
)
from vibecad.validation import EntityObservation
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1


def _program(
    operation: PartOffsetOperation,
    *,
    operation_definition: str | None = None,
) -> ReviewedIntentProgramV1:
    graph = adapter_cases._graph(  # noqa: SLF001
        operation,
        operation_definition=operation_definition,
    )
    reviewed = next(
        item for item in PART_OFFSET_MANIFEST.operations if item.operation_id == operation.value
    )
    namespace, version, term_id, digest = reviewed.semantic_term.semantic_identity
    return ReviewedIntentProgramV1(
        operation_id=f"{PART_OFFSET_MANIFEST.family_id}.{operation.value}",
        semantic_operation=f"{namespace}/{version}/{term_id}@{digest}",
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


def _family_and_routes():
    routes = REVIEWED_PART_OFFSET_ROUTES
    return routes[0].family, routes


def _install_routes(monkeypatch: pytest.MonkeyPatch):
    del monkeypatch
    return _family_and_routes()


def test_offset_descriptor_has_three_exact_reviewed_routes() -> None:
    family, routes = _family_and_routes()

    assert family.manifest is PART_OFFSET_MANIFEST
    assert PART_OFFSET_REVIEWED_PRODUCT_OPERATIONS == tuple(PartOffsetOperation)
    assert len(routes) == len(PART_OFFSET_REVIEWED_PRODUCT_IDENTITIES) == 3
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 78
    assert CURRENT_REVIEWED_INTENT_ROUTES[-48:-45] == routes
    assert tuple(item.operation.operation_id for item in routes) == (
        PART_OFFSET_REVIEWED_FAMILY_SPEC.operation_ids
    )

    formal = current_freecad_intent_capability_specs()
    for operation, identity in zip(
        PART_OFFSET_REVIEWED_PRODUCT_OPERATIONS,
        PART_OFFSET_REVIEWED_PRODUCT_IDENTITIES,
        strict=True,
    ):
        reviewed = resolve_part_offset_reviewed_operation(*identity)
        assert reviewed is not None and reviewed.operation_id == operation.value
        matching = tuple(item for item in formal if item.operation_id == identity[0])
        assert len(matching) == 1
        assert matching[0].semantic_operation == identity[1]
        assert matching[0].native_type_id == PART_OFFSET_NATIVE_TYPE_IDS[operation]


def test_offset_source_and_result_contracts_are_exact_and_ordered() -> None:
    assert PART_OFFSET_REQUIRED_SOURCE_ROLES == {
        PartOffsetOperation.SOLID_OFFSET.value: (PartOffsetSourceRole.SOLID_SOURCE,),
        PartOffsetOperation.PLANAR_WIRE_OFFSET.value: (PartOffsetSourceRole.PLANAR_WIRE_SOURCE,),
        PartOffsetOperation.EDGE_ON_FACE_PROJECTION.value: (
            PartOffsetSourceRole.SUPPORT_FACE,
            PartOffsetSourceRole.PROJECTION_EDGE,
        ),
    }
    assert {
        operation: (
            invariant.shape_type,
            invariant.solid_count,
            invariant.require_positive_length,
            invariant.require_positive_volume,
        )
        for operation, invariant in PART_OFFSET_RESULT_INVARIANTS.items()
    } == {
        PartOffsetOperation.SOLID_OFFSET: ("Solid", 1, False, True),
        PartOffsetOperation.PLANAR_WIRE_OFFSET: ("Wire", 0, True, False),
        PartOffsetOperation.EDGE_ON_FACE_PROJECTION: ("Compound", 0, True, False),
    }


@pytest.mark.parametrize("operation", PART_OFFSET_REVIEWED_PRODUCT_OPERATIONS)
def test_offset_routes_lower_to_canonical_source_bound_plans(
    monkeypatch: pytest.MonkeyPatch,
    operation: PartOffsetOperation,
) -> None:
    _, routes = _install_routes(monkeypatch)
    lowered = lower_reviewed_intent(_program(operation))

    assert lowered.route in routes
    assert type(lowered.plan) is PartOffsetBackendPlan
    assert lowered.plan.operation is operation
    assert lowered.payload == lowered.plan.canonical_bytes
    assert tuple(item.role for item in lowered.plan.sources) == PART_OFFSET_SOURCE_ROLES[operation]
    assert lowered.result.plan_document.document_digest == lowered.plan.plan_sha256
    assert (
        lowered.result.plan_document.content_sha256 == hashlib.sha256(lowered.payload).hexdigest()
    )
    assert b"Face1" not in lowered.payload
    assert b"Edge1" not in lowered.payload


def test_offset_unknown_and_tampered_routes_remain_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_routes(monkeypatch)
    operation_id, semantic_operation = PART_OFFSET_REVIEWED_PRODUCT_IDENTITIES[0]

    assert (
        resolve_part_offset_reviewed_operation(operation_id + "_unknown", semantic_operation)
        is None
    )
    assert (
        resolve_part_offset_reviewed_operation(operation_id, semantic_operation[:-1] + "0") is None
    )
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        lower_reviewed_intent(
            _program(
                PartOffsetOperation.SOLID_OFFSET,
                operation_definition="f" * 64,
            )
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.LOWERING_FAILED


class _Shape:
    def __init__(
        self,
        value: str,
        *,
        shape_type: str,
        vertex_count: int = 2,
        edge_count: int = 1,
        face_count: int = 0,
        wire_count: int = 0,
        solid_count: int = 0,
        length: float = 1.0,
        area: float = 0.0,
        volume: float = 0.0,
        closed: bool = False,
        planar: bool = False,
    ) -> None:
        self._value = value
        self.ShapeType = shape_type
        self.Vertexes = [object() for _ in range(vertex_count)]
        self.Edges = [object() for _ in range(edge_count)]
        self.Faces = [object() for _ in range(face_count)]
        self.Wires = [object() for _ in range(wire_count)]
        self.Solids = [object() for _ in range(solid_count)]
        self.Length = length
        self.Area = area
        self.Volume = volume
        self._closed = closed
        self._planar = planar

    def exportBrepToString(self) -> str:
        return self._value

    def isNull(self) -> bool:
        return False

    def isValid(self) -> bool:
        return True

    def isClosed(self) -> bool:
        return self._closed

    def findPlane(self):
        return object() if self._planar else None

    def mutate(self) -> None:
        self._value += "-mutated"
        self.Length += 1.0


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
        self.State = ("Up-to-date",)
        self.Shape = shape
        self.ExpressionEngine = ()
        self.OutListRecursive: tuple[object, ...] = ()

    def isValid(self) -> bool:
        return True

    def getParentGroup(self):
        return None


class _Document:
    def __init__(self) -> None:
        self.Objects: list[object] = []

    def getObject(self, name: str):
        return next((item for item in self.Objects if getattr(item, "Name", None) == name), None)


class _Session:
    def __init__(
        self,
        document: _Document,
        identities: dict[object, EntityIdentity],
    ) -> None:
        self.doc = document
        self.identities = identities

    def read_object_identity(self, item: object) -> EntityIdentity:
        return self.identities[item]


def _curve_routes() -> dict[PartCurveOperation, object]:
    return {
        operation: next(
            route
            for route in REVIEWED_PART_CURVE_ROUTES
            if route.operation.operation_id == operation.value
        )
        for operation in (
            PartCurveOperation.POLYGON,
            PartCurveOperation.PLANE,
            PartCurveOperation.LINE,
        )
    }


def _source_shape(role: PartOffsetSourceRole, name: str) -> _Shape:
    if role is PartOffsetSourceRole.SOLID_SOURCE:
        return _Shape(name, shape_type="Solid", face_count=6, solid_count=1, area=6.0, volume=1.0)
    if role is PartOffsetSourceRole.PLANAR_WIRE_SOURCE:
        return _Shape(
            name,
            shape_type="Wire",
            vertex_count=4,
            edge_count=4,
            wire_count=1,
            length=4.0,
            closed=True,
            planar=True,
        )
    if role is PartOffsetSourceRole.SUPPORT_FACE:
        return _Shape(
            name,
            shape_type="Face",
            vertex_count=4,
            edge_count=4,
            face_count=1,
            length=4.0,
            area=1.0,
        )
    return _Shape(name, shape_type="Edge", vertex_count=2, edge_count=1, length=1.0)


def _source_result(
    document: _Document,
    role: PartOffsetSourceRole,
    index: int,
    identities: dict[object, EntityIdentity],
) -> ReviewedNativeExecutionResult:
    routes = _curve_routes()
    plan_sha256 = hashlib.sha256(f"source-plan:{index}".encode()).hexdigest()
    name = f"Source{index}"
    shape = _source_shape(role, name)
    if role is PartOffsetSourceRole.SOLID_SOURCE:
        route = REVIEWED_PART_BOX_ROUTE
        receipt = PartCoreConformanceReceipt(
            plan_sha256=plan_sha256,
            operation=PartCoreOperation.BOX,
            object_name=name,
            source_shape_sha256s=(),
            result_shape_sha256=hashlib.sha256(shape.exportBrepToString().encode()).hexdigest(),
        )
    else:
        operation = {
            PartOffsetSourceRole.PLANAR_WIRE_SOURCE: PartCurveOperation.POLYGON,
            PartOffsetSourceRole.SUPPORT_FACE: PartCurveOperation.PLANE,
            PartOffsetSourceRole.PROJECTION_EDGE: PartCurveOperation.LINE,
        }[role]
        route = routes[operation]
        receipt = PartCurveConformanceReceipt(
            plan_sha256=plan_sha256,
            operation=operation,
            object_name=name,
            shape=PartCurveShapeSignature(
                shape_type=shape.ShapeType,
                vertex_count=len(shape.Vertexes),
                edge_count=len(shape.Edges),
                face_count=len(shape.Faces),
                length_mm=shape.Length,
                area_mm2=shape.Area,
            ),
        )
    feature = _Feature(document, name=name, type_id=route.operation.native_type_id, shape=shape)
    document.Objects.append(feature)
    identities[feature] = EntityIdentity(
        object_id=f"object_{index + 1:032x}",
        feature_id=f"feature_{index + 1:032x}",
        object_type=route.operation.native_type_id,
        semantic_role=SemanticRole.PRIMITIVE,
        provenance=Provenance(
            source=ProvenanceSource.MODEL,
            operation_id="apply_reviewed_intent",
        ),
    )
    return ReviewedNativeExecutionResult(
        route=route,
        object=feature,
        plan_sha256=plan_sha256,
        plan_content_sha256=hashlib.sha256(f"source-content:{index}".encode()).hexdigest(),
        native_receipt=receipt,
    )


def _result_feature(
    document: _Document,
    operation: PartOffsetOperation,
    sources: tuple[object, ...],
) -> _Feature:
    invariant = PART_OFFSET_RESULT_INVARIANTS[operation]
    result = _Feature(
        document,
        name=f"Result_{operation.value}",
        type_id=invariant.native_type_id,
        shape=_Shape(
            f"result:{operation.value}",
            shape_type=invariant.shape_type,
            wire_count=1 if invariant.shape_type == "Wire" else 0,
            solid_count=invariant.solid_count,
            length=1.0,
            volume=1.0 if invariant.require_positive_volume else 0.0,
        ),
    )
    result.OutListRecursive = sources
    if operation is PartOffsetOperation.SOLID_OFFSET:
        result.Source = sources[0]
        result.Value = 2.0
        result.Mode = "Skin"
        result.Join = "Arc"
        result.Fill = result.Intersection = result.SelfIntersection = False
    elif operation is PartOffsetOperation.PLANAR_WIRE_OFFSET:
        result.Source = sources[0]
        result.Value = 2.0
        result.Mode = "Pipe"
        result.Join = "Arc"
        result.Fill = result.Intersection = result.SelfIntersection = False
    else:
        result.SupportFace = (sources[0], ["Face1"])
        result.Projection = [(sources[1], ["Edge1"])]
        result.Direction = (0.0, 0.0, -1.0)
        result.Mode = "Edges"
        result.Offset = 0.0
        result.Height = 0.0
    return result


@pytest.mark.parametrize(
    ("operation", "shape_type", "solid_count", "volume"),
    (
        (PartOffsetOperation.SOLID_OFFSET, "Solid", 1, 0.0),
        (PartOffsetOperation.PLANAR_WIRE_OFFSET, "Edge", 0, 0.0),
        (PartOffsetOperation.EDGE_ON_FACE_PROJECTION, "Wire", 0, 0.0),
    ),
)
def test_offset_result_invariants_reject_wrong_shape_or_effect(
    operation: PartOffsetOperation,
    shape_type: str,
    solid_count: int,
    volume: float,
) -> None:
    document = _Document()
    invariant = PART_OFFSET_RESULT_INVARIANTS[operation]
    result = _Feature(
        document,
        name="InvalidResult",
        type_id=invariant.native_type_id,
        shape=_Shape(
            "invalid-result",
            shape_type=shape_type,
            solid_count=solid_count,
            length=1.0,
            volume=volume,
        ),
    )
    document.Objects.append(result)
    receipt = PartOffsetConformanceReceipt(
        plan_sha256=hashlib.sha256(b"plan").hexdigest(),
        operation=operation,
        object_name=result.Name,
        native_type_id=result.TypeId,
        source_object_names=tuple(
            f"Source{index}" for index, _role in enumerate(PART_OFFSET_SOURCE_ROLES[operation])
        ),
    )

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        invariant.validate_native_result(
            document,
            result,
            receipt,
            result_shape_sha256=hashlib.sha256(
                result.Shape.exportBrepToString().encode()
            ).hexdigest(),
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize("operation", PART_OFFSET_REVIEWED_PRODUCT_OPERATIONS)
def test_offset_native_callback_is_inert_without_authenticated_sources(
    monkeypatch: pytest.MonkeyPatch,
    operation: PartOffsetOperation,
) -> None:
    _install_routes(monkeypatch)
    lowered = lower_reviewed_intent(_program(operation))
    document = _Document()
    session = _Session(document, {})

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_part_offset_reviewed_plan(
            document,
            lowered.plan,
            lowered.payload,
            lowered.result.plan_document,
            lowered.route.operation,
            _ReviewedFamilyExecutionContext(
                session=session,
                document=document,
                source_results=(),
            ),
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert document.Objects == []


@pytest.mark.parametrize("operation", PART_OFFSET_REVIEWED_PRODUCT_OPERATIONS)
def test_offset_source_aware_hook_executes_and_closes_adoption(
    monkeypatch: pytest.MonkeyPatch,
    operation: PartOffsetOperation,
) -> None:
    _install_routes(monkeypatch)
    lowered = lower_reviewed_intent(_program(operation))
    document = _Document()
    identities: dict[object, EntityIdentity] = {}
    source_results = tuple(
        _source_result(document, role, index, identities)
        for index, role in enumerate(PART_OFFSET_SOURCE_ROLES[operation])
    )
    session = _Session(document, identities)

    def apply(raw, *, expected_content_sha256, expected_plan_sha256, bindings):
        assert raw == lowered.payload
        assert expected_content_sha256 == lowered.result.plan_document.content_sha256
        assert expected_plan_sha256 == lowered.plan.plan_sha256
        assert bindings.document is document
        assert tuple(item.role for item in bindings.sources) == PART_OFFSET_SOURCE_ROLES[operation]
        sources = tuple(item.native_object for item in bindings.sources)
        result = _result_feature(document, operation, sources)
        document.Objects.append(result)
        return PartOffsetConformanceReceipt(
            plan_sha256=lowered.plan.plan_sha256,
            operation=operation,
            object_name=result.Name,
            native_type_id=result.TypeId,
            source_object_names=tuple(item.Name for item in sources),
        )

    monkeypatch.setattr(offset_execution, "apply_part_offset_plan", apply)
    result = lowered.route.family.apply_plan(
        document,
        lowered.plan,
        lowered.payload,
        lowered.result.plan_document,
        lowered.route.operation,
        _ReviewedFamilyExecutionContext(
            session=session,
            document=document,
            source_results=source_results,
        ),
    )

    invariant = PART_OFFSET_RESULT_INVARIANTS[operation]
    assert result.object is document.Objects[-1]
    assert result.receipt.invariant is invariant
    assert result.receipt.native_type_id == invariant.native_type_id
    assert len(result.receipt.source_shape_sha256s) == len(source_results)
    observation = EntityObservation(
        object_id="object_" + "a" * 32,
        feature_id="feature_" + "b" * 32,
        object_type=invariant.native_type_id,
        semantic_role="feature",
        provenance={"source": "model", "operation_id": "apply_reviewed_intent"},
        placement=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
        volume_mm3=1.0 if invariant.require_positive_volume else 0.0,
        area_mm2=0.0,
        bbox_mm=(1.0, 1.0, 1.0),
        center_of_mass_mm=(0.0, 0.0, 0.0),
        valid_shape=True,
        solid_count=invariant.solid_count,
    )
    result.receipt.validate_adoption(document, result.object, observation)
    with pytest.raises(ReviewedIntentExecutionError):
        result.receipt.validate_adopted_observation(
            dataclasses.replace(observation, solid_count=1 - invariant.solid_count)
        )
    result.object.Shape.mutate()
    with pytest.raises(ReviewedIntentExecutionError):
        result.receipt.validate_adoption(document, result.object, observation)


@pytest.mark.parametrize(
    "failure",
    ("stale", "wrong_order", "duplicate", "wrong_cardinality", "tampered_payload"),
)
def test_offset_source_failures_are_rejected_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    _install_routes(monkeypatch)
    operation = PartOffsetOperation.EDGE_ON_FACE_PROJECTION
    lowered = lower_reviewed_intent(_program(operation))
    document = _Document()
    identities: dict[object, EntityIdentity] = {}
    source_results = tuple(
        _source_result(document, role, index, identities)
        for index, role in enumerate(PART_OFFSET_SOURCE_ROLES[operation])
    )
    session = _Session(document, identities)
    payload = lowered.payload
    if failure == "stale":
        source_results[0].object.Shape.mutate()
    elif failure == "wrong_order":
        source_results = tuple(reversed(source_results))
    elif failure == "duplicate":
        source_results = (source_results[0], source_results[0])
    elif failure == "wrong_cardinality":
        source_results = source_results[:1]
    else:
        payload += b" "
    before = tuple(document.Objects)
    called = False

    def apply(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid sources or payload must not reach native apply")

    monkeypatch.setattr(offset_execution, "apply_part_offset_plan", apply)
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_part_offset_reviewed_plan_with_sources(
            document,
            lowered.plan,
            payload,
            lowered.result.plan_document,
            lowered.route.operation,
            source_results,
            session=session,
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert called is False
    assert tuple(document.Objects) == before
