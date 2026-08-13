"""Focused tests for the backend-neutral sketch-intent boundary."""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

import vibecad.sketch.contracts as sketch_contracts
from vibecad.sketch import (
    MAX_SKETCH_ANCHORS_PER_NODE,
    MAX_SKETCH_INTENT_BYTES,
    MAX_SKETCH_INTENT_TERMS,
    MAX_SKETCH_ONTOLOGY_TERMS,
    SKETCH_INTENT_SCHEMA_VERSION,
    SKETCH_ONTOLOGY_SCHEMA_VERSION,
    SketchAnchor,
    SketchAnchorSlotSignature,
    SketchAnchorTargetKind,
    SketchConstraintNode,
    SketchElementKind,
    SketchElementRef,
    SketchGeometryNode,
    SketchIntentError,
    SketchIntentErrorCode,
    SketchIntentGraph,
    SketchOntologyCatalog,
    SketchOntologyError,
    SketchOntologyErrorCode,
    SketchOntologyTermDefinition,
    SketchOntologyTermRef,
    SketchProperty,
    SketchPropertySignature,
    SketchResultPort,
    SketchResultPortSignature,
    SketchTermKind,
    SketchTypedValue,
    SketchValueKind,
    decode_sketch_intent_graph,
    define_sketch_term,
    encode_sketch_intent_graph,
    resolve_sketch_intent,
    sketch_term_ref_from_visual_mapping,
)
from vibecad.visual.feature_graph import OntologyTermRef


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _unknown_ref(term_ref_id: str) -> SketchOntologyTermRef:
    return SketchOntologyTermRef(
        term_ref_id=term_ref_id,
        namespace="example.open-sketch",
        vocabulary_version="1.0",
        term_id=term_ref_id,
        term_definition_sha256=_sha(term_ref_id),
    )


def _core_ontology() -> SketchOntologyCatalog:
    unit_mm = define_sketch_term(
        term_ref_id="unit.mm",
        namespace="vibecad.sketch.core",
        vocabulary_version="1.0",
        term_id="unit/mm",
        kind=SketchTermKind.UNIT,
    )
    property_start = define_sketch_term(
        term_ref_id="property.start",
        namespace="vibecad.sketch.core",
        vocabulary_version="1.0",
        term_id="property/start",
        kind=SketchTermKind.PROPERTY,
    )
    property_end = define_sketch_term(
        term_ref_id="property.end",
        namespace="vibecad.sketch.core",
        vocabulary_version="1.0",
        term_id="property/end",
        kind=SketchTermKind.PROPERTY,
    )
    property_style = define_sketch_term(
        term_ref_id="property.style",
        namespace="vibecad.sketch.core",
        vocabulary_version="1.0",
        term_id="property/style",
        kind=SketchTermKind.PROPERTY,
    )
    value_vector2 = define_sketch_term(
        term_ref_id="value.vector2",
        namespace="vibecad.sketch.core",
        vocabulary_version="1.0",
        term_id="value/vector2",
        kind=SketchTermKind.VALUE_TYPE,
    )
    value_semantic = define_sketch_term(
        term_ref_id="value.semantic",
        namespace="vibecad.sketch.core",
        vocabulary_version="1.0",
        term_id="value/semantic",
        kind=SketchTermKind.VALUE_TYPE,
    )
    value_endpoint = define_sketch_term(
        term_ref_id="value.endpoint",
        namespace="vibecad.sketch.core",
        vocabulary_version="1.0",
        term_id="value/endpoint",
        kind=SketchTermKind.VALUE_TYPE,
    )
    anchor_whole = define_sketch_term(
        term_ref_id="anchor.whole",
        namespace="vibecad.sketch.core",
        vocabulary_version="1.0",
        term_id="anchor/whole",
        kind=SketchTermKind.ANCHOR_ROLE,
    )
    anchor_start = define_sketch_term(
        term_ref_id="anchor.start",
        namespace="vibecad.sketch.core",
        vocabulary_version="1.0",
        term_id="anchor/start",
        kind=SketchTermKind.ANCHOR_ROLE,
    )
    geometry_line = define_sketch_term(
        term_ref_id="geometry.line",
        namespace="vibecad.sketch.core",
        vocabulary_version="1.0",
        term_id="geometry/line",
        kind=SketchTermKind.GEOMETRY,
        properties=(
            SketchPropertySignature(
                property_term_ref_id="property.end",
                value_kinds=(SketchValueKind.VECTOR,),
                value_type_term_ref_ids=("value.vector2",),
                unit_term_ref_ids=("unit.mm",),
            ),
            SketchPropertySignature(
                property_term_ref_id="property.start",
                value_kinds=(SketchValueKind.VECTOR,),
                value_type_term_ref_ids=("value.vector2",),
                unit_term_ref_ids=("unit.mm",),
            ),
            SketchPropertySignature(
                property_term_ref_id="property.style",
                value_kinds=(SketchValueKind.TERM_REF,),
                value_type_term_ref_ids=("value.semantic",),
                required=False,
            ),
        ),
        result_ports=(
            SketchResultPortSignature(
                port_id="endpoint",
                value_type_term_ref_ids=("value.endpoint",),
                required=False,
            ),
        ),
    )
    constraint_horizontal = define_sketch_term(
        term_ref_id="constraint.horizontal",
        namespace="vibecad.sketch.core",
        vocabulary_version="1.0",
        term_id="constraint/horizontal",
        kind=SketchTermKind.CONSTRAINT,
        anchor_slots=(
            SketchAnchorSlotSignature(
                slot_id="subject",
                target_kinds=(
                    SketchAnchorTargetKind.GEOMETRY,
                    SketchAnchorTargetKind.RESULT,
                    SketchAnchorTargetKind.EXTERNAL,
                ),
                role_term_ref_ids=("anchor.whole",),
                result_type_term_ref_ids=("value.endpoint",),
            ),
        ),
    )
    return SketchOntologyCatalog(
        schema_version=SKETCH_ONTOLOGY_SCHEMA_VERSION,
        ontology_id="vibecad.sketch.core.v1",
        terms=(
            constraint_horizontal,
            geometry_line,
            anchor_start,
            property_end,
            anchor_whole,
            unit_mm,
            property_style,
            property_start,
            value_endpoint,
            value_semantic,
            value_vector2,
        ),
    )


def _line_properties(*, end_x: int | float = 20) -> tuple[SketchProperty, ...]:
    return (
        SketchProperty(
            property_term_ref_id="property.end",
            typed_value=SketchTypedValue(
                value_type_term_ref_id="value.vector2",
                value_kind=SketchValueKind.VECTOR,
                value=(end_x, 0.0),
            ),
            unit_term_ref_id="unit.mm",
        ),
        SketchProperty(
            property_term_ref_id="property.start",
            typed_value=SketchTypedValue(
                value_type_term_ref_id="value.vector2",
                value_kind=SketchValueKind.VECTOR,
                value=(0.0, 0.0),
            ),
            unit_term_ref_id="unit.mm",
        ),
    )


def _mixed_graph(ontology: SketchOntologyCatalog) -> SketchIntentGraph:
    unknown_curve = _unknown_ref("vendor.geometry.curve")
    unknown_constraint = _unknown_ref("vendor.constraint.aesthetic")
    return SketchIntentGraph(
        schema_version=SKETCH_INTENT_SCHEMA_VERSION,
        graph_id="graph.demo",
        sketch_id="sketch.front",
        terms=(
            unknown_constraint,
            *tuple(definition.reference for definition in reversed(ontology.terms)),
            unknown_curve,
        ),
        geometries=(
            SketchGeometryNode(
                geometry_id="geometry.vendor",
                geometry_term_ref_id=unknown_curve.term_ref_id,
            ),
            SketchGeometryNode(
                geometry_id="geometry.line.1",
                geometry_term_ref_id="geometry.line",
                properties=_line_properties(),
            ),
        ),
        anchors=(
            SketchAnchor(
                anchor_id="anchor.vendor.whole",
                target_kind=SketchAnchorTargetKind.GEOMETRY,
                target_id="geometry.vendor",
                role_term_ref_id="anchor.whole",
            ),
            SketchAnchor(
                anchor_id="anchor.line.whole",
                target_kind=SketchAnchorTargetKind.GEOMETRY,
                target_id="geometry.line.1",
                role_term_ref_id="anchor.whole",
            ),
            SketchAnchor(
                anchor_id="anchor.external.whole",
                target_kind=SketchAnchorTargetKind.EXTERNAL,
                target_id="external.edge.42",
                role_term_ref_id="anchor.whole",
            ),
        ),
        constraints=(
            SketchConstraintNode(
                constraint_id="constraint.vendor",
                constraint_term_ref_id=unknown_constraint.term_ref_id,
                anchor_ids=("anchor.line.whole",),
            ),
            SketchConstraintNode(
                constraint_id="constraint.known-on-unknown",
                constraint_term_ref_id="constraint.horizontal",
                anchor_ids=("anchor.vendor.whole",),
            ),
            SketchConstraintNode(
                constraint_id="constraint.external",
                constraint_term_ref_id="constraint.horizontal",
                anchor_ids=("anchor.external.whole",),
            ),
            SketchConstraintNode(
                constraint_id="constraint.horizontal.1",
                constraint_term_ref_id="constraint.horizontal",
                anchor_ids=("anchor.line.whole",),
            ),
        ),
    )


def test_graph_is_canonical_order_independent_and_round_trips() -> None:
    ontology = _core_ontology()
    graph = _mixed_graph(ontology)
    reordered = dataclasses.replace(
        graph,
        terms=tuple(reversed(graph.terms)),
        geometries=tuple(reversed(graph.geometries)),
        anchors=tuple(reversed(graph.anchors)),
        constraints=tuple(reversed(graph.constraints)),
    )

    assert reordered == graph
    assert reordered.graph_sha256 == graph.graph_sha256
    assert decode_sketch_intent_graph(encode_sketch_intent_graph(graph)) == graph
    assert encode_sketch_intent_graph(graph).isascii()


def test_unknown_terms_and_unresolved_dependencies_remain_inert() -> None:
    ontology = _core_ontology()
    graph = _mixed_graph(ontology)

    resolution = resolve_sketch_intent(graph, ontology)

    assert resolution.structurally_resolved_geometry_ids == ("geometry.line.1",)
    assert resolution.inert_geometry_ids == ("geometry.vendor",)
    assert resolution.structurally_resolved_constraint_ids == ("constraint.horizontal.1",)
    assert resolution.inert_constraint_ids == (
        "constraint.external",
        "constraint.known-on-unknown",
        "constraint.vendor",
    )
    assert resolution.unresolved_term_ref_ids == (
        "vendor.constraint.aesthetic",
        "vendor.geometry.curve",
    )


def test_unknown_term_valued_property_cannot_promote_a_known_node() -> None:
    ontology = _core_ontology()
    graph = _mixed_graph(ontology)
    unknown_style = _unknown_ref("vendor.style.decorative")
    geometries = tuple(
        dataclasses.replace(
            node,
            properties=(
                *node.properties,
                SketchProperty(
                    property_term_ref_id="property.style",
                    typed_value=SketchTypedValue(
                        value_type_term_ref_id="value.semantic",
                        value_kind=SketchValueKind.TERM_REF,
                        value=unknown_style.term_ref_id,
                    ),
                ),
            ),
        )
        if node.geometry_id == "geometry.line.1"
        else node
        for node in graph.geometries
    )
    styled = dataclasses.replace(
        graph,
        terms=(*graph.terms, unknown_style),
        geometries=geometries,
    )

    resolution = resolve_sketch_intent(styled, ontology)
    assert resolution.structurally_resolved_geometry_ids == ()
    assert "geometry.line.1" in resolution.inert_geometry_ids
    assert resolution.structurally_resolved_constraint_ids == ()
    assert "vendor.style.decorative" in resolution.unresolved_term_ref_ids


def test_unknown_value_type_keeps_known_node_inert_without_binding_it() -> None:
    ontology = _core_ontology()
    graph = _mixed_graph(ontology)
    unknown_type = _unknown_ref("vendor.value.vector2")
    geometries = tuple(
        dataclasses.replace(
            node,
            properties=tuple(
                dataclasses.replace(
                    prop,
                    typed_value=dataclasses.replace(
                        prop.typed_value,
                        value_type_term_ref_id=unknown_type.term_ref_id,
                    ),
                )
                if prop.property_term_ref_id == "property.start"
                else prop
                for prop in node.properties
            ),
        )
        if node.geometry_id == "geometry.line.1"
        else node
        for node in graph.geometries
    )
    extended = dataclasses.replace(
        graph,
        terms=(*graph.terms, unknown_type),
        geometries=geometries,
    )

    resolution = resolve_sketch_intent(extended, ontology)
    assert "geometry.line.1" in resolution.inert_geometry_ids
    assert unknown_type.term_ref_id in resolution.unresolved_term_ref_ids


def test_known_constraint_requires_its_typed_anchor_signature() -> None:
    ontology = _core_ontology()
    graph = _mixed_graph(ontology)
    anchors = tuple(
        dataclasses.replace(anchor, role_term_ref_id="anchor.start")
        if anchor.anchor_id == "anchor.line.whole"
        else anchor
        for anchor in graph.anchors
    )
    malformed = dataclasses.replace(graph, anchors=anchors)

    with pytest.raises(SketchIntentError) as failure:
        resolve_sketch_intent(malformed, ontology)
    assert failure.value.code is SketchIntentErrorCode.BINDING_MISMATCH


def test_term_identity_and_definition_digest_fail_closed_on_tamper() -> None:
    ontology = _core_ontology()
    graph = _mixed_graph(ontology)
    forged_terms = tuple(
        dataclasses.replace(term, term_definition_sha256=_sha("forged"))
        if term.term_ref_id == "geometry.line"
        else term
        for term in graph.terms
    )

    with pytest.raises(SketchIntentError) as graph_failure:
        resolve_sketch_intent(dataclasses.replace(graph, terms=forged_terms), ontology)
    assert graph_failure.value.code is SketchIntentErrorCode.INTEGRITY_FAILURE

    definition = ontology.by_id["geometry.line"]
    with pytest.raises(SketchOntologyError) as definition_failure:
        SketchOntologyTermDefinition(
            reference=dataclasses.replace(
                definition.reference,
                term_definition_sha256=_sha("forged-definition"),
            ),
            kind=definition.kind,
            anchor_slots=definition.anchor_slots,
            properties=definition.properties,
            result_ports=definition.result_ports,
        )
    assert definition_failure.value.code is SketchOntologyErrorCode.INTEGRITY_FAILURE


def test_wire_digest_detects_mutation_and_noncanonical_encodings() -> None:
    raw = encode_sketch_intent_graph(_mixed_graph(_core_ontology()))
    mapping = json.loads(raw)
    mapping["geometries"][0]["construction"] = True
    tampered = json.dumps(
        mapping,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")

    with pytest.raises(SketchIntentError) as mutation:
        decode_sketch_intent_graph(tampered)
    assert mutation.value.code is SketchIntentErrorCode.INTEGRITY_FAILURE

    with pytest.raises(SketchIntentError) as noncanonical:
        decode_sketch_intent_graph(raw + b"\n")
    assert noncanonical.value.code is SketchIntentErrorCode.INVALID_INPUT

    with pytest.raises(SketchIntentError) as oversized:
        decode_sketch_intent_graph(b"x" * (MAX_SKETCH_INTENT_BYTES + 1))
    assert oversized.value.code is SketchIntentErrorCode.BUDGET_EXCEEDED


def test_wire_contract_is_strict_and_does_not_persist_planning_state() -> None:
    graph = _mixed_graph(_core_ontology())
    mapping = graph.to_mapping()
    mapping["hypotheses"] = []

    with pytest.raises(SketchIntentError) as extra_field:
        SketchIntentGraph.from_mapping(mapping)
    assert extra_field.value.code is SketchIntentErrorCode.INVALID_INPUT

    with pytest.raises(SketchIntentError) as next_version:
        SketchIntentGraph.from_mapping(
            {
                **graph.to_mapping(),
                "schema_version": SKETCH_INTENT_SCHEMA_VERSION + 1,
            }
        )
    assert next_version.value.code is SketchIntentErrorCode.UNSUPPORTED_VERSION


def test_graph_term_budget_accepts_n_and_rejects_n_plus_one() -> None:
    terms = tuple(_unknown_ref(f"term.{index}") for index in range(MAX_SKETCH_INTENT_TERMS))
    graph = SketchIntentGraph(
        schema_version=SKETCH_INTENT_SCHEMA_VERSION,
        graph_id="graph.budget",
        sketch_id="sketch.budget",
        terms=terms,
        geometries=(),
        anchors=(),
        constraints=(),
    )
    assert len(graph.terms) == MAX_SKETCH_INTENT_TERMS

    with pytest.raises(SketchIntentError) as overflow:
        dataclasses.replace(
            graph,
            terms=(*terms, _unknown_ref("term.overflow")),
        )
    assert overflow.value.code is SketchIntentErrorCode.BUDGET_EXCEEDED


def test_node_anchor_budget_accepts_n_and_rejects_n_plus_one() -> None:
    anchor_ids = tuple(f"anchor.{index}" for index in range(MAX_SKETCH_ANCHORS_PER_NODE))
    node = SketchGeometryNode(
        geometry_id="geometry.budget",
        geometry_term_ref_id="geometry.open",
        anchor_ids=anchor_ids,
    )
    assert len(node.anchor_ids) == MAX_SKETCH_ANCHORS_PER_NODE

    with pytest.raises(SketchIntentError) as overflow:
        dataclasses.replace(node, anchor_ids=(*anchor_ids, "anchor.overflow"))
    assert overflow.value.code is SketchIntentErrorCode.BUDGET_EXCEEDED


def test_ontology_term_budget_accepts_n_and_rejects_n_plus_one() -> None:
    terms = tuple(
        define_sketch_term(
            term_ref_id=f"property.extension.{index}",
            namespace="example.open-sketch",
            vocabulary_version="1.0",
            term_id=f"property/extension/{index}",
            kind=SketchTermKind.PROPERTY,
        )
        for index in range(MAX_SKETCH_ONTOLOGY_TERMS)
    )
    catalog = SketchOntologyCatalog(
        schema_version=SKETCH_ONTOLOGY_SCHEMA_VERSION,
        ontology_id="example.maximum",
        terms=terms,
    )
    assert len(catalog.terms) == MAX_SKETCH_ONTOLOGY_TERMS

    with pytest.raises(SketchOntologyError) as overflow:
        dataclasses.replace(
            catalog,
            terms=(
                *terms,
                define_sketch_term(
                    term_ref_id="property.extension.overflow",
                    namespace="example.open-sketch",
                    vocabulary_version="1.0",
                    term_id="property/extension/overflow",
                    kind=SketchTermKind.PROPERTY,
                ),
            ),
        )
    assert overflow.value.code is SketchOntologyErrorCode.BUDGET_EXCEEDED


def test_only_final_anchor_slot_may_be_optional_or_repeated() -> None:
    first = SketchAnchorSlotSignature(
        slot_id="optional-first",
        target_kinds=(SketchAnchorTargetKind.GEOMETRY,),
        role_term_ref_ids=("anchor.whole",),
        minimum_occurrences=0,
    )
    second = SketchAnchorSlotSignature(
        slot_id="required-second",
        target_kinds=(SketchAnchorTargetKind.GEOMETRY,),
        role_term_ref_ids=("anchor.whole",),
    )

    with pytest.raises(SketchOntologyError) as ambiguous:
        define_sketch_term(
            term_ref_id="constraint.ambiguous",
            namespace="example.open-sketch",
            vocabulary_version="1.0",
            term_id="constraint/ambiguous",
            kind=SketchTermKind.CONSTRAINT,
            anchor_slots=(first, second),
        )
    assert ambiguous.value.code is SketchOntologyErrorCode.INVALID_INPUT


def test_element_ref_requires_existing_target_and_exact_element_type() -> None:
    ontology = _core_ontology()
    graph = _mixed_graph(ontology)
    source_term = define_sketch_term(
        term_ref_id="property.source",
        namespace="vibecad.sketch.core",
        vocabulary_version="1.0",
        term_id="property/source",
        kind=SketchTermKind.PROPERTY,
    )
    derived = define_sketch_term(
        term_ref_id="geometry.derived",
        namespace="vibecad.sketch.core",
        vocabulary_version="1.0",
        term_id="geometry/derived",
        kind=SketchTermKind.GEOMETRY,
        properties=(
            SketchPropertySignature(
                property_term_ref_id="property.source",
                value_kinds=(SketchValueKind.ELEMENT_REF,),
                value_type_term_ref_ids=("geometry.line",),
                element_kinds=(SketchElementKind.GEOMETRY,),
            ),
        ),
    )
    ontology = dataclasses.replace(
        ontology,
        terms=(*ontology.terms, source_term, derived),
    )
    source = SketchProperty(
        property_term_ref_id="property.source",
        typed_value=SketchTypedValue(
            value_type_term_ref_id="geometry.line",
            value_kind=SketchValueKind.ELEMENT_REF,
            value=SketchElementRef(
                element_id="geometry.line.1",
                element_kind=SketchElementKind.GEOMETRY,
            ),
        ),
    )
    derived_node = SketchGeometryNode(
        geometry_id="geometry.derived.1",
        geometry_term_ref_id="geometry.derived",
        properties=(source,),
    )
    extended = dataclasses.replace(
        graph,
        terms=(*graph.terms, source_term.reference, derived.reference),
        geometries=(*graph.geometries, derived_node),
    )
    resolution = resolve_sketch_intent(extended, ontology)
    assert "geometry.derived.1" in resolution.structurally_resolved_geometry_ids

    unknown_style = _unknown_ref("vendor.style.inert-source")
    dependent_on_inert = dataclasses.replace(
        extended,
        terms=(*extended.terms, unknown_style),
        geometries=tuple(
            dataclasses.replace(
                node,
                properties=(
                    *node.properties,
                    SketchProperty(
                        property_term_ref_id="property.style",
                        typed_value=SketchTypedValue(
                            value_type_term_ref_id="value.semantic",
                            value_kind=SketchValueKind.TERM_REF,
                            value=unknown_style.term_ref_id,
                        ),
                    ),
                ),
            )
            if node.geometry_id == "geometry.line.1"
            else node
            for node in extended.geometries
        ),
    )
    inert_resolution = resolve_sketch_intent(dependent_on_inert, ontology)
    assert "geometry.line.1" in inert_resolution.inert_geometry_ids
    assert "geometry.derived.1" in inert_resolution.inert_geometry_ids

    missing = dataclasses.replace(
        source,
        typed_value=SketchTypedValue(
            value_type_term_ref_id="geometry.line",
            value_kind=SketchValueKind.ELEMENT_REF,
            value={"element_id": "geometry.missing", "element_kind": "geometry"},
        ),
    )
    with pytest.raises(SketchIntentError) as missing_target:
        dataclasses.replace(
            extended,
            geometries=(
                *graph.geometries,
                dataclasses.replace(derived_node, properties=(missing,)),
            ),
        )
    assert missing_target.value.code is SketchIntentErrorCode.UNKNOWN_REFERENCE

    wrong_type = dataclasses.replace(
        source,
        typed_value=SketchTypedValue(
            value_type_term_ref_id="geometry.derived",
            value_kind=SketchValueKind.ELEMENT_REF,
            value={"element_id": "geometry.line.1", "element_kind": "geometry"},
        ),
    )
    with pytest.raises(SketchIntentError) as mismatch:
        dataclasses.replace(
            extended,
            geometries=(
                *graph.geometries,
                dataclasses.replace(derived_node, properties=(wrong_type,)),
            ),
        )
    assert mismatch.value.code is SketchIntentErrorCode.BINDING_MISMATCH


def test_producer_declares_typed_result_before_downstream_anchor_can_use_it() -> None:
    ontology = _core_ontology()
    graph = _mixed_graph(ontology)
    result = SketchResultPort(
        result_id="result.line.endpoint",
        producer_id="geometry.line.1",
        port_id="endpoint",
        value_type_term_ref_id="value.endpoint",
    )
    line_nodes = tuple(
        dataclasses.replace(node, result_ids=(result.result_id,))
        if node.geometry_id == "geometry.line.1"
        else node
        for node in graph.geometries
    )
    result_anchor = SketchAnchor(
        anchor_id="anchor.line.endpoint",
        target_kind=SketchAnchorTargetKind.RESULT,
        target_id=result.result_id,
        role_term_ref_id="anchor.whole",
    )
    constraints = tuple(
        dataclasses.replace(node, anchor_ids=(result_anchor.anchor_id,))
        if node.constraint_id == "constraint.horizontal.1"
        else node
        for node in graph.constraints
    )
    with_result = dataclasses.replace(
        graph,
        geometries=line_nodes,
        anchors=(*graph.anchors, result_anchor),
        constraints=constraints,
        results=(result,),
    )
    resolution = resolve_sketch_intent(with_result, ontology)
    assert resolution.structurally_resolved_constraint_ids == ("constraint.horizontal.1",)

    with pytest.raises(SketchIntentError) as undeclared:
        dataclasses.replace(with_result, results=())
    assert undeclared.value.code is SketchIntentErrorCode.BINDING_MISMATCH

    duplicate_port = dataclasses.replace(result, result_id="result.line.endpoint.duplicate")
    duplicated = dataclasses.replace(
        with_result,
        geometries=tuple(
            dataclasses.replace(
                node,
                result_ids=(result.result_id, duplicate_port.result_id),
            )
            if node.geometry_id == "geometry.line.1"
            else node
            for node in with_result.geometries
        ),
        results=(result, duplicate_port),
    )
    with pytest.raises(SketchIntentError) as duplicate_output_port:
        resolve_sketch_intent(duplicated, ontology)
    assert duplicate_output_port.value.code is SketchIntentErrorCode.BINDING_MISMATCH


@pytest.mark.parametrize(
    ("kind", "value"),
    (
        (SketchValueKind.LIST, [1, "two", {"nested": True}]),
        (SketchValueKind.RECORD, {"items": [1, 2], "name": "record"}),
        (SketchValueKind.MATRIX, [[1, 0], [0, 1]]),
        (
            SketchValueKind.PLACEMENT,
            {"translation": [1, 2, 3], "rotation_quaternion": [0, 0, 0, 1]},
        ),
        (SketchValueKind.EXPRESSION, "length * 2"),
        (
            SketchValueKind.CONTENT_REF,
            {
                "sha256": "a" * 64,
                "size_bytes": 42,
                "media_type": "application/json",
                "schema_term_ref_id": None,
            },
        ),
    ),
)
def test_open_typed_value_envelope_is_bounded_and_canonical(
    kind: SketchValueKind,
    value: object,
) -> None:
    typed = SketchTypedValue(
        value_type_term_ref_id="value.extension",
        value_kind=kind,
        value=value,
    )
    restored = SketchTypedValue.from_mapping(typed.to_mapping())
    assert restored == typed
    assert restored.decoded_value == typed.decoded_value


def test_semantic_identity_duplicates_and_known_wrong_kind_fail_closed() -> None:
    ontology = _core_ontology()
    graph = _mixed_graph(ontology)
    duplicate_identity = dataclasses.replace(
        ontology.terms[0].reference,
        term_ref_id="alias.same.identity",
    )
    with pytest.raises(SketchIntentError) as duplicate:
        dataclasses.replace(graph, terms=(*graph.terms, duplicate_identity))
    assert duplicate.value.code is SketchIntentErrorCode.DUPLICATE_ID

    wrong_kind = dataclasses.replace(
        graph.geometries[0],
        geometry_term_ref_id="property.start",
        properties=(),
    )
    malformed = dataclasses.replace(
        graph,
        geometries=(wrong_kind, *graph.geometries[1:]),
    )
    with pytest.raises(SketchIntentError) as wrong_known_kind:
        resolve_sketch_intent(malformed, ontology)
    assert wrong_known_kind.value.code is SketchIntentErrorCode.BINDING_MISMATCH


def test_visual_ontology_bridge_preserves_vfg_identity_bytes() -> None:
    visual = OntologyTermRef(
        term_ref_id="visual.edge",
        namespace="shared.ontology",
        vocabulary_version="1.0-beta",
        term_id="geometry/edge",
        term_definition_sha256=_sha("visual-edge"),
    )
    mapped = sketch_term_ref_from_visual_mapping(visual.to_mapping())
    assert mapped.to_mapping() == visual.to_mapping()


def test_constructor_wire_budget_includes_digest_envelope_n_and_n_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _mixed_graph(_core_ontology())
    encoded_size = len(encode_sketch_intent_graph(graph))

    monkeypatch.setattr(sketch_contracts, "MAX_SKETCH_INTENT_BYTES", encoded_size)
    exact = dataclasses.replace(graph)
    assert len(encode_sketch_intent_graph(exact)) == encoded_size

    monkeypatch.setattr(sketch_contracts, "MAX_SKETCH_INTENT_BYTES", encoded_size - 1)
    with pytest.raises(SketchIntentError) as overflow:
        dataclasses.replace(graph)
    assert overflow.value.code is SketchIntentErrorCode.BUDGET_EXCEEDED


def test_structural_resolution_does_not_claim_execution_authority() -> None:
    resolution = resolve_sketch_intent(_mixed_graph(_core_ontology()), _core_ontology())
    assert not hasattr(resolution, "executable_geometry_ids")
    assert not hasattr(resolution, "executable_constraint_ids")
