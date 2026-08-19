"""Focused gates for the reviewed PartDesign residual family."""

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
from vibecad.intent_bridge.freecad_partdesign_residual_adapter import (
    FREECAD_PARTDESIGN_RESIDUAL_ADAPTER_DESCRIPTOR,
    RESIDUAL_ANGLE_ROLE_TERM,
    RESIDUAL_ANGLE_TYPE_TERM,
    RESIDUAL_AXIS_RESULT_ROLE_TERM,
    RESIDUAL_AXIS_ROLE_TERM,
    RESIDUAL_AXIS_TYPE_TERM,
    RESIDUAL_BASE_ROLE_TERM,
    RESIDUAL_CANONICAL_JSON_TERM,
    RESIDUAL_CIRCULAR_PROFILE_RESULT_ROLE_TERM,
    RESIDUAL_CIRCULAR_PROFILE_TYPE_TERM,
    RESIDUAL_CLOSED_PROFILE_RESULT_ROLE_TERM,
    RESIDUAL_CLOSED_PROFILE_TYPE_TERM,
    RESIDUAL_DEPTH_ROLE_TERM,
    RESIDUAL_DIAMETER_ROLE_TERM,
    RESIDUAL_EXTENT_ROLE_TERM,
    RESIDUAL_EXTENT_TYPE_TERM,
    RESIDUAL_HORIZONTAL_AXIS_LOCATOR_TERM,
    RESIDUAL_LENGTH_TYPE_TERM,
    RESIDUAL_OPERATION_SPECS,
    RESIDUAL_OPERATION_TERMS,
    RESIDUAL_PFG_TERMS,
    RESIDUAL_PLACEMENT_ROLE_TERM,
    RESIDUAL_PLACEMENT_TYPE_TERM,
    RESIDUAL_PROFILE_ROLE_TERM,
    RESIDUAL_REQUEST_TERMS,
    RESIDUAL_SOLID_RESULT_ROLE_TERM,
    RESIDUAL_SOLID_TYPE_TERM,
    RESIDUAL_STRUCTURE_TERM,
    RESIDUAL_VERTICAL_AXIS_LOCATOR_TERM,
    FreeCADPartDesignResidualAdapter,
    build_partdesign_residual_capability_document,
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
    FeatureReferenceBindingV2,
    FeatureResultV2,
    ParametricFeatureGraphV2,
    SemanticReferenceScope,
    SemanticReferenceV2,
    SemanticTermRefV2,
    TermTypedValueV2,
)
from vibecad.parametric.freecad_partdesign_residual_rules import (
    MAX_PARTDESIGN_RESIDUAL_PLAN_BYTES,
    HoleExtent,
    PartDesignResidualOperation,
    PartDesignResidualRuleError,
    RevolutionAxis,
    decode_partdesign_residual_backend_plan,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pfg_term(term_ref_id: str, term_id: str) -> SemanticTermRefV2:
    return SemanticTermRefV2(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.residual-test-source",
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
) -> FeatureInputPortV2:
    return FeatureInputPortV2(
        port_id=port_id,
        semantic_role_term_ref_id=role.term_ref_id,
        value_type_term_ref_id=value_type.term_ref_id,
        minimum_cardinality=minimum,
        maximum_cardinality=maximum,
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
            encoding_term_ref_id=RESIDUAL_CANONICAL_JSON_TERM.term_ref_id,
            value=value,
        ),
    )


def _source_node(
    node_id: str,
    results: tuple[FeatureResultV2, ...],
) -> FeatureNodeV2:
    return FeatureNodeV2(
        node_id=node_id,
        body_id="body_main",
        name=f"Untrusted native-looking source {node_id}",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=SOURCE_TERMS[0].term_ref_id,
            family_term_ref_id=SOURCE_TERMS[1].term_ref_id,
            operation_term_ref_id=SOURCE_TERMS[2].term_ref_id,
        ),
        results=results,
    )


def _graph(
    operation: PartDesignResidualOperation,
    *,
    extent: HoleExtent = HoleExtent.DIMENSION,
    diameter_mm: float = 6.0,
    revolution_base: bool = False,
    revolution_axis: RevolutionAxis = RevolutionAxis.HORIZONTAL,
    operation_definition: str | None = None,
) -> ParametricFeatureGraphV2:
    operation_terms = next(
        item for item in RESIDUAL_OPERATION_TERMS if item.operation is operation
    )
    static_terms = list(RESIDUAL_PFG_TERMS)
    if operation_definition is not None:
        index = static_terms.index(operation_terms.operation_term)
        static_terms[index] = dataclasses.replace(
            operation_terms.operation_term,
            term_definition_sha256=operation_definition,
        )
    nodes: list[FeatureNodeV2] = []
    ports: list[FeatureInputPortV2] = []
    dependencies: list[FeatureDependencyV2] = []
    references: list[SemanticReferenceV2] = []
    reference_bindings: list[FeatureReferenceBindingV2] = []
    parameters: list[DesignParameterV2] = []
    parameter_bindings: list[FeatureParameterBindingV2] = []

    def add_parameter(
        kind: str,
        role: SemanticTermRefV2,
        value_type: SemanticTermRefV2,
        value: object,
        *,
        minimum: int = 1,
    ) -> None:
        ports.append(
            _port(f"port_{kind}", role, value_type, minimum=minimum, maximum=1)
        )
        if minimum or value is not None:
            parameters.append(_parameter(kind, role, value_type, value))
            parameter_bindings.append(
                FeatureParameterBindingV2(
                    binding_id=f"binding_{kind}",
                    port_id=f"port_{kind}",
                    parameter_id=f"parameter_{kind}",
                )
            )

    if operation is PartDesignResidualOperation.HOLE or (
        operation is PartDesignResidualOperation.REVOLUTION and revolution_base
    ):
        base = _source_node(
            "node_base",
            (
                FeatureResultV2(
                    result_id="result_base",
                    semantic_role_term_ref_id=RESIDUAL_SOLID_RESULT_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=RESIDUAL_SOLID_TYPE_TERM.term_ref_id,
                ),
            ),
        )
        nodes.append(base)
        ports.append(
            _port(
                "port_base",
                RESIDUAL_BASE_ROLE_TERM,
                RESIDUAL_SOLID_TYPE_TERM,
                minimum=(
                    0 if operation is PartDesignResidualOperation.REVOLUTION else 1
                ),
            )
        )
        dependencies.append(
            FeatureDependencyV2(
                dependency_id="dependency_base",
                port_id="port_base",
                upstream_node_id=base.node_id,
                upstream_result_id="result_base",
            )
        )
    elif operation is PartDesignResidualOperation.REVOLUTION:
        ports.append(
            _port(
                "port_base",
                RESIDUAL_BASE_ROLE_TERM,
                RESIDUAL_SOLID_TYPE_TERM,
                minimum=0,
            )
        )
    if operation is PartDesignResidualOperation.HOLE:
        profile = _source_node(
            "node_profile",
            (
                FeatureResultV2(
                    result_id="result_profile",
                    semantic_role_term_ref_id=(
                        RESIDUAL_CIRCULAR_PROFILE_RESULT_ROLE_TERM.term_ref_id
                    ),
                    value_type_term_ref_id=RESIDUAL_CIRCULAR_PROFILE_TYPE_TERM.term_ref_id,
                ),
            ),
        )
        nodes.append(profile)
        ports.append(
            _port(
                "port_profile",
                RESIDUAL_PROFILE_ROLE_TERM,
                RESIDUAL_CIRCULAR_PROFILE_TYPE_TERM,
            )
        )
        dependencies.append(
            FeatureDependencyV2(
                dependency_id="dependency_profile",
                port_id="port_profile",
                upstream_node_id=profile.node_id,
                upstream_result_id="result_profile",
            )
        )
        add_parameter("extent", RESIDUAL_EXTENT_ROLE_TERM, RESIDUAL_EXTENT_TYPE_TERM, extent.value)
        add_parameter(
            "diameter",
            RESIDUAL_DIAMETER_ROLE_TERM,
            RESIDUAL_LENGTH_TYPE_TERM,
            diameter_mm,
        )
        add_parameter(
            "depth",
            RESIDUAL_DEPTH_ROLE_TERM,
            RESIDUAL_LENGTH_TYPE_TERM,
            4.0 if extent is HoleExtent.DIMENSION else None,
            minimum=0,
        )
    elif operation is PartDesignResidualOperation.REVOLUTION:
        profile = _source_node(
            "node_profile",
            (
                FeatureResultV2(
                    result_id="result_axis",
                    semantic_role_term_ref_id=RESIDUAL_AXIS_RESULT_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=RESIDUAL_AXIS_TYPE_TERM.term_ref_id,
                ),
                FeatureResultV2(
                    result_id="result_profile",
                    semantic_role_term_ref_id=(
                        RESIDUAL_CLOSED_PROFILE_RESULT_ROLE_TERM.term_ref_id
                    ),
                    value_type_term_ref_id=RESIDUAL_CLOSED_PROFILE_TYPE_TERM.term_ref_id,
                ),
            ),
        )
        nodes.append(profile)
        ports.append(
            _port(
                "port_profile",
                RESIDUAL_PROFILE_ROLE_TERM,
                RESIDUAL_CLOSED_PROFILE_TYPE_TERM,
            )
        )
        dependencies.append(
            FeatureDependencyV2(
                dependency_id="dependency_profile",
                port_id="port_profile",
                upstream_node_id=profile.node_id,
                upstream_result_id="result_profile",
            )
        )
        ports.append(_port("port_axis", RESIDUAL_AXIS_ROLE_TERM, RESIDUAL_AXIS_TYPE_TERM))
        reference = SemanticReferenceV2(
            reference_id="reference_axis",
            scope=SemanticReferenceScope.FEATURE,
            semantic_role_term_ref_id=RESIDUAL_AXIS_ROLE_TERM.term_ref_id,
            value_type_term_ref_id=RESIDUAL_AXIS_TYPE_TERM.term_ref_id,
            locator_term_ref_id=(
                RESIDUAL_HORIZONTAL_AXIS_LOCATOR_TERM.term_ref_id
                if revolution_axis is RevolutionAxis.HORIZONTAL
                else RESIDUAL_VERTICAL_AXIS_LOCATOR_TERM.term_ref_id
            ),
            source_node_id=profile.node_id,
            source_geometry_id="result_axis",
        )
        references.append(reference)
        reference_bindings.append(
            FeatureReferenceBindingV2(
                binding_id="binding_axis",
                port_id="port_axis",
                reference_id=reference.reference_id,
            )
        )
        add_parameter(
            "angle",
            RESIDUAL_ANGLE_ROLE_TERM,
            RESIDUAL_ANGLE_TYPE_TERM,
            270.0,
        )
    else:
        add_parameter(
            "placement",
            RESIDUAL_PLACEMENT_ROLE_TERM,
            RESIDUAL_PLACEMENT_TYPE_TERM,
            {
                "position_mm": [10.0, 20.0, 30.0],
                "axis": [0.0, 0.0, 1.0],
                "angle_degrees": 45.0,
            },
        )

    target = FeatureNodeV2(
        node_id="node_target",
        body_id="body_main",
        name="Untrusted graph mentions PartDesign::Hole but grants no authority",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=RESIDUAL_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=operation_terms.family.term_ref_id,
            operation_term_ref_id=operation_terms.operation_term.term_ref_id,
            input_ports=tuple(ports),
            dependencies=tuple(dependencies),
            references=tuple(reference_bindings),
            parameter_bindings=tuple(parameter_bindings),
        ),
        results=(
            FeatureResultV2(
                result_id="result_target",
                semantic_role_term_ref_id=operation_terms.result_role.term_ref_id,
                value_type_term_ref_id=operation_terms.result_type.term_ref_id,
            ),
        ),
    )
    nodes.append(target)
    return ParametricFeatureGraphV2(
        graph_id="graph_residual",
        name="PartDesign residual test graph",
        terms=tuple((*static_terms, *SOURCE_TERMS)),
        bodies=(FeatureBodyV2(body_id="body_main", name="Main Body"),),
        parameters=tuple(parameters),
        references=tuple(references),
        nodes=tuple(nodes),
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
        namespace="org.vibecad.residual-proof-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"proof:{term_id}"),
    )


RULE = _proof_term("rule_residual_target", "rule.residual-target-reviewed")
PREDICATE = _proof_term(
    "predicate_residual_target", "predicate.residual-target-reviewed"
)
ROLE_PREMISE = _proof_term("role_residual_candidate", "proof-role.residual-candidate")
ROLE_CONCLUSION = _proof_term(
    "role_residual_validated", "proof-role.residual-validated"
)
RESIDUAL_STRUCTURE_BRIDGE = _bridge_from_pfg(RESIDUAL_STRUCTURE_TERM)


class _ResidualEvaluator:
    def __init__(self) -> None:
        def signature(role: BridgeTermRef) -> RuleEndpointSignature:
            return RuleEndpointSignature(
                selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
                role_term=role,
                subject_type_term=RESIDUAL_STRUCTURE_BRIDGE,
            )

        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="partdesign_residual_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("partdesign-residual-target-evaluator-v1"),
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
            role_term_ref_id=(
                next(
                    item
                    for item in RESIDUAL_REQUEST_TERMS
                    if item.term_id == "document-role.parametric-intent"
                ).term_ref_id
            ),
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
            RESIDUAL_STRUCTURE_BRIDGE,
            next(
                item
                for item in RESIDUAL_REQUEST_TERMS
                if item.term_id == "document-role.parametric-intent"
            ),
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_residual_target",
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
                producer_id="residual_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("residual-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("upstream-residual-compile-request"),
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
    max_output_bytes: int = MAX_PARTDESIGN_RESIDUAL_PLAN_BYTES,
) -> tuple[BackendLoweringRequest, _Reader, TrustedRulePolicy]:
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = build_partdesign_residual_capability_document()
    policy = TrustedRulePolicy(evaluators=(_ResidualEvaluator(),))
    request = BackendLoweringRequest(
        adapter=FREECAD_PARTDESIGN_RESIDUAL_ADAPTER_DESCRIPTOR,
        terms=tuple(
            (*RESIDUAL_REQUEST_TERMS, RULE, PREDICATE, ROLE_PREMISE, ROLE_CONCLUSION)
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
    adapter: FreeCADPartDesignResidualAdapter,
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


@pytest.mark.parametrize(
    ("operation", "extent", "axis"),
    (
        (
            PartDesignResidualOperation.HOLE,
            HoleExtent.DIMENSION,
            RevolutionAxis.HORIZONTAL,
        ),
        (
            PartDesignResidualOperation.HOLE,
            HoleExtent.THROUGH_ALL,
            RevolutionAxis.HORIZONTAL,
        ),
        (
            PartDesignResidualOperation.REVOLUTION,
            HoleExtent.DIMENSION,
            RevolutionAxis.HORIZONTAL,
        ),
        (
            PartDesignResidualOperation.REVOLUTION,
            HoleExtent.DIMENSION,
            RevolutionAxis.VERTICAL,
        ),
        (
            PartDesignResidualOperation.COORDINATE_SYSTEM,
            HoleExtent.DIMENSION,
            RevolutionAxis.HORIZONTAL,
        ),
    ),
)
def test_shared_adapter_lowers_narrow_family_deterministically(
    operation: PartDesignResidualOperation,
    extent: HoleExtent,
    axis: RevolutionAxis,
) -> None:
    request, reader, policy = _request(
        _graph(operation, extent=extent, revolution_axis=axis)
    )
    sink = _MemoryPlanSink()
    adapter = FreeCADPartDesignResidualAdapter(sink)
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    repeated_result, repeated_receipt = _lower(adapter, request, reader, policy)
    repeated_plan, repeated_payload = adapter.read_plan(repeated_receipt)

    assert plan.operation is operation
    assert result.plan_document.document_digest == plan.plan_sha256
    assert result.plan_document.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert repeated_result == result and repeated_receipt == receipt
    assert repeated_plan == plan and repeated_payload == payload
    assert result.supported_subjects == (_subject(),)
    assert len(sink.items) == 1
    assert not adapter.executable and not plan.executable
    assert not adapter.grants_execution_authority and not receipt.grants_execution_authority
    assert b"PartDesign::" not in payload
    if operation is PartDesignResidualOperation.HOLE:
        assert plan.hole_extent is extent
        assert (plan.depth_mm is None) is (extent is HoleExtent.THROUGH_ALL)
    elif operation is PartDesignResidualOperation.REVOLUTION:
        assert plan.revolution_axis is axis
        assert plan.axis_result_id == "result_axis"
    else:
        assert plan.placement is not None and plan.base is None and plan.profile is None


def test_full_semantic_identity_unknown_and_atomic_failure_are_inert() -> None:
    graph = _graph(
        PartDesignResidualOperation.HOLE,
        operation_definition=_sha("substituted operation definition"),
    )
    request, reader, policy = _request(graph)
    sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartDesignResidualAdapter(sink), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    request, reader, policy = _request(_graph(PartDesignResidualOperation.REVOLUTION))
    sink = _MemoryPlanSink(fail=True)
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartDesignResidualAdapter(sink), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert sink.items == {}


def test_numeric_n_n_plus_one_and_plan_tamper_gates() -> None:
    request, reader, policy = _request(
        _graph(PartDesignResidualOperation.HOLE, diameter_mm=1_000_000.0)
    )
    adapter = FreeCADPartDesignResidualAdapter(_MemoryPlanSink())
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    assert plan.diameter_mm == 1_000_000.0

    request, reader, policy = _request(
        _graph(PartDesignResidualOperation.HOLE, diameter_mm=1_000_000.1)
    )
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartDesignResidualAdapter(_MemoryPlanSink()), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION

    with pytest.raises(PartDesignResidualRuleError):
        decode_partdesign_residual_backend_plan(
            payload + b" ",
            expected_content_sha256=result.plan_document.content_sha256,
            expected_plan_sha256=plan.plan_sha256,
        )
    duplicate = payload.replace(b'{"authority":', b'{"authority":"none","authority":', 1)
    with pytest.raises(PartDesignResidualRuleError):
        decode_partdesign_residual_backend_plan(duplicate)

    request, reader, policy = _request(
        _graph(PartDesignResidualOperation.HOLE), max_output_bytes=1
    )
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartDesignResidualAdapter(_MemoryPlanSink()), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED


def test_manifest_has_exact_three_reviewed_specs() -> None:
    assert tuple(item.operation_id for item in RESIDUAL_OPERATION_SPECS) == (
        "hole",
        "revolution",
        "coordinate_system",
    )
    assert tuple(item.native_type_id for item in RESIDUAL_OPERATION_SPECS) == (
        "PartDesign::Hole",
        "PartDesign::Revolution",
        "PartDesign::CoordinateSystem",
    )


def test_revolution_optional_base_is_explicit_and_content_bound() -> None:
    request, reader, policy = _request(
        _graph(PartDesignResidualOperation.REVOLUTION, revolution_base=True)
    )
    adapter = FreeCADPartDesignResidualAdapter(_MemoryPlanSink())
    _, receipt = _lower(adapter, request, reader, policy)
    plan, _ = adapter.read_plan(receipt)
    assert plan.base is not None
    assert plan.base.node_id == "node_base" and plan.base.result_id == "result_base"


@pytest.mark.slow
def test_real_freecad_residual_batch_create_edit_reopen_and_rollback(
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

    case_specs = (
        (
            PartDesignResidualOperation.HOLE,
            HoleExtent.DIMENSION,
            RevolutionAxis.HORIZONTAL,
        ),
        (
            PartDesignResidualOperation.HOLE,
            HoleExtent.THROUGH_ALL,
            RevolutionAxis.HORIZONTAL,
        ),
        (
            PartDesignResidualOperation.REVOLUTION,
            HoleExtent.DIMENSION,
            RevolutionAxis.HORIZONTAL,
        ),
        (
            PartDesignResidualOperation.REVOLUTION,
            HoleExtent.DIMENSION,
            RevolutionAxis.VERTICAL,
        ),
        (
            PartDesignResidualOperation.COORDINATE_SYSTEM,
            HoleExtent.DIMENSION,
            RevolutionAxis.HORIZONTAL,
        ),
    )
    cases = []
    for index, (operation, extent, axis) in enumerate(case_specs):
        request, reader, policy = _request(
            _graph(operation, extent=extent, revolution_axis=axis)
        )
        adapter = FreeCADPartDesignResidualAdapter(_MemoryPlanSink())
        result, receipt = _lower(adapter, request, reader, policy)
        plan, payload = adapter.read_plan(receipt)
        plan_path = tmp_path / f"{index}_{operation.value}.json"
        plan_path.write_bytes(payload)
        cases.append(
            {
                "operation": operation.value,
                "extent": extent.value,
                "path": str(plan_path),
                "content_sha256": result.plan_document.content_sha256,
                "plan_sha256": result.plan_document.document_digest,
                "body_id": plan.body_id,
                "base": None
                if plan.base is None
                else (plan.base.node_id, plan.base.result_id),
                "profile": None
                if plan.profile is None
                else (plan.profile.node_id, plan.profile.result_id),
            }
        )
    source_root = Path(__file__).parents[1] / "src"
    output_root = tmp_path / "freecad-residual"
    output_root.mkdir()
    code = f"""
import os, sys
sys.path.insert(0, os.path.join(sys.prefix, 'lib'))
sys.path.insert(0, {str(source_root)!r})
from pathlib import Path
import FreeCAD, Part, Sketcher
from vibecad.parametric.freecad_partdesign_residual_rules import (
    AuthenticatedResidualObject,
    PartDesignResidualExecutionBindings,
    PartDesignResidualRuleError,
    apply_partdesign_residual_plan,
)

CASES = {cases!r}
OUTPUT_ROOT = Path({str(output_root)!r})
TYPE_IDS = {{
    'hole': 'PartDesign::Hole',
    'revolution': 'PartDesign::Revolution',
    'coordinate_system': 'PartDesign::CoordinateSystem',
}}

def add_rectangle(sketch, x0, y0, x1, y1):
    points = (
        FreeCAD.Vector(x0, y0, 0), FreeCAD.Vector(x1, y0, 0),
        FreeCAD.Vector(x1, y1, 0), FreeCAD.Vector(x0, y1, 0),
    )
    for index, start in enumerate(points):
        sketch.addGeometry(Part.LineSegment(start, points[(index + 1) % 4]), False)
    for index in range(4):
        sketch.addConstraint(
            Sketcher.Constraint('Coincident', index, 2, (index + 1) % 4, 1))

def snapshot(document, body):
    group = tuple(body.Group)
    return (
        tuple(document.Objects), group, body.Tip,
        tuple(bool(item.Visibility) for item in group), bool(document.HasPendingTransaction),
    )

def same_snapshot(document, body, before):
    return (
        tuple(document.Objects) == before[0]
        and tuple(body.Group) == before[1]
        and body.Tip is before[2]
        and tuple(bool(item.Visibility) for item in tuple(body.Group)) == before[3]
        and bool(document.HasPendingTransaction) == before[4]
    )

def fixture(entry, *, invalid_hole=False):
    document = FreeCAD.newDocument('Residual_' + entry['operation'] + entry['extent'])
    document.UndoMode = 1
    body = document.addObject('PartDesign::Body', 'Body')
    base = profile = None
    if entry['operation'] == 'hole':
        base = body.newObject('PartDesign::AdditiveBox', 'Base')
        base.Length, base.Width, base.Height = 30.0, 20.0, 10.0
        document.recompute()
        profile = body.newObject('Sketcher::SketchObject', 'HoleProfile')
        profile.AttachmentSupport = [(base, ['Face6'])]
        profile.MapMode = 'FlatFace'
        center = FreeCAD.Vector(100.0, 100.0, 0) if invalid_hole else FreeCAD.Vector(15, 10, 0)
        profile.addGeometry(Part.Circle(center, FreeCAD.Vector(0, 0, 1), 3.0), False)
        document.recompute()
    elif entry['operation'] == 'revolution':
        profile = body.newObject('Sketcher::SketchObject', 'RevolutionProfile')
        add_rectangle(profile, 4.0, 2.0, 8.0, 6.0)
        document.recompute()
    return document, body, base, profile

def bindings_for(entry, document, body, base, profile):
    return PartDesignResidualExecutionBindings(
        document=document,
        body=body,
        body_id=entry['body_id'],
        base=None if base is None else AuthenticatedResidualObject(
            object=base, node_id=entry['base'][0], result_id=entry['base'][1]),
        profile=None if profile is None else AuthenticatedResidualObject(
            object=profile, node_id=entry['profile'][0], result_id=entry['profile'][1]),
    )

persisted = []
for index, entry in enumerate(CASES):
    document, body, base, profile = fixture(entry)
    bindings = bindings_for(entry, document, body, base, profile)
    payload = Path(entry['path']).read_bytes()
    before = snapshot(document, body)
    try:
        apply_partdesign_residual_plan(
            payload + b' ',
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings,
        )
        raise AssertionError('tamper accepted')
    except PartDesignResidualRuleError:
        assert same_snapshot(document, body, before)
    receipt = apply_partdesign_residual_plan(
        payload,
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=bindings,
    )
    feature = document.getObject(receipt.object_name)
    assert feature.TypeId == TYPE_IDS[entry['operation']] and feature.isValid()
    before_edit = None if entry['operation'] == 'coordinate_system' else float(feature.Shape.Volume)
    if entry['operation'] == 'hole':
        feature.Diameter = 8.0
    elif entry['operation'] == 'revolution':
        feature.Angle = 180.0
    else:
        feature.Placement = FreeCAD.Placement(
            FreeCAD.Vector(12, 22, 32), FreeCAD.Rotation(FreeCAD.Vector(1, 0, 0), 30))
    document.recompute()
    if before_edit is not None:
        assert abs(float(feature.Shape.Volume) - before_edit) > 1e-6
    path = OUTPUT_ROOT / f'{{index}}.FCStd'
    document.saveAs(str(path))
    persisted.append((path, receipt.object_name, entry['operation']))
    FreeCAD.closeDocument(document.Name)

for path, object_name, operation in persisted:
    reopened = FreeCAD.openDocument(str(path))
    feature = reopened.getObject(object_name)
    assert feature is not None and feature.TypeId == TYPE_IDS[operation] and feature.isValid()
    if operation == 'hole':
        assert abs(float(feature.Diameter) - 8.0) < 1e-9
    elif operation == 'revolution':
        assert abs(float(feature.Angle) - 180.0) < 1e-9
    else:
        assert abs(float(feature.Placement.Base.x) - 12.0) < 1e-9
    FreeCAD.closeDocument(reopened.Name)

# A valid authenticated profile that lies outside the supported base passes
# preconditions, then fails post-mutation solid conformance.  The transaction
# must leave no native feature or visibility/tip residue.
entry = CASES[0]
document, body, base, profile = fixture(entry, invalid_hole=True)
bindings = bindings_for(entry, document, body, base, profile)
before = snapshot(document, body)
try:
    apply_partdesign_residual_plan(
        Path(entry['path']).read_bytes(),
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=bindings,
    )
    raise AssertionError('late invalid geometry accepted')
except PartDesignResidualRuleError:
    assert same_snapshot(document, body, before)
FreeCAD.closeDocument(document.Name)
print('RESIDUAL_BATCH_OK')
"""
    completed = subprocess.run(
        [str(runtime_python), "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "RESIDUAL_BATCH_OK" in completed.stdout
