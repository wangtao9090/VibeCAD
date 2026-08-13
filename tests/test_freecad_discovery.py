"""Read-only TypeId collector tests with a strict fake FreeCAD module."""

from __future__ import annotations

import hashlib

import pytest

from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
)
from vibecad.execution.freecad_capabilities import (
    MAX_FREECAD_REGISTERED_TYPES,
    FreeCadNativeTypeCategory,
    build_freecad_type_catalog,
)
from vibecad.execution.freecad_discovery import collect_loaded_freecad_type_snapshot


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


class _Type:
    def __init__(self, name: str, module: str, parent: _Type | None = None, *, bad=False):
        self.Name = name
        self.Module = module
        self._parent = parent
        self._bad = bad

    def isBad(self):
        return self._bad

    def getParent(self):
        return self._parent


class _Registry:
    def __init__(self, values: tuple[_Type, ...], derived: dict[str, tuple[_Type, ...]]):
        self._values = values
        self._derived = derived

    def getNumTypes(self):
        return len(self._values)

    def fromKey(self, key: int):
        return self._values[key]

    def getAllDerivedFrom(self, base: str):
        return list(self._derived[base])


class _Base:
    def __init__(self, registry: _Registry):
        self.TypeId = registry


class _FreeCad:
    def __init__(self, registry: _Registry, version=(1, 1, 0)):
        self.Base = _Base(registry)
        self._version = version

    def Version(self):
        return [str(item) for item in self._version] + ["build"]


def _runtime():
    bad = _Type("BadType", "Base", bad=True)
    base = _Type("Base::BaseClass", "Base", bad)
    document = _Type("App::DocumentObject", "App", base)
    prop = _Type("App::PropertyLength", "App", base)
    extension = _Type("App::Extension", "App", base)
    box = _Type("Part::Box", "Part", document)
    values = (bad, base, document, prop, extension, box)
    derived = {
        "App::DocumentObject": (document, box),
        "App::Property": (prop,),
        "App::Extension": (extension,),
    }
    return _FreeCad(_Registry(values, derived))


def _collect(**changes: object):
    values = {
        "freecad": _runtime(),
        "backend_version": (1, 1, 0),
        "build_fingerprint_sha256": _sha("managed-build"),
        "platform_id": "macos.x86_64",
        "probe_profile": CapabilityExecutionProfile.HEADLESS,
        "probe_modules": ("Part",),
    }
    values.update(changes)
    return collect_loaded_freecad_type_snapshot(**values)


def test_collector_classifies_loaded_types_without_instantiating_objects() -> None:
    snapshot = _collect()
    categories = {item.native_type_id: item.category for item in snapshot.registered_types}
    assert categories == {
        "App::DocumentObject": FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
        "App::Extension": FreeCadNativeTypeCategory.EXTENSION_TYPE,
        "App::PropertyLength": FreeCadNativeTypeCategory.PROPERTY_TYPE,
        "Base::BaseClass": FreeCadNativeTypeCategory.NATIVE_TYPE,
        "Part::Box": FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
    }
    assert all(item.native_type_id != "BadType" for item in snapshot.registered_types)
    catalog = build_freecad_type_catalog(snapshot)
    assert len(catalog.descriptors) == 8
    assert catalog.discovery_receipt_sha256 == snapshot.receipt_sha256


def test_collector_binds_exact_runtime_version_and_build_inputs() -> None:
    with pytest.raises(CapabilityCatalogError) as mismatch:
        _collect(backend_version=(1, 1, 1))
    assert mismatch.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    assert mismatch.value.path == "freecad/Version"

    with pytest.raises(CapabilityCatalogError) as malformed:
        _collect(backend_version=(True, 1, 0))
    assert malformed.value.code is CapabilityCatalogErrorCode.INVALID_INPUT


class _BrokenRegistry:
    def getNumTypes(self):
        raise RuntimeError("untrusted runtime diagnostic")


def test_collector_translates_runtime_failures_without_reflecting_details() -> None:
    broken = _FreeCad(_BrokenRegistry())
    with pytest.raises(CapabilityCatalogError) as failure:
        _collect(freecad=broken)
    assert failure.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    assert str(failure.value) == "integrity_failure"
    assert "untrusted" not in str(failure.value)


class _OversizedRegistry:
    def getNumTypes(self):
        return MAX_FREECAD_REGISTERED_TYPES + 2


def test_collector_rejects_registry_budget_before_iteration() -> None:
    oversized = _FreeCad(_OversizedRegistry())
    with pytest.raises(CapabilityCatalogError) as failure:
        _collect(freecad=oversized)
    assert failure.value.code is CapabilityCatalogErrorCode.BUDGET_EXCEEDED
    assert failure.value.path == "freecad/TypeId"
