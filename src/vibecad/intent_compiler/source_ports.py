"""Trusted source-selection boundary for the generic compiler."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from vibecad.intent_bridge.contracts import (
    MAX_BRIDGE_DOCUMENTS,
    MAX_BRIDGE_ENVELOPE_BYTES,
    BridgeTermRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    IntentCompileRequest,
    ProducerDescriptor,
)
from vibecad.intent_bridge.ports import ValidatedDocument
from vibecad.intent_compiler.contracts import (
    IntentSelection,
    canonical_bytes,
    semantic_term_mapping,
)

MAX_TRUSTED_SOURCE_ADAPTERS = 32


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceAdapterDescriptor:
    adapter_id: str
    adapter_version: str
    adapter_contract_sha256: str
    input_schema_terms: tuple[BridgeTermRef, ...]

    def __post_init__(self) -> None:
        # Local descriptors are included in the compiler content digest.  Use
        # the bridge value validators through their canonical constructor.
        ProducerDescriptor(
            producer_id=self.adapter_id,
            producer_version=self.adapter_version,
            producer_contract_sha256=self.adapter_contract_sha256,
            rule_catalog_sha256=self.adapter_contract_sha256,
        )
        if type(self.input_schema_terms) is not tuple or any(
            type(item) is not BridgeTermRef for item in self.input_schema_terms
        ):
            raise IntentBridgeError(IntentBridgeErrorCode.INVALID_INPUT, "/input_schema_terms")
        if not self.input_schema_terms:
            raise IntentBridgeError(
                IntentBridgeErrorCode.INVALID_INPUT,
                "/input_schema_terms",
            )
        if len(self.input_schema_terms) > MAX_BRIDGE_DOCUMENTS:
            raise IntentBridgeError(
                IntentBridgeErrorCode.BUDGET_EXCEEDED,
                "/input_schema_terms",
            )
        identities = tuple(item.semantic_identity for item in self.input_schema_terms)
        if len(set(identities)) != len(identities):
            raise IntentBridgeError(IntentBridgeErrorCode.INVALID_INPUT, "/input_schema_terms")
        definitions: dict[tuple[str, str, str], str] = {}
        for item in self.input_schema_terms:
            name = item.semantic_identity[:3]
            prior = definitions.setdefault(name, item.term_definition_sha256)
            if prior != item.term_definition_sha256:
                raise IntentBridgeError(
                    IntentBridgeErrorCode.INTEGRITY_FAILURE,
                    "/input_schema_terms",
                )

    def semantic_mapping(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "adapter_contract_sha256": self.adapter_contract_sha256,
            "input_schema_terms": [
                semantic_term_mapping(item)
                for item in sorted(
                    self.input_schema_terms,
                    key=lambda value: value.semantic_identity,
                )
            ],
        }


@runtime_checkable
class TrustedIntentSourceAdapter(Protocol):
    """Reviewed adapter that extracts one explicit decision from known inputs."""

    @property
    def descriptor(self) -> SourceAdapterDescriptor: ...

    def select(
        self,
        request: IntentCompileRequest,
        documents: tuple[ValidatedDocument, ...],
    ) -> IntentSelection | None:
        """Return one exact selection, or ``None`` when the input is inert."""


def source_adapter_catalog_sha256(descriptors: tuple[SourceAdapterDescriptor, ...]) -> str:
    """Content hash of host-injected source adapter descriptors."""

    if (
        type(descriptors) is not tuple
        or not descriptors
        or len(descriptors) > MAX_TRUSTED_SOURCE_ADAPTERS
        or any(type(item) is not SourceAdapterDescriptor for item in descriptors)
    ):
        raise IntentBridgeError(
            IntentBridgeErrorCode.BUDGET_EXCEEDED
            if type(descriptors) is tuple and len(descriptors) > MAX_TRUSTED_SOURCE_ADAPTERS
            else IntentBridgeErrorCode.INVALID_INPUT,
            "/source_adapters",
        )
    descriptors = tuple(
        sorted(
            descriptors,
            key=lambda item: (item.adapter_id, item.adapter_version),
        )
    )
    names = tuple((item.adapter_id, item.adapter_version) for item in descriptors)
    if len(set(names)) != len(names):
        raise IntentBridgeError(IntentBridgeErrorCode.INVALID_INPUT, "/source_adapters")
    definitions: dict[tuple[str, str, str], str] = {}
    for descriptor in descriptors:
        for term in descriptor.input_schema_terms:
            name = term.semantic_identity[:3]
            prior = definitions.setdefault(name, term.term_definition_sha256)
            if prior != term.term_definition_sha256:
                raise IntentBridgeError(
                    IntentBridgeErrorCode.INTEGRITY_FAILURE,
                    "/source_adapters/terms",
                )
    canonical = canonical_bytes([item.semantic_mapping() for item in descriptors])
    if len(canonical) > MAX_BRIDGE_ENVELOPE_BYTES:
        raise IntentBridgeError(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/source_adapters")
    return hashlib.sha256(
        b"vibecad.intent-compiler.source-adapter-catalog.v1\0" + canonical
    ).hexdigest()


__all__ = [
    "MAX_TRUSTED_SOURCE_ADAPTERS",
    "SourceAdapterDescriptor",
    "TrustedIntentSourceAdapter",
    "source_adapter_catalog_sha256",
]
