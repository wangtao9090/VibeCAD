from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from vibecad.freeform.compiler import (
    FreeformCompileError,
    FreeformCompileErrorCode,
    compile_freeform,
    generate_freecad_script,
)
from vibecad.freeform.contracts import (
    CurveRole,
    FreeformDesign,
    FreeformFeature,
    FreeformFeatureKind,
    Point3D,
    SplineCurve,
    SplineKind,
)


def _section(suffix: str, z_mm: float) -> SplineCurve:
    return SplineCurve(
        f"freeform_curve_{suffix * 32}",
        f"section-{suffix}",
        CurveRole.SECTION,
        SplineKind.BSPLINE,
        2,
        (
            Point3D(-5, -5, z_mm),
            Point3D(5, -5, z_mm),
            Point3D(5, 5, z_mm),
            Point3D(-5, 5, z_mm),
            Point3D(-5, -5, z_mm),
        ),
        (0, 0.5, 1),
        (3, 2, 3),
        (),
        True,
    )


def _design() -> FreeformDesign:
    first = _section("a", 0)
    second = _section("b", 10)
    return FreeformDesign(
        f"freeform_design_{'c' * 32}",
        "compiler loft",
        (first, second),
        FreeformFeature(
            f"freeform_feature_{'d' * 32}",
            "result loft",
            FreeformFeatureKind.LOFT,
            (first.id, second.id),
        ),
    )


def _sweep_design() -> FreeformDesign:
    section = _section("a", 0)
    guide = SplineCurve(
        f"freeform_curve_{'e' * 32}",
        "sweep guide",
        CurveRole.GUIDE,
        SplineKind.NURBS,
        2,
        (Point3D(0, 0, 0), Point3D(0, 0, 5), Point3D(0, 0, 10)),
        (0, 1),
        (3, 3),
        (1, 0.75, 1),
        False,
    )
    return FreeformDesign(
        f"freeform_design_{'f' * 32}",
        "compiler sweep",
        (section, guide),
        FreeformFeature(
            f"freeform_feature_{'1' * 32}",
            "result sweep",
            FreeformFeatureKind.SWEEP,
            (section.id,),
            (guide.id,),
        ),
    )


class _Vector(tuple):
    def __new__(cls, x: float, y: float, z: float):
        return super().__new__(cls, (x, y, z))


class _Edge:
    def __init__(self, closed: bool):
        self.closed = closed

    def isNull(self) -> bool:
        return False

    def isValid(self) -> bool:
        return True


class _Spline:
    def buildFromPolesMultsKnots(self, poles, *_args) -> None:
        self.closed = poles[0] == poles[-1]

    def toShape(self) -> _Edge:
        return _Edge(self.closed)


class _Wire:
    def __init__(self, edges):
        self.closed = edges[0].closed

    def isClosed(self) -> bool:
        return self.closed

    def makePipeShell(self, _profiles, _solid, _frenet):
        return _Shape()


class _Solid:
    def isClosed(self) -> bool:
        return True


class _Shape:
    def __init__(self, *, valid: bool = True, closed: bool = True, volume: float = 100):
        self._valid = valid
        self._closed = closed
        self.Volume = volume
        self.Solids = [_Solid()] if closed else []

    def isNull(self) -> bool:
        return False

    def isValid(self) -> bool:
        return self._valid

    def isClosed(self) -> bool:
        return self._closed


class _Part:
    def __init__(self, shape: _Shape | None = None):
        self.shape = shape or _Shape()

    BSplineCurve = _Spline
    Wire = _Wire

    def makeLoft(self, _sections, _solid, _ruled, _closed, _max_degree):
        return self.shape


class _Result:
    Name = "FakeResult"
    Label = ""
    Shape = None


class _Document:
    Name = "FakeDocument"

    def __init__(self):
        self.result = _Result()
        self.recomputed = False

    def addObject(self, type_id: str, name: str) -> _Result:
        assert type_id == "Part::Feature"
        assert name.startswith("FreeformResult_")
        return self.result

    def recompute(self) -> None:
        self.recomputed = True


class _CorruptDocument(_Document):
    def __init__(self):
        super().__init__()
        self.removed = []

    def recompute(self) -> None:
        self.recomputed = True
        self.result.Shape = _Shape(valid=False)

    def removeObject(self, name: str) -> None:
        self.removed.append(name)


class _FreeCAD:
    Vector = _Vector

    def __init__(self):
        self.document = _Document()
        self.closed = []

    def newDocument(self, _name: str) -> _Document:
        return self.document

    def closeDocument(self, name: str) -> None:
        self.closed.append(name)


def test_script_is_deterministic_and_contains_only_fixed_entrypoint() -> None:
    design = _design()

    first = generate_freecad_script(design)
    second = generate_freecad_script(design)

    assert first == second
    assert "_execute_script_payload" in first
    assert design.to_canonical_json() in json.loads(first.split("(", 1)[1].rsplit(")", 1)[0])
    assert "exec(" not in first


def test_compile_builds_one_checked_solid_and_result_object() -> None:
    freecad = _FreeCAD()

    result = compile_freeform(_design(), freecad=freecad, part=_Part())

    assert result.design_digest == _design().digest
    assert result.created_document
    assert result.result_shape.Volume == 100
    assert len(result.curves) == 2
    assert freecad.document.recomputed
    assert not freecad.closed


def test_compile_supports_one_section_one_guide_nurbs_sweep() -> None:
    result = compile_freeform(_sweep_design(), freecad=_FreeCAD(), part=_Part())

    assert result.result_shape.isValid()
    assert len(result.curves) == 2


@pytest.mark.parametrize(
    "shape",
    (
        _Shape(valid=False),
        _Shape(closed=False),
        _Shape(volume=0),
        _Shape(volume=float("nan")),
    ),
)
def test_invalid_or_non_watertight_result_fails_and_closes_owned_document(shape) -> None:
    freecad = _FreeCAD()

    with pytest.raises(FreeformCompileError) as raised:
        compile_freeform(_design(), freecad=freecad, part=_Part(shape))

    assert raised.value.code is FreeformCompileErrorCode.SOLID_FAILURE
    assert freecad.closed == ["FakeDocument"]


def test_bad_input_and_partial_module_injection_fail_closed() -> None:
    with pytest.raises(FreeformCompileError) as bad_design:
        compile_freeform(object())
    assert bad_design.value.code is FreeformCompileErrorCode.INVALID_INPUT

    with pytest.raises(FreeformCompileError) as partial:
        compile_freeform(_design(), freecad=_FreeCAD())
    assert partial.value.code is FreeformCompileErrorCode.INVALID_INPUT


def test_failed_external_document_compile_removes_partial_result() -> None:
    document = _CorruptDocument()

    with pytest.raises(FreeformCompileError) as raised:
        compile_freeform(_design(), freecad=_FreeCAD(), part=_Part(), document=document)

    assert raised.value.code is FreeformCompileErrorCode.SOLID_FAILURE
    assert document.removed == ["FakeResult"]


def test_real_freecad_loft_when_integration_runtime_is_enabled(runtime_env) -> None:
    repo = Path(__file__).resolve().parents[1]
    generated = generate_freecad_script(_design())
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(repo)!r})\n"
        f"{generated}"
        "result = VIBECAD_FREEFORM_RESULT\n"
        "print(json.dumps({'solids': len(result.result_shape.Solids), "
        "'valid': result.result_shape.isValid(), 'closed': result.result_shape.isClosed()}))\n"
    )

    completed = subprocess.run(
        [runtime_env, "-c", code], capture_output=True, text=True, timeout=180, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "solids": 1,
        "valid": True,
        "closed": True,
    }
