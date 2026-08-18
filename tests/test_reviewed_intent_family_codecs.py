"""Focused gates for family-owned Reviewed intent document bindings."""

from __future__ import annotations

import dataclasses
import hashlib
from types import MappingProxyType

import pytest

import vibecad.execution.freecad_reviewed_intent_execution as reviewed_execution
import vibecad.intent_bridge.freecad_sketch_intent_adapter as sketch_adapter
from tests.test_intent_bridge_freecad_sketch_intent_adapter import (
    _constraint_graph,
    _geometry_graph,
)
from tests.test_reviewed_intent_program import reviewed_box_program
from vibecad.execution.capabilities import (
    CapabilityExecutionProfile,
    CapabilityLifecycleStage,
    CapabilityRiskClass,
)
from vibecad.execution.freecad_intent_capabilities import FreeCadIntentCapabilitySpec
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    LoweredReviewedIntent,
    ReviewedIntentExecutionError,
    ReviewedIntentRoute,
    _ReviewedFormalSemanticBinding,
    _ReviewedIntentFamilyDescriptor,
    _ReviewedProductResultContract,
    _ReviewedProductResultKind,
    _sketch_intent_binding,
    lower_reviewed_intent,
    route_reviewed_intent,
)
from vibecad.execution.selectors import SemanticRole
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    ParametricFeatureGraphV2Codec,
)
from vibecad.intent_bridge.reviewed_family_engine import ExactReviewedFamilyAdapter
from vibecad.intent_bridge.sketch_intent_graph_codec import (
    SKETCH_CONSTRAINT_SELECTOR_TERM,
    SKETCH_GEOMETRY_SELECTOR_TERM,
    SKETCH_ROOT_SELECTOR_TERM,
    SKETCH_ROOT_SEMANTIC_TYPE_TERM,
)
from vibecad.parametric.freecad_sketch_intent_rules import (
    ReviewedSketchBackendPlan,
    ReviewedSketchOperation,
    decode_reviewed_sketch_backend_plan,
)
from vibecad.sketch.contracts import SketchIntentGraph, encode_sketch_intent_graph
from vibecad.workflow.reviewed_intent import (
    ReviewedIntentProgramError,
    ReviewedIntentProgramErrorCode,
    ReviewedIntentProgramV1,
)


def _sketch_operation(operation: ReviewedSketchOperation):
    return next(
        item
        for item in sketch_adapter.REVIEWED_SKETCH_OPERATION_SPECS
        if item.operation_id == operation.value
    )


def _sketch_adapter_factory(sink: PlanSink) -> ExactReviewedFamilyAdapter:
    return ExactReviewedFamilyAdapter(
        sketch_adapter.REVIEWED_SKETCH_FAMILY_MANIFEST,
        sink,
        build_plan=sketch_adapter._build_plan,  # noqa: SLF001 - exact reviewed callback
        decode_plan=decode_reviewed_sketch_backend_plan,
        validate_binding=sketch_adapter._validate_binding,  # noqa: SLF001
    )


def _never_execute(*_args: object) -> object:
    raise AssertionError("the synthetic family is lowering-only")


def _synthetic_route(
    monkeypatch: pytest.MonkeyPatch,
    operation: ReviewedSketchOperation,
    *,
    binding=None,
) -> ReviewedIntentRoute:
    operation_spec = _sketch_operation(operation)
    selected_binding = _sketch_intent_binding() if binding is None else binding
    family = _ReviewedIntentFamilyDescriptor(
        manifest=sketch_adapter.REVIEWED_SKETCH_FAMILY_MANIFEST,
        subject_type_term=SKETCH_ROOT_SEMANTIC_TYPE_TERM,
        intent_binding=selected_binding,
        adapter_factory=_sketch_adapter_factory,
        validate_plan=sketch_adapter._validate_binding,  # noqa: SLF001
        execute_plan=_never_execute,
        product_results=(
            _ReviewedProductResultContract(
                operation_id=operation.value,
                result_kind=_ReviewedProductResultKind.REFERENCE,
                owned_type_ids=(operation_spec.native_type_id,),
                semantic_roles=(SemanticRole.FEATURE,),
            ),
        ),
        formal_semantic_binding=_ReviewedFormalSemanticBinding.FULL_IDENTITY,
    )
    semantic_operation = reviewed_execution._semantic_operation(operation_spec)  # noqa: SLF001
    operation_id = f"{sketch_adapter.REVIEWED_SKETCH_FAMILY_MANIFEST.family_id}.{operation.value}"
    formal = FreeCadIntentCapabilitySpec(
        operation_id=operation_id,
        semantic_operation=semantic_operation,
        native_type_id=operation_spec.native_type_id,
        adapter_id=family.manifest.adapter.adapter_id,
        adapter_version=family.manifest.adapter.adapter_version,
        adapter_contract_sha256=family.manifest.adapter.adapter_contract_sha256,
        rule_id=family.manifest.rule_id,
        rule_contract_sha256=family.manifest.rule_contract_sha256,
        risk_class=CapabilityRiskClass.MUTATING,
        execution_profiles=(CapabilityExecutionProfile.HEADLESS,),
        lifecycle_stages=(CapabilityLifecycleStage.EXECUTE,),
    )
    monkeypatch.setattr(
        reviewed_execution,
        "current_freecad_intent_capability_specs",
        lambda: (formal,),
    )
    route = ReviewedIntentRoute(
        operation_id=operation_id,
        semantic_operation=semantic_operation,
        family=family,
        manifest=family.manifest,
        operation=operation_spec,
        subject_type_term=selected_binding.subject_type_for(operation_spec),
    )
    monkeypatch.setattr(
        reviewed_execution,
        "_ROUTES_BY_IDENTITY",
        MappingProxyType({(route.operation_id, route.semantic_operation): route}),
    )
    return route


def _program(route: ReviewedIntentRoute, graph: SketchIntentGraph) -> ReviewedIntentProgramV1:
    payload = encode_sketch_intent_graph(graph)
    return ReviewedIntentProgramV1(
        operation_id=route.operation_id,
        semantic_operation=route.semantic_operation,
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(payload).hexdigest(),
        intent_graph=graph,
    )


@pytest.mark.parametrize(
    ("operation", "graph_factory", "selector_term", "node_prefix"),
    (
        (
            ReviewedSketchOperation.LINE,
            _geometry_graph,
            SKETCH_GEOMETRY_SELECTOR_TERM,
            "geometry_",
        ),
        (
            ReviewedSketchOperation.HORIZONTAL,
            _constraint_graph,
            SKETCH_CONSTRAINT_SELECTOR_TERM,
            "constraint_",
        ),
    ),
)
def test_synthetic_sketch_binding_routes_and_lowers_operation_selected_subject(
    monkeypatch: pytest.MonkeyPatch,
    operation: ReviewedSketchOperation,
    graph_factory,
    selector_term,
    node_prefix: str,
) -> None:
    route = _synthetic_route(monkeypatch, operation)
    graph = graph_factory(operation)
    program = _program(route, graph)

    decoded = ReviewedIntentProgramV1.from_mapping(program.to_mapping())
    lowered = lower_reviewed_intent(decoded)

    assert route_reviewed_intent(decoded) is route
    assert type(decoded.intent_graph) is SketchIntentGraph
    assert type(lowered) is LoweredReviewedIntent
    assert type(lowered.plan) is ReviewedSketchBackendPlan
    assert lowered.route is route
    assert lowered.plan.operation is operation
    assert lowered.plan.node_id.startswith(node_prefix)
    assert lowered.receipt.source_document.document_id == graph.graph_id
    assert lowered.receipt.source_document.document_digest == graph.graph_sha256
    assert lowered.receipt.source_document.media_type == route.family.intent_binding.media_type
    assert route.subject_type_term == route.operation.semantic_term
    assert selector_term in route.family.intent_binding.terms_for(
        route.operation,
        selector_term,
    )
    assert SKETCH_ROOT_SEMANTIC_TYPE_TERM in route.family.intent_binding.terms_for(
        route.operation,
        selector_term,
    )
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 126
    assert route not in CURRENT_REVIEWED_INTENT_ROUTES


def test_reviewed_wire_union_preserves_pfg_v2_and_rejects_unknown_discriminator() -> None:
    pfg = reviewed_box_program()
    mapping = pfg.to_mapping()

    assert mapping["schema_version"] == 1
    assert mapping["intent_graph"]["schema_version"] == 2
    assert ReviewedIntentProgramV1.from_mapping(mapping).canonical_bytes == pfg.canonical_bytes

    unknown = {
        **mapping,
        "intent_graph": {**mapping["intent_graph"], "schema_version": 3},
    }
    with pytest.raises(ReviewedIntentProgramError) as captured:
        ReviewedIntentProgramV1.from_mapping(unknown)
    assert captured.value.code is ReviewedIntentProgramErrorCode.UNSUPPORTED_VERSION
    assert captured.value.path == "/intent_graph/schema_version"


def test_current_routes_bind_exact_pfg_document_contract() -> None:
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 126
    assert {route.family.intent_binding.binding_id for route in CURRENT_REVIEWED_INTENT_ROUTES} == {
        "reviewed_pfg_v2_feature_node",
        "reviewed_pm1_pfg_v2_whole_transaction",
        "reviewed_sketch_v1_operation_node",
    }
    assert {
        route.family.intent_binding.binding_version for route in CURRENT_REVIEWED_INTENT_ROUTES
    } == {"1.0.0"}
    legacy_route_catalog_sha256 = hashlib.sha256(
        "\n".join(
            f"{route.operation_id}:{route.route_contract_sha256}"
            for route in CURRENT_REVIEWED_INTENT_ROUTES[:78]
        ).encode("ascii")
    ).hexdigest()
    assert legacy_route_catalog_sha256 == (
        "65cfa7240d5e233af9bd4340a283c944f13f9cdca98be5d61b6ac7bbce715d30"
    )
    import81_route_catalog_sha256 = hashlib.sha256(
        "\n".join(
            f"{route.operation_id}:{route.route_contract_sha256}"
            for route in CURRENT_REVIEWED_INTENT_ROUTES[:81]
        ).encode("ascii")
    ).hexdigest()
    assert import81_route_catalog_sha256 == (
        "40c6ed706d59b612015d752c1d2cfb43c5910b9858b2ef991d633d50f345306b"
    )
    current_route_catalog_sha256 = hashlib.sha256(
        "\n".join(
            f"{route.operation_id}:{route.route_contract_sha256}"
            for route in CURRENT_REVIEWED_INTENT_ROUTES
        ).encode("ascii")
    ).hexdigest()
    assert current_route_catalog_sha256 == (
        "db733cab94f9e72f6ce380c3c2509791ecdd94b650249c28d719d1a190c351d3"
    )


def test_sketch_binding_rejects_wrong_payload_selector_schema_codec_and_tamper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = ReviewedSketchOperation.LINE
    binding = _sketch_intent_binding()
    route = _synthetic_route(monkeypatch, operation, binding=binding)
    graph = _geometry_graph(operation)
    program = _program(route, graph)

    pfg = reviewed_box_program()
    wrong_payload = ReviewedIntentProgramV1(
        operation_id=route.operation_id,
        semantic_operation=route.semantic_operation,
        intent_graph_sha256=pfg.intent_graph_sha256,
        intent_content_sha256=pfg.intent_content_sha256,
        intent_graph=pfg.intent_graph,
    )
    with pytest.raises(ReviewedIntentExecutionError):
        lower_reviewed_intent(wrong_payload)

    wrong_subject_graph = _geometry_graph(ReviewedSketchOperation.CIRCLE)
    with pytest.raises(ReviewedIntentExecutionError):
        lower_reviewed_intent(_program(route, wrong_subject_graph))

    def wrong_selector(value, operation_spec):
        selected = binding.select_document(value, operation_spec)
        return dataclasses.replace(
            selected,
            selector_kind_term=SKETCH_ROOT_SELECTOR_TERM,
        )

    wrong_selector_binding = dataclasses.replace(
        binding,
        select_document=wrong_selector,
    )
    wrong_selector_route = _synthetic_route(
        monkeypatch,
        operation,
        binding=wrong_selector_binding,
    )
    with pytest.raises(ReviewedIntentExecutionError):
        lower_reviewed_intent(_program(wrong_selector_route, graph))

    with pytest.raises(ReviewedIntentExecutionError):
        dataclasses.replace(
            binding,
            schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
        )
    wrong_media_binding = dataclasses.replace(
        binding,
        media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    )
    with pytest.raises(ReviewedIntentExecutionError):
        _synthetic_route(monkeypatch, operation, binding=wrong_media_binding)
    with pytest.raises(ReviewedIntentExecutionError):
        dataclasses.replace(
            binding,
            codec_factory=ParametricFeatureGraphV2Codec,
        )

    tampered = program.to_mapping()
    tampered_graph = dict(tampered["intent_graph"])
    tampered_graph["sketch_id"] = "sketch_tampered"
    with pytest.raises(ReviewedIntentProgramError) as captured:
        ReviewedIntentProgramV1.from_mapping({**tampered, "intent_graph": tampered_graph})
    assert captured.value.code is ReviewedIntentProgramErrorCode.INTEGRITY_FAILURE


def test_route_contract_binds_family_intent_binding_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = ReviewedSketchOperation.LINE
    binding = _sketch_intent_binding()
    route = _synthetic_route(monkeypatch, operation, binding=binding)
    rebound_binding = dataclasses.replace(
        binding,
        binding_contract_sha256="f" * 64,
    )
    rebound = _synthetic_route(monkeypatch, operation, binding=rebound_binding)

    assert route.route_contract_sha256 != rebound.route_contract_sha256
    assert route.family.intent_binding.codec_descriptor == binding.codec_descriptor
    assert route.family.intent_binding.binding_id == "reviewed_sketch_v1_operation_node"
