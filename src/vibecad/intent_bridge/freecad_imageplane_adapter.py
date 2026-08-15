"""Exact PFGv2 lowering for reviewed authenticated FreeCAD image planes.

This private adapter accepts one closed feature graph containing one bounded
configuration value and one content-addressed external raster reference.  It
does not accept a host path and does not grant execution authority.  Complete
ontology identity, not a local term-ref string, selects the single reviewed
``Image::ImagePlane`` operation.
"""

from __future__ import annotations

import hashlib
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
from vibecad.parametric.freecad_imageplane_rules import (
    IMAGEPLANE_ARTIFACT_ROLE_TERM_REF_ID,
    IMAGEPLANE_ARTIFACT_SPECS,
    IMAGEPLANE_ARTIFACT_VALUE_TYPE_TERM_REF_ID,
    IMAGEPLANE_FREECAD_ENGINE_BUILD_ID,
    IMAGEPLANE_PLAN_MEDIA_TYPE,
    IMAGEPLANE_RULE_CONTRACT_SHA256,
    IMAGEPLANE_RULE_ID,
    MAX_IMAGEPLANE_PLAN_BYTES,
    ImagePlaneBackendPlan,
    ImagePlaneRuleError,
    decode_imageplane_backend_plan,
    encode_imageplane_configuration,
    validate_imageplane_artifact_payload,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.reference-image"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.reference-image.ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.freecad-imageplane.adapter.v1\0"
_FREECAD_BUILD_DESCRIPTOR_SHA256 = hashlib.sha256(
    b"FreeCAD\0" + b"1.1.0\0" + IMAGEPLANE_FREECAD_ENGINE_BUILD_ID.encode("ascii")
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


IMAGEPLANE_INTENT_ROLE_TERM: Final = _bridge_term(
    "role_imageplane_intent",
    "document-role.parametric-intent",
)
IMAGEPLANE_CAPABILITY_ROLE_TERM: Final = _bridge_term(
    "role_imageplane_capability",
    "document-role.freecad-imageplane-capability",
)
IMAGEPLANE_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_imageplane_capability_v1",
    "document-schema.freecad-imageplane-capability-v1",
)
IMAGEPLANE_PLAN_ROLE_TERM: Final = _bridge_term(
    "role_imageplane_backend_plan",
    "document-role.freecad-backend-plan",
)
IMAGEPLANE_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_imageplane_plan_v1",
    "document-schema.freecad-imageplane-plan-v1",
)

IMAGEPLANE_STRUCTURE_TERM: Final = _pfg_term(
    "structure_imageplane_feature",
    "structure.reference-image-plane",
)
IMAGEPLANE_FAMILY_TERM: Final = _pfg_term(
    "family_reference_image",
    "feature-family.reference-image",
)
IMAGEPLANE_OPERATION_TERM: Final = _pfg_term(
    "operation_place_or_edit_imageplane",
    "operation.place-or-edit-authenticated-image-plane",
)
IMAGEPLANE_CONFIGURATION_ROLE_TERM: Final = _pfg_term(
    "role_imageplane_configuration",
    "input-role.image-plane-configuration",
)
IMAGEPLANE_CONFIGURATION_TYPE_TERM: Final = _pfg_term(
    "type_imageplane_configuration",
    "value-type.image-plane-configuration",
)
IMAGEPLANE_CANONICAL_JSON_ENCODING_TERM: Final = _pfg_term(
    "encoding_imageplane_canonical_json",
    "encoding.canonical-json",
)
IMAGEPLANE_ARTIFACT_ROLE_TERM: Final = _pfg_term(
    IMAGEPLANE_ARTIFACT_ROLE_TERM_REF_ID,
    "input-role.authenticated-raster-image",
)
IMAGEPLANE_ARTIFACT_TYPE_TERM: Final = _pfg_term(
    IMAGEPLANE_ARTIFACT_VALUE_TYPE_TERM_REF_ID,
    "value-type.raster-image",
)
IMAGEPLANE_ARTIFACT_LOCATOR_TERM: Final = _pfg_term(
    "locator_imageplane_external_artifact",
    "locator.external-artifact-id-and-content-sha256",
)
IMAGEPLANE_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_imageplane_result",
    "result-role.editable-reference-image-plane",
)
IMAGEPLANE_RESULT_TYPE_TERM: Final = _pfg_term(
    "type_imageplane_result",
    "value-type.reference-image-plane",
)

IMAGEPLANE_PFG_TERMS: Final = (
    IMAGEPLANE_STRUCTURE_TERM,
    IMAGEPLANE_FAMILY_TERM,
    IMAGEPLANE_OPERATION_TERM,
    IMAGEPLANE_CONFIGURATION_ROLE_TERM,
    IMAGEPLANE_CONFIGURATION_TYPE_TERM,
    IMAGEPLANE_CANONICAL_JSON_ENCODING_TERM,
    IMAGEPLANE_ARTIFACT_ROLE_TERM,
    IMAGEPLANE_ARTIFACT_TYPE_TERM,
    IMAGEPLANE_ARTIFACT_LOCATOR_TERM,
    IMAGEPLANE_RESULT_ROLE_TERM,
    IMAGEPLANE_RESULT_TYPE_TERM,
)

IMAGEPLANE_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    IMAGEPLANE_INTENT_ROLE_TERM,
    IMAGEPLANE_CAPABILITY_ROLE_TERM,
    IMAGEPLANE_CAPABILITY_SCHEMA_TERM,
    IMAGEPLANE_PLAN_ROLE_TERM,
    IMAGEPLANE_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in IMAGEPLANE_PFG_TERMS),
)

_ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            IMAGEPLANE_RULE_ID.encode("ascii"),
            IMAGEPLANE_RULE_CONTRACT_SHA256.encode("ascii"),
            b"canonical-pfg-v2;typed-configuration;external-content-ref;"
            b"reviewed-family-engine;no-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in IMAGEPLANE_REQUEST_TERMS
            ),
        )
    )
).hexdigest()

FREECAD_IMAGEPLANE_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_imageplane_adapter",
    adapter_version="1.0.0",
    adapter_contract_sha256=_ADAPTER_CONTRACT_SHA256,
)

IMAGEPLANE_OPERATION_SPEC: Final = ReviewedOperationSpec(
    operation_id="place_or_edit_image_plane",
    semantic_term=_as_bridge(IMAGEPLANE_OPERATION_TERM),
    native_type_id="Image::ImagePlane",
    native_operation="place_or_edit_authenticated_reference_image_plane",
    native_property_names=("ImageFile", "Placement", "XSize", "YSize"),
)

IMAGEPLANE_MANIFEST: Final = FamilyBatchManifest(
    family_id="freecad_imageplane",
    family_version="1.0.0",
    adapter=FREECAD_IMAGEPLANE_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=_FREECAD_BUILD_DESCRIPTOR_SHA256,
    rule_id=IMAGEPLANE_RULE_ID,
    rule_contract_sha256=IMAGEPLANE_RULE_CONTRACT_SHA256,
    intent_role_term=IMAGEPLANE_INTENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=IMAGEPLANE_CAPABILITY_ROLE_TERM,
    capability_schema_term=IMAGEPLANE_CAPABILITY_SCHEMA_TERM,
    capability_media_type="application/vnd.vibecad.freecad-imageplane-capability+json",
    plan_role_term=IMAGEPLANE_PLAN_ROLE_TERM,
    plan_schema_term=IMAGEPLANE_PLAN_SCHEMA_TERM,
    plan_media_type=IMAGEPLANE_PLAN_MEDIA_TYPE,
    request_terms=IMAGEPLANE_REQUEST_TERMS,
    operations=(IMAGEPLANE_OPERATION_SPEC,),
    max_plan_bytes=MAX_IMAGEPLANE_PLAN_BYTES,
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


def _assert_closed_graph(graph: object) -> None:
    if (
        graph.extensions
        or any(item.extension_ids for item in graph.bodies)
        or any(
            parameter.extension_ids
            or parameter.expression is not None
            or parameter.value.extension_ids
            for parameter in graph.parameters
        )
        or any(reference.extension_ids for reference in graph.references)
        or any(
            node.extension_ids
            or node.intent.extension_ids
            or node.intent.dependencies
            or any(port.extension_ids for port in node.intent.input_ports)
            or any(result.extension_ids for result in node.results)
            for node in graph.nodes
        )
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/extensions")


def _target_node(
    node: FeatureNodeV2,
    terms: dict[str, SemanticTermRefV2],
) -> bool:
    return (
        _matches(terms, node.intent.structural_kind_term_ref_id, IMAGEPLANE_STRUCTURE_TERM)
        and _matches(terms, node.intent.family_term_ref_id, IMAGEPLANE_FAMILY_TERM)
        and _matches(terms, node.intent.operation_term_ref_id, IMAGEPLANE_OPERATION_TERM)
    )


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
            or len(graph.references) != 1
            or len(graph.nodes) != 1
            or len(graph.graph_results) != 1
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")
        _assert_closed_graph(graph)
        terms = {item.term_ref_id: item for item in graph.terms}
        if len(graph.terms) != len(IMAGEPLANE_PFG_TERMS) or any(
            sum(_identity(item) == _identity(expected) for item in graph.terms) != 1
            for expected in IMAGEPLANE_PFG_TERMS
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/terms")
        candidates = tuple(node for node in graph.nodes if _target_node(node, terms))
        if len(candidates) != 1:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/target")
        target = candidates[0]
        body = graph.bodies[0]
        parameter = graph.parameters[0]
        reference = graph.references[0]
        if target.body_id != body.body_id:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/body")
        if (
            not _matches(
                terms,
                parameter.semantic_role_term_ref_id,
                IMAGEPLANE_CONFIGURATION_ROLE_TERM,
            )
            or not _matches(
                terms,
                parameter.value.value_type_term_ref_id,
                IMAGEPLANE_CONFIGURATION_TYPE_TERM,
            )
            or not _matches(
                terms,
                parameter.value.encoding_term_ref_id,
                IMAGEPLANE_CANONICAL_JSON_ENCODING_TERM,
            )
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/parameter")
        configuration_bytes = encode_imageplane_configuration(parameter.value.value)
        if configuration_bytes != parameter.value.canonical_value:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/parameter/value")
        if (
            reference.scope is not SemanticReferenceScope.EXTERNAL
            or not _matches(
                terms,
                reference.semantic_role_term_ref_id,
                IMAGEPLANE_ARTIFACT_ROLE_TERM,
            )
            or not _matches(
                terms,
                reference.value_type_term_ref_id,
                IMAGEPLANE_ARTIFACT_TYPE_TERM,
            )
            or not _matches(
                terms,
                reference.locator_term_ref_id,
                IMAGEPLANE_ARTIFACT_LOCATOR_TERM,
            )
            or reference.source_content_sha256 is None
            or reference.source_node_id is not None
            or reference.source_geometry_id is not None
            or reference.occurrence_path
            or reference.qualifier_term_ref_ids
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/reference")
        if (
            len(target.intent.input_ports) != 2
            or len(target.intent.parameter_bindings) != 1
            or len(target.intent.references) != 1
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/input")
        ports = {item.port_id: item for item in target.intent.input_ports}
        parameter_binding = target.intent.parameter_bindings[0]
        reference_binding = target.intent.references[0]
        parameter_port = ports.get(parameter_binding.port_id)
        reference_port = ports.get(reference_binding.port_id)
        if (
            parameter_port is None
            or reference_port is None
            or parameter_port is reference_port
            or not _matches(
                terms,
                parameter_port.semantic_role_term_ref_id,
                IMAGEPLANE_CONFIGURATION_ROLE_TERM,
            )
            or not _matches(
                terms,
                parameter_port.value_type_term_ref_id,
                IMAGEPLANE_CONFIGURATION_TYPE_TERM,
            )
            or not _matches(
                terms,
                reference_port.semantic_role_term_ref_id,
                IMAGEPLANE_ARTIFACT_ROLE_TERM,
            )
            or not _matches(
                terms,
                reference_port.value_type_term_ref_id,
                IMAGEPLANE_ARTIFACT_TYPE_TERM,
            )
            or any(
                port.minimum_cardinality != 1 or port.maximum_cardinality != 1 or port.ordered
                for port in (parameter_port, reference_port)
            )
            or parameter_binding.parameter_id != parameter.parameter_id
            or parameter_binding.ordinal != 0
            or reference_binding.reference_id != reference.reference_id
            or reference_binding.ordinal != 0
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/input")
        if len(target.results) != 1:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/result")
        result = target.results[0]
        graph_result = graph.graph_results[0]
        if (
            not _matches(
                terms,
                result.semantic_role_term_ref_id,
                IMAGEPLANE_RESULT_ROLE_TERM,
            )
            or not _matches(
                terms,
                result.value_type_term_ref_id,
                IMAGEPLANE_RESULT_TYPE_TERM,
            )
            or graph_result.node_id != target.node_id
            or graph_result.result_id != result.result_id
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/result")
        operation_spec = manifest.operation_for_term(_as_bridge(IMAGEPLANE_OPERATION_TERM))
        if operation_spec is None:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/operation")
        configuration = parameter.value.value
        artifact_spec = IMAGEPLANE_ARTIFACT_SPECS[configuration["media_type"]]
        plan = ImagePlaneBackendPlan(
            source_artifact_id=document.artifact_id,
            source_graph_id=graph.graph_id,
            source_graph_sha256=graph.graph_sha256,
            source_content_sha256=hashlib.sha256(payload).hexdigest(),
            lowering_request_sha256=request_digest,
            adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
            manifest_sha256=manifest.manifest_sha256,
            operation_specification_sha256=operation_spec.specification_sha256,
            container_id=body.body_id,
            node_id=target.node_id,
            result_id=result.result_id,
            artifact_id=reference.reference_id,
            artifact_content_sha256=reference.source_content_sha256,
            artifact_schema_term_ref_id=artifact_spec.schema_term_ref_id,
            artifact_media_type=artifact_spec.media_type,
            configuration_bytes=configuration_bytes,
        )
        return ReviewedPlanDraft(
            payload=plan.canonical_bytes,
            semantic_plan_sha256=plan.plan_sha256,
            operation_term=_as_bridge(IMAGEPLANE_OPERATION_TERM),
            subjects=(
                SubjectRef(
                    artifact_id=document.artifact_id,
                    selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
                    selector_id=target.node_id,
                ),
            ),
        )
    except IntentBridgeError:
        raise
    except ImagePlaneRuleError:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/configuration")
    except ParametricFeatureGraphError:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_document")
    except (Exception, SystemExit):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_document")


def _decode_plan(
    payload: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
) -> ImagePlaneBackendPlan:
    return decode_imageplane_backend_plan(
        payload,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )


def _validate_binding(
    decoded: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    if type(decoded) is not ImagePlaneBackendPlan:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan")
    if (
        operation.operation_id != "place_or_edit_image_plane"
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


def build_imageplane_capability_document(
    *,
    artifact_id: str = "artifact_freecad_imageplane_capability",
) -> tuple[DocumentRef, bytes]:
    return IMAGEPLANE_MANIFEST.capability_document(artifact_id=artifact_id)


def build_imageplane_artifact_document(
    payload: bytes,
    *,
    media_type: str,
    artifact_id: str = "artifact_imageplane_source",
) -> DocumentRef:
    try:
        validate_imageplane_artifact_payload(payload, media_type)
    except ImagePlaneRuleError:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/artifact")
    digest = hashlib.sha256(payload).hexdigest()
    artifact_spec = IMAGEPLANE_ARTIFACT_SPECS.get(media_type)
    if artifact_spec is None:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/artifact/media_type")
    return DocumentRef(
        artifact_id=artifact_id,
        role_term_ref_id=IMAGEPLANE_ARTIFACT_ROLE_TERM_REF_ID,
        schema_term_ref_id=artifact_spec.schema_term_ref_id,
        document_id=f"imageplane_{digest[:32]}",
        document_digest=digest,
        content_sha256=digest,
        size_bytes=len(payload),
        media_type=artifact_spec.media_type,
    )


class FreeCADImagePlaneAdapter(ExactReviewedFamilyAdapter):
    """Private reviewed lowerer for one authenticated reference image plane."""

    __slots__ = ()

    def __init__(self, sink: PlanSink) -> None:
        super().__init__(
            IMAGEPLANE_MANIFEST,
            sink,
            build_plan=_build_plan,
            decode_plan=_decode_plan,
            validate_binding=_validate_binding,
        )


__all__ = [
    "FREECAD_IMAGEPLANE_ADAPTER_DESCRIPTOR",
    "IMAGEPLANE_ARTIFACT_LOCATOR_TERM",
    "IMAGEPLANE_ARTIFACT_ROLE_TERM",
    "IMAGEPLANE_ARTIFACT_TYPE_TERM",
    "IMAGEPLANE_CANONICAL_JSON_ENCODING_TERM",
    "IMAGEPLANE_CAPABILITY_ROLE_TERM",
    "IMAGEPLANE_CAPABILITY_SCHEMA_TERM",
    "IMAGEPLANE_CONFIGURATION_ROLE_TERM",
    "IMAGEPLANE_CONFIGURATION_TYPE_TERM",
    "IMAGEPLANE_FAMILY_TERM",
    "IMAGEPLANE_INTENT_ROLE_TERM",
    "IMAGEPLANE_MANIFEST",
    "IMAGEPLANE_OPERATION_SPEC",
    "IMAGEPLANE_OPERATION_TERM",
    "IMAGEPLANE_PFG_TERMS",
    "IMAGEPLANE_PLAN_ROLE_TERM",
    "IMAGEPLANE_PLAN_SCHEMA_TERM",
    "IMAGEPLANE_REQUEST_TERMS",
    "IMAGEPLANE_RESULT_ROLE_TERM",
    "IMAGEPLANE_RESULT_TYPE_TERM",
    "IMAGEPLANE_STRUCTURE_TERM",
    "FreeCADImagePlaneAdapter",
    "build_imageplane_artifact_document",
    "build_imageplane_capability_document",
]
