"""Focused tests for backend-neutral intent and trusted adapter selection."""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityCatalogSegment,
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityKind,
    CapabilityLifecycleStage,
    CapabilityRiskClass,
    CapabilitySupportStatus,
    CapabilityTermRef,
    CapabilityVerificationRef,
)
from vibecad.execution.capability_index import CapabilityCatalogIndex
from vibecad.execution.capability_intent import (
    CapabilityAdapterBinding,
    CapabilityContentRef,
    CapabilityIntent,
    CapabilityIntentArgument,
    CapabilityIntentProof,
    CapabilityIntentSource,
    CapabilityIntentValueState,
    compile_capability_intent,
    decode_capability_intent,
    encode_capability_intent,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _term(term_ref_id: str, term_id: str | None = None) -> CapabilityTermRef:
    semantic_id = term_id or term_ref_id.replace(".", "/")
    return CapabilityTermRef(
        term_ref_id=term_ref_id,
        namespace="vcad.intent",
        vocabulary_version="1.0",
        term_id=semantic_id,
        term_definition_sha256=_sha(f"term:{semantic_id}"),
    )


def _terms() -> tuple[CapabilityTermRef, ...]:
    return (
        _term("argument.length"),
        _term("operation.create_prismatic_body"),
        _term("proof.geometry"),
        _term("role.visual_feature_graph"),
        _term("unit.mm"),
    )


def _content(label: str, media_type: str = "application/json") -> CapabilityContentRef:
    return CapabilityContentRef(
        sha256=_sha(label),
        size_bytes=128,
        media_type=media_type,
        schema_sha256=_sha(f"schema:{label}"),
    )


def _intent(
    *,
    state: CapabilityIntentValueState = CapabilityIntentValueState.CONFIRMED,
    value: object = 20.0,
    terms: tuple[CapabilityTermRef, ...] | None = None,
) -> CapabilityIntent:
    return CapabilityIntent(
        schema_version=1,
        intent_id="intent.create.body",
        operation_term_ref_id="operation.create_prismatic_body",
        terms=tuple(reversed(_terms())) if terms is None else terms,
        sources=(
            CapabilityIntentSource(
                source_id="source.visual.graph",
                role_term_ref_id="role.visual_feature_graph",
                content=_content("visual-graph", "application/vnd.vibecad.vfg+json"),
            ),
        ),
        arguments=(
            CapabilityIntentArgument(
                argument_id="argument.depth",
                semantic_term_ref_id="argument.length",
                state=state,
                value=value,
                unit_term_ref_id="unit.mm",
                evidence_element_ids=("measurement.depth",),
            ),
        ),
        proofs=(
            CapabilityIntentProof(
                proof_id="proof.depth",
                proof_kind_term_ref_id="proof.geometry",
                subject_argument_ids=("argument.depth",),
                content=_content("depth-proof"),
            ),
        ),
        acceptance=_content("acceptance"),
    )


def _backend(**changes: object) -> CapabilityBackend:
    values = {
        "backend_id": "freecad",
        "backend_version": (1, 1, 0),
        "build_fingerprint_sha256": _sha("managed-build"),
        "platform_id": "macos.x86_64",
        "discovery_profile": CapabilityExecutionProfile.HEADLESS,
    }
    values.update(changes)
    return CapabilityBackend(**values)


def _catalog(*, verified: bool = True) -> CapabilityCatalogIndex:
    module = CapabilityDescriptor(
        capability_id="vibecad.module.parametric",
        kind=CapabilityKind.MODULE,
        native_identifier="vibecad.parametric.compiler",
        declaring_module_id="vibecad.module.parametric",
        status=CapabilitySupportStatus.REPRESENTABLE,
        risk_class=CapabilityRiskClass.READ_ONLY,
        semantic_term_ref_ids=("semantic.module",),
    )
    receipt = (
        CapabilityVerificationRef(
            receipt_sha256=_sha("pad-conformance"),
            receipt_size_bytes=1024,
            verifier_id="vcad.managed.freecad",
            verifier_version="1.0",
        )
        if verified
        else None
    )
    operation = CapabilityDescriptor(
        capability_id="vibecad.compiler.parametric.feature.pad",
        kind=CapabilityKind.OPERATION,
        native_identifier="PartDesign::Pad",
        declaring_module_id="vibecad.module.parametric",
        status=(
            CapabilitySupportStatus.VERIFIED if verified else CapabilitySupportStatus.EXECUTABLE
        ),
        risk_class=CapabilityRiskClass.MUTATING,
        semantic_term_ref_ids=("semantic.operation",),
        execution_profiles=(CapabilityExecutionProfile.HEADLESS,),
        lifecycle_stages=(CapabilityLifecycleStage.EXECUTE,),
        verification=receipt,
    )
    segment = CapabilityCatalogSegment(
        schema_version=1,
        segment_id="test.verified.pad" if verified else "test.executable.pad",
        backend=_backend(),
        discovery_receipt_sha256=_sha("catalog-receipt"),
        discovery_algorithm_id="vcad.test.catalog",
        discovery_algorithm_version="1.0",
        terms=(
            _term("semantic.module"),
            _term("semantic.operation"),
        ),
        descriptors=(module, operation),
    )
    return CapabilityCatalogIndex((segment,))


def _binding(catalog: CapabilityCatalogIndex, **changes: object) -> CapabilityAdapterBinding:
    descriptor = catalog.lookup("vibecad.compiler.parametric.feature.pad")
    values = {
        "binding_id": "binding.intent.pad",
        "backend": _backend(),
        "catalog_sha256": catalog.catalog_sha256,
        "capability_id": descriptor.capability_id,
        "capability_descriptor_sha256": descriptor.descriptor_sha256,
        "operation_term": _term("operation.create_prismatic_body"),
        "execution_profile": CapabilityExecutionProfile.HEADLESS,
        "adapter_id": "vcad.freecad.parametric.pad",
        "adapter_version": "1.0",
        "adapter_receipt_sha256": _sha("adapter-source"),
        "input_contract": _content("input-contract"),
        "output_contract": _content("output-contract"),
        "proof_rule": _content("proof-rule"),
    }
    values.update(changes)
    return CapabilityAdapterBinding(**values)


def test_intent_round_trip_is_canonical_and_backend_neutral() -> None:
    intent = _intent()
    raw = encode_capability_intent(intent)
    decoded = decode_capability_intent(raw)
    assert decoded == intent
    assert decoded.intent_sha256 == intent.intent_sha256
    assert b"freecad" not in raw.lower()
    assert b"partdesign" not in raw.lower()
    assert [item.term_ref_id for item in decoded.terms] == sorted(
        item.term_ref_id for item in decoded.terms
    )


def test_intent_argument_value_is_snapshot_immutable() -> None:
    value = {"dimensions": [10.0, 20.0]}
    argument = CapabilityIntentArgument(
        argument_id="argument.profile",
        semantic_term_ref_id="argument.length",
        state=CapabilityIntentValueState.OBSERVED,
        value=value,
    )
    value["dimensions"].append(30.0)
    assert argument.decoded_value == {"dimensions": [10.0, 20.0]}
    decoded = argument.decoded_value
    decoded["dimensions"].append(40.0)
    assert argument.decoded_value == {"dimensions": [10.0, 20.0]}


def test_unknown_and_conflicted_values_cannot_carry_asserted_payload() -> None:
    for state in (
        CapabilityIntentValueState.UNKNOWN,
        CapabilityIntentValueState.CONFLICTED,
    ):
        with pytest.raises(CapabilityCatalogError) as failure:
            _intent(state=state, value=20.0)
        assert failure.value.code is CapabilityCatalogErrorCode.INVALID_INPUT

    with pytest.raises(CapabilityCatalogError) as absent:
        _intent(state=CapabilityIntentValueState.CONFIRMED, value=None)
    assert absent.value.code is CapabilityCatalogErrorCode.INVALID_INPUT


def test_intent_requires_term_and_proof_subject_closure() -> None:
    missing_unit = tuple(item for item in _terms() if item.term_ref_id != "unit.mm")
    with pytest.raises(CapabilityCatalogError) as term:
        _intent(terms=missing_unit)
    assert term.value.code is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE

    intent = _intent()
    broken_proof = dataclasses.replace(
        intent.proofs[0],
        subject_argument_ids=("argument.missing",),
    )
    with pytest.raises(CapabilityCatalogError) as proof:
        dataclasses.replace(intent, proofs=(broken_proof,))
    assert proof.value.code is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE


def test_compile_selects_exact_verified_capability_without_invoking_adapter() -> None:
    catalog = _catalog()
    intent = _intent()
    binding = _binding(catalog)
    invocation = compile_capability_intent(
        intent=intent,
        catalog=catalog,
        binding=binding,
    )
    assert invocation.intent_sha256 == intent.intent_sha256
    assert invocation.binding_sha256 == binding.binding_sha256
    assert invocation.capability_id == "vibecad.compiler.parametric.feature.pad"
    assert invocation.proof_content_sha256 == (_sha("depth-proof"),)
    assert invocation.invocation_sha256 == invocation.invocation_sha256
    assert not hasattr(invocation, "execute")
    assert not hasattr(invocation, "handler")


def test_compile_refuses_executable_but_unverified_capability() -> None:
    catalog = _catalog(verified=False)
    with pytest.raises(CapabilityCatalogError) as failure:
        compile_capability_intent(
            intent=_intent(),
            catalog=catalog,
            binding=_binding(catalog),
        )
    assert failure.value.code is CapabilityCatalogErrorCode.INVALID_STATUS
    assert failure.value.path == "binding/capability_id"


@pytest.mark.parametrize(
    "change,path",
    (
        ({"catalog_sha256": "0" * 64}, "binding/catalog_sha256"),
        ({"capability_descriptor_sha256": "0" * 64}, "binding/capability_id"),
        (
            {"backend": _backend(build_fingerprint_sha256=_sha("wrong-build"))},
            "binding/backend",
        ),
        (
            {"operation_term": _term("operation.other")},
            "binding/operation_term",
        ),
    ),
)
def test_compile_refuses_binding_drift(change: dict, path: str) -> None:
    catalog = _catalog()
    with pytest.raises(CapabilityCatalogError) as failure:
        compile_capability_intent(
            intent=_intent(),
            catalog=catalog,
            binding=_binding(catalog, **change),
        )
    assert failure.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    assert failure.value.path == path


def test_compile_refuses_unresolved_intent_and_missing_proof() -> None:
    catalog = _catalog()
    unknown = _intent(state=CapabilityIntentValueState.UNKNOWN, value=None)
    with pytest.raises(CapabilityCatalogError) as unresolved:
        compile_capability_intent(
            intent=unknown,
            catalog=catalog,
            binding=_binding(catalog),
        )
    assert unresolved.value.code is CapabilityCatalogErrorCode.INVALID_STATUS

    no_proof = dataclasses.replace(_intent(), proofs=())
    with pytest.raises(CapabilityCatalogError) as proof:
        compile_capability_intent(
            intent=no_proof,
            catalog=catalog,
            binding=_binding(catalog),
        )
    assert proof.value.code is CapabilityCatalogErrorCode.INVALID_STATUS


def test_decoder_rejects_noncanonical_duplicate_and_digest_tamper() -> None:
    raw = encode_capability_intent(_intent())
    value = json.loads(raw)
    noncanonical = json.dumps(value, indent=2, sort_keys=True).encode("ascii")
    duplicate = raw[:-1] + b',"intent_sha256":"' + b"0" * 64 + b'"}'
    for rejected in (noncanonical, duplicate):
        with pytest.raises(CapabilityCatalogError) as failure:
            decode_capability_intent(rejected)
        assert failure.value.code is CapabilityCatalogErrorCode.INVALID_INPUT

    value["intent_sha256"] = "0" * 64
    tampered = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("ascii")
    with pytest.raises(CapabilityCatalogError) as failure:
        decode_capability_intent(tampered)
    assert failure.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
