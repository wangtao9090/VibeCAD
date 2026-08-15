"""Close managed Reviewed receipts over the current FreeCAD capability set.

Family verifiers intentionally emit independent, authority-free receipts.  A
runtime projection must not cherry-pick those receipts or infer coverage from
an adapter name.  This module performs the single exact join from reviewed
family manifests and formal semantic specs to managed receipts, then derives
the native-TypeId verification bindings consumed by capability projection v2.

The result remains inert metadata.  It neither executes a backend nor grants
an intent permission; execution continues to require the reviewed adapter and
native transaction rule selected by the formal capability.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Final

from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
)
from vibecad.execution.freecad_capability_projection_v2 import (
    FreeCadPromotionVerificationBinding,
)
from vibecad.execution.freecad_intent_capabilities import FreeCadIntentCapabilitySpec
from vibecad.execution.freecad_reviewed_verification import (
    ReviewedConformanceEvidenceKind,
    ReviewedOperationVerificationBinding,
    ReviewedVerificationReceipt,
    build_promotion_verification_binding,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    FamilyBatchManifest,
    ReviewedOperationSpec,
)

FREECAD_REVIEWED_VERIFICATION_SET_SCHEMA_VERSION: Final = 1
MAX_FREECAD_REVIEWED_VERIFICATION_RECEIPTS: Final = 32
MAX_FREECAD_REVIEWED_VERIFICATION_SET_BYTES: Final = 2 * 1024 * 1024

_FORMAL_SPEC_DIGEST_DOMAIN = b"vibecad-freecad-reviewed-formal-spec-v1\0"
_VERIFICATION_SET_DIGEST_DOMAIN = b"vibecad-freecad-reviewed-verification-set-v1\0"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _fail(code: CapabilityCatalogErrorCode, path: str) -> None:
    raise CapabilityCatalogError(code, path)


def _canonical(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "verification_set")
    if not raw or len(raw) > MAX_FREECAD_REVIEWED_VERIFICATION_SET_BYTES:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "verification_set")
    return raw


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _backend_mapping(value: CapabilityBackend) -> dict[str, object]:
    return {
        "backend_id": value.backend_id,
        "backend_version": list(value.backend_version),
        "build_fingerprint_sha256": value.build_fingerprint_sha256,
        "discovery_profile": value.discovery_profile.value,
        "platform_id": value.platform_id,
    }


def _formal_spec_mapping(value: FreeCadIntentCapabilitySpec) -> dict[str, object]:
    return {
        "adapter": {
            "contract_sha256": value.adapter_contract_sha256,
            "id": value.adapter_id,
            "version": value.adapter_version,
        },
        "lifecycle_stages": [item.value for item in value.lifecycle_stages],
        "native_type_id": value.native_type_id,
        "operation_id": value.operation_id,
        "profiles": [item.value for item in value.execution_profiles],
        "risk_class": value.risk_class.value,
        "rule": {
            "contract_sha256": value.rule_contract_sha256,
            "id": value.rule_id,
        },
        "semantic_operation": value.semantic_operation,
    }


def _formal_spec_sha256(value: FreeCadIntentCapabilitySpec) -> str:
    return hashlib.sha256(
        _FORMAL_SPEC_DIGEST_DOMAIN + _canonical(_formal_spec_mapping(value))
    ).hexdigest()


def _verification_mapping(value: FreeCadPromotionVerificationBinding) -> dict[str, object]:
    return {
        "adapter_contract_sha256": value.adapter_contract_sha256,
        "runtime_build_sha256": value.runtime_build_sha256,
        "test_contract_sha256": value.test_contract_sha256,
        "test_receipt_sha256": value.test_receipt_sha256,
        "test_receipt_size_bytes": value.test_receipt_size_bytes,
        "verifier_id": value.verifier_id,
        "verifier_version": value.verifier_version,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadVerifiedFormalOperation:
    operation_id: str
    formal_spec_sha256: str
    test_receipt_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "formal_operation/operation_id")
        _digest(self.formal_spec_sha256, "formal_operation/formal_spec_sha256")
        _digest(self.test_receipt_sha256, "formal_operation/test_receipt_sha256")

    def _mapping(self) -> dict[str, str]:
        return {
            "formal_spec_sha256": self.formal_spec_sha256,
            "operation_id": self.operation_id,
            "test_receipt_sha256": self.test_receipt_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadVerifiedNativeType:
    native_type_id: str
    formal_operation_ids: tuple[str, ...]
    verification: FreeCadPromotionVerificationBinding

    def __post_init__(self) -> None:
        _identifier(self.native_type_id, "native_type/native_type_id")
        if (
            type(self.formal_operation_ids) is not tuple
            or not self.formal_operation_ids
            or self.formal_operation_ids != tuple(sorted(self.formal_operation_ids))
            or len(set(self.formal_operation_ids)) != len(self.formal_operation_ids)
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "native_type/formal_operation_ids")
        for item in self.formal_operation_ids:
            _identifier(item, "native_type/formal_operation_ids")
        if type(self.verification) is not FreeCadPromotionVerificationBinding:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "native_type/verification")

    def _mapping(self) -> dict[str, object]:
        return {
            "formal_operation_ids": list(self.formal_operation_ids),
            "native_type_id": self.native_type_id,
            "verification": _verification_mapping(self.verification),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadManagedReviewedVerificationSet:
    schema_version: int
    runtime_backend: CapabilityBackend
    receipt_sha256: tuple[str, ...]
    formal_operations: tuple[FreeCadVerifiedFormalOperation, ...]
    native_types: tuple[FreeCadVerifiedNativeType, ...]
    verification_set_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != FREECAD_REVIEWED_VERIFICATION_SET_SCHEMA_VERSION:
            _fail(CapabilityCatalogErrorCode.UNSUPPORTED_VERSION, "schema_version")
        if type(self.runtime_backend) is not CapabilityBackend:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "runtime_backend")
        if (
            type(self.receipt_sha256) is not tuple
            or not self.receipt_sha256
            or len(self.receipt_sha256) > MAX_FREECAD_REVIEWED_VERIFICATION_RECEIPTS
            or self.receipt_sha256 != tuple(sorted(self.receipt_sha256))
            or len(set(self.receipt_sha256)) != len(self.receipt_sha256)
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "receipt_sha256")
        for item in self.receipt_sha256:
            _digest(item, "receipt_sha256")
        if (
            type(self.formal_operations) is not tuple
            or not self.formal_operations
            or len(self.formal_operations) > 256
            or any(
                type(item) is not FreeCadVerifiedFormalOperation for item in self.formal_operations
            )
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "formal_operations")
        if (
            type(self.native_types) is not tuple
            or not self.native_types
            or len(self.native_types) > 256
            or any(type(item) is not FreeCadVerifiedNativeType for item in self.native_types)
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "native_types")
        if self.formal_operations != tuple(
            sorted(self.formal_operations, key=lambda item: item.operation_id)
        ) or len({item.operation_id for item in self.formal_operations}) != len(
            self.formal_operations
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "formal_operations")
        if self.native_types != tuple(
            sorted(self.native_types, key=lambda item: item.native_type_id)
        ) or len({item.native_type_id for item in self.native_types}) != len(self.native_types):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "native_types")
        formal_ids = {item.operation_id for item in self.formal_operations}
        if any(
            item.test_receipt_sha256 not in self.receipt_sha256 for item in self.formal_operations
        ) or any(
            not set(item.formal_operation_ids) <= formal_ids
            or item.verification.runtime_build_sha256
            != self.runtime_backend.build_fingerprint_sha256
            for item in self.native_types
        ):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "verification_set")
        body = self._mapping()
        object.__setattr__(
            self,
            "verification_set_sha256",
            hashlib.sha256(_VERIFICATION_SET_DIGEST_DOMAIN + _canonical(body)).hexdigest(),
        )

    def _mapping(self) -> dict[str, object]:
        return {
            "formal_operations": [item._mapping() for item in self.formal_operations],
            "native_types": [item._mapping() for item in self.native_types],
            "receipt_sha256": list(self.receipt_sha256),
            "runtime_backend": _backend_mapping(self.runtime_backend),
            "schema_version": self.schema_version,
        }

    @property
    def verification_by_native_type(
        self,
    ) -> dict[str, FreeCadPromotionVerificationBinding]:
        return {item.native_type_id: item.verification for item in self.native_types}


def _operation_contract_matches(
    binding: ReviewedOperationVerificationBinding,
    operation: ReviewedOperationSpec,
) -> bool:
    return (
        binding.operation_id == operation.operation_id
        and binding.operation_specification_sha256 == operation.specification_sha256
        and binding.native_type_id == operation.native_type_id
    )


def _validate_receipt_manifest(
    receipt: ReviewedVerificationReceipt,
    manifest: FamilyBatchManifest,
) -> None:
    contract = receipt.contract
    if (
        contract.evidence_kind is not ReviewedConformanceEvidenceKind.MANAGED_FREECAD
        or contract.family_id != manifest.family_id
        or contract.family_version != manifest.family_version
        or contract.family_manifest_sha256 != manifest.manifest_sha256
        or contract.adapter_id != manifest.adapter.adapter_id
        or contract.adapter_version != manifest.adapter.adapter_version
        or contract.adapter_contract_sha256 != manifest.adapter.adapter_contract_sha256
        or contract.rule_id != manifest.rule_id
        or contract.rule_contract_sha256 != manifest.rule_contract_sha256
        or len(contract.operations) != len(manifest.operations)
        or any(
            not _operation_contract_matches(binding, operation)
            for binding, operation in zip(
                contract.operations,
                manifest.operations,
                strict=True,
            )
        )
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "receipts/manifest")


def _semantic_spellings(operation: ReviewedOperationSpec) -> tuple[str, str]:
    namespace, vocabulary_version, term_id, definition_sha256 = (
        operation.semantic_term.semantic_identity
    )
    return (
        term_id,
        f"{namespace}/{vocabulary_version}/{term_id}@{definition_sha256}",
    )


def _spec_matches_operation(
    spec: FreeCadIntentCapabilitySpec,
    *,
    manifest: FamilyBatchManifest,
    operation: ReviewedOperationSpec,
) -> bool:
    operation_ids = (
        operation.operation_id,
        f"{manifest.family_id}.{operation.operation_id}",
    )
    return (
        spec.operation_id in operation_ids
        and spec.semantic_operation in _semantic_spellings(operation)
        and spec.native_type_id == operation.native_type_id
        and spec.adapter_id == manifest.adapter.adapter_id
        and spec.adapter_version == manifest.adapter.adapter_version
        and spec.adapter_contract_sha256 == manifest.adapter.adapter_contract_sha256
        and spec.rule_id == manifest.rule_id
        and spec.rule_contract_sha256 == manifest.rule_contract_sha256
        and spec.verification is None
    )


def build_managed_reviewed_verification_set(
    *,
    runtime_backend: CapabilityBackend,
    receipts: tuple[ReviewedVerificationReceipt, ...],
    manifests: tuple[FamilyBatchManifest, ...],
    formal_specs: tuple[FreeCadIntentCapabilitySpec, ...],
    promotion_specs: tuple[FreeCadIntentCapabilitySpec, ...],
) -> FreeCadManagedReviewedVerificationSet:
    """Require exact all-operation coverage and derive native bindings."""

    if type(runtime_backend) is not CapabilityBackend:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "runtime_backend")

    for path, values, item_type, maximum in (
        (
            "receipts",
            receipts,
            ReviewedVerificationReceipt,
            MAX_FREECAD_REVIEWED_VERIFICATION_RECEIPTS,
        ),
        ("manifests", manifests, FamilyBatchManifest, MAX_FREECAD_REVIEWED_VERIFICATION_RECEIPTS),
        ("formal_specs", formal_specs, FreeCadIntentCapabilitySpec, 256),
        ("promotion_specs", promotion_specs, FreeCadIntentCapabilitySpec, 256),
    ):
        if type(values) is not tuple or not values:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
        if len(values) > maximum:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, path)
        if any(type(item) is not item_type for item in values):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)

    receipts = tuple(sorted(receipts, key=lambda item: item.test_receipt_sha256))
    manifests = tuple(sorted(manifests, key=lambda item: item.manifest_sha256))
    formal_specs = tuple(sorted(formal_specs, key=lambda item: item.operation_id))
    promotion_specs = tuple(sorted(promotion_specs, key=lambda item: item.operation_id))
    if (
        len({item.test_receipt_sha256 for item in receipts}) != len(receipts)
        or len({item.contract.family_manifest_sha256 for item in receipts}) != len(receipts)
        or len({item.manifest_sha256 for item in manifests}) != len(manifests)
        or len({item.operation_id for item in formal_specs}) != len(formal_specs)
        or len({item.operation_id for item in promotion_specs}) != len(promotion_specs)
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "duplicates")
    if any(item.verification is not None for item in (*formal_specs, *promotion_specs)):
        _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "specs/verification")

    receipt_by_manifest = {item.contract.family_manifest_sha256: item for item in receipts}
    if set(receipt_by_manifest) != {item.manifest_sha256 for item in manifests}:
        _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "receipts/manifests")
    backends = {item.contract.runtime_backend for item in receipts}
    if len(backends) != 1:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "receipts/runtime_backend")
    receipt_backend = next(iter(backends))
    if receipt_backend != runtime_backend:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "receipts/runtime_backend")
    if (
        runtime_backend.backend_id != "freecad"
        or runtime_backend.discovery_profile is not CapabilityExecutionProfile.HEADLESS
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "receipts/runtime_backend")

    receipt_by_operation: dict[str, ReviewedVerificationReceipt] = {}
    for manifest in manifests:
        receipt = receipt_by_manifest[manifest.manifest_sha256]
        _validate_receipt_manifest(receipt, manifest)
        for operation in manifest.operations:
            matches = tuple(
                spec
                for spec in formal_specs
                if _spec_matches_operation(spec, manifest=manifest, operation=operation)
            )
            if len(matches) != 1 or matches[0].operation_id in receipt_by_operation:
                _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "formal_specs/coverage")
            receipt_by_operation[matches[0].operation_id] = receipt
    if set(receipt_by_operation) != {item.operation_id for item in formal_specs}:
        _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "formal_specs/coverage")

    formal_by_id = {item.operation_id: item for item in formal_specs}
    if any(
        item.operation_id not in formal_by_id
        or _formal_spec_sha256(item) != _formal_spec_sha256(formal_by_id[item.operation_id])
        for item in promotion_specs
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "promotion_specs")

    formal_refs = tuple(
        FreeCadVerifiedFormalOperation(
            operation_id=spec.operation_id,
            formal_spec_sha256=_formal_spec_sha256(spec),
            test_receipt_sha256=receipt_by_operation[spec.operation_id].test_receipt_sha256,
        )
        for spec in formal_specs
    )
    native_groups: dict[str, list[FreeCadIntentCapabilitySpec]] = {}
    for spec in promotion_specs:
        native_groups.setdefault(spec.native_type_id, []).append(spec)
    native_refs: list[FreeCadVerifiedNativeType] = []
    for native_type_id, specs in sorted(native_groups.items()):
        bindings = tuple(
            build_promotion_verification_binding(receipt_by_operation[item.operation_id])
            for item in specs
        )
        if len(set(bindings)) != 1:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "promotion_specs/native_type_id")
        native_refs.append(
            FreeCadVerifiedNativeType(
                native_type_id=native_type_id,
                formal_operation_ids=tuple(sorted(item.operation_id for item in specs)),
                verification=bindings[0],
            )
        )
    return FreeCadManagedReviewedVerificationSet(
        schema_version=FREECAD_REVIEWED_VERIFICATION_SET_SCHEMA_VERSION,
        runtime_backend=runtime_backend,
        receipt_sha256=tuple(item.test_receipt_sha256 for item in receipts),
        formal_operations=formal_refs,
        native_types=tuple(native_refs),
    )


__all__ = ()
