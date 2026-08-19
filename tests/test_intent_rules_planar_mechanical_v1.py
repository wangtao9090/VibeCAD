"""End-to-end gates for the first trusted VFG planar-mechanical rule pack."""

from __future__ import annotations

import dataclasses
import hashlib
import math

import pytest

from vibecad.intent_bridge.contracts import (
    BridgeBudget,
    BridgeDisposition,
    CompileInputBinding,
    DocumentRef,
    IntentBridgeError,
    IntentCompileRequest,
    RequestedOutput,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
)
from vibecad.intent_bridge.sketch_intent_graph_codec import (
    SKETCH_INTENT_GRAPH_MEDIA_TYPE,
    SKETCH_INTENT_GRAPH_SCHEMA_TERM,
)
from vibecad.intent_bridge.visual_feature_graph_codec import (
    VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
    VISUAL_FEATURE_GRAPH_SCHEMA_TERM,
)
from vibecad.intent_compiler.artifacts import (
    ArtifactPublisherDescriptor,
    InMemoryIntentArtifactPublisher,
)
from vibecad.intent_compiler.catalog import TrustedIntentRuleCatalog
from vibecad.intent_compiler.compiler import RuleDrivenIntentCompiler
from vibecad.intent_compiler.contracts import CompiledIntentDocument, RuleSetEmission
from vibecad.intent_compiler.vfg_source_adapter import PlanarMechanicalV1VFGSourceAdapter
from vibecad.intent_rules.planar_mechanical_v1.catalog import (
    build_planar_mechanical_v1_proof_policy,
    build_planar_mechanical_v1_stack,
    planar_mechanical_v1_request_terms,
)
from vibecad.intent_rules.planar_mechanical_v1.rule_set import PlanarMechanicalV1RuleSet
from vibecad.intent_rules.planar_mechanical_v1.terms import (
    PFG_OPERATION_ADD,
    PFG_OPERATION_REMOVE,
    ROLE_PARAMETRIC_INTENT,
    ROLE_SKETCH_INTENT,
    ROLE_VISUAL_EVIDENCE,
    VFG_CIRCLE,
    VFG_COMPONENT,
    VFG_DECISION,
    VFG_INNER_PROFILE,
    VFG_MODALITY_IMAGE,
    VFG_OUTER_PROFILE,
    VFG_QUANTITY_DEPTH,
    VFG_RELATION_DECISION_SUBJECT,
    VFG_RELATION_THROUGH_EXTENT,
    VFG_REQUIRED_TERMS,
    VFG_ROLE_COMPONENT,
    VFG_ROLE_DECISION,
    VFG_ROLE_PROFILE,
    VFG_ROTATED_RECTANGLE,
    VFG_SAMPLE_BOUNDARY,
    VFG_SAMPLE_CENTER,
    VFG_SAMPLE_CORNER,
    VFG_UNIT_MM,
    as_visual_term,
)
from vibecad.parametric.feature_graph_v2 import decode_parametric_feature_graph_v2
from vibecad.sketch.contracts import decode_sketch_intent_graph, encode_sketch_intent_graph
from vibecad.visual.feature_graph import (
    AssertionState,
    ClosureState,
    ContentRef,
    CoordinateFrame,
    CoordinateSample,
    EntityLayer,
    FeatureNode,
    FeatureRelation,
    GeometryRecord,
    GraphElementKind,
    GraphElementRef,
    MeasurementEstimate,
    MeasurementEstimateKind,
    MeasurementRecord,
    MetricPlaneFrameBinding,
    MetricUncertainty,
    MetricUncertaintyKind,
    ProvenanceKind,
    ProvenanceRecord,
    RelationEndpoint,
    SourceArtifact,
    VisualFeatureGraph,
    encode_visual_feature_graph,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _content(label: str, media_type: str = "application/json") -> ContentRef:
    return ContentRef(sha256=_sha(label), size_bytes=64, media_type=media_type)


def _sample(
    identifier: str,
    point: tuple[float, float],
    role_term_ref_id: str,
    *,
    uncertainty: float = 0.05,
) -> CoordinateSample:
    return CoordinateSample(
        sample_id=identifier,
        coordinates=point,
        uncertainty=MetricUncertainty(
            kind=MetricUncertaintyKind.AXIS_BOUNDS,
            bounds=(uncertainty, uncertainty),
        ),
        term_ref_ids=(role_term_ref_id,),
        provenance_ids=("provenance.fit",),
    )


def _rotate(point: tuple[float, float], angle: float) -> tuple[float, float]:
    cosine = math.cos(angle)
    sine = math.sin(angle)
    return (
        round(cosine * point[0] - sine * point[1], 9),
        round(sine * point[0] + cosine * point[1], 9),
    )


def _graph(
    circle_count: int,
    *,
    ambiguous: bool = False,
    conflicted: bool = False,
    high_uncertainty: bool = False,
    outside: bool = False,
    overlap: bool = False,
    omit_through: bool = False,
    rebound_term: bool = False,
    unknown_depth: bool = False,
) -> VisualFeatureGraph:
    angle = 0.25
    local_corners = ((-100.0, -60.0), (100.0, -60.0), (100.0, 60.0), (-100.0, 60.0))
    outer = GeometryRecord(
        geometry_id="geometry.outer",
        frame_id="frame.metric",
        representation_term_ref_id=VFG_ROTATED_RECTANGLE.term_ref_id,
        intrinsic_dimension=1,
        samples=tuple(
            _sample(
                f"sample.outer.{index:03d}",
                _rotate(point, angle),
                VFG_SAMPLE_CORNER.term_ref_id,
                uncertainty=0.30 if high_uncertainty and index == 0 else 0.05,
            )
            for index, point in enumerate(local_corners)
        ),
        closure=ClosureState.CLOSED,
        state=AssertionState.OBSERVED,
        term_ref_ids=(VFG_OUTER_PROFILE.term_ref_id,),
        provenance_ids=("provenance.fit",),
        advisory_support=0.99,
    )
    circles = []
    for index in range(circle_count):
        local_center = (
            ((index % 4) - 1.5) * 35.0,
            ((index // 4) - 1.5) * 25.0,
        )
        if outside and index == 0:
            local_center = (99.0, 0.0)
        if overlap and index == 1:
            local_center = (-52.5, -37.5)
        center = _rotate(local_center, angle)
        boundary = _rotate((local_center[0] + 3.0, local_center[1]), angle)
        circles.append(
            GeometryRecord(
                geometry_id=f"geometry.circle.{index:03d}",
                frame_id="frame.metric",
                representation_term_ref_id=VFG_CIRCLE.term_ref_id,
                intrinsic_dimension=1,
                samples=(
                    _sample(
                        f"sample.circle.{index:03d}.center",
                        center,
                        VFG_SAMPLE_CENTER.term_ref_id,
                    ),
                    _sample(
                        f"sample.circle.{index:03d}.boundary",
                        boundary,
                        VFG_SAMPLE_BOUNDARY.term_ref_id,
                    ),
                ),
                closure=ClosureState.CLOSED,
                state=AssertionState.OBSERVED,
                term_ref_ids=(VFG_INNER_PROFILE.term_ref_id,),
                provenance_ids=("provenance.fit",),
                advisory_support=0.99,
            )
        )

    nodes = [
        FeatureNode(
            node_id="node.decision",
            layer=EntityLayer.FEATURE,
            term_ref_ids=(VFG_DECISION.term_ref_id,),
            state=AssertionState.OBSERVED,
            provenance_ids=("provenance.human",),
            advisory_support=1.0,
        ),
        FeatureNode(
            node_id="node.component",
            layer=EntityLayer.COMPONENT,
            term_ref_ids=(VFG_COMPONENT.term_ref_id,),
            geometry_ids=(outer.geometry_id, *(item.geometry_id for item in circles)),
            source_ids=("source.photo",),
            state=AssertionState.CONFLICTED if conflicted else AssertionState.OBSERVED,
            provenance_ids=("provenance.fit",),
            advisory_support=0.99,
        ),
    ]
    if ambiguous:
        nodes.append(
            FeatureNode(
                node_id="node.decision.alternative",
                layer=EntityLayer.FEATURE,
                term_ref_ids=(VFG_DECISION.term_ref_id,),
                state=AssertionState.OBSERVED,
                provenance_ids=("provenance.human",),
                advisory_support=1.0,
            )
        )

    relations = [
        FeatureRelation(
            relation_id="relation.decision-subject",
            relation_term_ref_id=VFG_RELATION_DECISION_SUBJECT.term_ref_id,
            endpoints=(
                RelationEndpoint(
                    ordinal=0,
                    role_term_ref_id=VFG_ROLE_DECISION.term_ref_id,
                    element=GraphElementRef(
                        kind=GraphElementKind.NODE,
                        element_id="node.decision",
                    ),
                ),
                RelationEndpoint(
                    ordinal=1,
                    role_term_ref_id=VFG_ROLE_COMPONENT.term_ref_id,
                    element=GraphElementRef(
                        kind=GraphElementKind.NODE,
                        element_id="node.component",
                    ),
                ),
            ),
            state=AssertionState.OBSERVED,
            provenance_ids=("provenance.human",),
            advisory_support=1.0,
        )
    ]
    if not omit_through:
        relations.extend(
            FeatureRelation(
                relation_id=f"relation.through.{index:03d}",
                relation_term_ref_id=VFG_RELATION_THROUGH_EXTENT.term_ref_id,
                endpoints=(
                    RelationEndpoint(
                        ordinal=0,
                        role_term_ref_id=VFG_ROLE_COMPONENT.term_ref_id,
                        element=GraphElementRef(
                            kind=GraphElementKind.NODE,
                            element_id="node.component",
                        ),
                    ),
                    RelationEndpoint(
                        ordinal=1,
                        role_term_ref_id=VFG_ROLE_PROFILE.term_ref_id,
                        element=GraphElementRef(
                            kind=GraphElementKind.GEOMETRY,
                            element_id=circle.geometry_id,
                        ),
                    ),
                ),
                state=AssertionState.OBSERVED,
                provenance_ids=("provenance.fit",),
                advisory_support=0.99,
            )
            for index, circle in enumerate(circles)
        )

    ontology_terms = [as_visual_term(item) for item in VFG_REQUIRED_TERMS]
    if rebound_term:
        index = next(
            index
            for index, item in enumerate(ontology_terms)
            if item.term_ref_id == VFG_CIRCLE.term_ref_id
        )
        ontology_terms[index] = dataclasses.replace(
            ontology_terms[index], term_definition_sha256="f" * 64
        )
    return VisualFeatureGraph(
        scope_id="scope.planar-mechanical-test",
        scope_version=1,
        source_bundle_sha256=_sha("source-bundle"),
        producer_algorithm_id="test.vfg.builder",
        producer_algorithm_version="1.0.0",
        producer_contract_sha256=_sha("producer-contract"),
        ontology_terms=tuple(ontology_terms),
        provenance=(
            ProvenanceRecord(
                provenance_id="provenance.capture",
                kind=ProvenanceKind.SENSOR_CAPTURE,
                content=_content("capture"),
                producer_id="camera",
                producer_version="1.0",
                source_ids=("source.photo",),
            ),
            ProvenanceRecord(
                provenance_id="provenance.fit",
                kind=ProvenanceKind.DETERMINISTIC_DERIVATION,
                content=_content("fit"),
                producer_id="geometry.fit",
                producer_version="1.0",
                source_ids=("source.photo",),
                parent_provenance_ids=("provenance.capture",),
            ),
            ProvenanceRecord(
                provenance_id="provenance.human",
                kind=ProvenanceKind.HUMAN_CONFIRMATION,
                content=_content("confirmation"),
                producer_id="human.confirmation",
                producer_version="1.0",
                source_ids=("source.photo",),
                parent_provenance_ids=("provenance.fit",),
            ),
        ),
        sources=(
            SourceArtifact(
                source_id="source.photo",
                content=_content("photo", "image/jpeg"),
                modality_term_ref_ids=(VFG_MODALITY_IMAGE.term_ref_id,),
                provenance_ids=("provenance.capture",),
            ),
        ),
        frames=(
            CoordinateFrame(
                frame_id="frame.metric",
                source_id="source.photo",
                binding=MetricPlaneFrameBinding(
                    frame_record_sha256=_sha("frame"),
                    calibration_receipt_sha256=_sha("calibration-receipt"),
                    calibration_sha256=_sha("calibration"),
                ),
                provenance_ids=("provenance.fit",),
            ),
        ),
        geometries=(outer, *circles),
        nodes=tuple(nodes),
        relations=tuple(relations),
        measurements=(
            MeasurementRecord(
                measurement_id="measurement.depth",
                quantity_term_ref_id=VFG_QUANTITY_DEPTH.term_ref_id,
                unit_term_ref_id=VFG_UNIT_MM.term_ref_id,
                targets=(
                    GraphElementRef(
                        kind=GraphElementKind.NODE,
                        element_id="node.component",
                    ),
                ),
                estimate=(
                    MeasurementEstimate(kind=MeasurementEstimateKind.UNKNOWN)
                    if unknown_depth
                    else MeasurementEstimate(
                        kind=MeasurementEstimateKind.EXACT,
                        central=(8.0,),
                    )
                ),
                frame_ids=("frame.metric",),
                state=AssertionState.UNKNOWN if unknown_depth else AssertionState.OBSERVED,
                provenance_ids=("provenance.fit",),
                advisory_support=0.99,
            ),
        ),
    )


class _Reader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        assert len(self.payload) <= maximum_bytes
        return self.payload


def _document(graph: VisualFeatureGraph) -> tuple[DocumentRef, bytes]:
    payload = encode_visual_feature_graph(graph)
    return (
        DocumentRef(
            artifact_id="evidence.visual",
            role_term_ref_id=ROLE_VISUAL_EVIDENCE.term_ref_id,
            schema_term_ref_id=VISUAL_FEATURE_GRAPH_SCHEMA_TERM.term_ref_id,
            document_id=graph.graph_id,
            document_digest=graph.graph_digest,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type=VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
        ),
        payload,
    )


def _request(compiler, document: DocumentRef, payload_size: int) -> IntentCompileRequest:
    return IntentCompileRequest(
        compiler=compiler.descriptor,
        terms=planar_mechanical_v1_request_terms(),
        documents=(document,),
        inputs=(
            CompileInputBinding(
                binding_id="input.visual",
                ordinal=0,
                role_term_ref_id=ROLE_VISUAL_EVIDENCE.term_ref_id,
                artifact_id=document.artifact_id,
            ),
        ),
        requested_outputs=(
            RequestedOutput(
                output_id="output.sketch",
                ordinal=0,
                role_term_ref_id=ROLE_SKETCH_INTENT.term_ref_id,
                schema_term_ref_id=SKETCH_INTENT_GRAPH_SCHEMA_TERM.term_ref_id,
            ),
            RequestedOutput(
                output_id="output.parametric",
                ordinal=1,
                role_term_ref_id=ROLE_PARAMETRIC_INTENT.term_ref_id,
                schema_term_ref_id=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM.term_ref_id,
            ),
        ),
        budget=BridgeBudget(
            max_input_bytes=payload_size,
            max_output_bytes=512 * 1024,
            max_subject_lookups=6,
            max_rule_applications=2,
        ),
    )


def _compile(graph: VisualFeatureGraph, publisher=None):
    publisher = publisher or InMemoryIntentArtifactPublisher()
    stack = build_planar_mechanical_v1_stack(publisher=publisher)
    document, payload = _document(graph)
    request = _request(stack.compiler, document, len(payload))
    result = stack.compiler.compile(
        request,
        artifacts=_Reader(payload),
        codecs=stack.codecs,
        proof_policy=stack.proof_policy,
    )
    return result, publisher, stack, request, payload


@pytest.mark.parametrize("circle_count", [0, 1, 16])
def test_zero_one_and_sixteen_circles_compile_atomically_and_deterministically(
    circle_count: int,
) -> None:
    result, publisher, stack, request, payload = _compile(_graph(circle_count))

    assert result.disposition is BridgeDisposition.COMPLETE
    assert len(result.output_documents) == 2
    assert result.output_documents == publisher.published_documents
    assert result.proof_bundle is not None
    assert len(result.proof_bundle.assertions) == 2
    by_media = {item.media_type: item for item in result.output_documents}
    sketch_document = by_media[SKETCH_INTENT_GRAPH_MEDIA_TYPE]
    parametric_document = by_media[PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE]
    sketch = decode_sketch_intent_graph(publisher.read(sketch_document, 512 * 1024))
    parametric = decode_parametric_feature_graph_v2(
        publisher.read(parametric_document, 512 * 1024),
        expected_sha256=parametric_document.document_digest,
    )
    assert len(sketch.geometries) == 1 + circle_count
    assert len(parametric.nodes) == 2 + circle_count
    operations = {item.intent.operation_term_ref_id for item in parametric.nodes}
    assert PFG_OPERATION_ADD.term_ref_id in operations
    assert (PFG_OPERATION_REMOVE.term_ref_id in operations) is (circle_count > 0)
    parametric_text = parametric.canonical_bytes.decode()
    assert all(term not in parametric_text for term in ("Pad", "Hole", "FreeCAD", "TypeId"))

    repeated = stack.compiler.compile(
        request,
        artifacts=_Reader(payload),
        codecs=stack.codecs,
        proof_policy=stack.proof_policy,
    )
    assert repeated == result
    assert publisher.published_documents == result.output_documents


@pytest.mark.parametrize(
    ("count", "options"),
    [
        (17, {}),
        (1, {"outside": True}),
        (2, {"overlap": True}),
        (1, {"omit_through": True}),
        (1, {"high_uncertainty": True}),
        (1, {"ambiguous": True}),
        (1, {"conflicted": True}),
        (1, {"unknown_depth": True}),
        (1, {"rebound_term": True}),
    ],
)
def test_out_of_scope_conflicted_or_untrusted_evidence_is_inert(
    count: int,
    options: dict[str, bool],
) -> None:
    result, publisher, *_ = _compile(_graph(count, **options))

    assert result.disposition is BridgeDisposition.INERT
    assert not result.output_documents
    assert not publisher.published_documents


class _FailingPublisher:
    def __init__(self) -> None:
        self.published_documents: tuple[DocumentRef, ...] = ()
        self._descriptor = ArtifactPublisherDescriptor(
            publisher_id="publisher.pm1.failure-test",
            publisher_version="1.0.0",
            publisher_contract_sha256=_sha("publisher-failure"),
        )

    @property
    def descriptor(self) -> ArtifactPublisherDescriptor:
        return self._descriptor

    def publish_atomic(self, request_digest, documents, maximum_total_bytes):
        raise RuntimeError("injected failure")

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        raise KeyError(document.artifact_id)


def test_publisher_failure_leaves_zero_output() -> None:
    publisher = _FailingPublisher()
    with pytest.raises(IntentBridgeError):
        _compile(_graph(1), publisher)
    assert not publisher.published_documents


class _TamperingRuleSet:
    def __init__(self) -> None:
        self.inner = PlanarMechanicalV1RuleSet()

    @property
    def descriptor(self):
        return self.inner.descriptor

    def emit(self, context):
        emission = self.inner.emit(context)
        documents = []
        for item in emission.documents:
            if item.document.media_type != SKETCH_INTENT_GRAPH_MEDIA_TYPE:
                documents.append(item)
                continue
            graph = decode_sketch_intent_graph(item.payload)
            graph = dataclasses.replace(graph, graph_id=f"{graph.graph_id}.tampered")
            payload = encode_sketch_intent_graph(graph)
            documents.append(
                CompiledIntentDocument.create(
                    output_id=item.output_id,
                    artifact_id=item.document.artifact_id,
                    role_term_ref_id=item.document.role_term_ref_id,
                    schema_term_ref_id=item.document.schema_term_ref_id,
                    document_id=graph.graph_id,
                    document_digest=graph.graph_sha256,
                    media_type=item.document.media_type,
                    payload=payload,
                )
            )
        return RuleSetEmission(
            documents=tuple(documents),
            terms=emission.terms,
            assertions=emission.assertions,
        )


def test_semantically_tampered_output_is_rejected_before_publication() -> None:
    graph = _graph(1)
    document, payload = _document(graph)
    publisher = InMemoryIntentArtifactPublisher()
    policy = build_planar_mechanical_v1_proof_policy()
    compiler = RuleDrivenIntentCompiler(
        compiler_id="planar_mechanical_v1_tamper_test",
        source_adapters=(PlanarMechanicalV1VFGSourceAdapter(),),
        rule_catalog=TrustedIntentRuleCatalog(
            (_TamperingRuleSet(),),
            proof_policy_catalog_sha256=policy.catalog_sha256,
        ),
        publisher=publisher,
    )
    stack = build_planar_mechanical_v1_stack(publisher=InMemoryIntentArtifactPublisher())

    with pytest.raises(IntentBridgeError):
        compiler.compile(
            _request(compiler, document, len(payload)),
            artifacts=_Reader(payload),
            codecs=stack.codecs,
            proof_policy=policy,
        )
    assert not publisher.published_documents


def test_input_content_tamper_is_rejected_before_publication() -> None:
    graph = _graph(1)
    document, payload = _document(graph)
    publisher = InMemoryIntentArtifactPublisher()
    stack = build_planar_mechanical_v1_stack(publisher=publisher)

    with pytest.raises(IntentBridgeError):
        stack.compiler.compile(
            _request(stack.compiler, document, len(payload)),
            artifacts=_Reader(payload[:-1] + bytes([payload[-1] ^ 1])),
            codecs=stack.codecs,
            proof_policy=stack.proof_policy,
        )
    assert not publisher.published_documents
