"""Private reviewed lowering for bounded FreeCAD application-object intents.

The graph speaks only in backend-neutral document-object ontology terms.  A
trusted, static manifest binds ten complete semantic identities to the native
FreeCAD application layer; graph strings never select native types,
properties, code, expressions, or import paths.
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
    ParametricFeatureGraphV2,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_app_family_rules import (
    APP_FAMILY_FREECAD_ENGINE_BUILD_ID,
    APP_FAMILY_NATIVE_PROPERTIES,
    APP_FAMILY_NATIVE_TYPE_IDS,
    APP_FAMILY_PLAN_MEDIA_TYPE,
    APP_FAMILY_RELATION_KINDS,
    APP_FAMILY_RULE_CONTRACT_SHA256,
    APP_FAMILY_RULE_ID,
    MAX_APP_FAMILY_PLAN_BYTES,
    AppFamilyBackendPlan,
    AppFamilyOperation,
    AppFamilyRelationKind,
    decode_app_family_backend_plan,
    encode_app_family_configuration,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.application-object"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.application-object-ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.app-family-adapter.v1\0"
_MANIFEST_BUILD_ID: Final = hashlib.sha256(
    b"FreeCAD-build\0" + APP_FAMILY_FREECAD_ENGINE_BUILD_ID.encode("ascii")
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


APP_FAMILY_INTENT_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_app_family_parametric_intent", "document-role.parametric-intent"
)
APP_FAMILY_CAPABILITY_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_app_family_capability", "document-role.reviewed-application-object-capability"
)
APP_FAMILY_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_app_family_capability_v1",
    "document-schema.reviewed-application-object-capability-v1",
)
APP_FAMILY_PLAN_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_app_family_backend_plan", "document-role.reviewed-backend-plan"
)
APP_FAMILY_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_app_family_plan_v1", "document-schema.reviewed-application-object-plan-v1"
)

APP_FAMILY_STRUCTURE_TERM: Final = _pfg_term(
    "structure_application_object", "structure.application-document-object"
)
APP_FAMILY_SOURCE_STRUCTURE_TERM: Final = _pfg_term(
    "structure_existing_application_object",
    "structure.existing-application-document-object-reference",
)
APP_FAMILY_SOURCE_FAMILY_TERM: Final = _pfg_term(
    "family_existing_application_object", "feature-family.existing-document-object"
)
APP_FAMILY_SOURCE_OPERATION_TERM: Final = _pfg_term(
    "operation_existing_application_object",
    "operation.existing-application-document-object-reference",
)
APP_FAMILY_ANNOTATION_FAMILY_TERM: Final = _pfg_term(
    "family_document_annotation", "feature-family.document-annotation"
)
APP_FAMILY_STRUCTURE_FAMILY_TERM: Final = _pfg_term(
    "family_document_structure", "feature-family.document-structure"
)
APP_FAMILY_LINK_FAMILY_TERM: Final = _pfg_term(
    "family_document_link", "feature-family.document-link"
)
APP_FAMILY_METADATA_FAMILY_TERM: Final = _pfg_term(
    "family_document_metadata", "feature-family.document-metadata"
)
APP_FAMILY_REFERENCE_FAMILY_TERM: Final = _pfg_term(
    "family_document_reference", "feature-family.document-reference"
)

APP_FAMILY_CONFIGURATION_ROLE_TERM: Final = _pfg_term(
    "role_application_object_configuration", "input-role.application-object-configuration"
)
APP_FAMILY_CONFIGURATION_TYPE_TERM: Final = _pfg_term(
    "type_application_object_configuration", "value-type.application-object-configuration"
)
APP_FAMILY_CANONICAL_JSON_TERM: Final = _pfg_term(
    "encoding_application_object_canonical_json", "value-encoding.canonical-json"
)
APP_FAMILY_RELATED_OBJECT_ROLE_TERM: Final = _pfg_term(
    "role_related_application_object", "input-role.related-document-object"
)
APP_FAMILY_RELATED_OBJECT_TYPE_TERM: Final = _pfg_term(
    "type_related_application_object", "value-type.document-object-reference"
)
APP_FAMILY_SOURCE_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_existing_application_object_result", "result-role.existing-document-object"
)
APP_FAMILY_SOURCE_RESULT_TYPE_TERM: Final = APP_FAMILY_RELATED_OBJECT_TYPE_TERM


def _operation_term(operation: AppFamilyOperation, term_id: str) -> SemanticTermRefV2:
    return _pfg_term(f"operation_{operation.value}", term_id)


def _result_role_term(operation: AppFamilyOperation, term_id: str) -> SemanticTermRefV2:
    return _pfg_term(f"role_result_{operation.value}", term_id)


def _result_type_term(operation: AppFamilyOperation, term_id: str) -> SemanticTermRefV2:
    return _pfg_term(f"type_result_{operation.value}", term_id)


@dataclass(frozen=True, slots=True)
class AppFamilyOperationTerms:
    operation: AppFamilyOperation
    family_term: SemanticTermRefV2
    operation_term: SemanticTermRefV2
    result_role: SemanticTermRefV2
    result_type: SemanticTermRefV2
    native_operation: str


_OPERATION_DEFINITIONS: Final = (
    (
        AppFamilyOperation.TEXT_ANNOTATION,
        APP_FAMILY_ANNOTATION_FAMILY_TERM,
        "text-annotation",
        "TextAnnotation",
    ),
    (
        AppFamilyOperation.LEADER_ANNOTATION,
        APP_FAMILY_ANNOTATION_FAMILY_TERM,
        "leader-annotation",
        "LeaderAnnotation",
    ),
    (
        AppFamilyOperation.DOCUMENT_GROUP,
        APP_FAMILY_STRUCTURE_FAMILY_TERM,
        "plain-document-group",
        "DocumentGroup",
    ),
    (
        AppFamilyOperation.OBJECT_LINK,
        APP_FAMILY_LINK_FAMILY_TERM,
        "single-object-link",
        "ObjectLink",
    ),
    (
        AppFamilyOperation.LINK_GROUP,
        APP_FAMILY_LINK_FAMILY_TERM,
        "link-group",
        "LinkGroup",
    ),
    (
        AppFamilyOperation.MATERIAL_DEFINITION,
        APP_FAMILY_METADATA_FAMILY_TERM,
        "material-definition",
        "MaterialDefinition",
    ),
    (
        AppFamilyOperation.POSITIONED_PART,
        APP_FAMILY_STRUCTURE_FAMILY_TERM,
        "positioned-part-container",
        "PositionedPart",
    ),
    (
        AppFamilyOperation.PLACEMENT_REFERENCE,
        APP_FAMILY_REFERENCE_FAMILY_TERM,
        "explicit-placement-reference",
        "PlacementReference",
    ),
    (
        AppFamilyOperation.TEXT_DOCUMENT,
        APP_FAMILY_METADATA_FAMILY_TERM,
        "text-document",
        "TextDocument",
    ),
    (
        AppFamilyOperation.SCALAR_VARIABLE_SET,
        APP_FAMILY_METADATA_FAMILY_TERM,
        "scalar-variable-set",
        "ScalarVariableSet",
    ),
)

APP_FAMILY_OPERATION_TERMS: Final = tuple(
    AppFamilyOperationTerms(
        operation=operation,
        family_term=family,
        operation_term=_operation_term(operation, f"operation.{term_stem}"),
        result_role=_result_role_term(operation, f"result-role.{term_stem}"),
        result_type=_result_type_term(operation, f"value-type.{term_stem}"),
        native_operation=native_operation,
    )
    for operation, family, term_stem, native_operation in _OPERATION_DEFINITIONS
)

APP_FAMILY_PFG_TERMS: Final = (
    APP_FAMILY_STRUCTURE_TERM,
    APP_FAMILY_SOURCE_STRUCTURE_TERM,
    APP_FAMILY_SOURCE_FAMILY_TERM,
    APP_FAMILY_SOURCE_OPERATION_TERM,
    APP_FAMILY_ANNOTATION_FAMILY_TERM,
    APP_FAMILY_STRUCTURE_FAMILY_TERM,
    APP_FAMILY_LINK_FAMILY_TERM,
    APP_FAMILY_METADATA_FAMILY_TERM,
    APP_FAMILY_REFERENCE_FAMILY_TERM,
    APP_FAMILY_CONFIGURATION_ROLE_TERM,
    APP_FAMILY_CONFIGURATION_TYPE_TERM,
    APP_FAMILY_CANONICAL_JSON_TERM,
    APP_FAMILY_RELATED_OBJECT_ROLE_TERM,
    APP_FAMILY_RELATED_OBJECT_TYPE_TERM,
    APP_FAMILY_SOURCE_RESULT_ROLE_TERM,
    *(item.operation_term for item in APP_FAMILY_OPERATION_TERMS),
    *(item.result_role for item in APP_FAMILY_OPERATION_TERMS),
    *(item.result_type for item in APP_FAMILY_OPERATION_TERMS),
)

_ADAPTER_CONTRACT_SHA256: Final = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            APP_FAMILY_RULE_ID.encode("ascii"),
            APP_FAMILY_RULE_CONTRACT_SHA256.encode("ascii"),
            b"canonical-pfg-v2;full-semantic-identity;one-bounded-config;authenticated-one-object-relation;shared-reviewed-family-v1;no-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in (
                    APP_FAMILY_INTENT_DOCUMENT_ROLE_TERM,
                    APP_FAMILY_CAPABILITY_DOCUMENT_ROLE_TERM,
                    APP_FAMILY_CAPABILITY_SCHEMA_TERM,
                    APP_FAMILY_PLAN_DOCUMENT_ROLE_TERM,
                    APP_FAMILY_PLAN_SCHEMA_TERM,
                    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
                    PFG_SELECTOR_FEATURE_NODE,
                    *(_as_bridge(term) for term in APP_FAMILY_PFG_TERMS),
                )
            ),
        )
    )
).hexdigest()

FREECAD_APP_FAMILY_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_app_family_adapter",
    adapter_version="1.0.0",
    adapter_contract_sha256=_ADAPTER_CONTRACT_SHA256,
)

APP_FAMILY_OPERATION_SPECS: Final = tuple(
    ReviewedOperationSpec(
        operation_id=terms.operation.value,
        semantic_term=_as_bridge(terms.operation_term),
        native_type_id=APP_FAMILY_NATIVE_TYPE_IDS[terms.operation],
        native_operation=terms.native_operation,
        native_property_names=APP_FAMILY_NATIVE_PROPERTIES[terms.operation],
    )
    for terms in APP_FAMILY_OPERATION_TERMS
)

APP_FAMILY_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    APP_FAMILY_INTENT_DOCUMENT_ROLE_TERM,
    APP_FAMILY_CAPABILITY_DOCUMENT_ROLE_TERM,
    APP_FAMILY_CAPABILITY_SCHEMA_TERM,
    APP_FAMILY_PLAN_DOCUMENT_ROLE_TERM,
    APP_FAMILY_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in APP_FAMILY_PFG_TERMS),
)

APP_FAMILY_MANIFEST: Final = FamilyBatchManifest(
    family_id="application_document_objects",
    family_version="1.0.0",
    adapter=FREECAD_APP_FAMILY_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=_MANIFEST_BUILD_ID,
    rule_id=APP_FAMILY_RULE_ID,
    rule_contract_sha256=APP_FAMILY_RULE_CONTRACT_SHA256,
    intent_role_term=APP_FAMILY_INTENT_DOCUMENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=APP_FAMILY_CAPABILITY_DOCUMENT_ROLE_TERM,
    capability_schema_term=APP_FAMILY_CAPABILITY_SCHEMA_TERM,
    capability_media_type="application/vnd.vibecad.app-family-capability+json",
    plan_role_term=APP_FAMILY_PLAN_DOCUMENT_ROLE_TERM,
    plan_schema_term=APP_FAMILY_PLAN_SCHEMA_TERM,
    plan_media_type=APP_FAMILY_PLAN_MEDIA_TYPE,
    request_terms=APP_FAMILY_REQUEST_TERMS,
    operations=APP_FAMILY_OPERATION_SPECS,
    max_plan_bytes=MAX_APP_FAMILY_PLAN_BYTES,
)


def build_app_family_capability_document() -> tuple[DocumentRef, bytes]:
    """Return the exact content-addressed ten-spec capability manifest."""

    return APP_FAMILY_MANIFEST.capability_document(
        artifact_id="artifact_freecad_app_family_capability"
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
    if len(terms) != len(APP_FAMILY_PFG_TERMS) or any(
        (actual := terms.get(expected.term_ref_id)) is None
        or _identity(actual) != _identity(expected)
        for expected in APP_FAMILY_PFG_TERMS
    ):
        _fail("/graph/terms")
    return terms


def _operation_for_target(
    target: FeatureNodeV2,
    terms: dict[str, SemanticTermRefV2],
) -> AppFamilyOperationTerms:
    matches = tuple(
        item
        for item in APP_FAMILY_OPERATION_TERMS
        if _term_matches(
            terms, target.intent.structural_kind_term_ref_id, APP_FAMILY_STRUCTURE_TERM
        )
        and _term_matches(terms, target.intent.family_term_ref_id, item.family_term)
        and _term_matches(terms, target.intent.operation_term_ref_id, item.operation_term)
    )
    if len(matches) != 1:
        _fail("/graph/operation")
    return matches[0]


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


def _validate_source(
    source: FeatureNodeV2,
    *,
    body_id: str,
    dependency: object,
    terms: dict[str, SemanticTermRefV2],
) -> tuple[str, str]:
    if (
        source.body_id != body_id
        or not _term_matches(
            terms, source.intent.structural_kind_term_ref_id, APP_FAMILY_SOURCE_STRUCTURE_TERM
        )
        or not _term_matches(terms, source.intent.family_term_ref_id, APP_FAMILY_SOURCE_FAMILY_TERM)
        or not _term_matches(
            terms, source.intent.operation_term_ref_id, APP_FAMILY_SOURCE_OPERATION_TERM
        )
        or source.intent.input_ports
        or source.intent.dependencies
        or source.intent.references
        or source.intent.parameter_bindings
        or len(source.results) != 1
    ):
        _fail("/graph/relation/source")
    result = source.results[0]
    try:
        upstream_result_id = dependency.upstream_result_id
    except Exception:
        _fail("/graph/relation/dependency")
    if (
        result.result_id != upstream_result_id
        or not _term_matches(
            terms, result.semantic_role_term_ref_id, APP_FAMILY_SOURCE_RESULT_ROLE_TERM
        )
        or not _term_matches(
            terms, result.value_type_term_ref_id, APP_FAMILY_SOURCE_RESULT_TYPE_TERM
        )
    ):
        _fail("/graph/relation/source_result")
    return source.node_id, result.result_id


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
        or not 1 <= len(graph.nodes) <= 2
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
    relation_kind = APP_FAMILY_RELATION_KINDS[operation_terms.operation]
    body = graph.bodies[0]
    if target.body_id != body.body_id:
        _fail("/graph/body")

    ports = {item.port_id: item for item in target.intent.input_ports}
    config_ports = tuple(
        item
        for item in ports.values()
        if _term_matches(
            terms,
            item.semantic_role_term_ref_id,
            APP_FAMILY_CONFIGURATION_ROLE_TERM,
        )
        and _term_matches(
            terms,
            item.value_type_term_ref_id,
            APP_FAMILY_CONFIGURATION_TYPE_TERM,
        )
    )
    if len(config_ports) != 1:
        _fail("/graph/configuration/port")
    config_port = config_ports[0]
    bindings = target.intent.parameter_bindings
    parameter = graph.parameters[0]
    if (
        len(bindings) != 1
        or bindings[0].port_id != config_port.port_id
        or bindings[0].parameter_id != parameter.parameter_id
        or bindings[0].ordinal != 0
        or config_port.minimum_cardinality != 1
        or config_port.maximum_cardinality != 1
        or config_port.ordered
        or not _term_matches(
            terms,
            parameter.semantic_role_term_ref_id,
            APP_FAMILY_CONFIGURATION_ROLE_TERM,
        )
        or not _term_matches(
            terms,
            parameter.value.value_type_term_ref_id,
            APP_FAMILY_CONFIGURATION_TYPE_TERM,
        )
        or not _term_matches(
            terms,
            parameter.value.encoding_term_ref_id,
            APP_FAMILY_CANONICAL_JSON_TERM,
        )
    ):
        _fail("/graph/configuration")
    try:
        configuration_bytes = encode_app_family_configuration(
            operation_terms.operation, parameter.value.value
        )
    except Exception:
        _fail("/graph/configuration/value")

    related_node_id = None
    related_result_id = None
    if relation_kind is AppFamilyRelationKind.NONE:
        if (
            len(nodes) != 1
            or len(ports) != 1
            or target.intent.dependencies
            or target.intent.references
        ):
            _fail("/graph/relation")
    else:
        relation_ports = tuple(
            item
            for item in ports.values()
            if _term_matches(
                terms,
                item.semantic_role_term_ref_id,
                APP_FAMILY_RELATED_OBJECT_ROLE_TERM,
            )
            and _term_matches(
                terms,
                item.value_type_term_ref_id,
                APP_FAMILY_RELATED_OBJECT_TYPE_TERM,
            )
        )
        dependencies = target.intent.dependencies
        if (
            len(nodes) != 2
            or len(ports) != 2
            or len(relation_ports) != 1
            or len(dependencies) != 1
            or target.intent.references
        ):
            _fail("/graph/relation")
        relation_port = relation_ports[0]
        dependency = dependencies[0]
        if (
            dependency.port_id != relation_port.port_id
            or dependency.ordinal != 0
            or relation_port.minimum_cardinality != 1
            or relation_port.maximum_cardinality != 1
            or relation_port.ordered
        ):
            _fail("/graph/relation/dependency")
        source = nodes.get(dependency.upstream_node_id)
        if source is None or source is target:
            _fail("/graph/relation/source")
        related_node_id, related_result_id = _validate_source(
            source,
            body_id=body.body_id,
            dependency=dependency,
            terms=terms,
        )

    if not _term_matches(
        terms, result.semantic_role_term_ref_id, operation_terms.result_role
    ) or not _term_matches(terms, result.value_type_term_ref_id, operation_terms.result_type):
        _fail("/graph/result")
    plan = AppFamilyBackendPlan(
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
        related_node_id=related_node_id,
        related_result_id=related_result_id,
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
    if type(decoded) is not AppFamilyBackendPlan:
        _fail("/plan_document/type")
    expected = next(
        item for item in APP_FAMILY_OPERATION_TERMS if item.operation is decoded.operation
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
        or operation.native_type_id != APP_FAMILY_NATIVE_TYPE_IDS[decoded.operation]
        or operation.native_operation != expected.native_operation
        or operation.native_property_names
        != tuple(sorted(APP_FAMILY_NATIVE_PROPERTIES[decoded.operation]))
    ):
        _fail("/plan_document/binding")


class FreeCADAppFamilyAdapter(ExactReviewedFamilyAdapter):
    """Shared exact adapter specialized by the ten-spec application manifest."""

    def __init__(self, sink: PlanSink) -> None:
        super().__init__(
            APP_FAMILY_MANIFEST,
            sink,
            build_plan=_build_plan,
            decode_plan=decode_app_family_backend_plan,
            validate_binding=_validate_binding,
        )


__all__ = [
    "APP_FAMILY_ANNOTATION_FAMILY_TERM",
    "APP_FAMILY_CANONICAL_JSON_TERM",
    "APP_FAMILY_CAPABILITY_DOCUMENT_ROLE_TERM",
    "APP_FAMILY_CAPABILITY_SCHEMA_TERM",
    "APP_FAMILY_CONFIGURATION_ROLE_TERM",
    "APP_FAMILY_CONFIGURATION_TYPE_TERM",
    "APP_FAMILY_INTENT_DOCUMENT_ROLE_TERM",
    "APP_FAMILY_LINK_FAMILY_TERM",
    "APP_FAMILY_MANIFEST",
    "APP_FAMILY_METADATA_FAMILY_TERM",
    "APP_FAMILY_OPERATION_SPECS",
    "APP_FAMILY_OPERATION_TERMS",
    "APP_FAMILY_PFG_TERMS",
    "APP_FAMILY_PLAN_DOCUMENT_ROLE_TERM",
    "APP_FAMILY_PLAN_SCHEMA_TERM",
    "APP_FAMILY_REFERENCE_FAMILY_TERM",
    "APP_FAMILY_RELATED_OBJECT_ROLE_TERM",
    "APP_FAMILY_RELATED_OBJECT_TYPE_TERM",
    "APP_FAMILY_REQUEST_TERMS",
    "APP_FAMILY_SOURCE_FAMILY_TERM",
    "APP_FAMILY_SOURCE_OPERATION_TERM",
    "APP_FAMILY_SOURCE_RESULT_ROLE_TERM",
    "APP_FAMILY_SOURCE_RESULT_TYPE_TERM",
    "APP_FAMILY_SOURCE_STRUCTURE_TERM",
    "APP_FAMILY_STRUCTURE_FAMILY_TERM",
    "APP_FAMILY_STRUCTURE_TERM",
    "FREECAD_APP_FAMILY_ADAPTER_DESCRIPTOR",
    "AppFamilyOperationTerms",
    "FreeCADAppFamilyAdapter",
    "build_app_family_capability_document",
]
