"""Trusted structural codec for canonical :mod:`visual.feature_graph` bytes.

The codec is deliberately narrower than a compiler or proof policy.  It binds
one exact VisualFeatureGraph schema identity to canonical bytes and resolves a
small stable set of structural subjects.  It never interprets graph ontology,
selects a hypothesis alternative, adopts evidence, or grants execution.

``SubjectRef`` currently carries only a selector-kind *local ref-id*.  This
codec therefore accepts the four reserved ref-ids below exactly.  The matching
full selector-kind semantic identity (including its definition SHA-256) remains
the responsibility of the ProofBundle term table and the injected trusted
proof policy.  Aliases and unknown ref-ids are inert rather than guessed.
"""

from __future__ import annotations

import hashlib
import hmac

from vibecad.intent_bridge.contracts import (
    BridgeTermRef,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    SubjectRef,
)
from vibecad.intent_bridge.ports import GraphCodecDescriptor, ResolvedSubject
from vibecad.visual.feature_graph import (
    MAX_VISUAL_FEATURE_GRAPH_BYTES,
    VisualFeatureGraph,
    VisualFeatureGraphError,
    VisualFeatureGraphErrorCode,
    decode_visual_feature_graph,
    encode_visual_feature_graph,
)

VISUAL_FEATURE_GRAPH_MEDIA_TYPE = "application/vnd.vibecad.visual-feature-graph+json"

_ONTOLOGY_NAMESPACE = "org.vibecad.visual-feature-graph"
_ONTOLOGY_VERSION = "1.0.0"
_DEFINITION_DOMAIN = b"vibecad.intent-bridge.visual-feature-graph-term.v1\0"
_CODEC_CONTRACT = (
    b"vibecad.intent-bridge.visual-feature-graph-codec.v1\0"
    b"canonical-vfg-v1;document-id=graph-id;document-digest=graph-digest;"
    b"selectors=node,relation,hypothesis-set,hypothesis-alternative;"
    b"authority=structural-only"
)


def _definition_sha256(term_id: str, definition: str) -> str:
    return hashlib.sha256(
        _DEFINITION_DOMAIN + term_id.encode("ascii") + b"\0" + definition.encode("ascii")
    ).hexdigest()


def _term(*, term_ref_id: str, term_id: str, definition: str) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace=_ONTOLOGY_NAMESPACE,
        vocabulary_version=_ONTOLOGY_VERSION,
        term_id=term_id,
        term_definition_sha256=_definition_sha256(term_id, definition),
    )


VISUAL_FEATURE_GRAPH_SCHEMA_TERM = _term(
    term_ref_id="vfg.schema.v1",
    term_id="schema.visual-feature-graph.v1",
    definition="Canonical authority-free VisualFeatureGraph schema version 1.",
)
VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.node.v1",
    term_id="selector.visual-feature-graph.node.v1",
    definition="Select exactly one FeatureNode by its canonical node_id.",
)
VISUAL_FEATURE_GRAPH_RELATION_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.relation.v1",
    term_id="selector.visual-feature-graph.relation.v1",
    definition="Select exactly one FeatureRelation by its canonical relation_id.",
)
VISUAL_FEATURE_GRAPH_HYPOTHESIS_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.hypothesis.v1",
    term_id="selector.visual-feature-graph.hypothesis-set.v1",
    definition="Select exactly one HypothesisSet by its canonical hypothesis_set_id.",
)
VISUAL_FEATURE_GRAPH_ALTERNATIVE_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.alternative.v1",
    term_id="selector.visual-feature-graph.hypothesis-alternative.v1",
    definition="Select exactly one HypothesisAlternative by its canonical alternative_id.",
)

_SELECTOR_TERMS = (
    VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_RELATION_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_HYPOTHESIS_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_ALTERNATIVE_SELECTOR_TERM,
)

_DESCRIPTOR = GraphCodecDescriptor(
    codec_id="visual_feature_graph_codec",
    codec_version="1.0.0",
    codec_contract_sha256=hashlib.sha256(_CODEC_CONTRACT).hexdigest(),
    schema_term=VISUAL_FEATURE_GRAPH_SCHEMA_TERM,
)


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


def _mapped_graph_error(error: VisualFeatureGraphError) -> IntentBridgeErrorCode:
    if error.code is VisualFeatureGraphErrorCode.BUDGET_EXCEEDED:
        return IntentBridgeErrorCode.BUDGET_EXCEEDED
    if error.code is VisualFeatureGraphErrorCode.UNSUPPORTED_VERSION:
        return IntentBridgeErrorCode.UNSUPPORTED_VERSION
    if error.code is VisualFeatureGraphErrorCode.AUTHORITY_VIOLATION:
        return IntentBridgeErrorCode.AUTHORITY_VIOLATION
    return IntentBridgeErrorCode.INTEGRITY_FAILURE


def _decode_validated(document: DocumentRef, payload: bytes) -> VisualFeatureGraph:
    if type(document) is not DocumentRef or type(payload) is not bytes:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/visual_feature_graph")
    if len(payload) > MAX_VISUAL_FEATURE_GRAPH_BYTES:
        _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/visual_feature_graph/payload")
    if (
        len(payload) != document.size_bytes
        or document.media_type != VISUAL_FEATURE_GRAPH_MEDIA_TYPE
        or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), document.content_sha256)
    ):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/visual_feature_graph/content")
    try:
        graph = decode_visual_feature_graph(payload)
    except VisualFeatureGraphError as error:
        _fail(_mapped_graph_error(error), "/visual_feature_graph/payload")
    except Exception:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/visual_feature_graph/payload")
    if (
        not hmac.compare_digest(payload, encode_visual_feature_graph(graph))
        or not hmac.compare_digest(document.document_id, graph.graph_id)
        or not hmac.compare_digest(document.document_digest, graph.graph_digest)
    ):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/visual_feature_graph/document")
    return graph


def _selector_exists(
    graph: VisualFeatureGraph,
    selector_term: BridgeTermRef,
    selector_id: str,
) -> bool:
    if selector_term is VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM:
        return any(item.node_id == selector_id for item in graph.nodes)
    if selector_term is VISUAL_FEATURE_GRAPH_RELATION_SELECTOR_TERM:
        return any(item.relation_id == selector_id for item in graph.relations)
    if selector_term is VISUAL_FEATURE_GRAPH_HYPOTHESIS_SELECTOR_TERM:
        return any(item.hypothesis_set_id == selector_id for item in graph.hypothesis_sets)
    if selector_term is VISUAL_FEATURE_GRAPH_ALTERNATIVE_SELECTOR_TERM:
        return any(
            alternative.alternative_id == selector_id
            for hypothesis in graph.hypothesis_sets
            for alternative in hypothesis.alternatives
        )
    return False


class VisualFeatureGraphCodec:
    """Trusted, immutable GraphCodec for VisualFeatureGraph schema version 1."""

    __slots__ = ()

    @property
    def descriptor(self) -> GraphCodecDescriptor:
        return _DESCRIPTOR

    def validate_document(self, document: DocumentRef, payload: bytes) -> None:
        _decode_validated(document, payload)

    def resolve_subject(
        self,
        document: DocumentRef,
        payload: bytes,
        subject: SubjectRef,
    ) -> ResolvedSubject | None:
        if type(subject) is not SubjectRef:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/visual_feature_graph/subject")
        graph = _decode_validated(document, payload)
        if subject.artifact_id != document.artifact_id:
            return None
        selector_term = next(
            (
                item
                for item in _SELECTOR_TERMS
                if item.term_ref_id == subject.selector_kind_term_ref_id
            ),
            None,
        )
        if selector_term is None or not _selector_exists(graph, selector_term, subject.selector_id):
            return None
        return ResolvedSubject(subject=subject, semantic_type=selector_term)


__all__ = [
    "VISUAL_FEATURE_GRAPH_ALTERNATIVE_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_HYPOTHESIS_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_MEDIA_TYPE",
    "VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_RELATION_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_SCHEMA_TERM",
    "VisualFeatureGraphCodec",
]
