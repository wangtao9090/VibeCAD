"""Strict in-memory aggregation and monotonic promotion of catalog segments."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from types import MappingProxyType

from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityCatalogSegment,
    CapabilityDescriptor,
    CapabilityFact,
    CapabilityKind,
    CapabilityRiskClass,
    CapabilitySupportStatus,
)

MAX_CAPABILITY_INDEX_SEGMENTS = 64
MAX_CAPABILITY_INDEX_DESCRIPTORS = 8_192
_INDEX_DIGEST_DOMAIN = b"vibecad-capability-index-v1\0"


def _fail(code: CapabilityCatalogErrorCode, path: str) -> None:
    raise CapabilityCatalogError(code, path)


def _fact_map(value: CapabilityDescriptor) -> dict[str, CapabilityFact]:
    return {item.key_term_ref_id: item for item in value.facts}


def _verify_promotion(base: CapabilityDescriptor, promoted: CapabilityDescriptor) -> None:
    path = f"descriptors/{base.capability_id}"
    if base.capability_id != promoted.capability_id or base.status.rank >= promoted.status.rank:
        _fail(CapabilityCatalogErrorCode.INVALID_STATUS, path)
    identity = (
        "kind",
        "native_identifier",
        "declaring_module_id",
    )
    if any(getattr(base, field) != getattr(promoted, field) for field in identity):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)
    if (
        base.risk_class is not CapabilityRiskClass.UNKNOWN
        and base.risk_class is not promoted.risk_class
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)
    if not set(base.semantic_term_ref_ids) <= set(promoted.semantic_term_ref_ids):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)
    if not set(base.dependency_ids) <= set(promoted.dependency_ids):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)
    base_facts = _fact_map(base)
    promoted_facts = _fact_map(promoted)
    if not set(base_facts) <= set(promoted_facts) or any(
        base_facts[key] != promoted_facts[key] for key in base_facts
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)
    if base.status.rank >= CapabilitySupportStatus.EXECUTABLE.rank and (
        base.execution_profiles != promoted.execution_profiles
        or base.lifecycle_stages != promoted.lifecycle_stages
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)


def _active_descriptor(values: tuple[CapabilityDescriptor, ...]) -> CapabilityDescriptor:
    ordered = tuple(sorted(values, key=lambda item: item.status.rank))
    by_rank: dict[int, str] = {}
    unique: list[CapabilityDescriptor] = []
    for item in ordered:
        digest = item.descriptor_sha256
        prior = by_rank.get(item.status.rank)
        if prior is not None and not hmac.compare_digest(prior, digest):
            _fail(
                CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                f"descriptors/{item.capability_id}",
            )
        by_rank[item.status.rank] = digest
        if not unique or not hmac.compare_digest(unique[-1].descriptor_sha256, digest):
            unique.append(item)
    for base, promoted in zip(unique, unique[1:], strict=False):
        _verify_promotion(base, promoted)
    return unique[-1]


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityCoverageSummary:
    total: int
    discovered: int
    representable: int
    executable: int
    verified: int

    @property
    def execution_gap(self) -> int:
        return self.discovered + self.representable

    @property
    def verification_gap(self) -> int:
        return self.executable


class CapabilityCatalogIndex:
    """Closed view of compatible segments for one exact backend build."""

    __slots__ = (
        "_backend",
        "_by_native_identifier",
        "_catalog_sha256",
        "_descriptors",
        "_segments",
        "_terms",
    )

    def __init__(self, segments: tuple[CapabilityCatalogSegment, ...]) -> None:
        if type(segments) is not tuple or not segments:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "segments")
        if len(segments) > MAX_CAPABILITY_INDEX_SEGMENTS:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "segments")
        if not all(type(item) is CapabilityCatalogSegment for item in segments):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "segments")
        backend = segments[0].backend
        if any(item.backend != backend for item in segments):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "segments/backend")
        segment_digests = tuple(item.catalog_sha256 for item in segments)
        if len(set(segment_digests)) != len(segment_digests):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "segments")
        term_by_id = {}
        for segment in segments:
            for term in segment.terms:
                prior = term_by_id.get(term.term_ref_id)
                if prior is not None and prior != term:
                    _fail(
                        CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                        f"terms/{term.term_ref_id}",
                    )
                term_by_id[term.term_ref_id] = term
        versions_by_id: dict[str, list[CapabilityDescriptor]] = {}
        descriptors_by_digest: dict[str, CapabilityDescriptor] = {}
        total = 0
        for segment in segments:
            total += len(segment.descriptors)
            if total > MAX_CAPABILITY_INDEX_DESCRIPTORS:
                _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "segments/descriptors")
            for descriptor in segment.descriptors:
                versions_by_id.setdefault(descriptor.capability_id, []).append(descriptor)
                descriptors_by_digest[descriptor.descriptor_sha256] = descriptor
        for segment in segments:
            for external in segment.external_refs:
                target = descriptors_by_digest.get(external.descriptor_sha256)
                if target is None or target.capability_id != external.capability_id:
                    _fail(
                        CapabilityCatalogErrorCode.UNKNOWN_REFERENCE,
                        f"segments/{segment.segment_id}/external_refs",
                    )
        active = {
            capability_id: _active_descriptor(tuple(values))
            for capability_id, values in versions_by_id.items()
        }
        by_native: dict[str, list[CapabilityDescriptor]] = {}
        for descriptor in active.values():
            by_native.setdefault(descriptor.native_identifier, []).append(descriptor)
        digest_body = b"".join(bytes.fromhex(item) for item in sorted(segment_digests))
        object.__setattr__(self, "_backend", backend)
        object.__setattr__(
            self,
            "_segments",
            tuple(sorted(segments, key=lambda item: item.catalog_sha256)),
        )
        object.__setattr__(self, "_descriptors", MappingProxyType(active))
        object.__setattr__(self, "_terms", MappingProxyType(term_by_id))
        object.__setattr__(
            self,
            "_by_native_identifier",
            MappingProxyType(
                {
                    key: tuple(sorted(values, key=lambda item: item.capability_id))
                    for key, values in by_native.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "_catalog_sha256",
            hashlib.sha256(_INDEX_DIGEST_DOMAIN + digest_body).hexdigest(),
        )

    @property
    def backend(self) -> CapabilityBackend:
        return self._backend

    @property
    def catalog_sha256(self) -> str:
        return self._catalog_sha256

    @property
    def segments(self) -> tuple[CapabilityCatalogSegment, ...]:
        return self._segments

    @property
    def descriptors(self):
        return self._descriptors

    def lookup(self, capability_id: str) -> CapabilityDescriptor:
        if type(capability_id) is not str:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "capability_id")
        result = self._descriptors.get(capability_id)
        if result is None:
            _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "capability_id")
        return result

    def lookup_native(self, native_identifier: str) -> tuple[CapabilityDescriptor, ...]:
        if type(native_identifier) is not str:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "native_identifier")
        return self._by_native_identifier.get(native_identifier, ())

    def coverage(self, *, kind: CapabilityKind | None = None) -> CapabilityCoverageSummary:
        if kind is not None and type(kind) is not CapabilityKind:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "kind")
        values = tuple(
            item for item in self._descriptors.values() if kind is None or item.kind is kind
        )
        counts = {
            status: sum(item.status is status for item in values)
            for status in CapabilitySupportStatus
        }
        return CapabilityCoverageSummary(
            total=len(values),
            discovered=counts[CapabilitySupportStatus.DISCOVERED],
            representable=counts[CapabilitySupportStatus.REPRESENTABLE],
            executable=counts[CapabilitySupportStatus.EXECUTABLE],
            verified=counts[CapabilitySupportStatus.VERIFIED],
        )


__all__ = ()
