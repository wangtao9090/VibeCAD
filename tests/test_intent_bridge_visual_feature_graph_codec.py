"""Focused trust-boundary tests for the VisualFeatureGraph bridge codec."""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from vibecad.intent_bridge.contracts import (
    BridgeTermRef,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    SubjectRef,
)
from vibecad.intent_bridge.ports import (
    GraphCodec,
    TrustedCodecRegistry,
    resolve_subject,
    validate_documents,
)
from vibecad.intent_bridge.visual_feature_graph_codec import (
    VISUAL_FEATURE_GRAPH_ALTERNATIVE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_HYPOTHESIS_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
    VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_RELATION_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_SCHEMA_TERM,
    VisualFeatureGraphCodec,
)
from vibecad.visual.feature_graph import (
    MAX_VISUAL_FEATURE_GRAPH_BYTES,
    EntityLayer,
    FeatureNode,
    FeatureRelation,
    GraphElementKind,
    GraphElementRef,
    HypothesisAlternative,
    HypothesisSet,
    OntologyTermRef,
    RelationEndpoint,
    VisualFeatureGraph,
    encode_visual_feature_graph,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _graph_term(term_ref_id: str, term_id: str) -> OntologyTermRef:
    return OntologyTermRef(
        term_ref_id=term_ref_id,
        namespace="vfg-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"definition:{term_id}"),
    )


def _graph() -> VisualFeatureGraph:
    terms = (
        _graph_term("entity.object", "entity/object"),
        _graph_term("entity.region", "entity/region"),
        _graph_term("relation.partof", "relation/part-of"),
        _graph_term("role.part", "role/part"),
        _graph_term("role.whole", "role/whole"),
        _graph_term("hypothesis.shape", "hypothesis/shape"),
    )
    nodes = tuple(
        FeatureNode(
            node_id=node_id,
            layer=layer,
            term_ref_ids=(term_ref_id,),
        )
        for node_id, layer, term_ref_id in (
            ("node.object", EntityLayer.OBJECT, "entity.object"),
            ("node.part", EntityLayer.COMPONENT, "entity.object"),
            ("node.alt.a", EntityLayer.REGION, "entity.region"),
            ("node.alt.b", EntityLayer.REGION, "entity.region"),
        )
    )
    return VisualFeatureGraph(
        scope_id="scope.codec",
        scope_version=1,
        source_bundle_sha256=_sha("source-bundle"),
        producer_algorithm_id="visual.graph.builder",
        producer_algorithm_version="1.0.0",
        producer_contract_sha256=_sha("producer-contract"),
        ontology_terms=terms,
        nodes=nodes,
        relations=(
            FeatureRelation(
                relation_id="relation.partof",
                relation_term_ref_id="relation.partof",
                endpoints=(
                    RelationEndpoint(
                        ordinal=0,
                        role_term_ref_id="role.whole",
                        element=GraphElementRef(
                            kind=GraphElementKind.NODE,
                            element_id="node.object",
                        ),
                    ),
                    RelationEndpoint(
                        ordinal=1,
                        role_term_ref_id="role.part",
                        element=GraphElementRef(
                            kind=GraphElementKind.NODE,
                            element_id="node.part",
                        ),
                    ),
                ),
            ),
        ),
        hypothesis_sets=(
            HypothesisSet(
                hypothesis_set_id="hypothesis.shape",
                subject_refs=(
                    GraphElementRef(kind=GraphElementKind.NODE, element_id="node.object"),
                ),
                alternatives=(
                    HypothesisAlternative(
                        alternative_id="alternative.a",
                        member_refs=(
                            GraphElementRef(
                                kind=GraphElementKind.NODE,
                                element_id="node.alt.a",
                            ),
                        ),
                    ),
                    HypothesisAlternative(
                        alternative_id="alternative.b",
                        member_refs=(
                            GraphElementRef(
                                kind=GraphElementKind.NODE,
                                element_id="node.alt.b",
                            ),
                        ),
                    ),
                ),
                term_ref_ids=("hypothesis.shape",),
            ),
        ),
    )


def _document(graph: VisualFeatureGraph, *, payload: bytes | None = None) -> DocumentRef:
    raw = encode_visual_feature_graph(graph) if payload is None else payload
    return DocumentRef(
        artifact_id="artifact.vfg",
        role_term_ref_id="role.evidence",
        schema_term_ref_id=VISUAL_FEATURE_GRAPH_SCHEMA_TERM.term_ref_id,
        document_id=graph.graph_id,
        document_digest=graph.graph_digest,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        media_type=VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
    )


def _subject(term: BridgeTermRef, selector_id: str) -> SubjectRef:
    return SubjectRef(
        artifact_id="artifact.vfg",
        selector_kind_term_ref_id=term.term_ref_id,
        selector_id=selector_id,
    )


class _MemoryReader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.reads = 0

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        self.reads += 1
        assert document.artifact_id == "artifact.vfg"
        assert len(self.payload) <= maximum_bytes
        return self.payload


def test_descriptor_is_content_bound_and_registry_uses_full_schema_identity() -> None:
    codec = VisualFeatureGraphCodec()
    descriptor = codec.descriptor
    alias = dataclasses.replace(VISUAL_FEATURE_GRAPH_SCHEMA_TERM, term_ref_id="schema.alias")
    rebound = dataclasses.replace(
        VISUAL_FEATURE_GRAPH_SCHEMA_TERM,
        term_ref_id="schema.rebound",
        term_definition_sha256="f" * 64,
    )
    registry = TrustedCodecRegistry((codec,))

    assert isinstance(codec, GraphCodec)
    assert len(descriptor.codec_contract_sha256) == 64
    assert len(descriptor.schema_term.term_definition_sha256) == 64
    assert (
        len(
            {
                item.semantic_identity
                for item in (
                    VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM,
                    VISUAL_FEATURE_GRAPH_RELATION_SELECTOR_TERM,
                    VISUAL_FEATURE_GRAPH_HYPOTHESIS_SELECTOR_TERM,
                    VISUAL_FEATURE_GRAPH_ALTERNATIVE_SELECTOR_TERM,
                )
            }
        )
        == 4
    )
    assert registry.codec_for(alias) is codec
    assert registry.codec_for(rebound) is None


def test_canonical_document_and_all_reserved_subject_kinds_resolve_exactly() -> None:
    graph = _graph()
    raw = encode_visual_feature_graph(graph)
    document = _document(graph)
    codec = VisualFeatureGraphCodec()
    registry = TrustedCodecRegistry((codec,))
    report = validate_documents(
        terms=(VISUAL_FEATURE_GRAPH_SCHEMA_TERM,),
        documents=(document,),
        reader=_MemoryReader(raw),
        codecs=registry,
        maximum_total_bytes=len(raw),
    )
    cases = (
        (VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM, "node.object"),
        (VISUAL_FEATURE_GRAPH_RELATION_SELECTOR_TERM, "relation.partof"),
        (VISUAL_FEATURE_GRAPH_HYPOTHESIS_SELECTOR_TERM, "hypothesis.shape"),
        (VISUAL_FEATURE_GRAPH_ALTERNATIVE_SELECTOR_TERM, "alternative.a"),
    )

    codec.validate_document(document, raw)
    for selector_term, selector_id in cases:
        subject = _subject(selector_term, selector_id)
        resolved = resolve_subject(
            subject,
            validated_documents=report.validated,
            codecs=registry,
        )
        assert resolved is not None
        assert resolved.subject == subject
        assert resolved.semantic_type.semantic_identity == selector_term.semantic_identity

    assert graph.authority.value == "advisory_only"
    assert not any(hasattr(codec, name) for name in ("adopt", "compile", "execute", "lower"))


def test_tamper_noncanonical_and_wrong_semantic_document_binding_fail_closed() -> None:
    graph = _graph()
    raw = encode_visual_feature_graph(graph)
    codec = VisualFeatureGraphCodec()
    mapping = json.loads(raw)
    mapping["producer_algorithm_version"] = "2.0.0"
    tampered = json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode("ascii")
    cases = (
        (_document(graph, payload=tampered), tampered),
        (_document(graph, payload=b" " + raw), b" " + raw),
        (dataclasses.replace(_document(graph), content_sha256="f" * 64), raw),
        (dataclasses.replace(_document(graph), document_digest="f" * 64), raw),
        (dataclasses.replace(_document(graph), document_id="visual_feature_graph_wrong"), raw),
    )

    for document, payload in cases:
        with pytest.raises(IntentBridgeError) as error:
            codec.validate_document(document, payload)
        assert error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
        assert len(error.value.path.encode("utf-8")) <= 384


def test_wrong_schema_and_unknown_or_cross_kind_selectors_remain_inert() -> None:
    graph = _graph()
    raw = encode_visual_feature_graph(graph)
    document = _document(graph)
    codec = VisualFeatureGraphCodec()
    wrong_schema = dataclasses.replace(
        VISUAL_FEATURE_GRAPH_SCHEMA_TERM,
        term_definition_sha256="f" * 64,
    )
    reader = _MemoryReader(raw)
    report = validate_documents(
        terms=(wrong_schema,),
        documents=(document,),
        reader=reader,
        codecs=TrustedCodecRegistry((codec,)),
        maximum_total_bytes=len(raw),
    )

    assert report.validated == ()
    assert report.inert_artifact_ids == ("artifact.vfg",)
    assert reader.reads == 0
    unknown_kind = SubjectRef(
        artifact_id="artifact.vfg",
        selector_kind_term_ref_id="vfg.selector.future.v9",
        selector_id="node.object",
    )
    assert codec.resolve_subject(document, raw, unknown_kind) is None
    assert (
        codec.resolve_subject(
            document,
            raw,
            _subject(VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM, "node.missing"),
        )
        is None
    )
    assert (
        codec.resolve_subject(
            document,
            raw,
            _subject(VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM, "relation.partof"),
        )
        is None
    )


def test_payload_budget_classifies_n_and_n_plus_one_with_bounded_errors() -> None:
    codec = VisualFeatureGraphCodec()
    at_limit = b"x" * MAX_VISUAL_FEATURE_GRAPH_BYTES
    over_limit = at_limit + b"x"

    with pytest.raises(IntentBridgeError) as at_limit_error:
        codec.validate_document(_document(_graph(), payload=at_limit), at_limit)
    assert at_limit_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    with pytest.raises(IntentBridgeError) as over_limit_error:
        codec.validate_document(_document(_graph(), payload=over_limit), over_limit)
    assert over_limit_error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert len(over_limit_error.value.path.encode("utf-8")) <= 384
