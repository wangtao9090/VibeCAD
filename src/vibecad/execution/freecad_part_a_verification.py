"""Private managed-runtime verification for reviewed Part core and curves.

The reviewed family manifests prove static intent/adapter/native mappings.  This
module supplies the missing family-specific runtime evidence without changing
the production capability catalog: trusted, immutable fixtures are converted
to exact backend plans, decoded again, executed in managed FreeCAD, edited,
recomputed, saved, reopened, rejected when tampered, and forced through a late
transaction rollback.  The generic verification builder then emits a managed
receipt and a ``FreeCadPromotionVerificationBinding``.

There is deliberately no callback or claimed-result input.  The only caller
input is the already authenticated FreeCAD module; all case contracts,
fixtures, native operations, assertions, and observations are owned here.
Nothing persists receipts, promotes catalog entries, or grants CAD execution
authority.
"""

from __future__ import annotations

import hashlib
import importlib
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
from vibecad.intent_bridge.freecad_part_core_adapter import PART_CORE_MANIFEST
from vibecad.intent_bridge.freecad_part_curve_adapter import PART_CURVE_MANIFEST
from vibecad.intent_bridge.reviewed_family_engine import FamilyBatchManifest
from vibecad.parametric import freecad_part_core_rules as part_core_rules
from vibecad.parametric import freecad_part_curve_rules as part_curve_rules
from vibecad.parametric.freecad_part_core_rules import (
    PART_CORE_NATIVE_SPECS,
    AuthenticatedPartCoreObject,
    PartCoreBackendPlan,
    PartCoreExecutionBindings,
    PartCoreOperation,
    PartCoreParameterSet,
    PartCoreRuleError,
    PartCoreRuleErrorCode,
    PartCoreSelection,
    apply_part_core_plan,
    decode_part_core_backend_plan,
)
from vibecad.parametric.freecad_part_curve_rules import (
    PART_CURVE_NATIVE_SPECS,
    PartCurveBackendPlan,
    PartCurveExecutionBindings,
    PartCurveOperation,
    PartCurveParameterSet,
    PartCurveRuleError,
    PartCurveRuleErrorCode,
    apply_part_curve_plan,
    decode_part_curve_backend_plan,
)

PART_A_VERIFIER_ID: Final = "vcad.managed.freecad.part-a-conformance"
PART_A_VERIFIER_VERSION: Final = "1.0.0"

_CASE_CONTRACT_DOMAIN = b"vibecad-reviewed-part-a-case-contract-v1\0"
_FIXTURE_DIGEST_DOMAIN = b"vibecad-reviewed-part-a-fixture-v1\0"
_HARNESS_CONTRACT_DOMAIN = b"vibecad-reviewed-part-a-harness-contract-v1\0"
_PLAN_SOURCE_DIGEST_DOMAIN = b"vibecad-reviewed-part-a-plan-source-v1\0"
_OBSERVATION_DOMAIN = b"vibecad-reviewed-part-a-observation-v1\0"
_VERIFICATION_LOCK = threading.Lock()

_FACET_CONTRACTS: Final = {
    ReviewedConformanceFacet.CREATE: "exact-plan-create-native-type-and-valid-shape",
    ReviewedConformanceFacet.EDIT: "reviewed-property-or-source-edit-propagates-to-shape",
    ReviewedConformanceFacet.RECOMPUTE: "explicit-recompute-is-valid-and-shape-stable",
    ReviewedConformanceFacet.SAVE: "managed-fcstd-save-is-nonempty-and-complete",
    ReviewedConformanceFacet.REOPEN: "saved-object-reopens-with-type-and-shape-intact",
    ReviewedConformanceFacet.NEGATIVE: "tampered-plan-is-rejected-before-document-mutation",
    ReviewedConformanceFacet.LATE_ROLLBACK: "post-create-failure-restores-exact-document-state",
}


def _fail(path: str) -> None:
    raise CapabilityCatalogError(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)


def _canonical(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail("part_a/canonical")
    if not raw or len(raw) > 64 * 1024:
        _fail("part_a/canonical")
    return raw


def _sha(domain: bytes, value: str | bytes) -> str:
    raw = value if type(value) is bytes else value.encode("utf-8")
    return hashlib.sha256(domain + raw).hexdigest()


def _json(raw: bytes) -> object:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail("part_a/fixture")
    if _canonical(value) != raw:
        _fail("part_a/fixture")
    return value


_HARNESS_CONTRACT_SHA256: Final = _sha(
    _HARNESS_CONTRACT_DOMAIN,
    _canonical(
        {
            "schema_version": 1,
            "verifier": {
                "id": PART_A_VERIFIER_ID,
                "version": PART_A_VERIFIER_VERSION,
            },
            "execution": {
                "one_real_scenario_per_operation": True,
                "same_process_managed_freecad": True,
                "caller_result_input": False,
                "nonblocking_process_lock": True,
                "temporary_paths": "host-owned",
                "receipt_persistence": False,
            },
            "facets": {facet.value: contract for facet, contract in _FACET_CONTRACTS.items()},
        }
    ),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class _PartAFixture:
    operation_id: str
    parameters_bytes: bytes
    source_count: int
    source_strategy: str
    edit_strategy: str
    edit_bytes: bytes
    fixture_sha256: str = field(init=False)
    canonical_bytes: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.operation_id) is not str
            or not self.operation_id
            or type(self.parameters_bytes) is not bytes
            or type(self.source_count) is not int
            or not 0 <= self.source_count <= 16
            or type(self.source_strategy) is not str
            or type(self.edit_strategy) is not str
            or type(self.edit_bytes) is not bytes
        ):
            _fail("part_a/fixture")
        parameters = _json(self.parameters_bytes)
        edit = _json(self.edit_bytes)
        payload = _canonical(
            {
                "edit": {"strategy": self.edit_strategy, "value": edit},
                "operation_id": self.operation_id,
                "parameters": parameters,
                "sources": {
                    "count": self.source_count,
                    "strategy": self.source_strategy,
                },
            }
        )
        object.__setattr__(self, "canonical_bytes", payload)
        object.__setattr__(self, "fixture_sha256", _sha(_FIXTURE_DIGEST_DOMAIN, payload))

    @property
    def parameters(self) -> object:
        return _json(self.parameters_bytes)

    @property
    def edit(self) -> object:
        return _json(self.edit_bytes)


def _fixture(
    operation_id: str,
    parameters: object,
    *,
    source_count: int = 0,
    source_strategy: str = "none",
    edit_strategy: str,
    edit: object,
) -> _PartAFixture:
    return _PartAFixture(
        operation_id=operation_id,
        parameters_bytes=_canonical(parameters),
        source_count=source_count,
        source_strategy=source_strategy,
        edit_strategy=edit_strategy,
        edit_bytes=_canonical(edit),
    )


def _primitive_parameters(shape: dict[str, object]) -> dict[str, object]:
    return {
        "shape": shape,
        "placement": {
            "translation_mm": [0.0, 0.0, 0.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 0.0,
        },
    }


_CORE_PRIMITIVES: Final = {
    PartCoreOperation.BOX: (
        {"size_x_mm": 10.0, "size_y_mm": 8.0, "size_z_mm": 6.0},
        {"Length": 12.0},
    ),
    PartCoreOperation.CONE: (
        {
            "base_radius_mm": 5.0,
            "top_radius_mm": 2.0,
            "height_mm": 8.0,
            "sweep_degrees": 360.0,
        },
        {"Height": 9.0},
    ),
    PartCoreOperation.CYLINDER: (
        {"radius_mm": 5.0, "height_mm": 8.0, "sweep_degrees": 360.0},
        {"Radius": 6.0},
    ),
    PartCoreOperation.ELLIPSOID: (
        {
            "radius_x_mm": 5.0,
            "radius_y_mm": 4.0,
            "radius_z_mm": 3.0,
            "latitude_min_degrees": -90.0,
            "latitude_max_degrees": 90.0,
            "sweep_degrees": 360.0,
        },
        {"Radius1": 6.0},
    ),
    PartCoreOperation.PRISM: (
        {"side_count": 6, "circumradius_mm": 5.0, "height_mm": 8.0},
        {"Circumradius": 6.0},
    ),
    PartCoreOperation.SPHERE: (
        {
            "radius_mm": 5.0,
            "latitude_min_degrees": -90.0,
            "latitude_max_degrees": 90.0,
            "sweep_degrees": 360.0,
        },
        {"Radius": 6.0},
    ),
    PartCoreOperation.TORUS: (
        {
            "major_radius_mm": 8.0,
            "minor_radius_mm": 2.0,
            "latitude_min_degrees": -180.0,
            "latitude_max_degrees": 180.0,
            "sweep_degrees": 360.0,
        },
        {"Radius1": 9.0},
    ),
    PartCoreOperation.WEDGE: (
        {
            "x_min_mm": 0.0,
            "y_min_mm": 0.0,
            "z_min_mm": 0.0,
            "x_inner_min_mm": 2.0,
            "z_inner_min_mm": 1.0,
            "x_max_mm": 10.0,
            "y_max_mm": 8.0,
            "z_max_mm": 6.0,
            "x_inner_max_mm": 8.0,
            "z_inner_max_mm": 5.0,
        },
        {"Xmax": 12.0},
    ),
}


def _core_fixture(operation: PartCoreOperation) -> _PartAFixture:
    primitive = _CORE_PRIMITIVES.get(operation)
    if primitive is not None:
        parameters, edit = primitive
        return _fixture(
            operation.value,
            _primitive_parameters(parameters),
            edit_strategy="result-properties",
            edit=edit,
        )
    parameters: object = {}
    if operation is PartCoreOperation.MIRROR:
        parameters = {"base_point_mm": [0.0, 0.0, 0.0], "normal": [1.0, 0.0, 0.0]}
    elif operation is PartCoreOperation.SCALE:
        parameters = {"scale_xyz": [2.0, 2.0, 2.0]}
    source_count = PART_CORE_NATIVE_SPECS[operation].minimum_sources
    strategies: dict[PartCoreOperation, tuple[str, object]] = {
        PartCoreOperation.CUT: ("source-placement", {"index": 1, "x_mm": 6.0}),
        PartCoreOperation.FUSE: ("source-placement", {"index": 0, "x_mm": -2.0}),
        PartCoreOperation.SECTION: ("source-placement", {"index": 1, "x_mm": 6.0}),
        PartCoreOperation.MULTI_FUSE: (
            "source-placement",
            {"index": 0, "x_mm": -2.0},
        ),
        PartCoreOperation.MULTI_COMMON: (
            "source-length",
            {"index": 0, "length_mm": 6.0},
        ),
        PartCoreOperation.REFINE: ("refine-source-shape", {"split_x_mm": 12.0}),
        PartCoreOperation.SCALE: (
            "result-properties",
            {
                "UniformScale": 1.5,
                "XScale": 1.5,
                "YScale": 1.5,
                "ZScale": 1.5,
            },
        ),
    }
    edit_strategy, edit = strategies.get(
        operation,
        ("source-length", {"index": 0, "length_mm": 12.0}),
    )
    return _fixture(
        operation.value,
        parameters,
        source_count=source_count,
        source_strategy="refine-fused" if operation is PartCoreOperation.REFINE else "boxes",
        edit_strategy=edit_strategy,
        edit=edit,
    )


_PART_CORE_FIXTURES: Final = tuple(_core_fixture(operation) for operation in PartCoreOperation)


_CURVE_PARAMETERS: Final = {
    PartCurveOperation.CIRCLE: {
        "geometry": {
            "radius_mm": 5.0,
            "start_angle_degrees": 10.0,
            "end_angle_degrees": 300.0,
        },
        "placement": {
            "translation_mm": [1.0, 2.0, 3.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 15.0,
        },
    },
    PartCurveOperation.ELLIPSE: {
        "geometry": {
            "major_radius_mm": 8.0,
            "minor_radius_mm": 4.0,
            "start_angle_degrees": 15.0,
            "end_angle_degrees": 270.0,
        },
        "placement": {
            "translation_mm": [2.0, 1.0, 3.0],
            "rotation_axis": [0.0, 1.0, 0.0],
            "rotation_degrees": 10.0,
        },
    },
    PartCurveOperation.HELIX: {
        "geometry": {
            "pitch_mm": 3.0,
            "height_mm": 12.0,
            "radius_mm": 4.0,
            "cone_angle_degrees": 5.0,
            "handedness": "Left-handed",
        },
        "placement": {
            "translation_mm": [3.0, 2.0, 1.0],
            "rotation_axis": [1.0, 0.0, 0.0],
            "rotation_degrees": 20.0,
        },
    },
    PartCurveOperation.LINE: {
        "geometry": {
            "x1_mm": 1.0,
            "y1_mm": 2.0,
            "z1_mm": 3.0,
            "x2_mm": 9.0,
            "y2_mm": 5.0,
            "z2_mm": 7.0,
        },
        "placement": {
            "translation_mm": [1.0, 0.0, 0.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 5.0,
        },
    },
    PartCurveOperation.PLANE: {
        "geometry": {"length_mm": 20.0, "width_mm": 30.0},
        "placement": {
            "translation_mm": [0.0, 1.0, 0.0],
            "rotation_axis": [1.0, 0.0, 0.0],
            "rotation_degrees": 30.0,
        },
    },
    PartCurveOperation.POLYGON: {
        "geometry": {
            "points_mm": [
                [0.0, 0.0, 0.0],
                [10.0, 0.0, 0.0],
                [12.0, 6.0, 0.0],
                [2.0, 8.0, 0.0],
            ],
            "closed": True,
        },
        "placement": {
            "translation_mm": [0.0, 0.0, 1.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 25.0,
        },
    },
    PartCurveOperation.REGULAR_POLYGON: {
        "geometry": {"side_count": 5, "circumradius_mm": 6.0},
        "placement": {
            "translation_mm": [2.0, 0.0, 1.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 12.0,
        },
    },
    PartCurveOperation.SPIRAL: {
        "geometry": {
            "growth_mm": 1.5,
            "start_radius_mm": 2.0,
            "rotations": 3.0,
            "segment_length_mm": 0.5,
        },
        "placement": {
            "translation_mm": [1.0, 1.0, 1.0],
            "rotation_axis": [0.0, 1.0, 0.0],
            "rotation_degrees": 18.0,
        },
    },
    PartCurveOperation.VERTEX: {
        "geometry": {"x_mm": 1.0, "y_mm": 2.0, "z_mm": 3.0},
        "placement": {
            "translation_mm": [4.0, 5.0, 6.0],
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 0.0,
        },
    },
}

_CURVE_EDITS: Final = {
    PartCurveOperation.CIRCLE: {"property": "Radius", "value": 7.0},
    PartCurveOperation.ELLIPSE: {"property": "MajorRadius", "value": 10.0},
    PartCurveOperation.HELIX: {"property": "Radius", "value": 5.0},
    PartCurveOperation.LINE: {"property": "X2", "value": 12.0},
    PartCurveOperation.PLANE: {"property": "Length", "value": 25.0},
    PartCurveOperation.POLYGON: {
        "property": "Nodes",
        "value": [
            [0.0, 0.0, 0.0],
            [14.0, 0.0, 0.0],
            [12.0, 6.0, 0.0],
            [2.0, 8.0, 0.0],
        ],
    },
    PartCurveOperation.REGULAR_POLYGON: {"property": "Circumradius", "value": 8.0},
    PartCurveOperation.SPIRAL: {"property": "Radius", "value": 3.0},
    PartCurveOperation.VERTEX: {"property": "X", "value": 4.0},
}

_PART_CURVE_FIXTURES: Final = tuple(
    _fixture(
        operation.value,
        _CURVE_PARAMETERS[operation],
        edit_strategy="result-property",
        edit=_CURVE_EDITS[operation],
    )
    for operation in PartCurveOperation
)


def _build_case_manifest(
    manifest: FamilyBatchManifest,
    fixtures: tuple[_PartAFixture, ...],
) -> ReviewedConformanceCaseManifest:
    operations = {item.operation_id: item for item in manifest.operations}
    if set(operations) != {item.operation_id for item in fixtures}:
        _fail("part_a/fixtures/operations")
    cases = []
    for fixture in fixtures:
        operation = operations[fixture.operation_id]
        for facet in ReviewedConformanceFacet:
            descriptor = {
                "facet": facet.value,
                "facet_contract": _FACET_CONTRACTS[facet],
                "family_manifest_sha256": manifest.manifest_sha256,
                "fixture_sha256": fixture.fixture_sha256,
                "harness_contract_sha256": _HARNESS_CONTRACT_SHA256,
                "operation_id": fixture.operation_id,
                "operation_specification_sha256": operation.specification_sha256,
                "schema_version": REVIEWED_VERIFICATION_SCHEMA_VERSION,
            }
            contract_sha256 = _sha(_CASE_CONTRACT_DOMAIN, _canonical(descriptor))
            cases.append(
                ReviewedConformanceCase(
                    case_id=f"parta.{manifest.family_id}.{fixture.operation_id}.{facet.value}",
                    operation_id=fixture.operation_id,
                    operation_specification_sha256=operation.specification_sha256,
                    facet=facet,
                    case_contract_sha256=contract_sha256,
                )
            )
    return verification._admit_reviewed_host_conformance_case_manifest(  # noqa: SLF001
        manifest=manifest,
        cases=tuple(cases),
    )


PART_CORE_REVIEWED_HOST_CASE_MANIFEST: Final = _build_case_manifest(
    PART_CORE_MANIFEST,
    _PART_CORE_FIXTURES,
)
PART_CURVE_REVIEWED_HOST_CASE_MANIFEST: Final = _build_case_manifest(
    PART_CURVE_MANIFEST,
    _PART_CURVE_FIXTURES,
)


def _stable_number(value: object) -> float:
    result = round(float(value), 9)
    if not math.isfinite(result):
        _fail("part_a/shape")
    return 0.0 if result == 0.0 else result


def _shape_evidence(shape: object) -> dict[str, object]:
    try:
        box = shape.BoundBox
        brep = shape.exportBrepToString().encode("utf-8")
        return {
            "shape_type": str(shape.ShapeType),
            "vertices": len(shape.Vertexes),
            "edges": len(shape.Edges),
            "faces": len(shape.Faces),
            "solids": len(shape.Solids),
            "length_mm": _stable_number(shape.Length),
            "area_mm2": _stable_number(shape.Area),
            "volume_mm3": _stable_number(shape.Volume),
            "bounds_mm": [
                _stable_number(value)
                for value in (box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax)
            ],
            "brep_sha256": hashlib.sha256(brep).hexdigest(),
        }
    except CapabilityCatalogError:
        raise
    except BaseException:
        _fail("part_a/shape")


def _assert_valid_feature(feature: object, expected_type_id: str) -> dict[str, object]:
    try:
        if (
            feature is None
            or feature.TypeId != expected_type_id
            or not feature.isValid()
            or feature.Shape.isNull()
            or not feature.Shape.isValid()
        ):
            _fail("part_a/feature")
        return _shape_evidence(feature.Shape)
    except CapabilityCatalogError:
        raise
    except BaseException:
        _fail("part_a/feature")


def _same_shape_evidence(left: dict[str, object], right: dict[str, object]) -> bool:
    exact_keys = ("shape_type", "vertices", "edges", "faces", "solids")
    if any(left.get(key) != right.get(key) for key in exact_keys):
        return False
    scalar_keys = ("length_mm", "area_mm2", "volume_mm3")
    try:
        if any(
            not math.isclose(
                float(left[key]),
                float(right[key]),
                rel_tol=0.0,
                abs_tol=1e-6,
            )
            for key in scalar_keys
        ):
            return False
        left_bounds = left["bounds_mm"]
        right_bounds = right["bounds_mm"]
        return (
            type(left_bounds) is list
            and type(right_bounds) is list
            and len(left_bounds) == len(right_bounds) == 6
            and all(
                math.isclose(
                    float(left_value),
                    float(right_value),
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
                for left_value, right_value in zip(left_bounds, right_bounds, strict=True)
            )
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _close_owned_documents(freecad: object, owned_documents: dict[str, object]) -> None:
    try:
        open_documents = freecad.listDocuments()
        for name, document in tuple(owned_documents.items()):
            if open_documents.get(name) is document:
                freecad.closeDocument(name)
    except BaseException:
        _fail("part_a/documents/cleanup")


def _make_core_sources(
    freecad: object,
    part: object,
    document: object,
    operation: PartCoreOperation,
    fixture: _PartAFixture,
) -> tuple[object, ...]:
    sources = []
    for index in range(fixture.source_count):
        name = f"VerifySource_{operation.value}_{index}"
        if fixture.source_strategy == "refine-fused":
            obj = document.addObject("Part::Feature", name)
            obj.Shape = part.makeBox(10, 8, 6).fuse(
                part.makeBox(10, 8, 6, freecad.Vector(10, 0, 0))
            )
        elif fixture.source_strategy == "boxes":
            obj = document.addObject("Part::Box", name)
            obj.Length = obj.Width = obj.Height = 10
            if (
                operation
                in {
                    PartCoreOperation.CUT,
                    PartCoreOperation.FUSE,
                    PartCoreOperation.COMMON,
                    PartCoreOperation.SECTION,
                }
                and index == 1
            ):
                obj.Length = obj.Width = obj.Height = 8
                obj.Placement.Base = freecad.Vector(5, 0, 0)
            elif operation in {
                PartCoreOperation.MULTI_FUSE,
                PartCoreOperation.MULTI_COMMON,
                PartCoreOperation.COMPOUND,
            }:
                obj.Placement.Base = freecad.Vector(index * 5, 0, 0)
        else:
            _fail("part_a/core/source_strategy")
        sources.append(obj)
    document.recompute()
    return tuple(sources)


def _core_plan(operation: PartCoreOperation, fixture: _PartAFixture) -> PartCoreBackendPlan:
    operation_spec = next(
        item for item in PART_CORE_MANIFEST.operations if item.operation_id == operation.value
    )
    selections = tuple(
        PartCoreSelection(node_id=f"node_source_{index}", result_id=f"result_source_{index}")
        for index in range(fixture.source_count)
    )
    return PartCoreBackendPlan(
        source_artifact_id="artifact_part_a_core_fixture",
        source_graph_id=f"graph_part_a_core_{operation.value}",
        source_graph_sha256=_sha(
            _PLAN_SOURCE_DIGEST_DOMAIN,
            f"core:{operation.value}:graph:{fixture.fixture_sha256}",
        ),
        source_content_sha256=hashlib.sha256(fixture.canonical_bytes).hexdigest(),
        lowering_request_sha256=_sha(
            _PLAN_SOURCE_DIGEST_DOMAIN,
            f"core:{operation.value}:lowering:{fixture.fixture_sha256}",
        ),
        adapter_contract_sha256=PART_CORE_MANIFEST.adapter.adapter_contract_sha256,
        manifest_sha256=PART_CORE_MANIFEST.manifest_sha256,
        operation_specification_sha256=operation_spec.specification_sha256,
        body_id="body_part_a_core",
        target=PartCoreSelection(node_id="node_target", result_id="result_target"),
        operation=operation,
        sources=selections,
        parameters=PartCoreParameterSet.from_value(operation, fixture.parameters),
    )


def _core_bindings(
    document: object,
    plan: PartCoreBackendPlan,
    sources: tuple[object, ...],
) -> PartCoreExecutionBindings:
    return PartCoreExecutionBindings(
        document=document,
        body_id=plan.body_id,
        sources=tuple(
            AuthenticatedPartCoreObject(
                object=obj,
                node_id=selection.node_id,
                result_id=selection.result_id,
            )
            for obj, selection in zip(sources, plan.sources, strict=True)
        ),
    )


def _edit_core(
    freecad: object,
    part: object,
    document: object,
    result: object,
    sources: tuple[object, ...],
    fixture: _PartAFixture,
) -> None:
    edit = fixture.edit
    if type(edit) is not dict:
        _fail("part_a/core/edit")
    if fixture.edit_strategy == "result-properties":
        for name, value in edit.items():
            setattr(result, name, value)
    elif fixture.edit_strategy == "source-placement":
        index = edit.get("index")
        if type(index) is not int or not 0 <= index < len(sources):
            _fail("part_a/core/edit")
        sources[index].Placement.Base.x = float(edit["x_mm"])
    elif fixture.edit_strategy == "source-length":
        index = edit.get("index")
        if type(index) is not int or not 0 <= index < len(sources):
            _fail("part_a/core/edit")
        sources[index].Length = float(edit["length_mm"])
    elif fixture.edit_strategy == "refine-source-shape":
        split = float(edit["split_x_mm"])
        sources[0].Shape = part.makeBox(split, 8, 6).fuse(
            part.makeBox(10, 8, 6, freecad.Vector(split, 0, 0))
        )
    else:
        _fail("part_a/core/edit")
    document.recompute()


def _curve_plan(operation: PartCurveOperation, fixture: _PartAFixture) -> PartCurveBackendPlan:
    operation_spec = next(
        item for item in PART_CURVE_MANIFEST.operations if item.operation_id == operation.value
    )
    return PartCurveBackendPlan(
        source_artifact_id="artifact_part_a_curve_fixture",
        source_graph_id=f"graph_part_a_curve_{operation.value}",
        source_graph_sha256=_sha(
            _PLAN_SOURCE_DIGEST_DOMAIN,
            f"curve:{operation.value}:graph:{fixture.fixture_sha256}",
        ),
        source_content_sha256=hashlib.sha256(fixture.canonical_bytes).hexdigest(),
        lowering_request_sha256=_sha(
            _PLAN_SOURCE_DIGEST_DOMAIN,
            f"curve:{operation.value}:lowering:{fixture.fixture_sha256}",
        ),
        adapter_contract_sha256=PART_CURVE_MANIFEST.adapter.adapter_contract_sha256,
        manifest_sha256=PART_CURVE_MANIFEST.manifest_sha256,
        operation_specification_sha256=operation_spec.specification_sha256,
        body_id="body_part_a_curve",
        node_id="node_target",
        result_id="result_target",
        parameter_id="parameter_geometry",
        value_id="value_geometry",
        operation=operation,
        parameters=PartCurveParameterSet.from_value(operation, fixture.parameters),
    )


def _curve_bindings(document: object, plan: PartCurveBackendPlan) -> PartCurveExecutionBindings:
    return PartCurveExecutionBindings(
        document=document,
        expected_adapter_contract_sha256=plan.adapter_contract_sha256,
        expected_manifest_sha256=plan.manifest_sha256,
        expected_operation_specification_sha256=plan.operation_specification_sha256,
    )


def _edit_curve(freecad: object, document: object, result: object, fixture: _PartAFixture) -> None:
    edit = fixture.edit
    if fixture.edit_strategy != "result-property" or type(edit) is not dict:
        _fail("part_a/curve/edit")
    value = edit.get("value")
    if type(value) is list:
        value = [freecad.Vector(*point) for point in value]
    setattr(result, edit["property"], value)
    document.recompute()


def _save_and_reopen(
    freecad: object,
    document: object,
    result: object,
    model_path: Path,
    expected_type_id: str,
    edited: dict[str, object],
    owned_documents: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    document.saveAs(str(model_path))
    if not model_path.is_file() or model_path.stat().st_size <= 0:
        _fail("part_a/save")
    save_evidence = {
        "format": "FCStd",
        "nonempty": True,
        "object_count": len(document.Objects),
    }
    document_name = document.Name
    result_name = result.Name
    freecad.closeDocument(document_name)
    reopened = freecad.openDocument(str(model_path))
    owned_documents[reopened.Name] = reopened
    try:
        reopened.recompute()
        reopened_result = reopened.getObject(result_name)
        reopened_shape = _assert_valid_feature(reopened_result, expected_type_id)
        if not _same_shape_evidence(reopened_shape, edited):
            _fail("part_a/reopen")
        reopen_evidence = {
            "object_name": result_name,
            "type_id": expected_type_id,
            "shape": reopened_shape,
        }
    finally:
        freecad.closeDocument(reopened.Name)
    return save_evidence, reopen_evidence


class _PartCoreExecutor:
    __slots__ = ("_cache", "_fixtures", "_freecad")

    def __init__(self, freecad: object) -> None:
        self._freecad = freecad
        self._fixtures = {item.operation_id: item for item in _PART_CORE_FIXTURES}
        self._cache: dict[str, dict[ReviewedConformanceFacet, dict[str, object]]] = {}

    def __call__(self, case: ReviewedConformanceCase, challenge_sha256: str) -> bytes:
        fixture = self._fixtures.get(case.operation_id)
        if fixture is None:
            _fail("part_a/core/case")
        outcomes = self._cache.get(case.operation_id)
        if outcomes is None:
            outcomes = self._run(PartCoreOperation(case.operation_id), fixture)
            self._cache[case.operation_id] = outcomes
        return _case_observation(
            case=case,
            challenge_sha256=challenge_sha256,
            fixture=fixture,
            evidence=outcomes[case.facet],
        )

    def _run(
        self,
        operation: PartCoreOperation,
        fixture: _PartAFixture,
    ) -> dict[ReviewedConformanceFacet, dict[str, object]]:
        freecad = self._freecad
        part = importlib.import_module("Part")
        owned_documents: dict[str, object] = {}
        outcomes: dict[ReviewedConformanceFacet, dict[str, object]] = {}
        try:
            with tempfile.TemporaryDirectory(prefix="vibecad-part-core-verify-") as temporary:
                plan = _core_plan(operation, fixture)
                payload = plan.canonical_bytes
                decoded = decode_part_core_backend_plan(
                    payload,
                    expected_content_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_plan_sha256=plan.plan_sha256,
                )
                if decoded != plan:
                    _fail("part_a/core/readback")
                document = freecad.newDocument(f"VerifyCore_{operation.value}")
                owned_documents[document.Name] = document
                document.UndoMode = 1
                sources = _make_core_sources(
                    freecad,
                    part,
                    document,
                    operation,
                    fixture,
                )
                bindings = _core_bindings(document, plan, sources)
                before_negative = tuple(document.Objects)
                try:
                    apply_part_core_plan(
                        payload + b" ",
                        expected_content_sha256=hashlib.sha256(payload).hexdigest(),
                        expected_plan_sha256=plan.plan_sha256,
                        bindings=bindings,
                    )
                except PartCoreRuleError:
                    pass
                else:
                    _fail("part_a/core/negative")
                if tuple(document.Objects) != before_negative:
                    _fail("part_a/core/negative")
                outcomes[ReviewedConformanceFacet.NEGATIVE] = {
                    "mutation_count": 0,
                    "tamper": "append-noncanonical-byte",
                }

                receipt = apply_part_core_plan(
                    payload,
                    expected_content_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_plan_sha256=plan.plan_sha256,
                    bindings=bindings,
                )
                result = document.getObject(receipt.object_name)
                initial = _assert_valid_feature(
                    result,
                    PART_CORE_NATIVE_SPECS[operation].type_id,
                )
                outcomes[ReviewedConformanceFacet.CREATE] = {
                    "native_receipt_sha256": receipt.receipt_sha256,
                    "object_name": receipt.object_name,
                    "type_id": PART_CORE_NATIVE_SPECS[operation].type_id,
                    "shape": initial,
                }

                document.recompute()
                recomputed = _assert_valid_feature(
                    result,
                    PART_CORE_NATIVE_SPECS[operation].type_id,
                )
                if recomputed != initial:
                    _fail("part_a/core/recompute")
                outcomes[ReviewedConformanceFacet.RECOMPUTE] = {"shape": recomputed}

                _edit_core(
                    freecad,
                    part,
                    document,
                    result,
                    sources,
                    fixture,
                )
                edited = _assert_valid_feature(
                    result,
                    PART_CORE_NATIVE_SPECS[operation].type_id,
                )
                if edited["brep_sha256"] == initial["brep_sha256"]:
                    _fail("part_a/core/edit")
                outcomes[ReviewedConformanceFacet.EDIT] = {
                    "strategy": fixture.edit_strategy,
                    "shape": edited,
                }

                saved, reopened = _save_and_reopen(
                    freecad,
                    document,
                    result,
                    Path(temporary) / f"core-{operation.value}.FCStd",
                    PART_CORE_NATIVE_SPECS[operation].type_id,
                    edited,
                    owned_documents,
                )
                outcomes[ReviewedConformanceFacet.SAVE] = saved
                outcomes[ReviewedConformanceFacet.REOPEN] = reopened

                rollback = freecad.newDocument(f"VerifyCoreRollback_{operation.value}")
                owned_documents[rollback.Name] = rollback
                rollback.UndoMode = 1
                rollback_sources = _make_core_sources(
                    freecad,
                    part,
                    rollback,
                    operation,
                    fixture,
                )
                rollback_bindings = _core_bindings(rollback, plan, rollback_sources)
                before_rollback = tuple(rollback.Objects)
                before_visibility = tuple(bool(item.Visibility) for item in before_rollback)
                original = part_core_rules._validate_effect  # noqa: SLF001
                late_validation_reached = False

                def fail_after_native_create(*_args: object, **_kwargs: object) -> object:
                    nonlocal late_validation_reached
                    late_validation_reached = True
                    raise PartCoreRuleError(
                        PartCoreRuleErrorCode.CONFORMANCE_FAILED,
                        "/result/injected-late-failure",
                    )

                part_core_rules._validate_effect = fail_after_native_create  # noqa: SLF001
                try:
                    apply_part_core_plan(
                        payload,
                        expected_content_sha256=hashlib.sha256(payload).hexdigest(),
                        expected_plan_sha256=plan.plan_sha256,
                        bindings=rollback_bindings,
                    )
                except PartCoreRuleError:
                    pass
                else:
                    _fail("part_a/core/late_rollback")
                finally:
                    part_core_rules._validate_effect = original  # noqa: SLF001
                if (
                    not late_validation_reached
                    or tuple(rollback.Objects) != before_rollback
                    or tuple(bool(item.Visibility) for item in rollback.Objects)
                    != before_visibility
                    or bool(rollback.HasPendingTransaction)
                ):
                    _fail("part_a/core/late_rollback")
                outcomes[ReviewedConformanceFacet.LATE_ROLLBACK] = {
                    "objects_restored": len(before_rollback),
                    "pending_transaction": False,
                }
                freecad.closeDocument(rollback.Name)
        finally:
            _close_owned_documents(freecad, owned_documents)
        if set(outcomes) != set(ReviewedConformanceFacet):
            _fail("part_a/core/outcomes")
        return outcomes


class _PartCurveExecutor:
    __slots__ = ("_cache", "_fixtures", "_freecad")

    def __init__(self, freecad: object) -> None:
        self._freecad = freecad
        self._fixtures = {item.operation_id: item for item in _PART_CURVE_FIXTURES}
        self._cache: dict[str, dict[ReviewedConformanceFacet, dict[str, object]]] = {}

    def __call__(self, case: ReviewedConformanceCase, challenge_sha256: str) -> bytes:
        fixture = self._fixtures.get(case.operation_id)
        if fixture is None:
            _fail("part_a/curve/case")
        outcomes = self._cache.get(case.operation_id)
        if outcomes is None:
            outcomes = self._run(PartCurveOperation(case.operation_id), fixture)
            self._cache[case.operation_id] = outcomes
        return _case_observation(
            case=case,
            challenge_sha256=challenge_sha256,
            fixture=fixture,
            evidence=outcomes[case.facet],
        )

    def _run(
        self,
        operation: PartCurveOperation,
        fixture: _PartAFixture,
    ) -> dict[ReviewedConformanceFacet, dict[str, object]]:
        freecad = self._freecad
        owned_documents: dict[str, object] = {}
        outcomes: dict[ReviewedConformanceFacet, dict[str, object]] = {}
        try:
            with tempfile.TemporaryDirectory(prefix="vibecad-part-curve-verify-") as temporary:
                plan = _curve_plan(operation, fixture)
                payload = plan.canonical_bytes
                decoded = decode_part_curve_backend_plan(
                    payload,
                    expected_content_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_plan_sha256=plan.plan_sha256,
                )
                if decoded != plan:
                    _fail("part_a/curve/readback")
                document = freecad.newDocument(f"VerifyCurve_{operation.value}")
                owned_documents[document.Name] = document
                document.UndoMode = 1
                bindings = _curve_bindings(document, plan)
                before_negative = tuple(document.Objects)
                try:
                    apply_part_curve_plan(
                        payload + b" ",
                        expected_content_sha256=hashlib.sha256(payload).hexdigest(),
                        expected_plan_sha256=plan.plan_sha256,
                        bindings=bindings,
                    )
                except PartCurveRuleError:
                    pass
                else:
                    _fail("part_a/curve/negative")
                if tuple(document.Objects) != before_negative:
                    _fail("part_a/curve/negative")
                outcomes[ReviewedConformanceFacet.NEGATIVE] = {
                    "mutation_count": 0,
                    "tamper": "append-noncanonical-byte",
                }

                receipt = apply_part_curve_plan(
                    payload,
                    expected_content_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_plan_sha256=plan.plan_sha256,
                    bindings=bindings,
                )
                result = document.getObject(receipt.object_name)
                initial = _assert_valid_feature(
                    result,
                    PART_CURVE_NATIVE_SPECS[operation].type_id,
                )
                outcomes[ReviewedConformanceFacet.CREATE] = {
                    "native_receipt_sha256": receipt.receipt_sha256,
                    "object_name": receipt.object_name,
                    "type_id": PART_CURVE_NATIVE_SPECS[operation].type_id,
                    "shape": initial,
                }

                document.recompute()
                recomputed = _assert_valid_feature(
                    result,
                    PART_CURVE_NATIVE_SPECS[operation].type_id,
                )
                if recomputed != initial:
                    _fail("part_a/curve/recompute")
                outcomes[ReviewedConformanceFacet.RECOMPUTE] = {"shape": recomputed}

                _edit_curve(freecad, document, result, fixture)
                edited = _assert_valid_feature(
                    result,
                    PART_CURVE_NATIVE_SPECS[operation].type_id,
                )
                if edited["brep_sha256"] == initial["brep_sha256"]:
                    _fail("part_a/curve/edit")
                outcomes[ReviewedConformanceFacet.EDIT] = {
                    "strategy": fixture.edit_strategy,
                    "shape": edited,
                }

                saved, reopened = _save_and_reopen(
                    freecad,
                    document,
                    result,
                    Path(temporary) / f"curve-{operation.value}.FCStd",
                    PART_CURVE_NATIVE_SPECS[operation].type_id,
                    edited,
                    owned_documents,
                )
                outcomes[ReviewedConformanceFacet.SAVE] = saved
                outcomes[ReviewedConformanceFacet.REOPEN] = reopened

                rollback = freecad.newDocument(f"VerifyCurveRollback_{operation.value}")
                owned_documents[rollback.Name] = rollback
                rollback.UndoMode = 1
                rollback_bindings = _curve_bindings(rollback, plan)
                before_rollback = tuple(rollback.Objects)
                original = part_curve_rules._validate_created  # noqa: SLF001
                late_validation_reached = False

                def fail_after_native_create(*_args: object, **_kwargs: object) -> object:
                    nonlocal late_validation_reached
                    late_validation_reached = True
                    raise PartCurveRuleError(
                        PartCurveRuleErrorCode.CONFORMANCE_FAILED,
                        "/result/injected-late-failure",
                    )

                part_curve_rules._validate_created = fail_after_native_create  # noqa: SLF001
                try:
                    apply_part_curve_plan(
                        payload,
                        expected_content_sha256=hashlib.sha256(payload).hexdigest(),
                        expected_plan_sha256=plan.plan_sha256,
                        bindings=rollback_bindings,
                    )
                except PartCurveRuleError:
                    pass
                else:
                    _fail("part_a/curve/late_rollback")
                finally:
                    part_curve_rules._validate_created = original  # noqa: SLF001
                if (
                    not late_validation_reached
                    or tuple(rollback.Objects) != before_rollback
                    or bool(rollback.HasPendingTransaction)
                ):
                    _fail("part_a/curve/late_rollback")
                outcomes[ReviewedConformanceFacet.LATE_ROLLBACK] = {
                    "objects_restored": len(before_rollback),
                    "pending_transaction": False,
                }
                freecad.closeDocument(rollback.Name)
        finally:
            _close_owned_documents(freecad, owned_documents)
        if set(outcomes) != set(ReviewedConformanceFacet):
            _fail("part_a/curve/outcomes")
        return outcomes


def _case_observation(
    *,
    case: ReviewedConformanceCase,
    challenge_sha256: str,
    fixture: _PartAFixture,
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
        _fail("part_a/observation")
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
            "observation_sha256": _sha(
                _OBSERVATION_DOMAIN,
                _canonical(
                    {
                        "case_sha256": case.case_sha256,
                        "challenge_sha256": challenge_sha256,
                        "evidence": evidence,
                    }
                ),
            ),
            "operation_id": case.operation_id,
        }
    )


def _build_managed_verification(
    *,
    freecad: object,
    manifest: FamilyBatchManifest,
    case_manifest: ReviewedConformanceCaseManifest,
    executor: object,
) -> tuple[ReviewedVerificationReceipt, FreeCadPromotionVerificationBinding]:
    if not _VERIFICATION_LOCK.acquire(blocking=False):
        _fail("part_a/concurrent_verification")
    try:
        host = build_managed_freecad_conformance_host(
            freecad=freecad,
            case_manifest=case_manifest,
            execute_case=executor,
            verifier_id=PART_A_VERIFIER_ID,
            verifier_version=PART_A_VERIFIER_VERSION,
        )
        receipt = build_reviewed_verification_receipt(
            manifest=manifest,
            case_manifest=case_manifest,
            host=host,
        )
        return receipt, build_promotion_verification_binding(receipt)
    finally:
        _VERIFICATION_LOCK.release()


def build_part_core_managed_verification(
    freecad: object,
) -> tuple[ReviewedVerificationReceipt, FreeCadPromotionVerificationBinding]:
    """Run all 19 Part core operation scenarios and build one managed receipt."""

    return _build_managed_verification(
        freecad=freecad,
        manifest=PART_CORE_MANIFEST,
        case_manifest=PART_CORE_REVIEWED_HOST_CASE_MANIFEST,
        executor=_PartCoreExecutor(freecad),
    )


def build_part_curve_managed_verification(
    freecad: object,
) -> tuple[ReviewedVerificationReceipt, FreeCadPromotionVerificationBinding]:
    """Run all nine Part curve operation scenarios and build one managed receipt."""

    return _build_managed_verification(
        freecad=freecad,
        manifest=PART_CURVE_MANIFEST,
        case_manifest=PART_CURVE_REVIEWED_HOST_CASE_MANIFEST,
        executor=_PartCurveExecutor(freecad),
    )


__all__ = ()
