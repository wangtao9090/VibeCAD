"""Focused contracts and one real FreeCAD batch for Part profile/surfaces."""

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
from vibecad.intent_bridge.freecad_part_profile_surface_adapter import (
    FREECAD_PART_PROFILE_SURFACE_ADAPTER_DESCRIPTOR,
    PART_PROFILE_SURFACE_CANONICAL_JSON_TERM,
    PART_PROFILE_SURFACE_FAMILY_TERM,
    PART_PROFILE_SURFACE_INTENT_ROLE_TERM,
    PART_PROFILE_SURFACE_MANIFEST,
    PART_PROFILE_SURFACE_OPERATION_TERMS,
    PART_PROFILE_SURFACE_PARAMETERS_ROLE_TERM,
    PART_PROFILE_SURFACE_PARAMETERS_TYPE_TERM,
    PART_PROFILE_SURFACE_PFG_TERMS,
    PART_PROFILE_SURFACE_REQUEST_TERMS,
    PART_PROFILE_SURFACE_RESULT_ROLE_TERM,
    PART_PROFILE_SURFACE_SHAPE_TYPE_TERM,
    PART_PROFILE_SURFACE_SOURCE_FAMILY_TERM,
    PART_PROFILE_SURFACE_SOURCE_OPERATION_TERM,
    PART_PROFILE_SURFACE_SOURCE_RESULT_ROLE_TERM,
    PART_PROFILE_SURFACE_SOURCE_ROLE_TERMS,
    PART_PROFILE_SURFACE_SOURCE_STRUCTURE_TERM,
    PART_PROFILE_SURFACE_STRUCTURE_TERM,
    FreeCADPartProfileSurfaceAdapter,
    build_part_profile_surface_capability_document,
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
    FeatureDependencyV2,
    FeatureGraphResultV2,
    FeatureInputPortV2,
    FeatureIntentV2,
    FeatureNodeV2,
    FeatureParameterBindingV2,
    FeatureResultV2,
    ParametricFeatureGraphV2,
    TermTypedValueV2,
)
from vibecad.parametric.freecad_part_profile_surface_rules import (
    MAX_PART_PROFILE_SURFACE_PLAN_BYTES,
    PART_PROFILE_SURFACE_NATIVE_SPECS,
    PartProfileSurfaceBackendPlan,
    PartProfileSurfaceOperation,
    PartProfileSurfaceParameterSet,
    PartProfileSurfaceRuleError,
    PartProfileSurfaceSourceRole,
    decode_part_profile_surface_backend_plan,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _proof_term(term_ref_id: str, term_id: str) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.part-profile-surface-proof-test",
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


RULE = _proof_term("rule_part_profile_surface_target", "rule.part-profile-surface-reviewed")
PREDICATE = _proof_term(
    "predicate_part_profile_surface_target",
    "predicate.part-profile-surface-reviewed",
)
PREMISE_ROLE = _proof_term(
    "role_part_profile_surface_candidate",
    "proof-role.part-profile-surface-candidate",
)
CONCLUSION_ROLE = _proof_term(
    "role_part_profile_surface_validated",
    "proof-role.part-profile-surface-validated",
)
STRUCTURE_BRIDGE = _as_bridge(PART_PROFILE_SURFACE_STRUCTURE_TERM)

PARAMETERS = {
    PartProfileSurfaceOperation.EXTRUSION: {
        "direction": [0.0, 0.0, 1.0],
        "forward_length_mm": 8.0,
        "reverse_length_mm": 0.0,
    },
    PartProfileSurfaceOperation.REVOLUTION: {
        "axis_origin_mm": [0.0, 0.0, 0.0],
        "axis_direction": [0.0, 0.0, 1.0],
        "angle_degrees": 270.0,
    },
    PartProfileSurfaceOperation.LOFT: {"ruled": False},
    PartProfileSurfaceOperation.SWEEP: {"frenet": True},
    PartProfileSurfaceOperation.RULED_SURFACE: {},
    PartProfileSurfaceOperation.FACE: {},
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
            evaluator_id="part_profile_surface_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("part-profile-surface-target-evaluator-v1"),
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
            raise IntentBridgeError(
                IntentBridgeErrorCode.AUTHORITY_VIOLATION,
                "/part_profile_surface_target",
            )


def _operation_terms(operation: PartProfileSurfaceOperation):
    return next(
        item for item in PART_PROFILE_SURFACE_OPERATION_TERMS if item.operation is operation
    )


def _source_id(role: PartProfileSurfaceSourceRole, ordinal: int) -> tuple[str, str]:
    return f"node_source_{role.value}_{ordinal}", f"result_source_{role.value}_{ordinal}"


def _source_count(
    operation: PartProfileSurfaceOperation,
    role: PartProfileSurfaceSourceRole,
    minimum: int,
) -> int:
    if (
        operation is PartProfileSurfaceOperation.SWEEP
        and role is PartProfileSurfaceSourceRole.PROFILE
    ):
        return 2
    return minimum


def _graph(
    operation: PartProfileSurfaceOperation,
    *,
    parameters: object | None = None,
    source_operation_definition: str | None = None,
) -> ParametricFeatureGraphV2:
    operation_terms = _operation_terms(operation)
    terms = list(PART_PROFILE_SURFACE_PFG_TERMS)
    if source_operation_definition is not None:
        index = terms.index(PART_PROFILE_SURFACE_SOURCE_OPERATION_TERM)
        terms[index] = dataclasses.replace(
            PART_PROFILE_SURFACE_SOURCE_OPERATION_TERM,
            term_definition_sha256=source_operation_definition,
        )
    body_id = "body_part_document"
    sources = []
    dependencies = []
    ports = []
    native_spec = PART_PROFILE_SURFACE_NATIVE_SPECS[operation]
    for requirement in native_spec.source_requirements:
        count = _source_count(operation, requirement.role, requirement.minimum)
        port_id = f"port_{requirement.role.value}"
        ports.append(
            FeatureInputPortV2(
                port_id=port_id,
                semantic_role_term_ref_id=PART_PROFILE_SURFACE_SOURCE_ROLE_TERMS[
                    requirement.role
                ].term_ref_id,
                value_type_term_ref_id=PART_PROFILE_SURFACE_SHAPE_TYPE_TERM.term_ref_id,
                minimum_cardinality=requirement.minimum,
                maximum_cardinality=requirement.maximum,
                ordered=requirement.ordered,
            )
        )
        for ordinal in range(count):
            node_id, result_id = _source_id(requirement.role, ordinal)
            sources.append(
                FeatureNodeV2(
                    node_id=node_id,
                    body_id=body_id,
                    name=f"Authenticated {requirement.role.value} {ordinal}",
                    intent=FeatureIntentV2(
                        structural_kind_term_ref_id=(
                            PART_PROFILE_SURFACE_SOURCE_STRUCTURE_TERM.term_ref_id
                        ),
                        family_term_ref_id=PART_PROFILE_SURFACE_SOURCE_FAMILY_TERM.term_ref_id,
                        operation_term_ref_id=(
                            PART_PROFILE_SURFACE_SOURCE_OPERATION_TERM.term_ref_id
                        ),
                    ),
                    results=(
                        FeatureResultV2(
                            result_id=result_id,
                            semantic_role_term_ref_id=(
                                PART_PROFILE_SURFACE_SOURCE_RESULT_ROLE_TERM.term_ref_id
                            ),
                            value_type_term_ref_id=(
                                PART_PROFILE_SURFACE_SHAPE_TYPE_TERM.term_ref_id
                            ),
                        ),
                    ),
                )
            )
            dependencies.append(
                FeatureDependencyV2(
                    dependency_id=f"dependency_{requirement.role.value}_{ordinal}",
                    port_id=port_id,
                    upstream_node_id=node_id,
                    upstream_result_id=result_id,
                    ordinal=ordinal,
                )
            )
    parameter = DesignParameterV2(
        parameter_id="parameter_operation",
        name="Reviewed profile/surface parameters",
        semantic_role_term_ref_id=PART_PROFILE_SURFACE_PARAMETERS_ROLE_TERM.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id="value_operation",
            value_type_term_ref_id=PART_PROFILE_SURFACE_PARAMETERS_TYPE_TERM.term_ref_id,
            encoding_term_ref_id=PART_PROFILE_SURFACE_CANONICAL_JSON_TERM.term_ref_id,
            value=PARAMETERS[operation] if parameters is None else parameters,
        ),
    )
    ports.append(
        FeatureInputPortV2(
            port_id="port_parameters",
            semantic_role_term_ref_id=PART_PROFILE_SURFACE_PARAMETERS_ROLE_TERM.term_ref_id,
            value_type_term_ref_id=PART_PROFILE_SURFACE_PARAMETERS_TYPE_TERM.term_ref_id,
            minimum_cardinality=1,
            maximum_cardinality=1,
            ordered=False,
        )
    )
    target = FeatureNodeV2(
        node_id="node_target",
        body_id=body_id,
        name=f"Reviewed Part {operation.value}",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=PART_PROFILE_SURFACE_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=PART_PROFILE_SURFACE_FAMILY_TERM.term_ref_id,
            operation_term_ref_id=operation_terms.operation_term.term_ref_id,
            input_ports=tuple(ports),
            dependencies=tuple(dependencies),
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
                semantic_role_term_ref_id=PART_PROFILE_SURFACE_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=PART_PROFILE_SURFACE_SHAPE_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id=f"graph_part_profile_surface_{operation.value}",
        name=f"Part profile/surface {operation.value}",
        terms=tuple(terms),
        bodies=(FeatureBodyV2(body_id=body_id, name="Part document"),),
        parameters=(parameter,),
        references=(),
        nodes=(*sources, target),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection_target",
                node_id=target.node_id,
                result_id="result_target",
            ),
        ),
    )


def _intent_document(graph: ParametricFeatureGraphV2) -> tuple[DocumentRef, bytes]:
    payload = graph.canonical_bytes
    return (
        DocumentRef(
            artifact_id="artifact_part_profile_surface_pfg",
            role_term_ref_id=PART_PROFILE_SURFACE_INTENT_ROLE_TERM.term_ref_id,
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
        artifact_id="artifact_part_profile_surface_pfg",
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
            PART_PROFILE_SURFACE_INTENT_ROLE_TERM,
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_part_profile_surface_target",
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
                producer_id="part_profile_surface_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("part-profile-surface-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("part-profile-surface-upstream-request"),
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
    max_output_bytes: int = MAX_PART_PROFILE_SURFACE_PLAN_BYTES,
) -> tuple[BackendLoweringRequest, _Reader, TrustedRulePolicy]:
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = build_part_profile_surface_capability_document()
    policy = TrustedRulePolicy(evaluators=(_Evaluator(),))
    request = BackendLoweringRequest(
        adapter=FREECAD_PART_PROFILE_SURFACE_ADAPTER_DESCRIPTOR,
        terms=(
            *PART_PROFILE_SURFACE_REQUEST_TERMS,
            RULE,
            PREDICATE,
            PREMISE_ROLE,
            CONCLUSION_ROLE,
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


def test_part_profile_surface_fast_batch_all_six_share_g0_engine() -> None:
    assert len(PART_PROFILE_SURFACE_MANIFEST.operations) == 6
    assert set(PART_PROFILE_SURFACE_NATIVE_SPECS) == set(PartProfileSurfaceOperation)
    capability_document, capability_payload = build_part_profile_surface_capability_document()
    assert capability_document.document_digest == PART_PROFILE_SURFACE_MANIFEST.manifest_sha256
    assert capability_payload == PART_PROFILE_SURFACE_MANIFEST.canonical_bytes

    for operation in PartProfileSurfaceOperation:
        request, reader, policy = _request(_graph(operation))
        sink = _Sink()
        adapter = FreeCADPartProfileSurfaceAdapter(sink)
        result, receipt = _lower(adapter, request, reader, policy)
        decoded, payload = adapter.read_plan(receipt)
        repeated, repeated_receipt = _lower(adapter, request, reader, policy)

        assert isinstance(decoded, PartProfileSurfaceBackendPlan)
        assert decoded.operation is operation
        assert decoded.parameters == PartProfileSurfaceParameterSet.from_value(
            operation,
            PARAMETERS[operation],
        )
        expected_roles = tuple(
            (requirement.role, ordinal)
            for requirement in PART_PROFILE_SURFACE_NATIVE_SPECS[operation].source_requirements
            for ordinal in range(_source_count(operation, requirement.role, requirement.minimum))
        )
        assert tuple((item.role, item.ordinal) for item in decoded.sources) == expected_roles
        assert (
            receipt.operation.native_type_id == PART_PROFILE_SURFACE_NATIVE_SPECS[operation].type_id
        )
        assert result.supported_subjects == (_subject(),)
        assert result.plan_document == receipt.plan_document
        assert adapter.executable is False and adapter.grants_execution_authority is False
        assert receipt.executable is False and receipt.grants_execution_authority is False
        assert decoded.executable is False and decoded.grants_execution_authority is False
        assert payload == decoded.canonical_bytes
        assert b"Part::" not in payload
        assert repeated == result and repeated_receipt == receipt and len(sink.items) == 1


def test_part_profile_surface_invalid_graphs_fail_before_publication() -> None:
    invalid_extrusion = {
        **PARAMETERS[PartProfileSurfaceOperation.EXTRUSION],
        "direction": [0.0, 0.0, 2.0],
    }
    cases = (
        _graph(
            PartProfileSurfaceOperation.EXTRUSION,
            parameters=invalid_extrusion,
        ),
        _graph(
            PartProfileSurfaceOperation.EXTRUSION,
            source_operation_definition="f" * 64,
        ),
        _graph(
            PartProfileSurfaceOperation.REVOLUTION,
            parameters={
                **PARAMETERS[PartProfileSurfaceOperation.REVOLUTION],
                "angle_degrees": 0.0,
            },
        ),
        _graph(PartProfileSurfaceOperation.FACE, parameters={"unexpected": True}),
    )
    for graph in cases:
        request, reader, policy = _request(graph)
        sink = _Sink()
        with pytest.raises(IntentBridgeError) as error:
            _lower(FreeCADPartProfileSurfaceAdapter(sink), request, reader, policy)
        assert error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
        assert sink.items == {}


def test_part_profile_surface_plan_canonical_budget_and_tamper_gates() -> None:
    request, reader, policy = _request(_graph(PartProfileSurfaceOperation.LOFT))
    adapter = FreeCADPartProfileSurfaceAdapter(_Sink())
    _, receipt = _lower(adapter, request, reader, policy)
    _, payload = adapter.read_plan(receipt)
    with pytest.raises(PartProfileSurfaceRuleError):
        decode_part_profile_surface_backend_plan(payload + b" ")

    exact_request, exact_reader, exact_policy = _request(
        _graph(PartProfileSurfaceOperation.LOFT),
        max_output_bytes=len(payload),
    )
    exact_result, _ = _lower(
        FreeCADPartProfileSurfaceAdapter(_Sink()),
        exact_request,
        exact_reader,
        exact_policy,
    )
    assert exact_result.plan_document.size_bytes == len(payload)

    small_request, small_reader, small_policy = _request(
        _graph(PartProfileSurfaceOperation.LOFT),
        max_output_bytes=len(payload) - 1,
    )
    sink = _Sink()
    with pytest.raises(IntentBridgeError) as error:
        _lower(
            FreeCADPartProfileSurfaceAdapter(sink),
            small_request,
            small_reader,
            small_policy,
        )
    assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert sink.items == {}


@pytest.mark.slow
def test_real_freecad_part_profile_surface_batch(
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

    cases = []
    for operation in PartProfileSurfaceOperation:
        request, reader, policy = _request(_graph(operation))
        adapter = FreeCADPartProfileSurfaceAdapter(_Sink())
        result, receipt = _lower(adapter, request, reader, policy)
        plan, payload = adapter.read_plan(receipt)
        plan_path = tmp_path / f"part-profile-surface-{operation.value}.json"
        plan_path.write_bytes(payload)
        cases.append(
            {
                "operation": operation.value,
                "type_id": PART_PROFILE_SURFACE_NATIVE_SPECS[operation].type_id,
                "path": str(plan_path),
                "content_sha256": result.plan_document.content_sha256,
                "plan_sha256": result.plan_document.document_digest,
                "body_id": plan.body_id,
                "sources": [item.to_mapping() for item in plan.sources],
                "adapter_contract_sha256": receipt.adapter.adapter_contract_sha256,
                "manifest_sha256": receipt.manifest_sha256,
                "operation_specification_sha256": receipt.operation.specification_sha256,
            }
        )

    model_path = tmp_path / "part-profile-surfaces.FCStd"
    source_root = Path(__file__).parents[1] / "src"
    code = f"""
import math, os, sys
sys.path.insert(0, os.path.join(sys.prefix, 'lib'))
sys.path.insert(0, {str(source_root)!r})
from pathlib import Path
import FreeCAD, Part
from vibecad.parametric.freecad_part_profile_surface_rules import (
    AuthenticatedPartProfileSurfaceObject,
    PartProfileSurfaceExecutionBindings,
    PartProfileSurfaceRuleError,
    apply_part_profile_surface_plan,
)

CASES = {cases!r}

def rectangle_xy(x0, y0, z, width, height):
    return Part.makePolygon([
        FreeCAD.Vector(x0, y0, z),
        FreeCAD.Vector(x0 + width, y0, z),
        FreeCAD.Vector(x0 + width, y0 + height, z),
        FreeCAD.Vector(x0, y0 + height, z),
        FreeCAD.Vector(x0, y0, z),
    ])

def rectangle_xz(x0, z0, width, height):
    return Part.makePolygon([
        FreeCAD.Vector(x0, 0, z0),
        FreeCAD.Vector(x0 + width, 0, z0),
        FreeCAD.Vector(x0 + width, 0, z0 + height),
        FreeCAD.Vector(x0, 0, z0 + height),
        FreeCAD.Vector(x0, 0, z0),
    ])

def circle_wire(radius, z=0.0):
    return Part.Wire([Part.makeCircle(
        radius,
        FreeCAD.Vector(0, 0, z),
        FreeCAD.Vector(0, 0, 1),
    )])

def make_shape(operation, role, ordinal, edited=False):
    delta = 2.0 if edited else 0.0
    if role == 'profile':
        if operation == 'revolution':
            return rectangle_xz(2.0, 0.0, 2.0 + delta, 5.0)
        if operation == 'sweep':
            return circle_wire(2.0 + ordinal + delta, 12.0 * ordinal)
        size = 4.0 + 2.0 * ordinal + delta
        return rectangle_xy(-size / 2.0, -size / 2.0, 10.0 * ordinal, size, size)
    if role == 'spine':
        return Part.makeLine(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 12.0 + delta))
    if role == 'curve':
        y = 5.0 * ordinal + delta * ordinal
        z = 3.0 * ordinal + delta * ordinal
        return Part.makeLine(FreeCAD.Vector(0, y, z), FreeCAD.Vector(10, y, z))
    return rectangle_xy(0, 0, 0, 7.0 + delta, 5.0)

def signature(shape):
    box = shape.BoundBox
    return (
        shape.ShapeType,
        len(shape.Vertexes), len(shape.Edges), len(shape.Faces), len(shape.Solids),
        float(shape.Length), float(shape.Area), float(shape.Volume),
        float(box.XLength), float(box.YLength), float(box.ZLength),
    )

def different(left, right):
    if left[:5] != right[:5]:
        return True
    return any(
        not math.isclose(a, b, rel_tol=0.0, abs_tol=1e-8)
        for a, b in zip(left[5:], right[5:], strict=True)
    )

def bindings(document, entry, source_objects, *, adapter_digest=None):
    return PartProfileSurfaceExecutionBindings(
        document=document,
        body_id=entry['body_id'],
        sources=tuple(
            AuthenticatedPartProfileSurfaceObject(
                object=item,
                node_id=selection['node_id'],
                result_id=selection['result_id'],
            )
            for item, selection in zip(source_objects, entry['sources'], strict=True)
        ),
        expected_adapter_contract_sha256=(
            entry['adapter_contract_sha256'] if adapter_digest is None else adapter_digest
        ),
        expected_manifest_sha256=entry['manifest_sha256'],
        expected_operation_specification_sha256=entry['operation_specification_sha256'],
    )

document = FreeCAD.newDocument('PartProfileSurfaceBatch')
document.UndoMode = 1
persisted = []
sources_by_operation = {{}}
for entry in CASES:
    operation = entry['operation']
    source_objects = []
    for selection in entry['sources']:
        name = f"Source_{{operation}}_{{selection['role']}}_{{selection['ordinal']}}"
        item = document.addObject('Part::Feature', name)
        item.Shape = make_shape(operation, selection['role'], selection['ordinal'])
        source_objects.append(item)
    document.recompute()
    payload = Path(entry['path']).read_bytes()
    before = tuple(document.Objects)
    receipt = apply_part_profile_surface_plan(
        payload,
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=bindings(document, entry, source_objects),
    )
    result = document.getObject(receipt.object_name)
    assert result.TypeId == entry['type_id']
    assert result.isValid() and tuple(result.State) == ('Up-to-date',)
    assert not result.Shape.isNull() and result.Shape.isValid()
    assert tuple(document.Objects) == (*before, result)
    initial = signature(result.Shape)
    if operation == 'extrusion':
        result.LengthFwd = 11.0
    elif operation == 'revolution':
        result.Angle = 180.0
    else:
        if operation == 'sweep':
            edit_index = next(
                index
                for index, selection in enumerate(entry['sources'])
                if selection['role'] == 'spine'
            )
        else:
            edit_index = 1 if operation in {{'loft', 'ruled_surface'}} else 0
        selection = entry['sources'][edit_index]
        source_objects[edit_index].Shape = make_shape(
            operation,
            selection['role'],
            selection['ordinal'],
            edited=True,
        )
    document.recompute()
    edited = signature(result.Shape)
    assert different(initial, edited)
    persisted.append((result.Name, entry['type_id'], edited, len(source_objects)))
    sources_by_operation[operation] = source_objects

# Tampered plan and wrong reviewed binding fail before mutation.
entry = CASES[0]
payload = Path(entry['path']).read_bytes()
before_rejection = tuple(document.Objects)
for rejected_payload, adapter_digest in ((payload + b' ', None), (payload, 'f' * 64)):
    try:
        apply_part_profile_surface_plan(
            rejected_payload,
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings(
                document,
                entry,
                sources_by_operation[entry['operation']],
                adapter_digest=adapter_digest,
            ),
        )
    except PartProfileSurfaceRuleError:
        pass
    else:
        raise AssertionError('pre-mutation rejection must fail')
    assert tuple(document.Objects) == before_rejection

# An open extrusion profile is rejected before the native result is created.
extrusion_entry = next(item for item in CASES if item['operation'] == 'extrusion')
precondition_document = FreeCAD.newDocument('PartProfileSurfacePrecondition')
precondition_document.UndoMode = 1
open_profile = precondition_document.addObject('Part::Feature', 'OpenProfile')
open_profile.Shape = Part.makeLine(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(4, 0, 0))
precondition_document.recompute()
before_precondition = tuple(precondition_document.Objects)
try:
    apply_part_profile_surface_plan(
        Path(extrusion_entry['path']).read_bytes(),
        expected_content_sha256=extrusion_entry['content_sha256'],
        expected_plan_sha256=extrusion_entry['plan_sha256'],
        bindings=bindings(precondition_document, extrusion_entry, [open_profile]),
    )
except PartProfileSurfaceRuleError:
    pass
else:
    raise AssertionError('open profile must fail before native creation')
assert tuple(precondition_document.Objects) == before_precondition
assert all(item.TypeId != 'Part::Extrusion' for item in precondition_document.Objects)
assert not precondition_document.HasPendingTransaction
FreeCAD.closeDocument(precondition_document.Name)

# A native Loft failure occurs after object creation and must leave zero residue.
loft_entry = next(item for item in CASES if item['operation'] == 'loft')
failure_document = FreeCAD.newDocument('PartProfileSurfaceRollback')
failure_document.UndoMode = 1
failure_sources = []
identical = rectangle_xy(-2, -2, 0, 4, 4)
for index, selection in enumerate(loft_entry['sources']):
    item = failure_document.addObject('Part::Feature', f'FailureSource{{index}}')
    item.Shape = identical
    failure_sources.append(item)
failure_document.recompute()
before_failure = tuple(failure_document.Objects)
before_visibility = tuple(bool(item.Visibility) for item in before_failure)
try:
    apply_part_profile_surface_plan(
        Path(loft_entry['path']).read_bytes(),
        expected_content_sha256=loft_entry['content_sha256'],
        expected_plan_sha256=loft_entry['plan_sha256'],
        bindings=bindings(failure_document, loft_entry, failure_sources),
    )
except PartProfileSurfaceRuleError:
    pass
else:
    raise AssertionError('degenerate Loft must fail after native creation')
assert tuple(failure_document.Objects) == before_failure
assert tuple(bool(item.Visibility) for item in failure_document.Objects) == before_visibility
assert all(item.TypeId != 'Part::Loft' for item in failure_document.Objects)
assert not failure_document.HasPendingTransaction
FreeCAD.closeDocument(failure_document.Name)

document.saveAs({str(model_path)!r})
FreeCAD.closeDocument(document.Name)
reopened = FreeCAD.openDocument({str(model_path)!r})
reopened.recompute()
for name, type_id, expected, expected_source_count in persisted:
    result = reopened.getObject(name)
    assert result.TypeId == type_id
    assert result.isValid() and tuple(result.State) == ('Up-to-date',)
    assert len(result.OutList) == expected_source_count
    actual = signature(result.Shape)
    assert actual[:5] == expected[:5]
    assert all(
        math.isclose(a, b, rel_tol=0.0, abs_tol=1e-6)
        for a, b in zip(actual[5:], expected[5:], strict=True)
    )
FreeCAD.closeDocument(reopened.Name)
print('REAL_PART_PROFILE_SURFACE_BATCH_OK')
"""
    completed = subprocess.run(
        [str(runtime_python), "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    assert "REAL_PART_PROFILE_SURFACE_BATCH_OK" in completed.stdout
