"""Fast contract batch and one managed FreeCAD batch for Part curves."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from vibecad.intent_bridge.contracts import (
    BackendLoweringRequest,
    BridgeBudget,
    BridgeTermRef,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    ProducerBinding,
    ProducerDescriptor,
    ProofAssertion,
    ProofBundle,
    ProofEndpoint,
    SubjectRef,
)
from vibecad.intent_bridge.freecad_part_curve_adapter import (
    FREECAD_PART_CURVE_ADAPTER_DESCRIPTOR,
    PART_CURVE_CANONICAL_JSON_TERM,
    PART_CURVE_FAMILY_TERM,
    PART_CURVE_INTENT_DOCUMENT_ROLE_TERM,
    PART_CURVE_MANIFEST,
    PART_CURVE_OPERATION_TERMS,
    PART_CURVE_PARAMETERS_ROLE_TERM,
    PART_CURVE_PARAMETERS_TYPE_TERM,
    PART_CURVE_PFG_TERMS,
    PART_CURVE_REQUEST_TERMS,
    PART_CURVE_RESULT_ROLE_TERM,
    PART_CURVE_RESULT_TYPE_TERM,
    PART_CURVE_STRUCTURE_TERM,
    FreeCADPartCurveAdapter,
    build_part_curve_capability_document,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    ParametricFeatureGraphV2Codec,
)
from vibecad.intent_bridge.ports import TrustedCodecRegistry
from vibecad.intent_bridge.trusted_proof_policy import (
    RuleEndpointSignature,
    TrustedRuleEvaluation,
    TrustedRuleEvaluatorDescriptor,
    TrustedRulePolicy,
)
from vibecad.parametric.feature_graph_v2 import (
    DesignParameterV2,
    FeatureBodyV2,
    FeatureGraphResultV2,
    FeatureInputPortV2,
    FeatureIntentV2,
    FeatureNodeV2,
    FeatureParameterBindingV2,
    FeatureResultV2,
    ParametricFeatureGraphV2,
    TermTypedValueV2,
)
from vibecad.parametric.freecad_part_curve_rules import (
    MAX_PART_CURVE_PLAN_BYTES,
    PART_CURVE_NATIVE_SPECS,
    PartCurveBackendPlan,
    PartCurveOperation,
    PartCurveParameterSet,
    PartCurveRuleError,
    decode_part_curve_backend_plan,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _proof_term(term_ref_id: str, term_id: str) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.part-curve-proof-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"proof:{term_id}"),
    )


def _as_bridge(term) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


RULE = _proof_term("rule_part_curve_target", "rule.part-curve-target-reviewed")
PREDICATE = _proof_term("predicate_part_curve_target", "predicate.part-curve-target-reviewed")
PREMISE_ROLE = _proof_term("role_part_curve_candidate", "proof-role.part-curve-candidate")
CONCLUSION_ROLE = _proof_term("role_part_curve_validated", "proof-role.part-curve-validated")
STRUCTURE_BRIDGE = _as_bridge(PART_CURVE_STRUCTURE_TERM)


PARAMETERS = {
    PartCurveOperation.CIRCLE: {
        "geometry": {
            "radius_mm": 5.0,
            "start_angle_degrees": 10.0,
            "end_angle_degrees": 300.0,
        },
        "placement": {
            "translation_mm": [1.0, 2.0, 3.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 15.0,
        },
    },
    PartCurveOperation.ELLIPSE: {
        "geometry": {
            "major_radius_mm": 8.0,
            "minor_radius_mm": 4.0,
            "start_angle_degrees": 15.0,
            "end_angle_degrees": 270.0,
        },
        "placement": {
            "translation_mm": [2.0, 1.0, 3.0],
            "rotation_axis": [0.0, 1.0, 0.0],
            "rotation_degrees": 10.0,
        },
    },
    PartCurveOperation.HELIX: {
        "geometry": {
            "pitch_mm": 3.0,
            "height_mm": 12.0,
            "radius_mm": 4.0,
            "cone_angle_degrees": 5.0,
            "handedness": "Left-handed",
        },
        "placement": {
            "translation_mm": [3.0, 2.0, 1.0],
            "rotation_axis": [1.0, 0.0, 0.0],
            "rotation_degrees": 20.0,
        },
    },
    PartCurveOperation.LINE: {
        "geometry": {
            "x1_mm": 1.0,
            "y1_mm": 2.0,
            "z1_mm": 3.0,
            "x2_mm": 9.0,
            "y2_mm": 5.0,
            "z2_mm": 7.0,
        },
        "placement": {
            "translation_mm": [1.0, 0.0, 0.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 5.0,
        },
    },
    PartCurveOperation.PLANE: {
        "geometry": {"length_mm": 20.0, "width_mm": 30.0},
        "placement": {
            "translation_mm": [0.0, 1.0, 0.0],
            "rotation_axis": [1.0, 0.0, 0.0],
            "rotation_degrees": 30.0,
        },
    },
    PartCurveOperation.POLYGON: {
        "geometry": {
            "points_mm": [
                [0.0, 0.0, 0.0],
                [10.0, 0.0, 0.0],
                [12.0, 6.0, 0.0],
                [2.0, 8.0, 0.0],
            ],
            "closed": True,
        },
        "placement": {
            "translation_mm": [0.0, 0.0, 1.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 25.0,
        },
    },
    PartCurveOperation.REGULAR_POLYGON: {
        "geometry": {"side_count": 5, "circumradius_mm": 6.0},
        "placement": {
            "translation_mm": [2.0, 0.0, 1.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 12.0,
        },
    },
    PartCurveOperation.SPIRAL: {
        "geometry": {
            "growth_mm": 1.5,
            "start_radius_mm": 2.0,
            "rotations": 3.0,
            "segment_length_mm": 0.5,
        },
        "placement": {
            "translation_mm": [1.0, 1.0, 1.0],
            "rotation_axis": [0.0, 1.0, 0.0],
            "rotation_degrees": 18.0,
        },
    },
    PartCurveOperation.VERTEX: {
        "geometry": {"x_mm": 1.0, "y_mm": 2.0, "z_mm": 3.0},
        "placement": {
            "translation_mm": [4.0, 5.0, 6.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 0.0,
        },
    },
}


class _Evaluator:
    def __init__(self) -> None:
        def signature(role: BridgeTermRef) -> RuleEndpointSignature:
            return RuleEndpointSignature(
                selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
                role_term=role,
                subject_type_term=STRUCTURE_BRIDGE,
            )

        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="part_curve_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("part-curve-target-evaluator-v1"),
            rule_term=RULE,
            predicate_term=PREDICATE,
            premises=(signature(PREMISE_ROLE),),
            conclusions=(signature(CONCLUSION_ROLE),),
        )

    @property
    def descriptor(self) -> TrustedRuleEvaluatorDescriptor:
        return self._descriptor

    def validate(self, evaluation: TrustedRuleEvaluation) -> None:
        if (
            len(evaluation.documents) != 1
            or evaluation.premises[0].subject != _subject()
            or evaluation.conclusions[0].subject != _subject()
        ):
            raise IntentBridgeError(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/part_curve_target")


def _operation_terms(operation: PartCurveOperation):
    return next(item for item in PART_CURVE_OPERATION_TERMS if item.operation is operation)


def _graph(
    operation: PartCurveOperation,
    *,
    parameters: object | None = None,
    operation_definition: str | None = None,
) -> ParametricFeatureGraphV2:
    operation_terms = _operation_terms(operation)
    terms = list(PART_CURVE_PFG_TERMS)
    if operation_definition is not None:
        index = terms.index(operation_terms.operation_term)
        terms[index] = dataclasses.replace(
            operation_terms.operation_term,
            term_definition_sha256=operation_definition,
        )
    value = PARAMETERS[operation] if parameters is None else parameters
    parameter = DesignParameterV2(
        parameter_id="parameter_geometry",
        name="Reviewed curve parameters",
        semantic_role_term_ref_id=PART_CURVE_PARAMETERS_ROLE_TERM.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id="value_geometry",
            value_type_term_ref_id=PART_CURVE_PARAMETERS_TYPE_TERM.term_ref_id,
            encoding_term_ref_id=PART_CURVE_CANONICAL_JSON_TERM.term_ref_id,
            value=value,
        ),
    )
    node = FeatureNodeV2(
        node_id="node_target",
        body_id="body_part_document",
        name=f"Reviewed Part {operation.value}",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=PART_CURVE_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=PART_CURVE_FAMILY_TERM.term_ref_id,
            operation_term_ref_id=operation_terms.operation_term.term_ref_id,
            input_ports=(
                FeatureInputPortV2(
                    port_id="port_parameters",
                    semantic_role_term_ref_id=PART_CURVE_PARAMETERS_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=PART_CURVE_PARAMETERS_TYPE_TERM.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
            ),
            parameter_bindings=(
                FeatureParameterBindingV2(
                    binding_id="binding_parameters",
                    port_id="port_parameters",
                    parameter_id=parameter.parameter_id,
                ),
            ),
        ),
        results=(
            FeatureResultV2(
                result_id="result_target",
                semantic_role_term_ref_id=PART_CURVE_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=PART_CURVE_RESULT_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id=f"graph_part_curve_{operation.value}",
        name=f"Part curve {operation.value}",
        terms=tuple(terms),
        bodies=(FeatureBodyV2(body_id="body_part_document", name="Part document"),),
        parameters=(parameter,),
        references=(),
        nodes=(node,),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection_target",
                node_id=node.node_id,
                result_id="result_target",
            ),
        ),
    )


def _intent_document(graph: ParametricFeatureGraphV2) -> tuple[DocumentRef, bytes]:
    payload = graph.canonical_bytes
    return (
        DocumentRef(
            artifact_id="artifact_part_curve_pfg",
            role_term_ref_id=PART_CURVE_INTENT_DOCUMENT_ROLE_TERM.term_ref_id,
            schema_term_ref_id=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM.term_ref_id,
            document_id=graph.graph_id,
            document_digest=graph.graph_sha256,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
        ),
        payload,
    )


def _subject() -> SubjectRef:
    return SubjectRef(
        artifact_id="artifact_part_curve_pfg",
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id="node_target",
    )


def _proof(policy: TrustedRulePolicy, document: DocumentRef) -> ProofBundle:
    return ProofBundle(
        terms=(
            RULE,
            PREDICATE,
            PREMISE_ROLE,
            CONCLUSION_ROLE,
            STRUCTURE_BRIDGE,
            PART_CURVE_INTENT_DOCUMENT_ROLE_TERM,
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_part_curve_target",
                predicate_term_ref_id=PREDICATE.term_ref_id,
                rule_term_ref_id=RULE.term_ref_id,
                premises=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=PREMISE_ROLE.term_ref_id,
                        subject=_subject(),
                    ),
                ),
                conclusions=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=CONCLUSION_ROLE.term_ref_id,
                        subject=_subject(),
                    ),
                ),
            ),
        ),
        producer=ProducerBinding(
            descriptor=ProducerDescriptor(
                producer_id="part_curve_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("part-curve-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("part-curve-upstream-request"),
        ),
    )


class _Reader:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        payload = self.payloads[document.artifact_id]
        if len(payload) > maximum_bytes:
            raise RuntimeError("over budget")
        return payload


class _Sink:
    def __init__(self) -> None:
        self.items: dict[str, tuple[DocumentRef, bytes]] = {}

    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        existing = self.items.get(document.artifact_id)
        if existing is not None and existing != (document, payload):
            raise RuntimeError("collision")
        staged = dict(self.items)
        staged[document.artifact_id] = (document, payload)
        self.items = staged
        return payload

    def read_exact(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        stored_document, payload = self.items[document.artifact_id]
        if stored_document != document or len(payload) > maximum_bytes:
            raise RuntimeError("bad read")
        return payload


def _request(
    graph: ParametricFeatureGraphV2,
    *,
    max_output_bytes: int = MAX_PART_CURVE_PLAN_BYTES,
) -> tuple[BackendLoweringRequest, _Reader, TrustedRulePolicy]:
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = build_part_curve_capability_document()
    policy = TrustedRulePolicy(evaluators=(_Evaluator(),))
    request = BackendLoweringRequest(
        adapter=FREECAD_PART_CURVE_ADAPTER_DESCRIPTOR,
        terms=(
            *PART_CURVE_REQUEST_TERMS,
            RULE,
            PREDICATE,
            PREMISE_ROLE,
            CONCLUSION_ROLE,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(intent_document, capability_document),
        intent_artifact_ids=(intent_document.artifact_id,),
        capability_artifact_ids=(capability_document.artifact_id,),
        proof_bundle=_proof(policy, intent_document),
        budget=BridgeBudget(
            max_input_bytes=len(intent_payload) + len(capability_payload),
            max_output_bytes=max_output_bytes,
            max_subject_lookups=1,
            max_rule_applications=1,
        ),
    )
    return (
        request,
        _Reader(
            {
                intent_document.artifact_id: intent_payload,
                capability_document.artifact_id: capability_payload,
            }
        ),
        policy,
    )


def _lower(adapter, request, reader, policy):
    return adapter.lower_with_receipt(
        request,
        artifacts=reader,
        codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
        proof_policy=policy,
    )


def test_part_curve_fast_batch_all_nine_operations_share_g0_engine() -> None:
    assert len(PART_CURVE_MANIFEST.operations) == 9
    assert set(PART_CURVE_NATIVE_SPECS) == set(PartCurveOperation)
    capability_document, capability_payload = build_part_curve_capability_document()
    assert capability_document.document_digest == PART_CURVE_MANIFEST.manifest_sha256
    assert capability_payload == PART_CURVE_MANIFEST.canonical_bytes

    for operation in PartCurveOperation:
        request, reader, policy = _request(_graph(operation))
        sink = _Sink()
        adapter = FreeCADPartCurveAdapter(sink)
        result, receipt = _lower(adapter, request, reader, policy)
        decoded, payload = adapter.read_plan(receipt)
        repeated, repeated_receipt = _lower(adapter, request, reader, policy)

        assert isinstance(decoded, PartCurveBackendPlan)
        assert decoded.operation is operation
        assert decoded.parameters == PartCurveParameterSet.from_value(
            operation, PARAMETERS[operation]
        )
        assert receipt.operation.operation_id == operation.value
        assert receipt.operation.native_type_id == PART_CURVE_NATIVE_SPECS[operation].type_id
        assert decoded.operation_specification_sha256 == receipt.operation.specification_sha256
        assert result.supported_subjects == (_subject(),)
        assert result.plan_document == receipt.plan_document
        assert adapter.executable is False and adapter.grants_execution_authority is False
        assert receipt.executable is False and receipt.grants_execution_authority is False
        assert decoded.executable is False and decoded.grants_execution_authority is False
        assert payload == decoded.canonical_bytes
        assert b"Part::" not in payload
        assert repeated == result and repeated_receipt == receipt and len(sink.items) == 1
        assert (
            decode_part_curve_backend_plan(
                payload,
                expected_content_sha256=result.plan_document.content_sha256,
                expected_plan_sha256=result.plan_document.document_digest,
            )
            == decoded
        )


def test_part_curve_invalid_graphs_fail_closed_before_publication() -> None:
    invalid_parameters = dict(PARAMETERS[PartCurveOperation.LINE])
    invalid_parameters["geometry"] = {
        "x1_mm": 1.0,
        "y1_mm": 2.0,
        "z1_mm": 3.0,
        "x2_mm": 1.0,
        "y2_mm": 2.0,
        "z2_mm": 3.0,
    }
    cases = (
        _graph(
            PartCurveOperation.CIRCLE,
            operation_definition="f" * 64,
        ),
        _graph(PartCurveOperation.LINE, parameters=invalid_parameters),
        _graph(
            PartCurveOperation.ELLIPSE,
            parameters={
                **PARAMETERS[PartCurveOperation.ELLIPSE],
                "geometry": {
                    **PARAMETERS[PartCurveOperation.ELLIPSE]["geometry"],
                    "major_radius_mm": 4.0,
                    "minor_radius_mm": 4.0,
                },
            },
        ),
        _graph(
            PartCurveOperation.HELIX,
            parameters={
                **PARAMETERS[PartCurveOperation.HELIX],
                "geometry": {
                    **PARAMETERS[PartCurveOperation.HELIX]["geometry"],
                    "pitch_mm": 0.001,
                    "height_mm": 1_000_000.0,
                },
            },
        ),
        _graph(
            PartCurveOperation.SPIRAL,
            parameters={
                **PARAMETERS[PartCurveOperation.SPIRAL],
                "geometry": {
                    **PARAMETERS[PartCurveOperation.SPIRAL]["geometry"],
                    "segment_length_mm": 0.001,
                    "rotations": 100.0,
                },
            },
        ),
    )
    for graph in cases:
        request, reader, policy = _request(graph)
        sink = _Sink()
        with pytest.raises(IntentBridgeError) as error:
            _lower(FreeCADPartCurveAdapter(sink), request, reader, policy)
        assert error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
        assert sink.items == {}


def test_part_curve_plan_canonical_budget_and_tamper_gates() -> None:
    request, reader, policy = _request(_graph(PartCurveOperation.POLYGON))
    probe = FreeCADPartCurveAdapter(_Sink())
    _, receipt = _lower(probe, request, reader, policy)
    _, payload = probe.read_plan(receipt)
    with pytest.raises(PartCurveRuleError):
        decode_part_curve_backend_plan(payload + b" ")

    exact_request, exact_reader, exact_policy = _request(
        _graph(PartCurveOperation.POLYGON), max_output_bytes=len(payload)
    )
    exact_result, _ = _lower(
        FreeCADPartCurveAdapter(_Sink()), exact_request, exact_reader, exact_policy
    )
    assert exact_result.plan_document.size_bytes == len(payload)

    small_request, small_reader, small_policy = _request(
        _graph(PartCurveOperation.POLYGON), max_output_bytes=len(payload) - 1
    )
    sink = _Sink()
    with pytest.raises(IntentBridgeError) as error:
        _lower(FreeCADPartCurveAdapter(sink), small_request, small_reader, small_policy)
    assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert sink.items == {}


@pytest.mark.slow
def test_real_freecad_part_curve_batch_create_edit_reopen_noop_and_rollback(
    tmp_path: Path,
) -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the real FreeCAD batch gate")
    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime import status as runtime_status

    runtime_python = runtime_paths.active_runtime_python()
    if not runtime_python.is_file() or not runtime_paths.ready_sentinel().is_file():
        pytest.fail("an existing ready managed FreeCAD runtime is required")
    if not runtime_status.engine_compatible(runtime_python):
        pytest.fail("the existing managed FreeCAD runtime does not match current engine pins")

    edits = {
        PartCurveOperation.CIRCLE: ("Radius", 7.0),
        PartCurveOperation.ELLIPSE: ("MajorRadius", 10.0),
        PartCurveOperation.HELIX: ("Radius", 5.0),
        PartCurveOperation.LINE: ("X2", 12.0),
        PartCurveOperation.PLANE: ("Length", 25.0),
        PartCurveOperation.POLYGON: (
            "Nodes",
            [[0.0, 0.0, 0.0], [14.0, 0.0, 0.0], [12.0, 6.0, 0.0], [2.0, 8.0, 0.0]],
        ),
        PartCurveOperation.REGULAR_POLYGON: ("Circumradius", 8.0),
        PartCurveOperation.SPIRAL: ("Radius", 3.0),
        PartCurveOperation.VERTEX: ("X", 4.0),
    }
    cases = []
    for operation in PartCurveOperation:
        request, reader, policy = _request(_graph(operation))
        adapter = FreeCADPartCurveAdapter(_Sink())
        result, receipt = _lower(adapter, request, reader, policy)
        plan, payload = adapter.read_plan(receipt)
        plan_path = tmp_path / f"part-curve-{operation.value}.json"
        plan_path.write_bytes(payload)
        cases.append(
            {
                "operation": operation.value,
                "type_id": PART_CURVE_NATIVE_SPECS[operation].type_id,
                "path": str(plan_path),
                "content_sha256": result.plan_document.content_sha256,
                "plan_sha256": result.plan_document.document_digest,
                "edit": edits[operation],
                "plan_operation": plan.operation.value,
                "adapter_contract_sha256": receipt.adapter.adapter_contract_sha256,
                "manifest_sha256": receipt.manifest_sha256,
                "operation_specification_sha256": receipt.operation.specification_sha256,
            }
        )

    model_path = tmp_path / "part-curves.FCStd"
    source_root = Path(__file__).parents[1] / "src"
    code = f"""
import math, os, sys
sys.path.insert(0, {str(source_root)!r})
from vibecad.freecad_env import prepare_freecad_import
prepare_freecad_import()
from pathlib import Path
import FreeCAD
from vibecad.parametric.freecad_part_curve_rules import (
    PartCurveExecutionBindings,
    PartCurveRuleError,
    apply_part_curve_plan,
)
from vibecad.parametric.freecad_reviewed_transaction import (
    NativeTransactionError,
    NativeTransactionRunner,
)

CASES = {cases!r}

def fingerprint(shape):
    box = shape.BoundBox
    return (
        shape.ShapeType,
        len(shape.Vertexes), len(shape.Edges), len(shape.Faces),
        float(shape.Length), float(shape.Area),
        float(box.XMin), float(box.YMin), float(box.ZMin),
        float(box.XMax), float(box.YMax), float(box.ZMax),
    )

def different(left, right):
    if left[:4] != right[:4]:
        return True
    return any(
        not math.isclose(a, b, rel_tol=0.0, abs_tol=1e-8)
        for a, b in zip(left[4:], right[4:], strict=True)
    )

def native_value(value):
    if type(value) is list:
        return [FreeCAD.Vector(*item) for item in value]
    return value

def property_value(value):
    try:
        return tuple(tuple(float(item[index]) for index in range(3)) for item in value)
    except Exception:
        try:
            return float(value)
        except Exception:
            return str(value)

document = FreeCAD.newDocument('PartCurveBatch')
document.UndoMode = 1
persisted = []
for entry in CASES:
    payload = Path(entry['path']).read_bytes()
    before = tuple(document.Objects)
    receipt = apply_part_curve_plan(
        payload,
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=PartCurveExecutionBindings(
            document=document,
            expected_adapter_contract_sha256=entry['adapter_contract_sha256'],
            expected_manifest_sha256=entry['manifest_sha256'],
            expected_operation_specification_sha256=entry['operation_specification_sha256'],
        ),
    )
    feature = document.getObject(receipt.object_name)
    assert feature.TypeId == entry['type_id']
    assert feature.isValid() and tuple(feature.State) == ('Up-to-date',)
    assert not feature.Shape.isNull() and feature.Shape.isValid()
    assert tuple(document.Objects) == (*before, feature)
    initial = fingerprint(feature.Shape)
    property_name, edit_value = entry['edit']
    setattr(feature, property_name, native_value(edit_value))
    document.recompute()
    edited = fingerprint(feature.Shape)
    assert different(initial, edited)
    persisted.append((
        feature.Name,
        entry['type_id'],
        property_name,
        property_value(getattr(feature, property_name)),
        edited,
    ))

    # Same content-addressed object is a fail-closed no-op.
    before_noop = tuple(document.Objects)
    try:
        apply_part_curve_plan(
            payload,
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=PartCurveExecutionBindings(
                document=document,
                expected_adapter_contract_sha256=entry['adapter_contract_sha256'],
                expected_manifest_sha256=entry['manifest_sha256'],
                expected_operation_specification_sha256=entry['operation_specification_sha256'],
            ),
        )
    except PartCurveRuleError:
        pass
    else:
        raise AssertionError('duplicate plan must fail')
    assert tuple(document.Objects) == before_noop

# Decoder failure is before mutation.
before_invalid = tuple(document.Objects)
try:
    apply_part_curve_plan(
        Path(CASES[0]['path']).read_bytes() + b' ',
        expected_content_sha256=CASES[0]['content_sha256'],
        expected_plan_sha256=CASES[0]['plan_sha256'],
        bindings=PartCurveExecutionBindings(
            document=document,
            expected_adapter_contract_sha256=CASES[0]['adapter_contract_sha256'],
            expected_manifest_sha256=CASES[0]['manifest_sha256'],
            expected_operation_specification_sha256=CASES[0]['operation_specification_sha256'],
        ),
    )
except PartCurveRuleError:
    pass
else:
    raise AssertionError('tampered plan must fail')
assert tuple(document.Objects) == before_invalid

# The runtime repeats the reviewed adapter/manifest/operation binding before mutation.
try:
    apply_part_curve_plan(
        Path(CASES[0]['path']).read_bytes(),
        expected_content_sha256=CASES[0]['content_sha256'],
        expected_plan_sha256=CASES[0]['plan_sha256'],
        bindings=PartCurveExecutionBindings(
            document=document,
            expected_adapter_contract_sha256='f' * 64,
            expected_manifest_sha256=CASES[0]['manifest_sha256'],
            expected_operation_specification_sha256=CASES[0]['operation_specification_sha256'],
        ),
    )
except PartCurveRuleError:
    pass
else:
    raise AssertionError('wrong reviewed binding must fail')
assert tuple(document.Objects) == before_invalid

# The shared runner proves a real late native failure restores the document.
before_rollback = tuple(document.Objects)
def fail_after_create():
    document.addObject('Part::Circle', 'RollbackProbe')
    raise RuntimeError('late native failure')
try:
    NativeTransactionRunner().run(
        document,
        label='VibeCAD Part curve rollback gate',
        snapshot=lambda: before_rollback,
        apply=fail_after_create,
        rollback_matches=lambda expected: tuple(document.Objects) == expected,
    )
except NativeTransactionError:
    pass
else:
    raise AssertionError('late failure must roll back')
assert tuple(document.Objects) == before_rollback
assert document.getObject('RollbackProbe') is None
assert not document.HasPendingTransaction

document.saveAs({str(model_path)!r})
FreeCAD.closeDocument(document.Name)
reopened = FreeCAD.openDocument({str(model_path)!r})
reopened.recompute()
for name, type_id, property_name, expected_property, expected_shape in persisted:
    feature = reopened.getObject(name)
    assert feature.TypeId == type_id
    assert feature.isValid() and tuple(feature.State) == ('Up-to-date',)
    assert property_value(getattr(feature, property_name)) == expected_property
    actual = fingerprint(feature.Shape)
    assert actual[:4] == expected_shape[:4]
    assert all(
        math.isclose(a, b, rel_tol=0.0, abs_tol=1e-6)
        for a, b in zip(actual[4:], expected_shape[4:], strict=True)
    )
FreeCAD.closeDocument(reopened.Name)
print('REAL_PART_CURVE_BATCH_OK')
"""
    completed = subprocess.run(
        [str(runtime_python), "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    assert "REAL_PART_CURVE_BATCH_OK" in completed.stdout
