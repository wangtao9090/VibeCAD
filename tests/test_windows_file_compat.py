"""Native Windows capability, locking and exact-mutation contracts."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from vibecad import _file_compat

pytestmark = [
    pytest.mark.windows_contract,
    pytest.mark.skipif(sys.platform != "win32", reason="native Win32 contract"),
]


def _private_directory(path: Path) -> _file_compat.WindowsPathCapability:
    path.mkdir()
    _file_compat.set_private_dacl(path)
    owner, sddl = _file_compat._windows_security(path)
    try:
        return _file_compat.capture_windows_path(path, directory=True)
    except OSError as exc:
        pytest.fail(f"private DACL round-trip failed: {exc}; owner={owner}; sddl={sddl}")


def test_capability_mapping_uses_canonical_fixed_width_hex(tmp_path: Path) -> None:
    root = _private_directory(tmp_path / "private")

    mapping = root.to_mapping()

    assert mapping["volume"] == f"{root.volume:016x}"
    assert mapping["file_id"] == f"{root.file_id:032x}"
    assert _file_compat.WindowsPathCapability.from_mapping(mapping) == root
    for field, invalid in (("volume", "0"), ("file_id", "A" * 32)):
        malformed = dict(mapping)
        malformed[field] = invalid
        with pytest.raises(ValueError, match="invalid Windows path capability"):
            _file_compat.WindowsPathCapability.from_mapping(malformed)


def test_only_the_tokens_administrators_default_owner_is_trusted(monkeypatch) -> None:
    user = _file_compat.current_user_sid()
    administrators = "S-1-5-32-544"
    protected = f"O:{administrators}D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;{user})"

    monkeypatch.setattr(
        _file_compat,
        "_current_default_owner_sid",
        lambda: administrators,
    )
    _file_compat._validate_windows_security(administrators, protected)

    local_administrator_dacl = f"O:{administrators}D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;LA)"
    monkeypatch.setattr(_file_compat, "_sid_is_well_known", lambda sid, kind: True)
    _file_compat._validate_windows_security(administrators, local_administrator_dacl)

    monkeypatch.setattr(_file_compat, "_sid_is_well_known", lambda sid, kind: False)
    with pytest.raises(OSError, match="grants foreign access"):
        _file_compat._validate_windows_security(
            administrators,
            local_administrator_dacl,
        )

    monkeypatch.setattr(
        _file_compat,
        "_current_default_owner_sid",
        lambda: "S-1-5-32-545",
    )
    with pytest.raises(OSError, match="DACL is not protected"):
        _file_compat._validate_windows_security(administrators, protected)


def test_read_only_check_and_binary_positional_read_use_real_handle_access(
    tmp_path: Path,
) -> None:
    root = _private_directory(tmp_path / "private")
    payload = b"prefix\x1asuffix"
    descriptor, _capability = _file_compat.open_private_file(
        Path(root.path) / "payload.bin",
        expected_parent=root,
        exclusive=True,
    )
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    readonly = os.open(Path(root.path) / "payload.bin", os.O_RDONLY)
    readwrite = os.open(Path(root.path) / "payload.bin", os.O_RDWR | os.O_BINARY)
    try:
        _file_compat.require_read_only(readonly)
        assert _file_compat.pread(readonly, len(payload), 0) == payload
        with pytest.raises(OSError, match="not read-only"):
            _file_compat.require_read_only(readwrite)
    finally:
        os.close(readwrite)
        os.close(readonly)


def test_private_creation_rejects_unknown_existing_winner_without_rewriting_dacl(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "inherited"
    existing.mkdir()
    before = _file_compat._windows_security(existing)

    with pytest.raises(OSError, match="DACL is not protected"):
        _file_compat.ensure_private_directory(existing)

    assert _file_compat._windows_security(existing) == before


def test_raw_directory_handle_pins_and_validates_exact_capability(tmp_path: Path) -> None:
    root = _private_directory(tmp_path / "private")
    handle = _file_compat.open_windows_directory_handle(Path(root.path))
    try:
        assert (
            _file_compat.validate_windows_handle_path(
                handle,
                Path(root.path),
                expected=root,
            )
            == root
        )
        _file_compat.set_windows_handle_inheritable(handle, True)
        _file_compat.set_windows_handle_inheritable(handle, False)
    finally:
        _file_compat.close_windows_handle(handle)


def test_lockfileex_contends_then_releases_across_distinct_handles(tmp_path: Path) -> None:
    root = _private_directory(tmp_path / "private")
    path = Path(root.path) / "lease.lock"
    first, capability = _file_compat.open_private_file(path, expected_parent=root)
    second, opened_again = _file_compat.open_private_file(path, expected_parent=root)
    assert opened_again.file_id == capability.file_id
    try:
        _file_compat.flock(first, _file_compat.LOCK_EX | _file_compat.LOCK_NB)
        with pytest.raises(BlockingIOError):
            _file_compat.flock(second, _file_compat.LOCK_EX | _file_compat.LOCK_NB)
        _file_compat.flock(first, _file_compat.LOCK_UN)
        _file_compat.flock(second, _file_compat.LOCK_EX | _file_compat.LOCK_NB)
        _file_compat.flock(second, _file_compat.LOCK_UN)
    finally:
        os.close(second)
        os.close(first)


def test_write_through_replace_and_handle_exact_delete_preserve_file_id(
    tmp_path: Path,
) -> None:
    root = _private_directory(tmp_path / "private")
    source = Path(root.path) / "source.json"
    destination = Path(root.path) / "record.json"
    source_fd, source_capability = _file_compat.open_private_file(
        source,
        expected_parent=root,
        exclusive=True,
    )
    destination_fd, destination_capability = _file_compat.open_private_file(
        destination,
        expected_parent=root,
        exclusive=True,
    )
    try:
        os.write(source_fd, b"new")
        os.write(destination_fd, b"old")
        os.fsync(source_fd)
        os.fsync(destination_fd)
    finally:
        os.close(destination_fd)
        os.close(source_fd)

    moved = _file_compat.replace_windows_file(
        source,
        destination,
        source_parent=root,
        expected_source=source_capability,
        expected_destination=destination_capability,
    )

    assert not source.exists()
    assert destination.read_bytes() == b"new"
    assert (moved.volume, moved.file_id) == (
        source_capability.volume,
        source_capability.file_id,
    )
    _file_compat.delete_windows_file(destination, parent=root, expected=moved)
    assert not destination.exists()


def test_write_through_directory_publish_and_exact_delete_preserve_file_id(
    tmp_path: Path,
) -> None:
    root = _private_directory(tmp_path / "private")
    source = Path(root.path) / "staging"
    destination = Path(root.path) / "published"
    source_capability = _file_compat.ensure_private_directory(
        source,
        expected_parent=root,
    )

    moved = _file_compat.rename_windows_directory(
        source,
        destination,
        source_parent=root,
        expected_source=source_capability,
    )

    assert not source.exists()
    assert destination.is_dir()
    assert (moved.volume, moved.file_id) == (
        source_capability.volume,
        source_capability.file_id,
    )
    _file_compat.delete_windows_directory(destination, parent=root, expected=moved)
    assert not destination.exists()
