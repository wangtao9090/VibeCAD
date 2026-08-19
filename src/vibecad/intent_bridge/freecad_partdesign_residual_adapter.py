"""Private reviewed lowering for three backend-neutral mechanical intents.

The accepted ontology describes a circular hole, an additive angle revolution,
and an explicitly placed local coordinate system without naming a CAD backend.
Only the static :class:`ReviewedOperationSpec` table below binds those complete
semantic identities to reviewed native rules.  PFG strings never select a
``TypeId`` or native property.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Final

from vibecad.intent_bridge.contracts import (
    AdapterDescriptor,
    BridgeTermRef,
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
    FeatureNodeV2,
    ParametricFeatureGraphV2,
    SemanticReferenceScope,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_partdesign_residual_rules import (
    MAX_PARTDESIGN_RESIDUAL_PLAN_BYTES,
    PARTDESIGN_RESIDUAL_FREECAD_ENGINE_BUILD_ID,
    PARTDESIGN_RESIDUAL_NATIVE_PROPERTIES,
    PARTDESIGN_RESIDUAL_NATIVE_TYPE_IDS,
    PARTDESIGN_RESIDUAL_PLAN_MEDIA_TYPE,
    PARTDESIGN_RESIDUAL_RULE_CONTRACT_SHA256,
    PARTDESIGN_RESIDUAL_RULE_ID,
    ExplicitPlacement,
    HoleExtent,
    PartDesignResidualBackendPlan,
    PartDesignResidualOperation,
    RevolutionAxis,
    SemanticObjectSelection,
    decode_partdesign_residual_backend_plan,
)

_ONTOLOGY_NAMESPACE = "org.vibecad.parametric-mechanical"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DOMAIN = b"vibecad.parametric-mechanical-ontology.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.partdesign-residual-adapter.v1\0"
_MANIFEST_BUILD_ID: Final = hashlib.sha256(
    b"FreeCAD-build\0" + PARTDESIGN_RESIDUAL_FREECAD_ENGINE_BUILD_ID.encode("ascii")
).hexdigest()


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


def _as_bridge(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


RESIDUAL_INTENT_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_residual_parametric_intent", "document-role.parametric-intent"
)
RESIDUAL_CAPABILITY_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_residual_capability", "document-role.reviewed-mechanical-capability"
)
RESIDUAL_CAPABILITY_SCHEMA_TERM: Final = _bridge_term(
    "schema_residual_capability_v1", "document-schema.reviewed-mechanical-capability-v1"
)
RESIDUAL_PLAN_DOCUMENT_ROLE_TERM: Final = _bridge_term(
    "role_residual_backend_plan", "document-role.reviewed-backend-plan"
)
RESIDUAL_PLAN_SCHEMA_TERM: Final = _bridge_term(
    "schema_residual_plan_v1", "document-schema.reviewed-partdesign-residual-plan-v1"
)

RESIDUAL_STRUCTURE_TERM: Final = _pfg_term(
    "structure_body_feature", "structure.body-feature"
)
RESIDUAL_HOLE_FAMILY_TERM: Final = _pfg_term(
    "family_circular_hole", "feature-family.circular-hole"
)
RESIDUAL_REVOLUTION_FAMILY_TERM: Final = _pfg_term(
    "family_additive_revolution", "feature-family.additive-revolution"
)
RESIDUAL_REFERENCE_FAMILY_TERM: Final = _pfg_term(
    "family_local_reference", "feature-family.local-reference"
)
RESIDUAL_HOLE_OPERATION_TERM: Final = _pfg_term(
    "operation_circular_hole", "operation.remove-circular-hole"
)
RESIDUAL_REVOLUTION_OPERATION_TERM: Final = _pfg_term(
    "operation_additive_revolution_angle", "operation.additive-revolution-angle"
)
RESIDUAL_COORDINATE_SYSTEM_OPERATION_TERM: Final = _pfg_term(
    "operation_local_coordinate_system", "operation.local-coordinate-system"
)

RESIDUAL_BASE_ROLE_TERM: Final = _pfg_term("role_base_solid", "input-role.base-solid")
RESIDUAL_PROFILE_ROLE_TERM: Final = _pfg_term(
    "role_profile", "input-role.planar-profile"
)
RESIDUAL_AXIS_ROLE_TERM: Final = _pfg_term("role_profile_axis", "input-role.profile-axis")
RESIDUAL_EXTENT_ROLE_TERM: Final = _pfg_term("role_hole_extent", "input-role.hole-extent")
RESIDUAL_DIAMETER_ROLE_TERM: Final = _pfg_term(
    "role_hole_diameter", "input-role.hole-diameter-mm"
)
RESIDUAL_DEPTH_ROLE_TERM: Final = _pfg_term("role_hole_depth", "input-role.hole-depth-mm")
RESIDUAL_ANGLE_ROLE_TERM: Final = _pfg_term(
    "role_revolution_angle", "input-role.revolution-angle-degrees"
)
RESIDUAL_PLACEMENT_ROLE_TERM: Final = _pfg_term(
    "role_explicit_placement", "input-role.explicit-placement"
)

RESIDUAL_SOLID_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_solid", "result-role.solid"
)
RESIDUAL_CIRCULAR_PROFILE_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_circular_profile", "result-role.circular-profile"
)
RESIDUAL_CLOSED_PROFILE_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_closed_profile", "result-role.closed-planar-profile"
)
RESIDUAL_AXIS_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_profile_axis", "result-role.profile-axis"
)
RESIDUAL_COORDINATE_SYSTEM_RESULT_ROLE_TERM: Final = _pfg_term(
    "role_result_coordinate_system", "result-role.local-coordinate-system"
)

RESIDUAL_SOLID_TYPE_TERM: Final = _pfg_term("type_solid", "value-type.solid")
RESIDUAL_CIRCULAR_PROFILE_TYPE_TERM: Final = _pfg_term(
    "type_circular_profile", "value-type.single-circle-planar-profile"
)
RESIDUAL_CLOSED_PROFILE_TYPE_TERM: Final = _pfg_term(
    "type_closed_profile", "value-type.closed-planar-profile"
)
RESIDUAL_AXIS_TYPE_TERM: Final = _pfg_term("type_profile_axis", "value-type.profile-axis")
RESIDUAL_EXTENT_TYPE_TERM: Final = _pfg_term(
    "type_hole_extent", "value-type.hole-extent"
)
RESIDUAL_LENGTH_TYPE_TERM: Final = _pfg_term("type_length_mm", "value-type.length-mm")
RESIDUAL_ANGLE_TYPE_TERM: Final = _pfg_term(
    "type_angle_degrees", "value-type.angle-degrees"
)
RESIDUAL_PLACEMENT_TYPE_TERM: Final = _pfg_term(
    "type_explicit_placement", "value-type.explicit-placement"
)
RESIDUAL_COORDINATE_SYSTEM_TYPE_TERM: Final = _pfg_term(
    "type_coordinate_system", "value-type.local-coordinate-system"
)
RESIDUAL_CANONICAL_JSON_TERM: Final = _pfg_term(
    "encoding_canonical_json", "value-encoding.canonical-json"
)
RESIDUAL_HORIZONTAL_AXIS_LOCATOR_TERM: Final = _pfg_term(
    "locator_profile_horizontal_axis", "reference-locator.profile-horizontal-axis"
)
RESIDUAL_VERTICAL_AXIS_LOCATOR_TERM: Final = _pfg_term(
    "locator_profile_vertical_axis", "reference-locator.profile-vertical-axis"
)


@dataclass(frozen=True, slots=True)
class _OperationTerms:
    operation: PartDesignResidualOperation
    family: SemanticTermRefV2
    operation_term: SemanticTermRefV2
    result_role: SemanticTermRefV2
    result_type: SemanticTermRefV2


RESIDUAL_OPERATION_TERMS: Final = (
    _OperationTerms(
        PartDesignResidualOperation.HOLE,
        RESIDUAL_HOLE_FAMILY_TERM,
        RESIDUAL_HOLE_OPERATION_TERM,
        RESIDUAL_SOLID_RESULT_ROLE_TERM,
        RESIDUAL_SOLID_TYPE_TERM,
    ),
    _OperationTerms(
        PartDesignResidualOperation.REVOLUTION,
        RESIDUAL_REVOLUTION_FAMILY_TERM,
        RESIDUAL_REVOLUTION_OPERATION_TERM,
        RESIDUAL_SOLID_RESULT_ROLE_TERM,
        RESIDUAL_SOLID_TYPE_TERM,
    ),
    _OperationTerms(
        PartDesignResidualOperation.COORDINATE_SYSTEM,
        RESIDUAL_REFERENCE_FAMILY_TERM,
        RESIDUAL_COORDINATE_SYSTEM_OPERATION_TERM,
        RESIDUAL_COORDINATE_SYSTEM_RESULT_ROLE_TERM,
        RESIDUAL_COORDINATE_SYSTEM_TYPE_TERM,
    ),
)

RESIDUAL_PFG_TERMS: Final = (
    RESIDUAL_STRUCTURE_TERM,
    RESIDUAL_HOLE_FAMILY_TERM,
    RESIDUAL_REVOLUTION_FAMILY_TERM,
    RESIDUAL_REFERENCE_FAMILY_TERM,
    RESIDUAL_HOLE_OPERATION_TERM,
    RESIDUAL_REVOLUTION_OPERATION_TERM,
    RESIDUAL_COORDINATE_SYSTEM_OPERATION_TERM,
    RESIDUAL_BASE_ROLE_TERM,
    RESIDUAL_PROFILE_ROLE_TERM,
    RESIDUAL_AXIS_ROLE_TERM,
    RESIDUAL_EXTENT_ROLE_TERM,
    RESIDUAL_DIAMETER_ROLE_TERM,
    RESIDUAL_DEPTH_ROLE_TERM,
    RESIDUAL_ANGLE_ROLE_TERM,
    RESIDUAL_PLACEMENT_ROLE_TERM,
    RESIDUAL_SOLID_RESULT_ROLE_TERM,
    RESIDUAL_CIRCULAR_PROFILE_RESULT_ROLE_TERM,
    RESIDUAL_CLOSED_PROFILE_RESULT_ROLE_TERM,
    RESIDUAL_AXIS_RESULT_ROLE_TERM,
    RESIDUAL_COORDINATE_SYSTEM_RESULT_ROLE_TERM,
    RESIDUAL_SOLID_TYPE_TERM,
    RESIDUAL_CIRCULAR_PROFILE_TYPE_TERM,
    RESIDUAL_CLOSED_PROFILE_TYPE_TERM,
    RESIDUAL_AXIS_TYPE_TERM,
    RESIDUAL_EXTENT_TYPE_TERM,
    RESIDUAL_LENGTH_TYPE_TERM,
    RESIDUAL_ANGLE_TYPE_TERM,
    RESIDUAL_PLACEMENT_TYPE_TERM,
    RESIDUAL_COORDINATE_SYSTEM_TYPE_TERM,
    RESIDUAL_CANONICAL_JSON_TERM,
    RESIDUAL_HORIZONTAL_AXIS_LOCATOR_TERM,
    RESIDUAL_VERTICAL_AXIS_LOCATOR_TERM,
)

_ADAPTER_CONTRACT_SHA256: Final = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            PARTDESIGN_RESIDUAL_RULE_ID.encode("ascii"),
            PARTDESIGN_RESIDUAL_RULE_CONTRACT_SHA256.encode("ascii"),
            b"canonical-pfg-v2;full-semantic-identity;shared-reviewed-family-v1;no-authority",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in (
                    RESIDUAL_INTENT_DOCUMENT_ROLE_TERM,
                    RESIDUAL_CAPABILITY_DOCUMENT_ROLE_TERM,
                    RESIDUAL_CAPABILITY_SCHEMA_TERM,
                    RESIDUAL_PLAN_DOCUMENT_ROLE_TERM,
                    RESIDUAL_PLAN_SCHEMA_TERM,
                    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
                    PFG_SELECTOR_FEATURE_NODE,
                    *(_as_bridge(term) for term in RESIDUAL_PFG_TERMS),
                )
            ),
        )
    )
).hexdigest()

FREECAD_PARTDESIGN_RESIDUAL_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_partdesign_residual_adapter",
    adapter_version="1.0.0",
    adapter_contract_sha256=_ADAPTER_CONTRACT_SHA256,
)

RESIDUAL_OPERATION_SPECS: Final = tuple(
    ReviewedOperationSpec(
        operation_id=terms.operation.value,
        semantic_term=_as_bridge(terms.operation_term),
        native_type_id=PARTDESIGN_RESIDUAL_NATIVE_TYPE_IDS[terms.operation],
        native_operation={
            PartDesignResidualOperation.HOLE: "Hole",
            PartDesignResidualOperation.REVOLUTION: "Revolution",
            PartDesignResidualOperation.COORDINATE_SYSTEM: "CoordinateSystem",
        }[terms.operation],
        native_property_names=PARTDESIGN_RESIDUAL_NATIVE_PROPERTIES[terms.operation],
    )
    for terms in RESIDUAL_OPERATION_TERMS
)

RESIDUAL_REQUEST_TERMS: Final = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    RESIDUAL_INTENT_DOCUMENT_ROLE_TERM,
    RESIDUAL_CAPABILITY_DOCUMENT_ROLE_TERM,
    RESIDUAL_CAPABILITY_SCHEMA_TERM,
    RESIDUAL_PLAN_DOCUMENT_ROLE_TERM,
    RESIDUAL_PLAN_SCHEMA_TERM,
    *(_as_bridge(term) for term in RESIDUAL_PFG_TERMS),
)

PARTDESIGN_RESIDUAL_MANIFEST: Final = FamilyBatchManifest(
    family_id="partdesign_residual",
    family_version="1.0.0",
    adapter=FREECAD_PARTDESIGN_RESIDUAL_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=_MANIFEST_BUILD_ID,
    rule_id=PARTDESIGN_RESIDUAL_RULE_ID,
    rule_contract_sha256=PARTDESIGN_RESIDUAL_RULE_CONTRACT_SHA256,
    intent_role_term=RESIDUAL_INTENT_DOCUMENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=RESIDUAL_CAPABILITY_DOCUMENT_ROLE_TERM,
    capability_schema_term=RESIDUAL_CAPABILITY_SCHEMA_TERM,
    capability_media_type="application/vnd.vibecad.partdesign-residual-capability+json",
    plan_role_term=RESIDUAL_PLAN_DOCUMENT_ROLE_TERM,
    plan_schema_term=RESIDUAL_PLAN_SCHEMA_TERM,
    plan_media_type=PARTDESIGN_RESIDUAL_PLAN_MEDIA_TYPE,
    request_terms=RESIDUAL_REQUEST_TERMS,
    operations=RESIDUAL_OPERATION_SPECS,
    max_plan_bytes=MAX_PARTDESIGN_RESIDUAL_PLAN_BYTES,
)


def build_partdesign_residual_capability_document():
    """Return the exact content-addressed capability manifest document."""

    return PARTDESIGN_RESIDUAL_MANIFEST.capability_document(
        artifact_id="artifact_freecad_partdesign_residual_capability"
    )


def _fail(path: str) -> None:
    raise IntentBridgeError(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)


def _identity(term: object) -> tuple[str, str, str, str]:
    try:
        return (
            term.namespace,
            term.vocabulary_version,
            term.term_id,
            term.term_definition_sha256,
        )
    except Exception:
        _fail("/graph/terms")


def _graph_term(
    terms: dict[str, SemanticTermRefV2],
    term_ref_id: str,
    expected: SemanticTermRefV2,
    path: str,
) -> None:
    actual = terms.get(term_ref_id)
    if actual is None or _identity(actual) != _identity(expected):
        _fail(path)


def _result(
    node: FeatureNodeV2,
    terms: dict[str, SemanticTermRefV2],
    *,
    role: SemanticTermRefV2,
    value_type: SemanticTermRefV2,
    path: str,
):
    matches = tuple(
        item
        for item in node.results
        if terms.get(item.semantic_role_term_ref_id) is not None
        and terms.get(item.value_type_term_ref_id) is not None
        and _identity(terms[item.semantic_role_term_ref_id]) == _identity(role)
        and _identity(terms[item.value_type_term_ref_id]) == _identity(value_type)
    )
    if len(matches) != 1:
        _fail(path)
    return matches[0]


def _assert_closed_graph(graph: ParametricFeatureGraphV2) -> None:
    if graph.extensions or any(item.extension_ids for item in graph.bodies):
        _fail("/graph/extensions")
    if any(
        item.extension_ids or item.value.extension_ids or item.expression is not None
        for item in graph.parameters
    ):
        _fail("/graph/parameters")
    if any(
        item.extension_ids or item.occurrence_path or item.qualifier_term_ref_ids
        for item in graph.references
    ):
        _fail("/graph/references")
    if any(
        node.extension_ids
        or node.intent.extension_ids
        or any(port.extension_ids for port in node.intent.input_ports)
        or any(result.extension_ids for result in node.results)
        for node in graph.nodes
    ):
        _fail("/graph/nodes")


def _operation_for_target(
    target: FeatureNodeV2, terms: dict[str, SemanticTermRefV2]
) -> _OperationTerms | None:
    structural = terms.get(target.intent.structural_kind_term_ref_id)
    family = terms.get(target.intent.family_term_ref_id)
    operation = terms.get(target.intent.operation_term_ref_id)
    if structural is None or _identity(structural) != _identity(RESIDUAL_STRUCTURE_TERM):
        return None
    matches = tuple(
        item
        for item in RESIDUAL_OPERATION_TERMS
        if family is not None
        and operation is not None
        and _identity(family) == _identity(item.family)
        and _identity(operation) == _identity(item.operation_term)
    )
    return matches[0] if len(matches) == 1 else None


def _selection(
    node: FeatureNodeV2,
    terms: dict[str, SemanticTermRefV2],
    *,
    role: SemanticTermRefV2,
    value_type: SemanticTermRefV2,
    result_id: str,
    result_count: int,
    path: str,
) -> SemanticObjectSelection:
    result = _result(node, terms, role=role, value_type=value_type, path=path)
    if result.result_id != result_id or len(node.results) != result_count:
        _fail(path)
    return SemanticObjectSelection(node_id=node.node_id, result_id=result.result_id)


def _parameter_value(
    parameter: object,
    terms: dict[str, SemanticTermRefV2],
    *,
    role: SemanticTermRefV2,
    value_type: SemanticTermRefV2,
    path: str,
) -> object:
    if parameter is None or parameter.expression is not None:
        _fail(path)
    _graph_term(terms, parameter.semantic_role_term_ref_id, role, f"{path}/role")
    _graph_term(
        terms,
        parameter.value.value_type_term_ref_id,
        value_type,
        f"{path}/type",
    )
    _graph_term(
        terms,
        parameter.value.encoding_term_ref_id,
        RESIDUAL_CANONICAL_JSON_TERM,
        f"{path}/encoding",
    )
    return parameter.value.value


def _numeric(value: object, path: str, *, minimum: float, maximum: float) -> float:
    if type(value) not in {int, float}:
        _fail(path)
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        _fail(path)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        _fail(path)
    return result


def _port_contract(operation: PartDesignResidualOperation):
    common = {
        "base": (RESIDUAL_BASE_ROLE_TERM, RESIDUAL_SOLID_TYPE_TERM, 1, 1, "dependency"),
        "profile": (
            RESIDUAL_PROFILE_ROLE_TERM,
            RESIDUAL_CIRCULAR_PROFILE_TYPE_TERM,
            1,
            1,
            "dependency",
        ),
    }
    if operation is PartDesignResidualOperation.HOLE:
        return {
            **common,
            "extent": (
                RESIDUAL_EXTENT_ROLE_TERM,
                RESIDUAL_EXTENT_TYPE_TERM,
                1,
                1,
                "parameter",
            ),
            "diameter": (
                RESIDUAL_DIAMETER_ROLE_TERM,
                RESIDUAL_LENGTH_TYPE_TERM,
                1,
                1,
                "parameter",
            ),
            "depth": (
                RESIDUAL_DEPTH_ROLE_TERM,
                RESIDUAL_LENGTH_TYPE_TERM,
                0,
                1,
                "parameter",
            ),
        }
    if operation is PartDesignResidualOperation.REVOLUTION:
        return {
            "base": (
                RESIDUAL_BASE_ROLE_TERM,
                RESIDUAL_SOLID_TYPE_TERM,
                0,
                1,
                "dependency",
            ),
            "profile": (
                RESIDUAL_PROFILE_ROLE_TERM,
                RESIDUAL_CLOSED_PROFILE_TYPE_TERM,
                1,
                1,
                "dependency",
            ),
            "axis": (
                RESIDUAL_AXIS_ROLE_TERM,
                RESIDUAL_AXIS_TYPE_TERM,
                1,
                1,
                "reference",
            ),
            "angle": (
                RESIDUAL_ANGLE_ROLE_TERM,
                RESIDUAL_ANGLE_TYPE_TERM,
                1,
                1,
                "parameter",
            ),
        }
    return {
        "placement": (
            RESIDUAL_PLACEMENT_ROLE_TERM,
            RESIDUAL_PLACEMENT_TYPE_TERM,
            1,
            1,
            "parameter",
        )
    }


def _build_plan(
    document,
    payload: bytes,
    request_digest: str,
    manifest: FamilyBatchManifest,
) -> ReviewedPlanDraft:
    try:
        graph = decode_parametric_feature_graph_v2(
            payload, expected_sha256=document.document_digest
        )
    except Exception:
        _fail("/graph")
    if (
        graph.graph_id != document.document_id
        or len(graph.bodies) != 1
        or len(graph.graph_results) != 1
    ):
        _fail("/graph/scope")
    _assert_closed_graph(graph)
    terms = {item.term_ref_id: item for item in graph.terms}
    if any(
        sum(_identity(item) == _identity(expected) for item in graph.terms) != 1
        for expected in RESIDUAL_PFG_TERMS
    ):
        _fail("/graph/terms")
    candidates = tuple(
        (node, operation)
        for node in graph.nodes
        if (operation := _operation_for_target(node, terms)) is not None
    )
    if len(candidates) != 1:
        _fail("/graph/target")
    target, operation_terms = candidates[0]
    operation = operation_terms.operation
    body = graph.bodies[0]
    if target.body_id != body.body_id:
        _fail("/graph/body")

    expected_ports = _port_contract(operation)
    if len(target.intent.input_ports) != len(expected_ports):
        _fail("/graph/input_ports")
    port_kinds: dict[str, tuple[str, str]] = {}
    for port in target.intent.input_ports:
        role = terms.get(port.semantic_role_term_ref_id)
        value_type = terms.get(port.value_type_term_ref_id)
        matches = tuple(
            (kind, contract)
            for kind, contract in expected_ports.items()
            if role is not None
            and value_type is not None
            and _identity(role) == _identity(contract[0])
            and _identity(value_type) == _identity(contract[1])
        )
        if (
            len(matches) != 1
            or port.minimum_cardinality != matches[0][1][2]
            or port.maximum_cardinality != matches[0][1][3]
            or port.ordered
            or matches[0][0] in (item[0] for item in port_kinds.values())
        ):
            _fail("/graph/input_ports")
        port_kinds[port.port_id] = (matches[0][0], matches[0][1][4])

    grouped: dict[str, list[object]] = {kind: [] for kind in expected_ports}
    for binding, binding_type in (
        *((item, "dependency") for item in target.intent.dependencies),
        *((item, "reference") for item in target.intent.references),
        *((item, "parameter") for item in target.intent.parameter_bindings),
    ):
        port = port_kinds.get(binding.port_id)
        if port is None or port[1] != binding_type:
            _fail("/graph/bindings")
        grouped[port[0]].append(binding)
    if any(
        not contract[2] <= len(grouped[kind]) <= contract[3]
        for kind, contract in expected_ports.items()
    ):
        _fail("/graph/bindings")

    nodes = {item.node_id: item for item in graph.nodes}

    def dependency(
        kind: str,
        role: SemanticTermRefV2,
        value_type: SemanticTermRefV2,
        *,
        result_count: int,
    ) -> SemanticObjectSelection | None:
        if not grouped[kind]:
            return None
        item = grouped[kind][0]
        node = nodes.get(item.upstream_node_id)
        if node is None or node.body_id != body.body_id or node.node_id == target.node_id:
            _fail(f"/graph/{kind}")
        return _selection(
            node,
            terms,
            role=role,
            value_type=value_type,
            result_id=item.upstream_result_id,
            result_count=result_count,
            path=f"/graph/{kind}",
        )

    base = None
    profile = None
    axis_reference_id = None
    axis_result_id = None
    hole_extent = None
    diameter = depth = angle = None
    revolution_axis = None
    placement = None
    if operation is PartDesignResidualOperation.HOLE:
        base = dependency(
            "base",
            RESIDUAL_SOLID_RESULT_ROLE_TERM,
            RESIDUAL_SOLID_TYPE_TERM,
            result_count=1,
        )
        profile = dependency(
            "profile",
            RESIDUAL_CIRCULAR_PROFILE_RESULT_ROLE_TERM,
            RESIDUAL_CIRCULAR_PROFILE_TYPE_TERM,
            result_count=1,
        )
    elif operation is PartDesignResidualOperation.REVOLUTION:
        base = dependency(
            "base",
            RESIDUAL_SOLID_RESULT_ROLE_TERM,
            RESIDUAL_SOLID_TYPE_TERM,
            result_count=1,
        )
        profile = dependency(
            "profile",
            RESIDUAL_CLOSED_PROFILE_RESULT_ROLE_TERM,
            RESIDUAL_CLOSED_PROFILE_TYPE_TERM,
            result_count=2,
        )

    parameters = {item.parameter_id: item for item in graph.parameters}

    def parameter(kind: str, role: SemanticTermRefV2, value_type: SemanticTermRefV2):
        if not grouped[kind]:
            return None
        return _parameter_value(
            parameters.get(grouped[kind][0].parameter_id),
            terms,
            role=role,
            value_type=value_type,
            path=f"/graph/{kind}",
        )

    if operation is PartDesignResidualOperation.HOLE:
        extent_value = parameter("extent", RESIDUAL_EXTENT_ROLE_TERM, RESIDUAL_EXTENT_TYPE_TERM)
        try:
            hole_extent = HoleExtent(extent_value)
        except (TypeError, ValueError):
            _fail("/graph/extent/value")
        diameter = _numeric(
            parameter("diameter", RESIDUAL_DIAMETER_ROLE_TERM, RESIDUAL_LENGTH_TYPE_TERM),
            "/graph/diameter/value",
            minimum=0.01,
            maximum=1e6,
        )
        depth_value = parameter("depth", RESIDUAL_DEPTH_ROLE_TERM, RESIDUAL_LENGTH_TYPE_TERM)
        if hole_extent is HoleExtent.DIMENSION:
            if depth_value is None:
                _fail("/graph/depth")
            depth = _numeric(
                depth_value, "/graph/depth/value", minimum=0.01, maximum=1e6
            )
        elif depth_value is not None:
            _fail("/graph/depth")
    elif operation is PartDesignResidualOperation.REVOLUTION:
        reference_binding = grouped["axis"][0]
        reference = {item.reference_id: item for item in graph.references}.get(
            reference_binding.reference_id
        )
        if (
            reference is None
            or reference.scope is not SemanticReferenceScope.FEATURE
            or reference.source_node_id != profile.node_id
        ):
            _fail("/graph/axis")
        _graph_term(
            terms,
            reference.semantic_role_term_ref_id,
            RESIDUAL_AXIS_ROLE_TERM,
            "/graph/axis/role",
        )
        _graph_term(
            terms,
            reference.value_type_term_ref_id,
            RESIDUAL_AXIS_TYPE_TERM,
            "/graph/axis/type",
        )
        source = nodes[profile.node_id]
        axis_result = _result(
            source,
            terms,
            role=RESIDUAL_AXIS_RESULT_ROLE_TERM,
            value_type=RESIDUAL_AXIS_TYPE_TERM,
            path="/graph/axis/result",
        )
        if reference.source_geometry_id != axis_result.result_id:
            _fail("/graph/axis/result")
        locator = terms.get(reference.locator_term_ref_id)
        if locator is not None and _identity(locator) == _identity(
            RESIDUAL_HORIZONTAL_AXIS_LOCATOR_TERM
        ):
            revolution_axis = RevolutionAxis.HORIZONTAL
        elif locator is not None and _identity(locator) == _identity(
            RESIDUAL_VERTICAL_AXIS_LOCATOR_TERM
        ):
            revolution_axis = RevolutionAxis.VERTICAL
        else:
            _fail("/graph/axis/locator")
        axis_reference_id = reference.reference_id
        axis_result_id = axis_result.result_id
        angle = _numeric(
            parameter("angle", RESIDUAL_ANGLE_ROLE_TERM, RESIDUAL_ANGLE_TYPE_TERM),
            "/graph/angle/value",
            minimum=1e-6,
            maximum=360.0,
        )
    else:
        placement_value = parameter(
            "placement", RESIDUAL_PLACEMENT_ROLE_TERM, RESIDUAL_PLACEMENT_TYPE_TERM
        )
        if type(placement_value) is not dict or set(placement_value) != {
            "position_mm",
            "axis",
            "angle_degrees",
        }:
            _fail("/graph/placement/value")
        try:
            placement = ExplicitPlacement.from_mapping(
                placement_value, "/graph/placement/value"
            )
        except Exception:
            _fail("/graph/placement/value")

    consumed_nodes = {target.node_id}
    consumed_nodes.update(item.node_id for item in (base, profile) if item is not None)
    expected_reference_count = 1 if operation is PartDesignResidualOperation.REVOLUTION else 0
    expected_parameter_count = {
        PartDesignResidualOperation.HOLE: 3 if hole_extent is HoleExtent.DIMENSION else 2,
        PartDesignResidualOperation.REVOLUTION: 1,
        PartDesignResidualOperation.COORDINATE_SYSTEM: 1,
    }[operation]
    if (
        set(nodes) != consumed_nodes
        or len(graph.references) != expected_reference_count
        or len(graph.parameters) != expected_parameter_count
    ):
        _fail("/graph/scope")

    target_result = _result(
        target,
        terms,
        role=operation_terms.result_role,
        value_type=operation_terms.result_type,
        path="/graph/result",
    )
    graph_result = graph.graph_results[0]
    if (
        len(target.results) != 1
        or graph_result.node_id != target.node_id
        or graph_result.result_id != target_result.result_id
    ):
        _fail("/graph/graph_results")

    plan = PartDesignResidualBackendPlan(
        source_artifact_id=document.artifact_id,
        source_graph_id=graph.graph_id,
        source_graph_sha256=graph.graph_sha256,
        source_content_sha256=hashlib.sha256(payload).hexdigest(),
        lowering_request_sha256=request_digest,
        adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
        manifest_sha256=manifest.manifest_sha256,
        body_id=body.body_id,
        node_id=target.node_id,
        result_id=target_result.result_id,
        operation=operation,
        base=base,
        profile=profile,
        axis_reference_id=axis_reference_id,
        axis_result_id=axis_result_id,
        hole_extent=hole_extent,
        diameter_mm=diameter,
        depth_mm=depth,
        revolution_axis=revolution_axis,
        angle_degrees=angle,
        placement=placement,
    )
    subject = SubjectRef(
        artifact_id=document.artifact_id,
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id=target.node_id,
    )
    return ReviewedPlanDraft(
        payload=plan.canonical_bytes,
        semantic_plan_sha256=plan.plan_sha256,
        operation_term=_as_bridge(operation_terms.operation_term),
        subjects=(subject,),
    )


def _validate_binding(
    decoded: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    if type(decoded) is not PartDesignResidualBackendPlan:
        _fail("/plan_document/type")
    expected = next(
        item for item in RESIDUAL_OPERATION_TERMS if item.operation is decoded.operation
    )
    if (
        receipt.manifest_sha256 != decoded.manifest_sha256
        or receipt.request_digest != decoded.lowering_request_sha256
        or receipt.adapter.adapter_contract_sha256 != decoded.adapter_contract_sha256
        or receipt.source_document.artifact_id != decoded.source_artifact_id
        or receipt.source_document.document_id != decoded.source_graph_id
        or receipt.source_document.document_digest != decoded.source_graph_sha256
        or receipt.source_document.content_sha256 != decoded.source_content_sha256
        or receipt.plan_document.document_digest != decoded.plan_sha256
        or operation.operation_id != decoded.operation.value
        or operation.semantic_term != _as_bridge(expected.operation_term)
        or operation.native_type_id != PARTDESIGN_RESIDUAL_NATIVE_TYPE_IDS[decoded.operation]
        or operation.native_operation
        != {
            PartDesignResidualOperation.HOLE: "Hole",
            PartDesignResidualOperation.REVOLUTION: "Revolution",
            PartDesignResidualOperation.COORDINATE_SYSTEM: "CoordinateSystem",
        }[decoded.operation]
        or operation.native_property_names
        != tuple(sorted(PARTDESIGN_RESIDUAL_NATIVE_PROPERTIES[decoded.operation]))
    ):
        _fail("/plan_document/binding")


class FreeCADPartDesignResidualAdapter(ExactReviewedFamilyAdapter):
    """Shared exact adapter specialized only by this reviewed family table."""

    def __init__(self, sink: PlanSink) -> None:
        super().__init__(
            PARTDESIGN_RESIDUAL_MANIFEST,
            sink,
            build_plan=_build_plan,
            decode_plan=decode_partdesign_residual_backend_plan,
            validate_binding=_validate_binding,
        )


__all__ = [
    "FREECAD_PARTDESIGN_RESIDUAL_ADAPTER_DESCRIPTOR",
    "PARTDESIGN_RESIDUAL_MANIFEST",
    "RESIDUAL_ANGLE_ROLE_TERM",
    "RESIDUAL_ANGLE_TYPE_TERM",
    "RESIDUAL_AXIS_RESULT_ROLE_TERM",
    "RESIDUAL_AXIS_ROLE_TERM",
    "RESIDUAL_AXIS_TYPE_TERM",
    "RESIDUAL_BASE_ROLE_TERM",
    "RESIDUAL_CANONICAL_JSON_TERM",
    "RESIDUAL_CIRCULAR_PROFILE_RESULT_ROLE_TERM",
    "RESIDUAL_CIRCULAR_PROFILE_TYPE_TERM",
    "RESIDUAL_CLOSED_PROFILE_RESULT_ROLE_TERM",
    "RESIDUAL_CLOSED_PROFILE_TYPE_TERM",
    "RESIDUAL_COORDINATE_SYSTEM_OPERATION_TERM",
    "RESIDUAL_COORDINATE_SYSTEM_RESULT_ROLE_TERM",
    "RESIDUAL_COORDINATE_SYSTEM_TYPE_TERM",
    "RESIDUAL_DEPTH_ROLE_TERM",
    "RESIDUAL_DIAMETER_ROLE_TERM",
    "RESIDUAL_EXTENT_ROLE_TERM",
    "RESIDUAL_EXTENT_TYPE_TERM",
    "RESIDUAL_HOLE_FAMILY_TERM",
    "RESIDUAL_HOLE_OPERATION_TERM",
    "RESIDUAL_HORIZONTAL_AXIS_LOCATOR_TERM",
    "RESIDUAL_LENGTH_TYPE_TERM",
    "RESIDUAL_OPERATION_SPECS",
    "RESIDUAL_PFG_TERMS",
    "RESIDUAL_PLACEMENT_ROLE_TERM",
    "RESIDUAL_PLACEMENT_TYPE_TERM",
    "RESIDUAL_PROFILE_ROLE_TERM",
    "RESIDUAL_REFERENCE_FAMILY_TERM",
    "RESIDUAL_REQUEST_TERMS",
    "RESIDUAL_REVOLUTION_FAMILY_TERM",
    "RESIDUAL_REVOLUTION_OPERATION_TERM",
    "RESIDUAL_SOLID_RESULT_ROLE_TERM",
    "RESIDUAL_SOLID_TYPE_TERM",
    "RESIDUAL_STRUCTURE_TERM",
    "RESIDUAL_VERTICAL_AXIS_LOCATOR_TERM",
    "FreeCADPartDesignResidualAdapter",
    "build_partdesign_residual_capability_document",
]
