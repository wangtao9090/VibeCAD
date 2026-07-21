import json
import os

import pytest

from vibecad.runtime import paths


def test_home_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "vc"))
    assert paths.vibecad_home() == tmp_path / "vc"


def test_env_layout_unix(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(paths.platform, "is_windows", lambda: False)
    runtime = tmp_path / "runtime"
    assert paths.runtime_root() == runtime
    assert paths.env_prefix() == runtime / "mamba" / "envs" / "vibecad"
    assert paths.env_python() == runtime / "mamba" / "envs" / "vibecad" / "bin" / "python"
    assert paths.ready_sentinel() == runtime / "mamba" / "envs" / "vibecad" / ".vibecad_ready"
    assert paths.status_file() == runtime / "status.json"
    assert paths.install_lock() == runtime / ".install.lock"
    assert paths.maintenance_lock() == tmp_path / ".runtime-maintenance.lock"
    assert paths.removal_record() == tmp_path / ".runtime-removal.json"
    assert paths.install_log() == runtime / "install.log"


def test_durable_data_layout_is_sibling_of_replaceable_runtime(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))

    assert paths.data_root() == home / "data"
    assert paths.lease_root() == home / "data" / "locks"
    assert paths.task_store_root() == home / "data" / "tasks"
    assert paths.revision_store_root() == home / "data" / "projects"
    assert paths.bootstrap_root() == home / "data" / "bootstrap"
    assert paths.checkout_root() == home / "data" / "checkouts"
    assert paths.legacy_env_prefix() == home / "mamba" / "envs" / "vibecad"
    assert paths.external_runtime_receipt() == home / "runtime" / "external-runtime.json"


def test_env_python_windows(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(paths.platform, "is_windows", lambda: True)
    assert paths.env_python().name == "python.exe"
    assert paths.micromamba_path().name == "micromamba.exe"
    env = tmp_path / "runtime" / "mamba" / "envs" / "vibecad"
    assert paths.freecadcmd_path() == env / "Library" / "bin" / "FreeCADCmd.exe"


def test_active_runtime_prefers_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(paths.platform, "is_windows", lambda: False)
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(tmp_path / "myenv"))
    assert paths.active_runtime_prefix() == tmp_path / "myenv"
    assert paths.active_runtime_python() == tmp_path / "myenv" / "bin" / "python"
    assert paths.ready_sentinel() == tmp_path / "runtime" / "external-runtime.json"


def test_freecadcmd_honors_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(paths.platform, "is_windows", lambda: False)
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(tmp_path / "myenv"))
    assert paths.freecadcmd_path() == tmp_path / "myenv" / "bin" / "freecadcmd"


def test_freecadcmd_honors_override_windows(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(paths.platform, "is_windows", lambda: True)
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(tmp_path / "myenv"))
    assert paths.freecadcmd_path() == tmp_path / "myenv" / "Library" / "bin" / "FreeCADCmd.exe"


def test_bound_external_prefix_rejects_fifo_receipt_without_blocking(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "home"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    os.mkfifo(runtime / "external-runtime.json", 0o600)
    monkeypatch.setenv("VIBECAD_HOME", str(home))

    assert paths.bound_external_prefix() is None


@pytest.mark.parametrize("windows", [False, True])
def test_bound_external_prefix_accepts_identity_bound_regular_receipt(
    monkeypatch,
    tmp_path,
    windows,
):
    home = tmp_path / "home"
    runtime = home / "runtime"
    prefix = tmp_path / "external"
    runtime.mkdir(parents=True)
    prefix.mkdir()
    info = prefix.stat()
    (runtime / "external-runtime.json").write_text(
        json.dumps(
            {
                "prefix": str(prefix),
                "prefix_device": info.st_dev,
                "prefix_inode": info.st_ino,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setattr(paths.platform, "is_windows", lambda: windows)

    assert paths.bound_external_prefix() == prefix
