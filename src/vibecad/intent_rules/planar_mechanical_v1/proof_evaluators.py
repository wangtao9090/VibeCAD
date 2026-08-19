"""Independent semantic proof evaluators for planar-mechanical v1."""

from __future__ import annotations

import hashlib

from vibecad.intent_bridge.contracts import (
    IntentBridgeError,
    IntentBridgeErrorCode,
    SubjectRef,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    PFG_SELECTOR_GRAPH_RESULT,
)
from vibecad.intent_bridge.ports import ValidatedDocument
from vibecad.intent_bridge.sketch_intent_graph_codec import (
    SKETCH_INTENT_GRAPH_MEDIA_TYPE,
    SKETCH_ROOT_SELECTOR_TERM,
    SKETCH_ROOT_SEMANTIC_TYPE_TERM,
)
from vibecad.intent_bridge.trusted_proof_policy import (
    RuleEndpointSignature,
    TrustedRuleEvaluation,
    TrustedRuleEvaluatorDescriptor,
)
from vibecad.intent_bridge.visual_feature_graph_codec import (
    VISUAL_FEATURE_GRAPH_GEOMETRY_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_MEASUREMENT_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
    VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM,
)
from vibecad.intent_compiler.contracts import canonical_bytes
from vibecad.parametric.feature_graph_v2 import encode_parametric_feature_graph_v2
from vibecad.sketch.contracts import encode_sketch_intent_graph
from vibecad.visual.feature_graph import decode_visual_feature_graph

from .rule_set import (
    PLANAR_MECHANICAL_V1_RULE_SET_CONTRACT_SHA256,
    PlanarMechanicalEvidence,
    analyze_visual_feature_graph,
    build_intent_graphs,
)
from .terms import (
    PFG_TYPE_SOLID,
    PREDICATE_PARAMETRIC_COMPILED,
    PREDICATE_SKETCH_COMPILED,
    ROLE_COMPONENT,
    ROLE_DECISION,
    ROLE_DEPTH,
    ROLE_OUTER_PROFILE,
    ROLE_PARAMETRIC_INTENT,
    ROLE_SKETCH_INTENT,
    RULE_COMPILE_PARAMETRIC,
    RULE_COMPILE_SKETCH,
)

_EVALUATOR_CONTRACT_DOMAIN = b"vibecad.intent-rules.planar-mechanical-v1.evaluator\0"


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


def _contract(kind: str) -> str:
    return hashlib.sha256(
        _EVALUATOR_CONTRACT_DOMAIN
        + canonical_bytes(
            {
                "admission_contract_sha256": PLANAR_MECHANICAL_V1_RULE_SET_CONTRACT_SHA256,
                "comparison": "recompile-and-require-exact-canonical-bytes",
                "evaluator": kind,
                "required_documents": [
                    VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
                    SKETCH_INTENT_GRAPH_MEDIA_TYPE,
                    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
                ],
                "version": 1,
            }
        )
    ).hexdigest()


def _premise_signatures() -> tuple[RuleEndpointSignature, ...]:
    return (
        RuleEndpointSignature(
            selector_kind_term=VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM,
            role_term=ROLE_DECISION,
            subject_type_term=VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM,
        ),
        RuleEndpointSignature(
            selector_kind_term=VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM,
            role_term=ROLE_COMPONENT,
            subject_type_term=VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM,
        ),
        RuleEndpointSignature(
            selector_kind_term=VISUAL_FEATURE_GRAPH_GEOMETRY_SELECTOR_TERM,
            role_term=ROLE_OUTER_PROFILE,
            subject_type_term=VISUAL_FEATURE_GRAPH_GEOMETRY_SELECTOR_TERM,
        ),
        RuleEndpointSignature(
            selector_kind_term=VISUAL_FEATURE_GRAPH_MEASUREMENT_SELECTOR_TERM,
            role_term=ROLE_DEPTH,
            subject_type_term=VISUAL_FEATURE_GRAPH_MEASUREMENT_SELECTOR_TERM,
        ),
    )


def _document_by_media(
    evaluation: TrustedRuleEvaluation,
    media_type: str,
) -> ValidatedDocument:
    matches = [item for item in evaluation.documents if item.document.media_type == media_type]
    if len(matches) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/pm1/proof/documents")
    return matches[0]


def _evidence(
    evaluation: TrustedRuleEvaluation,
) -> tuple[PlanarMechanicalEvidence, ValidatedDocument, ValidatedDocument]:
    if len(evaluation.documents) != 3:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/pm1/proof/documents")
    visual = _document_by_media(evaluation, VISUAL_FEATURE_GRAPH_MEDIA_TYPE)
    sketch = _document_by_media(evaluation, SKETCH_INTENT_GRAPH_MEDIA_TYPE)
    parametric = _document_by_media(evaluation, PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE)
    graph = decode_visual_feature_graph(visual.payload)
    evidence = analyze_visual_feature_graph(graph)
    if evidence is None:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/pm1/proof/evidence")
    expected_premises = (
        SubjectRef(
            artifact_id=visual.document.artifact_id,
            selector_kind_term_ref_id=VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM.term_ref_id,
            selector_id=evidence.decision_node_id,
        ),
        SubjectRef(
            artifact_id=visual.document.artifact_id,
            selector_kind_term_ref_id=VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM.term_ref_id,
            selector_id=evidence.component_node_id,
        ),
        SubjectRef(
            artifact_id=visual.document.artifact_id,
            selector_kind_term_ref_id=VISUAL_FEATURE_GRAPH_GEOMETRY_SELECTOR_TERM.term_ref_id,
            selector_id=evidence.outer_geometry_id,
        ),
        SubjectRef(
            artifact_id=visual.document.artifact_id,
            selector_kind_term_ref_id=VISUAL_FEATURE_GRAPH_MEASUREMENT_SELECTOR_TERM.term_ref_id,
            selector_id=evidence.depth_measurement_id,
        ),
    )
    if tuple(item.subject for item in evaluation.premises) != expected_premises:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/pm1/proof/premises")
    return evidence, sketch, parametric


class PlanarMechanicalSketchProofEvaluator:
    __slots__ = ("_descriptor",)

    def __init__(self) -> None:
        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="planar_mechanical_v1_sketch_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_contract("sketch"),
            rule_term=RULE_COMPILE_SKETCH,
            predicate_term=PREDICATE_SKETCH_COMPILED,
            premises=_premise_signatures(),
            conclusions=(
                RuleEndpointSignature(
                    selector_kind_term=SKETCH_ROOT_SELECTOR_TERM,
                    role_term=ROLE_SKETCH_INTENT,
                    subject_type_term=SKETCH_ROOT_SEMANTIC_TYPE_TERM,
                ),
            ),
        )

    @property
    def descriptor(self) -> TrustedRuleEvaluatorDescriptor:
        return self._descriptor

    def validate(self, evaluation: TrustedRuleEvaluation) -> None:
        evidence, sketch_document, _ = _evidence(evaluation)
        expected_sketch, _ = build_intent_graphs(evidence)
        conclusion = evaluation.conclusions[0].subject
        if (
            conclusion.artifact_id != sketch_document.document.artifact_id
            or conclusion.selector_id != expected_sketch.sketch_id
            or sketch_document.payload != encode_sketch_intent_graph(expected_sketch)
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/pm1/proof/sketch")


class PlanarMechanicalParametricProofEvaluator:
    __slots__ = ("_descriptor",)

    def __init__(self) -> None:
        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="planar_mechanical_v1_parametric_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_contract("parametric"),
            rule_term=RULE_COMPILE_PARAMETRIC,
            predicate_term=PREDICATE_PARAMETRIC_COMPILED,
            premises=_premise_signatures(),
            conclusions=(
                RuleEndpointSignature(
                    selector_kind_term=PFG_SELECTOR_GRAPH_RESULT,
                    role_term=ROLE_PARAMETRIC_INTENT,
                    subject_type_term=PFG_TYPE_SOLID,
                ),
            ),
        )

    @property
    def descriptor(self) -> TrustedRuleEvaluatorDescriptor:
        return self._descriptor

    def validate(self, evaluation: TrustedRuleEvaluation) -> None:
        evidence, sketch_document, parametric_document = _evidence(evaluation)
        expected_sketch, expected_parametric = build_intent_graphs(evidence)
        conclusion = evaluation.conclusions[0].subject
        if (
            conclusion.artifact_id != parametric_document.document.artifact_id
            or conclusion.selector_id != "selection.primary"
            or sketch_document.payload != encode_sketch_intent_graph(expected_sketch)
            or parametric_document.payload
            != encode_parametric_feature_graph_v2(expected_parametric)
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/pm1/proof/parametric")


__all__ = [
    "PlanarMechanicalParametricProofEvaluator",
    "PlanarMechanicalSketchProofEvaluator",
]
