"""Private reviewed lowering for three backend-neutral Part dress-up intents.

The graph describes a solid dependency, a stable semantic edge/face role, and
one magnitude.  It contains neither a native type nor a topological index.  A
complete semantic identity selects one entry in this trusted static rule table;
the native rule later resolves that role uniquely against the live shape.
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
    FeatureInputPortV2,
    FeatureNodeV2,
    ParametricFeatureGraphV2,
    SemanticReferenceScope,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_part_dressup_rules import (
    MAX_PART_DRESSUP_PLAN_BYTES,
    PART_DRESSUP_FREECAD_ENGINE_BUILD_ID,
    PART_DRESSUP_NATIVE_PROPERTIES,
    PART_DRESSUP_NATIVE_TYPE_IDS,
    PART_DRESSUP_PLAN_MEDIA_TYPE,
    PART_DRESSUP_RULE_CONTRACT_SHA256,
    PART_DRESSUP_RULE_ID,
    PartDressupBackendPlan,
    PartDressupOperation,
    PartDressupSelectionRole,
    decode_part_dressup_backend_plan,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.parametric-dressup"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.parametric-dressup-ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.part-dressup-adapter.v1\0"
_MANIFEST_BUILD_ID: Final = hashlib.sha256(
    b"FreeCAD-build\0" + PART_DRESSUP_FREECAD_ENGINE_BUILD_ID.encode("ascii")
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


PART_DRESSUP_INTENT_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_part_dressup_parametric_intent", "document-role.parametric-intent"
)
PART_DRESSUP_CAPABILITY_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_part_dressup_capability", "document-role.reviewed-dressup-capability"
)
PART_DRESSUP_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_part_dressup_capability_v1",
    "document-schema.reviewed-dressup-capability-v1",
)
PART_DRESSUP_PLAN_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_part_dressup_backend_plan", "document-role.reviewed-backend-plan"
)
PART_DRESSUP_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_part_dressup_plan_v1", "document-schema.reviewed-part-dressup-plan-v1"
)

PART_DRESSUP_REFERENCE_STRUCTURE_TERM: Final = _pfg_term(
    "structure_part_dressup_source_reference", "structure.document-reference-feature"
)
PART_DRESSUP_REFERENCE_FAMILY_TERM: Final = _pfg_term(
    "family_part_dressup_source_reference", "feature-family.reference"
)
PART_DRESSUP_REFERENCE_OPERATION_TERM: Final = _pfg_term(
    "operation_part_dressup_existing_solid", "operation.existing-solid-reference"
)
PART_DRESSUP_TARGET_STRUCTURE_TERM: Final = _pfg_term(
    "structure_part_dressup_unary", "structure.unary-feature"
)
PART_DRESSUP_FAMILY_TERM: Final = _pfg_term(
    "family_part_dressup", "feature-family.dressup"
)
PART_DRESSUP_FILLET_OPERATION_TERM: Final = _pfg_term(
    "operation_part_dressup_edge_fillet",
    "operation.edge-fillet-single-constant-radius",
)
PART_DRESSUP_CHAMFER_OPERATION_TERM: Final = _pfg_term(
    "operation_part_dressup_edge_chamfer",
    "operation.edge-chamfer-single-equal-distance",
)
PART_DRESSUP_THICKNESS_OPERATION_TERM: Final = _pfg_term(
    "operation_part_dressup_face_thickness",
    "operation.face-thickness-single-skin-arc",
)

PART_DRESSUP_SOURCE_PORT_ROLE_TERM: Final = _pfg_term(
    "role_part_dressup_source_solid", "input-role.source-solid"
)
PART_DRESSUP_SELECTION_PORT_ROLE_TERM: Final = _pfg_term(
    "role_part_dressup_semantic_selection", "input-role.semantic-selection"
)
PART_DRESSUP_MAGNITUDE_ROLE_TERM: Final = _pfg_term(
    "role_part_dressup_magnitude", "input-role.dressup-magnitude"
)
PART_DRESSUP_SOLID_TYPE_TERM: Final = _pfg_term(
    "type_part_dressup_manifold_solid", "value-type.manifold-solid"
)
PART_DRESSUP_EDGE_REFERENCE_TYPE_TERM: Final = _pfg_term(
    "type_part_dressup_edge_reference", "value-type.semantic-edge-reference"
)
PART_DRESSUP_FACE_REFERENCE_TYPE_TERM: Final = _pfg_term(
    "type_part_dressup_face_reference", "value-type.semantic-face-reference"
)
PART_DRESSUP_LENGTH_TYPE_TERM: Final = _pfg_term(
    "type_part_dressup_length_mm", "value-type.length-mm"
)
PART_DRESSUP_SCALAR_JSON_TERM: Final = _pfg_term(
    "encoding_part_dressup_scalar_json", "value-encoding.canonical-json-scalar"
)

PART_DRESSUP_SOURCE_SOLID_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_part_dressup_source_solid", "result-role.source-solid"
)
PART_DRESSUP_SOURCE_EDGE_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_part_dressup_edge_selection", "result-role.semantic-edge-selection"
)
PART_DRESSUP_SOURCE_FACE_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_part_dressup_face_selection", "result-role.semantic-face-selection"
)
PART_DRESSUP_RESULT_SOLID_ROLE_TERM: Final = _pfg_term(
    "role_result_part_dressup_solid", "result-role.dressed-solid"
)
PART_DRESSUP_REFERENCE_ROLE_TERM: Final = _pfg_term(
    "role_reference_part_dressup_selection", "reference-role.dressup-selection"
)
PART_DRESSUP_EDGE_LOCATOR_TERM: Final = _pfg_term(
    "locator_part_dressup_outer_max_xy_parallel_z",
    "locator.outer-max-x-max-y-parallel-z",
)
PART_DRESSUP_FACE_LOCATOR_TERM: Final = _pfg_term(
    "locator_part_dressup_outer_max_z_planar_face",
    "locator.outer-max-z-planar-face",
)


@dataclass(frozen=True, slots=True)
class _OperationTerms:
    operation: PartDressupOperation
    operation_term: SemanticTermRefV2
    selection_result_role: SemanticTermRefV2
    selection_value_type: SemanticTermRefV2
    locator_term: SemanticTermRefV2
    selection_role: PartDressupSelectionRole
    native_operation: str


PART_DRESSUP_OPERATION_TERMS: Final = (
    _OperationTerms(
        PartDressupOperation.EDGE_FILLET,
        PART_DRESSUP_FILLET_OPERATION_TERM,
        PART_DRESSUP_SOURCE_EDGE_RESULT_ROLE_TERM,
        PART_DRESSUP_EDGE_REFERENCE_TYPE_TERM,
        PART_DRESSUP_EDGE_LOCATOR_TERM,
        PartDressupSelectionRole.OUTER_MAX_X_MAX_Y_PARALLEL_Z,
        "Fillet",
    ),
    _OperationTerms(
        PartDressupOperation.EDGE_CHAMFER,
        PART_DRESSUP_CHAMFER_OPERATION_TERM,
        PART_DRESSUP_SOURCE_EDGE_RESULT_ROLE_TERM,
        PART_DRESSUP_EDGE_REFERENCE_TYPE_TERM,
        PART_DRESSUP_EDGE_LOCATOR_TERM,
        PartDressupSelectionRole.OUTER_MAX_X_MAX_Y_PARALLEL_Z,
        "Chamfer",
    ),
    _OperationTerms(
        PartDressupOperation.FACE_THICKNESS,
        PART_DRESSUP_THICKNESS_OPERATION_TERM,
        PART_DRESSUP_SOURCE_FACE_RESULT_ROLE_TERM,
        PART_DRESSUP_FACE_REFERENCE_TYPE_TERM,
        PART_DRESSUP_FACE_LOCATOR_TERM,
        PartDressupSelectionRole.OUTER_MAX_Z_PLANAR_FACE,
        "Thickness",
    ),
)

PART_DRESSUP_PFG_TERMS: Final = (
    PART_DRESSUP_REFERENCE_STRUCTURE_TERM,
    PART_DRESSUP_REFERENCE_FAMILY_TERM,
    PART_DRESSUP_REFERENCE_OPERATION_TERM,
    PART_DRESSUP_TARGET_STRUCTURE_TERM,
    PART_DRESSUP_FAMILY_TERM,
    PART_DRESSUP_FILLET_OPERATION_TERM,
    PART_DRESSUP_CHAMFER_OPERATION_TERM,
    PART_DRESSUP_THICKNESS_OPERATION_TERM,
    PART_DRESSUP_SOURCE_PORT_ROLE_TERM,
    PART_DRESSUP_SELECTION_PORT_ROLE_TERM,
    PART_DRESSUP_MAGNITUDE_ROLE_TERM,
    PART_DRESSUP_SOLID_TYPE_TERM,
    PART_DRESSUP_EDGE_REFERENCE_TYPE_TERM,
    PART_DRESSUP_FACE_REFERENCE_TYPE_TERM,
    PART_DRESSUP_LENGTH_TYPE_TERM,
    PART_DRESSUP_SCALAR_JSON_TERM,
    PART_DRESSUP_SOURCE_SOLID_RESULT_ROLE_TERM,
    PART_DRESSUP_SOURCE_EDGE_RESULT_ROLE_TERM,
    PART_DRESSUP_SOURCE_FACE_RESULT_ROLE_TERM,
    PART_DRESSUP_RESULT_SOLID_ROLE_TERM,
    PART_DRESSUP_REFERENCE_ROLE_TERM,
    PART_DRESSUP_EDGE_LOCATOR_TERM,
    PART_DRESSUP_FACE_LOCATOR_TERM,
)

_ADAPTER_CONTRACT_SHA256: Final = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            PART_DRESSUP_RULE_ID.encode("ascii"),
            PART_DRESSUP_RULE_CONTRACT_SHA256.encode("ascii"),
            b"canonical-pfg-v2;live-semantic-selection;shared-reviewed-family-v1;no-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in (
                    PART_DRESSUP_INTENT_DOCUMENT_ROLE_TERM,
                    PART_DRESSUP_CAPABILITY_DOCUMENT_ROLE_TERM,
                    PART_DRESSUP_CAPABILITY_SCHEMA_TERM,
                    PART_DRESSUP_PLAN_DOCUMENT_ROLE_TERM,
                    PART_DRESSUP_PLAN_SCHEMA_TERM,
                    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
                    PFG_SELECTOR_FEATURE_NODE,
                    *(_as_bridge(term) for term in PART_DRESSUP_PFG_TERMS),
                )
            ),
        )
    )
).hexdigest()

FREECAD_PART_DRESSUP_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_part_dressup_adapter",
    adapter_version="1.0.0",
    adapter_contract_sha256=_ADAPTER_CONTRACT_SHA256,
)

PART_DRESSUP_OPERATION_SPECS: Final = tuple(
    ReviewedOperationSpec(
        operation_id=terms.operation.value,
        semantic_term=_as_bridge(terms.operation_term),
        native_type_id=PART_DRESSUP_NATIVE_TYPE_IDS[terms.operation],
        native_operation=terms.native_operation,
        native_property_names=PART_DRESSUP_NATIVE_PROPERTIES[terms.operation],
    )
    for terms in PART_DRESSUP_OPERATION_TERMS
)

PART_DRESSUP_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    PART_DRESSUP_INTENT_DOCUMENT_ROLE_TERM,
    PART_DRESSUP_CAPABILITY_DOCUMENT_ROLE_TERM,
    PART_DRESSUP_CAPABILITY_SCHEMA_TERM,
    PART_DRESSUP_PLAN_DOCUMENT_ROLE_TERM,
    PART_DRESSUP_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in PART_DRESSUP_PFG_TERMS),
)

PART_DRESSUP_MANIFEST: Final = FamilyBatchManifest(
    family_id="part_dressup",
    family_version="1.0.0",
    adapter=FREECAD_PART_DRESSUP_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=_MANIFEST_BUILD_ID,
    rule_id=PART_DRESSUP_RULE_ID,
    rule_contract_sha256=PART_DRESSUP_RULE_CONTRACT_SHA256,
    intent_role_term=PART_DRESSUP_INTENT_DOCUMENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=PART_DRESSUP_CAPABILITY_DOCUMENT_ROLE_TERM,
    capability_schema_term=PART_DRESSUP_CAPABILITY_SCHEMA_TERM,
    capability_media_type="application/vnd.vibecad.part-dressup-capability+json",
    plan_role_term=PART_DRESSUP_PLAN_DOCUMENT_ROLE_TERM,
    plan_schema_term=PART_DRESSUP_PLAN_SCHEMA_TERM,
    plan_media_type=PART_DRESSUP_PLAN_MEDIA_TYPE,
    request_terms=PART_DRESSUP_REQUEST_TERMS,
    operations=PART_DRESSUP_OPERATION_SPECS,
    max_plan_bytes=MAX_PART_DRESSUP_PLAN_BYTES,
)


def build_part_dressup_capability_document() -> tuple[DocumentRef, bytes]:
    """Return the exact content-addressed three-spec capability manifest."""

    return PART_DRESSUP_MANIFEST.capability_document(
        artifact_id="artifact_freecad_part_dressup_capability"
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


def _matches_node_kind(
    node: FeatureNodeV2,
    terms: dict[str, SemanticTermRefV2],
    structural: SemanticTermRefV2,
    family: SemanticTermRefV2,
    operation: SemanticTermRefV2,
) -> bool:
    actual = tuple(
        terms.get(item)
        for item in (
            node.intent.structural_kind_term_ref_id,
            node.intent.family_term_ref_id,
            node.intent.operation_term_ref_id,
        )
    )
    return all(item is not None for item in actual) and tuple(
        _identity(item) for item in actual
    ) == tuple(_identity(item) for item in (structural, family, operation))


def _operation_for_graph(
    graph: ParametricFeatureGraphV2,
    terms: dict[str, SemanticTermRefV2],
) -> tuple[FeatureNodeV2, _OperationTerms]:
    matches = tuple(
        (node, item)
        for node in graph.nodes
        for item in PART_DRESSUP_OPERATION_TERMS
        if _matches_node_kind(
            node,
            terms,
            PART_DRESSUP_TARGET_STRUCTURE_TERM,
            PART_DRESSUP_FAMILY_TERM,
            item.operation_term,
        )
    )
    if len(matches) != 1:
        _fail("/graph/operation")
    return matches[0]


def _assert_no_extensions(graph: ParametricFeatureGraphV2) -> None:
    if (
        graph.extensions
        or any(item.extension_ids for item in graph.bodies)
        or any(
            item.extension_ids
            or item.value.extension_ids
            or item.expression is not None
            for item in graph.parameters
        )
        or any(
            item.extension_ids or item.occurrence_path or item.qualifier_term_ref_ids
            for item in graph.references
        )
        or any(
            node.extension_ids
            or node.intent.extension_ids
            or any(item.extension_ids for item in node.intent.input_ports)
            or any(item.extension_ids for item in node.results)
            for node in graph.nodes
        )
    ):
        _fail("/graph/extensions")


def _exact_port(
    node: FeatureNodeV2,
    terms: dict[str, SemanticTermRefV2],
    role: SemanticTermRefV2,
    value_type: SemanticTermRefV2,
    path: str,
) -> FeatureInputPortV2:
    matches = tuple(
        item
        for item in node.intent.input_ports
        if terms.get(item.semantic_role_term_ref_id) is not None
        and terms.get(item.value_type_term_ref_id) is not None
        and _identity(terms[item.semantic_role_term_ref_id]) == _identity(role)
        and _identity(terms[item.value_type_term_ref_id]) == _identity(value_type)
    )
    if len(matches) != 1:
        _fail(path)
    port = matches[0]
    if (
        port.minimum_cardinality != 1
        or port.maximum_cardinality != 1
        or port.ordered
    ):
        _fail(path)
    return port


def _exact_result(
    node: FeatureNodeV2,
    terms: dict[str, SemanticTermRefV2],
    role: SemanticTermRefV2,
    value_type: SemanticTermRefV2,
    path: str,
):
    matches = tuple(
        item
        for item in node.results
        if terms.get(item.semantic_role_term_ref_id) is not None
        and terms.get(item.value_type_term_ref_id) is not None
        and _identity(terms[item.semantic_role_term_ref_id]) == _identity(role)
        and _identity(terms[item.value_type_term_ref_id]) == _identity(value_type)
    )
    if len(matches) != 1:
        _fail(path)
    return matches[0]


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
        or len(graph.nodes) != 2
        or len(graph.parameters) != 1
        or len(graph.references) != 1
        or len(graph.graph_results) != 1
    ):
        _fail("/graph/scope")
    _assert_no_extensions(graph)
    terms = {item.term_ref_id: item for item in graph.terms}
    if (
        len(graph.terms) != len(PART_DRESSUP_PFG_TERMS)
        or any(
            sum(_identity(item) == _identity(expected) for item in graph.terms) != 1
            for expected in PART_DRESSUP_PFG_TERMS
        )
    ):
        _fail("/graph/terms")

    target, operation_terms = _operation_for_graph(graph, terms)
    sources = tuple(
        node
        for node in graph.nodes
        if _matches_node_kind(
            node,
            terms,
            PART_DRESSUP_REFERENCE_STRUCTURE_TERM,
            PART_DRESSUP_REFERENCE_FAMILY_TERM,
            PART_DRESSUP_REFERENCE_OPERATION_TERM,
        )
    )
    if len(sources) != 1:
        _fail("/graph/source")
    source = sources[0]
    body = graph.bodies[0]
    if source is target or source.body_id != body.body_id or target.body_id != body.body_id:
        _fail("/graph/body")
    if (
        source.intent.input_ports
        or source.intent.dependencies
        or source.intent.references
        or source.intent.parameter_bindings
        or len(source.results) != 2
        or len(target.results) != 1
        or len(target.intent.input_ports) != 3
        or len(target.intent.dependencies) != 1
        or len(target.intent.references) != 1
        or len(target.intent.parameter_bindings) != 1
    ):
        _fail("/graph/bindings")

    source_solid = _exact_result(
        source,
        terms,
        PART_DRESSUP_SOURCE_SOLID_RESULT_ROLE_TERM,
        PART_DRESSUP_SOLID_TYPE_TERM,
        "/graph/source/solid",
    )
    source_selection = _exact_result(
        source,
        terms,
        operation_terms.selection_result_role,
        operation_terms.selection_value_type,
        "/graph/source/selection",
    )
    result = _exact_result(
        target,
        terms,
        PART_DRESSUP_RESULT_SOLID_ROLE_TERM,
        PART_DRESSUP_SOLID_TYPE_TERM,
        "/graph/result",
    )
    source_port = _exact_port(
        target,
        terms,
        PART_DRESSUP_SOURCE_PORT_ROLE_TERM,
        PART_DRESSUP_SOLID_TYPE_TERM,
        "/graph/ports/source",
    )
    selection_port = _exact_port(
        target,
        terms,
        PART_DRESSUP_SELECTION_PORT_ROLE_TERM,
        operation_terms.selection_value_type,
        "/graph/ports/selection",
    )
    magnitude_port = _exact_port(
        target,
        terms,
        PART_DRESSUP_MAGNITUDE_ROLE_TERM,
        PART_DRESSUP_LENGTH_TYPE_TERM,
        "/graph/ports/magnitude",
    )

    dependency = target.intent.dependencies[0]
    reference_binding = target.intent.references[0]
    parameter_binding = target.intent.parameter_bindings[0]
    reference = graph.references[0]
    parameter = graph.parameters[0]
    if (
        dependency.port_id != source_port.port_id
        or dependency.upstream_node_id != source.node_id
        or dependency.upstream_result_id != source_solid.result_id
        or dependency.ordinal != 0
        or reference_binding.port_id != selection_port.port_id
        or reference_binding.reference_id != reference.reference_id
        or reference_binding.ordinal != 0
        or parameter_binding.port_id != magnitude_port.port_id
        or parameter_binding.parameter_id != parameter.parameter_id
        or parameter_binding.ordinal != 0
    ):
        _fail("/graph/bindings")
    if (
        reference.scope is not SemanticReferenceScope.FEATURE
        or reference.source_node_id != source.node_id
        or reference.source_geometry_id != source_selection.result_id
        or reference.source_content_sha256 is not None
    ):
        _fail("/graph/reference/source")
    _graph_term(
        terms,
        reference.semantic_role_term_ref_id,
        PART_DRESSUP_REFERENCE_ROLE_TERM,
        "/graph/reference/role",
    )
    _graph_term(
        terms,
        reference.value_type_term_ref_id,
        operation_terms.selection_value_type,
        "/graph/reference/type",
    )
    _graph_term(
        terms,
        reference.locator_term_ref_id,
        operation_terms.locator_term,
        "/graph/reference/locator",
    )
    _graph_term(
        terms,
        parameter.semantic_role_term_ref_id,
        PART_DRESSUP_MAGNITUDE_ROLE_TERM,
        "/graph/magnitude/role",
    )
    _graph_term(
        terms,
        parameter.value.value_type_term_ref_id,
        PART_DRESSUP_LENGTH_TYPE_TERM,
        "/graph/magnitude/type",
    )
    _graph_term(
        terms,
        parameter.value.encoding_term_ref_id,
        PART_DRESSUP_SCALAR_JSON_TERM,
        "/graph/magnitude/encoding",
    )
    magnitude = parameter.value.value
    if type(magnitude) not in {int, float}:
        _fail("/graph/magnitude/value")
    graph_result = graph.graph_results[0]
    if graph_result.node_id != target.node_id or graph_result.result_id != result.result_id:
        _fail("/graph/graph_results")

    try:
        plan = PartDressupBackendPlan(
            source_artifact_id=document.artifact_id,
            source_graph_id=graph.graph_id,
            source_graph_sha256=graph.graph_sha256,
            source_content_sha256=hashlib.sha256(payload).hexdigest(),
            lowering_request_sha256=request_digest,
            adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
            manifest_sha256=manifest.manifest_sha256,
            container_id=body.body_id,
            source_node_id=source.node_id,
            source_solid_result_id=source_solid.result_id,
            source_selection_result_id=source_selection.result_id,
            semantic_reference_id=reference.reference_id,
            target_node_id=target.node_id,
            target_result_id=result.result_id,
            operation=operation_terms.operation,
            selection_role=operation_terms.selection_role,
            magnitude_mm=magnitude,
        )
    except Exception:
        _fail("/graph/magnitude/value")
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
    if type(decoded) is not PartDressupBackendPlan:
        _fail("/plan_document/type")
    expected = next(
        item for item in PART_DRESSUP_OPERATION_TERMS if item.operation is decoded.operation
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
        or operation.native_type_id != PART_DRESSUP_NATIVE_TYPE_IDS[decoded.operation]
        or operation.native_operation != expected.native_operation
        or operation.native_property_names
        != tuple(sorted(PART_DRESSUP_NATIVE_PROPERTIES[decoded.operation]))
    ):
        _fail("/plan_document/binding")


class FreeCADPartDressupAdapter(ExactReviewedFamilyAdapter):
    """Shared exact adapter specialized by the three-spec dress-up manifest."""

    def __init__(self, sink: PlanSink) -> None:
        super().__init__(
            PART_DRESSUP_MANIFEST,
            sink,
            build_plan=_build_plan,
            decode_plan=decode_part_dressup_backend_plan,
            validate_binding=_validate_binding,
        )


__all__ = [
    "FREECAD_PART_DRESSUP_ADAPTER_DESCRIPTOR",
    "PART_DRESSUP_CAPABILITY_DOCUMENT_ROLE_TERM",
    "PART_DRESSUP_CAPABILITY_SCHEMA_TERM",
    "PART_DRESSUP_CHAMFER_OPERATION_TERM",
    "PART_DRESSUP_EDGE_LOCATOR_TERM",
    "PART_DRESSUP_EDGE_REFERENCE_TYPE_TERM",
    "PART_DRESSUP_FACE_LOCATOR_TERM",
    "PART_DRESSUP_FACE_REFERENCE_TYPE_TERM",
    "PART_DRESSUP_FAMILY_TERM",
    "PART_DRESSUP_FILLET_OPERATION_TERM",
    "PART_DRESSUP_INTENT_DOCUMENT_ROLE_TERM",
    "PART_DRESSUP_LENGTH_TYPE_TERM",
    "PART_DRESSUP_MAGNITUDE_ROLE_TERM",
    "PART_DRESSUP_MANIFEST",
    "PART_DRESSUP_OPERATION_SPECS",
    "PART_DRESSUP_OPERATION_TERMS",
    "PART_DRESSUP_PFG_TERMS",
    "PART_DRESSUP_PLAN_DOCUMENT_ROLE_TERM",
    "PART_DRESSUP_PLAN_SCHEMA_TERM",
    "PART_DRESSUP_REFERENCE_FAMILY_TERM",
    "PART_DRESSUP_REFERENCE_OPERATION_TERM",
    "PART_DRESSUP_REFERENCE_ROLE_TERM",
    "PART_DRESSUP_REFERENCE_STRUCTURE_TERM",
    "PART_DRESSUP_REQUEST_TERMS",
    "PART_DRESSUP_RESULT_SOLID_ROLE_TERM",
    "PART_DRESSUP_SCALAR_JSON_TERM",
    "PART_DRESSUP_SELECTION_PORT_ROLE_TERM",
    "PART_DRESSUP_SOLID_TYPE_TERM",
    "PART_DRESSUP_SOURCE_EDGE_RESULT_ROLE_TERM",
    "PART_DRESSUP_SOURCE_FACE_RESULT_ROLE_TERM",
    "PART_DRESSUP_SOURCE_PORT_ROLE_TERM",
    "PART_DRESSUP_SOURCE_SOLID_RESULT_ROLE_TERM",
    "PART_DRESSUP_TARGET_STRUCTURE_TERM",
    "PART_DRESSUP_THICKNESS_OPERATION_TERM",
    "FreeCADPartDressupAdapter",
    "build_part_dressup_capability_document",
]
