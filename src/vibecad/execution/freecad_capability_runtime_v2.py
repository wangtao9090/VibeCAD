"""Internal managed-FreeCAD capability composition and bounded queries.

This module performs one headless discovery collection, composes the reviewed
compiler, operation, and built-in intent catalogs plus the reviewed built-in
intent promotion packs with caller-supplied formal catalogs and promotion
packs, and binds the result to the exact runtime build.  It remains an
in-process internal seam: no MCP exposure, registry mutation, persistence,
execution, or runtime lifecycle management occurs here.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import importlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from vibecad import __version__
from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityCatalogSegment,
    CapabilitySupportStatus,
)
from vibecad.execution.compiler_capabilities import (
    build_current_compiler_capability_catalog,
)
from vibecad.execution.freecad_builtin_intent_capabilities import (
    build_current_freecad_intent_capability_catalog,
    current_freecad_intent_promotion_specs,
)
from vibecad.execution.freecad_capability_projection_v2 import (
    MAX_FREECAD_CAPABILITY_FORMAL_CATALOGS,
    MAX_FREECAD_CAPABILITY_PROMOTION_PACKS,
    FreeCadCapabilityIndexEntry,
    FreeCadCapabilityProjectionV2,
    FreeCadCapabilityPromotionPack,
    FreeCadCapabilitySemanticKind,
    build_freecad_capability_projection_v2,
)
from vibecad.execution.freecad_discovery_runtime_v2 import (
    FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
    collect_managed_freecad_discovery_v2,
)
from vibecad.execution.freecad_discovery_v2 import (
    DEFAULT_FREECAD_DISCOVERY_V2_PAGE_DESCRIPTORS,
    FreeCadPagedCapabilityCatalog,
)
from vibecad.execution.freecad_intent_promotions import (
    build_freecad_intent_capability_promotion_packs,
)
from vibecad.execution.freecad_reviewed_release_attestation import (
    decode_freecad_reviewed_release_attestation,
    validate_freecad_reviewed_release_attestation,
)
from vibecad.execution.freecad_reviewed_release_attestation_resource import (
    FreeCadPackagedReviewedReleaseAttestation,
    load_current_packaged_freecad_reviewed_release_attestation,
)
from vibecad.execution.operation_capabilities import (
    build_operation_capability_catalog,
)
from vibecad.execution.registry import DEFAULT_OPERATION_REGISTRY

FREECAD_CAPABILITY_RUNTIME_V2_SCHEMA_VERSION = 1
FREECAD_CAPABILITY_QUERY_V2_SCHEMA_VERSION = 1
MAX_FREECAD_CAPABILITY_QUERY_PAGE_SIZE = 128
MAX_FREECAD_CAPABILITY_QUERY_CURSOR_BYTES = 2_048
MAX_FREECAD_CAPABILITY_RUNTIME_BINDING_BYTES = 256 * 1024
MAX_FREECAD_CAPABILITY_QUERY_PAGE_BYTES = 2 * 1024 * 1024

_MAX_SAFE_INTEGER = 2**53 - 1
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,191}$")
_CURSOR = re.compile(r"^[A-Za-z0-9_-]+$")
_RUNTIME_BINDING_DIGEST_DOMAIN = b"vibecad-freecad-capability-runtime-v2\0"
_QUERY_DIGEST_DOMAIN = b"vibecad-freecad-capability-query-v2\0"
_QUERY_CURSOR_DIGEST_DOMAIN = b"vibecad-freecad-capability-query-cursor-v2\0"
_QUERY_PAGE_DIGEST_DOMAIN = b"vibecad-freecad-capability-query-page-v2\0"


def _fail(code: CapabilityCatalogErrorCode, path: str) -> None:
    raise CapabilityCatalogError(code, path)


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _identifier(value: object, path: str) -> str:
    if type(value) is not str:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if (
        not value
        or size > 192
        or _IDENTIFIER.fullmatch(value) is None
        or ".." in value
        or "//" in value
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _canonical(value: object, *, maximum: int) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "canonical")
    if not raw or len(raw) > maximum:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "canonical")
    return raw


def _backend_mapping(value: CapabilityBackend) -> dict[str, object]:
    return {
        "backend_id": value.backend_id,
        "backend_version": list(value.backend_version),
        "build_fingerprint_sha256": value.build_fingerprint_sha256,
        "discovery_profile": value.discovery_profile.value,
        "platform_id": value.platform_id,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadCapabilityRuntimeBindingV2:
    """Content binding for one exact runtime discovery and projection."""

    schema_version: int
    backend: CapabilityBackend
    discovery_snapshot_sha256: str
    discovery_manifest_sha256: str
    projection_manifest_sha256: str
    projection_catalog_sha256: str
    compiler_catalog_sha256: str
    operation_catalog_sha256: str
    intent_catalog_sha256: str
    extra_formal_catalog_sha256: tuple[str, ...]
    promotion_pack_sha256: tuple[str, ...]
    native_type_count: int

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != FREECAD_CAPABILITY_RUNTIME_V2_SCHEMA_VERSION
        ):
            _fail(CapabilityCatalogErrorCode.UNSUPPORTED_VERSION, "schema_version")
        if type(self.backend) is not CapabilityBackend:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "backend")
        for path, value in (
            ("discovery_snapshot_sha256", self.discovery_snapshot_sha256),
            ("discovery_manifest_sha256", self.discovery_manifest_sha256),
            ("projection_manifest_sha256", self.projection_manifest_sha256),
            ("projection_catalog_sha256", self.projection_catalog_sha256),
            ("compiler_catalog_sha256", self.compiler_catalog_sha256),
            ("operation_catalog_sha256", self.operation_catalog_sha256),
            ("intent_catalog_sha256", self.intent_catalog_sha256),
        ):
            _digest(value, path)
        for path, values, maximum in (
            (
                "extra_formal_catalog_sha256",
                self.extra_formal_catalog_sha256,
                MAX_FREECAD_CAPABILITY_FORMAL_CATALOGS - 3,
            ),
            (
                "promotion_pack_sha256",
                self.promotion_pack_sha256,
                MAX_FREECAD_CAPABILITY_PROMOTION_PACKS,
            ),
        ):
            if type(values) is not tuple:
                _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
            if len(values) > maximum:
                _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, path)
            if values != tuple(sorted(values)) or len(set(values)) != len(values):
                _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
            for value in values:
                _digest(value, path)
        if (
            type(self.native_type_count) is not int
            or not 0 <= self.native_type_count <= _MAX_SAFE_INTEGER
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "native_type_count")
        _canonical(
            self._mapping(),
            maximum=MAX_FREECAD_CAPABILITY_RUNTIME_BINDING_BYTES,
        )

    def _mapping(self) -> dict[str, object]:
        return {
            "backend": _backend_mapping(self.backend),
            "compiler_catalog_sha256": self.compiler_catalog_sha256,
            "discovery_manifest_sha256": self.discovery_manifest_sha256,
            "discovery_snapshot_sha256": self.discovery_snapshot_sha256,
            "extra_formal_catalog_sha256": list(self.extra_formal_catalog_sha256),
            "intent_catalog_sha256": self.intent_catalog_sha256,
            "native_type_count": self.native_type_count,
            "operation_catalog_sha256": self.operation_catalog_sha256,
            "projection_catalog_sha256": self.projection_catalog_sha256,
            "projection_manifest_sha256": self.projection_manifest_sha256,
            "promotion_pack_sha256": list(self.promotion_pack_sha256),
            "schema_version": self.schema_version,
        }

    @property
    def binding_sha256(self) -> str:
        return hashlib.sha256(
            _RUNTIME_BINDING_DIGEST_DOMAIN
            + _canonical(
                self._mapping(),
                maximum=MAX_FREECAD_CAPABILITY_RUNTIME_BINDING_BYTES,
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadCapabilityRuntimeV2:
    """In-memory composition result; it owns no runtime or persistent state."""

    binding: FreeCadCapabilityRuntimeBindingV2
    discovery: FreeCadPagedCapabilityCatalog
    compiler_catalog: CapabilityCatalogSegment
    operation_catalog: CapabilityCatalogSegment
    intent_catalog: CapabilityCatalogSegment
    projection: FreeCadCapabilityProjectionV2

    def __post_init__(self) -> None:
        if type(self.binding) is not FreeCadCapabilityRuntimeBindingV2:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "binding")
        if type(self.discovery) is not FreeCadPagedCapabilityCatalog:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "discovery")
        if type(self.compiler_catalog) is not CapabilityCatalogSegment:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "compiler_catalog")
        if type(self.operation_catalog) is not CapabilityCatalogSegment:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "operation_catalog")
        if type(self.intent_catalog) is not CapabilityCatalogSegment:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "intent_catalog")
        if type(self.projection) is not FreeCadCapabilityProjectionV2:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "projection")
        expected_formal = tuple(
            sorted(
                (
                    self.binding.compiler_catalog_sha256,
                    self.binding.operation_catalog_sha256,
                    self.binding.intent_catalog_sha256,
                    *self.binding.extra_formal_catalog_sha256,
                )
            )
        )
        checks = (
            self.binding.backend == self.discovery.snapshot.backend,
            self.binding.backend == self.projection.manifest.backend,
            hmac.compare_digest(
                self.binding.discovery_snapshot_sha256,
                self.discovery.snapshot.snapshot_sha256,
            ),
            hmac.compare_digest(
                self.binding.discovery_manifest_sha256,
                self.discovery.manifest.manifest_sha256,
            ),
            hmac.compare_digest(
                self.binding.projection_manifest_sha256,
                self.projection.manifest.manifest_sha256,
            ),
            hmac.compare_digest(
                self.binding.projection_catalog_sha256,
                self.projection.index.catalog_sha256,
            ),
            hmac.compare_digest(
                self.binding.compiler_catalog_sha256,
                self.compiler_catalog.catalog_sha256,
            ),
            hmac.compare_digest(
                self.binding.operation_catalog_sha256,
                self.operation_catalog.catalog_sha256,
            ),
            hmac.compare_digest(
                self.binding.intent_catalog_sha256,
                self.intent_catalog.catalog_sha256,
            ),
            self.projection.discovery_pages == self.discovery.pages,
            expected_formal == self.projection.manifest.formal_catalog_sha256,
            self.binding.promotion_pack_sha256 == self.projection.manifest.promotion_pack_sha256,
            self.binding.native_type_count == len(self.projection.manifest.entries),
            self.binding.native_type_count == self.discovery.manifest.type_count,
        )
        if not all(checks):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "runtime")


def encode_freecad_capability_runtime_binding_v2(
    value: FreeCadCapabilityRuntimeBindingV2,
) -> bytes:
    if type(value) is not FreeCadCapabilityRuntimeBindingV2:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "binding")
    return _canonical(
        {**value._mapping(), "binding_sha256": value.binding_sha256},
        maximum=MAX_FREECAD_CAPABILITY_RUNTIME_BINDING_BYTES,
    )


def compose_managed_freecad_capability_runtime_v2(
    *,
    freecad: object,
    probe_modules: tuple[str, ...] = FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
    max_descriptors_per_page: int = DEFAULT_FREECAD_DISCOVERY_V2_PAGE_DESCRIPTORS,
    module_importer: Callable[[str], object] = importlib.import_module,
    extra_formal_catalogs: tuple[CapabilityCatalogSegment, ...] = (),
    promotion_packs: tuple[FreeCadCapabilityPromotionPack, ...] = (),
) -> FreeCadCapabilityRuntimeV2:
    """Collect and compose one exact managed runtime capability view.

    VERIFIED promotion can come only from the fixed, source-pinned package
    resource loaded inside this function.  There is deliberately no caller
    parameter for raw bytes, an attestation object, a source digest, or an
    ephemeral verification set.
    """

    if type(extra_formal_catalogs) is not tuple:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "extra_formal_catalogs")
    if len(extra_formal_catalogs) > MAX_FREECAD_CAPABILITY_FORMAL_CATALOGS - 3:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "extra_formal_catalogs")
    if not all(type(item) is CapabilityCatalogSegment for item in extra_formal_catalogs):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "extra_formal_catalogs")
    if type(promotion_packs) is not tuple:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "promotion_packs")
    if len(promotion_packs) > MAX_FREECAD_CAPABILITY_PROMOTION_PACKS:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "promotion_packs")
    if not all(type(item) is FreeCadCapabilityPromotionPack for item in promotion_packs):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "promotion_packs")
    packaged_attestation = load_current_packaged_freecad_reviewed_release_attestation()
    if type(packaged_attestation) is not FreeCadPackagedReviewedReleaseAttestation:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "package_attestation")
    release_attestation = decode_freecad_reviewed_release_attestation(
        packaged_attestation.raw,
        expected_source_attestation_sha256=packaged_attestation.resource_sha256,
    )
    discovery = collect_managed_freecad_discovery_v2(
        freecad=freecad,
        probe_modules=probe_modules,
        max_descriptors_per_page=max_descriptors_per_page,
        module_importer=module_importer,
    )
    backend = discovery.snapshot.backend
    validated_attestation = validate_freecad_reviewed_release_attestation(
        release_attestation,
        expected_release_version=__version__,
        runtime_backend=backend,
        discovery_snapshot_sha256=discovery.snapshot.snapshot_sha256,
        discovery_manifest_sha256=discovery.manifest.manifest_sha256,
        expected_source_attestation_sha256=packaged_attestation.resource_sha256,
    )
    verification_by_native_type = validated_attestation.verification_set.verification_by_native_type
    compiler_catalog = build_current_compiler_capability_catalog(backend=backend)
    operation_catalog = build_operation_capability_catalog(
        registry=DEFAULT_OPERATION_REGISTRY,
        backend=backend,
    )
    intent_catalog = build_current_freecad_intent_capability_catalog(backend=backend)
    intent_promotion_packs = build_freecad_intent_capability_promotion_packs(
        discovery=discovery,
        specs=current_freecad_intent_promotion_specs(),
        verification_by_native_type=verification_by_native_type,
    )
    if len(intent_promotion_packs) + len(promotion_packs) > MAX_FREECAD_CAPABILITY_PROMOTION_PACKS:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "promotion_packs")
    all_promotion_packs = tuple(
        sorted((*intent_promotion_packs, *promotion_packs), key=lambda item: item.pack_sha256)
    )
    projection = build_freecad_capability_projection_v2(
        discovery=discovery,
        promotion_packs=all_promotion_packs,
        formal_catalogs=(
            compiler_catalog,
            operation_catalog,
            intent_catalog,
            *extra_formal_catalogs,
        ),
    )
    binding = FreeCadCapabilityRuntimeBindingV2(
        schema_version=FREECAD_CAPABILITY_RUNTIME_V2_SCHEMA_VERSION,
        backend=backend,
        discovery_snapshot_sha256=discovery.snapshot.snapshot_sha256,
        discovery_manifest_sha256=discovery.manifest.manifest_sha256,
        projection_manifest_sha256=projection.manifest.manifest_sha256,
        projection_catalog_sha256=projection.index.catalog_sha256,
        compiler_catalog_sha256=compiler_catalog.catalog_sha256,
        operation_catalog_sha256=operation_catalog.catalog_sha256,
        intent_catalog_sha256=intent_catalog.catalog_sha256,
        extra_formal_catalog_sha256=tuple(
            sorted(item.catalog_sha256 for item in extra_formal_catalogs)
        ),
        promotion_pack_sha256=tuple(item.pack_sha256 for item in all_promotion_packs),
        native_type_count=discovery.manifest.type_count,
    )
    return FreeCadCapabilityRuntimeV2(
        binding=binding,
        discovery=discovery,
        compiler_catalog=compiler_catalog,
        operation_catalog=operation_catalog,
        intent_catalog=intent_catalog,
        projection=projection,
    )


def _query_mapping(
    *,
    runtime_binding_sha256: str,
    module: str | None,
    semantic_kind: FreeCadCapabilitySemanticKind | None,
    minimum_status: CapabilitySupportStatus,
    page_size: int,
) -> dict[str, object]:
    return {
        "minimum_status": minimum_status.value,
        "module": module,
        "page_size": page_size,
        "runtime_binding_sha256": runtime_binding_sha256,
        "schema_version": FREECAD_CAPABILITY_QUERY_V2_SCHEMA_VERSION,
        "semantic_kind": None if semantic_kind is None else semantic_kind.value,
    }


def _query_sha256(value: dict[str, object]) -> str:
    return hashlib.sha256(
        _QUERY_DIGEST_DOMAIN
        + _canonical(value, maximum=MAX_FREECAD_CAPABILITY_RUNTIME_BINDING_BYTES)
    ).hexdigest()


def _cursor_body(
    *,
    runtime_binding_sha256: str,
    query_sha256: str,
    page_size: int,
    offset: int,
) -> dict[str, object]:
    return {
        "offset": offset,
        "page_size": page_size,
        "query_sha256": query_sha256,
        "runtime_binding_sha256": runtime_binding_sha256,
        "schema_version": FREECAD_CAPABILITY_QUERY_V2_SCHEMA_VERSION,
    }


def _encode_cursor(
    *,
    runtime_binding_sha256: str,
    query_sha256: str,
    page_size: int,
    offset: int,
) -> str:
    body = _cursor_body(
        runtime_binding_sha256=runtime_binding_sha256,
        query_sha256=query_sha256,
        page_size=page_size,
        offset=offset,
    )
    body_raw = _canonical(body, maximum=MAX_FREECAD_CAPABILITY_QUERY_CURSOR_BYTES)
    cursor_sha256 = hashlib.sha256(_QUERY_CURSOR_DIGEST_DOMAIN + body_raw).hexdigest()
    raw = _canonical(
        {**body, "cursor_sha256": cursor_sha256},
        maximum=MAX_FREECAD_CAPABILITY_QUERY_CURSOR_BYTES,
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(
    value: object,
    *,
    runtime_binding_sha256: str,
    query_sha256: str,
    page_size: int,
) -> int:
    if type(value) is not str:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "cursor")
    try:
        encoded = value.encode("ascii")
    except UnicodeError:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "cursor")
    if (
        not encoded
        or len(encoded) > MAX_FREECAD_CAPABILITY_QUERY_CURSOR_BYTES
        or _CURSOR.fullmatch(value) is None
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "cursor")
    try:
        raw = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
    except (binascii.Error, ValueError, OverflowError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "cursor")
    if not raw or len(raw) > MAX_FREECAD_CAPABILITY_QUERY_CURSOR_BYTES:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "cursor")
    try:
        item = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "cursor")
    expected_keys = {
        "cursor_sha256",
        "offset",
        "page_size",
        "query_sha256",
        "runtime_binding_sha256",
        "schema_version",
    }
    if type(item) is not dict or set(item) != expected_keys:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "cursor")
    supplied_digest = _digest(item["cursor_sha256"], "cursor/cursor_sha256")
    supplied_runtime = _digest(
        item["runtime_binding_sha256"],
        "cursor/runtime_binding_sha256",
    )
    supplied_query = _digest(item["query_sha256"], "cursor/query_sha256")
    supplied_page_size = item["page_size"]
    offset = item["offset"]
    if (
        type(item["schema_version"]) is not int
        or item["schema_version"] != FREECAD_CAPABILITY_QUERY_V2_SCHEMA_VERSION
    ):
        _fail(CapabilityCatalogErrorCode.UNSUPPORTED_VERSION, "cursor/schema_version")
    if (
        type(supplied_page_size) is not int
        or type(offset) is not int
        or not 0 < offset <= _MAX_SAFE_INTEGER
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "cursor")
    body = _cursor_body(
        runtime_binding_sha256=supplied_runtime,
        query_sha256=supplied_query,
        page_size=supplied_page_size,
        offset=offset,
    )
    expected_digest = hashlib.sha256(
        _QUERY_CURSOR_DIGEST_DOMAIN
        + _canonical(body, maximum=MAX_FREECAD_CAPABILITY_QUERY_CURSOR_BYTES)
    ).hexdigest()
    canonical = _canonical(
        {**body, "cursor_sha256": supplied_digest},
        maximum=MAX_FREECAD_CAPABILITY_QUERY_CURSOR_BYTES,
    )
    if (
        not hmac.compare_digest(supplied_digest, expected_digest)
        or canonical != raw
        or not hmac.compare_digest(supplied_runtime, runtime_binding_sha256)
        or not hmac.compare_digest(supplied_query, query_sha256)
        or supplied_page_size != page_size
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "cursor")
    return offset


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadCapabilityQueryPageV2:
    schema_version: int
    runtime_binding_sha256: str
    query_sha256: str
    offset: int
    page_size: int
    total_matches: int
    entries: tuple[FreeCadCapabilityIndexEntry, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != FREECAD_CAPABILITY_QUERY_V2_SCHEMA_VERSION
        ):
            _fail(CapabilityCatalogErrorCode.UNSUPPORTED_VERSION, "schema_version")
        _digest(self.runtime_binding_sha256, "runtime_binding_sha256")
        _digest(self.query_sha256, "query_sha256")
        if type(self.offset) is not int or not 0 <= self.offset <= _MAX_SAFE_INTEGER:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "offset")
        if (
            type(self.page_size) is not int
            or not 1 <= self.page_size <= MAX_FREECAD_CAPABILITY_QUERY_PAGE_SIZE
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "page_size")
        if type(self.total_matches) is not int or not 0 <= self.total_matches <= _MAX_SAFE_INTEGER:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "total_matches")
        if type(self.entries) is not tuple or not all(
            type(item) is FreeCadCapabilityIndexEntry for item in self.entries
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "entries")
        if self.offset % self.page_size != 0 or self.offset > self.total_matches:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "entries")
        expected_count = min(self.page_size, self.total_matches - self.offset)
        if len(self.entries) != expected_count:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "entries")
        if self.entries != tuple(sorted(self.entries, key=lambda item: item.native_type_id)) or len(
            {item.native_type_id for item in self.entries}
        ) != len(self.entries):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "entries")
        has_more = self.offset + len(self.entries) < self.total_matches
        if has_more != (self.next_cursor is not None):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "next_cursor")
        if self.next_cursor is not None and _decode_cursor(
            self.next_cursor,
            runtime_binding_sha256=self.runtime_binding_sha256,
            query_sha256=self.query_sha256,
            page_size=self.page_size,
        ) != self.offset + len(self.entries):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "next_cursor")
        _canonical(
            self._mapping(),
            maximum=MAX_FREECAD_CAPABILITY_QUERY_PAGE_BYTES,
        )

    def _mapping(self) -> dict[str, object]:
        return {
            "entries": [item._mapping() for item in self.entries],
            "next_cursor": self.next_cursor,
            "offset": self.offset,
            "page_size": self.page_size,
            "query_sha256": self.query_sha256,
            "runtime_binding_sha256": self.runtime_binding_sha256,
            "schema_version": self.schema_version,
            "total_matches": self.total_matches,
        }

    @property
    def page_sha256(self) -> str:
        return hashlib.sha256(
            _QUERY_PAGE_DIGEST_DOMAIN
            + _canonical(
                self._mapping(),
                maximum=MAX_FREECAD_CAPABILITY_QUERY_PAGE_BYTES,
            )
        ).hexdigest()


def encode_freecad_capability_query_page_v2(
    value: FreeCadCapabilityQueryPageV2,
) -> bytes:
    if type(value) is not FreeCadCapabilityQueryPageV2:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "page")
    return _canonical(
        {**value._mapping(), "page_sha256": value.page_sha256},
        maximum=MAX_FREECAD_CAPABILITY_QUERY_PAGE_BYTES,
    )


def query_freecad_capability_runtime_v2(
    runtime: FreeCadCapabilityRuntimeV2,
    *,
    module: str | None = None,
    semantic_kind: FreeCadCapabilitySemanticKind | None = None,
    minimum_status: CapabilitySupportStatus = CapabilitySupportStatus.DISCOVERED,
    page_size: int = 64,
    cursor: str | None = None,
) -> FreeCadCapabilityQueryPageV2:
    """Query native TypeId entries with a projection-bound opaque cursor."""

    if type(runtime) is not FreeCadCapabilityRuntimeV2:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "runtime")
    if module is not None:
        _identifier(module, "module")
        known_modules = {key for key, _native_ids in runtime.projection.manifest.module_index}
        if module not in known_modules:
            _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "module")
    if semantic_kind is not None and type(semantic_kind) is not FreeCadCapabilitySemanticKind:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "semantic_kind")
    if type(minimum_status) is not CapabilitySupportStatus:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "minimum_status")
    if type(page_size) is not int or not 1 <= page_size <= MAX_FREECAD_CAPABILITY_QUERY_PAGE_SIZE:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "page_size")
    binding_sha256 = runtime.binding.binding_sha256
    query_body = _query_mapping(
        runtime_binding_sha256=binding_sha256,
        module=module,
        semantic_kind=semantic_kind,
        minimum_status=minimum_status,
        page_size=page_size,
    )
    query_sha256 = _query_sha256(query_body)
    matches = tuple(
        item
        for item in runtime.projection.manifest.entries
        if (module is None or item.declaring_module == module)
        and (semantic_kind is None or item.semantic_kind is semantic_kind)
        and item.status.rank >= minimum_status.rank
    )
    offset = 0
    if cursor is not None:
        offset = _decode_cursor(
            cursor,
            runtime_binding_sha256=binding_sha256,
            query_sha256=query_sha256,
            page_size=page_size,
        )
        if offset >= len(matches) or offset % page_size != 0:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "cursor/offset")
    entries = matches[offset : offset + page_size]
    next_offset = offset + len(entries)
    next_cursor = (
        _encode_cursor(
            runtime_binding_sha256=binding_sha256,
            query_sha256=query_sha256,
            page_size=page_size,
            offset=next_offset,
        )
        if next_offset < len(matches)
        else None
    )
    return FreeCadCapabilityQueryPageV2(
        schema_version=FREECAD_CAPABILITY_QUERY_V2_SCHEMA_VERSION,
        runtime_binding_sha256=binding_sha256,
        query_sha256=query_sha256,
        offset=offset,
        page_size=page_size,
        total_matches=len(matches),
        entries=entries,
        next_cursor=next_cursor,
    )


__all__ = ()
