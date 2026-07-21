"""运行时安装编排：micromamba → pinned env → pip 装 server 包 → 冒烟 → 写哨兵。"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import os
import secrets
import shutil
import stat
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

from vibecad.runtime import micromamba, paths, spec, status
from vibecad.runtime.status import Phase, RuntimeStatus

ProgressCb = Callable[[RuntimeStatus], None]
# Test seam that does not replace ``os.rename`` globally (which would change
# ``os.supports_dir_fd`` capability detection in :mod:`runtime.status`).
_rename = os.rename

_FD_EXEC_HELPER = (
    "import os,sys\n"
    "fd=int(sys.argv[1],10)\n"
    "target=sys.argv[2]\n"
    "if os.path.isabs(target) or not target.startswith(('./','../')):raise SystemExit(126)\n"
    "os.fchdir(fd)\n"
    "os.execv(target,sys.argv[2:])\n"
)


def _spawn_process(*args, **kwargs):
    return subprocess.run(*args, **kwargs)


def _expected_micromamba_sha256(subdir: str) -> str:
    raw = micromamba._fetch_text(micromamba._sha256_url(subdir))
    value = raw.split()[0].strip().lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise InstallError("micromamba checksum response is invalid")
    return value


def _download_micromamba_to_fd(url: str, file_descriptor: int) -> str:
    """Stream one download into an already-open no-follow staging file."""

    digest = hashlib.sha256()
    with urllib.request.urlopen(url) as response:  # noqa: S310 - pinned release URL
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            offset = 0
            while offset < len(chunk):
                written = os.write(file_descriptor, chunk[offset:])
                if written <= 0:
                    raise OSError("micromamba download made no progress")
                offset += written
    return digest.hexdigest()


def _sha256_fd(file_descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(file_descriptor, 0, os.SEEK_SET)
    while chunk := os.read(file_descriptor, 1 << 20):
        digest.update(chunk)
    return digest.hexdigest()


def _rename_noreplace_at(
    parent_fd: int,
    source: str,
    destination: str,
) -> bool:
    """Atomically rename within one directory without replacing destination.

    Darwin and Linux expose different libc entry points/flags.  An unsupported
    POSIX platform fails closed so the parked authority is retained.  Windows
    takes the separate path fallback, where ``os.rename`` already refuses an
    existing destination.
    """

    if sys.platform == "darwin":
        function_name = "renameatx_np"
        flag = 0x00000004  # RENAME_EXCL from <sys/stdio.h>
    elif sys.platform.startswith("linux"):
        function_name = "renameat2"
        flag = 0x00000001  # RENAME_NOREPLACE from <linux/fs.h>
    else:
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, function_name, None)
    if function is None:
        return False
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    result = function(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(destination),
        flag,
    )
    if result == 0:
        return True
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        return False
    raise OSError(error, os.strerror(error), destination)


class InstallError(RuntimeError):
    pass


def _run(
    cmd: list,
    *,
    cwd: str | None = None,
    cwd_fd: int | None = None,
    generation_guard: Callable[[], None] | None = None,
) -> None:
    """跑安装子进程；stdout/stderr 重定向到日志，绝不污染 MCP stdio（B2）。"""
    if cwd is not None and cwd_fd is not None:
        raise ValueError("cwd and cwd_fd are mutually exclusive")
    process_options = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
    }
    spawn_command = cmd
    if cwd_fd is not None:
        if sys.platform == "win32":
            raise InstallError("directory capability execution is unavailable on Windows")
        target = os.fspath(cmd[0]) if cmd else ""
        if os.path.isabs(target) or not target.startswith(("./", "../")):
            raise InstallError("directory capability execution requires a relative target")
        launcher = os.fspath(sys.executable)
        if not launcher or not os.path.isabs(launcher):
            raise InstallError("clean helper Python launcher is unavailable")
        # Popen first execs a clean Python.  Only that single-threaded helper
        # performs fchdir/exec; the multi-threaded server parent never runs a
        # Python closure between fork and exec.
        spawn_command = [
            launcher,
            "-I",
            "-B",
            "-c",
            _FD_EXEC_HELPER,
            str(cwd_fd),
            *cmd,
        ]
        process_options["pass_fds"] = (cwd_fd,)
    proc = _spawn_process(spawn_command, **process_options)
    if generation_guard is not None:
        generation_guard()
    output = proc.stdout or ""
    # ``append_install_log`` accepts a bounded record.  Preserve the useful tail
    # without letting a noisy package manager turn successful installation into
    # a logging failure.
    raw_record = f"$ {' '.join(map(str, cmd))}\n".encode("utf-8", "replace")
    raw_record += output.encode("utf-8", "replace")
    # Leave headroom for a split multibyte code point to decode as U+FFFD.
    record = (raw_record[-(63 * 1024) :] + b"\n").decode("utf-8", "replace")
    try:
        status.append_install_log(record)
    except Exception:  # noqa: BLE001 - logging must never change command semantics
        pass
    if generation_guard is not None:
        generation_guard()
    if proc.returncode != 0:
        tail = output[-2000:]
        raise InstallError(f"命令失败({proc.returncode}): {' '.join(map(str, cmd))}\n{tail}")


def _validate_owned_directory_pin(path: Path, pinned) -> None:
    """Require every VibeCAD-owned component to remain a real owned directory."""

    home = paths.vibecad_home().expanduser()
    try:
        path.relative_to(home)
    except ValueError as exc:
        raise InstallError(f"运行时目录逃逸 VIBECAD_HOME：{path}") from exc
    start = len(home.parts) - 1
    getuid = getattr(os, "geteuid", None)
    uid = getuid() if getuid is not None else None
    for fd in pinned.fds[start:]:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or (
            hasattr(info, "st_uid") and uid is not None and info.st_uid != uid
        ):
            raise InstallError(f"运行时目录不是当前用户拥有的真实目录：{path}")
    pinned.validate()


@contextlib.contextmanager
def _owned_directory(path: Path, *, create_missing: bool) -> Iterator[object | None]:
    """Pin a whole directory chain without following any component aliases."""

    try:
        if not status._secure_dir_fd_available():
            resolved = status._fallback_directory(path, create_missing=create_missing)
            yield None
            status._fallback_directory(resolved, create_missing=False)
            return
        opener = (
            status._ensure_pinned_directory if create_missing else status._open_pinned_directory
        )
        pinned = opener(path)
        try:
            _validate_owned_directory_pin(path, pinned)
            yield pinned
            _validate_owned_directory_pin(path, pinned)
        finally:
            pinned.close()
    except InstallError:
        raise
    except (OSError, ValueError) as exc:
        raise InstallError(f"运行时目录不安全或不可用：{path}") from exc


def _entry_info(parent: Path, name: str, pinned) -> os.stat_result | None:
    try:
        if pinned is None:
            return (parent / name).lstat()
        return os.stat(name, dir_fd=pinned.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _validate_regular_entry(
    parent: Path,
    name: str,
    pinned,
    *,
    required: bool,
) -> os.stat_result | None:
    """Reject aliases, devices, hard links and unowned installer binaries."""

    info = _entry_info(parent, name, pinned)
    if info is None:
        if required:
            raise InstallError(f"运行时文件未生成：{parent / name}")
        return None
    getuid = getattr(os, "geteuid", None)
    uid = getuid() if getuid is not None else None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (hasattr(info, "st_uid") and uid is not None and info.st_uid != uid)
    ):
        raise InstallError(f"运行时文件不是当前用户拥有的单一普通文件：{parent / name}")
    return info


def _open_regular_entry(parent: Path, name: str, pinned) -> tuple[int, os.stat_result]:
    """Open and bind one regular entry relative to its pinned parent."""

    expected = _validate_regular_entry(parent, name, pinned, required=True)
    assert expected is not None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    file_descriptor = -1
    try:
        if pinned is None:
            file_descriptor = os.open(parent / name, flags)
            live = (parent / name).lstat()
        else:
            pinned.validate()
            file_descriptor = os.open(name, flags, dir_fd=pinned.fd)
            live = os.stat(name, dir_fd=pinned.fd, follow_symlinks=False)
        opened = os.fstat(file_descriptor)
        expected_identity = (expected.st_dev, expected.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != expected_identity
            or (live.st_dev, live.st_ino) != expected_identity
        ):
            raise InstallError(f"运行时文件 identity changed：{parent / name}")
        return file_descriptor, opened
    except BaseException:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        raise


def _empty_directory_fd(directory_fd: int) -> None:
    """Delete one already-open directory tree without resolving path ancestors."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for name in os.listdir(directory_fd):
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            child_fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise InstallError("托管运行时子目录 identity changed")
                _empty_directory_fd(child_fd)
                live = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (live.st_dev, live.st_ino) != (before.st_dev, before.st_ino):
                    raise InstallError("托管运行时子目录 identity changed")
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _restore_parked_directory(
    parent_fd: int,
    *,
    live_name: str,
    parked_name: str,
) -> bool:
    """Restore a parked directory without replacing an unknown live entry."""

    try:
        parked = os.stat(parked_name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(parked.st_mode):
            return False
        if not _rename_noreplace_at(parent_fd, parked_name, live_name):
            return False
        restored = os.stat(live_name, dir_fd=parent_fd, follow_symlinks=False)
        if (restored.st_dev, restored.st_ino) != (parked.st_dev, parked.st_ino):
            raise InstallError("restored runtime authority identity changed")
        return True
    except OSError:
        return False


def _local_source_root() -> Path | None:
    """源码 checkout / MCPB editable install 时，找到包含当前文件的项目根。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "src" / "vibecad" / "runtime" / "installer.py"
        if (parent / "pyproject.toml").is_file() and candidate.is_file():
            try:
                if candidate.resolve() == here:
                    return parent
            except OSError:
                continue
    return None


def _pip_spec() -> str:
    """目标 server 必须与 bootstrap 同源同版本，禁止漂移到 PyPI latest。"""
    raw = os.environ.get("VIBECAD_PIP_SPEC")
    if raw:
        return os.path.abspath(raw) if os.path.exists(raw) else raw
    if source := _local_source_root():
        return str(source)
    return f"vibecad=={spec.VIBECAD_VERSION}"


class RuntimeInstaller:
    def __init__(self, on_progress: ProgressCb | None = None):
        self._cb = on_progress or (lambda s: None)
        self._runtime_pin = None
        self._runtime_identity: tuple[int, int] | None = None
        self._micromamba_digests: dict[Path, str] = {}

    def _validate_runtime_generation(self) -> None:
        """Recheck the runtime root held for this maintenance transaction."""

        if self._runtime_identity is None:
            return
        if self._runtime_pin is None:
            root = status._fallback_directory(paths.runtime_root(), create_missing=False)
            info = root.lstat()
        else:
            self._runtime_pin.validate()
            info = os.fstat(self._runtime_pin.fd)
            live = paths.runtime_root().lstat()
            if (live.st_dev, live.st_ino) != (info.st_dev, info.st_ino):
                raise InstallError("runtime generation identity changed")
        if (info.st_dev, info.st_ino) != self._runtime_identity:
            raise InstallError("runtime generation identity changed")

    def is_ready(self) -> bool:
        if not status.runtime_ready():
            return False
        # A legacy managed receipt proves ownership but not that this process has
        # performed the S3 adoption probe. Verify before installer short-circuit.
        if (
            paths.user_override_env() is None
            and paths.active_runtime_prefix() == paths.legacy_env_prefix()
            and paths.ready_sentinel() != paths.external_runtime_receipt()
        ):
            try:
                evidence = status.capture_runtime_generation_evidence(paths.legacy_env_prefix())
            except ValueError:
                return False
            if not status.verify_runtime_generation(evidence):
                return False
            try:
                self._revalidate_evidence(evidence)
            except InstallError:
                return False
            return True
        return True

    def _emit(self, phase: Phase, percent: float, message: str) -> None:
        s = RuntimeStatus(phase=phase, percent=percent, message=message)
        self._validate_runtime_generation()
        status.write_status(s)  # 跨进程可见（M5）
        self._validate_runtime_generation()
        self._cb(s)

    def install(self) -> None:
        # The stable home-level lock remains present while ``runtime`` is rebuilt
        # or removed, so an older uninstall can never observe/delete this install.
        with status.runtime_maintenance_lock():
            self._install_locked()

    def _install_locked(self) -> None:
        if self.is_ready():
            self._emit(Phase.READY, 100.0, "运行时已就绪")
            return
        with status._pinned_runtime_write_root() as runtime_pin:
            self._runtime_pin = runtime_pin
            root_info = (
                paths.runtime_root().lstat() if runtime_pin is None else os.fstat(runtime_pin.fd)
            )
            self._runtime_identity = (root_info.st_dev, root_info.st_ino)
            try:
                with status.FileLock(paths.install_lock()).acquire():
                    self._validate_runtime_generation()
                    # 两个 bootstrap 进程可能同时在旧 receipt 上起步；后来拿锁者必须二检，
                    # 避免前一个刚完成同步后又重复 pip/建 env。
                    if self.is_ready():
                        self._emit(Phase.READY, 100.0, "运行时已就绪")
                        return
                    try:
                        self._do_install()
                        self._validate_runtime_generation()
                    except Exception as exc:  # noqa: BLE001
                        s = RuntimeStatus(
                            phase=Phase.FAILED,
                            percent=0.0,
                            message="安装失败",
                            error=str(exc),
                        )
                        try:
                            self._validate_runtime_generation()
                            status.write_status(s)
                            self._validate_runtime_generation()
                        except Exception:  # noqa: BLE001 - never publish to a changed root
                            pass
                        self._cb(s)
                        raise InstallError(str(exc)) from exc
            finally:
                try:
                    self._validate_runtime_generation()
                finally:
                    self._runtime_pin = None
                    self._runtime_identity = None

    def _write_sentinel(
        self,
        prefix: Path,
        evidence: status.RuntimeGenerationEvidence,
    ) -> None:
        self._validate_runtime_generation()
        status.write_managed_runtime_receipt(prefix, evidence=evidence)
        self._validate_runtime_generation()

    def _write_external_receipt(
        self,
        prefix: Path,
        evidence: status.RuntimeGenerationEvidence,
    ) -> None:
        self._validate_runtime_generation()
        status.write_external_runtime_receipt(prefix, evidence=evidence)
        self._validate_runtime_generation()

    def _capture_evidence(
        self,
        prefix: Path,
    ) -> status.RuntimeGenerationEvidence:
        self._validate_runtime_generation()
        evidence = status.capture_runtime_generation_evidence(prefix)
        self._validate_runtime_generation()
        return evidence

    def _capture_existing_evidence(
        self,
        prefix: Path,
    ) -> status.RuntimeGenerationEvidence | None:
        try:
            return self._capture_evidence(prefix)
        except ValueError:
            return None

    def _revalidate_evidence(
        self,
        evidence: status.RuntimeGenerationEvidence,
    ) -> None:
        """Prove a verified interpreter is still the same exact generation."""

        self._validate_runtime_generation()
        prefix_pin = None
        try:
            if status._secure_dir_fd_available():
                prefix_pin = status._open_pinned_directory(evidence.prefix)
            status._validate_runtime_generation_evidence(
                evidence,
                evidence.prefix,
                prefix_pin,
            )
            if prefix_pin is not None:
                prefix_pin.validate()
            self._validate_runtime_generation()
        except (OSError, ValueError) as exc:
            raise InstallError("verified runtime generation identity changed") from exc
        finally:
            if prefix_pin is not None:
                prefix_pin.close()

    def _verify_generation_or_raise(self, prefix: Path, message: str):
        evidence = self._capture_evidence(prefix)
        if not status.verify_runtime_generation(evidence):
            raise InstallError(message)
        self._revalidate_evidence(evidence)
        return evidence

    def _ensure_current_layout(self) -> None:
        """Create only fixed managed directories through no-follow pinned parents."""

        runtime = paths.runtime_root()
        required = (
            runtime / "bin",
            paths.mamba_root_prefix(),
            paths.mamba_root_prefix() / "envs",
        )
        for directory in required:
            self._validate_runtime_generation()
            with _owned_directory(directory, create_missing=True):
                self._validate_runtime_generation()

    def _ensure_legacy_layout(self) -> None:
        for directory in (
            paths.legacy_micromamba_path().parent,
            paths.legacy_mamba_root_prefix(),
            paths.legacy_env_prefix().parent,
        ):
            self._validate_runtime_generation()
            with _owned_directory(directory, create_missing=True):
                self._validate_runtime_generation()

    def _ensure_micromamba(self, destination: Path) -> Path:
        """Download and publish through one pinned parent-directory capability."""

        self._validate_runtime_generation()
        subdir = micromamba.platform.conda_subdir()
        if not status._secure_dir_fd_available():
            if sys.platform != "win32":
                raise InstallError("pinned micromamba download is unavailable")
            result = micromamba.ensure_micromamba(destination, subdir=subdir)
            with _owned_directory(destination.parent, create_missing=False) as parent_pin:
                source_fd, _ = _open_regular_entry(
                    destination.parent,
                    destination.name,
                    parent_pin,
                )
                try:
                    self._micromamba_digests[destination] = _sha256_fd(source_fd)
                finally:
                    os.close(source_fd)
            return result

        expected_digest = _expected_micromamba_sha256(subdir)
        part_name = f"{destination.name}.part"
        with _owned_directory(destination.parent, create_missing=True) as parent_pin:
            assert parent_pin is not None
            current = _entry_info(destination.parent, destination.name, parent_pin)
            if current is not None:
                source_fd, _ = _open_regular_entry(
                    destination.parent,
                    destination.name,
                    parent_pin,
                )
                try:
                    if _sha256_fd(source_fd) == expected_digest:
                        self._micromamba_digests[destination] = expected_digest
                        return destination
                finally:
                    os.close(source_fd)

            if _entry_info(destination.parent, part_name, parent_pin) is not None:
                _validate_regular_entry(
                    destination.parent,
                    part_name,
                    parent_pin,
                    required=True,
                )
                parent_pin.validate()
                os.unlink(part_name, dir_fd=parent_pin.fd)

            staging_fd = -1
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                staging_fd = os.open(part_name, flags, 0o600, dir_fd=parent_pin.fd)
                actual_digest = _download_micromamba_to_fd(
                    micromamba.download_url(subdir),
                    staging_fd,
                )
                if actual_digest != expected_digest:
                    raise micromamba.ChecksumError(f"micromamba sha256 不符（subdir={subdir}）")
                os.fchmod(staging_fd, 0o700)
                os.fsync(staging_fd)
                staged = os.fstat(staging_fd)
                live_staged = os.stat(part_name, dir_fd=parent_pin.fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(staged.st_mode)
                    or staged.st_nlink != 1
                    or (staged.st_dev, staged.st_ino) != (live_staged.st_dev, live_staged.st_ino)
                ):
                    raise InstallError("micromamba staging identity changed")
                self._validate_runtime_generation()
                parent_pin.validate()
                os.replace(
                    part_name,
                    destination.name,
                    src_dir_fd=parent_pin.fd,
                    dst_dir_fd=parent_pin.fd,
                )
                os.fsync(parent_pin.fd)
                published = os.stat(
                    destination.name,
                    dir_fd=parent_pin.fd,
                    follow_symlinks=False,
                )
                if (published.st_dev, published.st_ino) != (staged.st_dev, staged.st_ino):
                    raise InstallError("micromamba publish identity changed")
                parent_pin.validate()
                self._validate_runtime_generation()
                self._micromamba_digests[destination] = expected_digest
                return destination
            finally:
                if staging_fd >= 0:
                    os.close(staging_fd)
                with contextlib.suppress(OSError):
                    os.unlink(part_name, dir_fd=parent_pin.fd)

    def _validate_managed_env(self, env: Path) -> None:
        if env not in {paths.env_prefix(), paths.legacy_env_prefix()}:
            raise InstallError(f"拒绝使用非托管运行时前缀：{env}")
        self._validate_runtime_generation()
        with _owned_directory(env, create_missing=False):
            self._validate_runtime_generation()

    def _prepare_empty_managed_env(self, env: Path) -> tuple[int, int]:
        """Create and bind the exact empty prefix before micromamba starts."""

        if env != paths.env_prefix():
            raise InstallError(f"拒绝创建非当前托管运行时前缀：{env}")
        with _owned_directory(env.parent, create_missing=False) as parent_pin:
            if parent_pin is None:
                env.mkdir(mode=0o700)
                info = status._fallback_directory(env, create_missing=False).lstat()
            else:
                parent_pin.validate()
                if _entry_info(env.parent, env.name, parent_pin) is not None:
                    raise InstallError(f"micromamba create 前托管前缀仍然存在：{env}")
                os.mkdir(env.name, 0o700, dir_fd=parent_pin.fd)
                info = os.stat(env.name, dir_fd=parent_pin.fd, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    raise InstallError("managed env creation did not produce a directory")
                parent_pin.validate()
        self._validate_runtime_generation()
        return info.st_dev, info.st_ino

    def _stage_micromamba_runner(
        self,
        source: Path,
        target_parent: Path,
        target_parent_pin,
    ) -> tuple[str, tuple[int, int]]:
        """Copy one checksum-bound runner into the already-pinned command parent."""

        expected_digest = self._micromamba_digests.get(source)
        if expected_digest is None:
            raise InstallError("micromamba generation has no validated digest")
        runner_name = f".vibecad-runner-{os.getpid()}-{secrets.token_hex(8)}"
        source_fd = -1
        runner_fd = -1
        completed = False
        try:
            with _owned_directory(source.parent, create_missing=False) as source_parent_pin:
                source_fd, source_info = _open_regular_entry(
                    source.parent,
                    source.name,
                    source_parent_pin,
                )
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                runner_fd = os.open(
                    runner_name,
                    flags,
                    0o700,
                    dir_fd=target_parent_pin.fd,
                )
                digest = hashlib.sha256()
                while chunk := os.read(source_fd, 1 << 20):
                    digest.update(chunk)
                    offset = 0
                    while offset < len(chunk):
                        written = os.write(runner_fd, chunk[offset:])
                        if written <= 0:
                            raise OSError("micromamba runner copy made no progress")
                        offset += written
                if digest.hexdigest() != expected_digest:
                    raise InstallError("validated micromamba generation identity changed")
                os.fchmod(runner_fd, 0o700)
                os.fsync(runner_fd)
                source_after = os.fstat(source_fd)
                if (source_after.st_dev, source_after.st_ino) != (
                    source_info.st_dev,
                    source_info.st_ino,
                ):
                    raise InstallError("validated micromamba generation identity changed")
                if source_parent_pin is not None:
                    source_parent_pin.validate()
            runner = os.fstat(runner_fd)
            live = os.stat(
                runner_name,
                dir_fd=target_parent_pin.fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(runner.st_mode)
                or runner.st_nlink != 1
                or (runner.st_dev, runner.st_ino) != (live.st_dev, live.st_ino)
            ):
                raise InstallError("private micromamba runner identity changed")
            target_parent_pin.validate()
            self._validate_runtime_generation()
            completed = True
            return runner_name, (runner.st_dev, runner.st_ino)
        finally:
            if runner_fd >= 0:
                os.close(runner_fd)
            if source_fd >= 0:
                os.close(source_fd)
            if not completed:
                with contextlib.suppress(OSError):
                    os.unlink(runner_name, dir_fd=target_parent_pin.fd)

    def _run_micromamba_command(
        self,
        micromamba_path: Path,
        root: Path,
        env: Path,
        arguments: list[str],
        *,
        expected_env_identity: tuple[int, int],
    ) -> None:
        if root != env.parent.parent or (root, env) not in {
            (paths.mamba_root_prefix(), paths.env_prefix()),
            (paths.legacy_mamba_root_prefix(), paths.legacy_env_prefix()),
        }:
            raise InstallError("拒绝在非固定托管前缀中执行 micromamba")
        if not status._secure_dir_fd_available():
            # Explicitly weaker Windows compatibility path: junction/reparse checks
            # happen before and after, but CPython cannot bind child cwd by dir-fd.
            if sys.platform != "win32":
                raise InstallError("directory capability execution is unavailable")
            _run(
                [
                    str(micromamba_path),
                    arguments[0],
                    "-r",
                    str(root),
                    "-p",
                    str(env),
                    *arguments[1:],
                ],
                generation_guard=self._validate_runtime_generation,
            )
            return

        with _owned_directory(env.parent, create_missing=False) as env_parent_pin:
            with _owned_directory(env, create_missing=False) as env_pin:
                assert env_parent_pin is not None and env_pin is not None
                env_info = os.fstat(env_pin.fd)
                if (env_info.st_dev, env_info.st_ino) != expected_env_identity:
                    raise InstallError("managed env generation identity changed")
                runner_name, runner_identity = self._stage_micromamba_runner(
                    micromamba_path,
                    env.parent,
                    env_parent_pin,
                )
                try:
                    env_parent_pin.validate()
                    env_pin.validate()
                    _run(
                        [
                            f"../{runner_name}",
                            arguments[0],
                            "-r",
                            "../..",
                            "-p",
                            "./",
                            *arguments[1:],
                        ],
                        cwd_fd=env_pin.fd,
                        generation_guard=self._validate_runtime_generation,
                    )
                    env_parent_pin.validate()
                    env_pin.validate()
                    final = os.fstat(env_pin.fd)
                    if (final.st_dev, final.st_ino) != expected_env_identity:
                        raise InstallError("managed env generation identity changed")
                finally:
                    with contextlib.suppress(OSError):
                        live_runner = os.stat(
                            runner_name,
                            dir_fd=env_parent_pin.fd,
                            follow_symlinks=False,
                        )
                        if (live_runner.st_dev, live_runner.st_ino) == runner_identity:
                            os.unlink(runner_name, dir_fd=env_parent_pin.fd)

    def _install_server_package(
        self,
        micromamba_path: Path,
        root: Path,
        env: Path,
        *,
        expected_env_identity: tuple[int, int],
    ) -> None:
        # Windows 直接启动 conda Python 可能缺 Library/bin 的 DLL/PATH 注入；统一经
        # micromamba run 进入环境，和首次安装的跨平台语义一致。
        if (micromamba_path, root, env) == (
            paths.micromamba_path(),
            paths.mamba_root_prefix(),
            paths.env_prefix(),
        ):
            self._ensure_current_layout()
        elif (micromamba_path, root, env) == (
            paths.legacy_micromamba_path(),
            paths.legacy_mamba_root_prefix(),
            paths.legacy_env_prefix(),
        ):
            self._ensure_legacy_layout()
        else:
            raise InstallError("拒绝在非固定托管前缀中执行 pip 同步")
        self._run_micromamba_command(
            micromamba_path,
            root,
            env,
            [
                "run",
                "python",
                "-m",
                "pip",
                "install",
                "--upgrade",
                _pip_spec(),
            ],
            expected_env_identity=expected_env_identity,
        )

    def _remove_managed_env(self, env: Path) -> None:
        """Identity-bind, park and delete only one proven managed environment."""

        current = paths.env_prefix()
        legacy = paths.legacy_env_prefix()
        if paths.user_override_env() is not None or env not in {current, legacy}:
            raise InstallError(f"拒绝删除非托管运行时前缀：{env}")
        if not os.path.lexists(env):
            return
        self._validate_runtime_generation()
        if not status._secure_dir_fd_available():
            self._remove_managed_env_fallback(env, legacy=env == legacy)
            self._validate_runtime_generation()
            return

        try:
            parent_pin = status._open_pinned_directory(env.parent)
            env_pin = status._open_pinned_directory(env)
        except (OSError, ValueError) as exc:
            raise InstallError(f"拒绝删除不安全的托管运行时前缀：{env}") from exc
        parked = f".{env.name}.remove-{os.getpid()}-{secrets.token_hex(8)}"
        parked_active = False
        try:
            _validate_owned_directory_pin(env.parent, parent_pin)
            _validate_owned_directory_pin(env, env_pin)
            self._validate_runtime_generation()
            if env == legacy:
                receipt = status.managed_legacy_receipt(env)
                env_pin.validate()
                parent_pin.validate()
                if receipt is None:
                    raise InstallError(f"拒绝删除无有效 ownership receipt 的 legacy 前缀：{env}")
            before = os.fstat(env_pin.fd)
            live = os.stat(env.name, dir_fd=parent_pin.fd, follow_symlinks=False)
            if not stat.S_ISDIR(live.st_mode) or (live.st_dev, live.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise InstallError("托管运行时前缀 identity changed")
            if _entry_info(env.parent, parked, parent_pin) is not None:
                raise InstallError("托管运行时临时删除路径冲突")
            parent_pin.validate()
            if not _rename_noreplace_at(parent_pin.fd, env.name, parked):
                raise InstallError("托管运行时临时删除路径冲突")
            parked_active = True
            parked_info = os.stat(parked, dir_fd=parent_pin.fd, follow_symlinks=False)
            if (parked_info.st_dev, parked_info.st_ino) != (before.st_dev, before.st_ino):
                raise InstallError("托管运行时 park identity changed")
            parent_pin.validate()
            self._validate_runtime_generation()
            _empty_directory_fd(env_pin.fd)
            parked_info = os.stat(parked, dir_fd=parent_pin.fd, follow_symlinks=False)
            if (parked_info.st_dev, parked_info.st_ino) != (before.st_dev, before.st_ino):
                raise InstallError("托管运行时 park identity changed")
            os.rmdir(parked, dir_fd=parent_pin.fd)
            parked_active = False
            os.fsync(parent_pin.fd)
            parent_pin.validate()
            self._validate_runtime_generation()
        except BaseException as exc:
            restored = False
            if parked_active:
                restored = _restore_parked_directory(
                    parent_pin.fd,
                    live_name=env.name,
                    parked_name=parked,
                )
                if not restored:
                    exc.add_note(f"parked runtime authority retained as {env.parent / parked}")
            if isinstance(exc, InstallError):
                raise
            if isinstance(exc, (OSError, ValueError)):
                raise InstallError(f"托管运行时安全删除失败：{env}") from exc
            raise
        finally:
            env_pin.close()
            parent_pin.close()

    def _remove_managed_env_fallback(self, env: Path, *, legacy: bool) -> None:
        """Windows compatibility path when CPython lacks the needed dir-fd APIs."""

        try:
            if sys.platform != "win32":
                raise InstallError("secure no-replace directory removal is unavailable")
            parent = status._fallback_directory(env.parent, create_missing=False)
            target = status._fallback_directory(env, create_missing=False)
            if legacy and status.managed_legacy_receipt(env) is None:
                raise InstallError(f"拒绝删除无有效 ownership receipt 的 legacy 前缀：{env}")
            before = target.lstat()
            parked = parent / f".{env.name}.remove-{os.getpid()}-{secrets.token_hex(8)}"
            if os.path.lexists(parked):
                raise InstallError("托管运行时临时删除路径冲突")
            _rename(env, parked)
            try:
                live = parked.lstat()
                if (live.st_dev, live.st_ino) != (before.st_dev, before.st_ino):
                    raise InstallError("托管运行时 park identity changed")
                shutil.rmtree(parked)
            except BaseException:
                if os.path.lexists(parked):
                    with contextlib.suppress(OSError):
                        # Windows ``os.rename`` is atomic and never replaces an
                        # existing destination, so concurrent live authority wins.
                        _rename(parked, env)
                raise
            status._fallback_directory(parent, create_missing=False)
        except InstallError:
            raise
        except (OSError, ValueError) as exc:
            raise InstallError(f"托管运行时安全删除失败：{env}") from exc

    def _reuse_external(self, prefix: Path, *, label: str) -> bool:
        self._emit(Phase.VERIFYING, 95.0, f"校验{label} FreeCAD env")
        evidence = self._capture_evidence(prefix)
        if not status.verify_runtime_generation(evidence):
            return False
        self._validate_runtime_generation()
        self._write_external_receipt(prefix, evidence)
        self._emit(Phase.READY, 100.0, f"运行时就绪（只读复用{label} env）")
        return True

    def _reuse_managed_legacy(self, prefix: Path) -> bool:
        evidence = self._capture_evidence(prefix)
        if status.verify_runtime_generation(evidence):
            self._revalidate_evidence(evidence)
            self._emit(Phase.READY, 100.0, "运行时就绪（原位复用 legacy FreeCAD）")
            return True
        self._validate_runtime_generation()
        if status.engine_compatible_generation(evidence):
            self._emit(
                Phase.INSTALLING_PIP,
                80.0,
                f"同步 server 到 VibeCAD v{spec.VIBECAD_VERSION}（复用 legacy FreeCAD）",
            )
            mm = paths.legacy_micromamba_path()
            root = paths.legacy_mamba_root_prefix()
            self._ensure_legacy_layout()
            self._ensure_micromamba(mm)
            self._install_server_package(
                mm,
                root,
                prefix,
                expected_env_identity=evidence.prefix_identity,
            )
            self._emit(Phase.VERIFYING, 95.0, "验证 legacy FreeCAD 与 server 版本")
            verified = self._verify_generation_or_raise(
                prefix,
                f"legacy FreeCAD env 中的 vibecad 未能同步到 v{spec.VIBECAD_VERSION}"
                "（详见 install.log）",
            )
            # This is an owned managed env, so advancing its server receipt is
            # authorized. No move/rename is ever attempted.
            self._write_sentinel(prefix, verified)
            self._emit(Phase.READY, 100.0, "运行时就绪（legacy FreeCAD 已同步 server）")
            return True
        # A canonical managed receipt plus fixed safe prefix proves ownership.
        # Removing an unhealthy owned legacy env is allowed; unknown siblings stay.
        self._remove_managed_env(prefix)
        return False

    def _try_legacy_runtime(self) -> bool:
        legacy = paths.legacy_env_prefix()
        if not os.path.lexists(legacy):
            return False
        if legacy.is_symlink():
            return False
        if status.legacy_external_receipt(legacy) is not None:
            return self._reuse_external(legacy, label="legacy")
        if status.managed_legacy_receipt(legacy) is not None:
            return self._reuse_managed_legacy(legacy)
        # Ambiguous/unowned legacy content is never modified or deleted.
        return False

    def _do_install(self) -> None:
        # M-B：override 优先——用户用 VIBECAD_FREECAD_ENV 指定现成 env 时只校验复用、绝不自建，
        # 保证「被校验解释器 == 写哨兵前缀 == launcher re-exec 进入的解释器」三者一致。
        if paths.user_override_env() is not None:
            override = paths.user_override_env()
            assert override is not None
            if not self._reuse_external(override, label="自带"):
                raise InstallError(
                    "VIBECAD_FREECAD_ENV 指定的 env 缺 FreeCAD/mcp/vibecad，"
                    f"或其中 vibecad 与当前 v{spec.VIBECAD_VERSION} 不一致；"
                    "不会自动改写用户 env，请手动安装匹配版本（详见 install.log）"
                )
            return

        root, env, mm = paths.mamba_root_prefix(), paths.env_prefix(), paths.micromamba_path()
        if os.path.lexists(env):
            # receipt 缺失/损坏也不等于 2-3GB 引擎损坏。精确验证通过时只补 receipt；
            # FreeCAD 健康但 server 旧/缺失时只同步 pip 包。
            evidence = self._capture_existing_evidence(env)
            if evidence is not None and status.verify_runtime_generation(evidence):
                self._validate_runtime_generation()
                self._write_sentinel(env, evidence)
                self._emit(Phase.READY, 100.0, "运行时就绪（已补全版本凭据）")
                return
            self._validate_runtime_generation()
            if evidence is not None and status.engine_compatible_generation(evidence):
                self._emit(
                    Phase.INSTALLING_PIP,
                    80.0,
                    f"同步 server 到 VibeCAD v{spec.VIBECAD_VERSION}（复用现有 FreeCAD）",
                )
                self._ensure_current_layout()
                self._ensure_micromamba(mm)
                self._install_server_package(
                    mm,
                    root,
                    env,
                    expected_env_identity=evidence.prefix_identity,
                )
                self._emit(Phase.VERIFYING, 95.0, "验证 FreeCAD 与 server 版本")
                verified = self._verify_generation_or_raise(
                    env,
                    f"现有 FreeCAD env 中的 vibecad 未能同步到 v{spec.VIBECAD_VERSION}"
                    "（详见 install.log）",
                )
                self._write_sentinel(env, verified)
                self._emit(Phase.READY, 100.0, "运行时就绪（已复用 FreeCAD 并同步 server）")
                return

            # micromamba create 不允许覆盖 existing prefix。确认是固定托管前缀后先删，
            # 其他 VIBECAD_HOME 内容（日志、views、micromamba、自定义文件）全部保留。
            self._emit(Phase.CREATING_ENV, 15.0, "现有 FreeCAD env 不健康，安全重建托管前缀")
            self._remove_managed_env(env)
        elif self._try_legacy_runtime():
            return

        self._ensure_current_layout()
        self._emit(Phase.DOWNLOADING_MICROMAMBA, 5.0, "获取 micromamba")
        self._ensure_micromamba(mm)
        self._emit(Phase.CREATING_ENV, 20.0, "创建环境并解析 FreeCAD（约 2-3GB，请耐心）")
        env_identity = self._prepare_empty_managed_env(env)
        self._run_micromamba_command(
            mm,
            root,
            env,
            [
                "create",
                "-y",
                "-c",
                "conda-forge",
                "--override-channels",
                spec.PYTHON_PIN,
                spec.FREECAD_PIN,
            ],
            expected_env_identity=env_identity,
        )
        # A successful exit code is not enough: the exact fixed prefix must now
        # be a real owned directory in the still-pinned runtime generation.
        self._validate_managed_env(env)
        self._emit(Phase.INSTALLING_PIP, 80.0, "安装 server 依赖")
        self._install_server_package(
            mm,
            root,
            env,
            expected_env_identity=env_identity,
        )
        self._emit(Phase.VERIFYING, 95.0, "冒烟验证 import FreeCAD + vibecad.server")
        # M-B/m-4：统一 active + 验 server 可起
        verified = self._verify_generation_or_raise(
            env,
            "env 已建但 FreeCAD/server import 或 vibecad 版本校验失败（详见 install.log）",
        )
        self._write_sentinel(env, verified)
        self._emit(Phase.READY, 100.0, "运行时就绪")
