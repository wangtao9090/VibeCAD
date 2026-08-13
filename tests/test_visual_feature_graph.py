"""Focused contract tests for the backend-neutral VisualFeatureGraph."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from vibecad.visual.feature_graph import (
    MAX_GRAPH_ALTERNATIVES_PER_SET,
    MAX_GRAPH_NODES,
    MAX_GRAPH_ONTOLOGY_TERMS,
    MAX_GRAPH_TOTAL_INLINE_SAMPLES,
    MAX_GRAPH_TOTAL_TERM_REFS,
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
    GenericFrameBinding,
    GeometryRecord,
    GraphElementKind,
    GraphElementRef,
    Handedness,
    HypothesisAlternative,
    HypothesisSet,
    MeasurementEstimate,
    MeasurementEstimateKind,
    MeasurementRecord,
    MetricPlaneFrameBinding,
    MetricSpaceFrameBinding,
    MetricUncertainty,
    MetricUncertaintyKind,
    OntologyTermRef,
    OverviewNormalizedFrameBinding,
    ProvenanceKind,
    ProvenanceRecord,
    RelationEndpoint,
    SourceArtifact,
    SourcePixelFrameBinding,
    TopologyCell,
    VisualFeatureGraph,
    VisualFeatureGraphError,
    VisualFeatureGraphErrorCode,
    VisualGraphAuthority,
    decode_visual_feature_graph,
    encode_visual_feature_graph,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _content(label: str, media_type: str = "application/json") -> ContentRef:
    return ContentRef(sha256=_sha(label), size_bytes=128, media_type=media_type)


_TERM_SPECS = {
    "axis.x": "axis/x",
    "axis.y": "axis/y",
    "axis.z": "axis/z",
    "cell.loop": "topology/loop",
    "coord.cartesian": "coordinate/cartesian",
    "entity.component": "entity/component",
    "entity.object": "entity/object",
    "entity.region": "entity/region",
    "extension.schema": "extension/schema",
    "geometry.freeform": "geometry/freeform-surface",
    "geometry.polyline": "geometry/polyline",
    "hypothesis.shape": "hypothesis/shape",
    "modality.image": "modality/raster-image",
    "relation.partof": "relation/part-of",
    "role.part": "role/part",
    "role.whole": "role/whole",
    "transform.projective": "transform/projective",
    "appearance.material": "appearance/material",
    "quantity.color": "quantity/color-channel",
    "quantity.length": "quantity/length",
    "unit.mm": "unit/millimetre",
    "unit.scalar": "unit/scalar",
}


def _term(ref_id: str, term_id: str | None = None) -> OntologyTermRef:
    semantic_id = _TERM_SPECS.get(ref_id, term_id or ref_id.replace(".", "/"))
    return OntologyTermRef(
        term_ref_id=ref_id,
        namespace="vfg",
        vocabulary_version="1.0",
        term_id=semantic_id,
        term_definition_sha256=_sha(f"term:{semantic_id}"),
    )


def _terms() -> tuple[OntologyTermRef, ...]:
    return tuple(_term(ref_id) for ref_id in reversed(tuple(_TERM_SPECS)))


def _ref(kind: GraphElementKind, identifier: str) -> GraphElementRef:
    return GraphElementRef(kind=kind, element_id=identifier)


def _sample(identifier: str, x: float, y: float) -> CoordinateSample:
    return CoordinateSample(
        sample_id=identifier,
        coordinates=(x, y),
        uncertainty=MetricUncertainty(
            kind=MetricUncertaintyKind.AXIS_BOUNDS,
            bounds=(0.05, 0.05),
        ),
        provenance_ids=("prov.fit",),
    )


def _graph(**changes: object) -> VisualFeatureGraph:
    terms = _terms()
    extension = ExtensionRef(
        extension_id="extension.vendor",
        namespace="vendor",
        vocabulary_version="1.0",
        schema_term_ref_id="extension.schema",
        payload=ContentRef(
            sha256=_sha("extension"),
            size_bytes=64,
            media_type="application/vnd.vendor.graph+json",
            schema_term_ref_id="extension.schema",
        ),
    )
    provenance = (
        ProvenanceRecord(
            provenance_id="prov.fit",
            kind=ProvenanceKind.DETERMINISTIC_DERIVATION,
            content=_content("fit-receipt"),
            producer_id="fit.engine",
            producer_version="1.0",
            source_ids=("source.primary",),
            parent_provenance_ids=("prov.provider",),
        ),
        ProvenanceRecord(
            provenance_id="prov.provider",
            kind=ProvenanceKind.PROVIDER_OUTPUT,
            content=_content("provider-output"),
            producer_id="vision.model",
            producer_version="2026.08",
            source_ids=("source.primary",),
            parent_provenance_ids=("prov.capture",),
        ),
        ProvenanceRecord(
            provenance_id="prov.capture",
            kind=ProvenanceKind.SENSOR_CAPTURE,
            content=_content("capture-receipt"),
            producer_id="camera.capture",
            producer_version="1.0",
            source_ids=("source.primary",),
        ),
    )
    sources = (
        SourceArtifact(
            source_id="source.primary",
            content=_content("source-image", "image/png"),
            modality_term_ref_ids=("modality.image",),
            provenance_ids=("prov.capture",),
            extension_ids=("extension.vendor",),
        ),
    )
    frames = (
        CoordinateFrame(
            frame_id="frame.metric",
            source_id="source.primary",
            binding=MetricPlaneFrameBinding(
                frame_record_sha256=_sha("metric-frame"),
                calibration_receipt_sha256=_sha("calibration-receipt"),
                calibration_sha256=_sha("calibration"),
            ),
            provenance_ids=("prov.fit",),
        ),
        CoordinateFrame(
            frame_id="frame.normalized",
            source_id="source.primary",
            binding=OverviewNormalizedFrameBinding(
                collection_id="collection.photo",
                collection_manifest_sha256=_sha("collection"),
                derivation_manifest_sha256=_sha("batch"),
                provider_asset_id="provider.overview",
                provider_asset_sha256=_sha("provider-overview"),
                width=1024,
                height=768,
            ),
            provenance_ids=("prov.provider",),
        ),
        CoordinateFrame(
            frame_id="frame.pixel",
            source_id="source.primary",
            binding=SourcePixelFrameBinding(
                source_sha256=_sha("source-image"),
                width=2048,
                height=1536,
            ),
            provenance_ids=("prov.capture",),
        ),
        CoordinateFrame(
            frame_id="frame.space",
            binding=MetricSpaceFrameBinding(
                frame_record_sha256=_sha("space-frame"),
                handedness=Handedness.RIGHT_HANDED,
                axis_term_ref_ids=("axis.x", "axis.y", "axis.z"),
            ),
            provenance_ids=("prov.fit",),
        ),
        CoordinateFrame(
            frame_id="frame.generic",
            binding=GenericFrameBinding(
                frame_record_sha256=_sha("generic-frame"),
                dimension=2,
                coordinate_system_term_ref_id="coord.cartesian",
                axis_term_ref_ids=("axis.x", "axis.y"),
                unit_term_ref_ids=("unit.scalar", "unit.scalar"),
            ),
        ),
    )
    transforms = (
        FrameTransformRef(
            transform_id="transform.metric",
            from_frame_id="frame.pixel",
            to_frame_id="frame.metric",
            transform_term_ref_id="transform.projective",
            receipt=_content("pixel-to-metric"),
            provenance_ids=("prov.fit",),
        ),
        FrameTransformRef(
            transform_id="transform.pixel",
            from_frame_id="frame.normalized",
            to_frame_id="frame.pixel",
            transform_term_ref_id="transform.projective",
            receipt=_content("normalized-to-pixel"),
            provenance_ids=("prov.fit",),
        ),
    )
    samples = (
        _sample("sample.a", 0.0, 0.0),
        _sample("sample.b", 80.0, 0.0),
        _sample("sample.c", 80.0, 160.0),
        _sample("sample.d", 0.0, 160.0),
    )
    geometries = (
        GeometryRecord(
            geometry_id="geometry.freeform",
            frame_id="frame.space",
            representation_term_ref_id="geometry.freeform",
            intrinsic_dimension=2,
            artifact=_content("freeform-patch", "model/vnd.vfg.surface"),
            closure=ClosureState.OPEN,
            state=AssertionState.INFERRED,
            provenance_ids=("prov.provider",),
            advisory_support=0.7,
        ),
        GeometryRecord(
            geometry_id="geometry.outline",
            frame_id="frame.metric",
            representation_term_ref_id="geometry.polyline",
            intrinsic_dimension=1,
            samples=samples,
            cells=(
                TopologyCell(
                    cell_id="cell.outline",
                    cell_term_ref_id="cell.loop",
                    sample_ids=tuple(item.sample_id for item in samples),
                    orientation=CellOrientation.POSITIVE,
                    provenance_ids=("prov.fit",),
                ),
            ),
            closure=ClosureState.CLOSED,
            state=AssertionState.OBSERVED,
            provenance_ids=("prov.fit",),
        ),
    )
    nodes = (
        FeatureNode(
            node_id="node.object",
            layer=EntityLayer.OBJECT,
            term_ref_ids=("entity.object",),
            geometry_ids=("geometry.outline",),
            source_ids=("source.primary",),
            state=AssertionState.OBSERVED,
            provenance_ids=("prov.fit",),
            extension_ids=("extension.vendor",),
        ),
        FeatureNode(
            node_id="node.component",
            layer=EntityLayer.COMPONENT,
            term_ref_ids=("entity.component",),
            geometry_ids=("geometry.freeform",),
            state=AssertionState.INFERRED,
            provenance_ids=("prov.provider",),
        ),
        FeatureNode(
            node_id="node.hyp.a",
            layer=EntityLayer.REGION,
            term_ref_ids=("entity.region",),
            state=AssertionState.INFERRED,
            provenance_ids=("prov.provider",),
        ),
        FeatureNode(
            node_id="node.hyp.b",
            layer=EntityLayer.REGION,
            term_ref_ids=("entity.region",),
            state=AssertionState.INFERRED,
            provenance_ids=("prov.provider",),
        ),
        FeatureNode(
            node_id="node.view.a",
            layer=EntityLayer.OBJECT,
            term_ref_ids=("entity.object",),
            geometry_ids=("geometry.outline",),
            state=AssertionState.OBSERVED,
        ),
        FeatureNode(
            node_id="node.view.b",
            layer=EntityLayer.OBJECT,
            term_ref_ids=("entity.object",),
            geometry_ids=("geometry.outline",),
            state=AssertionState.OBSERVED,
        ),
    )
    relations = (
        FeatureRelation(
            relation_id="relation.component",
            relation_term_ref_id="relation.partof",
            endpoints=(
                RelationEndpoint(
                    ordinal=1,
                    role_term_ref_id="role.part",
                    element=_ref(GraphElementKind.NODE, "node.component"),
                ),
                RelationEndpoint(
                    ordinal=0,
                    role_term_ref_id="role.whole",
                    element=_ref(GraphElementKind.NODE, "node.object"),
                ),
            ),
            state=AssertionState.INFERRED,
            provenance_ids=("prov.provider",),
        ),
    )
    measurements = (
        MeasurementRecord(
            measurement_id="measurement.color",
            quantity_term_ref_id="quantity.color",
            unit_term_ref_id="unit.scalar",
            targets=(_ref(GraphElementKind.NODE, "node.object"),),
            estimate=MeasurementEstimate(
                kind=MeasurementEstimateKind.EXACT,
                central=(0.1, 0.1, 0.1),
            ),
            state=AssertionState.OBSERVED,
            provenance_ids=("prov.provider",),
        ),
        MeasurementRecord(
            measurement_id="measurement.length",
            quantity_term_ref_id="quantity.length",
            unit_term_ref_id="unit.mm",
            targets=(_ref(GraphElementKind.GEOMETRY, "geometry.outline"),),
            estimate=MeasurementEstimate(
                kind=MeasurementEstimateKind.INTERVAL,
                central=(80.0,),
                lower=(79.8,),
                upper=(80.2,),
            ),
            frame_ids=("frame.metric", "frame.pixel"),
            transform_ids=("transform.metric",),
            state=AssertionState.OBSERVED,
            provenance_ids=("prov.fit",),
        ),
    )
    appearances = (
        AppearanceRecord(
            appearance_id="appearance.surface",
            target_node_id="node.object",
            appearance_term_ref_ids=("appearance.material",),
            channel_measurement_ids=("measurement.color",),
            texture_artifacts=(_content("texture", "image/png"),),
            source_ids=("source.primary",),
            state=AssertionState.OBSERVED,
            provenance_ids=("prov.provider",),
        ),
    )
    hypothesis_sets = (
        HypothesisSet(
            hypothesis_set_id="hypothesis.shape",
            subject_refs=(_ref(GraphElementKind.NODE, "node.object"),),
            alternatives=(
                HypothesisAlternative(
                    alternative_id="alternative.a",
                    member_refs=(_ref(GraphElementKind.NODE, "node.hyp.a"),),
                    advisory_support=0.9,
                    provenance_ids=("prov.provider",),
                ),
                HypothesisAlternative(
                    alternative_id="alternative.b",
                    member_refs=(_ref(GraphElementKind.NODE, "node.hyp.b"),),
                    advisory_support=0.1,
                    provenance_ids=("prov.provider",),
                ),
            ),
            term_ref_ids=("hypothesis.shape",),
            provenance_ids=("prov.provider",),
        ),
    )
    values: dict[str, object] = {
        "scope_id": "scope.photo",
        "scope_version": 1,
        "source_bundle_sha256": _sha("source-bundle"),
        "producer_algorithm_id": "visual.graph.builder",
        "producer_algorithm_version": "1.0",
        "producer_contract_sha256": _sha("producer-contract"),
        "ontology_terms": terms,
        "extensions": (extension,),
        "provenance": provenance,
        "sources": sources,
        "frames": frames,
        "transforms": transforms,
        "geometries": geometries,
        "nodes": nodes,
        "relations": relations,
        "equivalence_groups": (
            EquivalenceGroup(
                group_id="equivalence.views",
                member_node_ids=("node.view.a", "node.view.b"),
                state=AssertionState.OBSERVED,
            ),
        ),
        "measurements": measurements,
        "appearances": appearances,
        "hypothesis_sets": hypothesis_sets,
    }
    values.update(changes)
    return VisualFeatureGraph(**values)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _assert_error(
    callable_: object,
    code: VisualFeatureGraphErrorCode,
) -> None:
    with pytest.raises(VisualFeatureGraphError) as exc_info:
        callable_()  # type: ignore[operator]
    assert exc_info.value.code is code
    assert len(exc_info.value.path.encode("utf-8")) <= 256


def test_round_trip_is_canonical_comprehensive_and_authority_free() -> None:
    graph = _graph()
    raw = encode_visual_feature_graph(graph)
    decoded = decode_visual_feature_graph(raw)
    mapping = decoded.to_mapping()

    assert decoded == graph
    assert encode_visual_feature_graph(decoded) == raw
    assert mapping["authority"] == VisualGraphAuthority.ADVISORY_ONLY.value
    assert mapping["graph_id"].startswith("visual_feature_graph_")
    assert len(mapping["graph_digest"]) == 64
    assert mapping["sources"] and mapping["frames"] and mapping["transforms"]
    assert mapping["geometries"] and mapping["nodes"] and mapping["relations"]
    assert mapping["measurements"] and mapping["appearances"]
    assert mapping["equivalence_groups"] and mapping["hypothesis_sets"]
    assert "selected_alternative_id" not in raw.decode("ascii")
    assert not {
        "feature_kind",
        "backend",
        "operation",
        "acceptance",
        "adoption",
        "task",
        "mcp",
    }.intersection(mapping)


def test_constructor_canonicalizes_semantic_sets_but_preserves_ordered_coordinates() -> None:
    graph = _graph()
    reordered = _graph(
        ontology_terms=tuple(reversed(graph.ontology_terms)),
        provenance=tuple(reversed(graph.provenance)),
        frames=tuple(reversed(graph.frames)),
        nodes=tuple(reversed(graph.nodes)),
        measurements=tuple(reversed(graph.measurements)),
    )

    assert encode_visual_feature_graph(reordered) == encode_visual_feature_graph(graph)
    assert reordered.graph_digest == graph.graph_digest
    assert graph.geometries[1].cells[0].sample_ids == (
        "sample.a",
        "sample.b",
        "sample.c",
        "sample.d",
    )


def test_future_geometry_and_backend_terms_are_inert_data_without_schema_changes() -> None:
    graph = _graph()
    future = _term("future.superquadric", "geometry/superquadric-v8")
    expanded = _graph(ontology_terms=graph.ontology_terms + (future,))
    raw = encode_visual_feature_graph(expanded)

    assert decode_visual_feature_graph(raw) == expanded
    assert expanded.schema_version == graph.schema_version
    assert future.to_mapping() in expanded.to_mapping()["ontology_terms"]
    assert expanded.authority is VisualGraphAuthority.ADVISORY_ONLY


@pytest.mark.parametrize(
    "mutate",
    (
        lambda graph: dataclasses.replace(graph, producer_algorithm_version="1.1"),
        lambda graph: dataclasses.replace(
            graph,
            sources=(
                dataclasses.replace(graph.sources[0], content=_content("changed", "image/png")),
            ),
            frames=tuple(
                dataclasses.replace(
                    frame,
                    binding=dataclasses.replace(
                        frame.binding,
                        source_sha256=_sha("changed"),
                    ),
                )
                if frame.frame_id == "frame.pixel"
                else frame
                for frame in graph.frames
            ),
        ),
        lambda graph: dataclasses.replace(
            graph,
            geometries=tuple(
                dataclasses.replace(
                    geometry,
                    samples=tuple(
                        dataclasses.replace(sample, coordinates=(0.25, 0.0))
                        if sample.sample_id == "sample.a"
                        else sample
                        for sample in geometry.samples
                    ),
                )
                if geometry.geometry_id == "geometry.outline"
                else geometry
                for geometry in graph.geometries
            ),
        ),
        lambda graph: dataclasses.replace(
            graph,
            relations=(dataclasses.replace(graph.relations[0], state=AssertionState.CONFLICTED),),
        ),
        lambda graph: dataclasses.replace(
            graph,
            appearances=(dataclasses.replace(graph.appearances[0], advisory_support=0.5),),
        ),
        lambda graph: dataclasses.replace(
            graph,
            hypothesis_sets=(
                dataclasses.replace(
                    graph.hypothesis_sets[0],
                    alternatives=(
                        dataclasses.replace(
                            graph.hypothesis_sets[0].alternatives[0], advisory_support=0.8
                        ),
                        graph.hypothesis_sets[0].alternatives[1],
                    ),
                ),
            ),
        ),
    ),
)
def test_complete_digest_binds_every_semantic_dimension(mutate: object) -> None:
    graph = _graph()
    changed = mutate(graph)  # type: ignore[operator]

    assert changed.graph_digest != graph.graph_digest
    assert changed.graph_id != graph.graph_id


def test_decoder_rejects_tamper_noncanonical_duplicate_and_unsafe_numbers() -> None:
    raw = encode_visual_feature_graph(_graph())
    mapping = json.loads(raw)
    mapping["producer_algorithm_version"] = "2.0"
    tampered = _canonical(mapping)

    _assert_error(
        lambda: decode_visual_feature_graph(tampered),
        VisualFeatureGraphErrorCode.INTEGRITY_FAILURE,
    )
    _assert_error(
        lambda: decode_visual_feature_graph(b" " + raw),
        VisualFeatureGraphErrorCode.INTEGRITY_FAILURE,
    )
    duplicate = raw[:-1] + b',"graph_id":"visual_feature_graph_' + b"0" * 32 + b'"}'
    _assert_error(
        lambda: decode_visual_feature_graph(duplicate),
        VisualFeatureGraphErrorCode.INVALID_INPUT,
    )
    unsafe = raw.replace(b'"scope_version":1', b'"scope_version":9007199254740992')
    _assert_error(
        lambda: decode_visual_feature_graph(unsafe),
        VisualFeatureGraphErrorCode.INVALID_INPUT,
    )
    nonfinite = raw.replace(b'"scope_version":1', b'"scope_version":NaN')
    _assert_error(
        lambda: decode_visual_feature_graph(nonfinite),
        VisualFeatureGraphErrorCode.INVALID_INPUT,
    )


def test_frames_are_discriminated_and_cross_frame_measurements_require_receipts() -> None:
    graph = _graph()
    bad_pixel = tuple(
        dataclasses.replace(
            frame,
            binding=dataclasses.replace(frame.binding, source_sha256=_sha("other-source")),
        )
        if frame.frame_id == "frame.pixel"
        else frame
        for frame in graph.frames
    )
    _assert_error(
        lambda: _graph(frames=bad_pixel),
        VisualFeatureGraphErrorCode.BINDING_MISMATCH,
    )

    measurement = graph.measurements[1]
    _assert_error(
        lambda: dataclasses.replace(measurement, transform_ids=()),
        VisualFeatureGraphErrorCode.BINDING_MISMATCH,
    )
    unrelated = FrameTransformRef(
        transform_id="transform.unrelated",
        from_frame_id="frame.normalized",
        to_frame_id="frame.generic",
        transform_term_ref_id="transform.projective",
        receipt=_content("unrelated"),
    )
    _assert_error(
        lambda: _graph(
            transforms=graph.transforms + (unrelated,),
            measurements=tuple(
                dataclasses.replace(item, transform_ids=("transform.unrelated",))
                if item.measurement_id == "measurement.length"
                else item
                for item in graph.measurements
            ),
        ),
        VisualFeatureGraphErrorCode.BINDING_MISMATCH,
    )


def test_geometry_rejects_coordinate_space_mixing_and_dangling_topology() -> None:
    graph = _graph()
    normalized = GeometryRecord(
        geometry_id="geometry.normalized",
        frame_id="frame.normalized",
        representation_term_ref_id="geometry.polyline",
        intrinsic_dimension=1,
        samples=(_sample("sample.outside", 1.5, 0.5),),
        state=AssertionState.OBSERVED,
    )
    _assert_error(
        lambda: _graph(geometries=graph.geometries + (normalized,)),
        VisualFeatureGraphErrorCode.BINDING_MISMATCH,
    )
    _assert_error(
        lambda: GeometryRecord(
            geometry_id="geometry.dangling",
            frame_id="frame.metric",
            representation_term_ref_id="geometry.polyline",
            intrinsic_dimension=1,
            samples=(_sample("sample.local", 0.0, 0.0),),
            cells=(
                TopologyCell(
                    cell_id="cell.bad",
                    cell_term_ref_id="cell.loop",
                    sample_ids=("sample.unknown",),
                ),
            ),
        ),
        VisualFeatureGraphErrorCode.UNKNOWN_REFERENCE,
    )


def test_relations_equivalence_and_hypotheses_fail_closed_on_ambiguity() -> None:
    graph = _graph()
    endpoint = RelationEndpoint(
        ordinal=0,
        role_term_ref_id="role.whole",
        element=_ref(GraphElementKind.NODE, "node.object"),
    )
    _assert_error(
        lambda: FeatureRelation(
            relation_id="relation.self",
            relation_term_ref_id="relation.partof",
            endpoints=(endpoint, dataclasses.replace(endpoint, ordinal=1)),
        ),
        VisualFeatureGraphErrorCode.INVALID_INPUT,
    )
    _assert_error(
        lambda: _graph(
            relations=(
                graph.relations[0],
                dataclasses.replace(graph.relations[0], relation_id="relation.duplicate"),
            )
        ),
        VisualFeatureGraphErrorCode.INVALID_INPUT,
    )
    _assert_error(
        lambda: _graph(
            equivalence_groups=graph.equivalence_groups
            + (
                EquivalenceGroup(
                    group_id="equivalence.overlap",
                    member_node_ids=("node.view.a", "node.object"),
                ),
            )
        ),
        VisualFeatureGraphErrorCode.INVALID_INPUT,
    )
    alternative = graph.hypothesis_sets[0].alternatives[0]
    _assert_error(
        lambda: HypothesisSet(
            hypothesis_set_id="hypothesis.overlap",
            subject_refs=(_ref(GraphElementKind.NODE, "node.object"),),
            alternatives=(alternative, dataclasses.replace(alternative, alternative_id="alt.x")),
        ),
        VisualFeatureGraphErrorCode.INVALID_INPUT,
    )


def test_flat_dags_reject_source_and_provenance_cycles_without_recursion() -> None:
    graph = _graph()
    source_a = dataclasses.replace(
        graph.sources[0], source_id="source.a", parent_source_id="source.b"
    )
    source_b = dataclasses.replace(
        graph.sources[0], source_id="source.b", parent_source_id="source.a"
    )
    _assert_error(
        lambda: VisualFeatureGraph(
            scope_id="scope.source-cycle",
            scope_version=1,
            source_bundle_sha256=_sha("bundle"),
            producer_algorithm_id="builder",
            producer_algorithm_version="1.0",
            producer_contract_sha256=_sha("contract"),
            ontology_terms=(_term("modality.image"),),
            sources=(
                dataclasses.replace(source_a, provenance_ids=(), extension_ids=()),
                dataclasses.replace(source_b, provenance_ids=(), extension_ids=()),
            ),
        ),
        VisualFeatureGraphErrorCode.INVALID_INPUT,
    )
    prov_a = ProvenanceRecord(
        provenance_id="prov.a",
        kind=ProvenanceKind.UNKNOWN,
        content=_content("prov-a"),
        producer_id="producer.a",
        producer_version="1.0",
        parent_provenance_ids=("prov.b",),
    )
    prov_b = dataclasses.replace(
        prov_a,
        provenance_id="prov.b",
        parent_provenance_ids=("prov.a",),
    )
    _assert_error(
        lambda: VisualFeatureGraph(
            scope_id="scope.provenance-cycle",
            scope_version=1,
            source_bundle_sha256=_sha("bundle"),
            producer_algorithm_id="builder",
            producer_algorithm_version="1.0",
            producer_contract_sha256=_sha("contract"),
            ontology_terms=(),
            provenance=(prov_a, prov_b),
        ),
        VisualFeatureGraphErrorCode.INVALID_INPUT,
    )


def test_budget_boundaries_reject_n_plus_one_and_total_reference_explosion() -> None:
    term = _term("entity.object")
    nodes = tuple(
        FeatureNode(
            node_id=f"node.{index:03d}",
            layer=EntityLayer.OBJECT,
            term_ref_ids=(term.term_ref_id,),
        )
        for index in range(MAX_GRAPH_NODES + 1)
    )
    _assert_error(
        lambda: VisualFeatureGraph(
            scope_id="scope.budget",
            scope_version=1,
            source_bundle_sha256=_sha("bundle"),
            producer_algorithm_id="builder",
            producer_algorithm_version="1.0",
            producer_contract_sha256=_sha("contract"),
            ontology_terms=(term,),
            nodes=nodes,
        ),
        VisualFeatureGraphErrorCode.BUDGET_EXCEEDED,
    )
    too_many_terms = tuple(
        _term(f"term.{index:03d}") for index in range(MAX_GRAPH_ONTOLOGY_TERMS + 1)
    )
    _assert_error(
        lambda: dataclasses.replace(_graph(), ontology_terms=too_many_terms),
        VisualFeatureGraphErrorCode.BUDGET_EXCEEDED,
    )
    eight_terms = tuple(_term(f"future.{index}") for index in range(8))
    term_heavy_nodes = tuple(
        FeatureNode(
            node_id=f"heavy.{index:03d}",
            layer=EntityLayer.ATTRIBUTE,
            term_ref_ids=tuple(item.term_ref_id for item in eight_terms),
        )
        for index in range(MAX_GRAPH_TOTAL_TERM_REFS // 8 + 1)
    )
    _assert_error(
        lambda: VisualFeatureGraph(
            scope_id="scope.refs",
            scope_version=1,
            source_bundle_sha256=_sha("bundle"),
            producer_algorithm_id="builder",
            producer_algorithm_version="1.0",
            producer_contract_sha256=_sha("contract"),
            ontology_terms=eight_terms,
            nodes=term_heavy_nodes,
        ),
        VisualFeatureGraphErrorCode.BUDGET_EXCEEDED,
    )
    alternatives = tuple(
        HypothesisAlternative(
            alternative_id=f"alternative.{index}",
            member_refs=(_ref(GraphElementKind.NODE, f"node.{index}"),),
        )
        for index in range(MAX_GRAPH_ALTERNATIVES_PER_SET + 1)
    )
    _assert_error(
        lambda: HypothesisSet(
            hypothesis_set_id="hypothesis.too.large",
            subject_refs=(_ref(GraphElementKind.NODE, "node.subject"),),
            alternatives=alternatives,
        ),
        VisualFeatureGraphErrorCode.BUDGET_EXCEEDED,
    )


def test_total_inline_sample_budget_is_enforced_before_expensive_graph_work() -> None:
    term = _term("geometry.polyline")
    frame_term = _term("coord.cartesian")
    axis_x = _term("axis.x")
    axis_y = _term("axis.y")
    unit = _term("unit.scalar")
    frame = CoordinateFrame(
        frame_id="frame.generic",
        binding=GenericFrameBinding(
            frame_record_sha256=_sha("frame"),
            dimension=2,
            coordinate_system_term_ref_id=frame_term.term_ref_id,
            axis_term_ref_ids=(axis_x.term_ref_id, axis_y.term_ref_id),
            unit_term_ref_ids=(unit.term_ref_id, unit.term_ref_id),
        ),
    )
    counts = (256, 256, MAX_GRAPH_TOTAL_INLINE_SAMPLES - 512 + 1)
    geometries = tuple(
        GeometryRecord(
            geometry_id=f"geometry.{group}",
            frame_id=frame.frame_id,
            representation_term_ref_id=term.term_ref_id,
            intrinsic_dimension=1,
            samples=tuple(
                CoordinateSample(
                    sample_id=f"sample.{group}.{index:03d}",
                    coordinates=(float(index), float(group)),
                    uncertainty=MetricUncertainty(kind=MetricUncertaintyKind.UNKNOWN),
                )
                for index in range(count)
            ),
        )
        for group, count in enumerate(counts)
    )
    _assert_error(
        lambda: VisualFeatureGraph(
            scope_id="scope.samples",
            scope_version=1,
            source_bundle_sha256=_sha("bundle"),
            producer_algorithm_id="builder",
            producer_algorithm_version="1.0",
            producer_contract_sha256=_sha("contract"),
            ontology_terms=(term, frame_term, axis_x, axis_y, unit),
            frames=(frame,),
            geometries=geometries,
        ),
        VisualFeatureGraphErrorCode.BUDGET_EXCEEDED,
    )


def test_contract_has_stdlib_only_dependencies_and_no_cad_execution_schema() -> None:
    path = Path(__file__).parents[1] / "src/vibecad/visual/feature_graph.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots <= {
        "__future__",
        "dataclasses",
        "enum",
        "hashlib",
        "hmac",
        "json",
        "math",
        "re",
    }
    fields = set(VisualFeatureGraph.__dataclass_fields__)
    assert not {
        "feature_kind",
        "backend",
        "operation",
        "capability",
        "acceptance",
        "task",
        "mcp",
    }.intersection(fields)


def test_malformed_unicode_huge_numbers_and_deep_json_fail_bounded() -> None:
    _assert_error(
        lambda: OntologyTermRef(
            term_ref_id="term.bad",
            namespace="vfg",
            vocabulary_version="1.0",
            term_id="bad\ud800",
            term_definition_sha256=_sha("bad"),
        ),
        VisualFeatureGraphErrorCode.INVALID_INPUT,
    )
    _assert_error(
        lambda: MeasurementEstimate(
            kind=MeasurementEstimateKind.EXACT,
            central=(10**10000,),
        ),
        VisualFeatureGraphErrorCode.INVALID_INPUT,
    )
    _assert_error(
        lambda: CoordinateSample(
            sample_id="sample.non-psd",
            coordinates=(0.0, 0.0),
            uncertainty=MetricUncertainty(
                kind=MetricUncertaintyKind.COVARIANCE,
                covariance=(1.0, 2.0, 2.0, 1.0),
            ),
        ),
        VisualFeatureGraphErrorCode.INVALID_INPUT,
    )
    deep = b'{"x":' + b"[" * 34 + b"0" + b"]" * 34 + b"}"
    _assert_error(
        lambda: decode_visual_feature_graph(deep),
        VisualFeatureGraphErrorCode.BUDGET_EXCEEDED,
    )
