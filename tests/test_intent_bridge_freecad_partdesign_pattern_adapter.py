"""Focused tests for the three-operation PartDesign pattern batch."""

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
from vibecad.intent_bridge.freecad_partdesign_pattern_adapter import (
    FREECAD_PARTDESIGN_PATTERN_ADAPTER_DESCRIPTOR,
    PATTERN_ANGLE_ROLE_TERM,
    PATTERN_ANGLE_TYPE_TERM,
    PATTERN_AXIS_ROLE_TERM,
    PATTERN_BASE_ROLE_TERM,
    PATTERN_BOOLEAN_TYPE_TERM,
    PATTERN_CANONICAL_JSON_TERM,
    PATTERN_DIRECTION_ROLE_TERM,
    PATTERN_INTEGER_TYPE_TERM,
    PATTERN_INTENT_DOCUMENT_ROLE_TERM,
    PATTERN_LENGTH_TYPE_TERM,
    PATTERN_OCCURRENCES_ROLE_TERM,
    PATTERN_OPERATION_TERMS,
    PATTERN_ORIGIN_AXIS_TYPE_TERM,
    PATTERN_ORIGIN_PLANE_TYPE_TERM,
    PATTERN_PFG_TERMS,
    PATTERN_PLANE_ROLE_TERM,
    PATTERN_REQUEST_TERMS,
    PATTERN_REVERSED_ROLE_TERM,
    PATTERN_SOLID_RESULT_ROLE_TERM,
    PATTERN_SOLID_TYPE_TERM,
    PATTERN_SOURCE_ROLE_TERM,
    PATTERN_SPAN_ROLE_TERM,
    PATTERN_STRUCTURE_TERM,
    PATTERN_X_AXIS_LOCATOR_TERM,
    PATTERN_YZ_PLANE_LOCATOR_TERM,
    PATTERN_Z_AXIS_LOCATOR_TERM,
    FreeCADPartDesignPatternAdapter,
    build_pattern_capability_document,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    ParametricFeatureGraphV2Codec,
)
from vibecad.intent_bridge.ports import IntentBackendAdapter, TrustedCodecRegistry
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
    FeatureReferenceBindingV2,
    FeatureResultV2,
    ParametricFeatureGraphV2,
    SemanticReferenceScope,
    SemanticReferenceV2,
    SemanticTermRefV2,
    TermTypedValueV2,
)
from vibecad.parametric.freecad_partdesign_pattern_rules import (
    MAX_PARTDESIGN_PATTERN_OCCURRENCES,
    MAX_PARTDESIGN_PATTERN_PLAN_BYTES,
    PartDesignPatternOperation,
    PartDesignPatternRuleError,
    PatternOriginAxis,
    PatternOriginPlane,
    decode_partdesign_pattern_backend_plan,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pfg_term(term_ref_id: str, term_id: str) -> SemanticTermRefV2:
    return SemanticTermRefV2(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.pattern-test-source",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"pfg:{term_id}"),
    )


SOURCE_TERMS = (
    _pfg_term("source_structure", "structure.test-source"),
    _pfg_term("source_family", "family.test-source"),
    _pfg_term("source_operation", "operation.test-source"),
)


def _port(
    port_id: str, role: SemanticTermRefV2, value_type: SemanticTermRefV2
) -> FeatureInputPortV2:
    return FeatureInputPortV2(
        port_id=port_id,
        semantic_role_term_ref_id=role.term_ref_id,
        value_type_term_ref_id=value_type.term_ref_id,
        minimum_cardinality=1,
        maximum_cardinality=1,
        ordered=False,
    )


def _parameter(
    kind: str,
    role: SemanticTermRefV2,
    value_type: SemanticTermRefV2,
    value: object,
) -> DesignParameterV2:
    return DesignParameterV2(
        parameter_id=f"parameter_{kind}",
        name=kind.title(),
        semantic_role_term_ref_id=role.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id=f"value_{kind}",
            value_type_term_ref_id=value_type.term_ref_id,
            encoding_term_ref_id=PATTERN_CANONICAL_JSON_TERM.term_ref_id,
            value=value,
        ),
    )


def _graph(
    operation: PartDesignPatternOperation,
    *,
    occurrences: int = 3,
    operation_definition: str | None = None,
    locator_definition: str | None = None,
) -> ParametricFeatureGraphV2:
    operation_terms = next(item for item in PATTERN_OPERATION_TERMS if item.operation is operation)
    static_terms = list(PATTERN_PFG_TERMS)
    if operation_definition is not None:
        index = static_terms.index(operation_terms.operation_term)
        static_terms[index] = dataclasses.replace(
            operation_terms.operation_term,
            term_definition_sha256=operation_definition,
        )
    if operation is PartDesignPatternOperation.LINEAR_PATTERN:
        locator = PATTERN_X_AXIS_LOCATOR_TERM
        reference_role = PATTERN_DIRECTION_ROLE_TERM
        reference_type = PATTERN_ORIGIN_AXIS_TYPE_TERM
    elif operation is PartDesignPatternOperation.POLAR_PATTERN:
        locator = PATTERN_Z_AXIS_LOCATOR_TERM
        reference_role = PATTERN_AXIS_ROLE_TERM
        reference_type = PATTERN_ORIGIN_AXIS_TYPE_TERM
    else:
        locator = PATTERN_YZ_PLANE_LOCATOR_TERM
        reference_role = PATTERN_PLANE_ROLE_TERM
        reference_type = PATTERN_ORIGIN_PLANE_TYPE_TERM
    if locator_definition is not None:
        index = static_terms.index(locator)
        static_terms[index] = dataclasses.replace(
            locator,
            term_definition_sha256=locator_definition,
        )

    source = FeatureNodeV2(
        node_id="node_source",
        body_id="body_main",
        name="Untrusted PartDesign::LinearPattern source",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=SOURCE_TERMS[0].term_ref_id,
            family_term_ref_id=SOURCE_TERMS[1].term_ref_id,
            operation_term_ref_id=SOURCE_TERMS[2].term_ref_id,
        ),
        results=(
            FeatureResultV2(
                result_id="result_source",
                semantic_role_term_ref_id=PATTERN_SOLID_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=PATTERN_SOLID_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    ports = [
        _port("port_base", PATTERN_BASE_ROLE_TERM, PATTERN_SOLID_TYPE_TERM),
        _port("port_source", PATTERN_SOURCE_ROLE_TERM, PATTERN_SOLID_TYPE_TERM),
        _port("port_reference", reference_role, reference_type),
    ]
    parameters: list[DesignParameterV2] = []
    parameter_bindings: list[FeatureParameterBindingV2] = []
    if operation is PartDesignPatternOperation.LINEAR_PATTERN:
        specifications = (
            ("occurrences", PATTERN_OCCURRENCES_ROLE_TERM, PATTERN_INTEGER_TYPE_TERM, occurrences),
            ("span", PATTERN_SPAN_ROLE_TERM, PATTERN_LENGTH_TYPE_TERM, 30.0),
            ("reversed", PATTERN_REVERSED_ROLE_TERM, PATTERN_BOOLEAN_TYPE_TERM, False),
        )
    elif operation is PartDesignPatternOperation.POLAR_PATTERN:
        specifications = (
            ("occurrences", PATTERN_OCCURRENCES_ROLE_TERM, PATTERN_INTEGER_TYPE_TERM, occurrences),
            ("angle", PATTERN_ANGLE_ROLE_TERM, PATTERN_ANGLE_TYPE_TERM, 180.0),
            ("reversed", PATTERN_REVERSED_ROLE_TERM, PATTERN_BOOLEAN_TYPE_TERM, False),
        )
    else:
        specifications = ()
    for kind, role, value_type, value in specifications:
        ports.append(_port(f"port_{kind}", role, value_type))
        parameters.append(_parameter(kind, role, value_type, value))
        parameter_bindings.append(
            FeatureParameterBindingV2(
                binding_id=f"binding_{kind}",
                port_id=f"port_{kind}",
                parameter_id=f"parameter_{kind}",
            )
        )
    reference = SemanticReferenceV2(
        reference_id="reference_origin",
        scope=SemanticReferenceScope.ORIGIN,
        semantic_role_term_ref_id=reference_role.term_ref_id,
        value_type_term_ref_id=reference_type.term_ref_id,
        locator_term_ref_id=locator.term_ref_id,
    )
    target = FeatureNodeV2(
        node_id="node_target",
        body_id="body_main",
        name="Untrusted graph TypeId=PartDesign::Mirrored property=MirrorPlane",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=PATTERN_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=operation_terms.family_term.term_ref_id,
            operation_term_ref_id=operation_terms.operation_term.term_ref_id,
            input_ports=tuple(ports),
            dependencies=(
                FeatureDependencyV2(
                    dependency_id="dependency_base",
                    port_id="port_base",
                    upstream_node_id=source.node_id,
                    upstream_result_id="result_source",
                ),
                FeatureDependencyV2(
                    dependency_id="dependency_source",
                    port_id="port_source",
                    upstream_node_id=source.node_id,
                    upstream_result_id="result_source",
                ),
            ),
            references=(
                FeatureReferenceBindingV2(
                    binding_id="binding_reference",
                    port_id="port_reference",
                    reference_id=reference.reference_id,
                ),
            ),
            parameter_bindings=tuple(parameter_bindings),
        ),
        results=(
            FeatureResultV2(
                result_id="result_target",
                semantic_role_term_ref_id=PATTERN_SOLID_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=PATTERN_SOLID_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id="graph_pattern",
        name="Pattern graph",
        terms=tuple((*static_terms, *SOURCE_TERMS)),
        bodies=(FeatureBodyV2(body_id="body_main", name="Main Body"),),
        parameters=tuple(parameters),
        references=(reference,),
        nodes=(source, target),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection_result",
                node_id=target.node_id,
                result_id="result_target",
            ),
        ),
    )


def _bridge_from_pfg(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


def _proof_term(term_ref_id: str, term_id: str) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.pattern-proof-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"proof:{term_id}"),
    )


RULE = _proof_term("rule_pattern_target", "rule.pattern-target-reviewed")
PREDICATE = _proof_term("predicate_pattern_target", "predicate.pattern-target-reviewed")
ROLE_PREMISE = _proof_term("role_pattern_candidate", "proof-role.pattern-candidate")
ROLE_CONCLUSION = _proof_term("role_pattern_validated", "proof-role.pattern-validated")
PATTERN_STRUCTURE_BRIDGE = _bridge_from_pfg(PATTERN_STRUCTURE_TERM)


class _PatternEvaluator:
    def __init__(self) -> None:
        def signature(role: BridgeTermRef) -> RuleEndpointSignature:
            return RuleEndpointSignature(
                selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
                role_term=role,
                subject_type_term=PATTERN_STRUCTURE_BRIDGE,
            )

        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="partdesign_pattern_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("partdesign-pattern-target-evaluator-v1"),
            rule_term=RULE,
            predicate_term=PREDICATE,
            premises=(signature(ROLE_PREMISE),),
            conclusions=(signature(ROLE_CONCLUSION),),
        )

    @property
    def descriptor(self) -> TrustedRuleEvaluatorDescriptor:
        return self._descriptor

    def validate(self, evaluation: TrustedRuleEvaluation) -> None:
        if (
            len(evaluation.documents) != 1
            or evaluation.premises[0].subject.selector_id != "node_target"
            or evaluation.conclusions[0].subject.selector_id != "node_target"
        ):
            raise IntentBridgeError(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/target")


def _intent_document(graph: ParametricFeatureGraphV2) -> tuple[DocumentRef, bytes]:
    payload = graph.canonical_bytes
    return (
        DocumentRef(
            artifact_id="artifact_pfg",
            role_term_ref_id=PATTERN_INTENT_DOCUMENT_ROLE_TERM.term_ref_id,
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
        artifact_id="artifact_pfg",
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id="node_target",
    )


def _proof(policy: TrustedRulePolicy, document: DocumentRef) -> ProofBundle:
    return ProofBundle(
        terms=(
            RULE,
            PREDICATE,
            ROLE_PREMISE,
            ROLE_CONCLUSION,
            PATTERN_STRUCTURE_BRIDGE,
            PATTERN_INTENT_DOCUMENT_ROLE_TERM,
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_pattern_target",
                predicate_term_ref_id=PREDICATE.term_ref_id,
                rule_term_ref_id=RULE.term_ref_id,
                premises=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=ROLE_PREMISE.term_ref_id,
                        subject=_subject(),
                    ),
                ),
                conclusions=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=ROLE_CONCLUSION.term_ref_id,
                        subject=_subject(),
                    ),
                ),
            ),
        ),
        producer=ProducerBinding(
            descriptor=ProducerDescriptor(
                producer_id="pattern_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("pattern-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("upstream-pattern-compile-request"),
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


class _MemoryPlanSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.items: dict[str, tuple[DocumentRef, bytes]] = {}

    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        if self.fail:
            raise RuntimeError("untrusted detail")
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
    max_output_bytes: int = MAX_PARTDESIGN_PATTERN_PLAN_BYTES,
) -> tuple[BackendLoweringRequest, _Reader, TrustedRulePolicy]:
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = build_pattern_capability_document()
    policy = TrustedRulePolicy(evaluators=(_PatternEvaluator(),))
    request = BackendLoweringRequest(
        adapter=FREECAD_PARTDESIGN_PATTERN_ADAPTER_DESCRIPTOR,
        terms=tuple(
            (
                *PATTERN_REQUEST_TERMS,
                RULE,
                PREDICATE,
                ROLE_PREMISE,
                ROLE_CONCLUSION,
                PFG_SELECTOR_FEATURE_NODE,
            )
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


def _lower(
    adapter: FreeCADPartDesignPatternAdapter,
    request: BackendLoweringRequest,
    reader: _Reader,
    policy: TrustedRulePolicy,
):
    return adapter.lower_with_receipt(
        request,
        artifacts=reader,
        codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
        proof_policy=policy,
    )


@pytest.mark.parametrize("operation", tuple(PartDesignPatternOperation))
def test_shared_adapter_lowers_all_three_operations_without_native_string_authority(
    operation: PartDesignPatternOperation,
) -> None:
    request, reader, policy = _request(_graph(operation))
    sink = _MemoryPlanSink()
    adapter = FreeCADPartDesignPatternAdapter(sink)
    assert isinstance(adapter, IntentBackendAdapter)
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    repeated_result, repeated_receipt = _lower(adapter, request, reader, policy)
    repeated_plan, repeated_payload = adapter.read_plan(repeated_receipt)

    assert plan.operation is operation
    assert plan.base == plan.source_feature
    assert plan.reference_id == "reference_origin"
    assert result.plan_document.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert result.plan_document.document_digest == plan.plan_sha256
    assert repeated_result == result
    assert repeated_receipt == receipt
    assert repeated_plan == plan and repeated_payload == payload
    assert len(sink.items) == 1
    assert result.supported_subjects == (_subject(),)
    assert not plan.executable and not adapter.executable
    assert not receipt.grants_execution_authority and not adapter.grants_execution_authority
    assert b"PartDesign::" not in payload
    if operation is PartDesignPatternOperation.LINEAR_PATTERN:
        assert plan.axis is PatternOriginAxis.X and plan.span_mm == 30.0
    elif operation is PartDesignPatternOperation.POLAR_PATTERN:
        assert plan.axis is PatternOriginAxis.Z and plan.angle_degrees == 180.0
    else:
        assert plan.plane is PatternOriginPlane.YZ and plan.occurrences is None


@pytest.mark.parametrize("field", ("operation", "locator"))
def test_adapter_rejects_semantic_identity_substitution(field: str) -> None:
    graph = _graph(
        PartDesignPatternOperation.LINEAR_PATTERN,
        operation_definition=_sha("substituted operation") if field == "operation" else None,
        locator_definition=_sha("substituted locator") if field == "locator" else None,
    )
    request, reader, policy = _request(graph)
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartDesignPatternAdapter(_MemoryPlanSink()), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION


def test_occurrence_n_n_plus_one_and_atomic_sink_failure() -> None:
    request, reader, policy = _request(
        _graph(
            PartDesignPatternOperation.LINEAR_PATTERN,
            occurrences=MAX_PARTDESIGN_PATTERN_OCCURRENCES,
        )
    )
    adapter = FreeCADPartDesignPatternAdapter(_MemoryPlanSink())
    _lower(adapter, request, reader, policy)

    request, reader, policy = _request(
        _graph(
            PartDesignPatternOperation.LINEAR_PATTERN,
            occurrences=MAX_PARTDESIGN_PATTERN_OCCURRENCES + 1,
        )
    )
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartDesignPatternAdapter(_MemoryPlanSink()), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION

    request, reader, policy = _request(_graph(PartDesignPatternOperation.MIRRORED))
    sink = _MemoryPlanSink(fail=True)
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartDesignPatternAdapter(sink), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert sink.items == {}


def test_plan_decoder_rejects_tamper_duplicate_keys_and_budget() -> None:
    request, reader, policy = _request(_graph(PartDesignPatternOperation.POLAR_PATTERN))
    adapter = FreeCADPartDesignPatternAdapter(_MemoryPlanSink())
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    with pytest.raises(PartDesignPatternRuleError):
        decode_partdesign_pattern_backend_plan(
            payload + b" ",
            expected_content_sha256=result.plan_document.content_sha256,
            expected_plan_sha256=plan.plan_sha256,
        )
    duplicate = payload.replace(b'{"operation":', b'{"operation":{},"operation":', 1)
    with pytest.raises(PartDesignPatternRuleError):
        decode_partdesign_pattern_backend_plan(duplicate)

    request, reader, policy = _request(
        _graph(PartDesignPatternOperation.POLAR_PATTERN),
        max_output_bytes=len(payload) - 1,
    )
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartDesignPatternAdapter(_MemoryPlanSink()), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED


@pytest.mark.slow
def test_real_freecad_batch_create_edit_save_reopen_and_invalid_rollback(
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
    for operation in PartDesignPatternOperation:
        request, reader, policy = _request(_graph(operation))
        adapter = FreeCADPartDesignPatternAdapter(_MemoryPlanSink())
        result, receipt = _lower(adapter, request, reader, policy)
        plan, payload = adapter.read_plan(receipt)
        plan_path = tmp_path / f"{operation.value}.json"
        plan_path.write_bytes(payload)
        cases.append(
            {
                "operation": operation.value,
                "path": str(plan_path),
                "content_sha256": result.plan_document.content_sha256,
                "plan_sha256": result.plan_document.document_digest,
                "body_id": plan.body_id,
                "base": (plan.base.node_id, plan.base.result_id),
                "source": (plan.source_feature.node_id, plan.source_feature.result_id),
            }
        )
    source_root = Path(__file__).parents[1] / "src"
    output_root = tmp_path / "freecad-patterns"
    output_root.mkdir()
    code = f"""
import os, sys
sys.path.insert(0, os.path.join(sys.prefix, 'lib'))
sys.path.insert(0, {str(source_root)!r})
from pathlib import Path
import FreeCAD, Part, Sketcher
from vibecad.parametric.freecad_partdesign_pattern_rules import (
    AuthenticatedPatternObject,
    PartDesignPatternExecutionBindings,
    PartDesignPatternRuleError,
    apply_partdesign_pattern_plan,
)

CASES = {cases!r}
OUTPUT_ROOT = Path({str(output_root)!r})
TYPE_IDS = {{
    'linear_pattern': 'PartDesign::LinearPattern',
    'polar_pattern': 'PartDesign::PolarPattern',
    'mirrored': 'PartDesign::Mirrored',
}}

def add_rectangle(sketch, width=60.0, height=40.0):
    x0, x1 = -width / 2.0, width / 2.0
    y0, y1 = -height / 2.0, height / 2.0
    points = (
        FreeCAD.Vector(x0, y0, 0), FreeCAD.Vector(x1, y0, 0),
        FreeCAD.Vector(x1, y1, 0), FreeCAD.Vector(x0, y1, 0),
    )
    for index, start in enumerate(points):
        sketch.addGeometry(Part.LineSegment(start, points[(index + 1) % 4]), False)
    for index in range(4):
        sketch.addConstraint(Sketcher.Constraint('Coincident', index, 2, (index + 1) % 4, 1))

def make_source(document, operation, *, centered=False):
    # Force target-origin native names to carry suffixes.  The trusted rule
    # must resolve by the authenticated Body's origin Role, never by a global
    # document object name such as "X_Axis".
    document.addObject('PartDesign::Body', 'Decoy')
    body = document.addObject('PartDesign::Body', 'Body')
    outer = body.newObject('Sketcher::SketchObject', 'Outer')
    add_rectangle(outer)
    document.recompute()
    pad = body.newObject('PartDesign::Pad', 'Pad')
    pad.Profile = outer
    pad.Type = 'Length'
    pad.Length = 8.0
    pad.Refine = True
    document.recompute()
    hole = body.newObject('Sketcher::SketchObject', 'Hole')
    if centered:
        x, y = 0.0, 0.0
    elif operation == 'linear_pattern':
        x, y = -15.0, 0.0
    elif operation == 'polar_pattern':
        x, y = 15.0, 0.0
    else:
        x, y = 15.0, 6.0
    geometry = hole.addGeometry(
        Part.Circle(FreeCAD.Vector(x, y, 0), FreeCAD.Vector(0, 0, 1), 3.0), False)
    radius_constraint = hole.addConstraint(Sketcher.Constraint('Radius', geometry, 3.0))
    document.recompute()
    pocket = body.newObject('PartDesign::Pocket', 'Pocket')
    pocket.Profile = hole
    pocket.Type = 'ThroughAll'
    pocket.SideType = 'One side'
    pocket.AlongSketchNormal = True
    pocket.UseCustomVector = False
    pocket.Reversed = True
    pocket.Refine = True
    document.recompute()
    assert body.Tip is pocket and pocket.isValid() and len(pocket.Shape.Solids) == 1
    return body, hole, radius_constraint, pocket

persisted = []
for entry in CASES:
    document = FreeCAD.newDocument('Pattern_' + entry['operation'])
    document.UndoMode = 1
    body, hole, radius_constraint, source = make_source(document, entry['operation'])
    authentication = AuthenticatedPatternObject(
        object=source, node_id=entry['source'][0], result_id=entry['source'][1])
    bindings = PartDesignPatternExecutionBindings(
        document=document,
        body=body,
        body_id=entry['body_id'],
        base=AuthenticatedPatternObject(
            object=source, node_id=entry['base'][0], result_id=entry['base'][1]),
        source_feature=authentication,
    )
    payload = Path(entry['path']).read_bytes()
    receipt = apply_partdesign_pattern_plan(
        payload,
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=bindings,
    )
    feature = document.getObject(receipt.object_name)
    assert feature.TypeId == TYPE_IDS[entry['operation']]
    assert feature is body.Tip and feature.BaseFeature is source
    assert tuple(feature.Originals) == (source,)
    before_edit = float(feature.Shape.Volume)
    if entry['operation'] in {{'linear_pattern', 'polar_pattern'}}:
        feature.Occurrences = 4
    else:
        hole.setDatum(radius_constraint, FreeCAD.Units.Quantity('4 mm'))
    document.recompute()
    after_edit = float(feature.Shape.Volume)
    assert feature.isValid() and len(feature.Shape.Solids) == 1
    assert abs(after_edit - before_edit) > 1e-7
    path = OUTPUT_ROOT / (entry['operation'] + '.FCStd')
    document.saveAs(str(path))
    persisted.append((entry, str(path), feature.Name, after_edit))
    FreeCAD.closeDocument(document.Name)

for entry, path, feature_name, volume in persisted:
    reopened = FreeCAD.openDocument(path)
    reopened.recompute()
    feature = reopened.getObject(feature_name)
    assert feature.TypeId == TYPE_IDS[entry['operation']]
    assert feature.isValid() and len(feature.Shape.Solids) == 1
    assert tuple(feature.Originals)[0].TypeId == 'PartDesign::Pocket'
    assert abs(float(feature.Shape.Volume) - volume) < 1e-6
    FreeCAD.closeDocument(reopened.Name)

# Exact-byte failures are pre-mutation.
entry = CASES[0]
document = FreeCAD.newDocument('PatternTamper')
document.UndoMode = 1
body, _hole, _radius, source = make_source(document, entry['operation'])
bindings = PartDesignPatternExecutionBindings(
    document=document,
    body=body,
    body_id=entry['body_id'],
    base=AuthenticatedPatternObject(
        object=source, node_id=entry['base'][0], result_id=entry['base'][1]),
    source_feature=AuthenticatedPatternObject(
        object=source, node_id=entry['source'][0], result_id=entry['source'][1]),
)
payload = Path(entry['path']).read_bytes()
before = tuple(document.Objects)
try:
    apply_partdesign_pattern_plan(
        payload + b' ',
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=bindings,
    )
except PartDesignPatternRuleError:
    pass
else:
    raise AssertionError('tampered plan must fail')
assert tuple(document.Objects) == before and not document.HasPendingTransaction
FreeCAD.closeDocument(document.Name)

# A centered pocket mirrored across YZ is a native no-op.  The native feature
# is created, conformance rejects it, and the real FreeCAD transaction must
# restore objects, group, tip, visibility, and pending state exactly.
entry = next(item for item in CASES if item['operation'] == 'mirrored')
rollback = FreeCAD.newDocument('PatternRollback')
rollback.UndoMode = 1
body, _hole, _radius, source = make_source(rollback, entry['operation'], centered=True)
bindings = PartDesignPatternExecutionBindings(
    document=rollback,
    body=body,
    body_id=entry['body_id'],
    base=AuthenticatedPatternObject(
        object=source, node_id=entry['base'][0], result_id=entry['base'][1]),
    source_feature=AuthenticatedPatternObject(
        object=source, node_id=entry['source'][0], result_id=entry['source'][1]),
)
payload = Path(entry['path']).read_bytes()
before_objects = tuple(rollback.Objects)
before_group = tuple(body.Group)
before_tip = body.Tip
before_visibility = tuple(bool(item.Visibility) for item in before_group)
try:
    apply_partdesign_pattern_plan(
        payload,
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=bindings,
    )
except PartDesignPatternRuleError:
    pass
else:
    raise AssertionError('native no-op must fail')
assert tuple(rollback.Objects) == before_objects
assert tuple(body.Group) == before_group and body.Tip is before_tip
assert tuple(bool(item.Visibility) for item in before_group) == before_visibility
assert not rollback.HasPendingTransaction
FreeCAD.closeDocument(rollback.Name)
print('REAL_PARTDESIGN_PATTERN_BATCH_OK')
"""
    completed = subprocess.run(
        [str(runtime_python), "-c", code],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    assert "REAL_PARTDESIGN_PATTERN_BATCH_OK" in completed.stdout
