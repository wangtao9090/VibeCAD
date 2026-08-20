"""Task 4 Step 4 集成：真实执行待删清理后，supervisor 的解释器选择落 bootstrap。

独立成文件：test_launcher.py 的 autouse fixture 把监督进程整体 stub 掉，
本测试恰恰要走真实的 perform_pending_uninstall + _server_cmd 三态判据
（只 mock Supervisor.run 阻断真 spawn，并在其中固化当时的解释器选择）。
"""

import os
import sys

import pytest

from vibecad import launcher, supervisor
from vibecad._file_compat import ensure_private_directory, open_private_file
from vibecad.runtime import paths, status


def test_pending_uninstall_real_delete_then_bootstrap(monkeypatch, tmp_path):
    home = tmp_path / "home"
    env = home / "runtime" / "mamba" / "envs" / "vibecad"
    data = home / "data" / "projects"
    durable = data / "HEAD"
    if sys.platform == "win32":
        home_capability = ensure_private_directory(home)
        current = home
        current_capability = home_capability
        for part in ("runtime", "mamba", "envs", "vibecad"):
            current /= part
            current_capability = ensure_private_directory(
                current,
                expected_parent=current_capability,
            )
        python = paths.env_python_for(env)
        python_fd, _python_capability = open_private_file(
            python,
            expected_parent=current_capability,
        )
        os.close(python_fd)
        current = home
        current_capability = home_capability
        for part in ("data", "projects"):
            current /= part
            current_capability = ensure_private_directory(
                current,
                expected_parent=current_capability,
            )
        durable_fd, _durable_capability = open_private_file(
            durable,
            expected_parent=current_capability,
        )
        os.write(durable_fd, b"do not delete")
        os.close(durable_fd)
    else:
        paths.env_python_for(env).parent.mkdir(parents=True)
        paths.env_python_for(env).write_text("")
        data.mkdir(parents=True)
        durable.write_bytes(b"do not delete")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    status.write_runtime_receipt()  # 删除前精确就绪：不删则 _server_cmd 必选 conda
    if sys.platform == "win32":
        marker_fd, _marker_capability = open_private_file(
            home / ".uninstall_requested",
            expected_parent=home_capability,
        )
        os.close(marker_fd)
    else:
        (home / ".uninstall_requested").touch()
    monkeypatch.delenv("VIBECAD_SUPERVISOR_TEST_CMD", raising=False)
    monkeypatch.setattr(launcher.sys, "argv", ["vibecad"])
    routed = {}

    def _fake_run(self):
        routed["cmd"] = supervisor._server_cmd()  # 固化清理后的解释器选择，不真 spawn
        return 0

    monkeypatch.setattr(supervisor.Supervisor, "run", _fake_run)

    with pytest.raises(SystemExit) as exc:
        launcher.main()

    assert exc.value.code == 0
    assert home.exists() and not (home / "runtime").exists()
    assert durable.read_bytes() == b"do not delete"
    # 哨兵随目录消失，_server_cmd 安全落 bootstrap（当前解释器），绝不指向已删的 conda python
    assert routed["cmd"] == [sys.executable, "-m", "vibecad.server"]
