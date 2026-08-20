from __future__ import annotations

import hashlib
import json
import os
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from vibecad import freecad_launcher


class _Process:
    def __init__(self, returncode: int = 0) -> None:
        self.pid = 4321
        self.returncode = returncode
        self.sent: list[int] = []

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.sent.append(signal.SIGTERM)

    def kill(self) -> None:
        self.sent.append(signal.SIGKILL)

    def send_signal(self, signum: int) -> None:
        self.sent.append(signum)


def test_override_is_rejected_before_installer_construction(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(freecad_launcher.paths, "user_override_env", lambda: tmp_path)
    monkeypatch.setattr(
        freecad_launcher,
        "RuntimeInstaller",
        lambda **_kwargs: pytest.fail("override must fail before installer construction"),
    )

    with pytest.raises(freecad_launcher.FreeCADLaunchError, match="not accepted"):
        freecad_launcher._require_managed_runtime()


def test_local_distribution_wheel_uses_direct_install_artifact(
    monkeypatch,
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "vibecad-0.6.0-py3-none-any.whl"
    wheel.write_bytes(b"reviewed-wheel")

    class Distribution:
        @staticmethod
        def read_text(name: str) -> str | None:
            assert name == "direct_url.json"
            return json.dumps({"url": wheel.as_uri(), "archive_info": {}})

    monkeypatch.setattr(
        freecad_launcher.importlib.metadata,
        "distribution",
        lambda name: Distribution() if name == "vibecad" else None,
    )

    assert freecad_launcher._local_distribution_wheel() == (
        wheel,
        hashlib.sha256(b"reviewed-wheel").hexdigest(),
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows path/descriptor stat contract")
def test_wheel_binding_uses_birthtime_across_windows_stat_views() -> None:
    common = {
        "st_dev": 1,
        "st_ino": 2,
        "st_mode": 0o100600,
        "st_nlink": 1,
        "st_size": 14,
        "st_mtime_ns": 3,
        "st_birthtime_ns": 4,
    }
    path_view = SimpleNamespace(**common, st_ctime_ns=4)
    descriptor_view = SimpleNamespace(**common, st_ctime_ns=3)

    assert freecad_launcher._wheel_file_binding(path_view) == (
        freecad_launcher._wheel_file_binding(descriptor_view)
    )
    changed_birthtime = SimpleNamespace(**(common | {"st_birthtime_ns": 5}), st_ctime_ns=3)
    assert freecad_launcher._wheel_file_binding(path_view) != (
        freecad_launcher._wheel_file_binding(changed_birthtime)
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows addon file capability contract")
def test_packaged_addon_uses_windows_file_identity_instead_of_posix_mode_bits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    addon = (tmp_path / "VibeCAD").resolve()
    addon.mkdir()
    source = addon / "InitGui.py"
    source.write_bytes(b"# Windows Workbench\n")
    observed: list[object] = []
    real_open = freecad_launcher.open_windows_external_file
    real_validate = freecad_launcher.validate_windows_external_file

    def open_file(path: Path):
        descriptor, capability = real_open(path)
        observed.append(capability)
        return descriptor, capability

    def validate_file(capability):
        observed.append(capability)
        return real_validate(capability)

    monkeypatch.setattr(freecad_launcher, "_packaged_addon_root", lambda: addon)
    monkeypatch.setattr(freecad_launcher, "_ADDON_FILES", frozenset({"InitGui.py"}))
    monkeypatch.setattr(freecad_launcher, "open_windows_external_file", open_file)
    monkeypatch.setattr(freecad_launcher, "validate_windows_external_file", validate_file)

    assert freecad_launcher._require_packaged_addon() == addon
    assert len(observed) == 2
    assert observed[0] == observed[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows addon file capability contract")
def test_packaged_addon_rejects_windows_identity_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    addon = (tmp_path / "VibeCAD").resolve()
    addon.mkdir()
    (addon / "InitGui.py").write_bytes(b"# Windows Workbench\n")
    monkeypatch.setattr(freecad_launcher, "_packaged_addon_root", lambda: addon)
    monkeypatch.setattr(freecad_launcher, "_ADDON_FILES", frozenset({"InitGui.py"}))
    monkeypatch.setattr(
        freecad_launcher,
        "validate_windows_external_file",
        lambda _capability: (_ for _ in ()).throw(OSError("identity changed")),
    )

    with pytest.raises(freecad_launcher.FreeCADLaunchError, match="addon is incomplete"):
        freecad_launcher._require_packaged_addon()


def test_managed_runtime_refreshes_from_local_install_before_capture(
    monkeypatch,
    tmp_path: Path,
) -> None:
    prefix = (tmp_path / "managed").resolve()
    binary = prefix / "bin" / "FreeCAD"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"freecad")
    binary.chmod(0o755)
    wheel = tmp_path / "vibecad-0.6.0-py3-none-any.whl"
    calls: list[object] = []
    evidence = SimpleNamespace(prefix=prefix)

    class Installer:
        @staticmethod
        def is_ready() -> bool:
            return True

        @staticmethod
        def refresh_server_package(value: Path, *, expected_sha256: str) -> None:
            calls.append(("refresh", value, expected_sha256))

    monkeypatch.setattr(freecad_launcher.paths, "user_override_env", lambda: None)
    monkeypatch.setattr(freecad_launcher.paths, "env_prefix", lambda: prefix)
    monkeypatch.setattr(freecad_launcher.paths, "active_runtime_prefix", lambda: prefix)
    monkeypatch.setattr(freecad_launcher.paths, "freecad_path", lambda: binary)
    monkeypatch.setattr(freecad_launcher, "RuntimeInstaller", lambda **_kwargs: Installer())
    monkeypatch.setattr(
        freecad_launcher,
        "_local_distribution_wheel",
        lambda: (wheel, "a" * 64),
    )
    monkeypatch.setattr(
        freecad_launcher.status,
        "read_prefix_receipt",
        lambda value: freecad_launcher.spec.expected_receipt() if value == prefix else None,
    )

    def capture(value: Path):
        calls.append(("capture", value))
        return evidence

    monkeypatch.setattr(
        freecad_launcher.status,
        "capture_runtime_generation_evidence",
        capture,
    )
    monkeypatch.setattr(
        freecad_launcher.status,
        "verify_runtime_generation",
        lambda value: value is evidence,
    )

    assert freecad_launcher._require_managed_runtime() == (prefix, evidence, binary)
    assert calls[0] == ("refresh", wheel, "a" * 64)
    assert all(call[0] == "capture" for call in calls[1:])


def test_private_profile_and_activation_script_are_isolated(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "session"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    monkeypatch.setenv("PYTHONHOME", "host-python")
    monkeypatch.setenv("PYTHONPATH", "host-path")
    monkeypatch.setenv("VIRTUAL_ENV", "host-venv")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "host-uv")
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", "host-freecad")

    profile = freecad_launcher._prepare_private_profile(root.resolve(strict=True))
    activation_script = freecad_launcher._write_activation_script(root.resolve(strict=True))
    ready_file = root / "workbench.ready"
    error_file = root / "workbench.error"
    managed_prefix = tmp_path / "managed"
    environment = freecad_launcher._child_environment(
        profile,
        ready_file,
        error_file,
        managed_prefix,
    )

    source = activation_script.read_text(encoding="utf-8")
    compile(source, str(activation_script), "exec")
    assert "FreeCADGui.activateWorkbench(expected)" in source
    assert "host.workbench_snapshot()" in source
    assert "threading.Timer(0.1, observe)" in source
    if os.name == "nt":
        assert freecad_launcher.capture_windows_path(
            activation_script,
            directory=False,
        ).path == str(activation_script)
        for directory in (root, *profile):
            assert freecad_launcher.capture_windows_path(
                directory.resolve(strict=True),
                directory=True,
            ).path == str(directory.resolve(strict=True))
    else:
        assert activation_script.stat().st_mode & 0o777 == 0o600
    assert all(
        name not in environment
        for name in freecad_launcher._ENVIRONMENT_INJECTION
        if name not in {"VIBECAD_FREECAD_READY_FILE", "VIBECAD_FREECAD_ERROR_FILE"}
    )
    assert environment["FREECAD_USER_HOME"] == str(profile[0])
    assert environment["FREECAD_USER_DATA"] == str(profile[1])
    assert environment["FREECAD_USER_TEMP"] == str(profile[2])
    assert environment["TMPDIR"] == str(profile[3])
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["VIBECAD_FREECAD_ERROR_FILE"] == str(error_file)
    assert environment["VIBECAD_FREECAD_READY_FILE"] == str(ready_file)
    assert environment["VIBECAD_HOME"] == str(freecad_launcher.paths.vibecad_home())
    if os.name == "nt":
        assert environment["PATH"].split(os.pathsep)[:3] == [
            str(managed_prefix / "Library" / "bin"),
            str(managed_prefix),
            str(managed_prefix / "Scripts"),
        ]
        assert environment["TEMP"] == str(profile[3])
        assert environment["TMP"] == str(profile[3])


def test_launch_uses_one_absolute_managed_binary_and_packaged_addon(
    monkeypatch,
    tmp_path: Path,
) -> None:
    addon = tmp_path / "site-packages" / "vibecad" / "_freecad" / "VibeCAD"
    addon.mkdir(parents=True)
    prefix = tmp_path / "managed"
    prefix.mkdir()
    binary = prefix / "bin" / "FreeCAD"
    binary.parent.mkdir()
    binary.write_bytes(b"binary")
    evidence = object()
    process = _Process()
    launches: list[tuple[list[str], dict[str, str], bool]] = []

    monkeypatch.setattr(freecad_launcher, "_require_packaged_addon", lambda: addon)
    monkeypatch.setattr(
        freecad_launcher,
        "_require_managed_runtime",
        lambda: (prefix, evidence, binary),
    )
    monkeypatch.setattr(
        freecad_launcher.subprocess,
        "Popen",
        lambda command, *, env, start_new_session: (
            launches.append((command, env, start_new_session)),
            process,
        )[1],
    )
    monkeypatch.setattr(
        freecad_launcher,
        "_wait_for_ready",
        lambda _process, _ready, _error: None,
    )
    monkeypatch.setattr(freecad_launcher, "_wait_for_process", lambda value: value.wait())
    monkeypatch.setattr(
        freecad_launcher.status,
        "capture_runtime_generation_evidence",
        lambda value: evidence if value == prefix else pytest.fail("unexpected prefix"),
    )
    monkeypatch.setattr(
        freecad_launcher.status,
        "verify_runtime_generation",
        lambda value: value is evidence,
    )

    assert freecad_launcher.launch() == 0
    assert len(launches) == 1
    command, environment, start_new_session = launches[0]
    assert command[0:2] == [str(binary), "-u"]
    assert Path(command[2]).name == "user.cfg"
    assert command[3] == "--log-file"
    assert Path(command[4]).name == "FreeCAD.log"
    assert command[5:7] == ["-M", str(addon)]
    assert Path(command[7]).name == "activate_vibecad.py"
    assert start_new_session is True
    profile_roots = {
        Path(environment[name]).parent
        for name in ("FREECAD_USER_HOME", "FREECAD_USER_DATA", "FREECAD_USER_TEMP", "TMPDIR")
    }
    assert len(profile_roots) == 1
    assert profile_roots.pop().name.startswith("vibecad-freecad-")
    assert "VIBECAD_FREECAD_ENV" not in environment


def test_launch_fails_closed_if_runtime_changes_after_child_exit(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    prefix = tmp_path / "managed"
    binary = prefix / "bin" / "FreeCAD"
    evidence = object()
    monkeypatch.setattr(freecad_launcher, "_require_packaged_addon", lambda: tmp_path / "addon")
    monkeypatch.setattr(
        freecad_launcher,
        "_require_managed_runtime",
        lambda: (prefix, evidence, binary),
    )
    monkeypatch.setattr(freecad_launcher.subprocess, "Popen", lambda *_args, **_kwargs: _Process())
    monkeypatch.setattr(
        freecad_launcher,
        "_wait_for_ready",
        lambda _process, _ready, _error: None,
    )
    monkeypatch.setattr(freecad_launcher, "_wait_for_process", lambda process: process.wait())
    monkeypatch.setattr(
        freecad_launcher.status,
        "capture_runtime_generation_evidence",
        lambda _prefix: object(),
    )

    assert freecad_launcher.launch() == 1
    assert "changed during the GUI session" in capsys.readouterr().err


def test_launch_reaps_child_if_wait_setup_fails(monkeypatch, tmp_path: Path, capsys) -> None:
    prefix = tmp_path / "managed"
    binary = prefix / "bin" / "FreeCAD"
    evidence = object()
    process = _Process()
    reaped: list[_Process] = []
    monkeypatch.setattr(freecad_launcher, "_require_packaged_addon", lambda: tmp_path / "addon")
    monkeypatch.setattr(
        freecad_launcher,
        "_require_managed_runtime",
        lambda: (prefix, evidence, binary),
    )
    monkeypatch.setattr(freecad_launcher.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        freecad_launcher,
        "_wait_for_ready",
        lambda _process, _ready, _error: None,
    )
    monkeypatch.setattr(
        freecad_launcher,
        "_wait_for_process",
        lambda _process: (_ for _ in ()).throw(ValueError("signal setup failed")),
    )
    monkeypatch.setattr(
        freecad_launcher,
        "_terminate_and_reap",
        lambda value: reaped.append(value),
    )

    assert freecad_launcher.launch() == 1
    assert reaped == [process]
    assert "signal setup failed" in capsys.readouterr().err


def test_terminate_and_reap_uses_native_kill_when_sigkill_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 4321

        def __init__(self) -> None:
            self.waits = 0
            self.terminated = 0
            self.killed = 0

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated += 1

        def send_signal(self, signum: int) -> None:
            assert signum == signal.SIGTERM
            self.terminated += 1

        def kill(self) -> None:
            self.killed += 1

        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            if self.waits == 1:
                raise freecad_launcher.subprocess.TimeoutExpired("FreeCAD", timeout)
            return 1

    process = Process()
    monkeypatch.delattr(freecad_launcher.signal, "SIGKILL", raising=False)

    freecad_launcher._terminate_and_reap(process)  # type: ignore[arg-type]

    assert process.terminated == 1
    assert process.killed == 1
    assert process.waits == 2


def test_startup_handshake_requires_exact_active_workbench_marker(tmp_path: Path) -> None:
    ready_file = tmp_path / "workbench.ready"
    error_file = tmp_path / "workbench.error"
    ready_file.write_text("VibeCADWorkbench\n", encoding="utf-8")
    freecad_launcher._wait_for_ready(_Process(), ready_file, error_file)

    process = _Process()
    process.poll = lambda: 1  # type: ignore[method-assign]
    with pytest.raises(freecad_launcher.FreeCADLaunchError, match="exited before"):
        freecad_launcher._wait_for_ready(process, tmp_path / "absent.ready", error_file)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group forwarding")
def test_wait_for_process_maps_interrupt_to_gui_termination_and_restores_handlers(
    monkeypatch,
) -> None:
    process = _Process(returncode=-signal.SIGTERM)
    handlers: dict[int, object] = {
        signal.SIGINT: object(),
        signal.SIGTERM: object(),
    }
    original = dict(handlers)
    forwarded: list[tuple[int, int]] = []

    def install(signum: int, handler: object) -> object:
        previous = handlers[signum]
        handlers[signum] = handler
        return previous

    def wait() -> int:
        handler = handlers[signal.SIGINT]
        assert callable(handler)
        handler(signal.SIGINT, None)
        return process.returncode

    process.wait = wait  # type: ignore[method-assign]
    monkeypatch.setattr(freecad_launcher.signal, "signal", install)
    monkeypatch.setattr(
        freecad_launcher.os,
        "killpg",
        lambda pid, signum: forwarded.append((pid, signum)),
    )

    assert freecad_launcher._wait_for_process(process) == -signal.SIGINT
    assert forwarded == [(process.pid, signal.SIGTERM)]
    assert handlers == original
    assert freecad_launcher._exit_code(-signal.SIGINT) == 128 + signal.SIGINT
