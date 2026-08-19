"""Authority-free local contracts for trusted intent compilation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from vibecad.intent_bridge.contracts import (
    MAX_BRIDGE_DOCUMENTS,
    MAX_BRIDGE_TERMS,
    MAX_COMPILE_OUTPUTS,
    MAX_RULE_APPLICATIONS,
    MAX_SUBJECT_LOOKUPS,
    MAX_TOTAL_PAYLOAD_BYTES,
    BridgeTermRef,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    ProofAssertion,
    SubjectRef,
)

MAX_SELECTION_SUBJECTS = 256
MAX_RULE_SET_INPUTS = 32
MAX_RULE_SET_OUTPUTS = MAX_COMPILE_OUTPUTS
MAX_EMITTED_DOCUMENTS = MAX_COMPILE_OUTPUTS
MAX_RULE_DESCRIPTORS = 256

_MAX_IDENTIFIER_BYTES = 128
_MAX_MEDIA_TYPE_BYTES = 128
_MAX_VERSION_BYTES = 64
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_MEDIA_TYPE = re.compile(r"[a-z][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SEMANTIC_DOCUMENT_DOMAIN = b"vibecad.intent-compiler.emitted-document.v1\0"


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


def _bounded(value: object, path: str, *, maximum: int, pattern: re.Pattern[str]) -> str:
    if type(value) is not str:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    if (
        not value
        or value != value.strip()
        or not value.isprintable()
        or len(value.splitlines()) != 1
        or len(encoded) > maximum
        or pattern.fullmatch(value) is None
    ):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    return value


def _identifier(value: object, path: str) -> str:
    return _bounded(value, path, maximum=_MAX_IDENTIFIER_BYTES, pattern=_IDENTIFIER)


def _media_type(value: object, path: str) -> str:
    return _bounded(value, path, maximum=_MAX_MEDIA_TYPE_BYTES, pattern=_MEDIA_TYPE)


def _version(value: object, path: str) -> str:
    return _bounded(value, path, maximum=_MAX_VERSION_BYTES, pattern=_VERSION)


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    return value


def canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/canonical")


def semantic_term_mapping(term: BridgeTermRef) -> dict[str, str]:
    if type(term) is not BridgeTermRef:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/term")
    return {
        "namespace": term.namespace,
        "vocabulary_version": term.vocabulary_version,
        "term_id": term.term_id,
        "term_definition_sha256": term.term_definition_sha256,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentSignature:
    """Full semantic role/schema signature without local ref-id coupling."""

    role_term: BridgeTermRef
    schema_term: BridgeTermRef

    def __post_init__(self) -> None:
        if type(self.role_term) is not BridgeTermRef or type(self.schema_term) is not BridgeTermRef:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/document_signature")

    @property
    def semantic_identity(self) -> tuple[tuple[str, str, str, str], ...]:
        return (self.role_term.semantic_identity, self.schema_term.semantic_identity)

    def semantic_mapping(self) -> dict[str, object]:
        return {
            "role_term": semantic_term_mapping(self.role_term),
            "schema_term": semantic_term_mapping(self.schema_term),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class IntentSelection:
    """One exact, already-reviewed choice of a trusted rule set."""

    rule_set_term: BridgeTermRef
    decision_subjects: tuple[SubjectRef, ...]

    def __post_init__(self) -> None:
        if type(self.rule_set_term) is not BridgeTermRef:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/selection/rule_set_term")
        if type(self.decision_subjects) is not tuple or any(
            type(item) is not SubjectRef for item in self.decision_subjects
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/selection/decision_subjects")
        if not self.decision_subjects:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/selection/decision_subjects")
        if len(self.decision_subjects) > MAX_SELECTION_SUBJECTS:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/selection/decision_subjects")
        ordered = tuple(
            sorted(
                self.decision_subjects,
                key=lambda item: (
                    item.artifact_id,
                    item.selector_kind_term_ref_id,
                    item.selector_id,
                ),
            )
        )
        if len(set(ordered)) != len(ordered):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/selection/decision_subjects")
        object.__setattr__(self, "decision_subjects", ordered)


@dataclass(frozen=True, slots=True, kw_only=True)
class IntentRuleDescriptor:
    """Content-bound emitter rule represented in the independently checked proof."""

    rule_term: BridgeTermRef
    predicate_term: BridgeTermRef
    emitter_contract_sha256: str
    maximum_applications: int

    def __post_init__(self) -> None:
        if (
            type(self.rule_term) is not BridgeTermRef
            or type(self.predicate_term) is not BridgeTermRef
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/rule_descriptor")
        object.__setattr__(
            self,
            "emitter_contract_sha256",
            _digest(self.emitter_contract_sha256, "/rule_descriptor/emitter_contract_sha256"),
        )
        if (
            type(self.maximum_applications) is not int
            or not 1 <= self.maximum_applications <= MAX_RULE_APPLICATIONS
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/rule_descriptor/maximum_applications")

    def semantic_mapping(self) -> dict[str, object]:
        return {
            "rule_term": semantic_term_mapping(self.rule_term),
            "predicate_term": semantic_term_mapping(self.predicate_term),
            "emitter_contract_sha256": self.emitter_contract_sha256,
            "maximum_applications": self.maximum_applications,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class IntentRuleSetDescriptor:
    """Exact input/output signature and bounded rule inventory for one rule set."""

    rule_set_id: str
    rule_set_version: str
    rule_set_contract_sha256: str
    rule_set_term: BridgeTermRef
    input_signatures: tuple[DocumentSignature, ...]
    output_signatures: tuple[DocumentSignature, ...]
    rules: tuple[IntentRuleDescriptor, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_set_id", _identifier(self.rule_set_id, "/rule_set_id"))
        object.__setattr__(
            self,
            "rule_set_version",
            _version(self.rule_set_version, "/rule_set_version"),
        )
        object.__setattr__(
            self,
            "rule_set_contract_sha256",
            _digest(self.rule_set_contract_sha256, "/rule_set_contract_sha256"),
        )
        if type(self.rule_set_term) is not BridgeTermRef:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/rule_set_term")
        for name, values, maximum in (
            ("input_signatures", self.input_signatures, MAX_RULE_SET_INPUTS),
            ("output_signatures", self.output_signatures, MAX_RULE_SET_OUTPUTS),
        ):
            if (
                type(values) is not tuple
                or not values
                or len(values) > maximum
                or any(type(item) is not DocumentSignature for item in values)
            ):
                _fail(
                    IntentBridgeErrorCode.BUDGET_EXCEEDED
                    if type(values) is tuple and len(values) > maximum
                    else IntentBridgeErrorCode.INVALID_INPUT,
                    f"/{name}",
                )
            identities = tuple(item.semantic_identity for item in values)
            if len(set(identities)) != len(identities):
                _fail(IntentBridgeErrorCode.INVALID_INPUT, f"/{name}")
        if (
            type(self.rules) is not tuple
            or not self.rules
            or len(self.rules) > MAX_RULE_DESCRIPTORS
            or any(type(item) is not IntentRuleDescriptor for item in self.rules)
        ):
            _fail(
                IntentBridgeErrorCode.BUDGET_EXCEEDED
                if type(self.rules) is tuple and len(self.rules) > MAX_RULE_DESCRIPTORS
                else IntentBridgeErrorCode.INVALID_INPUT,
                "/rules",
            )
        rules = tuple(sorted(self.rules, key=lambda item: item.rule_term.semantic_identity))
        if len({item.rule_term.semantic_identity for item in rules}) != len(rules):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/rules")
        term_definitions: dict[tuple[str, str, str], str] = {}
        for term in (
            self.rule_set_term,
            *(
                value
                for item in self.input_signatures
                for value in (item.role_term, item.schema_term)
            ),
            *(
                value
                for item in self.output_signatures
                for value in (item.role_term, item.schema_term)
            ),
            *(value for item in rules for value in (item.rule_term, item.predicate_term)),
        ):
            name = term.semantic_identity[:3]
            prior = term_definitions.setdefault(name, term.term_definition_sha256)
            if prior != term.term_definition_sha256:
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/terms")
        object.__setattr__(self, "rules", rules)

    def semantic_mapping(self) -> dict[str, object]:
        return {
            "rule_set_id": self.rule_set_id,
            "rule_set_version": self.rule_set_version,
            "rule_set_contract_sha256": self.rule_set_contract_sha256,
            "rule_set_term": semantic_term_mapping(self.rule_set_term),
            "input_signatures": [item.semantic_mapping() for item in self.input_signatures],
            "output_signatures": [item.semantic_mapping() for item in self.output_signatures],
            "rules": [item.semantic_mapping() for item in self.rules],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CompiledIntentDocument:
    """Canonical emitted bytes plus their complete immutable document reference."""

    output_id: str
    document: DocumentRef
    payload: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_id", _identifier(self.output_id, "/output_id"))
        if type(self.document) is not DocumentRef or type(self.payload) is not bytes:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/emitted_document")
        if not self.payload:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/emitted_document/payload")
        if len(self.payload) > MAX_TOTAL_PAYLOAD_BYTES:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/emitted_document/payload")
        if (
            len(self.payload) != self.document.size_bytes
            or hashlib.sha256(self.payload).hexdigest() != self.document.content_sha256
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/emitted_document/payload")

    @classmethod
    def create(
        cls,
        *,
        output_id: str,
        artifact_id: str,
        role_term_ref_id: str,
        schema_term_ref_id: str,
        document_id: str,
        document_digest: str,
        media_type: str,
        payload: bytes,
    ) -> CompiledIntentDocument:
        if type(payload) is not bytes:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/payload")
        if not payload:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/payload")
        if len(payload) > MAX_TOTAL_PAYLOAD_BYTES:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/payload")
        _media_type(media_type, "/media_type")
        return cls(
            output_id=output_id,
            document=DocumentRef(
                artifact_id=artifact_id,
                role_term_ref_id=role_term_ref_id,
                schema_term_ref_id=schema_term_ref_id,
                document_id=document_id,
                document_digest=document_digest,
                content_sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                media_type=media_type,
            ),
            payload=payload,
        )

    @staticmethod
    def default_document_digest(*, schema_term: BridgeTermRef, payload: bytes) -> str:
        if type(schema_term) is not BridgeTermRef or type(payload) is not bytes or not payload:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/document_digest")
        return hashlib.sha256(
            _SEMANTIC_DOCUMENT_DOMAIN
            + canonical_bytes(semantic_term_mapping(schema_term))
            + b"\0"
            + payload
        ).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleSetEmission:
    """Complete candidate documents and proof components before publication."""

    documents: tuple[CompiledIntentDocument, ...]
    terms: tuple[BridgeTermRef, ...]
    assertions: tuple[ProofAssertion, ...]

    def __post_init__(self) -> None:
        if (
            type(self.documents) is not tuple
            or not self.documents
            or len(self.documents) > MAX_EMITTED_DOCUMENTS
            or any(type(item) is not CompiledIntentDocument for item in self.documents)
        ):
            _fail(
                IntentBridgeErrorCode.BUDGET_EXCEEDED
                if type(self.documents) is tuple and len(self.documents) > MAX_EMITTED_DOCUMENTS
                else IntentBridgeErrorCode.INVALID_INPUT,
                "/emission/documents",
            )
        if len({item.output_id for item in self.documents}) != len(self.documents) or len(
            {item.document.artifact_id for item in self.documents}
        ) != len(self.documents):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/emission/documents")
        if (
            type(self.terms) is not tuple
            or not self.terms
            or len(self.terms) > MAX_BRIDGE_TERMS
            or any(type(item) is not BridgeTermRef for item in self.terms)
        ):
            _fail(
                IntentBridgeErrorCode.BUDGET_EXCEEDED
                if type(self.terms) is tuple and len(self.terms) > MAX_BRIDGE_TERMS
                else IntentBridgeErrorCode.INVALID_INPUT,
                "/emission/terms",
            )
        terms = tuple(sorted(self.terms, key=lambda item: item.term_ref_id))
        if len({item.term_ref_id for item in terms}) != len(terms):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/emission/terms")
        if (
            type(self.assertions) is not tuple
            or not self.assertions
            or len(self.assertions) > MAX_RULE_APPLICATIONS
            or any(type(item) is not ProofAssertion for item in self.assertions)
        ):
            _fail(
                IntentBridgeErrorCode.BUDGET_EXCEEDED
                if type(self.assertions) is tuple and len(self.assertions) > MAX_RULE_APPLICATIONS
                else IntentBridgeErrorCode.INVALID_INPUT,
                "/emission/assertions",
            )
        assertions = tuple(sorted(self.assertions, key=lambda item: item.assertion_id))
        if len({item.assertion_id for item in assertions}) != len(assertions):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/emission/assertions")
        object.__setattr__(
            self,
            "documents",
            tuple(sorted(self.documents, key=lambda item: item.document.artifact_id)),
        )
        object.__setattr__(self, "terms", terms)
        object.__setattr__(self, "assertions", assertions)


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleSetCompileContext:
    """Exact canonical request inputs supplied to one reviewed rule set."""

    request_digest: str
    terms: tuple[BridgeTermRef, ...]
    input_documents: tuple[tuple[DocumentRef, bytes], ...]
    requested_outputs: tuple[tuple[str, DocumentSignature], ...]
    selection: IntentSelection
    max_output_bytes: int
    max_subject_lookups: int
    max_rule_applications: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_digest", _digest(self.request_digest, "/request_digest"))
        if (
            type(self.terms) is not tuple
            or len(self.terms) > MAX_BRIDGE_TERMS
            or any(type(item) is not BridgeTermRef for item in self.terms)
        ):
            _fail(
                IntentBridgeErrorCode.BUDGET_EXCEEDED
                if type(self.terms) is tuple and len(self.terms) > MAX_BRIDGE_TERMS
                else IntentBridgeErrorCode.INVALID_INPUT,
                "/context/terms",
            )
        if len({item.term_ref_id for item in self.terms}) != len(self.terms):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/context/terms")
        if (
            type(self.input_documents) is not tuple
            or not self.input_documents
            or len(self.input_documents) > MAX_BRIDGE_DOCUMENTS
        ):
            _fail(
                IntentBridgeErrorCode.BUDGET_EXCEEDED
                if type(self.input_documents) is tuple
                and len(self.input_documents) > MAX_BRIDGE_DOCUMENTS
                else IntentBridgeErrorCode.INVALID_INPUT,
                "/context/input_documents",
            )
        if any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not DocumentRef
            or type(item[1]) is not bytes
            for item in self.input_documents
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/context/input_documents")
        if any(
            len(payload) != document.size_bytes
            or hashlib.sha256(payload).hexdigest() != document.content_sha256
            for document, payload in self.input_documents
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/context/input_documents")
        if (
            type(self.requested_outputs) is not tuple
            or not self.requested_outputs
            or len(self.requested_outputs) > MAX_RULE_SET_OUTPUTS
            or any(
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not DocumentSignature
                for item in self.requested_outputs
            )
        ):
            _fail(
                IntentBridgeErrorCode.BUDGET_EXCEEDED
                if type(self.requested_outputs) is tuple
                and len(self.requested_outputs) > MAX_RULE_SET_OUTPUTS
                else IntentBridgeErrorCode.INVALID_INPUT,
                "/context/requested_outputs",
            )
        output_ids = tuple(
            _identifier(item[0], "/context/requested_outputs") for item in self.requested_outputs
        )
        if len(set(output_ids)) != len(output_ids):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/context/requested_outputs")
        if type(self.selection) is not IntentSelection:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/context/selection")
        maxima = (
            (self.max_output_bytes, MAX_TOTAL_PAYLOAD_BYTES, "/context/max_output_bytes"),
            (self.max_subject_lookups, MAX_SUBJECT_LOOKUPS, "/context/max_subject_lookups"),
            (self.max_rule_applications, MAX_RULE_APPLICATIONS, "/context/max_rule_applications"),
        )
        if any(type(value) is not int or not 1 <= value <= maximum for value, maximum, _ in maxima):
            path = next(
                path
                for value, maximum, path in maxima
                if type(value) is not int or not 1 <= value <= maximum
            )
            _fail(IntentBridgeErrorCode.INVALID_INPUT, path)


__all__ = [
    "CompiledIntentDocument",
    "DocumentSignature",
    "IntentRuleDescriptor",
    "IntentRuleSetDescriptor",
    "IntentSelection",
    "RuleSetCompileContext",
    "RuleSetEmission",
    "MAX_RULE_DESCRIPTORS",
    "canonical_bytes",
    "semantic_term_mapping",
]
