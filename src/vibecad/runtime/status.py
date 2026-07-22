"""运行时状态机、哨兵就绪探测、健康检查、跨进程文件锁。纯 stdlib。"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vibecad.runtime import paths, spec

# import FreeCAD 前的跨平台兜底（Windows 把 conda Library/bin 注入 PATH；macOS/Linux 无操作）
_PREP = (
    "import os,sys\n"
    "if sys.platform=='win32':\n"
    "    _b=os.path.join(sys.prefix,'Library','bin')\n"
    "    os.environ['PATH']=_b+os.pathsep+os.environ.get('PATH','')\n"
    "    try:\n"
    "        os.add_dll_directory(_b)\n"
    "    except Exception:\n"
    "        pass\n"
    "    _mods=[_b, os.path.join(sys.prefix,'Library','lib')]\n"
    "else:\n"
    "    _mods=[os.path.join(sys.prefix,'lib')]\n"
    "for _m in _mods:\n"
    "    if _m not in sys.path:\n"
    "        sys.path.insert(0, _m)\n"
)
_HEALTH_SNIPPET = _PREP + "import FreeCAD, Part\n"
# 托管 env 复用前必须精确匹配 pins；不能把“可 import 的其他 FreeCAD/Python”盖章成
# 当前 receipt，否则后续启动会跳过真正的引擎升级。
_ENGINE_SNIPPET = (
    _PREP
    + "import FreeCAD, Part, sys\n"
    + f"if sys.version_info[:2] != {spec.PYTHON_VERSION!r}:\n"
    + "    raise RuntimeError('managed runtime Python version mismatch')\n"
    + f"if tuple(map(int, FreeCAD.Version()[:3])) != {spec.FREECAD_VERSION!r}:\n"
    + "    raise RuntimeError('managed runtime FreeCAD version mismatch')\n"
)
# 更严就绪校验：精确引擎、server package epoch、MCP SDK 与完整公共 surface。
_SERVER_SNIPPET = (
    "import hashlib, importlib.metadata as _metadata, json\n"
    "import mcp, vibecad, vibecad.server\n"
    "from collections.abc import Mapping as _SurfaceMapping\n"
    "from vibecad.application.public_surface import public_tool_specs as _public_tool_specs\n"
    "from vibecad.runtime import spec as _installed_runtime_spec\n"
    "def _surface_thaw(_value):\n"
    "    if _value is None or type(_value) in {str, int, float, bool}:\n"
    "        return _value\n"
    "    if type(_value) in {tuple, list}:\n"
    "        return [_surface_thaw(_item) for _item in _value]\n"
    "    if isinstance(_value, _SurfaceMapping):\n"
    "        return {_key: _surface_thaw(_value[_key]) for _key in sorted(_value)}\n"
    "    raise RuntimeError('unsupported public surface value')\n"
    "_surface_projection = [\n"
    "    {\n"
    "        'name': _item.name,\n"
    "        'inputSchema': _surface_thaw(_item.input_schema),\n"
    "        'outputSchema': _surface_thaw(_item.output_schema),\n"
    "        'annotations': {\n"
    "            'readOnlyHint': _item.annotations.read_only,\n"
    "            'destructiveHint': _item.annotations.destructive,\n"
    "            'idempotentHint': _item.annotations.idempotent,\n"
    "            'openWorldHint': _item.annotations.open_world,\n"
    "        },\n"
    "    }\n"
    "    for _item in _public_tool_specs()\n"
    "]\n"
    "_surface_raw = json.dumps(\n"
    "    _surface_projection, ensure_ascii=False, allow_nan=False,\n"
    "    separators=(',', ':'), sort_keys=True,\n"
    ").encode('utf-8')\n"
    "_surface_digest = hashlib.sha256(_surface_raw).hexdigest()\n"
    f"if vibecad.__version__ != {spec.VIBECAD_VERSION!r}:\n"
    "    raise RuntimeError('vibecad runtime version mismatch: ' + vibecad.__version__)\n"
    f"if _installed_runtime_spec.SERVER_PACKAGE_EPOCH != {spec.SERVER_PACKAGE_EPOCH!r}:\n"
    "    raise RuntimeError('vibecad server package epoch mismatch')\n"
    f"if _metadata.version('mcp') != {spec.MCP_VERSION!r}:\n"
    "    raise RuntimeError('mcp SDK version mismatch')\n"
    f"if _surface_digest != {spec.PUBLIC_SURFACE_SHA256!r}:\n"
    "    raise RuntimeError('vibecad public surface mismatch')\n"
)
_VERIFY_SNIPPET = _ENGINE_SNIPPET + _SERVER_SNIPPET
_STALE_SECONDS = 3600
_MAX_RECEIPT_BYTES = 4096
_MAX_LOG_APPEND_BYTES = 64 * 1024
_BOUNDED_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRE_EPOCH_MANAGED_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "runtime_kind",
        "vibecad_version",
        "python_pin",
        "freecad_pin",
    }
)
_CURRENT_MANAGED_RECEIPT_KEYS = frozenset(spec.expected_receipt())
_EXTERNAL_RECEIPT_KEYS = {
    "schema",
    "runtime_kind",
    "vibecad_version",
    "server_package_epoch",
    "mcp_version",
    "public_surface_sha256",
    "prefix",
    "prefix_device",
    "prefix_inode",
    "python_version",
    "freecad_version",
}


class Phase(StrEnum):
    NOT_STARTED = "not_started"
    DOWNLOADING_MICROMAMBA = "downloading_micromamba"
    CREATING_ENV = "creating_env"
    INSTALLING_PIP = "installing_pip"
    VERIFYING = "verifying"
    READY = "ready"
    FAILED = "failed"


class ReceiptState(StrEnum):
    """廉价 receipt 分类；installer 据此区分 pip-only 同步与完整建 env。"""

    MISSING = "missing"
    LEGACY = "legacy"
    CURRENT = "current"
    SERVER_MISMATCH = "server_mismatch"
    INCOMPATIBLE = "incompatible"


class RecoveryKind(StrEnum):
    """启动时只靠 receipt + Python 路径即可判定的保守恢复类别。"""

    READY = "ready"
    UPGRADE_REQUIRED = "upgrade_required"
    REPAIR_REQUIRED = "repair_required"


@dataclass
class RuntimeStatus:
    phase: Phase = Phase.NOT_STARTED
    percent: float = 0.0
    message: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "phase": self.phase.value,
            "percent": self.percent,
            "message": self.message,
            "error": self.error,
        }


@dataclass(frozen=True)
class RuntimeGenerationEvidence:
    """Identity captured before a runtime verification subprocess is started.

    The Python entry and its resolved regular-file target are both bound.  Conda
    commonly exposes ``bin/python`` as a relative symlink, so rejecting every
    symlink would make a normal managed install unusable.
    """

    prefix: Path
    prefix_identity: tuple[int, int]
    python: Path
    python_entry_identity: tuple[int, int, int, int, int]
    python_target: Path
    python_target_identity: tuple[int, int, int, int, int]


def write_status(s: RuntimeStatus) -> None:
    raw = json.dumps(s.to_dict()).encode("utf-8")
    with _pinned_runtime_write_root() as pinned:
        _atomic_write(paths.status_file(), raw, pinned_parent=pinned)


def read_status() -> RuntimeStatus:
    f = paths.status_file()
    raw = _read_bounded_text(f)
    if raw is None:
        return RuntimeStatus()
    try:
        d = json.loads(raw)
        return RuntimeStatus(Phase(d["phase"]), d["percent"], d["message"], d.get("error"))
    except (TypeError, ValueError, KeyError):
        return RuntimeStatus()


def _read_receipt_raw() -> str | None:
    sentinel = paths.ready_sentinel()
    if sentinel == paths.env_prefix() / ".vibecad_ready" and not _current_managed_prefix_is_safe():
        return None
    return _read_bounded_text(sentinel)


def _read_bounded_text(path: Path, *, parent_fd: int | None = None) -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = -1
    try:
        selected = path.name if parent_fd is not None else path
        fd = os.open(selected, flags, dir_fd=parent_fd)
        before = os.fstat(fd)
        getuid = getattr(os, "geteuid", None)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > _MAX_RECEIPT_BYTES
            or before.st_nlink != 1
            or (sys.platform != "win32" and stat.S_IMODE(before.st_mode) & 0o022)
            or (hasattr(before, "st_uid") and getuid is not None and before.st_uid != getuid())
        ):
            return None
        chunks = []
        remaining = _MAX_RECEIPT_BYTES + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        live = os.stat(selected, dir_fd=parent_fd, follow_symlinks=False)
        identity = lambda value: (  # noqa: E731
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            stat.S_IFMT(value.st_mode),
        )
        if (
            len(raw) > _MAX_RECEIPT_BYTES
            or len(raw) != before.st_size
            or identity(before) != identity(after)
            or identity(after) != identity(live)
        ):
            return None
        return raw.decode("utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)


class _PinnedDirectory:
    __slots__ = ("bindings", "fds")

    def __init__(self, fds, bindings) -> None:
        self.fds = tuple(fds)
        self.bindings = tuple(bindings)

    @property
    def fd(self) -> int:
        return self.fds[-1]

    def validate(self) -> None:
        for parent_fd, name, identity in self.bindings:
            value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(value.st_mode) or (value.st_dev, value.st_ino) != identity:
                raise ValueError("runtime write directory identity changed")

    def close(self) -> None:
        for fd in reversed(self.fds):
            with contextlib.suppress(OSError):
                os.close(fd)


def _secure_dir_fd_available() -> bool:
    """Whether stdlib can provide the no-follow relative-directory protocol.

    CPython on Windows does not currently expose the needed ``dir_fd`` APIs.
    That platform therefore uses the compatibility path below instead of making
    runtime installation unusable.
    """

    if sys.platform == "win32":
        return False
    supported = getattr(os, "supports_dir_fd", ())
    required = (os.open, os.mkdir, os.stat, os.unlink, os.rmdir, os.rename)
    return (
        all(operation in supported for operation in required)
        and bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NOFOLLOW", 0))
    )


def _entry_is_alias(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction is not None and is_junction())
    except OSError:
        return True


def _fallback_directory(path: Path, *, create_missing: bool) -> Path:
    """Best-effort Windows compatibility path.

    This rejects visible symlinks/junctions before and after creation.  It cannot
    close the parent-replacement race without ``dir_fd`` and is intentionally
    kept separate from the stronger POSIX implementation.
    """

    path = path.expanduser()
    if (
        not path.is_absolute()
        or path == Path(path.anchor)
        or any(part in {".", ".."} for part in path.parts[1:])
    ):
        raise ValueError("runtime write directory is invalid")
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current = current / part
            if os.path.lexists(current) and _entry_is_alias(current):
                raise ValueError("runtime write directory contains an alias")
        if create_missing:
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or _entry_is_alias(path):
            raise ValueError("runtime write directory is unsafe")
        getuid = getattr(os, "geteuid", None)
        if hasattr(info, "st_uid") and getuid is not None and info.st_uid != getuid():
            raise ValueError("runtime write directory is not owned by the current user")
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            if _entry_is_alias(current):
                raise ValueError("runtime write directory contains an alias")
        return path
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("runtime write directory is unavailable") from exc


def _pin_directory(path: Path, *, create_missing: bool) -> _PinnedDirectory:
    """Pin every absolute component, optionally creating missing directories."""

    if not _secure_dir_fd_available():
        raise ValueError("secure relative-directory operations are unavailable")
    if (
        not path.is_absolute()
        or path == Path(path.anchor)
        or any(part in {".", ".."} for part in path.parts[1:])
    ):
        raise ValueError("runtime write directory is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fds = []
    bindings = []
    try:
        fds.append(os.open(path.anchor, flags))
        for part in path.parts[1:]:
            parent_fd = fds[-1]
            try:
                child_fd = os.open(part, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create_missing:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    # A concurrent creator won the race; the no-follow open and
                    # identity comparison below still decide whether it is safe.
                    pass
                child_fd = os.open(part, flags, dir_fd=parent_fd)
            child = os.fstat(child_fd)
            entry = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(child.st_mode)
                or not stat.S_ISDIR(entry.st_mode)
                or (child.st_dev, child.st_ino) != (entry.st_dev, entry.st_ino)
            ):
                os.close(child_fd)
                raise ValueError("runtime write directory is unsafe")
            bindings.append((parent_fd, part, (child.st_dev, child.st_ino)))
            fds.append(child_fd)
        final = os.fstat(fds[-1])
        getuid = getattr(os, "geteuid", None)
        if not stat.S_ISDIR(final.st_mode) or (
            hasattr(final, "st_uid") and getuid is not None and final.st_uid != getuid()
        ):
            raise ValueError("runtime write directory is not owned by the current user")
        pinned = _PinnedDirectory(fds, bindings)
        pinned.validate()
        return pinned
    except (OSError, ValueError) as exc:
        for fd in reversed(fds):
            with contextlib.suppress(OSError):
                os.close(fd)
        if isinstance(exc, ValueError):
            raise
        raise ValueError("runtime write directory is unavailable") from exc


def _open_pinned_directory(path: Path) -> _PinnedDirectory:
    """Open every existing absolute component without following links."""

    return _pin_directory(path, create_missing=False)


def _ensure_pinned_directory(path: Path) -> _PinnedDirectory:
    """Create a directory chain relative to pinned parents without following links."""

    return _pin_directory(path, create_missing=True)


def _open_relative_pinned_directory(
    parent: _PinnedDirectory,
    parts: tuple[str, ...],
) -> _PinnedDirectory:
    """Open an existing descendant while retaining the parent's live bindings."""

    if not parts or any(part in {"", ".", ".."} or Path(part).name != part for part in parts):
        raise ValueError("runtime descendant is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fds: list[int] = []
    bindings = list(parent.bindings)
    parent_fd = parent.fd
    try:
        parent.validate()
        for part in parts:
            child_fd = os.open(part, flags, dir_fd=parent_fd)
            child = os.fstat(child_fd)
            entry = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(child.st_mode)
                or not stat.S_ISDIR(entry.st_mode)
                or (child.st_dev, child.st_ino) != (entry.st_dev, entry.st_ino)
            ):
                os.close(child_fd)
                raise ValueError("runtime descendant is unsafe")
            bindings.append((parent_fd, part, (child.st_dev, child.st_ino)))
            fds.append(child_fd)
            parent_fd = child_fd
        final = os.fstat(fds[-1])
        getuid = getattr(os, "geteuid", None)
        if hasattr(final, "st_uid") and getuid is not None and final.st_uid != getuid():
            raise ValueError("runtime descendant is not owned by the current user")
        pinned = _PinnedDirectory(fds, bindings)
        pinned.validate()
        return pinned
    except (OSError, ValueError) as exc:
        for fd in reversed(fds):
            with contextlib.suppress(OSError):
                os.close(fd)
        if isinstance(exc, ValueError):
            raise
        raise ValueError("runtime descendant is unavailable") from exc


def _write_all(fd: int, raw: bytes) -> None:
    offset = 0
    while offset < len(raw):
        written = os.write(fd, raw[offset:])
        if written <= 0:
            raise OSError("runtime write made no progress")
        offset += written


def _atomic_write(
    path: Path,
    raw: bytes,
    *,
    pinned_parent: _PinnedDirectory | None = None,
) -> tuple[int, int]:
    """Publish one private file relative to a pinned no-follow directory chain."""

    if type(raw) is not bytes or not raw:
        raise ValueError("runtime write payload is invalid")
    if pinned_parent is None and not _secure_dir_fd_available():
        return _atomic_write_fallback(path, raw)
    pinned = pinned_parent or _open_pinned_directory(path.parent)
    owns_pinned = pinned_parent is None
    temp = f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    fd = -1
    try:
        pinned.validate()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temp, flags, 0o600, dir_fd=pinned.fd)
        _write_all(fd, raw)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        pinned.validate()
        os.replace(
            temp,
            path.name,
            src_dir_fd=pinned.fd,
            dst_dir_fd=pinned.fd,
        )
        os.fsync(pinned.fd)
        pinned.validate()
        published = os.stat(path.name, dir_fd=pinned.fd, follow_symlinks=False)
        if not stat.S_ISREG(published.st_mode) or published.st_nlink != 1:
            raise ValueError("published runtime file is unsafe")
        return published.st_dev, published.st_ino
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(temp, dir_fd=pinned.fd)
        if owns_pinned:
            pinned.close()


def _atomic_write_fallback(path: Path, raw: bytes) -> tuple[int, int]:
    parent = _fallback_directory(path.parent, create_missing=False)
    temp = parent / f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    fd = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temp, flags, 0o600)
        _write_all(fd, raw)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        _fallback_directory(parent, create_missing=False)
        os.replace(temp, path)
        _fallback_directory(parent, create_missing=False)
        published = path.lstat()
        if not stat.S_ISREG(published.st_mode) or published.st_nlink != 1:
            raise ValueError("published runtime file is unsafe")
        directory_fd = -1
        try:
            directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            os.fsync(directory_fd)
        except (OSError, ValueError):
            pass
        finally:
            if directory_fd >= 0:
                os.close(directory_fd)
        return published.st_dev, published.st_ino
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            temp.unlink()


def _revoke_exact_publication(
    path: Path,
    identity: tuple[int, int],
    pinned_parent: _PinnedDirectory | None,
) -> None:
    """Remove only the receipt generation published by this writer."""

    try:
        if pinned_parent is None:
            live = path.lstat()
            if identity == (live.st_dev, live.st_ino):
                path.unlink()
            return
        pinned_parent.validate()
        live = os.stat(path.name, dir_fd=pinned_parent.fd, follow_symlinks=False)
        if identity == (live.st_dev, live.st_ino):
            os.unlink(path.name, dir_fd=pinned_parent.fd)
            os.fsync(pinned_parent.fd)
            pinned_parent.validate()
    except (OSError, ValueError):
        return


def _canonical_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True)


def _has_exact_scalar_fields(
    receipt: object,
    expected: Mapping[str, int | str],
) -> bool:
    """Match JSON receipt scalars without Python's bool/int coercion."""

    return type(receipt) is dict and all(
        key in receipt
        and type(receipt[key]) is type(expected_value)
        and receipt[key] == expected_value
        for key, expected_value in expected.items()
    )


def _decode_canonical_receipt(path: Path, *, parent_fd: int | None = None) -> dict | None:
    raw = _read_bounded_text(path, parent_fd=parent_fd)
    if raw is None:
        return None
    try:
        receipt = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(receipt, dict) or raw != _canonical_json(receipt):
        return None
    return receipt


def _safe_prefix_identity(prefix: Path) -> tuple[Path, os.stat_result]:
    expanded = prefix.expanduser()
    if not expanded.is_absolute():
        raise ValueError("external runtime prefix must be absolute")
    if expanded.is_symlink():
        raise ValueError(f"external runtime prefix must not be a symlink: {expanded}")
    try:
        resolved = expanded.resolve(strict=True)
        info = expanded.lstat()
    except OSError as exc:
        raise ValueError(f"external runtime prefix is unavailable: {expanded}") from exc
    if expanded != resolved or not stat.S_ISDIR(info.st_mode):
        raise ValueError(
            f"external runtime prefix contains a symlink or is not a directory: {expanded}"
        )
    getuid = getattr(os, "getuid", None)
    if hasattr(info, "st_uid") and getuid is not None and info.st_uid != getuid():
        raise ValueError(f"external runtime prefix is not owned by the current user: {expanded}")
    return resolved, info


def _exact_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
    )


def _pinned_python_identities(
    prefix: Path,
    pinned_prefix: _PinnedDirectory,
    *,
    expected_target: Path | None = None,
) -> tuple[Path, tuple[int, int, int, int, int], Path, tuple[int, int, int, int, int]]:
    python = paths.env_python_for(prefix)
    try:
        parent_parts = python.parent.relative_to(prefix).parts
    except ValueError as exc:
        raise ValueError("runtime Python escaped its prefix") from exc
    python_parent = (
        pinned_prefix
        if not parent_parts
        else _open_relative_pinned_directory(pinned_prefix, tuple(parent_parts))
    )
    owns_python_parent = python_parent is not pinned_prefix
    target_parent = None
    fd = -1
    try:
        pinned_prefix.validate()
        entry = os.stat(python.name, dir_fd=python_parent.fd, follow_symlinks=False)
        if not (stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode)):
            raise ValueError("runtime Python entry is not a file or symlink")
        try:
            target = python.resolve(strict=True)
            target_parts = target.relative_to(prefix).parts
        except (OSError, ValueError) as exc:
            raise ValueError("runtime Python target escaped or is unavailable") from exc
        if not target_parts:
            raise ValueError("runtime Python target is invalid")
        if expected_target is not None and target != expected_target:
            raise ValueError("runtime Python target identity changed")
        target_parent_parts = target_parts[:-1]
        target_parent = (
            pinned_prefix
            if not target_parent_parts
            else _open_relative_pinned_directory(
                pinned_prefix,
                tuple(target_parent_parts),
            )
        )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(target.name, flags, dir_fd=target_parent.fd)
        target_info = os.fstat(fd)
        target_entry = os.stat(target.name, dir_fd=target_parent.fd, follow_symlinks=False)
        if not stat.S_ISREG(target_info.st_mode) or _exact_identity(target_info) != _exact_identity(
            target_entry
        ):
            raise ValueError("runtime Python target is not one exact regular file")
        entry_after = os.stat(python.name, dir_fd=python_parent.fd, follow_symlinks=False)
        python_parent.validate()
        target_parent.validate()
        pinned_prefix.validate()
        if _exact_identity(entry) != _exact_identity(entry_after):
            raise ValueError("runtime Python entry identity changed")
        return python, _exact_identity(entry), target, _exact_identity(target_info)
    except OSError as exc:
        raise ValueError("runtime Python is unavailable") from exc
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if target_parent is not None and target_parent is not pinned_prefix:
            target_parent.close()
        if owns_python_parent:
            python_parent.close()


def _fallback_python_identities(
    prefix: Path,
    *,
    expected_target: Path | None = None,
) -> tuple[Path, tuple[int, int, int, int, int], Path, tuple[int, int, int, int, int]]:
    _fallback_directory(prefix, create_missing=False)
    python = paths.env_python_for(prefix)
    try:
        entry = python.lstat()
        if not (stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode)):
            raise ValueError("runtime Python entry is not a file or symlink")
        target = python.resolve(strict=True)
        target.relative_to(prefix)
        if expected_target is not None and target != expected_target:
            raise ValueError("runtime Python target identity changed")
        target_info = target.lstat()
        if not stat.S_ISREG(target_info.st_mode) or _entry_is_alias(target):
            raise ValueError("runtime Python target is not one regular file")
        if _exact_identity(entry) != _exact_identity(python.lstat()):
            raise ValueError("runtime Python entry identity changed")
        return python, _exact_identity(entry), target, _exact_identity(target_info)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("runtime Python is unavailable") from exc


def capture_runtime_generation_evidence(prefix: Path) -> RuntimeGenerationEvidence:
    """Capture the exact prefix/interpreter generation to verify before commit."""

    resolved, info = _safe_prefix_identity(prefix)
    if not _secure_dir_fd_available():
        python, entry_identity, target, target_identity = _fallback_python_identities(resolved)
        final = resolved.lstat()
    else:
        pinned = _open_pinned_directory(resolved)
        try:
            python, entry_identity, target, target_identity = _pinned_python_identities(
                resolved,
                pinned,
            )
            pinned.validate()
            final = os.fstat(pinned.fd)
        finally:
            pinned.close()
    if (info.st_dev, info.st_ino) != (final.st_dev, final.st_ino):
        raise ValueError("runtime prefix identity changed while capturing evidence")
    return RuntimeGenerationEvidence(
        prefix=resolved,
        prefix_identity=(info.st_dev, info.st_ino),
        python=python,
        python_entry_identity=entry_identity,
        python_target=target,
        python_target_identity=target_identity,
    )


def _validate_runtime_generation_evidence(
    evidence: RuntimeGenerationEvidence,
    prefix: Path,
    pinned_prefix: _PinnedDirectory | None = None,
) -> None:
    if type(evidence) is not RuntimeGenerationEvidence or evidence.prefix != prefix:
        raise ValueError("runtime generation evidence does not match the prefix")
    if pinned_prefix is None:
        info = prefix.lstat()
        values = _fallback_python_identities(
            prefix,
            expected_target=evidence.python_target,
        )
    else:
        pinned_prefix.validate()
        info = os.fstat(pinned_prefix.fd)
        values = _pinned_python_identities(
            prefix,
            pinned_prefix,
            expected_target=evidence.python_target,
        )
    python, entry_identity, target, target_identity = values
    if (
        (info.st_dev, info.st_ino) != evidence.prefix_identity
        or python != evidence.python
        or entry_identity != evidence.python_entry_identity
        or target != evidence.python_target
        or target_identity != evidence.python_target_identity
    ):
        raise ValueError("verified runtime generation identity changed")


def _validate_runtime_data_boundary(home: Path, home_fd: int | None = None) -> None:
    try:
        if home_fd is None:
            data = (home / "data").lstat()
        else:
            data = os.stat("data", dir_fd=home_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    getuid = getattr(os, "geteuid", None)
    if not stat.S_ISDIR(data.st_mode) or (
        hasattr(data, "st_uid") and getuid is not None and data.st_uid != getuid()
    ):
        raise ValueError("durable data root must be a real owned directory")


@contextlib.contextmanager
def _pinned_runtime_write_root():
    """Yield one runtime generation through publication and final validation."""

    home = paths.vibecad_home().expanduser()
    if not home.is_absolute():
        raise ValueError("VibeCAD home must be absolute")
    root = paths.runtime_root()
    if root.parent != home or root.name != "runtime":
        raise ValueError("runtime root escaped VibeCAD home")
    if not _secure_dir_fd_available():
        home = _fallback_directory(home, create_missing=True)
        root = _fallback_directory(root, create_missing=True)
        _validate_runtime_data_boundary(home)
        yield None
        _fallback_directory(root, create_missing=False)
        _validate_runtime_data_boundary(home)
        return

    pinned = _ensure_pinned_directory(root)
    try:
        home_fd = pinned.fds[-2]
        _validate_runtime_data_boundary(home, home_fd)
        pinned.validate()
        yield pinned
        pinned.validate()
        _validate_runtime_data_boundary(home, home_fd)
    finally:
        pinned.close()


def _ensure_runtime_write_root() -> Path:
    """Create only the replaceable root and fail closed on runtime/data aliases."""

    with _pinned_runtime_write_root():
        pass
    return paths.runtime_root()


def _ensure_private_child_directory(
    parent: _PinnedDirectory,
    name: str,
) -> _PinnedDirectory:
    """Create/open one owned private child relative to a live pinned parent."""

    if (
        type(parent) is not _PinnedDirectory
        or not name
        or name in {".", ".."}
        or Path(name).name != name
    ):
        raise ValueError("FreeCAD process directory is unavailable")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        parent.validate()
        try:
            os.mkdir(name, 0o700, dir_fd=parent.fd)
        except FileExistsError:
            pass
        fd = os.open(name, flags, dir_fd=parent.fd)
        opened = os.fstat(fd)
        entry = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
        getuid = getattr(os, "geteuid", None)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(entry.st_mode)
            or (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino)
            or stat.S_IMODE(opened.st_mode) & 0o077
            or (hasattr(opened, "st_uid") and getuid is not None and opened.st_uid != getuid())
        ):
            raise ValueError("FreeCAD process directory is unavailable")
        pinned = _PinnedDirectory(
            (fd,),
            (
                *parent.bindings,
                (parent.fd, name, (opened.st_dev, opened.st_ino)),
            ),
        )
        fd = -1
        pinned.validate()
        parent.validate()
        return pinned
    except (OSError, ValueError):
        raise ValueError("FreeCAD process directory is unavailable") from None
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)


def _fallback_private_directory(path: Path) -> Path:
    """Best-effort compatibility check for one private FreeCAD directory.

    Windows ``st_mode`` does not describe the directory DACL, so that platform
    retains the fallback's alias/ownership checks and its existing S3-RES-02
    limitation instead of rejecting every normal directory as POSIX ``0777``.
    """

    try:
        directory = _fallback_directory(path, create_missing=True)
        info = directory.lstat()
        getuid = getattr(os, "geteuid", None)
        if (
            not stat.S_ISDIR(info.st_mode)
            or (sys.platform != "win32" and stat.S_IMODE(info.st_mode) & 0o077)
            or (hasattr(info, "st_uid") and getuid is not None and info.st_uid != getuid())
        ):
            raise ValueError("FreeCAD process directory is unavailable")
        return directory
    except (OSError, ValueError):
        raise ValueError("FreeCAD process directory is unavailable") from None


def _reject_selected_external_runtime_overlap(runtime: Path) -> None:
    """Reject an external prefix that could contain the private process tree."""

    selected = paths.user_override_env()
    if selected is None:
        bound = paths.bound_external_prefix()
        if bound is not None and paths.active_runtime_prefix() == bound:
            selected = bound
    if selected is None:
        return
    external = selected.expanduser().resolve(strict=False)
    resolved_runtime = runtime.expanduser().resolve(strict=False)
    if (
        external == resolved_runtime
        or external.is_relative_to(resolved_runtime)
        or resolved_runtime.is_relative_to(external)
    ):
        raise ValueError("FreeCAD process directory is unavailable")


def _validate_private_process_ancestors(pinned: _PinnedDirectory) -> None:
    """Reject a path component another local account could rename after validation.

    A root-owned or current-user-owned sticky ancestor (for example ``/tmp``)
    keeps its child entries protected.  The final runtime directory is stricter:
    it must be current-user owned and not group/world writable.  Replacement by
    another process under the same UID remains the explicit local-host boundary.
    """

    if type(pinned) is not _PinnedDirectory or not pinned.fds:
        raise ValueError("FreeCAD process directory is unavailable")
    getuid = getattr(os, "geteuid", None)
    current_uid = getuid() if getuid is not None else None
    final_index = len(pinned.fds) - 1
    for index, fd in enumerate(pinned.fds):
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("FreeCAD process directory is unavailable")
        writable_by_others = bool(stat.S_IMODE(info.st_mode) & 0o022)
        if not writable_by_others:
            continue
        owner = getattr(info, "st_uid", None)
        protected_sticky_ancestor = (
            index != final_index and bool(info.st_mode & stat.S_ISVTX) and owner in {0, current_uid}
        )
        if not protected_sticky_ancestor:
            raise ValueError("FreeCAD process directory is unavailable")
    final = os.fstat(pinned.fd)
    if current_uid is not None and hasattr(final, "st_uid") and final.st_uid != current_uid:
        raise ValueError("FreeCAD process directory is unavailable")
    pinned.validate()


def freecad_process_environment(
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a copied process environment with private FreeCAD directories.

    FreeCAD on POSIX resolves the account home through ``getpwuid_r`` unless all
    three custom locations already exist.  Some sandboxed agent hosts cannot
    resolve that account record.  Keep the workaround inside the replaceable
    runtime generation: never write the selected FreeCAD prefix, legacy trees,
    or durable project data, and never mutate ``os.environ``.
    """

    if base is None:
        environment: dict[str, str] = {}
    else:
        if not isinstance(base, Mapping):
            raise TypeError("base environment must be a string mapping")
        environment = dict(base)
        if any(
            type(key) is not str or type(value) is not str for key, value in environment.items()
        ):
            raise TypeError("base environment must be a string mapping")

    runtime = paths.runtime_root()
    container_path = runtime / "freecad-user"
    directory_names = {
        "FREECAD_USER_HOME": "home",
        "FREECAD_USER_DATA": "data",
        "FREECAD_USER_TEMP": "temp",
    }
    try:
        _reject_selected_external_runtime_overlap(runtime)
        with _pinned_runtime_write_root() as runtime_pinned:
            if runtime_pinned is None:
                _fallback_private_directory(container_path)
                for name in directory_names.values():
                    _fallback_private_directory(container_path / name)
            else:
                _validate_private_process_ancestors(runtime_pinned)
                container = _ensure_private_child_directory(runtime_pinned, "freecad-user")
                try:
                    for name in directory_names.values():
                        child = _ensure_private_child_directory(container, name)
                        try:
                            child.validate()
                        finally:
                            child.close()
                    container.validate()
                    _validate_private_process_ancestors(runtime_pinned)
                    runtime_pinned.validate()
                finally:
                    container.close()
    except (OSError, ValueError):
        raise ValueError("FreeCAD process directory is unavailable") from None

    environment.update(
        {variable: str(container_path / name) for variable, name in directory_names.items()}
    )
    return environment


def _ensure_maintenance_write_root() -> Path:
    """Create only the stable home container used by runtime maintenance locking."""
    home = paths.vibecad_home().expanduser()
    if not home.is_absolute():
        raise ValueError("VibeCAD home must be absolute")
    if not _secure_dir_fd_available():
        return _fallback_directory(home, create_missing=True)
    pinned = _ensure_pinned_directory(home)
    try:
        pinned.validate()
    finally:
        pinned.close()
    return home


def _current_managed_prefix_is_safe() -> bool:
    prefix = paths.env_prefix()
    try:
        if not _secure_dir_fd_available():
            _fallback_directory(prefix, create_missing=False)
            return True
        pinned = _open_pinned_directory(prefix)
        try:
            pinned.validate()
            return True
        finally:
            pinned.close()
    except ValueError:
        return False


def append_install_log(text: str) -> None:
    """Append one bounded installer record within one pinned runtime generation."""

    if type(text) is not str:
        raise ValueError("installer log record must be text")
    raw = text.encode("utf-8")
    if not raw:
        return
    if len(raw) > _MAX_LOG_APPEND_BYTES:
        raise ValueError("installer log record is too large")
    target = paths.install_log()
    with _pinned_runtime_write_root() as pinned:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = -1
        try:
            if pinned is None:
                _fallback_directory(target.parent, create_missing=False)
                if os.path.lexists(target) and _entry_is_alias(target):
                    raise ValueError("installer log path is an alias")
                fd = os.open(target, flags, 0o600)
                live = target.lstat()
            else:
                pinned.validate()
                fd = os.open(target.name, flags, 0o600, dir_fd=pinned.fd)
                live = os.stat(target.name, dir_fd=pinned.fd, follow_symlinks=False)
            opened = os.fstat(fd)
            getuid = getattr(os, "geteuid", None)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (sys.platform != "win32" and stat.S_IMODE(opened.st_mode) & 0o022)
                or (hasattr(opened, "st_uid") and getuid is not None and opened.st_uid != getuid())
                or (opened.st_dev, opened.st_ino) != (live.st_dev, live.st_ino)
            ):
                raise ValueError("installer log is not one safe regular file")
            _write_all(fd, raw)
            os.fsync(fd)
            if pinned is None:
                final = target.lstat()
                _fallback_directory(target.parent, create_missing=False)
            else:
                final = os.stat(target.name, dir_fd=pinned.fd, follow_symlinks=False)
                pinned.validate()
            if (opened.st_dev, opened.st_ino) != (final.st_dev, final.st_ino):
                raise ValueError("installer log identity changed")
        except OSError as exc:
            raise ValueError("installer log is unavailable") from exc
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)


def read_prefix_receipt(prefix: Path) -> dict | None:
    """Read a canonical in-prefix receipt without following a receipt symlink."""
    return _decode_canonical_receipt(prefix / ".vibecad_ready")


def _fixed_legacy_prefix_is_safe(prefix: Path) -> bool:
    if prefix != paths.legacy_env_prefix() or not prefix.is_absolute():
        return False
    try:
        info = prefix.lstat()
        home = paths.vibecad_home().expanduser()
        if home.is_symlink() or home.resolve(strict=True) != home:
            return False
        if prefix.is_symlink() or prefix.resolve(strict=True) != prefix:
            return False
        if not stat.S_ISDIR(info.st_mode):
            return False
        getuid = getattr(os, "getuid", None)
        return not (hasattr(info, "st_uid") and getuid is not None and info.st_uid != getuid())
    except OSError:
        return False


def _managed_receipt_has_compatible_engine(receipt: object) -> bool:
    """Recognize exact known managed shapes whose engine binding remains compatible."""

    if type(receipt) is not dict:
        return False
    keys = frozenset(receipt)
    if keys not in {_PRE_EPOCH_MANAGED_RECEIPT_KEYS, _CURRENT_MANAGED_RECEIPT_KEYS}:
        return False
    version = receipt.get("vibecad_version")
    if (
        type(receipt.get("schema")) is not int
        or receipt.get("schema") != spec.RECEIPT_SCHEMA
        or type(receipt.get("runtime_kind")) is not str
        or receipt.get("runtime_kind") != spec.MANAGED_KIND
        or type(receipt.get("python_pin")) is not str
        or receipt.get("python_pin") != spec.PYTHON_PIN
        or type(receipt.get("freecad_pin")) is not str
        or receipt.get("freecad_pin") != spec.FREECAD_PIN
        or type(version) is not str
        or _BOUNDED_VERSION.fullmatch(version) is None
    ):
        return False
    if keys == _PRE_EPOCH_MANAGED_RECEIPT_KEYS:
        return True
    epoch = receipt.get("server_package_epoch")
    mcp_version = receipt.get("mcp_version")
    surface = receipt.get("public_surface_sha256")
    return (
        type(epoch) is int
        and 0 < epoch <= 2_147_483_647
        and type(mcp_version) is str
        and _BOUNDED_VERSION.fullmatch(mcp_version) is not None
        and type(surface) is str
        and _SHA256.fullmatch(surface) is not None
    )


def managed_legacy_receipt(prefix: Path) -> dict | None:
    """Return the bounded ownership proof accepted for a legacy managed env."""
    if not _fixed_legacy_prefix_is_safe(prefix):
        return None
    receipt = read_prefix_receipt(prefix)
    if not _managed_receipt_has_compatible_engine(receipt):
        return None
    assert type(receipt) is dict
    return receipt


def legacy_external_receipt(prefix: Path) -> dict | None:
    if not _fixed_legacy_prefix_is_safe(prefix):
        return None
    receipt = read_prefix_receipt(prefix)
    expected = spec.expected_receipt(external=True)
    return (
        receipt
        if type(receipt) is dict
        and set(receipt) == set(expected)
        and _has_exact_scalar_fields(receipt, expected)
        else None
    )


def _read_external_receipt_from_fixed_runtime() -> dict | None:
    home = paths.vibecad_home().expanduser()
    root = paths.runtime_root()
    target = paths.external_runtime_receipt()
    if not home.is_absolute() or root.parent != home or target.parent != root:
        return None
    try:
        if not _secure_dir_fd_available():
            _fallback_directory(root, create_missing=False)
            _validate_runtime_data_boundary(home)
            receipt = _decode_canonical_receipt(target)
            _fallback_directory(root, create_missing=False)
            _validate_runtime_data_boundary(home)
            return receipt
        pinned = _open_pinned_directory(root)
        try:
            home_fd = pinned.fds[-2]
            _validate_runtime_data_boundary(home, home_fd)
            receipt = _decode_canonical_receipt(target, parent_fd=pinned.fd)
            pinned.validate()
            _validate_runtime_data_boundary(home, home_fd)
            return receipt
        finally:
            pinned.close()
    except (OSError, ValueError):
        return None


def _validated_external_binding() -> dict | None:
    receipt = _read_external_receipt_from_fixed_runtime()
    if receipt is None or set(receipt) != _EXTERNAL_RECEIPT_KEYS:
        return None
    expected = spec.expected_receipt(external=True)
    if not _has_exact_scalar_fields(receipt, expected):
        return None
    python_version = ".".join(map(str, spec.PYTHON_VERSION))
    freecad_version = ".".join(map(str, spec.FREECAD_VERSION))
    if (
        type(receipt.get("python_version")) is not str
        or receipt.get("python_version") != python_version
        or type(receipt.get("freecad_version")) is not str
        or receipt.get("freecad_version") != freecad_version
    ):
        return None
    raw_prefix = receipt.get("prefix")
    if type(raw_prefix) is not str:
        return None
    try:
        prefix, info = _safe_prefix_identity(Path(raw_prefix))
    except ValueError:
        return None
    if (
        str(prefix) != raw_prefix
        or type(receipt.get("prefix_device")) is not int
        or info.st_dev != receipt.get("prefix_device")
        or type(receipt.get("prefix_inode")) is not int
        or info.st_ino != receipt.get("prefix_inode")
        or not paths.env_python_for(prefix).is_file()
    ):
        return None
    override = paths.user_override_env()
    if override is not None:
        try:
            if override.expanduser().resolve(strict=True) != prefix:
                return None
        except (OSError, ValueError):
            return None
    return receipt


def read_runtime_receipt() -> dict | None:
    """读取 JSON receipt；legacy 纯文本、损坏内容和非对象均返回 ``None``。"""
    if paths.ready_sentinel() == paths.external_runtime_receipt():
        binding = _validated_external_binding()
        if binding is None:
            return None
        return {key: binding[key] for key in spec.expected_receipt(external=True)}
    raw = _read_receipt_raw()
    if raw is None:
        return None
    try:
        receipt = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return receipt if isinstance(receipt, dict) else None


def runtime_receipt_state() -> ReceiptState:
    """判定 receipt 与当前 bootstrap 的兼容关系，全程只做小文件读取。"""
    if paths.ready_sentinel() == paths.external_runtime_receipt():
        return (
            ReceiptState.CURRENT
            if _validated_external_binding() is not None
            else ReceiptState.INCOMPATIBLE
        )
    raw = _read_receipt_raw()
    if raw is None:
        return ReceiptState.MISSING
    if raw.strip() == spec.FREECAD_PIN:
        return ReceiptState.LEGACY
    try:
        receipt = json.loads(raw)
    except (TypeError, ValueError):
        return ReceiptState.INCOMPATIBLE
    if not isinstance(receipt, dict):
        return ReceiptState.INCOMPATIBLE

    expected = spec.expected_receipt()
    if set(receipt) == set(expected) and _has_exact_scalar_fields(receipt, expected):
        return ReceiptState.CURRENT
    if _managed_receipt_has_compatible_engine(receipt):
        return ReceiptState.SERVER_MISMATCH
    return ReceiptState.INCOMPATIBLE


def write_runtime_receipt(evidence: RuntimeGenerationEvidence | None = None) -> None:
    """验证成功后的提交点：原子替换 JSON receipt，避免 supervisor 读到半文件。"""
    if paths.user_override_env() is not None:
        write_external_runtime_receipt(paths.user_override_env(), evidence=evidence)
        return
    write_managed_runtime_receipt(paths.active_runtime_prefix(), evidence=evidence)


def write_managed_runtime_receipt(
    prefix: Path,
    evidence: RuntimeGenerationEvidence | None = None,
) -> None:
    """Atomically publish a managed receipt to one exact fixed prefix."""
    if prefix not in {paths.env_prefix(), paths.legacy_env_prefix()}:
        raise ValueError(f"refusing managed receipt outside fixed runtime prefixes: {prefix}")
    if prefix == paths.legacy_env_prefix() and not _fixed_legacy_prefix_is_safe(prefix):
        raise ValueError(f"unsafe legacy runtime prefix: {prefix}")
    evidence = evidence or capture_runtime_generation_evidence(prefix)
    raw = json.dumps(spec.expected_receipt(), sort_keys=True).encode("utf-8")
    if prefix == paths.env_prefix():
        with _pinned_runtime_write_root() as runtime_pinned:
            if runtime_pinned is None:
                current = _fallback_directory(prefix, create_missing=False)
                _validate_runtime_generation_evidence(evidence, current)
                sentinel = prefix / ".vibecad_ready"
                published = _atomic_write(sentinel, raw)
                try:
                    _validate_runtime_generation_evidence(evidence, current)
                except (OSError, ValueError):
                    _revoke_exact_publication(sentinel, published, None)
                    raise
                return
            relative = prefix.relative_to(paths.runtime_root())
            prefix_pinned = _open_relative_pinned_directory(
                runtime_pinned,
                tuple(relative.parts),
            )
            try:
                _validate_runtime_generation_evidence(evidence, prefix, prefix_pinned)
                sentinel = prefix / ".vibecad_ready"
                published = _atomic_write(
                    sentinel,
                    raw,
                    pinned_parent=prefix_pinned,
                )
                try:
                    _validate_runtime_generation_evidence(evidence, prefix, prefix_pinned)
                except (OSError, ValueError):
                    _revoke_exact_publication(sentinel, published, prefix_pinned)
                    raise
            finally:
                prefix_pinned.close()
        return

    if not _secure_dir_fd_available():
        legacy = _fallback_directory(prefix, create_missing=False)
        _validate_runtime_generation_evidence(evidence, legacy)
        sentinel = prefix / ".vibecad_ready"
        published = _atomic_write(sentinel, raw)
        try:
            _validate_runtime_generation_evidence(evidence, legacy)
        except (OSError, ValueError):
            _revoke_exact_publication(sentinel, published, None)
            raise
        return
    prefix_pinned = _open_pinned_directory(prefix)
    try:
        _validate_runtime_generation_evidence(evidence, prefix, prefix_pinned)
        sentinel = prefix / ".vibecad_ready"
        published = _atomic_write(
            sentinel,
            raw,
            pinned_parent=prefix_pinned,
        )
        try:
            _validate_runtime_generation_evidence(evidence, prefix, prefix_pinned)
        except (OSError, ValueError):
            _revoke_exact_publication(sentinel, published, prefix_pinned)
            raise
    finally:
        prefix_pinned.close()


def write_external_runtime_receipt(
    prefix: Path,
    evidence: RuntimeGenerationEvidence | None = None,
) -> None:
    """Bind verified external evidence under runtime without modifying the env."""
    resolved, _info = _safe_prefix_identity(prefix)
    if resolved == paths.legacy_env_prefix() and managed_legacy_receipt(resolved) is not None:
        raise ValueError("legacy runtime has conflicting managed ownership evidence")
    runtime = paths.runtime_root().expanduser().resolve(strict=False)
    if resolved == runtime or resolved.is_relative_to(runtime) or runtime.is_relative_to(resolved):
        raise ValueError("external runtime prefix overlaps the replaceable runtime root")
    evidence = evidence or capture_runtime_generation_evidence(resolved)
    receipt = {
        **spec.expected_receipt(external=True),
        "prefix": str(resolved),
        "prefix_device": evidence.prefix_identity[0],
        "prefix_inode": evidence.prefix_identity[1],
        "python_version": ".".join(map(str, spec.PYTHON_VERSION)),
        "freecad_version": ".".join(map(str, spec.FREECAD_VERSION)),
    }
    target = paths.external_runtime_receipt()
    if target.parent != paths.runtime_root():
        raise ValueError("external receipt escaped runtime root")
    raw = _canonical_json(receipt).encode("utf-8")
    if not _secure_dir_fd_available():
        _validate_runtime_generation_evidence(evidence, resolved)
        with _pinned_runtime_write_root() as runtime_pinned:
            published = _atomic_write(target, raw, pinned_parent=runtime_pinned)
            try:
                _validate_runtime_generation_evidence(evidence, resolved)
            except (OSError, ValueError):
                _revoke_exact_publication(target, published, runtime_pinned)
                raise
        return
    prefix_pinned = _open_pinned_directory(resolved)
    try:
        _validate_runtime_generation_evidence(evidence, resolved, prefix_pinned)
        with _pinned_runtime_write_root() as runtime_pinned:
            published = _atomic_write(target, raw, pinned_parent=runtime_pinned)
            try:
                _validate_runtime_generation_evidence(evidence, resolved, prefix_pinned)
            except (OSError, ValueError):
                _revoke_exact_publication(target, published, runtime_pinned)
                raise
    finally:
        prefix_pinned.close()


def runtime_ready() -> bool:
    """廉价就绪探测：receipt 精确匹配且目标 Python 存在，不 import FreeCAD。"""
    return runtime_recovery_kind() is RecoveryKind.READY


def runtime_recovery_kind() -> RecoveryKind:
    """区分轻量 server 同步与可能重建引擎；不启动子进程，保持握手廉价。

    receipt 缺失/损坏时即使 env 最终可被 installer 原地验证并复用，这里仍保守标为
    repair_required，避免在尚未验证前向用户承诺只是轻量升级。外部 override 也始终
    由用户维护；installer 不会改写它，因此版本不匹配不能承诺自动同步。
    """
    state = runtime_receipt_state()
    try:
        python_exists = paths.active_runtime_python().exists()
    except OSError:
        python_exists = False
    if state is ReceiptState.CURRENT and python_exists:
        return RecoveryKind.READY
    if state in {ReceiptState.LEGACY, ReceiptState.SERVER_MISMATCH} and python_exists:
        if paths.user_override_env() is not None:
            return RecoveryKind.REPAIR_REQUIRED
        return RecoveryKind.UPGRADE_REQUIRED
    return RecoveryKind.REPAIR_REQUIRED


def _probe(python: Path | None, snippet: str) -> bool:
    py = python or paths.active_runtime_python()
    if not Path(py).exists():
        return False
    try:
        environment = freecad_process_environment({**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        result = subprocess.run(
            [str(py), "-I", "-B", "-c", snippet],
            capture_output=True,
            timeout=120,
            env=environment,
        )
        return result.returncode == 0
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        return False


def _spawn_probe_process(*args, **kwargs):
    """Narrow spawn seam for deterministic capability-replacement tests."""

    return subprocess.run(*args, **kwargs)


_FD_EXEC_HELPER = (
    "import os,sys\n"
    "fd=int(sys.argv[1],10)\n"
    "target=sys.argv[2]\n"
    "if os.path.isabs(target) or not target.startswith(('./','../')):raise SystemExit(126)\n"
    "os.fchdir(fd)\n"
    "os.execv(target,sys.argv[2:])\n"
)


def _probe_runtime_generation(
    evidence: RuntimeGenerationEvidence,
    snippet: str,
) -> bool:
    """Probe from the evidence-bound Python parent instead of an absolute path.

    On POSIX this closes ancestor/prefix replacement: the child changes directory
    with an inherited pinned FD and executes only the relative Python entry.  A
    same-UID actor replacing that single entry inside the already-pinned parent is
    the explicit residual on platforms without ``fexecve`` (including macOS).
    """

    if type(evidence) is not RuntimeGenerationEvidence:
        return False
    if not _secure_dir_fd_available():
        if not _probe(evidence.python, snippet):
            return False
        try:
            _validate_runtime_generation_evidence(evidence, evidence.prefix)
        except (OSError, ValueError):
            return False
        return True

    prefix_pinned = None
    python_parent = None
    try:
        prefix_pinned = _open_pinned_directory(evidence.prefix)
        _validate_runtime_generation_evidence(
            evidence,
            evidence.prefix,
            prefix_pinned,
        )
        parent_parts = evidence.python.parent.relative_to(evidence.prefix).parts
        python_parent = (
            prefix_pinned
            if not parent_parts
            else _open_relative_pinned_directory(prefix_pinned, tuple(parent_parts))
        )
        python_parent.validate()
        environment = freecad_process_environment({**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        parent_fd = python_parent.fd
        launcher = os.fspath(sys.executable)
        if not launcher or not os.path.isabs(launcher):
            return False
        target_command = [f"./{evidence.python.name}", "-I", "-B", "-c", snippet]
        result = _spawn_probe_process(
            [
                launcher,
                "-I",
                "-B",
                "-c",
                _FD_EXEC_HELPER,
                str(parent_fd),
                *target_command,
            ],
            capture_output=True,
            timeout=120,
            env=environment,
            pass_fds=(parent_fd,),
        )
        python_parent.validate()
        _validate_runtime_generation_evidence(
            evidence,
            evidence.prefix,
            prefix_pinned,
        )
        return result.returncode == 0
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        return False
    finally:
        if python_parent is not None and python_parent is not prefix_pinned:
            python_parent.close()
        if prefix_pinned is not None:
            prefix_pinned.close()


def health_check(python: Path | None = None) -> bool:
    """子进程 import FreeCAD, Part。"""
    return _probe(python, _HEALTH_SNIPPET)


def engine_compatible(python: Path | None = None) -> bool:
    """托管 env 的 Python 与 FreeCAD 版本精确匹配当前 pins。"""
    return _probe(python, _ENGINE_SNIPPET)


def verify_runtime(python: Path | None = None) -> bool:
    """安装期/override 校验 FreeCAD、server 及其版本与 bootstrap 精确一致。"""
    return _probe(python, _VERIFY_SNIPPET)


def engine_compatible_generation(evidence: RuntimeGenerationEvidence) -> bool:
    """Capability-bound exact managed-engine probe."""

    return _probe_runtime_generation(evidence, _ENGINE_SNIPPET)


def verify_runtime_generation(evidence: RuntimeGenerationEvidence) -> bool:
    """Capability-bound full runtime/server probe."""

    return _probe_runtime_generation(evidence, _VERIFY_SNIPPET)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        # B-1：Windows 上 os.kill(pid,0) 会 TerminateProcess 杀掉目标！改用 OpenProcess 探活
        import ctypes

        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _try_exclusive_flock(fd: int) -> bool:
    """Take a kernel-released claim on one open POSIX lock generation."""

    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (ImportError, OSError):
        return False


class FileLock:
    """Identity/token-bound mkdir lock with single-winner stale reclamation."""

    def __init__(self, path: Path):
        self.path = Path(os.path.abspath(Path(path).expanduser()))
        self._meta = self.path / "owner.json"
        self._token: str | None = None
        self._identity: tuple[int, int] | None = None
        self._parent_pin: _PinnedDirectory | None = None
        self._lock_fd: int = -1

    def try_acquire(self) -> bool:
        if self._token is not None:
            return False
        if _secure_dir_fd_available():
            return self._try_acquire_pinned()
        return self._try_acquire_fallback()

    @staticmethod
    def _lock_flags() -> int:
        return (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )

    @staticmethod
    def _read_small_at(directory_fd: int, name: str) -> bytes | None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = -1
        try:
            fd = os.open(name, flags, dir_fd=directory_fd)
            before = os.fstat(fd)
            getuid = getattr(os, "geteuid", None)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size > _MAX_RECEIPT_BYTES
                or before.st_nlink != 1
                or (sys.platform != "win32" and stat.S_IMODE(before.st_mode) & 0o022)
                or (hasattr(before, "st_uid") and getuid is not None and before.st_uid != getuid())
            ):
                return None
            raw = os.read(fd, _MAX_RECEIPT_BYTES + 1)
            if len(raw) != before.st_size or os.read(fd, 1):
                return None
            after = os.fstat(fd)
            live = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if _exact_identity(before) != _exact_identity(after) or _exact_identity(
                after
            ) != _exact_identity(live):
                return None
            return raw
        except OSError:
            return None
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)

    @classmethod
    def _read_owner_at(cls, directory_fd: int) -> dict | None:
        raw = cls._read_small_at(directory_fd, "owner.json")
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _write_private_at(directory_fd: int, name: str, raw: bytes) -> tuple[int, int]:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = -1
        created_identity: tuple[int, int] | None = None
        try:
            fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
            created = os.fstat(fd)
            created_identity = (created.st_dev, created.st_ino)
            _write_all(fd, raw)
            os.fsync(fd)
            info = os.fstat(fd)
            live = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (live.st_dev, live.st_ino)
            ):
                raise OSError("lock metadata identity changed")
            return info.st_dev, info.st_ino
        except OSError:
            if created_identity is not None:
                with contextlib.suppress(OSError):
                    live = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if created_identity == (live.st_dev, live.st_ino):
                        os.unlink(name, dir_fd=directory_fd)
            raise
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)

    def _try_acquire_pinned(self) -> bool:
        try:
            parent = _open_pinned_directory(self.path.parent)
        except ValueError:
            return False
        try:
            for _attempt in range(2):
                try:
                    os.mkdir(self.path.name, 0o700, dir_fd=parent.fd)
                except FileExistsError:
                    if not self._reclaim_if_stale_pinned(parent):
                        return False
                    continue
                except OSError:
                    return False
                return self._initialize_pinned_owner(parent)
            return False
        finally:
            if self._parent_pin is not parent:
                parent.close()

    def _initialize_pinned_owner(self, parent: _PinnedDirectory) -> bool:
        lock_fd = -1
        identity: tuple[int, int] | None = None
        token = secrets.token_hex(16)
        owner_written = False
        try:
            lock_fd = os.open(self.path.name, self._lock_flags(), dir_fd=parent.fd)
            if not _try_exclusive_flock(lock_fd):
                raise OSError("lock generation already has a live claimant")
            info = os.fstat(lock_fd)
            live = os.stat(self.path.name, dir_fd=parent.fd, follow_symlinks=False)
            identity = (info.st_dev, info.st_ino)
            if not stat.S_ISDIR(info.st_mode) or identity != (live.st_dev, live.st_ino):
                raise OSError("lock directory identity changed")
            owner = json.dumps(
                {"pid": os.getpid(), "ts": time.time(), "token": token},
                sort_keys=True,
            ).encode("utf-8")
            self._write_private_at(lock_fd, "owner.json", owner)
            owner_written = True
            os.fsync(lock_fd)
            parent.validate()
            live = os.stat(self.path.name, dir_fd=parent.fd, follow_symlinks=False)
            if identity != (live.st_dev, live.st_ino):
                raise OSError("lock directory identity changed")
            self._token = token
            self._identity = identity
            self._parent_pin = parent
            self._lock_fd = lock_fd
            return True
        except (OSError, ValueError):
            if owner_written and lock_fd >= 0:
                with contextlib.suppress(OSError):
                    os.unlink("owner.json", dir_fd=lock_fd)
            if identity is not None:
                with contextlib.suppress(OSError):
                    live = os.stat(self.path.name, dir_fd=parent.fd, follow_symlinks=False)
                    if identity == (live.st_dev, live.st_ino):
                        os.rmdir(self.path.name, dir_fd=parent.fd)
            if lock_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(lock_fd)
            return False

    def _reclaim_if_stale_pinned(self, parent: _PinnedDirectory) -> bool:
        lock_fd = -1
        try:
            lock_fd = os.open(self.path.name, self._lock_flags(), dir_fd=parent.fd)
            # This claim disappears automatically on close/process death; unlike
            # an O_EXCL marker it cannot permanently strand the maintenance lock.
            if not _try_exclusive_flock(lock_fd):
                return False
            info = os.fstat(lock_fd)
            identity = (info.st_dev, info.st_ino)
            live = os.stat(self.path.name, dir_fd=parent.fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or identity != (live.st_dev, live.st_ino):
                return False
            owner = self._read_owner_at(lock_fd)
            if owner is not None and isinstance(owner.get("pid"), int):
                stale = not _pid_alive(owner["pid"])
            else:
                stale = time.time() - info.st_mtime > _STALE_SECONDS
            if not stale:
                return False
            live = os.stat(self.path.name, dir_fd=parent.fd, follow_symlinks=False)
            if identity != (live.st_dev, live.st_ino):
                return False
            try:
                names = set(os.listdir(lock_fd))
            except OSError:
                return False
            if not names.issubset({"owner.json", ".reclaim"}):
                return False
            # Converge a marker left by the pre-flock protocol.  A live legacy
            # claimant still blocks; dead JSON evidence or an aged unreadable
            # marker can be removed while this generation is kernel-claimed.
            if ".reclaim" in names:
                claim = self._read_small_at(lock_fd, ".reclaim")
                claim_info = os.stat(".reclaim", dir_fd=lock_fd, follow_symlinks=False)
                try:
                    claim_meta = json.loads(claim) if claim is not None else None
                except (TypeError, ValueError):
                    claim_meta = None
                if isinstance(claim_meta, dict) and isinstance(claim_meta.get("pid"), int):
                    if _pid_alive(claim_meta["pid"]):
                        return False
                elif time.time() - claim_info.st_mtime <= _STALE_SECONDS:
                    return False
                os.unlink(".reclaim", dir_fd=lock_fd)
            parked = f"{self.path.name}.stale.{os.getpid()}.{time.time_ns()}"
            os.rename(
                self.path.name,
                parked,
                src_dir_fd=parent.fd,
                dst_dir_fd=parent.fd,
            )
            parked_info = os.stat(parked, dir_fd=parent.fd, follow_symlinks=False)
            if identity != (parked_info.st_dev, parked_info.st_ino):
                return False
            with contextlib.suppress(OSError):
                os.unlink("owner.json", dir_fd=lock_fd)
            with contextlib.suppress(OSError):
                os.unlink(".reclaim", dir_fd=lock_fd)
            os.rmdir(parked, dir_fd=parent.fd)
            parent.validate()
            return True
        except (OSError, ValueError):
            return False
        finally:
            if lock_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(lock_fd)

    def _try_acquire_fallback(self) -> bool:
        try:
            _fallback_directory(self.path.parent, create_missing=False)
        except ValueError:
            return False
        for _attempt in range(2):
            try:
                os.mkdir(self.path, 0o700)
            except FileExistsError:
                if not self._reclaim_if_stale_fallback():
                    return False
                continue
            except OSError:
                return False
            return self._initialize_fallback_owner()
        return False

    def _initialize_fallback_owner(self) -> bool:
        token = secrets.token_hex(16)
        identity: tuple[int, int] | None = None
        owner_written = False
        try:
            info = self.path.lstat()
            if not stat.S_ISDIR(info.st_mode) or _entry_is_alias(self.path):
                raise OSError("lock directory is unsafe")
            identity = (info.st_dev, info.st_ino)
            raw = json.dumps(
                {"pid": os.getpid(), "ts": time.time(), "token": token},
                sort_keys=True,
            ).encode("utf-8")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self._meta, flags, 0o600)
            try:
                _write_all(fd, raw)
                os.fsync(fd)
                owner_written = True
            finally:
                os.close(fd)
            live = self.path.lstat()
            if identity != (live.st_dev, live.st_ino):
                raise OSError("lock directory identity changed")
            self._token = token
            self._identity = identity
            return True
        except OSError:
            if owner_written:
                self._remove_fallback_if_identity(identity, token)
            elif identity is not None:
                with contextlib.suppress(OSError):
                    live = self.path.lstat()
                    if identity == (live.st_dev, live.st_ino):
                        os.rmdir(self.path)
            return False

    def _read_fallback_owner(self) -> dict | None:
        raw = _read_bounded_text(self._meta)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _remove_stale_fallback_claim(claim: Path) -> bool:
        """Best-effort convergence for the Windows compatibility protocol."""

        try:
            before = claim.lstat()
            raw = _read_bounded_text(claim)
            try:
                value = json.loads(raw) if raw is not None else None
            except (TypeError, ValueError):
                value = None
            if isinstance(value, dict) and isinstance(value.get("pid"), int):
                stale = not _pid_alive(value["pid"])
            else:
                stale = time.time() - before.st_mtime > _STALE_SECONDS
            after = claim.lstat()
            if not stale or _exact_identity(before) != _exact_identity(after):
                return False
            claim.unlink()
            return True
        except OSError:
            return False

    def _reclaim_if_stale_fallback(self) -> bool:
        claim = self.path / ".reclaim"
        claim_info: os.stat_result | None = None
        try:
            info = self.path.lstat()
            if not stat.S_ISDIR(info.st_mode) or _entry_is_alias(self.path):
                return False
            identity = (info.st_dev, info.st_ino)
            owner = self._read_fallback_owner()
            if owner is not None and isinstance(owner.get("pid"), int):
                stale = not _pid_alive(owner["pid"])
            else:
                stale = time.time() - info.st_mtime > _STALE_SECONDS
            if not stale:
                return False
            claim_token = secrets.token_hex(16)
            claim_raw = json.dumps(
                {"pid": os.getpid(), "ts": time.time(), "token": claim_token},
                sort_keys=True,
            ).encode("utf-8")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            for attempt in range(2):
                try:
                    claim_fd = os.open(claim, flags, 0o600)
                    break
                except FileExistsError:
                    if attempt or not self._remove_stale_fallback_claim(claim):
                        return False
            else:
                return False
            try:
                _write_all(claim_fd, claim_raw)
                os.fsync(claim_fd)
                claim_info = os.fstat(claim_fd)
            finally:
                os.close(claim_fd)
            live = self.path.lstat()
            if identity != (live.st_dev, live.st_ino):
                return False
            if not set(os.listdir(self.path)).issubset({"owner.json", ".reclaim"}):
                return False
            parked = self.path.with_name(f"{self.path.name}.stale.{os.getpid()}.{time.time_ns()}")
            os.rename(self.path, parked)
            parked_info = parked.lstat()
            if identity != (parked_info.st_dev, parked_info.st_ino):
                return False
            with contextlib.suppress(OSError):
                (parked / "owner.json").unlink()
            with contextlib.suppress(OSError):
                (parked / ".reclaim").unlink()
            os.rmdir(parked)
            return True
        except OSError:
            return False
        finally:
            if claim_info is not None:
                with contextlib.suppress(OSError):
                    current = claim.lstat()
                    if (current.st_dev, current.st_ino) == (
                        claim_info.st_dev,
                        claim_info.st_ino,
                    ):
                        claim.unlink()

    @staticmethod
    def _force_remove_dir(d: Path) -> None:
        with contextlib.suppress(OSError):
            (d / "owner.json").unlink()
        with contextlib.suppress(OSError):
            os.rmdir(d)

    def _force_remove(self) -> None:
        self._release_owned()

    def _remove_fallback_if_identity(
        self,
        identity: tuple[int, int] | None,
        token: str | None,
    ) -> None:
        try:
            info = self.path.lstat()
            if identity is not None and identity != (info.st_dev, info.st_ino):
                return
            owner = self._read_fallback_owner()
            if token is not None and (owner is None or owner.get("token") != token):
                return
            if os.path.lexists(self.path / ".reclaim"):
                return
            with contextlib.suppress(OSError):
                self._meta.unlink()
            os.rmdir(self.path)
        except (OSError, ValueError):
            return

    def _release_owned(self) -> None:
        token = self._token
        identity = self._identity
        parent = self._parent_pin
        lock_fd = self._lock_fd
        self._token = None
        self._identity = None
        self._parent_pin = None
        self._lock_fd = -1
        if token is None or identity is None:
            return
        if parent is None or lock_fd < 0:
            self._remove_fallback_if_identity(identity, token)
            return
        try:
            parent.validate()
            live = os.stat(self.path.name, dir_fd=parent.fd, follow_symlinks=False)
            opened = os.fstat(lock_fd)
            if identity != (live.st_dev, live.st_ino) or identity != (
                opened.st_dev,
                opened.st_ino,
            ):
                return
            owner = self._read_owner_at(lock_fd)
            if owner is None or owner.get("token") != token:
                return
            try:
                os.stat(".reclaim", dir_fd=lock_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                return
            os.unlink("owner.json", dir_fd=lock_fd)
            parent.validate()
            live = os.stat(self.path.name, dir_fd=parent.fd, follow_symlinks=False)
            if identity == (live.st_dev, live.st_ino):
                os.rmdir(self.path.name, dir_fd=parent.fd)
                os.fsync(parent.fd)
                parent.validate()
        except (OSError, ValueError):
            return
        finally:
            with contextlib.suppress(OSError):
                os.close(lock_fd)
            parent.close()

    @contextlib.contextmanager
    def acquire(self):
        if not self.try_acquire():
            raise RuntimeError(
                f"安装已在进行（锁 {self.path}）；若确认无安装进程，请手动删除该目录"
            )
        try:
            yield
        finally:
            self._release_owned()

    @contextlib.contextmanager
    def acquire_wait(self, *, timeout: float = 300.0, poll_interval: float = 0.05):
        """Wait for an ordinary peer to finish instead of racing its maintenance."""
        deadline = time.monotonic() + timeout
        while not self.try_acquire():
            if time.monotonic() >= deadline:
                raise RuntimeError(f"等待运行时维护锁超时：{self.path}")
            time.sleep(poll_interval)
        try:
            yield
        finally:
            self._release_owned()


@contextlib.contextmanager
def runtime_maintenance_lock():
    """Serialize install, repair and uninstall across replacement generations."""
    _ensure_maintenance_write_root()
    with FileLock(paths.maintenance_lock()).acquire_wait():
        yield
