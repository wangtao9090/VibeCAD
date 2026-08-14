"""Focused PFGv2 lowering gates for the PartDesign reference family."""

from __future__ import annotations

import dataclasses
import hashlib

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
from vibecad.intent_bridge.freecad_partdesign_reference_adapter import (
    FREECAD_REFERENCE_ADAPTER_DESCRIPTOR,
    REFERENCE_INTENT_DOCUMENT_ROLE_TERM,
    REFERENCE_LOCATOR_TERM,
    REFERENCE_OPERATION_TERMS,
    REFERENCE_PFG_TERMS,
    REFERENCE_REQUEST_TERMS,
    REFERENCE_RESULT_ROLE_TERM,
    REFERENCE_RESULT_TYPE_TERMS,
    REFERENCE_STRUCTURE_TERM,
    REFERENCE_SUPPORT_ROLE_TERM,
    REFERENCE_SUPPORT_TYPE_TERM,
    FreeCADPartDesignReferenceAdapter,
    build_reference_capability_document,
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
    FeatureBodyV2,
    FeatureGraphResultV2,
    FeatureInputPortV2,
    FeatureIntentV2,
    FeatureNodeV2,
    FeatureReferenceBindingV2,
    FeatureResultV2,
    ParametricFeatureGraphV2,
    SemanticReferenceScope,
    SemanticReferenceV2,
)
from vibecad.parametric.freecad_partdesign_reference_rules import (
    MAX_REFERENCE_PLAN_BYTES,
    PartDesignReferenceKind,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bridge_from_pfg(term) -> BridgeTermRef:
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
        namespace="org.vibecad.reference-proof-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"proof:{term_id}"),
    )


RULE = _proof_term("rule_reference_target", "rule.reference-target-reviewed")
PREDICATE = _proof_term("predicate_reference_target", "predicate.reference-target-reviewed")
ROLE_PREMISE = _proof_term("role_reference_candidate", "proof-role.reference-candidate")
ROLE_CONCLUSION = _proof_term("role_reference_validated", "proof-role.reference-validated")
REFERENCE_STRUCTURE_BRIDGE = _bridge_from_pfg(REFERENCE_STRUCTURE_TERM)


def _graph(
    kind: PartDesignReferenceKind,
    *,
    operation_definition: str | None = None,
    locator_definition: str | None = None,
) -> ParametricFeatureGraphV2:
    terms = list(REFERENCE_PFG_TERMS)
    operation = REFERENCE_OPERATION_TERMS[kind]
    locator = REFERENCE_LOCATOR_TERM
    if operation_definition is not None:
        replacement = dataclasses.replace(operation, term_definition_sha256=operation_definition)
        terms[terms.index(operation)] = replacement
        operation = replacement
    if locator_definition is not None:
        replacement = dataclasses.replace(locator, term_definition_sha256=locator_definition)
        terms[terms.index(locator)] = replacement
        locator = replacement
    node_id = f"node_{kind.value}"
    result_id = f"result_{kind.value}"
    reference = SemanticReferenceV2(
        reference_id="reference_support",
        scope=SemanticReferenceScope.EXTERNAL,
        semantic_role_term_ref_id=REFERENCE_SUPPORT_ROLE_TERM.term_ref_id,
        value_type_term_ref_id=REFERENCE_SUPPORT_TYPE_TERM.term_ref_id,
        locator_term_ref_id=locator.term_ref_id,
        source_content_sha256="5" * 64,
    )
    node = FeatureNodeV2(
        node_id=node_id,
        body_id="body_main",
        name=f"Untrusted {kind.value}",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=REFERENCE_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id="family_partdesign_reference",
            operation_term_ref_id=operation.term_ref_id,
            input_ports=(
                FeatureInputPortV2(
                    port_id="port_support",
                    semantic_role_term_ref_id=REFERENCE_SUPPORT_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=REFERENCE_SUPPORT_TYPE_TERM.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
            ),
            references=(
                FeatureReferenceBindingV2(
                    binding_id="binding_support",
                    port_id="port_support",
                    reference_id=reference.reference_id,
                ),
            ),
        ),
        results=(
            FeatureResultV2(
                result_id=result_id,
                semantic_role_term_ref_id=REFERENCE_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=REFERENCE_RESULT_TYPE_TERMS[kind].term_ref_id,
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id=f"graph_{kind.value}",
        name="Reference graph",
        terms=tuple(terms),
        bodies=(FeatureBodyV2(body_id="body_main", name="Main"),),
        parameters=(),
        references=(reference,),
        nodes=(node,),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection_reference",
                node_id=node_id,
                result_id=result_id,
            ),
        ),
    )


def _subject(kind: PartDesignReferenceKind) -> SubjectRef:
    return SubjectRef(
        artifact_id="artifact_pfg",
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id=f"node_{kind.value}",
    )


class _ReferenceEvaluator:
    def __init__(self, kind: PartDesignReferenceKind) -> None:
        signature = lambda role: RuleEndpointSignature(  # noqa: E731
            selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
            role_term=role,
            subject_type_term=REFERENCE_STRUCTURE_BRIDGE,
        )
        self._kind = kind
        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="reference_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("reference-target-evaluator-v1"),
            rule_term=RULE,
            predicate_term=PREDICATE,
            premises=(signature(ROLE_PREMISE),),
            conclusions=(signature(ROLE_CONCLUSION),),
        )

    @property
    def descriptor(self) -> TrustedRuleEvaluatorDescriptor:
        return self._descriptor

    def validate(self, evaluation: TrustedRuleEvaluation) -> None:
        expected = f"node_{self._kind.value}"
        if (
            len(evaluation.documents) != 1
            or evaluation.premises[0].subject.selector_id != expected
            or evaluation.conclusions[0].subject.selector_id != expected
        ):
            raise IntentBridgeError(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/reference")


def _intent_document(graph: ParametricFeatureGraphV2) -> tuple[DocumentRef, bytes]:
    payload = graph.canonical_bytes
    return (
        DocumentRef(
            artifact_id="artifact_pfg",
            role_term_ref_id=REFERENCE_INTENT_DOCUMENT_ROLE_TERM.term_ref_id,
            schema_term_ref_id=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM.term_ref_id,
            document_id=graph.graph_id,
            document_digest=graph.graph_sha256,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
        ),
        payload,
    )


def _proof(
    policy: TrustedRulePolicy,
    intent_document: DocumentRef,
    kind: PartDesignReferenceKind,
) -> ProofBundle:
    return ProofBundle(
        terms=(
            RULE,
            PREDICATE,
            ROLE_PREMISE,
            ROLE_CONCLUSION,
            REFERENCE_STRUCTURE_BRIDGE,
            REFERENCE_INTENT_DOCUMENT_ROLE_TERM,
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(intent_document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_reference_target",
                predicate_term_ref_id=PREDICATE.term_ref_id,
                rule_term_ref_id=RULE.term_ref_id,
                premises=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=ROLE_PREMISE.term_ref_id,
                        subject=_subject(kind),
                    ),
                ),
                conclusions=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=ROLE_CONCLUSION.term_ref_id,
                        subject=_subject(kind),
                    ),
                ),
            ),
        ),
        producer=ProducerBinding(
            descriptor=ProducerDescriptor(
                producer_id="reference_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("reference-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("upstream-reference-request"),
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
    def __init__(self, *, fail: bool = False, corrupt_readback: bool = False) -> None:
        self.fail = fail
        self.corrupt_readback = corrupt_readback
        self.items: dict[str, tuple[DocumentRef, bytes]] = {}
        self.publish_calls = 0

    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        self.publish_calls += 1
        if self.fail:
            raise RuntimeError("not reflected")
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
        stored, payload = self.items[document.artifact_id]
        if stored != document or len(payload) > maximum_bytes:
            raise RuntimeError("bad read")
        return payload + b"x" if self.corrupt_readback else payload


def _request(
    graph: ParametricFeatureGraphV2,
    kind: PartDesignReferenceKind,
    *,
    max_output_bytes: int = MAX_REFERENCE_PLAN_BYTES,
) -> tuple[BackendLoweringRequest, _Reader, TrustedRulePolicy]:
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = build_reference_capability_document()
    policy = TrustedRulePolicy(evaluators=(_ReferenceEvaluator(kind),))
    proof = _proof(policy, intent_document, kind)
    request = BackendLoweringRequest(
        adapter=FREECAD_REFERENCE_ADAPTER_DESCRIPTOR,
        terms=tuple(
            (
                *REFERENCE_REQUEST_TERMS,
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
    adapter: FreeCADPartDesignReferenceAdapter,
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


@pytest.mark.parametrize("kind", tuple(PartDesignReferenceKind))
def test_adapter_lowers_all_reference_kinds_atomically_and_idempotently(
    kind: PartDesignReferenceKind,
) -> None:
    request, reader, policy = _request(_graph(kind), kind)
    sink = _MemoryPlanSink()
    adapter = FreeCADPartDesignReferenceAdapter(sink)
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    repeated, repeated_receipt = _lower(adapter, request, reader, policy)

    assert isinstance(sink, PlanSink)
    assert isinstance(adapter, IntentBackendAdapter)
    assert result.disposition is BridgeDisposition.COMPLETE
    assert result.supported_subjects == (_subject(kind),)
    assert plan.kind is kind
    assert plan.support_reference_id == "reference_support"
    assert plan.support_reference_sha256 == "5" * 64
    assert plan.lowering_request_sha256 == request.request_digest
    assert payload == plan.canonical_bytes
    assert result.plan_document.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert result.plan_document.document_digest == plan.plan_sha256
    assert adapter.executable is False and adapter.grants_execution_authority is False
    assert receipt.executable is False and receipt.grants_execution_authority is False
    assert repeated == result and repeated_receipt == receipt
    assert sink.publish_calls == 2 and len(sink.items) == 1
    assert "PartDesign::" not in payload.decode("ascii")


def test_adapter_rejects_semantic_rebinding_capability_tamper_and_sink_failure() -> None:
    kind = PartDesignReferenceKind.DATUM_PLANE
    wrong_request, wrong_reader, wrong_policy = _request(
        _graph(kind, operation_definition="f" * 64), kind
    )
    sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as semantic_error:
        _lower(
            FreeCADPartDesignReferenceAdapter(sink),
            wrong_request,
            wrong_reader,
            wrong_policy,
        )
    assert semantic_error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    request, reader, policy = _request(_graph(kind), kind)
    reader.payloads[request.capability_artifact_ids[0]] += b" "
    with pytest.raises(IntentBridgeError) as capability_error:
        _lower(FreeCADPartDesignReferenceAdapter(sink), request, reader, policy)
    assert capability_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert sink.items == {}

    request, reader, policy = _request(_graph(kind), kind)
    failed = _MemoryPlanSink(fail=True)
    with pytest.raises(IntentBridgeError) as sink_error:
        _lower(FreeCADPartDesignReferenceAdapter(failed), request, reader, policy)
    assert sink_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert failed.items == {}
    assert "not reflected" not in str(sink_error.value)


def test_adapter_rejects_locator_rebinding_and_corrupt_plan_readback() -> None:
    kind = PartDesignReferenceKind.SUBSHAPE_BINDER
    request, reader, policy = _request(_graph(kind, locator_definition="e" * 64), kind)
    with pytest.raises(IntentBridgeError) as locator_error:
        _lower(
            FreeCADPartDesignReferenceAdapter(_MemoryPlanSink()),
            request,
            reader,
            policy,
        )
    assert locator_error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION

    request, reader, policy = _request(_graph(kind), kind)
    sink = _MemoryPlanSink(corrupt_readback=True)
    _result, receipt = _lower(FreeCADPartDesignReferenceAdapter(sink), request, reader, policy)
    with pytest.raises(IntentBridgeError) as read_error:
        FreeCADPartDesignReferenceAdapter(sink).read_plan(receipt)
    assert read_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE


def test_adapter_enforces_exact_output_budget_before_publication() -> None:
    kind = PartDesignReferenceKind.SHAPE_BINDER
    request, reader, policy = _request(_graph(kind), kind)
    result, _receipt = _lower(
        FreeCADPartDesignReferenceAdapter(_MemoryPlanSink()),
        request,
        reader,
        policy,
    )
    size = result.plan_document.size_bytes
    exact_request, exact_reader, exact_policy = _request(_graph(kind), kind, max_output_bytes=size)
    exact_result, _ = _lower(
        FreeCADPartDesignReferenceAdapter(_MemoryPlanSink()),
        exact_request,
        exact_reader,
        exact_policy,
    )
    assert exact_result.plan_document.size_bytes == size

    small_request, small_reader, small_policy = _request(
        _graph(kind), kind, max_output_bytes=size - 1
    )
    small_sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as error:
        _lower(
            FreeCADPartDesignReferenceAdapter(small_sink),
            small_request,
            small_reader,
            small_policy,
        )
    assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert small_sink.items == {}
