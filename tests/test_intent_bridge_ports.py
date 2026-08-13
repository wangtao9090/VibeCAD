"""Focused tests for trusted intent bridge ports and inert fallback behavior."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json

import pytest

import vibecad.intent_bridge.contracts as bridge_contracts
import vibecad.intent_bridge.ports as bridge_ports
from vibecad.intent_bridge.contracts import (
    AdapterDescriptor,
    BackendLoweringRequest,
    BackendLoweringResult,
    BridgeBudget,
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
    ProofBundle,
    ProofEndpoint,
    RequestedOutput,
    SubjectRef,
)
from vibecad.intent_bridge.ports import (
    GraphCodec,
    GraphCodecDescriptor,
    IntentBackendAdapter,
    IntentCompiler,
    ResolvedSubject,
    TrustedCodecRegistry,
    read_verified_document,
    resolve_subject,
    validate_compile_result,
    validate_documents,
    validate_lowering_result,
    validate_proof_bundle,
)


def _term(
    term_ref_id: str,
    *,
    semantic: str | None = None,
    definition: str = "a",
) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.bridge-test",
        vocabulary_version="1.0.0",
        term_id=semantic or f"test.{term_ref_id}",
        term_definition_sha256=definition * 64,
    )


def _terms(*, future_intent: bool = False, decision: bool = False) -> tuple[BridgeTermRef, ...]:
    result = [
        _term("role_source"),
        _term("role_intent"),
        _term("role_capability"),
        _term("role_plan"),
        _term("schema_source", semantic="schema.source"),
        _term(
            "schema_intent",
            semantic="schema.intent.future" if future_intent else "schema.intent",
        ),
        _term("schema_capability", semantic="schema.capability"),
        _term("schema_plan", semantic="schema.plan"),
        _term("selector_node"),
        _term("predicate_derived"),
        _term("rule_compile"),
    ]
    if decision:
        result.append(_term("role_decision"))
    return tuple(result)


def _payload(artifact_id: str) -> bytes:
    return json.dumps(
        {"artifact_id": artifact_id, "subjects": ["node_1"]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


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


def _bundle(
    *,
    terms: tuple[BridgeTermRef, ...] | None = None,
    intent: DocumentRef | None = None,
    request_sha256: str = "b" * 64,
) -> ProofBundle:
    source = _document("source", role="role_source", schema="schema_source")
    target = intent or _document("intent", role="role_intent", schema="schema_intent")
    assertion = ProofAssertion(
        assertion_id="assertion_1",
        predicate_term_ref_id="predicate_derived",
        rule_term_ref_id="rule_compile",
        premises=(
            ProofEndpoint(
                ordinal=0,
                role_term_ref_id="role_source",
                subject=_subject("source"),
            ),
        ),
        conclusions=(
            ProofEndpoint(
                ordinal=0,
                role_term_ref_id="role_intent",
                subject=_subject("intent"),
            ),
        ),
    )
    return ProofBundle(
        terms=terms or _terms(),
        documents=(source, target),
        assertions=(assertion,),
        producer=_producer(request_sha256),
    )


def _budget() -> BridgeBudget:
    return BridgeBudget(
        max_input_bytes=4 * 1024 * 1024,
        max_output_bytes=4 * 1024 * 1024,
        max_subject_lookups=8_192,
        max_rule_applications=4_096,
    )


class _MemoryReader:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = dict(payloads)
        self.reads: list[str] = []

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        self.reads.append(document.artifact_id)
        value = self.payloads[document.artifact_id]
        if len(value) > maximum_bytes:
            raise RuntimeError("over budget")
        return value


class _FakeCodec:
    def __init__(self, schema_term: BridgeTermRef) -> None:
        self._descriptor = GraphCodecDescriptor(
            codec_id=f"codec_{schema_term.term_ref_id}",
            codec_version="1.0.0",
            codec_contract_sha256="e" * 64,
            schema_term=schema_term,
        )

    @property
    def descriptor(self) -> GraphCodecDescriptor:
        return self._descriptor

    def validate_document(self, document: DocumentRef, payload: bytes) -> None:
        decoded = json.loads(payload)
        if decoded["artifact_id"] != document.artifact_id:
            raise ValueError("identity mismatch")
        expected = hashlib.sha256(b"semantic\0" + payload).hexdigest()
        if expected != document.document_digest:
            raise ValueError("semantic digest mismatch")

    def resolve_subject(
        self,
        document: DocumentRef,
        payload: bytes,
        subject: SubjectRef,
    ) -> ResolvedSubject | None:
        if subject.selector_kind_term_ref_id != "selector_node":
            return None
        if subject.selector_id not in json.loads(payload)["subjects"]:
            return None
        return ResolvedSubject(
            subject=subject,
            semantic_type=_term("type_node"),
        )


class _AcceptingPolicy:
    def __init__(self, *, require_decision: bool = False, catalog: str = "d" * 64) -> None:
        self._catalog = catalog
        self.require_decision = require_decision
        self.calls = 0

    @property
    def catalog_sha256(self) -> str:
        return self._catalog

    def validate(self, bundle: ProofBundle, documents, resolved_subjects) -> None:
        self.calls += 1
        assert documents
        assert resolved_subjects
        if self.require_decision:
            terms = {item.term_ref_id: item.term_id for item in bundle.terms}
            if not any(
                terms[item.role_term_ref_id] == "test.role_decision" for item in bundle.documents
            ):
                raise IntentBridgeError(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/decision")


def _registry(*, include_intent: bool = True) -> TrustedCodecRegistry:
    codecs: list[GraphCodec] = [
        _FakeCodec(_term("codec_source", semantic="schema.source")),
    ]
    if include_intent:
        codecs.append(_FakeCodec(_term("codec_intent", semantic="schema.intent")))
    return TrustedCodecRegistry(tuple(codecs))


def _reader_for(bundle: ProofBundle) -> _MemoryReader:
    return _MemoryReader(
        {item.artifact_id: _payload(item.artifact_id) for item in bundle.documents}
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


def test_codec_registry_uses_full_content_bound_identity_not_local_ref_or_term_name():
    schema = _term("codec_source", semantic="schema.source")
    codec = _FakeCodec(schema)
    registry = TrustedCodecRegistry((codec,))

    alias = _term("request_source", semantic="schema.source")
    rebound = _term("request_source", semantic="schema.source", definition="f")

    assert registry.codec_for(alias) is codec
    assert registry.codec_for(rebound) is None
    assert registry.codec_for(_term("unknown", semantic="schema.unknown")) is None

    with pytest.raises(IntentBridgeError):
        TrustedCodecRegistry((codec, _FakeCodec(alias)))


def test_graph_codec_protocol_is_structural_but_registry_is_explicitly_host_created():
    codec = _FakeCodec(_term("codec_source", semantic="schema.source"))
    assert isinstance(codec, GraphCodec)
    assert TrustedCodecRegistry((codec,)).descriptors == (codec.descriptor,)


def test_reader_closes_raw_content_size_and_digest_before_codec_interpretation():
    document = _document("source", role="role_source", schema="schema_source")
    payload = _payload("source")
    reader = _MemoryReader({"source": payload})

    assert read_verified_document(reader, document, maximum_bytes=len(payload)) == payload
    with pytest.raises(IntentBridgeError) as budget_error:
        read_verified_document(reader, document, maximum_bytes=len(payload) - 1)
    assert budget_error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED

    tampered_reader = _MemoryReader({"source": payload + b"x"})
    with pytest.raises(IntentBridgeError) as integrity_error:
        read_verified_document(tampered_reader, document, maximum_bytes=len(payload) + 1)
    assert integrity_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE

    with pytest.raises(IntentBridgeError) as host_cap_error:
        read_verified_document(
            reader,
            document,
            maximum_bytes=bridge_contracts.MAX_TOTAL_PAYLOAD_BYTES + 1,
        )
    assert host_cap_error.value.code is IntentBridgeErrorCode.INVALID_INPUT


def test_known_documents_validate_and_unknown_schema_is_inert_without_being_read():
    known = _document("source", role="role_source", schema="schema_source")
    unknown = _document("intent", role="role_intent", schema="schema_intent")
    terms = _terms(future_intent=True)
    reader = _MemoryReader({"source": _payload("source")})

    report = validate_documents(
        terms=terms,
        documents=(known, unknown),
        reader=reader,
        codecs=_registry(include_intent=False),
        maximum_total_bytes=1_024,
    )

    assert tuple(item.document.artifact_id for item in report.validated) == ("source",)
    assert report.inert_artifact_ids == ("intent",)
    assert reader.reads == ["source"]


def test_codec_must_close_declared_semantic_document_digest():
    document = dataclasses.replace(
        _document("source", role="role_source", schema="schema_source"),
        document_digest="f" * 64,
    )
    with pytest.raises(IntentBridgeError) as error:
        validate_documents(
            terms=_terms(),
            documents=(document,),
            reader=_MemoryReader({"source": _payload("source")}),
            codecs=_registry(include_intent=False),
            maximum_total_bytes=1_024,
        )
    assert error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE


def test_subject_resolution_uses_codec_and_unknown_selector_remains_inert():
    bundle = _bundle()
    registry = _registry()
    report = validate_documents(
        terms=bundle.terms,
        documents=bundle.documents,
        reader=_reader_for(bundle),
        codecs=registry,
        maximum_total_bytes=4_096,
    )

    resolved = resolve_subject(
        _subject("intent"),
        validated_documents=report.validated,
        codecs=registry,
    )
    unresolved = resolve_subject(
        _subject("intent", "node_missing"),
        validated_documents=report.validated,
        codecs=registry,
    )

    assert resolved is not None
    assert resolved.subject == _subject("intent")
    assert unresolved is None


def test_proof_validation_is_complete_only_after_codec_and_exact_policy_validation():
    bundle = _bundle()
    policy = _AcceptingPolicy()
    report = validate_proof_bundle(
        bundle,
        reader=_reader_for(bundle),
        codecs=_registry(),
        proof_policy=policy,
        maximum_total_bytes=4_096,
        maximum_subject_lookups=2,
    )

    assert report.disposition is BridgeDisposition.COMPLETE
    assert not report.inert_subjects
    assert policy.calls == 1

    wrong_policy = _AcceptingPolicy(catalog="f" * 64)
    with pytest.raises(IntentBridgeError) as catalog_error:
        validate_proof_bundle(
            bundle,
            reader=_reader_for(bundle),
            codecs=_registry(),
            proof_policy=wrong_policy,
            maximum_total_bytes=4_096,
            maximum_subject_lookups=2,
        )
    assert catalog_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE


def test_unknown_document_or_subject_returns_inert_without_invoking_semantic_policy():
    future_terms = _terms(future_intent=True)
    future_intent = _document("intent", role="role_intent", schema="schema_intent")
    bundle = _bundle(terms=future_terms, intent=future_intent)
    policy = _AcceptingPolicy()

    report = validate_proof_bundle(
        bundle,
        reader=_reader_for(bundle),
        codecs=_registry(include_intent=False),
        proof_policy=policy,
        maximum_total_bytes=4_096,
        maximum_subject_lookups=2,
    )

    assert report.disposition is BridgeDisposition.INERT
    assert report.documents.inert_artifact_ids == ("intent",)
    assert report.inert_subjects == (_subject("intent"),)
    assert policy.calls == 0


def test_policy_can_require_explicit_decision_instead_of_implicitly_adopting_evidence():
    bundle = _bundle()
    policy = _AcceptingPolicy(require_decision=True)

    with pytest.raises(IntentBridgeError) as error:
        validate_proof_bundle(
            bundle,
            reader=_reader_for(bundle),
            codecs=_registry(),
            proof_policy=policy,
            maximum_total_bytes=4_096,
            maximum_subject_lookups=2,
        )
    assert error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION


def test_subject_lookup_budget_accepts_n_and_rejects_n_plus_one():
    bundle = _bundle()
    with pytest.raises(IntentBridgeError) as error:
        validate_proof_bundle(
            bundle,
            reader=_reader_for(bundle),
            codecs=_registry(),
            proof_policy=_AcceptingPolicy(),
            maximum_total_bytes=4_096,
            maximum_subject_lookups=1,
        )
    assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED


def test_complete_compiler_result_is_closed_to_request_terms_digests_and_output_budget():
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

    validate_compile_result(request, result)
    with pytest.raises(IntentBridgeError) as request_error:
        validate_compile_result(
            request,
            dataclasses.replace(result, request_digest="f" * 64),
        )
    assert request_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE

    wrong_role = dataclasses.replace(output, role_term_ref_id="role_source")
    source = next(item for item in proof.documents if item.artifact_id == "source")
    wrong_proof = dataclasses.replace(
        proof,
        documents=(source, wrong_role),
    )
    wrong_result = IntentCompileResult(
        request_digest=request.request_digest,
        compiler=request.compiler,
        disposition=BridgeDisposition.COMPLETE,
        output_documents=(wrong_role,),
        proof_bundle=wrong_proof,
    )
    with pytest.raises(IntentBridgeError) as output_error:
        validate_compile_result(request, wrong_result)
    assert output_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE


def test_lowering_result_is_closed_to_adapter_request_plan_budget_and_known_subjects():
    compile_request = _compile_request()
    proof = _bundle(request_sha256=compile_request.request_digest)
    intent = (
        proof.documents[0] if proof.documents[0].artifact_id == "intent" else proof.documents[1]
    )
    capability = _document(
        "capability",
        role="role_capability",
        schema="schema_capability",
    )
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
    result = BackendLoweringResult(
        request_digest=request.request_digest,
        adapter=request.adapter,
        disposition=BridgeDisposition.COMPLETE,
        plan_document=_document("plan", role="role_plan", schema="schema_plan"),
        supported_subjects=(_subject("intent"),),
    )

    validate_lowering_result(request, result)
    with pytest.raises(IntentBridgeError) as adapter_error:
        validate_lowering_result(
            request,
            dataclasses.replace(
                result,
                adapter=dataclasses.replace(result.adapter, adapter_version="2"),
            ),
        )
    assert adapter_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE

    with pytest.raises(IntentBridgeError) as subject_error:
        validate_lowering_result(
            request,
            dataclasses.replace(result, supported_subjects=(_subject("missing"),)),
        )
    assert subject_error.value.code is IntentBridgeErrorCode.UNKNOWN_REFERENCE

    with pytest.raises(IntentBridgeError) as complete_inert_error:
        dataclasses.replace(
            result,
            supported_subjects=(),
            inert_subjects=(_subject("intent"),),
        )
    assert complete_inert_error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION


class _FakeCompiler:
    @property
    def descriptor(self) -> ProducerDescriptor:
        return _producer().descriptor

    def compile(self, request, *, artifacts, codecs, proof_policy):
        return IntentCompileResult(
            request_digest=request.request_digest,
            compiler=self.descriptor,
            disposition=BridgeDisposition.INERT,
        )


class _FakeAdapter:
    @property
    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            adapter_id="adapter_test",
            adapter_version="1.0.0",
            adapter_contract_sha256="e" * 64,
        )

    def lower(self, request, *, artifacts, codecs, proof_policy):
        return BackendLoweringResult(
            request_digest=request.request_digest,
            adapter=self.descriptor,
            disposition=BridgeDisposition.INERT,
        )


def test_compiler_and_adapter_ports_are_structural_and_do_not_grant_execution():
    compiler = _FakeCompiler()
    adapter = _FakeAdapter()
    assert isinstance(compiler, IntentCompiler)
    assert isinstance(adapter, IntentBackendAdapter)
    assert (
        compiler.compile(
            _compile_request(),
            artifacts=_MemoryReader({}),
            codecs=TrustedCodecRegistry(()),
            proof_policy=_AcceptingPolicy(),
        ).disposition
        is BridgeDisposition.INERT
    )


def test_core_bridge_sources_contain_no_backend_native_vocabulary():
    source = inspect.getsource(bridge_contracts) + inspect.getsource(bridge_ports)
    for forbidden in ("TypeId", "PartDesign::", "App::Property", "Mesh::"):
        assert forbidden not in source
