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
import hmac
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
    REVIEWED_VERIFICATION_SCHEMA_VERSION,
    ReviewedConformanceEvidenceKind,
    ReviewedOperationVerificationBinding,
    ReviewedVerificationReceipt,
    ReviewedVerificationTestContract,
    build_promotion_verification_binding,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    FamilyBatchManifest,
    ReviewedOperationSpec,
)

FREECAD_REVIEWED_VERIFICATION_SET_SCHEMA_VERSION: Final = 2
MAX_FREECAD_REVIEWED_VERIFICATION_RECEIPTS: Final = 32
MAX_FREECAD_REVIEWED_VERIFICATION_SET_BYTES: Final = 2 * 1024 * 1024
MAX_FREECAD_REVIEWED_VERIFICATION_OPERATIONS: Final = 256
MAX_FREECAD_REVIEWED_VERIFICATION_NATIVE_TYPES: Final = 256

_FORMAL_SPEC_DIGEST_DOMAIN = b"vibecad-freecad-reviewed-formal-spec-v2\0"
_CURRENT_FORMAL_CATALOG_DIGEST_DOMAIN = b"vibecad-freecad-reviewed-current-formal-catalog-v2\0"
_CURRENT_PROMOTION_CATALOG_DIGEST_DOMAIN = (
    b"vibecad-freecad-reviewed-current-promotion-catalog-v2\0"
)
_VERIFICATION_SET_DIGEST_DOMAIN = b"vibecad-freecad-reviewed-verification-set-v2\0"
_VERIFICATION_SET_BUILDER_TOKEN = object()
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,255}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_SAFE_INTEGER = 2**53 - 1


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


def _version(value: object, path: str) -> str:
    if type(value) is not str or _VERSION.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _exact(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _decode_canonical_mapping(raw: object) -> dict[str, object]:
    if type(raw) is not bytes:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "verification_set")
    if not raw:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "verification_set")
    if len(raw) > MAX_FREECAD_REVIEWED_VERIFICATION_SET_BYTES:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "verification_set")

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        raise ValueError("non-finite number")

    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "verification_set")
    if type(value) is not dict or _canonical(value) != raw:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "verification_set")
    return value


def _backend_mapping(value: CapabilityBackend) -> dict[str, object]:
    return {
        "backend_id": value.backend_id,
        "backend_version": list(value.backend_version),
        "build_fingerprint_sha256": value.build_fingerprint_sha256,
        "discovery_profile": value.discovery_profile.value,
        "platform_id": value.platform_id,
    }


def _backend_from_mapping(value: object, path: str) -> CapabilityBackend:
    item = _exact(
        value,
        {
            "backend_id",
            "backend_version",
            "build_fingerprint_sha256",
            "discovery_profile",
            "platform_id",
        },
        path,
    )
    raw_version = item["backend_version"]
    if type(raw_version) is not list:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, f"{path}/backend_version")
    try:
        profile = CapabilityExecutionProfile(item["discovery_profile"])
    except (TypeError, ValueError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, f"{path}/discovery_profile")
    return CapabilityBackend(
        backend_id=item["backend_id"],
        backend_version=tuple(raw_version),
        build_fingerprint_sha256=item["build_fingerprint_sha256"],
        discovery_profile=profile,
        platform_id=item["platform_id"],
    )


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


def _verification_from_mapping(
    value: object,
    path: str,
) -> FreeCadPromotionVerificationBinding:
    item = _exact(
        value,
        {
            "adapter_contract_sha256",
            "runtime_build_sha256",
            "test_contract_sha256",
            "test_receipt_sha256",
            "test_receipt_size_bytes",
            "verifier_id",
            "verifier_version",
        },
        path,
    )
    return FreeCadPromotionVerificationBinding(**item)


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadReviewedReceiptContractSummary:
    """Durable, authority-free contract identity for one managed receipt."""

    test_receipt_sha256: str
    test_receipt_size_bytes: int
    test_contract_sha256: str
    case_manifest_sha256: str
    family_id: str
    family_version: str
    family_manifest_sha256: str
    adapter_id: str
    adapter_version: str
    adapter_contract_sha256: str
    rule_id: str
    rule_contract_sha256: str
    verifier_id: str
    verifier_version: str
    evidence_kind: ReviewedConformanceEvidenceKind

    def __post_init__(self) -> None:
        for path, value in (
            ("test_receipt_sha256", self.test_receipt_sha256),
            ("test_contract_sha256", self.test_contract_sha256),
            ("case_manifest_sha256", self.case_manifest_sha256),
            ("family_manifest_sha256", self.family_manifest_sha256),
            ("adapter_contract_sha256", self.adapter_contract_sha256),
            ("rule_contract_sha256", self.rule_contract_sha256),
        ):
            _digest(value, f"receipt/{path}")
        for path, value in (
            ("family_id", self.family_id),
            ("adapter_id", self.adapter_id),
            ("rule_id", self.rule_id),
            ("verifier_id", self.verifier_id),
        ):
            _identifier(value, f"receipt/{path}")
        for path, value in (
            ("family_version", self.family_version),
            ("adapter_version", self.adapter_version),
            ("verifier_version", self.verifier_version),
        ):
            _version(value, f"receipt/{path}")
        if (
            type(self.test_receipt_size_bytes) is not int
            or not 0 < self.test_receipt_size_bytes <= _MAX_SAFE_INTEGER
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "receipt/test_receipt_size_bytes")
        if self.evidence_kind is not ReviewedConformanceEvidenceKind.MANAGED_FREECAD:
            _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "receipt/evidence_kind")

    @classmethod
    def _from_receipt(
        cls,
        receipt: ReviewedVerificationReceipt,
    ) -> FreeCadReviewedReceiptContractSummary:
        if type(receipt) is not ReviewedVerificationReceipt:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "receipt")
        contract = receipt.contract
        return cls(
            test_receipt_sha256=receipt.test_receipt_sha256,
            test_receipt_size_bytes=receipt.test_receipt_size_bytes,
            test_contract_sha256=receipt.test_contract_sha256,
            case_manifest_sha256=receipt.case_manifest.case_manifest_sha256,
            family_id=contract.family_id,
            family_version=contract.family_version,
            family_manifest_sha256=contract.family_manifest_sha256,
            adapter_id=contract.adapter_id,
            adapter_version=contract.adapter_version,
            adapter_contract_sha256=contract.adapter_contract_sha256,
            rule_id=contract.rule_id,
            rule_contract_sha256=contract.rule_contract_sha256,
            verifier_id=contract.verifier_id,
            verifier_version=contract.verifier_version,
            evidence_kind=contract.evidence_kind,
        )

    @classmethod
    def _from_mapping(
        cls,
        value: object,
        path: str,
    ) -> FreeCadReviewedReceiptContractSummary:
        item = _exact(
            value,
            {
                "adapter",
                "case_manifest_sha256",
                "evidence_kind",
                "family",
                "rule",
                "test_contract_sha256",
                "test_receipt_sha256",
                "test_receipt_size_bytes",
                "verifier",
            },
            path,
        )
        adapter = _exact(
            item["adapter"],
            {"contract_sha256", "id", "version"},
            f"{path}/adapter",
        )
        family = _exact(
            item["family"],
            {"id", "manifest_sha256", "version"},
            f"{path}/family",
        )
        rule = _exact(
            item["rule"],
            {"contract_sha256", "id"},
            f"{path}/rule",
        )
        verifier = _exact(
            item["verifier"],
            {"id", "version"},
            f"{path}/verifier",
        )
        try:
            evidence_kind = ReviewedConformanceEvidenceKind(item["evidence_kind"])
        except (TypeError, ValueError):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, f"{path}/evidence_kind")
        return cls(
            test_receipt_sha256=item["test_receipt_sha256"],
            test_receipt_size_bytes=item["test_receipt_size_bytes"],
            test_contract_sha256=item["test_contract_sha256"],
            case_manifest_sha256=item["case_manifest_sha256"],
            family_id=family["id"],
            family_version=family["version"],
            family_manifest_sha256=family["manifest_sha256"],
            adapter_id=adapter["id"],
            adapter_version=adapter["version"],
            adapter_contract_sha256=adapter["contract_sha256"],
            rule_id=rule["id"],
            rule_contract_sha256=rule["contract_sha256"],
            verifier_id=verifier["id"],
            verifier_version=verifier["version"],
            evidence_kind=evidence_kind,
        )

    def _mapping(self) -> dict[str, object]:
        return {
            "adapter": {
                "contract_sha256": self.adapter_contract_sha256,
                "id": self.adapter_id,
                "version": self.adapter_version,
            },
            "case_manifest_sha256": self.case_manifest_sha256,
            "evidence_kind": self.evidence_kind.value,
            "family": {
                "id": self.family_id,
                "manifest_sha256": self.family_manifest_sha256,
                "version": self.family_version,
            },
            "rule": {
                "contract_sha256": self.rule_contract_sha256,
                "id": self.rule_id,
            },
            "test_contract_sha256": self.test_contract_sha256,
            "test_receipt_sha256": self.test_receipt_sha256,
            "test_receipt_size_bytes": self.test_receipt_size_bytes,
            "verifier": {"id": self.verifier_id, "version": self.verifier_version},
        }


def _verification_matches_receipt(
    verification: FreeCadPromotionVerificationBinding,
    receipt: FreeCadReviewedReceiptContractSummary | None,
) -> bool:
    return receipt is not None and (
        verification.adapter_contract_sha256 == receipt.adapter_contract_sha256
        and verification.test_contract_sha256 == receipt.test_contract_sha256
        and verification.test_receipt_sha256 == receipt.test_receipt_sha256
        and verification.test_receipt_size_bytes == receipt.test_receipt_size_bytes
        and verification.verifier_id == receipt.verifier_id
        and verification.verifier_version == receipt.verifier_version
    )


def _receipt_test_contract_matches(
    receipt: FreeCadReviewedReceiptContractSummary,
    *,
    manifest: FamilyBatchManifest,
    runtime_backend: CapabilityBackend,
) -> bool:
    contract = ReviewedVerificationTestContract(
        schema_version=REVIEWED_VERIFICATION_SCHEMA_VERSION,
        runtime_backend=runtime_backend,
        family_id=manifest.family_id,
        family_version=manifest.family_version,
        family_manifest_sha256=manifest.manifest_sha256,
        adapter_id=manifest.adapter.adapter_id,
        adapter_version=manifest.adapter.adapter_version,
        adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
        rule_id=manifest.rule_id,
        rule_contract_sha256=manifest.rule_contract_sha256,
        operations=tuple(
            ReviewedOperationVerificationBinding(
                operation_id=item.operation_id,
                operation_specification_sha256=item.specification_sha256,
                native_type_id=item.native_type_id,
            )
            for item in manifest.operations
        ),
        case_manifest_sha256=receipt.case_manifest_sha256,
        evidence_kind=receipt.evidence_kind,
        verifier_id=receipt.verifier_id,
        verifier_version=receipt.verifier_version,
    )
    return hmac.compare_digest(
        receipt.test_contract_sha256,
        contract.test_contract_sha256,
    )


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

    @classmethod
    def _from_mapping(cls, value: object, path: str) -> FreeCadVerifiedFormalOperation:
        item = _exact(
            value,
            {"formal_spec_sha256", "operation_id", "test_receipt_sha256"},
            path,
        )
        return cls(**item)


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

    @classmethod
    def _from_mapping(cls, value: object, path: str) -> FreeCadVerifiedNativeType:
        item = _exact(
            value,
            {"formal_operation_ids", "native_type_id", "verification"},
            path,
        )
        operation_ids = item["formal_operation_ids"]
        if type(operation_ids) is not list:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, f"{path}/formal_operation_ids")
        return cls(
            native_type_id=item["native_type_id"],
            formal_operation_ids=tuple(operation_ids),
            verification=_verification_from_mapping(
                item["verification"],
                f"{path}/verification",
            ),
        )


@dataclass(frozen=True, slots=True, init=False)
class FreeCadManagedReviewedVerificationSet:
    """Opaque, durable closure over exact managed receipts and current catalogs.

    The object remains inert metadata and grants no execution authority.  Its
    v2 receipt summaries retain the complete reviewed contract identity even
    when a formal operation has no independent native-TypeId promotion owner.
    """

    schema_version: int
    runtime_backend: CapabilityBackend
    current_formal_catalog_sha256: str
    current_promotion_catalog_sha256: str
    receipts: tuple[FreeCadReviewedReceiptContractSummary, ...]
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
        receipts: tuple[FreeCadReviewedReceiptContractSummary, ...],
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
            ("receipts", receipts),
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
        if type(self.receipts) is not tuple or not self.receipts:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "receipts")
        if len(self.receipts) > MAX_FREECAD_REVIEWED_VERIFICATION_RECEIPTS:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "receipts")
        if any(
            type(item) is not FreeCadReviewedReceiptContractSummary for item in self.receipts
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "receipts")
        if self.receipts != tuple(
            sorted(self.receipts, key=lambda item: item.test_receipt_sha256)
        ) or any(
            len({getattr(item, field_name) for item in self.receipts}) != len(self.receipts)
            for field_name in ("test_receipt_sha256", "family_id", "family_manifest_sha256")
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "receipts")
        if (
            type(self.formal_operations) is not tuple
            or not self.formal_operations
            or any(
                type(item) is not FreeCadVerifiedFormalOperation for item in self.formal_operations
            )
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "formal_operations")
        if len(self.formal_operations) > MAX_FREECAD_REVIEWED_VERIFICATION_OPERATIONS:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "formal_operations")
        if (
            type(self.native_types) is not tuple
            or not self.native_types
            or any(type(item) is not FreeCadVerifiedNativeType for item in self.native_types)
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "native_types")
        if len(self.native_types) > MAX_FREECAD_REVIEWED_VERIFICATION_NATIVE_TYPES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "native_types")
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
        receipt_by_sha256 = {item.test_receipt_sha256: item for item in self.receipts}
        referenced_receipts = {
            item.test_receipt_sha256 for item in self.formal_operations
        }
        native_formal_ids = [
            operation_id for item in self.native_types for operation_id in item.formal_operation_ids
        ]
        if (
            referenced_receipts != set(receipt_by_sha256)
            or any(
                item.test_receipt_sha256 not in receipt_by_sha256
                for item in self.formal_operations
            )
            or any(
                not set(item.formal_operation_ids) <= formal_ids
                or item.verification.runtime_build_sha256
                != self.runtime_backend.build_fingerprint_sha256
                or item.verification.test_receipt_sha256 not in receipt_by_sha256
                or not _verification_matches_receipt(
                    item.verification,
                    receipt_by_sha256.get(item.verification.test_receipt_sha256),
                )
                or {
                    formal.test_receipt_sha256
                    for formal in self.formal_operations
                    if formal.operation_id in item.formal_operation_ids
                }
                != {item.verification.test_receipt_sha256}
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
            "receipts": [item._mapping() for item in self.receipts],
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

    @property
    def receipt_sha256(self) -> tuple[str, ...]:
        """Compatibility view; durable data lives in ``receipts`` summaries."""

        return tuple(item.test_receipt_sha256 for item in self.receipts)


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
        receipts=tuple(
            sorted(
                (
                    FreeCadReviewedReceiptContractSummary._from_receipt(item)
                    for item in receipts
                ),
                key=lambda item: item.test_receipt_sha256,
            )
        ),
        formal_operations=formal_refs,
        native_types=tuple(native_refs),
        builder_token=_VERIFICATION_SET_BUILDER_TOKEN,
    )


def validate_managed_reviewed_verification_set(
    value: object,
    *,
    runtime_backend: CapabilityBackend,
    require_complete: bool = False,
) -> FreeCadManagedReviewedVerificationSet:
    """Revalidate one opaque set against the exact current runtime/catalogs."""

    if type(value) is not FreeCadManagedReviewedVerificationSet:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "verification_set")
    if type(runtime_backend) is not CapabilityBackend:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "runtime_backend")
    if type(require_complete) is not bool:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "require_complete")
    if value._builder_token is not _VERIFICATION_SET_BUILDER_TOKEN or not hmac.compare_digest(
        value.verification_set_sha256,
        value._expected_sha256(),
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
        not hmac.compare_digest(
            value.current_formal_catalog_sha256,
            current_formal_catalog_sha256,
        )
        or not hmac.compare_digest(
            value.current_promotion_catalog_sha256,
            current_promotion_catalog_sha256,
        )
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "verification_set/current_catalog")

    current_formal_by_id = {item.operation_id: item for item in current_formal}
    formal_by_id = {item.operation_id: item for item in value.formal_operations}
    formal_ids = set(formal_by_id)
    if any(
        item.operation_id not in current_formal_by_id
        or not hmac.compare_digest(
            item.formal_spec_sha256,
            _formal_spec_sha256(current_formal_by_id[item.operation_id]),
        )
        for item in value.formal_operations
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "verification_set/formal_specs")

    current_manifest_by_id = _current_manifests()
    receipts_by_sha256 = {item.test_receipt_sha256: item for item in value.receipts}
    receipt_family_ids = {item.family_id for item in value.receipts}
    for receipt in value.receipts:
        manifest = current_manifest_by_id.get(receipt.family_id)
        if manifest is None or (
            receipt.family_version != manifest.family_version
            or receipt.family_manifest_sha256 != manifest.manifest_sha256
            or receipt.adapter_id != manifest.adapter.adapter_id
            or receipt.adapter_version != manifest.adapter.adapter_version
            or receipt.adapter_contract_sha256 != manifest.adapter.adapter_contract_sha256
            or receipt.rule_id != manifest.rule_id
            or receipt.rule_contract_sha256 != manifest.rule_contract_sha256
        ):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "verification_set/receipts")
        if not _receipt_test_contract_matches(
            receipt,
            manifest=manifest,
            runtime_backend=runtime_backend,
        ):
            _fail(
                CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                "verification_set/receipts/test_contract",
            )
        expected_operation_ids = {
            _trusted_spec_for_operation(
                manifest=manifest,
                operation=operation,
                current_formal_by_id=current_formal_by_id,
            ).operation_id
            for operation in manifest.operations
        }
        actual_operation_ids = {
            operation.operation_id
            for operation in value.formal_operations
            if operation.test_receipt_sha256 == receipt.test_receipt_sha256
        }
        if actual_operation_ids != expected_operation_ids:
            _fail(
                CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                "verification_set/receipts/coverage",
            )

    current_native_groups: dict[str, list[FreeCadIntentCapabilitySpec]] = {}
    for spec in current_promotion:
        current_native_groups.setdefault(spec.native_type_id, []).append(spec)
    native_by_id = {item.native_type_id: item for item in value.native_types}
    for native in value.native_types:
        expected_specs = current_native_groups.get(native.native_type_id)
        expected_operation_ids = (
            ()
            if expected_specs is None
            else tuple(sorted(item.operation_id for item in expected_specs))
        )
        receipt = receipts_by_sha256.get(native.verification.test_receipt_sha256)
        if (
            native.formal_operation_ids != expected_operation_ids
            or not set(expected_operation_ids) <= formal_ids
            or not _verification_matches_receipt(native.verification, receipt)
            or any(
                spec.adapter_contract_sha256 != native.verification.adapter_contract_sha256
                for spec in expected_specs or ()
            )
        ):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "verification_set/native_scope")

    if require_complete and (
        formal_ids != set(current_formal_by_id)
        or set(native_by_id) != set(current_native_groups)
        or receipt_family_ids != set(current_manifest_by_id)
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "verification_set/completeness")
    return value


def validated_verification_by_native_type(
    value: object,
    *,
    runtime_backend: CapabilityBackend,
) -> dict[str, FreeCadPromotionVerificationBinding]:
    """Derive native bindings only after revalidating the opaque v2 set."""

    validated = validate_managed_reviewed_verification_set(
        value,
        runtime_backend=runtime_backend,
    )
    return validated.verification_by_native_type


def encode_freecad_managed_reviewed_verification_set(
    value: object,
) -> bytes:
    """Encode one opaque v2 set as bounded canonical JSON."""

    if type(value) is not FreeCadManagedReviewedVerificationSet:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "verification_set")
    validate_managed_reviewed_verification_set(
        value,
        runtime_backend=value.runtime_backend,
    )
    return _canonical(
        {**value._mapping(), "verification_set_sha256": value.verification_set_sha256}
    )


def decode_freecad_managed_reviewed_verification_set(
    raw: object,
) -> FreeCadManagedReviewedVerificationSet:
    """Strictly decode and current-catalog revalidate one canonical v2 set."""

    root = _exact(
        _decode_canonical_mapping(raw),
        {
            "current_formal_catalog_sha256",
            "current_promotion_catalog_sha256",
            "formal_operations",
            "native_types",
            "receipts",
            "runtime_backend",
            "schema_version",
            "verification_set_sha256",
        },
        "verification_set",
    )
    receipts = root["receipts"]
    formal_operations = root["formal_operations"]
    native_types = root["native_types"]
    for path, values, maximum in (
        ("receipts", receipts, MAX_FREECAD_REVIEWED_VERIFICATION_RECEIPTS),
        (
            "formal_operations",
            formal_operations,
            MAX_FREECAD_REVIEWED_VERIFICATION_OPERATIONS,
        ),
        (
            "native_types",
            native_types,
            MAX_FREECAD_REVIEWED_VERIFICATION_NATIVE_TYPES,
        ),
    ):
        if type(values) is not list or not values:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
        if len(values) > maximum:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, path)
    claimed_sha256 = _digest(
        root["verification_set_sha256"],
        "verification_set/verification_set_sha256",
    )
    value = FreeCadManagedReviewedVerificationSet._create(
        schema_version=root["schema_version"],
        runtime_backend=_backend_from_mapping(root["runtime_backend"], "runtime_backend"),
        current_formal_catalog_sha256=root["current_formal_catalog_sha256"],
        current_promotion_catalog_sha256=root["current_promotion_catalog_sha256"],
        receipts=tuple(
            FreeCadReviewedReceiptContractSummary._from_mapping(
                item,
                f"receipts/{index}",
            )
            for index, item in enumerate(receipts)
        ),
        formal_operations=tuple(
            FreeCadVerifiedFormalOperation._from_mapping(
                item,
                f"formal_operations/{index}",
            )
            for index, item in enumerate(formal_operations)
        ),
        native_types=tuple(
            FreeCadVerifiedNativeType._from_mapping(
                item,
                f"native_types/{index}",
            )
            for index, item in enumerate(native_types)
        ),
        builder_token=_VERIFICATION_SET_BUILDER_TOKEN,
    )
    if not hmac.compare_digest(claimed_sha256, value.verification_set_sha256):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "verification_set/digest")
    validate_managed_reviewed_verification_set(
        value,
        runtime_backend=value.runtime_backend,
    )
    if encode_freecad_managed_reviewed_verification_set(value) != raw:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "verification_set/canonical")
    return value


__all__ = ()
