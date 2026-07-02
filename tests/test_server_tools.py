import vibecad.server as srv


def test_ping_has_version():
    from vibecad import __version__
    assert __version__ in srv.ping()


def test_status_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))  # m-6：hermetic，不读真实 home
    d = srv.get_runtime_status()
    assert {"phase", "percent", "message", "error", "needs_reconnect"} <= set(d)


def test_ensure_ready(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    # Round 11：测试进程本身是引导解释器，ready 分支会安排自退——mock 掉以免真退出
    monkeypatch.setattr(srv, "_schedule_swap", lambda delay=1.0: None)
    assert srv._ensure_runtime_impl()["status"] == "ready"


def test_ensure_starts_bg(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: False)
    started = {}
    monkeypatch.setattr(srv, "_spawn_install", lambda: started.setdefault("bg", True))
    assert srv._ensure_runtime_impl()["status"] == "started"
    assert started["bg"]


def test_smoke_guard_not_ready(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: False)
    out = srv.smoke_cad()
    assert out["ok"] is False and "未就绪" in out["message"]


def test_smoke_guard_bootstrap_schedules_swap(monkeypatch):
    """Round 11：ready+bootstrap 不再要求手动重连，而是安排自退换芯后结构化拒绝。"""
    monkeypatch.setenv("VIBECAD_SUPERVISED", "1")            # I4：受监督才允许自杀
    monkeypatch.setattr(srv, "runtime_swappable", lambda: True)  # C1：判据通过
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: False)
    calls = []
    monkeypatch.setattr(srv, "_schedule_swap", lambda delay=1.0: calls.append(delay))
    out = srv.smoke_cad()
    assert out["ok"] is False and "自动切换" in out["message"]
    assert len(calls) == 1


def test_prepare_freecad_import_adds_module_dir(monkeypatch, tmp_path):
    # A1：<prefix>/lib（Windows: Library/bin）应被注入 sys.path
    monkeypatch.setattr(srv.sys, "prefix", str(tmp_path))
    saved = list(srv.sys.path)
    try:
        srv._prepare_freecad_import()
        import os as _os
        target = (
            _os.path.join(str(tmp_path), "Library", "bin")
            if srv.sys.platform == "win32"
            else _os.path.join(str(tmp_path), "lib")
        )
        assert target in srv.sys.path
    finally:
        srv.sys.path[:] = saved
