"""Focused gates for the three-spec reviewed Part offset/projection family."""

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
from vibecad.intent_bridge.freecad_part_offset_projection_adapter import (
    FREECAD_PART_OFFSET_ADAPTER_DESCRIPTOR,
    PART_OFFSET_CANONICAL_JSON_TERM,
    PART_OFFSET_CONFIGURATION_ROLE_TERM,
    PART_OFFSET_CONFIGURATION_TYPE_TERM,
    PART_OFFSET_INTENT_DOCUMENT_ROLE_TERM,
    PART_OFFSET_OPERATION_SPECS,
    PART_OFFSET_OPERATION_TERMS,
    PART_OFFSET_PFG_TERMS,
    PART_OFFSET_REQUEST_TERMS,
    PART_OFFSET_SOURCE_FAMILY_TERM,
    PART_OFFSET_SOURCE_OPERATION_TERM,
    PART_OFFSET_SOURCE_STRUCTURE_TERM,
    PART_OFFSET_SOURCE_TERMS,
    PART_OFFSET_STRUCTURE_TERM,
    FreeCADPartOffsetProjectionAdapter,
    build_part_offset_capability_document,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    ParametricFeatureGraphV2Codec,
)
from vibecad.intent_bridge.ports import TrustedCodecRegistry
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
    TermTypedValueV2,
)
from vibecad.parametric.freecad_part_offset_projection_rules import (
    MAX_OFFSET_MM,
    MAX_PART_OFFSET_PLAN_BYTES,
    MIN_OFFSET_MM,
    PART_OFFSET_EXCLUDED_CANDIDATES,
    PART_OFFSET_NATIVE_TYPE_IDS,
    PART_OFFSET_SOURCE_ROLES,
    PartOffsetOperation,
    PartOffsetRuleError,
    PartOffsetSourceRole,
    decode_part_offset_backend_plan,
    encode_part_offset_configuration,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


CONFIGURATIONS = {
    PartOffsetOperation.SOLID_OFFSET: {"distance_mm": 2.0},
    PartOffsetOperation.PLANAR_WIRE_OFFSET: {"distance_mm": 2.0},
    PartOffsetOperation.EDGE_ON_FACE_PROJECTION: {},
}


def _operation_terms(operation: PartOffsetOperation):
    return next(item for item in PART_OFFSET_OPERATION_TERMS if item.operation is operation)


def _source_terms(role: PartOffsetSourceRole):
    return next(item for item in PART_OFFSET_SOURCE_TERMS if item.role is role)


def _graph(
    operation: PartOffsetOperation,
    *,
    configuration: object | None = None,
    operation_definition: str | None = None,
) -> ParametricFeatureGraphV2:
    selected = _operation_terms(operation)
    terms = list(PART_OFFSET_PFG_TERMS)
    if operation_definition is not None:
        index = terms.index(selected.operation_term)
        terms[index] = dataclasses.replace(
            selected.operation_term,
            term_definition_sha256=operation_definition,
        )
    parameter = DesignParameterV2(
        parameter_id="parameter_configuration",
        name="Bounded offset/projection configuration",
        semantic_role_term_ref_id=PART_OFFSET_CONFIGURATION_ROLE_TERM.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id="value_configuration",
            value_type_term_ref_id=PART_OFFSET_CONFIGURATION_TYPE_TERM.term_ref_id,
            encoding_term_ref_id=PART_OFFSET_CANONICAL_JSON_TERM.term_ref_id,
            value=(CONFIGURATIONS[operation] if configuration is None else configuration),
        ),
    )
    ports = [
        FeatureInputPortV2(
            port_id="port_configuration",
            semantic_role_term_ref_id=PART_OFFSET_CONFIGURATION_ROLE_TERM.term_ref_id,
            value_type_term_ref_id=PART_OFFSET_CONFIGURATION_TYPE_TERM.term_ref_id,
            minimum_cardinality=1,
            maximum_cardinality=1,
            ordered=False,
        )
    ]
    dependencies = []
    source_nodes = []
    for index, role in enumerate(PART_OFFSET_SOURCE_ROLES[operation]):
        source_terms = _source_terms(role)
        port_id = f"port_source_{index}"
        node_id = f"node_source_{index}"
        result_id = f"result_source_{index}"
        ports.append(
            FeatureInputPortV2(
                port_id=port_id,
                semantic_role_term_ref_id=source_terms.input_role.term_ref_id,
                value_type_term_ref_id=source_terms.value_type.term_ref_id,
                minimum_cardinality=1,
                maximum_cardinality=1,
                ordered=False,
            )
        )
        dependencies.append(
            FeatureDependencyV2(
                dependency_id=f"dependency_source_{index}",
                port_id=port_id,
                upstream_node_id=node_id,
                upstream_result_id=result_id,
            )
        )
        source_nodes.append(
            FeatureNodeV2(
                node_id=node_id,
                body_id="part_space",
                name=f"Authenticated source {role.value}",
                intent=FeatureIntentV2(
                    structural_kind_term_ref_id=PART_OFFSET_SOURCE_STRUCTURE_TERM.term_ref_id,
                    family_term_ref_id=PART_OFFSET_SOURCE_FAMILY_TERM.term_ref_id,
                    operation_term_ref_id=PART_OFFSET_SOURCE_OPERATION_TERM.term_ref_id,
                ),
                results=(
                    FeatureResultV2(
                        result_id=result_id,
                        semantic_role_term_ref_id=source_terms.result_role.term_ref_id,
                        value_type_term_ref_id=source_terms.value_type.term_ref_id,
                    ),
                ),
            )
        )
    target = FeatureNodeV2(
        node_id="node_target",
        body_id="part_space",
        name="Backend-neutral offset or projection",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=PART_OFFSET_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=selected.family_term.term_ref_id,
            operation_term_ref_id=selected.operation_term.term_ref_id,
            input_ports=tuple(ports),
            dependencies=tuple(dependencies),
            parameter_bindings=(
                FeatureParameterBindingV2(
                    binding_id="binding_configuration",
                    port_id="port_configuration",
                    parameter_id=parameter.parameter_id,
                ),
            ),
        ),
        results=(
            FeatureResultV2(
                result_id="result_target",
                semantic_role_term_ref_id=selected.result_role.term_ref_id,
                value_type_term_ref_id=selected.result_type.term_ref_id,
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id=f"graph_part_{operation.value}",
        name="Part offset/projection graph",
        terms=tuple(terms),
        bodies=(FeatureBodyV2(body_id="part_space", name="Part space"),),
        parameters=(parameter,),
        references=(),
        nodes=tuple((*source_nodes, target)),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection_result",
                node_id=target.node_id,
                result_id="result_target",
            ),
        ),
    )


def _bridge_from_pfg(term) -> BridgeTermRef:
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
        namespace="org.vibecad.part-offset-proof-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"proof:{term_id}"),
    )


RULE = _proof_term("rule_part_offset_target", "rule.part-offset-target-reviewed")
PREDICATE = _proof_term("predicate_part_offset_target", "predicate.part-offset-target-reviewed")
ROLE_PREMISE = _proof_term("role_part_offset_candidate", "proof-role.part-offset-candidate")
ROLE_CONCLUSION = _proof_term("role_part_offset_validated", "proof-role.part-offset-validated")
PART_OFFSET_STRUCTURE_BRIDGE = _bridge_from_pfg(PART_OFFSET_STRUCTURE_TERM)


def _subject() -> SubjectRef:
    return SubjectRef(
        artifact_id="artifact_pfg",
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id="node_target",
    )


class _PartOffsetEvaluator:
    def __init__(self) -> None:
        def signature(role: BridgeTermRef) -> RuleEndpointSignature:
            return RuleEndpointSignature(
                selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
                role_term=role,
                subject_type_term=PART_OFFSET_STRUCTURE_BRIDGE,
            )

        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="part_offset_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("part-offset-target-evaluator-v1"),
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
            or evaluation.premises[0].subject != _subject()
            or evaluation.conclusions[0].subject != _subject()
        ):
            raise IntentBridgeError(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/target")


def _intent_document(graph: ParametricFeatureGraphV2) -> tuple[DocumentRef, bytes]:
    payload = graph.canonical_bytes
    return (
        DocumentRef(
            artifact_id="artifact_pfg",
            role_term_ref_id=PART_OFFSET_INTENT_DOCUMENT_ROLE_TERM.term_ref_id,
            schema_term_ref_id=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM.term_ref_id,
            document_id=graph.graph_id,
            document_digest=graph.graph_sha256,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
        ),
        payload,
    )


def _proof(policy: TrustedRulePolicy, document: DocumentRef) -> ProofBundle:
    return ProofBundle(
        terms=(
            RULE,
            PREDICATE,
            ROLE_PREMISE,
            ROLE_CONCLUSION,
            PART_OFFSET_STRUCTURE_BRIDGE,
            PART_OFFSET_INTENT_DOCUMENT_ROLE_TERM,
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_part_offset_target",
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
                producer_id="part_offset_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("part-offset-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("upstream-part-offset-compile-request"),
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
    max_output_bytes: int = MAX_PART_OFFSET_PLAN_BYTES,
) -> tuple[BackendLoweringRequest, _Reader, TrustedRulePolicy]:
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = build_part_offset_capability_document()
    policy = TrustedRulePolicy(evaluators=(_PartOffsetEvaluator(),))
    request = BackendLoweringRequest(
        adapter=FREECAD_PART_OFFSET_ADAPTER_DESCRIPTOR,
        terms=tuple((*PART_OFFSET_REQUEST_TERMS, RULE, PREDICATE, ROLE_PREMISE, ROLE_CONCLUSION)),
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
    adapter: FreeCADPartOffsetProjectionAdapter,
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


@pytest.mark.parametrize("operation", tuple(PartOffsetOperation))
def test_shared_adapter_lowers_exact_three_specs_deterministically(
    operation: PartOffsetOperation,
) -> None:
    request, reader, policy = _request(_graph(operation))
    sink = _MemoryPlanSink()
    adapter = FreeCADPartOffsetProjectionAdapter(sink)
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    repeated_result, repeated_receipt = _lower(adapter, request, reader, policy)
    repeated_plan, repeated_payload = adapter.read_plan(repeated_receipt)

    assert plan.operation is operation
    assert plan.configuration == CONFIGURATIONS[operation]
    assert tuple(item.role for item in plan.sources) == PART_OFFSET_SOURCE_ROLES[operation]
    assert result.plan_document.document_digest == plan.plan_sha256
    assert result.plan_document.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert repeated_result == result and repeated_receipt == receipt
    assert repeated_plan == plan and repeated_payload == payload
    assert result.supported_subjects == (_subject(),)
    assert len(sink.items) == 1
    assert not adapter.executable and not plan.executable
    assert not adapter.grants_execution_authority and not receipt.grants_execution_authority
    assert b"Part::" not in payload
    assert b"Face1" not in payload and b"Edge1" not in payload


def test_unknown_identity_and_sink_failure_publish_nothing() -> None:
    graph = _graph(
        PartOffsetOperation.SOLID_OFFSET,
        operation_definition=_sha("substituted operation definition"),
    )
    request, reader, policy = _request(graph)
    sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartOffsetProjectionAdapter(sink), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    graph = _graph(PartOffsetOperation.SOLID_OFFSET)
    unused = _operation_terms(PartOffsetOperation.PLANAR_WIRE_OFFSET).operation_term
    rebound_terms = tuple(
        dataclasses.replace(item, term_ref_id="rebound_unused_operation")
        if item == unused
        else item
        for item in graph.terms
    )
    request, reader, policy = _request(dataclasses.replace(graph, terms=rebound_terms))
    sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartOffsetProjectionAdapter(sink), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    request, reader, policy = _request(_graph(PartOffsetOperation.EDGE_ON_FACE_PROJECTION))
    sink = _MemoryPlanSink(fail=True)
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartOffsetProjectionAdapter(sink), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert sink.items == {}


@pytest.mark.parametrize("distance", (MIN_OFFSET_MM, -MAX_OFFSET_MM))
def test_distance_n_n_plus_one_tamper_and_output_budget(distance: float) -> None:
    request, reader, policy = _request(
        _graph(
            PartOffsetOperation.SOLID_OFFSET,
            configuration={"distance_mm": distance},
        )
    )
    adapter = FreeCADPartOffsetProjectionAdapter(_MemoryPlanSink())
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    assert plan.configuration["distance_mm"] == distance

    for rejected in (MIN_OFFSET_MM / 2, MAX_OFFSET_MM + 1):
        with pytest.raises(PartOffsetRuleError):
            encode_part_offset_configuration(
                PartOffsetOperation.SOLID_OFFSET, {"distance_mm": rejected}
            )
    with pytest.raises(PartOffsetRuleError):
        decode_part_offset_backend_plan(
            payload + b" ",
            expected_content_sha256=result.plan_document.content_sha256,
            expected_plan_sha256=plan.plan_sha256,
        )
    duplicate = payload.replace(b'{"authority":', b'{"authority":"none","authority":', 1)
    with pytest.raises(PartOffsetRuleError):
        decode_part_offset_backend_plan(duplicate)

    request, reader, policy = _request(
        _graph(PartOffsetOperation.PLANAR_WIRE_OFFSET), max_output_bytes=1
    )
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartOffsetProjectionAdapter(_MemoryPlanSink()), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED


def test_inventory_counts_three_semantics_and_excludes_shape_only_spline() -> None:
    assert tuple(item.operation_id for item in PART_OFFSET_OPERATION_SPECS) == tuple(
        item.value for item in PartOffsetOperation
    )
    assert tuple(item.native_type_id for item in PART_OFFSET_OPERATION_SPECS) == tuple(
        PART_OFFSET_NATIVE_TYPE_IDS[item] for item in PartOffsetOperation
    )
    assert PART_OFFSET_EXCLUDED_CANDIDATES == {
        "Part::Spline": ("shape-only-storage-without-native-control-point-or-recompute-properties")
    }


@pytest.mark.slow
def test_real_freecad_offset_projection_batch_create_edit_reopen_and_rollback(
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
    for index, operation in enumerate(PartOffsetOperation):
        request, reader, policy = _request(_graph(operation))
        adapter = FreeCADPartOffsetProjectionAdapter(_MemoryPlanSink())
        result, receipt = _lower(adapter, request, reader, policy)
        plan, payload = adapter.read_plan(receipt)
        path = tmp_path / f"{index}_{operation.value}.json"
        path.write_bytes(payload)
        cases.append(
            {
                "operation": operation.value,
                "path": str(path),
                "content_sha256": result.plan_document.content_sha256,
                "plan_sha256": result.plan_document.document_digest,
                "container_id": plan.container_id,
                "sources": [item.to_mapping() for item in plan.sources],
            }
        )
    source_root = Path(__file__).parents[1] / "src"
    output_root = tmp_path / "freecad-part-offset-projection"
    output_root.mkdir()
    code = f"""
import os, sys
sys.path.insert(0, {str(source_root)!r})
from vibecad.freecad_env import prepare_freecad_import
prepare_freecad_import()
from pathlib import Path
import FreeCAD, Part
from vibecad.parametric.freecad_part_offset_projection_rules import (
    PartOffsetExecutionBindings,
    PartOffsetRuleError,
    PartOffsetSourceBinding,
    PartOffsetSourceRole,
    apply_part_offset_plan,
)

CASES = {cases!r}
OUTPUT_ROOT = Path({str(output_root)!r})
TYPE_IDS = {dict((key.value, value) for key, value in PART_OFFSET_NATIVE_TYPE_IDS.items())!r}

def snapshot(document):
    objects = tuple(document.Objects)
    groups = tuple(
        (item, tuple(item.Group)) for item in objects if 'Group' in tuple(item.PropertiesList))
    return objects, groups, bool(document.HasPendingTransaction)

def same_identity(left, right):
    return len(tuple(left)) == len(right) and all(
        a is b for a, b in zip(tuple(left), right, strict=True))

def same_snapshot(document, before):
    objects, groups, pending = before
    return (
        same_identity(document.Objects, objects)
        and all(same_identity(item.Group, members) for item, members in groups)
        and bool(document.HasPendingTransaction) is pending
    )

def make_sources(document, operation):
    if operation == 'solid_offset':
        source = document.addObject('Part::Box', 'SolidSource')
        source.Length = 20; source.Width = 10; source.Height = 5
        return {{'solid_source': source}}
    if operation == 'planar_wire_offset':
        source = document.addObject('Part::Feature', 'WireSource')
        source.Shape = Part.makePolygon([
            FreeCAD.Vector(0,0,0), FreeCAD.Vector(20,0,0),
            FreeCAD.Vector(20,10,0), FreeCAD.Vector(0,10,0),
            FreeCAD.Vector(0,0,0)])
        return {{'planar_wire_source': source}}
    support = document.addObject('Part::Feature', 'SupportSource')
    support.Shape = Part.makePlane(20,20)
    edge = document.addObject('Part::Feature', 'ProjectionSource')
    edge.Shape = Part.makeLine(FreeCAD.Vector(2,2,10), FreeCAD.Vector(15,2,10))
    return {{'support_face': support, 'projection_edge': edge}}

def bindings(document, entry, objects):
    return PartOffsetExecutionBindings(
        document=document,
        container_id=entry['container_id'],
        sources=tuple(
            PartOffsetSourceBinding(
                role=PartOffsetSourceRole(item['role']),
                node_id=item['node_id'],
                result_id=item['result_id'],
                native_object=objects[item['role']],
            )
            for item in entry['sources']
        ),
    )

persisted = []
for index, entry in enumerate(CASES):
    document = FreeCAD.newDocument('PartOffset' + str(index))
    document.UndoMode = 1
    objects = make_sources(document, entry['operation'])
    document.recompute()
    execution_bindings = bindings(document, entry, objects)
    payload = Path(entry['path']).read_bytes()
    before = snapshot(document)
    try:
        apply_part_offset_plan(
            payload + b' ',
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=execution_bindings,
        )
        raise AssertionError('tamper accepted')
    except PartOffsetRuleError:
        assert same_snapshot(document, before)
    receipt = apply_part_offset_plan(
        payload,
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=execution_bindings,
    )
    feature = document.getObject(receipt.object_name)
    assert feature.TypeId == TYPE_IDS[entry['operation']] and feature.isValid()
    before_x = float(feature.Shape.BoundBox.XLength)
    if entry['operation'] == 'solid_offset':
        feature.Value = 1
        objects['solid_source'].Length = 30
    elif entry['operation'] == 'planar_wire_offset':
        feature.Value = 1
        objects['planar_wire_source'].Shape = Part.makePolygon([
            FreeCAD.Vector(0,0,0), FreeCAD.Vector(30,0,0),
            FreeCAD.Vector(30,10,0), FreeCAD.Vector(0,10,0),
            FreeCAD.Vector(0,0,0)])
    else:
        objects['projection_edge'].Shape = Part.makeLine(
            FreeCAD.Vector(2,2,10), FreeCAD.Vector(18,2,10))
    document.recompute()
    after_x = float(feature.Shape.BoundBox.XLength)
    assert after_x > before_x
    path = OUTPUT_ROOT / f'{{index}}.FCStd'
    document.saveAs(str(path))
    persisted.append((path, receipt.object_name, entry['operation'], after_x))
    FreeCAD.closeDocument(document.Name)

for path, object_name, operation, expected_x in persisted:
    reopened = FreeCAD.openDocument(str(path))
    feature = reopened.getObject(object_name)
    assert feature is not None and feature.TypeId == TYPE_IDS[operation] and feature.isValid()
    assert abs(float(feature.Shape.BoundBox.XLength) - expected_x) < 1e-9
    if operation == 'solid_offset':
        assert feature.Source is reopened.getObject('SolidSource')
        assert feature.Mode == 'Skin' and feature.Join == 'Arc'
    elif operation == 'planar_wire_offset':
        assert feature.Source is reopened.getObject('WireSource')
        assert feature.Mode == 'Pipe' and feature.Join == 'Arc'
    else:
        support, support_names = feature.SupportFace
        projection = tuple(feature.Projection)
        assert support is reopened.getObject('SupportSource')
        assert tuple(support_names) == ('Face1',)
        assert projection[0][0] is reopened.getObject('ProjectionSource')
        assert tuple(projection[0][1]) == ('Edge1',)
        assert tuple(float(item) for item in feature.Direction) == (0.0, 0.0, -1.0)
        assert feature.Mode == 'Edges'
    FreeCAD.closeDocument(reopened.Name)

# Invalid topology is rejected before mutation.
entry = next(item for item in CASES if item['operation'] == 'planar_wire_offset')
document = FreeCAD.newDocument('PartOffsetInvalidSource')
source = document.addObject('Part::Feature', 'OpenWire')
source.Shape = Part.makePolygon([
    FreeCAD.Vector(0,0,0), FreeCAD.Vector(20,0,0), FreeCAD.Vector(20,10,0)])
document.recompute()
before = snapshot(document)
try:
    apply_part_offset_plan(
        Path(entry['path']).read_bytes(),
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=bindings(document, entry, {{'planar_wire_source': source}}),
    )
    raise AssertionError('open wire accepted')
except PartOffsetRuleError:
    assert same_snapshot(document, before)
FreeCAD.closeDocument(document.Name)

# Foreign-document sources are rejected before mutation.
entry = next(item for item in CASES if item['operation'] == 'solid_offset')
document = FreeCAD.newDocument('PartOffsetForeignReject')
foreign = FreeCAD.newDocument('PartOffsetForeign')
source = foreign.addObject('Part::Box', 'ForeignSolid')
foreign.recompute()
before = snapshot(document)
try:
    apply_part_offset_plan(
        Path(entry['path']).read_bytes(),
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=bindings(document, entry, {{'solid_source': source}}),
    )
    raise AssertionError('foreign source accepted')
except PartOffsetRuleError:
    assert same_snapshot(document, before)
FreeCAD.closeDocument(foreign.Name)
FreeCAD.closeDocument(document.Name)

# A document observer violates root ownership only after native creation.
class LateOwnershipObserver:
    def __init__(self, group):
        self.group = group
    def slotCreatedObject(self, item):
        if item.TypeId == 'Part::Offset':
            self.group.addObject(item)

entry = next(item for item in CASES if item['operation'] == 'solid_offset')
document = FreeCAD.newDocument('PartOffsetLateRollback')
document.UndoMode = 1
objects = make_sources(document, entry['operation'])
group = document.addObject('App::DocumentObjectGroup', 'GuardGroup')
document.recompute()
observer = LateOwnershipObserver(group)
FreeCAD.addDocumentObserver(observer)
before = snapshot(document)
try:
    try:
        apply_part_offset_plan(
            Path(entry['path']).read_bytes(),
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings(document, entry, objects),
        )
        raise AssertionError('late ownership violation accepted')
    except PartOffsetRuleError:
        assert same_snapshot(document, before)
finally:
    FreeCAD.removeDocumentObserver(observer)
FreeCAD.closeDocument(document.Name)
print('PART_OFFSET_PROJECTION_BATCH_OK')
"""
    completed = subprocess.run(
        [str(runtime_python), "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "PART_OFFSET_PROJECTION_BATCH_OK" in completed.stdout
