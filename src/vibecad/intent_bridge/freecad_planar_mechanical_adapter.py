"""Trusted PM1 intent lowering into an authority-free FreeCAD native plan.

The adapter accepts only the exact, proof-validated SketchIntentGraph and
ParametricFeatureGraphV2 emitted by ``planar-mechanical-v1``.  Complete ontology
identities, codec contracts, the proof-policy catalog, the emitter contract,
and the reviewed native rule are all bound into the adapter contract digest.
No graph string is ever interpreted as a FreeCAD ``TypeId`` or property name.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Final

from vibecad.intent_bridge.contracts import (
    AdapterDescriptor,
    BackendLoweringRequest,
    BackendLoweringResult,
    BridgeDisposition,
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
    PFG_SELECTOR_GRAPH_RESULT,
    ParametricFeatureGraphV2Codec,
)
from vibecad.intent_bridge.ports import (
    ArtifactReader,
    TrustedCodecRegistry,
    TrustedProofPolicy,
    read_verified_document,
    validate_lowering_result,
    validate_proof_bundle,
)
from vibecad.intent_bridge.sketch_intent_graph_codec import (
    SKETCH_INTENT_GRAPH_MEDIA_TYPE,
    SKETCH_INTENT_GRAPH_SCHEMA_TERM,
    SKETCH_ROOT_SELECTOR_TERM,
    SketchIntentGraphCodec,
)
from vibecad.intent_bridge.visual_feature_graph_codec import (
    VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
    VISUAL_FEATURE_GRAPH_SCHEMA_TERM,
    VisualFeatureGraphCodec,
)
from vibecad.intent_rules.planar_mechanical_v1.catalog import (
    build_planar_mechanical_v1_proof_policy,
)
from vibecad.intent_rules.planar_mechanical_v1.rule_set import (
    PLANAR_MECHANICAL_V1_EMITTER_CONTRACT_SHA256,
    PLANAR_MECHANICAL_V1_RULE_SET_CONTRACT_SHA256,
    analyze_visual_feature_graph,
    build_intent_graphs,
)
from vibecad.intent_rules.planar_mechanical_v1.terms import (
    PFG_OUTPUT_TERMS,
    ROLE_PARAMETRIC_INTENT,
    ROLE_SKETCH_INTENT,
    SKETCH_OUTPUT_TERMS,
)
from vibecad.parametric.feature_graph_v2 import (
    ParametricFeatureGraphError,
    decode_parametric_feature_graph_v2,
    encode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_planar_mechanical_rules import (
    MAX_PLANAR_MECHANICAL_PLAN_BYTES,
    PLANAR_MECHANICAL_FREECAD_ENGINE_BUILD_ID,
    PLANAR_MECHANICAL_PLAN_MEDIA_TYPE,
    PLANAR_MECHANICAL_RULE_CONTRACT_SHA256,
    PLANAR_MECHANICAL_RULE_ID,
    PlanarCircleRemoval,
    PlanarDocumentBinding,
    PlanarMechanicalBackendPlan,
    PlanarMechanicalRuleError,
    PlanarRectangleProfile,
    decode_planar_mechanical_plan,
)
from vibecad.sketch.contracts import (
    SketchIntentError,
    decode_sketch_intent_graph,
    encode_sketch_intent_graph,
)
from vibecad.visual.feature_graph import (
    VisualFeatureGraphError,
    decode_visual_feature_graph,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.freecad-planar-mechanical"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.freecad-planar-mechanical-ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.freecad-planar-mechanical-adapter.v1\0"
_CAPABILITY_DIGEST_DOMAIN = b"vibecad.freecad-planar-mechanical-capability.v1\0"
_PLAN_DOCUMENT_DIGEST_DOMAIN = b"vibecad.freecad-planar-mechanical-document.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-planar-mechanical-lowering-receipt.v1\0"


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


def _term(term_ref_id: str, term_id: str) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace=_ONTOLOGY_NAMESPACE,
        vocabulary_version=_ONTOLOGY_VERSION,
        term_id=term_id,
        term_definition_sha256=_definition(term_id),
    )


PLANAR_CAPABILITY_DOCUMENT_ROLE_TERM: Final = _term(
    "role_freecad_planar_mechanical_capability",
    "document-role.freecad-planar-mechanical-capability",
)
PLANAR_CAPABILITY_SCHEMA_TERM: Final = _term(
    "schema_freecad_planar_mechanical_capability_v1",
    "document-schema.freecad-planar-mechanical-capability-v1",
)
PLANAR_PLAN_DOCUMENT_ROLE_TERM: Final = _term(
    "role_freecad_planar_mechanical_plan",
    "document-role.freecad-planar-mechanical-plan",
)
PLANAR_PLAN_SCHEMA_TERM: Final = _term(
    "schema_freecad_planar_mechanical_plan_v1",
    "document-schema.freecad-planar-mechanical-plan-v1",
)

_EXPECTED_CODEC_DESCRIPTORS = tuple(
    codec.descriptor
    for codec in (
        VisualFeatureGraphCodec(),
        SketchIntentGraphCodec(),
        ParametricFeatureGraphV2Codec(),
    )
)
_EXPECTED_PROOF_CATALOG_SHA256 = build_planar_mechanical_v1_proof_policy().catalog_sha256


def _identity(term: object) -> tuple[str, str, str, str]:
    try:
        return (
            term.namespace,
            term.vocabulary_version,
            term.term_id,
            term.term_definition_sha256,
        )
    except Exception:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/terms")


_CONTRACT_TERMS = (
    ROLE_SKETCH_INTENT,
    ROLE_PARAMETRIC_INTENT,
    VISUAL_FEATURE_GRAPH_SCHEMA_TERM,
    SKETCH_INTENT_GRAPH_SCHEMA_TERM,
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    SKETCH_ROOT_SELECTOR_TERM,
    PFG_SELECTOR_GRAPH_RESULT,
    PLANAR_CAPABILITY_DOCUMENT_ROLE_TERM,
    PLANAR_CAPABILITY_SCHEMA_TERM,
    PLANAR_PLAN_DOCUMENT_ROLE_TERM,
    PLANAR_PLAN_SCHEMA_TERM,
    *SKETCH_OUTPUT_TERMS,
    *PFG_OUTPUT_TERMS,
)

_ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            PLANAR_MECHANICAL_RULE_ID.encode("ascii"),
            PLANAR_MECHANICAL_RULE_CONTRACT_SHA256.encode("ascii"),
            PLANAR_MECHANICAL_V1_RULE_SET_CONTRACT_SHA256.encode("ascii"),
            PLANAR_MECHANICAL_V1_EMITTER_CONTRACT_SHA256.encode("ascii"),
            _EXPECTED_PROOF_CATALOG_SHA256.encode("ascii"),
            b"canonical-sketch-v1;canonical-pfg-v2;exact-proof;"
            b"static-pad-pocket-through-all;atomic-plan-sink;no-execution-authority",
            *(
                descriptor.codec_contract_sha256.encode("ascii")
                for descriptor in _EXPECTED_CODEC_DESCRIPTORS
            ),
            *(
                "|".join((term.term_ref_id, *_identity(term))).encode("utf-8")
                for term in sorted(_CONTRACT_TERMS, key=lambda item: item.term_ref_id)
            ),
        )
    )
).hexdigest()

FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_planar_mechanical_v1_adapter",
    adapter_version="1.0.0",
    adapter_contract_sha256=_ADAPTER_CONTRACT_SHA256,
)

PLANAR_REQUEST_TERMS: Final = tuple(
    sorted(
        (
            ROLE_SKETCH_INTENT,
            ROLE_PARAMETRIC_INTENT,
            SKETCH_INTENT_GRAPH_SCHEMA_TERM,
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PLANAR_CAPABILITY_DOCUMENT_ROLE_TERM,
            PLANAR_CAPABILITY_SCHEMA_TERM,
            PLANAR_PLAN_DOCUMENT_ROLE_TERM,
            PLANAR_PLAN_SCHEMA_TERM,
        ),
        key=lambda item: item.term_ref_id,
    )
)


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")


def _capability_mapping() -> dict[str, object]:
    return {
        "schema_version": 1,
        "authority": "none",
        "adapter": FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR.to_mapping(),
        "backend": {
            "engine": "FreeCAD",
            "engine_version": "1.1.0",
            "engine_build_id": PLANAR_MECHANICAL_FREECAD_ENGINE_BUILD_ID,
        },
        "source_contracts": {
            "planar_rule_set_sha256": PLANAR_MECHANICAL_V1_RULE_SET_CONTRACT_SHA256,
            "planar_emitter_sha256": PLANAR_MECHANICAL_V1_EMITTER_CONTRACT_SHA256,
            "proof_catalog_sha256": _EXPECTED_PROOF_CATALOG_SHA256,
            "codec_contracts": [
                {
                    "schema_identity": list(item.schema_term.semantic_identity),
                    "codec_contract_sha256": item.codec_contract_sha256,
                }
                for item in _EXPECTED_CODEC_DESCRIPTORS
            ],
        },
        "native_rule": {
            "rule_id": PLANAR_MECHANICAL_RULE_ID,
            "rule_contract_sha256": PLANAR_MECHANICAL_RULE_CONTRACT_SHA256,
            "mapping": "rectangle->pad;circle+extrusion/remove+through-all->pocket",
            "maximum_circles": 16,
        },
    }


def planar_mechanical_capability_payload() -> bytes:
    return _canonical_json(_capability_mapping())


def build_planar_mechanical_capability_document(
    *, artifact_id: str = "artifact_freecad_planar_mechanical_capability"
) -> tuple[DocumentRef, bytes]:
    payload = planar_mechanical_capability_payload()
    digest = hashlib.sha256(_CAPABILITY_DIGEST_DOMAIN + payload).hexdigest()
    return (
        DocumentRef(
            artifact_id=artifact_id,
            role_term_ref_id=PLANAR_CAPABILITY_DOCUMENT_ROLE_TERM.term_ref_id,
            schema_term_ref_id=PLANAR_CAPABILITY_SCHEMA_TERM.term_ref_id,
            document_id=f"freecad_planar_mechanical_capability_{digest[:32]}",
            document_digest=digest,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type=("application/vnd.vibecad.freecad-planar-mechanical-capability+json"),
        ),
        payload,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class LoweredPlanarMechanicalPlanReceipt:
    request_digest: str
    adapter: AdapterDescriptor
    sketch_document: DocumentRef
    parametric_document: DocumentRef
    plan_document: DocumentRef
    receipt_id: str = field(init=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.request_digest) is not str
            or len(self.request_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.request_digest)
            or type(self.adapter) is not AdapterDescriptor
            or type(self.sketch_document) is not DocumentRef
            or type(self.parametric_document) is not DocumentRef
            or type(self.plan_document) is not DocumentRef
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/receipt")
        body = {
            "authority": "none",
            "request_digest": self.request_digest,
            "adapter": self.adapter.to_mapping(),
            "sketch_document": self.sketch_document.to_mapping(),
            "parametric_document": self.parametric_document.to_mapping(),
            "plan_document": self.plan_document.to_mapping(),
        }
        digest = hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()
        object.__setattr__(self, "receipt_sha256", digest)
        object.__setattr__(self, "receipt_id", f"planar_lowering_{digest[:32]}")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _require_request_terms(request: BackendLoweringRequest) -> None:
    by_id = {item.term_ref_id: item for item in request.terms}
    for expected in PLANAR_REQUEST_TERMS:
        actual = by_id.get(expected.term_ref_id)
        if actual is None or _identity(actual) != _identity(expected):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/terms")


def _require_codec_contracts(codecs: TrustedCodecRegistry) -> None:
    if type(codecs) is not TrustedCodecRegistry:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/codecs")
    for expected in _EXPECTED_CODEC_DESCRIPTORS:
        codec = codecs.codec_for(expected.schema_term)
        try:
            actual = None if codec is None else codec.descriptor
        except Exception:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/codecs")
        if actual != expected:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/codecs")


def _document_by_media(documents, media_type: str, path: str):
    matches = tuple(item for item in documents if item.document.media_type == media_type)
    if len(matches) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)
    return matches[0]


def _source_binding(document: DocumentRef) -> PlanarDocumentBinding:
    return PlanarDocumentBinding(
        artifact_id=document.artifact_id,
        document_id=document.document_id,
        document_digest=document.document_digest,
        content_sha256=document.content_sha256,
    )


def _build_plan(
    *,
    request: BackendLoweringRequest,
    sketch_document: DocumentRef,
    parametric_document: DocumentRef,
    visual_payload: bytes,
    sketch_payload: bytes,
    parametric_payload: bytes,
) -> tuple[PlanarMechanicalBackendPlan, tuple[SubjectRef, SubjectRef]]:
    visual = decode_visual_feature_graph(visual_payload)
    evidence = analyze_visual_feature_graph(visual)
    if evidence is None:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/proof_bundle/evidence")
    expected_sketch, expected_parametric = build_intent_graphs(evidence)
    if not hmac.compare_digest(
        sketch_payload, encode_sketch_intent_graph(expected_sketch)
    ) or not hmac.compare_digest(
        parametric_payload,
        encode_parametric_feature_graph_v2(expected_parametric),
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/intent_documents")
    sketch = decode_sketch_intent_graph(sketch_payload)
    graph = decode_parametric_feature_graph_v2(
        parametric_payload,
        expected_sha256=parametric_document.document_digest,
    )
    if sketch != expected_sketch or graph != expected_parametric:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_documents")

    rectangle = PlanarRectangleProfile(
        geometry_id="geometry.outer",
        profile_result_id="result.profiles.outer",
        center_x_mm=evidence.rectangle.center[0],
        center_y_mm=evidence.rectangle.center[1],
        half_width_mm=evidence.rectangle.half_extents[0],
        half_height_mm=evidence.rectangle.half_extents[1],
        rotation_radians=evidence.rectangle.rotation_radians,
    )
    circles = []
    base_node_id = "node.add"
    base_result_id = "result.add.solid"
    for index, circle in enumerate(evidence.circles):
        suffix = f"{index:03d}"
        node_id = f"node.remove.{suffix}"
        result_id = f"result.remove.{suffix}.solid"
        circles.append(
            PlanarCircleRemoval(
                geometry_id=f"geometry.inner.{suffix}",
                profile_result_id=f"result.profiles.inner.{suffix}",
                node_id=node_id,
                result_id=result_id,
                base_node_id=base_node_id,
                base_result_id=base_result_id,
                center_x_mm=circle.center[0],
                center_y_mm=circle.center[1],
                radius_mm=circle.radius,
            )
        )
        base_node_id = node_id
        base_result_id = result_id
    plan = PlanarMechanicalBackendPlan(
        sketch_document=_source_binding(sketch_document),
        parametric_document=_source_binding(parametric_document),
        lowering_request_sha256=request.request_digest,
        adapter_contract_sha256=request.adapter.adapter_contract_sha256,
        body_id="body.main",
        profiles_node_id="node.profiles",
        add_node_id="node.add",
        add_result_id="result.add.solid",
        final_node_id=base_node_id,
        final_result_id=base_result_id,
        depth_parameter_id="parameter.depth",
        depth_mm=evidence.depth_mm,
        rectangle=rectangle,
        circles=tuple(circles),
    )
    targets = (
        SubjectRef(
            artifact_id=sketch_document.artifact_id,
            selector_kind_term_ref_id=SKETCH_ROOT_SELECTOR_TERM.term_ref_id,
            selector_id=sketch.sketch_id,
        ),
        SubjectRef(
            artifact_id=parametric_document.artifact_id,
            selector_kind_term_ref_id=PFG_SELECTOR_GRAPH_RESULT.term_ref_id,
            selector_id="selection.primary",
        ),
    )
    return plan, targets


def _plan_document(plan: PlanarMechanicalBackendPlan) -> DocumentRef:
    payload = plan.canonical_bytes
    content_sha256 = hashlib.sha256(payload).hexdigest()
    semantic_digest = hashlib.sha256(
        _PLAN_DOCUMENT_DIGEST_DOMAIN + bytes.fromhex(plan.plan_sha256)
    ).hexdigest()
    return DocumentRef(
        artifact_id=f"artifact_freecad_planar_plan_{content_sha256[:32]}",
        role_term_ref_id=PLANAR_PLAN_DOCUMENT_ROLE_TERM.term_ref_id,
        schema_term_ref_id=PLANAR_PLAN_SCHEMA_TERM.term_ref_id,
        document_id=f"freecad_planar_plan_{semantic_digest[:32]}",
        document_digest=plan.plan_sha256,
        content_sha256=content_sha256,
        size_bytes=len(payload),
        media_type=PLANAR_MECHANICAL_PLAN_MEDIA_TYPE,
    )


class FreeCADPlanarMechanicalAdapter:
    """Exact PM1 Sketch/PFG-to-plan adapter; never executes the plan."""

    __slots__ = ("_sink",)

    def __init__(self, sink: PlanSink) -> None:
        if not isinstance(sink, PlanSink):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/plan_sink")
        self._sink = sink

    @property
    def descriptor(self) -> AdapterDescriptor:
        return FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    def lower(
        self,
        request: BackendLoweringRequest,
        *,
        artifacts: ArtifactReader,
        codecs: TrustedCodecRegistry,
        proof_policy: TrustedProofPolicy,
    ) -> BackendLoweringResult:
        result, _ = self.lower_with_receipt(
            request,
            artifacts=artifacts,
            codecs=codecs,
            proof_policy=proof_policy,
        )
        return result

    def lower_with_receipt(
        self,
        request: BackendLoweringRequest,
        *,
        artifacts: ArtifactReader,
        codecs: TrustedCodecRegistry,
        proof_policy: TrustedProofPolicy,
    ) -> tuple[BackendLoweringResult, LoweredPlanarMechanicalPlanReceipt]:
        if type(request) is not BackendLoweringRequest or request.adapter != self.descriptor:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/adapter")
        _require_request_terms(request)
        _require_codec_contracts(codecs)
        try:
            catalog_sha256 = proof_policy.catalog_sha256
        except Exception:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/proof_policy")
        if type(catalog_sha256) is not str or not hmac.compare_digest(
            catalog_sha256, _EXPECTED_PROOF_CATALOG_SHA256
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/proof_policy")
        if (
            len(request.documents) != 3
            or len(request.intent_artifact_ids) != 2
            or len(request.capability_artifact_ids) != 1
            or set(request.intent_artifact_ids) & set(request.capability_artifact_ids)
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/request/scope")
        if len(request.proof_bundle.assertions) > request.budget.max_rule_applications:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/request/budget/rules")
        document_by_id = {item.artifact_id: item for item in request.documents}
        intent_documents = tuple(document_by_id[item] for item in request.intent_artifact_ids)
        capability_document = document_by_id[request.capability_artifact_ids[0]]
        sketch_matches = tuple(
            item for item in intent_documents if item.media_type == SKETCH_INTENT_GRAPH_MEDIA_TYPE
        )
        parametric_matches = tuple(
            item
            for item in intent_documents
            if item.media_type == PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE
        )
        if len(sketch_matches) != 1 or len(parametric_matches) != 1:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/intent_artifact_ids")
        sketch_document = sketch_matches[0]
        parametric_document = parametric_matches[0]
        request_terms = {item.term_ref_id: item for item in request.terms}
        expected_document_terms = (
            (sketch_document, ROLE_SKETCH_INTENT, SKETCH_INTENT_GRAPH_SCHEMA_TERM),
            (
                parametric_document,
                ROLE_PARAMETRIC_INTENT,
                PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            ),
            (
                capability_document,
                PLANAR_CAPABILITY_DOCUMENT_ROLE_TERM,
                PLANAR_CAPABILITY_SCHEMA_TERM,
            ),
        )
        for document, role, schema in expected_document_terms:
            try:
                actual_role = request_terms[document.role_term_ref_id]
                actual_schema = request_terms[document.schema_term_ref_id]
            except KeyError:
                _fail(IntentBridgeErrorCode.UNKNOWN_REFERENCE, "/documents")
            if _identity(actual_role) != _identity(role) or _identity(actual_schema) != _identity(
                schema
            ):
                _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/documents")

        proof_bytes = sum(item.size_bytes for item in request.proof_bundle.documents)
        if proof_bytes + capability_document.size_bytes > request.budget.max_input_bytes:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/request/budget")
        capability_payload = read_verified_document(
            artifacts,
            capability_document,
            maximum_bytes=capability_document.size_bytes,
        )
        expected_capability = planar_mechanical_capability_payload()
        expected_capability_digest = hashlib.sha256(
            _CAPABILITY_DIGEST_DOMAIN + expected_capability
        ).hexdigest()
        if (
            capability_document.media_type
            != "application/vnd.vibecad.freecad-planar-mechanical-capability+json"
            or not hmac.compare_digest(capability_payload, expected_capability)
            or not hmac.compare_digest(
                capability_document.document_digest, expected_capability_digest
            )
            or capability_document.document_id
            != f"freecad_planar_mechanical_capability_{expected_capability_digest[:32]}"
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/capability_document")

        report = validate_proof_bundle(
            request.proof_bundle,
            reader=artifacts,
            codecs=codecs,
            proof_policy=proof_policy,
            maximum_total_bytes=proof_bytes,
            maximum_subject_lookups=request.budget.max_subject_lookups,
        )
        if (
            report.disposition is not BridgeDisposition.COMPLETE
            or len(report.documents.validated) != 3
            or report.inert_subjects
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/proof_bundle")
        visual_item = _document_by_media(
            report.documents.validated,
            VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
            "/proof_bundle/visual",
        )
        sketch_item = _document_by_media(
            report.documents.validated,
            SKETCH_INTENT_GRAPH_MEDIA_TYPE,
            "/proof_bundle/sketch",
        )
        parametric_item = _document_by_media(
            report.documents.validated,
            PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
            "/proof_bundle/parametric",
        )
        if (
            sketch_item.document != sketch_document
            or parametric_item.document != parametric_document
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/proof_bundle/documents")
        try:
            plan, targets = _build_plan(
                request=request,
                sketch_document=sketch_document,
                parametric_document=parametric_document,
                visual_payload=visual_item.payload,
                sketch_payload=sketch_item.payload,
                parametric_payload=parametric_item.payload,
            )
        except IntentBridgeError:
            raise
        except (
            VisualFeatureGraphError,
            SketchIntentError,
            ParametricFeatureGraphError,
            PlanarMechanicalRuleError,
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_documents")
        except Exception:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_documents")
        resolved = {item.subject for item in report.resolved_subjects}
        if any(item not in resolved for item in targets):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/proof_bundle/targets")

        plan_document = _plan_document(plan)
        payload = plan.canonical_bytes
        if len(payload) > min(request.budget.max_output_bytes, MAX_PLANAR_MECHANICAL_PLAN_BYTES):
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/plan_document")
        try:
            decode_planar_mechanical_plan(
                payload,
                expected_content_sha256=plan_document.content_sha256,
                expected_plan_sha256=plan_document.document_digest,
            )
        except PlanarMechanicalRuleError:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_document")
        result = BackendLoweringResult(
            request_digest=request.request_digest,
            adapter=self.descriptor,
            disposition=BridgeDisposition.COMPLETE,
            plan_document=plan_document,
            supported_subjects=targets,
        )
        validate_lowering_result(request, result)
        receipt = LoweredPlanarMechanicalPlanReceipt(
            request_digest=request.request_digest,
            adapter=self.descriptor,
            sketch_document=sketch_document,
            parametric_document=parametric_document,
            plan_document=plan_document,
        )
        try:
            published = self._sink.publish_exact(plan_document, payload)
        except IntentBridgeError:
            raise
        except (Exception, SystemExit):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink")
        if type(published) is not bytes or not hmac.compare_digest(published, payload):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink/readback")
        return result, receipt

    def read_plan(
        self,
        receipt: LoweredPlanarMechanicalPlanReceipt,
    ) -> tuple[PlanarMechanicalBackendPlan, bytes]:
        if (
            type(receipt) is not LoweredPlanarMechanicalPlanReceipt
            or receipt.adapter != self.descriptor
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/receipt")
        document = receipt.plan_document
        if (
            document.role_term_ref_id != PLANAR_PLAN_DOCUMENT_ROLE_TERM.term_ref_id
            or document.schema_term_ref_id != PLANAR_PLAN_SCHEMA_TERM.term_ref_id
            or document.media_type != PLANAR_MECHANICAL_PLAN_MEDIA_TYPE
            or document.size_bytes > MAX_PLANAR_MECHANICAL_PLAN_BYTES
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/receipt/plan_document")
        try:
            payload = self._sink.read_exact(document, MAX_PLANAR_MECHANICAL_PLAN_BYTES)
        except (Exception, SystemExit):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink")
        if (
            type(payload) is not bytes
            or len(payload) != document.size_bytes
            or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), document.content_sha256)
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink/readback")
        try:
            plan = decode_planar_mechanical_plan(
                payload,
                expected_content_sha256=document.content_sha256,
                expected_plan_sha256=document.document_digest,
            )
        except PlanarMechanicalRuleError:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_document")
        sources = (
            (plan.sketch_document, receipt.sketch_document),
            (plan.parametric_document, receipt.parametric_document),
        )
        if (
            not hmac.compare_digest(plan.lowering_request_sha256, receipt.request_digest)
            or not hmac.compare_digest(
                plan.adapter_contract_sha256,
                receipt.adapter.adapter_contract_sha256,
            )
            or any(
                source
                != PlanarDocumentBinding(
                    artifact_id=document_ref.artifact_id,
                    document_id=document_ref.document_id,
                    document_digest=document_ref.document_digest,
                    content_sha256=document_ref.content_sha256,
                )
                for source, document_ref in sources
            )
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/receipt/source_documents")
        return plan, payload


__all__ = [
    "FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR",
    "PLANAR_CAPABILITY_DOCUMENT_ROLE_TERM",
    "PLANAR_CAPABILITY_SCHEMA_TERM",
    "PLANAR_PLAN_DOCUMENT_ROLE_TERM",
    "PLANAR_PLAN_SCHEMA_TERM",
    "PLANAR_REQUEST_TERMS",
    "FreeCADPlanarMechanicalAdapter",
    "LoweredPlanarMechanicalPlanReceipt",
    "build_planar_mechanical_capability_document",
    "planar_mechanical_capability_payload",
]
