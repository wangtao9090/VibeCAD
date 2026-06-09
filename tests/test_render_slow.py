import os
import subprocess

import pytest

from vibecad.runtime import status

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")


@pytest.mark.slow
def test_render_png_real_box(runtime_env):
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling\n"
        + "from vibecad.feedback.render import render_png\n"
        + "s = Session(); modeling.new_document(s, 'R')\n"
        + "r = modeling.add_box(s, 20, 20, 20)\n"
        + "png = render_png(s.get_object(r['name']).Shape)\n"
        + "assert png[:4] == b'\\x89PNG', png[:8]\n"
        + "assert len(png) > 2000, len(png)\n"
        + "print('RENDER_OK')\n"
    )
    p = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    assert "RENDER_OK" in p.stdout
