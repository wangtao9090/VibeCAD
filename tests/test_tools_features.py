"""features：参数校验快测 + @slow 真机事务回滚测试（更多 happy-path 后续任务补）。"""
import os
import subprocess

import pytest

from vibecad.runtime import status
from vibecad.tools import features

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")


def _run_in_env(env_python: str, body: str, timeout: int = 180) -> str:
    """conda runtime env python 子进程跑代码片段（_PREP + src 注入；非零退出即 fail）。"""
    code = status._PREP + f"import sys; sys.path.insert(0, {_SRC!r})\n" + body
    p = subprocess.run([env_python, "-c", code], capture_output=True, text=True, timeout=timeout)
    assert p.returncode == 0, p.stderr
    return p.stdout


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
    out = _run_in_env(runtime_env, (
        "from vibecad.engine import naming\n"
        "from vibecad.engine.session import Session\n"
        "from vibecad.tools import features, modeling\n"
        "s = Session(); modeling.new_document(s, 'RB')\n"
        "assert s.doc.UndoMode == 1, f'UndoMode={s.doc.UndoMode}'\n"
        "modeling.add_box(s, 10, 10, 10)\n"
        "shape = s.get_result_shape()\n"
        "top = max(range(len(shape.Faces)), key=lambda i: shape.Faces[i].CenterOfMass.z)\n"
        "s.set_labels({'A': naming.face_fingerprint(shape.Faces[top])}, {})\n"
        "n0 = len(s.doc.Objects); name0 = s.get_result_object().Name\n"
        "raised = False\n"
        "try:\n"
        "    features.add_hole(s, 'A', 4, offset=[500, 500])\n"  # 孔落在零件之外 → 断言失败
        "except RuntimeError:\n"
        "    raised = True\n"
        "assert raised, 'add_hole should raise when hole misses the part'\n"
        "assert len(s.doc.Objects) == n0, [o.Name for o in s.doc.Objects]\n"  # 无残留
        "assert s.get_result_object().Name == name0\n"                        # 不被劫持
        "r = features.add_hole(s, 'A', 4)\n"                                  # 会话可恢复
        "assert r['ok'] and r['volume'] < 1000.0, r\n"
        "print('ROLLBACK_OK')\n"
    ))
    assert "ROLLBACK_OK" in out


@pytest.mark.slow
def test_annotate_then_add_hole_by_label(runtime_env):
    """指代闭环主路径：标注图（PNG+表+注册表）→ 顶面标签 → add_hole 通孔体积精确。"""
    out = _run_in_env(runtime_env, (
        "import math\n"
        "from vibecad.engine.session import Session\n"
        "from vibecad.feedback.annotate import render_annotated\n"
        "from vibecad.tools import features, modeling\n"
        "s = Session(); modeling.new_document(s, 'Lab')\n"
        "modeling.add_box(s, 40, 30, 20)\n"
        "png, table, freg, ereg = render_annotated(s.get_result_shape(), mode='faces')\n"
        "assert png[:4] == b'\\x89PNG' and len(png) > 2000, len(png)\n"
        "s.set_labels(freg, ereg)\n"
        "top = next(lab for lab, d in table.items() if '顶面' in d)\n"
        "r = features.add_hole(s, top, diameter=8)\n"
        "expected = 24000 - math.pi * 16 * 20\n"  # 通孔挖掉整段 ⌀8 圆柱
        "assert r['ok'] and abs(r['volume'] - expected) < 1.0, (r['volume'], expected)\n"
        "print('HOLE_BY_LABEL_OK')\n"
    ))
    assert "HOLE_BY_LABEL_OK" in out


@pytest.mark.slow
def test_stale_label_raises_unchanged_face_still_resolves(runtime_env):
    """持久命名方案的灵魂测试：几何变更后，动过的面标签过期（响亮拒绝），
    没动过的面标签仍可解析。用盲孔（depth=10）——通孔会同时打穿底面让两个标签都过期。"""
    out = _run_in_env(runtime_env, (
        "from vibecad.engine import naming\n"
        "from vibecad.engine.session import Session\n"
        "from vibecad.feedback.annotate import render_annotated\n"
        "from vibecad.tools import features, modeling\n"
        "s = Session(); modeling.new_document(s, 'Stale')\n"
        "modeling.add_box(s, 40, 30, 20)\n"
        "png, table, freg, ereg = render_annotated(s.get_result_shape(), mode='faces')\n"
        "s.set_labels(freg, ereg)\n"
        "top = next(lab for lab, d in table.items() if '顶面' in d)\n"
        "bottom = next(lab for lab, d in table.items() if '底面' in d)\n"
        "features.add_hole(s, top, 8, depth=10)\n"  # 盲孔：只动顶面，不碰底面
        "raised = False\n"
        "try:\n"
        "    s.resolve_face(top)\n"  # 顶面面积变了（被孔开口）→ 必须过期
        "except naming.LabelExpiredError:\n"
        "    raised = True\n"
        "assert raised, 'stale top-face label must raise LabelExpiredError'\n"
        "idx = s.resolve_face(bottom)\n"  # 底面没动 → 仍可解析
        "assert isinstance(idx, int) and 0 <= idx < len(s.get_result_shape().Faces), idx\n"
        "print('STALE_OK')\n"
    ))
    assert "STALE_OK" in out


@pytest.mark.slow
def test_fillet_and_chamfer_by_edge_labels(runtime_env):
    """边标签指代：竖直边 fillet → 面数 +1；重新标注后对角竖直边 chamfer → 再 +1。
    真机发现：fillet 的圆角面与侧面之间是切线缝合边（同为长 30、中点 z=15 的 Line），
    对切边 chamfer OCCT 必败返回 NULL shape——必须以 RuntimeError 形态失败并回滚；
    成功路径须选离 fillet 边最远的对角边。"""
    out = _run_in_env(runtime_env, (
        "from vibecad.engine.session import Session\n"
        "from vibecad.feedback.annotate import render_annotated\n"
        "from vibecad.tools import features, modeling\n"
        "s = Session(); modeling.new_document(s, 'EdgeF')\n"
        "modeling.add_box(s, 30, 30, 30)\n"
        "png, table, freg, ereg = render_annotated(s.get_result_shape(), mode='edges')\n"
        "s.set_labels(freg, ereg)\n"
        "vert = next(lab for lab, fp in ereg.items()\n"
        "            if fp['curve'] == 'Line' and abs(fp['midpoint'][2] - 15) < 1e-6)\n"
        "assert '直线边' in table[vert], table[vert]\n"  # 中点 z=15 的只能是竖直边
        "fx, fy = ereg[vert]['midpoint'][0], ereg[vert]['midpoint'][1]\n"
        "r = features.fillet_edges(s, [vert], radius=3)\n"
        "nf = len(s.get_result_shape().Faces)\n"
        "assert r['ok'] and nf > 6, (r, nf)\n"
        "png2, t2, freg2, ereg2 = render_annotated(s.get_result_shape(), mode='edges')\n"
        "s.set_labels(freg2, ereg2)\n"  # fillet 后旧标签过期 → 重新标注
        "cands = [lab for lab, fp in ereg2.items()\n"
        "         if fp['curve'] == 'Line' and abs(fp['midpoint'][2] - 15) < 1e-6\n"
        "         and abs(fp['length'] - 30) < 1e-6]\n"
        "def d2(lab):\n"
        "    m = ereg2[lab]['midpoint']\n"
        "    return (m[0] - fx) ** 2 + (m[1] - fy) ** 2\n"
        "tangent = min(cands, key=d2)\n"  # 圆角面的切线缝合边：chamfer 必败（NULL shape）
        "msg = ''\n"
        "try:\n"
        "    features.chamfer_edges(s, [tangent], size=2)\n"
        "except RuntimeError as exc:\n"  # 必须是 RuntimeError——OCCError 泄漏即契约破坏
        "    msg = str(exc)\n"
        "assert 'NULL' in msg, msg\n"
        "vert2 = max(cands, key=d2)\n"  # 对角边离圆角最远 → chamfer 成功
        "r2 = features.chamfer_edges(s, [vert2], size=2)\n"  # 顺带验证失败事务已回滚
        "nf2 = len(s.get_result_shape().Faces)\n"
        "assert r2['ok'] and nf2 > nf, (r2, nf, nf2)\n"
        "print('EDGE_FEATURES_OK')\n"
    ))
    assert "EDGE_FEATURES_OK" in out


@pytest.mark.slow
def test_add_hole_offset_outside_raises(runtime_env):
    """offset 让孔完全落在零件外 → 几何断言响亮失败，消息可指导（含'未移除任何材料'）。"""
    out = _run_in_env(runtime_env, (
        "from vibecad.engine.session import Session\n"
        "from vibecad.feedback.annotate import render_annotated\n"
        "from vibecad.tools import features, modeling\n"
        "s = Session(); modeling.new_document(s, 'Off')\n"
        "modeling.add_box(s, 40, 30, 20)\n"
        "png, table, freg, ereg = render_annotated(s.get_result_shape(), mode='faces')\n"
        "s.set_labels(freg, ereg)\n"
        "top = next(lab for lab, d in table.items() if '顶面' in d)\n"
        "msg = ''\n"
        "try:\n"
        "    features.add_hole(s, top, 6, offset=[500, 500])\n"
        "except RuntimeError as exc:\n"
        "    msg = str(exc)\n"
        "assert '未移除任何材料' in msg, msg\n"
        "print('OFFSET_OUTSIDE_OK')\n"
    ))
    assert "OFFSET_OUTSIDE_OK" in out


@pytest.mark.slow
def test_blind_hole_depth_exceeding_material_raises(runtime_env):
    """盲孔超深静默打穿（终审 CRITICAL-2）：5mm 板打 depth=20 必须响亮失败（体积核算），
    不得报 ok:True depth=20 实际打穿；合法盲孔移除体积 ≈ π·r²·depth（±0.1）。"""
    out = _run_in_env(runtime_env, (
        "import math\n"
        "from vibecad.engine.session import Session\n"
        "from vibecad.feedback.annotate import render_annotated\n"
        "from vibecad.tools import features, modeling\n"
        "s = Session(); modeling.new_document(s, 'Blind')\n"
        "modeling.add_box(s, 60, 40, 5)\n"  # 5mm 薄板
        "png, table, freg, ereg = render_annotated(s.get_result_shape(), mode='faces')\n"
        "s.set_labels(freg, ereg)\n"
        "top = next(lab for lab, d in table.items() if '顶面' in d)\n"
        "msg = ''\n"
        "try:\n"
        "    features.add_hole(s, top, 10, depth=20)\n"  # 超深：必打穿
        "except RuntimeError as exc:\n"
        "    msg = str(exc)\n"
        "assert ('打穿' in msg or '超出' in msg), msg\n"
        "s.close_document(); modeling.new_document(s, 'Blind2')\n"
        "modeling.add_box(s, 40, 30, 20)\n"
        "p2, t2, freg2, ereg2 = render_annotated(s.get_result_shape(), mode='faces')\n"
        "s.set_labels(freg2, ereg2)\n"
        "top2 = next(lab for lab, d in t2.items() if '顶面' in d)\n"
        "r = features.add_hole(s, top2, 8, depth=10)\n"  # 合法盲孔
        "removed = 24000 - r['volume']\n"
        "assert abs(removed - math.pi * 16 * 10) < 0.1, removed\n"
        "print('BLIND_DEPTH_OK')\n"
    ))
    assert "BLIND_DEPTH_OK" in out


@pytest.mark.slow
def test_second_same_diameter_hole_cannot_hide_notch(runtime_env):
    """同径旧孔放行新孔缺口（终审 CRITICAL-3）：先打 d=10 完整孔，再打同径越界
    缺口孔必须被增量判据逮住（after >= before+1）；第二个合法同径孔正常放行。"""
    out = _run_in_env(runtime_env, (
        "from vibecad.engine.session import Session\n"
        "from vibecad.feedback.annotate import render_annotated\n"
        "from vibecad.tools import features, modeling\n"
        "from vibecad.tools.features import _count_full_cylinder_faces\n"
        "s = Session(); modeling.new_document(s, 'Pair')\n"
        "modeling.add_box(s, 60, 40, 8)\n"
        "png, table, freg, ereg = render_annotated(s.get_result_shape(), mode='faces')\n"
        "s.set_labels(freg, ereg)\n"
        "top = next(lab for lab, d in table.items() if '顶面' in d)\n"
        "r1 = features.add_hole(s, top, 10)\n"  # 第一个完整孔（居中）
        "assert r1['ok'], r1\n"
        "p2, t2, freg2, ereg2 = render_annotated(s.get_result_shape(), mode='faces')\n"
        "s.set_labels(freg2, ereg2)\n"  # 顶面被孔开口，标签过期 → 重新标注
        "top2 = next(lab for lab, d in t2.items() if '顶面' in d)\n"
        "msg = ''\n"
        "try:\n"
        "    features.add_hole(s, top2, 10, offset=[28, 0])\n"  # 孔心 x=58，越过 x=60 边缘
        "except RuntimeError as exc:\n"
        "    msg = str(exc)\n"
        "assert '未形成完整圆孔' in msg, msg\n"  # 旧存在性判据会被第一孔放行——必须逮住
        "r3 = features.add_hole(s, top2, 10, offset=[15, 0])\n"  # 第二个合法同径孔
        "assert r3['ok'], r3\n"
        "assert _count_full_cylinder_faces(s.get_result_shape(), 5.0) == 2\n"
        "print('NOTCH_CAUGHT_OK')\n"
    ))
    assert "NOTCH_CAUGHT_OK" in out


@pytest.mark.slow
def test_visibility_signs_on_real_box(runtime_env):
    """可见性符号（表是对外契约）：top 视角下顶面无'不可见'注；底面注明预设视角均不可见；
    打孔后孔壁（圆柱面，法向均值≈0）注明预设视角均不可见。"""
    out = _run_in_env(runtime_env, (
        "from vibecad.engine.session import Session\n"
        "from vibecad.feedback.annotate import render_annotated\n"
        "from vibecad.tools import features, modeling\n"
        "s = Session(); modeling.new_document(s, 'Vis')\n"
        "modeling.add_box(s, 40, 30, 20)\n"
        "png, table, freg, ereg = render_annotated(s.get_result_shape(), mode='faces',"
        " view='top')\n"
        "s.set_labels(freg, ereg)\n"
        "top = next(lab for lab, d in table.items() if '顶面' in d)\n"
        "bottom = next(lab for lab, d in table.items() if '底面' in d)\n"
        "assert '不可见' not in table[top], table[top]\n"
        "assert '不可见' in table[bottom], table[bottom]\n"
        "assert '预设视角均不可见' in table[bottom], table[bottom]\n"
        "features.add_hole(s, top, 8)\n"
        "png2, t2, _, _ = render_annotated(s.get_result_shape(), mode='faces', view='top')\n"
        "cyl = [d for d in t2.values() if '圆柱面' in d]\n"
        "assert len(cyl) == 1, t2\n"  # 通孔恰好引入一个孔壁圆柱面
        "assert '预设视角均不可见' in cyl[0], cyl[0]\n"
        "print('VIS_SIGNS_OK')\n"
    ))
    assert "VIS_SIGNS_OK" in out


@pytest.mark.slow
def test_unshown_edge_label_rejected_until_edges_annotated(runtime_env):
    """shown gate 真机闭环（终审 CRITICAL-1）：只看过 faces 标注图就编造 'E1' 做
    fillet → 响亮拒绝；annotate edges（同几何，shown 累积）后 → fillet 成功。"""
    out = _run_in_env(runtime_env, (
        "from vibecad.engine import naming\n"
        "from vibecad.engine.session import Session\n"
        "from vibecad.feedback.annotate import render_annotated\n"
        "from vibecad.tools import features, modeling\n"
        "s = Session(); modeling.new_document(s, 'Shown')\n"
        "modeling.add_box(s, 30, 30, 30)\n"
        "png, table, freg, ereg = render_annotated(s.get_result_shape(), mode='faces')\n"
        "s.set_labels(freg, ereg, shown=set(table.keys()))\n"  # 模拟 server：只展示面条目
        "msg = ''\n"
        "try:\n"
        "    features.fillet_edges(s, ['E1'], radius=2)\n"  # 没看过边标注图——编造
        "except naming.LabelExpiredError as exc:\n"
        "    msg = str(exc)\n"
        "assert '尚未' in msg, msg\n"
        "png2, t2, freg2, ereg2 = render_annotated(s.get_result_shape(), mode='edges')\n"
        "s.set_labels(freg2, ereg2, shown=set(t2.keys()))\n"  # 同几何 → shown 累积
        "r = features.fillet_edges(s, ['E1'], radius=2)\n"
        "assert r['ok'], r\n"
        "print('SHOWN_GATE_OK')\n"
    ))
    assert "SHOWN_GATE_OK" in out


@pytest.mark.slow
def test_edges_mode_keeps_face_labels(runtime_env):
    """T3 修复回归：看面→看边（每次 render 都整体 set_labels，模拟 server 行为）后，
    面标签仍可解析——注册表无论 mode 都全量注册，不因 edges 渲染丢面标签。"""
    out = _run_in_env(runtime_env, (
        "from vibecad.engine.session import Session\n"
        "from vibecad.feedback.annotate import render_annotated\n"
        "from vibecad.tools import modeling\n"
        "s = Session(); modeling.new_document(s, 'Keep')\n"
        "modeling.add_box(s, 20, 20, 20)\n"
        "p1, t1, freg1, ereg1 = render_annotated(s.get_result_shape(), mode='faces')\n"
        "s.set_labels(freg1, ereg1)\n"
        "p2, t2, freg2, ereg2 = render_annotated(s.get_result_shape(), mode='edges')\n"
        "s.set_labels(freg2, ereg2)\n"  # 整体覆盖——faces 注册表必须还在
        "idx = s.resolve_face('A')\n"
        "assert isinstance(idx, int) and 0 <= idx < 6, idx\n"
        "print('KEEP_LABELS_OK')\n"
    ))
    assert "KEEP_LABELS_OK" in out
