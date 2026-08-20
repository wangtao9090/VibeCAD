"""Focused lowering tests for the six-operation PartDesign promotion batch."""

from __future__ import annotations

import dataclasses
import hashlib
import json
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
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_partdesign_promotion_adapter import (
    FREECAD_PARTDESIGN_PROMOTION_ADAPTER_DESCRIPTOR,
    PROMOTION_ANGLE_ROLE_TERM,
    PROMOTION_ANGLE_TYPE_TERM,
    PROMOTION_AXIS_RESULT_ROLE_TERM,
    PROMOTION_AXIS_ROLE_TERM,
    PROMOTION_BASE_ROLE_TERM,
    PROMOTION_CANONICAL_JSON_TERM,
    PROMOTION_CLOSED_PROFILE_TYPE_TERM,
    PROMOTION_CONTINUOUS_SPINE_TYPE_TERM,
    PROMOTION_HEIGHT_ROLE_TERM,
    PROMOTION_INTENT_DOCUMENT_ROLE_TERM,
    PROMOTION_LENGTH_TYPE_TERM,
    PROMOTION_OPERATION_TERMS,
    PROMOTION_PFG_TERMS,
    PROMOTION_PITCH_ROLE_TERM,
    PROMOTION_PROFILE_RESULT_ROLE_TERM,
    PROMOTION_PROFILE_ROLE_TERM,
    PROMOTION_REQUEST_TERMS,
    PROMOTION_SKETCH_AXIS_TYPE_TERM,
    PROMOTION_SKETCH_V_AXIS_LOCATOR_TERM,
    PROMOTION_SOLID_RESULT_ROLE_TERM,
    PROMOTION_SOLID_TYPE_TERM,
    PROMOTION_SPINE_RESULT_ROLE_TERM,
    PROMOTION_SPINE_ROLE_TERM,
    PROMOTION_STRUCTURE_TERM,
    FreeCADPartDesignPromotionAdapter,
    build_promotion_capability_document,
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
from vibecad.parametric.freecad_partdesign_promotion_rules import (
    MAX_PARTDESIGN_PROMOTION_PLAN_BYTES,
    PartDesignPromotionOperation,
    PartDesignPromotionRuleError,
    decode_partdesign_promotion_backend_plan,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pfg_term(term_ref_id: str, term_id: str) -> SemanticTermRefV2:
    return SemanticTermRefV2(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.promotion-test-source",
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
    port_id: str,
    role: SemanticTermRefV2,
    value_type: SemanticTermRefV2,
    *,
    minimum: int = 1,
    maximum: int = 1,
    ordered: bool = False,
) -> FeatureInputPortV2:
    return FeatureInputPortV2(
        port_id=port_id,
        semantic_role_term_ref_id=role.term_ref_id,
        value_type_term_ref_id=value_type.term_ref_id,
        minimum_cardinality=minimum,
        maximum_cardinality=maximum,
        ordered=ordered,
    )


def _source_node(
    node_id: str,
    result_id: str,
    role: SemanticTermRefV2,
    value_type: SemanticTermRefV2,
    *,
    extra_result: FeatureResultV2 | None = None,
) -> FeatureNodeV2:
    results = [
        FeatureResultV2(
            result_id=result_id,
            semantic_role_term_ref_id=role.term_ref_id,
            value_type_term_ref_id=value_type.term_ref_id,
        )
    ]
    if extra_result is not None:
        results.append(extra_result)
    return FeatureNodeV2(
        node_id=node_id,
        body_id="body_main",
        name=f"Source {node_id}",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=SOURCE_TERMS[0].term_ref_id,
            family_term_ref_id=SOURCE_TERMS[1].term_ref_id,
            operation_term_ref_id=SOURCE_TERMS[2].term_ref_id,
        ),
        results=tuple(results),
    )


def _graph(
    operation: PartDesignPromotionOperation,
    *,
    base_for_additive: bool = False,
    profile_count: int | None = None,
    operation_definition: str | None = None,
    pitch: float = 4.0,
    height: float = 12.0,
    angle: float = 0.0,
) -> ParametricFeatureGraphV2:
    operation_terms = next(
        item for item in PROMOTION_OPERATION_TERMS if item.operation is operation
    )
    additive = operation.value.startswith("additive_")
    family = operation.value.rsplit("_", 1)[1]
    count = profile_count if profile_count is not None else (2 if family == "loft" else 1)
    static_terms = list(PROMOTION_PFG_TERMS)
    if operation_definition is not None:
        index = static_terms.index(operation_terms.operation_term)
        static_terms[index] = dataclasses.replace(
            operation_terms.operation_term,
            term_definition_sha256=operation_definition,
        )
    nodes: list[FeatureNodeV2] = []
    ports = [
        _port(
            "port_base",
            PROMOTION_BASE_ROLE_TERM,
            PROMOTION_SOLID_TYPE_TERM,
            minimum=0 if additive else 1,
        ),
        _port(
            "port_profiles",
            PROMOTION_PROFILE_ROLE_TERM,
            PROMOTION_CLOSED_PROFILE_TYPE_TERM,
            minimum=2 if family == "loft" else 1,
            maximum=8 if family == "loft" else 1,
            ordered=family == "loft",
        ),
    ]
    dependencies: list[FeatureDependencyV2] = []
    if not additive or base_for_additive:
        base = _source_node(
            "node_base",
            "result_base",
            PROMOTION_SOLID_RESULT_ROLE_TERM,
            PROMOTION_SOLID_TYPE_TERM,
        )
        nodes.append(base)
        dependencies.append(
            FeatureDependencyV2(
                dependency_id="dependency_base",
                port_id="port_base",
                upstream_node_id=base.node_id,
                upstream_result_id="result_base",
            )
        )
    profiles = []
    for index in range(count):
        extra_result = (
            FeatureResultV2(
                result_id="result_profile_axis",
                semantic_role_term_ref_id=PROMOTION_AXIS_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=PROMOTION_SKETCH_AXIS_TYPE_TERM.term_ref_id,
            )
            if family == "helix"
            else None
        )
        profile = _source_node(
            f"node_profile_{index}",
            f"result_profile_{index}",
            PROMOTION_PROFILE_RESULT_ROLE_TERM,
            PROMOTION_CLOSED_PROFILE_TYPE_TERM,
            extra_result=extra_result,
        )
        profiles.append(profile)
        nodes.append(profile)
        dependencies.append(
            FeatureDependencyV2(
                dependency_id=f"dependency_profile_{index}",
                port_id="port_profiles",
                upstream_node_id=profile.node_id,
                upstream_result_id=f"result_profile_{index}",
                ordinal=index,
            )
        )
    references = []
    reference_bindings = []
    parameters = []
    parameter_bindings = []
    if family == "pipe":
        ports.append(
            _port(
                "port_spine",
                PROMOTION_SPINE_ROLE_TERM,
                PROMOTION_CONTINUOUS_SPINE_TYPE_TERM,
            )
        )
        spine = _source_node(
            "node_spine",
            "result_spine",
            PROMOTION_SPINE_RESULT_ROLE_TERM,
            PROMOTION_CONTINUOUS_SPINE_TYPE_TERM,
        )
        nodes.append(spine)
        dependencies.append(
            FeatureDependencyV2(
                dependency_id="dependency_spine",
                port_id="port_spine",
                upstream_node_id=spine.node_id,
                upstream_result_id="result_spine",
            )
        )
    elif family == "helix":
        ports.append(
            _port(
                "port_axis",
                PROMOTION_AXIS_ROLE_TERM,
                PROMOTION_SKETCH_AXIS_TYPE_TERM,
            )
        )
        axis = SemanticReferenceV2(
            reference_id="reference_profile_axis",
            scope=SemanticReferenceScope.FEATURE,
            semantic_role_term_ref_id=PROMOTION_AXIS_ROLE_TERM.term_ref_id,
            value_type_term_ref_id=PROMOTION_SKETCH_AXIS_TYPE_TERM.term_ref_id,
            locator_term_ref_id=PROMOTION_SKETCH_V_AXIS_LOCATOR_TERM.term_ref_id,
            source_node_id=profiles[0].node_id,
            source_geometry_id="result_profile_axis",
        )
        references.append(axis)
        reference_bindings.append(
            FeatureReferenceBindingV2(
                binding_id="binding_axis",
                port_id="port_axis",
                reference_id=axis.reference_id,
            )
        )
        for kind, role, value_type, value in (
            ("pitch", PROMOTION_PITCH_ROLE_TERM, PROMOTION_LENGTH_TYPE_TERM, pitch),
            ("height", PROMOTION_HEIGHT_ROLE_TERM, PROMOTION_LENGTH_TYPE_TERM, height),
            ("angle", PROMOTION_ANGLE_ROLE_TERM, PROMOTION_ANGLE_TYPE_TERM, angle),
        ):
            port_id = f"port_{kind}"
            ports.append(_port(port_id, role, value_type))
            parameter = DesignParameterV2(
                parameter_id=f"parameter_{kind}",
                name=kind.title(),
                semantic_role_term_ref_id=role.term_ref_id,
                value=TermTypedValueV2.from_value(
                    value_id=f"value_{kind}",
                    value_type_term_ref_id=value_type.term_ref_id,
                    encoding_term_ref_id=PROMOTION_CANONICAL_JSON_TERM.term_ref_id,
                    value=value,
                ),
            )
            parameters.append(parameter)
            parameter_bindings.append(
                FeatureParameterBindingV2(
                    binding_id=f"binding_{kind}",
                    port_id=port_id,
                    parameter_id=parameter.parameter_id,
                )
            )
    target = FeatureNodeV2(
        node_id="node_target",
        body_id="body_main",
        name="Untrusted PartDesign::AdditivePipe TypeId property selector",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=PROMOTION_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=operation_terms.family_term.term_ref_id,
            operation_term_ref_id=operation_terms.operation_term.term_ref_id,
            input_ports=tuple(ports),
            dependencies=tuple(dependencies),
            references=tuple(reference_bindings),
            parameter_bindings=tuple(parameter_bindings),
        ),
        results=(
            FeatureResultV2(
                result_id="result_target",
                semantic_role_term_ref_id=PROMOTION_SOLID_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=PROMOTION_SOLID_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    nodes.append(target)
    return ParametricFeatureGraphV2(
        graph_id=(
            f"graph_{operation.value}_with_base"
            if base_for_additive
            else f"graph_{operation.value}"
        ),
        name=f"Promotion {operation.value}",
        terms=tuple((*static_terms, *SOURCE_TERMS)),
        bodies=(FeatureBodyV2(body_id="body_main", name="Main"),),
        parameters=tuple(parameters),
        references=tuple(references),
        nodes=tuple(nodes),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection_target",
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
        namespace="org.vibecad.promotion-proof-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"proof:{term_id}"),
    )


RULE = _proof_term("rule_promotion_target", "rule.promotion-target-reviewed")
PREDICATE = _proof_term("predicate_promotion_target", "predicate.promotion-target-reviewed")
ROLE_PREMISE = _proof_term("role_promotion_candidate", "proof-role.promotion-candidate")
ROLE_CONCLUSION = _proof_term("role_promotion_validated", "proof-role.promotion-validated")
PROMOTION_STRUCTURE_BRIDGE = _bridge_from_pfg(PROMOTION_STRUCTURE_TERM)


class _PromotionEvaluator:
    def __init__(self) -> None:
        def signature(role: BridgeTermRef) -> RuleEndpointSignature:
            return RuleEndpointSignature(
                selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
                role_term=role,
                subject_type_term=PROMOTION_STRUCTURE_BRIDGE,
            )

        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="partdesign_promotion_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("partdesign-promotion-target-evaluator-v1"),
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
            role_term_ref_id=PROMOTION_INTENT_DOCUMENT_ROLE_TERM.term_ref_id,
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
            PROMOTION_STRUCTURE_BRIDGE,
            PROMOTION_INTENT_DOCUMENT_ROLE_TERM,
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_promotion_target",
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
                producer_id="promotion_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("promotion-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("upstream-promotion-compile-request"),
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
    max_output_bytes: int = MAX_PARTDESIGN_PROMOTION_PLAN_BYTES,
) -> tuple[BackendLoweringRequest, _Reader, TrustedRulePolicy]:
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = build_promotion_capability_document()
    policy = TrustedRulePolicy(evaluators=(_PromotionEvaluator(),))
    request = BackendLoweringRequest(
        adapter=FREECAD_PARTDESIGN_PROMOTION_ADAPTER_DESCRIPTOR,
        terms=tuple(
            (
                *PROMOTION_REQUEST_TERMS,
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
    adapter: FreeCADPartDesignPromotionAdapter,
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


@pytest.mark.parametrize("operation", tuple(PartDesignPromotionOperation))
def test_shared_adapter_lowers_all_six_operations_without_native_string_authority(
    operation: PartDesignPromotionOperation,
) -> None:
    request, reader, policy = _request(_graph(operation))
    sink = _MemoryPlanSink()
    adapter = FreeCADPartDesignPromotionAdapter(sink)

    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    repeated, repeated_receipt = _lower(adapter, request, reader, policy)

    assert isinstance(sink, PlanSink)
    assert isinstance(adapter, IntentBackendAdapter)
    assert result.disposition is BridgeDisposition.COMPLETE
    assert result.plan_document == receipt.plan_document
    assert result.supported_subjects == (_subject(),)
    assert plan.operation is operation
    assert (plan.base is None) is operation.value.startswith("additive_")
    assert len(plan.profiles) == (2 if operation.value.endswith("_loft") else 1)
    assert (plan.spine is not None) is operation.value.endswith("_pipe")
    assert (plan.axis_reference_id is not None) is operation.value.endswith("_helix")
    assert payload == plan.canonical_bytes
    assert result.plan_document.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert result.plan_document.document_digest == plan.plan_sha256
    assert adapter.executable is False and adapter.grants_execution_authority is False
    assert receipt.executable is False and receipt.grants_execution_authority is False
    assert repeated == result and repeated_receipt == receipt
    assert len(sink.items) == 1
    text = payload.decode("ascii")
    assert "PartDesign::" not in text and "Profile" not in text and "Spine" not in text


@pytest.mark.parametrize(
    "operation",
    (
        PartDesignPromotionOperation.ADDITIVE_LOFT,
        PartDesignPromotionOperation.ADDITIVE_PIPE,
        PartDesignPromotionOperation.ADDITIVE_HELIX,
    ),
)
def test_additive_operations_accept_an_authenticated_optional_base(
    operation: PartDesignPromotionOperation,
) -> None:
    request, reader, policy = _request(_graph(operation, base_for_additive=True))
    adapter = FreeCADPartDesignPromotionAdapter(_MemoryPlanSink())
    _result, receipt = _lower(adapter, request, reader, policy)
    plan, _payload = adapter.read_plan(receipt)
    assert plan.operation is operation
    assert plan.base is not None and plan.base.node_id == "node_base"


def test_adapter_rejects_semantic_substitution_and_atomic_sink_failure() -> None:
    graph = _graph(
        PartDesignPromotionOperation.ADDITIVE_PIPE,
        operation_definition="f" * 64,
    )
    request, reader, policy = _request(graph)
    sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as semantic_error:
        _lower(FreeCADPartDesignPromotionAdapter(sink), request, reader, policy)
    assert semantic_error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    request, reader, policy = _request(_graph(PartDesignPromotionOperation.ADDITIVE_PIPE))
    failed_sink = _MemoryPlanSink(fail=True)
    with pytest.raises(IntentBridgeError) as sink_error:
        _lower(FreeCADPartDesignPromotionAdapter(failed_sink), request, reader, policy)
    assert sink_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert failed_sink.items == {}
    assert "untrusted detail" not in str(sink_error.value)


@pytest.mark.parametrize(
    ("pitch", "height", "angle"),
    ((0.0, 12.0, 0.0), (4.0, 0.0, 0.0), (0.01, 20.0, 0.0), (4.0, 12.0, 0.1)),
)
def test_adapter_rejects_unbounded_helix_parameters(
    pitch: float, height: float, angle: float
) -> None:
    request, reader, policy = _request(
        _graph(
            PartDesignPromotionOperation.ADDITIVE_HELIX,
            pitch=pitch,
            height=height,
            angle=angle,
        )
    )
    sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError):
        _lower(FreeCADPartDesignPromotionAdapter(sink), request, reader, policy)
    assert sink.items == {}


def test_adapter_accepts_eight_ordered_loft_profiles_and_enforces_plan_budget() -> None:
    graph = _graph(PartDesignPromotionOperation.ADDITIVE_LOFT, profile_count=8)
    request, reader, policy = _request(graph)
    adapter = FreeCADPartDesignPromotionAdapter(_MemoryPlanSink())
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    assert [item.node_id for item in plan.profiles] == [
        f"node_profile_{index}" for index in range(8)
    ]
    assert (
        decode_partdesign_promotion_backend_plan(
            payload,
            expected_content_sha256=result.plan_document.content_sha256,
            expected_plan_sha256=result.plan_document.document_digest,
        )
        == plan
    )
    size = result.plan_document.size_bytes

    exact, exact_reader, exact_policy = _request(graph, max_output_bytes=size)
    exact_result, _ = _lower(
        FreeCADPartDesignPromotionAdapter(_MemoryPlanSink()),
        exact,
        exact_reader,
        exact_policy,
    )
    assert exact_result.plan_document.size_bytes == size
    small, small_reader, small_policy = _request(graph, max_output_bytes=size - 1)
    small_sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as error:
        _lower(
            FreeCADPartDesignPromotionAdapter(small_sink),
            small,
            small_reader,
            small_policy,
        )
    assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert small_sink.items == {}

    with pytest.raises(PartDesignPromotionRuleError):
        decode_partdesign_promotion_backend_plan(payload + b" ")


def test_plan_decoder_bounds_adversarial_numbers_and_duplicate_semantic_sources() -> None:
    request, reader, policy = _request(_graph(PartDesignPromotionOperation.ADDITIVE_HELIX))
    adapter = FreeCADPartDesignPromotionAdapter(_MemoryPlanSink())
    _result, receipt = _lower(adapter, request, reader, policy)
    helix, _payload = adapter.read_plan(receipt)
    mapping = helix.to_mapping()
    mapping["operation"]["helix"]["turns"] = 10**4000
    adversarial = json.dumps(
        mapping,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    with pytest.raises(PartDesignPromotionRuleError) as numeric_error:
        decode_partdesign_promotion_backend_plan(adversarial)
    assert len(str(numeric_error.value)) < 160

    request, reader, policy = _request(_graph(PartDesignPromotionOperation.ADDITIVE_LOFT))
    adapter = FreeCADPartDesignPromotionAdapter(_MemoryPlanSink())
    _result, receipt = _lower(adapter, request, reader, policy)
    loft, _payload = adapter.read_plan(receipt)
    with pytest.raises(PartDesignPromotionRuleError):
        dataclasses.replace(loft, profiles=(loft.profiles[0], loft.profiles[0]))


@pytest.mark.slow
def test_real_freecad_batch_create_edit_save_reopen_and_rollback(
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
    for operation in PartDesignPromotionOperation:
        variants = [(False, None)]
        if operation.value.startswith("additive_"):
            variants.append((True, None))
        if operation is PartDesignPromotionOperation.ADDITIVE_LOFT:
            variants.append((False, 8))
        for with_base, profile_count in variants:
            request, reader, policy = _request(
                _graph(
                    operation,
                    base_for_additive=with_base,
                    profile_count=profile_count,
                )
            )
            adapter = FreeCADPartDesignPromotionAdapter(_MemoryPlanSink())
            result, receipt = _lower(adapter, request, reader, policy)
            plan, payload = adapter.read_plan(receipt)
            plan_path = tmp_path / (
                f"{operation.value}-base-{int(with_base)}-profiles-{len(plan.profiles)}.json"
            )
            plan_path.write_bytes(payload)
            cases.append(
                {
                    "operation": operation.value,
                    "with_base": plan.base is not None,
                    "path": str(plan_path),
                    "content_sha256": result.plan_document.content_sha256,
                    "plan_sha256": result.plan_document.document_digest,
                    "body_id": plan.body_id,
                    "base": (
                        None if plan.base is None else (plan.base.node_id, plan.base.result_id)
                    ),
                    "profiles": [(item.node_id, item.result_id) for item in plan.profiles],
                    "spine": (
                        None if plan.spine is None else (plan.spine.node_id, plan.spine.result_id)
                    ),
                }
            )
    model_path = tmp_path / "partdesign-promotions.FCStd"
    source_root = Path(__file__).parents[1] / "src"
    code = f"""
import os, sys
sys.path.insert(0, {str(source_root)!r})
from vibecad.freecad_env import prepare_freecad_import
prepare_freecad_import()
from pathlib import Path
import FreeCAD, Part, Sketcher
from vibecad.parametric.freecad_partdesign_promotion_rules import (
    AuthenticatedPromotionObject,
    PartDesignPromotionExecutionBindings,
    PartDesignPromotionRuleError,
    apply_partdesign_promotion_plan,
)

CASES = {cases!r}
TYPE_IDS = {{
    'additive_loft': 'PartDesign::AdditiveLoft',
    'subtractive_loft': 'PartDesign::SubtractiveLoft',
    'additive_pipe': 'PartDesign::AdditivePipe',
    'subtractive_pipe': 'PartDesign::SubtractivePipe',
    'additive_helix': 'PartDesign::AdditiveHelix',
    'subtractive_helix': 'PartDesign::SubtractiveHelix',
}}

def add_circle(body, name, x, radius, z=0.0):
    sketch = body.newObject('Sketcher::SketchObject', name)
    geometry = sketch.addGeometry(
        Part.Circle(FreeCAD.Vector(x, 0, 0), FreeCAD.Vector(0, 0, 1), radius), False)
    sketch.addConstraint(Sketcher.Constraint('Radius', geometry, radius))
    sketch.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 0, z), FreeCAD.Rotation())
    return sketch

def add_path(body, name, x, length, z=0.0):
    sketch = body.newObject('Sketcher::SketchObject', name)
    geometry = sketch.addGeometry(
        Part.LineSegment(FreeCAD.Vector(x, 0, 0), FreeCAD.Vector(x, length, 0)), False)
    sketch.addConstraint(Sketcher.Constraint('Distance', geometry, length))
    sketch.Placement = FreeCAD.Placement(
        FreeCAD.Vector(0, 0, z), FreeCAD.Rotation(FreeCAD.Vector(1, 0, 0), 90))
    return sketch

def make_case(document, entry, index, *, far=False, open_profile=False):
    operation = entry['operation']
    family = operation.rsplit('_', 1)[1]
    body = document.addObject('PartDesign::Body', f'Body{{index}}')
    base = None
    base_auth = None
    if entry['with_base']:
        base = body.newObject('PartDesign::Feature', f'Base{{index}}')
        if family == 'helix':
            base.Shape = Part.makeCylinder(
                6 if operation.startswith('additive_') else 10,
                12,
                FreeCAD.Vector(0, 0, 0),
                FreeCAD.Vector(0, 1, 0),
            )
        else:
            height = 5 if operation.startswith('additive_') else 20
            base.Shape = Part.makeCylinder(10, height)
        base_auth = AuthenticatedPromotionObject(
            object=base, node_id=entry['base'][0], result_id=entry['base'][1])
    center = 30 if far else (9 if family == 'helix' and operation.startswith('subtractive') else 5)
    profiles = []
    if open_profile:
        profile = body.newObject('Sketcher::SketchObject', f'Profile{{index}}_0')
        profile.addGeometry(
            Part.LineSegment(FreeCAD.Vector(center, 0, 0), FreeCAD.Vector(center + 2, 0, 0)),
            False)
        profiles.append(profile)
        if family == 'loft':
            profiles.append(add_circle(body, f'Profile{{index}}_1', 0, 5, 10))
    elif family == 'loft':
        start_z = 5 if operation == 'additive_loft' and entry['with_base'] else 0
        profile_count = len(entry['profiles'])
        for profile_index in range(profile_count):
            fraction = profile_index / (profile_count - 1)
            profiles.append(add_circle(
                body,
                f'Profile{{index}}_{{profile_index}}',
                center if far else 0,
                3 + 2 * fraction,
                start_z + 10 * fraction,
            ))
    else:
        start_z = 5 if operation == 'additive_pipe' and entry['with_base'] else 0
        radius = 2 if family == 'pipe' or operation.startswith('subtractive') else 1
        profiles.append(add_circle(body, f'Profile{{index}}_0', center, radius, start_z))
    profile_auth = tuple(
        AuthenticatedPromotionObject(
            object=obj, node_id=semantic[0], result_id=semantic[1])
        for obj, semantic in zip(profiles, entry['profiles'], strict=True)
    )
    spine = None
    spine_auth = None
    if family == 'pipe':
        start_z = 5 if operation == 'additive_pipe' and entry['with_base'] else 0
        spine = add_path(body, f'Spine{{index}}', center, 15, start_z)
        spine_auth = AuthenticatedPromotionObject(
            object=spine, node_id=entry['spine'][0], result_id=entry['spine'][1])
    if os.name == 'nt' and base is None:
        body.Tip = profiles[-1]
    document.recompute()
    bindings = PartDesignPromotionExecutionBindings(
        document=document,
        body=body,
        body_id=entry['body_id'],
        base=base_auth,
        profiles=profile_auth,
        spine=spine_auth,
    )
    return body, base, profiles, spine, bindings

document = FreeCAD.newDocument('PartDesignPromotionBatch')
document.UndoMode = 1
persisted = []
for index, entry in enumerate(CASES):
    body, base, profiles, spine, bindings = make_case(document, entry, index)
    payload = Path(entry['path']).read_bytes()
    try:
        receipt = apply_partdesign_promotion_plan(
            payload,
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings,
        )
    except Exception as error:
        raise AssertionError(
            f"create failed: {{entry['operation']}} base={{entry['with_base']}}") from error
    feature = document.getObject(receipt.object_name)
    assert feature.TypeId == TYPE_IDS[entry['operation']]
    assert feature is body.Tip and feature.BaseFeature is base
    assert feature.Profile[0] is profiles[0]
    before_edit = float(feature.Shape.Volume)
    family = entry['operation'].rsplit('_', 1)[1]
    if family == 'loft':
        profiles[-1].setDatum(0, FreeCAD.Units.Quantity('4 mm'))
    elif family == 'pipe':
        spine.setDatum(0, FreeCAD.Units.Quantity('12 mm'))
    else:
        feature.Height = 10.0
    document.recompute()
    after_edit = float(feature.Shape.Volume)
    assert feature.isValid() and len(feature.Shape.Solids) == 1
    assert abs(after_edit - before_edit) > 1e-7
    persisted.append((
        feature.Name,
        TYPE_IDS[entry['operation']],
        None if base is None else base.Name,
        profiles[0].Name,
        None if spine is None else spine.Name,
        after_edit,
    ))

document.saveAs({str(model_path)!r})
FreeCAD.closeDocument(document.Name)
reopened = FreeCAD.openDocument({str(model_path)!r})
reopened.recompute()
for name, type_id, base_name, profile_name, spine_name, volume in persisted:
    feature = reopened.getObject(name)
    assert feature.TypeId == type_id and feature.isValid()
    assert len(feature.Shape.Solids) == 1
    assert (None if feature.BaseFeature is None else feature.BaseFeature.Name) == base_name
    assert feature.Profile[0].Name == profile_name
    if spine_name is not None:
        assert feature.Spine[0].Name == spine_name
    assert abs(float(feature.Shape.Volume) - volume) < 1e-6
FreeCAD.closeDocument(reopened.Name)

# Every native TypeId crosses a real failed transaction and restores exact state.
rollback_entries = [
    entry for entry in CASES
    if entry['with_base'] and (
        entry['operation'].startswith('additive_')
        or entry['operation'].startswith('subtractive_')
    )
]
rollback_document = FreeCAD.newDocument('PartDesignPromotionRollbackBatch')
rollback_document.UndoMode = 1
for index, entry in enumerate(rollback_entries):
    body, _base, _profiles, _spine, bindings = make_case(
        rollback_document, entry, index, far=True)
    payload = Path(entry['path']).read_bytes()
    before_objects = tuple(rollback_document.Objects)
    before_group = tuple(body.Group)
    before_tip = body.Tip
    before_visibility = tuple(bool(item.Visibility) for item in before_group)
    try:
        apply_partdesign_promotion_plan(
            payload,
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings,
        )
    except PartDesignPromotionRuleError:
        pass
    else:
        raise AssertionError(f"{{entry['operation']}} disconnected/no-op must fail")
    assert tuple(rollback_document.Objects) == before_objects
    assert tuple(body.Group) == before_group and body.Tip is before_tip
    assert tuple(bool(item.Visibility) for item in before_group) == before_visibility
FreeCAD.closeDocument(rollback_document.Name)

# One open-profile precondition per family also leaves no native residue.
invalid_document = FreeCAD.newDocument('PartDesignPromotionInvalidProfiles')
invalid_document.UndoMode = 1
for index, family in enumerate(('loft', 'pipe', 'helix')):
    entry = next(
        item for item in CASES
        if item['operation'] == f'additive_{{family}}' and not item['with_base'])
    body, _base, _profiles, _spine, bindings = make_case(
        invalid_document, entry, index, open_profile=True)
    payload = Path(entry['path']).read_bytes()
    before_objects = tuple(invalid_document.Objects)
    before_group = tuple(body.Group)
    before_tip = body.Tip
    try:
        apply_partdesign_promotion_plan(
            payload,
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings,
        )
    except PartDesignPromotionRuleError:
        pass
    else:
        raise AssertionError(f'open {{family}} profile must fail')
    assert tuple(invalid_document.Objects) == before_objects
    assert tuple(body.Group) == before_group and body.Tip is before_tip
FreeCAD.closeDocument(invalid_document.Name)
print('REAL_PARTDESIGN_PROMOTION_BATCH_OK')
"""
    completed = subprocess.run(
        [str(runtime_python), "-c", code],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    assert "REAL_PARTDESIGN_PROMOTION_BATCH_OK" in completed.stdout
