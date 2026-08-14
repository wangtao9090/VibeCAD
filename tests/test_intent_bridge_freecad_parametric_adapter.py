"""Focused private lowering and real FreeCAD conformance tests for Groove."""

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
    BridgeDisposition,
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
from vibecad.intent_bridge.freecad_parametric_adapter import (
    FREECAD_GROOVE_ADAPTER_DESCRIPTOR,
    GROOVE_ANGLE_ROLE_TERM,
    GROOVE_ANGLE_TYPE_TERM,
    GROOVE_AXIS_RESULT_ROLE_TERM,
    GROOVE_AXIS_ROLE_TERM,
    GROOVE_BASE_ROLE_TERM,
    GROOVE_BOOLEAN_TYPE_TERM,
    GROOVE_CANONICAL_JSON_TERM,
    GROOVE_CLOSED_PROFILE_TYPE_TERM,
    GROOVE_FAMILY_TERM,
    GROOVE_INTENT_DOCUMENT_ROLE_TERM,
    GROOVE_OPERATION_TERM,
    GROOVE_PFG_TERMS,
    GROOVE_PROFILE_RESULT_ROLE_TERM,
    GROOVE_PROFILE_ROLE_TERM,
    GROOVE_REQUEST_TERMS,
    GROOVE_REVERSED_ROLE_TERM,
    GROOVE_SKETCH_AXIS_TYPE_TERM,
    GROOVE_SKETCH_V_AXIS_LOCATOR_TERM,
    GROOVE_SOLID_RESULT_ROLE_TERM,
    GROOVE_SOLID_TYPE_TERM,
    GROOVE_STRUCTURE_TERM,
    FreeCADParametricGrooveAdapter,
    PlanSink,
    build_groove_capability_document,
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
from vibecad.parametric.freecad_partdesign_sketch_rules import MAX_GROOVE_PLAN_BYTES


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pfg_term(term_ref_id: str, term_id: str) -> SemanticTermRefV2:
    return SemanticTermRefV2(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.groove-test-source",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"pfg:{term_id}"),
    )


SOURCE_TERMS = (
    _pfg_term("source_structure_base", "structure.test-base"),
    _pfg_term("source_family_base", "family.test-base"),
    _pfg_term("source_operation_base", "operation.test-base"),
    _pfg_term("source_structure_profile", "structure.test-profile"),
    _pfg_term("source_family_profile", "family.test-profile"),
    _pfg_term("source_operation_profile", "operation.test-profile"),
)


def _port(
    port_id: str,
    role: SemanticTermRefV2,
    value_type: SemanticTermRefV2,
) -> FeatureInputPortV2:
    return FeatureInputPortV2(
        port_id=port_id,
        semantic_role_term_ref_id=role.term_ref_id,
        value_type_term_ref_id=value_type.term_ref_id,
        minimum_cardinality=1,
        maximum_cardinality=1,
        ordered=False,
    )


def _graph(
    *,
    angle: float = 360.0,
    reversed_value: bool = False,
    family_definition: str | None = None,
    locator_definition: str | None = None,
) -> ParametricFeatureGraphV2:
    groove_terms = list(GROOVE_PFG_TERMS)
    if family_definition is not None:
        groove_terms[groove_terms.index(GROOVE_FAMILY_TERM)] = dataclasses.replace(
            GROOVE_FAMILY_TERM,
            term_definition_sha256=family_definition,
        )
    if locator_definition is not None:
        groove_terms[groove_terms.index(GROOVE_SKETCH_V_AXIS_LOCATOR_TERM)] = (
            dataclasses.replace(
                GROOVE_SKETCH_V_AXIS_LOCATOR_TERM,
                term_definition_sha256=locator_definition,
            )
        )
    base = FeatureNodeV2(
        node_id="node_base",
        body_id="body_main",
        name="Base input",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=SOURCE_TERMS[0].term_ref_id,
            family_term_ref_id=SOURCE_TERMS[1].term_ref_id,
            operation_term_ref_id=SOURCE_TERMS[2].term_ref_id,
        ),
        results=(
            FeatureResultV2(
                result_id="result_base_solid",
                semantic_role_term_ref_id=GROOVE_SOLID_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=GROOVE_SOLID_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    profile = FeatureNodeV2(
        node_id="node_profile",
        body_id="body_main",
        name="Profile input",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=SOURCE_TERMS[3].term_ref_id,
            family_term_ref_id=SOURCE_TERMS[4].term_ref_id,
            operation_term_ref_id=SOURCE_TERMS[5].term_ref_id,
        ),
        results=(
            FeatureResultV2(
                result_id="result_profile_closed",
                semantic_role_term_ref_id=GROOVE_PROFILE_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=GROOVE_CLOSED_PROFILE_TYPE_TERM.term_ref_id,
            ),
            FeatureResultV2(
                result_id="result_profile_v_axis",
                semantic_role_term_ref_id=GROOVE_AXIS_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=GROOVE_SKETCH_AXIS_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    angle_parameter = DesignParameterV2(
        parameter_id="parameter_angle",
        name="Angle",
        semantic_role_term_ref_id=GROOVE_ANGLE_ROLE_TERM.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id="value_angle",
            value_type_term_ref_id=GROOVE_ANGLE_TYPE_TERM.term_ref_id,
            encoding_term_ref_id=GROOVE_CANONICAL_JSON_TERM.term_ref_id,
            value=angle,
        ),
    )
    reversed_parameter = DesignParameterV2(
        parameter_id="parameter_reversed",
        name="Reversed",
        semantic_role_term_ref_id=GROOVE_REVERSED_ROLE_TERM.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id="value_reversed",
            value_type_term_ref_id=GROOVE_BOOLEAN_TYPE_TERM.term_ref_id,
            encoding_term_ref_id=GROOVE_CANONICAL_JSON_TERM.term_ref_id,
            value=reversed_value,
        ),
    )
    axis = SemanticReferenceV2(
        reference_id="reference_profile_v_axis",
        scope=SemanticReferenceScope.FEATURE,
        semantic_role_term_ref_id=GROOVE_AXIS_ROLE_TERM.term_ref_id,
        value_type_term_ref_id=GROOVE_SKETCH_AXIS_TYPE_TERM.term_ref_id,
        locator_term_ref_id=GROOVE_SKETCH_V_AXIS_LOCATOR_TERM.term_ref_id,
        source_node_id=profile.node_id,
        source_geometry_id="result_profile_v_axis",
    )
    groove = FeatureNodeV2(
        node_id="node_groove",
        body_id="body_main",
        name="Untrusted display name PartDesign::Groove Profile Angle",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=GROOVE_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=GROOVE_FAMILY_TERM.term_ref_id,
            operation_term_ref_id=GROOVE_OPERATION_TERM.term_ref_id,
            input_ports=(
                _port("port_base", GROOVE_BASE_ROLE_TERM, GROOVE_SOLID_TYPE_TERM),
                _port(
                    "port_profile",
                    GROOVE_PROFILE_ROLE_TERM,
                    GROOVE_CLOSED_PROFILE_TYPE_TERM,
                ),
                _port("port_axis", GROOVE_AXIS_ROLE_TERM, GROOVE_SKETCH_AXIS_TYPE_TERM),
                _port("port_angle", GROOVE_ANGLE_ROLE_TERM, GROOVE_ANGLE_TYPE_TERM),
                _port("port_reversed", GROOVE_REVERSED_ROLE_TERM, GROOVE_BOOLEAN_TYPE_TERM),
            ),
            dependencies=(
                FeatureDependencyV2(
                    dependency_id="dependency_base",
                    port_id="port_base",
                    upstream_node_id=base.node_id,
                    upstream_result_id="result_base_solid",
                ),
                FeatureDependencyV2(
                    dependency_id="dependency_profile",
                    port_id="port_profile",
                    upstream_node_id=profile.node_id,
                    upstream_result_id="result_profile_closed",
                ),
            ),
            references=(
                FeatureReferenceBindingV2(
                    binding_id="binding_axis",
                    port_id="port_axis",
                    reference_id=axis.reference_id,
                ),
            ),
            parameter_bindings=(
                FeatureParameterBindingV2(
                    binding_id="binding_angle",
                    port_id="port_angle",
                    parameter_id=angle_parameter.parameter_id,
                ),
                FeatureParameterBindingV2(
                    binding_id="binding_reversed",
                    port_id="port_reversed",
                    parameter_id=reversed_parameter.parameter_id,
                ),
            ),
        ),
        results=(
            FeatureResultV2(
                result_id="result_groove_solid",
                semantic_role_term_ref_id=GROOVE_SOLID_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=GROOVE_SOLID_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id="graph_groove",
        name="Groove graph",
        terms=tuple((*groove_terms, *SOURCE_TERMS)),
        bodies=(FeatureBodyV2(body_id="body_main", name="Main"),),
        parameters=(angle_parameter, reversed_parameter),
        references=(axis,),
        nodes=(base, profile, groove),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection_groove",
                node_id=groove.node_id,
                result_id="result_groove_solid",
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
        namespace="org.vibecad.groove-proof-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"proof:{term_id}"),
    )


RULE = _proof_term("rule_groove_target", "rule.groove-target-reviewed")
PREDICATE = _proof_term("predicate_groove_target", "predicate.groove-target-reviewed")
ROLE_PREMISE = _proof_term("role_groove_candidate", "proof-role.groove-candidate")
ROLE_CONCLUSION = _proof_term("role_groove_validated", "proof-role.groove-validated")
GROOVE_STRUCTURE_BRIDGE = _bridge_from_pfg(GROOVE_STRUCTURE_TERM)


class _GrooveEvaluator:
    def __init__(self) -> None:
        signature = lambda role: RuleEndpointSignature(  # noqa: E731
            selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
            role_term=role,
            subject_type_term=GROOVE_STRUCTURE_BRIDGE,
        )
        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="groove_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("groove-target-evaluator-v1"),
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
            or evaluation.premises[0].subject.selector_id != "node_groove"
            or evaluation.conclusions[0].subject.selector_id != "node_groove"
        ):
            raise IntentBridgeError(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/groove")


def _intent_document(graph: ParametricFeatureGraphV2) -> tuple[DocumentRef, bytes]:
    payload = graph.canonical_bytes
    return (
        DocumentRef(
            artifact_id="artifact_pfg",
            role_term_ref_id=GROOVE_INTENT_DOCUMENT_ROLE_TERM.term_ref_id,
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
        selector_id="node_groove",
    )


def _proof(policy: TrustedRulePolicy, intent_document: DocumentRef) -> ProofBundle:
    terms = (
        RULE,
        PREDICATE,
        ROLE_PREMISE,
        ROLE_CONCLUSION,
        GROOVE_STRUCTURE_BRIDGE,
        GROOVE_INTENT_DOCUMENT_ROLE_TERM,
        PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
        PFG_SELECTOR_FEATURE_NODE,
    )
    return ProofBundle(
        terms=terms,
        documents=(intent_document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_groove_target",
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
                producer_id="groove_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("groove-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("upstream-compile-request"),
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
    def __init__(
        self,
        *,
        fail: bool = False,
        corrupt_publish: bool = False,
        corrupt_readback: bool = False,
    ) -> None:
        self.fail = fail
        self.corrupt_publish = corrupt_publish
        self.corrupt_readback = corrupt_readback
        self.items: dict[str, tuple[DocumentRef, bytes]] = {}
        self.publish_calls = 0

    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        self.publish_calls += 1
        if self.fail:
            raise RuntimeError("not reflected")
        if self.corrupt_publish:
            return payload + b"x"
        existing = self.items.get(document.artifact_id)
        if existing is not None:
            if existing != (document, payload):
                raise RuntimeError("collision")
            return payload
        staged = dict(self.items)
        staged[document.artifact_id] = (document, payload)
        self.items = staged
        return payload

    def read_exact(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        stored_document, payload = self.items[document.artifact_id]
        if stored_document != document or len(payload) > maximum_bytes:
            raise RuntimeError("bad read")
        return payload + b"x" if self.corrupt_readback else payload


def _request(
    graph: ParametricFeatureGraphV2,
    *,
    max_output_bytes: int = MAX_GROOVE_PLAN_BYTES,
) -> tuple[BackendLoweringRequest, _Reader, TrustedRulePolicy]:
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = build_groove_capability_document()
    policy = TrustedRulePolicy(evaluators=(_GrooveEvaluator(),))
    proof = _proof(policy, intent_document)
    extra_terms = (RULE, PREDICATE, ROLE_PREMISE, ROLE_CONCLUSION, PFG_SELECTOR_FEATURE_NODE)
    request = BackendLoweringRequest(
        adapter=FREECAD_GROOVE_ADAPTER_DESCRIPTOR,
        terms=tuple((*GROOVE_REQUEST_TERMS, *extra_terms)),
        documents=(intent_document, capability_document),
        intent_artifact_ids=(intent_document.artifact_id,),
        capability_artifact_ids=(capability_document.artifact_id,),
        proof_bundle=proof,
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
    adapter: FreeCADParametricGrooveAdapter,
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


def test_adapter_lowers_exact_graph_to_atomic_content_addressed_authority_free_plan() -> None:
    request, reader, policy = _request(_graph())
    sink = _MemoryPlanSink()
    adapter = FreeCADParametricGrooveAdapter(sink)

    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    repeated, repeated_receipt = _lower(adapter, request, reader, policy)

    assert isinstance(sink, PlanSink)
    assert isinstance(adapter, IntentBackendAdapter)
    assert result.disposition is BridgeDisposition.COMPLETE
    assert result.plan_document == receipt.plan_document
    assert result.supported_subjects == (_subject(),)
    assert plan.node_id == "node_groove"
    assert plan.angle_degrees == 360.0
    assert plan.reversed is False
    assert plan.lowering_request_sha256 == request.request_digest
    assert (
        plan.adapter_contract_sha256
        == FREECAD_GROOVE_ADAPTER_DESCRIPTOR.adapter_contract_sha256
    )
    assert payload == plan.canonical_bytes
    assert result.plan_document.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert result.plan_document.document_digest == plan.plan_sha256
    assert adapter.executable is False and adapter.grants_execution_authority is False
    assert receipt.executable is False and receipt.grants_execution_authority is False
    assert repeated == result and repeated_receipt == receipt
    assert sink.publish_calls == 2 and len(sink.items) == 1
    assert "PartDesign::Groove" not in payload.decode("ascii")
    assert "Profile" not in payload.decode("ascii")

    substituted_receipt = dataclasses.replace(receipt, request_digest="f" * 64)
    with pytest.raises(IntentBridgeError) as receipt_error:
        adapter.read_plan(substituted_receipt)
    assert receipt_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE


def test_adapter_rejects_semantic_substitution_and_sink_failure_without_publication() -> None:
    wrong_request, wrong_reader, wrong_policy = _request(
        _graph(family_definition="f" * 64)
    )
    sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as semantic_error:
        _lower(FreeCADParametricGrooveAdapter(sink), wrong_request, wrong_reader, wrong_policy)
    assert semantic_error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    request, reader, policy = _request(_graph())
    failed_sink = _MemoryPlanSink(fail=True)
    with pytest.raises(IntentBridgeError) as sink_error:
        _lower(FreeCADParametricGrooveAdapter(failed_sink), request, reader, policy)
    assert sink_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert failed_sink.items == {}
    assert "not reflected" not in str(sink_error.value)

    wrong_publish_sink = _MemoryPlanSink(corrupt_publish=True)
    with pytest.raises(IntentBridgeError) as publish_error:
        _lower(
            FreeCADParametricGrooveAdapter(wrong_publish_sink),
            request,
            reader,
            policy,
        )
    assert publish_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert wrong_publish_sink.items == {}

    corrupt_sink = _MemoryPlanSink(corrupt_readback=True)
    _result, corrupt_receipt = _lower(
        FreeCADParametricGrooveAdapter(corrupt_sink),
        request,
        reader,
        policy,
    )
    with pytest.raises(IntentBridgeError) as readback_error:
        FreeCADParametricGrooveAdapter(corrupt_sink).read_plan(corrupt_receipt)
    assert readback_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize(
    "graph",
    (
        _graph(locator_definition="e" * 64),
        _graph(angle=0.0),
        _graph(angle=360.001),
        _graph(reversed_value=1),
    ),
)
def test_adapter_rejects_unauthenticated_axis_and_unbounded_typed_values(
    graph: ParametricFeatureGraphV2,
) -> None:
    request, reader, policy = _request(graph)
    sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as error:
        _lower(FreeCADParametricGrooveAdapter(sink), request, reader, policy)
    assert error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}


def test_adapter_rejects_capability_payload_tamper_before_plan_publication() -> None:
    request, reader, policy = _request(_graph())
    capability_id = request.capability_artifact_ids[0]
    reader.payloads[capability_id] += b" "
    sink = _MemoryPlanSink()

    with pytest.raises(IntentBridgeError) as error:
        _lower(FreeCADParametricGrooveAdapter(sink), request, reader, policy)

    assert error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert sink.items == {}


def test_adapter_enforces_plan_budget_at_exact_n_and_n_plus_one_boundary() -> None:
    request, reader, policy = _request(_graph())
    result, _receipt = _lower(
        FreeCADParametricGrooveAdapter(_MemoryPlanSink()),
        request,
        reader,
        policy,
    )
    size = result.plan_document.size_bytes

    exact_request, exact_reader, exact_policy = _request(_graph(), max_output_bytes=size)
    exact_result, _ = _lower(
        FreeCADParametricGrooveAdapter(_MemoryPlanSink()),
        exact_request,
        exact_reader,
        exact_policy,
    )
    assert exact_result.plan_document.size_bytes == size

    small_request, small_reader, small_policy = _request(_graph(), max_output_bytes=size - 1)
    small_sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as error:
        _lower(
            FreeCADParametricGrooveAdapter(small_sink),
            small_request,
            small_reader,
            small_policy,
        )
    assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert small_sink.items == {}


@pytest.mark.slow
def test_real_freecad_groove_create_edit_save_reopen_and_rollback(
    tmp_path: Path,
) -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the real FreeCAD gate")
    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime import status as runtime_status

    runtime_python = runtime_paths.active_runtime_python()
    if not runtime_python.is_file() or not runtime_paths.ready_sentinel().is_file():
        pytest.fail("an existing ready managed FreeCAD runtime is required")
    if not runtime_status.engine_compatible(runtime_python):
        pytest.fail("the existing managed FreeCAD runtime does not match current engine pins")

    request, reader, policy = _request(_graph())
    sink = _MemoryPlanSink()
    adapter = FreeCADParametricGrooveAdapter(sink)
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    plan_path = tmp_path / "groove-plan.json"
    plan_path.write_bytes(payload)
    reversed_request, reversed_reader, reversed_policy = _request(
        _graph(angle=180.0, reversed_value=True)
    )
    reversed_adapter = FreeCADParametricGrooveAdapter(_MemoryPlanSink())
    reversed_result, reversed_receipt = _lower(
        reversed_adapter,
        reversed_request,
        reversed_reader,
        reversed_policy,
    )
    reversed_plan, reversed_payload = reversed_adapter.read_plan(reversed_receipt)
    assert reversed_plan.reversed is True and reversed_plan.angle_degrees == 180.0
    reversed_plan_path = tmp_path / "groove-reversed-plan.json"
    reversed_plan_path.write_bytes(reversed_payload)
    model_path = tmp_path / "groove.FCStd"
    source_root = Path(__file__).parents[1] / "src"
    code = f"""
import os, sys
sys.path.insert(0, os.path.join(sys.prefix, 'lib'))
sys.path.insert(0, {str(source_root)!r})
from pathlib import Path
import FreeCAD, Part, Sketcher
from vibecad.parametric.freecad_partdesign_sketch_rules import (
    GrooveExecutionBindings, GrooveRuleError, apply_groove_plan,
)
payload = Path({str(plan_path)!r}).read_bytes()
expected_content = {result.plan_document.content_sha256!r}
expected_plan = {result.plan_document.document_digest!r}
reversed_payload = Path({str(reversed_plan_path)!r}).read_bytes()
reversed_content = {reversed_result.plan_document.content_sha256!r}
reversed_digest = {reversed_result.plan_document.document_digest!r}
def make_case(name, points):
    document = FreeCAD.newDocument(name)
    document.UndoMode = 1
    body = document.addObject('PartDesign::Body', 'Body')
    base = body.newObject('PartDesign::Feature', 'BaseSolid')
    base.Shape = Part.makeCylinder(10, 20)
    profile = body.newObject('Sketcher::SketchObject', 'Profile')
    profile.Placement = FreeCAD.Placement(
        FreeCAD.Vector(0, 0, 0), FreeCAD.Rotation(FreeCAD.Vector(1, 0, 0), 90))
    for index in range(len(points) - 1):
        profile.addGeometry(Part.LineSegment(
            FreeCAD.Vector(*points[index], 0), FreeCAD.Vector(*points[index + 1], 0)), False)
    document.recompute()
    bindings = GrooveExecutionBindings(
        document=document, body=body, base_feature=base, profile=profile,
        body_id={plan.body_id!r}, base_node_id={plan.base_node_id!r},
        base_result_id={plan.base_result_id!r}, profile_node_id={plan.profile_node_id!r},
        profile_result_id={plan.profile_result_id!r})
    return document, body, base, profile, bindings
points = [(8, 8), (12, 8), (12, 12), (8, 12), (8, 8)]
document, body, base, profile, bindings = make_case('GrooveValid', points)
receipt = apply_groove_plan(payload, expected_content_sha256=expected_content,
    expected_plan_sha256=expected_plan, bindings=bindings)
groove = document.getObject(receipt.object_name)
assert groove is body.Tip and groove.BaseFeature is base
assert groove.Profile[0] is profile and tuple(groove.Profile[1]) == ()
assert groove.ReferenceAxis[0] is profile and tuple(groove.ReferenceAxis[1]) == ('V_Axis',)
volume_360 = float(groove.Shape.Volume)
groove.Angle = 180.0
document.recompute()
volume_180 = float(groove.Shape.Volume)
assert volume_360 < volume_180 < float(base.Shape.Volume)
document.saveAs({str(model_path)!r})
name = receipt.object_name
FreeCAD.closeDocument(document.Name)
reopened = FreeCAD.openDocument({str(model_path)!r})
reopened.recompute()
persisted = reopened.getObject(name)
assert persisted.BaseFeature.Name == 'BaseSolid'
assert persisted.Profile[0].Name == 'Profile'
assert tuple(persisted.ReferenceAxis[1]) == ('V_Axis',)
assert abs(float(persisted.Angle) - 180.0) < 1e-9
assert abs(float(persisted.Shape.Volume) - volume_180) < 1e-7
FreeCAD.closeDocument(reopened.Name)
far = [(30, 8), (35, 8), (35, 12), (30, 12), (30, 8)]
bad, bad_body, _bad_base, _bad_profile, bad_bindings = make_case('GrooveNoOp', far)
before_names = tuple(item.Name for item in bad.Objects)
before_tip = bad_body.Tip
try:
    apply_groove_plan(payload, expected_content_sha256=expected_content,
        expected_plan_sha256=expected_plan, bindings=bad_bindings)
except GrooveRuleError:
    pass
else:
    raise AssertionError('non-intersecting Groove must fail')
assert tuple(item.Name for item in bad.Objects) == before_names and bad_body.Tip is before_tip
FreeCAD.closeDocument(bad.Name)
open_points = [(8, 8), (12, 8)]
invalid, invalid_body, _base, _profile, invalid_bindings = make_case(
    'GrooveInvalidProfile', open_points)
before_names = tuple(item.Name for item in invalid.Objects)
before_tip = invalid_body.Tip
try:
    apply_groove_plan(payload, expected_content_sha256=expected_content,
        expected_plan_sha256=expected_plan, bindings=invalid_bindings)
except GrooveRuleError:
    pass
else:
    raise AssertionError('open profile must fail')
assert (
    tuple(item.Name for item in invalid.Objects) == before_names
    and invalid_body.Tip is before_tip
)
FreeCAD.closeDocument(invalid.Name)
reversed_doc, reversed_body, _base, _profile, reversed_bindings = make_case(
    'GrooveReversed', points)
reversed_receipt = apply_groove_plan(
    reversed_payload, expected_content_sha256=reversed_content,
    expected_plan_sha256=reversed_digest, bindings=reversed_bindings)
reversed_groove = reversed_doc.getObject(reversed_receipt.object_name)
assert reversed_groove is reversed_body.Tip
assert bool(reversed_groove.Reversed) is True
assert abs(float(reversed_groove.Angle) - 180.0) < 1e-9
FreeCAD.closeDocument(reversed_doc.Name)
print('REAL_GROOVE_RULE_OK')
"""
    try:
        completed = subprocess.run(
            [str(runtime_python), "-c", code],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert completed.returncode == 0, completed.stderr
        assert "REAL_GROOVE_RULE_OK" in completed.stdout
    finally:
        plan_path.unlink(missing_ok=True)
        reversed_plan_path.unlink(missing_ok=True)
        model_path.unlink(missing_ok=True)
