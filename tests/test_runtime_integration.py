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
