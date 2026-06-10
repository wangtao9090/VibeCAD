# tests/test_tools_modify.py
"""modify：白名单校验矩阵 + list_parameters。快测（fake 对象，不碰 FreeCAD）。"""
import pytest

from vibecad.tools import modify


class _NoopSession:
    pass


@pytest.mark.parametrize("kwargs,msg", [
    ({"name": "", "parameter": "length", "value": 45}, "name"),
    ({"name": "Box", "parameter": "", "value": 45}, "parameter"),
    ({"name": "Box", "parameter": "length", "value": 0}, "value"),
    ({"name": "Box", "parameter": "length", "value": -5}, "value"),
    ({"name": "Box", "parameter": "length", "value": float("nan")}, "value"),
    ({"name": "Box", "parameter": "length", "value": float("inf")}, "value"),
])
def test_modify_part_validation(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        modify.modify_part(_NoopSession(), **kwargs)


class _FakeObj:
    def __init__(self, name, type_id, **attrs):
        self.Name = name
        self.TypeId = type_id
        for k, v in attrs.items():
            setattr(self, k, v)


class _FakeDoc:
    def __init__(self, objects):
        self.Objects = list(objects)


def test_list_parameters_whitelist_only():
    """list_parameters 统一新形态：{零件名: {对象名: {参数名: 值}}}。
    单零件模式（session=None）：零件键固定为 "Part1"。"""
    doc = _FakeDoc([
        _FakeObj("Box", "Part::Box", Length=40.0, Width=30.0, Height=20.0),
        _FakeObj("HoleTool", "Part::Cylinder", Radius=4.0, Height=42.0),
        _FakeObj("Cut", "Part::Cut"),  # 非白名单类型 → 不出现
        _FakeObj("Fillet", "Part::Fillet", Edges=[(3, 2.0, 2.0), (7, 2.0, 2.0)]),
    ])
    out = modify.list_parameters(doc)
    # 新形态：{零件键: {对象: {参数}}}
    assert out == {
        "Part1": {
            "Box": {"length": 40.0, "width": 30.0, "height": 20.0},
            "HoleTool": {"radius": 4.0, "height": 42.0},
            "Fillet": {"radius": 2.0},
        }
    }


def test_list_parameters_empty_doc():
    """空文档：返回 {"Part1": {}}（统一新形态，空则空 dict 对象层）。"""
    out = modify.list_parameters(_FakeDoc([]))
    assert out == {"Part1": {}}


def test_list_parameters_assembly_mode():
    """装配模式：session._parts 非空时，按零件分组。"""
    class _FakeSession:
        _parts = {
            "底板": {"objects": {"Box"}, "container": None},
            "盖板": {"objects": {"Cyl"}, "container": None},
        }

    doc = _FakeDoc([
        _FakeObj("Box", "Part::Box", Length=60.0, Width=40.0, Height=10.0),
        _FakeObj("Cyl", "Part::Cylinder", Radius=5.0, Height=20.0),
    ])
    out = modify.list_parameters(doc, session=_FakeSession())
    assert "底板" in out and "盖板" in out
    assert "Box" in out["底板"]
    assert out["底板"]["Box"]["length"] == 60.0
    assert "Cyl" in out["盖板"]
    assert out["盖板"]["Cyl"]["radius"] == 5.0
