from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

if os.name == "nt":
    try:
        from vibecad import _file_compat as _windows_files
    except ImportError:  # pragma: no cover - fail-closed packaging boundary
        _windows_files = None
else:
    _windows_files = None

__all__ = ("PreviewBinding", "PreviewCoordinator", "PreviewError")

_PROJECT_ID = re.compile(r"project_[0-9a-f]{32}")
_REVISION_ID = re.compile(r"revision_[0-9a-f]{32}")
_TASK_ID = re.compile(r"task_[0-9a-f]{32}")
_DRAFT_ID = re.compile(r"draft_[0-9a-f]{32}")
_CHECKOUT_ID = re.compile(r"checkout_[0-9a-f]{32}")
_OPEN_KEY = re.compile(r"checkout_open_[0-9a-f]{32}")
_GRANT_ID = re.compile(r"file_grant_[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_CYCLE_AUTHORITIES = 8
_MAX_ATTESTED_FILE_BYTES = 536_870_912
_ATTEST_READ_BYTES = 1_048_576
_MISSING_DOCUMENT_API = object()
_DESCRIPTOR_KEYS = frozenset(
    (
        "checkout_id",
        "open_key",
        "state",
        "authoritative",
        "dirty",
        "source",
        "initial_model_sha256",
        "current_model_sha256",
        "current_size_bytes",
        "source_head",
        "source_liveness",
    )
)
_RESOLVED_SOURCE_KEYS = frozenset(
    (
        "kind",
        "project_id",
        "revision_id",
        "manifest_sha256",
        "model_sha256",
        "size_bytes",
        "task_id",
        "draft_id",
        "task_generation",
    )
)
_SOURCE_HEAD_KEYS = frozenset(
    (
        "schema_version",
        "project_id",
        "generation",
        "revision_id",
        "manifest_sha256",
    )
)
_GRANT_KEYS = frozenset(("schema_version", "grant_id", "purpose", "expires_in_ms"))
_CLAIM_KEYS = frozenset(
    (
        "schema_version",
        "grant_id",
        "checkout_id",
        "purpose",
        "local_path",
        "current_model_sha256",
        "current_size_bytes",
    )
)


class PreviewError(ValueError):
    def __init__(
        self,
        message: str = "invalid preview mapping",
        *,
        primary_error: BaseException | None = None,
        cleanup_error: BaseException | None = None,
        recovery_required: bool = False,
        checkout_id: str | None = None,
        source: dict[str, object] | None = None,
        open_key: str | None = None,
        descriptor: dict[str, object] | None = None,
        cleanup_complete: bool = False,
    ) -> None:
        self.primary_error = primary_error
        self.cleanup_error = cleanup_error
        self.recovery_required = recovery_required
        self.checkout_id = checkout_id
        self.source = source
        self.open_key = open_key
        self.descriptor = descriptor
        self.cleanup_complete = cleanup_complete is True
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PreviewBinding:
    source: object
    open_key: str
    descriptor: object
    checkout: object
    claim: object
    document: object
    document_name: str


@dataclass(slots=True)
class _OwnedPreview:
    checkout_id: str
    source: dict[str, object]
    open_key: str
    descriptor: dict[str, object] | None = None
    binding: PreviewBinding | None = None
    document: object | None = None
    document_name: str | None = None
    document_closed: bool = True
    checkout_closed: bool = False
    ambiguous: bool = False
    cleanup_only: bool = False


@dataclass(slots=True)
class _PreviewCycle:
    cycle_id: int
    authorities: dict[str, _OwnedPreview]
    bindings: dict[str, PreviewBinding]
    poisoned: bool = False
    recovery_required: bool = False
    draining: bool = False
    retirement_ready: bool = False


def _invalid() -> None:
    raise PreviewError()


def _document_is_touched(document: object) -> bool:
    unbound = getattr(type(document), "isTouched", _MISSING_DOCUMENT_API)
    if unbound is _MISSING_DOCUMENT_API:
        value = getattr(document, "Modified", None)
    else:
        if not callable(unbound):
            _invalid()
        value = unbound(document)
    if type(value) is not bool:
        _invalid()
    return value


def _plain_copy(value: object, *, depth: int = 0) -> object:
    if depth > 8:
        _invalid()
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is list:
        return [_plain_copy(item, depth=depth + 1) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _plain_copy(item, depth=depth + 1) for key, item in value.items()}
    _invalid()


def _mapping(
    value: object,
    keys: frozenset[str] | None = None,
) -> dict[str, object]:
    copied = _plain_copy(value)
    if type(copied) is not dict or (keys is not None and set(copied) != keys):
        _invalid()
    return copied


def _identifier(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _invalid()
    return value


def _integer(value: object, *, maximum: int = _MAX_SAFE_INTEGER) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        _invalid()
    return value


def _source(value: object) -> dict[str, object]:
    mapping = _mapping(value)
    kind = mapping.get("kind")
    if kind == "head":
        if set(mapping) != {"kind", "project_id"}:
            _invalid()
        _identifier(mapping["project_id"], _PROJECT_ID)
    elif kind == "draft":
        if set(mapping) != {
            "kind",
            "task_id",
            "draft_id",
            "expected_generation",
        }:
            _invalid()
        _identifier(mapping["task_id"], _TASK_ID)
        _identifier(mapping["draft_id"], _DRAFT_ID)
        _integer(mapping["expected_generation"])
    else:
        _invalid()
    return mapping


def _source_head(value: object) -> dict[str, object]:
    mapping = _mapping(value, _SOURCE_HEAD_KEYS)
    if type(mapping["schema_version"]) is not int or mapping["schema_version"] != 1:
        _invalid()
    _identifier(mapping["project_id"], _PROJECT_ID)
    _integer(mapping["generation"])
    _identifier(mapping["revision_id"], _REVISION_ID)
    _identifier(mapping["manifest_sha256"], _DIGEST)
    return mapping


def _resolved_source(
    value: object,
    requested: dict[str, object],
) -> dict[str, object]:
    mapping = _mapping(value, _RESOLVED_SOURCE_KEYS)
    if mapping["kind"] != requested["kind"]:
        _invalid()
    _identifier(mapping["project_id"], _PROJECT_ID)
    _identifier(mapping["revision_id"], _REVISION_ID)
    _identifier(mapping["manifest_sha256"], _DIGEST)
    _identifier(mapping["model_sha256"], _DIGEST)
    _integer(mapping["size_bytes"])
    if requested["kind"] == "head":
        if mapping["project_id"] != requested["project_id"] or any(
            mapping[key] is not None for key in ("task_id", "draft_id", "task_generation")
        ):
            _invalid()
    elif (
        mapping["task_id"] != requested["task_id"]
        or mapping["draft_id"] != requested["draft_id"]
        or mapping["task_generation"] != requested["expected_generation"]
    ):
        _invalid()
    _identifier(mapping["task_id"], _TASK_ID) if mapping["task_id"] is not None else None
    _identifier(mapping["draft_id"], _DRAFT_ID) if mapping["draft_id"] is not None else None
    if mapping["task_generation"] is not None:
        _integer(mapping["task_generation"])
    return mapping


def _descriptor(
    value: object,
    *,
    requested: dict[str, object],
    open_key: str,
) -> dict[str, object]:
    mapping = _mapping(value, _DESCRIPTOR_KEYS)
    _identifier(mapping["checkout_id"], _CHECKOUT_ID)
    if mapping["open_key"] != open_key:
        _invalid()
    if (
        mapping["state"] not in {"open", "closed"}
        or type(mapping["state"]) is not str
        or mapping["authoritative"] is not False
        or type(mapping["dirty"]) is not bool
        or mapping["source_liveness"] not in {"live", "stale", "revoked", "recovery_required"}
        or type(mapping["source_liveness"]) is not str
    ):
        _invalid()
    resolved = _resolved_source(mapping["source"], requested)
    head = _source_head(mapping["source_head"])
    initial_digest = _identifier(mapping["initial_model_sha256"], _DIGEST)
    current_digest = _identifier(mapping["current_model_sha256"], _DIGEST)
    current_size = _integer(mapping["current_size_bytes"])
    if (
        resolved["project_id"] != head["project_id"]
        or (
            requested["kind"] == "head"
            and (
                resolved["revision_id"] != head["revision_id"]
                or resolved["manifest_sha256"] != head["manifest_sha256"]
            )
        )
        or resolved["model_sha256"] != initial_digest
        or initial_digest != current_digest
        or resolved["size_bytes"] != current_size
    ):
        _invalid()
    return mapping


def _grant(value: object) -> dict[str, object]:
    mapping = _mapping(value, _GRANT_KEYS)
    if (
        type(mapping["schema_version"]) is not int
        or mapping["schema_version"] != 1
        or mapping["purpose"] != "open_managed_checkout"
        or mapping["expires_in_ms"] != 30_000
    ):
        _invalid()
    _identifier(mapping["grant_id"], _GRANT_ID)
    return mapping


def _claim(
    value: object,
    *,
    grant_id: str,
    descriptor: dict[str, object],
) -> dict[str, object]:
    mapping = _mapping(value, _CLAIM_KEYS)
    if (
        type(mapping["schema_version"]) is not int
        or mapping["schema_version"] != 1
        or mapping["grant_id"] != grant_id
        or mapping["checkout_id"] != descriptor["checkout_id"]
        or mapping["purpose"] != "open_managed_checkout"
        or mapping["current_model_sha256"] != descriptor["current_model_sha256"]
        or mapping["current_size_bytes"] != descriptor["current_size_bytes"]
    ):
        _invalid()
    _identifier(mapping["grant_id"], _GRANT_ID)
    _identifier(mapping["checkout_id"], _CHECKOUT_ID)
    _identifier(mapping["current_model_sha256"], _DIGEST)
    _integer(mapping["current_size_bytes"])
    local_path = mapping["local_path"]
    if type(local_path) is not str:
        _invalid()
    path = Path(local_path)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path.name != "model.FCStd"
        or path.parent.name != descriptor["checkout_id"]
        or str(path) != local_path
    ):
        _invalid()
    return mapping


def _freeze(value: object) -> object:
    if type(value) is dict:
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, MappingProxyType) or type(value) is dict:
        return {key: _thaw(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw(item) for item in value]
    return value


def _cleanup_failure(
    primary: BaseException,
    cleanup: BaseException,
    *,
    checkout_id: str,
    source: dict[str, object],
    open_key: str,
    descriptor: dict[str, object] | None,
) -> PreviewError:
    return PreviewError(
        "preview recovery required",
        primary_error=primary,
        cleanup_error=cleanup,
        recovery_required=True,
        checkout_id=checkout_id,
        source=source,
        open_key=open_key,
        descriptor=descriptor,
    )


def _known_acquisition_failure(
    primary: BaseException,
    *,
    checkout_id: str,
    source: dict[str, object],
    open_key: str,
    descriptor: dict[str, object] | None,
) -> PreviewError:
    return PreviewError(
        "preview acquisition failed",
        primary_error=primary,
        recovery_required=False,
        checkout_id=checkout_id,
        source=source,
        open_key=open_key,
        descriptor=descriptor,
        cleanup_complete=True,
    )


def _validate_closed_descriptor(
    value: object,
    *,
    checkout_id: str,
    source: dict[str, object],
    open_key: str,
    descriptor: dict[str, object] | None,
) -> dict[str, object]:
    closed = _mapping(value, _DESCRIPTOR_KEYS)
    if (
        _identifier(closed["checkout_id"], _CHECKOUT_ID) != checkout_id
        or closed["open_key"] != open_key
        or closed["state"] != "closed"
        or closed["authoritative"] is not False
        or type(closed["dirty"]) is not bool
        or closed["source_liveness"] not in {"live", "stale", "revoked", "recovery_required"}
        or type(closed["source_liveness"]) is not str
    ):
        _invalid()
    resolved = _resolved_source(closed["source"], source)
    head = _source_head(closed["source_head"])
    initial_digest = _identifier(closed["initial_model_sha256"], _DIGEST)
    _identifier(closed["current_model_sha256"], _DIGEST)
    _integer(closed["current_size_bytes"])
    if descriptor is not None:
        opened = _mapping(_plain_copy(descriptor), _DESCRIPTOR_KEYS)
        opened_source = _resolved_source(opened["source"], source)
        opened_head = _source_head(opened["source_head"])
        opened_initial = _identifier(opened["initial_model_sha256"], _DIGEST)
        if (
            closed["checkout_id"] != opened["checkout_id"]
            or closed["open_key"] != opened["open_key"]
            or resolved != opened_source
            or initial_digest != opened_initial
            or head["project_id"] != opened_head["project_id"]
        ):
            _invalid()
    elif resolved["model_sha256"] != initial_digest:
        _invalid()
    return closed


def _raw_checkout_identity(
    value: object,
    *,
    open_key: str,
) -> str:
    if type(value) is not dict or value.get("open_key") != open_key:
        raise PreviewError(
            "preview recovery required",
            recovery_required=True,
            open_key=open_key,
        )
    try:
        return _identifier(value.get("checkout_id"), _CHECKOUT_ID)
    except PreviewError as primary:
        raise PreviewError(
            "preview recovery required",
            primary_error=primary,
            recovery_required=True,
            open_key=open_key,
        ) from primary


class PreviewCoordinator:
    def __init__(self, freecad: object) -> None:
        for name in (
            "openDocument",
            "getDocument",
            "listDocuments",
            "closeDocument",
        ):
            if not callable(getattr(freecad, name, None)):
                raise TypeError("freecad document host is required")
        self._freecad = freecad
        self._owner_thread_id = threading.get_ident()
        self._bindings: list[PreviewBinding] = []
        self._owned: dict[str, _OwnedPreview] = {}
        self._disabled: set[int] = set()
        self._cycle: _PreviewCycle | None = None
        self._next_cycle_id = 0
        self._client_closed = False
        self._recovery_required = False

    @staticmethod
    def acquire(
        client: object,
        *,
        source: object,
        open_key: object,
    ) -> dict[str, object]:
        canonical_source = _source(source)
        canonical_open_key = _identifier(open_key, _OPEN_KEY)
        open_checkout = getattr(client, "open_checkout", None)
        claim_file_grant = getattr(client, "claim_file_grant", None)
        close_checkout = getattr(client, "close_checkout", None)
        if (
            not callable(open_checkout)
            or not callable(claim_file_grant)
            or not callable(close_checkout)
        ):
            _invalid()
        opened = open_checkout(
            open_key=canonical_open_key,
            source=canonical_source,
        )
        checkout_id = _raw_checkout_identity(
            opened,
            open_key=canonical_open_key,
        )
        descriptor: dict[str, object] | None = None
        try:
            copied = _mapping(opened)
            if set(copied) != _DESCRIPTOR_KEYS | {"file_grant"}:
                _invalid()
            descriptor_value = dict(copied)
            del descriptor_value["file_grant"]
            descriptor = _descriptor(
                descriptor_value,
                requested=canonical_source,
                open_key=canonical_open_key,
            )
            grant = _grant(copied["file_grant"])
            claim = _claim(
                claim_file_grant(grant_id=grant["grant_id"]),
                grant_id=str(grant["grant_id"]),
                descriptor=descriptor,
            )
        except BaseException as primary:
            try:
                closed = close_checkout(checkout_id=checkout_id)
                _validate_closed_descriptor(
                    closed,
                    checkout_id=checkout_id,
                    source=canonical_source,
                    open_key=canonical_open_key,
                    descriptor=descriptor,
                )
            except BaseException as cleanup:
                raise _cleanup_failure(
                    primary,
                    cleanup,
                    checkout_id=checkout_id,
                    source=canonical_source,
                    open_key=canonical_open_key,
                    descriptor=descriptor,
                ) from cleanup
            raise _known_acquisition_failure(
                primary,
                checkout_id=checkout_id,
                source=canonical_source,
                open_key=canonical_open_key,
                descriptor=descriptor,
            ) from primary
        assert descriptor is not None
        return {
            "source": canonical_source,
            "open_key": canonical_open_key,
            "descriptor": descriptor,
            "claim": claim,
        }

    def _require_thread(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("preview thread authority violation")

    def _registry(self) -> dict[str, object]:
        try:
            registry = self._freecad.listDocuments()
            if type(registry) is not dict or any(
                type(name) is not str or not name for name in registry
            ):
                raise PreviewError("invalid document registry")
            copied = dict(registry)
            if any(
                getattr(document, "Name", None) != name
                or self._freecad.getDocument(name) is not document
                for name, document in copied.items()
            ) or len({id(document) for document in copied.values()}) != len(copied):
                raise PreviewError("invalid document registry")
        except BaseException as error:
            if isinstance(error, PreviewError) and error.recovery_required:
                raise
            raise PreviewError(
                "preview recovery required",
                primary_error=error,
                recovery_required=True,
            ) from error
        return copied

    @staticmethod
    def _same_registry(
        actual: dict[str, object],
        expected: dict[str, object],
    ) -> bool:
        return set(actual) == set(expected) and all(
            actual[name] is document for name, document in expected.items()
        )

    def _new_cycle(self) -> _PreviewCycle:
        if self._next_cycle_id > _MAX_SAFE_INTEGER:
            self._recovery_required = True
            raise PreviewError(
                "preview recovery required",
                recovery_required=True,
            )
        cycle = _PreviewCycle(
            cycle_id=self._next_cycle_id,
            authorities={},
            bindings={},
        )
        self._next_cycle_id += 1
        self._cycle = cycle
        self._owned = cycle.authorities
        self._bindings = []
        self._disabled.clear()
        return cycle

    def _active_cycle(self) -> _PreviewCycle:
        cycle = self._cycle
        if cycle is None:
            cycle = self._new_cycle()
        return cycle

    def _poison_cycle(
        self,
        *,
        binding: PreviewBinding | None = None,
        recovery_required: bool = False,
    ) -> None:
        cycle = self._cycle
        if cycle is not None:
            cycle.poisoned = True
            if recovery_required:
                cycle.recovery_required = True
        if binding is not None:
            self._disabled.add(id(binding))
        if recovery_required:
            self._recovery_required = True

    def _update_retirement_ready(self) -> bool:
        cycle = self._cycle
        if cycle is None:
            return True
        cycle.retirement_ready = (
            bool(cycle.authorities)
            and not cycle.recovery_required
            and not self._recovery_required
            and not cycle.bindings
            and not self._bindings
            and all(
                record.document_closed and record.checkout_closed and not record.ambiguous
                for record in cycle.authorities.values()
            )
        )
        return cycle.retirement_ready

    def _active_cycle_id(self) -> int | None:
        self._require_thread()
        return None if self._cycle is None else self._cycle.cycle_id

    def _binding_identity(
        self,
        checkout_id: object,
    ) -> tuple[int, str, int]:
        self._require_thread()
        canonical = _identifier(checkout_id, _CHECKOUT_ID)
        cycle = self._cycle
        record = self._owned.get(canonical)
        if cycle is None or record is None or record.binding is None:
            _invalid()
        return cycle.cycle_id, canonical, id(record.binding)

    def _validate_local_binding(
        self,
        checkout_id: object,
    ) -> PreviewBinding:
        return self._observe_local_binding(checkout_id)

    def _retired_cycle_ready(self, cycle_id: object) -> bool:
        self._require_thread()
        cycle = self._cycle
        return (
            type(cycle_id) is int
            and cycle is not None
            and cycle.cycle_id == cycle_id
            and self._update_retirement_ready()
        )

    def _draining_cycle_without_bindings(self, cycle_id: object) -> bool:
        self._require_thread()
        cycle = self._cycle
        return (
            type(cycle_id) is int
            and cycle is not None
            and cycle.cycle_id == cycle_id
            and cycle.draining
            and not cycle.bindings
            and not self._bindings
            and all(record.document_closed for record in cycle.authorities.values())
        )

    def _finalize_retired_cycle(self, cycle_id: object) -> None:
        self._require_thread()
        cycle = self._cycle
        if (
            type(cycle_id) is not int
            or cycle is None
            or cycle.cycle_id != cycle_id
            or not self._update_retirement_ready()
        ):
            raise PreviewError("preview cycle is not fully retired")
        self._cycle = None
        self._owned = {}
        self._bindings = []
        self._disabled.clear()
        self._recovery_required = False

    def adopt_checkout(
        self,
        acquired: object,
        *,
        source: object | None = None,
        open_key: object | None = None,
    ) -> _OwnedPreview:
        self._require_thread()
        if type(acquired) is not dict:
            self._poison_cycle(recovery_required=True)
            raise PreviewError(
                "preview recovery required",
                recovery_required=True,
            )
        raw_source = acquired.get("source") if source is None else source
        raw_open_key = acquired.get("open_key") if open_key is None else open_key
        try:
            canonical_source = _source(raw_source)
            canonical_open_key = _identifier(raw_open_key, _OPEN_KEY)
            if (
                acquired.get("source") != canonical_source
                or acquired.get("open_key") != canonical_open_key
            ):
                _invalid()
            checkout_id = _raw_checkout_identity(
                acquired.get("descriptor"),
                open_key=canonical_open_key,
            )
        except PreviewError as error:
            self._poison_cycle(recovery_required=True)
            raise PreviewError(
                "preview recovery required",
                primary_error=error,
                recovery_required=True,
            ) from error
        cycle = self._cycle
        cleanup_only = cycle is not None and self._update_retirement_ready()
        cycle = self._active_cycle()
        if (
            self._recovery_required
            or cycle.recovery_required
            or len(cycle.authorities) >= _MAX_CYCLE_AUTHORITIES
        ):
            self._poison_cycle(recovery_required=True)
            raise PreviewError(
                "preview recovery required",
                recovery_required=True,
                checkout_id=checkout_id,
                source=canonical_source,
                open_key=canonical_open_key,
            )
        if checkout_id in self._owned:
            raise PreviewError("checkout already owned")
        record = _OwnedPreview(
            checkout_id=checkout_id,
            source=canonical_source,
            open_key=canonical_open_key,
            cleanup_only=cleanup_only,
        )
        self._owned[checkout_id] = record
        cycle.retirement_ready = False
        return record

    def _validated(
        self,
        acquired: object,
    ) -> tuple[
        dict[str, object],
        str,
        dict[str, object],
        dict[str, object],
        str,
        _OwnedPreview,
    ]:
        record = self.adopt_checkout(acquired)
        outer = _mapping(
            acquired,
            frozenset(("source", "open_key", "descriptor", "claim")),
        )
        source = _source(outer["source"])
        open_key = _identifier(outer["open_key"], _OPEN_KEY)
        if source != record.source or open_key != record.open_key:
            _invalid()
        descriptor = _descriptor(
            outer["descriptor"],
            requested=source,
            open_key=open_key,
        )
        if descriptor["checkout_id"] != record.checkout_id:
            _invalid()
        record.descriptor = descriptor
        try:
            claim = _claim(
                outer["claim"],
                grant_id=_identifier(
                    _mapping(outer["claim"]).get("grant_id"),
                    _GRANT_ID,
                ),
                descriptor=descriptor,
            )
        except BaseException:
            record.document_closed = True
            raise
        return (
            source,
            open_key,
            descriptor,
            claim,
            str(claim["local_path"]),
            record,
        )

    def _ambiguous_document(
        self,
        record: _OwnedPreview,
        primary: BaseException,
    ) -> None:
        record.ambiguous = True
        record.document_closed = False
        self._poison_cycle(
            binding=record.binding,
            recovery_required=True,
        )
        raise PreviewError(
            "preview recovery required",
            primary_error=primary,
            recovery_required=True,
        ) from primary

    def _registered_document(
        self,
        before: dict[str, object],
        document: object | None,
        primary: BaseException,
        record: _OwnedPreview,
    ) -> tuple[str, object] | None:
        try:
            after = self._registry()
        except BaseException:
            self._ambiguous_document(record, primary)
        unchanged = len(after) >= len(before) and all(
            after.get(name) is candidate for name, candidate in before.items()
        )
        additions = [(name, candidate) for name, candidate in after.items() if name not in before]
        if not unchanged or len(additions) > 1:
            self._ambiguous_document(record, primary)
        if not additions:
            return None
        name, candidate = additions[0]
        if (
            any(candidate is existing for existing in before.values())
            or getattr(candidate, "Name", None) != name
            or (document is not None and candidate is not document)
        ):
            self._ambiguous_document(record, primary)
        return name, candidate

    def _retain_document(
        self,
        record: _OwnedPreview,
        name: str,
        document: object,
    ) -> None:
        record.document = document
        record.document_name = name
        record.document_closed = False
        record.ambiguous = False

    def _retryable_close_failure(
        self,
        record: _OwnedPreview,
        before: dict[str, object] | None,
    ) -> bool:
        if before is None or record.ambiguous:
            return False
        try:
            after = self._registry()
        except BaseException:
            return False
        return self._same_registry(after, before)

    def _rollback_document(
        self,
        primary: BaseException,
        record: _OwnedPreview,
    ) -> None:
        name = record.document_name
        document = record.document
        if type(name) is not str or document is None:
            self._ambiguous_document(record, primary)
        before: dict[str, object] | None = None
        try:
            before = self._registry()
            if before.get(name) is not document:
                record.ambiguous = True
                self._poison_cycle(
                    binding=record.binding,
                    recovery_required=True,
                )
                raise PreviewError("preview recovery required")
            self._freecad.closeDocument(name)
        except BaseException as cleanup:
            recovery_required = not self._retryable_close_failure(
                record,
                before,
            )
            if recovery_required:
                record.ambiguous = True
                self._poison_cycle(
                    binding=record.binding,
                    recovery_required=True,
                )
            elif self._cycle is not None:
                self._cycle.draining = True
            raise PreviewError(
                "preview recovery required",
                primary_error=primary,
                cleanup_error=cleanup,
                recovery_required=recovery_required,
            ) from cleanup
        assert before is not None
        try:
            after = self._registry()
            expected = dict(before)
            del expected[name]
            if not self._same_registry(after, expected):
                raise PreviewError("preview recovery required")
        except BaseException as cleanup:
            record.ambiguous = True
            self._poison_cycle(
                binding=record.binding,
                recovery_required=True,
            )
            raise PreviewError(
                "preview recovery required",
                primary_error=primary,
                cleanup_error=cleanup,
                recovery_required=True,
            ) from cleanup
        record.document = None
        record.document_name = None
        record.document_closed = True

    def open(self, acquired: object) -> PreviewBinding:
        self._require_thread()
        return self._open_validated(self._validated(acquired))

    def _open_validated(
        self,
        validated: tuple[
            dict[str, object],
            str,
            dict[str, object],
            dict[str, object],
            str,
            _OwnedPreview,
        ],
    ) -> PreviewBinding:
        source, open_key, descriptor, claim, local_path, record = validated
        if (
            descriptor["state"] != "open"
            or descriptor["dirty"] is not False
            or descriptor["source_liveness"] != "live"
        ):
            _invalid()
        if record.cleanup_only:
            cycle = self._cycle
            if cycle is not None:
                cycle.draining = True
            raise PreviewError(
                "preview cycle awaits host finalization",
                checkout_id=record.checkout_id,
                source=source,
                open_key=open_key,
                descriptor=descriptor,
            )
        if source["kind"] in self._active_cycle().bindings:
            self._poison_cycle()
            raise PreviewError("duplicate preview source authority")
        try:
            before = self._registry()
        except BaseException as error:
            record.ambiguous = True
            self._poison_cycle(recovery_required=True)
            if isinstance(error, PreviewError):
                raise
            raise PreviewError(
                "preview recovery required",
                primary_error=error,
                recovery_required=True,
            ) from error
        try:
            document = self._freecad.openDocument(local_path)
        except BaseException as primary:
            registered = self._registered_document(
                before,
                None,
                primary,
                record,
            )
            if registered is not None:
                self._ambiguous_document(record, primary)
            raise
        invalid = PreviewError()
        registered = self._registered_document(
            before,
            document,
            invalid,
            record,
        )
        if registered is None:
            raise invalid
        document_name, registered_document = registered
        self._retain_document(
            record,
            document_name,
            registered_document,
        )
        try:
            if (
                document is not registered_document
                or getattr(document, "Name", None) != document_name
                or _document_is_touched(document)
                or self._freecad.getDocument(document_name) is not document
                or any(
                    binding.document is document or binding.document_name == document_name
                    for binding in self._bindings
                )
            ):
                _invalid()
        except BaseException as primary:
            self._rollback_document(primary, record)
            raise
        frozen_descriptor = _freeze(descriptor)
        binding = PreviewBinding(
            source=_freeze(source),
            open_key=open_key,
            descriptor=frozen_descriptor,
            checkout=frozen_descriptor,
            claim=_freeze(claim),
            document=document,
            document_name=document_name,
        )
        record.binding = binding
        record.document = document
        record.document_name = document_name
        record.document_closed = False
        self._bindings.append(binding)
        self._active_cycle().bindings[str(source["kind"])] = binding
        return binding

    @staticmethod
    def _binding_material(
        binding: PreviewBinding,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        requested = _source(_thaw(binding.source))
        retained = _descriptor(
            _thaw(binding.descriptor),
            requested=requested,
            open_key=binding.open_key,
        )
        raw_claim = _mapping(_thaw(binding.claim), _CLAIM_KEYS)
        grant_id = _identifier(raw_claim["grant_id"], _GRANT_ID)
        claim = _claim(
            raw_claim,
            grant_id=grant_id,
            descriptor=retained,
        )
        return requested, retained, claim

    def _attest_claimed_file(
        self,
        local_path: str,
        expected_digest: str,
        expected_size: int,
    ) -> None:
        _identifier(expected_digest, _DIGEST)
        if not 0 <= expected_size <= _MAX_ATTESTED_FILE_BYTES:
            _invalid()
        if os.name == "nt":
            if _windows_files is None:
                _invalid()
            descriptor = -1
            try:
                path = Path(local_path)
                if not path.is_absolute() or Path(os.path.abspath(path)) != path:
                    _invalid()
                descriptor, capability = _windows_files.open_private_file(
                    path,
                    create=False,
                    read_write=False,
                )
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or before.st_size != expected_size
                ):
                    _invalid()
                digest = hashlib.sha256()
                offset = 0
                while offset < expected_size:
                    chunk = _windows_files.pread(
                        descriptor,
                        min(expected_size - offset, _ATTEST_READ_BYTES),
                        offset,
                    )
                    if not chunk:
                        _invalid()
                    digest.update(chunk)
                    offset += len(chunk)
                if _windows_files.pread(descriptor, 1, expected_size):
                    _invalid()
                current = _windows_files.capture_windows_fd(
                    descriptor,
                    directory=False,
                    generation_token=capability.generation_token,
                )
                after = os.fstat(descriptor)
                if (
                    current != capability
                    or _windows_files.validate_windows_path(capability, directory=False) != path
                    or (after.st_size, after.st_mtime_ns, after.st_nlink)
                    != (before.st_size, before.st_mtime_ns, before.st_nlink)
                    or not hmac.compare_digest(digest.hexdigest(), expected_digest)
                ):
                    _invalid()
            except PreviewError:
                raise
            except (TypeError, ValueError):
                _invalid()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            return
        flags = os.O_RDONLY
        for name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
            value = getattr(os, name, 0)
            if type(value) is int:
                flags |= value
        descriptor = os.open(local_path, flags)
        try:
            before = os.fstat(descriptor)
            current_euid = getattr(os, "geteuid", None)
            if (
                not stat.S_ISREG(before.st_mode)
                or not callable(current_euid)
                or before.st_uid != current_euid()
                or before.st_nlink != 1
                or before.st_size != expected_size
            ):
                _invalid()
            digest = hashlib.sha256()
            remaining = expected_size
            while remaining:
                chunk = os.read(
                    descriptor,
                    min(remaining, _ATTEST_READ_BYTES),
                )
                if not chunk:
                    _invalid()
                digest.update(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                _invalid()
            after = os.fstat(descriptor)
            before_identity = (
                before.st_mode,
                before.st_dev,
                before.st_ino,
                before.st_nlink,
                before.st_uid,
                before.st_gid,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            after_identity = (
                after.st_mode,
                after.st_dev,
                after.st_ino,
                after.st_nlink,
                after.st_uid,
                after.st_gid,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            observed_path = os.lstat(local_path)
            if (
                before_identity != after_identity
                or not stat.S_ISREG(observed_path.st_mode)
                or observed_path.st_dev != after.st_dev
                or observed_path.st_ino != after.st_ino
                or not hmac.compare_digest(
                    digest.hexdigest(),
                    expected_digest,
                )
            ):
                _invalid()
        finally:
            os.close(descriptor)

    def _observe_local_binding(
        self,
        checkout_id: object,
    ) -> PreviewBinding:
        self._require_thread()
        canonical = _identifier(checkout_id, _CHECKOUT_ID)
        record = self._owned.get(canonical)
        if record is None or record.binding is None:
            _invalid()
        binding = record.binding
        key = id(binding)
        if key in self._disabled:
            _invalid()
        try:
            binding_source, retained, claim = self._binding_material(binding)
            registry = self._registry()
            if (
                record.document_closed
                or record.checkout_closed
                or record.ambiguous
                or record.binding is not binding
                or record.document is not binding.document
                or record.document_name != binding.document_name
                or record.descriptor != retained
                or record.source != binding_source
                or record.open_key != binding.open_key
                or retained["checkout_id"] != canonical
                or retained["state"] != "open"
                or retained["dirty"] is not False
                or retained["source_liveness"] != "live"
                or not any(item is binding for item in self._bindings)
                or self._active_cycle().bindings.get(str(dict(binding.source)["kind"]))
                is not binding
                or registry.get(binding.document_name) is not binding.document
                or getattr(binding.document, "Name", None) != binding.document_name
                or getattr(binding.document, "FileName", None) != claim["local_path"]
                or _document_is_touched(binding.document)
            ):
                _invalid()
        except BaseException as error:
            self._poison_cycle(binding=binding)
            if isinstance(error, PreviewError):
                raise
            raise PreviewError(
                "invalid local preview observation",
                primary_error=error,
            ) from error
        return binding

    def attest_review_binding(
        self,
        checkout_id: object,
        descriptor: object,
    ) -> PreviewBinding:
        binding: PreviewBinding | None = None
        try:
            canonical = _identifier(checkout_id, _CHECKOUT_ID)
            record = self._owned.get(canonical)
            if record is None or record.binding is None:
                _invalid()
            binding = record.binding
            if self.validate_binding(canonical, descriptor) is not binding:
                _invalid()
            requested, retained, claim = self._binding_material(binding)
            fresh = _descriptor(
                _thaw(descriptor),
                requested=requested,
                open_key=binding.open_key,
            )
            expected_digest = _identifier(
                retained["current_model_sha256"],
                _DIGEST,
            )
            expected_size = _integer(retained["current_size_bytes"])
            if (
                fresh != retained
                or fresh["current_model_sha256"] != expected_digest
                or fresh["current_size_bytes"] != expected_size
                or claim["current_model_sha256"] != expected_digest
                or claim["current_size_bytes"] != expected_size
                or getattr(binding.document, "FileName", None) != claim["local_path"]
            ):
                _invalid()
            self._attest_claimed_file(
                str(claim["local_path"]),
                expected_digest,
                expected_size,
            )
        except BaseException as error:
            if binding is not None:
                self._poison_cycle(binding=binding)
            if isinstance(error, PreviewError):
                raise
            raise PreviewError(
                "invalid final local file observation",
                primary_error=error,
            ) from error
        return binding

    def validate_binding(
        self,
        checkout_id: object,
        descriptor: object,
    ) -> PreviewBinding:
        binding = self._observe_local_binding(checkout_id)
        try:
            current = _descriptor(
                _thaw(descriptor),
                requested=dict(binding.source),
                open_key=binding.open_key,
            )
            retained = _thaw(binding.descriptor)
            if (
                current != retained
                or current["state"] != "open"
                or current["dirty"] is not False
                or current["source_liveness"] != "live"
            ):
                _invalid()
        except BaseException:
            self._poison_cycle(binding=binding)
            raise
        return binding

    def poison_binding(self, checkout_id: object) -> None:
        self._require_thread()
        canonical = _identifier(checkout_id, _CHECKOUT_ID)
        record = self._owned.get(canonical)
        if record is None or record.binding is None:
            _invalid()
        self._poison_cycle(binding=record.binding)

    def review_eligible(
        self,
        binding: PreviewBinding,
        descriptor: object,
    ) -> bool:
        self._require_thread()
        key = id(binding)
        try:
            retained_checkout_id = dict(binding.descriptor)["checkout_id"]
            eligible = self.validate_binding(retained_checkout_id, descriptor) is binding
        except (PreviewError, TypeError, ValueError):
            eligible = False
        if not eligible:
            self._poison_cycle(binding=binding)
        cycle = self._cycle
        return eligible and key not in self._disabled and cycle is not None

    def aggregate_review_eligible(
        self,
        *,
        current_binding: PreviewBinding | None = None,
        current_descriptor: object | None = None,
        expected_project_id: object | None = None,
        expected_candidate_revision: object | None = None,
        expected_base_revision: object | None = None,
    ) -> bool:
        self._require_thread()
        cycle = self._cycle
        if (
            cycle is None
            or cycle.poisoned
            or cycle.recovery_required
            or cycle.draining
            or self._owned is not cycle.authorities
            or len(cycle.authorities) != 2
            or len(self._bindings) != 2
            or set(cycle.bindings) != {"head", "draft"}
            or len(cycle.bindings) != 2
        ):
            return False
        authority_bindings: list[PreviewBinding] = []
        authority_sources: set[str] = set()
        authority_projects: dict[str, str] = {}
        authority_resolved_sources: dict[str, dict[str, object]] = {}
        authority_heads: dict[str, dict[str, object]] = {}
        for checkout_id, record in cycle.authorities.items():
            binding = record.binding
            if (
                type(checkout_id) is not str
                or record.checkout_id != checkout_id
                or binding is None
                or record.document is None
                or record.document_name is None
                or record.document_closed
                or record.checkout_closed
                or record.ambiguous
            ):
                return False
            try:
                binding_source, binding_descriptor, _claim_value = self._binding_material(binding)
                source_kind = binding_source["kind"]
                resolved_source = _mapping(
                    binding_descriptor["source"],
                    _RESOLVED_SOURCE_KEYS,
                )
                resolved_project_id = _identifier(
                    resolved_source["project_id"],
                    _PROJECT_ID,
                )
                source_head = _source_head(binding_descriptor["source_head"])
            except (KeyError, PreviewError, TypeError, ValueError):
                return False
            if (
                type(source_kind) is not str
                or source_kind not in {"head", "draft"}
                or source_kind in authority_sources
                or record.source != binding_source
                or record.open_key != binding.open_key
                or record.descriptor != binding_descriptor
                or record.document is not binding.document
                or record.document_name != binding.document_name
                or cycle.bindings.get(source_kind) is not binding
                or sum(candidate is binding for candidate in self._bindings) != 1
            ):
                return False
            authority_sources.add(source_kind)
            authority_projects[source_kind] = resolved_project_id
            authority_resolved_sources[source_kind] = resolved_source
            authority_heads[source_kind] = source_head
            authority_bindings.append(binding)
        try:
            requested_head_project = _identifier(
                _mapping(_thaw(cycle.bindings["head"].source))["project_id"],
                _PROJECT_ID,
            )
            required_project = (
                None
                if expected_project_id is None
                else _identifier(expected_project_id, _PROJECT_ID)
            )
            required_candidate_revision = (
                None
                if expected_candidate_revision is None
                else _identifier(expected_candidate_revision, _REVISION_ID)
            )
            required_base_revision = (
                None
                if expected_base_revision is None
                else _identifier(expected_base_revision, _REVISION_ID)
            )
        except (KeyError, PreviewError, TypeError, ValueError):
            return False
        if (
            authority_sources != {"head", "draft"}
            or authority_projects.get("head") != authority_projects.get("draft")
            or authority_projects.get("head") != requested_head_project
            or (required_project is not None and authority_projects.get("head") != required_project)
            or authority_heads.get("head") != authority_heads.get("draft")
            or (required_candidate_revision is None) != (required_base_revision is None)
            or (
                required_candidate_revision is not None
                and authority_resolved_sources["draft"]["revision_id"]
                != required_candidate_revision
            )
            or (
                required_base_revision is not None
                and authority_heads["draft"]["revision_id"] != required_base_revision
            )
            or len({id(binding) for binding in authority_bindings}) != 2
            or any(
                sum(candidate is binding for candidate in authority_bindings) != 1
                for binding in self._bindings
            )
            or any(
                sum(candidate is binding for candidate in authority_bindings) != 1
                for binding in cycle.bindings.values()
            )
        ):
            return False
        for binding in tuple(self._bindings):
            descriptor = (
                current_descriptor if binding is current_binding else _thaw(binding.descriptor)
            )
            if not self.review_eligible(binding, descriptor):
                return False
        return True

    def refresh(
        self,
        binding: PreviewBinding,
        acquired: object,
    ) -> PreviewBinding:
        self._require_thread()
        if type(acquired) is not dict:
            _invalid()
        validated = self._validated(acquired)
        source, open_key, descriptor, _claim_value, _local_path, record = validated
        binding_source = _source(_thaw(binding.source))
        checkout_id = _identifier(
            _mapping(_thaw(binding.descriptor)).get("checkout_id"),
            _CHECKOUT_ID,
        )
        if source != binding_source or self.binding_for_checkout(checkout_id) is not binding:
            self._poison_cycle(binding=binding)
            cycle = self._cycle
            if cycle is not None:
                cycle.draining = True
            raise PreviewError(
                "preview source identity drift",
                checkout_id=record.checkout_id,
                source=source,
                open_key=open_key,
                descriptor=descriptor,
            )
        self._close_document(self._owned[checkout_id])
        return self._open_validated(validated)

    def binding_for_checkout(self, checkout_id: object) -> PreviewBinding:
        self._require_thread()
        canonical = _identifier(checkout_id, _CHECKOUT_ID)
        record = self._owned.get(canonical)
        if record is None or record.binding is None or record.document_closed:
            _invalid()
        return record.binding

    def discard_document(self, checkout_id: object) -> None:
        self._require_thread()
        canonical = _identifier(checkout_id, _CHECKOUT_ID)
        record = self._owned.get(canonical)
        if record is None or record.checkout_closed:
            _invalid()
        if self._cycle is not None:
            self._cycle.draining = True
        self._close_document(record)

    def _close_document(self, record: _OwnedPreview) -> None:
        if record.document_closed:
            return
        if record.ambiguous or record.document is None or type(record.document_name) is not str:
            self._poison_cycle(
                binding=record.binding,
                recovery_required=True,
            )
            raise PreviewError(
                "preview recovery required",
                recovery_required=True,
            )
        before: dict[str, object] | None = None
        try:
            before = self._registry()
            if before.get(record.document_name) is not record.document:
                record.ambiguous = True
                self._poison_cycle(
                    binding=record.binding,
                    recovery_required=True,
                )
                raise PreviewError("preview recovery required")
            self._freecad.closeDocument(record.document_name)
        except BaseException as error:
            recovery_required = not self._retryable_close_failure(
                record,
                before,
            )
            if recovery_required:
                record.ambiguous = True
                self._poison_cycle(
                    binding=record.binding,
                    recovery_required=True,
                )
            raise PreviewError(
                "preview recovery required",
                cleanup_error=error,
                recovery_required=recovery_required,
            ) from error
        assert before is not None
        try:
            after = self._registry()
            expected = dict(before)
            del expected[record.document_name]
            if not self._same_registry(after, expected):
                raise PreviewError("preview recovery required")
        except BaseException as error:
            record.ambiguous = True
            self._poison_cycle(
                binding=record.binding,
                recovery_required=True,
            )
            raise PreviewError(
                "preview recovery required",
                cleanup_error=error,
                recovery_required=True,
            ) from error
        record.document_closed = True
        if record.binding is not None:
            self._bindings = [
                binding for binding in self._bindings if binding is not record.binding
            ]
            cycle = self._cycle
            source_kind = str(dict(record.binding.source)["kind"])
            if cycle is not None and cycle.bindings.get(source_kind) is record.binding:
                del cycle.bindings[source_kind]

    def close_documents(self) -> None:
        self._require_thread()
        if self._cycle is not None:
            self._cycle.draining = True
        for record in self._owned.values():
            self._close_document(record)

    def ready_checkout_ids(self) -> tuple[str, ...]:
        self._require_thread()
        return tuple(
            record.checkout_id
            for record in self._owned.values()
            if record.document_closed and not record.checkout_closed and not record.ambiguous
        )

    def mark_checkout_closed(
        self,
        checkout_id: object,
        descriptor: object,
    ) -> None:
        self._require_thread()
        canonical = _identifier(checkout_id, _CHECKOUT_ID)
        record = self._owned.get(canonical)
        if (
            record is None
            or not record.document_closed
            or record.checkout_closed
            or record.ambiguous
        ):
            raise PreviewError("invalid checkout cleanup")
        try:
            _validate_closed_descriptor(
                descriptor,
                checkout_id=canonical,
                source=record.source,
                open_key=record.open_key,
                descriptor=record.descriptor,
            )
        except BaseException:
            self._poison_cycle(
                binding=record.binding,
                recovery_required=True,
            )
            raise
        record.checkout_closed = True
        self._update_retirement_ready()

    def cleanup_complete(self) -> bool:
        self._require_thread()
        if self._cycle is None:
            return not self._recovery_required
        return self._update_retirement_ready()

    def close_all(
        self,
        *,
        close_checkout: object,
        close_client: object,
    ) -> None:
        self._require_thread()
        if not callable(close_checkout) or not callable(close_client):
            raise TypeError("preview cleanup callbacks are required")
        self.close_documents()
        for checkout_id in self.ready_checkout_ids():
            try:
                descriptor = close_checkout(checkout_id)
                self.mark_checkout_closed(checkout_id, descriptor)
            except BaseException as error:
                raise PreviewError(
                    "preview recovery required",
                    cleanup_error=error,
                    recovery_required=True,
                    checkout_id=checkout_id,
                ) from error
        if self.cleanup_complete() and not self._client_closed:
            close_client()
            self._client_closed = True
