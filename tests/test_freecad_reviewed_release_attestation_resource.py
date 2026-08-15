"""Package-data boundary tests for release-bound reviewed attestation bytes."""

from __future__ import annotations

import hashlib
import json

import pytest

import vibecad.execution.freecad_reviewed_release_attestation_resource as resource
from vibecad import __version__
from vibecad.execution._attestations.freecad_reviewed_release_attestation_pins import (
    PACKAGED_FREECAD_REVIEWED_RELEASE_ATTESTATION_SHA256_BY_RELEASE,
)
from vibecad.execution.capabilities import CapabilityCatalogError, CapabilityCatalogErrorCode
from vibecad.execution.freecad_reviewed_release_attestation import (
    decode_freecad_reviewed_release_attestation,
)


def _raw(*, release_version: str = "0.10.0") -> bytes:
    return json.dumps(
        {
            "attestation_sha256": "a" * 64,
            "discovery_manifest_sha256": "b" * 64,
            "discovery_snapshot_sha256": "c" * 64,
            "release_version": release_version,
            "runtime_backend": {},
            "schema_version": 1,
            "verification_set": {},
        },
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _pin(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> None:
    monkeypatch.setattr(
        resource,
        "PACKAGED_FREECAD_REVIEWED_RELEASE_ATTESTATION_SHA256_BY_RELEASE",
        {"0.10.0": hashlib.sha256(raw).hexdigest()},
    )


def _noncanonical_raw() -> bytes:
    return json.dumps(json.loads(_raw()), ensure_ascii=True, sort_keys=True).encode("ascii")


def test_loads_only_canonical_pinned_bytes_for_the_installed_release(monkeypatch):
    raw = _raw()
    _pin(monkeypatch, raw)
    monkeypatch.setattr(resource, "_read_packaged_resource_bytes", lambda: raw)

    loaded = resource.load_current_packaged_freecad_reviewed_release_attestation()

    assert loaded.release_version == "0.10.0"
    assert loaded.attestation_sha256 == "a" * 64
    assert loaded.resource_sha256 == hashlib.sha256(raw).hexdigest()
    assert loaded.raw == raw


@pytest.mark.parametrize(
    ("raw", "pin", "path", "code"),
    [
        (
            _noncanonical_raw(),
            True,
            "package_attestation/canonical",
            CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
        ),
        (
            _raw(release_version="0.10.1"),
            True,
            "package_attestation/binding",
            CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
        ),
        (
            _raw(),
            False,
            "package_attestation/binding",
            CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
        ),
    ],
)
def test_rejects_noncanonical_tampered_and_release_drifted_resources(
    monkeypatch, raw, pin, path, code
):
    _pin(monkeypatch, _raw() if pin else b"tampered")
    monkeypatch.setattr(resource, "_read_packaged_resource_bytes", lambda: raw)

    with pytest.raises(CapabilityCatalogError) as raised:
        resource.load_current_packaged_freecad_reviewed_release_attestation()

    assert raised.value.code is code
    assert raised.value.path == path


def test_rejects_missing_release_pin_before_reading_a_resource(monkeypatch):
    monkeypatch.setattr(
        resource,
        "PACKAGED_FREECAD_REVIEWED_RELEASE_ATTESTATION_SHA256_BY_RELEASE",
        {},
    )
    monkeypatch.setattr(
        resource,
        "_read_packaged_resource_bytes",
        lambda: pytest.fail("must not read an unpinned resource"),
    )

    with pytest.raises(CapabilityCatalogError) as raised:
        resource.load_current_packaged_freecad_reviewed_release_attestation()

    assert raised.value.code is CapabilityCatalogErrorCode.INVALID_STATUS
    assert raised.value.path == "package_attestation/pin"


def test_rejects_a_missing_fixed_package_resource(monkeypatch):
    raw = _raw()
    _pin(monkeypatch, raw)

    def missing() -> bytes:
        raise CapabilityCatalogError(
            CapabilityCatalogErrorCode.UNKNOWN_REFERENCE,
            "package_attestation/resource",
        )

    monkeypatch.setattr(resource, "_read_packaged_resource_bytes", missing)
    with pytest.raises(CapabilityCatalogError) as raised:
        resource.load_current_packaged_freecad_reviewed_release_attestation()
    assert raised.value.code is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE
    assert raised.value.path == "package_attestation/resource"


def test_loader_has_no_path_or_environment_override_surface():
    assert "os.environ" not in resource.__doc__
    loader = resource.load_current_packaged_freecad_reviewed_release_attestation
    assert loader.__code__.co_argcount == 0


def test_checked_in_current_release_resource_is_pinned_canonical_and_complete() -> None:
    loaded = resource.load_current_packaged_freecad_reviewed_release_attestation()

    assert loaded.release_version == __version__ == "0.10.0"
    assert PACKAGED_FREECAD_REVIEWED_RELEASE_ATTESTATION_SHA256_BY_RELEASE == {
        __version__: loaded.resource_sha256
    }
    decoded = decode_freecad_reviewed_release_attestation(
        loaded.raw,
        expected_source_attestation_sha256=loaded.resource_sha256,
    )
    assert decoded.release_version == __version__
    assert decoded.runtime_backend.platform_id == "macos.x86_64"
    assert len(decoded.verification_set.receipts) == 19
    assert len(decoded.verification_set.formal_operations) == 124
    assert len(decoded.verification_set.native_types) == 102
