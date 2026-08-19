"""Trusted structural codec for canonical :mod:`visual.feature_graph` bytes.

The codec is deliberately narrower than a compiler or proof policy.  It binds
one exact VisualFeatureGraph schema identity to canonical bytes and resolves
every stable v1 document/element identifier.  It never interprets graph ontology,
selects a hypothesis alternative, adopts evidence, or grants execution.

``SubjectRef`` currently carries only a selector-kind *local ref-id*.  This
codec therefore accepts the reserved ref-ids below exactly.  The matching
full selector-kind semantic identity (including its definition SHA-256) remains
the responsibility of the ProofBundle term table and the injected trusted
proof policy.  Aliases and unknown ref-ids are inert rather than guessed.
"""

from __future__ import annotations

import hashlib
import hmac
import json

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
_CODEC_CONTRACT_DOMAIN = b"vibecad.intent-bridge.visual-feature-graph-codec.v1\0"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


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
VISUAL_FEATURE_GRAPH_DOCUMENT_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.document.v1",
    term_id="selector.visual-feature-graph.document.v1",
    definition="Select the canonical VisualFeatureGraph document by graph_id.",
)
VISUAL_FEATURE_GRAPH_ONTOLOGY_TERM_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.ontology-term.v1",
    term_id="selector.visual-feature-graph.ontology-term.v1",
    definition="Select exactly one graph ontology term by its canonical local ref-id.",
)
VISUAL_FEATURE_GRAPH_SOURCE_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.source.v1",
    term_id="selector.visual-feature-graph.source.v1",
    definition="Select exactly one SourceArtifact by its canonical source_id.",
)
VISUAL_FEATURE_GRAPH_FRAME_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.frame.v1",
    term_id="selector.visual-feature-graph.frame.v1",
    definition="Select exactly one CoordinateFrame by its canonical frame_id.",
)
VISUAL_FEATURE_GRAPH_TRANSFORM_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.transform.v1",
    term_id="selector.visual-feature-graph.transform.v1",
    definition="Select exactly one FrameTransformRef by its canonical transform_id.",
)
VISUAL_FEATURE_GRAPH_GEOMETRY_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.geometry.v1",
    term_id="selector.visual-feature-graph.geometry.v1",
    definition="Select exactly one GeometryRecord by its canonical geometry_id.",
)
VISUAL_FEATURE_GRAPH_SAMPLE_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.sample.v1",
    term_id="selector.visual-feature-graph.sample.v1",
    definition="Select exactly one CoordinateSample by its graph-unique sample_id.",
)
VISUAL_FEATURE_GRAPH_CELL_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.cell.v1",
    term_id="selector.visual-feature-graph.cell.v1",
    definition="Select exactly one TopologyCell by its graph-unique cell_id.",
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
VISUAL_FEATURE_GRAPH_EQUIVALENCE_GROUP_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.equivalence-group.v1",
    term_id="selector.visual-feature-graph.equivalence-group.v1",
    definition="Select exactly one EquivalenceGroup by its canonical group_id.",
)
VISUAL_FEATURE_GRAPH_MEASUREMENT_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.measurement.v1",
    term_id="selector.visual-feature-graph.measurement.v1",
    definition="Select exactly one MeasurementRecord by its canonical measurement_id.",
)
VISUAL_FEATURE_GRAPH_APPEARANCE_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.appearance.v1",
    term_id="selector.visual-feature-graph.appearance.v1",
    definition="Select exactly one AppearanceRecord by its canonical appearance_id.",
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
VISUAL_FEATURE_GRAPH_PROVENANCE_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.provenance.v1",
    term_id="selector.visual-feature-graph.provenance.v1",
    definition="Select exactly one ProvenanceRecord by its canonical provenance_id.",
)
VISUAL_FEATURE_GRAPH_EXTENSION_SELECTOR_TERM = _term(
    term_ref_id="vfg.selector.extension.v1",
    term_id="selector.visual-feature-graph.extension.v1",
    definition="Select exactly one ExtensionRef by its canonical extension_id.",
)

_SELECTOR_TERMS = (
    VISUAL_FEATURE_GRAPH_DOCUMENT_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_ONTOLOGY_TERM_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_SOURCE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_FRAME_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_TRANSFORM_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_GEOMETRY_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_SAMPLE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_CELL_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_RELATION_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_EQUIVALENCE_GROUP_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_MEASUREMENT_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_APPEARANCE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_HYPOTHESIS_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_ALTERNATIVE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_PROVENANCE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_EXTENSION_SELECTOR_TERM,
)


def _codec_contract_sha256(selector_terms: tuple[BridgeTermRef, ...]) -> str:
    return hashlib.sha256(
        _CODEC_CONTRACT_DOMAIN
        + _canonical(
            {
                "authority": "structural-only",
                "canonical_payload": True,
                "document_digest": "graph_digest",
                "document_id": "graph_id",
                "maximum_bytes": MAX_VISUAL_FEATURE_GRAPH_BYTES,
                "media_type": VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
                "schema_term": VISUAL_FEATURE_GRAPH_SCHEMA_TERM.to_mapping(),
                "selector_semantic_type": "selector-term",
                "selector_terms": [
                    item.to_mapping()
                    for item in sorted(selector_terms, key=lambda value: value.term_ref_id)
                ],
            }
        )
    ).hexdigest()


_DESCRIPTOR = GraphCodecDescriptor(
    codec_id="visual_feature_graph_codec",
    codec_version="1.1.0",
    codec_contract_sha256=_codec_contract_sha256(_SELECTOR_TERMS),
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
    if selector_term is VISUAL_FEATURE_GRAPH_DOCUMENT_SELECTOR_TERM:
        return selector_id == graph.graph_id
    if selector_term is VISUAL_FEATURE_GRAPH_ONTOLOGY_TERM_SELECTOR_TERM:
        return any(item.term_ref_id == selector_id for item in graph.ontology_terms)
    if selector_term is VISUAL_FEATURE_GRAPH_SOURCE_SELECTOR_TERM:
        return any(item.source_id == selector_id for item in graph.sources)
    if selector_term is VISUAL_FEATURE_GRAPH_FRAME_SELECTOR_TERM:
        return any(item.frame_id == selector_id for item in graph.frames)
    if selector_term is VISUAL_FEATURE_GRAPH_TRANSFORM_SELECTOR_TERM:
        return any(item.transform_id == selector_id for item in graph.transforms)
    if selector_term is VISUAL_FEATURE_GRAPH_GEOMETRY_SELECTOR_TERM:
        return any(item.geometry_id == selector_id for item in graph.geometries)
    if selector_term is VISUAL_FEATURE_GRAPH_SAMPLE_SELECTOR_TERM:
        return any(
            sample.sample_id == selector_id
            for geometry in graph.geometries
            for sample in geometry.samples
        )
    if selector_term is VISUAL_FEATURE_GRAPH_CELL_SELECTOR_TERM:
        return any(
            cell.cell_id == selector_id for geometry in graph.geometries for cell in geometry.cells
        )
    if selector_term is VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM:
        return any(item.node_id == selector_id for item in graph.nodes)
    if selector_term is VISUAL_FEATURE_GRAPH_RELATION_SELECTOR_TERM:
        return any(item.relation_id == selector_id for item in graph.relations)
    if selector_term is VISUAL_FEATURE_GRAPH_EQUIVALENCE_GROUP_SELECTOR_TERM:
        return any(item.group_id == selector_id for item in graph.equivalence_groups)
    if selector_term is VISUAL_FEATURE_GRAPH_MEASUREMENT_SELECTOR_TERM:
        return any(item.measurement_id == selector_id for item in graph.measurements)
    if selector_term is VISUAL_FEATURE_GRAPH_APPEARANCE_SELECTOR_TERM:
        return any(item.appearance_id == selector_id for item in graph.appearances)
    if selector_term is VISUAL_FEATURE_GRAPH_HYPOTHESIS_SELECTOR_TERM:
        return any(item.hypothesis_set_id == selector_id for item in graph.hypothesis_sets)
    if selector_term is VISUAL_FEATURE_GRAPH_ALTERNATIVE_SELECTOR_TERM:
        return any(
            alternative.alternative_id == selector_id
            for hypothesis in graph.hypothesis_sets
            for alternative in hypothesis.alternatives
        )
    if selector_term is VISUAL_FEATURE_GRAPH_PROVENANCE_SELECTOR_TERM:
        return any(item.provenance_id == selector_id for item in graph.provenance)
    if selector_term is VISUAL_FEATURE_GRAPH_EXTENSION_SELECTOR_TERM:
        return any(item.extension_id == selector_id for item in graph.extensions)
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
    "VISUAL_FEATURE_GRAPH_APPEARANCE_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_CELL_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_DOCUMENT_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_EQUIVALENCE_GROUP_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_EXTENSION_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_FRAME_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_GEOMETRY_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_HYPOTHESIS_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_MEDIA_TYPE",
    "VISUAL_FEATURE_GRAPH_MEASUREMENT_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_ONTOLOGY_TERM_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_PROVENANCE_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_RELATION_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_SAMPLE_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_SCHEMA_TERM",
    "VISUAL_FEATURE_GRAPH_SOURCE_SELECTOR_TERM",
    "VISUAL_FEATURE_GRAPH_TRANSFORM_SELECTOR_TERM",
    "VisualFeatureGraphCodec",
]
