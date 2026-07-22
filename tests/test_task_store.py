"""Atomic, fail-closed TaskRun persistence tests."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import stat
import subprocess
import sys
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import vibecad.workflow.state as state_module
import vibecad.workflow.store as store_module
from vibecad.workflow.contracts import (
    AcceptanceSpec,
    ErrorCategory,
    ModelCommand,
    ModelProgram,
    StepError,
    ValueSource,
)
from vibecad.workflow.lease import (
    LeaseError,
    LeaseErrorCode,
    LeaseRootTrust,
    ResourceLease,
    ResourceLeaseManager,
)
from vibecad.workflow.state import (
    ReasoningOwner,
    ReviewPolicy,
    TaskEvent,
    TaskRun,
    new_task_run,
    transition_task,
)
from vibecad.workflow.store import (
    StoredTaskRun,
    TaskRunStore,
    TaskStoreError,
    TaskStoreErrorCode,
    TaskStoreRootTrust,
)

TASK_ID = "task_0123456789abcdef0123456789abcdef"
OTHER_TASK_ID = "task_11111111111111111111111111111111"
PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
BASE_REVISION = "revision_0123456789abcdef0123456789abcdef"
KEY_DOMAIN = b"vibecad-task-store-key-v1\0"
CHECKSUM_DOMAIN = b"vibecad-stored-task-run-v1\0"
MAX_RECORD_BYTES = 2 * 1024 * 1024
MUTATION_JOURNAL_NAME = ".mutation.json"


class IdString(str):
    pass


class GenerationInteger(int):
    pass


class TaskRunSubclass(TaskRun):
    __slots__ = ()

    def to_mapping(self):
        raise AssertionError("TaskRun subclass serialization must not execute")


def _task(task_id: str = TASK_ID) -> TaskRun:
    return new_task_run(
        task_id=task_id,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )


def _task_with_unicode(text: str) -> TaskRun:
    task = transition_task(_task(), TaskEvent.REQUEST_PLAN)
    program = ModelProgram(
        task_id=TASK_ID,
        base_revision=BASE_REVISION,
        operations=(),
        acceptance=AcceptanceSpec(id=text, criteria=()),
    )
    return transition_task(task, TaskEvent.SUBMIT_PROGRAM, program=program)


def _task_with_float() -> TaskRun:
    task = transition_task(_task(), TaskEvent.REQUEST_PLAN)
    program = ModelProgram(
        task_id=TASK_ID,
        base_revision=BASE_REVISION,
        operations=(
            ModelCommand(
                id="inspect-float",
                op="inspect_model",
                target={},
                args={
                    "threshold": 1.25,
                    "negative_zero": -0.0,
                    "subnormal": 5e-324,
                    "maximum": 1.7976931348623157e308,
                },
                preserve=(),
                source=ValueSource.MODEL,
                depends_on=(),
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-float", criteria=()),
    )
    return transition_task(task, TaskEvent.SUBMIT_PROGRAM, program=program)


def _task_subclass() -> TaskRunSubclass:
    task = _task()
    return TaskRunSubclass(
        id=task.id,
        project_id=task.project_id,
        base_revision=task.base_revision,
        reasoning_owner=task.reasoning_owner,
        review_policy=task.review_policy,
        status=task.status,
    )


def _record_name(task_id: str = TASK_ID) -> str:
    digest = hashlib.sha256(KEY_DOMAIN + task_id.encode("utf-8")).hexdigest()
    return f"{digest}.json"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _record_bytes(task: TaskRun, generation: int = 0) -> bytes:
    body = {
        "generation": generation,
        "schema_version": 1,
        "task_run": task.to_mapping(),
    }
    return _record_from_body(body)


def _record_from_body(body: dict[str, object]) -> bytes:
    checksum = hashlib.sha256(CHECKSUM_DOMAIN + _canonical(body)).hexdigest()
    return _canonical({**body, "checksum": checksum})


def _record_from_raw_body(body_raw: bytes) -> bytes:
    checksum = hashlib.sha256(CHECKSUM_DOMAIN + body_raw).hexdigest().encode("ascii")
    assert body_raw.startswith(b"{")
    return b'{"checksum":"' + checksum + b'",' + body_raw[1:]


def _write_record(root: Path, raw: bytes, task_id: str = TASK_ID) -> Path:
    path = root / _record_name(task_id)
    path.write_bytes(raw)
    path.chmod(0o600)
    return path


def _with_wrong_uid(result: os.stat_result) -> os.stat_result:
    values = list(result)
    values[4] = result.st_uid + 1
    return os.stat_result(values)


def _assert_error(caught, code: TaskStoreErrorCode) -> TaskStoreError:
    error = caught.value
    assert type(error) is TaskStoreError
    assert error.code is code
    assert error.__cause__ is None
    assert error.__context__ is None
    assert len(str(error).splitlines()) == 1
    public_surface = str(error) + repr(error) + repr(getattr(error, "__dict__", {}))
    for secret in (TASK_ID, OTHER_TASK_ID, PROJECT_ID, "sentinel", _record_name()):
        assert secret not in public_surface
    return error


def _patch_dir_fd_callable(
    monkeypatch,
    name: str,
    replacement,
    *,
    follow_symlinks: bool = False,
) -> None:
    supported = set(store_module.os.supports_dir_fd)
    supported.add(replacement)
    monkeypatch.setattr(store_module.os, "supports_dir_fd", supported)
    if follow_symlinks:
        follow = set(store_module.os.supports_follow_symlinks)
        follow.add(replacement)
        monkeypatch.setattr(store_module.os, "supports_follow_symlinks", follow)
    monkeypatch.setattr(store_module.os, name, replacement)


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    root = tmp_path / "task-store"
    root.mkdir(mode=0o700)
    return root


@pytest.fixture
def lease_root(tmp_path: Path) -> Path:
    root = tmp_path / "task-locks"
    root.mkdir(mode=0o700)
    return root


@pytest.fixture
def store(store_root: Path, lease_root: Path) -> TaskRunStore:
    manager = ResourceLeaseManager(lease_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    return TaskRunStore(
        store_root,
        manager,
        trust=TaskStoreRootTrust.TRUSTED_LOCAL,
    )


def test_public_api_and_exact_signatures_are_stable():
    assert store_module.__all__ == (
        "StoredTaskRun",
        "TaskRunStore",
        "TaskStoreError",
        "TaskStoreErrorCode",
        "TaskStoreRootTrust",
    )
    init = inspect.signature(TaskRunStore.__init__).parameters
    assert tuple(init) == ("self", "root", "lease_manager", "trust")
    assert init["self"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert init["root"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert init["lease_manager"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert init["trust"].kind is inspect.Parameter.KEYWORD_ONLY
    assert all(parameter.default is inspect.Parameter.empty for parameter in init.values())
    expected = {
        "create": ("self", "task_run"),
        "load": ("self", "task_id"),
        "compare_and_set": ("self", "task_id", "expected_generation", "task_run"),
        "validate_record": ("self", "task_run", "generation"),
    }
    for method_name, names in expected.items():
        parameters = inspect.signature(getattr(TaskRunStore, method_name)).parameters
        assert tuple(parameters) == names
        assert all(
            parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            for parameter in parameters.values()
        )
        assert all(
            parameter.default is inspect.Parameter.empty for parameter in parameters.values()
        )


def test_error_codes_are_exact_and_complete():
    assert {item.value for item in TaskStoreErrorCode} == {
        "invalid_id",
        "not_found",
        "already_exists",
        "conflict",
        "corrupt_record",
        "record_too_large",
        "unsafe_store",
        "lock_unavailable",
        "io_error",
        "durability_uncertain",
        "resource_exhausted",
    }


def test_physical_store_limits_are_exact() -> None:
    assert store_module._MAX_TASK_RECORDS == 1024
    assert store_module._MAX_RECORD_BYTES == 2 * 1024 * 1024
    assert store_module._MAX_TASK_STORE_BYTES == 2_147_483_648
    assert store_module._MAX_JOURNAL_BYTES == 64 * 1024


def test_forged_loads_do_not_create_caller_derived_lock_entries(
    store: TaskRunStore,
    lease_root: Path,
) -> None:
    before = sorted(
        (entry.name, entry.stat().st_ino, entry.stat().st_size) for entry in lease_root.iterdir()
    )
    for index in range(10_000):
        task_id = f"task_{index:032x}"
        with pytest.raises(TaskStoreError) as caught:
            store.load(task_id)
        _assert_error(caught, TaskStoreErrorCode.NOT_FOUND)
    after = sorted(
        (entry.name, entry.stat().st_ino, entry.stat().st_size) for entry in lease_root.iterdir()
    )
    assert after == before


def test_record_count_n_plus_one_is_rejected_before_a_task_lock_is_created(
    store: TaskRunStore,
    store_root: Path,
    lease_root: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(store_module, "_MAX_TASK_RECORDS", 1, raising=False)
    store.create(_task())
    before_records = sorted((entry.name, entry.stat().st_ino) for entry in store_root.iterdir())
    before_locks = sorted((entry.name, entry.stat().st_ino) for entry in lease_root.iterdir())

    with pytest.raises(TaskStoreError) as caught:
        store.create(_task(OTHER_TASK_ID))

    _assert_error(caught, TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    assert (
        sorted((entry.name, entry.stat().st_ino) for entry in store_root.iterdir())
        == before_records
    )
    assert (
        sorted((entry.name, entry.stat().st_ino) for entry in lease_root.iterdir()) == before_locks
    )


def test_replacement_physical_peak_n_plus_one_preserves_the_existing_record(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
) -> None:
    store.create(_task())
    record = store_root / _record_name()
    before = (record.stat().st_ino, record.read_bytes())
    replacement = _record_bytes(_task(), generation=1)
    monkeypatch.setattr(
        store_module,
        "_MAX_TASK_STORE_BYTES",
        record.stat().st_size + len(replacement) - 1,
        raising=False,
    )

    with pytest.raises(TaskStoreError) as caught:
        store.compare_and_set(TASK_ID, 0, _task())

    _assert_error(caught, TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    assert (record.stat().st_ino, record.read_bytes()) == before
    assert [entry.name for entry in store_root.iterdir()] == [_record_name()]


def test_create_physical_peak_exact_equality_and_n_plus_one_include_journal_and_temp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    token = "c" * 32
    task = _task()
    raw = store_module._encode_record(task, 0)
    target = _record_name()
    temp_name = f".{target}.{token}.tmp"
    new_sha256 = hashlib.sha256(raw).hexdigest()
    reserved = store_module._journal_line(
        store_module._journal_body(
            state="RESERVED",
            task_id=TASK_ID,
            target=target,
            old_sha256=None,
            new_sha256=new_sha256,
            new_size=len(raw),
            temp_name=temp_name,
        )
    )
    staged = TaskRunStore._staged_bound_line(
        task_id=TASK_ID,
        target=target,
        old_sha256=None,
        new_sha256=new_sha256,
        new_size=len(raw),
        temp_name=temp_name,
    )
    exact_peak = len(reserved) + len(staged) + len(raw)

    roots = [tmp_path / name for name in ("equal-locks", "equal-store", "over-locks", "over-store")]
    for root in roots:
        root.mkdir(mode=0o700)
    equal = TaskRunStore(
        roots[1],
        ResourceLeaseManager(roots[0], trust=LeaseRootTrust.TRUSTED_LOCAL),
        trust=TaskStoreRootTrust.TRUSTED_LOCAL,
    )
    over = TaskRunStore(
        roots[3],
        ResourceLeaseManager(roots[2], trust=LeaseRootTrust.TRUSTED_LOCAL),
        trust=TaskStoreRootTrust.TRUSTED_LOCAL,
    )
    real_token_hex = store_module.secrets.token_hex
    monkeypatch.setattr(
        store_module.secrets,
        "token_hex",
        lambda size: token if size == 16 else real_token_hex(size),
    )
    monkeypatch.setattr(store_module, "_MAX_TASK_STORE_BYTES", exact_peak)
    assert equal.create(task) == StoredTaskRun(generation=0, task_run=task)

    monkeypatch.setattr(store_module, "_MAX_TASK_STORE_BYTES", exact_peak - 1)
    before = sorted(entry.name for entry in roots[2].iterdir())
    with pytest.raises(TaskStoreError) as caught:
        over.create(task)
    _assert_error(caught, TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    assert list(roots[3].iterdir()) == []
    assert sorted(entry.name for entry in roots[2].iterdir()) == before


def test_ten_thousand_over_capacity_creates_do_not_grow_store_or_lock_tree(
    store: TaskRunStore,
    store_root: Path,
    lease_root: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(store_module, "_MAX_TASK_RECORDS", 0)
    before_locks = sorted(
        (entry.name, entry.stat().st_ino, entry.stat().st_size) for entry in lease_root.iterdir()
    )
    for index in range(10_000):
        task_id = f"task_{index:032x}"
        with pytest.raises(TaskStoreError) as caught:
            store.create(_task(task_id))
        _assert_error(caught, TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    assert list(store_root.iterdir()) == []
    assert (
        sorted(
            (entry.name, entry.stat().st_ino, entry.stat().st_size)
            for entry in lease_root.iterdir()
        )
        == before_locks
    )


@pytest.mark.parametrize("kind", ["oversize", "corrupt", "hash_mismatch"])
def test_mutation_scan_rejects_oversize_corrupt_or_hash_mismatched_records(
    store: TaskRunStore,
    store_root: Path,
    lease_root: Path,
    kind: str,
) -> None:
    if kind == "oversize":
        raw = b"x" * (MAX_RECORD_BYTES + 1)
    elif kind == "corrupt":
        raw = b"{}"
    else:
        raw = _record_bytes(_task(OTHER_TASK_ID))
    path = _write_record(store_root, raw, TASK_ID)
    before = (
        path.stat().st_ino,
        path.stat().st_size,
        sorted((entry.name, entry.stat().st_ino) for entry in lease_root.iterdir()),
    )

    with pytest.raises(TaskStoreError) as caught:
        store.create(_task(OTHER_TASK_ID))

    _assert_error(caught, TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    assert (
        path.stat().st_ino,
        path.stat().st_size,
        sorted((entry.name, entry.stat().st_ino) for entry in lease_root.iterdir()),
    ) == before


def test_staged_journal_recovers_one_crash_remnant_without_a_second_temp(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    real_replace = store_module.os.replace
    crashed = False

    def crash_before_publish(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal crashed
        if not crashed and os.fsdecode(os.fspath(src)).endswith(".tmp"):
            crashed = True
            raise SimulatedProcessCrash
        return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "replace", crash_before_publish)
        with pytest.raises(SimulatedProcessCrash):
            store.create(_task())

    names = sorted(entry.name for entry in store_root.iterdir())
    assert MUTATION_JOURNAL_NAME in names
    assert len([name for name in names if name.endswith(".tmp")]) == 1
    journal_lines = (store_root / MUTATION_JOURNAL_NAME).read_text(encoding="utf-8").splitlines()
    assert json.loads(journal_lines[-1])["body"]["state"] == "STAGED"

    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.ALREADY_EXISTS)
    assert store.load(TASK_ID) == StoredTaskRun(generation=0, task_run=_task())
    assert sorted(entry.name for entry in store_root.iterdir()) == [_record_name()]


def test_reserved_journal_recovers_a_crash_before_temp_creation(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    real_open = store_module.os.open
    crashed = False

    def crash_on_first_temp(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal crashed
        if not crashed and os.fsdecode(os.fspath(path)).endswith(".tmp"):
            crashed = True
            raise SimulatedProcessCrash
        return real_open(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        _patch_dir_fd_callable(patch, "open", crash_on_first_temp)
        with pytest.raises(SimulatedProcessCrash):
            store.create(_task())

    names = sorted(entry.name for entry in store_root.iterdir())
    assert names == [MUTATION_JOURNAL_NAME]
    journal_lines = (store_root / MUTATION_JOURNAL_NAME).read_text(encoding="utf-8").splitlines()
    assert json.loads(journal_lines[-1])["body"]["state"] == "RESERVED"

    assert store.create(_task()) == StoredTaskRun(generation=0, task_run=_task())
    assert sorted(entry.name for entry in store_root.iterdir()) == [_record_name()]


def test_partial_reserved_journal_write_crash_is_preserved_fail_closed(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    real_open = store_module.os.open
    real_close = store_module.os.close
    real_write = store_module.os.write
    journal_fds: set[int] = set()
    journal_write_calls = 0

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if os.fsdecode(os.fspath(path)) == MUTATION_JOURNAL_NAME and flags & os.O_CREAT:
            journal_fds.add(fd)
        return fd

    def partial_then_crash(fd: int, data) -> int:
        nonlocal journal_write_calls
        if fd not in journal_fds:
            return real_write(fd, data)
        journal_write_calls += 1
        if journal_write_calls == 1:
            return real_write(fd, bytes(data[:17]))
        raise SimulatedProcessCrash

    with monkeypatch.context() as patch:
        _patch_dir_fd_callable(patch, "open", recording_open)
        patch.setattr(store_module.os, "write", partial_then_crash)
        with pytest.raises(SimulatedProcessCrash):
            store.create(_task())

    for fd in journal_fds:
        try:
            real_close(fd)
        except OSError:
            pass
    assert journal_write_calls == 2
    journal = store_root / MUTATION_JOURNAL_NAME
    assert 0 < journal.stat().st_size < len(_record_bytes(_task()))
    before = (journal.stat().st_ino, journal.read_bytes())

    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    assert (journal.stat().st_ino, journal.read_bytes()) == before


def test_reserved_journal_fsync_crash_restarts_from_the_durable_line(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    real_open = store_module.os.open
    real_close = store_module.os.close
    real_fsync = store_module.os.fsync
    journal_fds: set[int] = set()
    crashed = False

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if os.fsdecode(os.fspath(path)) == MUTATION_JOURNAL_NAME and flags & os.O_CREAT:
            journal_fds.add(fd)
        return fd

    def crash_after_journal_fsync(fd: int) -> None:
        nonlocal crashed
        real_fsync(fd)
        if fd in journal_fds and not crashed:
            crashed = True
            raise SimulatedProcessCrash

    with monkeypatch.context() as patch:
        _patch_dir_fd_callable(patch, "open", recording_open)
        patch.setattr(store_module.os, "fsync", crash_after_journal_fsync)
        with pytest.raises(SimulatedProcessCrash):
            store.create(_task())

    for fd in journal_fds:
        try:
            real_close(fd)
        except OSError:
            pass
    assert crashed
    assert sorted(entry.name for entry in store_root.iterdir()) == [MUTATION_JOURNAL_NAME]
    journal_lines = (store_root / MUTATION_JOURNAL_NAME).read_text(encoding="utf-8").splitlines()
    assert json.loads(journal_lines[-1])["body"]["state"] == "RESERVED"

    assert store.create(_task()) == StoredTaskRun(generation=0, task_run=_task())
    assert sorted(entry.name for entry in store_root.iterdir()) == [_record_name()]


def test_partial_temp_write_crash_is_preserved_and_restart_fails_closed(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    real_open = store_module.os.open
    real_write = store_module.os.write
    temp_fds: set[int] = set()
    temp_write_calls = 0

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if os.fsdecode(os.fspath(path)).endswith(".tmp") and flags & os.O_CREAT:
            temp_fds.add(fd)
        return fd

    def partial_then_crash(fd: int, data) -> int:
        nonlocal temp_write_calls
        if fd not in temp_fds:
            return real_write(fd, data)
        temp_write_calls += 1
        if temp_write_calls == 1:
            return real_write(fd, bytes(data[:7]))
        raise SimulatedProcessCrash

    with monkeypatch.context() as patch:
        _patch_dir_fd_callable(patch, "open", recording_open)
        patch.setattr(store_module.os, "write", partial_then_crash)
        with pytest.raises(SimulatedProcessCrash):
            store.create(_task())

    assert temp_write_calls == 2
    before = {
        entry.name: (entry.stat().st_ino, entry.read_bytes()) for entry in store_root.iterdir()
    }
    assert MUTATION_JOURNAL_NAME in before
    assert len([name for name in before if name.endswith(".tmp")]) == 1

    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    assert {
        entry.name: (entry.stat().st_ino, entry.read_bytes()) for entry in store_root.iterdir()
    } == before


def test_temp_file_fsync_crash_rolls_forward_on_restart(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    real_open = store_module.os.open
    real_fsync = store_module.os.fsync
    temp_fds: set[int] = set()
    crashed = False

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if os.fsdecode(os.fspath(path)).endswith(".tmp") and flags & os.O_CREAT:
            temp_fds.add(fd)
        return fd

    def crash_after_temp_fsync(fd: int) -> None:
        nonlocal crashed
        if fd in temp_fds and not crashed:
            crashed = True
            raise SimulatedProcessCrash
        real_fsync(fd)

    with monkeypatch.context() as patch:
        _patch_dir_fd_callable(patch, "open", recording_open)
        patch.setattr(store_module.os, "fsync", crash_after_temp_fsync)
        with pytest.raises(SimulatedProcessCrash):
            store.create(_task())

    assert crashed
    names = sorted(entry.name for entry in store_root.iterdir())
    assert MUTATION_JOURNAL_NAME in names
    assert len([name for name in names if name.endswith(".tmp")]) == 1
    before = {
        entry.name: (entry.stat().st_ino, entry.read_bytes()) for entry in store_root.iterdir()
    }

    def fail_recovery_temp_fsync(fd: int) -> None:
        result = os.fstat(fd)
        if stat.S_ISREG(result.st_mode) and result.st_size == len(_record_bytes(_task())):
            raise OSError("recovery temp fsync sentinel")
        real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "fsync", fail_recovery_temp_fsync)
        with pytest.raises(TaskStoreError) as caught:
            store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.IO_ERROR)
    assert {
        entry.name: (entry.stat().st_ino, entry.read_bytes()) for entry in store_root.iterdir()
    } == before

    recovery_regular_fsync_sizes: list[int] = []

    def recording_recovery_fsync(fd: int) -> None:
        result = os.fstat(fd)
        if stat.S_ISREG(result.st_mode):
            recovery_regular_fsync_sizes.append(result.st_size)
        real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "fsync", recording_recovery_fsync)
        with pytest.raises(TaskStoreError) as caught:
            store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.ALREADY_EXISTS)
    assert len(_record_bytes(_task())) in recovery_regular_fsync_sizes
    assert store.load(TASK_ID) == StoredTaskRun(generation=0, task_run=_task())
    assert sorted(entry.name for entry in store_root.iterdir()) == [_record_name()]


def test_staged_journal_fsync_crash_rolls_forward_on_restart(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    real_open = store_module.os.open
    real_close = store_module.os.close
    real_fsync = store_module.os.fsync
    staged_journal_fds: set[int] = set()
    crashed = False

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if os.fsdecode(os.fspath(path)) == MUTATION_JOURNAL_NAME and flags & os.O_APPEND:
            staged_journal_fds.add(fd)
        return fd

    def crash_after_staged_journal_fsync(fd: int) -> None:
        nonlocal crashed
        real_fsync(fd)
        if fd in staged_journal_fds and not crashed:
            crashed = True
            raise SimulatedProcessCrash

    with monkeypatch.context() as patch:
        _patch_dir_fd_callable(patch, "open", recording_open)
        patch.setattr(store_module.os, "fsync", crash_after_staged_journal_fsync)
        with pytest.raises(SimulatedProcessCrash):
            store.create(_task())

    for fd in staged_journal_fds:
        try:
            real_close(fd)
        except OSError:
            pass
    assert crashed
    journal_lines = (store_root / MUTATION_JOURNAL_NAME).read_text(encoding="utf-8").splitlines()
    assert json.loads(journal_lines[-1])["body"]["state"] == "STAGED"
    assert len([entry for entry in store_root.iterdir() if entry.name.endswith(".tmp")]) == 1

    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.ALREADY_EXISTS)
    assert store.load(TASK_ID) == StoredTaskRun(generation=0, task_run=_task())
    assert sorted(entry.name for entry in store_root.iterdir()) == [_record_name()]


def test_partial_staged_journal_write_crash_is_reconstructed_on_restart(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    real_open = store_module.os.open
    real_close = store_module.os.close
    real_write = store_module.os.write
    staged_journal_fds: set[int] = set()
    staged_write_calls = 0

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if os.fsdecode(os.fspath(path)) == MUTATION_JOURNAL_NAME and flags & os.O_APPEND:
            staged_journal_fds.add(fd)
        return fd

    def partial_then_crash(fd: int, data) -> int:
        nonlocal staged_write_calls
        if fd not in staged_journal_fds:
            return real_write(fd, data)
        staged_write_calls += 1
        if staged_write_calls == 1:
            return real_write(fd, bytes(data[:17]))
        raise SimulatedProcessCrash

    with monkeypatch.context() as patch:
        _patch_dir_fd_callable(patch, "open", recording_open)
        patch.setattr(store_module.os, "write", partial_then_crash)
        with pytest.raises(SimulatedProcessCrash):
            store.create(_task())

    for fd in staged_journal_fds:
        try:
            real_close(fd)
        except OSError:
            pass
    assert staged_write_calls == 2
    journal = store_root / MUTATION_JOURNAL_NAME
    raw = journal.read_bytes()
    assert raw.count(b"\n") == 1
    assert raw.split(b"\n", 1)[1] == b'{"body":{"new_sha'
    assert len([entry for entry in store_root.iterdir() if entry.name.endswith(".tmp")]) == 1

    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.ALREADY_EXISTS)
    assert store.load(TASK_ID) == StoredTaskRun(generation=0, task_run=_task())
    assert sorted(entry.name for entry in store_root.iterdir()) == [_record_name()]


def test_post_replace_readback_crash_reconciles_on_restart(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    real_read_record = store_module._read_record
    crashed = False

    def crash_on_readback(root_fd: int, root_stat, filename: str, task_id: str):
        nonlocal crashed
        if (
            not crashed
            and (store_root / _record_name()).exists()
            and (store_root / MUTATION_JOURNAL_NAME).exists()
            and not any(entry.name.endswith(".tmp") for entry in store_root.iterdir())
        ):
            crashed = True
            raise SimulatedProcessCrash
        return real_read_record(root_fd, root_stat, filename, task_id)

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_read_record", crash_on_readback)
        with pytest.raises(SimulatedProcessCrash):
            store.create(_task())

    assert crashed
    assert (store_root / _record_name()).read_bytes() == _record_bytes(_task())
    assert (store_root / MUTATION_JOURNAL_NAME).exists()

    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.ALREADY_EXISTS)
    assert store.load(TASK_ID) == StoredTaskRun(generation=0, task_run=_task())
    assert sorted(entry.name for entry in store_root.iterdir()) == [_record_name()]


def test_post_replace_directory_fsync_crash_reconciles_on_restart(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    real_fsync = store_module.os.fsync
    store_identity = (store_root.stat().st_dev, store_root.stat().st_ino)
    directory_calls = 0

    def crash_after_publish_directory_fsync(fd: int) -> None:
        nonlocal directory_calls
        result = os.fstat(fd)
        is_store_directory = (
            stat.S_ISDIR(result.st_mode)
            and (
                result.st_dev,
                result.st_ino,
            )
            == store_identity
        )
        real_fsync(fd)
        if is_store_directory:
            directory_calls += 1
            if directory_calls == 2:
                raise SimulatedProcessCrash

    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "fsync", crash_after_publish_directory_fsync)
        with pytest.raises(SimulatedProcessCrash):
            store.create(_task())

    assert directory_calls == 2
    assert (store_root / _record_name()).read_bytes() == _record_bytes(_task())
    assert (store_root / MUTATION_JOURNAL_NAME).exists()

    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.ALREADY_EXISTS)
    assert store.load(TASK_ID) == StoredTaskRun(generation=0, task_run=_task())
    assert sorted(entry.name for entry in store_root.iterdir()) == [_record_name()]


@pytest.mark.parametrize("with_valid_temp", [False, True], ids=("reserved", "staged"))
def test_arbitrary_partial_journal_tail_is_never_truncated_or_recovered(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
    with_valid_temp: bool,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    if with_valid_temp:
        real_replace = store_module.os.replace
        crashed = False

        def crash_before_publish(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            nonlocal crashed
            if not crashed and os.fsdecode(os.fspath(src)).endswith(".tmp"):
                crashed = True
                raise SimulatedProcessCrash
            return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with monkeypatch.context() as patch:
            patch.setattr(store_module.os, "replace", crash_before_publish)
            with pytest.raises(SimulatedProcessCrash):
                store.create(_task())
    else:
        real_open = store_module.os.open
        crashed = False

        def crash_before_temp(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal crashed
            if not crashed and os.fsdecode(os.fspath(path)).endswith(".tmp"):
                crashed = True
                raise SimulatedProcessCrash
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with monkeypatch.context() as patch:
            _patch_dir_fd_callable(patch, "open", crash_before_temp)
            with pytest.raises(SimulatedProcessCrash):
                store.create(_task())

    journal = store_root / MUTATION_JOURNAL_NAME
    raw = journal.read_bytes()
    reserved_end = raw.index(b"\n") + 1
    journal.write_bytes(raw[:reserved_end] + b"arbitrary-tail")
    journal.chmod(0o600)
    before = {
        entry.name: (entry.stat().st_ino, entry.read_bytes()) for entry in store_root.iterdir()
    }

    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())

    _assert_error(caught, TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    assert {
        entry.name: (entry.stat().st_ino, entry.read_bytes()) for entry in store_root.iterdir()
    } == before


@pytest.mark.parametrize(
    "kind",
    ("corrupt_journal", "oversize_journal", "orphan_temp", "two_temps", "extra_file"),
)
def test_corrupt_journal_and_extra_remnant_matrix_is_preserved_fail_closed(
    store: TaskRunStore,
    store_root: Path,
    kind: str,
) -> None:
    target = _record_name()
    if kind == "corrupt_journal":
        entries = {MUTATION_JOURNAL_NAME: b"{}"}
    elif kind == "oversize_journal":
        entries = {MUTATION_JOURNAL_NAME: b"x" * (64 * 1024 + 1)}
    elif kind == "orphan_temp":
        entries = {f".{target}.{'a' * 32}.tmp": b"orphan"}
    elif kind == "two_temps":
        entries = {
            MUTATION_JOURNAL_NAME: b"{}",
            f".{target}.{'a' * 32}.tmp": b"one",
            f".{target}.{'b' * 32}.tmp": b"two",
        }
    else:
        entries = {"unexpected-entry": b"extra"}
    for name, raw in entries.items():
        path = store_root / name
        path.write_bytes(raw)
        path.chmod(0o600)
    before = {
        entry.name: (entry.stat().st_ino, entry.read_bytes()) for entry in store_root.iterdir()
    }

    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())

    _assert_error(caught, TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    assert {
        entry.name: (entry.stat().st_ino, entry.read_bytes()) for entry in store_root.iterdir()
    } == before


def test_failed_cleanup_with_roll_forward_authority_is_durability_uncertain(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
) -> None:
    real_unlink = store_module.os.unlink

    def fail_publish(*_args, **_kwargs):
        raise OSError("pre-publish replace sentinel")

    def fail_temp_cleanup(path, *, dir_fd=None):
        if os.fsdecode(os.fspath(path)).endswith(".tmp"):
            raise OSError("temp cleanup sentinel")
        return real_unlink(path, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "replace", fail_publish)
        _patch_dir_fd_callable(patch, "unlink", fail_temp_cleanup)
        with pytest.raises(TaskStoreError) as caught:
            store.create(_task())
    error = _assert_error(caught, TaskStoreErrorCode.DURABILITY_UNCERTAIN)
    assert error.committed_generation == 0
    assert not (store_root / _record_name()).exists()
    assert (store_root / MUTATION_JOURNAL_NAME).exists()
    assert len([entry for entry in store_root.iterdir() if entry.name.endswith(".tmp")]) == 1

    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.ALREADY_EXISTS)
    assert store.load(TASK_ID) == StoredTaskRun(generation=0, task_run=_task())
    assert sorted(entry.name for entry in store_root.iterdir()) == [_record_name()]


def test_normal_publish_rechecks_temp_identity_after_latest_record_read(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
) -> None:
    original_require = TaskRunStore._require_expected
    calls = 0
    moved = store_root / "publication-identity-moved"

    def swap_before_publication(current, expected_generation):
        nonlocal calls
        original_require(current, expected_generation)
        calls += 1
        if calls == 3:
            temp = next(entry for entry in store_root.iterdir() if entry.name.endswith(".tmp"))
            temp.rename(moved)
            temp.write_bytes(_record_bytes(_task(), generation=1))
            temp.chmod(0o600)

    with monkeypatch.context() as patch:
        patch.setattr(
            TaskRunStore,
            "_require_expected",
            staticmethod(swap_before_publication),
        )
        with pytest.raises(TaskStoreError) as caught:
            store.create(_task())

    _assert_error(caught, TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    assert calls == 3
    assert not (store_root / _record_name()).exists()
    assert moved.read_bytes() == _record_bytes(_task())
    replacement = next(entry for entry in store_root.iterdir() if entry.name.endswith(".tmp"))
    assert replacement.read_bytes() == _record_bytes(_task(), generation=1)


@pytest.mark.parametrize("replacement_generation", [0, 1], ids=("same-bytes", "different-bytes"))
def test_first_temp_evidence_must_match_the_exclusive_create_identity(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
    replacement_generation: int,
) -> None:
    original_evidence = store_module._temp_evidence
    evidence_calls = 0
    moved = store_root / "exclusive-temp-moved"

    def swap_before_first_evidence(*args, **kwargs):
        nonlocal evidence_calls
        evidence_calls += 1
        if evidence_calls == 1:
            temp = store_root / kwargs["name"]
            temp.rename(moved)
            temp.write_bytes(_record_bytes(_task(), generation=replacement_generation))
            temp.chmod(0o600)
        return original_evidence(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_temp_evidence", swap_before_first_evidence)
        with pytest.raises(TaskStoreError) as caught:
            store.create(_task())

    _assert_error(caught, TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    assert evidence_calls == 1
    assert not (store_root / _record_name()).exists()
    assert moved.read_bytes() == _record_bytes(_task())
    replacement = next(entry for entry in store_root.iterdir() if entry.name.endswith(".tmp"))
    assert replacement.read_bytes() == _record_bytes(_task(), generation=replacement_generation)
    assert (store_root / MUTATION_JOURNAL_NAME).exists()


def test_recovery_publish_rechecks_temp_identity_immediately_before_replace(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    real_replace = store_module.os.replace
    crashed = False

    def crash_before_publish(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal crashed
        if not crashed and os.fsdecode(os.fspath(src)).endswith(".tmp"):
            crashed = True
            raise SimulatedProcessCrash
        return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "replace", crash_before_publish)
        with pytest.raises(SimulatedProcessCrash):
            store.create(_task())

    original_evidence = store_module._temp_evidence
    evidence_calls = 0
    moved = store_root / "recovery-identity-moved"

    def swap_on_publication_recheck(*args, **kwargs):
        nonlocal evidence_calls
        evidence_calls += 1
        if evidence_calls == 2:
            temp = next(entry for entry in store_root.iterdir() if entry.name.endswith(".tmp"))
            temp.rename(moved)
            temp.write_bytes(_record_bytes(_task(), generation=1))
            temp.chmod(0o600)
        return original_evidence(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_temp_evidence", swap_on_publication_recheck)
        with pytest.raises(TaskStoreError) as caught:
            store.create(_task())

    _assert_error(caught, TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    assert evidence_calls == 2
    assert not (store_root / _record_name()).exists()
    assert moved.read_bytes() == _record_bytes(_task())
    replacement = next(entry for entry in store_root.iterdir() if entry.name.endswith(".tmp"))
    assert replacement.read_bytes() == _record_bytes(_task(), generation=1)


def test_create_load_and_compare_and_set_round_trip(store: TaskRunStore, store_root: Path):
    created = store.create(_task())
    assert created == StoredTaskRun(generation=0, task_run=_task())
    assert store.load(TASK_ID) == created
    updated = store.compare_and_set(TASK_ID, 0, _task())
    assert updated == StoredTaskRun(generation=1, task_run=_task())
    assert store.load(TASK_ID) == updated
    path = store_root / _record_name()
    assert path.read_bytes() == _record_bytes(_task(), generation=1)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_finite_float_task_content_round_trips_canonically(store: TaskRunStore) -> None:
    task = _task_with_float()
    assert store.validate_record(task, 0) is None
    assert store.create(task) == StoredTaskRun(generation=0, task_run=task)
    loaded = store.load(TASK_ID)
    assert loaded == StoredTaskRun(generation=0, task_run=task)
    assert loaded.task_run.program is not None
    args = loaded.task_run.program.operations[0].args
    assert all(type(args[name]) is float for name in args)
    assert float.hex(args["threshold"]) == float.hex(1.25)
    assert float.hex(args["negative_zero"]) == float.hex(-0.0)
    assert float.hex(args["subnormal"]) == float.hex(5e-324)
    assert float.hex(args["maximum"]) == float.hex(1.7976931348623157e308)


def test_write_preflight_uses_the_same_task_decode_budget(
    store: TaskRunStore,
    store_root: Path,
) -> None:
    assert store.validate_record(_task_with_unicode("x" * 4096), 0) is None
    with pytest.raises(TaskStoreError) as caught:
        store.validate_record(_task_with_unicode("x" * 4097), 0)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)
    with pytest.raises(TaskStoreError) as caught:
        store.create(_task_with_unicode("x" * 4097))
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)
    assert list(store_root.iterdir()) == []


def test_stored_task_run_is_frozen_and_uses_exact_types():
    stored = StoredTaskRun(generation=0, task_run=_task())
    with pytest.raises(FrozenInstanceError):
        stored.generation = 1
    for generation in (True, -1, 2**53, 1.0, "0"):
        with pytest.raises((TypeError, ValueError)):
            StoredTaskRun(generation=generation, task_run=_task())
    with pytest.raises(TypeError):
        StoredTaskRun(generation=0, task_run=object())
    with pytest.raises((TypeError, ValueError)):
        StoredTaskRun(generation=GenerationInteger(0), task_run=_task())
    with pytest.raises(TypeError):
        StoredTaskRun(generation=0, task_run=_task_subclass())


def test_identifier_and_generation_subclasses_are_rejected_before_storage(
    store: TaskRunStore, monkeypatch
):
    touched: list[str] = []

    def storage_probe(*_args, **_kwargs):
        touched.append("storage")
        raise AssertionError("subclass reached storage")

    monkeypatch.setattr(store_module.os, "open", storage_probe)
    with pytest.raises(TaskStoreError) as caught:
        store.load(IdString(TASK_ID))
    _assert_error(caught, TaskStoreErrorCode.INVALID_ID)
    with pytest.raises(TaskStoreError) as caught:
        store.compare_and_set(
            IdString(TASK_ID),
            0,
            _task(),
        )
    _assert_error(caught, TaskStoreErrorCode.INVALID_ID)
    with pytest.raises(TaskStoreError) as caught:
        store.compare_and_set(TASK_ID, GenerationInteger(0), _task())
    _assert_error(caught, TaskStoreErrorCode.CONFLICT)
    assert touched == []


def test_task_run_subclass_is_rejected_before_serialization_lease_or_storage(
    store: TaskRunStore, monkeypatch
):
    touched: list[str] = []

    def storage_probe(*_args, **_kwargs):
        touched.append("storage")
        raise AssertionError("TaskRun subclass reached storage")

    monkeypatch.setattr(store_module.os, "open", storage_probe)
    with pytest.raises(TypeError):
        store.create(_task_subclass())
    with pytest.raises(TypeError):
        store.compare_and_set(TASK_ID, 0, _task_subclass())
    assert touched == []


def test_canonical_record_is_deterministic_and_domain_separated(
    store: TaskRunStore, store_root: Path
):
    store.create(_task())
    raw = (store_root / _record_name()).read_bytes()
    assert raw == _record_bytes(_task())
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert not raw.endswith(b"\n")
    decoded = json.loads(raw)
    body = {key: value for key, value in decoded.items() if key != "checksum"}
    assert decoded["checksum"] != hashlib.sha256(_canonical(body)).hexdigest()


@pytest.mark.parametrize(
    "task_id",
    [
        "",
        "task_0123456789ABCDEF0123456789abcdef",
        "task_0",
        "../task_0123456789abcdef0123456789abcdef",
        "task_0123456789abcdef0123456789abcdef/extra",
        "任务_0123456789abcdef0123456789abcdef",
        True,
        1,
    ],
)
def test_load_rejects_noncanonical_identifiers_before_storage_work(
    store: TaskRunStore, monkeypatch, task_id
):
    touched: list[str] = []

    def storage_probe(*_args, **_kwargs):
        touched.append("storage")
        raise AssertionError("invalid id reached storage")

    monkeypatch.setattr(store_module.os, "open", storage_probe)
    with pytest.raises(TaskStoreError) as caught:
        store.load(task_id)
    _assert_error(caught, TaskStoreErrorCode.INVALID_ID)
    assert touched == []


def test_create_rejects_non_task_run_before_storage_work(store: TaskRunStore, monkeypatch):
    touched: list[str] = []

    def storage_probe(*_args, **_kwargs):
        touched.append("storage")
        raise AssertionError("invalid value reached storage")

    monkeypatch.setattr(store_module.os, "open", storage_probe)
    with pytest.raises(TypeError):
        store.create(object())
    assert touched == []


def test_compare_and_set_rejects_mismatch_and_invalid_generation_before_storage(
    store: TaskRunStore, monkeypatch
):
    touched: list[str] = []

    def storage_probe(*_args, **_kwargs):
        touched.append("storage")
        raise AssertionError("invalid CAS reached storage")

    monkeypatch.setattr(store_module.os, "open", storage_probe)
    with pytest.raises(TaskStoreError) as caught:
        store.compare_and_set(TASK_ID, 0, _task(OTHER_TASK_ID))
    _assert_error(caught, TaskStoreErrorCode.INVALID_ID)
    for generation in (True, -1, 2**53, "0", 0.0):
        with pytest.raises(TaskStoreError) as caught:
            store.compare_and_set(TASK_ID, generation, _task())
        _assert_error(caught, TaskStoreErrorCode.CONFLICT)
    assert touched == []


def test_create_existing_load_missing_and_cas_outcomes(store: TaskRunStore):
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.NOT_FOUND)
    with pytest.raises(TaskStoreError) as caught:
        store.compare_and_set(TASK_ID, 0, _task())
    _assert_error(caught, TaskStoreErrorCode.NOT_FOUND)
    store.create(_task())
    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.ALREADY_EXISTS)
    with pytest.raises(TaskStoreError) as caught:
        store.compare_and_set(TASK_ID, 7, _task())
    _assert_error(caught, TaskStoreErrorCode.CONFLICT)


def test_constructor_requires_exact_trust_and_manager(store_root: Path, lease_root: Path):
    manager = ResourceLeaseManager(lease_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    for trust in (None, "trusted_local", True):
        with pytest.raises(TaskStoreError) as caught:
            TaskRunStore(store_root, manager, trust=trust)
        _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)
    with pytest.raises(TypeError):
        TaskRunStore(store_root, object(), trust=TaskStoreRootTrust.TRUSTED_LOCAL)


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o777])
def test_constructor_rejects_nonprivate_store_root(store_root: Path, lease_root: Path, mode: int):
    store_root.chmod(mode)
    manager = ResourceLeaseManager(lease_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    with pytest.raises(TaskStoreError) as caught:
        TaskRunStore(store_root, manager, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
    _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)


def test_constructor_rejects_symlink_and_non_directory_roots(tmp_path: Path, lease_root: Path):
    manager = ResourceLeaseManager(lease_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    real = tmp_path / "real-store"
    real.mkdir(mode=0o700)
    link = tmp_path / "store-link"
    link.symlink_to(real, target_is_directory=True)
    for root in (link, tmp_path / "missing"):
        with pytest.raises(TaskStoreError) as caught:
            TaskRunStore(root, manager, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
        _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)
    regular = tmp_path / "regular"
    regular.write_bytes(b"x")
    regular.chmod(0o700)
    with pytest.raises(TaskStoreError) as caught:
        TaskRunStore(regular, manager, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
    _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)


def test_constructor_maps_invalid_native_root_paths_and_closes_walk_fds(
    store_root: Path,
    lease_root: Path,
    monkeypatch,
):
    manager = ResourceLeaseManager(lease_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    real_open = store_module.os.open
    real_fstat = store_module.os.fstat
    opened_fds: set[int] = set()

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        opened_fds.add(fd)
        return fd

    invalid = str(store_root / "invalid\0component")
    with monkeypatch.context() as patch:
        _patch_dir_fd_callable(patch, "open", recording_open)
        for root in (invalid, Path(invalid)):
            with pytest.raises(TaskStoreError) as caught:
                TaskRunStore(root, manager, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
            _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)
    assert opened_fds
    for fd in opened_fds:
        with pytest.raises(OSError):
            real_fstat(fd)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"", TaskStoreErrorCode.CORRUPT_RECORD),
        (b"{}", TaskStoreErrorCode.CORRUPT_RECORD),
        (b"null", TaskStoreErrorCode.CORRUPT_RECORD),
        (b"[]", TaskStoreErrorCode.CORRUPT_RECORD),
        (b"{", TaskStoreErrorCode.CORRUPT_RECORD),
        (b"\xef\xbb\xbf{}", TaskStoreErrorCode.CORRUPT_RECORD),
        (b"\xff", TaskStoreErrorCode.CORRUPT_RECORD),
        (b"{}\n", TaskStoreErrorCode.CORRUPT_RECORD),
        (b"{} {}", TaskStoreErrorCode.CORRUPT_RECORD),
        (b"[" * 65 + b"]" * 65, TaskStoreErrorCode.CORRUPT_RECORD),
        (b"x" * (MAX_RECORD_BYTES + 1), TaskStoreErrorCode.RECORD_TOO_LARGE),
    ],
)
def test_malformed_records_fail_closed(store: TaskRunStore, store_root: Path, raw: bytes, code):
    _write_record(store_root, raw)
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, code)


def test_duplicate_keys_are_rejected_even_when_last_wins_has_valid_checksum(
    store: TaskRunStore, store_root: Path
):
    valid = _record_bytes(_task())
    duplicate_top = valid.replace(
        b'"schema_version":1',
        b'"schema_version":1,"schema_version":1',
        1,
    )
    encoded_id = json.dumps(TASK_ID).encode("ascii")
    duplicate_nested = valid.replace(
        b'"id":' + encoded_id,
        b'"id":' + encoded_id + b',"id":' + encoded_id,
        1,
    )
    for raw in (duplicate_top, duplicate_nested):
        _write_record(store_root, raw)
        with pytest.raises(TaskStoreError) as caught:
            store.load(TASK_ID)
        _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)


@pytest.mark.parametrize("number", [b"1.0", b"1e0", b"-0.0", b"NaN", b"Infinity", b"-Infinity"])
def test_integer_envelope_field_rejects_float_or_nonfinite_with_matching_checksum(
    store: TaskRunStore, store_root: Path, number: bytes
):
    task_raw = _canonical(_task().to_mapping())
    body_raw = b'{"generation":' + number + b',"schema_version":1,"task_run":' + task_raw + b"}"
    if number == b"1e0":
        checksum_body = body_raw.replace(b'"generation":1e0', b'"generation":1.0', 1)
        checksum = hashlib.sha256(CHECKSUM_DOMAIN + checksum_body).hexdigest().encode("ascii")
        raw = b'{"checksum":"' + checksum + b'",' + body_raw[1:]
    else:
        raw = _record_from_raw_body(body_raw)
    _write_record(store_root, raw)
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)


def test_noncanonical_but_semantically_equal_record_is_rejected(
    store: TaskRunStore, store_root: Path
):
    canonical = _record_bytes(_task())
    decoded = json.loads(canonical)
    variants = [
        json.dumps(decoded, indent=2, ensure_ascii=False).encode("utf-8"),
        json.dumps(decoded, sort_keys=False, ensure_ascii=True).encode("utf-8"),
        b" " + canonical,
        canonical + b"\n",
    ]
    for raw in variants:
        _write_record(store_root, raw)
        with pytest.raises(TaskStoreError) as caught:
            store.load(TASK_ID)
        _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)


def test_unicode_canonical_form_is_literal_and_never_normalized(
    store: TaskRunStore, store_root: Path
):
    decomposed = _task_with_unicode("café/e\u0301/汉")
    store.create(decomposed)
    raw = (store_root / _record_name()).read_bytes()
    assert "café/e\u0301/汉".encode("utf-8") in raw
    assert b"\\u6c49" not in raw
    escaped_unicode = json.dumps(
        json.loads(raw),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    _write_record(store_root, escaped_unicode)
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)
    escaped_slash = raw.replace(b"/", b"\\/", 1)
    _write_record(store_root, escaped_slash)
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)
    composed = _task_with_unicode("café/é/汉")
    assert _record_bytes(decomposed) != _record_bytes(composed)


def test_depth_scanner_ignores_brackets_and_escapes_inside_valid_strings(store: TaskRunStore):
    text = 'quoted-"-backslash-\\-' + "[{]}" * 70
    task = _task_with_unicode(text)
    assert store.create(task) == StoredTaskRun(generation=0, task_run=task)
    assert store.load(TASK_ID) == StoredTaskRun(generation=0, task_run=task)


def test_checksum_mismatch_and_wrong_domain_are_rejected(store: TaskRunStore, store_root: Path):
    decoded = json.loads(_record_bytes(_task()))
    body = {key: value for key, value in decoded.items() if key != "checksum"}
    variants = []
    wrong = dict(decoded)
    wrong["checksum"] = "0" * 64
    variants.append(_canonical(wrong))
    plain = dict(decoded)
    plain["checksum"] = hashlib.sha256(_canonical(body)).hexdigest()
    variants.append(_canonical(plain))
    for raw in variants:
        _write_record(store_root, raw)
        with pytest.raises(TaskStoreError) as caught:
            store.load(TASK_ID)
        _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("generation", True),
        ("generation", -1),
        ("generation", 2**53),
        ("generation", "0"),
        ("checksum", "A" * 64),
        ("checksum", "0" * 63),
        ("checksum", 0),
        ("task_run", []),
    ],
)
def test_envelope_types_and_ranges_are_exact(
    store: TaskRunStore, store_root: Path, field: str, value
):
    body = {
        "generation": 0,
        "schema_version": 1,
        "task_run": _task().to_mapping(),
    }
    if field == "checksum":
        decoded = json.loads(_record_from_body(body))
        decoded[field] = value
        raw = _canonical(decoded)
    else:
        body[field] = value
        raw = _record_from_body(body)
    _write_record(store_root, raw)
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)


def test_extra_or_missing_envelope_fields_are_rejected(store: TaskRunStore, store_root: Path):
    body = {
        "generation": 0,
        "schema_version": 1,
        "task_run": _task().to_mapping(),
    }
    variants = []
    for field in tuple(body):
        candidate = dict(body)
        del candidate[field]
        variants.append(_record_from_body(candidate))
    missing_checksum = json.loads(_record_from_body(body))
    del missing_checksum["checksum"]
    variants.append(_canonical(missing_checksum))
    variants.append(_record_from_body({**body, "extra": None}))
    for raw in variants:
        _write_record(store_root, raw)
        with pytest.raises(TaskStoreError) as caught:
            store.load(TASK_ID)
        _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)


def test_semantically_invalid_task_run_is_corrupt_and_redacted(
    store: TaskRunStore, store_root: Path
):
    decoded = json.loads(_record_bytes(_task()))
    decoded["task_run"]["project_id"] = "private-project-sentinel"
    body = {key: value for key, value in decoded.items() if key != "checksum"}
    decoded["checksum"] = hashlib.sha256(CHECKSUM_DOMAIN + _canonical(body)).hexdigest()
    _write_record(store_root, _canonical(decoded))
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    error = _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)
    assert "private-project-sentinel" not in str(error)


def test_oversize_is_rejected_before_json_decode(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    _write_record(store_root, b"x" * (MAX_RECORD_BYTES + 1))

    def decode_probe(*_args, **_kwargs):
        raise AssertionError("oversize input reached JSON decoding")

    monkeypatch.setattr(store_module.json, "loads", decode_probe)
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.RECORD_TOO_LARGE)


def test_excessive_nesting_is_rejected_before_json_decode(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    nested = b"[" * 64 + b"0" + b"]" * 64
    raw = b'{"checksum":"' + b"0" * 64 + b'","generation":0,"schema_version":1,"task_run":'
    _write_record(store_root, raw + nested + b"}")

    def decode_probe(*_args, **_kwargs):
        raise AssertionError("excessive nesting reached JSON decoding")

    monkeypatch.setattr(store_module.json, "loads", decode_probe)
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)


def test_depth_boundary_reaches_decoder_but_next_level_does_not(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    real_loads = json.loads
    calls: list[int] = []
    nested = b"[" * 63 + b"0" + b"]" * 63
    raw = b'{"checksum":"' + b"0" * 64 + b'","generation":0,"schema_version":1,"task_run":'
    _write_record(store_root, raw + nested + b"}")

    def recording_loads(*args, **kwargs):
        calls.append(1)
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(store_module.json, "loads", recording_loads)
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)
    assert calls == [1]


@pytest.mark.parametrize(
    "hostile",
    [
        [None] * 9000,
        "x" * 65537,
    ],
    ids=("node-flood", "string-budget"),
)
def test_post_parse_resource_budgets_precede_task_run_decode(
    store: TaskRunStore, store_root: Path, monkeypatch, hostile
):
    body = {
        "generation": 0,
        "schema_version": 1,
        "task_run": {**_task().to_mapping(), "hostile": hostile},
    }
    _write_record(store_root, _record_from_body(body))

    def decode_probe(_value):
        raise AssertionError("resource flood reached TaskRun decoding")

    monkeypatch.setattr(store_module.TaskRun, "from_mapping", decode_probe)
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)


def test_json_resource_budget_boundaries_are_exact():
    assert store_module._MAX_JSON_NODES == 8192
    assert store_module._MAX_JSON_STRING_BYTES == 65536
    assert store_module._validate_json_resources([None] * 8191) is None
    with pytest.raises(TaskStoreError) as caught:
        store_module._validate_json_resources([None] * 8192)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)
    assert store_module._validate_json_resources("x" * 65536) is None
    with pytest.raises(TaskStoreError) as caught:
        store_module._validate_json_resources("x" * 65537)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)


def test_huge_integer_and_json_recursion_errors_map_to_corrupt(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    huge = b"9" * 5000
    task_raw = _canonical(_task().to_mapping())
    body_raw = b'{"generation":' + huge + b',"schema_version":1,"task_run":' + task_raw + b"}"
    _write_record(store_root, _record_from_raw_body(body_raw))
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)
    _write_record(store_root, _record_bytes(_task()))

    def recurse(*_args, **_kwargs):
        raise RecursionError("parser recursion sentinel")

    monkeypatch.setattr(store_module.json, "loads", recurse)
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)


def test_exact_record_cap_is_not_classified_as_too_large(store: TaskRunStore, store_root: Path):
    _write_record(store_root, b" " * MAX_RECORD_BYTES)
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)


@pytest.mark.parametrize("operation", ["create", "cas"])
def test_encoded_record_cap_precedes_lease_and_storage(
    store: TaskRunStore, store_root: Path, monkeypatch, operation: str
):
    if operation == "cas":
        store.create(_task())
    touched: list[str] = []
    original = _task().to_mapping()
    old_bytes = (store_root / _record_name()).read_bytes() if operation == "cas" else None

    def oversized_mapping(_task_run):
        return {**original, "oversized": "x" * MAX_RECORD_BYTES}

    def lease_probe(*_args, **_kwargs):
        touched.append("lease")
        raise AssertionError("oversized encoding reached lease")

    monkeypatch.setattr(store_module.TaskRun, "to_mapping", oversized_mapping)
    monkeypatch.setattr(ResourceLeaseManager, "acquire", lease_probe)
    with pytest.raises(TaskStoreError) as caught:
        if operation == "create":
            store.create(_task())
        else:
            store.compare_and_set(TASK_ID, 0, _task())
    _assert_error(caught, TaskStoreErrorCode.RECORD_TOO_LARGE)
    assert touched == []
    if operation == "create":
        assert list(store_root.iterdir()) == []
    else:
        assert (store_root / _record_name()).read_bytes() == old_bytes


@pytest.mark.parametrize(
    "native_error", [KeyError("secret"), TypeError("secret"), ValueError("secret")]
)
def test_task_run_decode_errors_are_mapped_and_redacted(
    store: TaskRunStore, store_root: Path, monkeypatch, native_error: Exception
):
    _write_record(store_root, _record_bytes(_task()))

    def fail_decode(_value):
        raise native_error

    monkeypatch.setattr(store_module.TaskRun, "from_mapping", fail_decode)
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    error = _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)
    assert "secret" not in str(error)


def test_checksum_failure_precedes_task_run_decode(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    decoded = json.loads(_record_bytes(_task()))
    decoded["checksum"] = "0" * 64
    _write_record(store_root, _canonical(decoded))

    def decode_probe(_value):
        raise AssertionError("checksum mismatch reached TaskRun decoding")

    monkeypatch.setattr(store_module.TaskRun, "from_mapping", decode_probe)
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)


def test_task_run_decode_requires_exact_round_trip(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    body = {
        "generation": 0,
        "schema_version": 1,
        "task_run": {**_task().to_mapping(), "ignored_sentinel": "must-not-be-ignored"},
    }
    _write_record(store_root, _record_from_body(body))
    monkeypatch.setattr(store_module.TaskRun, "from_mapping", lambda _value: _task())
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)


def test_loaded_task_id_must_match_selected_record(store: TaskRunStore, store_root: Path):
    _write_record(store_root, _record_bytes(_task(OTHER_TASK_ID)))
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"checksum":"0',
        b'{"checksum":"' + b"0" * 64 + b'","generation":0',
        b'{"checksum":"' + b"0" * 64 + b'","generation":0,"schema_version":1,"task_run":"x',
        b'{"checksum":"' + b"0" * 64 + b'","generation":0,"schema_version":1,"task_run":"\\u00',
        b'{"checksum":"' + b"0" * 64 + b'","generation":0,"schema_version":1,"task_run":"\xc3',
    ],
)
def test_truncated_records_are_rejected(store: TaskRunStore, store_root: Path, raw: bytes):
    _write_record(store_root, raw)
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)


@pytest.mark.parametrize("kind", ["directory", "symlink", "fifo", "hardlink", "mode"])
def test_unsafe_final_entries_are_rejected(
    store: TaskRunStore, store_root: Path, tmp_path: Path, kind: str
):
    path = store_root / _record_name()
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "outside"
        target.write_bytes(_record_bytes(_task()))
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path, mode=0o600)
    elif kind == "hardlink":
        target = tmp_path / "outside"
        target.write_bytes(_record_bytes(_task()))
        target.chmod(0o600)
        os.link(target, path)
    else:
        _write_record(store_root, _record_bytes(_task()))
        path.chmod(0o644)
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)


def test_short_writes_are_completed(store: TaskRunStore, store_root: Path, monkeypatch):
    real_write = store_module.os.write
    calls: list[int] = []

    def short_write(fd: int, data) -> int:
        chunk = bytes(data[: max(1, len(data) // 3)])
        calls.append(len(chunk))
        return real_write(fd, chunk)

    monkeypatch.setattr(store_module.os, "write", short_write)
    assert store.create(_task()).generation == 0
    assert len(calls) > 1
    assert (store_root / _record_name()).read_bytes() == _record_bytes(_task())


def test_zero_length_write_fails_without_publishing(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    monkeypatch.setattr(store_module.os, "write", lambda _fd, _data: 0)
    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.IO_ERROR)
    assert not (store_root / _record_name()).exists()
    assert list(store_root.iterdir()) == []


@pytest.mark.parametrize("operation", ["write", "fsync", "replace"])
def test_precommit_failures_preserve_old_or_absent_record_and_cleanup_temp(
    store: TaskRunStore, store_root: Path, monkeypatch, operation: str
):
    real_fsync = store_module.os.fsync

    def fail(*_args, **_kwargs):
        raise OSError("injected precommit failure")

    def fail_regular_fsync(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("injected precommit failure")
        real_fsync(fd)

    injected = fail_regular_fsync if operation == "fsync" else fail
    monkeypatch.setattr(store_module.os, operation, injected)
    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.IO_ERROR)
    assert not (store_root / _record_name()).exists()
    assert list(store_root.iterdir()) == []


@pytest.mark.parametrize("operation", ["write", "fsync", "replace"])
def test_cas_precommit_failures_preserve_old_record(
    store: TaskRunStore, store_root: Path, monkeypatch, operation: str
):
    store.create(_task())
    path = store_root / _record_name()
    before = path.read_bytes()

    def fail(*_args, **_kwargs):
        raise OSError("injected CAS failure")

    monkeypatch.setattr(store_module.os, operation, fail)
    with pytest.raises(TaskStoreError) as caught:
        store.compare_and_set(TASK_ID, 0, _task())
    _assert_error(caught, TaskStoreErrorCode.IO_ERROR)
    assert path.read_bytes() == before
    assert [item.name for item in store_root.iterdir()] == [_record_name()]


@pytest.mark.parametrize("has_old", [False, True], ids=("create", "cas"))
def test_temp_open_failure_preserves_absent_or_old_final(
    store: TaskRunStore, store_root: Path, monkeypatch, has_old: bool
):
    if has_old:
        store.create(_task())
    path = store_root / _record_name()
    before = path.read_bytes() if has_old else None
    real_open = store_module.os.open

    def fail_temp_open(name, flags, mode=0o777, *, dir_fd=None):
        if os.fsdecode(os.fspath(name)).endswith(".tmp"):
            raise OSError("temp open sentinel")
        return real_open(name, flags, mode, dir_fd=dir_fd)

    _patch_dir_fd_callable(monkeypatch, "open", fail_temp_open)
    with pytest.raises(TaskStoreError) as caught:
        if has_old:
            store.compare_and_set(TASK_ID, 0, _task())
        else:
            store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.IO_ERROR)
    assert (path.read_bytes() if has_old else None) == before
    assert not path.exists() if not has_old else path.exists()


@pytest.mark.parametrize("has_old", [False, True], ids=("create", "cas"))
def test_partial_write_then_failure_preserves_absent_or_old_final(
    store: TaskRunStore, store_root: Path, monkeypatch, has_old: bool
):
    if has_old:
        store.create(_task())
    path = store_root / _record_name()
    before = path.read_bytes() if has_old else None
    real_write = store_module.os.write
    calls = 0

    def partial_then_fail(fd: int, data) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, bytes(data[:7]))
        raise OSError("write continuation sentinel")

    monkeypatch.setattr(store_module.os, "write", partial_then_fail)
    with pytest.raises(TaskStoreError) as caught:
        if has_old:
            store.compare_and_set(TASK_ID, 0, _task())
        else:
            store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.IO_ERROR)
    assert calls == 2
    assert (path.read_bytes() if has_old else None) == before
    assert not path.exists() if not has_old else path.exists()


@pytest.mark.parametrize("has_old", [False, True], ids=("create", "cas"))
def test_temp_close_failure_prevents_replace(
    store: TaskRunStore, store_root: Path, monkeypatch, has_old: bool
):
    if has_old:
        store.create(_task())
    path = store_root / _record_name()
    before = path.read_bytes() if has_old else None
    real_open = store_module.os.open
    real_close = store_module.os.close
    temp_fds: set[int] = set()
    failed: set[int] = set()

    def recording_open(name, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(name, flags, mode, dir_fd=dir_fd)
        if os.fsdecode(os.fspath(name)).endswith(".tmp"):
            temp_fds.add(fd)
        return fd

    def fail_temp_close(fd: int) -> None:
        if fd in temp_fds and fd not in failed:
            failed.add(fd)
            real_close(fd)
            raise OSError("temp close sentinel")
        real_close(fd)

    _patch_dir_fd_callable(monkeypatch, "open", recording_open)
    monkeypatch.setattr(store_module.os, "close", fail_temp_close)
    with pytest.raises(TaskStoreError) as caught:
        if has_old:
            store.compare_and_set(TASK_ID, 0, _task())
        else:
            store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.IO_ERROR)
    assert failed
    assert (path.read_bytes() if has_old else None) == before
    assert not path.exists() if not has_old else path.exists()
    assert [item.name for item in store_root.iterdir()] == ([] if not has_old else [_record_name()])


def test_pre_replace_create_revalidation_does_not_overwrite_new_final(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    path = store_root / _record_name()
    real_fsync = store_module.os.fsync
    injected = False

    def publish_external_final(fd: int) -> None:
        nonlocal injected
        real_fsync(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode) and not injected:
            injected = True
            _write_record(store_root, _record_bytes(_task(), generation=7))

    monkeypatch.setattr(store_module.os, "fsync", publish_external_final)
    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.ALREADY_EXISTS)
    assert path.read_bytes() == _record_bytes(_task(), generation=7)


def test_pre_replace_cas_revalidation_does_not_overwrite_changed_final(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    store.create(_task())
    path = store_root / _record_name()
    real_fsync = store_module.os.fsync
    injected = False

    def publish_external_final(fd: int) -> None:
        nonlocal injected
        real_fsync(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode) and not injected:
            injected = True
            _write_record(store_root, _record_bytes(_task(), generation=7))

    monkeypatch.setattr(store_module.os, "fsync", publish_external_final)
    with pytest.raises(TaskStoreError) as caught:
        store.compare_and_set(TASK_ID, 0, _task())
    _assert_error(caught, TaskStoreErrorCode.CONFLICT)
    assert path.read_bytes() == _record_bytes(_task(), generation=7)


def test_cleanup_failure_does_not_replace_primary_error(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    monkeypatch.setattr(store_module.os, "write", lambda _fd, _data: 0)

    def fail_cleanup(*_args, **_kwargs):
        raise OSError("cleanup unlink sentinel")

    _patch_dir_fd_callable(monkeypatch, "unlink", fail_cleanup)
    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    error = _assert_error(caught, TaskStoreErrorCode.IO_ERROR)
    assert "cleanup unlink sentinel" not in str(error)
    assert not (store_root / _record_name()).exists()
    assert [path.name for path in store_root.iterdir()] == [MUTATION_JOURNAL_NAME]


def test_precommit_primary_error_is_not_changed_by_release_failure(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    real_release = ResourceLease.release

    def release_then_fail(lease, *, owner_token: str):
        real_release(lease, owner_token=owner_token)
        raise LeaseError(LeaseErrorCode.IO_ERROR)

    monkeypatch.setattr(store_module.os, "write", lambda _fd, _data: 0)
    monkeypatch.setattr(ResourceLease, "release", release_then_fail)
    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    error = _assert_error(caught, TaskStoreErrorCode.IO_ERROR)
    assert not hasattr(error, "committed_generation")
    assert list(store_root.iterdir()) == []


def test_all_operations_use_one_canonical_lease_key(store: TaskRunStore, monkeypatch):
    real_acquire = ResourceLeaseManager.acquire
    resources: list[str] = []

    def recording_acquire(manager, resource_id: str):
        resources.append(resource_id)
        return real_acquire(manager, resource_id)

    monkeypatch.setattr(ResourceLeaseManager, "acquire", recording_acquire)
    store.create(_task())
    store.load(TASK_ID)
    store.compare_and_set(TASK_ID, 0, _task())
    assert resources == [
        "task-store:catalog",
        f"task-store:{TASK_ID}",
        f"task-store:{TASK_ID}",
        "task-store:catalog",
        f"task-store:{TASK_ID}",
    ]


def test_lease_acquisition_failure_precedes_store_mutation(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    def contend(_manager, _resource_id: str):
        raise LeaseError(LeaseErrorCode.CONTENDED)

    monkeypatch.setattr(ResourceLeaseManager, "acquire", contend)
    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.LOCK_UNAVAILABLE)
    assert list(store_root.iterdir()) == []


def test_temp_open_is_same_directory_exclusive_private_and_noninheritable(
    store: TaskRunStore, monkeypatch
):
    real_open = store_module.os.open
    observed: list[tuple[str, int, int, int | None, bool]] = []

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        text = os.fsdecode(os.fspath(path))
        if text.endswith(".tmp") and flags & os.O_CREAT:
            observed.append((text, flags, mode, dir_fd, os.get_inheritable(fd)))
        return fd

    _patch_dir_fd_callable(monkeypatch, "open", recording_open)
    store.create(_task())
    assert len(observed) == 1
    name, flags, mode, dir_fd, inheritable = observed[0]
    required = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    assert flags & required == required
    assert flags & (os.O_TRUNC | os.O_APPEND) == 0
    assert mode == 0o600
    assert dir_fd is not None
    assert not Path(name).is_absolute()
    assert inheritable is False


def test_precommit_cleanup_preserves_foreign_temp(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    foreign = store_root / ".foreign.tmp"
    foreign.write_bytes(b"foreign-sentinel")
    foreign.chmod(0o600)
    monkeypatch.setattr(store_module.os, "write", lambda _fd, _data: 0)
    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    assert foreign.read_bytes() == b"foreign-sentinel"
    assert [item.name for item in store_root.iterdir()] == [foreign.name]


def test_temp_name_collision_is_not_overwritten_or_deleted(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    token = "a" * 32
    monkeypatch.setattr(store_module.secrets, "token_hex", lambda _size: token)
    temp = store_root / f".{_record_name()}.{token}.tmp"
    temp.write_bytes(b"foreign-collision")
    temp.chmod(0o600)
    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    assert temp.read_bytes() == b"foreign-collision"
    assert not (store_root / _record_name()).exists()


def test_cleanup_does_not_unlink_replacement_at_owned_temp_name(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    token = "b" * 32
    temp = store_root / f".{_record_name()}.{token}.tmp"
    moved = store_root / "owned-temp-moved-aside"
    real_open = store_module.os.open
    real_write = store_module.os.write
    temp_fds: set[int] = set()
    injected = False

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if os.fsdecode(os.fspath(path)).endswith(".tmp") and flags & os.O_CREAT:
            temp_fds.add(fd)
        return fd

    def replace_temp_then_fail(fd: int, data) -> int:
        nonlocal injected
        if fd in temp_fds and not injected:
            injected = True
            temp.rename(moved)
            temp.write_bytes(b"foreign-replacement")
            temp.chmod(0o600)
            raise OSError("injected replacement race")
        return real_write(fd, data)

    monkeypatch.setattr(store_module.secrets, "token_hex", lambda _size: token)
    _patch_dir_fd_callable(monkeypatch, "open", recording_open)
    monkeypatch.setattr(store_module.os, "write", replace_temp_then_fail)
    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.IO_ERROR)
    assert temp.read_bytes() == b"foreign-replacement"
    assert moved.exists()
    assert not (store_root / _record_name()).exists()


def test_root_identity_replacement_after_construction_is_rejected(
    store: TaskRunStore, store_root: Path
):
    original = store_root.with_name("original-task-store")
    store_root.rename(original)
    store_root.mkdir(mode=0o700)
    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)
    assert list(store_root.iterdir()) == []
    assert list(original.iterdir()) == []


def test_root_replacement_during_transaction_is_rejected(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    moved = store_root.with_name("moved-during-write")
    real_fsync = store_module.os.fsync
    injected = False

    def replace_root_after_temp_fsync(fd: int) -> None:
        nonlocal injected
        real_fsync(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode) and not injected:
            injected = True
            store_root.rename(moved)
            store_root.mkdir(mode=0o700)

    monkeypatch.setattr(store_module.os, "fsync", replace_root_after_temp_fsync)
    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)
    assert list(store_root.iterdir()) == []
    assert not (moved / _record_name()).exists()


def test_final_name_replacement_after_open_is_rejected(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    store.create(_task())
    path = store_root / _record_name()
    moved = store_root / "opened-record-moved"
    real_open = store_module.os.open
    injected = False

    def replace_after_open(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal injected
        fd = real_open(name, flags, mode, dir_fd=dir_fd)
        if os.fsdecode(os.fspath(name)) == _record_name() and not injected:
            injected = True
            path.rename(moved)
            _write_record(store_root, _record_bytes(_task(), generation=7))
        return fd

    _patch_dir_fd_callable(monkeypatch, "open", replace_after_open)
    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)
    assert path.read_bytes() == _record_bytes(_task(), generation=7)
    assert moved.read_bytes() == _record_bytes(_task(), generation=0)


def test_runtime_root_and_final_mode_changes_are_rejected(store: TaskRunStore, store_root: Path):
    store_root.chmod(0o755)
    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)
    store_root.chmod(0o700)
    store.create(_task())
    final = store_root / _record_name()
    final.chmod(0o644)
    with pytest.raises(TaskStoreError) as caught:
        store.compare_and_set(TASK_ID, 0, _task())
    _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)
    assert stat.S_IMODE(final.stat().st_mode) == 0o644


@pytest.mark.parametrize("target", ["root", "final", "temp"])
def test_wrong_runtime_uid_is_rejected_before_publish(
    store: TaskRunStore, store_root: Path, monkeypatch, target: str
):
    if target == "final":
        store.create(_task())
    final = store_root / _record_name()
    root_identity = (store_root.stat().st_dev, store_root.stat().st_ino)
    final_identity = None
    if final.exists():
        final_identity = (final.stat().st_dev, final.stat().st_ino)
    real_open = store_module.os.open
    real_fstat = store_module.os.fstat
    temp_fds: set[int] = set()

    def recording_open(name, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(name, flags, mode, dir_fd=dir_fd)
        if os.fsdecode(os.fspath(name)).endswith(".tmp"):
            temp_fds.add(fd)
        return fd

    def wrong_fstat(fd: int):
        result = real_fstat(fd)
        identity = (result.st_dev, result.st_ino)
        wrong = (
            (target == "root" and identity == root_identity)
            or (target == "final" and identity == final_identity)
            or (target == "temp" and fd in temp_fds)
        )
        return _with_wrong_uid(result) if wrong else result

    _patch_dir_fd_callable(monkeypatch, "open", recording_open)
    monkeypatch.setattr(store_module.os, "fstat", wrong_fstat)
    with pytest.raises(TaskStoreError) as caught:
        if target == "final":
            store.load(TASK_ID)
        else:
            store.create(_task())
    _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)
    if target == "final":
        assert final.read_bytes() == _record_bytes(_task())
    else:
        assert not final.exists()


@pytest.mark.parametrize(
    ("target", "probe", "operation"),
    [
        ("root", "fstat-first", "load"),
        ("root", "inheritable", "load"),
        ("final", "fstat-first", "load"),
        ("final", "inheritable", "load"),
        ("final", "fstat-second", "load"),
        ("final", "fstat-second", "cas"),
    ],
)
def test_root_and_final_probe_failures_close_fds_and_release_lease(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
    target: str,
    probe: str,
    operation: str,
):
    store.create(_task())
    final = store_root / _record_name()
    root_identity = (store_root.stat().st_dev, store_root.stat().st_ino)
    final_identity = (final.stat().st_dev, final.stat().st_ino)
    target_identity = root_identity if target == "root" else final_identity
    real_fstat = store_module.os.fstat
    real_inheritable = store_module.os.get_inheritable
    owned_fds: set[int] = set()
    target_calls = 0
    injected = False

    def recording_fstat(fd: int):
        nonlocal injected, target_calls
        result = real_fstat(fd)
        identity = (result.st_dev, result.st_ino)
        if identity in (root_identity, final_identity):
            owned_fds.add(fd)
        if identity == target_identity:
            target_calls += 1
            expected_call = 1 if probe == "fstat-first" else 2
            if probe.startswith("fstat-") and target_calls == expected_call:
                injected = True
                raise OSError("metadata probe sentinel")
        return result

    def failing_inheritable(fd: int) -> bool:
        nonlocal injected
        result = real_fstat(fd)
        if (result.st_dev, result.st_ino) == target_identity:
            injected = True
            raise OSError("inheritability probe sentinel")
        return real_inheritable(fd)

    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "fstat", recording_fstat)
        if probe == "inheritable":
            patch.setattr(store_module.os, "get_inheritable", failing_inheritable)
        with pytest.raises(TaskStoreError) as caught:
            if operation == "cas":
                store.compare_and_set(TASK_ID, 0, _task())
            else:
                store.load(TASK_ID)
        _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)
    assert injected
    assert owned_fds
    for fd in owned_fds:
        with pytest.raises(OSError):
            real_fstat(fd)
    assert store.load(TASK_ID) == StoredTaskRun(generation=0, task_run=_task())


@pytest.mark.parametrize("probe", ["fstat", "inheritable"])
def test_temp_probe_failures_close_fds_cleanup_when_identified_and_release_lease(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
    probe: str,
):
    real_open = store_module.os.open
    real_fstat = store_module.os.fstat
    real_inheritable = store_module.os.get_inheritable
    temp_fds: set[int] = set()
    injected = False

    def recording_open(name, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(name, flags, mode, dir_fd=dir_fd)
        if os.fsdecode(os.fspath(name)).endswith(".tmp"):
            temp_fds.add(fd)
        return fd

    def failing_fstat(fd: int):
        nonlocal injected
        if fd in temp_fds and not injected:
            injected = True
            raise OSError("temp fstat sentinel")
        return real_fstat(fd)

    def failing_inheritable(fd: int) -> bool:
        nonlocal injected
        if fd in temp_fds and not injected:
            injected = True
            raise OSError("temp inheritable sentinel")
        return real_inheritable(fd)

    with monkeypatch.context() as patch:
        _patch_dir_fd_callable(patch, "open", recording_open)
        if probe == "fstat":
            patch.setattr(store_module.os, "fstat", failing_fstat)
        else:
            patch.setattr(store_module.os, "get_inheritable", failing_inheritable)
        with pytest.raises(TaskStoreError) as caught:
            store.create(_task())
        _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)
    assert injected
    assert temp_fds
    for fd in temp_fds:
        with pytest.raises(OSError):
            real_fstat(fd)
    assert not (store_root / _record_name()).exists()
    residue = [path for path in store_root.iterdir() if path.name.endswith(".tmp")]
    assert len(residue) == (1 if probe == "fstat" else 0)
    if probe == "fstat":
        assert (store_root / MUTATION_JOURNAL_NAME).exists()
        with pytest.raises(TaskStoreError) as caught:
            store.create(_task())
        _assert_error(caught, TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    else:
        assert store.create(_task()) == StoredTaskRun(generation=0, task_run=_task())


def test_mutation_initial_record_failure_closes_owned_fds_and_releases_lease(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
):
    store.create(_task())
    final = _write_record(store_root, b"{")
    root_identity = (store_root.stat().st_dev, store_root.stat().st_ino)
    final_identity = (final.stat().st_dev, final.stat().st_ino)
    real_open = store_module.os.open
    real_fstat = store_module.os.fstat
    owned_fds: set[int] = set()

    def recording_open(name, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(name, flags, mode, dir_fd=dir_fd)
        result = real_fstat(fd)
        if (result.st_dev, result.st_ino) in (root_identity, final_identity):
            owned_fds.add(fd)
        return fd

    with monkeypatch.context() as patch:
        _patch_dir_fd_callable(patch, "open", recording_open)
        with pytest.raises(TaskStoreError) as caught:
            store.compare_and_set(TASK_ID, 0, _task())
        _assert_error(caught, TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    assert owned_fds
    for fd in owned_fds:
        with pytest.raises(OSError):
            real_fstat(fd)
    _write_record(store_root, _record_bytes(_task()))
    assert store.load(TASK_ID) == StoredTaskRun(generation=0, task_run=_task())


def test_mutation_initial_read_error_closes_owned_fds_preserves_record_and_releases_lease(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
):
    store.create(_task())
    final = store_root / _record_name()
    before = final.read_bytes()
    root_identity = (store_root.stat().st_dev, store_root.stat().st_ino)
    final_identity = (final.stat().st_dev, final.stat().st_ino)
    real_open = store_module.os.open
    real_read = store_module.os.read
    real_fstat = store_module.os.fstat
    owned_fds: set[int] = set()
    final_fds: set[int] = set()
    injected = False

    def recording_open(name, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(name, flags, mode, dir_fd=dir_fd)
        result = real_fstat(fd)
        identity = (result.st_dev, result.st_ino)
        if identity in (root_identity, final_identity):
            owned_fds.add(fd)
        if identity == final_identity:
            final_fds.add(fd)
        return fd

    def failing_read(fd: int, size: int) -> bytes:
        nonlocal injected
        if fd in final_fds and not injected:
            injected = True
            raise OSError("initial read sentinel")
        return real_read(fd, size)

    with monkeypatch.context() as patch:
        _patch_dir_fd_callable(patch, "open", recording_open)
        patch.setattr(store_module.os, "read", failing_read)
        with pytest.raises(TaskStoreError) as caught:
            store.compare_and_set(TASK_ID, 0, _task())
        _assert_error(caught, TaskStoreErrorCode.IO_ERROR)
    assert injected
    assert final.read_bytes() == before
    assert owned_fds
    for fd in owned_fds:
        with pytest.raises(OSError):
            real_fstat(fd)
    assert store.load(TASK_ID) == StoredTaskRun(generation=0, task_run=_task())


def test_cleanup_stat_failure_preserves_primary_closes_root_and_releases_lease(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
):
    real_stat = store_module.os.stat

    def fail_temp_stat(path, *, dir_fd=None, follow_symlinks=True):
        if os.fsdecode(os.fspath(path)).endswith(".tmp"):
            raise OSError("cleanup stat sentinel")
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "write", lambda _fd, _data: 0)
        _patch_dir_fd_callable(
            patch,
            "stat",
            fail_temp_stat,
            follow_symlinks=True,
        )
        with pytest.raises(TaskStoreError) as caught:
            store.create(_task())
        _assert_error(caught, TaskStoreErrorCode.IO_ERROR)
    assert not (store_root / _record_name()).exists()
    assert len([path for path in store_root.iterdir() if path.name.endswith(".tmp")]) == 0
    assert store.create(_task()) == StoredTaskRun(generation=0, task_run=_task())


def test_unverified_platform_and_missing_no_follow_capability_fail_closed(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    with monkeypatch.context() as patch:
        patch.setattr(store_module.sys, "platform", "win32")
        with pytest.raises(TaskStoreError) as caught:
            store.create(_task())
        _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)
    with monkeypatch.context() as patch:
        patch.delattr(store_module.os, "O_NOFOLLOW")
        with pytest.raises(TaskStoreError) as caught:
            store.create(_task())
        _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)
    assert list(store_root.iterdir()) == []


@pytest.mark.parametrize(
    "missing",
    [
        "O_RDONLY",
        "O_WRONLY",
        "O_CREAT",
        "O_EXCL",
        "O_NOFOLLOW",
        "O_CLOEXEC",
        "O_DIRECTORY",
        "open",
        "stat",
        "fstat",
        "write",
        "read",
        "fsync",
        "ftruncate",
        "scandir",
        "replace",
        "unlink",
        "close",
        "geteuid",
        "get_inheritable",
        "supports_dir_fd",
        "supports_follow_symlinks",
    ],
)
def test_each_missing_storage_capability_fails_before_lease_or_storage(
    store: TaskRunStore, store_root: Path, monkeypatch, missing: str
):
    touched: list[str] = []

    def lease_probe(*_args, **_kwargs):
        touched.append("lease")
        raise AssertionError("missing capability reached lease")

    with monkeypatch.context() as patch:
        patch.setattr(ResourceLeaseManager, "acquire", lease_probe)
        patch.delattr(store_module.os, missing)
        with pytest.raises(TaskStoreError) as caught:
            store.create(_task())
        _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)
    assert touched == []
    assert list(store_root.iterdir()) == []


@pytest.mark.parametrize(
    "invalid",
    [
        ("O_EXCL", True),
        ("O_CLOEXEC", object()),
        ("write", None),
        ("ftruncate", None),
        ("scandir", None),
        ("replace", "not-callable"),
        ("supports_dir_fd", ()),
        ("supports_follow_symlinks", frozenset()),
    ],
)
def test_storage_capability_types_are_exact_and_fail_closed(
    store: TaskRunStore, store_root: Path, monkeypatch, invalid
):
    name, value = invalid
    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, name, value)
        with pytest.raises(TaskStoreError) as caught:
            store.create(_task())
        _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)
    assert list(store_root.iterdir()) == []


@pytest.mark.parametrize("missing_membership", ["open", "stat-dir", "stat-follow", "unlink"])
def test_required_dir_fd_and_no_follow_memberships_fail_closed(
    store: TaskRunStore, store_root: Path, monkeypatch, missing_membership: str
):
    dir_fd = set(store_module.os.supports_dir_fd)
    follow = set(store_module.os.supports_follow_symlinks)
    if missing_membership == "open":
        dir_fd.discard(store_module.os.open)
    elif missing_membership == "stat-dir":
        dir_fd.discard(store_module.os.stat)
    elif missing_membership == "stat-follow":
        follow.discard(store_module.os.stat)
    else:
        dir_fd.discard(store_module.os.unlink)
    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "supports_dir_fd", dir_fd)
        patch.setattr(store_module.os, "supports_follow_symlinks", follow)
        with pytest.raises(TaskStoreError) as caught:
            store.create(_task())
        _assert_error(caught, TaskStoreErrorCode.UNSAFE_STORE)
    assert list(store_root.iterdir()) == []


def test_directory_and_final_open_calls_are_relative_fail_closed_and_noninheritable(
    store: TaskRunStore, monkeypatch
):
    store.create(_task())
    real_open = store_module.os.open
    calls: list[tuple[str, int, int | None, int, bool]] = []

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        opened_mode = os.fstat(fd).st_mode
        calls.append(
            (
                os.fsdecode(os.fspath(path)),
                flags,
                dir_fd,
                opened_mode,
                os.get_inheritable(fd),
            )
        )
        return fd

    _patch_dir_fd_callable(monkeypatch, "open", recording_open)
    store.load(TASK_ID)
    directory_calls = [call for call in calls if stat.S_ISDIR(call[3])]
    assert directory_calls
    required_dir = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    assert all(flags & required_dir == required_dir for _, flags, _, _, _ in directory_calls)
    assert all(not inheritable for _, _, _, _, inheritable in directory_calls)
    assert all(
        path == "/" if dir_fd is None else not Path(path).is_absolute()
        for path, _, dir_fd, _, _ in directory_calls
    )
    final_calls = [call for call in calls if call[0] == _record_name() and stat.S_ISREG(call[3])]
    assert final_calls
    required_final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    assert all(flags & required_final == required_final for _, flags, _, _, _ in final_calls)
    assert all(flags & (os.O_CREAT | os.O_TRUNC) == 0 for _, flags, _, _, _ in final_calls)
    assert all(dir_fd is not None for _, _, dir_fd, _, _ in final_calls)
    assert all(not inheritable for _, _, _, _, inheritable in final_calls)


def test_directory_fsync_failure_reports_committed_generation(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    real_fsync = store_module.os.fsync
    store_identity = (store_root.stat().st_dev, store_root.stat().st_ino)
    directory_calls = 0

    def fail_directory_fsync(fd: int) -> None:
        nonlocal directory_calls
        mode = os.fstat(fd).st_mode
        is_store_directory = (
            stat.S_ISDIR(mode)
            and (
                os.fstat(fd).st_dev,
                os.fstat(fd).st_ino,
            )
            == store_identity
        )
        if is_store_directory:
            directory_calls += 1
        if directory_calls == 2 and is_store_directory:
            raise OSError("directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(store_module.os, "fsync", fail_directory_fsync)
    with pytest.raises(TaskStoreError) as caught:
        store.create(_task())
    error = _assert_error(caught, TaskStoreErrorCode.DURABILITY_UNCERTAIN)
    assert error.committed_generation == 0
    assert (store_root / _record_name()).read_bytes() == _record_bytes(_task())


def test_release_failure_after_replace_reports_durability_uncertain(
    store: TaskRunStore, monkeypatch
):
    real_release = ResourceLease.release

    def release_then_fail(lease, *, owner_token: str):
        real_release(lease, owner_token=owner_token)
        raise LeaseError(LeaseErrorCode.IO_ERROR)

    with monkeypatch.context() as patch:
        patch.setattr(ResourceLease, "release", release_then_fail)
        with pytest.raises(TaskStoreError) as caught:
            store.create(_task())
    error = _assert_error(caught, TaskStoreErrorCode.DURABILITY_UNCERTAIN)
    assert error.committed_generation == 0
    assert store.load(TASK_ID) == StoredTaskRun(generation=0, task_run=_task())


def test_cas_directory_fsync_failure_reports_new_generation(
    store: TaskRunStore,
    store_root: Path,
    monkeypatch,
):
    store.create(_task())
    real_fsync = store_module.os.fsync
    store_identity = (store_root.stat().st_dev, store_root.stat().st_ino)
    directory_calls = 0

    def fail_directory_fsync(fd: int) -> None:
        nonlocal directory_calls
        mode = os.fstat(fd).st_mode
        is_store_directory = (
            stat.S_ISDIR(mode)
            and (
                os.fstat(fd).st_dev,
                os.fstat(fd).st_ino,
            )
            == store_identity
        )
        if is_store_directory:
            directory_calls += 1
        if directory_calls == 2 and is_store_directory:
            raise OSError("CAS directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(store_module.os, "fsync", fail_directory_fsync)
    with pytest.raises(TaskStoreError) as caught:
        store.compare_and_set(TASK_ID, 0, _task())
    error = _assert_error(caught, TaskStoreErrorCode.DURABILITY_UNCERTAIN)
    assert error.committed_generation == 1


def test_cas_release_failure_after_replace_reports_new_generation(store: TaskRunStore, monkeypatch):
    store.create(_task())
    real_release = ResourceLease.release

    def release_then_fail(lease, *, owner_token: str):
        real_release(lease, owner_token=owner_token)
        raise LeaseError(LeaseErrorCode.IO_ERROR)

    with monkeypatch.context() as patch:
        patch.setattr(ResourceLease, "release", release_then_fail)
        with pytest.raises(TaskStoreError) as caught:
            store.compare_and_set(TASK_ID, 0, _task())
    error = _assert_error(caught, TaskStoreErrorCode.DURABILITY_UNCERTAIN)
    assert error.committed_generation == 1
    assert store.load(TASK_ID).generation == 1


def test_two_threads_create_one_record(store: TaskRunStore):
    barrier = threading.Barrier(3)
    results: list[tuple[str, object]] = []

    def worker() -> None:
        barrier.wait()
        try:
            results.append(("ok", store.create(_task())))
        except TaskStoreError as exc:
            results.append(("error", exc.code))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert sum(kind == "ok" for kind, _ in results) == 1
    assert {value for kind, value in results if kind == "error"} <= {
        TaskStoreErrorCode.ALREADY_EXISTS,
        TaskStoreErrorCode.LOCK_UNAVAILABLE,
    }
    assert store.load(TASK_ID).generation == 0


def test_two_threads_compare_and_set_have_one_winner(store: TaskRunStore):
    store.create(_task())
    barrier = threading.Barrier(3)
    results: list[tuple[str, object]] = []

    def worker() -> None:
        barrier.wait()
        try:
            results.append(("ok", store.compare_and_set(TASK_ID, 0, _task())))
        except TaskStoreError as exc:
            results.append(("error", exc.code))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert sum(kind == "ok" for kind, _ in results) == 1
    assert {value for kind, value in results if kind == "error"} <= {
        TaskStoreErrorCode.CONFLICT,
        TaskStoreErrorCode.LOCK_UNAVAILABLE,
    }
    assert store.load(TASK_ID).generation == 1


def test_concurrent_reader_observes_only_complete_old_or_new_records(store: TaskRunStore):
    store.create(_task())
    start = threading.Barrier(2)
    finished = threading.Event()
    reader_observed = threading.Event()
    reader_saw_new = threading.Event()
    yield_reader = threading.Event()
    reader_yielded = threading.Event()
    observed: list[int] = [store.load(TASK_ID).generation]
    unexpected: list[object] = []

    def reader() -> None:
        try:
            start.wait()
            while not finished.is_set():
                if yield_reader.is_set():
                    reader_yielded.set()
                    while yield_reader.is_set() and not finished.is_set():
                        finished.wait(0.001)
                    continue
                try:
                    generation = store.load(TASK_ID).generation
                    observed.append(generation)
                    reader_observed.set()
                    if generation == 1:
                        reader_saw_new.set()
                except TaskStoreError as exc:
                    if exc.code is not TaskStoreErrorCode.LOCK_UNAVAILABLE:
                        unexpected.append(exc.code)
                        finished.set()
        except Exception as exc:
            unexpected.append(exc)
            finished.set()

    thread = threading.Thread(target=reader)
    thread.start()
    start.wait()
    try:
        assert reader_observed.wait(timeout=5)
        try:
            store.compare_and_set(TASK_ID, 0, _task())
        except TaskStoreError as exc:
            if exc.code is not TaskStoreErrorCode.LOCK_UNAVAILABLE:
                raise
            yield_reader.set()
            assert reader_yielded.wait(timeout=5)
            store.compare_and_set(TASK_ID, 0, _task())
            yield_reader.clear()
        assert reader_saw_new.wait(timeout=5)
    finally:
        yield_reader.clear()
        finished.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert unexpected == []
    observed.append(store.load(TASK_ID).generation)
    assert set(observed) <= {0, 1}
    assert observed[0] == 0
    assert observed[-1] == 1


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="POSIX store only")
def test_two_processes_create_one_record(store_root: Path, lease_root: Path, tmp_path: Path):
    gate = tmp_path / "gate"
    script = """
import sys, time
from pathlib import Path
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager
from vibecad.workflow.state import ReasoningOwner, ReviewPolicy, new_task_run
from vibecad.workflow.store import TaskRunStore, TaskStoreError, TaskStoreRootTrust
root, locks, gate = map(Path, sys.argv[1:])
while not gate.exists():
    time.sleep(0.001)
manager = ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL)
store = TaskRunStore(root, manager, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
task = new_task_run(
    task_id='task_0123456789abcdef0123456789abcdef',
    project_id='project_0123456789abcdef0123456789abcdef',
    base_revision='revision_0123456789abcdef0123456789abcdef',
    reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
    review_policy=ReviewPolicy.AUTO_COMMIT,
)
try:
    store.create(task)
    print('ok')
except TaskStoreError as exc:
    print(exc.code.value)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(store_root), str(lease_root), str(gate)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(2)
    ]
    gate.write_text("go", encoding="utf-8")
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        outputs.append(stdout.strip())
    assert outputs.count("ok") == 1
    assert set(outputs) <= {"ok", "already_exists", "lock_unavailable"}


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="POSIX store only")
def test_cross_process_record_n_plus_one_never_creates_task_locks(
    store: TaskRunStore,
    store_root: Path,
    lease_root: Path,
    tmp_path: Path,
) -> None:
    store.create(_task())
    before_locks = sorted(
        (entry.name, entry.stat().st_ino, entry.stat().st_size) for entry in lease_root.iterdir()
    )
    gate = tmp_path / "capacity-gate"
    script = """
import sys, time
from pathlib import Path
import vibecad.workflow.store as store_module
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager
from vibecad.workflow.state import ReasoningOwner, ReviewPolicy, new_task_run
from vibecad.workflow.store import TaskRunStore, TaskStoreError, TaskStoreRootTrust
root, locks, gate = map(Path, sys.argv[1:4])
task_id = sys.argv[4]
while not gate.exists():
    time.sleep(0.001)
store_module._MAX_TASK_RECORDS = 1
manager = ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL)
store = TaskRunStore(root, manager, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
task = new_task_run(
    task_id=task_id,
    project_id='project_0123456789abcdef0123456789abcdef',
    base_revision='revision_0123456789abcdef0123456789abcdef',
    reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
    review_policy=ReviewPolicy.AUTO_COMMIT,
)
for _attempt in range(100):
    try:
        store.create(task)
        print('unexpected-success')
        break
    except TaskStoreError as exc:
        if exc.code.value == 'lock_unavailable':
            time.sleep(0.001)
            continue
        print(exc.code.value)
        break
else:
    print('retry-exhausted')
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    ids = (
        "task_11111111111111111111111111111111",
        "task_22222222222222222222222222222222",
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(store_root), str(lease_root), str(gate), task_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for task_id in ids
    ]
    gate.write_text("go", encoding="utf-8")
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        outputs.append(stdout.strip())
    assert outputs == ["resource_exhausted", "resource_exhausted"]
    assert [entry.name for entry in store_root.iterdir()] == [_record_name()]
    assert (
        sorted(
            (entry.name, entry.stat().st_ino, entry.stat().st_size)
            for entry in lease_root.iterdir()
        )
        == before_locks
    )


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="POSIX store only")
def test_constructor_accepts_a_safely_contended_fixed_catalog_entry(
    store_root: Path,
    lease_root: Path,
) -> None:
    manager = ResourceLeaseManager(lease_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    held = manager.acquire("task-store:catalog")
    script = """
import sys
from pathlib import Path
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager
from vibecad.workflow.store import TaskRunStore, TaskStoreRootTrust
root, locks = map(Path, sys.argv[1:])
manager = ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL)
TaskRunStore(root, manager, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
print('ok')
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script, str(store_root), str(lease_root)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    finally:
        held.release(owner_token=held.owner_token)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_catalog_then_task_contention_is_bounded_and_releases_catalog(
    store: TaskRunStore,
    lease_root: Path,
) -> None:
    store.create(_task())
    manager = ResourceLeaseManager(lease_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    held = manager.acquire(f"task-store:{TASK_ID}")
    result: list[TaskStoreErrorCode] = []

    def contend() -> None:
        try:
            store.compare_and_set(TASK_ID, 0, _task())
        except TaskStoreError as exc:
            result.append(exc.code)

    thread = threading.Thread(target=contend)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result == [TaskStoreErrorCode.LOCK_UNAVAILABLE]
    created = store.create(_task(OTHER_TASK_ID))
    assert created.task_run.id == OTHER_TASK_ID
    held.release(owner_token=held.owner_token)


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="POSIX store only")
def test_two_processes_compare_and_set_have_one_winner(
    store: TaskRunStore, store_root: Path, lease_root: Path, tmp_path: Path
):
    store.create(_task())
    gate = tmp_path / "cas-gate"
    script = """
import sys, time
from pathlib import Path
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager
from vibecad.workflow.state import ReasoningOwner, ReviewPolicy, new_task_run
from vibecad.workflow.store import TaskRunStore, TaskStoreError, TaskStoreRootTrust
root, locks, gate = map(Path, sys.argv[1:])
while not gate.exists():
    time.sleep(0.001)
manager = ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL)
store = TaskRunStore(root, manager, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
task = new_task_run(
    task_id='task_0123456789abcdef0123456789abcdef',
    project_id='project_0123456789abcdef0123456789abcdef',
    base_revision='revision_0123456789abcdef0123456789abcdef',
    reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
    review_policy=ReviewPolicy.AUTO_COMMIT,
)
try:
    store.compare_and_set(task.id, 0, task)
    print('ok')
except TaskStoreError as exc:
    print(exc.code.value)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(store_root), str(lease_root), str(gate)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(2)
    ]
    gate.write_text("go", encoding="utf-8")
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        outputs.append(stdout.strip())
    assert outputs.count("ok") == 1
    assert set(outputs) <= {"ok", "conflict", "lock_unavailable"}
    assert store.load(TASK_ID).generation == 1


def test_record_paths_are_hashes_and_task_text_never_selects_paths(
    store: TaskRunStore, store_root: Path
):
    store.create(_task())
    names = [path.name for path in store_root.iterdir()]
    assert names == [_record_name()]
    assert TASK_ID not in names[0]
    assert "/" not in names[0]


# S3-5 durable-review persistence REDs.  The helper resolves new state symbols
# lazily so their absence fails a focused assertion instead of test collection.
REVIEW_REVISION = "revision_11111111111111111111111111111111"
REVIEW_DRAFT_ID = "draft_11111111111111111111111111111111"
REVIEW_VERIFICATION_ID = "verification_0123456789abcdef0123456789abcdef"
REVIEW_BASE_GENERATION = 7
REVIEW_BASE_MANIFEST = "c" * 64
REVIEW_MANIFEST = "a" * 64
REVIEW_OBSERVATION = "b" * 64


def _durable_review_task(*, accepting: bool = False) -> TaskRun:
    review_policy = getattr(state_module, "ReviewPolicy", None)
    review_draft = getattr(state_module, "ReviewDraft", None)
    assert review_policy is not None, "S3-5 ReviewPolicy is missing"
    assert review_draft is not None, "S3-5 ReviewDraft is missing"
    task = state_module.new_task_run(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        reasoning_owner=state_module.ReasoningOwner.EXTERNAL_PLAN,
        review_policy=review_policy.REQUIRE_REVIEW,
    )
    task = state_module.transition_task(task, state_module.TaskEvent.REQUEST_PLAN)
    program = ModelProgram(
        task_id=TASK_ID,
        base_revision=BASE_REVISION,
        operations=(),
        acceptance=AcceptanceSpec(id="acceptance-1", criteria=()),
    )
    task = state_module.transition_task(
        task,
        state_module.TaskEvent.SUBMIT_PROGRAM,
        program=program,
    )
    task = state_module.transition_task(task, state_module.TaskEvent.START_VALIDATION)
    task = state_module.transition_task(
        task,
        state_module.TaskEvent.VALIDATE_PROGRAM,
        candidate_revision=REVIEW_REVISION,
    )
    task = state_module.transition_task(task, state_module.TaskEvent.COMPLETE_EXECUTION)
    report = state_module.VerificationReport(
        id=REVIEW_VERIFICATION_ID,
        acceptance_id="acceptance-1",
        candidate_revision=REVIEW_REVISION,
        manifest_sha256=REVIEW_MANIFEST,
        observation_digest=REVIEW_OBSERVATION,
        passed=True,
        verdicts=(
            state_module.CriterionVerdict(
                criterion_id="review-store",
                required=True,
                outcome=state_module.CriterionOutcome.PASS,
                message="Durable review candidate passed",
            ),
        ),
    )
    draft = review_draft(
        id=REVIEW_DRAFT_ID,
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        base_generation=REVIEW_BASE_GENERATION,
        base_manifest_sha256=REVIEW_BASE_MANIFEST,
        revision_id=REVIEW_REVISION,
        manifest_sha256=REVIEW_MANIFEST,
        verification_id=REVIEW_VERIFICATION_ID,
        acceptance_id="acceptance-1",
        observation_digest=REVIEW_OBSERVATION,
    )
    task = state_module.transition_task(
        task,
        state_module.TaskEvent.PREPARE_REVIEW,
        verification=report,
        draft=draft,
    )
    task = state_module.transition_task(task, state_module.TaskEvent.PUBLISH_DRAFT)
    if accepting:
        task = state_module.transition_task(task, state_module.TaskEvent.ACCEPT_DRAFT)
    return task


def _reopened_store(store_root: Path, lease_root: Path) -> TaskRunStore:
    manager = ResourceLeaseManager(lease_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    return TaskRunStore(
        store_root,
        manager,
        trust=TaskStoreRootTrust.TRUSTED_LOCAL,
    )


def test_awaiting_review_and_rejected_decision_round_trip_across_store_restart(
    store: TaskRunStore,
    store_root: Path,
    lease_root: Path,
):
    awaiting = _durable_review_task()
    assert awaiting.status.value == "awaiting_user_review"
    assert store.create(awaiting) == StoredTaskRun(generation=0, task_run=awaiting)

    restarted = _reopened_store(store_root, lease_root)
    loaded = restarted.load(TASK_ID)
    assert loaded == StoredTaskRun(generation=0, task_run=awaiting)
    assert loaded.task_run.draft.id == REVIEW_DRAFT_ID
    assert loaded.task_run.review_policy.value == "require_review"

    rejected = state_module.transition_task(
        loaded.task_run,
        state_module.TaskEvent.REJECT_DRAFT,
    )
    updated = restarted.compare_and_set(TASK_ID, loaded.generation, rejected)
    assert updated == StoredTaskRun(generation=1, task_run=rejected)
    assert updated.task_run.status.value == "rejected"
    assert updated.task_run.last_error is None
    assert updated.task_run.committed_revision is None
    assert _reopened_store(store_root, lease_root).load(TASK_ID) == updated


@pytest.mark.parametrize("missing", ["review_policy", "draft"])
def test_persisted_review_record_requires_exact_policy_and_draft_fields(
    store: TaskRunStore,
    store_root: Path,
    missing: str,
):
    task_mapping = _durable_review_task().to_mapping()
    del task_mapping[missing]
    body = {
        "generation": 0,
        "schema_version": 1,
        "task_run": task_mapping,
    }
    _write_record(store_root, _record_from_body(body))

    with pytest.raises(TaskStoreError) as caught:
        store.load(TASK_ID)
    _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)


@pytest.mark.parametrize(
    "attention_event",
    [state_module.TaskEvent.REQUIRE_RECOVERY, state_module.TaskEvent.REQUIRE_CLEANUP],
)
def test_review_attention_history_survives_restart_and_recovers_by_generation_cas(
    store: TaskRunStore,
    store_root: Path,
    lease_root: Path,
    attention_event,
):
    accepting = _durable_review_task(accepting=True)
    error = StepError(
        category=ErrorCategory.RUNTIME,
        code="review_transaction_uncertain",
        message="Review transaction needs reconciliation",
        retryable=False,
        needs_input=False,
        related_objects=(),
        diagnostic_artifacts=(),
    )
    attention = state_module.transition_task(
        accepting,
        attention_event,
        error=error,
    )
    store.create(attention)

    restarted = _reopened_store(store_root, lease_root)
    loaded = restarted.load(TASK_ID)
    assert loaded.task_run.status in {
        state_module.TaskStatus.RECOVERY_REQUIRED,
        state_module.TaskStatus.CLEANUP_REQUIRED,
    }
    restored = state_module.transition_task(
        loaded.task_run,
        state_module.TaskEvent.CONFIRM_DRAFT_UNCOMMITTED,
    )
    updated = restarted.compare_and_set(TASK_ID, loaded.generation, restored)
    assert updated.generation == 1
    assert updated.task_run.status is state_module.TaskStatus.AWAITING_USER_REVIEW
    assert updated.task_run.draft.id == REVIEW_DRAFT_ID
    assert updated.task_run.last_error is None
    assert _reopened_store(store_root, lease_root).load(TASK_ID) == updated


def test_error_shape_and_committed_generation_are_exact():
    regular = TaskStoreError(TaskStoreErrorCode.NOT_FOUND)
    assert regular.code is TaskStoreErrorCode.NOT_FOUND
    assert not hasattr(regular, "committed_generation")
    uncertain = TaskStoreError(
        TaskStoreErrorCode.DURABILITY_UNCERTAIN,
        committed_generation=0,
    )
    assert uncertain.code is TaskStoreErrorCode.DURABILITY_UNCERTAIN
    assert uncertain.committed_generation == 0
    for invalid in (True, -1, 2**53, "0", None):
        with pytest.raises((TypeError, ValueError)):
            TaskStoreError(
                TaskStoreErrorCode.DURABILITY_UNCERTAIN,
                committed_generation=invalid,
            )
    with pytest.raises(ValueError):
        TaskStoreError(TaskStoreErrorCode.IO_ERROR, committed_generation=0)


def test_source_uses_only_closed_storage_and_import_surfaces():
    source = Path(store_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    aliases: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
            aliases.extend(alias.lineno for alias in node.names if alias.asname is not None)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            aliases.extend(alias.lineno for alias in node.names if alias.asname is not None)
    allowed_imports = {
        "__future__",
        "dataclasses",
        "enum",
        "hashlib",
        "json",
        "math",
        "os",
        "pathlib",
        "re",
        "secrets",
        "stat",
        "sys",
        "vibecad.workflow.errors",
        "vibecad.workflow.contracts",
        "vibecad.workflow.lease",
        "vibecad.workflow.state",
    }
    assert imported <= allowed_imports
    assert aliases == []
    forbidden_calls = {
        "eval",
        "exec",
        "getattr",
        "glob",
        "iterdir",
        "listdir",
        "mkdir",
        "open",
        "popen",
        "read_bytes",
        "read_text",
        "replace",
        "resolve",
        "rglob",
        "rmtree",
        "scandir",
        "system",
        "walk",
        "write_bytes",
        "write_text",
    }
    unsafe_calls = []
    replace_calls = []
    indirect_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        direct_os_call = False
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
            direct_os_call = isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
            if name == "replace" and isinstance(node.func.value, ast.Name):
                if node.func.value.id == "os":
                    replace_calls.append(node)
        else:
            name = "indirect-call"
            indirect_calls.append(node.lineno)
        if name in {"replace", "scandir"} and direct_os_call:
            pass
        elif name in forbidden_calls and not (name == "open" and direct_os_call):
            unsafe_calls.append((name, node.lineno))
    assert unsafe_calls == []
    assert indirect_calls == []
    assert replace_calls
    assert all(
        {keyword.arg for keyword in node.keywords} == {"src_dir_fd", "dst_dir_fd"}
        for node in replace_calls
    )

    allowed_os_attributes = {
        "O_APPEND",
        "O_CLOEXEC",
        "O_CREAT",
        "O_DIRECTORY",
        "O_EXCL",
        "O_NOFOLLOW",
        "O_RDONLY",
        "O_TRUNC",
        "O_WRONLY",
        "close",
        "fstat",
        "fsync",
        "ftruncate",
        "geteuid",
        "get_inheritable",
        "open",
        "read",
        "replace",
        "scandir",
        "stat",
        "supports_dir_fd",
        "supports_follow_symlinks",
        "unlink",
        "write",
    }
    unsafe_os_attributes = [
        (node.attr, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
        and node.attr not in allowed_os_attributes
    ]
    assert unsafe_os_attributes == []
    forbidden_names = [
        (node.id, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id in {"__builtins__", "__import__", "compile", "eval", "exec"}
    ]
    assert forbidden_names == []

    def contains_broad_exception(value: ast.expr | None) -> bool:
        if value is None:
            return True
        if isinstance(value, ast.Name):
            return value.id in {"Exception", "BaseException"}
        if isinstance(value, ast.Tuple):
            return any(contains_broad_exception(item) for item in value.elts)
        return False

    broad_handlers = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and contains_broad_exception(node.type)
    ]
    assert broad_handlers == []
