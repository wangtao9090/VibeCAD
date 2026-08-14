"""Focused tests for private intent compiler contracts and artifact publication."""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from vibecad.intent_bridge.contracts import (
    BridgeTermRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    ProofAssertion,
    ProofEndpoint,
    SubjectRef,
)
from vibecad.intent_compiler.artifacts import InMemoryIntentArtifactPublisher
from vibecad.intent_compiler.contracts import (
    MAX_RULE_DESCRIPTORS,
    CompiledIntentDocument,
    DocumentSignature,
    IntentRuleDescriptor,
    IntentRuleSetDescriptor,
    IntentSelection,
    RuleSetEmission,
)
from vibecad.intent_compiler.source_ports import (
    MAX_TRUSTED_SOURCE_ADAPTERS,
    SourceAdapterDescriptor,
    source_adapter_catalog_sha256,
)


def _term(local: str, semantic: str | None = None, definition: str = "a") -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=local,
        namespace="org.vibecad.intent-compiler-test",
        vocabulary_version="1.0.0",
        term_id=semantic or f"test.{local}",
        term_definition_sha256=definition * 64,
    )


ROLE_SOURCE = _term("role_source")
ROLE_OUTPUT = _term("role_output")
SCHEMA_SOURCE = _term("schema_source")
SCHEMA_OUTPUT = _term("schema_output")
RULE_SET = _term("rule_set", "rule-set.dummy")
RULE = _term("rule_copy", "rule.copy")
PREDICATE = _term("predicate_copy", "predicate.derived")
SELECTOR = _term("selector_node", "selector.node")


def _subject(index: int) -> SubjectRef:
    return SubjectRef(
        artifact_id="source",
        selector_kind_term_ref_id=SELECTOR.term_ref_id,
        selector_id=f"node_{index:03d}",
    )


def _rule(index: int = 0) -> IntentRuleDescriptor:
    return IntentRuleDescriptor(
        rule_term=_term(f"rule_{index:03d}", f"rule.{index:03d}", definition="b"),
        predicate_term=_term(
            f"predicate_{index:03d}",
            f"predicate.{index:03d}",
            definition="c",
        ),
        emitter_contract_sha256=hashlib.sha256(f"emitter:{index}".encode()).hexdigest(),
        maximum_applications=4,
    )


def _descriptor(*, rules: tuple[IntentRuleDescriptor, ...] | None = None):
    return IntentRuleSetDescriptor(
        rule_set_id="rule_set_dummy",
        rule_set_version="1.0.0",
        rule_set_contract_sha256="d" * 64,
        rule_set_term=RULE_SET,
        input_signatures=(DocumentSignature(role_term=ROLE_SOURCE, schema_term=SCHEMA_SOURCE),),
        output_signatures=(DocumentSignature(role_term=ROLE_OUTPUT, schema_term=SCHEMA_OUTPUT),),
        rules=rules or (_rule(),),
    )


def _document(
    *,
    artifact_id: str = "intent",
    payload: bytes = b'{"subjects":["result"]}',
) -> CompiledIntentDocument:
    return CompiledIntentDocument.create(
        output_id="output_intent",
        artifact_id=artifact_id,
        role_term_ref_id=ROLE_OUTPUT.term_ref_id,
        schema_term_ref_id=SCHEMA_OUTPUT.term_ref_id,
        document_id=f"document_{artifact_id}",
        document_digest=hashlib.sha256(b"semantic\0" + payload).hexdigest(),
        media_type="application/json",
        payload=payload,
    )


def _assertion(assertion_id: str = "assertion_copy") -> ProofAssertion:
    return ProofAssertion(
        assertion_id=assertion_id,
        predicate_term_ref_id=PREDICATE.term_ref_id,
        rule_term_ref_id=RULE.term_ref_id,
        premises=(
            ProofEndpoint(
                ordinal=0,
                role_term_ref_id=ROLE_SOURCE.term_ref_id,
                subject=_subject(0),
            ),
        ),
        conclusions=(
            ProofEndpoint(
                ordinal=0,
                role_term_ref_id=ROLE_OUTPUT.term_ref_id,
                subject=SubjectRef(
                    artifact_id="intent",
                    selector_kind_term_ref_id=SELECTOR.term_ref_id,
                    selector_id="result",
                ),
            ),
        ),
    )


def test_selection_is_canonical_and_subject_budget_accepts_n_rejects_n_plus_one() -> None:
    selection = IntentSelection(
        rule_set_term=RULE_SET,
        decision_subjects=tuple(reversed(tuple(_subject(index) for index in range(256)))),
    )
    assert selection.decision_subjects == tuple(_subject(index) for index in range(256))

    with pytest.raises(IntentBridgeError) as error:
        IntentSelection(
            rule_set_term=RULE_SET,
            decision_subjects=tuple(_subject(index) for index in range(257)),
        )
    assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED


def test_rule_set_descriptor_is_content_bound_canonical_and_bounded() -> None:
    rules = tuple(_rule(index) for index in range(MAX_RULE_DESCRIPTORS))
    descriptor = _descriptor(rules=tuple(reversed(rules)))

    assert descriptor.rules == rules
    assert descriptor.semantic_mapping()["rules"][0]["rule_term"]["term_id"] == "rule.000"
    assert _descriptor().semantic_mapping() == _descriptor().semantic_mapping()

    with pytest.raises(IntentBridgeError) as error:
        _descriptor(rules=rules + (_rule(MAX_RULE_DESCRIPTORS),))
    assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED


def test_emitted_document_and_emission_close_raw_content_and_unique_slots() -> None:
    document = _document()
    emission = RuleSetEmission(
        documents=(document,),
        terms=(ROLE_SOURCE, ROLE_OUTPUT, SCHEMA_SOURCE, SCHEMA_OUTPUT, RULE, PREDICATE, SELECTOR),
        assertions=(_assertion(),),
    )

    assert emission.documents == (document,)
    assert document.document.content_sha256 == hashlib.sha256(document.payload).hexdigest()
    with pytest.raises(IntentBridgeError) as tamper_error:
        dataclasses.replace(document, payload=document.payload + b"x")
    assert tamper_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    with pytest.raises(IntentBridgeError):
        dataclasses.replace(emission, documents=(document, document))
    with pytest.raises(IntentBridgeError) as wrong_payload:
        CompiledIntentDocument.create(
            output_id="output_wrong",
            artifact_id="wrong",
            role_term_ref_id=ROLE_OUTPUT.term_ref_id,
            schema_term_ref_id=SCHEMA_OUTPUT.term_ref_id,
            document_id="document_wrong",
            document_digest="a" * 64,
            media_type="application/json",
            payload="not-bytes",
        )
    assert wrong_payload.value.code is IntentBridgeErrorCode.INVALID_INPUT


def test_source_adapter_catalog_budget_accepts_n_rejects_n_plus_one() -> None:
    descriptors = tuple(
        SourceAdapterDescriptor(
            adapter_id=f"adapter_{index:03d}",
            adapter_version="1.0.0",
            adapter_contract_sha256=hashlib.sha256(f"adapter:{index}".encode()).hexdigest(),
            input_schema_terms=(SCHEMA_SOURCE,),
        )
        for index in range(MAX_TRUSTED_SOURCE_ADAPTERS)
    )

    assert len(source_adapter_catalog_sha256(descriptors)) == 64
    with pytest.raises(IntentBridgeError) as error:
        source_adapter_catalog_sha256(
            descriptors
            + (
                SourceAdapterDescriptor(
                    adapter_id="adapter_overflow",
                    adapter_version="1.0.0",
                    adapter_contract_sha256="f" * 64,
                    input_schema_terms=(SCHEMA_SOURCE,),
                ),
            )
        )
    assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED

    rebound = SourceAdapterDescriptor(
        adapter_id="adapter_rebound",
        adapter_version="1.0.0",
        adapter_contract_sha256="e" * 64,
        input_schema_terms=(dataclasses.replace(SCHEMA_SOURCE, term_definition_sha256="f" * 64),),
    )
    with pytest.raises(IntentBridgeError) as rebound_error:
        source_adapter_catalog_sha256((descriptors[0], rebound))
    assert rebound_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE


def test_memory_publisher_is_content_addressed_idempotent_and_atomic_on_conflict() -> None:
    publisher = InMemoryIntentArtifactPublisher()
    first = _document()
    same_blob_other_ref = CompiledIntentDocument.create(
        output_id="output_other",
        artifact_id="intent_other",
        role_term_ref_id=ROLE_OUTPUT.term_ref_id,
        schema_term_ref_id=SCHEMA_OUTPUT.term_ref_id,
        document_id="document_intent_other",
        document_digest=first.document.document_digest,
        media_type=first.document.media_type,
        payload=first.payload,
    )
    request_a = "1" * 64
    refs = publisher.publish_atomic(request_a, (first, same_blob_other_ref), 4096)

    assert refs == tuple(
        sorted(
            (first.document, same_blob_other_ref.document),
            key=lambda item: item.artifact_id,
        )
    )
    assert publisher.publish_atomic(request_a, (first, same_blob_other_ref), 4096) == refs
    assert publisher.read(first.document, 4096) == first.payload

    new_document = _document(artifact_id="new_intent", payload=b'{"subjects":["new"]}')
    conflicting = _document(payload=b'{"subjects":["changed"]}')
    before = publisher.published_documents
    with pytest.raises(IntentBridgeError) as error:
        publisher.publish_atomic("2" * 64, (new_document, conflicting), 4096)
    assert error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert publisher.published_documents == before
    with pytest.raises(IntentBridgeError) as missing:
        publisher.read(new_document.document, 4096)
    assert missing.value.code is IntentBridgeErrorCode.UNKNOWN_REFERENCE


def test_memory_publisher_output_byte_budget_accepts_n_rejects_n_plus_one_without_writes() -> None:
    publisher = InMemoryIntentArtifactPublisher()
    document = _document(payload=b"x" * 64)
    publisher.publish_atomic("3" * 64, (document,), 64)
    before = publisher.published_documents

    larger = _document(artifact_id="larger", payload=b"y" * 65)
    with pytest.raises(IntentBridgeError) as error:
        publisher.publish_atomic("4" * 64, (larger,), 64)
    assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert publisher.published_documents == before
