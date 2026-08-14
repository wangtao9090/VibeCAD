"""Private PFGv2 lowering for one reviewed FreeCAD PartDesign Groove rule.

This module deliberately does not extend the public bridge wire.  A host injects
an atomic, content-addressed :class:`PlanSink`; lowering publishes canonical plan
bytes, reads them back exactly, and only then returns the existing
``BackendLoweringResult.plan_document`` reference.  Plans and receipts remain
authority-free.  Native execution is a separate explicit trusted-host action.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass, field
from typing import Final, Protocol, runtime_checkable

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
    SemanticReferenceScope,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_partdesign_sketch_rules import (
    GROOVE_FREECAD_ENGINE_BUILD_ID,
    GROOVE_PLAN_MEDIA_TYPE,
    GROOVE_RULE_CONTRACT_SHA256,
    GROOVE_RULE_ID,
    MAX_GROOVE_PLAN_BYTES,
    GrooveBackendPlan,
    GrooveRuleError,
    decode_groove_backend_plan,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.freecad-partdesign"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.freecad-partdesign-ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.freecad-parametric-adapter.v1\0"
_CAPABILITY_DIGEST_DOMAIN = b"vibecad.freecad-groove-capability.v1\0"
_PLAN_DOCUMENT_DIGEST_DOMAIN = b"vibecad.freecad-groove-plan-document.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-groove-lowering-receipt.v1\0"


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


GROOVE_INTENT_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_parametric_intent", "document-role.parametric-intent"
)
GROOVE_CAPABILITY_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_groove_capability", "document-role.freecad-groove-capability"
)
GROOVE_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_freecad_groove_capability_v1", "document-schema.freecad-groove-capability-v1"
)
GROOVE_PLAN_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_backend_plan", "document-role.freecad-backend-plan"
)
GROOVE_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_freecad_groove_plan_v1", "document-schema.freecad-groove-plan-v1"
)

GROOVE_STRUCTURE_TERM: Final = _pfg_term(
    "structure_partdesign_body_feature", "structure.partdesign-body-feature"
)
GROOVE_FAMILY_TERM: Final = _pfg_term(
    "family_subtractive_revolution", "feature-family.subtractive-revolution"
)
GROOVE_OPERATION_TERM: Final = _pfg_term(
    "operation_groove_angle", "operation.partdesign-groove-angle"
)
GROOVE_BASE_ROLE_TERM: Final = _pfg_term("role_base_solid", "input-role.base-solid")
GROOVE_PROFILE_ROLE_TERM: Final = _pfg_term(
    "role_closed_profile", "input-role.closed-profile"
)
GROOVE_AXIS_ROLE_TERM: Final = _pfg_term("role_revolution_axis", "input-role.revolution-axis")
GROOVE_ANGLE_ROLE_TERM: Final = _pfg_term("role_angle_degrees", "input-role.angle-degrees")
GROOVE_REVERSED_ROLE_TERM: Final = _pfg_term("role_reversed", "input-role.reversed")
GROOVE_SOLID_RESULT_ROLE_TERM: Final = _pfg_term("role_result_solid", "result-role.solid")
GROOVE_PROFILE_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_closed_profile", "result-role.closed-profile"
)
GROOVE_AXIS_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_sketch_axis", "result-role.sketch-axis"
)
GROOVE_SOLID_TYPE_TERM: Final = _pfg_term("type_solid", "value-type.solid")
GROOVE_CLOSED_PROFILE_TYPE_TERM: Final = _pfg_term(
    "type_closed_profile", "value-type.closed-profile"
)
GROOVE_SKETCH_AXIS_TYPE_TERM: Final = _pfg_term("type_sketch_axis", "value-type.sketch-axis")
GROOVE_ANGLE_TYPE_TERM: Final = _pfg_term("type_angle_degrees", "value-type.angle-degrees")
GROOVE_BOOLEAN_TYPE_TERM: Final = _pfg_term("type_boolean", "value-type.boolean")
GROOVE_CANONICAL_JSON_TERM: Final = _pfg_term(
    "encoding_canonical_json", "value-encoding.canonical-json"
)
GROOVE_SKETCH_V_AXIS_LOCATOR_TERM: Final = _pfg_term(
    "locator_sketch_v_axis", "reference-locator.sketch-v-axis"
)

GROOVE_PFG_TERMS: Final = (
    GROOVE_STRUCTURE_TERM,
    GROOVE_FAMILY_TERM,
    GROOVE_OPERATION_TERM,
    GROOVE_BASE_ROLE_TERM,
    GROOVE_PROFILE_ROLE_TERM,
    GROOVE_AXIS_ROLE_TERM,
    GROOVE_ANGLE_ROLE_TERM,
    GROOVE_REVERSED_ROLE_TERM,
    GROOVE_SOLID_RESULT_ROLE_TERM,
    GROOVE_PROFILE_RESULT_ROLE_TERM,
    GROOVE_AXIS_RESULT_ROLE_TERM,
    GROOVE_SOLID_TYPE_TERM,
    GROOVE_CLOSED_PROFILE_TYPE_TERM,
    GROOVE_SKETCH_AXIS_TYPE_TERM,
    GROOVE_ANGLE_TYPE_TERM,
    GROOVE_BOOLEAN_TYPE_TERM,
    GROOVE_CANONICAL_JSON_TERM,
    GROOVE_SKETCH_V_AXIS_LOCATOR_TERM,
)


def _as_bridge(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


GROOVE_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    GROOVE_INTENT_DOCUMENT_ROLE_TERM,
    GROOVE_CAPABILITY_DOCUMENT_ROLE_TERM,
    GROOVE_CAPABILITY_SCHEMA_TERM,
    GROOVE_PLAN_DOCUMENT_ROLE_TERM,
    GROOVE_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in GROOVE_PFG_TERMS),
)

_ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            GROOVE_RULE_ID.encode("ascii"),
            GROOVE_RULE_CONTRACT_SHA256.encode("ascii"),
            b"canonical-pfg-v2;exact-proof;static-terms;atomic-plan-sink;no-execution-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in (
                    *GROOVE_REQUEST_TERMS,
                    PFG_SELECTOR_FEATURE_NODE,
                )
            ),
        )
    )
).hexdigest()

FREECAD_GROOVE_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_parametric_groove_adapter",
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
    except (TypeError, ValueError, UnicodeError):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")


def _capability_mapping() -> dict[str, object]:
    return {
        "schema_version": 1,
        "authority": "none",
        "adapter": FREECAD_GROOVE_ADAPTER_DESCRIPTOR.to_mapping(),
        "backend": {
            "engine": "FreeCAD",
            "engine_version": "1.1.0",
            "engine_build_id": GROOVE_FREECAD_ENGINE_BUILD_ID,
        },
        "rule": {
            "rule_id": GROOVE_RULE_ID,
            "rule_contract_sha256": GROOVE_RULE_CONTRACT_SHA256,
            "operation": "PartDesign::Groove/Angle",
        },
    }


def groove_capability_payload() -> bytes:
    return _canonical_json(_capability_mapping())


def build_groove_capability_document(
    *, artifact_id: str = "artifact_freecad_groove_capability"
) -> tuple[DocumentRef, bytes]:
    payload = groove_capability_payload()
    document_digest = hashlib.sha256(_CAPABILITY_DIGEST_DOMAIN + payload).hexdigest()
    return (
        DocumentRef(
            artifact_id=artifact_id,
            role_term_ref_id=GROOVE_CAPABILITY_DOCUMENT_ROLE_TERM.term_ref_id,
            schema_term_ref_id=GROOVE_CAPABILITY_SCHEMA_TERM.term_ref_id,
            document_id=f"freecad_groove_capability_{document_digest[:32]}",
            document_digest=document_digest,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type="application/vnd.vibecad.freecad-groove-capability+json",
        ),
        payload,
    )


@runtime_checkable
class PlanSink(Protocol):
    """Host-owned atomic content-addressed plan publication boundary.

    ``publish_exact`` must be idempotent for identical bytes, reject a digest
    collision, atomically make the complete payload visible, and leave no new
    publication on failure *or a non-exact return*.  Its return is the exact
    post-publication readback.  Neither the sink nor anything stored in it grants
    execution authority.
    """

    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        """Atomically publish then return exact immutable bytes."""

    def read_exact(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        """Read immutable bytes without executing or adopting the plan."""


@dataclass(frozen=True, slots=True, kw_only=True)
class LoweredGroovePlanReceipt:
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
        object.__setattr__(self, "receipt_id", f"groove_lowering_{digest[:32]}")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _identity(term: object) -> tuple[str, str, str, str]:
    try:
        identity = (
            term.namespace,
            term.vocabulary_version,
            term.term_id,
            term.term_definition_sha256,
        )
    except Exception:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/terms")
    if type(identity) is not tuple or len(identity) != 4:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/terms")
    return identity


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
    matches = []
    for result in node.results:
        actual_role = terms.get(result.semantic_role_term_ref_id)
        actual_type = terms.get(result.value_type_term_ref_id)
        if (
            actual_role is not None
            and actual_type is not None
            and _identity(actual_role) == _identity(role)
            and _identity(actual_type) == _identity(value_type)
        ):
            matches.append(result)
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


def _build_plan(
    document: DocumentRef,
    payload: bytes,
    graph: ParametricFeatureGraphV2,
    request_digest: str,
) -> tuple[GrooveBackendPlan, SubjectRef]:
    if len(graph.bodies) != 1 or len(graph.nodes) != 3 or len(graph.graph_results) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")
    _assert_no_extensions(graph)
    terms = {term.term_ref_id: term for term in graph.terms}
    for expected in GROOVE_PFG_TERMS:
        matches = tuple(term for term in graph.terms if _identity(term) == _identity(expected))
        if len(matches) != 1:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/terms")

    candidates = []
    for node in graph.nodes:
        intent = node.intent
        expected = (
            (intent.structural_kind_term_ref_id, GROOVE_STRUCTURE_TERM),
            (intent.family_term_ref_id, GROOVE_FAMILY_TERM),
            (intent.operation_term_ref_id, GROOVE_OPERATION_TERM),
        )
        if all(
            terms.get(term_ref_id) is not None
            and _identity(terms[term_ref_id]) == _identity(term)
            for term_ref_id, term in expected
        ):
            candidates.append(node)
    if len(candidates) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/groove_node")
    target = candidates[0]
    body = graph.bodies[0]
    if target.body_id != body.body_id:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/body")

    intent = target.intent
    expected_ports = {
        _identity(GROOVE_BASE_ROLE_TERM): (GROOVE_SOLID_TYPE_TERM, "base"),
        _identity(GROOVE_PROFILE_ROLE_TERM): (GROOVE_CLOSED_PROFILE_TYPE_TERM, "profile"),
        _identity(GROOVE_AXIS_ROLE_TERM): (GROOVE_SKETCH_AXIS_TYPE_TERM, "axis"),
        _identity(GROOVE_ANGLE_ROLE_TERM): (GROOVE_ANGLE_TYPE_TERM, "angle"),
        _identity(GROOVE_REVERSED_ROLE_TERM): (GROOVE_BOOLEAN_TYPE_TERM, "reversed"),
    }
    if len(intent.input_ports) != len(expected_ports):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/input_ports")
    port_kinds: dict[str, str] = {}
    for port in intent.input_ports:
        role_term = terms.get(port.semantic_role_term_ref_id)
        type_term = terms.get(port.value_type_term_ref_id)
        match = None if role_term is None else expected_ports.get(_identity(role_term))
        if (
            match is None
            or type_term is None
            or _identity(type_term) != _identity(match[0])
            or port.minimum_cardinality != 1
            or port.maximum_cardinality != 1
            or port.ordered
            or match[1] in port_kinds.values()
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/input_ports")
        port_kinds[port.port_id] = match[1]

    if (
        len(intent.dependencies) != 2
        or len(intent.references) != 1
        or len(intent.parameter_bindings) != 2
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/bindings")
    bindings: dict[str, object] = {}
    for item in (*intent.dependencies, *intent.references, *intent.parameter_bindings):
        kind = port_kinds.get(item.port_id)
        if kind is None or item.ordinal != 0 or kind in bindings:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/bindings")
        bindings[kind] = item
    if set(bindings) != {"base", "profile", "axis", "angle", "reversed"}:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/bindings")

    node_by_id = {node.node_id: node for node in graph.nodes}
    base_binding = bindings["base"]
    profile_binding = bindings["profile"]
    base_node = node_by_id.get(base_binding.upstream_node_id)  # type: ignore[attr-defined]
    profile_node = node_by_id.get(profile_binding.upstream_node_id)  # type: ignore[attr-defined]
    if (
        base_node is None
        or profile_node is None
        or base_node is target
        or profile_node is target
        or base_node is profile_node
        or base_node.body_id != body.body_id
        or profile_node.body_id != body.body_id
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/dependencies")
    base_result = _result_with_identity(
        base_node,
        terms,
        role=GROOVE_SOLID_RESULT_ROLE_TERM,
        value_type=GROOVE_SOLID_TYPE_TERM,
        path="/graph/base_result",
    )
    profile_result = _result_with_identity(
        profile_node,
        terms,
        role=GROOVE_PROFILE_RESULT_ROLE_TERM,
        value_type=GROOVE_CLOSED_PROFILE_TYPE_TERM,
        path="/graph/profile_result",
    )
    axis_result = _result_with_identity(
        profile_node,
        terms,
        role=GROOVE_AXIS_RESULT_ROLE_TERM,
        value_type=GROOVE_SKETCH_AXIS_TYPE_TERM,
        path="/graph/axis_result",
    )
    if (
        base_binding.upstream_result_id != base_result.result_id  # type: ignore[attr-defined]
        or profile_binding.upstream_result_id != profile_result.result_id  # type: ignore[attr-defined]
        or len(base_node.results) != 1
        or len(profile_node.results) != 2
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/dependencies")

    reference_by_id = {reference.reference_id: reference for reference in graph.references}
    axis_binding = bindings["axis"]
    axis = reference_by_id.get(axis_binding.reference_id)  # type: ignore[attr-defined]
    if axis is None:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/axis")
    for term_ref_id, expected, path in (
        (axis.semantic_role_term_ref_id, GROOVE_AXIS_ROLE_TERM, "/graph/axis/role"),
        (axis.value_type_term_ref_id, GROOVE_SKETCH_AXIS_TYPE_TERM, "/graph/axis/type"),
        (axis.locator_term_ref_id, GROOVE_SKETCH_V_AXIS_LOCATOR_TERM, "/graph/axis/locator"),
    ):
        _graph_term(terms, term_ref_id, expected, path)
    if (
        axis.scope is not SemanticReferenceScope.FEATURE
        or axis.source_node_id != profile_node.node_id
        or axis.source_geometry_id != axis_result.result_id
        or axis.source_content_sha256 is not None
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/axis/source")

    parameter_by_id = {parameter.parameter_id: parameter for parameter in graph.parameters}
    values: dict[str, object] = {}
    for kind, expected_role, expected_type in (
        ("angle", GROOVE_ANGLE_ROLE_TERM, GROOVE_ANGLE_TYPE_TERM),
        ("reversed", GROOVE_REVERSED_ROLE_TERM, GROOVE_BOOLEAN_TYPE_TERM),
    ):
        binding = bindings[kind]
        parameter = parameter_by_id.get(binding.parameter_id)  # type: ignore[attr-defined]
        if parameter is None or parameter.expression is not None:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, f"/graph/{kind}")
        _graph_term(
            terms,
            parameter.semantic_role_term_ref_id,
            expected_role,
            f"/graph/{kind}/role",
        )
        _graph_term(
            terms,
            parameter.value.value_type_term_ref_id,
            expected_type,
            f"/graph/{kind}/type",
        )
        _graph_term(
            terms,
            parameter.value.encoding_term_ref_id,
            GROOVE_CANONICAL_JSON_TERM,
            f"/graph/{kind}/encoding",
        )
        values[kind] = parameter.value.value
    if len(graph.parameters) != 2 or len(graph.references) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/values")
    angle = values["angle"]
    if (
        type(angle) not in {int, float}
        or not math.isfinite(angle)
        or not 0.0 < float(angle) <= 360.0
        or type(values["reversed"]) is not bool
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/values")

    target_result = _result_with_identity(
        target,
        terms,
        role=GROOVE_SOLID_RESULT_ROLE_TERM,
        value_type=GROOVE_SOLID_TYPE_TERM,
        path="/graph/result",
    )
    selection = graph.graph_results[0]
    if (
        len(target.results) != 1
        or selection.node_id != target.node_id
        or selection.result_id != target_result.result_id
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/graph_results")

    plan = GrooveBackendPlan(
        source_artifact_id=document.artifact_id,
        source_graph_id=graph.graph_id,
        source_graph_sha256=graph.graph_sha256,
        source_content_sha256=hashlib.sha256(payload).hexdigest(),
        lowering_request_sha256=request_digest,
        adapter_contract_sha256=FREECAD_GROOVE_ADAPTER_DESCRIPTOR.adapter_contract_sha256,
        body_id=body.body_id,
        node_id=target.node_id,
        result_id=target_result.result_id,
        base_node_id=base_node.node_id,
        base_result_id=base_result.result_id,
        profile_node_id=profile_node.node_id,
        profile_result_id=profile_result.result_id,
        axis_reference_id=axis.reference_id,
        axis_result_id=axis_result.result_id,
        angle_degrees=float(angle),
        reversed=values["reversed"],
    )
    return plan, SubjectRef(
        artifact_id=document.artifact_id,
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id=target.node_id,
    )


def _plan_document(plan: GrooveBackendPlan) -> DocumentRef:
    payload = plan.canonical_bytes
    content_sha256 = hashlib.sha256(payload).hexdigest()
    semantic_digest = hashlib.sha256(
        _PLAN_DOCUMENT_DIGEST_DOMAIN + bytes.fromhex(plan.plan_sha256)
    ).hexdigest()
    return DocumentRef(
        artifact_id=f"artifact_freecad_groove_plan_{content_sha256[:32]}",
        role_term_ref_id=GROOVE_PLAN_DOCUMENT_ROLE_TERM.term_ref_id,
        schema_term_ref_id=GROOVE_PLAN_SCHEMA_TERM.term_ref_id,
        document_id=f"freecad_groove_plan_{semantic_digest[:32]}",
        document_digest=plan.plan_sha256,
        content_sha256=content_sha256,
        size_bytes=len(payload),
        media_type=GROOVE_PLAN_MEDIA_TYPE,
    )


class FreeCADParametricGrooveAdapter:
    """Exact PFGv2-to-plan adapter for the first Groove package."""

    __slots__ = ("_sink",)

    def __init__(self, sink: PlanSink) -> None:
        if not isinstance(sink, PlanSink):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/plan_sink")
        self._sink = sink

    @property
    def descriptor(self) -> AdapterDescriptor:
        return FREECAD_GROOVE_ADAPTER_DESCRIPTOR

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
    ) -> tuple[BackendLoweringResult, LoweredGroovePlanReceipt]:
        if type(request) is not BackendLoweringRequest or request.adapter != self.descriptor:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/adapter")
        if type(codecs) is not TrustedCodecRegistry:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/codecs")
        term_identities = {_identity(term) for term in request.terms}
        if any(_identity(term) not in term_identities for term in GROOVE_REQUEST_TERMS):
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
        document_by_id = {document.artifact_id: document for document in request.documents}
        intent_document = document_by_id[request.intent_artifact_ids[0]]
        capability_document = document_by_id[request.capability_artifact_ids[0]]
        request_terms = {term.term_ref_id: term for term in request.terms}
        if (
            _identity(request_terms[intent_document.role_term_ref_id])
            != _identity(GROOVE_INTENT_DOCUMENT_ROLE_TERM)
            or _identity(request_terms[intent_document.schema_term_ref_id])
            != _identity(PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM)
            or intent_document.media_type != PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE
            or _identity(request_terms[capability_document.role_term_ref_id])
            != _identity(GROOVE_CAPABILITY_DOCUMENT_ROLE_TERM)
            or _identity(request_terms[capability_document.schema_term_ref_id])
            != _identity(GROOVE_CAPABILITY_SCHEMA_TERM)
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/documents")
        capability_payload = read_verified_document(
            artifacts,
            capability_document,
            maximum_bytes=request.budget.max_input_bytes - intent_document.size_bytes,
        )
        expected_capability = groove_capability_payload()
        expected_capability_digest = hashlib.sha256(
            _CAPABILITY_DIGEST_DOMAIN + expected_capability
        ).hexdigest()
        if (
            not hmac.compare_digest(capability_payload, expected_capability)
            or capability_document.media_type
            != "application/vnd.vibecad.freecad-groove-capability+json"
            or not hmac.compare_digest(
                capability_document.document_digest, expected_capability_digest
            )
            or capability_document.document_id
            != f"freecad_groove_capability_{expected_capability_digest[:32]}"
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
            plan, target_subject = _build_plan(
                intent_document,
                payload,
                graph,
                request.request_digest,
            )
        except IntentBridgeError:
            raise
        except (ParametricFeatureGraphError, GrooveRuleError):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_document")
        except Exception:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_document")
        if (
            tuple(item.subject for item in report.resolved_subjects) != (target_subject,)
            or report.inert_subjects
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/proof_bundle/target")

        plan_document = _plan_document(plan)
        plan_payload = plan.canonical_bytes
        if len(plan_payload) > min(request.budget.max_output_bytes, MAX_GROOVE_PLAN_BYTES):
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/plan_document")
        try:
            decode_groove_backend_plan(
                plan_payload,
                expected_content_sha256=plan_document.content_sha256,
                expected_plan_sha256=plan_document.document_digest,
            )
        except GrooveRuleError:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_document")
        result = BackendLoweringResult(
            request_digest=request.request_digest,
            adapter=self.descriptor,
            disposition=BridgeDisposition.COMPLETE,
            plan_document=plan_document,
            supported_subjects=(target_subject,),
        )
        validate_lowering_result(request, result)
        receipt = LoweredGroovePlanReceipt(
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
        if (
            type(published) is not bytes
            or not hmac.compare_digest(published, plan_payload)
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink/readback")
        return result, receipt

    def read_plan(
        self,
        receipt: LoweredGroovePlanReceipt,
    ) -> tuple[GrooveBackendPlan, bytes]:
        """Exact sink read plus source/request binding revalidation; no execution."""

        if type(receipt) is not LoweredGroovePlanReceipt or receipt.adapter != self.descriptor:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/receipt")
        document = receipt.plan_document
        if (
            document.role_term_ref_id != GROOVE_PLAN_DOCUMENT_ROLE_TERM.term_ref_id
            or document.schema_term_ref_id != GROOVE_PLAN_SCHEMA_TERM.term_ref_id
            or document.media_type != GROOVE_PLAN_MEDIA_TYPE
            or document.size_bytes > MAX_GROOVE_PLAN_BYTES
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/receipt/plan_document")
        try:
            payload = self._sink.read_exact(document, MAX_GROOVE_PLAN_BYTES)
        except (Exception, SystemExit):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink")
        if (
            type(payload) is not bytes
            or len(payload) != document.size_bytes
            or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), document.content_sha256)
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink/readback")
        try:
            plan = decode_groove_backend_plan(
                payload,
                expected_content_sha256=document.content_sha256,
                expected_plan_sha256=document.document_digest,
            )
        except GrooveRuleError:
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
    "FREECAD_GROOVE_ADAPTER_DESCRIPTOR",
    "GROOVE_ANGLE_ROLE_TERM",
    "GROOVE_ANGLE_TYPE_TERM",
    "GROOVE_AXIS_RESULT_ROLE_TERM",
    "GROOVE_AXIS_ROLE_TERM",
    "GROOVE_BASE_ROLE_TERM",
    "GROOVE_BOOLEAN_TYPE_TERM",
    "GROOVE_CANONICAL_JSON_TERM",
    "GROOVE_CAPABILITY_DOCUMENT_ROLE_TERM",
    "GROOVE_CAPABILITY_SCHEMA_TERM",
    "GROOVE_CLOSED_PROFILE_TYPE_TERM",
    "GROOVE_FAMILY_TERM",
    "GROOVE_INTENT_DOCUMENT_ROLE_TERM",
    "GROOVE_OPERATION_TERM",
    "GROOVE_PFG_TERMS",
    "GROOVE_PLAN_DOCUMENT_ROLE_TERM",
    "GROOVE_PLAN_SCHEMA_TERM",
    "GROOVE_PROFILE_RESULT_ROLE_TERM",
    "GROOVE_PROFILE_ROLE_TERM",
    "GROOVE_REQUEST_TERMS",
    "GROOVE_REVERSED_ROLE_TERM",
    "GROOVE_SKETCH_AXIS_TYPE_TERM",
    "GROOVE_SKETCH_V_AXIS_LOCATOR_TERM",
    "GROOVE_SOLID_RESULT_ROLE_TERM",
    "GROOVE_SOLID_TYPE_TERM",
    "GROOVE_STRUCTURE_TERM",
    "FreeCADParametricGrooveAdapter",
    "LoweredGroovePlanReceipt",
    "PlanSink",
    "build_groove_capability_document",
    "groove_capability_payload",
]
