"""真实安装并验证 A1/A2/A3（慢，下载 2-3GB）。
运行：VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow tests/test_runtime_integration.py -v -s
"""
import os
import subprocess

import pytest

from vibecad.runtime import paths, status
from vibecad.runtime.installer import RuntimeInstaller

pytestmark = pytest.mark.slow
_RUN = os.environ.get("VIBECAD_RUN_INTEGRATION") == "1"
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")


@pytest.mark.skipif(not _RUN, reason="set VIBECAD_RUN_INTEGRATION=1")
def test_install_and_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "vchome"))
    monkeypatch.setenv("VIBECAD_PIP_SPEC", _REPO)  # 绝对路径装本地源（M8）
    phases = []
    RuntimeInstaller(on_progress=lambda s: phases.append(s.phase.value)).install()  # A2
    assert status.runtime_ready() is True
    assert status.health_check(paths.env_python()) is True  # A1（subprocess 级）

    # A1 进程内 import 经 conda env python 子进程执行后回读（不在 pytest 进程 import）
    step = str(tmp_path / "vc_smoke.step")
    code = (
        status._PREP +  # M-C：Windows DLL 兜底，否则 win CI 集成 import 必红
        "import FreeCAD, Part; b=Part.makeBox(10,10,10);"
        f"assert abs(b.Volume-1000.0)<1e-6; b.exportStep({step!r}); print('OK')"
    )
    r = subprocess.run([str(paths.env_python()), "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert os.path.exists(step)
    assert phases[-1] == "ready"


def test_walking_skeleton(runtime_env, tmp_path):
    """端到端 Walking Skeleton：建模→布尔→导出→诊断（真实 FreeCAD）。"""
    out = str(tmp_path)
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "import os\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling, export\n"
        + "from vibecad.feedback.text import describe_shape\n"
        + "s = Session(); modeling.new_document(s, 'WalkingSkeleton')\n"
        + "box = modeling.add_box(s, 30, 30, 30)\n"
        + "assert box['ok'] and abs(box['volume'] - 27000) < 1e-2, box\n"
        + "cyl = modeling.add_cylinder(s, 8, 40)\n"
        + "assert cyl['ok'] and cyl['volume'] > 0\n"
        + "cut = modeling.boolean_cut(s, box['name'], cyl['name'])\n"
        + "assert cut['ok'] and 0 < cut['volume'] < 27000, cut\n"
        + f"r = export.export_part(s, {out!r})\n"
        + "assert os.path.getsize(r['step']) > 0 and os.path.getsize(r['stl']) > 0\n"
        + "d = describe_shape(s.get_object(cut['name']).Shape)\n"
        + "assert d['valid'] and d['solid_count'] == 1\n"
        + "assert abs(d['volume'] - cut['volume']) < 1e-4\n"
        + "print('SKELETON_OK')\n"
    )
    p = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    assert "SKELETON_OK" in p.stdout


def test_render_and_gltf(runtime_env, tmp_path):
    """端到端视觉：建模→布尔→render_png(PNG)→export_gltf(glb)（真实 FreeCAD）。"""
    glb = str(tmp_path / "p.glb")
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from pathlib import Path\n"
        + "from pygltflib import GLTF2\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling\n"
        + "from vibecad.feedback.render import render_png\n"
        + "from vibecad.feedback.gltf import export_gltf\n"
        + "s = Session(); modeling.new_document(s, 'Visual')\n"
        + "b = modeling.add_box(s, 30, 20, 10)\n"
        + "c = modeling.add_cylinder(s, 5, 30)\n"
        + "cut = modeling.boolean_cut(s, b['name'], c['name'])\n"
        + "shape = s.get_object(cut['name']).Shape\n"
        + "png = render_png(shape, view='iso')\n"
        + "assert png[:4] == b'\\x89PNG' and len(png) > 2000, len(png)\n"
        + f"gp = export_gltf(shape, {glb!r})\n"
        + "g = GLTF2().load(gp)\n"
        + "assert Path(gp).stat().st_size > 0 and len(g.meshes[0].primitives) > 0\n"
        + "print('VISUAL_OK')\n"
    )
    pr = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert pr.returncode == 0, pr.stderr
    assert "VISUAL_OK" in pr.stdout


def test_positioned_part(runtime_env, tmp_path):
    """端到端：position 参数造居中贯穿孔 + 渲染（真实 FreeCAD）。"""
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "import math\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling\n"
        + "from vibecad.feedback.render import render_png\n"
        + "s = Session(); modeling.new_document(s, 'Plate')\n"
        + "b = modeling.add_box(s, 40, 30, 20)\n"
        + "c = modeling.add_cylinder(s, 6, 40, position=(20, 15, -10), axis='z')\n"
        + "cut = modeling.boolean_cut(s, b['name'], c['name'])\n"
        + "expected = 24000 - math.pi * 36 * 20\n"  # 居中贯穿 → 挖掉 box 内整段圆柱
        + "assert abs(cut['volume'] - expected) < 40, (cut['volume'], expected)\n"
        + "png = render_png(s.get_object(cut['name']).Shape)\n"
        + "assert png[:4] == b'\\x89PNG' and len(png) > 2000\n"
        + "print('POSITIONED_OK')\n"
    )
    pr = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert pr.returncode == 0, pr.stderr
    assert "POSITIONED_OK" in pr.stdout
