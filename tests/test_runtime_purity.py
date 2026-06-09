import subprocess
import sys


def test_runtime_imports_without_mcp():
    # 模拟 launcher 在无 mcp 的临时 env：import runtime 子模块不得拉 mcp
    code = (
        "import sys, importlib;"
        "import vibecad.runtime.paths, vibecad.runtime.status, vibecad.runtime.platform,"
        " vibecad.runtime.micromamba, vibecad.runtime.installer;"
        "assert 'mcp' not in sys.modules, 'runtime 不应拉起 mcp';"
        "print('pure-stdlib OK')"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "pure-stdlib OK" in r.stdout
