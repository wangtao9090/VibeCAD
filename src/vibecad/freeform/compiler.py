"""FreeCAD-bound compiler for one bounded loft or sweep solid.

The module remains import-safe when FreeCAD is absent.  It can always emit a
deterministic script and can execute directly once FreeCAD and Part are
available.  It does not create or mutate VibeCAD Tasks or Revisions.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum

from vibecad.freeform.contracts import (
    MAX_FREEFORM_IR_BYTES,
    FreeformDesign,
    FreeformFeatureKind,
    SplineCurve,
    SplineKind,
)

_HEX_32 = re.compile(r"[0-9a-f]{32}\Z")
_SCRIPT_RESULT_NAME = "VIBECAD_FREEFORM_RESULT"


class FreeformCompileErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    CAD_UNAVAILABLE = "cad_unavailable"
    CURVE_FAILURE = "curve_failure"
    PROFILE_FAILURE = "profile_failure"
    FEATURE_FAILURE = "feature_failure"
    SOLID_FAILURE = "solid_failure"
    DOCUMENT_FAILURE = "document_failure"


_MESSAGES = {
    FreeformCompileErrorCode.INVALID_INPUT: "The freeform compiler input is invalid.",
    FreeformCompileErrorCode.CAD_UNAVAILABLE: "The FreeCAD runtime is unavailable.",
    FreeformCompileErrorCode.CURVE_FAILURE: "A freeform curve could not be compiled.",
    FreeformCompileErrorCode.PROFILE_FAILURE: "A freeform section is not safely closed.",
    FreeformCompileErrorCode.FEATURE_FAILURE: "The freeform feature could not be built.",
    FreeformCompileErrorCode.SOLID_FAILURE: "The result is not one valid watertight solid.",
    FreeformCompileErrorCode.DOCUMENT_FAILURE: "The FreeCAD result object could not be created.",
}


class FreeformCompileError(RuntimeError):
    """Fixed non-reflective compiler failure."""

    def __init__(self, code: FreeformCompileErrorCode, path: str = "") -> None:
        if type(code) is not FreeformCompileErrorCode:
            raise TypeError("code must be FreeformCompileErrorCode")
        if type(path) is not str or len(path) > 512 or (path and not path.startswith("/")):
            raise ValueError("path must be a bounded JSON Pointer")
        self.code = code
        self.path = path
        self.message = _MESSAGES[code]
        super().__init__(self.message)


def _raise(code: FreeformCompileErrorCode, path: str = "") -> None:
    raise FreeformCompileError(code, path)


@dataclass(frozen=True, slots=True)
class CompiledCurve:
    curve_id: str
    edge: object
    wire: object


@dataclass(frozen=True, slots=True)
class CompiledFreeformDesign:
    design_id: str
    design_digest: str
    document: object
    result_object: object
    result_shape: object
    curves: tuple[CompiledCurve, ...]
    created_document: bool


def _load_freecad_modules() -> tuple[object, object]:
    try:
        from vibecad.freecad_env import prepare_freecad_import

        prepare_freecad_import()
        import FreeCAD  # noqa: PLC0415
        import Part  # noqa: PLC0415
    except Exception:
        _raise(FreeformCompileErrorCode.CAD_UNAVAILABLE)
    return FreeCAD, Part


def _suffix(value: str) -> str:
    result = value.rsplit("_", 1)[-1]
    if _HEX_32.fullmatch(result) is None:
        _raise(FreeformCompileErrorCode.INVALID_INPUT)
    return result


def generate_freecad_script(design: FreeformDesign) -> str:
    """Return a deterministic standalone FreeCAD-Python execution script."""

    if type(design) is not FreeformDesign:
        _raise(FreeformCompileErrorCode.INVALID_INPUT, "/design")
    payload = design.to_canonical_json()
    encoded = json.dumps(payload, ensure_ascii=False)
    return (
        "from vibecad.freeform.compiler import _execute_script_payload\n"
        f"{_SCRIPT_RESULT_NAME} = _execute_script_payload({encoded})\n"
    )


def _build_curve(curve: SplineCurve, freecad: object, part: object) -> CompiledCurve:
    try:
        poles = [
            freecad.Vector(point.x_mm, point.y_mm, point.z_mm)  # type: ignore[attr-defined]
            for point in curve.control_points
        ]
        spline = part.BSplineCurve()  # type: ignore[attr-defined]
        arguments: list[object] = [
            poles,
            list(curve.multiplicities),
            list(curve.knots),
            False,
            curve.degree,
        ]
        if curve.kind is SplineKind.NURBS:
            arguments.extend((list(curve.weights), False))
        spline.buildFromPolesMultsKnots(*arguments)
        edge = spline.toShape()
        wire = part.Wire([edge])  # type: ignore[attr-defined]
    except Exception:
        _raise(FreeformCompileErrorCode.CURVE_FAILURE)
    try:
        if bool(edge.isNull()) or not bool(edge.isValid()):
            _raise(FreeformCompileErrorCode.CURVE_FAILURE)
        if curve.closed and not bool(wire.isClosed()):
            _raise(FreeformCompileErrorCode.PROFILE_FAILURE)
        if not curve.closed and bool(wire.isClosed()):
            _raise(FreeformCompileErrorCode.CURVE_FAILURE)
    except FreeformCompileError:
        raise
    except Exception:
        _raise(FreeformCompileErrorCode.CURVE_FAILURE)
    return CompiledCurve(curve.id, edge, wire)


def _build_feature(
    design: FreeformDesign,
    compiled_by_id: dict[str, CompiledCurve],
    part: object,
) -> object:
    try:
        sections = [compiled_by_id[curve_id].wire for curve_id in design.feature.section_ids]
        if design.feature.kind is FreeformFeatureKind.LOFT:
            return part.makeLoft(sections, True, False, False, 5)  # type: ignore[attr-defined]
        guide = compiled_by_id[design.feature.guide_ids[0]].wire
        return guide.makePipeShell(sections, True, False)
    except Exception:
        _raise(FreeformCompileErrorCode.FEATURE_FAILURE)


def _validate_solid(shape: object) -> None:
    try:
        solids = tuple(shape.Solids)  # type: ignore[attr-defined]
        volume = float(shape.Volume)  # type: ignore[attr-defined]
        closed = bool(shape.isClosed())  # type: ignore[attr-defined]
        valid = bool(shape.isValid())  # type: ignore[attr-defined]
        null = bool(shape.isNull())  # type: ignore[attr-defined]
        solid_closed = len(solids) == 1 and bool(solids[0].isClosed())
    except Exception:
        _raise(FreeformCompileErrorCode.SOLID_FAILURE)
    if (
        null
        or not valid
        or not closed
        or not solid_closed
        or len(solids) != 1
        or not math.isfinite(volume)
        or volume <= 0
    ):
        _raise(FreeformCompileErrorCode.SOLID_FAILURE)


def compile_freeform(
    design: FreeformDesign,
    *,
    freecad: object | None = None,
    part: object | None = None,
    document: object | None = None,
) -> CompiledFreeformDesign:
    """Compile a validated design to one checked ``Part::Feature`` result."""

    if type(design) is not FreeformDesign:
        _raise(FreeformCompileErrorCode.INVALID_INPUT, "/design")
    if (freecad is None) != (part is None):
        _raise(FreeformCompileErrorCode.INVALID_INPUT, "/modules")
    if freecad is None:
        freecad, part = _load_freecad_modules()
    created_document = document is None
    if document is None:
        try:
            document = freecad.newDocument(f"VibeCADFreeform_{_suffix(design.id)}")  # type: ignore[attr-defined]
        except Exception:
            _raise(FreeformCompileErrorCode.DOCUMENT_FAILURE)
    result = None
    try:
        compiled = tuple(_build_curve(curve, freecad, part) for curve in design.curves)
        compiled_by_id = {curve.curve_id: curve for curve in compiled}
        shape = _build_feature(design, compiled_by_id, part)
        _validate_solid(shape)
        result = document.addObject(  # type: ignore[attr-defined]
            "Part::Feature", f"FreeformResult_{_suffix(design.feature.id)}"
        )
        result.Label = design.feature.name
        result.Shape = shape
        document.recompute()  # type: ignore[attr-defined]
        _validate_solid(result.Shape)
    except FreeformCompileError:
        if created_document:
            _close_document(freecad, document)
        elif result is not None:
            _remove_result(document, result)
        raise
    except Exception:
        if created_document:
            _close_document(freecad, document)
        elif result is not None:
            _remove_result(document, result)
        _raise(FreeformCompileErrorCode.DOCUMENT_FAILURE)
    return CompiledFreeformDesign(
        design.id,
        design.digest,
        document,
        result,
        result.Shape,
        compiled,
        created_document,
    )


def _close_document(freecad: object, document: object) -> None:
    try:
        freecad.closeDocument(document.Name)  # type: ignore[attr-defined]
    except Exception:
        pass


def _remove_result(document: object, result: object) -> None:
    try:
        document.removeObject(result.Name)  # type: ignore[attr-defined]
        document.recompute()  # type: ignore[attr-defined]
    except Exception:
        pass


def _execute_script_payload(payload_json: str) -> CompiledFreeformDesign:
    """Private fixed entrypoint used by generated scripts."""

    if type(payload_json) is not str or len(payload_json.encode("utf-8")) > MAX_FREEFORM_IR_BYTES:
        _raise(FreeformCompileErrorCode.INVALID_INPUT, "/payload")
    try:
        payload = json.loads(payload_json)
        design = FreeformDesign.from_mapping(payload)
    except Exception:
        _raise(FreeformCompileErrorCode.INVALID_INPUT, "/payload")
    return compile_freeform(design)


__all__ = [
    "CompiledCurve",
    "CompiledFreeformDesign",
    "FreeformCompileError",
    "FreeformCompileErrorCode",
    "compile_freeform",
    "generate_freecad_script",
]
