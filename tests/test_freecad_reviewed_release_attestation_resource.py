"""Package-data boundary tests for release-bound reviewed attestation bytes."""

from __future__ import annotations

import hashlib
import json

import pytest

import vibecad.execution.freecad_reviewed_release_attestation_resource as resource
import vibecad.execution.freecad_reviewed_verification_runtime as verification_runtime
from vibecad import __version__
from vibecad.execution._attestations.freecad_reviewed_release_attestation_pins import (
    PACKAGED_FREECAD_REVIEWED_RELEASE_ATTESTATION_SHA256_BY_RELEASE_PLATFORM,
)
from vibecad.execution.capabilities import CapabilityCatalogError, CapabilityCatalogErrorCode
from vibecad.execution.freecad_reviewed_release_attestation import (
    decode_freecad_reviewed_release_attestation,
)


def _raw(
    *,
    release_version: str = "0.10.1",
    platform_id: str = "macos.x86_64",
) -> bytes:
    return json.dumps(
        {
            "attestation_sha256": "a" * 64,
            "discovery_manifest_sha256": "b" * 64,
            "discovery_snapshot_sha256": "c" * 64,
            "release_version": release_version,
            "runtime_backend": {"platform_id": platform_id},
            "schema_version": 1,
            "verification_set": {},
        },
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _pin(
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
    *,
    platform_id: str = "macos.x86_64",
) -> None:
    monkeypatch.setattr(
        resource,
        "PACKAGED_FREECAD_REVIEWED_RELEASE_ATTESTATION_SHA256_BY_RELEASE_PLATFORM",
        {("0.10.1", platform_id): hashlib.sha256(raw).hexdigest()},
    )


def _noncanonical_raw() -> bytes:
    return json.dumps(json.loads(_raw()), ensure_ascii=True, sort_keys=True).encode("ascii")


def test_loads_only_canonical_pinned_bytes_for_the_installed_release(monkeypatch):
    raw = _raw()
    _pin(monkeypatch, raw)
    selected_names: list[str] = []
    monkeypatch.setattr(resource, "_platform_id", lambda: "macos.x86_64")
    monkeypatch.setattr(
        resource,
        "_read_packaged_resource_bytes",
        lambda name: selected_names.append(name) or raw,
    )

    loaded = resource.load_current_packaged_freecad_reviewed_release_attestation()

    assert loaded.release_version == "0.10.1"
    assert loaded.attestation_sha256 == "a" * 64
    assert loaded.resource_sha256 == hashlib.sha256(raw).hexdigest()
    assert loaded.raw == raw
    assert selected_names == ["freecad-reviewed-release-attestation-macos-x86_64-v1.json"]


def test_trusted_arm_platform_selects_only_the_fixed_arm_resource(monkeypatch):
    raw = _raw(platform_id="macos.arm64")
    _pin(monkeypatch, raw, platform_id="macos.arm64")
    selected_names: list[str] = []
    monkeypatch.setattr(resource, "_platform_id", lambda: "macos.arm64")
    monkeypatch.setattr(
        resource,
        "_read_packaged_resource_bytes",
        lambda name: selected_names.append(name) or raw,
    )

    loaded = resource.load_current_packaged_freecad_reviewed_release_attestation()

    assert loaded.raw == raw
    assert selected_names == ["freecad-reviewed-release-attestation-macos-arm64-v1.json"]


def test_candidate_platform_cannot_select_or_cross_bind_a_sibling_resource(monkeypatch):
    candidate = _raw(platform_id="macos.arm64")
    _pin(monkeypatch, candidate, platform_id="macos.x86_64")
    selected_names: list[str] = []
    monkeypatch.setattr(resource, "_platform_id", lambda: "macos.x86_64")
    monkeypatch.setattr(
        resource,
        "_read_packaged_resource_bytes",
        lambda name: selected_names.append(name) or candidate,
    )

    with pytest.raises(CapabilityCatalogError) as raised:
        resource.load_current_packaged_freecad_reviewed_release_attestation()

    assert raised.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    assert raised.value.path == "package_attestation/binding"
    assert selected_names == ["freecad-reviewed-release-attestation-macos-x86_64-v1.json"]


def test_unsupported_current_platform_fails_before_pin_or_resource_lookup(monkeypatch):
    monkeypatch.setattr(resource, "_platform_id", lambda: "linux.x86_64")
    monkeypatch.setattr(
        resource,
        "PACKAGED_FREECAD_REVIEWED_RELEASE_ATTESTATION_SHA256_BY_RELEASE_PLATFORM",
        pytest.fail,
    )
    monkeypatch.setattr(
        resource,
        "_read_packaged_resource_bytes",
        lambda _name: pytest.fail("must not read for an unsupported platform"),
    )

    with pytest.raises(CapabilityCatalogError) as raised:
        resource.load_current_packaged_freecad_reviewed_release_attestation()

    assert raised.value.code is CapabilityCatalogErrorCode.INVALID_STATUS
    assert raised.value.path == "package_attestation/platform"


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
            _raw(release_version="0.10.2"),
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
    monkeypatch.setattr(resource, "_platform_id", lambda: "macos.x86_64")
    monkeypatch.setattr(resource, "_read_packaged_resource_bytes", lambda _name: raw)

    with pytest.raises(CapabilityCatalogError) as raised:
        resource.load_current_packaged_freecad_reviewed_release_attestation()

    assert raised.value.code is code
    assert raised.value.path == path


def test_rejects_missing_release_pin_before_reading_a_resource(monkeypatch):
    monkeypatch.setattr(
        resource,
        "PACKAGED_FREECAD_REVIEWED_RELEASE_ATTESTATION_SHA256_BY_RELEASE_PLATFORM",
        {},
    )
    monkeypatch.setattr(resource, "_platform_id", lambda: "macos.arm64")
    monkeypatch.setattr(
        resource,
        "_read_packaged_resource_bytes",
        lambda _name: pytest.fail("must not read an unpinned resource or fall back to x86"),
    )

    with pytest.raises(CapabilityCatalogError) as raised:
        resource.load_current_packaged_freecad_reviewed_release_attestation()

    assert raised.value.code is CapabilityCatalogErrorCode.INVALID_STATUS
    assert raised.value.path == "package_attestation/pin"


def test_windows_platform_has_a_fixed_resource_and_fails_closed_without_its_pin(monkeypatch):
    monkeypatch.setattr(
        resource,
        "PACKAGED_FREECAD_REVIEWED_RELEASE_ATTESTATION_SHA256_BY_RELEASE_PLATFORM",
        {},
    )
    monkeypatch.setattr(resource, "_platform_id", lambda: "windows.x86_64")
    monkeypatch.setattr(
        resource,
        "_read_packaged_resource_bytes",
        lambda _name: pytest.fail("must not read an unpinned Windows resource"),
    )

    with pytest.raises(CapabilityCatalogError) as raised:
        resource.load_current_packaged_freecad_reviewed_release_attestation()

    assert raised.value.code is CapabilityCatalogErrorCode.INVALID_STATUS
    assert raised.value.path == "package_attestation/pin"
    assert (
        resource.FREECAD_REVIEWED_RELEASE_ATTESTATION_RESOURCE_NAME_BY_PLATFORM_ID["windows.x86_64"]
        == "freecad-reviewed-release-attestation-windows-x86_64-v1.json"
    )


def test_rejects_a_missing_fixed_package_resource(monkeypatch):
    raw = _raw()
    _pin(monkeypatch, raw)
    monkeypatch.setattr(resource, "_platform_id", lambda: "macos.x86_64")

    def missing(_resource_name: str) -> bytes:
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


def test_checked_in_resources_track_the_cross_platform_catalog126_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resource, "_platform_id", lambda: "macos.x86_64")
    loaded_x86 = resource.load_current_packaged_freecad_reviewed_release_attestation()
    monkeypatch.setattr(resource, "_platform_id", lambda: "macos.arm64")
    loaded_arm = resource.load_current_packaged_freecad_reviewed_release_attestation()
    monkeypatch.setattr(resource, "_platform_id", lambda: "windows.x86_64")
    loaded_windows = resource.load_current_packaged_freecad_reviewed_release_attestation()

    assert (
        loaded_x86.release_version
        == loaded_arm.release_version
        == loaded_windows.release_version
        == __version__
        == "0.10.1"
    )
    assert PACKAGED_FREECAD_REVIEWED_RELEASE_ATTESTATION_SHA256_BY_RELEASE_PLATFORM == {
        (__version__, "macos.arm64"): loaded_arm.resource_sha256,
        (__version__, "macos.x86_64"): loaded_x86.resource_sha256,
        (__version__, "windows.x86_64"): loaded_windows.resource_sha256,
    }
    x86_raw = json.loads(loaded_x86.raw)
    assert x86_raw["runtime_backend"]["platform_id"] == "macos.x86_64"
    assert len(x86_raw["verification_set"]["receipts"]) == 21
    assert len(x86_raw["verification_set"]["formal_operations"]) == 126
    assert len(x86_raw["verification_set"]["native_types"]) == 102
    assert (
        x86_raw["verification_set"]["current_formal_catalog_sha256"]
        == (
            verification_runtime._current_catalogs()[2]  # noqa: SLF001
        )
    )
    decode_freecad_reviewed_release_attestation(
        loaded_x86.raw,
        expected_source_attestation_sha256=loaded_x86.resource_sha256,
    )

    arm_raw = json.loads(loaded_arm.raw)
    assert arm_raw["runtime_backend"]["platform_id"] == "macos.arm64"
    assert len(arm_raw["verification_set"]["receipts"]) == 21
    assert len(arm_raw["verification_set"]["formal_operations"]) == 126
    assert len(arm_raw["verification_set"]["native_types"]) == 102
    assert (
        arm_raw["verification_set"]["current_formal_catalog_sha256"]
        == (
            verification_runtime._current_catalogs()[2]  # noqa: SLF001
        )
    )
    decode_freecad_reviewed_release_attestation(
        loaded_arm.raw,
        expected_source_attestation_sha256=loaded_arm.resource_sha256,
    )

    windows_raw = json.loads(loaded_windows.raw)
    assert windows_raw["runtime_backend"]["platform_id"] == "windows.x86_64"
    assert len(windows_raw["verification_set"]["receipts"]) == 21
    assert len(windows_raw["verification_set"]["formal_operations"]) == 126
    assert len(windows_raw["verification_set"]["native_types"]) == 102
    assert (
        windows_raw["verification_set"]["current_formal_catalog_sha256"]
        == (
            verification_runtime._current_catalogs()[2]  # noqa: SLF001
        )
    )
    decode_freecad_reviewed_release_attestation(
        loaded_windows.raw,
        expected_source_attestation_sha256=loaded_windows.resource_sha256,
    )

    assert (
        [
            (operation["operation_id"], operation["formal_spec_sha256"])
            for operation in x86_raw["verification_set"]["formal_operations"]
        ]
        == [
            (operation["operation_id"], operation["formal_spec_sha256"])
            for operation in arm_raw["verification_set"]["formal_operations"]
        ]
        == [
            (operation["operation_id"], operation["formal_spec_sha256"])
            for operation in windows_raw["verification_set"]["formal_operations"]
        ]
    )
    assert (
        [
            (native["native_type_id"], native["formal_operation_ids"])
            for native in x86_raw["verification_set"]["native_types"]
        ]
        == [
            (native["native_type_id"], native["formal_operation_ids"])
            for native in arm_raw["verification_set"]["native_types"]
        ]
        == [
            (native["native_type_id"], native["formal_operation_ids"])
            for native in windows_raw["verification_set"]["native_types"]
        ]
    )
