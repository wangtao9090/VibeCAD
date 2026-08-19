"""Trusted local ports for the backend-neutral intent bridge.

The protocols are dependency-injection boundaries, not model-controlled plugin
surfaces.  A codec is selected only by the complete content-bound identity of
its schema term.  No implementation is imported or discovered dynamically.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from vibecad.intent_bridge.contracts import (
    MAX_BRIDGE_DOCUMENTS,
    MAX_SUBJECT_LOOKUPS,
    MAX_TOTAL_PAYLOAD_BYTES,
    AdapterDescriptor,
    BackendLoweringRequest,
    BackendLoweringResult,
    BridgeDisposition,
    BridgeTermRef,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    IntentCompileRequest,
    IntentCompileResult,
    ProducerDescriptor,
    ProofBundle,
    SubjectRef,
)


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


@dataclass(frozen=True, slots=True, kw_only=True)
class GraphCodecDescriptor:
    codec_id: str
    codec_version: str
    codec_contract_sha256: str
    schema_term: BridgeTermRef

    def __post_init__(self) -> None:
        # Reuse the stable contract validators without introducing a second
        # identifier grammar.  This descriptor is host-local and not wire data.
        ProducerDescriptor(
            producer_id=self.codec_id,
            producer_version=self.codec_version,
            producer_contract_sha256=self.codec_contract_sha256,
            rule_catalog_sha256=self.codec_contract_sha256,
        )
        if type(self.schema_term) is not BridgeTermRef:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/schema_term")


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedSubject:
    subject: SubjectRef
    semantic_type: BridgeTermRef

    def __post_init__(self) -> None:
        if type(self.subject) is not SubjectRef or type(self.semantic_type) is not BridgeTermRef:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/resolved_subject")


@runtime_checkable
class ArtifactReader(Protocol):
    """Read exact immutable bytes for one declared content reference."""

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        """Return the bytes or raise without changing external state."""


@runtime_checkable
class GraphCodec(Protocol):
    """Trusted structural interpreter for one exact document schema."""

    @property
    def descriptor(self) -> GraphCodecDescriptor: ...

    def validate_document(self, document: DocumentRef, payload: bytes) -> None:
        """Verify canonical encoding plus declared document id and digest."""

    def resolve_subject(
        self,
        document: DocumentRef,
        payload: bytes,
        subject: SubjectRef,
    ) -> ResolvedSubject | None:
        """Return a trusted descriptor, or None when the selector is inert."""


@runtime_checkable
class TrustedProofPolicy(Protocol):
    """Trusted semantic rule/coverage policy bound by its catalog digest."""

    @property
    def catalog_sha256(self) -> str: ...

    def validate(
        self,
        bundle: ProofBundle,
        documents: tuple[ValidatedDocument, ...],
        resolved_subjects: tuple[ResolvedSubject, ...],
    ) -> None:
        """Validate rule arity, types, decisions, and target coverage."""


@runtime_checkable
class IntentCompiler(Protocol):
    """Pure deterministic producer of intent documents and proof."""

    @property
    def descriptor(self) -> ProducerDescriptor: ...

    def compile(
        self,
        request: IntentCompileRequest,
        *,
        artifacts: ArtifactReader,
        codecs: TrustedCodecRegistry,
        proof_policy: TrustedProofPolicy,
    ) -> IntentCompileResult: ...


@runtime_checkable
class IntentBackendAdapter(Protocol):
    """Lower validated intent into a plan without executing that plan."""

    @property
    def descriptor(self) -> AdapterDescriptor: ...

    def lower(
        self,
        request: BackendLoweringRequest,
        *,
        artifacts: ArtifactReader,
        codecs: TrustedCodecRegistry,
        proof_policy: TrustedProofPolicy,
    ) -> BackendLoweringResult: ...


class TrustedCodecRegistry:
    """Immutable host-created codec table keyed by full semantic identity."""

    __slots__ = ("_by_identity", "_descriptors")

    def __init__(self, codecs: tuple[GraphCodec, ...]) -> None:
        if type(codecs) is not tuple or len(codecs) > MAX_BRIDGE_DOCUMENTS:
            _fail(
                IntentBridgeErrorCode.BUDGET_EXCEEDED
                if type(codecs) is tuple
                else IntentBridgeErrorCode.INVALID_INPUT,
                "/codecs",
            )
        by_identity: dict[tuple[str, str, str, str], GraphCodec] = {}
        descriptors: list[GraphCodecDescriptor] = []
        for codec in codecs:
            if not isinstance(codec, GraphCodec):
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/codecs")
            try:
                descriptor = codec.descriptor
            except Exception:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/codecs")
            if type(descriptor) is not GraphCodecDescriptor:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/codecs/descriptor")
            identity = descriptor.schema_term.semantic_identity
            if identity in by_identity:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/codecs")
            by_identity[identity] = codec
            descriptors.append(descriptor)
        self._by_identity = MappingProxyType(by_identity)
        self._descriptors = tuple(
            sorted(descriptors, key=lambda item: item.schema_term.semantic_identity)
        )

    @property
    def descriptors(self) -> tuple[GraphCodecDescriptor, ...]:
        return self._descriptors

    def codec_for(self, schema_term: BridgeTermRef) -> GraphCodec | None:
        if type(schema_term) is not BridgeTermRef:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/schema_term")
        return self._by_identity.get(schema_term.semantic_identity)


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidatedDocument:
    document: DocumentRef
    payload: bytes
    codec_descriptor: GraphCodecDescriptor

    def __post_init__(self) -> None:
        if (
            type(self.document) is not DocumentRef
            or type(self.payload) is not bytes
            or type(self.codec_descriptor) is not GraphCodecDescriptor
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/validated_document")
        if len(self.payload) != self.document.size_bytes or not hmac.compare_digest(
            hashlib.sha256(self.payload).hexdigest(), self.document.content_sha256
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/validated_document/payload")


@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentValidationReport:
    validated: tuple[ValidatedDocument, ...]
    inert_artifact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.validated) is not tuple or any(
            type(item) is not ValidatedDocument for item in self.validated
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/validated")
        if type(self.inert_artifact_ids) is not tuple or any(
            type(item) is not str for item in self.inert_artifact_ids
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/inert_artifact_ids")
        validated_ids = tuple(item.document.artifact_id for item in self.validated)
        if (
            len(set(validated_ids)) != len(validated_ids)
            or len(set(self.inert_artifact_ids)) != len(self.inert_artifact_ids)
            or set(validated_ids) & set(self.inert_artifact_ids)
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/validated")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProofValidationReport:
    disposition: BridgeDisposition
    documents: DocumentValidationReport
    resolved_subjects: tuple[ResolvedSubject, ...]
    inert_subjects: tuple[SubjectRef, ...]

    def __post_init__(self) -> None:
        if (
            type(self.disposition) is not BridgeDisposition
            or type(self.documents) is not DocumentValidationReport
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/proof_validation")
        if type(self.resolved_subjects) is not tuple or any(
            type(item) is not ResolvedSubject for item in self.resolved_subjects
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/resolved_subjects")
        if type(self.inert_subjects) is not tuple or any(
            type(item) is not SubjectRef for item in self.inert_subjects
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/inert_subjects")
        if self.disposition is BridgeDisposition.COMPLETE and (
            self.documents.inert_artifact_ids or self.inert_subjects
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/proof_validation")


def read_verified_document(
    reader: ArtifactReader,
    document: DocumentRef,
    *,
    maximum_bytes: int,
) -> bytes:
    """Read one payload and verify declared size and raw content digest."""

    if not isinstance(reader, ArtifactReader) or type(document) is not DocumentRef:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/reader")
    if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= MAX_TOTAL_PAYLOAD_BYTES:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/maximum_bytes")
    if document.size_bytes > maximum_bytes:
        _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/document/size_bytes")
    try:
        payload = reader.read(document, maximum_bytes)
    except IntentBridgeError:
        raise
    except Exception:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/payload")
    if type(payload) is not bytes:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/payload")
    if len(payload) != document.size_bytes or len(payload) > maximum_bytes:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/size_bytes")
    if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), document.content_sha256):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/content_sha256")
    return payload


def validate_documents(
    *,
    terms: tuple[BridgeTermRef, ...],
    documents: tuple[DocumentRef, ...],
    reader: ArtifactReader,
    codecs: TrustedCodecRegistry,
    maximum_total_bytes: int,
) -> DocumentValidationReport:
    """Validate known schemas; preserve unknown schemas as explicitly inert."""

    if type(terms) is not tuple or any(type(item) is not BridgeTermRef for item in terms):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/terms")
    if type(documents) is not tuple or any(type(item) is not DocumentRef for item in documents):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/documents")
    if type(codecs) is not TrustedCodecRegistry:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/codecs")
    if (
        type(maximum_total_bytes) is not int
        or not 1 <= maximum_total_bytes <= MAX_TOTAL_PAYLOAD_BYTES
    ):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/maximum_total_bytes")
    term_by_id = {item.term_ref_id: item for item in terms}
    if len(term_by_id) != len(terms):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/terms")
    validated: list[ValidatedDocument] = []
    inert: list[str] = []
    consumed = 0
    for document in documents:
        schema_term = term_by_id.get(document.schema_term_ref_id)
        if schema_term is None:
            _fail(IntentBridgeErrorCode.UNKNOWN_REFERENCE, "/documents/schema_term_ref_id")
        codec = codecs.codec_for(schema_term)
        if codec is None:
            inert.append(document.artifact_id)
            continue
        if consumed + document.size_bytes > maximum_total_bytes:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/documents")
        payload = read_verified_document(
            reader,
            document,
            maximum_bytes=maximum_total_bytes - consumed,
        )
        try:
            codec.validate_document(document, payload)
        except IntentBridgeError:
            raise
        except Exception:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/documents")
        try:
            descriptor = codec.descriptor
        except Exception:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/codecs/descriptor")
        if (
            type(descriptor) is not GraphCodecDescriptor
            or descriptor.schema_term.semantic_identity != schema_term.semantic_identity
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/codecs/descriptor")
        consumed += len(payload)
        validated.append(
            ValidatedDocument(
                document=document,
                payload=payload,
                codec_descriptor=descriptor,
            )
        )
    return DocumentValidationReport(
        validated=tuple(sorted(validated, key=lambda item: item.document.artifact_id)),
        inert_artifact_ids=tuple(sorted(inert)),
    )


def resolve_subject(
    subject: SubjectRef,
    *,
    validated_documents: tuple[ValidatedDocument, ...],
    codecs: TrustedCodecRegistry,
) -> ResolvedSubject | None:
    """Resolve one subject without treating an unknown selector as executable."""

    if type(subject) is not SubjectRef or type(codecs) is not TrustedCodecRegistry:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/subject")
    if len(validated_documents) > MAX_BRIDGE_DOCUMENTS or any(
        type(item) is not ValidatedDocument for item in validated_documents
    ):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/validated_documents")
    matches = tuple(
        item for item in validated_documents if item.document.artifact_id == subject.artifact_id
    )
    if not matches:
        return None
    if len(matches) != 1:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/validated_documents")
    validated = matches[0]
    schema_identity = validated.codec_descriptor.schema_term.semantic_identity
    codec = codecs.codec_for(validated.codec_descriptor.schema_term)
    try:
        descriptor = None if codec is None else codec.descriptor
    except Exception:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/codecs")
    if (
        codec is None
        or type(descriptor) is not GraphCodecDescriptor
        or descriptor.schema_term.semantic_identity != schema_identity
    ):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/codecs")
    try:
        result = codec.resolve_subject(validated.document, validated.payload, subject)
    except IntentBridgeError:
        raise
    except Exception:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/subject")
    if result is None:
        return None
    if type(result) is not ResolvedSubject or result.subject != subject:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/subject")
    return result


def validate_proof_bundle(
    bundle: ProofBundle,
    *,
    reader: ArtifactReader,
    codecs: TrustedCodecRegistry,
    proof_policy: TrustedProofPolicy,
    maximum_total_bytes: int,
    maximum_subject_lookups: int,
) -> ProofValidationReport:
    """Validate structural documents/subjects, then the exact trusted policy.

    Missing codecs and unresolved selectors are preserved as inert and never
    reach semantic rule evaluation.
    """

    if type(bundle) is not ProofBundle or not isinstance(proof_policy, TrustedProofPolicy):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/proof_bundle")
    if (
        type(maximum_subject_lookups) is not int
        or not 1 <= maximum_subject_lookups <= MAX_SUBJECT_LOOKUPS
    ):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/maximum_subject_lookups")
    try:
        catalog_digest = proof_policy.catalog_sha256
    except Exception:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/proof_policy")
    if type(catalog_digest) is not str or not hmac.compare_digest(
        catalog_digest, bundle.producer.descriptor.rule_catalog_sha256
    ):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/proof_policy/catalog_sha256")
    document_report = validate_documents(
        terms=bundle.terms,
        documents=bundle.documents,
        reader=reader,
        codecs=codecs,
        maximum_total_bytes=maximum_total_bytes,
    )
    subjects = tuple(
        endpoint.subject
        for assertion in bundle.assertions
        for endpoint in (*assertion.premises, *assertion.conclusions)
    )
    unique_subjects = tuple(
        {
            (
                item.artifact_id,
                item.selector_kind_term_ref_id,
                item.selector_id,
            ): item
            for item in subjects
        }.values()
    )
    if len(unique_subjects) > maximum_subject_lookups:
        _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/assertions/subject")
    resolved: list[ResolvedSubject] = []
    inert: list[SubjectRef] = []
    for subject in unique_subjects:
        item = resolve_subject(
            subject,
            validated_documents=document_report.validated,
            codecs=codecs,
        )
        if item is None:
            inert.append(subject)
        else:
            resolved.append(item)
    if document_report.inert_artifact_ids or inert:
        return ProofValidationReport(
            disposition=BridgeDisposition.INERT,
            documents=document_report,
            resolved_subjects=tuple(resolved),
            inert_subjects=tuple(inert),
        )
    try:
        proof_policy.validate(bundle, document_report.validated, tuple(resolved))
    except IntentBridgeError:
        raise
    except Exception:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/proof_policy")
    return ProofValidationReport(
        disposition=BridgeDisposition.COMPLETE,
        documents=document_report,
        resolved_subjects=tuple(resolved),
        inert_subjects=(),
    )


def validate_compile_result(
    request: IntentCompileRequest,
    result: IntentCompileResult,
) -> None:
    """Close a compiler result to its exact request and requested output terms."""

    if type(request) is not IntentCompileRequest or type(result) is not IntentCompileResult:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")
    if (
        not hmac.compare_digest(request.request_digest, result.request_digest)
        or request.compiler != result.compiler
    ):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/request_digest")
    if sum(item.size_bytes for item in result.output_documents) > request.budget.max_output_bytes:
        _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/output_documents")
    if not result.output_documents:
        return
    request_terms = {item.term_ref_id: item.semantic_identity for item in request.terms}
    if result.proof_bundle is None:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/proof_bundle")
    proof_terms = {item.term_ref_id: item.semantic_identity for item in result.proof_bundle.terms}
    expected = sorted(
        (
            request_terms[item.role_term_ref_id],
            request_terms[item.schema_term_ref_id],
        )
        for item in request.requested_outputs
    )
    actual: list[tuple[tuple[str, str, str, str], tuple[str, str, str, str]]] = []
    for document in result.output_documents:
        try:
            actual.append(
                (
                    proof_terms[document.role_term_ref_id],
                    proof_terms[document.schema_term_ref_id],
                )
            )
        except KeyError:
            _fail(IntentBridgeErrorCode.UNKNOWN_REFERENCE, "/output_documents")
    remaining = list(expected)
    for item in actual:
        if item not in remaining:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/output_documents")
        remaining.remove(item)
    if result.disposition is BridgeDisposition.COMPLETE and remaining:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/output_documents")


def validate_lowering_result(
    request: BackendLoweringRequest,
    result: BackendLoweringResult,
) -> None:
    """Close a lowering result to one exact adapter request."""

    if type(request) is not BackendLoweringRequest or type(result) is not BackendLoweringResult:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")
    if (
        not hmac.compare_digest(request.request_digest, result.request_digest)
        or request.adapter != result.adapter
    ):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/request_digest")
    if (
        result.plan_document is not None
        and result.plan_document.size_bytes > request.budget.max_output_bytes
    ):
        _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/plan_document")
    known_artifacts = {item.artifact_id for item in request.documents}
    if any(
        item.artifact_id not in known_artifacts
        for item in (*result.supported_subjects, *result.inert_subjects)
    ):
        _fail(IntentBridgeErrorCode.UNKNOWN_REFERENCE, "/supported_subjects")
    if len(result.supported_subjects) + len(result.inert_subjects) > MAX_SUBJECT_LOOKUPS:
        _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/supported_subjects")


__all__ = [
    "ArtifactReader",
    "DocumentValidationReport",
    "GraphCodec",
    "GraphCodecDescriptor",
    "IntentBackendAdapter",
    "IntentCompiler",
    "ProofValidationReport",
    "ResolvedSubject",
    "TrustedCodecRegistry",
    "TrustedProofPolicy",
    "ValidatedDocument",
    "read_verified_document",
    "resolve_subject",
    "validate_compile_result",
    "validate_documents",
    "validate_proof_bundle",
    "validate_lowering_result",
]
