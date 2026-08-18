"""Exact PFGv2 lowering for the reviewed zero-source Sketch CREATE family.

This family is intentionally private and unregistered.  It introduces one new
semantic operation rather than reusing any of the twenty reviewed Sketch UPDATE
operations or planar-mechanical PM1.  Graph strings never select a native name
or type: ownership, plane, profile, and operation are matched by complete term
identity and copied into the canonical backend plan.
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
    FeatureBodyV2,
    FeatureGraphResultV2,
    FeatureInputPortV2,
    FeatureIntentV2,
    FeatureNodeV2,
    FeatureReferenceBindingV2,
    FeatureResultV2,
    ParametricFeatureGraphError,
    ParametricFeatureGraphV2,
    SemanticReferenceScope,
    SemanticReferenceV2,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_sketch_bootstrap_rules import (
    MAX_SKETCH_BOOTSTRAP_PLAN_BYTES,
    SKETCH_BOOTSTRAP_FREECAD_ENGINE_BUILD_ID,
    SKETCH_BOOTSTRAP_NATIVE_OPERATION,
    SKETCH_BOOTSTRAP_NATIVE_TYPE_ID,
    SKETCH_BOOTSTRAP_PLAN_MEDIA_TYPE,
    SKETCH_BOOTSTRAP_RULE_CONTRACT_SHA256,
    SKETCH_BOOTSTRAP_RULE_ID,
    SketchBootstrapBackendPlan,
    SketchBootstrapRuleError,
    SketchBootstrapSemanticIdentity,
    decode_sketch_bootstrap_backend_plan,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.freecad-sketch-bootstrap"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.freecad-sketch-bootstrap-ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.freecad-sketch-bootstrap-adapter.v1\0"


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


def _definition(term_id: str) -> str:
    return hashlib.sha256(
        b"\0".join(
            (
                _ONTOLOGY_DOMAIN,
                _ONTOLOGY_NAMESPACE.encode("ascii"),
                _ONTOLOGY_VERSION.encode("ascii"),
                term_id.encode("ascii"),
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


SKETCH_BOOTSTRAP_INTENT_ROLE_TERM: Final = _bridge_term(
    "role_sketch_bootstrap_intent", "document-role.sketch-bootstrap-intent"
)
SKETCH_BOOTSTRAP_CAPABILITY_ROLE_TERM: Final = _bridge_term(
    "role_sketch_bootstrap_capability", "document-role.sketch-bootstrap-capability"
)
SKETCH_BOOTSTRAP_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_sketch_bootstrap_capability_v1",
    "document-schema.sketch-bootstrap-capability-v1",
)
SKETCH_BOOTSTRAP_PLAN_ROLE_TERM: Final = _bridge_term(
    "role_sketch_bootstrap_plan", "document-role.sketch-bootstrap-plan"
)
SKETCH_BOOTSTRAP_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_sketch_bootstrap_plan_v1", "document-schema.sketch-bootstrap-plan-v1"
)

# Complete semantic identities.  Their ref ids are merely local graph handles.
SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM: Final = _pfg_term(
    "structure_body_owned_sketch", "ownership.partdesign-body-owned-sketch"
)
SKETCH_BOOTSTRAP_CLOSED_CIRCLE_PROFILE_TERM: Final = _pfg_term(
    "family_closed_circle_profile", "profile.closed-circle"
)
SKETCH_BOOTSTRAP_CREATE_OPERATION_TERM: Final = _pfg_term(
    "operation_create_body_owned_sketch", "operation.sketch.create-body-owned"
)
SKETCH_BOOTSTRAP_PLANE_ROLE_TERM: Final = _pfg_term(
    "role_support_plane", "input-role.support-plane"
)
SKETCH_BOOTSTRAP_PLANE_TYPE_TERM: Final = _pfg_term("type_origin_plane", "value-type.origin-plane")
SKETCH_BOOTSTRAP_XY_PLANE_TERM: Final = _pfg_term("locator_origin_xy_plane", "origin-plane.xy")
SKETCH_BOOTSTRAP_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_closed_profile_result", "result-role.closed-profile"
)
SKETCH_BOOTSTRAP_RESULT_TYPE_TERM: Final = _pfg_term(
    "type_body_owned_sketch_profile", "value-type.body-owned-sketch-profile"
)

SKETCH_BOOTSTRAP_PFG_TERMS: Final = (
    SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM,
    SKETCH_BOOTSTRAP_CLOSED_CIRCLE_PROFILE_TERM,
    SKETCH_BOOTSTRAP_CREATE_OPERATION_TERM,
    SKETCH_BOOTSTRAP_PLANE_ROLE_TERM,
    SKETCH_BOOTSTRAP_PLANE_TYPE_TERM,
    SKETCH_BOOTSTRAP_XY_PLANE_TERM,
    SKETCH_BOOTSTRAP_RESULT_ROLE_TERM,
    SKETCH_BOOTSTRAP_RESULT_TYPE_TERM,
)
SKETCH_BOOTSTRAP_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    SKETCH_BOOTSTRAP_INTENT_ROLE_TERM,
    SKETCH_BOOTSTRAP_CAPABILITY_ROLE_TERM,
    SKETCH_BOOTSTRAP_CAPABILITY_SCHEMA_TERM,
    SKETCH_BOOTSTRAP_PLAN_ROLE_TERM,
    SKETCH_BOOTSTRAP_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in SKETCH_BOOTSTRAP_PFG_TERMS),
)

_ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            SKETCH_BOOTSTRAP_RULE_ID.encode("ascii"),
            SKETCH_BOOTSTRAP_RULE_CONTRACT_SHA256.encode("ascii"),
            b"pfg-v2;one-node;zero-dependency;full-semantic-identity;"
            b"body-owned;origin-xy-plane;closed-circle;atomic-plan;no-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in SKETCH_BOOTSTRAP_REQUEST_TERMS
            ),
        )
    )
).hexdigest()

FREECAD_SKETCH_BOOTSTRAP_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_sketch_bootstrap_adapter",
    adapter_version="1.0.0",
    adapter_contract_sha256=_ADAPTER_CONTRACT_SHA256,
)

SKETCH_BOOTSTRAP_OPERATION_SPEC: Final = ReviewedOperationSpec(
    operation_id="create_body_owned_closed_circle",
    semantic_term=_as_bridge(SKETCH_BOOTSTRAP_CREATE_OPERATION_TERM),
    native_type_id=SKETCH_BOOTSTRAP_NATIVE_TYPE_ID,
    native_operation=SKETCH_BOOTSTRAP_NATIVE_OPERATION,
    native_property_names=(
        "AttachmentSupport",
        "Geometry",
        "MapMode",
        "OpenVertices",
    ),
)

SKETCH_BOOTSTRAP_FAMILY_MANIFEST: Final = FamilyBatchManifest(
    family_id="freecad_sketch_bootstrap",
    family_version="1.0.0",
    adapter=FREECAD_SKETCH_BOOTSTRAP_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=hashlib.sha256(
        SKETCH_BOOTSTRAP_FREECAD_ENGINE_BUILD_ID.encode("ascii")
    ).hexdigest(),
    rule_id=SKETCH_BOOTSTRAP_RULE_ID,
    rule_contract_sha256=SKETCH_BOOTSTRAP_RULE_CONTRACT_SHA256,
    intent_role_term=SKETCH_BOOTSTRAP_INTENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=SKETCH_BOOTSTRAP_CAPABILITY_ROLE_TERM,
    capability_schema_term=SKETCH_BOOTSTRAP_CAPABILITY_SCHEMA_TERM,
    capability_media_type="application/vnd.vibecad.freecad-sketch-bootstrap-capability+json",
    plan_role_term=SKETCH_BOOTSTRAP_PLAN_ROLE_TERM,
    plan_schema_term=SKETCH_BOOTSTRAP_PLAN_SCHEMA_TERM,
    plan_media_type=SKETCH_BOOTSTRAP_PLAN_MEDIA_TYPE,
    request_terms=SKETCH_BOOTSTRAP_REQUEST_TERMS,
    operations=(SKETCH_BOOTSTRAP_OPERATION_SPEC,),
    max_plan_bytes=MAX_SKETCH_BOOTSTRAP_PLAN_BYTES,
)


def _identity(term: object) -> tuple[str, str, str, str]:
    try:
        result = (
            term.namespace,
            term.vocabulary_version,
            term.term_id,
            term.term_definition_sha256,
        )
    except Exception:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/graph/terms")
    return result


def _semantic_identity(term: SemanticTermRefV2) -> SketchBootstrapSemanticIdentity:
    return SketchBootstrapSemanticIdentity(
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


def _graph_term(
    terms: dict[str, SemanticTermRefV2],
    ref_id: str,
    expected: SemanticTermRefV2,
    path: str,
) -> SemanticTermRefV2:
    term = terms.get(ref_id)
    if term is None or _identity(term) != _identity(expected):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)
    return term


def _assert_no_extensions(graph: ParametricFeatureGraphV2) -> None:
    if graph.extensions:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/extensions")
    elements = (
        *graph.terms,
        *graph.bodies,
        *graph.parameters,
        *graph.references,
        *graph.nodes,
    )
    if any(getattr(item, "extension_ids", ()) for item in elements):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/extensions")
    for node in graph.nodes:
        nested = (
            *node.intent.input_ports,
            *node.intent.references,
            *node.intent.dependencies,
            *node.intent.parameter_bindings,
            *node.results,
        )
        if node.intent.extension_ids or any(getattr(item, "extension_ids", ()) for item in nested):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/extensions")


def _build_plan(
    document: DocumentRef,
    payload: bytes,
    request_digest: str,
    manifest: FamilyBatchManifest,
) -> ReviewedPlanDraft:
    if manifest is not SKETCH_BOOTSTRAP_FAMILY_MANIFEST:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/manifest")
    try:
        graph = decode_parametric_feature_graph_v2(
            payload, expected_sha256=document.document_digest
        )
    except ParametricFeatureGraphError:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_document")
    if (
        graph.graph_id != document.document_id
        or len(graph.bodies) != 1
        or graph.parameters
        or len(graph.references) != 1
        or len(graph.nodes) != 1
        or len(graph.graph_results) != 1
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")
    _assert_no_extensions(graph)
    terms = {item.term_ref_id: item for item in graph.terms}
    for expected in SKETCH_BOOTSTRAP_PFG_TERMS:
        if sum(_identity(item) == _identity(expected) for item in graph.terms) != 1:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/terms")
    body = graph.bodies[0]
    target = graph.nodes[0]
    reference = graph.references[0]
    intent = target.intent
    ownership = _graph_term(
        terms,
        intent.structural_kind_term_ref_id,
        SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM,
        "/graph/ownership",
    )
    profile = _graph_term(
        terms,
        intent.family_term_ref_id,
        SKETCH_BOOTSTRAP_CLOSED_CIRCLE_PROFILE_TERM,
        "/graph/profile",
    )
    operation = _graph_term(
        terms,
        intent.operation_term_ref_id,
        SKETCH_BOOTSTRAP_CREATE_OPERATION_TERM,
        "/graph/operation",
    )
    plane = _graph_term(
        terms,
        reference.locator_term_ref_id,
        SKETCH_BOOTSTRAP_XY_PLANE_TERM,
        "/graph/plane",
    )
    if (
        target.body_id != body.body_id
        or len(intent.input_ports) != 1
        or intent.dependencies
        or intent.parameter_bindings
        or len(intent.references) != 1
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/source_count")
    port = intent.input_ports[0]
    binding = intent.references[0]
    _graph_term(
        terms,
        port.semantic_role_term_ref_id,
        SKETCH_BOOTSTRAP_PLANE_ROLE_TERM,
        "/graph/plane/role",
    )
    _graph_term(
        terms,
        port.value_type_term_ref_id,
        SKETCH_BOOTSTRAP_PLANE_TYPE_TERM,
        "/graph/plane/type",
    )
    _graph_term(
        terms,
        reference.semantic_role_term_ref_id,
        SKETCH_BOOTSTRAP_PLANE_ROLE_TERM,
        "/graph/reference/role",
    )
    _graph_term(
        terms,
        reference.value_type_term_ref_id,
        SKETCH_BOOTSTRAP_PLANE_TYPE_TERM,
        "/graph/reference/type",
    )
    if (
        port.minimum_cardinality != 1
        or port.maximum_cardinality != 1
        or port.ordered
        or binding.port_id != port.port_id
        or binding.reference_id != reference.reference_id
        or binding.ordinal != 0
        or reference.scope is not SemanticReferenceScope.ORIGIN
        or reference.source_node_id is not None
        or reference.source_geometry_id is not None
        or reference.source_content_sha256 is not None
        or reference.occurrence_path
        or reference.qualifier_term_ref_ids
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/plane")
    if len(target.results) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/result")
    result = target.results[0]
    _graph_term(
        terms,
        result.semantic_role_term_ref_id,
        SKETCH_BOOTSTRAP_RESULT_ROLE_TERM,
        "/graph/result/role",
    )
    _graph_term(
        terms,
        result.value_type_term_ref_id,
        SKETCH_BOOTSTRAP_RESULT_TYPE_TERM,
        "/graph/result/type",
    )
    graph_result = graph.graph_results[0]
    if graph_result.node_id != target.node_id or graph_result.result_id != result.result_id:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/graph_results")
    plan = SketchBootstrapBackendPlan(
        source_artifact_id=document.artifact_id,
        source_graph_id=graph.graph_id,
        source_graph_sha256=graph.graph_sha256,
        source_content_sha256=hashlib.sha256(payload).hexdigest(),
        lowering_request_sha256=request_digest,
        adapter_contract_sha256=FREECAD_SKETCH_BOOTSTRAP_ADAPTER_DESCRIPTOR.adapter_contract_sha256,
        body_id=body.body_id,
        node_id=target.node_id,
        result_id=result.result_id,
        operation_identity=_semantic_identity(operation),
        ownership_identity=_semantic_identity(ownership),
        plane_identity=_semantic_identity(plane),
        profile_identity=_semantic_identity(profile),
    )
    return ReviewedPlanDraft(
        payload=plan.canonical_bytes,
        semantic_plan_sha256=plan.plan_sha256,
        operation_term=_as_bridge(SKETCH_BOOTSTRAP_CREATE_OPERATION_TERM),
        subjects=(
            SubjectRef(
                artifact_id=document.artifact_id,
                selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
                selector_id=target.node_id,
            ),
        ),
    )


def _validate_plan_contract(
    plan: object,
    document: DocumentRef,
    operation: ReviewedOperationSpec,
) -> SketchBootstrapBackendPlan:
    expected_identities = (
        _semantic_identity(SKETCH_BOOTSTRAP_CREATE_OPERATION_TERM),
        _semantic_identity(SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM),
        _semantic_identity(SKETCH_BOOTSTRAP_XY_PLANE_TERM),
        _semantic_identity(SKETCH_BOOTSTRAP_CLOSED_CIRCLE_PROFILE_TERM),
    )
    if (
        type(plan) is not SketchBootstrapBackendPlan
        or type(document) is not DocumentRef
        or operation != SKETCH_BOOTSTRAP_OPERATION_SPEC
        or plan.source_count != 0
        or plan.adapter_contract_sha256
        != FREECAD_SKETCH_BOOTSTRAP_ADAPTER_DESCRIPTOR.adapter_contract_sha256
        or (
            plan.operation_identity,
            plan.ownership_identity,
            plan.plane_identity,
            plan.profile_identity,
        )
        != expected_identities
        or plan.plan_sha256 != document.document_digest
        or hashlib.sha256(plan.canonical_bytes).hexdigest() != document.content_sha256
        or len(plan.canonical_bytes) != document.size_bytes
    ):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan")
    try:
        decoded = decode_sketch_bootstrap_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=document.content_sha256,
            expected_plan_sha256=document.document_digest,
        )
    except SketchBootstrapRuleError:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan")
    if decoded != plan:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan")
    return plan


def validate_sketch_bootstrap_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    if (
        type(receipt) is not ReviewedPlanReceipt
        or receipt.operation != SKETCH_BOOTSTRAP_OPERATION_SPEC
        or operation != SKETCH_BOOTSTRAP_OPERATION_SPEC
        or receipt.manifest_sha256 != SKETCH_BOOTSTRAP_FAMILY_MANIFEST.manifest_sha256
        or receipt.adapter != FREECAD_SKETCH_BOOTSTRAP_ADAPTER_DESCRIPTOR
    ):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/receipt")
    checked = _validate_plan_contract(plan, receipt.plan_document, operation)
    if (
        checked.lowering_request_sha256 != receipt.request_digest
        or checked.source_artifact_id != receipt.source_document.artifact_id
        or checked.source_graph_id != receipt.source_document.document_id
        or checked.source_graph_sha256 != receipt.source_document.document_digest
        or checked.source_content_sha256 != receipt.source_document.content_sha256
    ):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/receipt/source")


def sketch_bootstrap_reviewed_adapter_factory(sink: PlanSink) -> ExactReviewedFamilyAdapter:
    return ExactReviewedFamilyAdapter(
        SKETCH_BOOTSTRAP_FAMILY_MANIFEST,
        sink,
        build_plan=_build_plan,
        decode_plan=decode_sketch_bootstrap_backend_plan,
        validate_binding=validate_sketch_bootstrap_reviewed_plan,
    )


def build_sketch_bootstrap_intent_graph(
    *,
    graph_id: str = "graph_sketch_bootstrap",
    body_id: str = "body_sketch_bootstrap",
    node_id: str = "node_sketch_bootstrap",
    result_id: str = "result_sketch_bootstrap",
) -> ParametricFeatureGraphV2:
    """Build the one canonical semantic shape; ids remain backend-neutral."""

    reference = SemanticReferenceV2(
        reference_id="reference_origin_xy_plane",
        scope=SemanticReferenceScope.ORIGIN,
        semantic_role_term_ref_id=SKETCH_BOOTSTRAP_PLANE_ROLE_TERM.term_ref_id,
        value_type_term_ref_id=SKETCH_BOOTSTRAP_PLANE_TYPE_TERM.term_ref_id,
        locator_term_ref_id=SKETCH_BOOTSTRAP_XY_PLANE_TERM.term_ref_id,
    )
    target = FeatureNodeV2(
        node_id=node_id,
        body_id=body_id,
        name="Reviewed body-owned closed Circle on semantic XY plane",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM.term_ref_id,
            family_term_ref_id=SKETCH_BOOTSTRAP_CLOSED_CIRCLE_PROFILE_TERM.term_ref_id,
            operation_term_ref_id=SKETCH_BOOTSTRAP_CREATE_OPERATION_TERM.term_ref_id,
            input_ports=(
                FeatureInputPortV2(
                    port_id="port_support_plane",
                    semantic_role_term_ref_id=SKETCH_BOOTSTRAP_PLANE_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=SKETCH_BOOTSTRAP_PLANE_TYPE_TERM.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
            ),
            references=(
                FeatureReferenceBindingV2(
                    binding_id="binding_support_plane",
                    port_id="port_support_plane",
                    reference_id=reference.reference_id,
                ),
            ),
        ),
        results=(
            FeatureResultV2(
                result_id=result_id,
                semantic_role_term_ref_id=SKETCH_BOOTSTRAP_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=SKETCH_BOOTSTRAP_RESULT_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id=graph_id,
        name="Reviewed Sketch CREATE bootstrap",
        terms=SKETCH_BOOTSTRAP_PFG_TERMS,
        bodies=(FeatureBodyV2(body_id=body_id, name="Semantic Body owner"),),
        parameters=(),
        references=(reference,),
        nodes=(target,),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection_sketch_bootstrap",
                node_id=node_id,
                result_id=result_id,
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class SketchBootstrapFormalHandoff:
    """Family-only handoff; this is evidence, never shared registration."""

    manifest_sha256: str
    operation_specification_sha256: str
    rule_contract_sha256: str
    blockers: tuple[str, ...]

    @property
    def shared_registration_ready(self) -> bool:
        return False


SKETCH_BOOTSTRAP_FORMAL_HANDOFF: Final = SketchBootstrapFormalHandoff(
    manifest_sha256=SKETCH_BOOTSTRAP_FAMILY_MANIFEST.manifest_sha256,
    operation_specification_sha256=SKETCH_BOOTSTRAP_OPERATION_SPEC.specification_sha256,
    rule_contract_sha256=SKETCH_BOOTSTRAP_RULE_CONTRACT_SHA256,
    blockers=(
        "shared-dispatcher-registration-not-in-family-scope",
        "formal-verification-receipt-pending",
        "intel-and-arm-release-attestation-refresh-pending",
    ),
)


__all__ = [
    "FREECAD_SKETCH_BOOTSTRAP_ADAPTER_DESCRIPTOR",
    "SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM",
    "SKETCH_BOOTSTRAP_CAPABILITY_ROLE_TERM",
    "SKETCH_BOOTSTRAP_CAPABILITY_SCHEMA_TERM",
    "SKETCH_BOOTSTRAP_CLOSED_CIRCLE_PROFILE_TERM",
    "SKETCH_BOOTSTRAP_CREATE_OPERATION_TERM",
    "SKETCH_BOOTSTRAP_FAMILY_MANIFEST",
    "SKETCH_BOOTSTRAP_FORMAL_HANDOFF",
    "SKETCH_BOOTSTRAP_INTENT_ROLE_TERM",
    "SKETCH_BOOTSTRAP_OPERATION_SPEC",
    "SKETCH_BOOTSTRAP_PFG_TERMS",
    "SKETCH_BOOTSTRAP_PLAN_ROLE_TERM",
    "SKETCH_BOOTSTRAP_PLAN_SCHEMA_TERM",
    "SKETCH_BOOTSTRAP_REQUEST_TERMS",
    "SKETCH_BOOTSTRAP_XY_PLANE_TERM",
    "SketchBootstrapFormalHandoff",
    "build_sketch_bootstrap_intent_graph",
    "sketch_bootstrap_reviewed_adapter_factory",
    "validate_sketch_bootstrap_reviewed_plan",
]
