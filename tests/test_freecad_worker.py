"""Private FreeCAD Worker generation, protocol, and real-engine tests."""

from __future__ import annotations

import array
import contextlib
import json
import os
import pickle
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import vibecad.execution.revisions as revisions_module
from vibecad.execution.revisions import (
    LocalRevisionStore,
    ProjectHead,
    RevisionStoreRootTrust,
)
from vibecad.worker import (
    FreeCadWorker,
    WorkerError,
    WorkerErrorCode,
    WorkerGenerationState,
)
from vibecad.worker.codec import (
    MAX_WORKER_REQUEST_BYTES,
    MAX_WORKER_RESPONSE_BYTES,
    WorkerCodecError,
    decode_worker_request,
    decode_worker_response,
    encode_worker_request,
    encode_worker_response,
)
from vibecad.worker.generation import _SpawnedProcess, _WorkerProcess
from vibecad.worker.service import WorkerService, _recv_header_with_descriptors
from vibecad.workflow.contracts import (
    AcceptanceCriterion,
    AcceptanceKind,
    AcceptanceSpec,
    ModelCommand,
    ModelProgram,
    ValueSource,
)
from vibecad.workflow.lease import (
    LeaseRootTrust,
    ProjectWriteLease,
    ResourceLeaseManager,
)

_PROJECT_ID = "project_" + "1" * 32
_BASE_REVISION = "revision_" + "2" * 32
_CANDIDATE_REVISION = "revision_" + "3" * 32
_MANIFEST = "4" * 64
_TASK_ID = "task_" + "5" * 32
_GENERATION = "worker_generation_" + "6" * 32
_REQUEST = "worker_request_" + "7" * 32


def _head() -> ProjectHead:
    return ProjectHead(
        project_id=_PROJECT_ID,
        generation=0,
        revision_id=_BASE_REVISION,
        manifest_sha256=_MANIFEST,
    )


def _acceptance() -> AcceptanceSpec:
    return AcceptanceSpec(
        id="acceptance-worker-smoke",
        criteria=(
            AcceptanceCriterion(
                id="criterion-volume",
                kind=AcceptanceKind.GEOMETRY,
                check="volume",
                target="body",
                expected=0,
                tolerance=None,
                parameters={"unit": "mm^3"},
            ),
        ),
    )


def _program(*, base_revision: str = _BASE_REVISION) -> ModelProgram:
    return ModelProgram(
        task_id=_TASK_ID,
        base_revision=base_revision,
        operations=(
            ModelCommand(
                id="box",
                op="create_box",
                target={},
                args={
                    "length_mm": 10,
                    "width_mm": 20,
                    "height_mm": 30,
                    "position_mm": (0, 0, 0),
                },
                depends_on=(),
                preserve=(),
                source=ValueSource.MODEL,
            ),
            ModelCommand(
                id="modify",
                op="modify_parameter",
                target={"object": {"command_id": "box", "slot": "object"}},
                args={"parameter": "length", "value_mm": 15},
                depends_on=("box",),
                preserve=(),
                source=ValueSource.MODEL,
            ),
            ModelCommand(
                id="inspect",
                op="inspect_model",
                target={},
                args={},
                depends_on=("modify",),
                preserve=(),
                source=ValueSource.MODEL,
            ),
        ),
        acceptance=_acceptance(),
    )


def _inspect_program(*, base_revision: str = _BASE_REVISION) -> ModelProgram:
    return ModelProgram(
        task_id=_TASK_ID,
        base_revision=base_revision,
        operations=(
            ModelCommand(
                id="inspect",
                op="inspect_model",
                target={},
                args={},
                depends_on=(),
                preserve=(),
                source=ValueSource.MODEL,
            ),
        ),
        acceptance=_acceptance(),
    )


def _candidate_directory_at(candidate: Path) -> Path:
    candidate.mkdir(mode=0o700)
    candidate.chmod(0o700)
    for name in ("model.FCStd", "model.step"):
        path = candidate / name
        path.touch(mode=0o600)
        path.chmod(0o600)
    return candidate


@dataclass(slots=True)
class _CandidateRig:
    manager: ResourceLeaseManager
    store: LocalRevisionStore
    lease: ProjectWriteLease
    head: ProjectHead
    revision_id: str
    directory: Path


@contextmanager
def _candidate_rig(root: Path, *, suffix: str = "store"):
    revisions_module._initialize_candidate_file_limit_runtime()
    base = root / suffix
    locks = base / "locks"
    revisions = base / "revisions"
    locks.mkdir(parents=True, mode=0o700)
    revisions.mkdir(parents=True, mode=0o700)
    locks.chmod(0o700)
    revisions.chmod(0o700)
    manager = ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL)
    store = LocalRevisionStore(
        revisions,
        manager,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    lease = manager.acquire_project_write(_PROJECT_ID)
    try:
        head = store.initialize_empty_project(_PROJECT_ID, lease)
        revision_id = store.begin_revision(_PROJECT_ID, head, lease)
        directory = store.candidate_model_path(
            _PROJECT_ID,
            revision_id,
            lease,
        ).parent
        yield _CandidateRig(
            manager=manager,
            store=store,
            lease=lease,
            head=head,
            revision_id=revision_id,
            directory=directory,
        )
    finally:
        if not lease.released:
            lease.release(owner_token=lease.owner_token)


def _fake_worker_script(root: Path, mode: str) -> tuple[Path, Path]:
    script = root / f"fake_worker_{mode}.py"
    grandchild = root / f"grandchild_{mode}.pid"
    script.write_text(
        textwrap.dedent(
            f"""
            import array
            import hashlib
            import json
            import os
            import signal
            import socket
            import stat
            import struct
            import subprocess
            import sys
            import time

            fd = int(sys.argv[sys.argv.index("--protocol-fd") + 1])
            generation = sys.argv[sys.argv.index("--generation-id") + 1]
            sock = socket.socket(fileno=fd)

            def read_exact(size):
                chunks = []
                while sum(map(len, chunks)) < size:
                    chunk = sock.recv(size - sum(map(len, chunks)))
                    if not chunk:
                        raise SystemExit(2)
                    chunks.append(chunk)
                return b"".join(chunks)

            def read_frame():
                header = bytearray()
                descriptors = []
                ancillary_size = socket.CMSG_SPACE(array.array("i", range(4)).itemsize * 4)
                while len(header) < 4:
                    fragment, ancillary, flags, _address = sock.recvmsg(
                        4 - len(header), ancillary_size
                    )
                    if not fragment or flags:
                        raise SystemExit(2)
                    header.extend(fragment)
                    for level, kind, data in ancillary:
                        if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                            raise SystemExit(2)
                        received = array.array("i")
                        received.frombytes(data[: len(data) - len(data) % received.itemsize])
                        descriptors.extend(received)
                size = struct.unpack(">I", header)[0]
                return json.loads(read_exact(size)), descriptors

            def write_candidate(name, value, candidate_fd):
                target = os.open(
                    name,
                    os.O_WRONLY | os.O_TRUNC,
                    dir_fd=candidate_fd,
                )
                try:
                    os.write(target, value)
                finally:
                    os.close(target)
                return hashlib.sha256(value).hexdigest()

            def replace_candidate(name, value, candidate_fd):
                temporary = ".worker-replacement"
                target = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=candidate_fd,
                )
                try:
                    os.write(target, value)
                finally:
                    os.close(target)
                os.rename(
                    temporary,
                    name,
                    src_dir_fd=candidate_fd,
                    dst_dir_fd=candidate_fd,
                )
                return hashlib.sha256(value).hexdigest()

            def send(value):
                raw = json.dumps(
                    value, allow_nan=False, separators=(",", ":"), sort_keys=True
                ).encode()
                sock.sendall(struct.pack(">I", len(raw)) + raw)

            request, descriptors = read_frame()
            if descriptors:
                raise SystemExit(2)
            leaked_fds = set()
            for boundary_fd in (200, 400):
                try:
                    os.fstat(boundary_fd)
                except OSError:
                    continue
                leaked_fds.add(boundary_fd)
            open_fds = set()
            for candidate_fd_number in range(512):
                try:
                    os.fstat(candidate_fd_number)
                except OSError:
                    continue
                open_fds.add(candidate_fd_number)
            freecad_directories = tuple(
                os.environ.get(name, "")
                for name in (
                    "FREECAD_USER_HOME",
                    "FREECAD_USER_DATA",
                    "FREECAD_USER_TEMP",
                )
            )
            home = os.environ.get("HOME", "")
            temp = os.environ.get("TMPDIR", "")
            home_real = os.path.realpath(home)
            clean = (
                "VIBECAD_TEST_SECRET" not in os.environ
                and not leaked_fds
                and os.getpid() == os.getsid(0) == os.getpgrp()
                and home
                and temp
                and os.path.samefile(os.getcwd(), home)
                and os.path.samefile(home, temp)
                and open_fds == {{0, 1, 2, fd}}
                and all(
                    value
                    and os.path.isdir(value)
                    and stat.S_IMODE(os.stat(value).st_mode) == 0o700
                    and os.path.commonpath(
                        (os.path.realpath(value), home_real)
                    )
                    == home_real
                    for value in freecad_directories
                )
            )
            mode = {mode!r}
            if mode.startswith("startup_"):
                with open({str(grandchild)!r}, "w", encoding="ascii") as handle:
                    handle.write(str(os.getpid()))
                    handle.flush()
                    os.fsync(handle.fileno())
                if mode == "startup_exit":
                    os._exit(17)
                if mode == "startup_corrupt":
                    sock.sendall(struct.pack(">I", 2) + b"{{}}")
                    while True:
                        time.sleep(60)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                while True:
                    time.sleep(60)
            send({{
                "generation_id": generation,
                "ok": True,
                "request_id": request["request_id"],
                "result": {{
                    "freecad_version": "1.1.0" if clean else "leaked",
                    "python_version": ".".join(map(str, sys.version_info[:3])),
                    "worker_pid": os.getpid(),
                }},
                "schema_version": 1,
            }})
            request, descriptors = read_frame()
            if mode in {{
                "proxy_idle",
                "hang_after_create",
                "close_error",
                "command_hang",
                "checkpoint_cross",
                "checkpoint_bad_claim",
                "export_cross",
                "export_replace",
            }}:
                candidate_fd = -1
                while True:
                    method = request["method"]
                    if method == "candidate.bind":
                        if len(descriptors) != 1 or candidate_fd >= 0:
                            raise SystemExit(2)
                        candidate_fd = descriptors[0]
                        result = {{"candidate_id": request["params"]["candidate_id"]}}
                    elif descriptors:
                        raise SystemExit(2)
                    elif method == "candidate.release":
                        os.close(candidate_fd)
                        candidate_fd = -1
                        result = {{"candidate_id": request["params"]["candidate_id"]}}
                    elif method == "session.create_empty":
                        result = {{"session_id": "worker_session_" + "8" * 32}}
                    elif method == "program.begin":
                        operations = request["params"]["program"]["operations"]
                        result = {{
                            "program_id": "worker_program_" + "9" * 32,
                            "command_ids": [item["id"] for item in operations],
                            "command_deadlines_ms": [
                                10000 if item["op"] == "inspect_model" else 30000
                                for item in operations
                            ],
                        }}
                    elif method == "program.execute_command" and mode == "command_hang":
                        while True:
                            time.sleep(60)
                    elif method == "session.checkpoint_fcstd" and mode == "checkpoint_cross":
                        model = b"checkpoint-model"
                        write_candidate("model.step", b"tampered-step", candidate_fd)
                        digest = write_candidate("model.FCStd", model, candidate_fd)
                        result = {{"sha256": digest, "size_bytes": len(model)}}
                    elif (
                        method == "session.checkpoint_fcstd"
                        and mode == "checkpoint_bad_claim"
                    ):
                        model = b"checkpoint-model"
                        write_candidate("model.FCStd", model, candidate_fd)
                        result = {{"sha256": "0" * 64, "size_bytes": len(model)}}
                    elif method == "session.export_step" and mode == "export_cross":
                        step = b"export-step"
                        write_candidate("model.FCStd", b"tampered-model", candidate_fd)
                        digest = write_candidate("model.step", step, candidate_fd)
                        result = {{"sha256": digest, "size_bytes": len(step)}}
                    elif method == "session.export_step" and mode == "export_replace":
                        step = b"export-step"
                        digest = replace_candidate("model.step", step, candidate_fd)
                        result = {{"sha256": digest, "size_bytes": len(step)}}
                    elif method == "session.close" and mode == "hang_after_create":
                        while True:
                            time.sleep(60)
                    elif method == "session.close" and mode == "close_error":
                        send({{
                            "error": {{"code": "cad_failure", "schema_version": 1}},
                            "generation_id": generation,
                            "ok": False,
                            "request_id": request["request_id"],
                            "schema_version": 1,
                        }})
                        request, descriptors = read_frame()
                        continue
                    elif method == "session.close":
                        result = {{"session_id": request["params"]["session_id"]}}
                    elif method == "worker.shutdown":
                        result = {{"closed": True}}
                    else:
                        result = {{}}
                    send({{
                        "generation_id": generation,
                        "ok": True,
                        "request_id": request["request_id"],
                        "result": result,
                        "schema_version": 1,
                    }})
                    if method == "worker.shutdown":
                        raise SystemExit(0)
                    request, descriptors = read_frame()
            for descriptor in descriptors:
                os.close(descriptor)
            if mode == "exit":
                os._exit(17)
            if mode in {{
                "hang",
                "term_leader_with_hung_child",
                "exit_leader_with_hung_child",
            }}:
                if mode == "hang":
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import signal,time; "
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        "time.sleep(60)",
                    ]
                )
                with open({str(grandchild)!r}, "w", encoding="ascii") as handle:
                    handle.write(str(child.pid))
                    handle.flush()
                    os.fsync(handle.fileno())
                if mode == "exit_leader_with_hung_child":
                    raise SystemExit(17)
                while True:
                    time.sleep(60)
            if mode == "wrong_generation":
                send({{
                    "generation_id": "worker_generation_" + "f" * 32,
                    "ok": True,
                    "request_id": request["request_id"],
                    "result": {{}},
                    "schema_version": 1,
                }})
            elif mode == "oversize":
                sock.sendall(struct.pack(">I", {MAX_WORKER_RESPONSE_BYTES + 1}))
            elif mode == "partial_header":
                sock.sendall(b"\\x00\\x00")
            elif mode == "partial_body":
                sock.sendall(struct.pack(">I", 128) + b"{{}}")
            elif mode == "noncanonical":
                raw = json.dumps({{
                    "generation_id": generation,
                    "ok": True,
                    "request_id": request["request_id"],
                    "result": {{}},
                    "schema_version": 1,
                }}).encode()
                sock.sendall(struct.pack(">I", len(raw)) + raw)
            elif mode == "trickle":
                raw = json.dumps(
                    {{
                        "generation_id": generation,
                        "ok": True,
                        "request_id": request["request_id"],
                        "result": {{}},
                        "schema_version": 1,
                    }},
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
                frame = struct.pack(">I", len(raw)) + raw
                for byte in frame:
                    sock.send(bytes((byte,)))
                    time.sleep(0.05)
            elif mode == "typed_error":
                send({{
                    "error": {{"code": "cad_failure", "schema_version": 1}},
                    "generation_id": generation,
                    "ok": False,
                    "request_id": request["request_id"],
                    "schema_version": 1,
                }})
            else:
                send({{
                    "generation_id": generation,
                    "ok": True,
                    "request_id": request["request_id"],
                    "result": {{}},
                    "schema_version": 1,
                }})
            """
        ),
        encoding="utf-8",
    )
    return script, grandchild


@contextmanager
def _spawn_boundary_probe():
    saved: dict[int, tuple[int, bool] | None] = {}
    for descriptor in (200, 400):
        try:
            saved[descriptor] = (
                os.dup(descriptor),
                os.get_inheritable(descriptor),
            )
        except OSError:
            saved[descriptor] = None
    prior_secret = os.environ.get("VIBECAD_TEST_SECRET")
    had_secret = "VIBECAD_TEST_SECRET" in os.environ
    for descriptor in (200, 400):
        leaked = os.open(__file__, os.O_RDONLY)
        if leaked in {200, 400}:
            replacement = os.dup(leaked)
            os.close(leaked)
            leaked = replacement
        os.dup2(leaked, descriptor, inheritable=True)
        os.close(leaked)
    os.environ["VIBECAD_TEST_SECRET"] = "must-not-cross-worker-boundary"
    try:
        yield
    finally:
        if had_secret:
            assert prior_secret is not None
            os.environ["VIBECAD_TEST_SECRET"] = prior_secret
        else:
            os.environ.pop("VIBECAD_TEST_SECRET", None)
        for descriptor, previous in saved.items():
            if previous is None:
                os.close(descriptor)
            else:
                saved_descriptor, original_inheritable = previous
                os.dup2(
                    saved_descriptor,
                    descriptor,
                    inheritable=original_inheritable,
                )
                os.close(saved_descriptor)


def _process(tmp_path: Path, mode: str) -> tuple[_WorkerProcess, Path]:
    script, grandchild = _fake_worker_script(tmp_path, mode)
    with _spawn_boundary_probe():
        process = _WorkerProcess.spawn_for_test(
            command=(sys.executable, str(script)),
            source_root=Path(__file__).parents[1] / "src",
            readiness_timeout_ms=5_000,
            shutdown_timeout_ms=250,
        )
    return process, grandchild


def _wait_gone(pid: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.01)
    return False


def test_worker_codec_is_canonical_bounded_and_exact() -> None:
    request = {
        "schema_version": 1,
        "generation_id": _GENERATION,
        "request_id": _REQUEST,
        "method": "worker.ready",
        "params": {},
    }
    raw = encode_worker_request(request)
    assert len(raw) <= MAX_WORKER_REQUEST_BYTES
    assert decode_worker_request(raw) == request

    with pytest.raises(WorkerCodecError):
        decode_worker_request(
            (
                '{"generation_id":"' + _GENERATION + '","method":"worker.ready","params":{},'
                '"request_id":"' + _REQUEST + '","schema_version":1,"schema_version":1}'
            ).encode()
        )
    with pytest.raises(WorkerCodecError):
        decode_worker_request(json.dumps(request).encode())
    with pytest.raises(WorkerCodecError):
        decode_worker_request(encode_worker_request({**request, "unexpected": True}))
    with pytest.raises(WorkerCodecError):
        decode_worker_request(b"\xff")
    with pytest.raises(WorkerCodecError):
        encode_worker_request(
            {
                **request,
                "params": {"unsafe_integer": 9_007_199_254_740_992},
            }
        )
    deep: object = None
    for _index in range(70):
        deep = [deep]
    with pytest.raises(WorkerCodecError):
        encode_worker_request({**request, "params": {"deep": deep}})


def test_worker_response_codec_rejects_wrong_generation_and_oversize() -> None:
    response = {
        "schema_version": 1,
        "generation_id": _GENERATION,
        "request_id": _REQUEST,
        "ok": True,
        "result": {},
    }
    raw = encode_worker_response(response)
    assert len(raw) <= MAX_WORKER_RESPONSE_BYTES
    assert (
        decode_worker_response(
            raw,
            expected_generation_id=_GENERATION,
            expected_request_id=_REQUEST,
        )
        == response
    )
    with pytest.raises(WorkerCodecError):
        decode_worker_response(
            raw,
            expected_generation_id="worker_generation_" + "f" * 32,
            expected_request_id=_REQUEST,
        )
    with pytest.raises(WorkerCodecError):
        decode_worker_response(
            b"{" + b" " * MAX_WORKER_RESPONSE_BYTES + b"}",
            expected_generation_id=_GENERATION,
            expected_request_id=_REQUEST,
        )
    deep: object = None
    for _index in range(70):
        deep = [deep]
    with pytest.raises(WorkerCodecError):
        encode_worker_response({**response, "result": {"deep": deep}})
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(WorkerCodecError):
        encode_worker_response({**response, "result": {"cyclic": cyclic}})
    with pytest.raises(WorkerCodecError):
        decode_worker_response(
            (
                '{"generation_id":"'
                + _GENERATION
                + '","ok":true,"request_id":"'
                + _REQUEST
                + '","result":{"value":"\\ud800"},"schema_version":1}'
            ).encode(),
            expected_generation_id=_GENERATION,
            expected_request_id=_REQUEST,
        )
    deeply_encoded = (
        (
            '{"generation_id":"'
            + _GENERATION
            + '","ok":true,"request_id":"'
            + _REQUEST
            + '","result":{"deep":'
        ).encode()
        + b"[" * 2_000
        + b"null"
        + b"]" * 2_000
        + b'},"schema_version":1}'
    )
    with pytest.raises(WorkerCodecError):
        decode_worker_response(
            deeply_encoded,
            expected_generation_id=_GENERATION,
            expected_request_id=_REQUEST,
        )


def test_worker_launch_uses_fresh_posix_spawn_without_parent_fork(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.worker.generation as generation_module

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("threaded parent fork path was used")

    monkeypatch.setattr(generation_module.os, "fork", forbidden)
    monkeypatch.setattr(generation_module.subprocess, "Popen", forbidden)
    process, _grandchild = _process(tmp_path, "typed_error")
    pid = process.pid
    home = process._home
    try:
        assert process.launch_primitive == "posix_spawn"
        assert process.pid == os.getsid(process.pid) == os.getpgid(process.pid)
    finally:
        process.terminate()
    assert _wait_gone(pid)
    assert not home.exists()


@pytest.mark.parametrize(
    "mode",
    ("startup_exit", "startup_corrupt", "startup_hang"),
)
def test_startup_failure_is_typed_and_reaps_generation(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.worker.generation as generation_module

    script, pid_file = _fake_worker_script(tmp_path, mode)
    original_mkdtemp = generation_module.tempfile.mkdtemp
    homes: list[Path] = []

    def recording_mkdtemp(*args: object, **kwargs: object) -> str:
        result = original_mkdtemp(*args, **kwargs)
        homes.append(Path(result))
        return result

    monkeypatch.setattr(generation_module.tempfile, "mkdtemp", recording_mkdtemp)
    with _spawn_boundary_probe():
        with pytest.raises(WorkerError) as caught:
            _WorkerProcess.spawn_for_test(
                command=(sys.executable, str(script)),
                source_root=Path(__file__).parents[1] / "src",
                readiness_timeout_ms=2_000,
                shutdown_timeout_ms=250,
            )
    assert caught.value.code is WorkerErrorCode.START_FAILED
    deadline = time.monotonic() + 1.0
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pid_file.exists()
    assert _wait_gone(int(pid_file.read_text(encoding="ascii")))
    assert len(homes) == 1
    assert not homes[0].exists()


def test_startup_cleanup_is_retained_until_observation_recovers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.worker.generation as generation_module

    script, _pid_file = _fake_worker_script(tmp_path, "startup_corrupt")
    original_group_exists = _WorkerProcess._group_exists
    observation_available = threading.Event()
    original_mkdtemp = generation_module.tempfile.mkdtemp
    homes: list[Path] = []

    def recording_mkdtemp(*args: object, **kwargs: object) -> str:
        result = original_mkdtemp(*args, **kwargs)
        homes.append(Path(result))
        return result

    def controlled_group_exists(process: _WorkerProcess) -> bool:
        if not observation_available.is_set():
            raise OSError("process-group observation unavailable")
        return original_group_exists(process)

    monkeypatch.setattr(generation_module.tempfile, "mkdtemp", recording_mkdtemp)
    monkeypatch.setattr(_WorkerProcess, "_group_exists", controlled_group_exists)
    with _spawn_boundary_probe():
        with pytest.raises(WorkerError) as caught:
            _WorkerProcess.spawn_for_test(
                command=(sys.executable, str(script)),
                source_root=Path(__file__).parents[1] / "src",
                readiness_timeout_ms=2_000,
                shutdown_timeout_ms=250,
            )
    assert caught.value.code is WorkerErrorCode.START_FAILED
    assert len(homes) == 1
    assert homes[0].exists()
    with generation_module._STARTUP_CLEANUP_LOCK:
        retained = tuple(
            process
            for process in generation_module._STARTUP_CLEANUP.values()
            if process._home == homes[0]
        )
        assert len(retained) == 1
        generation_id = retained[0].generation_id

    observation_available.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with generation_module._STARTUP_CLEANUP_LOCK:
            if generation_id not in generation_module._STARTUP_CLEANUP:
                break
        time.sleep(0.01)
    with generation_module._STARTUP_CLEANUP_LOCK:
        assert generation_id not in generation_module._STARTUP_CLEANUP
        monitor = generation_module._STARTUP_CLEANUP_SWEEPER
        ready = generation_module._STARTUP_CLEANUP_SWEEPER_READY
        assert monitor is not None and monitor.is_alive()
        assert ready is not None and ready.is_set()
    assert not homes[0].exists()


def test_cleanup_sweeper_is_ready_before_posix_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.worker.generation as generation_module

    original_spawn = generation_module._fresh_posix_spawn
    observed: list[bool] = []

    def recording_spawn(**kwargs: object) -> _SpawnedProcess:
        with generation_module._STARTUP_CLEANUP_LOCK:
            monitor = generation_module._STARTUP_CLEANUP_SWEEPER
            ready = generation_module._STARTUP_CLEANUP_SWEEPER_READY
            observed.append(
                monitor is not None and monitor.is_alive() and ready is not None and ready.is_set()
            )
        return original_spawn(**kwargs)

    monkeypatch.setattr(generation_module, "_fresh_posix_spawn", recording_spawn)
    process, _grandchild = _process(tmp_path, "typed_error")
    process.terminate()
    assert observed == [True]


def test_cleanup_sweeper_start_failure_aborts_before_spawn_or_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.worker.generation as generation_module

    script, _pid_file = _fake_worker_script(tmp_path, "startup_hang")

    def unavailable_start(_thread: threading.Thread) -> None:
        raise RuntimeError("thread startup unavailable")

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("spawn resources were created before cleanup supervision")

    with monkeypatch.context() as supervisor_patch:
        supervisor_patch.setattr(
            generation_module,
            "_STARTUP_CLEANUP_SWEEPER",
            None,
        )
        supervisor_patch.setattr(
            generation_module,
            "_STARTUP_CLEANUP_SWEEPER_READY",
            None,
        )
        supervisor_patch.setattr(threading.Thread, "start", unavailable_start)
        supervisor_patch.setattr(generation_module.tempfile, "mkdtemp", forbidden)
        supervisor_patch.setattr(
            generation_module,
            "_fresh_posix_spawn",
            forbidden,
        )
        with pytest.raises(WorkerError) as caught:
            _WorkerProcess.spawn_for_test(
                command=(sys.executable, str(script)),
                source_root=Path(__file__).parents[1] / "src",
                readiness_timeout_ms=250,
                shutdown_timeout_ms=250,
            )
    assert caught.value.code is WorkerErrorCode.START_FAILED


def test_startup_cleanup_retries_a_transient_leader_identity_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.worker.generation as generation_module

    script, _pid_file = _fake_worker_script(tmp_path, "startup_hang")
    original_probe = _SpawnedProcess.exited_without_reaping
    original_spawn = generation_module._fresh_posix_spawn
    probe_calls = 0
    spawned_processes: list[_SpawnedProcess] = []

    def transient_probe(process: _SpawnedProcess) -> bool:
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls == 1:
            raise OSError("transient leader observation failure")
        return original_probe(process)

    def recording_spawn(**kwargs: object) -> _SpawnedProcess:
        process = original_spawn(**kwargs)
        spawned_processes.append(process)
        return process

    monkeypatch.setattr(
        _SpawnedProcess,
        "exited_without_reaping",
        transient_probe,
    )
    monkeypatch.setattr(generation_module, "_fresh_posix_spawn", recording_spawn)
    with _spawn_boundary_probe():
        with pytest.raises(WorkerError) as caught:
            _WorkerProcess.spawn_for_test(
                command=(sys.executable, str(script)),
                source_root=Path(__file__).parents[1] / "src",
                readiness_timeout_ms=250,
                shutdown_timeout_ms=250,
            )
    assert caught.value.code is WorkerErrorCode.START_FAILED
    assert probe_calls >= 2
    assert len(spawned_processes) == 1
    assert _wait_gone(spawned_processes[0].pid)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with generation_module._STARTUP_CLEANUP_LOCK:
            matching = tuple(
                process
                for process in generation_module._STARTUP_CLEANUP.values()
                if process._process is spawned_processes[0]
            )
            if not matching:
                break
        time.sleep(0.01)
    with generation_module._STARTUP_CLEANUP_LOCK:
        assert all(
            process._process is not spawned_processes[0]
            for process in generation_module._STARTUP_CLEANUP.values()
        )


def test_managed_start_recheck_failure_retains_uncertain_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.worker.generation as generation_module
    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime import status as runtime_status

    process, _grandchild = _process(tmp_path, "proxy_idle")
    worker = FreeCadWorker(process)
    original_group_exists = _WorkerProcess._group_exists
    observation_available = threading.Event()
    first = SimpleNamespace(python=Path(sys.executable), marker=1)
    second = SimpleNamespace(python=Path(sys.executable), marker=2)
    evidence = iter((first, second))

    def controlled_group_exists(value: _WorkerProcess) -> bool:
        if not observation_available.is_set():
            raise OSError("process-group observation unavailable")
        return original_group_exists(value)

    def fake_start(
        _cls: type[FreeCadWorker],
        *,
        python: Path,
        source_root: Path,
    ) -> FreeCadWorker:
        assert python == first.python
        assert source_root == Path(__file__).parents[1] / "src"
        return worker

    monkeypatch.setattr(_WorkerProcess, "_group_exists", controlled_group_exists)
    monkeypatch.setattr(
        runtime_paths,
        "active_runtime_prefix",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        runtime_status,
        "capture_runtime_generation_evidence",
        lambda _prefix: next(evidence),
    )
    monkeypatch.setattr(
        runtime_status,
        "engine_compatible_generation",
        lambda value: value is first,
    )
    monkeypatch.setattr(FreeCadWorker, "start", classmethod(fake_start))

    with pytest.raises(WorkerError) as caught:
        FreeCadWorker.start_managed(source_root=Path(__file__).parents[1] / "src")
    assert caught.value.code is WorkerErrorCode.START_FAILED
    assert process.state is WorkerGenerationState.CLEANUP_REQUIRED
    with generation_module._STARTUP_CLEANUP_LOCK:
        assert generation_module._STARTUP_CLEANUP.get(process.generation_id) is process
    home = process._home
    assert home.exists()

    observation_available.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with generation_module._STARTUP_CLEANUP_LOCK:
            if process.generation_id not in generation_module._STARTUP_CLEANUP:
                break
        time.sleep(0.01)
    with generation_module._STARTUP_CLEANUP_LOCK:
        assert process.generation_id not in generation_module._STARTUP_CLEANUP
    assert process.state is WorkerGenerationState.DEAD
    assert not home.exists()


@pytest.mark.parametrize(
    "mode",
    (
        "exit",
        "wrong_generation",
        "oversize",
        "partial_header",
        "partial_body",
        "noncanonical",
        "trickle",
    ),
)
def test_uncertain_fake_worker_failure_kills_and_reaps_generation(
    mode: str,
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, mode)
    pid = process.pid
    with pytest.raises(WorkerError) as caught:
        process.request_for_test(timeout_ms=250)
    assert caught.value.code is WorkerErrorCode.GENERATION_LOST
    assert process.state is WorkerGenerationState.DEAD
    assert _wait_gone(pid)


def test_fake_worker_hang_kills_process_group_including_grandchild(
    tmp_path: Path,
) -> None:
    process, grandchild_file = _process(tmp_path, "hang")
    leader = process.pid
    started = time.monotonic()
    with pytest.raises(WorkerError) as caught:
        process.request_for_test(timeout_ms=150)
    assert time.monotonic() - started < 5
    assert caught.value.code is WorkerErrorCode.GENERATION_LOST
    assert process.state is WorkerGenerationState.DEAD
    deadline = time.monotonic() + 1.0
    while not grandchild_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    grandchild = int(grandchild_file.read_text(encoding="ascii"))
    assert _wait_gone(leader)
    assert _wait_gone(grandchild)


def test_second_identity_probe_failure_still_kills_anchored_process_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process, grandchild_file = _process(tmp_path, "term_leader_with_hung_child")
    original_probe = _SpawnedProcess.exited_without_reaping
    probe_calls = 0

    def second_probe_fails(process_handle: _SpawnedProcess) -> bool:
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls == 2:
            time.sleep(0.05)
            raise OSError("transient post-TERM observation failure")
        return original_probe(process_handle)

    monkeypatch.setattr(
        _SpawnedProcess,
        "exited_without_reaping",
        second_probe_fails,
    )
    leader = process.pid
    with pytest.raises(WorkerError) as caught:
        process.request_for_test(timeout_ms=1_000)
    assert caught.value.code is WorkerErrorCode.GENERATION_LOST
    deadline = time.monotonic() + 2
    while process.state is not WorkerGenerationState.DEAD and time.monotonic() < deadline:
        time.sleep(0.01)
    assert probe_calls >= 2
    assert process.state is WorkerGenerationState.DEAD
    assert _wait_gone(leader)
    assert grandchild_file.exists()
    assert _wait_gone(int(grandchild_file.read_text(encoding="ascii")))


def test_first_identity_probe_failure_does_not_reap_an_exited_group_leader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process, grandchild_file = _process(tmp_path, "exit_leader_with_hung_child")
    original_probe = _SpawnedProcess.exited_without_reaping
    probe_calls = 0

    def first_probe_fails(process_handle: _SpawnedProcess) -> bool:
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls == 1:
            raise OSError("transient initial observation failure")
        return original_probe(process_handle)

    monkeypatch.setattr(
        _SpawnedProcess,
        "exited_without_reaping",
        first_probe_fails,
    )
    leader = process.pid
    with pytest.raises(WorkerError) as caught:
        process.request_for_test(timeout_ms=1_000)
    assert caught.value.code is WorkerErrorCode.GENERATION_LOST
    deadline = time.monotonic() + 2
    while process.state is not WorkerGenerationState.DEAD and time.monotonic() < deadline:
        time.sleep(0.01)
    assert probe_calls >= 2
    assert process.state is WorkerGenerationState.DEAD
    assert _wait_gone(leader)
    assert grandchild_file.exists()
    assert _wait_gone(int(grandchild_file.read_text(encoding="ascii")))


def test_exact_typed_worker_error_does_not_poison_generation(tmp_path: Path) -> None:
    process, _grandchild = _process(tmp_path, "typed_error")
    try:
        with pytest.raises(WorkerError) as caught:
            process.request_for_test(timeout_ms=250)
        assert caught.value.code is WorkerErrorCode.CAD_FAILURE
        assert process.state is WorkerGenerationState.READY
    finally:
        process.terminate()


def test_response_cannot_publish_after_generation_termination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.worker.generation as generation_module

    process, _grandchild = _process(tmp_path, "proxy_idle")
    decoded = threading.Event()
    resume = threading.Event()
    original_decode = generation_module.decode_worker_response
    outcome: dict[str, object] = {}

    def blocking_decode(*args: object, **kwargs: object) -> dict[str, object]:
        result = original_decode(*args, **kwargs)
        decoded.set()
        if not resume.wait(2):
            raise AssertionError("response fence was not released")
        return result

    def request() -> None:
        try:
            outcome["result"] = process.request_for_test(timeout_ms=1_000)
        except BaseException as error:
            outcome["error"] = error

    monkeypatch.setattr(generation_module, "decode_worker_response", blocking_decode)
    thread = threading.Thread(target=request)
    thread.start()
    assert decoded.wait(1)
    process.terminate()
    resume.set()
    thread.join(2)
    assert not thread.is_alive()
    error = outcome.get("error")
    assert isinstance(error, WorkerError)
    assert error.code is WorkerErrorCode.GENERATION_LOST
    assert "result" not in outcome
    assert process.state is WorkerGenerationState.DEAD


def test_lost_generation_cleanup_cannot_kill_a_replacement(tmp_path: Path) -> None:
    first, _grandchild = _process(tmp_path, "exit")
    with pytest.raises(WorkerError):
        first.request_for_test(timeout_ms=250)
    replacement_root = tmp_path / "replacement"
    replacement_root.mkdir()
    replacement, _grandchild = _process(replacement_root, "typed_error")
    try:
        assert first.state is WorkerGenerationState.DEAD
        assert replacement.state is WorkerGenerationState.READY
        with pytest.raises(WorkerError) as caught:
            replacement.request_for_test(timeout_ms=250)
        assert caught.value.code is WorkerErrorCode.CAD_FAILURE
        assert replacement.state is WorkerGenerationState.READY
    finally:
        replacement.terminate()


def test_cleanup_required_can_be_retried_to_dead(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "proxy_idle")
    home = process._home
    original_group_exists = _WorkerProcess._group_exists
    original_killpg = os.killpg

    def unavailable(_self: _WorkerProcess) -> bool:
        raise OSError("process-group observation unavailable")

    monkeypatch.setattr(_WorkerProcess, "_group_exists", unavailable)
    process.terminate()
    assert process.state is WorkerGenerationState.CLEANUP_REQUIRED
    assert home.exists()

    monkeypatch.setattr(_WorkerProcess, "_group_exists", original_group_exists)
    observed_signals: list[int] = []

    def recording_killpg(process_group: int, signal_number: int) -> None:
        observed_signals.append(signal_number)
        original_killpg(process_group, signal_number)

    monkeypatch.setattr(os, "killpg", recording_killpg)
    process.terminate()
    assert process.state is WorkerGenerationState.DEAD
    assert not home.exists()
    assert observed_signals
    assert set(observed_signals) == {0}


def test_missing_leader_identity_never_signals_the_process_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "proxy_idle")
    original_killpg = os.killpg
    observed_signals: list[int] = []

    def identity_unavailable(_process: _SpawnedProcess) -> bool:
        raise OSError("leader identity unavailable")

    def wait_unavailable(
        process_handle: _SpawnedProcess,
        timeout: float,
    ) -> int:
        raise subprocess.TimeoutExpired(process_handle.pid, timeout)

    def recording_killpg(process_group: int, signal_number: int) -> None:
        observed_signals.append(signal_number)
        original_killpg(process_group, signal_number)

    with monkeypatch.context() as identity_patch:
        identity_patch.setattr(
            _SpawnedProcess,
            "exited_without_reaping",
            identity_unavailable,
        )
        identity_patch.setattr(_SpawnedProcess, "wait", wait_unavailable)
        identity_patch.setattr(os, "killpg", recording_killpg)
        process.terminate()
    assert process.state is WorkerGenerationState.CLEANUP_REQUIRED
    assert observed_signals
    assert set(observed_signals) == {0}

    try:
        process._process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        os.kill(process.pid, signal.SIGKILL)
        process._process.wait(timeout=1)
    process.terminate()
    assert process.state is WorkerGenerationState.DEAD


def test_unreleased_leader_identity_prevents_a_false_dead_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "proxy_idle")
    home = process._home

    def identity_unavailable(_process: _SpawnedProcess) -> bool:
        raise OSError("leader identity unavailable")

    with monkeypatch.context() as identity_patch:
        identity_patch.setattr(
            _SpawnedProcess,
            "exited_without_reaping",
            identity_unavailable,
        )
        identity_patch.setattr(
            _WorkerProcess,
            "_group_exists",
            lambda _process: False,
        )
        process.terminate()
        assert process.state is WorkerGenerationState.CLEANUP_REQUIRED
        assert home.exists()

    deadline = time.monotonic() + 2
    while process.state is not WorkerGenerationState.DEAD and time.monotonic() < deadline:
        time.sleep(0.01)
    assert process.state is WorkerGenerationState.DEAD
    assert not home.exists()


def test_home_cleanup_failure_is_retried_before_dead_is_published(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.worker.generation as generation_module

    process, _grandchild = _process(tmp_path, "proxy_idle")
    home = process._home
    original_remove = generation_module._remove_private_home
    cleanup_available = threading.Event()

    def controlled_remove(path: Path) -> bool:
        if not cleanup_available.is_set():
            return False
        return original_remove(path)

    monkeypatch.setattr(
        generation_module,
        "_remove_private_home",
        controlled_remove,
    )
    process.terminate()
    assert process.state is WorkerGenerationState.CLEANUP_REQUIRED
    assert home.exists()

    cleanup_available.set()
    deadline = time.monotonic() + 2
    while process.state is not WorkerGenerationState.DEAD and time.monotonic() < deadline:
        time.sleep(0.01)
    assert process.state is WorkerGenerationState.DEAD
    assert not home.exists()


@pytest.mark.parametrize(
    ("error_type", "propagates"),
    ((RuntimeError, False), (KeyboardInterrupt, True)),
)
def test_home_cleanup_exception_retains_recovery_responsibility(
    error_type: type[BaseException],
    propagates: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.worker.generation as generation_module

    process, _grandchild = _process(tmp_path, "proxy_idle")
    home = process._home
    original_remove = generation_module._remove_private_home
    cleanup_available = threading.Event()

    def controlled_remove(path: Path) -> bool:
        if not cleanup_available.is_set():
            raise error_type("private home cleanup unavailable")
        return original_remove(path)

    monkeypatch.setattr(
        generation_module,
        "_remove_private_home",
        controlled_remove,
    )
    if propagates:
        with pytest.raises(error_type):
            process.terminate()
    else:
        process.terminate()
    assert process.state is WorkerGenerationState.CLEANUP_REQUIRED
    assert home.exists()
    with generation_module._STARTUP_CLEANUP_LOCK:
        assert generation_module._STARTUP_CLEANUP.get(process.generation_id) is process

    cleanup_available.set()
    deadline = time.monotonic() + 2
    while process.state is not WorkerGenerationState.DEAD and time.monotonic() < deadline:
        time.sleep(0.01)
    assert process.state is WorkerGenerationState.DEAD
    assert not home.exists()


def test_group_observation_control_flow_retains_recovery_responsibility(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.worker.generation as generation_module

    process, _grandchild = _process(tmp_path, "proxy_idle")
    home = process._home

    def interrupted_observation(_process: _WorkerProcess) -> bool:
        raise KeyboardInterrupt

    with monkeypatch.context() as observation_patch:
        observation_patch.setattr(
            _WorkerProcess,
            "_group_exists",
            interrupted_observation,
        )
        with pytest.raises(KeyboardInterrupt):
            process.terminate()
        assert process.state is WorkerGenerationState.CLEANUP_REQUIRED
        assert home.exists()
        with generation_module._STARTUP_CLEANUP_LOCK:
            assert generation_module._STARTUP_CLEANUP.get(process.generation_id) is process

    deadline = time.monotonic() + 2
    while process.state is not WorkerGenerationState.DEAD and time.monotonic() < deadline:
        time.sleep(0.01)
    assert process.state is WorkerGenerationState.DEAD
    assert not home.exists()


def test_connection_control_flow_cannot_leave_termination_unowned(
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "proxy_idle")
    connection = process._connection
    assert connection is not None
    home = process._home

    class InterruptedConnection:
        def shutdown(self, how: int) -> None:
            connection.shutdown(how)
            raise KeyboardInterrupt

        def close(self) -> None:
            connection.close()

    process._connection = InterruptedConnection()  # type: ignore[assignment]
    with pytest.raises(KeyboardInterrupt):
        process.terminate()
    assert process.state is WorkerGenerationState.DEAD
    assert not home.exists()


def test_connection_close_failure_is_retained_until_retry(
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "proxy_idle")
    connection = process._connection
    assert connection is not None
    close_available = threading.Event()

    class RetryableConnection:
        def shutdown(self, how: int) -> None:
            connection.shutdown(how)

        def close(self) -> None:
            if not close_available.is_set():
                raise RuntimeError("connection close unavailable")
            connection.close()

    wrapper = RetryableConnection()
    process._connection = wrapper  # type: ignore[assignment]
    process.terminate()
    assert process.state is WorkerGenerationState.CLEANUP_REQUIRED
    assert process._connection is wrapper

    close_available.set()
    deadline = time.monotonic() + 2
    while process.state is not WorkerGenerationState.DEAD and time.monotonic() < deadline:
        time.sleep(0.01)
    assert process.state is WorkerGenerationState.DEAD
    assert process._connection is None


def test_registered_terminating_generation_can_be_taken_over(
    tmp_path: Path,
) -> None:
    import vibecad.worker.generation as generation_module

    process, _grandchild = _process(tmp_path, "proxy_idle")
    home = process._home
    with process._lifecycle_lock:
        process._state = WorkerGenerationState.TERMINATING
    generation_module._retain_startup_cleanup(process)

    process.terminate()
    deadline = time.monotonic() + 2
    while process.state is not WorkerGenerationState.DEAD and time.monotonic() < deadline:
        time.sleep(0.01)
    assert process.state is WorkerGenerationState.DEAD
    assert process._connection is None
    assert not home.exists()


def test_worker_handles_are_identity_capabilities_and_generation_fenced(
    tmp_path: Path,
) -> None:
    worker = object.__new__(FreeCadWorker)
    with pytest.raises((AttributeError, WorkerError, TypeError)):
        worker.bind_candidate(
            directory=_candidate_directory_at(tmp_path / "arbitrary"),
            base_head=_head(),
            revision_id=_CANDIDATE_REVISION,
        )

    for value in (worker,):
        with pytest.raises((pickle.PicklingError, TypeError, AttributeError)):
            pickle.dumps(value)


def test_candidate_bind_requires_a_live_store_reservation_and_lease(
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "proxy_idle")
    worker = FreeCadWorker(process)
    with _candidate_rig(tmp_path, suffix="released-lease") as rig:
        rig.lease.release(owner_token=rig.lease.owner_token)
        try:
            with pytest.raises(WorkerError) as caught:
                worker.bind_candidate(
                    store=rig.store,
                    lease=rig.lease,
                    base_head=rig.head,
                    revision_id=rig.revision_id,
                )
            assert caught.value.code is WorkerErrorCode.INVALID_CANDIDATE
            assert worker.state is WorkerGenerationState.READY
        finally:
            worker.close()


def test_candidate_release_reclaims_worker_capacity(tmp_path: Path) -> None:
    process, _grandchild = _process(tmp_path, "proxy_idle")
    worker = FreeCadWorker(process)
    with _candidate_rig(tmp_path, suffix="candidate-release") as rig:
        try:
            for _index in range(12):
                candidate = worker.bind_candidate(
                    store=rig.store,
                    lease=rig.lease,
                    base_head=rig.head,
                    revision_id=rig.revision_id,
                )
                state = worker._candidates[candidate]
                descriptors = (state.candidates_fd, state.directory_fd)
                worker.release_candidate(candidate)
                for descriptor in descriptors:
                    with pytest.raises(OSError):
                        os.fstat(descriptor)
                with pytest.raises(WorkerError) as stale:
                    worker.release_candidate(candidate)
                assert stale.value.code is WorkerErrorCode.INVALID_HANDLE
            assert worker.state is WorkerGenerationState.READY
            assert worker._candidates == {}
        finally:
            worker.close()


def test_production_service_release_closes_child_owned_descriptor(
    tmp_path: Path,
) -> None:
    service = WorkerService(_GENERATION)
    candidate_id = "worker_candidate_" + "a" * 32
    directory = _candidate_directory_at(tmp_path / "service-release")
    params = {
        "candidate_id": candidate_id,
        "project_id": _PROJECT_ID,
        "revision_id": _CANDIDATE_REVISION,
        "base_revision_id": _BASE_REVISION,
    }
    for _index in range(12):
        descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            assert service._bind(params, (descriptor,)) == {"candidate_id": candidate_id}
        except BaseException:
            os.close(descriptor)
            raise
        assert service._release_candidate({"candidate_id": candidate_id}) == {
            "candidate_id": candidate_id
        }
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert service._candidates == {}


def test_candidate_cleanup_remains_available_after_staging_is_revoked(
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "proxy_idle")
    worker = FreeCadWorker(process)
    with _candidate_rig(tmp_path, suffix="candidate-revoked") as rig:
        candidate = worker.bind_candidate(
            store=rig.store,
            lease=rig.lease,
            base_head=rig.head,
            revision_id=rig.revision_id,
        )
        rig.store.rollback_revision(
            rig.head.project_id,
            rig.revision_id,
            rig.lease,
        )
        try:
            with pytest.raises(WorkerError) as caught:
                worker.create_empty(candidate)
            assert caught.value.code is WorkerErrorCode.INTEGRITY_FAILURE
            assert worker.state is WorkerGenerationState.READY
            worker.release_candidate(candidate)
            assert worker._candidates == {}
            assert worker.state is WorkerGenerationState.READY
        finally:
            worker.close()


def test_candidate_response_cannot_publish_after_worker_termination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.worker.proxy as proxy_module

    process, _grandchild = _process(tmp_path, "proxy_idle")
    worker = FreeCadWorker(process)
    response_received = threading.Event()
    resume = threading.Event()
    original_request = _WorkerProcess.request
    original_open = proxy_module._open_worker_candidate_staging
    outcome: dict[str, object] = {}
    opened: list[int] = []

    def recording_open(*args: object, **kwargs: object) -> object:
        staging = original_open(*args, **kwargs)
        opened.extend((staging[0], staging[1]))
        return staging

    def blocking_request(
        self: _WorkerProcess,
        method: str,
        params: dict[str, object],
        *,
        timeout_ms: int,
        capability_fd: int | None = None,
    ) -> dict[str, object]:
        result = original_request(
            self,
            method,
            params,
            timeout_ms=timeout_ms,
            capability_fd=capability_fd,
        )
        if method == "candidate.bind":
            response_received.set()
            if not resume.wait(2):
                raise AssertionError("candidate publication fence was not released")
        return result

    monkeypatch.setattr(_WorkerProcess, "request", blocking_request)
    monkeypatch.setattr(proxy_module, "_open_worker_candidate_staging", recording_open)
    with _candidate_rig(tmp_path, suffix="bind-race") as rig:

        def bind() -> None:
            try:
                outcome["candidate"] = worker.bind_candidate(
                    store=rig.store,
                    lease=rig.lease,
                    base_head=rig.head,
                    revision_id=rig.revision_id,
                )
            except BaseException as error:
                outcome["error"] = error

        thread = threading.Thread(target=bind)
        thread.start()
        assert response_received.wait(1)
        worker.terminate()
        resume.set()
        thread.join(2)
        assert not thread.is_alive()
        error = outcome.get("error")
        assert isinstance(error, WorkerError)
        assert error.code is WorkerErrorCode.GENERATION_LOST
        assert "candidate" not in outcome
        assert worker._candidates == {}
        assert len(opened) == 2
        for descriptor in opened:
            with pytest.raises(OSError):
                os.fstat(descriptor)


def test_candidate_response_cannot_publish_after_lease_revocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.worker.proxy as proxy_module

    process, _grandchild = _process(tmp_path, "proxy_idle")
    worker = FreeCadWorker(process)
    response_received = threading.Event()
    resume = threading.Event()
    original_request = _WorkerProcess.request
    original_open = proxy_module._open_worker_candidate_staging
    outcome: dict[str, object] = {}
    opened: list[int] = []

    def recording_open(*args: object, **kwargs: object) -> object:
        staging = original_open(*args, **kwargs)
        opened.extend((staging[0], staging[1]))
        return staging

    def blocking_request(
        self: _WorkerProcess,
        method: str,
        params: dict[str, object],
        *,
        timeout_ms: int,
        capability_fd: int | None = None,
    ) -> dict[str, object]:
        result = original_request(
            self,
            method,
            params,
            timeout_ms=timeout_ms,
            capability_fd=capability_fd,
        )
        if method == "candidate.bind":
            response_received.set()
            if not resume.wait(2):
                raise AssertionError("candidate authority fence was not released")
        return result

    monkeypatch.setattr(_WorkerProcess, "request", blocking_request)
    monkeypatch.setattr(proxy_module, "_open_worker_candidate_staging", recording_open)
    with _candidate_rig(tmp_path, suffix="bind-lease-race") as rig:

        def bind() -> None:
            try:
                outcome["candidate"] = worker.bind_candidate(
                    store=rig.store,
                    lease=rig.lease,
                    base_head=rig.head,
                    revision_id=rig.revision_id,
                )
            except BaseException as error:
                outcome["error"] = error

        thread = threading.Thread(target=bind)
        thread.start()
        assert response_received.wait(1)
        rig.lease.release(owner_token=rig.lease.owner_token)
        resume.set()
        thread.join(2)
        assert not thread.is_alive()
        error = outcome.get("error")
        assert isinstance(error, WorkerError)
        assert error.code is WorkerErrorCode.GENERATION_LOST
        assert "candidate" not in outcome
        assert worker._candidates == {}
        assert worker.state is WorkerGenerationState.DEAD
        assert len(opened) == 2
        for descriptor in opened:
            with pytest.raises(OSError):
                os.fstat(descriptor)


def test_proxy_handles_are_nonserializable_and_generation_loss_invalidates_all(
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "hang_after_create")
    worker = FreeCadWorker(process)
    with _candidate_rig(tmp_path, suffix="loss-store") as rig:
        candidate = worker.bind_candidate(
            store=rig.store,
            lease=rig.lease,
            base_head=rig.head,
            revision_id=rig.revision_id,
        )
        session = worker.create_empty(candidate)
        with pytest.raises(TypeError):
            pickle.dumps(candidate)
        with pytest.raises(TypeError):
            pickle.dumps(session)

        with pytest.raises(WorkerError) as caught:
            worker.close_session(session)
        assert caught.value.code is WorkerErrorCode.GENERATION_LOST
        assert worker.state is WorkerGenerationState.DEAD
        with pytest.raises(WorkerError) as stale:
            worker.create_empty(candidate)
        assert stale.value.code is WorkerErrorCode.GENERATION_LOST


@pytest.mark.parametrize("entry_kind", ("symlink", "hardlink"))
def test_candidate_fd_capability_rejects_alias_entries_before_dispatch(
    entry_kind: str,
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "proxy_idle")
    worker = FreeCadWorker(process)
    with _candidate_rig(tmp_path, suffix=f"alias-{entry_kind}") as rig:
        outside = tmp_path / f"outside-{entry_kind}.FCStd"
        outside.write_bytes(b"outside-sentinel")
        outside.chmod(0o600)
        model = rig.directory / "model.FCStd"
        model.unlink()
        if entry_kind == "symlink":
            model.symlink_to(outside)
        else:
            os.link(outside, model)
        try:
            with pytest.raises(WorkerError) as caught:
                worker.bind_candidate(
                    store=rig.store,
                    lease=rig.lease,
                    base_head=rig.head,
                    revision_id=rig.revision_id,
                )
            assert caught.value.code is WorkerErrorCode.INVALID_CANDIDATE
            assert outside.read_bytes() == b"outside-sentinel"
            assert worker.state is WorkerGenerationState.READY
        finally:
            worker.close()


def test_bound_candidate_path_replacement_fails_locally_without_using_new_path(
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "proxy_idle")
    worker = FreeCadWorker(process)
    with _candidate_rig(tmp_path, suffix="rebind-store") as rig:
        candidate = worker.bind_candidate(
            store=rig.store,
            lease=rig.lease,
            base_head=rig.head,
            revision_id=rig.revision_id,
        )
        displaced = tmp_path / "displaced"
        rig.directory.rename(displaced)
        replacement = _candidate_directory_at(rig.directory)
        try:
            with pytest.raises(WorkerError) as caught:
                worker.create_empty(candidate)
            assert caught.value.code is WorkerErrorCode.INTEGRITY_FAILURE
            assert (replacement / "model.FCStd").stat().st_size == 0
            assert worker.state is WorkerGenerationState.READY
        finally:
            worker.close()


def test_typed_session_close_failure_invalidates_the_generation(
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "close_error")
    worker = FreeCadWorker(process)
    pid = worker.pid
    with _candidate_rig(tmp_path, suffix="close-error") as rig:
        candidate = worker.bind_candidate(
            store=rig.store,
            lease=rig.lease,
            base_head=rig.head,
            revision_id=rig.revision_id,
        )
        session = worker.create_empty(candidate)
        state = worker._candidates[candidate]
        descriptors = (state.candidates_fd, state.directory_fd)
        with pytest.raises(WorkerError) as caught:
            worker.close_session(session)
        assert caught.value.code is WorkerErrorCode.GENERATION_LOST
        assert worker.state is WorkerGenerationState.DEAD
        assert worker._sessions == {}
        assert worker._candidates == {}
        assert _wait_gone(pid)
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)


def test_session_close_and_candidate_release_use_exact_cleanup_deadlines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "proxy_idle")
    worker = FreeCadWorker(process)
    trace: list[tuple[str, int]] = []
    original_request = _WorkerProcess.request

    def recording_request(
        self: _WorkerProcess,
        method: str,
        params: dict[str, object],
        *,
        timeout_ms: int,
        capability_fd: int | None = None,
    ) -> dict[str, object]:
        trace.append((method, timeout_ms))
        return original_request(
            self,
            method,
            params,
            timeout_ms=timeout_ms,
            capability_fd=capability_fd,
        )

    monkeypatch.setattr(_WorkerProcess, "request", recording_request)
    with _candidate_rig(tmp_path, suffix="cleanup-deadlines") as rig:
        try:
            candidate = worker.bind_candidate(
                store=rig.store,
                lease=rig.lease,
                base_head=rig.head,
                revision_id=rig.revision_id,
            )
            session = worker.create_empty(candidate)
            worker.close_session(session)
            worker.release_candidate(candidate)
            assert trace == [
                ("candidate.bind", 30_000),
                ("session.create_empty", 30_000),
                ("session.close", 5_000),
                ("candidate.release", 5_000),
            ]
        finally:
            worker.close()


def test_command_timeout_uses_frozen_operation_deadline_and_kills_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "command_hang")
    worker = FreeCadWorker(process)
    pid = worker.pid
    trace: list[tuple[str, int]] = []
    original_request = _WorkerProcess.request

    def recording_request(
        self: _WorkerProcess,
        method: str,
        params: dict[str, object],
        *,
        timeout_ms: int,
        capability_fd: int | None = None,
    ) -> dict[str, object]:
        trace.append((method, timeout_ms))
        return original_request(
            self,
            method,
            params,
            timeout_ms=timeout_ms,
            capability_fd=capability_fd,
        )

    monkeypatch.setattr(_WorkerProcess, "request", recording_request)
    with _candidate_rig(tmp_path, suffix="command-timeout") as rig:
        candidate = worker.bind_candidate(
            store=rig.store,
            lease=rig.lease,
            base_head=rig.head,
            revision_id=rig.revision_id,
        )
        session = worker.create_empty(candidate)
        with pytest.raises(WorkerError) as caught:
            worker.execute_program(
                program=_inspect_program(base_revision=rig.head.revision_id),
                candidate=candidate,
                session=session,
            )
        assert caught.value.code is WorkerErrorCode.GENERATION_LOST
        assert trace == [
            ("candidate.bind", 30_000),
            ("session.create_empty", 30_000),
            ("program.begin", 30_000),
            ("program.execute_command", 10_000),
        ]
        assert worker.state is WorkerGenerationState.DEAD
        assert _wait_gone(pid)


@pytest.mark.parametrize(
    ("mode", "operation"),
    (
        ("checkpoint_cross", "checkpoint"),
        ("checkpoint_bad_claim", "checkpoint"),
        ("export_cross", "export_step"),
        ("export_replace", "export_step"),
    ),
)
def test_cross_artifact_mutation_is_generation_loss(
    mode: str,
    operation: str,
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, mode)
    worker = FreeCadWorker(process)
    with _candidate_rig(tmp_path, suffix=mode) as rig:
        candidate = worker.bind_candidate(
            store=rig.store,
            lease=rig.lease,
            base_head=rig.head,
            revision_id=rig.revision_id,
        )
        session = worker.create_empty(candidate)
        model = rig.directory / "model.FCStd"
        step = rig.directory / "model.step"
        model_before = model.stat()
        step_before = step.stat()
        with pytest.raises(WorkerError) as caught:
            getattr(worker, operation)(session=session, candidate=candidate)
        assert caught.value.code is WorkerErrorCode.GENERATION_LOST
        assert worker.state is WorkerGenerationState.DEAD
        assert worker._sessions == {}
        assert worker._candidates == {}
        if mode == "checkpoint_cross":
            assert model.read_bytes() == b"checkpoint-model"
            assert step.read_bytes() == b"tampered-step"
        elif mode == "checkpoint_bad_claim":
            assert model.read_bytes() == b"checkpoint-model"
            assert step.read_bytes() == b""
            assert step.stat().st_ino == step_before.st_ino
        elif mode == "export_cross":
            assert model.read_bytes() == b"tampered-model"
            assert step.read_bytes() == b"export-step"
        else:
            assert model.read_bytes() == b""
            assert model.stat().st_ino == model_before.st_ino
            assert step.read_bytes() == b"export-step"
            assert step.stat().st_ino != step_before.st_ino


def test_multiple_scm_rights_descriptors_are_rejected_and_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.worker.service as service_module

    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    first = os.open(__file__, os.O_RDONLY)
    second = os.open(__file__, os.O_RDONLY)
    real_close = os.close
    closed: list[int] = []

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(service_module.os, "close", recording_close)
    try:
        rights = array.array("i", (first, second))
        sent = left.sendmsg(
            (b"\x00\x00\x00\x02",),
            ((socket.SOL_SOCKET, socket.SCM_RIGHTS, rights),),
        )
        assert sent == 4
        with pytest.raises(WorkerCodecError):
            _recv_header_with_descriptors(right)
        assert len(closed) == 2
        assert first not in closed
        assert second not in closed
        for descriptor in closed:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        left.close()
        right.close()
        real_close(first)
        real_close(second)


def test_parent_importing_worker_supervision_does_not_import_freecad() -> None:
    assert "FreeCAD" not in sys.modules
    assert "Part" not in sys.modules


@pytest.mark.slow
def test_real_managed_worker_load_modify_checkpoint_and_export(
    tmp_path: Path,
) -> None:
    python_raw = os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON")
    if not python_raw:
        pytest.skip("managed FreeCAD Python was not requested")
    python = Path(python_raw)
    if not python.is_file():
        pytest.skip("managed FreeCAD Python is unavailable")

    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime.status import capture_runtime_generation_evidence

    evidence = capture_runtime_generation_evidence(runtime_paths.active_runtime_prefix())
    assert python.resolve() == evidence.python.resolve()

    source_root = Path(__file__).parents[1] / "src"
    with _candidate_rig(tmp_path, suffix="real-store") as rig:
        worker = FreeCadWorker.start_managed(source_root=source_root)
        candidate = None
        try:
            candidate = worker.bind_candidate(
                store=rig.store,
                lease=rig.lease,
                base_head=rig.head,
                revision_id=rig.revision_id,
            )
            session = worker.create_empty(candidate)
            outcomes = worker.execute_program(
                program=_program(base_revision=rig.head.revision_id),
                candidate=candidate,
                session=session,
            )
            assert [item.result.ok for item in outcomes] == [True, True, True]
            worker.checkpoint(session=session, candidate=candidate)
            worker.export_step(session=session, candidate=candidate)
            worker.close_session(session)

            loaded = worker.load_fcstd(candidate)
            loaded_outcomes = worker.execute_program(
                program=_inspect_program(base_revision=rig.head.revision_id),
                candidate=candidate,
                session=loaded,
            )
            assert len(loaded_outcomes) == 1
            assert loaded_outcomes[0].result.ok is True
            shape = loaded_outcomes[0].result.value["shape"]
            assert shape["volume_mm3"] == pytest.approx(9_000)
            assert tuple(shape["bbox_mm"]) == pytest.approx((15, 20, 30))
            worker.close_session(loaded)
            sessions = tuple(worker.load_fcstd(candidate) for _index in range(6))
            with pytest.raises(WorkerError) as capacity:
                worker.load_fcstd(candidate)
            assert capacity.value.code is WorkerErrorCode.RESOURCE_EXHAUSTED
            assert worker.state is WorkerGenerationState.READY
            for cached in sessions:
                worker.close_session(cached)
            assert (rig.directory / "model.FCStd").stat().st_size > 0
            assert (rig.directory / "model.step").stat().st_size > 0
            worker.release_candidate(candidate)
            candidate = None
            for _index in range(9):
                released = worker.bind_candidate(
                    store=rig.store,
                    lease=rig.lease,
                    base_head=rig.head,
                    revision_id=rig.revision_id,
                )
                worker.release_candidate(released)
        finally:
            if candidate is not None and worker.state is WorkerGenerationState.READY:
                with contextlib.suppress(WorkerError):
                    worker.release_candidate(candidate)
            worker.close()
