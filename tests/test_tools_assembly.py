# tests/test_tools_assembly.py
"""assembly：校验矩阵 + align 位姿数学纯函数。快测。"""
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
