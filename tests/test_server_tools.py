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


def test_smoke_guard_needs_reconnect(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: False)
    out = srv.smoke_cad()
    assert out["ok"] is False and "重连" in out["message"]


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
