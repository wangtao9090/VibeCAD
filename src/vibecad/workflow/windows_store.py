"""Windows authority backend for the durable TaskRun store."""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

from vibecad._file_compat import (
    WindowsPathCapability,
    capture_windows_fd,
    capture_windows_path,
    close_windows_handle,
    delete_windows_file,
    open_private_file,
    open_windows_directory_handle,
    pread,
    replace_windows_file,
    validate_windows_handle_path,
    validate_windows_path,
)
from vibecad.workflow import store as _store

_REQUIRED_NATIVE_CALLABLES = (
    "capture_windows_fd",
    "capture_windows_path",
    "close_windows_handle",
    "delete_windows_file",
    "open_private_file",
    "open_windows_directory_handle",
    "pread",
    "replace_windows_file",
    "validate_windows_handle_path",
    "validate_windows_path",
)


def require_windows_storage_capabilities() -> None:
    """Fail before leasing if a native authority primitive is unavailable."""

    module_globals = globals()
    invalid = any(
        not callable(module_globals.get(name)) for name in _REQUIRED_NATIVE_CALLABLES
    ) or any(
        not callable(getattr(os, name, None))
        for name in ("close", "fstat", "fsync", "scandir", "write")
    )
    if invalid:
        raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)


class WindowsTaskStoreBackend:
    """Persist task records using DACL- and File-ID-bound Windows operations."""

    __slots__ = ("_root", "_root_capability", "identity")

    def __init__(self, root: Path) -> None:
        if os.name != "nt":
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
        absolute = Path(os.path.abspath(root))
        if absolute != root or not absolute.is_absolute():
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
        capture_failed = False
        try:
            capability = capture_windows_path(absolute, directory=True)
            validate_windows_path(capability, directory=True)
        except (OSError, TypeError, ValueError):
            capture_failed = True
            capability = None
        if capture_failed or capability is None:
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
        self._root = absolute
        self._root_capability = capability
        self.identity = (capability.volume, capability.file_id)

    def _open_root(self) -> int:
        handle: int | None = None
        failed = False
        try:
            handle = open_windows_directory_handle(
                self._root,
                inheritable=False,
                deny_delete=True,
            )
            validate_windows_handle_path(
                handle,
                self._root,
                directory=True,
                expected=self._root_capability,
            )
        except (OSError, TypeError, ValueError):
            if handle is not None:
                try:
                    close_windows_handle(handle)
                except OSError:
                    pass
            handle = None
            failed = True
        if failed or handle is None:
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
        return handle

    def _validate_root_handle(self, handle: int) -> None:
        try:
            validate_windows_handle_path(
                handle,
                self._root,
                directory=True,
                expected=self._root_capability,
            )
        except (OSError, TypeError, ValueError):
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE) from None

    def _path(self, name: str) -> Path:
        if type(name) is not str or not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
        return self._root / name

    def _read_file(
        self,
        name: str,
        *,
        limit: int,
        too_large: _store.TaskStoreErrorCode,
    ) -> tuple[bytes, WindowsPathCapability] | None:
        path = self._path(name)
        root_handle = self._open_root()
        fd = -1
        capability: WindowsPathCapability | None = None
        failure: _store.TaskStoreError | None = None
        raw: bytes | None = None
        try:
            try:
                fd, capability = open_private_file(
                    path,
                    create=False,
                    read_write=False,
                    expected_parent=self._root_capability,
                )
            except FileNotFoundError:
                return None
            opened = os.fstat(fd)
            if type(opened.st_size) is not int or opened.st_size < 0:
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
            if opened.st_size > limit:
                raise _store.TaskStoreError(too_large)
            chunks: list[bytes] = []
            offset = 0
            while offset <= limit:
                read_failed = False
                try:
                    chunk = pread(fd, min(65_536, limit + 1 - offset), offset)
                except OSError:
                    read_failed = True
                    chunk = b""
                if read_failed:
                    raise _store.TaskStoreError(_store.TaskStoreErrorCode.IO_ERROR)
                if not chunk:
                    break
                chunks.append(chunk)
                offset += len(chunk)
            if offset > limit:
                raise _store.TaskStoreError(too_large)
            raw = b"".join(chunks)
            if len(raw) != opened.st_size:
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
            captured = capture_windows_fd(
                fd,
                directory=False,
                generation_token=capability.generation_token,
            )
            if captured != capability:
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
            validate_windows_path(capability, directory=False)
            self._validate_root_handle(root_handle)
        except _store.TaskStoreError as error:
            failure = error
        except (OSError, TypeError, ValueError):
            failure = _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
        finally:
            close_failed = False
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    close_failed = True
            close_windows_handle(root_handle)
        if failure is not None:
            raise failure
        if close_failed or raw is None or capability is None:
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.IO_ERROR)
        return raw, capability

    def _write_new(self, name: str, raw: bytes) -> WindowsPathCapability:
        path = self._path(name)
        root_handle = self._open_root()
        fd = -1
        capability: WindowsPathCapability | None = None
        failure: _store.TaskStoreError | None = None
        try:
            fd, capability = open_private_file(
                path,
                create=True,
                read_write=True,
                exclusive=True,
                expected_parent=self._root_capability,
            )
            offset = 0
            while offset < len(raw):
                written = os.write(fd, raw[offset:])
                if type(written) is not int or written <= 0:
                    raise OSError("short Windows task-store write")
                offset += written
            os.fsync(fd)
            if (
                capture_windows_fd(
                    fd,
                    directory=False,
                    generation_token=capability.generation_token,
                )
                != capability
            ):
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
            self._validate_root_handle(root_handle)
        except FileExistsError:
            failure = _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
        except _store.TaskStoreError as error:
            failure = error
        except (OSError, TypeError, ValueError):
            failure = _store.TaskStoreError(_store.TaskStoreErrorCode.IO_ERROR)
        finally:
            close_failed = False
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    close_failed = True
            close_windows_handle(root_handle)
        if failure is not None or close_failed or capability is None:
            if capability is not None:
                try:
                    delete_windows_file(
                        path,
                        parent=self._root_capability,
                        expected=capability,
                    )
                except OSError:
                    pass
            if failure is not None:
                raise failure
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.IO_ERROR)
        try:
            validate_windows_path(capability, directory=False)
        except (OSError, TypeError, ValueError):
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE) from None
        return capability

    def _delete(self, name: str, capability: WindowsPathCapability) -> None:
        try:
            delete_windows_file(
                self._path(name),
                parent=self._root_capability,
                expected=capability,
            )
        except (OSError, TypeError, ValueError):
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.IO_ERROR) from None

    def _sync_file(self, name: str, capability: WindowsPathCapability) -> None:
        """Durably revalidate a recovered file before making it authoritative."""

        path = self._path(name)
        root_handle = self._open_root()
        fd = -1
        failure: _store.TaskStoreError | None = None
        try:
            fd, _ = open_private_file(
                path,
                create=False,
                read_write=True,
                expected_parent=self._root_capability,
            )
            if (
                capture_windows_fd(
                    fd,
                    directory=False,
                    generation_token=capability.generation_token,
                )
                != capability
            ):
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
            os.fsync(fd)
            if (
                capture_windows_fd(
                    fd,
                    directory=False,
                    generation_token=capability.generation_token,
                )
                != capability
            ):
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
            validate_windows_path(capability, directory=False)
            self._validate_root_handle(root_handle)
        except _store.TaskStoreError as error:
            failure = error
        except (OSError, TypeError, ValueError):
            failure = _store.TaskStoreError(_store.TaskStoreErrorCode.IO_ERROR)
        finally:
            close_failed = False
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    close_failed = True
            close_windows_handle(root_handle)
        if failure is not None:
            raise failure
        if close_failed:
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.IO_ERROR)

    def _replace(
        self,
        source_name: str,
        destination_name: str,
        source: WindowsPathCapability,
        destination: WindowsPathCapability | None,
    ) -> WindowsPathCapability:
        destination_path = self._path(destination_name)
        if destination is None and os.path.lexists(destination_path):
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.CONFLICT)
        try:
            return replace_windows_file(
                self._path(source_name),
                destination_path,
                source_parent=self._root_capability,
                expected_source=source,
                expected_destination=destination,
            )
        except (OSError, TypeError, ValueError):
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.IO_ERROR) from None

    def _read_record_with_capability(
        self,
        task_id: str,
    ) -> tuple[_store.StoredTaskRun | None, WindowsPathCapability | None]:
        item = self._read_file(
            _store._record_name(task_id),
            limit=_store._MAX_RECORD_BYTES,
            too_large=_store.TaskStoreErrorCode.RECORD_TOO_LARGE,
        )
        if item is None:
            return None, None
        raw, capability = item
        return _store._decode_record(raw, task_id), capability

    def load(self, task_id: str):
        return self._read_record_with_capability(task_id)[0]

    def exists(self, task_id: str) -> bool:
        return self._read_record_with_capability(task_id)[0] is not None

    def _names(self) -> tuple[str, ...]:
        handle = self._open_root()
        try:
            names = tuple(entry.name for entry in os.scandir(self._root))
            if any(type(name) is not str or name in {"", ".", ".."} for name in names):
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
            if len(set(names)) != len(names):
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
            self._validate_root_handle(handle)
            return tuple(sorted(names))
        except _store.TaskStoreError:
            raise
        except OSError:
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.IO_ERROR) from None
        finally:
            close_windows_handle(handle)

    def _scan(self) -> _store._StoreSnapshot:
        total_bytes = 0
        record_bytes = 0
        record_count = 0
        journal_present = False
        temp_names: list[str] = []
        for name in self._names():
            if _store._RECORD_NAME_RE.fullmatch(name) is not None:
                item = self._read_file(
                    name,
                    limit=_store._MAX_RECORD_BYTES,
                    too_large=_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED,
                )
                if item is None:
                    raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
                raw, _capability = item
                _store._record_task_id_for_scan(raw, name)
                record_count += 1
                record_bytes += len(raw)
                total_bytes += len(raw)
            elif name == _store._MUTATION_JOURNAL_NAME:
                item = self._read_file(
                    name,
                    limit=_store._MAX_JOURNAL_BYTES,
                    too_large=_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED,
                )
                if item is None or journal_present:
                    raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
                journal_present = True
                total_bytes += len(item[0])
            elif _store._TEMP_NAME_RE.fullmatch(name) is not None:
                item = self._read_file(
                    name,
                    limit=_store._MAX_RECORD_BYTES,
                    too_large=_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED,
                )
                if item is None:
                    raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
                temp_names.append(name)
                total_bytes += len(item[0])
            else:
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
        if (
            total_bytes > _store._MAX_TASK_STORE_BYTES
            or record_count > _store._MAX_TASK_RECORDS
            or len(temp_names) > 1
            or (temp_names and not journal_present)
        ):
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
        return _store._StoreSnapshot(
            total_bytes=total_bytes,
            record_bytes=record_bytes,
            record_count=record_count,
            journal_present=journal_present,
            temp_names=tuple(sorted(temp_names)),
        )

    def snapshot(self) -> tuple[_store.TaskSnapshotEntry, ...]:
        names = self._names()
        for name in names:
            if _store._RECORD_NAME_RE.fullmatch(name) is not None:
                continue
            if name == _store._MUTATION_JOURNAL_NAME or (
                _store._TEMP_NAME_RE.fullmatch(name) is not None
            ):
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.CORRUPT_RECORD)
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
        records: list[_store.TaskSnapshotEntry] = []
        total_bytes = 0
        for name in names:
            item = self._read_file(
                name,
                limit=_store._MAX_RECORD_BYTES,
                too_large=_store.TaskStoreErrorCode.RECORD_TOO_LARGE,
            )
            if item is None:
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.UNSAFE_STORE)
            raw, _capability = item
            total_bytes += len(raw)
            if (
                len(records) >= _store._MAX_TASK_RECORDS
                or total_bytes > _store._MAX_TASK_STORE_BYTES
            ):
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
            task_id = _store._snapshot_task_id(raw, name)
            stored = _store._decode_record(raw, task_id)
            task = stored.task_run
            records.append(
                _store.TaskSnapshotEntry(
                    task_id=task.id,
                    project_id=task.project_id,
                    generation=stored.generation,
                    base_revision=task.base_revision,
                    reasoning_owner=task.reasoning_owner.value,
                    review_policy=task.review_policy.value,
                    status=task.status.value,
                    next_action=task.next_action.value,
                    candidate_revision=task.candidate_revision,
                    committed_revision=task.committed_revision,
                    draft_id=None if task.draft is None else task.draft.id,
                    record_sha256=hashlib.sha256(raw).hexdigest(),
                )
            )
        records.sort(key=lambda item: item.task_id)
        if len({item.task_id for item in records}) != len(records):
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
        return tuple(records)

    def prepare_mutation(
        self,
        task_id: str,
        expected_generation: int | None,
        raw: bytes,
    ) -> tuple[str, bytes, bytes]:
        snapshot = self._scan()
        if snapshot.journal_present or snapshot.temp_names:
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
        current, _current_capability = self._read_record_with_capability(task_id)
        _store.TaskRunStore._require_expected(current, expected_generation)
        old_sha256, _old_size = _store._record_sha256(current)
        new_sha256 = hashlib.sha256(raw).hexdigest()
        target = _store._record_name(task_id)
        temp_name = f".{target}.{secrets.token_hex(16)}.tmp"
        reserved = _store._journal_line(
            _store._journal_body(
                state="RESERVED",
                task_id=task_id,
                target=target,
                old_sha256=old_sha256,
                new_sha256=new_sha256,
                new_size=len(raw),
                temp_name=temp_name,
            )
        )
        staged_bound = _store.TaskRunStore._staged_bound_line(
            task_id=task_id,
            target=target,
            old_sha256=old_sha256,
            new_sha256=new_sha256,
            new_size=len(raw),
            temp_name=temp_name,
        )
        _store._assert_capacity(
            snapshot,
            current=current,
            raw=raw,
            reserved_line=reserved,
            staged_bound_line=staged_bound,
        )
        return temp_name, reserved, staged_bound

    def _read_journal(
        self,
    ) -> tuple[dict[str, object], WindowsPathCapability]:
        item = self._read_file(
            _store._MUTATION_JOURNAL_NAME,
            limit=_store._MAX_JOURNAL_BYTES,
            too_large=_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED,
        )
        if item is None:
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.IO_ERROR)
        raw, capability = item
        parts = raw.split(b"\n")
        if len(parts) != 2 or parts[1] != b"" or not parts[0]:
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
        body = _store._decode_journal_entry(parts[0])
        if body["state"] != "RESERVED":
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
        return body, capability

    def recover(self, owner: _store.TaskRunStore) -> None:
        snapshot = self._scan()
        if not snapshot.journal_present:
            if snapshot.temp_names:
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
            return
        journal, _capability = self._read_journal()
        lease = owner._acquire(journal["task_id"])
        try:
            self.recover_locked()
        finally:
            release_ok = _store._release(lease)
        if not release_ok:
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.IO_ERROR)

    def recover_locked(self) -> None:
        snapshot = self._scan()
        if not snapshot.journal_present:
            if snapshot.temp_names:
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
            return
        journal, journal_capability = self._read_journal()
        task_id = journal["task_id"]
        target = journal["target"]
        temp_name = journal["temp_name"]
        current, current_capability = self._read_record_with_capability(task_id)
        current_sha256, _current_size = _store._record_sha256(current)
        temp_present = temp_name in snapshot.temp_names
        if snapshot.temp_names and not temp_present:
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
        if not temp_present:
            if current_sha256 not in {journal["old_sha256"], journal["new_sha256"]}:
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
            self._delete(_store._MUTATION_JOURNAL_NAME, journal_capability)
            return
        temp_item = self._read_file(
            temp_name,
            limit=_store._MAX_RECORD_BYTES,
            too_large=_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED,
        )
        if temp_item is None:
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
        temp_raw, temp_capability = temp_item
        if len(temp_raw) != journal["new_size"] or not secrets.compare_digest(
            hashlib.sha256(temp_raw).hexdigest(),
            journal["new_sha256"],
        ):
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
        _store._decode_record(temp_raw, task_id)
        if current_sha256 == journal["old_sha256"]:
            self._sync_file(temp_name, temp_capability)
            self._replace(
                temp_name,
                target,
                temp_capability,
                current_capability,
            )
            readback, _readback_capability = self._read_record_with_capability(task_id)
            readback_sha256, _readback_size = _store._record_sha256(readback)
            if readback_sha256 != journal["new_sha256"]:
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.IO_ERROR)
        elif current_sha256 == journal["new_sha256"]:
            self._delete(temp_name, temp_capability)
        else:
            raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
        self._delete(_store._MUTATION_JOURNAL_NAME, journal_capability)

    def mutate_locked(
        self,
        task_id: str,
        expected_generation: int | None,
        next_generation: int,
        task_run,
        raw: bytes,
        prepared: tuple[str, bytes, bytes],
    ) -> _store.StoredTaskRun:
        temp_name, reserved_line, staged_bound = prepared
        journal_capability: WindowsPathCapability | None = None
        temp_capability: WindowsPathCapability | None = None
        replaced = False
        publication_identity_invalid = False
        try:
            snapshot = self._scan()
            if snapshot.journal_present or snapshot.temp_names:
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED)
            current, current_capability = self._read_record_with_capability(task_id)
            _store.TaskRunStore._require_expected(current, expected_generation)
            if current is not None and current.task_run.creation_digest != task_run.creation_digest:
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.CONFLICT)
            _store._assert_capacity(
                snapshot,
                current=current,
                raw=raw,
                reserved_line=reserved_line,
                staged_bound_line=staged_bound,
            )
            journal_capability = self._write_new(
                _store._MUTATION_JOURNAL_NAME,
                reserved_line,
            )
            temp_capability = self._write_new(temp_name, raw)
            latest, latest_capability = self._read_record_with_capability(task_id)
            _store.TaskRunStore._require_expected(latest, expected_generation)
            if latest is not None and latest.task_run.creation_digest != task_run.creation_digest:
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.CONFLICT)
            current_capability = latest_capability
            try:
                validate_windows_path(temp_capability, directory=False)
            except (OSError, TypeError, ValueError):
                publication_identity_invalid = True
                raise _store.TaskStoreError(_store.TaskStoreErrorCode.RESOURCE_EXHAUSTED) from None
            self._replace(
                temp_name,
                _store._record_name(task_id),
                temp_capability,
                current_capability,
            )
            replaced = True
            temp_capability = None
            readback, _readback_capability = self._read_record_with_capability(task_id)
            if (
                readback is None
                or readback.generation != next_generation
                or readback.task_run != task_run
            ):
                raise _store.TaskStoreError(
                    _store.TaskStoreErrorCode.DURABILITY_UNCERTAIN,
                    committed_generation=next_generation,
                )
            self._delete(_store._MUTATION_JOURNAL_NAME, journal_capability)
            journal_capability = None
            return readback
        except _store.TaskStoreError as error:
            cleanup_failed = False
            if not replaced and temp_capability is not None and not publication_identity_invalid:
                try:
                    self._delete(temp_name, temp_capability)
                    temp_capability = None
                except _store.TaskStoreError:
                    cleanup_failed = True
            if not replaced and journal_capability is not None and temp_capability is None:
                try:
                    self._delete(_store._MUTATION_JOURNAL_NAME, journal_capability)
                    journal_capability = None
                except _store.TaskStoreError:
                    cleanup_failed = True
            if (
                replaced
                or cleanup_failed
                or (
                    journal_capability is not None
                    and temp_capability is not None
                    and not publication_identity_invalid
                )
            ):
                raise _store.TaskStoreError(
                    _store.TaskStoreErrorCode.DURABILITY_UNCERTAIN,
                    committed_generation=next_generation,
                ) from None
            raise error
        except (OSError, TypeError, ValueError):
            raise _store.TaskStoreError(
                (
                    _store.TaskStoreErrorCode.DURABILITY_UNCERTAIN
                    if replaced
                    else _store.TaskStoreErrorCode.IO_ERROR
                ),
                **({"committed_generation": next_generation} if replaced else {}),
            ) from None
