import json
import runpy
from pathlib import Path

import pytest

from vibecad.runtime import installer as inst
from vibecad.runtime import spec
from vibecad.runtime.status import Phase


def test_install_happy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(inst.RuntimeInstaller, "is_ready", lambda self: False)   # 不短路
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", lambda dest, **k: dest)
    ran = []
    monkeypatch.setattr(inst, "_run", lambda cmd, **k: ran.append(cmd))
    monkeypatch.setattr(inst.status, "verify_runtime", lambda *a, **k: True)      # VERIFYING 过
    seen = []
    inst.RuntimeInstaller(on_progress=lambda s: seen.append(s.phase)).install()
    assert Phase.CREATING_ENV in seen and Phase.INSTALLING_PIP in seen and seen[-1] is Phase.READY
    create = " ".join(map(str, ran[0]))
    assert "create" in create and "python=3.12" in create and "freecad=1.1.0" in create
    assert inst.status.read_runtime_receipt() == spec.expected_receipt()
    assert "--upgrade" in ran[1]
    assert ran[1][0] == str(inst.paths.micromamba_path()) and ran[1][1] == "run"


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


@pytest.mark.parametrize("legacy", [True, False])
def test_legacy_or_old_version_reuses_healthy_env_for_pip_only(
    monkeypatch, tmp_path, legacy
):
    receipt = spec.FREECAD_PIN if legacy else {
        **spec.expected_receipt(),
        "vibecad_version": "0.3.0",
    }
    python, _ = _prepare_managed_env(monkeypatch, tmp_path, receipt)
    monkeypatch.setattr(inst.status, "engine_compatible", lambda py: py == python)
    verified = iter((False, True))
    monkeypatch.setattr(inst.status, "verify_runtime", lambda py: next(verified))
    ensured = []
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", lambda path: ensured.append(path))
    ran = []
    monkeypatch.setattr(inst, "_run", lambda cmd, **k: ran.append(cmd))

    inst.RuntimeInstaller().install()

    assert len(ran) == 1
    assert ran[0][:6] == [
        str(inst.paths.micromamba_path()),
        "run",
        "-r",
        str(inst.paths.mamba_root_prefix()),
        "-p",
        str(inst.paths.env_prefix()),
    ]
    assert ran[0][6:10] == ["python", "-m", "pip", "install"]
    assert "--upgrade" in ran[0]
    assert ensured == [inst.paths.micromamba_path()]
    assert inst.status.read_runtime_receipt() == spec.expected_receipt()


def test_legacy_receipt_migrates_without_pip_when_package_is_already_exact(
    monkeypatch, tmp_path
):
    python, _ = _prepare_managed_env(monkeypatch, tmp_path, spec.FREECAD_PIN)
    monkeypatch.setattr(inst.status, "engine_compatible", lambda py: py == python)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda py: True)
    monkeypatch.setattr(inst, "_run", lambda *a, **k: pytest.fail("无需重复 pip"))

    inst.RuntimeInstaller().install()

    assert inst.status.read_runtime_receipt() == spec.expected_receipt()


@pytest.mark.parametrize("receipt", [None, "{broken"])
def test_missing_or_corrupt_receipt_is_repaired_when_env_is_exact(
    monkeypatch, tmp_path, receipt
):
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


def test_failed_server_sync_keeps_old_receipt(monkeypatch, tmp_path):
    old = {**spec.expected_receipt(), "vibecad_version": "0.3.0"}
    python, sentinel = _prepare_managed_env(monkeypatch, tmp_path, old)
    monkeypatch.setattr(inst.status, "engine_compatible", lambda py: py == python)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda py: False)
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", lambda path: path)
    monkeypatch.setattr(inst, "_run", lambda cmd, **k: None)

    with pytest.raises(inst.InstallError):
        inst.RuntimeInstaller().install()

    assert json.loads(sentinel.read_text(encoding="utf-8")) == old
    assert inst.status.runtime_ready() is False


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
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", lambda path: path)
    ran = []

    def fake_run(cmd, **kwargs):
        assert not stale.exists(), "existing prefix 必须在 create 前删除"
        ran.append(cmd)

    monkeypatch.setattr(inst, "_run", fake_run)

    inst.RuntimeInstaller().install()

    assert ran[0][1] == "create"
    assert keep.read_text(encoding="utf-8") == "keep"
    assert inst.status.read_runtime_receipt() == spec.expected_receipt()
    assert python.parent.parent == env


def test_importable_but_wrong_version_engine_is_rebuilt_not_reused(monkeypatch, tmp_path):
    _prepare_managed_env(monkeypatch, tmp_path, "{broken")
    stale = inst.paths.env_prefix() / "wrong-version.marker"
    stale.write_text("wrong", encoding="utf-8")
    verified = iter((False, True))
    monkeypatch.setattr(inst.status, "verify_runtime", lambda py: next(verified))
    monkeypatch.setattr(inst.status, "health_check", lambda py: True)
    monkeypatch.setattr(inst.status, "engine_compatible", lambda py: False)
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", lambda path: path)
    ran = []
    monkeypatch.setattr(inst, "_run", lambda cmd, **kwargs: ran.append(cmd))

    inst.RuntimeInstaller().install()

    assert not stale.exists()
    assert ran[0][1] == "create"
    assert not any(cmd[1] == "run" for cmd in ran[:1])


def test_remove_managed_env_unlinks_symlink_without_touching_target(monkeypatch, tmp_path):
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "home"))
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    env = inst.paths.env_prefix()
    env.parent.mkdir(parents=True)
    env.symlink_to(outside, target_is_directory=True)

    inst.RuntimeInstaller()._remove_managed_env(env)

    assert not env.exists() and not env.is_symlink()
    assert marker.read_text(encoding="utf-8") == "keep"


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
    (override / ".vibecad_ready").write_text(spec.FREECAD_PIN, encoding="utf-8")
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(override))
    monkeypatch.setattr(inst.status, "verify_runtime", lambda py: py == python)
    monkeypatch.setattr(inst, "_run", lambda *a, **k: pytest.fail("不得改写用户 env"))

    inst.RuntimeInstaller().install()

    assert inst.status.read_runtime_receipt() == spec.expected_receipt(external=True)
    assert inst.status.runtime_ready() is True


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
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", lambda dest, **k: dest)
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


def test_run_raises_on_nonzero(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))  # m-7：非零返回码须抛 InstallError

    class P:
        returncode = 1
        stdout = "boom"
    monkeypatch.setattr(inst.subprocess, "run", lambda cmd, **kw: P())
    with pytest.raises(inst.InstallError):
        inst._run(["false"])
