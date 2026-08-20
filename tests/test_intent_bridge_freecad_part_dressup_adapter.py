"""Focused gates for the three-spec reviewed Part dress-up family."""

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
from vibecad.intent_bridge.freecad_part_dressup_adapter import (
    FREECAD_PART_DRESSUP_ADAPTER_DESCRIPTOR,
    PART_DRESSUP_CHAMFER_OPERATION_TERM,
    PART_DRESSUP_EDGE_LOCATOR_TERM,
    PART_DRESSUP_EDGE_REFERENCE_TYPE_TERM,
    PART_DRESSUP_FACE_LOCATOR_TERM,
    PART_DRESSUP_FACE_REFERENCE_TYPE_TERM,
    PART_DRESSUP_FAMILY_TERM,
    PART_DRESSUP_FILLET_OPERATION_TERM,
    PART_DRESSUP_INTENT_DOCUMENT_ROLE_TERM,
    PART_DRESSUP_LENGTH_TYPE_TERM,
    PART_DRESSUP_MAGNITUDE_ROLE_TERM,
    PART_DRESSUP_OPERATION_SPECS,
    PART_DRESSUP_OPERATION_TERMS,
    PART_DRESSUP_PFG_TERMS,
    PART_DRESSUP_REFERENCE_FAMILY_TERM,
    PART_DRESSUP_REFERENCE_OPERATION_TERM,
    PART_DRESSUP_REFERENCE_ROLE_TERM,
    PART_DRESSUP_REFERENCE_STRUCTURE_TERM,
    PART_DRESSUP_REQUEST_TERMS,
    PART_DRESSUP_RESULT_SOLID_ROLE_TERM,
    PART_DRESSUP_SCALAR_JSON_TERM,
    PART_DRESSUP_SELECTION_PORT_ROLE_TERM,
    PART_DRESSUP_SOLID_TYPE_TERM,
    PART_DRESSUP_SOURCE_PORT_ROLE_TERM,
    PART_DRESSUP_SOURCE_SOLID_RESULT_ROLE_TERM,
    PART_DRESSUP_TARGET_STRUCTURE_TERM,
    PART_DRESSUP_THICKNESS_OPERATION_TERM,
    FreeCADPartDressupAdapter,
    build_part_dressup_capability_document,
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
    FeatureReferenceBindingV2,
    FeatureResultV2,
    ParametricFeatureGraphV2,
    SemanticReferenceScope,
    SemanticReferenceV2,
    TermTypedValueV2,
)
from vibecad.parametric.freecad_part_dressup_rules import (
    MAX_DRESSUP_MAGNITUDE_MM,
    MAX_PART_DRESSUP_PLAN_BYTES,
    PART_DRESSUP_NATIVE_PROPERTIES,
    PartDressupOperation,
    PartDressupRuleError,
    decode_part_dressup_backend_plan,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _operation_terms(operation: PartDressupOperation):
    return next(item for item in PART_DRESSUP_OPERATION_TERMS if item.operation is operation)


def _graph(
    operation: PartDressupOperation,
    *,
    magnitude_mm: float = 2.0,
    locator_definition: str | None = None,
) -> ParametricFeatureGraphV2:
    operation_terms = _operation_terms(operation)
    terms = list(PART_DRESSUP_PFG_TERMS)
    if locator_definition is not None:
        index = terms.index(operation_terms.locator_term)
        terms[index] = dataclasses.replace(
            operation_terms.locator_term,
            term_definition_sha256=locator_definition,
        )
    parameter = DesignParameterV2(
        parameter_id="parameter_magnitude",
        name="Dress-up magnitude",
        semantic_role_term_ref_id=PART_DRESSUP_MAGNITUDE_ROLE_TERM.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id="value_magnitude",
            value_type_term_ref_id=PART_DRESSUP_LENGTH_TYPE_TERM.term_ref_id,
            encoding_term_ref_id=PART_DRESSUP_SCALAR_JSON_TERM.term_ref_id,
            value=magnitude_mm,
        ),
    )
    source = FeatureNodeV2(
        node_id="node_source",
        body_id="body_dressup",
        name="Authenticated source solid",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=PART_DRESSUP_REFERENCE_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=PART_DRESSUP_REFERENCE_FAMILY_TERM.term_ref_id,
            operation_term_ref_id=PART_DRESSUP_REFERENCE_OPERATION_TERM.term_ref_id,
        ),
        results=(
            FeatureResultV2(
                result_id="result_source_solid",
                semantic_role_term_ref_id=(PART_DRESSUP_SOURCE_SOLID_RESULT_ROLE_TERM.term_ref_id),
                value_type_term_ref_id=PART_DRESSUP_SOLID_TYPE_TERM.term_ref_id,
            ),
            FeatureResultV2(
                result_id="result_source_selection",
                semantic_role_term_ref_id=(operation_terms.selection_result_role.term_ref_id),
                value_type_term_ref_id=operation_terms.selection_value_type.term_ref_id,
            ),
        ),
    )
    reference = SemanticReferenceV2(
        reference_id="reference_dressup_selection",
        scope=SemanticReferenceScope.FEATURE,
        semantic_role_term_ref_id=PART_DRESSUP_REFERENCE_ROLE_TERM.term_ref_id,
        value_type_term_ref_id=operation_terms.selection_value_type.term_ref_id,
        locator_term_ref_id=operation_terms.locator_term.term_ref_id,
        source_node_id=source.node_id,
        source_geometry_id="result_source_selection",
    )
    target = FeatureNodeV2(
        node_id="node_target",
        body_id="body_dressup",
        name="Backend-neutral dress-up",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=PART_DRESSUP_TARGET_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=PART_DRESSUP_FAMILY_TERM.term_ref_id,
            operation_term_ref_id=operation_terms.operation_term.term_ref_id,
            input_ports=(
                FeatureInputPortV2(
                    port_id="port_source",
                    semantic_role_term_ref_id=PART_DRESSUP_SOURCE_PORT_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=PART_DRESSUP_SOLID_TYPE_TERM.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
                FeatureInputPortV2(
                    port_id="port_selection",
                    semantic_role_term_ref_id=(PART_DRESSUP_SELECTION_PORT_ROLE_TERM.term_ref_id),
                    value_type_term_ref_id=operation_terms.selection_value_type.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
                FeatureInputPortV2(
                    port_id="port_magnitude",
                    semantic_role_term_ref_id=PART_DRESSUP_MAGNITUDE_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=PART_DRESSUP_LENGTH_TYPE_TERM.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
            ),
            dependencies=(
                FeatureDependencyV2(
                    dependency_id="dependency_source",
                    port_id="port_source",
                    upstream_node_id=source.node_id,
                    upstream_result_id="result_source_solid",
                ),
            ),
            references=(
                FeatureReferenceBindingV2(
                    binding_id="binding_selection",
                    port_id="port_selection",
                    reference_id=reference.reference_id,
                ),
            ),
            parameter_bindings=(
                FeatureParameterBindingV2(
                    binding_id="binding_magnitude",
                    port_id="port_magnitude",
                    parameter_id=parameter.parameter_id,
                ),
            ),
        ),
        results=(
            FeatureResultV2(
                result_id="result_target",
                semantic_role_term_ref_id=PART_DRESSUP_RESULT_SOLID_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=PART_DRESSUP_SOLID_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id="graph_part_dressup",
        name="Part dress-up graph",
        terms=tuple(terms),
        bodies=(FeatureBodyV2(body_id="body_dressup", name="Dress-up body"),),
        parameters=(parameter,),
        references=(reference,),
        nodes=(source, target),
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
        namespace="org.vibecad.part-dressup-proof-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"proof:{term_id}"),
    )


RULE = _proof_term("rule_part_dressup_target", "rule.part-dressup-target-reviewed")
PREDICATE = _proof_term("predicate_part_dressup_target", "predicate.part-dressup-target-reviewed")
ROLE_PREMISE = _proof_term("role_part_dressup_candidate", "proof-role.dressup-candidate")
ROLE_CONCLUSION = _proof_term("role_part_dressup_validated", "proof-role.dressup-validated")
PART_DRESSUP_STRUCTURE_BRIDGE = _bridge_from_pfg(PART_DRESSUP_TARGET_STRUCTURE_TERM)


class _DressupEvaluator:
    def __init__(self) -> None:
        def signature(role: BridgeTermRef) -> RuleEndpointSignature:
            return RuleEndpointSignature(
                selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
                role_term=role,
                subject_type_term=PART_DRESSUP_STRUCTURE_BRIDGE,
            )

        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="part_dressup_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("part-dressup-target-evaluator-v1"),
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
            role_term_ref_id=PART_DRESSUP_INTENT_DOCUMENT_ROLE_TERM.term_ref_id,
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
            PART_DRESSUP_STRUCTURE_BRIDGE,
            PART_DRESSUP_INTENT_DOCUMENT_ROLE_TERM,
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_part_dressup_target",
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
                producer_id="part_dressup_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("part-dressup-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("upstream-part-dressup-compile-request"),
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
    max_output_bytes: int = MAX_PART_DRESSUP_PLAN_BYTES,
) -> tuple[BackendLoweringRequest, _Reader, TrustedRulePolicy]:
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = build_part_dressup_capability_document()
    policy = TrustedRulePolicy(evaluators=(_DressupEvaluator(),))
    request = BackendLoweringRequest(
        adapter=FREECAD_PART_DRESSUP_ADAPTER_DESCRIPTOR,
        terms=tuple((*PART_DRESSUP_REQUEST_TERMS, RULE, PREDICATE, ROLE_PREMISE, ROLE_CONCLUSION)),
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
    adapter: FreeCADPartDressupAdapter,
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


@pytest.mark.parametrize("operation", tuple(PartDressupOperation))
def test_shared_adapter_lowers_exact_three_specs_deterministically(
    operation: PartDressupOperation,
) -> None:
    request, reader, policy = _request(_graph(operation))
    sink = _MemoryPlanSink()
    adapter = FreeCADPartDressupAdapter(sink)
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    repeated_result, repeated_receipt = _lower(adapter, request, reader, policy)
    repeated_plan, repeated_payload = adapter.read_plan(repeated_receipt)

    assert plan.operation is operation
    assert plan.selection_role is _operation_terms(operation).selection_role
    assert plan.magnitude_mm == 2.0
    assert result.plan_document.document_digest == plan.plan_sha256
    assert result.plan_document.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert repeated_result == result and repeated_receipt == receipt
    assert repeated_plan == plan and repeated_payload == payload
    assert result.supported_subjects == (_subject(),)
    assert len(sink.items) == 1
    assert not adapter.executable and not plan.executable
    assert not adapter.grants_execution_authority and not receipt.grants_execution_authority
    assert b"Part::" not in payload
    assert b"Edge1" not in payload and b"Face1" not in payload


def test_unknown_locator_identity_and_sink_failure_are_inert() -> None:
    graph = _graph(
        PartDressupOperation.EDGE_FILLET,
        locator_definition=_sha("substituted locator definition"),
    )
    request, reader, policy = _request(graph)
    sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartDressupAdapter(sink), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    request, reader, policy = _request(_graph(PartDressupOperation.FACE_THICKNESS))
    sink = _MemoryPlanSink(fail=True)
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartDressupAdapter(sink), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert sink.items == {}


def test_magnitude_n_n_plus_one_plan_tamper_and_output_budget() -> None:
    request, reader, policy = _request(
        _graph(PartDressupOperation.EDGE_CHAMFER, magnitude_mm=MAX_DRESSUP_MAGNITUDE_MM)
    )
    adapter = FreeCADPartDressupAdapter(_MemoryPlanSink())
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    assert plan.magnitude_mm == MAX_DRESSUP_MAGNITUDE_MM

    request, reader, policy = _request(
        _graph(
            PartDressupOperation.EDGE_CHAMFER,
            magnitude_mm=MAX_DRESSUP_MAGNITUDE_MM + 0.001,
        )
    )
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartDressupAdapter(_MemoryPlanSink()), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION

    with pytest.raises(PartDressupRuleError):
        decode_part_dressup_backend_plan(
            payload + b" ",
            expected_content_sha256=result.plan_document.content_sha256,
            expected_plan_sha256=plan.plan_sha256,
        )
    duplicate = payload.replace(b'{"authority":', b'{"authority":"none","authority":', 1)
    with pytest.raises(PartDressupRuleError):
        decode_part_dressup_backend_plan(duplicate)

    request, reader, policy = _request(
        _graph(PartDressupOperation.EDGE_CHAMFER), max_output_bytes=1
    )
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADPartDressupAdapter(_MemoryPlanSink()), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED


def test_manifest_counts_three_user_semantics_and_exact_native_properties() -> None:
    assert tuple(item.operation_id for item in PART_DRESSUP_OPERATION_SPECS) == tuple(
        item.value for item in PartDressupOperation
    )
    assert tuple(item.native_type_id for item in PART_DRESSUP_OPERATION_SPECS) == (
        "Part::Fillet",
        "Part::Chamfer",
        "Part::Thickness",
    )
    assert PART_DRESSUP_NATIVE_PROPERTIES == {
        PartDressupOperation.EDGE_FILLET: ("Base", "EdgeLinks", "Edges"),
        PartDressupOperation.EDGE_CHAMFER: ("Base", "EdgeLinks", "Edges"),
        PartDressupOperation.FACE_THICKNESS: (
            "Faces",
            "Intersection",
            "Join",
            "Mode",
            "SelfIntersection",
            "Value",
        ),
    }


def test_selection_value_type_is_bound_to_operation_family() -> None:
    edge_terms = _operation_terms(PartDressupOperation.EDGE_FILLET)
    face_terms = _operation_terms(PartDressupOperation.FACE_THICKNESS)
    assert edge_terms.selection_value_type is PART_DRESSUP_EDGE_REFERENCE_TYPE_TERM
    assert edge_terms.locator_term is PART_DRESSUP_EDGE_LOCATOR_TERM
    assert face_terms.selection_value_type is PART_DRESSUP_FACE_REFERENCE_TYPE_TERM
    assert face_terms.locator_term is PART_DRESSUP_FACE_LOCATOR_TERM
    assert PART_DRESSUP_FILLET_OPERATION_TERM is edge_terms.operation_term
    assert (
        PART_DRESSUP_CHAMFER_OPERATION_TERM
        is _operation_terms(PartDressupOperation.EDGE_CHAMFER).operation_term
    )
    assert PART_DRESSUP_THICKNESS_OPERATION_TERM is face_terms.operation_term


@pytest.mark.slow
def test_real_freecad_part_dressup_batch_create_edit_reopen_and_rollback(
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
    for index, operation in enumerate(PartDressupOperation):
        request, reader, policy = _request(_graph(operation))
        adapter = FreeCADPartDressupAdapter(_MemoryPlanSink())
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
                "source_node_id": plan.source_node_id,
                "source_solid_result_id": plan.source_solid_result_id,
            }
        )
    source_root = Path(__file__).parents[1] / "src"
    output_root = tmp_path / "freecad-part-dressup"
    output_root.mkdir()
    code = f"""
import os, sys
sys.path.insert(0, {str(source_root)!r})
from vibecad.freecad_env import prepare_freecad_import
prepare_freecad_import()
from pathlib import Path
import FreeCAD, Part
from vibecad.parametric.freecad_part_dressup_rules import (
    PartDressupExecutionBindings,
    PartDressupRuleError,
    PartDressupRuleErrorCode,
    apply_part_dressup_plan,
)

CASES = {cases!r}
OUTPUT_ROOT = Path({str(output_root)!r})
TYPE_IDS = {{
    'edge_fillet': 'Part::Fillet',
    'edge_chamfer': 'Part::Chamfer',
    'face_thickness': 'Part::Thickness',
}}

def shape_signature(item):
    if 'Shape' not in tuple(item.PropertiesList):
        return None
    shape = item.Shape
    if shape.isNull():
        return (True,)
    return (
        False, int(shape.hashCode()), float(shape.Volume), float(shape.Area),
        len(tuple(shape.Edges)), len(tuple(shape.Faces)), len(tuple(shape.Solids)))

def snapshot(document):
    objects = tuple(document.Objects)
    groups = tuple(
        (item, tuple(item.Group)) for item in objects if 'Group' in tuple(item.PropertiesList))
    visibility = tuple(
        (item, bool(item.Visibility))
        for item in objects if 'Visibility' in tuple(item.PropertiesList))
    shapes = tuple((item, shape_signature(item)) for item in objects)
    return objects, groups, visibility, shapes, bool(document.HasPendingTransaction)

def same_snapshot(document, before):
    objects, groups, visibility, shapes, pending = before
    return (
        tuple(document.Objects) == objects
        and all(tuple(item.Group) == members for item, members in groups)
        and all(bool(item.Visibility) is value for item, value in visibility)
        and all(shape_signature(item) == signature for item, signature in shapes)
        and bool(document.HasPendingTransaction) is pending
    )

def bindings(entry, document, source):
    return PartDressupExecutionBindings(
        document=document,
        container_id=entry['container_id'],
        source_node_id=entry['source_node_id'],
        source_solid_result_id=entry['source_solid_result_id'],
        source_object=source,
    )

def bad_shape(operation, mode):
    if mode == 'zero':
        return Part.makeCylinder(5, 10) if operation != 'face_thickness' else Part.makeSphere(5)
    if operation != 'face_thickness':
        lower = Part.makeBox(30, 20, 5)
        connector = Part.makeBox(10, 10, 10, FreeCAD.Vector(0, 0, 5))
        upper = Part.makeBox(30, 20, 5, FreeCAD.Vector(0, 0, 15))
        return lower.fuse(connector).fuse(upper).removeSplitter()
    left = Part.makeBox(10, 10, 10)
    right = Part.makeBox(10, 10, 10, FreeCAD.Vector(20, 0, 0))
    bridge = Part.makeBox(30, 10, 2)
    return left.fuse(bridge).fuse(right).removeSplitter()

persisted = []
for index, entry in enumerate(CASES):
    document = FreeCAD.newDocument('PartDressup' + str(index))
    document.UndoMode = 1
    source = document.addObject('Part::Box', 'Source')
    source.Length = 30
    source.Width = 20
    source.Height = 10
    document.recompute()
    payload = Path(entry['path']).read_bytes()

    before = snapshot(document)
    try:
        apply_part_dressup_plan(
            payload + b' ',
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings(entry, document, source),
        )
        raise AssertionError('tamper accepted')
    except PartDressupRuleError:
        assert same_snapshot(document, before)

    for mode in ('zero', 'multiple'):
        bad = document.addObject('Part::Feature', 'Bad' + mode.title())
        bad.Shape = bad_shape(entry['operation'], mode)
        document.recompute()
        assert bad.Shape.isValid() and len(tuple(bad.Shape.Solids)) == 1
        before = snapshot(document)
        try:
            apply_part_dressup_plan(
                payload,
                expected_content_sha256=entry['content_sha256'],
                expected_plan_sha256=entry['plan_sha256'],
                bindings=bindings(entry, document, bad),
            )
            raise AssertionError(mode + ' semantic selection accepted')
        except PartDressupRuleError as error:
            assert error.code is PartDressupRuleErrorCode.SELECTION_FAILED
            assert same_snapshot(document, before)

    receipt = apply_part_dressup_plan(
        payload,
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=bindings(entry, document, source),
    )
    feature = document.getObject(receipt.object_name)
    assert feature.TypeId == TYPE_IDS[entry['operation']] and feature.isValid()
    initial_volume = float(feature.Shape.Volume)
    source.Length = 34
    source.Width = 22
    source.Height = 12
    document.recompute()
    propagated_volume = float(feature.Shape.Volume)
    assert propagated_volume != initial_volume and feature.isValid()
    if entry['operation'] == 'face_thickness':
        feature.Value = 2.5
    else:
        native_index = int(feature.Edges[0][0])
        feature.Edges = [(native_index, 3.0, 3.0)]
    document.recompute()
    edited_volume = float(feature.Shape.Volume)
    assert edited_volume != propagated_volume and feature.isValid()
    path = OUTPUT_ROOT / f'{{index}}.FCStd'
    document.saveAs(str(path))
    persisted.append((path, receipt.object_name, entry['operation'], edited_volume))
    FreeCAD.closeDocument(document.Name)

for path, object_name, operation, expected_volume in persisted:
    reopened = FreeCAD.openDocument(str(path))
    feature = reopened.getObject(object_name)
    source = reopened.getObject('Source')
    assert feature is not None and feature.TypeId == TYPE_IDS[operation] and feature.isValid()
    assert source is not None and source.isValid()
    assert abs(float(feature.Shape.Volume) - expected_volume) < 1e-7
    if operation == 'face_thickness':
        assert feature.Faces[0] is source and tuple(feature.Faces[1]) == ('Face6',)
        assert abs(float(feature.Value) - 2.5) < 1e-9
    else:
        edge_link_base, edge_link_names = feature.EdgeLinks
        assert feature.Base is source and edge_link_base is source
        assert tuple(edge_link_names) == ('Edge7',)
        assert tuple(feature.Edges[0][1:]) == (3.0, 3.0)
    FreeCAD.closeDocument(reopened.Name)

class LateOwnershipObserver:
    def __init__(self, group):
        self.group = group
    def slotCreatedObject(self, item):
        if item.TypeId == 'Part::Fillet':
            self.group.addObject(item)

entry = CASES[0]
document = FreeCAD.newDocument('PartDressupLateRollback')
document.UndoMode = 1
source = document.addObject('Part::Box', 'Source')
source.Length = 30
source.Width = 20
source.Height = 10
group = document.addObject('App::DocumentObjectGroup', 'GuardGroup')
document.recompute()
observer = LateOwnershipObserver(group)
FreeCAD.addDocumentObserver(observer)
before = snapshot(document)
try:
    try:
        apply_part_dressup_plan(
            Path(entry['path']).read_bytes(),
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings(entry, document, source),
        )
        raise AssertionError('late ownership violation accepted')
    except PartDressupRuleError:
        assert same_snapshot(document, before)
finally:
    FreeCAD.removeDocumentObserver(observer)
FreeCAD.closeDocument(document.Name)
print('PART_DRESSUP_BATCH_OK')
"""
    completed = subprocess.run(
        [str(runtime_python), "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "PART_DRESSUP_BATCH_OK" in completed.stdout
