"""Task 4 Step 4 集成：真实执行待删清理后，supervisor 的解释器选择落 bootstrap。

独立成文件：test_launcher.py 的 autouse fixture 把监督进程整体 stub 掉，
本测试恰恰要走真实的 perform_pending_uninstall + _server_cmd 三态判据
（只 mock Supervisor.run 阻断真 spawn，并在其中固化当时的解释器选择）。
"""
import sys

import pytest

from vibecad import launcher, supervisor
from vibecad.runtime import status


def test_pending_uninstall_real_delete_then_bootstrap(monkeypatch, tmp_path):
    home = tmp_path / "home"
    env = home / "mamba" / "envs" / "vibecad"
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_text("")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    status.write_runtime_receipt()  # 删除前精确就绪：不删则 _server_cmd 必选 conda
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
    assert not home.exists()  # 标记生效：home 整目录真实删除
    # 哨兵随目录消失，_server_cmd 安全落 bootstrap（当前解释器），绝不指向已删的 conda python
    assert routed["cmd"] == [sys.executable, "-m", "vibecad.server"]
