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
_CURRENT_FORMAL_CATALOG_DIGEST_DOMAIN = b"vibecad-freecad-reviewed-current-formal-catalog-v1\0"
_CURRENT_PROMOTION_CATALOG_DIGEST_DOMAIN = (
    b"vibecad-freecad-reviewed-current-promotion-catalog-v1\0"
)
_VERIFICATION_SET_DIGEST_DOMAIN = b"vibecad-freecad-reviewed-verification-set-v1\0"
_VERIFICATION_SET_BUILDER_TOKEN = object()
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


def _spec_catalog_sha256(
    *,
    domain: bytes,
    specs: tuple[FreeCadIntentCapabilitySpec, ...],
) -> str:
    ordered = tuple(sorted(specs, key=lambda item: item.operation_id))
    if (
        not ordered
        or len(ordered) > 256
        or len({item.operation_id for item in ordered}) != len(ordered)
        or any(item.verification is not None for item in ordered)
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "current_catalog")
    return hashlib.sha256(
        domain
        + _canonical(
            {
                "schema_version": FREECAD_REVIEWED_VERIFICATION_SET_SCHEMA_VERSION,
                "specs": [_formal_spec_mapping(item) for item in ordered],
            }
        )
    ).hexdigest()


def _current_catalogs() -> tuple[
    tuple[FreeCadIntentCapabilitySpec, ...],
    tuple[FreeCadIntentCapabilitySpec, ...],
    str,
    str,
]:
    # Lazy import keeps this authority-free ledger outside capability-module
    # initialization while still anchoring every accepted subset to the exact
    # built-in inventory shipped by the current source tree.
    from vibecad.execution.freecad_builtin_intent_capabilities import (
        current_freecad_intent_capability_specs,
        current_freecad_intent_promotion_specs,
    )

    formal = current_freecad_intent_capability_specs()
    promotion = current_freecad_intent_promotion_specs()
    if (
        type(formal) is not tuple
        or type(promotion) is not tuple
        or not formal
        or not promotion
        or any(type(item) is not FreeCadIntentCapabilitySpec for item in (*formal, *promotion))
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "current_catalog")
    formal_by_id = {item.operation_id: item for item in formal}
    if any(
        item.operation_id not in formal_by_id
        or _formal_spec_sha256(item) != _formal_spec_sha256(formal_by_id[item.operation_id])
        for item in promotion
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "current_catalog/promotion")
    return (
        formal,
        promotion,
        _spec_catalog_sha256(domain=_CURRENT_FORMAL_CATALOG_DIGEST_DOMAIN, specs=formal),
        _spec_catalog_sha256(
            domain=_CURRENT_PROMOTION_CATALOG_DIGEST_DOMAIN,
            specs=promotion,
        ),
    )


def _current_manifests() -> dict[str, FamilyBatchManifest]:
    from vibecad.execution.freecad_legacy_reviewed_verification import (
        LEGACY_REVIEWED_FAMILY_MANIFESTS,
    )
    from vibecad.execution.freecad_reviewed_family_capabilities import (
        CURRENT_FREECAD_REVIEWED_FAMILY_MANIFESTS,
    )

    manifests = (
        *LEGACY_REVIEWED_FAMILY_MANIFESTS,
        *CURRENT_FREECAD_REVIEWED_FAMILY_MANIFESTS,
    )
    by_id = {item.family_id: item for item in manifests}
    if (
        not manifests
        or len(by_id) != len(manifests)
        or len({item.manifest_sha256 for item in manifests}) != len(manifests)
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "current_manifests")
    return by_id


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


@dataclass(frozen=True, slots=True, init=False)
class FreeCadManagedReviewedVerificationSet:
    """Opaque, ephemeral closure over exact managed receipts and current catalogs.

    This v1 object is intentionally not a persisted release attestation.  It
    has no public constructor or decoder; a future durable attestation needs a
    versioned receipt-contract summary and its own trust policy.
    """

    schema_version: int
    runtime_backend: CapabilityBackend
    current_formal_catalog_sha256: str
    current_promotion_catalog_sha256: str
    receipt_sha256: tuple[str, ...]
    formal_operations: tuple[FreeCadVerifiedFormalOperation, ...]
    native_types: tuple[FreeCadVerifiedNativeType, ...]
    _builder_token: object = field(repr=False, compare=False)
    verification_set_sha256: str = field(init=False)

    @classmethod
    def _create(
        cls,
        *,
        schema_version: int,
        runtime_backend: CapabilityBackend,
        current_formal_catalog_sha256: str,
        current_promotion_catalog_sha256: str,
        receipt_sha256: tuple[str, ...],
        formal_operations: tuple[FreeCadVerifiedFormalOperation, ...],
        native_types: tuple[FreeCadVerifiedNativeType, ...],
        builder_token: object,
    ) -> FreeCadManagedReviewedVerificationSet:
        if builder_token is not _VERIFICATION_SET_BUILDER_TOKEN:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "verification_set/builder")
        value = object.__new__(cls)
        for name, field_value in (
            ("schema_version", schema_version),
            ("runtime_backend", runtime_backend),
            ("current_formal_catalog_sha256", current_formal_catalog_sha256),
            ("current_promotion_catalog_sha256", current_promotion_catalog_sha256),
            ("receipt_sha256", receipt_sha256),
            ("formal_operations", formal_operations),
            ("native_types", native_types),
            ("_builder_token", _VERIFICATION_SET_BUILDER_TOKEN),
        ):
            object.__setattr__(value, name, field_value)
        value.__post_init__()
        return value

    def __post_init__(self) -> None:
        if self._builder_token is not _VERIFICATION_SET_BUILDER_TOKEN:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "verification_set/builder")
        if (
            type(self.schema_version) is not int
            or self.schema_version != FREECAD_REVIEWED_VERIFICATION_SET_SCHEMA_VERSION
        ):
            _fail(CapabilityCatalogErrorCode.UNSUPPORTED_VERSION, "schema_version")
        if type(self.runtime_backend) is not CapabilityBackend:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "runtime_backend")
        _digest(
            self.current_formal_catalog_sha256,
            "current_formal_catalog_sha256",
        )
        _digest(
            self.current_promotion_catalog_sha256,
            "current_promotion_catalog_sha256",
        )
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
        if (
            self.formal_operations
            != tuple(sorted(self.formal_operations, key=lambda item: item.operation_id))
            or len({item.operation_id for item in self.formal_operations})
            != len(self.formal_operations)
            or len({item.formal_spec_sha256 for item in self.formal_operations})
            != len(self.formal_operations)
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "formal_operations")
        if self.native_types != tuple(
            sorted(self.native_types, key=lambda item: item.native_type_id)
        ) or len({item.native_type_id for item in self.native_types}) != len(self.native_types):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "native_types")
        formal_ids = {item.operation_id for item in self.formal_operations}
        native_formal_ids = [
            operation_id for item in self.native_types for operation_id in item.formal_operation_ids
        ]
        if (
            any(
                item.test_receipt_sha256 not in self.receipt_sha256
                for item in self.formal_operations
            )
            or any(
                not set(item.formal_operation_ids) <= formal_ids
                or item.verification.runtime_build_sha256
                != self.runtime_backend.build_fingerprint_sha256
                or item.verification.test_receipt_sha256 not in self.receipt_sha256
                for item in self.native_types
            )
            or len(set(native_formal_ids)) != len(native_formal_ids)
        ):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "verification_set")
        object.__setattr__(
            self,
            "verification_set_sha256",
            self._expected_sha256(),
        )

    def _mapping(self) -> dict[str, object]:
        return {
            "current_formal_catalog_sha256": self.current_formal_catalog_sha256,
            "current_promotion_catalog_sha256": self.current_promotion_catalog_sha256,
            "formal_operations": [item._mapping() for item in self.formal_operations],
            "native_types": [item._mapping() for item in self.native_types],
            "receipt_sha256": list(self.receipt_sha256),
            "runtime_backend": _backend_mapping(self.runtime_backend),
            "schema_version": self.schema_version,
        }

    def _expected_sha256(self) -> str:
        return hashlib.sha256(
            _VERIFICATION_SET_DIGEST_DOMAIN + _canonical(self._mapping())
        ).hexdigest()

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


def _trusted_spec_for_operation(
    *,
    manifest: FamilyBatchManifest,
    operation: ReviewedOperationSpec,
    current_formal_by_id: dict[str, FreeCadIntentCapabilitySpec],
) -> FreeCadIntentCapabilitySpec:
    # Legacy verification-only manifests already carry the complete formal
    # operation id.  Reviewed-family manifests carry a local id and project it
    # through ``family_id.local_id``.  The current catalog, rather than caller
    # spelling, selects exactly one of those two forms.
    candidate_ids = {
        operation.operation_id,
        f"{manifest.family_id}.{operation.operation_id}",
    }
    candidates = tuple(
        current_formal_by_id[operation_id]
        for operation_id in sorted(candidate_ids)
        if operation_id in current_formal_by_id
    )
    if len(candidates) != 1:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "current_catalog/operation_id")
    spec = candidates[0]
    if (
        spec.semantic_operation not in _semantic_spellings(operation)
        or spec.native_type_id != operation.native_type_id
        or spec.adapter_id != manifest.adapter.adapter_id
        or spec.adapter_version != manifest.adapter.adapter_version
        or spec.adapter_contract_sha256 != manifest.adapter.adapter_contract_sha256
        or spec.rule_id != manifest.rule_id
        or spec.rule_contract_sha256 != manifest.rule_contract_sha256
        or spec.verification is not None
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "current_catalog/operation")
    return spec


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
        or len({item.family_id for item in manifests}) != len(manifests)
        or len({item.operation_id for item in formal_specs}) != len(formal_specs)
        or len({item.operation_id for item in promotion_specs}) != len(promotion_specs)
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "duplicates")
    if any(item.verification is not None for item in (*formal_specs, *promotion_specs)):
        _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "specs/verification")

    (
        current_formal,
        current_promotion,
        current_formal_catalog_sha256,
        current_promotion_catalog_sha256,
    ) = _current_catalogs()
    current_formal_by_id = {item.operation_id: item for item in current_formal}
    current_promotion_by_id = {item.operation_id: item for item in current_promotion}
    for path, specs, trusted_by_id in (
        ("formal_specs/current_catalog", formal_specs, current_formal_by_id),
        ("promotion_specs/current_catalog", promotion_specs, current_promotion_by_id),
    ):
        if any(
            item.operation_id not in trusted_by_id
            or _formal_spec_sha256(item) != _formal_spec_sha256(trusted_by_id[item.operation_id])
            for item in specs
        ):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)

    current_manifest_by_id = _current_manifests()
    if any(
        manifest.family_id not in current_manifest_by_id
        or manifest != current_manifest_by_id[manifest.family_id]
        for manifest in manifests
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "manifests/current_registry")

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

    formal_by_id = {item.operation_id: item for item in formal_specs}
    receipt_by_operation: dict[str, ReviewedVerificationReceipt] = {}
    for manifest in manifests:
        receipt = receipt_by_manifest[manifest.manifest_sha256]
        _validate_receipt_manifest(receipt, manifest)
        for operation in manifest.operations:
            expected = _trusted_spec_for_operation(
                manifest=manifest,
                operation=operation,
                current_formal_by_id=current_formal_by_id,
            )
            supplied = formal_by_id.get(expected.operation_id)
            if (
                supplied is None
                or _formal_spec_sha256(supplied) != _formal_spec_sha256(expected)
                or expected.operation_id in receipt_by_operation
            ):
                _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "formal_specs/coverage")
            receipt_by_operation[expected.operation_id] = receipt
    if set(receipt_by_operation) != {item.operation_id for item in formal_specs}:
        _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "formal_specs/coverage")

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
    current_native_groups: dict[str, list[FreeCadIntentCapabilitySpec]] = {}
    for spec in current_promotion:
        current_native_groups.setdefault(spec.native_type_id, []).append(spec)
    native_refs: list[FreeCadVerifiedNativeType] = []
    for native_type_id, specs in sorted(native_groups.items()):
        expected_operation_ids = tuple(
            sorted(item.operation_id for item in current_native_groups[native_type_id])
        )
        actual_operation_ids = tuple(sorted(item.operation_id for item in specs))
        if actual_operation_ids != expected_operation_ids:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "promotion_specs/native_scope")
        bindings = tuple(
            build_promotion_verification_binding(receipt_by_operation[item.operation_id])
            for item in specs
        )
        if len(set(bindings)) != 1:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "promotion_specs/native_type_id")
        native_refs.append(
            FreeCadVerifiedNativeType(
                native_type_id=native_type_id,
                formal_operation_ids=actual_operation_ids,
                verification=bindings[0],
            )
        )
    return FreeCadManagedReviewedVerificationSet._create(
        schema_version=FREECAD_REVIEWED_VERIFICATION_SET_SCHEMA_VERSION,
        runtime_backend=runtime_backend,
        current_formal_catalog_sha256=current_formal_catalog_sha256,
        current_promotion_catalog_sha256=current_promotion_catalog_sha256,
        receipt_sha256=tuple(item.test_receipt_sha256 for item in receipts),
        formal_operations=formal_refs,
        native_types=tuple(native_refs),
        builder_token=_VERIFICATION_SET_BUILDER_TOKEN,
    )


def validated_verification_by_native_type(
    value: object,
    *,
    runtime_backend: CapabilityBackend,
) -> dict[str, FreeCadPromotionVerificationBinding]:
    """Revalidate one opaque set against the exact current runtime/catalogs."""

    if type(value) is not FreeCadManagedReviewedVerificationSet:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "verification_set")
    if type(runtime_backend) is not CapabilityBackend:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "runtime_backend")
    if value._builder_token is not _VERIFICATION_SET_BUILDER_TOKEN or (
        value.verification_set_sha256 != value._expected_sha256()
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "verification_set/digest")
    if value.runtime_backend != runtime_backend:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "verification_set/runtime_backend")

    (
        current_formal,
        current_promotion,
        current_formal_catalog_sha256,
        current_promotion_catalog_sha256,
    ) = _current_catalogs()
    if (
        value.current_formal_catalog_sha256 != current_formal_catalog_sha256
        or value.current_promotion_catalog_sha256 != current_promotion_catalog_sha256
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "verification_set/current_catalog")

    current_formal_by_id = {item.operation_id: item for item in current_formal}
    formal_ids = {item.operation_id for item in value.formal_operations}
    if any(
        item.operation_id not in current_formal_by_id
        or item.formal_spec_sha256 != _formal_spec_sha256(current_formal_by_id[item.operation_id])
        for item in value.formal_operations
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "verification_set/formal_specs")

    current_native_groups: dict[str, list[FreeCadIntentCapabilitySpec]] = {}
    for spec in current_promotion:
        current_native_groups.setdefault(spec.native_type_id, []).append(spec)
    for native in value.native_types:
        expected_specs = current_native_groups.get(native.native_type_id)
        expected_operation_ids = (
            ()
            if expected_specs is None
            else tuple(sorted(item.operation_id for item in expected_specs))
        )
        if (
            native.formal_operation_ids != expected_operation_ids
            or not set(expected_operation_ids) <= formal_ids
            or any(
                spec.adapter_contract_sha256 != native.verification.adapter_contract_sha256
                for spec in expected_specs or ()
            )
        ):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "verification_set/native_scope")
    return value.verification_by_native_type


__all__ = ()
