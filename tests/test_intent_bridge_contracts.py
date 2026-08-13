"""Focused tests for the backend-neutral intent bridge value contracts."""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from vibecad.intent_bridge.contracts import (
    MAX_BRIDGE_DOCUMENTS,
    MAX_BRIDGE_ENVELOPE_BYTES,
    MAX_BRIDGE_TERMS,
    MAX_COMPILE_INPUTS,
    MAX_COMPILE_OUTPUTS,
    MAX_PROOF_ASSERTIONS,
    MAX_RULE_APPLICATIONS,
    MAX_SUBJECT_LOOKUPS,
    MAX_SUBJECTS_PER_ASSERTION,
    MAX_TOTAL_PAYLOAD_BYTES,
    AdapterDescriptor,
    BackendLoweringRequest,
    BackendLoweringResult,
    BridgeBudget,
    BridgeDiagnostic,
    BridgeDisposition,
    BridgeTermRef,
    CompileInputBinding,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    IntentCompileRequest,
    IntentCompileResult,
    ProducerBinding,
    ProducerDescriptor,
    ProofAssertion,
    ProofAuthority,
    ProofBundle,
    ProofEndpoint,
    RequestedOutput,
    SubjectRef,
    decode_proof_bundle,
    encode_proof_bundle,
)


def _term(term_ref_id: str, *, definition: str = "a") -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.bridge-test",
        vocabulary_version="1.0.0",
        term_id=f"test.{term_ref_id}",
        term_definition_sha256=definition * 64,
    )


def _terms() -> tuple[BridgeTermRef, ...]:
    return tuple(
        _term(term_id)
        for term_id in (
            "role_source",
            "role_intent",
            "role_capability",
            "role_plan",
            "schema_source",
            "schema_intent",
            "schema_capability",
            "schema_plan",
            "selector_node",
            "predicate_derived",
            "rule_compile",
            "diagnostic_unknown",
        )
    )


def _payload(name: str) -> bytes:
    return json.dumps({"document": name}, sort_keys=True, separators=(",", ":")).encode()


def _document(
    artifact_id: str,
    *,
    role: str,
    schema: str,
    payload: bytes | None = None,
) -> DocumentRef:
    raw = payload if payload is not None else _payload(artifact_id)
    return DocumentRef(
        artifact_id=artifact_id,
        role_term_ref_id=role,
        schema_term_ref_id=schema,
        document_id=f"document_{artifact_id}",
        document_digest=hashlib.sha256(b"semantic\0" + raw).hexdigest(),
        content_sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        media_type="application/json",
    )


def _subject(artifact_id: str, selector_id: str = "node_1") -> SubjectRef:
    return SubjectRef(
        artifact_id=artifact_id,
        selector_kind_term_ref_id="selector_node",
        selector_id=selector_id,
    )


def _endpoint(ordinal: int, artifact_id: str, selector_id: str = "node_1") -> ProofEndpoint:
    return ProofEndpoint(
        ordinal=ordinal,
        role_term_ref_id="role_source" if artifact_id == "source" else "role_intent",
        subject=_subject(artifact_id, selector_id),
    )


def _producer(request_sha256: str = "b" * 64) -> ProducerBinding:
    return ProducerBinding(
        descriptor=ProducerDescriptor(
            producer_id="compiler_test",
            producer_version="1.0.0",
            producer_contract_sha256="c" * 64,
            rule_catalog_sha256="d" * 64,
        ),
        request_sha256=request_sha256,
    )


def _assertion(
    assertion_id: str = "assertion_1",
    *,
    parents: tuple[str, ...] = (),
    premise_selector: str = "node_1",
    conclusion_selector: str = "node_1",
) -> ProofAssertion:
    return ProofAssertion(
        assertion_id=assertion_id,
        predicate_term_ref_id="predicate_derived",
        rule_term_ref_id="rule_compile",
        premises=(_endpoint(0, "source", premise_selector),),
        conclusions=(_endpoint(0, "intent", conclusion_selector),),
        parent_assertion_ids=parents,
    )


def _bundle(
    *,
    terms: tuple[BridgeTermRef, ...] | None = None,
    documents: tuple[DocumentRef, ...] | None = None,
    assertions: tuple[ProofAssertion, ...] | None = None,
    request_sha256: str = "b" * 64,
) -> ProofBundle:
    return ProofBundle(
        terms=terms or _terms(),
        documents=documents
        or (
            _document("source", role="role_source", schema="schema_source"),
            _document("intent", role="role_intent", schema="schema_intent"),
        ),
        assertions=assertions or (_assertion(),),
        producer=_producer(request_sha256),
    )


def _budget() -> BridgeBudget:
    return BridgeBudget(
        max_input_bytes=MAX_TOTAL_PAYLOAD_BYTES,
        max_output_bytes=MAX_TOTAL_PAYLOAD_BYTES,
        max_subject_lookups=MAX_SUBJECT_LOOKUPS,
        max_rule_applications=MAX_RULE_APPLICATIONS,
    )


def _compile_request() -> IntentCompileRequest:
    source = _document("source", role="role_source", schema="schema_source")
    return IntentCompileRequest(
        compiler=_producer().descriptor,
        terms=_terms(),
        documents=(source,),
        inputs=(
            CompileInputBinding(
                binding_id="input_source",
                ordinal=0,
                role_term_ref_id="role_source",
                artifact_id="source",
            ),
        ),
        requested_outputs=(
            RequestedOutput(
                output_id="output_intent",
                ordinal=0,
                role_term_ref_id="role_intent",
                schema_term_ref_id="schema_intent",
            ),
        ),
        budget=_budget(),
    )


def test_proof_bundle_roundtrips_canonically_and_has_no_authority():
    bundle = _bundle()
    raw = encode_proof_bundle(bundle)

    restored = decode_proof_bundle(raw)

    assert restored == bundle
    assert restored.bundle_id.startswith("proof_bundle_")
    assert restored.authority is ProofAuthority.EVIDENCE_ONLY
    assert restored.executable is False
    assert restored.adapter_binding_required is True
    assert encode_proof_bundle(restored) == raw


def test_collection_order_does_not_change_canonical_bundle_identity():
    first = _assertion("assertion_1")
    second = _assertion("assertion_2", parents=("assertion_1",))
    baseline = _bundle(assertions=(first, second))
    reordered = _bundle(
        terms=tuple(reversed(_terms())),
        documents=tuple(reversed(baseline.documents)),
        assertions=(second, first),
    )

    assert reordered.bundle_digest == baseline.bundle_digest
    assert encode_proof_bundle(reordered) == encode_proof_bundle(baseline)


def test_endpoint_and_request_binding_ordinals_canonicalize_constructor_order():
    premise_0 = _endpoint(0, "source", "node_0")
    premise_1 = _endpoint(1, "source", "node_1")
    assertion = ProofAssertion(
        assertion_id="assertion_order",
        predicate_term_ref_id="predicate_derived",
        rule_term_ref_id="rule_compile",
        premises=(premise_1, premise_0),
        conclusions=(_endpoint(0, "intent"),),
    )
    source = _document("source", role="role_source", schema="schema_source")
    request = IntentCompileRequest(
        compiler=_producer().descriptor,
        terms=_terms(),
        documents=(source,),
        inputs=(
            CompileInputBinding(
                binding_id="input_1",
                ordinal=1,
                role_term_ref_id="role_source",
                artifact_id="source",
            ),
            CompileInputBinding(
                binding_id="input_0",
                ordinal=0,
                role_term_ref_id="role_source",
                artifact_id="source",
            ),
        ),
        requested_outputs=(
            RequestedOutput(
                output_id="output_0",
                ordinal=0,
                role_term_ref_id="role_intent",
                schema_term_ref_id="schema_intent",
            ),
        ),
        budget=_budget(),
    )

    assert tuple(item.ordinal for item in assertion.premises) == (0, 1)
    assert tuple(item.ordinal for item in request.inputs) == (0, 1)


def test_decode_rejects_digest_tampering_duplicate_keys_and_noncanonical_json():
    raw = encode_proof_bundle(_bundle())
    mapping = json.loads(raw)
    mapping["documents"][0]["content_sha256"] = "e" * 64
    tampered = json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(IntentBridgeError) as tamper_error:
        decode_proof_bundle(tampered)
    assert tamper_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE

    duplicate = raw.replace(
        b'"authority":"evidence_only"',
        b'"authority":"evidence_only","authority":"evidence_only"',
        1,
    )
    with pytest.raises(IntentBridgeError) as duplicate_error:
        decode_proof_bundle(duplicate)
    assert duplicate_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE

    with pytest.raises(IntentBridgeError) as whitespace_error:
        decode_proof_bundle(b" " + raw)
    assert whitespace_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE

    with pytest.raises(IntentBridgeError) as byte_budget_error:
        decode_proof_bundle(b"x" * (MAX_BRIDGE_ENVELOPE_BYTES + 1))
    assert byte_budget_error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED

    with pytest.raises(IntentBridgeError) as depth_error:
        decode_proof_bundle(b"[" * 2_000 + b"]" * 2_000)
    assert depth_error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED


def test_bundle_rejects_dangling_subject_term_parent_and_proof_cycle():
    with pytest.raises(IntentBridgeError) as subject_error:
        _bundle(
            assertions=(
                dataclasses.replace(
                    _assertion(),
                    premises=(_endpoint(0, "missing"),),
                ),
            )
        )
    assert subject_error.value.code is IntentBridgeErrorCode.UNKNOWN_REFERENCE

    with pytest.raises(IntentBridgeError) as term_error:
        _bundle(
            assertions=(dataclasses.replace(_assertion(), predicate_term_ref_id="missing_term"),)
        )
    assert term_error.value.code is IntentBridgeErrorCode.UNKNOWN_REFERENCE

    with pytest.raises(IntentBridgeError) as parent_error:
        _bundle(assertions=(_assertion(parents=("missing_assertion",)),))
    assert parent_error.value.code is IntentBridgeErrorCode.UNKNOWN_REFERENCE

    first = _assertion("assertion_1", parents=("assertion_2",))
    second = _assertion("assertion_2", parents=("assertion_1",))
    with pytest.raises(IntentBridgeError) as cycle_error:
        _bundle(assertions=(first, second))
    assert cycle_error.value.code is IntentBridgeErrorCode.INVALID_INPUT


def test_deep_proof_dependency_chain_is_validated_iteratively():
    assertions = tuple(
        _assertion(
            f"chain_{index}",
            parents=() if index == 0 else (f"chain_{index - 1}",),
        )
        for index in range(650)
    )

    bundle = _bundle(assertions=assertions)

    assert len(bundle.assertions) == 650


def test_assertion_endpoint_budget_is_total_across_premises_and_conclusions():
    premise_count = MAX_SUBJECTS_PER_ASSERTION - 1
    premises = tuple(
        _endpoint(index, "source", f"premise_{index}") for index in range(premise_count)
    )
    at_limit = ProofAssertion(
        assertion_id="assertion_endpoint_limit",
        predicate_term_ref_id="predicate_derived",
        rule_term_ref_id="rule_compile",
        premises=premises,
        conclusions=(_endpoint(0, "intent"),),
    )
    assert len(at_limit.premises) + len(at_limit.conclusions) == MAX_SUBJECTS_PER_ASSERTION

    with pytest.raises(IntentBridgeError) as error:
        dataclasses.replace(
            at_limit,
            conclusions=(
                _endpoint(0, "intent", "conclusion_0"),
                _endpoint(1, "intent", "conclusion_1"),
            ),
        )
    assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED


def test_term_identity_is_content_bound_and_document_identity_is_unambiguous():
    rebound = dataclasses.replace(_term("role_source"), term_definition_sha256="f" * 64)
    with pytest.raises(IntentBridgeError) as term_error:
        _bundle(terms=(*_terms(), rebound))
    assert term_error.value.code is IntentBridgeErrorCode.INVALID_INPUT

    source = _document("source", role="role_source", schema="schema_source")
    alias = dataclasses.replace(source, artifact_id="alias")
    with pytest.raises(IntentBridgeError) as document_error:
        _bundle(
            documents=(
                source,
                alias,
                _document("intent", role="role_intent", schema="schema_intent"),
            )
        )
    assert document_error.value.code is IntentBridgeErrorCode.INVALID_INPUT


def test_unknown_content_bound_terms_are_preserved_without_becoming_executable():
    unknown_schema = _term("schema_future", definition="9")
    future = _document("intent", role="role_intent", schema="schema_future")
    bundle = _bundle(
        terms=(*_terms(), unknown_schema),
        documents=(
            _document("source", role="role_source", schema="schema_source"),
            future,
        ),
    )

    assert decode_proof_bundle(encode_proof_bundle(bundle)) == bundle
    assert bundle.executable is False
    assert bundle.adapter_binding_required is True


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("max_input_bytes", MAX_TOTAL_PAYLOAD_BYTES + 1),
        ("max_output_bytes", MAX_TOTAL_PAYLOAD_BYTES + 1),
        ("max_subject_lookups", MAX_SUBJECT_LOOKUPS + 1),
        ("max_rule_applications", MAX_RULE_APPLICATIONS + 1),
        ("max_input_bytes", True),
        ("max_output_bytes", 0),
    ),
)
def test_bridge_budget_rejects_n_plus_one_and_non_positive_values(field, value):
    values = dataclasses.asdict(_budget())
    values[field] = value
    with pytest.raises(IntentBridgeError) as error:
        BridgeBudget(**values)
    assert error.value.code is IntentBridgeErrorCode.INVALID_INPUT


def test_term_and_document_collection_budgets_accept_n_and_reject_n_plus_one():
    base_terms = _terms()
    extra_terms = tuple(
        _term(f"extra_{index}") for index in range(MAX_BRIDGE_TERMS - len(base_terms))
    )
    at_term_limit = _bundle(terms=(*base_terms, *extra_terms))
    assert len(at_term_limit.terms) == MAX_BRIDGE_TERMS
    with pytest.raises(IntentBridgeError) as term_error:
        _bundle(terms=(*at_term_limit.terms, _term("term_overflow")))
    assert term_error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED

    required = (
        _document("source", role="role_source", schema="schema_source"),
        _document("intent", role="role_intent", schema="schema_intent"),
    )
    extras = tuple(
        _document(
            f"extra_{index}",
            role="role_source",
            schema="schema_source",
        )
        for index in range(MAX_BRIDGE_DOCUMENTS - len(required))
    )
    at_document_limit = _bundle(documents=(*required, *extras))
    assert len(at_document_limit.documents) == MAX_BRIDGE_DOCUMENTS
    with pytest.raises(IntentBridgeError) as document_error:
        _bundle(
            documents=(
                *at_document_limit.documents,
                _document("document_overflow", role="role_source", schema="schema_source"),
            )
        )
    assert document_error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED


def test_assertion_budget_accepts_n_and_rejects_n_plus_one():
    assertions = tuple(_assertion(f"a{index}") for index in range(MAX_PROOF_ASSERTIONS))
    at_limit = _bundle(assertions=assertions)
    assert len(at_limit.assertions) == MAX_PROOF_ASSERTIONS

    with pytest.raises(IntentBridgeError) as error:
        _bundle(assertions=(*assertions, _assertion("assertion_overflow")))
    assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED


def test_compile_request_digest_binds_content_roles_schemas_and_budget():
    request = _compile_request()
    changed = dataclasses.replace(
        request,
        budget=dataclasses.replace(request.budget, max_rule_applications=1),
    )

    assert request.request_id.startswith("compile_request_")
    assert request.request_digest != changed.request_digest
    with pytest.raises(IntentBridgeError) as dangling:
        dataclasses.replace(
            request,
            inputs=(dataclasses.replace(request.inputs[0], artifact_id="missing"),),
        )
    assert dangling.value.code is IntentBridgeErrorCode.UNKNOWN_REFERENCE


def test_compile_input_and_output_budgets_reject_n_plus_one():
    request = _compile_request()
    inputs = tuple(
        CompileInputBinding(
            binding_id=f"input_{index}",
            ordinal=index,
            role_term_ref_id="role_source",
            artifact_id="source",
        )
        for index in range(MAX_COMPILE_INPUTS)
    )
    assert len(dataclasses.replace(request, inputs=inputs).inputs) == MAX_COMPILE_INPUTS
    with pytest.raises(IntentBridgeError) as input_error:
        dataclasses.replace(
            request,
            inputs=(
                *inputs,
                CompileInputBinding(
                    binding_id="input_overflow",
                    ordinal=0,
                    role_term_ref_id="role_source",
                    artifact_id="source",
                ),
            ),
        )
    assert input_error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED

    outputs = tuple(
        RequestedOutput(
            output_id=f"output_{index}",
            ordinal=index,
            role_term_ref_id="role_intent",
            schema_term_ref_id="schema_intent",
        )
        for index in range(MAX_COMPILE_OUTPUTS)
    )
    assert (
        len(dataclasses.replace(request, requested_outputs=outputs).requested_outputs)
        == MAX_COMPILE_OUTPUTS
    )
    with pytest.raises(IntentBridgeError) as output_error:
        dataclasses.replace(
            request,
            requested_outputs=(
                *outputs,
                RequestedOutput(
                    output_id="output_overflow",
                    ordinal=0,
                    role_term_ref_id="role_intent",
                    schema_term_ref_id="schema_intent",
                ),
            ),
        )
    assert output_error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED


def test_complete_compile_result_requires_exact_producer_proof_and_output_documents():
    request = _compile_request()
    output = _document("intent", role="role_intent", schema="schema_intent")
    proof = _bundle(request_sha256=request.request_digest)
    result = IntentCompileResult(
        request_digest=request.request_digest,
        compiler=request.compiler,
        disposition=BridgeDisposition.COMPLETE,
        output_documents=(output,),
        proof_bundle=proof,
    )
    assert result.proof_bundle == proof

    with pytest.raises(IntentBridgeError) as producer_error:
        dataclasses.replace(
            result, compiler=dataclasses.replace(request.compiler, producer_version="2")
        )
    assert producer_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE

    with pytest.raises(IntentBridgeError) as inert_error:
        dataclasses.replace(result, disposition=BridgeDisposition.INERT)
    assert inert_error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION


def test_lowering_request_binds_exact_intent_proof_capability_and_result_plan_rules():
    compile_request = _compile_request()
    intent = _document("intent", role="role_intent", schema="schema_intent")
    capability = _document(
        "capability",
        role="role_capability",
        schema="schema_capability",
    )
    proof = _bundle(request_sha256=compile_request.request_digest)
    request = BackendLoweringRequest(
        adapter=AdapterDescriptor(
            adapter_id="adapter_test",
            adapter_version="1.0.0",
            adapter_contract_sha256="e" * 64,
        ),
        terms=_terms(),
        documents=(intent, capability),
        intent_artifact_ids=("intent",),
        capability_artifact_ids=("capability",),
        proof_bundle=proof,
        budget=_budget(),
    )
    plan = _document("plan", role="role_plan", schema="schema_plan")
    result = BackendLoweringResult(
        request_digest=request.request_digest,
        adapter=request.adapter,
        disposition=BridgeDisposition.COMPLETE,
        plan_document=plan,
        supported_subjects=(_subject("intent"),),
    )

    assert request.request_id.startswith("lowering_request_")
    assert result.plan_document == plan
    with pytest.raises(IntentBridgeError) as inert_error:
        dataclasses.replace(result, disposition=BridgeDisposition.INERT)
    assert inert_error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION

    substituted_intent = dataclasses.replace(intent, content_sha256="f" * 64)
    with pytest.raises(IntentBridgeError) as substitution_error:
        dataclasses.replace(request, documents=(substituted_intent, capability))
    assert substitution_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE


def test_diagnostics_are_content_term_bound_bounded_and_deduplicated():
    diagnostic = BridgeDiagnostic(
        diagnostic_id="diagnostic_1",
        diagnostic_term_ref_id="diagnostic_unknown",
        subjects=(_subject("source"),),
    )
    result = IntentCompileResult(
        request_digest="a" * 64,
        compiler=_producer().descriptor,
        disposition=BridgeDisposition.INERT,
        diagnostics=(diagnostic,),
    )
    assert result.diagnostics == (diagnostic,)

    with pytest.raises(IntentBridgeError):
        dataclasses.replace(result, diagnostics=(diagnostic, diagnostic))
