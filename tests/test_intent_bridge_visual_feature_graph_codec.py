"""Focused trust-boundary tests for the VisualFeatureGraph bridge codec."""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

import vibecad.intent_bridge.visual_feature_graph_codec as vfg_codec
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
    VISUAL_FEATURE_GRAPH_APPEARANCE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_CELL_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_DOCUMENT_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_EQUIVALENCE_GROUP_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_EXTENSION_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_FRAME_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_GEOMETRY_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_HYPOTHESIS_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_MEASUREMENT_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
    VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_ONTOLOGY_TERM_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_PROVENANCE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_RELATION_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_SAMPLE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_SCHEMA_TERM,
    VISUAL_FEATURE_GRAPH_SOURCE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_TRANSFORM_SELECTOR_TERM,
    VisualFeatureGraphCodec,
)
from vibecad.visual.feature_graph import (
    MAX_VISUAL_FEATURE_GRAPH_BYTES,
    AppearanceRecord,
    AssertionState,
    CellOrientation,
    ClosureState,
    ContentRef,
    CoordinateFrame,
    CoordinateSample,
    EntityLayer,
    EquivalenceGroup,
    ExtensionRef,
    FeatureNode,
    FeatureRelation,
    FrameTransformRef,
    GeometryRecord,
    GraphElementKind,
    GraphElementRef,
    HypothesisAlternative,
    HypothesisSet,
    MeasurementEstimate,
    MeasurementEstimateKind,
    MeasurementRecord,
    MetricUncertainty,
    MetricUncertaintyKind,
    OntologyTermRef,
    ProvenanceKind,
    ProvenanceRecord,
    RelationEndpoint,
    SourceArtifact,
    SourcePixelFrameBinding,
    TopologyCell,
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
        _graph_term("modality.image", "modality/image"),
        _graph_term("geometry.point", "geometry/point"),
        _graph_term("cell.vertex", "topology/vertex"),
        _graph_term("transform.planar", "transform/planar"),
        _graph_term("quantity.length", "quantity/length"),
        _graph_term("unit.mm", "unit/mm"),
        _graph_term("appearance.dark", "appearance/dark"),
        _graph_term("extension.schema", "schema/extension"),
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
        extensions=(
            ExtensionRef(
                extension_id="extension.fixture",
                namespace="vfg-test",
                vocabulary_version="1.0.0",
                schema_term_ref_id="extension.schema",
                payload=ContentRef(
                    sha256=_sha("extension-payload"),
                    size_bytes=17,
                    media_type="application/json",
                    schema_term_ref_id="extension.schema",
                ),
            ),
        ),
        provenance=(
            ProvenanceRecord(
                provenance_id="provenance.fixture",
                kind=ProvenanceKind.DETERMINISTIC_DERIVATION,
                content=ContentRef(
                    sha256=_sha("provenance-payload"),
                    size_bytes=19,
                    media_type="application/json",
                ),
                producer_id="fixture.builder",
                producer_version="1.0.0",
            ),
        ),
        sources=(
            SourceArtifact(
                source_id="source.image",
                content=ContentRef(
                    sha256=_sha("source-image"),
                    size_bytes=101,
                    media_type="image/png",
                ),
                modality_term_ref_ids=("modality.image",),
                provenance_ids=("provenance.fixture",),
            ),
        ),
        frames=(
            CoordinateFrame(
                frame_id="frame.image",
                binding=SourcePixelFrameBinding(
                    source_sha256=_sha("source-image"),
                    width=101,
                    height=101,
                ),
                source_id="source.image",
            ),
            CoordinateFrame(
                frame_id="frame.image.secondary",
                binding=SourcePixelFrameBinding(
                    source_sha256=_sha("source-image"),
                    width=101,
                    height=101,
                ),
                source_id="source.image",
            ),
        ),
        transforms=(
            FrameTransformRef(
                transform_id="transform.views",
                from_frame_id="frame.image",
                to_frame_id="frame.image.secondary",
                transform_term_ref_id="transform.planar",
                receipt=ContentRef(
                    sha256=_sha("transform-receipt"),
                    size_bytes=23,
                    media_type="application/json",
                ),
            ),
        ),
        geometries=(
            GeometryRecord(
                geometry_id="geometry.landmark",
                frame_id="frame.image",
                representation_term_ref_id="geometry.point",
                intrinsic_dimension=0,
                samples=(
                    CoordinateSample(
                        sample_id="sample.landmark",
                        coordinates=(20.0, 30.0),
                        uncertainty=MetricUncertainty(
                            kind=MetricUncertaintyKind.ABSOLUTE_BOUND,
                            bounds=(0.5,),
                        ),
                    ),
                ),
                cells=(
                    TopologyCell(
                        cell_id="cell.landmark",
                        cell_term_ref_id="cell.vertex",
                        sample_ids=("sample.landmark",),
                        orientation=CellOrientation.UNKNOWN,
                    ),
                ),
                closure=ClosureState.CLOSED,
                state=AssertionState.OBSERVED,
            ),
        ),
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
        equivalence_groups=(
            EquivalenceGroup(
                group_id="equivalence.parts",
                member_node_ids=("node.object", "node.part"),
                state=AssertionState.INFERRED,
            ),
        ),
        measurements=(
            MeasurementRecord(
                measurement_id="measurement.length",
                quantity_term_ref_id="quantity.length",
                unit_term_ref_id="unit.mm",
                targets=(
                    GraphElementRef(
                        kind=GraphElementKind.GEOMETRY,
                        element_id="geometry.landmark",
                    ),
                ),
                estimate=MeasurementEstimate(
                    kind=MeasurementEstimateKind.EXACT,
                    central=(12.0,),
                ),
                frame_ids=("frame.image",),
                state=AssertionState.OBSERVED,
            ),
        ),
        appearances=(
            AppearanceRecord(
                appearance_id="appearance.object",
                target_node_id="node.object",
                appearance_term_ref_ids=("appearance.dark",),
                source_ids=("source.image",),
                state=AssertionState.OBSERVED,
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
    assert descriptor.codec_version == "1.1.0"
    assert len(descriptor.codec_contract_sha256) == 64
    assert len(descriptor.schema_term.term_definition_sha256) == 64
    assert (
        len(
            {
                item.semantic_identity
                for item in (
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
            }
        )
        == 17
    )
    assert registry.codec_for(alias) is codec
    assert registry.codec_for(rebound) is None

    rebound_selector = dataclasses.replace(
        VISUAL_FEATURE_GRAPH_DOCUMENT_SELECTOR_TERM,
        term_definition_sha256="f" * 64,
    )
    rebound_terms = tuple(
        rebound_selector if item is VISUAL_FEATURE_GRAPH_DOCUMENT_SELECTOR_TERM else item
        for item in vfg_codec._SELECTOR_TERMS
    )
    assert vfg_codec._codec_contract_sha256(rebound_terms) != descriptor.codec_contract_sha256


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
        (VISUAL_FEATURE_GRAPH_DOCUMENT_SELECTOR_TERM, graph.graph_id),
        (VISUAL_FEATURE_GRAPH_ONTOLOGY_TERM_SELECTOR_TERM, "entity.object"),
        (VISUAL_FEATURE_GRAPH_SOURCE_SELECTOR_TERM, "source.image"),
        (VISUAL_FEATURE_GRAPH_FRAME_SELECTOR_TERM, "frame.image"),
        (VISUAL_FEATURE_GRAPH_TRANSFORM_SELECTOR_TERM, "transform.views"),
        (VISUAL_FEATURE_GRAPH_GEOMETRY_SELECTOR_TERM, "geometry.landmark"),
        (VISUAL_FEATURE_GRAPH_SAMPLE_SELECTOR_TERM, "sample.landmark"),
        (VISUAL_FEATURE_GRAPH_CELL_SELECTOR_TERM, "cell.landmark"),
        (VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM, "node.object"),
        (VISUAL_FEATURE_GRAPH_RELATION_SELECTOR_TERM, "relation.partof"),
        (VISUAL_FEATURE_GRAPH_EQUIVALENCE_GROUP_SELECTOR_TERM, "equivalence.parts"),
        (VISUAL_FEATURE_GRAPH_MEASUREMENT_SELECTOR_TERM, "measurement.length"),
        (VISUAL_FEATURE_GRAPH_APPEARANCE_SELECTOR_TERM, "appearance.object"),
        (VISUAL_FEATURE_GRAPH_HYPOTHESIS_SELECTOR_TERM, "hypothesis.shape"),
        (VISUAL_FEATURE_GRAPH_ALTERNATIVE_SELECTOR_TERM, "alternative.a"),
        (VISUAL_FEATURE_GRAPH_PROVENANCE_SELECTOR_TERM, "provenance.fixture"),
        (VISUAL_FEATURE_GRAPH_EXTENSION_SELECTOR_TERM, "extension.fixture"),
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
