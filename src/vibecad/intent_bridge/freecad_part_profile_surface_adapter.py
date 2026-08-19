"""Exact PFGv2 lowering for reviewed FreeCAD Part profile/surface operations."""

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
from vibecad.parametric.freecad_part_profile_surface_rules import (
    MAX_PART_PROFILE_SURFACE_PLAN_BYTES,
    PART_PROFILE_SURFACE_FREECAD_ENGINE_BUILD_ID,
    PART_PROFILE_SURFACE_NATIVE_SPECS,
    PART_PROFILE_SURFACE_PLAN_MEDIA_TYPE,
    PART_PROFILE_SURFACE_RULE_CONTRACT_SHA256,
    PART_PROFILE_SURFACE_RULE_ID,
    PartProfileSurfaceBackendPlan,
    PartProfileSurfaceOperation,
    PartProfileSurfaceParameterSet,
    PartProfileSurfaceRuleError,
    PartProfileSurfaceSelection,
    PartProfileSurfaceSourceRole,
    decode_part_profile_surface_backend_plan,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.freecad-part"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.freecad-part.ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.freecad-part-profile-surface.adapter.v1\0"
_FREECAD_BUILD_DESCRIPTOR_SHA256 = hashlib.sha256(
    b"FreeCAD\0" + b"1.1.0\0" + PART_PROFILE_SURFACE_FREECAD_ENGINE_BUILD_ID.encode("ascii")
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


PART_PROFILE_SURFACE_INTENT_ROLE_TERM: Final = _bridge_term(
    "role_part_profile_surface_intent",
    "document-role.parametric-intent",
)
PART_PROFILE_SURFACE_CAPABILITY_ROLE_TERM: Final = _bridge_term(
    "role_part_profile_surface_capability",
    "document-role.freecad-part-profile-surface-capability",
)
PART_PROFILE_SURFACE_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_part_profile_surface_capability_v1",
    "document-schema.freecad-part-profile-surface-capability-v1",
)
PART_PROFILE_SURFACE_PLAN_ROLE_TERM: Final = _bridge_term(
    "role_part_profile_surface_backend_plan",
    "document-role.freecad-backend-plan",
)
PART_PROFILE_SURFACE_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_part_profile_surface_plan_v1",
    "document-schema.freecad-part-profile-surface-plan-v1",
)

PART_PROFILE_SURFACE_STRUCTURE_TERM: Final = _pfg_term(
    "structure_part_profile_surface_feature",
    "structure.part-profile-surface-feature",
)
PART_PROFILE_SURFACE_FAMILY_TERM: Final = _pfg_term(
    "family_part_profile_surface",
    "feature-family.part-profile-surface",
)
PART_PROFILE_SURFACE_SOURCE_STRUCTURE_TERM: Final = _pfg_term(
    "structure_part_profile_surface_source",
    "structure.part-source",
)
PART_PROFILE_SURFACE_SOURCE_FAMILY_TERM: Final = _pfg_term(
    "family_part_profile_surface_source",
    "feature-family.part-source",
)
PART_PROFILE_SURFACE_SOURCE_OPERATION_TERM: Final = _pfg_term(
    "operation_part_profile_surface_source",
    "operation.part-source",
)
PART_PROFILE_SURFACE_SOURCE_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_part_profile_surface_source_result",
    "result-role.source-shape",
)
PART_PROFILE_SURFACE_PARAMETERS_ROLE_TERM: Final = _pfg_term(
    "role_part_profile_surface_parameters",
    "input-role.operation-parameters",
)
PART_PROFILE_SURFACE_PARAMETERS_TYPE_TERM: Final = _pfg_term(
    "type_part_profile_surface_parameters",
    "value-type.part-profile-surface-parameters",
)
PART_PROFILE_SURFACE_CANONICAL_JSON_TERM: Final = _pfg_term(
    "encoding_part_profile_surface_canonical_json",
    "value-encoding.canonical-json",
)
PART_PROFILE_SURFACE_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_part_profile_surface_result",
    "result-role.shape",
)
PART_PROFILE_SURFACE_SHAPE_TYPE_TERM: Final = _pfg_term(
    "type_part_profile_surface_shape",
    "value-type.shape",
)

PART_PROFILE_SURFACE_SOURCE_ROLE_TERMS: Final = {
    role: _pfg_term(
        f"role_part_profile_surface_{role.value}",
        f"input-role.{role.value}",
    )
    for role in PartProfileSurfaceSourceRole
}


@dataclass(frozen=True, slots=True)
class PartProfileSurfaceOperationTerms:
    operation: PartProfileSurfaceOperation
    operation_term: SemanticTermRefV2


PART_PROFILE_SURFACE_OPERATION_TERMS: Final = tuple(
    PartProfileSurfaceOperationTerms(
        operation,
        _pfg_term(
            f"operation_part_{operation.value}",
            f"operation.part-{operation.value.replace('_', '-')}",
        ),
    )
    for operation in PartProfileSurfaceOperation
)

PART_PROFILE_SURFACE_PFG_TERMS: Final = (
    PART_PROFILE_SURFACE_STRUCTURE_TERM,
    PART_PROFILE_SURFACE_FAMILY_TERM,
    PART_PROFILE_SURFACE_SOURCE_STRUCTURE_TERM,
    PART_PROFILE_SURFACE_SOURCE_FAMILY_TERM,
    PART_PROFILE_SURFACE_SOURCE_OPERATION_TERM,
    PART_PROFILE_SURFACE_SOURCE_RESULT_ROLE_TERM,
    PART_PROFILE_SURFACE_PARAMETERS_ROLE_TERM,
    PART_PROFILE_SURFACE_PARAMETERS_TYPE_TERM,
    PART_PROFILE_SURFACE_CANONICAL_JSON_TERM,
    PART_PROFILE_SURFACE_RESULT_ROLE_TERM,
    PART_PROFILE_SURFACE_SHAPE_TYPE_TERM,
    *(PART_PROFILE_SURFACE_SOURCE_ROLE_TERMS[role] for role in PartProfileSurfaceSourceRole),
    *(item.operation_term for item in PART_PROFILE_SURFACE_OPERATION_TERMS),
)

PART_PROFILE_SURFACE_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    PART_PROFILE_SURFACE_INTENT_ROLE_TERM,
    PART_PROFILE_SURFACE_CAPABILITY_ROLE_TERM,
    PART_PROFILE_SURFACE_CAPABILITY_SCHEMA_TERM,
    PART_PROFILE_SURFACE_PLAN_ROLE_TERM,
    PART_PROFILE_SURFACE_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in PART_PROFILE_SURFACE_PFG_TERMS),
)

_ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            PART_PROFILE_SURFACE_RULE_ID.encode("ascii"),
            PART_PROFILE_SURFACE_RULE_CONTRACT_SHA256.encode("ascii"),
            b"canonical-pfg-v2;ordered-authenticated-sources;reviewed-family-engine;no-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in PART_PROFILE_SURFACE_REQUEST_TERMS
            ),
        )
    )
).hexdigest()

FREECAD_PART_PROFILE_SURFACE_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_part_profile_surface_adapter",
    adapter_version="1.0.0",
    adapter_contract_sha256=_ADAPTER_CONTRACT_SHA256,
)

PART_PROFILE_SURFACE_OPERATION_SPECS: Final = tuple(
    ReviewedOperationSpec(
        operation_id=item.operation.value,
        semantic_term=_as_bridge(item.operation_term),
        native_type_id=PART_PROFILE_SURFACE_NATIVE_SPECS[item.operation].type_id,
        native_operation=PART_PROFILE_SURFACE_NATIVE_SPECS[item.operation].native_operation,
        native_property_names=PART_PROFILE_SURFACE_NATIVE_SPECS[
            item.operation
        ].native_property_names,
    )
    for item in PART_PROFILE_SURFACE_OPERATION_TERMS
)

PART_PROFILE_SURFACE_MANIFEST: Final = FamilyBatchManifest(
    family_id="freecad_part_profile_surface",
    family_version="1.0.0",
    adapter=FREECAD_PART_PROFILE_SURFACE_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=_FREECAD_BUILD_DESCRIPTOR_SHA256,
    rule_id=PART_PROFILE_SURFACE_RULE_ID,
    rule_contract_sha256=PART_PROFILE_SURFACE_RULE_CONTRACT_SHA256,
    intent_role_term=PART_PROFILE_SURFACE_INTENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=PART_PROFILE_SURFACE_CAPABILITY_ROLE_TERM,
    capability_schema_term=PART_PROFILE_SURFACE_CAPABILITY_SCHEMA_TERM,
    capability_media_type="application/vnd.vibecad.freecad-part-profile-surface-capability+json",
    plan_role_term=PART_PROFILE_SURFACE_PLAN_ROLE_TERM,
    plan_schema_term=PART_PROFILE_SURFACE_PLAN_SCHEMA_TERM,
    plan_media_type=PART_PROFILE_SURFACE_PLAN_MEDIA_TYPE,
    request_terms=PART_PROFILE_SURFACE_REQUEST_TERMS,
    operations=PART_PROFILE_SURFACE_OPERATION_SPECS,
    max_plan_bytes=MAX_PART_PROFILE_SURFACE_PLAN_BYTES,
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
    except (Exception, SystemExit):
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
) -> PartProfileSurfaceOperationTerms | None:
    if not _matches(
        terms,
        node.intent.structural_kind_term_ref_id,
        PART_PROFILE_SURFACE_STRUCTURE_TERM,
    ) or not _matches(
        terms,
        node.intent.family_term_ref_id,
        PART_PROFILE_SURFACE_FAMILY_TERM,
    ):
        return None
    operation = terms.get(node.intent.operation_term_ref_id)
    matches = tuple(
        item
        for item in PART_PROFILE_SURFACE_OPERATION_TERMS
        if operation is not None and _identity(operation) == _identity(item.operation_term)
    )
    return matches[0] if len(matches) == 1 else None


def _assert_closed_graph(graph: object) -> None:
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
            or node.intent.references
            or any(port.extension_ids for port in node.intent.input_ports)
            or any(result.extension_ids for result in node.results)
            for node in graph.nodes
        )
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/extensions")


def _validate_source_node(
    source: FeatureNodeV2,
    *,
    body_id: str,
    expected_result_id: str,
    terms: dict[str, SemanticTermRefV2],
    index: int,
) -> None:
    if (
        source.body_id != body_id
        or not _matches(
            terms,
            source.intent.structural_kind_term_ref_id,
            PART_PROFILE_SURFACE_SOURCE_STRUCTURE_TERM,
        )
        or not _matches(
            terms,
            source.intent.family_term_ref_id,
            PART_PROFILE_SURFACE_SOURCE_FAMILY_TERM,
        )
        or not _matches(
            terms,
            source.intent.operation_term_ref_id,
            PART_PROFILE_SURFACE_SOURCE_OPERATION_TERM,
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
        result.result_id != expected_result_id
        or not _matches(
            terms,
            result.semantic_role_term_ref_id,
            PART_PROFILE_SURFACE_SOURCE_RESULT_ROLE_TERM,
        )
        or not _matches(
            terms,
            result.value_type_term_ref_id,
            PART_PROFILE_SURFACE_SHAPE_TYPE_TERM,
        )
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, f"/graph/sources/{index}/result")


def _build_plan(
    document: DocumentRef,
    payload: bytes,
    request_digest: str,
    manifest: FamilyBatchManifest,
) -> ReviewedPlanDraft:
    try:
        graph = decode_parametric_feature_graph_v2(
            payload,
            expected_sha256=document.document_digest,
        )
        if (
            graph.graph_id != document.document_id
            or hashlib.sha256(payload).hexdigest() != document.content_sha256
            or len(graph.bodies) != 1
            or len(graph.parameters) != 1
            or len(graph.graph_results) != 1
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")
        _assert_closed_graph(graph)
        terms = {item.term_ref_id: item for item in graph.terms}
        if len(graph.terms) != len(PART_PROFILE_SURFACE_PFG_TERMS) or any(
            sum(_identity(item) == _identity(expected) for item in graph.terms) != 1
            for expected in PART_PROFILE_SURFACE_PFG_TERMS
        ):
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
        native_spec = PART_PROFILE_SURFACE_NATIVE_SPECS[operation]
        body = graph.bodies[0]
        if target.body_id != body.body_id:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/body")

        source_port_by_role: dict[PartProfileSurfaceSourceRole, object] = {}
        parameter_port = None
        for port in target.intent.input_ports:
            matched_roles = tuple(
                role
                for role, term in PART_PROFILE_SURFACE_SOURCE_ROLE_TERMS.items()
                if _matches(terms, port.semantic_role_term_ref_id, term)
            )
            if len(matched_roles) == 1:
                role = matched_roles[0]
                requirement = next(
                    (item for item in native_spec.source_requirements if item.role is role),
                    None,
                )
                if (
                    requirement is None
                    or role in source_port_by_role
                    or not _matches(
                        terms,
                        port.value_type_term_ref_id,
                        PART_PROFILE_SURFACE_SHAPE_TYPE_TERM,
                    )
                    or port.minimum_cardinality != requirement.minimum
                    or port.maximum_cardinality != requirement.maximum
                    or port.ordered is not requirement.ordered
                ):
                    _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/input_ports")
                source_port_by_role[role] = port
            elif _matches(
                terms,
                port.semantic_role_term_ref_id,
                PART_PROFILE_SURFACE_PARAMETERS_ROLE_TERM,
            ):
                if (
                    parameter_port is not None
                    or not _matches(
                        terms,
                        port.value_type_term_ref_id,
                        PART_PROFILE_SURFACE_PARAMETERS_TYPE_TERM,
                    )
                    or port.minimum_cardinality != 1
                    or port.maximum_cardinality != 1
                    or port.ordered
                ):
                    _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/input_ports")
                parameter_port = port
            else:
                _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/input_ports")
        if parameter_port is None or set(source_port_by_role) != {
            item.role for item in native_spec.source_requirements
        }:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/input_ports")

        nodes = {node.node_id: node for node in graph.nodes}
        selections: list[PartProfileSurfaceSelection] = []
        consumed_dependencies: set[str] = set()
        for requirement in native_spec.source_requirements:
            port = source_port_by_role[requirement.role]
            dependencies = tuple(
                item for item in target.intent.dependencies if item.port_id == port.port_id
            )
            if not requirement.minimum <= len(dependencies) <= requirement.maximum or tuple(
                item.ordinal for item in dependencies
            ) != tuple(range(len(dependencies))):
                _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/dependencies")
            for dependency in dependencies:
                index = len(selections)
                source = nodes.get(dependency.upstream_node_id)
                if source is None:
                    _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, f"/graph/sources/{index}")
                _validate_source_node(
                    source,
                    body_id=body.body_id,
                    expected_result_id=dependency.upstream_result_id,
                    terms=terms,
                    index=index,
                )
                consumed_dependencies.add(dependency.dependency_id)
                selections.append(
                    PartProfileSurfaceSelection(
                        role=requirement.role,
                        node_id=source.node_id,
                        result_id=dependency.upstream_result_id,
                        ordinal=dependency.ordinal,
                    )
                )
        if len(consumed_dependencies) != len(target.intent.dependencies):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/dependencies")
        if len({item.node_id for item in selections}) != len(selections) or set(nodes) != {
            target.node_id,
            *(item.node_id for item in selections),
        }:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")

        bindings = tuple(
            item
            for item in target.intent.parameter_bindings
            if item.port_id == parameter_port.port_id
        )
        if (
            len(bindings) != 1
            or len(target.intent.parameter_bindings) != 1
            or bindings[0].ordinal != 0
            or bindings[0].parameter_id != graph.parameters[0].parameter_id
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/parameters")
        parameter = graph.parameters[0]
        if (
            not _matches(
                terms,
                parameter.semantic_role_term_ref_id,
                PART_PROFILE_SURFACE_PARAMETERS_ROLE_TERM,
            )
            or not _matches(
                terms,
                parameter.value.value_type_term_ref_id,
                PART_PROFILE_SURFACE_PARAMETERS_TYPE_TERM,
            )
            or not _matches(
                terms,
                parameter.value.encoding_term_ref_id,
                PART_PROFILE_SURFACE_CANONICAL_JSON_TERM,
            )
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/parameters")
        parameters = PartProfileSurfaceParameterSet.from_value(
            operation,
            parameter.value.value,
        )
        if len(target.results) != 1:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/result")
        result = target.results[0]
        graph_result = graph.graph_results[0]
        if (
            not _matches(
                terms,
                result.semantic_role_term_ref_id,
                PART_PROFILE_SURFACE_RESULT_ROLE_TERM,
            )
            or not _matches(
                terms,
                result.value_type_term_ref_id,
                PART_PROFILE_SURFACE_SHAPE_TYPE_TERM,
            )
            or graph_result.node_id != target.node_id
            or graph_result.result_id != result.result_id
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/result")
        operation_spec = manifest.operation_for_term(_as_bridge(operation_terms.operation_term))
        if operation_spec is None:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/operation")
        plan = PartProfileSurfaceBackendPlan(
            source_artifact_id=document.artifact_id,
            source_graph_id=graph.graph_id,
            source_graph_sha256=graph.graph_sha256,
            source_content_sha256=hashlib.sha256(payload).hexdigest(),
            lowering_request_sha256=request_digest,
            adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
            manifest_sha256=manifest.manifest_sha256,
            operation_specification_sha256=operation_spec.specification_sha256,
            body_id=body.body_id,
            node_id=target.node_id,
            result_id=result.result_id,
            parameter_id=parameter.parameter_id,
            value_id=parameter.value.value_id,
            operation=operation,
            sources=tuple(selections),
            parameters=parameters,
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
    except IntentBridgeError:
        raise
    except PartProfileSurfaceRuleError:
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
) -> PartProfileSurfaceBackendPlan:
    return decode_part_profile_surface_backend_plan(
        payload,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )


def _validate_binding(
    decoded: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    if type(decoded) is not PartProfileSurfaceBackendPlan:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan")
    if (
        decoded.operation.value != operation.operation_id
        or decoded.operation_specification_sha256 != operation.specification_sha256
        or decoded.manifest_sha256 != receipt.manifest_sha256
        or decoded.lowering_request_sha256 != receipt.request_digest
        or decoded.adapter_contract_sha256 != receipt.adapter.adapter_contract_sha256
        or decoded.source_artifact_id != receipt.source_document.artifact_id
        or decoded.source_graph_id != receipt.source_document.document_id
        or decoded.source_graph_sha256 != receipt.source_document.document_digest
        or decoded.source_content_sha256 != receipt.source_document.content_sha256
        or decoded.plan_sha256 != receipt.plan_document.document_digest
    ):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan/binding")


def build_part_profile_surface_capability_document(
    *,
    artifact_id: str = "artifact_freecad_part_profile_surface_capability",
) -> tuple[DocumentRef, bytes]:
    return PART_PROFILE_SURFACE_MANIFEST.capability_document(artifact_id=artifact_id)


class FreeCADPartProfileSurfaceAdapter(ExactReviewedFamilyAdapter):
    """Shared reviewed-family adapter for all six profile/surface operations."""

    __slots__ = ()

    def __init__(self, sink: PlanSink) -> None:
        super().__init__(
            PART_PROFILE_SURFACE_MANIFEST,
            sink,
            build_plan=_build_plan,
            decode_plan=_decode_plan,
            validate_binding=_validate_binding,
        )


__all__ = [
    "FREECAD_PART_PROFILE_SURFACE_ADAPTER_DESCRIPTOR",
    "PART_PROFILE_SURFACE_CANONICAL_JSON_TERM",
    "PART_PROFILE_SURFACE_CAPABILITY_ROLE_TERM",
    "PART_PROFILE_SURFACE_CAPABILITY_SCHEMA_TERM",
    "PART_PROFILE_SURFACE_FAMILY_TERM",
    "PART_PROFILE_SURFACE_INTENT_ROLE_TERM",
    "PART_PROFILE_SURFACE_MANIFEST",
    "PART_PROFILE_SURFACE_OPERATION_SPECS",
    "PART_PROFILE_SURFACE_OPERATION_TERMS",
    "PART_PROFILE_SURFACE_PARAMETERS_ROLE_TERM",
    "PART_PROFILE_SURFACE_PARAMETERS_TYPE_TERM",
    "PART_PROFILE_SURFACE_PFG_TERMS",
    "PART_PROFILE_SURFACE_PLAN_ROLE_TERM",
    "PART_PROFILE_SURFACE_PLAN_SCHEMA_TERM",
    "PART_PROFILE_SURFACE_REQUEST_TERMS",
    "PART_PROFILE_SURFACE_RESULT_ROLE_TERM",
    "PART_PROFILE_SURFACE_SHAPE_TYPE_TERM",
    "PART_PROFILE_SURFACE_SOURCE_FAMILY_TERM",
    "PART_PROFILE_SURFACE_SOURCE_OPERATION_TERM",
    "PART_PROFILE_SURFACE_SOURCE_RESULT_ROLE_TERM",
    "PART_PROFILE_SURFACE_SOURCE_ROLE_TERMS",
    "PART_PROFILE_SURFACE_SOURCE_STRUCTURE_TERM",
    "PART_PROFILE_SURFACE_STRUCTURE_TERM",
    "FreeCADPartProfileSurfaceAdapter",
    "PartProfileSurfaceOperationTerms",
    "build_part_profile_surface_capability_document",
]
