"""Focused tests for host-injected semantic proof evaluators."""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from vibecad.intent_bridge.contracts import (
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
from vibecad.intent_bridge.ports import (
    GraphCodecDescriptor,
    ResolvedSubject,
    TrustedCodecRegistry,
    ValidatedDocument,
    validate_proof_bundle,
)
from vibecad.intent_bridge.trusted_proof_policy import (
    MAX_TRUSTED_RULE_EVALUATORS,
    RuleEndpointSignature,
    TrustedRuleEvaluation,
    TrustedRuleEvaluatorDescriptor,
    TrustedRulePolicy,
)


def _term(
    term_ref_id: str,
    term_id: str,
    *,
    definition: str,
) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.intent-policy-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=definition * 64,
    )


def _semantic_alias(term: BridgeTermRef, term_ref_id: str) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


RULE = _term("rule_local", "rule.copy-measured-shape", definition="1")
PREDICATE = _term("predicate_local", "predicate.derived-from", definition="2")
ROLE_SOURCE = _term("role_source", "role.source", definition="3")
ROLE_RESULT = _term("role_result", "role.result", definition="4")
TYPE_SOURCE = _term("type_source", "type.measured-shape", definition="5")
TYPE_RESULT = _term("type_result", "type.intent-shape", definition="6")
ROLE_DOCUMENT = _term("role_document", "role.document", definition="7")
SCHEMA = _term("schema_graph", "schema.test-graph", definition="8")
SELECTOR = _term("selector_node", "selector.node", definition="9")


def _descriptor(
    *,
    rule: BridgeTermRef = RULE,
    predicate: BridgeTermRef = PREDICATE,
    source_role: BridgeTermRef = ROLE_SOURCE,
) -> TrustedRuleEvaluatorDescriptor:
    return TrustedRuleEvaluatorDescriptor(
        evaluator_id=f"evaluator_{rule.term_ref_id}",
        evaluator_version="1.0.0",
        evaluator_contract_sha256="a" * 64,
        rule_term=_semantic_alias(rule, f"descriptor_{rule.term_ref_id}"),
        predicate_term=_semantic_alias(predicate, f"descriptor_{predicate.term_ref_id}"),
        premises=(
            RuleEndpointSignature(
                selector_kind_term=_semantic_alias(
                    SELECTOR,
                    "descriptor_selector_source",
                ),
                role_term=_semantic_alias(source_role, "descriptor_role_source"),
                subject_type_term=_semantic_alias(TYPE_SOURCE, "descriptor_type_source"),
            ),
        ),
        conclusions=(
            RuleEndpointSignature(
                selector_kind_term=_semantic_alias(
                    SELECTOR,
                    "descriptor_selector_result",
                ),
                role_term=_semantic_alias(ROLE_RESULT, "descriptor_role_result"),
                subject_type_term=_semantic_alias(TYPE_RESULT, "descriptor_type_result"),
            ),
        ),
    )


class _Evaluator:
    def __init__(
        self,
        descriptor: TrustedRuleEvaluatorDescriptor | None = None,
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.current_descriptor = descriptor or _descriptor()
        self.failure = failure
        self.evaluations: list[TrustedRuleEvaluation] = []

    @property
    def descriptor(self) -> TrustedRuleEvaluatorDescriptor:
        return self.current_descriptor

    def validate(self, evaluation: TrustedRuleEvaluation) -> None:
        self.evaluations.append(evaluation)
        if self.failure is not None:
            raise self.failure
        source = json.loads(evaluation.documents[0].payload)["subjects"]
        if source != ["measured", "intent"]:
            raise IntentBridgeError(
                IntentBridgeErrorCode.AUTHORITY_VIOLATION,
                "/semantic_fact",
            )


def _payload() -> bytes:
    return b'{"subjects":["measured","intent"]}'


def _document(payload: bytes | None = None) -> DocumentRef:
    raw = payload or _payload()
    return DocumentRef(
        artifact_id="graph_artifact",
        role_term_ref_id=ROLE_DOCUMENT.term_ref_id,
        schema_term_ref_id=SCHEMA.term_ref_id,
        document_id="graph_document",
        document_digest=hashlib.sha256(b"test-graph\0" + raw).hexdigest(),
        content_sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        media_type="application/json",
    )


def _subject(selector_id: str) -> SubjectRef:
    return SubjectRef(
        artifact_id="graph_artifact",
        selector_kind_term_ref_id=SELECTOR.term_ref_id,
        selector_id=selector_id,
    )


def _bundle(
    policy: TrustedRulePolicy,
    *,
    rule: BridgeTermRef = RULE,
    predicate: BridgeTermRef = PREDICATE,
    source_role: BridgeTermRef = ROLE_SOURCE,
) -> ProofBundle:
    terms = (
        rule,
        predicate,
        source_role,
        ROLE_RESULT,
        TYPE_SOURCE,
        TYPE_RESULT,
        ROLE_DOCUMENT,
        SCHEMA,
        SELECTOR,
    )
    return ProofBundle(
        terms=terms,
        documents=(_document(),),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_copy_shape",
                predicate_term_ref_id=predicate.term_ref_id,
                rule_term_ref_id=rule.term_ref_id,
                premises=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=source_role.term_ref_id,
                        subject=_subject("measured"),
                    ),
                ),
                conclusions=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=ROLE_RESULT.term_ref_id,
                        subject=_subject("intent"),
                    ),
                ),
            ),
        ),
        producer=ProducerBinding(
            descriptor=ProducerDescriptor(
                producer_id="compiler_test",
                producer_version="1.0.0",
                producer_contract_sha256="b" * 64,
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256="c" * 64,
        ),
    )


class _Reader:
    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        assert document == _document()
        assert len(_payload()) <= maximum_bytes
        return _payload()


class _Codec:
    def __init__(self) -> None:
        self._descriptor = GraphCodecDescriptor(
            codec_id="codec_test_graph",
            codec_version="1.0.0",
            codec_contract_sha256="d" * 64,
            schema_term=_semantic_alias(SCHEMA, "codec_schema"),
        )

    @property
    def descriptor(self) -> GraphCodecDescriptor:
        return self._descriptor

    def validate_document(self, document: DocumentRef, payload: bytes) -> None:
        if document != _document(payload):
            raise ValueError("document mismatch")

    def resolve_subject(
        self,
        document: DocumentRef,
        payload: bytes,
        subject: SubjectRef,
    ) -> ResolvedSubject | None:
        if (
            document != _document(payload)
            or subject.selector_kind_term_ref_id != SELECTOR.term_ref_id
            or subject.selector_id not in json.loads(payload)["subjects"]
        ):
            return None
        semantic_type = TYPE_SOURCE if subject.selector_id == "measured" else TYPE_RESULT
        return ResolvedSubject(
            subject=subject,
            semantic_type=_semantic_alias(semantic_type, f"codec_{semantic_type.term_ref_id}"),
        )


def _context(
    bundle: ProofBundle,
    *,
    source_type: BridgeTermRef = TYPE_SOURCE,
) -> tuple[tuple[ValidatedDocument, ...], tuple[ResolvedSubject, ...]]:
    document = ValidatedDocument(
        document=bundle.documents[0],
        payload=_payload(),
        codec_descriptor=_Codec().descriptor,
    )
    return (
        (document,),
        (
            ResolvedSubject(
                subject=_subject("measured"),
                semantic_type=source_type,
            ),
            ResolvedSubject(
                subject=_subject("intent"),
                semantic_type=TYPE_RESULT,
            ),
        ),
    )


def test_policy_runs_actual_semantic_evaluator_through_outer_proof_gate() -> None:
    evaluator = _Evaluator()
    policy = TrustedRulePolicy(evaluators=(evaluator,))
    bundle = _bundle(policy)

    report = validate_proof_bundle(
        bundle,
        reader=_Reader(),
        codecs=TrustedCodecRegistry((_Codec(),)),
        proof_policy=policy,
        maximum_total_bytes=1_024,
        maximum_subject_lookups=2,
    )

    assert report.disposition is BridgeDisposition.COMPLETE
    assert len(evaluator.evaluations) == 1
    assert evaluator.evaluations[0].bundle == bundle
    assert not bundle.executable
    assert bundle.adapter_binding_required


def test_unknown_rule_predicate_selector_role_and_subject_type_never_select_code() -> None:
    evaluator = _Evaluator()
    policy = TrustedRulePolicy(evaluators=(evaluator,))
    unknown_rule = _term("rule_unknown", "rule.unknown", definition="e")
    wrong_predicate = _term("predicate_wrong", "predicate.wrong", definition="f")
    wrong_role = _term("role_wrong", "role.wrong", definition="0")
    rebound_selector = dataclasses.replace(
        SELECTOR,
        term_definition_sha256="f" * 64,
    )
    rebound_bundle = _bundle(policy)
    rebound_bundle = dataclasses.replace(
        rebound_bundle,
        terms=tuple(
            rebound_selector if term.term_ref_id == SELECTOR.term_ref_id else term
            for term in rebound_bundle.terms
        ),
    )

    cases = (
        (_bundle(policy, rule=unknown_rule), TYPE_SOURCE),
        (_bundle(policy, predicate=wrong_predicate), TYPE_SOURCE),
        (_bundle(policy, source_role=wrong_role), TYPE_SOURCE),
        (rebound_bundle, TYPE_SOURCE),
        (_bundle(policy), TYPE_RESULT),
    )
    for bundle, source_type in cases:
        documents, subjects = _context(bundle, source_type=source_type)
        with pytest.raises(IntentBridgeError) as error:
            policy.validate(bundle, documents, subjects)
        assert error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION

    assert not evaluator.evaluations


def test_rule_descriptor_is_pinned_and_cannot_drift_after_catalog_creation() -> None:
    evaluator = _Evaluator()
    policy = TrustedRulePolicy(evaluators=(evaluator,))
    original_catalog = policy.catalog_sha256
    bundle = _bundle(policy)
    documents, subjects = _context(bundle)
    changed_predicate = _term("predicate_changed", "predicate.changed", definition="e")
    evaluator.current_descriptor = _descriptor(predicate=changed_predicate)

    policy.validate(bundle, documents, subjects)

    assert policy.catalog_sha256 == original_catalog
    assert len(evaluator.evaluations) == 1
    changed_bundle = _bundle(policy, predicate=changed_predicate)
    with pytest.raises(IntentBridgeError) as error:
        policy.validate(changed_bundle, *_context(changed_bundle))
    assert error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION


def test_evaluator_failures_are_bounded_and_cannot_become_complete() -> None:
    rejected = _Evaluator(
        failure=IntentBridgeError(
            IntentBridgeErrorCode.AUTHORITY_VIOLATION,
            "/semantic_fact",
        )
    )
    rejected_policy = TrustedRulePolicy(evaluators=(rejected,))
    rejected_bundle = _bundle(rejected_policy)
    with pytest.raises(IntentBridgeError) as rejected_error:
        rejected_policy.validate(rejected_bundle, *_context(rejected_bundle))
    assert rejected_error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION

    crashed = _Evaluator(failure=RuntimeError("do not reflect me"))
    crashed_policy = TrustedRulePolicy(evaluators=(crashed,))
    crashed_bundle = _bundle(crashed_policy)
    with pytest.raises(IntentBridgeError) as crashed_error:
        crashed_policy.validate(crashed_bundle, *_context(crashed_bundle))
    assert crashed_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert str(crashed_error.value) == (
        "intent bridge error (integrity_failure) at /evaluators/validate"
    )

    exited = _Evaluator(failure=SystemExit(17))
    exited_policy = TrustedRulePolicy(evaluators=(exited,))
    exited_bundle = _bundle(exited_policy)
    with pytest.raises(IntentBridgeError) as exited_error:
        exited_policy.validate(exited_bundle, *_context(exited_bundle))
    assert exited_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE


def test_policy_catalog_is_order_stable_and_rejects_semantic_equivocation() -> None:
    other_rule = _term("rule_other", "rule.other", definition="e")
    first = _Evaluator()
    second = _Evaluator(_descriptor(rule=other_rule))
    forward = TrustedRulePolicy(evaluators=(first, second))
    reverse = TrustedRulePolicy(evaluators=(second, first))

    assert forward.catalog_sha256 == reverse.catalog_sha256
    assert forward.catalog_id == reverse.catalog_id

    with pytest.raises(IntentBridgeError) as duplicate:
        TrustedRulePolicy(evaluators=(first, _Evaluator(_descriptor())))
    assert duplicate.value.code is IntentBridgeErrorCode.INVALID_INPUT

    conflicting_predicate = dataclasses.replace(
        PREDICATE,
        term_ref_id="predicate_conflict",
        term_definition_sha256="f" * 64,
    )
    with pytest.raises(IntentBridgeError) as conflict:
        TrustedRulePolicy(
            evaluators=(
                first,
                _Evaluator(_descriptor(rule=other_rule, predicate=conflicting_predicate)),
            )
        )
    assert conflict.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE


def test_policy_evaluator_count_is_bounded_before_descriptor_walk() -> None:
    evaluator = _Evaluator()
    with pytest.raises(IntentBridgeError) as error:
        TrustedRulePolicy(
            evaluators=(evaluator,) * (MAX_TRUSTED_RULE_EVALUATORS + 1),
        )
    assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED


def test_policy_rejects_document_or_resolved_subject_substitution() -> None:
    policy = TrustedRulePolicy(evaluators=(_Evaluator(),))
    bundle = _bundle(policy)
    documents, subjects = _context(bundle)
    extra_payload = b'{"subjects":["measured","intent","extra"]}'
    extra_ref = _document(extra_payload)
    extra_document = ValidatedDocument(
        document=extra_ref,
        payload=extra_payload,
        codec_descriptor=_Codec().descriptor,
    )

    with pytest.raises(IntentBridgeError) as document_error:
        policy.validate(bundle, (extra_document,), subjects)
    assert document_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE

    with pytest.raises(IntentBridgeError) as duplicate_document_error:
        policy.validate(bundle, (documents[0], documents[0]), subjects)
    assert duplicate_document_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE

    with pytest.raises(IntentBridgeError) as subject_error:
        policy.validate(bundle, documents, subjects[:1])
    assert subject_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
