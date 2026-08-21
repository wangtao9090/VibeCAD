"""Launch the packaged VibeCAD Workbench in the managed FreeCAD GUI."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from vibecad._file_compat import (
    capture_windows_external_fd,
    capture_windows_path,
    ensure_private_directory,
    open_private_file,
    open_windows_external_file,
    set_private_dacl,
    validate_windows_external_file,
    validate_windows_path,
)
from vibecad.freecad_env import activate_windows_runtime_environment
from vibecad.runtime import paths, spec, status
from vibecad.runtime.installer import RuntimeInstaller

_ADDON_FILES = frozenset(
    {
        "Init.py",
        "InitGui.py",
        "package.xml",
        "vibecad_workbench/__init__.py",
        "vibecad_workbench/bridge.py",
        "vibecad_workbench/dock.py",
        "vibecad_workbench/gateway.py",
        "vibecad_workbench/host.py",
        "vibecad_workbench/preview.py",
        "vibecad_workbench/selection.py",
        "vibecad_workbench/state.py",
    }
)
_ENVIRONMENT_INJECTION = frozenset(
    {
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "UV_PROJECT_ENVIRONMENT",
        "VIBECAD_FREECAD_ENV",
        "VIBECAD_FREECAD_ERROR_FILE",
        "VIBECAD_FREECAD_READY_FILE",
    }
)
_USER_CONFIG = """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<FCParameters>
  <FCParamGroup Name="Root">
    <FCParamGroup Name="BaseApp">
      <FCParamGroup Name="Preferences">
        <FCParamGroup Name="General"/>
      </FCParamGroup>
    </FCParamGroup>
  </FCParamGroup>
</FCParameters>
"""
_ACTIVATION_SCRIPT = """import os
from pathlib import Path

error_file = Path(os.environ["VIBECAD_FREECAD_ERROR_FILE"])
error_file.write_text("pending_started\\n", encoding="utf-8")
try:
    import threading
    import time
    import FreeCADGui

    expected = "VibeCADWorkbench"
    error_file.write_text("pending_activating\\n", encoding="utf-8")
    if not FreeCADGui.activateWorkbench(expected):
        raise RuntimeError("VibeCAD Workbench activation failed")
    if FreeCADGui.activeWorkbench().name() != expected:
        raise RuntimeError("VibeCAD Workbench is not active")
    error_file.write_text("pending_activated\\n", encoding="utf-8")
    ready_file = Path(os.environ["VIBECAD_FREECAD_READY_FILE"])
    deadline = time.monotonic() + 45

    def observe():
        try:
            from vibecad_workbench import host

            snapshot = host.workbench_snapshot()
            worker = int(isinstance(snapshot.get("worker_thread_id"), int))
            clients = snapshot.get("client_construction_count")
            heartbeat = snapshot.get("heartbeat_count")
            error_file.write_text(
                "pending_"
                + str(snapshot.get("lifecycle"))
                + "_w"
                + str(worker)
                + "_c"
                + str(clients if isinstance(clients, int) else -1)
                + "_h"
                + str(heartbeat if isinstance(heartbeat, int) else -1)
                + "\\n",
                encoding="utf-8",
            )
            if (
                snapshot.get("lifecycle") == "active"
                and snapshot.get("dock_count") == 1
                and isinstance(snapshot.get("daemon_id"), str)
            ):
                ready_file.write_text(expected + "\\n", encoding="utf-8")
                error_file.unlink(missing_ok=True)
                return
            if (
                snapshot.get("lifecycle") in {"inactive", "stopping"}
                or time.monotonic() >= deadline
            ):
                error_file.write_text("daemon_unavailable\\n", encoding="utf-8")
                return
            schedule_observation()
        except BaseException as error:
            error_file.write_text(type(error).__name__ + "\\n", encoding="utf-8")

    def schedule_observation():
        timer = threading.Timer(0.1, observe)
        timer.daemon = True
        timer.start()

    schedule_observation()
except BaseException as error:
    error_file.write_text(type(error).__name__ + "\\n", encoding="utf-8")
    raise
"""


class FreeCADLaunchError(RuntimeError):
    """A fail-closed managed FreeCAD launch error."""


def _packaged_addon_root() -> Path:
    return Path(__file__).resolve().parent / "_freecad" / "VibeCAD"


def _require_packaged_addon() -> Path:
    root = _packaged_addon_root()
    try:
        if root != root.resolve(strict=True) or not stat.S_ISDIR(root.lstat().st_mode):
            raise OSError
        for relative in _ADDON_FILES:
            source = root / relative
            info = source.lstat()
            if source != source.resolve(strict=True) or not stat.S_ISREG(info.st_mode):
                raise OSError
            if sys.platform == "win32":
                descriptor, capability = open_windows_external_file(source)
                try:
                    opened = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or _wheel_file_binding(opened) != _wheel_file_binding(info)
                        or capture_windows_external_fd(
                            descriptor,
                            generation_token=capability.generation_token,
                        )
                        != capability
                        or validate_windows_external_file(capability) != source
                    ):
                        raise OSError
                finally:
                    os.close(descriptor)
            elif stat.S_IMODE(info.st_mode) & 0o022:
                raise OSError
    except OSError:
        raise FreeCADLaunchError("the installed VibeCAD FreeCAD addon is incomplete") from None
    return root


def _progress(value: status.RuntimeStatus) -> None:
    message = value.message.strip() if type(value.message) is str else ""
    if message:
        print(f"VibeCAD runtime: {message}", file=sys.stderr, flush=True)


def _wheel_file_binding(value: os.stat_result) -> tuple[int, ...]:
    """Return stable mutation fields while deliberately excluding access time.

    On Windows, a path stat and a CRT-descriptor stat can expose different
    ``st_ctime`` meanings (NTFS birth time versus change time).  Birth time is
    stable across those two views; the native File ID capability remains the
    identity authority.
    """

    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        (int(value.st_birthtime_ns) if sys.platform == "win32" else value.st_ctime_ns),
    )


def _local_distribution_wheel() -> tuple[Path, str] | None:
    """Return the immutable wheel behind a direct local installation, if any."""

    try:
        direct_text = importlib.metadata.distribution("vibecad").read_text("direct_url.json")
        if direct_text is None:
            return None
        direct = json.loads(direct_text)
        url = direct.get("url") if type(direct) is dict else None
        parsed = urllib.parse.urlsplit(url) if type(url) is str else None
        if (
            parsed is None
            or parsed.scheme != "file"
            or parsed.netloc not in {"", "localhost"}
            or parsed.query
            or parsed.fragment
        ):
            raise OSError
        wheel = Path(urllib.request.url2pathname(parsed.path))
        if not wheel.is_absolute():
            raise OSError
        canonical = wheel.resolve(strict=True)
        info = wheel.lstat()
        if canonical != wheel or not stat.S_ISREG(info.st_mode):
            raise OSError
        digest = hashlib.sha256()
        if sys.platform == "win32":
            descriptor, capability = open_windows_external_file(wheel)
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or _wheel_file_binding(before) != _wheel_file_binding(info)
                    or capture_windows_external_fd(
                        descriptor,
                        generation_token=capability.generation_token,
                    )
                    != capability
                ):
                    raise OSError
                while chunk := os.read(descriptor, 1 << 20):
                    digest.update(chunk)
                after = os.fstat(descriptor)
                if (
                    _wheel_file_binding(after) != _wheel_file_binding(before)
                    or capture_windows_external_fd(
                        descriptor,
                        generation_token=capability.generation_token,
                    )
                    != capability
                    or validate_windows_external_file(capability) != wheel
                ):
                    raise OSError
            finally:
                os.close(descriptor)
        else:
            with wheel.open("rb") as stream:
                while chunk := stream.read(1 << 20):
                    digest.update(chunk)
            if _wheel_file_binding(wheel.lstat()) != _wheel_file_binding(info):
                raise OSError
        return wheel, digest.hexdigest()
    except (ImportError, json.JSONDecodeError, OSError, TypeError, UnicodeError, ValueError):
        raise FreeCADLaunchError(
            "the local VibeCAD installation wheel is unavailable; reinstall from a wheel"
        ) from None


def _require_managed_runtime() -> tuple[Path, status.RuntimeGenerationEvidence, Path]:
    if paths.user_override_env() is not None:
        raise FreeCADLaunchError("VIBECAD_FREECAD_ENV is not accepted by --freecad")

    installer = RuntimeInstaller(on_progress=_progress)
    if not installer.is_ready():
        installer.install()
    local_wheel = _local_distribution_wheel()
    if local_wheel is not None:
        wheel, wheel_sha256 = local_wheel
        installer.refresh_server_package(
            wheel,
            expected_sha256=wheel_sha256,
        )

    try:
        prefix = paths.env_prefix()
        selected = paths.active_runtime_prefix()
        canonical = prefix.resolve(strict=True)
        if prefix != canonical or selected != prefix:
            raise OSError
        if status.read_prefix_receipt(prefix) != spec.expected_receipt():
            raise OSError
        evidence = status.capture_runtime_generation_evidence(prefix)
        if evidence.prefix != prefix or status.verify_runtime_generation(evidence) is not True:
            raise OSError
        if status.capture_runtime_generation_evidence(prefix) != evidence:
            raise OSError
    except (OSError, ValueError):
        raise FreeCADLaunchError("the canonical managed FreeCAD runtime is not ready") from None

    binary = paths.freecad_path()
    try:
        info = binary.lstat()
        target = binary.resolve(strict=True)
        target.relative_to(prefix)
        target_info = target.lstat()
        if not binary.is_absolute() or not stat.S_ISREG(info.st_mode):
            raise OSError
        if sys.platform == "win32":
            if binary != target or not stat.S_ISREG(target_info.st_mode):
                raise OSError
            descriptor, capability = open_windows_external_file(binary)
            try:
                if (
                    not stat.S_ISREG(os.fstat(descriptor).st_mode)
                    or capture_windows_external_fd(
                        descriptor,
                        generation_token=capability.generation_token,
                    )
                    != capability
                    or validate_windows_external_file(capability) != binary
                ):
                    raise OSError
            finally:
                os.close(descriptor)
        elif (
            not stat.S_ISREG(target_info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o022
            or stat.S_IMODE(target_info.st_mode) & 0o022
            or not os.access(target, os.X_OK)
        ):
            raise OSError
    except (OSError, ValueError):
        raise FreeCADLaunchError("the managed FreeCAD GUI executable is unavailable") from None
    return prefix, evidence, binary


def _prepare_private_profile(root: Path) -> tuple[Path, Path, Path, Path]:
    children = tuple(root / name for name in ("home", "data", "temp", "tmp"))
    if sys.platform == "win32":
        try:
            if root != root.resolve(strict=True) or not stat.S_ISDIR(root.lstat().st_mode):
                raise OSError
            set_private_dacl(root)
            root_capability = capture_windows_path(root, directory=True)
            child_capabilities = tuple(
                ensure_private_directory(
                    child,
                    expected_parent=root_capability,
                    exclusive=True,
                )
                for child in children
            )
            config = children[0] / "user.cfg"
            descriptor, config_capability = open_private_file(
                config,
                exclusive=True,
                expected_parent=child_capabilities[0],
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_USER_CONFIG.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            if (
                validate_windows_path(root_capability, directory=True) != root
                or any(
                    validate_windows_path(capability, directory=True) != child
                    for capability, child in zip(child_capabilities, children, strict=True)
                )
                or validate_windows_path(config_capability, directory=False) != config
            ):
                raise OSError
        except (OSError, TypeError, ValueError):
            raise FreeCADLaunchError("the private FreeCAD profile is unavailable") from None
        return children

    if root != root.resolve(strict=True) or stat.S_IMODE(root.lstat().st_mode) != 0o700:
        raise FreeCADLaunchError("the private FreeCAD session root is unsafe")
    try:
        for child in children:
            child.mkdir(mode=0o700)
            if child != child.resolve(strict=True) or stat.S_IMODE(child.lstat().st_mode) != 0o700:
                raise OSError
        (children[0] / "user.cfg").write_text(_USER_CONFIG, encoding="utf-8")
    except OSError:
        raise FreeCADLaunchError("the private FreeCAD profile is unavailable") from None
    return children


def _write_activation_script(root: Path) -> Path:
    script = root / "activate_vibecad.py"
    if sys.platform == "win32":
        try:
            root_capability = capture_windows_path(root, directory=True)
            descriptor, script_capability = open_private_file(
                script,
                exclusive=True,
                expected_parent=root_capability,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_ACTIVATION_SCRIPT.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            if validate_windows_path(script_capability, directory=False) != script:
                raise OSError
        except (OSError, TypeError, ValueError):
            raise FreeCADLaunchError("the VibeCAD activation script is unavailable") from None
        return script

    try:
        script.write_text(_ACTIVATION_SCRIPT, encoding="utf-8")
        script.chmod(0o600)
        if script != script.resolve(strict=True) or not stat.S_ISREG(script.lstat().st_mode):
            raise OSError
    except OSError:
        raise FreeCADLaunchError("the VibeCAD activation script is unavailable") from None
    return script


def _child_environment(
    profile: tuple[Path, Path, Path, Path],
    ready_file: Path,
    error_file: Path,
    managed_prefix: Path,
) -> dict[str, str]:
    home, data, freecad_temp, process_temp = profile
    environment = dict(os.environ)
    for name in _ENVIRONMENT_INJECTION:
        environment.pop(name, None)
    environment.update(
        {
            "FREECAD_USER_HOME": str(home),
            "FREECAD_USER_DATA": str(data),
            "FREECAD_USER_TEMP": str(freecad_temp),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "TMPDIR": str(process_temp),
            "VIBECAD_FREECAD_ERROR_FILE": str(error_file),
            "VIBECAD_FREECAD_READY_FILE": str(ready_file),
        }
    )
    environment.setdefault("VIBECAD_HOME", str(paths.vibecad_home()))
    if sys.platform == "win32":
        # FreeCAD.exe is linked against DLLs shipped beside the managed
        # conda environment.  Launching it by absolute path does not perform
        # conda activation, so Windows cannot otherwise resolve Qt, OCCT, and
        # the other runtime DLLs.  Keep the user's remaining PATH entries,
        # but put the exact reviewed prefix first and keep all GUI scratch
        # files inside this session's private directory.
        environment = activate_windows_runtime_environment(environment, managed_prefix)
        environment["TEMP"] = str(process_temp)
        environment["TMP"] = str(process_temp)
    return environment


def _forward_signal(process: subprocess.Popen[bytes], signum: int) -> bool:
    if process.poll() is not None:
        return True
    try:
        if os.name == "posix":
            os.killpg(process.pid, signum)
        else:  # pragma: no cover - exercised on Windows
            process.send_signal(signum)
    except OSError:
        return process.poll() is not None
    return True


def _wait_for_process(process: subprocess.Popen[bytes]) -> int:
    previous: dict[int, object] = {}
    received_signal: int | None = None

    def forward(signum: int, _frame: object) -> None:
        nonlocal received_signal
        if received_signal is None:
            received_signal = signum
        child_signal = signal.SIGTERM if signum == signal.SIGINT else signum
        _forward_signal(process, child_signal)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, forward)
        returncode = process.wait()
        return -received_signal if received_signal is not None else returncode
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if not _forward_signal(process, signal.SIGTERM):
        process.terminate()
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    # Windows exposes no SIGKILL.  Popen.kill() is the native process-handle
    # termination primitive there; looking up signal.SIGKILL would itself
    # raise AttributeError on the GUI failure path we are trying to contain.
    kill_signal = getattr(signal, "SIGKILL", None)
    if kill_signal is None or not _forward_signal(process, kill_signal):
        process.kill()
    process.wait(timeout=5)


def _wait_for_ready(
    process: subprocess.Popen[bytes],
    ready_file: Path,
    error_file: Path,
) -> None:
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline:
        try:
            if (
                ready_file.read_text(encoding="utf-8") == "VibeCADWorkbench\n"
                and not ready_file.is_symlink()
                and stat.S_ISREG(ready_file.lstat().st_mode)
            ):
                return
        except (FileNotFoundError, OSError, UnicodeError):
            pass
        if error_file.exists():
            try:
                detail = error_file.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                detail = ""
            valid_detail = (
                bool(detail)
                and len(detail) <= 64
                and all(
                    character.isascii() and (character.isalnum() or character == "_")
                    for character in detail
                )
            )
            if valid_detail and not detail.startswith("pending_"):
                raise FreeCADLaunchError(
                    f"VibeCAD Workbench could not connect to the local daemon ({detail})"
                )
        if process.poll() is not None:
            raise FreeCADLaunchError("FreeCAD exited before VibeCAD Workbench activation")
        time.sleep(0.05)
    try:
        detail = error_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        detail = "unknown_stage"
    raise FreeCADLaunchError(f"VibeCAD Workbench activation timed out ({detail})")


def _report_log_tail(log_file: Path) -> None:
    try:
        tail = log_file.read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        return
    if tail:
        print("FreeCAD startup log tail:\n" + tail, file=sys.stderr)


def _exit_code(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def launch() -> int:
    """Launch one GUI child and return its shell-compatible exit status."""

    try:
        addon = _require_packaged_addon()
        prefix, evidence, binary = _require_managed_runtime()
        with tempfile.TemporaryDirectory(prefix="vibecad-freecad-") as temporary:
            root = Path(temporary).resolve(strict=True)
            profile = _prepare_private_profile(root)
            activation_script = _write_activation_script(root)
            ready_file = root / "workbench.ready"
            error_file = root / "workbench.error"
            user_config = profile[0] / "user.cfg"
            log_file = profile[3] / "FreeCAD.log"
            process = subprocess.Popen(
                [
                    str(binary),
                    "-u",
                    str(user_config),
                    "--log-file",
                    str(log_file),
                    "-M",
                    str(addon),
                    str(activation_script),
                ],
                env=_child_environment(profile, ready_file, error_file, prefix),
                start_new_session=True,
            )
            try:
                _wait_for_ready(process, ready_file, error_file)
                returncode = _wait_for_process(process)
            except BaseException:
                _report_log_tail(log_file)
                with contextlib.suppress(Exception):
                    _terminate_and_reap(process)
                raise
            if (
                status.capture_runtime_generation_evidence(prefix) != evidence
                or status.verify_runtime_generation(evidence) is not True
            ):
                raise FreeCADLaunchError("the managed runtime changed during the GUI session")
        return _exit_code(returncode)
    except (FreeCADLaunchError, OSError, RuntimeError, ValueError) as error:
        print(f"vibecad --freecad: {error}", file=sys.stderr)
        return 1
