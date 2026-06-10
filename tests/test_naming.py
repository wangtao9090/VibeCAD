# tests/test_naming.py
"""naming：指纹/容差匹配/过期错误/语义名。全部纯逻辑快测（fake 几何对象）。"""
import pytest

from vibecad.engine import naming
from vibecad.engine.naming import LabelExpiredError
from vibecad.engine.session import Session


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


def test_match_face_radius_mismatch_expired():
    fp = naming.face_fingerprint(FakeFace(Cylinder(6.0), 750.0, (20, 15, 10)))
    with pytest.raises(LabelExpiredError):
        naming.match_face(fp, [FakeFace(Cylinder(5.0), 750.0, (20, 15, 10))])


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


# --- Session 注册表（fake shape，不碰 FreeCAD）---


class _FakeShape:
    def __init__(self, faces=(), edges=()):
        self.Faces = list(faces)
        self.Edges = list(edges)


def _session_with_shape(monkeypatch, shape):
    s = Session()
    monkeypatch.setattr(Session, "get_result_shape", lambda self: shape, raising=False)
    return s


def test_resolve_face_roundtrip(monkeypatch):
    top = _top()
    other = _top(area=600.0, center=(1, 1, 1))
    s = _session_with_shape(monkeypatch, _FakeShape(faces=[other, top]))
    s.set_labels({"A": naming.face_fingerprint(top)}, {})
    assert s.resolve_face("A") == 1


def test_resolve_face_unknown_label(monkeypatch):
    s = _session_with_shape(monkeypatch, _FakeShape())
    s.set_labels({}, {})
    with pytest.raises(LabelExpiredError):
        s.resolve_face("Z")


def test_resolve_without_labels_raises(monkeypatch):
    s = _session_with_shape(monkeypatch, _FakeShape())
    with pytest.raises(LabelExpiredError):
        s.resolve_face("A")


def test_resolve_edge_roundtrip(monkeypatch):
    e = FakeEdge(Line(), 40.0, (20, 0, 0))
    s = _session_with_shape(monkeypatch, _FakeShape(edges=[e]))
    s.set_labels({}, {"E1": naming.edge_fingerprint(e)})
    assert s.resolve_edge("E1") == 0


def test_unshown_label_rejected_until_shown(monkeypatch):
    """shown gate：注册表全量注册，但只有标签表实际展示过的键可被指认——
    AI 没看过任何边标注图就编造 'E1' 必须响亮拒绝（终审 CRITICAL-1）。"""
    top = _top()
    e = FakeEdge(Line(), 40.0, (20, 0, 0))
    s = _session_with_shape(monkeypatch, _FakeShape(faces=[top], edges=[e]))
    freg = {"A": naming.face_fingerprint(top)}
    ereg = {"E1": naming.edge_fingerprint(e)}
    s.set_labels(freg, ereg, shown={"A"})  # faces 标注：表里只有面条目
    assert s.resolve_face("A") == 0
    with pytest.raises(LabelExpiredError, match="尚未"):
        s.resolve_edge("E1")  # 边标注图从没展示过——编造
    s.set_labels(freg, ereg, shown={"E1"})  # 同注册表（几何没变）→ shown 累积
    assert s.resolve_face("A") == 0 and s.resolve_edge("E1") == 0


def test_shown_resets_when_registry_changes(monkeypatch):
    """注册表变化（几何变了）→ 旧 shown 重置，只认本次展示的键。"""
    top = _top()
    e = FakeEdge(Line(), 40.0, (20, 0, 0))
    s = _session_with_shape(monkeypatch, _FakeShape(faces=[top], edges=[e]))
    s.set_labels({"A": naming.face_fingerprint(top)}, {}, shown={"A"})
    assert s.resolve_face("A") == 0
    s.set_labels({"A": naming.face_fingerprint(_top(area=999.0))},
                 {"E1": naming.edge_fingerprint(e)}, shown={"E1"})  # faces 注册表变了
    with pytest.raises(LabelExpiredError, match="尚未"):
        s.resolve_face("A")  # 旧 shown 不得跨几何残留
    assert s.resolve_edge("E1") == 0


def test_set_labels_shown_none_shows_all(monkeypatch):
    """shown=None 向后兼容：视为全部展示（内部/测试用法）。"""
    top = _top()
    e = FakeEdge(Line(), 40.0, (20, 0, 0))
    s = _session_with_shape(monkeypatch, _FakeShape(faces=[top], edges=[e]))
    s.set_labels({"A": naming.face_fingerprint(top)}, {"E1": naming.edge_fingerprint(e)})
    assert s.resolve_face("A") == 0 and s.resolve_edge("E1") == 0


def test_match_face_tolerance_boundaries():
    """指纹容差边界：容差公式 max(1e-3, 1e-3*|area|)，center 绝对容差 1e-3。

    对 fp_area=1200，area 容差 = max(1e-3, 1.2) = 1.2：
      - 差 1.0 < 1.2 → 命中；差 2.0 > 1.2 → 过期
    center 绝对容差 = 1e-3：
      - 差 0.0009 < 0.001 → 命中；差 0.002 > 0.001 → 过期
    双候选 center 相同 → 双命中 → 过期（歧义）
    """
    fp = naming.face_fingerprint(_top(area=1200.0, center=(20.0, 15.0, 20.0)))

    # area 差 1.0 < 容差 1.2：匹配
    assert naming.match_face(fp, [_top(area=1201.0, center=(20.0, 15.0, 20.0))]) == 0

    # area 差 2.0 > 容差 1.2：过期
    with pytest.raises(LabelExpiredError):
        naming.match_face(fp, [_top(area=1202.0, center=(20.0, 15.0, 20.0))])

    # center 差 0.0009 < 绝对容差 0.001：匹配
    assert naming.match_face(fp, [_top(area=1200.0, center=(20.0009, 15.0, 20.0))]) == 0

    # center 差 0.002 > 绝对容差 0.001：过期
    with pytest.raises(LabelExpiredError):
        naming.match_face(fp, [_top(area=1200.0, center=(20.002, 15.0, 20.0))])

    # 两个候选 center 均在容差内 → 双命中 → 过期（歧义）
    cand_a = _top(area=1200.0, center=(20.0009, 15.0, 20.0))
    cand_b = _top(area=1200.0, center=(20.0, 15.0009, 20.0))
    with pytest.raises(LabelExpiredError):
        naming.match_face(fp, [cand_a, cand_b])


def test_labels_cleared_on_close_document(monkeypatch):
    """关文档必须清空标签快照（真实路径：close_document 先清 _labels，
    _doc is None 时早退不碰 FreeCAD，故可在 dev venv 直接调用）。"""
    top = _top()
    s = _session_with_shape(monkeypatch, _FakeShape(faces=[top]))
    s.set_labels({"A": naming.face_fingerprint(top)}, {})
    assert s.resolve_face("A") == 0  # 清理前可解析
    s.close_document()
    assert s._labels is None
    with pytest.raises(LabelExpiredError):
        s.resolve_face("A")
