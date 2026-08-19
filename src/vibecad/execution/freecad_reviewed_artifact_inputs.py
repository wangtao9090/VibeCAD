"""Private run-scoped artifact authority for reviewed FreeCAD families.

This module defines metadata and opaque capabilities only.  It does not know
how artifacts enter the application, where they are stored, or how a Worker
receives them.  A trusted host must first create one immutable catalog
snapshot and supply run-owned payload and staging capabilities.  Reviewed
families can then resolve only an exact catalog entry for an already-selected
static family route.

No path, native object name, store key, or caller-provided callable crosses
this boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from vibecad.intent_bridge.contracts import DocumentRef, IntentBridgeError
from vibecad.intent_bridge.ports import ArtifactReader

MAX_REVIEWED_ARTIFACTS = 64
MAX_REVIEWED_ARTIFACT_OPERATIONS = 32
MAX_REVIEWED_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_REVIEWED_ARTIFACT_TOTAL_BYTES = 64 * 1024 * 1024

_CATALOG_SCHEMA_VERSION = 1
_CATALOG_DIGEST_DOMAIN = b"vibecad-reviewed-artifact-catalog-v1\0"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_MEDIA_TYPE = re.compile(r"[a-z][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_IDENTIFIER_BYTES = 128
_MAX_MEDIA_TYPE_BYTES = 128


class ReviewedArtifactInputErrorCode(StrEnum):
    """Stable private failures from the reviewed artifact authority."""

    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    UNKNOWN_ARTIFACT = "unknown_artifact"
    AUTHORITY_VIOLATION = "authority_violation"
    INTEGRITY_FAILURE = "integrity_failure"
    CLOSED = "closed"
    CLEANUP_FAILED = "cleanup_failed"


class ReviewedArtifactInputError(ValueError):
    """Bounded path-free failure from the private artifact seam."""

    __slots__ = ("code",)

    def __init__(self, code: ReviewedArtifactInputErrorCode) -> None:
        if type(code) is not ReviewedArtifactInputErrorCode:
            raise TypeError("code must be a ReviewedArtifactInputErrorCode")
        self.code = code
        super().__init__(code.value)


def _fail(code: ReviewedArtifactInputErrorCode) -> None:
    raise ReviewedArtifactInputError(code)


def _text(value: object, pattern: re.Pattern[str], maximum: int) -> str:
    if type(value) is not str:
        _fail(ReviewedArtifactInputErrorCode.INVALID_INPUT)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        _fail(ReviewedArtifactInputErrorCode.INVALID_INPUT)
    if not encoded or len(encoded) > maximum or pattern.fullmatch(value) is None:
        _fail(ReviewedArtifactInputErrorCode.INVALID_INPUT)
    return value


def _identifier(value: object) -> str:
    return _text(value, _IDENTIFIER, _MAX_IDENTIFIER_BYTES)


def _digest(value: object) -> str:
    return _text(value, _DIGEST, 64)


def _media_type(value: object) -> str:
    return _text(value, _MEDIA_TYPE, _MAX_MEDIA_TYPE_BYTES)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _fail(ReviewedArtifactInputErrorCode.INVALID_INPUT)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedArtifactCatalogRecord:
    """One sealed input and its complete statically allowed use."""

    artifact_id: str
    content_sha256: str
    size_bytes: int
    media_type: str
    role_term_ref_id: str
    schema_term_ref_id: str
    document_id: str
    family_id: str
    operation_ids: tuple[str, ...]
    maximum_bytes: int

    def __post_init__(self) -> None:
        for name in (
            "artifact_id",
            "role_term_ref_id",
            "schema_term_ref_id",
            "document_id",
            "family_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name)))
        object.__setattr__(self, "content_sha256", _digest(self.content_sha256))
        object.__setattr__(self, "media_type", _media_type(self.media_type))
        if (
            type(self.size_bytes) is not int
            or type(self.maximum_bytes) is not int
            or not 1 <= self.size_bytes <= self.maximum_bytes <= MAX_REVIEWED_ARTIFACT_BYTES
            or type(self.operation_ids) is not tuple
            or not 1 <= len(self.operation_ids) <= MAX_REVIEWED_ARTIFACT_OPERATIONS
        ):
            code = (
                ReviewedArtifactInputErrorCode.BUDGET_EXCEEDED
                if type(self.size_bytes) is int
                and type(self.maximum_bytes) is int
                and (
                    self.size_bytes > MAX_REVIEWED_ARTIFACT_BYTES
                    or self.maximum_bytes > MAX_REVIEWED_ARTIFACT_BYTES
                )
                else ReviewedArtifactInputErrorCode.INVALID_INPUT
            )
            _fail(code)
        operations = tuple(_identifier(item) for item in self.operation_ids)
        if len(set(operations)) != len(operations):
            _fail(ReviewedArtifactInputErrorCode.INVALID_INPUT)
        object.__setattr__(self, "operation_ids", tuple(sorted(operations)))
        try:
            DocumentRef(
                artifact_id=self.artifact_id,
                role_term_ref_id=self.role_term_ref_id,
                schema_term_ref_id=self.schema_term_ref_id,
                document_id=self.document_id,
                document_digest=self.content_sha256,
                content_sha256=self.content_sha256,
                size_bytes=self.size_bytes,
                media_type=self.media_type,
            )
        except IntentBridgeError:
            _fail(ReviewedArtifactInputErrorCode.INVALID_INPUT)

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "content_sha256": self.content_sha256,
            "document_id": self.document_id,
            "family_id": self.family_id,
            "maximum_bytes": self.maximum_bytes,
            "media_type": self.media_type,
            "operation_ids": list(self.operation_ids),
            "role_term_ref_id": self.role_term_ref_id,
            "schema_term_ref_id": self.schema_term_ref_id,
            "size_bytes": self.size_bytes,
        }


def _catalog_mapping(
    *,
    task_id: str,
    project_id: str,
    base_revision: str,
    run_id: str,
    records: tuple[ReviewedArtifactCatalogRecord, ...],
) -> dict[str, object]:
    return {
        "base_revision": base_revision,
        "project_id": project_id,
        "records": [item.to_mapping() for item in records],
        "run_id": run_id,
        "schema_version": _CATALOG_SCHEMA_VERSION,
        "task_id": task_id,
    }


def _record_copy(record: ReviewedArtifactCatalogRecord) -> ReviewedArtifactCatalogRecord:
    try:
        return ReviewedArtifactCatalogRecord(
            artifact_id=record.artifact_id,
            content_sha256=record.content_sha256,
            size_bytes=record.size_bytes,
            media_type=record.media_type,
            role_term_ref_id=record.role_term_ref_id,
            schema_term_ref_id=record.schema_term_ref_id,
            document_id=record.document_id,
            family_id=record.family_id,
            operation_ids=record.operation_ids,
            maximum_bytes=record.maximum_bytes,
        )
    except ReviewedArtifactInputError:
        _fail(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)
    except BaseException:
        _fail(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)


def _catalog_digest(
    *,
    task_id: str,
    project_id: str,
    base_revision: str,
    run_id: str,
    records: tuple[ReviewedArtifactCatalogRecord, ...],
) -> tuple[bytes, str]:
    raw = _canonical_json(
        _catalog_mapping(
            task_id=task_id,
            project_id=project_id,
            base_revision=base_revision,
            run_id=run_id,
            records=records,
        )
    )
    return raw, hashlib.sha256(_CATALOG_DIGEST_DOMAIN + raw).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedArtifactCatalogSnapshot:
    """Canonical immutable metadata captured for one exact program run."""

    task_id: str
    project_id: str
    base_revision: str
    run_id: str
    records: tuple[ReviewedArtifactCatalogRecord, ...]
    catalog_sha256: str = field(init=False)
    canonical_bytes: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in ("task_id", "project_id", "base_revision", "run_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name)))
        if (
            type(self.records) is not tuple
            or len(self.records) > MAX_REVIEWED_ARTIFACTS
            or any(type(item) is not ReviewedArtifactCatalogRecord for item in self.records)
        ):
            code = (
                ReviewedArtifactInputErrorCode.BUDGET_EXCEEDED
                if type(self.records) is tuple and len(self.records) > MAX_REVIEWED_ARTIFACTS
                else ReviewedArtifactInputErrorCode.INVALID_INPUT
            )
            _fail(code)
        records = tuple(
            sorted((_record_copy(item) for item in self.records), key=lambda item: item.artifact_id)
        )
        artifact_ids = tuple(item.artifact_id for item in records)
        if len(set(artifact_ids)) != len(artifact_ids):
            _fail(ReviewedArtifactInputErrorCode.INVALID_INPUT)
        if sum(item.size_bytes for item in records) > MAX_REVIEWED_ARTIFACT_TOTAL_BYTES:
            _fail(ReviewedArtifactInputErrorCode.BUDGET_EXCEEDED)
        object.__setattr__(self, "records", records)
        raw, digest = _catalog_digest(
            task_id=self.task_id,
            project_id=self.project_id,
            base_revision=self.base_revision,
            run_id=self.run_id,
            records=records,
        )
        object.__setattr__(self, "canonical_bytes", raw)
        object.__setattr__(self, "catalog_sha256", digest)

    def to_mapping(self) -> dict[str, object]:
        result = _catalog_mapping(
            task_id=self.task_id,
            project_id=self.project_id,
            base_revision=self.base_revision,
            run_id=self.run_id,
            records=self.records,
        )
        result["catalog_sha256"] = self.catalog_sha256
        return result


@runtime_checkable
class ReviewedArtifactPayloadSource(Protocol):
    """Run-owned host capability for reading one catalog record."""

    def read(self, record: ReviewedArtifactCatalogRecord, maximum_bytes: int) -> bytes: ...

    def close(self) -> None: ...


@runtime_checkable
class ReviewedArtifactStagerFactory(Protocol):
    """Run-owned host capability for creating one family-native stager."""

    def create(
        self,
        *,
        record: ReviewedArtifactCatalogRecord,
        family_id: str,
        operation_id: str,
    ) -> object: ...

    def close(self) -> None: ...


@runtime_checkable
class ReviewedArtifactStagerHandle(Protocol):
    """Exact route-bound handle exposed to a family integration adapter."""

    def create(self) -> object: ...


class _OpaqueCapability:
    __slots__ = ()

    def __copy__(self):
        raise TypeError("reviewed artifact capabilities cannot be copied")

    def __deepcopy__(self, memo: object):
        del memo
        raise TypeError("reviewed artifact capabilities cannot be copied")

    def __reduce__(self):
        raise TypeError("reviewed artifact capabilities cannot be serialized")

    def __reduce_ex__(self, protocol: object):
        del protocol
        raise TypeError("reviewed artifact capabilities cannot be serialized")


class _ExactArtifactReader(_OpaqueCapability):
    __slots__ = ("_document", "_record", "_resolver")

    def __init__(
        self,
        resolver: _ReviewedArtifactRunResolver,
        record: ReviewedArtifactCatalogRecord,
        document: DocumentRef,
    ) -> None:
        self._resolver = resolver
        self._record = record
        self._document = document

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        resolver = self._resolver
        resolver._require_live()
        resolver._require_snapshot_authentic()
        record = self._record
        if (
            type(document) is not DocumentRef
            or document != self._document
            or type(maximum_bytes) is not int
            or maximum_bytes != record.maximum_bytes
            or resolver._record(record.artifact_id) != record
        ):
            _fail(ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION)
        try:
            payload = resolver._source.read(record, maximum_bytes)
        except ReviewedArtifactInputError:
            raise
        except BaseException:
            _fail(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)
        if (
            type(payload) is not bytes
            or len(payload) != record.size_bytes
            or len(payload) > maximum_bytes
            or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), record.content_sha256)
        ):
            _fail(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)
        return payload


class _ExactStagerHandle(_OpaqueCapability):
    __slots__ = ("_family_id", "_operation_id", "_record", "_resolver")

    def __init__(
        self,
        resolver: _ReviewedArtifactRunResolver,
        record: ReviewedArtifactCatalogRecord,
        family_id: str,
        operation_id: str,
    ) -> None:
        self._resolver = resolver
        self._record = record
        self._family_id = family_id
        self._operation_id = operation_id

    def create(self) -> object:
        resolver = self._resolver
        resolver._require_live()
        resolver._require_snapshot_authentic()
        record = self._record
        if (
            resolver._record(record.artifact_id) != record
            or record.family_id != self._family_id
            or self._operation_id not in record.operation_ids
        ):
            _fail(ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION)
        try:
            stager = resolver._stagers.create(
                record=record,
                family_id=self._family_id,
                operation_id=self._operation_id,
            )
        except ReviewedArtifactInputError:
            raise
        except BaseException:
            _fail(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)
        if stager is None:
            _fail(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)
        return stager


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class ReviewedArtifactContext(_OpaqueCapability):
    """One exact document reader plus one exact family stager handle."""

    artifact_document: DocumentRef
    artifacts: ArtifactReader
    stager_factory: ReviewedArtifactStagerHandle

    def __post_init__(self) -> None:
        if (
            type(self.artifact_document) is not DocumentRef
            or not isinstance(self.artifacts, ArtifactReader)
            or not isinstance(self.stager_factory, ReviewedArtifactStagerHandle)
        ):
            _fail(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class ReviewedArtifactResolution(_OpaqueCapability):
    """Dispatcher-facing result; the common field name is ``artifact_context``."""

    artifact_context: ReviewedArtifactContext

    def __post_init__(self) -> None:
        if type(self.artifact_context) is not ReviewedArtifactContext:
            _fail(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)


class _ReviewedArtifactRunResolver(_OpaqueCapability):
    """Resolve immutable task inputs only within one exact program run."""

    __slots__ = (
        "_canonical_bytes",
        "_catalog_sha256",
        "_closed",
        "_run_token",
        "_snapshot",
        "_source",
        "_stagers",
    )

    def __init__(
        self,
        *,
        snapshot: ReviewedArtifactCatalogSnapshot,
        source: ReviewedArtifactPayloadSource,
        stager_factory: ReviewedArtifactStagerFactory,
        task_id: str,
        project_id: str,
        base_revision: str,
        run_id: str,
        run_token: object,
    ) -> None:
        if (
            type(snapshot) is not ReviewedArtifactCatalogSnapshot
            or not isinstance(source, ReviewedArtifactPayloadSource)
            or not isinstance(stager_factory, ReviewedArtifactStagerFactory)
            or run_token is None
        ):
            _fail(ReviewedArtifactInputErrorCode.INVALID_INPUT)
        binding = tuple(_identifier(item) for item in (task_id, project_id, base_revision, run_id))
        if binding != (
            snapshot.task_id,
            snapshot.project_id,
            snapshot.base_revision,
            snapshot.run_id,
        ):
            _fail(ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION)
        self._snapshot = snapshot
        self._source = source
        self._stagers = stager_factory
        self._run_token = run_token
        self._closed = False
        self._require_snapshot_authentic()
        self._canonical_bytes = snapshot.canonical_bytes
        self._catalog_sha256 = snapshot.catalog_sha256

    @property
    def catalog_sha256(self) -> str:
        self._require_live()
        self._require_snapshot_authentic()
        return self._snapshot.catalog_sha256

    def _require_live(self) -> None:
        if self._closed:
            _fail(ReviewedArtifactInputErrorCode.CLOSED)

    def _require_snapshot_authentic(self) -> None:
        snapshot = self._snapshot
        try:
            raw, digest = _catalog_digest(
                task_id=snapshot.task_id,
                project_id=snapshot.project_id,
                base_revision=snapshot.base_revision,
                run_id=snapshot.run_id,
                records=snapshot.records,
            )
        except ReviewedArtifactInputError:
            raise
        except BaseException:
            _fail(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)
        if (
            type(snapshot.canonical_bytes) is not bytes
            or not hmac.compare_digest(raw, snapshot.canonical_bytes)
            or type(snapshot.catalog_sha256) is not str
            or not hmac.compare_digest(digest, snapshot.catalog_sha256)
            or (
                hasattr(self, "_canonical_bytes")
                and not hmac.compare_digest(raw, self._canonical_bytes)
            )
            or (
                hasattr(self, "_catalog_sha256")
                and not hmac.compare_digest(digest, self._catalog_sha256)
            )
        ):
            _fail(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)

    def _record(self, artifact_id: str) -> ReviewedArtifactCatalogRecord | None:
        return next(
            (item for item in self._snapshot.records if item.artifact_id == artifact_id),
            None,
        )

    def resolve(
        self,
        *,
        run_token: object,
        family_id: str,
        operation_id: str,
        artifact_id: str,
        content_sha256: str,
        role_term_ref_id: str,
        schema_term_ref_id: str,
        media_type: str,
        maximum_bytes: int,
    ) -> ReviewedArtifactResolution:
        """Return capabilities for one exact catalog entry and static route."""

        self._require_live()
        self._require_snapshot_authentic()
        if run_token is not self._run_token:
            _fail(ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION)
        family = _identifier(family_id)
        operation = _identifier(operation_id)
        selected_id = _identifier(artifact_id)
        digest = _digest(content_sha256)
        role = _identifier(role_term_ref_id)
        schema = _identifier(schema_term_ref_id)
        media = _media_type(media_type)
        if type(maximum_bytes) is not int:
            _fail(ReviewedArtifactInputErrorCode.INVALID_INPUT)
        record = self._record(selected_id)
        if record is None:
            _fail(ReviewedArtifactInputErrorCode.UNKNOWN_ARTIFACT)
        if (
            not hmac.compare_digest(record.content_sha256, digest)
            or record.role_term_ref_id != role
            or record.schema_term_ref_id != schema
            or record.media_type != media
            or record.maximum_bytes != maximum_bytes
            or record.family_id != family
            or operation not in record.operation_ids
        ):
            _fail(ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION)
        document = DocumentRef(
            artifact_id=record.artifact_id,
            role_term_ref_id=record.role_term_ref_id,
            schema_term_ref_id=record.schema_term_ref_id,
            document_id=record.document_id,
            document_digest=record.content_sha256,
            content_sha256=record.content_sha256,
            size_bytes=record.size_bytes,
            media_type=record.media_type,
        )
        context = ReviewedArtifactContext(
            artifact_document=document,
            artifacts=_ExactArtifactReader(self, record, document),
            stager_factory=_ExactStagerHandle(self, record, family, operation),
        )
        return ReviewedArtifactResolution(artifact_context=context)

    def resolve_unique(
        self,
        *,
        run_token: object,
        family_id: str,
        operation_id: str,
        role_term_ref_id: str,
        schema_term_ref_id: str,
        media_type: str,
        maximum_bytes: int,
    ) -> ReviewedArtifactResolution:
        """Resolve one uniquely matching sealed route input.

        Some reviewed inputs are primary evidence rather than references named
        by the model-visible intent graph.  For those inputs, trusted host code
        selects only the complete static family/operation/document contract and
        this resolver requires the catalog to contain exactly one match.  It
        never falls back to a path, label, object name, or insertion order.
        """

        self._require_live()
        self._require_snapshot_authentic()
        if run_token is not self._run_token:
            _fail(ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION)
        family = _identifier(family_id)
        operation = _identifier(operation_id)
        role = _identifier(role_term_ref_id)
        schema = _identifier(schema_term_ref_id)
        media = _media_type(media_type)
        if type(maximum_bytes) is not int:
            _fail(ReviewedArtifactInputErrorCode.INVALID_INPUT)
        matches = tuple(
            record
            for record in self._snapshot.records
            if record.family_id == family
            and operation in record.operation_ids
            and record.role_term_ref_id == role
            and record.schema_term_ref_id == schema
            and record.media_type == media
            and record.maximum_bytes == maximum_bytes
        )
        if not matches:
            _fail(ReviewedArtifactInputErrorCode.UNKNOWN_ARTIFACT)
        if len(matches) != 1:
            _fail(ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION)
        record = matches[0]
        document = DocumentRef(
            artifact_id=record.artifact_id,
            role_term_ref_id=record.role_term_ref_id,
            schema_term_ref_id=record.schema_term_ref_id,
            document_id=record.document_id,
            document_digest=record.content_sha256,
            content_sha256=record.content_sha256,
            size_bytes=record.size_bytes,
            media_type=record.media_type,
        )
        return ReviewedArtifactResolution(
            artifact_context=ReviewedArtifactContext(
                artifact_document=document,
                artifacts=_ExactArtifactReader(self, record, document),
                stager_factory=_ExactStagerHandle(self, record, family, operation),
            )
        )

    def close(self) -> None:
        """Close all owned capabilities exactly once, remaining closed on error."""

        if self._closed:
            return
        self._closed = True
        failures = 0
        closed: set[int] = set()
        for capability in (self._stagers, self._source):
            identity = id(capability)
            if identity in closed:
                continue
            closed.add(identity)
            try:
                capability.close()
            except BaseException:
                failures += 1
        if failures:
            _fail(ReviewedArtifactInputErrorCode.CLEANUP_FAILED)


__all__ = (
    "MAX_REVIEWED_ARTIFACTS",
    "MAX_REVIEWED_ARTIFACT_BYTES",
    "MAX_REVIEWED_ARTIFACT_OPERATIONS",
    "MAX_REVIEWED_ARTIFACT_TOTAL_BYTES",
    "ReviewedArtifactCatalogRecord",
    "ReviewedArtifactCatalogSnapshot",
    "ReviewedArtifactContext",
    "ReviewedArtifactInputError",
    "ReviewedArtifactInputErrorCode",
    "ReviewedArtifactPayloadSource",
    "ReviewedArtifactResolution",
    "ReviewedArtifactStagerFactory",
    "ReviewedArtifactStagerHandle",
    "_ReviewedArtifactRunResolver",
)
