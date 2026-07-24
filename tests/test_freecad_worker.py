"""Private FreeCAD Worker generation, protocol, and real-engine tests."""

from __future__ import annotations

import array
import contextlib
import hashlib
import json
import os
import pickle
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
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
    WorkerWireErrorCode,
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


def _validation_directory_at(directory: Path) -> tuple[Path, str]:
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    stage_name = ".stage." + "c" * 32 + ".FCStd"
    normalized_name = ".normalized." + "d" * 32 + ".FCStd"
    for name, value in (
        (stage_name, b"normalized"),
        (normalized_name, b"normalized"),
        ("model.FCStd", b"model"),
        ("model.step", b"step"),
    ):
        path = directory / name
        path.write_bytes(value)
        path.chmod(0o600)
    return directory, stage_name


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

            def hash_candidate(name, candidate_fd):
                target = os.open(name, os.O_RDONLY, dir_fd=candidate_fd)
                try:
                    chunks = []
                    while True:
                        chunk = os.read(target, 1024 * 1024)
                        if not chunk:
                            break
                        chunks.append(chunk)
                finally:
                    os.close(target)
                value = b"".join(chunks)
                return hashlib.sha256(value).hexdigest(), len(value)

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
                "revision_observe",
                "observe_bad",
                "validation_idle",
                "validation_bad_claim",
                "validation_cross",
            }}:
                candidate_fd = -1
                revision_fd = -1
                while True:
                    method = request["method"]
                    if method == "candidate.bind":
                        if len(descriptors) != 1 or candidate_fd >= 0:
                            raise SystemExit(2)
                        candidate_fd = descriptors[0]
                        result = {{"candidate_id": request["params"]["candidate_id"]}}
                    elif method == "revision.bind":
                        if len(descriptors) != 1 or revision_fd >= 0:
                            raise SystemExit(2)
                        revision_fd = descriptors[0]
                        result = {{"revision_id": request["params"]["revision_id"]}}
                    elif method in {{
                        "validation.validate_import",
                        "validation.revalidate_import",
                        "validation.validate_materialization",
                    }}:
                        if len(descriptors) != 1:
                            raise SystemExit(2)
                        validation_fd = descriptors[0]
                        try:
                            if method == "validation.validate_materialization":
                                fcstd_digest, fcstd_size = hash_candidate(
                                    "model.FCStd", validation_fd
                                )
                                step_digest, step_size = hash_candidate(
                                    "model.step", validation_fd
                                )
                                result = {{
                                    "fcstd_sha256": fcstd_digest,
                                    "fcstd_size_bytes": fcstd_size,
                                    "step_sha256": step_digest,
                                    "step_size_bytes": step_size,
                                }}
                            else:
                                if mode == "validation_cross":
                                    write_candidate(
                                        "model.step", b"cross-mutation", validation_fd
                                    )
                                digest, size = hash_candidate(
                                    request["params"]["name"], validation_fd
                                )
                                if mode == "validation_bad_claim":
                                    digest = "0" * 64
                                result = {{"sha256": digest, "size_bytes": size}}
                        finally:
                            os.close(validation_fd)
                    elif descriptors:
                        raise SystemExit(2)
                    elif method == "candidate.release":
                        os.close(candidate_fd)
                        candidate_fd = -1
                        result = {{"candidate_id": request["params"]["candidate_id"]}}
                    elif method == "revision.release":
                        os.close(revision_fd)
                        revision_fd = -1
                        result = {{"revision_id": request["params"]["revision_id"]}}
                    elif method == "session.create_empty":
                        result = {{"session_id": "worker_session_" + "8" * 32}}
                    elif method == "session.load_revision":
                        result = {{"session_id": "worker_session_" + "8" * 32}}
                    elif method == "session.observe" and mode == "observe_bad":
                        result = {{"entities": [], "shape": {{}}}}
                    elif method == "session.observe":
                        result = {{
                            "entities": [],
                            "shape": {{
                                "area_mm2": 6,
                                "bbox_mm": [1, 1, 1],
                                "center_of_mass_mm": [0.5, 0.5, 0.5],
                                "schema_version": 1,
                                "solid_count": 1,
                                "target": "body",
                                "valid_shape": True,
                                "volume_mm3": 1,
                            }},
                        }}
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
                        with open({str(grandchild)!r}, "w", encoding="ascii") as handle:
                            handle.write(str(os.getpid()))
                            handle.flush()
                            os.fsync(handle.fileno())
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
                        if candidate_fd >= 0:
                            os.close(candidate_fd)
                            candidate_fd = -1
                        if revision_fd >= 0:
                            os.close(revision_fd)
                            revision_fd = -1
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


def test_agent_active_cancel_kills_mutating_worker_and_recovers_clean_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from vibecad.application.agent import AgentApplication
    from vibecad.application.project import ProjectRuntime
    from vibecad.application.task_api import (
        TaskServicePortErrorCode,
        TaskServicePortFailure,
    )
    from vibecad.execution.revisions import (
        CommitJournalState,
        ReconciliationStatus,
    )
    from vibecad.execution.worker_port import WorkerCadExecutionPort
    from vibecad.workflow.state import (
        ReasoningOwner,
        ReviewPolicy,
        TaskEvent,
        TaskStatus,
    )
    from vibecad.workflow.store import StoredTaskRun

    command_requested = threading.Event()
    original_request = _WorkerProcess.request

    def observe_command_request(
        self: _WorkerProcess,
        method: str,
        params: dict[str, object],
        *,
        timeout_ms: int,
        capability_fd: int | None = None,
    ) -> dict[str, object]:
        if method == "program.execute_command":
            command_requested.set()
        return original_request(
            self,
            method,
            params,
            timeout_ms=timeout_ms,
            capability_fd=capability_fd,
        )

    monkeypatch.setattr(_WorkerProcess, "request", observe_command_request)

    application: AgentApplication | None = None
    submitter: threading.Thread | None = None
    application_closed = False
    task_id = _TASK_ID
    processes: list[_WorkerProcess] = []
    workers: list[FreeCadWorker] = []
    command_markers: list[Path] = []
    ports: list[WorkerCadExecutionPort] = []
    termination_snapshots: list[StoredTaskRun] = []
    submission_results: list[object] = []
    submission_errors: list[BaseException] = []
    modes = iter(("command_hang", "proxy_idle"))

    def start_worker(*, source_root: Path) -> FreeCadWorker:
        assert source_root == Path(__file__).parents[1] / "src"
        try:
            mode = next(modes)
        except StopIteration:
            raise AssertionError("unexpected third Worker generation") from None
        process, marker = _process(tmp_path, mode)
        if mode == "command_hang":
            process._test_timeout_cap_ms = 5_000  # noqa: SLF001
        worker = FreeCadWorker(process)
        processes.append(process)
        workers.append(worker)
        command_markers.append(marker)
        return worker

    class ObservedWorkerCadExecutionPort(WorkerCadExecutionPort):
        def terminate_generation(self) -> None:
            if command_requested.is_set() and not termination_snapshots:
                assert application is not None
                termination_snapshots.append(
                    application._task_store.load(task_id)  # noqa: SLF001
                )
            super().terminate_generation()

    def build_port(*, revision_store: LocalRevisionStore) -> WorkerCadExecutionPort:
        port = ObservedWorkerCadExecutionPort(
            store=revision_store,
            worker_factory=start_worker,
        )
        ports.append(port)
        return port

    try:
        home = tmp_path / "agent-home"
        home.mkdir(mode=0o700)
        application = AgentApplication.open(
            data_root=home / "data",
            cad_port_factory=build_port,
        )
        project = application.bootstrap_empty()
        base_head = project.head
        base_revision = application._revision_store.load_revision(  # noqa: SLF001
            base_head.project_id,
            base_head.revision_id,
        )
        created = application.create_task(
            task_id=task_id,
            project_id=base_head.project_id,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.AUTO_COMMIT,
        )
        assert type(created) is StoredTaskRun

        def submit() -> None:
            assert application is not None
            try:
                submission_results.append(
                    application.submit_model_program(
                        task_id=task_id,
                        expected_generation=created.generation,
                        program=_program(base_revision=base_head.revision_id),
                    )
                )
            except BaseException as error:
                submission_errors.append(error)

        submitter = threading.Thread(target=submit, name="agent-mutating-submit")
        submitter.start()
        assert command_requested.wait(timeout=15)
        assert len(processes) == len(workers) == len(command_markers) == 1
        command_marker = command_markers[0]
        expected_marker = str(processes[0].pid)
        observed_marker = None
        marker_deadline = time.monotonic() + 15
        while time.monotonic() < marker_deadline:
            with contextlib.suppress(OSError, UnicodeError):
                observed_marker = command_marker.read_text(encoding="ascii")
            if observed_marker == expected_marker:
                break
            time.sleep(0.01)
        assert observed_marker == expected_marker

        executing = application._task_store.load(task_id)  # noqa: SLF001
        assert executing.task_run.status is TaskStatus.EXECUTING
        assert executing.task_run.candidate_revision is not None
        candidate_revision = executing.task_run.candidate_revision
        first_pid = processes[0].pid
        first_generation = workers[0].generation_id

        cancelled = application.cancel_task(
            task_id=task_id,
            expected_generation=executing.generation,
        )
        if type(cancelled) is TaskServicePortFailure:
            assert cancelled.code is TaskServicePortErrorCode.RECOVERY_REQUIRED
            pending = application._task_store.load(task_id)  # noqa: SLF001
            assert pending.task_run.status in {
                TaskStatus.CANCEL_REQUESTED,
                TaskStatus.CANCELLING,
            }
            assert [
                record.event
                for record in pending.task_run.transitions
                if record.event
                in {
                    TaskEvent.REQUEST_CANCEL,
                    TaskEvent.REQUEST_ACTIVE_CANCEL,
                }
            ] == [TaskEvent.REQUEST_CANCEL]
            assert len(ports) == len(workers) == 1
            assert workers[0].generation_id == first_generation
            if pending.task_run.status is TaskStatus.CANCEL_REQUESTED:
                assert application._cad_execution_port is ports[0]  # noqa: SLF001
                assert ports[0]._worker is workers[0]  # noqa: SLF001
            else:
                assert (  # noqa: SLF001
                    application._cad_execution_port is None
                    or application._cad_execution_port is ports[0]
                )
                assert ports[0]._worker is None or ports[0]._worker is workers[0]  # noqa: SLF001
            cancelled = application.cancel_task(
                task_id=task_id,
                expected_generation=executing.generation,
            )
        assert type(cancelled) is StoredTaskRun
        submitter.join(timeout=5)

        assert not submitter.is_alive()
        assert submission_errors == []
        assert len(submission_results) == 1
        submission = submission_results[0]
        assert type(submission) is StoredTaskRun
        assert any(
            record.event is TaskEvent.REQUEST_CANCEL for record in submission.task_run.transitions
        )
        assert submission.task_run.status in {
            TaskStatus.CANCEL_REQUESTED,
            TaskStatus.CANCELLING,
            TaskStatus.CANCELLED,
        }
        assert len(termination_snapshots) == 1
        assert termination_snapshots[0].task_run.status is TaskStatus.CANCEL_REQUESTED
        assert termination_snapshots[0].generation == executing.generation + 1
        assert termination_snapshots[0].task_run.transitions[-1].event is (TaskEvent.REQUEST_CANCEL)

        assert cancelled.task_run.status is TaskStatus.CANCELLED
        assert application._task_store.load(task_id) == cancelled  # noqa: SLF001
        assert [
            record.event
            for record in cancelled.task_run.transitions
            if record.event
            in {
                TaskEvent.REQUEST_CANCEL,
                TaskEvent.START_CANCELLATION,
                TaskEvent.CONFIRM_CANCELLED,
            }
        ] == [
            TaskEvent.REQUEST_CANCEL,
            TaskEvent.START_CANCELLATION,
            TaskEvent.CONFIRM_CANCELLED,
        ]
        assert workers[0].state is WorkerGenerationState.DEAD
        assert processes[0].state is WorkerGenerationState.DEAD
        assert _wait_gone(first_pid)
        assert ports[0].generation_lost is True
        assert application._cad_execution_port is None  # noqa: SLF001
        assert application._runtimes == {}  # noqa: SLF001
        assert application._cad_task_admissions == {}  # noqa: SLF001

        assert application._revision_store.load_head(base_head.project_id) == (  # noqa: SLF001
            base_head
        )
        assert (
            application._revision_store.load_revision(  # noqa: SLF001
                base_head.project_id,
                base_head.revision_id,
            )
            == base_revision
        )
        with application._lease_manager.acquire_project_write(  # noqa: SLF001
            base_head.project_id
        ) as lease:
            reconciliation = application._revision_store.reconcile(  # noqa: SLF001
                base_head.project_id,
                lease,
            )
        assert reconciliation.status is ReconciliationStatus.NOT_COMMITTED
        assert reconciliation.head == base_head
        assert reconciliation.journal is not None
        assert reconciliation.journal.project_id == base_head.project_id
        assert reconciliation.journal.expected_head == base_head
        assert reconciliation.journal.candidate_revision == candidate_revision
        assert reconciliation.journal.manifest_sha256 == base_head.manifest_sha256
        assert reconciliation.journal.state is CommitJournalState.NOT_COMMITTED
        assert reconciliation.journal.id.startswith("transaction_")
        assert len(reconciliation.journal.id) == len("transaction_") + 32
        int(reconciliation.journal.id.removeprefix("transaction_"), 16)

        candidate_roots = tuple(application._layout.projects.rglob("candidates"))  # noqa: SLF001
        reservation_roots = tuple(
            application._layout.projects.rglob("reservations")  # noqa: SLF001
        )
        assert len(candidate_roots) == len(reservation_roots) == 1
        assert all(tuple(root.iterdir()) == () for root in candidate_roots)
        assert all(tuple(root.iterdir()) == () for root in reservation_roots)

        with application._cad_gate:  # noqa: SLF001
            replacement = application._runtime_for(  # noqa: SLF001
                base_head.project_id
            )
        assert type(replacement) is ProjectRuntime
        assert len(ports) == len(processes) == len(workers) == 2
        assert ports[1] is application._cad_execution_port  # noqa: SLF001
        assert replacement is application._runtimes[base_head.project_id]  # noqa: SLF001
        assert workers[1].state is WorkerGenerationState.READY
        assert processes[1].state is WorkerGenerationState.READY
        assert workers[1].generation_id != first_generation
        assert workers[1].pid != first_pid
        second_pid = workers[1].pid

        application.close()
        application_closed = True
        assert application._cad_execution_port is None  # noqa: SLF001
        assert application._runtimes == {}  # noqa: SLF001
        assert workers[1].state is WorkerGenerationState.DEAD
        assert processes[1].state is WorkerGenerationState.DEAD
        assert _wait_gone(second_pid)
        assert all(worker.state is WorkerGenerationState.DEAD for worker in workers)
    finally:
        if application is not None and not application_closed:
            with contextlib.suppress(Exception):
                application._fence_cad_generation()  # noqa: SLF001
            with contextlib.suppress(Exception):
                application.close()
        for worker in workers:
            with contextlib.suppress(Exception):
                worker.terminate()
        if submitter is not None and submitter.is_alive():
            submitter.join(timeout=5)


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


@pytest.mark.parametrize(
    "method",
    (
        "revision.bind",
        "revision.release",
        "session.load_revision",
        "session.observe",
        "validation.validate_import",
        "validation.revalidate_import",
        "validation.validate_materialization",
    ),
)
def test_worker_codec_admits_only_declared_private_capability_methods(method: str) -> None:
    raw = encode_worker_request(
        {
            "schema_version": 1,
            "generation_id": _GENERATION,
            "request_id": _REQUEST,
            "method": method,
            "params": {},
        }
    )
    assert decode_worker_request(raw)["method"] == method


def test_worker_revision_is_an_opaque_generation_capability() -> None:
    from vibecad.worker import WorkerRevision

    handle = WorkerRevision(
        generation_id=_GENERATION,
        capability_id="worker_revision_" + "8" * 32,
    )
    with pytest.raises(TypeError):
        pickle.dumps(handle)


def test_descriptor_bound_validation_never_sends_an_absolute_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from vibecad.interaction.cad import (
        ValidatedImportEvidence,
        ValidatedMaterializationEvidence,
    )

    directory = tmp_path / "private-validation"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    stage_name = ".import." + "8" * 32 + ".FCStd"
    stage = directory / stage_name
    stage.write_bytes(b"normalized")
    stage.chmod(0o600)
    work_name = ".work." + "a" * 32 + ".FCStd"
    work = directory / work_name
    work.write_bytes(b"work")
    work.chmod(0o600)
    normalized_name = ".normalized." + "9" * 32 + ".FCStd"
    normalized = directory / normalized_name
    normalized.write_bytes(b"normalized")
    normalized.chmod(0o600)
    model = directory / "model.FCStd"
    step = directory / "model.step"
    model.write_bytes(b"model")
    step.write_bytes(b"step")
    model.chmod(0o600)
    step.chmod(0o600)

    service = WorkerService(_GENERATION)
    calls: list[tuple[str, object]] = []

    class Engine:
        def validate_import(self, path: Path) -> ValidatedImportEvidence:
            calls.append(("validate_import", path))
            return ValidatedImportEvidence(
                sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                size_bytes=Path(path).stat().st_size,
            )

        def revalidate_normalized_import(self, path: Path) -> ValidatedImportEvidence:
            calls.append(("revalidate", path))
            return ValidatedImportEvidence(
                sha256=hashlib.sha256(normalized.read_bytes()).hexdigest(),
                size_bytes=normalized.stat().st_size,
            )

        def validate_materialization(
            self,
            *,
            fcstd: Path,
            step: Path,
        ) -> ValidatedMaterializationEvidence:
            calls.append(("materialization", (fcstd, step)))
            return ValidatedMaterializationEvidence(
                fcstd_sha256=hashlib.sha256(model.read_bytes()).hexdigest(),
                fcstd_size_bytes=model.stat().st_size,
                step_sha256=hashlib.sha256(step.read_bytes()).hexdigest(),
                step_size_bytes=step.stat().st_size,
            )

    monkeypatch.setattr(service, "_engine", Engine())
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        assert service.dispatch(
            "validation.validate_import",
            {"name": stage_name},
            (descriptor,),
        )["size_bytes"] == len(b"normalized")
        assert service.dispatch(
            "validation.validate_import",
            {"name": work_name},
            (descriptor,),
        )["size_bytes"] == len(b"work")
        assert service.dispatch(
            "validation.revalidate_import",
            {"name": normalized_name},
            (descriptor,),
        )["size_bytes"] == len(b"normalized")
        assert service.dispatch(
            "validation.validate_materialization",
            {},
            (descriptor,),
        )["step_size_bytes"] == len(b"step")
    finally:
        os.close(descriptor)
        service.close()

    assert calls == [
        ("validate_import", Path(stage_name)),
        ("validate_import", Path(work_name)),
        ("revalidate", Path(normalized_name)),
        ("materialization", (Path("model.FCStd"), Path("model.step"))),
    ]


def test_parent_descriptor_bound_validation_returns_exact_evidence(
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "validation_idle")
    worker = FreeCadWorker(process)
    directory, stage_name = _validation_directory_at(tmp_path / "proxy-validation")
    work_name = ".work." + "a" * 32 + ".FCStd"
    work = directory / work_name
    work.write_bytes(b"work")
    work.chmod(0o600)
    normalized_name = ".normalized." + "d" * 32 + ".FCStd"
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        imported = worker.validate_import(
            directory_fd=descriptor,
            name=stage_name,
        )
        work_imported = worker.validate_import(
            directory_fd=descriptor,
            name=work_name,
        )
        revalidated = worker.revalidate_normalized_import(
            directory_fd=descriptor,
            name=normalized_name,
        )
        materialized = worker.validate_materialization(
            directory_fd=descriptor,
        )
        assert imported == revalidated
        assert imported.sha256 == hashlib.sha256(b"normalized").hexdigest()
        assert imported.size_bytes == len(b"normalized")
        assert work_imported.sha256 == hashlib.sha256(b"work").hexdigest()
        assert work_imported.size_bytes == len(b"work")
        assert materialized.fcstd_sha256 == hashlib.sha256(b"model").hexdigest()
        assert materialized.step_sha256 == hashlib.sha256(b"step").hexdigest()
        assert os.fstat(descriptor).st_ino == directory.stat().st_ino
        assert worker.state is WorkerGenerationState.READY
    finally:
        os.close(descriptor)
        worker.close()


@pytest.mark.parametrize("mode", ["validation_bad_claim", "validation_cross"])
def test_validation_claim_or_cross_file_mutation_fences_generation(
    mode: str,
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, mode)
    worker = FreeCadWorker(process)
    directory, stage_name = _validation_directory_at(tmp_path / mode)
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        with pytest.raises(WorkerError) as caught:
            worker.validate_import(
                directory_fd=descriptor,
                name=stage_name,
            )
        assert caught.value.code is WorkerErrorCode.GENERATION_LOST
        assert worker.state is WorkerGenerationState.DEAD
        assert os.fstat(descriptor).st_ino == directory.stat().st_ino
        if mode == "validation_cross":
            assert (directory / "model.step").read_bytes() == b"cross-mutation"
    finally:
        os.close(descriptor)


@pytest.mark.parametrize(
    "name",
    (
        "model.FCStd",
        "../.normalized." + "d" * 32 + ".FCStd",
        ".normalized." + "d" * 32 + ".fcstd",
        ".other." + "d" * 32 + ".FCStd",
    ),
)
def test_validation_name_capability_is_a_closed_relative_allowlist(
    name: str,
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "validation_idle")
    worker = FreeCadWorker(process)
    directory, _stage_name = _validation_directory_at(tmp_path / "closed-name")
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        with pytest.raises(WorkerError) as caught:
            worker.revalidate_normalized_import(
                directory_fd=descriptor,
                name=name,
            )
        assert caught.value.code is WorkerErrorCode.INVALID_INPUT
        assert worker.state is WorkerGenerationState.READY
    finally:
        os.close(descriptor)
        worker.close()


@pytest.mark.parametrize("has_model", [False, True])
def test_production_service_revision_sessions_are_read_only_and_exactly_bound(
    has_model: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.worker.service as service_module
    from vibecad.validation import ShapeObservation

    directory = tmp_path / f"revision-{has_model}"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    contents = {"manifest.json": b"manifest"}
    if has_model:
        contents.update(
            {
                "model.FCStd": b"fcstd",
                "model.step": b"step",
            }
        )
    for name, raw in contents.items():
        path = directory / name
        path.write_bytes(raw)
        path.chmod(0o600)
    files = [
        {
            "name": name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
        for name, raw in sorted(contents.items())
    ]
    calls: list[tuple[str, object]] = []

    class Engine:
        def create_empty(self, *, revision_id: str) -> object:
            calls.append(("create_empty", revision_id))
            return SimpleNamespace(doc=SimpleNamespace(Objects=[]))

        def load_fcstd(self, path: Path) -> object:
            calls.append(("load_fcstd", path))
            return SimpleNamespace(doc=SimpleNamespace(Objects=[object()]))

        def close(self, value: object) -> None:
            calls.append(("close", value))

    monkeypatch.setattr(
        service_module,
        "_shape_observation",
        lambda _session: ShapeObservation(
            target="body",
            volume_mm3=1,
            area_mm2=6,
            bbox_mm=(1, 1, 1),
            center_of_mass_mm=(0.5, 0.5, 0.5),
            valid_shape=True,
            solid_count=1,
        ),
    )
    monkeypatch.setattr(service_module, "_entity_observations", lambda _session: ())
    service = WorkerService(_GENERATION)
    monkeypatch.setattr(service, "_engine", Engine())
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    revision_id = "worker_revision_" + "a" * 32
    try:
        assert service.dispatch(
            "revision.bind",
            {
                "revision_id": revision_id,
                "project_id": _PROJECT_ID,
                "store_revision_id": _BASE_REVISION,
                "model_name": "model.FCStd" if has_model else None,
                "files": files,
            },
            (descriptor,),
        ) == {"revision_id": revision_id}
        session_id = service.dispatch(
            "session.load_revision",
            {"revision_id": revision_id},
            (),
        )["session_id"]
        observation = service.dispatch(
            "session.observe",
            {
                "session_id": session_id,
                "capability_kind": "revision",
                "capability_id": revision_id,
            },
            (),
        )
        assert observation["entities"] == []
        assert (observation["shape"] is None) is (not has_model)
        assert service.dispatch(
            "session.close",
            {"session_id": session_id},
            (),
        ) == {"session_id": session_id}
        assert service.dispatch(
            "revision.release",
            {"revision_id": revision_id},
            (),
        ) == {"revision_id": revision_id}
        with pytest.raises(OSError):
            os.fstat(descriptor)
        descriptor = -1
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        service.close()

    assert calls[0] == (
        ("load_fcstd", Path("model.FCStd")) if has_model else ("create_empty", _BASE_REVISION)
    )
    assert calls[-1][0] == "close"


@pytest.mark.parametrize(
    ("close_fails", "expected_code"),
    (
        (False, WorkerWireErrorCode.INTEGRITY_FAILURE),
        (True, WorkerWireErrorCode.INTERNAL_ERROR),
    ),
)
def test_revision_drift_after_load_requires_confirmed_session_cleanup(
    close_fails: bool,
    expected_code: WorkerWireErrorCode,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.worker.service as service_module

    directory = tmp_path / f"revision-load-drift-{close_fails}"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    contents = {
        "manifest.json": b"manifest",
        "model.FCStd": b"fcstd",
        "model.step": b"step",
    }
    for name, raw in contents.items():
        path = directory / name
        path.write_bytes(raw)
        path.chmod(0o600)
    files = [
        {
            "name": name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
        for name, raw in sorted(contents.items())
    ]
    close_calls = 0

    class Engine:
        def load_fcstd(self, path: Path) -> object:
            assert path == Path("model.FCStd")
            model = directory / "model.FCStd"
            model.write_bytes(b"drifted-after-load")
            model.chmod(0o600)
            return object()

        def close(self, value: object) -> None:
            nonlocal close_calls
            del value
            close_calls += 1
            if close_fails:
                raise RuntimeError("unconfirmed close")

    service = WorkerService(_GENERATION)
    monkeypatch.setattr(service, "_engine", Engine())
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    revision_id = "worker_revision_" + "b" * 32
    try:
        assert service.dispatch(
            "revision.bind",
            {
                "revision_id": revision_id,
                "project_id": _PROJECT_ID,
                "store_revision_id": _BASE_REVISION,
                "model_name": "model.FCStd",
                "files": files,
            },
            (descriptor,),
        ) == {"revision_id": revision_id}
        descriptor = -1
        with pytest.raises(service_module._ServiceError) as caught:  # noqa: SLF001
            service.dispatch(
                "session.load_revision",
                {"revision_id": revision_id},
                (),
            )
        assert caught.value.code is expected_code
        assert service._sessions == {}  # noqa: SLF001
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        service.close()

    assert close_calls == 1


def test_parent_revision_observe_lifecycle_uses_opaque_capabilities(
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "revision_observe")
    worker = FreeCadWorker(process)
    with _candidate_rig(tmp_path, suffix="revision-observe-store") as rig:
        (rig.directory / "model.FCStd").write_bytes(b"fcstd")
        (rig.directory / "model.step").write_bytes(b"step")
        revision = rig.store.seal_revision(
            rig.head.project_id,
            rig.revision_id,
            rig.lease,
        )
        handle = worker.bind_revision(store=rig.store, revision=revision)
        session = worker.load_revision(handle)
        shape, entities = worker.observe(session=session, capability=handle)
        assert shape is not None
        assert shape.volume_mm3 == 1
        assert entities == ()
        with pytest.raises(WorkerError) as still_bound:
            worker.release_revision(handle)
        assert still_bound.value.code is WorkerErrorCode.INVALID_HANDLE
        worker.close_session(session)
        worker.release_revision(handle)
        assert worker._revisions == {}
        worker.close()


def test_malformed_observation_fences_the_entire_generation(
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "observe_bad")
    worker = FreeCadWorker(process)
    with _candidate_rig(tmp_path, suffix="observe-bad-store") as rig:
        revision = rig.store.load_revision(
            rig.head.project_id,
            rig.head.revision_id,
        )
        handle = worker.bind_revision(store=rig.store, revision=revision)
        session = worker.load_revision(handle)
        descriptors = (
            worker._revisions[handle].revisions_fd,
            worker._revisions[handle].directory_fd,
        )
        with pytest.raises(WorkerError) as caught:
            worker.observe(session=session, capability=handle)
        assert caught.value.code is WorkerErrorCode.GENERATION_LOST
        assert worker.state is WorkerGenerationState.DEAD
        assert worker._revisions == {}
        assert worker._sessions == {}
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
        with pytest.raises(WorkerError) as stale:
            worker.close_session(session)
        assert stale.value.code is WorkerErrorCode.GENERATION_LOST


def test_revision_sessions_cannot_cross_into_candidate_write_operations(
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "revision_observe")
    worker = FreeCadWorker(process)
    with _candidate_rig(tmp_path, suffix="cross-kind-store") as rig:
        baseline = rig.store.load_revision(
            rig.head.project_id,
            rig.head.revision_id,
        )
        revision = worker.bind_revision(store=rig.store, revision=baseline)
        revision_session = worker.load_revision(revision)
        candidate = worker.bind_candidate(
            store=rig.store,
            lease=rig.lease,
            base_head=rig.head,
            revision_id=rig.revision_id,
        )
        candidate_session = worker.create_empty(candidate)
        with pytest.raises(WorkerError) as observed:
            worker.observe(
                session=revision_session,
                capability=candidate,
            )
        assert observed.value.code is WorkerErrorCode.INVALID_HANDLE
        with pytest.raises(WorkerError) as executed:
            worker.execute_program(
                program=_inspect_program(base_revision=rig.head.revision_id),
                candidate=candidate,
                session=revision_session,
            )
        assert executed.value.code is WorkerErrorCode.INVALID_HANDLE
        with pytest.raises(WorkerError) as checkpointed:
            worker.checkpoint(
                session=revision_session,
                candidate=candidate,
            )
        assert checkpointed.value.code is WorkerErrorCode.INVALID_HANDLE
        assert worker.state is WorkerGenerationState.READY
        worker.close_session(candidate_session)
        worker.release_candidate(candidate)
        worker.close_session(revision_session)
        worker.release_revision(revision)
        worker.close()


def test_revision_store_drift_revokes_operations_without_losing_cleanup(
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "revision_observe")
    worker = FreeCadWorker(process)
    with _candidate_rig(tmp_path, suffix="revision-drift-store") as rig:
        (rig.directory / "model.FCStd").write_bytes(b"fcstd")
        (rig.directory / "model.step").write_bytes(b"step")
        revision = rig.store.seal_revision(
            rig.head.project_id,
            rig.revision_id,
            rig.lease,
        )
        handle = worker.bind_revision(store=rig.store, revision=revision)
        state = worker._revisions[handle]
        descriptor = state.directory_fd
        target_fd = os.open(
            "model.FCStd",
            os.O_WRONLY | os.O_TRUNC,
            dir_fd=descriptor,
        )
        try:
            os.write(target_fd, b"drift")
        finally:
            os.close(target_fd)
        with pytest.raises(WorkerError) as caught:
            worker.load_revision(handle)
        assert caught.value.code is WorkerErrorCode.INTEGRITY_FAILURE
        assert worker.state is WorkerGenerationState.READY
        worker.release_revision(handle)
        worker.close()


_MANAGED_FAULT_CHILD = r"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

window, mode, marker_raw, arm_raw = sys.argv[1:5]
protocol_index = sys.argv.index("--protocol-fd")
generation_index = sys.argv.index("--generation-id")
protocol_fd = sys.argv[protocol_index + 1]
generation_id = sys.argv[generation_index + 1]
marker = Path(marker_raw)
arm = Path(arm_raw)

from vibecad.worker import service as service_module

original_dispatch = service_module.WorkerService.dispatch
program_operations = {}
ready_version = None
arm_seen = False
post_export = False


def persist_marker(method):
    payload = {
        "freecad_version": ready_version,
        "generation_id": generation_id,
        "method": method,
        "mode": mode,
        "pgid": os.getpgrp(),
        "pid": os.getpid(),
        "window": window,
    }
    temporary = marker.with_name(marker.name + ".tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        if os.write(descriptor, raw) != len(raw):
            raise OSError("short marker write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, marker)
    parent = os.open(
        marker.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def fault(method):
    if mode == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    persist_marker(method)
    if mode == "crash":
        os._exit(86)
    while True:
        time.sleep(60)


def dispatch(self, method, params, descriptors):
    global arm_seen, post_export, ready_version
    armed = arm.is_file()
    if armed and not arm_seen:
        arm_seen = True
        post_export = False
    result = original_dispatch(self, method, params, descriptors)
    if method == "worker.ready":
        ready_version = result["freecad_version"]
    if method == "program.begin":
        program_id = result["program_id"]
        program_operations[program_id] = tuple(
            item["op"] for item in params["program"]["operations"]
        )
    selected = False
    if armed and window == "load" and method == "session.load_revision":
        selected = True
    elif armed and window == "mutation" and method == "program.execute_command":
        operations = program_operations.get(params["program_id"], ())
        index = params["index"]
        selected = index < len(operations) and operations[index] == "modify_parameter"
    elif armed and window == "checkpoint" and method == "session.checkpoint_fcstd":
        selected = True
    elif armed and window == "export" and method == "session.export_step":
        selected = True
    elif armed and method == "session.export_step":
        post_export = True
    elif (
        armed
        and window == "evidence"
        and post_export
        and method == "session.observe"
    ):
        selected = True
    if selected:
        fault(method)
    return result


service_module.WorkerService.dispatch = dispatch
sys.argv = [
    sys.argv[0],
    "--protocol-fd",
    protocol_fd,
    "--generation-id",
    generation_id,
]
from vibecad.worker.__main__ import main

raise SystemExit(main())
"""


def _managed_fault_program(
    *,
    task_id: str,
    base_revision: str,
    inspect_only: bool = False,
) -> ModelProgram:
    acceptance = AcceptanceSpec(
        id="managed-fault-matrix",
        criteria=(
            AcceptanceCriterion(
                id="valid-shape",
                kind=AcceptanceKind.TOPOLOGY,
                check="valid_shape",
                target="body",
                expected=True,
            ),
        ),
    )
    if inspect_only:
        operations = (
            ModelCommand(
                id="inspect",
                op="inspect_model",
                target={},
                args={},
                depends_on=(),
                preserve=(),
                source=ValueSource.MODEL,
            ),
        )
    else:
        operations = (
            ModelCommand(
                id="fault-box",
                op="create_box",
                target={},
                args={
                    "length_mm": 4,
                    "width_mm": 5,
                    "height_mm": 6,
                    "position_mm": (20, 0, 0),
                },
                depends_on=(),
                preserve=(),
                source=ValueSource.MODEL,
            ),
            ModelCommand(
                id="fault-modify",
                op="modify_parameter",
                target={"object": {"command_id": "fault-box", "slot": "object"}},
                args={"parameter": "length", "value_mm": 7},
                depends_on=("fault-box",),
                preserve=(),
                source=ValueSource.MODEL,
            ),
        )
    return ModelProgram(
        task_id=task_id,
        base_revision=base_revision,
        operations=operations,
        acceptance=acceptance,
    )


def _managed_setup_program(*, task_id: str, base_revision: str) -> ModelProgram:
    return ModelProgram(
        task_id=task_id,
        base_revision=base_revision,
        operations=(
            ModelCommand(
                id="baseline-box",
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
        ),
        acceptance=AcceptanceSpec(
            id="managed-fault-baseline",
            criteria=(
                AcceptanceCriterion(
                    id="valid-shape",
                    kind=AcceptanceKind.TOPOLOGY,
                    check="valid_shape",
                    target="body",
                    expected=True,
                ),
            ),
        ),
    )


def _managed_tree_snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        observed = path.lstat()
        raw = path.read_bytes() if path.is_file() else None
        snapshot[relative] = (
            observed.st_dev,
            observed.st_ino,
            observed.st_mode,
            observed.st_size,
            observed.st_mtime_ns,
            raw,
        )
    return snapshot


def _managed_path_snapshot(path: Path) -> tuple[object, ...]:
    observed = path.lstat()
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_size,
        observed.st_mtime_ns,
        path.read_bytes(),
    )


def _managed_application_call(client, operation: str, request: dict[str, object]):
    response = client.call(
        "application.call",
        {"operation": operation, "request": request},
    )
    assert response.error is None
    assert type(response.result) is dict
    return response.result


def _managed_success(client, operation: str, request: dict[str, object]):
    envelope = _managed_application_call(client, operation, request)
    assert envelope["ok"] is True, envelope
    assert envelope["error"] is None
    assert type(envelope["result"]) is dict
    return envelope["result"]


def _managed_create_task(client, *, project_id: str, key_hex: str):
    return _managed_success(
        client,
        "create_task",
        {
            "schema_version": 1,
            "create_key": "task_create_" + key_hex,
            "project_id": project_id,
            "review_policy": "auto_commit",
        },
    )


def _managed_submit_request(created: dict[str, object], program: ModelProgram):
    task = created["task_run"]
    assert type(task) is dict
    return {
        "schema_version": 1,
        "task_id": task["id"],
        "expected_generation": created["generation"],
        "program_json": json.dumps(
            program.to_mapping(),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    }


def _wait_process_group_gone(pgid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.01)
    return False


@pytest.mark.slow
@pytest.mark.skipif(sys.platform != "darwin", reason="local kernel process identity is macOS-only")
@pytest.mark.parametrize(
    ("window", "mode"),
    tuple(
        (window, mode)
        for window in ("load", "mutation", "checkpoint", "export", "evidence")
        for mode in ("hang", "crash")
    ),
)
def test_real_managed_daemon_fault_matrix_recovers_without_source_corruption(
    window: str,
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("managed integration was not requested")
    python_raw = os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON")
    if not python_raw:
        pytest.skip("managed FreeCAD Python was not requested")
    python = Path(python_raw).expanduser().resolve(strict=False)
    if not python.is_file():
        pytest.skip("managed FreeCAD Python is unavailable")

    import vibecad.worker.generation as generation_module
    from vibecad.application.agent import AgentApplication
    from vibecad.daemon import LocalKernelClient, LocalKernelDaemon, LocalKernelState
    from vibecad.execution.worker_port import WorkerCadExecutionPort
    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime.status import (
        capture_runtime_generation_evidence,
        engine_compatible_generation,
    )
    from vibecad.workflow.state import TaskEvent, TaskStatus

    runtime_evidence = capture_runtime_generation_evidence(runtime_paths.active_runtime_prefix())
    assert engine_compatible_generation(runtime_evidence)
    assert runtime_evidence.python.resolve() == python

    case_number = (
        ("load", "mutation", "checkpoint", "export", "evidence").index(window) * 2
        + ("hang", "crash").index(mode)
        + 1
    )
    fault_script = tmp_path / "managed_fault_worker.py"
    fault_script.write_text(_MANAGED_FAULT_CHILD, encoding="utf-8")
    fault_script.chmod(0o600)
    arm = tmp_path / "fault.arm"
    marker = tmp_path / "fault.json"
    workers: list[FreeCadWorker] = []
    ports: list[WorkerCadExecutionPort] = []
    ready_results: list[dict[str, object]] = []
    killpg_calls: list[tuple[int, int]] = []
    original_rpc = _WorkerProcess._rpc
    original_killpg = os.killpg

    def recording_killpg(pgid: int, signum: int) -> None:
        killpg_calls.append((pgid, signum))
        original_killpg(pgid, signum)

    def recording_rpc(
        self: _WorkerProcess,
        method: str,
        params: dict[str, object],
        *,
        timeout_ms: int,
        capability_fd: int | None = None,
        allow_starting: bool = False,
    ) -> dict[str, object]:
        result = original_rpc(
            self,
            method,
            params,
            timeout_ms=timeout_ms,
            capability_fd=capability_fd,
            allow_starting=allow_starting,
        )
        if method == "worker.ready":
            ready_results.append(dict(result))
        return result

    monkeypatch.setattr(_WorkerProcess, "_rpc", recording_rpc)
    monkeypatch.setattr(generation_module.os, "killpg", recording_killpg)

    def start_worker(*, source_root: Path) -> FreeCadWorker:
        assert source_root == Path(__file__).parents[1] / "src"
        if workers:
            if len(workers) != 1:
                raise AssertionError("unexpected third Worker generation")
            worker = FreeCadWorker.start_managed(source_root=source_root)
        else:
            process = _WorkerProcess._spawn(  # noqa: SLF001
                command=(
                    str(python),
                    "-B",
                    str(fault_script),
                    window,
                    mode,
                    str(marker),
                    str(arm),
                ),
                source_root=source_root,
                readiness_timeout_ms=15_000,
                shutdown_timeout_ms=750,
                test_timeout_cap_ms=5_000,
            )
            worker = FreeCadWorker(process)
        workers.append(worker)
        return worker

    def build_port(*, revision_store: LocalRevisionStore) -> WorkerCadExecutionPort:
        port = WorkerCadExecutionPort(
            store=revision_store,
            worker_factory=start_worker,
        )
        ports.append(port)
        return port

    applications: list[AgentApplication] = []

    def application_factory(*, layout, lease_manager):
        application = AgentApplication.from_captured_layout(
            layout=layout,
            lease_manager=lease_manager,
            cad_port_factory=build_port,
        )
        applications.append(application)
        return application

    daemon = None
    submit_client = None
    control_client = None
    recovery_client = None
    submitter = None
    submission_results: list[dict[str, object]] = []
    submission_errors: list[BaseException] = []
    healthy_pid = None
    healthy_home = None
    retained_audit_root = None
    retained_audit_snapshot = None
    short_root = Path(tempfile.mkdtemp(prefix="vc-m03-", dir="/private/tmp"))
    short_root.chmod(0o700)
    try:
        daemon = LocalKernelDaemon.start(
            data_root=short_root / "data",
            application_factory=application_factory,
        )
        submit_client = LocalKernelClient.connect(daemon.run_root)
        control_client = LocalKernelClient.connect(daemon.run_root)
        daemon_id = daemon.daemon_id
        assert submit_client.daemon_id == control_client.daemon_id == daemon_id
        initial_ping = control_client.call("kernel.ping", {})
        assert initial_ping.error is None
        assert initial_ping.result["daemon_id"] == daemon_id

        project = _managed_success(
            control_client,
            "create_project",
            {
                "schema_version": 1,
                "create_key": "project_create_" + f"{case_number:032x}",
                "kind": "empty",
            },
        )
        project_id = project["project_id"]
        setup = _managed_create_task(
            control_client,
            project_id=project_id,
            key_hex=f"{case_number + 32:032x}",
        )
        setup_task = setup["task_run"]
        setup_program = _managed_setup_program(
            task_id=setup_task["id"],
            base_revision=setup_task["base_revision"],
        )
        setup_terminal = _managed_success(
            control_client,
            "submit_model_program",
            _managed_submit_request(setup, setup_program),
        )
        assert setup_terminal["task_run"]["status"] == TaskStatus.SUCCEEDED.value
        assert len(workers) == len(ports) == len(ready_results) == 1
        assert ready_results[0]["freecad_version"] == "1.1.0"

        application = applications[0]
        if window == "load":
            with application._cad_gate:  # noqa: SLF001
                with application._generation_lock:  # noqa: SLF001
                    runtime = application._runtimes.pop(project_id)  # noqa: SLF001
                assert runtime.close() is True
        baseline_head = application._revision_store.load_head(project_id)  # noqa: SLF001
        assert baseline_head.generation == 1
        baseline_revision = application._revision_store.load_revision(  # noqa: SLF001
            project_id,
            baseline_head.revision_id,
        )
        assert baseline_revision.model is not None
        assert len(baseline_revision.artifacts) == 1
        project_heads = tuple(application._layout.projects.rglob("HEAD.json"))  # noqa: SLF001
        assert len(project_heads) == 1
        head_path = project_heads[0]
        project_root = head_path.parent
        revision_root = project_root / "revisions"
        immutable_source_revision_root = application._revision_store.revision_model_path(  # noqa: SLF001
            project_id,
            baseline_head.revision_id,
        ).parent
        head_before = _managed_path_snapshot(head_path)
        immutable_source_before = _managed_tree_snapshot(immutable_source_revision_root)
        revision_entries_before = tuple(sorted(path.name for path in revision_root.iterdir()))
        temporary_before = tuple(
            sorted(
                path.relative_to(application._layout.projects).as_posix()  # noqa: SLF001
                for path in application._layout.projects.rglob(".*")  # noqa: SLF001
            )
        )

        fault = _managed_create_task(
            control_client,
            project_id=project_id,
            key_hex=f"{case_number + 64:032x}",
        )
        fault_task = fault["task_run"]
        fault_program = _managed_fault_program(
            task_id=fault_task["id"],
            base_revision=fault_task["base_revision"],
        )
        fault_request = _managed_submit_request(fault, fault_program)
        fault_worker = workers[0]
        fault_pid = fault_worker.pid
        fault_generation = fault_worker.generation_id
        fault_home = fault_worker._process._home  # noqa: SLF001
        assert os.getpid() != fault_pid
        assert os.getpgid(fault_pid) == os.getsid(fault_pid) == fault_pid
        arm.write_bytes(b"armed\n")
        arm.chmod(0o600)

        if mode == "hang":

            def submit_fault() -> None:
                try:
                    submission_results.append(
                        _managed_application_call(
                            submit_client,
                            "submit_model_program",
                            fault_request,
                        )
                    )
                except BaseException as error:
                    submission_errors.append(error)

            submitter = threading.Thread(
                target=submit_fault,
                name=f"managed-{window}-hang-submit",
            )
            submitter.start()
            deadline = time.monotonic() + 20
            while not marker.is_file() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.is_file()
            active = _managed_success(
                control_client,
                "get_task",
                {"schema_version": 1, "task_id": fault_task["id"]},
            )
            request_event = (
                TaskEvent.REQUEST_ACTIVE_CANCEL if window == "load" else TaskEvent.REQUEST_CANCEL
            )
            cancel_request = {
                "schema_version": 1,
                "task_id": fault_task["id"],
                "expected_generation": active["generation"],
            }
            original_cancel_request = dict(cancel_request)
            cancel_envelope = _managed_application_call(
                control_client,
                "cancel_task",
                cancel_request,
            )
            if cancel_envelope["ok"] is False:
                assert cancel_envelope["error"]["code"] == "recovery_required"
                pending_cancel = _managed_success(
                    control_client,
                    "get_task",
                    {"schema_version": 1, "task_id": fault_task["id"]},
                )
                assert pending_cancel["task_run"]["status"] in {
                    TaskStatus.CANCEL_REQUESTED.value,
                    TaskStatus.CANCELLING.value,
                }
                assert [
                    item["event"]
                    for item in pending_cancel["task_run"]["transitions"]
                    if item["event"]
                    in {
                        TaskEvent.REQUEST_CANCEL.value,
                        TaskEvent.REQUEST_ACTIVE_CANCEL.value,
                    }
                ] == [request_event.value]
                assert len(workers) == 1
                assert workers[0] is fault_worker
                assert workers[0].generation_id == fault_generation
                if pending_cancel["task_run"]["status"] == TaskStatus.CANCEL_REQUESTED.value:
                    assert application._cad_execution_port is ports[0]  # noqa: SLF001
                    assert ports[0]._worker is fault_worker  # noqa: SLF001
                else:
                    assert (  # noqa: SLF001
                        application._cad_execution_port is None
                        or application._cad_execution_port is ports[0]
                    )
                    assert (  # noqa: SLF001
                        ports[0]._worker is None or ports[0]._worker is fault_worker
                    )
                assert cancel_request == original_cancel_request
                cancel_envelope = _managed_application_call(
                    control_client,
                    "cancel_task",
                    cancel_request,
                )
            assert cancel_envelope["ok"] is True, cancel_envelope
            cancelled = cancel_envelope["result"]
            assert type(cancelled) is dict
            submitter.join(timeout=15)
            assert not submitter.is_alive()
            assert submission_errors == []
            assert len(submission_results) == 1
            assert submission_results[0]["ok"] is True
            assert cancelled["task_run"]["status"] == TaskStatus.CANCELLED.value
            cancellation_events = [
                item["event"]
                for item in cancelled["task_run"]["transitions"]
                if item["event"]
                in {
                    TaskEvent.REQUEST_CANCEL.value,
                    TaskEvent.REQUEST_ACTIVE_CANCEL.value,
                    TaskEvent.START_CANCELLATION.value,
                    TaskEvent.CONFIRM_CANCELLED.value,
                }
            ]
            assert cancellation_events == [
                request_event.value,
                TaskEvent.START_CANCELLATION.value,
                TaskEvent.CONFIRM_CANCELLED.value,
            ]
            fault_terminal = cancelled
        else:
            crash_result = _managed_application_call(
                submit_client,
                "submit_model_program",
                fault_request,
            )
            submission_results.append(crash_result)
            assert marker.is_file()
            observed_fault = _managed_success(
                control_client,
                "get_task",
                {"schema_version": 1, "task_id": fault_task["id"]},
            )
            if window == "load":
                assert crash_result["ok"] is False
                assert crash_result["error"]["code"] == "recovery_required"
                assert observed_fault["task_run"]["status"] == TaskStatus.NEEDS_PLAN.value
            else:
                assert crash_result["ok"] is True
                assert observed_fault["task_run"]["status"] == TaskStatus.FAILED.value
                failure_events = [
                    item["event"] for item in observed_fault["task_run"]["transitions"]
                ]
                assert failure_events[-2:] == [
                    TaskEvent.FAIL_EXECUTION.value,
                    TaskEvent.COMPLETE_ROLLBACK.value,
                ]
            fault_terminal = observed_fault

        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        assert marker_payload == {
            "freecad_version": "1.1.0",
            "generation_id": fault_generation,
            "method": {
                "load": "session.load_revision",
                "mutation": "program.execute_command",
                "checkpoint": "session.checkpoint_fcstd",
                "export": "session.export_step",
                "evidence": "session.observe",
            }[window],
            "mode": mode,
            "pgid": fault_pid,
            "pid": fault_pid,
            "window": window,
        }
        assert _wait_gone(fault_pid, timeout=5)
        assert _wait_process_group_gone(fault_pid)
        assert fault_worker.state is WorkerGenerationState.DEAD
        assert not fault_home.exists()
        if mode == "hang":
            assert (fault_pid, signal.SIGTERM) in killpg_calls
            assert (fault_pid, signal.SIGKILL) in killpg_calls
        assert application._cad_execution_port is None  # noqa: SLF001
        assert application._runtimes == {}  # noqa: SLF001
        assert application._cad_fence_required is False  # noqa: SLF001
        assert application._revision_store.load_head(project_id) == baseline_head  # noqa: SLF001
        assert (
            application._revision_store.load_revision(  # noqa: SLF001
                project_id,
                baseline_head.revision_id,
            )
            == baseline_revision
        )
        assert _managed_path_snapshot(head_path) == head_before
        assert _managed_tree_snapshot(immutable_source_revision_root) == immutable_source_before
        revision_entries_after = tuple(sorted(path.name for path in revision_root.iterdir()))
        if window == "evidence":
            # P0B-RES-06 deliberately defers cross-store retention/GC.  Once
            # sealing completed, the immutable detached revision remains owned
            # by the durable terminal TaskRun; it is not an unreferenced orphan.
            retained_revision_id = fault_terminal["task_run"]["candidate_revision"]
            assert type(retained_revision_id) is str
            retained_revision = application._revision_store.load_revision(  # noqa: SLF001
                project_id,
                retained_revision_id,
            )
            assert retained_revision.id == retained_revision_id
            assert retained_revision.project_id == project_id
            assert retained_revision.base_revision == baseline_head.revision_id
            assert retained_revision.id != baseline_head.revision_id
            assert retained_revision.model is not None
            assert retained_revision.model.size_bytes > 0
            assert len(retained_revision.artifacts) == 1
            assert retained_revision.artifacts[0].format == "step"
            assert retained_revision.artifacts[0].size_bytes > 0
            retained_audit_root = application._revision_store.revision_model_path(  # noqa: SLF001
                project_id,
                retained_revision_id,
            ).parent
            assert (
                hashlib.sha256((retained_audit_root / "manifest.json").read_bytes()).hexdigest()
                == retained_revision.manifest_sha256
            )
            assert (
                hashlib.sha256(
                    (retained_audit_root / retained_revision.model.name).read_bytes()
                ).hexdigest()
                == retained_revision.model.sha256
            )
            retained_step = application._revision_store.revision_artifact_path(  # noqa: SLF001
                project_id,
                retained_revision_id,
                retained_revision.artifacts[0].id,
            )
            assert hashlib.sha256(retained_step.read_bytes()).hexdigest() == (
                retained_revision.artifacts[0].sha256
            )
            retained_audit_snapshot = _managed_tree_snapshot(retained_audit_root)
            extra_revisions = set(revision_entries_after) - set(revision_entries_before)
            assert set(revision_entries_before) < set(revision_entries_after)
            assert extra_revisions == {retained_audit_root.name}
        else:
            assert revision_entries_after == revision_entries_before
        temporary_after = tuple(
            sorted(
                path.relative_to(application._layout.projects).as_posix()  # noqa: SLF001
                for path in application._layout.projects.rglob(".*")  # noqa: SLF001
            )
        )
        assert temporary_after == temporary_before
        candidate_roots = tuple(application._layout.projects.rglob("candidates"))  # noqa: SLF001
        reservation_roots = tuple(
            application._layout.projects.rglob("reservations")  # noqa: SLF001
        )
        assert candidate_roots
        assert reservation_roots
        assert all(tuple(path.iterdir()) == () for path in candidate_roots)
        assert all(tuple(path.iterdir()) == () for path in reservation_roots)
        assert daemon.state is LocalKernelState.RUNNING
        after_fault_ping = control_client.call("kernel.ping", {})
        assert after_fault_ping.error is None
        assert after_fault_ping.result["daemon_id"] == daemon_id
        recovery_client = LocalKernelClient.connect(daemon.run_root)
        recovery_ping = recovery_client.call("kernel.ping", {})
        assert recovery_ping.error is None
        assert recovery_ping.result["daemon_id"] == daemon_id
        durable_fault = _managed_success(
            recovery_client,
            "get_task",
            {"schema_version": 1, "task_id": fault_task["id"]},
        )
        if mode == "hang":
            assert durable_fault["task_run"]["status"] == TaskStatus.CANCELLED.value
        elif window == "load":
            assert durable_fault["task_run"]["status"] == TaskStatus.NEEDS_PLAN.value
        else:
            assert durable_fault["task_run"]["status"] == TaskStatus.FAILED.value
        if window == "evidence":
            assert (
                durable_fault["task_run"]["candidate_revision"]
                == fault_terminal["task_run"]["candidate_revision"]
            )

        healthy = _managed_create_task(
            recovery_client,
            project_id=project_id,
            key_hex=f"{case_number + 96:032x}",
        )
        healthy_task = healthy["task_run"]
        healthy_program = _managed_fault_program(
            task_id=healthy_task["id"],
            base_revision=healthy_task["base_revision"],
            inspect_only=True,
        )
        healthy_terminal = _managed_success(
            recovery_client,
            "submit_model_program",
            _managed_submit_request(healthy, healthy_program),
        )
        assert healthy_terminal["task_run"]["status"] == TaskStatus.SUCCEEDED.value
        assert len(workers) == len(ports) == len(ready_results) == 2
        assert all(item["freecad_version"] == "1.1.0" for item in ready_results)
        healthy_worker = workers[1]
        healthy_pid = healthy_worker.pid
        healthy_home = healthy_worker._process._home  # noqa: SLF001
        assert healthy_pid != fault_pid
        assert healthy_worker.generation_id != fault_generation
        assert os.getpgid(healthy_pid) == os.getsid(healthy_pid) == healthy_pid

        final_head = application._revision_store.load_head(project_id)  # noqa: SLF001
        assert final_head.generation == baseline_head.generation + 1
        assert final_head.revision_id == healthy_terminal["task_run"]["committed_revision"]
        final_revision = application._revision_store.load_revision(  # noqa: SLF001
            project_id,
            final_head.revision_id,
        )
        assert final_revision.model is not None
        assert final_revision.model.size_bytes > 0
        assert len(final_revision.artifacts) == 1
        assert final_revision.artifacts[0].format == "step"
        assert final_revision.artifacts[0].size_bytes > 0
        final_directory = application._revision_store.revision_model_path(  # noqa: SLF001
            project_id,
            final_head.revision_id,
        ).parent
        validation_fd = os.open(
            final_directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            materialized = healthy_worker.validate_materialization(
                directory_fd=validation_fd,
            )
        finally:
            os.close(validation_fd)
        assert materialized.fcstd_size_bytes == final_revision.model.size_bytes
        assert materialized.step_size_bytes == final_revision.artifacts[0].size_bytes
        if retained_audit_root is not None:
            assert retained_audit_snapshot is not None
            assert _managed_tree_snapshot(retained_audit_root) == retained_audit_snapshot

        final_ping = recovery_client.call("kernel.ping", {})
        assert final_ping.error is None
        assert final_ping.result["daemon_id"] == daemon_id
    finally:
        if submitter is not None and submitter.is_alive():
            if submit_client is not None:
                submit_client.close()
                submit_client = None
            for worker in workers:
                with contextlib.suppress(Exception):
                    worker.terminate()
            if daemon is not None:
                with contextlib.suppress(Exception):
                    daemon.close()
                daemon = None
            submitter.join(timeout=5)
        if submit_client is not None:
            submit_client.close()
        if control_client is not None:
            control_client.close()
        if recovery_client is not None:
            recovery_client.close()
        if daemon is not None:
            with contextlib.suppress(Exception):
                daemon.close()
        for worker in workers:
            with contextlib.suppress(Exception):
                worker.terminate()
        if healthy_pid is not None:
            assert _wait_gone(healthy_pid, timeout=5)
            assert _wait_process_group_gone(healthy_pid)
        if healthy_home is not None:
            assert not healthy_home.exists()
        shutil.rmtree(short_root, ignore_errors=True)


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
            baseline = rig.store.load_revision(
                rig.head.project_id,
                rig.head.revision_id,
            )
            baseline_handle = worker.bind_revision(
                store=rig.store,
                revision=baseline,
            )
            baseline_session = worker.load_revision(baseline_handle)
            baseline_shape, baseline_entities = worker.observe(
                session=baseline_session,
                capability=baseline_handle,
            )
            assert baseline_shape is None
            assert baseline_entities == ()
            worker.close_session(baseline_session)
            worker.release_revision(baseline_handle)

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
            observed_shape, observed_entities = worker.observe(
                session=loaded,
                capability=candidate,
            )
            assert observed_shape is not None
            assert observed_shape.volume_mm3 == pytest.approx(9_000)
            assert observed_entities
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

            validation_directory = tmp_path / "real-validation"
            validation_directory.mkdir(mode=0o700)
            validation_directory.chmod(0o700)
            stage_name = ".stage." + "d" * 32 + ".FCStd"
            normalized_name = ".normalized." + "e" * 32 + ".FCStd"
            for name, source in (
                (stage_name, rig.directory / "model.FCStd"),
                (normalized_name, rig.directory / "model.FCStd"),
                ("model.FCStd", rig.directory / "model.FCStd"),
                ("model.step", rig.directory / "model.step"),
            ):
                target = validation_directory / name
                target.write_bytes(source.read_bytes())
                target.chmod(0o600)
            validation_fd = os.open(
                validation_directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                imported = worker.validate_import(
                    directory_fd=validation_fd,
                    name=stage_name,
                )
                normalized_path = validation_directory / normalized_name
                normalized_path.write_bytes((validation_directory / stage_name).read_bytes())
                normalized_path.chmod(0o600)
                revalidated = worker.revalidate_normalized_import(
                    directory_fd=validation_fd,
                    name=normalized_name,
                )
                materialized = worker.validate_materialization(
                    directory_fd=validation_fd,
                )
            finally:
                os.close(validation_fd)
            assert imported == revalidated
            assert materialized.fcstd_size_bytes > 0
            assert materialized.step_size_bytes > 0

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
            sealed = rig.store.seal_revision(
                rig.head.project_id,
                rig.revision_id,
                rig.lease,
            )
            revision_handle = worker.bind_revision(
                store=rig.store,
                revision=sealed,
            )
            revision_session = worker.load_revision(revision_handle)
            revision_shape, revision_entities = worker.observe(
                session=revision_session,
                capability=revision_handle,
            )
            assert revision_shape is not None
            assert revision_shape.volume_mm3 == pytest.approx(9_000)
            assert revision_entities
            worker.close_session(revision_session)
            worker.release_revision(revision_handle)
        finally:
            if candidate is not None and worker.state is WorkerGenerationState.READY:
                with contextlib.suppress(WorkerError):
                    worker.release_candidate(candidate)
            worker.close()
