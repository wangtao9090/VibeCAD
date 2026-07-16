import json

import pytest

from vibecad.runtime import spec, status


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


def _managed_paths(monkeypatch, tmp_path):
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    python = status.paths.active_runtime_python()
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()
    return status.paths.ready_sentinel(), python


def test_runtime_ready_requires_current_json_receipt_and_python(monkeypatch, tmp_path):
    sentinel, python = _managed_paths(monkeypatch, tmp_path)
    assert status.runtime_ready() is False

    status.write_runtime_receipt()
    assert status.read_runtime_receipt() == spec.expected_receipt()
    assert status.runtime_receipt_state() is status.ReceiptState.CURRENT
    assert status.runtime_ready() is True

    python.unlink()
    assert status.runtime_ready() is False
    assert sentinel.exists()  # Python 缺失不能因 receipt 仍在而误判就绪


def test_legacy_receipt_requires_server_sync(monkeypatch, tmp_path):
    sentinel, _ = _managed_paths(monkeypatch, tmp_path)
    sentinel.write_text(spec.FREECAD_PIN, encoding="utf-8")
    assert status.runtime_receipt_state() is status.ReceiptState.LEGACY
    assert status.read_runtime_receipt() is None
    assert status.runtime_ready() is False


def test_server_version_mismatch_is_not_ready(monkeypatch, tmp_path):
    sentinel, _ = _managed_paths(monkeypatch, tmp_path)
    receipt = spec.expected_receipt()
    receipt["vibecad_version"] = "0.3.0"
    sentinel.write_text(json.dumps(receipt), encoding="utf-8")
    assert status.runtime_receipt_state() is status.ReceiptState.SERVER_MISMATCH
    assert status.runtime_ready() is False


def test_corrupt_or_engine_mismatch_receipt_is_incompatible(monkeypatch, tmp_path):
    sentinel, _ = _managed_paths(monkeypatch, tmp_path)
    sentinel.write_text("{broken", encoding="utf-8")
    assert status.runtime_receipt_state() is status.ReceiptState.INCOMPATIBLE
    assert status.runtime_ready() is False

    receipt = spec.expected_receipt()
    receipt["freecad_pin"] = "freecad=9.9"
    sentinel.write_text(json.dumps(receipt), encoding="utf-8")
    assert status.runtime_receipt_state() is status.ReceiptState.INCOMPATIBLE
    assert status.runtime_ready() is False


def test_write_runtime_receipt_uses_atomic_replace(monkeypatch, tmp_path):
    sentinel, _ = _managed_paths(monkeypatch, tmp_path)
    replaced = []
    real_replace = status.os.replace

    def record_replace(src, dst):
        replaced.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(status.os, "replace", record_replace)
    status.write_runtime_receipt()

    assert replaced and replaced[0][1] == sentinel
    assert replaced[0][0] != sentinel
    assert json.loads(sentinel.read_text(encoding="utf-8")) == spec.expected_receipt()
    assert not replaced[0][0].exists()


def test_health_snippet_has_win_dll_prep():
    # M4: -c 片段在 import 前注入 PATH 兜底
    assert "Library" in status._HEALTH_SNIPPET and "import FreeCAD" in status._HEALTH_SNIPPET


def test_verify_snippet_requires_exact_vibecad_version():
    assert "vibecad.__version__" in status._VERIFY_SNIPPET
    assert spec.VIBECAD_VERSION in status._VERIFY_SNIPPET
    assert "raise RuntimeError" in status._VERIFY_SNIPPET
    assert "assert vibecad.__version__" not in status._VERIFY_SNIPPET


def test_engine_and_verify_snippets_enforce_exact_pins_without_assert():
    assert repr(spec.PYTHON_VERSION) in status._ENGINE_SNIPPET
    assert repr(spec.FREECAD_VERSION) in status._ENGINE_SNIPPET
    assert "sys.version_info[:2]" in status._VERIFY_SNIPPET
    assert "FreeCAD.Version()[:3]" in status._VERIFY_SNIPPET
    assert "raise RuntimeError" in status._ENGINE_SNIPPET
    assert "assert " not in status._ENGINE_SNIPPET


def test_runtime_recovery_kind_is_conservative(monkeypatch, tmp_path):
    sentinel, python = _managed_paths(monkeypatch, tmp_path)

    status.write_runtime_receipt()
    assert status.runtime_recovery_kind() is status.RecoveryKind.READY

    receipt = spec.expected_receipt()
    receipt["vibecad_version"] = "0.3.0"
    sentinel.write_text(json.dumps(receipt), encoding="utf-8")
    assert status.runtime_recovery_kind() is status.RecoveryKind.UPGRADE_REQUIRED

    sentinel.write_text("{broken", encoding="utf-8")
    assert status.runtime_recovery_kind() is status.RecoveryKind.REPAIR_REQUIRED

    sentinel.unlink()
    assert status.runtime_recovery_kind() is status.RecoveryKind.REPAIR_REQUIRED

    status.write_runtime_receipt()
    python.unlink()
    assert status.runtime_recovery_kind() is status.RecoveryKind.REPAIR_REQUIRED


@pytest.mark.parametrize("legacy", [False, True])
def test_external_runtime_never_promises_automatic_server_upgrade(
    monkeypatch, tmp_path, legacy,
):
    override = tmp_path / "external"
    python = status.paths.env_python_for(override)
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(override))
    sentinel = status.paths.ready_sentinel()
    if legacy:
        sentinel.write_text(spec.FREECAD_PIN, encoding="utf-8")
    else:
        receipt = spec.expected_receipt(external=True)
        receipt["vibecad_version"] = "0.3.0"
        sentinel.write_text(json.dumps(receipt), encoding="utf-8")

    assert status.runtime_recovery_kind() is status.RecoveryKind.REPAIR_REQUIRED


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
