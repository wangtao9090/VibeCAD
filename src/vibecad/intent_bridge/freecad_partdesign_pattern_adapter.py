"""Private backend-neutral PFGv2 lowering for three PartDesign patterns.

The adapter recognizes complete semantic identities for linear, polar, and
mirrored transforms and emits a canonical authority-free plan.  Native
``TypeId`` and property selection remain entirely inside the trusted native
rule module; graph strings never cross that boundary.
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
from vibecad.parametric.freecad_partdesign_pattern_rules import (
    MAX_PARTDESIGN_PATTERN_OCCURRENCES,
    MAX_PARTDESIGN_PATTERN_PLAN_BYTES,
    PARTDESIGN_PATTERN_FREECAD_ENGINE_BUILD_ID,
    PARTDESIGN_PATTERN_PLAN_MEDIA_TYPE,
    PARTDESIGN_PATTERN_RULE_CONTRACT_SHA256,
    PARTDESIGN_PATTERN_RULE_ID,
    PartDesignPatternBackendPlan,
    PartDesignPatternOperation,
    PartDesignPatternRuleError,
    PatternObjectSelection,
    PatternOriginAxis,
    PatternOriginPlane,
    decode_partdesign_pattern_backend_plan,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.freecad-partdesign"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.freecad-partdesign-ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.freecad-partdesign-pattern-adapter.v1\0"
_CAPABILITY_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-pattern-capability.v1\0"
_PLAN_DOCUMENT_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-pattern-document.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-pattern-lowering-receipt.v1\0"


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


PATTERN_INTENT_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_parametric_intent", "document-role.parametric-intent"
)
PATTERN_CAPABILITY_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_partdesign_pattern_capability",
    "document-role.freecad-partdesign-pattern-capability",
)
PATTERN_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_freecad_partdesign_pattern_capability_v1",
    "document-schema.freecad-partdesign-pattern-capability-v1",
)
PATTERN_PLAN_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_freecad_backend_plan", "document-role.freecad-backend-plan"
)
PATTERN_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_freecad_partdesign_pattern_plan_v1",
    "document-schema.freecad-partdesign-pattern-plan-v1",
)

PATTERN_STRUCTURE_TERM: Final = _pfg_term(
    "structure_partdesign_body_feature", "structure.partdesign-body-feature"
)
PATTERN_BASE_ROLE_TERM: Final = _pfg_term("role_base_solid", "input-role.base-solid")
PATTERN_SOURCE_ROLE_TERM: Final = _pfg_term("role_source_feature", "input-role.source-feature")
PATTERN_DIRECTION_ROLE_TERM: Final = _pfg_term(
    "role_pattern_direction", "input-role.pattern-direction"
)
PATTERN_AXIS_ROLE_TERM: Final = _pfg_term("role_pattern_axis", "input-role.pattern-axis")
PATTERN_PLANE_ROLE_TERM: Final = _pfg_term("role_mirror_plane", "input-role.mirror-plane")
PATTERN_OCCURRENCES_ROLE_TERM: Final = _pfg_term(
    "role_pattern_occurrences", "input-role.pattern-occurrences"
)
PATTERN_SPAN_ROLE_TERM: Final = _pfg_term("role_pattern_span_mm", "input-role.pattern-span-mm")
PATTERN_ANGLE_ROLE_TERM: Final = _pfg_term(
    "role_pattern_angle_degrees", "input-role.pattern-angle-degrees"
)
PATTERN_REVERSED_ROLE_TERM: Final = _pfg_term(
    "role_pattern_reversed", "input-role.pattern-reversed"
)
PATTERN_SOLID_RESULT_ROLE_TERM: Final = _pfg_term("role_result_solid", "result-role.solid")
PATTERN_SOLID_TYPE_TERM: Final = _pfg_term("type_solid", "value-type.solid")
PATTERN_ORIGIN_AXIS_TYPE_TERM: Final = _pfg_term("type_origin_axis", "value-type.origin-axis")
PATTERN_ORIGIN_PLANE_TYPE_TERM: Final = _pfg_term("type_origin_plane", "value-type.origin-plane")
PATTERN_INTEGER_TYPE_TERM: Final = _pfg_term("type_integer_count", "value-type.integer-count")
PATTERN_LENGTH_TYPE_TERM: Final = _pfg_term("type_length_mm", "value-type.length-mm")
PATTERN_ANGLE_TYPE_TERM: Final = _pfg_term("type_angle_degrees", "value-type.angle-degrees")
PATTERN_BOOLEAN_TYPE_TERM: Final = _pfg_term("type_boolean", "value-type.boolean")
PATTERN_CANONICAL_JSON_TERM: Final = _pfg_term(
    "encoding_canonical_json", "value-encoding.canonical-json"
)

PATTERN_X_AXIS_LOCATOR_TERM: Final = _pfg_term(
    "locator_body_origin_x_axis", "reference-locator.body-origin-x-axis"
)
PATTERN_Y_AXIS_LOCATOR_TERM: Final = _pfg_term(
    "locator_body_origin_y_axis", "reference-locator.body-origin-y-axis"
)
PATTERN_Z_AXIS_LOCATOR_TERM: Final = _pfg_term(
    "locator_body_origin_z_axis", "reference-locator.body-origin-z-axis"
)
PATTERN_XY_PLANE_LOCATOR_TERM: Final = _pfg_term(
    "locator_body_origin_xy_plane", "reference-locator.body-origin-xy-plane"
)
PATTERN_XZ_PLANE_LOCATOR_TERM: Final = _pfg_term(
    "locator_body_origin_xz_plane", "reference-locator.body-origin-xz-plane"
)
PATTERN_YZ_PLANE_LOCATOR_TERM: Final = _pfg_term(
    "locator_body_origin_yz_plane", "reference-locator.body-origin-yz-plane"
)


@dataclass(frozen=True, slots=True)
class _PatternOperationTerms:
    operation: PartDesignPatternOperation
    family_term: SemanticTermRefV2
    operation_term: SemanticTermRefV2
    reference_role_term: SemanticTermRefV2
    reference_type_term: SemanticTermRefV2


PATTERN_OPERATION_TERMS: Final = (
    _PatternOperationTerms(
        PartDesignPatternOperation.LINEAR_PATTERN,
        _pfg_term("family_linear_pattern", "feature-family.linear-pattern"),
        _pfg_term("operation_linear_pattern", "operation.pattern-linear"),
        PATTERN_DIRECTION_ROLE_TERM,
        PATTERN_ORIGIN_AXIS_TYPE_TERM,
    ),
    _PatternOperationTerms(
        PartDesignPatternOperation.POLAR_PATTERN,
        _pfg_term("family_polar_pattern", "feature-family.polar-pattern"),
        _pfg_term("operation_polar_pattern", "operation.pattern-polar"),
        PATTERN_AXIS_ROLE_TERM,
        PATTERN_ORIGIN_AXIS_TYPE_TERM,
    ),
    _PatternOperationTerms(
        PartDesignPatternOperation.MIRRORED,
        _pfg_term("family_mirrored", "feature-family.mirrored"),
        _pfg_term("operation_mirrored", "operation.pattern-mirror"),
        PATTERN_PLANE_ROLE_TERM,
        PATTERN_ORIGIN_PLANE_TYPE_TERM,
    ),
)

_AXIS_LOCATORS: Final = (
    (PATTERN_X_AXIS_LOCATOR_TERM, PatternOriginAxis.X),
    (PATTERN_Y_AXIS_LOCATOR_TERM, PatternOriginAxis.Y),
    (PATTERN_Z_AXIS_LOCATOR_TERM, PatternOriginAxis.Z),
)
_PLANE_LOCATORS: Final = (
    (PATTERN_XY_PLANE_LOCATOR_TERM, PatternOriginPlane.XY),
    (PATTERN_XZ_PLANE_LOCATOR_TERM, PatternOriginPlane.XZ),
    (PATTERN_YZ_PLANE_LOCATOR_TERM, PatternOriginPlane.YZ),
)

_COMMON_PFG_TERMS: Final = (
    PATTERN_STRUCTURE_TERM,
    PATTERN_BASE_ROLE_TERM,
    PATTERN_SOURCE_ROLE_TERM,
    PATTERN_DIRECTION_ROLE_TERM,
    PATTERN_AXIS_ROLE_TERM,
    PATTERN_PLANE_ROLE_TERM,
    PATTERN_OCCURRENCES_ROLE_TERM,
    PATTERN_SPAN_ROLE_TERM,
    PATTERN_ANGLE_ROLE_TERM,
    PATTERN_REVERSED_ROLE_TERM,
    PATTERN_SOLID_RESULT_ROLE_TERM,
    PATTERN_SOLID_TYPE_TERM,
    PATTERN_ORIGIN_AXIS_TYPE_TERM,
    PATTERN_ORIGIN_PLANE_TYPE_TERM,
    PATTERN_INTEGER_TYPE_TERM,
    PATTERN_LENGTH_TYPE_TERM,
    PATTERN_ANGLE_TYPE_TERM,
    PATTERN_BOOLEAN_TYPE_TERM,
    PATTERN_CANONICAL_JSON_TERM,
    PATTERN_X_AXIS_LOCATOR_TERM,
    PATTERN_Y_AXIS_LOCATOR_TERM,
    PATTERN_Z_AXIS_LOCATOR_TERM,
    PATTERN_XY_PLANE_LOCATOR_TERM,
    PATTERN_XZ_PLANE_LOCATOR_TERM,
    PATTERN_YZ_PLANE_LOCATOR_TERM,
)
PATTERN_PFG_TERMS: Final = (
    *_COMMON_PFG_TERMS,
    *(item.family_term for item in PATTERN_OPERATION_TERMS),
    *(item.operation_term for item in PATTERN_OPERATION_TERMS),
)


def _as_bridge(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


PATTERN_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PATTERN_INTENT_DOCUMENT_ROLE_TERM,
    PATTERN_CAPABILITY_DOCUMENT_ROLE_TERM,
    PATTERN_CAPABILITY_SCHEMA_TERM,
    PATTERN_PLAN_DOCUMENT_ROLE_TERM,
    PATTERN_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in PATTERN_PFG_TERMS),
)

_ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            PARTDESIGN_PATTERN_RULE_ID.encode("ascii"),
            PARTDESIGN_PATTERN_RULE_CONTRACT_SHA256.encode("ascii"),
            b"canonical-pfg-v2;origin-references;exact-proof;atomic-plan-sink;no-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in (*PATTERN_REQUEST_TERMS, PFG_SELECTOR_FEATURE_NODE)
            ),
        )
    )
).hexdigest()

FREECAD_PARTDESIGN_PATTERN_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_partdesign_pattern_adapter",
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


def pattern_capability_payload() -> bytes:
    return _canonical_json(
        {
            "schema_version": 1,
            "authority": "none",
            "adapter": FREECAD_PARTDESIGN_PATTERN_ADAPTER_DESCRIPTOR.to_mapping(),
            "backend": {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PARTDESIGN_PATTERN_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": PARTDESIGN_PATTERN_RULE_ID,
                "rule_contract_sha256": PARTDESIGN_PATTERN_RULE_CONTRACT_SHA256,
                "operations": [item.operation.value for item in PATTERN_OPERATION_TERMS],
                "max_occurrences": MAX_PARTDESIGN_PATTERN_OCCURRENCES,
            },
        }
    )


def build_pattern_capability_document(
    *, artifact_id: str = "artifact_freecad_partdesign_pattern_capability"
) -> tuple[DocumentRef, bytes]:
    payload = pattern_capability_payload()
    digest = hashlib.sha256(_CAPABILITY_DIGEST_DOMAIN + payload).hexdigest()
    return (
        DocumentRef(
            artifact_id=artifact_id,
            role_term_ref_id=PATTERN_CAPABILITY_DOCUMENT_ROLE_TERM.term_ref_id,
            schema_term_ref_id=PATTERN_CAPABILITY_SCHEMA_TERM.term_ref_id,
            document_id=f"freecad_partdesign_pattern_capability_{digest[:32]}",
            document_digest=digest,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type="application/vnd.vibecad.freecad-partdesign-pattern-capability+json",
        ),
        payload,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class LoweredPartDesignPatternPlanReceipt:
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
        object.__setattr__(self, "receipt_id", f"partdesign_pattern_lowering_{digest[:32]}")

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
    target: FeatureNodeV2, terms: dict[str, SemanticTermRefV2]
) -> _PatternOperationTerms | None:
    structural = terms.get(target.intent.structural_kind_term_ref_id)
    family = terms.get(target.intent.family_term_ref_id)
    operation = terms.get(target.intent.operation_term_ref_id)
    if structural is None or _identity(structural) != _identity(PATTERN_STRUCTURE_TERM):
        return None
    matches = tuple(
        item
        for item in PATTERN_OPERATION_TERMS
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
) -> tuple[PartDesignPatternBackendPlan, SubjectRef]:
    if (
        graph.graph_id != document.document_id
        or len(graph.bodies) != 1
        or len(graph.graph_results) != 1
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")
    _assert_no_extensions(graph)
    terms = {term.term_ref_id: term for term in graph.terms}
    for expected in PATTERN_PFG_TERMS:
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

    expected_ports: dict[tuple[str, str, str, str], tuple[SemanticTermRefV2, str]] = {
        _identity(PATTERN_BASE_ROLE_TERM): (PATTERN_SOLID_TYPE_TERM, "base"),
        _identity(PATTERN_SOURCE_ROLE_TERM): (PATTERN_SOLID_TYPE_TERM, "source"),
        _identity(operation.reference_role_term): (operation.reference_type_term, "reference"),
    }
    if operation.operation is PartDesignPatternOperation.LINEAR_PATTERN:
        expected_ports.update(
            {
                _identity(PATTERN_OCCURRENCES_ROLE_TERM): (
                    PATTERN_INTEGER_TYPE_TERM,
                    "occurrences",
                ),
                _identity(PATTERN_SPAN_ROLE_TERM): (PATTERN_LENGTH_TYPE_TERM, "span"),
                _identity(PATTERN_REVERSED_ROLE_TERM): (PATTERN_BOOLEAN_TYPE_TERM, "reversed"),
            }
        )
    elif operation.operation is PartDesignPatternOperation.POLAR_PATTERN:
        expected_ports.update(
            {
                _identity(PATTERN_OCCURRENCES_ROLE_TERM): (
                    PATTERN_INTEGER_TYPE_TERM,
                    "occurrences",
                ),
                _identity(PATTERN_ANGLE_ROLE_TERM): (PATTERN_ANGLE_TYPE_TERM, "angle"),
                _identity(PATTERN_REVERSED_ROLE_TERM): (PATTERN_BOOLEAN_TYPE_TERM, "reversed"),
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
            or port.minimum_cardinality != 1
            or port.maximum_cardinality != 1
            or port.ordered
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
    parameter_count = 0 if operation.operation is PartDesignPatternOperation.MIRRORED else 3
    if (
        len(target.intent.dependencies) != 2
        or len(target.intent.references) != 1
        or len(target.intent.parameter_bindings) != parameter_count
        or any(len(items) != 1 for items in grouped.values())
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/bindings")

    nodes = {node.node_id: node for node in graph.nodes}

    def dependency_selection(item: object, path: str) -> PatternObjectSelection:
        node = nodes.get(item.upstream_node_id)
        if node is None or node.body_id != body.body_id or node.node_id == target.node_id:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)
        result = _result_with_identity(
            node,
            terms,
            role=PATTERN_SOLID_RESULT_ROLE_TERM,
            value_type=PATTERN_SOLID_TYPE_TERM,
            path=path,
        )
        if item.upstream_result_id != result.result_id or len(node.results) != 1:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)
        return PatternObjectSelection(node_id=node.node_id, result_id=result.result_id)

    base = dependency_selection(grouped["base"][0], "/graph/base")
    source = dependency_selection(grouped["source"][0], "/graph/source")
    consumed_nodes = {target.node_id, base.node_id, source.node_id}
    if set(nodes) != consumed_nodes:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")

    references = {item.reference_id: item for item in graph.references}
    reference_binding = grouped["reference"][0]
    reference = references.get(reference_binding.reference_id)
    if reference is None:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/reference")
    _graph_term(
        terms,
        reference.semantic_role_term_ref_id,
        operation.reference_role_term,
        "/graph/reference/role",
    )
    _graph_term(
        terms,
        reference.value_type_term_ref_id,
        operation.reference_type_term,
        "/graph/reference/type",
    )
    if reference.scope is not SemanticReferenceScope.ORIGIN:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/reference/scope")
    locator = terms.get(reference.locator_term_ref_id)
    locator_table = (
        _PLANE_LOCATORS
        if operation.operation is PartDesignPatternOperation.MIRRORED
        else _AXIS_LOCATORS
    )
    matches = tuple(value for term, value in locator_table if _identity(locator) == _identity(term))
    if len(matches) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/reference/locator")
    axis = None if operation.operation is PartDesignPatternOperation.MIRRORED else matches[0]
    plane = matches[0] if operation.operation is PartDesignPatternOperation.MIRRORED else None

    parameters = {item.parameter_id: item for item in graph.parameters}
    values: dict[str, object] = {}
    parameter_specs = {
        "occurrences": (PATTERN_OCCURRENCES_ROLE_TERM, PATTERN_INTEGER_TYPE_TERM),
        "span": (PATTERN_SPAN_ROLE_TERM, PATTERN_LENGTH_TYPE_TERM),
        "angle": (PATTERN_ANGLE_ROLE_TERM, PATTERN_ANGLE_TYPE_TERM),
        "reversed": (PATTERN_REVERSED_ROLE_TERM, PATTERN_BOOLEAN_TYPE_TERM),
    }
    for kind in grouped:
        if kind not in parameter_specs:
            continue
        binding = grouped[kind][0]
        parameter = parameters.get(binding.parameter_id)
        role, value_type = parameter_specs[kind]
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
            PATTERN_CANONICAL_JSON_TERM,
            f"/graph/{kind}/encoding",
        )
        values[kind] = parameter.value.value
    if len(graph.parameters) != parameter_count or len(graph.references) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/values")
    occurrences = values.get("occurrences")
    if occurrences is not None and (
        type(occurrences) is not int or not 2 <= occurrences <= MAX_PARTDESIGN_PATTERN_OCCURRENCES
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/occurrences/value")
    reversed_value = values.get("reversed", False)
    if type(reversed_value) is not bool:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/reversed/value")
    span = values.get("span")
    angle = values.get("angle")
    for kind, value, maximum in (("span", span, 1_000_000.0), ("angle", angle, 360.0)):
        if value is None:
            continue
        if type(value) not in {int, float}:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, f"/graph/{kind}/value")
        try:
            numeric = float(value)
        except (OverflowError, TypeError, ValueError):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, f"/graph/{kind}/value")
        if not math.isfinite(numeric) or not 0.0 < numeric <= maximum:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, f"/graph/{kind}/value")
        values[kind] = numeric

    target_result = _result_with_identity(
        target,
        terms,
        role=PATTERN_SOLID_RESULT_ROLE_TERM,
        value_type=PATTERN_SOLID_TYPE_TERM,
        path="/graph/result",
    )
    selection = graph.graph_results[0]
    if (
        len(target.results) != 1
        or selection.node_id != target.node_id
        or selection.result_id != target_result.result_id
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/graph_results")
    plan = PartDesignPatternBackendPlan(
        source_artifact_id=document.artifact_id,
        source_graph_id=graph.graph_id,
        source_graph_sha256=graph.graph_sha256,
        source_content_sha256=hashlib.sha256(payload).hexdigest(),
        lowering_request_sha256=request_digest,
        adapter_contract_sha256=FREECAD_PARTDESIGN_PATTERN_ADAPTER_DESCRIPTOR.adapter_contract_sha256,
        body_id=body.body_id,
        node_id=target.node_id,
        result_id=target_result.result_id,
        operation=operation.operation,
        base=base,
        source_feature=source,
        reference_id=reference.reference_id,
        axis=axis,
        plane=plane,
        occurrences=occurrences,
        span_mm=values.get("span"),
        angle_degrees=values.get("angle"),
        reversed=reversed_value,
    )
    return plan, SubjectRef(
        artifact_id=document.artifact_id,
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id=target.node_id,
    )


def _plan_document(plan: PartDesignPatternBackendPlan) -> DocumentRef:
    payload = plan.canonical_bytes
    content_sha256 = hashlib.sha256(payload).hexdigest()
    semantic_digest = hashlib.sha256(
        _PLAN_DOCUMENT_DIGEST_DOMAIN + bytes.fromhex(plan.plan_sha256)
    ).hexdigest()
    return DocumentRef(
        artifact_id=f"artifact_freecad_partdesign_pattern_plan_{content_sha256[:32]}",
        role_term_ref_id=PATTERN_PLAN_DOCUMENT_ROLE_TERM.term_ref_id,
        schema_term_ref_id=PATTERN_PLAN_SCHEMA_TERM.term_ref_id,
        document_id=f"freecad_partdesign_pattern_plan_{semantic_digest[:32]}",
        document_digest=plan.plan_sha256,
        content_sha256=content_sha256,
        size_bytes=len(payload),
        media_type=PARTDESIGN_PATTERN_PLAN_MEDIA_TYPE,
    )


class FreeCADPartDesignPatternAdapter:
    """Single exact PFGv2-to-plan adapter for the three-operation batch."""

    __slots__ = ("_sink",)

    def __init__(self, sink: PlanSink) -> None:
        if not isinstance(sink, PlanSink):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/plan_sink")
        self._sink = sink

    @property
    def descriptor(self) -> AdapterDescriptor:
        return FREECAD_PARTDESIGN_PATTERN_ADAPTER_DESCRIPTOR

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
    ) -> tuple[BackendLoweringResult, LoweredPartDesignPatternPlanReceipt]:
        if type(request) is not BackendLoweringRequest or request.adapter != self.descriptor:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/adapter")
        if type(codecs) is not TrustedCodecRegistry:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/codecs")
        identities = {_identity(term) for term in request.terms}
        if any(_identity(term) not in identities for term in PATTERN_REQUEST_TERMS):
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
            != _identity(PATTERN_INTENT_DOCUMENT_ROLE_TERM)
            or _identity(request_terms[intent_document.schema_term_ref_id])
            != _identity(PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM)
            or intent_document.media_type != PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE
            or _identity(request_terms[capability_document.role_term_ref_id])
            != _identity(PATTERN_CAPABILITY_DOCUMENT_ROLE_TERM)
            or _identity(request_terms[capability_document.schema_term_ref_id])
            != _identity(PATTERN_CAPABILITY_SCHEMA_TERM)
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/documents")
        capability_payload = read_verified_document(
            artifacts,
            capability_document,
            maximum_bytes=request.budget.max_input_bytes - intent_document.size_bytes,
        )
        expected_capability = pattern_capability_payload()
        expected_digest = hashlib.sha256(
            _CAPABILITY_DIGEST_DOMAIN + expected_capability
        ).hexdigest()
        if (
            not hmac.compare_digest(capability_payload, expected_capability)
            or capability_document.media_type
            != "application/vnd.vibecad.freecad-partdesign-pattern-capability+json"
            or not hmac.compare_digest(capability_document.document_digest, expected_digest)
            or capability_document.document_id
            != f"freecad_partdesign_pattern_capability_{expected_digest[:32]}"
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
        except (ParametricFeatureGraphError, PartDesignPatternRuleError):
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
            request.budget.max_output_bytes, MAX_PARTDESIGN_PATTERN_PLAN_BYTES
        ):
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/plan_document")
        try:
            decode_partdesign_pattern_backend_plan(
                plan_payload,
                expected_content_sha256=plan_document.content_sha256,
                expected_plan_sha256=plan_document.document_digest,
            )
        except PartDesignPatternRuleError:
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_document")
        result = BackendLoweringResult(
            request_digest=request.request_digest,
            adapter=self.descriptor,
            disposition=BridgeDisposition.COMPLETE,
            plan_document=plan_document,
            supported_subjects=(subject,),
        )
        validate_lowering_result(request, result)
        receipt = LoweredPartDesignPatternPlanReceipt(
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
        self, receipt: LoweredPartDesignPatternPlanReceipt
    ) -> tuple[PartDesignPatternBackendPlan, bytes]:
        if (
            type(receipt) is not LoweredPartDesignPatternPlanReceipt
            or receipt.adapter != self.descriptor
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/receipt")
        document = receipt.plan_document
        if (
            document.role_term_ref_id != PATTERN_PLAN_DOCUMENT_ROLE_TERM.term_ref_id
            or document.schema_term_ref_id != PATTERN_PLAN_SCHEMA_TERM.term_ref_id
            or document.media_type != PARTDESIGN_PATTERN_PLAN_MEDIA_TYPE
            or document.size_bytes > MAX_PARTDESIGN_PATTERN_PLAN_BYTES
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/receipt/plan_document")
        try:
            payload = self._sink.read_exact(document, MAX_PARTDESIGN_PATTERN_PLAN_BYTES)
        except (Exception, SystemExit):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink")
        if (
            type(payload) is not bytes
            or len(payload) != document.size_bytes
            or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), document.content_sha256)
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan_sink/readback")
        try:
            plan = decode_partdesign_pattern_backend_plan(
                payload,
                expected_content_sha256=document.content_sha256,
                expected_plan_sha256=document.document_digest,
            )
        except PartDesignPatternRuleError:
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
    "FREECAD_PARTDESIGN_PATTERN_ADAPTER_DESCRIPTOR",
    "PATTERN_ANGLE_ROLE_TERM",
    "PATTERN_ANGLE_TYPE_TERM",
    "PATTERN_AXIS_ROLE_TERM",
    "PATTERN_BASE_ROLE_TERM",
    "PATTERN_BOOLEAN_TYPE_TERM",
    "PATTERN_CANONICAL_JSON_TERM",
    "PATTERN_CAPABILITY_DOCUMENT_ROLE_TERM",
    "PATTERN_CAPABILITY_SCHEMA_TERM",
    "PATTERN_DIRECTION_ROLE_TERM",
    "PATTERN_INTEGER_TYPE_TERM",
    "PATTERN_INTENT_DOCUMENT_ROLE_TERM",
    "PATTERN_LENGTH_TYPE_TERM",
    "PATTERN_OCCURRENCES_ROLE_TERM",
    "PATTERN_OPERATION_TERMS",
    "PATTERN_ORIGIN_AXIS_TYPE_TERM",
    "PATTERN_ORIGIN_PLANE_TYPE_TERM",
    "PATTERN_PFG_TERMS",
    "PATTERN_PLANE_ROLE_TERM",
    "PATTERN_PLAN_DOCUMENT_ROLE_TERM",
    "PATTERN_PLAN_SCHEMA_TERM",
    "PATTERN_REQUEST_TERMS",
    "PATTERN_REVERSED_ROLE_TERM",
    "PATTERN_SOLID_RESULT_ROLE_TERM",
    "PATTERN_SOLID_TYPE_TERM",
    "PATTERN_SOURCE_ROLE_TERM",
    "PATTERN_SPAN_ROLE_TERM",
    "PATTERN_STRUCTURE_TERM",
    "PATTERN_X_AXIS_LOCATOR_TERM",
    "PATTERN_XY_PLANE_LOCATOR_TERM",
    "PATTERN_XZ_PLANE_LOCATOR_TERM",
    "PATTERN_Y_AXIS_LOCATOR_TERM",
    "PATTERN_YZ_PLANE_LOCATOR_TERM",
    "PATTERN_Z_AXIS_LOCATOR_TERM",
    "FreeCADPartDesignPatternAdapter",
    "LoweredPartDesignPatternPlanReceipt",
    "build_pattern_capability_document",
    "pattern_capability_payload",
]
