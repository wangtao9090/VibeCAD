"""Exact PFGv2 lowering for reviewed, authenticated FreeCAD file imports."""

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
    SemanticReferenceScope,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_part_file_import_rules import (
    MAX_PART_FILE_IMPORT_ARTIFACT_BYTES,
    MAX_PART_FILE_IMPORT_PLAN_BYTES,
    PART_FILE_IMPORT_FREECAD_ENGINE_BUILD_ID,
    PART_FILE_IMPORT_NATIVE_SPECS,
    PART_FILE_IMPORT_PLAN_MEDIA_TYPE,
    PART_FILE_IMPORT_RULE_CONTRACT_SHA256,
    PART_FILE_IMPORT_RULE_ID,
    PartFileImportBackendPlan,
    PartFileImportOperation,
    PartFileImportRuleError,
    decode_part_file_import_backend_plan,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.freecad-part"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.freecad-part.ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.freecad-part-file-import.adapter.v1\0"
_FREECAD_BUILD_DESCRIPTOR_SHA256 = hashlib.sha256(
    b"FreeCAD\0" + b"1.1.0\0" + PART_FILE_IMPORT_FREECAD_ENGINE_BUILD_ID.encode("ascii")
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


PART_FILE_IMPORT_INTENT_ROLE_TERM: Final = _bridge_term(
    "role_part_file_import_intent",
    "document-role.parametric-intent",
)
PART_FILE_IMPORT_CAPABILITY_ROLE_TERM: Final = _bridge_term(
    "role_part_file_import_capability",
    "document-role.freecad-part-file-import-capability",
)
PART_FILE_IMPORT_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_part_file_import_capability_v1",
    "document-schema.freecad-part-file-import-capability-v1",
)
PART_FILE_IMPORT_PLAN_ROLE_TERM: Final = _bridge_term(
    "role_part_file_import_backend_plan",
    "document-role.freecad-backend-plan",
)
PART_FILE_IMPORT_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_part_file_import_plan_v1",
    "document-schema.freecad-part-file-import-plan-v1",
)

PART_FILE_IMPORT_STRUCTURE_TERM: Final = _pfg_term(
    "structure_part_file_import_feature",
    "structure.part-file-import-feature",
)
PART_FILE_IMPORT_FAMILY_TERM: Final = _pfg_term(
    "family_part_file_import",
    "feature-family.part-file-import",
)
PART_FILE_IMPORT_ARTIFACT_ROLE_TERM: Final = _pfg_term(
    "role_part_file_import_artifact",
    "input-role.authenticated-cad-artifact",
)
PART_FILE_IMPORT_ARTIFACT_LOCATOR_TERM: Final = _pfg_term(
    "locator_part_file_import_external_artifact",
    "locator.external-artifact-id-and-content-sha256",
)
PART_FILE_IMPORT_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_part_file_import_result",
    "result-role.imported-topology-snapshot",
)
PART_FILE_IMPORT_SHAPE_TYPE_TERM: Final = _pfg_term(
    "type_part_file_import_shape",
    "value-type.shape",
)


@dataclass(frozen=True, slots=True)
class PartFileImportOperationTerms:
    operation: PartFileImportOperation
    operation_term: SemanticTermRefV2
    artifact_schema_term: SemanticTermRefV2
    artifact_value_type_term: SemanticTermRefV2


PART_FILE_IMPORT_OPERATION_TERMS: Final = tuple(
    PartFileImportOperationTerms(
        operation=operation,
        operation_term=_pfg_term(
            f"operation_part_import_{operation.value}",
            f"operation.part-import-{operation.value}-snapshot",
        ),
        artifact_schema_term=_pfg_term(
            spec.artifact_schema_term_ref_id,
            f"document-schema.{operation.value}-artifact-v1",
        ),
        artifact_value_type_term=_pfg_term(
            spec.artifact_value_type_term_ref_id,
            f"value-type.{operation.value}-artifact",
        ),
    )
    for operation, spec in PART_FILE_IMPORT_NATIVE_SPECS.items()
)

PART_FILE_IMPORT_PFG_TERMS: Final = (
    PART_FILE_IMPORT_STRUCTURE_TERM,
    PART_FILE_IMPORT_FAMILY_TERM,
    PART_FILE_IMPORT_ARTIFACT_ROLE_TERM,
    PART_FILE_IMPORT_ARTIFACT_LOCATOR_TERM,
    PART_FILE_IMPORT_RESULT_ROLE_TERM,
    PART_FILE_IMPORT_SHAPE_TYPE_TERM,
    *(item.operation_term for item in PART_FILE_IMPORT_OPERATION_TERMS),
    *(item.artifact_schema_term for item in PART_FILE_IMPORT_OPERATION_TERMS),
    *(item.artifact_value_type_term for item in PART_FILE_IMPORT_OPERATION_TERMS),
)

PART_FILE_IMPORT_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    PART_FILE_IMPORT_INTENT_ROLE_TERM,
    PART_FILE_IMPORT_CAPABILITY_ROLE_TERM,
    PART_FILE_IMPORT_CAPABILITY_SCHEMA_TERM,
    PART_FILE_IMPORT_PLAN_ROLE_TERM,
    PART_FILE_IMPORT_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in PART_FILE_IMPORT_PFG_TERMS),
)

_ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            PART_FILE_IMPORT_RULE_ID.encode("ascii"),
            PART_FILE_IMPORT_RULE_CONTRACT_SHA256.encode("ascii"),
            b"canonical-pfg-v2;external-content-ref;reviewed-family-engine;no-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in PART_FILE_IMPORT_REQUEST_TERMS
            ),
        )
    )
).hexdigest()

FREECAD_PART_FILE_IMPORT_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_part_file_import_adapter",
    adapter_version="1.0.0",
    adapter_contract_sha256=_ADAPTER_CONTRACT_SHA256,
)

PART_FILE_IMPORT_OPERATION_SPECS: Final = tuple(
    ReviewedOperationSpec(
        operation_id=item.operation.value,
        semantic_term=_as_bridge(item.operation_term),
        native_type_id=PART_FILE_IMPORT_NATIVE_SPECS[item.operation].type_id,
        native_operation=PART_FILE_IMPORT_NATIVE_SPECS[item.operation].native_operation,
        native_property_names=PART_FILE_IMPORT_NATIVE_SPECS[item.operation].native_property_names,
    )
    for item in PART_FILE_IMPORT_OPERATION_TERMS
)

PART_FILE_IMPORT_MANIFEST: Final = FamilyBatchManifest(
    family_id="freecad_part_file_import",
    family_version="1.0.0",
    adapter=FREECAD_PART_FILE_IMPORT_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=_FREECAD_BUILD_DESCRIPTOR_SHA256,
    rule_id=PART_FILE_IMPORT_RULE_ID,
    rule_contract_sha256=PART_FILE_IMPORT_RULE_CONTRACT_SHA256,
    intent_role_term=PART_FILE_IMPORT_INTENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=PART_FILE_IMPORT_CAPABILITY_ROLE_TERM,
    capability_schema_term=PART_FILE_IMPORT_CAPABILITY_SCHEMA_TERM,
    capability_media_type="application/vnd.vibecad.freecad-part-file-import-capability+json",
    plan_role_term=PART_FILE_IMPORT_PLAN_ROLE_TERM,
    plan_schema_term=PART_FILE_IMPORT_PLAN_SCHEMA_TERM,
    plan_media_type=PART_FILE_IMPORT_PLAN_MEDIA_TYPE,
    request_terms=PART_FILE_IMPORT_REQUEST_TERMS,
    operations=PART_FILE_IMPORT_OPERATION_SPECS,
    max_plan_bytes=MAX_PART_FILE_IMPORT_PLAN_BYTES,
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
) -> PartFileImportOperationTerms | None:
    if not _matches(
        terms, node.intent.structural_kind_term_ref_id, PART_FILE_IMPORT_STRUCTURE_TERM
    ) or not _matches(terms, node.intent.family_term_ref_id, PART_FILE_IMPORT_FAMILY_TERM):
        return None
    operation = terms.get(node.intent.operation_term_ref_id)
    matches = tuple(
        item
        for item in PART_FILE_IMPORT_OPERATION_TERMS
        if operation is not None and _identity(operation) == _identity(item.operation_term)
    )
    return matches[0] if len(matches) == 1 else None


def _assert_closed_graph(graph: object) -> None:
    if (
        graph.extensions
        or graph.parameters
        or any(item.extension_ids for item in graph.bodies)
        or any(reference.extension_ids for reference in graph.references)
        or any(
            node.extension_ids
            or node.intent.extension_ids
            or node.intent.dependencies
            or node.intent.parameter_bindings
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
            payload,
            expected_sha256=document.document_digest,
        )
        if (
            graph.graph_id != document.document_id
            or hashlib.sha256(payload).hexdigest() != document.content_sha256
            or len(graph.bodies) != 1
            or len(graph.references) != 1
            or len(graph.nodes) != 1
            or len(graph.graph_results) != 1
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")
        _assert_closed_graph(graph)
        terms = {item.term_ref_id: item for item in graph.terms}
        if len(graph.terms) != len(PART_FILE_IMPORT_PFG_TERMS) or any(
            sum(_identity(item) == _identity(expected) for item in graph.terms) != 1
            for expected in PART_FILE_IMPORT_PFG_TERMS
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
        spec = PART_FILE_IMPORT_NATIVE_SPECS[operation]
        body = graph.bodies[0]
        if target.body_id != body.body_id:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/body")
        if len(target.intent.input_ports) != 1 or len(target.intent.references) != 1:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/input")
        port = target.intent.input_ports[0]
        binding = target.intent.references[0]
        reference = graph.references[0]
        if (
            not _matches(
                terms,
                port.semantic_role_term_ref_id,
                PART_FILE_IMPORT_ARTIFACT_ROLE_TERM,
            )
            or not _matches(
                terms,
                port.value_type_term_ref_id,
                operation_terms.artifact_value_type_term,
            )
            or port.minimum_cardinality != 1
            or port.maximum_cardinality != 1
            or port.ordered
            or binding.port_id != port.port_id
            or binding.reference_id != reference.reference_id
            or binding.ordinal != 0
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/input")
        if (
            reference.scope is not SemanticReferenceScope.EXTERNAL
            or not _matches(
                terms,
                reference.semantic_role_term_ref_id,
                PART_FILE_IMPORT_ARTIFACT_ROLE_TERM,
            )
            or not _matches(
                terms,
                reference.value_type_term_ref_id,
                operation_terms.artifact_value_type_term,
            )
            or not _matches(
                terms,
                reference.locator_term_ref_id,
                PART_FILE_IMPORT_ARTIFACT_LOCATOR_TERM,
            )
            or reference.source_content_sha256 is None
            or reference.source_node_id is not None
            or reference.source_geometry_id is not None
            or reference.occurrence_path
            or reference.qualifier_term_ref_ids
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/reference")
        if len(target.results) != 1:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/result")
        result = target.results[0]
        graph_result = graph.graph_results[0]
        if (
            not _matches(
                terms,
                result.semantic_role_term_ref_id,
                PART_FILE_IMPORT_RESULT_ROLE_TERM,
            )
            or not _matches(
                terms,
                result.value_type_term_ref_id,
                PART_FILE_IMPORT_SHAPE_TYPE_TERM,
            )
            or graph_result.node_id != target.node_id
            or graph_result.result_id != result.result_id
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/result")
        operation_spec = manifest.operation_for_term(_as_bridge(operation_terms.operation_term))
        if operation_spec is None:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/operation")
        plan = PartFileImportBackendPlan(
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
            operation=operation,
            artifact_id=reference.reference_id,
            artifact_content_sha256=reference.source_content_sha256,
            artifact_role_term_ref_id=spec.artifact_role_term_ref_id,
            artifact_schema_term_ref_id=spec.artifact_schema_term_ref_id,
            artifact_value_type_term_ref_id=spec.artifact_value_type_term_ref_id,
            artifact_media_type=spec.artifact_media_type,
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
    except PartFileImportRuleError:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/reference")
    except ParametricFeatureGraphError:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_document")
    except (Exception, SystemExit):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_document")


def _decode_plan(
    payload: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
) -> PartFileImportBackendPlan:
    return decode_part_file_import_backend_plan(
        payload,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )


def _validate_binding(
    decoded: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    if type(decoded) is not PartFileImportBackendPlan:
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


def build_part_file_import_capability_document(
    *,
    artifact_id: str = "artifact_freecad_part_file_import_capability",
) -> tuple[DocumentRef, bytes]:
    return PART_FILE_IMPORT_MANIFEST.capability_document(artifact_id=artifact_id)


def build_part_file_import_artifact_document(
    operation: PartFileImportOperation,
    payload: bytes,
    *,
    artifact_id: str = "artifact_part_file_import_source",
) -> DocumentRef:
    if (
        type(operation) is not PartFileImportOperation
        or type(payload) is not bytes
        or not (1 <= len(payload) <= MAX_PART_FILE_IMPORT_ARTIFACT_BYTES)
    ):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/artifact")
    digest = hashlib.sha256(payload).hexdigest()
    spec = PART_FILE_IMPORT_NATIVE_SPECS[operation]
    return DocumentRef(
        artifact_id=artifact_id,
        role_term_ref_id=spec.artifact_role_term_ref_id,
        schema_term_ref_id=spec.artifact_schema_term_ref_id,
        document_id=f"part_file_import_{digest[:32]}",
        document_digest=digest,
        content_sha256=digest,
        size_bytes=len(payload),
        media_type=spec.artifact_media_type,
    )


class FreeCADPartFileImportAdapter(ExactReviewedFamilyAdapter):
    """Shared reviewed-family adapter for BREP, IGES, and STEP snapshots."""

    __slots__ = ()

    def __init__(self, sink: PlanSink) -> None:
        super().__init__(
            PART_FILE_IMPORT_MANIFEST,
            sink,
            build_plan=_build_plan,
            decode_plan=_decode_plan,
            validate_binding=_validate_binding,
        )


__all__ = [
    "FREECAD_PART_FILE_IMPORT_ADAPTER_DESCRIPTOR",
    "PART_FILE_IMPORT_ARTIFACT_LOCATOR_TERM",
    "PART_FILE_IMPORT_ARTIFACT_ROLE_TERM",
    "PART_FILE_IMPORT_CAPABILITY_ROLE_TERM",
    "PART_FILE_IMPORT_CAPABILITY_SCHEMA_TERM",
    "PART_FILE_IMPORT_FAMILY_TERM",
    "PART_FILE_IMPORT_INTENT_ROLE_TERM",
    "PART_FILE_IMPORT_MANIFEST",
    "PART_FILE_IMPORT_OPERATION_SPECS",
    "PART_FILE_IMPORT_OPERATION_TERMS",
    "PART_FILE_IMPORT_PFG_TERMS",
    "PART_FILE_IMPORT_PLAN_ROLE_TERM",
    "PART_FILE_IMPORT_PLAN_SCHEMA_TERM",
    "PART_FILE_IMPORT_REQUEST_TERMS",
    "PART_FILE_IMPORT_RESULT_ROLE_TERM",
    "PART_FILE_IMPORT_SHAPE_TYPE_TERM",
    "PART_FILE_IMPORT_STRUCTURE_TERM",
    "FreeCADPartFileImportAdapter",
    "PartFileImportOperationTerms",
    "build_part_file_import_artifact_document",
    "build_part_file_import_capability_document",
]
