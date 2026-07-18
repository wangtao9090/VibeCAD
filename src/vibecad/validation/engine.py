"""Pure compilation, verification, and one-shot receipt consumption."""

from __future__ import annotations

import hashlib

from vibecad.validation.checks import CompiledSpecData, compile_spec, evaluate_criterion
from vibecad.validation.contracts import (
    CompiledAcceptance,
    ObservationSnapshot,
    ValidationErrorCode,
    VerificationReceipt,
    VerificationResult,
    _canonical_json_bytes,
    _CompiledBinding,
    _consume_receipt,
    _issue_compiled,
    _issue_receipt,
    _lookup_compiled,
    _raise_validation,
    _ReceiptBinding,
    _validate_digest,
    _validate_revision,
    _validated_snapshot,
)
from vibecad.workflow.contracts import AcceptanceSpec
from vibecad.workflow.state import CriterionOutcome, VerificationReport

_SPEC_DOMAIN = b"vibecad-compiled-acceptance-spec-v1\0"
_REPORT_DOMAIN = b"vibecad-verification-report-v1\0"


def compile_acceptance_spec(spec: AcceptanceSpec) -> CompiledAcceptance:
    """Compile an exact specification into an authentic process-local capability."""

    compiled = compile_spec(spec)
    digest = hashlib.sha256(_SPEC_DOMAIN + compiled.canonical_bytes).hexdigest()
    return _issue_compiled(
        _CompiledBinding(
            acceptance_id=compiled.acceptance_id,
            spec_digest=digest,
            payload=compiled,
        )
    )


def _report_id(
    compiled: _CompiledBinding,
    *,
    candidate_revision: str,
    manifest_sha256: str,
    observation_digest: str,
    passed: bool,
    verdicts: tuple[object, ...],
) -> str:
    payload = {
        "schema_version": 1,
        "acceptance_id": compiled.acceptance_id,
        "spec_digest": compiled.spec_digest,
        "candidate_revision": candidate_revision,
        "manifest_sha256": manifest_sha256,
        "observation_digest": observation_digest,
        "passed": passed,
        "verdicts": [verdict.to_mapping() for verdict in verdicts],
    }
    digest = hashlib.sha256(_REPORT_DOMAIN + _canonical_json_bytes(payload)).hexdigest()
    return f"verification_{digest[:32]}"


def verify_acceptance(
    compiled: CompiledAcceptance,
    snapshot: ObservationSnapshot,
    *,
    candidate_revision: str,
    manifest_sha256: str,
) -> VerificationResult:
    """Verify a trusted immutable snapshot against a compiled specification."""

    binding = _lookup_compiled(compiled)
    if type(binding.payload) is not CompiledSpecData:
        _raise_validation(ValidationErrorCode.FORGED_CAPABILITY, "/compiled")
    trusted_snapshot = _validated_snapshot(snapshot)
    revision = _validate_revision(candidate_revision)
    manifest = _validate_digest(manifest_sha256, "/manifest_sha256")
    if revision != trusted_snapshot.candidate_revision:
        _raise_validation(ValidationErrorCode.BINDING_MISMATCH, "/candidate_revision")

    verdicts = tuple(
        evaluate_criterion(criterion, trusted_snapshot) for criterion in binding.payload.criteria
    )
    passed = all(
        not verdict.required or verdict.outcome is CriterionOutcome.PASS for verdict in verdicts
    )
    report = VerificationReport(
        id=_report_id(
            binding,
            candidate_revision=revision,
            manifest_sha256=manifest,
            observation_digest=trusted_snapshot.observation_digest,
            passed=passed,
            verdicts=verdicts,
        ),
        acceptance_id=binding.acceptance_id,
        candidate_revision=revision,
        manifest_sha256=manifest,
        observation_digest=trusted_snapshot.observation_digest,
        passed=passed,
        verdicts=verdicts,
    )
    receipt = None
    if passed:
        receipt = _issue_receipt(
            _ReceiptBinding(
                compiled=compiled,
                spec_digest=binding.spec_digest,
                acceptance_id=binding.acceptance_id,
                candidate_revision=revision,
                manifest_sha256=manifest,
                observation_digest=trusted_snapshot.observation_digest,
                report=report,
            )
        )
    return VerificationResult(report=report, receipt=receipt)


def consume_verification_receipt(
    receipt: VerificationReceipt,
    compiled: CompiledAcceptance,
    snapshot: ObservationSnapshot,
    *,
    candidate_revision: str,
    manifest_sha256: str,
) -> VerificationReport:
    """Atomically consume one correctly bound successful receipt."""

    trusted_snapshot = _validated_snapshot(snapshot)
    revision = _validate_revision(candidate_revision)
    manifest = _validate_digest(manifest_sha256, "/manifest_sha256")
    if revision != trusted_snapshot.candidate_revision:
        _raise_validation(ValidationErrorCode.BINDING_MISMATCH, "/candidate_revision")
    return _consume_receipt(
        receipt,
        compiled,
        candidate_revision=revision,
        manifest_sha256=manifest,
        observation_digest=trusted_snapshot.observation_digest,
    )
