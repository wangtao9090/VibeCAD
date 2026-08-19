"""Private reviewed lowering for backend-neutral Part offset/projection intents."""

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
    ParametricFeatureGraphV2,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_part_offset_projection_rules import (
    MAX_PART_OFFSET_PLAN_BYTES,
    PART_OFFSET_FREECAD_ENGINE_BUILD_ID,
    PART_OFFSET_NATIVE_PROPERTIES,
    PART_OFFSET_NATIVE_TYPE_IDS,
    PART_OFFSET_PLAN_MEDIA_TYPE,
    PART_OFFSET_RULE_CONTRACT_SHA256,
    PART_OFFSET_RULE_ID,
    PART_OFFSET_SOURCE_ROLES,
    PartOffsetBackendPlan,
    PartOffsetOperation,
    PartOffsetSelection,
    PartOffsetSourceRole,
    decode_part_offset_backend_plan,
    encode_part_offset_configuration,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.part-offset-projection"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.part-offset-projection-ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.part-offset-projection-adapter.v1\0"
_MANIFEST_BUILD_ID: Final = hashlib.sha256(
    b"FreeCAD-build\0" + PART_OFFSET_FREECAD_ENGINE_BUILD_ID.encode("ascii")
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


PART_OFFSET_INTENT_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_part_offset_parametric_intent", "document-role.parametric-intent"
)
PART_OFFSET_CAPABILITY_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_part_offset_capability", "document-role.reviewed-offset-projection-capability"
)
PART_OFFSET_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_part_offset_capability_v1",
    "document-schema.reviewed-offset-projection-capability-v1",
)
PART_OFFSET_PLAN_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_part_offset_backend_plan", "document-role.reviewed-backend-plan"
)
PART_OFFSET_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_part_offset_plan_v1", "document-schema.reviewed-offset-projection-plan-v1"
)

PART_OFFSET_SOURCE_STRUCTURE_TERM: Final = _pfg_term(
    "structure_existing_part_geometry", "structure.existing-part-geometry-reference"
)
PART_OFFSET_SOURCE_FAMILY_TERM: Final = _pfg_term(
    "family_existing_part_geometry", "feature-family.existing-part-geometry"
)
PART_OFFSET_SOURCE_OPERATION_TERM: Final = _pfg_term(
    "operation_existing_part_geometry", "operation.existing-part-geometry-reference"
)
PART_OFFSET_STRUCTURE_TERM: Final = _pfg_term(
    "structure_generated_part_geometry", "structure.generated-part-geometry"
)
PART_OFFSET_OFFSET_FAMILY_TERM: Final = _pfg_term(
    "family_offset_geometry", "feature-family.offset-geometry"
)
PART_OFFSET_PROJECTION_FAMILY_TERM: Final = _pfg_term(
    "family_projected_geometry", "feature-family.projected-geometry"
)
PART_OFFSET_CONFIGURATION_ROLE_TERM: Final = _pfg_term(
    "role_offset_configuration", "input-role.offset-projection-configuration"
)
PART_OFFSET_CONFIGURATION_TYPE_TERM: Final = _pfg_term(
    "type_offset_configuration", "value-type.offset-projection-configuration"
)
PART_OFFSET_CANONICAL_JSON_TERM: Final = _pfg_term(
    "encoding_offset_canonical_json", "value-encoding.canonical-json"
)


@dataclass(frozen=True, slots=True)
class PartOffsetSourceTerms:
    role: PartOffsetSourceRole
    input_role: SemanticTermRefV2
    result_role: SemanticTermRefV2
    value_type: SemanticTermRefV2


def _source_terms(
    role: PartOffsetSourceRole,
    stem: str,
) -> PartOffsetSourceTerms:
    return PartOffsetSourceTerms(
        role=role,
        input_role=_pfg_term(f"role_input_{role.value}", f"input-role.{stem}"),
        result_role=_pfg_term(f"role_result_{role.value}", f"result-role.{stem}"),
        value_type=_pfg_term(f"type_{role.value}", f"value-type.{stem}"),
    )


PART_OFFSET_SOURCE_TERMS: Final = (
    _source_terms(PartOffsetSourceRole.SOLID_SOURCE, "single-solid-source"),
    _source_terms(PartOffsetSourceRole.PLANAR_WIRE_SOURCE, "closed-planar-wire-source"),
    _source_terms(PartOffsetSourceRole.SUPPORT_FACE, "single-face-support"),
    _source_terms(PartOffsetSourceRole.PROJECTION_EDGE, "single-edge-projection-source"),
)


@dataclass(frozen=True, slots=True)
class PartOffsetOperationTerms:
    operation: PartOffsetOperation
    family_term: SemanticTermRefV2
    operation_term: SemanticTermRefV2
    result_role: SemanticTermRefV2
    result_type: SemanticTermRefV2
    native_operation: str


def _operation_terms(
    operation: PartOffsetOperation,
    family: SemanticTermRefV2,
    stem: str,
    native_operation: str,
) -> PartOffsetOperationTerms:
    return PartOffsetOperationTerms(
        operation=operation,
        family_term=family,
        operation_term=_pfg_term(f"operation_{operation.value}", f"operation.{stem}"),
        result_role=_pfg_term(f"role_result_{operation.value}", f"result-role.{stem}"),
        result_type=_pfg_term(f"type_result_{operation.value}", f"value-type.{stem}"),
        native_operation=native_operation,
    )


PART_OFFSET_OPERATION_TERMS: Final = (
    _operation_terms(
        PartOffsetOperation.SOLID_OFFSET,
        PART_OFFSET_OFFSET_FAMILY_TERM,
        "solid-offset",
        "SolidOffset",
    ),
    _operation_terms(
        PartOffsetOperation.PLANAR_WIRE_OFFSET,
        PART_OFFSET_OFFSET_FAMILY_TERM,
        "planar-wire-offset",
        "PlanarWireOffset",
    ),
    _operation_terms(
        PartOffsetOperation.EDGE_ON_FACE_PROJECTION,
        PART_OFFSET_PROJECTION_FAMILY_TERM,
        "edge-on-face-projection",
        "EdgeOnFaceProjection",
    ),
)

PART_OFFSET_PFG_TERMS: Final = (
    PART_OFFSET_SOURCE_STRUCTURE_TERM,
    PART_OFFSET_SOURCE_FAMILY_TERM,
    PART_OFFSET_SOURCE_OPERATION_TERM,
    PART_OFFSET_STRUCTURE_TERM,
    PART_OFFSET_OFFSET_FAMILY_TERM,
    PART_OFFSET_PROJECTION_FAMILY_TERM,
    PART_OFFSET_CONFIGURATION_ROLE_TERM,
    PART_OFFSET_CONFIGURATION_TYPE_TERM,
    PART_OFFSET_CANONICAL_JSON_TERM,
    *(item.input_role for item in PART_OFFSET_SOURCE_TERMS),
    *(item.result_role for item in PART_OFFSET_SOURCE_TERMS),
    *(item.value_type for item in PART_OFFSET_SOURCE_TERMS),
    *(item.operation_term for item in PART_OFFSET_OPERATION_TERMS),
    *(item.result_role for item in PART_OFFSET_OPERATION_TERMS),
    *(item.result_type for item in PART_OFFSET_OPERATION_TERMS),
)

_ADAPTER_CONTRACT_SHA256: Final = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            PART_OFFSET_RULE_ID.encode("ascii"),
            PART_OFFSET_RULE_CONTRACT_SHA256.encode("ascii"),
            b"canonical-pfg-v2;full-semantic-identity;authenticated-whole-object-singleton-topology;trusted-static-topology-labels;shared-reviewed-family-v1;no-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in (
                    PART_OFFSET_INTENT_DOCUMENT_ROLE_TERM,
                    PART_OFFSET_CAPABILITY_DOCUMENT_ROLE_TERM,
                    PART_OFFSET_CAPABILITY_SCHEMA_TERM,
                    PART_OFFSET_PLAN_DOCUMENT_ROLE_TERM,
                    PART_OFFSET_PLAN_SCHEMA_TERM,
                    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
                    PFG_SELECTOR_FEATURE_NODE,
                    *(_as_bridge(term) for term in PART_OFFSET_PFG_TERMS),
                )
            ),
        )
    )
).hexdigest()

FREECAD_PART_OFFSET_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_part_offset_projection_adapter",
    adapter_version="1.0.0",
    adapter_contract_sha256=_ADAPTER_CONTRACT_SHA256,
)

PART_OFFSET_OPERATION_SPECS: Final = tuple(
    ReviewedOperationSpec(
        operation_id=terms.operation.value,
        semantic_term=_as_bridge(terms.operation_term),
        native_type_id=PART_OFFSET_NATIVE_TYPE_IDS[terms.operation],
        native_operation=terms.native_operation,
        native_property_names=PART_OFFSET_NATIVE_PROPERTIES[terms.operation],
    )
    for terms in PART_OFFSET_OPERATION_TERMS
)

PART_OFFSET_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    PART_OFFSET_INTENT_DOCUMENT_ROLE_TERM,
    PART_OFFSET_CAPABILITY_DOCUMENT_ROLE_TERM,
    PART_OFFSET_CAPABILITY_SCHEMA_TERM,
    PART_OFFSET_PLAN_DOCUMENT_ROLE_TERM,
    PART_OFFSET_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in PART_OFFSET_PFG_TERMS),
)

PART_OFFSET_MANIFEST: Final = FamilyBatchManifest(
    family_id="part_offset_projection",
    family_version="1.0.0",
    adapter=FREECAD_PART_OFFSET_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=_MANIFEST_BUILD_ID,
    rule_id=PART_OFFSET_RULE_ID,
    rule_contract_sha256=PART_OFFSET_RULE_CONTRACT_SHA256,
    intent_role_term=PART_OFFSET_INTENT_DOCUMENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=PART_OFFSET_CAPABILITY_DOCUMENT_ROLE_TERM,
    capability_schema_term=PART_OFFSET_CAPABILITY_SCHEMA_TERM,
    capability_media_type="application/vnd.vibecad.part-offset-projection-capability+json",
    plan_role_term=PART_OFFSET_PLAN_DOCUMENT_ROLE_TERM,
    plan_schema_term=PART_OFFSET_PLAN_SCHEMA_TERM,
    plan_media_type=PART_OFFSET_PLAN_MEDIA_TYPE,
    request_terms=PART_OFFSET_REQUEST_TERMS,
    operations=PART_OFFSET_OPERATION_SPECS,
    max_plan_bytes=MAX_PART_OFFSET_PLAN_BYTES,
)


def build_part_offset_capability_document() -> tuple[DocumentRef, bytes]:
    return PART_OFFSET_MANIFEST.capability_document(
        artifact_id="artifact_freecad_part_offset_projection_capability"
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


def _term_matches(
    terms: dict[str, SemanticTermRefV2],
    term_ref_id: str,
    expected: SemanticTermRefV2,
) -> bool:
    actual = terms.get(term_ref_id)
    return actual is not None and _identity(actual) == _identity(expected)


def _assert_exact_terms(graph: ParametricFeatureGraphV2) -> dict[str, SemanticTermRefV2]:
    terms = {item.term_ref_id: item for item in graph.terms}
    if len(terms) != len(PART_OFFSET_PFG_TERMS) or any(
        (actual := terms.get(expected.term_ref_id)) is None
        or _identity(actual) != _identity(expected)
        for expected in PART_OFFSET_PFG_TERMS
    ):
        _fail("/graph/terms")
    return terms


def _assert_no_extensions(graph: ParametricFeatureGraphV2) -> None:
    if graph.extensions or any(item.extension_ids for item in graph.bodies):
        _fail("/graph/extensions")
    for parameter in graph.parameters:
        if (
            parameter.extension_ids
            or parameter.value.extension_ids
            or parameter.expression is not None
        ):
            _fail("/graph/extensions")
    for node in graph.nodes:
        if (
            node.extension_ids
            or node.intent.extension_ids
            or any(item.extension_ids for item in node.intent.input_ports)
            or any(item.extension_ids for item in node.results)
        ):
            _fail("/graph/extensions")


def _operation_for_target(
    target: FeatureNodeV2,
    terms: dict[str, SemanticTermRefV2],
) -> PartOffsetOperationTerms:
    matches = tuple(
        item
        for item in PART_OFFSET_OPERATION_TERMS
        if _term_matches(
            terms, target.intent.structural_kind_term_ref_id, PART_OFFSET_STRUCTURE_TERM
        )
        and _term_matches(terms, target.intent.family_term_ref_id, item.family_term)
        and _term_matches(terms, target.intent.operation_term_ref_id, item.operation_term)
    )
    if len(matches) != 1:
        _fail("/graph/operation")
    return matches[0]


def _source_terms(role: PartOffsetSourceRole) -> PartOffsetSourceTerms:
    return next(item for item in PART_OFFSET_SOURCE_TERMS if item.role is role)


def _validate_source(
    source: FeatureNodeV2,
    *,
    body_id: str,
    dependency: object,
    expected: PartOffsetSourceTerms,
    terms: dict[str, SemanticTermRefV2],
) -> PartOffsetSelection:
    if (
        source.body_id != body_id
        or not _term_matches(
            terms, source.intent.structural_kind_term_ref_id, PART_OFFSET_SOURCE_STRUCTURE_TERM
        )
        or not _term_matches(
            terms, source.intent.family_term_ref_id, PART_OFFSET_SOURCE_FAMILY_TERM
        )
        or not _term_matches(
            terms, source.intent.operation_term_ref_id, PART_OFFSET_SOURCE_OPERATION_TERM
        )
        or source.intent.input_ports
        or source.intent.dependencies
        or source.intent.references
        or source.intent.parameter_bindings
        or len(source.results) != 1
    ):
        _fail(f"/graph/sources/{expected.role.value}")
    result = source.results[0]
    try:
        upstream_result_id = dependency.upstream_result_id
    except Exception:
        _fail(f"/graph/sources/{expected.role.value}")
    if (
        result.result_id != upstream_result_id
        or not _term_matches(terms, result.semantic_role_term_ref_id, expected.result_role)
        or not _term_matches(terms, result.value_type_term_ref_id, expected.value_type)
    ):
        _fail(f"/graph/sources/{expected.role.value}/result")
    return PartOffsetSelection(
        role=expected.role,
        node_id=source.node_id,
        result_id=result.result_id,
    )


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
        or len(graph.parameters) != 1
        or graph.references
        or len(graph.graph_results) != 1
        or not 2 <= len(graph.nodes) <= 3
    ):
        _fail("/graph/scope")
    _assert_no_extensions(graph)
    terms = _assert_exact_terms(graph)
    nodes = {item.node_id: item for item in graph.nodes}
    graph_result = graph.graph_results[0]
    target = nodes.get(graph_result.node_id)
    if target is None or len(target.results) != 1:
        _fail("/graph/result")
    result = target.results[0]
    if graph_result.result_id != result.result_id:
        _fail("/graph/result")
    operation_terms = _operation_for_target(target, terms)
    body = graph.bodies[0]
    if target.body_id != body.body_id or target.intent.references:
        _fail("/graph/body")

    ports = {item.port_id: item for item in target.intent.input_ports}
    config_ports = tuple(
        item
        for item in ports.values()
        if _term_matches(terms, item.semantic_role_term_ref_id, PART_OFFSET_CONFIGURATION_ROLE_TERM)
        and _term_matches(terms, item.value_type_term_ref_id, PART_OFFSET_CONFIGURATION_TYPE_TERM)
    )
    parameter = graph.parameters[0]
    bindings = target.intent.parameter_bindings
    if (
        len(config_ports) != 1
        or len(bindings) != 1
        or bindings[0].port_id != config_ports[0].port_id
        or bindings[0].parameter_id != parameter.parameter_id
        or bindings[0].ordinal != 0
        or config_ports[0].minimum_cardinality != 1
        or config_ports[0].maximum_cardinality != 1
        or config_ports[0].ordered
        or not _term_matches(
            terms,
            parameter.semantic_role_term_ref_id,
            PART_OFFSET_CONFIGURATION_ROLE_TERM,
        )
        or not _term_matches(
            terms,
            parameter.value.value_type_term_ref_id,
            PART_OFFSET_CONFIGURATION_TYPE_TERM,
        )
        or not _term_matches(
            terms,
            parameter.value.encoding_term_ref_id,
            PART_OFFSET_CANONICAL_JSON_TERM,
        )
    ):
        _fail("/graph/configuration")
    try:
        configuration_bytes = encode_part_offset_configuration(
            operation_terms.operation, parameter.value.value
        )
    except Exception:
        _fail("/graph/configuration/value")

    expected_roles = PART_OFFSET_SOURCE_ROLES[operation_terms.operation]
    dependencies = target.intent.dependencies
    if (
        len(nodes) != len(expected_roles) + 1
        or len(ports) != len(expected_roles) + 1
        or len(dependencies) != len(expected_roles)
    ):
        _fail("/graph/sources")
    selections: list[PartOffsetSelection] = []
    used_nodes = {target.node_id}
    for expected_role in expected_roles:
        expected = _source_terms(expected_role)
        matching_ports = tuple(
            item
            for item in ports.values()
            if _term_matches(terms, item.semantic_role_term_ref_id, expected.input_role)
            and _term_matches(terms, item.value_type_term_ref_id, expected.value_type)
        )
        if len(matching_ports) != 1:
            _fail(f"/graph/sources/{expected_role.value}/port")
        port = matching_ports[0]
        matching_dependencies = tuple(item for item in dependencies if item.port_id == port.port_id)
        if (
            len(matching_dependencies) != 1
            or matching_dependencies[0].ordinal != 0
            or port.minimum_cardinality != 1
            or port.maximum_cardinality != 1
            or port.ordered
        ):
            _fail(f"/graph/sources/{expected_role.value}/binding")
        dependency = matching_dependencies[0]
        source = nodes.get(dependency.upstream_node_id)
        if source is None or source.node_id in used_nodes:
            _fail(f"/graph/sources/{expected_role.value}")
        selections.append(
            _validate_source(
                source,
                body_id=body.body_id,
                dependency=dependency,
                expected=expected,
                terms=terms,
            )
        )
        used_nodes.add(source.node_id)
    if used_nodes != set(nodes):
        _fail("/graph/scope")

    if not _term_matches(
        terms, result.semantic_role_term_ref_id, operation_terms.result_role
    ) or not _term_matches(terms, result.value_type_term_ref_id, operation_terms.result_type):
        _fail("/graph/result")
    plan = PartOffsetBackendPlan(
        source_artifact_id=document.artifact_id,
        source_graph_id=graph.graph_id,
        source_graph_sha256=graph.graph_sha256,
        source_content_sha256=hashlib.sha256(payload).hexdigest(),
        lowering_request_sha256=request_digest,
        adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
        manifest_sha256=manifest.manifest_sha256,
        container_id=body.body_id,
        target_node_id=target.node_id,
        target_result_id=result.result_id,
        operation=operation_terms.operation,
        configuration_bytes=configuration_bytes,
        sources=tuple(selections),
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
    if type(decoded) is not PartOffsetBackendPlan:
        _fail("/plan_document/type")
    expected = next(
        item for item in PART_OFFSET_OPERATION_TERMS if item.operation is decoded.operation
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
        or operation.native_type_id != PART_OFFSET_NATIVE_TYPE_IDS[decoded.operation]
        or operation.native_operation != expected.native_operation
        or operation.native_property_names
        != tuple(sorted(PART_OFFSET_NATIVE_PROPERTIES[decoded.operation]))
    ):
        _fail("/plan_document/binding")


class FreeCADPartOffsetProjectionAdapter(ExactReviewedFamilyAdapter):
    def __init__(self, sink: PlanSink) -> None:
        super().__init__(
            PART_OFFSET_MANIFEST,
            sink,
            build_plan=_build_plan,
            decode_plan=decode_part_offset_backend_plan,
            validate_binding=_validate_binding,
        )


__all__ = [
    "FREECAD_PART_OFFSET_ADAPTER_DESCRIPTOR",
    "PART_OFFSET_CANONICAL_JSON_TERM",
    "PART_OFFSET_CAPABILITY_DOCUMENT_ROLE_TERM",
    "PART_OFFSET_CAPABILITY_SCHEMA_TERM",
    "PART_OFFSET_CONFIGURATION_ROLE_TERM",
    "PART_OFFSET_CONFIGURATION_TYPE_TERM",
    "PART_OFFSET_INTENT_DOCUMENT_ROLE_TERM",
    "PART_OFFSET_MANIFEST",
    "PART_OFFSET_OFFSET_FAMILY_TERM",
    "PART_OFFSET_OPERATION_SPECS",
    "PART_OFFSET_OPERATION_TERMS",
    "PART_OFFSET_PFG_TERMS",
    "PART_OFFSET_PLAN_DOCUMENT_ROLE_TERM",
    "PART_OFFSET_PLAN_SCHEMA_TERM",
    "PART_OFFSET_PROJECTION_FAMILY_TERM",
    "PART_OFFSET_REQUEST_TERMS",
    "PART_OFFSET_SOURCE_FAMILY_TERM",
    "PART_OFFSET_SOURCE_OPERATION_TERM",
    "PART_OFFSET_SOURCE_STRUCTURE_TERM",
    "PART_OFFSET_SOURCE_TERMS",
    "PART_OFFSET_STRUCTURE_TERM",
    "FreeCADPartOffsetProjectionAdapter",
    "PartOffsetOperationTerms",
    "PartOffsetSourceTerms",
    "build_part_offset_capability_document",
]
