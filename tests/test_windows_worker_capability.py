from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from vibecad import _file_compat
from vibecad._file_compat import WindowsPathCapability
from vibecad.worker import windows_files
from vibecad.worker.codec import WorkerWireErrorCode
from vibecad.worker.proxy import FreeCadWorker
from vibecad.worker.service import WorkerService, _candidate_entries, _ServiceError

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only capability tests")

_GENERATION = "worker_generation_" + "1" * 32


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _file_compat.set_private_dacl(path)


def _private_file(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    _file_compat.set_private_dacl(path)


def _candidate_directory(tmp_path: Path) -> tuple[Path, WindowsPathCapability]:
    root = tmp_path / "candidate"
    _private_directory(root)
    _private_file(root / "model.FCStd", b"fcstd")
    _private_file(root / "model.step", b"step")
    return root, _file_compat.capture_windows_path(root, directory=True)


def test_windows_worker_capability_hashes_only_the_captured_file(tmp_path: Path) -> None:
    root, capability = _candidate_directory(tmp_path)

    entries = dict(windows_files.capture_entries(capability, maximum_entries=4))
    digest, size, hashed = windows_files.hash_entry(
        capability,
        "model.FCStd",
        maximum_bytes=64,
        expected=entries["model.FCStd"],
    )

    assert size == 5
    assert len(digest) == 64
    assert hashed == entries["model.FCStd"]
    assert windows_files.validate_directory(capability) == root.resolve()


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("volume", 1),
        ("volume", "1"),
        ("volume", "A" * 16),
        ("volume", "0" * 17),
        ("file_id", 1),
        ("file_id", "1"),
        ("file_id", "A" * 32),
        ("file_id", "0" * 33),
    ),
)
def test_windows_capability_wire_rejects_noncanonical_wide_integers(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    _root, capability = _candidate_directory(tmp_path)
    mapping = capability.to_mapping()
    assert WindowsPathCapability.from_mapping(mapping) == capability
    assert type(mapping["volume"]) is str and len(mapping["volume"]) == 16
    assert type(mapping["file_id"]) is str and len(mapping["file_id"]) == 32
    mapping[field] = replacement

    with pytest.raises(ValueError, match="invalid Windows path capability"):
        WindowsPathCapability.from_mapping(mapping)


def test_windows_worker_capability_rejects_file_id_replacement(tmp_path: Path) -> None:
    root, capability = _candidate_directory(tmp_path)
    original = dict(windows_files.capture_entries(capability, maximum_entries=4))["model.FCStd"]
    replacement = root / "replacement.FCStd"
    _private_file(replacement, b"other")

    os.replace(replacement, root / "model.FCStd")

    with pytest.raises(OSError):
        windows_files.validate_entry(original)
    with pytest.raises(OSError):
        windows_files.hash_entry(
            capability,
            "model.FCStd",
            maximum_bytes=64,
            expected=original,
        )


def test_windows_worker_capability_rejects_dacl_downgrade(tmp_path: Path) -> None:
    root, capability = _candidate_directory(tmp_path)
    target = dict(windows_files.capture_entries(capability, maximum_entries=4))["model.FCStd"]

    changed = subprocess.run(
        ["icacls.exe", os.fspath(root / "model.FCStd"), "/inheritance:e"],
        check=False,
        capture_output=True,
    )
    if changed.returncode != 0:
        pytest.skip("icacls could not alter the test DACL")

    with pytest.raises(OSError):
        windows_files.validate_entry(target)


def test_windows_worker_capability_rejects_directory_reparse_point(tmp_path: Path) -> None:
    target = tmp_path / "target"
    link = tmp_path / "link"
    _private_directory(target)
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(OSError):
        _file_compat.capture_windows_path(Path(os.path.abspath(link)), directory=True)


def test_windows_worker_service_binds_mapping_without_scm_rights(tmp_path: Path) -> None:
    _root, capability = _candidate_directory(tmp_path)
    service = WorkerService(_GENERATION)
    candidate_id = "worker_candidate_" + "2" * 32

    result = service.dispatch(
        "candidate.bind",
        {
            "candidate_id": candidate_id,
            "project_id": "project_" + "3" * 32,
            "revision_id": "revision_" + "4" * 32,
            "base_revision_id": "revision_" + "5" * 32,
            "path_capability": capability.to_mapping(),
        },
        (),
    )

    assert result == {"candidate_id": candidate_id}
    candidate = service._candidates[candidate_id]  # noqa: SLF001
    assert candidate.directory_fd == -1
    assert candidate.directory_capability == capability
    assert _candidate_entries(candidate) == (
        candidate.model_identity,
        candidate.step_identity,
    )
    assert service.dispatch(
        "candidate.release",
        {"candidate_id": candidate_id},
        (),
    ) == {"candidate_id": candidate_id}


def test_windows_worker_service_rejects_tampered_wire_identity(tmp_path: Path) -> None:
    _root, capability = _candidate_directory(tmp_path)
    mapping = capability.to_mapping()
    mapping["file_id"] = f"{int(str(mapping['file_id']), 16) + 1:032x}"
    service = WorkerService(_GENERATION)

    with pytest.raises(_ServiceError) as caught:
        service.dispatch(
            "candidate.bind",
            {
                "candidate_id": "worker_candidate_" + "6" * 32,
                "project_id": "project_" + "7" * 32,
                "revision_id": "revision_" + "8" * 32,
                "base_revision_id": "revision_" + "9" * 32,
                "path_capability": mapping,
            },
            (),
        )

    assert caught.value.code is WorkerWireErrorCode.INTEGRITY_FAILURE


@pytest.mark.slow
def test_real_windows_worker_receives_path_capability_without_scm_rights(
    tmp_path: Path,
) -> None:
    python_raw = os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON")
    if not python_raw:
        pytest.skip("managed FreeCAD Python was not requested")
    _root, capability = _candidate_directory(tmp_path)
    source_root = Path(__file__).resolve().parents[1] / "src"
    worker = FreeCadWorker.start(
        python=Path(python_raw),
        source_root=source_root,
    )
    candidate_id = "worker_candidate_" + "a" * 32
    try:
        result = worker._process.request(  # noqa: SLF001
            "candidate.bind",
            {
                "candidate_id": candidate_id,
                "project_id": "project_" + "b" * 32,
                "revision_id": "revision_" + "c" * 32,
                "base_revision_id": "revision_" + "d" * 32,
                "path_capability": capability.to_mapping(),
            },
            timeout_ms=30_000,
        )
        assert result == {"candidate_id": candidate_id}
        assert worker._process.request(  # noqa: SLF001
            "candidate.release",
            {"candidate_id": candidate_id},
            timeout_ms=5_000,
        ) == {"candidate_id": candidate_id}
    finally:
        worker.terminate()
