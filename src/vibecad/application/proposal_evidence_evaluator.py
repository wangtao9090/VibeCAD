"""Authority-free evidence evaluation for the first A11 photo-CAD slice.

This private application module derives the complete proposal coverage plan
internally.  Callers cannot select requirements, tolerances, feature mappings,
or adoption policy.  A COMPLETE report means only that the bounded in-memory
evidence contract closed; it never grants Task, Revision, HEAD, or adoption
authority.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass
from enum import StrEnum

from vibecad.visual.calibration_authority import (
    ConfirmedPlanarLandmark,
    ConfirmedPlanarMetricBasis,
    InMemoryPlanarCalibrationReceipt,
    build_in_memory_planar_calibration_receipt,
)
from vibecad.visual.capture_quality import (
    CaptureQualityDecision,
    CaptureQualityReport,
    NormalizedCaptureImage,
    assess_capture_quality,
)
from vibecad.visual.contracts import ImageSet
from vibecad.visual.evidence import (
    BoundVisualEvidence,
    ProviderFeatureEvidence,
    bind_visual_evidence,
)
from vibecad.visual.fit_pipeline import (
    EvidenceFeatureFit,
    EvidenceFeatureFitStatus,
    FeatureFitPolicy,
    SourcePlanarCalibration,
    VisualEvidenceFitReport,
    fit_bound_visual_evidence,
)
from vibecad.visual.geometry_fit import (
    CirclePrimitive,
    GeometryFitStatus,
    PrimitiveFamily,
    RotatedRectanglePrimitive,
)
from vibecad.visual.inputs import VisualInputStore
from vibecad.visual.proposal_coverage import (
    MAX_FIRST_SLICE_CONSUMERS,
    ConsumerRequirement,
    ProposalCoverageError,
    ProposalCoverageErrorCode,
    ProposalCoveragePlan,
    derive_proposal_coverage_plan,
)
from vibecad.visual.provider_images import (
    ProviderImageBatch,
    ProviderImagePartKind,
    prepare_provider_image_batch,
)
from vibecad.visual.reconstruction import (
    ClarificationKind,
    ReconstructionProposal,
    VisualClaim,
    VisualClaimStatus,
    VisualClaimUnit,
)

PROPOSAL_EVIDENCE_EVALUATOR_SCHEMA_VERSION = 1
MAX_PROPOSAL_EVALUATION_RECORD_BYTES = 128 * 1024
MAX_NUMERIC_CHECKS = 256
MIN_COMPARISON_TOLERANCE_MM = 0.05
MAX_COMPARISON_TOLERANCE_MM = 0.50
RELATIVE_COMPARISON_TOLERANCE = 0.0025
FIRST_SLICE_FIT_RESIDUAL_TOLERANCE_MM = 0.05

_REPORT_DIGEST_DOMAIN = b"vibecad-proposal-evidence-evaluation-v1\0"


class ProposalEvidenceEvaluationErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    BINDING_MISMATCH = "binding_mismatch"
    INTEGRITY_FAILURE = "integrity_failure"


class ProposalEvidenceEvaluationError(ValueError):
    """Bounded structural failure; ordinary evidence gaps remain report findings."""

    def __init__(self, code: ProposalEvidenceEvaluationErrorCode, path: str = "") -> None:
        if type(code) is not ProposalEvidenceEvaluationErrorCode:
            raise TypeError("code must be an exact ProposalEvidenceEvaluationErrorCode")
        if type(path) is not str or len(path.encode("utf-8")) > 256:
            raise ValueError("path must be bounded")
        self.code = code
        self.path = path
        super().__init__(code.value)


def _fail(code: ProposalEvidenceEvaluationErrorCode, path: str = "") -> None:
    raise ProposalEvidenceEvaluationError(code, path)


class ProposalEvidenceDecision(StrEnum):
    COMPLETE = "complete"
    UNKNOWN = "unknown"
    OUT_OF_ENVELOPE = "out_of_envelope"


class ConsumerClosureMode(StrEnum):
    FIXED_POLICY = "fixed_policy"
    RECTANGLE_FIT = "rectangle_fit"
    CIRCLE_FIT = "circle_fit"
    EXPLICIT_CONFIRMATION = "explicit_confirmation"
    DERIVED = "derived"
    PROGRAM_BINDING = "program_binding"


class ConsumerClosureReason(StrEnum):
    COMPLETE = "complete"
    PROPOSAL_OUT_OF_ENVELOPE = "proposal_out_of_envelope"
    UNSUPPORTED_CONSUMER = "unsupported_consumer"
    MISSING_EXPLICIT_CONFIRMATION = "missing_explicit_confirmation"
    MISSING_FIT = "missing_fit"
    FIT_UNKNOWN = "fit_unknown"
    LINE_ONLY_CANNOT_PROVE_ENDPOINTS = "line_only_cannot_prove_endpoints"
    AMBIGUOUS_FIT = "ambiguous_fit"
    CAPTURE_UNREADABLE = "capture_unreadable"
    UNCERTAINTY_EXCEEDED = "uncertainty_exceeded"
    NUMERIC_MISMATCH = "numeric_mismatch"
    ORPHAN_INPUT = "orphan_input"


@dataclass(frozen=True, slots=True, kw_only=True)
class NumericCheck:
    field_path: str
    expected: float
    observed: float
    allowed_error: float
    observed_error: float

    def to_mapping(self) -> dict[str, object]:
        return {
            "field_path": self.field_path,
            "expected": self.expected,
            "observed": self.observed,
            "allowed_error": self.allowed_error,
            "observed_error": self.observed_error,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumerClosure:
    consumer_path: str
    requirement_digest: str
    decision: ProposalEvidenceDecision
    mode: ConsumerClosureMode
    reason: ConsumerClosureReason
    evidence_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    fit_keys: tuple[str, ...] = ()
    calibration_receipt_sha256s: tuple[str, ...] = ()
    clarification_answer_digests: tuple[str, ...] = ()
    numeric_checks: tuple[NumericCheck, ...] = ()

    def to_mapping(self) -> dict[str, object]:
        return {
            "consumer_path": self.consumer_path,
            "requirement_digest": self.requirement_digest,
            "decision": self.decision.value,
            "mode": self.mode.value,
            "reason": self.reason.value,
            "evidence_ids": list(self.evidence_ids),
            "claim_ids": list(self.claim_ids),
            "fit_keys": list(self.fit_keys),
            "calibration_receipt_sha256s": list(self.calibration_receipt_sha256s),
            "clarification_answer_digests": list(self.clarification_answer_digests),
            "numeric_checks": [item.to_mapping() for item in self.numeric_checks],
        }


@dataclass(frozen=True, slots=True, kw_only=True, init=False)
class ProposalEvidenceEvaluationReport:
    decision: ProposalEvidenceDecision
    reasons: tuple[ConsumerClosureReason, ...]
    proposal_id: str
    proposal_digest: str
    coverage_plan_digest: str | None
    image_set_id: str
    image_set_manifest_sha256: str
    capture_quality_sha256: str
    evidence_sha256: str
    fit_report_sha256: str
    calibration_receipt_sha256s: tuple[str, ...]
    clarification_answer_digests: tuple[str, ...]
    consumers: tuple[ConsumerClosure, ...]
    digest: str
    schema_version: int

    def __new__(cls) -> ProposalEvidenceEvaluationReport:
        raise TypeError("reports are created only by evaluate_proposal_evidence")

    @property
    def task_adoption_eligible(self) -> bool:
        return False

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "decision": self.decision.value,
            "reasons": [item.value for item in self.reasons],
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "coverage_plan_digest": self.coverage_plan_digest,
            "image_set_id": self.image_set_id,
            "image_set_manifest_sha256": self.image_set_manifest_sha256,
            "capture_quality_sha256": self.capture_quality_sha256,
            "evidence_sha256": self.evidence_sha256,
            "fit_report_sha256": self.fit_report_sha256,
            "calibration_receipt_sha256s": list(self.calibration_receipt_sha256s),
            "clarification_answer_digests": list(self.clarification_answer_digests),
            "consumers": [item.to_mapping() for item in self.consumers],
        }

    def to_mapping(self) -> dict[str, object]:
        return self._body_mapping() | {
            "digest": self.digest,
            "task_adoption_eligible": False,
        }


def _canonical_json(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail(ProposalEvidenceEvaluationErrorCode.INVALID_INPUT)
    if not raw or len(raw) > MAX_PROPOSAL_EVALUATION_RECORD_BYTES:
        _fail(ProposalEvidenceEvaluationErrorCode.BUDGET_EXCEEDED)
    return raw


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _new_report(
    *,
    decision: ProposalEvidenceDecision,
    reasons: tuple[ConsumerClosureReason, ...],
    proposal: ReconstructionProposal,
    plan: ProposalCoveragePlan | None,
    image_set: ImageSet,
    capture_quality_sha256: str,
    evidence_sha256: str,
    fit_report_sha256: str,
    calibration_receipt_sha256s: tuple[str, ...],
    clarification_answer_digests: tuple[str, ...],
    consumers: tuple[ConsumerClosure, ...],
) -> ProposalEvidenceEvaluationReport:
    report = object.__new__(ProposalEvidenceEvaluationReport)
    values = {
        "decision": decision,
        "reasons": tuple(sorted(set(reasons), key=lambda item: item.value)),
        "proposal_id": proposal.id,
        "proposal_digest": proposal.digest,
        "coverage_plan_digest": None if plan is None else plan.digest,
        "image_set_id": image_set.id,
        "image_set_manifest_sha256": image_set.manifest_sha256,
        "capture_quality_sha256": capture_quality_sha256,
        "evidence_sha256": evidence_sha256,
        "fit_report_sha256": fit_report_sha256,
        "calibration_receipt_sha256s": tuple(sorted(calibration_receipt_sha256s)),
        "clarification_answer_digests": tuple(sorted(clarification_answer_digests)),
        "consumers": tuple(sorted(consumers, key=lambda item: item.consumer_path)),
        "schema_version": PROPOSAL_EVIDENCE_EVALUATOR_SCHEMA_VERSION,
    }
    for name, value in values.items():
        object.__setattr__(report, name, value)
    if sum(len(item.numeric_checks) for item in report.consumers) > MAX_NUMERIC_CHECKS:
        _fail(ProposalEvidenceEvaluationErrorCode.BUDGET_EXCEEDED, "/consumers")
    body = report._body_mapping()
    digest = hashlib.sha256(_REPORT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()
    object.__setattr__(report, "digest", digest)
    _canonical_json(report.to_mapping())
    return report


def _fit_key(item: EvidenceFeatureFit) -> str:
    return f"{item.source_index}:{item.local_feature_id}"


def _allowed_tolerance(span: float) -> float:
    return min(
        MAX_COMPARISON_TOLERANCE_MM,
        max(MIN_COMPARISON_TOLERANCE_MM, RELATIVE_COMPARISON_TOLERANCE * span),
    )


def _fit_uncertainty(
    fitted: EvidenceFeatureFit,
    receipt: InMemoryPlanarCalibrationReceipt,
) -> float:
    result = fitted.fit_result
    if result is None or result.max_residual_mm is None:
        return math.inf
    point_uncertainty = max((item.uncertainty_mm for item in fitted.plane_points), default=math.inf)
    values = (
        receipt.calibration.fit_error_indicator_mm,
        point_uncertainty,
        result.max_residual_mm,
    )
    if any(
        type(item) not in {int, float} or not math.isfinite(item) or item < 0
        for item in values
    ):
        return math.inf
    return math.fsum(float(item) for item in values)


def _closure(
    requirement: ConsumerRequirement,
    *,
    decision: ProposalEvidenceDecision,
    mode: ConsumerClosureMode,
    reason: ConsumerClosureReason,
    fit_keys: tuple[str, ...] = (),
    receipts: tuple[str, ...] = (),
    answers: tuple[str, ...] = (),
    checks: tuple[NumericCheck, ...] = (),
) -> ConsumerClosure:
    return ConsumerClosure(
        consumer_path=requirement.consumer_path,
        requirement_digest=requirement.digest,
        decision=decision,
        mode=mode,
        reason=reason,
        evidence_ids=requirement.evidence_ids,
        claim_ids=requirement.claim_ids,
        fit_keys=tuple(sorted(fit_keys)),
        calibration_receipt_sha256s=tuple(sorted(receipts)),
        clarification_answer_digests=tuple(sorted(answers)),
        numeric_checks=checks,
    )


def _explicit_answers(
    requirement: ConsumerRequirement,
    *,
    claims: dict[str, VisualClaim],
    proposal: ReconstructionProposal,
) -> tuple[str, ...] | None:
    question_by_claim = {item.claim_id: item for item in proposal.observation.questions}
    answer_by_question = {item.question_id: item for item in proposal.clarification_answers}
    digests: list[str] = []
    for claim_id in requirement.claim_ids:
        claim = claims[claim_id]
        question = question_by_claim.get(claim_id)
        answer = None if question is None else answer_by_question.get(question.id)
        if (
            claim.status is not VisualClaimStatus.ASSUMED
            or question is None
            or question.kind is not ClarificationKind.CONFIRM_ASSUMPTION
            or answer is None
            or answer.claim_id != claim_id
            or answer.response is not True
        ):
            return None
        digests.append(answer.digest)
    return tuple(sorted(digests))


def _assumed_value_matches(
    requirement: ConsumerRequirement,
    *,
    claims: dict[str, VisualClaim],
    expected: float | bool,
) -> bool:
    for claim_id in requirement.claim_ids:
        claim = claims[claim_id]
        if type(expected) is bool:
            if claim.unit is not None or claim.value is not expected:
                return False
        elif (
            claim.unit is not VisualClaimUnit.MM
            or type(claim.value) not in {int, float}
            or not math.isclose(float(claim.value), expected, rel_tol=0.0, abs_tol=1e-9)
        ):
            return False
    return True


def _rectangle_vertices(design: object) -> tuple[tuple[float, float], ...]:
    sketch = next(item for item in design.sketches if item.role.value == "profile")
    points = {
        (float(geometry.dimensions["x1_mm"]), float(geometry.dimensions["y1_mm"]))
        for geometry in sketch.geometries
    } | {
        (float(geometry.dimensions["x2_mm"]), float(geometry.dimensions["y2_mm"]))
        for geometry in sketch.geometries
    }
    return tuple(sorted(points))


def _fitted_rectangle_vertices(
    primitive: RotatedRectanglePrimitive,
) -> tuple[tuple[float, float], ...]:
    cosine = math.cos(primitive.angle_rad)
    sine = math.sin(primitive.angle_rad)
    ux, uy = cosine, sine
    vx, vy = -sine, cosine
    return tuple(
        (
            primitive.center_x_mm
            + sx * primitive.width_mm * ux / 2
            + sy * primitive.height_mm * vx / 2,
            primitive.center_y_mm
            + sx * primitive.width_mm * uy / 2
            + sy * primitive.height_mm * vy / 2,
        )
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))
    )


def _point_error(
    expected: tuple[tuple[float, float], ...],
    observed: tuple[tuple[float, float], ...],
) -> float:
    if len(expected) != len(observed):
        return math.inf
    return min(
        max(math.dist(left, right) for left, right in zip(expected, order, strict=True))
        for order in itertools.permutations(observed)
    )


def _decision_for(consumers: tuple[ConsumerClosure, ...]) -> ProposalEvidenceDecision:
    if any(item.decision is ProposalEvidenceDecision.OUT_OF_ENVELOPE for item in consumers):
        return ProposalEvidenceDecision.OUT_OF_ENVELOPE
    if any(item.decision is ProposalEvidenceDecision.UNKNOWN for item in consumers):
        return ProposalEvidenceDecision.UNKNOWN
    return ProposalEvidenceDecision.COMPLETE


def _evaluate_recomputed_pipeline(
    *,
    proposal: ReconstructionProposal,
    image_set: ImageSet,
    capture_quality: CaptureQualityReport,
    evidence: BoundVisualEvidence,
    fit_report: VisualEvidenceFitReport,
    calibration_receipt: InMemoryPlanarCalibrationReceipt,
) -> ProposalEvidenceEvaluationReport:
    """Close consumers over values recomputed by the application entry."""

    receipt = calibration_receipt
    clarification_facts = proposal.clarification_answers
    capture_digest = _sha256(
        {
            "decision": capture_quality.decision.value,
            "readable": capture_quality.readable_source_indices,
        }
    )
    evidence_digest = _sha256(
        [
            {
                "key": (item.source_index, item.local_feature_id),
                "family": item.family.value,
                "claims": item.claim_ids,
                "points": tuple((point.x, point.y) for point in item.normalized_points),
            }
            for item in evidence.features
        ]
    )
    fit_digest = _sha256(
        {
            "evidence": evidence_digest,
            "receipt": receipt.receipt_sha256,
            "policy_mm": FIRST_SLICE_FIT_RESIDUAL_TOLERANCE_MM,
        }
    )
    receipt_digests = (receipt.receipt_sha256,)
    answer_digests = tuple(item.digest for item in clarification_facts)
    try:
        plan = derive_proposal_coverage_plan(proposal=proposal)
    except ProposalCoverageError as error:
        if error.code in {
            ProposalCoverageErrorCode.OUT_OF_ENVELOPE,
            ProposalCoverageErrorCode.MISSING_COVERAGE,
            ProposalCoverageErrorCode.AMBIGUOUS_COVERAGE,
            ProposalCoverageErrorCode.ORPHAN_EVIDENCE,
            ProposalCoverageErrorCode.ORPHAN_CLAIM,
        }:
            return _new_report(
                decision=ProposalEvidenceDecision.OUT_OF_ENVELOPE,
                reasons=(ConsumerClosureReason.PROPOSAL_OUT_OF_ENVELOPE,),
                proposal=proposal,
                plan=None,
                image_set=image_set,
                capture_quality_sha256=capture_digest,
                evidence_sha256=evidence_digest,
                fit_report_sha256=fit_digest,
                calibration_receipt_sha256s=receipt_digests,
                clarification_answer_digests=answer_digests,
                consumers=(),
            )
        _fail(ProposalEvidenceEvaluationErrorCode.INTEGRITY_FAILURE, error.path)

    requirements = {item.consumer_path: item for item in plan.requirements}
    if len(requirements) > MAX_FIRST_SLICE_CONSUMERS:
        _fail(ProposalEvidenceEvaluationErrorCode.BUDGET_EXCEEDED, "/requirements")
    design = proposal.design
    profile_index = next(
        index for index, item in enumerate(design.sketches) if item.role.value == "profile"
    )
    profile = design.sketches[profile_index]
    profile_path = f"/design/sketches/{profile_index}"
    line_paths = tuple(
        f"{profile_path}/geometries/{index}" for index, _item in enumerate(profile.geometries)
    )
    location_entry = next(
        (
            (index, item)
            for index, item in enumerate(design.sketches)
            if item.role.value == "hole_locations"
        ),
        None,
    )
    hole_paths: tuple[str, ...] = ()
    location_path: str | None = None
    if location_entry is not None:
        location_index, locations = location_entry
        location_path = f"/design/sketches/{location_index}"
        hole_paths = tuple(
            f"{location_path}/geometries/{index}"
            for index, _item in enumerate(locations.geometries)
        )

    claims = {item.id: item for item in proposal.observation.claims}
    fits = fit_report.feature_fits
    readable = set(capture_quality.readable_source_indices)
    consumers: dict[str, ConsumerClosure] = {}
    used_fit_keys: set[str] = set()
    used_answers: set[str] = set()

    def fixed(path: str, mode: ConsumerClosureMode) -> None:
        requirement = requirements[path]
        consumers[path] = _closure(
            requirement,
            decision=ProposalEvidenceDecision.COMPLETE,
            mode=mode,
            reason=ConsumerClosureReason.COMPLETE,
        )

    fixed("/design", ConsumerClosureMode.FIXED_POLICY)
    fixed("/program/operations/0", ConsumerClosureMode.PROGRAM_BINDING)

    rectangle_claims = set(requirements[profile_path].claim_ids)
    for path in line_paths:
        rectangle_claims.update(requirements[path].claim_ids)
    rectangle_candidates = tuple(
        item
        for item in fits
        if item.family is PrimitiveFamily.ROTATED_RECTANGLE
        and set(item.claim_ids) == rectangle_claims
    )
    rectangle_paths = (profile_path, *line_paths)
    if len(rectangle_candidates) > 1:
        for path in rectangle_paths:
            consumers[path] = _closure(
                requirements[path],
                decision=ProposalEvidenceDecision.OUT_OF_ENVELOPE,
                mode=ConsumerClosureMode.RECTANGLE_FIT,
                reason=ConsumerClosureReason.AMBIGUOUS_FIT,
            )
    elif not rectangle_candidates:
        line_candidates = tuple(
            item
            for item in fits
            if item.family is PrimitiveFamily.LINE
            and rectangle_claims & set(item.claim_ids)
        )
        for item in line_candidates:
            used_fit_keys.add(_fit_key(item))
        reason = (
            ConsumerClosureReason.LINE_ONLY_CANNOT_PROVE_ENDPOINTS
            if line_candidates
            else ConsumerClosureReason.MISSING_FIT
        )
        for path in rectangle_paths:
            consumers[path] = _closure(
                requirements[path],
                decision=ProposalEvidenceDecision.UNKNOWN,
                mode=ConsumerClosureMode.RECTANGLE_FIT,
                reason=reason,
            )
    else:
        fitted = rectangle_candidates[0]
        key = _fit_key(fitted)
        used_fit_keys.add(key)
        decision = ProposalEvidenceDecision.COMPLETE
        reason = ConsumerClosureReason.COMPLETE
        checks: tuple[NumericCheck, ...] = ()
        if (
            capture_quality.decision is CaptureQualityDecision.STOP
            or fitted.source_index not in readable
        ):
            decision, reason = (
                ProposalEvidenceDecision.UNKNOWN,
                ConsumerClosureReason.CAPTURE_UNREADABLE,
            )
        elif (
            fitted.status is not EvidenceFeatureFitStatus.FITTED
            or fitted.fit_result is None
            or fitted.fit_result.status is not GeometryFitStatus.FITTED
            or type(fitted.fit_result.primitive) is not RotatedRectanglePrimitive
        ):
            decision, reason = ProposalEvidenceDecision.UNKNOWN, ConsumerClosureReason.FIT_UNKNOWN
        else:
            primitive = fitted.fit_result.primitive
            span = max(primitive.width_mm, primitive.height_mm)
            tolerance = _allowed_tolerance(span)
            uncertainty = _fit_uncertainty(fitted, receipt)
            error = _point_error(
                _rectangle_vertices(design),
                _fitted_rectangle_vertices(primitive),
            )
            checks = (
                NumericCheck(
                    field_path=profile_path,
                    expected=0.0,
                    observed=error,
                    allowed_error=tolerance,
                    observed_error=error,
                ),
            )
            if uncertainty > tolerance:
                decision, reason = (
                    ProposalEvidenceDecision.UNKNOWN,
                    ConsumerClosureReason.UNCERTAINTY_EXCEEDED,
                )
            elif error > tolerance:
                decision, reason = (
                    ProposalEvidenceDecision.UNKNOWN,
                    ConsumerClosureReason.NUMERIC_MISMATCH,
                )
        receipt_ids = () if receipt is None else (receipt.receipt_sha256,)
        for path in rectangle_paths:
            consumers[path] = _closure(
                requirements[path],
                decision=decision,
                mode=ConsumerClosureMode.RECTANGLE_FIT,
                reason=reason,
                fit_keys=(key,),
                receipts=receipt_ids,
                checks=checks,
            )

    pad = design.features[0]
    pad_path = "/design/features/0"
    pad_length_id = pad.parameters["length"]
    pad_length = float(next(item.value for item in design.parameters if item.id == pad_length_id))
    parameter_paths = {
        item.id: f"/design/parameters/{index}" for index, item in enumerate(design.parameters)
    }
    pad_length_path = parameter_paths[pad_length_id]
    explicit_paths = (pad_length_path, pad_path)
    if len(design.features) == 2:
        explicit_paths += ("/design/features/1",)
    for path in explicit_paths:
        requirement = requirements[path]
        answers = _explicit_answers(requirement, claims=claims, proposal=proposal)
        if answers is None:
            consumers[path] = _closure(
                requirement,
                decision=ProposalEvidenceDecision.UNKNOWN,
                mode=ConsumerClosureMode.EXPLICIT_CONFIRMATION,
                reason=ConsumerClosureReason.MISSING_EXPLICIT_CONFIRMATION,
            )
        else:
            used_answers.update(answers)
            expected: float | bool = pad_length if path == pad_length_path else True
            matches = _assumed_value_matches(
                requirement,
                claims=claims,
                expected=expected,
            )
            consumers[path] = _closure(
                requirement,
                decision=(
                    ProposalEvidenceDecision.COMPLETE
                    if matches
                    else ProposalEvidenceDecision.UNKNOWN
                ),
                mode=ConsumerClosureMode.EXPLICIT_CONFIRMATION,
                reason=(
                    ConsumerClosureReason.COMPLETE
                    if matches
                    else ConsumerClosureReason.NUMERIC_MISMATCH
                ),
                answers=answers,
                checks=(
                    NumericCheck(
                        field_path=f"{path}/assumed_value",
                        expected=float(expected),
                        observed=float(claims[requirement.claim_ids[0]].value),
                        allowed_error=0.0,
                        observed_error=abs(
                            float(expected)
                            - float(claims[requirement.claim_ids[0]].value)
                        ),
                    ),
                )
                if type(expected) is float
                and type(claims[requirement.claim_ids[0]].value) in {int, float}
                else (),
            )

    if location_entry is not None:
        _location_index, locations = location_entry
        assert location_path is not None
        diameter_id = design.features[1].parameters["diameter"]
        diameter_path = parameter_paths[diameter_id]
        diameter_value = float(
            next(item.value for item in design.parameters if item.id == diameter_id)
        )
        location_claims = set(requirements[location_path].claim_ids)
        diameter_claims = set(requirements[diameter_path].claim_ids)
        hole_closures: list[ConsumerClosure] = []
        for geometry, path in zip(locations.geometries, hole_paths, strict=True):
            required_claims = set(requirements[path].claim_ids)
            candidates = tuple(
                item
                for item in fits
                if item.family is PrimitiveFamily.CIRCLE
                and set(item.claim_ids)
                == required_claims | location_claims | diameter_claims
            )
            if len(candidates) > 1:
                closure = _closure(
                    requirements[path],
                    decision=ProposalEvidenceDecision.OUT_OF_ENVELOPE,
                    mode=ConsumerClosureMode.CIRCLE_FIT,
                    reason=ConsumerClosureReason.AMBIGUOUS_FIT,
                )
            elif not candidates:
                closure = _closure(
                    requirements[path],
                    decision=ProposalEvidenceDecision.UNKNOWN,
                    mode=ConsumerClosureMode.CIRCLE_FIT,
                    reason=ConsumerClosureReason.MISSING_FIT,
                )
            else:
                fitted = candidates[0]
                key = _fit_key(fitted)
                used_fit_keys.add(key)
                decision = ProposalEvidenceDecision.COMPLETE
                reason = ConsumerClosureReason.COMPLETE
                checks: tuple[NumericCheck, ...] = ()
                if (
                    capture_quality.decision is CaptureQualityDecision.STOP
                    or fitted.source_index not in readable
                ):
                    decision, reason = (
                        ProposalEvidenceDecision.UNKNOWN,
                        ConsumerClosureReason.CAPTURE_UNREADABLE,
                    )
                elif (
                    fitted.status is not EvidenceFeatureFitStatus.FITTED
                    or fitted.fit_result is None
                    or fitted.fit_result.status is not GeometryFitStatus.FITTED
                    or type(fitted.fit_result.primitive) is not CirclePrimitive
                ):
                    decision, reason = (
                        ProposalEvidenceDecision.UNKNOWN,
                        ConsumerClosureReason.FIT_UNKNOWN,
                    )
                else:
                    primitive = fitted.fit_result.primitive
                    tolerance = _allowed_tolerance(diameter_value)
                    uncertainty = _fit_uncertainty(fitted, receipt)
                    expected = (
                        float(geometry.dimensions["cx_mm"]),
                        float(geometry.dimensions["cy_mm"]),
                        float(geometry.dimensions["radius_mm"]),
                        diameter_value,
                    )
                    observed = (
                        primitive.center_x_mm,
                        primitive.center_y_mm,
                        primitive.radius_mm,
                        2.0 * primitive.radius_mm,
                    )
                    names = ("cx_mm", "cy_mm", "radius_mm", "diameter_mm")
                    checks = tuple(
                        NumericCheck(
                            field_path=f"{path}/{name}",
                            expected=left,
                            observed=right,
                            allowed_error=tolerance,
                            observed_error=abs(left - right),
                        )
                        for name, left, right in zip(names, expected, observed, strict=True)
                    )
                    if uncertainty > tolerance:
                        decision, reason = (
                            ProposalEvidenceDecision.UNKNOWN,
                            ConsumerClosureReason.UNCERTAINTY_EXCEEDED,
                        )
                    elif any(item.observed_error > tolerance for item in checks):
                        decision, reason = (
                            ProposalEvidenceDecision.UNKNOWN,
                            ConsumerClosureReason.NUMERIC_MISMATCH,
                        )
                receipt_ids = () if receipt is None else (receipt.receipt_sha256,)
                closure = _closure(
                    requirements[path],
                    decision=decision,
                    mode=ConsumerClosureMode.CIRCLE_FIT,
                    reason=reason,
                    fit_keys=(key,),
                    receipts=receipt_ids,
                    checks=checks,
                )
            consumers[path] = closure
            hole_closures.append(closure)
        dependent_decision = _decision_for(tuple(hole_closures))
        dependent_reason = (
            ConsumerClosureReason.COMPLETE
            if dependent_decision is ProposalEvidenceDecision.COMPLETE
            else next(item.reason for item in hole_closures if item.decision is dependent_decision)
        )
        shared_fit_keys = tuple(key for item in hole_closures for key in item.fit_keys)
        shared_receipts = tuple(
            value for item in hole_closures for value in item.calibration_receipt_sha256s
        )
        for path in (location_path, diameter_path):
            consumers[path] = _closure(
                requirements[path],
                decision=dependent_decision,
                mode=ConsumerClosureMode.DERIVED,
                reason=dependent_reason,
                fit_keys=shared_fit_keys,
                receipts=shared_receipts,
            )

    for path, requirement in requirements.items():
        if path in consumers:
            continue
        consumers[path] = _closure(
            requirement,
            decision=ProposalEvidenceDecision.OUT_OF_ENVELOPE,
            mode=ConsumerClosureMode.DERIVED,
            reason=ConsumerClosureReason.UNSUPPORTED_CONSUMER,
        )

    all_fit_keys = {_fit_key(item) for item in fits}
    orphan = all_fit_keys - used_fit_keys
    orphan_answers = set(answer_digests) - used_answers
    if orphan or orphan_answers:
        root = consumers["/design"]
        consumers["/design"] = _closure(
            requirements["/design"],
            decision=ProposalEvidenceDecision.OUT_OF_ENVELOPE,
            mode=root.mode,
            reason=ConsumerClosureReason.ORPHAN_INPUT,
        )
    ordered = tuple(consumers.values())
    decision = _decision_for(ordered)
    reasons = tuple(
        item.reason
        for item in ordered
        if item.reason is not ConsumerClosureReason.COMPLETE
    )
    if not reasons:
        reasons = (ConsumerClosureReason.COMPLETE,)
    return _new_report(
        decision=decision,
        reasons=reasons,
        proposal=proposal,
        plan=plan,
        image_set=image_set,
        capture_quality_sha256=capture_digest,
        evidence_sha256=evidence_digest,
        fit_report_sha256=fit_digest,
        calibration_receipt_sha256s=receipt_digests,
        clarification_answer_digests=answer_digests,
        consumers=ordered,
    )


def _batch_is_exact(actual: ProviderImageBatch, expected: ProviderImageBatch) -> bool:
    return actual == expected and all(
        left.data == right.data
        for left, right in zip(actual.parts, expected.parts, strict=True)
    )


def evaluate_proposal_evidence(
    *,
    proposal: ReconstructionProposal,
    visual_input_store: VisualInputStore,
    image_batch: ProviderImageBatch,
    provider_features: tuple[ProviderFeatureEvidence, ...],
    calibration_landmarks: tuple[ConfirmedPlanarLandmark, ...],
    metric_basis: ConfirmedPlanarMetricBasis,
) -> ProposalEvidenceEvaluationReport:
    """Recompute every derived decision input without granting authority."""

    if (
        type(proposal) is not ReconstructionProposal
        or type(visual_input_store) is not VisualInputStore
        or type(image_batch) is not ProviderImageBatch
        or type(provider_features) is not tuple
        or any(type(item) is not ProviderFeatureEvidence for item in provider_features)
        or type(calibration_landmarks) is not tuple
        or any(type(item) is not ConfirmedPlanarLandmark for item in calibration_landmarks)
        or type(metric_basis) is not ConfirmedPlanarMetricBasis
    ):
        _fail(ProposalEvidenceEvaluationErrorCode.INVALID_INPUT)
    observation = proposal.observation
    image_set, normalized_bytes = visual_input_store.read_provider_images_exact(
        observation.image_set_id,
        observation.image_set_manifest_sha256,
    )
    captures = tuple(
        NormalizedCaptureImage(
            source_index=index,
            visual_input_id=item.normalized.id,
            width=item.normalized.width,
            height=item.normalized.height,
            sha256=item.normalized.sha256,
            data=normalized_bytes[index],
        )
        for index, item in enumerate(image_set.inputs)
    )
    capture_quality = assess_capture_quality(captures)
    expected_batch = prepare_provider_image_batch(
        image_set=image_set,
        normalized_images=normalized_bytes,
        profile=image_batch.profile,
        detail_crops=(),
    )
    if (
        any(item.kind is not ProviderImagePartKind.OVERVIEW for item in image_batch.parts)
        or not _batch_is_exact(image_batch, expected_batch)
    ):
        _fail(ProposalEvidenceEvaluationErrorCode.BINDING_MISMATCH, "/image_batch")
    sources = {item.source_index for item in provider_features}
    if len(sources) != 1:
        _fail(ProposalEvidenceEvaluationErrorCode.INVALID_INPUT, "/provider_features")
    source_index = next(iter(sources))
    receipt = build_in_memory_planar_calibration_receipt(
        image_set=image_set,
        image_batch=expected_batch,
        source_index=source_index,
        landmarks=calibration_landmarks,
        metric_basis=metric_basis,
    )
    evidence = bind_visual_evidence(
        observation=observation,
        image_set=image_set,
        image_batch=expected_batch,
        features=provider_features,
    )
    policies = tuple(
        FeatureFitPolicy(
            source_index=item.source_index,
            local_feature_id=item.local_feature_id,
            residual_tolerance_mm=FIRST_SLICE_FIT_RESIDUAL_TOLERANCE_MM,
        )
        for item in evidence.features
    )
    calibration = SourcePlanarCalibration(
        source_index=receipt.source_index,
        image_set_manifest_sha256=receipt.image_set_manifest_sha256,
        provider_image_id=receipt.provider_image_id,
        frame_id=receipt.metric_basis.frame_id,
        calibration=receipt.calibration,
    )
    fit_report = fit_bound_visual_evidence(
        evidence=evidence,
        calibrations=(calibration,),
        policies=policies,
    )
    return _evaluate_recomputed_pipeline(
        proposal=proposal,
        image_set=image_set,
        capture_quality=capture_quality,
        evidence=evidence,
        fit_report=fit_report,
        calibration_receipt=receipt,
    )


__all__: tuple[str, ...] = ()
