"""Focused tests for the full-identity trusted intent rule catalog."""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from vibecad.intent_bridge.contracts import (
    BridgeTermRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
)
from vibecad.intent_compiler.catalog import (
    MAX_TRUSTED_RULE_SETS,
    TrustedIntentRuleCatalog,
)
from vibecad.intent_compiler.contracts import (
    DocumentSignature,
    IntentRuleDescriptor,
    IntentRuleSetDescriptor,
)

POLICY_DIGEST = "e" * 64


def _term(local: str, semantic: str, definition: str = "a") -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=local,
        namespace="org.vibecad.intent-compiler-catalog-test",
        vocabulary_version="1.0.0",
        term_id=semantic,
        term_definition_sha256=definition * 64,
    )


def _descriptor(index: int) -> IntentRuleSetDescriptor:
    return IntentRuleSetDescriptor(
        rule_set_id=f"rule_set_{index:03d}",
        rule_set_version="1.0.0",
        rule_set_contract_sha256=hashlib.sha256(f"set:{index}".encode()).hexdigest(),
        rule_set_term=_term(f"set_{index:03d}", f"rule-set.{index:03d}"),
        input_signatures=(
            DocumentSignature(
                role_term=_term(f"source_role_{index:03d}", "role.source"),
                schema_term=_term(f"source_schema_{index:03d}", "schema.source"),
            ),
        ),
        output_signatures=(
            DocumentSignature(
                role_term=_term(f"output_role_{index:03d}", "role.output"),
                schema_term=_term(f"output_schema_{index:03d}", "schema.output"),
            ),
        ),
        rules=(
            IntentRuleDescriptor(
                rule_term=_term(f"rule_{index:03d}", f"rule.{index:03d}", "b"),
                predicate_term=_term(
                    f"predicate_{index:03d}",
                    f"predicate.{index:03d}",
                    "c",
                ),
                emitter_contract_sha256=hashlib.sha256(f"emitter:{index}".encode()).hexdigest(),
                maximum_applications=1,
            ),
        ),
    )


class _RuleSet:
    def __init__(self, descriptor: IntentRuleSetDescriptor) -> None:
        self.current_descriptor = descriptor

    @property
    def descriptor(self) -> IntentRuleSetDescriptor:
        return self.current_descriptor

    def emit(self, context):  # pragma: no cover - catalog never executes emitters
        raise AssertionError(context)


def _catalog(*rule_sets: _RuleSet, policy: str = POLICY_DIGEST) -> TrustedIntentRuleCatalog:
    return TrustedIntentRuleCatalog(
        tuple(rule_sets),
        proof_policy_catalog_sha256=policy,
    )


def _alias(term: BridgeTermRef, local: str) -> BridgeTermRef:
    return dataclasses.replace(term, term_ref_id=local)


def test_catalog_is_order_independent_and_resolves_only_full_semantic_identity() -> None:
    first = _RuleSet(_descriptor(0))
    second = _RuleSet(_descriptor(1))
    forward = _catalog(first, second)
    reverse = _catalog(second, first)

    assert forward.catalog_sha256 == reverse.catalog_sha256
    assert (
        forward.resolve(_alias(first.descriptor.rule_set_term, "request_rule_set")).rule_set
        is first
    )
    rebound = dataclasses.replace(
        first.descriptor.rule_set_term,
        term_ref_id="request_rule_set",
        term_definition_sha256="f" * 64,
    )
    assert forward.resolve(rebound) is None


def test_catalog_pins_descriptor_and_digest_when_emitter_descriptor_drifts() -> None:
    emitter = _RuleSet(_descriptor(0))
    catalog = _catalog(emitter)
    pinned = catalog.resolve(_alias(emitter.descriptor.rule_set_term, "request_rule_set"))
    original_digest = catalog.catalog_sha256
    original_descriptor = pinned.descriptor

    emitter.current_descriptor = _descriptor(1)

    assert catalog.catalog_sha256 == original_digest
    assert pinned.descriptor == original_descriptor
    assert catalog.resolve(_alias(original_descriptor.rule_set_term, "still_original")) == pinned
    assert catalog.resolve(_alias(emitter.descriptor.rule_set_term, "now_changed")) is None
    with pytest.raises(AttributeError):
        catalog.catalog_sha256 = "f" * 64


def test_catalog_digest_binds_emitter_contract_and_rejects_duplicate_identity() -> None:
    descriptor = _descriptor(0)
    changed_rule = dataclasses.replace(
        descriptor.rules[0],
        emitter_contract_sha256="e" * 64,
    )
    changed = dataclasses.replace(descriptor, rules=(changed_rule,))

    assert (
        _catalog(_RuleSet(descriptor)).catalog_sha256 != _catalog(_RuleSet(changed)).catalog_sha256
    )
    assert (
        _catalog(_RuleSet(descriptor)).catalog_sha256
        != _catalog(
            _RuleSet(descriptor),
            policy="f" * 64,
        ).catalog_sha256
    )
    with pytest.raises(IntentBridgeError) as error:
        _catalog(
            _RuleSet(descriptor),
            _RuleSet(
                dataclasses.replace(
                    descriptor,
                    rule_set_id="different_local_name",
                )
            ),
        )
    assert error.value.code is IntentBridgeErrorCode.INVALID_INPUT

    rebound_schema = dataclasses.replace(
        _descriptor(1),
        input_signatures=(
            DocumentSignature(
                role_term=_descriptor(1).input_signatures[0].role_term,
                schema_term=dataclasses.replace(
                    _descriptor(1).input_signatures[0].schema_term,
                    term_definition_sha256="f" * 64,
                ),
            ),
        ),
    )
    with pytest.raises(IntentBridgeError) as rebound_error:
        _catalog(_RuleSet(descriptor), _RuleSet(rebound_schema))
    assert rebound_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE


def test_catalog_rule_set_budget_accepts_n_rejects_n_plus_one() -> None:
    rule_sets = tuple(_RuleSet(_descriptor(index)) for index in range(MAX_TRUSTED_RULE_SETS))
    catalog = _catalog(*reversed(rule_sets))

    assert len(catalog.descriptors) == MAX_TRUSTED_RULE_SETS
    with pytest.raises(IntentBridgeError) as error:
        _catalog(*rule_sets, _RuleSet(_descriptor(MAX_TRUSTED_RULE_SETS)))
    assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
