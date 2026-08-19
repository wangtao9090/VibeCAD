"""Exact PFGv2 lowering for a source-bound FlatFace Sketch bootstrap."""

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
    FeatureDependencyV2,
    FeatureGraphResultV2,
    FeatureInputPortV2,
    FeatureIntentV2,
    FeatureNodeV2,
    FeatureResultV2,
    ParametricFeatureGraphError,
    ParametricFeatureGraphV2,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_sketch_flatface_bootstrap_rules import (
    FLATFACE_SKETCH_FREECAD_ENGINE_BUILD_ID,
    FLATFACE_SKETCH_NATIVE_OPERATION,
    FLATFACE_SKETCH_NATIVE_TYPE_ID,
    FLATFACE_SKETCH_PLAN_MEDIA_TYPE,
    FLATFACE_SKETCH_RULE_CONTRACT_SHA256,
    FLATFACE_SKETCH_RULE_ID,
    MAX_FLATFACE_SKETCH_PLAN_BYTES,
    FlatFaceSketchBackendPlan,
    FlatFaceSketchRuleError,
    FlatFaceSketchSemanticIdentity,
    decode_flatface_sketch_backend_plan,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.freecad-sketch-flatface-bootstrap"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.freecad-flatface-sketch-ontology.v1\0"
_ADAPTER_DOMAIN = b"vibecad.freecad-flatface-sketch-adapter.v1\0"


def _fail(
    path: str, code: IntentBridgeErrorCode = IntentBridgeErrorCode.AUTHORITY_VIOLATION
) -> None:
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


def _bridge(ref_id: str, term_id: str) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=ref_id,
        namespace=_ONTOLOGY_NAMESPACE,
        vocabulary_version=_ONTOLOGY_VERSION,
        term_id=term_id,
        term_definition_sha256=_definition(term_id),
    )


def _pfg(ref_id: str, term_id: str) -> SemanticTermRefV2:
    return SemanticTermRefV2(
        term_ref_id=ref_id,
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


FLATFACE_SKETCH_INTENT_ROLE_TERM: Final = _bridge(
    "role_flatface_sketch_intent", "document-role.flatface-sketch-intent"
)
FLATFACE_SKETCH_CAPABILITY_ROLE_TERM: Final = _bridge(
    "role_flatface_sketch_capability", "document-role.flatface-sketch-capability"
)
FLATFACE_SKETCH_CAPABILITY_SCHEMA_TERM: Final = _bridge(
    "schema_flatface_sketch_capability_v1", "document-schema.flatface-sketch-capability-v1"
)
FLATFACE_SKETCH_PLAN_ROLE_TERM: Final = _bridge(
    "role_flatface_sketch_plan", "document-role.flatface-sketch-plan"
)
FLATFACE_SKETCH_PLAN_SCHEMA_TERM: Final = _bridge(
    "schema_flatface_sketch_plan_v1", "document-schema.flatface-sketch-plan-v1"
)

FLATFACE_SKETCH_BODY_OWNERSHIP_TERM: Final = _pfg(
    "structure_existing_body_owned_sketch", "ownership.existing-partdesign-body-sketch"
)
FLATFACE_SKETCH_SELECTOR_TERM: Final = _pfg(
    "selector_unique_zmax_planar_face", "selector.unique-z-max-planar-face"
)
FLATFACE_SKETCH_CREATE_OPERATION_TERM: Final = _pfg(
    "operation_create_flatface_circle_sketch", "operation.sketch.create-flatface-circle"
)
FLATFACE_SKETCH_BASE_ROLE_TERM: Final = _pfg("role_base_solid", "input-role.base-solid")
FLATFACE_SKETCH_SOLID_TYPE_TERM: Final = _pfg("type_solid", "value-type.solid")
FLATFACE_SKETCH_SOLID_RESULT_ROLE_TERM: Final = _pfg("role_solid_result", "result-role.solid")
FLATFACE_SKETCH_PROFILE_TERM: Final = _pfg("role_closed_circle", "profile.closed-circle")
FLATFACE_SKETCH_RESULT_TYPE_TERM: Final = _pfg(
    "type_flatface_circle_sketch", "value-type.flatface-circular-sketch"
)
FLATFACE_SKETCH_SOURCE_STRUCTURE_TERM: Final = _pfg(
    "source_structure_feature", "source.structure.feature"
)
FLATFACE_SKETCH_SOURCE_FAMILY_TERM: Final = _pfg(
    "source_family_reviewed_solid", "source.family.reviewed-solid"
)
FLATFACE_SKETCH_SOURCE_OPERATION_TERM: Final = _pfg(
    "source_operation_existing", "source.operation.existing"
)

FLATFACE_SKETCH_PFG_TERMS: Final = (
    FLATFACE_SKETCH_BODY_OWNERSHIP_TERM,
    FLATFACE_SKETCH_SELECTOR_TERM,
    FLATFACE_SKETCH_CREATE_OPERATION_TERM,
    FLATFACE_SKETCH_BASE_ROLE_TERM,
    FLATFACE_SKETCH_SOLID_TYPE_TERM,
    FLATFACE_SKETCH_SOLID_RESULT_ROLE_TERM,
    FLATFACE_SKETCH_PROFILE_TERM,
    FLATFACE_SKETCH_RESULT_TYPE_TERM,
    FLATFACE_SKETCH_SOURCE_STRUCTURE_TERM,
    FLATFACE_SKETCH_SOURCE_FAMILY_TERM,
    FLATFACE_SKETCH_SOURCE_OPERATION_TERM,
)
FLATFACE_SKETCH_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    FLATFACE_SKETCH_INTENT_ROLE_TERM,
    FLATFACE_SKETCH_CAPABILITY_ROLE_TERM,
    FLATFACE_SKETCH_CAPABILITY_SCHEMA_TERM,
    FLATFACE_SKETCH_PLAN_ROLE_TERM,
    FLATFACE_SKETCH_PLAN_SCHEMA_TERM,
    *(_as_bridge(item) for item in FLATFACE_SKETCH_PFG_TERMS),
)

_ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_DOMAIN,
            FLATFACE_SKETCH_RULE_ID.encode("ascii"),
            FLATFACE_SKETCH_RULE_CONTRACT_SHA256.encode("ascii"),
            b"pfg-v2;two-nodes;exact-one-solid-dependency;same-body;"
            b"family-owned-unique-zmax-planar-face;no-native-subelement-in-plan;"
            b"closed-circle;content-bound-receipt;no-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in FLATFACE_SKETCH_REQUEST_TERMS
            ),
        )
    )
).hexdigest()

FREECAD_FLATFACE_SKETCH_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_sketch_flatface_bootstrap_adapter",
    adapter_version="1.0.0",
    adapter_contract_sha256=_ADAPTER_CONTRACT_SHA256,
)

FLATFACE_SKETCH_OPERATION_SPEC: Final = ReviewedOperationSpec(
    operation_id="create_closed_circle_on_unique_zmax_planar_face",
    semantic_term=_as_bridge(FLATFACE_SKETCH_CREATE_OPERATION_TERM),
    native_type_id=FLATFACE_SKETCH_NATIVE_TYPE_ID,
    native_operation=FLATFACE_SKETCH_NATIVE_OPERATION,
    native_property_names=("AttachmentSupport", "Geometry", "MapMode", "OpenVertices"),
)

FLATFACE_SKETCH_FAMILY_MANIFEST: Final = FamilyBatchManifest(
    family_id="freecad_sketch_flatface_bootstrap",
    family_version="1.0.0",
    adapter=FREECAD_FLATFACE_SKETCH_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=hashlib.sha256(
        FLATFACE_SKETCH_FREECAD_ENGINE_BUILD_ID.encode("ascii")
    ).hexdigest(),
    rule_id=FLATFACE_SKETCH_RULE_ID,
    rule_contract_sha256=FLATFACE_SKETCH_RULE_CONTRACT_SHA256,
    intent_role_term=FLATFACE_SKETCH_INTENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=FLATFACE_SKETCH_CAPABILITY_ROLE_TERM,
    capability_schema_term=FLATFACE_SKETCH_CAPABILITY_SCHEMA_TERM,
    capability_media_type="application/vnd.vibecad.freecad-sketch-flatface-bootstrap-capability+json",
    plan_role_term=FLATFACE_SKETCH_PLAN_ROLE_TERM,
    plan_schema_term=FLATFACE_SKETCH_PLAN_SCHEMA_TERM,
    plan_media_type=FLATFACE_SKETCH_PLAN_MEDIA_TYPE,
    request_terms=FLATFACE_SKETCH_REQUEST_TERMS,
    operations=(FLATFACE_SKETCH_OPERATION_SPEC,),
    max_plan_bytes=MAX_FLATFACE_SKETCH_PLAN_BYTES,
)


def _identity(term: object) -> tuple[str, str, str, str]:
    try:
        return (
            term.namespace,
            term.vocabulary_version,
            term.term_id,
            term.term_definition_sha256,
        )
    except Exception:
        _fail("/graph/terms", IntentBridgeErrorCode.INTEGRITY_FAILURE)


def _semantic(term: SemanticTermRefV2) -> FlatFaceSketchSemanticIdentity:
    return FlatFaceSketchSemanticIdentity(
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


def _term(
    terms: dict[str, SemanticTermRefV2],
    ref_id: str,
    expected: SemanticTermRefV2,
    path: str,
) -> SemanticTermRefV2:
    result = terms.get(ref_id)
    if result is None or _identity(result) != _identity(expected):
        _fail(path)
    return result


def _assert_closed(graph: ParametricFeatureGraphV2) -> None:
    if graph.extensions or graph.parameters or graph.references:
        _fail("/graph/extensions")
    elements = (*graph.terms, *graph.bodies, *graph.nodes)
    if any(getattr(item, "extension_ids", ()) for item in elements):
        _fail("/graph/extensions")
    for node in graph.nodes:
        nested = (*node.intent.input_ports, *node.intent.dependencies, *node.results)
        if (
            node.intent.references
            or node.intent.parameter_bindings
            or any(getattr(item, "extension_ids", ()) for item in nested)
        ):
            _fail("/graph/extensions")


def _build_plan(
    document: DocumentRef,
    payload: bytes,
    request_digest: str,
    manifest: FamilyBatchManifest,
) -> ReviewedPlanDraft:
    if manifest is not FLATFACE_SKETCH_FAMILY_MANIFEST:
        _fail("/manifest", IntentBridgeErrorCode.INTEGRITY_FAILURE)
    try:
        graph = decode_parametric_feature_graph_v2(
            payload, expected_sha256=document.document_digest
        )
    except ParametricFeatureGraphError:
        _fail("/intent_document", IntentBridgeErrorCode.INTEGRITY_FAILURE)
    if (
        graph.graph_id != document.document_id
        or len(graph.bodies) != 1
        or len(graph.nodes) != 2
        or len(graph.graph_results) != 1
    ):
        _fail("/graph/scope")
    _assert_closed(graph)
    terms = {item.term_ref_id: item for item in graph.terms}
    if any(
        sum(_identity(item) == _identity(expected) for item in graph.terms) != 1
        for expected in FLATFACE_SKETCH_PFG_TERMS
    ):
        _fail("/graph/terms")
    candidates = tuple(
        node
        for node in graph.nodes
        if _identity(terms.get(node.intent.operation_term_ref_id))
        == _identity(FLATFACE_SKETCH_CREATE_OPERATION_TERM)
    )
    if len(candidates) != 1:
        _fail("/graph/target")
    target = candidates[0]
    source = next((item for item in graph.nodes if item is not target), None)
    body = graph.bodies[0]
    if source is None or target.body_id != body.body_id or source.body_id != body.body_id:
        _fail("/graph/body")
    _term(
        terms,
        target.intent.structural_kind_term_ref_id,
        FLATFACE_SKETCH_BODY_OWNERSHIP_TERM,
        "/graph/ownership",
    )
    selector = _term(
        terms, target.intent.family_term_ref_id, FLATFACE_SKETCH_SELECTOR_TERM, "/graph/selector"
    )
    operation = _term(
        terms,
        target.intent.operation_term_ref_id,
        FLATFACE_SKETCH_CREATE_OPERATION_TERM,
        "/graph/operation",
    )
    _term(
        terms,
        source.intent.structural_kind_term_ref_id,
        FLATFACE_SKETCH_SOURCE_STRUCTURE_TERM,
        "/graph/source/structure",
    )
    _term(
        terms,
        source.intent.family_term_ref_id,
        FLATFACE_SKETCH_SOURCE_FAMILY_TERM,
        "/graph/source/family",
    )
    _term(
        terms,
        source.intent.operation_term_ref_id,
        FLATFACE_SKETCH_SOURCE_OPERATION_TERM,
        "/graph/source/operation",
    )
    if (
        source.intent.input_ports
        or source.intent.dependencies
        or len(source.results) != 1
        or len(target.intent.input_ports) != 1
        or len(target.intent.dependencies) != 1
        or len(target.results) != 1
    ):
        _fail("/graph/source_count")
    source_result = source.results[0]
    _term(
        terms,
        source_result.semantic_role_term_ref_id,
        FLATFACE_SKETCH_SOLID_RESULT_ROLE_TERM,
        "/graph/source/result/role",
    )
    _term(
        terms,
        source_result.value_type_term_ref_id,
        FLATFACE_SKETCH_SOLID_TYPE_TERM,
        "/graph/source/result/type",
    )
    port = target.intent.input_ports[0]
    dependency = target.intent.dependencies[0]
    _term(terms, port.semantic_role_term_ref_id, FLATFACE_SKETCH_BASE_ROLE_TERM, "/graph/base/role")
    _term(terms, port.value_type_term_ref_id, FLATFACE_SKETCH_SOLID_TYPE_TERM, "/graph/base/type")
    if (
        port.minimum_cardinality != 1
        or port.maximum_cardinality != 1
        or port.ordered
        or dependency.port_id != port.port_id
        or dependency.upstream_node_id != source.node_id
        or dependency.upstream_result_id != source_result.result_id
        or dependency.ordinal != 0
    ):
        _fail("/graph/base")
    result = target.results[0]
    profile = _term(
        terms,
        result.semantic_role_term_ref_id,
        FLATFACE_SKETCH_PROFILE_TERM,
        "/graph/result/profile",
    )
    _term(
        terms, result.value_type_term_ref_id, FLATFACE_SKETCH_RESULT_TYPE_TERM, "/graph/result/type"
    )
    graph_result = graph.graph_results[0]
    if graph_result.node_id != target.node_id or graph_result.result_id != result.result_id:
        _fail("/graph/result")
    plan = FlatFaceSketchBackendPlan(
        source_artifact_id=document.artifact_id,
        source_graph_id=graph.graph_id,
        source_graph_sha256=graph.graph_sha256,
        source_content_sha256=hashlib.sha256(payload).hexdigest(),
        lowering_request_sha256=request_digest,
        adapter_contract_sha256=FREECAD_FLATFACE_SKETCH_ADAPTER_DESCRIPTOR.adapter_contract_sha256,
        manifest_sha256=FLATFACE_SKETCH_FAMILY_MANIFEST.manifest_sha256,
        body_id=body.body_id,
        base_node_id=source.node_id,
        base_result_id=source_result.result_id,
        node_id=target.node_id,
        result_id=result.result_id,
        operation_identity=_semantic(operation),
        ownership_identity=_semantic(FLATFACE_SKETCH_BODY_OWNERSHIP_TERM),
        selector_identity=_semantic(selector),
        profile_identity=_semantic(profile),
    )
    return ReviewedPlanDraft(
        payload=plan.canonical_bytes,
        semantic_plan_sha256=plan.plan_sha256,
        operation_term=_as_bridge(FLATFACE_SKETCH_CREATE_OPERATION_TERM),
        subjects=(
            SubjectRef(
                artifact_id=document.artifact_id,
                selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
                selector_id=target.node_id,
            ),
        ),
    )


def validate_flatface_sketch_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    if (
        type(plan) is not FlatFaceSketchBackendPlan
        or type(receipt) is not ReviewedPlanReceipt
        or operation != FLATFACE_SKETCH_OPERATION_SPEC
        or receipt.operation != operation
        or receipt.manifest_sha256 != FLATFACE_SKETCH_FAMILY_MANIFEST.manifest_sha256
        or receipt.adapter != FREECAD_FLATFACE_SKETCH_ADAPTER_DESCRIPTOR
        or plan.manifest_sha256 != FLATFACE_SKETCH_FAMILY_MANIFEST.manifest_sha256
        or plan.adapter_contract_sha256
        != FREECAD_FLATFACE_SKETCH_ADAPTER_DESCRIPTOR.adapter_contract_sha256
        or plan.plan_sha256 != receipt.plan_document.document_digest
        or hashlib.sha256(plan.canonical_bytes).hexdigest() != receipt.plan_document.content_sha256
        or len(plan.canonical_bytes) != receipt.plan_document.size_bytes
        or (
            plan.operation_identity,
            plan.ownership_identity,
            plan.selector_identity,
            plan.profile_identity,
        )
        != (
            _semantic(FLATFACE_SKETCH_CREATE_OPERATION_TERM),
            _semantic(FLATFACE_SKETCH_BODY_OWNERSHIP_TERM),
            _semantic(FLATFACE_SKETCH_SELECTOR_TERM),
            _semantic(FLATFACE_SKETCH_PROFILE_TERM),
        )
    ):
        _fail("/receipt", IntentBridgeErrorCode.INTEGRITY_FAILURE)
    try:
        decoded = decode_flatface_sketch_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=receipt.plan_document.content_sha256,
            expected_plan_sha256=receipt.plan_document.document_digest,
        )
    except FlatFaceSketchRuleError:
        _fail("/plan", IntentBridgeErrorCode.INTEGRITY_FAILURE)
    if (
        decoded != plan
        or plan.lowering_request_sha256 != receipt.request_digest
        or plan.source_artifact_id != receipt.source_document.artifact_id
        or plan.source_graph_id != receipt.source_document.document_id
        or plan.source_graph_sha256 != receipt.source_document.document_digest
        or plan.source_content_sha256 != receipt.source_document.content_sha256
    ):
        _fail("/receipt/source", IntentBridgeErrorCode.INTEGRITY_FAILURE)


def flatface_sketch_reviewed_adapter_factory(sink: PlanSink) -> ExactReviewedFamilyAdapter:
    return ExactReviewedFamilyAdapter(
        FLATFACE_SKETCH_FAMILY_MANIFEST,
        sink,
        build_plan=_build_plan,
        decode_plan=decode_flatface_sketch_backend_plan,
        validate_binding=validate_flatface_sketch_reviewed_plan,
    )


def build_flatface_sketch_intent_graph(
    *,
    graph_id: str = "graph_flatface_sketch",
    body_id: str = "body_main",
    base_node_id: str = "node_base",
    base_result_id: str = "result_base",
    node_id: str = "node_flatface_sketch",
    result_id: str = "result_flatface_sketch",
) -> ParametricFeatureGraphV2:
    source = FeatureNodeV2(
        node_id=base_node_id,
        body_id=body_id,
        name="Reviewed same-run base solid",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=FLATFACE_SKETCH_SOURCE_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=FLATFACE_SKETCH_SOURCE_FAMILY_TERM.term_ref_id,
            operation_term_ref_id=FLATFACE_SKETCH_SOURCE_OPERATION_TERM.term_ref_id,
        ),
        results=(
            FeatureResultV2(
                result_id=base_result_id,
                semantic_role_term_ref_id=FLATFACE_SKETCH_SOLID_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=FLATFACE_SKETCH_SOLID_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    port = FeatureInputPortV2(
        port_id="port_base_solid",
        semantic_role_term_ref_id=FLATFACE_SKETCH_BASE_ROLE_TERM.term_ref_id,
        value_type_term_ref_id=FLATFACE_SKETCH_SOLID_TYPE_TERM.term_ref_id,
        minimum_cardinality=1,
        maximum_cardinality=1,
        ordered=False,
    )
    target = FeatureNodeV2(
        node_id=node_id,
        body_id=body_id,
        name="Closed Circle Sketch on unique z-max planar face",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=FLATFACE_SKETCH_BODY_OWNERSHIP_TERM.term_ref_id,
            family_term_ref_id=FLATFACE_SKETCH_SELECTOR_TERM.term_ref_id,
            operation_term_ref_id=FLATFACE_SKETCH_CREATE_OPERATION_TERM.term_ref_id,
            input_ports=(port,),
            dependencies=(
                FeatureDependencyV2(
                    dependency_id="dependency_base_solid",
                    port_id=port.port_id,
                    upstream_node_id=base_node_id,
                    upstream_result_id=base_result_id,
                ),
            ),
        ),
        results=(
            FeatureResultV2(
                result_id=result_id,
                semantic_role_term_ref_id=FLATFACE_SKETCH_PROFILE_TERM.term_ref_id,
                value_type_term_ref_id=FLATFACE_SKETCH_RESULT_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id=graph_id,
        name="Reviewed FlatFace Sketch bootstrap",
        terms=FLATFACE_SKETCH_PFG_TERMS,
        bodies=(FeatureBodyV2(body_id=body_id, name="Existing source Body"),),
        parameters=(),
        references=(),
        nodes=(source, target),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection_flatface_sketch",
                node_id=node_id,
                result_id=result_id,
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class FlatFaceSketchFormalHandoff:
    manifest_sha256: str
    operation_specification_sha256: str
    rule_contract_sha256: str
    future_formal_operation_count: int
    blockers: tuple[str, ...]

    @property
    def shared_registration_ready(self) -> bool:
        return False


FLATFACE_SKETCH_FORMAL_HANDOFF: Final = FlatFaceSketchFormalHandoff(
    manifest_sha256=FLATFACE_SKETCH_FAMILY_MANIFEST.manifest_sha256,
    operation_specification_sha256=FLATFACE_SKETCH_OPERATION_SPEC.specification_sha256,
    rule_contract_sha256=FLATFACE_SKETCH_RULE_CONTRACT_SHA256,
    future_formal_operation_count=126,
    blockers=(
        "shared-dispatcher-registration-not-in-family-scope",
        "intel-and-arm-release-attestation-refresh-pending",
    ),
)


__all__ = [
    "FLATFACE_SKETCH_BODY_OWNERSHIP_TERM",
    "FLATFACE_SKETCH_CREATE_OPERATION_TERM",
    "FLATFACE_SKETCH_FAMILY_MANIFEST",
    "FLATFACE_SKETCH_FORMAL_HANDOFF",
    "FLATFACE_SKETCH_OPERATION_SPEC",
    "FLATFACE_SKETCH_PFG_TERMS",
    "FLATFACE_SKETCH_PROFILE_TERM",
    "FLATFACE_SKETCH_REQUEST_TERMS",
    "FLATFACE_SKETCH_SELECTOR_TERM",
    "FREECAD_FLATFACE_SKETCH_ADAPTER_DESCRIPTOR",
    "FlatFaceSketchFormalHandoff",
    "build_flatface_sketch_intent_graph",
    "flatface_sketch_reviewed_adapter_factory",
    "validate_flatface_sketch_reviewed_plan",
]
