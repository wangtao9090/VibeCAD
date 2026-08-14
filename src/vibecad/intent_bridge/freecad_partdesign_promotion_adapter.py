"""Private PFGv2 lowering for six reviewed FreeCAD PartDesign operations.

One static semantic catalog lowers Additive/Subtractive Loft, Pipe, and Helix
into one canonical authority-free plan format.  A graph can select only a full
reviewed semantic identity; no graph string is interpreted as a FreeCAD
``TypeId`` or property.  Native execution remains a separate trusted-host act.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
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
    SemanticReferenceScope,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_partdesign_promotion_rules import (
    MAX_PARTDESIGN_PROMOTION_PLAN_BYTES,
    PARTDESIGN_PROMOTION_FREECAD_ENGINE_BUILD_ID,
    PARTDESIGN_PROMOTION_PLAN_MEDIA_TYPE,
    PARTDESIGN_PROMOTION_RULE_CONTRACT_SHA256,
    PARTDESIGN_PROMOTION_RULE_ID,
    PartDesignPromotionBackendPlan,
    PartDesignPromotionOperation,
    PartDesignPromotionRuleError,
    SemanticObjectSelection,
    decode_partdesign_promotion_backend_plan,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.freecad-partdesign"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.freecad-partdesign-ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.freecad-partdesign-promotion-adapter.v1\0"
_CAPABILITY_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-promotion-capability.v1\0"
_PLAN_DOCUMENT_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-promotion-document.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-promotion-lowering-receipt.v1\0"


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


PROMOTION_INTENT_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_parametric_intent", "document-role.parametric-intent"
)
PROMOTION_CAPABILITY_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_partdesign_promotion_capability",
    "document-role.freecad-partdesign-promotion-capability",
)
PROMOTION_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_freecad_partdesign_promotion_capability_v1",
    "document-schema.freecad-partdesign-promotion-capability-v1",
)
PROMOTION_PLAN_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_backend_plan", "document-role.freecad-backend-plan"
)
PROMOTION_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_freecad_partdesign_promotion_plan_v1",
    "document-schema.freecad-partdesign-promotion-plan-v1",
)

PROMOTION_STRUCTURE_TERM: Final = _pfg_term(
    "structure_partdesign_body_feature", "structure.partdesign-body-feature"
)
PROMOTION_BASE_ROLE_TERM: Final = _pfg_term("role_base_solid", "input-role.base-solid")
PROMOTION_PROFILE_ROLE_TERM: Final = _pfg_term("role_closed_profile", "input-role.closed-profile")
PROMOTION_SPINE_ROLE_TERM: Final = _pfg_term("role_continuous_spine", "input-role.continuous-spine")
PROMOTION_AXIS_ROLE_TERM: Final = _pfg_term("role_revolution_axis", "input-role.revolution-axis")
PROMOTION_PITCH_ROLE_TERM: Final = _pfg_term("role_pitch_mm", "input-role.pitch-mm")
PROMOTION_HEIGHT_ROLE_TERM: Final = _pfg_term("role_height_mm", "input-role.height-mm")
PROMOTION_ANGLE_ROLE_TERM: Final = _pfg_term(
    "role_helix_angle_degrees", "input-role.helix-angle-degrees"
)
PROMOTION_SOLID_RESULT_ROLE_TERM: Final = _pfg_term("role_result_solid", "result-role.solid")
PROMOTION_PROFILE_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_closed_profile", "result-role.closed-profile"
)
PROMOTION_SPINE_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_continuous_spine", "result-role.continuous-spine"
)
PROMOTION_AXIS_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_sketch_axis", "result-role.sketch-axis"
)
PROMOTION_SOLID_TYPE_TERM: Final = _pfg_term("type_solid", "value-type.solid")
PROMOTION_CLOSED_PROFILE_TYPE_TERM: Final = _pfg_term(
    "type_closed_profile", "value-type.closed-profile"
)
PROMOTION_CONTINUOUS_SPINE_TYPE_TERM: Final = _pfg_term(
    "type_continuous_spine", "value-type.continuous-spine"
)
PROMOTION_SKETCH_AXIS_TYPE_TERM: Final = _pfg_term("type_sketch_axis", "value-type.sketch-axis")
PROMOTION_LENGTH_TYPE_TERM: Final = _pfg_term("type_length_mm", "value-type.length-mm")
PROMOTION_ANGLE_TYPE_TERM: Final = _pfg_term("type_angle_degrees", "value-type.angle-degrees")
PROMOTION_CANONICAL_JSON_TERM: Final = _pfg_term(
    "encoding_canonical_json", "value-encoding.canonical-json"
)
PROMOTION_SKETCH_V_AXIS_LOCATOR_TERM: Final = _pfg_term(
    "locator_sketch_v_axis", "reference-locator.sketch-v-axis"
)


@dataclass(frozen=True, slots=True)
class _OperationTerms:
    operation: PartDesignPromotionOperation
    family_term: SemanticTermRefV2
    operation_term: SemanticTermRefV2

    @property
    def additive(self) -> bool:
        return self.operation.value.startswith("additive_")

    @property
    def family(self) -> str:
        return self.operation.value.rsplit("_", 1)[1]


PROMOTION_OPERATION_TERMS: Final = (
    _OperationTerms(
        PartDesignPromotionOperation.ADDITIVE_LOFT,
        _pfg_term("family_additive_loft", "feature-family.additive-loft"),
        _pfg_term("operation_additive_loft", "operation.partdesign-additive-loft"),
    ),
    _OperationTerms(
        PartDesignPromotionOperation.SUBTRACTIVE_LOFT,
        _pfg_term("family_subtractive_loft", "feature-family.subtractive-loft"),
        _pfg_term("operation_subtractive_loft", "operation.partdesign-subtractive-loft"),
    ),
    _OperationTerms(
        PartDesignPromotionOperation.ADDITIVE_PIPE,
        _pfg_term("family_additive_pipe", "feature-family.additive-pipe"),
        _pfg_term("operation_additive_pipe", "operation.partdesign-additive-pipe"),
    ),
    _OperationTerms(
        PartDesignPromotionOperation.SUBTRACTIVE_PIPE,
        _pfg_term("family_subtractive_pipe", "feature-family.subtractive-pipe"),
        _pfg_term("operation_subtractive_pipe", "operation.partdesign-subtractive-pipe"),
    ),
    _OperationTerms(
        PartDesignPromotionOperation.ADDITIVE_HELIX,
        _pfg_term("family_additive_helix", "feature-family.additive-helix"),
        _pfg_term("operation_additive_helix", "operation.partdesign-additive-helix"),
    ),
    _OperationTerms(
        PartDesignPromotionOperation.SUBTRACTIVE_HELIX,
        _pfg_term("family_subtractive_helix", "feature-family.subtractive-helix"),
        _pfg_term("operation_subtractive_helix", "operation.partdesign-subtractive-helix"),
    ),
)

_COMMON_PFG_TERMS: Final = (
    PROMOTION_STRUCTURE_TERM,
    PROMOTION_BASE_ROLE_TERM,
    PROMOTION_PROFILE_ROLE_TERM,
    PROMOTION_SPINE_ROLE_TERM,
    PROMOTION_AXIS_ROLE_TERM,
    PROMOTION_PITCH_ROLE_TERM,
    PROMOTION_HEIGHT_ROLE_TERM,
    PROMOTION_ANGLE_ROLE_TERM,
    PROMOTION_SOLID_RESULT_ROLE_TERM,
    PROMOTION_PROFILE_RESULT_ROLE_TERM,
    PROMOTION_SPINE_RESULT_ROLE_TERM,
    PROMOTION_AXIS_RESULT_ROLE_TERM,
    PROMOTION_SOLID_TYPE_TERM,
    PROMOTION_CLOSED_PROFILE_TYPE_TERM,
    PROMOTION_CONTINUOUS_SPINE_TYPE_TERM,
    PROMOTION_SKETCH_AXIS_TYPE_TERM,
    PROMOTION_LENGTH_TYPE_TERM,
    PROMOTION_ANGLE_TYPE_TERM,
    PROMOTION_CANONICAL_JSON_TERM,
    PROMOTION_SKETCH_V_AXIS_LOCATOR_TERM,
)
PROMOTION_PFG_TERMS: Final = (
    *_COMMON_PFG_TERMS,
    *(item.family_term for item in PROMOTION_OPERATION_TERMS),
    *(item.operation_term for item in PROMOTION_OPERATION_TERMS),
)


def _as_bridge(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


PROMOTION_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PROMOTION_INTENT_DOCUMENT_ROLE_TERM,
    PROMOTION_CAPABILITY_DOCUMENT_ROLE_TERM,
    PROMOTION_CAPABILITY_SCHEMA_TERM,
    PROMOTION_PLAN_DOCUMENT_ROLE_TERM,
    PROMOTION_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in PROMOTION_PFG_TERMS),
)

_ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            PARTDESIGN_PROMOTION_RULE_ID.encode("ascii"),
            PARTDESIGN_PROMOTION_RULE_CONTRACT_SHA256.encode("ascii"),
            b"canonical-pfg-v2;exact-proof;full-static-terms;atomic-plan-sink;no-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in (*PROMOTION_REQUEST_TERMS, PFG_SELECTOR_FEATURE_NODE)
            ),
        )
    )
).hexdigest()

FREECAD_PARTDESIGN_PROMOTION_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_partdesign_promotion_adapter",
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


def promotion_capability_payload() -> bytes:
    return _canonical_json(
        {
            "schema_version": 1,
            "authority": "none",
            "adapter": FREECAD_PARTDESIGN_PROMOTION_ADAPTER_DESCRIPTOR.to_mapping(),
            "backend": {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PARTDESIGN_PROMOTION_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": PARTDESIGN_PROMOTION_RULE_ID,
                "rule_contract_sha256": PARTDESIGN_PROMOTION_RULE_CONTRACT_SHA256,
                "operations": [item.operation.value for item in PROMOTION_OPERATION_TERMS],
            },
        }
    )


def build_promotion_capability_document(
    *, artifact_id: str = "artifact_freecad_partdesign_promotion_capability"
) -> tuple[DocumentRef, bytes]:
    payload = promotion_capability_payload()
    digest = hashlib.sha256(_CAPABILITY_DIGEST_DOMAIN + payload).hexdigest()
    return (
        DocumentRef(
            artifact_id=artifact_id,
            role_term_ref_id=PROMOTION_CAPABILITY_DOCUMENT_ROLE_TERM.term_ref_id,
            schema_term_ref_id=PROMOTION_CAPABILITY_SCHEMA_TERM.term_ref_id,
            document_id=f"freecad_partdesign_promotion_capability_{digest[:32]}",
            document_digest=digest,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type="application/vnd.vibecad.freecad-partdesign-promotion-capability+json",
        ),
        payload,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class LoweredPartDesignPromotionPlanReceipt:
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
        object.__setattr__(self, "receipt_id", f"partdesign_promotion_lowering_{digest[:32]}")

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
) -> _OperationTerms | None:
    structural = terms.get(target.intent.structural_kind_term_ref_id)
    family = terms.get(target.intent.family_term_ref_id)
    operation = terms.get(target.intent.operation_term_ref_id)
    if structural is None or _identity(structural) != _identity(PROMOTION_STRUCTURE_TERM):
        return None
    matches = tuple(
        item
        for item in PROMOTION_OPERATION_TERMS
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
) -> tuple[PartDesignPromotionBackendPlan, SubjectRef]:
    if (
        graph.graph_id != document.document_id
        or len(graph.bodies) != 1
        or len(graph.graph_results) != 1
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")
    _assert_no_extensions(graph)
    terms = {term.term_ref_id: term for term in graph.terms}
    for expected in PROMOTION_PFG_TERMS:
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
    required_base = not operation.additive
    expected_ports = {
        _identity(PROMOTION_BASE_ROLE_TERM): (
            PROMOTION_SOLID_TYPE_TERM,
            "base",
            1 if required_base else 0,
            1,
            False,
        ),
        _identity(PROMOTION_PROFILE_ROLE_TERM): (
            PROMOTION_CLOSED_PROFILE_TYPE_TERM,
            "profile",
            2 if operation.family == "loft" else 1,
            8 if operation.family == "loft" else 1,
            operation.family == "loft",
        ),
    }
    if operation.family == "pipe":
        expected_ports[_identity(PROMOTION_SPINE_ROLE_TERM)] = (
            PROMOTION_CONTINUOUS_SPINE_TYPE_TERM,
            "spine",
            1,
            1,
            False,
        )
    elif operation.family == "helix":
        expected_ports.update(
            {
                _identity(PROMOTION_AXIS_ROLE_TERM): (
                    PROMOTION_SKETCH_AXIS_TYPE_TERM,
                    "axis",
                    1,
                    1,
                    False,
                ),
                _identity(PROMOTION_PITCH_ROLE_TERM): (
                    PROMOTION_LENGTH_TYPE_TERM,
                    "pitch",
                    1,
                    1,
                    False,
                ),
                _identity(PROMOTION_HEIGHT_ROLE_TERM): (
                    PROMOTION_LENGTH_TYPE_TERM,
                    "height",
                    1,
                    1,
                    False,
                ),
                _identity(PROMOTION_ANGLE_ROLE_TERM): (
                    PROMOTION_ANGLE_TYPE_TERM,
                    "angle",
                    1,
                    1,
                    False,
                ),
            }
        )
    if len(target.intent.input_ports) != len(expected_ports):
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

    grouped: dict[str, list[object]] = {kind: [] for kind in port_kinds.values()}
    for item in (
        *target.intent.dependencies,
        *target.intent.references,
        *target.intent.parameter_bindings,
    ):
        kind = port_kinds.get(item.port_id)
        if kind is None:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/bindings")
        grouped[kind].append(item)
    if operation.family == "loft":
        valid_counts = (
            (len(grouped["base"]) in ({1} if required_base else {0, 1}))
            and 2 <= len(grouped["profile"]) <= 8
            and not target.intent.references
            and not target.intent.parameter_bindings
        )
    elif operation.family == "pipe":
        valid_counts = (
            (len(grouped["base"]) in ({1} if required_base else {0, 1}))
            and len(grouped["profile"]) == 1
            and len(grouped["spine"]) == 1
            and not target.intent.references
            and not target.intent.parameter_bindings
        )
    else:
        valid_counts = (
            (len(grouped["base"]) in ({1} if required_base else {0, 1}))
            and len(grouped["profile"]) == 1
            and len(grouped["axis"]) == 1
            and len(grouped["pitch"]) == 1
            and len(grouped["height"]) == 1
            and len(grouped["angle"]) == 1
            and len(target.intent.references) == 1
            and len(target.intent.parameter_bindings) == 3
        )
    if not valid_counts:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/bindings")
    for _kind, values in grouped.items():
        if sorted(item.ordinal for item in values) != list(range(len(values))):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/ordinals")

    nodes = {node.node_id: node for node in graph.nodes}
    consumed_nodes = {target.node_id}

    def dependency_selection(
        item: object,
        *,
        role: SemanticTermRefV2,
        value_type: SemanticTermRefV2,
        expected_result_count: int,
        path: str,
    ) -> tuple[SemanticObjectSelection, FeatureNodeV2]:
        node = nodes.get(item.upstream_node_id)
        if node is None or node.body_id != body.body_id or node.node_id in consumed_nodes:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)
        result = _result_with_identity(node, terms, role=role, value_type=value_type, path=path)
        if (
            item.upstream_result_id != result.result_id
            or len(node.results) != expected_result_count
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)
        consumed_nodes.add(node.node_id)
        return SemanticObjectSelection(node_id=node.node_id, result_id=result.result_id), node

    base = None
    if grouped["base"]:
        base, _ = dependency_selection(
            grouped["base"][0],
            role=PROMOTION_SOLID_RESULT_ROLE_TERM,
            value_type=PROMOTION_SOLID_TYPE_TERM,
            expected_result_count=1,
            path="/graph/base",
        )
    profiles: list[SemanticObjectSelection] = []
    profile_nodes: list[FeatureNodeV2] = []
    for item in sorted(grouped["profile"], key=lambda value: value.ordinal):
        selection, profile_node = dependency_selection(
            item,
            role=PROMOTION_PROFILE_RESULT_ROLE_TERM,
            value_type=PROMOTION_CLOSED_PROFILE_TYPE_TERM,
            expected_result_count=2 if operation.family == "helix" else 1,
            path="/graph/profiles",
        )
        profiles.append(selection)
        profile_nodes.append(profile_node)
    spine = None
    if operation.family == "pipe":
        spine, _ = dependency_selection(
            grouped["spine"][0],
            role=PROMOTION_SPINE_RESULT_ROLE_TERM,
            value_type=PROMOTION_CONTINUOUS_SPINE_TYPE_TERM,
            expected_result_count=1,
            path="/graph/spine",
        )
    axis_reference_id = None
    axis_result_id = None
    values: dict[str, float] = {}
    if operation.family == "helix":
        profile_node = profile_nodes[0]
        axis_result = _result_with_identity(
            profile_node,
            terms,
            role=PROMOTION_AXIS_RESULT_ROLE_TERM,
            value_type=PROMOTION_SKETCH_AXIS_TYPE_TERM,
            path="/graph/axis/result",
        )
        references = {item.reference_id: item for item in graph.references}
        axis_binding = grouped["axis"][0]
        axis = references.get(axis_binding.reference_id)
        if axis is None:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/axis")
        for ref_id, expected, path in (
            (axis.semantic_role_term_ref_id, PROMOTION_AXIS_ROLE_TERM, "/graph/axis/role"),
            (axis.value_type_term_ref_id, PROMOTION_SKETCH_AXIS_TYPE_TERM, "/graph/axis/type"),
            (
                axis.locator_term_ref_id,
                PROMOTION_SKETCH_V_AXIS_LOCATOR_TERM,
                "/graph/axis/locator",
            ),
        ):
            _graph_term(terms, ref_id, expected, path)
        if (
            axis.scope is not SemanticReferenceScope.FEATURE
            or axis.source_node_id != profile_node.node_id
            or axis.source_geometry_id != axis_result.result_id
            or axis.source_content_sha256 is not None
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/axis/source")
        axis_reference_id = axis.reference_id
        axis_result_id = axis_result.result_id
        parameters = {item.parameter_id: item for item in graph.parameters}
        parameter_specs = {
            "pitch": (PROMOTION_PITCH_ROLE_TERM, PROMOTION_LENGTH_TYPE_TERM),
            "height": (PROMOTION_HEIGHT_ROLE_TERM, PROMOTION_LENGTH_TYPE_TERM),
            "angle": (PROMOTION_ANGLE_ROLE_TERM, PROMOTION_ANGLE_TYPE_TERM),
        }
        for kind, (role, value_type) in parameter_specs.items():
            binding = grouped[kind][0]
            parameter = parameters.get(binding.parameter_id)
            if parameter is None or parameter.expression is not None:
                _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, f"/graph/{kind}")
            _graph_term(terms, parameter.semantic_role_term_ref_id, role, f"/graph/{kind}/role")
            _graph_term(
                terms,
                parameter.value.value_type_term_ref_id,
                value_type,
                f"/graph/{kind}/type",
            )
            _graph_term(
                terms,
                parameter.value.encoding_term_ref_id,
                PROMOTION_CANONICAL_JSON_TERM,
                f"/graph/{kind}/encoding",
            )
            value = parameter.value.value
            if type(value) not in {int, float}:
                _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, f"/graph/{kind}/value")
            try:
                numeric_value = float(value)
            except (OverflowError, TypeError, ValueError):
                _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, f"/graph/{kind}/value")
            if not math.isfinite(numeric_value):
                _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, f"/graph/{kind}/value")
            values[kind] = numeric_value
        if len(graph.parameters) != 3 or len(graph.references) != 1:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/values")
    elif graph.parameters or graph.references:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/values")
    if set(nodes) != consumed_nodes:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")

    target_result = _result_with_identity(
        target,
        terms,
        role=PROMOTION_SOLID_RESULT_ROLE_TERM,
        value_type=PROMOTION_SOLID_TYPE_TERM,
        path="/graph/result",
    )
    selection = graph.graph_results[0]
    if (
        len(target.results) != 1
        or selection.node_id != target.node_id
        or selection.result_id != target_result.result_id
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/graph_results")
    plan = PartDesignPromotionBackendPlan(
        source_artifact_id=document.artifact_id,
        source_graph_id=graph.graph_id,
        source_graph_sha256=graph.graph_sha256,
        source_content_sha256=hashlib.sha256(payload).hexdigest(),
        lowering_request_sha256=request_digest,
        adapter_contract_sha256=FREECAD_PARTDESIGN_PROMOTION_ADAPTER_DESCRIPTOR.adapter_contract_sha256,
        body_id=body.body_id,
        node_id=target.node_id,
        result_id=target_result.result_id,
        operation=operation.operation,
        base=base,
        profiles=tuple(profiles),
        spine=spine,
        axis_reference_id=axis_reference_id,
        axis_result_id=axis_result_id,
        pitch_mm=values.get("pitch"),
        height_mm=values.get("height"),
        angle_degrees=values.get("angle"),
    )
    return plan, SubjectRef(
        artifact_id=document.artifact_id,
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id=target.node_id,
    )


def _plan_document(plan: PartDesignPromotionBackendPlan) -> DocumentRef:
    payload = plan.canonical_bytes
    content_sha256 = hashlib.sha256(payload).hexdigest()
    semantic_digest = hashlib.sha256(
        _PLAN_DOCUMENT_DIGEST_DOMAIN + bytes.fromhex(plan.plan_sha256)
    ).hexdigest()
    return DocumentRef(
        artifact_id=f"artifact_freecad_partdesign_promotion_plan_{content_sha256[:32]}",
        role_term_ref_id=PROMOTION_PLAN_DOCUMENT_ROLE_TERM.term_ref_id,
        schema_term_ref_id=PROMOTION_PLAN_SCHEMA_TERM.term_ref_id,
        document_id=f"freecad_partdesign_promotion_plan_{semantic_digest[:32]}",
        document_digest=plan.plan_sha256,
        content_sha256=content_sha256,
        size_bytes=len(payload),
        media_type=PARTDESIGN_PROMOTION_PLAN_MEDIA_TYPE,
    )


class FreeCADPartDesignPromotionAdapter:
    """Single exact PFGv2-to-plan adapter for the six-operation batch."""

    __slots__ = ("_sink",)

    def __init__(self, sink: PlanSink) -> None:
        if not isinstance(sink, PlanSink):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/plan_sink")
        self._sink = sink

    @property
    def descriptor(self) -> AdapterDescriptor:
        return FREECAD_PARTDESIGN_PROMOTION_ADAPTER_DESCRIPTOR

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
    ) -> tuple[BackendLoweringResult, LoweredPartDesignPromotionPlanReceipt]:
        if type(request) is not BackendLoweringRequest or request.adapter != self.descriptor:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/adapter")
        if type(codecs) is not TrustedCodecRegistry:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/codecs")
        identities = {_identity(term) for term in request.terms}
        if any(_identity(term) not in identities for term in PROMOTION_REQUEST_TERMS):
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
            != _identity(PROMOTION_INTENT_DOCUMENT_ROLE_TERM)
            or _identity(request_terms[intent_document.schema_term_ref_id])
            != _identity(PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM)
            or intent_document.media_type != PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE
            or _identity(request_terms[capability_document.role_term_ref_id])
            != _identity(PROMOTION_CAPABILITY_DOCUMENT_ROLE_TERM)
            or _identity(request_terms[capability_document.schema_term_ref_id])
            != _identity(PROMOTION_CAPABILITY_SCHEMA_TERM)
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/documents")
        capability_payload = read_verified_document(
            artifacts,
            capability_document,
            maximum_bytes=request.budget.max_input_bytes - intent_document.size_bytes,
        )
        expected_capability = promotion_capability_payload()
        expected_digest = hashlib.sha256(
            _CAPABILITY_DIGEST_DOMAIN + expected_capability
        ).hexdigest()
        if (
            not hmac.compare_digest(capability_payload, expected_capability)
            or capability_document.media_type
            != "application/vnd.vibecad.freecad-partdesign-promotion-capability+json"
            or not hmac.compare_digest(capability_document.document_digest, expected_digest)
            or capability_document.document_id
            != f"freecad_partdesign_promotion_capability_{expected_digest[:32]}"
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
        except (ParametricFeatureGraphError, PartDesignPromotionRuleError):
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
            request.budget.max_output_bytes, MAX_PARTDESIGN_PROMOTION_PLAN_BYTES
        ):
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/plan_document")
        try:
            decode_partdesign_promotion_backend_plan(
                plan_payload,
                expected_content_sha256=plan_document.content_sha256,
                expected_plan_sha256=plan_document.document_digest,
            )
        except PartDesignPromotionRuleError:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_document")
        result = BackendLoweringResult(
            request_digest=request.request_digest,
            adapter=self.descriptor,
            disposition=BridgeDisposition.COMPLETE,
            plan_document=plan_document,
            supported_subjects=(subject,),
        )
        validate_lowering_result(request, result)
        receipt = LoweredPartDesignPromotionPlanReceipt(
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
        self, receipt: LoweredPartDesignPromotionPlanReceipt
    ) -> tuple[PartDesignPromotionBackendPlan, bytes]:
        if (
            type(receipt) is not LoweredPartDesignPromotionPlanReceipt
            or receipt.adapter != self.descriptor
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/receipt")
        document = receipt.plan_document
        if (
            document.role_term_ref_id != PROMOTION_PLAN_DOCUMENT_ROLE_TERM.term_ref_id
            or document.schema_term_ref_id != PROMOTION_PLAN_SCHEMA_TERM.term_ref_id
            or document.media_type != PARTDESIGN_PROMOTION_PLAN_MEDIA_TYPE
            or document.size_bytes > MAX_PARTDESIGN_PROMOTION_PLAN_BYTES
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/receipt/plan_document")
        try:
            payload = self._sink.read_exact(document, MAX_PARTDESIGN_PROMOTION_PLAN_BYTES)
        except (Exception, SystemExit):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink")
        if (
            type(payload) is not bytes
            or len(payload) != document.size_bytes
            or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), document.content_sha256)
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink/readback")
        try:
            plan = decode_partdesign_promotion_backend_plan(
                payload,
                expected_content_sha256=document.content_sha256,
                expected_plan_sha256=document.document_digest,
            )
        except PartDesignPromotionRuleError:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_document")
        if (
            not hmac.compare_digest(plan.lowering_request_sha256, receipt.request_digest)
            or not hmac.compare_digest(
                plan.adapter_contract_sha256, receipt.adapter.adapter_contract_sha256
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
    "FREECAD_PARTDESIGN_PROMOTION_ADAPTER_DESCRIPTOR",
    "PROMOTION_ANGLE_ROLE_TERM",
    "PROMOTION_ANGLE_TYPE_TERM",
    "PROMOTION_AXIS_RESULT_ROLE_TERM",
    "PROMOTION_AXIS_ROLE_TERM",
    "PROMOTION_BASE_ROLE_TERM",
    "PROMOTION_CANONICAL_JSON_TERM",
    "PROMOTION_CAPABILITY_DOCUMENT_ROLE_TERM",
    "PROMOTION_CAPABILITY_SCHEMA_TERM",
    "PROMOTION_CLOSED_PROFILE_TYPE_TERM",
    "PROMOTION_CONTINUOUS_SPINE_TYPE_TERM",
    "PROMOTION_HEIGHT_ROLE_TERM",
    "PROMOTION_INTENT_DOCUMENT_ROLE_TERM",
    "PROMOTION_LENGTH_TYPE_TERM",
    "PROMOTION_OPERATION_TERMS",
    "PROMOTION_PFG_TERMS",
    "PROMOTION_PITCH_ROLE_TERM",
    "PROMOTION_PLAN_DOCUMENT_ROLE_TERM",
    "PROMOTION_PLAN_SCHEMA_TERM",
    "PROMOTION_PROFILE_RESULT_ROLE_TERM",
    "PROMOTION_PROFILE_ROLE_TERM",
    "PROMOTION_REQUEST_TERMS",
    "PROMOTION_SKETCH_AXIS_TYPE_TERM",
    "PROMOTION_SKETCH_V_AXIS_LOCATOR_TERM",
    "PROMOTION_SOLID_RESULT_ROLE_TERM",
    "PROMOTION_SOLID_TYPE_TERM",
    "PROMOTION_SPINE_RESULT_ROLE_TERM",
    "PROMOTION_SPINE_ROLE_TERM",
    "PROMOTION_STRUCTURE_TERM",
    "FreeCADPartDesignPromotionAdapter",
    "LoweredPartDesignPromotionPlanReceipt",
    "build_promotion_capability_document",
    "promotion_capability_payload",
]
