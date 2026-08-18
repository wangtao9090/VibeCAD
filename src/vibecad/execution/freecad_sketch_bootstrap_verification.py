"""Private managed verification for the current reviewed Sketch CREATE family.

The bootstrap family is part of the current formal catalog and reviewed-family
registry, but intentionally remains outside the public product route.  This
module supplies one admitted ``REVIEWED_HOST`` case manifest, a same-process
managed-FreeCAD executor, and the inert current formal specification.

Callers can provide only the authenticated FreeCAD module.  The executor,
case results, challenges, pass decision, receipt, and promotion binding are
constructed internally.  Returned evidence is ephemeral: this module neither
persists it nor changes any capability to ``VERIFIED``.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from vibecad.execution import freecad_reviewed_verification as verification
from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
)
from vibecad.execution.freecad_capability_projection_v2 import (
    FreeCadPromotionVerificationBinding,
)
from vibecad.execution.freecad_intent_capabilities import FreeCadIntentCapabilitySpec
from vibecad.execution.freecad_reviewed_family_capabilities import (
    build_reviewed_family_capability_specs,
)
from vibecad.execution.freecad_reviewed_verification import (
    REVIEWED_VERIFICATION_SCHEMA_VERSION,
    ReviewedConformanceCase,
    ReviewedConformanceCaseManifest,
    ReviewedConformanceFacet,
    ReviewedVerificationReceipt,
    build_managed_freecad_conformance_host,
    build_promotion_verification_binding,
    build_reviewed_verification_receipt,
)
from vibecad.execution.freecad_sketch_bootstrap_reviewed_execution import (
    execute_sketch_bootstrap_reviewed_plan_with_sources,
)
from vibecad.intent_bridge.contracts import (
    BackendLoweringRequest,
    BridgeBudget,
    BridgeDisposition,
    BridgeTermRef,
    DocumentRef,
    ProducerBinding,
    ProducerDescriptor,
    ProofAssertion,
    ProofBundle,
    ProofEndpoint,
    SubjectRef,
)
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_sketch_bootstrap_adapter import (
    SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM,
    SKETCH_BOOTSTRAP_FAMILY_MANIFEST,
    SKETCH_BOOTSTRAP_OPERATION_SPEC,
    build_sketch_bootstrap_intent_graph,
    sketch_bootstrap_reviewed_adapter_factory,
    validate_sketch_bootstrap_reviewed_plan,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    ParametricFeatureGraphV2Codec,
)
from vibecad.intent_bridge.ports import TrustedCodecRegistry
from vibecad.intent_bridge.reviewed_family_engine import ReviewedPlanReceipt
from vibecad.intent_bridge.trusted_proof_policy import (
    RuleEndpointSignature,
    TrustedRuleEvaluation,
    TrustedRuleEvaluatorDescriptor,
    TrustedRulePolicy,
)
from vibecad.parametric import freecad_sketch_bootstrap_rules as bootstrap_rules
from vibecad.parametric.freecad_sketch_bootstrap_rules import (
    SKETCH_BOOTSTRAP_NATIVE_TYPE_ID,
    SketchBootstrapBackendPlan,
    SketchBootstrapExecutionBindings,
    SketchBootstrapRuleError,
    apply_sketch_bootstrap_plan,
)

SKETCH_BOOTSTRAP_VERIFIER_ID: Final = "vcad.managed.freecad.sketch-bootstrap-conformance"
SKETCH_BOOTSTRAP_VERIFIER_VERSION: Final = "1.0.0"

_CASE_CONTRACT_DOMAIN = b"vibecad-sketch-bootstrap-case-contract-v1\0"
_FIXTURE_DIGEST_DOMAIN = b"vibecad-sketch-bootstrap-fixture-v1\0"
_HARNESS_CONTRACT_DOMAIN = b"vibecad-sketch-bootstrap-harness-contract-v1\0"
_OBSERVATION_DOMAIN = b"vibecad-sketch-bootstrap-observation-v1\0"
_PROOF_TERM_DOMAIN = b"vibecad-sketch-bootstrap-proof-term-v1\0"
_PROOF_EVALUATOR_DOMAIN = b"vibecad-sketch-bootstrap-proof-evaluator-v1\0"
_VERIFICATION_LOCK = threading.Lock()

_FACET_CONTRACTS: Final = {
    ReviewedConformanceFacet.CREATE: (
        "exact-adapter-plan-creates-body-origin7-and-body-owned-closed-circle"
    ),
    ReviewedConformanceFacet.EDIT: (
        "native-circle-center-edit-preserves-body-ownership-and-closed-profile"
    ),
    ReviewedConformanceFacet.RECOMPUTE: (
        "explicit-recompute-preserves-primary-state-shape-geometry-constraint-digests"
    ),
    ReviewedConformanceFacet.SAVE: "managed-fcstd-save-is-nonempty",
    ReviewedConformanceFacet.REOPEN: (
        "saved-body-origin7-sketch-closure-reopens-with-exact-primary-digests"
    ),
    ReviewedConformanceFacet.NEGATIVE: ("tampered-plan-is-rejected-before-document-mutation"),
    ReviewedConformanceFacet.LATE_ROLLBACK: (
        "post-native-create-failure-restores-sequence-group-tip-and-visibility"
    ),
}


def _fail(path: str) -> None:
    raise CapabilityCatalogError(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)


def _canonical(value: object, *, maximum: int = 64 * 1024) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail("sketch_bootstrap_verification/canonical")
    if not raw or len(raw) > maximum:
        _fail("sketch_bootstrap_verification/canonical")
    return raw


def _sha(domain: bytes, value: bytes | str) -> str:
    raw = value if type(value) is bytes else value.encode("utf-8")
    return hashlib.sha256(domain + raw).hexdigest()


def _pfg_identity(term: object) -> tuple[str, str, str, str]:
    try:
        return (
            term.namespace,
            term.vocabulary_version,
            term.term_id,
            term.term_definition_sha256,
        )
    except AttributeError:
        _fail("sketch_bootstrap_verification/term_identity")


_HARNESS_CONTRACT_SHA256: Final = _sha(
    _HARNESS_CONTRACT_DOMAIN,
    _canonical(
        {
            "schema_version": 1,
            "verifier": {
                "id": SKETCH_BOOTSTRAP_VERIFIER_ID,
                "version": SKETCH_BOOTSTRAP_VERIFIER_VERSION,
            },
            "execution": {
                "caller_callback_input": False,
                "caller_pass_input": False,
                "caller_result_input": False,
                "exact_adapter_lower_with_receipt": True,
                "native_rule_execution": True,
                "same_process_managed_freecad": True,
                "documents_closed_between_host_cases": True,
                "receipt_persistence": False,
            },
            "facets": {facet.value: value for facet, value in _FACET_CONTRACTS.items()},
        }
    ),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchBootstrapVerificationFixture:
    """Content-bound recipe for the sole CREATE semantic."""

    family_id: str
    operation_id: str
    recipe_bytes: bytes
    fixture_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            self.family_id != SKETCH_BOOTSTRAP_FAMILY_MANIFEST.family_id
            or self.operation_id != SKETCH_BOOTSTRAP_OPERATION_SPEC.operation_id
            or type(self.recipe_bytes) is not bytes
            or not self.recipe_bytes
            or len(self.recipe_bytes) > 16 * 1024
        ):
            _fail("sketch_bootstrap_verification/fixture")
        try:
            decoded = json.loads(self.recipe_bytes)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            _fail("sketch_bootstrap_verification/fixture")
        if _canonical(decoded, maximum=16 * 1024) != self.recipe_bytes:
            _fail("sketch_bootstrap_verification/fixture")
        object.__setattr__(
            self,
            "fixture_sha256",
            _sha(
                _FIXTURE_DIGEST_DOMAIN,
                _canonical(
                    {
                        "family_id": self.family_id,
                        "operation_id": self.operation_id,
                        "recipe": decoded,
                    },
                    maximum=20 * 1024,
                ),
            ),
        )


SKETCH_BOOTSTRAP_VERIFICATION_FIXTURE: Final = SketchBootstrapVerificationFixture(
    family_id=SKETCH_BOOTSTRAP_FAMILY_MANIFEST.family_id,
    operation_id=SKETCH_BOOTSTRAP_OPERATION_SPEC.operation_id,
    recipe_bytes=_canonical(
        {
            "lifecycle": "create",
            "product_sources": 0,
            "owner": "PartDesign::Body",
            "support": "semantic-origin-xy-plane",
            "profile": {
                "kind": "circle",
                "center_mm": [0.0, 0.0],
                "radius_mm": 10.0,
                "closed": True,
            },
            "closure": ["Body", "Origin", "OriginFeature*7", "Sketch"],
            "primary": "body-owned Sketcher::SketchObject",
        },
        maximum=16 * 1024,
    ),
)


def _build_case_manifest() -> ReviewedConformanceCaseManifest:
    cases: list[ReviewedConformanceCase] = []
    for facet in ReviewedConformanceFacet:
        contract = _canonical(
            {
                "facet": facet.value,
                "facet_contract": _FACET_CONTRACTS[facet],
                "family_manifest_sha256": SKETCH_BOOTSTRAP_FAMILY_MANIFEST.manifest_sha256,
                "fixture_sha256": SKETCH_BOOTSTRAP_VERIFICATION_FIXTURE.fixture_sha256,
                "harness_contract_sha256": _HARNESS_CONTRACT_SHA256,
                "operation_id": SKETCH_BOOTSTRAP_OPERATION_SPEC.operation_id,
                "operation_specification_sha256": (
                    SKETCH_BOOTSTRAP_OPERATION_SPEC.specification_sha256
                ),
                "schema_version": REVIEWED_VERIFICATION_SCHEMA_VERSION,
            }
        )
        cases.append(
            ReviewedConformanceCase(
                case_id=(
                    f"sketch_bootstrap.{SKETCH_BOOTSTRAP_OPERATION_SPEC.operation_id}.{facet.value}"
                ),
                operation_id=SKETCH_BOOTSTRAP_OPERATION_SPEC.operation_id,
                operation_specification_sha256=(
                    SKETCH_BOOTSTRAP_OPERATION_SPEC.specification_sha256
                ),
                facet=facet,
                case_contract_sha256=_sha(_CASE_CONTRACT_DOMAIN, contract),
            )
        )
    return verification._admit_reviewed_host_conformance_case_manifest(  # noqa: SLF001
        manifest=SKETCH_BOOTSTRAP_FAMILY_MANIFEST,
        cases=tuple(cases),
    )


SKETCH_BOOTSTRAP_REVIEWED_HOST_CASE_MANIFEST: Final = _build_case_manifest()

# The candidate names remain stable for the verification-to-catalog handoff;
# their values now equal the current family and formal catalog entries.
SKETCH_BOOTSTRAP_CANDIDATE_FAMILY_MANIFEST: Final = SKETCH_BOOTSTRAP_FAMILY_MANIFEST
SKETCH_BOOTSTRAP_CANDIDATE_FORMAL_SPEC: Final[FreeCadIntentCapabilitySpec] = (
    build_reviewed_family_capability_specs((SKETCH_BOOTSTRAP_FAMILY_MANIFEST,))[0]
)


@dataclass(frozen=True, slots=True)
class SketchBootstrapFormalVerificationHandoff:
    """Catalog126/family21 handoff with release evidence still pending."""

    family_manifest_sha256: str
    case_manifest_sha256: str
    candidate_operation_id: str
    future_formal_operation_count: int = 126
    future_reviewed_family_count: int = 21
    current_catalog_registered: bool = True
    current_family_registered: bool = True
    release_attestation_refreshed: bool = False

    @property
    def defaults_to_verified(self) -> bool:
        return False


SKETCH_BOOTSTRAP_FORMAL_VERIFICATION_HANDOFF: Final = SketchBootstrapFormalVerificationHandoff(
    family_manifest_sha256=SKETCH_BOOTSTRAP_FAMILY_MANIFEST.manifest_sha256,
    case_manifest_sha256=(SKETCH_BOOTSTRAP_REVIEWED_HOST_CASE_MANIFEST.case_manifest_sha256),
    candidate_operation_id=SKETCH_BOOTSTRAP_CANDIDATE_FORMAL_SPEC.operation_id,
)


def _proof_term(term_ref_id: str, term_id: str) -> BridgeTermRef:
    namespace = "org.vibecad.freecad-sketch-bootstrap-verification"
    version = "1.0.0"
    definition = hashlib.sha256(
        b"\0".join(
            (
                _PROOF_TERM_DOMAIN,
                namespace.encode("ascii"),
                version.encode("ascii"),
                term_id.encode("ascii"),
            )
        )
    ).hexdigest()
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace=namespace,
        vocabulary_version=version,
        term_id=term_id,
        term_definition_sha256=definition,
    )


_PROOF_RULE_TERM: Final = _proof_term(
    "rule_sketch_bootstrap_verified_shape",
    "rule.exact-reviewed-sketch-bootstrap-shape",
)
_PROOF_PREDICATE_TERM: Final = _proof_term(
    "predicate_sketch_bootstrap_exact",
    "predicate.exact-reviewed-shape",
)
_PROOF_PREMISE_ROLE_TERM: Final = _proof_term(
    "role_sketch_bootstrap_candidate",
    "proof-role.canonical-candidate",
)
_PROOF_CONCLUSION_ROLE_TERM: Final = _proof_term(
    "role_sketch_bootstrap_admissible",
    "proof-role.exact-lowering-admissible",
)
_PROOF_EVALUATOR_CONTRACT_SHA256: Final = _sha(
    _PROOF_EVALUATOR_DOMAIN,
    _canonical(
        {
            "family_manifest_sha256": SKETCH_BOOTSTRAP_FAMILY_MANIFEST.manifest_sha256,
            "operation_specification_sha256": (
                SKETCH_BOOTSTRAP_OPERATION_SPEC.specification_sha256
            ),
            "selector": list(PFG_SELECTOR_FEATURE_NODE.semantic_identity),
            "subject_type": list(_pfg_identity(SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM)),
            "canonical_graph_only": True,
        }
    ),
)


class _ExactBootstrapShapeEvaluator:
    __slots__ = ("_descriptor",)

    def __init__(self) -> None:
        endpoint = {
            "selector_kind_term": PFG_SELECTOR_FEATURE_NODE,
            "subject_type_term": BridgeTermRef(
                term_ref_id=SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM.term_ref_id,
                namespace=SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM.namespace,
                vocabulary_version=SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM.vocabulary_version,
                term_id=SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM.term_id,
                term_definition_sha256=(
                    SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM.term_definition_sha256
                ),
            ),
        }
        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="sketch_bootstrap_exact_shape_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_PROOF_EVALUATOR_CONTRACT_SHA256,
            rule_term=_PROOF_RULE_TERM,
            predicate_term=_PROOF_PREDICATE_TERM,
            premises=(
                RuleEndpointSignature(
                    role_term=_PROOF_PREMISE_ROLE_TERM,
                    **endpoint,
                ),
            ),
            conclusions=(
                RuleEndpointSignature(
                    role_term=_PROOF_CONCLUSION_ROLE_TERM,
                    **endpoint,
                ),
            ),
        )

    @property
    def descriptor(self) -> TrustedRuleEvaluatorDescriptor:
        return self._descriptor

    def validate(self, evaluation: TrustedRuleEvaluation) -> None:
        expected_graph = build_sketch_bootstrap_intent_graph()
        expected_subject = SubjectRef(
            artifact_id="artifact_sketch_bootstrap_verification_intent",
            selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
            selector_id="node_sketch_bootstrap",
        )
        if (
            type(evaluation) is not TrustedRuleEvaluation
            or len(evaluation.documents) != 1
            or evaluation.documents[0].payload != expected_graph.canonical_bytes
            or len(evaluation.premises) != 1
            or len(evaluation.conclusions) != 1
            or evaluation.premises[0].subject != expected_subject
            or evaluation.conclusions[0].subject != expected_subject
            or evaluation.premises[0].semantic_type.semantic_identity
            != _pfg_identity(SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM)
            or evaluation.conclusions[0].semantic_type.semantic_identity
            != _pfg_identity(SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM)
        ):
            _fail("sketch_bootstrap_verification/proof_evaluator")


class _MemoryArtifacts:
    __slots__ = ("_payloads",)

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self._payloads = dict(payloads)

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        try:
            payload = self._payloads[document.artifact_id]
        except (AttributeError, KeyError):
            _fail("sketch_bootstrap_verification/artifacts")
        if len(payload) > maximum_bytes:
            _fail("sketch_bootstrap_verification/artifacts")
        return payload


class _MemoryPlanSink:
    __slots__ = ("_document", "_payload")

    def __init__(self) -> None:
        self._document: DocumentRef | None = None
        self._payload: bytes | None = None

    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        if self._document is not None or self._payload is not None:
            _fail("sketch_bootstrap_verification/plan_sink")
        self._document = document
        self._payload = payload
        return payload

    def read_exact(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        if (
            document != self._document
            or self._payload is None
            or len(self._payload) > maximum_bytes
        ):
            _fail("sketch_bootstrap_verification/plan_sink")
        return self._payload


def _lower_exact_plan() -> tuple[
    SketchBootstrapBackendPlan,
    bytes,
    DocumentRef,
    ReviewedPlanReceipt,
]:
    """Exercise the public exact adapter API with an internal trusted proof."""

    graph = build_sketch_bootstrap_intent_graph()
    intent_payload = graph.canonical_bytes
    intent_document = DocumentRef(
        artifact_id="artifact_sketch_bootstrap_verification_intent",
        role_term_ref_id=SKETCH_BOOTSTRAP_FAMILY_MANIFEST.intent_role_term.term_ref_id,
        schema_term_ref_id=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM.term_ref_id,
        document_id=graph.graph_id,
        document_digest=graph.graph_sha256,
        content_sha256=hashlib.sha256(intent_payload).hexdigest(),
        size_bytes=len(intent_payload),
        media_type=SKETCH_BOOTSTRAP_FAMILY_MANIFEST.intent_media_type,
    )
    capability_document, capability_payload = SKETCH_BOOTSTRAP_FAMILY_MANIFEST.capability_document(
        artifact_id="artifact_sketch_bootstrap_verification_capability"
    )
    evaluator = _ExactBootstrapShapeEvaluator()
    policy = TrustedRulePolicy(evaluators=(evaluator,))
    subject = SubjectRef(
        artifact_id=intent_document.artifact_id,
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id="node_sketch_bootstrap",
    )
    proof = ProofBundle(
        terms=(
            *SKETCH_BOOTSTRAP_FAMILY_MANIFEST.request_terms,
            _PROOF_RULE_TERM,
            _PROOF_PREDICATE_TERM,
            _PROOF_PREMISE_ROLE_TERM,
            _PROOF_CONCLUSION_ROLE_TERM,
        ),
        documents=(intent_document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_exact_sketch_bootstrap_shape",
                predicate_term_ref_id=_PROOF_PREDICATE_TERM.term_ref_id,
                rule_term_ref_id=_PROOF_RULE_TERM.term_ref_id,
                premises=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=_PROOF_PREMISE_ROLE_TERM.term_ref_id,
                        subject=subject,
                    ),
                ),
                conclusions=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=_PROOF_CONCLUSION_ROLE_TERM.term_ref_id,
                        subject=subject,
                    ),
                ),
            ),
        ),
        producer=ProducerBinding(
            descriptor=ProducerDescriptor(
                producer_id="sketch_bootstrap_verification_producer",
                producer_version="1.0.0",
                producer_contract_sha256=_PROOF_EVALUATOR_CONTRACT_SHA256,
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha(_PROOF_EVALUATOR_DOMAIN, intent_payload),
        ),
    )
    request = BackendLoweringRequest(
        adapter=SKETCH_BOOTSTRAP_FAMILY_MANIFEST.adapter,
        terms=proof.terms,
        documents=(intent_document, capability_document),
        intent_artifact_ids=(intent_document.artifact_id,),
        capability_artifact_ids=(capability_document.artifact_id,),
        proof_bundle=proof,
        budget=BridgeBudget(
            max_input_bytes=len(intent_payload) + len(capability_payload),
            max_output_bytes=SKETCH_BOOTSTRAP_FAMILY_MANIFEST.max_plan_bytes,
            max_subject_lookups=1,
            max_rule_applications=1,
        ),
    )
    artifacts = _MemoryArtifacts(
        {
            intent_document.artifact_id: intent_payload,
            capability_document.artifact_id: capability_payload,
        }
    )
    sink: PlanSink = _MemoryPlanSink()
    adapter = sketch_bootstrap_reviewed_adapter_factory(sink)
    result, receipt = adapter.lower_with_receipt(
        request,
        artifacts=artifacts,
        codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
        proof_policy=policy,
    )
    plan, payload = adapter.read_plan(receipt)
    if (
        result.disposition is not BridgeDisposition.COMPLETE
        or result.plan_document != receipt.plan_document
        or type(plan) is not SketchBootstrapBackendPlan
        or type(payload) is not bytes
    ):
        _fail("sketch_bootstrap_verification/lowering")
    validate_sketch_bootstrap_reviewed_plan(plan, receipt, receipt.operation)
    return plan, payload, receipt.plan_document, receipt


def _same_object_sequence(left: object, right: tuple[object, ...]) -> bool:
    try:
        values = tuple(left)
    except BaseException:
        return False
    return len(values) == len(right) and all(
        actual is expected for actual, expected in zip(values, right, strict=True)
    )


def _edited_geometry_facts(sketch: object) -> tuple[dict[str, object], ...]:
    """Digest an edited circle without reasserting its CREATE-time center."""

    try:
        geometry = tuple(sketch.Geometry)
        item = geometry[0]
        center = item.Center
        axis = item.Axis
        facts = (
            {
                "type_id": str(item.TypeId),
                "center": [float(center.x), float(center.y), float(center.z)],
                "axis": [float(axis.x), float(axis.y), float(axis.z)],
                "radius_mm": float(item.Radius),
                "construction": bool(sketch.getConstruction(0)),
            },
        )
        if (
            int(sketch.GeometryCount) != 1
            or len(geometry) != 1
            or facts[0]["type_id"] != "Part::GeomCircle"
            or facts[0]["axis"] != [0.0, 0.0, 1.0]
            or facts[0]["radius_mm"] != bootstrap_rules.SKETCH_BOOTSTRAP_CIRCLE_RADIUS_MM
            or facts[0]["construction"] is not False
        ):
            raise ValueError
    except (AttributeError, IndexError, TypeError, ValueError, OverflowError):
        _fail("sketch_bootstrap_verification/edited_geometry")
    return facts


def _primary_digests(
    body: object,
    sketch: object,
    *,
    require_create_geometry: bool = True,
) -> dict[str, str]:
    try:
        origin = bootstrap_rules._origin_closure(body)  # noqa: SLF001
        return {
            "state_sha256": bootstrap_rules._state_digest(  # noqa: SLF001
                body, sketch, origin[4]
            ),
            "shape_sha256": bootstrap_rules._shape_sha256(sketch),  # noqa: SLF001
            "geometry_sha256": bootstrap_rules._canonical_digest(  # noqa: SLF001
                bootstrap_rules._GEOMETRY_DIGEST_DOMAIN,  # noqa: SLF001
                (
                    bootstrap_rules._native_geometry_facts(sketch)  # noqa: SLF001
                    if require_create_geometry
                    else _edited_geometry_facts(sketch)
                ),
            ),
            "constraint_sha256": bootstrap_rules._canonical_digest(  # noqa: SLF001
                bootstrap_rules._CONSTRAINT_DIGEST_DOMAIN,  # noqa: SLF001
                bootstrap_rules._constraint_facts(sketch),  # noqa: SLF001
            ),
        }
    except SketchBootstrapRuleError:
        raise
    except BaseException:
        _fail("sketch_bootstrap_verification/primary_digests")


def _primary_topology(body: object, sketch: object) -> dict[str, object]:
    try:
        wires = tuple(sketch.Shape.Wires)
        origin = bootstrap_rules._origin_closure(body)  # noqa: SLF001
        topology = {
            "body_owned": tuple(body.Group) == (sketch,),
            "body_tip": body.Tip is sketch,
            "origin_closure_count": len(origin),
            "geometry_count": int(sketch.GeometryCount),
            "constraint_count": int(sketch.ConstraintCount),
            "wire_count": len(wires),
            "wire_closed": len(wires) == 1 and bool(wires[0].isClosed()),
            "open_vertex_count": len(tuple(sketch.OpenVertices)),
            "native_type_id": sketch.TypeId,
        }
    except (AttributeError, TypeError, ValueError):
        _fail("sketch_bootstrap_verification/primary_topology")
    if topology != {
        "body_owned": True,
        "body_tip": True,
        "origin_closure_count": 8,
        "geometry_count": 1,
        "constraint_count": 0,
        "wire_count": 1,
        "wire_closed": True,
        "open_vertex_count": 0,
        "native_type_id": SKETCH_BOOTSTRAP_NATIVE_TYPE_ID,
    }:
        _fail("sketch_bootstrap_verification/primary_topology")
    return topology


def _close_owned_documents(freecad: object, owned: dict[str, object]) -> None:
    try:
        open_documents = freecad.listDocuments()
        for name, document in tuple(owned.items()):
            if open_documents.get(name) is document:
                freecad.closeDocument(name)
    except BaseException:
        _fail("sketch_bootstrap_verification/cleanup")


def _late_rollback(
    freecad: object,
    plan: SketchBootstrapBackendPlan,
    payload: bytes,
    plan_document: DocumentRef,
) -> dict[str, object]:
    owned: dict[str, object] = {}
    try:
        document = freecad.newDocument("VerifySketchBootstrapLateRollback")
        owned[document.Name] = document
        document.UndoMode = 1
        existing_body = document.addObject("PartDesign::Body", "ExistingBody")
        existing_body.Visibility = False
        before = tuple(document.Objects)
        before_group = tuple(existing_body.Group)
        before_tip = existing_body.Tip
        before_visibility = bool(existing_body.Visibility)
        original = bootstrap_rules._state_digest  # noqa: SLF001
        late_validation_reached = False

        def fail_after_native_create(*_args: object, **_kwargs: object) -> str:
            nonlocal late_validation_reached
            late_validation_reached = True
            raise SketchBootstrapRuleError(
                bootstrap_rules.SketchBootstrapRuleErrorCode.CONFORMANCE_FAILED,
                "/injected-late-state-digest",
            )

        bootstrap_rules._state_digest = fail_after_native_create  # noqa: SLF001
        try:
            apply_sketch_bootstrap_plan(
                payload,
                expected_content_sha256=plan_document.content_sha256,
                expected_plan_sha256=plan_document.document_digest,
                bindings=SketchBootstrapExecutionBindings(
                    document=document,
                    body_id=plan.body_id,
                ),
            )
        except SketchBootstrapRuleError:
            pass
        else:
            _fail("sketch_bootstrap_verification/late_rollback")
        finally:
            bootstrap_rules._state_digest = original  # noqa: SLF001
        if (
            not late_validation_reached
            or not _same_object_sequence(document.Objects, before)
            or tuple(existing_body.Group) != before_group
            or existing_body.Tip is not before_tip
            or bool(existing_body.Visibility) is not before_visibility
            or bool(document.HasPendingTransaction)
        ):
            _fail("sketch_bootstrap_verification/late_rollback")
        return {
            "injection_point": "state-digest-after-native-create",
            "native_mutation_reached": True,
            "object_sequence_restored": True,
            "body_group_restored": True,
            "body_tip_restored": True,
            "visibility_restored": True,
            "pending_transaction": False,
        }
    finally:
        _close_owned_documents(freecad, owned)


def _case_observation(
    *,
    case: ReviewedConformanceCase,
    challenge_sha256: str,
    evidence: dict[str, object],
) -> bytes:
    if (
        type(case) is not ReviewedConformanceCase
        or case.operation_id != SKETCH_BOOTSTRAP_OPERATION_SPEC.operation_id
        or type(challenge_sha256) is not str
        or len(challenge_sha256) != 64
        or any(character not in "0123456789abcdef" for character in challenge_sha256)
        or type(evidence) is not dict
    ):
        _fail("sketch_bootstrap_verification/observation")
    body = {
        "case_sha256": case.case_sha256,
        "challenge_sha256": challenge_sha256,
        "evidence": evidence,
    }
    return _canonical(
        {
            "authority": "none",
            "case_contract_sha256": case.case_contract_sha256,
            "case_sha256": case.case_sha256,
            "challenge_sha256": challenge_sha256,
            "evidence": evidence,
            "facet": case.facet.value,
            "fixture_sha256": SKETCH_BOOTSTRAP_VERIFICATION_FIXTURE.fixture_sha256,
            "harness_contract_sha256": _HARNESS_CONTRACT_SHA256,
            "observation_schema": 1,
            "observation_sha256": _sha(_OBSERVATION_DOMAIN, _canonical(body)),
            "operation_id": case.operation_id,
        }
    )


class _SketchBootstrapExecutor:
    __slots__ = ("_cache", "_freecad")

    def __init__(self, freecad: object) -> None:
        self._freecad = freecad
        self._cache: dict[ReviewedConformanceFacet, dict[str, object]] | None = None

    def __call__(self, case: ReviewedConformanceCase, challenge_sha256: str) -> bytes:
        if (
            type(case) is not ReviewedConformanceCase
            or case.operation_id != SKETCH_BOOTSTRAP_OPERATION_SPEC.operation_id
            or case not in SKETCH_BOOTSTRAP_REVIEWED_HOST_CASE_MANIFEST.cases
        ):
            _fail("sketch_bootstrap_verification/case")
        if self._cache is None:
            self._cache = self._run()
        evidence = self._cache.get(case.facet)
        if evidence is None:
            _fail("sketch_bootstrap_verification/case")
        return _case_observation(
            case=case,
            challenge_sha256=challenge_sha256,
            evidence=evidence,
        )

    def _run(self) -> dict[ReviewedConformanceFacet, dict[str, object]]:
        freecad = self._freecad
        owned: dict[str, object] = {}
        outcomes: dict[ReviewedConformanceFacet, dict[str, object]] = {}
        plan, payload, plan_document, plan_receipt = _lower_exact_plan()
        try:
            with tempfile.TemporaryDirectory(prefix="vibecad-sketch-bootstrap-") as temporary:
                document = freecad.newDocument("VerifySketchBootstrap")
                owned[document.Name] = document
                document.UndoMode = 1

                before_negative = tuple(document.Objects)
                try:
                    apply_sketch_bootstrap_plan(
                        payload + b" ",
                        expected_content_sha256=plan_document.content_sha256,
                        expected_plan_sha256=plan_document.document_digest,
                        bindings=SketchBootstrapExecutionBindings(
                            document=document,
                            body_id=plan.body_id,
                        ),
                    )
                except SketchBootstrapRuleError:
                    pass
                else:
                    _fail("sketch_bootstrap_verification/negative")
                if not _same_object_sequence(document.Objects, before_negative) or bool(
                    document.HasPendingTransaction
                ):
                    _fail("sketch_bootstrap_verification/negative")
                outcomes[ReviewedConformanceFacet.NEGATIVE] = {
                    "tamper": "append-noncanonical-byte",
                    "mutation_count": 0,
                    "pending_transaction": False,
                }

                execution = execute_sketch_bootstrap_reviewed_plan_with_sources(
                    document,
                    plan,
                    payload,
                    plan_document,
                    plan_receipt.operation,
                    (),
                )
                sketch = execution.object
                body = execution.receipt.body
                topology = _primary_topology(body, sketch)
                digests = _primary_digests(body, sketch)
                native_receipt = execution.receipt.native_receipt
                if (
                    len(tuple(document.Objects)) != 10
                    or tuple(item.Name for item in document.Objects) != native_receipt.closure_names
                    or digests
                    != {
                        "state_sha256": native_receipt.state_sha256,
                        "shape_sha256": native_receipt.shape_sha256,
                        "geometry_sha256": native_receipt.geometry_sha256,
                        "constraint_sha256": native_receipt.constraint_sha256,
                    }
                ):
                    _fail("sketch_bootstrap_verification/create")
                outcomes[ReviewedConformanceFacet.CREATE] = {
                    "plan_receipt_sha256": plan_receipt.receipt_sha256,
                    "native_receipt_sha256": native_receipt.receipt_sha256,
                    "closure_names": list(native_receipt.closure_names),
                    "topology": topology,
                    "primary_digests": digests,
                }

                document.recompute()
                recomputed = _primary_digests(body, sketch)
                if recomputed != digests or _primary_topology(body, sketch) != topology:
                    _fail("sketch_bootstrap_verification/recompute")
                outcomes[ReviewedConformanceFacet.RECOMPUTE] = {
                    "primary_digests": recomputed,
                    "topology_preserved": True,
                }

                path = Path(temporary) / "sketch-bootstrap.FCStd"
                document.saveAs(str(path))
                if not path.is_file() or path.stat().st_size <= 0:
                    _fail("sketch_bootstrap_verification/save")
                outcomes[ReviewedConformanceFacet.SAVE] = {
                    "format": "FCStd",
                    "nonempty": True,
                    "object_count": 10,
                }
                original_name = document.Name
                freecad.closeDocument(original_name)
                reopened = freecad.openDocument(str(path))
                owned[reopened.Name] = reopened
                reopened.recompute()
                reopened_body = reopened.getObject(native_receipt.body_name)
                reopened_sketch = reopened.getObject(native_receipt.object_name)
                if reopened_body is None or reopened_sketch is None:
                    _fail("sketch_bootstrap_verification/reopen")
                reopened_topology = _primary_topology(reopened_body, reopened_sketch)
                reopened_digests = _primary_digests(reopened_body, reopened_sketch)
                if (
                    tuple(item.Name for item in reopened.Objects) != native_receipt.closure_names
                    or reopened_topology != topology
                    or reopened_digests != digests
                ):
                    _fail("sketch_bootstrap_verification/reopen")
                outcomes[ReviewedConformanceFacet.REOPEN] = {
                    "closure_names": list(native_receipt.closure_names),
                    "topology": reopened_topology,
                    "primary_digests": reopened_digests,
                }
                freecad.closeDocument(reopened.Name)

                edit_document = freecad.newDocument("VerifySketchBootstrapEdit")
                owned[edit_document.Name] = edit_document
                edit_document.UndoMode = 1
                edit_execution = execute_sketch_bootstrap_reviewed_plan_with_sources(
                    edit_document,
                    plan,
                    payload,
                    plan_document,
                    plan_receipt.operation,
                    (),
                )
                edit_sketch = edit_execution.object
                edit_body = edit_execution.receipt.body
                before_edit = _primary_digests(edit_body, edit_sketch)
                edit_sketch.moveGeometry(0, 3, freecad.Vector(2.0, 3.0, 0.0))
                edit_document.recompute()
                after_edit = _primary_digests(
                    edit_body,
                    edit_sketch,
                    require_create_geometry=False,
                )
                edit_topology = _primary_topology(edit_body, edit_sketch)
                if (
                    after_edit["shape_sha256"] == before_edit["shape_sha256"]
                    or after_edit["geometry_sha256"] == before_edit["geometry_sha256"]
                    or after_edit["constraint_sha256"] != before_edit["constraint_sha256"]
                ):
                    _fail("sketch_bootstrap_verification/edit")
                outcomes[ReviewedConformanceFacet.EDIT] = {
                    "strategy": "move-circle-center",
                    "before_primary_digests": before_edit,
                    "after_primary_digests": after_edit,
                    "topology": edit_topology,
                }
                freecad.closeDocument(edit_document.Name)

                outcomes[ReviewedConformanceFacet.LATE_ROLLBACK] = _late_rollback(
                    freecad,
                    plan,
                    payload,
                    plan_document,
                )
        finally:
            _close_owned_documents(freecad, owned)
        if set(outcomes) != set(ReviewedConformanceFacet):
            _fail("sketch_bootstrap_verification/outcomes")
        return outcomes


def build_sketch_bootstrap_managed_verification(
    *, freecad: object
) -> tuple[ReviewedVerificationReceipt, FreeCadPromotionVerificationBinding]:
    """Run the exact one-operation/seven-facet managed verification matrix."""

    if not _VERIFICATION_LOCK.acquire(blocking=False):
        _fail("sketch_bootstrap_verification/concurrent_verification")
    try:
        host = build_managed_freecad_conformance_host(
            freecad=freecad,
            case_manifest=SKETCH_BOOTSTRAP_REVIEWED_HOST_CASE_MANIFEST,
            execute_case=_SketchBootstrapExecutor(freecad),
            verifier_id=SKETCH_BOOTSTRAP_VERIFIER_ID,
            verifier_version=SKETCH_BOOTSTRAP_VERIFIER_VERSION,
        )
        receipt = build_reviewed_verification_receipt(
            manifest=SKETCH_BOOTSTRAP_FAMILY_MANIFEST,
            case_manifest=SKETCH_BOOTSTRAP_REVIEWED_HOST_CASE_MANIFEST,
            host=host,
        )
        return receipt, build_promotion_verification_binding(receipt)
    finally:
        _VERIFICATION_LOCK.release()


__all__ = (
    "SKETCH_BOOTSTRAP_CANDIDATE_FAMILY_MANIFEST",
    "SKETCH_BOOTSTRAP_CANDIDATE_FORMAL_SPEC",
    "SKETCH_BOOTSTRAP_FORMAL_VERIFICATION_HANDOFF",
    "SKETCH_BOOTSTRAP_REVIEWED_HOST_CASE_MANIFEST",
    "SKETCH_BOOTSTRAP_VERIFICATION_FIXTURE",
    "SKETCH_BOOTSTRAP_VERIFIER_ID",
    "SKETCH_BOOTSTRAP_VERIFIER_VERSION",
    "SketchBootstrapFormalVerificationHandoff",
    "SketchBootstrapVerificationFixture",
    "build_sketch_bootstrap_managed_verification",
)
