from __future__ import annotations

import dataclasses
import hashlib

import pytest

from vibecad.execution import freecad_reviewed_verification as verification
from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
)
from vibecad.execution.freecad_reviewed_family_capabilities import (
    build_reviewed_family_capability_specs,
)
from vibecad.execution.freecad_reviewed_verification import (
    ReviewedCaseManifestKind,
    ReviewedConformanceEvidenceKind,
    build_reviewed_family_conformance_case_manifest,
    build_reviewed_verification_receipt,
)
from vibecad.execution.freecad_reviewed_verification_runtime import (
    build_managed_reviewed_verification_set,
)
from vibecad.intent_bridge.freecad_part_core_adapter import PART_CORE_MANIFEST
from vibecad.intent_bridge.freecad_part_curve_adapter import PART_CURVE_MANIFEST

_MANAGED_BUILD_SHA256 = hashlib.sha256(b"managed-build").hexdigest()


def _backend(build_sha256: str = _MANAGED_BUILD_SHA256) -> CapabilityBackend:
    return CapabilityBackend(
        backend_id="freecad",
        backend_version=(1, 1, 0),
        build_fingerprint_sha256=build_sha256,
        platform_id="macos.arm64",
        discovery_profile=CapabilityExecutionProfile.HEADLESS,
    )


def _managed_receipt(manifest, *, backend=None):
    synthetic = build_reviewed_family_conformance_case_manifest(manifest)
    case_manifest = verification._admit_reviewed_host_conformance_case_manifest(  # noqa: SLF001
        manifest=manifest,
        cases=synthetic.cases,
    )
    assert case_manifest.manifest_kind is ReviewedCaseManifestKind.REVIEWED_HOST
    runtime_backend = backend or _backend()

    def execute(case, challenge_sha256):
        return f"{case.case_sha256}:{challenge_sha256}:managed-test-observation".encode("ascii")

    host = verification._ReviewedConformanceHost._create(  # noqa: SLF001
        runtime_backend=runtime_backend,
        case_manifest_sha256=case_manifest.case_manifest_sha256,
        evidence_kind=ReviewedConformanceEvidenceKind.MANAGED_FREECAD,
        verifier_id="vcad.test.reviewed-runtime",
        verifier_version="1.0.0",
        execute_case=execute,
        guard=lambda: None,
        revalidate=lambda: runtime_backend,
        builder_token=verification._HOST_BUILDER_TOKEN,  # noqa: SLF001
    )
    return build_reviewed_verification_receipt(
        manifest=manifest,
        case_manifest=case_manifest,
        host=host,
    )


def _inputs():
    manifests = (PART_CORE_MANIFEST, PART_CURVE_MANIFEST)
    specs = build_reviewed_family_capability_specs(manifests)
    receipts = tuple(_managed_receipt(item) for item in manifests)
    return manifests, specs, receipts


def _code(call) -> CapabilityCatalogErrorCode:
    with pytest.raises(CapabilityCatalogError) as caught:
        call()
    return caught.value.code


def test_complete_managed_receipts_close_formal_and_native_coverage_deterministically() -> None:
    manifests, specs, receipts = _inputs()
    forward = build_managed_reviewed_verification_set(
        runtime_backend=_backend(),
        receipts=receipts,
        manifests=manifests,
        formal_specs=specs,
        promotion_specs=specs,
    )
    reverse = build_managed_reviewed_verification_set(
        runtime_backend=_backend(),
        receipts=tuple(reversed(receipts)),
        manifests=tuple(reversed(manifests)),
        formal_specs=tuple(reversed(specs)),
        promotion_specs=tuple(reversed(specs)),
    )

    assert forward == reverse
    assert forward.verification_set_sha256 == reverse.verification_set_sha256
    assert len(forward.formal_operations) == len(specs) == 28
    assert len(forward.native_types) == len(forward.verification_by_native_type) == 28
    assert set(forward.verification_by_native_type) == {item.native_type_id for item in specs}
    assert all(
        item.runtime_build_sha256 == _MANAGED_BUILD_SHA256
        for item in forward.verification_by_native_type.values()
    )


def test_missing_extra_synthetic_and_build_drift_receipts_fail_closed() -> None:
    manifests, specs, receipts = _inputs()
    assert (
        _code(
            lambda: build_managed_reviewed_verification_set(
                runtime_backend=_backend(),
                receipts=receipts[:1],
                manifests=manifests,
                formal_specs=specs,
                promotion_specs=specs,
            )
        )
        is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE
    )
    assert (
        _code(
            lambda: build_managed_reviewed_verification_set(
                runtime_backend=_backend(),
                receipts=(receipts[0], receipts[0]),
                manifests=(manifests[0], manifests[0]),
                formal_specs=specs[:19],
                promotion_specs=specs[:19],
            )
        )
        is CapabilityCatalogErrorCode.INVALID_INPUT
    )

    synthetic_cases = build_reviewed_family_conformance_case_manifest(PART_CORE_MANIFEST)
    synthetic_host = verification.build_deterministic_synthetic_conformance_host(
        runtime_backend=_backend(),
        case_manifest=synthetic_cases,
    )
    synthetic_receipt = build_reviewed_verification_receipt(
        manifest=PART_CORE_MANIFEST,
        case_manifest=synthetic_cases,
        host=synthetic_host,
    )
    assert (
        _code(
            lambda: build_managed_reviewed_verification_set(
                runtime_backend=_backend(),
                receipts=(synthetic_receipt,),
                manifests=(PART_CORE_MANIFEST,),
                formal_specs=specs[:19],
                promotion_specs=specs[:19],
            )
        )
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )

    drifted = _managed_receipt(
        PART_CORE_MANIFEST, backend=_backend(hashlib.sha256(b"drift").hexdigest())
    )
    assert (
        _code(
            lambda: build_managed_reviewed_verification_set(
                runtime_backend=_backend(),
                receipts=(drifted,),
                manifests=(PART_CORE_MANIFEST,),
                formal_specs=specs[:19],
                promotion_specs=specs[:19],
            )
        )
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )


def test_formal_semantic_adapter_and_promotion_substitution_fail_closed() -> None:
    manifests, specs, receipts = _inputs()
    rebound_semantic = dataclasses.replace(
        specs[0],
        semantic_operation="vendor/rebound/operation@" + "f" * 64,
    )
    rebound_adapter = dataclasses.replace(
        specs[0],
        adapter_contract_sha256="e" * 64,
    )
    for rebound in (rebound_semantic, rebound_adapter):
        changed = (rebound, *specs[1:])
        assert (
            _code(
                lambda changed=changed: build_managed_reviewed_verification_set(
                    runtime_backend=_backend(),
                    receipts=receipts,
                    manifests=manifests,
                    formal_specs=changed,
                    promotion_specs=changed,
                )
            )
            is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
        )

    drifted_promotion = dataclasses.replace(
        specs[0],
        rule_contract_sha256="d" * 64,
    )
    assert (
        _code(
            lambda: build_managed_reviewed_verification_set(
                runtime_backend=_backend(),
                receipts=receipts,
                manifests=manifests,
                formal_specs=specs,
                promotion_specs=(drifted_promotion, *specs[1:]),
            )
        )
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )
