"""Full-identity trusted rule catalog and exact request matching."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from vibecad.intent_bridge.contracts import (
    MAX_BRIDGE_ENVELOPE_BYTES,
    BridgeTermRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
)
from vibecad.intent_compiler.contracts import (
    IntentRuleSetDescriptor,
    RuleSetCompileContext,
    RuleSetEmission,
    canonical_bytes,
)

MAX_TRUSTED_RULE_SETS = 64
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


@runtime_checkable
class TrustedIntentRuleSet(Protocol):
    """Reviewed deterministic emitter for one exact rule-set identity."""

    @property
    def descriptor(self) -> IntentRuleSetDescriptor: ...

    def emit(self, context: RuleSetCompileContext) -> RuleSetEmission:
        """Return a complete candidate; the core independently revalidates it."""


@dataclass(frozen=True, slots=True, kw_only=True)
class TrustedIntentRuleBinding:
    """Descriptor pinned when the host constructs the trusted catalog."""

    descriptor: IntentRuleSetDescriptor
    rule_set: TrustedIntentRuleSet


class TrustedIntentRuleCatalog:
    """Immutable host-created rule table keyed by full semantic identity."""

    __slots__ = (
        "_by_identity",
        "_catalog_sha256",
        "_descriptors",
        "_proof_policy_catalog_sha256",
    )

    def __init__(
        self,
        rule_sets: tuple[TrustedIntentRuleSet, ...],
        *,
        proof_policy_catalog_sha256: str,
    ) -> None:
        if (
            type(proof_policy_catalog_sha256) is not str
            or _SHA256.fullmatch(proof_policy_catalog_sha256) is None
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/proof_policy_catalog_sha256")
        if (
            type(rule_sets) is not tuple
            or not rule_sets
            or len(rule_sets) > MAX_TRUSTED_RULE_SETS
            or any(not isinstance(item, TrustedIntentRuleSet) for item in rule_sets)
        ):
            _fail(
                IntentBridgeErrorCode.BUDGET_EXCEEDED
                if type(rule_sets) is tuple and len(rule_sets) > MAX_TRUSTED_RULE_SETS
                else IntentBridgeErrorCode.INVALID_INPUT,
                "/rule_sets",
            )
        by_identity: dict[tuple[str, str, str, str], TrustedIntentRuleBinding] = {}
        descriptors: list[IntentRuleSetDescriptor] = []
        names: set[tuple[str, str]] = set()
        term_definitions: dict[tuple[str, str, str], str] = {}
        for rule_set in rule_sets:
            try:
                descriptor = rule_set.descriptor
            except KeyboardInterrupt:
                raise
            except SystemExit:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/rule_sets/descriptor")
            except Exception:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/rule_sets/descriptor")
            if type(descriptor) is not IntentRuleSetDescriptor:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/rule_sets/descriptor")
            identity = descriptor.rule_set_term.semantic_identity
            name = (descriptor.rule_set_id, descriptor.rule_set_version)
            if identity in by_identity or name in names:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/rule_sets")
            terms = (
                descriptor.rule_set_term,
                *(
                    term
                    for signature in (
                        *descriptor.input_signatures,
                        *descriptor.output_signatures,
                    )
                    for term in (signature.role_term, signature.schema_term)
                ),
                *(
                    term
                    for rule in descriptor.rules
                    for term in (rule.rule_term, rule.predicate_term)
                ),
            )
            for term in terms:
                semantic_name = term.semantic_identity[:3]
                prior = term_definitions.setdefault(
                    semantic_name,
                    term.term_definition_sha256,
                )
                if prior != term.term_definition_sha256:
                    _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/rule_sets/terms")
            by_identity[identity] = TrustedIntentRuleBinding(
                descriptor=descriptor,
                rule_set=rule_set,
            )
            names.add(name)
            descriptors.append(descriptor)
        ordered = tuple(sorted(descriptors, key=lambda item: item.rule_set_term.semantic_identity))
        canonical = canonical_bytes(
            {
                "proof_policy_catalog_sha256": proof_policy_catalog_sha256,
                "rule_sets": [item.semantic_mapping() for item in ordered],
            }
        )
        if len(canonical) > MAX_BRIDGE_ENVELOPE_BYTES:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/rule_sets")
        digest = hashlib.sha256(
            b"vibecad.intent-compiler.rule-catalog.v1\0" + canonical
        ).hexdigest()
        self._by_identity = MappingProxyType(by_identity)
        self._descriptors = ordered
        self._catalog_sha256 = digest
        self._proof_policy_catalog_sha256 = proof_policy_catalog_sha256

    @property
    def catalog_sha256(self) -> str:
        return self._catalog_sha256

    @property
    def proof_policy_catalog_sha256(self) -> str:
        return self._proof_policy_catalog_sha256

    @property
    def descriptors(self) -> tuple[IntentRuleSetDescriptor, ...]:
        return self._descriptors

    def resolve(self, rule_set_term: BridgeTermRef) -> TrustedIntentRuleBinding | None:
        if type(rule_set_term) is not BridgeTermRef:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/rule_set_term")
        return self._by_identity.get(rule_set_term.semantic_identity)


__all__ = [
    "TrustedIntentRuleBinding",
    "TrustedIntentRuleCatalog",
    "TrustedIntentRuleSet",
]
