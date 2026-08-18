from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass

import pytest

import vibecad.execution.freecad_part_curve_reviewed_execution as curve_execution
import vibecad.execution.freecad_reviewed_intent_execution as shared_execution
from tests.test_intent_bridge_freecad_part_curve_adapter import _graph
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_part_curve_reviewed_execution import (
    PART_CURVE_REVIEWED_FAMILY_SPEC,
    PART_CURVE_REVIEWED_PRODUCT_IDENTITIES,
    PART_CURVE_REVIEWED_PRODUCT_OPERATIONS,
    execute_part_curve_reviewed_plan,
    part_curve_reviewed_adapter_factory,
    resolve_part_curve_reviewed_operation,
    validate_part_curve_reviewed_plan,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PART_CURVE_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    _ReviewedFamilyExecutionContext,
    lower_reviewed_intent,
    route_reviewed_intent,
)
from vibecad.intent_bridge.contracts import DocumentRef
from vibecad.intent_bridge.freecad_part_curve_adapter import (
    PART_CURVE_MANIFEST,
    FreeCADPartCurveAdapter,
)
from vibecad.intent_bridge.reviewed_family_engine import ReviewedPlanReceipt
from vibecad.parametric.freecad_part_curve_rules import (
    PART_CURVE_NATIVE_SPECS,
    PartCurveBackendPlan,
    PartCurveConformanceReceipt,
    PartCurveOperation,
    PartCurveParameterSet,
    PartCurveShapeSignature,
)
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1

_PARAMETERS: dict[PartCurveOperation, dict[str, object]] = {
    PartCurveOperation.CIRCLE: {
        "geometry": {
            "radius_mm": 5.0,
            "start_angle_degrees": 0.0,
            "end_angle_degrees": 360.0,
        },
        "placement": {
            "translation_mm": [0.0, 0.0, 0.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 0.0,
        },
    },
    PartCurveOperation.ELLIPSE: {
        "geometry": {
            "major_radius_mm": 8.0,
            "minor_radius_mm": 4.0,
            "start_angle_degrees": 0.0,
            "end_angle_degrees": 360.0,
        },
        "placement": {
            "translation_mm": [0.0, 0.0, 0.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 0.0,
        },
    },
    PartCurveOperation.HELIX: {
        "geometry": {
            "pitch_mm": 3.0,
            "height_mm": 12.0,
            "radius_mm": 4.0,
            "cone_angle_degrees": 0.0,
            "handedness": "Right-handed",
        },
        "placement": {
            "translation_mm": [0.0, 0.0, 0.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 0.0,
        },
    },
    PartCurveOperation.LINE: {
        "geometry": {
            "x1_mm": 0.0,
            "y1_mm": 0.0,
            "z1_mm": 0.0,
            "x2_mm": 5.0,
            "y2_mm": 2.0,
            "z2_mm": 1.0,
        },
        "placement": {
            "translation_mm": [0.0, 0.0, 0.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 0.0,
        },
    },
    PartCurveOperation.PLANE: {
        "geometry": {"length_mm": 20.0, "width_mm": 30.0},
        "placement": {
            "translation_mm": [0.0, 0.0, 0.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 0.0,
        },
    },
    PartCurveOperation.POLYGON: {
        "geometry": {
            "points_mm": [
                [0.0, 0.0, 0.0],
                [4.0, 0.0, 0.0],
                [4.0, 3.0, 0.0],
            ],
            "closed": True,
        },
        "placement": {
            "translation_mm": [0.0, 0.0, 0.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 0.0,
        },
    },
    PartCurveOperation.REGULAR_POLYGON: {
        "geometry": {"side_count": 5, "circumradius_mm": 6.0},
        "placement": {
            "translation_mm": [0.0, 0.0, 0.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 0.0,
        },
    },
    PartCurveOperation.SPIRAL: {
        "geometry": {
            "growth_mm": 1.0,
            "start_radius_mm": 2.0,
            "rotations": 2.0,
            "segment_length_mm": 0.5,
        },
        "placement": {
            "translation_mm": [0.0, 0.0, 0.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 0.0,
        },
    },
    PartCurveOperation.VERTEX: {
        "geometry": {"x_mm": 1.0, "y_mm": 2.0, "z_mm": 3.0},
        "placement": {
            "translation_mm": [0.0, 0.0, 0.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 0.0,
        },
    },
}


def _reviewed_operation(operation: PartCurveOperation):
    return next(
        item for item in PART_CURVE_MANIFEST.operations if item.operation_id == operation.value
    )


def _reviewed_program(operation: PartCurveOperation) -> ReviewedIntentProgramV1:
    graph = _graph(operation)
    reviewed = _reviewed_operation(operation)
    namespace, version, term_id, digest = reviewed.semantic_term.semantic_identity
    return ReviewedIntentProgramV1(
        operation_id=f"{PART_CURVE_MANIFEST.family_id}.{operation.value}",
        semantic_operation=f"{namespace}/{version}/{term_id}@{digest}",
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


def _source_document(operation: PartCurveOperation) -> DocumentRef:
    return DocumentRef(
        artifact_id=f"artifact_curve_{operation.value}",
        role_term_ref_id=PART_CURVE_MANIFEST.intent_role_term.term_ref_id,
        schema_term_ref_id=PART_CURVE_MANIFEST.intent_schema_term.term_ref_id,
        document_id=f"graph_curve_{operation.value}",
        document_digest=hashlib.sha256(f"graph:{operation.value}".encode()).hexdigest(),
        content_sha256=hashlib.sha256(f"content:{operation.value}".encode()).hexdigest(),
        size_bytes=128,
        media_type=PART_CURVE_MANIFEST.intent_media_type,
    )


def _plan_and_receipt(
    operation: PartCurveOperation,
) -> tuple[PartCurveBackendPlan, ReviewedPlanReceipt]:
    reviewed = _reviewed_operation(operation)
    source = _source_document(operation)
    request_digest = hashlib.sha256(f"request:{operation.value}".encode()).hexdigest()
    plan = PartCurveBackendPlan(
        source_artifact_id=source.artifact_id,
        source_graph_id=source.document_id,
        source_graph_sha256=source.document_digest,
        source_content_sha256=source.content_sha256,
        lowering_request_sha256=request_digest,
        adapter_contract_sha256=PART_CURVE_MANIFEST.adapter.adapter_contract_sha256,
        manifest_sha256=PART_CURVE_MANIFEST.manifest_sha256,
        operation_specification_sha256=reviewed.specification_sha256,
        body_id="body_main",
        node_id=f"node_{operation.value}",
        result_id=f"result_{operation.value}",
        parameter_id=f"parameter_{operation.value}",
        value_id=f"value_{operation.value}",
        operation=operation,
        parameters=PartCurveParameterSet.from_value(operation, _PARAMETERS[operation]),
    )
    plan_document = PART_CURVE_MANIFEST.plan_document(plan.canonical_bytes, plan.plan_sha256)
    return plan, ReviewedPlanReceipt(
        manifest_sha256=PART_CURVE_MANIFEST.manifest_sha256,
        request_digest=request_digest,
        adapter=PART_CURVE_MANIFEST.adapter,
        operation=reviewed,
        source_document=source,
        plan_document=plan_document,
    )


class _Sink:
    def __init__(self) -> None:
        self.items: dict[str, tuple[DocumentRef, bytes]] = {}

    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        self.items[document.artifact_id] = (document, payload)
        return payload

    def read_exact(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        expected, payload = self.items[document.artifact_id]
        assert expected == document
        assert len(payload) <= maximum_bytes
        return payload


def test_curve_family_spec_exposes_all_nine_exact_reviewed_identities() -> None:
    assert PART_CURVE_REVIEWED_PRODUCT_OPERATIONS == tuple(PartCurveOperation)
    assert len(PART_CURVE_REVIEWED_PRODUCT_IDENTITIES) == 9
    assert PART_CURVE_REVIEWED_FAMILY_SPEC.manifest is PART_CURVE_MANIFEST
    assert PART_CURVE_REVIEWED_FAMILY_SPEC.operation_ids == tuple(
        item.value for item in PartCurveOperation
    )
    assert (
        tuple(
            (route.operation_id, route.semantic_operation) for route in REVIEWED_PART_CURVE_ROUTES
        )
        == PART_CURVE_REVIEWED_PRODUCT_IDENTITIES
    )

    formal = current_freecad_intent_capability_specs()
    for operation, identity in zip(
        PART_CURVE_REVIEWED_PRODUCT_OPERATIONS,
        PART_CURVE_REVIEWED_PRODUCT_IDENTITIES,
        strict=True,
    ):
        reviewed = _reviewed_operation(operation)
        assert resolve_part_curve_reviewed_operation(*identity) is reviewed
        matching = tuple(item for item in formal if item.operation_id == identity[0])
        assert len(matching) == 1
        assert matching[0].semantic_operation == identity[1]
        assert matching[0].native_type_id == reviewed.native_type_id

    adapter = part_curve_reviewed_adapter_factory(_Sink())
    assert type(adapter) is FreeCADPartCurveAdapter
    assert adapter.manifest is PART_CURVE_MANIFEST


@pytest.mark.parametrize("operation", PART_CURVE_REVIEWED_PRODUCT_OPERATIONS)
def test_shared_product_bridge_routes_and_lowers_all_nine_curves(
    operation: PartCurveOperation,
) -> None:
    program = _reviewed_program(operation)

    route = route_reviewed_intent(program)
    lowered = lower_reviewed_intent(program)

    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 39
    assert route in REVIEWED_PART_CURVE_ROUTES
    assert route.operation_id == program.operation_id
    assert route.semantic_operation == program.semantic_operation
    assert lowered.route is route
    assert type(lowered.plan) is PartCurveBackendPlan
    assert lowered.plan.operation is operation
    assert lowered.receipt.operation is route.operation
    assert lowered.result.plan_document.document_digest == lowered.plan.plan_sha256


def test_curve_identity_router_is_inert_for_unknown_or_tampered_values() -> None:
    operation_id, semantic_operation = PART_CURVE_REVIEWED_PRODUCT_IDENTITIES[0]
    assert (
        resolve_part_curve_reviewed_operation(operation_id + "_unknown", semantic_operation) is None
    )
    assert (
        resolve_part_curve_reviewed_operation(operation_id, semantic_operation[:-1] + "0") is None
    )
    assert resolve_part_curve_reviewed_operation(None, semantic_operation) is None
    assert resolve_part_curve_reviewed_operation(operation_id, None) is None


@pytest.mark.parametrize("operation", PART_CURVE_REVIEWED_PRODUCT_OPERATIONS)
def test_curve_family_validates_canonical_plan_binding(
    operation: PartCurveOperation,
) -> None:
    plan, receipt = _plan_and_receipt(operation)

    validate_part_curve_reviewed_plan(plan, receipt, receipt.operation)
    assert receipt.plan_document.content_sha256 == hashlib.sha256(plan.canonical_bytes).hexdigest()
    assert receipt.plan_document.document_digest == plan.plan_sha256

    tampered = dataclasses.replace(
        receipt,
        request_digest=hashlib.sha256(b"tampered").hexdigest(),
    )
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        validate_part_curve_reviewed_plan(plan, tampered, receipt.operation)
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


@dataclass
class _NativeResult:
    object: object
    receipt: object


class _Feature:
    def __init__(self, document: _Document, *, name: str, type_id: str) -> None:
        self.Document = document
        self.Name = name
        self.TypeId = type_id


class _Document:
    def __init__(self) -> None:
        self.Objects: list[object] = []

    def getObject(self, name: str):
        return next((item for item in self.Objects if getattr(item, "Name", None) == name), None)


@pytest.mark.parametrize("operation", PART_CURVE_REVIEWED_PRODUCT_OPERATIONS)
def test_curve_family_native_callback_returns_exact_product_object(
    monkeypatch: pytest.MonkeyPatch,
    operation: PartCurveOperation,
) -> None:
    plan, lowered = _plan_and_receipt(operation)
    document = _Document()
    reviewed = lowered.operation
    native_spec = PART_CURVE_NATIVE_SPECS[operation]

    def apply(raw, *, expected_content_sha256, expected_plan_sha256, bindings):
        assert raw == plan.canonical_bytes
        assert expected_content_sha256 == lowered.plan_document.content_sha256
        assert expected_plan_sha256 == lowered.plan_document.document_digest
        assert bindings.document is document
        name = f"{native_spec.object_prefix}_{plan.plan_sha256[:16]}"
        feature = _Feature(document, name=name, type_id=reviewed.native_type_id)
        document.Objects.append(feature)
        return PartCurveConformanceReceipt(
            plan_sha256=plan.plan_sha256,
            operation=operation,
            object_name=name,
            shape=PartCurveShapeSignature(
                shape_type=native_spec.shape_type,
                vertex_count=native_spec.minimum_vertices,
                edge_count=native_spec.minimum_edges,
                face_count=native_spec.minimum_faces,
                length_mm=1.0,
                area_mm2=1.0,
            ),
        )

    monkeypatch.setattr(curve_execution, "apply_part_curve_plan", apply)
    monkeypatch.setattr(
        shared_execution,
        "_ReviewedFamilyNativeExecution",
        _NativeResult,
        raising=False,
    )

    result = execute_part_curve_reviewed_plan(
        document,
        plan,
        plan.canonical_bytes,
        lowered.plan_document,
        reviewed,
        _ReviewedFamilyExecutionContext(
            session=object(),
            document=document,
            source_results=(),
        ),
    )

    assert result.object is document.Objects[0]
    assert result.object.Document is document
    assert result.object.TypeId == reviewed.native_type_id
    assert result.receipt.operation is operation


def test_curve_family_tamper_fails_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, lowered = _plan_and_receipt(PartCurveOperation.CIRCLE)
    document = _Document()
    called = False

    def apply(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("native apply must remain inert")

    monkeypatch.setattr(curve_execution, "apply_part_curve_plan", apply)
    tampered_document = dataclasses.replace(
        lowered.plan_document,
        content_sha256=hashlib.sha256(b"tampered").hexdigest(),
    )

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_part_curve_reviewed_plan(
            document,
            plan,
            plan.canonical_bytes,
            tampered_document,
            lowered.operation,
            _ReviewedFamilyExecutionContext(
                session=object(),
                document=document,
                source_results=(),
            ),
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert called is False
    assert document.Objects == []
