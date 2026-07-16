import vibecad.server as srv
from vibecad.runtime import status


def test_ping_has_version():
    from vibecad import __version__
    assert __version__ in srv.ping()


def test_status_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))  # m-6：hermetic，不读真实 home
    d = srv.get_runtime_status()
    assert {"phase", "percent", "message", "error", "needs_reconnect"} <= set(d)


def test_stale_ready_status_reports_lightweight_upgrade_only_when_safe(monkeypatch):
    monkeypatch.setattr(
        srv.status,
        "read_status",
        lambda: status.RuntimeStatus(phase=status.Phase.READY),
    )
    monkeypatch.setattr(srv.status, "runtime_ready", lambda: False)
    monkeypatch.setattr(srv.status, "read_runtime_receipt", lambda: {"vibecad_version": "0.3.0"})
    monkeypatch.setattr(
        srv.status,
        "runtime_recovery_kind",
        lambda: status.RecoveryKind.UPGRADE_REQUIRED,
    )

    out = srv.get_runtime_status()

    assert out["phase"] == "upgrade_required"
    assert out["runtime_action"] == "upgrade_required"
    assert "不会重新下载 FreeCAD" in out["message"]


def test_stale_ready_status_reports_repair_when_env_is_not_compatible(monkeypatch):
    monkeypatch.setattr(
        srv.status,
        "read_status",
        lambda: status.RuntimeStatus(phase=status.Phase.READY),
    )
    monkeypatch.setattr(srv.status, "runtime_ready", lambda: False)
    monkeypatch.setattr(srv.status, "read_runtime_receipt", lambda: None)
    monkeypatch.setattr(
        srv.status,
        "runtime_recovery_kind",
        lambda: status.RecoveryKind.REPAIR_REQUIRED,
    )

    out = srv.get_runtime_status()

    assert out["phase"] == "repair_required"
    assert out["runtime_action"] == "repair_required"
    assert "2-3GB" in out["message"]


def test_runtime_guard_ready_but_incompatible_distinguishes_upgrade_and_repair(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: False)
    monkeypatch.setattr(
        srv.status,
        "read_status",
        lambda: status.RuntimeStatus(phase=status.Phase.READY),
    )

    monkeypatch.setattr(
        srv.status,
        "runtime_recovery_kind",
        lambda: status.RecoveryKind.UPGRADE_REQUIRED,
    )
    upgrade = srv._runtime_guard()
    assert upgrade["phase"] == "upgrade_required"
    assert "不会重新下载 FreeCAD" in upgrade["message"]

    monkeypatch.setattr(
        srv.status,
        "runtime_recovery_kind",
        lambda: status.RecoveryKind.REPAIR_REQUIRED,
    )
    repair = srv._runtime_guard()
    assert repair["phase"] == "repair_required"
    assert "2-3GB" in repair["message"]


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
    monkeypatch.setattr(
        srv.status,
        "read_status",
        lambda: status.RuntimeStatus(phase=status.Phase.NOT_STARTED),
    )
    out = srv.smoke_cad()
    assert out["ok"] is False and "ensure_runtime" in out["message"]


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
