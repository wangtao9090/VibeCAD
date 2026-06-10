# tests/test_naming.py
"""naming：指纹/容差匹配/过期错误/语义名。全部纯逻辑快测（fake 几何对象）。"""
import pytest

from vibecad.engine import naming
from vibecad.engine.naming import LabelExpiredError


class _Vec:
    def __init__(self, x, y, z): self.x, self.y, self.z = x, y, z


class Plane:  # 类名即 surface 判定依据（type(face.Surface).__name__）
    def __init__(self, axis=(0, 0, 1)): self.Axis = _Vec(*axis)


class Cylinder:
    def __init__(self, radius=6.0, axis=(0, 0, 1)):
        self.Radius = radius
        self.Axis = _Vec(*axis)


class FakeFace:
    def __init__(self, surface, area, center):
        self.Surface = surface
        self.Area = area
        self.CenterOfMass = _Vec(*center)


class Line:
    pass


class FakeEdge:
    def __init__(self, curve, length, mid):
        self.Curve = curve
        self.Length = length
        self.CenterOfMass = _Vec(*mid)


def _top(area=1200.0, center=(20, 15, 20)):
    return FakeFace(Plane((0, 0, 1)), area, center)


def test_face_fingerprint_fields():
    fp = naming.face_fingerprint(_top())
    assert fp["surface"] == "Plane" and fp["area"] == 1200.0
    assert fp["center"] == (20.0, 15.0, 20.0) and fp["axis"] == (0.0, 0.0, 1.0)


def test_face_fingerprint_cylinder_radius():
    fp = naming.face_fingerprint(FakeFace(Cylinder(6.0), 753.98, (20, 15, 10)))
    assert fp["surface"] == "Cylinder" and fp["radius"] == 6.0


def test_match_face_unique_hit():
    fp = naming.face_fingerprint(_top())
    faces = [FakeFace(Plane((0, 0, 1)), 600.0, (20, 15, 0)), _top()]
    assert naming.match_face(fp, faces) == 1


def test_match_face_axis_sign_insensitive():
    fp = naming.face_fingerprint(FakeFace(Plane((0, 0, -1)), 1200.0, (20, 15, 20)))
    assert naming.match_face(fp, [_top()]) == 0  # Plane.Axis 定向不稳，反号视为同面


def test_match_face_expired_when_area_changed():
    fp = naming.face_fingerprint(_top(area=1200.0))
    with pytest.raises(LabelExpiredError):  # 打孔后顶面面积变小 → 过期
        naming.match_face(fp, [_top(area=1086.9)])


def test_match_face_ambiguous_raises():
    fp = naming.face_fingerprint(_top())
    with pytest.raises(LabelExpiredError):
        naming.match_face(fp, [_top(), _top()])


def test_edge_fingerprint_and_match():
    fp = naming.edge_fingerprint(FakeEdge(Line(), 40.0, (20, 0, 0)))
    edges = [FakeEdge(Line(), 30.0, (0, 15, 0)), FakeEdge(Line(), 40.0, (20, 0, 0))]
    assert naming.match_edge(fp, edges) == 1
    with pytest.raises(LabelExpiredError):
        naming.match_edge(fp, edges[:1])


def test_semantic_name_top_bottom():
    bbox = (0, 0, 0, 40, 30, 20)
    assert naming.semantic_name(naming.face_fingerprint(_top()), bbox) == "顶面"
    bot = FakeFace(Plane((0, 0, -1)), 1200.0, (20, 15, 0))
    assert naming.semantic_name(naming.face_fingerprint(bot), bbox) == "底面"
    hole = FakeFace(Cylinder(), 750.0, (20, 15, 10))
    assert naming.semantic_name(naming.face_fingerprint(hole), bbox) is None


def test_face_summary_readable():
    s = naming.face_summary(naming.face_fingerprint(_top()), (0, 0, 0, 40, 30, 20))
    assert "顶面" in s and "平面" in s
    s2 = naming.face_summary(naming.face_fingerprint(FakeFace(Cylinder(6.0), 750.0, (20, 15, 10))),
                             (0, 0, 0, 40, 30, 20))
    assert "圆柱面" in s2 and "6" in s2


def test_face_labels_sequence():
    assert naming.face_labels(3) == ["A", "B", "C"]
    assert naming.face_labels(28)[26] == "AA"
