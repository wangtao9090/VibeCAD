# tests/test_tools_assembly.py
"""assembly 工具 + integrity 装配语义：校验矩阵 + align 位姿数学纯函数 + 分流断言。快测。"""
import sys

import pytest

from vibecad.tools import assembly


class _NoopSession:
    pass


@pytest.mark.parametrize("kwargs,msg", [
    ({"part": "", "position": [0, 0, 0]}, "part"),
    ({"part": "盖板"}, "至少"),
    ({"part": "盖板", "position": [0, 0]}, "position"),
    ({"part": "盖板", "rotation_axis": "w", "angle": 90}, "axis"),
    ({"part": "盖板", "rotation_axis": "z", "angle": 0}, "angle"),
])
def test_place_part_validation(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        assembly.place_part(_NoopSession(), **kwargs)


@pytest.mark.parametrize("kwargs,msg", [
    ({"moving_part": "", "moving_face": "A",
      "target_part": "底板", "target_face": "F"}, "moving_part"),
    ({"moving_part": "盖板", "moving_face": "",
      "target_part": "底板", "target_face": "F"}, "moving_face"),
    ({"moving_part": "盖板", "moving_face": "A",
      "target_part": "盖板", "target_face": "F"}, "不同"),
    ({"moving_part": "盖板", "moving_face": "A",
      "target_part": "底板", "target_face": "F", "gap": float("nan")}, "gap"),
])
def test_align_parts_validation(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        assembly.align_parts(_NoopSession(), **kwargs)


def test_align_placement_math():
    """纯数学：盖板底面(法向 -Z，面心 (30,20,100)) 贴底板顶面(法向 +Z，面心 (30,20,10))。
    期望：旋转为恒等（-Z 已对 -(+Z)），平移 z: 100→10。"""
    pos, rot_axis_angle = assembly._align_placement(
        moving_normal=(0, 0, -1), moving_center=(30, 20, 100),
        target_normal=(0, 0, 1), target_center=(30, 20, 10),
        target_e1=(1, 0, 0), target_e2=(0, 1, 0), offset=(0, 0), gap=0.0)
    assert pos == pytest.approx((0, 0, -90))          # 平移量
    assert rot_axis_angle[1] == pytest.approx(0.0)    # 无旋转


def test_align_placement_math_flip():
    """盖板底面法向 +Z（倒扣着）→ 需翻转 180°。"""
    _pos, (axis, angle) = assembly._align_placement(
        moving_normal=(0, 0, 1), moving_center=(0, 0, 0),
        target_normal=(0, 0, 1), target_center=(0, 0, 10),
        target_e1=(1, 0, 0), target_e2=(0, 1, 0), offset=(0, 0), gap=0.0)
    assert abs(angle) == pytest.approx(180.0)


def test_align_placement_math_offset():
    """offset 向量在 target 面内 e1/e2 方向上正确偏移锚点。"""
    # target 面心 (0,0,0)，法向 +Z，offset=(5,3) → 锚点 = (5,3,0)
    # moving 面心 (0,0,50)，法向 -Z（无需旋转）
    # 平移 = 锚点 - R(moving_center) = (5,3,0) - R(0,0,50)
    # R=恒等（-Z 对 -(+Z) 恒等） → 平移 = (5,3,-50)
    pos, (axis, angle) = assembly._align_placement(
        moving_normal=(0, 0, -1), moving_center=(0, 0, 50),
        target_normal=(0, 0, 1), target_center=(0, 0, 0),
        target_e1=(1, 0, 0), target_e2=(0, 1, 0), offset=(5, 3), gap=0.0)
    assert pos == pytest.approx((5, 3, -50))
    assert angle == pytest.approx(0.0)


def test_align_placement_math_gap():
    """gap > 0 时 anchor 沿 target 法向外移 gap，产生精确间隙。"""
    # target 面心 (0,0,0)，法向 +Z，gap=2.0
    # anchor = (0,0,0) + (0,0,1)*2 = (0,0,2)
    # moving 面心 (0,0,50)，法向 -Z（恒等旋转）
    # 平移 = (0,0,2) - (0,0,50) = (0,0,-48)
    pos, (axis, angle) = assembly._align_placement(
        moving_normal=(0, 0, -1), moving_center=(0, 0, 50),
        target_normal=(0, 0, 1), target_center=(0, 0, 0),
        target_e1=(1, 0, 0), target_e2=(0, 1, 0), offset=(0, 0), gap=2.0)
    assert pos == pytest.approx((0, 0, -48))
    assert angle == pytest.approx(0.0)


def test_align_placement_arbitrary_normal():
    """法向为 +X 的面：moving 法向 -X 贴上，旋转恒等，平移沿 X。"""
    # moving 法向 -X，面心 (50,0,0)；target 法向 +X，面心 (10,0,0)
    # -X 对 -(+X) = -X 对 -X → 恒等旋转
    # anchor = (10,0,0)，平移 = (10,0,0)-(50,0,0) = (-40,0,0)
    pos, (axis, angle) = assembly._align_placement(
        moving_normal=(-1, 0, 0), moving_center=(50, 0, 0),
        target_normal=(1, 0, 0), target_center=(10, 0, 0),
        target_e1=(0, 1, 0), target_e2=(0, 0, 1), offset=(0, 0), gap=0.0)
    assert pos == pytest.approx((-40, 0, 0))
    assert angle == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Task 3：assert_solid_integrity 分流逻辑快测（纯 fake，不碰 FreeCAD）
# ---------------------------------------------------------------------------

from vibecad.tools._integrity import assert_solid_integrity  # noqa: E402


class _FakeSolid:
    """模拟有 N 个 solid 的 shape。"""
    def __init__(self, n_solids: int):
        self.Solids = [object() for _ in range(n_solids)]


class _FakeSession:
    """最小 fake session：控制 _parts（含 objects 集合）、活动零件和 get_result_shape。"""
    def __init__(self, parts: dict, active=None, objects=None):
        # parts: {name: shape}；objects: {name: set}（缺省非空哨兵——有几何）
        self._parts = {k: {"objects": (objects or {}).get(k, {"_x"})} for k in parts}
        self._shapes = parts
        self._active_part = active if active is not None else next(iter(parts), None)

    def get_result_shape(self, part_name=None):
        return self._shapes[part_name or self._active_part]


def test_assert_solid_integrity_single_mode_pass():
    """单零件模式（_parts 空）：shape 1 solid → 通过。"""
    session = _FakeSession({})
    assert_solid_integrity(session, _FakeSolid(1), "test_op")


def test_assert_solid_integrity_single_mode_fail():
    """单零件模式（_parts 空）：shape 2 solids → RuntimeError。"""
    session = _FakeSession({})
    with pytest.raises(RuntimeError, match="切成 2 块"):
        assert_solid_integrity(session, _FakeSolid(2), "test_op")


def test_assert_solid_integrity_assembly_checks_passed_shape():
    """装配模式（终审 C-A）：传入 shape 承载新几何，**必须直接断言**——差集归属
    尚未发生，回取 owner 旧 shape 会漏检飞地 pad（切成 2 块仍 ok）。"""
    session = _FakeSession({"底板": _FakeSolid(1), "盖板": _FakeSolid(1)}, active="底板")
    with pytest.raises(RuntimeError, match="底板.*切成 2 块"):
        assert_solid_integrity(session, _FakeSolid(2), "test_op")


def test_assert_solid_integrity_assembly_mode_all_pass():
    """装配模式：传入 shape 1 solid + 其余零件各 1 solid → 通过。"""
    session = _FakeSession({"底板": _FakeSolid(1), "盖板": _FakeSolid(1)}, active="底板")
    assert_solid_integrity(session, _FakeSolid(1), "test_op")


def test_assert_solid_integrity_assembly_mode_other_broken():
    """装配模式：其余零件 2 solids → RuntimeError 含该零件名。"""
    session = _FakeSession({"底板": _FakeSolid(1), "盖板": _FakeSolid(2)}, active="底板")
    with pytest.raises(RuntimeError, match="盖板"):
        assert_solid_integrity(session, _FakeSolid(1), "test_op")


def test_assert_solid_integrity_assembly_part_anchors_owner():
    """part= 显式锚定 owner（非活动零件）：owner 回取被跳过（新 shape 已传入），
    活动零件照常回取断言。"""
    session = _FakeSession({"底板": _FakeSolid(2), "盖板": _FakeSolid(1)}, active="盖板")
    # owner=底板：传入的新 shape 1 solid 通过；底板旧 shape（2 块）不回取——不误拒
    assert_solid_integrity(session, _FakeSolid(1), "test_op", part="底板")
    # 对照：不传 part（锚活动零件盖板）时底板会被回取 → 拒
    with pytest.raises(RuntimeError, match="底板"):
        assert_solid_integrity(session, _FakeSolid(1), "test_op")


def test_assert_solid_integrity_assembly_skips_empty_parts():
    """装配模式：objects 为空的零件（new_part 后未建几何）跳过回取——
    空零件不应让其它零件的操作误拒/崩溃（终审 C-A：无基体 pad 不可用）。"""
    session = _FakeSession({"臂": _FakeSolid(1), "空件": None},
                           active="臂", objects={"空件": set()})
    assert_solid_integrity(session, _FakeSolid(1), "test_op")  # 不碰 None shape


def test_assert_no_interference_single_part_mode():
    """单零件模式（_parts 空）：assert_no_interference 直接返回空列表。"""
    from vibecad.tools.assembly import assert_no_interference
    session = _FakeSession({})  # _parts 空
    result = assert_no_interference(session)
    assert result == []


def _fake_part_module(monkeypatch):
    """注入 stub Part 模块（assert_no_interference 顶部 import Part）。"""
    import types
    fake = types.ModuleType("Part")
    fake.OCCError = type("OCCError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "Part", fake)
    return fake


def test_assert_no_interference_empty_part_noted(monkeypatch):
    """终审 C-B："绝不静默"——空零件（objects 空）跳过配对但必须显式注明
    （{"parts": [X], "volume": None, "note": ...}），绝不返回静默 []。"""
    from vibecad.tools.assembly import assert_no_interference
    _fake_part_module(monkeypatch)
    session = _FakeSession({"A": _FakeSolid(1), "B": None}, objects={"B": set()})
    result = assert_no_interference(session, allow=False)
    assert result == [{"parts": ["B"], "volume": None,
                       "note": "零件 B 无几何，干涉未检查"}]


def test_assert_no_interference_occ_error_raises(monkeypatch):
    """终审 C-B：OCC 布尔崩溃必须转 RuntimeError"干涉检查无法完成"——
    修复前 except Exception → vol=0.0 把真实重叠静默放行。"""
    from vibecad.tools.assembly import assert_no_interference
    fake = _fake_part_module(monkeypatch)

    class _BoomShape:
        def transformed(self, m):
            return self

        def common(self, other):
            raise fake.OCCError("boolean failed")

    session = _FakeSession({"A": _BoomShape(), "B": _BoomShape()})
    # fake 容器（transformed 消费 Placement.toMatrix）
    import types as _t
    for info in session._parts.values():
        info["container"] = _t.SimpleNamespace(
            Placement=_t.SimpleNamespace(toMatrix=lambda: "M"))
    with pytest.raises(RuntimeError, match="干涉检查无法完成"):
        assert_no_interference(session)
