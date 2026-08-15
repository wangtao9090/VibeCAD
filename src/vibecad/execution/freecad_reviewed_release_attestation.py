"""Opaque, build-bound release attestations for Reviewed FreeCAD support.

The full mutating conformance matrix is a maintainer/CI activity.  Runtime
consumers load this bounded canonical artifact, authenticate its raw bytes
against a source-pinned package-resource SHA-256, validate it against the
exact managed FreeCAD discovery and current formal catalogs, and only then
derive VERIFIED promotion metadata from its opaque verification set.

Neither this artifact nor Python-private builder tokens grant authority.  A
directly built object is intentionally not runtime-validatable.  The sole
runtime source boundary is strict decoding with a digest that the caller got
from trusted source code, never one calculated from the candidate bytes.  The
artifact has no callbacks and is not a runtime persistence format.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Final

from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
)
from vibecad.execution.freecad_reviewed_verification_runtime import (
    MAX_FREECAD_REVIEWED_VERIFICATION_SET_BYTES,
    FreeCadManagedReviewedVerificationSet,
    decode_freecad_managed_reviewed_verification_set,
    encode_freecad_managed_reviewed_verification_set,
    validate_managed_reviewed_verification_set,
)

FREECAD_REVIEWED_RELEASE_ATTESTATION_SCHEMA_VERSION: Final = 1
MAX_FREECAD_REVIEWED_RELEASE_ATTESTATION_BYTES: Final = (
    MAX_FREECAD_REVIEWED_VERIFICATION_SET_BYTES + 64 * 1024
)

_ATTESTATION_DIGEST_DOMAIN = b"vibecad-freecad-reviewed-release-attestation-v1\0"
_ATTESTATION_BUILDER_TOKEN = object()
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _fail(code: CapabilityCatalogErrorCode, path: str) -> None:
    raise CapabilityCatalogError(code, path)


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
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "release_attestation")
    if not raw:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "release_attestation")
    if len(raw) > maximum:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "release_attestation")
    return raw


def _exact(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _version(value: object, path: str) -> str:
    if type(value) is not str or _VERSION.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _backend_mapping(value: CapabilityBackend) -> dict[str, object]:
    return {
        "backend_id": value.backend_id,
        "backend_version": list(value.backend_version),
        "build_fingerprint_sha256": value.build_fingerprint_sha256,
        "discovery_profile": value.discovery_profile.value,
        "platform_id": value.platform_id,
    }


def _backend_from_mapping(value: object) -> CapabilityBackend:
    item = _exact(
        value,
        {
            "backend_id",
            "backend_version",
            "build_fingerprint_sha256",
            "discovery_profile",
            "platform_id",
        },
        "release_attestation/runtime_backend",
    )
    raw_version = item["backend_version"]
    if type(raw_version) is not list:
        _fail(
            CapabilityCatalogErrorCode.INVALID_INPUT,
            "release_attestation/runtime_backend/backend_version",
        )
    try:
        profile = CapabilityExecutionProfile(item["discovery_profile"])
    except (TypeError, ValueError):
        _fail(
            CapabilityCatalogErrorCode.INVALID_INPUT,
            "release_attestation/runtime_backend/discovery_profile",
        )
    return CapabilityBackend(
        backend_id=item["backend_id"],
        backend_version=tuple(raw_version),
        build_fingerprint_sha256=item["build_fingerprint_sha256"],
        discovery_profile=profile,
        platform_id=item["platform_id"],
    )


def _decode_canonical_mapping(raw: object) -> dict[str, object]:
    if type(raw) is not bytes:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "release_attestation")
    if not raw:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "release_attestation")
    if len(raw) > MAX_FREECAD_REVIEWED_RELEASE_ATTESTATION_BYTES:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "release_attestation")

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        raise ValueError("non-finite number")

    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "release_attestation")
    if (
        type(value) is not dict
        or _canonical(
            value,
            maximum=MAX_FREECAD_REVIEWED_RELEASE_ATTESTATION_BYTES,
        )
        != raw
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "release_attestation")
    return value


def _verification_set_mapping(value: FreeCadManagedReviewedVerificationSet) -> dict[str, object]:
    try:
        decoded = json.loads(encode_freecad_managed_reviewed_verification_set(value))
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "release_attestation/verification_set")
    if type(decoded) is not dict:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "release_attestation/verification_set")
    return decoded


@dataclass(frozen=True, slots=True, init=False)
class FreeCadReviewedReleaseAttestation:
    """One canonical release assertion over a complete opaque v2 set.

    Maintainer-built instances are source-unauthenticated.  Only strict
    decoding with a caller-supplied, source-pinned raw digest makes an
    instance eligible for runtime validation.
    """

    schema_version: int
    release_version: str
    runtime_backend: CapabilityBackend
    discovery_snapshot_sha256: str
    discovery_manifest_sha256: str
    verification_set: FreeCadManagedReviewedVerificationSet
    _builder_token: object = field(repr=False, compare=False)
    _source_attestation_sha256: str | None = field(repr=False, compare=False)
    attestation_sha256: str = field(init=False)

    @classmethod
    def _create(
        cls,
        *,
        schema_version: int,
        release_version: str,
        runtime_backend: CapabilityBackend,
        discovery_snapshot_sha256: str,
        discovery_manifest_sha256: str,
        verification_set: FreeCadManagedReviewedVerificationSet,
        source_attestation_sha256: str | None,
        builder_token: object,
    ) -> FreeCadReviewedReleaseAttestation:
        if builder_token is not _ATTESTATION_BUILDER_TOKEN:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "release_attestation/builder")
        value = object.__new__(cls)
        for name, field_value in (
            ("schema_version", schema_version),
            ("release_version", release_version),
            ("runtime_backend", runtime_backend),
            ("discovery_snapshot_sha256", discovery_snapshot_sha256),
            ("discovery_manifest_sha256", discovery_manifest_sha256),
            ("verification_set", verification_set),
            ("_builder_token", _ATTESTATION_BUILDER_TOKEN),
            ("_source_attestation_sha256", source_attestation_sha256),
        ):
            object.__setattr__(value, name, field_value)
        value.__post_init__()
        return value

    def __post_init__(self) -> None:
        if self._builder_token is not _ATTESTATION_BUILDER_TOKEN:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "release_attestation/builder")
        if (
            type(self.schema_version) is not int
            or self.schema_version != FREECAD_REVIEWED_RELEASE_ATTESTATION_SCHEMA_VERSION
        ):
            _fail(
                CapabilityCatalogErrorCode.UNSUPPORTED_VERSION,
                "release_attestation/schema_version",
            )
        _version(self.release_version, "release_attestation/release_version")
        if type(self.runtime_backend) is not CapabilityBackend:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "release_attestation/runtime_backend")
        if (
            self.runtime_backend.backend_id != "freecad"
            or self.runtime_backend.discovery_profile is not CapabilityExecutionProfile.HEADLESS
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "release_attestation/runtime_backend")
        _digest(
            self.discovery_snapshot_sha256,
            "release_attestation/discovery_snapshot_sha256",
        )
        _digest(
            self.discovery_manifest_sha256,
            "release_attestation/discovery_manifest_sha256",
        )
        if self._source_attestation_sha256 is not None:
            _digest(
                self._source_attestation_sha256,
                "release_attestation/source_attestation_sha256",
            )
        validate_managed_reviewed_verification_set(
            self.verification_set,
            runtime_backend=self.runtime_backend,
            require_complete=True,
        )
        object.__setattr__(self, "attestation_sha256", self._expected_sha256())

    def _mapping(self) -> dict[str, object]:
        return {
            "discovery_manifest_sha256": self.discovery_manifest_sha256,
            "discovery_snapshot_sha256": self.discovery_snapshot_sha256,
            "release_version": self.release_version,
            "runtime_backend": _backend_mapping(self.runtime_backend),
            "schema_version": self.schema_version,
            "verification_set": _verification_set_mapping(self.verification_set),
        }

    def _expected_sha256(self) -> str:
        return hashlib.sha256(
            _ATTESTATION_DIGEST_DOMAIN
            + _canonical(
                self._mapping(),
                maximum=MAX_FREECAD_REVIEWED_RELEASE_ATTESTATION_BYTES,
            )
        ).hexdigest()


def build_freecad_reviewed_release_attestation(
    *,
    release_version: str,
    runtime_backend: CapabilityBackend,
    discovery_snapshot_sha256: str,
    discovery_manifest_sha256: str,
    verification_set: FreeCadManagedReviewedVerificationSet,
) -> FreeCadReviewedReleaseAttestation:
    """Build one complete maintainer artifact without runtime source authority."""

    return FreeCadReviewedReleaseAttestation._create(
        schema_version=FREECAD_REVIEWED_RELEASE_ATTESTATION_SCHEMA_VERSION,
        release_version=release_version,
        runtime_backend=runtime_backend,
        discovery_snapshot_sha256=discovery_snapshot_sha256,
        discovery_manifest_sha256=discovery_manifest_sha256,
        verification_set=verification_set,
        source_attestation_sha256=None,
        builder_token=_ATTESTATION_BUILDER_TOKEN,
    )


def validate_freecad_reviewed_release_attestation(
    value: object,
    *,
    expected_release_version: str,
    runtime_backend: CapabilityBackend,
    discovery_snapshot_sha256: str,
    discovery_manifest_sha256: str,
    expected_source_attestation_sha256: str,
) -> FreeCadReviewedReleaseAttestation:
    """Bind one source-authenticated artifact to the release and runtime.

    ``expected_source_attestation_sha256`` must be fixed in trusted package
    source.  Computing it from candidate bytes at runtime defeats this source
    boundary and is forbidden for consumers.
    """

    if type(value) is not FreeCadReviewedReleaseAttestation:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "release_attestation")
    _version(expected_release_version, "expected_release_version")
    if type(runtime_backend) is not CapabilityBackend:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "runtime_backend")
    _digest(discovery_snapshot_sha256, "discovery_snapshot_sha256")
    _digest(discovery_manifest_sha256, "discovery_manifest_sha256")
    _digest(
        expected_source_attestation_sha256,
        "expected_source_attestation_sha256",
    )
    if value._builder_token is not _ATTESTATION_BUILDER_TOKEN or not hmac.compare_digest(
        value.attestation_sha256,
        value._expected_sha256(),
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "release_attestation/digest")
    if value.release_version != expected_release_version:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "release_attestation/release_version")
    if value.runtime_backend != runtime_backend:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "release_attestation/runtime_backend")
    if value._source_attestation_sha256 is None or not hmac.compare_digest(
        value._source_attestation_sha256,
        expected_source_attestation_sha256,
    ):
        _fail(
            CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
            "release_attestation/source_attestation_sha256",
        )
    canonical_source_sha256 = hashlib.sha256(
        encode_freecad_reviewed_release_attestation(value)
    ).hexdigest()
    if not hmac.compare_digest(
        canonical_source_sha256,
        expected_source_attestation_sha256,
    ):
        _fail(
            CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
            "release_attestation/source_attestation_sha256",
        )
    if not hmac.compare_digest(
        value.discovery_snapshot_sha256,
        discovery_snapshot_sha256,
    ):
        _fail(
            CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
            "release_attestation/discovery_snapshot_sha256",
        )
    if not hmac.compare_digest(
        value.discovery_manifest_sha256,
        discovery_manifest_sha256,
    ):
        _fail(
            CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
            "release_attestation/discovery_manifest_sha256",
        )
    validate_managed_reviewed_verification_set(
        value.verification_set,
        runtime_backend=runtime_backend,
        require_complete=True,
    )
    return value


def encode_freecad_reviewed_release_attestation(value: object) -> bytes:
    """Encode one maintainer artifact without granting source authority."""

    if type(value) is not FreeCadReviewedReleaseAttestation:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "release_attestation")
    if value._builder_token is not _ATTESTATION_BUILDER_TOKEN or not hmac.compare_digest(
        value.attestation_sha256,
        value._expected_sha256(),
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "release_attestation/digest")
    validate_managed_reviewed_verification_set(
        value.verification_set,
        runtime_backend=value.runtime_backend,
        require_complete=True,
    )
    return _canonical(
        {**value._mapping(), "attestation_sha256": value.attestation_sha256},
        maximum=MAX_FREECAD_REVIEWED_RELEASE_ATTESTATION_BYTES,
    )


def decode_freecad_reviewed_release_attestation(
    raw: object,
    *,
    expected_source_attestation_sha256: str,
) -> FreeCadReviewedReleaseAttestation:
    """Strictly decode bytes authenticated by a source-pinned raw SHA-256."""

    expected_source_attestation_sha256 = _digest(
        expected_source_attestation_sha256,
        "expected_source_attestation_sha256",
    )
    if type(raw) is not bytes:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "release_attestation")
    if not raw:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "release_attestation")
    if len(raw) > MAX_FREECAD_REVIEWED_RELEASE_ATTESTATION_BYTES:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "release_attestation")
    if not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(),
        expected_source_attestation_sha256,
    ):
        _fail(
            CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
            "release_attestation/source_attestation_sha256",
        )

    root = _exact(
        _decode_canonical_mapping(raw),
        {
            "attestation_sha256",
            "discovery_manifest_sha256",
            "discovery_snapshot_sha256",
            "release_version",
            "runtime_backend",
            "schema_version",
            "verification_set",
        },
        "release_attestation",
    )
    verification_set_mapping = root["verification_set"]
    if type(verification_set_mapping) is not dict:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "release_attestation/verification_set")
    verification_set = decode_freecad_managed_reviewed_verification_set(
        _canonical(
            verification_set_mapping,
            maximum=MAX_FREECAD_REVIEWED_VERIFICATION_SET_BYTES,
        )
    )
    claimed_sha256 = _digest(
        root["attestation_sha256"],
        "release_attestation/attestation_sha256",
    )
    value = FreeCadReviewedReleaseAttestation._create(
        schema_version=root["schema_version"],
        release_version=root["release_version"],
        runtime_backend=_backend_from_mapping(root["runtime_backend"]),
        discovery_snapshot_sha256=root["discovery_snapshot_sha256"],
        discovery_manifest_sha256=root["discovery_manifest_sha256"],
        verification_set=verification_set,
        source_attestation_sha256=expected_source_attestation_sha256,
        builder_token=_ATTESTATION_BUILDER_TOKEN,
    )
    if not hmac.compare_digest(claimed_sha256, value.attestation_sha256):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "release_attestation/digest")
    if encode_freecad_reviewed_release_attestation(value) != raw:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "release_attestation/canonical")
    return value


__all__ = ()
