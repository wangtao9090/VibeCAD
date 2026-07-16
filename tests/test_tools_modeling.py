import contextlib
import os
import subprocess
from types import SimpleNamespace

import pytest

from vibecad.runtime import status
from vibecad.tools import modeling

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")


class MockSession:
    def __init__(self):
        self.opened = None
        self.doc = None

    def is_dirty(self):
        return False

    def open_document(self, name):
        self.opened = name
        self.doc = SimpleNamespace(Name=name)
        return self.doc


def test_new_document_returns_ok():
    s = MockSession()
    assert modeling.new_document(s, "MyPart") == {"ok": True, "name": "MyPart"}
    assert s.opened == "MyPart"


def test_new_document_rejects_empty():
    with pytest.raises(ValueError):
        modeling.new_document(MockSession(), "")


def test_new_document_protects_unsaved_session():
    s = MockSession()
    s.doc = object()
    s.is_dirty = lambda: True
    with pytest.raises(ValueError, match="未保存"):
        modeling.new_document(s, "Next")
    assert modeling.new_document(s, "Next", discard_unsaved=True)["ok"] is True


def test_add_box_rejects_zero():
    with pytest.raises(ValueError, match="length"):
        modeling.add_box(MockSession(), 0, 10, 10)


def test_add_box_rejects_negative_width():
    with pytest.raises(ValueError, match="width"):
        modeling.add_box(MockSession(), 10, -1, 10)


def test_add_cylinder_rejects_zero_radius():
    with pytest.raises(ValueError, match="radius"):
        modeling.add_cylinder(MockSession(), 0, 10)


def test_boolean_cut_rejects_empty_base():
    with pytest.raises(ValueError, match="base_name"):
        modeling.boolean_cut(MockSession(), "", "Cyl")


def test_boolean_cut_rejects_empty_tool():
    with pytest.raises(ValueError, match="tool_name"):
        modeling.boolean_cut(MockSession(), "Box", "")


@pytest.mark.parametrize("func", [modeling.boolean_cut, modeling.boolean_fuse,
                                   modeling.boolean_common])
def test_boolean_rejects_same_object(func):
    with pytest.raises(ValueError, match="同一对象"):
        func(MockSession(), "Box", "Box")


class _OwnerSession:
    _parts = {"A": {}, "B": {}}

    def owner_of(self, name):
        return {"Box": "A", "Cylinder": "B"}.get(name)


@pytest.mark.parametrize("func", [modeling.boolean_cut, modeling.boolean_fuse,
                                   modeling.boolean_common])
def test_boolean_rejects_cross_part_operands(func):
    with pytest.raises(ValueError, match="同一零件"):
        func(_OwnerSession(), "Box", "Cylinder")


def test_boolean_fuse_volume_guard_rejects_lost_material(monkeypatch):
    """有效单 solid 仍可能因内核异常丢料；并集必须至少容纳较大输入。"""
    class Shape:
        def __init__(self, volume):
            self.Volume = volume
            self.Solids = [object()]

    class Obj:
        def __init__(self, name, volume):
            self.Name = name
            self.Shape = Shape(volume)

    class Doc:
        def __init__(self):
            self.result = Obj("Fuse", 90)

        def recompute(self):
            return None

        def addObject(self, _type, _label):
            return self.result

    class Session:
        _parts = {}
        _labels = None

        def __init__(self):
            self.doc = Doc()
            self.objects = {"Base": Obj("Base", 100), "Tool": Obj("Tool", 50)}

        def get_object(self, name):
            return self.objects[name]

        def assert_valid_solid(self, _shape):
            return None

        def set_result_object(self, _obj, part=None):
            return None

        @contextlib.contextmanager
        def _transaction(self, _label, part=None):
            yield

    monkeypatch.setattr(modeling, "assert_solid_integrity", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="丢失材料"):
        modeling.boolean_fuse(Session(), "Base", "Tool")


def test_add_box_rejects_bad_position():
    with pytest.raises(ValueError, match="position"):
        modeling.add_box(MockSession(), 10, 10, 10, position=(1, 2))


def test_add_cylinder_rejects_bad_axis():
    with pytest.raises(ValueError, match="axis"):
        modeling.add_cylinder(MockSession(), 5, 10, axis="w")


def test_add_cylinder_rejects_bad_position():
    with pytest.raises(ValueError, match="position"):
        modeling.add_cylinder(MockSession(), 5, 10, position="nope")


def test_add_box_rejects_nan_position():
    with pytest.raises(ValueError, match="position"):
        modeling.add_box(MockSession(), 10, 10, 10, position=(float("nan"), 0, 0))


def test_add_box_rejects_empty_position():
    with pytest.raises(ValueError, match="position"):
        modeling.add_box(MockSession(), 10, 10, 10, position=[])


@pytest.mark.slow
def test_add_box_real(runtime_env):
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling\n"
        + "s = Session(); modeling.new_document(s, 'T')\n"
        + "r = modeling.add_box(s, 10, 20, 30)\n"
        + "assert r['ok'] and abs(r['volume'] - 6000.0) < 1e-3, r\n"
        + "assert s.get_object(r['name']).Length == 10\n"
        + "print('BOX_OK')\n"
    )
    p = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    assert "BOX_OK" in p.stdout


@pytest.mark.slow
def test_boolean_cut_real(runtime_env):
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling\n"
        + "s = Session(); modeling.new_document(s, 'C')\n"
        + "b = modeling.add_box(s, 10, 10, 10)\n"
        + "c = modeling.add_cylinder(s, 3, 15)\n"
        + "r = modeling.boolean_cut(s, b['name'], c['name'])\n"
        + "assert r['ok'] and 0 < r['volume'] < 1000.0, r\n"
        + "print('CUT_OK')\n"
    )
    p = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    assert "CUT_OK" in p.stdout


@pytest.mark.slow
def test_boolean_cut_noop_raises(runtime_env):
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling\n"
        + "s = Session(); modeling.new_document(s, 'Noop')\n"
        + "b = modeling.add_box(s, 10, 10, 10)\n"           # 原点 0..10
        + "c = modeling.add_cylinder(s, 2, 5, position=(1000, 1000, 1000))\n"  # 远离 base
        + "raised = False\n"
        + "try:\n"
        + "    modeling.boolean_cut(s, b['name'], c['name'])\n"
        + "except RuntimeError:\n"
        + "    raised = True\n"
        + "assert raised, 'boolean_cut should raise when tool does not intersect base'\n"
        + "print('NOOP_RAISES_OK')\n"
    )
    p = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    assert "NOOP_RAISES_OK" in p.stdout


@pytest.mark.slow
def test_cylinder_axis_x_orientation(runtime_env):
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling\n"
        + "s = Session(); modeling.new_document(s, 'AxisX')\n"
        + "r = modeling.add_cylinder(s, 2, 30, axis='x')\n"
        + "bb = s.get_object(r['name']).Shape.BoundBox\n"
        + "assert abs(bb.XLength - 30) < 1e-3, f'XLength={bb.XLength}'\n"
        + "assert abs(bb.YLength - 4) < 1e-3, f'YLength={bb.YLength}'\n"
        + "assert abs(bb.ZLength - 4) < 1e-3, f'ZLength={bb.ZLength}'\n"
        + "print('AXIS_X_OK')\n"
    )
    p = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    assert "AXIS_X_OK" in p.stdout


@pytest.mark.slow
def test_positioned_centered_through_hole(runtime_env):
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "import math\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling\n"
        + "s = Session(); modeling.new_document(s, 'Pos')\n"
        + "b = modeling.add_box(s, 20, 20, 20)\n"            # 原点 0..20
        + "c = modeling.add_cylinder(s, 4, 30, position=(10, 10, -5), axis='z')\n"  # 居中、贯穿
        + "cut = modeling.boolean_cut(s, b['name'], c['name'])\n"
        + "expected = 8000 - math.pi * 16 * 20\n"            # 整根圆柱被挖掉 ≈ 6994.7
        + "assert abs(cut['volume'] - expected) < 30, (cut['volume'], expected)\n"
        + "print('POS_OK')\n"
    )
    p = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    assert "POS_OK" in p.stdout


@pytest.mark.slow
def test_boolean_fuse_common_and_explicit_root_real(runtime_env):
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling\n"
        + "s = Session(); modeling.new_document(s, 'Combine')\n"
        + "a = modeling.add_box(s, 10, 10, 10)\n"
        + "b = modeling.add_box(s, 10, 10, 10, position=(5,0,0))\n"
        + "common = modeling.boolean_common(s, a['name'], b['name'])\n"
        + "assert abs(common['volume'] - 500) < 1e-6, common\n"
        + "c = modeling.add_box(s, 10, 10, 10, position=(5,0,0))\n"
        + "fuse = modeling.boolean_fuse(s, a['name'], c['name'])\n"
        + "assert abs(fuse['volume'] - 1500) < 1e-6, fuse\n"
        + "assert s.get_result_object().Name == fuse['name']\n"
        + "print('COMBINE_OK')\n"
    )
    p = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    assert "COMBINE_OK" in p.stdout
