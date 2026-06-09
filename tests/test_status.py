from vibecad.runtime import status


def test_status_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    s = status.RuntimeStatus(phase=status.Phase.CREATING_ENV, percent=20.0, message="建环境")
    status.write_status(s)
    got = status.read_status()
    assert got.phase is status.Phase.CREATING_ENV and got.percent == 20.0
    assert status.read_status().to_dict()["message"] == "建环境"


def test_read_status_default_when_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    assert status.read_status().phase is status.Phase.NOT_STARTED


def test_runtime_ready_reads_sentinel(monkeypatch, tmp_path):
    sentinel = tmp_path / ".vibecad_ready"
    monkeypatch.setattr(status.paths, "ready_sentinel", lambda: sentinel)
    assert status.runtime_ready() is False
    sentinel.write_text("freecad=1.1.0")
    assert status.runtime_ready() is True


def test_health_snippet_has_win_dll_prep():
    # M4: -c 片段在 import 前注入 PATH 兜底
    assert "Library" in status._HEALTH_SNIPPET and "import FreeCAD" in status._HEALTH_SNIPPET


def test_health_check_false_when_python_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(status.paths, "active_runtime_python", lambda: tmp_path / "nope")
    assert status.health_check() is False


def test_file_lock_exclusive_and_reentrant(tmp_path):
    lock = status.FileLock(tmp_path / "lock")
    with lock.acquire():
        assert status.FileLock(tmp_path / "lock").try_acquire() is False
    assert status.FileLock(tmp_path / "lock").try_acquire() is True


def test_file_lock_reclaims_dead_pid(tmp_path, monkeypatch):
    lock_dir = tmp_path / "lock"
    lock = status.FileLock(lock_dir)
    assert lock.try_acquire() is True            # 留下 owner.json（本进程 pid）
    monkeypatch.setattr(status, "_pid_alive", lambda pid: False)  # 模拟持锁进程已死
    assert status.FileLock(lock_dir).try_acquire() is True        # 回收陈旧锁


def test_pid_alive_self_and_dead():
    import os  # B-1：跨平台探活（Windows 用 OpenProcess 而非杀进程的 os.kill）
    assert status._pid_alive(os.getpid()) is True
    assert status._pid_alive(2_000_000_000) is False
    assert status._pid_alive(None) is False


def test_prep_injects_freecad_module_path():
    # A1：conda-forge 把 FreeCAD.so 放 <prefix>/lib（Windows: Library/bin），须注入 sys.path
    assert "sys.path" in status._PREP
    assert "lib" in status._PREP
