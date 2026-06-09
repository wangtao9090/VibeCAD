import os
import subprocess

import pytest

from vibecad.runtime import status

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")


@pytest.mark.slow
def test_export_gltf_real_box(runtime_env, tmp_path):
    out = str(tmp_path / "box.glb")
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from pathlib import Path\n"
        + "from pygltflib import GLTF2\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling\n"
        + "from vibecad.feedback.gltf import export_gltf\n"
        + "s = Session(); modeling.new_document(s, 'G')\n"
        + "r = modeling.add_box(s, 10, 10, 10)\n"
        + f"p = export_gltf(s.get_object(r['name']).Shape, {out!r})\n"
        + "assert Path(p).stat().st_size > 0\n"
        + "g = GLTF2().load(p)\n"
        + "assert len(g.meshes[0].primitives) == 6, len(g.meshes[0].primitives)\n"
        + "print('GLTF_OK')\n"
    )
    pr = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert pr.returncode == 0, pr.stderr
    assert "GLTF_OK" in pr.stdout
