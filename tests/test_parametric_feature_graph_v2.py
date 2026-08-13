"""Focused tests for the backend-neutral ParametricFeatureGraphV2 contract."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from vibecad.parametric.feature_graph_v2 import (
    MAX_FEATURE_GRAPH_NODES,
    MAX_TYPED_VALUE_BYTES,
    DesignParameterV2,
    ExpressionInputV2,
    ExpressionNodeV2,
    FeatureBodyV2,
    FeatureDependencyV2,
    FeatureGraphResultV2,
    FeatureInputPortV2,
    FeatureIntentV2,
    FeatureNodeKind,
    FeatureNodeV2,
    FeatureParameterBindingV2,
    FeatureReferenceBindingV2,
    FeatureResultV2,
    InertExtensionV2,
    OccurrencePathStepV2,
    ParametricFeatureGraphError,
    ParametricFeatureGraphErrorCode,
    ParametricFeatureGraphV2,
    SemanticReferenceScope,
    SemanticReferenceV2,
    SemanticTermRefV2,
    TermBoundExpressionV2,
    TermTypedValueV2,
    decode_parametric_feature_graph_v2,
    encode_parametric_feature_graph_v2,
)

FAMILIES = (
    "extrusion",
    "revolution",
    "loft",
    "sweep",
    "helix",
    "primitive",
    "hole",
    "transform",
    "dressup",
    "boolean",
    "reference",
)


def _sha256(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _term(term_ref_id: str) -> SemanticTermRefV2:
    return SemanticTermRefV2(
        term_ref_id=term_ref_id,
        namespace="vibecad.foundation",
        vocabulary_version="2.0.0",
        term_id=f"foundation:{term_ref_id}",
        term_definition_sha256=_sha256(f"term:{term_ref_id}"),
    )


def _value(value_id: str, value_type: str, value: object) -> TermTypedValueV2:
    return TermTypedValueV2.from_value(
        value_id=f"value.{value_id}",
        value_type_term_ref_id=f"type.{value_type}",
        encoding_term_ref_id="encoding.canonical-json",
        value=value,
    )


def _parameter(
    parameter_id: str,
    value_type: str,
    value: object,
    *,
    expression: TermBoundExpressionV2 | None = None,
    extension_ids: tuple[str, ...] = (),
) -> DesignParameterV2:
    suffix = parameter_id.removeprefix("parameter.")
    return DesignParameterV2(
        parameter_id=parameter_id,
        name=suffix.replace("-", " ").title(),
        semantic_role_term_ref_id="semantic.parameter",
        value=_value(suffix, value_type, value),
        expression=expression,
        extension_ids=extension_ids,
    )


def _port(
    node: str,
    role: str,
    value_type: str,
    *,
    minimum: int = 1,
    maximum: int = 1,
    ordered: bool = False,
) -> FeatureInputPortV2:
    return FeatureInputPortV2(
        port_id=f"port.{node}.{role}",
        semantic_role_term_ref_id=f"port-role.{role}",
        value_type_term_ref_id=f"type.{value_type}",
        minimum_cardinality=minimum,
        maximum_cardinality=maximum,
        ordered=ordered,
    )


def _result(node: str, suffix: str, value_type: str) -> FeatureResultV2:
    return FeatureResultV2(
        result_id=f"result.{node}.{suffix}",
        semantic_role_term_ref_id=f"result-role.{suffix}",
        value_type_term_ref_id=f"type.{value_type}",
    )


def _dependency(
    node: str,
    port_role: str,
    upstream_node: str,
    upstream_result: str,
    *,
    ordinal: int = 0,
) -> FeatureDependencyV2:
    return FeatureDependencyV2(
        dependency_id=f"dependency.{node}.{port_role}.{ordinal}",
        port_id=f"port.{node}.{port_role}",
        upstream_node_id=f"node.{upstream_node}",
        upstream_result_id=f"result.{upstream_result}",
        ordinal=ordinal,
    )


def _reference_binding(
    node: str,
    port_role: str,
    reference: str,
    *,
    ordinal: int = 0,
) -> FeatureReferenceBindingV2:
    return FeatureReferenceBindingV2(
        binding_id=f"binding.reference.{node}.{port_role}.{ordinal}",
        port_id=f"port.{node}.{port_role}",
        reference_id=f"reference.{reference}",
        ordinal=ordinal,
    )


def _parameter_binding(
    node: str,
    port_role: str,
    parameter: str,
    *,
    ordinal: int = 0,
) -> FeatureParameterBindingV2:
    return FeatureParameterBindingV2(
        binding_id=f"binding.parameter.{node}.{port_role}.{ordinal}",
        port_id=f"port.{node}.{port_role}",
        parameter_id=f"parameter.{parameter}",
        ordinal=ordinal,
    )


def _node(
    name: str,
    family: str,
    *,
    body: str = "main",
    kind: FeatureNodeKind = FeatureNodeKind.FEATURE,
    ports: tuple[FeatureInputPortV2, ...] = (),
    dependencies: tuple[FeatureDependencyV2, ...] = (),
    references: tuple[FeatureReferenceBindingV2, ...] = (),
    parameters: tuple[FeatureParameterBindingV2, ...] = (),
    results: tuple[FeatureResultV2, ...] | None = None,
) -> FeatureNodeV2:
    return FeatureNodeV2(
        node_id=f"node.{name}",
        body_id=f"body.{body}",
        name=name.title(),
        intent=FeatureIntentV2(
            node_kind=kind,
            family_term_ref_id=f"family.{family}",
            operation_term_ref_id=f"operation.{family}",
            input_ports=ports,
            dependencies=dependencies,
            references=references,
            parameter_bindings=parameters,
        ),
        results=results or (_result(name, "solid", "solid"),),
    )


def _graph() -> ParametricFeatureGraphV2:
    extension = InertExtensionV2(
        extension_id="extension.vendor-note",
        namespace="vendor.example",
        vocabulary_version="2026.1",
        schema_term_ref_id="schema.extension",
        payload_sha256=_sha256("opaque vendor payload"),
        payload_size_bytes=21,
        media_type="application/json",
    )
    expression = TermBoundExpressionV2(
        expression_id="expression.double-length",
        nodes=(
            ExpressionNodeV2(
                expression_node_id="expr.sin",
                operator_term_ref_id="operator.sin",
                result_type_term_ref_id="type.scalar",
                inputs=(
                    ExpressionInputV2(
                        input_id="expr-input.sin.length",
                        role_term_ref_id="expression-role.operand",
                        value_type_term_ref_id="type.scalar",
                        source_id="parameter.length",
                    ),
                ),
            ),
            ExpressionNodeV2(
                expression_node_id="expr.scale",
                operator_term_ref_id="operator.scale",
                result_type_term_ref_id="type.scalar",
                inputs=(
                    ExpressionInputV2(
                        input_id="expr-input.scale.sin",
                        role_term_ref_id="expression-role.operand",
                        value_type_term_ref_id="type.scalar",
                        source_id="expr.sin",
                    ),
                ),
            ),
        ),
        result_node_id="expr.scale",
    )
    parameters = (
        _parameter("parameter.length", "scalar", 10, extension_ids=(extension.extension_id,)),
        _parameter("parameter.label", "string", "steel"),
        _parameter("parameter.vector", "vector", [1, 2, 3]),
        _parameter(
            "parameter.placement",
            "placement",
            {"rotation": [0, 0, 0, 1], "translation": [1, 2, 3]},
        ),
        _parameter("parameter.matrix", "matrix", list(range(16))),
        _parameter("parameter.sequence", "sequence", [1, "two", {"three": 3}]),
        _parameter("parameter.record", "record", {"pitch": 2, "turns": 5}),
        _parameter(
            "parameter.content-ref",
            "content-ref",
            {
                "media_type": "image/png",
                "schema_term_ref_id": "schema.blob",
                "sha256": _sha256("image"),
                "size_bytes": 123,
            },
        ),
        _parameter("parameter.double-length", "scalar", 20, expression=expression),
    )
    references = (
        SemanticReferenceV2(
            reference_id="reference.profile-a",
            scope=SemanticReferenceScope.FEATURE,
            semantic_role_term_ref_id="reference-role.profile",
            value_type_term_ref_id="type.wire",
            locator_term_ref_id="locator.declared-result",
            source_node_id="node.reference",
            source_geometry_id="result.reference.profile-a",
        ),
        SemanticReferenceV2(
            reference_id="reference.profile-b",
            scope=SemanticReferenceScope.FEATURE,
            semantic_role_term_ref_id="reference-role.profile",
            value_type_term_ref_id="type.wire",
            locator_term_ref_id="locator.declared-result",
            source_node_id="node.reference",
            source_geometry_id="result.reference.profile-b",
        ),
        SemanticReferenceV2(
            reference_id="reference.axis",
            scope=SemanticReferenceScope.ORIGIN,
            semantic_role_term_ref_id="reference-role.axis",
            value_type_term_ref_id="type.axis",
            locator_term_ref_id="locator.origin-z-axis",
        ),
        SemanticReferenceV2(
            reference_id="reference.edge",
            scope=SemanticReferenceScope.FEATURE,
            semantic_role_term_ref_id="reference-role.edge",
            value_type_term_ref_id="type.edge",
            locator_term_ref_id="locator.declared-result",
            source_node_id="node.extrusion",
            source_geometry_id="result.extrusion.edge",
            occurrence_path=(
                OccurrencePathStepV2(
                    transform_node_id="node.transform",
                    transform_result_id="result.transform.transform",
                    occurrence_index=0,
                ),
            ),
        ),
    )
    nodes = (
        _node(
            "reference",
            "reference",
            body="reference",
            kind=FeatureNodeKind.REFERENCE,
            results=(
                _result("reference", "profile-a", "wire"),
                _result("reference", "profile-b", "wire"),
            ),
        ),
        _node("primitive", "primitive", body="tool"),
        _node(
            "extrusion",
            "extrusion",
            ports=(
                _port("extrusion", "support", "wire"),
                _port("extrusion", "profile", "wire"),
                _port("extrusion", "length", "scalar"),
            ),
            dependencies=(_dependency("extrusion", "support", "reference", "reference.profile-a"),),
            references=(_reference_binding("extrusion", "profile", "profile-a"),),
            parameters=(_parameter_binding("extrusion", "length", "length"),),
            results=(
                _result("extrusion", "solid", "solid"),
                _result("extrusion", "edge", "edge"),
            ),
        ),
        _node(
            "revolution",
            "revolution",
            ports=(
                _port("revolution", "base", "solid"),
                _port("revolution", "profile", "wire"),
                _port("revolution", "axis", "axis"),
            ),
            dependencies=(_dependency("revolution", "base", "extrusion", "extrusion.solid"),),
            references=(
                _reference_binding("revolution", "profile", "profile-a"),
                _reference_binding("revolution", "axis", "axis"),
            ),
        ),
        _node(
            "loft",
            "loft",
            ports=(
                _port("loft", "base", "solid"),
                _port("loft", "sections", "wire", minimum=2, maximum=8, ordered=True),
            ),
            dependencies=(_dependency("loft", "base", "revolution", "revolution.solid"),),
            references=(
                _reference_binding("loft", "sections", "profile-a"),
                _reference_binding("loft", "sections", "profile-b", ordinal=1),
            ),
        ),
        _node(
            "sweep",
            "sweep",
            ports=(
                _port("sweep", "base", "solid"),
                _port("sweep", "profile", "wire"),
                _port("sweep", "path", "axis"),
            ),
            dependencies=(_dependency("sweep", "base", "loft", "loft.solid"),),
            references=(
                _reference_binding("sweep", "profile", "profile-a"),
                _reference_binding("sweep", "path", "axis"),
            ),
        ),
        _node(
            "helix",
            "helix",
            ports=(
                _port("helix", "base", "solid"),
                _port("helix", "profile", "wire"),
                _port("helix", "axis", "axis"),
            ),
            dependencies=(_dependency("helix", "base", "sweep", "sweep.solid"),),
            references=(
                _reference_binding("helix", "profile", "profile-a"),
                _reference_binding("helix", "axis", "axis"),
            ),
        ),
        _node(
            "hole",
            "hole",
            ports=(_port("hole", "base", "solid"), _port("hole", "axis", "axis")),
            dependencies=(_dependency("hole", "base", "helix", "helix.solid"),),
            references=(_reference_binding("hole", "axis", "axis"),),
        ),
        _node(
            "transform",
            "transform",
            ports=(_port("transform", "base", "solid"),),
            dependencies=(_dependency("transform", "base", "hole", "hole.solid"),),
            results=(
                _result("transform", "solid", "solid"),
                _result("transform", "transform", "transform"),
            ),
        ),
        _node(
            "dressup",
            "dressup",
            ports=(
                _port("dressup", "base", "solid"),
                _port("dressup", "target", "edge"),
            ),
            dependencies=(_dependency("dressup", "base", "transform", "transform.solid"),),
            references=(_reference_binding("dressup", "target", "edge"),),
        ),
        _node(
            "boolean",
            "boolean",
            ports=(_port("boolean", "operands", "solid", minimum=2, maximum=32),),
            dependencies=(
                _dependency("boolean", "operands", "dressup", "dressup.solid"),
                _dependency("boolean", "operands", "primitive", "primitive.solid", ordinal=1),
            ),
        ),
    )
    term_ids = {
        "encoding.canonical-json",
        "expression-role.operand",
        "locator.declared-result",
        "locator.origin-z-axis",
        "operator.scale",
        "operator.sin",
        "reference-role.axis",
        "reference-role.edge",
        "reference-role.profile",
        "result-role.edge",
        "result-role.profile-a",
        "result-role.profile-b",
        "result-role.solid",
        "result-role.transform",
        "schema.blob",
        "schema.extension",
        "semantic.parameter",
        *(f"family.{family}" for family in FAMILIES),
        *(f"operation.{family}" for family in FAMILIES),
        *(
            f"port-role.{role}"
            for role in (
                "axis",
                "base",
                "length",
                "operands",
                "path",
                "profile",
                "sections",
                "support",
                "target",
            )
        ),
        *(
            f"type.{value_type}"
            for value_type in (
                "axis",
                "content-ref",
                "edge",
                "matrix",
                "placement",
                "record",
                "scalar",
                "sequence",
                "solid",
                "string",
                "transform",
                "vector",
                "wire",
            )
        ),
    }
    return ParametricFeatureGraphV2(
        graph_id="graph.complete-ontology-sample",
        name="Complete ontology sample",
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
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection.primary",
                node_id="node.boolean",
                result_id="result.boolean.solid",
            ),
        ),
        extensions=(extension,),
    )


def _assert_error(code: ParametricFeatureGraphErrorCode, operation) -> ParametricFeatureGraphError:
    with pytest.raises(ParametricFeatureGraphError) as caught:
        operation()
    assert caught.value.code is code
    return caught.value


def _canonical(mapping: dict[str, object]) -> bytes:
    return json.dumps(
        mapping,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _find(items: list[dict[str, object]], key: str, value: str) -> dict[str, object]:
    return next(item for item in items if item[key] == value)


def test_round_trip_covers_ontology_families_ports_results_and_value_envelopes() -> None:
    graph = _graph()

    encoded = encode_parametric_feature_graph_v2(graph)
    restored = decode_parametric_feature_graph_v2(encoded, expected_sha256=graph.graph_sha256)

    assert restored == graph
    assert restored.canonical_bytes == encoded
    assert {node.intent.family_term_ref_id for node in restored.nodes} == {
        f"family.{family}" for family in FAMILIES
    }
    assert {parameter.value.value_type_term_ref_id for parameter in restored.parameters} >= {
        "type.scalar",
        "type.string",
        "type.vector",
        "type.placement",
        "type.matrix",
        "type.sequence",
        "type.record",
        "type.content-ref",
    }
    assert restored.executable is False
    assert restored.adapter_binding_required is True
    assert restored.extensions[0].executable is False


def test_review_repro_source_geometry_must_name_declared_typed_result() -> None:
    dangling = _graph().to_mapping()
    reference = _find(dangling["references"], "reference_id", "reference.profile-a")
    reference["source_geometry_id"] = "result.reference.undeclared"

    error = _assert_error(
        ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE,
        lambda: ParametricFeatureGraphV2.from_mapping(dangling),
    )
    assert error.path.endswith("/source_geometry_id")

    wrong_type = _graph().to_mapping()
    reference = _find(wrong_type["references"], "reference_id", "reference.edge")
    reference["source_node_id"] = "node.extrusion"
    reference["source_geometry_id"] = "result.extrusion.solid"
    _assert_error(
        ParametricFeatureGraphErrorCode.INVALID_INPUT,
        lambda: ParametricFeatureGraphV2.from_mapping(wrong_type),
    )


def test_dependency_closes_to_exact_upstream_result_and_typed_port() -> None:
    dangling = _graph().to_mapping()
    boolean = _find(dangling["nodes"], "node_id", "node.boolean")
    boolean["intent"]["dependencies"][0]["upstream_result_id"] = "result.dressup.missing"
    _assert_error(
        ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE,
        lambda: ParametricFeatureGraphV2.from_mapping(dangling),
    )

    type_mismatch = _graph().to_mapping()
    boolean = _find(type_mismatch["nodes"], "node_id", "node.boolean")
    boolean["intent"]["dependencies"][0]["upstream_node_id"] = "node.extrusion"
    boolean["intent"]["dependencies"][0]["upstream_result_id"] = "result.extrusion.edge"
    _assert_error(
        ParametricFeatureGraphErrorCode.INVALID_INPUT,
        lambda: ParametricFeatureGraphV2.from_mapping(type_mismatch),
    )


def test_new_family_type_and_operator_terms_round_trip_inert_without_wire_change() -> None:
    mapping = _graph().to_mapping()
    for term_id in (
        "family.vendor-flow",
        "operation.vendor-flow",
        "type.vendor-law",
        "operator.vendor-law",
    ):
        mapping["terms"].append(_term(term_id).to_mapping())
    boolean = _find(mapping["nodes"], "node_id", "node.boolean")
    boolean["intent"]["family_term_ref_id"] = "family.vendor-flow"
    boolean["intent"]["operation_term_ref_id"] = "operation.vendor-flow"
    record = _find(mapping["parameters"], "parameter_id", "parameter.record")
    record["value"]["value_type_term_ref_id"] = "type.vendor-law"
    double_length = _find(mapping["parameters"], "parameter_id", "parameter.double-length")
    expression_node = _find(
        double_length["expression"]["nodes"], "expression_node_id", "expr.scale"
    )
    expression_node["operator_term_ref_id"] = "operator.vendor-law"

    restored = ParametricFeatureGraphV2.from_mapping(mapping)

    assert (
        _find(restored.to_mapping()["nodes"], "node_id", "node.boolean")["intent"][
            "family_term_ref_id"
        ]
        == "family.vendor-flow"
    )
    assert restored.executable is False
    assert restored.adapter_binding_required is True

    undeclared = restored.to_mapping()
    undeclared["terms"] = [
        term for term in undeclared["terms"] if term["term_ref_id"] != "operator.vendor-law"
    ]
    _assert_error(
        ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE,
        lambda: ParametricFeatureGraphV2.from_mapping(undeclared),
    )


def test_expression_dangling_source_and_local_cycle_fail_closed() -> None:
    dangling = _graph().to_mapping()
    parameter = _find(dangling["parameters"], "parameter_id", "parameter.double-length")
    sin_node = _find(parameter["expression"]["nodes"], "expression_node_id", "expr.sin")
    sin_node["inputs"][0]["source_id"] = "parameter.missing"
    _assert_error(
        ParametricFeatureGraphErrorCode.UNKNOWN_REFERENCE,
        lambda: ParametricFeatureGraphV2.from_mapping(dangling),
    )

    cyclic = _graph().to_mapping()
    parameter = _find(cyclic["parameters"], "parameter_id", "parameter.double-length")
    sin_node = _find(parameter["expression"]["nodes"], "expression_node_id", "expr.sin")
    sin_node["inputs"][0]["source_id"] = "expr.scale"
    _assert_error(
        ParametricFeatureGraphErrorCode.CYCLE,
        lambda: ParametricFeatureGraphV2.from_mapping(cyclic),
    )


def test_node_dependency_cycle_is_rejected() -> None:
    cyclic = _graph().to_mapping()
    reference = _find(cyclic["nodes"], "node_id", "node.reference")
    reference["intent"]["input_ports"].append(_port("reference", "base", "solid").to_mapping())
    reference["intent"]["dependencies"].append(
        _dependency("reference", "base", "boolean", "boolean.solid").to_mapping()
    )

    _assert_error(
        ParametricFeatureGraphErrorCode.CYCLE,
        lambda: ParametricFeatureGraphV2.from_mapping(cyclic),
    )


def _budget_graph() -> ParametricFeatureGraphV2:
    nodes = tuple(
        _node(
            f"n{index:03d}",
            "reference",
            kind=FeatureNodeKind.REFERENCE,
        )
        for index in range(MAX_FEATURE_GRAPH_NODES)
    )
    return ParametricFeatureGraphV2(
        graph_id="graph.node-budget",
        name="Node budget",
        terms=tuple(
            _term(term_id)
            for term_id in (
                "family.reference",
                "operation.reference",
                "result-role.solid",
                "type.solid",
            )
        ),
        bodies=(FeatureBodyV2(body_id="body.main", name="Main"),),
        parameters=(),
        references=(),
        nodes=nodes,
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection.primary",
                node_id="node.n000",
                result_id="result.n000.solid",
            ),
        ),
    )


def test_node_budget_accepts_n_and_rejects_n_plus_one() -> None:
    exact = _budget_graph()
    assert len(exact.nodes) == MAX_FEATURE_GRAPH_NODES

    oversized = exact.to_mapping()
    extra = deepcopy(oversized["nodes"][-1])
    extra["node_id"] = "node.overflow"
    extra["name"] = "Overflow"
    extra["results"][0]["result_id"] = "result.overflow.solid"
    oversized["nodes"].append(extra)
    _assert_error(
        ParametricFeatureGraphErrorCode.BUDGET_EXCEEDED,
        lambda: ParametricFeatureGraphV2.from_mapping(oversized),
    )


def test_typed_value_byte_budget_accepts_n_and_rejects_n_plus_one() -> None:
    exact = TermTypedValueV2.from_value(
        value_id="value.maximum",
        value_type_term_ref_id="type.string",
        encoding_term_ref_id="encoding.canonical-json",
        value="x" * (MAX_TYPED_VALUE_BYTES - 2),
    )
    assert len(exact.canonical_value) == MAX_TYPED_VALUE_BYTES

    _assert_error(
        ParametricFeatureGraphErrorCode.BUDGET_EXCEEDED,
        lambda: TermTypedValueV2.from_value(
            value_id="value.overflow",
            value_type_term_ref_id="type.string",
            encoding_term_ref_id="encoding.canonical-json",
            value="x" * (MAX_TYPED_VALUE_BYTES - 1),
        ),
    )


def test_canonical_digest_is_order_independent_content_bound_and_tamper_evident() -> None:
    graph = _graph()
    reordered = graph.to_mapping()
    for key in (
        "terms",
        "bodies",
        "parameters",
        "references",
        "nodes",
        "graph_results",
        "extensions",
    ):
        reordered[key] = list(reversed(reordered[key]))
    for node in reordered["nodes"]:
        for key in ("input_ports", "dependencies", "references", "parameter_bindings"):
            node["intent"][key] = list(reversed(node["intent"][key]))
        node["results"] = list(reversed(node["results"]))

    canonicalized = ParametricFeatureGraphV2.from_mapping(reordered)
    assert canonicalized.canonical_bytes == graph.canonical_bytes
    assert canonicalized.graph_sha256 == graph.graph_sha256

    rebound_term = graph.to_mapping()
    rebound_term["terms"][0]["term_definition_sha256"] = _sha256("different definition")
    assert ParametricFeatureGraphV2.from_mapping(rebound_term).graph_sha256 != graph.graph_sha256

    tampered = graph.to_mapping()
    tampered["name"] = "Tampered graph"
    _assert_error(
        ParametricFeatureGraphErrorCode.INTEGRITY_FAILURE,
        lambda: decode_parametric_feature_graph_v2(
            _canonical(tampered), expected_sha256=graph.graph_sha256
        ),
    )


def test_extension_cannot_be_promoted_to_executable() -> None:
    mapping = _graph().to_mapping()
    mapping["extensions"][0]["disposition"] = "executable"
    _assert_error(
        ParametricFeatureGraphErrorCode.INVALID_INPUT,
        lambda: ParametricFeatureGraphV2.from_mapping(mapping),
    )


def test_contract_source_contains_no_backend_property_or_type_identifiers() -> None:
    source = (
        Path(__file__)
        .parents[1]
        .joinpath("src/vibecad/parametric/feature_graph_v2.py")
        .read_text(encoding="utf-8")
    )

    for backend_token in ("PartDesign::", "Part::", "App::Property", "FreeCAD"):
        assert backend_token not in source
