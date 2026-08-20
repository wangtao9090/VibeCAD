"""Native Windows contracts for the interaction filesystem authority."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from vibecad import _file_compat
from vibecad.interaction import storage as storage_module
from vibecad.interaction.storage import SafeRoot, StorageFailure

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Win32 contract")


def _root(path: Path) -> SafeRoot:
    path.mkdir()
    _file_compat.set_private_dacl(path)
    return SafeRoot(path.resolve())


def test_safe_root_atomic_read_hash_and_exact_entry_validation(tmp_path: Path) -> None:
    root = _root(tmp_path / "private")
    root_fd = root.open()
    try:
        root.atomic_write(root_fd, "record.json", b"first", token="a" * 32)
        raw, first = root.read_file_at(root_fd, "record.json", maximum=16)
        digest, size, hashed = root.hash_open_file(root_fd, "record.json", maximum=16)
        assert raw == b"first"
        assert digest == "a7937b64b8caa58f03721bb6bacf5c78cb235febe0e70b1b84cd99541461a08e"
        assert size == 5
        assert (hashed.st_dev, hashed.st_ino) == (first.st_dev, first.st_ino)

        root.atomic_write(root_fd, "record.json", b"second", token="b" * 32)
        raw, second = root.read_file_at(root_fd, "record.json", maximum=16)
        assert raw == b"second"
        assert (second.st_dev, second.st_ino) != (first.st_dev, first.st_ino)
    finally:
        os.close(root_fd)


def test_dirfd_adapter_publishes_and_deletes_exact_private_tree(tmp_path: Path) -> None:
    root = _root(tmp_path / "private")
    root_fd = root.open()
    try:
        storage_module.os.mkdir(".checkout.tmp", 0o700, dir_fd=root_fd)
        staging_fd, _ = root.open_directory_at(root_fd, ".checkout.tmp")
        try:
            payload_fd = storage_module.os.open(
                "model.FCStd",
                storage_module.os.O_WRONLY | storage_module.os.O_CREAT | storage_module.os.O_EXCL,
                0o600,
                dir_fd=staging_fd,
            )
            try:
                os.write(payload_fd, b"model")
                os.fsync(payload_fd)
            finally:
                os.close(payload_fd)
            storage_module.os.fsync(staging_fd)
        finally:
            os.close(staging_fd)

        storage_module.os.rename(
            ".checkout.tmp",
            "checkout",
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
        assert storage_module.os.listdir(root_fd) == ["checkout"]
        published_fd, _ = root.open_directory_at(root_fd, "checkout")
        try:
            assert storage_module.os.listdir(published_fd) == ["model.FCStd"]
            storage_module.os.unlink("model.FCStd", dir_fd=published_fd)
        finally:
            os.close(published_fd)
        storage_module.os.rmdir("checkout", dir_fd=root_fd)
        assert storage_module.os.listdir(root_fd) == []
    finally:
        os.close(root_fd)


def test_safe_root_rejects_unprotected_child_without_rewriting_it(tmp_path: Path) -> None:
    root = _root(tmp_path / "private")
    child = Path(root.path) / "untrusted.json"
    child.write_bytes(b"{}")
    before = _file_compat._windows_security(child)
    root_fd = root.open()
    try:
        with pytest.raises(StorageFailure, match="storage file is unsafe"):
            root.read_file_at(root_fd, child.name, maximum=16)
    finally:
        os.close(root_fd)
    assert _file_compat._windows_security(child) == before
