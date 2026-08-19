"""Managed REVIEWED_HOST verification wave for reviewed Part families.

This module owns a closed, content-bound case pack and the corresponding
same-process FreeCAD executor.  The public entry point accepts only the
authenticated ``FreeCAD`` module; callers cannot provide observations or a
case callback.  Receipts remain authority-free and are never persisted or
applied to the production capability registry here.
"""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final

from vibecad.execution.freecad_reviewed_verification import (
    MAX_REVIEWED_OBSERVATION_BYTES,
    REQUIRED_REVIEWED_CONFORMANCE_FACETS,
    ReviewedConformanceCase,
    ReviewedConformanceCaseManifest,
    ReviewedConformanceFacet,
    ReviewedVerificationReceipt,
    _admit_reviewed_host_conformance_case_manifest,
    build_managed_freecad_conformance_host,
    build_reviewed_verification_receipt,
)
from vibecad.intent_bridge.freecad_part_datum_adapter import PART_DATUM_MANIFEST
from vibecad.intent_bridge.freecad_part_dressup_adapter import PART_DRESSUP_MANIFEST
from vibecad.intent_bridge.freecad_part_profile_surface_adapter import (
    PART_PROFILE_SURFACE_MANIFEST,
)
from vibecad.intent_bridge.freecad_partdesign_residual_adapter import (
    PARTDESIGN_RESIDUAL_MANIFEST,
)
from vibecad.intent_bridge.reviewed_family_engine import FamilyBatchManifest
from vibecad.parametric.freecad_part_datum_rules import (
    ExplicitDatumPlacement,
    PartDatumBackendPlan,
    PartDatumExecutionBindings,
    PartDatumOperation,
    PartDatumRuleError,
    apply_part_datum_plan,
)
from vibecad.parametric.freecad_part_dressup_rules import (
    PartDressupBackendPlan,
    PartDressupExecutionBindings,
    PartDressupOperation,
    PartDressupRuleError,
    PartDressupSelectionRole,
    apply_part_dressup_plan,
)
from vibecad.parametric.freecad_part_profile_surface_rules import (
    PART_PROFILE_SURFACE_NATIVE_SPECS,
    AuthenticatedPartProfileSurfaceObject,
    PartProfileSurfaceBackendPlan,
    PartProfileSurfaceExecutionBindings,
    PartProfileSurfaceOperation,
    PartProfileSurfaceParameterSet,
    PartProfileSurfaceRuleError,
    PartProfileSurfaceSelection,
    PartProfileSurfaceSourceRole,
    apply_part_profile_surface_plan,
)
from vibecad.parametric.freecad_partdesign_residual_rules import (
    PARTDESIGN_RESIDUAL_NATIVE_TYPE_IDS,
    AuthenticatedResidualObject,
    ExplicitPlacement,
    HoleExtent,
    PartDesignResidualBackendPlan,
    PartDesignResidualExecutionBindings,
    PartDesignResidualOperation,
    PartDesignResidualRuleError,
    RevolutionAxis,
    SemanticObjectSelection,
    apply_partdesign_residual_plan,
)

PART_B_VERIFIER_ID: Final = "vcad.managed.freecad.reviewed-part-b"
PART_B_VERIFIER_VERSION: Final = "1.0.0"
PART_B_CASE_SCHEMA_VERSION: Final = 1
_CASE_CONTRACT_DOMAIN = b"vibecad.reviewed-freecad.part-b.case-contract.v1\0"
_PACK_CONTRACT_DOMAIN = b"vibecad.reviewed-freecad.part-b.pack-contract.v1\0"
_FIXTURE_CONTRACT_VERSION = "1.0.0"


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _canonical(value: object, *, maximum: int = MAX_REVIEWED_OBSERVATION_BYTES) -> bytes:
    raw = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if not raw or len(raw) > maximum:
        raise RuntimeError("Part B verification observation exceeds its fixed budget")
    return raw


def _operation_spec(manifest: FamilyBatchManifest, operation_id: str):
    matches = tuple(item for item in manifest.operations if item.operation_id == operation_id)
    if len(matches) != 1:
        raise RuntimeError("Part B operation manifest closure is invalid")
    return matches[0]


def _common_plan_fields(manifest: FamilyBatchManifest, operation_id: str) -> dict[str, str]:
    return {
        "source_artifact_id": f"artifact_verify_{manifest.family_id}_{operation_id}",
        "source_graph_id": f"graph_verify_{manifest.family_id}_{operation_id}",
        "source_graph_sha256": _sha(f"part-b:{manifest.family_id}:{operation_id}:graph"),
        "source_content_sha256": _sha(f"part-b:{manifest.family_id}:{operation_id}:source"),
        "lowering_request_sha256": _sha(f"part-b:{manifest.family_id}:{operation_id}:request"),
        "adapter_contract_sha256": manifest.adapter.adapter_contract_sha256,
        "manifest_sha256": manifest.manifest_sha256,
    }


def _residual_plan(operation: PartDesignResidualOperation) -> PartDesignResidualBackendPlan:
    common = _common_plan_fields(PARTDESIGN_RESIDUAL_MANIFEST, operation.value)
    base = SemanticObjectSelection(node_id="node_base", result_id="result_base")
    profile = SemanticObjectSelection(node_id="node_profile", result_id="result_profile")
    if operation is PartDesignResidualOperation.HOLE:
        return PartDesignResidualBackendPlan(
            **common,
            body_id="body_verify",
            node_id="node_hole",
            result_id="result_hole",
            operation=operation,
            base=base,
            profile=profile,
            hole_extent=HoleExtent.DIMENSION,
            diameter_mm=6.0,
            depth_mm=5.0,
        )
    if operation is PartDesignResidualOperation.REVOLUTION:
        return PartDesignResidualBackendPlan(
            **common,
            body_id="body_verify",
            node_id="node_revolution",
            result_id="result_revolution",
            operation=operation,
            profile=profile,
            axis_reference_id="reference_sketch_horizontal_axis",
            axis_result_id="result_sketch_horizontal_axis",
            revolution_axis=RevolutionAxis.HORIZONTAL,
            angle_degrees=270.0,
        )
    return PartDesignResidualBackendPlan(
        **common,
        body_id="body_verify",
        node_id="node_coordinate_system",
        result_id="result_coordinate_system",
        operation=operation,
        placement=ExplicitPlacement(
            position_mm=(10.0, 20.0, 30.0),
            axis=(0.0, 0.0, 1.0),
            angle_degrees=15.0,
        ),
    )


def _datum_plan(operation: PartDatumOperation) -> PartDatumBackendPlan:
    return PartDatumBackendPlan(
        **_common_plan_fields(PART_DATUM_MANIFEST, operation.value),
        container_id="document_root",
        node_id=f"node_{operation.value}",
        result_id=f"result_{operation.value}",
        operation=operation,
        placement=ExplicitDatumPlacement(
            position_mm=(10.0, 20.0, 30.0),
            axis=(0.0, 0.0, 1.0),
            angle_degrees=15.0,
        ),
    )


def _dressup_plan(operation: PartDressupOperation) -> PartDressupBackendPlan:
    role = (
        PartDressupSelectionRole.OUTER_MAX_Z_PLANAR_FACE
        if operation is PartDressupOperation.FACE_THICKNESS
        else PartDressupSelectionRole.OUTER_MAX_X_MAX_Y_PARALLEL_Z
    )
    return PartDressupBackendPlan(
        **_common_plan_fields(PART_DRESSUP_MANIFEST, operation.value),
        container_id="document_root",
        source_node_id="node_source",
        source_solid_result_id="result_source_solid",
        source_selection_result_id="result_source_selection",
        semantic_reference_id="reference_reviewed_role",
        target_node_id=f"node_{operation.value}",
        target_result_id=f"result_{operation.value}",
        operation=operation,
        selection_role=role,
        magnitude_mm=2.0,
    )


_PROFILE_PARAMETERS: Final = {
    PartProfileSurfaceOperation.EXTRUSION: {
        "direction": [0.0, 0.0, 1.0],
        "forward_length_mm": 8.0,
        "reverse_length_mm": 0.0,
    },
    PartProfileSurfaceOperation.REVOLUTION: {
        "axis_origin_mm": [0.0, 0.0, 0.0],
        "axis_direction": [0.0, 0.0, 1.0],
        "angle_degrees": 270.0,
    },
    PartProfileSurfaceOperation.LOFT: {"ruled": False},
    PartProfileSurfaceOperation.SWEEP: {"frenet": True},
    PartProfileSurfaceOperation.RULED_SURFACE: {},
    PartProfileSurfaceOperation.FACE: {},
}


def _profile_source_count(
    operation: PartProfileSurfaceOperation,
    role: PartProfileSurfaceSourceRole,
    minimum: int,
) -> int:
    if (
        operation is PartProfileSurfaceOperation.SWEEP
        and role is PartProfileSurfaceSourceRole.PROFILE
    ):
        return 2
    return minimum


def _profile_plan(operation: PartProfileSurfaceOperation) -> PartProfileSurfaceBackendPlan:
    selections: list[PartProfileSurfaceSelection] = []
    for requirement in PART_PROFILE_SURFACE_NATIVE_SPECS[operation].source_requirements:
        for ordinal in range(
            _profile_source_count(operation, requirement.role, requirement.minimum)
        ):
            selections.append(
                PartProfileSurfaceSelection(
                    role=requirement.role,
                    node_id=f"node_source_{requirement.role.value}_{ordinal}",
                    result_id=f"result_source_{requirement.role.value}_{ordinal}",
                    ordinal=ordinal,
                )
            )
    specification = _operation_spec(PART_PROFILE_SURFACE_MANIFEST, operation.value)
    return PartProfileSurfaceBackendPlan(
        **_common_plan_fields(PART_PROFILE_SURFACE_MANIFEST, operation.value),
        operation_specification_sha256=specification.specification_sha256,
        body_id="document_root",
        node_id=f"node_{operation.value}",
        result_id=f"result_{operation.value}",
        parameter_id=f"parameter_{operation.value}",
        value_id=f"value_{operation.value}",
        operation=operation,
        sources=tuple(selections),
        parameters=PartProfileSurfaceParameterSet.from_value(
            operation,
            _PROFILE_PARAMETERS[operation],
        ),
    )


_PLAN_BY_FAMILY_OPERATION: Final = MappingProxyType(
    {
        **{
            (PARTDESIGN_RESIDUAL_MANIFEST.family_id, operation.value): _residual_plan(operation)
            for operation in PartDesignResidualOperation
        },
        **{
            (PART_DATUM_MANIFEST.family_id, operation.value): _datum_plan(operation)
            for operation in PartDatumOperation
        },
        **{
            (PART_DRESSUP_MANIFEST.family_id, operation.value): _dressup_plan(operation)
            for operation in PartDressupOperation
        },
        **{
            (PART_PROFILE_SURFACE_MANIFEST.family_id, operation.value): _profile_plan(operation)
            for operation in PartProfileSurfaceOperation
        },
    }
)

PART_B_FAMILY_MANIFESTS: Final = (
    PARTDESIGN_RESIDUAL_MANIFEST,
    PART_DATUM_MANIFEST,
    PART_DRESSUP_MANIFEST,
    PART_PROFILE_SURFACE_MANIFEST,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartBReviewedCaseDescriptor:
    family_id: str
    family_manifest_sha256: str
    operation_id: str
    operation_specification_sha256: str
    native_type_id: str
    facet: ReviewedConformanceFacet
    fixture_contract_version: str
    fixture_plan_sha256: str
    case_contract_sha256: str = field(init=False)
    case: ReviewedConformanceCase = field(init=False, repr=False)

    def __post_init__(self) -> None:
        manifest_matches = tuple(
            item for item in PART_B_FAMILY_MANIFESTS if item.family_id == self.family_id
        )
        if len(manifest_matches) != 1:
            raise ValueError("unknown Part B family")
        manifest = manifest_matches[0]
        operation = _operation_spec(manifest, self.operation_id)
        plan = _PLAN_BY_FAMILY_OPERATION.get((self.family_id, self.operation_id))
        if (
            manifest.manifest_sha256 != self.family_manifest_sha256
            or operation.specification_sha256 != self.operation_specification_sha256
            or operation.native_type_id != self.native_type_id
            or type(self.facet) is not ReviewedConformanceFacet
            or self.fixture_contract_version != _FIXTURE_CONTRACT_VERSION
            or plan is None
            or plan.plan_sha256 != self.fixture_plan_sha256
        ):
            raise ValueError("Part B case descriptor does not close over its reviewed contract")
        body = {
            "family_id": self.family_id,
            "family_manifest_sha256": self.family_manifest_sha256,
            "facet": self.facet.value,
            "fixture_contract_version": self.fixture_contract_version,
            "fixture_plan_sha256": self.fixture_plan_sha256,
            "native_type_id": self.native_type_id,
            "operation_id": self.operation_id,
            "operation_specification_sha256": self.operation_specification_sha256,
            "schema_version": PART_B_CASE_SCHEMA_VERSION,
        }
        contract_sha256 = hashlib.sha256(
            _CASE_CONTRACT_DOMAIN + _canonical(body, maximum=8 * 1024)
        ).hexdigest()
        object.__setattr__(self, "case_contract_sha256", contract_sha256)
        object.__setattr__(
            self,
            "case",
            ReviewedConformanceCase(
                case_id=f"case.part-b.{contract_sha256[:32]}",
                operation_id=self.operation_id,
                operation_specification_sha256=self.operation_specification_sha256,
                facet=self.facet,
                case_contract_sha256=contract_sha256,
            ),
        )


def _build_descriptors() -> tuple[PartBReviewedCaseDescriptor, ...]:
    descriptors = []
    for manifest in PART_B_FAMILY_MANIFESTS:
        for operation in manifest.operations:
            plan = _PLAN_BY_FAMILY_OPERATION[(manifest.family_id, operation.operation_id)]
            for facet in REQUIRED_REVIEWED_CONFORMANCE_FACETS:
                descriptors.append(
                    PartBReviewedCaseDescriptor(
                        family_id=manifest.family_id,
                        family_manifest_sha256=manifest.manifest_sha256,
                        operation_id=operation.operation_id,
                        operation_specification_sha256=operation.specification_sha256,
                        native_type_id=operation.native_type_id,
                        facet=facet,
                        fixture_contract_version=_FIXTURE_CONTRACT_VERSION,
                        fixture_plan_sha256=plan.plan_sha256,
                    )
                )
    return tuple(descriptors)


PART_B_REVIEWED_CASE_DESCRIPTORS: Final = _build_descriptors()
PART_B_VERIFIER_CONTRACT_SHA256: Final = hashlib.sha256(
    _PACK_CONTRACT_DOMAIN
    + _canonical(
        [
            {
                "case_contract_sha256": item.case_contract_sha256,
                "case_sha256": item.case.case_sha256,
                "family_id": item.family_id,
            }
            for item in PART_B_REVIEWED_CASE_DESCRIPTORS
        ],
        maximum=256 * 1024,
    )
).hexdigest()


def _case_manifest_for(manifest: FamilyBatchManifest) -> ReviewedConformanceCaseManifest:
    return _admit_reviewed_host_conformance_case_manifest(
        manifest=manifest,
        cases=tuple(
            item.case
            for item in PART_B_REVIEWED_CASE_DESCRIPTORS
            if item.family_id == manifest.family_id
        ),
    )


PART_B_REVIEWED_CASE_MANIFESTS: Final = tuple(
    _case_manifest_for(manifest) for manifest in PART_B_FAMILY_MANIFESTS
)


def _shape_facts(shape: object) -> dict[str, object]:
    box = shape.BoundBox
    return {
        "area": round(float(shape.Area), 9),
        "bounds": [
            round(float(box.XMin), 9),
            round(float(box.YMin), 9),
            round(float(box.ZMin), 9),
            round(float(box.XLength), 9),
            round(float(box.YLength), 9),
            round(float(box.ZLength), 9),
        ],
        "edges": len(tuple(shape.Edges)),
        "faces": len(tuple(shape.Faces)),
        "shape_type": str(shape.ShapeType),
        "solids": len(tuple(shape.Solids)),
        "valid": bool(shape.isValid()) and not bool(shape.isNull()),
        "volume": round(float(shape.Volume), 9),
    }


def _different_shape(left: dict[str, object], right: dict[str, object]) -> bool:
    return left != right


def _document_snapshot(document: object) -> tuple[object, ...]:
    objects = tuple(document.Objects)
    return (
        objects,
        tuple(
            (item, tuple(item.Group)) for item in objects if "Group" in tuple(item.PropertiesList)
        ),
        tuple(
            (item, bool(item.Visibility))
            for item in objects
            if "Visibility" in tuple(item.PropertiesList)
        ),
        tuple(
            (item, _shape_facts(item.Shape))
            for item in objects
            if "Shape" in tuple(item.PropertiesList) and not item.Shape.isNull()
        ),
        tuple((item, item.Tip) for item in objects if "Tip" in tuple(item.PropertiesList)),
        bool(document.HasPendingTransaction),
    )


def _same_document_snapshot(document: object, before: tuple[object, ...]) -> bool:
    objects, groups, visibility, shapes, tips, pending = before
    return (
        tuple(document.Objects) == objects
        and all(tuple(item.Group) == members for item, members in groups)
        and all(bool(item.Visibility) is value for item, value in visibility)
        and all(_shape_facts(item.Shape) == value for item, value in shapes)
        and all(item.Tip is value for item, value in tips)
        and bool(document.HasPendingTransaction) is pending
    )


def _observation(
    descriptor: PartBReviewedCaseDescriptor,
    challenge_sha256: str,
    facts: dict[str, object],
) -> bytes:
    return _canonical(
        {
            "case_contract_sha256": descriptor.case_contract_sha256,
            "case_sha256": descriptor.case.case_sha256,
            "challenge_sha256": challenge_sha256,
            "direct_observation": facts,
            "evidence": "managed_freecad_same_process",
            "family_id": descriptor.family_id,
            "facet": descriptor.facet.value,
            "operation_id": descriptor.operation_id,
            "schema_version": PART_B_CASE_SCHEMA_VERSION,
            "verifier_contract_sha256": PART_B_VERIFIER_CONTRACT_SHA256,
        }
    )


def _require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _content_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _add_rectangle_sketch(
    freecad: object,
    part: object,
    sketcher: object,
    sketch: object,
) -> None:
    points = (
        freecad.Vector(4.0, 2.0, 0.0),
        freecad.Vector(8.0, 2.0, 0.0),
        freecad.Vector(8.0, 6.0, 0.0),
        freecad.Vector(4.0, 6.0, 0.0),
    )
    for index, start in enumerate(points):
        sketch.addGeometry(part.LineSegment(start, points[(index + 1) % 4]), False)
    for index in range(4):
        sketch.addConstraint(sketcher.Constraint("Coincident", index, 2, (index + 1) % 4, 1))


def _residual_fixture(
    freecad: object,
    part: object,
    sketcher: object,
    plan: PartDesignResidualBackendPlan,
) -> tuple[object, object, object | None, object | None, PartDesignResidualExecutionBindings]:
    document = freecad.newDocument(f"VerifyResidual_{plan.operation.value}_{plan.plan_sha256[:8]}")
    document.UndoMode = 1
    body = document.addObject("PartDesign::Body", "Body")
    base = None
    profile = None
    if plan.operation is PartDesignResidualOperation.HOLE:
        base = body.newObject("PartDesign::AdditiveBox", "Base")
        base.Length = 30.0
        base.Width = 20.0
        base.Height = 10.0
        document.recompute()
        profile = body.newObject("Sketcher::SketchObject", "HoleProfile")
        profile.AttachmentSupport = [(base, ["Face6"])]
        profile.MapMode = "FlatFace"
        profile.addGeometry(
            part.Circle(
                freecad.Vector(15.0, 10.0, 0.0),
                freecad.Vector(0.0, 0.0, 1.0),
                3.0,
            ),
            False,
        )
        document.recompute()
    elif plan.operation is PartDesignResidualOperation.REVOLUTION:
        profile = body.newObject("Sketcher::SketchObject", "RevolutionProfile")
        _add_rectangle_sketch(freecad, part, sketcher, profile)
        document.recompute()
    bindings = PartDesignResidualExecutionBindings(
        document=document,
        body=body,
        body_id=plan.body_id,
        base=(
            None
            if base is None or plan.base is None
            else AuthenticatedResidualObject(
                object=base,
                node_id=plan.base.node_id,
                result_id=plan.base.result_id,
            )
        ),
        profile=(
            None
            if profile is None or plan.profile is None
            else AuthenticatedResidualObject(
                object=profile,
                node_id=plan.profile.node_id,
                result_id=plan.profile.result_id,
            )
        ),
    )
    return document, body, base, profile, bindings


def _apply_residual(
    plan: PartDesignResidualBackendPlan,
    bindings: PartDesignResidualExecutionBindings,
):
    raw = plan.canonical_bytes
    return apply_partdesign_residual_plan(
        raw,
        expected_content_sha256=_content_sha256(raw),
        expected_plan_sha256=plan.plan_sha256,
        bindings=bindings,
    )


def _execute_residual(
    freecad: object,
    part: object,
    sketcher: object,
    descriptor: PartBReviewedCaseDescriptor,
    temporary_root: Path,
) -> dict[str, object]:
    operation = PartDesignResidualOperation(descriptor.operation_id)
    plan = _PLAN_BY_FAMILY_OPERATION[(descriptor.family_id, descriptor.operation_id)]
    _require(type(plan) is PartDesignResidualBackendPlan, "residual fixture plan type drift")
    _require(
        plan.plan_sha256 == descriptor.fixture_plan_sha256,
        "residual fixture plan contract drift",
    )
    document, body, base, profile, bindings = _residual_fixture(freecad, part, sketcher, plan)
    raw = plan.canonical_bytes
    if descriptor.facet is ReviewedConformanceFacet.NEGATIVE:
        before = _document_snapshot(document)
        try:
            apply_partdesign_residual_plan(
                raw + b" ",
                expected_content_sha256=_content_sha256(raw),
                expected_plan_sha256=plan.plan_sha256,
                bindings=bindings,
            )
        except PartDesignResidualRuleError as error:
            _require(_same_document_snapshot(document, before), "residual reject mutated document")
            return {
                "error_code": error.code.value,
                "error_path": error.path,
                "object_count": len(tuple(document.Objects)),
                "rollback_exact": True,
            }
        raise RuntimeError("tampered residual plan was accepted")
    if descriptor.facet is ReviewedConformanceFacet.LATE_ROLLBACK:
        expected_type = PARTDESIGN_RESIDUAL_NATIVE_TYPE_IDS[operation]

        class LateStateObserver:
            fired = False

            def slotRecomputedDocument(self, recomputed: object) -> None:
                for item in tuple(recomputed.Objects):
                    if not self.fired and item.TypeId == expected_type:
                        self.fired = True
                        item.touch()

        observer = LateStateObserver()
        freecad.addDocumentObserver(observer)
        before = _document_snapshot(document)
        try:
            try:
                _apply_residual(plan, bindings)
            except PartDesignResidualRuleError as error:
                _require(observer.fired, "residual sabotage observer did not fire")
                _require(
                    _same_document_snapshot(document, before),
                    "residual late failure did not restore exact snapshot",
                )
                return {
                    "error_code": error.code.value,
                    "error_path": error.path,
                    "object_count": len(tuple(document.Objects)),
                    "rollback_exact": True,
                    "sabotage_observed": True,
                }
            raise RuntimeError("residual late state sabotage was accepted")
        finally:
            freecad.removeDocumentObserver(observer)

    receipt = _apply_residual(plan, bindings)
    feature = document.getObject(receipt.object_name)
    _require(
        feature is not None
        and feature.TypeId == PARTDESIGN_RESIDUAL_NATIVE_TYPE_IDS[operation]
        and feature.isValid(),
        "residual create readback failed",
    )
    if descriptor.facet is ReviewedConformanceFacet.CREATE:
        return {
            "native_type_id": feature.TypeId,
            "object_name": feature.Name,
            "owned_by_body": feature in tuple(body.Group),
            "receipt_sha256": receipt.receipt_sha256,
            "shape": None
            if operation is PartDesignResidualOperation.COORDINATE_SYSTEM
            else _shape_facts(feature.Shape),
        }
    if descriptor.facet is ReviewedConformanceFacet.EDIT:
        before_shape = (
            None
            if operation is PartDesignResidualOperation.COORDINATE_SYSTEM
            else _shape_facts(feature.Shape)
        )
        if operation is PartDesignResidualOperation.HOLE:
            feature.Diameter = 8.0
        elif operation is PartDesignResidualOperation.REVOLUTION:
            feature.Angle = 180.0
        else:
            feature.Placement = freecad.Placement(
                freecad.Vector(12.0, 22.0, 32.0),
                freecad.Rotation(freecad.Vector(1.0, 0.0, 0.0), 30.0),
            )
        document.recompute()
        if before_shape is not None:
            after_shape = _shape_facts(feature.Shape)
            _require(_different_shape(before_shape, after_shape), "residual edit did not propagate")
        else:
            after_shape = None
            _require(math.isclose(float(feature.Placement.Base.x), 12.0), "LCS edit failed")
        return {
            "native_type_id": feature.TypeId,
            "shape_before": before_shape,
            "shape_after": after_shape,
            "state": tuple(feature.State),
        }
    if descriptor.facet is ReviewedConformanceFacet.RECOMPUTE:
        before_shape = (
            None
            if operation is PartDesignResidualOperation.COORDINATE_SYSTEM
            else _shape_facts(feature.Shape)
        )
        if operation is PartDesignResidualOperation.HOLE:
            _require(base is not None, "hole base missing")
            base.Length = 34.0
        elif operation is PartDesignResidualOperation.REVOLUTION:
            _require(profile is not None, "revolution profile missing")
            profile.Placement = freecad.Placement(freecad.Vector(0.0, 0.0, 3.0), freecad.Rotation())
        else:
            consumer = document.addObject("Part::Feature", "Consumer")
            consumer.setExpression("Placement.Base.x", f"{feature.Name}.Placement.Base.x")
            feature.Placement = freecad.Placement(
                freecad.Vector(44.0, 20.0, 30.0), freecad.Rotation()
            )
        document.recompute()
        if before_shape is not None:
            after_shape = _shape_facts(feature.Shape)
            _require(
                _different_shape(before_shape, after_shape),
                "residual upstream recompute did not propagate",
            )
            propagated = after_shape
        else:
            propagated = round(float(consumer.Placement.Base.x), 9)
            _require(math.isclose(float(propagated), 44.0), "LCS consumer propagation failed")
        return {"native_type_id": feature.TypeId, "propagated": propagated}
    save_path = temporary_root / f"{descriptor.case.case_sha256}.FCStd"
    document.saveAs(str(save_path))
    size = save_path.stat().st_size
    _require(size > 0, "residual save produced an empty file")
    if descriptor.facet is ReviewedConformanceFacet.SAVE:
        return {
            "format": "FCStd",
            "native_type_id": feature.TypeId,
            "nonempty": True,
            "saved": True,
        }
    _require(descriptor.facet is ReviewedConformanceFacet.REOPEN, "unexpected residual facet")
    object_name = feature.Name
    freecad.closeDocument(document.Name)
    reopened = freecad.openDocument(str(save_path))
    reopened_feature = reopened.getObject(object_name)
    _require(
        reopened_feature is not None
        and reopened_feature.TypeId == PARTDESIGN_RESIDUAL_NATIVE_TYPE_IDS[operation]
        and reopened_feature.isValid(),
        "residual reopen readback failed",
    )
    return {
        "format": "FCStd",
        "native_type_id": reopened_feature.TypeId,
        "nonempty": True,
        "reopened": True,
        "state": tuple(reopened_feature.State),
    }


def _apply_datum(plan: PartDatumBackendPlan, document: object):
    raw = plan.canonical_bytes
    return apply_part_datum_plan(
        raw,
        expected_content_sha256=_content_sha256(raw),
        expected_plan_sha256=plan.plan_sha256,
        bindings=PartDatumExecutionBindings(
            document=document,
            container_id=plan.container_id,
        ),
    )


def _execute_datum(
    freecad: object,
    descriptor: PartBReviewedCaseDescriptor,
    temporary_root: Path,
) -> dict[str, object]:
    operation = PartDatumOperation(descriptor.operation_id)
    plan = _PLAN_BY_FAMILY_OPERATION[(descriptor.family_id, descriptor.operation_id)]
    _require(type(plan) is PartDatumBackendPlan, "datum fixture plan type drift")
    _require(
        plan.plan_sha256 == descriptor.fixture_plan_sha256,
        "datum fixture plan contract drift",
    )
    document = freecad.newDocument(f"VerifyDatum_{operation.value}_{plan.plan_sha256[:8]}")
    document.UndoMode = 1
    raw = plan.canonical_bytes
    if descriptor.facet is ReviewedConformanceFacet.NEGATIVE:
        before = _document_snapshot(document)
        try:
            apply_part_datum_plan(
                raw + b" ",
                expected_content_sha256=_content_sha256(raw),
                expected_plan_sha256=plan.plan_sha256,
                bindings=PartDatumExecutionBindings(
                    document=document, container_id=plan.container_id
                ),
            )
        except PartDatumRuleError as error:
            _require(_same_document_snapshot(document, before), "datum reject mutated document")
            return {
                "error_code": error.code.value,
                "error_path": error.path,
                "object_count": len(tuple(document.Objects)),
                "rollback_exact": True,
            }
        raise RuntimeError("tampered datum plan was accepted")
    if descriptor.facet is ReviewedConformanceFacet.LATE_ROLLBACK:
        group = document.addObject("App::DocumentObjectGroup", "GuardGroup")
        expected_type = _operation_spec(PART_DATUM_MANIFEST, operation.value).native_type_id

        class LateOwnershipObserver:
            def slotCreatedObject(self, item: object) -> None:
                if item.TypeId == expected_type:
                    group.addObject(item)

        observer = LateOwnershipObserver()
        freecad.addDocumentObserver(observer)
        before = _document_snapshot(document)
        try:
            try:
                _apply_datum(plan, document)
            except PartDatumRuleError as error:
                _require(
                    _same_document_snapshot(document, before),
                    "datum late failure did not restore exact snapshot",
                )
                return {
                    "error_code": error.code.value,
                    "error_path": error.path,
                    "object_count": len(tuple(document.Objects)),
                    "rollback_exact": True,
                }
            raise RuntimeError("datum late ownership sabotage was accepted")
        finally:
            freecad.removeDocumentObserver(observer)

    receipt = _apply_datum(plan, document)
    feature = document.getObject(receipt.object_name)
    expected_type = _operation_spec(PART_DATUM_MANIFEST, operation.value).native_type_id
    _require(
        feature is not None and feature.TypeId == expected_type and feature.isValid(),
        "datum create readback failed",
    )
    if descriptor.facet is ReviewedConformanceFacet.CREATE:
        return {
            "native_type_id": feature.TypeId,
            "object_name": feature.Name,
            "owned_object_count": len(receipt.owned_object_names),
            "receipt_sha256": receipt.receipt_sha256,
            "root_owned": not tuple(feature.getParentGroup() or ()),
        }
    if descriptor.facet is ReviewedConformanceFacet.EDIT:
        feature.Placement = freecad.Placement(
            freecad.Vector(40.0, 50.0, 60.0),
            freecad.Rotation(freecad.Vector(1.0, 0.0, 0.0), 30.0),
        )
        document.recompute()
        actual = round(float(feature.Placement.Base.x), 9)
        _require(math.isclose(actual, 40.0), "datum edit readback failed")
        return {
            "native_type_id": feature.TypeId,
            "placement_x": actual,
            "state": tuple(feature.State),
        }
    if descriptor.facet is ReviewedConformanceFacet.RECOMPUTE:
        consumer = document.addObject("Part::Feature", "Consumer")
        consumer.setExpression("Placement.Base.x", f"{feature.Name}.Placement.Base.x")
        feature.Placement = freecad.Placement(freecad.Vector(44.0, 50.0, 60.0), freecad.Rotation())
        document.recompute()
        actual = round(float(consumer.Placement.Base.x), 9)
        _require(math.isclose(actual, 44.0), "datum consumer propagation failed")
        return {
            "consumer_placement_x": actual,
            "native_type_id": feature.TypeId,
            "state": tuple(feature.State),
        }
    save_path = temporary_root / f"{descriptor.case.case_sha256}.FCStd"
    document.saveAs(str(save_path))
    size = save_path.stat().st_size
    _require(size > 0, "datum save produced an empty file")
    if descriptor.facet is ReviewedConformanceFacet.SAVE:
        return {
            "format": "FCStd",
            "native_type_id": feature.TypeId,
            "nonempty": True,
            "saved": True,
        }
    _require(descriptor.facet is ReviewedConformanceFacet.REOPEN, "unexpected datum facet")
    object_name = feature.Name
    expected_owned = len(receipt.owned_object_names)
    freecad.closeDocument(document.Name)
    reopened = freecad.openDocument(str(save_path))
    reopened_feature = reopened.getObject(object_name)
    _require(
        reopened_feature is not None
        and reopened_feature.TypeId == expected_type
        and reopened_feature.isValid(),
        "datum reopen readback failed",
    )
    if operation is PartDatumOperation.LOCAL_COORDINATE_SYSTEM:
        _require(
            len(tuple(reopened_feature.OriginFeatures)) == 7,
            "datum LCS helper reopen readback failed",
        )
    return {
        "format": "FCStd",
        "native_type_id": reopened_feature.TypeId,
        "nonempty": True,
        "owned_object_count": expected_owned,
        "reopened": True,
    }


def _dressup_fixture(
    freecad: object,
    plan: PartDressupBackendPlan,
) -> tuple[object, object, PartDressupExecutionBindings]:
    document = freecad.newDocument(f"VerifyDressup_{plan.operation.value}_{plan.plan_sha256[:8]}")
    document.UndoMode = 1
    source = document.addObject("Part::Box", "Source")
    source.Length = 30.0
    source.Width = 20.0
    source.Height = 10.0
    document.recompute()
    return (
        document,
        source,
        PartDressupExecutionBindings(
            document=document,
            container_id=plan.container_id,
            source_node_id=plan.source_node_id,
            source_solid_result_id=plan.source_solid_result_id,
            source_object=source,
        ),
    )


def _apply_dressup(
    plan: PartDressupBackendPlan,
    bindings: PartDressupExecutionBindings,
):
    raw = plan.canonical_bytes
    return apply_part_dressup_plan(
        raw,
        expected_content_sha256=_content_sha256(raw),
        expected_plan_sha256=plan.plan_sha256,
        bindings=bindings,
    )


def _execute_dressup(
    freecad: object,
    descriptor: PartBReviewedCaseDescriptor,
    temporary_root: Path,
) -> dict[str, object]:
    operation = PartDressupOperation(descriptor.operation_id)
    plan = _PLAN_BY_FAMILY_OPERATION[(descriptor.family_id, descriptor.operation_id)]
    _require(type(plan) is PartDressupBackendPlan, "dressup fixture plan type drift")
    _require(
        plan.plan_sha256 == descriptor.fixture_plan_sha256,
        "dressup fixture plan contract drift",
    )
    document, source, bindings = _dressup_fixture(freecad, plan)
    raw = plan.canonical_bytes
    if descriptor.facet is ReviewedConformanceFacet.NEGATIVE:
        before = _document_snapshot(document)
        try:
            apply_part_dressup_plan(
                raw + b" ",
                expected_content_sha256=_content_sha256(raw),
                expected_plan_sha256=plan.plan_sha256,
                bindings=bindings,
            )
        except PartDressupRuleError as error:
            _require(
                _same_document_snapshot(document, before),
                "dressup reject mutated document",
            )
            return {
                "error_code": error.code.value,
                "error_path": error.path,
                "object_count": len(tuple(document.Objects)),
                "rollback_exact": True,
            }
        raise RuntimeError("tampered dressup plan was accepted")
    if descriptor.facet is ReviewedConformanceFacet.LATE_ROLLBACK:
        group = document.addObject("App::DocumentObjectGroup", "GuardGroup")
        expected_type = _operation_spec(PART_DRESSUP_MANIFEST, operation.value).native_type_id

        class LateOwnershipObserver:
            def slotCreatedObject(self, item: object) -> None:
                if item.TypeId == expected_type:
                    group.addObject(item)

        observer = LateOwnershipObserver()
        freecad.addDocumentObserver(observer)
        before = _document_snapshot(document)
        try:
            try:
                _apply_dressup(plan, bindings)
            except PartDressupRuleError as error:
                _require(
                    _same_document_snapshot(document, before),
                    "dressup late failure did not restore exact snapshot",
                )
                return {
                    "error_code": error.code.value,
                    "error_path": error.path,
                    "object_count": len(tuple(document.Objects)),
                    "rollback_exact": True,
                }
            raise RuntimeError("dressup late ownership sabotage was accepted")
        finally:
            freecad.removeDocumentObserver(observer)

    receipt = _apply_dressup(plan, bindings)
    feature = document.getObject(receipt.object_name)
    expected_type = _operation_spec(PART_DRESSUP_MANIFEST, operation.value).native_type_id
    _require(
        feature is not None and feature.TypeId == expected_type and feature.isValid(),
        "dressup create readback failed",
    )
    if descriptor.facet is ReviewedConformanceFacet.CREATE:
        return {
            "native_type_id": feature.TypeId,
            "object_name": feature.Name,
            "receipt_sha256": receipt.receipt_sha256,
            "root_owned": not tuple(feature.getParentGroup() or ()),
            "shape": _shape_facts(feature.Shape),
        }
    if descriptor.facet is ReviewedConformanceFacet.EDIT:
        before_shape = _shape_facts(feature.Shape)
        if operation is PartDressupOperation.FACE_THICKNESS:
            feature.Value = 2.5
            edited_value = round(float(feature.Value), 9)
        else:
            native_index = int(feature.Edges[0][0])
            feature.Edges = [(native_index, 3.0, 3.0)]
            edited_value = round(float(feature.Edges[0][1]), 9)
        document.recompute()
        after_shape = _shape_facts(feature.Shape)
        _require(_different_shape(before_shape, after_shape), "dressup edit did not propagate")
        return {
            "edited_value": edited_value,
            "native_type_id": feature.TypeId,
            "shape_after": after_shape,
            "shape_before": before_shape,
        }
    if descriptor.facet is ReviewedConformanceFacet.RECOMPUTE:
        before_shape = _shape_facts(feature.Shape)
        source.Length = 34.0
        source.Width = 22.0
        source.Height = 12.0
        document.recompute()
        after_shape = _shape_facts(feature.Shape)
        _require(
            feature.isValid() and _different_shape(before_shape, after_shape),
            "dressup source recompute did not propagate",
        )
        return {
            "native_type_id": feature.TypeId,
            "shape_after": after_shape,
            "shape_before": before_shape,
            "source_length": round(float(source.Length), 9),
        }
    save_path = temporary_root / f"{descriptor.case.case_sha256}.FCStd"
    document.saveAs(str(save_path))
    size = save_path.stat().st_size
    _require(size > 0, "dressup save produced an empty file")
    if descriptor.facet is ReviewedConformanceFacet.SAVE:
        return {
            "format": "FCStd",
            "native_type_id": feature.TypeId,
            "nonempty": True,
            "saved": True,
        }
    _require(descriptor.facet is ReviewedConformanceFacet.REOPEN, "unexpected dressup facet")
    object_name = feature.Name
    expected_shape = _shape_facts(feature.Shape)
    freecad.closeDocument(document.Name)
    reopened = freecad.openDocument(str(save_path))
    reopened_feature = reopened.getObject(object_name)
    _require(
        reopened_feature is not None
        and reopened_feature.TypeId == expected_type
        and reopened_feature.isValid()
        and _shape_facts(reopened_feature.Shape) == expected_shape,
        "dressup reopen readback failed",
    )
    return {
        "format": "FCStd",
        "native_type_id": reopened_feature.TypeId,
        "nonempty": True,
        "reopened": True,
        "shape": expected_shape,
    }


def _rectangle_xy(
    freecad: object,
    part: object,
    x0: float,
    y0: float,
    z: float,
    width: float,
    height: float,
):
    return part.makePolygon(
        [
            freecad.Vector(x0, y0, z),
            freecad.Vector(x0 + width, y0, z),
            freecad.Vector(x0 + width, y0 + height, z),
            freecad.Vector(x0, y0 + height, z),
            freecad.Vector(x0, y0, z),
        ]
    )


def _rectangle_xz(
    freecad: object,
    part: object,
    x0: float,
    z0: float,
    width: float,
    height: float,
):
    return part.makePolygon(
        [
            freecad.Vector(x0, 0.0, z0),
            freecad.Vector(x0 + width, 0.0, z0),
            freecad.Vector(x0 + width, 0.0, z0 + height),
            freecad.Vector(x0, 0.0, z0 + height),
            freecad.Vector(x0, 0.0, z0),
        ]
    )


def _profile_source_shape(
    freecad: object,
    part: object,
    operation: PartProfileSurfaceOperation,
    role: PartProfileSurfaceSourceRole,
    ordinal: int,
    *,
    edited: bool = False,
):
    delta = 2.0 if edited else 0.0
    if role is PartProfileSurfaceSourceRole.PROFILE:
        if operation is PartProfileSurfaceOperation.REVOLUTION:
            return _rectangle_xz(freecad, part, 2.0, 0.0, 2.0 + delta, 5.0)
        if operation is PartProfileSurfaceOperation.SWEEP:
            return part.Wire(
                [
                    part.makeCircle(
                        2.0 + ordinal + delta,
                        freecad.Vector(0.0, 0.0, 12.0 * ordinal),
                        freecad.Vector(0.0, 0.0, 1.0),
                    )
                ]
            )
        size = 4.0 + 2.0 * ordinal + delta
        return _rectangle_xy(
            freecad,
            part,
            -size / 2.0,
            -size / 2.0,
            10.0 * ordinal,
            size,
            size,
        )
    if role is PartProfileSurfaceSourceRole.SPINE:
        return part.makeLine(
            freecad.Vector(0.0, 0.0, 0.0),
            freecad.Vector(0.0, 0.0, 12.0 + delta),
        )
    if role is PartProfileSurfaceSourceRole.CURVE:
        y = 5.0 * ordinal + delta * ordinal
        z = 3.0 * ordinal + delta * ordinal
        return part.makeLine(
            freecad.Vector(0.0, y, z),
            freecad.Vector(10.0, y, z),
        )
    return _rectangle_xy(freecad, part, 0.0, 0.0, 0.0, 7.0 + delta, 5.0)


def _profile_fixture(
    freecad: object,
    part: object,
    plan: PartProfileSurfaceBackendPlan,
) -> tuple[object, list[object], PartProfileSurfaceExecutionBindings]:
    document = freecad.newDocument(f"VerifyProfile_{plan.operation.value}_{plan.plan_sha256[:8]}")
    document.UndoMode = 1
    sources: list[object] = []
    for selection in plan.sources:
        source = document.addObject(
            "Part::Feature",
            f"Source_{selection.role.value}_{selection.ordinal}",
        )
        source.Shape = _profile_source_shape(
            freecad,
            part,
            plan.operation,
            selection.role,
            selection.ordinal,
        )
        sources.append(source)
    document.recompute()
    bindings = PartProfileSurfaceExecutionBindings(
        document=document,
        body_id=plan.body_id,
        sources=tuple(
            AuthenticatedPartProfileSurfaceObject(
                object=source,
                node_id=selection.node_id,
                result_id=selection.result_id,
            )
            for source, selection in zip(sources, plan.sources, strict=True)
        ),
        expected_adapter_contract_sha256=plan.adapter_contract_sha256,
        expected_manifest_sha256=plan.manifest_sha256,
        expected_operation_specification_sha256=(plan.operation_specification_sha256),
    )
    return document, sources, bindings


def _apply_profile(
    plan: PartProfileSurfaceBackendPlan,
    bindings: PartProfileSurfaceExecutionBindings,
):
    raw = plan.canonical_bytes
    return apply_part_profile_surface_plan(
        raw,
        expected_content_sha256=_content_sha256(raw),
        expected_plan_sha256=plan.plan_sha256,
        bindings=bindings,
    )


def _edit_profile_result(
    freecad: object,
    part: object,
    plan: PartProfileSurfaceBackendPlan,
    feature: object,
    document: object,
    sources: list[object],
) -> dict[str, object]:
    operation = plan.operation
    before = _shape_facts(feature.Shape)
    if operation is PartProfileSurfaceOperation.EXTRUSION:
        feature.LengthFwd = 11.0
        edited_property = ["LengthFwd", 11.0]
    elif operation is PartProfileSurfaceOperation.REVOLUTION:
        feature.Angle = 180.0
        edited_property = ["Angle", 180.0]
    elif operation is PartProfileSurfaceOperation.LOFT:
        feature.Ruled = True
        edited_property = ["Ruled", True]
    elif operation is PartProfileSurfaceOperation.SWEEP:
        feature.Frenet = False
        edited_property = ["Frenet", False]
    elif operation is PartProfileSurfaceOperation.RULED_SURFACE:
        first = feature.Curve1
        second = feature.Curve2
        feature.Curve1 = second
        feature.Curve2 = first
        edited_property = ["CurveOrder", "reversed"]
    else:
        alternate = document.addObject("Part::Feature", "EditedBoundary")
        alternate.Shape = _profile_source_shape(
            freecad,
            part,
            operation,
            PartProfileSurfaceSourceRole.BOUNDARY,
            0,
            edited=True,
        )
        feature.Sources = [alternate]
        sources.append(alternate)
        edited_property = ["Sources", alternate.Name]
    document.recompute()
    after = _shape_facts(feature.Shape)
    _require(
        feature.isValid() and tuple(feature.State) == ("Up-to-date",),
        "profile/surface native edit is invalid",
    )
    return {
        "edited_property": edited_property,
        "shape_before": before,
        "shape_after": after,
    }


def _profile_recompute_source_index(plan: PartProfileSurfaceBackendPlan) -> int:
    if plan.operation is PartProfileSurfaceOperation.SWEEP:
        return next(
            index
            for index, selection in enumerate(plan.sources)
            if selection.role is PartProfileSurfaceSourceRole.SPINE
        )
    if plan.operation in {
        PartProfileSurfaceOperation.LOFT,
        PartProfileSurfaceOperation.RULED_SURFACE,
    }:
        return 1
    return 0


def _execute_profile(
    freecad: object,
    part: object,
    descriptor: PartBReviewedCaseDescriptor,
    temporary_root: Path,
) -> dict[str, object]:
    operation = PartProfileSurfaceOperation(descriptor.operation_id)
    plan = _PLAN_BY_FAMILY_OPERATION[(descriptor.family_id, descriptor.operation_id)]
    _require(type(plan) is PartProfileSurfaceBackendPlan, "profile fixture plan type drift")
    _require(
        plan.plan_sha256 == descriptor.fixture_plan_sha256,
        "profile fixture plan contract drift",
    )
    document, sources, bindings = _profile_fixture(freecad, part, plan)
    raw = plan.canonical_bytes
    if descriptor.facet is ReviewedConformanceFacet.NEGATIVE:
        before = _document_snapshot(document)
        try:
            apply_part_profile_surface_plan(
                raw + b" ",
                expected_content_sha256=_content_sha256(raw),
                expected_plan_sha256=plan.plan_sha256,
                bindings=bindings,
            )
        except PartProfileSurfaceRuleError as error:
            _require(
                _same_document_snapshot(document, before),
                "profile/surface reject mutated document",
            )
            return {
                "error_code": error.code.value,
                "error_path": error.path,
                "object_count": len(tuple(document.Objects)),
                "rollback_exact": True,
            }
        raise RuntimeError("tampered profile/surface plan was accepted")
    if descriptor.facet is ReviewedConformanceFacet.LATE_ROLLBACK:
        expected_type = PART_PROFILE_SURFACE_NATIVE_SPECS[operation].type_id

        class LateStateObserver:
            fired = False

            def slotRecomputedDocument(self, recomputed: object) -> None:
                for item in tuple(recomputed.Objects):
                    if not self.fired and item.TypeId == expected_type:
                        self.fired = True
                        item.touch()

        observer = LateStateObserver()
        freecad.addDocumentObserver(observer)
        before = _document_snapshot(document)
        try:
            try:
                _apply_profile(plan, bindings)
            except PartProfileSurfaceRuleError as error:
                _require(observer.fired, "profile/surface sabotage observer did not fire")
                _require(
                    _same_document_snapshot(document, before),
                    "profile/surface late failure did not restore exact snapshot",
                )
                return {
                    "error_code": error.code.value,
                    "error_path": error.path,
                    "object_count": len(tuple(document.Objects)),
                    "rollback_exact": True,
                    "sabotage_observed": True,
                }
            raise RuntimeError("profile/surface late state sabotage was accepted")
        finally:
            freecad.removeDocumentObserver(observer)

    receipt = _apply_profile(plan, bindings)
    feature = document.getObject(receipt.object_name)
    expected_type = PART_PROFILE_SURFACE_NATIVE_SPECS[operation].type_id
    _require(
        feature is not None
        and feature.TypeId == expected_type
        and feature.isValid()
        and tuple(feature.State) == ("Up-to-date",),
        "profile/surface create readback failed",
    )
    if descriptor.facet is ReviewedConformanceFacet.CREATE:
        return {
            "native_type_id": feature.TypeId,
            "object_name": feature.Name,
            "receipt_sha256": receipt.receipt_sha256,
            "shape": _shape_facts(feature.Shape),
            "source_count": len(sources),
        }
    if descriptor.facet is ReviewedConformanceFacet.EDIT:
        facts = _edit_profile_result(freecad, part, plan, feature, document, sources)
        return {"native_type_id": feature.TypeId, **facts}
    if descriptor.facet is ReviewedConformanceFacet.RECOMPUTE:
        before_shape = _shape_facts(feature.Shape)
        index = _profile_recompute_source_index(plan)
        selection = plan.sources[index]
        sources[index].Shape = _profile_source_shape(
            freecad,
            part,
            operation,
            selection.role,
            selection.ordinal,
            edited=True,
        )
        document.recompute()
        after_shape = _shape_facts(feature.Shape)
        _require(
            feature.isValid()
            and tuple(feature.State) == ("Up-to-date",)
            and _different_shape(before_shape, after_shape),
            "profile/surface upstream recompute did not propagate",
        )
        return {
            "edited_source_role": selection.role.value,
            "native_type_id": feature.TypeId,
            "shape_after": after_shape,
            "shape_before": before_shape,
        }
    save_path = temporary_root / f"{descriptor.case.case_sha256}.FCStd"
    expected_shape = _shape_facts(feature.Shape)
    document.saveAs(str(save_path))
    size = save_path.stat().st_size
    _require(size > 0, "profile/surface save produced an empty file")
    if descriptor.facet is ReviewedConformanceFacet.SAVE:
        return {
            "format": "FCStd",
            "native_type_id": feature.TypeId,
            "nonempty": True,
            "saved": True,
        }
    _require(descriptor.facet is ReviewedConformanceFacet.REOPEN, "unexpected profile facet")
    object_name = feature.Name
    expected_source_count = len(sources)
    freecad.closeDocument(document.Name)
    reopened = freecad.openDocument(str(save_path))
    reopened.recompute()
    reopened_feature = reopened.getObject(object_name)
    _require(
        reopened_feature is not None
        and reopened_feature.TypeId == expected_type
        and reopened_feature.isValid()
        and tuple(reopened_feature.State) == ("Up-to-date",)
        and len(tuple(reopened_feature.OutList)) == expected_source_count
        and _shape_facts(reopened_feature.Shape) == expected_shape,
        "profile/surface reopen readback failed",
    )
    return {
        "format": "FCStd",
        "native_type_id": reopened_feature.TypeId,
        "nonempty": True,
        "reopened": True,
        "shape": expected_shape,
        "source_count": expected_source_count,
    }


_DESCRIPTOR_BY_CASE_SHA256: Final = MappingProxyType(
    {item.case.case_sha256: item for item in PART_B_REVIEWED_CASE_DESCRIPTORS}
)


def _execute_part_b_case(
    freecad: object,
    case: ReviewedConformanceCase,
    challenge_sha256: str,
) -> bytes:
    descriptor = _DESCRIPTOR_BY_CASE_SHA256.get(case.case_sha256)
    if descriptor is None or descriptor.case != case:
        raise RuntimeError("unreviewed Part B verification case")
    try:
        with tempfile.TemporaryDirectory(prefix="vibecad-part-b-") as temporary:
            temporary_root = Path(temporary)
            if descriptor.family_id == PARTDESIGN_RESIDUAL_MANIFEST.family_id:
                import Part  # type: ignore[import-not-found]  # noqa: PLC0415
                import Sketcher  # type: ignore[import-not-found]  # noqa: PLC0415

                facts = _execute_residual(
                    freecad,
                    Part,
                    Sketcher,
                    descriptor,
                    temporary_root,
                )
            elif descriptor.family_id == PART_DATUM_MANIFEST.family_id:
                facts = _execute_datum(freecad, descriptor, temporary_root)
            elif descriptor.family_id == PART_DRESSUP_MANIFEST.family_id:
                facts = _execute_dressup(freecad, descriptor, temporary_root)
            elif descriptor.family_id == PART_PROFILE_SURFACE_MANIFEST.family_id:
                import Part  # type: ignore[import-not-found]  # noqa: PLC0415

                facts = _execute_profile(freecad, Part, descriptor, temporary_root)
            else:
                raise RuntimeError("unreviewed Part B verification family")
            return _observation(descriptor, challenge_sha256, facts)
    finally:
        for document_name in tuple(freecad.listDocuments()):
            freecad.closeDocument(document_name)


def build_managed_freecad_part_b_verification_receipts(
    *,
    freecad: object,
) -> tuple[ReviewedVerificationReceipt, ...]:
    """Run the exact 16-operation/112-case matrix and return four receipts.

    Each family receipt remains separately bound to its reviewed adapter and
    rule contract.  The tuple is not persisted and does not update capability
    status; callers may explicitly convert each receipt to the existing
    promotion-verification binding after reviewing the result.
    """

    receipts = []
    for manifest, case_manifest in zip(
        PART_B_FAMILY_MANIFESTS,
        PART_B_REVIEWED_CASE_MANIFESTS,
        strict=True,
    ):

        def execute(
            case: ReviewedConformanceCase,
            challenge_sha256: str,
        ) -> bytes:
            return _execute_part_b_case(freecad, case, challenge_sha256)

        host = build_managed_freecad_conformance_host(
            freecad=freecad,
            case_manifest=case_manifest,
            execute_case=execute,
            verifier_id=f"{PART_B_VERIFIER_ID}.{PART_B_VERIFIER_CONTRACT_SHA256[:16]}",
            verifier_version=PART_B_VERIFIER_VERSION,
        )
        receipts.append(
            build_reviewed_verification_receipt(
                manifest=manifest,
                case_manifest=case_manifest,
                host=host,
            )
        )
    return tuple(receipts)


__all__ = (
    "PART_B_FAMILY_MANIFESTS",
    "PART_B_REVIEWED_CASE_DESCRIPTORS",
    "PART_B_REVIEWED_CASE_MANIFESTS",
    "PART_B_VERIFIER_CONTRACT_SHA256",
    "PART_B_VERIFIER_ID",
    "PART_B_VERIFIER_VERSION",
    "PartBReviewedCaseDescriptor",
    "build_managed_freecad_part_b_verification_receipts",
)
