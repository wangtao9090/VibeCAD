"""Exact macOS user-FreeCAD pilot discovery and reversible addon ownership."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import plistlib
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from vibecad import __version__

_BUNDLE_ID = "org.freecad.FreeCAD"
_FREECAD_VERSION = "1.1.3"
_PYTHON_VERSION = "3.11"
_PYSIDE_VERSION = "6.8.3"
_BRIDGE_PROTOCOL = "vibecad-freecad-bridge"
_BRIDGE_PROTOCOL_VERSION = 1
_INSTALL_KIND = "vibecad-freecad-addon"
_RECEIPT_NAME = ".vibecad-install.json"
_CONFIG_NAME = "bridge.json"
_LOCK_NAME = ".vibecad-addon.lock"
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "receipt_id",
        "target",
        "host_app",
        "host_fingerprint",
        "host_version",
        "bridge_protocol",
        "package_version",
        "files",
    }
)
_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "protocol",
        "protocol_version",
        "package_version",
        "python_path",
        "python_sha256",
        "python_target",
    }
)


class ExternalFreeCADError(RuntimeError):
    """Actionable but non-reflective external-host failure."""


@dataclass(frozen=True, slots=True)
class ExternalFreeCADHost:
    app_path: Path
    executable: Path
    freecad_version: str
    python_version: str
    pyside_version: str
    fingerprint: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _safe_directory(path: Path, *, create: bool = False) -> Path:
    try:
        if create and not os.path.lexists(path):
            parent = _safe_directory(path.parent, create=True)
            if parent != path.parent:
                raise OSError
            path.mkdir(mode=0o700)
        canonical = path.resolve(strict=True)
        info = path.lstat()
        if (
            path != canonical
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise OSError
        return canonical
    except OSError:
        raise ExternalFreeCADError("the per-user FreeCAD directory is unsafe") from None


def _safe_regular(
    path: Path,
    *,
    executable: bool = False,
    allow_group_write: bool = False,
) -> os.stat_result:
    try:
        if path != path.resolve(strict=True):
            raise OSError
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.getuid()}
            or stat.S_IMODE(info.st_mode) & (0o002 if allow_group_write else 0o022)
            or (executable and not os.access(path, os.X_OK))
        ):
            raise OSError
        return info
    except OSError:
        raise ExternalFreeCADError("the FreeCAD pilot identity is unsafe") from None


def _stable_digest(path: Path, *, allow_group_write: bool = False) -> str:
    before = _safe_regular(path, allow_group_write=allow_group_write)
    digest = _sha256(path)
    after = _safe_regular(path, allow_group_write=allow_group_write)
    if before != after:
        raise ExternalFreeCADError("the FreeCAD pilot identity changed during inspection")
    return digest


def _has_private_user_ancestor(path: Path) -> bool:
    for ancestor in path.parents:
        try:
            info = ancestor.lstat()
            if ancestor != ancestor.resolve(strict=True) or not stat.S_ISDIR(info.st_mode):
                return False
        except OSError:
            return False
        if info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0:
            return True
    return False


def inspect_freecad_app(value: object) -> ExternalFreeCADHost:
    if type(value) not in {str, type(Path("/"))}:
        raise ExternalFreeCADError("--freecad-app requires an absolute .app path")
    app = Path(value)
    try:
        if (
            not app.is_absolute()
            or app.suffix != ".app"
            or app != app.resolve(strict=True)
            or not stat.S_ISDIR(app.lstat().st_mode)
            or app.lstat().st_uid not in {0, os.getuid()}
            or stat.S_IMODE(app.lstat().st_mode) & 0o022
        ):
            raise OSError
        info_path = app / "Contents" / "Info.plist"
        _safe_regular(info_path, allow_group_write=True)
        with info_path.open("rb") as stream:
            info = plistlib.load(stream)
        if type(info) is not dict:
            raise ValueError
        executable_name = info.get("CFBundleExecutable")
        version = info.get("CFBundleVersion")
        if (
            info.get("CFBundleIdentifier") != _BUNDLE_ID
            or executable_name != "FreeCAD"
            or version != _FREECAD_VERSION
        ):
            raise ExternalFreeCADError("the FreeCAD build is not admitted by this pilot")
        executable = app / "Contents" / "MacOS" / executable_name
        _safe_regular(executable, executable=True, allow_group_write=True)
        resources = app / "Contents" / "Resources"
        python_library = resources / "lib" / f"libpython{_PYTHON_VERSION}.dylib"
        _safe_regular(python_library, allow_group_write=True)
        pyside_roots = tuple(
            (resources / "lib" / f"python{_PYTHON_VERSION}" / "site-packages").glob(
                "PySide6-*.dist-info"
            )
        )
        if len(pyside_roots) != 1 or not stat.S_ISDIR(pyside_roots[0].lstat().st_mode):
            raise OSError
        metadata = pyside_roots[0] / "METADATA"
        _safe_regular(metadata, allow_group_write=True)
        versions = [
            line.removeprefix("Version:").strip()
            for line in metadata.read_text(encoding="utf-8").splitlines()
            if line.startswith("Version:")
        ]
        shim = resources / "Ext" / "PySide" / "__init__.py"
        _safe_regular(shim, allow_group_write=True)
        if versions != [_PYSIDE_VERSION] or "PySide6" not in shim.read_text(encoding="utf-8"):
            raise ExternalFreeCADError("the FreeCAD Qt binding is not admitted by this pilot")
        fingerprint_input = {
            "bundle_id": _BUNDLE_ID,
            "freecad_version": version,
            "files": {
                path.relative_to(app).as_posix(): _stable_digest(
                    path,
                    allow_group_write=True,
                )
                for path in (info_path, executable, python_library, metadata, shim)
            },
        }
        fingerprint = hashlib.sha256(_canonical_json(fingerprint_input)).hexdigest()
        return ExternalFreeCADHost(
            app_path=app,
            executable=executable,
            freecad_version=version,
            python_version=_PYTHON_VERSION,
            pyside_version=_PYSIDE_VERSION,
            fingerprint=fingerprint,
        )
    except ExternalFreeCADError:
        raise
    except (OSError, TypeError, UnicodeError, ValueError, plistlib.InvalidFileException):
        raise ExternalFreeCADError("the FreeCAD build is not admitted by this pilot") from None


def doctor(app: object) -> dict[str, object]:
    host = inspect_freecad_app(app)
    return {
        "schema_version": 1,
        "compatible": True,
        "host": {
            "app_path": str(host.app_path),
            "bundle_id": _BUNDLE_ID,
            "freecad_version": host.freecad_version,
            "python_version": host.python_version,
            "pyside_version": host.pyside_version,
            "host_fingerprint": host.fingerprint,
        },
        "managed_fallback": "vibecad --freecad",
    }


def _default_user_data_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "FreeCAD"


def _target_root(user_data_root: object | None) -> tuple[Path, Path]:
    root = _default_user_data_root() if user_data_root is None else Path(user_data_root)
    if not root.is_absolute() or ".." in root.parts:
        raise ExternalFreeCADError("the per-user FreeCAD directory is unsafe")
    root = _safe_directory(root, create=True)
    mod = _safe_directory(root / "Mod", create=True)
    return mod, mod / "VibeCAD"


@contextlib.contextmanager
def _install_lock(mod: Path) -> Iterator[None]:
    path = mod / _LOCK_NAME
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise OSError
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise ExternalFreeCADError("another FreeCAD addon operation is active") from None
    except OSError:
        raise ExternalFreeCADError("the FreeCAD addon lock is unsafe") from None
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _source_files() -> tuple[str, ...]:
    from vibecad.freecad_launcher import _ADDON_FILES

    return tuple(sorted(_ADDON_FILES))


def _bridge_executable(python: Path) -> tuple[Path, Path]:
    try:
        if not python.is_absolute() or python.parent != python.parent.resolve(strict=True):
            raise OSError
        entry = python.lstat()
        if entry.st_uid not in {0, os.getuid()} or not (
            stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode)
        ):
            raise OSError
        target = python.resolve(strict=True)
        info = _safe_regular(target, executable=True, allow_group_write=True)
        if (
            python != target and (not stat.S_ISLNK(entry.st_mode) or python.parent != target.parent)
        ) or (stat.S_IMODE(info.st_mode) & 0o020 and not _has_private_user_ancestor(target)):
            raise OSError
        return python, target
    except (ExternalFreeCADError, OSError):
        raise ExternalFreeCADError("the managed bridge executable is unsafe") from None


def _bridge_configuration(python: Path) -> dict[str, object]:
    entry, target = _bridge_executable(python)
    return {
        "schema_version": 1,
        "protocol": _BRIDGE_PROTOCOL,
        "protocol_version": _BRIDGE_PROTOCOL_VERSION,
        "package_version": __version__,
        "python_path": str(entry),
        "python_target": str(target),
        "python_sha256": _sha256(target),
    }


def _prepare_staging(
    mod: Path,
    *,
    host: ExternalFreeCADHost,
    target: Path,
    packaged_addon: Path,
    bridge_python: Path,
) -> tuple[Path, dict[str, object]]:
    try:
        source_root = packaged_addon.resolve(strict=True)
        if packaged_addon != source_root or not stat.S_ISDIR(packaged_addon.lstat().st_mode):
            raise OSError
        staging = Path(tempfile.mkdtemp(prefix=".VibeCAD-stage-", dir=mod))
        files: dict[str, str] = {}
        for relative in _source_files():
            source = source_root / relative
            _safe_regular(source)
            destination = staging / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(0o600)
            files[relative] = _sha256(destination)
        config = _bridge_configuration(bridge_python)
        config_path = staging / _CONFIG_NAME
        config_path.write_bytes(_canonical_json(config))
        config_path.chmod(0o600)
        files[_CONFIG_NAME] = _sha256(config_path)
        receipt_base: dict[str, object] = {
            "schema_version": 1,
            "kind": _INSTALL_KIND,
            "target": str(target),
            "host_app": str(host.app_path),
            "host_fingerprint": host.fingerprint,
            "host_version": host.freecad_version,
            "bridge_protocol": _BRIDGE_PROTOCOL_VERSION,
            "package_version": __version__,
            "files": files,
        }
        receipt_id = hashlib.sha256(_canonical_json(receipt_base)).hexdigest()
        receipt = {**receipt_base, "receipt_id": receipt_id}
        receipt_path = staging / _RECEIPT_NAME
        receipt_path.write_bytes(_canonical_json(receipt))
        receipt_path.chmod(0o600)
        for directory in sorted(
            (path for path in staging.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            directory.chmod(0o500)
        for path in staging.rglob("*"):
            if path.is_file():
                path.chmod(0o400)
        staging.chmod(0o500)
        return staging, receipt
    except BaseException:
        if "staging" in locals() and staging.exists():
            _make_tree_writable(staging)
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _receipt(path: Path) -> dict[str, object]:
    try:
        receipt_path = path / _RECEIPT_NAME
        _safe_regular(receipt_path)
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
        if type(value) is not dict or set(value) != _RECEIPT_KEYS:
            raise ValueError
        files = value["files"]
        receipt_id = value["receipt_id"]
        package_version = value["package_version"]
        if (
            value["schema_version"] != 1
            or value["kind"] != _INSTALL_KIND
            or value["host_version"] != _FREECAD_VERSION
            or type(value["host_fingerprint"]) is not str
            or len(value["host_fingerprint"]) != 64
            or any(character not in "0123456789abcdef" for character in value["host_fingerprint"])
            or value["bridge_protocol"] != _BRIDGE_PROTOCOL_VERSION
            or type(package_version) is not str
            or not package_version
            or len(package_version) > 64
            or any(
                not character.isascii() or not (character.isalnum() or character in ".+-_")
                for character in package_version
            )
            or type(receipt_id) is not str
            or len(receipt_id) != 64
            or any(character not in "0123456789abcdef" for character in receipt_id)
            or type(files) is not dict
            or not files
        ):
            raise ValueError
        for relative, digest in files.items():
            candidate = Path(relative)
            if (
                type(relative) is not str
                or not relative
                or candidate.is_absolute()
                or ".." in candidate.parts
                or type(digest) is not str
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError
        base = dict(value)
        del base["receipt_id"]
        if hashlib.sha256(_canonical_json(base)).hexdigest() != receipt_id:
            raise ValueError
        return value
    except (ExternalFreeCADError, KeyError, OSError, TypeError, UnicodeError, ValueError):
        raise ExternalFreeCADError("a foreign FreeCAD addon tree exists") from None


def _verify_owned_tree(
    target: Path,
    *,
    host: ExternalFreeCADHost,
) -> dict[str, object]:
    try:
        if target != target.resolve(strict=True):
            raise OSError
        root_info = target.lstat()
        if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid():
            raise OSError
    except OSError:
        raise ExternalFreeCADError("a foreign FreeCAD addon tree exists") from None
    receipt = _receipt(target)
    if (
        receipt["target"] != str(target)
        or receipt["host_app"] != str(host.app_path)
        or receipt["host_fingerprint"] != host.fingerprint
    ):
        raise ExternalFreeCADError("the FreeCAD addon ownership receipt does not match")
    expected = set(receipt["files"]) | {_RECEIPT_NAME}
    actual: set[str] = set()
    try:
        for path in target.rglob("*"):
            relative = path.relative_to(target).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise OSError
            if stat.S_ISREG(info.st_mode):
                actual.add(relative)
            elif not stat.S_ISDIR(info.st_mode):
                raise OSError
        if actual != expected:
            raise OSError
        for relative, digest in receipt["files"].items():
            path = target / relative
            _safe_regular(path)
            if _sha256(path) != digest:
                raise OSError
    except (ExternalFreeCADError, OSError):
        raise ExternalFreeCADError("the owned FreeCAD addon is mutated") from None
    return receipt


def _make_tree_writable(root: Path) -> None:
    if not os.path.lexists(root):
        return
    for path in root.rglob("*"):
        with contextlib.suppress(OSError):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
            elif path.is_file() and not path.is_symlink():
                path.chmod(0o600)
    with contextlib.suppress(OSError):
        root.chmod(0o700)


def _remove_verified_tree(path: Path) -> None:
    _make_tree_writable(path)
    try:
        shutil.rmtree(path)
    except OSError:
        raise ExternalFreeCADError("FreeCAD addon cleanup requires manual recovery") from None


def install_addon(
    app: object,
    *,
    user_data_root: object | None = None,
    packaged_addon: object,
    bridge_python: object,
) -> dict[str, object]:
    host = inspect_freecad_app(app)
    mod, target = _target_root(user_data_root)
    source = Path(packaged_addon)
    python = Path(bridge_python)
    if not python.is_absolute():
        raise ExternalFreeCADError("the managed bridge executable is unsafe")
    _bridge_executable(python)
    with _install_lock(mod):
        staging, expected = _prepare_staging(
            mod,
            host=host,
            target=target,
            packaged_addon=source,
            bridge_python=python,
        )
        try:
            if os.path.lexists(target):
                current = _verify_owned_tree(target, host=host)
                if current["receipt_id"] == expected["receipt_id"]:
                    _remove_verified_tree(staging)
                    return {
                        "schema_version": 1,
                        "status": "already_installed",
                        "target": str(target),
                        "receipt_id": expected["receipt_id"],
                    }
                parked = mod / f".VibeCAD-replaced-{current['receipt_id']}"
                if os.path.lexists(parked):
                    raise ExternalFreeCADError("FreeCAD addon upgrade requires manual recovery")
                os.rename(target, parked)
                try:
                    os.rename(staging, target)
                except BaseException as error:
                    try:
                        os.rename(parked, target)
                    except OSError:
                        raise ExternalFreeCADError(
                            "FreeCAD addon upgrade requires manual recovery"
                        ) from error
                    raise
                _remove_verified_tree(parked)
                status = "upgraded"
            else:
                os.rename(staging, target)
                status = "installed"
            return {
                "schema_version": 1,
                "status": status,
                "target": str(target),
                "receipt_id": expected["receipt_id"],
            }
        except BaseException:
            if os.path.lexists(staging):
                _remove_verified_tree(staging)
            raise


def uninstall_addon(
    app: object,
    *,
    user_data_root: object | None = None,
) -> dict[str, object]:
    host = inspect_freecad_app(app)
    mod, target = _target_root(user_data_root)
    with _install_lock(mod):
        if not os.path.lexists(target):
            return {
                "schema_version": 1,
                "status": "not_installed",
                "target": str(target),
            }
        receipt = _verify_owned_tree(target, host=host)
        parked = mod / f".VibeCAD-remove-{receipt['receipt_id']}"
        if os.path.lexists(parked):
            raise ExternalFreeCADError("FreeCAD addon uninstall requires manual recovery")
        os.rename(target, parked)
        _remove_verified_tree(parked)
        return {
            "schema_version": 1,
            "status": "uninstalled",
            "target": str(target),
            "receipt_id": receipt["receipt_id"],
        }


def _install_from_current_package(app: Path) -> dict[str, object]:
    from vibecad import freecad_launcher
    from vibecad.runtime import paths

    addon = freecad_launcher._require_packaged_addon()
    prefix, _evidence, _freecad = freecad_launcher._require_managed_runtime()
    python = paths.env_python_for(prefix)
    return install_addon(
        app,
        packaged_addon=addon,
        bridge_python=python,
    )


def handle_cli(arguments: list[str]) -> int:
    if (
        len(arguments) != 3
        or arguments[0] != "--freecad-app"
        or arguments[2] not in {"--doctor", "--install-addon", "--uninstall-addon"}
    ):
        print(
            "usage: vibecad --freecad-app <absolute.app> "
            "(--doctor|--install-addon|--uninstall-addon)",
            file=sys.stderr,
        )
        return 2
    app = Path(arguments[1])
    try:
        if arguments[2] == "--doctor":
            result = doctor(app)
        elif arguments[2] == "--install-addon":
            result = _install_from_current_package(app)
        else:
            result = uninstall_addon(app)
    except (ExternalFreeCADError, OSError, RuntimeError, ValueError) as error:
        print(f"vibecad --freecad-app: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


__all__ = (
    "ExternalFreeCADError",
    "ExternalFreeCADHost",
    "doctor",
    "handle_cli",
    "inspect_freecad_app",
    "install_addon",
    "uninstall_addon",
)
