"""Immutable artifact staging, overlay reads, and atomic publication ports."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from vibecad.intent_bridge.contracts import (
    MAX_COMPILE_OUTPUTS,
    MAX_TOTAL_PAYLOAD_BYTES,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    ProducerDescriptor,
)
from vibecad.intent_bridge.ports import ArtifactReader
from vibecad.intent_compiler.contracts import CompiledIntentDocument


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactPublisherDescriptor:
    publisher_id: str
    publisher_version: str
    publisher_contract_sha256: str

    def __post_init__(self) -> None:
        ProducerDescriptor(
            producer_id=self.publisher_id,
            producer_version=self.publisher_version,
            producer_contract_sha256=self.publisher_contract_sha256,
            rule_catalog_sha256=self.publisher_contract_sha256,
        )

    def semantic_mapping(self) -> dict[str, str]:
        return {
            "publisher_id": self.publisher_id,
            "publisher_version": self.publisher_version,
            "publisher_contract_sha256": self.publisher_contract_sha256,
        }


@runtime_checkable
class IntentArtifactPublisher(Protocol):
    """All-or-none immutable content store owned by the local host."""

    @property
    def descriptor(self) -> ArtifactPublisherDescriptor: ...

    def publish_atomic(
        self,
        request_digest: str,
        documents: tuple[CompiledIntentDocument, ...],
        maximum_total_bytes: int,
    ) -> tuple[DocumentRef, ...]:
        """Idempotently expose every document, or expose none."""

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        """Read back the exact immutable bytes of a published document."""


class OverlayArtifactReader:
    """Read staged outputs first and delegate other immutable artifacts."""

    __slots__ = ("_base", "_staged")

    def __init__(
        self,
        base: ArtifactReader,
        documents: tuple[CompiledIntentDocument, ...],
    ) -> None:
        if not isinstance(base, ArtifactReader):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/artifacts")
        if type(documents) is not tuple or any(
            type(item) is not CompiledIntentDocument for item in documents
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/documents")
        staged: dict[str, CompiledIntentDocument] = {}
        for item in documents:
            if item.document.artifact_id in staged:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/documents")
            staged[item.document.artifact_id] = item
        self._base = base
        self._staged = MappingProxyType(staged)

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        staged = self._staged.get(document.artifact_id)
        if staged is None:
            return self._base.read(document, maximum_bytes)
        if staged.document != document:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/overlay/document")
        if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= MAX_TOTAL_PAYLOAD_BYTES:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/maximum_bytes")
        if len(staged.payload) > maximum_bytes:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/overlay/payload")
        return staged.payload


class InMemoryIntentArtifactPublisher:
    """Reference publisher with content addressing and atomic copy-on-write."""

    __slots__ = ("_blobs", "_descriptor", "_documents", "_requests")

    def __init__(self, descriptor: ArtifactPublisherDescriptor | None = None) -> None:
        if descriptor is None:
            descriptor = ArtifactPublisherDescriptor(
                publisher_id="publisher.memory.intent_artifacts",
                publisher_version="1.0.0",
                publisher_contract_sha256=hashlib.sha256(
                    b"vibecad.intent-compiler.memory-publisher.v1\0"
                    b"content-addressed;idempotent;all-or-none;exact-readback"
                ).hexdigest(),
            )
        if type(descriptor) is not ArtifactPublisherDescriptor:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/descriptor")
        self._descriptor = descriptor
        self._blobs: dict[str, bytes] = {}
        self._documents: dict[str, DocumentRef] = {}
        self._requests: dict[str, tuple[DocumentRef, ...]] = {}

    @property
    def descriptor(self) -> ArtifactPublisherDescriptor:
        return self._descriptor

    @property
    def published_documents(self) -> tuple[DocumentRef, ...]:
        return tuple(sorted(self._documents.values(), key=lambda item: item.artifact_id))

    def publish_atomic(
        self,
        request_digest: str,
        documents: tuple[CompiledIntentDocument, ...],
        maximum_total_bytes: int,
    ) -> tuple[DocumentRef, ...]:
        if type(request_digest) is not str or _SHA256.fullmatch(request_digest) is None:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/request_digest")
        if (
            type(documents) is not tuple
            or not documents
            or len(documents) > MAX_COMPILE_OUTPUTS
            or any(type(item) is not CompiledIntentDocument for item in documents)
        ):
            _fail(
                IntentBridgeErrorCode.BUDGET_EXCEEDED
                if type(documents) is tuple and len(documents) > MAX_COMPILE_OUTPUTS
                else IntentBridgeErrorCode.INVALID_INPUT,
                "/documents",
            )
        if (
            type(maximum_total_bytes) is not int
            or not 1 <= maximum_total_bytes <= MAX_TOTAL_PAYLOAD_BYTES
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/maximum_total_bytes")
        if sum(len(item.payload) for item in documents) > maximum_total_bytes:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/documents")
        ordered = tuple(sorted(documents, key=lambda item: item.document.artifact_id))
        refs = tuple(item.document for item in ordered)
        if len({item.artifact_id for item in refs}) != len(refs):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/documents")
        prior_request = self._requests.get(request_digest)
        if prior_request is not None:
            if prior_request != refs:
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/request_digest")
            return prior_request

        candidate_blobs = dict(self._blobs)
        candidate_documents = dict(self._documents)
        for item in ordered:
            content_key = item.document.content_sha256
            prior_blob = candidate_blobs.get(content_key)
            if prior_blob is not None:
                if prior_blob != item.payload:
                    _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/documents/content")
            else:
                candidate_blobs[content_key] = item.payload
            prior_document = candidate_documents.get(item.document.artifact_id)
            if prior_document is not None and prior_document != item.document:
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/documents/artifact_id")
            candidate_documents[item.document.artifact_id] = item.document
        # Swap both maps only after every invariant above has succeeded.
        self._blobs = candidate_blobs
        self._documents = candidate_documents
        self._requests = {**self._requests, request_digest: refs}
        return refs

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        if type(document) is not DocumentRef:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/document")
        stored_document = self._documents.get(document.artifact_id)
        payload = self._blobs.get(document.content_sha256)
        if stored_document != document or payload is None:
            _fail(IntentBridgeErrorCode.UNKNOWN_REFERENCE, "/document")
        if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= MAX_TOTAL_PAYLOAD_BYTES:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/maximum_bytes")
        if len(payload) > maximum_bytes:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/document")
        if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), document.content_sha256):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document")
        return payload


__all__ = [
    "ArtifactPublisherDescriptor",
    "InMemoryIntentArtifactPublisher",
    "IntentArtifactPublisher",
    "OverlayArtifactReader",
]
