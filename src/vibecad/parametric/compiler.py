"""FreeCAD-bound compiler for the bounded editable single-body slice.

The module is import-safe outside FreeCAD.  It creates native Sketcher and
PartDesign objects plus locked IR/index metadata only; selector identity and
Task authority remain with the execution layer that adopts the compiled body.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from vibecad.parametric.contracts import (
    ConstraintKind,
    DesignParameter,
    DesignUnit,
    FeatureExtent,
    FeatureKind,
    GeometryKind,
    OriginPlane,
    ParametricDesignIR,
    ParametricSketch,
    PartDesignFeature,
    PlaneKind,
    ReferencePoint,
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
_FEATURE_TYPE_IDS = {
    FeatureKind.PAD: "PartDesign::Pad",
    FeatureKind.POCKET: "PartDesign::Pocket",
    FeatureKind.REVOLVE: "PartDesign::Revolution",
    FeatureKind.HOLE: "PartDesign::Hole",
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
    parameter_carrier: object
    sketches: tuple[CompiledSketchBinding, ...]
    features: tuple[CompiledFeatureBinding, ...] = ()


CompiledSketchSet = CompiledParametricDesign


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
        for geometry in sketch.geometries:
            if geometry.kind is GeometryKind.SLOT:
                source_index = _geometry_source_index(sketch, geometry)
                _raise(
                    ParametricCompileErrorCode.UNSUPPORTED,
                    f"/sketches/{sketch_index}/geometries/{source_index}/kind",
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
    return design


def _expected_profile_edge_count(sketch: ParametricSketch) -> int:
    count = sum(
        not geometry.construction
        and geometry.kind in {GeometryKind.LINE, GeometryKind.CIRCLE, GeometryKind.ARC}
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
    """Keep subtractive v1 outcomes unambiguous until per-wire proof exists."""

    if kind in {FeatureKind.POCKET, FeatureKind.HOLE} and wire_count != 1:
        _raise(ParametricCompileErrorCode.UNSUPPORTED, path)


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
    bindings = [("diameter", feature.parameters["diameter"], "Diameter")]
    if feature.extent is FeatureExtent.LENGTH:
        bindings.append(("depth", feature.parameters["depth"], "Depth"))
    return tuple(bindings)


def _require_feature_shape(
    obj: object,
    previous: object | None,
    kind: FeatureKind,
    *,
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
        if kind in {FeatureKind.PAD, FeatureKind.REVOLVE}:
            changed = volume > previous_volume + tolerance
        else:
            changed = volume < previous_volume - tolerance
        if not changed:
            _raise(ParametricCompileErrorCode.FEATURE_FAILURE, path)
    return volume


def _parameter_property(parameter: DesignParameter) -> str:
    return f"P_{_suffix(parameter.id)}"


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
    if kind not in {"body", "feature", "parameters", "sketch"}:
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
        data = _exact_mapping(parsed, common | {"feature_ids", "sketch_ids"})
    elif kind == "parameters":
        data = _exact_mapping(parsed, common | {"parameters"})
    elif kind == "sketch":
        data = _exact_mapping(parsed, common | {"geometries", "constraints"})
    elif kind == "feature":
        data = _exact_mapping(
            parsed,
            common
            | {
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


def _geometry_value(FreeCAD: object, Part: object, geometry: SketchGeometry) -> tuple[object, str]:
    values = geometry.dimensions
    try:
        vector = FreeCAD.Vector  # type: ignore[attr-defined]
        if geometry.kind is GeometryKind.POINT:
            return Part.Point(vector(values["x_mm"], values["y_mm"], 0)), "Part::GeomPoint"  # type: ignore[attr-defined]
        if geometry.kind is GeometryKind.LINE:
            return (
                Part.LineSegment(  # type: ignore[attr-defined]
                    vector(values["x1_mm"], values["y1_mm"], 0),
                    vector(values["x2_mm"], values["y2_mm"], 0),
                ),
                "Part::GeomLineSegment",
            )
        if geometry.kind is GeometryKind.CIRCLE:
            return (
                Part.Circle(  # type: ignore[attr-defined]
                    vector(values["cx_mm"], values["cy_mm"], 0),
                    vector(0, 0, 1),
                    values["radius_mm"],
                ),
                "Part::GeomCircle",
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

            return Part.Arc(point(start), point(middle), point(end)), "Part::GeomArcOfCircle"  # type: ignore[attr-defined]
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


def _validate_parameter_metadata(
    obj: object, data: dict[str, object]
) -> tuple[ParametricEntityFact, ...]:
    if getattr(obj, "TypeId", None) != "Part::Feature":
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    entries = _sequence(data["parameters"], maximum=64)
    seen_ids: set[str] = set()
    seen_properties: set[str] = set()
    facts: list[ParametricEntityFact] = []
    for raw in entries:
        entry = _exact_mapping(raw, {"id", "property", "unit"})
        parameter_id = _text(entry["id"], _IR_ID)
        if not parameter_id.startswith("ir_parameter_") or parameter_id in seen_ids:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        property_name = _text(entry["property"], _PARAMETER_PROPERTY)
        if property_name in seen_properties:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        unit = _text(entry["unit"])
        expected_type = {"mm": "App::PropertyLength", "deg": "App::PropertyAngle"}.get(unit)
        if expected_type is None:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
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
    if tuple(entry["id"] for entry in entries) != tuple(sorted(seen_ids)):
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
    if name not in {"angle", "depth", "diameter", "length"}:
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    parameter_id = _text(data["parameter_id"], _IR_ID)
    if not parameter_id.startswith("ir_parameter_"):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)
    property_name = _text(data["property"], _PARAMETER_PROPERTY)
    target = _text(data["target"])
    if target not in {"Angle", "Depth", "Diameter", "Length"}:
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

    facts = [
        ParametricEntityFact("parametric.feature.extent", "none" if extent is None else extent),
        ParametricEntityFact("parametric.feature.index", feature_index),
        ParametricEntityFact("parametric.feature.kind", kind.value),
        ParametricEntityFact("parametric.profile.wire_count", wire_count),
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
    _require_feature_shape(obj, None, kind, path=f"/features/{feature_index}")
    return tuple(facts)


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
        indices = _sequence(entry["indices"], maximum=8)
        types = _sequence(entry["types"], maximum=8)
        names = _sequence(entry["names"], maximum=8)
        bindings = _sequence(entry["bindings"], maximum=8)
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
    if (
        len(bodies) != 1
        or len(carriers) != 1
        or not 1 <= len(sketches) <= 8
        or not 1 <= len(features) <= 8
    ):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    design_ids = {_text(data["design_id"], _IR_ID) for _, data in records}
    design_digests = {_text(data["design_digest"], _HEX_64) for _, data in records}
    ir_ids = tuple(_text(data["ir_id"], _IR_ID) for _, data in records)
    if len(design_ids) != 1 or len(design_digests) != 1 or len(set(ir_ids)) != len(ir_ids):
        _raise(ParametricCompileErrorCode.METADATA_FAILURE)

    body, body_data = bodies[0]
    carrier, carrier_data = carriers[0]
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
    previous_feature: object | None = None
    consumed_sketch_ids: set[str] = set()
    for feature_index, (feature_obj, feature_data) in enumerate(indexed_features):
        _validate_feature_metadata(feature_obj, feature_data)
        if _profile_object(feature_obj) not in sketch_objects:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        try:
            feature_kind = FeatureKind(_text(feature_data["feature_kind"]))
        except ValueError:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        feature_id = _text(feature_data["ir_id"], _IR_ID)
        sketch_id = _text(feature_data["sketch_id"], _IR_ID)
        expected_base_id = None if feature_index == 0 else actual_feature_ids[feature_index - 1]
        if (
            feature_data["base_feature_id"] != expected_base_id
            or (feature_index == 0 and feature_kind not in {FeatureKind.PAD, FeatureKind.REVOLVE})
            or sketch_id in consumed_sketch_ids
            or feature_id != actual_feature_ids[feature_index]
        ):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        try:
            if feature_obj.BaseFeature is not previous_feature:  # type: ignore[attr-defined]
                raise ValueError
        except Exception:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        _require_feature_shape(
            feature_obj,
            previous_feature,
            feature_kind,
            path=f"/features/{feature_index}",
        )
        consumed_sketch_ids.add(sketch_id)
        previous_feature = feature_obj

    parameter_pairs: set[tuple[str, str]] = set()
    parameter_units: dict[tuple[str, str], str] = {}
    for raw in _sequence(carrier_data["parameters"], maximum=64):
        entry = _exact_mapping(raw, {"id", "property", "unit"})
        pair = (_text(entry["id"], _IR_ID), _text(entry["property"], _PARAMETER_PROPERTY))
        if pair in parameter_pairs:
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        parameter_pairs.add(pair)
        parameter_units[pair] = _text(entry["unit"])
    for _, sketch_data in sketches:
        for raw in _sequence(sketch_data["constraints"], maximum=256):
            entry = _exact_mapping(raw, {"id", "indices", "types", "names", "bindings"})
            for raw_binding in _sequence(entry["bindings"], maximum=8):
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
        if (
            any(not _text(item, _IR_ID).startswith("ir_sketch_") for item in sketch_ids)
            or sketch_ids != sorted(set(sketch_ids))
            or any(not _text(item, _IR_ID).startswith("ir_feature_") for item in feature_ids)
            or len(feature_ids) != len(set(feature_ids))
        ):
            _raise(ParametricCompileErrorCode.METADATA_FAILURE)
        facts.extend(
            (
                ParametricEntityFact("parametric.feature_count", len(feature_ids)),
                ParametricEntityFact("parametric.sketch_count", len(sketch_ids)),
            )
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
                parameter_entries.append(
                    {
                        "id": parameter.id,
                        "property": property_name,
                        "unit": parameter.unit.value,
                    }
                )
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
                    value, type_id = _geometry_value(FreeCAD, Part, geometry)
                    index = sketch_object.addGeometry(value, geometry.construction)
                    if type(index) is not int or index < 0:
                        _raise(ParametricCompileErrorCode.CAD_FAILURE)
                    try:
                        if sketch_object.Geometry[index].TypeId != type_id:
                            raise ValueError
                    except Exception:
                        _raise(ParametricCompileErrorCode.CAD_FAILURE)
                    geometry_indices[geometry.id] = (index,)
                    geometry_entries.append(
                        {
                            "id": geometry.id,
                            "indices": [index],
                            "type_ids": [type_id],
                            "construction": [geometry.construction],
                        }
                    )
                geometry_by_id = {item.id: item for item in sketch.geometries}
                constraint_indices: dict[str, tuple[int, ...]] = {}
                constraint_entries: list[dict[str, object]] = []
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
            _write_metadata(
                body,
                {
                    "schema": _METADATA_SCHEMA,
                    "kind": "body",
                    "design_id": checked.id,
                    "design_digest": checked.digest,
                    "ir_id": checked.body.id,
                    "feature_ids": [item.id for item in checked.features],
                    "sketch_ids": [item.id for item in checked.sketches],
                },
            )
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
            previous_feature: object | None = None
            for feature_index, feature in enumerate(checked.features):
                sketch = sketches_by_id[feature.sketch_id]
                sketch_object = sketch_bindings[feature.sketch_id].object
                feature_object = body.newObject(  # type: ignore[attr-defined]
                    _FEATURE_TYPE_IDS[feature.kind],
                    f"VibeCADFeature_{_suffix(feature.id)}",
                )
                feature_object.Label = feature.name
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

                axis_token: str | None = None
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
                            if not 0 <= axis_index < int(sketch_object.AxisCount):
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

                _write_metadata(
                    feature_object,
                    {
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
                    },
                )
                document.recompute()
                _validate_feature_metadata(
                    feature_object,
                    _read_metadata(feature_object, required=True),  # type: ignore[arg-type]
                )
                _require_feature_shape(
                    feature_object,
                    previous_feature,
                    feature.kind,
                    path=f"/features/{feature_index}",
                )
                compiled_features.append(
                    CompiledFeatureBinding(feature_id=feature.id, object=feature_object)
                )
                previous_feature = feature_object

            document.recompute()
            parametric_entity_facts(carrier)
            parametric_entity_facts(body)
            _validate_parametric_graph(_parametric_records(document))
            compiled = CompiledParametricDesign(
                design_id=checked.id,
                design_digest=checked.digest,
                body=body,
                parameter_carrier=carrier,
                sketches=bindings,
                features=tuple(compiled_features),
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
    "SketchSolverFacts",
    "compile_design_sketches",
    "compile_parametric_design",
    "parametric_entity_facts",
    "stabilize_parametric_session",
)
