"""Content-addressed contracts for evidence-to-intent compilation.

These values form a backend-neutral, authority-free seam.  Documents and
subjects are described by content-bound ontology terms rather than a closed
union of graph or backend types.  Unknown terms remain serializable but inert;
only separately injected trusted codecs, compiler rules, and backend adapters
may interpret them.

No value in this module grants execution authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self

INTENT_BRIDGE_SCHEMA_VERSION = 1
MAX_BRIDGE_ENVELOPE_BYTES = 512 * 1024
MAX_BRIDGE_TERMS = 512
MAX_BRIDGE_DOCUMENTS = 32
MAX_PROOF_ASSERTIONS = 1_024
MAX_SUBJECTS_PER_ASSERTION = 32
MAX_TOTAL_PROOF_SUBJECTS = 8_192
MAX_PARENTS_PER_ASSERTION = 64
MAX_TOTAL_PROOF_PARENTS = 8_192
MAX_COMPILE_INPUTS = 32
MAX_COMPILE_OUTPUTS = 16
MAX_DIAGNOSTICS = 256
MAX_DIAGNOSTIC_SUBJECTS = 16
MAX_TOTAL_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_SUBJECT_LOOKUPS = 8_192
MAX_RULE_APPLICATIONS = 4_096

_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_IDENTIFIER_BYTES = 128
_MAX_TERM_BYTES = 256
_MAX_VERSION_BYTES = 64
_MAX_MEDIA_TYPE_BYTES = 128
_MAX_ERROR_PATH_BYTES = 384
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 65_536
_MAX_JSON_STRING_BYTES = 64 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]*\Z")
_MEDIA_TYPE = re.compile(r"[a-z][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*\Z")

_PROOF_DIGEST_DOMAIN = b"vibecad.intent-bridge.proof-bundle.v1\0"
_COMPILE_REQUEST_DIGEST_DOMAIN = b"vibecad.intent-bridge.compile-request.v1\0"
_LOWERING_REQUEST_DIGEST_DOMAIN = b"vibecad.intent-bridge.lowering-request.v1\0"


class IntentBridgeErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_VERSION = "unsupported_version"
    BUDGET_EXCEEDED = "budget_exceeded"
    UNKNOWN_REFERENCE = "unknown_reference"
    INTEGRITY_FAILURE = "integrity_failure"
    AUTHORITY_VIOLATION = "authority_violation"


class IntentBridgeError(ValueError):
    """Bounded stable failure from an intent-bridge contract."""

    def __init__(self, code: IntentBridgeErrorCode, path: str = "/") -> None:
        if type(code) is not IntentBridgeErrorCode:
            raise TypeError("code must be an IntentBridgeErrorCode")
        try:
            path_size = len(path.encode("utf-8")) if type(path) is str else 0
        except UnicodeError:
            path_size = _MAX_ERROR_PATH_BYTES + 1
        if (
            type(path) is not str
            or not path.startswith("/")
            or not path.isprintable()
            or len(path.splitlines()) != 1
            or path_size > _MAX_ERROR_PATH_BYTES
        ):
            path = "/"
        self.code = code
        self.path = path
        super().__init__(f"intent bridge error ({code.value}) at {path}")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": INTENT_BRIDGE_SCHEMA_VERSION,
            "code": self.code.value,
            "path": self.path,
        }


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


def _bounded_text(
    value: object,
    path: str,
    *,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if type(value) is not str:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    if (
        not value
        or value != value.strip()
        or not value.isprintable()
        or len(value.splitlines()) != 1
        or len(encoded) > maximum
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    return value


def _identifier(value: object, path: str) -> str:
    return _bounded_text(value, path, maximum=_MAX_IDENTIFIER_BYTES, pattern=_IDENTIFIER)


def _term(value: object, path: str) -> str:
    return _bounded_text(value, path, maximum=_MAX_TERM_BYTES)


def _version(value: object, path: str) -> str:
    return _bounded_text(value, path, maximum=_MAX_VERSION_BYTES, pattern=_VERSION)


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    return value


def _positive_integer(value: object, path: str, *, maximum: int = _MAX_SAFE_INTEGER) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    return value


def _ordinal(value: object, path: str, *, maximum: int) -> int:
    if type(value) is not int or not 0 <= value < maximum:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    return value


def _enum_value[EnumT: StrEnum](value: object, enum_type: type[EnumT], path: str) -> EnumT:
    if type(value) is not str:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    try:
        return enum_type(value)
    except ValueError:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)


def _strict_fields(
    value: object,
    *,
    required: set[str],
    path: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != required:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    if any(type(key) is not str for key in value):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    return value


def _wire_list(value: object, path: str, *, maximum: int) -> list[object]:
    if type(value) is not list:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    if len(value) > maximum:
        _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, path)
    return value


def _exact_tuple[ItemT](
    value: object,
    item_type: type[ItemT],
    path: str,
    *,
    maximum: int,
    minimum: int = 0,
) -> tuple[ItemT, ...]:
    if type(value) is not tuple or any(type(item) is not item_type for item in value):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    if len(value) > maximum:
        _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, path)
    if len(value) < minimum:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    return value


def _identifier_tuple(
    value: object,
    path: str,
    *,
    maximum: int,
    minimum: int = 0,
    ordered: bool = False,
) -> tuple[str, ...]:
    if type(value) is not tuple:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    if len(value) > maximum:
        _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, path)
    if len(value) < minimum:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    result = tuple(_identifier(item, path) for item in value)
    if len(set(result)) != len(result):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    return result if ordered else tuple(sorted(result))


def _validate_json(value: object) -> None:
    nodes = 0
    active: set[int] = set()

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/")
        if depth > _MAX_JSON_DEPTH:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/")
        if item is None or type(item) is bool:
            return
        if type(item) is int:
            if abs(item) > _MAX_SAFE_INTEGER:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")
            return
        if type(item) is float:
            if not math.isfinite(item):
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")
            return
        if type(item) is str:
            try:
                size = len(item.encode("utf-8"))
            except UnicodeError:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")
            if size > _MAX_JSON_STRING_BYTES:
                _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/")
            return
        if type(item) not in {list, dict}:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")
        identity = id(item)
        if identity in active:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")
        active.add(identity)
        try:
            if type(item) is list:
                for child in item:
                    visit(child, depth + 1)
            else:
                for key, child in item.items():
                    if type(key) is not str:
                        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")
                    visit(key, depth + 1)
                    visit(child, depth + 1)
        finally:
            active.remove(identity)

    visit(value, 0)


def _canonical_json(value: object, *, maximum: int = MAX_BRIDGE_ENVELOPE_BYTES) -> bytes:
    _validate_json(value)
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")
    if len(raw) > maximum:
        _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/")
    return raw


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _decode_json(raw: object) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_BRIDGE_ENVELOPE_BYTES:
        code = (
            IntentBridgeErrorCode.BUDGET_EXCEEDED
            if type(raw) is bytes and len(raw) > MAX_BRIDGE_ENVELOPE_BYTES
            else IntentBridgeErrorCode.INVALID_INPUT
        )
        _fail(code, "/")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKeyError:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/")
    except (UnicodeError, ValueError, RecursionError):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")
    _validate_json(value)
    if type(value) is not dict:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")
    if not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class BridgeTermRef:
    term_ref_id: str
    namespace: str
    vocabulary_version: str
    term_id: str
    term_definition_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "term_ref_id", _identifier(self.term_ref_id, "/term_ref_id"))
        object.__setattr__(self, "namespace", _identifier(self.namespace, "/namespace"))
        object.__setattr__(
            self,
            "vocabulary_version",
            _version(self.vocabulary_version, "/vocabulary_version"),
        )
        object.__setattr__(self, "term_id", _term(self.term_id, "/term_id"))
        object.__setattr__(
            self,
            "term_definition_sha256",
            _digest(self.term_definition_sha256, "/term_definition_sha256"),
        )

    @property
    def semantic_identity(self) -> tuple[str, str, str, str]:
        return (
            self.namespace,
            self.vocabulary_version,
            self.term_id,
            self.term_definition_sha256,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "term_ref_id": self.term_ref_id,
            "namespace": self.namespace,
            "vocabulary_version": self.vocabulary_version,
            "term_id": self.term_id,
            "term_definition_sha256": self.term_definition_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "/term") -> Self:
        fields = _strict_fields(
            value,
            required={
                "term_ref_id",
                "namespace",
                "vocabulary_version",
                "term_id",
                "term_definition_sha256",
            },
            path=path,
        )
        return cls(**fields)


@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentRef:
    artifact_id: str
    role_term_ref_id: str
    schema_term_ref_id: str
    document_id: str
    document_digest: str
    content_sha256: str
    size_bytes: int
    media_type: str

    def __post_init__(self) -> None:
        for name in ("artifact_id", "role_term_ref_id", "schema_term_ref_id", "document_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        object.__setattr__(
            self, "document_digest", _digest(self.document_digest, "/document_digest")
        )
        object.__setattr__(self, "content_sha256", _digest(self.content_sha256, "/content_sha256"))
        object.__setattr__(self, "size_bytes", _positive_integer(self.size_bytes, "/size_bytes"))
        object.__setattr__(
            self,
            "media_type",
            _bounded_text(
                self.media_type,
                "/media_type",
                maximum=_MAX_MEDIA_TYPE_BYTES,
                pattern=_MEDIA_TYPE,
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "role_term_ref_id": self.role_term_ref_id,
            "schema_term_ref_id": self.schema_term_ref_id,
            "document_id": self.document_id,
            "document_digest": self.document_digest,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "/document") -> Self:
        keys = {
            "artifact_id",
            "role_term_ref_id",
            "schema_term_ref_id",
            "document_id",
            "document_digest",
            "content_sha256",
            "size_bytes",
            "media_type",
        }
        return cls(**_strict_fields(value, required=keys, path=path))


@dataclass(frozen=True, slots=True, kw_only=True)
class SubjectRef:
    artifact_id: str
    selector_kind_term_ref_id: str
    selector_id: str

    def __post_init__(self) -> None:
        for name in ("artifact_id", "selector_kind_term_ref_id", "selector_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "selector_kind_term_ref_id": self.selector_kind_term_ref_id,
            "selector_id": self.selector_id,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "/subject") -> Self:
        keys = {"artifact_id", "selector_kind_term_ref_id", "selector_id"}
        return cls(**_strict_fields(value, required=keys, path=path))


@dataclass(frozen=True, slots=True, kw_only=True)
class ProofEndpoint:
    ordinal: int
    role_term_ref_id: str
    subject: SubjectRef

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "ordinal",
            _ordinal(self.ordinal, "/ordinal", maximum=MAX_SUBJECTS_PER_ASSERTION),
        )
        object.__setattr__(
            self,
            "role_term_ref_id",
            _identifier(self.role_term_ref_id, "/role_term_ref_id"),
        )
        if type(self.subject) is not SubjectRef:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/subject")

    def to_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "role_term_ref_id": self.role_term_ref_id,
            "subject": self.subject.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "/endpoint") -> Self:
        fields = _strict_fields(
            value,
            required={"ordinal", "role_term_ref_id", "subject"},
            path=path,
        )
        return cls(
            ordinal=fields["ordinal"],
            role_term_ref_id=fields["role_term_ref_id"],
            subject=SubjectRef.from_mapping(fields["subject"], f"{path}/subject"),
        )


def _endpoint_tuple(value: object, path: str) -> tuple[ProofEndpoint, ...]:
    endpoints = _exact_tuple(
        value,
        ProofEndpoint,
        path,
        maximum=MAX_SUBJECTS_PER_ASSERTION,
        minimum=1,
    )
    endpoints = tuple(sorted(endpoints, key=lambda item: item.ordinal))
    if tuple(item.ordinal for item in endpoints) != tuple(range(len(endpoints))):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    subjects = tuple(
        (
            item.role_term_ref_id,
            item.subject.artifact_id,
            item.subject.selector_kind_term_ref_id,
            item.subject.selector_id,
        )
        for item in endpoints
    )
    if len(set(subjects)) != len(subjects):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    return endpoints


@dataclass(frozen=True, slots=True, kw_only=True)
class ProofAssertion:
    assertion_id: str
    predicate_term_ref_id: str
    rule_term_ref_id: str
    premises: tuple[ProofEndpoint, ...]
    conclusions: tuple[ProofEndpoint, ...]
    parent_assertion_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("assertion_id", "predicate_term_ref_id", "rule_term_ref_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        object.__setattr__(self, "premises", _endpoint_tuple(self.premises, "/premises"))
        object.__setattr__(self, "conclusions", _endpoint_tuple(self.conclusions, "/conclusions"))
        if len(self.premises) + len(self.conclusions) > MAX_SUBJECTS_PER_ASSERTION:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/endpoints")
        object.__setattr__(
            self,
            "parent_assertion_ids",
            _identifier_tuple(
                self.parent_assertion_ids,
                "/parent_assertion_ids",
                maximum=MAX_PARENTS_PER_ASSERTION,
            ),
        )
        if self.assertion_id in self.parent_assertion_ids:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/parent_assertion_ids")

    def to_mapping(self) -> dict[str, object]:
        return {
            "assertion_id": self.assertion_id,
            "predicate_term_ref_id": self.predicate_term_ref_id,
            "rule_term_ref_id": self.rule_term_ref_id,
            "premises": [item.to_mapping() for item in self.premises],
            "conclusions": [item.to_mapping() for item in self.conclusions],
            "parent_assertion_ids": list(self.parent_assertion_ids),
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "/assertion") -> Self:
        keys = {
            "assertion_id",
            "predicate_term_ref_id",
            "rule_term_ref_id",
            "premises",
            "conclusions",
            "parent_assertion_ids",
        }
        fields = _strict_fields(value, required=keys, path=path)
        raw_premises = _wire_list(
            fields["premises"], f"{path}/premises", maximum=MAX_SUBJECTS_PER_ASSERTION
        )
        raw_conclusions = _wire_list(
            fields["conclusions"], f"{path}/conclusions", maximum=MAX_SUBJECTS_PER_ASSERTION
        )
        raw_parents = _wire_list(
            fields["parent_assertion_ids"],
            f"{path}/parent_assertion_ids",
            maximum=MAX_PARENTS_PER_ASSERTION,
        )
        return cls(
            assertion_id=fields["assertion_id"],
            predicate_term_ref_id=fields["predicate_term_ref_id"],
            rule_term_ref_id=fields["rule_term_ref_id"],
            premises=tuple(
                ProofEndpoint.from_mapping(item, f"{path}/premises/{index}")
                for index, item in enumerate(raw_premises)
            ),
            conclusions=tuple(
                ProofEndpoint.from_mapping(item, f"{path}/conclusions/{index}")
                for index, item in enumerate(raw_conclusions)
            ),
            parent_assertion_ids=tuple(raw_parents),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProducerDescriptor:
    producer_id: str
    producer_version: str
    producer_contract_sha256: str
    rule_catalog_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "producer_id", _identifier(self.producer_id, "/producer_id"))
        object.__setattr__(
            self,
            "producer_version",
            _version(self.producer_version, "/producer_version"),
        )
        object.__setattr__(
            self,
            "producer_contract_sha256",
            _digest(self.producer_contract_sha256, "/producer_contract_sha256"),
        )
        object.__setattr__(
            self,
            "rule_catalog_sha256",
            _digest(self.rule_catalog_sha256, "/rule_catalog_sha256"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "producer_contract_sha256": self.producer_contract_sha256,
            "rule_catalog_sha256": self.rule_catalog_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "/producer") -> Self:
        keys = {
            "producer_id",
            "producer_version",
            "producer_contract_sha256",
            "rule_catalog_sha256",
        }
        return cls(**_strict_fields(value, required=keys, path=path))


@dataclass(frozen=True, slots=True, kw_only=True)
class ProducerBinding:
    descriptor: ProducerDescriptor
    request_sha256: str

    def __post_init__(self) -> None:
        if type(self.descriptor) is not ProducerDescriptor:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/descriptor")
        object.__setattr__(self, "request_sha256", _digest(self.request_sha256, "/request_sha256"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "descriptor": self.descriptor.to_mapping(),
            "request_sha256": self.request_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "/producer_binding") -> Self:
        fields = _strict_fields(
            value,
            required={"descriptor", "request_sha256"},
            path=path,
        )
        return cls(
            descriptor=ProducerDescriptor.from_mapping(fields["descriptor"], f"{path}/descriptor"),
            request_sha256=fields["request_sha256"],
        )


class ProofAuthority(StrEnum):
    EVIDENCE_ONLY = "evidence_only"


def _term_table(terms: object, path: str = "/terms") -> tuple[BridgeTermRef, ...]:
    checked = _exact_tuple(
        terms,
        BridgeTermRef,
        path,
        maximum=MAX_BRIDGE_TERMS,
        minimum=1,
    )
    result = tuple(sorted(checked, key=lambda item: item.term_ref_id))
    if len({item.term_ref_id for item in result}) != len(result):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    identities = tuple(item.semantic_identity[:3] for item in result)
    if len(set(identities)) != len(identities):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    return result


def _document_table(documents: object, path: str = "/documents") -> tuple[DocumentRef, ...]:
    checked = _exact_tuple(
        documents,
        DocumentRef,
        path,
        maximum=MAX_BRIDGE_DOCUMENTS,
        minimum=1,
    )
    result = tuple(sorted(checked, key=lambda item: item.artifact_id))
    if len({item.artifact_id for item in result}) != len(result):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    identities = tuple((item.schema_term_ref_id, item.document_id) for item in result)
    if len(set(identities)) != len(identities):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    return result


def _validate_term_refs(term_ids: set[str], referenced: set[str], path: str) -> None:
    if not referenced <= term_ids:
        _fail(IntentBridgeErrorCode.UNKNOWN_REFERENCE, path)


def _validate_proof_dag(assertions: tuple[ProofAssertion, ...]) -> None:
    ids = {item.assertion_id for item in assertions}
    parents = {item.assertion_id: item.parent_assertion_ids for item in assertions}
    if any(not set(items) <= ids for items in parents.values()):
        _fail(IntentBridgeErrorCode.UNKNOWN_REFERENCE, "/assertions/parent_assertion_ids")
    children: dict[str, list[str]] = {item: [] for item in ids}
    indegree = {item: 0 for item in ids}
    for assertion_id, parent_ids in parents.items():
        indegree[assertion_id] = len(parent_ids)
        for parent_id in parent_ids:
            children[parent_id].append(assertion_id)
    ready = sorted(item for item, count in indegree.items() if count == 0)
    visited = 0
    while ready:
        assertion_id = ready.pop()
        visited += 1
        for child_id in children[assertion_id]:
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                ready.append(child_id)
    if visited != len(ids):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/assertions/parent_assertion_ids")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProofBundle:
    terms: tuple[BridgeTermRef, ...]
    documents: tuple[DocumentRef, ...]
    assertions: tuple[ProofAssertion, ...]
    producer: ProducerBinding
    authority: ProofAuthority = ProofAuthority.EVIDENCE_ONLY
    schema_version: int = INTENT_BRIDGE_SCHEMA_VERSION
    bundle_id: str = field(init=False)
    bundle_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != INTENT_BRIDGE_SCHEMA_VERSION
        ):
            _fail(IntentBridgeErrorCode.UNSUPPORTED_VERSION, "/schema_version")
        if (
            type(self.authority) is not ProofAuthority
            or self.authority is not ProofAuthority.EVIDENCE_ONLY
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/authority")
        if type(self.producer) is not ProducerBinding:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/producer")
        terms = _term_table(self.terms)
        documents = _document_table(self.documents)
        assertions = _exact_tuple(
            self.assertions,
            ProofAssertion,
            "/assertions",
            maximum=MAX_PROOF_ASSERTIONS,
            minimum=1,
        )
        assertions = tuple(sorted(assertions, key=lambda item: item.assertion_id))
        if len({item.assertion_id for item in assertions}) != len(assertions):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/assertions")
        total_subjects = sum(len(item.premises) + len(item.conclusions) for item in assertions)
        if total_subjects > MAX_TOTAL_PROOF_SUBJECTS:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/assertions")
        if sum(len(item.parent_assertion_ids) for item in assertions) > MAX_TOTAL_PROOF_PARENTS:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/assertions")
        term_ids = {item.term_ref_id for item in terms}
        referenced_terms = {
            term_id
            for item in documents
            for term_id in (item.role_term_ref_id, item.schema_term_ref_id)
        }
        referenced_terms.update(
            term_id
            for item in assertions
            for term_id in (item.predicate_term_ref_id, item.rule_term_ref_id)
        )
        referenced_terms.update(
            term_id
            for item in assertions
            for endpoint in (*item.premises, *item.conclusions)
            for term_id in (
                endpoint.role_term_ref_id,
                endpoint.subject.selector_kind_term_ref_id,
            )
        )
        _validate_term_refs(term_ids, referenced_terms, "/terms")
        artifact_ids = {item.artifact_id for item in documents}
        if any(
            endpoint.subject.artifact_id not in artifact_ids
            for item in assertions
            for endpoint in (*item.premises, *item.conclusions)
        ):
            _fail(IntentBridgeErrorCode.UNKNOWN_REFERENCE, "/assertions/subject")
        _validate_proof_dag(assertions)
        object.__setattr__(self, "terms", terms)
        object.__setattr__(self, "documents", documents)
        object.__setattr__(self, "assertions", assertions)
        body = self._body_mapping()
        digest = hashlib.sha256(_PROOF_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()
        object.__setattr__(self, "bundle_digest", digest)
        object.__setattr__(self, "bundle_id", f"proof_bundle_{digest[:32]}")
        _canonical_json(self.to_mapping())

    @property
    def executable(self) -> bool:
        return False

    @property
    def adapter_binding_required(self) -> bool:
        return True

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority": self.authority.value,
            "terms": [item.to_mapping() for item in self.terms],
            "documents": [item.to_mapping() for item in self.documents],
            "assertions": [item.to_mapping() for item in self.assertions],
            "producer": self.producer.to_mapping(),
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._body_mapping(),
            "bundle_id": self.bundle_id,
            "bundle_digest": self.bundle_digest,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        keys = {
            "schema_version",
            "authority",
            "terms",
            "documents",
            "assertions",
            "producer",
            "bundle_id",
            "bundle_digest",
        }
        fields = _strict_fields(value, required=keys, path="/")
        raw_terms = _wire_list(fields["terms"], "/terms", maximum=MAX_BRIDGE_TERMS)
        raw_documents = _wire_list(fields["documents"], "/documents", maximum=MAX_BRIDGE_DOCUMENTS)
        raw_assertions = _wire_list(
            fields["assertions"], "/assertions", maximum=MAX_PROOF_ASSERTIONS
        )
        result = cls(
            schema_version=fields["schema_version"],
            authority=_enum_value(fields["authority"], ProofAuthority, "/authority"),
            terms=tuple(
                BridgeTermRef.from_mapping(item, f"/terms/{index}")
                for index, item in enumerate(raw_terms)
            ),
            documents=tuple(
                DocumentRef.from_mapping(item, f"/documents/{index}")
                for index, item in enumerate(raw_documents)
            ),
            assertions=tuple(
                ProofAssertion.from_mapping(item, f"/assertions/{index}")
                for index, item in enumerate(raw_assertions)
            ),
            producer=ProducerBinding.from_mapping(fields["producer"], "/producer"),
        )
        if (
            type(fields["bundle_id"]) is not str
            or type(fields["bundle_digest"]) is not str
            or not hmac.compare_digest(fields["bundle_id"], result.bundle_id)
            or not hmac.compare_digest(fields["bundle_digest"], result.bundle_digest)
        ):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/bundle_digest")
        return result


def encode_proof_bundle(value: ProofBundle) -> bytes:
    if type(value) is not ProofBundle:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")
    return _canonical_json(value.to_mapping())


def decode_proof_bundle(raw: bytes) -> ProofBundle:
    result = ProofBundle.from_mapping(_decode_json(raw))
    if not hmac.compare_digest(raw, encode_proof_bundle(result)):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class BridgeBudget:
    max_input_bytes: int
    max_output_bytes: int
    max_subject_lookups: int
    max_rule_applications: int

    def __post_init__(self) -> None:
        maxima = {
            "max_input_bytes": MAX_TOTAL_PAYLOAD_BYTES,
            "max_output_bytes": MAX_TOTAL_PAYLOAD_BYTES,
            "max_subject_lookups": MAX_SUBJECT_LOOKUPS,
            "max_rule_applications": MAX_RULE_APPLICATIONS,
        }
        for name, maximum in maxima.items():
            object.__setattr__(
                self,
                name,
                _positive_integer(getattr(self, name), f"/{name}", maximum=maximum),
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "max_input_bytes": self.max_input_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_subject_lookups": self.max_subject_lookups,
            "max_rule_applications": self.max_rule_applications,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str = "/budget") -> Self:
        keys = {
            "max_input_bytes",
            "max_output_bytes",
            "max_subject_lookups",
            "max_rule_applications",
        }
        return cls(**_strict_fields(value, required=keys, path=path))


@dataclass(frozen=True, slots=True, kw_only=True)
class CompileInputBinding:
    binding_id: str
    ordinal: int
    role_term_ref_id: str
    artifact_id: str

    def __post_init__(self) -> None:
        for name in ("binding_id", "role_term_ref_id", "artifact_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        object.__setattr__(
            self,
            "ordinal",
            _ordinal(self.ordinal, "/ordinal", maximum=MAX_COMPILE_INPUTS),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "ordinal": self.ordinal,
            "role_term_ref_id": self.role_term_ref_id,
            "artifact_id": self.artifact_id,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str) -> Self:
        keys = {"binding_id", "ordinal", "role_term_ref_id", "artifact_id"}
        return cls(**_strict_fields(value, required=keys, path=path))


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestedOutput:
    output_id: str
    ordinal: int
    role_term_ref_id: str
    schema_term_ref_id: str

    def __post_init__(self) -> None:
        for name in ("output_id", "role_term_ref_id", "schema_term_ref_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        object.__setattr__(
            self,
            "ordinal",
            _ordinal(self.ordinal, "/ordinal", maximum=MAX_COMPILE_OUTPUTS),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "output_id": self.output_id,
            "ordinal": self.ordinal,
            "role_term_ref_id": self.role_term_ref_id,
            "schema_term_ref_id": self.schema_term_ref_id,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str) -> Self:
        keys = {"output_id", "ordinal", "role_term_ref_id", "schema_term_ref_id"}
        return cls(**_strict_fields(value, required=keys, path=path))


@dataclass(frozen=True, slots=True, kw_only=True)
class IntentCompileRequest:
    compiler: ProducerDescriptor
    terms: tuple[BridgeTermRef, ...]
    documents: tuple[DocumentRef, ...]
    inputs: tuple[CompileInputBinding, ...]
    requested_outputs: tuple[RequestedOutput, ...]
    budget: BridgeBudget
    schema_version: int = INTENT_BRIDGE_SCHEMA_VERSION
    request_id: str = field(init=False)
    request_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != INTENT_BRIDGE_SCHEMA_VERSION
        ):
            _fail(IntentBridgeErrorCode.UNSUPPORTED_VERSION, "/schema_version")
        if type(self.compiler) is not ProducerDescriptor:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/compiler")
        if type(self.budget) is not BridgeBudget:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/budget")
        terms = _term_table(self.terms)
        documents = _document_table(self.documents)
        inputs = _exact_tuple(
            self.inputs,
            CompileInputBinding,
            "/inputs",
            maximum=MAX_COMPILE_INPUTS,
            minimum=1,
        )
        outputs = _exact_tuple(
            self.requested_outputs,
            RequestedOutput,
            "/requested_outputs",
            maximum=MAX_COMPILE_OUTPUTS,
            minimum=1,
        )
        inputs = tuple(sorted(inputs, key=lambda item: item.ordinal))
        outputs = tuple(sorted(outputs, key=lambda item: item.ordinal))
        if tuple(item.ordinal for item in inputs) != tuple(range(len(inputs))):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/inputs")
        if tuple(item.ordinal for item in outputs) != tuple(range(len(outputs))):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/requested_outputs")
        for values, attribute, path in (
            (inputs, "binding_id", "/inputs"),
            (outputs, "output_id", "/requested_outputs"),
        ):
            if len({getattr(item, attribute) for item in values}) != len(values):
                _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
        term_ids = {item.term_ref_id for item in terms}
        referenced_terms = {
            term_id
            for document in documents
            for term_id in (document.role_term_ref_id, document.schema_term_ref_id)
        }
        referenced_terms.update(item.role_term_ref_id for item in inputs)
        referenced_terms.update(
            term_id
            for item in outputs
            for term_id in (item.role_term_ref_id, item.schema_term_ref_id)
        )
        _validate_term_refs(term_ids, referenced_terms, "/terms")
        artifact_ids = {item.artifact_id for item in documents}
        if any(item.artifact_id not in artifact_ids for item in inputs):
            _fail(IntentBridgeErrorCode.UNKNOWN_REFERENCE, "/inputs/artifact_id")
        object.__setattr__(self, "terms", terms)
        object.__setattr__(self, "documents", documents)
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "requested_outputs", outputs)
        digest = hashlib.sha256(
            _COMPILE_REQUEST_DIGEST_DOMAIN + _canonical_json(self._body_mapping())
        ).hexdigest()
        object.__setattr__(self, "request_digest", digest)
        object.__setattr__(self, "request_id", f"compile_request_{digest[:32]}")
        _canonical_json(self.to_mapping())

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "compiler": self.compiler.to_mapping(),
            "terms": [item.to_mapping() for item in self.terms],
            "documents": [item.to_mapping() for item in self.documents],
            "inputs": [item.to_mapping() for item in self.inputs],
            "requested_outputs": [item.to_mapping() for item in self.requested_outputs],
            "budget": self.budget.to_mapping(),
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._body_mapping(),
            "request_id": self.request_id,
            "request_digest": self.request_digest,
        }


class BridgeDisposition(StrEnum):
    INERT = "inert"
    PARTIAL = "partial"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True, kw_only=True)
class BridgeDiagnostic:
    diagnostic_id: str
    diagnostic_term_ref_id: str
    subjects: tuple[SubjectRef, ...] = ()

    def __post_init__(self) -> None:
        for name in ("diagnostic_id", "diagnostic_term_ref_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        subjects = _exact_tuple(
            self.subjects,
            SubjectRef,
            "/subjects",
            maximum=MAX_DIAGNOSTIC_SUBJECTS,
        )
        keys = tuple(
            (item.artifact_id, item.selector_kind_term_ref_id, item.selector_id)
            for item in subjects
        )
        if len(set(keys)) != len(keys):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/subjects")
        object.__setattr__(
            self,
            "subjects",
            tuple(
                sorted(
                    subjects,
                    key=lambda item: (
                        item.artifact_id,
                        item.selector_kind_term_ref_id,
                        item.selector_id,
                    ),
                )
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "diagnostic_id": self.diagnostic_id,
            "diagnostic_term_ref_id": self.diagnostic_term_ref_id,
            "subjects": [item.to_mapping() for item in self.subjects],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class IntentCompileResult:
    request_digest: str
    compiler: ProducerDescriptor
    disposition: BridgeDisposition
    output_documents: tuple[DocumentRef, ...] = ()
    proof_bundle: ProofBundle | None = None
    diagnostics: tuple[BridgeDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_digest", _digest(self.request_digest, "/request_digest"))
        if type(self.compiler) is not ProducerDescriptor:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/compiler")
        if type(self.disposition) is not BridgeDisposition:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/disposition")
        outputs = _exact_tuple(
            self.output_documents,
            DocumentRef,
            "/output_documents",
            maximum=MAX_COMPILE_OUTPUTS,
        )
        outputs = tuple(sorted(outputs, key=lambda item: item.artifact_id))
        if len({item.artifact_id for item in outputs}) != len(outputs):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/output_documents")
        diagnostics = _exact_tuple(
            self.diagnostics,
            BridgeDiagnostic,
            "/diagnostics",
            maximum=MAX_DIAGNOSTICS,
        )
        diagnostics = tuple(sorted(diagnostics, key=lambda item: item.diagnostic_id))
        if len({item.diagnostic_id for item in diagnostics}) != len(diagnostics):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/diagnostics")
        if self.proof_bundle is not None and type(self.proof_bundle) is not ProofBundle:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/proof_bundle")
        if self.disposition is BridgeDisposition.INERT and (
            outputs or self.proof_bundle is not None
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/disposition")
        if self.disposition is BridgeDisposition.COMPLETE:
            if not outputs or self.proof_bundle is None:
                _fail(IntentBridgeErrorCode.INVALID_INPUT, "/disposition")
            if self.proof_bundle.producer.descriptor != self.compiler or not hmac.compare_digest(
                self.proof_bundle.producer.request_sha256, self.request_digest
            ):
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/proof_bundle/producer")
            proof_documents = {item.artifact_id: item for item in self.proof_bundle.documents}
            if any(proof_documents.get(item.artifact_id) != item for item in outputs):
                _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/output_documents")
        object.__setattr__(self, "output_documents", outputs)
        object.__setattr__(self, "diagnostics", diagnostics)


@dataclass(frozen=True, slots=True, kw_only=True)
class AdapterDescriptor:
    adapter_id: str
    adapter_version: str
    adapter_contract_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter_id", _identifier(self.adapter_id, "/adapter_id"))
        object.__setattr__(
            self,
            "adapter_version",
            _version(self.adapter_version, "/adapter_version"),
        )
        object.__setattr__(
            self,
            "adapter_contract_sha256",
            _digest(self.adapter_contract_sha256, "/adapter_contract_sha256"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "adapter_contract_sha256": self.adapter_contract_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendLoweringRequest:
    adapter: AdapterDescriptor
    terms: tuple[BridgeTermRef, ...]
    documents: tuple[DocumentRef, ...]
    intent_artifact_ids: tuple[str, ...]
    capability_artifact_ids: tuple[str, ...]
    proof_bundle: ProofBundle
    budget: BridgeBudget
    schema_version: int = INTENT_BRIDGE_SCHEMA_VERSION
    request_id: str = field(init=False)
    request_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != INTENT_BRIDGE_SCHEMA_VERSION
        ):
            _fail(IntentBridgeErrorCode.UNSUPPORTED_VERSION, "/schema_version")
        if type(self.adapter) is not AdapterDescriptor:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/adapter")
        if type(self.proof_bundle) is not ProofBundle or type(self.budget) is not BridgeBudget:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")
        terms = _term_table(self.terms)
        documents = _document_table(self.documents)
        intents = _identifier_tuple(
            self.intent_artifact_ids,
            "/intent_artifact_ids",
            maximum=MAX_COMPILE_OUTPUTS,
            minimum=1,
        )
        capabilities = _identifier_tuple(
            self.capability_artifact_ids,
            "/capability_artifact_ids",
            maximum=MAX_COMPILE_INPUTS,
            minimum=1,
        )
        term_ids = {item.term_ref_id for item in terms}
        _validate_term_refs(
            term_ids,
            {
                term_id
                for item in documents
                for term_id in (item.role_term_ref_id, item.schema_term_ref_id)
            },
            "/terms",
        )
        document_by_id = {item.artifact_id: item for item in documents}
        if not set((*intents, *capabilities)) <= set(document_by_id):
            _fail(IntentBridgeErrorCode.UNKNOWN_REFERENCE, "/documents")
        proof_documents = {item.artifact_id: item for item in self.proof_bundle.documents}
        if any(proof_documents.get(item) != document_by_id[item] for item in intents):
            _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/intent_artifact_ids")
        object.__setattr__(self, "terms", terms)
        object.__setattr__(self, "documents", documents)
        object.__setattr__(self, "intent_artifact_ids", intents)
        object.__setattr__(self, "capability_artifact_ids", capabilities)
        digest = hashlib.sha256(
            _LOWERING_REQUEST_DIGEST_DOMAIN + _canonical_json(self._body_mapping())
        ).hexdigest()
        object.__setattr__(self, "request_digest", digest)
        object.__setattr__(self, "request_id", f"lowering_request_{digest[:32]}")
        _canonical_json(self.to_mapping())

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "adapter": self.adapter.to_mapping(),
            "terms": [item.to_mapping() for item in self.terms],
            "documents": [item.to_mapping() for item in self.documents],
            "intent_artifact_ids": list(self.intent_artifact_ids),
            "capability_artifact_ids": list(self.capability_artifact_ids),
            "proof_bundle": self.proof_bundle.to_mapping(),
            "budget": self.budget.to_mapping(),
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            **self._body_mapping(),
            "request_id": self.request_id,
            "request_digest": self.request_digest,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendLoweringResult:
    request_digest: str
    adapter: AdapterDescriptor
    disposition: BridgeDisposition
    plan_document: DocumentRef | None = None
    supported_subjects: tuple[SubjectRef, ...] = ()
    inert_subjects: tuple[SubjectRef, ...] = ()
    diagnostics: tuple[BridgeDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_digest", _digest(self.request_digest, "/request_digest"))
        if (
            type(self.adapter) is not AdapterDescriptor
            or type(self.disposition) is not BridgeDisposition
        ):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/")
        if self.plan_document is not None and type(self.plan_document) is not DocumentRef:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/plan_document")
        supported = _exact_tuple(
            self.supported_subjects,
            SubjectRef,
            "/supported_subjects",
            maximum=MAX_SUBJECT_LOOKUPS,
        )
        inert = _exact_tuple(
            self.inert_subjects,
            SubjectRef,
            "/inert_subjects",
            maximum=MAX_SUBJECT_LOOKUPS,
        )
        if set(supported) & set(inert):
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/supported_subjects")
        diagnostics = _exact_tuple(
            self.diagnostics,
            BridgeDiagnostic,
            "/diagnostics",
            maximum=MAX_DIAGNOSTICS,
        )
        if self.disposition is BridgeDisposition.INERT and self.plan_document is not None:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/plan_document")
        if self.disposition is BridgeDisposition.COMPLETE and self.plan_document is None:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/plan_document")
        if self.disposition is BridgeDisposition.COMPLETE and inert:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/inert_subjects")
        subject_key = lambda item: (  # noqa: E731 - local canonical ordering key
            item.artifact_id,
            item.selector_kind_term_ref_id,
            item.selector_id,
        )
        object.__setattr__(self, "supported_subjects", tuple(sorted(supported, key=subject_key)))
        object.__setattr__(self, "inert_subjects", tuple(sorted(inert, key=subject_key)))
        object.__setattr__(
            self,
            "diagnostics",
            tuple(sorted(diagnostics, key=lambda item: item.diagnostic_id)),
        )


__all__ = [
    "AdapterDescriptor",
    "BackendLoweringRequest",
    "BackendLoweringResult",
    "BridgeBudget",
    "BridgeDiagnostic",
    "BridgeDisposition",
    "BridgeTermRef",
    "CompileInputBinding",
    "DocumentRef",
    "INTENT_BRIDGE_SCHEMA_VERSION",
    "IntentBridgeError",
    "IntentBridgeErrorCode",
    "IntentCompileRequest",
    "IntentCompileResult",
    "ProducerBinding",
    "ProducerDescriptor",
    "ProofAssertion",
    "ProofAuthority",
    "ProofBundle",
    "ProofEndpoint",
    "RequestedOutput",
    "SubjectRef",
    "decode_proof_bundle",
    "encode_proof_bundle",
]
