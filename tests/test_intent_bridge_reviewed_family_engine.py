"""Focused gates for the private reviewed-family lowering engine."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json

import pytest

from vibecad.intent_bridge.contracts import (
    AdapterDescriptor,
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
from vibecad.intent_bridge.ports import (
    GraphCodecDescriptor,
    ResolvedSubject,
    TrustedCodecRegistry,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanDraft,
)


def _sha(value: str | bytes) -> str:
    payload = value if type(value) is bytes else value.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _term(ref_id: str, term_id: str) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=ref_id,
        namespace="org.vibecad.reviewed-family-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"definition:{term_id}"),
    )


INTENT_ROLE = _term("role_test_intent", "document-role.test-intent")
INTENT_SCHEMA = _term("schema_test_intent_v1", "document-schema.test-intent-v1")
CAPABILITY_ROLE = _term("role_test_capability", "document-role.test-capability")
CAPABILITY_SCHEMA = _term("schema_test_capability_v1", "document-schema.test-capability-v1")
PLAN_ROLE = _term("role_test_plan", "document-role.test-plan")
PLAN_SCHEMA = _term("schema_test_plan_v1", "document-schema.test-plan-v1")
SELECTOR = _term("selector_test_node", "selector.test-node")
SUBJECT_TYPE = _term("type_test_feature", "subject-type.test-feature")
OPERATION_BOX = _term("operation_test_box", "operation.test-box")
OPERATION_CYLINDER = _term("operation_test_cylinder", "operation.test-cylinder")
OPERATION_UNKNOWN = _term("operation_test_unknown", "operation.test-unknown")
RULE = _term("rule_test_reviewed", "rule.test-reviewed")
PREDICATE = _term("predicate_test_reviewed", "predicate.test-reviewed")
PREMISE_ROLE = _term("role_test_candidate", "proof-role.test-candidate")
CONCLUSION_ROLE = _term("role_test_validated", "proof-role.test-validated")

ADAPTER = AdapterDescriptor(
    adapter_id="test_reviewed_family_adapter",
    adapter_version="1.0.0",
    adapter_contract_sha256=_sha("test-reviewed-family-adapter-v1"),
)


def _operation_specs() -> tuple[ReviewedOperationSpec, ...]:
    return (
        ReviewedOperationSpec(
            operation_id="box",
            semantic_term=OPERATION_BOX,
            native_type_id="Part::Box",
            native_operation="Box",
            native_property_names=("Width", "Length", "Height"),
        ),
        ReviewedOperationSpec(
            operation_id="cylinder",
            semantic_term=OPERATION_CYLINDER,
            native_type_id="Part::Cylinder",
            native_operation="Cylinder",
            native_property_names=("Height", "Radius"),
        ),
    )


def _manifest(
    *, operations: tuple[ReviewedOperationSpec, ...] | None = None
) -> FamilyBatchManifest:
    operations = _operation_specs() if operations is None else operations
    request_terms = (
        INTENT_ROLE,
        INTENT_SCHEMA,
        CAPABILITY_ROLE,
        CAPABILITY_SCHEMA,
        PLAN_ROLE,
        PLAN_SCHEMA,
        *(item.semantic_term for item in operations),
    )
    return FamilyBatchManifest(
        family_id="test_reviewed_family",
        family_version="1.0.0",
        adapter=ADAPTER,
        backend_engine="FreeCAD",
        backend_version="1.1.0",
        backend_build_id=_sha("freecad-1.1.0-test-build"),
        rule_id="test_reviewed_family_rule",
        rule_contract_sha256=_sha("test-reviewed-family-rule-v1"),
        intent_role_term=INTENT_ROLE,
        intent_schema_term=INTENT_SCHEMA,
        intent_media_type="application/vnd.vibecad.test-intent+json",
        capability_role_term=CAPABILITY_ROLE,
        capability_schema_term=CAPABILITY_SCHEMA,
        capability_media_type="application/vnd.vibecad.test-capability+json",
        plan_role_term=PLAN_ROLE,
        plan_schema_term=PLAN_SCHEMA,
        plan_media_type="application/vnd.vibecad.test-plan+json",
        request_terms=request_terms,
        operations=operations,
        max_plan_bytes=16 * 1024,
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _intent_payload(operation: BridgeTermRef) -> bytes:
    return _canonical(
        {
            "authority": "trusted_adapter_required",
            "operation_term": operation.to_mapping(),
            "schema_version": 1,
            "selector_id": "target",
        }
    )


def _intent_document(payload: bytes) -> DocumentRef:
    return DocumentRef(
        artifact_id="artifact_test_intent",
        role_term_ref_id=INTENT_ROLE.term_ref_id,
        schema_term_ref_id=INTENT_SCHEMA.term_ref_id,
        document_id="test_intent_document",
        document_digest=_sha(b"test-intent-domain\0" + payload),
        content_sha256=_sha(payload),
        size_bytes=len(payload),
        media_type="application/vnd.vibecad.test-intent+json",
    )


def _subject() -> SubjectRef:
    return SubjectRef(
        artifact_id="artifact_test_intent",
        selector_kind_term_ref_id=SELECTOR.term_ref_id,
        selector_id="target",
    )


class _Codec:
    def __init__(self) -> None:
        self._descriptor = GraphCodecDescriptor(
            codec_id="test_reviewed_family_codec",
            codec_version="1.0.0",
            codec_contract_sha256=_sha("test-reviewed-family-codec-v1"),
            schema_term=INTENT_SCHEMA,
        )

    @property
    def descriptor(self) -> GraphCodecDescriptor:
        return self._descriptor

    def validate_document(self, document: DocumentRef, payload: bytes) -> None:
        if (
            document != _intent_document(payload)
            or payload != _canonical(json.loads(payload))
            or json.loads(payload).get("authority") != "trusted_adapter_required"
        ):
            raise IntentBridgeError(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/test_codec")

    def resolve_subject(
        self,
        document: DocumentRef,
        payload: bytes,
        subject: SubjectRef,
    ) -> ResolvedSubject | None:
        del document, payload
        if subject != _subject():
            return None
        return ResolvedSubject(subject=subject, semantic_type=SUBJECT_TYPE)


class _ProofPolicy:
    @property
    def catalog_sha256(self) -> str:
        return _sha("test-reviewed-family-proof-catalog")

    def validate(self, bundle, documents, resolved_subjects) -> None:
        if (
            len(bundle.assertions) != 1
            or len(documents) != 1
            or tuple(item.subject for item in resolved_subjects) != (_subject(),)
            or any(item.semantic_type != SUBJECT_TYPE for item in resolved_subjects)
        ):
            raise IntentBridgeError(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/test_policy")


class _Reader:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        payload = self.payloads[document.artifact_id]
        if len(payload) > maximum_bytes:
            raise RuntimeError("over budget")
        return payload


class _Sink:
    def __init__(self, *, failure: str | None = None) -> None:
        self.failure = failure
        self.items: dict[str, tuple[DocumentRef, bytes]] = {}

    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        if self.failure == "raise":
            raise RuntimeError("untrusted secret")
        if self.failure == "exit":
            raise SystemExit("untrusted secret")
        if self.failure == "wrong":
            return payload + b" "
        existing = self.items.get(document.artifact_id)
        if existing is not None and existing != (document, payload):
            raise RuntimeError("collision")
        staged = dict(self.items)
        staged[document.artifact_id] = (document, payload)
        self.items = staged
        return payload

    def read_exact(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        if self.failure == "read_exit":
            raise SystemExit("untrusted secret")
        stored_document, payload = self.items[document.artifact_id]
        if stored_document != document or len(payload) > maximum_bytes:
            raise RuntimeError("bad read")
        return payload


def _proof(document: DocumentRef, policy: _ProofPolicy) -> ProofBundle:
    return ProofBundle(
        terms=(
            INTENT_ROLE,
            INTENT_SCHEMA,
            SELECTOR,
            SUBJECT_TYPE,
            RULE,
            PREDICATE,
            PREMISE_ROLE,
            CONCLUSION_ROLE,
        ),
        documents=(document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_test_target",
                predicate_term_ref_id=PREDICATE.term_ref_id,
                rule_term_ref_id=RULE.term_ref_id,
                premises=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=PREMISE_ROLE.term_ref_id,
                        subject=_subject(),
                    ),
                ),
                conclusions=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=CONCLUSION_ROLE.term_ref_id,
                        subject=_subject(),
                    ),
                ),
            ),
        ),
        producer=ProducerBinding(
            descriptor=ProducerDescriptor(
                producer_id="test_reviewed_family_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("test-reviewed-family-compiler-v1"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("test-reviewed-family-compile-request"),
        ),
    )


def _request(
    operation: BridgeTermRef,
    *,
    manifest: FamilyBatchManifest | None = None,
    max_output_bytes: int = 16 * 1024,
    extra_terms: tuple[BridgeTermRef, ...] = (),
) -> tuple[BackendLoweringRequest, _Reader, _ProofPolicy]:
    manifest = _manifest() if manifest is None else manifest
    intent_payload = _intent_payload(operation)
    intent_document = _intent_document(intent_payload)
    capability_document, capability_payload = manifest.capability_document()
    policy = _ProofPolicy()
    request = BackendLoweringRequest(
        adapter=manifest.adapter,
        terms=(
            *manifest.request_terms,
            SELECTOR,
            SUBJECT_TYPE,
            RULE,
            PREDICATE,
            PREMISE_ROLE,
            CONCLUSION_ROLE,
            *extra_terms,
        ),
        documents=(intent_document, capability_document),
        intent_artifact_ids=(intent_document.artifact_id,),
        capability_artifact_ids=(capability_document.artifact_id,),
        proof_bundle=_proof(intent_document, policy),
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


def _decode_intent(payload: bytes) -> dict[str, object]:
    value = json.loads(payload)
    if type(value) is not dict or payload != _canonical(value):
        raise ValueError("noncanonical intent")
    return value


def _build_plan(
    document: DocumentRef,
    payload: bytes,
    request_digest: str,
    manifest: FamilyBatchManifest,
) -> ReviewedPlanDraft:
    value = _decode_intent(payload)
    operation = BridgeTermRef.from_mapping(value["operation_term"])
    specification = manifest.operation_for_term(operation)
    body = {
        "authority": "none",
        "adapter_contract_sha256": manifest.adapter.adapter_contract_sha256,
        "manifest_sha256": manifest.manifest_sha256,
        "operation_specification_sha256": (
            "0" * 64 if specification is None else specification.specification_sha256
        ),
        "request_digest": request_digest,
        "source": document.to_mapping(),
        "subject": _subject().to_mapping(),
    }
    plan_payload = _canonical(body)
    return ReviewedPlanDraft(
        payload=plan_payload,
        semantic_plan_sha256=_sha(b"test-plan-domain\0" + plan_payload),
        operation_term=operation,
        subjects=(_subject(),),
    )


def _decode_plan(
    payload: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
):
    value = json.loads(payload)
    if (
        type(value) is not dict
        or payload != _canonical(value)
        or not hmac.compare_digest(_sha(payload), expected_content_sha256)
        or not hmac.compare_digest(_sha(b"test-plan-domain\0" + payload), expected_plan_sha256)
    ):
        raise ValueError("invalid plan")
    return value


def _validate_binding(
    plan: object,
    receipt,
    operation: ReviewedOperationSpec,
) -> None:
    if type(plan) is not dict:
        raise ValueError("invalid plan")
    if (
        plan.get("authority") != "none"
        or plan.get("adapter_contract_sha256") != receipt.adapter.adapter_contract_sha256
        or plan.get("manifest_sha256") != receipt.manifest_sha256
        or plan.get("operation_specification_sha256") != operation.specification_sha256
        or plan.get("request_digest") != receipt.request_digest
        or plan.get("source") != receipt.source_document.to_mapping()
    ):
        raise ValueError("binding mismatch")


def _adapter(
    sink: _Sink,
    *,
    manifest: FamilyBatchManifest | None = None,
    decoder=_decode_plan,
) -> ExactReviewedFamilyAdapter:
    return ExactReviewedFamilyAdapter(
        _manifest() if manifest is None else manifest,
        sink,
        build_plan=_build_plan,
        decode_plan=decoder,
        validate_binding=_validate_binding,
    )


def _lower(adapter, request, reader, policy):
    return adapter.lower_with_receipt(
        request,
        artifacts=reader,
        codecs=TrustedCodecRegistry((_Codec(),)),
        proof_policy=policy,
    )


def test_manifest_is_canonical_reorder_stable_and_full_identity_closed() -> None:
    first = _manifest()
    second = _manifest(operations=tuple(reversed(_operation_specs())))
    assert first == second
    assert first.canonical_bytes == second.canonical_bytes
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.executable is False and first.grants_execution_authority is False
    assert first.operation_for_term(OPERATION_BOX).native_type_id == "Part::Box"
    assert first.operation_for_identity(OPERATION_UNKNOWN.semantic_identity) is None
    document, payload = first.capability_document()
    assert payload == first.canonical_bytes
    assert document.document_digest == first.manifest_sha256
    assert b'"authority":"none"' in payload

    duplicate_identity = dataclasses.replace(
        _operation_specs()[1],
        operation_id="duplicate",
        semantic_term=dataclasses.replace(OPERATION_BOX, term_ref_id="operation_alias"),
    )
    with pytest.raises(IntentBridgeError) as duplicate:
        _manifest(operations=(_operation_specs()[0], duplicate_identity))
    assert duplicate.value.code is IntentBridgeErrorCode.INVALID_INPUT

    with pytest.raises(IntentBridgeError) as malicious:
        ReviewedOperationSpec(
            operation_id="malicious",
            semantic_term=OPERATION_BOX,
            native_type_id="Part::Box;import os",
            native_operation="Box",
        )
    assert malicious.value.code is IntentBridgeErrorCode.INVALID_INPUT

    with pytest.raises(IntentBridgeError) as missing_term:
        dataclasses.replace(
            first,
            request_terms=tuple(item for item in first.request_terms if item != OPERATION_BOX),
        )
    assert missing_term.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION


def test_two_operations_share_exact_authority_free_adapter_and_readback() -> None:
    for operation_term, native_type_id in (
        (OPERATION_BOX, "Part::Box"),
        (OPERATION_CYLINDER, "Part::Cylinder"),
    ):
        request, reader, policy = _request(operation_term)
        sink = _Sink()
        adapter = _adapter(sink)
        result, receipt = _lower(adapter, request, reader, policy)
        plan, payload = adapter.read_plan(receipt)
        repeated, repeated_receipt = _lower(adapter, request, reader, policy)

        assert result.plan_document == receipt.plan_document
        assert result.supported_subjects == (_subject(),)
        assert receipt.operation.native_type_id == native_type_id
        assert receipt.executable is False and receipt.grants_execution_authority is False
        assert adapter.executable is False and adapter.grants_execution_authority is False
        assert plan["authority"] == "none"
        assert payload == sink.items[result.plan_document.artifact_id][1]
        assert repeated == result and repeated_receipt == receipt
        assert len(sink.items) == 1


def test_unknown_operation_capability_tamper_and_sink_failures_publish_nothing() -> None:
    request, reader, policy = _request(
        OPERATION_UNKNOWN,
        extra_terms=(OPERATION_UNKNOWN,),
    )
    sink = _Sink()
    with pytest.raises(IntentBridgeError) as unknown:
        _lower(_adapter(sink), request, reader, policy)
    assert unknown.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    request, reader, policy = _request(OPERATION_BOX)
    capability_id = request.capability_artifact_ids[0]
    reader.payloads[capability_id] += b" "
    sink = _Sink()
    with pytest.raises(IntentBridgeError) as tamper:
        _lower(_adapter(sink), request, reader, policy)
    assert tamper.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert sink.items == {}

    for failure in ("raise", "exit", "wrong"):
        request, reader, policy = _request(OPERATION_BOX)
        sink = _Sink(failure=failure)
        with pytest.raises(IntentBridgeError) as sink_error:
            _lower(_adapter(sink), request, reader, policy)
        assert sink_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
        assert "untrusted secret" not in str(sink_error.value)
        assert sink.items == {}


def test_plan_budget_decoder_and_readback_are_exact_and_bounded() -> None:
    request, reader, policy = _request(OPERATION_BOX)
    probe = _adapter(_Sink())
    _, receipt = _lower(probe, request, reader, policy)
    _, payload = probe.read_plan(receipt)

    exact_request, exact_reader, exact_policy = _request(
        OPERATION_BOX, max_output_bytes=len(payload)
    )
    exact_sink = _Sink()
    result, _ = _lower(_adapter(exact_sink), exact_request, exact_reader, exact_policy)
    assert result.plan_document.size_bytes == len(payload)

    small_request, small_reader, small_policy = _request(
        OPERATION_BOX, max_output_bytes=len(payload) - 1
    )
    small_sink = _Sink()
    with pytest.raises(IntentBridgeError) as budget:
        _lower(_adapter(small_sink), small_request, small_reader, small_policy)
    assert budget.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert small_sink.items == {}

    def exit_decoder(*args, **kwargs):
        del args, kwargs
        raise SystemExit("untrusted secret")

    request, reader, policy = _request(OPERATION_BOX)
    decoder_sink = _Sink()
    with pytest.raises(IntentBridgeError) as decoder:
        _lower(
            _adapter(decoder_sink, decoder=exit_decoder),
            request,
            reader,
            policy,
        )
    assert decoder.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert "untrusted secret" not in str(decoder.value)
    assert decoder_sink.items == {}

    request, reader, policy = _request(OPERATION_CYLINDER)
    read_sink = _Sink()
    adapter = _adapter(read_sink)
    _, receipt = _lower(adapter, request, reader, policy)
    document, stored = read_sink.items[receipt.plan_document.artifact_id]
    read_sink.items[document.artifact_id] = (document, stored + b" ")
    with pytest.raises(IntentBridgeError) as readback:
        adapter.read_plan(receipt)
    assert readback.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
