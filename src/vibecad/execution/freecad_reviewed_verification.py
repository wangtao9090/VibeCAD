"""Private canonical receipts for Reviewed FreeCAD family conformance.

Reviewed manifests prove that code and native mappings were statically
reviewed.  They are not runtime verification evidence.  This module closes
the metadata gap without changing that boundary: a trusted host executes a
complete, bounded case matrix in the current process; the builder creates the
results itself and emits a content-addressed receipt.  Callers cannot submit a
tuple of claimed passing results.

Synthetic hosts are useful for contract tests only and can never produce a
``FreeCadPromotionVerificationBinding``.  A managed receipt is eligible only
when the managed-FreeCAD host factory authenticated and revalidated the exact
headless runtime in the same process.  Nothing here publishes a receipt,
persists it, promotes a capability, or grants execution authority.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
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
from vibecad.execution.freecad_discovery_runtime_v2 import (
    FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
    collect_managed_freecad_discovery_v2,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    MAX_REVIEWED_OPERATIONS,
    FamilyBatchManifest,
)

REVIEWED_VERIFICATION_SCHEMA_VERSION: Final = 1
MAX_REVIEWED_CONFORMANCE_CASES: Final = MAX_REVIEWED_OPERATIONS * 7
MAX_REVIEWED_OBSERVATION_BYTES: Final = 64 * 1024
MAX_REVIEWED_CASE_MANIFEST_BYTES: Final = 1024 * 1024
MAX_REVIEWED_TEST_CONTRACT_BYTES: Final = 1024 * 1024
MAX_REVIEWED_TEST_RECEIPT_BYTES: Final = 2 * 1024 * 1024

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CASE_DIGEST_DOMAIN = b"vibecad-reviewed-conformance-case-v1\0"
_CASE_CONTRACT_DIGEST_DOMAIN = b"vibecad-reviewed-conformance-case-contract-v1\0"
_CASE_MANIFEST_DIGEST_DOMAIN = b"vibecad-reviewed-conformance-case-manifest-v1\0"
_TEST_CONTRACT_DIGEST_DOMAIN = b"vibecad-reviewed-verification-test-contract-v1\0"
_CASE_CHALLENGE_DIGEST_DOMAIN = b"vibecad-reviewed-verification-case-challenge-v1\0"
_OBSERVATION_DIGEST_DOMAIN = b"vibecad-reviewed-conformance-observation-v1\0"
_CASE_RESULT_DIGEST_DOMAIN = b"vibecad-reviewed-conformance-case-result-v1\0"
_TEST_RECEIPT_DIGEST_DOMAIN = b"vibecad-reviewed-verification-test-receipt-v1\0"
_CASE_ADMISSION_DIGEST_DOMAIN = b"vibecad-reviewed-conformance-case-admission-v1\0"
_RECEIPT_BUILDER_TOKEN = object()
_HOST_BUILDER_TOKEN = object()
_CASE_ADMISSION_TOKEN = object()


class ReviewedConformanceFacet(StrEnum):
    CREATE = "create"
    EDIT = "edit"
    RECOMPUTE = "recompute"
    SAVE = "save"
    REOPEN = "reopen"
    NEGATIVE = "negative"
    LATE_ROLLBACK = "late_rollback"


class ReviewedConformanceEvidenceKind(StrEnum):
    SYNTHETIC = "synthetic"
    MANAGED_FREECAD = "managed_freecad"


class ReviewedCaseManifestKind(StrEnum):
    SYNTHETIC = "synthetic"
    REVIEWED_HOST = "reviewed_host"


REQUIRED_REVIEWED_CONFORMANCE_FACETS: Final = tuple(ReviewedConformanceFacet)


def _fail(code: CapabilityCatalogErrorCode, path: str) -> None:
    raise CapabilityCatalogError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if (
        not value
        or size > 128
        or _IDENTIFIER.fullmatch(value) is None
        or ".." in value
        or "//" in value
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _version(value: object, path: str) -> str:
    if type(value) is not str or _VERSION.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _canonical(value: object, *, maximum: int, path: str) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if not raw or len(raw) > maximum:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, path)
    return raw


def _backend_mapping(value: CapabilityBackend) -> dict[str, object]:
    return {
        "backend_id": value.backend_id,
        "backend_version": list(value.backend_version),
        "build_fingerprint_sha256": value.build_fingerprint_sha256,
        "discovery_profile": value.discovery_profile.value,
        "platform_id": value.platform_id,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedConformanceCase:
    case_id: str
    operation_id: str
    operation_specification_sha256: str
    facet: ReviewedConformanceFacet
    case_contract_sha256: str
    case_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", _identifier(self.case_id, "case/case_id"))
        object.__setattr__(
            self,
            "operation_id",
            _identifier(self.operation_id, "case/operation_id"),
        )
        object.__setattr__(
            self,
            "operation_specification_sha256",
            _digest(
                self.operation_specification_sha256,
                "case/operation_specification_sha256",
            ),
        )
        if type(self.facet) is not ReviewedConformanceFacet:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "case/facet")
        object.__setattr__(
            self,
            "case_contract_sha256",
            _digest(self.case_contract_sha256, "case/case_contract_sha256"),
        )
        object.__setattr__(
            self,
            "case_sha256",
            hashlib.sha256(
                _CASE_DIGEST_DOMAIN + _canonical(self._mapping(), maximum=4 * 1024, path="case")
            ).hexdigest(),
        )

    def _mapping(self) -> dict[str, object]:
        return {
            "case_contract_sha256": self.case_contract_sha256,
            "case_id": self.case_id,
            "facet": self.facet.value,
            "operation_id": self.operation_id,
            "operation_specification_sha256": self.operation_specification_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedConformanceCaseManifest:
    schema_version: int
    family_manifest_sha256: str
    manifest_kind: ReviewedCaseManifestKind
    cases: tuple[ReviewedConformanceCase, ...]
    _admission_token: object | None = field(default=None, repr=False, compare=False)
    _admission_sha256: str | None = field(default=None, repr=False, compare=False)
    case_manifest_sha256: str = field(init=False)
    canonical_bytes: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != REVIEWED_VERIFICATION_SCHEMA_VERSION
        ):
            _fail(CapabilityCatalogErrorCode.UNSUPPORTED_VERSION, "case_manifest/schema_version")
        object.__setattr__(
            self,
            "family_manifest_sha256",
            _digest(self.family_manifest_sha256, "case_manifest/family_manifest_sha256"),
        )
        if type(self.manifest_kind) is not ReviewedCaseManifestKind:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "case_manifest/manifest_kind")
        cases = self.cases
        if type(cases) is not tuple or not cases:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "case_manifest/cases")
        if len(cases) > MAX_REVIEWED_CONFORMANCE_CASES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "case_manifest/cases")
        if any(type(item) is not ReviewedConformanceCase for item in cases):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "case_manifest/cases")
        cases = tuple(
            sorted(cases, key=lambda item: (item.operation_id, item.facet.value, item.case_id))
        )
        if (
            len({item.case_id for item in cases}) != len(cases)
            or len({item.case_sha256 for item in cases}) != len(cases)
            or len({(item.operation_id, item.facet) for item in cases}) != len(cases)
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "case_manifest/cases")
        object.__setattr__(self, "cases", cases)
        body = self._mapping()
        payload = _canonical(
            body,
            maximum=MAX_REVIEWED_CASE_MANIFEST_BYTES,
            path="case_manifest",
        )
        if self.manifest_kind is ReviewedCaseManifestKind.REVIEWED_HOST:
            expected_admission_sha256 = hashlib.sha256(
                _CASE_ADMISSION_DIGEST_DOMAIN + payload
            ).hexdigest()
            if (
                self._admission_token is not _CASE_ADMISSION_TOKEN
                or type(self._admission_sha256) is not str
                or _DIGEST.fullmatch(self._admission_sha256) is None
                or not hmac.compare_digest(
                    self._admission_sha256,
                    expected_admission_sha256,
                )
            ):
                _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "case_manifest/admission")
        elif self._admission_token is not None or self._admission_sha256 is not None:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "case_manifest/admission")
        digest = hashlib.sha256(_CASE_MANIFEST_DIGEST_DOMAIN + payload).hexdigest()
        object.__setattr__(self, "case_manifest_sha256", digest)
        object.__setattr__(
            self,
            "canonical_bytes",
            _canonical(
                {**body, "case_manifest_sha256": digest},
                maximum=MAX_REVIEWED_CASE_MANIFEST_BYTES,
                path="case_manifest",
            ),
        )

    def _mapping(self) -> dict[str, object]:
        return {
            "cases": [item._mapping() | {"case_sha256": item.case_sha256} for item in self.cases],
            "family_manifest_sha256": self.family_manifest_sha256,
            "manifest_kind": self.manifest_kind.value,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedOperationVerificationBinding:
    operation_id: str
    operation_specification_sha256: str
    native_type_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _identifier(self.operation_id, "operation/operation_id"),
        )
        object.__setattr__(
            self,
            "operation_specification_sha256",
            _digest(
                self.operation_specification_sha256,
                "operation/operation_specification_sha256",
            ),
        )
        object.__setattr__(
            self,
            "native_type_id",
            _identifier(self.native_type_id, "operation/native_type_id"),
        )

    def _mapping(self) -> dict[str, str]:
        return {
            "native_type_id": self.native_type_id,
            "operation_id": self.operation_id,
            "operation_specification_sha256": self.operation_specification_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedVerificationTestContract:
    schema_version: int
    runtime_backend: CapabilityBackend
    family_id: str
    family_version: str
    family_manifest_sha256: str
    adapter_id: str
    adapter_version: str
    adapter_contract_sha256: str
    rule_id: str
    rule_contract_sha256: str
    operations: tuple[ReviewedOperationVerificationBinding, ...]
    case_manifest_sha256: str
    evidence_kind: ReviewedConformanceEvidenceKind
    verifier_id: str
    verifier_version: str
    test_contract_sha256: str = field(init=False)
    canonical_bytes: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != REVIEWED_VERIFICATION_SCHEMA_VERSION
        ):
            _fail(CapabilityCatalogErrorCode.UNSUPPORTED_VERSION, "contract/schema_version")
        if type(self.runtime_backend) is not CapabilityBackend:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "contract/runtime_backend")
        for name in ("family_id", "adapter_id", "rule_id", "verifier_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"contract/{name}"))
        for name in ("family_version", "adapter_version", "verifier_version"):
            object.__setattr__(self, name, _version(getattr(self, name), f"contract/{name}"))
        for name in (
            "family_manifest_sha256",
            "adapter_contract_sha256",
            "rule_contract_sha256",
            "case_manifest_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"contract/{name}"))
        operations = self.operations
        if (
            type(operations) is not tuple
            or not operations
            or len(operations) > MAX_REVIEWED_OPERATIONS
            or any(type(item) is not ReviewedOperationVerificationBinding for item in operations)
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "contract/operations")
        operations = tuple(sorted(operations, key=lambda item: item.operation_id))
        if len({item.operation_id for item in operations}) != len(operations) or len(
            {item.operation_specification_sha256 for item in operations}
        ) != len(operations):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "contract/operations")
        if type(self.evidence_kind) is not ReviewedConformanceEvidenceKind:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "contract/evidence_kind")
        object.__setattr__(self, "operations", operations)
        body = self._mapping()
        payload = _canonical(
            body,
            maximum=MAX_REVIEWED_TEST_CONTRACT_BYTES,
            path="contract",
        )
        digest = hashlib.sha256(_TEST_CONTRACT_DIGEST_DOMAIN + payload).hexdigest()
        object.__setattr__(self, "test_contract_sha256", digest)
        object.__setattr__(
            self,
            "canonical_bytes",
            _canonical(
                {**body, "test_contract_sha256": digest},
                maximum=MAX_REVIEWED_TEST_CONTRACT_BYTES,
                path="contract",
            ),
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
            "operations": [item._mapping() for item in self.operations],
            "required_facets": [item.value for item in REQUIRED_REVIEWED_CONFORMANCE_FACETS],
            "rule": {
                "contract_sha256": self.rule_contract_sha256,
                "id": self.rule_id,
            },
            "runtime": _backend_mapping(self.runtime_backend),
            "schema_version": self.schema_version,
            "verifier": {"id": self.verifier_id, "version": self.verifier_version},
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class _ReviewedConformanceCaseResult:
    case_id: str
    case_sha256: str
    operation_id: str
    facet: ReviewedConformanceFacet
    challenge_sha256: str
    observation_sha256: str
    observation_size_bytes: int
    result_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", _identifier(self.case_id, "result/case_id"))
        object.__setattr__(
            self,
            "operation_id",
            _identifier(self.operation_id, "result/operation_id"),
        )
        for name in ("case_sha256", "challenge_sha256", "observation_sha256"):
            object.__setattr__(self, name, _digest(getattr(self, name), f"result/{name}"))
        if type(self.facet) is not ReviewedConformanceFacet:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "result/facet")
        if (
            type(self.observation_size_bytes) is not int
            or not 1 <= self.observation_size_bytes <= MAX_REVIEWED_OBSERVATION_BYTES
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "result/observation_size_bytes")
        object.__setattr__(
            self,
            "result_sha256",
            hashlib.sha256(
                _CASE_RESULT_DIGEST_DOMAIN
                + _canonical(self._mapping(), maximum=4 * 1024, path="result")
            ).hexdigest(),
        )

    def _mapping(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "case_sha256": self.case_sha256,
            "challenge_sha256": self.challenge_sha256,
            "facet": self.facet.value,
            "observation_sha256": self.observation_sha256,
            "observation_size_bytes": self.observation_size_bytes,
            "operation_id": self.operation_id,
            "status": "passed",
        }


@dataclass(frozen=True, slots=True, init=False)
class ReviewedVerificationReceipt:
    contract: ReviewedVerificationTestContract
    case_manifest: ReviewedConformanceCaseManifest
    results: tuple[_ReviewedConformanceCaseResult, ...]
    _builder_token: object = field(repr=False, compare=False)
    test_receipt_sha256: str = field(init=False)
    test_receipt_size_bytes: int = field(init=False)
    canonical_bytes: bytes = field(init=False, repr=False)

    @classmethod
    def _create(
        cls,
        *,
        contract: ReviewedVerificationTestContract,
        case_manifest: ReviewedConformanceCaseManifest,
        results: tuple[_ReviewedConformanceCaseResult, ...],
        builder_token: object,
    ) -> ReviewedVerificationReceipt:
        if builder_token is not _RECEIPT_BUILDER_TOKEN:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "receipt/builder")
        value = object.__new__(cls)
        object.__setattr__(value, "contract", contract)
        object.__setattr__(value, "case_manifest", case_manifest)
        object.__setattr__(value, "results", results)
        object.__setattr__(value, "_builder_token", _RECEIPT_BUILDER_TOKEN)
        value.__post_init__()
        return value

    def __post_init__(self) -> None:
        if self._builder_token is not _RECEIPT_BUILDER_TOKEN:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "receipt/builder")
        if type(self.contract) is not ReviewedVerificationTestContract:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "receipt/contract")
        if type(self.case_manifest) is not ReviewedConformanceCaseManifest:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "receipt/case_manifest")
        results = self.results
        if (
            type(results) is not tuple
            or not results
            or len(results) > MAX_REVIEWED_CONFORMANCE_CASES
            or any(type(item) is not _ReviewedConformanceCaseResult for item in results)
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "receipt/results")
        results = tuple(sorted(results, key=lambda item: item.case_id))
        cases = tuple(sorted(self.case_manifest.cases, key=lambda item: item.case_id))
        if (
            self.contract.case_manifest_sha256 != self.case_manifest.case_manifest_sha256
            or len(results) != len(cases)
            or any(
                result.case_id != case.case_id
                or result.case_sha256 != case.case_sha256
                or result.operation_id != case.operation_id
                or result.facet is not case.facet
                or result.challenge_sha256
                != hashlib.sha256(
                    _CASE_CHALLENGE_DIGEST_DOMAIN
                    + self.contract.test_contract_sha256.encode("ascii")
                    + case.case_sha256.encode("ascii")
                ).hexdigest()
                for result, case in zip(results, cases, strict=True)
            )
        ):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "receipt/results")
        object.__setattr__(self, "results", results)
        body = self._mapping()
        body_bytes = _canonical(
            body,
            maximum=MAX_REVIEWED_TEST_RECEIPT_BYTES,
            path="receipt",
        )
        digest = hashlib.sha256(_TEST_RECEIPT_DIGEST_DOMAIN + body_bytes).hexdigest()
        encoded = _canonical(
            {**body, "test_receipt_sha256": digest},
            maximum=MAX_REVIEWED_TEST_RECEIPT_BYTES,
            path="receipt",
        )
        object.__setattr__(self, "test_receipt_sha256", digest)
        object.__setattr__(self, "test_receipt_size_bytes", len(encoded))
        object.__setattr__(self, "canonical_bytes", encoded)

    def _mapping(self) -> dict[str, object]:
        return {
            "authority": "none",
            "case_manifest": json.loads(self.case_manifest.canonical_bytes),
            "contract": json.loads(self.contract.canonical_bytes),
            "results": [
                item._mapping() | {"result_sha256": item.result_sha256} for item in self.results
            ],
            "schema_version": REVIEWED_VERIFICATION_SCHEMA_VERSION,
        }

    @property
    def test_contract_sha256(self) -> str:
        return self.contract.test_contract_sha256

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


CaseExecutor = Callable[[ReviewedConformanceCase, str], bytes]
RuntimeGuard = Callable[[], None]
RuntimeRevalidator = Callable[[], CapabilityBackend]


@dataclass(frozen=True, slots=True, init=False)
class _ReviewedConformanceHost:
    runtime_backend: CapabilityBackend
    case_manifest_sha256: str
    evidence_kind: ReviewedConformanceEvidenceKind
    verifier_id: str
    verifier_version: str
    execute_case: CaseExecutor = field(repr=False, compare=False)
    guard: RuntimeGuard = field(repr=False, compare=False)
    revalidate: RuntimeRevalidator = field(repr=False, compare=False)
    builder_token: object = field(repr=False, compare=False)

    @classmethod
    def _create(
        cls,
        *,
        runtime_backend: CapabilityBackend,
        case_manifest_sha256: str,
        evidence_kind: ReviewedConformanceEvidenceKind,
        verifier_id: str,
        verifier_version: str,
        execute_case: CaseExecutor,
        guard: RuntimeGuard,
        revalidate: RuntimeRevalidator,
        builder_token: object,
    ) -> _ReviewedConformanceHost:
        if builder_token is not _HOST_BUILDER_TOKEN:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "host/builder")
        value = object.__new__(cls)
        for name, field_value in (
            ("runtime_backend", runtime_backend),
            ("case_manifest_sha256", case_manifest_sha256),
            ("evidence_kind", evidence_kind),
            ("verifier_id", verifier_id),
            ("verifier_version", verifier_version),
            ("execute_case", execute_case),
            ("guard", guard),
            ("revalidate", revalidate),
            ("builder_token", _HOST_BUILDER_TOKEN),
        ):
            object.__setattr__(value, name, field_value)
        value.__post_init__()
        return value

    def __post_init__(self) -> None:
        if self.builder_token is not _HOST_BUILDER_TOKEN:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "host/builder")
        if type(self.runtime_backend) is not CapabilityBackend:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "host/runtime_backend")
        object.__setattr__(
            self,
            "case_manifest_sha256",
            _digest(self.case_manifest_sha256, "host/case_manifest_sha256"),
        )
        if type(self.evidence_kind) is not ReviewedConformanceEvidenceKind:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "host/evidence_kind")
        object.__setattr__(self, "verifier_id", _identifier(self.verifier_id, "host/verifier_id"))
        object.__setattr__(
            self,
            "verifier_version",
            _version(self.verifier_version, "host/verifier_version"),
        )
        if (
            not callable(self.execute_case)
            or not callable(self.guard)
            or not callable(self.revalidate)
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "host/callbacks")


def build_reviewed_family_conformance_case_manifest(
    manifest: FamilyBatchManifest,
) -> ReviewedConformanceCaseManifest:
    """Build the complete deterministic matrix for one real family manifest."""

    if type(manifest) is not FamilyBatchManifest:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "manifest")
    cases: list[ReviewedConformanceCase] = []
    for operation in manifest.operations:
        for facet in REQUIRED_REVIEWED_CONFORMANCE_FACETS:
            descriptor = {
                "facet": facet.value,
                "family_manifest_sha256": manifest.manifest_sha256,
                "operation_id": operation.operation_id,
                "operation_specification_sha256": operation.specification_sha256,
                "schema_version": REVIEWED_VERIFICATION_SCHEMA_VERSION,
            }
            descriptor_bytes = _canonical(
                descriptor,
                maximum=4 * 1024,
                path="case_contract",
            )
            contract_sha256 = hashlib.sha256(
                _CASE_CONTRACT_DIGEST_DOMAIN + descriptor_bytes
            ).hexdigest()
            cases.append(
                ReviewedConformanceCase(
                    case_id=f"case.{contract_sha256[:32]}",
                    operation_id=operation.operation_id,
                    operation_specification_sha256=operation.specification_sha256,
                    facet=facet,
                    case_contract_sha256=contract_sha256,
                )
            )
    return ReviewedConformanceCaseManifest(
        schema_version=REVIEWED_VERIFICATION_SCHEMA_VERSION,
        family_manifest_sha256=manifest.manifest_sha256,
        manifest_kind=ReviewedCaseManifestKind.SYNTHETIC,
        cases=tuple(cases),
    )


def _admit_reviewed_host_conformance_case_manifest(
    *,
    manifest: FamilyBatchManifest,
    cases: tuple[ReviewedConformanceCase, ...],
) -> ReviewedConformanceCaseManifest:
    """Bind host-reviewed real case contracts to one exact family manifest."""

    if type(manifest) is not FamilyBatchManifest:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "manifest")
    validated = ReviewedConformanceCaseManifest(
        schema_version=REVIEWED_VERIFICATION_SCHEMA_VERSION,
        family_manifest_sha256=manifest.manifest_sha256,
        manifest_kind=ReviewedCaseManifestKind.SYNTHETIC,
        cases=cases,
    )
    reviewed_body = {
        **validated._mapping(),
        "manifest_kind": ReviewedCaseManifestKind.REVIEWED_HOST.value,
    }
    admission_sha256 = hashlib.sha256(
        _CASE_ADMISSION_DIGEST_DOMAIN
        + _canonical(
            reviewed_body,
            maximum=MAX_REVIEWED_CASE_MANIFEST_BYTES,
            path="case_manifest/admission",
        )
    ).hexdigest()
    return ReviewedConformanceCaseManifest(
        schema_version=REVIEWED_VERIFICATION_SCHEMA_VERSION,
        family_manifest_sha256=manifest.manifest_sha256,
        manifest_kind=ReviewedCaseManifestKind.REVIEWED_HOST,
        cases=validated.cases,
        _admission_token=_CASE_ADMISSION_TOKEN,
        _admission_sha256=admission_sha256,
    )


def build_deterministic_synthetic_conformance_host(
    *,
    runtime_backend: CapabilityBackend,
    case_manifest: ReviewedConformanceCaseManifest,
) -> _ReviewedConformanceHost:
    """Return a deterministic contract-test host that is never promotion eligible."""

    if type(runtime_backend) is not CapabilityBackend:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "runtime_backend")
    if type(case_manifest) is not ReviewedConformanceCaseManifest:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "case_manifest")
    if case_manifest.manifest_kind is not ReviewedCaseManifestKind.SYNTHETIC:
        _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "case_manifest/manifest_kind")

    def execute(case: ReviewedConformanceCase, challenge_sha256: str) -> bytes:
        return _canonical(
            {
                "case_sha256": case.case_sha256,
                "challenge_sha256": challenge_sha256,
                "evidence_kind": ReviewedConformanceEvidenceKind.SYNTHETIC.value,
            },
            maximum=MAX_REVIEWED_OBSERVATION_BYTES,
            path="synthetic_observation",
        )

    return _ReviewedConformanceHost._create(
        runtime_backend=runtime_backend,
        case_manifest_sha256=case_manifest.case_manifest_sha256,
        evidence_kind=ReviewedConformanceEvidenceKind.SYNTHETIC,
        verifier_id="vcad.synthetic.reviewed-conformance",
        verifier_version="1.0.0",
        execute_case=execute,
        guard=lambda: None,
        revalidate=lambda: runtime_backend,
        builder_token=_HOST_BUILDER_TOKEN,
    )


def _require_managed_headless_empty(freecad: object) -> None:
    try:
        gui_up = freecad.GuiUp
        documents = freecad.listDocuments()
    except BaseException:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "host/runtime/inspection")
    if type(gui_up) is not int or gui_up != 0:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "host/runtime/gui")
    if type(documents) is not dict:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "host/runtime/documents/type")
    if documents:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "host/runtime/documents/open")
    if "FreeCADGui" in sys.modules:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "host/runtime/gui_module")


def build_managed_freecad_conformance_host(
    *,
    freecad: object,
    case_manifest: ReviewedConformanceCaseManifest,
    execute_case: CaseExecutor,
    verifier_id: str = "vcad.managed.freecad.reviewed-conformance",
    verifier_version: str = "1.0.0",
) -> _ReviewedConformanceHost:
    """Authenticate one host-owned same-process managed FreeCAD executor.

    ``execute_case`` is the trust boundary and must be a reviewed host callback,
    never a product/user callback.  The host factory proves the exact runtime;
    the receipt builder calls the callback exactly once per canonical case and
    revalidates the runtime after the complete matrix.
    """

    if (
        freecad is None
        or type(case_manifest) is not ReviewedConformanceCaseManifest
        or not callable(execute_case)
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "host")
    if case_manifest.manifest_kind is not ReviewedCaseManifestKind.REVIEWED_HOST:
        _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "host/case_manifest")
    _require_managed_headless_empty(freecad)

    def collect_backend() -> CapabilityBackend:
        _require_managed_headless_empty(freecad)
        bundle = collect_managed_freecad_discovery_v2(
            freecad=freecad,
            probe_modules=FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
            module_importer=importlib.import_module,
        )
        _require_managed_headless_empty(freecad)
        return bundle.snapshot.backend

    backend = collect_backend()
    return _ReviewedConformanceHost._create(
        runtime_backend=backend,
        case_manifest_sha256=case_manifest.case_manifest_sha256,
        evidence_kind=ReviewedConformanceEvidenceKind.MANAGED_FREECAD,
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        execute_case=execute_case,
        guard=lambda: _require_managed_headless_empty(freecad),
        revalidate=collect_backend,
        builder_token=_HOST_BUILDER_TOKEN,
    )


def _guard(host: _ReviewedConformanceHost, path: str) -> None:
    try:
        host.guard()
    except CapabilityCatalogError as exc:
        exc.add_note(f"reviewed host guard failed at {path}: {exc.path}")
        raise
    except BaseException:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)


def _revalidate(host: _ReviewedConformanceHost, path: str) -> None:
    try:
        backend = host.revalidate()
    except CapabilityCatalogError:
        raise
    except BaseException:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)
    if type(backend) is not CapabilityBackend or backend != host.runtime_backend:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)


def _validate_matrix(
    *, manifest: FamilyBatchManifest, case_manifest: ReviewedConformanceCaseManifest
) -> None:
    if case_manifest.family_manifest_sha256 != manifest.manifest_sha256:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "case_manifest/family")
    operations = {item.operation_id: item for item in manifest.operations}
    expected = {
        (operation_id, facet)
        for operation_id in operations
        for facet in REQUIRED_REVIEWED_CONFORMANCE_FACETS
    }
    actual = {(item.operation_id, item.facet) for item in case_manifest.cases}
    if actual != expected or len(case_manifest.cases) != len(expected):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "case_manifest/matrix")
    for index, case in enumerate(case_manifest.cases):
        operation = operations.get(case.operation_id)
        if (
            operation is None
            or case.operation_specification_sha256 != operation.specification_sha256
        ):
            _fail(
                CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                f"case_manifest/cases/{index}",
            )


def build_reviewed_verification_receipt(
    *,
    manifest: FamilyBatchManifest,
    case_manifest: ReviewedConformanceCaseManifest,
    host: object,
) -> ReviewedVerificationReceipt:
    """Execute and revalidate a complete matrix, then build one exact receipt."""

    if type(manifest) is not FamilyBatchManifest:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "manifest")
    if type(case_manifest) is not ReviewedConformanceCaseManifest:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "case_manifest")
    if type(host) is not _ReviewedConformanceHost:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "host")
    if host.case_manifest_sha256 != case_manifest.case_manifest_sha256:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "host/case_manifest")
    expected_manifest_kind = (
        ReviewedCaseManifestKind.SYNTHETIC
        if host.evidence_kind is ReviewedConformanceEvidenceKind.SYNTHETIC
        else ReviewedCaseManifestKind.REVIEWED_HOST
    )
    if case_manifest.manifest_kind is not expected_manifest_kind:
        _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "host/evidence_kind")
    backend_version = ".".join(str(item) for item in host.runtime_backend.backend_version)
    if (
        manifest.backend_engine != "FreeCAD"
        or manifest.backend_version != backend_version
        or host.runtime_backend.backend_id != "freecad"
        or host.runtime_backend.discovery_profile is not CapabilityExecutionProfile.HEADLESS
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "runtime_backend")
    _validate_matrix(manifest=manifest, case_manifest=case_manifest)
    operation_bindings = tuple(
        ReviewedOperationVerificationBinding(
            operation_id=item.operation_id,
            operation_specification_sha256=item.specification_sha256,
            native_type_id=item.native_type_id,
        )
        for item in manifest.operations
    )
    contract = ReviewedVerificationTestContract(
        schema_version=REVIEWED_VERIFICATION_SCHEMA_VERSION,
        runtime_backend=host.runtime_backend,
        family_id=manifest.family_id,
        family_version=manifest.family_version,
        family_manifest_sha256=manifest.manifest_sha256,
        adapter_id=manifest.adapter.adapter_id,
        adapter_version=manifest.adapter.adapter_version,
        adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
        rule_id=manifest.rule_id,
        rule_contract_sha256=manifest.rule_contract_sha256,
        operations=operation_bindings,
        case_manifest_sha256=case_manifest.case_manifest_sha256,
        evidence_kind=host.evidence_kind,
        verifier_id=host.verifier_id,
        verifier_version=host.verifier_version,
    )
    _guard(host, "host/guard/start")
    _revalidate(host, "host/revalidate/start")
    results: list[_ReviewedConformanceCaseResult] = []
    for index, case in enumerate(case_manifest.cases):
        _guard(host, f"host/guard/{index}")
        challenge_sha256 = hashlib.sha256(
            _CASE_CHALLENGE_DIGEST_DOMAIN
            + contract.test_contract_sha256.encode("ascii")
            + case.case_sha256.encode("ascii")
        ).hexdigest()
        observation: object = None
        execution_failed = False
        try:
            observation = host.execute_case(case, challenge_sha256)
        except BaseException:
            execution_failed = True
        _guard(host, f"host/guard/{index}")
        if execution_failed:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, f"host/results/{index}")
        if type(observation) is not bytes or not observation:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, f"host/results/{index}")
        if len(observation) > MAX_REVIEWED_OBSERVATION_BYTES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, f"host/results/{index}")
        results.append(
            _ReviewedConformanceCaseResult(
                case_id=case.case_id,
                case_sha256=case.case_sha256,
                operation_id=case.operation_id,
                facet=case.facet,
                challenge_sha256=challenge_sha256,
                observation_sha256=hashlib.sha256(
                    _OBSERVATION_DIGEST_DOMAIN + observation
                ).hexdigest(),
                observation_size_bytes=len(observation),
            )
        )
    _revalidate(host, "host/revalidate/final")
    _guard(host, "host/guard/final")
    return ReviewedVerificationReceipt._create(
        contract=contract,
        case_manifest=case_manifest,
        results=tuple(results),
        builder_token=_RECEIPT_BUILDER_TOKEN,
    )


def build_promotion_verification_binding(
    receipt: ReviewedVerificationReceipt,
) -> FreeCadPromotionVerificationBinding:
    """Convert only same-process managed evidence into the existing wire type."""

    if type(receipt) is not ReviewedVerificationReceipt:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "receipt")
    if receipt.contract.evidence_kind is not ReviewedConformanceEvidenceKind.MANAGED_FREECAD:
        _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "receipt/evidence_kind")
    if receipt.case_manifest.manifest_kind is not ReviewedCaseManifestKind.REVIEWED_HOST:
        _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "receipt/case_manifest")
    return FreeCadPromotionVerificationBinding(
        runtime_build_sha256=receipt.contract.runtime_backend.build_fingerprint_sha256,
        adapter_contract_sha256=receipt.contract.adapter_contract_sha256,
        test_contract_sha256=receipt.test_contract_sha256,
        test_receipt_sha256=receipt.test_receipt_sha256,
        test_receipt_size_bytes=receipt.test_receipt_size_bytes,
        verifier_id=receipt.contract.verifier_id,
        verifier_version=receipt.contract.verifier_version,
    )


def encode_reviewed_conformance_case_manifest(
    value: ReviewedConformanceCaseManifest,
) -> bytes:
    if type(value) is not ReviewedConformanceCaseManifest:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "case_manifest")
    return value.canonical_bytes


def encode_reviewed_verification_test_contract(
    value: ReviewedVerificationTestContract,
) -> bytes:
    if type(value) is not ReviewedVerificationTestContract:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "contract")
    return value.canonical_bytes


def encode_reviewed_verification_receipt(value: ReviewedVerificationReceipt) -> bytes:
    if type(value) is not ReviewedVerificationReceipt:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "receipt")
    return value.canonical_bytes


__all__ = ()
