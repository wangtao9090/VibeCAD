from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from vibecad.engine.document_assets import (
    DocumentAssetWorkspace,
    DocumentAssetWorkspaceError,
    DocumentAssetWorkspaceErrorCode,
)
from vibecad.engine.session import Session
from vibecad.runtime import paths, status

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"


@pytest.fixture(scope="session")
def existing_managed_runtime_python() -> str:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the real FreeCAD gate")
    python = paths.active_runtime_python()
    if not python.is_file() or not paths.ready_sentinel().is_file():
        pytest.fail("an existing ready managed FreeCAD runtime is required")
    if not status.engine_compatible(python):
        pytest.fail("the existing managed FreeCAD runtime does not match current engine pins")
    return str(python)


class _AssetDocument:
    def __init__(self, name: str = "AssetDocument") -> None:
        self.Name = name
        self.TransientDir = ""
        self.UndoMode = 0


class _RejectingAssetDocument(_AssetDocument):
    @property
    def TransientDir(self) -> str:  # type: ignore[override]
        return ""

    @TransientDir.setter
    def TransientDir(self, value: str) -> None:
        if value:
            raise OSError("setter fault")


def _private_root(tmp_path: Path) -> Path:
    root = tmp_path / "document-assets"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return root


def test_workspace_attaches_exact_private_directory_and_cleans_only_owned_tree(
    tmp_path: Path,
) -> None:
    root = _private_root(tmp_path)
    unrelated = tmp_path / "project.FCStd"
    unrelated.write_bytes(b"durable project")
    document = _AssetDocument()
    workspace = DocumentAssetWorkspace(root)

    directory = workspace.attach(document)

    info = directory.lstat()
    assert directory.parent == root
    assert document.TransientDir == str(directory)
    assert stat.S_ISDIR(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o700
    assert workspace.require_attached(document) == directory
    retained = directory / "included.bin"
    retained.write_bytes(b"included bytes")
    os.chmod(retained, 0o600)

    workspace.release_after_close(document)

    assert not directory.exists()
    assert root.is_dir()
    assert unrelated.read_bytes() == b"durable project"


@pytest.mark.parametrize("mode", (0o750, 0o770, 0o777))
def test_workspace_rejects_non_private_configured_root(tmp_path: Path, mode: int) -> None:
    root = _private_root(tmp_path)
    os.chmod(root, mode)

    with pytest.raises(DocumentAssetWorkspaceError) as caught:
        DocumentAssetWorkspace(root)

    assert caught.value.code is DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED
    assert caught.value.path == "/workspace/root"


def test_workspace_rejects_symlink_root_and_preexisting_document_path(
    tmp_path: Path,
) -> None:
    root = _private_root(tmp_path)
    alias = tmp_path / "root-alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(DocumentAssetWorkspaceError) as caught:
        DocumentAssetWorkspace(alias)
    assert caught.value.code is DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED

    workspace = DocumentAssetWorkspace(root)
    document = _AssetDocument()
    document.TransientDir = str(tmp_path / "model-selected-path")
    with pytest.raises(DocumentAssetWorkspaceError) as caught:
        workspace.attach(document)
    assert caught.value.code is DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED
    assert tuple(root.iterdir()) == ()


def test_workspace_attach_failure_and_drift_are_bounded_and_recoverable(
    tmp_path: Path,
) -> None:
    root = _private_root(tmp_path)
    workspace = DocumentAssetWorkspace(root)

    with pytest.raises(DocumentAssetWorkspaceError) as caught:
        workspace.attach(_RejectingAssetDocument())
    assert caught.value.code is DocumentAssetWorkspaceErrorCode.ATTACH_FAILED
    assert tuple(root.iterdir()) == ()

    document = _AssetDocument()
    directory = workspace.attach(document)
    os.chmod(directory, 0o755)
    with pytest.raises(DocumentAssetWorkspaceError) as caught:
        workspace.require_attached(document)
    assert caught.value.code is DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED
    os.chmod(directory, 0o700)
    workspace.release_after_close(document)
    assert tuple(root.iterdir()) == ()


def test_default_workspace_removes_its_private_parent_after_close() -> None:
    document = _AssetDocument()
    workspace = DocumentAssetWorkspace()
    directory = workspace.attach(document)
    owned_root = directory.parent
    assert owned_root.is_dir()

    workspace.release_after_close(document)

    assert not directory.exists()
    assert not owned_root.exists()


def test_session_attaches_before_open_or_load_and_cleans_failed_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _private_root(tmp_path)
    source = tmp_path / "candidate.FCStd"
    source.write_bytes(b"not an FCStd")
    created: list[_AssetDocument] = []
    closed: list[_AssetDocument] = []

    class Candidate(_AssetDocument):
        def load(self, path: str) -> None:
            assert Path(path) == source
            assert self.TransientDir
            assert Path(self.TransientDir).is_dir()
            raise ValueError("invalid FCStd")

        def recompute(self) -> None:
            raise AssertionError("unreachable")

    def new_document(*args: object) -> Candidate:
        candidate = Candidate(f"Candidate{len(created)}")
        created.append(candidate)
        return candidate

    session = Session(document_asset_root=root)
    monkeypatch.setattr(session, "_ensure_freecad", lambda: None)
    monkeypatch.setitem(sys.modules, "FreeCAD", SimpleNamespace(newDocument=new_document))

    def close_owned(document: _AssetDocument) -> None:
        closed.append(document)

    monkeypatch.setattr(session, "_close_owned_document", close_owned)
    monkeypatch.setattr(
        session,
        "_replace_document",
        lambda document, *, restore_state: (document, restore_state),
    )

    opened, restore_state = session.open_document("Open")
    assert restore_state is False
    opened_dir = Path(opened.TransientDir)
    assert opened_dir.is_dir()
    session._document_assets.release_after_close(opened)

    with pytest.raises(ValueError, match="invalid FCStd"):
        session.load_document(source)
    assert closed == [created[-1]]
    assert not Path(created[-1].TransientDir).exists()
    assert tuple(root.iterdir()) == ()


@pytest.mark.slow
def test_real_freecad_included_asset_roundtrip_abort_and_candidate_cleanup(
    existing_managed_runtime_python: str,
    tmp_path: Path,
) -> None:
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {str(_SRC)!r})\n"
        + "import hashlib, os, zipfile\n"
        + "from pathlib import Path\n"
        + "from vibecad.engine.session import Session\n"
        + f"root = Path({str(tmp_path)!r})\n"
        + "os.chmod(root, 0o700)\n"
        + "source = root / 'source.bin'\n"
        + "payload = b'vibecad included asset bytes\\x00v1'\n"
        + "source.write_bytes(payload)\n"
        + "digest = hashlib.sha256(payload).hexdigest()\n"
        + "alias = digest + '.bin'\n"
        + "session = Session(checkpoint_dir=root, document_asset_root=root)\n"
        + "loaded = None\n"
        + "try:\n"
        + "    session.open_document('IncludedAsset')\n"
        + "    transient = Path(session.doc.TransientDir)\n"
        + "    assert transient.parent == root and transient.is_dir()\n"
        + "    assert oct(transient.stat().st_mode & 0o777) == '0o700'\n"
        + "    with session._transaction('include exact asset'):\n"
        + "        item = session.doc.addObject('App::DocumentObjectFileIncluded', 'Asset')\n"
        + "        item.File = (str(source), alias)\n"
        + "        session.doc.recompute()\n"
        + "    retained = Path(item.File)\n"
        + "    assert retained.parent == transient and retained.name == alias\n"
        + "    assert hashlib.sha256(retained.read_bytes()).hexdigest() == digest\n"
        + "    source.unlink()\n"
        + "    before_abort = tuple(sorted(path.name for path in transient.iterdir()))\n"
        + "    failed = root / 'failed.bin'\n"
        + "    failed.write_bytes(b'rollback me')\n"
        + "    try:\n"
        + "        with session._transaction('abort included asset'):\n"
        + "            doomed = session.doc.addObject(\n"
        + "                'App::DocumentObjectFileIncluded', 'Doomed'\n"
        + "            )\n"
        + "            failed_digest = hashlib.sha256(failed.read_bytes()).hexdigest()\n"
        + "            doomed.File = (str(failed), failed_digest + '.bin')\n"
        + "            raise ValueError('late fault')\n"
        + "    except ValueError:\n"
        + "        pass\n"
        + "    else:\n"
        + "        raise AssertionError('late fault was swallowed')\n"
        + "    assert session.doc.getObject('Doomed') is None\n"
        + "    assert tuple(sorted(path.name for path in transient.iterdir())) == before_abort\n"
        + "    checkpoint = session._checkpoint()\n"
        + "    with zipfile.ZipFile(checkpoint) as archive:\n"
        + "        assert hashlib.sha256(archive.read(alias)).hexdigest() == digest\n"
        + "    old = session.doc\n"
        + "    bad = root / 'bad.FCStd'\n"
        + "    bad.write_bytes(b'not an FCStd')\n"
        + "    children_before = tuple(\n"
        + "        sorted(path.name for path in root.iterdir() if path.is_dir())\n"
        + "    )\n"
        + "    try:\n"
        + "        session.load_document(bad)\n"
        + "    except Exception:\n"
        + "        pass\n"
        + "    else:\n"
        + "        raise AssertionError('invalid candidate loaded')\n"
        + "    assert session.doc is old\n"
        + "    assert tuple(\n"
        + "        sorted(path.name for path in root.iterdir() if path.is_dir())\n"
        + "    ) == children_before\n"
        + "    session.close_document()\n"
        + "    assert not transient.exists()\n"
        + "    loaded = Session(checkpoint_dir=root, document_asset_root=root)\n"
        + "    loaded.load_document(checkpoint)\n"
        + "    extracted = Path(loaded.doc.getObject('Asset').File)\n"
        + "    assert extracted.parent == Path(loaded.doc.TransientDir)\n"
        + "    assert extracted.name == alias and extracted.is_file()\n"
        + "    assert hashlib.sha256(extracted.read_bytes()).hexdigest() == digest\n"
        + "    second = loaded._checkpoint()\n"
        + "    with zipfile.ZipFile(second) as archive:\n"
        + "        assert hashlib.sha256(archive.read(alias)).hexdigest() == digest\n"
        + "    loaded_transient = Path(loaded.doc.TransientDir)\n"
        + "    loaded.close_document()\n"
        + "    loaded = None\n"
        + "    assert not loaded_transient.exists()\n"
        + "    print('DOCUMENT_ASSET_WORKSPACE_OK')\n"
        + "finally:\n"
        + "    if loaded is not None and loaded.doc is not None:\n"
        + "        loaded.close_document()\n"
        + "    if session.doc is not None:\n"
        + "        session.close_document()\n"
    )
    result = subprocess.run(
        [existing_managed_runtime_python, "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert "DOCUMENT_ASSET_WORKSPACE_OK" in result.stdout


def test_workspace_errors_bound_host_details() -> None:
    error = DocumentAssetWorkspaceError(
        DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED,
        "/" + "x" * 1000,
    )
    assert error.path == "/"
    assert "x" not in str(error)
