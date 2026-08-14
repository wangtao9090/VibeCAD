"""Generic rule-driven intent compiler with independent proof revalidation."""

from __future__ import annotations

import hashlib
import hmac

from vibecad.intent_bridge.contracts import (
    MAX_TOTAL_PAYLOAD_BYTES,
    BridgeDisposition,
    BridgeTermRef,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    IntentCompileRequest,
    IntentCompileResult,
    ProducerBinding,
    ProducerDescriptor,
    ProofBundle,
)
from vibecad.intent_bridge.ports import (
    ArtifactReader,
    TrustedCodecRegistry,
    TrustedProofPolicy,
    ValidatedDocument,
    validate_compile_result,
    validate_documents,
    validate_proof_bundle,
)
from vibecad.intent_compiler.artifacts import (
    ArtifactPublisherDescriptor,
    IntentArtifactPublisher,
    OverlayArtifactReader,
)
from vibecad.intent_compiler.catalog import (
    TrustedIntentRuleCatalog,
)
from vibecad.intent_compiler.contracts import (
    DocumentSignature,
    IntentRuleSetDescriptor,
    IntentSelection,
    RuleSetCompileContext,
    RuleSetEmission,
    canonical_bytes,
)
from vibecad.intent_compiler.source_ports import (
    MAX_TRUSTED_SOURCE_ADAPTERS,
    SourceAdapterDescriptor,
    TrustedIntentSourceAdapter,
    source_adapter_catalog_sha256,
)

_COMPILER_VERSION = "1.0.0"
_CORE_CONTRACT_DOMAIN = b"vibecad.intent-compiler.rule-driven-core.v1\0"
_OUTPUT_DERIVATION_CONTRACT = {
    "artifact_id": "emitter-declared-and-request-output-closed",
    "complete_only": True,
    "document_ref": "exact-content-and-semantic-digest",
    "publication": "post-validation-atomic-and-exact-readback",
}
_DIAGNOSTIC_CONTRACT = {
    "inert": "no-output-no-proof",
    "unknown-selection": "inert",
    "ambiguous-selection": "inert",
    "unknown-rule-set": "inert",
    "unexpected-exception": "bounded-integrity-failure",
}


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


def _semantic_lookup(terms: tuple[BridgeTermRef, ...]) -> dict[str, BridgeTermRef]:
    result = {item.term_ref_id: item for item in terms}
    if len(result) != len(terms):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/terms")
    return result


class RuleDrivenIntentCompiler:
    """Backend-neutral compiler whose semantic knowledge is entirely injected."""

    __slots__ = (
        "_descriptor",
        "_publisher",
        "_rule_catalog",
        "_source_adapters",
    )

    def __init__(
        self,
        *,
        compiler_id: str,
        source_adapters: tuple[TrustedIntentSourceAdapter, ...],
        rule_catalog: TrustedIntentRuleCatalog,
        publisher: IntentArtifactPublisher,
    ) -> None:
        if (
            type(source_adapters) is not tuple
            or not source_adapters
            or len(source_adapters) > MAX_TRUSTED_SOURCE_ADAPTERS
            or any(not isinstance(item, TrustedIntentSourceAdapter) for item in source_adapters)
        ):
            _fail(
                IntentBridgeErrorCode.BUDGET_EXCEEDED
                if type(source_adapters) is tuple
                and len(source_adapters) > MAX_TRUSTED_SOURCE_ADAPTERS
                else IntentBridgeErrorCode.INVALID_INPUT,
                "/source_adapters",
            )
        if type(rule_catalog) is not TrustedIntentRuleCatalog:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/rule_catalog")
        if not isinstance(publisher, IntentArtifactPublisher):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/publisher")
        pinned_adapters = []
        for adapter in source_adapters:
            try:
                descriptor = adapter.descriptor
            except KeyboardInterrupt:
                raise
            except SystemExit:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/source_adapters/descriptor")
            except Exception:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/source_adapters/descriptor")
            if type(descriptor) is not SourceAdapterDescriptor:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/source_adapters/descriptor")
            pinned_adapters.append((descriptor, adapter))
        names = [(item.adapter_id, item.adapter_version) for item, _ in pinned_adapters]
        if len(set(names)) != len(names):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/source_adapters")
        pinned_adapters.sort(key=lambda item: (item[0].adapter_id, item[0].adapter_version))
        try:
            publisher_descriptor = publisher.descriptor
        except KeyboardInterrupt:
            raise
        except SystemExit:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/publisher/descriptor")
        except Exception:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/publisher/descriptor")
        if type(publisher_descriptor) is not ArtifactPublisherDescriptor:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/publisher/descriptor")
        contract = {
            "core": {
                "algorithm": "single-source-selection-single-rule-set-emission",
                "bridge_protocol": "IntentCompiler.v1",
                "version": _COMPILER_VERSION,
            },
            "source_adapter_catalog_sha256": source_adapter_catalog_sha256(
                tuple(item for item, _ in pinned_adapters)
            ),
            "rule_emitter_catalog_sha256": rule_catalog.catalog_sha256,
            "publisher": publisher_descriptor.semantic_mapping(),
            "output_derivation": _OUTPUT_DERIVATION_CONTRACT,
            "diagnostics": _DIAGNOSTIC_CONTRACT,
        }
        producer_contract_sha256 = hashlib.sha256(
            _CORE_CONTRACT_DOMAIN + canonical_bytes(contract)
        ).hexdigest()
        self._descriptor = ProducerDescriptor(
            producer_id=compiler_id,
            producer_version=_COMPILER_VERSION,
            producer_contract_sha256=producer_contract_sha256,
            rule_catalog_sha256=rule_catalog.proof_policy_catalog_sha256,
        )
        self._source_adapters = tuple(pinned_adapters)
        self._rule_catalog = rule_catalog
        self._publisher = publisher

    @property
    def descriptor(self) -> ProducerDescriptor:
        return self._descriptor

    def compile(
        self,
        request: IntentCompileRequest,
        *,
        artifacts: ArtifactReader,
        codecs: TrustedCodecRegistry,
        proof_policy: TrustedProofPolicy,
    ) -> IntentCompileResult:
        if type(request) is not IntentCompileRequest:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/request")
        if request.compiler != self._descriptor:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/request/compiler")
        if not isinstance(artifacts, ArtifactReader) or type(codecs) is not TrustedCodecRegistry:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/artifacts")
        if not isinstance(proof_policy, TrustedProofPolicy):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/proof_policy")
        try:
            proof_catalog_sha256 = proof_policy.catalog_sha256
        except KeyboardInterrupt:
            raise
        except SystemExit:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/proof_policy")
        except Exception:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/proof_policy")
        if type(proof_catalog_sha256) is not str or not hmac.compare_digest(
            proof_catalog_sha256,
            self._descriptor.rule_catalog_sha256,
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/proof_policy/catalog_sha256")

        try:
            report = validate_documents(
                terms=request.terms,
                documents=request.documents,
                reader=artifacts,
                codecs=codecs,
                maximum_total_bytes=request.budget.max_input_bytes,
            )
        except IntentBridgeError:
            raise
        except KeyboardInterrupt:
            raise
        except SystemExit:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/documents")
        except Exception:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/documents")
        validated_by_id = {item.document.artifact_id: item for item in report.validated}
        if report.inert_artifact_ids or any(
            item.artifact_id not in validated_by_id for item in request.inputs
        ):
            return self._inert(request)
        validated_inputs = tuple(validated_by_id[item.artifact_id] for item in request.inputs)
        selections = self._select(request, validated_inputs)
        if len(selections) != 1:
            return self._inert(request)
        selection = selections[0]
        binding = self._rule_catalog.resolve(selection.rule_set_term)
        if binding is None:
            return self._inert(request)
        descriptor = binding.descriptor
        if not self._matches(request, validated_inputs, descriptor):
            return self._inert(request)

        context = self._context(request, validated_inputs, selection)
        try:
            emission = binding.rule_set.emit(context)
        except IntentBridgeError:
            raise
        except KeyboardInterrupt:
            raise
        except SystemExit:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/rule_set/emit")
        except Exception:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/rule_set/emit")
        if type(emission) is not RuleSetEmission:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/rule_set/emission")
        self._validate_emission(request, descriptor, emission, selection)
        all_terms = self._merge_terms(request.terms, emission.terms)
        proof = ProofBundle(
            terms=all_terms,
            documents=tuple(
                sorted(
                    (*request.documents, *(item.document for item in emission.documents)),
                    key=lambda item: item.artifact_id,
                )
            ),
            assertions=emission.assertions,
            producer=ProducerBinding(
                descriptor=self._descriptor,
                request_sha256=request.request_digest,
            ),
        )
        candidate = IntentCompileResult(
            request_digest=request.request_digest,
            compiler=self._descriptor,
            disposition=BridgeDisposition.COMPLETE,
            output_documents=tuple(item.document for item in emission.documents),
            proof_bundle=proof,
        )
        validate_compile_result(request, candidate)
        overlay = OverlayArtifactReader(artifacts, emission.documents)
        proof_bytes = sum(item.size_bytes for item in proof.documents)
        if proof_bytes > MAX_TOTAL_PAYLOAD_BYTES:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/proof_bundle/documents")
        try:
            proof_report = validate_proof_bundle(
                proof,
                reader=overlay,
                codecs=codecs,
                proof_policy=proof_policy,
                maximum_total_bytes=min(
                    MAX_TOTAL_PAYLOAD_BYTES,
                    request.budget.max_input_bytes + request.budget.max_output_bytes,
                ),
                maximum_subject_lookups=request.budget.max_subject_lookups,
            )
        except IntentBridgeError:
            raise
        except KeyboardInterrupt:
            raise
        except SystemExit:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/proof_bundle")
        except Exception:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/proof_bundle")
        if proof_report.disposition is not BridgeDisposition.COMPLETE:
            return self._inert(request)
        self._publish(request, emission, candidate.output_documents)
        return candidate

    def _select(
        self,
        request: IntentCompileRequest,
        documents: tuple[ValidatedDocument, ...],
    ) -> tuple[IntentSelection, ...]:
        selected: dict[tuple[str, str], IntentSelection] = {}
        for descriptor, adapter in self._source_adapters:
            supported = {item.semantic_identity for item in descriptor.input_schema_terms}
            actual = {item.codec_descriptor.schema_term.semantic_identity for item in documents}
            if not actual <= supported:
                continue
            try:
                selection = adapter.select(request, documents)
            except IntentBridgeError:
                raise
            except KeyboardInterrupt:
                raise
            except SystemExit:
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/source_adapter/select")
            except Exception:
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/source_adapter/select")
            if selection is None:
                continue
            if type(selection) is not IntentSelection:
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/source_adapter/selection")
            key = (descriptor.adapter_id, descriptor.adapter_version)
            selected[key] = selection
        return tuple(selected[key] for key in sorted(selected))

    @staticmethod
    def _matches(
        request: IntentCompileRequest,
        documents: tuple[ValidatedDocument, ...],
        descriptor: IntentRuleSetDescriptor,
    ) -> bool:
        term_by_id = _semantic_lookup(request.terms)
        actual_inputs = tuple(
            (
                term_by_id[binding.role_term_ref_id].semantic_identity,
                document.codec_descriptor.schema_term.semantic_identity,
            )
            for binding, document in zip(request.inputs, documents, strict=True)
        )
        expected_inputs = tuple(item.semantic_identity for item in descriptor.input_signatures)
        actual_outputs = tuple(
            (
                term_by_id[item.role_term_ref_id].semantic_identity,
                term_by_id[item.schema_term_ref_id].semantic_identity,
            )
            for item in request.requested_outputs
        )
        expected_outputs = tuple(item.semantic_identity for item in descriptor.output_signatures)
        return actual_inputs == expected_inputs and actual_outputs == expected_outputs

    @staticmethod
    def _merge_terms(
        request_terms: tuple[BridgeTermRef, ...],
        emitted_terms: tuple[BridgeTermRef, ...],
    ) -> tuple[BridgeTermRef, ...]:
        by_ref_id = _semantic_lookup(request_terms)
        semantic_names: dict[tuple[str, str, str], str] = {}
        for item in request_terms:
            semantic_name = item.semantic_identity[:3]
            prior_definition = semantic_names.setdefault(
                semantic_name,
                item.term_definition_sha256,
            )
            if prior_definition != item.term_definition_sha256:
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/emission/terms")
        for term in emitted_terms:
            prior_ref = by_ref_id.get(term.term_ref_id)
            if prior_ref is not None and prior_ref.semantic_identity != term.semantic_identity:
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/emission/terms")
            semantic_name = term.semantic_identity[:3]
            prior_definition = semantic_names.setdefault(
                semantic_name,
                term.term_definition_sha256,
            )
            if prior_definition != term.term_definition_sha256:
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/emission/terms")
            by_ref_id[term.term_ref_id] = term
        return tuple(sorted(by_ref_id.values(), key=lambda item: item.term_ref_id))

    @staticmethod
    def _context(
        request: IntentCompileRequest,
        documents: tuple[ValidatedDocument, ...],
        selection: IntentSelection,
    ) -> RuleSetCompileContext:
        term_by_id = _semantic_lookup(request.terms)
        documents_by_id = {item.document.artifact_id: item for item in documents}
        return RuleSetCompileContext(
            request_digest=request.request_digest,
            terms=request.terms,
            input_documents=tuple(
                (
                    documents_by_id[item.artifact_id].document,
                    documents_by_id[item.artifact_id].payload,
                )
                for item in request.inputs
            ),
            requested_outputs=tuple(
                (
                    item.output_id,
                    DocumentSignature(
                        role_term=term_by_id[item.role_term_ref_id],
                        schema_term=term_by_id[item.schema_term_ref_id],
                    ),
                )
                for item in request.requested_outputs
            ),
            selection=selection,
            max_output_bytes=request.budget.max_output_bytes,
            max_subject_lookups=request.budget.max_subject_lookups,
            max_rule_applications=request.budget.max_rule_applications,
        )

    @staticmethod
    def _validate_emission(
        request: IntentCompileRequest,
        descriptor: IntentRuleSetDescriptor,
        emission: RuleSetEmission,
        selection: IntentSelection,
    ) -> None:
        requested = {item.output_id: item for item in request.requested_outputs}
        if set(requested) != {item.output_id for item in emission.documents}:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/emission/documents")
        request_terms = _semantic_lookup(request.terms)
        term_by_id = _semantic_lookup(
            RuleDrivenIntentCompiler._merge_terms(request.terms, emission.terms)
        )
        for item in emission.documents:
            expected = requested[item.output_id]
            try:
                actual_role = term_by_id[item.document.role_term_ref_id]
                actual_schema = term_by_id[item.document.schema_term_ref_id]
            except KeyError:
                _fail(IntentBridgeErrorCode.UNKNOWN_REFERENCE, "/emission/terms")
            if (
                actual_role.semantic_identity
                != request_terms[expected.role_term_ref_id].semantic_identity
                or actual_schema.semantic_identity
                != request_terms[expected.schema_term_ref_id].semantic_identity
            ):
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/emission/documents")
        if sum(len(item.payload) for item in emission.documents) > request.budget.max_output_bytes:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/emission/documents")
        if len(emission.assertions) > request.budget.max_rule_applications:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/emission/assertions")
        allowed = {item.rule_term.semantic_identity: item for item in descriptor.rules}
        counts: dict[tuple[str, str, str, str], int] = {}
        input_artifacts = {item.artifact_id for item in request.inputs}
        decision_subjects = set()
        for assertion in emission.assertions:
            try:
                rule = term_by_id[assertion.rule_term_ref_id]
                predicate = term_by_id[assertion.predicate_term_ref_id]
            except KeyError:
                _fail(IntentBridgeErrorCode.UNKNOWN_REFERENCE, "/emission/assertions")
            rule_descriptor = allowed.get(rule.semantic_identity)
            if (
                rule_descriptor is None
                or predicate.semantic_identity != rule_descriptor.predicate_term.semantic_identity
            ):
                _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/emission/assertions")
            counts[rule.semantic_identity] = counts.get(rule.semantic_identity, 0) + 1
            if counts[rule.semantic_identity] > rule_descriptor.maximum_applications:
                _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/emission/assertions")
            decision_subjects.update(
                endpoint.subject
                for endpoint in assertion.premises
                if endpoint.subject.artifact_id in input_artifacts
            )
        if not set(selection.decision_subjects) <= decision_subjects:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/emission/decision_subjects")

    def _publish(
        self,
        request: IntentCompileRequest,
        emission: RuleSetEmission,
        expected_documents: tuple[DocumentRef, ...],
    ) -> None:
        try:
            published = self._publisher.publish_atomic(
                request.request_digest,
                emission.documents,
                request.budget.max_output_bytes,
            )
        except IntentBridgeError:
            raise
        except KeyboardInterrupt:
            raise
        except SystemExit:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/publisher/publish_atomic")
        except Exception:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/publisher/publish_atomic")
        if type(published) is not tuple:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/publisher/documents")
        if any(type(item) is not DocumentRef for item in published):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/publisher/documents")
        if published != expected_documents:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/publisher/documents")
        expected_payloads = {item.document.artifact_id: item.payload for item in emission.documents}
        for document in published:
            try:
                payload = self._publisher.read(document, request.budget.max_output_bytes)
            except IntentBridgeError:
                raise
            except KeyboardInterrupt:
                raise
            except SystemExit:
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/publisher/readback")
            except Exception:
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/publisher/readback")
            if (
                type(payload) is not bytes
                or len(payload) != document.size_bytes
                or payload != expected_payloads.get(document.artifact_id)
                or not hmac.compare_digest(
                    hashlib.sha256(payload).hexdigest(), document.content_sha256
                )
            ):
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/publisher/readback")

    def _inert(self, request: IntentCompileRequest) -> IntentCompileResult:
        result = IntentCompileResult(
            request_digest=request.request_digest,
            compiler=self._descriptor,
            disposition=BridgeDisposition.INERT,
        )
        validate_compile_result(request, result)
        return result


__all__ = ["RuleDrivenIntentCompiler"]
