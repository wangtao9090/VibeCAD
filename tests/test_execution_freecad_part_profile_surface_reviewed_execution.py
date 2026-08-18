from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass

import pytest

import tests.test_intent_bridge_freecad_part_profile_surface_adapter as adapter_cases
import vibecad.execution.freecad_part_profile_surface_reviewed_execution as profile_execution
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_part_profile_surface_reviewed_execution import (
    PART_PROFILE_SURFACE_REQUIRED_SOURCE_BINDINGS,
    PART_PROFILE_SURFACE_RESULT_INVARIANTS,
    PART_PROFILE_SURFACE_REVIEWED_FAMILY_SPEC,
    PART_PROFILE_SURFACE_REVIEWED_PRODUCT_IDENTITIES,
    PART_PROFILE_SURFACE_REVIEWED_PRODUCT_OPERATIONS,
    execute_part_profile_surface_reviewed_plan,
    execute_part_profile_surface_reviewed_plan_with_sources,
    resolve_part_profile_surface_reviewed_operation,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PART_BOX_ROUTE,
    REVIEWED_PART_CURVE_ROUTES,
    REVIEWED_PART_PROFILE_SURFACE_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    ReviewedNativeExecutionResult,
    _ReviewedFamilyExecutionContext,
    execute_reviewed_intent_native,
    lower_reviewed_intent,
)
from vibecad.execution.selectors import (
    EntityIdentity,
    Provenance,
    ProvenanceSource,
    SemanticRole,
)
from vibecad.intent_bridge.freecad_part_profile_surface_adapter import (
    PART_PROFILE_SURFACE_MANIFEST,
)
from vibecad.parametric.freecad_part_profile_surface_rules import (
    PART_PROFILE_SURFACE_NATIVE_SPECS,
    PartProfileSurfaceBackendPlan,
    PartProfileSurfaceConformanceReceipt,
    PartProfileSurfaceOperation,
    PartProfileSurfaceSourceRole,
)
from vibecad.validation import EntityObservation
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1


def _program(
    operation: PartProfileSurfaceOperation,
    *,
    source_operation_definition: str | None = None,
    source_count: int | None = None,
) -> ReviewedIntentProgramV1:
    graph = adapter_cases._graph(  # noqa: SLF001
        operation,
        source_operation_definition=source_operation_definition,
    )
    if source_count is not None:
        if operation is not PartProfileSurfaceOperation.LOFT or not 2 <= source_count <= 8:
            raise ValueError("source_count is only supported for bounded loft profiles")
        source_nodes = list(graph.nodes[:-1])
        target = graph.nodes[-1]
        template = source_nodes[-1]
        dependencies = list(target.intent.dependencies)
        for ordinal in range(len(source_nodes), source_count):
            node_id = f"node_source_profile_{ordinal}"
            result_id = f"result_source_profile_{ordinal}"
            source_nodes.append(
                dataclasses.replace(
                    template,
                    node_id=node_id,
                    name=f"Authenticated profile {ordinal}",
                    results=(
                        dataclasses.replace(
                            template.results[0],
                            result_id=result_id,
                        ),
                    ),
                )
            )
            dependencies.append(
                dataclasses.replace(
                    dependencies[-1],
                    dependency_id=f"dependency_profile_{ordinal}",
                    upstream_node_id=node_id,
                    upstream_result_id=result_id,
                    ordinal=ordinal,
                )
            )
        target = dataclasses.replace(
            target,
            intent=dataclasses.replace(
                target.intent,
                dependencies=tuple(dependencies),
            ),
        )
        graph = dataclasses.replace(graph, nodes=(*source_nodes, target))
    reviewed = next(
        item
        for item in PART_PROFILE_SURFACE_MANIFEST.operations
        if item.operation_id == operation.value
    )
    namespace, version, term_id, digest = reviewed.semantic_term.semantic_identity
    return ReviewedIntentProgramV1(
        operation_id=f"{PART_PROFILE_SURFACE_MANIFEST.family_id}.{operation.value}",
        semantic_operation=f"{namespace}/{version}/{term_id}@{digest}",
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


def _family_and_routes():
    routes = REVIEWED_PART_PROFILE_SURFACE_ROUTES
    return routes[0].family, routes


def _install_routes(monkeypatch: pytest.MonkeyPatch):
    del monkeypatch
    return _family_and_routes()


def test_profile_surface_descriptor_has_six_exact_reviewed_routes() -> None:
    family, routes = _family_and_routes()

    assert family.manifest is PART_PROFILE_SURFACE_MANIFEST
    assert PART_PROFILE_SURFACE_REVIEWED_PRODUCT_OPERATIONS == tuple(PartProfileSurfaceOperation)
    assert len(routes) == len(PART_PROFILE_SURFACE_REVIEWED_PRODUCT_IDENTITIES) == 6
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 33
    assert CURRENT_REVIEWED_INTENT_ROUTES[-9:-3] == routes
    assert tuple(item.operation.operation_id for item in routes) == (
        PART_PROFILE_SURFACE_REVIEWED_FAMILY_SPEC.operation_ids
    )
    contracts = tuple(route.family.product_result(route.operation) for route in routes)
    assert tuple(item.result_kind.value for item in contracts) == (
        "solid",
        "solid",
        "solid",
        "solid",
        "valid_shape",
        "valid_shape",
    )
    assert all(item.semantic_roles == (SemanticRole.FEATURE,) for item in contracts)
    assert all(len(item.owned_type_ids) == 1 for item in contracts)

    formal = current_freecad_intent_capability_specs()
    for operation, identity in zip(
        PART_PROFILE_SURFACE_REVIEWED_PRODUCT_OPERATIONS,
        PART_PROFILE_SURFACE_REVIEWED_PRODUCT_IDENTITIES,
        strict=True,
    ):
        reviewed = resolve_part_profile_surface_reviewed_operation(*identity)
        assert reviewed is not None and reviewed.operation_id == operation.value
        matching = tuple(item for item in formal if item.operation_id == identity[0])
        assert len(matching) == 1
        assert matching[0].semantic_operation == identity[1]
        assert matching[0].native_type_id == PART_PROFILE_SURFACE_NATIVE_SPECS[operation].type_id


def test_profile_surface_result_invariants_exactly_partition_solids_and_faces() -> None:
    solid_operations = {
        PartProfileSurfaceOperation.EXTRUSION,
        PartProfileSurfaceOperation.REVOLUTION,
        PartProfileSurfaceOperation.LOFT,
        PartProfileSurfaceOperation.SWEEP,
    }
    surface_operations = {
        PartProfileSurfaceOperation.RULED_SURFACE,
        PartProfileSurfaceOperation.FACE,
    }

    assert set(PART_PROFILE_SURFACE_RESULT_INVARIANTS) == set(PartProfileSurfaceOperation)
    for operation, invariant in PART_PROFILE_SURFACE_RESULT_INVARIANTS.items():
        assert invariant.native_type_id == PART_PROFILE_SURFACE_NATIVE_SPECS[operation].type_id
        assert invariant.semantic_role.value == "feature"
        assert invariant.minimum_edge_count == invariant.minimum_face_count == 1
        if operation in solid_operations:
            assert invariant.shape_types == ("Solid",)
            assert invariant.solid_count == 1
            assert invariant.exact_face_count is None
            assert invariant.require_positive_volume is True
            assert invariant.require_positive_area is False
        else:
            assert operation in surface_operations
            assert invariant.shape_types == ("Face",)
            assert invariant.solid_count == 0
            assert invariant.exact_face_count == 1
            assert invariant.require_positive_area is True
            assert invariant.require_positive_volume is False


@pytest.mark.parametrize(
    ("operation", "shape_type", "solid_count", "area", "volume"),
    (
        (PartProfileSurfaceOperation.EXTRUSION, "Face", 0, 1.0, 0.0),
        (PartProfileSurfaceOperation.EXTRUSION, "Solid", 1, 1.0, 0.0),
        (PartProfileSurfaceOperation.FACE, "Solid", 1, 1.0, 1.0),
        (PartProfileSurfaceOperation.FACE, "Face", 0, 0.0, 0.0),
    ),
)
def test_profile_surface_result_invariants_reject_wrong_shape_or_effect(
    operation: PartProfileSurfaceOperation,
    shape_type: str,
    solid_count: int,
    area: float,
    volume: float,
) -> None:
    document = _Document()
    invariant = PART_PROFILE_SURFACE_RESULT_INVARIANTS[operation]
    result = _Feature(
        document,
        name="InvalidResult",
        type_id=invariant.native_type_id,
        shape_type=shape_type,
        solid_count=solid_count,
        area=area,
        volume=volume,
    )
    document.Objects.append(result)
    receipt = PartProfileSurfaceConformanceReceipt(
        plan_sha256=hashlib.sha256(b"plan").hexdigest(),
        operation=operation,
        object_name=result.Name,
        source_shape_sha256s=(hashlib.sha256(b"source").hexdigest(),),
        result_shape_sha256=hashlib.sha256(result.Shape.exportBrepToString().encode()).hexdigest(),
    )

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        invariant.validate_native_result(document, result, receipt)
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize(
    "operation",
    PART_PROFILE_SURFACE_REVIEWED_PRODUCT_OPERATIONS,
)
def test_profile_surface_routes_lower_to_canonical_source_bound_plans(
    monkeypatch: pytest.MonkeyPatch,
    operation: PartProfileSurfaceOperation,
) -> None:
    _, routes = _install_routes(monkeypatch)
    lowered = lower_reviewed_intent(_program(operation))

    assert lowered.route in routes
    assert type(lowered.plan) is PartProfileSurfaceBackendPlan
    assert lowered.plan.operation is operation
    assert lowered.payload == lowered.plan.canonical_bytes
    assert lowered.result.plan_document.document_digest == lowered.plan.plan_sha256
    assert (
        lowered.result.plan_document.content_sha256 == hashlib.sha256(lowered.payload).hexdigest()
    )
    assert lowered.receipt.operation is lowered.route.operation
    assert lowered.plan.sources
    requirements = PART_PROFILE_SURFACE_REQUIRED_SOURCE_BINDINGS[operation.value]
    assert requirements == PART_PROFILE_SURFACE_NATIVE_SPECS[operation].source_requirements
    assert all(item.minimum >= 1 for item in requirements)


def test_three_profile_loft_lowers_one_exact_ordered_source_contract() -> None:
    lowered = lower_reviewed_intent(_program(PartProfileSurfaceOperation.LOFT, source_count=3))

    assert lowered.plan.operation is PartProfileSurfaceOperation.LOFT
    assert tuple(
        (item.role, item.node_id, item.result_id, item.ordinal) for item in lowered.plan.sources
    ) == tuple(
        (
            PartProfileSurfaceSourceRole.PROFILE,
            f"node_source_profile_{ordinal}",
            f"result_source_profile_{ordinal}",
            ordinal,
        )
        for ordinal in range(3)
    )


def test_profile_surface_unknown_and_tampered_routes_remain_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_routes(monkeypatch)
    operation_id, semantic_operation = PART_PROFILE_SURFACE_REVIEWED_PRODUCT_IDENTITIES[0]

    assert (
        resolve_part_profile_surface_reviewed_operation(
            f"{operation_id}_unknown",
            semantic_operation,
        )
        is None
    )
    assert (
        resolve_part_profile_surface_reviewed_operation(
            operation_id,
            f"{semantic_operation[:-1]}0",
        )
        is None
    )
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        lower_reviewed_intent(
            _program(
                PartProfileSurfaceOperation.EXTRUSION,
                source_operation_definition="f" * 64,
            )
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.LOWERING_FAILED


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


class _Feature:
    def __init__(
        self,
        document: _Document,
        *,
        name: str,
        type_id: str,
        shape_type: str = "Solid",
        edge_count: int = 1,
        solid_count: int = 1,
        face_count: int = 1,
        area: float = 1.0,
        volume: float = 1.0,
    ) -> None:
        self.Document = document
        self.Name = name
        self.TypeId = type_id
        self.State = ("Up-to-date",)
        self.Shape = _Shape(
            name,
            shape_type=shape_type,
            edge_count=edge_count,
            solid_count=solid_count,
            face_count=face_count,
            area=area,
            volume=volume,
        )

    def isValid(self) -> bool:
        return True


class _Shape:
    def __init__(
        self,
        value: str,
        *,
        shape_type: str,
        edge_count: int,
        solid_count: int,
        face_count: int,
        area: float,
        volume: float,
    ) -> None:
        self._value = value
        self.ShapeType = shape_type
        self.Edges = [object() for _ in range(edge_count)]
        self.Faces = [object() for _ in range(face_count)]
        self.Solids = [object() for _ in range(solid_count)]
        self.Wires = [_Wire()] if shape_type == "Wire" else []
        self.Length = 1.0
        self.Area = area
        self.Volume = volume

    def exportBrepToString(self) -> str:
        return self._value

    def isNull(self) -> bool:
        return False

    def isValid(self) -> bool:
        return True

    def mutate(self) -> None:
        self._value = f"{self._value}-mutated"


class _Wire:
    def isClosed(self) -> bool:
        return True


@dataclass(frozen=True)
class _SourceReceipt:
    plan_sha256: str
    result_shape_sha256: str


@pytest.mark.parametrize(
    "operation",
    PART_PROFILE_SURFACE_REVIEWED_PRODUCT_OPERATIONS,
)
def test_profile_surface_descriptor_is_inert_without_authenticated_sources(
    monkeypatch: pytest.MonkeyPatch,
    operation: PartProfileSurfaceOperation,
) -> None:
    _, routes = _install_routes(monkeypatch)
    document = _Document()
    session = _Session(document, {})

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_reviewed_intent_native(session, _program(operation))
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INVALID_INPUT
    assert document.Objects == []
    assert len(routes) == 6


@pytest.mark.parametrize("source_count", (2, 4))
def test_three_profile_loft_rejects_n_minus_one_and_n_plus_one_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    source_count: int,
) -> None:
    document = _Document()
    session = _Session(document, {})
    called = False

    def apply(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("wrong source cardinality must remain inert")

    monkeypatch.setattr(profile_execution, "apply_part_profile_surface_plan", apply)
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_reviewed_intent_native(
            session,
            _program(PartProfileSurfaceOperation.LOFT, source_count=3),
            source_results=tuple(object() for _ in range(source_count)),
        )

    assert caught.value.code is ReviewedIntentExecutionErrorCode.EXECUTION_FAILED
    assert called is False
    assert document.Objects == []


def test_profile_surface_plan_document_tamper_fails_before_document_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_routes(monkeypatch)
    lowered = lower_reviewed_intent(_program(PartProfileSurfaceOperation.FACE))
    document = _Document()
    session = _Session(document, {})
    tampered_document = dataclasses.replace(
        lowered.result.plan_document,
        content_sha256=hashlib.sha256(b"tampered").hexdigest(),
    )

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_part_profile_surface_reviewed_plan(
            document,
            lowered.plan,
            lowered.payload,
            tampered_document,
            lowered.route.operation,
            _ReviewedFamilyExecutionContext(
                session=session,
                document=document,
                source_results=(),
            ),
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert document.Objects == []


@pytest.mark.parametrize(
    "operation",
    PART_PROFILE_SURFACE_REVIEWED_PRODUCT_OPERATIONS,
)
def test_profile_surface_source_aware_hook_consumes_only_ordered_reviewed_results(
    monkeypatch: pytest.MonkeyPatch,
    operation: PartProfileSurfaceOperation,
) -> None:
    _install_routes(monkeypatch)
    lowered = lower_reviewed_intent(
        _program(
            operation,
            source_count=3 if operation is PartProfileSurfaceOperation.LOFT else None,
        )
    )
    document = _Document()
    source_results = []
    identities = {}
    for index, selection in enumerate(lowered.plan.sources):
        plan_sha256 = hashlib.sha256(f"source-plan:{index}".encode()).hexdigest()
        profile_source = selection.role in {
            PartProfileSurfaceSourceRole.PROFILE,
            PartProfileSurfaceSourceRole.BOUNDARY,
        }
        route = REVIEWED_PART_CURVE_ROUTES[5 if profile_source else 3]
        source = _Feature(
            document,
            name=f"Source{index}",
            type_id=route.operation.native_type_id,
            shape_type="Wire" if profile_source else "Edge",
            edge_count=4 if profile_source else 1,
            solid_count=0,
            face_count=0,
            volume=0.0,
        )
        document.Objects.append(source)
        identities[source] = EntityIdentity(
            object_id=f"object_{index + 1:032x}",
            feature_id=f"feature_{index + 1:032x}",
            object_type=route.operation.native_type_id,
            semantic_role=SemanticRole.PRIMITIVE,
            provenance=Provenance(
                source=ProvenanceSource.MODEL,
                operation_id=f"reviewed_profile_source_{index}",
            ),
        )
        source_results.append(
            ReviewedNativeExecutionResult(
                route=route,
                object=source,
                plan_sha256=plan_sha256,
                plan_content_sha256=hashlib.sha256(f"source-content:{index}".encode()).hexdigest(),
                native_receipt=_SourceReceipt(
                    plan_sha256,
                    hashlib.sha256(source.Shape.exportBrepToString().encode()).hexdigest(),
                ),
            )
        )
    session = _Session(document, identities)

    def apply(raw, *, expected_content_sha256, expected_plan_sha256, bindings):
        assert raw == lowered.payload
        assert expected_content_sha256 == lowered.result.plan_document.content_sha256
        assert expected_plan_sha256 == lowered.result.plan_document.document_digest
        assert bindings.document is document
        assert tuple((item.node_id, item.result_id) for item in bindings.sources) == tuple(
            (item.node_id, item.result_id) for item in lowered.plan.sources
        )
        assert tuple(item.object.Name for item in bindings.sources) == tuple(
            f"Source{index}" for index in range(len(lowered.plan.sources))
        )
        name = f"Result_{operation.value}"
        invariant = PART_PROFILE_SURFACE_RESULT_INVARIANTS[operation]
        result = _Feature(
            document,
            name=name,
            type_id=lowered.route.operation.native_type_id,
            shape_type=invariant.shape_types[0],
            solid_count=invariant.solid_count,
            face_count=invariant.exact_face_count or invariant.minimum_face_count,
            area=1.0,
            volume=1.0 if invariant.require_positive_volume else 0.0,
        )
        document.Objects.append(result)
        return PartProfileSurfaceConformanceReceipt(
            plan_sha256=lowered.plan.plan_sha256,
            operation=operation,
            object_name=name,
            source_shape_sha256s=tuple(
                hashlib.sha256(item.object.Shape.exportBrepToString().encode()).hexdigest()
                for item in source_results
            ),
            result_shape_sha256=hashlib.sha256(
                result.Shape.exportBrepToString().encode()
            ).hexdigest(),
        )

    monkeypatch.setattr(profile_execution, "apply_part_profile_surface_plan", apply)
    result = execute_part_profile_surface_reviewed_plan_with_sources(
        document,
        lowered.plan,
        lowered.payload,
        lowered.result.plan_document,
        lowered.route.operation,
        tuple(source_results),
        session=session,
    )

    assert result.object is document.Objects[-1]
    assert result.object.TypeId == lowered.route.operation.native_type_id
    assert result.receipt.operation is operation
    invariant = PART_PROFILE_SURFACE_RESULT_INVARIANTS[operation]
    assert result.receipt.invariant is invariant
    assert result.receipt.native_type_id == invariant.native_type_id
    assert result.receipt.semantic_role is invariant.semantic_role
    observation = EntityObservation(
        object_id="object_" + "a" * 32,
        feature_id="feature_" + "b" * 32,
        object_type=invariant.native_type_id,
        semantic_role="feature",
        provenance={"source": "model", "operation_id": "apply_reviewed_intent"},
        placement=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
        volume_mm3=1.0 if invariant.require_positive_volume else 0.0,
        area_mm2=1.0,
        bbox_mm=(1.0, 1.0, 1.0),
        center_of_mass_mm=(0.0, 0.0, 0.0),
        valid_shape=True,
        solid_count=invariant.solid_count,
    )
    result.receipt.validate_adoption(document, result.object, observation)
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        result.receipt.validate_adopted_observation(
            dataclasses.replace(
                observation,
                solid_count=0 if invariant.solid_count == 1 else 1,
            )
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


def test_three_profile_loft_rejects_duplicate_managed_source_by_family_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lowered = lower_reviewed_intent(_program(PartProfileSurfaceOperation.LOFT, source_count=3))
    document = _Document()
    route = REVIEWED_PART_CURVE_ROUTES[5]
    source = _Feature(
        document,
        name="DuplicateProfile",
        type_id=route.operation.native_type_id,
        shape_type="Wire",
        edge_count=4,
        solid_count=0,
        face_count=0,
        volume=0.0,
    )
    document.Objects.append(source)
    identity = EntityIdentity(
        object_id="object_" + "d" * 32,
        feature_id="feature_" + "e" * 32,
        object_type=source.TypeId,
        semantic_role=SemanticRole.PRIMITIVE,
        provenance=Provenance(
            source=ProvenanceSource.MODEL,
            operation_id="reviewed_duplicate_profile",
        ),
    )
    plan_sha256 = hashlib.sha256(b"duplicate-profile-plan").hexdigest()
    source_result = ReviewedNativeExecutionResult(
        route=route,
        object=source,
        plan_sha256=plan_sha256,
        plan_content_sha256=hashlib.sha256(b"duplicate-profile-content").hexdigest(),
        native_receipt=_SourceReceipt(
            plan_sha256,
            hashlib.sha256(source.Shape.exportBrepToString().encode()).hexdigest(),
        ),
    )
    called = False

    def apply(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("duplicate sources must remain inert")

    monkeypatch.setattr(profile_execution, "apply_part_profile_surface_plan", apply)
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_part_profile_surface_reviewed_plan_with_sources(
            document,
            lowered.plan,
            lowered.payload,
            lowered.result.plan_document,
            lowered.route.operation,
            (source_result, source_result, source_result),
            session=_Session(document, {source: identity}),
        )

    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert called is False
    assert document.Objects == [source]


def test_profile_surface_source_aware_hook_rejects_stale_shape_before_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_routes(monkeypatch)
    lowered = lower_reviewed_intent(_program(PartProfileSurfaceOperation.FACE))
    document = _Document()
    route = REVIEWED_PART_CURVE_ROUTES[5]
    source = _Feature(
        document,
        name="SourceBoundary",
        type_id=route.operation.native_type_id,
        shape_type="Wire",
        edge_count=4,
        solid_count=0,
        face_count=0,
        volume=0.0,
    )
    document.Objects.append(source)
    session = _Session(
        document,
        {
            source: EntityIdentity(
                object_id="object_" + "1" * 32,
                feature_id="feature_" + "2" * 32,
                object_type=route.operation.native_type_id,
                semantic_role=SemanticRole.PRIMITIVE,
                provenance=Provenance(
                    source=ProvenanceSource.MODEL,
                    operation_id="reviewed_boundary",
                ),
            )
        },
    )
    plan_sha256 = hashlib.sha256(b"source-plan").hexdigest()
    source_result = ReviewedNativeExecutionResult(
        route=route,
        object=source,
        plan_sha256=plan_sha256,
        plan_content_sha256=hashlib.sha256(b"source-content").hexdigest(),
        native_receipt=_SourceReceipt(
            plan_sha256,
            hashlib.sha256(source.Shape.exportBrepToString().encode()).hexdigest(),
        ),
    )
    source.Shape.mutate()
    called = False

    def apply(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("stale source must not reach native apply")

    monkeypatch.setattr(profile_execution, "apply_part_profile_surface_plan", apply)
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_part_profile_surface_reviewed_plan_with_sources(
            document,
            lowered.plan,
            lowered.payload,
            lowered.result.plan_document,
            lowered.route.operation,
            (source_result,),
            session=session,
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert called is False
    assert document.Objects == [source]


def test_reviewed_primitive_cannot_masquerade_as_profile_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lowered = lower_reviewed_intent(_program(PartProfileSurfaceOperation.FACE))
    document = _Document()
    source = _Feature(
        document,
        name="ReviewedSolid",
        type_id=REVIEWED_PART_BOX_ROUTE.operation.native_type_id,
    )
    document.Objects.append(source)
    identity = EntityIdentity(
        object_id="object_" + "3" * 32,
        feature_id="feature_" + "4" * 32,
        object_type=source.TypeId,
        semantic_role=SemanticRole.PRIMITIVE,
        provenance=Provenance(
            source=ProvenanceSource.MODEL,
            operation_id="reviewed_solid",
        ),
    )
    plan_sha256 = hashlib.sha256(b"solid-plan").hexdigest()
    result = ReviewedNativeExecutionResult(
        route=REVIEWED_PART_BOX_ROUTE,
        object=source,
        plan_sha256=plan_sha256,
        plan_content_sha256=hashlib.sha256(b"solid-content").hexdigest(),
        native_receipt=_SourceReceipt(
            plan_sha256,
            hashlib.sha256(source.Shape.exportBrepToString().encode()).hexdigest(),
        ),
    )
    called = False

    def apply(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("solid source must not reach native apply")

    monkeypatch.setattr(profile_execution, "apply_part_profile_surface_plan", apply)
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_part_profile_surface_reviewed_plan_with_sources(
            document,
            lowered.plan,
            lowered.payload,
            lowered.result.plan_document,
            lowered.route.operation,
            (result,),
            session=_Session(document, {source: identity}),
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert called is False
    assert document.Objects == [source]
