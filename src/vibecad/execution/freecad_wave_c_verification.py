"""Private managed-runtime verification for reviewed Sketch and App families.

The reviewed family manifests freeze the semantic/native mapping, but they do
not by themselves prove behaviour in a managed FreeCAD process.  This module
owns a closed set of real fixtures for the twenty reviewed Sketch operations
and ten reviewed App operations.  Every operation is exercised once and its
evidence is bound to the seven canonical conformance facets before the generic
verification layer emits a managed receipt and promotion-verification binding.

There is no caller-supplied result or executor seam.  The only input is the
authenticated FreeCAD module.  Receipts remain ephemeral, grant no execution
authority, and are never applied to the production capability catalog here.
"""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from vibecad.execution import freecad_reviewed_verification as verification
from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
)
from vibecad.execution.freecad_capability_projection_v2 import (
    FreeCadPromotionVerificationBinding,
)
from vibecad.execution.freecad_reviewed_verification import (
    REVIEWED_VERIFICATION_SCHEMA_VERSION,
    ReviewedConformanceCase,
    ReviewedConformanceCaseManifest,
    ReviewedConformanceFacet,
    ReviewedVerificationReceipt,
    build_managed_freecad_conformance_host,
    build_promotion_verification_binding,
    build_reviewed_verification_receipt,
)
from vibecad.intent_bridge.freecad_app_family_adapter import APP_FAMILY_MANIFEST
from vibecad.intent_bridge.freecad_sketch_intent_adapter import (
    REVIEWED_SKETCH_FAMILY_MANIFEST,
)
from vibecad.intent_bridge.reviewed_family_engine import FamilyBatchManifest
from vibecad.parametric import freecad_app_family_rules as app_rules
from vibecad.parametric import freecad_sketch_intent_rules as sketch_rules
from vibecad.parametric.freecad_app_family_rules import (
    APP_FAMILY_NATIVE_TYPE_IDS,
    APP_FAMILY_RELATION_KINDS,
    AppFamilyBackendPlan,
    AppFamilyExecutionBindings,
    AppFamilyOperation,
    AppFamilyRelationKind,
    AppFamilyRuleError,
    AppFamilyRuleErrorCode,
    apply_app_family_plan,
    decode_app_family_backend_plan,
    encode_app_family_configuration,
)
from vibecad.parametric.freecad_sketch_intent_rules import (
    REVIEWED_SKETCH_NATIVE_TYPE_ID,
    ReviewedSketchBackendPlan,
    ReviewedSketchExecutionBindings,
    ReviewedSketchOperation,
    ReviewedSketchParameter,
    ReviewedSketchReference,
    ReviewedSketchResult,
    ReviewedSketchRuleError,
    ReviewedSketchRuleErrorCode,
    apply_reviewed_sketch_plan,
    decode_reviewed_sketch_backend_plan,
    reviewed_sketch_node_sha256,
)

WAVE_C_VERIFIER_ID: Final = "vcad.managed.freecad.wave-c-conformance"
WAVE_C_VERIFIER_VERSION: Final = "1.0.0"

_CASE_CONTRACT_DOMAIN = b"vibecad-reviewed-wave-c-case-contract-v1\0"
_FIXTURE_DIGEST_DOMAIN = b"vibecad-reviewed-wave-c-fixture-v1\0"
_HARNESS_CONTRACT_DOMAIN = b"vibecad-reviewed-wave-c-harness-contract-v1\0"
_OBSERVATION_DOMAIN = b"vibecad-reviewed-wave-c-observation-v1\0"
_VERIFICATION_LOCK = threading.Lock()

_FACET_CONTRACTS: Final = {
    ReviewedConformanceFacet.CREATE: "exact-plan-create-native-and-family-invariants",
    ReviewedConformanceFacet.EDIT: "native-edit-propagates-through-solver-or-links",
    ReviewedConformanceFacet.RECOMPUTE: "explicit-recompute-preserves-valid-native-state",
    ReviewedConformanceFacet.SAVE: "managed-fcstd-save-is-nonempty",
    ReviewedConformanceFacet.REOPEN: "saved-state-reopens-with-topology-and-links-intact",
    ReviewedConformanceFacet.NEGATIVE: "tampered-plan-rejected-before-document-mutation",
    ReviewedConformanceFacet.LATE_ROLLBACK: "post-create-failure-restores-exact-state",
}


def _fail(path: str) -> None:
    raise CapabilityCatalogError(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)


def _canonical(value: object, *, maximum: int = 64 * 1024) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail("wave_c/canonical")
    if not raw or len(raw) > maximum:
        _fail("wave_c/canonical")
    return raw


def _sha(domain: bytes, value: bytes | str) -> str:
    raw = value if type(value) is bytes else value.encode("utf-8")
    return hashlib.sha256(domain + raw).hexdigest()


_HARNESS_CONTRACT_SHA256: Final = _sha(
    _HARNESS_CONTRACT_DOMAIN,
    _canonical(
        {
            "schema_version": 1,
            "verifier": {
                "id": WAVE_C_VERIFIER_ID,
                "version": WAVE_C_VERIFIER_VERSION,
            },
            "execution": {
                "caller_result_input": False,
                "one_real_scenario_per_operation": True,
                "same_process_managed_freecad": True,
                "temporary_paths": "host-owned",
                "receipt_persistence": False,
            },
            "family_obligations": {
                "sketch": ["solver", "topology", "overconstraint-rollback"],
                "app": ["root-ownership", "link-cycle", "metadata-persistence"],
            },
            "facets": {facet.value: value for facet, value in _FACET_CONTRACTS.items()},
        }
    ),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class WaveCFixtureDescriptor:
    family_id: str
    operation_id: str
    recipe_bytes: bytes
    fixture_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.family_id) is not str
            or not self.family_id
            or type(self.operation_id) is not str
            or not self.operation_id
            or type(self.recipe_bytes) is not bytes
            or not self.recipe_bytes
            or len(self.recipe_bytes) > 16 * 1024
        ):
            _fail("wave_c/fixture")
        try:
            decoded = json.loads(self.recipe_bytes)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            _fail("wave_c/fixture")
        if _canonical(decoded, maximum=16 * 1024) != self.recipe_bytes:
            _fail("wave_c/fixture")
        object.__setattr__(
            self,
            "fixture_sha256",
            _sha(
                _FIXTURE_DIGEST_DOMAIN,
                _canonical(
                    {
                        "family_id": self.family_id,
                        "operation_id": self.operation_id,
                        "recipe": decoded,
                    },
                    maximum=20 * 1024,
                ),
            ),
        )

    @property
    def recipe(self) -> dict[str, object]:
        value = json.loads(self.recipe_bytes)
        if type(value) is not dict:
            _fail("wave_c/fixture")
        return value


def _fixture(
    family_id: str,
    operation_id: str,
    recipe: dict[str, object],
) -> WaveCFixtureDescriptor:
    return WaveCFixtureDescriptor(
        family_id=family_id,
        operation_id=operation_id,
        recipe_bytes=_canonical(recipe, maximum=16 * 1024),
    )


_SKETCH_GEOMETRY_RECIPES: Final = {
    ReviewedSketchOperation.POINT: {
        "parameters": {"x_mm": 1.0, "y_mm": 2.0},
        "results": [["point", "result_target_point"]],
        "edit": "move-point",
    },
    ReviewedSketchOperation.LINE: {
        "parameters": {"x1_mm": 0.0, "x2_mm": 10.0, "y1_mm": 0.0, "y2_mm": 3.0},
        "results": [["curve", "result_target_line"]],
        "edit": "move-line-end",
    },
    ReviewedSketchOperation.CIRCLE: {
        "parameters": {"cx_mm": 20.0, "cy_mm": 0.0, "radius_mm": 4.0},
        "results": [["curve", "result_target_circle"]],
        "edit": "move-circle-center",
    },
    ReviewedSketchOperation.ARC: {
        "parameters": {
            "cx_mm": -20.0,
            "cy_mm": 0.0,
            "radius_mm": 5.0,
            "start_angle_rad": 0.0,
            "sweep_angle_rad": math.pi / 2.0,
        },
        "results": [["curve", "result_target_arc"]],
        "edit": "move-arc-center",
    },
    ReviewedSketchOperation.SLOT: {
        "parameters": {
            "width_mm": 4.0,
            "x1_mm": -10.0,
            "x2_mm": 10.0,
            "y1_mm": -10.0,
            "y2_mm": -7.0,
        },
        "results": [
            ["cap_end", "result_target_slot_cap_end"],
            ["cap_start", "result_target_slot_cap_start"],
            ["side_a", "result_target_slot_side_a"],
            ["side_b", "result_target_slot_side_b"],
        ],
        "edit": "move-slot-end",
    },
}

_SKETCH_CONSTRAINT_VALUES: Final = {
    ReviewedSketchOperation.DISTANCE: 10.0,
    ReviewedSketchOperation.DISTANCE_X: 6.0,
    ReviewedSketchOperation.DISTANCE_Y: 8.0,
    ReviewedSketchOperation.LENGTH: 10.0,
    ReviewedSketchOperation.RADIUS: 5.0,
    ReviewedSketchOperation.DIAMETER: 10.0,
    ReviewedSketchOperation.ANGLE: math.pi / 4.0,
}

_SKETCH_FIXTURES: Final = tuple(
    _fixture(
        REVIEWED_SKETCH_FAMILY_MANIFEST.family_id,
        operation.value,
        (
            _SKETCH_GEOMETRY_RECIPES[operation]
            if operation in _SKETCH_GEOMETRY_RECIPES
            else {
                "reference_recipe": operation.value,
                "value": _SKETCH_CONSTRAINT_VALUES.get(operation),
                "edit": "deactivate-reactivate-constraint",
            }
        ),
    )
    for operation in ReviewedSketchOperation
)

_PLACEMENT: Final = {
    "position_mm": [3.0, 4.0, 5.0],
    "axis": [0.0, 0.0, 1.0],
    "angle_degrees": 30.0,
}
_APP_CONFIGURATIONS: Final = {
    AppFamilyOperation.TEXT_ANNOTATION: {
        "lines": ["reviewed", "annotation"],
        "position_mm": [1.0, 2.0, 3.0],
    },
    AppFamilyOperation.LEADER_ANNOTATION: {
        "lines": ["reviewed leader"],
        "base_position_mm": [1.0, 2.0, 3.0],
        "text_position_mm": [4.0, 5.0, 6.0],
    },
    AppFamilyOperation.DOCUMENT_GROUP: {},
    AppFamilyOperation.OBJECT_LINK: {"placement": _PLACEMENT},
    AppFamilyOperation.LINK_GROUP: {"placement": _PLACEMENT},
    AppFamilyOperation.MATERIAL_DEFINITION: {
        "name": "Reviewed material",
        "description": "Bounded metadata",
        "density_kg_m3": 2700.0,
    },
    AppFamilyOperation.POSITIONED_PART: {"placement": _PLACEMENT},
    AppFamilyOperation.PLACEMENT_REFERENCE: {"placement": _PLACEMENT},
    AppFamilyOperation.TEXT_DOCUMENT: {"text": "Bounded reviewed text"},
    AppFamilyOperation.SCALAR_VARIABLE_SET: {"value": 12.5},
}
_APP_FIXTURES: Final = tuple(
    _fixture(
        APP_FAMILY_MANIFEST.family_id,
        operation.value,
        {
            "configuration": _APP_CONFIGURATIONS[operation],
            "relation_kind": APP_FAMILY_RELATION_KINDS[operation].value,
            "edit": operation.value,
        },
    )
    for operation in AppFamilyOperation
)


def _build_case_manifest(
    manifest: FamilyBatchManifest,
    fixtures: tuple[WaveCFixtureDescriptor, ...],
) -> ReviewedConformanceCaseManifest:
    operations = {item.operation_id: item for item in manifest.operations}
    if (
        {item.operation_id for item in fixtures} != set(operations)
        or {item.family_id for item in fixtures} != {manifest.family_id}
    ):
        _fail("wave_c/fixtures/closure")
    cases = []
    for fixture in fixtures:
        operation = operations[fixture.operation_id]
        for facet in ReviewedConformanceFacet:
            contract = _canonical(
                {
                    "facet": facet.value,
                    "facet_contract": _FACET_CONTRACTS[facet],
                    "family_manifest_sha256": manifest.manifest_sha256,
                    "fixture_sha256": fixture.fixture_sha256,
                    "harness_contract_sha256": _HARNESS_CONTRACT_SHA256,
                    "operation_id": operation.operation_id,
                    "operation_specification_sha256": operation.specification_sha256,
                    "schema_version": REVIEWED_VERIFICATION_SCHEMA_VERSION,
                }
            )
            contract_sha256 = _sha(_CASE_CONTRACT_DOMAIN, contract)
            cases.append(
                ReviewedConformanceCase(
                    case_id=f"wavec.{manifest.family_id}.{operation.operation_id}.{facet.value}",
                    operation_id=operation.operation_id,
                    operation_specification_sha256=operation.specification_sha256,
                    facet=facet,
                    case_contract_sha256=contract_sha256,
                )
            )
    return verification._admit_reviewed_host_conformance_case_manifest(  # noqa: SLF001
        manifest=manifest,
        cases=tuple(cases),
    )


SKETCH_REVIEWED_HOST_CASE_MANIFEST: Final = _build_case_manifest(
    REVIEWED_SKETCH_FAMILY_MANIFEST,
    _SKETCH_FIXTURES,
)
APP_REVIEWED_HOST_CASE_MANIFEST: Final = _build_case_manifest(
    APP_FAMILY_MANIFEST,
    _APP_FIXTURES,
)


def _close_owned_documents(freecad: object, owned: dict[str, object]) -> None:
    try:
        open_documents = freecad.listDocuments()
        for name, document in tuple(owned.items()):
            if open_documents.get(name) is document:
                freecad.closeDocument(name)
    except BaseException:
        _fail("wave_c/cleanup")


def _case_observation(
    *,
    case: ReviewedConformanceCase,
    challenge_sha256: str,
    fixture: WaveCFixtureDescriptor,
    evidence: dict[str, object],
) -> bytes:
    if (
        type(case) is not ReviewedConformanceCase
        or type(challenge_sha256) is not str
        or len(challenge_sha256) != 64
        or any(character not in "0123456789abcdef" for character in challenge_sha256)
        or case.operation_id != fixture.operation_id
        or type(evidence) is not dict
    ):
        _fail("wave_c/observation")
    observation_body = {
        "case_sha256": case.case_sha256,
        "challenge_sha256": challenge_sha256,
        "evidence": evidence,
    }
    return _canonical(
        {
            "authority": "none",
            "case_contract_sha256": case.case_contract_sha256,
            "case_sha256": case.case_sha256,
            "challenge_sha256": challenge_sha256,
            "evidence": evidence,
            "facet": case.facet.value,
            "fixture_sha256": fixture.fixture_sha256,
            "harness_contract_sha256": _HARNESS_CONTRACT_SHA256,
            "observation_schema": 1,
            "observation_sha256": _sha(_OBSERVATION_DOMAIN, _canonical(observation_body)),
            "operation_id": case.operation_id,
        }
    )


def _specification_sha256(manifest: FamilyBatchManifest, operation_id: str) -> str:
    try:
        return next(
            item.specification_sha256
            for item in manifest.operations
            if item.operation_id == operation_id
        )
    except StopIteration:
        _fail("wave_c/operation")


def _sketch_plan(
    operation: ReviewedSketchOperation,
    *,
    sketch_id: str,
    node_id: str,
    parameters: dict[str, float],
    references: tuple[ReviewedSketchReference, ...] = (),
    results: tuple[tuple[str, str], ...],
    geometry: bool,
    enabled: bool = True,
) -> ReviewedSketchBackendPlan:
    return ReviewedSketchBackendPlan(
        source_artifact_id="artifact_wave_c_sketch",
        source_graph_id="graph_wave_c_sketch",
        source_graph_sha256="1" * 64,
        source_content_sha256="2" * 64,
        request_digest="3" * 64,
        adapter_contract_sha256=(
            REVIEWED_SKETCH_FAMILY_MANIFEST.adapter.adapter_contract_sha256
        ),
        manifest_sha256=REVIEWED_SKETCH_FAMILY_MANIFEST.manifest_sha256,
        operation_specification_sha256=_specification_sha256(
            REVIEWED_SKETCH_FAMILY_MANIFEST,
            operation.value,
        ),
        sketch_id=sketch_id,
        node_id=node_id,
        node_sha256=reviewed_sketch_node_sha256(
            {"node_id": node_id, "operation": operation.value, "parameters": parameters}
        ),
        operation=operation,
        parameters=tuple(
            ReviewedSketchParameter(key=key, value=value)
            for key, value in parameters.items()
        ),
        references=references,
        results=tuple(
            ReviewedSketchResult(port_id=port_id, result_id=result_id)
            for port_id, result_id in results
        ),
        construction=False if geometry else None,
        mode=None if geometry else "driving",
        enabled=None if geometry else enabled,
    )


def _apply_sketch(
    document: object,
    sketch: object,
    sketch_id: str,
    plan: ReviewedSketchBackendPlan,
):
    payload = plan.canonical_bytes
    return apply_reviewed_sketch_plan(
        payload,
        expected_content_sha256=hashlib.sha256(payload).hexdigest(),
        expected_plan_sha256=plan.plan_sha256,
        bindings=ReviewedSketchExecutionBindings(
            document=document,
            sketch=sketch,
            sketch_id=sketch_id,
        ),
    )


def _add_geometry(
    document: object,
    sketch: object,
    sketch_id: str,
    operation: ReviewedSketchOperation,
    suffix: str,
    parameters: dict[str, float],
    results: tuple[tuple[str, str], ...],
) -> tuple[ReviewedSketchBackendPlan, object]:
    plan = _sketch_plan(
        operation,
        sketch_id=sketch_id,
        node_id=f"geometry_{suffix}",
        parameters=parameters,
        results=results,
        geometry=True,
    )
    return plan, _apply_sketch(document, sketch, sketch_id, plan)


def _result_reference(
    plan: ReviewedSketchBackendPlan,
    result: ReviewedSketchResult,
    role: str,
    value_type: str,
) -> ReviewedSketchReference:
    return ReviewedSketchReference(
        source_kind="result",
        target_id=result.result_id,
        role=role,
        producer_geometry_id=plan.node_id,
        producer_node_sha256=plan.node_sha256,
        port_id=result.port_id,
        value_type=value_type,
    )


def _sketch_reference(sketch_id: str, role: str) -> ReviewedSketchReference:
    return ReviewedSketchReference(
        source_kind="sketch",
        target_id=sketch_id,
        role=role,
    )


def _constraint_target_plan(
    operation: ReviewedSketchOperation,
    document: object,
    sketch: object,
    sketch_id: str,
) -> ReviewedSketchBackendPlan:
    """Create authenticated prerequisite geometry and return one target plan."""

    if operation is ReviewedSketchOperation.COINCIDENT:
        first, receipt = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.LINE,
            "line",
            {"x1_mm": 0.0, "x2_mm": 10.0, "y1_mm": 0.0, "y2_mm": 2.0},
            (("curve", "result_line"),),
        )
        refs = (
            _result_reference(first, first.results[0], "start", "line"),
            _sketch_reference(sketch_id, "origin"),
        )
    elif operation in {ReviewedSketchOperation.HORIZONTAL, ReviewedSketchOperation.LENGTH}:
        first, receipt = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.LINE,
            "line",
            {"x1_mm": 0.0, "x2_mm": 10.0, "y1_mm": 0.0, "y2_mm": 0.0},
            (("curve", "result_line"),),
        )
        refs = (_result_reference(first, first.results[0], "whole", "line"),)
    elif operation is ReviewedSketchOperation.VERTICAL:
        first, receipt = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.LINE,
            "line",
            {"x1_mm": 0.0, "x2_mm": 0.0, "y1_mm": 0.0, "y2_mm": 10.0},
            (("curve", "result_line"),),
        )
        refs = (_result_reference(first, first.results[0], "whole", "line"),)
    elif operation in {ReviewedSketchOperation.PARALLEL, ReviewedSketchOperation.EQUAL}:
        first, receipt = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.LINE,
            "line_a",
            {"x1_mm": 0.0, "x2_mm": 10.0, "y1_mm": 0.0, "y2_mm": 0.0},
            (("curve", "result_line_a"),),
        )
        second, _ = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.LINE,
            "line_b",
            {"x1_mm": 0.0, "x2_mm": 10.0, "y1_mm": 5.0, "y2_mm": 5.0},
            (("curve", "result_line_b"),),
        )
        refs = (
            _result_reference(first, first.results[0], "whole", "line"),
            _result_reference(second, second.results[0], "whole", "line"),
        )
    elif operation is ReviewedSketchOperation.PERPENDICULAR:
        first, receipt = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.LINE,
            "line_a",
            {"x1_mm": 0.0, "x2_mm": 10.0, "y1_mm": 0.0, "y2_mm": 0.0},
            (("curve", "result_line_a"),),
        )
        second, _ = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.LINE,
            "line_b",
            {"x1_mm": 0.0, "x2_mm": 0.0, "y1_mm": 0.0, "y2_mm": 10.0},
            (("curve", "result_line_b"),),
        )
        refs = (
            _result_reference(first, first.results[0], "whole", "line"),
            _result_reference(second, second.results[0], "whole", "line"),
        )
    elif operation is ReviewedSketchOperation.TANGENT:
        first, receipt = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.LINE,
            "line",
            {"x1_mm": -10.0, "x2_mm": 10.0, "y1_mm": 5.0, "y2_mm": 5.0},
            (("curve", "result_line"),),
        )
        second, _ = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.CIRCLE,
            "circle",
            {"cx_mm": 0.0, "cy_mm": 0.0, "radius_mm": 5.0},
            (("curve", "result_circle"),),
        )
        refs = (
            _result_reference(first, first.results[0], "whole", "line"),
            _result_reference(second, second.results[0], "whole", "circle"),
        )
    elif operation is ReviewedSketchOperation.SYMMETRIC:
        first, receipt = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.POINT,
            "point_a",
            {"x_mm": -3.0, "y_mm": 2.0},
            (("point", "result_point_a"),),
        )
        second, _ = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.POINT,
            "point_b",
            {"x_mm": 3.0, "y_mm": 2.0},
            (("point", "result_point_b"),),
        )
        axis, _ = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.LINE,
            "axis",
            {"x1_mm": 0.0, "x2_mm": 0.0, "y1_mm": -10.0, "y2_mm": 10.0},
            (("curve", "result_axis"),),
        )
        refs = (
            _result_reference(first, first.results[0], "point", "point"),
            _result_reference(second, second.results[0], "point", "point"),
            _result_reference(axis, axis.results[0], "whole", "line"),
        )
    elif operation in {
        ReviewedSketchOperation.DISTANCE,
        ReviewedSketchOperation.DISTANCE_X,
        ReviewedSketchOperation.DISTANCE_Y,
    }:
        first, receipt = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.POINT,
            "point_a",
            {"x_mm": 0.0, "y_mm": 0.0},
            (("point", "result_point_a"),),
        )
        second, _ = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.POINT,
            "point_b",
            {"x_mm": 6.0, "y_mm": 8.0},
            (("point", "result_point_b"),),
        )
        refs = (
            _result_reference(first, first.results[0], "point", "point"),
            _result_reference(second, second.results[0], "point", "point"),
        )
    elif operation in {ReviewedSketchOperation.RADIUS, ReviewedSketchOperation.DIAMETER}:
        first, receipt = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.CIRCLE,
            "circle",
            {"cx_mm": 0.0, "cy_mm": 0.0, "radius_mm": 5.0},
            (("curve", "result_circle"),),
        )
        refs = (_result_reference(first, first.results[0], "whole", "circle"),)
    else:
        first, receipt = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.LINE,
            "line_a",
            {"x1_mm": 0.0, "x2_mm": 10.0, "y1_mm": 0.0, "y2_mm": 0.0},
            (("curve", "result_line_a"),),
        )
        second, _ = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.LINE,
            "line_b",
            {"x1_mm": 0.0, "x2_mm": 10.0, "y1_mm": 0.0, "y2_mm": 10.0},
            (("curve", "result_line_b"),),
        )
        refs = (
            _result_reference(first, first.results[0], "whole", "line"),
            _result_reference(second, second.results[0], "whole", "line"),
        )
    del receipt
    parameters: dict[str, float] = {}
    if operation in _SKETCH_CONSTRAINT_VALUES:
        key = "value_rad" if operation is ReviewedSketchOperation.ANGLE else "value_mm"
        parameters[key] = _SKETCH_CONSTRAINT_VALUES[operation]
    return _sketch_plan(
        operation,
        sketch_id=sketch_id,
        node_id=f"constraint_{operation.value}",
        parameters=parameters,
        references=refs,
        results=(("constraint", f"result_constraint_{operation.value}"),),
        geometry=False,
    )


def _target_sketch_plan(
    operation: ReviewedSketchOperation,
    document: object,
    sketch: object,
    sketch_id: str,
) -> ReviewedSketchBackendPlan:
    if operation in _SKETCH_GEOMETRY_RECIPES:
        recipe = _SKETCH_GEOMETRY_RECIPES[operation]
        parameters = recipe["parameters"]
        results = recipe["results"]
        if type(parameters) is not dict or type(results) is not list:
            _fail("wave_c/sketch/recipe")
        return _sketch_plan(
            operation,
            sketch_id=sketch_id,
            node_id=f"geometry_target_{operation.value}",
            parameters={key: float(value) for key, value in parameters.items()},
            results=tuple((str(item[0]), str(item[1])) for item in results),
            geometry=True,
        )
    return _constraint_target_plan(operation, document, sketch, sketch_id)


def _sketch_state(sketch: object) -> dict[str, object]:
    try:
        solve_result, dof, fully_constrained = sketch_rules._solver_facts(sketch)  # noqa: SLF001
        geometry_types = [item.TypeId for item in sketch.Geometry]
        constraint_types = [item.Type for item in sketch.Constraints]
        active = [bool(sketch.getActive(index)) for index in range(sketch.ConstraintCount)]
        metadata = sketch.VibeCADReviewedSketchIntent
        signature = sketch_rules._native_state_signature(sketch)  # noqa: SLF001
    except CapabilityCatalogError:
        raise
    except BaseException:
        _fail("wave_c/sketch/state")
    return {
        "solve_result": solve_result,
        "dof": dof,
        "fully_constrained": fully_constrained,
        "geometry_count": len(geometry_types),
        "constraint_count": len(constraint_types),
        "geometry_types": geometry_types,
        "constraint_types": constraint_types,
        "active_constraints": active,
        "metadata_sha256": hashlib.sha256(metadata.encode("utf-8")).hexdigest(),
        "native_signature_sha256": _sha(
            b"vibecad-reviewed-wave-c-sketch-state-v1\0",
            _canonical(signature, maximum=256 * 1024),
        ),
        "diagnostics_empty": not any(
            tuple(value)
            for value in (
                sketch.ConflictingConstraints,
                sketch.RedundantConstraints,
                sketch.PartiallyRedundantConstraints,
                sketch.MalformedConstraints,
            )
        ),
    }


def _sketch_snapshot(document: object, sketch: object) -> tuple[object, ...]:
    return (
        tuple(document.Objects),
        tuple(item.TypeId for item in sketch.Geometry),
        tuple((item.Type, item.Name) for item in sketch.Constraints),
        getattr(sketch, "VibeCADReviewedSketchIntent", None),
        sketch.GeometryCount,
        sketch.ConstraintCount,
        sketch_rules._native_state_signature(sketch),  # noqa: SLF001
        bool(document.HasPendingTransaction),
    )


def _same_object_identity(left: object, right: tuple[object, ...]) -> bool:
    try:
        values = tuple(left)
    except BaseException:
        return False
    return len(values) == len(right) and all(
        actual is expected for actual, expected in zip(values, right, strict=True)
    )


def _edit_sketch_geometry(
    freecad: object,
    document: object,
    sketch: object,
    operation: ReviewedSketchOperation,
    receipt: object,
) -> dict[str, object]:
    before = sketch_rules._native_state_signature(sketch)  # noqa: SLF001
    index = receipt.geometry_indices[0]
    point_position = {
        ReviewedSketchOperation.POINT: 1,
        ReviewedSketchOperation.LINE: 2,
        ReviewedSketchOperation.CIRCLE: 3,
        ReviewedSketchOperation.ARC: 3,
        ReviewedSketchOperation.SLOT: 2,
    }[operation]
    try:
        sketch.moveGeometry(index, point_position, freecad.Vector(12.0, 7.0, 0.0))
        document.recompute()
        solve_result, dof, fully_constrained = sketch_rules._solver_facts(  # noqa: SLF001
            sketch
        )
        after = sketch_rules._native_state_signature(sketch)  # noqa: SLF001
    except ReviewedSketchRuleError:
        raise
    except BaseException:
        _fail("wave_c/sketch/edit")
    if after == before:
        _fail("wave_c/sketch/edit")
    return {
        "strategy": _SKETCH_GEOMETRY_RECIPES[operation]["edit"],
        "topology_preserved": (
            len(before[0]) == len(after[0]) and len(before[1]) == len(after[1])
        ),
        "solver": {
            "result": solve_result,
            "dof": dof,
            "fully_constrained": fully_constrained,
        },
    }


def _edit_sketch_constraint(document: object, sketch: object, receipt: object) -> dict[str, object]:
    index = receipt.constraint_indices[0]
    before_dof = int(sketch.DoF)
    try:
        sketch.setActive(index, False)
        document.recompute()
        off_result, off_dof, _ = sketch_rules._solver_facts(sketch)  # noqa: SLF001
        sketch.setActive(index, True)
        document.recompute()
        on_result, on_dof, on_fully = sketch_rules._solver_facts(sketch)  # noqa: SLF001
    except ReviewedSketchRuleError:
        raise
    except BaseException:
        _fail("wave_c/sketch/edit")
    if off_dof < before_dof or on_dof != before_dof:
        _fail("wave_c/sketch/edit")
    return {
        "strategy": "deactivate-reactivate-constraint",
        "inactive_solver_result": off_result,
        "inactive_dof": off_dof,
        "restored_solver_result": on_result,
        "restored_dof": on_dof,
        "restored_fully_constrained": on_fully,
    }


def _save_reopen_sketch(
    freecad: object,
    document: object,
    sketch: object,
    path: Path,
    owned: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    before = _sketch_state(sketch)
    document.saveAs(str(path))
    if not path.is_file() or path.stat().st_size <= 0:
        _fail("wave_c/sketch/save")
    object_name = sketch.Name
    freecad.closeDocument(document.Name)
    reopened = freecad.openDocument(str(path))
    owned[reopened.Name] = reopened
    try:
        reopened.recompute()
        reopened_sketch = reopened.getObject(object_name)
        if reopened_sketch is None or reopened_sketch.TypeId != REVIEWED_SKETCH_NATIVE_TYPE_ID:
            _fail("wave_c/sketch/reopen")
        after = _sketch_state(reopened_sketch)
        stable_keys = (
            "geometry_count",
            "constraint_count",
            "geometry_types",
            "constraint_types",
            "active_constraints",
            "metadata_sha256",
            "native_signature_sha256",
        )
        if any(before[key] != after[key] for key in stable_keys):
            _fail("wave_c/sketch/reopen")
    finally:
        freecad.closeDocument(reopened.Name)
    return (
        {"format": "FCStd", "nonempty": True, "object_count": 1},
        {"native_type_id": REVIEWED_SKETCH_NATIVE_TYPE_ID, "state": after},
    )


def _sketch_late_rollback(
    freecad: object,
    operation: ReviewedSketchOperation,
) -> dict[str, object]:
    owned: dict[str, object] = {}
    try:
        document = freecad.newDocument(f"VerifySketchRollback_{operation.value}")
        owned[document.Name] = document
        document.UndoMode = 1
        sketch_id = f"rollback_sketch_{operation.value}"
        sketch = document.addObject(REVIEWED_SKETCH_NATIVE_TYPE_ID, "Sketch")
        plan = _target_sketch_plan(operation, document, sketch, sketch_id)
        before = _sketch_snapshot(document, sketch)
        original = sketch_rules._write_metadata  # noqa: SLF001
        late_validation_reached = False

        def fail_after_native_create(*_args: object, **_kwargs: object) -> None:
            nonlocal late_validation_reached
            late_validation_reached = True
            raise ReviewedSketchRuleError(
                ReviewedSketchRuleErrorCode.CONFORMANCE_FAILED,
                "/result/injected-late-failure",
            )

        sketch_rules._write_metadata = fail_after_native_create  # noqa: SLF001
        try:
            _apply_sketch(document, sketch, sketch_id, plan)
        except ReviewedSketchRuleError:
            pass
        else:
            _fail("wave_c/sketch/late_rollback")
        finally:
            sketch_rules._write_metadata = original  # noqa: SLF001
        after = _sketch_snapshot(document, sketch)
        if (
            not late_validation_reached
            or not _same_object_identity(after[0], before[0])
            or after[1:] != before[1:]
            or bool(document.HasPendingTransaction)
        ):
            _fail("wave_c/sketch/late_rollback")
        return {
            "injection_point": "metadata-write-after-native-create",
            "native_mutation_reached": True,
            "objects_restored": len(before[0]),
            "topology_restored": True,
            "pending_transaction": False,
        }
    finally:
        _close_owned_documents(freecad, owned)


def _sketch_overconstraint_probe(freecad: object) -> dict[str, object]:
    """Prove a real contradictory constraint is rejected and rolled back."""

    owned: dict[str, object] = {}
    try:
        document = freecad.newDocument("VerifySketchOverconstraint")
        owned[document.Name] = document
        document.UndoMode = 1
        sketch_id = "sketch_overconstraint"
        sketch = document.addObject(REVIEWED_SKETCH_NATIVE_TYPE_ID, "Sketch")
        line, _ = _add_geometry(
            document,
            sketch,
            sketch_id,
            ReviewedSketchOperation.LINE,
            "full_line",
            {"x1_mm": 0.0, "x2_mm": 10.0, "y1_mm": 0.0, "y2_mm": 0.0},
            (("curve", "result_full_line"),),
        )
        whole = _result_reference(line, line.results[0], "whole", "line")
        start = _result_reference(line, line.results[0], "start", "line")
        prerequisites = (
            (
                ReviewedSketchOperation.COINCIDENT,
                (start, _sketch_reference(sketch_id, "origin")),
                {},
            ),
            (ReviewedSketchOperation.HORIZONTAL, (whole,), {}),
            (ReviewedSketchOperation.LENGTH, (whole,), {"value_mm": 10.0}),
        )
        for index, (operation, references, parameters) in enumerate(prerequisites):
            plan = _sketch_plan(
                operation,
                sketch_id=sketch_id,
                node_id=f"constraint_guard_{index}",
                parameters=parameters,
                references=references,
                results=(("constraint", f"result_guard_{index}"),),
                geometry=False,
            )
            _apply_sketch(document, sketch, sketch_id, plan)
        if sketch.DoF != 0 or not sketch.FullyConstrained:
            _fail("wave_c/sketch/overconstraint")
        before = _sketch_snapshot(document, sketch)
        contradictory = _sketch_plan(
            ReviewedSketchOperation.VERTICAL,
            sketch_id=sketch_id,
            node_id="constraint_contradictory_vertical",
            parameters={},
            references=(whole,),
            results=(("constraint", "result_contradictory_vertical"),),
            geometry=False,
        )
        try:
            _apply_sketch(document, sketch, sketch_id, contradictory)
        except ReviewedSketchRuleError:
            pass
        else:
            _fail("wave_c/sketch/overconstraint")
        after = _sketch_snapshot(document, sketch)
        if (
            not _same_object_identity(after[0], before[0])
            or after[1:] != before[1:]
            or sketch.solve() != 0
            or sketch.DoF != 0
            or not sketch.FullyConstrained
        ):
            _fail("wave_c/sketch/overconstraint")
        return {
            "contradiction": "horizontal-plus-vertical-on-fully-constrained-line",
            "rejected": True,
            "topology_restored": True,
            "solver_recovered": True,
        }
    finally:
        _close_owned_documents(freecad, owned)


class _SketchExecutor:
    __slots__ = ("_cache", "_fixtures", "_freecad", "_overconstraint")

    def __init__(self, freecad: object) -> None:
        self._freecad = freecad
        self._fixtures = {item.operation_id: item for item in _SKETCH_FIXTURES}
        self._cache: dict[str, dict[ReviewedConformanceFacet, dict[str, object]]] = {}
        self._overconstraint: dict[str, object] | None = None

    def __call__(self, case: ReviewedConformanceCase, challenge_sha256: str) -> bytes:
        fixture = self._fixtures.get(case.operation_id)
        if fixture is None:
            _fail("wave_c/sketch/case")
        outcomes = self._cache.get(case.operation_id)
        if outcomes is None:
            outcomes = self._run(ReviewedSketchOperation(case.operation_id))
            self._cache[case.operation_id] = outcomes
        return _case_observation(
            case=case,
            challenge_sha256=challenge_sha256,
            fixture=fixture,
            evidence=outcomes[case.facet],
        )

    def _run(
        self,
        operation: ReviewedSketchOperation,
    ) -> dict[ReviewedConformanceFacet, dict[str, object]]:
        freecad = self._freecad
        owned: dict[str, object] = {}
        outcomes: dict[ReviewedConformanceFacet, dict[str, object]] = {}
        try:
            with tempfile.TemporaryDirectory(prefix="vibecad-wave-c-sketch-") as temporary:
                document = freecad.newDocument(f"VerifySketch_{operation.value}")
                owned[document.Name] = document
                document.UndoMode = 1
                sketch_id = f"sketch_{operation.value}"
                sketch = document.addObject(REVIEWED_SKETCH_NATIVE_TYPE_ID, "Sketch")
                plan = _target_sketch_plan(operation, document, sketch, sketch_id)
                payload = plan.canonical_bytes
                decoded = decode_reviewed_sketch_backend_plan(
                    payload,
                    expected_content_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_plan_sha256=plan.plan_sha256,
                )
                if decoded != plan:
                    _fail("wave_c/sketch/readback")

                before_negative = _sketch_snapshot(document, sketch)
                try:
                    apply_reviewed_sketch_plan(
                        payload + b" ",
                        expected_content_sha256=hashlib.sha256(payload).hexdigest(),
                        expected_plan_sha256=plan.plan_sha256,
                        bindings=ReviewedSketchExecutionBindings(
                            document=document,
                            sketch=sketch,
                            sketch_id=sketch_id,
                        ),
                    )
                except ReviewedSketchRuleError:
                    pass
                else:
                    _fail("wave_c/sketch/negative")
                after_negative = _sketch_snapshot(document, sketch)
                if (
                    not _same_object_identity(after_negative[0], before_negative[0])
                    or after_negative[1:] != before_negative[1:]
                ):
                    _fail("wave_c/sketch/negative")
                outcomes[ReviewedConformanceFacet.NEGATIVE] = {
                    "tamper": "append-noncanonical-byte",
                    "mutation_count": 0,
                    "topology_preserved": True,
                }

                receipt = _apply_sketch(document, sketch, sketch_id, plan)
                created_state = _sketch_state(sketch)
                if (
                    receipt.operation is not operation
                    or not created_state["diagnostics_empty"]
                    or sketch.TypeId != REVIEWED_SKETCH_NATIVE_TYPE_ID
                ):
                    _fail("wave_c/sketch/create")
                outcomes[ReviewedConformanceFacet.CREATE] = {
                    "native_receipt_sha256": receipt.receipt_sha256,
                    "native_type_id": REVIEWED_SKETCH_NATIVE_TYPE_ID,
                    "topology": {
                        "geometry_count": created_state["geometry_count"],
                        "constraint_count": created_state["constraint_count"],
                        "geometry_types": created_state["geometry_types"],
                        "constraint_types": created_state["constraint_types"],
                    },
                    "solver": {
                        "result": created_state["solve_result"],
                        "dof": created_state["dof"],
                        "fully_constrained": created_state["fully_constrained"],
                        "diagnostics_empty": created_state["diagnostics_empty"],
                    },
                }

                before_recompute = created_state["native_signature_sha256"]
                document.recompute()
                recomputed = _sketch_state(sketch)
                if recomputed["native_signature_sha256"] != before_recompute:
                    _fail("wave_c/sketch/recompute")
                outcomes[ReviewedConformanceFacet.RECOMPUTE] = {
                    "state_sha256": before_recompute,
                    "solver_result": recomputed["solve_result"],
                    "diagnostics_empty": recomputed["diagnostics_empty"],
                }

                saved, reopened = _save_reopen_sketch(
                    freecad,
                    document,
                    sketch,
                    Path(temporary) / f"sketch-{operation.value}.FCStd",
                    owned,
                )
                outcomes[ReviewedConformanceFacet.SAVE] = saved
                outcomes[ReviewedConformanceFacet.REOPEN] = reopened

                # The original document was closed by save/reopen.  Use an
                # independent exact scenario for native edit propagation.
                edit_document = freecad.newDocument(f"VerifySketchEdit_{operation.value}")
                owned[edit_document.Name] = edit_document
                edit_document.UndoMode = 1
                edit_sketch = edit_document.addObject(REVIEWED_SKETCH_NATIVE_TYPE_ID, "Sketch")
                edit_plan = _target_sketch_plan(
                    operation,
                    edit_document,
                    edit_sketch,
                    sketch_id,
                )
                edit_receipt = _apply_sketch(edit_document, edit_sketch, sketch_id, edit_plan)
                if operation in _SKETCH_GEOMETRY_RECIPES:
                    edit_evidence = _edit_sketch_geometry(
                        freecad,
                        edit_document,
                        edit_sketch,
                        operation,
                        edit_receipt,
                    )
                else:
                    edit_evidence = _edit_sketch_constraint(
                        edit_document,
                        edit_sketch,
                        edit_receipt,
                    )
                outcomes[ReviewedConformanceFacet.EDIT] = edit_evidence
                freecad.closeDocument(edit_document.Name)

                rollback = _sketch_late_rollback(freecad, operation)
                if operation is ReviewedSketchOperation.VERTICAL:
                    if self._overconstraint is None:
                        self._overconstraint = _sketch_overconstraint_probe(freecad)
                    rollback = {**rollback, "overconstraint": self._overconstraint}
                outcomes[ReviewedConformanceFacet.LATE_ROLLBACK] = rollback
        finally:
            _close_owned_documents(freecad, owned)
        if set(outcomes) != set(ReviewedConformanceFacet):
            _fail("wave_c/sketch/outcomes")
        return outcomes


def _app_plan(operation: AppFamilyOperation) -> AppFamilyBackendPlan:
    relation_kind = APP_FAMILY_RELATION_KINDS[operation]
    return AppFamilyBackendPlan(
        source_artifact_id="artifact_wave_c_app",
        source_graph_id="graph_wave_c_app",
        source_graph_sha256="4" * 64,
        source_content_sha256="5" * 64,
        lowering_request_sha256="6" * 64,
        adapter_contract_sha256=APP_FAMILY_MANIFEST.adapter.adapter_contract_sha256,
        manifest_sha256=APP_FAMILY_MANIFEST.manifest_sha256,
        container_id="document_space",
        target_node_id=f"node_{operation.value}",
        target_result_id=f"result_{operation.value}",
        operation=operation,
        configuration_bytes=encode_app_family_configuration(
            operation,
            _APP_CONFIGURATIONS[operation],
        ),
        related_node_id=(
            None if relation_kind is AppFamilyRelationKind.NONE else "node_related"
        ),
        related_result_id=(
            None if relation_kind is AppFamilyRelationKind.NONE else "result_related"
        ),
    )


def _app_bindings(
    document: object,
    plan: AppFamilyBackendPlan,
    related: object | None,
) -> AppFamilyExecutionBindings:
    return AppFamilyExecutionBindings(
        document=document,
        container_id=plan.container_id,
        related_node_id=plan.related_node_id,
        related_result_id=plan.related_result_id,
        related_object=related,
    )


def _app_related(document: object, operation: AppFamilyOperation) -> object | None:
    relation_kind = APP_FAMILY_RELATION_KINDS[operation]
    if relation_kind is AppFamilyRelationKind.NONE:
        return None
    type_id = "Part::Box" if operation is AppFamilyOperation.OBJECT_LINK else "Part::Feature"
    related = document.addObject(type_id, "Related")
    document.recompute()
    return related


def _vector(value: object) -> list[float]:
    try:
        return [round(float(item), 9) for item in value]
    except BaseException:
        _fail("wave_c/app/vector")


def _placement(value: object) -> dict[str, object]:
    try:
        return {
            "base": [
                round(float(value.Base.x), 9),
                round(float(value.Base.y), 9),
                round(float(value.Base.z), 9),
            ],
            "rotation_q": [round(float(item), 12) for item in value.Rotation.Q],
        }
    except BaseException:
        _fail("wave_c/app/placement")


def _app_state(feature: object, operation: AppFamilyOperation) -> dict[str, object]:
    """Return bounded native metadata/link evidence without object reprs."""

    try:
        state: dict[str, object] = {
            "type_id": feature.TypeId,
            "valid": bool(feature.isValid()),
            "root_owned": feature.getParentGroup() is None,
            "expression_count": len(tuple(feature.ExpressionEngine)),
        }
        if operation is AppFamilyOperation.TEXT_ANNOTATION:
            state["metadata"] = {
                "lines": list(feature.LabelText),
                "position": _vector(feature.Position),
            }
        elif operation is AppFamilyOperation.LEADER_ANNOTATION:
            state["metadata"] = {
                "lines": list(feature.LabelText),
                "base_position": _vector(feature.BasePosition),
                "text_position": _vector(feature.TextPosition),
            }
        elif operation is AppFamilyOperation.DOCUMENT_GROUP:
            state["members"] = [item.Name for item in feature.Group]
        elif operation is AppFamilyOperation.OBJECT_LINK:
            state["linked_object"] = feature.LinkedObject.Name
            state["placement"] = _placement(feature.Placement)
            state["link_transform"] = bool(feature.LinkTransform)
            state["shape_null"] = bool(feature.Shape.isNull())
        elif operation is AppFamilyOperation.LINK_GROUP:
            state["members"] = [item.Name for item in feature.ElementList]
            state["placement"] = _placement(feature.Placement)
        elif operation is AppFamilyOperation.MATERIAL_DEFINITION:
            state["metadata"] = dict(feature.Material)
        elif operation is AppFamilyOperation.POSITIONED_PART:
            state["members"] = [item.Name for item in feature.Group]
            state["placement"] = _placement(feature.Placement)
            state["origin_feature_count"] = len(tuple(feature.Origin.OriginFeatures))
        elif operation is AppFamilyOperation.PLACEMENT_REFERENCE:
            state["placement"] = _placement(feature.Placement)
        elif operation is AppFamilyOperation.TEXT_DOCUMENT:
            state["metadata"] = {"text": feature.Text}
        elif operation is AppFamilyOperation.SCALAR_VARIABLE_SET:
            state["metadata"] = {
                "value": round(float(feature.Value), 9),
                "property_type": feature.getTypeIdOfProperty("Value"),
                "property_group": feature.getGroupOfProperty("Value"),
            }
        else:
            _fail("wave_c/app/state")
        related = tuple(feature.OutListRecursive)
        # FreeCAD does not promise a stable traversal order for App::Part's
        # generated Origin helpers across save/reopen.  The semantic closure is
        # a set; names remain stable and are compared canonically here.
        state["out_list_recursive"] = sorted(item.Name for item in related)
        state["acyclic"] = not any(item is feature for item in related)
        return state
    except CapabilityCatalogError:
        raise
    except BaseException:
        _fail("wave_c/app/state")


def _app_document_snapshot(document: object) -> tuple[object, ...]:
    return (
        tuple(document.Objects),
        app_rules._snapshot(document),  # noqa: SLF001
        bool(document.HasPendingTransaction),
    )


def _app_snapshot_matches(document: object, before: tuple[object, ...]) -> bool:
    try:
        return (
            _same_object_identity(document.Objects, before[0])
            and app_rules._rollback_matches(document, before[1])  # noqa: SLF001
            and bool(document.HasPendingTransaction) is before[2]
        )
    except BaseException:
        return False


def _edit_app_feature(
    freecad: object,
    document: object,
    feature: object,
    related: object | None,
    operation: AppFamilyOperation,
) -> dict[str, object]:
    before = _app_state(feature, operation)
    if operation is AppFamilyOperation.TEXT_ANNOTATION:
        feature.LabelText = ["edited"]
        feature.Position = freecad.Vector(8.0, 9.0, 10.0)
    elif operation is AppFamilyOperation.LEADER_ANNOTATION:
        feature.LabelText = ["edited leader"]
        feature.TextPosition = freecad.Vector(8.0, 9.0, 10.0)
    elif operation is AppFamilyOperation.DOCUMENT_GROUP:
        feature.addObject(document.addObject("Part::Feature", "Second"))
    elif operation is AppFamilyOperation.OBJECT_LINK:
        if related is None:
            _fail("wave_c/app/edit")
        before_x = float(feature.Shape.BoundBox.XMin)
        related.Placement.Base.x = 10.0
        document.recompute()
        if abs(float(feature.Shape.BoundBox.XMin) - before_x) <= 1.0:
            _fail("wave_c/app/edit")
        feature.Placement.Base.y = 8.0
    elif operation is AppFamilyOperation.LINK_GROUP:
        if related is None:
            _fail("wave_c/app/edit")
        second = document.addObject("Part::Feature", "Second")
        feature.setLink([related, second])
        feature.Placement.Base.y = 8.0
    elif operation is AppFamilyOperation.MATERIAL_DEFINITION:
        feature.Material = {
            "Name": "Edited",
            "Description": "still bounded",
            "Density": "7800 kg/m^3",
        }
    elif operation is AppFamilyOperation.POSITIONED_PART:
        if related is None:
            _fail("wave_c/app/edit")
        feature.Placement.Base.x = 11.0
        document.recompute()
        if abs(float(related.getGlobalPlacement().Base.x) - 11.0) > 1e-9:
            _fail("wave_c/app/edit")
    elif operation is AppFamilyOperation.PLACEMENT_REFERENCE:
        consumer = document.addObject("Part::Feature", "Consumer")
        consumer.setExpression("Placement.Base.x", f"{feature.Name}.Placement.Base.x")
        feature.Placement.Base.x = 12.0
        document.recompute()
        if abs(float(consumer.Placement.Base.x) - 12.0) > 1e-9:
            _fail("wave_c/app/edit")
    elif operation is AppFamilyOperation.TEXT_DOCUMENT:
        feature.Text = "Edited bounded text"
    elif operation is AppFamilyOperation.SCALAR_VARIABLE_SET:
        consumer = document.addObject("Part::Feature", "Consumer")
        consumer.addProperty("App::PropertyFloat", "Observed")
        consumer.setExpression("Observed", f"{feature.Name}.Value")
        feature.Value = 25.0
        document.recompute()
        if abs(float(consumer.Observed) - 25.0) > 1e-9:
            _fail("wave_c/app/edit")
    else:
        _fail("wave_c/app/edit")
    document.recompute()
    after = _app_state(feature, operation)
    if after == before or not after["root_owned"] or not after["acyclic"]:
        _fail("wave_c/app/edit")
    return {
        "strategy": operation.value,
        "before_sha256": _sha(
            b"vibecad-reviewed-wave-c-app-state-v1\0",
            _canonical(before),
        ),
        "after": after,
        "link_or_expression_propagated": operation
        in {
            AppFamilyOperation.OBJECT_LINK,
            AppFamilyOperation.POSITIONED_PART,
            AppFamilyOperation.PLACEMENT_REFERENCE,
            AppFamilyOperation.SCALAR_VARIABLE_SET,
        },
    }


def _save_reopen_app(
    freecad: object,
    document: object,
    feature: object,
    operation: AppFamilyOperation,
    path: Path,
    owned: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    before = _app_state(feature, operation)
    object_name = feature.Name
    document.saveAs(str(path))
    if not path.is_file() or path.stat().st_size <= 0:
        _fail("wave_c/app/save")
    object_count = len(document.Objects)
    freecad.closeDocument(document.Name)
    reopened = freecad.openDocument(str(path))
    owned[reopened.Name] = reopened
    try:
        reopened.recompute()
        reopened_feature = reopened.getObject(object_name)
        if (
            reopened_feature is None
            or reopened_feature.TypeId != APP_FAMILY_NATIVE_TYPE_IDS[operation]
            or not reopened_feature.isValid()
        ):
            _fail("wave_c/app/reopen")
        after = _app_state(reopened_feature, operation)
        if after != before:
            _fail("wave_c/app/reopen")
    finally:
        freecad.closeDocument(reopened.Name)
    return (
        {"format": "FCStd", "nonempty": True, "object_count": object_count},
        {"native_type_id": APP_FAMILY_NATIVE_TYPE_IDS[operation], "state": after},
    )


class _LateOwnershipObserver:
    __slots__ = ("group",)

    def __init__(self, group: object) -> None:
        self.group = group

    def slotCreatedObject(self, item: object) -> None:  # noqa: N802
        if item.TypeId == "App::Annotation":
            self.group.addObject(item)


class _LateCycleObserver:
    __slots__ = ("related",)

    def __init__(self, related: object) -> None:
        self.related = related

    def slotCreatedObject(self, item: object) -> None:  # noqa: N802
        if item.TypeId == "App::DocumentObjectGroup":
            self.related.addObject(item)


def _app_late_rollback(
    freecad: object,
    operation: AppFamilyOperation,
    plan: AppFamilyBackendPlan,
) -> dict[str, object]:
    owned: dict[str, object] = {}
    observer: object | None = None
    original_validator = app_rules._validate_feature  # noqa: SLF001
    late_validation_reached = False
    try:
        document = freecad.newDocument(f"VerifyAppRollback_{operation.value}")
        owned[document.Name] = document
        document.UndoMode = 1
        if operation is AppFamilyOperation.DOCUMENT_GROUP:
            related = document.addObject("App::DocumentObjectGroup", "Related")
            observer = _LateCycleObserver(related)
            freecad.addDocumentObserver(observer)
            injection_point = "observer-created-link-cycle"
        else:
            related = _app_related(document, operation)
        if operation is AppFamilyOperation.TEXT_ANNOTATION:
            guard = document.addObject("App::DocumentObjectGroup", "GuardGroup")
            observer = _LateOwnershipObserver(guard)
            freecad.addDocumentObserver(observer)
            injection_point = "observer-root-ownership-violation"
        elif operation is not AppFamilyOperation.DOCUMENT_GROUP:
            injection_point = "post-create-native-validator"

            def fail_after_native_create(*_args: object, **_kwargs: object) -> None:
                nonlocal late_validation_reached
                late_validation_reached = True
                raise AppFamilyRuleError(
                    AppFamilyRuleErrorCode.CONFORMANCE_FAILED,
                    "/result/injected-late-failure",
                )

            app_rules._validate_feature = fail_after_native_create  # noqa: SLF001
        before = _app_document_snapshot(document)
        payload = plan.canonical_bytes
        try:
            apply_app_family_plan(
                payload,
                expected_content_sha256=hashlib.sha256(payload).hexdigest(),
                expected_plan_sha256=plan.plan_sha256,
                bindings=_app_bindings(document, plan, related),
            )
        except AppFamilyRuleError:
            pass
        else:
            _fail("wave_c/app/late_rollback")
        if observer is not None:
            late_validation_reached = True
        if (
            not late_validation_reached
            or not _app_snapshot_matches(document, before)
            or bool(document.HasPendingTransaction)
        ):
            _fail("wave_c/app/late_rollback")
        return {
            "injection_point": injection_point,
            "native_mutation_reached": True,
            "objects_restored": len(before[0]),
            "ownership_or_links_restored": True,
            "pending_transaction": False,
        }
    finally:
        app_rules._validate_feature = original_validator  # noqa: SLF001
        if observer is not None:
            try:
                freecad.removeDocumentObserver(observer)
            except BaseException:
                _fail("wave_c/app/observer")
        _close_owned_documents(freecad, owned)


class _AppExecutor:
    __slots__ = ("_cache", "_fixtures", "_freecad")

    def __init__(self, freecad: object) -> None:
        self._freecad = freecad
        self._fixtures = {item.operation_id: item for item in _APP_FIXTURES}
        self._cache: dict[str, dict[ReviewedConformanceFacet, dict[str, object]]] = {}

    def __call__(self, case: ReviewedConformanceCase, challenge_sha256: str) -> bytes:
        fixture = self._fixtures.get(case.operation_id)
        if fixture is None:
            _fail("wave_c/app/case")
        outcomes = self._cache.get(case.operation_id)
        if outcomes is None:
            outcomes = self._run(AppFamilyOperation(case.operation_id))
            self._cache[case.operation_id] = outcomes
        return _case_observation(
            case=case,
            challenge_sha256=challenge_sha256,
            fixture=fixture,
            evidence=outcomes[case.facet],
        )

    def _run(
        self,
        operation: AppFamilyOperation,
    ) -> dict[ReviewedConformanceFacet, dict[str, object]]:
        freecad = self._freecad
        owned: dict[str, object] = {}
        outcomes: dict[ReviewedConformanceFacet, dict[str, object]] = {}
        try:
            with tempfile.TemporaryDirectory(prefix="vibecad-wave-c-app-") as temporary:
                plan = _app_plan(operation)
                payload = plan.canonical_bytes
                decoded = decode_app_family_backend_plan(
                    payload,
                    expected_content_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_plan_sha256=plan.plan_sha256,
                )
                if decoded != plan:
                    _fail("wave_c/app/readback")
                document = freecad.newDocument(f"VerifyApp_{operation.value}")
                owned[document.Name] = document
                document.UndoMode = 1
                related = _app_related(document, operation)
                bindings = _app_bindings(document, plan, related)
                before_negative = _app_document_snapshot(document)
                try:
                    apply_app_family_plan(
                        payload + b" ",
                        expected_content_sha256=hashlib.sha256(payload).hexdigest(),
                        expected_plan_sha256=plan.plan_sha256,
                        bindings=bindings,
                    )
                except AppFamilyRuleError:
                    pass
                else:
                    _fail("wave_c/app/negative")
                if not _app_snapshot_matches(document, before_negative):
                    _fail("wave_c/app/negative")
                outcomes[ReviewedConformanceFacet.NEGATIVE] = {
                    "tamper": "append-noncanonical-byte",
                    "mutation_count": 0,
                    "ownership_and_links_preserved": True,
                }

                receipt = apply_app_family_plan(
                    payload,
                    expected_content_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_plan_sha256=plan.plan_sha256,
                    bindings=bindings,
                )
                feature = document.getObject(receipt.object_name)
                created_state = _app_state(feature, operation)
                if (
                    created_state["type_id"] != APP_FAMILY_NATIVE_TYPE_IDS[operation]
                    or not created_state["valid"]
                    or not created_state["root_owned"]
                    or not created_state["acyclic"]
                    or created_state["expression_count"] != 0
                ):
                    _fail("wave_c/app/create")
                outcomes[ReviewedConformanceFacet.CREATE] = {
                    "native_receipt_sha256": receipt.receipt_sha256,
                    "owned_object_count": len(receipt.owned_object_names),
                    "state": created_state,
                    "configuration_sha256": hashlib.sha256(
                        plan.configuration_bytes
                    ).hexdigest(),
                }

                before_recompute = _app_state(feature, operation)
                document.recompute()
                after_recompute = _app_state(feature, operation)
                if after_recompute != before_recompute:
                    _fail("wave_c/app/recompute")
                outcomes[ReviewedConformanceFacet.RECOMPUTE] = {
                    "state_sha256": _sha(
                        b"vibecad-reviewed-wave-c-app-state-v1\0",
                        _canonical(after_recompute),
                    ),
                    "valid": True,
                }

                outcomes[ReviewedConformanceFacet.EDIT] = _edit_app_feature(
                    freecad,
                    document,
                    feature,
                    related,
                    operation,
                )
                saved, reopened = _save_reopen_app(
                    freecad,
                    document,
                    feature,
                    operation,
                    Path(temporary) / f"app-{operation.value}.FCStd",
                    owned,
                )
                outcomes[ReviewedConformanceFacet.SAVE] = saved
                outcomes[ReviewedConformanceFacet.REOPEN] = reopened
                outcomes[ReviewedConformanceFacet.LATE_ROLLBACK] = _app_late_rollback(
                    freecad,
                    operation,
                    plan,
                )
        finally:
            _close_owned_documents(freecad, owned)
        if set(outcomes) != set(ReviewedConformanceFacet):
            _fail("wave_c/app/outcomes")
        return outcomes


def _build_managed(
    *,
    freecad: object,
    manifest: FamilyBatchManifest,
    case_manifest: ReviewedConformanceCaseManifest,
    executor: object,
) -> tuple[ReviewedVerificationReceipt, FreeCadPromotionVerificationBinding]:
    host = build_managed_freecad_conformance_host(
        freecad=freecad,
        case_manifest=case_manifest,
        execute_case=executor,
        verifier_id=WAVE_C_VERIFIER_ID,
        verifier_version=WAVE_C_VERIFIER_VERSION,
    )
    receipt = build_reviewed_verification_receipt(
        manifest=manifest,
        case_manifest=case_manifest,
        host=host,
    )
    return receipt, build_promotion_verification_binding(receipt)


def build_app_family_managed_verification(
    *, freecad: object
) -> tuple[ReviewedVerificationReceipt, FreeCadPromotionVerificationBinding]:
    """Run the exact ten-operation/70-case App matrix.

    Sketch descriptors and their reviewed executor live beside this function,
    but a managed Sketch receipt is intentionally not exposed until the
    existing native Slot solver instability is fixed under a new exact rule
    contract.  The returned App receipt and binding remain ephemeral.
    """

    if not _VERIFICATION_LOCK.acquire(blocking=False):
        _fail("wave_c/concurrent_verification")
    try:
        return _build_managed(
            freecad=freecad,
            manifest=APP_FAMILY_MANIFEST,
            case_manifest=APP_REVIEWED_HOST_CASE_MANIFEST,
            executor=_AppExecutor(freecad),
        )
    finally:
        _VERIFICATION_LOCK.release()


__all__ = (
    "APP_REVIEWED_HOST_CASE_MANIFEST",
    "SKETCH_REVIEWED_HOST_CASE_MANIFEST",
    "WAVE_C_VERIFIER_ID",
    "WAVE_C_VERIFIER_VERSION",
    "WaveCFixtureDescriptor",
    "build_app_family_managed_verification",
)
