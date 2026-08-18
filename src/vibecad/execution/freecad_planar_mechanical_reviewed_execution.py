"""Private product handoff for the reviewed planar-mechanical PM1 family.

PM1 is one whole-model transaction.  Each formal operation selects a different
primary from that transaction, but every product result owns the complete
``Body -> Sketch -> Pad -> (Sketch -> Pocket)*`` closure, including the Body's
engine-created Origin helpers.  This module never presents the focused Sketch,
Pad, or Pocket as the transaction's only side effect.

The existing PM1 adapter requires a VisualFeatureGraph proof plus exact Sketch
and ParametricFeatureGraph intent documents.  The reviewed product wire keeps
the public v1 ParametricFeatureGraph program while a family-owned binding
compiles one uniquely sealed VisualFeatureGraph into the original Sketch/PFG
pair and validates the adapter's unchanged three-document proof contract.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from vibecad.execution.selectors import SemanticRole
from vibecad.intent_bridge.contracts import (
    BackendLoweringRequest,
    BridgeBudget,
    BridgeDisposition,
    BridgeTermRef,
    CompileInputBinding,
    DocumentRef,
    IntentCompileRequest,
    RequestedOutput,
)
from vibecad.intent_bridge.freecad_planar_mechanical_adapter import (
    FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR,
    PLANAR_CAPABILITY_DOCUMENT_ROLE_TERM,
    PLANAR_CAPABILITY_SCHEMA_TERM,
    PLANAR_PLAN_DOCUMENT_ROLE_TERM,
    PLANAR_PLAN_SCHEMA_TERM,
    PLANAR_REQUEST_TERMS,
    FreeCADPlanarMechanicalAdapter,
    build_planar_mechanical_capability_document,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_TYPE_DOCUMENT_ROOT,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanReceipt,
)
from vibecad.intent_bridge.sketch_intent_graph_codec import (
    SKETCH_INTENT_GRAPH_MEDIA_TYPE,
    SKETCH_INTENT_GRAPH_SCHEMA_TERM,
)
from vibecad.intent_bridge.visual_feature_graph_codec import (
    VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
    VISUAL_FEATURE_GRAPH_SCHEMA_TERM,
)
from vibecad.intent_compiler.artifacts import InMemoryIntentArtifactPublisher
from vibecad.intent_rules.planar_mechanical_v1.catalog import (
    build_planar_mechanical_v1_stack,
    planar_mechanical_v1_request_terms,
)
from vibecad.intent_rules.planar_mechanical_v1.terms import (
    PFG_OPERATION_ADD,
    PFG_OPERATION_REFERENCE_PROFILES,
    PFG_OPERATION_REMOVE,
    ROLE_PARAMETRIC_INTENT,
    ROLE_SKETCH_INTENT,
    ROLE_VISUAL_EVIDENCE,
)
from vibecad.parametric.feature_graph_v2 import (
    ParametricFeatureGraphV2,
    SemanticTermRefV2,
)
from vibecad.parametric.freecad_planar_mechanical_rules import (
    MAX_PLANAR_MECHANICAL_PLAN_BYTES,
    PLANAR_MECHANICAL_FREECAD_ENGINE_BUILD_ID,
    PLANAR_MECHANICAL_PLAN_MEDIA_TYPE,
    PLANAR_MECHANICAL_RULE_CONTRACT_SHA256,
    PLANAR_MECHANICAL_RULE_ID,
    PlanarMechanicalBackendPlan,
    PlanarMechanicalConformanceReceipt,
    PlanarMechanicalExecutionBindings,
    apply_planar_mechanical_plan,
    decode_planar_mechanical_plan,
)
from vibecad.visual.feature_graph import (
    decode_visual_feature_graph,
    encode_visual_feature_graph,
)
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1

_OWNERSHIP_DIGEST_DOMAIN = b"vibecad.planar-mechanical-product-ownership.v1\0"
_HANDOFF_DIGEST_DOMAIN = b"vibecad.planar-mechanical-product-handoff.v1\0"
_CREATE_RECOVERY_STATE_DOMAIN = b"vibecad.planar-mechanical-create-recovery-state.v1\0"
_LEGACY_MANIFEST_SHA256 = "4c1479a158c2bc15eb384fc51d3ec4b574e9e76b1c600a5de785d39ea6721feb"

_BODY_HELPER_TYPE_IDS: Final = (
    "PartDesign::Body",
    "App::Origin",
    "App::Line",
    "App::Line",
    "App::Line",
    "App::Plane",
    "App::Plane",
    "App::Plane",
    "App::Point",
)
_ORIGIN_FEATURE_ROLES: Final = (
    "X_Axis",
    "Y_Axis",
    "Z_Axis",
    "XY_Plane",
    "XZ_Plane",
    "YZ_Plane",
    "Origin",
)


def _integrity_failure() -> None:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


def _execution_failure() -> None:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.EXECUTION_FAILED)


def _bridge_term(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


PLANAR_MECHANICAL_REVIEWED_OPERATION_SPECS: Final = (
    ReviewedOperationSpec(
        operation_id="partdesign.planar-mechanical.reference-profiles",
        semantic_term=_bridge_term(PFG_OPERATION_REFERENCE_PROFILES),
        native_type_id="Sketcher::SketchObject",
        native_operation="reference_profiles",
        native_property_names=("Geometry", "MapMode", "Support"),
    ),
    ReviewedOperationSpec(
        operation_id="partdesign.planar-mechanical.add",
        semantic_term=_bridge_term(PFG_OPERATION_ADD),
        native_type_id="PartDesign::Pad",
        native_operation="add",
        native_property_names=("Length", "Midplane", "Profile", "Reversed", "Type"),
    ),
    ReviewedOperationSpec(
        operation_id="partdesign.planar-mechanical.remove",
        semantic_term=_bridge_term(PFG_OPERATION_REMOVE),
        native_type_id="PartDesign::Pocket",
        native_operation="remove",
        native_property_names=("Length", "Midplane", "Profile", "Reversed", "Type"),
    ),
)
_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in PLANAR_MECHANICAL_REVIEWED_OPERATION_SPECS}
)

PLANAR_MECHANICAL_PRODUCT_OPERATION_SPECS: Final = tuple(
    ReviewedOperationSpec(
        operation_id=operation.operation_id.removeprefix("partdesign.planar-mechanical."),
        semantic_term=operation.semantic_term,
        native_type_id=operation.native_type_id,
        native_operation=operation.native_operation,
        native_property_names=operation.native_property_names,
    )
    for operation in PLANAR_MECHANICAL_REVIEWED_OPERATION_SPECS
)
_PRODUCT_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in PLANAR_MECHANICAL_PRODUCT_OPERATION_SPECS}
)


def _merge_terms(*groups: tuple[BridgeTermRef, ...]) -> tuple[BridgeTermRef, ...]:
    by_id: dict[str, BridgeTermRef] = {}
    for term in (item for group in groups for item in group):
        prior = by_id.get(term.term_ref_id)
        if prior is not None and prior != term:
            _integrity_failure()
        by_id[term.term_ref_id] = term
    return tuple(sorted(by_id.values(), key=lambda item: item.term_ref_id))


PLANAR_MECHANICAL_PRODUCT_MANIFEST: Final = FamilyBatchManifest(
    family_id="partdesign.planar-mechanical",
    family_version="1.0.0",
    adapter=FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=hashlib.sha256(
        PLANAR_MECHANICAL_FREECAD_ENGINE_BUILD_ID.encode("ascii")
    ).hexdigest(),
    rule_id=PLANAR_MECHANICAL_RULE_ID,
    rule_contract_sha256=PLANAR_MECHANICAL_RULE_CONTRACT_SHA256,
    intent_role_term=ROLE_PARAMETRIC_INTENT,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=PLANAR_CAPABILITY_DOCUMENT_ROLE_TERM,
    capability_schema_term=PLANAR_CAPABILITY_SCHEMA_TERM,
    capability_media_type=("application/vnd.vibecad.freecad-planar-mechanical-capability+json"),
    plan_role_term=PLANAR_PLAN_DOCUMENT_ROLE_TERM,
    plan_schema_term=PLANAR_PLAN_SCHEMA_TERM,
    plan_media_type=PLANAR_MECHANICAL_PLAN_MEDIA_TYPE,
    request_terms=_merge_terms(
        PLANAR_REQUEST_TERMS,
        tuple(item.semantic_term for item in PLANAR_MECHANICAL_PRODUCT_OPERATION_SPECS),
        (PFG_TYPE_DOCUMENT_ROOT,),
    ),
    operations=PLANAR_MECHANICAL_PRODUCT_OPERATION_SPECS,
    max_plan_bytes=MAX_PLANAR_MECHANICAL_PLAN_BYTES,
)


def _operation_kind(operation: ReviewedOperationSpec) -> str:
    identifier = operation.operation_id
    if identifier.startswith("partdesign.planar-mechanical."):
        identifier = identifier.removeprefix("partdesign.planar-mechanical.")
        expected = _OPERATIONS_BY_ID.get(operation.operation_id)
    else:
        expected = _PRODUCT_OPERATIONS_BY_ID.get(identifier)
    if operation != expected or identifier not in {"reference-profiles", "add", "remove"}:
        _integrity_failure()
    return identifier


@dataclass(frozen=True, slots=True)
class PlanarMechanicalReviewedProductHandoff:
    """Static bridge facts for the sealed-visual multi-document wire."""

    operation_specs: tuple[ReviewedOperationSpec, ...]
    required_intent_media_types: tuple[str, ...]
    required_proof_media_types: tuple[str, ...]
    legacy_manifest_sha256: str
    adapter_contract_sha256: str
    rule_contract_sha256: str
    lowering_ready: bool = True
    minimum_source_results: int = 0
    maximum_source_results: int = 0
    handoff_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            self.operation_specs != PLANAR_MECHANICAL_REVIEWED_OPERATION_SPECS
            or self.required_intent_media_types
            != (SKETCH_INTENT_GRAPH_MEDIA_TYPE, PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE)
            or self.required_proof_media_types
            != (
                VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
                SKETCH_INTENT_GRAPH_MEDIA_TYPE,
                PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
            )
            or self.legacy_manifest_sha256 != _LEGACY_MANIFEST_SHA256
            or self.adapter_contract_sha256
            != FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR.adapter_contract_sha256
            or self.rule_contract_sha256 != PLANAR_MECHANICAL_RULE_CONTRACT_SHA256
            or self.lowering_ready is not True
            or self.minimum_source_results != 0
            or self.maximum_source_results != 0
        ):
            _integrity_failure()
        body = "\0".join(
            (
                self.legacy_manifest_sha256,
                self.adapter_contract_sha256,
                PLANAR_MECHANICAL_RULE_ID,
                self.rule_contract_sha256,
                *(item.operation_id for item in self.operation_specs),
                *self.required_intent_media_types,
                *self.required_proof_media_types,
                "lowering-ready=true",
                "source-results=0",
                "owned-additions=11+2N",
            )
        ).encode("ascii")
        object.__setattr__(
            self,
            "handoff_sha256",
            hashlib.sha256(_HANDOFF_DIGEST_DOMAIN + body).hexdigest(),
        )


PLANAR_MECHANICAL_REVIEWED_PRODUCT_HANDOFF: Final = PlanarMechanicalReviewedProductHandoff(
    operation_specs=PLANAR_MECHANICAL_REVIEWED_OPERATION_SPECS,
    required_intent_media_types=(
        SKETCH_INTENT_GRAPH_MEDIA_TYPE,
        PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    ),
    required_proof_media_types=(
        VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
        SKETCH_INTENT_GRAPH_MEDIA_TYPE,
        PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    ),
    legacy_manifest_sha256=_LEGACY_MANIFEST_SHA256,
    adapter_contract_sha256=(FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR.adapter_contract_sha256),
    rule_contract_sha256=PLANAR_MECHANICAL_RULE_CONTRACT_SHA256,
)


def resolve_planar_mechanical_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    """Resolve only an exact formal id plus full content-bound term identity."""

    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    operation = _OPERATIONS_BY_ID.get(operation_id)
    if operation is None:
        return None
    namespace, version, term_id, digest = operation.semantic_term.semantic_identity
    expected = f"{namespace}/{version}/{term_id}@{digest}"
    return operation if hmac.compare_digest(semantic_operation, expected) else None


class _PlanarMechanicalDocumentReader:
    __slots__ = ("_items",)

    def __init__(self, items: tuple[tuple[DocumentRef, bytes], ...]) -> None:
        if (
            type(items) is not tuple
            or not items
            or any(
                type(document) is not DocumentRef or type(payload) is not bytes
                for document, payload in items
            )
            or len({document.artifact_id for document, _payload in items}) != len(items)
        ):
            _integrity_failure()
        self._items = MappingProxyType(
            {document.artifact_id: (document, payload) for document, payload in items}
        )

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        try:
            expected, payload = self._items[document.artifact_id]
        except (AttributeError, KeyError):
            _integrity_failure()
        if (
            expected != document
            or type(maximum_bytes) is not int
            or len(payload) > maximum_bytes
            or len(payload) != document.size_bytes
            or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), document.content_sha256)
        ):
            _integrity_failure()
        return payload


def _visual_compile_request(
    compiler: object,
    visual_document: DocumentRef,
    visual_size: int,
) -> IntentCompileRequest:
    try:
        descriptor = compiler.descriptor
    except (Exception, SystemExit):
        _integrity_failure()
    return IntentCompileRequest(
        compiler=descriptor,
        terms=planar_mechanical_v1_request_terms(),
        documents=(visual_document,),
        inputs=(
            CompileInputBinding(
                binding_id="input.visual",
                ordinal=0,
                role_term_ref_id=ROLE_VISUAL_EVIDENCE.term_ref_id,
                artifact_id=visual_document.artifact_id,
            ),
        ),
        requested_outputs=(
            RequestedOutput(
                output_id="output.sketch",
                ordinal=0,
                role_term_ref_id=ROLE_SKETCH_INTENT.term_ref_id,
                schema_term_ref_id=SKETCH_INTENT_GRAPH_SCHEMA_TERM.term_ref_id,
            ),
            RequestedOutput(
                output_id="output.parametric",
                ordinal=1,
                role_term_ref_id=ROLE_PARAMETRIC_INTENT.term_ref_id,
                schema_term_ref_id=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM.term_ref_id,
            ),
        ),
        budget=BridgeBudget(
            max_input_bytes=visual_size,
            max_output_bytes=512 * 1024,
            max_subject_lookups=6,
            max_rule_applications=2,
        ),
    )


def lower_planar_mechanical_reviewed_multi_document(
    value: ReviewedIntentProgramV1,
    operation: ReviewedOperationSpec,
    resolver: object,
    run_token: object,
    manifest: FamilyBatchManifest,
) -> object:
    """Compile sealed VFG evidence and lower its original three-document proof."""

    from vibecad.execution.freecad_reviewed_artifact_inputs import (  # noqa: PLC0415
        MAX_REVIEWED_ARTIFACT_BYTES,
        ReviewedArtifactContext,
        ReviewedArtifactResolution,
        _ReviewedArtifactRunResolver,
    )
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ExactPlanSink,
        _ReviewedMultiDocumentLowering,
    )

    kind = _operation_kind(operation)
    if (
        type(value) is not ReviewedIntentProgramV1
        or type(value.intent_graph) is not ParametricFeatureGraphV2
        or type(resolver) is not _ReviewedArtifactRunResolver
        or run_token is None
        or manifest != PLANAR_MECHANICAL_PRODUCT_MANIFEST
    ):
        _integrity_failure()
    resolution = resolver.resolve_unique(
        run_token=run_token,
        family_id=manifest.family_id,
        operation_id=operation.operation_id,
        role_term_ref_id=ROLE_VISUAL_EVIDENCE.term_ref_id,
        schema_term_ref_id=VISUAL_FEATURE_GRAPH_SCHEMA_TERM.term_ref_id,
        media_type=VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
        maximum_bytes=MAX_REVIEWED_ARTIFACT_BYTES,
    )
    if (
        type(resolution) is not ReviewedArtifactResolution
        or type(resolution.artifact_context) is not ReviewedArtifactContext
    ):
        _integrity_failure()
    context = resolution.artifact_context
    visual_payload = context.artifacts.read(
        context.artifact_document,
        MAX_REVIEWED_ARTIFACT_BYTES,
    )
    visual = decode_visual_feature_graph(visual_payload)
    if not hmac.compare_digest(visual_payload, encode_visual_feature_graph(visual)):
        _integrity_failure()
    visual_document = DocumentRef(
        artifact_id=context.artifact_document.artifact_id,
        role_term_ref_id=ROLE_VISUAL_EVIDENCE.term_ref_id,
        schema_term_ref_id=VISUAL_FEATURE_GRAPH_SCHEMA_TERM.term_ref_id,
        document_id=visual.graph_id,
        document_digest=visual.graph_digest,
        content_sha256=hashlib.sha256(visual_payload).hexdigest(),
        size_bytes=len(visual_payload),
        media_type=VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
    )
    publisher = InMemoryIntentArtifactPublisher()
    stack = build_planar_mechanical_v1_stack(publisher=publisher)
    compile_result = stack.compiler.compile(
        _visual_compile_request(stack.compiler, visual_document, len(visual_payload)),
        artifacts=_PlanarMechanicalDocumentReader(((visual_document, visual_payload),)),
        codecs=stack.codecs,
        proof_policy=stack.proof_policy,
    )
    if (
        compile_result.disposition is not BridgeDisposition.COMPLETE
        or compile_result.proof_bundle is None
        or len(compile_result.output_documents) != 2
        or len(compile_result.proof_bundle.documents) != 3
        or len(compile_result.proof_bundle.assertions) != 2
    ):
        _integrity_failure()
    by_media = {item.media_type: item for item in compile_result.output_documents}
    try:
        sketch_document = by_media[SKETCH_INTENT_GRAPH_MEDIA_TYPE]
        parametric_document = by_media[PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE]
        sketch_payload = publisher.read(sketch_document, 512 * 1024)
        parametric_payload = publisher.read(parametric_document, 512 * 1024)
    except (Exception, SystemExit):
        _integrity_failure()
    if (
        not hmac.compare_digest(parametric_payload, value.intent_graph.canonical_bytes)
        or not hmac.compare_digest(parametric_document.document_digest, value.intent_graph_sha256)
        or not hmac.compare_digest(parametric_document.content_sha256, value.intent_content_sha256)
    ):
        _integrity_failure()
    matching_nodes = tuple(
        node
        for node in value.intent_graph.nodes
        if node.intent.operation_term_ref_id == operation.semantic_term.term_ref_id
    )
    if (kind in {"reference-profiles", "add"} and len(matching_nodes) != 1) or (
        kind == "remove" and not matching_nodes
    ):
        _integrity_failure()
    if kind == "remove":
        final_node_id = value.intent_graph.graph_results[0].node_id
        if matching_nodes[-1].node_id != final_node_id:
            _integrity_failure()

    capability_document, capability_payload = build_planar_mechanical_capability_document()
    proof_bytes = sum(item.size_bytes for item in compile_result.proof_bundle.documents)
    request = BackendLoweringRequest(
        adapter=manifest.adapter,
        terms=PLANAR_REQUEST_TERMS,
        documents=(*compile_result.output_documents, capability_document),
        intent_artifact_ids=tuple(item.artifact_id for item in compile_result.output_documents),
        capability_artifact_ids=(capability_document.artifact_id,),
        proof_bundle=compile_result.proof_bundle,
        budget=BridgeBudget(
            max_input_bytes=proof_bytes + len(capability_payload),
            max_output_bytes=manifest.max_plan_bytes,
            max_subject_lookups=6,
            max_rule_applications=2,
        ),
    )
    reader = _PlanarMechanicalDocumentReader(
        (
            (visual_document, visual_payload),
            (sketch_document, sketch_payload),
            (parametric_document, parametric_payload),
            (capability_document, capability_payload),
        )
    )
    sink = _ExactPlanSink()
    adapter = FreeCADPlanarMechanicalAdapter(sink)
    result, planar_receipt = adapter.lower_with_receipt(
        request,
        artifacts=reader,
        codecs=stack.codecs,
        proof_policy=stack.proof_policy,
    )
    plan, payload = adapter.read_plan(planar_receipt)
    if (
        planar_receipt.parametric_document != parametric_document
        or planar_receipt.sketch_document != sketch_document
        or plan.parametric_document.artifact_id != parametric_document.artifact_id
        or plan.sketch_document.artifact_id != sketch_document.artifact_id
    ):
        _integrity_failure()
    receipt = ReviewedPlanReceipt(
        manifest_sha256=manifest.manifest_sha256,
        request_digest=request.request_digest,
        adapter=manifest.adapter,
        operation=operation,
        source_document=parametric_document,
        plan_document=result.plan_document,
    )
    return _ReviewedMultiDocumentLowering(
        result=result,
        receipt=receipt,
        plan=plan,
        payload=payload,
    )


def _shape_sha256(item: object) -> str:
    try:
        raw = item.Shape.exportBrepToString().encode("utf-8")
    except (Exception, SystemExit):
        _integrity_failure()
    if not raw:
        _integrity_failure()
    return hashlib.sha256(raw).hexdigest()


def _valid_shape(item: object, *, solid: bool) -> bool:
    try:
        shape = item.Shape
        return bool(
            item.isValid()
            and tuple(item.State) == ("Up-to-date",)
            and not shape.isNull()
            and shape.isValid()
            and (
                not solid
                or (
                    len(shape.Solids) == 1
                    and math.isfinite(float(shape.Volume))
                    and float(shape.Volume) > 0.0
                )
            )
        )
    except (Exception, SystemExit, TypeError, ValueError, OverflowError):
        return False


def _validate_body_helpers(document: object, body: object) -> tuple[object, ...]:
    try:
        origin = body.Origin
        helpers = tuple(origin.OriginFeatures)
        closure = (body, origin, *helpers)
        document_objects = tuple(document.Objects)
        valid = (
            tuple(item.TypeId for item in closure) == _BODY_HELPER_TYPE_IDS
            and origin.Document is document
            and tuple(origin.Group) == ()
            and all(item.Document is document for item in closure)
            and all(any(item is existing for existing in document_objects) for item in closure)
            and len({id(item) for item in closure}) == len(closure)
            and all(
                helper.Role == role
                and tuple(helper.InList) == (origin,)
                and helper.isValid()
                and tuple(helper.State) == ("Up-to-date",)
                for helper, role in zip(helpers, _ORIGIN_FEATURE_ROLES, strict=True)
            )
        )
    except (Exception, SystemExit):
        valid = False
        closure = ()
    if not valid:
        _integrity_failure()
    return closure


def _expected_visibility(
    *,
    pad: object,
    circle_sketches: tuple[object, ...],
    pockets: tuple[object, ...],
) -> bool:
    try:
        if not bool(pad.Visibility) == (not pockets):
            return False
        if any(not bool(item.Visibility) for item in circle_sketches):
            return False
        return all(
            bool(item.Visibility) == (index == len(pockets) - 1)
            for index, item in enumerate(pockets)
        )
    except (Exception, SystemExit):
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanarMechanicalOwnershipClosure:
    """Content-bound ownership of every object created by one PM1 transaction."""

    operation: ReviewedOperationSpec
    plan: PlanarMechanicalBackendPlan
    native_receipt: PlanarMechanicalConformanceReceipt
    primary: object = field(repr=False, compare=False)
    body: object = field(repr=False, compare=False)
    body_closure: tuple[object, ...] = field(repr=False, compare=False)
    outer_sketch: object = field(repr=False, compare=False)
    pad: object = field(repr=False, compare=False)
    circle_sketches: tuple[object, ...] = field(repr=False, compare=False)
    pockets: tuple[object, ...] = field(repr=False, compare=False)
    primary_shape_sha256: str
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        operation_kind = _operation_kind(self.operation)
        if (
            type(self.operation) is not ReviewedOperationSpec
            or type(self.plan) is not PlanarMechanicalBackendPlan
            or type(self.native_receipt) is not PlanarMechanicalConformanceReceipt
            or self.native_receipt.plan_sha256 != self.plan.plan_sha256
            or self.primary is None
            or self.body is None
            or type(self.body_closure) is not tuple
            or len(self.body_closure) != len(_BODY_HELPER_TYPE_IDS)
            or self.body_closure[0] is not self.body
            or self.outer_sketch is None
            or self.pad is None
            or type(self.circle_sketches) is not tuple
            or type(self.pockets) is not tuple
            or len(self.circle_sketches) != len(self.plan.circles)
            or len(self.pockets) != len(self.plan.circles)
            or len(self.circle_sketches) != len(self.pockets)
            or not hmac.compare_digest(self.primary_shape_sha256, _shape_sha256(self.primary))
        ):
            _integrity_failure()
        expected_primary = {
            "reference-profiles": self.outer_sketch,
            "add": self.pad,
            "remove": self.pockets[-1] if self.pockets else None,
        }[operation_kind]
        if self.primary is not expected_primary:
            _integrity_failure()
        body = "\0".join(
            (
                self.native_receipt.receipt_sha256,
                self.operation.operation_id,
                self.plan.plan_sha256,
                self.primary_shape_sha256,
                self.body.Name,
                self.outer_sketch.Name,
                self.pad.Name,
                *(item.Name for item in self.circle_sketches),
                *(item.Name for item in self.pockets),
            )
        ).encode("utf-8")
        object.__setattr__(
            self,
            "receipt_sha256",
            hashlib.sha256(_OWNERSHIP_DIGEST_DOMAIN + body).hexdigest(),
        )

    @property
    def plan_sha256(self) -> str:
        return self.plan.plan_sha256

    @property
    def plan_content_sha256(self) -> str:
        return hashlib.sha256(self.plan.canonical_bytes).hexdigest()

    @property
    def object_name(self) -> str:
        return self.primary.Name

    def transaction_objects(self) -> tuple[object, ...]:
        members: list[object] = [*self.body_closure, self.outer_sketch, self.pad]
        for sketch, pocket in zip(self.circle_sketches, self.pockets, strict=True):
            members.extend((sketch, pocket))
        return tuple(members)

    def owned_objects(self, result: object) -> tuple[object, ...]:
        if result is not self.primary:
            _integrity_failure()
        additions = self.transaction_objects()
        return (result, *(item for item in additions if item is not result))

    def validate_native_result(self, document: object, result: object) -> None:
        if result is not self.primary:
            _integrity_failure()
        current_body_closure = _validate_body_helpers(document, self.body)
        try:
            objects = tuple(document.Objects)
            expected_group: list[object] = [self.outer_sketch, self.pad]
            for sketch, pocket in zip(self.circle_sketches, self.pockets, strict=True):
                expected_group.extend((sketch, pocket))
            final_feature = self.pockets[-1] if self.pockets else self.pad
            previous = self.pad
            chain_valid = True
            for sketch, pocket in zip(self.circle_sketches, self.pockets, strict=True):
                chain_valid = chain_valid and (
                    pocket.Profile[0] is sketch
                    and tuple(pocket.Profile[1]) == ()
                    and pocket.BaseFeature is previous
                )
                previous = pocket
            all_transaction_objects = self.transaction_objects()
            valid = (
                len(current_body_closure) == len(self.body_closure)
                and all(
                    current is original
                    for current, original in zip(
                        current_body_closure, self.body_closure, strict=True
                    )
                )
                and len(all_transaction_objects) == 11 + 2 * len(self.plan.circles)
                and len({id(item) for item in all_transaction_objects})
                == len(all_transaction_objects)
                and all(item.Document is document for item in all_transaction_objects)
                and all(
                    any(item is existing for existing in objects)
                    for item in all_transaction_objects
                )
                and self.body.TypeId == "PartDesign::Body"
                and tuple(self.body.Group) == tuple(expected_group)
                and self.body.Tip is final_feature
                and all(
                    item.isValid()
                    and tuple(item.State) == ("Up-to-date",)
                    and bool(item.Visibility)
                    for item in self.body_closure
                )
                and bool(self.outer_sketch.Visibility)
                and self.outer_sketch.TypeId == "Sketcher::SketchObject"
                and self.outer_sketch.isValid()
                and tuple(self.outer_sketch.State) == ("Up-to-date",)
                and self.pad.TypeId == "PartDesign::Pad"
                and self.pad.Profile[0] is self.outer_sketch
                and tuple(self.pad.Profile[1]) == ()
                and self.pad.Type == "Length"
                and math.isclose(
                    float(self.pad.Length),
                    self.plan.depth_mm,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                and not bool(self.pad.Midplane)
                and not bool(self.pad.Reversed)
                and bool(self.pad.Refine)
                and not bool(self.pad.AllowMultiFace)
                and _valid_shape(self.pad, solid=True)
                and all(
                    item.TypeId == "Sketcher::SketchObject"
                    and item.isValid()
                    and tuple(item.State) == ("Up-to-date",)
                    for item in self.circle_sketches
                )
                and all(
                    item.TypeId == "PartDesign::Pocket"
                    and item.Type == "ThroughAll"
                    and item.SideType == "One side"
                    and bool(item.AlongSketchNormal)
                    and not bool(item.UseCustomVector)
                    and math.isclose(float(item.Offset), 0.0, rel_tol=0.0, abs_tol=1e-12)
                    and math.isclose(float(item.Offset2), 0.0, rel_tol=0.0, abs_tol=1e-12)
                    and math.isclose(float(item.TaperAngle), 0.0, rel_tol=0.0, abs_tol=1e-12)
                    and math.isclose(float(item.TaperAngle2), 0.0, rel_tol=0.0, abs_tol=1e-12)
                    and bool(item.Reversed)
                    and bool(item.Refine)
                    and _valid_shape(item, solid=True)
                    for item in self.pockets
                )
                and chain_valid
                and _expected_visibility(
                    pad=self.pad,
                    circle_sketches=self.circle_sketches,
                    pockets=self.pockets,
                )
                and _valid_shape(
                    self.primary,
                    solid=_operation_kind(self.operation) != "reference-profiles",
                )
                and hmac.compare_digest(
                    self.primary_shape_sha256,
                    _shape_sha256(self.primary),
                )
                and math.isclose(
                    float(final_feature.Shape.Volume),
                    self.plan.expected_volume_mm3,
                    rel_tol=0.0,
                    abs_tol=max(1e-6, self.plan.expected_volume_mm3 * 1e-8),
                )
                and math.isclose(
                    self.native_receipt.volume_mm3,
                    float(final_feature.Shape.Volume),
                    rel_tol=0.0,
                    abs_tol=max(1e-6, self.plan.expected_volume_mm3 * 1e-8),
                )
            )
        except (Exception, SystemExit):
            valid = False
        if not valid:
            _integrity_failure()

    def validate_adoption(self, document: object, result: object, observation: object) -> None:
        from vibecad.validation import EntityObservation  # noqa: PLC0415

        self.validate_native_result(document, result)
        reference = _operation_kind(self.operation) == "reference-profiles"
        try:
            valid = (
                type(observation) is EntityObservation
                and observation.feature_id is not None
                and observation.object_type == self.operation.native_type_id
                and observation.semantic_role
                == (SemanticRole.SUPPORT.value if reference else SemanticRole.FEATURE.value)
                and observation.valid_shape is True
                and (
                    reference
                    or (
                        observation.solid_count == 1
                        and observation.volume_mm3 is not None
                        and observation.volume_mm3 > 0.0
                    )
                )
            )
        except (Exception, SystemExit, TypeError, ValueError):
            valid = False
        if not valid:
            _integrity_failure()


def _document_snapshot(document: object) -> tuple[object, ...]:
    try:
        before = tuple(document.Objects)
        if (
            getattr(document, "UndoMode", 0) != 1
            or bool(document.HasPendingTransaction)
            or len({id(item) for item in before}) != len(before)
        ):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        _integrity_failure()
    return before


def _added_objects(document: object, before: tuple[object, ...]) -> tuple[object, ...]:
    try:
        after = tuple(document.Objects)
    except (AttributeError, TypeError):
        _integrity_failure()
    return tuple(item for item in after if not any(item is existing for existing in before))


def _restore_document(document: object, before: tuple[object, ...]) -> None:
    try:
        added = _added_objects(document, before)
        bodies = tuple(
            item for item in added if getattr(item, "TypeId", None) == "PartDesign::Body"
        )
        others = tuple(item for item in added if not any(item is body for body in bodies))
        for item in (*bodies, *reversed(others)):
            name = getattr(item, "Name", None)
            if type(name) is str and document.getObject(name) is item:
                document.removeObject(name)
        document.recompute()
        current = tuple(document.Objects)
        restored = len(current) == len(before) and all(
            actual is original for actual, original in zip(current, before, strict=True)
        )
    except (Exception, SystemExit):
        restored = False
    if not restored:
        _integrity_failure()


@dataclass(frozen=True, slots=True)
class _PlanarMechanicalCreateRecovery:
    document: object = field(repr=False, compare=False)
    before: tuple[object, ...] = field(repr=False, compare=False)
    state_sha256: str

    def __post_init__(self) -> None:
        if (
            self.document is None
            or type(self.before) is not tuple
            or len({id(item) for item in self.before}) != len(self.before)
            or type(self.state_sha256) is not str
            or len(self.state_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.state_sha256)
        ):
            _integrity_failure()


def _object_identity_sequence_sha256(before: tuple[object, ...]) -> str:
    try:
        body = "\0".join(f"{index}:{id(item):x}" for index, item in enumerate(before)).encode(
            "ascii"
        )
    except (Exception, SystemExit):
        _integrity_failure()
    return hashlib.sha256(_CREATE_RECOVERY_STATE_DOMAIN + body).hexdigest()


def _create_recovery_state_sha256(document: object, before: tuple[object, ...]) -> str:
    try:
        current = tuple(document.Objects)
        if len(current) != len(before) or any(
            actual is not expected for actual, expected in zip(current, before, strict=True)
        ):
            raise ValueError
    except (Exception, SystemExit):
        _integrity_failure()
    return _object_identity_sequence_sha256(before)


def _require_create_recovery(
    document: object,
    operation: ReviewedOperationSpec,
    context: object,
    opaque: object,
) -> _PlanarMechanicalCreateRecovery:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyExecutionContext,
    )

    if (
        type(opaque) is not _PlanarMechanicalCreateRecovery
        or opaque.document is not document
        or type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
        or context.source_results
        or _operation_kind(operation) not in {"reference-profiles", "add", "remove"}
        or not hmac.compare_digest(
            opaque.state_sha256,
            _object_identity_sequence_sha256(opaque.before),
        )
    ):
        _integrity_failure()
    return opaque


def _prepare_planar_mechanical_create_recovery(
    document: object,
    operation: ReviewedOperationSpec,
    context: object,
) -> tuple[str, object]:
    before = _document_snapshot(document)
    state_sha256 = _create_recovery_state_sha256(document, before)
    recovery = _PlanarMechanicalCreateRecovery(
        document=document,
        before=before,
        state_sha256=state_sha256,
    )
    _require_create_recovery(document, operation, context, recovery)
    return state_sha256, recovery


def _recover_planar_mechanical_create(
    document: object,
    opaque: object,
    operation: ReviewedOperationSpec,
    context: object,
) -> None:
    checked = _require_create_recovery(document, operation, context, opaque)
    _restore_document(document, checked.before)


def _verify_planar_mechanical_create_state(
    document: object,
    opaque: object,
    operation: ReviewedOperationSpec,
    context: object,
) -> str:
    checked = _require_create_recovery(document, operation, context, opaque)
    return _create_recovery_state_sha256(document, checked.before)


def _commit_planar_mechanical_create(
    document: object,
    opaque: object,
    operation: ReviewedOperationSpec,
    context: object,
) -> None:
    checked = _require_create_recovery(document, operation, context, opaque)
    try:
        current = tuple(document.Objects)
        added = current[len(checked.before) :]
        valid = (
            len(current) >= len(checked.before)
            and all(
                actual is expected
                for actual, expected in zip(
                    current[: len(checked.before)],
                    checked.before,
                    strict=True,
                )
            )
            and len(added) in range(11, 44, 2)
            and len({id(item) for item in added}) == len(added)
            and sum(getattr(item, "TypeId", None) == "PartDesign::Body" for item in added) == 1
            and all(getattr(item, "Document", None) is document for item in added)
        )
    except (Exception, SystemExit):
        valid = False
    if not valid:
        _integrity_failure()


def planar_mechanical_create_recovery_descriptor() -> object:
    """Build the shared late-adoption recovery descriptor for one PM1 CREATE."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedCreateRecoveryDescriptor,
    )

    return _ReviewedCreateRecoveryDescriptor(
        descriptor_id="reviewed_pm1_whole_transaction",
        descriptor_version="1.0.0",
        descriptor_contract_sha256=hashlib.sha256(
            _CREATE_RECOVERY_STATE_DOMAIN + b"exact-pre-object-identities;whole-create-rollback;"
            b"owned-additions=11+2N;N<=16"
        ).hexdigest(),
        operation_ids=tuple(
            item.operation_id for item in PLANAR_MECHANICAL_PRODUCT_MANIFEST.operations
        ),
        prepare=_prepare_planar_mechanical_create_recovery,
        recover=_recover_planar_mechanical_create,
        verify=_verify_planar_mechanical_create_state,
        commit=_commit_planar_mechanical_create,
    )


def _checked_plan(
    plan: object,
    payload: object,
    plan_document: object,
    operation: object,
) -> PlanarMechanicalBackendPlan:
    operation_kind = _operation_kind(operation)
    if (
        type(plan) is not PlanarMechanicalBackendPlan
        or type(payload) is not bytes
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or plan.adapter_contract_sha256
        != FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR.adapter_contract_sha256
        or plan_document.role_term_ref_id != PLANAR_PLAN_DOCUMENT_ROLE_TERM.term_ref_id
        or plan_document.schema_term_ref_id != PLANAR_PLAN_SCHEMA_TERM.term_ref_id
        or plan_document.media_type != PLANAR_MECHANICAL_PLAN_MEDIA_TYPE
        or plan_document.size_bytes != len(payload)
        or not hmac.compare_digest(
            plan_document.content_sha256,
            hashlib.sha256(payload).hexdigest(),
        )
        or not hmac.compare_digest(plan_document.document_digest, plan.plan_sha256)
        or not hmac.compare_digest(payload, plan.canonical_bytes)
        or (operation_kind == "remove" and not plan.circles)
    ):
        _integrity_failure()
    try:
        decoded = decode_planar_mechanical_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    return plan


def validate_planar_mechanical_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    operation_kind = _operation_kind(operation)
    if (
        type(plan) is not PlanarMechanicalBackendPlan
        or type(receipt) is not ReviewedPlanReceipt
        or receipt.operation != operation
        or receipt.manifest_sha256 != PLANAR_MECHANICAL_PRODUCT_MANIFEST.manifest_sha256
        or receipt.adapter != FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR
        or receipt.source_document.media_type != PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE
        or plan.plan_sha256 != receipt.plan_document.document_digest
        or plan.parametric_document.artifact_id != receipt.source_document.artifact_id
        or plan.parametric_document.document_id != receipt.source_document.document_id
        or plan.parametric_document.document_digest != receipt.source_document.document_digest
        or plan.parametric_document.content_sha256 != receipt.source_document.content_sha256
        or (operation_kind == "remove" and not plan.circles)
    ):
        _integrity_failure()


def _resolve_native_closure(
    document: object,
    plan: PlanarMechanicalBackendPlan,
    operation: ReviewedOperationSpec,
    receipt: PlanarMechanicalConformanceReceipt,
) -> PlanarMechanicalOwnershipClosure:
    try:
        body = document.getObject(receipt.body_name)
        outer = document.getObject(receipt.outer_sketch_name)
        pad = document.getObject(receipt.pad_name)
        circle_sketches = tuple(document.getObject(name) for name in receipt.circle_sketch_names)
        pockets = tuple(document.getObject(name) for name in receipt.pocket_names)
        primary = {
            "reference-profiles": outer,
            "add": pad,
            "remove": pockets[-1] if pockets else None,
        }[_operation_kind(operation)]
    except (Exception, SystemExit):
        _integrity_failure()
    if primary is None:
        _integrity_failure()
    return PlanarMechanicalOwnershipClosure(
        operation=operation,
        plan=plan,
        native_receipt=receipt,
        primary=primary,
        body=body,
        body_closure=_validate_body_helpers(document, body),
        outer_sketch=outer,
        pad=pad,
        circle_sketches=circle_sketches,
        pockets=pockets,
        primary_shape_sha256=_shape_sha256(primary),
    )


def execute_planar_mechanical_reviewed_plan_with_sources(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    source_results: tuple[object, ...],
    *,
    session: object,
) -> object:
    """Execute one proven PM1 plan and return its full owned transaction closure."""

    checked = _checked_plan(plan, payload, plan_document, operation)
    if (
        document is None
        or type(source_results) is not tuple
        or source_results
        or session is None
        or getattr(session, "doc", None) is not document
    ):
        _integrity_failure()
    before = _document_snapshot(document)
    try:
        receipt = apply_planar_mechanical_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
            bindings=PlanarMechanicalExecutionBindings(document=document),
        )
        if (
            type(receipt) is not PlanarMechanicalConformanceReceipt
            or receipt.plan_sha256 != checked.plan_sha256
            or len(receipt.circle_sketch_names) != len(checked.circles)
            or len(receipt.pocket_names) != len(checked.circles)
        ):
            _integrity_failure()
        ownership = _resolve_native_closure(document, checked, operation, receipt)
        ownership.validate_native_result(document, ownership.primary)
        added = _added_objects(document, before)
        expected_added = ownership.transaction_objects()
        if len(added) != len(expected_added) or any(
            item is not expected for item, expected in zip(added, expected_added, strict=True)
        ):
            _integrity_failure()
        owned = ownership.owned_objects(ownership.primary)
    except KeyboardInterrupt:
        _restore_document(document, before)
        raise
    except BaseException:
        _restore_document(document, before)
        _execution_failure()

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(
        object=ownership.primary,
        receipt=ownership,
        owned_objects=owned,
    )


def execute_planar_mechanical_reviewed_plan(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    context: object,
) -> object:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyExecutionContext,
    )

    if type(context) is not _ReviewedFamilyExecutionContext or context.document is not document:
        _integrity_failure()
    return execute_planar_mechanical_reviewed_plan_with_sources(
        document,
        plan,
        payload,
        plan_document,
        operation,
        context.source_results,
        session=context.session,
    )


def resolve_planar_mechanical_product_contract(
    plan: object,
    operation: object,
) -> object:
    """Build the exact dynamic owned-TypeId/role contract for one PM1 plan."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedProductExecutionMode,
        _ReviewedProductResultContract,
        _ReviewedProductResultKind,
    )

    operation_kind = _operation_kind(operation)
    if (
        type(plan) is not PlanarMechanicalBackendPlan
        or type(operation) is not ReviewedOperationSpec
        or (operation_kind == "remove" and not plan.circles)
    ):
        _integrity_failure()
    additions_types: list[str] = [
        *_BODY_HELPER_TYPE_IDS,
        "Sketcher::SketchObject",
        "PartDesign::Pad",
    ]
    additions_roles: list[SemanticRole] = [
        SemanticRole.PART,
        *(SemanticRole.SUPPORT,) * 8,
        SemanticRole.SUPPORT,
        SemanticRole.FEATURE,
    ]
    for _circle in plan.circles:
        additions_types.extend(("Sketcher::SketchObject", "PartDesign::Pocket"))
        additions_roles.extend((SemanticRole.SUPPORT, SemanticRole.FEATURE))
    primary_index = {
        "reference-profiles": 9,
        "add": 10,
        "remove": len(additions_types) - 1,
    }[operation_kind]
    owned_types = (
        additions_types[primary_index],
        *(item for index, item in enumerate(additions_types) if index != primary_index),
    )
    owned_roles = (
        additions_roles[primary_index],
        *(item for index, item in enumerate(additions_roles) if index != primary_index),
    )
    return _ReviewedProductResultContract(
        operation_id=operation.operation_id,
        result_kind=(
            _ReviewedProductResultKind.VALID_SHAPE
            if operation_kind == "reference-profiles"
            else _ReviewedProductResultKind.SOLID
        ),
        owned_type_ids=tuple(owned_types),
        semantic_roles=tuple(owned_roles),
        source_count=None,
        execution_mode=_ReviewedProductExecutionMode.CREATE,
    )


__all__ = [
    "PLANAR_MECHANICAL_PRODUCT_MANIFEST",
    "PLANAR_MECHANICAL_PRODUCT_OPERATION_SPECS",
    "PLANAR_MECHANICAL_REVIEWED_OPERATION_SPECS",
    "PLANAR_MECHANICAL_REVIEWED_PRODUCT_HANDOFF",
    "PlanarMechanicalOwnershipClosure",
    "PlanarMechanicalReviewedProductHandoff",
    "execute_planar_mechanical_reviewed_plan",
    "execute_planar_mechanical_reviewed_plan_with_sources",
    "lower_planar_mechanical_reviewed_multi_document",
    "planar_mechanical_create_recovery_descriptor",
    "resolve_planar_mechanical_product_contract",
    "resolve_planar_mechanical_reviewed_operation",
    "validate_planar_mechanical_reviewed_plan",
]
