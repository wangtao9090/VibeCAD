"""Private exact PFGv2 lowering for reviewed PartDesign dress-ups/transforms.

All six operations share one authority-free plan and one static native rule
catalog.  The adapter accepts only exact canonical PFGv2, complete proof and
capability documents, and complete semantic term identities.  Graph strings
cannot select FreeCAD TypeIds, property names, or native sub-element names.
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
from vibecad.parametric.freecad_partdesign_dressup_transform_rules import (
    MAX_PARTDESIGN_DRESSUP_TRANSFORM_PLAN_BYTES,
    PARTDESIGN_DRESSUP_TRANSFORM_FREECAD_ENGINE_BUILD_ID,
    PARTDESIGN_DRESSUP_TRANSFORM_PLAN_MEDIA_TYPE,
    PARTDESIGN_DRESSUP_TRANSFORM_RULE_CONTRACT_SHA256,
    PARTDESIGN_DRESSUP_TRANSFORM_RULE_ID,
    PartDesignDressupTransformBackendPlan,
    PartDesignDressupTransformOperation,
    PartDesignDressupTransformRuleError,
    SemanticObjectSelection,
    decode_partdesign_dressup_transform_backend_plan,
    operation_parameters_from_value,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.freecad-partdesign"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.freecad-partdesign-ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.freecad-partdesign-dressup-transform-adapter.v1\0"
_CAPABILITY_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-dressup-transform-capability.v1\0"
_PLAN_DOCUMENT_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-dressup-transform-document.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-dressup-transform-lowering-receipt.v1\0"


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


DRESSUP_TRANSFORM_INTENT_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_parametric_intent", "document-role.parametric-intent"
)
DRESSUP_TRANSFORM_CAPABILITY_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_partdesign_dressup_transform_capability",
    "document-role.freecad-partdesign-dressup-transform-capability",
)
DRESSUP_TRANSFORM_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_freecad_partdesign_dressup_transform_capability_v1",
    "document-schema.freecad-partdesign-dressup-transform-capability-v1",
)
DRESSUP_TRANSFORM_PLAN_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_backend_plan", "document-role.freecad-backend-plan"
)
DRESSUP_TRANSFORM_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_freecad_partdesign_dressup_transform_plan_v1",
    "document-schema.freecad-partdesign-dressup-transform-plan-v1",
)

DRESSUP_TRANSFORM_STRUCTURE_TERM: Final = _pfg_term(
    "structure_partdesign_body_feature", "structure.partdesign-body-feature"
)
DRESSUP_TRANSFORM_BASE_ROLE_TERM: Final = _pfg_term(
    "role_base_solid", "input-role.base-solid"
)
DRESSUP_TRANSFORM_PARAMETERS_ROLE_TERM: Final = _pfg_term(
    "role_dressup_transform_parameters", "input-role.dressup-transform-parameters"
)
DRESSUP_TRANSFORM_SOLID_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_solid", "result-role.solid"
)
DRESSUP_TRANSFORM_SOLID_TYPE_TERM: Final = _pfg_term("type_solid", "value-type.solid")
DRESSUP_TRANSFORM_PARAMETERS_TYPE_TERM: Final = _pfg_term(
    "type_dressup_transform_parameter_set", "value-type.dressup-transform-parameter-set"
)
DRESSUP_TRANSFORM_CANONICAL_JSON_TERM: Final = _pfg_term(
    "encoding_canonical_json", "value-encoding.canonical-json"
)

_OPERATION_METADATA: Final = {
    PartDesignDressupTransformOperation.SCALED: (
        "feature-family.scaled-pattern",
        "operation.partdesign-scaled",
    ),
    PartDesignDressupTransformOperation.MULTI_TRANSFORM: (
        "feature-family.multi-transform",
        "operation.partdesign-multi-transform",
    ),
    PartDesignDressupTransformOperation.FILLET: (
        "feature-family.edge-fillet",
        "operation.partdesign-fillet",
    ),
    PartDesignDressupTransformOperation.CHAMFER: (
        "feature-family.edge-chamfer",
        "operation.partdesign-chamfer",
    ),
    PartDesignDressupTransformOperation.DRAFT: (
        "feature-family.face-draft",
        "operation.partdesign-draft",
    ),
    PartDesignDressupTransformOperation.THICKNESS: (
        "feature-family.shell-thickness",
        "operation.partdesign-thickness-skin-arc",
    ),
}


@dataclass(frozen=True, slots=True)
class DressupTransformOperationTerms:
    operation: PartDesignDressupTransformOperation
    family_term: SemanticTermRefV2
    operation_term: SemanticTermRefV2


DRESSUP_TRANSFORM_OPERATION_TERMS: Final = tuple(
    DressupTransformOperationTerms(
        operation=operation,
        family_term=_pfg_term(
            f"family_{operation.value}", _OPERATION_METADATA[operation][0]
        ),
        operation_term=_pfg_term(
            f"operation_{operation.value}", _OPERATION_METADATA[operation][1]
        ),
    )
    for operation in PartDesignDressupTransformOperation
)

DRESSUP_TRANSFORM_PFG_TERMS: Final = (
    DRESSUP_TRANSFORM_STRUCTURE_TERM,
    DRESSUP_TRANSFORM_BASE_ROLE_TERM,
    DRESSUP_TRANSFORM_PARAMETERS_ROLE_TERM,
    DRESSUP_TRANSFORM_SOLID_RESULT_ROLE_TERM,
    DRESSUP_TRANSFORM_SOLID_TYPE_TERM,
    DRESSUP_TRANSFORM_PARAMETERS_TYPE_TERM,
    DRESSUP_TRANSFORM_CANONICAL_JSON_TERM,
    *(item.family_term for item in DRESSUP_TRANSFORM_OPERATION_TERMS),
    *(item.operation_term for item in DRESSUP_TRANSFORM_OPERATION_TERMS),
)


def _as_bridge(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


DRESSUP_TRANSFORM_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    DRESSUP_TRANSFORM_INTENT_DOCUMENT_ROLE_TERM,
    DRESSUP_TRANSFORM_CAPABILITY_DOCUMENT_ROLE_TERM,
    DRESSUP_TRANSFORM_CAPABILITY_SCHEMA_TERM,
    DRESSUP_TRANSFORM_PLAN_DOCUMENT_ROLE_TERM,
    DRESSUP_TRANSFORM_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in DRESSUP_TRANSFORM_PFG_TERMS),
)

_ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            PARTDESIGN_DRESSUP_TRANSFORM_RULE_ID.encode("ascii"),
            PARTDESIGN_DRESSUP_TRANSFORM_RULE_CONTRACT_SHA256.encode("ascii"),
            b"canonical-pfg-v2;exact-proof;full-static-terms;atomic-plan-sink;no-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in (*DRESSUP_TRANSFORM_REQUEST_TERMS, PFG_SELECTOR_FEATURE_NODE)
            ),
        )
    )
).hexdigest()

FREECAD_PARTDESIGN_DRESSUP_TRANSFORM_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_partdesign_dressup_transform_adapter",
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


def dressup_transform_capability_payload() -> bytes:
    return _canonical_json(
        {
            "schema_version": 1,
            "authority": "none",
            "adapter": FREECAD_PARTDESIGN_DRESSUP_TRANSFORM_ADAPTER_DESCRIPTOR.to_mapping(),
            "backend": {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PARTDESIGN_DRESSUP_TRANSFORM_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": PARTDESIGN_DRESSUP_TRANSFORM_RULE_ID,
                "rule_contract_sha256": PARTDESIGN_DRESSUP_TRANSFORM_RULE_CONTRACT_SHA256,
                "operations": [
                    item.operation.value for item in DRESSUP_TRANSFORM_OPERATION_TERMS
                ],
                "semantic_subelement_resolution": "unique-live-axis-aligned-role",
            },
        }
    )


def build_dressup_transform_capability_document(
    *, artifact_id: str = "artifact_freecad_partdesign_dressup_transform_capability"
) -> tuple[DocumentRef, bytes]:
    payload = dressup_transform_capability_payload()
    digest = hashlib.sha256(_CAPABILITY_DIGEST_DOMAIN + payload).hexdigest()
    return (
        DocumentRef(
            artifact_id=artifact_id,
            role_term_ref_id=DRESSUP_TRANSFORM_CAPABILITY_DOCUMENT_ROLE_TERM.term_ref_id,
            schema_term_ref_id=DRESSUP_TRANSFORM_CAPABILITY_SCHEMA_TERM.term_ref_id,
            document_id=f"freecad_partdesign_dressup_transform_capability_{digest[:32]}",
            document_digest=digest,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type=(
                "application/vnd.vibecad.freecad-partdesign-dressup-transform-capability+json"
            ),
        ),
        payload,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class LoweredPartDesignDressupTransformPlanReceipt:
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
        object.__setattr__(
            self, "receipt_id", f"partdesign_dressup_transform_lowering_{digest[:32]}"
        )

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


def _graph_term(
    terms: dict[str, SemanticTermRefV2],
    term_ref_id: str,
    expected: SemanticTermRefV2,
    path: str,
) -> None:
    actual = terms.get(term_ref_id)
    if actual is None or _identity(actual) != _identity(expected):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)


def _result_with_identity(
    node: FeatureNodeV2,
    terms: dict[str, SemanticTermRefV2],
    *,
    role: SemanticTermRefV2,
    value_type: SemanticTermRefV2,
    path: str,
):
    matches = tuple(
        result
        for result in node.results
        if terms.get(result.semantic_role_term_ref_id) is not None
        and terms.get(result.value_type_term_ref_id) is not None
        and _identity(terms[result.semantic_role_term_ref_id]) == _identity(role)
        and _identity(terms[result.value_type_term_ref_id]) == _identity(value_type)
    )
    if len(matches) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)
    return matches[0]


def _assert_no_extensions(graph: ParametricFeatureGraphV2) -> None:
    if graph.extensions or any(item.extension_ids for item in graph.bodies):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/extensions")
    if any(
        item.extension_ids or item.value.extension_ids or item.expression is not None
        for item in graph.parameters
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/parameters")
    if any(
        item.extension_ids or item.occurrence_path or item.qualifier_term_ref_ids
        for item in graph.references
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/references")
    if any(
        node.extension_ids
        or node.intent.extension_ids
        or any(port.extension_ids for port in node.intent.input_ports)
        or any(result.extension_ids for result in node.results)
        for node in graph.nodes
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/nodes")


def _operation_for_target(
    target: FeatureNodeV2,
    terms: dict[str, SemanticTermRefV2],
) -> DressupTransformOperationTerms | None:
    structural = terms.get(target.intent.structural_kind_term_ref_id)
    family = terms.get(target.intent.family_term_ref_id)
    operation = terms.get(target.intent.operation_term_ref_id)
    if structural is None or _identity(structural) != _identity(DRESSUP_TRANSFORM_STRUCTURE_TERM):
        return None
    matches = tuple(
        item
        for item in DRESSUP_TRANSFORM_OPERATION_TERMS
        if family is not None
        and operation is not None
        and _identity(family) == _identity(item.family_term)
        and _identity(operation) == _identity(item.operation_term)
    )
    return matches[0] if len(matches) == 1 else None


def _build_plan(
    document: DocumentRef,
    payload: bytes,
    graph: ParametricFeatureGraphV2,
    request_digest: str,
) -> tuple[PartDesignDressupTransformBackendPlan, SubjectRef]:
    if (
        graph.graph_id != document.document_id
        or len(graph.bodies) != 1
        or len(graph.graph_results) != 1
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")
    _assert_no_extensions(graph)
    terms = {term.term_ref_id: term for term in graph.terms}
    for expected in DRESSUP_TRANSFORM_PFG_TERMS:
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
    body = graph.bodies[0]
    if target.body_id != body.body_id:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/body")

    expected_ports = {
        _identity(DRESSUP_TRANSFORM_BASE_ROLE_TERM): (
            DRESSUP_TRANSFORM_SOLID_TYPE_TERM,
            "base",
        ),
        _identity(DRESSUP_TRANSFORM_PARAMETERS_ROLE_TERM): (
            DRESSUP_TRANSFORM_PARAMETERS_TYPE_TERM,
            "parameters",
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
            or port.minimum_cardinality != 1
            or port.maximum_cardinality != 1
            or port.ordered
            or expected[1] in port_kinds.values()
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/input_ports")
        port_kinds[port.port_id] = expected[1]
    dependencies = target.intent.dependencies
    parameter_bindings = target.intent.parameter_bindings
    if (
        target.intent.references
        or len(dependencies) != 1
        or len(parameter_bindings) != 1
        or port_kinds.get(dependencies[0].port_id) != "base"
        or dependencies[0].ordinal != 0
        or port_kinds.get(parameter_bindings[0].port_id) != "parameters"
        or parameter_bindings[0].ordinal != 0
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/bindings")

    nodes = {node.node_id: node for node in graph.nodes}
    dependency = dependencies[0]
    base_node = nodes.get(dependency.upstream_node_id)
    if base_node is None or base_node.body_id != body.body_id or base_node is target:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/base")
    base_result = _result_with_identity(
        base_node,
        terms,
        role=DRESSUP_TRANSFORM_SOLID_RESULT_ROLE_TERM,
        value_type=DRESSUP_TRANSFORM_SOLID_TYPE_TERM,
        path="/graph/base",
    )
    if dependency.upstream_result_id != base_result.result_id or len(base_node.results) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/base")
    if set(nodes) != {target.node_id, base_node.node_id}:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")
    base = SemanticObjectSelection(node_id=base_node.node_id, result_id=base_result.result_id)

    if graph.references or len(graph.parameters) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/parameters")
    parameter = graph.parameters[0]
    binding = parameter_bindings[0]
    if binding.parameter_id != parameter.parameter_id or parameter.expression is not None:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/parameters")
    _graph_term(
        terms,
        parameter.semantic_role_term_ref_id,
        DRESSUP_TRANSFORM_PARAMETERS_ROLE_TERM,
        "/graph/parameters/role",
    )
    _graph_term(
        terms,
        parameter.value.value_type_term_ref_id,
        DRESSUP_TRANSFORM_PARAMETERS_TYPE_TERM,
        "/graph/parameters/type",
    )
    _graph_term(
        terms,
        parameter.value.encoding_term_ref_id,
        DRESSUP_TRANSFORM_CANONICAL_JSON_TERM,
        "/graph/parameters/encoding",
    )
    try:
        parameters = operation_parameters_from_value(
            operation.operation, parameter.value.value
        )
    except PartDesignDressupTransformRuleError:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/parameters/value")

    target_result = _result_with_identity(
        target,
        terms,
        role=DRESSUP_TRANSFORM_SOLID_RESULT_ROLE_TERM,
        value_type=DRESSUP_TRANSFORM_SOLID_TYPE_TERM,
        path="/graph/result",
    )
    graph_selection = graph.graph_results[0]
    if (
        len(target.results) != 1
        or graph_selection.node_id != target.node_id
        or graph_selection.result_id != target_result.result_id
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/graph_results")
    plan = PartDesignDressupTransformBackendPlan(
        source_artifact_id=document.artifact_id,
        source_graph_id=graph.graph_id,
        source_graph_sha256=graph.graph_sha256,
        source_content_sha256=hashlib.sha256(payload).hexdigest(),
        lowering_request_sha256=request_digest,
        adapter_contract_sha256=(
            FREECAD_PARTDESIGN_DRESSUP_TRANSFORM_ADAPTER_DESCRIPTOR.adapter_contract_sha256
        ),
        body_id=body.body_id,
        node_id=target.node_id,
        result_id=target_result.result_id,
        operation=operation.operation,
        base=base,
        parameter_id=parameter.parameter_id,
        value_id=parameter.value.value_id,
        parameters=parameters,
    )
    return plan, SubjectRef(
        artifact_id=document.artifact_id,
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id=target.node_id,
    )


def _plan_document(plan: PartDesignDressupTransformBackendPlan) -> DocumentRef:
    payload = plan.canonical_bytes
    content_sha256 = hashlib.sha256(payload).hexdigest()
    semantic_digest = hashlib.sha256(
        _PLAN_DOCUMENT_DIGEST_DOMAIN + bytes.fromhex(plan.plan_sha256)
    ).hexdigest()
    return DocumentRef(
        artifact_id=f"artifact_freecad_partdesign_dressup_transform_plan_{content_sha256[:32]}",
        role_term_ref_id=DRESSUP_TRANSFORM_PLAN_DOCUMENT_ROLE_TERM.term_ref_id,
        schema_term_ref_id=DRESSUP_TRANSFORM_PLAN_SCHEMA_TERM.term_ref_id,
        document_id=f"freecad_partdesign_dressup_transform_plan_{semantic_digest[:32]}",
        document_digest=plan.plan_sha256,
        content_sha256=content_sha256,
        size_bytes=len(payload),
        media_type=PARTDESIGN_DRESSUP_TRANSFORM_PLAN_MEDIA_TYPE,
    )


class FreeCADPartDesignDressupTransformAdapter:
    """One exact PFGv2-to-plan adapter for all six reviewed operations."""

    __slots__ = ("_sink",)

    def __init__(self, sink: PlanSink) -> None:
        if not isinstance(sink, PlanSink):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/plan_sink")
        self._sink = sink

    @property
    def descriptor(self) -> AdapterDescriptor:
        return FREECAD_PARTDESIGN_DRESSUP_TRANSFORM_ADAPTER_DESCRIPTOR

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
            request, artifacts=artifacts, codecs=codecs, proof_policy=proof_policy
        )[0]

    def lower_with_receipt(
        self,
        request: BackendLoweringRequest,
        *,
        artifacts: ArtifactReader,
        codecs: TrustedCodecRegistry,
        proof_policy: TrustedProofPolicy,
    ) -> tuple[BackendLoweringResult, LoweredPartDesignDressupTransformPlanReceipt]:
        if type(request) is not BackendLoweringRequest or request.adapter != self.descriptor:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/adapter")
        if type(codecs) is not TrustedCodecRegistry:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/codecs")
        identities = {_identity(term) for term in request.terms}
        if any(_identity(term) not in identities for term in DRESSUP_TRANSFORM_REQUEST_TERMS):
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
        try:
            intent_document = documents[request.intent_artifact_ids[0]]
            capability_document = documents[request.capability_artifact_ids[0]]
            request_terms = {term.term_ref_id: term for term in request.terms}
            document_identities_valid = (
                _identity(request_terms[intent_document.role_term_ref_id])
                == _identity(DRESSUP_TRANSFORM_INTENT_DOCUMENT_ROLE_TERM)
                and _identity(request_terms[intent_document.schema_term_ref_id])
                == _identity(PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM)
                and intent_document.media_type == PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE
                and _identity(request_terms[capability_document.role_term_ref_id])
                == _identity(DRESSUP_TRANSFORM_CAPABILITY_DOCUMENT_ROLE_TERM)
                and _identity(request_terms[capability_document.schema_term_ref_id])
                == _identity(DRESSUP_TRANSFORM_CAPABILITY_SCHEMA_TERM)
            )
        except (KeyError, TypeError, AttributeError):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/documents")
        if not document_identities_valid:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/documents")
        capability_payload = read_verified_document(
            artifacts,
            capability_document,
            maximum_bytes=request.budget.max_input_bytes - intent_document.size_bytes,
        )
        expected_capability = dressup_transform_capability_payload()
        expected_digest = hashlib.sha256(
            _CAPABILITY_DIGEST_DOMAIN + expected_capability
        ).hexdigest()
        if (
            not hmac.compare_digest(capability_payload, expected_capability)
            or capability_document.media_type
            != "application/vnd.vibecad.freecad-partdesign-dressup-transform-capability+json"
            or not hmac.compare_digest(capability_document.document_digest, expected_digest)
            or capability_document.document_id
            != f"freecad_partdesign_dressup_transform_capability_{expected_digest[:32]}"
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
            plan, subject = _build_plan(intent_document, payload, graph, request.request_digest)
        except IntentBridgeError:
            raise
        except (ParametricFeatureGraphError, PartDesignDressupTransformRuleError):
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
            MAX_PARTDESIGN_DRESSUP_TRANSFORM_PLAN_BYTES,
        ):
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/plan_document")
        try:
            decode_partdesign_dressup_transform_backend_plan(
                plan_payload,
                expected_content_sha256=plan_document.content_sha256,
                expected_plan_sha256=plan_document.document_digest,
            )
        except PartDesignDressupTransformRuleError:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_document")
        result = BackendLoweringResult(
            request_digest=request.request_digest,
            adapter=self.descriptor,
            disposition=BridgeDisposition.COMPLETE,
            plan_document=plan_document,
            supported_subjects=(subject,),
        )
        validate_lowering_result(request, result)
        receipt = LoweredPartDesignDressupTransformPlanReceipt(
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
        self, receipt: LoweredPartDesignDressupTransformPlanReceipt
    ) -> tuple[PartDesignDressupTransformBackendPlan, bytes]:
        if (
            type(receipt) is not LoweredPartDesignDressupTransformPlanReceipt
            or receipt.adapter != self.descriptor
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/receipt")
        document = receipt.plan_document
        if (
            document.role_term_ref_id != DRESSUP_TRANSFORM_PLAN_DOCUMENT_ROLE_TERM.term_ref_id
            or document.schema_term_ref_id != DRESSUP_TRANSFORM_PLAN_SCHEMA_TERM.term_ref_id
            or document.media_type != PARTDESIGN_DRESSUP_TRANSFORM_PLAN_MEDIA_TYPE
            or document.size_bytes > MAX_PARTDESIGN_DRESSUP_TRANSFORM_PLAN_BYTES
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/receipt/plan_document")
        try:
            payload = self._sink.read_exact(
                document, MAX_PARTDESIGN_DRESSUP_TRANSFORM_PLAN_BYTES
            )
        except (Exception, SystemExit):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink")
        if (
            type(payload) is not bytes
            or len(payload) != document.size_bytes
            or not hmac.compare_digest(
                hashlib.sha256(payload).hexdigest(), document.content_sha256
            )
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink/readback")
        try:
            plan = decode_partdesign_dressup_transform_backend_plan(
                payload,
                expected_content_sha256=document.content_sha256,
                expected_plan_sha256=document.document_digest,
            )
        except PartDesignDressupTransformRuleError:
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
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/receipt/binding")
        return plan, payload
