"""Focused and one batched real gate for the reviewed Part core family."""

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
from vibecad.intent_bridge.freecad_part_core_adapter import (
    FREECAD_PART_CORE_ADAPTER_DESCRIPTOR,
    PART_CORE_CANONICAL_JSON_TERM,
    PART_CORE_INTENT_ROLE_TERM,
    PART_CORE_MANIFEST,
    PART_CORE_OPERATION_SPECS,
    PART_CORE_OPERATION_TERMS,
    PART_CORE_PARAMETERS_ROLE_TERM,
    PART_CORE_PARAMETERS_TYPE_TERM,
    PART_CORE_PFG_TERMS,
    PART_CORE_REQUEST_TERMS,
    PART_CORE_RESULT_ROLE_TERM,
    PART_CORE_SHAPE_TYPE_TERM,
    PART_CORE_SOURCE_FAMILY_TERM,
    PART_CORE_SOURCE_OPERATION_TERM,
    PART_CORE_SOURCE_ROLE_TERM,
    PART_CORE_SOURCE_STRUCTURE_TERM,
    PART_CORE_STRUCTURE_TERM,
    build_part_core_adapter,
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
    SemanticTermRefV2,
    TermTypedValueV2,
)
from vibecad.parametric.freecad_part_core_rules import (
    MAX_PART_CORE_PLAN_BYTES,
    PART_CORE_NATIVE_SPECS,
    PART_CORE_RULE_CONTRACT_SHA256,
    PartCoreOperation,
    PartCoreRuleError,
    decode_part_core_backend_plan,
)


def _sha(value: str | bytes) -> str:
    raw = value if type(value) is bytes else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _bridge_from_pfg(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


_PRIMITIVE_PARAMETERS: dict[PartCoreOperation, dict[str, object]] = {
    PartCoreOperation.BOX: {
        "size_x_mm": 10.0,
        "size_y_mm": 8.0,
        "size_z_mm": 6.0,
    },
    PartCoreOperation.CONE: {
        "base_radius_mm": 5.0,
        "top_radius_mm": 2.0,
        "height_mm": 8.0,
        "sweep_degrees": 360.0,
    },
    PartCoreOperation.CYLINDER: {
        "radius_mm": 5.0,
        "height_mm": 8.0,
        "sweep_degrees": 360.0,
    },
    PartCoreOperation.ELLIPSOID: {
        "radius_x_mm": 5.0,
        "radius_y_mm": 4.0,
        "radius_z_mm": 3.0,
        "latitude_min_degrees": -90.0,
        "latitude_max_degrees": 90.0,
        "sweep_degrees": 360.0,
    },
    PartCoreOperation.PRISM: {
        "side_count": 6,
        "circumradius_mm": 5.0,
        "height_mm": 8.0,
    },
    PartCoreOperation.SPHERE: {
        "radius_mm": 5.0,
        "latitude_min_degrees": -90.0,
        "latitude_max_degrees": 90.0,
        "sweep_degrees": 360.0,
    },
    PartCoreOperation.TORUS: {
        "major_radius_mm": 8.0,
        "minor_radius_mm": 2.0,
        "latitude_min_degrees": -180.0,
        "latitude_max_degrees": 180.0,
        "sweep_degrees": 360.0,
    },
    PartCoreOperation.WEDGE: {
        "x_min_mm": 0.0,
        "y_min_mm": 0.0,
        "z_min_mm": 0.0,
        "x_inner_min_mm": 2.0,
        "z_inner_min_mm": 1.0,
        "x_max_mm": 10.0,
        "y_max_mm": 8.0,
        "z_max_mm": 6.0,
        "x_inner_max_mm": 8.0,
        "z_inner_max_mm": 5.0,
    },
}


def _parameters(operation: PartCoreOperation) -> dict[str, object]:
    shape = _PRIMITIVE_PARAMETERS.get(operation)
    if shape is not None:
        return {
            "shape": dict(shape),
            "placement": {
                "translation_mm": [0.0, 0.0, 0.0],
                "rotation_axis": [0.0, 0.0, 1.0],
                "rotation_degrees": 0.0,
            },
        }
    if operation is PartCoreOperation.MIRROR:
        return {"base_point_mm": [0.0, 0.0, 0.0], "normal": [1.0, 0.0, 0.0]}
    if operation is PartCoreOperation.SCALE:
        return {"scale_xyz": [2.0, 2.0, 2.0]}
    return {}


def _source_node(index: int) -> FeatureNodeV2:
    return FeatureNodeV2(
        node_id=f"node_source_{index}",
        body_id="body_main",
        name=f"Authenticated source {index}",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=PART_CORE_SOURCE_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=PART_CORE_SOURCE_FAMILY_TERM.term_ref_id,
            operation_term_ref_id=PART_CORE_SOURCE_OPERATION_TERM.term_ref_id,
        ),
        results=(
            FeatureResultV2(
                result_id=f"result_source_{index}",
                semantic_role_term_ref_id=PART_CORE_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=PART_CORE_SHAPE_TYPE_TERM.term_ref_id,
            ),
        ),
    )


def _graph(
    operation: PartCoreOperation,
    *,
    source_count: int | None = None,
    parameter_value: object | None = None,
    operation_definition: str | None = None,
) -> ParametricFeatureGraphV2:
    spec = PART_CORE_NATIVE_SPECS[operation]
    if source_count is None:
        source_count = spec.minimum_sources
    sources = tuple(_source_node(index) for index in range(source_count))
    operation_terms = next(
        item for item in PART_CORE_OPERATION_TERMS if item.operation is operation
    )
    terms = list(PART_CORE_PFG_TERMS)
    if operation_definition is not None:
        index = terms.index(operation_terms.operation_term)
        terms[index] = dataclasses.replace(
            operation_terms.operation_term,
            term_definition_sha256=operation_definition,
        )
    parameter = DesignParameterV2(
        parameter_id="parameter_target",
        name="Native TypeId and property names are inert graph text",
        semantic_role_term_ref_id=PART_CORE_PARAMETERS_ROLE_TERM.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id="value_target",
            value_type_term_ref_id=PART_CORE_PARAMETERS_TYPE_TERM.term_ref_id,
            encoding_term_ref_id=PART_CORE_CANONICAL_JSON_TERM.term_ref_id,
            value=(_parameters(operation) if parameter_value is None else parameter_value),
        ),
    )
    target = FeatureNodeV2(
        node_id="node_target",
        body_id="body_main",
        name="Part::Box Base Tool Source must not select native code",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=PART_CORE_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=operation_terms.family_term.term_ref_id,
            operation_term_ref_id=operation_terms.operation_term.term_ref_id,
            input_ports=(
                FeatureInputPortV2(
                    port_id="port_sources",
                    semantic_role_term_ref_id=PART_CORE_SOURCE_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=PART_CORE_SHAPE_TYPE_TERM.term_ref_id,
                    minimum_cardinality=spec.minimum_sources,
                    maximum_cardinality=max(1, spec.maximum_sources),
                    ordered=spec.maximum_sources > 1,
                ),
                FeatureInputPortV2(
                    port_id="port_parameters",
                    semantic_role_term_ref_id=PART_CORE_PARAMETERS_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=PART_CORE_PARAMETERS_TYPE_TERM.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
            ),
            dependencies=tuple(
                FeatureDependencyV2(
                    dependency_id=f"dependency_source_{index}",
                    port_id="port_sources",
                    upstream_node_id=source.node_id,
                    upstream_result_id=source.results[0].result_id,
                    ordinal=index,
                )
                for index, source in enumerate(sources)
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
                semantic_role_term_ref_id=PART_CORE_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=PART_CORE_SHAPE_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id="graph_part_core",
        name="Reviewed Part core intent",
        terms=tuple(terms),
        bodies=(FeatureBodyV2(body_id="body_main", name="Part result set"),),
        parameters=(parameter,),
        references=(),
        nodes=(*sources, target),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection_target",
                node_id=target.node_id,
                result_id=target.results[0].result_id,
            ),
        ),
    )


def _proof_term(term_ref_id: str, term_id: str) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.part-core-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"proof:{term_id}"),
    )


RULE = _proof_term("rule_part_core_target", "rule.part-core-target")
PREDICATE = _proof_term("predicate_part_core_target", "predicate.part-core-target")
ROLE_PREMISE = _proof_term("role_part_core_candidate", "proof-role.candidate")
ROLE_CONCLUSION = _proof_term("role_part_core_validated", "proof-role.validated")
PART_CORE_STRUCTURE_BRIDGE = _bridge_from_pfg(PART_CORE_STRUCTURE_TERM)


class _Evaluator:
    def __init__(self) -> None:
        def signature(role: BridgeTermRef) -> RuleEndpointSignature:
            return RuleEndpointSignature(
                selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
                role_term=role,
                subject_type_term=PART_CORE_STRUCTURE_BRIDGE,
            )

        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="part_core_test_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("part-core-evaluator-v1"),
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


class _Reader:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        payload = self.payloads[document.artifact_id]
        if len(payload) > maximum_bytes:
            raise RuntimeError("over budget")
        return payload


class _Sink:
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


def _intent_document(graph: ParametricFeatureGraphV2) -> tuple[DocumentRef, bytes]:
    payload = graph.canonical_bytes
    return (
        DocumentRef(
            artifact_id="artifact_part_core_pfg",
            role_term_ref_id=PART_CORE_INTENT_ROLE_TERM.term_ref_id,
            schema_term_ref_id=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM.term_ref_id,
            document_id=graph.graph_id,
            document_digest=graph.graph_sha256,
            content_sha256=_sha(payload),
            size_bytes=len(payload),
            media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
        ),
        payload,
    )


def _subject() -> SubjectRef:
    return SubjectRef(
        artifact_id="artifact_part_core_pfg",
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
            PART_CORE_STRUCTURE_BRIDGE,
            PART_CORE_INTENT_ROLE_TERM,
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_part_core_target",
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
                producer_id="part_core_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("part-core-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("part-core-upstream-request"),
        ),
    )


def _request(graph: ParametricFeatureGraphV2):
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = PART_CORE_MANIFEST.capability_document()
    policy = TrustedRulePolicy(evaluators=(_Evaluator(),))
    request = BackendLoweringRequest(
        adapter=FREECAD_PART_CORE_ADAPTER_DESCRIPTOR,
        terms=(*PART_CORE_REQUEST_TERMS, RULE, PREDICATE, ROLE_PREMISE, ROLE_CONCLUSION),
        documents=(intent_document, capability_document),
        intent_artifact_ids=(intent_document.artifact_id,),
        capability_artifact_ids=(capability_document.artifact_id,),
        proof_bundle=_proof(policy, intent_document),
        budget=BridgeBudget(
            max_input_bytes=len(intent_payload) + len(capability_payload),
            max_output_bytes=MAX_PART_CORE_PLAN_BYTES,
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


@pytest.mark.parametrize("operation", tuple(PartCoreOperation))
def test_shared_adapter_lowers_every_honest_part_core_semantic(
    operation: PartCoreOperation,
) -> None:
    request, reader, policy = _request(_graph(operation))
    sink = _Sink()
    adapter = build_part_core_adapter(sink)

    result, receipt = adapter.lower_with_receipt(
        request,
        artifacts=reader,
        codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
        proof_policy=policy,
    )
    plan, payload = adapter.read_plan(receipt)
    repeated, repeated_receipt = adapter.lower_with_receipt(
        request,
        artifacts=reader,
        codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
        proof_policy=policy,
    )

    assert result.disposition is BridgeDisposition.COMPLETE
    assert repeated == result and repeated_receipt == receipt
    assert plan.operation is operation
    assert plan.source_graph_sha256 == _graph(operation).graph_sha256
    assert len(plan.sources) == PART_CORE_NATIVE_SPECS[operation].minimum_sources
    assert payload == plan.canonical_bytes
    assert receipt.operation.operation_id == operation.value
    assert receipt.grants_execution_authority is False


def test_inventory_excludes_compound2_alias_and_binds_all_native_contracts() -> None:
    assert len(PART_CORE_OPERATION_SPECS) == len(PartCoreOperation) == 19
    assert len({item.semantic_term.semantic_identity for item in PART_CORE_OPERATION_SPECS}) == 19
    assert {item.native_type_id for item in PART_CORE_OPERATION_SPECS} == {
        item.type_id for item in PART_CORE_NATIVE_SPECS.values()
    }
    assert "Part::Compound" in {item.native_type_id for item in PART_CORE_OPERATION_SPECS}
    assert "Part::Compound2" not in {item.native_type_id for item in PART_CORE_OPERATION_SPECS}
    assert len(PART_CORE_RULE_CONTRACT_SHA256) == 64


def test_rebound_semantics_invalid_parameters_and_sink_failure_publish_nothing() -> None:
    sink = _Sink()
    adapter = build_part_core_adapter(sink)
    rebound = _graph(PartCoreOperation.BOX, operation_definition="f" * 64)
    request, reader, policy = _request(rebound)
    with pytest.raises(IntentBridgeError) as semantic:
        adapter.lower_with_receipt(
            request,
            artifacts=reader,
            codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
            proof_policy=policy,
        )
    assert semantic.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    invalid = _parameters(PartCoreOperation.SCALE)
    invalid["scale_xyz"] = [1.0, 0.0, 1.0]
    request, reader, policy = _request(_graph(PartCoreOperation.SCALE, parameter_value=invalid))
    with pytest.raises(IntentBridgeError) as parameters:
        adapter.lower_with_receipt(
            request,
            artifacts=reader,
            codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
            proof_policy=policy,
        )
    assert parameters.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    request, reader, policy = _request(_graph(PartCoreOperation.COMPOUND))
    failed_sink = _Sink(fail=True)
    with pytest.raises(IntentBridgeError) as publication:
        build_part_core_adapter(failed_sink).lower_with_receipt(
            request,
            artifacts=reader,
            codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
            proof_policy=policy,
        )
    assert publication.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert failed_sink.items == {}


def test_parameter_term_alias_rebinding_is_not_local_id_authority() -> None:
    graph = _graph(PartCoreOperation.BOX)
    terms = list(graph.terms)
    role_index = terms.index(PART_CORE_PARAMETERS_ROLE_TERM)
    result_index = terms.index(PART_CORE_RESULT_ROLE_TERM)
    role_ref = terms[role_index].term_ref_id
    result_ref = terms[result_index].term_ref_id
    terms[role_index] = dataclasses.replace(terms[role_index], term_ref_id=result_ref)
    terms[result_index] = dataclasses.replace(terms[result_index], term_ref_id=role_ref)
    rebound = dataclasses.replace(graph, terms=tuple(terms))
    request, reader, policy = _request(rebound)
    sink = _Sink()

    with pytest.raises(IntentBridgeError) as error:
        build_part_core_adapter(sink).lower_with_receipt(
            request,
            artifacts=reader,
            codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
            proof_policy=policy,
        )
    assert error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}


def test_plan_decoder_is_canonical_bounded_and_tamper_evident() -> None:
    request, reader, policy = _request(_graph(PartCoreOperation.MULTI_FUSE))
    adapter = build_part_core_adapter(_Sink())
    result, receipt = adapter.lower_with_receipt(
        request,
        artifacts=reader,
        codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
        proof_policy=policy,
    )
    plan, payload = adapter.read_plan(receipt)
    assert (
        decode_part_core_backend_plan(
            payload,
            expected_content_sha256=result.plan_document.content_sha256,
            expected_plan_sha256=result.plan_document.document_digest,
        )
        == plan
    )
    with pytest.raises(PartCoreRuleError):
        decode_part_core_backend_plan(payload + b" ")
    mapping = json.loads(payload)
    mapping["operation"] = "cut"
    tampered = json.dumps(mapping, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )
    with pytest.raises(PartCoreRuleError):
        decode_part_core_backend_plan(tampered)


@pytest.mark.slow
def test_real_freecad_part_core_batch_create_edit_reopen_and_rollback(
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

    plan_entries = []
    for operation in PartCoreOperation:
        request, reader, policy = _request(_graph(operation))
        adapter = build_part_core_adapter(_Sink())
        result, receipt = adapter.lower_with_receipt(
            request,
            artifacts=reader,
            codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
            proof_policy=policy,
        )
        plan, payload = adapter.read_plan(receipt)
        path = tmp_path / f"{operation.value}.json"
        path.write_bytes(payload)
        plan_entries.append(
            {
                "operation": operation.value,
                "path": str(path),
                "content_sha256": result.plan_document.content_sha256,
                "plan_sha256": result.plan_document.document_digest,
                "body_id": plan.body_id,
                "sources": [(item.node_id, item.result_id) for item in plan.sources],
            }
        )

    model_path = tmp_path / "part-core.FCStd"
    source_root = Path(__file__).parents[1] / "src"
    code = _real_batch_code(plan_entries, model_path, source_root)
    completed = subprocess.run(
        [str(runtime_python), "-c", code],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    assert "REAL_PART_CORE_BATCH_OK" in completed.stdout


def _real_batch_code(
    plan_entries: list[dict[str, object]], model_path: Path, source_root: Path
) -> str:
    """Return the one managed-runtime script; kept separate for readable failure output."""

    return f"""
import os, sys
sys.path.insert(0, {str(source_root)!r})
from vibecad.freecad_env import prepare_freecad_import
prepare_freecad_import()
from pathlib import Path
import FreeCAD, Part
import vibecad.parametric.freecad_part_core_rules as part_core_rules
from vibecad.parametric.freecad_part_core_rules import (
    AuthenticatedPartCoreObject,
    PartCoreExecutionBindings,
    PartCoreOperation,
    PartCoreRuleError,
    PartCoreRuleErrorCode,
    apply_part_core_plan,
)

ENTRIES = {plan_entries!r}

def make_source(document, operation, index, ordinal):
    name = f'Source_{{index}}_{{ordinal}}'
    if operation == 'refine':
        left = Part.makeBox(10, 8, 6)
        right = Part.makeBox(10, 8, 6, FreeCAD.Vector(10, 0, 0))
        obj = document.addObject('Part::Feature', name)
        obj.Shape = left.fuse(right)
        return obj
    obj = document.addObject('Part::Box', name)
    obj.Length = 10
    obj.Width = 10
    obj.Height = 10
    if operation in ('cut', 'fuse', 'common', 'section') and ordinal == 1:
        obj.Length = obj.Width = obj.Height = 8
        obj.Placement.Base = FreeCAD.Vector(5, 0, 0)
    elif operation in ('multi_fuse', 'multi_common', 'compound'):
        obj.Placement.Base = FreeCAD.Vector(ordinal * 5, 0, 0)
    document.recompute()
    return obj

document = FreeCAD.newDocument('PartCoreBatch')
document.UndoMode = 1
persisted = []
for index, entry in enumerate(ENTRIES):
    sources = [
        make_source(document, entry['operation'], index, ordinal)
        for ordinal, _selection in enumerate(entry['sources'])
    ]
    authenticated = tuple(
        AuthenticatedPartCoreObject(
            object=obj,
            node_id=selection[0],
            result_id=selection[1],
        )
        for obj, selection in zip(sources, entry['sources'])
    )
    bindings = PartCoreExecutionBindings(
        document=document,
        body_id=entry['body_id'],
        sources=authenticated,
    )
    payload = Path(entry['path']).read_bytes()
    try:
        receipt = apply_part_core_plan(
            payload,
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings,
        )
    except Exception as error:
        raise AssertionError('create failed: ' + entry['operation']) from error
    result = document.getObject(receipt.object_name)
    assert result is not None and result.isValid()
    before = result.Shape.exportBrepToString()
    primitive_edits = {{
        'box': ('Length', 12.0),
        'cone': ('Height', 9.0),
        'cylinder': ('Radius', 6.0),
        'ellipsoid': ('Radius1', 6.0),
        'prism': ('Circumradius', 6.0),
        'sphere': ('Radius', 6.0),
        'torus': ('Radius1', 9.0),
        'wedge': ('Xmax', 12.0),
    }}
    if entry['operation'] in primitive_edits:
        property_name, value = primitive_edits[entry['operation']]
        setattr(result, property_name, value)
    elif entry['operation'] == 'scale':
        result.UniformScale = 1.5
        result.XScale = result.YScale = result.ZScale = 1.5
    elif entry['operation'] in ('fuse', 'multi_fuse'):
        sources[0].Placement.Base.x = -2
    elif entry['operation'] == 'cut':
        sources[1].Placement.Base.x = 6
    elif entry['operation'] == 'multi_common':
        sources[0].Length = 6
    elif entry['operation'] == 'section':
        sources[1].Placement.Base.x = 6
    elif entry['operation'] == 'refine':
        left = Part.makeBox(12, 8, 6)
        right = Part.makeBox(10, 8, 6, FreeCAD.Vector(12, 0, 0))
        sources[0].Shape = left.fuse(right)
    elif sources:
        sources[0].Length = 12
    document.recompute()
    after = result.Shape.exportBrepToString()
    assert before != after
    persisted.append((result.Name, result.TypeId, result.Shape.ShapeType))

document.saveAs({str(model_path)!r})
FreeCAD.closeDocument(document.Name)
reopened = FreeCAD.openDocument({str(model_path)!r})
reopened.recompute()
for name, type_id, shape_type in persisted:
    result = reopened.getObject(name)
    assert result.TypeId == type_id and result.isValid()
    assert result.Shape.ShapeType == shape_type and not result.Shape.isNull()
FreeCAD.closeDocument(reopened.Name)

# Every semantic crosses a real post-create validation failure and proves the
# native transaction restores objects and source visibility exactly.
rollback = FreeCAD.newDocument('PartCoreRollback')
rollback.UndoMode = 1
for index, entry in enumerate(ENTRIES):
    sources = [
        make_source(rollback, entry['operation'], index, ordinal)
        for ordinal, _selection in enumerate(entry['sources'])
    ]
    authenticated = tuple(
        AuthenticatedPartCoreObject(
            object=obj,
            node_id=selection[0],
            result_id=selection[1],
        )
        for obj, selection in zip(sources, entry['sources'])
    )
    bindings = PartCoreExecutionBindings(
        document=rollback,
        body_id=entry['body_id'],
        sources=authenticated,
    )
    payload = Path(entry['path']).read_bytes()
    before = tuple(rollback.Objects)
    before_visibility = tuple(bool(item.Visibility) for item in before)
    original_validate_effect = part_core_rules._validate_effect
    def fail_after_native_create(*_args, **_kwargs):
        raise PartCoreRuleError(
            PartCoreRuleErrorCode.CONFORMANCE_FAILED,
            '/result/injected-late-failure',
        )
    part_core_rules._validate_effect = fail_after_native_create
    try:
        apply_part_core_plan(
            payload,
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings,
        )
    except PartCoreRuleError:
        pass
    else:
        raise AssertionError(entry['operation'] + ' late failure must roll back')
    finally:
        part_core_rules._validate_effect = original_validate_effect
    assert tuple(rollback.Objects) == before
    assert tuple(bool(item.Visibility) for item in rollback.Objects) == before_visibility
FreeCAD.closeDocument(rollback.Name)
print('REAL_PART_CORE_BATCH_OK')
"""
