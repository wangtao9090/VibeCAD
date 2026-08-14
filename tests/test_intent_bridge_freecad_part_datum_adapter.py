"""Focused gates for the four-spec reviewed Part datum family."""

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
from vibecad.intent_bridge.freecad_part_datum_adapter import (
    FREECAD_PART_DATUM_ADAPTER_DESCRIPTOR,
    PART_DATUM_CANONICAL_JSON_TERM,
    PART_DATUM_FAMILY_TERM,
    PART_DATUM_INTENT_DOCUMENT_ROLE_TERM,
    PART_DATUM_OPERATION_SPECS,
    PART_DATUM_OPERATION_TERMS,
    PART_DATUM_PFG_TERMS,
    PART_DATUM_PLACEMENT_ROLE_TERM,
    PART_DATUM_PLACEMENT_TYPE_TERM,
    PART_DATUM_REQUEST_TERMS,
    PART_DATUM_STRUCTURE_TERM,
    FreeCADPartDatumAdapter,
    build_part_datum_capability_document,
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
    FeatureGraphResultV2,
    FeatureInputPortV2,
    FeatureIntentV2,
    FeatureNodeV2,
    FeatureParameterBindingV2,
    FeatureResultV2,
    ParametricFeatureGraphV2,
    TermTypedValueV2,
)
from vibecad.parametric.freecad_part_datum_rules import (
    MAX_PART_DATUM_PLAN_BYTES,
    PartDatumOperation,
    PartDatumRuleError,
    decode_part_datum_backend_plan,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _graph(
    operation: PartDatumOperation,
    *,
    x_mm: float = 10.0,
    operation_definition: str | None = None,
) -> ParametricFeatureGraphV2:
    operation_terms = next(
        item for item in PART_DATUM_OPERATION_TERMS if item.operation is operation
    )
    terms = list(PART_DATUM_PFG_TERMS)
    if operation_definition is not None:
        index = terms.index(operation_terms.operation_term)
        terms[index] = dataclasses.replace(
            operation_terms.operation_term,
            term_definition_sha256=operation_definition,
        )
    parameter = DesignParameterV2(
        parameter_id="parameter_placement",
        name="Explicit placement",
        semantic_role_term_ref_id=PART_DATUM_PLACEMENT_ROLE_TERM.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id="value_placement",
            value_type_term_ref_id=PART_DATUM_PLACEMENT_TYPE_TERM.term_ref_id,
            encoding_term_ref_id=PART_DATUM_CANONICAL_JSON_TERM.term_ref_id,
            value={
                "position_mm": [x_mm, 20.0, 30.0],
                "axis": [0.0, 0.0, 1.0],
                "angle_degrees": 45.0,
            },
        ),
    )
    target = FeatureNodeV2(
        node_id="node_target",
        body_id="reference_space",
        name="Backend-neutral document reference",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=PART_DATUM_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=PART_DATUM_FAMILY_TERM.term_ref_id,
            operation_term_ref_id=operation_terms.operation_term.term_ref_id,
            input_ports=(
                FeatureInputPortV2(
                    port_id="port_placement",
                    semantic_role_term_ref_id=PART_DATUM_PLACEMENT_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=PART_DATUM_PLACEMENT_TYPE_TERM.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
            ),
            parameter_bindings=(
                FeatureParameterBindingV2(
                    binding_id="binding_placement",
                    port_id="port_placement",
                    parameter_id=parameter.parameter_id,
                ),
            ),
        ),
        results=(
            FeatureResultV2(
                result_id="result_target",
                semantic_role_term_ref_id=operation_terms.result_role.term_ref_id,
                value_type_term_ref_id=operation_terms.result_type.term_ref_id,
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id="graph_part_datum",
        name="Part datum graph",
        terms=tuple(terms),
        bodies=(FeatureBodyV2(body_id="reference_space", name="Reference space"),),
        parameters=(parameter,),
        references=(),
        nodes=(target,),
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
        namespace="org.vibecad.part-datum-proof-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"proof:{term_id}"),
    )


RULE = _proof_term("rule_part_datum_target", "rule.part-datum-target-reviewed")
PREDICATE = _proof_term(
    "predicate_part_datum_target", "predicate.part-datum-target-reviewed"
)
ROLE_PREMISE = _proof_term("role_part_datum_candidate", "proof-role.datum-candidate")
ROLE_CONCLUSION = _proof_term(
    "role_part_datum_validated", "proof-role.datum-validated"
)
PART_DATUM_STRUCTURE_BRIDGE = _bridge_from_pfg(PART_DATUM_STRUCTURE_TERM)


class _DatumEvaluator:
    def __init__(self) -> None:
        def signature(role: BridgeTermRef) -> RuleEndpointSignature:
            return RuleEndpointSignature(
                selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
                role_term=role,
                subject_type_term=PART_DATUM_STRUCTURE_BRIDGE,
            )

        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="part_datum_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("part-datum-target-evaluator-v1"),
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
            role_term_ref_id=PART_DATUM_INTENT_DOCUMENT_ROLE_TERM.term_ref_id,
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
            PART_DATUM_STRUCTURE_BRIDGE,
            PART_DATUM_INTENT_DOCUMENT_ROLE_TERM,
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_part_datum_target",
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
                producer_id="part_datum_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("part-datum-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("upstream-part-datum-compile-request"),
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
    max_output_bytes: int = MAX_PART_DATUM_PLAN_BYTES,
) -> tuple[BackendLoweringRequest, _Reader, TrustedRulePolicy]:
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = build_part_datum_capability_document()
    policy = TrustedRulePolicy(evaluators=(_DatumEvaluator(),))
    request = BackendLoweringRequest(
        adapter=FREECAD_PART_DATUM_ADAPTER_DESCRIPTOR,
        terms=tuple(
            (*PART_DATUM_REQUEST_TERMS, RULE, PREDICATE, ROLE_PREMISE, ROLE_CONCLUSION)
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
    adapter: FreeCADPartDatumAdapter,
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


@pytest.mark.parametrize("operation", tuple(PartDatumOperation))
def test_shared_adapter_lowers_exact_four_specs_deterministically(
    operation: PartDatumOperation,
) -> None:
    request, reader, policy = _request(_graph(operation))
    sink = _MemoryPlanSink()
    adapter = FreeCADPartDatumAdapter(sink)
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    repeated_result, repeated_receipt = _lower(adapter, request, reader, policy)
    repeated_plan, repeated_payload = adapter.read_plan(repeated_receipt)

    assert plan.operation is operation
    assert plan.placement.position_mm == (10.0, 20.0, 30.0)
    assert result.plan_document.document_digest == plan.plan_sha256
    assert result.plan_document.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert repeated_result == result and repeated_receipt == receipt
    assert repeated_plan == plan and repeated_payload == payload
    assert result.supported_subjects == (_subject(),)
    assert len(sink.items) == 1
    assert not adapter.executable and not plan.executable
    assert not adapter.grants_execution_authority and not receipt.grants_execution_authority
    assert b"Part::" not in payload


def test_unknown_semantic_identity_and_sink_failure_are_inert() -> None:
    graph = _graph(
        PartDatumOperation.DATUM_LINE,
        operation_definition=_sha("substituted operation definition"),
    )
    request, reader, policy = _request(graph)
    sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartDatumAdapter(sink), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    request, reader, policy = _request(_graph(PartDatumOperation.DATUM_POINT))
    sink = _MemoryPlanSink(fail=True)
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartDatumAdapter(sink), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert sink.items == {}


def test_placement_n_n_plus_one_plan_tamper_and_output_budget() -> None:
    request, reader, policy = _request(
        _graph(PartDatumOperation.DATUM_PLANE, x_mm=1_000_000.0)
    )
    adapter = FreeCADPartDatumAdapter(_MemoryPlanSink())
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    assert plan.placement.position_mm[0] == 1_000_000.0

    request, reader, policy = _request(
        _graph(PartDatumOperation.DATUM_PLANE, x_mm=1_000_000.1)
    )
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartDatumAdapter(_MemoryPlanSink()), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION

    with pytest.raises(PartDatumRuleError):
        decode_part_datum_backend_plan(
            payload + b" ",
            expected_content_sha256=result.plan_document.content_sha256,
            expected_plan_sha256=plan.plan_sha256,
        )
    duplicate = payload.replace(b'{"authority":', b'{"authority":"none","authority":', 1)
    with pytest.raises(PartDatumRuleError):
        decode_part_datum_backend_plan(duplicate)

    request, reader, policy = _request(
        _graph(PartDatumOperation.DATUM_PLANE), max_output_bytes=1
    )
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartDatumAdapter(_MemoryPlanSink()), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED


def test_manifest_counts_only_four_user_semantics_not_lcs_helpers() -> None:
    assert tuple(item.operation_id for item in PART_DATUM_OPERATION_SPECS) == tuple(
        item.value for item in PartDatumOperation
    )
    assert tuple(item.native_type_id for item in PART_DATUM_OPERATION_SPECS) == (
        "Part::DatumLine",
        "Part::DatumPlane",
        "Part::DatumPoint",
        "Part::LocalCoordinateSystem",
    )
    assert not any(
        item.native_type_id in {"App::Line", "App::Plane", "App::Point"}
        for item in PART_DATUM_OPERATION_SPECS
    )


@pytest.mark.slow
def test_real_freecad_part_datum_batch_create_propagate_reopen_and_rollback(
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
    for index, operation in enumerate(PartDatumOperation):
        request, reader, policy = _request(_graph(operation))
        adapter = FreeCADPartDatumAdapter(_MemoryPlanSink())
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
            }
        )
    source_root = Path(__file__).parents[1] / "src"
    output_root = tmp_path / "freecad-part-datums"
    output_root.mkdir()
    code = f"""
import os, sys
sys.path.insert(0, os.path.join(sys.prefix, 'lib'))
sys.path.insert(0, {str(source_root)!r})
from pathlib import Path
import FreeCAD
from vibecad.parametric.freecad_part_datum_rules import (
    PartDatumExecutionBindings,
    PartDatumRuleError,
    apply_part_datum_plan,
)

CASES = {cases!r}
OUTPUT_ROOT = Path({str(output_root)!r})
TYPE_IDS = {{
    'datum_line': 'Part::DatumLine',
    'datum_plane': 'Part::DatumPlane',
    'datum_point': 'Part::DatumPoint',
    'local_coordinate_system': 'Part::LocalCoordinateSystem',
}}

def snapshot(document):
    objects = tuple(document.Objects)
    groups = tuple(
        (item, tuple(item.Group)) for item in objects if 'Group' in tuple(item.PropertiesList))
    visibility = tuple(
        (item, bool(item.Visibility))
        for item in objects if 'Visibility' in tuple(item.PropertiesList))
    return objects, groups, visibility, bool(document.HasPendingTransaction)

def same_snapshot(document, before):
    objects, groups, visibility, pending = before
    return (
        tuple(document.Objects) == objects
        and all(tuple(item.Group) == members for item, members in groups)
        and all(bool(item.Visibility) is value for item, value in visibility)
        and bool(document.HasPendingTransaction) is pending
    )

persisted = []
for index, entry in enumerate(CASES):
    document = FreeCAD.newDocument('PartDatum' + str(index))
    document.UndoMode = 1
    bindings = PartDatumExecutionBindings(
        document=document, container_id=entry['container_id'])
    payload = Path(entry['path']).read_bytes()
    before = snapshot(document)
    try:
        apply_part_datum_plan(
            payload + b' ',
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings,
        )
        raise AssertionError('tamper accepted')
    except PartDatumRuleError:
        assert same_snapshot(document, before)
    receipt = apply_part_datum_plan(
        payload,
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=bindings,
    )
    feature = document.getObject(receipt.object_name)
    assert feature.TypeId == TYPE_IDS[entry['operation']] and feature.isValid()
    expected_owned = 8 if entry['operation'] == 'local_coordinate_system' else 1
    assert len(receipt.owned_object_names) == expected_owned
    if entry['operation'] == 'local_coordinate_system':
        assert [item.Role for item in feature.OriginFeatures] == [
            'X_Axis', 'Y_Axis', 'Z_Axis',
            'XY_Plane', 'XZ_Plane', 'YZ_Plane', 'Origin']
    consumer = document.addObject('Part::Feature', 'Consumer')
    consumer.setExpression('Placement.Base.x', feature.Name + '.Placement.Base.x')
    feature.Placement = FreeCAD.Placement(
        FreeCAD.Vector(40 + index, 50 + index, 60 + index),
        FreeCAD.Rotation(FreeCAD.Vector(1, 0, 0), 30 + index))
    document.recompute()
    assert abs(float(consumer.Placement.Base.x) - (40 + index)) < 1e-9
    path = OUTPUT_ROOT / f'{{index}}.FCStd'
    document.saveAs(str(path))
    persisted.append((path, receipt.object_name, entry['operation'], 40 + index))
    FreeCAD.closeDocument(document.Name)

for path, object_name, operation, expected_x in persisted:
    reopened = FreeCAD.openDocument(str(path))
    feature = reopened.getObject(object_name)
    consumer = reopened.getObject('Consumer')
    assert feature is not None and feature.TypeId == TYPE_IDS[operation] and feature.isValid()
    assert abs(float(feature.Placement.Base.x) - expected_x) < 1e-9
    assert abs(float(consumer.Placement.Base.x) - expected_x) < 1e-9
    if operation == 'local_coordinate_system':
        assert len(tuple(feature.OriginFeatures)) == 7
        assert all(item.isValid() for item in feature.OriginFeatures)
    FreeCAD.closeDocument(reopened.Name)

# A real document observer moves the new line into a group after creation.
# This violates the reviewed root-ownership postcondition only after mutation;
# abortTransaction must restore both document objects and group membership.
class LateOwnershipObserver:
    def __init__(self, group):
        self.group = group
    def slotCreatedObject(self, item):
        if item.TypeId == 'Part::DatumLine':
            self.group.addObject(item)

entry = CASES[0]
document = FreeCAD.newDocument('PartDatumLateRollback')
document.UndoMode = 1
group = document.addObject('App::DocumentObjectGroup', 'GuardGroup')
observer = LateOwnershipObserver(group)
FreeCAD.addDocumentObserver(observer)
before = snapshot(document)
try:
    try:
        apply_part_datum_plan(
            Path(entry['path']).read_bytes(),
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=PartDatumExecutionBindings(
                document=document, container_id=entry['container_id']),
        )
        raise AssertionError('late ownership violation accepted')
    except PartDatumRuleError:
        assert same_snapshot(document, before)
finally:
    FreeCAD.removeDocumentObserver(observer)
FreeCAD.closeDocument(document.Name)
print('PART_DATUM_BATCH_OK')
"""
    completed = subprocess.run(
        [str(runtime_python), "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "PART_DATUM_BATCH_OK" in completed.stdout
