from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from vibecad._file_compat import (
    WindowsExternalFileCapability,
    open_windows_external_file,
    set_private_dacl,
    validate_windows_external_file,
)
from vibecad.application.agent import AgentApplication
from vibecad.application.project_api import (
    ProjectCreateResult,
    ProjectCurrentResult,
    ProjectKind,
    ProjectServicePortErrorCode,
    ProjectServicePortFailure,
)
from vibecad.application.project_create import DurableProjectService
from vibecad.interaction.cad import CadExecutionPort, ValidatedImportEvidence

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only contract")

_CREATE_KEY = "project_create_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class _HashingImportPort(CadExecutionPort):
    def validate_import(self, path: Path) -> ValidatedImportEvidence:
        raw = path.read_bytes()
        return ValidatedImportEvidence(
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
        )

    def revalidate_normalized_import(self, path: Path) -> ValidatedImportEvidence:
        return self.validate_import(path)


def _service(app: AgentApplication) -> DurableProjectService:
    return DurableProjectService(
        bootstrap_root=app._layout.bootstrap,  # noqa: SLF001
        data_root=app._layout.root,  # noqa: SLF001
        revision_store=app._revision_store,  # noqa: SLF001
        lease_manager=app._lease_manager,  # noqa: SLF001
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )


def _data_root(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home / "data"


def test_windows_project_import_restarts_and_replays_without_source(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    source = tmp_path / "source.FCStd"
    payload = b"windows durable project import"
    source.write_bytes(payload)

    app = AgentApplication.open(data_root=data_root)
    created = _service(app).create_project(
        create_key=_CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        source_path=os.fspath(source),
    )
    assert type(created) is ProjectCreateResult
    assert created.revision.model is not None
    assert created.revision.model.sha256 == hashlib.sha256(payload).hexdigest()
    project_id = created.project_id
    app.close()
    source.unlink()

    reopened = AgentApplication.open(data_root=data_root)
    try:
        service = _service(reopened)
        replayed = service.create_project(
            create_key=_CREATE_KEY,
            kind=ProjectKind.IMPORT_FCSTD,
            source_path=os.fspath(source),
        )
        current = service.get_project(project_id=project_id)
        assert replayed == created
        assert type(current) is ProjectCurrentResult
        assert current.head == created.head
        assert current.revision == created.revision
    finally:
        reopened.close()


def test_windows_agent_public_import_request_restarts_without_source(
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    source = tmp_path / "public-source.FCStd"
    source.write_bytes(b"windows public project import")
    request = {
        "schema_version": 1,
        "create_key": _CREATE_KEY,
        "kind": "import_fcstd",
        "source_path": os.fspath(source),
    }

    app = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    created = app.create_project_request(request)
    assert created["ok"] is True
    project_id = created["result"]["project_id"]
    app.close()
    source.unlink()

    reopened = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _HashingImportPort(),
    )
    try:
        assert reopened.create_project_request(request) == created
        current = reopened.get_project_request(
            {"schema_version": 1, "project_id": project_id}
        )
        assert current["ok"] is True
        assert current["result"]["current"] == created["result"]["generation_zero"]
    finally:
        reopened.close()


def test_windows_project_import_rejects_same_size_content_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _data_root(tmp_path)
    source = tmp_path / "source.FCStd"
    source.write_bytes(b"original-contents")
    app = AgentApplication.open(data_root=data_root)
    original_copy = DurableProjectService._copy_source_to_stage

    def rewrite_then_copy(self, opened, record):
        source.write_bytes(b"rewritten-content")
        assert source.stat().st_size == opened.before.st_size
        return original_copy(self, opened, record)

    monkeypatch.setattr(DurableProjectService, "_copy_source_to_stage", rewrite_then_copy)
    try:
        failed = _service(app).create_project(
            create_key=_CREATE_KEY,
            kind=ProjectKind.IMPORT_FCSTD,
            source_path=os.fspath(source),
        )
        assert type(failed) is ProjectServicePortFailure
        assert failed.code is ProjectServicePortErrorCode.INVALID_INPUT
    finally:
        app.close()


def test_windows_external_capability_rejects_same_content_file_id_replacement(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.FCStd"
    payload = b"same content, new File ID"
    source.write_bytes(payload)
    descriptor, capability = open_windows_external_file(source)
    os.close(descriptor)

    displaced = tmp_path / "displaced.FCStd"
    source.rename(displaced)
    source.write_bytes(payload)
    with pytest.raises(OSError):
        validate_windows_external_file(capability)

    mapping = capability.to_mapping()
    assert len(mapping["volume"]) == 16
    assert len(mapping["file_id"]) == 32
    assert WindowsExternalFileCapability.from_mapping(mapping) == capability
    with pytest.raises(ValueError):
        WindowsExternalFileCapability.from_mapping(
            mapping | {"file_id": f"A{str(mapping['file_id'])[1:]}"}
        )


def test_windows_bootstrap_recovery_removes_only_private_orphan_guard(
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    app.close()
    bootstrap = data_root / "bootstrap"
    orphan = bootstrap / f".bootstrap-guard.{'a' * 32}"
    orphan.write_bytes(b"")
    set_private_dacl(orphan)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"must survive")
    reparse = bootstrap / f".bootstrap-guard.{'b' * 32}"
    reparse.symlink_to(outside)

    reopened = AgentApplication.open(data_root=data_root)
    try:
        assert not orphan.exists()
        assert reparse.is_symlink()
        assert outside.read_bytes() == b"must survive"
    finally:
        reopened.close()
