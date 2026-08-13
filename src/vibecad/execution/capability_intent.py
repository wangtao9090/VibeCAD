"""Backend-neutral intent and exact trusted-adapter selection receipt.

``CapabilityIntent`` describes requested semantics, evidence subjects, and
content-addressed proof/acceptance artifacts without naming a CAD backend.
``CapabilityAdapterBinding`` is maintained by trusted code and binds one
semantic operation to one exact verified catalog descriptor.  Compilation only
creates an immutable selection receipt; it never invokes an adapter.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum

from vibecad.execution.capabilities import (
    MAX_CAPABILITY_CATALOG_BYTES,
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
    CapabilitySupportStatus,
    CapabilityTermRef,
)
from vibecad.execution.capability_index import CapabilityCatalogIndex

CAPABILITY_INTENT_SCHEMA_VERSION = 1
MAX_CAPABILITY_INTENT_BYTES = 256 * 1024
MAX_CAPABILITY_INTENT_TERMS = 256
MAX_CAPABILITY_INTENT_SOURCES = 64
MAX_CAPABILITY_INTENT_ARGUMENTS = 256
MAX_CAPABILITY_INTENT_PROOFS = 128
MAX_CAPABILITY_ARGUMENT_EVIDENCE_IDS = 64
MAX_CAPABILITY_PROOF_SUBJECTS = 128
MAX_CAPABILITY_ARGUMENT_VALUE_BYTES = 64 * 1024

_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 32_768
_MAX_JSON_STRING_BYTES = 64 * 1024
_MAX_IDENTIFIER_BYTES = 128
_MAX_MEDIA_TYPE_BYTES = 128
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,127}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_INTENT_DIGEST_DOMAIN = b"vibecad-capability-intent-v1\0"
_BINDING_DIGEST_DOMAIN = b"vibecad-capability-adapter-binding-v1\0"
_INVOCATION_DIGEST_DOMAIN = b"vibecad-compiled-capability-invocation-v1\0"


def _fail(code: CapabilityCatalogErrorCode, path: str) -> None:
    raise CapabilityCatalogError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if not value or size > _MAX_IDENTIFIER_BYTES or _IDENTIFIER.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if ".." in value or "//" in value:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _json_tree(value: object, path: str, depth: int, remaining: list[int]) -> None:
    remaining[0] -= 1
    if remaining[0] < 0 or depth > _MAX_JSON_DEPTH:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, path)
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > _MAX_SAFE_INTEGER:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
        return
    if type(value) is str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
        if size > _MAX_JSON_STRING_BYTES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, path)
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _json_tree(item, f"{path}/{index}", depth + 1, remaining)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
            _json_tree(key, path, depth + 1, remaining)
            _json_tree(item, f"{path}/{key}", depth + 1, remaining)
        return
    _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)


def _canonical(value: object, *, maximum: int) -> bytes:
    _json_tree(value, "", 0, [_MAX_JSON_NODES])
    try:
        raw = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "")
    if not raw or len(raw) > maximum:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "")
    return raw


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if type(key) is not str or key in result:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "")
        result[key] = value
    return result


def _constant(_value: str) -> object:
    _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "")


def _decode(raw: object, *, maximum: int) -> object:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except CapabilityCatalogError:
        raise
    except (
        json.JSONDecodeError,
        UnicodeError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "")
    _json_tree(value, "", 0, [_MAX_JSON_NODES])
    if _canonical(value, maximum=maximum) != raw:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "")
    return value


def _freeze_json(value: object, path: str) -> bytes:
    raw = _canonical(value, maximum=MAX_CAPABILITY_ARGUMENT_VALUE_BYTES)
    _decode(raw, maximum=MAX_CAPABILITY_ARGUMENT_VALUE_BYTES)
    return raw


def _tuple(
    value: object,
    path: str,
    *,
    item_type: type,
    maximum: int,
    key,
) -> tuple:
    if type(value) is not tuple:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if len(value) > maximum:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, path)
    if not all(type(item) is item_type for item in value):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    keys = tuple(key(item) for item in value)
    if len(set(keys)) != len(keys):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _identifier_tuple(value: object, path: str, *, maximum: int) -> tuple[str, ...]:
    if type(value) is not tuple:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if len(value) > maximum:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, path)
    result = tuple(_identifier(item, f"{path}/{index}") for index, item in enumerate(value))
    if len(set(result)) != len(result):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return tuple(sorted(result))


def _term_mapping(item: CapabilityTermRef) -> dict[str, object]:
    return {
        "namespace": item.namespace,
        "term_definition_sha256": item.term_definition_sha256,
        "term_id": item.term_id,
        "term_ref_id": item.term_ref_id,
        "vocabulary_version": item.vocabulary_version,
    }


def _term_from(value: object, path: str) -> CapabilityTermRef:
    item = _exact(
        value,
        {
            "namespace",
            "term_definition_sha256",
            "term_id",
            "term_ref_id",
            "vocabulary_version",
        },
        path,
    )
    return CapabilityTermRef(**item)


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityContentRef:
    sha256: str
    size_bytes: int
    media_type: str
    schema_sha256: str | None = None

    def __post_init__(self) -> None:
        _digest(self.sha256, "sha256")
        if type(self.size_bytes) is not int or not 0 < self.size_bytes <= _MAX_SAFE_INTEGER:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "size_bytes")
        if (
            type(self.media_type) is not str
            or len(self.media_type.encode("ascii", errors="ignore")) > _MAX_MEDIA_TYPE_BYTES
            or _MEDIA_TYPE.fullmatch(self.media_type) is None
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "media_type")
        if self.schema_sha256 is not None:
            _digest(self.schema_sha256, "schema_sha256")


def _content_mapping(item: CapabilityContentRef) -> dict[str, object]:
    return {
        "media_type": item.media_type,
        "schema_sha256": item.schema_sha256,
        "sha256": item.sha256,
        "size_bytes": item.size_bytes,
    }


def _content_from(value: object, path: str) -> CapabilityContentRef:
    return CapabilityContentRef(
        **_exact(value, {"media_type", "schema_sha256", "sha256", "size_bytes"}, path)
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityIntentSource:
    source_id: str
    role_term_ref_id: str
    content: CapabilityContentRef

    def __post_init__(self) -> None:
        _identifier(self.source_id, "source_id")
        _identifier(self.role_term_ref_id, "role_term_ref_id")
        if type(self.content) is not CapabilityContentRef:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "content")


class CapabilityIntentValueState(StrEnum):
    OBSERVED = "observed"
    CONFIRMED = "confirmed"
    ASSUMED = "assumed"
    UNKNOWN = "unknown"
    CONFLICTED = "conflicted"


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityIntentArgument:
    argument_id: str
    semantic_term_ref_id: str
    state: CapabilityIntentValueState
    value: object
    unit_term_ref_id: str | None = None
    evidence_element_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.argument_id, "argument_id")
        _identifier(self.semantic_term_ref_id, "semantic_term_ref_id")
        if type(self.state) is not CapabilityIntentValueState:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "state")
        if self.unit_term_ref_id is not None:
            _identifier(self.unit_term_ref_id, "unit_term_ref_id")
        evidence = _identifier_tuple(
            self.evidence_element_ids,
            "evidence_element_ids",
            maximum=MAX_CAPABILITY_ARGUMENT_EVIDENCE_IDS,
        )
        if (
            self.state
            in {
                CapabilityIntentValueState.UNKNOWN,
                CapabilityIntentValueState.CONFLICTED,
            }
            and self.value is not None
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "value")
        if (
            self.state
            not in {
                CapabilityIntentValueState.UNKNOWN,
                CapabilityIntentValueState.CONFLICTED,
            }
            and self.value is None
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "value")
        object.__setattr__(self, "value", _freeze_json(self.value, "value"))
        object.__setattr__(self, "evidence_element_ids", evidence)

    @property
    def decoded_value(self) -> object:
        return _decode(self.value, maximum=MAX_CAPABILITY_ARGUMENT_VALUE_BYTES)


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityIntentProof:
    proof_id: str
    proof_kind_term_ref_id: str
    subject_argument_ids: tuple[str, ...]
    content: CapabilityContentRef

    def __post_init__(self) -> None:
        _identifier(self.proof_id, "proof_id")
        _identifier(self.proof_kind_term_ref_id, "proof_kind_term_ref_id")
        subjects = _identifier_tuple(
            self.subject_argument_ids,
            "subject_argument_ids",
            maximum=MAX_CAPABILITY_PROOF_SUBJECTS,
        )
        if not subjects:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "subject_argument_ids")
        if type(self.content) is not CapabilityContentRef:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "content")
        object.__setattr__(self, "subject_argument_ids", subjects)


def _source_mapping(item: CapabilityIntentSource) -> dict[str, object]:
    return {
        "content": _content_mapping(item.content),
        "role_term_ref_id": item.role_term_ref_id,
        "source_id": item.source_id,
    }


def _argument_mapping(item: CapabilityIntentArgument) -> dict[str, object]:
    return {
        "argument_id": item.argument_id,
        "evidence_element_ids": list(item.evidence_element_ids),
        "semantic_term_ref_id": item.semantic_term_ref_id,
        "state": item.state.value,
        "unit_term_ref_id": item.unit_term_ref_id,
        "value": item.decoded_value,
    }


def _proof_mapping(item: CapabilityIntentProof) -> dict[str, object]:
    return {
        "content": _content_mapping(item.content),
        "proof_id": item.proof_id,
        "proof_kind_term_ref_id": item.proof_kind_term_ref_id,
        "subject_argument_ids": list(item.subject_argument_ids),
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityIntent:
    schema_version: int
    intent_id: str
    operation_term_ref_id: str
    terms: tuple[CapabilityTermRef, ...]
    sources: tuple[CapabilityIntentSource, ...]
    arguments: tuple[CapabilityIntentArgument, ...]
    proofs: tuple[CapabilityIntentProof, ...]
    acceptance: CapabilityContentRef

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != CAPABILITY_INTENT_SCHEMA_VERSION
        ):
            _fail(CapabilityCatalogErrorCode.UNSUPPORTED_VERSION, "schema_version")
        _identifier(self.intent_id, "intent_id")
        _identifier(self.operation_term_ref_id, "operation_term_ref_id")
        terms = _tuple(
            self.terms,
            "terms",
            item_type=CapabilityTermRef,
            maximum=MAX_CAPABILITY_INTENT_TERMS,
            key=lambda item: item.term_ref_id,
        )
        sources = _tuple(
            self.sources,
            "sources",
            item_type=CapabilityIntentSource,
            maximum=MAX_CAPABILITY_INTENT_SOURCES,
            key=lambda item: item.source_id,
        )
        arguments = _tuple(
            self.arguments,
            "arguments",
            item_type=CapabilityIntentArgument,
            maximum=MAX_CAPABILITY_INTENT_ARGUMENTS,
            key=lambda item: item.argument_id,
        )
        proofs = _tuple(
            self.proofs,
            "proofs",
            item_type=CapabilityIntentProof,
            maximum=MAX_CAPABILITY_INTENT_PROOFS,
            key=lambda item: item.proof_id,
        )
        if type(self.acceptance) is not CapabilityContentRef:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "acceptance")
        term_ids = {item.term_ref_id for item in terms}
        refs = {self.operation_term_ref_id}
        refs.update(item.role_term_ref_id for item in sources)
        refs.update(item.semantic_term_ref_id for item in arguments)
        refs.update(
            item.unit_term_ref_id for item in arguments if item.unit_term_ref_id is not None
        )
        refs.update(item.proof_kind_term_ref_id for item in proofs)
        if not refs <= term_ids:
            _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "terms")
        argument_ids = {item.argument_id for item in arguments}
        if any(not set(item.subject_argument_ids) <= argument_ids for item in proofs):
            _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "proofs")
        object.__setattr__(self, "terms", tuple(sorted(terms, key=lambda item: item.term_ref_id)))
        object.__setattr__(self, "sources", tuple(sorted(sources, key=lambda item: item.source_id)))
        object.__setattr__(
            self,
            "arguments",
            tuple(sorted(arguments, key=lambda item: item.argument_id)),
        )
        object.__setattr__(self, "proofs", tuple(sorted(proofs, key=lambda item: item.proof_id)))
        _canonical(self._body(), maximum=MAX_CAPABILITY_INTENT_BYTES)

    def _body(self) -> dict[str, object]:
        return {
            "acceptance": _content_mapping(self.acceptance),
            "arguments": [_argument_mapping(item) for item in self.arguments],
            "intent_id": self.intent_id,
            "operation_term_ref_id": self.operation_term_ref_id,
            "proofs": [_proof_mapping(item) for item in self.proofs],
            "schema_version": self.schema_version,
            "sources": [_source_mapping(item) for item in self.sources],
            "terms": [_term_mapping(item) for item in self.terms],
        }

    @property
    def intent_sha256(self) -> str:
        return hashlib.sha256(
            _INTENT_DIGEST_DOMAIN + _canonical(self._body(), maximum=MAX_CAPABILITY_INTENT_BYTES)
        ).hexdigest()


def encode_capability_intent(value: object) -> bytes:
    if type(value) is not CapabilityIntent:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "")
    envelope = value._body()
    envelope["intent_sha256"] = value.intent_sha256
    return _canonical(envelope, maximum=MAX_CAPABILITY_INTENT_BYTES)


def _exact(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _source_from(value: object, path: str) -> CapabilityIntentSource:
    item = _exact(value, {"content", "role_term_ref_id", "source_id"}, path)
    return CapabilityIntentSource(
        source_id=item["source_id"],
        role_term_ref_id=item["role_term_ref_id"],
        content=_content_from(item["content"], f"{path}/content"),
    )


def _argument_from(value: object, path: str) -> CapabilityIntentArgument:
    item = _exact(
        value,
        {
            "argument_id",
            "evidence_element_ids",
            "semantic_term_ref_id",
            "state",
            "unit_term_ref_id",
            "value",
        },
        path,
    )
    try:
        state = CapabilityIntentValueState(item["state"])
    except (TypeError, ValueError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, f"{path}/state")
    return CapabilityIntentArgument(
        argument_id=item["argument_id"],
        semantic_term_ref_id=item["semantic_term_ref_id"],
        state=state,
        value=item["value"],
        unit_term_ref_id=item["unit_term_ref_id"],
        evidence_element_ids=(
            tuple(item["evidence_element_ids"])
            if type(item["evidence_element_ids"]) is list
            else item["evidence_element_ids"]
        ),
    )


def _proof_from(value: object, path: str) -> CapabilityIntentProof:
    item = _exact(
        value,
        {"content", "proof_id", "proof_kind_term_ref_id", "subject_argument_ids"},
        path,
    )
    return CapabilityIntentProof(
        proof_id=item["proof_id"],
        proof_kind_term_ref_id=item["proof_kind_term_ref_id"],
        subject_argument_ids=(
            tuple(item["subject_argument_ids"])
            if type(item["subject_argument_ids"]) is list
            else item["subject_argument_ids"]
        ),
        content=_content_from(item["content"], f"{path}/content"),
    )


def decode_capability_intent(raw: object) -> CapabilityIntent:
    value = _decode(raw, maximum=MAX_CAPABILITY_INTENT_BYTES)
    item = _exact(
        value,
        {
            "acceptance",
            "arguments",
            "intent_id",
            "intent_sha256",
            "operation_term_ref_id",
            "proofs",
            "schema_version",
            "sources",
            "terms",
        },
        "",
    )
    if not all(type(item[key]) is list for key in ("arguments", "proofs", "sources", "terms")):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "")
    result = CapabilityIntent(
        schema_version=item["schema_version"],
        intent_id=item["intent_id"],
        operation_term_ref_id=item["operation_term_ref_id"],
        terms=tuple(
            _term_from(entry, f"terms/{index}") for index, entry in enumerate(item["terms"])
        ),
        sources=tuple(
            _source_from(entry, f"sources/{index}") for index, entry in enumerate(item["sources"])
        ),
        arguments=tuple(
            _argument_from(entry, f"arguments/{index}")
            for index, entry in enumerate(item["arguments"])
        ),
        proofs=tuple(
            _proof_from(entry, f"proofs/{index}") for index, entry in enumerate(item["proofs"])
        ),
        acceptance=_content_from(item["acceptance"], "acceptance"),
    )
    if not hmac.compare_digest(
        result.intent_sha256, _digest(item["intent_sha256"], "intent_sha256")
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "intent_sha256")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityAdapterBinding:
    binding_id: str
    backend: CapabilityBackend
    catalog_sha256: str
    capability_id: str
    capability_descriptor_sha256: str
    operation_term: CapabilityTermRef
    execution_profile: CapabilityExecutionProfile
    adapter_id: str
    adapter_version: str
    adapter_receipt_sha256: str
    input_contract: CapabilityContentRef
    output_contract: CapabilityContentRef
    proof_rule: CapabilityContentRef

    def __post_init__(self) -> None:
        _identifier(self.binding_id, "binding_id")
        if type(self.backend) is not CapabilityBackend:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "backend")
        _digest(self.catalog_sha256, "catalog_sha256")
        _identifier(self.capability_id, "capability_id")
        _digest(self.capability_descriptor_sha256, "capability_descriptor_sha256")
        if type(self.operation_term) is not CapabilityTermRef:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "operation_term")
        if type(self.execution_profile) is not CapabilityExecutionProfile:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "execution_profile")
        _identifier(self.adapter_id, "adapter_id")
        _identifier(self.adapter_version, "adapter_version")
        _digest(self.adapter_receipt_sha256, "adapter_receipt_sha256")
        for path, value in (
            ("input_contract", self.input_contract),
            ("output_contract", self.output_contract),
            ("proof_rule", self.proof_rule),
        ):
            if type(value) is not CapabilityContentRef:
                _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)

    def _body(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_receipt_sha256": self.adapter_receipt_sha256,
            "adapter_version": self.adapter_version,
            "backend": {
                "backend_id": self.backend.backend_id,
                "backend_version": list(self.backend.backend_version),
                "build_fingerprint_sha256": self.backend.build_fingerprint_sha256,
                "discovery_profile": self.backend.discovery_profile.value,
                "platform_id": self.backend.platform_id,
            },
            "binding_id": self.binding_id,
            "capability_descriptor_sha256": self.capability_descriptor_sha256,
            "capability_id": self.capability_id,
            "catalog_sha256": self.catalog_sha256,
            "execution_profile": self.execution_profile.value,
            "input_contract": _content_mapping(self.input_contract),
            "operation_term": _term_mapping(self.operation_term),
            "output_contract": _content_mapping(self.output_contract),
            "proof_rule": _content_mapping(self.proof_rule),
        }

    @property
    def binding_sha256(self) -> str:
        return hashlib.sha256(
            _BINDING_DIGEST_DOMAIN + _canonical(self._body(), maximum=MAX_CAPABILITY_CATALOG_BYTES)
        ).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class CompiledCapabilityInvocation:
    intent_id: str
    intent_sha256: str
    catalog_sha256: str
    capability_id: str
    capability_descriptor_sha256: str
    binding_id: str
    binding_sha256: str
    execution_profile: CapabilityExecutionProfile
    proof_content_sha256: tuple[str, ...]
    acceptance_sha256: str

    def __post_init__(self) -> None:
        for path, value in (
            ("intent_id", self.intent_id),
            ("capability_id", self.capability_id),
            ("binding_id", self.binding_id),
        ):
            _identifier(value, path)
        for path, value in (
            ("intent_sha256", self.intent_sha256),
            ("catalog_sha256", self.catalog_sha256),
            ("capability_descriptor_sha256", self.capability_descriptor_sha256),
            ("binding_sha256", self.binding_sha256),
            ("acceptance_sha256", self.acceptance_sha256),
        ):
            _digest(value, path)
        if type(self.execution_profile) is not CapabilityExecutionProfile:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "execution_profile")
        if (
            type(self.proof_content_sha256) is not tuple
            or len(self.proof_content_sha256) > MAX_CAPABILITY_INTENT_PROOFS
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "proof_content_sha256")
        proofs = tuple(
            _digest(item, f"proof_content_sha256/{index}")
            for index, item in enumerate(self.proof_content_sha256)
        )
        if len(set(proofs)) != len(proofs):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "proof_content_sha256")
        object.__setattr__(self, "proof_content_sha256", tuple(sorted(proofs)))

    @property
    def invocation_sha256(self) -> str:
        body = {
            "acceptance_sha256": self.acceptance_sha256,
            "binding_id": self.binding_id,
            "binding_sha256": self.binding_sha256,
            "capability_descriptor_sha256": self.capability_descriptor_sha256,
            "capability_id": self.capability_id,
            "catalog_sha256": self.catalog_sha256,
            "execution_profile": self.execution_profile.value,
            "intent_id": self.intent_id,
            "intent_sha256": self.intent_sha256,
            "proof_content_sha256": list(self.proof_content_sha256),
        }
        return hashlib.sha256(
            _INVOCATION_DIGEST_DOMAIN + _canonical(body, maximum=MAX_CAPABILITY_CATALOG_BYTES)
        ).hexdigest()


def compile_capability_intent(
    *,
    intent: CapabilityIntent,
    catalog: CapabilityCatalogIndex,
    binding: CapabilityAdapterBinding,
) -> CompiledCapabilityInvocation:
    """Select an exact verified capability without running its adapter."""

    if type(intent) is not CapabilityIntent:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "intent")
    if type(catalog) is not CapabilityCatalogIndex:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "catalog")
    if type(binding) is not CapabilityAdapterBinding:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "binding")
    if binding.backend != catalog.backend:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "binding/backend")
    if not hmac.compare_digest(binding.catalog_sha256, catalog.catalog_sha256):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "binding/catalog_sha256")
    descriptor = catalog.lookup(binding.capability_id)
    if descriptor.status is not CapabilitySupportStatus.VERIFIED:
        _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "binding/capability_id")
    if not hmac.compare_digest(
        descriptor.descriptor_sha256,
        binding.capability_descriptor_sha256,
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "binding/capability_id")
    if binding.execution_profile not in descriptor.execution_profiles:
        _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "binding/execution_profile")
    terms = {item.term_ref_id: item for item in intent.terms}
    intent_operation = terms[intent.operation_term_ref_id]
    if intent_operation != binding.operation_term:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "binding/operation_term")
    if not intent.proofs:
        _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "intent/proofs")
    if any(
        argument.state
        in {CapabilityIntentValueState.UNKNOWN, CapabilityIntentValueState.CONFLICTED}
        for argument in intent.arguments
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "intent/arguments")
    return CompiledCapabilityInvocation(
        intent_id=intent.intent_id,
        intent_sha256=intent.intent_sha256,
        catalog_sha256=catalog.catalog_sha256,
        capability_id=descriptor.capability_id,
        capability_descriptor_sha256=descriptor.descriptor_sha256,
        binding_id=binding.binding_id,
        binding_sha256=binding.binding_sha256,
        execution_profile=binding.execution_profile,
        proof_content_sha256=tuple(item.content.sha256 for item in intent.proofs),
        acceptance_sha256=intent.acceptance.sha256,
    )


__all__ = ()
