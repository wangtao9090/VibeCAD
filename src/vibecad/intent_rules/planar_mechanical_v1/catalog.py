"""Host-local composition helpers for planar-mechanical v1."""

from __future__ import annotations

from dataclasses import dataclass

from vibecad.intent_bridge.contracts import BridgeTermRef
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_GRAPH_RESULT,
    ParametricFeatureGraphV2Codec,
)
from vibecad.intent_bridge.ports import TrustedCodecRegistry
from vibecad.intent_bridge.sketch_intent_graph_codec import (
    SKETCH_INTENT_GRAPH_SCHEMA_TERM,
    SKETCH_ROOT_SELECTOR_TERM,
    SKETCH_ROOT_SEMANTIC_TYPE_TERM,
    SketchIntentGraphCodec,
)
from vibecad.intent_bridge.trusted_proof_policy import TrustedRulePolicy
from vibecad.intent_bridge.visual_feature_graph_codec import (
    VISUAL_FEATURE_GRAPH_GEOMETRY_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_MEASUREMENT_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_SCHEMA_TERM,
    VisualFeatureGraphCodec,
)
from vibecad.intent_compiler.artifacts import IntentArtifactPublisher
from vibecad.intent_compiler.catalog import TrustedIntentRuleCatalog
from vibecad.intent_compiler.compiler import RuleDrivenIntentCompiler
from vibecad.intent_compiler.vfg_source_adapter import PlanarMechanicalV1VFGSourceAdapter

from .proof_evaluators import (
    PlanarMechanicalParametricProofEvaluator,
    PlanarMechanicalSketchProofEvaluator,
)
from .rule_set import PlanarMechanicalV1RuleSet
from .terms import PLANAR_MECHANICAL_V1_CUSTOM_BRIDGE_TERMS


@dataclass(frozen=True, slots=True)
class PlanarMechanicalV1CompilerStack:
    compiler: RuleDrivenIntentCompiler
    proof_policy: TrustedRulePolicy
    codecs: TrustedCodecRegistry


def planar_mechanical_v1_request_terms() -> tuple[BridgeTermRef, ...]:
    """Exact canonical term table required by a compile request."""

    terms = (
        *PLANAR_MECHANICAL_V1_CUSTOM_BRIDGE_TERMS,
        VISUAL_FEATURE_GRAPH_SCHEMA_TERM,
        VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM,
        VISUAL_FEATURE_GRAPH_GEOMETRY_SELECTOR_TERM,
        VISUAL_FEATURE_GRAPH_MEASUREMENT_SELECTOR_TERM,
        SKETCH_INTENT_GRAPH_SCHEMA_TERM,
        SKETCH_ROOT_SELECTOR_TERM,
        SKETCH_ROOT_SEMANTIC_TYPE_TERM,
        PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
        PFG_SELECTOR_GRAPH_RESULT,
    )
    by_id = {item.term_ref_id: item for item in terms}
    return tuple(sorted(by_id.values(), key=lambda item: item.term_ref_id))


def build_planar_mechanical_v1_proof_policy() -> TrustedRulePolicy:
    return TrustedRulePolicy(
        evaluators=(
            PlanarMechanicalSketchProofEvaluator(),
            PlanarMechanicalParametricProofEvaluator(),
        )
    )


def build_planar_mechanical_v1_stack(
    *,
    publisher: IntentArtifactPublisher,
) -> PlanarMechanicalV1CompilerStack:
    policy = build_planar_mechanical_v1_proof_policy()
    rule_catalog = TrustedIntentRuleCatalog(
        (PlanarMechanicalV1RuleSet(),),
        proof_policy_catalog_sha256=policy.catalog_sha256,
    )
    compiler = RuleDrivenIntentCompiler(
        compiler_id="planar_mechanical_v1_compiler",
        source_adapters=(PlanarMechanicalV1VFGSourceAdapter(),),
        rule_catalog=rule_catalog,
        publisher=publisher,
    )
    return PlanarMechanicalV1CompilerStack(
        compiler=compiler,
        proof_policy=policy,
        codecs=TrustedCodecRegistry(
            (
                VisualFeatureGraphCodec(),
                SketchIntentGraphCodec(),
                ParametricFeatureGraphV2Codec(),
            )
        ),
    )


__all__ = [
    "PlanarMechanicalV1CompilerStack",
    "build_planar_mechanical_v1_proof_policy",
    "build_planar_mechanical_v1_stack",
    "planar_mechanical_v1_request_terms",
]
