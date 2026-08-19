"""Private PFGv2 lowering for the reviewed PartDesign reference family."""

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
    PFG_SELECTOR_FEATURE_NODE,
)
from vibecad.intent_bridge.ports import (
    ArtifactReader,
    TrustedCodecRegistry,
    TrustedProofPolicy,
    read_verified_document,
    validate_lowering_result,
    validate_proof_bundle,
)
from vibecad.parametric.feature_graph_v2 import (
    ParametricFeatureGraphError,
    ParametricFeatureGraphV2,
    SemanticReferenceScope,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_partdesign_reference_rules import (
    MAX_REFERENCE_PLAN_BYTES,
    REFERENCE_FREECAD_ENGINE_BUILD_ID,
    REFERENCE_PLAN_MEDIA_TYPE,
    REFERENCE_RULE_CONTRACT_SHA256,
    REFERENCE_RULE_ID,
    PartDesignReferenceKind,
    PartDesignReferencePlan,
    ReferenceRuleError,
    decode_partdesign_reference_plan,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.freecad-partdesign"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.freecad-partdesign-ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.freecad-reference-adapter.v1\0"
_CAPABILITY_DIGEST_DOMAIN = b"vibecad.freecad-reference-capability.v1\0"
_PLAN_DOCUMENT_DIGEST_DOMAIN = b"vibecad.freecad-reference-plan-document.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-reference-lowering-receipt.v1\0"


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


REFERENCE_INTENT_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_parametric_intent", "document-role.parametric-intent"
)
REFERENCE_CAPABILITY_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_reference_capability", "document-role.freecad-reference-capability"
)
REFERENCE_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_freecad_reference_capability_v1",
    "document-schema.freecad-reference-capability-v1",
)
REFERENCE_PLAN_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_reference_backend_plan", "document-role.freecad-backend-plan"
)
REFERENCE_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_freecad_reference_plan_v1", "document-schema.freecad-reference-plan-v1"
)

REFERENCE_STRUCTURE_TERM: Final = _pfg_term(
    "structure_partdesign_reference", "structure.partdesign-reference-object"
)
REFERENCE_FAMILY_TERM: Final = _pfg_term("family_partdesign_reference", "feature-family.reference")
REFERENCE_SUPPORT_ROLE_TERM: Final = _pfg_term(
    "role_reference_support", "input-role.support-reference"
)
REFERENCE_SUPPORT_TYPE_TERM: Final = _pfg_term("type_shape_reference", "value-type.shape-reference")
REFERENCE_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_reference_result", "result-role.reference-object"
)
REFERENCE_LOCATOR_TERM: Final = _pfg_term(
    "locator_authenticated_support", "reference-locator.authenticated-content-v1"
)

REFERENCE_OPERATION_TERMS: Final = {
    PartDesignReferenceKind.DATUM_PLANE: _pfg_term(
        "operation_datum_plane", "operation.partdesign-datum-plane"
    ),
    PartDesignReferenceKind.DATUM_LINE: _pfg_term(
        "operation_datum_line", "operation.partdesign-datum-line"
    ),
    PartDesignReferenceKind.DATUM_POINT: _pfg_term(
        "operation_datum_point", "operation.partdesign-datum-point"
    ),
    PartDesignReferenceKind.SHAPE_BINDER: _pfg_term(
        "operation_shape_binder", "operation.partdesign-shape-binder"
    ),
    PartDesignReferenceKind.SUBSHAPE_BINDER: _pfg_term(
        "operation_subshape_binder", "operation.partdesign-subshape-binder"
    ),
}
REFERENCE_RESULT_TYPE_TERMS: Final = {
    PartDesignReferenceKind.DATUM_PLANE: _pfg_term("type_datum_plane", "value-type.datum-plane"),
    PartDesignReferenceKind.DATUM_LINE: _pfg_term("type_datum_line", "value-type.datum-line"),
    PartDesignReferenceKind.DATUM_POINT: _pfg_term("type_datum_point", "value-type.datum-point"),
    PartDesignReferenceKind.SHAPE_BINDER: _pfg_term("type_shape_binder", "value-type.bound-shape"),
    PartDesignReferenceKind.SUBSHAPE_BINDER: _pfg_term(
        "type_subshape_binder", "value-type.bound-subshape"
    ),
}

REFERENCE_PFG_TERMS: Final = (
    REFERENCE_STRUCTURE_TERM,
    REFERENCE_FAMILY_TERM,
    REFERENCE_SUPPORT_ROLE_TERM,
    REFERENCE_SUPPORT_TYPE_TERM,
    REFERENCE_RESULT_ROLE_TERM,
    REFERENCE_LOCATOR_TERM,
    *REFERENCE_OPERATION_TERMS.values(),
    *REFERENCE_RESULT_TYPE_TERMS.values(),
)


def _as_bridge(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


REFERENCE_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    REFERENCE_INTENT_DOCUMENT_ROLE_TERM,
    REFERENCE_CAPABILITY_DOCUMENT_ROLE_TERM,
    REFERENCE_CAPABILITY_SCHEMA_TERM,
    REFERENCE_PLAN_DOCUMENT_ROLE_TERM,
    REFERENCE_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in REFERENCE_PFG_TERMS),
)

_ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            REFERENCE_RULE_ID.encode("ascii"),
            REFERENCE_RULE_CONTRACT_SHA256.encode("ascii"),
            b"canonical-pfg-v2;exact-proof;five-static-operations;"
            b"authenticated-support;atomic-plan-sink;no-execution-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in (*REFERENCE_REQUEST_TERMS, PFG_SELECTOR_FEATURE_NODE)
            ),
        )
    )
).hexdigest()

FREECAD_REFERENCE_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_partdesign_reference_adapter",
    adapter_version="1.0.0",
    adapter_contract_sha256=_ADAPTER_CONTRACT_SHA256,
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


def _identity(term: object) -> tuple[str, str, str, str]:
    try:
        result = (
            term.namespace,
            term.vocabulary_version,
            term.term_id,
            term.term_definition_sha256,
        )
    except Exception:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/terms")
    return result


def _capability_mapping() -> dict[str, object]:
    return {
        "schema_version": 1,
        "authority": "none",
        "adapter": FREECAD_REFERENCE_ADAPTER_DESCRIPTOR.to_mapping(),
        "backend": {
            "engine": "FreeCAD",
            "engine_version": "1.1.0",
            "engine_build_id": REFERENCE_FREECAD_ENGINE_BUILD_ID,
        },
        "rule": {
            "rule_id": REFERENCE_RULE_ID,
            "rule_contract_sha256": REFERENCE_RULE_CONTRACT_SHA256,
            "operations": sorted(kind.value for kind in PartDesignReferenceKind),
        },
    }


def reference_capability_payload() -> bytes:
    return _canonical_json(_capability_mapping())


def build_reference_capability_document(
    *, artifact_id: str = "artifact_freecad_reference_capability"
) -> tuple[DocumentRef, bytes]:
    payload = reference_capability_payload()
    digest = hashlib.sha256(_CAPABILITY_DIGEST_DOMAIN + payload).hexdigest()
    return (
        DocumentRef(
            artifact_id=artifact_id,
            role_term_ref_id=REFERENCE_CAPABILITY_DOCUMENT_ROLE_TERM.term_ref_id,
            schema_term_ref_id=REFERENCE_CAPABILITY_SCHEMA_TERM.term_ref_id,
            document_id=f"freecad_reference_capability_{digest[:32]}",
            document_digest=digest,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type="application/vnd.vibecad.freecad-reference-capability+json",
        ),
        payload,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class LoweredReferencePlanReceipt:
    request_digest: str
    adapter: AdapterDescriptor
    source_document: DocumentRef
    plan_document: DocumentRef
    receipt_id: str = field(init=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.request_digest) is not str
            or len(self.request_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.request_digest)
            or type(self.adapter) is not AdapterDescriptor
            or type(self.source_document) is not DocumentRef
            or type(self.plan_document) is not DocumentRef
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/receipt")
        body = {
            "authority": "none",
            "request_digest": self.request_digest,
            "adapter": self.adapter.to_mapping(),
            "source_document": self.source_document.to_mapping(),
            "plan_document": self.plan_document.to_mapping(),
        }
        digest = hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()
        object.__setattr__(self, "receipt_sha256", digest)
        object.__setattr__(self, "receipt_id", f"reference_lowering_{digest[:32]}")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _assert_graph_closed(graph: ParametricFeatureGraphV2) -> None:
    if (
        len(graph.bodies) != 1
        or len(graph.nodes) != 1
        or len(graph.references) != 1
        or len(graph.graph_results) != 1
        or graph.parameters
        or graph.extensions
        or graph.bodies[0].extension_ids
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")
    reference = graph.references[0]
    node = graph.nodes[0]
    if (
        reference.extension_ids
        or reference.occurrence_path
        or reference.qualifier_term_ref_ids
        or node.extension_ids
        or node.intent.extension_ids
        or any(item.extension_ids for item in node.intent.input_ports)
        or any(item.extension_ids for item in node.results)
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/extensions")


def _match_kind(
    graph: ParametricFeatureGraphV2,
) -> tuple[PartDesignReferenceKind, dict[str, SemanticTermRefV2]]:
    terms = {term.term_ref_id: term for term in graph.terms}
    required = (
        REFERENCE_STRUCTURE_TERM,
        REFERENCE_FAMILY_TERM,
        REFERENCE_SUPPORT_ROLE_TERM,
        REFERENCE_SUPPORT_TYPE_TERM,
        REFERENCE_RESULT_ROLE_TERM,
        REFERENCE_LOCATOR_TERM,
    )
    for expected in required:
        actual = terms.get(expected.term_ref_id)
        if actual is None or _identity(actual) != _identity(expected):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/terms")
    node = graph.nodes[0]
    if _identity(terms[node.intent.structural_kind_term_ref_id]) != _identity(
        REFERENCE_STRUCTURE_TERM
    ) or _identity(terms[node.intent.family_term_ref_id]) != _identity(REFERENCE_FAMILY_TERM):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/node")
    matches = tuple(
        kind
        for kind, operation in REFERENCE_OPERATION_TERMS.items()
        if _identity(terms[node.intent.operation_term_ref_id]) == _identity(operation)
    )
    if len(matches) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/operation")
    return matches[0], terms


def _build_plan(
    document: DocumentRef,
    graph: ParametricFeatureGraphV2,
    request_digest: str,
) -> tuple[PartDesignReferencePlan, SubjectRef]:
    _assert_graph_closed(graph)
    kind, terms = _match_kind(graph)
    body = graph.bodies[0]
    node = graph.nodes[0]
    reference = graph.references[0]
    result = node.results[0] if len(node.results) == 1 else None
    port = node.intent.input_ports[0] if len(node.intent.input_ports) == 1 else None
    binding = node.intent.references[0] if len(node.intent.references) == 1 else None
    selection = graph.graph_results[0]
    if (
        node.body_id != body.body_id
        or node.intent.dependencies
        or node.intent.parameter_bindings
        or port is None
        or binding is None
        or result is None
        or port.minimum_cardinality != 1
        or port.maximum_cardinality != 1
        or port.ordered
        or binding.port_id != port.port_id
        or binding.ordinal != 0
        or binding.reference_id != reference.reference_id
        or _identity(terms[port.semantic_role_term_ref_id])
        != _identity(REFERENCE_SUPPORT_ROLE_TERM)
        or _identity(terms[port.value_type_term_ref_id]) != _identity(REFERENCE_SUPPORT_TYPE_TERM)
        or reference.scope is not SemanticReferenceScope.EXTERNAL
        or reference.source_content_sha256 is None
        or _identity(terms[reference.semantic_role_term_ref_id])
        != _identity(REFERENCE_SUPPORT_ROLE_TERM)
        or _identity(terms[reference.value_type_term_ref_id])
        != _identity(REFERENCE_SUPPORT_TYPE_TERM)
        or _identity(terms[reference.locator_term_ref_id]) != _identity(REFERENCE_LOCATOR_TERM)
        or _identity(terms[result.semantic_role_term_ref_id])
        != _identity(REFERENCE_RESULT_ROLE_TERM)
        or _identity(terms[result.value_type_term_ref_id])
        != _identity(REFERENCE_RESULT_TYPE_TERMS[kind])
        or selection.node_id != node.node_id
        or selection.result_id != result.result_id
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/bindings")
    plan = PartDesignReferencePlan(
        source_artifact_id=document.artifact_id,
        source_graph_id=graph.graph_id,
        source_graph_sha256=graph.graph_sha256,
        source_content_sha256=document.content_sha256,
        lowering_request_sha256=request_digest,
        adapter_contract_sha256=FREECAD_REFERENCE_ADAPTER_DESCRIPTOR.adapter_contract_sha256,
        body_id=body.body_id,
        node_id=node.node_id,
        result_id=result.result_id,
        support_reference_id=reference.reference_id,
        support_reference_sha256=reference.source_content_sha256,
        kind=kind,
    )
    return (
        plan,
        SubjectRef(
            artifact_id=document.artifact_id,
            selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
            selector_id=node.node_id,
        ),
    )


def _plan_document(plan: PartDesignReferencePlan) -> DocumentRef:
    payload = plan.canonical_bytes
    digest = plan.plan_sha256
    expected = hashlib.sha256(_PLAN_DOCUMENT_DIGEST_DOMAIN + payload).hexdigest()
    # The plan's native domain digest is the semantic document digest. The
    # additional adapter domain is bound into the content-addressed artifact id.
    return DocumentRef(
        artifact_id=f"artifact_freecad_reference_plan_{expected[:24]}",
        role_term_ref_id=REFERENCE_PLAN_DOCUMENT_ROLE_TERM.term_ref_id,
        schema_term_ref_id=REFERENCE_PLAN_SCHEMA_TERM.term_ref_id,
        document_id=f"freecad_reference_plan_{digest[:32]}",
        document_digest=digest,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        media_type=REFERENCE_PLAN_MEDIA_TYPE,
    )


class FreeCADPartDesignReferenceAdapter:
    """Exact PFGv2-to-plan adapter for five reviewed reference operations."""

    descriptor = FREECAD_REFERENCE_ADAPTER_DESCRIPTOR

    def __init__(self, sink: PlanSink) -> None:
        if not isinstance(sink, PlanSink):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/plan_sink")
        self._sink = sink

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
        result, _receipt = self.lower_with_receipt(
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
    ) -> tuple[BackendLoweringResult, LoweredReferencePlanReceipt]:
        if type(request) is not BackendLoweringRequest or request.adapter != self.descriptor:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/adapter")
        if type(codecs) is not TrustedCodecRegistry:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/codecs")
        identities = {_identity(term) for term in request.terms}
        if any(_identity(term) not in identities for term in REFERENCE_REQUEST_TERMS):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/terms")
        if (
            len(request.documents) != 2
            or len(request.intent_artifact_ids) != 1
            or len(request.capability_artifact_ids) != 1
            or request.intent_artifact_ids == request.capability_artifact_ids
            or sum(document.size_bytes for document in request.documents)
            > request.budget.max_input_bytes
            or len(request.proof_bundle.assertions) > request.budget.max_rule_applications
        ):
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/request/scope")
        documents = {document.artifact_id: document for document in request.documents}
        intent_document = documents[request.intent_artifact_ids[0]]
        capability_document = documents[request.capability_artifact_ids[0]]
        request_terms = {term.term_ref_id: term for term in request.terms}
        if (
            _identity(request_terms[intent_document.role_term_ref_id])
            != _identity(REFERENCE_INTENT_DOCUMENT_ROLE_TERM)
            or _identity(request_terms[intent_document.schema_term_ref_id])
            != _identity(PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM)
            or intent_document.media_type != PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE
            or _identity(request_terms[capability_document.role_term_ref_id])
            != _identity(REFERENCE_CAPABILITY_DOCUMENT_ROLE_TERM)
            or _identity(request_terms[capability_document.schema_term_ref_id])
            != _identity(REFERENCE_CAPABILITY_SCHEMA_TERM)
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/documents")
        capability_payload = read_verified_document(
            artifacts,
            capability_document,
            maximum_bytes=request.budget.max_input_bytes - intent_document.size_bytes,
        )
        expected_capability = reference_capability_payload()
        expected_digest = hashlib.sha256(
            _CAPABILITY_DIGEST_DOMAIN + expected_capability
        ).hexdigest()
        if (
            not hmac.compare_digest(capability_payload, expected_capability)
            or capability_document.media_type
            != "application/vnd.vibecad.freecad-reference-capability+json"
            or not hmac.compare_digest(capability_document.document_digest, expected_digest)
            or capability_document.document_id
            != f"freecad_reference_capability_{expected_digest[:32]}"
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/capability_document")
        report = validate_proof_bundle(
            request.proof_bundle,
            reader=artifacts,
            codecs=codecs,
            proof_policy=proof_policy,
            maximum_total_bytes=intent_document.size_bytes,
            maximum_subject_lookups=request.budget.max_subject_lookups,
        )
        if (
            report.disposition is not BridgeDisposition.COMPLETE
            or len(report.documents.validated) != 1
            or report.documents.validated[0].document != intent_document
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/proof_bundle")
        payload = report.documents.validated[0].payload
        try:
            graph = decode_parametric_feature_graph_v2(
                payload, expected_sha256=intent_document.document_digest
            )
            plan, target = _build_plan(intent_document, graph, request.request_digest)
        except IntentBridgeError:
            raise
        except (ParametricFeatureGraphError, ReferenceRuleError):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_document")
        except Exception:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_document")
        if (
            tuple(item.subject for item in report.resolved_subjects) != (target,)
            or report.inert_subjects
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/proof_bundle/target")
        plan_document = _plan_document(plan)
        plan_payload = plan.canonical_bytes
        if len(plan_payload) > min(request.budget.max_output_bytes, MAX_REFERENCE_PLAN_BYTES):
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/plan_document")
        try:
            decode_partdesign_reference_plan(
                plan_payload,
                expected_content_sha256=plan_document.content_sha256,
                expected_plan_sha256=plan_document.document_digest,
            )
        except ReferenceRuleError:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_document")
        result = BackendLoweringResult(
            request_digest=request.request_digest,
            adapter=self.descriptor,
            disposition=BridgeDisposition.COMPLETE,
            plan_document=plan_document,
            supported_subjects=(target,),
        )
        validate_lowering_result(request, result)
        receipt = LoweredReferencePlanReceipt(
            request_digest=request.request_digest,
            adapter=self.descriptor,
            source_document=intent_document,
            plan_document=plan_document,
        )
        try:
            published = self._sink.publish_exact(plan_document, plan_payload)
        except IntentBridgeError:
            raise
        except (Exception, SystemExit):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink")
        if type(published) is not bytes or not hmac.compare_digest(published, plan_payload):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink/readback")
        return result, receipt

    def read_plan(
        self, receipt: LoweredReferencePlanReceipt
    ) -> tuple[PartDesignReferencePlan, bytes]:
        if type(receipt) is not LoweredReferencePlanReceipt or receipt.adapter != self.descriptor:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/receipt")
        document = receipt.plan_document
        if (
            document.role_term_ref_id != REFERENCE_PLAN_DOCUMENT_ROLE_TERM.term_ref_id
            or document.schema_term_ref_id != REFERENCE_PLAN_SCHEMA_TERM.term_ref_id
            or document.media_type != REFERENCE_PLAN_MEDIA_TYPE
            or document.size_bytes > MAX_REFERENCE_PLAN_BYTES
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/receipt/plan_document")
        try:
            payload = self._sink.read_exact(document, MAX_REFERENCE_PLAN_BYTES)
        except (Exception, SystemExit):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink")
        if (
            type(payload) is not bytes
            or len(payload) != document.size_bytes
            or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), document.content_sha256)
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink/readback")
        try:
            plan = decode_partdesign_reference_plan(
                payload,
                expected_content_sha256=document.content_sha256,
                expected_plan_sha256=document.document_digest,
            )
        except ReferenceRuleError:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_document")
        if (
            not hmac.compare_digest(plan.lowering_request_sha256, receipt.request_digest)
            or not hmac.compare_digest(
                plan.adapter_contract_sha256,
                receipt.adapter.adapter_contract_sha256,
            )
            or plan.source_artifact_id != receipt.source_document.artifact_id
            or plan.source_graph_id != receipt.source_document.document_id
            or not hmac.compare_digest(
                plan.source_graph_sha256, receipt.source_document.document_digest
            )
            or not hmac.compare_digest(
                plan.source_content_sha256, receipt.source_document.content_sha256
            )
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/receipt/source_document")
        return plan, payload


__all__ = [
    "FREECAD_REFERENCE_ADAPTER_DESCRIPTOR",
    "REFERENCE_CAPABILITY_DOCUMENT_ROLE_TERM",
    "REFERENCE_CAPABILITY_SCHEMA_TERM",
    "REFERENCE_FAMILY_TERM",
    "REFERENCE_INTENT_DOCUMENT_ROLE_TERM",
    "REFERENCE_LOCATOR_TERM",
    "REFERENCE_OPERATION_TERMS",
    "REFERENCE_PFG_TERMS",
    "REFERENCE_PLAN_DOCUMENT_ROLE_TERM",
    "REFERENCE_PLAN_SCHEMA_TERM",
    "REFERENCE_REQUEST_TERMS",
    "REFERENCE_RESULT_ROLE_TERM",
    "REFERENCE_RESULT_TYPE_TERMS",
    "REFERENCE_STRUCTURE_TERM",
    "REFERENCE_SUPPORT_ROLE_TERM",
    "REFERENCE_SUPPORT_TYPE_TERM",
    "FreeCADPartDesignReferenceAdapter",
    "LoweredReferencePlanReceipt",
    "build_reference_capability_document",
    "reference_capability_payload",
]
