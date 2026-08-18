#!/usr/bin/env python3
"""Generate or verify the fixed VibeCAD Reviewed-FreeCAD release attestation.

This maintainer-only command must run inside the pinned managed FreeCAD
Python.  It has no path or platform override: the trusted current platform
selects one fixed canonical JSON resource and one key in the shared pin source.
Generation preserves every sibling-platform pin.  ``--check`` performs the same
real discovery and 125-by-seven conformance run, but only reads and compares
the two selected checked-in files.  A directory-inode process lock serializes
pin read/merge/publication while allowing concurrent read-only checks.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import fcntl
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from vibecad import __version__
from vibecad.execution.freecad_current_managed_verification import (
    CURRENT_MANAGED_VERIFICATION_FORMAL_OPERATION_COUNT,
    CURRENT_MANAGED_VERIFICATION_NATIVE_TYPE_COUNT,
    CURRENT_MANAGED_VERIFICATION_RECEIPT_COUNT,
    build_current_managed_freecad_reviewed_verification_set_for_maintainers,
)
from vibecad.execution.freecad_discovery_runtime_v2 import (
    FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
    _platform_id,
    collect_managed_freecad_discovery_v2,
)
from vibecad.execution.freecad_reviewed_release_attestation import (
    build_freecad_reviewed_release_attestation,
    decode_freecad_reviewed_release_attestation,
    encode_freecad_reviewed_release_attestation,
    validate_freecad_reviewed_release_attestation,
)

_ROOT = Path(__file__).resolve().parents[2]
_ATTESTATION_DIRECTORY = _ROOT / "src/vibecad/execution/_attestations"
_PINS_PATH = _ATTESTATION_DIRECTORY / "freecad_reviewed_release_attestation_pins.py"
_RESOURCE_NAME_BY_PLATFORM_ID = {
    "macos.arm64": "freecad-reviewed-release-attestation-macos-arm64-v1.json",
    "macos.x86_64": "freecad-reviewed-release-attestation-macos-x86_64-v1.json",
}
_PIN_NAME = "PACKAGED_FREECAD_REVIEWED_RELEASE_ATTESTATION_SHA256_BY_RELEASE_PLATFORM"
_MAX_EXISTING_FILE_BYTES = 2 * 1024 * 1024
_FREECAD_USER_TEMP_ENV = "FREECAD_USER_TEMP"

_PINS_DOCSTRING = """Generated platform pins for packaged reviewed FreeCAD attestations.

The release generator replaces this mapping together with the canonical JSON
resource for its trusted current platform.  A key is ``(release_version,
platform_id)``; the generator preserves sibling-platform keys.  Keeping an
empty mapping in an un-attested source checkout is intentional: consumers fail
closed and cannot manufacture VERIFIED coverage.
"""


class GenerationError(RuntimeError):
    """The fixed release-attestation generation boundary failed closed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class GenerationResult:
    release_version: str
    resource: bytes
    resource_sha256: str
    attestation_sha256: str
    discovery_snapshot_sha256: str
    discovery_manifest_sha256: str
    runtime_platform_id: str
    receipt_count: int
    formal_operation_count: int
    native_type_count: int
    elapsed_seconds: float


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
        raise GenerationError("cannot encode the generation summary canonically") from exc


def _render_pins(mapping: dict[tuple[str, str], str]) -> bytes:
    if type(mapping) is not dict or any(
        type(key) is not tuple
        or len(key) != 2
        or type(key[0]) is not str
        or not key[0]
        or type(key[1]) is not str
        or key[1] not in _RESOURCE_NAME_BY_PLATFORM_ID
        or type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        for key, digest in mapping.items()
    ):
        raise GenerationError("invalid release-platform pin mapping")
    lines = [
        f'"""{_PINS_DOCSTRING}"""',
        "",
        "from __future__ import annotations",
        "",
        "from types import MappingProxyType",
        "from typing import Final",
        "",
    ]
    if not mapping:
        lines.append(f"{_PIN_NAME}: Final = MappingProxyType({{}})")
    else:
        lines.extend(
            (
                f"{_PIN_NAME}: Final = MappingProxyType(",
                "    {",
                *(
                    line
                    for key in sorted(mapping)
                    for line in (
                        "        (",
                        f"            {json.dumps(key[0], ensure_ascii=True)},",
                        f"            {json.dumps(key[1], ensure_ascii=True)},",
                        f"        ): {json.dumps(mapping[key], ensure_ascii=True)},",
                    )
                ),
                "    }",
                ")",
            )
        )
    return ("\n".join(lines) + "\n").encode("ascii")


def _decode_canonical_pins(raw: bytes) -> dict[tuple[str, str], str]:
    if type(raw) is not bytes or not raw or len(raw) > _MAX_EXISTING_FILE_BYTES:
        raise GenerationError("the existing pin source is missing or oversized")
    try:
        module = ast.parse(raw.decode("ascii"), filename=str(_PINS_PATH))
        assignment = module.body[-1]
        if (
            not isinstance(assignment, ast.AnnAssign)
            or not isinstance(assignment.target, ast.Name)
            or assignment.target.id != _PIN_NAME
            or not isinstance(assignment.value, ast.Call)
            or not isinstance(assignment.value.func, ast.Name)
            or assignment.value.func.id != "MappingProxyType"
            or len(assignment.value.args) != 1
            or assignment.value.keywords
        ):
            raise ValueError("unexpected pin declaration")
        value = ast.literal_eval(assignment.value.args[0])
    except (SyntaxError, UnicodeError, ValueError, TypeError, IndexError) as exc:
        raise GenerationError("the existing pin source is not canonical") from exc
    if type(value) is not dict or any(
        type(key) is not tuple
        or len(key) != 2
        or type(key[0]) is not str
        or type(key[1]) is not str
        or type(digest) is not str
        for key, digest in value.items()
    ):
        raise GenerationError("the existing pin source is not canonical")
    if _render_pins(value) != raw:
        raise GenerationError("the existing pin source is not canonical")
    return value


def _decode_canonical_resource(raw: bytes) -> None:
    if type(raw) is not bytes or not raw or len(raw) > _MAX_EXISTING_FILE_BYTES:
        raise GenerationError("the existing attestation resource is missing or oversized")
    source_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        decode_freecad_reviewed_release_attestation(
            raw,
            expected_source_attestation_sha256=source_sha256,
        )
    except Exception as exc:
        raise GenerationError("the existing attestation resource is not canonical") from exc


def _resource_path_for_platform(platform_id: object) -> Path:
    resource_name = (
        _RESOURCE_NAME_BY_PLATFORM_ID.get(platform_id) if type(platform_id) is str else None
    )
    if type(resource_name) is not str:
        raise GenerationError("the current platform has no fixed attestation resource")
    return _ATTESTATION_DIRECTORY / resource_name


def _fixed_output_paths() -> frozenset[Path]:
    return frozenset(
        {
            _PINS_PATH,
            *(
                _ATTESTATION_DIRECTORY / resource_name
                for resource_name in _RESOURCE_NAME_BY_PLATFORM_ID.values()
            ),
        }
    )


def _validate_attestation_lock_directory(
    descriptor: int,
    expected: os.stat_result,
) -> None:
    try:
        opened = os.fstat(descriptor)
        live = _ATTESTATION_DIRECTORY.lstat()
    except OSError as exc:
        raise GenerationError("cannot validate the attestation directory lock") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(live.st_mode)
        or stat.S_ISLNK(live.st_mode)
        or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
        or (live.st_dev, live.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise GenerationError("the attestation directory changed while locked")


@contextlib.contextmanager
def _attestation_index_lock(*, exclusive: bool):
    """Hold one inode-stable process lock over the shared resource/pin index."""

    if type(exclusive) is not bool:
        raise GenerationError("invalid attestation lock mode")
    descriptor = -1
    try:
        expected = _ATTESTATION_DIRECTORY.lstat()
        if not stat.S_ISDIR(expected.st_mode) or stat.S_ISLNK(expected.st_mode):
            raise GenerationError("the attestation lock target is not a real directory")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(_ATTESTATION_DIRECTORY, flags)
        _validate_attestation_lock_directory(descriptor, expected)
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except GenerationError:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        raise GenerationError("cannot acquire the attestation directory lock") from exc
    try:
        _validate_attestation_lock_directory(descriptor, expected)
        yield
        _validate_attestation_lock_directory(descriptor, expected)
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(descriptor)


def _read_fixed_file(path: Path, *, required: bool) -> bytes | None:
    if path not in _fixed_output_paths():
        raise GenerationError("a non-fixed output path was requested")
    try:
        parent = path.parent.resolve(strict=True)
        expected_parent = _ATTESTATION_DIRECTORY.resolve(strict=True)
    except OSError as exc:
        raise GenerationError("the fixed attestation directory is unavailable") from exc
    if parent != expected_parent or path.is_symlink():
        raise GenerationError(f"fixed output must not traverse a symlink: {path.name}")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if required:
            raise GenerationError(f"fixed output is missing: {path.name}") from None
        return None
    except OSError as exc:
        raise GenerationError(f"cannot inspect fixed output: {path.name}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_EXISTING_FILE_BYTES:
        raise GenerationError(f"fixed output is not a bounded regular file: {path.name}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GenerationError(f"cannot read fixed output: {path.name}") from exc
    if len(raw) != metadata.st_size:
        raise GenerationError(f"fixed output changed while reading: {path.name}")
    if path == _PINS_PATH:
        _decode_canonical_pins(raw)
    else:
        _decode_canonical_resource(raw)
    return raw


def _assert_headless_empty(freecad: object, *, stage: str) -> None:
    try:
        gui_up = freecad.GuiUp
        documents = freecad.listDocuments()
    except BaseException as exc:
        raise GenerationError(f"cannot inspect managed FreeCAD at {stage}") from exc
    if (
        type(gui_up) is not int
        or gui_up != 0
        or type(documents) is not dict
        or documents
        or "FreeCADGui" in sys.modules
    ):
        raise GenerationError(f"managed FreeCAD is not headless and empty at {stage}")


def _assert_freecad_user_temp(freecad: object, expected_root: Path) -> None:
    try:
        actual = Path(freecad.getUserCachePath())
        actual_info = actual.lstat()
        expected_info = expected_root.lstat()
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise GenerationError("cannot authenticate the FreeCAD document cache") from exc
    if (
        actual.resolve(strict=True) != expected_root.resolve(strict=True)
        or stat.S_ISLNK(actual_info.st_mode)
        or actual_info.st_dev != expected_info.st_dev
        or actual_info.st_ino != expected_info.st_ino
    ):
        raise GenerationError("FreeCAD ignored the private document cache binding")


@contextlib.contextmanager
def _private_freecad_user_temp():
    """Bind one private native document-cache root before importing FreeCAD."""

    previous = os.environ.get(_FREECAD_USER_TEMP_ENV)
    failed = False
    with tempfile.TemporaryDirectory(prefix=".vibecad-reviewed-freecad-") as raw_root:
        root = Path(raw_root).resolve(strict=True)
        root.chmod(0o700)
        info = root.lstat()
        if (
            root.is_symlink()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise GenerationError("cannot establish a private FreeCAD document cache")
        os.environ[_FREECAD_USER_TEMP_ENV] = str(root)
        try:
            yield root
        except BaseException:
            failed = True
            raise
        finally:
            if previous is None:
                os.environ.pop(_FREECAD_USER_TEMP_ENV, None)
            else:
                os.environ[_FREECAD_USER_TEMP_ENV] = previous
            if not failed:
                try:
                    residual = tuple(root.iterdir())
                except OSError as exc:
                    raise GenerationError(
                        "cannot verify the private FreeCAD document cache"
                    ) from exc
                if residual:
                    raise GenerationError("FreeCAD left a document cache entry behind")


def _build_release_attestation() -> GenerationResult:
    started = time.perf_counter()
    with _private_freecad_user_temp() as native_root:
        return _build_release_attestation_in_private_profile(
            started=started,
            native_root=native_root,
        )


def _build_release_attestation_in_private_profile(
    *,
    started: float,
    native_root: Path,
) -> GenerationResult:
    from vibecad.freecad_env import prepare_freecad_import

    prepare_freecad_import()
    import FreeCAD  # noqa: PLC0415

    _assert_freecad_user_temp(FreeCAD, native_root)
    _assert_headless_empty(FreeCAD, stage="start")
    discovery = collect_managed_freecad_discovery_v2(
        freecad=FreeCAD,
        probe_modules=FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
    )
    _assert_headless_empty(FreeCAD, stage="after-discovery")
    verification_set = build_current_managed_freecad_reviewed_verification_set_for_maintainers(
        freecad=FreeCAD
    )
    _assert_headless_empty(FreeCAD, stage="after-verification")
    if verification_set.runtime_backend != discovery.snapshot.backend:
        raise GenerationError("discovery and verification observed different managed builds")
    if (
        len(verification_set.receipts) != CURRENT_MANAGED_VERIFICATION_RECEIPT_COUNT
        or len(verification_set.formal_operations)
        != CURRENT_MANAGED_VERIFICATION_FORMAL_OPERATION_COUNT
        or len(verification_set.native_types) != CURRENT_MANAGED_VERIFICATION_NATIVE_TYPE_COUNT
    ):
        raise GenerationError("the current reviewed inventory is incomplete")

    attestation = build_freecad_reviewed_release_attestation(
        release_version=__version__,
        runtime_backend=discovery.snapshot.backend,
        discovery_snapshot_sha256=discovery.snapshot.snapshot_sha256,
        discovery_manifest_sha256=discovery.manifest.manifest_sha256,
        verification_set=verification_set,
    )
    raw = encode_freecad_reviewed_release_attestation(attestation)
    resource_sha256 = hashlib.sha256(raw).hexdigest()
    decoded = decode_freecad_reviewed_release_attestation(
        raw,
        expected_source_attestation_sha256=resource_sha256,
    )
    validate_freecad_reviewed_release_attestation(
        decoded,
        expected_release_version=__version__,
        runtime_backend=discovery.snapshot.backend,
        discovery_snapshot_sha256=discovery.snapshot.snapshot_sha256,
        discovery_manifest_sha256=discovery.manifest.manifest_sha256,
        expected_source_attestation_sha256=resource_sha256,
    )
    _assert_headless_empty(FreeCAD, stage="finish")
    return GenerationResult(
        release_version=__version__,
        resource=raw,
        resource_sha256=resource_sha256,
        attestation_sha256=attestation.attestation_sha256,
        discovery_snapshot_sha256=discovery.snapshot.snapshot_sha256,
        discovery_manifest_sha256=discovery.manifest.manifest_sha256,
        runtime_platform_id=discovery.snapshot.platform_id,
        receipt_count=len(verification_set.receipts),
        formal_operation_count=len(verification_set.formal_operations),
        native_type_count=len(verification_set.native_types),
        elapsed_seconds=time.perf_counter() - started,
    )


def _stage_file(*, directory: Path, name: str, raw: bytes) -> Path:
    descriptor = -1
    path: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
        path = Path(raw_path)
        os.fchmod(descriptor, 0o644)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        return path
    except BaseException:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink()
        raise


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_pair(*, resource_path: Path, resource: bytes, pins: bytes) -> bool:
    if resource_path not in _fixed_output_paths() or resource_path == _PINS_PATH:
        raise GenerationError("a non-fixed resource path was requested")
    _decode_canonical_resource(resource)
    _decode_canonical_pins(pins)
    old_resource = _read_fixed_file(resource_path, required=False)
    old_pins = _read_fixed_file(_PINS_PATH, required=True)
    if old_resource == resource and old_pins == pins:
        return False

    staged_resource: Path | None = None
    staged_pins: Path | None = None
    rollback_resource: Path | None = None
    rollback_pins: Path | None = None
    resource_replaced = False
    pins_replaced = False
    try:
        staged_resource = _stage_file(
            directory=_ATTESTATION_DIRECTORY,
            name=resource_path.name,
            raw=resource,
        )
        staged_pins = _stage_file(
            directory=_ATTESTATION_DIRECTORY,
            name=_PINS_PATH.name,
            raw=pins,
        )
        if old_resource is not None:
            rollback_resource = _stage_file(
                directory=_ATTESTATION_DIRECTORY,
                name=f"rollback-{resource_path.name}",
                raw=old_resource,
            )
        rollback_pins = _stage_file(
            directory=_ATTESTATION_DIRECTORY,
            name=f"rollback-{_PINS_PATH.name}",
            raw=old_pins,
        )
        os.replace(staged_resource, resource_path)
        staged_resource = None
        resource_replaced = True
        os.replace(staged_pins, _PINS_PATH)
        staged_pins = None
        pins_replaced = True
        _fsync_directory(_ATTESTATION_DIRECTORY)
    except Exception as exc:
        try:
            if pins_replaced:
                assert rollback_pins is not None
                os.replace(rollback_pins, _PINS_PATH)
                rollback_pins = None
            if resource_replaced:
                if rollback_resource is None:
                    resource_path.unlink(missing_ok=True)
                else:
                    os.replace(rollback_resource, resource_path)
                    rollback_resource = None
            _fsync_directory(_ATTESTATION_DIRECTORY)
        except Exception as rollback_exc:
            raise GenerationError(
                "attestation publication failed and rollback could not restore both files"
            ) from rollback_exc
        raise GenerationError("attestation publication failed; both files were restored") from exc
    finally:
        for temporary in (
            staged_resource,
            staged_pins,
            rollback_resource,
            rollback_pins,
        ):
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink()
    return True


def _check_pair(*, resource_path: Path, resource: bytes, pins: bytes) -> None:
    if resource_path not in _fixed_output_paths() or resource_path == _PINS_PATH:
        raise GenerationError("a non-fixed resource path was requested")
    _decode_canonical_resource(resource)
    _decode_canonical_pins(pins)
    if _read_fixed_file(resource_path, required=True) != resource:
        raise GenerationError("the packaged attestation resource is stale")
    if _read_fixed_file(_PINS_PATH, required=True) != pins:
        raise GenerationError("the packaged attestation pin source is stale")


def _summary(result: GenerationResult, *, mode: str, changed: bool) -> bytes:
    return _canonical_json(
        {
            "attestation_sha256": result.attestation_sha256,
            "changed": changed,
            "discovery_manifest_sha256": result.discovery_manifest_sha256,
            "discovery_snapshot_sha256": result.discovery_snapshot_sha256,
            "elapsed_seconds": round(result.elapsed_seconds, 3),
            "formal_operation_count": result.formal_operation_count,
            "mode": mode,
            "native_type_count": result.native_type_count,
            "receipt_count": result.receipt_count,
            "release_version": result.release_version,
            "resource_sha256": result.resource_sha256,
            "resource_size_bytes": len(result.resource),
            "runtime_platform_id": result.runtime_platform_id,
        }
    )


def _current_platform_publication(result: GenerationResult) -> tuple[str, Path, bytes]:
    try:
        current_platform_id = _platform_id()
    except Exception as exc:
        raise GenerationError("cannot identify the trusted current platform") from exc
    if type(current_platform_id) is not str or result.runtime_platform_id != current_platform_id:
        raise GenerationError("discovery does not match the trusted current platform")
    resource_path = _resource_path_for_platform(current_platform_id)
    existing_pins_raw = _read_fixed_file(_PINS_PATH, required=True)
    assert existing_pins_raw is not None
    updated_pins = _decode_canonical_pins(existing_pins_raw)
    updated_pins[(result.release_version, current_platform_id)] = result.resource_sha256
    return current_platform_id, resource_path, _render_pins(updated_pins)


def _apply_current_platform_result(
    result: GenerationResult,
    *,
    check: bool,
) -> tuple[bool, str]:
    """Check or publish one result while holding the whole shared-index transaction."""

    if type(check) is not bool:
        raise GenerationError("invalid release-attestation mode")
    with _attestation_index_lock(exclusive=not check):
        _current_platform_id, resource_path, pins = _current_platform_publication(result)
        if check:
            _check_pair(resource_path=resource_path, resource=result.resource, pins=pins)
            return False, "check"
        changed = _publish_pair(
            resource_path=resource_path,
            resource=result.resource,
            pins=pins,
        )
        return changed, "generate"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate or verify the fixed Reviewed-FreeCAD release attestation."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="run real verification and require exact checked-in bytes without writing",
    )
    args = parser.parse_args(argv)
    try:
        result = _build_release_attestation()
        changed, mode = _apply_current_platform_result(result, check=args.check)
    except GenerationError as exc:
        parser.exit(1, f"release attestation failed: {exc}\n")
    sys.stdout.buffer.write(_summary(result, mode=mode, changed=changed) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
