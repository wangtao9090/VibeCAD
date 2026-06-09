import os

import pytest


@pytest.fixture(scope="session")
def runtime_env():
    """就绪的 conda env python 路径；复用既有 env，必要时只装一次。
    优先级：VIBECAD_FREECAD_ENV override > 固定缓存 VIBECAD_HOME=<repo>/.vibecad-test-runtime。"""
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run slow integration tests")
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    if not os.environ.get("VIBECAD_FREECAD_ENV"):
        os.environ.setdefault("VIBECAD_HOME", str(repo / ".vibecad-test-runtime"))
        os.environ.setdefault("VIBECAD_PIP_SPEC", str(repo))  # vibecad 未发布 PyPI，装本地源
    from vibecad.runtime import paths, status
    from vibecad.runtime.installer import RuntimeInstaller

    if not status.runtime_ready():
        RuntimeInstaller().install()  # 幂等：sentinel 就绪则秒回
    assert status.runtime_ready()
    return str(paths.active_runtime_python())
