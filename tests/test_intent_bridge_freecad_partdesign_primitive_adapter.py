"""Focused and one real batch gate for the PartDesign primitive adapter."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from vibecad.intent_bridge.contracts import (
    BackendLoweringRequest,
    BridgeBudget,
    BridgeDisposition,
    BridgeTermRef,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    ProducerBinding,
    ProducerDescriptor,
    ProofAssertion,
    ProofBundle,
    ProofEndpoint,
    SubjectRef,
)
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_partdesign_primitive_adapter import (
    FREECAD_PARTDESIGN_PRIMITIVE_ADAPTER_DESCRIPTOR,
    PRIMITIVE_BASE_ROLE_TERM,
    PRIMITIVE_CANONICAL_JSON_TERM,
    PRIMITIVE_INTENT_DOCUMENT_ROLE_TERM,
    PRIMITIVE_OPERATION_TERMS,
    PRIMITIVE_PARAMETERS_ROLE_TERM,
    PRIMITIVE_PARAMETERS_TYPE_TERM,
    PRIMITIVE_PFG_TERMS,
    PRIMITIVE_REQUEST_TERMS,
    PRIMITIVE_SOLID_RESULT_ROLE_TERM,
    PRIMITIVE_SOLID_TYPE_TERM,
    PRIMITIVE_STRUCTURE_TERM,
    FreeCADPartDesignPrimitiveAdapter,
    build_primitive_capability_document,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    ParametricFeatureGraphV2Codec,
)
from vibecad.intent_bridge.ports import IntentBackendAdapter, TrustedCodecRegistry
from vibecad.intent_bridge.trusted_proof_policy import (
    RuleEndpointSignature,
    TrustedRuleEvaluation,
    TrustedRuleEvaluatorDescriptor,
    TrustedRulePolicy,
)
from vibecad.parametric.feature_graph_v2 import (
    DesignParameterV2,
    FeatureBodyV2,
    FeatureDependencyV2,
    FeatureGraphResultV2,
    FeatureInputPortV2,
    FeatureIntentV2,
    FeatureNodeV2,
    FeatureParameterBindingV2,
    FeatureResultV2,
    ParametricFeatureGraphV2,
    SemanticTermRefV2,
    TermTypedValueV2,
)
from vibecad.parametric.freecad_partdesign_primitive_rules import (
    MAX_PARTDESIGN_PRIMITIVE_PLAN_BYTES,
    PartDesignPrimitiveOperation,
    PartDesignPrimitiveRuleError,
    decode_partdesign_primitive_backend_plan,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pfg_term(term_ref_id: str, term_id: str) -> SemanticTermRefV2:
    return SemanticTermRefV2(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.primitive-test-source",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"pfg:{term_id}"),
    )


SOURCE_TERMS = (
    _pfg_term("source_structure", "structure.test-source"),
    _pfg_term("source_family", "family.test-source"),
    _pfg_term("source_operation", "operation.test-source"),
)

_SHAPE_PARAMETERS: dict[str, dict[str, int | float]] = {
    "box": {"size_x_mm": 6.0, "size_y_mm": 7.0, "size_z_mm": 8.0},
    "cylinder": {"radius_mm": 4.0, "height_mm": 8.0, "sweep_degrees": 360.0},
    "sphere": {
        "radius_mm": 5.0,
        "latitude_min_degrees": -90.0,
        "latitude_max_degrees": 90.0,
        "sweep_degrees": 360.0,
    },
    "cone": {
        "base_radius_mm": 4.0,
        "top_radius_mm": 2.0,
        "height_mm": 8.0,
        "sweep_degrees": 360.0,
    },
    "ellipsoid": {
        "radius_x_mm": 5.0,
        "radius_y_mm": 4.0,
        "radius_z_mm": 3.0,
        "latitude_min_degrees": -90.0,
        "latitude_max_degrees": 90.0,
        "sweep_degrees": 360.0,
    },
    "prism": {"side_count": 6, "circumradius_mm": 5.0, "height_mm": 8.0},
    "wedge": {
        "x_min_mm": 0.0,
        "y_min_mm": 0.0,
        "z_min_mm": 0.0,
        "x_inner_min_mm": 2.0,
        "z_inner_min_mm": 2.0,
        "x_max_mm": 10.0,
        "y_max_mm": 10.0,
        "z_max_mm": 10.0,
        "x_inner_max_mm": 8.0,
        "z_inner_max_mm": 8.0,
    },
    "torus": {
        "major_radius_mm": 7.0,
        "minor_radius_mm": 2.0,
        "latitude_min_degrees": -180.0,
        "latitude_max_degrees": 180.0,
        "sweep_degrees": 360.0,
    },
}


def _parameters(family: str, translation: tuple[float, float, float]) -> dict[str, object]:
    return {
        "shape": dict(_SHAPE_PARAMETERS[family]),
        "placement": {
            "translation_mm": list(translation),
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 0.0,
        },
    }


def _source_node() -> FeatureNodeV2:
    return FeatureNodeV2(
        node_id="node_base",
        body_id="body_main",
        name="Authenticated source solid",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=SOURCE_TERMS[0].term_ref_id,
            family_term_ref_id=SOURCE_TERMS[1].term_ref_id,
            operation_term_ref_id=SOURCE_TERMS[2].term_ref_id,
        ),
        results=(
            FeatureResultV2(
                result_id="result_base",
                semantic_role_term_ref_id=PRIMITIVE_SOLID_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=PRIMITIVE_SOLID_TYPE_TERM.term_ref_id,
            ),
        ),
    )


def _graph(
    operation: PartDesignPrimitiveOperation,
    *,
    base_for_additive: bool = False,
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0),
    parameter_value: object | None = None,
    operation_definition: str | None = None,
) -> ParametricFeatureGraphV2:
    operation_terms = next(
        item for item in PRIMITIVE_OPERATION_TERMS if item.operation is operation
    )
    additive = operation.value.startswith("additive_")
    family = operation.value.split("_", 1)[1]
    static_terms = list(PRIMITIVE_PFG_TERMS)
    if operation_definition is not None:
        index = static_terms.index(operation_terms.operation_term)
        static_terms[index] = dataclasses.replace(
            operation_terms.operation_term,
            term_definition_sha256=operation_definition,
        )
    nodes: list[FeatureNodeV2] = []
    dependencies: list[FeatureDependencyV2] = []
    if not additive or base_for_additive:
        base = _source_node()
        nodes.append(base)
        dependencies.append(
            FeatureDependencyV2(
                dependency_id="dependency_base",
                port_id="port_base",
                upstream_node_id=base.node_id,
                upstream_result_id="result_base",
            )
        )
    parameter = DesignParameterV2(
        parameter_id="parameter_primitive",
        name="Untrusted Radius1 TypeId property strings are inert",
        semantic_role_term_ref_id=PRIMITIVE_PARAMETERS_ROLE_TERM.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id="value_primitive",
            value_type_term_ref_id=PRIMITIVE_PARAMETERS_TYPE_TERM.term_ref_id,
            encoding_term_ref_id=PRIMITIVE_CANONICAL_JSON_TERM.term_ref_id,
            value=(
                _parameters(family, translation) if parameter_value is None else parameter_value
            ),
        ),
    )
    target = FeatureNodeV2(
        node_id="node_target",
        body_id="body_main",
        name="PartDesign::SubtractiveTorus Radius1 must never select native code",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=PRIMITIVE_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=operation_terms.family_term.term_ref_id,
            operation_term_ref_id=operation_terms.operation_term.term_ref_id,
            input_ports=(
                FeatureInputPortV2(
                    port_id="port_base",
                    semantic_role_term_ref_id=PRIMITIVE_BASE_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=PRIMITIVE_SOLID_TYPE_TERM.term_ref_id,
                    minimum_cardinality=0 if additive else 1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
                FeatureInputPortV2(
                    port_id="port_parameters",
                    semantic_role_term_ref_id=PRIMITIVE_PARAMETERS_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=PRIMITIVE_PARAMETERS_TYPE_TERM.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
            ),
            dependencies=tuple(dependencies),
            parameter_bindings=(
                FeatureParameterBindingV2(
                    binding_id="binding_parameters",
                    port_id="port_parameters",
                    parameter_id=parameter.parameter_id,
                ),
            ),
        ),
        results=(
            FeatureResultV2(
                result_id="result_target",
                semantic_role_term_ref_id=PRIMITIVE_SOLID_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=PRIMITIVE_SOLID_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    nodes.append(target)
    suffix = "with_base" if base_for_additive else "first_or_subtractive"
    if translation != (0.0, 0.0, 0.0):
        suffix += "_translated"
    return ParametricFeatureGraphV2(
        graph_id=f"graph_{operation.value}_{suffix}",
        name=f"Primitive {operation.value}",
        terms=tuple((*static_terms, *SOURCE_TERMS)),
        bodies=(FeatureBodyV2(body_id="body_main", name="Main"),),
        parameters=(parameter,),
        references=(),
        nodes=tuple(nodes),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection_target",
                node_id=target.node_id,
                result_id="result_target",
            ),
        ),
    )


def _bridge_from_pfg(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


def _proof_term(term_ref_id: str, term_id: str) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.primitive-proof-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"proof:{term_id}"),
    )


RULE = _proof_term("rule_primitive_target", "rule.primitive-target-reviewed")
PREDICATE = _proof_term("predicate_primitive_target", "predicate.primitive-target-reviewed")
ROLE_PREMISE = _proof_term("role_primitive_candidate", "proof-role.primitive-candidate")
ROLE_CONCLUSION = _proof_term("role_primitive_validated", "proof-role.primitive-validated")
PRIMITIVE_STRUCTURE_BRIDGE = _bridge_from_pfg(PRIMITIVE_STRUCTURE_TERM)


class _PrimitiveEvaluator:
    def __init__(self) -> None:
        def signature(role: BridgeTermRef) -> RuleEndpointSignature:
            return RuleEndpointSignature(
                selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
                role_term=role,
                subject_type_term=PRIMITIVE_STRUCTURE_BRIDGE,
            )

        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="partdesign_primitive_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("partdesign-primitive-target-evaluator-v1"),
            rule_term=RULE,
            predicate_term=PREDICATE,
            premises=(signature(ROLE_PREMISE),),
            conclusions=(signature(ROLE_CONCLUSION),),
        )

    @property
    def descriptor(self) -> TrustedRuleEvaluatorDescriptor:
        return self._descriptor

    def validate(self, evaluation: TrustedRuleEvaluation) -> None:
        if (
            len(evaluation.documents) != 1
            or evaluation.premises[0].subject.selector_id != "node_target"
            or evaluation.conclusions[0].subject.selector_id != "node_target"
        ):
            raise IntentBridgeError(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/target")


def _intent_document(graph: ParametricFeatureGraphV2) -> tuple[DocumentRef, bytes]:
    payload = graph.canonical_bytes
    return (
        DocumentRef(
            artifact_id="artifact_pfg",
            role_term_ref_id=PRIMITIVE_INTENT_DOCUMENT_ROLE_TERM.term_ref_id,
            schema_term_ref_id=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM.term_ref_id,
            document_id=graph.graph_id,
            document_digest=graph.graph_sha256,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
        ),
        payload,
    )


def _subject() -> SubjectRef:
    return SubjectRef(
        artifact_id="artifact_pfg",
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id="node_target",
    )


def _proof(policy: TrustedRulePolicy, document: DocumentRef) -> ProofBundle:
    return ProofBundle(
        terms=(
            RULE,
            PREDICATE,
            ROLE_PREMISE,
            ROLE_CONCLUSION,
            PRIMITIVE_STRUCTURE_BRIDGE,
            PRIMITIVE_INTENT_DOCUMENT_ROLE_TERM,
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_primitive_target",
                predicate_term_ref_id=PREDICATE.term_ref_id,
                rule_term_ref_id=RULE.term_ref_id,
                premises=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=ROLE_PREMISE.term_ref_id,
                        subject=_subject(),
                    ),
                ),
                conclusions=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=ROLE_CONCLUSION.term_ref_id,
                        subject=_subject(),
                    ),
                ),
            ),
        ),
        producer=ProducerBinding(
            descriptor=ProducerDescriptor(
                producer_id="primitive_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("primitive-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("upstream-primitive-compile-request"),
        ),
    )


class _Reader:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        payload = self.payloads[document.artifact_id]
        if len(payload) > maximum_bytes:
            raise RuntimeError("over budget")
        return payload


class _MemoryPlanSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.items: dict[str, tuple[DocumentRef, bytes]] = {}

    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        if self.fail:
            raise RuntimeError("untrusted detail")
        existing = self.items.get(document.artifact_id)
        if existing is not None and existing != (document, payload):
            raise RuntimeError("collision")
        staged = dict(self.items)
        staged[document.artifact_id] = (document, payload)
        self.items = staged
        return payload

    def read_exact(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        stored_document, payload = self.items[document.artifact_id]
        if stored_document != document or len(payload) > maximum_bytes:
            raise RuntimeError("bad read")
        return payload


def _request(
    graph: ParametricFeatureGraphV2,
    *,
    max_output_bytes: int = MAX_PARTDESIGN_PRIMITIVE_PLAN_BYTES,
) -> tuple[BackendLoweringRequest, _Reader, TrustedRulePolicy]:
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = build_primitive_capability_document()
    policy = TrustedRulePolicy(evaluators=(_PrimitiveEvaluator(),))
    request = BackendLoweringRequest(
        adapter=FREECAD_PARTDESIGN_PRIMITIVE_ADAPTER_DESCRIPTOR,
        terms=tuple(
            (
                *PRIMITIVE_REQUEST_TERMS,
                RULE,
                PREDICATE,
                ROLE_PREMISE,
                ROLE_CONCLUSION,
                PFG_SELECTOR_FEATURE_NODE,
            )
        ),
        documents=(intent_document, capability_document),
        intent_artifact_ids=(intent_document.artifact_id,),
        capability_artifact_ids=(capability_document.artifact_id,),
        proof_bundle=_proof(policy, intent_document),
        budget=BridgeBudget(
            max_input_bytes=len(intent_payload) + len(capability_payload),
            max_output_bytes=max_output_bytes,
            max_subject_lookups=1,
            max_rule_applications=1,
        ),
    )
    return (
        request,
        _Reader(
            {
                intent_document.artifact_id: intent_payload,
                capability_document.artifact_id: capability_payload,
            }
        ),
        policy,
    )


def _lower(
    adapter: FreeCADPartDesignPrimitiveAdapter,
    request: BackendLoweringRequest,
    reader: _Reader,
    policy: TrustedRulePolicy,
):
    return adapter.lower_with_receipt(
        request,
        artifacts=reader,
        codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
        proof_policy=policy,
    )


@pytest.mark.parametrize("operation", tuple(PartDesignPrimitiveOperation))
def test_shared_adapter_lowers_all_sixteen_without_native_string_authority(
    operation: PartDesignPrimitiveOperation,
) -> None:
    request, reader, policy = _request(_graph(operation))
    sink = _MemoryPlanSink()
    adapter = FreeCADPartDesignPrimitiveAdapter(sink)

    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    repeated, repeated_receipt = _lower(adapter, request, reader, policy)

    assert isinstance(sink, PlanSink)
    assert isinstance(adapter, IntentBackendAdapter)
    assert result.disposition is BridgeDisposition.COMPLETE
    assert result.plan_document == receipt.plan_document
    assert result.supported_subjects == (_subject(),)
    assert plan.operation is operation
    assert (plan.base is None) is operation.value.startswith("additive_")
    assert payload == plan.canonical_bytes
    assert result.plan_document.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert result.plan_document.document_digest == plan.plan_sha256
    assert adapter.executable is False and adapter.grants_execution_authority is False
    assert receipt.executable is False and receipt.grants_execution_authority is False
    assert plan.executable is False and plan.grants_execution_authority is False
    assert repeated == result and repeated_receipt == receipt and len(sink.items) == 1
    text = payload.decode("ascii")
    assert "PartDesign::" not in text
    assert all(name not in text for name in ("Length", "Radius1", "Polygon", "X2max"))


@pytest.mark.parametrize(
    "operation",
    tuple(
        operation
        for operation in PartDesignPrimitiveOperation
        if operation.value.startswith("additive_")
    ),
)
def test_additive_primitives_accept_one_authenticated_optional_base(
    operation: PartDesignPrimitiveOperation,
) -> None:
    request, reader, policy = _request(_graph(operation, base_for_additive=True))
    adapter = FreeCADPartDesignPrimitiveAdapter(_MemoryPlanSink())
    _result, receipt = _lower(adapter, request, reader, policy)
    plan, _payload = adapter.read_plan(receipt)
    assert plan.base is not None and plan.base.node_id == "node_base"


def test_adapter_rejects_term_or_parameter_substitution_and_atomic_sink_failure() -> None:
    graph = _graph(
        PartDesignPrimitiveOperation.ADDITIVE_BOX,
        operation_definition="f" * 64,
    )
    request, reader, policy = _request(graph)
    sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as semantic_error:
        _lower(FreeCADPartDesignPrimitiveAdapter(sink), request, reader, policy)
    assert semantic_error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    injected = _parameters("box", (0.0, 0.0, 0.0))
    injected["shape"]["Radius1"] = 99.0
    request, reader, policy = _request(
        _graph(PartDesignPrimitiveOperation.ADDITIVE_BOX, parameter_value=injected)
    )
    with pytest.raises(IntentBridgeError) as parameter_error:
        _lower(FreeCADPartDesignPrimitiveAdapter(sink), request, reader, policy)
    assert parameter_error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    request, reader, policy = _request(_graph(PartDesignPrimitiveOperation.SUBTRACTIVE_TORUS))
    failed_sink = _MemoryPlanSink(fail=True)
    with pytest.raises(IntentBridgeError) as sink_error:
        _lower(FreeCADPartDesignPrimitiveAdapter(failed_sink), request, reader, policy)
    assert sink_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert failed_sink.items == {}
    assert "untrusted detail" not in str(sink_error.value)


def test_plan_budget_canonical_decoder_and_relational_bounds() -> None:
    graph = _graph(PartDesignPrimitiveOperation.ADDITIVE_WEDGE)
    request, reader, policy = _request(graph)
    adapter = FreeCADPartDesignPrimitiveAdapter(_MemoryPlanSink())
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    assert (
        decode_partdesign_primitive_backend_plan(
            payload,
            expected_content_sha256=result.plan_document.content_sha256,
            expected_plan_sha256=result.plan_document.document_digest,
        )
        == plan
    )
    size = len(payload)
    exact, exact_reader, exact_policy = _request(graph, max_output_bytes=size)
    exact_result, _ = _lower(
        FreeCADPartDesignPrimitiveAdapter(_MemoryPlanSink()),
        exact,
        exact_reader,
        exact_policy,
    )
    assert exact_result.plan_document.size_bytes == size
    small, small_reader, small_policy = _request(graph, max_output_bytes=size - 1)
    small_sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as budget_error:
        _lower(
            FreeCADPartDesignPrimitiveAdapter(small_sink),
            small,
            small_reader,
            small_policy,
        )
    assert budget_error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert small_sink.items == {}
    with pytest.raises(PartDesignPrimitiveRuleError):
        decode_partdesign_primitive_backend_plan(payload + b" ")

    invalid = _parameters("torus", (0.0, 0.0, 0.0))
    invalid["shape"]["major_radius_mm"] = 1.0
    invalid["shape"]["minor_radius_mm"] = 2.0
    request, reader, policy = _request(
        _graph(PartDesignPrimitiveOperation.ADDITIVE_TORUS, parameter_value=invalid)
    )
    with pytest.raises(IntentBridgeError) as relation_error:
        _lower(FreeCADPartDesignPrimitiveAdapter(_MemoryPlanSink()), request, reader, policy)
    assert relation_error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION

    mapping = plan.to_mapping()
    mapping["operation"]["parameters"]["placement"]["rotation_degrees"] = 10**4000
    adversarial = json.dumps(
        mapping,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    with pytest.raises(PartDesignPrimitiveRuleError) as numeric_error:
        decode_partdesign_primitive_backend_plan(adversarial)
    assert len(str(numeric_error.value)) < 160


@pytest.mark.slow
def test_real_freecad_all_primitives_create_edit_save_reopen_and_rollback(
    tmp_path: Path,
) -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the real FreeCAD batch gate")
    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime import status as runtime_status

    runtime_python = runtime_paths.active_runtime_python()
    if not runtime_python.is_file() or not runtime_paths.ready_sentinel().is_file():
        pytest.fail("an existing ready managed FreeCAD runtime is required")
    if not runtime_status.engine_compatible(runtime_python):
        pytest.fail("the existing managed FreeCAD runtime does not match current engine pins")

    cases = []
    rollback_cases = []
    for operation in PartDesignPrimitiveOperation:
        variants = [
            (cases, not operation.value.startswith("additive_"), (0.0, 0.0, 0.0), "success"),
            (rollback_cases, True, (100.0, 100.0, 100.0), "rollback"),
        ]
        if operation.value.startswith("additive_"):
            variants.append((cases, True, (18.0, 0.0, 0.0), "success-with-base"))
        for collection, with_base, translation, label in variants:
            request, reader, policy = _request(
                _graph(
                    operation,
                    base_for_additive=with_base,
                    translation=translation,
                )
            )
            adapter = FreeCADPartDesignPrimitiveAdapter(_MemoryPlanSink())
            result, receipt = _lower(adapter, request, reader, policy)
            plan, payload = adapter.read_plan(receipt)
            plan_path = tmp_path / f"{operation.value}-{label}.json"
            plan_path.write_bytes(payload)
            collection.append(
                {
                    "operation": operation.value,
                    "path": str(plan_path),
                    "content_sha256": result.plan_document.content_sha256,
                    "plan_sha256": result.plan_document.document_digest,
                    "body_id": plan.body_id,
                    "base": (
                        None if plan.base is None else (plan.base.node_id, plan.base.result_id)
                    ),
                }
            )

    model_path = tmp_path / "partdesign-primitives.FCStd"
    source_root = Path(__file__).parents[1] / "src"
    code = f"""
import os, sys
sys.path.insert(0, {str(source_root)!r})
from vibecad.freecad_env import prepare_freecad_import
prepare_freecad_import()
from pathlib import Path
import FreeCAD, Part
from vibecad.parametric.freecad_partdesign_primitive_rules import (
    AuthenticatedPrimitiveObject,
    PartDesignPrimitiveExecutionBindings,
    PartDesignPrimitiveRuleError,
    apply_partdesign_primitive_plan,
)

CASES = {cases!r}
ROLLBACK_CASES = {rollback_cases!r}
EDIT = {{
    'box': ('Length', 7.0),
    'cylinder': ('Radius', 4.5),
    'sphere': ('Radius', 5.5),
    'cone': ('Height', 9.0),
    'ellipsoid': ('Radius1', 5.5),
    'prism': ('Circumradius', 5.5),
    'wedge': ('Xmax', 11.0),
    'torus': ('Radius1', 7.5),
}}

def expected_type(operation):
    mode, family = operation.split('_', 1)
    return f"PartDesign::{{mode.title()}}{{family.title()}}"

def make_bindings(document, entry, index):
    body = document.addObject('PartDesign::Body', f'Body{{index}}')
    base = None
    authenticated = None
    if entry['base'] is not None:
        base = body.newObject('PartDesign::Feature', f'Base{{index}}')
        base.Shape = Part.makeBox(40, 40, 40, FreeCAD.Vector(-20, -20, -20))
        authenticated = AuthenticatedPrimitiveObject(
            object=base,
            node_id=entry['base'][0],
            result_id=entry['base'][1],
        )
    document.recompute()
    return body, base, PartDesignPrimitiveExecutionBindings(
        document=document,
        body=body,
        body_id=entry['body_id'],
        base=authenticated,
    )

document = FreeCAD.newDocument('PartDesignPrimitiveBatch')
document.UndoMode = 1
persisted = []
for index, entry in enumerate(CASES):
    body, base, bindings = make_bindings(document, entry, index)
    payload = Path(entry['path']).read_bytes()
    try:
        receipt = apply_partdesign_primitive_plan(
            payload,
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings,
        )
    except Exception as error:
        raise AssertionError(f"create failed: {{entry['operation']}}") from error
    feature = document.getObject(receipt.object_name)
    assert feature.TypeId == expected_type(entry['operation'])
    assert feature is body.Tip and feature.BaseFeature is base
    assert feature.isValid() and len(feature.Shape.Solids) == 1
    before_edit = float(feature.Shape.Volume)
    family = entry['operation'].split('_', 1)[1]
    property_name, value = EDIT[family]
    setattr(feature, property_name, value)
    document.recompute()
    after_edit = float(feature.Shape.Volume)
    assert feature.isValid() and len(feature.Shape.Solids) == 1
    assert abs(after_edit - before_edit) > 1e-7
    persisted.append((
        feature.Name,
        expected_type(entry['operation']),
        None if base is None else base.Name,
        property_name,
        value,
        after_edit,
    ))

document.saveAs({str(model_path)!r})
FreeCAD.closeDocument(document.Name)
reopened = FreeCAD.openDocument({str(model_path)!r})
reopened.recompute()
for name, type_id, base_name, property_name, value, volume in persisted:
    feature = reopened.getObject(name)
    assert feature.TypeId == type_id and feature.isValid()
    assert len(feature.Shape.Solids) == 1
    assert (None if feature.BaseFeature is None else feature.BaseFeature.Name) == base_name
    assert abs(float(getattr(feature, property_name)) - value) < 1e-9
    assert abs(float(feature.Shape.Volume) - volume) < 1e-6
FreeCAD.closeDocument(reopened.Name)

# Every reviewed TypeId crosses the same failed-transaction gate and restores
# document objects, Body membership/Tip, and source visibility exactly.
rollback_document = FreeCAD.newDocument('PartDesignPrimitiveRollbackBatch')
rollback_document.UndoMode = 1
for index, entry in enumerate(ROLLBACK_CASES):
    body, _base, bindings = make_bindings(rollback_document, entry, index)
    payload = Path(entry['path']).read_bytes()
    before_objects = tuple(rollback_document.Objects)
    before_group = tuple(body.Group)
    before_tip = body.Tip
    before_visibility = tuple(bool(item.Visibility) for item in before_group)
    try:
        apply_partdesign_primitive_plan(
            payload,
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings,
        )
    except PartDesignPrimitiveRuleError:
        pass
    else:
        raise AssertionError(f"{{entry['operation']}} disconnected/no-op must fail")
    assert tuple(rollback_document.Objects) == before_objects
    assert tuple(body.Group) == before_group and body.Tip is before_tip
    assert tuple(bool(item.Visibility) for item in before_group) == before_visibility
FreeCAD.closeDocument(rollback_document.Name)
print('REAL_PARTDESIGN_PRIMITIVE_BATCH_OK')
"""
    completed = subprocess.run(
        [str(runtime_python), "-c", code],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    assert "REAL_PARTDESIGN_PRIMITIVE_BATCH_OK" in completed.stdout
