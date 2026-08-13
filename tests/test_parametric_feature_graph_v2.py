"""Focused tests for the backend-neutral ParametricFeatureGraphV2 contract."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from vibecad.parametric.feature_graph_v2 import (
    MAX_FEATURE_GRAPH_NODES,
    AffineParameterExpressionV2,
    DesignParameterV2,
    FeatureBodyV2,
    FeatureDependencyV2,
    FeatureFamily,
    FeatureIntentV2,
    FeatureNodeV2,
    FeatureParameterBindingV2,
    FeatureReferenceBindingV2,
    InertExtensionV2,
    ParameterExpressionTermV2,
    ParameterValueKind,
    ParametricFeatureGraphError,
    ParametricFeatureGraphErrorCode,
    ParametricFeatureGraphV2,
    SemanticElementKind,
    SemanticReferenceScope,
    SemanticReferenceV2,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
    encode_parametric_feature_graph_v2,
)


def _sha256(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _term(term_ref_id: str) -> SemanticTermRefV2:
    return SemanticTermRefV2(
        term_ref_id=term_ref_id,
        namespace="vibecad.foundation",
        vocabulary_version="1.0.0",
        term_id=f"foundation:{term_ref_id}",
        term_definition_sha256=_sha256(f"term:{term_ref_id}"),
    )


def _dependency(
    node_id: str,
    upstream_node_id: str,
    *,
    ordinal: int = 0,
    role: str = "role.base",
) -> FeatureDependencyV2:
    return FeatureDependencyV2(
        dependency_id=f"dependency.{node_id}.{ordinal}",
        role_term_ref_id=role,
        upstream_node_id=upstream_node_id,
        ordinal=ordinal,
    )


def _reference_binding(
    node_id: str,
    reference_id: str,
    *,
    ordinal: int,
    role: str,
) -> FeatureReferenceBindingV2:
    return FeatureReferenceBindingV2(
        binding_id=f"reference.binding.{node_id}.{role}.{ordinal}",
        role_term_ref_id=role,
        reference_id=reference_id,
        ordinal=ordinal,
    )


def _node(
    family: FeatureFamily,
    *,
    body_id: str = "body.main",
    dependencies: tuple[FeatureDependencyV2, ...] = (),
    references: tuple[FeatureReferenceBindingV2, ...] = (),
    parameter_bindings: tuple[FeatureParameterBindingV2, ...] = (),
) -> FeatureNodeV2:
    node_id = f"node.{family.value}"
    return FeatureNodeV2(
        node_id=node_id,
        body_id=body_id,
        name=family.value.title(),
        intent=FeatureIntentV2(
            family=family,
            operation_term_ref_id=f"op.{family.value}",
            dependencies=dependencies,
            references=references,
            parameter_bindings=parameter_bindings,
        ),
        result_term_ref_ids=(
            "result.reference" if family is FeatureFamily.REFERENCE else "result.solid",
        ),
    )


def _graph() -> ParametricFeatureGraphV2:
    term_ids = {
        "result.reference",
        "result.solid",
        "role.axis",
        "role.base",
        "role.length",
        "role.path",
        "role.profile",
        "role.section",
        "role.target",
        "role.tool",
        "schema.extension",
        "semantic.length",
        "unit.mm",
        *(f"op.{family.value}" for family in FeatureFamily),
    }
    extension = InertExtensionV2(
        extension_id="extension.vendor-note",
        namespace="vendor.example",
        vocabulary_version="2026.1",
        schema_term_ref_id="schema.extension",
        payload_sha256=_sha256("opaque vendor payload"),
        payload_size_bytes=21,
        media_type="application/json",
    )
    parameters = (
        DesignParameterV2(
            parameter_id="parameter.length",
            name="Length",
            semantic_role_term_ref_id="semantic.length",
            value_kind=ParameterValueKind.SCALAR,
            value=10,
            unit_term_ref_id="unit.mm",
            minimum=0,
            maximum=100,
            extension_ids=(extension.extension_id,),
        ),
        DesignParameterV2(
            parameter_id="parameter.double-length",
            name="Double length",
            semantic_role_term_ref_id="semantic.length",
            value_kind=ParameterValueKind.SCALAR,
            value=20,
            unit_term_ref_id="unit.mm",
            expression=AffineParameterExpressionV2(
                terms=(
                    ParameterExpressionTermV2(
                        parameter_id="parameter.length",
                        coefficient=2,
                    ),
                ),
            ),
        ),
    )
    references = (
        SemanticReferenceV2(
            reference_id="reference.profile-a",
            scope=SemanticReferenceScope.FEATURE,
            element_kind=SemanticElementKind.WIRE,
            semantic_role_term_ref_id="role.profile",
            source_node_id="node.reference",
            source_geometry_id="geometry.profile-a",
        ),
        SemanticReferenceV2(
            reference_id="reference.profile-b",
            scope=SemanticReferenceScope.FEATURE,
            element_kind=SemanticElementKind.WIRE,
            semantic_role_term_ref_id="role.section",
            source_node_id="node.reference",
            source_geometry_id="geometry.profile-b",
        ),
        SemanticReferenceV2(
            reference_id="reference.axis",
            scope=SemanticReferenceScope.ORIGIN,
            element_kind=SemanticElementKind.AXIS,
            semantic_role_term_ref_id="role.axis",
        ),
        SemanticReferenceV2(
            reference_id="reference.edge",
            scope=SemanticReferenceScope.FEATURE,
            element_kind=SemanticElementKind.EDGE,
            semantic_role_term_ref_id="role.target",
            source_node_id="node.extrusion",
            source_geometry_id="geometry.edge-a",
        ),
    )
    nodes = (
        _node(FeatureFamily.REFERENCE, body_id="body.reference"),
        _node(FeatureFamily.PRIMITIVE, body_id="body.tool"),
        _node(
            FeatureFamily.EXTRUSION,
            dependencies=(_dependency("node.extrusion", "node.reference"),),
            references=(
                _reference_binding(
                    "node.extrusion",
                    "reference.profile-a",
                    ordinal=0,
                    role="role.profile",
                ),
            ),
            parameter_bindings=(
                FeatureParameterBindingV2(
                    binding_id="parameter.binding.node.extrusion.0",
                    role_term_ref_id="role.length",
                    parameter_id="parameter.length",
                ),
            ),
        ),
        _node(
            FeatureFamily.REVOLUTION,
            dependencies=(_dependency("node.revolution", "node.extrusion"),),
            references=(
                _reference_binding(
                    "node.revolution",
                    "reference.profile-a",
                    ordinal=0,
                    role="role.profile",
                ),
                _reference_binding(
                    "node.revolution", "reference.axis", ordinal=0, role="role.axis"
                ),
            ),
        ),
        _node(
            FeatureFamily.LOFT,
            dependencies=(_dependency("node.loft", "node.revolution"),),
            references=(
                _reference_binding(
                    "node.loft", "reference.profile-a", ordinal=0, role="role.section"
                ),
                _reference_binding(
                    "node.loft", "reference.profile-b", ordinal=1, role="role.section"
                ),
            ),
        ),
        _node(
            FeatureFamily.SWEEP,
            dependencies=(_dependency("node.sweep", "node.loft"),),
            references=(
                _reference_binding(
                    "node.sweep", "reference.profile-a", ordinal=0, role="role.profile"
                ),
                _reference_binding("node.sweep", "reference.axis", ordinal=0, role="role.path"),
            ),
        ),
        _node(
            FeatureFamily.HELIX,
            dependencies=(_dependency("node.helix", "node.sweep"),),
            references=(
                _reference_binding(
                    "node.helix", "reference.profile-a", ordinal=0, role="role.profile"
                ),
                _reference_binding("node.helix", "reference.axis", ordinal=0, role="role.axis"),
            ),
        ),
        _node(
            FeatureFamily.HOLE,
            dependencies=(_dependency("node.hole", "node.helix"),),
            references=(
                _reference_binding("node.hole", "reference.axis", ordinal=0, role="role.axis"),
            ),
        ),
        _node(
            FeatureFamily.TRANSFORM,
            dependencies=(_dependency("node.transform", "node.hole"),),
        ),
        _node(
            FeatureFamily.DRESSUP,
            dependencies=(_dependency("node.dressup", "node.transform"),),
            references=(
                _reference_binding("node.dressup", "reference.edge", ordinal=0, role="role.target"),
            ),
        ),
        _node(
            FeatureFamily.BOOLEAN,
            dependencies=(
                _dependency("node.boolean", "node.dressup", role="role.base"),
                _dependency("node.boolean", "node.primitive", ordinal=1, role="role.tool"),
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id="graph.complete-family-sample",
        name="Complete family sample",
        terms=tuple(_term(term_id) for term_id in reversed(sorted(term_ids))),
        bodies=(
            FeatureBodyV2(body_id="body.tool", name="Boolean tool"),
            FeatureBodyV2(body_id="body.reference", name="Reference geometry"),
            FeatureBodyV2(
                body_id="body.main",
                name="Main result",
                extension_ids=(extension.extension_id,),
            ),
        ),
        parameters=tuple(reversed(parameters)),
        references=tuple(reversed(references)),
        nodes=tuple(reversed(nodes)),
        result_node_ids=("node.boolean",),
        extensions=(extension,),
    )


def _assert_error(code: ParametricFeatureGraphErrorCode, operation) -> None:
    with pytest.raises(ParametricFeatureGraphError) as caught:
        operation()
    assert caught.value.code is code


def _canonical(mapping: dict[str, object]) -> bytes:
    return json.dumps(
        mapping,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def test_round_trip_covers_closed_family_union_and_keeps_extensions_inert() -> None:
    graph = _graph()

    encoded = encode_parametric_feature_graph_v2(graph)
    restored = decode_parametric_feature_graph_v2(encoded, expected_sha256=graph.graph_sha256)

    assert restored == graph
    assert restored.canonical_bytes == encoded
    assert {node.intent.family for node in restored.nodes} == set(FeatureFamily)
    assert restored.executable is False
    assert restored.extensions[0].executable is False
    assert restored.extensions[0].namespace == "vendor.example"


def test_canonical_digest_is_order_independent_and_detects_tampering() -> None:
    graph = _graph()
    reordered = graph.to_mapping()
    for key in ("terms", "bodies", "parameters", "references", "nodes", "extensions"):
        reordered[key] = list(reversed(reordered[key]))

    canonicalized = ParametricFeatureGraphV2.from_mapping(reordered)
    assert canonicalized.canonical_bytes == graph.canonical_bytes
    assert canonicalized.graph_sha256 == graph.graph_sha256

    tampered = graph.to_mapping()
    tampered["name"] = "Tampered graph"
    _assert_error(
        ParametricFeatureGraphErrorCode.INTEGRITY_FAILURE,
        lambda: decode_parametric_feature_graph_v2(
            _canonical(tampered), expected_sha256=graph.graph_sha256
        ),
    )


def test_unknown_family_and_executable_extension_are_rejected() -> None:
    unknown_family = _graph().to_mapping()
    unknown_family["nodes"][0]["intent"]["family"] = "vendor-magic"
    _assert_error(
        ParametricFeatureGraphErrorCode.INVALID_INPUT,
        lambda: ParametricFeatureGraphV2.from_mapping(unknown_family),
    )

    executable_extension = _graph().to_mapping()
    executable_extension["extensions"][0]["disposition"] = "executable"
    _assert_error(
        ParametricFeatureGraphErrorCode.INVALID_INPUT,
        lambda: ParametricFeatureGraphV2.from_mapping(executable_extension),
    )


def test_dangling_dependency_and_graph_cycle_are_rejected() -> None:
    dangling = _graph().to_mapping()
    extrusion = next(node for node in dangling["nodes"] if node["node_id"] == "node.extrusion")
    extrusion["intent"]["dependencies"][0]["upstream_node_id"] = "node.missing"
    _assert_error(
        ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE,
        lambda: ParametricFeatureGraphV2.from_mapping(dangling),
    )

    cyclic = _graph().to_mapping()
    reference = next(node for node in cyclic["nodes"] if node["node_id"] == "node.reference")
    reference["intent"]["dependencies"].append(
        {
            "dependency_id": "dependency.cycle",
            "role_term_ref_id": "role.base",
            "upstream_node_id": "node.boolean",
            "ordinal": 0,
        }
    )
    _assert_error(
        ParametricFeatureGraphErrorCode.CYCLE,
        lambda: ParametricFeatureGraphV2.from_mapping(cyclic),
    )


def test_parameter_dependency_cycle_is_rejected() -> None:
    cyclic = _graph().to_mapping()
    length = next(
        parameter
        for parameter in cyclic["parameters"]
        if parameter["parameter_id"] == "parameter.length"
    )
    length["expression"] = {
        "terms": [{"parameter_id": "parameter.double-length", "coefficient": 0.5}],
        "constant": 0.0,
    }

    _assert_error(
        ParametricFeatureGraphErrorCode.CYCLE,
        lambda: ParametricFeatureGraphV2.from_mapping(cyclic),
    )


def test_node_budget_rejects_n_plus_one_before_graph_construction() -> None:
    oversized = _graph().to_mapping()
    template = next(node for node in oversized["nodes"] if node["node_id"] == "node.reference")
    oversized["bodies"] = [{"body_id": "body.main", "name": "Main", "extension_ids": []}]
    oversized["parameters"] = []
    oversized["references"] = []
    oversized["extensions"] = []
    oversized["nodes"] = []
    for index in range(MAX_FEATURE_GRAPH_NODES + 1):
        item = deepcopy(template)
        item["node_id"] = f"node.n{index:03d}"
        item["body_id"] = "body.main"
        item["name"] = f"Node {index}"
        oversized["nodes"].append(item)
    oversized["result_node_ids"] = ["node.n000"]

    _assert_error(
        ParametricFeatureGraphErrorCode.BUDGET_EXCEEDED,
        lambda: ParametricFeatureGraphV2.from_mapping(oversized),
    )


def test_integer_parameters_can_represent_signed_counts_or_offsets() -> None:
    parameter = DesignParameterV2(
        parameter_id="parameter.offset-index",
        name="Offset index",
        semantic_role_term_ref_id="semantic.length",
        value_kind=ParameterValueKind.INTEGER,
        value=-2,
        minimum=-10,
        maximum=10,
    )

    assert parameter.value == -2
