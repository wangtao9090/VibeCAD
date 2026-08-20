"""Focused contract gate and one managed FreeCAD batch for PartDesign Boolean."""

from __future__ import annotations

import dataclasses
import hashlib
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
from vibecad.intent_bridge.freecad_partdesign_boolean_adapter import (
    BOOLEAN_BASE_ROLE_TERM,
    BOOLEAN_FAMILY_TERM,
    BOOLEAN_INTENT_DOCUMENT_ROLE_TERM,
    BOOLEAN_OPERATION_TERMS,
    BOOLEAN_PFG_TERMS,
    BOOLEAN_REQUEST_TERMS,
    BOOLEAN_SOLID_RESULT_ROLE_TERM,
    BOOLEAN_SOLID_TYPE_TERM,
    BOOLEAN_STRUCTURE_TERM,
    BOOLEAN_TOOLS_ROLE_TERM,
    FREECAD_PARTDESIGN_BOOLEAN_ADAPTER_DESCRIPTOR,
    FreeCADPartDesignBooleanAdapter,
    build_boolean_capability_document,
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
    FeatureBodyV2,
    FeatureDependencyV2,
    FeatureGraphResultV2,
    FeatureInputPortV2,
    FeatureIntentV2,
    FeatureNodeV2,
    FeatureResultV2,
    ParametricFeatureGraphV2,
    SemanticTermRefV2,
)
from vibecad.parametric.freecad_partdesign_boolean_rules import (
    MAX_PARTDESIGN_BOOLEAN_PLAN_BYTES,
    PartDesignBooleanOperation,
    PartDesignBooleanRuleError,
    decode_partdesign_boolean_backend_plan,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pfg_term(term_ref_id: str, term_id: str) -> SemanticTermRefV2:
    return SemanticTermRefV2(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.boolean-test-source",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"pfg:{term_id}"),
    )


SOURCE_TERMS = (
    _pfg_term("source_structure", "structure.test-source"),
    _pfg_term("source_family", "family.test-source"),
    _pfg_term("source_operation", "operation.test-source"),
)


def _source_node(node_id: str, body_id: str, result_id: str) -> FeatureNodeV2:
    return FeatureNodeV2(
        node_id=node_id,
        body_id=body_id,
        name="Untrusted PartDesign::Boolean Group Type strings are inert",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=SOURCE_TERMS[0].term_ref_id,
            family_term_ref_id=SOURCE_TERMS[1].term_ref_id,
            operation_term_ref_id=SOURCE_TERMS[2].term_ref_id,
        ),
        results=(
            FeatureResultV2(
                result_id=result_id,
                semantic_role_term_ref_id=BOOLEAN_SOLID_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=BOOLEAN_SOLID_TYPE_TERM.term_ref_id,
            ),
        ),
    )


def _graph(
    operation: PartDesignBooleanOperation,
    *,
    tool_in_target_body: bool = False,
    operation_definition: str | None = None,
) -> ParametricFeatureGraphV2:
    operation_terms = next(item for item in BOOLEAN_OPERATION_TERMS if item.operation is operation)
    static_terms = list(BOOLEAN_PFG_TERMS)
    if operation_definition is not None:
        index = static_terms.index(operation_terms.operation_term)
        static_terms[index] = dataclasses.replace(
            operation_terms.operation_term,
            term_definition_sha256=operation_definition,
        )
    tool_body_id = "body_main" if tool_in_target_body else "body_tool"
    base = _source_node("node_base", "body_main", "result_base")
    tool = _source_node("node_tool", tool_body_id, "result_tool")
    target = FeatureNodeV2(
        node_id="node_target",
        body_id="body_main",
        name="Reviewed semantic Boolean target",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=BOOLEAN_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=BOOLEAN_FAMILY_TERM.term_ref_id,
            operation_term_ref_id=operation_terms.operation_term.term_ref_id,
            input_ports=(
                FeatureInputPortV2(
                    port_id="port_base",
                    semantic_role_term_ref_id=BOOLEAN_BASE_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=BOOLEAN_SOLID_TYPE_TERM.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
                FeatureInputPortV2(
                    port_id="port_tools",
                    semantic_role_term_ref_id=BOOLEAN_TOOLS_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=BOOLEAN_SOLID_TYPE_TERM.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=True,
                ),
            ),
            dependencies=(
                FeatureDependencyV2(
                    dependency_id="dependency_base",
                    port_id="port_base",
                    upstream_node_id=base.node_id,
                    upstream_result_id="result_base",
                ),
                FeatureDependencyV2(
                    dependency_id="dependency_tool",
                    port_id="port_tools",
                    upstream_node_id=tool.node_id,
                    upstream_result_id="result_tool",
                ),
            ),
        ),
        results=(
            FeatureResultV2(
                result_id="result_target",
                semantic_role_term_ref_id=BOOLEAN_SOLID_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=BOOLEAN_SOLID_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    bodies = [FeatureBodyV2(body_id="body_main", name="Target")]
    if not tool_in_target_body:
        bodies.append(FeatureBodyV2(body_id="body_tool", name="Tool"))
    return ParametricFeatureGraphV2(
        graph_id=f"graph_partdesign_boolean_{operation.value}",
        name=f"PartDesign Boolean {operation.value}",
        terms=tuple((*static_terms, *SOURCE_TERMS)),
        bodies=tuple(bodies),
        parameters=(),
        references=(),
        nodes=(base, tool, target),
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
        namespace="org.vibecad.boolean-proof-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"proof:{term_id}"),
    )


RULE = _proof_term("rule_boolean_target", "rule.boolean-target-reviewed")
PREDICATE = _proof_term("predicate_boolean_target", "predicate.boolean-target-reviewed")
ROLE_PREMISE = _proof_term("role_boolean_candidate", "proof-role.boolean-candidate")
ROLE_CONCLUSION = _proof_term("role_boolean_validated", "proof-role.boolean-validated")
BOOLEAN_STRUCTURE_BRIDGE = _bridge_from_pfg(BOOLEAN_STRUCTURE_TERM)


class _BooleanEvaluator:
    def __init__(self) -> None:
        def signature(role: BridgeTermRef) -> RuleEndpointSignature:
            return RuleEndpointSignature(
                selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
                role_term=role,
                subject_type_term=BOOLEAN_STRUCTURE_BRIDGE,
            )

        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="partdesign_boolean_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("partdesign-boolean-target-evaluator-v1"),
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
            artifact_id="artifact_boolean_pfg",
            role_term_ref_id=BOOLEAN_INTENT_DOCUMENT_ROLE_TERM.term_ref_id,
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
        artifact_id="artifact_boolean_pfg",
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
            BOOLEAN_STRUCTURE_BRIDGE,
            BOOLEAN_INTENT_DOCUMENT_ROLE_TERM,
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_boolean_target",
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
                producer_id="boolean_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("boolean-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("upstream-boolean-compile-request"),
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
    max_output_bytes: int = MAX_PARTDESIGN_BOOLEAN_PLAN_BYTES,
) -> tuple[BackendLoweringRequest, _Reader, TrustedRulePolicy]:
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = build_boolean_capability_document()
    policy = TrustedRulePolicy(evaluators=(_BooleanEvaluator(),))
    request = BackendLoweringRequest(
        adapter=FREECAD_PARTDESIGN_BOOLEAN_ADAPTER_DESCRIPTOR,
        terms=tuple(
            (
                *BOOLEAN_REQUEST_TERMS,
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
    adapter: FreeCADPartDesignBooleanAdapter,
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


def test_boolean_adapter_canonical_authority_free_and_fail_closed() -> None:
    for operation in PartDesignBooleanOperation:
        request, reader, policy = _request(_graph(operation))
        sink = _MemoryPlanSink()
        adapter = FreeCADPartDesignBooleanAdapter(sink)
        result, receipt = _lower(adapter, request, reader, policy)
        plan, payload = adapter.read_plan(receipt)
        repeated, repeated_receipt = _lower(adapter, request, reader, policy)

        assert isinstance(sink, PlanSink)
        assert isinstance(adapter, IntentBackendAdapter)
        assert result.disposition is BridgeDisposition.COMPLETE
        assert result.plan_document == receipt.plan_document
        assert result.supported_subjects == (_subject(),)
        assert plan.operation is operation
        assert plan.base.to_mapping() == {
            "body_id": "body_main",
            "node_id": "node_base",
            "result_id": "result_base",
        }
        assert tuple(item.body_id for item in plan.tools) == ("body_tool",)
        assert payload == plan.canonical_bytes
        assert result.plan_document.content_sha256 == hashlib.sha256(payload).hexdigest()
        assert result.plan_document.document_digest == plan.plan_sha256
        assert adapter.executable is False and adapter.grants_execution_authority is False
        assert receipt.executable is False and receipt.grants_execution_authority is False
        assert plan.executable is False and plan.grants_execution_authority is False
        assert repeated == result and repeated_receipt == receipt and len(sink.items) == 1
        text = payload.decode("ascii")
        assert "PartDesign::Boolean" not in text
        assert all(name not in text for name in ("BaseFeature", "Group", "Fuse", "Cut", "Common"))
        assert (
            decode_partdesign_boolean_backend_plan(
                payload,
                expected_content_sha256=result.plan_document.content_sha256,
                expected_plan_sha256=result.plan_document.document_digest,
            )
            == plan
        )
        exact_request, exact_reader, exact_policy = _request(
            _graph(operation),
            max_output_bytes=len(payload),
        )
        exact_result, _ = _lower(
            FreeCADPartDesignBooleanAdapter(_MemoryPlanSink()),
            exact_request,
            exact_reader,
            exact_policy,
        )
        assert exact_result.plan_document.size_bytes == len(payload)

    with pytest.raises(PartDesignBooleanRuleError):
        decode_partdesign_boolean_backend_plan(payload + b" ")

    wide_graph = _graph(PartDesignBooleanOperation.COMMON)
    wide_target = next(node for node in wide_graph.nodes if node.node_id == "node_target")
    wide_ports = tuple(
        dataclasses.replace(port, maximum_cardinality=2) if port.port_id == "port_tools" else port
        for port in wide_target.intent.input_ports
    )
    wide_target = dataclasses.replace(
        wide_target,
        intent=dataclasses.replace(wide_target.intent, input_ports=wide_ports),
    )
    wide_graph = dataclasses.replace(
        wide_graph,
        nodes=tuple(
            wide_target if node.node_id == wide_target.node_id else node
            for node in wide_graph.nodes
        ),
    )
    for invalid_graph in (
        _graph(PartDesignBooleanOperation.FUSE, tool_in_target_body=True),
        _graph(
            PartDesignBooleanOperation.CUT,
            operation_definition="f" * 64,
        ),
        wide_graph,
    ):
        request, reader, policy = _request(invalid_graph)
        sink = _MemoryPlanSink()
        with pytest.raises(IntentBridgeError) as error:
            _lower(FreeCADPartDesignBooleanAdapter(sink), request, reader, policy)
        assert error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
        assert sink.items == {}

    request, reader, policy = _request(_graph(PartDesignBooleanOperation.COMMON))
    failed_sink = _MemoryPlanSink(fail=True)
    with pytest.raises(IntentBridgeError) as sink_error:
        _lower(FreeCADPartDesignBooleanAdapter(failed_sink), request, reader, policy)
    assert sink_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert failed_sink.items == {}
    assert "untrusted detail" not in str(sink_error.value)

    request, reader, policy = _request(_graph(PartDesignBooleanOperation.FUSE))
    probe_adapter = FreeCADPartDesignBooleanAdapter(_MemoryPlanSink())
    _, probe_receipt = _lower(probe_adapter, request, reader, policy)
    _, probe_payload = probe_adapter.read_plan(probe_receipt)
    too_small, small_reader, small_policy = _request(
        _graph(PartDesignBooleanOperation.FUSE),
        max_output_bytes=len(probe_payload) - 1,
    )
    small_sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as budget_error:
        _lower(
            FreeCADPartDesignBooleanAdapter(small_sink),
            too_small,
            small_reader,
            small_policy,
        )
    assert budget_error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert small_sink.items == {}


@pytest.mark.slow
def test_real_freecad_boolean_batch_create_edit_reopen_invalid_and_rollback(
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
    for operation in PartDesignBooleanOperation:
        request, reader, policy = _request(_graph(operation))
        adapter = FreeCADPartDesignBooleanAdapter(_MemoryPlanSink())
        result, receipt = _lower(adapter, request, reader, policy)
        plan, payload = adapter.read_plan(receipt)
        plan_path = tmp_path / f"partdesign-boolean-{operation.value}.json"
        plan_path.write_bytes(payload)
        cases.append(
            {
                "operation": operation.value,
                "path": str(plan_path),
                "content_sha256": result.plan_document.content_sha256,
                "plan_sha256": result.plan_document.document_digest,
                "target_body_id": plan.body_id,
                "base": plan.base.to_mapping(),
                "tools": [item.to_mapping() for item in plan.tools],
            }
        )

    model_path = tmp_path / "partdesign-boolean.FCStd"
    source_root = Path(__file__).parents[1] / "src"
    code = f"""
import os, sys
sys.path.insert(0, {str(source_root)!r})
from vibecad.freecad_env import prepare_freecad_import
prepare_freecad_import()
from pathlib import Path
import FreeCAD
from vibecad.parametric.freecad_partdesign_boolean_rules import (
    AuthenticatedBooleanOperand,
    PartDesignBooleanExecutionBindings,
    PartDesignBooleanRuleError,
    apply_partdesign_boolean_plan,
)

CASES = {cases!r}
NATIVE = {{'fuse': 'Fuse', 'cut': 'Cut', 'common': 'Common'}}

def make_bindings(document, entry, index, *, tool_z=-5.0):
    target = document.addObject('PartDesign::Body', f'TargetBody{{index}}')
    base = target.newObject('PartDesign::AdditiveBox', f'Base{{index}}')
    base.Length = 20.0
    base.Width = 20.0
    base.Height = 20.0
    tool_body = document.addObject('PartDesign::Body', f'ToolBody{{index}}')
    tool = tool_body.newObject('PartDesign::AdditiveBox', f'Tool{{index}}')
    tool.Length = 10.0
    tool.Width = 10.0
    tool.Height = 30.0
    tool.Placement.Base = FreeCAD.Vector(5.0, 5.0, tool_z)
    document.recompute()
    base_binding = AuthenticatedBooleanOperand(
        object=base,
        body=target,
        body_id=entry['base']['body_id'],
        node_id=entry['base']['node_id'],
        result_id=entry['base']['result_id'],
    )
    tool_spec = entry['tools'][0]
    tool_binding = AuthenticatedBooleanOperand(
        object=tool,
        body=tool_body,
        body_id=tool_spec['body_id'],
        node_id=tool_spec['node_id'],
        result_id=tool_spec['result_id'],
    )
    return target, base, tool_body, tool, PartDesignBooleanExecutionBindings(
        document=document,
        target_body=target,
        target_body_id=entry['target_body_id'],
        base=base_binding,
        tools=(tool_binding,),
    )

document = FreeCAD.newDocument('PartDesignBooleanBatch')
document.UndoMode = 1
persisted = []
for index, entry in enumerate(CASES):
    target, base, tool_body, tool, bindings = make_bindings(document, entry, index)
    payload = Path(entry['path']).read_bytes()
    receipt = apply_partdesign_boolean_plan(
        payload,
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=bindings,
    )
    feature = document.getObject(receipt.object_name)
    assert feature.TypeId == 'PartDesign::Boolean'
    assert str(feature.Type) == NATIVE[entry['operation']]
    assert feature.BaseFeature is base and tuple(feature.Group) == (tool_body,)
    assert target.Tip is feature and feature.isValid() and len(feature.Shape.Solids) == 1
    before_edit = float(feature.Shape.Volume)
    tool.Length = 12.0
    document.recompute()
    after_edit = float(feature.Shape.Volume)
    assert feature.isValid() and tuple(feature.State) == ('Up-to-date',)
    assert abs(after_edit - before_edit) > 1e-7
    persisted.append((
        feature.Name,
        NATIVE[entry['operation']],
        base.Name,
        tool_body.Name,
        tool.Name,
        after_edit,
    ))

document.saveAs({str(model_path)!r})
FreeCAD.closeDocument(document.Name)
reopened = FreeCAD.openDocument({str(model_path)!r})
reopened.recompute()
for feature_name, native, base_name, tool_body_name, tool_name, volume in persisted:
    feature = reopened.getObject(feature_name)
    assert feature.TypeId == 'PartDesign::Boolean' and str(feature.Type) == native
    assert feature.BaseFeature.Name == base_name
    assert tuple(item.Name for item in feature.Group) == (tool_body_name,)
    assert abs(float(reopened.getObject(tool_name).Length) - 12.0) < 1e-9
    assert feature.isValid() and len(feature.Shape.Solids) == 1
    assert abs(float(feature.Shape.Volume) - volume) < 1e-6
FreeCAD.closeDocument(reopened.Name)

# A semantically correct ID bound to the wrong live owner Body must fail before mutation.
invalid_document = FreeCAD.newDocument('PartDesignBooleanCrossBodyInvalid')
invalid_document.UndoMode = 1
entry = CASES[0]
target, base, tool_body, tool, bindings = make_bindings(invalid_document, entry, 100)
tool_semantic = entry['tools'][0]
wrong_tool = AuthenticatedBooleanOperand(
    object=tool,
    body=target,
    body_id=tool_semantic['body_id'],
    node_id=tool_semantic['node_id'],
    result_id=tool_semantic['result_id'],
)
wrong_bindings = PartDesignBooleanExecutionBindings(
    document=invalid_document,
    target_body=target,
    target_body_id=entry['target_body_id'],
    base=bindings.base,
    tools=(wrong_tool,),
)
before_objects = tuple(invalid_document.Objects)
before_group = tuple(target.Group)
before_tip = target.Tip
try:
    apply_partdesign_boolean_plan(
        Path(entry['path']).read_bytes(),
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=wrong_bindings,
    )
except PartDesignBooleanRuleError:
    pass
else:
    raise AssertionError('cross-body owner substitution must fail')
assert tuple(invalid_document.Objects) == before_objects
assert tuple(target.Group) == before_group and target.Tip is before_tip
FreeCAD.closeDocument(invalid_document.Name)

# A late native Common failure must abort and restore topology and visibility exactly.
rollback_document = FreeCAD.newDocument('PartDesignBooleanRollback')
rollback_document.UndoMode = 1
entry = next(item for item in CASES if item['operation'] == 'common')
target, base, tool_body, tool, bindings = make_bindings(
    rollback_document, entry, 200, tool_z=100.0
)
tool.Placement.Base = FreeCAD.Vector(100.0, 100.0, 100.0)
rollback_document.recompute()
before_objects = tuple(rollback_document.Objects)
before_target_group = tuple(target.Group)
before_target_tip = target.Tip
before_tool_group = tuple(tool_body.Group)
before_tool_tip = tool_body.Tip
before_visibility = tuple(
    (item, bool(item.Visibility))
    for item in rollback_document.Objects
    if hasattr(item, 'Visibility')
)
try:
    apply_partdesign_boolean_plan(
        Path(entry['path']).read_bytes(),
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=bindings,
    )
except PartDesignBooleanRuleError:
    pass
else:
    raise AssertionError('disjoint Common must fail and roll back')
assert tuple(rollback_document.Objects) == before_objects
assert tuple(target.Group) == before_target_group and target.Tip is before_target_tip
assert tuple(tool_body.Group) == before_tool_group and tool_body.Tip is before_tool_tip
assert all(bool(item.Visibility) is visible for item, visible in before_visibility)
assert not rollback_document.HasPendingTransaction
FreeCAD.closeDocument(rollback_document.Name)
print('REAL_PARTDESIGN_BOOLEAN_BATCH_OK')
"""
    completed = subprocess.run(
        [str(runtime_python), "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    assert "REAL_PARTDESIGN_BOOLEAN_BATCH_OK" in completed.stdout
