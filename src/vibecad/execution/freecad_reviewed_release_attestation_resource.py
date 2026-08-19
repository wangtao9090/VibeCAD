"""Read one platform-indexed reviewed-FreeCAD attestation from package data.

This is deliberately a narrow package-data boundary.  It does not execute
FreeCAD, accept a file path or environment override, decode a user cache, or
grant a capability.  The v2 attestation codec owns semantic validation; this
module establishes that its exact canonical bytes are present in the installed
package, pinned for this VibeCAD release and the trusted current platform, and
self-identify with both bindings.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.resources
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from vibecad import __version__
from vibecad.execution._attestations.freecad_reviewed_release_attestation_pins import (
    PACKAGED_FREECAD_REVIEWED_RELEASE_ATTESTATION_SHA256_BY_RELEASE_PLATFORM,
)
from vibecad.execution.capabilities import CapabilityCatalogError, CapabilityCatalogErrorCode
from vibecad.execution.freecad_discovery_runtime_v2 import _platform_id

FREECAD_REVIEWED_RELEASE_ATTESTATION_RESOURCE_PACKAGE: Final = "vibecad.execution._attestations"
FREECAD_REVIEWED_RELEASE_ATTESTATION_RESOURCE_NAME_BY_PLATFORM_ID: Final = MappingProxyType(
    {
        "macos.arm64": "freecad-reviewed-release-attestation-macos-arm64-v1.json",
        "macos.x86_64": "freecad-reviewed-release-attestation-macos-x86_64-v1.json",
    }
)
MAX_FREECAD_REVIEWED_RELEASE_ATTESTATION_RESOURCE_BYTES: Final = 2 * 1024 * 1024

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[A-Za-z0-9.+-]{0,64})?$")
_OUTER_KEYS = {
    "schema_version",
    "release_version",
    "runtime_backend",
    "discovery_snapshot_sha256",
    "discovery_manifest_sha256",
    "verification_set",
    "attestation_sha256",
}


def _fail(code: CapabilityCatalogErrorCode, path: str) -> None:
    raise CapabilityCatalogError(code, path)


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if type(key) is not str or key in result:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "package_attestation/json")
        result[key] = value
    return result


def _constant(_value: str) -> object:
    _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "package_attestation/json")


def _canonical(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "package_attestation/json")
    if not raw or len(raw) > MAX_FREECAD_REVIEWED_RELEASE_ATTESTATION_RESOURCE_BYTES:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "package_attestation/json")
    return raw


def _decode_outer_header(raw: object) -> tuple[str, str, str]:
    if type(raw) is not bytes or not raw:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "package_attestation/resource")
    if len(raw) > MAX_FREECAD_REVIEWED_RELEASE_ATTESTATION_RESOURCE_BYTES:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "package_attestation/resource")
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
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "package_attestation/json")
    if type(value) is not dict or set(value) != _OUTER_KEYS or _canonical(value) != raw:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "package_attestation/canonical")
    release_version = value["release_version"]
    attestation_sha256 = value["attestation_sha256"]
    runtime_backend = value["runtime_backend"]
    runtime_platform_id = (
        runtime_backend.get("platform_id") if type(runtime_backend) is dict else None
    )
    if (
        type(release_version) is not str
        or _RELEASE_VERSION.fullmatch(release_version) is None
        or type(attestation_sha256) is not str
        or _DIGEST.fullmatch(attestation_sha256) is None
        or type(runtime_platform_id) is not str
        or runtime_platform_id
        not in FREECAD_REVIEWED_RELEASE_ATTESTATION_RESOURCE_NAME_BY_PLATFORM_ID
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "package_attestation/header")
    return release_version, attestation_sha256, runtime_platform_id


def _read_packaged_resource_bytes(resource_name: object) -> bytes:
    """Read one allowlisted package resource selected by trusted platform state."""

    if (
        type(resource_name) is not str
        or resource_name
        not in FREECAD_REVIEWED_RELEASE_ATTESTATION_RESOURCE_NAME_BY_PLATFORM_ID.values()
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "package_attestation/resource_name")

    try:
        raw = (
            importlib.resources.files(FREECAD_REVIEWED_RELEASE_ATTESTATION_RESOURCE_PACKAGE)
            .joinpath(resource_name)
            .read_bytes()
        )
    except (AttributeError, FileNotFoundError, IsADirectoryError, ModuleNotFoundError, OSError):
        _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "package_attestation/resource")
    if type(raw) is not bytes:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "package_attestation/resource")
    return raw


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadPackagedReviewedReleaseAttestation:
    """Opaque canonical resource bytes bound to the installed source release."""

    release_version: str
    attestation_sha256: str
    resource_sha256: str
    raw: bytes

    def __post_init__(self) -> None:
        if (
            type(self.release_version) is not str
            or _RELEASE_VERSION.fullmatch(self.release_version) is None
            or type(self.attestation_sha256) is not str
            or _DIGEST.fullmatch(self.attestation_sha256) is None
            or type(self.resource_sha256) is not str
            or _DIGEST.fullmatch(self.resource_sha256) is None
            or type(self.raw) is not bytes
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "package_attestation")
        header_release, header_attestation, _header_platform = _decode_outer_header(self.raw)
        expected_resource = hashlib.sha256(self.raw).hexdigest()
        if (
            header_release != self.release_version
            or not hmac.compare_digest(header_attestation, self.attestation_sha256)
            or not hmac.compare_digest(expected_resource, self.resource_sha256)
        ):
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "package_attestation")


def load_current_packaged_freecad_reviewed_release_attestation() -> (
    FreeCadPackagedReviewedReleaseAttestation
):
    """Load the current release's pinned canonical attestation resource.

    The absence of a generated pin/resource is a closed failure.  Callers
    must never substitute a cache, test artifact, path, or dynamically
    reverified receipt for this release-bound evidence.
    """

    runtime_platform_id = _platform_id()
    resource_name = FREECAD_REVIEWED_RELEASE_ATTESTATION_RESOURCE_NAME_BY_PLATFORM_ID.get(
        runtime_platform_id
    )
    if type(resource_name) is not str:
        _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "package_attestation/platform")
    expected_resource_sha256 = (
        PACKAGED_FREECAD_REVIEWED_RELEASE_ATTESTATION_SHA256_BY_RELEASE_PLATFORM.get(
            (__version__, runtime_platform_id)
        )
    )
    if (
        type(expected_resource_sha256) is not str
        or _DIGEST.fullmatch(expected_resource_sha256) is None
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_STATUS, "package_attestation/pin")
    raw = _read_packaged_resource_bytes(resource_name)
    release_version, attestation_sha256, resource_platform_id = _decode_outer_header(raw)
    resource_sha256 = hashlib.sha256(raw).hexdigest()
    if (
        not hmac.compare_digest(resource_sha256, expected_resource_sha256)
        or release_version != __version__
        or resource_platform_id != runtime_platform_id
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "package_attestation/binding")
    return FreeCadPackagedReviewedReleaseAttestation(
        release_version=release_version,
        attestation_sha256=attestation_sha256,
        resource_sha256=resource_sha256,
        raw=raw,
    )


__all__ = ()
