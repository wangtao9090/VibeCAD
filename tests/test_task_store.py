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

import vibecad.workflow.store as store_module
from vibecad.workflow.contracts import AcceptanceSpec, ModelProgram
from vibecad.workflow.lease import (
    LeaseError,
    LeaseErrorCode,
    LeaseRootTrust,
    ResourceLease,
    ResourceLeaseManager,
)
from vibecad.workflow.state import (
    ReasoningOwner,
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


def _task_subclass() -> TaskRunSubclass:
    task = _task()
    return TaskRunSubclass(
        id=task.id,
        project_id=task.project_id,
        base_revision=task.base_revision,
        reasoning_owner=task.reasoning_owner,
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
    }


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
def test_every_float_or_nonfinite_number_is_rejected_with_matching_checksum(
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
    assert any(path.name.endswith(".tmp") for path in store_root.iterdir())


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
    assert resources == [f"task-store:{TASK_ID}"] * 3


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
        if text.endswith(".tmp"):
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
    _assert_error(caught, TaskStoreErrorCode.IO_ERROR)
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
    _assert_error(caught, TaskStoreErrorCode.IO_ERROR)
    assert temp.read_bytes() == b"foreign-collision"
    assert not (store_root / _record_name()).exists()


def test_cleanup_does_not_unlink_replacement_at_owned_temp_name(
    store: TaskRunStore, store_root: Path, monkeypatch
):
    token = "b" * 32
    temp = store_root / f".{_record_name()}.{token}.tmp"
    moved = store_root / "owned-temp-moved-aside"
    real_write = store_module.os.write
    injected = False

    def replace_temp_then_fail(fd: int, data) -> int:
        nonlocal injected
        if not injected:
            injected = True
            temp.rename(moved)
            temp.write_bytes(b"foreign-replacement")
            temp.chmod(0o600)
            raise OSError("injected replacement race")
        return real_write(fd, data)

    monkeypatch.setattr(store_module.secrets, "token_hex", lambda _size: token)
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
        _assert_error(caught, TaskStoreErrorCode.CORRUPT_RECORD)
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
    assert len([path for path in store_root.iterdir() if path.name.endswith(".tmp")]) == 1
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
    regular_seen = False

    def fail_directory_fsync(fd: int) -> None:
        nonlocal regular_seen
        mode = os.fstat(fd).st_mode
        if stat.S_ISREG(mode):
            regular_seen = True
            real_fsync(fd)
            return
        if regular_seen and stat.S_ISDIR(mode):
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


def test_cas_directory_fsync_failure_reports_new_generation(store: TaskRunStore, monkeypatch):
    store.create(_task())
    real_fsync = store_module.os.fsync
    regular_seen = False

    def fail_directory_fsync(fd: int) -> None:
        nonlocal regular_seen
        mode = os.fstat(fd).st_mode
        if stat.S_ISREG(mode):
            regular_seen = True
            real_fsync(fd)
            return
        if regular_seen and stat.S_ISDIR(mode):
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
from vibecad.workflow.state import ReasoningOwner, new_task_run
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
def test_two_processes_compare_and_set_have_one_winner(
    store: TaskRunStore, store_root: Path, lease_root: Path, tmp_path: Path
):
    store.create(_task())
    gate = tmp_path / "cas-gate"
    script = """
import sys, time
from pathlib import Path
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager
from vibecad.workflow.state import ReasoningOwner, new_task_run
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
        if name == "replace" and direct_os_call:
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
        "geteuid",
        "get_inheritable",
        "open",
        "read",
        "replace",
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
