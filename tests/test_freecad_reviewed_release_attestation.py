from __future__ import annotations

import copy
import dataclasses
import hashlib
import json

import pytest

import vibecad.execution.freecad_builtin_intent_capabilities as builtin_capabilities
import vibecad.execution.freecad_reviewed_verification_runtime as verification_runtime
from vibecad.execution import freecad_reviewed_verification as verification
from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
)
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
    current_freecad_intent_promotion_specs,
)
from vibecad.execution.freecad_legacy_reviewed_verification import (
    LEGACY_REVIEWED_FAMILY_MANIFESTS,
)
from vibecad.execution.freecad_reviewed_family_capabilities import (
    CURRENT_FREECAD_REVIEWED_FAMILY_MANIFESTS,
)
from vibecad.execution.freecad_reviewed_release_attestation import (
    MAX_FREECAD_REVIEWED_RELEASE_ATTESTATION_BYTES,
    build_freecad_reviewed_release_attestation,
    decode_freecad_reviewed_release_attestation,
    encode_freecad_reviewed_release_attestation,
    validate_freecad_reviewed_release_attestation,
)
from vibecad.execution.freecad_reviewed_verification import (
    ReviewedCaseManifestKind,
    ReviewedConformanceEvidenceKind,
    build_reviewed_family_conformance_case_manifest,
    build_reviewed_verification_receipt,
)
from vibecad.execution.freecad_reviewed_verification_runtime import (
    MAX_FREECAD_REVIEWED_VERIFICATION_RECEIPTS,
    MAX_FREECAD_REVIEWED_VERIFICATION_SET_BYTES,
    build_managed_reviewed_verification_set,
    decode_freecad_managed_reviewed_verification_set,
    encode_freecad_managed_reviewed_verification_set,
    validate_managed_reviewed_verification_set,
)

_BUILD_SHA256 = hashlib.sha256(b"managed-freecad-release-build").hexdigest()
_DISCOVERY_SNAPSHOT_SHA256 = hashlib.sha256(b"exact-discovery-snapshot").hexdigest()
_DISCOVERY_MANIFEST_SHA256 = hashlib.sha256(b"exact-discovery-manifest").hexdigest()
_RELEASE_VERSION = "0.10.0"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _backend() -> CapabilityBackend:
    return CapabilityBackend(
        backend_id="freecad",
        backend_version=(1, 1, 0),
        build_fingerprint_sha256=_BUILD_SHA256,
        platform_id="macos.arm64",
        discovery_profile=CapabilityExecutionProfile.HEADLESS,
    )


def _managed_receipt(manifest):
    synthetic = build_reviewed_family_conformance_case_manifest(manifest)
    case_manifest = verification._admit_reviewed_host_conformance_case_manifest(  # noqa: SLF001
        manifest=manifest,
        cases=synthetic.cases,
    )
    assert case_manifest.manifest_kind is ReviewedCaseManifestKind.REVIEWED_HOST

    def execute(case, challenge_sha256):
        return f"{case.case_sha256}:{challenge_sha256}:release-observation".encode("ascii")

    host = verification._ReviewedConformanceHost._create(  # noqa: SLF001
        runtime_backend=_backend(),
        case_manifest_sha256=case_manifest.case_manifest_sha256,
        evidence_kind=ReviewedConformanceEvidenceKind.MANAGED_FREECAD,
        verifier_id="vcad.test.release-attestation",
        verifier_version="1.0.0",
        execute_case=execute,
        guard=lambda: None,
        revalidate=_backend,
        builder_token=verification._HOST_BUILDER_TOKEN,  # noqa: SLF001
    )
    return build_reviewed_verification_receipt(
        manifest=manifest,
        case_manifest=case_manifest,
        host=host,
    )


@pytest.fixture(scope="module")
def current_closure():
    manifests = tuple(
        sorted(
            (*LEGACY_REVIEWED_FAMILY_MANIFESTS, *CURRENT_FREECAD_REVIEWED_FAMILY_MANIFESTS),
            key=lambda item: item.family_id,
        )
    )
    receipts = tuple(_managed_receipt(item) for item in manifests)
    formal = current_freecad_intent_capability_specs()
    promotion = current_freecad_intent_promotion_specs()
    verification_set = build_managed_reviewed_verification_set(
        runtime_backend=_backend(),
        receipts=receipts,
        manifests=manifests,
        formal_specs=formal,
        promotion_specs=promotion,
    )
    attestation = build_freecad_reviewed_release_attestation(
        release_version=_RELEASE_VERSION,
        runtime_backend=_backend(),
        discovery_snapshot_sha256=_DISCOVERY_SNAPSHOT_SHA256,
        discovery_manifest_sha256=_DISCOVERY_MANIFEST_SHA256,
        verification_set=verification_set,
    )
    return manifests, receipts, formal, promotion, verification_set, attestation


def _code(call) -> CapabilityCatalogErrorCode:
    with pytest.raises(CapabilityCatalogError) as caught:
        call()
    return caught.value.code


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _decode_outer_for_test(raw: bytes):
    """Tests supply an explicit fixture pin; production pins must be source constants."""

    return decode_freecad_reviewed_release_attestation(
        raw,
        expected_source_attestation_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _readdress_set(mapping: dict[str, object]) -> bytes:
    body = {key: value for key, value in mapping.items() if key != "verification_set_sha256"}
    mapping["verification_set_sha256"] = hashlib.sha256(
        verification_runtime._VERIFICATION_SET_DIGEST_DOMAIN  # noqa: SLF001
        + _canonical(body)
    ).hexdigest()
    return _canonical(mapping)


def test_v2_full_current_closure_retains_all_receipts_and_sketch_without_native_owner(
    current_closure,
) -> None:
    manifests, receipts, formal, promotion, verification_set, _attestation = current_closure

    assert len(manifests) == len(receipts) == len(verification_set.receipts) == 19
    assert len(formal) == len(verification_set.formal_operations) == 124
    assert len(promotion) == 104
    assert len(verification_set.native_types) == 102
    assert (
        validate_managed_reviewed_verification_set(
            verification_set,
            runtime_backend=_backend(),
            require_complete=True,
        )
        is verification_set
    )

    sketch = next(item for item in verification_set.receipts if "reviewed_sketch" in item.family_id)
    assert any(
        item.test_receipt_sha256 == sketch.test_receipt_sha256
        for item in verification_set.formal_operations
    )
    assert all(
        item.verification.test_receipt_sha256 != sketch.test_receipt_sha256
        for item in verification_set.native_types
    )
    assert sketch.evidence_kind is ReviewedConformanceEvidenceKind.MANAGED_FREECAD
    assert all(
        len(value) == 64
        for value in (
            sketch.test_receipt_sha256,
            sketch.test_contract_sha256,
            sketch.case_manifest_sha256,
            sketch.family_manifest_sha256,
            sketch.adapter_contract_sha256,
            sketch.rule_contract_sha256,
        )
    )


def test_v2_set_and_outer_attestation_round_trip_canonically_and_ignore_input_order(
    current_closure,
) -> None:
    manifests, receipts, formal, promotion, verification_set, attestation = current_closure
    set_raw = encode_freecad_managed_reviewed_verification_set(verification_set)
    attestation_raw = encode_freecad_reviewed_release_attestation(attestation)

    assert decode_freecad_managed_reviewed_verification_set(set_raw) == verification_set
    assert _decode_outer_for_test(attestation_raw) == attestation
    assert _canonical(json.loads(set_raw)) == set_raw
    assert _canonical(json.loads(attestation_raw)) == attestation_raw

    reversed_set = build_managed_reviewed_verification_set(
        runtime_backend=_backend(),
        receipts=tuple(reversed(receipts)),
        manifests=tuple(reversed(manifests)),
        formal_specs=tuple(reversed(formal)),
        promotion_specs=tuple(reversed(promotion)),
    )
    assert reversed_set == verification_set
    assert encode_freecad_managed_reviewed_verification_set(reversed_set) == set_raw


def test_set_decoder_rejects_duplicate_unknown_noncanonical_budget_and_digest_tamper(
    current_closure,
) -> None:
    verification_set = current_closure[4]
    raw = encode_freecad_managed_reviewed_verification_set(verification_set)
    mapping = json.loads(raw)
    unknown = _canonical(mapping | {"unknown": None})
    duplicate = raw[:-1] + b',"schema_version":2}'
    noncanonical = b" " + raw
    tampered_mapping = copy.deepcopy(mapping)
    tampered_mapping["receipts"][0]["rule"]["contract_sha256"] = _sha("tampered-rule")
    tampered = _canonical(tampered_mapping)

    for candidate in (unknown, duplicate, noncanonical):
        assert (
            _code(
                lambda candidate=candidate: decode_freecad_managed_reviewed_verification_set(
                    candidate
                )
            )
            is CapabilityCatalogErrorCode.INVALID_INPUT
        )
    assert (
        _code(lambda: decode_freecad_managed_reviewed_verification_set(tampered))
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )
    assert (
        _code(
            lambda: decode_freecad_managed_reviewed_verification_set(
                b"x" * (MAX_FREECAD_REVIEWED_VERIFICATION_SET_BYTES + 1)
            )
        )
        is CapabilityCatalogErrorCode.BUDGET_EXCEEDED
    )


def test_self_consistent_receipt_orphan_and_n_plus_one_fail_closed(current_closure) -> None:
    mapping = json.loads(encode_freecad_managed_reviewed_verification_set(current_closure[4]))
    rebound = copy.deepcopy(mapping)
    target_receipt = rebound["native_types"][0]["verification"]["test_receipt_sha256"]
    rebound_test_contract = _sha("rebound-test-contract")
    for receipt in rebound["receipts"]:
        if receipt["test_receipt_sha256"] == target_receipt:
            receipt["test_contract_sha256"] = rebound_test_contract
    for native in rebound["native_types"]:
        if native["verification"]["test_receipt_sha256"] == target_receipt:
            native["verification"]["test_contract_sha256"] = rebound_test_contract
    assert (
        _code(lambda: decode_freecad_managed_reviewed_verification_set(_readdress_set(rebound)))
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )

    orphan = copy.deepcopy(mapping["receipts"][0])
    orphan["test_receipt_sha256"] = _sha("orphan-receipt")
    orphan["family"]["id"] = "vcad.test.orphan-family"
    orphan["family"]["manifest_sha256"] = _sha("orphan-family-manifest")
    mapping["receipts"].append(orphan)
    mapping["receipts"].sort(key=lambda item: item["test_receipt_sha256"])
    assert (
        _code(lambda: decode_freecad_managed_reviewed_verification_set(_readdress_set(mapping)))
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )

    over = json.loads(encode_freecad_managed_reviewed_verification_set(current_closure[4]))
    template = over["receipts"][0]
    over["receipts"] = []
    for index in range(MAX_FREECAD_REVIEWED_VERIFICATION_RECEIPTS + 1):
        item = copy.deepcopy(template)
        item["test_receipt_sha256"] = hashlib.sha256(f"receipt-{index}".encode()).hexdigest()
        item["family"]["id"] = f"vcad.test.family-{index}"
        item["family"]["manifest_sha256"] = hashlib.sha256(f"manifest-{index}".encode()).hexdigest()
        over["receipts"].append(item)
    over["receipts"].sort(key=lambda item: item["test_receipt_sha256"])
    assert (
        _code(lambda: decode_freecad_managed_reviewed_verification_set(_readdress_set(over)))
        is CapabilityCatalogErrorCode.BUDGET_EXCEEDED
    )


def test_current_catalog_drift_rejects_an_encoded_set(
    current_closure,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = encode_freecad_managed_reviewed_verification_set(current_closure[4])
    formal = current_freecad_intent_capability_specs()
    drifted = (
        dataclasses.replace(formal[0], rule_contract_sha256=_sha("current-catalog-drift")),
        *formal[1:],
    )
    monkeypatch.setattr(
        builtin_capabilities,
        "current_freecad_intent_capability_specs",
        lambda: drifted,
    )
    assert (
        _code(lambda: decode_freecad_managed_reviewed_verification_set(raw))
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )


def test_direct_build_is_inert_and_source_pinned_outer_binds_release_build_and_discovery(
    current_closure,
) -> None:
    built = current_closure[5]
    raw = encode_freecad_reviewed_release_attestation(built)
    source_sha256 = hashlib.sha256(raw).hexdigest()
    attestation = decode_freecad_reviewed_release_attestation(
        raw,
        expected_source_attestation_sha256=source_sha256,
    )
    assert (
        validate_freecad_reviewed_release_attestation(
            attestation,
            expected_release_version=_RELEASE_VERSION,
            runtime_backend=_backend(),
            discovery_snapshot_sha256=_DISCOVERY_SNAPSHOT_SHA256,
            discovery_manifest_sha256=_DISCOVERY_MANIFEST_SHA256,
            expected_source_attestation_sha256=source_sha256,
        )
        is attestation
    )

    assert (
        _code(
            lambda: validate_freecad_reviewed_release_attestation(
                built,
                expected_release_version=_RELEASE_VERSION,
                runtime_backend=_backend(),
                discovery_snapshot_sha256=_DISCOVERY_SNAPSHOT_SHA256,
                discovery_manifest_sha256=_DISCOVERY_MANIFEST_SHA256,
                expected_source_attestation_sha256=source_sha256,
            )
        )
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )

    drifted_backend = dataclasses.replace(
        _backend(),
        build_fingerprint_sha256=_sha("other-build"),
    )
    cases = (
        {"expected_release_version": "0.9.9"},
        {"runtime_backend": drifted_backend},
        {"discovery_snapshot_sha256": _sha("other-snapshot")},
        {"discovery_manifest_sha256": _sha("other-manifest")},
    )
    base = {
        "expected_release_version": _RELEASE_VERSION,
        "runtime_backend": _backend(),
        "discovery_snapshot_sha256": _DISCOVERY_SNAPSHOT_SHA256,
        "discovery_manifest_sha256": _DISCOVERY_MANIFEST_SHA256,
        "expected_source_attestation_sha256": source_sha256,
    }
    for changed in cases:
        arguments = base | changed
        assert (
            _code(
                lambda arguments=arguments: validate_freecad_reviewed_release_attestation(
                    attestation,
                    **arguments,
                )
            )
            is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
        )


def test_outer_decoder_rejects_structure_digest_catalog_and_budget_tamper(
    current_closure,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = encode_freecad_reviewed_release_attestation(current_closure[5])
    assert (
        _code(
            lambda: decode_freecad_reviewed_release_attestation(
                raw,
                expected_source_attestation_sha256=_sha("untrusted-source-pin"),
            )
        )
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )
    mapping = json.loads(raw)
    unknown = _canonical(mapping | {"unknown": None})
    duplicate = raw[:-1] + b',"schema_version":1}'
    noncanonical = b" " + raw
    tampered_mapping = copy.deepcopy(mapping)
    tampered_mapping["discovery_manifest_sha256"] = _sha("tampered-discovery")
    tampered = _canonical(tampered_mapping)
    for candidate in (unknown, duplicate, noncanonical):
        assert (
            _code(lambda candidate=candidate: _decode_outer_for_test(candidate))
            is CapabilityCatalogErrorCode.INVALID_INPUT
        )
    assert (
        _code(lambda: _decode_outer_for_test(tampered))
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )
    assert (
        _code(
            lambda: _decode_outer_for_test(
                b"x" * (MAX_FREECAD_REVIEWED_RELEASE_ATTESTATION_BYTES + 1)
            )
        )
        is CapabilityCatalogErrorCode.BUDGET_EXCEEDED
    )

    formal = current_freecad_intent_capability_specs()
    monkeypatch.setattr(
        builtin_capabilities,
        "current_freecad_intent_capability_specs",
        lambda: (
            dataclasses.replace(formal[0], adapter_contract_sha256=_sha("adapter-drift")),
            *formal[1:],
        ),
    )
    assert (
        _code(lambda: _decode_outer_for_test(raw)) is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )
