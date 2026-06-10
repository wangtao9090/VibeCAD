# tests/test_integrity.py
"""_integrity 守卫的 owner 锚定快测（纯 fake，不碰 FreeCAD）。
终审系统性根因：装配守卫锚定"活动零件"而非"被操作零件"——本文件锁死锚定语义。"""
import sys
import types

import pytest

from vibecad.tools._integrity import assert_no_sealed_holes, assert_result_not_drifted

# ─── assert_no_sealed_holes：owner_names 过滤（终审 C-E）──────────────────────


class _PoisonTool:
    """毒丸：任何属性访问即炸——跨零件 Part::Cut 被探测就是 C-E 回归。"""

    def __getattr__(self, item):
        raise AssertionError(f"跨零件 Part::Cut 不应被探测（访问了 .{item}）")


class _ForeignCut:
    TypeId = "Part::Cut"
    Name = "ForeignHole"
    Tool = _PoisonTool()


class _Doc:
    Objects = [_ForeignCut()]


class _Shape:
    Solids = []


@pytest.fixture()
def _fake_freecad(monkeypatch):
    """注入 stub FreeCAD 模块（assert_no_sealed_holes 顶部 import）。"""
    fake = types.ModuleType("FreeCAD")
    fake.Vector = lambda *a: None
    monkeypatch.setitem(sys.modules, "FreeCAD", fake)


def test_sealed_holes_skips_foreign_cuts(_fake_freecad):
    """owner_names 不含该 Part::Cut → 跳过不探测（跨零件局部坐标系混用会误报）。"""
    assert_no_sealed_holes(_Doc(), _Shape(), owner_names={"MyHole"})


def test_sealed_holes_none_means_full_doc(_fake_freecad):
    """owner_names=None（单零件模式）→ 全文档遍历（R7 行为不变）——毒丸被触碰即证。"""
    with pytest.raises(AssertionError, match="跨零件"):
        assert_no_sealed_holes(_Doc(), _Shape(), owner_names=None)


def test_sealed_holes_owner_cut_probed(_fake_freecad):
    """owner_names 含该 Part::Cut → 必须被探测（不能因过滤把自己的孔也漏了）。"""
    with pytest.raises(AssertionError, match="跨零件"):
        assert_no_sealed_holes(_Doc(), _Shape(), owner_names={"ForeignHole"})


# ─── assert_result_not_drifted：part 透传（终审 C-D 同源）─────────────────────


class _SessionStub:
    def get_result_object(self, part=None):
        return types.SimpleNamespace(Name=f"R_{part}")


def test_result_not_drifted_part_passthrough():
    """part 必须透传到 get_result_object——锚错零件会假漂移/漏漂移。"""
    s = _SessionStub()
    assert_result_not_drifted(s, "R_盖板", part="盖板")  # owner 锚定一致 → 通过
    with pytest.raises(RuntimeError, match="漂移"):
        assert_result_not_drifted(s, "R_盖板", part="底板")  # 锚错零件 → 响亮


def test_result_not_drifted_default_single_mode():
    """part 缺省 None：单零件模式原行为（get_result_object() 无参语义）。"""
    s = _SessionStub()
    assert_result_not_drifted(s, "R_None")
