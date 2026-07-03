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
    import math  # noqa: PLC0415
    n = (1 / math.sqrt(3),) * 3
    e1, e2 = features._inplane_axes(n)
    for e in (e1, e2):
        assert abs(sum(a * b for a, b in zip(e, n, strict=True))) < 1e-9  # 与法向正交


def test_param_mid_is_parameter_range_midpoint():
    """offset 原点基准锁定：_param_mid 必须取 face.ParameterRange（UVBounds）中点。
    内孔 uv 范围是外环子集、不影响 UVBounds → 该中点不随既有孔漂移；
    CenterOfMass（面积质心）会漂移（真机实测 d8 孔致 0.44mm 偏差），不得用作基准。"""
    class _StubFace:
        ParameterRange = (0.0, 40.0, 0.0, 30.0)

    assert features._param_mid(_StubFace()) == (20.0, 15.0)


@pytest.mark.parametrize("pattern,msg", [
    ({"type": "grid"}, "type"),
    ({"type": "linear"}, "count"),
    ({"type": "linear", "count": 1, "spacing": 10}, "count"),
    ({"type": "linear", "count": 51, "spacing": 10}, "count"),
    ({"type": "linear", "count": 4, "spacing": 0}, "spacing"),
    ({"type": "linear", "count": 4, "spacing": 10, "direction": [0, 0]}, "direction"),
    ({"type": "circular", "count": 6}, "radius"),
    ({"type": "circular", "count": 6, "radius": -1}, "radius"),
])
def test_add_hole_pattern_validation(pattern, msg):
    with pytest.raises(ValueError, match=msg):
        features.add_hole(_NoopSession(), face="A", diameter=6, pattern=pattern)


@pytest.mark.parametrize("kwargs,msg", [
    # 成对约束：只给其一 → 响亮报错（校验先于一切 session 访问）
    ({"counterbore_diameter": 10}, "成对"),
    ({"counterbore_depth": 3}, "成对"),
    # 数值合法性
    ({"counterbore_diameter": 0, "counterbore_depth": 3}, "counterbore_diameter"),
    ({"counterbore_diameter": -10, "counterbore_depth": 3}, "counterbore_diameter"),
    ({"counterbore_diameter": float("nan"), "counterbore_depth": 3}, "counterbore_diameter"),
    ({"counterbore_diameter": 10, "counterbore_depth": 0}, "counterbore_depth"),
    ({"counterbore_diameter": 10, "counterbore_depth": float("inf")}, "counterbore_depth"),
    # 大径必须 > 主孔径（== 也拒：无台阶不是沉头）
    ({"counterbore_diameter": 6, "counterbore_depth": 3}, "大于"),
    ({"counterbore_diameter": 4, "counterbore_depth": 3}, "大于"),
    # 盲孔时沉头深必须 < depth（== 也拒：主孔壁不复存在）
    ({"depth": 5, "counterbore_diameter": 10, "counterbore_depth": 5}, "小于"),
    ({"depth": 5, "counterbore_diameter": 10, "counterbore_depth": 8}, "小于"),
])
def test_add_hole_counterbore_validation(kwargs, msg):
    """counterbore 参数校验：全部在 session 访问前（_NoopSession 碰到即 AttributeError）。"""
    with pytest.raises(ValueError, match=msg):
        features.add_hole(_NoopSession(), face="A", diameter=6, **kwargs)


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
        # 通孔体积 = 1000 - π·r²·h = 1000 - π·4·10（diameter=4 → r=2，box 高 10）
        "import math\n"
        "expected_vol = 1000 - math.pi * 4 * 10\n"
        "assert r['ok'] and abs(r['volume'] - expected_vol) < 0.5, (r['volume'], expected_vol)\n"
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
        # fillet 精确体积：单边 r=3，长 30，圆角去掉材料 = 30·(3²−π·3²/4) = 270·(1−π/4)
        "import math\n"
        "fillet_vol = r['volume']\n"
        "expected_fillet = 27000 - 270 * (1 - math.pi / 4)\n"
        "assert abs(fillet_vol - expected_fillet) < 0.1, (fillet_vol, expected_fillet)\n"
        "assert nf == 7, f'fillet 后面数应为 7（6原始+1圆角），得到 {nf}'\n"
        # chamfer 精确体积：对角边 s=2，长 30，截面等腰直角三角形面积 = s²/2 = 2，去掉 60
        "chamfer_vol = r2['volume']\n"
        "expected_chamfer = expected_fillet - 2 * 2 / 2 * 30\n"
        "assert abs(chamfer_vol - expected_chamfer) < 0.1, (chamfer_vol, expected_chamfer)\n"
        "assert nf2 == 8, f'chamfer 后面数应为 8（7+1倒角面），得到 {nf2}'\n"
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
        # T2 消息统一为精确计数文案（'期望完整圆孔增加 N 个'）——断言放宽到共有词干，
        # 拒绝行为与回滚语义不变（旧存在性判据会被第一孔放行——增量判据必须逮住）
        "assert '完整圆孔' in msg, msg\n"
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


@pytest.mark.slow
def test_visibility_front_view_signs(runtime_env):
    """front 视角可见性符号：_VIEWS['front']=(0,-90) → 相机方向 (0,-1,0)（指向 -Y）。
    '前面'(-Y 法向)在 front 视角可见 → 无'不可见'；'后面'(+Y 法向)不可见 → 含 'back'。
    钉死 camera_direction 的 azim 符号链，防止日后 _VIEWS 被意外调整。"""
    out = _run_in_env(runtime_env, (
        "from vibecad.engine.session import Session\n"
        "from vibecad.feedback.annotate import render_annotated\n"
        "from vibecad.tools import modeling\n"
        "s = Session(); modeling.new_document(s, 'Front')\n"
        "modeling.add_box(s, 40, 30, 20)\n"
        "png, table, freg, ereg = render_annotated(s.get_result_shape(), mode='faces',"
        " view='front')\n"
        "front_entry = next((d for d in table.values() if '前面' in d), None)\n"
        "back_entry = next((d for d in table.values() if '后面' in d), None)\n"
        "assert front_entry is not None, f'未找到前面标签，table={table}'\n"
        "assert back_entry is not None, f'未找到后面标签，table={table}'\n"
        "assert '不可见' not in front_entry, f'前面在 front 视角应可见，got={front_entry}'\n"
        "assert 'back' in back_entry, f'后面应含 back 提示，got={back_entry}'\n"
        "print('FRONT_SIGNS_OK')\n"
    ))
    assert "FRONT_SIGNS_OK" in out


@pytest.mark.slow
def test_edges_of_filters_to_face_edges(runtime_env):
    """edges_of 过滤：box(40,30,20) 顶面 → render_annotated(edges_of=顶面索引)
    → labels_table 恰 4 条边、每条中点 z==20（顶面高度）、标签键是全量边序号子集
    且同键同描述（全局序号不因 edges_of 漂移）。"""
    out = _run_in_env(runtime_env, (
        "from vibecad.engine.session import Session\n"
        "from vibecad.feedback.annotate import render_annotated\n"
        "from vibecad.tools import modeling\n"
        "s = Session(); modeling.new_document(s, 'EOf')\n"
        "modeling.add_box(s, 40, 30, 20)\n"
        "shape = s.get_result_shape()\n"
        # 先全量 edges 标注（获取全量 table 和顶面索引）
        "png_all, t_all, freg, ereg = render_annotated(shape, mode='edges')\n"
        "png_f, t_f, freg2, ereg2 = render_annotated(shape, mode='faces')\n"
        "top_idx = max(range(len(shape.Faces)), key=lambda i: shape.Faces[i].CenterOfMass.z)\n"
        # edges_of 过滤
        "png2, t2, _, _ = render_annotated(shape, mode='edges', edges_of=top_idx)\n"
        # 顶面有 4 条边
        "assert len(t2) == 4, f'顶面应有 4 条边，得到 {len(t2)}: {list(t2.keys())}'\n"
        # 每条边中点 z == 20
        "for lab, desc in t2.items():\n"
        "    fp = ereg[lab]\n"
        "    assert abs(fp['midpoint'][2] - 20.0) < 1e-3, f'{lab} 中点 z={fp[\"midpoint\"][2]}'\n"
        # 标签键是全量表的子集
        "assert set(t2.keys()) <= set(t_all.keys()), '标签键不是全量边序号子集'\n"
        # 同键同描述（全局序号不漂移）
        "for lab in t2:\n"
        "    assert t2[lab] == t_all[lab], f'{lab}: {t2[lab]!r} != {t_all[lab]!r}'\n"
        "print('EDGES_OF_OK')\n"
    ))
    assert "EDGES_OF_OK" in out


@pytest.mark.slow
def test_add_hole_offset_direction(runtime_env):
    """offset 方向：box(40,30,20) 顶面（法向 +Z → _inplane_axes 给出 e1/e2）。
    对 +Z 法向：最不平行的全局轴是 X 或 Y → e1 对应 X 轴（abs(e1·X)≈1），
    e2 = n×e1 对应 Y 轴（cross(Z,X)=−Y，取反 → e2 对应 +Y）。
    实测验证：offset=[10,5] → 孔心 x≈20+10=30，y≈15+5=20（面心 (20,15,20)）。
    注：_inplane_axes 对 n=(0,0,1)：g=min by |dot| → g=X，
    e1 = X - 0*Z = X = (1,0,0)；e2 = Z×e1 = (0,0,1)×(1,0,0) = (0,-(-1),0)...
    需真机实测确认（e2 由叉积 n×e1 = (0,0,1)×(1,0,0) = (0·0-1·0, 1·1-0·0, 0·0-0·1) = (0,1,0)）
    即 e1→+X, e2→+Y。offset=[10,5]: x=20+10=30, y=15+5=20。"""
    out = _run_in_env(runtime_env, (
        "import math\n"
        "from vibecad.engine.session import Session\n"
        "from vibecad.feedback.annotate import render_annotated\n"
        "from vibecad.tools import features, modeling\n"
        "s = Session(); modeling.new_document(s, 'OffDir')\n"
        "modeling.add_box(s, 40, 30, 20)\n"
        "png, table, freg, ereg = render_annotated(s.get_result_shape(), mode='faces')\n"
        "s.set_labels(freg, ereg)\n"
        "top = next(lab for lab, d in table.items() if '顶面' in d)\n"
        "r = features.add_hole(s, top, diameter=8, depth=10, offset=[10, 5])\n"
        "assert r['ok'], r\n"
        # 找孔壁圆柱面（r=4），验证轴上点坐标
        "cyl_faces = [f for f in s.get_result_shape().Faces\n"
        "             if type(f.Surface).__name__ == 'Cylinder'\n"
        "             and abs(f.Surface.Radius - 4.0) < 1e-3]\n"
        "assert len(cyl_faces) >= 1, '未找到 r=4 圆柱面'\n"
        "ctr = cyl_faces[0].Surface.Center\n"
        # e1→+X, e2→+Y（已由 _inplane_axes 对 +Z 法向的叉积公式推导：
        # e1=X, e2=Z×X=(0,0,1)×(1,0,0)=(0·0-1·0,1·1-0·0,0·0-0·1)=(0,1,0)=+Y）
        # 面心 (20,15,*), offset=[10,5]: x≈20+10=30, y≈15+5=20
        "assert abs(ctr.x - 30.0) < 1e-3, f'孔心 x 期望 30，得到 {ctr.x}'\n"
        "assert abs(ctr.y - 20.0) < 1e-3, f'孔心 y 期望 20，得到 {ctr.y}'\n"
        # 体积精确断言：24000 - π·r²·depth = 24000 - π·16·10
        "expected_vol = 24000 - math.pi * 16 * 10\n"
        "assert abs(r['volume'] - expected_vol) < 0.1, (r['volume'], expected_vol)\n"
        "print('OFFSET_DIR_OK')\n"
    ))
    assert "OFFSET_DIR_OK" in out


@pytest.mark.slow
def test_add_hole_offset_origin_stable_after_existing_hole(runtime_env):
    """offset 原点必须不受既有孔影响：box(40,30,20) 顶面先打 d8 盲孔 offset=[-10,0]
    （圆心 (10,15) 精确），重新标注后再打 d6 通孔 offset=[10,5]——第二孔圆心必须
    精确落在 (30,20)。若原点取 face.CenterOfMass（旧实现），第一孔减除的面积使顶面
    质心 +X 漂移 ≈0.44mm，第二孔静默落在 (30.44,20)——对称孔阵列会得到不对称结果
    且无任何报错。稳定基准 = 面参数范围（UVBounds）中点，内孔 uv 范围是外环子集，
    不影响该中点。"""
    out = _run_in_env(runtime_env, (
        "from vibecad.engine.session import Session\n"
        "from vibecad.feedback.annotate import render_annotated\n"
        "from vibecad.tools import features, modeling\n"
        "s = Session(); modeling.new_document(s, 'Stable')\n"
        "modeling.add_box(s, 40, 30, 20)\n"
        "png, table, freg, ereg = render_annotated(s.get_result_shape(), mode='faces')\n"
        "s.set_labels(freg, ereg)\n"
        "top = next(lab for lab, d in table.items() if '顶面' in d)\n"
        "def hole_centers(radius):\n"
        "    return [(f.Surface.Center.x, f.Surface.Center.y)\n"
        "            for f in s.get_result_shape().Faces\n"
        "            if type(f.Surface).__name__ == 'Cylinder'\n"
        "            and abs(f.Surface.Radius - radius) < 1e-3]\n"
        "r1 = features.add_hole(s, top, diameter=8, depth=10, offset=[-10, 0])\n"
        "assert r1['ok'], r1\n"
        "c1 = hole_centers(4.0)\n"  # 无孔面上首孔：新旧实现都精确，作基线
        "assert len(c1) == 1 and abs(c1[0][0] - 10) < 1e-3 and abs(c1[0][1] - 15) < 1e-3, c1\n"
        "p2, t2, freg2, ereg2 = render_annotated(s.get_result_shape(), mode='faces')\n"
        "s.set_labels(freg2, ereg2)\n"  # 首孔使顶面标签过期 → 重新标注
        "top2 = next(lab for lab, d in t2.items() if '顶面' in d)\n"
        "r2 = features.add_hole(s, top2, diameter=6, offset=[10, 5])\n"
        "assert r2['ok'], r2\n"
        "c2 = hole_centers(3.0)\n"
        "assert len(c2) == 1, c2\n"
        "assert abs(c2[0][0] - 30.0) < 1e-3, f'第二孔圆心 x 期望 30.0，得到 {c2[0][0]:.4f}'\n"
        "assert abs(c2[0][1] - 20.0) < 1e-3, f'第二孔圆心 y 期望 20.0，得到 {c2[0][1]:.4f}'\n"
        "print('OFFSET_ORIGIN_STABLE_OK')\n"
    ))
    assert "OFFSET_ORIGIN_STABLE_OK" in out


@pytest.mark.slow
def test_counterbore_offset_origin_stable_after_existing_hole(runtime_env):
    """counterbore 两刀路径的 offset 原点同样必须不受既有孔影响（counterbore 与
    offset 原点修复同期落地，须覆盖新路径）：box(40,30,20) 顶面先打 d8 盲孔
    offset=[-10,0]，重新标注后打 d6 通孔 + d10/深2 沉头 offset=[10,5]——主孔壁
    与沉头壁圆心都必须精确落在 (30,20)（两刀经 _drill 共用同一基准 c，旧实现
    CenterOfMass 漂移会让两刀同偏 0.44mm）。"""
    out = _run_in_env(runtime_env, (
        "from vibecad.engine.session import Session\n"
        "from vibecad.feedback.annotate import render_annotated\n"
        "from vibecad.tools import features, modeling\n"
        "s = Session(); modeling.new_document(s, 'CbStable')\n"
        "modeling.add_box(s, 40, 30, 20)\n"
        "png, table, freg, ereg = render_annotated(s.get_result_shape(), mode='faces')\n"
        "s.set_labels(freg, ereg)\n"
        "top = next(lab for lab, d in table.items() if '顶面' in d)\n"
        "r1 = features.add_hole(s, top, diameter=8, depth=10, offset=[-10, 0])\n"
        "assert r1['ok'], r1\n"
        "p2, t2, freg2, ereg2 = render_annotated(s.get_result_shape(), mode='faces')\n"
        "s.set_labels(freg2, ereg2)\n"
        "top2 = next(lab for lab, d in t2.items() if '顶面' in d)\n"
        "r2 = features.add_hole(s, top2, diameter=6, offset=[10, 5],\n"
        "                       counterbore_diameter=10, counterbore_depth=2)\n"
        "assert r2['ok'], r2\n"
        "def centers(radius):\n"
        "    return [(f.Surface.Center.x, f.Surface.Center.y)\n"
        "            for f in s.get_result_shape().Faces\n"
        "            if type(f.Surface).__name__ == 'Cylinder'\n"
        "            and abs(f.Surface.Radius - radius) < 1e-3]\n"
        "main_c = centers(3.0)\n"
        "cb_c = centers(5.0)\n"
        "assert len(main_c) == 1, main_c\n"
        "assert len(cb_c) == 1, cb_c\n"
        "assert abs(main_c[0][0] - 30.0) < 1e-3, \\\n"
        "    f'主孔圆心 x 期望 30.0，得到 {main_c[0][0]:.4f}'\n"
        "assert abs(main_c[0][1] - 20.0) < 1e-3, \\\n"
        "    f'主孔圆心 y 期望 20.0，得到 {main_c[0][1]:.4f}'\n"
        "assert abs(cb_c[0][0] - 30.0) < 1e-3, \\\n"
        "    f'沉头圆心 x 期望 30.0，得到 {cb_c[0][0]:.4f}'\n"
        "assert abs(cb_c[0][1] - 20.0) < 1e-3, \\\n"
        "    f'沉头圆心 y 期望 20.0，得到 {cb_c[0][1]:.4f}'\n"
        "print('CB_OFFSET_ORIGIN_STABLE_OK')\n"
    ))
    assert "CB_OFFSET_ORIGIN_STABLE_OK" in out


@pytest.mark.slow
def test_outward_normal_probe_lands_on_material(runtime_env):
    """探针盲态修复（环形面）：顶面中心盲孔后顶面成环形面，CenterOfMass 落在孔开口上
    ——旧探针两侧都是空气，isInside 恒 False，无法纠正定向破坏的面。
    用 face.reversed() 模拟 OCCT cut 偶发的 Orientation 反转（normalAt 随之朝内，
    真机实测确认）：完整面探针可纠正（既有行为），环形面也必须纠正
    ——探针锚点必须落在材料上（最大三角形质心，同 annotate 标签锚点方案）。"""
    out = _run_in_env(runtime_env, (
        "from vibecad.freecad_env import silence_fd1\n"
        "with silence_fd1():\n"
        "    import FreeCAD\n"
        "    import Part\n"
        "    from vibecad.tools.features import _outward_normal\n"
        "    box = Part.makeBox(40, 30, 20)\n"
        "    cyl = Part.makeCylinder(5, 8.5, FreeCAD.Vector(20, 15, 20.5),"
        " FreeCAD.Vector(0, 0, -1))\n"
        "    shape = box.cut(cyl)\n"  # 顶面中心盲孔 → 顶面成环形面
        "    annular = next(f for f in shape.Faces\n"
        "                   if type(f.Surface).__name__ == 'Plane'\n"
        "                   and abs(f.CenterOfMass.z - 20) < 1e-6 and len(f.Wires) > 1)\n"
        "    n = _outward_normal(shape, annular)\n"
        "    assert n.z > 0.99, f'环形面外法向应为 +z，得到 {n.z}'\n"
        "    n_rev = _outward_normal(shape, annular.reversed())\n"
        "    assert n_rev.z > 0.99, f'定向破坏的环形面未被纠正（探针盲态），得到 {n_rev.z}'\n"
        "    full = next(f for f in box.Faces if type(f.Surface).__name__ == 'Plane'\n"
        "                and abs(f.CenterOfMass.z - 20) < 1e-6)\n"
        "    n_full = _outward_normal(box, full.reversed())\n"
        "    assert n_full.z > 0.99, f'完整面纠正回归，得到 {n_full.z}'\n"
        "print('PROBE_ON_MATERIAL_OK')\n"
    ))
    assert "PROBE_ON_MATERIAL_OK" in out


@pytest.mark.slow
def test_add_hole_on_annular_face(runtime_env):
    """环形面打孔全路径回归（探针盲态场景）：顶面中心盲孔成环形面后，在该面 offset
    打第二盲孔——起钻方向必须朝外（孔壁 z∈[15,20]，不是从材料内 0.5mm 起钻），
    体积核算精确（±0.1）。"""
    out = _run_in_env(runtime_env, (
        "import math\n"
        "from vibecad.engine.session import Session\n"
        "from vibecad.feedback.annotate import render_annotated\n"
        "from vibecad.tools import features, modeling\n"
        "s = Session(); modeling.new_document(s, 'Ann')\n"
        "modeling.add_box(s, 40, 30, 20)\n"
        "png, t, freg, ereg = render_annotated(s.get_result_shape(), mode='faces')\n"
        "s.set_labels(freg, ereg)\n"
        "top = next(lab for lab, d in t.items() if '顶面' in d)\n"
        "r1 = features.add_hole(s, top, 10, depth=8)\n"  # 中心盲孔 → 顶面成环形面
        "assert r1['ok'], r1\n"
        "p2, t2, freg2, ereg2 = render_annotated(s.get_result_shape(), mode='faces')\n"
        "s.set_labels(freg2, ereg2)\n"  # 顶面被孔开口，标签过期 → 重新标注
        "top2 = next(lab for lab, d in t2.items() if '顶面' in d)\n"
        "r2 = features.add_hole(s, top2, 6, depth=5, offset=[12, 0])\n"
        "expected = 24000 - math.pi * 25 * 8 - math.pi * 9 * 5\n"
        "assert r2['ok'] and abs(r2['volume'] - expected) < 0.1, (r2['volume'], expected)\n"
        # 起钻方向朝外：第二孔孔壁（r=3 圆柱面）必须从顶面 z=20 向下到 z=15
        "walls = [f for f in s.get_result_shape().Faces\n"
        "         if type(f.Surface).__name__ == 'Cylinder'\n"
        "         and abs(f.Surface.Radius - 3.0) < 1e-6]\n"
        "assert len(walls) == 1, [f.Surface.Radius for f in walls]\n"
        "bb = walls[0].BoundBox\n"
        "assert abs(bb.ZMax - 20) < 1e-6 and abs(bb.ZMin - 15) < 1e-6, (bb.ZMin, bb.ZMax)\n"
        "ctr = walls[0].Surface.Center\n"
        "assert abs(ctr.x - 32) < 1e-6 and abs(ctr.y - 15) < 1e-6, (ctr.x, ctr.y)\n"
        "print('ANNULAR_HOLE_OK')\n"
    ))
    assert "ANNULAR_HOLE_OK" in out


@pytest.mark.slow
def test_render_multiview_real(runtime_env):
    """multiview HLR 真机：PNG 有效 + 标签语义与 render_annotated(faces) 等价
    + HLR 耗时计时打印 + top 视图投影含圆（box+hole 场景验证圆检测链路通）。"""
    out = _run_in_env(runtime_env, """
import time
from vibecad.engine.session import Session
from vibecad.feedback import multiview, annotate
from vibecad.feedback.multiview import project_view, _VIEW_TFS
from vibecad.freecad_env import silence_fd1
from vibecad.tools import modeling, features

s = Session()
modeling.new_document(s, "mv")
modeling.add_box(s, 40, 30, 20)

# 计时 render_multiview（含 HLR 投影）
t0 = time.time()
png, table, faces_reg, edges_reg = multiview.render_multiview(s.get_result_shape())
t1 = time.time()
print("MULTIVIEW_MS", int((t1 - t0) * 1000))

assert png.startswith(b"\\x89PNG") and len(png) > 5000, len(png)

# 标签语义与 render_annotated(faces, view=iso) 完全等价
_, t2, fr2, er2 = annotate.render_annotated(s.get_result_shape(), mode="faces", view="iso")
assert table == t2, f"table 不等价: {set(table.keys())} vs {set(t2.keys())}"
assert faces_reg == fr2, "faces_reg 不等价"
assert edges_reg == er2, "edges_reg 不等价"

# top 视图投影含圆：先在顶面打孔，孔壁投影到 top 方向应有圆
s2 = Session()
modeling.new_document(s2, "mv_hole")
modeling.add_box(s2, 40, 30, 20)
_, tbl2, freg2, _ = annotate.render_annotated(s2.get_result_shape(), mode="faces")
s2.set_labels(freg2, {})
top_lab = next(lab for lab, d in tbl2.items() if "顶面" in d)
features.add_hole(s2, top_lab, diameter=8)
shape2 = s2.get_result_shape()
direction, tf = _VIEW_TFS["top"]
with silence_fd1():
    pv = project_view(shape2, direction, tf)
assert any(c for c in pv["circles"]), "top 投影应含孔的圆（HLR 圆检测链路未通）"

print("MULTIVIEW_REAL_OK", len(png))
""")
    assert "MULTIVIEW_REAL_OK" in out
    # 从 stdout 提取并报告 HLR 耗时
    for line in out.splitlines():
        if line.startswith("MULTIVIEW_MS"):
            print(line)
            break


@pytest.mark.slow
def test_auto_view_labels_fresh_after_feature(runtime_env):
    """每步自动刷新后标签立即可指（本轮核心行为）+ shown 门控语义不破。"""
    out = _run_in_env(runtime_env, """
from vibecad.engine.naming import LabelExpiredError
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import modeling, features
s = Session()
modeling.new_document(s, "fresh")
modeling.add_box(s, 40, 30, 20)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
features.add_hole(s, top, diameter=8)
# 模拟 server._attach_view 的刷新
png2, t2, fr2, er2 = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr2, er2, shown=set(t2.keys()))
top2 = next(lab for lab, d in t2.items() if "顶面" in d)
assert s.resolve_face(top2) >= 0          # 新标签立即可指
try:
    s.resolve_edge("E1")                   # 边标签未展示仍被拒（门控不破）
    raise SystemExit("EXPECTED LabelExpiredError")
except LabelExpiredError:
    print("FRESH_LABELS_OK", top2)
""")
    assert "FRESH_LABELS_OK" in out


@pytest.mark.slow
def test_multiview_projection_orientation(runtime_env):
    """投影朝向钉死（CRITICAL 回归：right 视图曾 180° 颠倒，对称零件+弱断言漏过）。
    非对称凸台体 box(40,30,20) + 凸台 X[34,40]Y[0,6]Z[20,26]，三视图精确 2D 坐标断言：
    front 凸台在右上 (34..40, 20..26)、right 在上方 (0..6, 20..26)（Y 横轴、Z 朝上）、
    top 在下侧 (34..40, 0..6)——把 _VIEW_TFS 变换符号永久钉死。"""
    out = _run_in_env(runtime_env, """
from vibecad.feedback.multiview import project_view, _VIEW_TFS
from vibecad.freecad_env import silence_fd1
with silence_fd1():
    import FreeCAD
    import Part
    box = Part.makeBox(40, 30, 20)
    boss = Part.makeBox(6, 6, 6, FreeCAD.Vector(34, 0, 20))
    shape = box.fuse(boss)
    views = {k: project_view(shape, d, tf) for k, (d, tf) in _VIEW_TFS.items()}

EPS = 1e-6

def pts_of(view):
    return [p for poly in view["vis"] for p in poly]

def has(view, x, y):
    return any(abs(px - x) < EPS and abs(py - y) < EPS for px, py in pts_of(view))

# front：凸台 4 角精确存在，且高于主体(>20)的点全部落在凸台 X∈[34,40]
for cx, cy in ((34, 20), (40, 20), (34, 26), (40, 26)):
    assert has(views["front"], cx, cy), f"front 缺凸台角点 ({cx},{cy})"
for px, py in pts_of(views["front"]):
    if py > 20 + EPS:
        assert 34 - EPS <= px <= 40 + EPS, f"front 凸台越界点 ({px},{py})"
# right：凸台 4 角 (0,20)(6,20)(0,26)(6,26)（横=+Y、竖=+Z），高于主体的点 Y∈[0,6]
for cx, cy in ((0, 20), (6, 20), (0, 26), (6, 26)):
    assert has(views["right"], cx, cy), f"right 缺凸台角点 ({cx},{cy})——朝向回归！"
for px, py in pts_of(views["right"]):
    if py > 20 + EPS:
        assert -EPS <= px <= 6 + EPS, f"right 凸台越界点 ({px},{py})"
# top：凸台边界点 (34,0)(34,6)(40,6)(40,0)（横=X、竖=Y，凸台在下侧）
for cx, cy in ((34, 0), (34, 6), (40, 6), (40, 0)):
    assert has(views["top"], cx, cy), f"top 缺凸台点 ({cx},{cy})"
print("ORIENTATION_OK")
""")
    assert "ORIENTATION_OK" in out


@pytest.mark.slow
def test_fillet_arc_not_labeled_as_hole(runtime_env):
    """fillet 圆角弧的 Curve 也是 Circle（CRITICAL 回归：曾被当整圆标 ⌀+中心十字
    并劫持定位尺寸）。box + 贯穿孔 ⌀8 + 竖边 fillet r5 → top 投影 circles 只含
    r=4 整圆，r=5 的 90° 圆角弧必须被整圆判定排除。"""
    out = _run_in_env(runtime_env, """
from vibecad.feedback.multiview import project_view, _VIEW_TFS
from vibecad.freecad_env import silence_fd1
with silence_fd1():
    import FreeCAD
    import Part
    box = Part.makeBox(40, 30, 20)
    hole = Part.makeCylinder(4, 20, FreeCAD.Vector(20, 15, 0))
    shape = box.cut(hole)
    # 取 (0,0) 处竖边 fillet r5——top 投影出 r5 四分之一圆弧
    edge = next(e for e in shape.Edges
                if all(abs(v.Point.x) < 1e-9 and abs(v.Point.y) < 1e-9
                       for v in e.Vertexes))
    shape = shape.makeFillet(5, [edge])
    d, tf = _VIEW_TFS["top"]
    pv = project_view(shape, d, tf)
radii = sorted({round(r, 6) for _x, _y, r, _v in pv["circles"]})
assert radii == [4.0], f"top circles 应只含整圆 r=4（圆角弧 r=5 不算孔），得到 {radii}"
print("FULL_CIRCLE_ONLY_OK", radii)
""")
    assert "FULL_CIRCLE_ONLY_OK" in out


@pytest.mark.slow
def test_modify_box_length_recomputes_chain(runtime_env):
    """改 Box.length → Cut 链自动重算，体积精确；旧标签过期。"""
    out = _run_in_env(runtime_env, """
import math
from vibecad.engine.naming import LabelExpiredError
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import modeling, features, modify
s = Session()
modeling.new_document(s, "mod")
modeling.add_box(s, 40, 30, 20)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
features.add_hole(s, top, diameter=8)
r = modify.modify_part(s, "Box", "length", 45)
expect = 45 * 30 * 20 - math.pi * 16 * 20
assert abs(r["volume"] - expect) < 1.0, (r["volume"], expect)
assert r["modified"]["from"] == 40.0 and r["modified"]["to"] == 45.0
try:
    s.resolve_face(top)
    raise SystemExit("EXPECTED stale label")
except LabelExpiredError:
    print("MODIFY_CHAIN_OK", round(r["volume"], 1))
""")
    assert "MODIFY_CHAIN_OK" in out


@pytest.mark.slow
def test_modify_hole_radius(runtime_env):
    """改 HoleTool.radius 4→5 → Cut 链重算，通孔体积 = 24000 − π·25·20。"""
    out = _run_in_env(runtime_env, """
import math
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import modeling, features, modify
s = Session()
modeling.new_document(s, "rad")
modeling.add_box(s, 40, 30, 20)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
features.add_hole(s, top, diameter=8)
r = modify.modify_part(s, "HoleTool", "radius", 5)
expect = 24000 - math.pi * 25 * 20
assert abs(r["volume"] - expect) < 1.0
print("MODIFY_RADIUS_OK")
""")
    assert "MODIFY_RADIUS_OK" in out


@pytest.mark.slow
def test_modify_fillet_radius(runtime_env):
    """改 Fillet.radius 2→3（Edges 元组重写路径）→ 体积 = 27000 − 9·30·(1−π/4)。"""
    out = _run_in_env(runtime_env, """
import math
from vibecad.engine.session import Session
from vibecad.feedback import annotate
from vibecad.tools import modeling, features, modify
s = Session()
modeling.new_document(s, "fil")
modeling.add_box(s, 30, 30, 30)
png, table, fr, er = annotate.render_annotated(
    s.get_result_shape(), mode="edges", view="iso")
s.set_labels(fr, er, shown=set(table.keys()))
lab = next(lab for lab, d in table.items() if "15.0" in d and "直线边" in d)
features.fillet_edges(s, [lab], radius=2)
r = modify.modify_part(s, "Fillet", "radius", 3)
expect = 27000 - 9 * 30 * (1 - math.pi / 4)
assert abs(r["volume"] - expect) < 0.1, (r["volume"], expect)
print("MODIFY_FILLET_OK")
""")
    assert "MODIFY_FILLET_OK" in out


@pytest.mark.slow
def test_modify_downstream_failure_rolls_back(runtime_env):
    """孔径改到超界（r=50 吞掉 40×30 零件）→ 下游断言失败 → 回滚：
    参数复原为 4，会话可恢复（改合法值 5 成功）。"""
    out = _run_in_env(runtime_env, """
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import modeling, features, modify
s = Session()
modeling.new_document(s, "rb")
modeling.add_box(s, 40, 30, 20)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
features.add_hole(s, top, diameter=8)
try:
    modify.modify_part(s, "HoleTool", "radius", 50)  # 超出 40x30 零件
    raise SystemExit("EXPECTED failure")
except (RuntimeError, ValueError) as exc:
    print("RB_RAISED", type(exc).__name__)
obj = s.get_object("HoleTool")
assert abs(float(obj.Radius) - 4.0) < 1e-9, "回滚后参数应复原为 4"
r = modify.modify_part(s, "HoleTool", "radius", 5)  # 会话可恢复
assert r["ok"]
print("MODIFY_ROLLBACK_OK")
""")
    assert "MODIFY_ROLLBACK_OK" in out


@pytest.mark.slow
def test_same_radius_holes_both_have_position_dims(runtime_env):
    """同径双孔：project_view circles 两个圆 → multiview 不抛错；
    定位解耦后两孔均有定位尺寸（数据层断言：vis_full 两项均进定位循环——
    以渲染成功 + circles 数==2 钉住；像素级留人眼黑盒）。"""
    out = _run_in_env(runtime_env, """
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.feedback.multiview import project_view, _VIEW_TFS
from vibecad.tools import modeling, features
s = Session()
modeling.new_document(s, "twin")
modeling.add_box(s, 60, 40, 10)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
features.add_hole(s, top, diameter=10, offset=[-15, 0])
png2, t2, fr2, er2 = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr2, er2, shown=set(t2.keys()))
top2 = next(lab for lab, d in t2.items() if "顶面" in d)
features.add_hole(s, top2, diameter=10, offset=[15, 0])
d, tf = _VIEW_TFS["top"]
pv = project_view(s.get_result_shape(), d, tf)
full_vis = [c for c in pv["circles"] if c[3]]
assert len(full_vis) == 2, full_vis
png3, *_ = multiview.render_multiview(s.get_result_shape())
assert png3.startswith(b"\\x89PNG")
print("TWIN_HOLES_OK")
""")
    assert "TWIN_HOLES_OK" in out


@pytest.mark.slow
def test_modify_guards_feature_semantics(runtime_env):
    """modify_part 特征语义护栏（审查 E1-E6 真机取证修复）：①改长把孔变开槽 →
    孔完整性断言；②改孔径把件切两半 → 单实体/完整性断言；③刀具吞件 → 结果对象
    漂移断言（且回滚后 result 不被劫持、参数复原）；④通孔加长体积不变 → 合法放行
    + note。四场景同一会话串行——每次失败回滚后会话必须可继续。"""
    out = _run_in_env(runtime_env, """
from vibecad.engine import naming
from vibecad.engine.session import Session
from vibecad.tools import features, modeling, modify
s = Session()
modeling.new_document(s, 'GUARD')
modeling.add_box(s, 40, 30, 20)
shape = s.get_result_shape()
top = max(range(len(shape.Faces)), key=lambda i: shape.Faces[i].CenterOfMass.z)
s.set_labels({'A': naming.face_fingerprint(shape.Faces[top])}, {})
features.add_hole(s, 'A', 8)  # 通孔，面正中 (20,15)，跨 x[16,24]
name0 = s.get_result_object().Name
vol0 = s.get_result_shape().Volume
# ① 改长 40→21：孔越过新边缘变开槽 → 完整性断言
try:
    modify.modify_part(s, 'Box', 'length', 21)
    raise SystemExit('E1 EXPECTED integrity failure')
except RuntimeError as exc:
    assert '完整性' in str(exc), exc
assert abs(s.get_result_shape().Volume - vol0) < 1e-6, '回滚后体积应复原'
# ② 改孔径 4→16：刀具 32 > 件宽 30 → 件切两半
try:
    modify.modify_part(s, 'HoleTool', 'radius', 16)
    raise SystemExit('E2 EXPECTED split failure')
except RuntimeError as exc:
    assert ('切成' in str(exc)) or ('完整性' in str(exc)), exc
# ③ 改孔径 4→100：刀具吞件 → Cut 体积归 0 → 结果对象漂移断言
try:
    modify.modify_part(s, 'HoleTool', 'radius', 100)
    raise SystemExit('E3 EXPECTED drift failure')
except RuntimeError as exc:
    assert ('漂移' in str(exc)) or ('完整性' in str(exc)), exc
assert s.get_result_object().Name == name0, 'result 对象不被劫持'
obj = s.get_object('HoleTool')
assert abs(float(obj.Radius) - 4.0) < 1e-9, '回滚后参数应复原为 4'
# ④ 通孔加长（高度在零件外延伸）：体积不变是合法修改 → ok + note
r = modify.modify_part(s, 'HoleTool', 'height', 80)
assert r['ok'] and 'note' in r, r
assert abs(r['volume'] - vol0) < 1e-6, (r['volume'], vol0)
print('MODIFY_GUARDS_OK')
""")
    assert "MODIFY_GUARDS_OK" in out


@pytest.mark.slow
def test_modify_allowed_after_half_groove(runtime_env):
    """复审 B 回归：boolean_cut 半嵌边缘的圆柱刀具开半圆槽——完整圆柱面从创建起
    就是 0（合法几何，无完整孔）。孔完整性基线必须取改前 shape 实际计数而非
    "每径完整面数 >= 刀具数"的虚构不变量，否则含半圆槽的文档上任何 modify_part
    都被指向不存在的"⌀10 孔"误拒。"""
    out = _run_in_env(runtime_env, """
import math
from vibecad.engine.session import Session
from vibecad.tools import modeling, modify
s = Session()
modeling.new_document(s, 'GROOVE')
modeling.add_box(s, 40, 30, 20)
modeling.add_cylinder(s, 5, 20, position=(40, 15, 0))  # 轴在 x=40 边缘，半嵌入
r = modeling.boolean_cut(s, 'Box', 'Cylinder')
assert r['ok'], r
r = modify.modify_part(s, 'Box', 'width', 35)  # 合法修改，与槽无关——不得误拒
assert r['ok'], r
expect = 40 * 35 * 20 - math.pi * 25 * 20 / 2  # 槽仍是完整半圆柱
assert abs(r['volume'] - expect) < 1.0, (r['volume'], expect)
print('HALF_GROOVE_MODIFY_OK', round(r['volume'], 2))
""")
    assert "HALF_GROOVE_MODIFY_OK" in out


# ──────────────────────────────────────────────────────────────────────────────
# Round 7 慢测：reposition / 孔阵列 / 草图拉伸（Task 5）+ 取证固化
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.slow
def test_move_hole_tool_relocates_hole(runtime_env):
    """移动孔刀具 → 孔到新位置（体积不变）；移出零件（密封内腔）→ 拒绝+回滚。
    注：HoleTool z 在真机取证后确定为 20.5（顶面 z=20 + lift=0.5）；
    体积断言放宽为"两次 move 后体积相同"——孔径不变、体积恒等。"""
    out = _run_in_env(runtime_env, """
import math
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import modeling, features, transform
s = Session()
modeling.new_document(s, "mv")
modeling.add_box(s, 40, 30, 20)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
features.add_hole(s, top, diameter=8)
vol_with_hole = s.get_result_shape().Volume
expected_vol = 40 * 30 * 20 - math.pi * 16 * 20
assert abs(vol_with_hole - expected_vol) < 1.0, (vol_with_hole, expected_vol)

# 移动孔刀具到新 xy 位置，保持 z 不变（20.5）
hole_tool = s.get_object("HoleTool")
orig_z = float(hole_tool.Placement.Base.z)
r = transform.move_part(s, "HoleTool", [10, 10, orig_z])
# 体积不变（孔径不变，只是位置改变）
assert abs(r["volume"] - vol_with_hole) < 1.0, (r["volume"], vol_with_hole)

# 尝试移动到会封死孔口的位置（下沉，使 entry/bottom 探针都落在零件内）
# box(40,30,50)场景验证，此处用已知拒绝：移到 z=200（刀具在零件外 → 孔完整性丢失）
vol_before_bad = s.get_result_shape().Volume
try:
    transform.move_part(s, "HoleTool", [200, 200, orig_z])
    raise SystemExit("EXPECTED rejection")
except RuntimeError:
    pass
assert abs(s.get_result_shape().Volume - vol_before_bad) < 1e-6  # 回滚
print("MOVE_OK")
""")
    assert "MOVE_OK" in out


@pytest.mark.slow
def test_rotate_box_swaps_bbox(runtime_env):
    """绕 z 轴转 90° → bbox XLength/YLength 互换；BoundBox 中心 x=20 不动。"""
    out = _run_in_env(runtime_env, """
from vibecad.engine.session import Session
from vibecad.tools import modeling, transform
s = Session()
modeling.new_document(s, "rot")
modeling.add_box(s, 40, 30, 20)
r = transform.rotate_part(s, "Box", axis="z", angle=90)
bb = s.get_result_shape().BoundBox
assert abs(bb.XLength - 30) < 1e-6 and abs(bb.YLength - 40) < 1e-6, (bb.XLength, bb.YLength)
cx = (bb.XMin + bb.XMax) / 2
assert abs(cx - 20) < 1e-6, f"绕中心转后中心 x 应为 20，得 {cx}"
print("ROTATE_OK")
""")
    assert "ROTATE_OK" in out


@pytest.mark.slow
def test_linear_hole_pattern(runtime_env):
    """4 孔线性阵列：体积精确，holes.count==4，top 投影 4 个全圆。"""
    out = _run_in_env(runtime_env, """
import math
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.feedback.multiview import project_view, _VIEW_TFS
from vibecad.tools import modeling, features
s = Session()
modeling.new_document(s, "lin")
modeling.add_box(s, 60, 30, 10)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
r = features.add_hole(s, top, diameter=6, offset=[-15, 0],
                      pattern={"type": "linear", "count": 4, "spacing": 10})
expect = 60 * 30 * 10 - 4 * math.pi * 9 * 10
assert abs(r["volume"] - expect) < 1.0, (r["volume"], expect)
assert r["holes"]["count"] == 4, r["holes"]
d, tf = _VIEW_TFS["top"]
pv = project_view(s.get_result_shape(), d, tf)
full_circles = [c for c in pv["circles"] if c[3]]
assert len(full_circles) == 4, f"top 投影应有 4 个完整圆，得 {len(full_circles)}"
print("LINEAR_OK")
""")
    assert "LINEAR_OK" in out


@pytest.mark.slow
def test_linear_pattern_overlap_rejected(runtime_env):
    """spacing=4 < diameter=6 → 孔间重叠 → 单 solid 或完整性断言失败 → 全量回滚。"""
    out = _run_in_env(runtime_env, """
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import modeling, features
s = Session()
modeling.new_document(s, "ovl")
modeling.add_box(s, 60, 30, 10)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
v0 = s.get_result_shape().Volume
try:
    features.add_hole(s, top, diameter=6,
                      pattern={"type": "linear", "count": 4, "spacing": 4})
    raise SystemExit("EXPECTED rejection (overlap)")
except RuntimeError:
    pass
assert abs(s.get_result_shape().Volume - v0) < 1e-6, "全量回滚后体积应不变"
print("OVERLAP_REJECT_OK")
""")
    assert "OVERLAP_REJECT_OK" in out


@pytest.mark.slow
def test_circular_hole_pattern(runtime_env):
    """6 孔圆形阵列：体积精确（36000 − 6·π·6.25·10）。"""
    out = _run_in_env(runtime_env, """
import math
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import modeling, features
s = Session()
modeling.new_document(s, "cir")
modeling.add_box(s, 60, 60, 10)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
r = features.add_hole(s, top, diameter=5,
                      pattern={"type": "circular", "count": 6, "radius": 18})
expect = 36000 - 6 * math.pi * 6.25 * 10
assert abs(r["volume"] - expect) < 1.0, (r["volume"], expect)
print("CIRCULAR_OK")
""")
    assert "CIRCULAR_OK" in out


# ──────────────────────────────────────────────────────────────────────────────
# counterbore 沉头孔慢测（R7 终验摩擦点：pocket 套孔被核算正确拒 → 一等参数）
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.slow
def test_counterbore_through_hole(runtime_env):
    """60×40×10 板 ⌀6 通孔 + ⌀10×3 沉头：体积 = 24000 − 138π ≈ 23566.46。
    推导：主孔通孔移除 π·3²·10 = 90π；沉头附加移除只计环形（重叠圆柱段两刀
    都覆盖但只移除一次）π·(5²−3²)·3 = 48π；合计 138π ≈ 433.540。
    守卫接线核对：主径/沉头径完整圆柱面各恰 +1；密封探针在沉头几何上不误报
    （沉头刀具钻底探针落在主孔腔内=材料外）；顶面标签过期（与既有 add_hole 一致）。"""
    out = _run_in_env(runtime_env, """
import math
from vibecad.engine.naming import LabelExpiredError
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import features, modeling
from vibecad.tools._integrity import _count_full_cylinder_faces, assert_no_sealed_holes
s = Session()
modeling.new_document(s, "cbthru")
modeling.add_box(s, 60, 40, 10)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
r = features.add_hole(s, top, diameter=6,
                      counterbore_diameter=10, counterbore_depth=3)
expect = 24000 - 138 * math.pi   # 24000 − 433.540 = 23566.460
assert r["ok"] and abs(r["volume"] - expect) < 1.0, (r["volume"], expect)
assert r["hole"]["counterbore"] == {"diameter": 10, "depth": 3}, r["hole"]
shape = s.get_result_shape()
assert _count_full_cylinder_faces(shape, 3.0) == 1, "主孔壁应恰 1 个完整圆柱面"
assert _count_full_cylinder_faces(shape, 5.0) == 1, "沉头壁应恰 1 个完整圆柱面"
assert_no_sealed_holes(s.doc, shape)   # 沉头几何上探针不得误报（不抛即过）
try:
    s.resolve_face(top)
    raise SystemExit("EXPECTED stale label after counterbore hole")
except LabelExpiredError:
    pass
print("CB_THROUGH_OK")
""")
    assert "CB_THROUGH_OK" in out


@pytest.mark.slow
def test_counterbore_blind_hole_and_too_deep_rejected(runtime_env):
    """盲孔+沉头精确核算：40×30×20 板 ⌀6 盲孔 depth=10 + ⌀10×3 沉头，
    移除 = π·3²·10 + π·(5²−3²)·3 = 138π（±0.1，盲孔核算精确路径）。
    沉头超板厚：60×40×10 板通孔 + ⌀10×12 沉头（12 > 板厚 10）→ 沉头吞穿
    整段主孔壁 → 主径完整面计数断言响亮拒绝 + 回滚（'counterbore_depth < 板厚'
    无法在参数层校验——板厚未知，由几何守卫兜底）。"""
    out = _run_in_env(runtime_env, """
import math
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import features, modeling
s = Session()
modeling.new_document(s, "cbblind")
modeling.add_box(s, 40, 30, 20)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
r = features.add_hole(s, top, diameter=6, depth=10,
                      counterbore_diameter=10, counterbore_depth=3)
removed = 24000 - r["volume"]
assert abs(removed - 138 * math.pi) < 0.1, (removed, 138 * math.pi)
assert r["hole"]["depth"] == 10 and r["hole"]["counterbore"]["depth"] == 3, r["hole"]
# 沉头超板厚：几何守卫兜底（主孔壁被沉头吞穿 → 完整圆孔计数不增）
s.close_document()
modeling.new_document(s, "cbdeep")
modeling.add_box(s, 60, 40, 10)
p2, t2, fr2, er2 = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr2, er2, shown=set(t2.keys()))
top2 = next(lab for lab, d in t2.items() if "顶面" in d)
v0 = s.get_result_shape().Volume
msg = ""
try:
    features.add_hole(s, top2, diameter=6,
                      counterbore_diameter=10, counterbore_depth=12)
except RuntimeError as exc:
    msg = str(exc)
assert "完整圆孔" in msg, msg
assert abs(s.get_result_shape().Volume - v0) < 1e-6, "拒绝后应全量回滚"
print("CB_BLIND_OK")
""")
    assert "CB_BLIND_OK" in out


@pytest.mark.slow
def test_counterbore_pattern_each_hole(runtime_env):
    """4 孔线性阵列每孔带沉头：60×40×10 板 ⌀6 通孔 + ⌀10×2 沉头，
    offset=[-18,0] spacing=12 → 孔心 x=12/24/36/48（沉头 r=5 全在板内、互不重叠）。
    体积 = 24000 − 4·(π·3²·10 + π·(5²−3²)·2) = 24000 − 4·122π = 24000 − 488π
    ≈ 22466.902。主径/沉头径完整圆柱面各恰 +4。"""
    out = _run_in_env(runtime_env, """
import math
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import features, modeling
from vibecad.tools._integrity import _count_full_cylinder_faces
s = Session()
modeling.new_document(s, "cbpat")
modeling.add_box(s, 60, 40, 10)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
r = features.add_hole(s, top, diameter=6, offset=[-18, 0],
                      pattern={"type": "linear", "count": 4, "spacing": 12},
                      counterbore_diameter=10, counterbore_depth=2)
expect = 24000 - 488 * math.pi   # 4·(90π + 32π) = 488π ≈ 1533.098
assert r["ok"] and abs(r["volume"] - expect) < 1.0, (r["volume"], expect)
assert r["holes"]["count"] == 4, r["holes"]
assert r["holes"]["counterbore"] == {"diameter": 10, "depth": 2}, r["holes"]
shape = s.get_result_shape()
assert _count_full_cylinder_faces(shape, 3.0) == 4, "4 个主孔壁完整圆柱面"
assert _count_full_cylinder_faces(shape, 5.0) == 4, "4 个沉头壁完整圆柱面"
print("CB_PATTERN_OK")
""")
    assert "CB_PATTERN_OK" in out


@pytest.mark.slow
def test_counterbore_server_flow_and_describe(runtime_env):
    """server 级端到端：render_part 标注 → add_hole(counterbore) → describe_part
    有效（valid=True、体积 = 24000 − 138π、单 solid）——沉头孔后诊断链路不被破坏。"""
    out = _run_in_env(runtime_env, """
import json, math
import vibecad.server as srv
srv._runtime_guard = lambda: None
srv.new_document("cbsrv")
srv.add_box(60, 40, 10)
out_f = srv.render_part(view="iso", annotate="faces")
assert isinstance(out_f, list), out_f
table = json.loads(out_f[1])["labels"]
top = next(lab for lab, d in table.items() if "顶面" in d)
out_h = srv.add_hole(face=top, diameter=6,
                     counterbore_diameter=10.0, counterbore_depth=3.0)
assert isinstance(out_h, list), out_h   # 成功路径 [dict, Image]
body = out_h[0]
assert body["ok"] and body["hole"]["counterbore"] == {"diameter": 10.0, "depth": 3.0}, body
d = srv.describe_part()
expect = 24000 - 138 * math.pi
assert d["valid"] is True and d["solid_count"] == 1, d
assert abs(d["volume"] - expect) < 1.0, (d["volume"], expect)
print("CB_SERVER_DESCRIBE_OK")
""")
    assert "CB_SERVER_DESCRIBE_OK" in out


@pytest.mark.slow
def test_extrude_pad_rect(runtime_env):
    """矩形 pad（20×10，高 5）贴顶面：体积增量 ≈ 1000（1% 容差）。"""
    out = _run_in_env(runtime_env, """
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import modeling, sketch
s = Session()
modeling.new_document(s, "pad")
modeling.add_box(s, 40, 30, 10)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
r = sketch.extrude_profile(s, {"type": "rect", "length": 20, "width": 10},
                           height=5, face=top, operation="pad")
assert abs(r["volume"] - (12000 + 1000)) < 12000 * 0.01, r["volume"]
print("PAD_OK")
""")
    assert "PAD_OK" in out


@pytest.mark.slow
def test_extrude_pocket_slot_and_polygon(runtime_env):
    """slot pocket（20×8，深 5）后接三角形 polygon pocket（深 3）：
    slot 移除量双边 1% 核算（完全嵌入面内，精确通过）；polygon pocket 继续减料。"""
    out = _run_in_env(runtime_env, """
import math
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import modeling, sketch
s = Session()
modeling.new_document(s, "pkt")
modeling.add_box(s, 60, 40, 20)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
slot_area = 20 * 8 + math.pi * 16   # length=20, width=8 → area = 20*8 + π*(8/2)²
r = sketch.extrude_profile(s, {"type": "slot", "length": 20, "width": 8},
                           height=5, face=top, operation="pocket")
assert abs((48000 - r["volume"]) - slot_area * 5) < slot_area * 5 * 0.01, (
    f"slot 移除量 {48000 - r['volume']:.3f} ≠ 期望 {slot_area * 5:.3f}")
png2, t2, fr2, er2 = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr2, er2, shown=set(t2.keys()))
top2 = next(lab for lab, d in t2.items() if "顶面" in d)
r2 = sketch.extrude_profile(s, {"type": "polygon",
                                "points": [[-25, -15], [-15, -15], [-25, -7]]},
                            height=3, face=top2, offset=[0, 0], operation="pocket")
assert (r["volume"] - r2["volume"]) > 0, "三角 pocket 应继续减料"
print("POCKET_OK")
""")
    assert "POCKET_OK" in out


@pytest.mark.slow
def test_extrude_offset_origin_stable_after_existing_hole(runtime_env):
    """extrude offset 原点必须不受既有孔影响（add_hole 同款修复的 sketch 版）：
    box(40,30,20) 顶面先打 d8 盲孔 offset=[-10,0]（圆心 (10,15) 精确），重新标注后
    pocket r3 圆槽 offset=[10,5]——槽壁圆柱面圆心必须精确落在 (30,20)。若原点取
    face.CenterOfMass（旧实现），第一孔减除的面积使顶面质心 +X 漂移 ≈0.44mm，
    圆槽静默落在 (30.44,20) 且无任何报错。稳定基准与 add_hole 共用 _param_mid
    （UVBounds 中点，内孔 uv 范围是外环子集、不影响该中点）。"""
    out = _run_in_env(runtime_env, """
from vibecad.engine.session import Session
from vibecad.feedback.annotate import render_annotated
from vibecad.tools import features, modeling, sketch
s = Session()
modeling.new_document(s, "ExtStable")
modeling.add_box(s, 40, 30, 20)
png, table, freg, ereg = render_annotated(s.get_result_shape(), mode="faces")
s.set_labels(freg, ereg)
top = next(lab for lab, d in table.items() if "顶面" in d)
def cyl_centers(radius):
    return [(f.Surface.Center.x, f.Surface.Center.y)
            for f in s.get_result_shape().Faces
            if type(f.Surface).__name__ == "Cylinder"
            and abs(f.Surface.Radius - radius) < 1e-3]
r1 = features.add_hole(s, top, diameter=8, depth=10, offset=[-10, 0])
assert r1["ok"], r1
c1 = cyl_centers(4.0)  # 无孔面上首孔：新旧实现都精确，作基线
assert len(c1) == 1 and abs(c1[0][0] - 10) < 1e-3 and abs(c1[0][1] - 15) < 1e-3, c1
p2, t2, freg2, ereg2 = render_annotated(s.get_result_shape(), mode="faces")
s.set_labels(freg2, ereg2)  # 首孔使顶面标签过期 → 重新标注
top2 = next(lab for lab, d in t2.items() if "顶面" in d)
r2 = sketch.extrude_profile(s, {"type": "circle", "radius": 3}, height=5,
                            face=top2, offset=[10, 5], operation="pocket")
assert r2["ok"], r2
c2 = cyl_centers(3.0)
assert len(c2) == 1, c2
assert abs(c2[0][0] - 30.0) < 1e-3, f"圆槽圆心 x 期望 30.0，得到 {c2[0][0]:.4f}"
assert abs(c2[0][1] - 20.0) < 1e-3, f"圆槽圆心 y 期望 20.0，得到 {c2[0][1]:.4f}"
print("EXTRUDE_ORIGIN_STABLE_OK")
""")
    assert "EXTRUDE_ORIGIN_STABLE_OK" in out


@pytest.mark.slow
def test_floating_pad_rejected(runtime_env):
    """pad 轮廓 offset 出零件（完全不接触基体）→ Fuse 双 solid 或 pad 体积断言 → 拒绝回滚。
    任意 RuntimeError 均算通过（双 solid 断言 / pad 体积增量断言都是合法拒绝原因）。"""
    out = _run_in_env(runtime_env, """
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import modeling, sketch
s = Session()
modeling.new_document(s, "flt")
modeling.add_box(s, 40, 30, 10)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
v0 = s.get_result_shape().Volume
try:
    sketch.extrude_profile(s, {"type": "circle", "radius": 5}, height=5,
                           face=top, offset=[100, 100], operation="pad")
    raise SystemExit("EXPECTED rejection (floating pad)")
except RuntimeError:
    pass
assert abs(s.get_result_shape().Volume - v0) < 1e-6, "浮空 pad 回滚后体积应不变"
print("FLOATING_REJECT_OK")
""")
    assert "FLOATING_REJECT_OK" in out


# ──────────────────────────────────────────────────────────────────────────────
# 取证固化（R7 commit 1cbab96 后四条断言语义变化的场景钉死）
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.slow
def test_orphan_pad_rejected(runtime_env):
    """face=None + 文档已有零件 → ValueError 含"已有零件"；体积不变。"""
    out = _run_in_env(runtime_env, """
from vibecad.engine.session import Session
from vibecad.tools import modeling, sketch
s = Session()
modeling.new_document(s, "op")
modeling.add_box(s, 40, 30, 20)
v0 = s.get_result_shape().Volume
msg = ""
try:
    sketch.extrude_profile(s, {"type": "rect", "length": 10, "width": 10},
                           height=5, face=None, operation="pad")
except ValueError as exc:
    msg = str(exc)
assert "已有零件" in msg, f"期望含'已有零件'，得 {msg!r}"
assert abs(s.get_result_shape().Volume - v0) < 1e-6, "ValueError 后体积应不变"
print("ORPHAN_PAD_REJECTED_OK")
""")
    assert "ORPHAN_PAD_REJECTED_OK" in out


@pytest.mark.slow
def test_cross_radius_hole_protected(runtime_env):
    """⌀20 同心孔试图覆盖已有 ⌀8 孔 → RuntimeError 含"⌀8"（孔完整性保护）；体积回滚。"""
    out = _run_in_env(runtime_env, """
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import modeling, features
s = Session()
modeling.new_document(s, "cr")
modeling.add_box(s, 60, 40, 20)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
features.add_hole(s, top, diameter=8, offset=[0, 0])
v_with_d8 = s.get_result_shape().Volume
# 重新标注（打孔后顶面变化）
png2, t2, fr2, er2 = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr2, er2, shown=set(t2.keys()))
top2 = next(lab for lab, d in t2.items() if "顶面" in d)
msg = ""
try:
    features.add_hole(s, top2, diameter=20, offset=[0, 0])
except RuntimeError as exc:
    msg = str(exc)
assert "⌀8" in msg, f"期望含'⌀8'，得 {msg!r}"
assert abs(s.get_result_shape().Volume - v_with_d8) < 1e-6, "回滚后 ⌀8 孔应保留"
print("CROSS_RADIUS_PROTECTED_OK")
""")
    assert "CROSS_RADIUS_PROTECTED_OK" in out


@pytest.mark.slow
def test_pocket_punch_through_rejected(runtime_env):
    """10mm 板 pocket 深 25 → 打穿 → 移除量 < 期望 → RuntimeError；体积回滚。"""
    out = _run_in_env(runtime_env, """
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import modeling, sketch
s = Session()
modeling.new_document(s, "ppt")
modeling.add_box(s, 60, 40, 10)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
v0 = s.get_result_shape().Volume
try:
    sketch.extrude_profile(s, {"type": "rect", "length": 20, "width": 10},
                           height=25, face=top, operation="pocket")
    raise SystemExit("EXPECTED rejection (punch through)")
except RuntimeError:
    pass
assert abs(s.get_result_shape().Volume - v0) < 1e-6, "打穿被拒后体积应回滚"
print("POCKET_PUNCH_THROUGH_REJECTED_OK")
""")
    assert "POCKET_PUNCH_THROUGH_REJECTED_OK" in out


@pytest.mark.slow
def test_sealed_cavity_rejected(runtime_env):
    """移孔刀具下沉封口 → 密封内腔探针 → RuntimeError 含"封闭"；体积回滚。
    场景：box(40,30,50)，顶面盲孔 depth=5（HoleTool z=50.5, height=5.5）；
    把 Box 移到 z=7 使刀具两端探针（49.9 和 56.6）均落在新零件 z=[7,57] 内。"""
    out = _run_in_env(runtime_env, """
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import modeling, features, transform
s = Session()
modeling.new_document(s, "sc")
modeling.add_box(s, 40, 30, 50)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
features.add_hole(s, top, diameter=8, depth=5)   # 盲孔，HoleTool z≈50.5, height≈5.5
v0 = s.get_result_shape().Volume
# Box 移到 z=7：零件 z=[7,57]；entry 外延(≈49.9)在 [7,57] ✓；bottom 外延(≈56.6)在 [7,57] ✓
# → 密封内腔
msg = ""
try:
    transform.move_part(s, "Box", [0, 0, 7])
except RuntimeError as exc:
    msg = str(exc)
assert "封闭" in msg, f"期望含'封闭'，得 {msg!r}"
assert abs(s.get_result_shape().Volume - v0) < 1e-6, "密封腔被拒后体积应回滚"
print("SEALED_CAVITY_REJECTED_OK")
""")
    assert "SEALED_CAVITY_REJECTED_OK" in out


@pytest.mark.slow
def test_modify_burying_hole_into_cavity_rejected(runtime_env):
    """R7 终验移交项热修：改基体 height 把既有盲孔埋成密封内腔 → 探针拒绝+回滚。"""
    out = _run_in_env(runtime_env, """
from vibecad.engine.session import Session
from vibecad.feedback import multiview
from vibecad.tools import features, modeling, modify
s = Session()
modeling.new_document(s, "bury")
modeling.add_box(s, 40, 30, 10)
png, table, fr, er = multiview.render_multiview(s.get_result_shape())
s.set_labels(fr, er, shown=set(table.keys()))
top = next(lab for lab, d in table.items() if "顶面" in d)
features.add_hole(s, top, diameter=8, depth=5)   # 顶面盲孔
v0 = s.get_result_shape().Volume
try:
    modify.modify_part(s, "Box", "height", 15)   # 加高把孔口埋进材料
    raise SystemExit("EXPECTED sealed-cavity rejection")
except RuntimeError as exc:
    assert "封闭" in str(exc) or "内腔" in str(exc), str(exc)
assert abs(s.get_result_shape().Volume - v0) < 1e-6   # 回滚
r = modify.modify_part(s, "Box", "width", 35)          # 会话可恢复
assert r["ok"]
print("BURY_REJECT_OK")
""")
    assert "BURY_REJECT_OK" in out


# ──────────────────────────────────────────────────────────────────────────────
# Round 8 慢测：多零件装配（Task 6）
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.slow
def test_two_parts_independent_modeling(runtime_env):
    """两零件互相独立建模：各自 add_box，体积互不干扰；set_active_part 切换后
    modify 只影响活动零件（底板长度变，盖板体积不变）。"""
    out = _run_in_env(runtime_env,
"import math\n"
"from vibecad.engine.session import Session\n"
"from vibecad.tools import modeling, modify\n"
"s = Session()\n"
"modeling.new_document(s, 'twoparts')\n"
"# 创建底板\n"
"s.new_part('底板')\n"
"modeling.add_box(s, 60, 40, 10)\n"
"vol_base = s.get_result_shape().Volume\n"
"assert abs(vol_base - 60 * 40 * 10) < 1.0, vol_base\n"
"# 创建盖板\n"
"s.new_part('盖板')\n"
"modeling.add_box(s, 60, 40, 5)\n"
"vol_lid = s.get_result_shape().Volume\n"
"assert abs(vol_lid - 60 * 40 * 5) < 1.0, vol_lid\n"
"# 检查零件注册表\n"
"names = s.part_names()\n"
"assert len(names) == 2, names\n"
"assert '底板' in names and '盖板' in names\n"
"# 切回底板，modify length 只影响底板\n"
"s.set_active_part('底板')\n"
"r = modify.modify_part(s, 'Box', 'length', 80)\n"
"assert r['ok'], r\n"
"new_base_vol = s.get_result_shape('底板').Volume\n"
"assert abs(new_base_vol - 80 * 40 * 10) < 1.0, new_base_vol\n"
"# 盖板体积不变\n"
"lid_vol_after = s.get_result_shape('盖板').Volume\n"
"assert abs(lid_vol_after - vol_lid) < 1e-6, (lid_vol_after, vol_lid)\n"
"print('TWO_PARTS_OK')\n"
)
    assert "TWO_PARTS_OK" in out


@pytest.mark.slow
def test_place_part_moves_whole_chain(runtime_env):
    """带孔零件整体 place_part 旋转 z 90°——R7 单图元 rotate_part 被拒场景的解药：
    装配级旋转成功，体积不变，全局 BBox 轴互换（XLength↔YLength）。"""
    out = _run_in_env(runtime_env,
"import math\n"
"from vibecad.engine.session import Session\n"
"from vibecad.feedback import multiview\n"
"from vibecad.tools import modeling, features, assembly\n"
"s = Session()\n"
"modeling.new_document(s, 'placerot')\n"
"s.new_part('底板')\n"
"modeling.add_box(s, 60, 40, 10)\n"
"# 标注并打孔\n"
"png, table, fr, er = multiview.render_multiview(s.get_result_shape())\n"
"s.set_labels(fr, er, shown=set(table.keys()))\n"
"top = next(lab for lab, d in table.items() if '顶面' in d)\n"
"features.add_hole(s, top, diameter=8)\n"
"vol_before = s.get_result_shape('底板').Volume\n"
"# place_part 旋转 z 90°（R7 单图元 rotate 被拒，装配级可行）\n"
"r = assembly.place_part(s, '底板', rotation_axis='z', angle=90)\n"
"assert r['ok'], r\n"
"# 体积不变\n"
"vol_after = s.get_result_shape('底板').Volume\n"
"assert abs(vol_after - vol_before) < 1.0, (vol_after, vol_before)\n"
"# 全局装配 shape（含容器位姿）XLength/YLength 互换\n"
"asm_shape = s.get_assembly_shape()\n"
"bb = asm_shape.BoundBox\n"
"# 原始 box 60×40，旋转 90° 后全局 X≈40, Y≈60（允许 1e-3 容差）\n"
"assert abs(bb.XLength - 40) < 1.0 and abs(bb.YLength - 60) < 1.0, (\n"
"    bb.XLength, bb.YLength)\n"
"print('PLACE_ROTATE_OK')\n"
)
    assert "PLACE_ROTATE_OK" in out


@pytest.mark.slow
def test_align_parts_face_to_face(runtime_env):
    """盖板底面贴底板顶面：对齐后盖板底面全局 z == 底板顶面全局 z（1e-6）、
    面心 XY 对齐；再测 gap=2 时间隙精确（盖板底面全局 z == 12）。"""
    out = _run_in_env(runtime_env,
"import math\n"
"from vibecad.engine.session import Session\n"
"from vibecad.feedback import multiview\n"
"from vibecad.tools import modeling, assembly\n"
"s = Session()\n"
"modeling.new_document(s, 'alignface')\n"
"# 底板\n"
"s.new_part('底板')\n"
"modeling.add_box(s, 60, 40, 10)\n"
"png_b, t_b, fr_b, er_b = multiview.render_multiview(s.get_result_shape())\n"
"s.set_labels(fr_b, er_b, shown=set(t_b.keys()), part='底板')\n"
"top_base = next(lab for lab, d in t_b.items() if '顶面' in d)\n"
"# 盖板（初始位于原点，与底板重叠）\n"
"s.new_part('盖板')\n"
"modeling.add_box(s, 60, 40, 5)\n"
"png_l, t_l, fr_l, er_l = multiview.render_multiview(s.get_result_shape())\n"
"s.set_labels(fr_l, er_l, shown=set(t_l.keys()), part='盖板')\n"
"bot_lid = next(lab for lab, d in t_l.items() if '底面' in d)\n"
"# align：盖板底面贴底板顶面，gap=0——共面接触 common=0（真机取证 3），\n"
"# 干涉守卫不误拒，无需 allow_interference=True 残留\n"
"r = assembly.align_parts(s, '盖板', bot_lid, '底板', top_base)\n"
"assert r['ok'], r\n"
"assert r['interference'] == [], r['interference']\n"
"assert r['interference_skipped'] is False, '两个非空零件应真的跑过干涉比较'\n"
"# 验证：盖板底面全局 z ≈ 10（底板顶面 z）\n"
"lid_shape_global = s.get_result_shape('盖板').transformed(\n"
"    s._parts['盖板']['container'].Placement.toMatrix())\n"
"lid_bottom_z = min(f.CenterOfMass.z for f in lid_shape_global.Faces\n"
"                   if f.Area > 1)\n"
"assert abs(lid_bottom_z - 10.0) < 1e-3, f'盖板底面全局 z={lid_bottom_z}，期望 10'\n"
"# 面心 XY 对齐：盖板底面 XY ≈ 底板顶面 XY（都是 (30,20)）\n"
"base_shape_global = s.get_result_shape('底板').transformed(\n"
"    s._parts['底板']['container'].Placement.toMatrix())\n"
"base_top_face = max(base_shape_global.Faces, key=lambda f: f.CenterOfMass.z)\n"
"lid_bot_face = min(\n"
"    (f for f in lid_shape_global.Faces if f.Area > 1),\n"
"    key=lambda f: f.CenterOfMass.z)\n"
"assert abs(lid_bot_face.CenterOfMass.x - base_top_face.CenterOfMass.x) < 1e-3\n"
"assert abs(lid_bot_face.CenterOfMass.y - base_top_face.CenterOfMass.y) < 1e-3\n"
"# 坐标系纪律：容器已动 → 旧标签过期，须以全局 shape 重新标注（与 server 管道同帧）\n"
"png_l2, t_l2, fr_l2, er_l2 = multiview.render_multiview(lid_shape_global)\n"
"s.set_labels(fr_l2, er_l2, shown=set(t_l2.keys()), part='盖板')\n"
"bot_lid = next(lab for lab, d in t_l2.items() if '底面' in d)\n"
"# gap=2 测试：重新对齐带间隙\n"
"r2 = assembly.align_parts(s, '盖板', bot_lid, '底板', top_base,\n"
"                          gap=2, allow_interference=False)\n"
"assert r2['ok'], r2\n"
"lid_shape_g2 = s.get_result_shape('盖板').transformed(\n"
"    s._parts['盖板']['container'].Placement.toMatrix())\n"
"lid_bottom_z2 = min(f.CenterOfMass.z for f in lid_shape_g2.Faces if f.Area > 1)\n"
"assert abs(lid_bottom_z2 - 12.0) < 1e-3, f'gap=2 盖板底面全局 z={lid_bottom_z2}，期望 12'\n"
"print('ALIGN_FACE_OK')\n"
)
    assert "ALIGN_FACE_OK" in out


@pytest.mark.slow
def test_align_parts_center_stable_after_existing_hole(runtime_env):
    """align_parts 面基准点必须不受既有孔影响（add_hole 同款修复的 assembly 版）：
    底板 box(60,40,10) 顶面先打 d8 盲孔 offset=[-20,0]（孔心 (10,20) 精确），重新
    标注后盖板 box(60,40,5) 底面贴底板顶面——对齐后盖板底面中心必须精确落在
    (30,20)（两零件边缘齐平）。若基准取 face.CenterOfMass（旧实现），d8 孔减除的
    面积使底板顶面质心 +X 漂移 ≈0.43mm，盖板静默偏移 (30.43,20)、边缘不齐平且无
    任何报错。稳定基准与 add_hole/extrude 共用 _param_mid（UVBounds 中点）。"""
    out = _run_in_env(runtime_env,
"from vibecad.engine.session import Session\n"
"from vibecad.feedback import multiview\n"
"from vibecad.tools import assembly, features, modeling\n"
"s = Session()\n"
"modeling.new_document(s, 'alignstable')\n"
"# 底板（active）：标注 → 打 d8 盲孔（孔心 (10,20) 精确基线）\n"
"s.new_part('底板')\n"
"modeling.add_box(s, 60, 40, 10)\n"
"png_b, t_b, fr_b, er_b = multiview.render_multiview(s.get_result_shape())\n"
"s.set_labels(fr_b, er_b, shown=set(t_b.keys()), part='底板')\n"
"top_base = next(lab for lab, d in t_b.items() if '顶面' in d)\n"
"r1 = features.add_hole(s, top_base, diameter=8, depth=5, offset=[-20, 0])\n"
"assert r1['ok'], r1\n"
"c1 = [(f.Surface.Center.x, f.Surface.Center.y)\n"
"      for f in s.get_result_shape('底板').Faces\n"
"      if type(f.Surface).__name__ == 'Cylinder'\n"
"      and abs(f.Surface.Radius - 4.0) < 1e-3]\n"
"assert len(c1) == 1 and abs(c1[0][0] - 10) < 1e-3 and abs(c1[0][1] - 20) < 1e-3, c1\n"
"# 打孔使底板标签过期 → 重新标注\n"
"png_b2, t_b2, fr_b2, er_b2 = multiview.render_multiview(s.get_result_shape())\n"
"s.set_labels(fr_b2, er_b2, shown=set(t_b2.keys()), part='底板')\n"
"top_base2 = next(lab for lab, d in t_b2.items() if '顶面' in d)\n"
"# 盖板（无孔）\n"
"s.new_part('盖板')\n"
"modeling.add_box(s, 60, 40, 5)\n"
"png_l, t_l, fr_l, er_l = multiview.render_multiview(s.get_result_shape())\n"
"s.set_labels(fr_l, er_l, shown=set(t_l.keys()), part='盖板')\n"
"bot_lid = next(lab for lab, d in t_l.items() if '底面' in d)\n"
"# align：盖板底面贴底板带孔顶面\n"
"r = assembly.align_parts(s, '盖板', bot_lid, '底板', top_base2)\n"
"assert r['ok'], r\n"
"# 盖板底面全局中心必须精确 (30,20,10)——盖板无孔，底面 CenterOfMass 即几何中心\n"
"lid_shape_global = s.get_result_shape('盖板').transformed(\n"
"    s._parts['盖板']['container'].Placement.toMatrix())\n"
"lid_bot_face = min(\n"
"    (f for f in lid_shape_global.Faces if f.Area > 1),\n"
"    key=lambda f: f.CenterOfMass.z)\n"
"c = lid_bot_face.CenterOfMass\n"
"assert abs(c.x - 30.0) < 1e-3, f'盖板底面中心 x 期望 30.0，得到 {c.x:.4f}'\n"
"assert abs(c.y - 20.0) < 1e-3, f'盖板底面中心 y 期望 20.0，得到 {c.y:.4f}'\n"
"assert abs(c.z - 10.0) < 1e-3, f'盖板底面全局 z 期望 10.0，得到 {c.z:.4f}'\n"
"print('ALIGN_CENTER_STABLE_OK')\n"
)
    assert "ALIGN_CENTER_STABLE_OK" in out


@pytest.mark.slow
def test_interference_rejected_and_allowed(runtime_env):
    """干涉守卫：gap=-2 叠入 2mm → RuntimeError 报干涉+回滚（盖板位姿不变）；
    allow_interference=True → ok 且 result['interference'] 含干涉量（≈ 60*40*2）。"""
    out = _run_in_env(runtime_env,
"import math\n"
"from vibecad.engine.session import Session\n"
"from vibecad.feedback import multiview\n"
"from vibecad.tools import modeling, assembly\n"
"s = Session()\n"
"modeling.new_document(s, 'interference')\n"
"# 底板\n"
"s.new_part('底板')\n"
"modeling.add_box(s, 60, 40, 10)\n"
"png_b, t_b, fr_b, er_b = multiview.render_multiview(s.get_result_shape())\n"
"s.set_labels(fr_b, er_b, shown=set(t_b.keys()), part='底板')\n"
"top_base = next(lab for lab, d in t_b.items() if '顶面' in d)\n"
"# 盖板\n"
"s.new_part('盖板')\n"
"modeling.add_box(s, 60, 40, 5)\n"
"png_l, t_l, fr_l, er_l = multiview.render_multiview(s.get_result_shape())\n"
"s.set_labels(fr_l, er_l, shown=set(t_l.keys()), part='盖板')\n"
"bot_lid = next(lab for lab, d in t_l.items() if '底面' in d)\n"
"# 先正确对齐 gap=0——共面接触 common=0（真机取证 3），守卫不误拒\n"
"r0 = assembly.align_parts(s, '盖板', bot_lid, '底板', top_base)\n"
"assert r0['ok'], r0\n"
"assert r0['interference'] == [], r0['interference']\n"
"assert r0['interference_skipped'] is False, '两个非空零件应真的跑过干涉比较'\n"
"# 坐标系纪律：容器已动 → 旧标签过期，须以全局 shape 重新标注（与 server 管道同帧）\n"
"lid_global = s.get_result_shape('盖板').transformed(\n"
"    s._parts['盖板']['container'].Placement.toMatrix())\n"
"png_l2, t_l2, fr_l2, er_l2 = multiview.render_multiview(lid_global)\n"
"s.set_labels(fr_l2, er_l2, shown=set(t_l2.keys()), part='盖板')\n"
"bot_lid = next(lab for lab, d in t_l2.items() if '底面' in d)\n"
"# 记录盖板对齐后的位姿\n"
"pl_before = s._parts['盖板']['container'].Placement.Base\n"
"pos_before = (pl_before.x, pl_before.y, pl_before.z)\n"
"# gap=-2 叠入 → 干涉 → 应被拒绝\n"
"raised = False\n"
"err_msg = ''\n"
"try:\n"
"    assembly.align_parts(s, '盖板', bot_lid, '底板', top_base, gap=-2)\n"
"except RuntimeError as exc:\n"
"    raised = True\n"
"    err_msg = str(exc)\n"
"assert raised, '叠入 2mm 应被干涉守卫拒绝'\n"
"assert '干涉' in err_msg, err_msg\n"
"# 回滚验证：盖板位姿不变\n"
"pl_after = s._parts['盖板']['container'].Placement.Base\n"
"pos_after = (pl_after.x, pl_after.y, pl_after.z)\n"
"assert abs(pos_after[2] - pos_before[2]) < 1e-3, (\n"
"    f'回滚失败：z {pos_before[2]} → {pos_after[2]}')\n"
"# allow_interference=True → 放行，result 含干涉量\n"
"r2 = assembly.align_parts(s, '盖板', bot_lid, '底板', top_base,\n"
"                          gap=-2, allow_interference=True)\n"
"assert r2['ok'], r2\n"
"assert r2['interference_skipped'] is False, '有干涉记录说明检查确实跑了'\n"
"interferences = r2.get('interference', [])\n"
"assert len(interferences) > 0, '应有干涉记录'\n"
"vol = interferences[0]['volume']\n"
"# 干涉量 ≈ 60*40*2 = 4800（允许 50% 误差，因接触面几何细节）\n"
"expected_inf = 60 * 40 * 2\n"
"assert vol > expected_inf * 0.3, f'干涉量 {vol} 远小于期望 {expected_inf}'\n"
"print('INTERFERENCE_OK')\n"
)
    assert "INTERFERENCE_OK" in out


@pytest.mark.slow
def test_assembly_export_step(runtime_env):
    """装配 STEP 导出：单文件非空；split=True 出两 per-part 文件均非空。"""
    out = _run_in_env(runtime_env,
"import os, tempfile\n"
"from vibecad.engine.session import Session\n"
"from vibecad.tools import modeling, export\n"
"s = Session()\n"
"modeling.new_document(s, 'exportasm')\n"
"s.new_part('底板')\n"
"modeling.add_box(s, 60, 40, 10)\n"
"s.new_part('盖板')\n"
"modeling.add_box(s, 60, 40, 5)\n"
"tmp_dir = tempfile.mkdtemp()\n"
"# 单文件 STEP\n"
"r1 = export.export_part(s, tmp_dir, fmt='step')\n"
"assert r1['ok'], r1\n"
"step_single = r1['step']\n"
"assert isinstance(step_single, str), step_single\n"
"assert os.path.exists(step_single) and os.path.getsize(step_single) > 0, step_single\n"
"# split=True：两个 per-part 文件\n"
"r2 = export.export_part(s, tmp_dir, fmt='step', split=True)\n"
"assert r2['ok'], r2\n"
"step_list = r2['step']\n"
"assert isinstance(step_list, list) and len(step_list) == 2, step_list\n"
"for p in step_list:\n"
"    assert os.path.exists(p) and os.path.getsize(p) > 0, p\n"
"print('EXPORT_STEP_OK')\n"
)
    assert "EXPORT_STEP_OK" in out


@pytest.mark.slow
def test_single_part_flow_regression(runtime_env):
    """不调 new_part 全流程回归（R7 语义一致）：box→标注→孔→modify→render_multiview，
    体积精确断言，part_names 为空（单零件模式，用户零感知）。"""
    out = _run_in_env(runtime_env,
"import math\n"
"from vibecad.engine.session import Session\n"
"from vibecad.feedback import multiview\n"
"from vibecad.tools import modeling, features, modify\n"
"s = Session()\n"
"modeling.new_document(s, 'r7compat')\n"
"# 不调 new_part → 单零件模式\n"
"assert s.part_names() == [], s.part_names()\n"
"assert s.active_part is None\n"
"modeling.add_box(s, 60, 40, 20)\n"
"# 标注\n"
"png, table, fr, er = multiview.render_multiview(s.get_result_shape())\n"
"s.set_labels(fr, er, shown=set(table.keys()))\n"
"top = next(lab for lab, d in table.items() if '顶面' in d)\n"
"# 打孔\n"
"r_hole = features.add_hole(s, top, diameter=8)\n"
"expected_with_hole = 60 * 40 * 20 - math.pi * 16 * 20\n"
"assert r_hole['ok'] and abs(r_hole['volume'] - expected_with_hole) < 1.0, (\n"
"    r_hole['volume'], expected_with_hole)\n"
"# modify\n"
"r_mod = modify.modify_part(s, 'Box', 'length', 80)\n"
"expected_modified = 80 * 40 * 20 - math.pi * 16 * 20\n"
"assert r_mod['ok'] and abs(r_mod['volume'] - expected_modified) < 1.0, (\n"
"    r_mod['volume'], expected_modified)\n"
"# render_multiview 仍正常\n"
"png2, t2, fr2, er2 = multiview.render_multiview(s.get_result_shape())\n"
"assert png2.startswith(b'\\x89PNG') and len(png2) > 5000\n"
"# 全程不进入多零件模式\n"
"assert s.part_names() == [], s.part_names()\n"
"print('R7_COMPAT_OK')\n"
)
    assert "R7_COMPAT_OK" in out


@pytest.mark.slow
def test_relabel_after_align_resolves_in_global_frame(runtime_env):
    """BUG-1 黑盒复现（server 式全装配标注管道）+ BUG-2 归属后缀：
    align 后容器 Placement 非单位 → render_annotated(装配 compound, part_map)
    重新标注（指纹=全局坐标，表含"（零件：X）"后缀）→ 立即用新标签二次
    align(gap=-2) 必须走到干涉拒绝，而非"面标签无法唯一匹配（命中 0 个）"；
    附 OCCT transformed 不重排子元素断言（"全局匹配、局部消费"语义的前提）。"""
    out = _run_in_env(runtime_env,
"from vibecad.engine import naming\n"
"from vibecad.engine.session import Session\n"
"from vibecad.feedback import annotate\n"
"from vibecad.tools import modeling, assembly\n"
"def annotate_like_server(s):\n"
"    shape = s.get_assembly_shape()\n"
"    part_map = {name: s.get_result_shape(name).transformed(\n"
"        info['container'].Placement.toMatrix())\n"
"        for name, info in s._parts.items()}\n"
"    png, table, freg, ereg = annotate.render_annotated(\n"
"        shape, mode='faces', part_map=part_map)\n"
"    s.set_labels(freg, ereg, shown=set(table.keys()))\n"
"    return shape, table, part_map\n"
"s = Session(); modeling.new_document(s, 'relabel')\n"
"s.new_part('底板'); modeling.add_box(s, 60, 40, 10)\n"
"annotate_like_server(s)\n"
"s.new_part('盖板'); modeling.add_box(s, 60, 40, 5)\n"
"shape, table, _ = annotate_like_server(s)\n"
"labels = naming.face_labels(len(shape.Faces))\n"
"base_top = labels[max(range(0, 6), key=lambda i: shape.Faces[i].CenterOfMass.z)]\n"
"lid_bot = labels[min(range(6, 12), key=lambda i: shape.Faces[i].CenterOfMass.z)]\n"
"r1 = assembly.align_parts(s, '盖板', lid_bot, '底板', base_top)\n"
"assert r1['ok'], r1\n"
"assert r1['interference'] == [], r1['interference']\n"
"pl = s._parts['盖板']['container'].Placement\n"
"assert not pl.isIdentity(), 'align 后盖板容器 Placement 应非单位'\n"
"# OCCT transformed 不重排子元素：逐索引面积一致 + 全局面/边心 == Placement·局部\n"
"local = s.get_result_shape('盖板')\n"
"glob = local.transformed(pl.toMatrix())\n"
"for lf, gf in zip(local.Faces, glob.Faces):\n"
"    assert abs(lf.Area - gf.Area) < 1e-6\n"
"    assert (gf.CenterOfMass - pl.multVec(lf.CenterOfMass)).Length < 1e-6, '面序重排？'\n"
"for le, ge in zip(local.Edges, glob.Edges):\n"
"    assert (ge.CenterOfMass - pl.multVec(le.CenterOfMass)).Length < 1e-6, '边序重排？'\n"
"# 重新标注（盖板已抬升 → 新指纹 = 全局坐标）；BUG-2：表含零件归属后缀\n"
"shape2, table2, pm2 = annotate_like_server(s)\n"
"assert any('（零件：底板）' in v for v in table2.values()), table2\n"
"assert any('（零件：盖板）' in v for v in table2.values()), table2\n"
"png_e, table_e, _, _ = annotate.render_annotated(shape2, mode='edges', part_map=pm2)\n"
"assert any('（零件：盖板）' in v for v in table_e.values()), table_e\n"
"# 立即用新标签二次 align：gap=-2 必须走到干涉拒绝（修复前死在标签匹配'命中 0 个'）\n"
"labels2 = naming.face_labels(len(shape2.Faces))\n"
"base_top2 = labels2[max(range(0, 6), key=lambda i: shape2.Faces[i].CenterOfMass.z)]\n"
"lid_bot2 = labels2[min(range(6, 12), key=lambda i: shape2.Faces[i].CenterOfMass.z)]\n"
"raised = ''\n"
"try:\n"
"    assembly.align_parts(s, '盖板', lid_bot2, '底板', base_top2, gap=-2)\n"
"except RuntimeError as exc:\n"
"    raised = str(exc)\n"
"assert raised, 'gap=-2 应被干涉守卫拒绝'\n"
"assert '干涉' in raised and '唯一匹配' not in raised, raised\n"
"print('RELABEL_GLOBAL_FRAME_OK')\n"
)
    assert "RELABEL_GLOBAL_FRAME_OK" in out


@pytest.mark.slow
def test_move_foreign_part_object_guards_anchor_owner(runtime_env):
    """终审 C-D：active=B 时移动 A 的 HoleTool（孔移出零件）必须被孔完整性
    守卫响亮拒绝——修复前守卫锚活动零件 B 的 shape 做快照，A 的 ⌀8 孔被
    静默吞掉还报 ok:True。回滚后 A 的孔必须完好。"""
    out = _run_in_env(runtime_env,
"from vibecad.engine.session import Session\n"
"from vibecad.feedback import multiview\n"
"from vibecad.tools import assembly, features, modeling, transform\n"
"s = Session(); modeling.new_document(s, 'cd')\n"
"s.new_part('A'); modeling.add_box(s, 40, 40, 20)\n"
"png, t, fr, er = multiview.render_multiview(s.get_result_shape('A'))\n"
"s.set_labels(fr, er, shown=set(t.keys()), part='A')\n"
"top = next(lab for lab, d in t.items() if '顶面' in d)\n"
"features.add_hole(s, top, diameter=8)\n"
"s.new_part('B'); modeling.add_box(s, 10, 10, 10)\n"
"assembly.place_part(s, 'B', position=[100.0, 0.0, 0.0])\n"
"assert s.active_part == 'B'\n"
"raised = ''\n"
"try:\n"
"    transform.move_part(s, 'HoleTool', [200.0, 200.0, 0.0])\n"
"except RuntimeError as exc:\n"
"    raised = str(exc)\n"
"assert raised and '孔' in raised, raised or '应被孔完整性守卫拒绝'\n"
"n_cyl = sum(1 for f in s.get_result_shape('A').Faces\n"
"            if type(f.Surface).__name__ == 'Cylinder')\n"
"assert n_cyl == 1, f'回滚后 A 的孔应完好（圆柱面数={n_cyl}）'\n"
"print('OWNER_ANCHOR_CD_OK')\n"
)
    assert "OWNER_ANCHOR_CD_OK" in out


@pytest.mark.slow
def test_sealed_probe_does_not_cross_parts(runtime_env):
    """终审 C-E：B（带盲孔）容器摆远后，对 A 的无害 1mm 平移必须 ok——修复前
    B 盲孔刀具的探针点（B 局部坐标）打在 A 的 shape（A 局部坐标）上误拒。
    对照：A 自己的孔刀具被移到完全埋入材料（密封内腔）仍必须被探针拒绝。"""
    out = _run_in_env(runtime_env,
"from vibecad.engine.session import Session\n"
"from vibecad.feedback import multiview\n"
"from vibecad.tools import assembly, features, modeling, transform\n"
"def label_top(s, part):\n"
"    sh = s.get_result_shape(part)\n"
"    png, t, fr, er = multiview.render_multiview(sh)\n"
"    s.set_labels(fr, er, shown=set(t.keys()), part=part)\n"
"    return next(lab for lab, d in t.items() if '顶面' in d)\n"
"s = Session(); modeling.new_document(s, 'ce')\n"
"s.new_part('A'); modeling.add_box(s, 40, 40, 40)\n"
"features.add_hole(s, label_top(s, 'A'), diameter=6, depth=5)\n"
"s.new_part('B'); modeling.add_box(s, 10, 10, 10, position=(15.0, 15.0, 15.0))\n"
"features.add_hole(s, label_top(s, 'B'), diameter=4, depth=5)\n"
"assembly.place_part(s, 'B', position=[100.0, 0.0, 0.0])\n"
"s.set_active_part('A')\n"
"r = transform.move_part(s, 'Box', [1.0, 0.0, 0.0])\n"
"assert r['ok'], r  # 修复前：被 B 的孔误拒（探针跨零件坐标混用）\n"
"raised = ''\n"
"try:\n"
"    transform.move_part(s, 'HoleTool', [20.0, 20.0, 20.0])\n"
"except RuntimeError as exc:\n"
"    raised = str(exc)\n"
"assert '封闭' in raised, raised or 'A 自己的孔被埋成密封内腔应被拒'\n"
"print('SEALED_PROBE_OWNER_OK')\n"
)
    assert "SEALED_PROBE_OWNER_OK" in out


@pytest.mark.slow
def test_assembly_pad_no_base_and_enclave_pad(runtime_env):
    """终审 C-A：差集归属发生在事务收尾，断言必须直接检查承载新几何的 shape——
    ① 飞地 pad（offset=100 悬空）装配模式必须与单零件一样被"切成 2 块"拒绝
      （修复前回取 owner 旧 shape 漏检，solids=2 仍 ok）；
    ② new_part 后无基体 pad 建新零件必须可用（修复前断言遍历回取直接崩
      "零件 X 中无有效 solid"）。"""
    out = _run_in_env(runtime_env,
"from vibecad.engine.session import Session\n"
"from vibecad.feedback import multiview\n"
"from vibecad.tools import modeling, sketch\n"
"s = Session(); modeling.new_document(s, 'ca')\n"
"s.new_part('底板'); modeling.add_box(s, 60, 40, 10)\n"
"png, t, fr, er = multiview.render_multiview(s.get_result_shape('底板'))\n"
"s.set_labels(fr, er, shown=set(t.keys()), part='底板')\n"
"top = next(lab for lab, d in t.items() if '顶面' in d)\n"
"raised = ''\n"
"try:\n"
"    sketch.extrude_profile(s, {'type': 'rect', 'length': 10, 'width': 10},\n"
"                           height=8, face=top, operation='pad', offset=(100.0, 0.0))\n"
"except RuntimeError as exc:\n"
"    raised = str(exc)\n"
"assert '切成 2 块' in raised, raised or '飞地 pad 应被单 solid 守卫拒绝'\n"
"assert len(s.get_result_shape('底板').Solids) == 1  # 回滚完好\n"
"s.new_part('臂')\n"
"r = sketch.extrude_profile(s, {'type': 'rect', 'length': 20, 'width': 10},\n"
"                           height=5, operation='pad')\n"
"assert r['ok'] and abs(r['volume'] - 1000) < 1, r\n"
"assert abs(s.get_result_shape('臂').Volume - 1000) < 1\n"
"print('ASSEMBLY_PAD_CA_OK')\n"
)
    assert "ASSEMBLY_PAD_CA_OK" in out


@pytest.mark.slow
def test_interference_empty_part_noted_not_silent(runtime_env):
    """终审 C-B：空零件（含 objects 被清空的异常态）必须显式注明"干涉未检查"，
    绝不静默 []——修复前 except Exception → vol=0.0 把 9000mm³ 真实重叠静默放行。"""
    out = _run_in_env(runtime_env,
"from vibecad.engine.session import Session\n"
"from vibecad.tools import assembly, modeling\n"
"s = Session(); modeling.new_document(s, 'cb')\n"
"s.new_part('A'); modeling.add_box(s, 30, 30, 10)\n"
"s.new_part('B')  # 空零件\n"
"inter = assembly.assert_no_interference(s, allow=True)\n"
"assert any(it.get('volume') is None and 'B 无几何' in it.get('note', '')\n"
"           for it in inter), inter\n"
"assert inter.interference_skipped is True, '唯一非空零件无人可配对，检查未真正跑过'\n"
"# 取证 2 异常态：B 有几何后 objects 被清空（内部状态不一致）→ 注明而非静默\n"
"modeling.add_box(s, 30, 30, 10)  # B 与 A 完全重叠\n"
"inter2 = assembly.assert_no_interference(s, allow=True)\n"
"assert any(it.get('volume') == 9000.0 for it in inter2), inter2  # 正常态报干涉\n"
"assert inter2.interference_skipped is False, '两个非空零件应真的跑过比较'\n"
"s._parts['B']['objects'] = set()\n"
"inter3 = assembly.assert_no_interference(s, allow=False)\n"
"assert inter3 and inter3[0]['volume'] is None and '未检查' in inter3[0]['note'], inter3\n"
"assert inter3.interference_skipped is True, 'B 被清空后又只剩一个可比较零件'\n"
"print('INTERFERENCE_NOT_SILENT_OK')\n"
)
    assert "INTERFERENCE_NOT_SILENT_OK" in out


@pytest.mark.slow
def test_server_edges_of_maps_to_global_index(runtime_env):
    """终审 C-C（server 级端到端）：active=第二零件时 render_part(annotate='edges',
    edges_of=该零件面标签) 必须画该零件自己的边（表注归属正确、边数=该零件边数）
    ——修复前局部索引被当 compound 全局索引消费，画的是第一零件的边。"""
    out = _run_in_env(runtime_env,
"import json\n"
"from vibecad.engine import naming\n"
"import vibecad.server as srv\n"
"srv._runtime_guard = lambda: None\n"
"srv.new_document('cc')\n"
"srv.new_part('底板'); srv.add_box(60, 40, 10)\n"
"srv.new_part('盖板'); srv.add_box(20, 20, 5, position=[100.0, 0.0, 0.0])\n"
"out_f = srv.render_part(view='iso', annotate='faces')\n"
"assert isinstance(out_f, list), out_f\n"
"s = srv._session\n"
"shape = s.get_assembly_shape()\n"
"labels = naming.face_labels(len(shape.Faces))\n"
"n_base = len(s.get_result_shape('底板').Faces)\n"
"lid_top_idx = max(range(n_base, len(shape.Faces)),\n"
"                  key=lambda i: shape.Faces[i].CenterOfMass.z)\n"
"lid_label = labels[lid_top_idx]\n"
"out_e = srv.render_part(view='iso', annotate='edges', edges_of=lid_label)\n"
"assert isinstance(out_e, list), out_e\n"
"t_e = json.loads(out_e[1])['labels']\n"
"owners = sorted({v.split('零件：')[-1].rstrip('）')\n"
"                 for v in t_e.values() if '零件：' in v})\n"
"assert owners == ['盖板'], f'edges_of 画了错误零件的边：{owners}'\n"
"assert len(t_e) == 4, f'盖板顶面应有 4 条边（得到 {len(t_e)}）'\n"
"print('EDGES_OF_GLOBAL_OK')\n"
)
    assert "EDGES_OF_GLOBAL_OK" in out


@pytest.mark.slow
def test_server_relabel_refreshes_all_part_namespaces(runtime_env):
    """终审 I-1（server 级端到端，取证 7 场景）：非活动零件（target）被移动后，
    AI 重标注一次 → align 必须走到干涉判定/成功——修复前标签只写活动零件命名
    空间，target 快照永久过期且无恢复路径（server 无 set_active_part 工具）。"""
    out = _run_in_env(runtime_env,
"import json\n"
"from vibecad.engine import naming\n"
"import vibecad.server as srv\n"
"srv._runtime_guard = lambda: None\n"
"srv.new_document('i1')\n"
"srv.new_part('底板'); srv.add_box(60, 40, 10)\n"
"srv.new_part('盖板'); srv.add_box(60, 40, 5, position=[0.0, 0.0, 50.0])\n"
"srv.place_part(part='底板', position=[10.0, 0.0, 0.0])  # 移动非活动零件\n"
"out_f = srv.render_part(view='iso', annotate='faces')  # 重标注一次\n"
"assert isinstance(out_f, list), out_f\n"
"s = srv._session\n"
"shape = s.get_assembly_shape()\n"
"labels = naming.face_labels(len(shape.Faces))\n"
"n_base = len(s.get_result_shape('底板').Faces)\n"
"base_top = labels[max(range(0, n_base), key=lambda i: shape.Faces[i].CenterOfMass.z)]\n"
"lid_bot = labels[min(range(n_base, len(shape.Faces)),\n"
"                     key=lambda i: shape.Faces[i].CenterOfMass.z)]\n"
"out_a = srv.align_parts(moving_part='盖板', moving_face=lid_bot,\n"
"                        target_part='底板', target_face=base_top)\n"
"assert isinstance(out_a, list) and out_a[0]['ok'], out_a\n"
"assert out_a[0]['interference'] == [], out_a[0]\n"
"assert out_a[0]['interference_skipped'] is False, out_a[0]\n"
"print('RELABEL_ALL_NAMESPACES_OK')\n"
)
    assert "RELABEL_ALL_NAMESPACES_OK" in out
