"""Exact PFGv2 lowering for the reviewed standalone FreeCAD Part core family."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

from vibecad.intent_bridge.contracts import (
    AdapterDescriptor,
    BridgeTermRef,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    SubjectRef,
)
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanDraft,
    ReviewedPlanReceipt,
)
from vibecad.parametric.feature_graph_v2 import (
    FeatureNodeV2,
    ParametricFeatureGraphError,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_part_core_rules import (
    MAX_PART_CORE_PLAN_BYTES,
    PART_CORE_FREECAD_ENGINE_BUILD_ID,
    PART_CORE_NATIVE_SPECS,
    PART_CORE_PLAN_MEDIA_TYPE,
    PART_CORE_RULE_CONTRACT_SHA256,
    PART_CORE_RULE_ID,
    PartCoreBackendPlan,
    PartCoreOperation,
    PartCoreParameterSet,
    PartCoreRuleError,
    PartCoreSelection,
    decode_part_core_backend_plan,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.freecad-part"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.freecad-part.ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.freecad-part-core.adapter.v1\0"


def _definition(term_id: str) -> str:
    return hashlib.sha256(
        b"\0".join(
            (
                _ONTOLOGY_DOMAIN,
                _ONTOLOGY_NAMESPACE.encode("ascii"),
                _ONTOLOGY_VERSION.encode("ascii"),
                term_id.encode("utf-8"),
            )
        )
    ).hexdigest()


def _bridge_term(term_ref_id: str, term_id: str) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace=_ONTOLOGY_NAMESPACE,
        vocabulary_version=_ONTOLOGY_VERSION,
        term_id=term_id,
        term_definition_sha256=_definition(term_id),
    )


def _pfg_term(term_ref_id: str, term_id: str) -> SemanticTermRefV2:
    return SemanticTermRefV2(
        term_ref_id=term_ref_id,
        namespace=_ONTOLOGY_NAMESPACE,
        vocabulary_version=_ONTOLOGY_VERSION,
        term_id=term_id,
        term_definition_sha256=_definition(term_id),
    )


def _as_bridge(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


PART_CORE_INTENT_ROLE_TERM: Final = _bridge_term(
    "role_part_core_intent", "document-role.parametric-intent"
)
PART_CORE_CAPABILITY_ROLE_TERM: Final = _bridge_term(
    "role_part_core_capability", "document-role.freecad-part-core-capability"
)
PART_CORE_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_part_core_capability_v1", "document-schema.freecad-part-core-capability-v1"
)
PART_CORE_PLAN_ROLE_TERM: Final = _bridge_term(
    "role_part_core_backend_plan", "document-role.freecad-backend-plan"
)
PART_CORE_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_part_core_plan_v1", "document-schema.freecad-part-core-plan-v1"
)

PART_CORE_STRUCTURE_TERM: Final = _pfg_term("structure_part_feature", "structure.part-feature")
PART_CORE_SOURCE_STRUCTURE_TERM: Final = _pfg_term("structure_part_source", "structure.part-source")
PART_CORE_SOURCE_FAMILY_TERM: Final = _pfg_term("family_part_source", "feature-family.part-source")
PART_CORE_SOURCE_OPERATION_TERM: Final = _pfg_term("operation_part_source", "operation.part-source")
PART_CORE_SOURCE_ROLE_TERM: Final = _pfg_term("role_part_source_shape", "input-role.source-shape")
PART_CORE_PARAMETERS_ROLE_TERM: Final = _pfg_term(
    "role_part_parameters", "input-role.operation-parameters"
)
PART_CORE_RESULT_ROLE_TERM: Final = _pfg_term("role_part_result_shape", "result-role.shape")
PART_CORE_SHAPE_TYPE_TERM: Final = _pfg_term("type_part_shape", "value-type.shape")
PART_CORE_PARAMETERS_TYPE_TERM: Final = _pfg_term(
    "type_part_parameters", "value-type.part-operation-parameters"
)
PART_CORE_CANONICAL_JSON_TERM: Final = _pfg_term(
    "encoding_part_canonical_json", "value-encoding.canonical-json"
)

_GROUP_BY_OPERATION: Final = {
    PartCoreOperation.BOX: "primitive",
    PartCoreOperation.CONE: "primitive",
    PartCoreOperation.CYLINDER: "primitive",
    PartCoreOperation.ELLIPSOID: "primitive",
    PartCoreOperation.PRISM: "primitive",
    PartCoreOperation.SPHERE: "primitive",
    PartCoreOperation.TORUS: "primitive",
    PartCoreOperation.WEDGE: "primitive",
    PartCoreOperation.CUT: "csg",
    PartCoreOperation.FUSE: "csg",
    PartCoreOperation.COMMON: "csg",
    PartCoreOperation.SECTION: "csg",
    PartCoreOperation.MULTI_FUSE: "aggregate",
    PartCoreOperation.MULTI_COMMON: "aggregate",
    PartCoreOperation.COMPOUND: "aggregate",
    PartCoreOperation.MIRROR: "transform",
    PartCoreOperation.SCALE: "transform",
    PartCoreOperation.REVERSE: "transform",
    PartCoreOperation.REFINE: "transform",
}
_FAMILY_TERMS: Final = {
    group: _pfg_term(f"family_part_{group}", f"feature-family.part-{group}")
    for group in sorted(set(_GROUP_BY_OPERATION.values()))
}


@dataclass(frozen=True, slots=True)
class PartCoreOperationTerms:
    operation: PartCoreOperation
    family_term: SemanticTermRefV2
    operation_term: SemanticTermRefV2


PART_CORE_OPERATION_TERMS: Final = tuple(
    PartCoreOperationTerms(
        operation=operation,
        family_term=_FAMILY_TERMS[_GROUP_BY_OPERATION[operation]],
        operation_term=_pfg_term(
            f"operation_part_{operation.value}",
            f"operation.part-{operation.value.replace('_', '-')}",
        ),
    )
    for operation in PartCoreOperation
)

PART_CORE_PFG_TERMS: Final = (
    PART_CORE_STRUCTURE_TERM,
    PART_CORE_SOURCE_STRUCTURE_TERM,
    PART_CORE_SOURCE_FAMILY_TERM,
    PART_CORE_SOURCE_OPERATION_TERM,
    PART_CORE_SOURCE_ROLE_TERM,
    PART_CORE_PARAMETERS_ROLE_TERM,
    PART_CORE_RESULT_ROLE_TERM,
    PART_CORE_SHAPE_TYPE_TERM,
    PART_CORE_PARAMETERS_TYPE_TERM,
    PART_CORE_CANONICAL_JSON_TERM,
    *(_FAMILY_TERMS[group] for group in sorted(_FAMILY_TERMS)),
    *(item.operation_term for item in PART_CORE_OPERATION_TERMS),
)

PART_CORE_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    PART_CORE_INTENT_ROLE_TERM,
    PART_CORE_CAPABILITY_ROLE_TERM,
    PART_CORE_CAPABILITY_SCHEMA_TERM,
    PART_CORE_PLAN_ROLE_TERM,
    PART_CORE_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in PART_CORE_PFG_TERMS),
)

_ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            PART_CORE_RULE_ID.encode("ascii"),
            PART_CORE_RULE_CONTRACT_SHA256.encode("ascii"),
            b"canonical-pfg-v2;exact-proof;static-native-map;reviewed-family-engine;no-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in PART_CORE_REQUEST_TERMS
            ),
        )
    )
).hexdigest()

FREECAD_PART_CORE_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_part_core_adapter",
    adapter_version="1.0.0",
    adapter_contract_sha256=_ADAPTER_CONTRACT_SHA256,
)

PART_CORE_OPERATION_SPECS: Final = tuple(
    ReviewedOperationSpec(
        operation_id=item.operation.value,
        semantic_term=_as_bridge(item.operation_term),
        native_type_id=PART_CORE_NATIVE_SPECS[item.operation].type_id,
        native_operation=PART_CORE_NATIVE_SPECS[item.operation].native_operation,
        native_property_names=PART_CORE_NATIVE_SPECS[item.operation].property_names,
    )
    for item in PART_CORE_OPERATION_TERMS
)

PART_CORE_MANIFEST: Final = FamilyBatchManifest(
    family_id="freecad_part_core",
    family_version="1.0.0",
    adapter=FREECAD_PART_CORE_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=hashlib.sha256(PART_CORE_FREECAD_ENGINE_BUILD_ID.encode("ascii")).hexdigest(),
    rule_id=PART_CORE_RULE_ID,
    rule_contract_sha256=PART_CORE_RULE_CONTRACT_SHA256,
    intent_role_term=PART_CORE_INTENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=PART_CORE_CAPABILITY_ROLE_TERM,
    capability_schema_term=PART_CORE_CAPABILITY_SCHEMA_TERM,
    capability_media_type="application/vnd.vibecad.freecad-part-core-capability+json",
    plan_role_term=PART_CORE_PLAN_ROLE_TERM,
    plan_schema_term=PART_CORE_PLAN_SCHEMA_TERM,
    plan_media_type=PART_CORE_PLAN_MEDIA_TYPE,
    request_terms=PART_CORE_REQUEST_TERMS,
    operations=PART_CORE_OPERATION_SPECS,
    max_plan_bytes=MAX_PART_CORE_PLAN_BYTES,
)


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


def _identity(term: object) -> tuple[str, str, str, str]:
    try:
        return (
            term.namespace,
            term.vocabulary_version,
            term.term_id,
            term.term_definition_sha256,
        )
    except Exception:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/terms")


def _term_matches(
    terms: dict[str, SemanticTermRefV2],
    term_ref_id: str,
    expected: SemanticTermRefV2,
) -> bool:
    actual = terms.get(term_ref_id)
    return actual is not None and _identity(actual) == _identity(expected)


def _operation_for_node(
    node: FeatureNodeV2, terms: dict[str, SemanticTermRefV2]
) -> PartCoreOperationTerms | None:
    if not _term_matches(terms, node.intent.structural_kind_term_ref_id, PART_CORE_STRUCTURE_TERM):
        return None
    matches = tuple(
        item
        for item in PART_CORE_OPERATION_TERMS
        if _term_matches(terms, node.intent.family_term_ref_id, item.family_term)
        and _term_matches(terms, node.intent.operation_term_ref_id, item.operation_term)
    )
    return matches[0] if len(matches) == 1 else None


def _assert_closed_graph(graph) -> None:
    if graph.extensions or graph.references or any(item.extension_ids for item in graph.bodies):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/extensions")
    if any(
        item.extension_ids or item.value.extension_ids or item.expression is not None
        for item in graph.parameters
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/parameters")
    if any(
        node.extension_ids
        or node.intent.extension_ids
        or node.intent.references
        or any(port.extension_ids for port in node.intent.input_ports)
        or any(result.extension_ids for result in node.results)
        for node in graph.nodes
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/nodes")


def _build_plan(
    document: DocumentRef,
    payload: bytes,
    request_digest: str,
    manifest: FamilyBatchManifest,
) -> ReviewedPlanDraft:
    try:
        graph = decode_parametric_feature_graph_v2(
            payload, expected_sha256=document.document_digest
        )
    except ParametricFeatureGraphError:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_document")
    if (
        graph.graph_id != document.document_id
        or hashlib.sha256(payload).hexdigest() != document.content_sha256
        or len(graph.bodies) != 1
        or len(graph.graph_results) != 1
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")
    _assert_closed_graph(graph)
    terms = {item.term_ref_id: item for item in graph.terms}
    for expected in PART_CORE_PFG_TERMS:
        if sum(_identity(term) == _identity(expected) for term in graph.terms) != 1:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/terms")
    candidates = tuple(
        (node, operation)
        for node in graph.nodes
        if (operation := _operation_for_node(node, terms)) is not None
    )
    if len(candidates) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/target")
    target, operation_terms = candidates[0]
    operation = operation_terms.operation
    native_spec = PART_CORE_NATIVE_SPECS[operation]
    body = graph.bodies[0]
    if target.body_id != body.body_id:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/body")
    port_by_role: dict[str, object] = {}
    for port in target.intent.input_ports:
        if _term_matches(terms, port.semantic_role_term_ref_id, PART_CORE_SOURCE_ROLE_TERM):
            key = "sources"
            expected_type = PART_CORE_SHAPE_TYPE_TERM
            expected_minimum = native_spec.minimum_sources
            expected_maximum = max(1, native_spec.maximum_sources)
            expected_ordered = native_spec.maximum_sources > 1
        elif _term_matches(terms, port.semantic_role_term_ref_id, PART_CORE_PARAMETERS_ROLE_TERM):
            key = "parameters"
            expected_type = PART_CORE_PARAMETERS_TYPE_TERM
            expected_minimum = expected_maximum = 1
            expected_ordered = False
        else:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/input_ports")
        if (
            key in port_by_role
            or not _term_matches(terms, port.value_type_term_ref_id, expected_type)
            or port.minimum_cardinality != expected_minimum
            or port.maximum_cardinality != expected_maximum
            or port.ordered is not expected_ordered
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/input_ports")
        port_by_role[key] = port
    if set(port_by_role) != {"sources", "parameters"}:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/input_ports")
    source_port = port_by_role["sources"]
    parameter_port = port_by_role["parameters"]
    dependencies = tuple(
        item for item in target.intent.dependencies if item.port_id == source_port.port_id
    )
    bindings = tuple(
        item for item in target.intent.parameter_bindings if item.port_id == parameter_port.port_id
    )
    if (
        len(dependencies) != len(target.intent.dependencies)
        or not native_spec.minimum_sources <= len(dependencies) <= native_spec.maximum_sources
        or tuple(item.ordinal for item in dependencies) != tuple(range(len(dependencies)))
        or len(bindings) != 1
        or len(target.intent.parameter_bindings) != 1
        or bindings[0].ordinal != 0
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/bindings")
    nodes = {node.node_id: node for node in graph.nodes}
    sources: list[PartCoreSelection] = []
    for index, dependency in enumerate(dependencies):
        source = nodes.get(dependency.upstream_node_id)
        if (
            source is None
            or source.body_id != body.body_id
            or not _term_matches(
                terms,
                source.intent.structural_kind_term_ref_id,
                PART_CORE_SOURCE_STRUCTURE_TERM,
            )
            or not _term_matches(
                terms, source.intent.family_term_ref_id, PART_CORE_SOURCE_FAMILY_TERM
            )
            or not _term_matches(
                terms,
                source.intent.operation_term_ref_id,
                PART_CORE_SOURCE_OPERATION_TERM,
            )
            or source.intent.input_ports
            or source.intent.dependencies
            or source.intent.references
            or source.intent.parameter_bindings
            or len(source.results) != 1
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, f"/graph/sources/{index}")
        result = source.results[0]
        if (
            result.result_id != dependency.upstream_result_id
            or not _term_matches(
                terms, result.semantic_role_term_ref_id, PART_CORE_RESULT_ROLE_TERM
            )
            or not _term_matches(terms, result.value_type_term_ref_id, PART_CORE_SHAPE_TYPE_TERM)
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, f"/graph/sources/{index}")
        sources.append(PartCoreSelection(node_id=source.node_id, result_id=result.result_id))
    if set(nodes) != {target.node_id, *(item.node_id for item in sources)}:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")
    if len(graph.parameters) != 1 or bindings[0].parameter_id != graph.parameters[0].parameter_id:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/parameters")
    parameter = graph.parameters[0]
    if (
        not _term_matches(
            terms,
            parameter.semantic_role_term_ref_id,
            PART_CORE_PARAMETERS_ROLE_TERM,
        )
        or not _term_matches(
            terms,
            parameter.value.value_type_term_ref_id,
            PART_CORE_PARAMETERS_TYPE_TERM,
        )
        or not _term_matches(
            terms,
            parameter.value.encoding_term_ref_id,
            PART_CORE_CANONICAL_JSON_TERM,
        )
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/parameters")
    try:
        parameters = PartCoreParameterSet.from_value(operation, parameter.value.value)
    except PartCoreRuleError:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/parameters")
    if len(target.results) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/result")
    result = target.results[0]
    selection = graph.graph_results[0]
    if (
        not _term_matches(terms, result.semantic_role_term_ref_id, PART_CORE_RESULT_ROLE_TERM)
        or not _term_matches(terms, result.value_type_term_ref_id, PART_CORE_SHAPE_TYPE_TERM)
        or selection.node_id != target.node_id
        or selection.result_id != result.result_id
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/result")
    operation_spec = manifest.operation_for_term(_as_bridge(operation_terms.operation_term))
    if operation_spec is None:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/operation")
    plan = PartCoreBackendPlan(
        source_artifact_id=document.artifact_id,
        source_graph_id=graph.graph_id,
        source_graph_sha256=graph.graph_sha256,
        source_content_sha256=hashlib.sha256(payload).hexdigest(),
        lowering_request_sha256=request_digest,
        adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
        manifest_sha256=manifest.manifest_sha256,
        operation_specification_sha256=operation_spec.specification_sha256,
        body_id=body.body_id,
        target=PartCoreSelection(node_id=target.node_id, result_id=result.result_id),
        operation=operation,
        sources=tuple(sources),
        parameters=parameters,
    )
    subjects = (
        SubjectRef(
            artifact_id=document.artifact_id,
            selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
            selector_id=target.node_id,
        ),
    )
    return ReviewedPlanDraft(
        payload=plan.canonical_bytes,
        semantic_plan_sha256=plan.plan_sha256,
        operation_term=_as_bridge(operation_terms.operation_term),
        subjects=subjects,
    )


def _validate_binding(
    decoded: object,
    receipt: ReviewedPlanReceipt,
    operation_spec: ReviewedOperationSpec,
) -> None:
    if type(decoded) is not PartCoreBackendPlan:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan")
    operation_terms = next(
        (
            item
            for item in PART_CORE_OPERATION_TERMS
            if item.operation.value == operation_spec.operation_id
        ),
        None,
    )
    if (
        operation_terms is None
        or decoded.operation is not operation_terms.operation
        or decoded.source_artifact_id != receipt.source_document.artifact_id
        or decoded.source_graph_id != receipt.source_document.document_id
        or decoded.source_graph_sha256 != receipt.source_document.document_digest
        or decoded.source_content_sha256 != receipt.source_document.content_sha256
        or decoded.lowering_request_sha256 != receipt.request_digest
        or decoded.adapter_contract_sha256 != receipt.adapter.adapter_contract_sha256
        or decoded.manifest_sha256 != receipt.manifest_sha256
        or decoded.operation_specification_sha256 != operation_spec.specification_sha256
        or decoded.plan_sha256 != receipt.plan_document.document_digest
    ):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan/binding")


def build_part_core_adapter(sink: PlanSink) -> ExactReviewedFamilyAdapter:
    return ExactReviewedFamilyAdapter(
        PART_CORE_MANIFEST,
        sink,
        build_plan=_build_plan,
        decode_plan=decode_part_core_backend_plan,
        validate_binding=_validate_binding,
    )


__all__ = [
    "FREECAD_PART_CORE_ADAPTER_DESCRIPTOR",
    "PART_CORE_CANONICAL_JSON_TERM",
    "PART_CORE_CAPABILITY_ROLE_TERM",
    "PART_CORE_CAPABILITY_SCHEMA_TERM",
    "PART_CORE_INTENT_ROLE_TERM",
    "PART_CORE_MANIFEST",
    "PART_CORE_OPERATION_SPECS",
    "PART_CORE_OPERATION_TERMS",
    "PART_CORE_PARAMETERS_ROLE_TERM",
    "PART_CORE_PARAMETERS_TYPE_TERM",
    "PART_CORE_PFG_TERMS",
    "PART_CORE_PLAN_ROLE_TERM",
    "PART_CORE_PLAN_SCHEMA_TERM",
    "PART_CORE_REQUEST_TERMS",
    "PART_CORE_RESULT_ROLE_TERM",
    "PART_CORE_SHAPE_TYPE_TERM",
    "PART_CORE_SOURCE_FAMILY_TERM",
    "PART_CORE_SOURCE_OPERATION_TERM",
    "PART_CORE_SOURCE_ROLE_TERM",
    "PART_CORE_SOURCE_STRUCTURE_TERM",
    "PART_CORE_STRUCTURE_TERM",
    "PartCoreOperationTerms",
    "build_part_core_adapter",
]
