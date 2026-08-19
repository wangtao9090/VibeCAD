"""Concrete trusted VFG selector for the planar-mechanical v1 rule pack."""

from __future__ import annotations

import hashlib

from vibecad.intent_bridge.contracts import IntentCompileRequest, SubjectRef
from vibecad.intent_bridge.ports import ValidatedDocument
from vibecad.intent_bridge.visual_feature_graph_codec import (
    VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_SCHEMA_TERM,
    VisualFeatureGraphCodec,
)
from vibecad.intent_compiler.contracts import IntentSelection, canonical_bytes
from vibecad.intent_compiler.source_ports import SourceAdapterDescriptor
from vibecad.intent_rules.planar_mechanical_v1.rule_set import (
    PLANAR_MECHANICAL_V1_RULE_SET_CONTRACT_SHA256,
    analyze_visual_feature_graph,
)
from vibecad.intent_rules.planar_mechanical_v1.terms import (
    ROLE_VISUAL_EVIDENCE,
    RULE_SET_PLANAR_MECHANICAL_V1,
)
from vibecad.visual.feature_graph import (
    VisualFeatureGraphError,
    decode_visual_feature_graph,
)

_ADAPTER_CONTRACT_DOMAIN = b"vibecad.intent-compiler.vfg-planar-mechanical-v1-adapter\0"


class PlanarMechanicalV1VFGSourceAdapter:
    """Select one exact human-confirmed decision, otherwise remain inert."""

    __slots__ = ("_descriptor",)

    def __init__(self) -> None:
        codec = VisualFeatureGraphCodec().descriptor
        contract = {
            "admission_contract_sha256": PLANAR_MECHANICAL_V1_RULE_SET_CONTRACT_SHA256,
            "codec_contract_sha256": codec.codec_contract_sha256,
            "decision_selector": VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM.to_mapping(),
            "input_role": ROLE_VISUAL_EVIDENCE.to_mapping(),
            "rule_set": RULE_SET_PLANAR_MECHANICAL_V1.to_mapping(),
            "selection": "one-exact-human-confirmed-decision-or-inert",
            "version": 1,
        }
        self._descriptor = SourceAdapterDescriptor(
            adapter_id="vfg_planar_mechanical_v1",
            adapter_version="1.0.0",
            adapter_contract_sha256=hashlib.sha256(
                _ADAPTER_CONTRACT_DOMAIN + canonical_bytes(contract)
            ).hexdigest(),
            input_schema_terms=(VISUAL_FEATURE_GRAPH_SCHEMA_TERM,),
        )

    @property
    def descriptor(self) -> SourceAdapterDescriptor:
        return self._descriptor

    def select(
        self,
        request: IntentCompileRequest,
        documents: tuple[ValidatedDocument, ...],
    ) -> IntentSelection | None:
        if len(documents) != 1 or len(request.inputs) != 1:
            return None
        document = documents[0]
        binding = request.inputs[0]
        term_by_id = {item.term_ref_id: item for item in request.terms}
        required = (
            RULE_SET_PLANAR_MECHANICAL_V1,
            ROLE_VISUAL_EVIDENCE,
            VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM,
        )
        if any(
            (actual := term_by_id.get(expected.term_ref_id)) is None
            or actual.semantic_identity != expected.semantic_identity
            for expected in required
        ):
            return None
        if (
            binding.artifact_id != document.document.artifact_id
            or binding.role_term_ref_id != ROLE_VISUAL_EVIDENCE.term_ref_id
            or document.codec_descriptor.schema_term.semantic_identity
            != VISUAL_FEATURE_GRAPH_SCHEMA_TERM.semantic_identity
        ):
            return None
        try:
            graph = decode_visual_feature_graph(document.payload)
        except VisualFeatureGraphError:
            return None
        evidence = analyze_visual_feature_graph(graph)
        if evidence is None:
            return None
        return IntentSelection(
            rule_set_term=RULE_SET_PLANAR_MECHANICAL_V1,
            decision_subjects=(
                SubjectRef(
                    artifact_id=document.document.artifact_id,
                    selector_kind_term_ref_id=VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM.term_ref_id,
                    selector_id=evidence.decision_node_id,
                ),
            ),
        )


__all__ = ["PlanarMechanicalV1VFGSourceAdapter"]
