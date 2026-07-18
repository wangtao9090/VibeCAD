"""Deterministic validation contracts and verifier entry points."""

from vibecad.validation.contracts import (
    ArtifactObservation,
    CompiledAcceptance,
    ObservationSnapshot,
    ShapeObservation,
    ValidationError,
    ValidationErrorCode,
    VerificationReceipt,
    VerificationResult,
)
from vibecad.validation.engine import (
    compile_acceptance_spec,
    consume_verification_receipt,
    verify_acceptance,
)

__all__ = (
    "ArtifactObservation",
    "CompiledAcceptance",
    "ObservationSnapshot",
    "ShapeObservation",
    "ValidationError",
    "ValidationErrorCode",
    "VerificationReceipt",
    "VerificationResult",
    "compile_acceptance_spec",
    "consume_verification_receipt",
    "verify_acceptance",
)
