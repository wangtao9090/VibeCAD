"""Secure generation-zero project bootstrap tests."""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest

import vibecad.application.agent as agent_module
import vibecad.application.project as project_module
from vibecad.application.agent import AgentApplication
from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionStoreError,
    RevisionStoreErrorCode,
)
from vibecad.interaction.cad import CadExecutionPort, ValidatedImportEvidence
from vibecad.interaction.checkouts import CheckoutState, HeadCheckoutSource
from vibecad.workflow.lease import LeaseError, LeaseErrorCode, ResourceLeaseManager

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
    original = LocalRevisionStore.import_trusted_fcstd

    def publish_then_lose_response(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RevisionStoreError(
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            head_committed=True,
        )

    monkeypatch.setattr(
        LocalRevisionStore,
        "import_trusted_fcstd",
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
    original = LocalRevisionStore.import_trusted_fcstd

    def publish_then_replace_live_root(self, *args, **kwargs):
        outcome = original(self, *args, **kwargs)
        live = data_root / "bootstrap"
        live.rename(data_root / "bootstrap-detached")
        live.symlink_to(outside, target_is_directory=True)
        return outcome

    monkeypatch.setattr(
        LocalRevisionStore,
        "import_trusted_fcstd",
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
    assert len(tuple((data_root / "projects").iterdir())) == 1
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
