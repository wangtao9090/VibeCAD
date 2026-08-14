"""Private shared lowering seam for statically reviewed backend families.

The model-visible graph may name ontology terms, but it cannot select native
types, properties, or implementation code.  Only :class:`ReviewedOperationSpec`
instances constructed by trusted host code carry that mapping.  This module
validates one exact intent document and one exact capability document, delegates
family semantics to injected trusted callbacks, and publishes an authority-free
canonical plan through the existing atomic :class:`PlanSink` boundary.

This is intentionally not exported from ``intent_bridge.__init__`` and does not
change the public bridge wire.  It is a reuse seam for future private adapters,
not a dynamic plugin or execution surface.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from vibecad.intent_bridge.contracts import (
    MAX_BRIDGE_TERMS,
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
    SubjectRef,
)
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.ports import (
    ArtifactReader,
    TrustedCodecRegistry,
    TrustedProofPolicy,
    read_verified_document,
    validate_lowering_result,
    validate_proof_bundle,
)

MAX_REVIEWED_OPERATIONS: Final = 128
MAX_REVIEWED_PROPERTIES: Final = 128
MAX_FAMILY_MANIFEST_BYTES: Final = 256 * 1024

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]*\Z")
_MEDIA_TYPE = re.compile(r"[a-z][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*\Z")
_NATIVE_TYPE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]*::[A-Za-z][A-Za-z0-9_.]*\Z")
_NATIVE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

_OPERATION_SPEC_DIGEST_DOMAIN = b"vibecad.reviewed-family.operation-spec.v1\0"
_MANIFEST_DIGEST_DOMAIN = b"vibecad.reviewed-family.manifest.v1\0"
_PLAN_DOCUMENT_DIGEST_DOMAIN = b"vibecad.reviewed-family.plan-document.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.reviewed-family.receipt.v1\0"


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


def _bounded_match(
    value: object,
    path: str,
    *,
    maximum: int,
    pattern: re.Pattern[str],
) -> str:
    if type(value) is not str:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    if not 1 <= size <= maximum or pattern.fullmatch(value) is None:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    return value


def _identifier(value: object, path: str) -> str:
    return _bounded_match(value, path, maximum=128, pattern=_IDENTIFIER)


def _version(value: object, path: str) -> str:
    return _bounded_match(value, path, maximum=64, pattern=_VERSION)


def _media_type(value: object, path: str) -> str:
    return _bounded_match(value, path, maximum=128, pattern=_MEDIA_TYPE)


def _native_type_id(value: object, path: str) -> str:
    return _bounded_match(value, path, maximum=128, pattern=_NATIVE_TYPE_ID)


def _native_name(value: object, path: str) -> str:
    return _bounded_match(value, path, maximum=128, pattern=_NATIVE_NAME)


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    return value


def _canonical_json(value: object, *, maximum: int) -> bytes:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/manifest")
    if len(payload) > maximum:
        _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/manifest")
    return payload


def _subject_key(subject: SubjectRef) -> tuple[str, str, str]:
    return (
        subject.artifact_id,
        subject.selector_kind_term_ref_id,
        subject.selector_id,
    )


def _trusted_call(path: str, callback: Callable[..., object], *args: object, **kwargs: object):
    """Bound trusted-callback failures without swallowing user cancellation."""

    try:
        return callback(*args, **kwargs)
    except IntentBridgeError:
        raise
    except (Exception, SystemExit):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, path)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedOperationSpec:
    """One static semantic-identity to native-operation mapping.

    ``native_*`` values must be literals supplied by reviewed host code.  An
    adapter resolves them only through the complete ontology identity; graph
    strings and local ``term_ref_id`` values never select native code.
    """

    operation_id: str
    semantic_term: BridgeTermRef
    native_type_id: str
    native_operation: str
    native_property_names: tuple[str, ...] = ()
    specification_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_id", _identifier(self.operation_id, "/operation_id"))
        if type(self.semantic_term) is not BridgeTermRef:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/semantic_term")
        object.__setattr__(
            self,
            "native_type_id",
            _native_type_id(self.native_type_id, "/native_type_id"),
        )
        object.__setattr__(
            self,
            "native_operation",
            _identifier(self.native_operation, "/native_operation"),
        )
        properties = self.native_property_names
        if type(properties) is not tuple or len(properties) > MAX_REVIEWED_PROPERTIES:
            code = (
                IntentBridgeErrorCode.BUDGET_EXCEEDED
                if type(properties) is tuple
                else IntentBridgeErrorCode.INVALID_INPUT
            )
            _fail(code, "/native_property_names")
        checked = tuple(_native_name(item, "/native_property_names/item") for item in properties)
        if len(set(checked)) != len(checked):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/native_property_names")
        object.__setattr__(self, "native_property_names", tuple(sorted(checked)))
        digest = hashlib.sha256(
            _OPERATION_SPEC_DIGEST_DOMAIN + _canonical_json(self.to_mapping(), maximum=16 * 1024)
        ).hexdigest()
        object.__setattr__(self, "specification_sha256", digest)

    def to_mapping(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "semantic_term": self.semantic_term.to_mapping(),
            "native": {
                "type_id": self.native_type_id,
                "operation": self.native_operation,
                "property_names": list(self.native_property_names),
            },
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FamilyBatchManifest:
    """Canonical reviewed inventory and exact document contract for one family."""

    family_id: str
    family_version: str
    adapter: AdapterDescriptor
    backend_engine: str
    backend_version: str
    backend_build_id: str
    rule_id: str
    rule_contract_sha256: str
    intent_role_term: BridgeTermRef
    intent_schema_term: BridgeTermRef
    intent_media_type: str
    capability_role_term: BridgeTermRef
    capability_schema_term: BridgeTermRef
    capability_media_type: str
    plan_role_term: BridgeTermRef
    plan_schema_term: BridgeTermRef
    plan_media_type: str
    request_terms: tuple[BridgeTermRef, ...]
    operations: tuple[ReviewedOperationSpec, ...]
    max_plan_bytes: int
    manifest_sha256: str = field(init=False)
    canonical_bytes: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "family_id", _identifier(self.family_id, "/family_id"))
        object.__setattr__(
            self,
            "family_version",
            _version(self.family_version, "/family_version"),
        )
        if type(self.adapter) is not AdapterDescriptor:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/adapter")
        object.__setattr__(
            self,
            "backend_engine",
            _identifier(self.backend_engine, "/backend_engine"),
        )
        object.__setattr__(
            self,
            "backend_version",
            _version(self.backend_version, "/backend_version"),
        )
        object.__setattr__(
            self,
            "backend_build_id",
            _digest(self.backend_build_id, "/backend_build_id"),
        )
        object.__setattr__(self, "rule_id", _identifier(self.rule_id, "/rule_id"))
        object.__setattr__(
            self,
            "rule_contract_sha256",
            _digest(self.rule_contract_sha256, "/rule_contract_sha256"),
        )
        term_fields = (
            "intent_role_term",
            "intent_schema_term",
            "capability_role_term",
            "capability_schema_term",
            "plan_role_term",
            "plan_schema_term",
        )
        if any(type(getattr(self, name)) is not BridgeTermRef for name in term_fields):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/document_terms")
        for name in ("intent_media_type", "capability_media_type", "plan_media_type"):
            object.__setattr__(self, name, _media_type(getattr(self, name), f"/{name}"))
        terms = self.request_terms
        if (
            type(terms) is not tuple
            or not terms
            or len(terms) > MAX_BRIDGE_TERMS
            or any(type(item) is not BridgeTermRef for item in terms)
        ):
            code = (
                IntentBridgeErrorCode.BUDGET_EXCEEDED
                if type(terms) is tuple and len(terms) > MAX_BRIDGE_TERMS
                else IntentBridgeErrorCode.INVALID_INPUT
            )
            _fail(code, "/request_terms")
        terms = tuple(sorted(terms, key=lambda item: item.term_ref_id))
        if len({item.term_ref_id for item in terms}) != len(terms) or len(
            {item.semantic_identity[:3] for item in terms}
        ) != len(terms):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/request_terms")
        operations = self.operations
        if (
            type(operations) is not tuple
            or not operations
            or len(operations) > MAX_REVIEWED_OPERATIONS
            or any(type(item) is not ReviewedOperationSpec for item in operations)
        ):
            code = (
                IntentBridgeErrorCode.BUDGET_EXCEEDED
                if type(operations) is tuple and len(operations) > MAX_REVIEWED_OPERATIONS
                else IntentBridgeErrorCode.INVALID_INPUT
            )
            _fail(code, "/operations")
        operations = tuple(sorted(operations, key=lambda item: item.operation_id))
        if (
            len({item.operation_id for item in operations}) != len(operations)
            or len({item.semantic_term.semantic_identity for item in operations}) != len(operations)
            or len({item.specification_sha256 for item in operations}) != len(operations)
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/operations")
        required_terms = (
            *(getattr(self, name) for name in term_fields),
            *(item.semantic_term for item in operations),
        )
        term_by_ref = {item.term_ref_id: item for item in terms}
        if any(term_by_ref.get(item.term_ref_id) != item for item in required_terms):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/request_terms")
        if (
            type(self.max_plan_bytes) is not int
            or not 1 <= self.max_plan_bytes <= MAX_TOTAL_PAYLOAD_BYTES
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/max_plan_bytes")
        object.__setattr__(self, "request_terms", terms)
        object.__setattr__(self, "operations", operations)
        payload = _canonical_json(self.to_mapping(), maximum=MAX_FAMILY_MANIFEST_BYTES)
        object.__setattr__(self, "canonical_bytes", payload)
        object.__setattr__(
            self,
            "manifest_sha256",
            hashlib.sha256(_MANIFEST_DIGEST_DOMAIN + payload).hexdigest(),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "authority": "none",
            "family": {"id": self.family_id, "version": self.family_version},
            "adapter": self.adapter.to_mapping(),
            "backend": {
                "engine": self.backend_engine,
                "version": self.backend_version,
                "build_id": self.backend_build_id,
            },
            "rule": {
                "rule_id": self.rule_id,
                "rule_contract_sha256": self.rule_contract_sha256,
            },
            "documents": {
                "intent": {
                    "role_term": self.intent_role_term.to_mapping(),
                    "schema_term": self.intent_schema_term.to_mapping(),
                    "media_type": self.intent_media_type,
                },
                "capability": {
                    "role_term": self.capability_role_term.to_mapping(),
                    "schema_term": self.capability_schema_term.to_mapping(),
                    "media_type": self.capability_media_type,
                },
                "plan": {
                    "role_term": self.plan_role_term.to_mapping(),
                    "schema_term": self.plan_schema_term.to_mapping(),
                    "media_type": self.plan_media_type,
                    "max_bytes": self.max_plan_bytes,
                },
            },
            "request_terms": [item.to_mapping() for item in self.request_terms],
            "operations": [item.to_mapping() for item in self.operations],
        }

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    def operation_for_identity(
        self, identity: tuple[str, str, str, str]
    ) -> ReviewedOperationSpec | None:
        if (
            type(identity) is not tuple
            or len(identity) != 4
            or any(type(item) is not str for item in identity)
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/operation_identity")
        return next(
            (item for item in self.operations if item.semantic_term.semantic_identity == identity),
            None,
        )

    def operation_for_term(self, term: BridgeTermRef) -> ReviewedOperationSpec | None:
        if type(term) is not BridgeTermRef:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/operation_term")
        return self.operation_for_identity(term.semantic_identity)

    def capability_document(
        self,
        *,
        artifact_id: str = "artifact_reviewed_family_capability",
    ) -> tuple[DocumentRef, bytes]:
        artifact_id = _identifier(artifact_id, "/artifact_id")
        payload = self.canonical_bytes
        return (
            DocumentRef(
                artifact_id=artifact_id,
                role_term_ref_id=self.capability_role_term.term_ref_id,
                schema_term_ref_id=self.capability_schema_term.term_ref_id,
                document_id=f"reviewed_family_capability_{self.manifest_sha256[:32]}",
                document_digest=self.manifest_sha256,
                content_sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                media_type=self.capability_media_type,
            ),
            payload,
        )

    def plan_document(self, payload: bytes, semantic_plan_sha256: str) -> DocumentRef:
        if type(payload) is not bytes or not payload:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/plan_document/payload")
        if len(payload) > self.max_plan_bytes:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/plan_document/payload")
        semantic_plan_sha256 = _digest(semantic_plan_sha256, "/plan_document/document_digest")
        content_sha256 = hashlib.sha256(payload).hexdigest()
        identifier_digest = hashlib.sha256(
            b"\0".join(
                (
                    _PLAN_DOCUMENT_DIGEST_DOMAIN,
                    bytes.fromhex(self.manifest_sha256),
                    bytes.fromhex(semantic_plan_sha256),
                    bytes.fromhex(content_sha256),
                )
            )
        ).hexdigest()
        return DocumentRef(
            artifact_id=f"artifact_reviewed_plan_{identifier_digest[:32]}",
            role_term_ref_id=self.plan_role_term.term_ref_id,
            schema_term_ref_id=self.plan_schema_term.term_ref_id,
            document_id=f"reviewed_plan_{identifier_digest[:32]}",
            document_digest=semantic_plan_sha256,
            content_sha256=content_sha256,
            size_bytes=len(payload),
            media_type=self.plan_media_type,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedPlanDraft:
    """Family callback output before publication."""

    payload: bytes
    semantic_plan_sha256: str
    operation_term: BridgeTermRef
    subjects: tuple[SubjectRef, ...]

    def __post_init__(self) -> None:
        if type(self.payload) is not bytes or not self.payload:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/plan_draft/payload")
        if len(self.payload) > MAX_TOTAL_PAYLOAD_BYTES:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/plan_draft/payload")
        object.__setattr__(
            self,
            "semantic_plan_sha256",
            _digest(self.semantic_plan_sha256, "/plan_draft/semantic_plan_sha256"),
        )
        if type(self.operation_term) is not BridgeTermRef:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/plan_draft/operation_term")
        subjects = self.subjects
        if (
            type(subjects) is not tuple
            or not subjects
            or len(subjects) > MAX_SUBJECT_LOOKUPS
            or any(type(item) is not SubjectRef for item in subjects)
        ):
            code = (
                IntentBridgeErrorCode.BUDGET_EXCEEDED
                if type(subjects) is tuple and len(subjects) > MAX_SUBJECT_LOOKUPS
                else IntentBridgeErrorCode.INVALID_INPUT
            )
            _fail(code, "/plan_draft/subjects")
        subjects = tuple(sorted(subjects, key=_subject_key))
        if len(set(subjects)) != len(subjects):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/plan_draft/subjects")
        object.__setattr__(self, "subjects", subjects)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedPlanReceipt:
    """Content-bound read receipt; never an execution or adoption grant."""

    manifest_sha256: str
    request_digest: str
    adapter: AdapterDescriptor
    operation: ReviewedOperationSpec
    source_document: DocumentRef
    plan_document: DocumentRef
    receipt_id: str = field(init=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "manifest_sha256",
            _digest(self.manifest_sha256, "/receipt/manifest_sha256"),
        )
        object.__setattr__(
            self,
            "request_digest",
            _digest(self.request_digest, "/receipt/request_digest"),
        )
        if (
            type(self.adapter) is not AdapterDescriptor
            or type(self.operation) is not ReviewedOperationSpec
            or type(self.source_document) is not DocumentRef
            or type(self.plan_document) is not DocumentRef
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/receipt")
        body = {
            "authority": "none",
            "manifest_sha256": self.manifest_sha256,
            "request_digest": self.request_digest,
            "adapter": self.adapter.to_mapping(),
            "operation_specification_sha256": self.operation.specification_sha256,
            "source_document": self.source_document.to_mapping(),
            "plan_document": self.plan_document.to_mapping(),
        }
        digest = hashlib.sha256(
            _RECEIPT_DIGEST_DOMAIN + _canonical_json(body, maximum=32 * 1024)
        ).hexdigest()
        object.__setattr__(self, "receipt_sha256", digest)
        object.__setattr__(self, "receipt_id", f"reviewed_plan_receipt_{digest[:32]}")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


PlanBuilder = Callable[[DocumentRef, bytes, str, FamilyBatchManifest], ReviewedPlanDraft]
PlanDecoder = Callable[..., object]
PlanBindingValidator = Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]


class ExactReviewedFamilyAdapter:
    """Generic exact lowerer driven only by one trusted static manifest."""

    __slots__ = ("_build_plan", "_decode_plan", "_manifest", "_sink", "_validate_binding")

    def __init__(
        self,
        manifest: FamilyBatchManifest,
        sink: PlanSink,
        *,
        build_plan: PlanBuilder,
        decode_plan: PlanDecoder,
        validate_binding: PlanBindingValidator,
    ) -> None:
        if type(manifest) is not FamilyBatchManifest:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/manifest")
        if not isinstance(sink, PlanSink):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/plan_sink")
        if not callable(build_plan) or not callable(decode_plan) or not callable(validate_binding):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/callbacks")
        self._manifest = manifest
        self._sink = sink
        self._build_plan = build_plan
        self._decode_plan = decode_plan
        self._validate_binding = validate_binding

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._manifest.adapter

    @property
    def manifest(self) -> FamilyBatchManifest:
        return self._manifest

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    def lower(
        self,
        request: BackendLoweringRequest,
        *,
        artifacts: ArtifactReader,
        codecs: TrustedCodecRegistry,
        proof_policy: TrustedProofPolicy,
    ) -> BackendLoweringResult:
        return self.lower_with_receipt(
            request,
            artifacts=artifacts,
            codecs=codecs,
            proof_policy=proof_policy,
        )[0]

    def lower_with_receipt(
        self,
        request: BackendLoweringRequest,
        *,
        artifacts: ArtifactReader,
        codecs: TrustedCodecRegistry,
        proof_policy: TrustedProofPolicy,
    ) -> tuple[BackendLoweringResult, ReviewedPlanReceipt]:
        manifest = self._manifest
        if type(request) is not BackendLoweringRequest or request.adapter != self.descriptor:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/adapter")
        if type(codecs) is not TrustedCodecRegistry:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/codecs")
        request_terms = {item.term_ref_id: item for item in request.terms}
        if any(request_terms.get(item.term_ref_id) != item for item in manifest.request_terms):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/terms")
        if (
            len(request.documents) != 2
            or len(request.intent_artifact_ids) != 1
            or len(request.capability_artifact_ids) != 1
            or request.intent_artifact_ids == request.capability_artifact_ids
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/request/scope")
        if (
            sum(document.size_bytes for document in request.documents)
            > request.budget.max_input_bytes
            or len(request.proof_bundle.assertions) > request.budget.max_rule_applications
        ):
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/request/scope")
        documents = {item.artifact_id: item for item in request.documents}
        intent_document = documents[request.intent_artifact_ids[0]]
        capability_document = documents[request.capability_artifact_ids[0]]

        def document_matches(
            document: DocumentRef,
            role: BridgeTermRef,
            schema: BridgeTermRef,
            media_type: str,
        ) -> bool:
            return (
                request_terms.get(document.role_term_ref_id) == role
                and request_terms.get(document.schema_term_ref_id) == schema
                and document.media_type == media_type
            )

        if not document_matches(
            intent_document,
            manifest.intent_role_term,
            manifest.intent_schema_term,
            manifest.intent_media_type,
        ) or not document_matches(
            capability_document,
            manifest.capability_role_term,
            manifest.capability_schema_term,
            manifest.capability_media_type,
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/documents")
        expected_capability, expected_payload = manifest.capability_document(
            artifact_id=capability_document.artifact_id
        )
        capability_payload = _trusted_call(
            "/capability_document",
            read_verified_document,
            artifacts,
            capability_document,
            maximum_bytes=request.budget.max_input_bytes - intent_document.size_bytes,
        )
        if (
            capability_document != expected_capability
            or type(capability_payload) is not bytes
            or not hmac.compare_digest(capability_payload, expected_payload)
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/capability_document")
        report = _trusted_call(
            "/proof_bundle",
            validate_proof_bundle,
            request.proof_bundle,
            reader=artifacts,
            codecs=codecs,
            proof_policy=proof_policy,
            maximum_total_bytes=intent_document.size_bytes,
            maximum_subject_lookups=request.budget.max_subject_lookups,
        )
        if (
            report.disposition is not BridgeDisposition.COMPLETE
            or len(report.documents.validated) != 1
            or report.documents.validated[0].document != intent_document
            or report.documents.inert_artifact_ids
            or report.inert_subjects
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/proof_bundle")
        source_payload = report.documents.validated[0].payload
        draft = _trusted_call(
            "/intent_document",
            self._build_plan,
            intent_document,
            source_payload,
            request.request_digest,
            manifest,
        )
        if type(draft) is not ReviewedPlanDraft:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_draft")
        operation = manifest.operation_for_term(draft.operation_term)
        if operation is None:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/plan_draft/operation_term")
        resolved_subjects = tuple(
            sorted((item.subject for item in report.resolved_subjects), key=_subject_key)
        )
        if resolved_subjects != draft.subjects:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/proof_bundle/target")
        if len(draft.payload) > min(request.budget.max_output_bytes, manifest.max_plan_bytes):
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/plan_document")
        plan_document = manifest.plan_document(draft.payload, draft.semantic_plan_sha256)
        decoded = _trusted_call(
            "/plan_document",
            self._decode_plan,
            draft.payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
        result = BackendLoweringResult(
            request_digest=request.request_digest,
            adapter=self.descriptor,
            disposition=BridgeDisposition.COMPLETE,
            plan_document=plan_document,
            supported_subjects=draft.subjects,
        )
        validate_lowering_result(request, result)
        receipt = ReviewedPlanReceipt(
            manifest_sha256=manifest.manifest_sha256,
            request_digest=request.request_digest,
            adapter=self.descriptor,
            operation=operation,
            source_document=intent_document,
            plan_document=plan_document,
        )
        _trusted_call(
            "/plan_document/binding",
            self._validate_binding,
            decoded,
            receipt,
            operation,
        )
        published = _trusted_call(
            "/plan_sink",
            self._sink.publish_exact,
            plan_document,
            draft.payload,
        )
        if type(published) is not bytes or not hmac.compare_digest(published, draft.payload):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink/readback")
        return result, receipt

    def read_plan(self, receipt: ReviewedPlanReceipt) -> tuple[object, bytes]:
        manifest = self._manifest
        if (
            type(receipt) is not ReviewedPlanReceipt
            or receipt.adapter != self.descriptor
            or not hmac.compare_digest(receipt.manifest_sha256, manifest.manifest_sha256)
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/receipt")
        operation = next(
            (
                item
                for item in manifest.operations
                if item.operation_id == receipt.operation.operation_id
            ),
            None,
        )
        if operation is None or operation != receipt.operation:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/receipt/operation")
        document = receipt.plan_document
        if (
            document.role_term_ref_id != manifest.plan_role_term.term_ref_id
            or document.schema_term_ref_id != manifest.plan_schema_term.term_ref_id
            or document.media_type != manifest.plan_media_type
            or document.size_bytes > manifest.max_plan_bytes
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/receipt/plan_document")
        payload = _trusted_call(
            "/plan_sink",
            self._sink.read_exact,
            document,
            manifest.max_plan_bytes,
        )
        if (
            type(payload) is not bytes
            or len(payload) != document.size_bytes
            or len(payload) > manifest.max_plan_bytes
            or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), document.content_sha256)
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink/readback")
        expected_document = manifest.plan_document(payload, document.document_digest)
        if document != expected_document:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink/readback")
        decoded = _trusted_call(
            "/plan_document",
            self._decode_plan,
            payload,
            expected_content_sha256=document.content_sha256,
            expected_plan_sha256=document.document_digest,
        )
        _trusted_call(
            "/receipt/binding",
            self._validate_binding,
            decoded,
            receipt,
            operation,
        )
        return decoded, payload


__all__ = [
    "MAX_FAMILY_MANIFEST_BYTES",
    "MAX_REVIEWED_OPERATIONS",
    "MAX_REVIEWED_PROPERTIES",
    "ExactReviewedFamilyAdapter",
    "FamilyBatchManifest",
    "PlanBindingValidator",
    "PlanBuilder",
    "PlanDecoder",
    "ReviewedOperationSpec",
    "ReviewedPlanDraft",
    "ReviewedPlanReceipt",
]
