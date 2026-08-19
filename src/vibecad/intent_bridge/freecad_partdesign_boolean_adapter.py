"""Private PFGv2 lowering for the reviewed PartDesign Boolean batch.

Only complete semantic identities can select Fuse, Cut, or Common.  Graph text
is never interpreted as a native TypeId, property, or enumeration label.  The
result is a canonical authority-free plan published through an atomic sink;
native execution remains a separate trusted-host action.
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
    FeatureNodeV2,
    ParametricFeatureGraphError,
    ParametricFeatureGraphV2,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_partdesign_boolean_rules import (
    MAX_PARTDESIGN_BOOLEAN_PLAN_BYTES,
    PARTDESIGN_BOOLEAN_FREECAD_ENGINE_BUILD_ID,
    PARTDESIGN_BOOLEAN_NATIVE_TYPE_ID,
    PARTDESIGN_BOOLEAN_PLAN_MEDIA_TYPE,
    PARTDESIGN_BOOLEAN_RULE_CONTRACT_SHA256,
    PARTDESIGN_BOOLEAN_RULE_ID,
    BooleanOperandSelection,
    PartDesignBooleanBackendPlan,
    PartDesignBooleanOperation,
    PartDesignBooleanRuleError,
    decode_partdesign_boolean_backend_plan,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.freecad-partdesign"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.freecad-partdesign-ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.freecad-partdesign-boolean-adapter.v1\0"
_CAPABILITY_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-boolean-capability.v1\0"
_PLAN_DOCUMENT_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-boolean-document.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-boolean-lowering-receipt.v1\0"


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


BOOLEAN_INTENT_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_parametric_intent", "document-role.parametric-intent"
)
BOOLEAN_CAPABILITY_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_partdesign_boolean_capability",
    "document-role.freecad-partdesign-boolean-capability",
)
BOOLEAN_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_freecad_partdesign_boolean_capability_v1",
    "document-schema.freecad-partdesign-boolean-capability-v1",
)
BOOLEAN_PLAN_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_backend_plan", "document-role.freecad-backend-plan"
)
BOOLEAN_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_freecad_partdesign_boolean_plan_v1",
    "document-schema.freecad-partdesign-boolean-plan-v1",
)

BOOLEAN_STRUCTURE_TERM: Final = _pfg_term(
    "structure_partdesign_body_feature", "structure.partdesign-body-feature"
)
BOOLEAN_FAMILY_TERM: Final = _pfg_term(
    "family_partdesign_boolean", "feature-family.partdesign-boolean"
)
BOOLEAN_BASE_ROLE_TERM: Final = _pfg_term("role_base_solid", "input-role.base-solid")
BOOLEAN_TOOLS_ROLE_TERM: Final = _pfg_term("role_tool_solids", "input-role.tool-solids")
BOOLEAN_SOLID_RESULT_ROLE_TERM: Final = _pfg_term("role_result_solid", "result-role.solid")
BOOLEAN_SOLID_TYPE_TERM: Final = _pfg_term("type_solid", "value-type.solid")


@dataclass(frozen=True, slots=True)
class BooleanOperationTerms:
    operation: PartDesignBooleanOperation
    operation_term: SemanticTermRefV2


BOOLEAN_OPERATION_TERMS: Final = (
    BooleanOperationTerms(
        PartDesignBooleanOperation.FUSE,
        _pfg_term("operation_partdesign_boolean_fuse", "operation.partdesign-boolean-fuse"),
    ),
    BooleanOperationTerms(
        PartDesignBooleanOperation.CUT,
        _pfg_term("operation_partdesign_boolean_cut", "operation.partdesign-boolean-cut"),
    ),
    BooleanOperationTerms(
        PartDesignBooleanOperation.COMMON,
        _pfg_term(
            "operation_partdesign_boolean_common",
            "operation.partdesign-boolean-common",
        ),
    ),
)

BOOLEAN_PFG_TERMS: Final = (
    BOOLEAN_STRUCTURE_TERM,
    BOOLEAN_FAMILY_TERM,
    BOOLEAN_BASE_ROLE_TERM,
    BOOLEAN_TOOLS_ROLE_TERM,
    BOOLEAN_SOLID_RESULT_ROLE_TERM,
    BOOLEAN_SOLID_TYPE_TERM,
    *(item.operation_term for item in BOOLEAN_OPERATION_TERMS),
)


def _as_bridge(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


BOOLEAN_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    BOOLEAN_INTENT_DOCUMENT_ROLE_TERM,
    BOOLEAN_CAPABILITY_DOCUMENT_ROLE_TERM,
    BOOLEAN_CAPABILITY_SCHEMA_TERM,
    BOOLEAN_PLAN_DOCUMENT_ROLE_TERM,
    BOOLEAN_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in BOOLEAN_PFG_TERMS),
)

_ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            PARTDESIGN_BOOLEAN_RULE_ID.encode("ascii"),
            PARTDESIGN_BOOLEAN_RULE_CONTRACT_SHA256.encode("ascii"),
            b"canonical-pfg-v2;exact-body-result-dependencies;one-tool;atomic-plan-sink;no-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in (*BOOLEAN_REQUEST_TERMS, PFG_SELECTOR_FEATURE_NODE)
            ),
        )
    )
).hexdigest()

FREECAD_PARTDESIGN_BOOLEAN_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_partdesign_boolean_adapter",
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
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")


def boolean_capability_payload() -> bytes:
    return _canonical_json(
        {
            "schema_version": 1,
            "authority": "none",
            "adapter": FREECAD_PARTDESIGN_BOOLEAN_ADAPTER_DESCRIPTOR.to_mapping(),
            "backend": {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PARTDESIGN_BOOLEAN_FREECAD_ENGINE_BUILD_ID,
                "native_type_id": PARTDESIGN_BOOLEAN_NATIVE_TYPE_ID,
            },
            "rule": {
                "rule_id": PARTDESIGN_BOOLEAN_RULE_ID,
                "rule_contract_sha256": PARTDESIGN_BOOLEAN_RULE_CONTRACT_SHA256,
                "operations": [item.operation.value for item in BOOLEAN_OPERATION_TERMS],
                "tool_cardinality": {"minimum": 1, "maximum": 1},
            },
        }
    )


def build_boolean_capability_document(
    *,
    artifact_id: str = "artifact_freecad_partdesign_boolean_capability",
) -> tuple[DocumentRef, bytes]:
    payload = boolean_capability_payload()
    digest = hashlib.sha256(_CAPABILITY_DIGEST_DOMAIN + payload).hexdigest()
    return (
        DocumentRef(
            artifact_id=artifact_id,
            role_term_ref_id=BOOLEAN_CAPABILITY_DOCUMENT_ROLE_TERM.term_ref_id,
            schema_term_ref_id=BOOLEAN_CAPABILITY_SCHEMA_TERM.term_ref_id,
            document_id=f"freecad_partdesign_boolean_capability_{digest[:32]}",
            document_digest=digest,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type="application/vnd.vibecad.freecad-partdesign-boolean-capability+json",
        ),
        payload,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class LoweredPartDesignBooleanPlanReceipt:
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
        object.__setattr__(self, "receipt_id", f"partdesign_boolean_lowering_{digest[:32]}")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


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


def _result_with_identity(
    node: FeatureNodeV2,
    terms: dict[str, SemanticTermRefV2],
    *,
    path: str,
):
    matches = tuple(
        result
        for result in node.results
        if terms.get(result.semantic_role_term_ref_id) is not None
        and terms.get(result.value_type_term_ref_id) is not None
        and _identity(terms[result.semantic_role_term_ref_id])
        == _identity(BOOLEAN_SOLID_RESULT_ROLE_TERM)
        and _identity(terms[result.value_type_term_ref_id]) == _identity(BOOLEAN_SOLID_TYPE_TERM)
    )
    if len(matches) != 1 or len(node.results) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)
    return matches[0]


def _assert_closed_graph(graph: ParametricFeatureGraphV2) -> None:
    if (
        graph.extensions
        or graph.parameters
        or graph.references
        or any(item.extension_ids for item in graph.bodies)
        or any(
            node.extension_ids
            or node.intent.extension_ids
            or node.intent.references
            or node.intent.parameter_bindings
            or any(port.extension_ids for port in node.intent.input_ports)
            or any(result.extension_ids for result in node.results)
            for node in graph.nodes
        )
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/extensions")


def _operation_for_target(
    target: FeatureNodeV2,
    terms: dict[str, SemanticTermRefV2],
) -> BooleanOperationTerms | None:
    structural = terms.get(target.intent.structural_kind_term_ref_id)
    family = terms.get(target.intent.family_term_ref_id)
    operation = terms.get(target.intent.operation_term_ref_id)
    if (
        structural is None
        or family is None
        or _identity(structural) != _identity(BOOLEAN_STRUCTURE_TERM)
        or _identity(family) != _identity(BOOLEAN_FAMILY_TERM)
    ):
        return None
    matches = tuple(
        item
        for item in BOOLEAN_OPERATION_TERMS
        if operation is not None and _identity(operation) == _identity(item.operation_term)
    )
    return matches[0] if len(matches) == 1 else None


def _build_plan(
    document: DocumentRef,
    payload: bytes,
    graph: ParametricFeatureGraphV2,
    request_digest: str,
) -> tuple[PartDesignBooleanBackendPlan, SubjectRef]:
    if graph.graph_id != document.document_id or len(graph.graph_results) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")
    _assert_closed_graph(graph)
    terms = {term.term_ref_id: term for term in graph.terms}
    for expected in BOOLEAN_PFG_TERMS:
        if sum(_identity(term) == _identity(expected) for term in graph.terms) != 1:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/terms")
    candidates = tuple(
        (node, operation)
        for node in graph.nodes
        if (operation := _operation_for_target(node, terms)) is not None
    )
    if len(candidates) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/target")
    target, operation = candidates[0]
    bodies = {body.body_id: body for body in graph.bodies}
    if target.body_id not in bodies:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/body")

    expected_ports = {
        _identity(BOOLEAN_BASE_ROLE_TERM): (
            BOOLEAN_SOLID_TYPE_TERM,
            "base",
            1,
            1,
            False,
        ),
        _identity(BOOLEAN_TOOLS_ROLE_TERM): (
            BOOLEAN_SOLID_TYPE_TERM,
            "tools",
            1,
            1,
            True,
        ),
    }
    if len(target.intent.input_ports) != 2:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/input_ports")
    port_kinds: dict[str, str] = {}
    for port in target.intent.input_ports:
        role = terms.get(port.semantic_role_term_ref_id)
        value_type = terms.get(port.value_type_term_ref_id)
        expected = None if role is None else expected_ports.get(_identity(role))
        if (
            expected is None
            or value_type is None
            or _identity(value_type) != _identity(expected[0])
            or port.minimum_cardinality != expected[2]
            or port.maximum_cardinality != expected[3]
            or port.ordered is not expected[4]
            or expected[1] in port_kinds.values()
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/input_ports")
        port_kinds[port.port_id] = expected[1]
    grouped: dict[str, list[object]] = {"base": [], "tools": []}
    for dependency in target.intent.dependencies:
        kind = port_kinds.get(dependency.port_id)
        if kind is None:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/dependencies")
        grouped[kind].append(dependency)
    if (
        len(grouped["base"]) != 1
        or len(grouped["tools"]) != 1
        or grouped["base"][0].ordinal != 0
        or sorted(item.ordinal for item in grouped["tools"]) != list(range(len(grouped["tools"])))
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/dependencies")

    nodes = {node.node_id: node for node in graph.nodes}
    consumed_nodes = {target.node_id}

    def selection(item: object, *, expected_body: str | None, path: str) -> BooleanOperandSelection:
        node = nodes.get(item.upstream_node_id)
        if node is None or node.node_id in consumed_nodes:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)
        if expected_body is not None and node.body_id != expected_body:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)
        result = _result_with_identity(node, terms, path=path)
        if item.upstream_result_id != result.result_id:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)
        consumed_nodes.add(node.node_id)
        return BooleanOperandSelection(
            body_id=node.body_id,
            node_id=node.node_id,
            result_id=result.result_id,
        )

    base = selection(grouped["base"][0], expected_body=target.body_id, path="/graph/base")
    tools = tuple(
        selection(item, expected_body=None, path="/graph/tools")
        for item in sorted(grouped["tools"], key=lambda value: value.ordinal)
    )
    if (
        any(item.body_id == target.body_id for item in tools)
        or len({item.body_id for item in tools}) != len(tools)
        or set(nodes) != consumed_nodes
        or set(bodies) != {target.body_id, *(item.body_id for item in tools)}
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")

    target_result = _result_with_identity(target, terms, path="/graph/result")
    graph_result = graph.graph_results[0]
    if graph_result.node_id != target.node_id or graph_result.result_id != target_result.result_id:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/graph_results")
    plan = PartDesignBooleanBackendPlan(
        source_artifact_id=document.artifact_id,
        source_graph_id=graph.graph_id,
        source_graph_sha256=graph.graph_sha256,
        source_content_sha256=hashlib.sha256(payload).hexdigest(),
        lowering_request_sha256=request_digest,
        adapter_contract_sha256=FREECAD_PARTDESIGN_BOOLEAN_ADAPTER_DESCRIPTOR.adapter_contract_sha256,
        body_id=target.body_id,
        node_id=target.node_id,
        result_id=target_result.result_id,
        operation=operation.operation,
        base=base,
        tools=tools,
    )
    return plan, SubjectRef(
        artifact_id=document.artifact_id,
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id=target.node_id,
    )


def _plan_document(plan: PartDesignBooleanBackendPlan) -> DocumentRef:
    payload = plan.canonical_bytes
    content_sha256 = hashlib.sha256(payload).hexdigest()
    semantic_digest = hashlib.sha256(
        _PLAN_DOCUMENT_DIGEST_DOMAIN + bytes.fromhex(plan.plan_sha256)
    ).hexdigest()
    return DocumentRef(
        artifact_id=f"artifact_freecad_partdesign_boolean_plan_{content_sha256[:32]}",
        role_term_ref_id=BOOLEAN_PLAN_DOCUMENT_ROLE_TERM.term_ref_id,
        schema_term_ref_id=BOOLEAN_PLAN_SCHEMA_TERM.term_ref_id,
        document_id=f"freecad_partdesign_boolean_plan_{semantic_digest[:32]}",
        document_digest=plan.plan_sha256,
        content_sha256=content_sha256,
        size_bytes=len(payload),
        media_type=PARTDESIGN_BOOLEAN_PLAN_MEDIA_TYPE,
    )


class FreeCADPartDesignBooleanAdapter:
    """Exact PFGv2-to-plan adapter for Fuse, Cut, and Common."""

    __slots__ = ("_sink",)

    def __init__(self, sink: PlanSink) -> None:
        if not isinstance(sink, PlanSink):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/plan_sink")
        self._sink = sink

    @property
    def descriptor(self) -> AdapterDescriptor:
        return FREECAD_PARTDESIGN_BOOLEAN_ADAPTER_DESCRIPTOR

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
        return self.lower_with_receipt(
            request,
            artifacts=artifacts,
            codecs=codecs,
            proof_policy=proof_policy,
        )[0]

    def lower_with_receipt(
        self,
        request: BackendLoweringRequest,
        *,
        artifacts: ArtifactReader,
        codecs: TrustedCodecRegistry,
        proof_policy: TrustedProofPolicy,
    ) -> tuple[BackendLoweringResult, LoweredPartDesignBooleanPlanReceipt]:
        if type(request) is not BackendLoweringRequest or request.adapter != self.descriptor:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/adapter")
        if type(codecs) is not TrustedCodecRegistry:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/codecs")
        identities = {_identity(term) for term in request.terms}
        if any(_identity(term) not in identities for term in BOOLEAN_REQUEST_TERMS):
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
        try:
            correct_documents = (
                _identity(request_terms[intent_document.role_term_ref_id])
                == _identity(BOOLEAN_INTENT_DOCUMENT_ROLE_TERM)
                and _identity(request_terms[intent_document.schema_term_ref_id])
                == _identity(PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM)
                and intent_document.media_type == PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE
                and _identity(request_terms[capability_document.role_term_ref_id])
                == _identity(BOOLEAN_CAPABILITY_DOCUMENT_ROLE_TERM)
                and _identity(request_terms[capability_document.schema_term_ref_id])
                == _identity(BOOLEAN_CAPABILITY_SCHEMA_TERM)
            )
        except KeyError:
            correct_documents = False
        if not correct_documents:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/documents")
        capability_payload = read_verified_document(
            artifacts,
            capability_document,
            maximum_bytes=request.budget.max_input_bytes - intent_document.size_bytes,
        )
        expected_capability = boolean_capability_payload()
        expected_digest = hashlib.sha256(
            _CAPABILITY_DIGEST_DOMAIN + expected_capability
        ).hexdigest()
        if (
            not hmac.compare_digest(capability_payload, expected_capability)
            or capability_document.media_type
            != "application/vnd.vibecad.freecad-partdesign-boolean-capability+json"
            or not hmac.compare_digest(capability_document.document_digest, expected_digest)
            or capability_document.document_id
            != f"freecad_partdesign_boolean_capability_{expected_digest[:32]}"
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
                payload,
                expected_sha256=intent_document.document_digest,
            )
            plan, subject = _build_plan(
                intent_document,
                payload,
                graph,
                request.request_digest,
            )
        except IntentBridgeError:
            raise
        except (ParametricFeatureGraphError, PartDesignBooleanRuleError):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_document")
        except (Exception, SystemExit):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_document")
        if (
            tuple(item.subject for item in report.resolved_subjects) != (subject,)
            or report.inert_subjects
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/proof_bundle/target")
        plan_document = _plan_document(plan)
        plan_payload = plan.canonical_bytes
        if len(plan_payload) > min(
            request.budget.max_output_bytes,
            MAX_PARTDESIGN_BOOLEAN_PLAN_BYTES,
        ):
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/plan_document")
        try:
            decode_partdesign_boolean_backend_plan(
                plan_payload,
                expected_content_sha256=plan_document.content_sha256,
                expected_plan_sha256=plan_document.document_digest,
            )
        except PartDesignBooleanRuleError:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_document")
        result = BackendLoweringResult(
            request_digest=request.request_digest,
            adapter=self.descriptor,
            disposition=BridgeDisposition.COMPLETE,
            plan_document=plan_document,
            supported_subjects=(subject,),
        )
        validate_lowering_result(request, result)
        receipt = LoweredPartDesignBooleanPlanReceipt(
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
        self,
        receipt: LoweredPartDesignBooleanPlanReceipt,
    ) -> tuple[PartDesignBooleanBackendPlan, bytes]:
        if (
            type(receipt) is not LoweredPartDesignBooleanPlanReceipt
            or receipt.adapter != self.descriptor
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/receipt")
        document = receipt.plan_document
        if (
            document.role_term_ref_id != BOOLEAN_PLAN_DOCUMENT_ROLE_TERM.term_ref_id
            or document.schema_term_ref_id != BOOLEAN_PLAN_SCHEMA_TERM.term_ref_id
            or document.media_type != PARTDESIGN_BOOLEAN_PLAN_MEDIA_TYPE
            or document.size_bytes > MAX_PARTDESIGN_BOOLEAN_PLAN_BYTES
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/receipt/plan_document")
        try:
            payload = self._sink.read_exact(document, MAX_PARTDESIGN_BOOLEAN_PLAN_BYTES)
        except (Exception, SystemExit):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink")
        if (
            type(payload) is not bytes
            or len(payload) != document.size_bytes
            or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), document.content_sha256)
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink/readback")
        try:
            plan = decode_partdesign_boolean_backend_plan(
                payload,
                expected_content_sha256=document.content_sha256,
                expected_plan_sha256=document.document_digest,
            )
        except PartDesignBooleanRuleError:
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
                plan.source_graph_sha256,
                receipt.source_document.document_digest,
            )
            or not hmac.compare_digest(
                plan.source_content_sha256,
                receipt.source_document.content_sha256,
            )
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/receipt/source_document")
        return plan, payload


__all__ = [
    "BOOLEAN_BASE_ROLE_TERM",
    "BOOLEAN_CAPABILITY_DOCUMENT_ROLE_TERM",
    "BOOLEAN_CAPABILITY_SCHEMA_TERM",
    "BOOLEAN_FAMILY_TERM",
    "BOOLEAN_INTENT_DOCUMENT_ROLE_TERM",
    "BOOLEAN_OPERATION_TERMS",
    "BOOLEAN_PFG_TERMS",
    "BOOLEAN_PLAN_DOCUMENT_ROLE_TERM",
    "BOOLEAN_PLAN_SCHEMA_TERM",
    "BOOLEAN_REQUEST_TERMS",
    "BOOLEAN_SOLID_RESULT_ROLE_TERM",
    "BOOLEAN_SOLID_TYPE_TERM",
    "BOOLEAN_STRUCTURE_TERM",
    "BOOLEAN_TOOLS_ROLE_TERM",
    "FREECAD_PARTDESIGN_BOOLEAN_ADAPTER_DESCRIPTOR",
    "BooleanOperationTerms",
    "FreeCADPartDesignBooleanAdapter",
    "LoweredPartDesignBooleanPlanReceipt",
    "boolean_capability_payload",
    "build_boolean_capability_document",
]
