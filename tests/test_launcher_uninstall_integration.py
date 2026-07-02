"""Task 4 Step 4 集成：真实执行待删清理后，三态判断落 bootstrap 而非 re-exec。

独立成文件：test_launcher.py 的 autouse fixture 把 paths/status 三态判断整体 stub 掉，
本测试恰恰要走真实的 perform_pending_uninstall + active_runtime_python + runtime_ready。
"""
from vibecad import launcher


def test_pending_uninstall_real_delete_then_bootstrap(monkeypatch, tmp_path):
    home = tmp_path / "home"
    env = home / "mamba" / "envs" / "vibecad"
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_text("")
    (env / ".vibecad_ready").write_text("")  # 删除前哨兵就绪：不删则必走 re-exec
    (home / ".uninstall_requested").touch()
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    monkeypatch.setattr(launcher.sys, "argv", ["vibecad"])
    monkeypatch.setattr(launcher.sys, "executable", "/uv/tmp/python")
    routed = {}
    monkeypatch.setattr(launcher, "_run_server", lambda: routed.setdefault("bootstrap", True))
    monkeypatch.setattr(launcher, "_reexec_into", lambda p: routed.setdefault("reexec", p))

    launcher.main()

    assert not home.exists()  # 标记生效：home 整目录真实删除
    assert routed == {"bootstrap": True}  # 哨兵随目录消失，三态判断安全落 bootstrap
