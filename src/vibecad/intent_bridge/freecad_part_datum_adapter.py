"""Private reviewed lowering for four backend-neutral datum intents.

The PFG vocabulary describes document-root reference geometry and an explicit
placement without naming a CAD backend.  Only the static reviewed operation
table binds complete semantic identities to native FreeCAD types.
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
    ParametricFeatureGraphV2,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_part_datum_rules import (
    MAX_PART_DATUM_PLAN_BYTES,
    PART_DATUM_FREECAD_ENGINE_BUILD_ID,
    PART_DATUM_NATIVE_PROPERTIES,
    PART_DATUM_NATIVE_TYPE_IDS,
    PART_DATUM_PLAN_MEDIA_TYPE,
    PART_DATUM_RULE_CONTRACT_SHA256,
    PART_DATUM_RULE_ID,
    ExplicitDatumPlacement,
    PartDatumBackendPlan,
    PartDatumOperation,
    decode_part_datum_backend_plan,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.parametric-reference"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.parametric-reference-ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.part-datum-adapter.v1\0"
_MANIFEST_BUILD_ID: Final = hashlib.sha256(
    b"FreeCAD-build\0" + PART_DATUM_FREECAD_ENGINE_BUILD_ID.encode("ascii")
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


PART_DATUM_INTENT_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_part_datum_parametric_intent", "document-role.parametric-intent"
)
PART_DATUM_CAPABILITY_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_part_datum_capability", "document-role.reviewed-datum-capability"
)
PART_DATUM_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_part_datum_capability_v1", "document-schema.reviewed-datum-capability-v1"
)
PART_DATUM_PLAN_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_part_datum_backend_plan", "document-role.reviewed-backend-plan"
)
PART_DATUM_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_part_datum_plan_v1", "document-schema.reviewed-part-datum-plan-v1"
)

PART_DATUM_STRUCTURE_TERM: Final = _pfg_term(
    "structure_document_reference", "structure.document-reference-feature"
)
PART_DATUM_FAMILY_TERM: Final = _pfg_term(
    "family_datum_reference", "feature-family.datum-reference"
)
PART_DATUM_LINE_OPERATION_TERM: Final = _pfg_term(
    "operation_datum_line", "operation.datum-line"
)
PART_DATUM_PLANE_OPERATION_TERM: Final = _pfg_term(
    "operation_datum_plane", "operation.datum-plane"
)
PART_DATUM_POINT_OPERATION_TERM: Final = _pfg_term(
    "operation_datum_point", "operation.datum-point"
)
PART_LOCAL_COORDINATE_SYSTEM_OPERATION_TERM: Final = _pfg_term(
    "operation_local_coordinate_system", "operation.local-coordinate-system"
)
PART_DATUM_PLACEMENT_ROLE_TERM: Final = _pfg_term(
    "role_explicit_placement", "input-role.explicit-placement"
)
PART_DATUM_PLACEMENT_TYPE_TERM: Final = _pfg_term(
    "type_explicit_placement", "value-type.explicit-placement"
)
PART_DATUM_CANONICAL_JSON_TERM: Final = _pfg_term(
    "encoding_canonical_json", "value-encoding.canonical-json"
)

PART_DATUM_LINE_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_datum_line", "result-role.datum-line"
)
PART_DATUM_PLANE_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_datum_plane", "result-role.datum-plane"
)
PART_DATUM_POINT_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_datum_point", "result-role.datum-point"
)
PART_LOCAL_COORDINATE_SYSTEM_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_local_coordinate_system", "result-role.local-coordinate-system"
)
PART_DATUM_LINE_TYPE_TERM: Final = _pfg_term("type_datum_line", "value-type.datum-line")
PART_DATUM_PLANE_TYPE_TERM: Final = _pfg_term(
    "type_datum_plane", "value-type.datum-plane"
)
PART_DATUM_POINT_TYPE_TERM: Final = _pfg_term(
    "type_datum_point", "value-type.datum-point"
)
PART_LOCAL_COORDINATE_SYSTEM_TYPE_TERM: Final = _pfg_term(
    "type_local_coordinate_system", "value-type.local-coordinate-system"
)


@dataclass(frozen=True, slots=True)
class _OperationTerms:
    operation: PartDatumOperation
    operation_term: SemanticTermRefV2
    result_role: SemanticTermRefV2
    result_type: SemanticTermRefV2
    native_operation: str


PART_DATUM_OPERATION_TERMS: Final = (
    _OperationTerms(
        PartDatumOperation.DATUM_LINE,
        PART_DATUM_LINE_OPERATION_TERM,
        PART_DATUM_LINE_RESULT_ROLE_TERM,
        PART_DATUM_LINE_TYPE_TERM,
        "DatumLine",
    ),
    _OperationTerms(
        PartDatumOperation.DATUM_PLANE,
        PART_DATUM_PLANE_OPERATION_TERM,
        PART_DATUM_PLANE_RESULT_ROLE_TERM,
        PART_DATUM_PLANE_TYPE_TERM,
        "DatumPlane",
    ),
    _OperationTerms(
        PartDatumOperation.DATUM_POINT,
        PART_DATUM_POINT_OPERATION_TERM,
        PART_DATUM_POINT_RESULT_ROLE_TERM,
        PART_DATUM_POINT_TYPE_TERM,
        "DatumPoint",
    ),
    _OperationTerms(
        PartDatumOperation.LOCAL_COORDINATE_SYSTEM,
        PART_LOCAL_COORDINATE_SYSTEM_OPERATION_TERM,
        PART_LOCAL_COORDINATE_SYSTEM_RESULT_ROLE_TERM,
        PART_LOCAL_COORDINATE_SYSTEM_TYPE_TERM,
        "LocalCoordinateSystem",
    ),
)

PART_DATUM_PFG_TERMS: Final = (
    PART_DATUM_STRUCTURE_TERM,
    PART_DATUM_FAMILY_TERM,
    PART_DATUM_LINE_OPERATION_TERM,
    PART_DATUM_PLANE_OPERATION_TERM,
    PART_DATUM_POINT_OPERATION_TERM,
    PART_LOCAL_COORDINATE_SYSTEM_OPERATION_TERM,
    PART_DATUM_PLACEMENT_ROLE_TERM,
    PART_DATUM_PLACEMENT_TYPE_TERM,
    PART_DATUM_CANONICAL_JSON_TERM,
    PART_DATUM_LINE_RESULT_ROLE_TERM,
    PART_DATUM_PLANE_RESULT_ROLE_TERM,
    PART_DATUM_POINT_RESULT_ROLE_TERM,
    PART_LOCAL_COORDINATE_SYSTEM_RESULT_ROLE_TERM,
    PART_DATUM_LINE_TYPE_TERM,
    PART_DATUM_PLANE_TYPE_TERM,
    PART_DATUM_POINT_TYPE_TERM,
    PART_LOCAL_COORDINATE_SYSTEM_TYPE_TERM,
)

_ADAPTER_CONTRACT_SHA256: Final = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            PART_DATUM_RULE_ID.encode("ascii"),
            PART_DATUM_RULE_CONTRACT_SHA256.encode("ascii"),
            b"canonical-pfg-v2;full-semantic-identity;shared-reviewed-family-v1;no-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in (
                    PART_DATUM_INTENT_DOCUMENT_ROLE_TERM,
                    PART_DATUM_CAPABILITY_DOCUMENT_ROLE_TERM,
                    PART_DATUM_CAPABILITY_SCHEMA_TERM,
                    PART_DATUM_PLAN_DOCUMENT_ROLE_TERM,
                    PART_DATUM_PLAN_SCHEMA_TERM,
                    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
                    PFG_SELECTOR_FEATURE_NODE,
                    *(_as_bridge(term) for term in PART_DATUM_PFG_TERMS),
                )
            ),
        )
    )
).hexdigest()

FREECAD_PART_DATUM_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_part_datum_adapter",
    adapter_version="1.0.0",
    adapter_contract_sha256=_ADAPTER_CONTRACT_SHA256,
)

PART_DATUM_OPERATION_SPECS: Final = tuple(
    ReviewedOperationSpec(
        operation_id=terms.operation.value,
        semantic_term=_as_bridge(terms.operation_term),
        native_type_id=PART_DATUM_NATIVE_TYPE_IDS[terms.operation],
        native_operation=terms.native_operation,
        native_property_names=PART_DATUM_NATIVE_PROPERTIES[terms.operation],
    )
    for terms in PART_DATUM_OPERATION_TERMS
)

PART_DATUM_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    PART_DATUM_INTENT_DOCUMENT_ROLE_TERM,
    PART_DATUM_CAPABILITY_DOCUMENT_ROLE_TERM,
    PART_DATUM_CAPABILITY_SCHEMA_TERM,
    PART_DATUM_PLAN_DOCUMENT_ROLE_TERM,
    PART_DATUM_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in PART_DATUM_PFG_TERMS),
)

PART_DATUM_MANIFEST: Final = FamilyBatchManifest(
    family_id="part_datum",
    family_version="1.0.0",
    adapter=FREECAD_PART_DATUM_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=_MANIFEST_BUILD_ID,
    rule_id=PART_DATUM_RULE_ID,
    rule_contract_sha256=PART_DATUM_RULE_CONTRACT_SHA256,
    intent_role_term=PART_DATUM_INTENT_DOCUMENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=PART_DATUM_CAPABILITY_DOCUMENT_ROLE_TERM,
    capability_schema_term=PART_DATUM_CAPABILITY_SCHEMA_TERM,
    capability_media_type="application/vnd.vibecad.part-datum-capability+json",
    plan_role_term=PART_DATUM_PLAN_DOCUMENT_ROLE_TERM,
    plan_schema_term=PART_DATUM_PLAN_SCHEMA_TERM,
    plan_media_type=PART_DATUM_PLAN_MEDIA_TYPE,
    request_terms=PART_DATUM_REQUEST_TERMS,
    operations=PART_DATUM_OPERATION_SPECS,
    max_plan_bytes=MAX_PART_DATUM_PLAN_BYTES,
)


def build_part_datum_capability_document() -> tuple[DocumentRef, bytes]:
    """Return the exact content-addressed four-spec capability manifest."""

    return PART_DATUM_MANIFEST.capability_document(
        artifact_id="artifact_freecad_part_datum_capability"
    )


def _fail(path: str) -> None:
    raise IntentBridgeError(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)


def _identity(term: object) -> tuple[str, str, str, str]:
    try:
        return (
            term.namespace,
            term.vocabulary_version,
            term.term_id,
            term.term_definition_sha256,
        )
    except Exception:
        _fail("/graph/terms")


def _graph_term(
    terms: dict[str, SemanticTermRefV2],
    term_ref_id: str,
    expected: SemanticTermRefV2,
    path: str,
) -> None:
    actual = terms.get(term_ref_id)
    if actual is None or _identity(actual) != _identity(expected):
        _fail(path)


def _operation_for_graph(
    graph: ParametricFeatureGraphV2,
    terms: dict[str, SemanticTermRefV2],
) -> _OperationTerms:
    target = graph.nodes[0]
    structural = terms.get(target.intent.structural_kind_term_ref_id)
    family = terms.get(target.intent.family_term_ref_id)
    operation = terms.get(target.intent.operation_term_ref_id)
    matches = tuple(
        item
        for item in PART_DATUM_OPERATION_TERMS
        if structural is not None
        and family is not None
        and operation is not None
        and _identity(structural) == _identity(PART_DATUM_STRUCTURE_TERM)
        and _identity(family) == _identity(PART_DATUM_FAMILY_TERM)
        and _identity(operation) == _identity(item.operation_term)
    )
    if len(matches) != 1:
        _fail("/graph/operation")
    return matches[0]


def _assert_no_extensions(graph: ParametricFeatureGraphV2) -> None:
    target = graph.nodes[0]
    if (
        graph.extensions
        or graph.bodies[0].extension_ids
        or graph.parameters[0].extension_ids
        or graph.parameters[0].value.extension_ids
        or graph.parameters[0].expression is not None
        or target.extension_ids
        or target.intent.extension_ids
        or any(item.extension_ids for item in target.intent.input_ports)
        or any(item.extension_ids for item in target.results)
    ):
        _fail("/graph/extensions")


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
    except Exception:
        _fail("/graph")
    if (
        graph.graph_id != document.document_id
        or len(graph.bodies) != 1
        or len(graph.nodes) != 1
        or len(graph.parameters) != 1
        or graph.references
        or len(graph.graph_results) != 1
    ):
        _fail("/graph/scope")
    _assert_no_extensions(graph)
    terms = {item.term_ref_id: item for item in graph.terms}
    if any(
        sum(_identity(item) == _identity(expected) for item in graph.terms) != 1
        for expected in PART_DATUM_PFG_TERMS
    ):
        _fail("/graph/terms")
    operation_terms = _operation_for_graph(graph, terms)
    target = graph.nodes[0]
    body = graph.bodies[0]
    parameter = graph.parameters[0]
    if target.body_id != body.body_id:
        _fail("/graph/body")
    if (
        len(target.intent.input_ports) != 1
        or target.intent.dependencies
        or target.intent.references
        or len(target.intent.parameter_bindings) != 1
    ):
        _fail("/graph/bindings")
    port = target.intent.input_ports[0]
    binding = target.intent.parameter_bindings[0]
    if (
        binding.port_id != port.port_id
        or binding.parameter_id != parameter.parameter_id
        or binding.ordinal != 0
        or port.minimum_cardinality != 1
        or port.maximum_cardinality != 1
        or port.ordered
    ):
        _fail("/graph/bindings")
    _graph_term(
        terms,
        port.semantic_role_term_ref_id,
        PART_DATUM_PLACEMENT_ROLE_TERM,
        "/graph/placement/port_role",
    )
    _graph_term(
        terms,
        port.value_type_term_ref_id,
        PART_DATUM_PLACEMENT_TYPE_TERM,
        "/graph/placement/port_type",
    )
    _graph_term(
        terms,
        parameter.semantic_role_term_ref_id,
        PART_DATUM_PLACEMENT_ROLE_TERM,
        "/graph/placement/role",
    )
    _graph_term(
        terms,
        parameter.value.value_type_term_ref_id,
        PART_DATUM_PLACEMENT_TYPE_TERM,
        "/graph/placement/type",
    )
    _graph_term(
        terms,
        parameter.value.encoding_term_ref_id,
        PART_DATUM_CANONICAL_JSON_TERM,
        "/graph/placement/encoding",
    )
    placement_value = parameter.value.value
    if type(placement_value) is not dict or set(placement_value) != {
        "position_mm",
        "axis",
        "angle_degrees",
    }:
        _fail("/graph/placement/value")
    try:
        placement = ExplicitDatumPlacement.from_mapping(
            placement_value, "/graph/placement/value"
        )
    except Exception:
        _fail("/graph/placement/value")
    if len(target.results) != 1:
        _fail("/graph/result")
    result = target.results[0]
    _graph_term(
        terms,
        result.semantic_role_term_ref_id,
        operation_terms.result_role,
        "/graph/result/role",
    )
    _graph_term(
        terms,
        result.value_type_term_ref_id,
        operation_terms.result_type,
        "/graph/result/type",
    )
    graph_result = graph.graph_results[0]
    if graph_result.node_id != target.node_id or graph_result.result_id != result.result_id:
        _fail("/graph/graph_results")

    plan = PartDatumBackendPlan(
        source_artifact_id=document.artifact_id,
        source_graph_id=graph.graph_id,
        source_graph_sha256=graph.graph_sha256,
        source_content_sha256=hashlib.sha256(payload).hexdigest(),
        lowering_request_sha256=request_digest,
        adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
        manifest_sha256=manifest.manifest_sha256,
        container_id=body.body_id,
        node_id=target.node_id,
        result_id=result.result_id,
        operation=operation_terms.operation,
        placement=placement,
    )
    subject = SubjectRef(
        artifact_id=document.artifact_id,
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id=target.node_id,
    )
    return ReviewedPlanDraft(
        payload=plan.canonical_bytes,
        semantic_plan_sha256=plan.plan_sha256,
        operation_term=_as_bridge(operation_terms.operation_term),
        subjects=(subject,),
    )


def _validate_binding(
    decoded: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    if type(decoded) is not PartDatumBackendPlan:
        _fail("/plan_document/type")
    expected = next(
        item for item in PART_DATUM_OPERATION_TERMS if item.operation is decoded.operation
    )
    if (
        receipt.manifest_sha256 != decoded.manifest_sha256
        or receipt.request_digest != decoded.lowering_request_sha256
        or receipt.adapter.adapter_contract_sha256 != decoded.adapter_contract_sha256
        or receipt.source_document.artifact_id != decoded.source_artifact_id
        or receipt.source_document.document_id != decoded.source_graph_id
        or receipt.source_document.document_digest != decoded.source_graph_sha256
        or receipt.source_document.content_sha256 != decoded.source_content_sha256
        or receipt.plan_document.document_digest != decoded.plan_sha256
        or operation.operation_id != decoded.operation.value
        or operation.semantic_term != _as_bridge(expected.operation_term)
        or operation.native_type_id != PART_DATUM_NATIVE_TYPE_IDS[decoded.operation]
        or operation.native_operation != expected.native_operation
        or operation.native_property_names
        != tuple(sorted(PART_DATUM_NATIVE_PROPERTIES[decoded.operation]))
    ):
        _fail("/plan_document/binding")


class FreeCADPartDatumAdapter(ExactReviewedFamilyAdapter):
    """Shared exact adapter specialized by the four-spec datum manifest."""

    def __init__(self, sink: PlanSink) -> None:
        super().__init__(
            PART_DATUM_MANIFEST,
            sink,
            build_plan=_build_plan,
            decode_plan=decode_part_datum_backend_plan,
            validate_binding=_validate_binding,
        )


__all__ = [
    "FREECAD_PART_DATUM_ADAPTER_DESCRIPTOR",
    "PART_DATUM_CANONICAL_JSON_TERM",
    "PART_DATUM_FAMILY_TERM",
    "PART_DATUM_INTENT_DOCUMENT_ROLE_TERM",
    "PART_DATUM_LINE_OPERATION_TERM",
    "PART_DATUM_LINE_RESULT_ROLE_TERM",
    "PART_DATUM_LINE_TYPE_TERM",
    "PART_DATUM_MANIFEST",
    "PART_DATUM_OPERATION_SPECS",
    "PART_DATUM_OPERATION_TERMS",
    "PART_DATUM_PFG_TERMS",
    "PART_DATUM_PLACEMENT_ROLE_TERM",
    "PART_DATUM_PLACEMENT_TYPE_TERM",
    "PART_DATUM_PLANE_OPERATION_TERM",
    "PART_DATUM_PLANE_RESULT_ROLE_TERM",
    "PART_DATUM_PLANE_TYPE_TERM",
    "PART_DATUM_POINT_OPERATION_TERM",
    "PART_DATUM_POINT_RESULT_ROLE_TERM",
    "PART_DATUM_POINT_TYPE_TERM",
    "PART_DATUM_REQUEST_TERMS",
    "PART_DATUM_STRUCTURE_TERM",
    "PART_LOCAL_COORDINATE_SYSTEM_OPERATION_TERM",
    "PART_LOCAL_COORDINATE_SYSTEM_RESULT_ROLE_TERM",
    "PART_LOCAL_COORDINATE_SYSTEM_TYPE_TERM",
    "FreeCADPartDatumAdapter",
    "build_part_datum_capability_document",
]
