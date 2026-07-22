"""Secure generation-zero project bootstrap tests."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import vibecad.application.agent as agent_module
import vibecad.application.project as project_module
import vibecad.application.project_create as project_create_module
from vibecad.application.agent import AgentApplication
from vibecad.application.project_api import (
    ProjectCreateResult,
    ProjectCurrentResult,
    ProjectKind,
    ProjectServicePortErrorCode,
    ProjectServicePortFailure,
)
from vibecad.application.project_create import DurableProjectService
from vibecad.execution.executor import ExecutorError, ExecutorErrorCode
from vibecad.execution.revisions import (
    LocalRevisionStore,
    ProjectHead,
    RevisionSourceBinding,
    RevisionStoreError,
    RevisionStoreErrorCode,
)
from vibecad.interaction.cad import CadExecutionPort, ValidatedImportEvidence
from vibecad.interaction.checkouts import CheckoutState, HeadCheckoutSource
from vibecad.interaction.storage import SafeRoot, StorageFailure
from vibecad.workflow.lease import (
    LeaseError,
    LeaseErrorCode,
    ProjectWriteLease,
    ResourceLeaseManager,
)

PROJECT_ID = "project_11111111111111111111111111111111"


@pytest.fixture(autouse=True)
def _fixed_project_id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(agent_module, "_new_project_id", lambda: PROJECT_ID)


def _data_root(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return home / "data"


def _source(tmp_path: Path, content: bytes = b"normalized-fcstd") -> Path:
    source = tmp_path / "source.FCStd"
    source.write_bytes(content)
    source.chmod(0o600)
    return source


def _durable_service(
    app: AgentApplication,
    *,
    cad_port_factory=None,
) -> DurableProjectService:
    return DurableProjectService(
        bootstrap_root=app._layout.bootstrap,  # noqa: SLF001
        data_root=app._layout.root,  # noqa: SLF001
        revision_store=app._revision_store,  # noqa: SLF001
        lease_manager=app._lease_manager,  # noqa: SLF001
        cad_port_factory=cad_port_factory or app._cad_port_factory,  # noqa: SLF001
    )


CREATE_KEY = "project_create_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _assert_port_failure(value, code: ProjectServicePortErrorCode) -> None:
    assert type(value) is ProjectServicePortFailure
    assert value.code is code


def _assert_only_zero_quarantine_tombstones(directory: Path) -> None:
    for entry in directory.iterdir():
        assert entry.is_file()
        if project_create_module._QUARANTINE_NAME.fullmatch(entry.name):  # noqa: SLF001
            assert entry.stat().st_size == 0
            continue
        assert (  # noqa: SLF001
            project_create_module._QUARANTINE_RECEIPT_NAME.fullmatch(entry.name) is not None
        )
        project_create_module._quarantine_receipt_from_bytes(  # noqa: SLF001
            entry.read_bytes(),
            expected_name=entry.name,
        )


def _unmetered_cleanup_quota(*, extra_bytes: int, extra_files: int) -> tuple[int, int]:
    assert extra_bytes >= 0
    assert extra_files >= 0
    return 0, 0


def _remove_partial(root: SafeRoot, name: str) -> bool:
    return project_create_module._quarantine_unlink(  # noqa: SLF001
        root,
        name,
        expected=None,
        receipt_required=True,
        quota_admit=_unmetered_cleanup_quota,
    )


def _unlink_bound(root: SafeRoot, binding) -> bool:
    return project_create_module._quarantine_unlink(  # noqa: SLF001
        root,
        binding.name,
        expected=binding,
        receipt_required=True,
        quota_admit=_unmetered_cleanup_quota,
    )


def test_durable_empty_create_replays_one_frozen_generation_zero(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)

    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert type(created) is ProjectCreateResult
    assert created.create_key == CREATE_KEY
    assert created.kind is ProjectKind.EMPTY
    assert created.cleanup_required is False
    assert created.head.generation == 0
    assert created.revision.base_revision is None
    assert created.revision.model is None

    reopened = _durable_service(app)
    replayed = reopened.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert replayed == created
    records = tuple((data_root / "bootstrap" / "requests").glob("*.json"))
    assert len([path for path in records if path.name != "hmac-key.json"]) == 1
    app.close()


def test_durable_import_replay_never_reopens_source_or_reexecutes_cad(
    tmp_path: Path,
) -> None:
    port = _HashingImportPort()
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )
    service = _durable_service(app)
    source = _source(tmp_path)

    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    assert type(created) is ProjectCreateResult
    assert created.revision.model is not None
    assert len(port.paths) == 1
    source.unlink()

    replayed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    assert replayed == created
    assert len(port.paths) == 1
    durable_bytes = b"".join(
        path.read_bytes() for path in (data_root / "bootstrap").rglob("*") if path.is_file()
    )
    assert str(source).encode() not in durable_bytes
    app.close()


def test_durable_import_publishes_from_one_descriptor_bound_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, str, RevisionSourceBinding]] = []
    original_at = LocalRevisionStore.import_trusted_fcstd_at

    def forbid_path_import(*_args, **_kwargs):
        raise AssertionError("path import must not be used")

    def record_at(self, project_id, **kwargs):
        calls.append(
            (
                kwargs["source_parent_fd"],
                kwargs["source_name"],
                kwargs["expected_binding"],
            )
        )
        return original_at(self, project_id, **kwargs)

    monkeypatch.setattr(LocalRevisionStore, "import_trusted_fcstd", forbid_path_import)
    monkeypatch.setattr(LocalRevisionStore, "import_trusted_fcstd_at", record_at)
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    service = _durable_service(app)

    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(_source(tmp_path)),
    )

    assert type(created) is ProjectCreateResult
    assert len(calls) == 1
    parent_fd, source_name, binding = calls[0]
    assert source_name.startswith(".normalized.") and source_name.endswith(".FCStd")
    assert type(binding) is RevisionSourceBinding
    with pytest.raises(OSError):
        os.fstat(parent_fd)
    app.close()


def test_durable_import_closes_pinned_source_if_project_lease_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_normalized: list[int] = []
    original_open = project_create_module._open_owned  # noqa: SLF001

    def capture_open(root):
        descriptor = original_open(root)
        if root.path.name == "normalized":
            opened_normalized.append(descriptor)
        return descriptor

    def fail_lease(_self, _project_id):
        raise project_create_module._ServiceError(  # noqa: SLF001
            ProjectServicePortErrorCode.LEASE_UNAVAILABLE
        )

    monkeypatch.setattr(project_create_module, "_open_owned", capture_open)
    monkeypatch.setattr(DurableProjectService, "_project_lease", fail_lease)
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    service = _durable_service(app)

    failed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(_source(tmp_path)),
    )

    _assert_port_failure(failed, ProjectServicePortErrorCode.LEASE_UNAVAILABLE)
    assert opened_normalized
    descriptor = opened_normalized[-1]
    try:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    finally:
        try:
            os.fstat(descriptor)
        except OSError:
            pass
        else:
            os.close(descriptor)
    app.close()


def test_durable_create_key_conflicts_without_second_effect(tmp_path: Path) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    service = _durable_service(app)
    first = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert type(first) is ProjectCreateResult

    conflict = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(_source(tmp_path)),
    )
    _assert_port_failure(conflict, ProjectServicePortErrorCode.CONFLICT)
    assert service.get_project(project_id=first.project_id) == ProjectCurrentResult(
        project_id=first.project_id,
        head=first.head,
        revision=first.revision,
    )
    app.close()


@pytest.mark.parametrize("linked_kind", ["symlink", "hardlink"])
def test_durable_import_rejects_final_links_before_cad(
    tmp_path: Path,
    linked_kind: str,
) -> None:
    source = _source(tmp_path)
    linked = tmp_path / "linked.FCStd"
    if linked_kind == "symlink":
        linked.symlink_to(source)
    else:
        os.link(source, linked)
    calls: list[str] = []
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: calls.append("cad"),
    )

    value = _durable_service(app).create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(linked),
    )
    _assert_port_failure(value, ProjectServicePortErrorCode.INVALID_INPUT)
    assert calls == []
    app.close()


def test_durable_import_rejects_every_data_root_alias_before_cad(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    managed_source = data_root / "managed.FCStd"
    managed_source.write_bytes(b"secret-managed-data")
    managed_source.chmod(0o600)
    calls: list[str] = []
    service = _durable_service(app, cad_port_factory=lambda **_kwargs: calls.append("cad"))

    value = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(managed_source),
    )
    _assert_port_failure(value, ProjectServicePortErrorCode.INVALID_INPUT)
    assert calls == []
    app.close()


def _durable_record(data_root: Path, create_key: str = CREATE_KEY) -> dict[str, object]:
    suffix = create_key.removeprefix("project_create_")
    raw = (data_root / "bootstrap" / "requests" / f"request_{suffix}.json").read_bytes()
    envelope = json.loads(raw)
    assert set(envelope) == {"schema_version", "body", "body_sha256"}
    return envelope["body"]


def _rewrite_durable_record(
    data_root: Path,
    field_path: tuple[str, ...],
    value: object,
) -> None:
    record_path = (
        data_root / "bootstrap" / "requests" / "request_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json"
    )
    envelope = json.loads(record_path.read_bytes())
    target = envelope
    for key in field_path[:-1]:
        target = target[key]
    target[field_path[-1]] = value
    body = json.dumps(
        envelope["body"],
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    envelope["body_sha256"] = hashlib.sha256(
        b"vibecad-project-create-record-v1\0" + body
    ).hexdigest()
    record_path.write_text(
        json.dumps(
            envelope,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "field_path",
    [
        ("schema_version",),
        ("body", "schema_version"),
        ("body", "generation_zero", "head", "schema_version"),
        ("body", "generation_zero", "head", "generation"),
        ("body", "generation_zero", "revision", "schema_version"),
    ],
)
def test_durable_record_rejects_bool_for_every_version_or_generation_integer(
    tmp_path: Path,
    field_path: tuple[str, ...],
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert type(created) is ProjectCreateResult
    _rewrite_durable_record(
        data_root,
        field_path,
        True if "schema_version" in field_path else False,
    )

    replayed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )

    _assert_port_failure(replayed, ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    app.close()


@pytest.mark.parametrize(
    ("field_path", "value"),
    [
        (("body", "failure_code"), "invalid_input"),
        (("body", "reservation_bytes"), 1),
        (("body", "reservation_bytes"), 2 * 1024 * 1024 * 1024 + 1),
        (("body", "validation_started"), True),
        (("body", "outcome"), "REJECTED"),
        (("body", "generation_zero"), None),
    ],
)
def test_durable_published_record_rejects_illegal_phase_field_combinations(
    tmp_path: Path,
    field_path: tuple[str, ...],
    value: object,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert type(created) is ProjectCreateResult
    _rewrite_durable_record(data_root, field_path, value)

    replayed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )

    _assert_port_failure(replayed, ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    app.close()


def _durable_import_record_for_phase(
    service: DurableProjectService,
    source: Path,
    phase: str,
):
    base = service._load_record(CREATE_KEY)  # noqa: SLF001
    assert base is not None
    assert base.phase == "PUBLISHED"
    assert base.source_size is not None
    assert base.generation_zero is not None
    info = source.stat()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    token = base.intent_hmac[:32]

    def bound(name: str):
        return project_create_module._Binding(  # noqa: SLF001
            name=name,
            dev=info.st_dev,
            ino=info.st_ino,
            mode=info.st_mode,
            uid=info.st_uid,
            nlink=info.st_nlink,
            size=info.st_size,
            mtime_ns=str(info.st_mtime_ns),
            sha256=digest,
        )

    stage = bound(f".stage.{token}.FCStd")
    work = bound(f".work.{token}.FCStd")
    normalized = bound(f".normalized.{token}.FCStd")
    reservation = base.source_size + (2 * 512 * 1024 * 1024)
    if phase == "RESERVED":
        return replace(
            base,
            phase=phase,
            reservation_bytes=reservation,
            outcome=None,
            generation_zero=None,
        )
    if phase == "STAGED":
        return replace(
            base,
            phase=phase,
            reservation_bytes=reservation,
            stage=stage,
            work=work,
            outcome=None,
            generation_zero=None,
        )
    if phase == "VALIDATED":
        return replace(
            base,
            phase=phase,
            reservation_bytes=reservation,
            stage=stage,
            validation_started=True,
            normalized=normalized,
            outcome=None,
            generation_zero=None,
        )
    if phase == "CLEANUP_REQUIRED":
        return replace(
            base,
            phase=phase,
            reservation_bytes=reservation,
            stage=stage,
            validation_started=True,
            normalized=normalized,
            generation_zero=replace(base.generation_zero, cleanup_required=True),
        )
    if phase == "REJECTED":
        return replace(
            base,
            phase=phase,
            outcome="REJECTED",
            failure_code="invalid_input",
            generation_zero=None,
        )
    assert phase == "PUBLISHED"
    return base


@pytest.mark.parametrize(
    "phase",
    ["RESERVED", "STAGED", "VALIDATED", "CLEANUP_REQUIRED", "PUBLISHED", "REJECTED"],
)
def test_durable_record_parser_closes_every_phase_against_illegal_fields(
    tmp_path: Path,
    phase: str,
) -> None:
    data_root = _data_root(tmp_path)
    port = _HashingImportPort()
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )
    service = _durable_service(app)
    source = _source(tmp_path)
    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    assert type(created) is ProjectCreateResult
    valid = _durable_import_record_for_phase(service, source, phase)
    project_create_module._validate_record_state(valid)  # noqa: SLF001
    invalid = {
        "RESERVED": replace(valid, outcome="PUBLISHED"),
        "STAGED": replace(valid, failure_code="invalid_input"),
        "VALIDATED": replace(valid, validation_started=False),
        "CLEANUP_REQUIRED": replace(valid, failure_code="invalid_input"),
        "PUBLISHED": replace(valid, reservation_bytes=1),
        "REJECTED": replace(valid, generation_zero=created),
    }[phase]
    record_path = (
        data_root / "bootstrap" / "requests" / "request_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json"
    )
    record_path.write_bytes(project_create_module._record_bytes(invalid))  # noqa: SLF001

    replayed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )

    _assert_port_failure(replayed, ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    app.close()


def test_durable_record_never_cleans_a_binding_owned_by_another_intent(
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    service = _durable_service(app)
    source = _source(tmp_path)
    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    assert type(created) is ProjectCreateResult
    staged = _durable_import_record_for_phase(service, source, "STAGED")
    record_path = (
        data_root / "bootstrap" / "requests" / "request_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json"
    )
    record_path.write_bytes(project_create_module._record_bytes(staged))  # noqa: SLF001
    replacement = (
        data_root / "bootstrap" / "staging" / ".stage.bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.FCStd"
    )
    replacement.write_bytes(b"belongs-to-another-intent")
    replacement.chmod(0o600)
    _rewrite_durable_record(
        data_root,
        ("body", "stage", "name"),
        replacement.name,
    )

    replayed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )

    _assert_port_failure(replayed, ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    assert replacement.read_bytes() == b"belongs-to-another-intent"
    app.close()


@pytest.mark.parametrize(
    "damage",
    ["binding_mode", "binding_uid", "binding_nlink", "normalized_receipt"],
)
def test_durable_record_parser_rejects_unsafe_bindings_and_unbound_receipts(
    tmp_path: Path,
    damage: str,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    service = _durable_service(app)
    source = _source(tmp_path)
    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    assert type(created) is ProjectCreateResult
    phase = "CLEANUP_REQUIRED" if damage == "normalized_receipt" else "STAGED"
    valid = _durable_import_record_for_phase(service, source, phase)
    assert valid.stage is not None
    if damage == "binding_mode":
        invalid = replace(valid, stage=replace(valid.stage, mode=valid.stage.mode | 0o040))
    elif damage == "binding_uid":
        invalid = replace(valid, stage=replace(valid.stage, uid=valid.stage.uid + 1))
    elif damage == "binding_nlink":
        invalid = replace(valid, stage=replace(valid.stage, nlink=valid.stage.nlink + 1))
    else:
        generation = valid.generation_zero
        assert generation is not None
        model = generation.revision.model
        assert model is not None
        invalid = replace(
            valid,
            generation_zero=replace(
                generation,
                revision=replace(
                    generation.revision,
                    model=replace(model, sha256="0" * 64),
                ),
            ),
        )
    raw = project_create_module._record_bytes(invalid)  # noqa: SLF001

    with pytest.raises(project_create_module._ServiceError) as caught:  # noqa: SLF001
        project_create_module._record_from_bytes(  # noqa: SLF001
            raw,
            expected_name="request_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json",
        )

    assert caught.value.code is ProjectServicePortErrorCode.INTEGRITY_FAILURE
    app.close()


@pytest.mark.parametrize("damage", ["stage_size", "work_size", "work_hash"])
def test_durable_record_parser_rejects_impossible_staged_copy_relationships(
    tmp_path: Path,
    damage: str,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    service = _durable_service(app)
    source = _source(tmp_path)
    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    assert type(created) is ProjectCreateResult
    valid = _durable_import_record_for_phase(service, source, "STAGED")
    assert valid.stage is not None
    assert valid.work is not None
    if damage == "stage_size":
        invalid = replace(valid, stage=replace(valid.stage, size=valid.stage.size + 1))
    elif damage == "work_size":
        invalid = replace(valid, work=replace(valid.work, size=valid.work.size + 1))
    else:
        invalid = replace(valid, work=replace(valid.work, sha256="0" * 64))
    raw = project_create_module._record_bytes(invalid)  # noqa: SLF001

    with pytest.raises(project_create_module._ServiceError) as caught:  # noqa: SLF001
        project_create_module._record_from_bytes(  # noqa: SLF001
            raw,
            expected_name="request_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json",
        )

    assert caught.value.code is ProjectServicePortErrorCode.INTEGRITY_FAILURE
    app.close()


@pytest.mark.parametrize(
    ("store_code", "port_code"),
    [
        (RevisionStoreErrorCode.RESOURCE_EXHAUSTED, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED),
        (RevisionStoreErrorCode.RECOVERY_REQUIRED, ProjectServicePortErrorCode.RECOVERY_REQUIRED),
    ],
)
def test_durable_import_limiter_failures_remain_resumable_and_never_reject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_code: RevisionStoreErrorCode,
    port_code: ProjectServicePortErrorCode,
) -> None:
    class FailingLimit:
        def __enter__(self):
            raise RevisionStoreError(store_code)

        def __exit__(self, *_args):
            return False

    port = _HashingImportPort()
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )
    service = _durable_service(app)
    source = _source(tmp_path)
    monkeypatch.setattr(
        project_create_module,
        "_candidate_file_limit",
        lambda _store: FailingLimit(),
    )

    failed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    _assert_port_failure(failed, port_code)
    record = _durable_record(data_root)
    assert record["phase"] == "STAGED"
    assert record["failure_code"] is None
    assert record["outcome"] is None
    assert port.paths == []

    monkeypatch.setattr(
        project_create_module,
        "_candidate_file_limit",
        lambda _store: project_module._candidate_file_limit(app._revision_store),  # noqa: SLF001
    )
    source.unlink()
    recovered = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    assert type(recovered) is ProjectCreateResult
    assert len(port.paths) == 1
    app.close()


def test_durable_import_transient_cad_failure_keeps_staged_retry_authority(
    tmp_path: Path,
) -> None:
    class TransientPort(_HashingImportPort):
        failing = True

        def validate_import(self, path: Path) -> ValidatedImportEvidence:
            if self.failing:
                raise RuntimeError("private transient failure")
            return super().validate_import(path)

    port = TransientPort()
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )
    service = _durable_service(app)
    source = _source(tmp_path)

    failed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    _assert_port_failure(failed, ProjectServicePortErrorCode.CAD_FAILURE)
    record = _durable_record(data_root)
    assert record["phase"] == "STAGED"
    assert record["work"] is None
    assert record["failure_code"] is None
    source.unlink()

    port.failing = False
    recovered = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    assert type(recovered) is ProjectCreateResult
    app.close()


def test_durable_import_exact_malformed_receipt_precedes_cleanup_and_replays(
    tmp_path: Path,
) -> None:
    class MalformedPort(CadExecutionPort):
        calls = 0

        def validate_import(self, path: Path) -> ValidatedImportEvidence:
            self.calls += 1
            assert path.read_bytes() == b"normalized-fcstd"
            raise ValueError("private malformed details")

    port = MalformedPort()
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )
    service = _durable_service(app)
    source = _source(tmp_path)

    failed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    _assert_port_failure(failed, ProjectServicePortErrorCode.INVALID_INPUT)
    record = _durable_record(data_root)
    assert record["phase"] == "REJECTED"
    assert record["failure_code"] == "invalid_input"
    assert record["stage"] is None
    assert record["work"] is None
    source.unlink()

    replayed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    _assert_port_failure(replayed, ProjectServicePortErrorCode.INVALID_INPUT)
    assert port.calls == 1
    app.close()


@pytest.mark.parametrize("mutation", ["replace", "rewrite"])
def test_durable_import_rejects_same_size_source_swap_after_reserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    source = _source(tmp_path, b"original-contents")
    original_copy = DurableProjectService._copy_source_to_stage

    def mutate_then_copy(self, opened, record):
        if mutation == "replace":
            replacement = source.with_suffix(".replacement")
            replacement.write_bytes(b"replacement-data!")
            replacement.chmod(0o600)
            assert replacement.stat().st_size == source.stat().st_size
            os.replace(replacement, source)
        else:
            source.write_bytes(b"rewritten-content")
            source.chmod(0o600)
            assert source.stat().st_size == opened.before.st_size
        return original_copy(self, opened, record)

    monkeypatch.setattr(DurableProjectService, "_copy_source_to_stage", mutate_then_copy)
    failed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    _assert_port_failure(failed, ProjectServicePortErrorCode.INVALID_INPUT)
    assert _durable_record(data_root)["phase"] == "RESERVED"
    assert tuple((data_root / "bootstrap" / "staging").iterdir()) == ()
    app.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "replacement"])
def test_durable_hmac_key_lifecycle_fails_closed_with_existing_records(
    tmp_path: Path,
    damage: str,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert type(created) is ProjectCreateResult
    key_path = data_root / "bootstrap" / "requests" / "hmac-key.json"
    if damage == "missing":
        key_path.unlink()
        expected = ProjectServicePortErrorCode.RECOVERY_REQUIRED
    elif damage == "corrupt":
        key_path.write_text('{"schema_version":1,"key_hex":"broken"}', encoding="utf-8")
        key_path.chmod(0o600)
        expected = ProjectServicePortErrorCode.INTEGRITY_FAILURE
    else:
        key = b"r" * 32
        key_id = hashlib.sha256(b"vibecad-project-create-hmac-key-v1\0" + key).hexdigest()
        key_path.write_text(
            json.dumps(
                {"schema_version": 1, "key_hex": key.hex(), "key_id": key_id},
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        key_path.chmod(0o600)
        expected = ProjectServicePortErrorCode.RECOVERY_REQUIRED

    replayed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    _assert_port_failure(replayed, expected)
    app.close()


@pytest.mark.parametrize("damage", ["checksum", "duplicate", "substitution", "oversize"])
def test_durable_request_record_integrity_damage_is_never_repaired(
    tmp_path: Path,
    damage: str,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert type(created) is ProjectCreateResult
    record_path = (
        data_root / "bootstrap" / "requests" / "request_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json"
    )
    original = record_path.read_bytes()
    if damage == "checksum":
        mapping = json.loads(original)
        mapping["body"]["project_id"] = "project_ffffffffffffffffffffffffffffffff"
        record_path.write_text(
            json.dumps(mapping, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
    elif damage == "duplicate":
        record_path.write_bytes(
            original.replace(
                b'{"body":',
                b'{"schema_version":1,"schema_version":1,"body":',
                1,
            )
        )
    elif damage == "substitution":
        record_path.rename(record_path.with_name("request_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.json"))
    else:
        record_path.write_bytes(original + (b" " * (65_537 - len(original))))
    if damage != "substitution":
        record_path.chmod(0o600)

    replayed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    _assert_port_failure(replayed, ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    app.close()


def _hold_all_project_create_slots(app: AgentApplication):
    return [
        app._lease_manager.acquire(f"vibecad-project-create-slot-v1:{index}")  # noqa: SLF001
        for index in range(8)
    ]


def _release_leases(leases) -> None:
    for lease in reversed(leases):
        lease.release(owner_token=lease.owner_token)


def test_durable_eight_live_slots_bound_ninth_without_losing_reserved_identity(
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    slots = _hold_all_project_create_slots(app)
    try:
        blocked = service.create_project(
            create_key=CREATE_KEY,
            kind=ProjectKind.EMPTY,
            source_path=None,
        )
        _assert_port_failure(blocked, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
        reserved_id = _durable_record(data_root)["project_id"]
        assert _durable_record(data_root)["phase"] == "RESERVED"
        assert (
            tuple(
                path for path in (data_root / "projects").iterdir() if not path.name.startswith(".")
            )
            == ()
        )
    finally:
        _release_leases(slots)

    recovered = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert type(recovered) is ProjectCreateResult
    assert recovered.project_id == reserved_id
    app.close()


def test_durable_record_count_n_plus_one_creates_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(project_create_module, "_MAX_RECORDS", 2)
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    slots = _hold_all_project_create_slots(app)
    try:
        for token in ("a", "b"):
            value = service.create_project(
                create_key=f"project_create_{token * 32}",
                kind=ProjectKind.EMPTY,
                source_path=None,
            )
            _assert_port_failure(value, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
        lock_snapshot = tuple(sorted(path.name for path in (data_root / "locks").iterdir()))
        request_snapshot = tuple(
            sorted(path.name for path in (data_root / "bootstrap" / "requests").iterdir())
        )
        exhausted = service.create_project(
            create_key="project_create_cccccccccccccccccccccccccccccccc",
            kind=ProjectKind.EMPTY,
            source_path=None,
        )
        _assert_port_failure(exhausted, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
        assert tuple(sorted(path.name for path in (data_root / "locks").iterdir())) == lock_snapshot
        assert (
            tuple(sorted(path.name for path in (data_root / "bootstrap" / "requests").iterdir()))
            == request_snapshot
        )
    finally:
        _release_leases(slots)
    app.close()


def test_frozen_record_capacity_and_tombstone_file_ceiling_are_mechanical() -> None:
    assert project_create_module._MAX_RECORDS == 4096  # noqa: SLF001
    assert project_create_module._MAX_RESERVED_IMPORTS == 1  # noqa: SLF001
    per_record = 1 + (  # one durable request
        project_create_module._RECOVERY_ROLES  # noqa: SLF001
        * project_create_module._RECOVERY_FILES_PER_ROLE  # noqa: SLF001
    )
    expected = (
        project_create_module._MANAGED_DIRECTORY_COUNT  # noqa: SLF001
        + 1  # HMAC key
        + (4096 * per_record)
        + project_create_module._MAX_LIVE_OWNED_FILES  # noqa: SLF001
        + project_create_module._CATALOG_TRANSIENT_FILE_HEADROOM  # noqa: SLF001
    )
    assert project_create_module._MANAGED_DIRECTORY_COUNT == len(  # noqa: SLF001
        project_create_module._DIRECTORIES  # noqa: SLF001
    )
    assert project_create_module._MAX_STORE_FILES == expected  # noqa: SLF001


def test_frozen_4096th_record_is_admitted_and_4097th_is_atomic(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    slots = _hold_all_project_create_slots(app)
    try:
        first = service.create_project(
            create_key=CREATE_KEY,
            kind=ProjectKind.EMPTY,
            source_path=None,
        )
        _assert_port_failure(first, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
    finally:
        _release_leases(slots)
    base = service._load_record(CREATE_KEY)  # noqa: SLF001
    assert base is not None
    requests = data_root / "bootstrap" / "requests"
    used_tokens = {base.intent_hmac[:32]}
    for index in range(1, project_create_module._MAX_RECORDS):  # noqa: SLF001
        token_index = index
        token = f"{token_index:032x}"
        while token in used_tokens:
            token_index += project_create_module._MAX_RECORDS  # noqa: SLF001
            token = f"{token_index:032x}"
        used_tokens.add(token)
        create_key = f"project_create_{index:032x}"
        record = replace(
            base,
            create_key=create_key,
            intent_hmac=token + hashlib.sha256(token.encode()).hexdigest()[:32],
            project_id=f"project_{index:032x}",
        )
        path = requests / f"request_{index:032x}.json"
        path.write_bytes(project_create_module._record_bytes(record))  # noqa: SLF001
        path.chmod(0o600)

    _, records, _ = service._scan_store()  # noqa: SLF001
    assert records == 4096
    before = frozenset(path.name for path in requests.iterdir())
    exhausted = service.create_project(
        create_key="project_create_ffffffffffffffffffffffffffffffff",
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    _assert_port_failure(exhausted, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
    assert frozenset(path.name for path in requests.iterdir()) == before
    app.close()


def test_record_temp_file_requires_headroom_before_any_namespace_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    managed_entries = sum(1 for _path in (data_root / "bootstrap").rglob("*"))
    monkeypatch.setattr(project_create_module, "_MAX_STORE_FILES", managed_entries)

    exhausted = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )

    _assert_port_failure(exhausted, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
    assert tuple((data_root / "bootstrap" / "requests").iterdir()) == ()
    app.close()


def test_quota_admission_has_exact_byte_and_file_n_plus_one_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    service = _durable_service(app)
    monkeypatch.setattr(
        DurableProjectService,
        "_scan_store_snapshot",
        lambda _self: (90, 0, 90, 9),
        raising=False,
    )
    monkeypatch.setattr(project_create_module, "_MAX_STORE_BYTES", 100)
    monkeypatch.setattr(project_create_module, "_MAX_STORE_FILES", 10)

    assert service._quota_admit(extra_bytes=10, extra_files=1) == (90, 0)  # noqa: SLF001
    with pytest.raises(project_create_module._ServiceError) as byte_error:  # noqa: SLF001
        service._quota_admit(extra_bytes=11, extra_files=1)  # noqa: SLF001
    assert byte_error.value.code is ProjectServicePortErrorCode.RESOURCE_EXHAUSTED
    with pytest.raises(project_create_module._ServiceError) as file_error:  # noqa: SLF001
        service._quota_admit(extra_bytes=10, extra_files=2)  # noqa: SLF001
    assert file_error.value.code is ProjectServicePortErrorCode.RESOURCE_EXHAUSTED
    app.close()


def test_remaining_legacy_bootstrap_entry_blocks_new_durable_mutation(
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    legacy = data_root / "bootstrap" / ".import.11111111111111111111111111111111.FCStd"
    legacy.write_bytes(b"legacy")
    legacy.chmod(0o600)

    failed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )

    _assert_port_failure(failed, ProjectServicePortErrorCode.RECOVERY_REQUIRED)
    assert legacy.read_bytes() == b"legacy"
    assert tuple((data_root / "bootstrap" / "requests").iterdir()) == ()
    app.close()


def test_durable_two_key_reservation_n_plus_one_is_atomic(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    source = _source(tmp_path, b"small")
    slots = _hold_all_project_create_slots(app)
    try:
        first = service.create_project(
            create_key="project_create_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            kind=ProjectKind.IMPORT_FCSTD,
            source_path=str(source),
        )
        _assert_port_failure(first, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
        lock_snapshot = tuple(sorted(path.name for path in (data_root / "locks").iterdir()))
        second = service.create_project(
            create_key="project_create_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            kind=ProjectKind.IMPORT_FCSTD,
            source_path=str(source),
        )
        _assert_port_failure(second, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
        records = tuple((data_root / "bootstrap" / "requests").glob("request_*.json"))
        assert len(records) == 1
        assert tuple(sorted(path.name for path in (data_root / "locks").iterdir())) == lock_snapshot
    finally:
        _release_leases(slots)
    app.close()


def test_durable_sparse_unknown_entry_fails_closed_before_admission(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    unknown = data_root / "bootstrap" / "unknown.bin"
    with unknown.open("wb") as stream:
        stream.truncate((2 * 1024 * 1024 * 1024) + 1)
    unknown.chmod(0o600)

    value = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    _assert_port_failure(value, ProjectServicePortErrorCode.INTEGRITY_FAILURE)
    assert tuple((data_root / "bootstrap" / "requests").iterdir()) == ()
    app.close()


def test_durable_get_uses_double_head_and_conflicts_on_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    service = _durable_service(app)
    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert type(created) is ProjectCreateResult
    original = LocalRevisionStore.load_head
    calls = 0

    def changing_head(self, project_id):
        nonlocal calls
        calls += 1
        head = original(self, project_id)
        if calls == 2:
            return replace(head, generation=head.generation + 1)
        return head

    monkeypatch.setattr(LocalRevisionStore, "load_head", changing_head)
    value = service.get_project(project_id=created.project_id)
    _assert_port_failure(value, ProjectServicePortErrorCode.CONFLICT)
    assert calls == 2
    app.close()


def test_durable_terminal_replay_never_requires_current_head_generation_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    service = _durable_service(app)
    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert type(created) is ProjectCreateResult

    def advanced_head(*_args, **_kwargs):
        return ProjectHead(
            project_id=created.project_id,
            generation=99,
            revision_id="revision_ffffffffffffffffffffffffffffffff",
            manifest_sha256="f" * 64,
        )

    monkeypatch.setattr(LocalRevisionStore, "load_head", advanced_head)
    replayed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert replayed == created
    app.close()


def test_durable_published_cleanup_failure_returns_frozen_success_and_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _HashingImportPort()
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )
    service = _durable_service(app)
    original_unlink = DurableProjectService._unlink_bound
    monkeypatch.setattr(DurableProjectService, "_unlink_bound", lambda *_args: False)
    source = _source(tmp_path)

    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    assert type(created) is ProjectCreateResult
    assert created.cleanup_required is True
    assert _durable_record(data_root)["phase"] == "CLEANUP_REQUIRED"

    monkeypatch.setattr(DurableProjectService, "_unlink_bound", original_unlink)
    source.unlink()
    replayed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    assert type(replayed) is ProjectCreateResult
    assert replayed.cleanup_required is False
    assert len(port.paths) == 1
    app.close()


def test_durable_eight_abandoned_reserved_records_do_not_consume_live_slots(
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    slots = _hold_all_project_create_slots(app)
    try:
        for index in range(8):
            blocked = service.create_project(
                create_key=f"project_create_{index:032x}",
                kind=ProjectKind.EMPTY,
                source_path=None,
            )
            _assert_port_failure(blocked, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
    finally:
        _release_leases(slots)

    ninth = service.create_project(
        create_key="project_create_ffffffffffffffffffffffffffffffff",
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert type(ninth) is ProjectCreateResult
    assert len(tuple((data_root / "bootstrap" / "requests").glob("request_*.json"))) == 9
    app.close()


def test_durable_rejected_cleanup_failure_keeps_receipt_until_convergence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MalformedPort(CadExecutionPort):
        calls = 0

        def validate_import(self, _path: Path) -> ValidatedImportEvidence:
            self.calls += 1
            raise ValueError("private malformed details")

    port = MalformedPort()
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )
    service = _durable_service(app)
    original_unlink = DurableProjectService._unlink_bound
    monkeypatch.setattr(DurableProjectService, "_unlink_bound", lambda *_args: False)
    source = _source(tmp_path)

    failed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    _assert_port_failure(failed, ProjectServicePortErrorCode.RECOVERY_REQUIRED)
    record = _durable_record(data_root)
    assert record["phase"] == "CLEANUP_REQUIRED"
    assert record["outcome"] == "REJECTED"
    assert record["failure_code"] == "invalid_input"

    monkeypatch.setattr(DurableProjectService, "_unlink_bound", original_unlink)
    source.unlink()
    replayed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    _assert_port_failure(replayed, ProjectServicePortErrorCode.INVALID_INPUT)
    assert port.calls == 1
    assert _durable_record(data_root)["phase"] == "REJECTED"
    app.close()


def test_durable_import_rejects_ancestor_swap_after_descriptor_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "input-root"
    source_root.mkdir(mode=0o700)
    source = source_root / "source.FCStd"
    source.write_bytes(b"trusted-source")
    source.chmod(0o600)
    outside = tmp_path / "outside-source"
    outside.mkdir(mode=0o700)
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    original_copy = DurableProjectService._copy_source_to_stage

    def swap_then_copy(self, opened, record):
        source_root.rename(tmp_path / "detached-input-root")
        source_root.symlink_to(outside, target_is_directory=True)
        return original_copy(self, opened, record)

    monkeypatch.setattr(DurableProjectService, "_copy_source_to_stage", swap_then_copy)
    failed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    _assert_port_failure(failed, ProjectServicePortErrorCode.INVALID_INPUT)
    assert tuple(outside.iterdir()) == ()
    assert tuple((data_root / "bootstrap" / "staging").iterdir()) == ()
    app.close()


def test_durable_import_root_swap_never_touches_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside-bootstrap"
    outside.mkdir(mode=0o700)
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    source = _source(tmp_path)
    live = data_root / "bootstrap"
    detached = data_root / "bootstrap-detached"
    original_copy = DurableProjectService._copy_source_to_stage

    def swap_then_copy(self, opened, record):
        live.rename(detached)
        live.symlink_to(outside, target_is_directory=True)
        return original_copy(self, opened, record)

    monkeypatch.setattr(DurableProjectService, "_copy_source_to_stage", swap_then_copy)
    failed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    _assert_port_failure(failed, ProjectServicePortErrorCode.STORE_FAILURE)
    assert tuple(outside.iterdir()) == ()
    live.unlink()
    detached.rename(live)
    app.close()


def test_durable_import_raw_source_is_opened_exactly_once(tmp_path: Path, monkeypatch) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(
        app,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    source = _source(tmp_path)
    source_parent = source.parent.stat()
    original_open = os.open
    raw_opens = 0

    def counting_open(path, flags, *args, dir_fd=None, **kwargs):
        nonlocal raw_opens
        if path == source.name and dir_fd is not None:
            parent = os.fstat(dir_fd)
            if (parent.st_dev, parent.st_ino) == (
                source_parent.st_dev,
                source_parent.st_ino,
            ):
                raw_opens += 1
        return original_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(os, "open", counting_open)
    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    assert type(created) is ProjectCreateResult
    assert raw_opens == 1
    app.close()


def _fail_record_phase_once(
    monkeypatch: pytest.MonkeyPatch,
    predicate,
) -> list[dict[str, object]]:
    original = SafeRoot.atomic_write
    failed: list[dict[str, object]] = []

    def injected(self, root_fd, name, raw, *, token):
        if name.startswith("request_"):
            body = json.loads(raw)["body"]
            if not failed and predicate(body):
                failed.append(body)
                raise StorageFailure("injected durable record failure")
        return original(self, root_fd, name, raw, token=token)

    monkeypatch.setattr(SafeRoot, "atomic_write", injected)
    return failed


def test_durable_reserved_import_recovers_a_partial_stage_copy(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    port = _HashingImportPort()
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )
    service = _durable_service(app)
    source = _source(tmp_path)
    slots = _hold_all_project_create_slots(app)
    try:
        blocked = service.create_project(
            create_key=CREATE_KEY,
            kind=ProjectKind.IMPORT_FCSTD,
            source_path=str(source),
        )
        _assert_port_failure(blocked, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
    finally:
        _release_leases(slots)
    body = _durable_record(data_root)
    assert body["phase"] == "RESERVED"
    token = body["intent_hmac"][:32]
    partial = data_root / "bootstrap" / "staging" / f".stage.{token}.FCStd"
    partial.write_bytes(b"partial")
    partial.chmod(0o600)

    recovered = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )

    assert type(recovered) is ProjectCreateResult
    assert len(port.paths) == 1
    assert not partial.exists()
    app.close()


def test_durable_staged_import_recovers_a_partial_work_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _data_root(tmp_path)
    port = _HashingImportPort()
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )
    service = _durable_service(app)
    source = _source(tmp_path)
    failed_writes = _fail_record_phase_once(
        monkeypatch,
        lambda body: (
            body["phase"] == "STAGED"
            and body["work"] is not None
            and body["validation_started"] is False
        ),
    )
    first = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    _assert_port_failure(first, ProjectServicePortErrorCode.STORE_FAILURE)
    assert len(failed_writes) == 1
    assert port.paths == []
    body = _durable_record(data_root)
    assert body["phase"] == "STAGED"
    assert body["work"] is None
    token = body["intent_hmac"][:32]
    work = data_root / "bootstrap" / "work" / f".work.{token}.FCStd"
    assert work.exists()
    replacement = tmp_path / "partial-work.FCStd"
    replacement.write_bytes(b"partial")
    replacement.chmod(0o600)
    os.replace(replacement, work)

    recovered = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )

    assert type(recovered) is ProjectCreateResult
    assert len(port.paths) == 1
    assert not work.exists()
    app.close()


def test_partial_cleanup_never_unlinks_an_entry_swapped_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    partial = root_path / name
    partial.write_bytes(b"partial")
    partial.chmod(0o600)
    replacement = tmp_path / "replacement.FCStd"
    replacement.write_bytes(b"must-survive")
    replacement.chmod(0o600)
    original_stat = os.stat
    named_stats = 0

    def swap_before_final_stat(path, *args, dir_fd=None, **kwargs):
        nonlocal named_stats
        if path == name and dir_fd is not None:
            named_stats += 1
            if named_stats == 2:
                os.replace(replacement, partial)
        return original_stat(path, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(project_create_module.os, "stat", swap_before_final_stat)

    removed = _remove_partial(root, name)  # noqa: SLF001

    assert removed is False
    assert named_stats == 2
    assert partial.read_bytes() == b"must-survive"


@pytest.mark.parametrize("bound_cleanup", [False, True], ids=["partial", "bound"])
def test_cleanup_never_deletes_a_replacement_swapped_at_the_final_name_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound_cleanup: bool,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    target = root_path / name
    target.write_bytes(b"record-owned")
    target.chmod(0o600)
    target_info = target.stat()
    binding = project_create_module._binding(  # noqa: SLF001
        target_info,
        name=name,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    replacement = tmp_path / "replacement.FCStd"
    replacement.write_bytes(b"must-survive")
    replacement.chmod(0o600)
    original_unlink = os.unlink
    original_rename = getattr(project_create_module, "_rename_noreplace", None)
    swapped = False

    def swap_once() -> None:
        nonlocal swapped
        if not swapped:
            os.replace(replacement, target)
            swapped = True

    def swap_before_rename(root_fd, source, destination):
        if source == name:
            swap_once()
        if original_rename is None:
            return False
        return original_rename(root_fd, source, destination)

    def swap_before_unlink(path, *args, dir_fd=None, **kwargs):
        if path == name and dir_fd is not None:
            swap_once()
        return original_unlink(path, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(
        project_create_module,
        "_rename_noreplace",
        swap_before_rename,
        raising=False,
    )
    monkeypatch.setattr(project_create_module.os, "unlink", swap_before_unlink)

    if bound_cleanup:
        removed = _unlink_bound(root, binding)  # noqa: SLF001
    else:
        removed = _remove_partial(  # noqa: SLF001
            root,
            name,
        )

    assert swapped is True
    assert removed is False
    assert not target.exists()
    assert any(
        entry.read_bytes() == b"must-survive" for entry in root_path.glob(".quarantine.*.FCStd")
    )


@pytest.mark.parametrize("bound_cleanup", [False, True], ids=["partial", "bound"])
def test_cleanup_retry_converges_after_a_crash_immediately_after_quarantine_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound_cleanup: bool,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    target = root_path / name
    target.write_bytes(b"record-owned")
    target.chmod(0o600)
    target_info = target.stat()
    binding = project_create_module._binding(  # noqa: SLF001
        target_info,
        name=name,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    original_rename = getattr(project_create_module, "_rename_noreplace", None)
    assert callable(original_rename)
    crashed = False

    def crash_after_rename(root_fd, source, destination):
        nonlocal crashed
        renamed = original_rename(root_fd, source, destination)
        if renamed and source == name and not crashed:
            crashed = True
            raise KeyboardInterrupt
        return renamed

    monkeypatch.setattr(
        project_create_module,
        "_rename_noreplace",
        crash_after_rename,
    )
    with pytest.raises(KeyboardInterrupt):
        if bound_cleanup:
            _unlink_bound(root, binding)  # noqa: SLF001
        else:
            _remove_partial(  # noqa: SLF001
                root,
                name,
            )
    assert crashed is True
    assert not target.exists()
    quarantines = tuple(root_path.glob(".quarantine.*"))
    assert len(quarantines) == 1
    monkeypatch.setattr(project_create_module, "_rename_noreplace", original_rename)

    if bound_cleanup:
        converged = _unlink_bound(root, binding)  # noqa: SLF001
    else:
        converged = _remove_partial(  # noqa: SLF001
            root,
            name,
        )

    assert converged is True
    _assert_only_zero_quarantine_tombstones(root_path)


@pytest.mark.parametrize("bound_cleanup", [False, True], ids=["partial", "bound"])
def test_cleanup_never_deletes_a_replacement_swapped_into_the_quarantine_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound_cleanup: bool,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    target = root_path / name
    target.write_bytes(b"record-owned")
    target.chmod(0o600)
    binding = project_create_module._binding(  # noqa: SLF001
        target.stat(),
        name=name,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001
    crashed = False

    def crash_after_rename(root_fd, source_name, destination_name):
        nonlocal crashed
        renamed = original_rename(root_fd, source_name, destination_name)
        if renamed and source_name == name and not crashed:
            crashed = True
            raise KeyboardInterrupt
        return renamed

    monkeypatch.setattr(project_create_module, "_rename_noreplace", crash_after_rename)
    with pytest.raises(KeyboardInterrupt):
        if bound_cleanup:
            _unlink_bound(root, binding)  # noqa: SLF001
        else:
            _remove_partial(  # noqa: SLF001
                root,
                name,
            )
    monkeypatch.setattr(project_create_module, "_rename_noreplace", original_rename)
    quarantine = next(root_path.glob(".quarantine.*.FCStd"))
    quarantine_inode = quarantine.stat().st_ino
    replacement = tmp_path / "replacement.FCStd"
    replacement.write_bytes(b"must-survive")
    replacement.chmod(0o600)
    original_unlink = os.unlink
    original_ftruncate = os.ftruncate
    swapped = False

    def swap_once() -> None:
        nonlocal swapped
        if not swapped:
            os.replace(replacement, quarantine)
            swapped = True

    def swap_before_unlink(path, *args, dir_fd=None, **kwargs):
        if path == quarantine.name and dir_fd is not None:
            swap_once()
        return original_unlink(path, *args, dir_fd=dir_fd, **kwargs)

    def swap_before_truncate(descriptor, length):
        if length == 0 and os.fstat(descriptor).st_ino == quarantine_inode:
            swap_once()
        return original_ftruncate(descriptor, length)

    monkeypatch.setattr(project_create_module.os, "unlink", swap_before_unlink)
    monkeypatch.setattr(project_create_module.os, "ftruncate", swap_before_truncate)

    if bound_cleanup:
        converged = _unlink_bound(root, binding)  # noqa: SLF001
    else:
        converged = _remove_partial(  # noqa: SLF001
            root,
            name,
        )

    assert swapped is True
    assert converged is False
    assert quarantine.read_bytes() == b"must-survive"


def test_cleanup_keeps_immutable_receipt_unchanged_after_convergence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    target = root_path / name
    target.write_bytes(b"record-owned")
    target.chmod(0o600)
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001
    crashed = False

    def crash_after_rename(root_fd, source_name, destination_name):
        nonlocal crashed
        renamed = original_rename(root_fd, source_name, destination_name)
        if renamed and source_name == name and not crashed:
            crashed = True
            raise KeyboardInterrupt
        return renamed

    monkeypatch.setattr(project_create_module, "_rename_noreplace", crash_after_rename)
    with pytest.raises(KeyboardInterrupt):
        _remove_partial(root, name)  # noqa: SLF001
    monkeypatch.setattr(project_create_module, "_rename_noreplace", original_rename)
    receipt = next(root_path.glob(".quarantine-receipt.*.json"))
    receipt_inode = receipt.stat().st_ino
    receipt_bytes = receipt.read_bytes()

    converged = _remove_partial(  # noqa: SLF001
        root,
        name,
    )

    assert converged is True
    assert receipt.stat().st_ino == receipt_inode
    assert receipt.read_bytes() == receipt_bytes
    assert next(root_path.glob(".quarantine.*.FCStd")).stat().st_size == 0


def test_cleanup_rechecks_original_absence_after_fresh_quarantine_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    target = root_path / name
    target.write_bytes(b"record-owned")
    target.chmod(0o600)
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001
    original_stat = os.stat
    armed = False
    injected = False

    def arm_after_rename(
        root_fd,
        source_name,
        destination_name,
        *,
        destination_root_fd=None,
    ):
        nonlocal armed
        if destination_root_fd is None:
            renamed = original_rename(root_fd, source_name, destination_name)
        else:
            renamed = original_rename(
                root_fd,
                source_name,
                destination_name,
                destination_root_fd=destination_root_fd,
            )
        if renamed and source_name == name:
            armed = True
        return renamed

    def replace_original_before_absence_check(path, *args, dir_fd=None, **kwargs):
        nonlocal injected
        if armed and not injected and path == name and dir_fd is not None:
            target.write_bytes(b"must-survive")
            target.chmod(0o600)
            injected = True
        return original_stat(path, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(project_create_module, "_rename_noreplace", arm_after_rename)
    monkeypatch.setattr(
        project_create_module.os,
        "stat",
        replace_original_before_absence_check,
    )

    removed = _remove_partial(root, name)  # noqa: SLF001

    assert armed is True
    assert injected is True
    assert removed is False
    assert target.read_bytes() == b"must-survive"
    assert next(root_path.glob(".quarantine.*.FCStd")).read_bytes() == b"record-owned"


def test_cleanup_rechecks_original_absence_before_recovery_truncate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    target = root_path / name
    target.write_bytes(b"record-owned")
    target.chmod(0o600)
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001

    def crash_after_rename(root_fd, source_name, destination_name):
        renamed = original_rename(root_fd, source_name, destination_name)
        if renamed and source_name == name:
            raise KeyboardInterrupt
        return renamed

    monkeypatch.setattr(project_create_module, "_rename_noreplace", crash_after_rename)
    with pytest.raises(KeyboardInterrupt):
        _remove_partial(root, name)  # noqa: SLF001
    monkeypatch.setattr(project_create_module, "_rename_noreplace", original_rename)
    quarantine = next(root_path.glob(".quarantine.*.FCStd"))
    original_open = project_create_module._open_hashed_owned_file  # noqa: SLF001
    original_stat = os.stat
    armed = False
    injected = False

    def arm_after_quarantine_open(open_root, root_fd, opened_name):
        nonlocal armed
        opened = original_open(open_root, root_fd, opened_name)
        if opened_name == quarantine.name:
            armed = True
        return opened

    def replace_original_before_absence_check(path, *args, dir_fd=None, **kwargs):
        nonlocal injected
        if armed and not injected and path == name and dir_fd is not None:
            target.write_bytes(b"must-survive")
            target.chmod(0o600)
            injected = True
        return original_stat(path, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(
        project_create_module,
        "_open_hashed_owned_file",
        arm_after_quarantine_open,
    )
    monkeypatch.setattr(
        project_create_module.os,
        "stat",
        replace_original_before_absence_check,
    )

    recovered = _remove_partial(root, name)  # noqa: SLF001

    assert armed is True
    assert injected is True
    assert recovered is False
    assert target.read_bytes() == b"must-survive"
    assert quarantine.read_bytes() == b"record-owned"


@pytest.mark.parametrize("replacement_bytes", [b"", b"{}"], ids=["zero", "malformed"])
def test_cleanup_pins_final_receipt_through_quarantine_truncate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_bytes: bytes,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    target = root_path / name
    target.write_bytes(b"record-owned")
    target.chmod(0o600)
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001

    def crash_after_rename(root_fd, source_name, destination_name):
        renamed = original_rename(root_fd, source_name, destination_name)
        if renamed and source_name == name:
            raise KeyboardInterrupt
        return renamed

    monkeypatch.setattr(project_create_module, "_rename_noreplace", crash_after_rename)
    with pytest.raises(KeyboardInterrupt):
        _remove_partial(root, name)  # noqa: SLF001
    monkeypatch.setattr(project_create_module, "_rename_noreplace", original_rename)
    quarantine = next(root_path.glob(".quarantine.*.FCStd"))
    receipt = next(root_path.glob(".quarantine-receipt.*.json"))
    replacement = tmp_path / "replacement-receipt.json"
    replacement.write_bytes(replacement_bytes)
    replacement.chmod(0o600)
    original_open = project_create_module._open_hashed_owned_file  # noqa: SLF001
    replaced = False

    def replace_receipt_after_quarantine_open(open_root, root_fd, opened_name):
        nonlocal replaced
        opened = original_open(open_root, root_fd, opened_name)
        if opened_name == quarantine.name and not replaced:
            os.replace(replacement, receipt)
            replaced = True
        return opened

    monkeypatch.setattr(
        project_create_module,
        "_open_hashed_owned_file",
        replace_receipt_after_quarantine_open,
    )

    recovered = _remove_partial(root, name)  # noqa: SLF001

    assert replaced is True
    assert recovered is False
    assert quarantine.read_bytes() == b"record-owned"
    assert receipt.read_bytes() == replacement_bytes


def test_recovery_quota_rejection_precedes_quarantine_truncate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    target = root_path / name
    target.write_bytes(b"record-owned")
    target.chmod(0o600)
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001

    def crash_after_rename(root_fd, source_name, destination_name):
        renamed = original_rename(root_fd, source_name, destination_name)
        if renamed and source_name == name:
            raise KeyboardInterrupt
        return renamed

    monkeypatch.setattr(project_create_module, "_rename_noreplace", crash_after_rename)
    with pytest.raises(KeyboardInterrupt):
        _remove_partial(root, name)  # noqa: SLF001
    monkeypatch.setattr(project_create_module, "_rename_noreplace", original_rename)
    quarantine = next(root_path.glob(".quarantine.*.FCStd"))
    admissions: list[tuple[int, int]] = []

    def reject(*, extra_bytes: int, extra_files: int) -> tuple[int, int]:
        admissions.append((extra_bytes, extra_files))
        raise project_create_module._ServiceError(  # noqa: SLF001
            ProjectServicePortErrorCode.RESOURCE_EXHAUSTED
        )

    recovered = project_create_module._quarantine_unlink(  # noqa: SLF001
        root,
        name,
        expected=None,
        receipt_required=True,
        quota_admit=reject,
    )

    assert admissions == [(0, 0)]
    assert recovered is False
    assert quarantine.read_bytes() == b"record-owned"


def test_bound_cleanup_requires_receipt_for_the_exact_expected_binding(
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    target = root_path / name
    target.write_bytes(b"first-binding")
    target.chmod(0o600)
    first = project_create_module._binding(  # noqa: SLF001
        target.stat(),
        name=name,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    assert _unlink_bound(root, first) is True

    target.write_bytes(b"second-binding")
    target.chmod(0o600)
    second = project_create_module._binding(  # noqa: SLF001
        target.stat(),
        name=name,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    target.unlink()

    removed = _unlink_bound(root, second)  # noqa: SLF001

    assert removed is False
    _assert_only_zero_quarantine_tombstones(root_path)


def test_cleanup_never_writes_an_existing_zero_receipt_entry(
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    target = root_path / name
    target.write_bytes(b"record-owned")
    target.chmod(0o600)
    binding = project_create_module._binding(  # noqa: SLF001
        target.stat(),
        name=name,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    receipt = project_create_module._QuarantineReceipt(  # noqa: SLF001
        original_name=name,
        quarantine_name=project_create_module._quarantine_file_name(binding),  # noqa: SLF001
        binding=binding,
    )
    receipt_name = project_create_module._quarantine_receipt_name(binding)  # noqa: SLF001
    replacement = root_path / receipt_name
    replacement.touch(mode=0o600)
    replacement_inode = replacement.stat().st_ino
    root_fd = root.open()
    try:
        written = project_create_module._write_quarantine_receipt_at(  # noqa: SLF001
            root,
            root_fd,
            receipt,
            quota_admit=_unmetered_cleanup_quota,
        )
    finally:
        os.close(root_fd)

    assert written is False
    assert replacement.stat().st_ino == replacement_inode
    assert replacement.read_bytes() == b""


def test_partial_receipt_short_write_recovers_without_rewriting_the_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    target = root_path / name
    target.write_bytes(b"record-owned")
    target.chmod(0o600)
    original_write = os.write
    crashed = False

    def short_then_crash(descriptor, data):
        nonlocal crashed
        if not crashed:
            crashed = True
            written = original_write(descriptor, data[:17])
            assert written == 17
            raise KeyboardInterrupt
        return original_write(descriptor, data)

    monkeypatch.setattr(project_create_module.os, "write", short_then_crash)
    with pytest.raises(KeyboardInterrupt):
        _remove_partial(root, name)  # noqa: SLF001
    monkeypatch.setattr(project_create_module.os, "write", original_write)

    recovered = _remove_partial(root, name)  # noqa: SLF001

    assert recovered is True
    assert not target.exists()
    quarantine = next(root_path.glob(".quarantine.*.FCStd"))
    assert quarantine.stat().st_size == 0
    receipt = next(root_path.glob(".quarantine-receipt.*.json"))
    parsed = project_create_module._quarantine_receipt_from_bytes(  # noqa: SLF001
        receipt.read_bytes(),
        expected_name=receipt.name,
    )
    assert parsed.original_name == name


@pytest.mark.parametrize("crash_after_rename", [False, True], ids=["prepared", "published"])
def test_partial_receipt_prepare_and_publish_crashes_recover_immutably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_after_rename: bool,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    target = root_path / name
    target.write_bytes(b"record-owned")
    target.chmod(0o600)
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001
    crashed = False

    def crash_at_receipt_publish(root_fd, source_name, destination_name):
        nonlocal crashed
        is_receipt = (
            project_create_module._QUARANTINE_RECEIPT_TEMP_NAME.fullmatch(source_name)  # noqa: SLF001
            is not None
        )
        if is_receipt and not crash_after_rename and not crashed:
            crashed = True
            raise KeyboardInterrupt
        renamed = original_rename(root_fd, source_name, destination_name)
        if is_receipt and crash_after_rename and renamed and not crashed:
            crashed = True
            raise KeyboardInterrupt
        return renamed

    monkeypatch.setattr(
        project_create_module,
        "_rename_noreplace",
        crash_at_receipt_publish,
    )
    with pytest.raises(KeyboardInterrupt):
        _remove_partial(root, name)  # noqa: SLF001
    assert crashed is True
    assert target.read_bytes() == b"record-owned"
    monkeypatch.setattr(project_create_module, "_rename_noreplace", original_rename)

    recovered = _remove_partial(root, name)  # noqa: SLF001

    assert recovered is True
    assert not target.exists()
    assert tuple(root_path.glob(".quarantine-receipt.*.tmp")) == ()
    receipt = next(root_path.glob(".quarantine-receipt.*.json"))
    project_create_module._quarantine_receipt_from_bytes(  # noqa: SLF001
        receipt.read_bytes(),
        expected_name=receipt.name,
    )
    assert next(root_path.glob(".quarantine.*.FCStd")).stat().st_size == 0


def test_receipt_quota_rejection_precedes_receipt_or_quarantine_mutation(
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    target = root_path / name
    target.write_bytes(b"record-owned")
    target.chmod(0o600)
    binding = project_create_module._binding(  # noqa: SLF001
        target.stat(),
        name=name,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    receipt = project_create_module._QuarantineReceipt(  # noqa: SLF001
        original_name=name,
        quarantine_name=project_create_module._quarantine_file_name(binding),  # noqa: SLF001
        binding=binding,
    )
    admitted: list[tuple[int, int]] = []

    def reject(*, extra_bytes: int, extra_files: int) -> tuple[int, int]:
        admitted.append((extra_bytes, extra_files))
        raise project_create_module._ServiceError(  # noqa: SLF001
            ProjectServicePortErrorCode.RESOURCE_EXHAUSTED
        )

    root_fd = root.open()
    try:
        written = project_create_module._write_quarantine_receipt_at(  # noqa: SLF001
            root,
            root_fd,
            receipt,
            quota_admit=reject,
        )
    finally:
        os.close(root_fd)

    assert written is False
    assert admitted and admitted[0][0] > 0 and admitted[0][1] == 1
    assert target.read_bytes() == b"record-owned"
    assert tuple(root_path.glob(".quarantine*")) == ()


@pytest.mark.parametrize("bound_cleanup", [False, True], ids=["partial", "bound"])
def test_cleanup_preserves_original_and_quarantine_when_both_names_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound_cleanup: bool,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    target = root_path / name
    target.write_bytes(b"record-owned")
    target.chmod(0o600)
    binding = project_create_module._binding(  # noqa: SLF001
        target.stat(),
        name=name,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001
    crashed = False

    def crash_after_rename(root_fd, source_name, destination_name):
        nonlocal crashed
        renamed = original_rename(root_fd, source_name, destination_name)
        if renamed and source_name == name and not crashed:
            crashed = True
            raise KeyboardInterrupt
        return renamed

    monkeypatch.setattr(project_create_module, "_rename_noreplace", crash_after_rename)
    with pytest.raises(KeyboardInterrupt):
        if bound_cleanup:
            _unlink_bound(root, binding)  # noqa: SLF001
        else:
            _remove_partial(  # noqa: SLF001
                root,
                name,
            )
    monkeypatch.setattr(project_create_module, "_rename_noreplace", original_rename)
    quarantine = next(root_path.glob(".quarantine.*.FCStd"))
    target.write_bytes(b"must-survive")
    target.chmod(0o600)

    if bound_cleanup:
        converged = _unlink_bound(root, binding)  # noqa: SLF001
    else:
        converged = _remove_partial(  # noqa: SLF001
            root,
            name,
        )

    assert converged is False
    assert target.read_bytes() == b"must-survive"
    assert quarantine.read_bytes() == b"record-owned"


def test_lost_receipt_authority_never_truncates_a_new_original_next_to_old_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    target = root_path / name
    target.write_bytes(b"record-owned")
    target.chmod(0o600)
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001

    def crash_after_rename(root_fd, source_name, destination_name):
        renamed = original_rename(root_fd, source_name, destination_name)
        if renamed and source_name == name:
            raise KeyboardInterrupt
        return renamed

    monkeypatch.setattr(project_create_module, "_rename_noreplace", crash_after_rename)
    with pytest.raises(KeyboardInterrupt):
        _remove_partial(root, name)  # noqa: SLF001
    monkeypatch.setattr(project_create_module, "_rename_noreplace", original_rename)
    quarantine = next(root_path.glob(".quarantine.*.FCStd"))
    receipt = next(root_path.glob(".quarantine-receipt.*.json"))
    receipt.write_bytes(b"")
    target.write_bytes(b"must-survive")
    target.chmod(0o600)

    recovered = _remove_partial(root, name)  # noqa: SLF001

    assert recovered is False
    assert target.read_bytes() == b"must-survive"
    assert quarantine.read_bytes() == b"record-owned"
    assert receipt.read_bytes() == b""


def test_mismatched_quarantine_replacement_is_never_moved_to_original_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    name = ".stage.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.FCStd"
    target = root_path / name
    target.write_bytes(b"record-owned")
    target.chmod(0o600)
    binding = project_create_module._binding(  # noqa: SLF001
        target.stat(),
        name=name,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001

    def crash_after_rename(root_fd, source_name, destination_name):
        renamed = original_rename(root_fd, source_name, destination_name)
        if renamed and source_name == name:
            raise KeyboardInterrupt
        return renamed

    monkeypatch.setattr(project_create_module, "_rename_noreplace", crash_after_rename)
    with pytest.raises(KeyboardInterrupt):
        _unlink_bound(root, binding)  # noqa: SLF001
    monkeypatch.setattr(project_create_module, "_rename_noreplace", original_rename)
    quarantine = next(root_path.glob(".quarantine.*.FCStd"))
    replacement = tmp_path / "replacement.FCStd"
    replacement.write_bytes(b"unrelated replacement")
    replacement.chmod(0o600)
    os.replace(replacement, quarantine)

    recovered = _unlink_bound(root, binding)  # noqa: SLF001

    assert recovered is False
    assert not target.exists()
    assert quarantine.read_bytes() == b"unrelated replacement"


def test_quarantine_data_is_not_counted_twice_against_the_active_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    source = _source(tmp_path)
    slots = _hold_all_project_create_slots(app)
    try:
        blocked = service.create_project(
            create_key=CREATE_KEY,
            kind=ProjectKind.IMPORT_FCSTD,
            source_path=str(source),
        )
        _assert_port_failure(blocked, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
    finally:
        _release_leases(slots)
    body = _durable_record(data_root)
    token = body["intent_hmac"][:32]
    name = f".stage.{token}.FCStd"
    partial = data_root / "bootstrap" / "staging" / name
    partial.write_bytes(b"1234567")
    partial.chmod(0o600)
    root = SafeRoot(partial.parent)
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001

    def crash_after_rename(root_fd, source_name, destination_name):
        renamed = original_rename(root_fd, source_name, destination_name)
        if renamed and source_name == name:
            raise KeyboardInterrupt
        return renamed

    monkeypatch.setattr(project_create_module, "_rename_noreplace", crash_after_rename)
    with pytest.raises(KeyboardInterrupt):
        _remove_partial(root, name)  # noqa: SLF001
    monkeypatch.setattr(project_create_module, "_rename_noreplace", original_rename)
    quarantine = next(partial.parent.glob(".quarantine.*.FCStd"))
    total = sum(
        entry.stat().st_size for entry in (data_root / "bootstrap").rglob("*") if entry.is_file()
    )
    expected_accounted = total - quarantine.stat().st_size + body["reservation_bytes"]
    monkeypatch.setattr(project_create_module, "_MAX_STORE_BYTES", expected_accounted)

    scanned_total, records, accounted = service._scan_store()  # noqa: SLF001

    assert scanned_total == total
    assert records == 1
    assert accounted == expected_accounted
    app.close()


def test_store_scan_hashes_nonzero_quarantine_against_its_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    service = _durable_service(app)
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001

    def crash_after_stage_rename(
        root_fd,
        source_name,
        destination_name,
        *,
        destination_root_fd=None,
    ):
        if destination_root_fd is None:
            renamed = original_rename(root_fd, source_name, destination_name)
        else:
            renamed = original_rename(
                root_fd,
                source_name,
                destination_name,
                destination_root_fd=destination_root_fd,
            )
        if renamed and source_name.startswith(".stage."):
            raise KeyboardInterrupt
        return renamed

    monkeypatch.setattr(
        project_create_module,
        "_rename_noreplace",
        crash_after_stage_rename,
    )
    with pytest.raises(KeyboardInterrupt):
        service.create_project(
            create_key=CREATE_KEY,
            kind=ProjectKind.IMPORT_FCSTD,
            source_path=str(_source(tmp_path)),
        )
    monkeypatch.setattr(project_create_module, "_rename_noreplace", original_rename)
    service._scan_store()  # noqa: SLF001
    quarantine = next((data_root / "bootstrap" / "staging").glob(".quarantine.*.FCStd"))
    receipt_path = next(quarantine.parent.glob(".quarantine-receipt.*.json"))
    receipt = project_create_module._quarantine_receipt_from_bytes(  # noqa: SLF001
        receipt_path.read_bytes(),
        expected_name=receipt_path.name,
    )
    before = quarantine.stat()
    original = quarantine.read_bytes()
    quarantine.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    os.utime(
        quarantine,
        ns=(before.st_atime_ns, int(receipt.binding.mtime_ns)),
    )
    assert quarantine.stat().st_ino == before.st_ino
    assert quarantine.stat().st_size == before.st_size
    assert str(quarantine.stat().st_mtime_ns) == receipt.binding.mtime_ns

    with pytest.raises(project_create_module._ServiceError) as raised:  # noqa: SLF001
        service._scan_store()  # noqa: SLF001

    assert raised.value.code is ProjectServicePortErrorCode.RECOVERY_REQUIRED
    app.close()


def test_third_quarantine_tombstone_for_one_role_is_resource_exhausted(
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert type(created) is ProjectCreateResult
    token = _durable_record(data_root)["intent_hmac"][:32]
    staging = data_root / "bootstrap" / "staging"
    for index in range(project_create_module._MAX_QUARANTINES_PER_ROLE + 1):  # noqa: SLF001
        tombstone = staging / f".quarantine.stage.{token}.{index:064x}.FCStd"
        tombstone.touch(mode=0o600)

    exhausted = service.create_project(
        create_key="project_create_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        kind=ProjectKind.EMPTY,
        source_path=None,
    )

    _assert_port_failure(exhausted, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
    assert not (
        data_root / "bootstrap" / "requests" / "request_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.json"
    ).exists()
    app.close()


def test_cleanup_refuses_to_create_a_third_quarantine_tombstone(tmp_path: Path) -> None:
    root_path = tmp_path / "staging"
    root_path.mkdir(mode=0o700)
    root = SafeRoot(root_path)
    token = "a" * 32
    name = f".stage.{token}.FCStd"
    target = root_path / name
    target.write_bytes(b"record-owned")
    target.chmod(0o600)
    binding = project_create_module._binding(  # noqa: SLF001
        target.stat(),
        name=name,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    for index in range(project_create_module._MAX_QUARANTINES_PER_ROLE):  # noqa: SLF001
        tombstone = root_path / f".quarantine.stage.{token}.{index:064x}.FCStd"
        tombstone.touch(mode=0o600)

    removed = _unlink_bound(root, binding)  # noqa: SLF001

    assert removed is False
    assert target.read_bytes() == b"record-owned"
    assert len(tuple(root_path.glob(".quarantine.*.FCStd"))) == 2


def test_zero_byte_quarantine_tombstones_still_consume_the_file_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    service = _durable_service(app)
    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(_source(tmp_path)),
    )
    assert type(created) is ProjectCreateResult
    files = tuple(path for path in (data_root / "bootstrap").rglob("*") if path.is_file())
    tombstones = tuple(
        path for path in files if ".quarantine" in path.name and path.stat().st_size == 0
    )
    assert tombstones
    managed_entries = len(files) + len(project_create_module._DIRECTORIES)  # noqa: SLF001
    monkeypatch.setattr(project_create_module, "_MAX_STORE_FILES", managed_entries - 1)

    exhausted = service.create_project(
        create_key="project_create_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        kind=ProjectKind.EMPTY,
        source_path=None,
    )

    _assert_port_failure(exhausted, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
    app.close()


def test_terminal_record_cannot_retain_even_a_zero_byte_owned_artifact(
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert type(created) is ProjectCreateResult
    token = _durable_record(data_root)["intent_hmac"][:32]
    unexpected = data_root / "bootstrap" / "staging" / f".stage.{token}.FCStd"
    unexpected.touch(mode=0o600)

    failed = service.create_project(
        create_key="project_create_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        kind=ProjectKind.EMPTY,
        source_path=None,
    )

    _assert_port_failure(failed, ProjectServicePortErrorCode.RECOVERY_REQUIRED)
    assert unexpected.exists()
    app.close()


def test_terminal_nonzero_quarantine_precedes_reservation_exhaustion(
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert type(created) is ProjectCreateResult
    token = _durable_record(data_root)["intent_hmac"][:32]
    quarantine = data_root / "bootstrap" / "staging" / f".quarantine.stage.{token}.{'1' * 64}.FCStd"
    quarantine.write_bytes(b"must-recover")
    quarantine.chmod(0o600)

    failed = service.create_project(
        create_key="project_create_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        kind=ProjectKind.EMPTY,
        source_path=None,
    )

    _assert_port_failure(failed, ProjectServicePortErrorCode.RECOVERY_REQUIRED)
    assert quarantine.read_bytes() == b"must-recover"
    app.close()


def test_durable_partial_quarantine_crash_converges_before_recopy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    service = _durable_service(app)
    source = _source(tmp_path)
    slots = _hold_all_project_create_slots(app)
    try:
        blocked = service.create_project(
            create_key=CREATE_KEY,
            kind=ProjectKind.IMPORT_FCSTD,
            source_path=str(source),
        )
        _assert_port_failure(blocked, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
    finally:
        _release_leases(slots)
    token = _durable_record(data_root)["intent_hmac"][:32]
    stage_name = f".stage.{token}.FCStd"
    partial = data_root / "bootstrap" / "staging" / stage_name
    partial.write_bytes(b"partial")
    partial.chmod(0o600)
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001
    crashed = False

    def crash_after_partial_rename(root_fd, source_name, destination_name):
        nonlocal crashed
        renamed = original_rename(root_fd, source_name, destination_name)
        if renamed and source_name == stage_name and not crashed:
            crashed = True
            raise KeyboardInterrupt
        return renamed

    monkeypatch.setattr(
        project_create_module,
        "_rename_noreplace",
        crash_after_partial_rename,
    )
    with pytest.raises(KeyboardInterrupt):
        service.create_project(
            create_key=CREATE_KEY,
            kind=ProjectKind.IMPORT_FCSTD,
            source_path=str(source),
        )
    assert crashed is True
    monkeypatch.setattr(project_create_module, "_rename_noreplace", original_rename)

    recovered = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    replayed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )

    assert type(recovered) is ProjectCreateResult
    assert replayed == recovered
    for directory in ("staging", "work", "normalized"):
        _assert_only_zero_quarantine_tombstones(data_root / "bootstrap" / directory)
    app.close()


def test_durable_partial_original_and_quarantine_collision_requires_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _data_root(tmp_path)
    port = _HashingImportPort()
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )
    service = _durable_service(app)
    source = _source(tmp_path)
    slots = _hold_all_project_create_slots(app)
    try:
        blocked = service.create_project(
            create_key=CREATE_KEY,
            kind=ProjectKind.IMPORT_FCSTD,
            source_path=str(source),
        )
        _assert_port_failure(blocked, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
    finally:
        _release_leases(slots)
    record = _durable_record(data_root)
    token = record["intent_hmac"][:32]
    stage_name = f".stage.{token}.FCStd"
    stage = data_root / "bootstrap" / "staging" / stage_name
    stage.write_bytes(b"record-owned")
    stage.chmod(0o600)
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001
    crashed = False

    def crash_after_rename(root_fd, source_name, destination_name):
        nonlocal crashed
        renamed = original_rename(root_fd, source_name, destination_name)
        if renamed and source_name == stage_name and not crashed:
            crashed = True
            raise KeyboardInterrupt
        return renamed

    monkeypatch.setattr(project_create_module, "_rename_noreplace", crash_after_rename)
    with pytest.raises(KeyboardInterrupt):
        service.create_project(
            create_key=CREATE_KEY,
            kind=ProjectKind.IMPORT_FCSTD,
            source_path=str(source),
        )
    monkeypatch.setattr(project_create_module, "_rename_noreplace", original_rename)
    quarantine = next(stage.parent.glob(".quarantine.*.FCStd"))
    receipt = next(stage.parent.glob(".quarantine-receipt.*.json"))
    receipt_bytes = receipt.read_bytes()
    stage.write_bytes(b"must-survive")
    stage.chmod(0o600)

    recovered = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )

    _assert_port_failure(recovered, ProjectServicePortErrorCode.RECOVERY_REQUIRED)
    assert _durable_record(data_root)["phase"] == "RESERVED"
    assert port.paths == []
    assert stage.read_bytes() == b"must-survive"
    assert quarantine.read_bytes() == b"record-owned"
    assert receipt.read_bytes() == receipt_bytes
    app.close()


def test_durable_bound_quarantine_crash_replays_cleanup_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    service = _durable_service(app)
    source = _source(tmp_path)
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001
    crashed = False

    def crash_after_bound_rename(
        root_fd,
        source_name,
        destination_name,
        *,
        destination_root_fd=None,
    ):
        nonlocal crashed
        if destination_root_fd is None:
            renamed = original_rename(root_fd, source_name, destination_name)
        else:
            renamed = original_rename(
                root_fd,
                source_name,
                destination_name,
                destination_root_fd=destination_root_fd,
            )
        if renamed and source_name.startswith(".stage.") and not crashed:
            crashed = True
            raise KeyboardInterrupt
        return renamed

    monkeypatch.setattr(
        project_create_module,
        "_rename_noreplace",
        crash_after_bound_rename,
    )
    with pytest.raises(KeyboardInterrupt):
        service.create_project(
            create_key=CREATE_KEY,
            kind=ProjectKind.IMPORT_FCSTD,
            source_path=str(source),
        )
    assert crashed is True
    assert _durable_record(data_root)["phase"] == "CLEANUP_REQUIRED"
    monkeypatch.setattr(project_create_module, "_rename_noreplace", original_rename)

    recovered = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )

    assert type(recovered) is ProjectCreateResult
    assert _durable_record(data_root)["phase"] == "PUBLISHED"
    for directory in ("staging", "work", "normalized"):
        _assert_only_zero_quarantine_tombstones(data_root / "bootstrap" / directory)
    app.close()


def test_durable_bound_original_and_quarantine_collision_keeps_cleanup_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _data_root(tmp_path)
    port = _HashingImportPort()
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )
    service = _durable_service(app)
    source = _source(tmp_path)
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001
    crashed = False

    def crash_after_bound_rename(
        root_fd,
        source_name,
        destination_name,
        *,
        destination_root_fd=None,
    ):
        nonlocal crashed
        if destination_root_fd is None:
            renamed = original_rename(root_fd, source_name, destination_name)
        else:
            renamed = original_rename(
                root_fd,
                source_name,
                destination_name,
                destination_root_fd=destination_root_fd,
            )
        if renamed and source_name.startswith(".stage.") and not crashed:
            crashed = True
            raise KeyboardInterrupt
        return renamed

    monkeypatch.setattr(
        project_create_module,
        "_rename_noreplace",
        crash_after_bound_rename,
    )
    with pytest.raises(KeyboardInterrupt):
        service.create_project(
            create_key=CREATE_KEY,
            kind=ProjectKind.IMPORT_FCSTD,
            source_path=str(source),
        )
    monkeypatch.setattr(project_create_module, "_rename_noreplace", original_rename)
    record = _durable_record(data_root)
    assert record["phase"] == "CLEANUP_REQUIRED"
    stage_name = record["stage"]["name"]
    stage = data_root / "bootstrap" / "staging" / stage_name
    quarantine = next(stage.parent.glob(".quarantine.*.FCStd"))
    quarantine_bytes = quarantine.read_bytes()
    stage.write_bytes(b"must-survive")
    stage.chmod(0o600)

    replayed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )

    _assert_port_failure(replayed, ProjectServicePortErrorCode.RECOVERY_REQUIRED)
    assert _durable_record(data_root)["phase"] == "CLEANUP_REQUIRED"
    assert len(port.paths) == 1
    assert stage.read_bytes() == b"must-survive"
    assert quarantine.read_bytes() == quarantine_bytes
    app.close()


def test_validated_work_move_never_overwrites_a_racing_normalized_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    service = _durable_service(app)
    original_rename = project_create_module._rename_noreplace  # noqa: SLF001
    normalized_root = data_root / "bootstrap" / "normalized"
    injected = False

    def race_normalized_destination(
        root_fd,
        source_name,
        destination_name,
        *,
        destination_root_fd=None,
    ):
        nonlocal injected
        if destination_root_fd is not None and source_name.startswith(".work.") and not injected:
            replacement = normalized_root / destination_name
            replacement.write_bytes(b"must-survive")
            replacement.chmod(0o600)
            injected = True
        if destination_root_fd is None:
            return original_rename(root_fd, source_name, destination_name)
        return original_rename(
            root_fd,
            source_name,
            destination_name,
            destination_root_fd=destination_root_fd,
        )

    monkeypatch.setattr(
        project_create_module,
        "_rename_noreplace",
        race_normalized_destination,
    )

    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(_source(tmp_path)),
    )

    _assert_port_failure(created, ProjectServicePortErrorCode.RECOVERY_REQUIRED)
    assert injected is True
    normalized = next(normalized_root.glob(".normalized.*.FCStd"))
    assert normalized.read_bytes() == b"must-survive"
    app.close()


def test_successful_import_retains_paired_cleanup_receipts_across_restart(
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    service = _durable_service(app)
    source = _source(tmp_path)

    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    assert type(created) is ProjectCreateResult
    quarantines = tuple(
        path
        for directory in ("staging", "work", "normalized")
        for path in (data_root / "bootstrap" / directory).glob(".quarantine.*.FCStd")
    )
    receipts = tuple(
        path
        for directory in ("staging", "work", "normalized")
        for path in (data_root / "bootstrap" / directory).glob(".quarantine-receipt.*.json")
    )
    assert len(quarantines) == 2
    assert len(receipts) == len(quarantines)
    receipt_names = {path.name for path in receipts}
    for quarantine in quarantines:
        match = project_create_module._QUARANTINE_NAME.fullmatch(  # noqa: SLF001
            quarantine.name
        )
        assert match is not None
        expected_receipt = (
            f".quarantine-receipt.{match.group(1)}.{match.group(2)}.{match.group(3)}.json"
        )
        assert expected_receipt in receipt_names
        parsed = project_create_module._quarantine_receipt_from_bytes(  # noqa: SLF001
            (quarantine.parent / expected_receipt).read_bytes(),
            expected_name=expected_receipt,
        )
        assert parsed.quarantine_name == quarantine.name
        assert project_create_module._is_quarantine_tombstone(  # noqa: SLF001
            quarantine.stat(),
            parsed.binding,
        )
    app.close()

    reopened_app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    reopened = _durable_service(reopened_app)
    _, records, _ = reopened._scan_store()  # noqa: SLF001
    assert records == 1
    assert (
        reopened.create_project(
            create_key=CREATE_KEY,
            kind=ProjectKind.IMPORT_FCSTD,
            source_path=str(source),
        )
        == created
    )

    receipts[0].unlink()
    failed = reopened.create_project(
        create_key="project_create_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    _assert_port_failure(failed, ProjectServicePortErrorCode.RECOVERY_REQUIRED)
    reopened_app.close()


@pytest.mark.parametrize("point", ["stage", "work", "normalized"])
def test_durable_import_copy_phase_record_failure_recovers_without_second_cad(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
) -> None:
    port = _HashingImportPort()
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )
    service = _durable_service(app)
    source = _source(tmp_path)

    def target(body):
        if point == "stage":
            return body["phase"] == "STAGED" and body["work"] is None
        if point == "work":
            return (
                body["phase"] == "STAGED"
                and body["work"] is not None
                and body["validation_started"] is False
            )
        return body["phase"] == "VALIDATED"

    failed_writes = _fail_record_phase_once(monkeypatch, target)
    first = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    _assert_port_failure(first, ProjectServicePortErrorCode.STORE_FAILURE)
    assert len(failed_writes) == 1

    recovered = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    assert type(recovered) is ProjectCreateResult
    assert len(port.paths) == 1
    app.close()


def test_durable_post_cad_record_failure_never_executes_cad_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _HashingImportPort()
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )
    service = _durable_service(app)
    source = _source(tmp_path)
    failed_writes = _fail_record_phase_once(
        monkeypatch,
        lambda body: (
            body["phase"] == "STAGED"
            and body["validation_started"] is True
            and body["work_validated"] is True
        ),
    )

    first = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    _assert_port_failure(first, ProjectServicePortErrorCode.STORE_FAILURE)
    assert len(failed_writes) == 1
    assert len(port.paths) == 1
    assert _durable_record(data_root)["validation_started"] is True

    replayed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(source),
    )
    assert type(replayed) is ProjectCreateResult
    assert len(port.paths) == 1
    assert len(port.revalidation_paths) == 1
    assert not port.paths[0].is_absolute()
    assert not port.revalidation_paths[0].is_absolute()
    app.close()


@pytest.mark.parametrize(
    ("executor_code", "service_code"),
    [
        (ExecutorErrorCode.INVALID_INPUT, ProjectServicePortErrorCode.INVALID_INPUT),
        (ExecutorErrorCode.INVALID_CANDIDATE, ProjectServicePortErrorCode.INTERNAL_ERROR),
        (ExecutorErrorCode.INVALID_LEASE, ProjectServicePortErrorCode.INTERNAL_ERROR),
        (ExecutorErrorCode.CAD_FAILURE, ProjectServicePortErrorCode.CAD_FAILURE),
        (ExecutorErrorCode.ARTIFACT_FAILURE, ProjectServicePortErrorCode.INTEGRITY_FAILURE),
        (ExecutorErrorCode.INTEGRITY_FAILURE, ProjectServicePortErrorCode.INTEGRITY_FAILURE),
    ],
)
def test_durable_initial_validation_maps_exact_executor_failures(
    tmp_path: Path,
    executor_code: ExecutorErrorCode,
    service_code: ProjectServicePortErrorCode,
) -> None:
    port = _ValidationFailurePort(executor_code)
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )
    service = _durable_service(app)

    failed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(_source(tmp_path)),
    )

    _assert_port_failure(failed, service_code)
    assert len(port.paths) == 1
    if executor_code is ExecutorErrorCode.INVALID_INPUT:
        record = _durable_record(data_root)
        assert record["phase"] == "REJECTED"
        assert record["failure_code"] == "invalid_input"
        assert record["stage"] is None
        assert record["work"] is None
    app.close()


@pytest.mark.parametrize(
    ("executor_code", "service_code"),
    [
        (ExecutorErrorCode.INVALID_INPUT, ProjectServicePortErrorCode.INVALID_INPUT),
        (ExecutorErrorCode.INVALID_CANDIDATE, ProjectServicePortErrorCode.INTERNAL_ERROR),
        (ExecutorErrorCode.INVALID_LEASE, ProjectServicePortErrorCode.INTERNAL_ERROR),
        (ExecutorErrorCode.CAD_FAILURE, ProjectServicePortErrorCode.CAD_FAILURE),
        (ExecutorErrorCode.ARTIFACT_FAILURE, ProjectServicePortErrorCode.INTEGRITY_FAILURE),
        (ExecutorErrorCode.INTEGRITY_FAILURE, ProjectServicePortErrorCode.INTEGRITY_FAILURE),
    ],
)
def test_durable_revalidation_maps_exact_executor_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executor_code: ExecutorErrorCode,
    service_code: ProjectServicePortErrorCode,
) -> None:
    port = _RevalidationFailurePort(executor_code)
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )
    service = _durable_service(app)
    failed_writes = _fail_record_phase_once(
        monkeypatch,
        lambda body: (
            body["phase"] == "STAGED"
            and body["validation_started"] is True
            and body["work_validated"] is True
        ),
    )

    first = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(_source(tmp_path)),
    )
    _assert_port_failure(first, ProjectServicePortErrorCode.STORE_FAILURE)
    assert len(failed_writes) == 1

    replayed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(_source(tmp_path)),
    )

    _assert_port_failure(replayed, service_code)
    assert len(port.paths) == 1
    assert len(port.revalidation_paths) == 1
    if executor_code is ExecutorErrorCode.INVALID_INPUT:
        record = _durable_record(data_root)
        assert record["phase"] == "REJECTED"
        assert record["failure_code"] == "invalid_input"
        assert record["stage"] is None
        assert record["work"] is None
    app.close()


@pytest.mark.parametrize("kind", [ProjectKind.EMPTY, ProjectKind.IMPORT_FCSTD])
def test_durable_publication_receipt_failure_replays_original_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: ProjectKind,
) -> None:
    port = _HashingImportPort()
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )
    service = _durable_service(app)
    source = None if kind is ProjectKind.EMPTY else _source(tmp_path)
    target_phase = "PUBLISHED" if kind is ProjectKind.EMPTY else "CLEANUP_REQUIRED"
    failed_writes = _fail_record_phase_once(
        monkeypatch,
        lambda body: body["phase"] == target_phase,
    )

    first = service.create_project(
        create_key=CREATE_KEY,
        kind=kind,
        source_path=None if source is None else str(source),
    )
    _assert_port_failure(first, ProjectServicePortErrorCode.STORE_FAILURE)
    assert len(failed_writes) == 1
    reserved_id = _durable_record(data_root)["project_id"]

    recovered = service.create_project(
        create_key=CREATE_KEY,
        kind=kind,
        source_path=None if source is None else str(source),
    )
    assert type(recovered) is ProjectCreateResult
    assert recovered.project_id == reserved_id
    assert len(port.paths) == (0 if kind is ProjectKind.EMPTY else 1)
    app.close()


def test_durable_cleanup_runs_only_after_published_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    service = _durable_service(app)
    original = DurableProjectService._unlink_bound
    observed: list[tuple[str, str]] = []

    def observing_unlink(self, root, value):
        body = _durable_record(data_root)
        observed.append((body["phase"], body["outcome"]))
        return original(self, root, value)

    monkeypatch.setattr(DurableProjectService, "_unlink_bound", observing_unlink)
    created = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=str(_source(tmp_path)),
    )
    assert type(created) is ProjectCreateResult
    assert observed
    assert set(observed) == {("CLEANUP_REQUIRED", "PUBLISHED")}
    app.close()


@pytest.mark.parametrize("fatal_type", [KeyboardInterrupt, SystemExit])
def test_durable_service_releases_owned_capacity_before_propagating_system_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fatal_type,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    service = _durable_service(app)
    original_resume = DurableProjectService._resume

    def interrupt(*_args, **_kwargs):
        raise fatal_type

    monkeypatch.setattr(DurableProjectService, "_resume", interrupt)
    with pytest.raises(fatal_type):
        service.create_project(
            create_key=CREATE_KEY,
            kind=ProjectKind.EMPTY,
            source_path=None,
        )

    monkeypatch.setattr(DurableProjectService, "_resume", original_resume)
    recovered = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert type(recovered) is ProjectCreateResult
    app.close()


@pytest.mark.parametrize("fault", ["create", "iterate", "close"])
def test_durable_scandir_io_fault_maps_to_store_failure_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    service = _durable_service(app)
    original_scandir = os.scandir

    class FaultingScandir:
        def __init__(self, value):
            self.value = value

        def __iter__(self):
            if fault == "iterate":
                raise OSError("private iteration failure")
            return iter(self.value)

        def close(self):
            self.value.close()
            if fault == "close":
                raise OSError("private close failure")

    def injected_scandir(path):
        if fault == "create":
            raise OSError("private create failure")
        return FaultingScandir(original_scandir(path))

    monkeypatch.setattr(os, "scandir", injected_scandir)
    failed = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    _assert_port_failure(failed, ProjectServicePortErrorCode.STORE_FAILURE)

    monkeypatch.setattr(os, "scandir", original_scandir)
    recovered = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert type(recovered) is ProjectCreateResult
    app.close()


def test_durable_record_replace_checks_physical_peak_not_only_final_logical_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    service = _durable_service(app)
    slots = _hold_all_project_create_slots(app)
    try:
        blocked = service.create_project(
            create_key=CREATE_KEY,
            kind=ProjectKind.EMPTY,
            source_path=None,
        )
        _assert_port_failure(blocked, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
    finally:
        _release_leases(slots)

    requests = data_root / "bootstrap" / "requests"
    record_path = requests / "request_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json"
    old_raw = record_path.read_bytes()
    current_physical = sum(
        path.stat().st_size for path in (data_root / "bootstrap").rglob("*") if path.is_file()
    )
    monkeypatch.setattr(project_create_module, "_MAX_STORE_BYTES", current_physical)
    first = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    _assert_port_failure(first, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
    assert record_path.read_bytes() == old_raw

    record = service._load_record(CREATE_KEY)  # noqa: SLF001
    assert record is not None
    generation_zero = service._generation_zero(record)  # noqa: SLF001
    terminal = replace(
        record,
        phase="PUBLISHED",
        reservation_bytes=0,
        outcome="PUBLISHED",
        generation_zero=generation_zero,
    )
    new_size = len(project_create_module._record_bytes(terminal))  # noqa: SLF001
    final_logical = current_physical - len(old_raw) + new_size
    physical_peak = current_physical + new_size
    assert final_logical < physical_peak

    monkeypatch.setattr(project_create_module, "_MAX_STORE_BYTES", final_logical)
    peak_blocked = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    _assert_port_failure(peak_blocked, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
    assert record_path.read_bytes() == old_raw

    monkeypatch.setattr(project_create_module, "_MAX_STORE_BYTES", physical_peak)
    recovered = service.create_project(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        source_path=None,
    )
    assert type(recovered) is ProjectCreateResult
    app.close()


def test_durable_eight_actual_attempts_hold_exactly_eight_live_slots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    service = _durable_service(app)
    original_publish = DurableProjectService._publish
    guard = threading.Lock()
    all_entered = threading.Event()
    release = threading.Event()
    entered = 0

    def blocking_publish(self, record):
        nonlocal entered
        with guard:
            entered += 1
            if entered == 8:
                all_entered.set()
        assert release.wait(timeout=5)
        return original_publish(self, record)

    monkeypatch.setattr(DurableProjectService, "_publish", blocking_publish)
    results: list[object] = [None] * 8

    def create_at(index: int) -> None:
        results[index] = service.create_project(
            create_key=f"project_create_{index:032x}",
            kind=ProjectKind.EMPTY,
            source_path=None,
        )

    threads = [threading.Thread(target=create_at, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    try:
        assert all_entered.wait(timeout=5)
        ninth = service.create_project(
            create_key="project_create_ffffffffffffffffffffffffffffffff",
            kind=ProjectKind.EMPTY,
            source_path=None,
        )
        _assert_port_failure(ninth, ProjectServicePortErrorCode.RESOURCE_EXHAUSTED)
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert all(type(value) is ProjectCreateResult for value in results)
    app.close()


class _HashingImportPort(CadExecutionPort):
    def __init__(
        self,
        *,
        swap_after_validation: bool = False,
        swap_root_to: Path | None = None,
        swap_live_root: Path | None = None,
        normalized_after_root_swap: bytes | None = None,
    ) -> None:
        self.paths = []
        self.revalidation_paths = []
        self.swap_after_validation = swap_after_validation
        self.swap_root_to = swap_root_to
        self.swap_live_root = swap_live_root
        self.normalized_after_root_swap = normalized_after_root_swap

    def validate_import(self, path: Path) -> ValidatedImportEvidence:
        self.paths.append(path)
        content = path.read_bytes()
        if self.swap_after_validation:
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(b"swapped-after-validation")
            replacement.chmod(0o600)
            os.replace(replacement, path)
        if self.swap_root_to is not None:
            assert self.swap_live_root is not None
            detached = self.swap_live_root.with_name("bootstrap-detached")
            self.swap_live_root.rename(detached)
            self.swap_live_root.symlink_to(
                self.swap_root_to,
                target_is_directory=True,
            )
            if self.normalized_after_root_swap is not None:
                path.write_bytes(self.normalized_after_root_swap)
                path.chmod(0o600)
                content = self.normalized_after_root_swap
        return ValidatedImportEvidence(
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )

    def revalidate_normalized_import(self, path: Path) -> ValidatedImportEvidence:
        self.revalidation_paths.append(path)
        content = path.read_bytes()
        return ValidatedImportEvidence(
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )


class _NormalizingImportPort(CadExecutionPort):
    def __init__(self, normalized: bytes) -> None:
        self.normalized = normalized
        self.before_inode = None
        self.after_inode = None

    def validate_import(self, path: Path) -> ValidatedImportEvidence:
        self.before_inode = path.stat().st_ino
        temporary = path.with_name(f"{path.name}.normalized")
        temporary.write_bytes(self.normalized)
        temporary.chmod(0o600)
        os.replace(temporary, path)
        self.after_inode = path.stat().st_ino
        return ValidatedImportEvidence(
            sha256=hashlib.sha256(self.normalized).hexdigest(),
            size_bytes=len(self.normalized),
        )


class _RevalidationFailurePort(_HashingImportPort):
    def __init__(self, code: ExecutorErrorCode) -> None:
        super().__init__()
        self.code = code

    def revalidate_normalized_import(self, path: Path) -> ValidatedImportEvidence:
        self.revalidation_paths.append(path)
        raise ExecutorError(self.code)


class _ValidationFailurePort(CadExecutionPort):
    def __init__(self, code: ExecutorErrorCode) -> None:
        self.code = code
        self.paths: list[Path] = []

    def validate_import(self, path: Path) -> ValidatedImportEvidence:
        self.paths.append(path)
        raise ExecutorError(self.code)


def test_import_bootstrap_publishes_only_exact_validated_generation_zero(tmp_path: Path):
    port = _HashingImportPort()
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: port,
    )
    source = _source(tmp_path)
    result = app.bootstrap_import(source=source)

    expected_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert result.head.project_id == PROJECT_ID
    assert result.head.generation == 0
    assert result.revision.id == result.head.revision_id
    assert result.revision.base_revision is None
    assert result.revision.model is not None
    assert result.revision.model.sha256 == expected_digest
    assert result.revision.model.size_bytes == source.stat().st_size
    assert result.cleanup_required is False
    assert len(port.paths) == 1
    assert not port.paths[0].is_absolute()
    assert not port.paths[0].exists()

    durable = app._revision_store.revision_model_path(  # noqa: SLF001
        PROJECT_ID, result.head.revision_id
    )
    assert durable.read_bytes() == source.read_bytes()
    assert durable.stat().st_ino != source.stat().st_ino

    checkout = app.open_checkout(
        open_key="checkout_open_0123456789abcdef0123456789abcdef",
        source=HeadCheckoutSource(project_id=PROJECT_ID),
    )
    assert checkout.state is CheckoutState.OPEN
    assert checkout.local_path is not None
    assert checkout.local_path.read_bytes() == source.read_bytes()
    assert "local_path" not in checkout.to_wire_mapping()
    closed = app.close_checkout(checkout_id=checkout.checkout_id)
    assert closed.state is CheckoutState.CLOSED
    assert closed.local_path is None
    assert durable.read_bytes() == source.read_bytes()
    app.close()


def test_legacy_bootstrap_publication_also_uses_descriptor_bound_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, str, RevisionSourceBinding]] = []
    original_at = LocalRevisionStore.import_trusted_fcstd_at

    def forbid_path_import(*_args, **_kwargs):
        raise AssertionError("path import must not be used")

    def record_at(self, project_id, **kwargs):
        calls.append(
            (
                kwargs["source_parent_fd"],
                kwargs["source_name"],
                kwargs["expected_binding"],
            )
        )
        return original_at(self, project_id, **kwargs)

    monkeypatch.setattr(LocalRevisionStore, "import_trusted_fcstd", forbid_path_import)
    monkeypatch.setattr(LocalRevisionStore, "import_trusted_fcstd_at", record_at)
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )

    created = app.bootstrap_import(source=_source(tmp_path))

    assert type(created) is project_module.ProjectBootstrapResult
    assert len(calls) == 1
    parent_fd, source_name, binding = calls[0]
    assert project_module._STAGE_NAME.fullmatch(source_name) is not None  # noqa: SLF001
    assert type(binding) is RevisionSourceBinding
    with pytest.raises(OSError):
        os.fstat(parent_fd)
    app.close()


def test_import_validation_runs_inside_the_candidate_file_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class RecordingLimit:
        def __enter__(self):
            events.append("file_limit.enter")
            return self

        def __exit__(self, *_args):
            events.append("file_limit.exit")
            return False

    class RecordingPort(_HashingImportPort):
        def validate_import(self, path: Path) -> ValidatedImportEvidence:
            events.append("validate_import")
            return super().validate_import(path)

    port = RecordingPort()
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: port,
    )
    monkeypatch.setattr(
        project_module,
        "_candidate_file_limit",
        lambda store: RecordingLimit() if store is app._revision_store else None,  # noqa: SLF001
    )

    result = app.bootstrap_import(source=_source(tmp_path))

    assert result.head.project_id == PROJECT_ID
    assert events == ["file_limit.enter", "validate_import", "file_limit.exit"]
    app.close()


def test_import_bootstrap_accepts_trusted_atomic_normalization(tmp_path: Path):
    normalized = b"normalized-fcstd-with-different-size"
    port = _NormalizingImportPort(normalized)
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: port,
    )

    result = app.bootstrap_import(source=_source(tmp_path, b"raw"))

    assert port.before_inode is not None
    assert port.after_inode is not None
    assert port.after_inode != port.before_inode
    assert result.revision.model is not None
    assert result.revision.model.sha256 == hashlib.sha256(normalized).hexdigest()
    assert result.revision.model.size_bytes == len(normalized)
    durable = app._revision_store.revision_model_path(  # noqa: SLF001
        PROJECT_ID, result.revision.id
    )
    assert durable.read_bytes() == normalized
    app.close()


@pytest.mark.parametrize("kind", ["empty", "import"])
def test_generation_zero_readback_survives_lease_release_response_loss(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_release = ResourceLeaseManager.release
    release_calls = 0

    def release_then_lose_response(self, lease, *, owner_token):
        nonlocal release_calls
        original_release(self, lease, owner_token=owner_token)
        if type(lease) is not ProjectWriteLease:
            return
        release_calls += 1
        raise LeaseError(LeaseErrorCode.IO_ERROR, resource_key=lease.resource_key)

    monkeypatch.setattr(ResourceLeaseManager, "release", release_then_lose_response)
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )

    if kind == "empty":
        result = app.bootstrap_empty()
    else:
        result = app.bootstrap_import(source=_source(tmp_path))

    assert release_calls == 1
    assert result.head.project_id == PROJECT_ID
    assert result.head.generation == 0
    assert result.cleanup_required is True
    assert app._revision_store.load_head(PROJECT_ID) == result.head  # noqa: SLF001
    app.close()


@pytest.mark.parametrize("kind", ["empty", "import"])
def test_generation_zero_does_not_report_success_if_lease_release_never_took_effect(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_release = ResourceLeaseManager.release
    captured = []

    def fail_before_release(self, lease, *, owner_token):
        if type(lease) is not ProjectWriteLease:
            return original_release(self, lease, owner_token=owner_token)
        captured.append(lease)
        raise LeaseError(LeaseErrorCode.IO_ERROR, resource_key=lease.resource_key)

    monkeypatch.setattr(ResourceLeaseManager, "release", fail_before_release)
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )

    with pytest.raises(LeaseError) as caught:
        if kind == "empty":
            app.bootstrap_empty()
        else:
            app.bootstrap_import(source=_source(tmp_path))

    assert caught.value.code is LeaseErrorCode.IO_ERROR
    assert len(captured) == 1
    assert captured[0].released is False
    monkeypatch.setattr(ResourceLeaseManager, "release", original_release)
    captured[0].release(owner_token=captured[0].owner_token)
    app.close()


def test_import_bootstrap_uses_a_thread_local_pinned_working_directory(tmp_path: Path):
    process_working_directory = Path.cwd()
    observed: list[Path] = []

    class ObservingImportPort(CadExecutionPort):
        def validate_import(self, path: Path) -> ValidatedImportEvidence:
            observer = threading.Thread(target=lambda: observed.append(Path.cwd()))
            observer.start()
            observer.join()
            content = path.read_bytes()
            return ValidatedImportEvidence(
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
            )

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: ObservingImportPort(),
    )

    result = app.bootstrap_import(source=_source(tmp_path))

    assert result.head.generation == 0
    assert observed == [process_working_directory]
    assert Path.cwd() == process_working_directory
    app.close()


def test_import_bootstrap_rejects_staging_swap_before_project_publication(tmp_path: Path):
    port = _HashingImportPort(swap_after_validation=True)
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: port,
    )

    with pytest.raises(RevisionStoreError) as caught:
        app.bootstrap_import(source=_source(tmp_path))
    assert caught.value.code is RevisionStoreErrorCode.CORRUPT_CONTENT
    assert tuple((data_root / "projects").iterdir()) == ()
    assert len(port.paths) == 1
    assert not port.paths[0].exists()
    app.close()


def test_import_bootstrap_rejects_root_swap_without_touching_outside(
    tmp_path: Path,
):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(
            swap_root_to=outside,
            swap_live_root=data_root / "bootstrap",
        ),
    )

    with pytest.raises(RevisionStoreError) as caught:
        app.bootstrap_import(source=_source(tmp_path))
    assert caught.value.code is RevisionStoreErrorCode.CORRUPT_CONTENT
    assert tuple(outside.iterdir()) == ()
    assert tuple((data_root / "projects").iterdir()) == ()
    assert tuple((data_root / "bootstrap-detached").iterdir()) == ()
    app.close()


def test_import_bootstrap_never_exposes_live_root_path_to_cad_checkpoint(
    tmp_path: Path,
):
    outside = tmp_path / "outside-cad-window"
    outside.mkdir(mode=0o700)
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(
            swap_root_to=outside,
            swap_live_root=data_root / "bootstrap",
            normalized_after_root_swap=b"trusted-normalized-copy",
        ),
    )

    with pytest.raises(RevisionStoreError) as caught:
        app.bootstrap_import(source=_source(tmp_path))

    assert caught.value.code is RevisionStoreErrorCode.CORRUPT_CONTENT
    assert tuple(outside.iterdir()) == ()
    assert tuple((data_root / "projects").iterdir()) == ()
    assert tuple((data_root / "bootstrap-detached").iterdir()) == ()
    app.close()


def test_import_bootstrap_recovers_a_lost_post_publication_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    original = LocalRevisionStore.import_trusted_fcstd_at

    def publish_then_lose_response(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RevisionStoreError(
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            head_committed=True,
        )

    monkeypatch.setattr(
        LocalRevisionStore,
        "import_trusted_fcstd_at",
        publish_then_lose_response,
    )
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    result = app.bootstrap_import(source=_source(tmp_path))
    assert result.head.generation == 0
    assert result.revision.id == result.head.revision_id
    assert result.cleanup_required is False
    app.close()


def test_import_bootstrap_keeps_exact_success_after_postpublication_root_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    outside = tmp_path / "outside-postpublication"
    outside.mkdir(mode=0o700)
    data_root = _data_root(tmp_path)
    original = LocalRevisionStore.import_trusted_fcstd_at

    def publish_then_replace_live_root(self, *args, **kwargs):
        outcome = original(self, *args, **kwargs)
        live = data_root / "bootstrap"
        live.rename(data_root / "bootstrap-detached")
        live.symlink_to(outside, target_is_directory=True)
        return outcome

    monkeypatch.setattr(
        LocalRevisionStore,
        "import_trusted_fcstd_at",
        publish_then_replace_live_root,
    )
    source = _source(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )

    result = app.bootstrap_import(source=source)

    assert result.head.project_id == PROJECT_ID
    assert result.head.generation == 0
    assert result.revision.model is not None
    assert result.revision.model.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result.cleanup_required is True
    project_entries = tuple(
        entry for entry in (data_root / "projects").iterdir() if not entry.name.startswith(".")
    )
    assert len(project_entries) == 1
    assert tuple(outside.iterdir()) == ()
    assert tuple((data_root / "bootstrap-detached").iterdir()) == ()
    app.close()


def test_import_cleanup_failure_keeps_success_and_a_durable_retry_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    original_remove = project_module._remove_staging
    remove_calls = 0

    def fail_once(path, **kwargs):
        nonlocal remove_calls
        remove_calls += 1
        if remove_calls == 1:
            return False
        return original_remove(path, **kwargs)

    monkeypatch.setattr(project_module, "_remove_staging", fail_once)
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    result = app.bootstrap_import(source=_source(tmp_path))
    assert result.head.generation == 0
    assert result.cleanup_required is True
    records = tuple((data_root / "bootstrap").glob("cleanup_*.json"))
    assert len(records) == 1
    assert records[0].stat().st_mode & 0o777 == 0o600
    app.close()

    reopened = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    assert tuple((data_root / "bootstrap").iterdir()) == ()
    reopened.close()


def test_cleanup_recovery_does_not_follow_external_record_symlink(tmp_path: Path):
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    app.close()
    outside = tmp_path / "outside-cleanup.json"
    outside.write_text(
        '{"project_id":"project_22222222222222222222222222222222",'
        '"published":true,"schema_version":1,'
        '"stage_name":".import.22222222222222222222222222222222.FCStd"}',
        encoding="utf-8",
    )
    record = data_root / "bootstrap" / ("cleanup_22222222222222222222222222222222.json")
    record.symlink_to(outside)

    reopened = AgentApplication.open(data_root=data_root)
    assert record.is_symlink()
    assert outside.exists()
    reopened.close()


def test_import_never_claims_cleanup_authority_when_record_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        project_module,
        "_remove_staging",
        lambda _path, **_kwargs: False,
    )

    def fail_record(*_args, **_kwargs):
        raise OSError

    monkeypatch.setattr(project_module, "_write_cleanup_record", fail_record)
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    with pytest.raises(RevisionStoreError) as caught:
        app.bootstrap_import(source=_source(tmp_path))
    assert caught.value.code is RevisionStoreErrorCode.DURABILITY_UNCERTAIN
    assert caught.value.head_committed is True
    assert app._revision_store.load_head(PROJECT_ID).generation == 0  # noqa: SLF001
    app.close()


def test_import_primary_failure_and_lost_cleanup_record_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class FailingImportPort(CadExecutionPort):
        def validate_import(self, path: Path) -> ValidatedImportEvidence:
            assert path.read_bytes() == b"normalized-fcstd"
            raise ValueError("private CAD failure")

    monkeypatch.setattr(
        project_module,
        "_remove_staging",
        lambda _path, **_kwargs: False,
    )

    def fail_record(*_args, **_kwargs):
        raise OSError

    monkeypatch.setattr(project_module, "_write_cleanup_record", fail_record)
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: FailingImportPort(),
    )

    with pytest.raises(RevisionStoreError) as caught:
        app.bootstrap_import(source=_source(tmp_path))

    assert caught.value.code is RevisionStoreErrorCode.CLEANUP_REQUIRED
    assert not hasattr(caught.value, "head_committed")
    assert type(caught.value.__cause__) is ValueError
    assert tuple((data_root / "projects").iterdir()) == ()
    assert len(tuple((data_root / "bootstrap").glob(".import.*.FCStd"))) == 1
    assert tuple((data_root / "bootstrap").glob("cleanup_*.json")) == ()
    app.close()


def test_postpublication_primary_failure_and_lost_cleanup_record_is_durability_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        project_module,
        "_remove_staging",
        lambda _path, **_kwargs: False,
    )

    def fail_record(*_args, **_kwargs):
        raise OSError

    monkeypatch.setattr(project_module, "_write_cleanup_record", fail_record)

    def lose_readback(*_args, **_kwargs):
        raise ValueError("private readback failure")

    monkeypatch.setattr(project_module, "verify_generation_zero", lose_readback)
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )

    with pytest.raises(RevisionStoreError) as caught:
        app.bootstrap_import(source=_source(tmp_path))

    assert caught.value.code is RevisionStoreErrorCode.DURABILITY_UNCERTAIN
    assert caught.value.head_committed is True
    assert type(caught.value.__cause__) is ValueError
    assert app._revision_store.load_head(PROJECT_ID).generation == 0  # noqa: SLF001
    assert len(tuple((data_root / "bootstrap").glob(".import.*.FCStd"))) == 1
    assert tuple((data_root / "bootstrap").glob("cleanup_*.json")) == ()
    app.close()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_import_bootstrap_rejects_linked_external_sources(tmp_path: Path, kind: str):
    source = _source(tmp_path)
    linked = tmp_path / "linked.FCStd"
    if kind == "symlink":
        linked.symlink_to(source)
    else:
        os.link(source, linked)
    calls = []
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: calls.append("port"),
    )
    with pytest.raises(ValueError):
        app.bootstrap_import(source=linked)
    assert calls == []
    app.close()
