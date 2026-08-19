"""Private PFGv2 lowering for reviewed FreeCAD Part curves and paths.

All nine operations use the shared reviewed-family engine.  This file owns only
the family ontology and PFG-to-plan semantics; exact proof/capability checks,
canonical plan publication, receipts, and readback stay in the G0 seam.
"""

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
    ParametricFeatureGraphV2,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_part_curve_rules import (
    MAX_PART_CURVE_PLAN_BYTES,
    PART_CURVE_FREECAD_ENGINE_BUILD_ID,
    PART_CURVE_NATIVE_SPECS,
    PART_CURVE_PLAN_MEDIA_TYPE,
    PART_CURVE_RULE_CONTRACT_SHA256,
    PART_CURVE_RULE_ID,
    PartCurveBackendPlan,
    PartCurveOperation,
    PartCurveParameterSet,
    PartCurveRuleError,
    decode_part_curve_backend_plan,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.freecad-part"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.freecad-part-ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.freecad-part-curve-adapter.v1\0"
_FREECAD_BUILD_DESCRIPTOR_SHA256 = hashlib.sha256(
    b"FreeCAD\0" + b"1.1.0\0" + PART_CURVE_FREECAD_ENGINE_BUILD_ID.encode("ascii")
).hexdigest()


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


PART_CURVE_INTENT_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_part_curve_intent", "document-role.part-curve-intent"
)
PART_CURVE_CAPABILITY_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_part_curve_capability", "document-role.part-curve-capability"
)
PART_CURVE_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_freecad_part_curve_capability_v1",
    "document-schema.part-curve-capability-v1",
)
PART_CURVE_PLAN_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_part_curve_plan", "document-role.part-curve-plan"
)
PART_CURVE_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_freecad_part_curve_plan_v1", "document-schema.part-curve-plan-v1"
)

PART_CURVE_STRUCTURE_TERM: Final = _pfg_term("structure_part_object", "structure.part-object")
PART_CURVE_FAMILY_TERM: Final = _pfg_term(
    "family_part_curve_path", "feature-family.part-curve-path"
)
PART_CURVE_PARAMETERS_ROLE_TERM: Final = _pfg_term(
    "role_part_curve_parameters", "input-role.part-curve-parameters"
)
PART_CURVE_PARAMETERS_TYPE_TERM: Final = _pfg_term(
    "type_part_curve_parameter_set", "value-type.part-curve-parameter-set"
)
PART_CURVE_CANONICAL_JSON_TERM: Final = _pfg_term(
    "encoding_canonical_json", "value-encoding.canonical-json"
)
PART_CURVE_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_part_geometry_result", "result-role.part-geometry"
)
PART_CURVE_RESULT_TYPE_TERM: Final = _pfg_term("type_part_geometry", "value-type.part-geometry")


@dataclass(frozen=True, slots=True)
class PartCurveOperationTerms:
    operation: PartCurveOperation
    operation_term: SemanticTermRefV2


PART_CURVE_OPERATION_TERMS: Final = tuple(
    PartCurveOperationTerms(
        operation,
        _pfg_term(
            f"operation_part_{operation.value}",
            f"operation.part-{operation.value.replace('_', '-')}",
        ),
    )
    for operation in PartCurveOperation
)

PART_CURVE_PFG_TERMS: Final = (
    PART_CURVE_STRUCTURE_TERM,
    PART_CURVE_FAMILY_TERM,
    PART_CURVE_PARAMETERS_ROLE_TERM,
    PART_CURVE_PARAMETERS_TYPE_TERM,
    PART_CURVE_CANONICAL_JSON_TERM,
    PART_CURVE_RESULT_ROLE_TERM,
    PART_CURVE_RESULT_TYPE_TERM,
    *(item.operation_term for item in PART_CURVE_OPERATION_TERMS),
)

PART_CURVE_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PART_CURVE_INTENT_DOCUMENT_ROLE_TERM,
    PART_CURVE_CAPABILITY_DOCUMENT_ROLE_TERM,
    PART_CURVE_CAPABILITY_SCHEMA_TERM,
    PART_CURVE_PLAN_DOCUMENT_ROLE_TERM,
    PART_CURVE_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in PART_CURVE_PFG_TERMS),
)

_ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            PART_CURVE_RULE_ID.encode("ascii"),
            PART_CURVE_RULE_CONTRACT_SHA256.encode("ascii"),
            b"pfg-v2;one-part-object;one-parameter-set;shared-reviewed-family-engine;no-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in (*PART_CURVE_REQUEST_TERMS, PFG_SELECTOR_FEATURE_NODE)
            ),
        )
    )
).hexdigest()

FREECAD_PART_CURVE_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_part_curve_adapter",
    adapter_version="1.0.0",
    adapter_contract_sha256=_ADAPTER_CONTRACT_SHA256,
)

PART_CURVE_REVIEWED_OPERATIONS: Final = tuple(
    ReviewedOperationSpec(
        operation_id=item.operation.value,
        semantic_term=_as_bridge(item.operation_term),
        native_type_id=PART_CURVE_NATIVE_SPECS[item.operation].type_id,
        native_operation=item.operation.value,
        native_property_names=tuple(
            {
                "Placement",
                *(
                    parameter.property_name
                    for parameter in PART_CURVE_NATIVE_SPECS[item.operation].parameters
                ),
                *(name for name, _ in PART_CURVE_NATIVE_SPECS[item.operation].fixed_properties),
            }
        ),
    )
    for item in PART_CURVE_OPERATION_TERMS
)

PART_CURVE_MANIFEST: Final = FamilyBatchManifest(
    family_id="freecad_part_curve_path",
    family_version="1.0.0",
    adapter=FREECAD_PART_CURVE_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=_FREECAD_BUILD_DESCRIPTOR_SHA256,
    rule_id=PART_CURVE_RULE_ID,
    rule_contract_sha256=PART_CURVE_RULE_CONTRACT_SHA256,
    intent_role_term=PART_CURVE_INTENT_DOCUMENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=PART_CURVE_CAPABILITY_DOCUMENT_ROLE_TERM,
    capability_schema_term=PART_CURVE_CAPABILITY_SCHEMA_TERM,
    capability_media_type="application/vnd.vibecad.freecad-part-curve-capability+json",
    plan_role_term=PART_CURVE_PLAN_DOCUMENT_ROLE_TERM,
    plan_schema_term=PART_CURVE_PLAN_SCHEMA_TERM,
    plan_media_type=PART_CURVE_PLAN_MEDIA_TYPE,
    request_terms=PART_CURVE_REQUEST_TERMS,
    operations=PART_CURVE_REVIEWED_OPERATIONS,
    max_plan_bytes=MAX_PART_CURVE_PLAN_BYTES,
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


def _matches(
    terms: dict[str, SemanticTermRefV2],
    term_ref_id: str,
    expected: SemanticTermRefV2,
) -> bool:
    actual = terms.get(term_ref_id)
    return actual is not None and _identity(actual) == _identity(expected)


def _operation_for_node(
    node: FeatureNodeV2,
    terms: dict[str, SemanticTermRefV2],
) -> PartCurveOperationTerms | None:
    if not _matches(
        terms, node.intent.structural_kind_term_ref_id, PART_CURVE_STRUCTURE_TERM
    ) or not _matches(terms, node.intent.family_term_ref_id, PART_CURVE_FAMILY_TERM):
        return None
    operation = terms.get(node.intent.operation_term_ref_id)
    matches = tuple(
        item
        for item in PART_CURVE_OPERATION_TERMS
        if operation is not None and _identity(operation) == _identity(item.operation_term)
    )
    return matches[0] if len(matches) == 1 else None


def _assert_closed_graph(graph: ParametricFeatureGraphV2) -> None:
    if (
        graph.extensions
        or graph.references
        or any(item.extension_ids for item in graph.bodies)
        or any(
            item.extension_ids or item.value.extension_ids or item.expression is not None
            for item in graph.parameters
        )
        or any(
            node.extension_ids
            or node.intent.extension_ids
            or node.intent.dependencies
            or node.intent.references
            or any(port.extension_ids for port in node.intent.input_ports)
            or any(result.extension_ids for result in node.results)
            for node in graph.nodes
        )
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/extensions")


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
        if (
            graph.graph_id != document.document_id
            or len(graph.bodies) != 1
            or len(graph.nodes) != 1
            or len(graph.parameters) != 1
            or len(graph.graph_results) != 1
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")
        _assert_closed_graph(graph)
        terms = {item.term_ref_id: item for item in graph.terms}
        if len(graph.terms) != len(PART_CURVE_PFG_TERMS) or any(
            sum(_identity(item) == _identity(expected) for item in graph.terms) != 1
            for expected in PART_CURVE_PFG_TERMS
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/terms")
        node = graph.nodes[0]
        body = graph.bodies[0]
        operation_terms = _operation_for_node(node, terms)
        if operation_terms is None or node.body_id != body.body_id:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/target")
        if (
            len(node.intent.input_ports) != 1
            or len(node.intent.parameter_bindings) != 1
            or len(node.results) != 1
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/shape")
        port = node.intent.input_ports[0]
        binding = node.intent.parameter_bindings[0]
        parameter = graph.parameters[0]
        result = node.results[0]
        selection = graph.graph_results[0]
        if (
            not _matches(terms, port.semantic_role_term_ref_id, PART_CURVE_PARAMETERS_ROLE_TERM)
            or not _matches(terms, port.value_type_term_ref_id, PART_CURVE_PARAMETERS_TYPE_TERM)
            or port.minimum_cardinality != 1
            or port.maximum_cardinality != 1
            or port.ordered
            or binding.port_id != port.port_id
            or binding.parameter_id != parameter.parameter_id
            or binding.ordinal != 0
            or not _matches(
                terms,
                parameter.semantic_role_term_ref_id,
                PART_CURVE_PARAMETERS_ROLE_TERM,
            )
            or not _matches(
                terms,
                parameter.value.value_type_term_ref_id,
                PART_CURVE_PARAMETERS_TYPE_TERM,
            )
            or not _matches(
                terms,
                parameter.value.encoding_term_ref_id,
                PART_CURVE_CANONICAL_JSON_TERM,
            )
            or not _matches(terms, result.semantic_role_term_ref_id, PART_CURVE_RESULT_ROLE_TERM)
            or not _matches(terms, result.value_type_term_ref_id, PART_CURVE_RESULT_TYPE_TERM)
            or selection.node_id != node.node_id
            or selection.result_id != result.result_id
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/bindings")
        parameters = PartCurveParameterSet.from_value(
            operation_terms.operation, parameter.value.value
        )
        operation = manifest.operation_for_term(_as_bridge(operation_terms.operation_term))
        if operation is None:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/operation")
        plan = PartCurveBackendPlan(
            source_artifact_id=document.artifact_id,
            source_graph_id=graph.graph_id,
            source_graph_sha256=graph.graph_sha256,
            source_content_sha256=hashlib.sha256(payload).hexdigest(),
            lowering_request_sha256=request_digest,
            adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
            manifest_sha256=manifest.manifest_sha256,
            operation_specification_sha256=operation.specification_sha256,
            body_id=body.body_id,
            node_id=node.node_id,
            result_id=result.result_id,
            parameter_id=parameter.parameter_id,
            value_id=parameter.value.value_id,
            operation=operation_terms.operation,
            parameters=parameters,
        )
        subject = SubjectRef(
            artifact_id=document.artifact_id,
            selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
            selector_id=node.node_id,
        )
        return ReviewedPlanDraft(
            payload=plan.canonical_bytes,
            semantic_plan_sha256=plan.plan_sha256,
            operation_term=_as_bridge(operation_terms.operation_term),
            subjects=(subject,),
        )
    except IntentBridgeError:
        raise
    except PartCurveRuleError:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/parameters")
    except ParametricFeatureGraphError:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_document")
    except (Exception, SystemExit):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_document")


def _decode_plan(
    payload: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
) -> PartCurveBackendPlan:
    return decode_part_curve_backend_plan(
        payload,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )


def _validate_binding(
    decoded: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    if type(decoded) is not PartCurveBackendPlan:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_document")
    plan = decoded
    if (
        plan.operation.value != operation.operation_id
        or plan.operation_specification_sha256 != operation.specification_sha256
        or plan.manifest_sha256 != receipt.manifest_sha256
        or plan.lowering_request_sha256 != receipt.request_digest
        or plan.adapter_contract_sha256 != receipt.adapter.adapter_contract_sha256
        or plan.source_artifact_id != receipt.source_document.artifact_id
        or plan.source_graph_id != receipt.source_document.document_id
        or plan.source_graph_sha256 != receipt.source_document.document_digest
        or plan.source_content_sha256 != receipt.source_document.content_sha256
    ):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/receipt/binding")


def build_part_curve_capability_document(
    *,
    artifact_id: str = "artifact_freecad_part_curve_capability",
) -> tuple[DocumentRef, bytes]:
    return PART_CURVE_MANIFEST.capability_document(artifact_id=artifact_id)


class FreeCADPartCurveAdapter(ExactReviewedFamilyAdapter):
    """Exact shared-engine adapter for all nine reviewed Part curve semantics."""

    __slots__ = ()

    def __init__(self, sink: PlanSink) -> None:
        super().__init__(
            PART_CURVE_MANIFEST,
            sink,
            build_plan=_build_plan,
            decode_plan=_decode_plan,
            validate_binding=_validate_binding,
        )


__all__ = [
    "FREECAD_PART_CURVE_ADAPTER_DESCRIPTOR",
    "PART_CURVE_CANONICAL_JSON_TERM",
    "PART_CURVE_CAPABILITY_DOCUMENT_ROLE_TERM",
    "PART_CURVE_CAPABILITY_SCHEMA_TERM",
    "PART_CURVE_FAMILY_TERM",
    "PART_CURVE_INTENT_DOCUMENT_ROLE_TERM",
    "PART_CURVE_MANIFEST",
    "PART_CURVE_OPERATION_TERMS",
    "PART_CURVE_PARAMETERS_ROLE_TERM",
    "PART_CURVE_PARAMETERS_TYPE_TERM",
    "PART_CURVE_PFG_TERMS",
    "PART_CURVE_PLAN_DOCUMENT_ROLE_TERM",
    "PART_CURVE_PLAN_SCHEMA_TERM",
    "PART_CURVE_REQUEST_TERMS",
    "PART_CURVE_RESULT_ROLE_TERM",
    "PART_CURVE_RESULT_TYPE_TERM",
    "PART_CURVE_REVIEWED_OPERATIONS",
    "PART_CURVE_STRUCTURE_TERM",
    "FreeCADPartCurveAdapter",
    "PartCurveOperationTerms",
    "build_part_curve_capability_document",
]
