"""Deterministic validation contracts and verifier entry points."""

from vibecad.validation.contracts import (
    ArtifactObservation,
    CompiledAcceptance,
    EntityObservation,
    EntityParameterObservation,
    ObservationSnapshot,
    PreservationObservation,
    ShapeObservation,
    ValidationError,
    ValidationErrorCode,
    VerificationReceipt,
    VerificationResult,
)
from vibecad.validation.engine import (
    compare_entity_preservation,
    compile_acceptance_spec,
    consume_verification_receipt,
    verify_acceptance,
)

__all__ = (
    "ArtifactObservation",
    "CompiledAcceptance",
    "EntityObservation",
    "EntityParameterObservation",
    "ObservationSnapshot",
    "PreservationObservation",
    "ShapeObservation",
    "ValidationError",
    "ValidationErrorCode",
    "VerificationReceipt",
    "VerificationResult",
    "compare_entity_preservation",
    "compile_acceptance_spec",
    "consume_verification_receipt",
    "verify_acceptance",
)
