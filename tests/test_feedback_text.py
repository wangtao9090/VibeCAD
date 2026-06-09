import os
import subprocess

import pytest

from vibecad.feedback import text
from vibecad.runtime import status

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")


class _Vec:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class _BB:
    XLength = 10.0
    YLength = 8.0
    ZLength = 5.0


class _FakeShape:
    Volume = 400.0
    BoundBox = _BB()
    CenterOfMass = _Vec(5.0, 4.0, 2.5)
    Solids = [1]
    Shells = [1]

    def isValid(self):
        return True


def test_describe_shape_keys_and_values():
    d = text.describe_shape(_FakeShape())
    assert {"valid", "volume", "bbox", "center_of_mass", "solid_count", "shell_count"} <= d.keys()
    assert d["valid"] is True
    assert d["volume"] == 400.0
    assert d["bbox"] == {"x": 10.0, "y": 8.0, "z": 5.0}
    assert d["center_of_mass"] == [5.0, 4.0, 2.5]
    assert d["solid_count"] == 1 and d["shell_count"] == 1


def test_describe_shape_center_of_mass_is_json_list():
    import json
    d = text.describe_shape(_FakeShape())
    json.dumps(d)  # 不抛 = 可序列化（center_of_mass 是 list 非 Vector）
    assert isinstance(d["center_of_mass"], list)


def test_describe_shape_compound_falls_back_to_first_solid():
    # Part.Compound（布尔结果）无 CenterOfMass 属性：退到首个 Solid
    class _Solid:
        CenterOfMass = _Vec(1.0, 2.0, 3.0)

    class _Compound:
        Volume = 100.0
        BoundBox = _BB()
        Solids = [_Solid()]
        Shells = []

        def isValid(self):
            return True

    d = text.describe_shape(_Compound())
    assert d["center_of_mass"] == [1.0, 2.0, 3.0]
    assert d["solid_count"] == 1 and d["shell_count"] == 0


@pytest.mark.slow
def test_describe_shape_real_box(runtime_env):
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling\n"
        + "from vibecad.feedback.text import describe_shape\n"
        + "s = Session(); modeling.new_document(s, 'D')\n"
        + "r = modeling.add_box(s, 10, 10, 10)\n"
        + "d = describe_shape(s.get_object(r['name']).Shape)\n"
        + "assert d['valid'] is True\n"
        + "assert abs(d['volume'] - 1000.0) < 1e-3, d\n"
        + "assert d['solid_count'] == 1\n"
        + "print('DESCRIBE_OK')\n"
    )
    p = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    assert "DESCRIBE_OK" in p.stdout
