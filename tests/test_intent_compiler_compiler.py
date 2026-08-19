"""End-to-end gates for the generic, backend-neutral intent compiler core."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect

import pytest

from vibecad.intent_bridge.contracts import (
    BridgeBudget,
    BridgeDisposition,
    BridgeTermRef,
    CompileInputBinding,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    IntentCompileRequest,
    ProofAssertion,
    ProofBundle,
    ProofEndpoint,
    RequestedOutput,
    SubjectRef,
)
from vibecad.intent_bridge.ports import (
    GraphCodecDescriptor,
    IntentCompiler,
    ResolvedSubject,
    TrustedCodecRegistry,
)
from vibecad.intent_compiler.artifacts import (
    ArtifactPublisherDescriptor,
    InMemoryIntentArtifactPublisher,
)
from vibecad.intent_compiler.catalog import TrustedIntentRuleCatalog
from vibecad.intent_compiler.compiler import RuleDrivenIntentCompiler
from vibecad.intent_compiler.contracts import (
    CompiledIntentDocument,
    DocumentSignature,
    IntentRuleDescriptor,
    IntentRuleSetDescriptor,
    IntentSelection,
    RuleSetCompileContext,
    RuleSetEmission,
)
from vibecad.intent_compiler.source_ports import SourceAdapterDescriptor


def _term(local: str, semantic: str | None = None, definition: str = "a") -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=local,
        namespace="org.vibecad.intent-compiler-e2e-test",
        vocabulary_version="1.0.0",
        term_id=semantic or f"test.{local}",
        term_definition_sha256=definition * 64,
    )


ROLE_DOCUMENT = _term("role_document", "role.evidence-document")
ROLE_SOURCE = _term("role_source", "role.compiler-source")
ROLE_OUTPUT = _term("role_output", "role.compiler-output")
SCHEMA_SOURCE = _term("schema_source", "schema.source")
SCHEMA_OUTPUT = _term("schema_output", "schema.output")
SCHEMA_FUTURE = _term("schema_future", "schema.future", "f")
SELECTOR = _term("selector_subject", "selector.subject")
FUTURE_SELECTOR = _term("selector_future", "selector.future", "f")
TYPE_SOURCE = _term("type_source", "type.source")
TYPE_OUTPUT = _term("type_output", "type.output")
RULE_SET = _term("rule_set", "rule-set.copy")
RULE_SET_TWO = _term("rule_set_two", "rule-set.copy-two", "b")
RULE = _term("rule_copy", "rule.copy", "c")
PREDICATE = _term("predicate_copy", "predicate.copy", "d")

REQUEST_TERMS = (
    ROLE_DOCUMENT,
    ROLE_SOURCE,
    ROLE_OUTPUT,
    SCHEMA_SOURCE,
    SCHEMA_OUTPUT,
    SCHEMA_FUTURE,
    SELECTOR,
    FUTURE_SELECTOR,
    TYPE_SOURCE,
    TYPE_OUTPUT,
    RULE_SET,
    RULE_SET_TWO,
    RULE,
    PREDICATE,
)
POLICY_DIGEST = "e" * 64


def _semantic_alias(term: BridgeTermRef, local: str) -> BridgeTermRef:
    return dataclasses.replace(term, term_ref_id=local)


def _source_payload() -> bytes:
    return b"trusted source evidence"


def _document(
    *,
    artifact_id: str,
    role: BridgeTermRef,
    schema: BridgeTermRef,
    payload: bytes,
) -> DocumentRef:
    return DocumentRef(
        artifact_id=artifact_id,
        role_term_ref_id=role.term_ref_id,
        schema_term_ref_id=schema.term_ref_id,
        document_id=f"document_{artifact_id}",
        document_digest=hashlib.sha256(b"semantic\0" + payload).hexdigest(),
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        media_type="application/octet-stream",
    )


def _subject(
    artifact_id: str,
    selector_id: str,
    selector_kind: BridgeTermRef = SELECTOR,
) -> SubjectRef:
    return SubjectRef(
        artifact_id=artifact_id,
        selector_kind_term_ref_id=selector_kind.term_ref_id,
        selector_id=selector_id,
    )


class _Reader:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = dict(payloads)
        self.reads: list[str] = []

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        self.reads.append(document.artifact_id)
        payload = self.payloads[document.artifact_id]
        if len(payload) > maximum_bytes:
            raise RuntimeError
        return payload


class _Codec:
    def __init__(self, schema: BridgeTermRef, *, output: bool) -> None:
        self._descriptor = GraphCodecDescriptor(
            codec_id=f"codec_{schema.term_ref_id}",
            codec_version="1.0.0",
            codec_contract_sha256=hashlib.sha256(f"codec:{schema.term_id}".encode()).hexdigest(),
            schema_term=_semantic_alias(schema, f"codec_{schema.term_ref_id}"),
        )
        self.output = output
        self.validations = 0
        self.resolutions = 0

    @property
    def descriptor(self) -> GraphCodecDescriptor:
        return self._descriptor

    def validate_document(self, document: DocumentRef, payload: bytes) -> None:
        self.validations += 1
        if document.document_digest != hashlib.sha256(b"semantic\0" + payload).hexdigest():
            raise ValueError

    def resolve_subject(
        self,
        document: DocumentRef,
        payload: bytes,
        subject: SubjectRef,
    ) -> ResolvedSubject | None:
        self.resolutions += 1
        if subject.selector_kind_term_ref_id != SELECTOR.term_ref_id:
            return None
        if self.output:
            if not subject.selector_id.startswith("result"):
                return None
            semantic_type = TYPE_OUTPUT
        else:
            if subject.selector_id != "measured":
                return None
            semantic_type = TYPE_SOURCE
        return ResolvedSubject(
            subject=subject,
            semantic_type=_semantic_alias(semantic_type, f"codec_{semantic_type.term_ref_id}"),
        )


class _SourceAdapter:
    def __init__(
        self,
        *,
        adapter_id: str = "source_adapter_test",
        rule_set_term: BridgeTermRef | None = RULE_SET,
        selector_id: str = "measured",
        selector_kind: BridgeTermRef = SELECTOR,
    ) -> None:
        self._descriptor = SourceAdapterDescriptor(
            adapter_id=adapter_id,
            adapter_version="1.0.0",
            adapter_contract_sha256=hashlib.sha256(adapter_id.encode()).hexdigest(),
            input_schema_terms=(_semantic_alias(SCHEMA_SOURCE, f"{adapter_id}_schema"),),
        )
        self.rule_set_term = rule_set_term
        self.selector_id = selector_id
        self.selector_kind = selector_kind
        self.calls = 0

    @property
    def descriptor(self) -> SourceAdapterDescriptor:
        return self._descriptor

    def select(self, request, documents) -> IntentSelection | None:
        self.calls += 1
        assert request.inputs[0].role_term_ref_id == ROLE_SOURCE.term_ref_id
        assert documents[0].document.role_term_ref_id == ROLE_DOCUMENT.term_ref_id
        if self.rule_set_term is None:
            return None
        return IntentSelection(
            rule_set_term=self.rule_set_term,
            decision_subjects=(_subject("source", self.selector_id, self.selector_kind),),
        )


def _rule_set_descriptor(
    *,
    rule_set_term: BridgeTermRef = RULE_SET,
    identity: str = "copy",
    maximum_applications: int = 4,
) -> IntentRuleSetDescriptor:
    return IntentRuleSetDescriptor(
        rule_set_id=f"rule_set_{identity}",
        rule_set_version="1.0.0",
        rule_set_contract_sha256=hashlib.sha256(f"set:{identity}".encode()).hexdigest(),
        rule_set_term=rule_set_term,
        input_signatures=(DocumentSignature(role_term=ROLE_SOURCE, schema_term=SCHEMA_SOURCE),),
        output_signatures=(DocumentSignature(role_term=ROLE_OUTPUT, schema_term=SCHEMA_OUTPUT),),
        rules=(
            IntentRuleDescriptor(
                rule_term=RULE,
                predicate_term=PREDICATE,
                emitter_contract_sha256=hashlib.sha256(f"emitter:{identity}".encode()).hexdigest(),
                maximum_applications=maximum_applications,
            ),
        ),
    )


class _RuleSet:
    def __init__(
        self,
        descriptor: IntentRuleSetDescriptor | None = None,
        *,
        output_size: int = 64,
        assertion_count: int = 1,
        semantic_tamper: bool = False,
        cycle: bool = False,
        emit_only_additional_terms: bool = False,
    ) -> None:
        self._descriptor = descriptor or _rule_set_descriptor()
        self.output_size = output_size
        self.assertion_count = assertion_count
        self.semantic_tamper = semantic_tamper
        self.cycle = cycle
        self.emit_only_additional_terms = emit_only_additional_terms
        self.calls = 0

    @property
    def descriptor(self) -> IntentRuleSetDescriptor:
        return self._descriptor

    def emit(self, context: RuleSetCompileContext) -> RuleSetEmission:
        self.calls += 1
        payload = b"I" * self.output_size
        output_id, signature = context.requested_outputs[0]
        digest = hashlib.sha256(b"semantic\0" + payload).hexdigest()
        if self.semantic_tamper:
            digest = "0" * 64
        document = CompiledIntentDocument.create(
            output_id=output_id,
            artifact_id="intent",
            role_term_ref_id=signature.role_term.term_ref_id,
            schema_term_ref_id=signature.schema_term.term_ref_id,
            document_id="document_intent",
            document_digest=digest,
            media_type="application/octet-stream",
            payload=payload,
        )
        assertions = tuple(
            ProofAssertion(
                assertion_id=f"assertion_{index:03d}",
                predicate_term_ref_id=PREDICATE.term_ref_id,
                rule_term_ref_id=RULE.term_ref_id,
                premises=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=ROLE_SOURCE.term_ref_id,
                        subject=context.selection.decision_subjects[0],
                    ),
                ),
                conclusions=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=ROLE_OUTPUT.term_ref_id,
                        subject=_subject("intent", f"result_{index:03d}"),
                    ),
                ),
                parent_assertion_ids=(
                    (f"assertion_{1 - index:03d}",)
                    if self.cycle and self.assertion_count == 2
                    else ()
                ),
            )
            for index in range(self.assertion_count)
        )
        return RuleSetEmission(
            documents=(document,),
            terms=(RULE, PREDICATE) if self.emit_only_additional_terms else context.terms,
            assertions=assertions,
        )


class _ProofPolicy:
    def __init__(
        self,
        *,
        fail: bool = False,
        exit_during_validation: bool = False,
        catalog: object = POLICY_DIGEST,
    ) -> None:
        self._catalog = catalog
        self.fail = fail
        self.exit_during_validation = exit_during_validation
        self.calls = 0

    @property
    def catalog_sha256(self):
        return self._catalog

    def validate(self, bundle: ProofBundle, documents, resolved_subjects) -> None:
        self.calls += 1
        assert {item.document.artifact_id for item in documents} == {"source", "intent"}
        assert {item.subject.artifact_id for item in resolved_subjects} == {"source", "intent"}
        if self.exit_during_validation:
            raise SystemExit
        if self.fail:
            raise IntentBridgeError(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/policy")


class _Publisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.inner = InMemoryIntentArtifactPublisher()
        self.fail = fail
        self.calls = 0

    @property
    def descriptor(self) -> ArtifactPublisherDescriptor:
        return self.inner.descriptor

    @property
    def published_documents(self) -> tuple[DocumentRef, ...]:
        return self.inner.published_documents

    def publish_atomic(self, request_digest, documents, maximum_total_bytes):
        self.calls += 1
        if self.fail:
            raise RuntimeError
        return self.inner.publish_atomic(request_digest, documents, maximum_total_bytes)

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        return self.inner.read(document, maximum_bytes)


def _compiler(
    source_adapters: tuple[_SourceAdapter, ...],
    rule_sets: tuple[_RuleSet, ...],
    publisher: _Publisher,
) -> RuleDrivenIntentCompiler:
    return RuleDrivenIntentCompiler(
        compiler_id="compiler_rule_driven_test",
        source_adapters=source_adapters,
        rule_catalog=TrustedIntentRuleCatalog(
            rule_sets,
            proof_policy_catalog_sha256=POLICY_DIGEST,
        ),
        publisher=publisher,
    )


def _request(
    compiler: RuleDrivenIntentCompiler,
    *,
    max_output_bytes: int = 4_096,
    max_rule_applications: int = 32,
    extra_document: DocumentRef | None = None,
) -> tuple[IntentCompileRequest, _Reader, TrustedCodecRegistry, _Codec, _Codec]:
    payload = _source_payload()
    source = _document(
        artifact_id="source",
        role=ROLE_DOCUMENT,
        schema=SCHEMA_SOURCE,
        payload=payload,
    )
    request = IntentCompileRequest(
        compiler=compiler.descriptor,
        terms=REQUEST_TERMS,
        documents=(source,) if extra_document is None else (source, extra_document),
        inputs=(
            CompileInputBinding(
                binding_id="input_source",
                ordinal=0,
                role_term_ref_id=ROLE_SOURCE.term_ref_id,
                artifact_id="source",
            ),
        ),
        requested_outputs=(
            RequestedOutput(
                output_id="output_intent",
                ordinal=0,
                role_term_ref_id=ROLE_OUTPUT.term_ref_id,
                schema_term_ref_id=SCHEMA_OUTPUT.term_ref_id,
            ),
        ),
        budget=BridgeBudget(
            max_input_bytes=4_096,
            max_output_bytes=max_output_bytes,
            max_subject_lookups=32,
            max_rule_applications=max_rule_applications,
        ),
    )
    source_codec = _Codec(SCHEMA_SOURCE, output=False)
    output_codec = _Codec(SCHEMA_OUTPUT, output=True)
    return (
        request,
        _Reader({"source": payload}),
        TrustedCodecRegistry((source_codec, output_codec)),
        source_codec,
        output_codec,
    )


def _run(
    compiler: RuleDrivenIntentCompiler,
    policy: _ProofPolicy | None = None,
    **request_options,
):
    request, reader, codecs, source_codec, output_codec = _request(
        compiler,
        **request_options,
    )
    result = compiler.compile(
        request,
        artifacts=reader,
        codecs=codecs,
        proof_policy=policy or _ProofPolicy(),
    )
    return result, request, reader, source_codec, output_codec


def test_complete_candidate_is_revalidated_then_atomically_published_and_idempotent() -> None:
    source = _SourceAdapter()
    rule_set = _RuleSet()
    publisher = _Publisher()
    compiler = _compiler((source,), (rule_set,), publisher)
    policy = _ProofPolicy()

    result, request, reader, source_codec, output_codec = _run(compiler, policy)

    assert isinstance(compiler, IntentCompiler)
    assert result.disposition is BridgeDisposition.COMPLETE
    assert result.request_digest == request.request_digest
    assert result.output_documents == publisher.published_documents
    assert source.calls == rule_set.calls == policy.calls == publisher.calls == 1
    assert reader.reads == ["source", "source"]
    assert source_codec.validations == 2
    assert output_codec.validations == 1

    repeated = compiler.compile(
        request,
        artifacts=_Reader({"source": _source_payload()}),
        codecs=TrustedCodecRegistry(
            (
                _Codec(SCHEMA_SOURCE, output=False),
                _Codec(SCHEMA_OUTPUT, output=True),
            )
        ),
        proof_policy=policy,
    )
    assert repeated == result
    assert publisher.published_documents == result.output_documents
    assert source.calls == rule_set.calls == policy.calls == publisher.calls == 2


@pytest.mark.parametrize(
    "case",
    [
        "none",
        "ambiguous",
        "unknown_rule",
        "unknown_selector_id",
        "unknown_selector_kind",
    ],
)
def test_unknown_or_ambiguous_selection_and_selector_are_inert_without_publication(
    case: str,
) -> None:
    rule_set = _RuleSet()
    if case == "none":
        sources = (_SourceAdapter(rule_set_term=None),)
    elif case == "ambiguous":
        sources = (
            _SourceAdapter(adapter_id="source_adapter_one"),
            _SourceAdapter(adapter_id="source_adapter_two"),
        )
    elif case == "unknown_rule":
        sources = (_SourceAdapter(rule_set_term=_term("unknown_set", "rule-set.unknown")),)
    elif case == "unknown_selector_id":
        sources = (_SourceAdapter(selector_id="missing"),)
    else:
        sources = (_SourceAdapter(selector_kind=FUTURE_SELECTOR),)
    publisher = _Publisher()
    compiler = _compiler(sources, (rule_set,), publisher)

    result, *_ = _run(compiler)

    assert result.disposition is BridgeDisposition.INERT
    assert not result.output_documents
    assert publisher.calls == 0
    assert not publisher.published_documents
    assert rule_set.calls == (1 if case.startswith("unknown_selector") else 0)


def test_tamper_cycle_policy_and_publisher_failure_never_publish() -> None:
    cases = (
        (_RuleSet(semantic_tamper=True), _ProofPolicy(), _Publisher()),
        (_RuleSet(assertion_count=2, cycle=True), _ProofPolicy(), _Publisher()),
        (_RuleSet(), _ProofPolicy(fail=True), _Publisher()),
        (_RuleSet(), _ProofPolicy(exit_during_validation=True), _Publisher()),
        (_RuleSet(), _ProofPolicy(), _Publisher(fail=True)),
    )
    expected_codes = (
        IntentBridgeErrorCode.INTEGRITY_FAILURE,
        IntentBridgeErrorCode.INVALID_INPUT,
        IntentBridgeErrorCode.AUTHORITY_VIOLATION,
        IntentBridgeErrorCode.INTEGRITY_FAILURE,
        IntentBridgeErrorCode.INTEGRITY_FAILURE,
    )
    for (rule_set, policy, publisher), expected_code in zip(
        cases,
        expected_codes,
        strict=True,
    ):
        compiler = _compiler((_SourceAdapter(),), (rule_set,), publisher)
        with pytest.raises(IntentBridgeError) as error:
            _run(compiler, policy)
        assert error.value.code is expected_code
        assert not publisher.published_documents


def test_output_byte_and_rule_application_budgets_accept_n_reject_n_plus_one() -> None:
    exact_publisher = _Publisher()
    exact = _compiler(
        (_SourceAdapter(),),
        (_RuleSet(output_size=64, assertion_count=2),),
        exact_publisher,
    )
    result, *_ = _run(exact, max_output_bytes=64, max_rule_applications=2)
    assert result.disposition is BridgeDisposition.COMPLETE

    for rule_set, options in (
        (_RuleSet(output_size=65), {"max_output_bytes": 64}),
        (_RuleSet(assertion_count=3), {"max_rule_applications": 2}),
    ):
        publisher = _Publisher()
        compiler = _compiler((_SourceAdapter(),), (rule_set,), publisher)
        with pytest.raises(IntentBridgeError) as error:
            _run(compiler, **options)
        assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
        assert not publisher.published_documents


def test_policy_catalog_is_checked_before_selection_with_bounded_failure() -> None:
    source = _SourceAdapter()
    rule_set = _RuleSet()
    publisher = _Publisher()
    compiler = _compiler((source,), (rule_set,), publisher)

    for catalog in ("f" * 64, 7):
        with pytest.raises(IntentBridgeError) as error:
            _run(compiler, _ProofPolicy(catalog=catalog))
        assert error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert source.calls == rule_set.calls == publisher.calls == 0


def test_second_dummy_rule_set_is_added_by_injection_without_core_source_changes() -> None:
    core_source_before = inspect.getsource(RuleDrivenIntentCompiler)
    second_descriptor = _rule_set_descriptor(
        rule_set_term=RULE_SET_TWO,
        identity="copy_two",
    )
    first = _RuleSet()
    second = _RuleSet(second_descriptor)
    publisher = _Publisher()
    compiler = _compiler(
        (_SourceAdapter(rule_set_term=RULE_SET_TWO),),
        (first, second),
        publisher,
    )

    result, *_ = _run(compiler)

    assert result.disposition is BridgeDisposition.COMPLETE
    assert first.calls == 0
    assert second.calls == 1
    assert inspect.getsource(RuleDrivenIntentCompiler) == core_source_before


def test_producer_contract_binds_source_rule_and_publisher_catalogs() -> None:
    publisher = _Publisher()
    baseline = _compiler((_SourceAdapter(),), (_RuleSet(),), publisher)
    changed_source = _compiler(
        (_SourceAdapter(adapter_id="source_adapter_changed"),),
        (_RuleSet(),),
        _Publisher(),
    )
    changed_rule = _compiler(
        (_SourceAdapter(),),
        (_RuleSet(_rule_set_descriptor(identity="changed")),),
        _Publisher(),
    )
    changed_publisher = _Publisher()
    changed_publisher.inner = InMemoryIntentArtifactPublisher(
        ArtifactPublisherDescriptor(
            publisher_id="publisher_changed",
            publisher_version="1.0.0",
            publisher_contract_sha256="f" * 64,
        )
    )
    changed_publication = _compiler(
        (_SourceAdapter(),),
        (_RuleSet(),),
        changed_publisher,
    )

    digests = {
        item.descriptor.producer_contract_sha256
        for item in (baseline, changed_source, changed_rule, changed_publication)
    }
    assert len(digests) == 4


def test_source_adapter_injection_order_is_canonical() -> None:
    one = _SourceAdapter(adapter_id="source_adapter_one", rule_set_term=None)
    two = _SourceAdapter(adapter_id="source_adapter_two")

    forward = _compiler((one, two), (_RuleSet(),), _Publisher())
    reverse = _compiler((two, one), (_RuleSet(),), _Publisher())

    assert forward.descriptor == reverse.descriptor
    assert _run(forward)[0].disposition is BridgeDisposition.COMPLETE
    assert _run(reverse)[0].disposition is BridgeDisposition.COMPLETE


def test_rule_set_may_emit_only_additional_terms_and_core_merges_request_table() -> None:
    publisher = _Publisher()
    compiler = _compiler(
        (_SourceAdapter(),),
        (_RuleSet(emit_only_additional_terms=True),),
        publisher,
    )

    result, *_ = _run(compiler)

    assert result.disposition is BridgeDisposition.COMPLETE
    assert result.proof_bundle is not None
    assert {item.term_ref_id for item in result.proof_bundle.terms} == {
        item.term_ref_id for item in REQUEST_TERMS
    }


def test_unknown_unbound_document_makes_whole_compile_inert_before_emission() -> None:
    unknown_payload = b"future attachment"
    unknown = _document(
        artifact_id="future_attachment",
        role=ROLE_DOCUMENT,
        schema=SCHEMA_FUTURE,
        payload=unknown_payload,
    )
    source = _SourceAdapter()
    rule_set = _RuleSet()
    publisher = _Publisher()
    compiler = _compiler((source,), (rule_set,), publisher)
    request, _, codecs, *_ = _request(compiler, extra_document=unknown)

    result = compiler.compile(
        request,
        artifacts=_Reader(
            {
                "source": _source_payload(),
                "future_attachment": unknown_payload,
            }
        ),
        codecs=codecs,
        proof_policy=_ProofPolicy(),
    )

    assert result.disposition is BridgeDisposition.INERT
    assert publisher.calls == 0
    assert rule_set.calls == 0
