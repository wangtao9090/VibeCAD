import hashlib
import json
import runpy
import stat
import threading
import zipfile
from pathlib import Path

import pytest

from vibecad.runtime import installer as inst
from vibecad.runtime import spec
from vibecad.runtime.status import Phase

_FAKE_MICROMAMBA = b"#!/bin/sh\nexit 0\n"
_REAL_VERIFY_GENERATION = inst.status.verify_runtime_generation
_REAL_ENGINE_GENERATION = inst.status.engine_compatible_generation


@pytest.fixture(autouse=True)
def _offline_capability_download(monkeypatch):
    """Every installer test is offline unless it installs a stricter FD seam."""

    digest = hashlib.sha256(_FAKE_MICROMAMBA).hexdigest()
    monkeypatch.setattr(inst, "_expected_micromamba_sha256", lambda subdir: digest)

    def download_to_fd(_url, file_descriptor):
        inst.os.write(file_descriptor, _FAKE_MICROMAMBA)
        return digest

    monkeypatch.setattr(inst, "_download_micromamba_to_fd", download_to_fd)
    monkeypatch.setattr(
        inst.micromamba,
        "_fetch_text",
        lambda *args, **kwargs: pytest.fail("installer tests must not access the network"),
    )
    monkeypatch.setattr(
        inst.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("installer tests must not access the network"),
    )
    # Existing unit tests control probe outcomes through the historical path
    # seams.  Capability-specific tests below restore the real implementations.
    monkeypatch.setattr(
        inst.status,
        "verify_runtime_generation",
        lambda evidence: inst.status.verify_runtime(evidence.python),
    )
    monkeypatch.setattr(
        inst.status,
        "engine_compatible_generation",
        lambda evidence: inst.status.engine_compatible(evidence.python),
    )


def _tree_fingerprint(root: Path) -> dict[str, tuple[int, int, int, str | None]]:
    """Capture entry identity and bytes without following symlinks."""
    found = {}
    if not root.exists():
        return found
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        digest = None
        if stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        found[str(path.relative_to(root))] = (
            info.st_ino,
            stat.S_IFMT(info.st_mode),
            info.st_size,
            digest,
        )
    return found


def _materialize_created_env(command: list) -> None:
    """Make a mocked micromamba create leave the prefix it promises."""

    if len(command) < 2 or command[1] != "create":
        return
    raw_prefix = command[command.index("-p") + 1]
    prefix = inst.paths.env_prefix() if raw_prefix in {".", "./"} else Path(raw_prefix)
    python = inst.paths.env_python_for(prefix)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()


def _materialize_micromamba(destination: Path, calls: list | None = None) -> Path:
    """Make a mocked successful download leave one regular binary."""

    if calls is not None:
        calls.append(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"mock micromamba")
    return destination


def _install_capability_runner(monkeypatch, payload: bytes) -> None:
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(inst, "_expected_micromamba_sha256", lambda subdir: digest)

    def download_to_fd(_url, file_descriptor):
        inst.os.write(file_descriptor, payload)
        return digest

    monkeypatch.setattr(inst, "_download_micromamba_to_fd", download_to_fd)


def _private_runner_directory(env: Path) -> Path:
    return env.parent / inst._RUNNER_DIRECTORY_NAME


def _assert_persistent_private_runner(env: Path, payload: bytes) -> Path:
    directory = _private_runner_directory(env)
    assert directory.is_dir()
    assert stat.S_IMODE(directory.lstat().st_mode) == 0o700
    runner = directory / inst._RUNNER_EXECUTABLE_NAME
    assert list(directory.iterdir()) == [runner]
    assert runner.read_bytes() == payload
    assert stat.S_IMODE(runner.lstat().st_mode) == 0o700
    assert runner.lstat().st_nlink == 1
    return runner


def test_install_happy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(inst.RuntimeInstaller, "is_ready", lambda self: False)  # 不短路
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", _materialize_micromamba)
    ran = []

    def fake_run(command, **_kwargs):
        ran.append(command)
        _materialize_created_env(command)

    monkeypatch.setattr(inst, "_run", fake_run)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda *a, **k: True)  # VERIFYING 过
    seen = []
    inst.RuntimeInstaller(on_progress=lambda s: seen.append(s.phase)).install()
    assert Phase.CREATING_ENV in seen and Phase.INSTALLING_PIP in seen and seen[-1] is Phase.READY
    create = " ".join(map(str, ran[0]))
    assert "create" in create and "python=3.12" in create and "freecad=1.1.0" in create
    assert inst.status.read_runtime_receipt() == spec.expected_receipt()
    assert "--upgrade" in ran[1]
    assert Path(ran[1][0]).name == "micromamba" and ran[1][1] == "run"
    assert Path(ran[1][0]).parent.name == inst._RUNNER_DIRECTORY_NAME
    assert ran[1][2:6] == ["-r", "../..", "-p", "./"]


def test_installer_verification_fails_closed_without_freecad_process_directory(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    prefix = inst.paths.env_prefix()
    python = inst.paths.env_python_for(prefix)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    legacy = inst.paths.legacy_env_prefix()
    legacy.mkdir(parents=True)
    legacy_marker = legacy / "engine.bin"
    legacy_marker.write_bytes(b"external-engine")

    calls = []

    def unavailable(base=None):
        calls.append(base)
        raise ValueError("FreeCAD process directory is unavailable")

    monkeypatch.setattr(inst.status, "freecad_process_environment", unavailable, raising=False)
    monkeypatch.setattr(inst.status, "verify_runtime_generation", _REAL_VERIFY_GENERATION)

    with pytest.raises(inst.InstallError, match="verification stopped"):
        inst.RuntimeInstaller()._verify_generation_or_raise(prefix, "verification stopped")

    assert len(calls) == 1 and calls[0]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert not (prefix / ".vibecad_ready").exists()
    assert legacy_marker.read_bytes() == b"external-engine"


def test_is_ready_uses_sentinel(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(inst.status, "runtime_ready", lambda: True)
    assert inst.RuntimeInstaller().is_ready() is True


def test_install_rechecks_ready_after_acquiring_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    answers = iter((False, True))
    monkeypatch.setattr(inst.RuntimeInstaller, "is_ready", lambda self: next(answers))
    monkeypatch.setattr(
        inst.RuntimeInstaller,
        "_do_install",
        lambda self: pytest.fail("锁内二检已 ready，不应重复安装"),
    )
    seen = []
    inst.RuntimeInstaller(on_progress=lambda s: seen.append(s.phase)).install()
    assert seen == [Phase.READY]


def test_install_holds_stable_home_maintenance_lock_outside_runtime(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setattr(inst.RuntimeInstaller, "is_ready", lambda self: False)

    def observe_locks(self):
        assert inst.paths.maintenance_lock().is_dir()
        assert inst.paths.install_lock().is_dir()
        assert inst.paths.maintenance_lock().parent == home
        assert inst.paths.maintenance_lock().parent != inst.paths.runtime_root()

    monkeypatch.setattr(inst.RuntimeInstaller, "_do_install", observe_locks)

    inst.RuntimeInstaller().install()

    assert not inst.paths.maintenance_lock().exists()
    assert not inst.paths.install_lock().exists()


def test_runtime_generation_windows_fallback_validates_root_once(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    installer = inst.RuntimeInstaller()
    identity = runtime.lstat()
    installer._runtime_identity = (identity.st_dev, identity.st_ino)
    installer._runtime_pin = None
    original = inst.status._fallback_directory
    calls = []

    def observe(path, *, create_missing):
        calls.append((path, create_missing))
        return original(path, create_missing=create_missing)

    monkeypatch.setattr(inst.status, "_fallback_directory", observe)

    installer._validate_runtime_generation()

    assert calls == [(runtime, False)]


def test_install_repair_rejects_data_alias_into_replaceable_runtime(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    misplaced = home / "runtime" / "misplaced-durable"
    misplaced.mkdir(parents=True)
    head = misplaced / "HEAD"
    head.write_bytes(b"durable-head")
    (home / "data").symlink_to(misplaced, target_is_directory=True)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setattr(inst.RuntimeInstaller, "is_ready", lambda self: False)
    monkeypatch.setattr(
        inst.RuntimeInstaller,
        "_do_install",
        lambda self: pytest.fail("unsafe repair must stop before installation"),
    )
    before = _tree_fingerprint(home / "runtime")

    with pytest.raises(ValueError, match="durable data"):
        inst.RuntimeInstaller().install()

    assert _tree_fingerprint(home / "runtime") == before
    assert head.read_bytes() == b"durable-head"


@pytest.mark.parametrize("component", ["bin", "mamba", "envs"])
def test_install_rejects_managed_layout_alias_before_download_or_run(
    monkeypatch, tmp_path, component
):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    if component == "bin":
        (runtime / "bin").symlink_to(outside, target_is_directory=True)
    elif component == "mamba":
        (runtime / "mamba").symlink_to(outside, target_is_directory=True)
    else:
        (runtime / "mamba").mkdir()
        (runtime / "mamba" / "envs").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setattr(inst.RuntimeInstaller, "is_ready", lambda self: False)
    ensured = []
    ran = []
    monkeypatch.setattr(
        inst.micromamba,
        "ensure_micromamba",
        lambda path: ensured.append(path),
    )
    monkeypatch.setattr(inst, "_run", lambda command, **kwargs: ran.append(command))

    with pytest.raises(inst.InstallError, match="不安全|unsafe"):
        inst.RuntimeInstaller().install()

    assert ensured == []
    assert ran == []
    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("entry", ["destination", "part"])
@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_install_rejects_unsafe_existing_micromamba_entries(monkeypatch, tmp_path, entry, kind):
    home = tmp_path / "VibeCAD"
    bin_dir = home / "runtime" / "bin"
    (home / "runtime" / "mamba" / "envs").mkdir(parents=True)
    bin_dir.mkdir()
    destination = home / "runtime" / "bin" / "micromamba"
    target = destination if entry == "destination" else destination.with_name("micromamba.part")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    if kind == "symlink":
        target.symlink_to(outside)
    else:
        target.mkdir()
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setattr(inst.RuntimeInstaller, "is_ready", lambda self: False)
    ensured = []
    ran = []
    monkeypatch.setattr(
        inst.micromamba,
        "ensure_micromamba",
        lambda path: ensured.append(path),
    )
    monkeypatch.setattr(inst, "_run", lambda command, **kwargs: ran.append(command))

    with pytest.raises(inst.InstallError, match="普通文件"):
        inst.RuntimeInstaller().install()

    assert ensured == []
    assert ran == []
    assert outside.read_bytes() == b"outside"


def test_pinned_download_parent_replacement_never_writes_outside(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    bin_dir = home / "runtime" / "bin"
    bin_dir.mkdir(parents=True)
    detached = home / "runtime" / "bin-detached"
    outside = tmp_path / "outside-bin"
    outside.mkdir()
    before = _tree_fingerprint(outside)
    digest = hashlib.sha256(_FAKE_MICROMAMBA).hexdigest()

    def replace_parent_after_part_open(_url, file_descriptor):
        bin_dir.rename(detached)
        bin_dir.symlink_to(outside, target_is_directory=True)
        inst.os.write(file_descriptor, _FAKE_MICROMAMBA)
        return digest

    monkeypatch.setattr(inst, "_download_micromamba_to_fd", replace_parent_after_part_open)

    with pytest.raises(inst.InstallError, match="identity changed|不安全"):
        inst.RuntimeInstaller()._ensure_micromamba(inst.paths.micromamba_path())

    assert _tree_fingerprint(outside) == before
    assert not (outside / "micromamba").exists()
    assert not (outside / "micromamba.part").exists()


def _prepare_managed_env(monkeypatch, tmp_path, receipt):
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    python = inst.paths.env_python()
    python.parent.mkdir(parents=True)
    python.touch()
    sentinel = inst.paths.ready_sentinel()
    if receipt is None:
        pass
    elif isinstance(receipt, str):
        sentinel.write_text(receipt, encoding="utf-8")
    else:
        sentinel.write_text(json.dumps(receipt), encoding="utf-8")
    return python, sentinel


def _write_same_version_wheel(
    root: Path,
    *,
    sources: dict[str, bytes],
) -> tuple[Path, str]:
    wheel = root / f"vibecad-{spec.VIBECAD_VERSION}-py3-none-any.whl"
    dist_info = f"vibecad-{spec.VIBECAD_VERSION}.dist-info"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(sources.items()):
            archive.writestr(f"vibecad/{name}", content)
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.4\nName: vibecad\nVersion: {spec.VIBECAD_VERSION}\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    return wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()


def _write_installed_server_identity(
    prefix: Path,
    *,
    sources: dict[str, bytes],
    wheel_sha256: str,
) -> None:
    site_packages = (
        prefix / "Lib" / "site-packages"
        if inst.sys.platform == "win32"
        else prefix
        / "lib"
        / f"python{spec.PYTHON_VERSION[0]}.{spec.PYTHON_VERSION[1]}"
        / "site-packages"
    )
    package = site_packages / "vibecad"
    package.mkdir(parents=True, exist_ok=True)
    for name, content in sources.items():
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    dist_info = site_packages / f"vibecad-{spec.VIBECAD_VERSION}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "direct_url.json").write_text(
        json.dumps(
            {
                "archive_info": {
                    "hash": f"sha256={wheel_sha256}",
                    "hashes": {"sha256": wheel_sha256},
                },
                "url": "file:///private/reviewed/vibecad.whl",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _prepare_current_same_version_server(
    monkeypatch,
    tmp_path: Path,
    *,
    sources: dict[str, bytes],
    wheel_sha256: str,
) -> tuple[Path, Path]:
    python, sentinel = _prepare_managed_env(monkeypatch, tmp_path, None)
    _write_installed_server_identity(
        inst.paths.env_prefix(),
        sources=sources,
        wheel_sha256=wheel_sha256,
    )
    sentinel.write_text(
        json.dumps(spec.expected_receipt(), sort_keys=True),
        encoding="utf-8",
    )
    sentinel.chmod(0o600)
    return python, sentinel


@pytest.mark.parametrize("legacy", [True, False])
def test_legacy_or_old_version_reuses_healthy_env_for_pip_only(monkeypatch, tmp_path, legacy):
    receipt = (
        spec.FREECAD_PIN
        if legacy
        else {
            **spec.expected_receipt(),
            "vibecad_version": "0.3.0",
        }
    )
    python, _ = _prepare_managed_env(monkeypatch, tmp_path, receipt)
    monkeypatch.setattr(inst.status, "engine_compatible", lambda py: py == python)
    verified = iter((False, True))
    monkeypatch.setattr(inst.status, "verify_runtime", lambda py: next(verified))
    ensured = []
    monkeypatch.setattr(
        inst.micromamba,
        "ensure_micromamba",
        lambda path: _materialize_micromamba(path, ensured),
    )
    ran = []
    monkeypatch.setattr(inst, "_run", lambda cmd, **k: ran.append(cmd))

    inst.RuntimeInstaller().install()

    assert len(ran) == 1
    assert Path(ran[0][0]).name == "micromamba"
    assert Path(ran[0][0]).parent.name == inst._RUNNER_DIRECTORY_NAME
    assert ran[0][1:6] == [
        "run",
        "-r",
        "../..",
        "-p",
        "./",
    ]
    assert ran[0][6:10] == ["python", "-m", "pip", "install"]
    assert "--upgrade" in ran[0]
    assert ensured == []
    assert inst.paths.micromamba_path().read_bytes() == _FAKE_MICROMAMBA
    assert inst.status.read_runtime_receipt() == spec.expected_receipt()


def test_same_version_pre_epoch_receipt_runs_pip_only_and_reissues_current_receipt(
    monkeypatch,
    tmp_path,
):
    old_receipt = {
        "schema": spec.RECEIPT_SCHEMA,
        "runtime_kind": spec.MANAGED_KIND,
        "vibecad_version": spec.VIBECAD_VERSION,
        "python_pin": spec.PYTHON_PIN,
        "freecad_pin": spec.FREECAD_PIN,
    }
    python, _ = _prepare_managed_env(monkeypatch, tmp_path, old_receipt)
    monkeypatch.setattr(inst.status, "engine_compatible", lambda candidate: candidate == python)
    verified = iter((False, True))
    monkeypatch.setattr(inst.status, "verify_runtime", lambda candidate: next(verified))
    monkeypatch.setattr(
        inst.micromamba,
        "ensure_micromamba",
        _materialize_micromamba,
    )
    ran = []
    monkeypatch.setattr(inst, "_run", lambda command, **kwargs: ran.append(command))

    inst.RuntimeInstaller().install()

    assert len(ran) == 1
    assert ran[0][6:10] == ["python", "-m", "pip", "install"]
    assert "create" not in ran[0]
    assert inst.status.read_runtime_receipt() == spec.expected_receipt()


def test_previous_positive_epoch_runs_one_pip_sync_without_replacing_managed_prefix(
    monkeypatch,
    tmp_path,
):
    old_receipt = {
        **spec.expected_receipt(),
        "server_package_epoch": spec.SERVER_PACKAGE_EPOCH - 1,
    }
    python, sentinel = _prepare_managed_env(monkeypatch, tmp_path, old_receipt)
    prefix = inst.paths.env_prefix()
    before = prefix.lstat()
    monkeypatch.setattr(
        inst.RuntimeInstaller,
        "_remove_managed_env",
        lambda *args, **kwargs: pytest.fail("previous epoch must not replace the managed prefix"),
    )
    monkeypatch.setattr(inst.status, "engine_compatible", lambda candidate: candidate == python)
    observed_receipts = []

    def verify(candidate):
        assert candidate == python
        observed_receipts.append(json.loads(sentinel.read_text(encoding="utf-8")))
        return len(observed_receipts) == 2

    monkeypatch.setattr(inst.status, "verify_runtime", verify)
    ran = []

    def run(command, **kwargs):
        assert json.loads(sentinel.read_text(encoding="utf-8")) == old_receipt
        ran.append(command)

    monkeypatch.setattr(inst, "_run", run)

    inst.RuntimeInstaller().install()

    after = prefix.lstat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert len(ran) == 1
    assert ran[0][6:10] == ["python", "-m", "pip", "install"]
    assert "create" not in ran[0]
    assert observed_receipts == [old_receipt, old_receipt]
    current_receipt = inst.status.read_runtime_receipt()
    assert current_receipt == spec.expected_receipt()
    assert current_receipt["server_package_epoch"] == 4


@pytest.mark.parametrize("failure_at", ("pip", "verify"))
def test_previous_positive_epoch_sync_failure_keeps_old_receipt_and_prefix(
    monkeypatch,
    tmp_path,
    failure_at,
):
    old_receipt = {
        **spec.expected_receipt(),
        "server_package_epoch": spec.SERVER_PACKAGE_EPOCH - 1,
    }
    python, sentinel = _prepare_managed_env(monkeypatch, tmp_path, old_receipt)
    prefix = inst.paths.env_prefix()
    before = prefix.lstat()
    monkeypatch.setattr(
        inst.RuntimeInstaller,
        "_remove_managed_env",
        lambda *args, **kwargs: pytest.fail("failed sync must not replace the managed prefix"),
    )
    monkeypatch.setattr(inst.status, "engine_compatible", lambda candidate: candidate == python)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda candidate: False)
    ran = []

    def run(command, **kwargs):
        assert json.loads(sentinel.read_text(encoding="utf-8")) == old_receipt
        ran.append(command)
        if failure_at == "pip":
            raise RuntimeError("simulated pip failure")

    monkeypatch.setattr(inst, "_run", run)

    with pytest.raises(inst.InstallError):
        inst.RuntimeInstaller().install()

    after = prefix.lstat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert len(ran) == 1
    assert ran[0][6:10] == ["python", "-m", "pip", "install"]
    assert "create" not in ran[0]
    assert json.loads(sentinel.read_text(encoding="utf-8")) == old_receipt


def test_legacy_receipt_migrates_without_pip_when_package_is_already_exact(monkeypatch, tmp_path):
    python, _ = _prepare_managed_env(monkeypatch, tmp_path, spec.FREECAD_PIN)
    monkeypatch.setattr(inst.status, "engine_compatible", lambda py: py == python)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda py: True)
    monkeypatch.setattr(inst, "_run", lambda *a, **k: pytest.fail("无需重复 pip"))

    inst.RuntimeInstaller().install()

    assert inst.status.read_runtime_receipt() == spec.expected_receipt()


@pytest.mark.parametrize("receipt", [None, "{broken"])
def test_missing_or_corrupt_receipt_is_repaired_when_env_is_exact(monkeypatch, tmp_path, receipt):
    python, _ = _prepare_managed_env(monkeypatch, tmp_path, receipt)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda py: py == python)
    monkeypatch.setattr(
        inst.status,
        "engine_compatible",
        lambda py: pytest.fail("精确验证已通过，无需降级 health probe"),
    )
    monkeypatch.setattr(inst, "_run", lambda *a, **k: pytest.fail("无需 pip 或重建"))

    inst.RuntimeInstaller().install()

    assert inst.status.read_runtime_receipt() == spec.expected_receipt()
    assert inst.status.runtime_ready() is True


def test_managed_receipt_is_not_published_when_python_changes_after_verify(monkeypatch, tmp_path):
    python, sentinel = _prepare_managed_env(monkeypatch, tmp_path, None)

    def replace_python_after_verify(candidate):
        assert candidate == python
        replacement = python.with_name("python.replacement")
        replacement.write_bytes(b"different interpreter")
        replacement.replace(python)
        return True

    monkeypatch.setattr(inst.status, "verify_runtime", replace_python_after_verify)
    monkeypatch.setattr(
        inst.micromamba,
        "ensure_micromamba",
        lambda *args, **kwargs: pytest.fail("exact env must not download"),
    )
    monkeypatch.setattr(inst, "_run", lambda *args, **kwargs: pytest.fail("must not run"))

    with pytest.raises(inst.InstallError, match="generation identity changed"):
        inst.RuntimeInstaller().install()

    assert not sentinel.exists()
    assert inst.status.runtime_ready() is False


def test_failed_server_sync_keeps_old_receipt(monkeypatch, tmp_path):
    old = {**spec.expected_receipt(), "vibecad_version": "0.3.0"}
    python, sentinel = _prepare_managed_env(monkeypatch, tmp_path, old)
    monkeypatch.setattr(inst.status, "engine_compatible", lambda py: py == python)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda py: False)
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", _materialize_micromamba)
    monkeypatch.setattr(inst, "_run", lambda cmd, **k: None)

    with pytest.raises(inst.InstallError):
        inst.RuntimeInstaller().install()

    assert json.loads(sentinel.read_text(encoding="utf-8")) == old
    assert inst.status.runtime_ready() is False


def test_same_version_server_refresh_forces_exact_wheel_before_receipt_commit(
    monkeypatch,
    tmp_path,
):
    old_sources = {
        "__init__.py": b'__version__ = "0.6.0"\n',
        "worker/service.py": b"OLD_WORKER = True\n",
    }
    final_sources = {
        "__init__.py": b'__version__ = "0.6.0"\n',
        "worker/service.py": b"FINAL_WORKER = True\n",
    }
    wheel, wheel_sha256 = _write_same_version_wheel(tmp_path, sources=final_sources)
    python, sentinel = _prepare_current_same_version_server(
        monkeypatch,
        tmp_path / "home",
        sources=old_sources,
        wheel_sha256="0" * 64,
    )
    prefix = inst.paths.env_prefix()
    prefix_before = prefix.lstat()
    monkeypatch.setattr(inst.status, "engine_compatible", lambda candidate: candidate == python)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda candidate: candidate == python)
    calls = []

    def install_final(command, **_kwargs):
        calls.append(command)
        assert inst.paths.maintenance_lock().is_dir()
        assert inst.paths.install_lock().is_dir()
        assert not sentinel.exists()
        assert inst.status.runtime_receipt_state() is inst.status.ReceiptState.MISSING
        _write_installed_server_identity(
            prefix,
            sources=final_sources,
            wheel_sha256=wheel_sha256,
        )

    monkeypatch.setattr(inst, "_run", install_final)
    phases = []

    def observe_progress(value):
        if value.phase is Phase.READY:
            assert sentinel.exists()
            assert inst.status.runtime_receipt_state() is inst.status.ReceiptState.CURRENT
        phases.append(value.phase)

    installer = inst.RuntimeInstaller(on_progress=observe_progress)
    installer.refresh_server_package(
        wheel,
        expected_sha256=wheel_sha256,
    )

    prefix_after = prefix.lstat()
    assert (prefix_after.st_dev, prefix_after.st_ino) == (
        prefix_before.st_dev,
        prefix_before.st_ino,
    )
    assert len(calls) == 1
    command = calls[0]
    assert command[1:6] == ["run", "-r", "../..", "-p", "./"]
    assert command[6:11] == ["python", "-B", "-m", "pip", "install"]
    assert "--no-index" in command
    assert "--force-reinstall" in command
    assert "--no-deps" in command
    assert str(wheel.resolve()) in command
    assert "create" not in command
    assert phases == [Phase.INSTALLING_PIP, Phase.VERIFYING, Phase.READY]
    assert inst.status.read_runtime_receipt() == spec.expected_receipt()
    assert inst.status.runtime_receipt_state() is inst.status.ReceiptState.CURRENT
    assert inst.status.runtime_ready() is True


def test_same_version_server_refresh_is_idempotent_for_exact_installed_wheel(
    monkeypatch,
    tmp_path,
):
    sources = {
        "__init__.py": b'__version__ = "0.6.0"\n',
        "worker/service.py": b"FINAL_WORKER = True\n",
    }
    wheel, wheel_sha256 = _write_same_version_wheel(tmp_path, sources=sources)
    python, sentinel = _prepare_current_same_version_server(
        monkeypatch,
        tmp_path / "home",
        sources=sources,
        wheel_sha256=wheel_sha256,
    )
    receipt_before = sentinel.lstat()
    monkeypatch.setattr(inst.status, "engine_compatible", lambda candidate: candidate == python)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda candidate: candidate == python)
    monkeypatch.setattr(
        inst,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("exact same-version wheel must not reinstall"),
    )
    phases = []

    installer = inst.RuntimeInstaller(on_progress=lambda value: phases.append(value.phase))
    installer.refresh_server_package(
        wheel,
        expected_sha256=wheel_sha256,
    )

    receipt_after = sentinel.lstat()
    assert (receipt_after.st_dev, receipt_after.st_ino) == (
        receipt_before.st_dev,
        receipt_before.st_ino,
    )
    assert phases == [Phase.READY]
    assert inst.status.runtime_receipt_state() is inst.status.ReceiptState.CURRENT


def test_same_version_server_refresh_revokes_commit_when_ready_callback_fails(
    monkeypatch,
    tmp_path,
):
    old_sources = {
        "__init__.py": b'__version__ = "0.6.0"\n',
        "worker/service.py": b"OLD_WORKER = True\n",
    }
    final_sources = {
        "__init__.py": b'__version__ = "0.6.0"\n',
        "worker/service.py": b"FINAL_WORKER = True\n",
    }
    wheel, wheel_sha256 = _write_same_version_wheel(tmp_path, sources=final_sources)
    python, sentinel = _prepare_current_same_version_server(
        monkeypatch,
        tmp_path / "home",
        sources=old_sources,
        wheel_sha256="0" * 64,
    )
    prefix = inst.paths.env_prefix()
    monkeypatch.setattr(inst.status, "engine_compatible", lambda candidate: candidate == python)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda candidate: candidate == python)

    def install_final(_command, **_kwargs):
        assert not sentinel.exists()
        _write_installed_server_identity(
            prefix,
            sources=final_sources,
            wheel_sha256=wheel_sha256,
        )

    def fail_ready(value):
        if value.phase is Phase.READY:
            assert inst.status.runtime_receipt_state() is inst.status.ReceiptState.CURRENT
            raise RuntimeError("simulated READY callback failure")

    monkeypatch.setattr(inst, "_run", install_final)

    with pytest.raises(inst.InstallError, match="READY callback failure"):
        inst.RuntimeInstaller(on_progress=fail_ready).refresh_server_package(
            wheel,
            expected_sha256=wheel_sha256,
        )

    assert not sentinel.exists()
    assert inst.status.runtime_receipt_state() is inst.status.ReceiptState.MISSING
    assert inst.status.runtime_ready() is False


def test_same_version_server_refresh_does_not_publish_failure_to_changed_generation(
    monkeypatch,
    tmp_path,
):
    old_sources = {
        "__init__.py": b'__version__ = "0.6.0"\n',
        "worker/service.py": b"OLD_WORKER = True\n",
    }
    final_sources = {
        "__init__.py": b'__version__ = "0.6.0"\n',
        "worker/service.py": b"FINAL_WORKER = True\n",
    }
    wheel, wheel_sha256 = _write_same_version_wheel(tmp_path, sources=final_sources)
    python, sentinel = _prepare_current_same_version_server(
        monkeypatch,
        tmp_path / "home",
        sources=old_sources,
        wheel_sha256="0" * 64,
    )
    monkeypatch.setattr(inst.status, "engine_compatible", lambda candidate: candidate == python)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda candidate: candidate == python)

    def replace_python_and_fail(_command, **_kwargs):
        replacement = python.with_name("python.replacement")
        replacement.write_bytes(b"different interpreter")
        replacement.replace(python)
        raise RuntimeError("simulated failure after generation replacement")

    monkeypatch.setattr(inst, "_run", replace_python_and_fail)
    phases = []

    with pytest.raises(inst.InstallError, match="generation replacement"):
        inst.RuntimeInstaller(
            on_progress=lambda value: phases.append(value.phase)
        ).refresh_server_package(
            wheel,
            expected_sha256=wheel_sha256,
        )

    assert phases == [Phase.INSTALLING_PIP]
    assert not sentinel.exists()
    assert inst.status.runtime_receipt_state() is inst.status.ReceiptState.MISSING


def test_same_version_receipt_revocation_handles_short_descriptor_reads(
    monkeypatch,
    tmp_path,
):
    sources = {"__init__.py": b'__version__ = "0.6.0"\n'}
    python, sentinel = _prepare_current_same_version_server(
        monkeypatch,
        tmp_path / "home",
        sources=sources,
        wheel_sha256="0" * 64,
    )
    prefix = inst.paths.env_prefix()
    evidence = inst.status.capture_runtime_generation_evidence(prefix)
    real_read = inst.os.read

    def short_read(descriptor, maximum):
        return real_read(descriptor, min(maximum, 7))

    monkeypatch.setattr(inst.os, "read", short_read)
    inst.status.revoke_current_managed_runtime_receipt(prefix, evidence)

    assert python.exists()
    assert not sentinel.exists()
    assert inst.status.runtime_receipt_state() is inst.status.ReceiptState.MISSING


def test_same_version_server_wheel_rejects_excess_total_uncompressed_bytes(
    monkeypatch,
    tmp_path,
):
    sources = {
        "first.py": b"x" * 40_000,
        "second.py": b"y" * 40_000,
    }
    wheel, wheel_sha256 = _write_same_version_wheel(tmp_path, sources=sources)
    assert wheel.stat().st_size < 70_000
    monkeypatch.setattr(inst, "_MAX_SERVER_WHEEL_BYTES", 70_000)

    with pytest.raises(inst.InstallError, match="unsafe entry"):
        inst._validated_server_wheel(wheel, wheel_sha256)


@pytest.mark.parametrize("failure_at", ("pip", "verify", "source_parity"))
def test_same_version_server_refresh_failure_never_leaves_current_receipt(
    monkeypatch,
    tmp_path,
    failure_at,
):
    old_sources = {
        "__init__.py": b'__version__ = "0.6.0"\n',
        "worker/service.py": b"OLD_WORKER = True\n",
    }
    final_sources = {
        "__init__.py": b'__version__ = "0.6.0"\n',
        "worker/service.py": b"FINAL_WORKER = True\n",
    }
    wheel, wheel_sha256 = _write_same_version_wheel(tmp_path, sources=final_sources)
    python, sentinel = _prepare_current_same_version_server(
        monkeypatch,
        tmp_path / "home",
        sources=old_sources,
        wheel_sha256="0" * 64,
    )
    prefix = inst.paths.env_prefix()
    prefix_before = prefix.lstat()
    monkeypatch.setattr(inst.status, "engine_compatible", lambda candidate: candidate == python)
    verify_results = iter((True, failure_at != "verify"))
    monkeypatch.setattr(
        inst.status,
        "verify_runtime",
        lambda candidate: candidate == python and next(verify_results),
    )

    def fail_or_install(command, **_kwargs):
        assert not sentinel.exists()
        if failure_at == "pip":
            raise RuntimeError("simulated same-version pip failure")
        _write_installed_server_identity(
            prefix,
            sources=(old_sources if failure_at == "source_parity" else final_sources),
            wheel_sha256=wheel_sha256,
        )

    monkeypatch.setattr(inst, "_run", fail_or_install)

    with pytest.raises(inst.InstallError):
        inst.RuntimeInstaller().refresh_server_package(
            wheel,
            expected_sha256=wheel_sha256,
        )

    prefix_after = prefix.lstat()
    assert (prefix_after.st_dev, prefix_after.st_ino) == (
        prefix_before.st_dev,
        prefix_before.st_ino,
    )
    assert not sentinel.exists()
    assert inst.status.runtime_receipt_state() is inst.status.ReceiptState.MISSING
    assert inst.status.runtime_ready() is False


def test_same_version_server_refresh_rejects_wrong_wheel_without_revoking_current(
    monkeypatch,
    tmp_path,
):
    sources = {"__init__.py": b'__version__ = "0.6.0"\n'}
    wheel, wheel_sha256 = _write_same_version_wheel(tmp_path, sources=sources)
    python, sentinel = _prepare_current_same_version_server(
        monkeypatch,
        tmp_path / "home",
        sources=sources,
        wheel_sha256=wheel_sha256,
    )
    receipt_before = sentinel.read_bytes()
    monkeypatch.setattr(inst.status, "engine_compatible", lambda candidate: candidate == python)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda candidate: candidate == python)
    monkeypatch.setattr(
        inst,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("wrong wheel must stop before pip"),
    )

    with pytest.raises(inst.InstallError, match="digest"):
        inst.RuntimeInstaller().refresh_server_package(
            wheel,
            expected_sha256="f" * 64,
        )

    assert sentinel.read_bytes() == receipt_before
    assert inst.status.runtime_receipt_state() is inst.status.ReceiptState.CURRENT


def test_unhealthy_existing_managed_env_is_removed_before_create(monkeypatch, tmp_path):
    python, _ = _prepare_managed_env(monkeypatch, tmp_path, None)
    env = inst.paths.env_prefix()
    stale = env / "stale.bin"
    stale.write_text("broken", encoding="utf-8")
    keep = inst.paths.mamba_root_prefix() / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    verified = iter((False, True))
    monkeypatch.setattr(inst.status, "verify_runtime", lambda py: next(verified))
    monkeypatch.setattr(inst.status, "engine_compatible", lambda py: False)
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", _materialize_micromamba)
    ran = []

    def fake_run(cmd, **kwargs):
        assert not stale.exists(), "existing prefix 必须在 create 前删除"
        ran.append(cmd)
        _materialize_created_env(cmd)

    monkeypatch.setattr(inst, "_run", fake_run)

    inst.RuntimeInstaller().install()

    assert ran[0][1] == "create"
    assert keep.read_text(encoding="utf-8") == "keep"
    assert inst.status.read_runtime_receipt() == spec.expected_receipt()
    assert python.parent.parent == env


def test_successful_create_without_python_stops_after_pip_and_before_receipt(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(inst.RuntimeInstaller, "is_ready", lambda self: False)
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", _materialize_micromamba)
    calls = []
    monkeypatch.setattr(inst, "_run", lambda command, **kwargs: calls.append(command))
    monkeypatch.setattr(
        inst.status,
        "verify_runtime",
        lambda *args, **kwargs: pytest.fail("missing env must not be verified"),
    )

    with pytest.raises(inst.InstallError, match="目录不安全|unavailable"):
        inst.RuntimeInstaller().install()

    assert [command[1] for command in calls] == ["create", "run"]
    assert not inst.paths.ready_sentinel().exists()


def test_runtime_generation_replacement_after_create_stops_before_pip(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setattr(inst.RuntimeInstaller, "is_ready", lambda self: False)
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", _materialize_micromamba)
    runtime = home / "runtime"
    detached = home / "runtime-detached"
    outside = tmp_path / "outside-runtime"
    outside.mkdir()
    calls = []

    def replace_runtime_after_create(command, **_kwargs):
        calls.append(command)
        assert command[1] == "create", "pip must not run after runtime replacement"
        _materialize_created_env(command)
        inst.os.rename(runtime, detached)
        runtime.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(inst, "_run", replace_runtime_after_create)

    with pytest.raises((inst.InstallError, ValueError), match="identity changed"):
        inst.RuntimeInstaller().install()

    assert len(calls) == 1 and calls[0][1] == "create"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("replaced_entry", ["root", "env"])
def test_capability_bound_create_never_writes_replacement_tree(
    monkeypatch, tmp_path, replaced_entry
):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    safe_marker = tmp_path / "safe-create.marker"
    monkeypatch.setenv("VIBECAD_TEST_MARKER", str(safe_marker))
    payload = b'#!/bin/sh\nprintf safe > "$VIBECAD_TEST_MARKER"\nexit 0\n'
    _install_capability_runner(monkeypatch, payload)
    installer = inst.RuntimeInstaller()
    installer._ensure_current_layout()
    installer._ensure_micromamba(inst.paths.micromamba_path())
    env = inst.paths.env_prefix()
    root = inst.paths.mamba_root_prefix()
    env_identity = installer._prepare_empty_managed_env(env)
    real_spawn = inst._spawn_process

    if replaced_entry == "env":
        detached = env.with_name("vibecad-detached")
        outside = tmp_path / "outside-env"
        outside.mkdir()

        def replace_before_spawn(*args, **kwargs):
            env.rename(detached)
            env.symlink_to(outside, target_is_directory=True)
            return real_spawn(*args, **kwargs)

    else:
        detached = root.with_name("mamba-detached")
        outside = tmp_path / "outside-root"
        (outside / "envs" / "vibecad").mkdir(parents=True)

        def replace_before_spawn(*args, **kwargs):
            root.rename(detached)
            root.symlink_to(outside, target_is_directory=True)
            return real_spawn(*args, **kwargs)

    before = _tree_fingerprint(outside)
    monkeypatch.setattr(inst, "_spawn_process", replace_before_spawn)

    with pytest.raises(inst.InstallError, match="identity changed|不安全"):
        installer._run_micromamba_command(
            inst.paths.micromamba_path(),
            root,
            env,
            ["create", "-y"],
            expected_env_identity=env_identity,
        )

    assert safe_marker.read_bytes() == b"safe"
    assert _tree_fingerprint(outside) == before


def test_capability_bound_pip_never_writes_replacement_env(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    safe_marker = tmp_path / "safe-pip.marker"
    monkeypatch.setenv("VIBECAD_TEST_MARKER", str(safe_marker))
    payload = b'#!/bin/sh\nprintf safe > "$VIBECAD_TEST_MARKER"\nexit 0\n'
    _install_capability_runner(monkeypatch, payload)
    installer = inst.RuntimeInstaller()
    installer._ensure_current_layout()
    installer._ensure_micromamba(inst.paths.micromamba_path())
    env = inst.paths.env_prefix()
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_bytes(b"python")
    identity = env.lstat()
    detached = env.with_name("vibecad-detached")
    outside = tmp_path / "outside-env"
    outside.mkdir()
    before = _tree_fingerprint(outside)
    real_spawn = inst._spawn_process

    def replace_before_spawn(*args, **kwargs):
        env.rename(detached)
        env.symlink_to(outside, target_is_directory=True)
        return real_spawn(*args, **kwargs)

    monkeypatch.setattr(inst, "_spawn_process", replace_before_spawn)

    with pytest.raises(inst.InstallError, match="identity changed|不安全"):
        installer._install_server_package(
            inst.paths.micromamba_path(),
            inst.paths.mamba_root_prefix(),
            env,
            expected_env_identity=(identity.st_dev, identity.st_ino),
        )

    assert safe_marker.read_bytes() == b"safe"
    assert _tree_fingerprint(outside) == before


def test_staged_validated_runner_ignores_source_binary_replacement(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    safe_marker = tmp_path / "safe-runner.marker"
    evil_marker = tmp_path / "evil-runner.marker"
    monkeypatch.setenv("VIBECAD_TEST_MARKER", str(safe_marker))
    monkeypatch.setenv("VIBECAD_EVIL_MARKER", str(evil_marker))
    payload = b'#!/bin/sh\nprintf safe > "$VIBECAD_TEST_MARKER"\nexit 0\n'
    _install_capability_runner(monkeypatch, payload)
    installer = inst.RuntimeInstaller()
    installer._ensure_current_layout()
    micromamba_path = installer._ensure_micromamba(inst.paths.micromamba_path())
    env = inst.paths.env_prefix()
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_bytes(b"python")
    identity = env.lstat()
    real_spawn = inst._spawn_process

    def replace_source_before_spawn(*args, **kwargs):
        replacement = micromamba_path.with_name("micromamba.replacement")
        replacement.write_bytes(b'#!/bin/sh\nprintf evil > "$VIBECAD_EVIL_MARKER"\nexit 0\n')
        replacement.chmod(0o700)
        replacement.replace(micromamba_path)
        return real_spawn(*args, **kwargs)

    monkeypatch.setattr(inst, "_spawn_process", replace_source_before_spawn)

    installer._install_server_package(
        micromamba_path,
        inst.paths.mamba_root_prefix(),
        env,
        expected_env_identity=(identity.st_dev, identity.st_ino),
    )

    assert safe_marker.read_bytes() == b"safe"
    assert not evil_marker.exists()


def test_persistent_runner_preserves_basename_and_validates_private_directory(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    payload = b"#!/bin/sh\nexit 0\n"
    _install_capability_runner(monkeypatch, payload)
    installer = inst.RuntimeInstaller()
    installer._ensure_current_layout()
    micromamba_path = installer._ensure_micromamba(inst.paths.micromamba_path())
    env = inst.paths.env_prefix()
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_bytes(b"python")
    env_info = env.lstat()
    commands = []

    def inspect_runner(command, **_kwargs):
        commands.append(command)
        assert Path(command[0]).name == "micromamba"
        assert Path(command[0]).parent.name == inst._RUNNER_DIRECTORY_NAME
        runner = env / command[0]
        runner_info = runner.lstat()
        directory_info = runner.parent.lstat()
        assert stat.S_ISREG(runner_info.st_mode) and runner_info.st_nlink == 1
        assert stat.S_ISDIR(directory_info.st_mode) and directory_info.st_nlink > 0
        assert stat.S_IMODE(runner_info.st_mode) == 0o700
        assert stat.S_IMODE(directory_info.st_mode) == 0o700
        if hasattr(inst.os, "geteuid"):
            assert runner_info.st_uid == inst.os.geteuid()
            assert directory_info.st_uid == inst.os.geteuid()

    monkeypatch.setattr(inst, "_run", inspect_runner)

    installer._install_server_package(
        micromamba_path,
        inst.paths.mamba_root_prefix(),
        env,
        expected_env_identity=(env_info.st_dev, env_info.st_ino),
    )

    assert len(commands) == 1
    _assert_persistent_private_runner(env, payload)


def test_staged_runner_real_exec_observes_exact_micromamba_basename(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    marker = tmp_path / "runner-basename.txt"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_TEST_MARKER", str(marker))
    payload = b'#!/bin/sh\nprintf "%s" "${0##*/}" > "$VIBECAD_TEST_MARKER"\nexit 0\n'
    _install_capability_runner(monkeypatch, payload)
    installer = inst.RuntimeInstaller()
    installer._ensure_current_layout()
    micromamba_path = installer._ensure_micromamba(inst.paths.micromamba_path())
    env = inst.paths.env_prefix()
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_bytes(b"python")
    env_info = env.lstat()

    installer._install_server_package(
        micromamba_path,
        inst.paths.mamba_root_prefix(),
        env,
        expected_env_identity=(env_info.st_dev, env_info.st_ino),
    )

    assert marker.read_text(encoding="utf-8") == "micromamba"
    _assert_persistent_private_runner(env, payload)


@pytest.mark.parametrize("fault", ("copy", "stored_bytes", "digest", "command"))
def test_persistent_runner_operational_failure_is_fixed_and_bounded(
    monkeypatch,
    tmp_path,
    fault,
):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    payload = b"#!/bin/sh\nexit 0\n"
    _install_capability_runner(monkeypatch, payload)
    installer = inst.RuntimeInstaller()
    installer._ensure_current_layout()
    micromamba_path = installer._ensure_micromamba(inst.paths.micromamba_path())
    env = inst.paths.env_prefix()
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_bytes(b"python")
    env_info = env.lstat()

    if fault == "copy":
        monkeypatch.setattr(
            inst,
            "_runner_write",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("copy fault")),
        )
        expected_error = inst.InstallError
    elif fault == "stored_bytes":
        real_runner_write = inst._runner_write
        monkeypatch.setattr(
            inst,
            "_runner_write",
            lambda file_descriptor, data: real_runner_write(
                file_descriptor,
                b"x" * len(data),
            ),
        )
        expected_error = inst.InstallError
    elif fault == "digest":
        installer._micromamba_digests[micromamba_path] = "0" * 64
        expected_error = inst.InstallError
    else:
        monkeypatch.setattr(
            inst,
            "_run",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("command fault")),
        )
        expected_error = RuntimeError

    with pytest.raises(expected_error):
        installer._install_server_package(
            micromamba_path,
            inst.paths.mamba_root_prefix(),
            env,
            expected_env_identity=(env_info.st_dev, env_info.st_ino),
        )

    runner = _private_runner_directory(env) / inst._RUNNER_EXECUTABLE_NAME
    expected_payload = {
        "copy": b"",
        "stored_bytes": b"x" * len(payload),
        "digest": payload,
        "command": payload,
    }[fault]
    assert runner.read_bytes() == expected_payload
    assert stat.S_IMODE(runner.lstat().st_mode) == 0o700
    before = _tree_fingerprint(_private_runner_directory(env))

    if fault == "command":
        monkeypatch.setattr(inst, "_run", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            inst,
            "_runner_write",
            lambda *_args, **_kwargs: pytest.fail("exact persistent runner must be reused"),
        )
        installer._install_server_package(
            micromamba_path,
            inst.paths.mamba_root_prefix(),
            env,
            expected_env_identity=(env_info.st_dev, env_info.st_ino),
        )
    else:
        with pytest.raises(inst.InstallError, match="does not match"):
            installer._install_server_package(
                micromamba_path,
                inst.paths.mamba_root_prefix(),
                env,
                expected_env_identity=(env_info.st_dev, env_info.st_ino),
            )

    assert _tree_fingerprint(_private_runner_directory(env)) == before


def test_staged_runner_never_unlinks_replacement_file(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    payload = b"#!/bin/sh\nexit 0\n"
    _install_capability_runner(monkeypatch, payload)
    installer = inst.RuntimeInstaller()
    installer._ensure_current_layout()
    micromamba_path = installer._ensure_micromamba(inst.paths.micromamba_path())
    env = inst.paths.env_prefix()
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_bytes(b"python")
    env_info = env.lstat()
    replacement_payload = b"replacement must survive"
    replacement_path = None

    def replace_staged_file(command, **_kwargs):
        nonlocal replacement_path
        runner = env / command[0]
        runner.rename(runner.with_name("original-micromamba"))
        runner.write_bytes(replacement_payload)
        runner.chmod(0o700)
        replacement_path = runner

    monkeypatch.setattr(inst, "_run", replace_staged_file)

    with pytest.raises(inst.InstallError, match="identity changed"):
        installer._install_server_package(
            micromamba_path,
            inst.paths.mamba_root_prefix(),
            env,
            expected_env_identity=(env_info.st_dev, env_info.st_ino),
        )

    assert replacement_path is not None
    assert replacement_path.read_bytes() == replacement_payload
    assert (replacement_path.parent / "original-micromamba").read_bytes() == payload


def test_persistent_runner_post_command_sha_rejects_same_inode_mutation(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    payload = b"#!/bin/sh\nexit 0\n"
    mutated_payload = b"same inode, changed bytes"
    _install_capability_runner(monkeypatch, payload)
    installer = inst.RuntimeInstaller()
    installer._ensure_current_layout()
    micromamba_path = installer._ensure_micromamba(inst.paths.micromamba_path())
    env = inst.paths.env_prefix()
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_bytes(b"python")
    env_info = env.lstat()
    original_identity = None

    def mutate_staged_file(command, **_kwargs):
        nonlocal original_identity
        runner = env / command[0]
        info = runner.lstat()
        original_identity = info.st_dev, info.st_ino
        runner.write_bytes(mutated_payload)
        assert (runner.lstat().st_dev, runner.lstat().st_ino) == original_identity

    monkeypatch.setattr(inst, "_run", mutate_staged_file)

    with pytest.raises(inst.InstallError, match="identity changed"):
        installer._install_server_package(
            micromamba_path,
            inst.paths.mamba_root_prefix(),
            env,
            expected_env_identity=(env_info.st_dev, env_info.st_ino),
        )

    runner = _private_runner_directory(env) / inst._RUNNER_EXECUTABLE_NAME
    assert (runner.lstat().st_dev, runner.lstat().st_ino) == original_identity
    assert runner.read_bytes() == mutated_payload


def test_staged_runner_directory_replacement_is_not_removed(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    payload = b"#!/bin/sh\nexit 0\n"
    _install_capability_runner(monkeypatch, payload)
    installer = inst.RuntimeInstaller()
    installer._ensure_current_layout()
    micromamba_path = installer._ensure_micromamba(inst.paths.micromamba_path())
    env = inst.paths.env_prefix()
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_bytes(b"python")
    env_info = env.lstat()
    runner_directory = _private_runner_directory(env)
    detached_directory = env.parent / ".vibecad-runner-detached"
    replacement_payload = b"replacement directory must survive"

    def replace_runner_directory(_command, **_kwargs):
        runner_directory.rename(detached_directory)
        runner_directory.mkdir(mode=0o700)
        replacement = runner_directory / "micromamba"
        replacement.write_bytes(replacement_payload)
        replacement.chmod(0o700)

    monkeypatch.setattr(inst, "_run", replace_runner_directory)

    with pytest.raises(inst.InstallError, match="identity changed"):
        installer._install_server_package(
            micromamba_path,
            inst.paths.mamba_root_prefix(),
            env,
            expected_env_identity=(env_info.st_dev, env_info.st_ino),
        )

    assert (runner_directory / "micromamba").read_bytes() == replacement_payload
    assert (detached_directory / "micromamba").read_bytes() == payload


def test_persistent_runner_never_reaches_name_unlink_replacement_seam(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    payload = b"#!/bin/sh\nexit 0\n"
    replacement_payload = b"replacement must never be name-unlinked"
    _install_capability_runner(monkeypatch, payload)
    installer = inst.RuntimeInstaller()
    installer._ensure_current_layout()
    micromamba_path = installer._ensure_micromamba(inst.paths.micromamba_path())
    env = inst.paths.env_prefix()
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_bytes(b"python")
    env_info = env.lstat()
    runner = _private_runner_directory(env) / inst._RUNNER_EXECUTABLE_NAME
    detached_original = tmp_path / "validated-runner-original"
    replacement_deleted = []
    real_unlink = inst._runner_unlink
    monkeypatch.setattr(inst, "_run", lambda *_args, **_kwargs: None)

    def replace_verified_inode_then_unlink(path, *args, **kwargs):
        assert path == inst._RUNNER_EXECUTABLE_NAME
        runner.replace(detached_original)
        runner.write_bytes(replacement_payload)
        runner.chmod(0o700)
        real_unlink(path, *args, **kwargs)
        replacement_deleted.append(not runner.exists())

    monkeypatch.setattr(inst, "_runner_unlink", replace_verified_inode_then_unlink)

    installer._install_server_package(
        micromamba_path,
        inst.paths.mamba_root_prefix(),
        env,
        expected_env_identity=(env_info.st_dev, env_info.st_ino),
    )

    assert replacement_deleted == []
    assert runner.read_bytes() == payload
    assert not detached_original.exists()


def test_persistent_exact_runner_is_reused_without_copy_or_unlink(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    payload = b"#!/bin/sh\nexit 0\n"
    _install_capability_runner(monkeypatch, payload)
    installer = inst.RuntimeInstaller()
    installer._ensure_current_layout()
    micromamba_path = installer._ensure_micromamba(inst.paths.micromamba_path())
    env = inst.paths.env_prefix()
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_bytes(b"python")
    env_info = env.lstat()
    commands = []
    monkeypatch.setattr(inst, "_run", lambda command, **_kwargs: commands.append(command))
    installer._install_server_package(
        micromamba_path,
        inst.paths.mamba_root_prefix(),
        env,
        expected_env_identity=(env_info.st_dev, env_info.st_ino),
    )
    runner = _assert_persistent_private_runner(env, payload)
    first_identity = runner.lstat().st_dev, runner.lstat().st_ino
    monkeypatch.setattr(
        inst,
        "_runner_write",
        lambda *_args, **_kwargs: pytest.fail("exact persistent runner must be reused, not copied"),
    )
    monkeypatch.setattr(
        inst,
        "_runner_unlink",
        lambda *_args, **_kwargs: pytest.fail("persistent runner must never be unlinked"),
    )
    installer._install_server_package(
        micromamba_path,
        inst.paths.mamba_root_prefix(),
        env,
        expected_env_identity=(env_info.st_dev, env_info.st_ino),
    )

    assert len(commands) == 2
    assert (runner.lstat().st_dev, runner.lstat().st_ino) == first_identity
    _assert_persistent_private_runner(env, payload)


@pytest.mark.parametrize("remnant", ("mismatch", "extra"))
def test_staged_runner_refuses_untrusted_fixed_directory_remnant(
    monkeypatch,
    tmp_path,
    remnant,
):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    payload = b"#!/bin/sh\nexit 0\n"
    _install_capability_runner(monkeypatch, payload)
    installer = inst.RuntimeInstaller()
    installer._ensure_current_layout()
    micromamba_path = installer._ensure_micromamba(inst.paths.micromamba_path())
    env = inst.paths.env_prefix()
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_bytes(b"python")
    env_info = env.lstat()
    runner_directory = _private_runner_directory(env)
    runner_directory.mkdir(mode=0o700)
    entry = runner_directory / ("micromamba" if remnant == "mismatch" else "unexpected")
    entry.write_bytes(b"untrusted remnant")
    entry.chmod(0o700)
    before = _tree_fingerprint(runner_directory)
    expected_message = "does not match" if remnant == "mismatch" else "extra entries"

    with pytest.raises(inst.InstallError, match=expected_message):
        installer._install_server_package(
            micromamba_path,
            inst.paths.mamba_root_prefix(),
            env,
            expected_env_identity=(env_info.st_dev, env_info.st_ino),
        )

    assert _tree_fingerprint(runner_directory) == before


def test_importable_but_wrong_version_engine_is_rebuilt_not_reused(monkeypatch, tmp_path):
    _prepare_managed_env(monkeypatch, tmp_path, "{broken")
    stale = inst.paths.env_prefix() / "wrong-version.marker"
    stale.write_text("wrong", encoding="utf-8")
    verified = iter((False, True))
    monkeypatch.setattr(inst.status, "verify_runtime", lambda py: next(verified))
    monkeypatch.setattr(inst.status, "health_check", lambda py: True)
    monkeypatch.setattr(inst.status, "engine_compatible", lambda py: False)
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", _materialize_micromamba)
    ran = []

    def fake_run(command, **_kwargs):
        ran.append(command)
        _materialize_created_env(command)

    monkeypatch.setattr(inst, "_run", fake_run)

    inst.RuntimeInstaller().install()

    assert not stale.exists()
    assert ran[0][1] == "create"
    assert not any(cmd[1] == "run" for cmd in ran[:1])


def test_remove_managed_env_rejects_symlink_without_touching_target(monkeypatch, tmp_path):
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "home"))
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    env = inst.paths.env_prefix()
    env.parent.mkdir(parents=True)
    env.symlink_to(outside, target_is_directory=True)

    with pytest.raises(inst.InstallError, match="不安全"):
        inst.RuntimeInstaller()._remove_managed_env(env)

    assert env.is_symlink()
    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("prefix_kind", ["current", "legacy"])
def test_remove_managed_env_rejects_ancestor_alias_without_touching_target(
    monkeypatch, tmp_path, prefix_kind
):
    home = tmp_path / "home"
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    outside_mamba = tmp_path / "outside-mamba"
    outside_env = outside_mamba / "envs" / "vibecad"
    outside_env.mkdir(parents=True)
    marker = outside_env / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    if prefix_kind == "current":
        (home / "runtime").mkdir(parents=True)
        (home / "runtime" / "mamba").symlink_to(
            outside_mamba,
            target_is_directory=True,
        )
        env = inst.paths.env_prefix()
    else:
        home.mkdir(parents=True)
        (home / "mamba").symlink_to(outside_mamba, target_is_directory=True)
        env = inst.paths.legacy_env_prefix()
        (outside_env / ".vibecad_ready").write_text(
            json.dumps(spec.expected_receipt(), sort_keys=True),
            encoding="utf-8",
        )

    with pytest.raises(inst.InstallError, match="不安全"):
        inst.RuntimeInstaller()._remove_managed_env(env)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert outside_env.is_dir()


def test_remove_managed_env_does_not_follow_nested_symlink(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    env = inst.paths.env_prefix()
    nested = env / "nested"
    nested.mkdir(parents=True)
    (nested / "stale.bin").write_bytes(b"stale")
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    (env / "external-link").symlink_to(outside, target_is_directory=True)

    inst.RuntimeInstaller()._remove_managed_env(env)

    assert not env.exists()
    assert marker.read_text(encoding="utf-8") == "keep"


def test_remove_managed_env_restores_parked_entry_when_parent_is_replaced(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    env = inst.paths.env_prefix()
    env.mkdir(parents=True)
    original = env / "original.bin"
    original.write_bytes(b"original")
    parent = env.parent
    detached = parent.with_name("envs-detached")
    outside_parent = tmp_path / "outside-envs"
    outside_env = outside_parent / env.name
    outside_env.mkdir(parents=True)
    outside_marker = outside_env / "keep.txt"
    outside_marker.write_text("keep", encoding="utf-8")
    real_rename = inst._rename
    real_rename_noreplace = inst._rename_noreplace_at
    swapped = False

    def replace_parent_before_park(parent_fd, source, destination):
        nonlocal swapped
        if not swapped:
            swapped = True
            real_rename(parent, detached)
            parent.symlink_to(outside_parent, target_is_directory=True)
        return real_rename_noreplace(parent_fd, source, destination)

    monkeypatch.setattr(inst, "_rename_noreplace_at", replace_parent_before_park)

    with pytest.raises(inst.InstallError, match="安全删除失败"):
        inst.RuntimeInstaller()._remove_managed_env(env)

    assert outside_marker.read_text(encoding="utf-8") == "keep"
    assert (detached / env.name / "original.bin").read_bytes() == b"original"


def test_remove_managed_env_never_replaces_concurrent_live_generation(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    env = inst.paths.env_prefix()
    env.mkdir(parents=True)
    (env / "original.bin").write_bytes(b"original")

    def publish_replacement_then_fail(_directory_fd):
        env.mkdir()
        (env / "replacement.bin").write_bytes(b"replacement")
        raise OSError("deterministic delete interruption")

    monkeypatch.setattr(inst, "_empty_directory_fd", publish_replacement_then_fail)

    with pytest.raises(inst.InstallError, match="安全删除失败"):
        inst.RuntimeInstaller()._remove_managed_env(env)

    assert (env / "replacement.bin").read_bytes() == b"replacement"
    parked = list(env.parent.glob(f".{env.name}.remove-*"))
    assert len(parked) == 1
    assert (parked[0] / "original.bin").read_bytes() == b"original"


def test_remove_managed_env_windows_fallback_parks_and_deletes_once(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    env = inst.paths.env_prefix()
    (env / "nested").mkdir(parents=True)
    (env / "nested" / "stale.bin").write_bytes(b"stale")

    class WindowsPlatform:
        platform = "win32"

    monkeypatch.setattr(inst, "sys", WindowsPlatform())
    original_rename = inst._rename
    renames = []

    def observe_rename(source, destination, *args, **kwargs):
        renames.append((source, destination))
        return original_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(inst, "_rename", observe_rename)

    inst.RuntimeInstaller()._remove_managed_env_fallback(env, legacy=False)

    assert not env.exists()
    assert len(renames) == 1
    assert renames[0][0] == env
    assert not renames[0][1].exists()


def test_override_version_mismatch_never_pip_installs(monkeypatch, tmp_path):
    override = tmp_path / "external"
    python = override / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    (override / ".vibecad_ready").write_text(spec.FREECAD_PIN, encoding="utf-8")
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(override))
    monkeypatch.setattr(inst.status, "verify_runtime", lambda py: False)
    monkeypatch.setattr(inst, "_run", lambda *a, **k: pytest.fail("不得改写用户 env"))

    with pytest.raises(inst.InstallError, match="不会自动改写用户 env"):
        inst.RuntimeInstaller().install()

    assert (override / ".vibecad_ready").read_text(encoding="utf-8") == spec.FREECAD_PIN


def test_matching_override_migrates_receipt_without_pip(monkeypatch, tmp_path):
    override = tmp_path / "external"
    python = override / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    (override / "engine.bin").write_bytes(b"external engine")
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(override))
    monkeypatch.setattr(inst.status, "verify_runtime", lambda py: py == python)
    monkeypatch.setattr(inst, "_run", lambda *a, **k: pytest.fail("不得改写用户 env"))

    before = _tree_fingerprint(override)
    monkeypatch.setattr(
        inst.micromamba,
        "ensure_micromamba",
        lambda *a, **k: pytest.fail("external override 不得触发下载"),
    )

    inst.RuntimeInstaller().install()

    assert inst.status.read_runtime_receipt() == spec.expected_receipt(external=True)
    assert inst.status.runtime_ready() is True
    assert _tree_fingerprint(override) == before
    bound = json.loads(inst.paths.external_runtime_receipt().read_text(encoding="utf-8"))
    assert bound["prefix"] == str(override.resolve())
    assert bound["prefix_inode"] == override.stat().st_ino


@pytest.mark.parametrize("route", ["override", "legacy"])
def test_external_receipt_is_not_published_when_python_changes_after_verify(
    monkeypatch, tmp_path, route
):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    if route == "override":
        prefix = tmp_path / "external"
        monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(prefix))
    else:
        prefix = inst.paths.legacy_env_prefix()
        monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    python = inst.paths.env_python_for(prefix)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"original interpreter")
    if route == "legacy":
        (prefix / ".vibecad_ready").write_text(
            json.dumps(spec.expected_receipt(external=True), sort_keys=True),
            encoding="utf-8",
        )

    def replace_python_after_verify(candidate):
        assert candidate == python
        replacement = python.with_name("python.replacement")
        replacement.write_bytes(b"different interpreter")
        replacement.replace(python)
        return True

    monkeypatch.setattr(inst.status, "verify_runtime", replace_python_after_verify)
    monkeypatch.setattr(
        inst.micromamba,
        "ensure_micromamba",
        lambda *args, **kwargs: pytest.fail("external env must not download"),
    )
    monkeypatch.setattr(inst, "_run", lambda *args, **kwargs: pytest.fail("must not run"))

    with pytest.raises(inst.InstallError, match="generation identity changed"):
        inst.RuntimeInstaller().install()

    assert not inst.paths.external_runtime_receipt().exists()
    assert inst.status.runtime_ready() is False


def test_capability_bound_python_probe_ignores_prefix_replacement_and_writes_no_receipt(
    monkeypatch, tmp_path
):
    home = tmp_path / "VibeCAD"
    prefix = tmp_path / "external"
    python = inst.paths.env_python_for(prefix)
    safe_marker = tmp_path / "safe-python.marker"
    evil_marker = tmp_path / "evil-python.marker"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(prefix))
    monkeypatch.setenv("VIBECAD_TEST_MARKER", str(safe_marker))
    monkeypatch.setenv("VIBECAD_EVIL_MARKER", str(evil_marker))
    python.parent.mkdir(parents=True)
    python.write_bytes(b'#!/bin/sh\nprintf safe > "$VIBECAD_TEST_MARKER"\nexit 0\n')
    python.chmod(0o700)
    monkeypatch.setattr(inst.status, "verify_runtime_generation", _REAL_VERIFY_GENERATION)
    real_spawn = inst.status._spawn_probe_process
    detached = prefix.with_name("external-detached")
    swapped = False

    def replace_prefix_before_spawn(*args, **kwargs):
        nonlocal swapped
        assert not swapped
        swapped = True
        prefix.rename(detached)
        replacement = inst.paths.env_python_for(prefix)
        replacement.parent.mkdir(parents=True)
        replacement.write_bytes(b'#!/bin/sh\nprintf evil > "$VIBECAD_EVIL_MARKER"\nexit 0\n')
        replacement.chmod(0o700)
        return real_spawn(*args, **kwargs)

    monkeypatch.setattr(inst.status, "_spawn_probe_process", replace_prefix_before_spawn)

    with pytest.raises(inst.InstallError, match="不会自动改写用户 env"):
        inst.RuntimeInstaller().install()

    assert swapped is True
    assert safe_marker.read_bytes() == b"safe"
    assert not evil_marker.exists()
    assert not inst.paths.external_runtime_receipt().exists()


def test_exact_legacy_external_runtime_is_reused_read_only_without_second_install(
    monkeypatch, tmp_path
):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    legacy = home / "mamba" / "envs" / "vibecad"
    python = inst.paths.env_python_for(legacy)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"legacy python")
    (legacy / "engine.bin").write_bytes(b"legacy FreeCAD")
    (legacy / ".vibecad_ready").write_text(
        json.dumps(spec.expected_receipt(external=True), sort_keys=True),
        encoding="utf-8",
    )
    before = _tree_fingerprint(legacy)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda py: py == python)
    monkeypatch.setattr(
        inst.micromamba,
        "ensure_micromamba",
        lambda *a, **k: pytest.fail("健康 legacy FreeCAD 不得下载第二套引擎"),
    )
    monkeypatch.setattr(
        inst,
        "_run",
        lambda *a, **k: pytest.fail("健康 external-kind legacy 不得 create/pip"),
    )

    inst.RuntimeInstaller().install()

    assert inst.paths.active_runtime_prefix() == legacy
    assert _tree_fingerprint(legacy) == before
    receipt = json.loads(inst.paths.external_runtime_receipt().read_text(encoding="utf-8"))
    assert receipt["prefix"] == str(legacy.resolve())
    assert receipt["runtime_kind"] == spec.EXTERNAL_KIND


@pytest.mark.parametrize("receipt_kind", ("unknown", "pre_epoch_external"))
def test_unowned_legacy_is_preserved_while_new_runtime_is_created(
    monkeypatch,
    tmp_path,
    receipt_kind,
):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    legacy = home / "mamba" / "envs" / "vibecad"
    legacy_python = inst.paths.env_python_for(legacy)
    legacy_python.parent.mkdir(parents=True)
    legacy_python.write_bytes(b"unknown python")
    if receipt_kind == "unknown":
        receipt = "{not-owned"
    else:
        old_external = {
            "schema": spec.RECEIPT_SCHEMA,
            "runtime_kind": spec.EXTERNAL_KIND,
            "vibecad_version": spec.VIBECAD_VERSION,
        }
        receipt = json.dumps(old_external, sort_keys=True)
    (legacy / ".vibecad_ready").write_text(receipt, encoding="utf-8")
    before = _tree_fingerprint(legacy)
    calls = []
    monkeypatch.setattr(inst.RuntimeInstaller, "is_ready", lambda self: False)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda py: py == inst.paths.env_python())
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", _materialize_micromamba)

    def fake_run(command, **_kwargs):
        calls.append(command)
        _materialize_created_env(command)

    monkeypatch.setattr(inst, "_run", fake_run)

    inst.RuntimeInstaller().install()

    assert _tree_fingerprint(legacy) == before
    create = next(command for command in calls if command[1] == "create")
    assert create[create.index("-p") + 1] == "./"
    assert create[create.index("-r") + 1] == "../.."
    assert inst.paths.env_prefix().is_dir()


def test_exact_managed_legacy_is_verified_and_reused_in_place(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    legacy = home / "mamba" / "envs" / "vibecad"
    python = inst.paths.env_python_for(legacy)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"managed python")
    (legacy / ".vibecad_ready").write_text(
        json.dumps(spec.expected_receipt(), sort_keys=True), encoding="utf-8"
    )
    before = _tree_fingerprint(legacy)
    probes = []
    monkeypatch.setattr(
        inst.status,
        "verify_runtime",
        lambda candidate: probes.append(candidate) or candidate == python,
    )
    monkeypatch.setattr(inst, "_run", lambda *a, **k: pytest.fail("精确 legacy 不得重装"))
    monkeypatch.setattr(
        inst.micromamba,
        "ensure_micromamba",
        lambda *a, **k: pytest.fail("精确 legacy 不得下载"),
    )

    inst.RuntimeInstaller().install()

    assert probes and all(candidate == python for candidate in probes)
    assert inst.paths.active_runtime_prefix() == legacy
    assert _tree_fingerprint(legacy) == before


def test_managed_legacy_is_not_ready_when_python_changes_during_probe(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    legacy = inst.paths.legacy_env_prefix()
    python = inst.paths.env_python_for(legacy)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"managed python")
    (legacy / ".vibecad_ready").write_text(
        json.dumps(spec.expected_receipt(), sort_keys=True),
        encoding="utf-8",
    )

    def replace_python_after_verify(candidate):
        replacement = python.with_name("python.replacement")
        replacement.write_bytes(b"different interpreter")
        replacement.replace(python)
        return True

    monkeypatch.setattr(inst.status, "verify_runtime", replace_python_after_verify)

    assert inst.RuntimeInstaller().is_ready() is False


def test_unhealthy_external_legacy_is_preserved_and_new_runtime_is_installed(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    legacy = inst.paths.legacy_env_prefix()
    legacy_python = inst.paths.env_python_for(legacy)
    legacy_python.parent.mkdir(parents=True)
    legacy_python.write_bytes(b"unhealthy external python")
    (legacy / ".vibecad_ready").write_text(
        json.dumps(spec.expected_receipt(external=True), sort_keys=True), encoding="utf-8"
    )
    before = _tree_fingerprint(legacy)
    calls = []
    monkeypatch.setattr(inst.RuntimeInstaller, "is_ready", lambda self: False)
    monkeypatch.setattr(
        inst.status,
        "verify_runtime",
        lambda candidate: candidate == inst.paths.env_python(),
    )
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", _materialize_micromamba)

    def fake_run(command, **_kwargs):
        calls.append(command)
        _materialize_created_env(command)

    monkeypatch.setattr(inst, "_run", fake_run)

    inst.RuntimeInstaller().install()

    assert _tree_fingerprint(legacy) == before
    assert any(command[1] == "create" for command in calls)
    assert inst.status.read_runtime_receipt() == spec.expected_receipt()


def test_stale_owned_legacy_uses_legacy_micromamba_for_pip_only(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    legacy = inst.paths.legacy_env_prefix()
    python = inst.paths.env_python_for(legacy)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"managed python")
    stale = {**spec.expected_receipt(), "vibecad_version": "0.3.0"}
    (legacy / ".vibecad_ready").write_text(json.dumps(stale, sort_keys=True), encoding="utf-8")
    verified = iter((False, True))
    monkeypatch.setattr(inst.status, "verify_runtime", lambda candidate: next(verified))
    monkeypatch.setattr(inst.status, "engine_compatible", lambda candidate: candidate == python)
    ensured = []
    monkeypatch.setattr(
        inst.micromamba,
        "ensure_micromamba",
        lambda path: _materialize_micromamba(path, ensured),
    )
    calls = []
    monkeypatch.setattr(inst, "_run", lambda command, **kwargs: calls.append(command))

    inst.RuntimeInstaller().install()

    assert ensured == []
    assert inst.paths.legacy_micromamba_path().read_bytes() == _FAKE_MICROMAMBA
    assert len(calls) == 1
    assert Path(calls[0][0]).name == "micromamba"
    assert Path(calls[0][0]).parent.name == inst._RUNNER_DIRECTORY_NAME
    assert calls[0][1:6] == [
        "run",
        "-r",
        "../..",
        "-p",
        "./",
    ]
    assert inst.status.read_prefix_receipt(legacy) == spec.expected_receipt()


def test_legacy_pip_sync_keeps_old_receipt_when_verified_python_is_replaced(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    legacy = inst.paths.legacy_env_prefix()
    python = inst.paths.env_python_for(legacy)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"managed python")
    stale = {**spec.expected_receipt(), "vibecad_version": "0.3.0"}
    receipt = legacy / ".vibecad_ready"
    receipt.write_text(json.dumps(stale, sort_keys=True), encoding="utf-8")
    probes = 0

    def verify_and_replace(candidate):
        nonlocal probes
        probes += 1
        if probes == 1:
            return False
        replacement = python.with_name("python.replacement")
        replacement.write_bytes(b"different interpreter")
        replacement.replace(python)
        return True

    monkeypatch.setattr(inst.status, "verify_runtime", verify_and_replace)
    monkeypatch.setattr(inst.status, "engine_compatible", lambda candidate: candidate == python)
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", _materialize_micromamba)
    monkeypatch.setattr(inst, "_run", lambda command, **kwargs: None)

    with pytest.raises(inst.InstallError, match="generation identity changed"):
        inst.RuntimeInstaller().install()

    assert json.loads(receipt.read_text(encoding="utf-8")) == stale
    assert inst.status.runtime_ready() is False


def test_pip_spec_prefers_explicit_then_checkout_then_exact_version(monkeypatch, tmp_path):
    local = tmp_path / "local source"
    local.mkdir()
    monkeypatch.setenv("VIBECAD_PIP_SPEC", str(local))
    assert inst._pip_spec() == str(local.resolve())

    monkeypatch.delenv("VIBECAD_PIP_SPEC")
    monkeypatch.setattr(inst, "_local_source_root", lambda: local)
    assert inst._pip_spec() == str(local)

    monkeypatch.setattr(inst, "_local_source_root", lambda: None)
    assert inst._pip_spec() == f"vibecad=={spec.VIBECAD_VERSION}"


def test_mcpb_entry_uses_its_own_project_as_pip_source(monkeypatch):
    root = Path(__file__).resolve().parent.parent
    monkeypatch.delenv("VIBECAD_PIP_SPEC", raising=False)

    runpy.run_path(str(root / "mcpb_entry.py"), run_name="mcpb_entry_test")

    assert inst.os.environ["VIBECAD_PIP_SPEC"] == str(root)


def test_install_failed_on_smoke(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(inst.RuntimeInstaller, "is_ready", lambda self: False)
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", _materialize_micromamba)
    monkeypatch.setattr(inst, "_run", lambda cmd, **k: None)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda *a, **k: False)
    seen = []
    with pytest.raises(inst.InstallError):
        inst.RuntimeInstaller(on_progress=lambda s: seen.append(s)).install()
    assert seen[-1].phase is Phase.FAILED


def test_run_redirects_stdout(monkeypatch, tmp_path):
    # B2: 子进程绝不继承 fd1
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    captured = {}

    class P:
        returncode = 0
        stdout = "ok"

    def fake_run(cmd, **kw):
        captured.update(kw)
        return P()

    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    inst._run(["echo", "hi"])
    assert captured["stdout"] is inst.subprocess.PIPE
    assert captured["stderr"] is inst.subprocess.STDOUT


def test_run_fd_spawn_uses_clean_helper_without_preexec(monkeypatch, tmp_path):
    directory = tmp_path / "pinned"
    directory.mkdir()
    directory_fd = inst.os.open(directory, inst.os.O_RDONLY)
    captured = {}

    class P:
        returncode = 0
        stdout = "ok"

    def fake_spawn(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return P()

    monkeypatch.setattr(inst, "_spawn_process", fake_spawn)
    try:
        inst._run(["./runner", "argument"], cwd_fd=directory_fd)
    finally:
        inst.os.close(directory_fd)

    command = captured["command"]
    options = captured["kwargs"]
    assert "preexec_fn" not in options
    assert options["pass_fds"] == (directory_fd,)
    assert command[:4] == [inst.sys.executable, "-I", "-B", "-c"]
    assert command[4] == inst._FD_EXEC_HELPER
    assert command[5] == str(directory_fd)
    assert command[6:] == ["./runner", "argument"]


def test_run_fd_helper_finishes_while_another_thread_holds_a_python_lock(monkeypatch, tmp_path):
    directory = tmp_path / "pinned"
    directory.mkdir()
    target = directory / "python"
    target.symlink_to(inst.sys.executable)
    marker = tmp_path / "helper.marker"
    directory_fd = inst.os.open(directory, inst.os.O_RDONLY)
    held_lock = threading.Lock()
    ready = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with held_lock:
            ready.set()
            release.wait(timeout=5)

    worker = threading.Thread(target=hold_lock)
    worker.start()
    assert ready.wait(timeout=2)
    monkeypatch.setattr(inst.status, "append_install_log", lambda record: None)
    try:
        inst._run(
            [
                "./python",
                "-I",
                "-B",
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('done')",
            ],
            cwd_fd=directory_fd,
        )
    finally:
        inst.os.close(directory_fd)
        release.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert marker.read_text(encoding="utf-8") == "done"


def test_run_does_not_follow_install_log_symlink_or_change_command_result(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    data = home / "data"
    runtime.mkdir(parents=True)
    data.mkdir()
    protected = data / "protected.log"
    protected.write_text("durable", encoding="utf-8")
    (runtime / "install.log").symlink_to(protected)
    monkeypatch.setenv("VIBECAD_HOME", str(home))

    class P:
        returncode = 0
        stdout = "command succeeded"

    monkeypatch.setattr(inst.subprocess, "run", lambda command, **kwargs: P())

    inst._run(["safe", "command"])

    assert protected.read_text(encoding="utf-8") == "durable"
    assert (runtime / "install.log").is_symlink()


def test_run_raises_on_nonzero(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))  # m-7：非零返回码须抛 InstallError

    class P:
        returncode = 1
        stdout = "boom"

    monkeypatch.setattr(inst.subprocess, "run", lambda cmd, **kw: P())
    with pytest.raises(inst.InstallError):
        inst._run(["false"])
