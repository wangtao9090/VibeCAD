"""Fail-closed runtime removal that never deletes durable VibeCAD data."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import os
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from vibecad.runtime import paths, status

_MARKER_NAME = ".uninstall_requested"
_PARK_SUFFIX = ".vibecad-removing"
_DELETE_SUFFIX = ".vibecad-deleting"

_DIR_FD_SUPPORTED = os.name == "posix" and all(
    operation in getattr(os, "supports_dir_fd", ())
    for operation in (os.open, os.stat, os.rename, os.unlink, os.mkdir, os.rmdir)
)


@dataclass(frozen=True)
class _Target:
    path: Path
    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class _Plan:
    targets: tuple[_Target, ...]
    preserved: tuple[Path, ...]


@dataclass
class _PinnedParent:
    path: Path
    fds: tuple[int, ...]
    bindings: tuple[tuple[int, str, tuple[int, int]], ...]

    @property
    def fd(self) -> int | None:
        return self.fds[-1] if self.fds else None

    def validate(self) -> None:
        if self.fd is None:
            return
        for parent_fd, name, identity in self.bindings:
            value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(value.st_mode) or (value.st_dev, value.st_ino) != identity:
                raise ValueError("运行时目标父目录 identity 已变化，拒绝继续")

    def sync(self) -> None:
        if self.fd is not None:
            os.fsync(self.fd)
            self.validate()
            return
        with contextlib.suppress(OSError):
            fd = os.open(self.path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def close(self) -> None:
        for fd in reversed(self.fds):
            with contextlib.suppress(OSError):
                os.close(fd)


_REMOVAL_RECORD_KEYS = {"schema", "path", "device", "inode", "mode"}


def uninstall_marker() -> Path:
    return paths.vibecad_home() / _MARKER_NAME


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _paths_overlap(left: Path, right: Path) -> bool:
    return _is_relative_to(left, right) or _is_relative_to(right, left)


def _protected_external_prefixes() -> tuple[Path, ...]:
    protected: list[Path] = []
    if override := paths.user_override_env():
        try:
            protected.append(override.expanduser().resolve(strict=True))
        except OSError as exc:
            raise ValueError("无法验证 external override，拒绝删除运行时") from exc
    if bound := paths.bound_external_prefix():
        try:
            protected.append(bound.resolve(strict=True))
        except OSError as exc:
            raise ValueError("无法验证 external runtime binding，拒绝删除运行时") from exc
    return tuple(dict.fromkeys(protected))


def _safe_home() -> tuple[Path, Path]:
    home = paths.vibecad_home().expanduser()
    if not home.is_absolute():
        raise ValueError(f"拒绝使用非绝对 VIBECAD_HOME：{home}")
    if home.is_symlink():
        raise ValueError(f"拒绝使用符号链接 VIBECAD_HOME：{home}")
    resolved = home.resolve(strict=False)
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise ValueError(f"拒绝删除危险路径下的运行时：{resolved}")
    if len(resolved.parts) < 3:
        raise ValueError(f"拒绝删除过浅路径下的运行时：{resolved}")
    if home.exists() and home.resolve(strict=True) != resolved:
        raise ValueError(f"VIBECAD_HOME identity 不稳定：{home}")
    return home, resolved


def _target(path: Path, *, home_resolved: Path) -> _Target:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"拒绝删除符号链接运行时目标：{path}")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise ValueError(f"运行时目标包含符号链接祖先，拒绝删除：{path}")
    if not _is_relative_to(resolved, home_resolved):
        raise ValueError(f"运行时目标越过 VIBECAD_HOME：{path}")
    if hasattr(info, "st_uid") and hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError(f"运行时目标不属于当前用户：{path}")
    return _Target(path=path, device=info.st_dev, inode=info.st_ino, mode=info.st_mode)


def _durable_data_root(data: Path, *, home_resolved: Path) -> Path | None:
    """Return one trusted durable root, rejecting aliases before planning deletes."""
    if not os.path.lexists(data):
        return None
    try:
        info = data.lstat()
        resolved = data.resolve(strict=True)
    except OSError as exc:
        raise ValueError("无法验证 durable data identity，拒绝删除") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("data 必须是独立的真实目录，拒绝删除运行时")
    if resolved != data or not _is_relative_to(resolved, home_resolved):
        raise ValueError("data 包含符号链接祖先或越过 VIBECAD_HOME，拒绝删除")
    if hasattr(info, "st_uid") and hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("data 不属于当前用户，拒绝删除运行时")
    return resolved


def _assert_disjoint_from_data(target: _Target, data_resolved: Path | None) -> None:
    if data_resolved is None:
        return
    target_resolved = target.path.resolve(strict=True)
    if _paths_overlap(target_resolved, data_resolved):
        raise ValueError(f"运行时目标与 durable data 重叠，拒绝删除：{target.path}")


def _existing_top_level(home: Path) -> list[Path]:
    try:
        return list(home.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ValueError(f"无法枚举 VIBECAD_HOME：{home}（{exc}）") from exc


def _build_plan() -> _Plan:
    home, home_resolved = _safe_home()
    if not home.exists():
        return _Plan((), ())

    runtime = paths.runtime_root()
    data = paths.data_root()
    targets: list[_Target] = []
    authorized_paths: set[Path] = set()
    protected_external = _protected_external_prefixes()
    data_resolved = _durable_data_root(data, home_resolved=home_resolved)

    def authorize(path: Path) -> _Target:
        candidate = _target(path, home_resolved=home_resolved)
        _assert_disjoint_from_data(candidate, data_resolved)
        targets.append(candidate)
        authorized_paths.add(path)
        return candidate

    if os.path.lexists(runtime):
        runtime_target = _target(runtime, home_resolved=home_resolved)
        if not stat.S_ISDIR(runtime_target.mode):
            raise ValueError(f"runtime 固定目标不是目录，拒绝删除：{runtime}")
        runtime_resolved = runtime.resolve(strict=True)
        _assert_disjoint_from_data(runtime_target, data_resolved)
        if any(_paths_overlap(runtime_resolved, prefix) for prefix in protected_external):
            raise ValueError("external runtime 与 replaceable runtime 重叠，拒绝删除")
        targets.append(runtime_target)
        authorized_paths.add(runtime)

    legacy = paths.legacy_env_prefix()
    managed_legacy = False
    protected_legacy = False
    if os.path.lexists(legacy):
        if legacy.is_symlink():
            managed_legacy = False
        else:
            managed_legacy = status.managed_legacy_receipt(legacy) is not None
            legacy_resolved = legacy.resolve(strict=True)
            protected_legacy = any(
                _paths_overlap(legacy_resolved, prefix) for prefix in protected_external
            )
        if managed_legacy and not protected_legacy:
            authorize(legacy)

            legacy_mm = paths.legacy_micromamba_path()
            if os.path.lexists(legacy_mm):
                mm_target = _target(legacy_mm, home_resolved=home_resolved)
                if not stat.S_ISREG(mm_target.mode):
                    raise ValueError(f"legacy micromamba 不是普通文件：{legacy_mm}")
                _assert_disjoint_from_data(mm_target, data_resolved)
                targets.append(mm_target)
                authorized_paths.add(legacy_mm)

    preserved: list[Path] = []
    if os.path.lexists(legacy) and (not managed_legacy or protected_legacy):
        preserved.append(legacy)
    legacy_mm = paths.legacy_micromamba_path()
    if os.path.lexists(legacy_mm) and not managed_legacy:
        preserved.append(legacy_mm)

    fixed_known = {
        runtime,
        data,
        home / "views",
        uninstall_marker(),
        paths.maintenance_lock(),
        paths.removal_record(),
        home / "mamba",
        home / "bin",
        home / "status.json",
        home / "install.log",
        home / ".install.lock",
    }
    for entry in _existing_top_level(home):
        if entry not in fixed_known and entry not in authorized_paths:
            preserved.append(entry)
    for fixed in (home / "status.json", home / "install.log", home / ".install.lock"):
        if os.path.lexists(fixed) and fixed not in authorized_paths:
            preserved.append(fixed)

    # Children before parents; fixed targets never overlap except legacy parents,
    # which are intentionally not targets so unknown siblings survive.
    targets.sort(key=lambda item: (item.path == legacy, -len(item.path.parts)))
    return _Plan(tuple(targets), tuple(dict.fromkeys(preserved)))


def _target_unchanged(target: _Target) -> bool:
    return _path_matches_target(target.path, target)


def _path_matches_target(path: Path, target: _Target) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return (
        info.st_dev == target.device
        and info.st_ino == target.inode
        and stat.S_IFMT(info.st_mode) == stat.S_IFMT(target.mode)
        and not stat.S_ISLNK(info.st_mode)
    )


def _same_entry(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
    )


def _info_matches_target(info: os.stat_result, target: _Target) -> bool:
    return (
        info.st_dev == target.device
        and info.st_ino == target.inode
        and stat.S_IFMT(info.st_mode) == stat.S_IFMT(target.mode)
        and not stat.S_ISLNK(info.st_mode)
    )


@contextlib.contextmanager
def _open_pinned_parent(path: Path):
    """Pin every existing parent component, or use a Windows-safe fallback."""
    if not _DIR_FD_SUPPORTED:
        yield _PinnedParent(path, (), ())
        return
    if not path.is_absolute() or path == Path(path.anchor):
        raise ValueError(f"运行时目标父目录无效：{path}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fds: list[int] = []
    bindings: list[tuple[int, str, tuple[int, int]]] = []
    try:
        fds.append(os.open(path.anchor, flags))
        for part in path.parts[1:]:
            parent_fd = fds[-1]
            child_fd = os.open(part, flags, dir_fd=parent_fd)
            child = os.fstat(child_fd)
            entry = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(child.st_mode)
                or not stat.S_ISDIR(entry.st_mode)
                or not _same_entry(child, entry)
            ):
                os.close(child_fd)
                raise ValueError("运行时目标父目录不安全，拒绝继续")
            bindings.append((parent_fd, part, (child.st_dev, child.st_ino)))
            fds.append(child_fd)
        final = os.fstat(fds[-1])
        getuid = getattr(os, "geteuid", None)
        if not stat.S_ISDIR(final.st_mode) or (
            hasattr(final, "st_uid") and getuid is not None and final.st_uid != getuid()
        ):
            raise ValueError("运行时目标父目录不属于当前用户，拒绝继续")
        pinned = _PinnedParent(path, tuple(fds), tuple(bindings))
        pinned.validate()
    except (OSError, ValueError) as exc:
        for fd in reversed(fds):
            with contextlib.suppress(OSError):
                os.close(fd)
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"无法 pin 运行时目标父目录：{path}") from exc
    try:
        yield pinned
    finally:
        for fd in reversed(fds):
            with contextlib.suppress(OSError):
                os.close(fd)


def _entry_stat(parent: _PinnedParent, name: str) -> os.stat_result | None:
    try:
        if parent.fd is None:
            return (parent.path / name).lstat()
        return os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _rename_entry(parent: _PinnedParent, source: str, destination: str) -> None:
    if parent.fd is None:
        os.rename(parent.path / source, parent.path / destination)
        return
    os.rename(source, destination, src_dir_fd=parent.fd, dst_dir_fd=parent.fd)


def _rename_no_replace(parent: _PinnedParent, source: str, destination: str) -> None:
    """Atomically move one sibling only while the destination is absent."""

    if os.name == "nt":
        # MoveFileEx without replace semantics is exposed by os.rename on Windows.
        _rename_entry(parent, source, destination)
        return

    directory_fd = parent.fd if parent.fd is not None else -100  # AT_FDCWD
    if parent.fd is None:
        source_raw = os.fsencode(parent.path / source)
        destination_raw = os.fsencode(parent.path / destination)
    else:
        source_raw = os.fsencode(source)
        destination_raw = os.fsencode(destination)
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        operation = getattr(library, "renameatx_np", None)
        flag = 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        operation = getattr(library, "renameat2", None)
        flag = 0x00000001  # RENAME_NOREPLACE
    else:
        operation = None
        flag = 0
    if operation is None:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    operation.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    operation.restype = ctypes.c_int
    if operation(directory_fd, source_raw, directory_fd, destination_raw, flag) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _restore_moved_entry(
    parent: _PinnedParent,
    source: str,
    destination: str,
    expected: os.stat_result,
) -> bool:
    """Restore exactly one moved entry without overwriting a published name."""
    current = _entry_stat(parent, source)
    if current is None or not _same_entry(current, expected):
        return False
    if _entry_stat(parent, destination) is not None:
        return False
    try:
        current = _entry_stat(parent, source)
        if current is None or not _same_entry(current, expected):
            return False
        _rename_no_replace(parent, source, destination)
        parent.sync()
    except (FileExistsError, FileNotFoundError, OSError):
        return False
    restored = _entry_stat(parent, destination)
    return restored is not None and _same_entry(restored, expected)


def _parked_path(path: Path) -> Path:
    return path.with_name(f".{path.name}{_PARK_SUFFIX}")


def _deleting_path(path: Path) -> Path:
    return path.with_name(f".{path.name}{_DELETE_SUFFIX}")


def _known_target_paths() -> tuple[Path, ...]:
    home = paths.vibecad_home().expanduser()
    return (
        paths.runtime_root(),
        paths.legacy_env_prefix(),
        paths.legacy_micromamba_path(),
        home / "status.json",
        home / "install.log",
        home / ".install.lock",
    )


def _known_parked_paths() -> tuple[Path, ...]:
    found: list[Path] = []
    for candidate in _known_target_paths():
        for transient in (_parked_path(candidate), _deleting_path(candidate)):
            if os.path.lexists(transient):
                found.append(transient)
    return tuple(found)


def _record_payload(target: _Target) -> dict:
    return {
        "schema": 1,
        "path": str(target.path),
        "device": target.device,
        "inode": target.inode,
        "mode": target.mode,
    }


def _write_removal_record(target: _Target) -> None:
    record = paths.removal_record()
    if os.path.lexists(record):
        raise ValueError(f"发现未完成的运行时卸载记录：{record}")
    raw = json.dumps(_record_payload(target), sort_keys=True).encode("utf-8")
    tmp = record.with_name(f"{record.name}.{os.getpid()}.{time.time_ns()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp, flags, 0o600)
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                raise OSError("卸载记录发生 short write")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, record)
        # Directory fsync is available on POSIX; Windows may reject directory
        # handles, where atomic replace still provides the supported guarantee.
        with contextlib.suppress(OSError):
            parent_fd = os.open(record.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _read_removal_record() -> _Target | None:
    record = paths.removal_record()
    try:
        info = record.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
        raise ValueError(f"运行时卸载记录不安全：{record}")
    try:
        raw = record.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"运行时卸载记录损坏：{record}") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != _REMOVAL_RECORD_KEYS
        or raw != json.dumps(payload, sort_keys=True)
        or type(payload.get("schema")) is not int
        or payload.get("schema") != 1
    ):
        raise ValueError(f"运行时卸载记录损坏：{record}")
    path_raw = payload.get("path")
    device = payload.get("device")
    inode = payload.get("inode")
    mode = payload.get("mode")
    if (
        not isinstance(path_raw, str)
        or type(device) is not int
        or type(inode) is not int
        or type(mode) is not int
    ):
        raise ValueError(f"运行时卸载记录字段无效：{record}")
    target = _Target(Path(path_raw), device, inode, mode)
    if target.path not in _known_target_paths():
        raise ValueError(f"运行时卸载记录目标越界：{target.path}")
    return target


def _clear_removal_record() -> None:
    record = paths.removal_record()
    record.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        parent_fd = os.open(record.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)


def _clear_directory_fd(directory_fd: int) -> None:
    """Clear one already-pinned directory without following child links."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    for name in os.listdir(directory_fd):
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode) and not stat.S_ISLNK(before.st_mode):
            child_fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                live = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not _same_entry(before, opened) or not _same_entry(opened, live):
                    raise ValueError("卸载目录 child identity 已变化，拒绝继续")
                _clear_directory_fd(child_fd)
                live = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not _same_entry(opened, live):
                    raise ValueError("卸载目录 child identity 已变化，拒绝继续")
                os.rmdir(name, dir_fd=directory_fd)
            finally:
                os.close(child_fd)
            continue
        live = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not _same_entry(before, live):
            raise ValueError("卸载目录 child identity 已变化，拒绝继续")
        os.unlink(name, dir_fd=directory_fd)


def _delete_private_target(parent: _PinnedParent, name: str, target: _Target) -> None:
    """Delete only the recorded entry below an already-pinned parent."""
    before = _entry_stat(parent, name)
    if before is None or not _info_matches_target(before, target):
        raise ValueError(f"卸载暂存 identity 与记录不匹配：{parent.path / name}")
    if parent.fd is None:
        candidate = parent.path / name
        if stat.S_ISDIR(target.mode):
            shutil.rmtree(candidate)
        else:
            candidate.unlink()
        parent.sync()
        return
    if stat.S_ISDIR(target.mode):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        target_fd = os.open(name, flags, dir_fd=parent.fd)
        try:
            opened = os.fstat(target_fd)
            live = _entry_stat(parent, name)
            if (
                live is None
                or not _info_matches_target(opened, target)
                or not _same_entry(opened, live)
            ):
                raise ValueError("卸载目录 identity 已变化，拒绝继续")
            _clear_directory_fd(target_fd)
            live = _entry_stat(parent, name)
            if live is None or not _same_entry(opened, live):
                raise ValueError("卸载目录 identity 已变化，拒绝继续")
            os.rmdir(name, dir_fd=parent.fd)
        finally:
            os.close(target_fd)
    else:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
        target_fd = os.open(name, flags, dir_fd=parent.fd)
        try:
            opened = os.fstat(target_fd)
            live = _entry_stat(parent, name)
            if (
                live is None
                or not _info_matches_target(opened, target)
                or not _same_entry(opened, live)
            ):
                raise ValueError("卸载文件 identity 已变化，拒绝继续")
            live = _entry_stat(parent, name)
            if live is None or not _info_matches_target(live, target):
                raise ValueError("卸载文件 identity 已变化，拒绝继续")
            os.unlink(name, dir_fd=parent.fd)
        finally:
            os.close(target_fd)
    parent.sync()


def _delete_parked_target(
    parked: Path, target: _Target, *, parent: _PinnedParent | None = None
) -> None:
    if parent is None:
        with _open_pinned_parent(parked.parent) as opened:
            _delete_parked_target(parked, target, parent=opened)
        return
    deleting = _deleting_path(target.path)
    if _entry_stat(parent, deleting.name) is not None:
        raise ValueError(f"发现未确认的历史卸载删除暂存：{deleting}")
    parked_info = _entry_stat(parent, parked.name)
    if parked_info is None or not _info_matches_target(parked_info, target):
        raise ValueError(f"卸载暂存 identity 与记录不匹配：{parked}")
    _rename_entry(parent, parked.name, deleting.name)
    parent.sync()
    moved = _entry_stat(parent, deleting.name)
    if moved is None:
        raise ValueError(f"卸载暂存移动失败：{parked}")
    if not _info_matches_target(moved, target):
        _restore_moved_entry(parent, deleting.name, parked.name, moved)
        raise ValueError(f"卸载暂存 identity 已变化，拒绝删除：{parked}")
    try:
        _delete_private_target(parent, deleting.name, target)
    except BaseException:
        restored = _entry_stat(parent, deleting.name)
        if restored is not None:
            _restore_moved_entry(parent, deleting.name, parked.name, restored)
        raise


def _recover_interrupted_removal() -> bool:
    """Resume one journaled park; return true when a newer fixed target survived."""
    target = _read_removal_record()
    parked_paths = _known_parked_paths()
    if target is None:
        if parked_paths:
            raise ValueError("发现无 identity 记录的历史卸载暂存，拒绝继续")
        return False
    parked = _parked_path(target.path)
    deleting = _deleting_path(target.path)
    expected_transients = {parked, deleting}
    unexpected = tuple(path for path in parked_paths if path not in expected_transients)
    if unexpected:
        raise ValueError("发现与 identity 记录无关的历史卸载暂存，拒绝继续")
    with _open_pinned_parent(target.path.parent) as parent:
        fixed = _entry_stat(parent, target.path.name)
        fixed_matches = fixed is not None and _info_matches_target(fixed, target)
        parked_info = _entry_stat(parent, parked.name)
        deleting_info = _entry_stat(parent, deleting.name)
        if parked_info is not None and deleting_info is not None:
            raise ValueError("发现多个卸载暂存 generation，拒绝继续")
        if deleting_info is not None:
            if not _info_matches_target(deleting_info, target):
                if _entry_stat(parent, parked.name) is None:
                    _restore_moved_entry(parent, deleting.name, parked.name, deleting_info)
                raise ValueError("卸载删除暂存 identity 与记录不匹配")
            _delete_private_target(parent, deleting.name, target)
            replacement_survived = fixed is not None and not fixed_matches
            _clear_removal_record()
            return replacement_survived
        if parked_info is not None:
            if not _info_matches_target(parked_info, target):
                if fixed is None and _restore_moved_entry(
                    parent, parked.name, target.path.name, parked_info
                ):
                    _clear_removal_record()
                    return True
                raise ValueError("卸载暂存 identity 与记录不匹配")
            _delete_parked_target(parked, target, parent=parent)
            replacement_survived = fixed is not None and not fixed_matches
            _clear_removal_record()
            return replacement_survived
        if fixed_matches:
            # Crash occurred after journal publication but before the atomic park.
            _clear_removal_record()
            return False
        # The planned generation was already removed. Any current fixed target is new.
        _clear_removal_record()
        return fixed is not None


def _restore_parked_target(
    parked: Path, target: _Target, *, parent: _PinnedParent | None = None
) -> bool:
    """Best-effort rollback after a failed delete; never overwrite a new generation."""
    if parent is None:
        with _open_pinned_parent(target.path.parent) as opened:
            return _restore_parked_target(parked, target, parent=opened)
    parked_info = _entry_stat(parent, parked.name)
    if parked_info is None or not _info_matches_target(parked_info, target):
        return False
    return _restore_moved_entry(parent, parked.name, target.path.name, parked_info)


def _remove_target(target: _Target) -> None:
    parked = _parked_path(target.path)
    deleting = _deleting_path(target.path)
    with _open_pinned_parent(target.path.parent) as parent:
        current = _entry_stat(parent, target.path.name)
        if current is None:
            return
        if not _info_matches_target(current, target):
            raise ValueError(f"运行时目标 identity 已变化，拒绝继续：{target.path}")
        if _entry_stat(parent, parked.name) is not None:
            raise ValueError(f"发现未确认的历史卸载暂存，拒绝覆盖：{parked}")
        if _entry_stat(parent, deleting.name) is not None:
            raise ValueError(f"发现未确认的历史卸载删除暂存：{deleting}")
        _write_removal_record(target)
        try:
            _rename_entry(parent, target.path.name, parked.name)
            parent.sync()
        except FileNotFoundError:
            _clear_removal_record()
            return
        moved = _entry_stat(parent, parked.name)
        if moved is None:
            raise ValueError(f"运行时目标暂存失败：{target.path}")
        if not _info_matches_target(moved, target):
            if _restore_moved_entry(parent, parked.name, target.path.name, moved):
                _clear_removal_record()
            raise ValueError(f"运行时目标 identity 已变化，拒绝继续：{target.path}")
        try:
            _delete_parked_target(parked, target, parent=parent)
        except BaseException:
            if _restore_parked_target(parked, target, parent=parent):
                _clear_removal_record()
            raise
        _clear_removal_record()


def dir_size_mb(path: Path) -> float:
    """Size ordinary files below one authorized target without following links."""
    total = 0
    try:
        root_info = path.lstat()
    except OSError:
        return 0.0
    if stat.S_ISREG(root_info.st_mode):
        return root_info.st_size / 1e6
    if not stat.S_ISDIR(root_info.st_mode):
        return 0.0
    for current, dirs, files in os.walk(path, followlinks=False):
        current_path = Path(current)
        dirs[:] = [name for name in dirs if not (current_path / name).is_symlink()]
        for name in files:
            candidate = current_path / name
            try:
                info = candidate.lstat()
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
            except OSError:
                continue
    return total / 1e6


def preview_uninstall() -> dict:
    try:
        plan = _build_plan()
    except ValueError as exc:
        return {"ok": False, "data_preserved": True, "message": str(exc)}
    runtime = paths.runtime_root()
    target_paths = [str(target.path) for target in plan.targets]
    size = sum(dir_size_mb(target.path) for target in plan.targets)
    return {
        "ok": True,
        "confirm_required": True,
        "path": str(runtime),
        "paths": target_paths,
        "size_mb": round(size, 1),
        "preserved_paths": [str(path) for path in plan.preserved],
        "data_preserved": True,
    }


def _write_marker() -> None:
    marker = uninstall_marker()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(marker, flags, 0o600)
    except FileExistsError:
        try:
            info = marker.lstat()
        except FileNotFoundError:
            # A racing remover won; retry the exclusive create once.
            fd = os.open(marker, flags, 0o600)
        else:
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ValueError(f"卸载 marker 不是普通文件，拒绝使用：{marker}")
            return
    os.close(fd)


def _read_marker_identity(marker: Path) -> tuple[int, int] | None:
    """Return one valid uninstall authorization identity without opening its bytes."""

    try:
        info = marker.lstat()
    except FileNotFoundError:
        return None
    getuid = getattr(os, "getuid", None)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (hasattr(info, "st_uid") and getuid is not None and info.st_uid != getuid())
    ):
        raise ValueError(f"卸载 marker 不是当前用户的单链接普通文件：{marker}")
    return info.st_dev, info.st_ino


def _clear_matching_marker(marker: Path, identity: tuple[int, int]) -> bool:
    if _read_marker_identity(marker) != identity:
        return False
    try:
        marker.unlink()
    except FileNotFoundError:
        return False
    return True


def _remove_home_if_empty() -> None:
    """Remove only an actually empty container after authorized cleanup."""
    home = paths.vibecad_home().expanduser()
    with contextlib.suppress(OSError):
        home.rmdir()


def _retire_kernel_before_runtime_removal() -> bool:
    """Retire the authenticated detached Kernel without ever spawning one."""

    try:
        from vibecad.daemon.bootstrap import retire_local_kernel

        return retire_local_kernel(
            reason="runtime_uninstall",
            _maintenance_held=True,
        )
    except BaseException:
        return False


def request_uninstall() -> dict:
    cleanup_home = False
    try:
        _safe_home()
        cleanup_home = True
        with status.runtime_maintenance_lock():
            # A hard-crashed direct uninstall has no pending marker. A fresh
            # public request is therefore also a supported recovery entrypoint.
            _recover_interrupted_removal()
            plan = _build_plan()
            if not plan.targets:
                return {
                    "ok": True,
                    "already_clean": True,
                    "data_preserved": True,
                    "preserved_paths": [str(path) for path in plan.preserved],
                    "message": "没有可授权删除的运行时；durable data 与未知内容均已保留",
                }
            _write_marker()
            return {
                "ok": True,
                "marked": True,
                "path": str(paths.runtime_root()),
                "paths": [str(target.path) for target in plan.targets],
                "preserved_paths": [str(path) for path in plan.preserved],
                "data_preserved": True,
            }
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "data_preserved": True, "message": str(exc)}
    finally:
        if cleanup_home:
            _remove_home_if_empty()


def perform_pending_uninstall() -> bool:
    """Delete authorized fixed targets; retain marker whenever convergence is uncertain."""
    cleanup_home = False
    marker = uninstall_marker()
    try:
        marker_identity = _read_marker_identity(marker)
    except (OSError, ValueError):
        return False
    if marker_identity is None:
        return False
    try:
        _safe_home()
        cleanup_home = True
        with status.runtime_maintenance_lock():
            if _read_marker_identity(marker) != marker_identity:
                return False
            if not _retire_kernel_before_runtime_removal():
                return False
            if _recover_interrupted_removal():
                # The identity-bound old generation is gone and a newer fixed
                # generation exists. Resolve only the old marker; never delete it.
                return _clear_matching_marker(marker, marker_identity)
            plan = _build_plan()
            for target in plan.targets:
                _remove_target(target)
            remaining = _build_plan()
            if remaining.targets or _known_parked_paths():
                return False
            return _clear_matching_marker(marker, marker_identity)
    except (OSError, RuntimeError, ValueError):
        return False
    finally:
        if cleanup_home:
            _remove_home_if_empty()


def uninstall_now() -> dict:
    """Direct CLI removal of authorized runtime targets only."""
    cleanup_home = False
    try:
        _safe_home()
        cleanup_home = True
        with status.runtime_maintenance_lock():
            if _recover_interrupted_removal():
                raise ValueError("旧卸载已恢复，但检测到新的运行时 generation；请重新确认")
            plan = _build_plan()
            if not plan.targets:
                return {
                    "ok": True,
                    "already_clean": True,
                    "freed_mb": 0,
                    "data_preserved": True,
                    "preserved_paths": [str(path) for path in plan.preserved],
                    "message": "没有可授权删除的运行时；durable data 与未知内容均已保留",
                }
            _write_marker()
            marker = uninstall_marker()
            marker_identity = _read_marker_identity(marker)
            if marker_identity is None:
                raise ValueError("无法固定卸载 marker identity，拒绝删除运行时")
            if not _retire_kernel_before_runtime_removal():
                return {
                    "ok": False,
                    "freed_mb": 0,
                    "data_preserved": True,
                    "message": "Task Kernel 未能安全退役；运行时和卸载标记均已保留",
                }
            size = sum(dir_size_mb(target.path) for target in plan.targets)
            try:
                for target in plan.targets:
                    _remove_target(target)
                remaining = _build_plan()
            except (OSError, ValueError) as exc:
                return {
                    "ok": False,
                    "freed_mb": 0,
                    "data_preserved": True,
                    "message": f"删除未完成；固定运行时目标保留待重试（{exc}）",
                }
            if remaining.targets or _known_parked_paths():
                return {
                    "ok": False,
                    "freed_mb": 0,
                    "data_preserved": True,
                    "message": "删除未完成；仍有授权运行时目标，下次重试",
                }
            if not _clear_matching_marker(marker, marker_identity):
                return {
                    "ok": False,
                    "freed_mb": 0,
                    "data_preserved": True,
                    "message": "运行时已清理，但卸载 marker identity 已变化；保留待恢复",
                }
            freed = f"{size / 1000:.1f} GB" if size >= 1000 else f"{size:.1f} MB"
            return {
                "ok": True,
                "freed_mb": round(size, 1),
                "path": str(paths.runtime_root()),
                "paths": [str(target.path) for target in plan.targets],
                "preserved_paths": [str(path) for path in remaining.preserved],
                "data_preserved": True,
                "message": f"已删除授权运行时（释放约 {freed}）；durable data 已保留",
            }
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "freed_mb": 0, "data_preserved": True, "message": str(exc)}
    finally:
        if cleanup_home:
            _remove_home_if_empty()
