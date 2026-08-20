"""Focused and one real batch gate for dress-up/transform promotion."""

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
from vibecad.intent_bridge.freecad_partdesign_dressup_transform_adapter import (
    DRESSUP_TRANSFORM_BASE_ROLE_TERM,
    DRESSUP_TRANSFORM_CANONICAL_JSON_TERM,
    DRESSUP_TRANSFORM_INTENT_DOCUMENT_ROLE_TERM,
    DRESSUP_TRANSFORM_OPERATION_TERMS,
    DRESSUP_TRANSFORM_PARAMETERS_ROLE_TERM,
    DRESSUP_TRANSFORM_PARAMETERS_TYPE_TERM,
    DRESSUP_TRANSFORM_PFG_TERMS,
    DRESSUP_TRANSFORM_REQUEST_TERMS,
    DRESSUP_TRANSFORM_SOLID_RESULT_ROLE_TERM,
    DRESSUP_TRANSFORM_SOLID_TYPE_TERM,
    DRESSUP_TRANSFORM_STRUCTURE_TERM,
    FREECAD_PARTDESIGN_DRESSUP_TRANSFORM_ADAPTER_DESCRIPTOR,
    FreeCADPartDesignDressupTransformAdapter,
    build_dressup_transform_capability_document,
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
from vibecad.parametric.freecad_partdesign_dressup_transform_rules import (
    MAX_PARTDESIGN_DRESSUP_TRANSFORM_PLAN_BYTES,
    MultiTransformParameters,
    PartDesignDressupTransformOperation,
    PartDesignDressupTransformRuleError,
    decode_partdesign_dressup_transform_backend_plan,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pfg_term(term_ref_id: str, term_id: str) -> SemanticTermRefV2:
    return SemanticTermRefV2(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.dressup-transform-test-source",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"pfg:{term_id}"),
    )


SOURCE_TERMS = (
    _pfg_term("source_structure", "structure.test-source"),
    _pfg_term("source_family", "family.test-source"),
    _pfg_term("source_operation", "operation.test-source"),
)


def _parameters(operation: PartDesignDressupTransformOperation) -> dict[str, object]:
    edge_role = {"axis": "z", "first_side": "minimum", "second_side": "minimum"}
    face_role = {"axis": "z", "side": "maximum"}
    return {
        PartDesignDressupTransformOperation.SCALED: {
            "factor": 1.5,
            "occurrences": 2,
        },
        PartDesignDressupTransformOperation.MULTI_TRANSFORM: {
            "steps": [
                {
                    "step_id": "scale_primary",
                    "kind": "scaled",
                    "parameters": {"factor": 1.25, "occurrences": 2},
                },
                {
                    "step_id": "mirror_yz",
                    "kind": "mirrored",
                    "parameters": {"mirror_plane": "yz"},
                },
            ]
        },
        PartDesignDressupTransformOperation.FILLET: {
            "edge_role": edge_role,
            "radius_mm": 1.0,
        },
        PartDesignDressupTransformOperation.CHAMFER: {
            "edge_role": edge_role,
            "size_mm": 1.0,
        },
        PartDesignDressupTransformOperation.DRAFT: {
            "face_role": face_role,
            "neutral_plane": "yz",
            "pull_direction": "x",
            "angle_degrees": 5.0,
            "reversed": False,
        },
        PartDesignDressupTransformOperation.THICKNESS: {
            "face_role": face_role,
            "value_mm": 1.0,
        },
    }[operation]


def _failure_parameters(
    operation: PartDesignDressupTransformOperation,
) -> dict[str, object]:
    value = _parameters(operation)
    if operation is PartDesignDressupTransformOperation.SCALED:
        value["factor"] = 1.0
    elif operation is PartDesignDressupTransformOperation.MULTI_TRANSFORM:
        value["steps"] = [
            {
                "step_id": "scale_noop_a",
                "kind": "scaled",
                "parameters": {"factor": 1.0, "occurrences": 2},
            },
            {
                "step_id": "scale_noop_b",
                "kind": "scaled",
                "parameters": {"factor": 1.0, "occurrences": 2},
            },
        ]
    elif operation is PartDesignDressupTransformOperation.FILLET:
        value["radius_mm"] = 1_000.0
    elif operation is PartDesignDressupTransformOperation.CHAMFER:
        value["size_mm"] = 1_000.0
    elif operation is PartDesignDressupTransformOperation.DRAFT:
        value["angle_degrees"] = 0.0
    else:
        value["value_mm"] = 1_000.0
    return value


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
                semantic_role_term_ref_id=DRESSUP_TRANSFORM_SOLID_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=DRESSUP_TRANSFORM_SOLID_TYPE_TERM.term_ref_id,
            ),
        ),
    )


def _graph(
    operation: PartDesignDressupTransformOperation,
    *,
    parameter_value: object | None = None,
    operation_definition: str | None = None,
) -> ParametricFeatureGraphV2:
    operation_terms = next(
        item for item in DRESSUP_TRANSFORM_OPERATION_TERMS if item.operation is operation
    )
    static_terms = list(DRESSUP_TRANSFORM_PFG_TERMS)
    if operation_definition is not None:
        index = static_terms.index(operation_terms.operation_term)
        static_terms[index] = dataclasses.replace(
            operation_terms.operation_term,
            term_definition_sha256=operation_definition,
        )
    base = _source_node()
    parameter = DesignParameterV2(
        parameter_id="parameter_operation",
        name="PartDesign::Fillet Edge1 TypeId strings are inert",
        semantic_role_term_ref_id=DRESSUP_TRANSFORM_PARAMETERS_ROLE_TERM.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id="value_operation",
            value_type_term_ref_id=DRESSUP_TRANSFORM_PARAMETERS_TYPE_TERM.term_ref_id,
            encoding_term_ref_id=DRESSUP_TRANSFORM_CANONICAL_JSON_TERM.term_ref_id,
            value=_parameters(operation) if parameter_value is None else parameter_value,
        ),
    )
    target = FeatureNodeV2(
        node_id="node_target",
        body_id="body_main",
        name="Face6 Radius UseAllEdges must never select native code",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=DRESSUP_TRANSFORM_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=operation_terms.family_term.term_ref_id,
            operation_term_ref_id=operation_terms.operation_term.term_ref_id,
            input_ports=(
                FeatureInputPortV2(
                    port_id="port_base",
                    semantic_role_term_ref_id=DRESSUP_TRANSFORM_BASE_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=DRESSUP_TRANSFORM_SOLID_TYPE_TERM.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
                FeatureInputPortV2(
                    port_id="port_parameters",
                    semantic_role_term_ref_id=DRESSUP_TRANSFORM_PARAMETERS_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=DRESSUP_TRANSFORM_PARAMETERS_TYPE_TERM.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
            ),
            dependencies=(
                FeatureDependencyV2(
                    dependency_id="dependency_base",
                    port_id="port_base",
                    upstream_node_id=base.node_id,
                    upstream_result_id="result_base",
                ),
            ),
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
                semantic_role_term_ref_id=DRESSUP_TRANSFORM_SOLID_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=DRESSUP_TRANSFORM_SOLID_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id=f"graph_{operation.value}",
        name=f"Dress-up/transform {operation.value}",
        terms=tuple((*static_terms, *SOURCE_TERMS)),
        bodies=(FeatureBodyV2(body_id="body_main", name="Main"),),
        parameters=(parameter,),
        references=(),
        nodes=(base, target),
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
        namespace="org.vibecad.dressup-transform-proof-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"proof:{term_id}"),
    )


RULE = _proof_term("rule_dressup_transform_target", "rule.dressup-transform-target-reviewed")
PREDICATE = _proof_term(
    "predicate_dressup_transform_target", "predicate.dressup-transform-target-reviewed"
)
ROLE_PREMISE = _proof_term(
    "role_dressup_transform_candidate", "proof-role.dressup-transform-candidate"
)
ROLE_CONCLUSION = _proof_term(
    "role_dressup_transform_validated", "proof-role.dressup-transform-validated"
)
STRUCTURE_BRIDGE = _bridge_from_pfg(DRESSUP_TRANSFORM_STRUCTURE_TERM)


class _Evaluator:
    def __init__(self) -> None:
        def signature(role: BridgeTermRef) -> RuleEndpointSignature:
            return RuleEndpointSignature(
                selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
                role_term=role,
                subject_type_term=STRUCTURE_BRIDGE,
            )

        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="partdesign_dressup_transform_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("partdesign-dressup-transform-target-evaluator-v1"),
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
            role_term_ref_id=DRESSUP_TRANSFORM_INTENT_DOCUMENT_ROLE_TERM.term_ref_id,
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
            STRUCTURE_BRIDGE,
            DRESSUP_TRANSFORM_INTENT_DOCUMENT_ROLE_TERM,
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_dressup_transform_target",
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
                producer_id="dressup_transform_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("dressup-transform-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("upstream-dressup-transform-compile-request"),
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
    max_output_bytes: int = MAX_PARTDESIGN_DRESSUP_TRANSFORM_PLAN_BYTES,
) -> tuple[BackendLoweringRequest, _Reader, TrustedRulePolicy]:
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = build_dressup_transform_capability_document()
    policy = TrustedRulePolicy(evaluators=(_Evaluator(),))
    request = BackendLoweringRequest(
        adapter=FREECAD_PARTDESIGN_DRESSUP_TRANSFORM_ADAPTER_DESCRIPTOR,
        terms=tuple(
            (
                *DRESSUP_TRANSFORM_REQUEST_TERMS,
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
    adapter: FreeCADPartDesignDressupTransformAdapter,
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


@pytest.mark.parametrize("operation", tuple(PartDesignDressupTransformOperation))
def test_shared_adapter_lowers_all_six_without_native_string_authority(
    operation: PartDesignDressupTransformOperation,
) -> None:
    request, reader, policy = _request(_graph(operation))
    sink = _MemoryPlanSink()
    adapter = FreeCADPartDesignDressupTransformAdapter(sink)

    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    repeated, repeated_receipt = _lower(adapter, request, reader, policy)

    assert isinstance(sink, PlanSink)
    assert isinstance(adapter, IntentBackendAdapter)
    assert result.disposition is BridgeDisposition.COMPLETE
    assert result.plan_document == receipt.plan_document
    assert result.supported_subjects == (_subject(),)
    assert plan.operation is operation
    assert payload == plan.canonical_bytes
    assert result.plan_document.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert result.plan_document.document_digest == plan.plan_sha256
    assert adapter.executable is False and adapter.grants_execution_authority is False
    assert receipt.executable is False and receipt.grants_execution_authority is False
    assert plan.executable is False and plan.grants_execution_authority is False
    assert repeated == result and repeated_receipt == receipt and len(sink.items) == 1
    text = payload.decode("ascii")
    assert "PartDesign::" not in text
    assert "Edge1" not in text and "Face6" not in text
    assert all(name not in text for name in ("UseAllEdges", "Transformations", "Radius"))
    if operation is PartDesignDressupTransformOperation.MULTI_TRANSFORM:
        assert isinstance(plan.parameters, MultiTransformParameters)
        assert [step.step_id for step in plan.parameters.steps] == [
            "scale_primary",
            "mirror_yz",
        ]


def test_adapter_rejects_semantic_substitution_native_injection_and_sink_failure() -> None:
    graph = _graph(
        PartDesignDressupTransformOperation.FILLET,
        operation_definition="f" * 64,
    )
    request, reader, policy = _request(graph)
    sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as semantic_error:
        _lower(FreeCADPartDesignDressupTransformAdapter(sink), request, reader, policy)
    assert semantic_error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    injected = _parameters(PartDesignDressupTransformOperation.FILLET)
    injected["TypeId"] = "PartDesign::Thickness"
    request, reader, policy = _request(
        _graph(PartDesignDressupTransformOperation.FILLET, parameter_value=injected)
    )
    with pytest.raises(IntentBridgeError) as parameter_error:
        _lower(FreeCADPartDesignDressupTransformAdapter(sink), request, reader, policy)
    assert parameter_error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    request, reader, policy = _request(_graph(PartDesignDressupTransformOperation.THICKNESS))
    failed_sink = _MemoryPlanSink(fail=True)
    with pytest.raises(IntentBridgeError) as sink_error:
        _lower(FreeCADPartDesignDressupTransformAdapter(failed_sink), request, reader, policy)
    assert sink_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert failed_sink.items == {}
    assert "untrusted detail" not in str(sink_error.value)


def test_plan_budget_canonical_decoder_and_multi_step_closure() -> None:
    graph = _graph(PartDesignDressupTransformOperation.MULTI_TRANSFORM)
    request, reader, policy = _request(graph)
    adapter = FreeCADPartDesignDressupTransformAdapter(_MemoryPlanSink())
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    assert (
        decode_partdesign_dressup_transform_backend_plan(
            payload,
            expected_content_sha256=result.plan_document.content_sha256,
            expected_plan_sha256=result.plan_document.document_digest,
        )
        == plan
    )
    exact, exact_reader, exact_policy = _request(graph, max_output_bytes=len(payload))
    exact_result, _ = _lower(
        FreeCADPartDesignDressupTransformAdapter(_MemoryPlanSink()),
        exact,
        exact_reader,
        exact_policy,
    )
    assert exact_result.plan_document.size_bytes == len(payload)
    small, small_reader, small_policy = _request(graph, max_output_bytes=len(payload) - 1)
    small_sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as budget_error:
        _lower(
            FreeCADPartDesignDressupTransformAdapter(small_sink),
            small,
            small_reader,
            small_policy,
        )
    assert budget_error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert small_sink.items == {}
    with pytest.raises(PartDesignDressupTransformRuleError):
        decode_partdesign_dressup_transform_backend_plan(payload + b" ")

    duplicate = _parameters(PartDesignDressupTransformOperation.MULTI_TRANSFORM)
    duplicate["steps"][1]["step_id"] = "scale_primary"
    request, reader, policy = _request(
        _graph(PartDesignDressupTransformOperation.MULTI_TRANSFORM, parameter_value=duplicate)
    )
    with pytest.raises(IntentBridgeError) as closure_error:
        _lower(FreeCADPartDesignDressupTransformAdapter(_MemoryPlanSink()), request, reader, policy)
    assert closure_error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION

    mapping = plan.to_mapping()
    mapping["operation"]["parameters"]["steps"][0]["parameters"]["factor"] = 10**4000
    adversarial = json.dumps(
        mapping,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    with pytest.raises(PartDesignDressupTransformRuleError) as numeric_error:
        decode_partdesign_dressup_transform_backend_plan(adversarial)
    assert len(str(numeric_error.value)) < 180


@pytest.mark.slow
def test_real_freecad_dressups_transforms_batch_create_edit_reopen_and_rollback(
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

    success_cases = []
    failure_cases = []
    for operation in PartDesignDressupTransformOperation:
        for collection, parameter_value, label in (
            (success_cases, _parameters(operation), "success"),
            (failure_cases, _failure_parameters(operation), "rollback"),
        ):
            request, reader, policy = _request(_graph(operation, parameter_value=parameter_value))
            adapter = FreeCADPartDesignDressupTransformAdapter(_MemoryPlanSink())
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
                    "base": (plan.base.node_id, plan.base.result_id),
                }
            )

    model_path = tmp_path / "partdesign-dressup-transform.FCStd"
    source_root = Path(__file__).parents[1] / "src"
    code = f"""
import os, sys
sys.path.insert(0, {str(source_root)!r})
from vibecad.freecad_env import prepare_freecad_import
prepare_freecad_import()
from pathlib import Path
import FreeCAD, Part
from vibecad.parametric.freecad_partdesign_dressup_transform_rules import (
    AuthenticatedDressupTransformObject,
    PartDesignDressupTransformExecutionBindings,
    PartDesignDressupTransformRuleError,
    PartDesignDressupTransformRuleErrorCode,
    apply_partdesign_dressup_transform_plan,
)

SUCCESS_CASES = {success_cases!r}
FAILURE_CASES = {failure_cases!r}
EXPECTED_TYPE = {{
    'scaled': 'PartDesign::Scaled',
    'multi_transform': 'PartDesign::MultiTransform',
    'fillet': 'PartDesign::Fillet',
    'chamfer': 'PartDesign::Chamfer',
    'draft': 'PartDesign::Draft',
    'thickness': 'PartDesign::Thickness',
}}
EDIT = {{
    'scaled': ('Factor', 1.6),
    'multi_transform': ('Factor', 1.35),
    'fillet': ('Radius', 1.5),
    'chamfer': ('Size', 1.5),
    'draft': ('Angle', 8.0),
    'thickness': ('Value', 1.5),
}}

def make_bindings(document, entry, index, *, cylinder=False):
    body = document.addObject('PartDesign::Body', f'Body{{index}}')
    if cylinder:
        base = body.newObject('PartDesign::Feature', f'Base{{index}}')
        base.Shape = Part.makeCylinder(5, 14)
    else:
        base = body.newObject('PartDesign::AdditiveBox', f'Base{{index}}')
        base.Length = 10.0
        base.Width = 12.0
        base.Height = 14.0
    document.recompute()
    authenticated = AuthenticatedDressupTransformObject(
        object=base,
        node_id=entry['base'][0],
        result_id=entry['base'][1],
    )
    return body, base, PartDesignDressupTransformExecutionBindings(
        document=document,
        body=body,
        body_id=entry['body_id'],
        base=authenticated,
    )

document = FreeCAD.newDocument('PartDesignDressupTransformBatch')
document.UndoMode = 1
persisted = []
for index, entry in enumerate(SUCCESS_CASES):
    body, base, bindings = make_bindings(document, entry, index)
    payload = Path(entry['path']).read_bytes()
    try:
        receipt = apply_partdesign_dressup_transform_plan(
            payload,
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings,
        )
    except Exception as error:
        raise AssertionError(f"create failed: {{entry['operation']}}") from error
    feature = document.getObject(receipt.object_names[0])
    assert feature.TypeId == EXPECTED_TYPE[entry['operation']]
    assert feature is body.Tip and feature.BaseFeature is base
    assert feature.isValid() and len(feature.Shape.Solids) == 1
    if entry['operation'] == 'multi_transform':
        assert len(receipt.object_names) == 3
        assert tuple(item.Name for item in feature.Transformations) == receipt.object_names[1:]
        assert tuple(item.TypeId for item in feature.Transformations) == (
            'PartDesign::Scaled', 'PartDesign::Mirrored'
        )
        edit_target = feature.Transformations[0]
    else:
        assert len(receipt.object_names) == 1
        edit_target = feature
    before_edit = float(feature.Shape.Volume)
    property_name, value = EDIT[entry['operation']]
    setattr(edit_target, property_name, value)
    document.recompute()
    after_edit = float(feature.Shape.Volume)
    assert feature.isValid() and len(feature.Shape.Solids) == 1
    assert abs(after_edit - before_edit) > 1e-7
    base.Length = 11.0
    document.recompute()
    after_base_edit = float(feature.Shape.Volume)
    assert feature.isValid() and len(feature.Shape.Solids) == 1
    assert abs(after_base_edit - after_edit) > 1e-7
    persisted.append((
        feature.Name,
        EXPECTED_TYPE[entry['operation']],
        base.Name,
        None if entry['operation'] != 'multi_transform' else receipt.object_names[1:],
        after_base_edit,
    ))

document.saveAs({str(model_path)!r})
FreeCAD.closeDocument(document.Name)
reopened = FreeCAD.openDocument({str(model_path)!r})
reopened.recompute()
for name, type_id, base_name, child_names, volume in persisted:
    feature = reopened.getObject(name)
    assert feature.TypeId == type_id and feature.isValid()
    assert len(feature.Shape.Solids) == 1 and feature.BaseFeature.Name == base_name
    assert abs(float(feature.Shape.Volume) - volume) < 1e-6
    if child_names is not None:
        assert tuple(item.Name for item in feature.Transformations) == tuple(child_names)
FreeCAD.closeDocument(reopened.Name)

# Each operation crosses the same transactional failure gate.  Aborting must
# restore document objects, Body membership/Tip and source visibility exactly.
rollback_document = FreeCAD.newDocument('PartDesignDressupTransformRollbackBatch')
rollback_document.UndoMode = 1
for index, entry in enumerate(FAILURE_CASES):
    body, _base, bindings = make_bindings(rollback_document, entry, index)
    payload = Path(entry['path']).read_bytes()
    before_objects = tuple(rollback_document.Objects)
    before_group = tuple(body.Group)
    before_tip = body.Tip
    before_visibility = tuple(bool(item.Visibility) for item in before_group)
    try:
        apply_partdesign_dressup_transform_plan(
            payload,
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings,
        )
    except PartDesignDressupTransformRuleError:
        pass
    else:
        raise AssertionError(f"{{entry['operation']}} invalid/no-op must fail")
    assert tuple(rollback_document.Objects) == before_objects
    assert tuple(body.Group) == before_group and body.Tip is before_tip
    assert tuple(bool(item.Visibility) for item in before_group) == before_visibility
FreeCAD.closeDocument(rollback_document.Name)

# Fillet/Chamfer never trust a durable EdgeN.  The same semantic role is
# resolved against a cylindrical authenticated source and fails closed before
# any mutation because no unique full bounding-box line edge exists.
resolution_document = FreeCAD.newDocument('PartDesignDressupResolutionBatch')
resolution_document.UndoMode = 1
for index, entry in enumerate(
    item for item in SUCCESS_CASES if item['operation'] in ('fillet', 'chamfer')
):
    body, _base, bindings = make_bindings(
        resolution_document, entry, index, cylinder=True
    )
    before_objects = tuple(resolution_document.Objects)
    before_group = tuple(body.Group)
    try:
        apply_partdesign_dressup_transform_plan(
            Path(entry['path']).read_bytes(),
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings,
        )
    except PartDesignDressupTransformRuleError as error:
        assert error.code is PartDesignDressupTransformRuleErrorCode.RESOLUTION_FAILED
    else:
        raise AssertionError('semantic edge resolution must fail closed')
    assert tuple(resolution_document.Objects) == before_objects
    assert tuple(body.Group) == before_group and body.Tip is before_group[-1]
FreeCAD.closeDocument(resolution_document.Name)
print('REAL_PARTDESIGN_DRESSUP_TRANSFORM_BATCH_OK')
"""
    completed = subprocess.run(
        [str(runtime_python), "-c", code],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    assert "REAL_PARTDESIGN_DRESSUP_TRANSFORM_BATCH_OK" in completed.stdout
