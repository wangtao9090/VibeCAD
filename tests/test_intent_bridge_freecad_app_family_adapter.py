"""Focused gates for the ten-spec reviewed FreeCAD application family."""

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
from vibecad.intent_bridge.freecad_app_family_adapter import (
    APP_FAMILY_CANONICAL_JSON_TERM,
    APP_FAMILY_CONFIGURATION_ROLE_TERM,
    APP_FAMILY_CONFIGURATION_TYPE_TERM,
    APP_FAMILY_INTENT_DOCUMENT_ROLE_TERM,
    APP_FAMILY_OPERATION_SPECS,
    APP_FAMILY_OPERATION_TERMS,
    APP_FAMILY_PFG_TERMS,
    APP_FAMILY_RELATED_OBJECT_ROLE_TERM,
    APP_FAMILY_RELATED_OBJECT_TYPE_TERM,
    APP_FAMILY_REQUEST_TERMS,
    APP_FAMILY_SOURCE_FAMILY_TERM,
    APP_FAMILY_SOURCE_OPERATION_TERM,
    APP_FAMILY_SOURCE_RESULT_ROLE_TERM,
    APP_FAMILY_SOURCE_RESULT_TYPE_TERM,
    APP_FAMILY_SOURCE_STRUCTURE_TERM,
    APP_FAMILY_STRUCTURE_TERM,
    FREECAD_APP_FAMILY_ADAPTER_DESCRIPTOR,
    FreeCADAppFamilyAdapter,
    build_app_family_capability_document,
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
from vibecad.parametric.freecad_app_family_rules import (
    APP_FAMILY_EXCLUDED_CANDIDATES,
    APP_FAMILY_NATIVE_TYPE_IDS,
    APP_FAMILY_RELATION_KINDS,
    MAX_ANNOTATION_LINES,
    MAX_APP_FAMILY_PLAN_BYTES,
    AppFamilyOperation,
    AppFamilyRelationKind,
    AppFamilyRuleError,
    decode_app_family_backend_plan,
    encode_app_family_configuration,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_PLACEMENT = {
    "position_mm": [3.0, 4.0, 5.0],
    "axis": [0.0, 0.0, 1.0],
    "angle_degrees": 30.0,
}

CONFIGURATIONS = {
    AppFamilyOperation.TEXT_ANNOTATION: {
        "lines": ["reviewed", "annotation"],
        "position_mm": [1.0, 2.0, 3.0],
    },
    AppFamilyOperation.LEADER_ANNOTATION: {
        "lines": ["reviewed leader"],
        "base_position_mm": [1.0, 2.0, 3.0],
        "text_position_mm": [4.0, 5.0, 6.0],
    },
    AppFamilyOperation.DOCUMENT_GROUP: {},
    AppFamilyOperation.OBJECT_LINK: {"placement": _PLACEMENT},
    AppFamilyOperation.LINK_GROUP: {"placement": _PLACEMENT},
    AppFamilyOperation.MATERIAL_DEFINITION: {
        "name": "Reviewed material",
        "description": "Bounded metadata",
        "density_kg_m3": 2700.0,
    },
    AppFamilyOperation.POSITIONED_PART: {"placement": _PLACEMENT},
    AppFamilyOperation.PLACEMENT_REFERENCE: {"placement": _PLACEMENT},
    AppFamilyOperation.TEXT_DOCUMENT: {"text": "Bounded reviewed text"},
    AppFamilyOperation.SCALAR_VARIABLE_SET: {"value": 12.5},
}


def _operation_terms(operation: AppFamilyOperation):
    return next(item for item in APP_FAMILY_OPERATION_TERMS if item.operation is operation)


def _graph(
    operation: AppFamilyOperation,
    *,
    configuration: object | None = None,
    operation_definition: str | None = None,
) -> ParametricFeatureGraphV2:
    selected = _operation_terms(operation)
    terms = list(APP_FAMILY_PFG_TERMS)
    if operation_definition is not None:
        index = terms.index(selected.operation_term)
        terms[index] = dataclasses.replace(
            selected.operation_term,
            term_definition_sha256=operation_definition,
        )
    parameter = DesignParameterV2(
        parameter_id="parameter_configuration",
        name="Bounded application-object configuration",
        semantic_role_term_ref_id=APP_FAMILY_CONFIGURATION_ROLE_TERM.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id="value_configuration",
            value_type_term_ref_id=APP_FAMILY_CONFIGURATION_TYPE_TERM.term_ref_id,
            encoding_term_ref_id=APP_FAMILY_CANONICAL_JSON_TERM.term_ref_id,
            value=(CONFIGURATIONS[operation] if configuration is None else configuration),
        ),
    )
    relation_kind = APP_FAMILY_RELATION_KINDS[operation]
    ports = [
        FeatureInputPortV2(
            port_id="port_configuration",
            semantic_role_term_ref_id=APP_FAMILY_CONFIGURATION_ROLE_TERM.term_ref_id,
            value_type_term_ref_id=APP_FAMILY_CONFIGURATION_TYPE_TERM.term_ref_id,
            minimum_cardinality=1,
            maximum_cardinality=1,
            ordered=False,
        )
    ]
    dependencies = []
    source = None
    if relation_kind is not AppFamilyRelationKind.NONE:
        ports.append(
            FeatureInputPortV2(
                port_id="port_related",
                semantic_role_term_ref_id=APP_FAMILY_RELATED_OBJECT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=APP_FAMILY_RELATED_OBJECT_TYPE_TERM.term_ref_id,
                minimum_cardinality=1,
                maximum_cardinality=1,
                ordered=False,
            )
        )
        dependencies.append(
            FeatureDependencyV2(
                dependency_id="dependency_related",
                port_id="port_related",
                upstream_node_id="node_related",
                upstream_result_id="result_related",
            )
        )
        source = FeatureNodeV2(
            node_id="node_related",
            body_id="document_space",
            name="Authenticated existing document object",
            intent=FeatureIntentV2(
                structural_kind_term_ref_id=APP_FAMILY_SOURCE_STRUCTURE_TERM.term_ref_id,
                family_term_ref_id=APP_FAMILY_SOURCE_FAMILY_TERM.term_ref_id,
                operation_term_ref_id=APP_FAMILY_SOURCE_OPERATION_TERM.term_ref_id,
            ),
            results=(
                FeatureResultV2(
                    result_id="result_related",
                    semantic_role_term_ref_id=APP_FAMILY_SOURCE_RESULT_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=APP_FAMILY_SOURCE_RESULT_TYPE_TERM.term_ref_id,
                ),
            ),
        )
    target = FeatureNodeV2(
        node_id="node_target",
        body_id="document_space",
        name="Backend-neutral application document object",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=APP_FAMILY_STRUCTURE_TERM.term_ref_id,
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
    nodes = (target,) if source is None else (source, target)
    return ParametricFeatureGraphV2(
        graph_id=f"graph_app_{operation.value}",
        name="Application document-object graph",
        terms=tuple(terms),
        bodies=(FeatureBodyV2(body_id="document_space", name="Document space"),),
        parameters=(parameter,),
        references=(),
        nodes=nodes,
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
        namespace="org.vibecad.app-family-proof-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"proof:{term_id}"),
    )


RULE = _proof_term("rule_app_family_target", "rule.app-family-target-reviewed")
PREDICATE = _proof_term("predicate_app_family_target", "predicate.app-family-target-reviewed")
ROLE_PREMISE = _proof_term("role_app_family_candidate", "proof-role.app-family-candidate")
ROLE_CONCLUSION = _proof_term("role_app_family_validated", "proof-role.app-family-validated")
APP_STRUCTURE_BRIDGE = _bridge_from_pfg(APP_FAMILY_STRUCTURE_TERM)


def _subject() -> SubjectRef:
    return SubjectRef(
        artifact_id="artifact_pfg",
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id="node_target",
    )


class _AppFamilyEvaluator:
    def __init__(self) -> None:
        def signature(role: BridgeTermRef) -> RuleEndpointSignature:
            return RuleEndpointSignature(
                selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
                role_term=role,
                subject_type_term=APP_STRUCTURE_BRIDGE,
            )

        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="app_family_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("app-family-target-evaluator-v1"),
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
            role_term_ref_id=APP_FAMILY_INTENT_DOCUMENT_ROLE_TERM.term_ref_id,
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
            APP_STRUCTURE_BRIDGE,
            APP_FAMILY_INTENT_DOCUMENT_ROLE_TERM,
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_app_family_target",
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
                producer_id="app_family_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("app-family-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("upstream-app-family-compile-request"),
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
    max_output_bytes: int = MAX_APP_FAMILY_PLAN_BYTES,
) -> tuple[BackendLoweringRequest, _Reader, TrustedRulePolicy]:
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = build_app_family_capability_document()
    policy = TrustedRulePolicy(evaluators=(_AppFamilyEvaluator(),))
    request = BackendLoweringRequest(
        adapter=FREECAD_APP_FAMILY_ADAPTER_DESCRIPTOR,
        terms=tuple((*APP_FAMILY_REQUEST_TERMS, RULE, PREDICATE, ROLE_PREMISE, ROLE_CONCLUSION)),
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
    adapter: FreeCADAppFamilyAdapter,
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


@pytest.mark.parametrize("operation", tuple(AppFamilyOperation))
def test_shared_adapter_lowers_exact_ten_specs_deterministically(
    operation: AppFamilyOperation,
) -> None:
    request, reader, policy = _request(_graph(operation))
    sink = _MemoryPlanSink()
    adapter = FreeCADAppFamilyAdapter(sink)
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    repeated_result, repeated_receipt = _lower(adapter, request, reader, policy)
    repeated_plan, repeated_payload = adapter.read_plan(repeated_receipt)

    assert plan.operation is operation
    assert plan.configuration == CONFIGURATIONS[operation]
    assert result.plan_document.document_digest == plan.plan_sha256
    assert result.plan_document.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert repeated_result == result and repeated_receipt == receipt
    assert repeated_plan == plan and repeated_payload == payload
    assert result.supported_subjects == (_subject(),)
    assert len(sink.items) == 1
    assert not adapter.executable and not plan.executable
    assert not adapter.grants_execution_authority and not receipt.grants_execution_authority
    assert b"App::" not in payload
    assert b"python" not in payload.lower() and b"expression" not in payload.lower()


def test_unknown_identity_and_sink_failure_publish_nothing() -> None:
    graph = _graph(
        AppFamilyOperation.TEXT_ANNOTATION,
        operation_definition=_sha("substituted operation definition"),
    )
    request, reader, policy = _request(graph)
    sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADAppFamilyAdapter(sink), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    graph = _graph(AppFamilyOperation.TEXT_ANNOTATION)
    unused = _operation_terms(AppFamilyOperation.TEXT_DOCUMENT).operation_term
    rebound_terms = tuple(
        dataclasses.replace(item, term_ref_id="rebound_unused_operation")
        if item == unused
        else item
        for item in graph.terms
    )
    request, reader, policy = _request(dataclasses.replace(graph, terms=rebound_terms))
    sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADAppFamilyAdapter(sink), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}

    request, reader, policy = _request(_graph(AppFamilyOperation.TEXT_DOCUMENT))
    sink = _MemoryPlanSink(fail=True)
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADAppFamilyAdapter(sink), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert sink.items == {}


def test_text_n_n_plus_one_plan_tamper_and_output_budget() -> None:
    exact_lines = ["x" * 512] * MAX_ANNOTATION_LINES
    request, reader, policy = _request(
        _graph(
            AppFamilyOperation.TEXT_ANNOTATION,
            configuration={"lines": exact_lines, "position_mm": [0.0, 0.0, 0.0]},
        )
    )
    adapter = FreeCADAppFamilyAdapter(_MemoryPlanSink())
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    assert plan.configuration["lines"] == exact_lines

    with pytest.raises(AppFamilyRuleError):
        encode_app_family_configuration(
            AppFamilyOperation.TEXT_ANNOTATION,
            {"lines": ["x" * 513], "position_mm": [0.0, 0.0, 0.0]},
        )
    with pytest.raises(AppFamilyRuleError):
        encode_app_family_configuration(
            AppFamilyOperation.TEXT_ANNOTATION,
            {
                "lines": ["x"] * (MAX_ANNOTATION_LINES + 1),
                "position_mm": [0.0, 0.0, 0.0],
            },
        )
    with pytest.raises(AppFamilyRuleError):
        decode_app_family_backend_plan(
            payload + b" ",
            expected_content_sha256=result.plan_document.content_sha256,
            expected_plan_sha256=plan.plan_sha256,
        )
    duplicate = payload.replace(b'{"authority":', b'{"authority":"none","authority":', 1)
    with pytest.raises(AppFamilyRuleError):
        decode_app_family_backend_plan(duplicate)

    request, reader, policy = _request(
        _graph(AppFamilyOperation.PLACEMENT_REFERENCE), max_output_bytes=1
    )
    with pytest.raises(IntentBridgeError) as caught:
        _lower(FreeCADAppFamilyAdapter(_MemoryPlanSink()), request, reader, policy)
    assert caught.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED


def test_inventory_counts_ten_user_semantics_and_excludes_two_helpers() -> None:
    assert tuple(item.operation_id for item in APP_FAMILY_OPERATION_SPECS) == tuple(
        item.value for item in AppFamilyOperation
    )
    assert tuple(item.native_type_id for item in APP_FAMILY_OPERATION_SPECS) == tuple(
        APP_FAMILY_NATIVE_TYPE_IDS[item] for item in AppFamilyOperation
    )
    assert APP_FAMILY_EXCLUDED_CANDIDATES == {
        "App::LinkElement": "generated-helper-and-duplicate-single-link-semantics",
        "App::LocalCoordinateSystem": (
            "base-of-reviewed-Part::LocalCoordinateSystem-same-user-semantics"
        ),
    }
    assert set(item.native_type_id for item in APP_FAMILY_OPERATION_SPECS).isdisjoint(
        APP_FAMILY_EXCLUDED_CANDIDATES
    )


@pytest.mark.parametrize(
    ("operation", "accepted", "rejected"),
    (
        (
            AppFamilyOperation.TEXT_DOCUMENT,
            {"text": "x" * 4096},
            {"text": "x" * 4097},
        ),
        (
            AppFamilyOperation.MATERIAL_DEFINITION,
            {"name": "m", "description": "x" * 512, "density_kg_m3": 1e9},
            {"name": "m", "description": "x" * 513, "density_kg_m3": 1e9},
        ),
        (
            AppFamilyOperation.SCALAR_VARIABLE_SET,
            {"value": 1e12},
            {"value": 1e12 + 1},
        ),
        (
            AppFamilyOperation.PLACEMENT_REFERENCE,
            {
                "placement": {
                    "position_mm": [1e6, -1e6, 0.0],
                    "axis": [0.0, 0.0, 1.0],
                    "angle_degrees": 360.0,
                }
            },
            {
                "placement": {
                    "position_mm": [1e6 + 1, 0.0, 0.0],
                    "axis": [0.0, 0.0, 1.0],
                    "angle_degrees": 0.0,
                }
            },
        ),
    ),
)
def test_bounded_configuration_n_n_plus_one(
    operation: AppFamilyOperation,
    accepted: object,
    rejected: object,
) -> None:
    assert encode_app_family_configuration(operation, accepted)
    with pytest.raises(AppFamilyRuleError):
        encode_app_family_configuration(operation, rejected)


@pytest.mark.slow
def test_real_freecad_app_family_batch_create_edit_reopen_and_rollback(
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
    for index, operation in enumerate(AppFamilyOperation):
        request, reader, policy = _request(_graph(operation))
        adapter = FreeCADAppFamilyAdapter(_MemoryPlanSink())
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
                "related_node_id": plan.related_node_id,
                "related_result_id": plan.related_result_id,
            }
        )
    source_root = Path(__file__).parents[1] / "src"
    output_root = tmp_path / "freecad-app-family"
    output_root.mkdir()
    relation_operations = tuple(
        key.value
        for key, value in APP_FAMILY_RELATION_KINDS.items()
        if value is not AppFamilyRelationKind.NONE
    )
    code = f"""
import os, sys
sys.path.insert(0, os.path.join(sys.prefix, 'lib'))
sys.path.insert(0, {str(source_root)!r})
from pathlib import Path
import FreeCAD
from vibecad.parametric.freecad_app_family_rules import (
    AppFamilyExecutionBindings,
    AppFamilyRuleError,
    apply_app_family_plan,
)

CASES = {cases!r}
OUTPUT_ROOT = Path({str(output_root)!r})
TYPE_IDS = {dict((key.value, value) for key, value in APP_FAMILY_NATIVE_TYPE_IDS.items())!r}
RELATION_OPS = {relation_operations!r}

def snapshot(document):
    objects = tuple(document.Objects)
    groups = tuple(
        (item, tuple(item.Group)) for item in objects if 'Group' in tuple(item.PropertiesList))
    links = tuple(
        (item, item.LinkedObject) for item in objects
        if 'LinkedObject' in tuple(item.PropertiesList))
    elements = tuple(
        (item, tuple(item.ElementList)) for item in objects
        if 'ElementList' in tuple(item.PropertiesList))
    return objects, groups, links, elements, bool(document.HasPendingTransaction)

def same_identity(left, right):
    return len(tuple(left)) == len(right) and all(
        a is b for a, b in zip(tuple(left), right, strict=True))

def same_snapshot(document, before):
    objects, groups, links, elements, pending = before
    return (
        same_identity(document.Objects, objects)
        and all(same_identity(item.Group, members) for item, members in groups)
        and all(item.LinkedObject is target for item, target in links)
        and all(same_identity(item.ElementList, members) for item, members in elements)
        and bool(document.HasPendingTransaction) is pending
    )

persisted = []
for index, entry in enumerate(CASES):
    document = FreeCAD.newDocument('AppFamily' + str(index))
    document.UndoMode = 1
    related = None
    if entry['operation'] in RELATION_OPS:
        related_type = 'Part::Box' if entry['operation'] == 'object_link' else 'Part::Feature'
        related = document.addObject(related_type, 'Related')
        document.recompute()
    bindings = AppFamilyExecutionBindings(
        document=document,
        container_id=entry['container_id'],
        related_node_id=entry['related_node_id'],
        related_result_id=entry['related_result_id'],
        related_object=related,
    )
    payload = Path(entry['path']).read_bytes()
    before = snapshot(document)
    try:
        apply_app_family_plan(
            payload + b' ',
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=bindings,
        )
        raise AssertionError('tamper accepted')
    except AppFamilyRuleError:
        assert same_snapshot(document, before)
    receipt = apply_app_family_plan(
        payload,
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=bindings,
    )
    feature = document.getObject(receipt.object_name)
    assert feature.TypeId == TYPE_IDS[entry['operation']] and feature.isValid()
    assert len(receipt.owned_object_names) == (9 if entry['operation'] == 'positioned_part' else 1)
    if entry['operation'] == 'text_annotation':
        feature.LabelText = ['edited']
        feature.Position = FreeCAD.Vector(8, 9, 10)
    elif entry['operation'] == 'leader_annotation':
        feature.LabelText = ['edited leader']
        feature.TextPosition = FreeCAD.Vector(8, 9, 10)
    elif entry['operation'] == 'document_group':
        second = document.addObject('Part::Feature', 'Second')
        feature.addObject(second)
    elif entry['operation'] == 'object_link':
        before_x = float(feature.Shape.BoundBox.XMin)
        related.Placement.Base.x = 10
        document.recompute()
        assert abs(float(feature.Shape.BoundBox.XMin) - before_x) > 1.0
        feature.Placement.Base.y = 8
    elif entry['operation'] == 'link_group':
        second = document.addObject('Part::Feature', 'Second')
        feature.setLink([related, second])
        feature.Placement.Base.y = 8
    elif entry['operation'] == 'material_definition':
        feature.Material = {{
            'Name': 'Edited',
            'Description': 'still bounded',
            'Density': '7800 kg/m^3',
        }}
    elif entry['operation'] == 'positioned_part':
        feature.Placement.Base.x = 11
        document.recompute()
        assert abs(float(related.getGlobalPlacement().Base.x) - 11.0) < 1e-9
    elif entry['operation'] == 'placement_reference':
        consumer = document.addObject('Part::Feature', 'Consumer')
        consumer.setExpression('Placement.Base.x', feature.Name + '.Placement.Base.x')
        feature.Placement.Base.x = 12
        document.recompute()
        assert abs(float(consumer.Placement.Base.x) - 12.0) < 1e-9
    elif entry['operation'] == 'text_document':
        feature.Text = 'Edited bounded text'
    elif entry['operation'] == 'scalar_variable_set':
        consumer = document.addObject('Part::Feature', 'Consumer')
        consumer.addProperty('App::PropertyFloat', 'Observed')
        consumer.setExpression('Observed', feature.Name + '.Value')
        feature.Value = 25.0
        document.recompute()
        assert abs(float(consumer.Observed) - 25.0) < 1e-9
    document.recompute()
    path = OUTPUT_ROOT / f'{{index}}.FCStd'
    document.saveAs(str(path))
    persisted.append((path, receipt.object_name, entry['operation']))
    FreeCAD.closeDocument(document.Name)

for path, object_name, operation in persisted:
    reopened = FreeCAD.openDocument(str(path))
    feature = reopened.getObject(object_name)
    assert feature is not None and feature.TypeId == TYPE_IDS[operation] and feature.isValid()
    if operation == 'text_annotation':
        assert tuple(feature.LabelText) == ('edited',)
    elif operation == 'document_group':
        assert len(tuple(feature.Group)) == 2
    elif operation == 'object_link':
        assert feature.LinkedObject is reopened.getObject('Related')
        assert abs(float(reopened.getObject('Related').Placement.Base.x) - 10.0) < 1e-9
        assert not feature.Shape.isNull()
    elif operation == 'link_group':
        assert len(tuple(feature.ElementList)) == 2
    elif operation == 'positioned_part':
        assert len(tuple(feature.Origin.OriginFeatures)) == 7
        assert reopened.getObject('Related').getParentGeoFeatureGroup() is feature
    elif operation == 'text_document':
        assert feature.Text == 'Edited bounded text'
    elif operation == 'scalar_variable_set':
        assert abs(float(feature.Value) - 25.0) < 1e-9
        assert abs(float(reopened.getObject('Consumer').Observed) - 25.0) < 1e-9
    FreeCAD.closeDocument(reopened.Name)

# Pre-mutation relation authentication rejects a foreign-document object.
entry = next(item for item in CASES if item['operation'] == 'document_group')
document = FreeCAD.newDocument('AppFamilyPreReject')
foreign = FreeCAD.newDocument('AppFamilyForeign')
related = foreign.addObject('Part::Feature', 'ForeignRelated')
before = snapshot(document)
try:
    apply_app_family_plan(
        Path(entry['path']).read_bytes(),
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=AppFamilyExecutionBindings(
            document=document,
            container_id=entry['container_id'],
            related_node_id=entry['related_node_id'],
            related_result_id=entry['related_result_id'],
            related_object=related,
        ),
    )
    raise AssertionError('foreign relation accepted')
except AppFamilyRuleError:
    assert same_snapshot(document, before)
FreeCAD.closeDocument(foreign.Name)
FreeCAD.closeDocument(document.Name)

# A real observer violates root ownership only after object creation.
class LateOwnershipObserver:
    def __init__(self, group):
        self.group = group
    def slotCreatedObject(self, item):
        if item.TypeId == 'App::Annotation':
            self.group.addObject(item)

entry = next(item for item in CASES if item['operation'] == 'text_annotation')
document = FreeCAD.newDocument('AppFamilyLateRollback')
document.UndoMode = 1
group = document.addObject('App::DocumentObjectGroup', 'GuardGroup')
observer = LateOwnershipObserver(group)
FreeCAD.addDocumentObserver(observer)
before = snapshot(document)
try:
    try:
        apply_app_family_plan(
            Path(entry['path']).read_bytes(),
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=AppFamilyExecutionBindings(
                document=document, container_id=entry['container_id']),
        )
        raise AssertionError('late ownership violation accepted')
    except AppFamilyRuleError:
        assert same_snapshot(document, before)
finally:
    FreeCAD.removeDocumentObserver(observer)
FreeCAD.closeDocument(document.Name)

# A relation created by an observer would make the requested group edge cyclic.
class LateCycleObserver:
    def __init__(self, related):
        self.related = related
    def slotCreatedObject(self, item):
        if item.TypeId == 'App::DocumentObjectGroup':
            self.related.addObject(item)

entry = next(item for item in CASES if item['operation'] == 'document_group')
document = FreeCAD.newDocument('AppFamilyCycleRollback')
document.UndoMode = 1
related = document.addObject('App::DocumentObjectGroup', 'Related')
observer = LateCycleObserver(related)
FreeCAD.addDocumentObserver(observer)
before = snapshot(document)
try:
    try:
        apply_app_family_plan(
            Path(entry['path']).read_bytes(),
            expected_content_sha256=entry['content_sha256'],
            expected_plan_sha256=entry['plan_sha256'],
            bindings=AppFamilyExecutionBindings(
                document=document,
                container_id=entry['container_id'],
                related_node_id=entry['related_node_id'],
                related_result_id=entry['related_result_id'],
                related_object=related,
            ),
        )
        raise AssertionError('cycle accepted')
    except AppFamilyRuleError:
        assert same_snapshot(document, before)
finally:
    FreeCAD.removeDocumentObserver(observer)
FreeCAD.closeDocument(document.Name)
print('APP_FAMILY_BATCH_OK')
"""
    completed = subprocess.run(
        [str(runtime_python), "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "APP_FAMILY_BATCH_OK" in completed.stdout
