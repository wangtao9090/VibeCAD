import ctypes
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vibecad.runtime import windows_job_runner as runner


class _FakePipe:
    def close(self):
        return None


class _FakeGate:
    def __init__(self, events):
        self._handle = 456
        self.stdin = _FakePipe()
        self.stdout = _FakePipe()
        self.returncode = 0
        self._events = events

    def communicate(self, request, timeout=None):
        self._events.append(("dispatch", request, timeout))
        self.stdin = None
        return b"guarded output", None

    def poll(self):
        return self.returncode

    def kill(self):
        self._events.append(("kill",))

    def wait(self, timeout=None):
        self._events.append(("wait", timeout))
        return self.returncode


def test_assignment_precedes_guard_and_private_request_dispatch(monkeypatch):
    events = []
    spawned = {}
    gate = _FakeGate(events)
    monkeypatch.setattr(runner.sys, "platform", "win32")
    monkeypatch.setattr(runner, "_create_kill_on_close_job", lambda: 123)
    monkeypatch.setattr(runner, "_close_handle", lambda handle: events.append(("close", handle)))
    monkeypatch.setattr(
        runner,
        "_assign_process_to_job",
        lambda job, process: events.append(("assign", job, process)),
    )
    def fake_popen(command, **kwargs):
        spawned["command"] = command
        spawned["environment"] = kwargs["env"]
        return gate

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)

    completed = runner.run_in_job(
        ["tool.exe", "argument"],
        environment={"SystemRoot": "C:\\Windows", "PRIVATE_VALUE": "secret"},
        before_dispatch=lambda: events.append(("guard",)),
    )

    assert completed.returncode == 0
    assert completed.stdout == "guarded output"
    assert [event[0] for event in events[:3]] == ["assign", "guard", "dispatch"]
    request = json.loads(events[2][1])
    assert request["command"] == ["tool.exe", "argument"]
    assert request["environment"]["PRIVATE_VALUE"] == "secret"
    assert all("secret" not in argument for argument in spawned["command"])
    assert spawned["command"][0] == runner.os.fspath(runner.sys._base_executable)
    assert "PRIVATE_VALUE" not in spawned["environment"]
    assert events[-1] == ("close", 123)


def test_assignment_failure_sends_no_request_and_fails_closed(monkeypatch):
    events = []
    gate = _FakeGate(events)
    monkeypatch.setattr(runner.sys, "platform", "win32")
    monkeypatch.setattr(runner, "_create_kill_on_close_job", lambda: 123)
    monkeypatch.setattr(runner, "_close_handle", lambda handle: events.append(("close", handle)))
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: gate)
    monkeypatch.setattr(
        runner,
        "_assign_process_to_job",
        lambda *_args: (_ for _ in ()).throw(runner.WindowsJobError("denied")),
    )

    with pytest.raises(runner.WindowsJobError, match="denied"):
        runner.run_in_job(
            ["must-not-start.exe"],
            environment={"SystemRoot": "C:\\Windows"},
            before_dispatch=lambda: events.append(("guard",)),
        )

    assert not any(event[0] in {"guard", "dispatch"} for event in events)
    assert any(event[0] == "wait" for event in events)


def test_gate_request_validation_rejects_invalid_environment():
    raw = json.dumps(
        {
            "schema": 1,
            "command": ["tool.exe"],
            "cwd": None,
            "environment": {"BAD=NAME": "value"},
        }
    ).encode("utf-8") + b"\n"

    with pytest.raises(runner.WindowsJobError, match="environment"):
        runner._validated_gate_request(raw)


def test_gate_refuses_to_fall_back_to_virtualenv_redirector(monkeypatch):
    monkeypatch.setattr(runner.sys, "prefix", "C:\\isolated-venv")
    monkeypatch.setattr(runner.sys, "base_prefix", "C:\\base-python")
    monkeypatch.setattr(runner.sys, "_base_executable", runner.sys.executable)

    with pytest.raises(runner.WindowsJobError, match="redirector"):
        runner._gate_command()


def test_gate_fails_closed_without_base_executable(monkeypatch):
    monkeypatch.setattr(runner.sys, "_base_executable", None)

    with pytest.raises(runner.WindowsJobError, match="unavailable"):
        runner._gate_command()


@pytest.mark.windows_contract
@pytest.mark.skipif(sys.platform != "win32", reason="native Windows Job Object contract")
def test_native_job_gate_forwards_combined_output_and_exit_code():
    completed = runner.run_in_job(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sys; print('stdout-line'); print('stderr-line', file=sys.stderr); sys.exit(7)",
        ],
        environment=dict(runner.os.environ),
    )

    assert completed.returncode == 7
    assert "stdout-line" in completed.stdout
    assert "stderr-line" in completed.stdout


@pytest.mark.windows_contract
@pytest.mark.skipif(sys.platform != "win32", reason="native Windows Job Object contract")
def test_persistent_job_inherits_only_declared_socket_and_terminates_tree():
    parent, child = socket.socketpair()
    source = """
import socket, sys, time
connection = socket.socket(fileno=int(sys.argv[1]))
connection.set_inheritable(False)
assert connection.recv(4) == b'ping'
connection.sendall(b'pong')
while True:
    time.sleep(0.05)
"""
    process = None
    try:
        process = runner.spawn_persistent_in_job(
            [sys.executable, "-I", "-B", "-c", source, str(child.fileno())],
            environment=dict(runner.os.environ),
            socket_handles=(child.fileno(),),
        )
        child.close()
        parent.settimeout(5)
        parent.sendall(b"ping")
        assert parent.recv(4) == b"pong"
        assert process.launch_primitive == "windows_job"
        assert _process_is_active(process.pid)
        process.terminate_tree()
        assert process.poll() is not None
        assert not _process_is_active(process.pid)
    finally:
        parent.close()
        child.close()
        if process is not None:
            process.terminate_tree()


@pytest.mark.windows_contract
@pytest.mark.skipif(sys.platform != "win32", reason="native Windows Job Object contract")
def test_native_job_gate_timeout_terminates_descendant(tmp_path):
    marker = tmp_path / "timeout-writer.pid"
    source = """
import os, pathlib, sys, time
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
while True:
    time.sleep(0.05)
"""
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run_in_job(
            [sys.executable, "-I", "-B", "-c", source, str(marker)],
            environment=dict(runner.os.environ),
            timeout=0.5,
        )

    writer_pid = int(marker.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 10
    while _process_is_active(writer_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _process_is_active(writer_pid)


def _process_is_active(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and (
            exit_code.value == 259  # STILL_ACTIVE
        )
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.windows_contract
@pytest.mark.skipif(sys.platform != "win32", reason="native Windows Job Object contract")
def test_hard_parent_exit_terminates_gate_and_descendant_writer(tmp_path):
    marker = tmp_path / "job-descendant.log"
    module_file = Path(runner.__file__).resolve()
    writer_source = """
import os, sys, time
path = sys.argv[1]
with open(path, "w", encoding="utf-8", buffering=1) as stream:
    stream.write(str(os.getpid()) + "\\n")
    stream.flush()
    os.fsync(stream.fileno())
    while True:
        stream.write("tick-" + ("x" * 256) + "\\n")
        stream.flush()
        os.fsync(stream.fileno())
        time.sleep(0.03)
"""
    parent_source = """
import importlib.util, os, sys, threading, time
spec = importlib.util.spec_from_file_location("vibecad_windows_job_runner", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
marker = sys.argv[2]
writer = sys.argv[3]
def hard_exit_after_writer_starts():
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if os.path.getsize(marker) > 0:
                time.sleep(0.15)
                os._exit(73)
        except OSError:
            pass
        time.sleep(0.02)
    os._exit(74)
threading.Thread(target=hard_exit_after_writer_starts, daemon=True).start()
module.run_in_job(
    [sys.executable, "-I", "-B", "-c", writer, marker],
    environment=dict(os.environ),
)
os._exit(75)
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            parent_source,
            str(module_file),
            str(marker),
            writer_source,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 73, completed.stdout.decode("utf-8", "replace")
    first_line = marker.read_text(encoding="utf-8").splitlines()[0]
    writer_pid = int(first_line)
    deadline = time.monotonic() + 10
    while _process_is_active(writer_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _process_is_active(writer_pid)
    size_after_exit = marker.stat().st_size
    time.sleep(0.5)
    assert marker.stat().st_size == size_after_exit


@pytest.mark.windows_contract
@pytest.mark.skipif(sys.platform != "win32", reason="native Windows crash cleanup contract")
def test_hard_parent_exit_kills_cache_writer_before_helper_converges(tmp_path):
    home = tmp_path / "home"
    environment = dict(runner.os.environ)
    environment["VIBECAD_HOME"] = str(home)
    writer_source = """
import ctypes, os, pathlib, sys, time
locked = sys.argv[1]
ready = pathlib.Path(sys.argv[2])
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
create_file = kernel32.CreateFileW
create_file.argtypes = (
    ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
)
create_file.restype = ctypes.c_void_p
write_file = kernel32.WriteFile
write_file.argtypes = (
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
)
write_file.restype = ctypes.c_int
flush_file = kernel32.FlushFileBuffers
flush_file.argtypes = (ctypes.c_void_p,)
flush_file.restype = ctypes.c_int
handle = create_file(locked, 0x40000000, 0, None, 2, 0x80, None)
assert handle not in (None, ctypes.c_void_p(-1).value), ctypes.get_last_error()
block = ctypes.create_string_buffer(b"job-writer-tick\\n")
count = 0
while True:
    written = ctypes.c_uint32()
    assert write_file(handle, block, len(block.raw), ctypes.byref(written), None)
    assert flush_file(handle)
    count += 1
    ready.write_text(f"{os.getpid()}:{count}", encoding="utf-8")
    time.sleep(0.02)
"""
    parent_source = """
import os, pathlib, sys, threading, time
from vibecad.runtime.windows_job_runner import run_in_job
from vibecad.runtime.windows_package_cache import package_cache_session
manager = package_cache_session()
session = manager.__enter__()
locked = session.root / "job-writer.locked"
ready = session.root / "job-writer.ready"
print("CACHE_ROOT=" + str(session.root), flush=True)
print("CACHE_RECORD=" + str(session._record_path), flush=True)
print("HELPER_PID=" + str(session._receipt["helper_pid"]), flush=True)
def hard_exit_after_writer_starts():
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            writer_pid, tick_count = ready.read_text(encoding="utf-8").strip().split(":", 1)
            if writer_pid and int(tick_count) >= 3:
                print("WRITER_PID=" + writer_pid, flush=True)
                os._exit(79)
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    os._exit(80)
threading.Thread(target=hard_exit_after_writer_starts, daemon=True).start()
run_in_job(
    [sys.executable, "-I", "-B", "-c", sys.argv[1], str(locked), str(ready)],
    environment=session.child_environment(),
)
os._exit(81)
"""

    completed = subprocess.run(
        [sys.executable, "-B", "-c", parent_source, writer_source],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=40,
        check=False,
    )

    assert completed.returncode == 79, completed.stdout
    evidence = {
        key: value
        for line in completed.stdout.splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }
    cache_root = Path(evidence["CACHE_ROOT"])
    cache_record = Path(evidence["CACHE_RECORD"])
    writer_pid = int(evidence["WRITER_PID"])
    helper_pid = int(evidence["HELPER_PID"])

    deadline = time.monotonic() + 40
    while (
        cache_root.exists()
        or cache_record.exists()
        or _process_is_active(writer_pid)
        or _process_is_active(helper_pid)
    ) and time.monotonic() < deadline:
        time.sleep(0.05)

    assert not _process_is_active(writer_pid)
    assert not _process_is_active(helper_pid)
    assert not cache_root.exists()
    assert not cache_record.exists()
    assert cache_record == home / ".package-cache-session.json"
