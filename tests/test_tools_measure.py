from types import SimpleNamespace

import pytest

from vibecad.tools import measure as measure_tools


class _Vec:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z

    def dot(self, other):
        return self.x * other.x + self.y * other.y + self.z * other.z

    def normalize(self):
        return None


class _Edge:
    Length = 4.0


class _Shape:
    Volume = 24.0
    Area = 52.0
    CenterOfMass = _Vec(1, 2, 3)
    BoundBox = SimpleNamespace(
        XMin=0, YMin=0, ZMin=0, XMax=2, YMax=3, ZMax=4,
        XLength=2, YLength=3, ZLength=4)
    Solids = [object()]
    Faces = [object()] * 6
    Edges = [_Edge()] * 12


class _SummarySession:
    _parts = {}

    def _require_doc(self):
        return None

    def get_assembly_shape(self):
        return _Shape()


def test_measure_summary_reports_units_and_geometry():
    out = measure_tools.measure(_SummarySession())
    assert out["volume_mm3"] == 24.0
    assert out["bbox_mm"]["size"] == [2.0, 3.0, 4.0]
    assert out["units"]["volume"] == "mm^3"


def test_measure_summary_compound_uses_volume_weighted_center():
    class Solid:
        def __init__(self, volume, center):
            self.Volume = volume
            self.CenterOfMass = _Vec(*center)

    class Compound(_Shape):
        CenterOfMass = None
        Solids = [Solid(1, (0, 0, 0)), Solid(3, (4, 0, 0))]

    class Session(_SummarySession):
        def get_assembly_shape(self):
            return Compound()

    out = measure_tools.measure(Session())
    assert out["center_mm"] == [3.0, 0.0, 0.0]


class _DistanceShape:
    def distToShape(self, other):
        return 5.0, [(_Vec(0, 0, 0), _Vec(0, 0, 5))], []


def test_distance_serializes_closest_points(monkeypatch):
    monkeypatch.setattr(
        measure_tools, "_entity",
        lambda session, ref, entity, part: (_DistanceShape(), ref))
    session = _SummarySession()
    out = measure_tools.measure(session, kind="distance", first="A", second="B")
    assert out["distance_mm"] == 5.0
    assert out["closest_points_mm"] == [[[0.0, 0.0, 0.0], [0.0, 0.0, 5.0]]]


class Plane:
    pass


class _Face(_DistanceShape):
    Surface = Plane()
    ParameterRange = (0.0, 1.0, 0.0, 1.0)

    def __init__(self, normal, center):
        self._normal = normal
        self.CenterOfMass = center

    def normalAt(self, u, v):
        return self._normal


def test_angle_and_thickness_require_explicit_parallel_faces(monkeypatch):
    faces = {
        "A": _Face(_Vec(0, 0, 1), _Vec(0, 0, 0)),
        "B": _Face(_Vec(0, 0, -1), _Vec(20, 0, 5)),
    }
    monkeypatch.setattr(
        measure_tools, "_entity",
        lambda session, ref, entity, part: (faces[ref], ref))
    session = _SummarySession()
    angle = measure_tools.measure(
        session, kind="angle", entity="face", first="A", second="B")
    assert angle["normal_angle_deg"] == pytest.approx(180.0)
    assert angle["plane_angle_deg"] == pytest.approx(0.0)
    thickness = measure_tools.measure(
        session, kind="thickness", entity="face", first="A", second="B")
    assert thickness["thickness_mm"] == 5.0


def test_measure_rejects_bad_mode_before_geometry_access():
    with pytest.raises(ValueError, match="kind"):
        measure_tools.measure(_SummarySession(), kind="guess")
