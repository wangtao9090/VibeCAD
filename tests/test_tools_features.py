"""features：参数校验快测 + @slow 真机事务回滚测试（更多 happy-path 后续任务补）。"""
import os
import subprocess

import pytest

from vibecad.runtime import status
from vibecad.tools import features

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")


class _NoopSession:
    pass


@pytest.mark.parametrize("kwargs,msg", [
    ({"face": "", "diameter": 6}, "face"),
    ({"face": "A", "diameter": 0}, "diameter"),
    ({"face": "A", "diameter": -2}, "diameter"),
    ({"face": "A", "diameter": float("nan")}, "diameter"),
    ({"face": "A", "diameter": 6, "depth": 0}, "depth"),
    ({"face": "A", "diameter": 6, "depth": float("nan")}, "depth"),
    ({"face": "A", "diameter": 6, "offset": [1]}, "offset"),
    ({"face": "A", "diameter": 6, "offset": ["a", "b"]}, "offset"),
])
def test_add_hole_validation(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        features.add_hole(_NoopSession(), **kwargs)


@pytest.mark.parametrize("fn,kwargs,msg", [
    (features.fillet_edges, {"edges": [], "radius": 2}, "edges"),
    (features.fillet_edges, {"edges": ["E1"], "radius": 0}, "radius"),
    (features.fillet_edges, {"edges": ["E1"], "radius": float("nan")}, "radius"),
    (features.fillet_edges, {"edges": "E1", "radius": 2}, "edges"),
    (features.chamfer_edges, {"edges": [], "size": 1}, "edges"),
    (features.chamfer_edges, {"edges": ["E1"], "size": -1}, "size"),
    (features.chamfer_edges, {"edges": ["E1"], "size": float("nan")}, "size"),
])
def test_edge_features_validation(fn, kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        fn(_NoopSession(), **kwargs)


def test_inplane_axes_orthonormal():
    e1, e2 = features._inplane_axes((0.0, 0.0, 1.0))
    assert abs(sum(a * b for a, b in zip(e1, e2, strict=True))) < 1e-9
    assert abs(sum(a * a for a in e1) - 1) < 1e-9 and abs(e1[2]) < 1e-9


def test_inplane_axes_arbitrary_normal():
    import math
    n = (1 / math.sqrt(3),) * 3
    e1, e2 = features._inplane_axes(n)
    for e in (e1, e2):
        assert abs(sum(a * b for a, b in zip(e, n, strict=True))) < 1e-9  # 与法向正交


@pytest.mark.slow
def test_failed_feature_rolls_back(runtime_env):
    """失败的特征操作必须随事务回滚：无残留对象、result object 不被劫持、会话可恢复。
    依赖 open_document 显式开启 UndoMode=1（headless 默认 0，abortTransaction 是 no-op）。"""
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from vibecad.engine import naming\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import features, modeling\n"
        + "s = Session(); modeling.new_document(s, 'RB')\n"
        + "assert s.doc.UndoMode == 1, f'UndoMode={s.doc.UndoMode}'\n"
        + "modeling.add_box(s, 10, 10, 10)\n"
        + "shape = s.get_result_shape()\n"
        + "top = max(range(len(shape.Faces)), key=lambda i: shape.Faces[i].CenterOfMass.z)\n"
        + "s.set_labels({'A': naming.face_fingerprint(shape.Faces[top])}, {})\n"
        + "n0 = len(s.doc.Objects); name0 = s.get_result_object().Name\n"
        + "raised = False\n"
        + "try:\n"
        + "    features.add_hole(s, 'A', 4, offset=[500, 500])\n"  # 孔落在零件之外 → 断言失败
        + "except RuntimeError:\n"
        + "    raised = True\n"
        + "assert raised, 'add_hole should raise when hole misses the part'\n"
        + "assert len(s.doc.Objects) == n0, [o.Name for o in s.doc.Objects]\n"  # 无残留
        + "assert s.get_result_object().Name == name0\n"                        # 不被劫持
        + "r = features.add_hole(s, 'A', 4)\n"                                  # 会话可恢复
        + "assert r['ok'] and r['volume'] < 1000.0, r\n"
        + "print('ROLLBACK_OK')\n"
    )
    p = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    assert "ROLLBACK_OK" in p.stdout
