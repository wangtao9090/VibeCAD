"""Host-injected semantic validators for backend-neutral proof bundles.

Proof terms are open data and never select Python code.  The host constructs a
``TrustedRulePolicy`` from reviewed evaluator objects.  Evaluators are keyed by
the complete content-bound identity of their rule term and must validate the
actual semantic fact represented by an assertion.  Merely matching endpoint
shapes is not enough.

This module remains authority-free with respect to CAD execution: successful
validation only proves that an evidence-only ``ProofBundle`` satisfies the
injected semantic policy.  Lowering and runtime execution remain separate,
nominally injected boundaries.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from vibecad.intent_bridge.contracts import (
    MAX_BRIDGE_ENVELOPE_BYTES,
    MAX_RULE_APPLICATIONS,
    MAX_SUBJECTS_PER_ASSERTION,
    BridgeTermRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    ProducerDescriptor,
    ProofAssertion,
    ProofBundle,
    ProofEndpoint,
    SubjectRef,
)
from vibecad.intent_bridge.ports import ResolvedSubject, ValidatedDocument

MAX_TRUSTED_RULE_EVALUATORS = 512
TRUSTED_RULE_POLICY_SCHEMA_VERSION = 1

_CATALOG_DOMAIN = b"vibecad.intent-bridge.trusted-rule-policy.v1\0"


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


def _semantic_mapping(term: BridgeTermRef) -> dict[str, str]:
    return {
        "namespace": term.namespace,
        "vocabulary_version": term.vocabulary_version,
        "term_id": term.term_id,
        "term_definition_sha256": term.term_definition_sha256,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleEndpointSignature:
    """Exact ontology selector, role, and subject type for one endpoint."""

    selector_kind_term: BridgeTermRef
    role_term: BridgeTermRef
    subject_type_term: BridgeTermRef

    def __post_init__(self) -> None:
        if any(
            type(item) is not BridgeTermRef
            for item in (
                self.selector_kind_term,
                self.role_term,
                self.subject_type_term,
            )
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/endpoint_signature")

    def semantic_mapping(self) -> dict[str, object]:
        return {
            "selector_kind_term": _semantic_mapping(self.selector_kind_term),
            "role_term": _semantic_mapping(self.role_term),
            "subject_type_term": _semantic_mapping(self.subject_type_term),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class TrustedRuleEvaluatorDescriptor:
    """Host-local binding from one exact semantic rule to reviewed code."""

    evaluator_id: str
    evaluator_version: str
    evaluator_contract_sha256: str
    rule_term: BridgeTermRef
    predicate_term: BridgeTermRef
    premises: tuple[RuleEndpointSignature, ...]
    conclusions: tuple[RuleEndpointSignature, ...]

    def __post_init__(self) -> None:
        ProducerDescriptor(
            producer_id=self.evaluator_id,
            producer_version=self.evaluator_version,
            producer_contract_sha256=self.evaluator_contract_sha256,
            rule_catalog_sha256=self.evaluator_contract_sha256,
        )
        if (
            type(self.rule_term) is not BridgeTermRef
            or type(self.predicate_term) is not BridgeTermRef
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/evaluator_descriptor")
        if type(self.premises) is not tuple or type(self.conclusions) is not tuple:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/evaluator_descriptor/endpoints")
        endpoints = (*self.premises, *self.conclusions)
        if (
            not self.premises
            or not self.conclusions
            or any(type(item) is not RuleEndpointSignature for item in endpoints)
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/evaluator_descriptor/endpoints")
        if len(endpoints) > MAX_SUBJECTS_PER_ASSERTION:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/evaluator_descriptor/endpoints")

    def semantic_mapping(self) -> dict[str, object]:
        return {
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "evaluator_contract_sha256": self.evaluator_contract_sha256,
            "rule_term": _semantic_mapping(self.rule_term),
            "predicate_term": _semantic_mapping(self.predicate_term),
            "premises": [item.semantic_mapping() for item in self.premises],
            "conclusions": [item.semantic_mapping() for item in self.conclusions],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class TrustedRuleEvaluation:
    """Read-only exact context given to one reviewed semantic evaluator."""

    bundle: ProofBundle
    assertion: ProofAssertion
    documents: tuple[ValidatedDocument, ...]
    premises: tuple[ResolvedSubject, ...]
    conclusions: tuple[ResolvedSubject, ...]

    def __post_init__(self) -> None:
        if type(self.bundle) is not ProofBundle or type(self.assertion) is not ProofAssertion:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/rule_evaluation")
        if self.assertion not in self.bundle.assertions:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/rule_evaluation/assertion")
        if type(self.documents) is not tuple or any(
            type(item) is not ValidatedDocument for item in self.documents
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/rule_evaluation/documents")
        if (
            type(self.premises) is not tuple
            or type(self.conclusions) is not tuple
            or any(
                type(item) is not ResolvedSubject for item in (*self.premises, *self.conclusions)
            )
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/rule_evaluation/subjects")


@runtime_checkable
class TrustedRuleEvaluator(Protocol):
    """Reviewed code that validates one exact semantic rule application."""

    @property
    def descriptor(self) -> TrustedRuleEvaluatorDescriptor: ...

    def validate(self, evaluation: TrustedRuleEvaluation) -> None:
        """Raise a bounded error unless the represented semantic fact is true."""


@dataclass(frozen=True, slots=True, kw_only=True)
class TrustedRulePolicy:
    """Immutable ``TrustedProofPolicy`` composed only from injected evaluators."""

    evaluators: tuple[TrustedRuleEvaluator, ...]
    schema_version: int = TRUSTED_RULE_POLICY_SCHEMA_VERSION
    catalog_id: str = field(init=False)
    catalog_sha256: str = field(init=False)
    _by_rule_identity: Mapping[
        tuple[str, str, str, str],
        tuple[TrustedRuleEvaluatorDescriptor, TrustedRuleEvaluator],
    ] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(IntentBridgeErrorCode.UNSUPPORTED_VERSION, "/schema_version")
        if type(self.evaluators) is not tuple or any(
            not isinstance(item, TrustedRuleEvaluator) for item in self.evaluators
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/evaluators")
        if not self.evaluators:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/evaluators")
        if len(self.evaluators) > MAX_TRUSTED_RULE_EVALUATORS:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/evaluators")

        descriptors: list[TrustedRuleEvaluatorDescriptor] = []
        by_rule: dict[
            tuple[str, str, str, str],
            tuple[TrustedRuleEvaluatorDescriptor, TrustedRuleEvaluator],
        ] = {}
        term_definitions: dict[tuple[str, str, str], str] = {}
        for evaluator in self.evaluators:
            try:
                descriptor = evaluator.descriptor
            except Exception:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/evaluators/descriptor")
            if type(descriptor) is not TrustedRuleEvaluatorDescriptor:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/evaluators/descriptor")
            rule_identity = descriptor.rule_term.semantic_identity
            if rule_identity in by_rule:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/evaluators")
            # Pin the descriptor used to construct the catalog.  Validation
            # never reads the property again, so a mutable implementation
            # cannot change its advertised signature after policy creation.
            by_rule[rule_identity] = (descriptor, evaluator)
            descriptors.append(descriptor)
            terms = (
                descriptor.rule_term,
                descriptor.predicate_term,
                *(
                    term
                    for endpoint in (*descriptor.premises, *descriptor.conclusions)
                    for term in (
                        endpoint.selector_kind_term,
                        endpoint.role_term,
                        endpoint.subject_type_term,
                    )
                ),
            )
            for term in terms:
                semantic_name = term.semantic_identity[:3]
                prior = term_definitions.setdefault(
                    semantic_name,
                    term.term_definition_sha256,
                )
                if prior != term.term_definition_sha256:
                    _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/evaluators/terms")

        ordered = tuple(sorted(descriptors, key=lambda item: item.rule_term.semantic_identity))
        body = {
            "schema_version": self.schema_version,
            "evaluators": [item.semantic_mapping() for item in ordered],
        }
        try:
            canonical = json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/evaluators")
        if len(canonical) > MAX_BRIDGE_ENVELOPE_BYTES:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/evaluators")
        digest = hashlib.sha256(_CATALOG_DOMAIN + canonical).hexdigest()
        object.__setattr__(self, "catalog_sha256", digest)
        object.__setattr__(self, "catalog_id", f"trusted_rule_policy_{digest[:32]}")
        object.__setattr__(self, "_by_rule_identity", MappingProxyType(by_rule))

    def validate(
        self,
        bundle: ProofBundle,
        documents: tuple[ValidatedDocument, ...],
        resolved_subjects: tuple[ResolvedSubject, ...],
    ) -> None:
        """Match exact signatures, then invoke every semantic evaluator."""

        if type(bundle) is not ProofBundle:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/proof_bundle")
        if type(documents) is not tuple or any(
            type(item) is not ValidatedDocument for item in documents
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/documents")
        if type(resolved_subjects) is not tuple or any(
            type(item) is not ResolvedSubject for item in resolved_subjects
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/resolved_subjects")
        if len(bundle.assertions) > MAX_RULE_APPLICATIONS:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/assertions")

        term_by_id = {item.term_ref_id: item for item in bundle.terms}
        resolved_by_subject = {item.subject: item for item in resolved_subjects}
        if len(resolved_by_subject) != len(resolved_subjects):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/resolved_subjects")
        referenced_subjects = {
            endpoint.subject
            for assertion in bundle.assertions
            for endpoint in (*assertion.premises, *assertion.conclusions)
        }
        if set(resolved_by_subject) != referenced_subjects:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/resolved_subjects")
        actual_documents = tuple(item.document for item in documents)
        document_ids = {item.artifact_id for item in actual_documents}
        if actual_documents != bundle.documents or any(
            item.subject.artifact_id not in document_ids for item in resolved_subjects
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/documents")

        for assertion in bundle.assertions:
            rule_term = term_by_id[assertion.rule_term_ref_id]
            binding = self._by_rule_identity.get(rule_term.semantic_identity)
            if binding is None:
                _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/assertions/rule")
            descriptor, evaluator = binding
            predicate_term = term_by_id[assertion.predicate_term_ref_id]
            if predicate_term.semantic_identity != descriptor.predicate_term.semantic_identity:
                _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/assertions/predicate")
            premises = self._resolve_endpoints(
                assertion.premises,
                descriptor.premises,
                term_by_id,
                resolved_by_subject,
                path="/assertions/premises",
            )
            conclusions = self._resolve_endpoints(
                assertion.conclusions,
                descriptor.conclusions,
                term_by_id,
                resolved_by_subject,
                path="/assertions/conclusions",
            )
            try:
                evaluator.validate(
                    TrustedRuleEvaluation(
                        bundle=bundle,
                        assertion=assertion,
                        documents=documents,
                        premises=premises,
                        conclusions=conclusions,
                    )
                )
            except IntentBridgeError:
                raise
            except SystemExit:
                # A reviewed evaluator must not terminate its host process.
                # Interactive cancellation signals such as KeyboardInterrupt
                # deliberately remain host-owned and are not swallowed here.
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/evaluators/validate")
            except Exception:
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/evaluators/validate")

    @staticmethod
    def _resolve_endpoints(
        actual: tuple[ProofEndpoint, ...],
        expected: tuple[RuleEndpointSignature, ...],
        term_by_id: dict[str, BridgeTermRef],
        resolved_by_subject: dict[SubjectRef, ResolvedSubject],
        *,
        path: str,
    ) -> tuple[ResolvedSubject, ...]:
        if len(actual) != len(expected):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)
        result: list[ResolvedSubject] = []
        for endpoint, signature in zip(actual, expected, strict=True):
            selector_term = term_by_id[endpoint.subject.selector_kind_term_ref_id]
            role_term = term_by_id[endpoint.role_term_ref_id]
            resolved = resolved_by_subject[endpoint.subject]
            if (
                selector_term.semantic_identity != signature.selector_kind_term.semantic_identity
                or role_term.semantic_identity != signature.role_term.semantic_identity
                or resolved.semantic_type.semantic_identity
                != signature.subject_type_term.semantic_identity
            ):
                _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)
            result.append(resolved)
        return tuple(result)


__all__ = [
    "MAX_TRUSTED_RULE_EVALUATORS",
    "RuleEndpointSignature",
    "TRUSTED_RULE_POLICY_SCHEMA_VERSION",
    "TrustedRuleEvaluation",
    "TrustedRuleEvaluator",
    "TrustedRuleEvaluatorDescriptor",
    "TrustedRulePolicy",
]
