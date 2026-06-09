import os
import subprocess

import pytest

from vibecad.runtime import status
from vibecad.tools import export

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")


class _FakeShape:
    def __init__(self, calls):
        self.calls = calls

    def exportStep(self, p):
        self.calls.append(("step", p))

    def exportStl(self, p):
        self.calls.append(("stl", p))


class _MockSession:
    def __init__(self):
        self.calls = []
        self._shape = _FakeShape(self.calls)

        class _Doc:
            Name = "Mock"

        self.doc = _Doc()

    def get_result_shape(self):
        return self._shape


def test_export_rejects_invalid_fmt(tmp_path):
    with pytest.raises(ValueError, match="fmt"):
        export.export_part(_MockSession(), str(tmp_path), fmt="dxf")


def test_export_both_writes_step_and_stl(tmp_path):
    s = _MockSession()
    r = export.export_part(s, str(tmp_path), fmt="both")
    assert r["ok"] is True
    assert r["step"].endswith("Mock.step")
    assert r["stl"].endswith("Mock.stl")
    assert ("step", r["step"]) in s.calls and ("stl", r["stl"]) in s.calls


def test_export_step_only(tmp_path):
    s = _MockSession()
    r = export.export_part(s, str(tmp_path), fmt="step")
    assert r["step"] is not None and r["stl"] is None


@pytest.mark.slow
def test_export_real_files(runtime_env, tmp_path):
    out = str(tmp_path)
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from pathlib import Path\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling, export\n"
        + "s = Session(); modeling.new_document(s, 'Exp')\n"
        + "modeling.add_box(s, 20, 20, 20)\n"
        + f"r = export.export_part(s, {out!r})\n"
        + "assert r['ok']\n"
        + "assert Path(r['step']).exists() and Path(r['step']).stat().st_size > 0\n"
        + "assert Path(r['stl']).exists() and Path(r['stl']).stat().st_size > 0\n"
        + "print('EXPORT_OK')\n"
    )
    p = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    assert "EXPORT_OK" in p.stdout
