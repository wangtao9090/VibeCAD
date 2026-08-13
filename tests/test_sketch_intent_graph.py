"""Focused tests for the backend-neutral sketch-intent boundary."""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

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
    SketchTermKind,
    SketchValueKind,
    decode_sketch_intent_graph,
    define_sketch_term,
    encode_sketch_intent_graph,
    resolve_sketch_intent,
)


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
                unit_term_ref_ids=("unit.mm",),
            ),
            SketchPropertySignature(
                property_term_ref_id="property.start",
                value_kinds=(SketchValueKind.VECTOR,),
                unit_term_ref_ids=("unit.mm",),
            ),
            SketchPropertySignature(
                property_term_ref_id="property.style",
                value_kinds=(SketchValueKind.TERM_REF,),
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
                    SketchAnchorTargetKind.EXTERNAL,
                ),
                role_term_ref_ids=("anchor.whole",),
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
        ),
    )


def _line_properties(*, end_x: int | float = 20) -> tuple[SketchProperty, ...]:
    return (
        SketchProperty(
            property_term_ref_id="property.end",
            value_kind=SketchValueKind.VECTOR,
            value=(end_x, 0.0),
            unit_term_ref_id="unit.mm",
        ),
        SketchProperty(
            property_term_ref_id="property.start",
            value_kind=SketchValueKind.VECTOR,
            value=(0.0, 0.0),
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

    assert resolution.executable_geometry_ids == ("geometry.line.1",)
    assert resolution.inert_geometry_ids == ("geometry.vendor",)
    assert resolution.executable_constraint_ids == ("constraint.horizontal.1",)
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
                    value_kind=SketchValueKind.TERM_REF,
                    value=unknown_style.term_ref_id,
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
    assert resolution.executable_geometry_ids == ()
    assert "geometry.line.1" in resolution.inert_geometry_ids
    assert resolution.executable_constraint_ids == ()
    assert "vendor.style.decorative" in resolution.unresolved_term_ref_ids


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
