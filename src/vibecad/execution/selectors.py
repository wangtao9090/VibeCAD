"""Strict, FreeCAD-independent Level-A entity selector contracts.

The helpers in this module read only VibeCAD-owned identity metadata and the
actual ``TypeId`` of duck-typed document objects.  They never inspect or fall
back to ``Name``, ``Label``, object order, or sub-element indices.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

from vibecad.workflow.errors import (
    SCHEMA_VERSION,
    is_canonical_json_pointer,
    join_json_pointer,
)

_PROJECT_RE = re.compile(r"^project_[0-9a-f]{32}$")
_REVISION_RE = re.compile(r"^revision_[0-9a-f]{32}$")
_OBJECT_RE = re.compile(r"^object_[0-9a-f]{32}$")
_FEATURE_RE = re.compile(r"^feature_[0-9a-f]{32}$")
_OBJECT_TYPE_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*(?:::[A-Za-z][A-Za-z0-9_]*)+$"
)
_MAX_OPERATION_ID_BYTES = 256
_MAX_OBJECT_TYPE_BYTES = 128
_MAX_ERROR_PATH_LENGTH = 256
_MAX_PROVENANCE_BYTES = 1024
_MISSING = object()


class EntityKind(StrEnum):
    """Closed selector entity kinds supported by Level A."""

    OBJECT = "object"
    FEATURE = "feature"


class SemanticRole(StrEnum):
    """Immutable semantic classification stored on managed objects."""

    PART = "part"
    PRIMITIVE = "primitive"
    FEATURE = "feature"
    SUPPORT = "support"


class ProvenanceSource(StrEnum):
    """Closed origin of one managed object identity."""

    USER = "user"
    MODEL = "model"
    SYSTEM = "system"
    IMPORTED = "imported"


class SelectorErrorCode(StrEnum):
    """Stable, non-reflective selector rejection reasons."""

    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    UNSUPPORTED_VERSION = "unsupported_version"
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"
    INVALID_ERROR_RECORD = "invalid_error_record"
    WRONG_PROJECT = "wrong_project"
    STALE_REVISION = "stale_revision"
    MALFORMED_IDENTITY = "malformed_identity"
    DUPLICATE_IDENTITY = "duplicate_identity"
    ZERO_MATCH = "zero_match"
    MULTIPLE_MATCHES = "multiple_matches"
    INVALID_INPUT = "invalid_input"


_ERROR_MESSAGES = {
    SelectorErrorCode.MISSING_FIELD: "A required selector field is missing.",
    SelectorErrorCode.UNKNOWN_FIELD: "The selector field is not supported.",
    SelectorErrorCode.UNSUPPORTED_VERSION: "The selector schema version is unsupported.",
    SelectorErrorCode.INVALID_TYPE: "The selector value has an invalid type.",
    SelectorErrorCode.INVALID_VALUE: "The selector value is invalid.",
    SelectorErrorCode.INVALID_ERROR_RECORD: "The selector error record is invalid.",
    SelectorErrorCode.WRONG_PROJECT: "The selector belongs to a different project.",
    SelectorErrorCode.STALE_REVISION: "The selector is stale for this revision.",
    SelectorErrorCode.MALFORMED_IDENTITY: "Managed entity identity metadata is malformed.",
    SelectorErrorCode.DUPLICATE_IDENTITY: "Managed entity identity metadata is duplicated.",
    SelectorErrorCode.ZERO_MATCH: "The selector did not resolve to an entity.",
    SelectorErrorCode.MULTIPLE_MATCHES: "The selector resolved to multiple entities.",
    SelectorErrorCode.INVALID_INPUT: "The selector resolver input is invalid.",
}


class SelectorError(ValueError):
    """Fixed bounded error envelope that never reflects rejected data."""

    __slots__ = ("schema_version", "code", "path", "message")

    def __init__(self, code: SelectorErrorCode, path: str = "") -> None:
        if type(code) is not SelectorErrorCode:
            raise TypeError("code must be a SelectorErrorCode")
        if (
            type(path) is not str
            or len(path) > _MAX_ERROR_PATH_LENGTH
            or not is_canonical_json_pointer(path)
        ):
            raise ValueError("path must be a bounded canonical JSON Pointer")
        self.schema_version = SCHEMA_VERSION
        self.code = code
        self.path = path
        self.message = _ERROR_MESSAGES[code]
        self.args = (self.message,)

    def to_mapping(self) -> dict[str, int | str]:
        return {
            "schema_version": self.schema_version,
            "code": self.code.value,
            "path": self.path,
            "message": self.message,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        try:
            if type(value) is not dict or set(value) != {
                "schema_version",
                "code",
                "path",
                "message",
            }:
                raise ValueError
            schema_version = value["schema_version"]
            raw_code = value["code"]
            path = value["path"]
            message = value["message"]
            if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
                raise ValueError
            if type(raw_code) is not str:
                raise ValueError
            code = SelectorErrorCode(raw_code)
            if (
                type(path) is not str
                or len(path) > _MAX_ERROR_PATH_LENGTH
                or not is_canonical_json_pointer(path)
            ):
                raise ValueError
            if type(message) is not str or message != _ERROR_MESSAGES[code]:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise cls(SelectorErrorCode.INVALID_ERROR_RECORD) from None
        return cls(code, path)


def _raise(code: SelectorErrorCode, path: str = "") -> None:
    raise SelectorError(code, path)


def _fields(
    value: object,
    *,
    allowed: frozenset[str],
    path: str = "",
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _raise(SelectorErrorCode.INVALID_TYPE, path)
    try:
        keys = tuple(value)
    except Exception:
        _raise(SelectorErrorCode.INVALID_TYPE, path)
    if not all(type(key) is str for key in keys):
        _raise(SelectorErrorCode.INVALID_TYPE, path)
    if len(keys) != len(set(keys)):
        _raise(SelectorErrorCode.INVALID_VALUE, path)
    unknown = sorted(set(keys) - allowed)
    if unknown:
        unknown_path = _safe_field_path(path, unknown[0])
        _raise(SelectorErrorCode.UNKNOWN_FIELD, unknown_path)
    missing = sorted(allowed - set(keys))
    if missing:
        missing_path = _safe_field_path(path, missing[0])
        _raise(SelectorErrorCode.MISSING_FIELD, missing_path)
    try:
        return {key: value[key] for key in keys}
    except Exception:
        _raise(SelectorErrorCode.INVALID_TYPE, path)


def _safe_field_path(parent: str, name: str) -> str:
    if (
        len(name) > 128
        or not name.isprintable()
        or len(name.splitlines()) != 1
    ):
        name = "__unknown__"
    result = join_json_pointer(parent, name)
    return result if len(result) <= _MAX_ERROR_PATH_LENGTH else parent


def _schema(value: object, path: str = "/schema_version") -> int:
    if type(value) is not int:
        _raise(SelectorErrorCode.INVALID_TYPE, path)
    if value != SCHEMA_VERSION:
        _raise(SelectorErrorCode.UNSUPPORTED_VERSION, path)
    return value


def _enum[EnumT: StrEnum](value: object, enum_type: type[EnumT], path: str) -> EnumT:
    if type(value) is enum_type:
        return value
    if type(value) is not str:
        _raise(SelectorErrorCode.INVALID_TYPE, path)
    try:
        return enum_type(value)
    except ValueError:
        _raise(SelectorErrorCode.INVALID_VALUE, path)


def _identifier(value: object, pattern: re.Pattern[str], path: str) -> str:
    if type(value) is not str:
        _raise(SelectorErrorCode.INVALID_TYPE, path)
    if pattern.fullmatch(value) is None:
        _raise(SelectorErrorCode.INVALID_VALUE, path)
    return value


def _bounded_operation_id(value: object, path: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        _raise(SelectorErrorCode.INVALID_TYPE, path)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        _raise(SelectorErrorCode.INVALID_VALUE, path)
    if (
        not value.strip()
        or not value.isprintable()
        or len(value.splitlines()) != 1
        or len(encoded) > _MAX_OPERATION_ID_BYTES
    ):
        _raise(SelectorErrorCode.INVALID_VALUE, path)
    return value


def _object_type(value: object, path: str = "/object_type") -> str:
    if type(value) is not str:
        _raise(SelectorErrorCode.INVALID_TYPE, path)
    try:
        encoded = value.encode("ascii")
    except UnicodeError:
        _raise(SelectorErrorCode.INVALID_VALUE, path)
    if len(encoded) > _MAX_OBJECT_TYPE_BYTES or _OBJECT_TYPE_RE.fullmatch(value) is None:
        _raise(SelectorErrorCode.INVALID_VALUE, path)
    return value


class _FrozenValue:
    __slots__ = ()

    def __copy__(self):
        return self

    def __deepcopy__(self, _memo):
        return self


@dataclass(frozen=True, slots=True, kw_only=True)
class Provenance(_FrozenValue):
    """Canonical engine-owned creation provenance."""

    source: ProvenanceSource
    operation_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source",
            _enum(self.source, ProvenanceSource, "/source"),
        )
        object.__setattr__(
            self,
            "operation_id",
            _bounded_operation_id(self.operation_id, "/operation_id"),
        )

    def to_mapping(self) -> dict[str, str | None]:
        return {"source": self.source.value, "operation_id": self.operation_id}

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            allowed=frozenset({"source", "operation_id"}),
        )
        return cls(source=data["source"], operation_id=data["operation_id"])


@dataclass(frozen=True, slots=True, kw_only=True)
class EntityIdentity(_FrozenValue):
    """Strict identity metadata read from one managed DocumentObject."""

    object_id: str
    feature_id: str | None
    object_type: str
    semantic_role: SemanticRole
    provenance: Provenance

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "object_id",
            _identifier(self.object_id, _OBJECT_RE, "/object_id"),
        )
        if self.feature_id is not None:
            object.__setattr__(
                self,
                "feature_id",
                _identifier(self.feature_id, _FEATURE_RE, "/feature_id"),
            )
        object.__setattr__(self, "object_type", _object_type(self.object_type))
        object.__setattr__(
            self,
            "semantic_role",
            _enum(self.semantic_role, SemanticRole, "/semantic_role"),
        )
        if type(self.provenance) is not Provenance:
            _raise(SelectorErrorCode.INVALID_TYPE, "/provenance")
        object.__setattr__(
            self,
            "provenance",
            Provenance.from_mapping(self.provenance.to_mapping()),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "object_id": self.object_id,
            "feature_id": self.feature_id,
            "object_type": self.object_type,
            "semantic_role": self.semantic_role.value,
            "provenance": self.provenance.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            allowed=frozenset(
                {"object_id", "feature_id", "object_type", "semantic_role", "provenance"}
            ),
        )
        try:
            provenance = Provenance.from_mapping(data["provenance"])
        except SelectorError as error:
            raise SelectorError(error.code, f"/provenance{error.path}") from None
        return cls(
            object_id=data["object_id"],
            feature_id=data["feature_id"],
            object_type=data["object_type"],
            semantic_role=data["semantic_role"],
            provenance=provenance,
        )

    def to_selector(
        self,
        *,
        project_id: str,
        revision_id: str,
        entity_kind: EntityKind,
    ) -> SelectorV1:
        checked_kind = _enum(entity_kind, EntityKind, "/entity_kind")
        if checked_kind is EntityKind.FEATURE and self.feature_id is None:
            _raise(SelectorErrorCode.INVALID_VALUE, "/feature_id")
        return SelectorV1(
            project_id=project_id,
            revision_id=revision_id,
            entity_kind=checked_kind,
            object_id=self.object_id,
            feature_id=self.feature_id if checked_kind is EntityKind.FEATURE else None,
            object_type=self.object_type,
            semantic_role=self.semantic_role,
            provenance=self.provenance,
            expected_cardinality=1,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectorV1(_FrozenValue):
    """Revision-bound persistent Level-A selector."""

    project_id: str
    revision_id: str
    entity_kind: EntityKind
    object_id: str
    feature_id: str | None
    object_type: str
    semantic_role: SemanticRole
    provenance: Provenance
    expected_cardinality: int = 1
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(
            self,
            "project_id",
            _identifier(self.project_id, _PROJECT_RE, "/project_id"),
        )
        object.__setattr__(
            self,
            "revision_id",
            _identifier(self.revision_id, _REVISION_RE, "/revision_id"),
        )
        kind = _enum(self.entity_kind, EntityKind, "/entity_kind")
        object.__setattr__(self, "entity_kind", kind)
        object.__setattr__(
            self,
            "object_id",
            _identifier(self.object_id, _OBJECT_RE, "/object_id"),
        )
        feature_id = self.feature_id
        if kind is EntityKind.OBJECT:
            if feature_id is not None:
                _raise(SelectorErrorCode.INVALID_VALUE, "/feature_id")
        else:
            if feature_id is None:
                _raise(SelectorErrorCode.INVALID_VALUE, "/feature_id")
            object.__setattr__(
                self,
                "feature_id",
                _identifier(feature_id, _FEATURE_RE, "/feature_id"),
            )
        object.__setattr__(self, "object_type", _object_type(self.object_type))
        object.__setattr__(
            self,
            "semantic_role",
            _enum(self.semantic_role, SemanticRole, "/semantic_role"),
        )
        if type(self.provenance) is not Provenance:
            _raise(SelectorErrorCode.INVALID_TYPE, "/provenance")
        object.__setattr__(
            self,
            "provenance",
            Provenance.from_mapping(self.provenance.to_mapping()),
        )
        if type(self.expected_cardinality) is not int:
            _raise(SelectorErrorCode.INVALID_TYPE, "/expected_cardinality")
        if self.expected_cardinality != 1:
            _raise(SelectorErrorCode.INVALID_VALUE, "/expected_cardinality")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "revision_id": self.revision_id,
            "entity_kind": self.entity_kind.value,
            "object_id": self.object_id,
            "feature_id": self.feature_id,
            "object_type": self.object_type,
            "semantic_role": self.semantic_role.value,
            "provenance": self.provenance.to_mapping(),
            "expected_cardinality": self.expected_cardinality,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            allowed=frozenset(
                {
                    "schema_version",
                    "project_id",
                    "revision_id",
                    "entity_kind",
                    "object_id",
                    "feature_id",
                    "object_type",
                    "semantic_role",
                    "provenance",
                    "expected_cardinality",
                }
            ),
        )
        try:
            provenance = Provenance.from_mapping(data["provenance"])
        except SelectorError as error:
            raise SelectorError(error.code, f"/provenance{error.path}") from None
        return cls(
            schema_version=data["schema_version"],
            project_id=data["project_id"],
            revision_id=data["revision_id"],
            entity_kind=data["entity_kind"],
            object_id=data["object_id"],
            feature_id=data["feature_id"],
            object_type=data["object_type"],
            semantic_role=data["semantic_role"],
            provenance=provenance,
            expected_cardinality=data["expected_cardinality"],
        )


def encode_provenance_metadata(provenance: Provenance) -> str:
    """Encode the sole canonical App::PropertyString provenance form."""

    if type(provenance) is not Provenance:
        _raise(SelectorErrorCode.INVALID_TYPE, "/provenance")
    checked = Provenance.from_mapping(provenance.to_mapping())
    return json.dumps(
        checked.to_mapping(),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_object(pairs: list[tuple[object, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError
        result[key] = value
    return result


def _parse_provenance_metadata(value: object) -> Provenance:
    if type(value) is not str:
        _raise(SelectorErrorCode.INVALID_TYPE, "/provenance")
    try:
        raw = value.encode("utf-8")
    except UnicodeError:
        _raise(SelectorErrorCode.INVALID_VALUE, "/provenance")
    if not raw or len(raw) > _MAX_PROVENANCE_BYTES:
        _raise(SelectorErrorCode.INVALID_VALUE, "/provenance")
    try:
        mapping = json.loads(value, object_pairs_hook=_json_object)
    except (TypeError, ValueError, json.JSONDecodeError):
        _raise(SelectorErrorCode.INVALID_VALUE, "/provenance")
    provenance = Provenance.from_mapping(mapping)
    if value != encode_provenance_metadata(provenance):
        _raise(SelectorErrorCode.INVALID_VALUE, "/provenance")
    return provenance


def parse_entity_identity(value: object) -> EntityIdentity:
    """Parse untrusted duck-typed VibeCAD metadata without name fallback."""

    try:
        object_id = value.VibeCADObjectId  # type: ignore[attr-defined]
        feature_id = value.VibeCADFeatureId  # type: ignore[attr-defined]
        semantic_role = value.VibeCADSemanticRole  # type: ignore[attr-defined]
        provenance_raw = value.VibeCADProvenance  # type: ignore[attr-defined]
        object_type = value.TypeId  # type: ignore[attr-defined]
    except Exception:
        raise SelectorError(SelectorErrorCode.MALFORMED_IDENTITY) from None
    if feature_id is None or (type(feature_id) is str and feature_id == ""):
        feature_id = None
    try:
        provenance = _parse_provenance_metadata(provenance_raw)
        return EntityIdentity(
            object_id=object_id,
            feature_id=feature_id,
            object_type=object_type,
            semantic_role=semantic_role,
            provenance=provenance,
        )
    except SelectorError as error:
        raise SelectorError(SelectorErrorCode.MALFORMED_IDENTITY, error.path) from None


@dataclass(frozen=True, slots=True)
class _IndexedEntity:
    value: object
    identity: EntityIdentity


def _index_records(
    objects: object,
) -> tuple[tuple[_IndexedEntity, ...], frozenset[str], frozenset[str]]:
    try:
        iterator = iter(objects)  # type: ignore[arg-type]
    except Exception:
        raise SelectorError(SelectorErrorCode.INVALID_INPUT, "/objects") from None
    records: list[_IndexedEntity] = []
    object_ids: set[str] = set()
    feature_ids: set[str] = set()
    duplicate_objects: set[str] = set()
    duplicate_features: set[str] = set()
    while True:
        try:
            item = next(iterator)
        except StopIteration:
            break
        except Exception:
            raise SelectorError(SelectorErrorCode.INVALID_INPUT, "/objects") from None
        identity = parse_entity_identity(item)
        if identity.object_id in object_ids:
            duplicate_objects.add(identity.object_id)
        object_ids.add(identity.object_id)
        if identity.feature_id is not None:
            if identity.feature_id in feature_ids:
                duplicate_features.add(identity.feature_id)
            feature_ids.add(identity.feature_id)
        records.append(_IndexedEntity(item, identity))
    return (
        tuple(records),
        frozenset(duplicate_objects),
        frozenset(duplicate_features),
    )


def index_entity_identities(objects: Iterable[object]) -> tuple[EntityIdentity, ...]:
    """Parse a complete identity inventory and reject any duplicate ID."""

    records, duplicate_objects, duplicate_features = _index_records(objects)
    if duplicate_objects or duplicate_features:
        _raise(SelectorErrorCode.DUPLICATE_IDENTITY, "/objects")
    return tuple(record.identity for record in records)


def _mismatch_path(selector: SelectorV1, identity: EntityIdentity) -> str:
    if identity.object_id != selector.object_id:
        return "/object_id"
    if selector.entity_kind is EntityKind.FEATURE and identity.feature_id != selector.feature_id:
        return "/feature_id"
    if identity.object_type != selector.object_type:
        return "/object_type"
    if identity.semantic_role is not selector.semantic_role:
        return "/semantic_role"
    if identity.provenance != selector.provenance:
        return "/provenance"
    return ""


def resolve_selector(
    selector: SelectorV1,
    objects: object,
    *,
    project_id: str,
    revision_id: str,
) -> object:
    """Resolve exactly one entity after checking authority before traversal."""

    if type(selector) is not SelectorV1:
        _raise(SelectorErrorCode.INVALID_TYPE, "/selector")
    try:
        checked = SelectorV1.from_mapping(selector.to_mapping())
    except SelectorError:
        _raise(SelectorErrorCode.INVALID_VALUE, "/selector")
    active_project = _identifier(project_id, _PROJECT_RE, "/project_id")
    active_revision = _identifier(revision_id, _REVISION_RE, "/revision_id")
    if checked.project_id != active_project:
        _raise(SelectorErrorCode.WRONG_PROJECT, "/project_id")
    if checked.revision_id != active_revision:
        _raise(SelectorErrorCode.STALE_REVISION, "/revision_id")

    records, duplicate_objects, duplicate_features = _index_records(objects)
    matches = tuple(
        record for record in records if _mismatch_path(checked, record.identity) == ""
    )
    if len(matches) > checked.expected_cardinality:
        _raise(SelectorErrorCode.MULTIPLE_MATCHES, "/expected_cardinality")
    if duplicate_objects or duplicate_features:
        _raise(SelectorErrorCode.DUPLICATE_IDENTITY, "/objects")
    if not matches:
        mismatch = _mismatch_path(checked, records[0].identity) if len(records) == 1 else ""
        _raise(SelectorErrorCode.ZERO_MATCH, mismatch)
    if len(matches) != checked.expected_cardinality:
        _raise(SelectorErrorCode.ZERO_MATCH, "/expected_cardinality")
    return matches[0].value


__all__ = (
    "EntityIdentity",
    "EntityKind",
    "Provenance",
    "ProvenanceSource",
    "SelectorError",
    "SelectorErrorCode",
    "SelectorV1",
    "SemanticRole",
    "encode_provenance_metadata",
    "index_entity_identities",
    "parse_entity_identity",
    "resolve_selector",
)
