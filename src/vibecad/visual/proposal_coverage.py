"""Authority-free coverage planning for the first ordinary-photo CAD envelope.

This module only freezes what a later evidence evaluator would have to prove.
It does not read images, invoke a provider, evaluate a fit, construct a
``ModelProgram``, create a Task, touch a Revision, or grant adoption authority.

The important boundary is that callers cannot supply a subset of required
features or consumers.  The complete requirement set is derived from one exact
``ReconstructionProposal`` and binds the proposal, design, observation,
acceptance, and the only future ``create_parametric_design`` operation payload.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from vibecad.parametric.contracts import (
    ConstraintKind,
    DesignUnit,
    FeatureExtent,
    FeatureKind,
    GeometryKind,
    OriginPlane,
    ParametricDesignIR,
    ParametricSketch,
    PlaneKind,
    SketchGeometry,
    SketchRole,
)
from vibecad.visual.reconstruction import ReconstructionProposal
from vibecad.workflow.contracts import ModelCommand, ValueSource
from vibecad.workflow.errors import is_canonical_json_pointer

PROPOSAL_COVERAGE_SCHEMA_VERSION = 1
MAX_FIRST_SLICE_CONSUMERS = 64
MAX_REQUIREMENT_EVIDENCE_IDS = 8
MAX_REQUIREMENT_CLAIM_IDS = 64
MAX_REQUIREMENT_DEPENDENCIES = 64
MAX_TOTAL_DEPENDENCIES = 512
MAX_PROPOSAL_COVERAGE_RECORD_BYTES = 128 * 1024
MAX_FIRST_SLICE_HOLES = 16

_MAX_ERROR_PATH_BYTES = 512
_DIGEST_DOMAIN = b"vibecad-proposal-coverage-plan-v1\0"
_PAYLOAD_DIGEST_DOMAIN = b"vibecad-proposal-coverage-payload-v1\0"
_REQUIREMENT_DIGEST_DOMAIN = b"vibecad-proposal-coverage-requirement-v1\0"
_OPERATION_DIGEST_DOMAIN = b"vibecad-proposal-coverage-operation-v1\0"


class ProposalCoverageErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    OUT_OF_ENVELOPE = "out_of_envelope"
    MISSING_COVERAGE = "missing_coverage"
    AMBIGUOUS_COVERAGE = "ambiguous_coverage"
    ORPHAN_EVIDENCE = "orphan_evidence"
    ORPHAN_CLAIM = "orphan_claim"
    INTEGRITY_FAILURE = "integrity_failure"


class ProposalCoverageError(ValueError):
    """Bounded failure for malformed or incomplete coverage plans."""

    def __init__(self, code: ProposalCoverageErrorCode, path: str = "") -> None:
        if type(code) is not ProposalCoverageErrorCode:
            raise TypeError("code must be an exact ProposalCoverageErrorCode")
        if (
            type(path) is not str
            or len(path.encode("utf-8")) > _MAX_ERROR_PATH_BYTES
            or not is_canonical_json_pointer(path)
        ):
            raise ValueError("path must be a bounded canonical JSON Pointer")
        self.code = code
        self.path = path
        super().__init__(code.value)


def _fail(code: ProposalCoverageErrorCode, path: str = "") -> None:
    raise ProposalCoverageError(code, path)


class CoverageConsumerKind(StrEnum):
    DESIGN_ROOT = "design_root"
    PARAMETER = "parameter"
    SKETCH = "sketch"
    GEOMETRY = "geometry"
    CONSTRAINT = "constraint"
    FEATURE = "feature"
    PROGRAM_OPERATION = "program_operation"


class CoverageMode(StrEnum):
    FIXED_POLICY = "fixed_policy"
    EVIDENCE_REQUIRED = "evidence_required"
    DERIVED = "derived"
    PROGRAM_BINDING = "program_binding"


def _canonical_json(value: object, *, maximum: int) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail(ProposalCoverageErrorCode.INVALID_INPUT)
    if not raw or len(raw) > maximum:
        _fail(ProposalCoverageErrorCode.BUDGET_EXCEEDED)
    return raw


def _sha256(domain: bytes, value: object, *, maximum: int) -> str:
    return hashlib.sha256(domain + _canonical_json(value, maximum=maximum)).hexdigest()


def _bounded_text(value: object, path: str) -> str:
    if type(value) is not str:
        _fail(ProposalCoverageErrorCode.INVALID_INPUT, path)
    try:
        raw = value.encode("utf-8")
    except UnicodeError:
        _fail(ProposalCoverageErrorCode.INVALID_INPUT, path)
    if not raw or len(raw) > 256 or value.strip() != value or not value.isprintable():
        _fail(ProposalCoverageErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail(ProposalCoverageErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsumerRequirement:
    """One complete CAD-effective consumer derived from the frozen proposal."""

    consumer_path: str
    consumer_id: str
    kind: CoverageConsumerKind
    mode: CoverageMode
    payload_sha256: str
    evidence_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    dependency_paths: tuple[str, ...] = ()
    digest: str = ""
    schema_version: int = PROPOSAL_COVERAGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(ProposalCoverageErrorCode.INVALID_INPUT, "/schema_version")
        if (
            type(self.consumer_path) is not str
            or not is_canonical_json_pointer(self.consumer_path)
            or not self.consumer_path
        ):
            _fail(ProposalCoverageErrorCode.INVALID_INPUT, "/consumer_path")
        object.__setattr__(
            self,
            "consumer_id",
            _bounded_text(self.consumer_id, "/consumer_id"),
        )
        if type(self.kind) is not CoverageConsumerKind:
            _fail(ProposalCoverageErrorCode.INVALID_INPUT, "/kind")
        if type(self.mode) is not CoverageMode:
            _fail(ProposalCoverageErrorCode.INVALID_INPUT, "/mode")
        object.__setattr__(
            self,
            "payload_sha256",
            _digest(self.payload_sha256, "/payload_sha256"),
        )
        for name, maximum in (
            ("evidence_ids", MAX_REQUIREMENT_EVIDENCE_IDS),
            ("claim_ids", MAX_REQUIREMENT_CLAIM_IDS),
            ("dependency_paths", MAX_REQUIREMENT_DEPENDENCIES),
        ):
            values = getattr(self, name)
            if (
                type(values) is not tuple
                or len(values) > maximum
                or len(set(values)) != len(values)
                or any(type(item) is not str or not item for item in values)
            ):
                _fail(ProposalCoverageErrorCode.INVALID_INPUT, f"/{name}")
            if name == "dependency_paths" and any(
                not is_canonical_json_pointer(item) or not item for item in values
            ):
                _fail(ProposalCoverageErrorCode.INVALID_INPUT, f"/{name}")
            object.__setattr__(self, name, tuple(sorted(values)))
        if self.mode is CoverageMode.EVIDENCE_REQUIRED:
            if not self.evidence_ids or not self.claim_ids:
                _fail(ProposalCoverageErrorCode.MISSING_COVERAGE, "/evidence_ids")
        elif self.evidence_ids or self.claim_ids:
            _fail(ProposalCoverageErrorCode.INVALID_INPUT, "/evidence_ids")
        if self.mode is CoverageMode.DERIVED and not self.dependency_paths:
            _fail(ProposalCoverageErrorCode.MISSING_COVERAGE, "/dependency_paths")
        body = self._body_mapping()
        expected = _sha256(
            _REQUIREMENT_DIGEST_DOMAIN,
            body,
            maximum=16 * 1024,
        )
        if self.digest and _digest(self.digest, "/digest") != expected:
            _fail(ProposalCoverageErrorCode.INTEGRITY_FAILURE, "/digest")
        object.__setattr__(self, "digest", expected)

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "consumer_path": self.consumer_path,
            "consumer_id": self.consumer_id,
            "kind": self.kind.value,
            "mode": self.mode.value,
            "payload_sha256": self.payload_sha256,
            "evidence_ids": list(self.evidence_ids),
            "claim_ids": list(self.claim_ids),
            "dependency_paths": list(self.dependency_paths),
        }

    def to_mapping(self) -> dict[str, object]:
        return self._body_mapping() | {"digest": self.digest}


@dataclass(frozen=True, slots=True, kw_only=True, init=False)
class ProposalCoveragePlan:
    """Authority-free proof that the complete first-slice consumer set was frozen.

    The ordinary constructor is intentionally unavailable.  A future evaluator
    must accept the exact proposal and call :func:`derive_proposal_coverage_plan`
    internally; it must never accept a caller-supplied plan or requirements.
    """

    proposal_id: str
    proposal_digest: str
    design_digest: str
    observation_digest: str
    acceptance_digest: str
    expected_operation_payload_sha256: str
    requirements: tuple[ConsumerRequirement, ...]
    digest: str = ""
    schema_version: int = PROPOSAL_COVERAGE_SCHEMA_VERSION

    def _seal(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(ProposalCoverageErrorCode.INVALID_INPUT, "/schema_version")
        object.__setattr__(self, "proposal_id", _bounded_text(self.proposal_id, "/proposal_id"))
        for name in (
            "proposal_digest",
            "design_digest",
            "observation_digest",
            "acceptance_digest",
            "expected_operation_payload_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if (
            type(self.requirements) is not tuple
            or not self.requirements
            or len(self.requirements) > MAX_FIRST_SLICE_CONSUMERS
            or any(type(item) is not ConsumerRequirement for item in self.requirements)
        ):
            _fail(ProposalCoverageErrorCode.BUDGET_EXCEEDED, "/requirements")
        paths = tuple(item.consumer_path for item in self.requirements)
        if len(set(paths)) != len(paths):
            _fail(ProposalCoverageErrorCode.AMBIGUOUS_COVERAGE, "/requirements")
        requirements = tuple(sorted(self.requirements, key=lambda item: item.consumer_path))
        requirement_paths = {item.consumer_path for item in requirements}
        for item in requirements:
            if not set(item.dependency_paths).issubset(requirement_paths):
                _fail(ProposalCoverageErrorCode.INTEGRITY_FAILURE, "/requirements")
        if sum(len(item.dependency_paths) for item in requirements) > MAX_TOTAL_DEPENDENCIES:
            _fail(ProposalCoverageErrorCode.BUDGET_EXCEEDED, "/requirements")
        object.__setattr__(self, "requirements", requirements)
        expected = _sha256(
            _DIGEST_DOMAIN,
            self._body_mapping(),
            maximum=MAX_PROPOSAL_COVERAGE_RECORD_BYTES,
        )
        if self.digest and _digest(self.digest, "/digest") != expected:
            _fail(ProposalCoverageErrorCode.INTEGRITY_FAILURE, "/digest")
        object.__setattr__(self, "digest", expected)

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "design_digest": self.design_digest,
            "observation_digest": self.observation_digest,
            "acceptance_digest": self.acceptance_digest,
            "expected_operation_payload_sha256": self.expected_operation_payload_sha256,
            "requirements": [item.to_mapping() for item in self.requirements],
        }

    def to_mapping(self) -> dict[str, object]:
        return self._body_mapping() | {"digest": self.digest}


def _new_proposal_coverage_plan(
    *,
    proposal_id: str,
    proposal_digest: str,
    design_digest: str,
    observation_digest: str,
    acceptance_digest: str,
    expected_operation_payload_sha256: str,
    requirements: tuple[ConsumerRequirement, ...],
) -> ProposalCoveragePlan:
    """Construct one plan for the derivation path; never consume caller input."""

    plan = object.__new__(ProposalCoveragePlan)
    for name, value in (
        ("proposal_id", proposal_id),
        ("proposal_digest", proposal_digest),
        ("design_digest", design_digest),
        ("observation_digest", observation_digest),
        ("acceptance_digest", acceptance_digest),
        ("expected_operation_payload_sha256", expected_operation_payload_sha256),
        ("requirements", requirements),
        ("digest", ""),
        ("schema_version", PROPOSAL_COVERAGE_SCHEMA_VERSION),
    ):
        object.__setattr__(plan, name, value)
    plan._seal()
    return plan


def _payload_digest(value: object) -> str:
    return _sha256(_PAYLOAD_DIGEST_DOMAIN, value, maximum=MAX_PROPOSAL_COVERAGE_RECORD_BYTES)


def _evidence_claims(
    proposal: ReconstructionProposal,
) -> tuple[dict[str, tuple[str, ...]], set[str]]:
    claims_by_evidence = {
        binding.evidence_id: binding.claim_ids for binding in proposal.evidence_bindings
    }
    return claims_by_evidence, set(claims_by_evidence)


def _requirement(
    *,
    claims_by_evidence: Mapping[str, tuple[str, ...]],
    consumer_path: str,
    consumer_id: str,
    kind: CoverageConsumerKind,
    mode: CoverageMode,
    payload: object,
    evidence_ids: tuple[str, ...] = (),
    dependency_paths: tuple[str, ...] = (),
) -> ConsumerRequirement:
    claim_ids: set[str] = set()
    if mode is CoverageMode.EVIDENCE_REQUIRED:
        if not evidence_ids:
            _fail(ProposalCoverageErrorCode.MISSING_COVERAGE, consumer_path)
        for evidence_id in evidence_ids:
            claims = claims_by_evidence.get(evidence_id)
            if claims is None:
                _fail(ProposalCoverageErrorCode.INTEGRITY_FAILURE, consumer_path)
            claim_ids.update(claims)
    return ConsumerRequirement(
        consumer_path=consumer_path,
        consumer_id=consumer_id,
        kind=kind,
        mode=mode,
        payload_sha256=_payload_digest(payload),
        evidence_ids=tuple(evidence_ids),
        claim_ids=tuple(sorted(claim_ids)),
        dependency_paths=dependency_paths,
    )


def _point(geometry: SketchGeometry, prefix: str) -> tuple[float, float]:
    return (
        float(geometry.dimensions[f"x{prefix}_mm"]),
        float(geometry.dimensions[f"y{prefix}_mm"]),
    )


def _is_rectangle(geometries: tuple[SketchGeometry, ...]) -> bool:
    if len(geometries) != 4 or any(
        item.kind is not GeometryKind.LINE or item.construction for item in geometries
    ):
        return False
    edges = tuple((_point(item, "1"), _point(item, "2")) for item in geometries)
    counts = Counter(point for edge in edges for point in edge)
    if len(counts) != 4 or set(counts.values()) != {2}:
        return False
    adjacency: dict[tuple[float, float], set[tuple[float, float]]] = {
        point: set() for point in counts
    }
    for first, second in edges:
        adjacency[first].add(second)
        adjacency[second].add(first)
    if any(len(neighbours) != 2 for neighbours in adjacency.values()):
        return False
    start = min(adjacency)
    ordered = [start]
    previous: tuple[float, float] | None = None
    current = start
    for _ in range(3):
        candidates = sorted(item for item in adjacency[current] if item != previous)
        if not candidates:
            return False
        following = candidates[0]
        ordered.append(following)
        previous, current = current, following
    if start not in adjacency[current] or len(set(ordered)) != 4:
        return False
    vectors = tuple(
        (
            ordered[(index + 1) % 4][0] - ordered[index][0],
            ordered[(index + 1) % 4][1] - ordered[index][1],
        )
        for index in range(4)
    )
    lengths = tuple(math.hypot(*vector) for vector in vectors)
    if any(length <= 0.0 or not math.isfinite(length) for length in lengths):
        return False
    for index in range(4):
        dot = (
            vectors[index][0] * vectors[(index + 1) % 4][0]
            + vectors[index][1] * vectors[(index + 1) % 4][1]
        )
        if not math.isclose(dot, 0.0, rel_tol=1e-9, abs_tol=1e-9):
            return False
    return math.isclose(lengths[0], lengths[2], rel_tol=1e-9, abs_tol=1e-9) and math.isclose(
        lengths[1], lengths[3], rel_tol=1e-9, abs_tol=1e-9
    )


_SUPPORTED_CONSTRAINTS = frozenset(
    {
        ConstraintKind.COINCIDENT,
        ConstraintKind.HORIZONTAL,
        ConstraintKind.VERTICAL,
        ConstraintKind.PARALLEL,
        ConstraintKind.PERPENDICULAR,
        ConstraintKind.EQUAL,
        ConstraintKind.SYMMETRIC,
        ConstraintKind.DISTANCE,
        ConstraintKind.DISTANCE_X,
        ConstraintKind.DISTANCE_Y,
        ConstraintKind.LENGTH,
        ConstraintKind.RADIUS,
        ConstraintKind.DIAMETER,
    }
)


def _check_sketches(design: ParametricDesignIR) -> tuple[ParametricSketch, ParametricSketch | None]:
    if not 1 <= len(design.sketches) <= 2:
        _fail(ProposalCoverageErrorCode.OUT_OF_ENVELOPE, "/design/sketches")
    profile = tuple(item for item in design.sketches if item.role is SketchRole.PROFILE)
    locations = tuple(item for item in design.sketches if item.role is SketchRole.HOLE_LOCATIONS)
    if (
        len(profile) != 1
        or len(locations) > 1
        or len(profile) + len(locations) != len(design.sketches)
    ):
        _fail(ProposalCoverageErrorCode.OUT_OF_ENVELOPE, "/design/sketches")
    for index, sketch in enumerate(design.sketches):
        if (
            sketch.plane.kind is not PlaneKind.ORIGIN
            or sketch.plane.origin is not OriginPlane.XY
            or sketch.plane.datum_id is not None
        ):
            _fail(
                ProposalCoverageErrorCode.OUT_OF_ENVELOPE,
                f"/design/sketches/{index}/plane",
            )
        if any(item.kind not in _SUPPORTED_CONSTRAINTS for item in sketch.constraints):
            _fail(
                ProposalCoverageErrorCode.OUT_OF_ENVELOPE,
                f"/design/sketches/{index}/constraints",
            )
    if not _is_rectangle(profile[0].geometries):
        _fail(ProposalCoverageErrorCode.OUT_OF_ENVELOPE, "/design/sketches")
    location = locations[0] if locations else None
    if location is not None:
        if not 1 <= len(location.geometries) <= MAX_FIRST_SLICE_HOLES or any(
            item.kind is not GeometryKind.CIRCLE or item.construction
            for item in location.geometries
        ):
            _fail(ProposalCoverageErrorCode.OUT_OF_ENVELOPE, "/design/sketches")
        centers = tuple(
            (item.dimensions["cx_mm"], item.dimensions["cy_mm"]) for item in location.geometries
        )
        if len(set(centers)) != len(centers):
            _fail(ProposalCoverageErrorCode.OUT_OF_ENVELOPE, "/design/sketches")
    return profile[0], location


def _check_features(
    design: ParametricDesignIR,
    *,
    profile: ParametricSketch,
    locations: ParametricSketch | None,
) -> None:
    expected_count = 1 if locations is None else 2
    if len(design.features) != expected_count:
        _fail(ProposalCoverageErrorCode.OUT_OF_ENVELOPE, "/design/features")
    pad = design.features[0]
    if (
        pad.kind is not FeatureKind.PAD
        or pad.sketch_id != profile.id
        or pad.base_feature_id is not None
        or set(pad.parameters) != {"length"}
        or pad.extent is not FeatureExtent.LENGTH
        or pad.axis is not None
        or pad.location_geometry_ids
        or pad.reversed
        or pad.symmetric
    ):
        _fail(ProposalCoverageErrorCode.OUT_OF_ENVELOPE, "/design/features/0")
    if locations is None:
        return
    hole = design.features[1]
    if (
        hole.kind is not FeatureKind.HOLE
        or hole.sketch_id != locations.id
        or hole.base_feature_id != pad.id
        or set(hole.parameters) != {"diameter"}
        or hole.extent is not FeatureExtent.THROUGH_ALL
        or hole.axis is not None
        or set(hole.location_geometry_ids) != {item.id for item in locations.geometries}
        # The verified origin-XY compiler/guided-photo contract cuts through
        # the positive Pad by reversing the Hole direction.
        or not hole.reversed
        or hole.symmetric
    ):
        _fail(ProposalCoverageErrorCode.OUT_OF_ENVELOPE, "/design/features/1")


def _check_parameters(design: ParametricDesignIR) -> None:
    consumed = {
        parameter_id for feature in design.features for parameter_id in feature.parameters.values()
    }
    consumed.update(
        constraint.parameter_id
        for sketch in design.sketches
        for constraint in sketch.constraints
        if constraint.parameter_id is not None
    )
    if consumed != {item.id for item in design.parameters}:
        _fail(ProposalCoverageErrorCode.OUT_OF_ENVELOPE, "/design/parameters")
    for index, parameter in enumerate(design.parameters):
        if parameter.unit is not DesignUnit.MM or parameter.expression is not None:
            _fail(
                ProposalCoverageErrorCode.OUT_OF_ENVELOPE,
                f"/design/parameters/{index}",
            )


def _consumer_paths(
    design: ParametricDesignIR,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    parameter_paths = {
        item.id: f"/design/parameters/{index}" for index, item in enumerate(design.parameters)
    }
    geometry_paths = {
        geometry.id: f"/design/sketches/{sketch_index}/geometries/{geometry_index}"
        for sketch_index, sketch in enumerate(design.sketches)
        for geometry_index, geometry in enumerate(sketch.geometries)
    }
    feature_paths = {
        item.id: f"/design/features/{index}" for index, item in enumerate(design.features)
    }
    return parameter_paths, geometry_paths, feature_paths


def _constraint_dependencies(
    constraint: object,
    *,
    parameter_paths: Mapping[str, str],
    geometry_paths: Mapping[str, str],
) -> tuple[str, ...]:
    dependencies = {
        geometry_paths[reference.target]
        for reference in constraint.references
        if not reference.target.startswith("@")
    }
    if constraint.parameter_id is not None:
        dependencies.add(parameter_paths[constraint.parameter_id])
    return tuple(sorted(dependencies))


def derive_proposal_coverage_plan(*, proposal: ReconstructionProposal) -> ProposalCoveragePlan:
    """Derive every first-slice consumer without accepting caller requirements.

    A returned plan is only a frozen checklist.  It is not evidence that any
    image, calibration, fit, clarification, CAD candidate, or Task is valid.
    """

    if type(proposal) is not ReconstructionProposal:
        _fail(ProposalCoverageErrorCode.INVALID_INPUT, "/proposal")
    design = proposal.design
    if type(design) is not ParametricDesignIR:
        _fail(ProposalCoverageErrorCode.INVALID_INPUT, "/proposal/design")
    if design.datum_planes:
        _fail(ProposalCoverageErrorCode.OUT_OF_ENVELOPE, "/design/datum_planes")
    if design.edge_treatments:
        _fail(ProposalCoverageErrorCode.OUT_OF_ENVELOPE, "/design/edge_treatments")
    profile, locations = _check_sketches(design)
    _check_features(design, profile=profile, locations=locations)
    _check_parameters(design)

    claims_by_evidence, declared_evidence_ids = _evidence_claims(proposal)
    requirements: list[ConsumerRequirement] = []
    direct_evidence_references: list[str] = []
    parameter_paths, geometry_paths, feature_paths = _consumer_paths(design)

    root_path = "/design"
    requirements.append(
        _requirement(
            claims_by_evidence=claims_by_evidence,
            consumer_path=root_path,
            consumer_id=design.id,
            kind=CoverageConsumerKind.DESIGN_ROOT,
            mode=CoverageMode.FIXED_POLICY,
            payload={
                "schema_version": design.schema_version,
                "id": design.id,
                "name": design.name,
                "units": design.units.to_mapping(),
                "body": design.body.to_mapping(),
                "parameter_ids": [item.id for item in design.parameters],
                "sketch_ids": [item.id for item in design.sketches],
                "feature_ids": [item.id for item in design.features],
                "edge_treatment_ids": [],
            },
        )
    )

    for parameter in design.parameters:
        path = parameter_paths[parameter.id]
        direct_evidence_references.extend(parameter.evidence_ids)
        requirements.append(
            _requirement(
                claims_by_evidence=claims_by_evidence,
                consumer_path=path,
                consumer_id=parameter.id,
                kind=CoverageConsumerKind.PARAMETER,
                mode=CoverageMode.EVIDENCE_REQUIRED,
                payload=parameter.to_mapping(),
                evidence_ids=parameter.evidence_ids,
            )
        )

    constraint_paths: dict[str, str] = {}
    for sketch_index, sketch in enumerate(design.sketches):
        sketch_path = f"/design/sketches/{sketch_index}"
        for constraint_index, constraint in enumerate(sketch.constraints):
            constraint_paths[constraint.id] = f"{sketch_path}/constraints/{constraint_index}"
        geometry_dependencies = tuple(geometry_paths[item.id] for item in sketch.geometries)
        constraint_dependencies = tuple(constraint_paths[item.id] for item in sketch.constraints)
        direct_evidence_references.extend(sketch.evidence_ids)
        requirements.append(
            _requirement(
                claims_by_evidence=claims_by_evidence,
                consumer_path=sketch_path,
                consumer_id=sketch.id,
                kind=CoverageConsumerKind.SKETCH,
                mode=CoverageMode.EVIDENCE_REQUIRED,
                payload={
                    "schema_version": sketch.schema_version,
                    "id": sketch.id,
                    "name": sketch.name,
                    "role": sketch.role.value,
                    "plane": sketch.plane.to_mapping(),
                    "geometry_ids": [item.id for item in sketch.geometries],
                    "constraint_ids": [item.id for item in sketch.constraints],
                    "evidence_ids": list(sketch.evidence_ids),
                },
                evidence_ids=sketch.evidence_ids,
                dependency_paths=geometry_dependencies + constraint_dependencies,
            )
        )
        for geometry in sketch.geometries:
            path = geometry_paths[geometry.id]
            direct_evidence_references.extend(geometry.evidence_ids)
            requirements.append(
                _requirement(
                    claims_by_evidence=claims_by_evidence,
                    consumer_path=path,
                    consumer_id=geometry.id,
                    kind=CoverageConsumerKind.GEOMETRY,
                    mode=CoverageMode.EVIDENCE_REQUIRED,
                    payload=geometry.to_mapping(),
                    evidence_ids=geometry.evidence_ids,
                )
            )
        for constraint in sketch.constraints:
            path = constraint_paths[constraint.id]
            dependencies = _constraint_dependencies(
                constraint,
                parameter_paths=parameter_paths,
                geometry_paths=geometry_paths,
            )
            mode = (
                CoverageMode.EVIDENCE_REQUIRED if constraint.evidence_ids else CoverageMode.DERIVED
            )
            direct_evidence_references.extend(constraint.evidence_ids)
            requirements.append(
                _requirement(
                    claims_by_evidence=claims_by_evidence,
                    consumer_path=path,
                    consumer_id=constraint.id,
                    kind=CoverageConsumerKind.CONSTRAINT,
                    mode=mode,
                    payload=constraint.to_mapping(),
                    evidence_ids=constraint.evidence_ids,
                    dependency_paths=dependencies,
                )
            )

    for feature in design.features:
        path = feature_paths[feature.id]
        dependencies = {parameter_paths[item] for item in feature.parameters.values()}
        if feature.sketch_id is not None:
            sketch_index = next(
                index for index, item in enumerate(design.sketches) if item.id == feature.sketch_id
            )
            dependencies.add(f"/design/sketches/{sketch_index}")
        if feature.base_feature_id is not None:
            dependencies.add(feature_paths[feature.base_feature_id])
        direct_evidence_references.extend(feature.evidence_ids)
        requirements.append(
            _requirement(
                claims_by_evidence=claims_by_evidence,
                consumer_path=path,
                consumer_id=feature.id,
                kind=CoverageConsumerKind.FEATURE,
                mode=CoverageMode.EVIDENCE_REQUIRED,
                payload=feature.to_mapping(),
                evidence_ids=feature.evidence_ids,
                dependency_paths=tuple(sorted(dependencies)),
            )
        )

    evidence_counts = Counter(direct_evidence_references)
    shared = {item for item, count in evidence_counts.items() if count > 1}
    if shared:
        _fail(ProposalCoverageErrorCode.AMBIGUOUS_COVERAGE, "/design/evidence")
    claim_counts = Counter(
        claim_id
        for requirement in requirements
        if requirement.mode is CoverageMode.EVIDENCE_REQUIRED
        for claim_id in requirement.claim_ids
    )
    if any(count > 1 for count in claim_counts.values()):
        _fail(ProposalCoverageErrorCode.AMBIGUOUS_COVERAGE, "/observation/claims")
    referenced = set(evidence_counts)
    if referenced != declared_evidence_ids:
        missing = declared_evidence_ids - referenced
        code = (
            ProposalCoverageErrorCode.ORPHAN_EVIDENCE
            if missing
            else ProposalCoverageErrorCode.INTEGRITY_FAILURE
        )
        _fail(
            code,
            "/design/evidence",
        )
    consumed_claim_ids = set(claim_counts)
    declared_claim_ids = {claim.id for claim in proposal.observation.claims}
    if consumed_claim_ids != declared_claim_ids:
        code = (
            ProposalCoverageErrorCode.ORPHAN_CLAIM
            if declared_claim_ids - consumed_claim_ids
            else ProposalCoverageErrorCode.INTEGRITY_FAILURE
        )
        _fail(code, "/observation/claims")

    operation = ModelCommand(
        id="visual-adoption-create-design",
        op="create_parametric_design",
        args={"design": design.to_mapping()},
        source=ValueSource.MODEL,
    ).to_mapping()
    operation_sha256 = _sha256(
        _OPERATION_DIGEST_DOMAIN,
        operation,
        maximum=MAX_PROPOSAL_COVERAGE_RECORD_BYTES,
    )
    requirements.append(
        _requirement(
            claims_by_evidence=claims_by_evidence,
            consumer_path="/program/operations/0",
            consumer_id="visual-adoption-create-design",
            kind=CoverageConsumerKind.PROGRAM_OPERATION,
            mode=CoverageMode.PROGRAM_BINDING,
            payload=operation,
            # The operation payload itself contains the complete canonical
            # design.  A single dependency on the design root avoids a second
            # unbounded fan-out while still preventing a detached operation.
            dependency_paths=(root_path,),
        )
    )
    if len(requirements) > MAX_FIRST_SLICE_CONSUMERS:
        _fail(ProposalCoverageErrorCode.BUDGET_EXCEEDED, "/requirements")
    return _new_proposal_coverage_plan(
        proposal_id=proposal.id,
        proposal_digest=proposal.digest,
        design_digest=proposal.design_digest,
        observation_digest=proposal.observation.digest,
        acceptance_digest=proposal.acceptance_digest,
        expected_operation_payload_sha256=operation_sha256,
        requirements=tuple(requirements),
    )


__all__ = [
    "MAX_FIRST_SLICE_CONSUMERS",
    "MAX_FIRST_SLICE_HOLES",
    "MAX_PROPOSAL_COVERAGE_RECORD_BYTES",
    "PROPOSAL_COVERAGE_SCHEMA_VERSION",
    "ConsumerRequirement",
    "CoverageConsumerKind",
    "CoverageMode",
    "ProposalCoverageError",
    "ProposalCoverageErrorCode",
    "ProposalCoveragePlan",
    "derive_proposal_coverage_plan",
]
