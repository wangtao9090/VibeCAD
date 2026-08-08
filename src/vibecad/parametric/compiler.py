"""FreeCAD-bound compiler for the bounded editable single-body slice.

The module is import-safe outside FreeCAD.  It creates native Sketcher and
PartDesign objects, an optional native Part Fillet/Chamfer tail, and locked
IR/index metadata only; selector identity and Task authority remain with the
execution layer that adopts the compiled result.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType

from vibecad.parametric.contracts import (
    MAX_DESIGN_PARAMETERS,
    MAX_DRAFT_FACES,
    MAX_PATTERN_FEATURES,
    MAX_PATTERN_INSTANCES,
    MAX_PATTERN_OCCURRENCES,
    MAX_SURFACE_MODIFIERS,
    MAX_THICKNESS_FACES,
    ConstraintKind,
    DerivedParameterExpression,
    DesignParameter,
    DesignUnit,
    EdgeTreatmentFeature,
    EdgeTreatmentKind,
    FeatureExtent,
    FeatureKind,
    GeometryKind,
    MirrorPlane,
    OriginPlane,
    ParametricDesignIR,
    ParametricSketch,
    PartDesignFeature,
    PatternDirection,
    PlaneKind,
    ReferencePoint,
    SemanticEdgeReference,
    SemanticEdgeRole,
    SemanticFaceReference,
    SemanticFaceRole,
    SketchConstraint,
    SketchGeometry,
    SketchReference,
    SketchRole,
)
from vibecad.workflow.errors import is_canonical_json_pointer

PARAMETRIC_METADATA_PROPERTY = "VibeCADParametricMetadata"

_METADATA_SCHEMA = 1
_METADATA_GROUP = "VibeCAD"
_METADATA_DOC = "Canonical VibeCAD parametric IR/index mapping"
_MAX_METADATA_BYTES = 128 * 1024
_MAX_ERROR_PATH = 512
_MAX_COMPILED_HOLE_LOCATIONS = 16
_MAX_COMPILED_CONSTRAINTS_PER_ENTRY = 16
_MAX_COMPILED_SKETCH_CONSTRAINT_ENTRIES = 256
_MAX_COMPILED_SKETCH_GEOMETRIES = 256
_MAX_COMPILED_SKETCH_CONSTRAINTS = 1024
_SLOT_NATIVE_GEOMETRY_COUNT = 4
_SLOT_NATIVE_CONSTRAINT_COUNT = 14
_SOLVER_RESULTS = frozenset({0, -1, -2, -3, -4, -5})
_HEX_32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_IR_ID = re.compile(
    r"ir_(?:design|body|parameter|datum|sketch|geometry|constraint|feature)_"
    r"[0-9a-f]{32}\Z"
)
_PARAMETER_PROPERTY = re.compile(r"P_[0-9a-f]{32}\Z")
_CONSTRAINT_NAME = re.compile(r"C_[0-9a-f]{32}\Z")
_METADATA_DOMAIN = b"vibecad-parametric-freecad-metadata-v1\0"
_SLOT_CONSTRAINT_DOMAIN = b"vibecad-parametric-slot-constraint-v1\0"
_FEATURE_TYPE_IDS = {
    FeatureKind.PAD: "PartDesign::Pad",
    FeatureKind.POCKET: "PartDesign::Pocket",
    FeatureKind.REVOLVE: "PartDesign::Revolution",
    FeatureKind.HOLE: "PartDesign::Hole",
    FeatureKind.LINEAR_PATTERN: "PartDesign::LinearPattern",
    FeatureKind.CIRCULAR_PATTERN: "PartDesign::PolarPattern",
    FeatureKind.MIRROR: "PartDesign::Mirrored",
    FeatureKind.THICKNESS: "PartDesign::Thickness",
    FeatureKind.DRAFT: "PartDesign::Draft",
}
_EDGE_TREATMENT_TYPE_IDS = {
    EdgeTreatmentKind.FILLET: "Part::Fillet",
    EdgeTreatmentKind.CHAMFER: "Part::Chamfer",
}
_PATTERN_KINDS = frozenset(
    {FeatureKind.LINEAR_PATTERN, FeatureKind.CIRCULAR_PATTERN, FeatureKind.MIRROR}
)
_SURFACE_MODIFIER_KINDS = frozenset({FeatureKind.THICKNESS, FeatureKind.DRAFT})
_SKETCHLESS_FEATURE_KINDS = _PATTERN_KINDS | _SURFACE_MODIFIER_KINDS
_PROFILE_FEATURE_KINDS = frozenset(
    {FeatureKind.PAD, FeatureKind.POCKET, FeatureKind.REVOLVE, FeatureKind.HOLE}
)
_PATTERN_AXIS_OBJECTS = {
    "@body_x": "X_Axis",
    "@body_y": "Y_Axis",
    "@body_z": "Z_Axis",
}
_PATTERN_DIRECTION_OBJECTS = {
    PatternDirection.X_AXIS: "X_Axis",
    PatternDirection.Y_AXIS: "Y_Axis",
    PatternDirection.Z_AXIS: "Z_Axis",
}
_MIRROR_PLANE_OBJECTS = {
    MirrorPlane.XY_PLANE: "XY_Plane",
    MirrorPlane.XZ_PLANE: "XZ_Plane",
    MirrorPlane.YZ_PLANE: "YZ_Plane",
}
_ORIGIN_PLANE_OBJECTS = {
    OriginPlane.XY: "XY_Plane",
    OriginPlane.XZ: "XZ_Plane",
    OriginPlane.YZ: "YZ_Plane",
}
_ORIGIN_PLANE_PULL_DIRECTIONS = {
    OriginPlane.XY: "Z_Axis",
    OriginPlane.XZ: "Y_Axis",
    OriginPlane.YZ: "X_Axis",
}


class ParametricCompileErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED = "unsupported"
    CAD_FAILURE = "cad_failure"
    SOLVER_FAILURE = "solver_failure"
    PROFILE_FAILURE = "profile_failure"
    FEATURE_FAILURE = "feature_failure"
    METADATA_FAILURE = "metadata_failure"


_ERROR_MESSAGES = {
    ParametricCompileErrorCode.INVALID_INPUT: ("The parametric compiler input is invalid."),
    ParametricCompileErrorCode.UNSUPPORTED: (
        "The parametric value is not supported by this compiler slice."
    ),
    ParametricCompileErrorCode.CAD_FAILURE: (
        "The CAD runtime could not compile the parametric design."
    ),
    ParametricCompileErrorCode.SOLVER_FAILURE: (
        "The parametric sketch could not be solved safely."
    ),
    ParametricCompileErrorCode.PROFILE_FAILURE: (
        "The parametric feature profile is not safely closed."
    ),
    ParametricCompileErrorCode.FEATURE_FAILURE: (
        "The parametric feature did not produce a safe single solid."
    ),
    ParametricCompileErrorCode.METADATA_FAILURE: "The parametric CAD metadata is invalid.",
}


class ParametricCompileError(RuntimeError):
    """Fixed non-reflective compiler failure."""

    __slots__ = ("code", "path", "message")

    def __init__(self, code: ParametricCompileErrorCode, path: str = "") -> None:
        if type(code) is not ParametricCompileErrorCode:
            raise TypeError("code must be ParametricCompileErrorCode")
        if (
            type(path) is not str
            or len(path) > _MAX_ERROR_PATH
            or not is_canonical_json_pointer(path)
        ):
            raise ValueError("path must be a bounded canonical JSON Pointer")
        self.code = code
        self.path = path
        self.message = _ERROR_MESSAGES[code]
        super().__init__(self.message)


def _raise(code: ParametricCompileErrorCode, path: str = "") -> None:
    raise ParametricCompileError(code, path)


@dataclass(frozen=True, slots=True)
class ParametricEntityFact:
    name: str
    value: bool | int | float | str
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class SketchSolverFacts:
    solve_result: int
    dof: int
    fully_constrained: bool
    geometry_count: int
    constraint_count: int
    conflicting_constraint_count: int
    redundant_constraint_count: int
    malformed_constraint_count: int

    @property
    def solver_ok(self) -> bool:
        return self.solve_result == 0


@dataclass(frozen=True, slots=True)
class CompiledSketchBinding:
    sketch_id: str
    object: object
    geometry_indices: Mapping[str, tuple[int, ...]]
    constraint_indices: Mapping[str, tuple[int, ...]]
    solver: SketchSolverFacts


@dataclass(frozen=True, slots=True)
class CompiledFeatureBinding:
    feature_id: str
    object: object


@dataclass(frozen=True, slots=True)
class CompiledParametricDesign:
    design_id: str
    design_digest: str
    body: object
    result_object: object
    parameter_carrier: object
    sketches: tuple[CompiledSketchBinding, ...]
    features: tuple[CompiledFeatureBinding, ...] = ()
    edge_treatments: tuple[CompiledFeatureBinding, ...] = ()


CompiledSketchSet = CompiledParametricDesign


@dataclass(frozen=True, slots=True)
class ParametricParameterEdit:
    design_id: str
    design_digest: str
    body: object
    parameter_id: str
    parameter_name: str
    unit: str
    before_value: float
    after_value: float
    consumer_ids: tuple[str, ...]


def _canonical(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)


def _suffix(value: str) -> str:
    suffix = value.rsplit("_", 1)[-1]
    if _HEX_32.fullmatch(suffix) is None:
        _raise(ParametricCompileErrorCode.INVALID_INPUT)
    return suffix


def _load_freecad_modules() -> tuple[object, object, object]:
    from vibecad.freecad_env import prepare_freecad_import

    prepare_freecad_import()
    import FreeCAD  # noqa: PLC0415
    import Part  # noqa: PLC0415
    import Sketcher  # noqa: PLC0415

    return FreeCAD, Part, Sketcher


def _geometry_source_index(sketch: ParametricSketch, geometry: SketchGeometry) -> int:
    indexes = getattr(sketch, "_geometry_source_indexes", None)
    if isinstance(indexes, Mapping) and geometry.id in indexes:
        value = indexes[geometry.id]
        if type(value) is int:
            return value
    return next(index for index, item in enumerate(sketch.geometries) if item.id == geometry.id)


def _slot_axis(geometry: SketchGeometry) -> str | None:
    if geometry.kind is not GeometryKind.SLOT:
        return None
    values = geometry.dimensions
    if values["x1_mm"] == values["x2_mm"]:
        return "vertical"
    if values["y1_mm"] == values["y2_mm"]:
        return "horizontal"
    return None


def _preflight(session: object, design: object) -> ParametricDesignIR:
    if type(design) is not ParametricDesignIR:
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/design")
    try:
        document = session.doc  # type: ignore[attr-defined]
        objects = tuple(document.Objects)
        undo_mode = document.UndoMode
        transaction = session._transaction  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/session")
    if (
        document is None
        or objects
        or type(undo_mode) is not int
        or undo_mode != 1
        or not callable(transaction)
    ):
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/session")
    for sketch_index, sketch in enumerate(design.sketches):
        geometry_by_id = {item.id: item for item in sketch.geometries}
        declared_constraint_ids = {item.id for item in sketch.constraints}
        generated_constraint_ids = {
            _slot_constraint_id(geometry)
            for geometry in sketch.geometries
            if geometry.kind is GeometryKind.SLOT
        }
        if declared_constraint_ids & generated_constraint_ids:
            _raise(
                ParametricCompileErrorCode.INVALID_INPUT,
                f"/sketches/{sketch_index}/constraints",
            )
        expanded_geometry_count = sum(
            _SLOT_NATIVE_GEOMETRY_COUNT if geometry.kind is GeometryKind.SLOT else 1
            for geometry in sketch.geometries
        )
        expanded_constraint_count = len(sketch.constraints) + sum(
            _SLOT_NATIVE_CONSTRAINT_COUNT
            for geometry in sketch.geometries
            if geometry.kind is GeometryKind.SLOT
        )
        metadata_constraint_entry_count = len(sketch.constraints) + sum(
            geometry.kind is GeometryKind.SLOT for geometry in sketch.geometries
        )
        if expanded_geometry_count > _MAX_COMPILED_SKETCH_GEOMETRIES:
            _raise(
                ParametricCompileErrorCode.UNSUPPORTED,
                f"/sketches/{sketch_index}/geometries",
            )
        if expanded_constraint_count > _MAX_COMPILED_SKETCH_CONSTRAINTS:
            _raise(
                ParametricCompileErrorCode.UNSUPPORTED,
                f"/sketches/{sketch_index}/constraints",
            )
        if metadata_constraint_entry_count > _MAX_COMPILED_SKETCH_CONSTRAINT_ENTRIES:
            _raise(
                ParametricCompileErrorCode.UNSUPPORTED,
                f"/sketches/{sketch_index}/constraints",
            )
        for geometry in sketch.geometries:
            if geometry.kind is GeometryKind.SLOT and _slot_axis(geometry) is None:
                source_index = _geometry_source_index(sketch, geometry)
                _raise(
                    ParametricCompileErrorCode.UNSUPPORTED,
                    f"/sketches/{sketch_index}/geometries/{source_index}/dimensions",
                )
            if (
                not geometry.construction
                and sketch.role is SketchRole.PROFILE
                and geometry.kind is GeometryKind.POINT
            ):
                source_index = _geometry_source_index(sketch, geometry)
                _raise(
                    ParametricCompileErrorCode.UNSUPPORTED,
                    f"/sketches/{sketch_index}/geometries/{source_index}/kind",
                )
            if (
                not geometry.construction
                and sketch.role is SketchRole.HOLE_LOCATIONS
                and geometry.kind is not GeometryKind.CIRCLE
            ):
                source_index = _geometry_source_index(sketch, geometry)
                _raise(
                    ParametricCompileErrorCode.UNSUPPORTED,
                    f"/sketches/{sketch_index}/geometries/{source_index}/kind",
                )
        for constraint_index, constraint in enumerate(sketch.constraints):
            for reference_index, reference in enumerate(constraint.references):
                geometry = geometry_by_id.get(reference.target)
                if geometry is not None and geometry.kind is GeometryKind.SLOT:
                    _raise(
                        ParametricCompileErrorCode.UNSUPPORTED,
                        f"/sketches/{sketch_index}/constraints/{constraint_index}"
                        f"/references/{reference_index}/target",
                    )
    for feature_index, feature in enumerate(design.features):
        if (
            feature.kind is FeatureKind.HOLE
            and len(feature.location_geometry_ids) > _MAX_COMPILED_HOLE_LOCATIONS
        ):
            _raise(
                ParametricCompileErrorCode.UNSUPPORTED,
                f"/features/{feature_index}/location_geometry_ids",
            )
    return design


def _expected_profile_edge_count(sketch: ParametricSketch) -> int:
    count = sum(
        0
        if geometry.construction
        else _SLOT_NATIVE_GEOMETRY_COUNT
        if geometry.kind is GeometryKind.SLOT
        else 1
        if geometry.kind in {GeometryKind.LINE, GeometryKind.CIRCLE, GeometryKind.ARC}
        else 0
        for geometry in sketch.geometries
    )
    if count < 1:
        _raise(ParametricCompileErrorCode.PROFILE_FAILURE)
    return count


def _require_profile_closure(
    obj: object,
    *,
    expected_edge_count: int,
    path: str = "",
) -> int:
    try:
        shape = obj.Shape  # type: ignore[attr-defined]
        edges = tuple(shape.Edges)
        wires = tuple(shape.Wires)
        closed = tuple(wire.isClosed() for wire in wires)
        wire_edge_count = sum(len(tuple(wire.Edges)) for wire in wires)
        valid = not shape.isNull() and shape.isValid()
    except Exception:
        _raise(ParametricCompileErrorCode.PROFILE_FAILURE, path)
    if (
        type(expected_edge_count) is not int
        or expected_edge_count < 1
        or not valid
        or len(edges) != expected_edge_count
        or not wires
        or not all(closed)
        or wire_edge_count != expected_edge_count
    ):
        _raise(ParametricCompileErrorCode.PROFILE_FAILURE, path)
    return len(wires)


def _require_supported_feature_profile(
    kind: FeatureKind,
    wire_count: int,
    *,
    path: str = "",
) -> None:
    """Keep multi-loop pockets closed until every pocket loop has its own proof."""

    if kind is FeatureKind.POCKET and wire_count != 1:
        _raise(ParametricCompileErrorCode.UNSUPPORTED, path)


def _require_hole_location_cuts(
    FreeCAD: object,
    sketch_object: object,
    previous: object,
    result: object,
    *,
    location_geometry_ids: tuple[str, ...],
    depth_mm: float | None,
    path: str = "",
) -> None:
    """Prove that every declared hole axis removes material from the prior solid.

    A total volume decrease is insufficient for a multi-location Hole: one valid
    bore could otherwise hide a missed location.  Probe a bounded set of points
    along each declared sketch-normal axis and require at least one point that is
    inside the previous solid and outside the resulting solid.
    """

    try:
        before_shape = previous.Shape  # type: ignore[attr-defined]
        after_shape = result.Shape  # type: ignore[attr-defined]
        placement = sketch_object.Placement  # type: ignore[attr-defined]
        normal = placement.Rotation.multVec(FreeCAD.Vector(0, 0, 1))
        normal_length = float(normal.Length)
        span = float(before_shape.BoundBox.DiagonalLength)
        sketch_metadata = _read_metadata(sketch_object, required=True)
        geometry_entries = _sequence(sketch_metadata["geometries"], maximum=256)
    except Exception:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE, path)
    if (
        not math.isfinite(normal_length)
        or normal_length <= 0
        or not math.isfinite(span)
        or span <= 0
    ):
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE, path)
    try:
        unit_normal = FreeCAD.Vector(
            float(normal.x) / normal_length,
            float(normal.y) / normal_length,
            float(normal.z) / normal_length,
        )
    except Exception:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE, path)
    geometry_index_by_id: dict[str, int] = {}
    for entry in geometry_entries:
        try:
            data = _exact_mapping(entry, {"id", "indices", "type_ids", "construction"})
            geometry_id = _text(data["id"], _IR_ID)
            indices = _sequence(data["indices"], maximum=1)
            type_ids = _sequence(data["type_ids"], maximum=1)
            construction = _sequence(data["construction"], maximum=1)
        except ParametricCompileError:
            raise
        except Exception:
            _raise(ParametricCompileErrorCode.FEATURE_FAILURE, path)
        if (
            len(indices) == 1
            and type(indices[0]) is int
            and indices[0] >= 0
            and type_ids == ["Part::GeomCircle"]
            and construction == [False]
        ):
            geometry_index_by_id[geometry_id] = indices[0]
    scan_offsets = tuple(-span + (2.0 * span * index / 64.0) for index in range(65))
    near_plane_offsets = tuple(
        sign * span * fraction
        for sign in (-1.0, 1.0)
        for fraction in (2e-9, 1e-8, 1e-7, 1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 0.1, 0.25, 0.5)
    )
    local_offsets: tuple[float, ...] = ()
    if depth_mm is not None:
        if not math.isfinite(depth_mm) or depth_mm <= 0:
            _raise(ParametricCompileErrorCode.FEATURE_FAILURE, path)
        local_offsets = tuple(
            sign * depth_mm * fraction
            for sign in (-1.0, 1.0)
            for fraction in (0.001, 0.01, 0.1, 0.25, 0.5, 0.75, 0.999)
        )
    tolerance = max(1.0, span) * 1e-9

    def probe(origin: object, offset: float) -> object:
        return origin + FreeCAD.Vector(
            float(unit_normal.x) * offset,
            float(unit_normal.y) * offset,
            float(unit_normal.z) * offset,
        )

    for location_index, geometry_id in enumerate(location_geometry_ids):
        geometry_index = geometry_index_by_id.get(geometry_id)
        if geometry_index is None:
            _raise(
                ParametricCompileErrorCode.FEATURE_FAILURE,
                f"{path}/location_geometry_ids/{location_index}",
            )
        try:
            local_center = sketch_object.Geometry[geometry_index].Center
            center = placement.multVec(local_center)
            removed = any(
                before_shape.isInside(probe(center, offset), tolerance, False)
                and not after_shape.isInside(probe(center, offset), tolerance, False)
                for offset in (*local_offsets, *near_plane_offsets, *scan_offsets)
            )
        except Exception:
            _raise(
                ParametricCompileErrorCode.FEATURE_FAILURE,
                f"{path}/location_geometry_ids/{location_index}",
            )
        if not removed:
            _raise(
                ParametricCompileErrorCode.FEATURE_FAILURE,
                f"{path}/location_geometry_ids/{location_index}",
            )


def _revolution_axis_token(sketch: ParametricSketch, axis: str) -> str:
    if axis == "@sketch_x":
        return "H_Axis"
    if axis == "@sketch_y":
        return "V_Axis"
    construction_axes = tuple(
        geometry.id
        for geometry in sketch.geometries
        if geometry.construction and geometry.kind is GeometryKind.LINE
    )
    try:
        return f"Axis{construction_axes.index(axis)}"
    except ValueError:
        _raise(ParametricCompileErrorCode.INVALID_INPUT)


def _feature_parameter_bindings(
    feature: PartDesignFeature,
) -> tuple[tuple[str, str, str], ...]:
    if feature.kind is FeatureKind.PAD:
        return (("length", feature.parameters["length"], "Length"),)
    if feature.kind is FeatureKind.POCKET:
        if feature.extent is FeatureExtent.THROUGH_ALL:
            return ()
        return (("length", feature.parameters["length"], "Length"),)
    if feature.kind is FeatureKind.REVOLVE:
        return (("angle", feature.parameters["angle"], "Angle"),)
    if feature.kind is FeatureKind.LINEAR_PATTERN:
        return (("length", feature.parameters["length"], "Length"),)
    if feature.kind is FeatureKind.CIRCULAR_PATTERN:
        return (("angle", feature.parameters["angle"], "Angle"),)
    if feature.kind is FeatureKind.MIRROR:
        return ()
    if feature.kind is FeatureKind.THICKNESS:
        return (("thickness", feature.parameters["thickness"], "Value"),)
    if feature.kind is FeatureKind.DRAFT:
        return (("angle", feature.parameters["angle"], "Angle"),)
    bindings = [("diameter", feature.parameters["diameter"], "Diameter")]
    if feature.extent is FeatureExtent.LENGTH:
        bindings.append(("depth", feature.parameters["depth"], "Depth"))
    return tuple(bindings)


def _edge_treatment_metadata_targets(
    treatment: EdgeTreatmentFeature,
    parameter_properties: Mapping[str, str],
) -> list[dict[str, object]]:
    return [
        {
            "edge": {
                "source_feature_id": target.edge.source_feature_id,
                "geometry_id": target.edge.geometry_id,
                "role": target.edge.role.value,
                "point": target.edge.point.value,
            },
            "start_parameter_id": target.start_parameter_id,
            "start_property": parameter_properties[target.start_parameter_id],
            "end_parameter_id": target.end_parameter_id,
            "end_property": parameter_properties[target.end_parameter_id],
            "forward": None,
        }
        for target in treatment.targets
    ]


def _surface_modifier_metadata_targets(
    feature: PartDesignFeature,
) -> list[dict[str, object]]:
    return [
        {
            "source_feature_id": target.source_feature_id,
            "role": target.role.value,
            "geometry_id": target.geometry_id,
        }
        for target in feature.face_targets
    ]


def _require_feature_shape(
    obj: object,
    previous: object | None,
    kind: FeatureKind,
    *,
    additive: bool | None = None,
    path: str = "",
) -> float:
    try:
        shape = obj.Shape  # type: ignore[attr-defined]
        solids = tuple(shape.Solids)
        volume = float(shape.Volume)
        valid = not shape.isNull() and shape.isValid()
        state = tuple(obj.State)  # type: ignore[attr-defined]
        status_method = obj.getStatusString  # type: ignore[attr-defined]
        if not callable(status_method):
            raise TypeError
        status = status_method()
        previous_volume = None if previous is None else float(previous.Shape.Volume)  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE, path)
    if (
        not valid
        or len(solids) != 1
        or not math.isfinite(volume)
        or volume <= 0
        or state != ("Up-to-date",)
        or status != "Valid"
    ):
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE, path)
    if previous_volume is not None:
        if not math.isfinite(previous_volume) or previous_volume <= 0:
            _raise(ParametricCompileErrorCode.FEATURE_FAILURE, path)
        tolerance = max(1.0, abs(previous_volume), abs(volume)) * 1e-9
        if kind in _SURFACE_MODIFIER_KINDS:
            changed = not math.isclose(volume, previous_volume, rel_tol=0.0, abs_tol=tolerance)
        else:
            if additive is None:
                additive = kind in {FeatureKind.PAD, FeatureKind.REVOLVE}
            if additive:
                changed = volume > previous_volume + tolerance
            else:
                changed = volume < previous_volume - tolerance
        if not changed:
            _raise(ParametricCompileErrorCode.FEATURE_FAILURE, path)
    return volume


def _pattern_is_additive(
    feature: PartDesignFeature,
    feature_by_id: Mapping[str, PartDesignFeature],
) -> bool:
    if feature.kind not in _PATTERN_KINDS or feature.source_feature_id is None:
        _raise(ParametricCompileErrorCode.INVALID_INPUT)
    source = feature_by_id.get(feature.source_feature_id)
    if source is None or source.kind not in _PROFILE_FEATURE_KINDS:
        _raise(ParametricCompileErrorCode.INVALID_INPUT)
    return source.kind in {FeatureKind.PAD, FeatureKind.REVOLVE}


def _origin_reference(document: object, name: str) -> object:
    try:
        reference = document.getObject(name)  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.CAD_FAILURE)
    if reference is None:
        _raise(ParametricCompileErrorCode.CAD_FAILURE)
    return reference


@dataclass(frozen=True, slots=True)
class _ResolvedSemanticEdge:
    index: int
    forward: bool


@dataclass(frozen=True, slots=True)
class _ResolvedTreatmentEdge:
    index: int
    start: float
    end: float
    forward: bool | None

    @property
    def native(self) -> tuple[int, float, float]:
        return self.index, self.start, self.end


@dataclass(frozen=True, slots=True)
class _ResolvedSemanticFace:
    index: int

    @property
    def native(self) -> str:
        return f"Face{self.index}"


def _point_tuple(value: object) -> tuple[float, float, float]:
    try:
        point = (float(value.x), float(value.y), float(value.z))  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if not all(math.isfinite(item) for item in point):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return point


def _distance_squared(left: object, right: object) -> float:
    a = _point_tuple(left)
    b = _point_tuple(right)
    return math.fsum((a[index] - b[index]) ** 2 for index in range(3))


def _edge_vertices(edge: object) -> tuple[object, ...]:
    try:
        vertices = tuple(edge.Vertexes)  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if not 1 <= len(vertices) <= 2:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    return vertices


def _edge_center(edge: object) -> object:
    try:
        return edge.CenterOfMass  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)


def _shape_edges(obj: object) -> tuple[object, ...]:
    try:
        edges = tuple(obj.Shape.Edges)  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if not 1 <= len(edges) <= 4096:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    return edges


def _shape_faces(obj: object) -> tuple[object, ...]:
    try:
        faces = tuple(obj.Shape.Faces)  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if not 1 <= len(faces) <= 4096:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    return faces


def _face_normal(face: object) -> object:
    try:
        if face.Surface.TypeId != "Part::GeomPlane":  # type: ignore[attr-defined]
            raise ValueError
        u_min, u_max, v_min, v_max = face.ParameterRange  # type: ignore[attr-defined]
        normal = face.normalAt(  # type: ignore[attr-defined]
            (float(u_min) + float(u_max)) / 2,
            (float(v_min) + float(v_max)) / 2,
        )
        length = float(normal.Length)
    except Exception:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    if not math.isfinite(length) or length <= 1e-12:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    return normal * (1.0 / length)


def _face_is_planar(face: object) -> bool:
    try:
        return face.Surface.TypeId == "Part::GeomPlane"  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)


def _face_center(face: object) -> object:
    try:
        return face.CenterOfMass  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)


def _element_history(obj: object, element_name: str) -> tuple[tuple[object, str], ...]:
    try:
        raw = obj.getElementHistory(element_name)  # type: ignore[attr-defined]
        entries = tuple(raw)
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    result: list[tuple[object, str]] = []
    for entry in entries:
        if type(entry) not in {list, tuple} or len(entry) < 2 or type(entry[1]) is not str:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        result.append((entry[0], entry[1]))
        if len(entry) >= 3:
            children = entry[2]
            if type(children) not in {list, tuple} or any(
                type(name) is not str for name in children
            ):
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            result.extend((entry[0], name) for name in children)
    return tuple(result)


def _history_contains(
    history: tuple[tuple[object, str], ...],
    obj: object,
    token: str,
) -> bool:
    return any(item is obj and name == token for item, name in history)


def _geometry_native_index(sketch_data: dict[str, object], geometry_id: str) -> int:
    matches: list[int] = []
    for raw in _sequence(sketch_data["geometries"], maximum=128):
        entry = _exact_mapping(raw, {"id", "indices", "type_ids", "construction"})
        if entry["id"] != geometry_id:
            continue
        indices = _sequence(entry["indices"], maximum=8)
        if len(indices) != 1 or type(indices[0]) is not int or indices[0] < 0:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        matches.append(indices[0])
    if len(matches) != 1:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return matches[0]


def _edge_candidates(
    obj: object,
    *,
    sketch: object | None = None,
    sketch_token: str | None = None,
    source_mapped_name: str | None = None,
) -> tuple[int, ...]:
    result: list[int] = []
    for index, _edge in enumerate(_shape_edges(obj), 1):
        history = _element_history(obj, f"Edge{index}")
        if sketch_token is not None and (
            sketch is None or not _history_contains(history, sketch, sketch_token)
        ):
            continue
        if source_mapped_name is not None and not any(
            name == source_mapped_name for _item, name in history
        ):
            continue
        result.append(index)
    return tuple(result)


def _same_edge_candidates(obj: object, source_edge: object) -> tuple[int, ...]:
    matches: list[int] = []
    for index, edge in enumerate(_shape_edges(obj), 1):
        try:
            same = bool(edge.isSame(source_edge))  # type: ignore[attr-defined]
        except Exception:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        if same:
            matches.append(index)
    return tuple(matches)


def _history_has_profile_section(
    history: tuple[tuple[object, str], ...],
    sketch: object,
) -> bool:
    return any(
        item is sketch and re.fullmatch(r"g[1-9][0-9]*;SKT", name) is not None
        for item, name in history
    )


def _sweep_source_edge(
    source_feature: object,
    sketch: object,
    feature_data: dict[str, object],
    geometry_index: int,
    point: ReferencePoint,
) -> int:
    """Resolve one generated sweep edge from live geometry, not transient EdgeN names.

    OCCT may canonicalize a shared sketch vertex under either adjacent profile
    geometry after an otherwise harmless recompute.  The generated edge itself
    remains the unique edge through the selected sketch endpoint and, for linear
    operations, parallel to the feature direction.
    """

    start, end = _original_geometry_points(sketch, geometry_index)
    origin = start if point is ReferencePoint.START else end
    try:
        kind = FeatureKind(_text(feature_data["feature_kind"]))
    except ValueError:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    direction = (
        None
        if kind is FeatureKind.REVOLVE
        else _linear_feature_direction(
            sketch,
            feature_data,
        )
    )
    candidates: list[int] = []
    for index, edge in enumerate(_shape_edges(source_feature), 1):
        vertices = _edge_vertices(edge)
        if direction is None:
            try:
                _FreeCAD, Part, _Sketcher = _load_freecad_modules()
                distance = float(edge.distToShape(Part.Vertex(origin))[0])  # type: ignore[attr-defined]
            except Exception:
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            if distance > 1e-7:
                continue
        else:
            if len(vertices) != 2:
                continue
            try:
                delta = vertices[1].Point - vertices[0].Point  # type: ignore[attr-defined]
                offset = origin - vertices[0].Point  # type: ignore[attr-defined]
                length_squared = float(delta.dot(delta))
                direction_length_squared = float(direction.dot(direction))
                if length_squared <= 1e-18 or direction_length_squared <= 1e-18:
                    continue
                alignment = abs(float(delta.dot(direction))) / math.sqrt(
                    length_squared * direction_length_squared
                )
                projection = float(offset.dot(delta)) / length_squared
                residual = offset - delta * projection
                distance = float(residual.Length)
            except Exception:
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            if (
                not math.isclose(alignment, 1.0, rel_tol=0.0, abs_tol=1e-7)
                or not -1e-7 <= projection <= 1.0 + 1e-7
                or distance > 1e-7
            ):
                continue
        history = _element_history(source_feature, f"Edge{index}")
        if _history_has_profile_section(history, sketch):
            continue
        candidates.append(index)
    if len(candidates) != 1:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    return candidates[0]


def _linear_feature_direction(sketch: object, feature_data: dict[str, object]) -> object:
    try:
        base = sketch.Placement.Base  # type: ignore[attr-defined]
        vector_type = type(base)
        normal = sketch.Placement.Rotation.multVec(vector_type(0, 0, 1))  # type: ignore[attr-defined]
        if feature_data["reversed"]:
            normal = normal * -1
        if float(normal.Length) <= 1e-12:
            raise ValueError
        return normal
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)


def _unique_extreme(
    values: tuple[tuple[int, float], ...],
    *,
    maximum: bool,
) -> int:
    if not values:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    ordered = sorted(values, key=lambda item: item[1], reverse=maximum)
    if len(ordered) > 1 and math.isclose(
        ordered[0][1],
        ordered[1][1],
        rel_tol=0.0,
        abs_tol=1e-7,
    ):
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    return ordered[0][0]


def _original_geometry_points(
    sketch: object,
    geometry_index: int,
) -> tuple[object, object]:
    try:
        geometry = sketch.Geometry[geometry_index]  # type: ignore[attr-defined]
        start = sketch.Placement.multVec(geometry.StartPoint)  # type: ignore[attr-defined]
        end = sketch.Placement.multVec(geometry.EndPoint)  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    return start, end


def _section_source_edge(
    source_feature: object,
    sketch: object,
    feature_data: dict[str, object],
    geometry_index: int,
    candidates: tuple[int, ...],
    role: SemanticEdgeRole,
) -> int:
    if len(candidates) < 2:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    try:
        kind = FeatureKind(_text(feature_data["feature_kind"]))
    except ValueError:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    edges = _shape_edges(source_feature)
    if kind is FeatureKind.REVOLVE:
        if len(candidates) != 2:
            _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
        start, end = _original_geometry_points(sketch, geometry_index)
        scores: list[tuple[int, float]] = []
        for index in candidates:
            vertices = _edge_vertices(edges[index - 1])
            if len(vertices) != 2:
                _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
            direct = _distance_squared(vertices[0].Point, start) + _distance_squared(  # type: ignore[attr-defined]
                vertices[1].Point,
                end,  # type: ignore[attr-defined]
            )
            crossed = _distance_squared(vertices[0].Point, end) + _distance_squared(  # type: ignore[attr-defined]
                vertices[1].Point,
                start,  # type: ignore[attr-defined]
            )
            scores.append((index, min(direct, crossed)))
        selected_start = _unique_extreme(tuple(scores), maximum=False)
        selected_end = next(index for index in candidates if index != selected_start)
        return selected_start if role is SemanticEdgeRole.SECTION_START else selected_end

    direction = _linear_feature_direction(sketch, feature_data)
    values = tuple(
        (index, float(_edge_center(edges[index - 1]).dot(direction)))  # type: ignore[attr-defined]
        for index in candidates
    )
    return _unique_extreme(values, maximum=role is SemanticEdgeRole.SECTION_END)


def _mapped_name(obj: object, edge_index: int) -> str:
    try:
        name = obj.Shape.getElementMappedName(f"Edge{edge_index}")  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return _text(name)


def _same_edge_orientation(current: object, source: object) -> bool:
    current_vertices = _edge_vertices(current)
    source_vertices = _edge_vertices(source)
    if len(current_vertices) != 2 or len(source_vertices) != 2:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    direct = _distance_squared(
        current_vertices[0].Point,
        source_vertices[0].Point,  # type: ignore[attr-defined]
    ) + _distance_squared(
        current_vertices[1].Point,
        source_vertices[1].Point,  # type: ignore[attr-defined]
    )
    crossed = _distance_squared(
        current_vertices[0].Point,
        source_vertices[1].Point,  # type: ignore[attr-defined]
    ) + _distance_squared(
        current_vertices[1].Point,
        source_vertices[0].Point,  # type: ignore[attr-defined]
    )
    if math.isclose(direct, crossed, rel_tol=0.0, abs_tol=1e-12):
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    return direct < crossed


def _section_source_forward(
    source_feature: object,
    sketch: object,
    geometry_index: int,
    edge_index: int,
) -> bool:
    edge = _shape_edges(source_feature)[edge_index - 1]
    vertices = _edge_vertices(edge)
    if len(vertices) != 2:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    expected = (f"g{geometry_index + 1}v11;SKT", f"g{geometry_index + 1}v22;SKT")
    actual: list[str] = []
    try:
        shape_vertices = tuple(source_feature.Shape.Vertexes)  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    for endpoint in vertices:
        indexes = tuple(
            index
            for index, vertex in enumerate(shape_vertices, 1)
            if bool(endpoint.isSame(vertex))  # type: ignore[attr-defined]
        )
        if len(indexes) != 1:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        history = _element_history(source_feature, f"Vertex{indexes[0]}")
        tokens = tuple(token for token in expected if _history_contains(history, sketch, token))
        if len(tokens) != 1:
            _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
        actual.append(tokens[0])
    if tuple(actual) == expected:
        return True
    if tuple(reversed(actual)) == expected:
        return False
    _raise(ParametricCompileErrorCode.FEATURE_FAILURE)


def _sweep_forward(
    edge: object,
    sketch: object,
    feature_data: dict[str, object],
    geometry_index: int,
    point: ReferencePoint,
) -> bool:
    vertices = _edge_vertices(edge)
    if len(vertices) != 2:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    try:
        kind = FeatureKind(_text(feature_data["feature_kind"]))
    except ValueError:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if kind is not FeatureKind.REVOLVE:
        direction = _linear_feature_direction(sketch, feature_data)
        delta = vertices[1].Point - vertices[0].Point  # type: ignore[attr-defined]
        score = float(delta.dot(direction))
        if math.isclose(score, 0.0, rel_tol=0.0, abs_tol=1e-9):
            _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
        return score > 0
    start, end = _original_geometry_points(sketch, geometry_index)
    origin = start if point is ReferencePoint.START else end
    distances = tuple(_distance_squared(vertex.Point, origin) for vertex in vertices)  # type: ignore[attr-defined]
    if math.isclose(distances[0], distances[1], rel_tol=0.0, abs_tol=1e-12):
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    return distances[0] < distances[1]


def _resolve_semantic_edge(
    base: object,
    *,
    source_feature: object,
    sketch: object,
    feature_data: dict[str, object],
    sketch_data: dict[str, object],
    reference: SemanticEdgeReference,
    require_orientation: bool,
) -> _ResolvedSemanticEdge:
    geometry_index = _geometry_native_index(sketch_data, reference.geometry_id)
    if reference.role is SemanticEdgeRole.SWEEP:
        sketch_token = None
        source_index = _sweep_source_edge(
            source_feature,
            sketch,
            feature_data,
            geometry_index,
            reference.point,
        )
    else:
        sketch_token = f"g{geometry_index + 1};SKT"
        source_candidates = _edge_candidates(
            source_feature,
            sketch=sketch,
            sketch_token=sketch_token,
        )
        source_index = _section_source_edge(
            source_feature,
            sketch,
            feature_data,
            geometry_index,
            source_candidates,
            reference.role,
        )
    source_mapped_name = _mapped_name(source_feature, source_index)
    matches = _edge_candidates(
        base,
        sketch=sketch,
        sketch_token=sketch_token,
        source_mapped_name=source_mapped_name,
    )
    if len(matches) != 1:
        # Native surface modifiers retain the source Pad's mapped-edge
        # identity, but may drop the direct Sketcher history token.  The
        # mapped name is still compiler-derived from the authenticated source
        # edge; accept it only when it resolves uniquely in the current base.
        matches = _edge_candidates(base, source_mapped_name=source_mapped_name)
    if len(matches) != 1:
        matches = _same_edge_candidates(
            base,
            _shape_edges(source_feature)[source_index - 1],
        )
    if len(matches) != 1 and reference.role in {
        SemanticEdgeRole.SECTION_START,
        SemanticEdgeRole.SECTION_END,
    }:
        matches = _semantic_section_edge_candidates(
            base,
            source_feature=source_feature,
            sketch=sketch,
            feature_data=feature_data,
            sketch_data=sketch_data,
            reference=reference,
        )
    if len(matches) != 1:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    current_index = matches[0]
    if not require_orientation:
        return _ResolvedSemanticEdge(index=current_index, forward=True)
    current_edge = _shape_edges(base)[current_index - 1]
    source_edge = _shape_edges(source_feature)[source_index - 1]
    if reference.role is SemanticEdgeRole.SWEEP:
        forward = _sweep_forward(
            current_edge,
            sketch,
            feature_data,
            geometry_index,
            reference.point,
        )
    else:
        source_forward = _section_source_forward(
            source_feature,
            sketch,
            geometry_index,
            source_index,
        )
        forward = (
            source_forward
            if _same_edge_orientation(current_edge, source_edge)
            else not source_forward
        )
    return _ResolvedSemanticEdge(index=current_index, forward=forward)


def _face_candidates(
    obj: object,
    *,
    source_mapped_name: str,
) -> tuple[int, ...]:
    result: list[int] = []
    for index, _face in enumerate(_shape_faces(obj), 1):
        history = _element_history(obj, f"Face{index}")
        if any(name == source_mapped_name for _item, name in history):
            result.append(index)
    return tuple(result)


def _same_face_candidates(obj: object, source_face: object) -> tuple[int, ...]:
    matches: list[int] = []
    for index, face in enumerate(_shape_faces(obj), 1):
        try:
            same = bool(face.isSame(source_face))  # type: ignore[attr-defined]
        except Exception:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        if same:
            matches.append(index)
    return tuple(matches)


def _source_semantic_face(
    source_feature: object,
    *,
    sketch: object,
    feature_data: dict[str, object],
    sketch_data: dict[str, object],
    reference: SemanticFaceReference,
) -> int:
    try:
        if FeatureKind(_text(feature_data["feature_kind"])) is not FeatureKind.PAD:
            raise ValueError
    except ValueError:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    direction = _linear_feature_direction(sketch, feature_data)
    try:
        direction = direction * (1.0 / float(direction.Length))
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    faces = _shape_faces(source_feature)
    if reference.role is SemanticFaceRole.SWEEP:
        if reference.geometry_id is None:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        geometry_index = _geometry_native_index(sketch_data, reference.geometry_id)
        sketch_token = f"g{geometry_index + 1};SKT"
        candidates: list[int] = []
        for index, face in enumerate(faces, 1):
            history = _element_history(source_feature, f"Face{index}")
            if not _history_contains(history, sketch, sketch_token):
                continue
            alignment = abs(float(_face_normal(face).dot(direction)))  # type: ignore[attr-defined]
            if math.isclose(alignment, 0.0, rel_tol=0.0, abs_tol=1e-7):
                candidates.append(index)
        if len(candidates) != 1:
            _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
        return candidates[0]

    values: list[tuple[int, float]] = []
    for index, face in enumerate(faces, 1):
        if not _face_is_planar(face):
            continue
        normal = _face_normal(face)
        alignment = abs(float(normal.dot(direction)))  # type: ignore[attr-defined]
        if not math.isclose(alignment, 1.0, rel_tol=0.0, abs_tol=1e-7):
            continue
        values.append((index, float(_face_center(face).dot(direction))))  # type: ignore[attr-defined]
    return _unique_extreme(
        tuple(values),
        maximum=reference.role is SemanticFaceRole.SECTION_END,
    )


def _resolve_semantic_face(
    base: object,
    *,
    source_feature: object,
    sketch: object,
    feature_data: dict[str, object],
    sketch_data: dict[str, object],
    reference: SemanticFaceReference,
    require_planar: bool,
) -> _ResolvedSemanticFace:
    source_index = _source_semantic_face(
        source_feature,
        sketch=sketch,
        feature_data=feature_data,
        sketch_data=sketch_data,
        reference=reference,
    )
    source_face = _shape_faces(source_feature)[source_index - 1]
    try:
        source_mapped_name = _text(
            source_feature.Shape.getElementMappedName(  # type: ignore[attr-defined]
                f"Face{source_index}"
            )
        )
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    matches = _face_candidates(base, source_mapped_name=source_mapped_name)
    if len(matches) != 1:
        matches = _same_face_candidates(base, source_face)
    if len(matches) != 1:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    current_index = matches[0]
    if require_planar:
        _face_normal(_shape_faces(base)[current_index - 1])
    return _ResolvedSemanticFace(index=current_index)


def _shared_face_edge_candidates(
    base: object,
    first_face_index: int,
    second_face_index: int,
) -> tuple[int, ...]:
    faces = _shape_faces(base)
    if not (
        1 <= first_face_index <= len(faces)
        and 1 <= second_face_index <= len(faces)
        and first_face_index != second_face_index
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    try:
        first_edges = tuple(faces[first_face_index - 1].Edges)  # type: ignore[attr-defined]
        second_edges = tuple(faces[second_face_index - 1].Edges)  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if not first_edges or not second_edges:
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)

    matches: list[int] = []
    for index, edge in enumerate(_shape_edges(base), 1):
        try:
            belongs_to_first = any(
                bool(edge.isSame(candidate))
                for candidate in first_edges  # type: ignore[attr-defined]
            )
            belongs_to_second = any(
                bool(edge.isSame(candidate))
                for candidate in second_edges  # type: ignore[attr-defined]
            )
        except Exception:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        if belongs_to_first and belongs_to_second:
            matches.append(index)
    return tuple(matches)


def _semantic_section_edge_candidates(
    base: object,
    *,
    source_feature: object,
    sketch: object,
    feature_data: dict[str, object],
    sketch_data: dict[str, object],
    reference: SemanticEdgeReference,
) -> tuple[int, ...]:
    try:
        section_role = SemanticFaceRole(reference.role.value)
    except ValueError:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    sweep = _resolve_semantic_face(
        base,
        source_feature=source_feature,
        sketch=sketch,
        feature_data=feature_data,
        sketch_data=sketch_data,
        reference=SemanticFaceReference(
            source_feature_id=reference.source_feature_id,
            role=SemanticFaceRole.SWEEP,
            geometry_id=reference.geometry_id,
        ),
        require_planar=False,
    )
    section = _resolve_semantic_face(
        base,
        source_feature=source_feature,
        sketch=sketch,
        feature_data=feature_data,
        sketch_data=sketch_data,
        reference=SemanticFaceReference(
            source_feature_id=reference.source_feature_id,
            role=section_role,
        ),
        require_planar=True,
    )
    return _shared_face_edge_candidates(base, sweep.index, section.index)


def _surface_face_target(value: object) -> SemanticFaceReference:
    entry = _exact_mapping(value, {"source_feature_id", "role", "geometry_id"})
    try:
        return SemanticFaceReference(
            source_feature_id=_text(entry["source_feature_id"], _IR_ID),
            role=_text(entry["role"]),
            geometry_id=(
                None if entry["geometry_id"] is None else _text(entry["geometry_id"], _IR_ID)
            ),
        )
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)


def _resolved_surface_faces(
    base: object,
    data: dict[str, object],
    by_ir_id: Mapping[str, tuple[object, dict[str, object]]],
) -> tuple[_ResolvedSemanticFace, ...]:
    try:
        kind = FeatureKind(_text(data["feature_kind"]))
    except ValueError:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    maximum = MAX_THICKNESS_FACES if kind is FeatureKind.THICKNESS else MAX_DRAFT_FACES
    references = tuple(
        _surface_face_target(item) for item in _sequence(data["face_targets"], maximum=maximum)
    )
    if not references or len({item.source_feature_id for item in references}) != 1:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    source_record = by_ir_id.get(references[0].source_feature_id)
    if source_record is None or source_record[1]["kind"] != "feature":
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    source_feature, source_data = source_record
    sketch_id = _text(source_data["sketch_id"], _IR_ID)
    sketch_record = by_ir_id.get(sketch_id)
    if sketch_record is None or sketch_record[1]["kind"] != "sketch":
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    sketch, sketch_data = sketch_record
    resolved = tuple(
        _resolve_semantic_face(
            base,
            source_feature=source_feature,
            sketch=sketch,
            feature_data=source_data,
            sketch_data=sketch_data,
            reference=reference,
            require_planar=kind is FeatureKind.DRAFT,
        )
        for reference in references
    )
    if len({item.index for item in resolved}) != len(resolved):
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    return resolved


def _parameter_property(parameter: DesignParameter) -> str:
    return f"P_{_suffix(parameter.id)}"


def _compiled_parameter_expression(
    expression: DerivedParameterExpression,
    parameter_properties: Mapping[str, str],
    unit: DesignUnit,
) -> str:
    terms: list[str] = []
    for parameter_id, coefficient in expression.terms.items():
        property_name = parameter_properties.get(parameter_id)
        if property_name is None:
            _raise(ParametricCompileErrorCode.INVALID_INPUT)
        terms.append(f"{_canonical(coefficient)} * {property_name}")
    if expression.constant != 0:
        terms.append(f"{_canonical(expression.constant)} {unit.value}")
    if not terms:
        _raise(ParametricCompileErrorCode.INVALID_INPUT)
    return " + ".join(terms)


def _parameter_expression_metadata(
    expression: DerivedParameterExpression,
    parameter_properties: Mapping[str, str],
    unit: DesignUnit,
) -> dict[str, object]:
    return {
        "compiled": _compiled_parameter_expression(expression, parameter_properties, unit),
        "constant": expression.constant,
        "terms": [
            {
                "coefficient": coefficient,
                "parameter_id": parameter_id,
                "property": parameter_properties[parameter_id],
            }
            for parameter_id, coefficient in expression.terms.items()
        ],
    }


def _constraint_name(constraint: SketchConstraint) -> str:
    return f"C_{_suffix(constraint.id)}"


def _write_metadata(obj: object, payload: Mapping[str, object]) -> str:
    raw = _canonical(payload)
    if len(raw.encode("utf-8")) > _MAX_METADATA_BYTES:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    try:
        properties = set(obj.PropertiesList)  # type: ignore[attr-defined]
        if PARAMETRIC_METADATA_PROPERTY in properties:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        obj.addProperty(  # type: ignore[attr-defined]
            "App::PropertyString",
            PARAMETRIC_METADATA_PROPERTY,
            _METADATA_GROUP,
            _METADATA_DOC,
        )
        setattr(obj, PARAMETRIC_METADATA_PROPERTY, raw)
        obj.setEditorMode(PARAMETRIC_METADATA_PROPERTY, 3)  # type: ignore[attr-defined]
        obj.setPropertyStatus(  # type: ignore[attr-defined]
            PARAMETRIC_METADATA_PROPERTY,
            "LockDynamic",
        )
    except ParametricCompileError:
        raise
    except Exception:
        _raise(ParametricCompileErrorCode.CAD_FAILURE)
    _read_metadata(obj, required=True)
    return raw


def _metadata_property_envelope(obj: object) -> bool:
    try:
        raw_properties = getattr(obj, "PropertiesList", None)
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if raw_properties is None:
        return False
    try:
        properties = set(raw_properties)
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if PARAMETRIC_METADATA_PROPERTY not in properties:
        return False
    try:
        property_type = obj.getTypeIdOfProperty(PARAMETRIC_METADATA_PROPERTY)  # type: ignore[attr-defined]
        editor_modes = set(obj.getEditorMode(PARAMETRIC_METADATA_PROPERTY))  # type: ignore[attr-defined]
        property_status = set(obj.getPropertyStatus(PARAMETRIC_METADATA_PROPERTY))  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if (
        property_type != "App::PropertyString"
        or not {"ReadOnly", "Hidden"}.issubset(editor_modes)
        or "LockDynamic" not in property_status
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return True


def _exact_mapping(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return value


def _text(value: object, pattern: re.Pattern[str] | None = None) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > 256:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if pattern is not None and pattern.fullmatch(value) is None:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return value


def _integer(value: object, *, maximum: int) -> int:
    if type(value) is not int or value < 0 or value > maximum:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return value


def _sequence(value: object, *, maximum: int) -> list[object]:
    if type(value) is not list or len(value) > maximum:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return value


def _validate_common_metadata(data: dict[str, object]) -> str:
    if data["schema"] != _METADATA_SCHEMA:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    kind = _text(data["kind"])
    if kind not in {"body", "edge_treatment", "feature", "parameters", "sketch"}:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    design_id = _text(data["design_id"], _IR_ID)
    if not design_id.startswith("ir_design_"):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    _text(data["design_digest"], _HEX_64)
    _text(data["ir_id"], _IR_ID)
    return kind


def _read_metadata(obj: object, *, required: bool = False) -> dict[str, object] | None:
    if not _metadata_property_envelope(obj):
        if required:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        return None
    try:
        raw = getattr(obj, PARAMETRIC_METADATA_PROPERTY)
        if type(raw) is not str or len(raw.encode("utf-8")) > _MAX_METADATA_BYTES:
            raise ValueError
        parsed = json.loads(raw)
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if _canonical(parsed) != raw or type(parsed) is not dict:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    kind = parsed.get("kind")
    common = {"schema", "kind", "design_id", "design_digest", "ir_id"}
    if kind == "body":
        allowed = common | {"edge_treatment_ids", "feature_ids", "sketch_ids"}
        if not set(parsed) <= allowed or not common | {"feature_ids", "sketch_ids"} <= set(parsed):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        data = parsed
    elif kind == "parameters":
        data = _exact_mapping(parsed, common | {"parameters"})
    elif kind == "sketch":
        data = _exact_mapping(parsed, common | {"geometries", "constraints"})
    elif kind == "feature":
        feature_keys = common | {
            "axis",
            "axis_token",
            "base_feature_id",
            "bindings",
            "extent",
            "feature_index",
            "feature_kind",
            "location_geometry_ids",
            "reversed",
            "sketch_id",
            "symmetric",
        }
        pattern_keys = {
            "direction",
            "direction_token",
            "mirror_plane",
            "mirror_plane_token",
            "occurrences",
            "source_feature_id",
        }
        surface_keys = {
            "face_targets",
            "neutral_plane",
            "neutral_plane_token",
            "pull_direction_token",
            "refine",
            "thickness_intersection",
            "thickness_join",
            "thickness_mode",
        }
        if parsed.get("feature_kind") in {item.value for item in _PATTERN_KINDS}:
            data = _exact_mapping(parsed, feature_keys | pattern_keys)
        elif parsed.get("feature_kind") in {item.value for item in _SURFACE_MODIFIER_KINDS}:
            data = _exact_mapping(parsed, feature_keys | surface_keys)
        else:
            data = _exact_mapping(parsed, feature_keys)
    elif kind == "edge_treatment":
        data = _exact_mapping(
            parsed,
            common
            | {
                "base_feature_id",
                "targets",
                "treatment_index",
                "treatment_kind",
            },
        )
    else:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    _validate_common_metadata(data)
    return data


def _plane_basis(
    design: ParametricDesignIR, sketch: ParametricSketch
) -> tuple[tuple[float, ...], ...]:
    if sketch.plane.kind is PlaneKind.ORIGIN:
        if sketch.plane.origin is OriginPlane.XY:
            return ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        if sketch.plane.origin is OriginPlane.XZ:
            return ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0))
        return ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0))
    datum = next((item for item in design.datum_planes if item.id == sketch.plane.datum_id), None)
    if datum is None:
        _raise(ParametricCompileErrorCode.INVALID_INPUT)
    normal = tuple(float(item) for item in datum.normal)
    x_axis = tuple(float(item) for item in datum.x_axis)
    y_axis = (
        normal[1] * x_axis[2] - normal[2] * x_axis[1],
        normal[2] * x_axis[0] - normal[0] * x_axis[2],
        normal[0] * x_axis[1] - normal[1] * x_axis[0],
    )
    return (
        tuple(float(item) for item in datum.origin_mm),
        x_axis,
        y_axis,
        normal,
    )


def _apply_plane(FreeCAD: object, obj: object, basis: tuple[tuple[float, ...], ...]) -> None:
    origin, x_axis, y_axis, normal = basis
    try:
        matrix = FreeCAD.Matrix()  # type: ignore[attr-defined]
        matrix.A11, matrix.A21, matrix.A31 = x_axis
        matrix.A12, matrix.A22, matrix.A32 = y_axis
        matrix.A13, matrix.A23, matrix.A33 = normal
        obj.Placement = FreeCAD.Placement(  # type: ignore[attr-defined]
            FreeCAD.Vector(*origin),  # type: ignore[attr-defined]
            FreeCAD.Rotation(matrix),  # type: ignore[attr-defined]
        )
    except Exception:
        _raise(ParametricCompileErrorCode.CAD_FAILURE)


def _geometry_values(
    FreeCAD: object,
    Part: object,
    geometry: SketchGeometry,
) -> tuple[tuple[object, str], ...]:
    values = geometry.dimensions
    try:
        vector = FreeCAD.Vector  # type: ignore[attr-defined]
        if geometry.kind is GeometryKind.POINT:
            return (
                (
                    Part.Point(vector(values["x_mm"], values["y_mm"], 0)),  # type: ignore[attr-defined]
                    "Part::GeomPoint",
                ),
            )
        if geometry.kind is GeometryKind.LINE:
            return (
                (
                    Part.LineSegment(  # type: ignore[attr-defined]
                        vector(values["x1_mm"], values["y1_mm"], 0),
                        vector(values["x2_mm"], values["y2_mm"], 0),
                    ),
                    "Part::GeomLineSegment",
                ),
            )
        if geometry.kind is GeometryKind.CIRCLE:
            return (
                (
                    Part.Circle(  # type: ignore[attr-defined]
                        vector(values["cx_mm"], values["cy_mm"], 0),
                        vector(0, 0, 1),
                        values["radius_mm"],
                    ),
                    "Part::GeomCircle",
                ),
            )
        if geometry.kind is GeometryKind.ARC:
            center_x = float(values["cx_mm"])
            center_y = float(values["cy_mm"])
            radius = float(values["radius_mm"])
            start = math.radians(float(values["start_angle_deg"]))
            middle = start + math.radians(float(values["sweep_angle_deg"])) / 2.0
            end = start + math.radians(float(values["sweep_angle_deg"]))

            def point(angle: float) -> object:
                return vector(
                    center_x + radius * math.cos(angle),
                    center_y + radius * math.sin(angle),
                    0,
                )

            return (
                (
                    Part.Arc(point(start), point(middle), point(end)),  # type: ignore[attr-defined]
                    "Part::GeomArcOfCircle",
                ),
            )
        if geometry.kind is GeometryKind.SLOT:
            radius = float(values["width_mm"]) / 2.0
            if _slot_axis(geometry) == "horizontal":
                left = min(float(values["x1_mm"]), float(values["x2_mm"]))
                right = max(float(values["x1_mm"]), float(values["x2_mm"]))
                center_y = float(values["y1_mm"])
                return (
                    (
                        Part.LineSegment(  # type: ignore[attr-defined]
                            vector(left, center_y + radius, 0),
                            vector(right, center_y + radius, 0),
                        ),
                        "Part::GeomLineSegment",
                    ),
                    (
                        Part.Arc(  # type: ignore[attr-defined]
                            vector(right, center_y + radius, 0),
                            vector(right + radius, center_y, 0),
                            vector(right, center_y - radius, 0),
                        ),
                        "Part::GeomArcOfCircle",
                    ),
                    (
                        Part.LineSegment(  # type: ignore[attr-defined]
                            vector(right, center_y - radius, 0),
                            vector(left, center_y - radius, 0),
                        ),
                        "Part::GeomLineSegment",
                    ),
                    (
                        Part.Arc(  # type: ignore[attr-defined]
                            vector(left, center_y - radius, 0),
                            vector(left - radius, center_y, 0),
                            vector(left, center_y + radius, 0),
                        ),
                        "Part::GeomArcOfCircle",
                    ),
                )
            if _slot_axis(geometry) == "vertical":
                center_x = float(values["x1_mm"])
                bottom = min(float(values["y1_mm"]), float(values["y2_mm"]))
                top = max(float(values["y1_mm"]), float(values["y2_mm"]))
                return (
                    (
                        Part.LineSegment(  # type: ignore[attr-defined]
                            vector(center_x + radius, bottom, 0),
                            vector(center_x + radius, top, 0),
                        ),
                        "Part::GeomLineSegment",
                    ),
                    (
                        Part.Arc(  # type: ignore[attr-defined]
                            vector(center_x + radius, top, 0),
                            vector(center_x, top + radius, 0),
                            vector(center_x - radius, top, 0),
                        ),
                        "Part::GeomArcOfCircle",
                    ),
                    (
                        Part.LineSegment(  # type: ignore[attr-defined]
                            vector(center_x - radius, top, 0),
                            vector(center_x - radius, bottom, 0),
                        ),
                        "Part::GeomLineSegment",
                    ),
                    (
                        Part.Arc(  # type: ignore[attr-defined]
                            vector(center_x - radius, bottom, 0),
                            vector(center_x, bottom - radius, 0),
                            vector(center_x + radius, bottom, 0),
                        ),
                        "Part::GeomArcOfCircle",
                    ),
                )
    except Exception:
        _raise(ParametricCompileErrorCode.CAD_FAILURE)
    _raise(ParametricCompileErrorCode.UNSUPPORTED)


def _slot_constraint_id(geometry: SketchGeometry) -> str:
    """Return the stable metadata identity for one compiler-derived slot group."""

    digest = hashlib.sha256(
        _SLOT_CONSTRAINT_DOMAIN + b"group\0" + geometry.id.encode("ascii")
    ).hexdigest()[:32]
    return f"ir_constraint_{digest}"


def _slot_constraint_name(geometry: SketchGeometry, index: int) -> str:
    """Name one native constraint without pretending it was declared in the IR."""

    digest = hashlib.sha256(
        _SLOT_CONSTRAINT_DOMAIN
        + b"name\0"
        + geometry.id.encode("ascii")
        + b"\0"
        + str(index).encode("ascii")
    ).hexdigest()[:32]
    return f"C_{digest}"


def _slot_constraint_objects(
    Sketcher: object,
    geometry: SketchGeometry,
    indexes: tuple[int, ...],
) -> tuple[object, ...]:
    """Fully constrain one axis-aligned capsule using editable native constraints."""

    if len(indexes) != _SLOT_NATIVE_GEOMETRY_COUNT:
        _raise(ParametricCompileErrorCode.CAD_FAILURE)
    values = geometry.dimensions
    radius = float(values["width_mm"]) / 2.0
    first, second, third, fourth = indexes
    try:
        make = Sketcher.Constraint  # type: ignore[attr-defined]
        closure = (
            make("Coincident", first, 2, second, 1),
            make("Coincident", second, 2, third, 1),
            make("Coincident", third, 2, fourth, 1),
            make("Coincident", fourth, 2, first, 1),
        )
        if _slot_axis(geometry) == "horizontal":
            left = min(float(values["x1_mm"]), float(values["x2_mm"]))
            right = max(float(values["x1_mm"]), float(values["x2_mm"]))
            center_y = float(values["y1_mm"])
            return closure + (
                make("Horizontal", first),
                make("Horizontal", third),
                make("Radius", second, radius),
                make("DistanceX", -1, 1, second, 3, right),
                make("DistanceY", -1, 1, second, 3, center_y),
                make("DistanceX", -1, 1, fourth, 3, left),
                make("DistanceX", second, 3, second, 1, 0.0),
                make("DistanceX", second, 3, second, 2, 0.0),
                make("DistanceX", fourth, 3, fourth, 1, 0.0),
                make("DistanceX", fourth, 3, fourth, 2, 0.0),
            )
        if _slot_axis(geometry) == "vertical":
            center_x = float(values["x1_mm"])
            bottom = min(float(values["y1_mm"]), float(values["y2_mm"]))
            top = max(float(values["y1_mm"]), float(values["y2_mm"]))
            return closure + (
                make("Vertical", first),
                make("Vertical", third),
                make("Radius", second, radius),
                make("DistanceX", -1, 1, second, 3, center_x),
                make("DistanceY", -1, 1, second, 3, top),
                make("DistanceY", -1, 1, fourth, 3, bottom),
                make("DistanceY", second, 3, second, 1, 0.0),
                make("DistanceY", second, 3, second, 2, 0.0),
                make("DistanceY", fourth, 3, fourth, 1, 0.0),
                make("DistanceY", fourth, 3, fourth, 2, 0.0),
            )
    except Exception:
        _raise(ParametricCompileErrorCode.CAD_FAILURE)
    _raise(ParametricCompileErrorCode.UNSUPPORTED)


@dataclass(frozen=True, slots=True)
class _ResolvedReference:
    index: int
    point: int | None


def _point_code(reference: SketchReference, geometry: SketchGeometry | None) -> int | None:
    if geometry is not None and geometry.kind is GeometryKind.POINT:
        if reference.point in {ReferencePoint.WHOLE, ReferencePoint.CENTER}:
            return 1
        _raise(ParametricCompileErrorCode.INVALID_INPUT)
    if reference.point is ReferencePoint.WHOLE:
        return None
    if reference.target == "@origin":
        return 1
    if geometry is None:
        _raise(ParametricCompileErrorCode.INVALID_INPUT)
    return {
        ReferencePoint.START: 1,
        ReferencePoint.END: 2,
        ReferencePoint.CENTER: 3,
    }.get(reference.point)


def _resolve_reference(
    reference: SketchReference,
    geometry_by_id: Mapping[str, SketchGeometry],
    geometry_indices: Mapping[str, tuple[int, ...]],
) -> _ResolvedReference:
    if reference.target == "@origin":
        return _ResolvedReference(-1, 1)
    if reference.target == "@x_axis":
        return _ResolvedReference(-1, None)
    if reference.target == "@y_axis":
        return _ResolvedReference(-2, None)
    indexes = geometry_indices.get(reference.target)
    geometry = geometry_by_id.get(reference.target)
    if indexes is None or len(indexes) != 1 or geometry is None:
        _raise(ParametricCompileErrorCode.UNSUPPORTED)
    return _ResolvedReference(indexes[0], _point_code(reference, geometry))


def _constraint_value(parameter: DesignParameter) -> float:
    value = float(parameter.value)
    return math.radians(value) if parameter.unit is DesignUnit.DEG else value


def _constraint_object(
    Sketcher: object,
    constraint: SketchConstraint,
    references: tuple[_ResolvedReference, ...],
    parameter: DesignParameter | None,
) -> object:
    try:
        make = Sketcher.Constraint  # type: ignore[attr-defined]
        kind = constraint.kind
        if kind is ConstraintKind.COINCIDENT:
            return make(
                "Coincident",
                references[0].index,
                references[0].point,
                references[1].index,
                references[1].point,
            )
        if kind in {ConstraintKind.HORIZONTAL, ConstraintKind.VERTICAL}:
            return make(kind.value.title(), references[0].index)
        if kind in {
            ConstraintKind.PARALLEL,
            ConstraintKind.PERPENDICULAR,
            ConstraintKind.TANGENT,
            ConstraintKind.EQUAL,
        }:
            return make(kind.value.title(), references[0].index, references[1].index)
        if kind is ConstraintKind.SYMMETRIC:
            return make(
                "Symmetric",
                references[0].index,
                references[0].point,
                references[1].index,
                references[1].point,
                references[2].index,
            )
        if parameter is None:
            _raise(ParametricCompileErrorCode.INVALID_INPUT)
        value = _constraint_value(parameter)
        if kind in {
            ConstraintKind.DISTANCE,
            ConstraintKind.DISTANCE_X,
            ConstraintKind.DISTANCE_Y,
        }:
            freecad_kind = {
                ConstraintKind.DISTANCE: "Distance",
                ConstraintKind.DISTANCE_X: "DistanceX",
                ConstraintKind.DISTANCE_Y: "DistanceY",
            }[kind]
            return make(
                freecad_kind,
                references[0].index,
                references[0].point,
                references[1].index,
                references[1].point,
                value,
            )
        if kind is ConstraintKind.LENGTH:
            return make("Distance", references[0].index, value)
        if kind in {ConstraintKind.RADIUS, ConstraintKind.DIAMETER}:
            return make(kind.value.title(), references[0].index, value)
        if kind is ConstraintKind.ANGLE:
            return make("Angle", references[0].index, references[1].index, value)
    except ParametricCompileError:
        raise
    except Exception:
        _raise(ParametricCompileErrorCode.CAD_FAILURE)
    _raise(ParametricCompileErrorCode.UNSUPPORTED)


def _diagnostic_indexes(value: object, constraint_count: int) -> tuple[int, ...]:
    if type(value) not in {list, tuple}:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    result: list[int] = []
    for item in value:
        if type(item) is not int or item < 1 or item > constraint_count:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        result.append(item)
    return tuple(result)


def _solver_facts(sketch: object) -> SketchSolverFacts:
    try:
        solve_result = sketch.solve()  # type: ignore[attr-defined]
        dof = sketch.DoF  # type: ignore[attr-defined]
        fully_constrained = sketch.FullyConstrained  # type: ignore[attr-defined]
        geometry_count = sketch.GeometryCount  # type: ignore[attr-defined]
        constraint_count = sketch.ConstraintCount  # type: ignore[attr-defined]
        conflicting = sketch.ConflictingConstraints  # type: ignore[attr-defined]
        redundant = sketch.RedundantConstraints  # type: ignore[attr-defined]
        partial = sketch.PartiallyRedundantConstraints  # type: ignore[attr-defined]
        malformed = sketch.MalformedConstraints  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.CAD_FAILURE)
    if type(solve_result) is not int or solve_result not in _SOLVER_RESULTS:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    geometry_count = _integer(geometry_count, maximum=256)
    constraint_count = _integer(constraint_count, maximum=1024)
    dof = _integer(dof, maximum=4096)
    if type(fully_constrained) is not bool:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    conflicting_indexes = _diagnostic_indexes(conflicting, constraint_count)
    redundant_indexes = _diagnostic_indexes(redundant, constraint_count)
    partial_indexes = _diagnostic_indexes(partial, constraint_count)
    malformed_indexes = _diagnostic_indexes(malformed, constraint_count)
    return SketchSolverFacts(
        solve_result=solve_result,
        dof=dof,
        fully_constrained=fully_constrained,
        geometry_count=geometry_count,
        constraint_count=constraint_count,
        conflicting_constraint_count=len(set(conflicting_indexes)),
        redundant_constraint_count=len(set(redundant_indexes) | set(partial_indexes)),
        malformed_constraint_count=len(set(malformed_indexes)),
    )


def _require_solver_success(facts: SketchSolverFacts) -> None:
    if (
        not facts.solver_ok
        or facts.conflicting_constraint_count
        or facts.redundant_constraint_count
        or facts.malformed_constraint_count
    ):
        _raise(ParametricCompileErrorCode.SOLVER_FAILURE)


def _metadata_number(value: object) -> int | float:
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    result = float(value)
    if abs(result) > 1_000_000_000_000:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return value


def _parameter_expression_binding(
    value: object,
) -> tuple[int | float, str, tuple[tuple[str, str, int | float], ...]]:
    data = _exact_mapping(value, {"compiled", "constant", "terms"})
    compiled = _text(data["compiled"])
    constant = _metadata_number(data["constant"])
    terms: list[tuple[str, str, int | float]] = []
    for raw in _sequence(data["terms"], maximum=8):
        term = _exact_mapping(raw, {"coefficient", "parameter_id", "property"})
        parameter_id = _text(term["parameter_id"], _IR_ID)
        if not parameter_id.startswith("ir_parameter_"):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        property_name = _text(term["property"], _PARAMETER_PROPERTY)
        coefficient = _metadata_number(term["coefficient"])
        if coefficient == 0:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        terms.append((parameter_id, property_name, coefficient))
    if not terms or tuple(item[0] for item in terms) != tuple(sorted({item[0] for item in terms})):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return constant, compiled, tuple(terms)


def _parameter_metadata_entry(
    value: object,
) -> tuple[
    str,
    str,
    str,
    tuple[int | float, str, tuple[tuple[str, str, int | float], ...]] | None,
]:
    if type(value) is not dict:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    keys = set(value)
    base_keys = {"id", "property", "unit"}
    if keys != base_keys and keys != base_keys | {"expression"}:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    parameter_id = _text(value["id"], _IR_ID)
    if not parameter_id.startswith("ir_parameter_"):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    property_name = _text(value["property"], _PARAMETER_PROPERTY)
    unit = _text(value["unit"])
    if unit not in {"mm", "deg"}:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    expression = (
        None if "expression" not in value else _parameter_expression_binding(value["expression"])
    )
    return parameter_id, property_name, unit, expression


def _parameter_expression_engine(obj: object) -> dict[str, str]:
    try:
        entries = tuple(obj.ExpressionEngine)  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    result: dict[str, str] = {}
    for raw in entries:
        if type(raw) not in {list, tuple} or len(raw) != 2:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        path = _text(raw[0])
        if path.startswith("."):
            path = path[1:]
        if _PARAMETER_PROPERTY.fullmatch(path) is None or path in result:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        result[path] = _text(raw[1])
    return result


def _validate_parameter_metadata(
    obj: object, data: dict[str, object]
) -> tuple[ParametricEntityFact, ...]:
    if getattr(obj, "TypeId", None) != "Part::Feature":
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    entries = _sequence(data["parameters"], maximum=MAX_DESIGN_PARAMETERS)
    seen_ids: set[str] = set()
    seen_properties: set[str] = set()
    expected_expressions: dict[str, str] = {}
    facts: list[ParametricEntityFact] = []
    for raw in entries:
        parameter_id, property_name, unit, expression = _parameter_metadata_entry(raw)
        if parameter_id in seen_ids:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        if property_name in seen_properties:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        expected_type = {"mm": "App::PropertyLength", "deg": "App::PropertyAngle"}.get(unit)
        assert expected_type is not None
        try:
            if obj.getTypeIdOfProperty(property_name) != expected_type:  # type: ignore[attr-defined]
                raise ValueError
            raw_value = getattr(obj, property_name)
            value = raw_value if type(raw_value) in {int, float} else raw_value.Value
        except Exception:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        if type(value) not in {int, float} or not math.isfinite(float(value)):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        facts.append(
            ParametricEntityFact(
                name=f"parametric.parameter.{_suffix(parameter_id)}",
                value=value,
                unit=unit,
            )
        )
        seen_ids.add(parameter_id)
        seen_properties.add(property_name)
        if expression is not None:
            expected_expressions[property_name] = expression[1]
    if tuple(_parameter_metadata_entry(entry)[0] for entry in entries) != tuple(sorted(seen_ids)):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if _parameter_expression_engine(obj) != expected_expressions:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return tuple(facts)


def _constraint_binding(value: object) -> tuple[str, str, str] | None:
    if value is None:
        return None
    data = _exact_mapping(value, {"parameter_id", "property", "expression"})
    parameter_id = _text(data["parameter_id"], _IR_ID)
    if not parameter_id.startswith("ir_parameter_"):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    property_name = _text(data["property"], _PARAMETER_PROPERTY)
    expression = _text(data["expression"])
    return parameter_id, property_name, expression


def _constraint_expression_engine(obj: object) -> dict[str, str]:
    try:
        entries = tuple(obj.ExpressionEngine)  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    result: dict[str, str] = {}
    for raw in entries:
        if type(raw) not in {list, tuple} or len(raw) != 2:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        path = _text(raw[0])
        if path.startswith("."):
            path = path[1:]
        name = path.removeprefix("Constraints.")
        if path != f"Constraints.{name}" or _CONSTRAINT_NAME.fullmatch(name) is None:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        expression = _text(raw[1])
        if path in result:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        result[path] = expression
    return result


def _feature_binding(value: object) -> tuple[str, str, str, str, str]:
    data = _exact_mapping(
        value,
        {"expression", "name", "parameter_id", "property", "target"},
    )
    name = _text(data["name"])
    if name not in {"angle", "depth", "diameter", "length", "thickness"}:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    parameter_id = _text(data["parameter_id"], _IR_ID)
    if not parameter_id.startswith("ir_parameter_"):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    property_name = _text(data["property"], _PARAMETER_PROPERTY)
    target = _text(data["target"])
    if target not in {"Angle", "Depth", "Diameter", "Length", "Value"}:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    expression = _text(data["expression"])
    return name, parameter_id, property_name, target, expression


def _feature_expression_engine(obj: object, targets: set[str]) -> dict[str, str]:
    try:
        entries = tuple(obj.ExpressionEngine)  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    result: dict[str, str] = {}
    for raw in entries:
        if type(raw) not in {list, tuple} or len(raw) != 2:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        path = _text(raw[0])
        if path.startswith("."):
            path = path[1:]
        if path not in targets or path in result:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        result[path] = _text(raw[1])
    return result


def _quantity_value(obj: object, property_name: str) -> float:
    try:
        raw = getattr(obj, property_name)
        value = raw if type(raw) in {int, float} else raw.Value
        converted = float(value)
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if not math.isfinite(converted):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return converted


def _profile_object(obj: object) -> object:
    try:
        value = obj.Profile  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if type(value) not in {list, tuple} or len(value) != 2:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    profile, subelements = value
    if type(subelements) not in {list, tuple} or tuple(subelements):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return profile


def _profile_edge_count(data: dict[str, object]) -> tuple[int, tuple[str, ...]]:
    count = 0
    circle_ids: list[str] = []
    for raw in _sequence(data["geometries"], maximum=128):
        entry = _exact_mapping(raw, {"id", "indices", "type_ids", "construction"})
        geometry_id = _text(entry["id"], _IR_ID)
        type_ids = _sequence(entry["type_ids"], maximum=8)
        construction = _sequence(entry["construction"], maximum=8)
        if len(type_ids) != len(construction):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        for type_id, is_construction in zip(type_ids, construction, strict=True):
            checked_type = _text(type_id)
            if type(is_construction) is not bool:
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            if is_construction:
                continue
            if checked_type not in {
                "Part::GeomLineSegment",
                "Part::GeomCircle",
                "Part::GeomArcOfCircle",
            }:
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            count += 1
            if checked_type == "Part::GeomCircle":
                circle_ids.append(geometry_id)
    if count < 1:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return count, tuple(sorted(circle_ids))


def _construction_axis_tokens(data: dict[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in _sequence(data["geometries"], maximum=128):
        entry = _exact_mapping(raw, {"id", "indices", "type_ids", "construction"})
        geometry_id = _text(entry["id"], _IR_ID)
        type_ids = _sequence(entry["type_ids"], maximum=8)
        construction = _sequence(entry["construction"], maximum=8)
        if len(type_ids) != len(construction):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        if len(type_ids) == 1 and type_ids[0] == "Part::GeomLineSegment" and construction == [True]:
            result[geometry_id] = f"Axis{len(result)}"
    return result


def _pattern_link_reference(obj: object, property_name: str, expected_name: str) -> None:
    try:
        reference, subelements = getattr(obj, property_name)
        expected = obj.Document.getObject(expected_name)  # type: ignore[attr-defined]
        subelement_tuple = tuple(subelements)
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if reference is not expected or subelement_tuple not in {(), ("",)}:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)


def _validate_pattern_feature_metadata(
    obj: object,
    data: dict[str, object],
    kind: FeatureKind,
) -> tuple[ParametricEntityFact, ...]:
    feature_index = _integer(data["feature_index"], maximum=7)
    feature_id = _text(data["ir_id"], _IR_ID)
    source_feature_id = _text(data["source_feature_id"], _IR_ID)
    base_feature_id = data["base_feature_id"]
    if base_feature_id is not None:
        base_feature_id = _text(base_feature_id, _IR_ID)
    if (
        not feature_id.startswith("ir_feature_")
        or not source_feature_id.startswith("ir_feature_")
        or (base_feature_id is not None and not base_feature_id.startswith("ir_feature_"))
        or data["sketch_id"] is not None
        or data["extent"] is not None
        or data["location_geometry_ids"] != []
        or type(data["reversed"]) is not bool
        or data["symmetric"] is not False
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    try:
        originals = tuple(obj.Originals)  # type: ignore[attr-defined]
        actual_base = obj.BaseFeature  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if len(originals) != 1:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    source_data = _read_metadata(originals[0], required=True)
    if source_data is None or source_data["kind"] != "feature":
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    try:
        source_kind = FeatureKind(_text(source_data["feature_kind"]))
    except ValueError:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if source_data["ir_id"] != source_feature_id or source_kind not in _PROFILE_FEATURE_KINDS:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if base_feature_id is None:
        if actual_base is not None:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    else:
        base_data = _read_metadata(actual_base, required=True)
        if (
            base_data is None
            or base_data["kind"] != "feature"
            or base_data["ir_id"] != base_feature_id
        ):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    direction = data["direction"]
    direction_token = data["direction_token"]
    axis = data["axis"]
    axis_token = data["axis_token"]
    mirror_plane = data["mirror_plane"]
    mirror_plane_token = data["mirror_plane_token"]
    occurrences = data["occurrences"]
    expected_binding_shape: tuple[tuple[str, str], ...]
    expected_occurrences: int | None = None
    reference_fact: ParametricEntityFact
    if kind is FeatureKind.LINEAR_PATTERN:
        try:
            checked_direction = PatternDirection(_text(direction))
        except ValueError:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        expected_token = _PATTERN_DIRECTION_OBJECTS[checked_direction]
        if (
            direction_token != expected_token
            or axis is not None
            or axis_token is not None
            or mirror_plane is not None
            or mirror_plane_token is not None
        ):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        expected_occurrences = _integer(occurrences, maximum=MAX_PATTERN_OCCURRENCES)
        if expected_occurrences < 2:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        try:
            if (
                int(obj.Occurrences) != expected_occurrences
                or bool(obj.Reversed)
                is not data[  # type: ignore[attr-defined]
                    "reversed"
                ]
            ):
                raise ValueError
        except Exception:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        _pattern_link_reference(obj, "Direction", expected_token)
        expected_binding_shape = (("length", "Length"),)
        reference_fact = ParametricEntityFact(
            "parametric.pattern.direction", checked_direction.value
        )
    elif kind is FeatureKind.CIRCULAR_PATTERN:
        checked_axis = _text(axis)
        expected_token = _PATTERN_AXIS_OBJECTS.get(checked_axis)
        if (
            expected_token is None
            or axis_token != expected_token
            or direction is not None
            or direction_token is not None
            or mirror_plane is not None
            or mirror_plane_token is not None
        ):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        expected_occurrences = _integer(occurrences, maximum=MAX_PATTERN_OCCURRENCES)
        if expected_occurrences < 2:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        try:
            if (
                int(obj.Occurrences) != expected_occurrences
                or bool(obj.Reversed)
                is not data[  # type: ignore[attr-defined]
                    "reversed"
                ]
            ):
                raise ValueError
        except Exception:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        _pattern_link_reference(obj, "Axis", expected_token)
        expected_binding_shape = (("angle", "Angle"),)
        reference_fact = ParametricEntityFact("parametric.pattern.axis", checked_axis)
    elif kind is FeatureKind.MIRROR:
        try:
            checked_plane = MirrorPlane(_text(mirror_plane))
        except ValueError:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        expected_token = _MIRROR_PLANE_OBJECTS[checked_plane]
        if (
            mirror_plane_token != expected_token
            or direction is not None
            or direction_token is not None
            or axis is not None
            or axis_token is not None
            or occurrences is not None
            or data["reversed"] is not False
        ):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        _pattern_link_reference(obj, "MirrorPlane", expected_token)
        expected_binding_shape = ()
        reference_fact = ParametricEntityFact(
            "parametric.pattern.mirror_plane", checked_plane.value
        )
    else:  # pragma: no cover - caller closes the enum
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    bindings = tuple(_feature_binding(item) for item in _sequence(data["bindings"], maximum=4))
    if tuple(item[0] for item in bindings) != tuple(sorted(item[0] for item in bindings)):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    binding_shape = tuple((item[0], item[3]) for item in bindings)
    expected_expressions = {item[3]: item[4] for item in bindings}
    if (
        binding_shape != expected_binding_shape
        or len(expected_expressions) != len(bindings)
        or _feature_expression_engine(obj, set(expected_expressions)) != expected_expressions
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    facts = [
        ParametricEntityFact("parametric.feature.extent", "none"),
        ParametricEntityFact("parametric.feature.index", feature_index),
        ParametricEntityFact("parametric.feature.kind", kind.value),
        ParametricEntityFact("parametric.pattern.source_feature_id", source_feature_id),
        reference_fact,
        ParametricEntityFact("parametric.shape_valid", True),
        ParametricEntityFact("parametric.solid_count", 1),
    ]
    if expected_occurrences is not None:
        facts.append(ParametricEntityFact("parametric.pattern.occurrences", expected_occurrences))
    for name, _, _, target, _ in bindings:
        expected_type = "App::PropertyAngle" if target == "Angle" else "App::PropertyLength"
        try:
            if obj.getTypeIdOfProperty(target) != expected_type:  # type: ignore[attr-defined]
                raise ValueError
        except Exception:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        facts.append(
            ParametricEntityFact(
                f"parametric.feature.parameter.{name}",
                _quantity_value(obj, target),
                "deg" if target == "Angle" else "mm",
            )
        )
    _require_feature_shape(obj, None, kind, path=f"/features/{feature_index}")
    return tuple(facts)


def _native_surface_faces(obj: object, *, maximum: int) -> tuple[object, tuple[int, ...]]:
    try:
        raw = obj.Base  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if type(raw) not in {list, tuple} or len(raw) != 2:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    base, names = raw
    if type(names) not in {list, tuple} or not 1 <= len(names) <= maximum:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    indexes: list[int] = []
    for name in names:
        if type(name) is not str or re.fullmatch(r"Face([1-9][0-9]{0,3})", name) is None:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        indexes.append(int(name.removeprefix("Face")))
    if len(set(indexes)) != len(indexes):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return base, tuple(indexes)


def _source_thickness_limit(source: object) -> float:
    try:
        box = source.Shape.BoundBox  # type: ignore[attr-defined]
        lengths = (float(box.XLength), float(box.YLength), float(box.ZLength))
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if not all(math.isfinite(item) and item > 1e-9 for item in lengths):
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    return min(lengths) * 0.25


def _validate_surface_modifier_metadata(
    obj: object,
    data: dict[str, object],
    kind: FeatureKind,
) -> tuple[ParametricEntityFact, ...]:
    feature_index = _integer(data["feature_index"], maximum=7)
    feature_id = _text(data["ir_id"], _IR_ID)
    base_feature_id = _text(data["base_feature_id"], _IR_ID)
    maximum = MAX_THICKNESS_FACES if kind is FeatureKind.THICKNESS else MAX_DRAFT_FACES
    targets = tuple(
        _surface_face_target(item) for item in _sequence(data["face_targets"], maximum=maximum)
    )
    if (
        not feature_id.startswith("ir_feature_")
        or not base_feature_id.startswith("ir_feature_")
        or not targets
        or len({item.source_feature_id for item in targets}) != 1
        or len({(item.role.value, item.geometry_id) for item in targets}) != len(targets)
        or data["sketch_id"] is not None
        or data["extent"] is not None
        or data["axis"] is not None
        or data["axis_token"] is not None
        or data["location_geometry_ids"] != []
        or data["refine"] is not True
        or type(data["reversed"]) is not bool
        or data["symmetric"] is not False
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    records = _parametric_records(obj.Document)  # type: ignore[attr-defined]
    by_ir_id = {_text(item[1]["ir_id"], _IR_ID): item for item in records}
    source_id = targets[0].source_feature_id
    source_record = by_ir_id.get(source_id)
    if source_record is None or source_record[1]["kind"] != "feature":
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    source, source_data = source_record
    try:
        source_kind = FeatureKind(_text(source_data["feature_kind"]))
    except ValueError:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if source_kind is not FeatureKind.PAD:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    base, native_indexes = _native_surface_faces(obj, maximum=maximum)
    base_data = _read_metadata(base, required=True)
    try:
        base_feature = obj.BaseFeature  # type: ignore[attr-defined]
        refine = bool(obj.Refine)  # type: ignore[attr-defined]
        reversed_value = bool(obj.Reversed)  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if (
        base_data is None
        or base_data["kind"] != "feature"
        or base_data["ir_id"] != base_feature_id
        or base_feature is not base
        or refine is not True
        or reversed_value is not data["reversed"]
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    resolved = _resolved_surface_faces(base, data, by_ir_id)
    if native_indexes != tuple(item.index for item in resolved):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    bindings = tuple(_feature_binding(item) for item in _sequence(data["bindings"], maximum=4))
    if tuple(item[0] for item in bindings) != tuple(sorted(item[0] for item in bindings)):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    binding_shape = tuple((item[0], item[3]) for item in bindings)
    expected_expressions = {item[3]: item[4] for item in bindings}
    if len(expected_expressions) != len(bindings):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    reference_fact: ParametricEntityFact
    if kind is FeatureKind.THICKNESS:
        if (
            data["neutral_plane"] is not None
            or data["neutral_plane_token"] is not None
            or data["pull_direction_token"] is not None
            or data["thickness_mode"] != "Skin"
            or data["thickness_join"] != "Arc"
            or data["thickness_intersection"] is not False
            or binding_shape != (("thickness", "Value"),)
        ):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        try:
            if (
                obj.Mode != "Skin"  # type: ignore[attr-defined]
                or obj.Join != "Arc"  # type: ignore[attr-defined]
                or bool(obj.Intersection) is not False  # type: ignore[attr-defined]
            ):
                raise ValueError
        except Exception:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        thickness = _quantity_value(obj, "Value")
        if thickness <= 0 or thickness > _source_thickness_limit(source) + 1e-9:
            _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
        reference_fact = ParametricEntityFact(
            "parametric.surface_modifier.thickness_limit",
            _source_thickness_limit(source),
            "mm",
        )
    else:
        if (
            data["thickness_mode"] is not None
            or data["thickness_join"] is not None
            or data["thickness_intersection"] is not None
            or binding_shape != (("angle", "Angle"),)
            or source_data["symmetric"] is not False
            or any(item.role is not SemanticFaceRole.SWEEP for item in targets)
        ):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        try:
            plane = OriginPlane(_text(data["neutral_plane"]))
        except ValueError:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        plane_token = _ORIGIN_PLANE_OBJECTS[plane]
        direction_token = _ORIGIN_PLANE_PULL_DIRECTIONS[plane]
        if (
            data["neutral_plane_token"] != plane_token
            or data["pull_direction_token"] != direction_token
        ):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        _pattern_link_reference(obj, "NeutralPlane", plane_token)
        _pattern_link_reference(obj, "PullDirection", direction_token)
        angle = _quantity_value(obj, "Angle")
        if not 0 < angle <= 30:
            _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
        reference_fact = ParametricEntityFact(
            "parametric.surface_modifier.neutral_plane",
            plane.value,
        )

    if _feature_expression_engine(obj, set(expected_expressions)) != expected_expressions:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    facts = [
        ParametricEntityFact("parametric.feature.extent", "none"),
        ParametricEntityFact("parametric.feature.index", feature_index),
        ParametricEntityFact("parametric.feature.kind", kind.value),
        ParametricEntityFact("parametric.surface_modifier.face_count", len(targets)),
        ParametricEntityFact("parametric.surface_modifier.source_feature_id", source_id),
        reference_fact,
        ParametricEntityFact("parametric.shape_valid", True),
        ParametricEntityFact("parametric.solid_count", 1),
    ]
    for name, _, _, target, _ in bindings:
        expected_type = "App::PropertyAngle" if target == "Angle" else "App::PropertyLength"
        try:
            if obj.getTypeIdOfProperty(target) != expected_type:  # type: ignore[attr-defined]
                raise ValueError
        except Exception:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        facts.append(
            ParametricEntityFact(
                f"parametric.feature.parameter.{name}",
                _quantity_value(obj, target),
                "deg" if target == "Angle" else "mm",
            )
        )
    _require_feature_shape(obj, base, kind, path=f"/features/{feature_index}")
    return tuple(facts)


def _validate_feature_metadata(
    obj: object,
    data: dict[str, object],
) -> tuple[ParametricEntityFact, ...]:
    try:
        kind = FeatureKind(_text(data["feature_kind"]))
    except ValueError:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if getattr(obj, "TypeId", None) != _FEATURE_TYPE_IDS[kind]:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if kind in _PATTERN_KINDS:
        return _validate_pattern_feature_metadata(obj, data, kind)
    if kind in _SURFACE_MODIFIER_KINDS:
        return _validate_surface_modifier_metadata(obj, data, kind)
    feature_id = _text(data["ir_id"], _IR_ID)
    if not feature_id.startswith("ir_feature_"):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    feature_index = _integer(data["feature_index"], maximum=7)
    sketch_id = _text(data["sketch_id"], _IR_ID)
    if not sketch_id.startswith("ir_sketch_"):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    base_feature_id = data["base_feature_id"]
    if base_feature_id is not None:
        base_feature_id = _text(base_feature_id, _IR_ID)
        if not base_feature_id.startswith("ir_feature_"):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    extent = data["extent"]
    if extent is not None:
        extent = _text(extent)
        if extent not in {FeatureExtent.LENGTH.value, FeatureExtent.THROUGH_ALL.value}:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if kind in {FeatureKind.POCKET, FeatureKind.HOLE} and extent not in {
        FeatureExtent.LENGTH.value,
        FeatureExtent.THROUGH_ALL.value,
    }:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    axis = data["axis"]
    axis_token = data["axis_token"]
    if kind is FeatureKind.REVOLVE:
        axis = _text(axis)
        axis_token = _text(axis_token)
        if axis not in {"@sketch_x", "@sketch_y"} and not axis.startswith("ir_geometry_"):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        if (
            axis_token not in {"H_Axis", "V_Axis"}
            and re.fullmatch(r"Axis(?:0|[1-9][0-9]{0,2})", axis_token) is None
        ):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    elif axis is not None or axis_token is not None:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    reversed_value = data["reversed"]
    symmetric = data["symmetric"]
    if type(reversed_value) is not bool or type(symmetric) is not bool:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    profile = _profile_object(obj)
    profile_data = _read_metadata(profile, required=True)
    if profile_data["kind"] != "sketch" or profile_data["ir_id"] != sketch_id:  # type: ignore[index]
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    expected_edge_count, circle_ids = _profile_edge_count(profile_data)  # type: ignore[arg-type]
    construction_axes = _construction_axis_tokens(profile_data)  # type: ignore[arg-type]
    wire_count = _require_profile_closure(
        profile,
        expected_edge_count=expected_edge_count,
        path=f"/features/{feature_index}",
    )
    _require_supported_feature_profile(
        kind,
        wire_count,
        path=f"/features/{feature_index}",
    )

    locations = tuple(
        _text(item, _IR_ID) for item in _sequence(data["location_geometry_ids"], maximum=128)
    )
    if locations != tuple(sorted(set(locations))):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if kind is FeatureKind.HOLE:
        if locations != circle_ids or expected_edge_count != len(circle_ids):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    elif locations:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    try:
        if bool(obj.Reversed) is not reversed_value:  # type: ignore[attr-defined]
            raise ValueError
        if bool(obj.Refine) is not True:  # type: ignore[attr-defined]
            raise ValueError
        if kind in {FeatureKind.PAD, FeatureKind.POCKET}:
            expected_side_type = "Symmetric" if symmetric else "One side"
            if obj.SideType != expected_side_type:  # type: ignore[attr-defined]
                raise ValueError
            if (
                bool(obj.AlongSketchNormal) is not True  # type: ignore[attr-defined]
                or bool(obj.UseCustomVector) is not False  # type: ignore[attr-defined]
                or _quantity_value(obj, "Offset") != 0
                or _quantity_value(obj, "Offset2") != 0
                or _quantity_value(obj, "TaperAngle") != 0
                or _quantity_value(obj, "TaperAngle2") != 0
            ):
                raise ValueError
        elif bool(obj.Midplane) is not symmetric:  # type: ignore[attr-defined]
            raise ValueError
        if kind is FeatureKind.HOLE and (
            obj.HoleCutType != "None"  # type: ignore[attr-defined]
            or bool(obj.HoleCutCustomValues) is not False  # type: ignore[attr-defined]
            or obj.ThreadType != "None"  # type: ignore[attr-defined]
            or bool(obj.Threaded) is not False  # type: ignore[attr-defined]
            or bool(obj.ModelThread) is not False  # type: ignore[attr-defined]
            or bool(obj.Tapered) is not False  # type: ignore[attr-defined]
            or obj.DrillPoint != "Flat"  # type: ignore[attr-defined]
            or bool(obj.DrillForDepth) is not False  # type: ignore[attr-defined]
            or bool(obj.UseCustomThreadClearance) is not False  # type: ignore[attr-defined]
        ):
            raise ValueError
        actual_base = obj.BaseFeature  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if base_feature_id is None:
        if actual_base is not None:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    else:
        base_data = _read_metadata(actual_base, required=True)
        if base_data["kind"] != "feature" or base_data["ir_id"] != base_feature_id:  # type: ignore[index]
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    expected_mode: str
    if kind is FeatureKind.PAD:
        expected_mode = "Length"
        actual_mode = obj.Type  # type: ignore[attr-defined]
    elif kind is FeatureKind.POCKET:
        expected_mode = "Length" if extent == FeatureExtent.LENGTH.value else "ThroughAll"
        actual_mode = obj.Type  # type: ignore[attr-defined]
    elif kind is FeatureKind.HOLE:
        expected_mode = "Dimension" if extent == FeatureExtent.LENGTH.value else "ThroughAll"
        actual_mode = obj.DepthType  # type: ignore[attr-defined]
    else:
        expected_mode = "Angle"
        actual_mode = obj.Type  # type: ignore[attr-defined]
    if actual_mode != expected_mode:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    if kind is FeatureKind.REVOLVE:
        expected_axis_token = {
            "@sketch_x": "H_Axis",
            "@sketch_y": "V_Axis",
        }.get(axis, construction_axes.get(axis))
        if axis_token != expected_axis_token:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        try:
            if int(profile.AxisCount) != len(construction_axes):  # type: ignore[attr-defined]
                raise ValueError
        except Exception:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        try:
            reference, subelements = obj.ReferenceAxis  # type: ignore[attr-defined]
        except Exception:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        saved_axis_token = "Axis" if axis_token == "Axis0" else axis_token
        if reference is not profile or tuple(subelements) != (saved_axis_token,):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    bindings = tuple(_feature_binding(item) for item in _sequence(data["bindings"], maximum=4))
    if tuple(item[0] for item in bindings) != tuple(sorted(item[0] for item in bindings)):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    expected_expressions = {item[3]: item[4] for item in bindings}
    if len(expected_expressions) != len(bindings):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if kind is FeatureKind.PAD:
        expected_extent = FeatureExtent.LENGTH.value
        expected_binding_shape = (("length", "Length"),)
    elif kind is FeatureKind.POCKET:
        expected_extent = extent
        expected_binding_shape = (
            (("length", "Length"),) if extent == FeatureExtent.LENGTH.value else ()
        )
    elif kind is FeatureKind.REVOLVE:
        expected_extent = None
        expected_binding_shape = (("angle", "Angle"),)
    else:
        expected_extent = extent
        expected_binding_shape = (
            (("depth", "Depth"), ("diameter", "Diameter"))
            if extent == FeatureExtent.LENGTH.value
            else (("diameter", "Diameter"),)
        )
    binding_shape = tuple((item[0], item[3]) for item in bindings)
    if (
        extent != expected_extent
        or binding_shape != expected_binding_shape
        or (kind is FeatureKind.HOLE and symmetric)
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if _feature_expression_engine(obj, set(expected_expressions)) != expected_expressions:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    if kind is FeatureKind.HOLE and len(locations) > 1:
        if actual_base is None:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        try:
            FreeCAD, _Part, _Sketcher = _load_freecad_modules()
        except ParametricCompileError:
            raise
        except Exception:
            _raise(ParametricCompileErrorCode.CAD_FAILURE)
        _require_hole_location_cuts(
            FreeCAD,
            profile,
            actual_base,
            obj,
            location_geometry_ids=locations,
            depth_mm=(
                _quantity_value(obj, "Depth") if extent == FeatureExtent.LENGTH.value else None
            ),
            path=f"/features/{feature_index}",
        )

    facts = [
        ParametricEntityFact("parametric.feature.extent", "none" if extent is None else extent),
        ParametricEntityFact("parametric.feature.index", feature_index),
        ParametricEntityFact("parametric.feature.kind", kind.value),
        ParametricEntityFact("parametric.profile.wire_count", wire_count),
        ParametricEntityFact("parametric.shape_valid", True),
        ParametricEntityFact("parametric.solid_count", 1),
    ]
    if kind is FeatureKind.HOLE:
        facts.append(ParametricEntityFact("parametric.hole.location_count", len(locations)))
    for name, _, _, target, _ in bindings:
        expected_type = "App::PropertyAngle" if target == "Angle" else "App::PropertyLength"
        try:
            if obj.getTypeIdOfProperty(target) != expected_type:  # type: ignore[attr-defined]
                raise ValueError
        except Exception:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        facts.append(
            ParametricEntityFact(
                f"parametric.feature.parameter.{name}",
                _quantity_value(obj, target),
                "deg" if target == "Angle" else "mm",
            )
        )
    _require_feature_shape(obj, None, kind, path=f"/features/{feature_index}")
    return tuple(facts)


def _edge_treatment_target(
    value: object,
) -> tuple[SemanticEdgeReference, str, str, str, str, bool | None]:
    entry = _exact_mapping(
        value,
        {
            "edge",
            "end_parameter_id",
            "end_property",
            "forward",
            "start_parameter_id",
            "start_property",
        },
    )
    edge = _exact_mapping(
        entry["edge"],
        {"geometry_id", "point", "role", "source_feature_id"},
    )
    try:
        reference = SemanticEdgeReference(
            source_feature_id=_text(edge["source_feature_id"], _IR_ID),
            geometry_id=_text(edge["geometry_id"], _IR_ID),
            role=_text(edge["role"]),
            point=_text(edge["point"]),
        )
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    start_parameter_id = _text(entry["start_parameter_id"], _IR_ID)
    end_parameter_id = _text(entry["end_parameter_id"], _IR_ID)
    start_property = _text(entry["start_property"], _PARAMETER_PROPERTY)
    end_property = _text(entry["end_property"], _PARAMETER_PROPERTY)
    forward = entry["forward"]
    if forward is not None and type(forward) is not bool:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if not start_parameter_id.startswith("ir_parameter_") or not end_parameter_id.startswith(
        "ir_parameter_"
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return (
        reference,
        start_parameter_id,
        start_property,
        end_parameter_id,
        end_property,
        forward,
    )


def _native_treatment_edges(obj: object) -> tuple[tuple[int, float, float], ...]:
    try:
        raw_edges = tuple(obj.Edges)  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    result: list[tuple[int, float, float]] = []
    for raw in raw_edges:
        if type(raw) not in {list, tuple} or len(raw) != 3 or type(raw[0]) is not int:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        try:
            first = float(raw[1])
            second = float(raw[2])
        except Exception:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        if raw[0] < 1 or not all(math.isfinite(item) and item > 0 for item in (first, second)):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        result.append((raw[0], first, second))
    if not 1 <= len(result) <= 16 or len({item[0] for item in result}) != len(result):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return tuple(result)


def _validate_edge_treatment_metadata(
    obj: object,
    data: dict[str, object],
) -> tuple[ParametricEntityFact, ...]:
    try:
        kind = EdgeTreatmentKind(_text(data["treatment_kind"]))
    except ValueError:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if getattr(obj, "TypeId", None) != _EDGE_TREATMENT_TYPE_IDS[kind]:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    treatment_id = _text(data["ir_id"], _IR_ID)
    base_feature_id = _text(data["base_feature_id"], _IR_ID)
    if not treatment_id.startswith("ir_feature_") or not base_feature_id.startswith("ir_feature_"):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    treatment_index = _integer(data["treatment_index"], maximum=7)
    targets = tuple(_edge_treatment_target(item) for item in _sequence(data["targets"], maximum=16))
    if not targets:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if kind is EdgeTreatmentKind.CHAMFER and any(
        start_id != end_id for _, start_id, _, end_id, _, _ in targets
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    native_edges = _native_treatment_edges(obj)
    if len(native_edges) != len(targets):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    try:
        base = obj.Base  # type: ignore[attr-defined]
        edge_link_base, edge_link_names = obj.EdgeLinks  # type: ignore[attr-defined]
        shape = obj.Shape  # type: ignore[attr-defined]
        solids = tuple(shape.Solids)
        volume = float(shape.Volume)
        base_volume = float(base.Shape.Volume)
        state = tuple(obj.State)  # type: ignore[attr-defined]
        status = obj.getStatusString()  # type: ignore[attr-defined]
        valid = not shape.isNull() and shape.isValid()
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    expected_names = tuple(f"Edge{item[0]}" for item in native_edges)
    tolerance = max(1.0, abs(volume), abs(base_volume)) * 1e-9
    if (
        edge_link_base is not base
        or tuple(edge_link_names) != expected_names
        or not valid
        or len(solids) != 1
        or not math.isfinite(volume)
        or volume <= 0
        or not math.isfinite(base_volume)
        or base_volume <= 0
        or math.isclose(volume, base_volume, rel_tol=0.0, abs_tol=tolerance)
        or state != ("Up-to-date",)
        or status != "Valid"
    ):
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    facts = [
        ParametricEntityFact("parametric.edge_treatment.edge_count", len(targets)),
        ParametricEntityFact("parametric.edge_treatment.index", treatment_index),
        ParametricEntityFact("parametric.edge_treatment.kind", kind.value),
        ParametricEntityFact(
            "parametric.edge_treatment.variable_edge_count",
            sum(start_id != end_id for _, start_id, _, end_id, _, _ in targets),
        ),
        ParametricEntityFact("parametric.shape_valid", True),
        ParametricEntityFact("parametric.solid_count", 1),
    ]
    for index, (
        _reference,
        _start_id,
        start_property,
        _end_id,
        end_property,
        _forward,
    ) in enumerate(targets):
        facts.extend(
            (
                ParametricEntityFact(
                    f"parametric.edge_treatment.target.{index}.start",
                    _quantity_value(_edge_treatment_carrier(obj), start_property),
                    "mm",
                ),
                ParametricEntityFact(
                    f"parametric.edge_treatment.target.{index}.end",
                    _quantity_value(_edge_treatment_carrier(obj), end_property),
                    "mm",
                ),
            )
        )
    return tuple(facts)


def _edge_treatment_carrier(obj: object) -> object:
    try:
        document = obj.Document  # type: ignore[attr-defined]
        records = _parametric_records(document)
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    carriers = tuple(item for item, data in records if data["kind"] == "parameters")
    if len(carriers) != 1:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return carriers[0]


def _resolved_treatment_edges(
    base: object,
    treatment_data: dict[str, object],
    by_ir_id: Mapping[str, tuple[object, dict[str, object]]],
    carrier: object,
    *,
    validate_orientation: bool = True,
) -> tuple[_ResolvedTreatmentEdge, ...]:
    result: list[_ResolvedTreatmentEdge] = []
    for raw in _sequence(treatment_data["targets"], maximum=16):
        (
            reference,
            start_id,
            start_property,
            end_id,
            end_property,
            stored_forward,
        ) = _edge_treatment_target(raw)
        source_record = by_ir_id.get(reference.source_feature_id)
        if source_record is None or source_record[1]["kind"] != "feature":
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        source_feature, source_data = source_record
        sketch_id = _text(source_data["sketch_id"], _IR_ID)
        sketch_record = by_ir_id.get(sketch_id)
        if sketch_record is None or sketch_record[1]["kind"] != "sketch":
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        sketch, sketch_data = sketch_record
        start = _quantity_value(carrier, start_property)
        end = _quantity_value(carrier, end_property)
        if start <= 0 or end <= 0:
            _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
        require_orientation = start_id != end_id
        resolved = _resolve_semantic_edge(
            base,
            source_feature=source_feature,
            sketch=sketch,
            feature_data=source_data,
            sketch_data=sketch_data,
            reference=reference,
            require_orientation=require_orientation,
        )
        if validate_orientation:
            if require_orientation:
                if stored_forward is None or stored_forward is not resolved.forward:
                    _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
            elif stored_forward is not None:
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        first, second = (start, end) if resolved.forward else (end, start)
        result.append(
            _ResolvedTreatmentEdge(
                index=resolved.index,
                start=first,
                end=second,
                forward=resolved.forward if require_orientation else None,
            )
        )
    if len({item.index for item in result}) != len(result):
        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
    return tuple(result)


def _validate_treatment_resolution(
    obj: object,
    data: dict[str, object],
    *,
    base: object,
    by_ir_id: Mapping[str, tuple[object, dict[str, object]]],
    carrier: object,
) -> None:
    try:
        if obj.Base is not base:  # type: ignore[attr-defined]
            raise ValueError
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    expected = _resolved_treatment_edges(base, data, by_ir_id, carrier)
    actual = _native_treatment_edges(obj)
    if len(expected) != len(actual) or any(
        expected.index != actual_index
        or not math.isclose(expected.start, actual_start, rel_tol=0.0, abs_tol=1e-8)
        or not math.isclose(expected.end, actual_end, rel_tol=0.0, abs_tol=1e-8)
        for expected, (
            actual_index,
            actual_start,
            actual_end,
        ) in zip(expected, actual, strict=True)
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)


def _validate_sketch_metadata(obj: object, data: dict[str, object]) -> SketchSolverFacts:
    if getattr(obj, "TypeId", None) != "Sketcher::SketchObject":
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    geometries = _sequence(data["geometries"], maximum=128)
    constraints = _sequence(data["constraints"], maximum=256)
    geometry_indexes: set[int] = set()
    geometry_ids: list[str] = []
    for raw in geometries:
        entry = _exact_mapping(raw, {"id", "indices", "type_ids", "construction"})
        geometry_id = _text(entry["id"], _IR_ID)
        if not geometry_id.startswith("ir_geometry_") or geometry_id in geometry_ids:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        indices = _sequence(entry["indices"], maximum=8)
        type_ids = _sequence(entry["type_ids"], maximum=8)
        construction = _sequence(entry["construction"], maximum=8)
        if not indices or not (len(indices) == len(type_ids) == len(construction)):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        for index, type_id, is_construction in zip(indices, type_ids, construction, strict=True):
            checked_index = _integer(index, maximum=255)
            if checked_index in geometry_indexes or type(is_construction) is not bool:
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            expected_type = _text(type_id)
            try:
                actual = obj.Geometry[checked_index]  # type: ignore[attr-defined]
                if (
                    actual.TypeId != expected_type
                    or obj.getConstruction(checked_index) is not is_construction
                ):  # type: ignore[attr-defined]
                    raise ValueError
            except Exception:
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            geometry_indexes.add(checked_index)
        geometry_ids.append(geometry_id)
    constraint_indexes: set[int] = set()
    constraint_ids: list[str] = []
    expected_expressions: dict[str, str] = {}
    for raw in constraints:
        entry = _exact_mapping(raw, {"id", "indices", "types", "names", "bindings"})
        constraint_id = _text(entry["id"], _IR_ID)
        if not constraint_id.startswith("ir_constraint_") or constraint_id in constraint_ids:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        indices = _sequence(entry["indices"], maximum=_MAX_COMPILED_CONSTRAINTS_PER_ENTRY)
        types = _sequence(entry["types"], maximum=_MAX_COMPILED_CONSTRAINTS_PER_ENTRY)
        names = _sequence(entry["names"], maximum=_MAX_COMPILED_CONSTRAINTS_PER_ENTRY)
        bindings = _sequence(entry["bindings"], maximum=_MAX_COMPILED_CONSTRAINTS_PER_ENTRY)
        if not indices or not (len(indices) == len(types) == len(names) == len(bindings)):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        for index, expected_type, expected_name, raw_binding in zip(
            indices,
            types,
            names,
            bindings,
            strict=True,
        ):
            checked_index = _integer(index, maximum=1023)
            if checked_index in constraint_indexes:
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            type_name = _text(expected_type)
            name = _text(expected_name, _CONSTRAINT_NAME)
            try:
                actual = obj.Constraints[checked_index]  # type: ignore[attr-defined]
                if actual.Type != type_name or actual.Name != name:
                    raise ValueError
            except Exception:
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            constraint_indexes.add(checked_index)
            binding = _constraint_binding(raw_binding)
            if binding is not None:
                path = f"Constraints.{name}"
                if path in expected_expressions:
                    _raise(ParametricCompileErrorCode.METADATA_FAILURE)
                expected_expressions[path] = binding[2]
        constraint_ids.append(constraint_id)
    if _constraint_expression_engine(obj) != expected_expressions:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    facts = _solver_facts(obj)
    if geometry_indexes != set(range(facts.geometry_count)):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if constraint_indexes != set(range(facts.constraint_count)):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if geometry_ids != sorted(geometry_ids) or constraint_ids != sorted(constraint_ids):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return facts


def _parametric_records(
    document: object,
) -> tuple[tuple[object, dict[str, object]], ...]:
    try:
        objects = tuple(document.Objects)  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/session")
    records: list[tuple[object, dict[str, object]]] = []
    for obj in objects:
        data = _read_metadata(obj)
        if data is not None:
            records.append((obj, data))
    return tuple(records)


def _validate_parametric_graph(
    records: tuple[tuple[object, dict[str, object]], ...],
) -> None:
    if not records:
        return
    bodies = tuple(item for item in records if item[1]["kind"] == "body")
    carriers = tuple(item for item in records if item[1]["kind"] == "parameters")
    sketches = tuple(item for item in records if item[1]["kind"] == "sketch")
    features = tuple(item for item in records if item[1]["kind"] == "feature")
    edge_treatments = tuple(item for item in records if item[1]["kind"] == "edge_treatment")
    if (
        len(bodies) != 1
        or len(carriers) != 1
        or not 1 <= len(sketches) <= 8
        or not 1 <= len(features) <= 8
        or len(edge_treatments) > 8
        or len(features) + len(edge_treatments) > 8
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    design_ids = {_text(data["design_id"], _IR_ID) for _, data in records}
    design_digests = {_text(data["design_digest"], _HEX_64) for _, data in records}
    ir_ids = tuple(_text(data["ir_id"], _IR_ID) for _, data in records)
    if len(design_ids) != 1 or len(design_digests) != 1 or len(set(ir_ids)) != len(ir_ids):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    body, body_data = bodies[0]
    carrier, carrier_data = carriers[0]
    _validate_parameter_metadata(carrier, carrier_data)
    design_id = next(iter(design_ids))
    body_id = _text(body_data["ir_id"], _IR_ID)
    carrier_id = _text(carrier_data["ir_id"], _IR_ID)
    if not body_id.startswith("ir_body_") or carrier_id != design_id:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    expected_sketch_ids = tuple(
        _text(item, _IR_ID) for item in _sequence(body_data["sketch_ids"], maximum=8)
    )
    actual_sketch_ids = tuple(sorted(_text(data["ir_id"], _IR_ID) for _, data in sketches))
    if expected_sketch_ids != actual_sketch_ids or any(
        not item.startswith("ir_sketch_") for item in actual_sketch_ids
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    expected_feature_ids = tuple(
        _text(item, _IR_ID) for item in _sequence(body_data["feature_ids"], maximum=8)
    )
    indexed_features = tuple(
        sorted(
            features,
            key=lambda item: _integer(item[1]["feature_index"], maximum=7),
        )
    )
    actual_feature_ids = tuple(_text(data["ir_id"], _IR_ID) for _, data in indexed_features)
    actual_feature_indexes = tuple(
        _integer(data["feature_index"], maximum=7) for _, data in indexed_features
    )
    if (
        expected_feature_ids != actual_feature_ids
        or actual_feature_indexes != tuple(range(len(indexed_features)))
        or any(not item.startswith("ir_feature_") for item in actual_feature_ids)
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    pattern_feature_count = 0
    pattern_instance_count = 0
    surface_kinds: list[FeatureKind] = []
    all_feature_kinds: list[FeatureKind] = []
    for _, feature_data in indexed_features:
        try:
            feature_kind = FeatureKind(_text(feature_data["feature_kind"]))
        except ValueError:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        all_feature_kinds.append(feature_kind)
        if feature_kind in _SURFACE_MODIFIER_KINDS:
            surface_kinds.append(feature_kind)
        if feature_kind not in _PATTERN_KINDS:
            continue
        pattern_feature_count += 1
        if feature_kind is FeatureKind.MIRROR:
            pattern_instance_count += 2
        else:
            occurrences = _integer(
                feature_data["occurrences"],
                maximum=MAX_PATTERN_OCCURRENCES,
            )
            if occurrences < 2:
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            pattern_instance_count += occurrences
    if (
        pattern_feature_count > MAX_PATTERN_FEATURES
        or pattern_instance_count > MAX_PATTERN_INSTANCES
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if (
        len(surface_kinds) > MAX_SURFACE_MODIFIERS
        or surface_kinds.count(FeatureKind.THICKNESS) > 1
        or surface_kinds.count(FeatureKind.DRAFT) > 1
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if surface_kinds and tuple(all_feature_kinds) not in {
        (FeatureKind.PAD, FeatureKind.DRAFT),
        (FeatureKind.PAD, FeatureKind.THICKNESS),
        (FeatureKind.PAD, FeatureKind.DRAFT, FeatureKind.THICKNESS),
    }:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    expected_treatment_ids = tuple(
        _text(item, _IR_ID)
        for item in _sequence(body_data.get("edge_treatment_ids", []), maximum=8)
    )
    indexed_treatments = tuple(
        sorted(
            edge_treatments,
            key=lambda item: _integer(item[1]["treatment_index"], maximum=7),
        )
    )
    actual_treatment_ids = tuple(_text(data["ir_id"], _IR_ID) for _, data in indexed_treatments)
    actual_treatment_indexes = tuple(
        _integer(data["treatment_index"], maximum=7) for _, data in indexed_treatments
    )
    if (
        expected_treatment_ids != actual_treatment_ids
        or actual_treatment_indexes != tuple(range(len(indexed_treatments)))
        or any(not item.startswith("ir_feature_") for item in actual_treatment_ids)
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    try:
        body_group = tuple(body.Group)  # type: ignore[attr-defined]
        managed_sketch_names = {obj.Name for obj, _ in sketches}  # type: ignore[attr-defined]
        managed_feature_names = {obj.Name for obj, _ in features}  # type: ignore[attr-defined]
        carrier_name = carrier.Name  # type: ignore[attr-defined]
        grouped_names = {obj.Name for obj in body_group}  # type: ignore[attr-defined]
        tip = body.Tip  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if (
        grouped_names != managed_sketch_names | managed_feature_names
        or carrier_name in grouped_names
        or tip is not indexed_features[-1][0]
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    sketch_objects = {obj for obj, _ in sketches}
    indexed_feature_by_id = {
        _text(data["ir_id"], _IR_ID): (obj, data) for obj, data in indexed_features
    }
    previous_feature: object | None = None
    consumed_sketch_ids: set[str] = set()
    for feature_index, (feature_obj, feature_data) in enumerate(indexed_features):
        _validate_feature_metadata(feature_obj, feature_data)
        try:
            feature_kind = FeatureKind(_text(feature_data["feature_kind"]))
        except ValueError:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        feature_id = _text(feature_data["ir_id"], _IR_ID)
        expected_base_id = None if feature_index == 0 else actual_feature_ids[feature_index - 1]
        if (
            feature_data["base_feature_id"] != expected_base_id
            or (feature_index == 0 and feature_kind not in {FeatureKind.PAD, FeatureKind.REVOLVE})
            or feature_id != actual_feature_ids[feature_index]
        ):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        additive: bool | None = None
        if feature_kind in _PATTERN_KINDS:
            source_feature_id = _text(feature_data["source_feature_id"], _IR_ID)
            source_entry = indexed_feature_by_id.get(source_feature_id)
            if source_entry is None:
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            try:
                source_kind = FeatureKind(_text(source_entry[1]["feature_kind"]))
                source_index = actual_feature_ids.index(source_feature_id)
            except (ValueError, TypeError):
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            if source_index >= feature_index or source_kind not in _PROFILE_FEATURE_KINDS:
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            additive = source_kind in {FeatureKind.PAD, FeatureKind.REVOLVE}
        elif feature_kind in _SURFACE_MODIFIER_KINDS:
            maximum = (
                MAX_THICKNESS_FACES if feature_kind is FeatureKind.THICKNESS else MAX_DRAFT_FACES
            )
            targets = tuple(
                _surface_face_target(item)
                for item in _sequence(feature_data["face_targets"], maximum=maximum)
            )
            if not targets or len({item.source_feature_id for item in targets}) != 1:
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            source_feature_id = targets[0].source_feature_id
            source_entry = indexed_feature_by_id.get(source_feature_id)
            if source_entry is None:
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            try:
                source_kind = FeatureKind(_text(source_entry[1]["feature_kind"]))
                source_index = actual_feature_ids.index(source_feature_id)
            except (ValueError, TypeError):
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            if source_index >= feature_index or source_kind is not FeatureKind.PAD:
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        else:
            sketch_id = _text(feature_data["sketch_id"], _IR_ID)
            if (
                sketch_id in consumed_sketch_ids
                or _profile_object(feature_obj) not in sketch_objects
            ):
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            consumed_sketch_ids.add(sketch_id)
        try:
            if feature_obj.BaseFeature is not previous_feature:  # type: ignore[attr-defined]
                raise ValueError
        except Exception:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        _require_feature_shape(
            feature_obj,
            previous_feature,
            feature_kind,
            additive=additive,
            path=f"/features/{feature_index}",
        )
        previous_feature = feature_obj

    by_ir_id = {_text(data["ir_id"], _IR_ID): (obj, data) for obj, data in records}
    previous_treatment: object = body
    for treatment_index, (treatment_obj, treatment_data) in enumerate(indexed_treatments):
        _validate_edge_treatment_metadata(treatment_obj, treatment_data)
        expected_base_id = (
            actual_feature_ids[-1]
            if treatment_index == 0
            else actual_treatment_ids[treatment_index - 1]
        )
        if treatment_data["base_feature_id"] != expected_base_id:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        _validate_treatment_resolution(
            treatment_obj,
            treatment_data,
            base=previous_treatment,
            by_ir_id=by_ir_id,
            carrier=carrier,
        )
        previous_treatment = treatment_obj

    parameter_pairs: set[tuple[str, str]] = set()
    parameter_units: dict[tuple[str, str], str] = {}
    parameter_properties: dict[str, str] = {}
    parameter_expressions: dict[
        str, tuple[int | float, str, tuple[tuple[str, str, int | float], ...]]
    ] = {}
    for raw in _sequence(carrier_data["parameters"], maximum=MAX_DESIGN_PARAMETERS):
        parameter_id, property_name, unit, expression = _parameter_metadata_entry(raw)
        pair = (parameter_id, property_name)
        if pair in parameter_pairs or parameter_id in parameter_properties:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        parameter_pairs.add(pair)
        parameter_units[pair] = unit
        parameter_properties[parameter_id] = property_name
        if expression is not None:
            parameter_expressions[parameter_id] = expression
    for parameter_id, (constant, compiled, terms) in parameter_expressions.items():
        target_pair = (parameter_id, parameter_properties[parameter_id])
        target_unit = parameter_units[target_pair]
        expected_parts: list[str] = []
        for source_id, source_property, coefficient in terms:
            registered_property = parameter_properties.get(source_id)
            source_pair = (source_id, source_property)
            if (
                registered_property != source_property
                or source_pair not in parameter_pairs
                or parameter_units[source_pair] != target_unit
            ):
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            expected_parts.append(f"{_canonical(coefficient)} * {source_property}")
        if constant != 0:
            expected_parts.append(f"{_canonical(constant)} {target_unit}")
        if compiled != " + ".join(expected_parts):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    visited_parameters: set[str] = set()
    visiting_parameters: set[str] = set()

    def visit_parameter(parameter_id: str) -> None:
        if parameter_id in visited_parameters:
            return
        if parameter_id in visiting_parameters:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        visiting_parameters.add(parameter_id)
        expression = parameter_expressions.get(parameter_id)
        if expression is not None:
            for source_id, _, _ in expression[2]:
                visit_parameter(source_id)
        visiting_parameters.remove(parameter_id)
        visited_parameters.add(parameter_id)

    for parameter_id in parameter_properties:
        visit_parameter(parameter_id)
    for _, sketch_data in sketches:
        for raw in _sequence(sketch_data["constraints"], maximum=256):
            entry = _exact_mapping(raw, {"id", "indices", "types", "names", "bindings"})
            for raw_binding in _sequence(
                entry["bindings"], maximum=_MAX_COMPILED_CONSTRAINTS_PER_ENTRY
            ):
                binding = _constraint_binding(raw_binding)
                if binding is None:
                    continue
                parameter_id, property_name, expression = binding
                if (
                    parameter_id,
                    property_name,
                ) not in parameter_pairs or expression != f"{carrier_name}.{property_name}":
                    _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    for _, feature_data in features:
        for raw_binding in _sequence(feature_data["bindings"], maximum=4):
            _, parameter_id, property_name, target, expression = _feature_binding(raw_binding)
            pair = (parameter_id, property_name)
            if (
                pair not in parameter_pairs
                or expression != f"{carrier_name}.{property_name}"
                or parameter_units[pair] != ("deg" if target == "Angle" else "mm")
            ):
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    for _, treatment_data in edge_treatments:
        try:
            treatment_kind = EdgeTreatmentKind(_text(treatment_data["treatment_kind"]))
        except ValueError:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        for raw_target in _sequence(treatment_data["targets"], maximum=16):
            (
                _reference,
                start_parameter_id,
                start_property,
                end_parameter_id,
                end_property,
                _forward,
            ) = _edge_treatment_target(raw_target)
            for parameter_id, property_name in (
                (start_parameter_id, start_property),
                (end_parameter_id, end_property),
            ):
                pair = (parameter_id, property_name)
                if pair not in parameter_pairs or parameter_units[pair] != "mm":
                    _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            if treatment_kind is EdgeTreatmentKind.CHAMFER and (
                start_parameter_id != end_parameter_id or start_property != end_property
            ):
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)


def parametric_entity_facts(obj: object) -> tuple[ParametricEntityFact, ...]:
    """Read strict primitive facts from one compiler-owned FreeCAD object."""

    data = _read_metadata(obj)
    if data is None:
        return ()
    kind = data["kind"]
    raw = getattr(obj, PARAMETRIC_METADATA_PROPERTY)
    facts = [
        ParametricEntityFact("parametric.design_ir_digest", data["design_digest"]),
        ParametricEntityFact(
            "parametric.mapping_digest",
            hashlib.sha256(_METADATA_DOMAIN + raw.encode("utf-8")).hexdigest(),
        ),
    ]
    if kind == "body":
        if getattr(obj, "TypeId", None) != "PartDesign::Body":
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        sketch_ids = _sequence(data["sketch_ids"], maximum=8)
        feature_ids = _sequence(data["feature_ids"], maximum=8)
        edge_treatment_ids = _sequence(data.get("edge_treatment_ids", []), maximum=8)
        if (
            any(not _text(item, _IR_ID).startswith("ir_sketch_") for item in sketch_ids)
            or sketch_ids != sorted(set(sketch_ids))
            or any(not _text(item, _IR_ID).startswith("ir_feature_") for item in feature_ids)
            or len(feature_ids) != len(set(feature_ids))
            or any(not _text(item, _IR_ID).startswith("ir_feature_") for item in edge_treatment_ids)
            or len(edge_treatment_ids) != len(set(edge_treatment_ids))
            or set(feature_ids) & set(edge_treatment_ids)
        ):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        facts.extend(
            (
                ParametricEntityFact("parametric.feature_count", len(feature_ids)),
                ParametricEntityFact("parametric.sketch_count", len(sketch_ids)),
            )
        )
        if "edge_treatment_ids" in data:
            facts.append(
                ParametricEntityFact("parametric.edge_treatment_count", len(edge_treatment_ids))
            )
    elif kind == "parameters":
        facts.extend(_validate_parameter_metadata(obj, data))
    elif kind == "sketch":
        solver = _validate_sketch_metadata(obj, data)
        facts.extend(
            (
                ParametricEntityFact("parametric.constraint_count", solver.constraint_count),
                ParametricEntityFact(
                    "parametric.conflicting_constraint_count",
                    solver.conflicting_constraint_count,
                ),
                ParametricEntityFact("parametric.dof", solver.dof),
                ParametricEntityFact("parametric.fully_constrained", solver.fully_constrained),
                ParametricEntityFact("parametric.geometry_count", solver.geometry_count),
                ParametricEntityFact(
                    "parametric.malformed_constraint_count",
                    solver.malformed_constraint_count,
                ),
                ParametricEntityFact(
                    "parametric.redundant_constraint_count",
                    solver.redundant_constraint_count,
                ),
                ParametricEntityFact("parametric.solver_ok", solver.solver_ok),
            )
        )
    elif kind == "feature":
        facts.extend(_validate_feature_metadata(obj, data))
    elif kind == "edge_treatment":
        facts.extend(_validate_edge_treatment_metadata(obj, data))
    else:  # pragma: no cover - _read_metadata closes this enum
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return tuple(sorted(facts, key=lambda item: item.name))


def stabilize_parametric_session(session: object) -> None:
    """Validate compiler-owned sketches and feature solids before observation."""

    try:
        document = session.doc  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/session")
    records = _parametric_records(document)
    if not records:
        return
    for obj, data in records:
        if data["kind"] == "sketch":
            _require_solver_success(_validate_sketch_metadata(obj, data))
    try:
        document.recompute()
    except Exception:
        _raise(ParametricCompileErrorCode.CAD_FAILURE)
    _validate_parametric_graph(records)
    for obj, data in records:
        if data["kind"] != "sketch":
            parametric_entity_facts(obj)


def _source_parameter_mapping(
    records: tuple[tuple[object, dict[str, object]], ...],
    design: ParametricDesignIR,
) -> tuple[object, tuple[dict[str, object], ...]]:
    """Authenticate the persisted compiler graph against one immutable source IR."""

    expected_ids = {
        design.id,
        design.body.id,
        *(item.id for item in design.sketches),
        *(item.id for item in design.features),
        *(item.id for item in design.edge_treatments),
    }
    if len(records) != 2 + len(design.sketches) + len(design.features) + len(
        design.edge_treatments
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    by_ir_id: dict[str, tuple[object, dict[str, object]]] = {}
    for obj, data in records:
        if data["design_id"] != design.id or data["design_digest"] != design.digest:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        ir_id = _text(data["ir_id"], _IR_ID)
        if ir_id in by_ir_id:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        by_ir_id[ir_id] = (obj, data)
    if set(by_ir_id) != expected_ids:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    _, body_data = by_ir_id[design.body.id]
    if (
        body_data["kind"] != "body"
        or body_data["feature_ids"] != [item.id for item in design.features]
        or body_data["sketch_ids"] != [item.id for item in design.sketches]
        or body_data.get("edge_treatment_ids", []) != [item.id for item in design.edge_treatments]
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    carrier, carrier_data = by_ir_id[design.id]
    if carrier_data["kind"] != "parameters":
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    raw_entries = _sequence(carrier_data["parameters"], maximum=MAX_DESIGN_PARAMETERS)
    entries: list[dict[str, object]] = []
    for raw in raw_entries:
        _parameter_metadata_entry(raw)
        assert type(raw) is dict
        entries.append(raw)
    properties = {item.id: _parameter_property(item) for item in design.parameters}
    for index, feature in enumerate(design.features):
        if feature.kind not in _SURFACE_MODIFIER_KINDS:
            continue
        _, feature_data = by_ir_id[feature.id]
        expected_bindings = []
        for name, parameter_id, target in sorted(
            _feature_parameter_bindings(feature),
            key=lambda item: item[0],
        ):
            property_name = properties[parameter_id]
            expected_bindings.append(
                {
                    "name": name,
                    "parameter_id": parameter_id,
                    "property": property_name,
                    "target": target,
                    "expression": f"{carrier.Name}.{property_name}",  # type: ignore[attr-defined]
                }
            )
        expected_plane_token = (
            None if feature.neutral_plane is None else _ORIGIN_PLANE_OBJECTS[feature.neutral_plane]
        )
        expected_direction_token = (
            None
            if feature.neutral_plane is None
            else _ORIGIN_PLANE_PULL_DIRECTIONS[feature.neutral_plane]
        )
        expected_intersection = False if feature.kind is FeatureKind.THICKNESS else None
        if (
            feature_data["kind"] != "feature"
            or feature_data["feature_index"] != index
            or feature_data["feature_kind"] != feature.kind.value
            or feature_data["base_feature_id"] != feature.base_feature_id
            or feature_data["bindings"] != expected_bindings
            or feature_data["face_targets"] != _surface_modifier_metadata_targets(feature)
            or feature_data["neutral_plane"]
            != (None if feature.neutral_plane is None else feature.neutral_plane.value)
            or feature_data["neutral_plane_token"] != expected_plane_token
            or feature_data["pull_direction_token"] != expected_direction_token
            or feature_data["refine"] is not True
            or feature_data["reversed"] is not feature.reversed
            or feature_data["thickness_mode"]
            != ("Skin" if feature.kind is FeatureKind.THICKNESS else None)
            or feature_data["thickness_join"]
            != ("Arc" if feature.kind is FeatureKind.THICKNESS else None)
            or feature_data["thickness_intersection"] is not expected_intersection
        ):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    for index, treatment in enumerate(design.edge_treatments):
        _, treatment_data = by_ir_id[treatment.id]
        expected_base = (
            design.features[-1].id if index == 0 else design.edge_treatments[index - 1].id
        )
        actual_targets = _sequence(treatment_data["targets"], maximum=16)
        expected_targets = _edge_treatment_metadata_targets(treatment, properties)
        targets_match = len(actual_targets) == len(expected_targets)
        if targets_match:
            for actual, expected, target in zip(
                actual_targets,
                expected_targets,
                treatment.targets,
                strict=True,
            ):
                parsed = _edge_treatment_target(actual)
                expected["forward"] = parsed[-1]
                if actual != expected or (
                    (target.start_parameter_id != target.end_parameter_id)
                    != (type(parsed[-1]) is bool)
                ):
                    targets_match = False
                    break
        if (
            treatment_data["kind"] != "edge_treatment"
            or treatment_data["base_feature_id"] != expected_base
            or treatment_data["treatment_index"] != index
            or treatment_data["treatment_kind"] != treatment.kind.value
            or not targets_match
        ):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    expected_entries: list[dict[str, object]] = []
    for item in design.parameters:
        expected: dict[str, object] = {
            "id": item.id,
            "property": properties[item.id],
            "unit": item.unit.value,
        }
        if item.expression is not None:
            expected["expression"] = _parameter_expression_metadata(
                item.expression,
                properties,
                item.unit,
            )
        expected_entries.append(expected)
    if entries != expected_entries:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return carrier, tuple(entries)


def _placement_snapshot(obj: object) -> tuple[float, ...]:
    try:
        placement = obj.Placement  # type: ignore[attr-defined]
        base = placement.Base
        quaternion = tuple(placement.Rotation.Q)
        values = (base.x, base.y, base.z, *quaternion)
        snapshot = tuple(float(item) for item in values)
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if len(snapshot) != 7 or not all(math.isfinite(item) for item in snapshot):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return snapshot


def _quantity_as_unit(value: object, unit: str) -> float:
    try:
        convert = value.getValueAs  # type: ignore[attr-defined]
    except Exception:
        convert = None
    if callable(convert):
        try:
            value = convert(unit)
        except Exception:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    try:
        raw = value if type(value) in {int, float} else value.Value  # type: ignore[attr-defined]
        result = float(raw)
    except Exception:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    if not math.isfinite(result):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    return result


def _require_parameter_consumer_values(
    records: tuple[tuple[object, dict[str, object]], ...],
    *,
    parameter_id: str,
    unit: str,
    expected: float,
    consumer_ids: tuple[str, ...],
) -> None:
    actual_consumers: set[str] = set()
    for obj, data in records:
        if data["kind"] == "sketch":
            for raw in _sequence(data["constraints"], maximum=256):
                entry = _exact_mapping(raw, {"id", "indices", "types", "names", "bindings"})
                indices = _sequence(entry["indices"], maximum=_MAX_COMPILED_CONSTRAINTS_PER_ENTRY)
                bindings = _sequence(entry["bindings"], maximum=_MAX_COMPILED_CONSTRAINTS_PER_ENTRY)
                if len(indices) != len(bindings):
                    _raise(ParametricCompileErrorCode.METADATA_FAILURE)
                for index, raw_binding in zip(indices, bindings, strict=True):
                    binding = _constraint_binding(raw_binding)
                    if binding is None or binding[0] != parameter_id:
                        continue
                    try:
                        datum = obj.getDatum(index)  # type: ignore[attr-defined]
                    except Exception:
                        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
                    if not math.isclose(
                        _quantity_as_unit(datum, unit),
                        expected,
                        rel_tol=0.0,
                        abs_tol=1e-8,
                    ):
                        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
                    actual_consumers.add(_text(data["ir_id"], _IR_ID))
        elif data["kind"] == "feature":
            for raw_binding in _sequence(data["bindings"], maximum=4):
                _, bound_id, _, target, _ = _feature_binding(raw_binding)
                if bound_id != parameter_id:
                    continue
                try:
                    quantity = getattr(obj, target)
                except Exception:
                    _raise(ParametricCompileErrorCode.METADATA_FAILURE)
                if not math.isclose(
                    _quantity_as_unit(quantity, unit),
                    expected,
                    rel_tol=0.0,
                    abs_tol=1e-8,
                ):
                    _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
                actual_consumers.add(_text(data["ir_id"], _IR_ID))
        elif data["kind"] == "edge_treatment":
            matched = False
            for raw_target in _sequence(data["targets"], maximum=16):
                (
                    _reference,
                    start_id,
                    start_property,
                    end_id,
                    end_property,
                    _forward,
                ) = _edge_treatment_target(raw_target)
                for bound_id, property_name in (
                    (start_id, start_property),
                    (end_id, end_property),
                ):
                    if bound_id != parameter_id:
                        continue
                    if not math.isclose(
                        _quantity_value(_edge_treatment_carrier(obj), property_name),
                        expected,
                        rel_tol=0.0,
                        abs_tol=1e-8,
                    ):
                        _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
                    matched = True
            if matched:
                actual_consumers.add(_text(data["ir_id"], _IR_ID))
    if tuple(sorted(actual_consumers)) != consumer_ids:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)


def _direct_parameter_consumer_ids(
    design: ParametricDesignIR,
    parameter_id: str,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                *(
                    sketch.id
                    for sketch in design.sketches
                    if any(
                        constraint.parameter_id == parameter_id for constraint in sketch.constraints
                    )
                ),
                *(
                    feature.id
                    for feature in design.features
                    if parameter_id in feature.parameters.values()
                ),
                *(
                    treatment.id
                    for treatment in design.edge_treatments
                    if any(
                        parameter_id in {target.start_parameter_id, target.end_parameter_id}
                        for target in treatment.targets
                    )
                ),
            }
        )
    )


def _affected_parameter_ids(
    design: ParametricDesignIR,
    source_parameter_id: str,
) -> tuple[str, ...]:
    affected = {source_parameter_id}
    changed = True
    while changed:
        changed = False
        for parameter in design.parameters:
            expression = parameter.expression
            if (
                parameter.id not in affected
                and expression is not None
                and any(source_id in affected for source_id in expression.terms)
            ):
                affected.add(parameter.id)
                changed = True
    return tuple(sorted(affected))


def _resolved_live_parameter_values(
    design: ParametricDesignIR,
    source_values: Mapping[str, float],
) -> dict[str, float]:
    parameters = {item.id: item for item in design.parameters}
    resolved: dict[str, float] = {}

    def resolve(parameter_id: str) -> float:
        if parameter_id in resolved:
            return resolved[parameter_id]
        parameter = parameters[parameter_id]
        expression = parameter.expression
        if expression is None:
            try:
                value = float(source_values[parameter_id])
            except (KeyError, TypeError, ValueError):
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        else:
            value = math.fsum(
                (
                    float(expression.constant),
                    *(
                        float(coefficient) * resolve(source_id)
                        for source_id, coefficient in expression.terms.items()
                    ),
                )
            )
        if not math.isfinite(value) or abs(value) > 1_000_000_000_000:
            _raise(ParametricCompileErrorCode.INVALID_INPUT, "/value")
        resolved[parameter_id] = value
        return value

    for parameter in design.parameters:
        resolve(parameter.id)
    return resolved


def _refresh_edge_treatment_tail(
    document: object,
    records: tuple[tuple[object, dict[str, object]], ...],
) -> None:
    treatments = tuple(
        sorted(
            (item for item in records if item[1]["kind"] == "edge_treatment"),
            key=lambda item: _integer(item[1]["treatment_index"], maximum=7),
        )
    )
    if not treatments:
        document.recompute()  # type: ignore[attr-defined]
        return
    bodies = tuple(item for item in records if item[1]["kind"] == "body")
    carriers = tuple(item for item in records if item[1]["kind"] == "parameters")
    if len(bodies) != 1 or len(carriers) != 1:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    by_ir_id = {_text(data["ir_id"], _IR_ID): (obj, data) for obj, data in records}
    carrier = carriers[0][0]
    base = bodies[0][0]
    try:
        document.recompute()  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.CAD_FAILURE)
    for obj, data in treatments:
        expected = _resolved_treatment_edges(base, data, by_ir_id, carrier)
        try:
            if obj.Base is not base:  # type: ignore[attr-defined]
                raise ValueError
            obj.Edges = [item.native for item in expected]  # type: ignore[attr-defined]
            document.recompute()  # type: ignore[attr-defined]
        except Exception:
            _raise(ParametricCompileErrorCode.CAD_FAILURE)
        _validate_edge_treatment_metadata(obj, data)
        _validate_treatment_resolution(
            obj,
            data,
            base=base,
            by_ir_id=by_ir_id,
            carrier=carrier,
        )
        base = obj


def modify_parametric_parameter(
    session: object,
    design: object,
    *,
    body: object,
    parameter_id: object,
    value: object,
    verify: Callable[[ParametricParameterEdit], None] | None = None,
) -> ParametricParameterEdit:
    """Atomically edit one public design parameter in an existing native body."""

    if type(design) is not ParametricDesignIR:
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/design")
    if type(parameter_id) is not str or _IR_ID.fullmatch(parameter_id) is None:
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/parameter_id")
    if not parameter_id.startswith("ir_parameter_"):
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/parameter_id")
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/value")
    if verify is not None and not callable(verify):
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/verify")
    parameter = next((item for item in design.parameters if item.id == parameter_id), None)
    if parameter is None or not parameter.public:
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/parameter_id")
    affected_parameter_ids = _affected_parameter_ids(design, parameter_id)
    direct_consumers = {
        affected_id: _direct_parameter_consumer_ids(design, affected_id)
        for affected_id in affected_parameter_ids
    }
    consumer_ids = tuple(
        sorted(
            {
                consumer_id
                for affected_id in affected_parameter_ids
                for consumer_id in direct_consumers[affected_id]
            }
        )
    )
    if not consumer_ids:
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/parameter_id")

    try:
        document = session.doc  # type: ignore[attr-defined]
        objects_before = tuple(document.Objects)
        undo_mode = document.UndoMode
        transaction = session._transaction  # type: ignore[attr-defined]
    except Exception:
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/session")
    if (
        document is None
        or not any(item is body for item in objects_before)
        or type(undo_mode) is not int
        or undo_mode != 1
        or not callable(transaction)
    ):
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/session")

    stabilize_parametric_session(session)
    objects_before = tuple(document.Objects)
    records = _parametric_records(document)
    body_records = tuple(item for item in records if item[1]["kind"] == "body")
    carrier_records = tuple(item for item in records if item[1]["kind"] == "parameters")
    if len(body_records) != 1 or len(carrier_records) != 1 or body_records[0][0] is not body:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    body_data = body_records[0][1]
    if (
        body_data["design_id"] != design.id
        or body_data["design_digest"] != design.digest
        or body_data["ir_id"] != design.body.id
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    carrier, entries = _source_parameter_mapping(records, design)
    entry = next((item for item in entries if item["id"] == parameter_id), None)
    expected_unit = parameter.unit.value
    expected_property = _parameter_property(parameter)
    if entry is None or entry["unit"] != expected_unit or entry["property"] != expected_property:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    before_values = {
        item.id: _quantity_value(carrier, _parameter_property(item)) for item in design.parameters
    }
    resolved_before_values = _resolved_live_parameter_values(design, before_values)
    if any(
        not math.isclose(
            before_values[item.id],
            resolved_before_values[item.id],
            rel_tol=0.0,
            abs_tol=1e-8,
        )
        for item in design.parameters
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    before_value = before_values[parameter.id]
    if math.isclose(before_value, float(value), rel_tol=0.0, abs_tol=1e-9):
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/value")

    requested_source_values = dict(before_values)
    requested_source_values[parameter.id] = float(value)
    after_values = _resolved_live_parameter_values(design, requested_source_values)
    try:
        replace(
            design,
            parameters=tuple(
                replace(item, value=after_values[item.id]) for item in design.parameters
            ),
        )
    except Exception:
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/value")

    metadata_before = tuple((obj, getattr(obj, PARAMETRIC_METADATA_PROPERTY)) for obj, _ in records)
    placements_before = tuple((obj, _placement_snapshot(obj)) for obj, _ in records)
    edit: ParametricParameterEdit | None = None

    try:
        with transaction("modify_parametric_parameter", claim_new_objects=False):
            setattr(carrier, expected_property, value)
            _refresh_edge_treatment_tail(document, records)
            stabilize_parametric_session(session)
            objects_after = tuple(document.Objects)
            if len(objects_after) != len(objects_before) or any(
                current is not previous
                for current, previous in zip(objects_after, objects_before, strict=True)
            ):
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            after_value = _quantity_value(carrier, expected_property)
            if not math.isclose(after_value, float(value), rel_tol=0.0, abs_tol=1e-9):
                _raise(ParametricCompileErrorCode.FEATURE_FAILURE)
            for item in design.parameters:
                actual = _quantity_value(carrier, _parameter_property(item))
                expected = after_values[item.id]
                if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9):
                    _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            if any(
                getattr(obj, PARAMETRIC_METADATA_PROPERTY) != raw for obj, raw in metadata_before
            ) or any(
                any(
                    not math.isclose(left, right, rel_tol=0.0, abs_tol=1e-9)
                    for left, right in zip(_placement_snapshot(obj), snapshot, strict=True)
                )
                for obj, snapshot in placements_before
            ):
                _raise(ParametricCompileErrorCode.METADATA_FAILURE)
            current_records = _parametric_records(document)
            _source_parameter_mapping(current_records, design)
            parameters_by_id = {item.id: item for item in design.parameters}
            for affected_id in affected_parameter_ids:
                affected_consumers = direct_consumers[affected_id]
                if not affected_consumers:
                    continue
                _require_parameter_consumer_values(
                    current_records,
                    parameter_id=affected_id,
                    unit=parameters_by_id[affected_id].unit.value,
                    expected=after_values[affected_id],
                    consumer_ids=affected_consumers,
                )
            edit = ParametricParameterEdit(
                design_id=design.id,
                design_digest=design.digest,
                body=body,
                parameter_id=parameter.id,
                parameter_name=parameter.name,
                unit=expected_unit,
                before_value=before_value,
                after_value=after_value,
                consumer_ids=consumer_ids,
            )
            if verify is not None:
                verify(edit)
    except ParametricCompileError:
        raise
    except Exception:
        _raise(ParametricCompileErrorCode.CAD_FAILURE)
    if edit is None:  # pragma: no cover - the transaction must either assign or raise
        _raise(ParametricCompileErrorCode.CAD_FAILURE)
    return edit


def compile_parametric_design(
    session: object,
    design: object,
    *,
    adopt: Callable[[CompiledParametricDesign], None] | None = None,
) -> CompiledParametricDesign:
    """Compile a validated design into one native editable PartDesign body."""

    if adopt is not None and not callable(adopt):
        _raise(ParametricCompileErrorCode.INVALID_INPUT, "/adopt")
    checked = _preflight(session, design)
    parameter_by_id = {item.id: item for item in checked.parameters}
    parameter_properties = {item.id: _parameter_property(item) for item in checked.parameters}
    document = session.doc  # type: ignore[attr-defined]
    transaction = session._transaction  # type: ignore[attr-defined]
    try:
        FreeCAD, Part, Sketcher = _load_freecad_modules()
        with transaction("compile_parametric_design", claim_new_objects=False):
            body = document.addObject(  # type: ignore[attr-defined]
                "PartDesign::Body",
                f"VibeCADBody_{_suffix(checked.body.id)}",
            )
            body.Label = checked.body.name
            carrier = document.addObject(  # type: ignore[attr-defined]
                "Part::Feature",
                f"VibeCADParameters_{_suffix(checked.id)}",
            )
            carrier.Label = f"{checked.name} Parameters"
            parameter_entries: list[dict[str, object]] = []
            for parameter in checked.parameters:
                property_name = parameter_properties[parameter.id]
                property_type = (
                    "App::PropertyLength"
                    if parameter.unit is DesignUnit.MM
                    else "App::PropertyAngle"
                )
                carrier.addProperty(  # type: ignore[attr-defined]
                    property_type,
                    property_name,
                    "VibeCAD Parameters",
                    parameter.name,
                )
                setattr(carrier, property_name, parameter.value)
                if not parameter.public:
                    carrier.setEditorMode(property_name, 2)  # type: ignore[attr-defined]
                entry: dict[str, object] = {
                    "id": parameter.id,
                    "property": property_name,
                    "unit": parameter.unit.value,
                }
                if parameter.expression is not None:
                    entry["expression"] = _parameter_expression_metadata(
                        parameter.expression,
                        parameter_properties,
                        parameter.unit,
                    )
                parameter_entries.append(entry)
            for parameter in checked.parameters:
                if parameter.expression is None:
                    continue
                carrier.setExpression(  # type: ignore[attr-defined]
                    parameter_properties[parameter.id],
                    _compiled_parameter_expression(
                        parameter.expression,
                        parameter_properties,
                        parameter.unit,
                    ),
                )
            document.recompute()
            _write_metadata(
                carrier,
                {
                    "schema": _METADATA_SCHEMA,
                    "kind": "parameters",
                    "design_id": checked.id,
                    "design_digest": checked.digest,
                    "ir_id": checked.id,
                    "parameters": parameter_entries,
                },
            )

            pending: list[
                tuple[
                    ParametricSketch, object, dict[str, tuple[int, ...]], dict[str, tuple[int, ...]]
                ]
            ] = []
            for sketch in checked.sketches:
                sketch_object = document.addObject(  # type: ignore[attr-defined]
                    "Sketcher::SketchObject",
                    f"VibeCADSketch_{_suffix(sketch.id)}",
                )
                sketch_object.Label = sketch.name
                body.addObject(sketch_object)
                _apply_plane(FreeCAD, sketch_object, _plane_basis(checked, sketch))
                geometry_indices: dict[str, tuple[int, ...]] = {}
                geometry_entries: list[dict[str, object]] = []
                for geometry in sketch.geometries:
                    indexes: list[int] = []
                    type_ids: list[str] = []
                    for value, type_id in _geometry_values(FreeCAD, Part, geometry):
                        index = sketch_object.addGeometry(value, geometry.construction)
                        if type(index) is not int or index < 0:
                            _raise(ParametricCompileErrorCode.CAD_FAILURE)
                        try:
                            if sketch_object.Geometry[index].TypeId != type_id:
                                raise ValueError
                        except Exception:
                            _raise(ParametricCompileErrorCode.CAD_FAILURE)
                        indexes.append(index)
                        type_ids.append(type_id)
                    geometry_indices[geometry.id] = tuple(indexes)
                    geometry_entries.append(
                        {
                            "id": geometry.id,
                            "indices": indexes,
                            "type_ids": type_ids,
                            "construction": [geometry.construction] * len(indexes),
                        }
                    )
                geometry_by_id = {item.id: item for item in sketch.geometries}
                constraint_indices: dict[str, tuple[int, ...]] = {}
                constraint_entries: list[dict[str, object]] = []
                occupied_constraint_ids = {item.id for item in sketch.constraints}
                for geometry in sketch.geometries:
                    if geometry.kind is not GeometryKind.SLOT:
                        continue
                    generated_id = _slot_constraint_id(geometry)
                    if generated_id in occupied_constraint_ids:
                        _raise(ParametricCompileErrorCode.INVALID_INPUT)
                    occupied_constraint_ids.add(generated_id)
                    generated_indices: list[int] = []
                    generated_types: list[str] = []
                    generated_names: list[str] = []
                    generated_values = _slot_constraint_objects(
                        Sketcher,
                        geometry,
                        geometry_indices[geometry.id],
                    )
                    if len(generated_values) != _SLOT_NATIVE_CONSTRAINT_COUNT:
                        _raise(ParametricCompileErrorCode.CAD_FAILURE)
                    for generated_index, constraint_value in enumerate(generated_values):
                        index = sketch_object.addConstraint(constraint_value)
                        if type(index) is not int or index < 0:
                            _raise(ParametricCompileErrorCode.CAD_FAILURE)
                        name = _slot_constraint_name(geometry, generated_index)
                        sketch_object.renameConstraint(index, name)
                        actual = sketch_object.Constraints[index]
                        generated_indices.append(index)
                        generated_types.append(actual.Type)
                        generated_names.append(name)
                    constraint_entries.append(
                        {
                            "id": generated_id,
                            "indices": generated_indices,
                            "types": generated_types,
                            "names": generated_names,
                            "bindings": [None] * len(generated_indices),
                        }
                    )
                for constraint in sketch.constraints:
                    references = tuple(
                        _resolve_reference(item, geometry_by_id, geometry_indices)
                        for item in constraint.references
                    )
                    parameter = (
                        None
                        if constraint.parameter_id is None
                        else parameter_by_id[constraint.parameter_id]
                    )
                    constraint_value = _constraint_object(
                        Sketcher,
                        constraint,
                        references,
                        parameter,
                    )
                    index = sketch_object.addConstraint(constraint_value)
                    if type(index) is not int or index < 0:
                        _raise(ParametricCompileErrorCode.CAD_FAILURE)
                    name = _constraint_name(constraint)
                    sketch_object.renameConstraint(index, name)
                    binding = None
                    if parameter is not None:
                        property_name = parameter_properties[parameter.id]
                        expression = f"{carrier.Name}.{property_name}"
                        sketch_object.setExpression(
                            f"Constraints.{name}",
                            expression,
                        )
                        binding = {
                            "parameter_id": parameter.id,
                            "property": property_name,
                            "expression": expression,
                        }
                    actual = sketch_object.Constraints[index]
                    constraint_indices[constraint.id] = (index,)
                    constraint_entries.append(
                        {
                            "id": constraint.id,
                            "indices": [index],
                            "types": [actual.Type],
                            "names": [name],
                            "bindings": [binding],
                        }
                    )
                constraint_entries.sort(key=lambda entry: str(entry["id"]))
                _write_metadata(
                    sketch_object,
                    {
                        "schema": _METADATA_SCHEMA,
                        "kind": "sketch",
                        "design_id": checked.id,
                        "design_digest": checked.digest,
                        "ir_id": sketch.id,
                        "geometries": geometry_entries,
                        "constraints": constraint_entries,
                    },
                )
                pending.append((sketch, sketch_object, geometry_indices, constraint_indices))
            body_metadata: dict[str, object] = {
                "schema": _METADATA_SCHEMA,
                "kind": "body",
                "design_id": checked.id,
                "design_digest": checked.digest,
                "ir_id": checked.body.id,
                "feature_ids": [item.id for item in checked.features],
                "sketch_ids": [item.id for item in checked.sketches],
            }
            if checked.edge_treatments:
                body_metadata["edge_treatment_ids"] = [item.id for item in checked.edge_treatments]
            _write_metadata(body, body_metadata)
            document.recompute()
            bindings = tuple(
                CompiledSketchBinding(
                    sketch_id=sketch.id,
                    object=sketch_object,
                    geometry_indices=MappingProxyType(dict(geometry_indices)),
                    constraint_indices=MappingProxyType(dict(constraint_indices)),
                    solver=_validate_sketch_metadata(
                        sketch_object,
                        _read_metadata(sketch_object, required=True),  # type: ignore[arg-type]
                    ),
                )
                for sketch, sketch_object, geometry_indices, constraint_indices in pending
            )
            for binding in bindings:
                _require_solver_success(binding.solver)

            sketch_bindings = {item.sketch_id: item for item in bindings}
            sketches_by_id = {item.id: item for item in checked.sketches}
            for feature_index, feature in enumerate(checked.features):
                if feature.kind in _SKETCHLESS_FEATURE_KINDS:
                    continue
                if feature.sketch_id is None:
                    _raise(ParametricCompileErrorCode.INVALID_INPUT)
                wire_count = _require_profile_closure(
                    sketch_bindings[feature.sketch_id].object,
                    expected_edge_count=_expected_profile_edge_count(
                        sketches_by_id[feature.sketch_id]
                    ),
                    path=f"/features/{feature_index}",
                )
                _require_supported_feature_profile(
                    feature.kind,
                    wire_count,
                    path=f"/features/{feature_index}",
                )

            compiled_features: list[CompiledFeatureBinding] = []
            feature_contracts_by_id = {item.id: item for item in checked.features}
            feature_objects_by_id: dict[str, object] = {}
            previous_feature: object | None = None
            for feature_index, feature in enumerate(checked.features):
                feature_object = body.newObject(  # type: ignore[attr-defined]
                    _FEATURE_TYPE_IDS[feature.kind],
                    f"VibeCADFeature_{_suffix(feature.id)}",
                )
                feature_object.Label = feature.name
                sketch: ParametricSketch | None = None
                sketch_object: object | None = None
                axis_token: str | None = None
                direction_token: str | None = None
                mirror_plane_token: str | None = None
                neutral_plane_token: str | None = None
                pull_direction_token: str | None = None
                thickness_mode: str | None = None
                thickness_join: str | None = None
                thickness_intersection: bool | None = None
                if feature.kind in _PATTERN_KINDS:
                    if feature.source_feature_id is None:
                        _raise(ParametricCompileErrorCode.INVALID_INPUT)
                    source_object = feature_objects_by_id.get(feature.source_feature_id)
                    if source_object is None:
                        _raise(ParametricCompileErrorCode.INVALID_INPUT)
                    feature_object.Originals = [source_object]
                    if feature.kind is FeatureKind.LINEAR_PATTERN:
                        if feature.direction is None or feature.occurrences is None:
                            _raise(ParametricCompileErrorCode.INVALID_INPUT)
                        direction_token = _PATTERN_DIRECTION_OBJECTS[feature.direction]
                        feature_object.Direction = (
                            _origin_reference(document, direction_token),
                            [""],
                        )
                        feature_object.Occurrences = feature.occurrences
                        feature_object.Reversed = feature.reversed
                    elif feature.kind is FeatureKind.CIRCULAR_PATTERN:
                        if feature.axis is None or feature.occurrences is None:
                            _raise(ParametricCompileErrorCode.INVALID_INPUT)
                        axis_token = _PATTERN_AXIS_OBJECTS[feature.axis]
                        feature_object.Axis = (
                            _origin_reference(document, axis_token),
                            [""],
                        )
                        feature_object.Occurrences = feature.occurrences
                        feature_object.Reversed = feature.reversed
                    else:
                        if feature.mirror_plane is None:
                            _raise(ParametricCompileErrorCode.INVALID_INPUT)
                        mirror_plane_token = _MIRROR_PLANE_OBJECTS[feature.mirror_plane]
                        feature_object.MirrorPlane = (
                            _origin_reference(document, mirror_plane_token),
                            [""],
                        )
                    # Assigning ``Originals`` makes FreeCAD restore the source
                    # feature as the Body tip.  Reassert the new native
                    # pattern before recompute so its shape is active and the
                    # next PartDesign feature receives it as ``BaseFeature``.
                    body.Tip = feature_object
                elif feature.kind in _SURFACE_MODIFIER_KINDS:
                    if previous_feature is None:
                        _raise(ParametricCompileErrorCode.INVALID_INPUT)
                    face_targets = _surface_modifier_metadata_targets(feature)
                    by_ir_id = {
                        _text(data["ir_id"], _IR_ID): (obj, data)
                        for obj, data in _parametric_records(document)
                    }
                    resolved_faces = _resolved_surface_faces(
                        previous_feature,
                        {
                            "feature_kind": feature.kind.value,
                            "face_targets": face_targets,
                        },
                        by_ir_id,
                    )
                    feature_object.Base = (
                        previous_feature,
                        [item.native for item in resolved_faces],
                    )
                    feature_object.Refine = True
                    feature_object.Reversed = feature.reversed
                    if feature.kind is FeatureKind.THICKNESS:
                        thickness_mode = "Skin"
                        thickness_join = "Arc"
                        thickness_intersection = False
                        feature_object.Mode = thickness_mode
                        feature_object.Join = thickness_join
                        feature_object.Intersection = thickness_intersection
                    else:
                        if feature.neutral_plane is None:
                            _raise(ParametricCompileErrorCode.INVALID_INPUT)
                        neutral_plane_token = _ORIGIN_PLANE_OBJECTS[feature.neutral_plane]
                        pull_direction_token = _ORIGIN_PLANE_PULL_DIRECTIONS[feature.neutral_plane]
                        feature_object.NeutralPlane = (
                            _origin_reference(document, neutral_plane_token),
                            [""],
                        )
                        feature_object.PullDirection = (
                            _origin_reference(document, pull_direction_token),
                            [""],
                        )
                    body.Tip = feature_object
                else:
                    if feature.sketch_id is None:
                        _raise(ParametricCompileErrorCode.INVALID_INPUT)
                    sketch = sketches_by_id[feature.sketch_id]
                    sketch_object = sketch_bindings[feature.sketch_id].object
                    feature_object.Profile = sketch_object
                    feature_object.Reversed = feature.reversed
                    feature_object.Refine = True
                    if feature.kind in {FeatureKind.PAD, FeatureKind.POCKET}:
                        feature_object.SideType = "Symmetric" if feature.symmetric else "One side"
                        feature_object.AlongSketchNormal = True
                        feature_object.UseCustomVector = False
                        feature_object.Offset = 0
                        feature_object.Offset2 = 0
                        feature_object.TaperAngle = 0
                        feature_object.TaperAngle2 = 0
                    else:
                        feature_object.Midplane = feature.symmetric

                    if feature.kind is FeatureKind.PAD:
                        feature_object.Type = "Length"
                    elif feature.kind is FeatureKind.POCKET:
                        feature_object.Type = (
                            "Length" if feature.extent is FeatureExtent.LENGTH else "ThroughAll"
                        )
                    elif feature.kind is FeatureKind.REVOLVE:
                        feature_object.Type = "Angle"
                        axis_token = _revolution_axis_token(sketch, feature.axis or "")
                        if axis_token.startswith("Axis"):
                            try:
                                axis_index = int(axis_token.removeprefix("Axis"))
                                if not 0 <= axis_index < int(sketch_object.AxisCount):  # type: ignore[attr-defined]
                                    raise ValueError
                            except Exception:
                                _raise(ParametricCompileErrorCode.CAD_FAILURE)
                        feature_object.ReferenceAxis = (sketch_object, [axis_token])
                    else:
                        feature_object.DepthType = (
                            "Dimension" if feature.extent is FeatureExtent.LENGTH else "ThroughAll"
                        )
                        feature_object.HoleCutType = "None"
                        feature_object.HoleCutCustomValues = False
                        feature_object.ThreadType = "None"
                        feature_object.Threaded = False
                        feature_object.ModelThread = False
                        feature_object.Tapered = False
                        feature_object.DrillPoint = "Flat"
                        feature_object.DrillForDepth = False
                        feature_object.UseCustomThreadClearance = False

                feature_binding_entries: list[dict[str, object]] = []
                for name, parameter_id, target in sorted(
                    _feature_parameter_bindings(feature),
                    key=lambda item: item[0],
                ):
                    parameter = parameter_by_id[parameter_id]
                    property_name = parameter_properties[parameter_id]
                    expression = f"{carrier.Name}.{property_name}"
                    setattr(feature_object, target, parameter.value)
                    feature_object.setExpression(target, expression)
                    feature_binding_entries.append(
                        {
                            "name": name,
                            "parameter_id": parameter_id,
                            "property": property_name,
                            "target": target,
                            "expression": expression,
                        }
                    )

                feature_metadata: dict[str, object] = {
                    "schema": _METADATA_SCHEMA,
                    "kind": "feature",
                    "design_id": checked.id,
                    "design_digest": checked.digest,
                    "ir_id": feature.id,
                    "axis": feature.axis,
                    "axis_token": axis_token,
                    "base_feature_id": feature.base_feature_id,
                    "bindings": feature_binding_entries,
                    "extent": None if feature.extent is None else feature.extent.value,
                    "feature_index": feature_index,
                    "feature_kind": feature.kind.value,
                    "location_geometry_ids": list(feature.location_geometry_ids),
                    "reversed": feature.reversed,
                    "sketch_id": feature.sketch_id,
                    "symmetric": feature.symmetric,
                }
                if feature.kind in _PATTERN_KINDS:
                    feature_metadata.update(
                        {
                            "direction": (
                                None if feature.direction is None else feature.direction.value
                            ),
                            "direction_token": direction_token,
                            "mirror_plane": (
                                None if feature.mirror_plane is None else feature.mirror_plane.value
                            ),
                            "mirror_plane_token": mirror_plane_token,
                            "occurrences": feature.occurrences,
                            "source_feature_id": feature.source_feature_id,
                        }
                    )
                elif feature.kind in _SURFACE_MODIFIER_KINDS:
                    feature_metadata.update(
                        {
                            "face_targets": _surface_modifier_metadata_targets(feature),
                            "neutral_plane": (
                                None
                                if feature.neutral_plane is None
                                else feature.neutral_plane.value
                            ),
                            "neutral_plane_token": neutral_plane_token,
                            "pull_direction_token": pull_direction_token,
                            "refine": True,
                            "thickness_intersection": thickness_intersection,
                            "thickness_join": thickness_join,
                            "thickness_mode": thickness_mode,
                        }
                    )
                _write_metadata(feature_object, feature_metadata)
                document.recompute()
                _validate_feature_metadata(
                    feature_object,
                    _read_metadata(feature_object, required=True),  # type: ignore[arg-type]
                )
                _require_feature_shape(
                    feature_object,
                    previous_feature,
                    feature.kind,
                    additive=(
                        _pattern_is_additive(feature, feature_contracts_by_id)
                        if feature.kind in _PATTERN_KINDS
                        else None
                    ),
                    path=f"/features/{feature_index}",
                )
                compiled_features.append(
                    CompiledFeatureBinding(feature_id=feature.id, object=feature_object)
                )
                feature_objects_by_id[feature.id] = feature_object
                previous_feature = feature_object

            compiled_treatments: list[CompiledFeatureBinding] = []
            result_object: object = body
            by_ir_id = {
                _text(data["ir_id"], _IR_ID): (obj, data)
                for obj, data in _parametric_records(document)
            }
            for treatment_index, treatment in enumerate(checked.edge_treatments):
                treatment_object = document.addObject(  # type: ignore[attr-defined]
                    _EDGE_TREATMENT_TYPE_IDS[treatment.kind],
                    f"VibeCADEdgeTreatment_{_suffix(treatment.id)}",
                )
                treatment_object.Label = treatment.name
                treatment_object.Base = result_object
                treatment_metadata = {
                    "schema": _METADATA_SCHEMA,
                    "kind": "edge_treatment",
                    "design_id": checked.id,
                    "design_digest": checked.digest,
                    "ir_id": treatment.id,
                    "base_feature_id": treatment.base_feature_id,
                    "targets": _edge_treatment_metadata_targets(
                        treatment,
                        parameter_properties,
                    ),
                    "treatment_index": treatment_index,
                    "treatment_kind": treatment.kind.value,
                }
                resolved_edges = _resolved_treatment_edges(
                    result_object,
                    treatment_metadata,
                    by_ir_id,
                    carrier,
                    validate_orientation=False,
                )
                for target, resolved in zip(
                    treatment_metadata["targets"],
                    resolved_edges,
                    strict=True,
                ):
                    target["forward"] = resolved.forward
                _write_metadata(treatment_object, treatment_metadata)
                by_ir_id[treatment.id] = (
                    treatment_object,
                    _read_metadata(treatment_object, required=True),  # type: ignore[arg-type]
                )
                treatment_object.Edges = [  # type: ignore[attr-defined]
                    item.native for item in resolved_edges
                ]
                document.recompute()
                _validate_edge_treatment_metadata(treatment_object, treatment_metadata)
                _validate_treatment_resolution(
                    treatment_object,
                    treatment_metadata,
                    base=result_object,
                    by_ir_id=by_ir_id,
                    carrier=carrier,
                )
                compiled_treatments.append(
                    CompiledFeatureBinding(
                        feature_id=treatment.id,
                        object=treatment_object,
                    )
                )
                result_object = treatment_object

            document.recompute()
            parametric_entity_facts(carrier)
            parametric_entity_facts(body)
            for treatment in compiled_treatments:
                parametric_entity_facts(treatment.object)
            _validate_parametric_graph(_parametric_records(document))
            compiled = CompiledParametricDesign(
                design_id=checked.id,
                design_digest=checked.digest,
                body=body,
                result_object=result_object,
                parameter_carrier=carrier,
                sketches=bindings,
                features=tuple(compiled_features),
                edge_treatments=tuple(compiled_treatments),
            )
            if adopt is not None:
                adopt(compiled)
    except ParametricCompileError:
        raise
    except Exception:
        _raise(ParametricCompileErrorCode.CAD_FAILURE)
    return compiled


def compile_design_sketches(session: object, design: object) -> CompiledSketchSet:
    """Compatibility name for the now-complete native parametric compiler."""

    return compile_parametric_design(session, design)


__all__ = (
    "PARAMETRIC_METADATA_PROPERTY",
    "CompiledFeatureBinding",
    "CompiledParametricDesign",
    "CompiledSketchBinding",
    "CompiledSketchSet",
    "ParametricCompileError",
    "ParametricCompileErrorCode",
    "ParametricEntityFact",
    "ParametricParameterEdit",
    "SketchSolverFacts",
    "compile_design_sketches",
    "compile_parametric_design",
    "modify_parametric_parameter",
    "parametric_entity_facts",
    "stabilize_parametric_session",
)
