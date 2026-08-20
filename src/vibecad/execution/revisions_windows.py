"""Identity-pinned Windows backend for :mod:`vibecad.execution.revisions`.

The POSIX revision store intentionally relies on ``dir_fd`` and immutable
descriptor-relative authority.  Windows does not provide that contract to
Python.  This backend therefore keeps the on-disk record format identical but
uses private-DACL path capabilities, File IDs and volume identities at every
security-sensitive boundary.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

from vibecad._file_compat import (
    WindowsPathCapability,
    capture_windows_fd,
    capture_windows_path,
    delete_windows_directory,
    delete_windows_file,
    ensure_private_directory,
    open_private_file,
    rename_windows_directory,
    replace_windows_file,
    validate_windows_path,
    windows_extended_path,
)
from vibecad.workflow.lease import LeaseError

_MAX_DIRECTORY_ENTRIES = 65_536


def _r():
    # Imported lazily so revisions.py can dispatch here without a module cycle.
    from vibecad.execution import revisions

    return revisions


def _raise(code, *, head_committed=None):
    r = _r()
    if head_committed is None:
        raise r.RevisionStoreError(code)
    raise r.RevisionStoreError(code, head_committed=head_committed)


def _windows_path(value) -> Path:
    r = _r()
    if type(value) is str:
        path = Path(value)
    elif type(value) is type(Path()):
        path = value
    else:
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    if not path.is_absolute() or ".." in path.parts:
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute():
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    return absolute


def initialize_store(root):
    """Validate the trusted root and return constructor state."""

    r = _r()
    path = _windows_path(root)
    try:
        capability = capture_windows_path(path, directory=True)
        validate_windows_path(capability, directory=True)
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    return (
        path,
        tuple(path.parts),
        (capability.volume, capability.file_id),
        capability,
    )


def _root(store) -> tuple[Path, WindowsPathCapability]:
    r = _r()
    capability = store._windows_root_capability
    if type(capability) is not WindowsPathCapability:
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    try:
        path = validate_windows_path(capability, directory=True)
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    if os.path.normcase(os.fspath(path)) != os.path.normcase(os.fspath(store._root)):
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    return path, capability


@contextmanager
def _quota_mutation(store):
    """Exclude quota snapshots while one stable physical state is changing."""

    r = _r()
    acquired, code = r._acquire_quota_lease(store)
    if code is not None:
        _raise(code)
    try:
        yield
    finally:
        release_code = r._release_quota_lease(acquired)
        if release_code is not None:
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)


def _require_lease(store, project_id, lease) -> None:
    r = _r()
    code = r._require_mutation(store, project_id, lease)
    if code is not None:
        _raise(code)
    try:
        lease.require_current()
    except (LeaseError, OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.INVALID_LEASE)
    _root(store)


def _validate_lease_after(store, lease) -> None:
    r = _r()
    try:
        lease.require_current()
        _root(store)
    except (LeaseError, OSError, TypeError, ValueError, r.RevisionStoreError):
        _raise(r.RevisionStoreErrorCode.INVALID_LEASE)


def _same_object(left: WindowsPathCapability, right: WindowsPathCapability) -> bool:
    return (
        left.volume == right.volume
        and left.file_id == right.file_id
        and left.owner_sid == right.owner_sid
        and left.security_sha256 == right.security_sha256
    )


def _child_path(parent: WindowsPathCapability, name: str) -> Path:
    if (
        type(name) is not str
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
    ):
        raise ValueError("invalid Windows store entry name")
    return Path(parent.path) / name


def _capture_directory(
    path: Path,
    parent: WindowsPathCapability,
    *,
    missing_code=None,
) -> WindowsPathCapability:
    r = _r()
    if missing_code is None:
        missing_code = r.RevisionStoreErrorCode.NOT_FOUND
    try:
        validate_windows_path(parent, directory=True)
        if os.path.normcase(os.fspath(path.parent)) != os.path.normcase(parent.path):
            raise OSError
        capability = capture_windows_path(path, directory=True)
        if capability.volume != parent.volume:
            raise OSError
        validate_windows_path(parent, directory=True)
        return capability
    except FileNotFoundError:
        _raise(missing_code)
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)


def _create_directory(
    path: Path,
    parent: WindowsPathCapability,
    *,
    exclusive: bool = False,
) -> WindowsPathCapability:
    r = _r()
    try:
        capability = ensure_private_directory(
            path,
            expected_parent=parent,
            exclusive=exclusive,
        )
        if capability.volume != parent.volume:
            raise OSError
        validate_windows_path(parent, directory=True)
        return capability
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.IO_ERROR)


def _entry_missing(path: Path) -> bool:
    try:
        os.lstat(windows_extended_path(path))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _read_file(
    parent: WindowsPathCapability,
    name: str,
    maximum: int,
    *,
    missing_code,
    allow_empty: bool = False,
) -> tuple[bytes, WindowsPathCapability]:
    r = _r()
    path = _child_path(parent, name)
    fd = None
    capability = None
    try:
        fd, capability = open_private_file(
            path,
            create=False,
            read_write=False,
            expected_parent=parent,
        )
        before = os.fstat(fd)
        size = int(before.st_size)
        if size < 0 or size > maximum or (not allow_empty and size == 0):
            _raise(
                r.RevisionStoreErrorCode.BUDGET_EXCEEDED
                if size > maximum
                else r.RevisionStoreErrorCode.CORRUPT_CONTENT
            )
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(fd, min(r._COPY_CHUNK_BYTES, remaining))
            if not chunk or len(chunk) > remaining:
                raise OSError
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        pinned = capture_windows_fd(
            fd,
            directory=False,
            generation_token=capability.generation_token,
        )
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or pinned != capability
        ):
            raise OSError
        validate_windows_path(capability, directory=False)
        validate_windows_path(parent, directory=True)
        return b"".join(chunks), capability
    except FileNotFoundError:
        _raise(missing_code)
    except r.RevisionStoreError:
        raise
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _write_all(fd: int, raw: bytes) -> None:
    r = _r()
    view = memoryview(raw)
    offset = 0
    while offset < len(view):
        try:
            written = os.write(fd, view[offset:])
        except OSError:
            _raise(r.RevisionStoreErrorCode.IO_ERROR)
        if written <= 0:
            _raise(r.RevisionStoreErrorCode.IO_ERROR)
        offset += written


def _write_new_file(
    parent: WindowsPathCapability,
    name: str,
    raw: bytes,
) -> WindowsPathCapability:
    r = _r()
    if type(raw) is not bytes:
        _raise(r.RevisionStoreErrorCode.INVALID_INPUT)
    path = _child_path(parent, name)
    fd = None
    capability = None
    succeeded = False
    try:
        fd, capability = open_private_file(
            path,
            create=True,
            read_write=True,
            exclusive=True,
            expected_parent=parent,
        )
        _write_all(fd, raw)
        os.fsync(fd)
        pinned = capture_windows_fd(
            fd,
            directory=False,
            generation_token=capability.generation_token,
        )
        if pinned != capability or os.fstat(fd).st_size != len(raw):
            raise OSError
        os.close(fd)
        fd = None
        validate_windows_path(capability, directory=False)
        validate_windows_path(parent, directory=True)
        succeeded = True
        return capability
    except r.RevisionStoreError:
        raise
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.IO_ERROR)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if not succeeded and capability is not None:
            try:
                delete_windows_file(path, parent=parent, expected=capability)
            except (OSError, TypeError, ValueError):
                pass


def _replace_record(
    parent: WindowsPathCapability,
    name: str,
    raw: bytes,
) -> WindowsPathCapability:
    r = _r()
    token = secrets.token_hex(16)
    temp_name = f".{name}.{token}.tmp"
    temp_path = _child_path(parent, temp_name)
    destination = _child_path(parent, name)
    source_capability = _write_new_file(parent, temp_name, raw)
    destination_capability = None
    try:
        try:
            destination_capability = capture_windows_path(destination, directory=False)
        except FileNotFoundError:
            destination_capability = None
        moved = replace_windows_file(
            temp_path,
            destination,
            source_parent=parent,
            expected_source=source_capability,
            expected_destination=destination_capability,
        )
        if not _same_object(moved, source_capability):
            raise OSError
        validate_windows_path(parent, directory=True)
        return moved
    except (OSError, TypeError, ValueError):
        try:
            validate_windows_path(source_capability, directory=False)
        except (OSError, TypeError, ValueError):
            pass
        else:
            try:
                delete_windows_file(
                    temp_path,
                    parent=parent,
                    expected=source_capability,
                )
            except (OSError, TypeError, ValueError):
                pass
        _raise(r.RevisionStoreErrorCode.IO_ERROR)


def _delete_file(parent: WindowsPathCapability, name: str, *, missing_ok: bool) -> None:
    r = _r()
    path = _child_path(parent, name)
    try:
        capability = capture_windows_path(path, directory=False)
    except FileNotFoundError:
        if missing_ok:
            return
        _raise(r.RevisionStoreErrorCode.NOT_FOUND)
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    try:
        delete_windows_file(path, parent=parent, expected=capability)
        validate_windows_path(parent, directory=True)
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.IO_ERROR)


def _rename_directory(
    parent: WindowsPathCapability,
    source_name: str,
    destination_name: str,
    source: WindowsPathCapability,
) -> WindowsPathCapability:
    r = _r()
    source_path = _child_path(parent, source_name)
    destination = _child_path(parent, destination_name)
    if not _entry_missing(destination):
        _raise(r.RevisionStoreErrorCode.CONFLICT)
    try:
        moved = rename_windows_directory(
            source_path,
            destination,
            source_parent=parent,
            expected_source=source,
        )
        if not _same_object(moved, source):
            raise OSError
        if not _entry_missing(source_path):
            raise OSError
        validate_windows_path(parent, directory=True)
        return moved
    except FileExistsError:
        _raise(r.RevisionStoreErrorCode.CONFLICT)
    except r.RevisionStoreError:
        raise
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.IO_ERROR)


def _directory_names(capability: WindowsPathCapability) -> tuple[str, ...]:
    r = _r()
    try:
        path = validate_windows_path(capability, directory=True)
        values: list[str] = []
        with os.scandir(windows_extended_path(path)) as iterator:
            for entry in iterator:
                if (
                    type(entry.name) is not str
                    or entry.name in {".", ".."}
                    or len(values) >= _MAX_DIRECTORY_ENTRIES
                ):
                    raise OSError
                values.append(entry.name)
        validate_windows_path(capability, directory=True)
        return tuple(sorted(values))
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)


def _project_context(store, project_id):
    r = _r()
    code = r._identifier_code(project_id, r._PROJECT_PATTERN)
    if code is not None:
        _raise(code)
    root_path, root_capability = _root(store)
    project_path = root_path / r._project_key(project_id)
    project = _capture_directory(project_path, root_capability)
    revisions = _capture_directory(project_path / "revisions", project)
    candidates = _capture_directory(project_path / "candidates", project)
    validate_windows_path(root_capability, directory=True)
    return {
        "root_path": root_path,
        "root": root_capability,
        "project_path": project_path,
        "project": project,
        "revisions_path": project_path / "revisions",
        "revisions": revisions,
        "candidates_path": project_path / "candidates",
        "candidates": candidates,
    }


def _parse_record(raw: bytes, domain: bytes, maximum: int):
    r = _r()
    parsed, code = r._parse_checked_record(raw, domain, maximum)
    if code is not None:
        _raise(code)
    return parsed


def _load_revision_context(context, project_id, revision_id):
    r = _r()
    code = r._identifier_code(revision_id, r._REVISION_PATTERN)
    if code is not None:
        _raise(code)
    revision_name = r._revision_key(revision_id)
    revision_path = context["revisions_path"] / revision_name
    revision_capability = _capture_directory(revision_path, context["revisions"])
    raw, _manifest_capability = _read_file(
        revision_capability,
        "manifest.json",
        r._MAX_MANIFEST_BYTES,
        missing_code=r.RevisionStoreErrorCode.CORRUPT_RECORD,
    )
    body = _parse_record(raw, r._MANIFEST_CHECKSUM_DOMAIN, r._MAX_MANIFEST_BYTES)
    revision, code = r._revision_from_manifest(body, raw)
    if code is not None:
        _raise(code)
    if revision.project_id != project_id or revision.id != revision_id:
        _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
    expected_names = {"manifest.json"}
    if revision.model is not None:
        expected_names.add(revision.model.name)
    for artifact in revision.artifacts:
        expected_names.add(artifact.name)
    names = _directory_names(revision_capability)
    if set(names) != expected_names or len(names) != len(expected_names):
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    references = ()
    if revision.model is not None:
        references = (revision.model,)
    references = references + revision.artifacts
    for reference in references:
        payload, _capability = _read_file(
            revision_capability,
            reference.name,
            r._MAX_FILE_BYTES,
            missing_code=r.RevisionStoreErrorCode.CORRUPT_CONTENT,
        )
        if (
            len(payload) != reference.size_bytes
            or hashlib.sha256(payload).hexdigest() != reference.sha256
        ):
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
    after, _ = _read_file(
        revision_capability,
        "manifest.json",
        r._MAX_MANIFEST_BYTES,
        missing_code=r.RevisionStoreErrorCode.CORRUPT_RECORD,
    )
    if after != raw:
        _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
    validate_windows_path(revision_capability, directory=True)
    validate_windows_path(context["revisions"], directory=True)
    validate_windows_path(context["root"], directory=True)
    return revision, revision_capability


def _load_head_context(context, project_id):
    r = _r()
    raw, _capability = _read_file(
        context["project"],
        "HEAD.json",
        r._MAX_HEAD_BYTES,
        missing_code=r.RevisionStoreErrorCode.CORRUPT_RECORD,
    )
    body = _parse_record(raw, r._HEAD_CHECKSUM_DOMAIN, r._MAX_HEAD_BYTES)
    head, code = r._head_from_record(body)
    if code is not None:
        _raise(code)
    if head.project_id != project_id:
        _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
    revision, _capability = _load_revision_context(
        context,
        project_id,
        head.revision_id,
    )
    if revision.manifest_sha256 != head.manifest_sha256:
        _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
    return head


def load_head(store, project_id):
    context = _project_context(store, project_id)
    return _load_head_context(context, project_id)


def probe_candidate_reservation(
    store,
    project_id,
    base_revision,
    reservation_key,
    key_digest,
):
    """Observe an exact pre-candidate reservation under the Win32 quota fence."""

    r = _r()
    with _quota_mutation(store):
        context = _project_context(store, project_id)
        head = _load_head_context(context, project_id)
        reservations, _snapshot = _quota_admission_state(store)
        project_reservations = tuple(
            reservation for reservation in reservations if reservation["project_id"] == project_id
        )
        status = r.CandidateReservationPresenceStatus.AMBIGUOUS
        revision_id = None
        if not project_reservations:
            status = r.CandidateReservationPresenceStatus.ABSENT
        elif len(project_reservations) == 1 and project_reservations[0]["key_sha256"] != key_digest:
            status = r.CandidateReservationPresenceStatus.ABSENT
        elif len(project_reservations) == 1:
            reservation = project_reservations[0]
            expected_ceiling_files = (
                9 if re.fullmatch(r"revert:[0-9a-f]{64}", reservation_key) is not None else 8
            )
            if (
                reservation["kind"] == "candidate"
                and reservation["expected_head"] == head
                and head.revision_id == base_revision
                and reservation["state"] == "reserved"
                and reservation["project_temp"] is None
                and reservation["revision_temp"] is None
                and reservation["ceiling_files"] == expected_ceiling_files
            ):
                status = r.CandidateReservationPresenceStatus.EXACT_PRE_CANDIDATE
                revision_id = reservation["revision_id"]
        validate_windows_path(context["project"], directory=True)
        validate_windows_path(context["root"], directory=True)
        return r.CandidateReservationPresence(
            project_id=project_id,
            status=status,
            head=head,
            revision_id=revision_id,
        )


def load_revision(store, project_id, revision_id):
    context = _project_context(store, project_id)
    return _load_revision_context(context, project_id, revision_id)[0]


def _head_record_only(context, project_id):
    r = _r()
    raw, _capability = _read_file(
        context["project"],
        "HEAD.json",
        r._MAX_HEAD_BYTES,
        missing_code=r.RevisionStoreErrorCode.CORRUPT_RECORD,
    )
    body = _parse_record(raw, r._HEAD_CHECKSUM_DOMAIN, r._MAX_HEAD_BYTES)
    head, code = r._head_from_record(body)
    if code is not None:
        _raise(code)
    if head.project_id != project_id:
        _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
    return head


def _observe_model_binding(
    revision_capability: WindowsPathCapability,
    reference,
):
    r = _r()
    path = _child_path(revision_capability, reference.name)
    fd = None
    capability = None
    try:
        fd, capability = open_private_file(
            path,
            create=False,
            read_write=False,
            expected_parent=revision_capability,
        )
        before = os.fstat(fd)
        if before.st_size != reference.size_bytes or before.st_size <= 0:
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
        digest = hashlib.sha256()
        remaining = int(before.st_size)
        while remaining:
            chunk = os.read(fd, min(r._COPY_CHUNK_BYTES, remaining))
            if not chunk or len(chunk) > remaining:
                raise OSError
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        before_birthtime = int(getattr(before, "st_birthtime_ns", before.st_ctime_ns))
        after_birthtime = int(getattr(after, "st_birthtime_ns", after.st_ctime_ns))
        pinned = capture_windows_fd(
            fd,
            directory=False,
            generation_token=capability.generation_token,
        )
        if (
            pinned != capability
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_mode != after.st_mode
            or before.st_nlink != after.st_nlink
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before_birthtime != after_birthtime
            or digest.hexdigest() != reference.sha256
        ):
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
        binding = r.RevisionSourceBinding(
            dev=int(after.st_dev),
            ino=int(after.st_ino),
            mode=int(after.st_mode),
            uid=int(after.st_uid),
            nlink=int(after.st_nlink),
            size=int(after.st_size),
            mtime_ns=int(after.st_mtime_ns),
            ctime_ns=after_birthtime,
        )
        os.close(fd)
        fd = None
        validate_windows_path(capability, directory=False)
        validate_windows_path(revision_capability, directory=True)
        return binding
    except FileNotFoundError:
        _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
    except r.RevisionStoreError:
        raise
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def observe_model_source(store, project_id, revision_id):
    """Observe one model under Windows File-ID and private-DACL authority."""

    r = _r()
    project_code = r._identifier_code(project_id, r._PROJECT_PATTERN)
    if project_code is not None:
        _raise(project_code)
    if revision_id is not None:
        revision_code = r._identifier_code(revision_id, r._REVISION_PATTERN)
        if revision_code is not None:
            _raise(revision_code)
    attempt = 0
    while attempt < r._MAX_RECORD_OPEN_ATTEMPTS:
        attempt += 1
        context = _project_context(store, project_id)
        head_before = _head_record_only(context, project_id)
        target_revision_id = revision_id
        if target_revision_id is None:
            target_revision_id = head_before.revision_id
        target, target_capability = _load_revision_context(
            context,
            project_id,
            target_revision_id,
        )
        if target.model is None:
            _raise(r.RevisionStoreErrorCode.NOT_FOUND)
        if target_revision_id == head_before.revision_id:
            head_revision = target
        else:
            head_revision, _head_capability = _load_revision_context(
                context,
                project_id,
                head_before.revision_id,
            )
        if (
            head_revision.id != head_before.revision_id
            or head_revision.project_id != project_id
            or head_revision.manifest_sha256 != head_before.manifest_sha256
        ):
            _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
        binding = _observe_model_binding(target_capability, target.model)
        head_after = _head_record_only(context, project_id)
        if head_after != head_before:
            continue
        validate_windows_path(context["project"], directory=True)
        validate_windows_path(context["revisions"], directory=True)
        validate_windows_path(target_capability, directory=True)
        _root(store)
        model_path = Path(
            windows_extended_path(
                context["revisions_path"] / r._revision_key(target_revision_id) / target.model.name
            )
        )
        return r.RevisionSourceObservation(
            head=head_after,
            revision=target,
            model_path=model_path,
            model_binding=binding,
        )
    _raise(r.RevisionStoreErrorCode.CONFLICT)


def _capability_identity(capability: WindowsPathCapability) -> tuple[object, ...]:
    """Return the stable, wire-independent portion of a path capability."""

    return (
        f"{capability.volume:016x}",
        f"{capability.file_id:032x}",
        capability.owner_sid,
        capability.security_sha256,
    )


def _capability_stat(
    capability: WindowsPathCapability,
    *,
    directory: bool,
) -> tuple[object, ...]:
    """Capture metadata while the identity-pinned path remains authoritative."""

    r = _r()
    try:
        path = validate_windows_path(capability, directory=directory)
        value = os.lstat(windows_extended_path(path))
        attributes = int(getattr(value, "st_file_attributes", 0))
        if (
            stat.S_ISDIR(value.st_mode) != directory
            or stat.S_ISLNK(value.st_mode)
            or bool(attributes & 0x400)
            or (not directory and value.st_nlink != 1)
        ):
            raise OSError
        validate_windows_path(capability, directory=directory)
        return _capability_identity(capability) + (
            int(value.st_mode),
            int(value.st_nlink),
            int(value.st_size),
            int(value.st_mtime_ns),
            int(value.st_ctime_ns),
        )
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)


def _snapshot_directory(
    capability: WindowsPathCapability,
    verification: dict[str, list[object]],
) -> tuple[str, ...]:
    names = _directory_names(capability)
    identity = _capability_stat(capability, directory=True)
    verification["directories"].append((capability, names, identity))
    return names


def _snapshot_record(
    parent: WindowsPathCapability,
    name: str,
    maximum: int,
    missing_code,
    verification: dict[str, list[object]],
    usage: dict[str, int],
) -> tuple[bytes, WindowsPathCapability, tuple[object, ...]]:
    raw, capability = _read_file(
        parent,
        name,
        maximum,
        missing_code=missing_code,
    )
    identity = _capability_stat(capability, directory=False)
    verification["records"].append((parent, name, maximum, missing_code, raw, capability, identity))
    usage["files"] += 1
    usage["bytes"] += len(raw)
    return raw, capability, identity


def _snapshot_content(
    parent: WindowsPathCapability,
    name: str,
    expected_size: int | None,
    verification: dict[str, list[object]],
    usage: dict[str, int],
    *,
    immutable: bool,
) -> tuple[object, ...]:
    """Validate content metadata without streaming or hashing CAD bytes."""

    r = _r()
    path = _child_path(parent, name)
    fd = None
    capability = None
    try:
        fd, capability = open_private_file(
            path,
            create=False,
            read_write=False,
            expected_parent=parent,
        )
        before = os.fstat(fd)
        size = int(before.st_size)
        if size < 0 or size > r._MAX_FILE_BYTES:
            _raise(
                r.RevisionStoreErrorCode.BUDGET_EXCEEDED
                if size > r._MAX_FILE_BYTES
                else r.RevisionStoreErrorCode.CORRUPT_CONTENT
            )
        if expected_size is not None and size != expected_size:
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
        pinned = capture_windows_fd(
            fd,
            directory=False,
            generation_token=capability.generation_token,
        )
        after = os.fstat(fd)
        if (
            pinned != capability
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise OSError
        os.close(fd)
        fd = None
        validate_windows_path(capability, directory=False)
        validate_windows_path(parent, directory=True)
        identity = _capability_identity(capability) + (
            int(after.st_mode),
            int(after.st_nlink),
            size,
            int(after.st_mtime_ns),
            int(after.st_ctime_ns),
        )
        if immutable:
            verification["files"].append((parent, name, expected_size, capability, identity))
        usage["files"] += 1
        usage["bytes"] += size
        return identity
    except FileNotFoundError:
        _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
    except r.RevisionStoreError:
        raise
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _verify_snapshot(verification: dict[str, list[object]]) -> None:
    r = _r()
    for item in verification["records"]:
        parent, name, maximum, missing_code, expected_raw, expected, identity = item
        raw, current = _read_file(
            parent,
            name,
            maximum,
            missing_code=missing_code,
        )
        if (
            raw != expected_raw
            or not _same_object(current, expected)
            or _capability_stat(current, directory=False) != identity
        ):
            _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    for item in verification["files"]:
        parent, name, expected_size, expected, identity = item
        scratch = {"directories": [], "records": [], "files": []}
        usage = {"bytes": 0, "files": 0}
        current_identity = _snapshot_content(
            parent,
            name,
            expected_size,
            scratch,
            usage,
            immutable=False,
        )
        try:
            current = capture_windows_path(_child_path(parent, name), directory=False)
        except (OSError, TypeError, ValueError):
            _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
        if not _same_object(current, expected) or current_identity != identity:
            _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    for capability, names, identity in verification["directories"]:
        if (
            _directory_names(capability) != names
            or _capability_stat(capability, directory=True) != identity
        ):
            _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)


def _snapshot_reservations(
    root_path: Path,
    root: WindowsPathCapability,
    root_names: tuple[str, ...],
    verification: dict[str, list[object]],
    usage: dict[str, int],
) -> tuple[tuple[dict[str, object], ...], dict[str, tuple[dict[str, object], ...]]]:
    r = _r()
    if r._QUOTA_DIRECTORY not in root_names:
        return (), {}
    quota = _capture_directory(
        root_path / r._QUOTA_DIRECTORY,
        root,
        missing_code=r.RevisionStoreErrorCode.UNSAFE_STORE,
    )
    quota_names = _snapshot_directory(quota, verification)
    if quota_names != (r._RESERVATIONS_DIRECTORY,):
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    reservations = _capture_directory(
        root_path / r._QUOTA_DIRECTORY / r._RESERVATIONS_DIRECTORY,
        quota,
        missing_code=r.RevisionStoreErrorCode.UNSAFE_STORE,
    )
    names = _snapshot_directory(reservations, verification)
    values: list[dict[str, object]] = []
    for name in names:
        if re.fullmatch(r"[0-9a-f]{64}", name) is None:
            _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
        directory = _capture_directory(
            Path(reservations.path) / name,
            reservations,
            missing_code=r.RevisionStoreErrorCode.CORRUPT_RECORD,
        )
        if _snapshot_directory(directory, verification) != (r._RESERVATION_RECORD,):
            _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
        raw, _capability, _identity = _snapshot_record(
            directory,
            r._RESERVATION_RECORD,
            r._MAX_JOURNAL_BYTES,
            r.RevisionStoreErrorCode.CORRUPT_RECORD,
            verification,
            usage,
        )
        body = _parse_record(raw, r._RESERVATION_CHECKSUM_DOMAIN, r._MAX_JOURNAL_BYTES)
        parsed, code = r._parse_reservation_body(body)
        if code is not None:
            _raise(code)
        if r._revision_key(parsed["revision_id"]) != name:
            _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
        values.append(parsed)
    ordered = tuple(sorted(values, key=lambda value: value["revision_id"]))
    index: dict[str, list[dict[str, object]]] = {}
    for reservation in ordered:
        project_values = index.setdefault(reservation["project_id"], [])
        project_values.append(reservation)
    return ordered, {
        project_id: tuple(project_values) for project_id, project_values in index.items()
    }


def _quota_file_observation(
    parent: WindowsPathCapability,
    name: str,
) -> tuple[WindowsPathCapability, tuple[object, ...], int]:
    """Observe one quota file through an identity-pinned private handle."""

    r = _r()
    path = _child_path(parent, name)
    fd = None
    try:
        fd, capability = open_private_file(
            path,
            create=False,
            read_write=False,
            expected_parent=parent,
        )
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 0
        ):
            raise OSError
        pinned = capture_windows_fd(
            fd,
            directory=False,
            generation_token=capability.generation_token,
        )
        after = os.fstat(fd)
        if (
            pinned != capability
            or before.st_mode != after.st_mode
            or before.st_nlink != after.st_nlink
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise OSError
        os.close(fd)
        fd = None
        validate_windows_path(capability, directory=False)
        validate_windows_path(parent, directory=True)
        identity = _capability_identity(capability) + (
            int(after.st_mode),
            int(after.st_nlink),
            int(after.st_size),
            int(after.st_mtime_ns),
            int(after.st_ctime_ns),
        )
        return capability, identity, int(after.st_size)
    except r.RevisionStoreError:
        raise
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _scan_quota_tree(
    directory: WindowsPathCapability,
    relative: tuple[str, ...],
    snapshot: dict[str, object],
    verification: dict[str, list[object]],
) -> None:
    """Mirror the POSIX quota walk using File-ID/DACL-pinned children."""

    r = _r()
    if len(relative) > 4:
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    names = _snapshot_directory(directory, verification)
    for name in names:
        if type(name) is not str or name in {".", ".."}:
            _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
        path = _child_path(directory, name)
        try:
            entry = os.lstat(windows_extended_path(path))
            attributes = int(getattr(entry, "st_file_attributes", 0))
        except (OSError, TypeError, ValueError):
            _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
        is_directory = stat.S_ISDIR(entry.st_mode)
        is_file = stat.S_ISREG(entry.st_mode)
        if (
            (not is_directory and not is_file)
            or stat.S_ISLNK(entry.st_mode)
            or bool(attributes & 0x400)
            or not r._quota_entry_allowed(relative, name, is_directory)
        ):
            _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
        child_relative = relative + (name,)
        if is_file:
            capability, identity, size = _quota_file_observation(directory, name)
            verification["quota_files"].append((directory, name, capability, identity))
            snapshot["bytes"] += size
            snapshot["files"] += 1
            snapshot["file_sizes"][child_relative] = size
            if r._quota_temporary_path(child_relative):
                snapshot["temporary_entries"][child_relative] = True
            if snapshot["files"] > r._MAX_ORDINARY_FILES:
                snapshot["over_limit"] = True
            continue

        child = _capture_directory(
            path,
            directory,
            missing_code=r.RevisionStoreErrorCode.UNSAFE_STORE,
        )
        category = None
        if len(relative) == 0 and (
            re.fullmatch(r"[0-9a-f]{64}", name) is not None
            or re.fullmatch(r"\.project\.[0-9a-f]{32}\.tmp", name) is not None
        ):
            category = "projects"
            snapshot["projects"] += 1
        if (
            len(relative) == 2
            and relative[1] == "revisions"
            and (
                re.fullmatch(r"[0-9a-f]{64}", name) is not None
                or re.fullmatch(r"\.revision\.[0-9a-f]{32}\.tmp", name) is not None
            )
        ):
            category = "revisions"
            snapshot["revisions"] += 1
        if (
            len(relative) == 2
            and relative[1] == "candidates"
            and re.fullmatch(r"[0-9a-f]{64}", name) is not None
        ) or relative == (r._QUOTA_DIRECTORY, r._RESERVATIONS_DIRECTORY):
            category = "candidate_reservations"
            snapshot["candidate_reservations"] += 1
        if category is not None:
            snapshot["directory_categories"][child_relative] = category
        if r._quota_temporary_path(child_relative):
            snapshot["temporary_entries"][child_relative] = True
        _scan_quota_tree(child, child_relative, snapshot, verification)

    if (
        snapshot["bytes"] > r._MAX_STORE_BYTES
        or snapshot["projects"] > r._MAX_PROJECTS
        or snapshot["revisions"] > r._MAX_REVISIONS
        or snapshot["candidate_reservations"] > r._MAX_CANDIDATES_AND_RESERVATIONS
    ):
        snapshot["over_limit"] = True


def _verify_quota_files(verification: dict[str, list[object]]) -> None:
    r = _r()
    for parent, name, expected, expected_identity in verification["quota_files"]:
        current, current_identity, _size = _quota_file_observation(parent, name)
        if not _same_object(current, expected) or current_identity != expected_identity:
            _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)


def _quota_admission_state(store):
    """Return reservations and one race-checked physical quota snapshot."""

    r = _r()
    root_path, root = _root(store)
    verification: dict[str, list[object]] = {
        "directories": [],
        "records": [],
        "files": [],
        "quota_files": [],
    }
    root_names = _snapshot_directory(root, verification)
    parse_usage = {"bytes": 0, "files": 0}
    reservations, _reservation_index = _snapshot_reservations(
        root_path,
        root,
        root_names,
        verification,
        parse_usage,
    )
    prefix_owner = {}
    journal_owner = {}
    for reservation in reservations:
        revision_id = reservation["revision_id"]
        for prefix in r._reservation_prefixes(reservation):
            if prefix in prefix_owner:
                _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
            prefix_owner[prefix] = revision_id
        if reservation["kind"] == "candidate":
            project_key = r._project_key(reservation["project_id"])
            if project_key in journal_owner:
                _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
            journal_owner[project_key] = revision_id

    snapshot = {
        "bytes": 0,
        "files": 0,
        "projects": 0,
        "revisions": 0,
        "candidate_reservations": 0,
        "file_sizes": {},
        "directory_categories": {},
        "temporary_entries": {},
        "over_limit": False,
    }
    _scan_quota_tree(root, (), snapshot, verification)
    observed_bytes = {}
    observed_files = {}
    observed_directories = {}
    for relative, size in snapshot["file_sizes"].items():
        owner = r._quota_path_owner(relative, prefix_owner, journal_owner)
        if owner == r._QUOTA_OWNER_CONFLICT:
            _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
        if owner is not None:
            observed_bytes[owner] = observed_bytes.get(owner, 0) + size
            observed_files[owner] = observed_files.get(owner, 0) + 1
    for relative, category in snapshot["directory_categories"].items():
        owner = r._quota_path_owner(relative, prefix_owner, journal_owner)
        if owner == r._QUOTA_OWNER_CONFLICT:
            _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
        if owner is not None:
            owned = observed_directories.setdefault(
                owner,
                {"projects": 0, "revisions": 0, "candidate_reservations": 0},
            )
            owned[category] += 1
    observed_temporary_entries = {}
    unowned_temporary_entries = 0
    for relative in snapshot["temporary_entries"]:
        owner = r._quota_path_owner(relative, prefix_owner, journal_owner)
        if owner == r._QUOTA_OWNER_CONFLICT:
            _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
        if owner is None:
            unowned_temporary_entries += 1
        else:
            observed_temporary_entries[owner] = observed_temporary_entries.get(owner, 0) + 1
    snapshot["observed_bytes"] = observed_bytes
    snapshot["observed_files"] = observed_files
    snapshot["observed_directories"] = observed_directories
    snapshot["observed_temporary_entries"] = observed_temporary_entries
    snapshot["unowned_temporary_entries"] = unowned_temporary_entries
    _verify_snapshot(verification)
    _verify_quota_files(verification)
    return reservations, snapshot


def validate_candidate_file_budget_under_lease(store) -> None:
    """Enforce CAD file ceilings while the caller owns the quota lease."""

    r = _r()
    _reservations, snapshot = _quota_admission_state(store)
    for relative, size in snapshot["file_sizes"].items():
        if (
            len(relative) == 4
            and relative[1] == "candidates"
            and relative[3] in {"model.FCStd", "model.step"}
            and size > r._MAX_CANDIDATE_FILE_BYTES
        ):
            _raise(r.RevisionStoreErrorCode.BUDGET_EXCEEDED)


def validate_candidate_file_budget(store) -> None:
    """Enforce the per-file CAD writer ceiling after a Windows callback."""

    with _quota_mutation(store):
        validate_candidate_file_budget_under_lease(store)


def _snapshot_manifest(
    revisions: WindowsPathCapability,
    revisions_path: Path,
    project_id: str,
    physical_name: str,
    verification: dict[str, list[object]],
    usage: dict[str, int],
):
    r = _r()
    directory = _capture_directory(
        revisions_path / physical_name,
        revisions,
        missing_code=r.RevisionStoreErrorCode.CORRUPT_RECORD,
    )
    names = _snapshot_directory(directory, verification)
    raw, _manifest, manifest_identity = _snapshot_record(
        directory,
        "manifest.json",
        r._MAX_MANIFEST_BYTES,
        r.RevisionStoreErrorCode.CORRUPT_RECORD,
        verification,
        usage,
    )
    body = _parse_record(raw, r._MANIFEST_CHECKSUM_DOMAIN, r._MAX_MANIFEST_BYTES)
    revision, code = r._revision_from_manifest(body, raw)
    if code is not None:
        _raise(code)
    if revision.project_id != project_id or r._revision_key(revision.id) != physical_name:
        _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
    expected_names = {"manifest.json"}
    references = ()
    if revision.model is not None:
        expected_names.add(revision.model.name)
        references = (revision.model,)
    for artifact in revision.artifacts:
        expected_names.add(artifact.name)
        references = references + (artifact,)
    if set(names) != expected_names or len(names) != len(expected_names):
        _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
    content_identities = []
    for reference in references:
        identity = _snapshot_content(
            directory,
            reference.name,
            reference.size_bytes,
            verification,
            usage,
            immutable=True,
        )
        content_identities.append((reference.name, identity))
    return revision, (
        revision.id,
        hashlib.sha256(raw).hexdigest(),
        manifest_identity,
        _capability_stat(directory, directory=True),
        tuple(content_identities),
    )


def _snapshot_candidate(
    candidates: WindowsPathCapability,
    candidates_path: Path,
    revision_id: str,
    reservation,
    verification: dict[str, list[object]],
    usage: dict[str, int],
) -> None:
    r = _r()
    name = r._candidate_key(revision_id)
    candidate = _capture_directory(
        candidates_path / name,
        candidates,
        missing_code=r.RevisionStoreErrorCode.RECOVERY_REQUIRED,
    )
    names = _snapshot_directory(candidate, verification)
    seeded = reservation["ceiling_files"] == 9
    expected = {"model.FCStd", "model.step"}
    if seeded:
        controls = {
            r._SEED_INTENT_RECORD,
            r._SEED_BINDING_RECORD,
        }.intersection(names)
        if len(controls) != 1:
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        expected.update(controls)
    if set(names) != expected or len(names) != len(expected):
        _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
    total_size = 0
    for content_name in ("model.FCStd", "model.step"):
        identity = _snapshot_content(
            candidate,
            content_name,
            None,
            verification,
            usage,
            immutable=False,
        )
        total_size += int(identity[-3])
    if total_size > r._MAX_REVISION_BYTES:
        _raise(r.RevisionStoreErrorCode.BUDGET_EXCEEDED)
    if seeded:
        control_name = next(iter(controls))
        domain = r._SEED_INTENT_CHECKSUM_DOMAIN
        parser = r._seed_intent_from_body
        if control_name == r._SEED_BINDING_RECORD:
            domain = r._SEED_BINDING_CHECKSUM_DOMAIN
            parser = r._seed_binding_from_body
        raw, _capability, _identity = _snapshot_record(
            candidate,
            control_name,
            r._MAX_JOURNAL_BYTES,
            r.RevisionStoreErrorCode.RECOVERY_REQUIRED,
            verification,
            usage,
        )
        body = _parse_record(raw, domain, r._MAX_JOURNAL_BYTES)
        parsed, code = parser(body)
        if code is not None:
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        code = r._seed_control_binding_code(
            parsed,
            reservation["project_id"],
            reservation["revision_id"],
            reservation["expected_head"],
            reservation["key_sha256"],
        )
        if code is not None:
            _raise(code)


def _snapshot_journal_code(
    *,
    project_id,
    head,
    journal,
    revisions,
    depths,
    reservations,
    candidate_names,
    candidates,
    candidates_path,
    verification,
    usage,
):
    r = _r()
    if journal is None:
        if reservations or candidate_names:
            return r.RevisionStoreErrorCode.RECOVERY_REQUIRED
        return None
    if journal.project_id != project_id or not r._head_matches_discovery_graph(
        journal.expected_head,
        revisions,
        depths,
    ):
        return r.RevisionStoreErrorCode.RECOVERY_REQUIRED
    if journal.state is r.CommitJournalState.STAGING:
        if (
            head != journal.expected_head
            or journal.candidate_revision in revisions
            or len(reservations) != 1
            or candidate_names != (r._candidate_key(journal.candidate_revision),)
        ):
            return r.RevisionStoreErrorCode.RECOVERY_REQUIRED
        reservation = reservations[0]
        if (
            reservation["kind"] != "candidate"
            or reservation["project_id"] != project_id
            or reservation["revision_id"] != journal.candidate_revision
            or reservation["expected_head"] != journal.expected_head
            or reservation["state"] != "staged"
            or reservation["project_temp"] is not None
            or reservation["revision_temp"] is not None
        ):
            return r.RevisionStoreErrorCode.RECOVERY_REQUIRED
        _snapshot_candidate(
            candidates,
            candidates_path,
            journal.candidate_revision,
            reservation,
            verification,
            usage,
        )
        return None
    if reservations or candidate_names:
        return r.RevisionStoreErrorCode.RECOVERY_REQUIRED
    sealed = revisions.get(journal.candidate_revision)
    if journal.state is r.CommitJournalState.PREPARED:
        if (
            sealed is None
            or sealed.base_revision != journal.expected_head.revision_id
            or sealed.manifest_sha256 != journal.manifest_sha256
        ):
            return r.RevisionStoreErrorCode.RECOVERY_REQUIRED
        if head != journal.expected_head and not r._new_head_matches(head, journal):
            return r.RevisionStoreErrorCode.RECOVERY_REQUIRED
        return None
    if journal.state is r.CommitJournalState.COMMITTED:
        if (
            not r._new_head_matches(head, journal)
            or sealed is None
            or sealed.base_revision != journal.expected_head.revision_id
            or sealed.manifest_sha256 != journal.manifest_sha256
        ):
            return r.RevisionStoreErrorCode.RECOVERY_REQUIRED
        return None
    if journal.state is r.CommitJournalState.NOT_COMMITTED:
        if head != journal.expected_head:
            return r.RevisionStoreErrorCode.RECOVERY_REQUIRED
        if sealed is None:
            if journal.manifest_sha256 != journal.expected_head.manifest_sha256:
                return r.RevisionStoreErrorCode.RECOVERY_REQUIRED
        elif (
            sealed.base_revision != journal.expected_head.revision_id
            or sealed.manifest_sha256 != journal.manifest_sha256
        ):
            return r.RevisionStoreErrorCode.RECOVERY_REQUIRED
        return None
    return r.RevisionStoreErrorCode.RECOVERY_REQUIRED


def _snapshot_project(
    root_path: Path,
    root: WindowsPathCapability,
    physical_name: str,
    reservation_index,
    usage: dict[str, int],
):
    r = _r()
    verification: dict[str, list[object]] = {
        "directories": [],
        "records": [],
        "files": [],
    }
    project_path = root_path / physical_name
    project = _capture_directory(
        project_path,
        root,
        missing_code=r.RevisionStoreErrorCode.CORRUPT_RECORD,
    )
    names = _snapshot_directory(project, verification)
    allowed = {"HEAD.json", "journal.json", "revisions", "candidates"}
    if not set(names).issubset(allowed):
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    if not {"HEAD.json", "revisions", "candidates"}.issubset(names):
        _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
    head_raw, _head_capability, head_identity = _snapshot_record(
        project,
        "HEAD.json",
        r._MAX_HEAD_BYTES,
        r.RevisionStoreErrorCode.CORRUPT_RECORD,
        verification,
        usage,
    )
    head_body = _parse_record(head_raw, r._HEAD_CHECKSUM_DOMAIN, r._MAX_HEAD_BYTES)
    head, code = r._head_from_record(head_body)
    if code is not None:
        _raise(code)
    if r._project_key(head.project_id) != physical_name:
        _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
    journal = None
    journal_identity = None
    if "journal.json" in names:
        journal_raw, _journal_capability, journal_stat = _snapshot_record(
            project,
            "journal.json",
            r._MAX_JOURNAL_BYTES,
            r.RevisionStoreErrorCode.CORRUPT_RECORD,
            verification,
            usage,
        )
        journal_body = _parse_record(
            journal_raw,
            r._JOURNAL_CHECKSUM_DOMAIN,
            r._MAX_JOURNAL_BYTES,
        )
        journal, code = r._journal_from_record(journal_body)
        if code is not None:
            _raise(code)
        journal_identity = (hashlib.sha256(journal_raw).hexdigest(), journal_stat)
    revisions_path = project_path / "revisions"
    candidates_path = project_path / "candidates"
    revisions_capability = _capture_directory(revisions_path, project)
    candidates_capability = _capture_directory(candidates_path, project)
    revision_names = _snapshot_directory(revisions_capability, verification)
    candidate_names = _snapshot_directory(candidates_capability, verification)
    revisions = {}
    revision_identities = []
    for name in revision_names:
        if re.fullmatch(r"[0-9a-f]{64}", name) is None:
            _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
        revision, identity = _snapshot_manifest(
            revisions_capability,
            revisions_path,
            head.project_id,
            name,
            verification,
            usage,
        )
        if revision.id in revisions:
            _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
        revisions[revision.id] = revision
        revision_identities.append(identity)
        usage["revisions"] += 1
    if not revisions:
        _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
    depths, code = r._discovery_depths(revisions)
    if code is not None:
        _raise(code)
    if not r._head_matches_discovery_graph(head, revisions, depths):
        _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
    for name in candidate_names:
        if re.fullmatch(r"[0-9a-f]{64}", name) is None:
            _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
        # Capture every candidate directory even when journal validation will
        # reject it, so reparse points and unsafe DACLs never become a softer
        # recovery error.
        _capture_directory(
            candidates_path / name,
            candidates_capability,
            missing_code=r.RevisionStoreErrorCode.UNSAFE_STORE,
        )
        usage["candidate_reservations"] += 1
    reservations = r._discovery_reservations_for_project(
        reservation_index,
        head.project_id,
    )
    code = _snapshot_journal_code(
        project_id=head.project_id,
        head=head,
        journal=journal,
        revisions=revisions,
        depths=depths,
        reservations=reservations,
        candidate_names=candidate_names,
        candidates=candidates_capability,
        candidates_path=candidates_path,
        verification=verification,
        usage=usage,
    )
    if code is not None:
        _raise(code)
    ancestry = []
    current = head.revision_id
    seen = set()
    while current is not None:
        if current in seen or current not in revisions:
            _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
        seen.add(current)
        revision = revisions[current]
        ancestry.append(
            r.RevisionSnapshotEntry(
                id=revision.id,
                project_id=revision.project_id,
                base_revision=revision.base_revision,
                manifest_sha256=revision.manifest_sha256,
            )
        )
        current = revision.base_revision
    if len(ancestry) != head.generation + 1:
        _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
    reservation_identities = []
    for reservation in reservations:
        body = r._reservation_body(
            reservation["kind"],
            reservation["project_id"],
            reservation["expected_head"],
            reservation["revision_id"],
            reservation["key_sha256"],
            reservation["ceiling_files"],
            reservation["state"],
            reservation["project_temp"],
            reservation["revision_temp"],
        )
        reservation_identities.append(hashlib.sha256(r._canonical_bytes(body)).hexdigest())
    state_body = (
        head.project_id,
        hashlib.sha256(head_raw).hexdigest(),
        head_identity,
        _capability_stat(project, directory=True),
        journal_identity,
        tuple(sorted(revision_identities, key=lambda value: value[0])),
        tuple(reservation_identities),
        candidate_names,
    )
    _verify_snapshot(verification)
    state_sha256 = hashlib.sha256(
        r._DISCOVERY_PROJECT_STATE_DOMAIN + r._canonical_bytes(state_body)
    ).hexdigest()
    return r.RevisionAncestrySnapshot(
        project_id=head.project_id,
        head=head,
        revisions=tuple(sorted(ancestry, key=lambda value: value.id)),
        state_sha256=state_sha256,
    )


def snapshot_store(store):
    """Return a fail-closed, path-free snapshot of the complete Windows store."""

    r = _r()
    acquired, code = r._acquire_quota_lease(store)
    if code is not None:
        _raise(code)
    failure = None
    result = None
    release_code = None
    try:
        root_path, root = _root(store)
        verification: dict[str, list[object]] = {
            "directories": [],
            "records": [],
            "files": [],
        }
        root_names = _snapshot_directory(root, verification)
        usage = {
            "bytes": 0,
            "files": 0,
            "projects": 0,
            "revisions": 0,
            "candidate_reservations": 0,
        }
        reservations, reservation_index = _snapshot_reservations(
            root_path,
            root,
            root_names,
            verification,
            usage,
        )
        usage["candidate_reservations"] += len(reservations)
        physical_projects = []
        for name in root_names:
            if name == r._QUOTA_DIRECTORY:
                continue
            if re.fullmatch(r"\.project\.[0-9a-f]{32}\.tmp", name) is not None:
                _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
            if re.fullmatch(r"[0-9a-f]{64}", name) is None:
                _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
            physical_projects.append(name)
        usage["projects"] = len(physical_projects)
        discovered = []
        identifiers = set()
        for name in physical_projects:
            snapshot = _snapshot_project(
                root_path,
                root,
                name,
                reservation_index,
                usage,
            )
            if snapshot.project_id in identifiers:
                _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
            identifiers.add(snapshot.project_id)
            discovered.append(snapshot)
        for reservation in reservations:
            if reservation["kind"] != "candidate" or reservation["project_id"] not in identifiers:
                _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        if (
            usage["bytes"] > r._MAX_STORE_BYTES
            or usage["files"] > r._MAX_ORDINARY_FILES
            or usage["projects"] > r._MAX_PROJECTS
            or usage["revisions"] > r._MAX_REVISIONS
            or usage["candidate_reservations"] > r._MAX_CANDIDATES_AND_RESERVATIONS
        ):
            _raise(r.RevisionStoreErrorCode.RESOURCE_EXHAUSTED)
        _verify_snapshot(verification)
        ordered = tuple(sorted(discovered, key=lambda value: value.project_id))
        ancestries = {value.project_id: value for value in ordered}
        projects = tuple(
            r.ProjectSnapshotEntry(
                project_id=value.project_id,
                generation=value.head.generation,
                revision_id=value.head.revision_id,
                manifest_sha256=value.head.manifest_sha256,
                state_sha256=value.state_sha256,
            )
            for value in ordered
        )
        result = (projects, ancestries)
    except r.RevisionStoreError as error:
        failure = error
    finally:
        release_code = r._release_quota_lease(acquired)
    if failure is not None:
        raise failure
    if release_code is not None or result is None:
        _raise(r.RevisionStoreErrorCode.IO_ERROR)
    return result


def _load_journal(context):
    r = _r()
    path = context["project_path"] / "journal.json"
    if _entry_missing(path):
        return None
    raw, _capability = _read_file(
        context["project"],
        "journal.json",
        r._MAX_JOURNAL_BYTES,
        missing_code=r.RevisionStoreErrorCode.CORRUPT_RECORD,
    )
    body = _parse_record(raw, r._JOURNAL_CHECKSUM_DOMAIN, r._MAX_JOURNAL_BYTES)
    journal, code = r._journal_from_record(body)
    if code is not None:
        _raise(code)
    return journal


def _write_journal(context, journal):
    r = _r()
    raw = r._checked_record_bytes(r._journal_mapping(journal), r._JOURNAL_CHECKSUM_DOMAIN)
    return _replace_record(context["project"], "journal.json", raw)


def _quota_context(store, *, create: bool):
    r = _r()
    root_path, root_capability = _root(store)
    quota_path = root_path / r._QUOTA_DIRECTORY
    reservations_path = quota_path / r._RESERVATIONS_DIRECTORY
    if create:
        quota = _create_directory(quota_path, root_capability)
        reservations = _create_directory(reservations_path, quota)
    else:
        quota = _capture_directory(quota_path, root_capability)
        reservations = _capture_directory(reservations_path, quota)
    return {
        "root": root_capability,
        "quota_path": quota_path,
        "quota": quota,
        "reservations_path": reservations_path,
        "reservations": reservations,
    }


def _reservation_context(store, revision_id, *, create: bool):
    r = _r()
    quota = _quota_context(store, create=create)
    name = r._revision_key(revision_id)
    path = quota["reservations_path"] / name
    if create:
        capability = _create_directory(path, quota["reservations"])
    else:
        capability = _capture_directory(path, quota["reservations"])
    quota.update({"reservation_path": path, "reservation": capability})
    return quota


def _load_reservation(store, revision_id):
    r = _r()
    context = _reservation_context(store, revision_id, create=False)
    raw, _capability = _read_file(
        context["reservation"],
        r._RESERVATION_RECORD,
        r._MAX_JOURNAL_BYTES,
        missing_code=r.RevisionStoreErrorCode.CORRUPT_RECORD,
    )
    body = _parse_record(raw, r._RESERVATION_CHECKSUM_DOMAIN, r._MAX_JOURNAL_BYTES)
    parsed, code = r._parse_reservation_body(body)
    if code is not None:
        _raise(code)
    if parsed["revision_id"] != revision_id:
        _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
    return parsed, context


def _write_reservation(context, reservation):
    r = _r()
    raw = r._checked_record_bytes(reservation, r._RESERVATION_CHECKSUM_DOMAIN)
    return _replace_record(context["reservation"], r._RESERVATION_RECORD, raw)


def _replace_reservation(store, reservation):
    context = _reservation_context(store, reservation["revision_id"], create=False)
    _write_reservation(context, reservation)
    return context


def _remove_reservation(store, revision_id) -> bool:
    r = _r()
    try:
        reservation, context = _load_reservation(store, revision_id)
        del reservation
        _delete_file(context["reservation"], r._RESERVATION_RECORD, missing_ok=False)
        if _directory_names(context["reservation"]):
            return True
        validate_windows_path(context["reservation"], directory=True)
        os.rmdir(windows_extended_path(context["reservation_path"]))
        validate_windows_path(context["reservations"], directory=True)
        return False
    except r.RevisionStoreError:
        return True
    except (OSError, TypeError, ValueError):
        return True


def _reservation_matches(
    reservation,
    *,
    project_id,
    expected_head,
    revision_id,
    key_digest,
    states,
) -> bool:
    return (
        reservation["kind"] == "candidate"
        and reservation["project_id"] == project_id
        and reservation["expected_head"] == expected_head
        and reservation["revision_id"] == revision_id
        and reservation["key_sha256"] == key_digest
        and reservation["state"] in states
        and reservation["project_temp"] is None
    )


def _candidate_context(context, revision_id):
    r = _r()
    name = r._candidate_key(revision_id)
    path = context["candidates_path"] / name
    capability = _capture_directory(path, context["candidates"])
    return name, path, capability


def _validate_candidate_entries(capability) -> tuple[str, ...]:
    r = _r()
    names = _directory_names(capability)
    allowed = {
        "model.FCStd",
        "model.step",
        r._SEED_INTENT_RECORD,
        r._SEED_BINDING_RECORD,
    }
    if "model.FCStd" not in names or "model.step" not in names or not set(names).issubset(allowed):
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    for name in names:
        try:
            capture_windows_path(_child_path(capability, name), directory=False)
        except (OSError, TypeError, ValueError):
            _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    validate_windows_path(capability, directory=True)
    return names


def _authority(store, project_id, revision_id, lease, *, expected_head=None):
    r = _r()
    _require_lease(store, project_id, lease)
    code = r._identifier_code(revision_id, r._REVISION_PATTERN)
    if code is not None:
        _raise(code)
    context = _project_context(store, project_id)
    head = _load_head_context(context, project_id)
    journal = _load_journal(context)
    if (
        journal is None
        or journal.state is not r.CommitJournalState.STAGING
        or journal.project_id != project_id
        or journal.candidate_revision != revision_id
        or journal.expected_head != head
        or (expected_head is not None and journal.expected_head != expected_head)
    ):
        _raise(r.RevisionStoreErrorCode.CONFLICT)
    candidate_name, candidate_path, candidate = _candidate_context(context, revision_id)
    _validate_candidate_entries(candidate)
    return context, head, journal, candidate_name, candidate_path, candidate


def candidate_path(store, project_id, revision_id, lease, name):
    context, _head, _journal, _candidate_name, path, capability = _authority(
        store,
        project_id,
        revision_id,
        lease,
    )
    del context
    _validate_candidate_entries(capability)
    _validate_lease_after(store, lease)
    return Path(windows_extended_path(path / name))


def revision_model_path(store, project_id, revision_id):
    r = _r()
    revision = load_revision(store, project_id, revision_id)
    if revision.model is None:
        _raise(r.RevisionStoreErrorCode.NOT_FOUND)
    path = (
        store._root
        / r._project_key(project_id)
        / "revisions"
        / r._revision_key(revision_id)
        / revision.model.name
    )
    return Path(windows_extended_path(path))


def revision_artifact_path(store, project_id, revision_id, artifact_id):
    r = _r()
    code = r._identifier_code(artifact_id, r._ARTIFACT_PATTERN)
    if code is not None:
        _raise(code)
    revision = load_revision(store, project_id, revision_id)
    found = None
    for artifact in revision.artifacts:
        if artifact.id == artifact_id:
            found = artifact
    if found is None:
        _raise(r.RevisionStoreErrorCode.NOT_FOUND)
    path = (
        store._root
        / r._project_key(project_id)
        / "revisions"
        / r._revision_key(revision_id)
        / found.name
    )
    return Path(windows_extended_path(path))


def _external_payload(source, expected_sha256, expected_size) -> bytes:
    r = _r()
    if type(source) is str:
        source_path = Path(source)
    elif type(source) is type(Path()):
        source_path = source
    else:
        _raise(r.RevisionStoreErrorCode.INVALID_INPUT)
    if not source_path.is_absolute() or ".." in source_path.parts:
        _raise(r.RevisionStoreErrorCode.INVALID_INPUT)
    source_path = Path(os.path.abspath(source_path))
    # An explicit import source is not part of the private revision namespace.
    # Pin it through a read-only CRT handle and require its path/file identity,
    # metadata and caller-supplied digest to remain exact across the read.  This
    # permits ordinary user files without weakening stored capability DACLs.
    fd = None
    try:
        before_path = os.lstat(windows_extended_path(source_path))
        attributes = int(getattr(before_path, "st_file_attributes", 0))
        if (
            not stat.S_ISREG(before_path.st_mode)
            or stat.S_ISLNK(before_path.st_mode)
            or bool(attributes & 0x400)
            or before_path.st_nlink != 1
            or before_path.st_size != expected_size
            or before_path.st_size <= 0
            or before_path.st_size > r._MAX_FILE_BYTES
        ):
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
        flags = os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT
        fd = os.open(windows_extended_path(source_path), flags)
        os.set_inheritable(fd, False)
        before_fd = os.fstat(fd)
        if (
            os.get_inheritable(fd)
            or before_fd.st_dev != before_path.st_dev
            or before_fd.st_ino != before_path.st_ino
            or before_fd.st_size != before_path.st_size
            or before_fd.st_mtime_ns != before_path.st_mtime_ns
        ):
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
        chunks: list[bytes] = []
        remaining = expected_size
        while remaining:
            chunk = os.read(fd, min(r._COPY_CHUNK_BYTES, remaining))
            if not chunk or len(chunk) > remaining:
                _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
            chunks.append(chunk)
            remaining -= len(chunk)
        after_fd = os.fstat(fd)
        after_path = os.lstat(windows_extended_path(source_path))
        identity = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(after_fd, field) != getattr(before_fd, field)
            or getattr(after_path, field) != getattr(before_path, field)
            for field in identity
        ):
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
        payload = b"".join(chunks)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
        return payload
    except FileNotFoundError:
        _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
    except r.RevisionStoreError:
        raise
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.IO_ERROR)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _external_payload_at(source_at, expected_sha256, expected_size) -> bytes:
    """Read one private source through an exact borrowed directory capability."""

    r = _r()
    if (
        type(source_at) is not tuple
        or len(source_at) != 3
        or type(source_at[0]) is not int
        or source_at[0] < 0
        or type(source_at[1]) is not str
        or re.fullmatch(r._SOURCE_NAME_PATTERN, source_at[1]) is None
        or type(source_at[2]) is not r.RevisionSourceBinding
    ):
        _raise(r.RevisionStoreErrorCode.INVALID_INPUT)
    source_parent_fd, source_name, expected_binding = source_at
    source_fd = None
    try:
        try:
            if os.get_inheritable(source_parent_fd):
                raise OSError
            source_parent = capture_windows_fd(source_parent_fd, directory=True)
            validate_windows_path(source_parent, directory=True)
            source_path = _child_path(source_parent, source_name)
            source_fd, source_capability = open_private_file(
                source_path,
                create=False,
                read_write=False,
                expected_parent=source_parent,
            )
        except (OSError, TypeError, ValueError):
            _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)

        before = os.fstat(source_fd)
        if (
            _windows_source_binding(before) != expected_binding
            or before.st_size != expected_size
            or source_capability.volume != source_parent.volume
        ):
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)

        payload = bytearray()
        digest = hashlib.sha256()
        remaining = expected_size
        while remaining:
            chunk = os.read(source_fd, min(r._COPY_CHUNK_BYTES, remaining))
            if not chunk or len(chunk) > remaining:
                _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
            payload.extend(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(source_fd, 1):
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)

        after = os.fstat(source_fd)
        source_after = capture_windows_fd(
            source_fd,
            directory=False,
            generation_token=source_capability.generation_token,
        )
        parent_after = capture_windows_fd(
            source_parent_fd,
            directory=True,
            generation_token=source_parent.generation_token,
        )
        if (
            digest.hexdigest() != expected_sha256
            or _windows_source_binding(after) != expected_binding
            or source_after != source_capability
            or parent_after != source_parent
        ):
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
        validate_windows_path(source_capability, directory=False)
        validate_windows_path(source_parent, directory=True)
        return bytes(payload)
    except r.RevisionStoreError:
        raise
    except FileNotFoundError:
        _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.IO_ERROR)
    finally:
        if source_fd is not None:
            try:
                os.close(source_fd)
            except OSError:
                pass


def _cleanup_generation_zero_temp(
    root_path: Path,
    root: WindowsPathCapability,
    temp_name: str,
    revision_id: str,
) -> bool:
    """Remove only the exact reservation-owned unpublished project namespace."""

    r = _r()
    temp_path = root_path / temp_name
    if _entry_missing(temp_path):
        return False
    try:
        temp = _capture_directory(
            temp_path,
            root,
            missing_code=r.RevisionStoreErrorCode.RECOVERY_REQUIRED,
        )
        names = set(_directory_names(temp))
        if not names.issubset({"HEAD.json", "revisions", "candidates"}):
            return True
        if "HEAD.json" in names:
            _delete_file(temp, "HEAD.json", missing_ok=False)
        if "candidates" in names:
            candidates_path = temp_path / "candidates"
            candidates = _capture_directory(candidates_path, temp)
            if _directory_names(candidates):
                return True
            delete_windows_directory(
                candidates_path,
                parent=temp,
                expected=candidates,
            )
        if "revisions" in names:
            revisions_path = temp_path / "revisions"
            revisions = _capture_directory(revisions_path, temp)
            revision_names = _directory_names(revisions)
            expected_revision = r._revision_key(revision_id)
            if any(name != expected_revision for name in revision_names):
                return True
            if revision_names:
                revision_path = revisions_path / expected_revision
                revision = _capture_directory(revision_path, revisions)
                contents = _directory_names(revision)
                if not set(contents).issubset({"model.FCStd", "manifest.json"}):
                    return True
                for name in contents:
                    _delete_file(revision, name, missing_ok=False)
                delete_windows_directory(
                    revision_path,
                    parent=revisions,
                    expected=revision,
                )
            delete_windows_directory(
                revisions_path,
                parent=temp,
                expected=revisions,
            )
        if _directory_names(temp):
            return True
        delete_windows_directory(temp_path, parent=root, expected=temp)
        validate_windows_path(root, directory=True)
        return False
    except (OSError, TypeError, ValueError, r.RevisionStoreError):
        return True


def _converge_generation_zero(
    store,
    project_id,
    expected_sha256,
    expected_size,
    reservation_key,
    ceiling_files,
):
    r = _r()
    acquired, code = r._acquire_quota_lease(store)
    if code is not None:
        return code
    reservation = None
    failure_code = None
    try:
        reservations, _snapshot = _quota_admission_state(store)
        for candidate in reservations:
            if candidate["project_id"] == project_id:
                if reservation is not None:
                    _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
                reservation = candidate
        key_digest, key_code = r._reservation_key_digest(reservation_key)
        if key_code is not None:
            failure_code = key_code
        elif reservation is None:
            failure_code = r.RevisionStoreErrorCode.ALREADY_EXISTS
        elif (
            reservation["kind"] != "generation_zero"
            or reservation["expected_head"] is not None
            or reservation["key_sha256"] != key_digest
            or reservation["ceiling_files"] != ceiling_files
            or reservation["state"] not in {"publishing", "published"}
        ):
            failure_code = r.RevisionStoreErrorCode.RECOVERY_REQUIRED
        elif not _entry_missing(Path(store._root) / reservation["project_temp"]):
            failure_code = r.RevisionStoreErrorCode.RECOVERY_REQUIRED
        if failure_code is None:
            context = _project_context(store, project_id)
            head = _load_head_context(context, project_id)
            revision, _capability = _load_revision_context(
                context,
                project_id,
                reservation["revision_id"],
            )
            if (
                head.generation != 0
                or head.revision_id != reservation["revision_id"]
                or revision.id != reservation["revision_id"]
                or revision.base_revision is not None
                or head.manifest_sha256 != revision.manifest_sha256
                or revision.artifacts != ()
            ):
                failure_code = r.RevisionStoreErrorCode.RECOVERY_REQUIRED
            elif expected_sha256 is None and revision.model is not None:
                failure_code = r.RevisionStoreErrorCode.RECOVERY_REQUIRED
            elif expected_sha256 is not None and (
                revision.model is None
                or revision.model.name != "model.FCStd"
                or revision.model.format != "fcstd"
                or revision.model.sha256 != expected_sha256
                or revision.model.size_bytes != expected_size
            ):
                failure_code = r.RevisionStoreErrorCode.RECOVERY_REQUIRED
    except r.RevisionStoreError as error:
        failure_code = r.RevisionStoreErrorCode.RECOVERY_REQUIRED
        if error.code is r.RevisionStoreErrorCode.ALREADY_EXISTS:
            failure_code = error.code
    finally:
        release_code = r._release_quota_lease(acquired)
    if failure_code is None and release_code is not None:
        failure_code = r.RevisionStoreErrorCode.RECOVERY_REQUIRED
    if failure_code is not None:
        return failure_code
    if reservation["state"] == "publishing":
        _phase, phase_code = r._set_reservation_phase(
            store,
            reservation["revision_id"],
            "generation_zero",
            project_id,
            None,
            reservation_key,
            "published",
            None,
        )
        if phase_code is not None:
            return r.RevisionStoreErrorCode.RECOVERY_REQUIRED
    released = r._release_reservation(
        store,
        reservation["revision_id"],
        "generation_zero",
        project_id,
        None,
        reservation_key,
    )
    if released is not None:
        return r.RevisionStoreErrorCode.RECOVERY_REQUIRED
    return r.RevisionStoreErrorCode.ALREADY_EXISTS


def initialize_project(
    store,
    project_id,
    source,
    expected_sha256,
    expected_size,
    lease,
    source_at=None,
):
    r = _r()
    _require_lease(store, project_id, lease)
    if source is not None and source_at is not None:
        _raise(r.RevisionStoreErrorCode.INVALID_INPUT)
    payload = None
    if source is None and source_at is None:
        if expected_sha256 is not None or expected_size is not None:
            _raise(r.RevisionStoreErrorCode.INVALID_INPUT)
    else:
        if (
            type(expected_sha256) is not str
            or re.fullmatch(r._DIGEST_PATTERN, expected_sha256) is None
            or type(expected_size) is not int
            or expected_size <= 0
        ):
            _raise(r.RevisionStoreErrorCode.INVALID_INPUT)
        if expected_size > r._MAX_FILE_BYTES:
            _raise(r.RevisionStoreErrorCode.BUDGET_EXCEEDED)
        if source_at is None:
            payload = _external_payload(source, expected_sha256, expected_size)
        else:
            payload = _external_payload_at(source_at, expected_sha256, expected_size)

    root_path, root_capability = _root(store)
    final_name = r._project_key(project_id)
    final_path = root_path / final_name
    reservation_key = "generation-zero:" + project_id
    ceiling_files = 5 if payload is not None else 4
    if not _entry_missing(final_path):
        try:
            _capture_directory(final_path, root_capability)
        except r.RevisionStoreError:
            raise
        _raise(
            _converge_generation_zero(
                store,
                project_id,
                expected_sha256,
                expected_size,
                reservation_key,
                ceiling_files,
            )
        )

    revision_id = r._new_revision_id()
    code = r._identifier_code(revision_id, r._REVISION_PATTERN)
    if code is not None:
        _raise(code)
    temp_name = ".project." + secrets.token_hex(16) + ".tmp"
    key_digest, code = r._reservation_key_digest(reservation_key)
    if code is not None:
        _raise(code)
    reservation, reused = _reserve_quota(
        store,
        "generation_zero",
        project_id,
        None,
        revision_id,
        key_digest,
        temp_name,
        ceiling_files,
    )
    revision_id = reservation["revision_id"]
    temp_name = reservation["project_temp"]
    if reservation["state"] == "published":
        _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)

    temp_path = root_path / temp_name
    published = False
    try:
        mutation_lease, mutation_code = r._acquire_quota_lease(store)
        if mutation_code is not None:
            _raise(mutation_code)
        try:
            if reused and _cleanup_generation_zero_temp(
                root_path,
                root_capability,
                temp_name,
                revision_id,
            ):
                _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
            temp = _create_directory(temp_path, root_capability, exclusive=True)
            revisions_path = temp_path / "revisions"
            candidates_path = temp_path / "candidates"
            revisions = _create_directory(revisions_path, temp, exclusive=True)
            _create_directory(candidates_path, temp, exclusive=True)
            revision_name = r._revision_key(revision_id)
            revision_path = revisions_path / revision_name
            revision_capability = _create_directory(
                revision_path,
                revisions,
                exclusive=True,
            )

            model = None
            if payload is not None:
                model_id = r._new_artifact_id()
                if r._identifier_code(model_id, r._ARTIFACT_PATTERN) is not None:
                    _raise(r.RevisionStoreErrorCode.INVALID_IDENTIFIER)
                _write_new_file(revision_capability, "model.FCStd", payload)
                model = r.RevisionArtifactRef(
                    id=model_id,
                    name="model.FCStd",
                    format="fcstd",
                    sha256=expected_sha256,
                    size_bytes=expected_size,
                )
            manifest = r._checked_record_bytes(
                r._manifest_body(project_id, revision_id, None, model, ()),
                r._MANIFEST_CHECKSUM_DOMAIN,
            )
            _write_new_file(revision_capability, "manifest.json", manifest)
            head = r.ProjectHead(
                project_id=project_id,
                generation=0,
                revision_id=revision_id,
                manifest_sha256=hashlib.sha256(manifest).hexdigest(),
            )
            head_raw = r._checked_record_bytes(
                r._head_mapping(head),
                r._HEAD_CHECKSUM_DOMAIN,
            )
            _write_new_file(temp, "HEAD.json", head_raw)
        finally:
            mutation_release_code = r._release_quota_lease(mutation_lease)
            if mutation_release_code is not None:
                _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        _phase, phase_code = r._set_reservation_phase(
            store,
            revision_id,
            "generation_zero",
            project_id,
            None,
            reservation_key,
            "publishing",
            None,
        )
        if phase_code is not None:
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        mutation_lease, mutation_code = r._acquire_quota_lease(store)
        if mutation_code is not None:
            _raise(mutation_code)
        try:
            try:
                _rename_directory(root_capability, temp_name, final_name, temp)
                published = True
            except r.RevisionStoreError:
                if not _entry_missing(final_path):
                    published = True
                    _raise(
                        r.RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
                        head_committed=True,
                    )
                raise
        finally:
            mutation_release_code = r._release_quota_lease(mutation_lease)
            if mutation_release_code is not None:
                if published or not _entry_missing(final_path):
                    published = True
                    _raise(
                        r.RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
                        head_committed=True,
                    )
                _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        loaded = load_head(store, project_id)
        if loaded != head:
            _raise(
                r.RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
                head_committed=True,
            )
        _phase, phase_code = r._set_reservation_phase(
            store,
            revision_id,
            "generation_zero",
            project_id,
            None,
            reservation_key,
            "published",
            None,
        )
        if phase_code is not None:
            _raise(
                r.RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
                head_committed=True,
            )
        release_code = r._release_reservation(
            store,
            revision_id,
            "generation_zero",
            project_id,
            None,
            reservation_key,
        )
        if release_code is not None:
            _raise(
                r.RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
                head_committed=True,
            )
        _validate_lease_after(store, lease)
        return head
    except r.RevisionStoreError as error:
        if published:
            raise
        cleanup_lease, cleanup_code = r._acquire_quota_lease(store)
        cleanup_failed = cleanup_code is not None
        if not cleanup_failed:
            try:
                cleanup_failed = _cleanup_generation_zero_temp(
                    root_path,
                    root_capability,
                    temp_name,
                    revision_id,
                )
            finally:
                cleanup_release_code = r._release_quota_lease(cleanup_lease)
                cleanup_failed = cleanup_release_code is not None or cleanup_failed
        release_code = None
        if not cleanup_failed:
            release_code = r._release_reservation(
                store,
                revision_id,
                "generation_zero",
                project_id,
                None,
                reservation_key,
            )
        if cleanup_failed or release_code is not None:
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        raise error


def _terminal_journal_matches_head(head, journal) -> bool:
    r = _r()
    if journal.state is r.CommitJournalState.COMMITTED:
        return (
            head.generation == journal.expected_head.generation + 1
            and head.project_id == journal.project_id
            and head.revision_id == journal.candidate_revision
            and head.manifest_sha256 == journal.manifest_sha256
        )
    if journal.state is r.CommitJournalState.NOT_COMMITTED:
        return head == journal.expected_head
    return False


def _reservation_body(
    project_id,
    expected_head,
    revision_id,
    key_digest,
    ceiling_files,
    state,
    revision_temp=None,
):
    r = _r()
    return r._reservation_body(
        "candidate",
        project_id,
        expected_head,
        revision_id,
        key_digest,
        ceiling_files,
        state,
        None,
        revision_temp,
    )


def _reserve_quota(
    store,
    kind,
    project_id,
    expected_head,
    revision_id,
    key_digest,
    project_temp,
    ceiling_files,
):
    """Atomically admit and persist one capacity reservation."""

    r = _r()
    acquired, code = r._acquire_quota_lease(store)
    if code is not None:
        _raise(code)
    failure = None
    result = None
    created_context = None
    try:
        reservations, snapshot = _quota_admission_state(store)
        existing = None
        for reservation in reservations:
            if reservation["project_id"] != project_id:
                continue
            if (
                reservation["kind"] == kind
                and reservation["key_sha256"] == key_digest
                and reservation["expected_head"] == expected_head
                and reservation["ceiling_files"] == ceiling_files
            ):
                existing = reservation
            else:
                _raise(r.RevisionStoreErrorCode.CONFLICT)
            break
        if existing is not None:
            result = (existing, True)
        else:
            code = r._reservation_admission_code(
                snapshot,
                reservations,
                kind,
                ceiling_files,
            )
            if code is not None:
                _raise(code)
            reserved = r._reservation_body(
                kind,
                project_id,
                expected_head,
                revision_id,
                key_digest,
                ceiling_files,
                "reserved",
                project_temp,
                None,
            )
            created_context = _reservation_context(store, revision_id, create=True)
            if _directory_names(created_context["reservation"]):
                _raise(r.RevisionStoreErrorCode.CONFLICT)
            _write_new_file(
                created_context["reservation"],
                r._RESERVATION_RECORD,
                r._checked_record_bytes(reserved, r._RESERVATION_CHECKSUM_DOMAIN),
            )
            validate_windows_path(created_context["reservation"], directory=True)
            validate_windows_path(created_context["reservations"], directory=True)
            result = (reserved, False)
    except r.RevisionStoreError as error:
        failure = error
        if created_context is not None:
            try:
                if not _directory_names(created_context["reservation"]):
                    delete_windows_directory(
                        created_context["reservation_path"],
                        parent=created_context["reservations"],
                        expected=created_context["reservation"],
                    )
            except (OSError, TypeError, ValueError, r.RevisionStoreError):
                failure = r.RevisionStoreError(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
    finally:
        release_code = r._release_quota_lease(acquired)
    if failure is not None:
        raise failure
    if release_code is not None or result is None:
        _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
    return result


def _reserve_candidate_quota(
    store,
    project_id,
    expected_head,
    revision_id,
    key_digest,
    ceiling_files,
):
    return _reserve_quota(
        store,
        "candidate",
        project_id,
        expected_head,
        revision_id,
        key_digest,
        None,
        ceiling_files,
    )


def set_reservation_phase(
    store,
    revision_id,
    kind,
    project_id,
    expected_head,
    reservation_key,
    state,
    revision_temp,
):
    """Change reservation phase under the global quota lease without re-admission."""

    r = _r()
    acquired, code = r._acquire_quota_lease(store)
    if code is not None:
        return None, code
    failure_code = None
    updated = None
    try:
        current, _context = _load_reservation(store, revision_id)
        failure_code = r._reservation_binding_code(
            current,
            kind,
            project_id,
            expected_head,
            reservation_key,
        )
        if failure_code is None:
            updated = r._reservation_body(
                current["kind"],
                current["project_id"],
                current["expected_head"],
                current["revision_id"],
                current["key_sha256"],
                current["ceiling_files"],
                state,
                current["project_temp"],
                revision_temp,
            )
            _replace_reservation(store, updated)
    except r.RevisionStoreError as error:
        failure_code = error.code
    finally:
        release_code = r._release_quota_lease(acquired)
    if failure_code is None and release_code is not None:
        failure_code = r.RevisionStoreErrorCode.RECOVERY_REQUIRED
    if failure_code is not None:
        return None, failure_code
    return updated, None


def release_reservation(
    store,
    revision_id,
    kind,
    project_id,
    expected_head,
    reservation_key,
):
    """Validate physical ownership and remove one reservation under quota lock."""

    r = _r()
    acquired, code = r._acquire_quota_lease(store)
    if code is not None:
        return code
    failure_code = None
    deleted = False
    try:
        reservations, snapshot = _quota_admission_state(store)
        reservation = None
        for value in reservations:
            if value["revision_id"] == revision_id:
                reservation = value
                break
        if reservation is None:
            failure_code = r.RevisionStoreErrorCode.NOT_FOUND
        else:
            failure_code = r._reservation_binding_code(
                reservation,
                kind,
                project_id,
                expected_head,
                reservation_key,
            )
        if failure_code is None:
            failure_code = r._reservation_release_code(
                snapshot,
                reservations,
                reservation,
            )
        if failure_code is None:
            context = _reservation_context(store, revision_id, create=False)
            _delete_file(
                context["reservation"],
                r._RESERVATION_RECORD,
                missing_ok=False,
            )
            if _directory_names(context["reservation"]):
                _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
            delete_windows_directory(
                context["reservation_path"],
                parent=context["reservations"],
                expected=context["reservation"],
            )
            validate_windows_path(context["reservations"], directory=True)
            deleted = True
    except r.RevisionStoreError as error:
        failure_code = error.code
    except (OSError, TypeError, ValueError):
        failure_code = r.RevisionStoreErrorCode.RECOVERY_REQUIRED
    finally:
        release_code = r._release_quota_lease(acquired)
    if release_code is not None and (failure_code is None or deleted):
        return r.RevisionStoreErrorCode.RECOVERY_REQUIRED
    return failure_code


def _copy_base_model(context, candidate, base_revision) -> None:
    r = _r()
    if base_revision.model is None:
        return
    _validated, revision_capability = _load_revision_context(
        context,
        base_revision.project_id,
        base_revision.id,
    )
    payload, _source = _read_file(
        revision_capability,
        base_revision.model.name,
        r._MAX_FILE_BYTES,
        missing_code=r.RevisionStoreErrorCode.CORRUPT_CONTENT,
    )
    if (
        len(payload) != base_revision.model.size_bytes
        or hashlib.sha256(payload).hexdigest() != base_revision.model.sha256
    ):
        _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
    _replace_record(candidate, "model.FCStd", payload)


def _seed_integrity_code(code):
    r = _r()
    if code in {
        r.RevisionStoreErrorCode.NOT_FOUND,
        r.RevisionStoreErrorCode.CORRUPT_RECORD,
        r.RevisionStoreErrorCode.CORRUPT_CONTENT,
        r.RevisionStoreErrorCode.UNSAFE_STORE,
    }:
        return r.RevisionStoreErrorCode.RECOVERY_REQUIRED
    return code


def _strict_seed_source(
    context,
    expected_head,
    expected_source,
    *,
    bound: bool,
):
    r = _r()
    try:
        head_revision, _capability = _load_revision_context(
            context,
            expected_head.project_id,
            expected_head.revision_id,
        )
        if head_revision.manifest_sha256 != expected_head.manifest_sha256:
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        current = head_revision.base_revision
        traversed = 0
        while current is not None:
            traversed += 1
            if traversed > _MAX_DIRECTORY_ENTRIES:
                _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
            loaded, capability = _load_revision_context(
                context,
                expected_head.project_id,
                current,
            )
            if loaded.id == expected_source.id:
                if loaded != expected_source:
                    _raise(
                        r.RevisionStoreErrorCode.RECOVERY_REQUIRED
                        if bound
                        else r.RevisionStoreErrorCode.CONFLICT
                    )
                return loaded, capability
            current = loaded.base_revision
    except r.RevisionStoreError as error:
        _raise(_seed_integrity_code(error.code))
    _raise(
        r.RevisionStoreErrorCode.RECOVERY_REQUIRED if bound else r.RevisionStoreErrorCode.CONFLICT
    )


def _read_seed_control(candidate, name, domain, *, bound: bool):
    r = _r()
    try:
        raw, _capability = _read_file(
            candidate,
            name,
            r._MAX_JOURNAL_BYTES,
            missing_code=r.RevisionStoreErrorCode.NOT_FOUND,
        )
    except r.RevisionStoreError as error:
        if error.code is r.RevisionStoreErrorCode.NOT_FOUND:
            return None, error.code
        return None, r.RevisionStoreErrorCode.RECOVERY_REQUIRED
    try:
        body = _parse_record(raw, domain, r._MAX_JOURNAL_BYTES)
        parsed, code = r._seed_binding_from_body(body) if bound else r._seed_intent_from_body(body)
    except r.RevisionStoreError:
        return None, r.RevisionStoreErrorCode.RECOVERY_REQUIRED
    if code is not None:
        return None, r.RevisionStoreErrorCode.RECOVERY_REQUIRED
    return parsed, None


def _bind_seed_source(
    store,
    candidate,
    project_id,
    revision_id,
    expected_head,
    expected_source,
    key_digest,
):
    r = _r()
    acquired, code = r._acquire_quota_lease(store)
    if code is not None:
        _raise(code)
    failure_code = None
    try:
        binding, binding_code = _read_seed_control(
            candidate,
            r._SEED_BINDING_RECORD,
            r._SEED_BINDING_CHECKSUM_DOMAIN,
            bound=True,
        )
        intent, intent_code = _read_seed_control(
            candidate,
            r._SEED_INTENT_RECORD,
            r._SEED_INTENT_CHECKSUM_DOMAIN,
            bound=False,
        )
        if binding_code not in {None, r.RevisionStoreErrorCode.NOT_FOUND}:
            failure_code = binding_code
        elif intent_code not in {None, r.RevisionStoreErrorCode.NOT_FOUND}:
            failure_code = intent_code
        elif binding is not None:
            failure_code = r._seed_control_binding_code(
                binding,
                project_id,
                revision_id,
                expected_head,
                key_digest,
            )
            if failure_code is None and binding["source_revision"] != expected_source:
                failure_code = r.RevisionStoreErrorCode.CONFLICT
            if failure_code is None and intent is not None:
                intent_binding_code = r._seed_control_binding_code(
                    intent,
                    project_id,
                    revision_id,
                    expected_head,
                    key_digest,
                )
                if intent_binding_code is not None:
                    failure_code = intent_binding_code
                else:
                    _delete_file(candidate, r._SEED_INTENT_RECORD, missing_ok=False)
        elif intent is None:
            failure_code = r.RevisionStoreErrorCode.RECOVERY_REQUIRED
        else:
            failure_code = r._seed_control_binding_code(
                intent,
                project_id,
                revision_id,
                expected_head,
                key_digest,
            )
            if failure_code is None:
                raw = r._checked_record_bytes(
                    r._seed_binding_body(
                        project_id,
                        revision_id,
                        expected_head,
                        expected_source,
                        key_digest,
                    ),
                    r._SEED_BINDING_CHECKSUM_DOMAIN,
                )
                _write_new_file(candidate, r._SEED_BINDING_RECORD, raw)
                rebound, rebound_code = _read_seed_control(
                    candidate,
                    r._SEED_BINDING_RECORD,
                    r._SEED_BINDING_CHECKSUM_DOMAIN,
                    bound=True,
                )
                if rebound_code is not None or rebound["source_revision"] != expected_source:
                    failure_code = r.RevisionStoreErrorCode.RECOVERY_REQUIRED
                else:
                    _delete_file(candidate, r._SEED_INTENT_RECORD, missing_ok=False)
        validate_windows_path(candidate, directory=True)
    except r.RevisionStoreError as error:
        failure_code = error.code
    finally:
        release_code = r._release_quota_lease(acquired)
    if failure_code is None and release_code is not None:
        failure_code = r.RevisionStoreErrorCode.RECOVERY_REQUIRED
    if failure_code is not None:
        _raise(failure_code)


def _validate_seed_binding(
    candidate,
    project_id,
    revision_id,
    expected_head,
    expected_source,
    key_digest,
):
    r = _r()
    binding, binding_code = _read_seed_control(
        candidate,
        r._SEED_BINDING_RECORD,
        r._SEED_BINDING_CHECKSUM_DOMAIN,
        bound=True,
    )
    intent, intent_code = _read_seed_control(
        candidate,
        r._SEED_INTENT_RECORD,
        r._SEED_INTENT_CHECKSUM_DOMAIN,
        bound=False,
    )
    if binding_code is not None or intent_code is not r.RevisionStoreErrorCode.NOT_FOUND:
        _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
    code = r._seed_control_binding_code(
        binding,
        project_id,
        revision_id,
        expected_head,
        key_digest,
    )
    if code is None and binding["source_revision"] != expected_source:
        code = r.RevisionStoreErrorCode.CONFLICT
    if code is not None:
        _raise(code)


def _validate_seed_payload(candidate, expected_source) -> tuple[bytes, bytes]:
    r = _r()
    model_raw, _model = _read_file(
        candidate,
        "model.FCStd",
        r._MAX_FILE_BYTES,
        missing_code=r.RevisionStoreErrorCode.CORRUPT_CONTENT,
        allow_empty=True,
    )
    step_raw, _step = _read_file(
        candidate,
        "model.step",
        r._MAX_FILE_BYTES,
        missing_code=r.RevisionStoreErrorCode.CORRUPT_CONTENT,
        allow_empty=True,
    )
    if (
        len(model_raw) != expected_source.model.size_bytes
        or hashlib.sha256(model_raw).hexdigest() != expected_source.model.sha256
        or len(step_raw) != expected_source.artifacts[0].size_bytes
        or hashlib.sha256(step_raw).hexdigest() != expected_source.artifacts[0].sha256
    ):
        _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
    return model_raw, step_raw


def reserve_candidate(store, project_id, expected_head, reservation_key, lease):
    r = _r()
    _require_lease(store, project_id, lease)
    if type(expected_head) is not r.ProjectHead or expected_head.project_id != project_id:
        _raise(r.RevisionStoreErrorCode.INVALID_INPUT)
    key_digest, code = r._reservation_key_digest(reservation_key)
    if code is not None:
        _raise(code)
    context = _project_context(store, project_id)
    head = _load_head_context(context, project_id)
    if head != expected_head:
        _raise(r.RevisionStoreErrorCode.CONFLICT)
    journal = _load_journal(context)
    if journal is not None and journal.state is r.CommitJournalState.STAGING:
        try:
            reservation, _reservation_context_value = _load_reservation(
                store,
                journal.candidate_revision,
            )
        except r.RevisionStoreError as error:
            if error.code is r.RevisionStoreErrorCode.NOT_FOUND:
                _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
            raise
        if not _reservation_matches(
            reservation,
            project_id=project_id,
            expected_head=expected_head,
            revision_id=journal.candidate_revision,
            key_digest=key_digest,
            states={"reserved", "staged"},
        ):
            _raise(r.RevisionStoreErrorCode.CONFLICT)
        _name, _path, capability = _candidate_context(
            context,
            journal.candidate_revision,
        )
        _validate_candidate_entries(capability)
        if reservation["state"] == "reserved":
            reservation, phase_code = r._set_reservation_phase(
                store,
                journal.candidate_revision,
                "candidate",
                project_id,
                expected_head,
                reservation_key,
                "staged",
                None,
            )
            if phase_code is not None or reservation is None:
                _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        _validate_lease_after(store, lease)
        return journal.candidate_revision
    if journal is not None:
        if not _terminal_journal_matches_head(head, journal):
            _raise(r.RevisionStoreErrorCode.CONFLICT)
        with _quota_mutation(store):
            _delete_file(context["project"], "journal.json", missing_ok=False)

    revision_id = r._new_revision_id()
    transaction_id = r._new_transaction_id()
    if r._identifier_code(revision_id, r._REVISION_PATTERN) is not None:
        _raise(r.RevisionStoreErrorCode.INVALID_IDENTIFIER)
    if r._identifier_code(transaction_id, r._TRANSACTION_PATTERN) is not None:
        _raise(r.RevisionStoreErrorCode.INVALID_IDENTIFIER)
    seeded = bool(
        type(reservation_key) is str
        and re.fullmatch(r"revert:[0-9a-f]{64}", reservation_key) is not None
    )
    ceiling_files = 9 if seeded else 8
    reserved, reused = _reserve_candidate_quota(
        store,
        project_id,
        expected_head,
        revision_id,
        key_digest,
        ceiling_files,
    )
    revision_id = reserved["revision_id"]

    candidate_name = r._candidate_key(revision_id)
    candidate_path_value = context["candidates_path"] / candidate_name
    with _quota_mutation(store):
        if (
            reused
            and not _entry_missing(candidate_path_value)
            and _remove_candidate(
                context,
                revision_id,
            )
        ):
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        candidate = _create_directory(candidate_path_value, context["candidates"])
        if _directory_names(candidate):
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        _write_new_file(candidate, "model.FCStd", b"")
        _write_new_file(candidate, "model.step", b"")
        base_revision, _base_capability = _load_revision_context(
            context,
            project_id,
            head.revision_id,
        )
        _copy_base_model(context, candidate, base_revision)
        if seeded:
            intent = r._checked_record_bytes(
                r._seed_intent_body(project_id, revision_id, expected_head, key_digest),
                r._SEED_INTENT_CHECKSUM_DOMAIN,
            )
            _write_new_file(candidate, r._SEED_INTENT_RECORD, intent)
        staging = r.CommitJournal(
            id=transaction_id,
            project_id=project_id,
            expected_head=head,
            candidate_revision=revision_id,
            manifest_sha256=None,
            state=r.CommitJournalState.STAGING,
        )
        _write_journal(context, staging)
    staged, phase_code = r._set_reservation_phase(
        store,
        revision_id,
        "candidate",
        project_id,
        expected_head,
        reservation_key,
        "staged",
        None,
    )
    if phase_code is not None or staged is None:
        _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
    _validate_candidate_entries(candidate)
    _validate_lease_after(store, lease)
    return revision_id


def validate_candidate_reservation(
    store,
    project_id,
    expected_head,
    revision_id,
    reservation_key,
    lease,
):
    r = _r()
    try:
        key_digest, code = r._reservation_key_digest(reservation_key)
        if code is not None:
            return code
        context, head, journal, _name, _path, capability = _authority(
            store,
            project_id,
            revision_id,
            lease,
            expected_head=expected_head,
        )
        del context
        if head != expected_head or journal.expected_head != expected_head:
            return r.RevisionStoreErrorCode.CONFLICT
        reservation, _reservation_context_value = _load_reservation(store, revision_id)
        if not _reservation_matches(
            reservation,
            project_id=project_id,
            expected_head=expected_head,
            revision_id=revision_id,
            key_digest=key_digest,
            states={"staged"},
        ):
            return r.RevisionStoreErrorCode.CONFLICT
        _validate_candidate_entries(capability)
        _validate_lease_after(store, lease)
        return None
    except r.RevisionStoreError as error:
        return error.code


def seed_candidate_from_revision(
    store,
    project_id,
    expected_head,
    revision_id,
    expected_source,
    reservation_key,
    lease,
):
    r = _r()
    project_code = r._identifier_code(project_id, r._PROJECT_PATTERN)
    revision_code = r._identifier_code(revision_id, r._REVISION_PATTERN)
    source_code = r._seed_source_code(project_id, expected_source)
    key_digest, key_code = r._seed_reservation_key_digest(reservation_key)
    if project_code is not None:
        _raise(project_code)
    if revision_code is not None:
        _raise(revision_code)
    if type(expected_head) is not r.ProjectHead or expected_head.project_id != project_id:
        _raise(r.RevisionStoreErrorCode.INVALID_INPUT)
    if source_code is not None:
        _raise(source_code)
    if key_code is not None:
        _raise(key_code)
    reservation_code = validate_candidate_reservation(
        store,
        project_id,
        expected_head,
        revision_id,
        reservation_key,
        lease,
    )
    if reservation_code is not None:
        _raise(reservation_code)
    context, _head, _journal, _name, _path, candidate = _authority(
        store,
        project_id,
        revision_id,
        lease,
        expected_head=expected_head,
    )
    _loaded, source_capability = _strict_seed_source(
        context,
        expected_head,
        expected_source,
        bound=False,
    )
    _bind_seed_source(
        store,
        candidate,
        project_id,
        revision_id,
        expected_head,
        expected_source,
        key_digest,
    )
    model_raw, _model = _read_file(
        source_capability,
        "model.FCStd",
        r._MAX_FILE_BYTES,
        missing_code=r.RevisionStoreErrorCode.CORRUPT_CONTENT,
    )
    step_raw, _step = _read_file(
        source_capability,
        "model.step",
        r._MAX_FILE_BYTES,
        missing_code=r.RevisionStoreErrorCode.CORRUPT_CONTENT,
    )
    if (
        len(model_raw) != expected_source.model.size_bytes
        or hashlib.sha256(model_raw).hexdigest() != expected_source.model.sha256
        or len(step_raw) != expected_source.artifacts[0].size_bytes
        or hashlib.sha256(step_raw).hexdigest() != expected_source.artifacts[0].sha256
    ):
        _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
    with _quota_mutation(store):
        _replace_record(candidate, "model.FCStd", model_raw)
        _replace_record(candidate, "model.step", step_raw)
        _validate_seed_payload(candidate, expected_source)
    loaded_after, _source_after = _load_revision_context(
        context,
        project_id,
        expected_source.id,
    )
    if loaded_after != expected_source:
        _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
    _validate_candidate_entries(candidate)
    _validate_lease_after(store, lease)


def validate_candidate_payload(
    store,
    project_id,
    revision_id,
    expected_source,
    lease,
):
    r = _r()
    project_code = r._identifier_code(project_id, r._PROJECT_PATTERN)
    revision_code = r._identifier_code(revision_id, r._REVISION_PATTERN)
    source_code = r._seed_source_code(project_id, expected_source)
    if project_code is not None:
        _raise(project_code)
    if revision_code is not None:
        _raise(revision_code)
    if source_code is not None:
        _raise(source_code)
    context, _head, journal, _name, _path, candidate = _authority(
        store,
        project_id,
        revision_id,
        lease,
    )
    reservation, _reservation_context_value = _load_reservation(store, revision_id)
    if (
        reservation["kind"] != "candidate"
        or reservation["project_id"] != project_id
        or reservation["expected_head"] != journal.expected_head
        or reservation["revision_id"] != revision_id
        or reservation["state"] != "staged"
        or reservation["ceiling_files"] != 9
    ):
        _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
    _validate_seed_binding(
        candidate,
        project_id,
        revision_id,
        journal.expected_head,
        expected_source,
        reservation["key_sha256"],
    )
    _loaded, _capability = _strict_seed_source(
        context,
        journal.expected_head,
        expected_source,
        bound=True,
    )
    _validate_seed_payload(candidate, expected_source)
    _validate_candidate_entries(candidate)
    _validate_lease_after(store, lease)


def _windows_source_binding(value):
    r = _r()
    birthtime = int(getattr(value, "st_birthtime_ns", value.st_ctime_ns))
    try:
        return r.RevisionSourceBinding(
            dev=int(value.st_dev),
            ino=int(value.st_ino),
            mode=int(value.st_mode),
            uid=int(value.st_uid),
            nlink=int(value.st_nlink),
            size=int(value.st_size),
            mtime_ns=int(value.st_mtime_ns),
            ctime_ns=birthtime,
        )
    except (r.RevisionStoreError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)


def _hash_open_windows_source(fd: int, expected_size: int, chunk_bytes: int) -> str:
    r = _r()
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        remaining = expected_size
        digest = hashlib.sha256()
        while remaining:
            chunk = os.read(fd, min(chunk_bytes, remaining))
            if not chunk or len(chunk) > remaining:
                raise OSError
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
        return digest.hexdigest()
    except r.RevisionStoreError:
        raise
    except OSError:
        _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)


def replace_candidate_model_at(
    store,
    project_id,
    expected_head,
    revision_id,
    reservation_key,
    source_parent_fd,
    source_name,
    expected_binding,
    expected_sha256,
    expected_size,
    lease,
):
    """Copy a checkout file under exact Windows handle/path capability."""

    r = _r()
    reservation_code = validate_candidate_reservation(
        store,
        project_id,
        expected_head,
        revision_id,
        reservation_key,
        lease,
    )
    if reservation_code is not None:
        _raise(reservation_code)
    if (
        type(source_parent_fd) is not int
        or source_parent_fd < 0
        or type(source_name) is not str
        or re.fullmatch(r._SOURCE_NAME_PATTERN, source_name) is None
        or type(expected_binding) is not r.RevisionSourceBinding
        or type(expected_sha256) is not str
        or re.fullmatch(r._DIGEST_PATTERN, expected_sha256) is None
        or type(expected_size) is not int
        or expected_size <= 0
    ):
        _raise(r.RevisionStoreErrorCode.INVALID_INPUT)
    if expected_size > r._MAX_FILE_BYTES:
        _raise(r.RevisionStoreErrorCode.BUDGET_EXCEEDED)
    source_fd = None
    source_capability = None
    try:
        try:
            if os.get_inheritable(source_parent_fd):
                raise OSError
            source_parent = capture_windows_fd(source_parent_fd, directory=True)
            validate_windows_path(source_parent, directory=True)
            source_path = _child_path(source_parent, source_name)
            source_fd, source_capability = open_private_file(
                source_path,
                create=False,
                read_write=False,
                expected_parent=source_parent,
            )
        except (OSError, TypeError, ValueError):
            _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
        before = os.fstat(source_fd)
        if (
            _windows_source_binding(before) != expected_binding
            or before.st_size != expected_size
            or source_capability.volume != source_parent.volume
        ):
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
        digest = _hash_open_windows_source(
            source_fd,
            expected_size,
            r._COPY_CHUNK_BYTES,
        )
        after_read = os.fstat(source_fd)
        pinned = capture_windows_fd(
            source_fd,
            directory=False,
            generation_token=source_capability.generation_token,
        )
        if (
            digest != expected_sha256
            or _windows_source_binding(after_read) != expected_binding
            or pinned != source_capability
        ):
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
        os.lseek(source_fd, 0, os.SEEK_SET)
        payload = bytearray()
        remaining = expected_size
        while remaining:
            chunk = os.read(source_fd, min(r._COPY_CHUNK_BYTES, remaining))
            if not chunk or len(chunk) > remaining:
                _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
            payload.extend(chunk)
            remaining -= len(chunk)
        context, _head, _journal, _name, _path, candidate = _authority(
            store,
            project_id,
            revision_id,
            lease,
            expected_head=expected_head,
        )
        with _quota_mutation(store):
            _replace_record(candidate, "model.FCStd", bytes(payload))
            copied, _copied_capability = _read_file(
                candidate,
                "model.FCStd",
                r._MAX_FILE_BYTES,
                missing_code=r.RevisionStoreErrorCode.CORRUPT_CONTENT,
            )
            if (
                len(copied) != expected_size
                or hashlib.sha256(copied).hexdigest() != expected_sha256
            ):
                _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
        final_digest = _hash_open_windows_source(
            source_fd,
            expected_size,
            r._COPY_CHUNK_BYTES,
        )
        final_stat = os.fstat(source_fd)
        final_pinned = capture_windows_fd(
            source_fd,
            directory=False,
            generation_token=source_capability.generation_token,
        )
        if (
            final_digest != expected_sha256
            or _windows_source_binding(final_stat) != expected_binding
            or final_pinned != source_capability
        ):
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
        validate_windows_path(source_capability, directory=False)
        validate_windows_path(source_parent, directory=True)
        _validate_candidate_entries(candidate)
        validate_windows_path(context["root"], directory=True)
        _validate_lease_after(store, lease)
    except r.RevisionStoreError:
        raise
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.IO_ERROR)
    finally:
        if source_fd is not None:
            try:
                os.close(source_fd)
            except OSError:
                pass


def _copy_destination_file(
    directory: WindowsPathCapability,
    reference,
    payload: bytes,
    cursor,
    chunk_bytes: int,
) -> None:
    r = _r()
    path = _child_path(directory, reference.name)
    fd = None
    capability = None
    try:
        if cursor is None:
            fd, capability = open_private_file(
                path,
                create=True,
                read_write=True,
                exclusive=True,
                expected_parent=directory,
            )
            offset = 0
        else:
            fd, capability = open_private_file(
                path,
                create=False,
                read_write=True,
                expected_parent=directory,
            )
            opened = os.fstat(fd)
            if opened.st_size != cursor.size_bytes:
                _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
            os.lseek(fd, 0, os.SEEK_SET)
            remaining = cursor.size_bytes
            digest = hashlib.sha256()
            while remaining:
                chunk = os.read(fd, min(chunk_bytes, remaining))
                if not chunk or len(chunk) > remaining:
                    _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
                digest.update(chunk)
                remaining -= len(chunk)
            if digest.hexdigest() != cursor.sha256:
                _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
            offset = cursor.size_bytes
        os.lseek(fd, offset, os.SEEK_SET)
        source = memoryview(payload)[offset:]
        while source:
            piece = source[:chunk_bytes]
            written = os.write(fd, piece)
            if written <= 0:
                raise OSError
            source = source[written:]
        os.fsync(fd)
        before_hash = os.fstat(fd)
        if before_hash.st_size != reference.size_bytes:
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
        os.lseek(fd, 0, os.SEEK_SET)
        remaining = reference.size_bytes
        digest = hashlib.sha256()
        while remaining:
            chunk = os.read(fd, min(chunk_bytes, remaining))
            if not chunk or len(chunk) > remaining:
                _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
            digest.update(chunk)
            remaining -= len(chunk)
        after_hash = os.fstat(fd)
        pinned = capture_windows_fd(
            fd,
            directory=False,
            generation_token=capability.generation_token,
        )
        if (
            digest.hexdigest() != reference.sha256
            or before_hash.st_dev != after_hash.st_dev
            or before_hash.st_ino != after_hash.st_ino
            or before_hash.st_mode != after_hash.st_mode
            or before_hash.st_nlink != after_hash.st_nlink
            or before_hash.st_size != after_hash.st_size
            or before_hash.st_mtime_ns != after_hash.st_mtime_ns
            or before_hash.st_ctime_ns != after_hash.st_ctime_ns
            or pinned != capability
        ):
            _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
        os.close(fd)
        fd = None
        validate_windows_path(capability, directory=False)
        validate_windows_path(directory, directory=True)
    except FileNotFoundError:
        _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
    except r.RevisionStoreError:
        raise
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def copy_revision_artifacts_at(
    store,
    expected_revision,
    destination_directory_fd,
    cursors,
    chunk_bytes,
):
    """Copy a sealed FCStd/STEP pair into a protected Windows directory."""

    r = _r()
    request = r._copy_request_parts(
        expected_revision,
        destination_directory_fd,
        cursors,
        chunk_bytes,
    )
    if request[4] is not None:
        _raise(request[4])
    model, step, model_cursor, step_cursor = request[:4]
    try:
        if os.get_inheritable(destination_directory_fd):
            raise OSError
        destination = capture_windows_fd(destination_directory_fd, directory=True)
        validate_windows_path(destination, directory=True)
    except (OSError, TypeError, ValueError):
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    names = _directory_names(destination)
    expected_names = ()
    if model_cursor is not None:
        expected_names = (model.name,)
    if step_cursor is not None:
        expected_names = tuple(sorted((model.name, step.name)))
    if names != expected_names:
        _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
    context = _project_context(store, expected_revision.project_id)
    revision, revision_capability = _load_revision_context(
        context,
        expected_revision.project_id,
        expected_revision.id,
    )
    if revision != expected_revision:
        _raise(r.RevisionStoreErrorCode.CONFLICT)
    model_payload, _model_capability = _read_file(
        revision_capability,
        model.name,
        r._MAX_FILE_BYTES,
        missing_code=r.RevisionStoreErrorCode.CORRUPT_CONTENT,
    )
    step_payload, _step_capability = _read_file(
        revision_capability,
        step.name,
        r._MAX_FILE_BYTES,
        missing_code=r.RevisionStoreErrorCode.CORRUPT_CONTENT,
    )
    if (
        len(model_payload) != model.size_bytes
        or hashlib.sha256(model_payload).hexdigest() != model.sha256
        or len(step_payload) != step.size_bytes
        or hashlib.sha256(step_payload).hexdigest() != step.sha256
    ):
        _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
    _copy_destination_file(
        destination,
        model,
        model_payload,
        model_cursor,
        chunk_bytes,
    )
    _copy_destination_file(
        destination,
        step,
        step_payload,
        step_cursor,
        chunk_bytes,
    )
    if _directory_names(destination) != tuple(sorted((model.name, step.name))):
        _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
    after, _capability = _load_revision_context(
        context,
        expected_revision.project_id,
        expected_revision.id,
    )
    if after != expected_revision:
        _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
    final_destination = capture_windows_fd(
        destination_directory_fd,
        directory=True,
        generation_token=destination.generation_token,
    )
    if final_destination != destination:
        _raise(r.RevisionStoreErrorCode.UNSAFE_STORE)
    validate_windows_path(destination, directory=True)
    validate_windows_path(context["root"], directory=True)
    return None


def open_worker_candidate(store, *, expected_head, revision_id, lease):
    r = _r()
    if (
        type(expected_head) is not r.ProjectHead
        or type(store) is not r.LocalRevisionStore
        or r._identifier_code(revision_id, r._REVISION_PATTERN) is not None
    ):
        _raise(r.RevisionStoreErrorCode.INVALID_INPUT)
    context, head, journal, name, _path, candidate = _authority(
        store,
        expected_head.project_id,
        revision_id,
        lease,
        expected_head=expected_head,
    )
    if head != expected_head or journal.expected_head != expected_head:
        _raise(r.RevisionStoreErrorCode.CONFLICT)
    reservation, _reservation_context_value = _load_reservation(store, revision_id)
    if (
        reservation["state"] != "staged"
        or reservation["kind"] != "candidate"
        or reservation["project_id"] != expected_head.project_id
        or reservation["expected_head"] != expected_head
    ):
        _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
    _validate_candidate_entries(candidate)
    validate_windows_path(context["candidates"], directory=True)
    validate_windows_path(candidate, directory=True)
    _validate_lease_after(store, lease)
    return (
        context["candidates"],
        candidate,
        name,
        context["root"].volume,
    )


def open_worker_revision(store, *, expected_revision):
    r = _r()
    if type(store) is not r.LocalRevisionStore or type(expected_revision) is not r.RevisionRef:
        _raise(r.RevisionStoreErrorCode.INVALID_INPUT)
    context = _project_context(store, expected_revision.project_id)
    revision, capability = _load_revision_context(
        context,
        expected_revision.project_id,
        expected_revision.id,
    )
    if revision != expected_revision:
        _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)
    name = r._revision_key(expected_revision.id)
    validate_windows_path(context["revisions"], directory=True)
    validate_windows_path(capability, directory=True)
    return (
        context["revisions"],
        capability,
        name,
        context["root"].volume,
    )


def _remove_candidate(context, revision_id) -> bool:
    r = _r()
    try:
        _name, path, capability = _candidate_context(context, revision_id)
        names = _validate_candidate_entries(capability)
        for name in names:
            _delete_file(capability, name, missing_ok=False)
        if _directory_names(capability):
            return True
        validate_windows_path(capability, directory=True)
        validate_windows_path(context["candidates"], directory=True)
        os.rmdir(windows_extended_path(path))
        validate_windows_path(context["candidates"], directory=True)
        return False
    except r.RevisionStoreError:
        return True
    except (OSError, TypeError, ValueError):
        return True


def _cleanup_revision_temp(context, temp_name: str) -> bool:
    """Remove only the exact reservation-bound unpublished revision tree."""

    r = _r()
    try:
        temp_path = _child_path(context["revisions"], temp_name)
    except ValueError:
        return True
    if _entry_missing(temp_path):
        return False
    try:
        temp = _capture_directory(
            temp_path,
            context["revisions"],
            missing_code=r.RevisionStoreErrorCode.RECOVERY_REQUIRED,
        )
        names = _directory_names(temp)
        if not set(names).issubset({"model.FCStd", "model.step", "manifest.json"}):
            return True
        for name in names:
            _delete_file(temp, name, missing_ok=False)
        if _directory_names(temp):
            return True
        delete_windows_directory(
            temp_path,
            parent=context["revisions"],
            expected=temp,
        )
        validate_windows_path(context["revisions"], directory=True)
        return False
    except (OSError, TypeError, ValueError, r.RevisionStoreError):
        return True


def seal_revision(store, project_id, revision_id, lease):
    r = _r()
    context, _head, journal, _candidate_name, _candidate_path, candidate = _authority(
        store,
        project_id,
        revision_id,
        lease,
    )
    reservation, _reservation_context_value = _load_reservation(store, revision_id)
    if (
        reservation["kind"] != "candidate"
        or reservation["project_id"] != project_id
        or reservation["expected_head"] != journal.expected_head
        or reservation["revision_id"] != revision_id
    ):
        _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
    if reservation["state"] == "publishing":
        bound_temp = reservation["revision_temp"]
        final_path = context["revisions_path"] / r._revision_key(revision_id)
        if bound_temp is None or not _entry_missing(final_path):
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        with _quota_mutation(store):
            if _cleanup_revision_temp(context, bound_temp):
                _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
            reservation = _reservation_body(
                project_id,
                journal.expected_head,
                revision_id,
                reservation["key_sha256"],
                reservation["ceiling_files"],
                "staged",
            )
            _replace_reservation(store, reservation)
            restored, _restored_context = _load_reservation(store, revision_id)
            if not _reservation_matches(
                restored,
                project_id=project_id,
                expected_head=journal.expected_head,
                revision_id=revision_id,
                key_digest=reservation["key_sha256"],
                states={"staged"},
            ) or restored["revision_temp"] is not None:
                _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
            reservation = restored
    elif reservation["state"] != "staged" or reservation["revision_temp"] is not None:
        _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
    names_before = _validate_candidate_entries(candidate)
    expected_source = None
    if reservation["ceiling_files"] == 9:
        binding, binding_code = _read_seed_control(
            candidate,
            r._SEED_BINDING_RECORD,
            r._SEED_BINDING_CHECKSUM_DOMAIN,
            bound=True,
        )
        _intent, intent_code = _read_seed_control(
            candidate,
            r._SEED_INTENT_RECORD,
            r._SEED_INTENT_CHECKSUM_DOMAIN,
            bound=False,
        )
        if (
            binding_code is not None
            or intent_code is not r.RevisionStoreErrorCode.NOT_FOUND
            or set(names_before) != {"model.FCStd", "model.step", r._SEED_BINDING_RECORD}
        ):
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        code = r._seed_control_binding_code(
            binding,
            project_id,
            revision_id,
            journal.expected_head,
            reservation["key_sha256"],
        )
        if code is not None:
            _raise(code)
        expected_source = binding["source_revision"]
        _loaded, _source_capability = _strict_seed_source(
            context,
            journal.expected_head,
            expected_source,
            bound=True,
        )
        model_raw, step_raw = _validate_seed_payload(candidate, expected_source)
    else:
        if set(names_before) != {"model.FCStd", "model.step"}:
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        model_raw, _model_capability = _read_file(
            candidate,
            "model.FCStd",
            r._MAX_FILE_BYTES,
            missing_code=r.RevisionStoreErrorCode.CORRUPT_CONTENT,
        )
        step_raw, _step_capability = _read_file(
            candidate,
            "model.step",
            r._MAX_FILE_BYTES,
            missing_code=r.RevisionStoreErrorCode.CORRUPT_CONTENT,
        )
    if len(model_raw) + len(step_raw) > r._MAX_REVISION_BYTES:
        _raise(r.RevisionStoreErrorCode.BUDGET_EXCEEDED)
    if _validate_candidate_entries(candidate) != names_before:
        _raise(r.RevisionStoreErrorCode.CORRUPT_CONTENT)

    final_name = r._revision_key(revision_id)
    final_path = context["revisions_path"] / final_name
    if not _entry_missing(final_path):
        _raise(r.RevisionStoreErrorCode.CONFLICT)
    temp_name = ".revision." + secrets.token_hex(16) + ".tmp"
    publishing = _reservation_body(
        project_id,
        journal.expected_head,
        revision_id,
        reservation["key_sha256"],
        reservation["ceiling_files"],
        "publishing",
        temp_name,
    )
    with _quota_mutation(store):
        _replace_reservation(store, publishing)
        temp_path = context["revisions_path"] / temp_name
        revision_capability = _create_directory(temp_path, context["revisions"])
        _write_new_file(revision_capability, "model.FCStd", model_raw)
        _write_new_file(revision_capability, "model.step", step_raw)
        model_id = r._new_artifact_id()
        step_id = r._new_artifact_id()
        if (
            r._identifier_code(model_id, r._ARTIFACT_PATTERN) is not None
            or r._identifier_code(step_id, r._ARTIFACT_PATTERN) is not None
        ):
            _raise(r.RevisionStoreErrorCode.INVALID_IDENTIFIER)
        if model_id == step_id:
            _raise(r.RevisionStoreErrorCode.INVALID_INPUT)
        model = r.RevisionArtifactRef(
            id=model_id,
            name="model.FCStd",
            format="fcstd",
            sha256=hashlib.sha256(model_raw).hexdigest(),
            size_bytes=len(model_raw),
        )
        step = r.RevisionArtifactRef(
            id=step_id,
            name="model.step",
            format="step",
            sha256=hashlib.sha256(step_raw).hexdigest(),
            size_bytes=len(step_raw),
        )
        manifest_raw = r._checked_record_bytes(
            r._manifest_body(
                project_id,
                revision_id,
                journal.expected_head.revision_id,
                model,
                (step,),
            ),
            r._MANIFEST_CHECKSUM_DOMAIN,
        )
        _write_new_file(revision_capability, "manifest.json", manifest_raw)
        moved = _rename_directory(
            context["revisions"],
            temp_name,
            final_name,
            revision_capability,
        )
        sealed = r.RevisionRef(
            id=revision_id,
            project_id=project_id,
            base_revision=journal.expected_head.revision_id,
            manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
            model=model,
            artifacts=(step,),
        )
        readback, readback_capability = _load_revision_context(
            context,
            project_id,
            revision_id,
        )
        if readback != sealed or not _same_object(moved, readback_capability):
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        prepared = r.CommitJournal(
            id=journal.id,
            project_id=project_id,
            expected_head=journal.expected_head,
            candidate_revision=revision_id,
            manifest_sha256=sealed.manifest_sha256,
            state=r.CommitJournalState.PREPARED,
        )
        _write_journal(context, prepared)
        cleanup_failed = _remove_candidate(context, revision_id)
        reservation_failed = _remove_reservation(store, revision_id)
    _validate_lease_after(store, lease)
    if cleanup_failed or reservation_failed:
        _raise(r.RevisionStoreErrorCode.CLEANUP_REQUIRED)
    return sealed


def prepare_revision(
    store,
    project_id,
    expected_head,
    revision_id,
    manifest_sha256,
    lease,
):
    r = _r()
    _require_lease(store, project_id, lease)
    if (
        type(expected_head) is not r.ProjectHead
        or expected_head.project_id != project_id
        or r._identifier_code(revision_id, r._REVISION_PATTERN) is not None
        or r._digest_code(manifest_sha256) is not None
    ):
        _raise(r.RevisionStoreErrorCode.INVALID_INPUT)
    context = _project_context(store, project_id)
    head = _load_head_context(context, project_id)
    if head != expected_head:
        _raise(r.RevisionStoreErrorCode.CONFLICT)
    journal = _load_journal(context)
    if journal is not None:
        if journal.state not in {
            r.CommitJournalState.COMMITTED,
            r.CommitJournalState.NOT_COMMITTED,
        } or not _terminal_journal_matches_head(head, journal):
            _raise(r.RevisionStoreErrorCode.CONFLICT)
    sealed, _capability = _load_revision_context(context, project_id, revision_id)
    if (
        sealed.base_revision != expected_head.revision_id
        or sealed.manifest_sha256 != manifest_sha256
    ):
        _raise(r.RevisionStoreErrorCode.CONFLICT)
    transaction_id = r._new_transaction_id()
    if r._identifier_code(transaction_id, r._TRANSACTION_PATTERN) is not None:
        _raise(r.RevisionStoreErrorCode.INVALID_IDENTIFIER)
    prepared = r.CommitJournal(
        id=transaction_id,
        project_id=project_id,
        expected_head=expected_head,
        candidate_revision=revision_id,
        manifest_sha256=sealed.manifest_sha256,
        state=r.CommitJournalState.PREPARED,
    )
    with _quota_mutation(store):
        _write_journal(context, prepared)
    _validate_lease_after(store, lease)
    return sealed


def commit_revision(store, project_id, expected_head, revision_id, lease):
    r = _r()
    _require_lease(store, project_id, lease)
    if (
        type(expected_head) is not r.ProjectHead
        or expected_head.project_id != project_id
        or r._identifier_code(revision_id, r._REVISION_PATTERN) is not None
    ):
        _raise(r.RevisionStoreErrorCode.INVALID_INPUT)
    if expected_head.generation >= r.MAX_SAFE_JSON_INTEGER:
        _raise(r.RevisionStoreErrorCode.BUDGET_EXCEEDED)
    context = _project_context(store, project_id)
    current = _load_head_context(context, project_id)
    if current != expected_head:
        _raise(r.RevisionStoreErrorCode.CONFLICT)
    journal = _load_journal(context)
    if journal is None:
        _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
    if (
        journal.state is not r.CommitJournalState.PREPARED
        or journal.candidate_revision != revision_id
        or journal.expected_head != expected_head
    ):
        _raise(r.RevisionStoreErrorCode.CONFLICT)
    sealed, _capability = _load_revision_context(context, project_id, revision_id)
    if (
        sealed.manifest_sha256 != journal.manifest_sha256
        or sealed.base_revision != expected_head.revision_id
    ):
        _raise(r.RevisionStoreErrorCode.CORRUPT_RECORD)
    new_head = r.ProjectHead(
        project_id=project_id,
        generation=expected_head.generation + 1,
        revision_id=revision_id,
        manifest_sha256=sealed.manifest_sha256,
    )
    raw = r._checked_record_bytes(r._head_mapping(new_head), r._HEAD_CHECKSUM_DOMAIN)
    _replace_record(context["project"], "HEAD.json", raw)
    # From this point onward HEAD is committed.  Any inability to prove the
    # terminal journal or lease state is explicitly reported as such.
    try:
        if _load_head_context(context, project_id) != new_head:
            raise OSError
        committed = r.CommitJournal(
            id=journal.id,
            project_id=project_id,
            expected_head=journal.expected_head,
            candidate_revision=revision_id,
            manifest_sha256=journal.manifest_sha256,
            state=r.CommitJournalState.COMMITTED,
        )
        _write_journal(context, committed)
        _validate_lease_after(store, lease)
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        _raise(r.RevisionStoreErrorCode.DURABILITY_UNCERTAIN, head_committed=True)
    return new_head


def _persist_terminal(context, journal, state):
    r = _r()
    digest = journal.manifest_sha256
    if state is r.CommitJournalState.NOT_COMMITTED and digest is None:
        digest = journal.expected_head.manifest_sha256
    terminal = r.CommitJournal(
        id=journal.id,
        project_id=journal.project_id,
        expected_head=journal.expected_head,
        candidate_revision=journal.candidate_revision,
        manifest_sha256=digest,
        state=state,
    )
    _write_journal(context, terminal)
    return terminal


def reconcile(store, project_id, lease):
    r = _r()
    _require_lease(store, project_id, lease)
    context = _project_context(store, project_id)
    head = _load_head_context(context, project_id)
    journal = _load_journal(context)
    if journal is None:
        _validate_lease_after(store, lease)
        return r.ReconciliationResult(
            project_id=project_id,
            status=r.ReconciliationStatus.CLEAN,
            head=head,
            journal=None,
        )
    if journal.project_id != project_id:
        _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
    old_match = head == journal.expected_head
    new_match = (
        head.generation == journal.expected_head.generation + 1
        and head.revision_id == journal.candidate_revision
        and head.manifest_sha256 == journal.manifest_sha256
    )
    if new_match:
        if journal.state not in {
            r.CommitJournalState.PREPARED,
            r.CommitJournalState.COMMITTED,
        }:
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        sealed, _capability = _load_revision_context(
            context,
            project_id,
            journal.candidate_revision,
        )
        if (
            sealed.manifest_sha256 != journal.manifest_sha256
            or sealed.base_revision != journal.expected_head.revision_id
        ):
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        result_journal = journal
        if journal.state is r.CommitJournalState.PREPARED:
            with _quota_mutation(store):
                result_journal = _persist_terminal(
                    context,
                    journal,
                    r.CommitJournalState.COMMITTED,
                )
        _validate_lease_after(store, lease)
        return r.ReconciliationResult(
            project_id=project_id,
            status=r.ReconciliationStatus.COMMITTED,
            head=head,
            journal=result_journal,
        )
    if not old_match or journal.state is r.CommitJournalState.COMMITTED:
        _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
    if journal.state is r.CommitJournalState.PREPARED:
        sealed, _capability = _load_revision_context(
            context,
            project_id,
            journal.candidate_revision,
        )
        if (
            sealed.manifest_sha256 != journal.manifest_sha256
            or sealed.base_revision != journal.expected_head.revision_id
        ):
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
    with _quota_mutation(store):
        result_journal = journal
        if journal.state is not r.CommitJournalState.NOT_COMMITTED:
            result_journal = _persist_terminal(
                context,
                journal,
                r.CommitJournalState.NOT_COMMITTED,
            )
        cleanup_failed = False
        try:
            _candidate_context(context, journal.candidate_revision)
        except r.RevisionStoreError as error:
            if error.code is not r.RevisionStoreErrorCode.NOT_FOUND:
                cleanup_failed = True
        else:
            cleanup_failed = _remove_candidate(context, journal.candidate_revision)
        try:
            _load_reservation(store, journal.candidate_revision)
        except r.RevisionStoreError as error:
            if error.code is not r.RevisionStoreErrorCode.NOT_FOUND:
                cleanup_failed = True
        else:
            cleanup_failed = (
                _remove_reservation(store, journal.candidate_revision) or cleanup_failed
            )
    _validate_lease_after(store, lease)
    return r.ReconciliationResult(
        project_id=project_id,
        status=(
            r.ReconciliationStatus.CLEANUP_REQUIRED
            if cleanup_failed
            else r.ReconciliationStatus.NOT_COMMITTED
        ),
        head=head,
        journal=result_journal,
    )


def reconcile_candidate_reservation(
    store,
    project_id,
    base_revision,
    reservation_key,
    key_digest,
    lease,
):
    """Cancel one exact candidate reservation under Win32 capability authority."""

    r = _r()
    _require_lease(store, project_id, lease)
    expected_ceiling_files = (
        9 if re.fullmatch(r"revert:[0-9a-f]{64}", reservation_key) is not None else 8
    )
    delegate_reconcile = False
    cleanup_failed = False
    head = None
    with _quota_mutation(store):
        context = _project_context(store, project_id)
        head = _load_head_context(context, project_id)
        reservations, _snapshot = _quota_admission_state(store)
        project_reservations = tuple(
            reservation for reservation in reservations if reservation["project_id"] == project_id
        )
        matching = tuple(
            reservation
            for reservation in project_reservations
            if reservation["key_sha256"] == key_digest
        )
        if len(project_reservations) > 1 or len(matching) > 1:
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        if not matching:
            validate_windows_path(context["candidates"], directory=True)
            validate_windows_path(context["project"], directory=True)
        else:
            reservation = matching[0]
            if (
                reservation["kind"] != "candidate"
                or reservation["expected_head"] != head
                or head.revision_id != base_revision
                or reservation["state"] not in {"reserved", "staged"}
                or reservation["project_temp"] is not None
                or reservation["revision_temp"] is not None
                or reservation["ceiling_files"] != expected_ceiling_files
            ):
                _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
            journal = _load_journal(context)
            if journal is not None and (
                journal.project_id != project_id
                or journal.expected_head != head
                or journal.state
                not in {
                    r.CommitJournalState.STAGING,
                    r.CommitJournalState.NOT_COMMITTED,
                }
                or journal.candidate_revision != reservation["revision_id"]
            ):
                _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
            candidate_names = _directory_names(context["candidates"])
            expected_name = r._candidate_key(reservation["revision_id"])
            if candidate_names not in {(), (expected_name,)}:
                _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
            delegate_reconcile = journal is not None
            if not delegate_reconcile:
                if candidate_names:
                    cleanup_failed = _remove_candidate(
                        context,
                        reservation["revision_id"],
                    )
                cleanup_failed = (
                    _remove_reservation(store, reservation["revision_id"])
                    or cleanup_failed
                )
            validate_windows_path(context["candidates"], directory=True)
            validate_windows_path(context["project"], directory=True)
    assert head is not None
    _validate_lease_after(store, lease)
    if not matching:
        return r.CandidateReservationReconciliation(
            project_id=project_id,
            status=r.CandidateReservationStatus.ABSENT,
            head=head,
        )
    if delegate_reconcile:
        reconciled = reconcile(store, project_id, lease)
        if reconciled.status is r.ReconciliationStatus.CLEANUP_REQUIRED:
            status = r.CandidateReservationStatus.CLEANUP_REQUIRED
        elif reconciled.status is r.ReconciliationStatus.NOT_COMMITTED:
            status = r.CandidateReservationStatus.NOT_COMMITTED
        else:
            _raise(r.RevisionStoreErrorCode.RECOVERY_REQUIRED)
        return r.CandidateReservationReconciliation(
            project_id=project_id,
            status=status,
            head=reconciled.head,
        )
    return r.CandidateReservationReconciliation(
        project_id=project_id,
        status=(
            r.CandidateReservationStatus.CLEANUP_REQUIRED
            if cleanup_failed
            else r.CandidateReservationStatus.NOT_COMMITTED
        ),
        head=head,
    )


def rollback_revision(store, project_id, revision_id, lease):
    r = _r()
    _require_lease(store, project_id, lease)
    if r._identifier_code(revision_id, r._REVISION_PATTERN) is not None:
        _raise(r.RevisionStoreErrorCode.INVALID_IDENTIFIER)
    context = _project_context(store, project_id)
    journal = _load_journal(context)
    if journal is None or journal.candidate_revision != revision_id:
        _raise(r.RevisionStoreErrorCode.CONFLICT)
    return reconcile(store, project_id, lease)
