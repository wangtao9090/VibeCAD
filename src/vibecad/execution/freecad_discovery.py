"""Read-only collection of an already-loaded FreeCAD TypeId registry.

The caller owns runtime/module loading and process isolation.  This collector
does not import workbenches, instantiate objects, execute commands, or mutate a
document; it only snapshots the currently registered TypeIds into the pure
contract in ``freecad_capabilities``.
"""

from __future__ import annotations

from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
)
from vibecad.execution.freecad_capabilities import (
    MAX_FREECAD_REGISTERED_TYPES,
    FreeCadNativeTypeCategory,
    FreeCadRegisteredType,
    FreeCadTypeRegistrySnapshot,
)


def _fail(code: CapabilityCatalogErrorCode, path: str) -> None:
    raise CapabilityCatalogError(code, path)


def _exact_version(value: object) -> tuple[int, ...]:
    if (
        type(value) is not tuple
        or not 1 <= len(value) <= 4
        or not all(type(item) is int and 0 <= item <= 999_999 for item in value)
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "backend_version")
    return value


def _runtime_version(freecad: object) -> tuple[int, int, int]:
    try:
        raw = freecad.Version()
        if type(raw) not in {list, tuple} or len(raw) < 3:
            raise ValueError
        value = tuple(int(raw[index]) for index in range(3))
    except Exception:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/Version")
    if any(item < 0 or item > 999_999 for item in value):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/Version")
    return value


def _derived_names(type_registry: object, base: str) -> set[str]:
    try:
        values = type_registry.getAllDerivedFrom(base)
        if type(values) not in {list, tuple}:
            raise ValueError
        result = {item.Name for item in values}
    except Exception:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/TypeId")
    if not all(type(item) is str for item in result):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/TypeId")
    return result


def collect_loaded_freecad_type_snapshot(
    *,
    freecad: object,
    backend_version: tuple[int, ...],
    build_fingerprint_sha256: str,
    platform_id: str,
    probe_profile: CapabilityExecutionProfile,
    probe_modules: tuple[str, ...],
    probe_algorithm_version: str = "1.0",
) -> FreeCadTypeRegistrySnapshot:
    """Collect registered types without importing or instantiating a capability."""

    expected_version = _exact_version(backend_version)
    actual_version = _runtime_version(freecad)
    if expected_version[:3] != actual_version:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/Version")
    try:
        type_registry = freecad.Base.TypeId
        count = type_registry.getNumTypes()
    except Exception:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/TypeId")
    if type(count) is not int or count < 0:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/TypeId")
    if count > MAX_FREECAD_REGISTERED_TYPES + 1:  # one BadType sentinel may be present
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "freecad/TypeId")
    document_objects = _derived_names(type_registry, "App::DocumentObject")
    property_types = _derived_names(type_registry, "App::Property")
    extension_types = _derived_names(type_registry, "App::Extension")
    registered: list[FreeCadRegisteredType] = []
    for key in range(count):
        try:
            native_type = type_registry.fromKey(key)
            if type(native_type.isBad()) is not bool:
                raise ValueError
            if native_type.isBad():
                continue
            native_type_id = native_type.Name
            declaring_module = native_type.Module
            parent = native_type.getParent()
            parent_native_type_id = None if parent.isBad() else parent.Name
        except Exception:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/TypeId")
        category = FreeCadNativeTypeCategory.NATIVE_TYPE
        if native_type_id in document_objects:
            category = FreeCadNativeTypeCategory.DOCUMENT_OBJECT
        elif native_type_id in property_types:
            category = FreeCadNativeTypeCategory.PROPERTY_TYPE
        elif native_type_id in extension_types:
            category = FreeCadNativeTypeCategory.EXTENSION_TYPE
        registered.append(
            FreeCadRegisteredType(
                native_type_id=native_type_id,
                declaring_module=declaring_module,
                parent_native_type_id=parent_native_type_id,
                category=category,
            )
        )
    return FreeCadTypeRegistrySnapshot(
        schema_version=1,
        backend_version=expected_version,
        build_fingerprint_sha256=build_fingerprint_sha256,
        platform_id=platform_id,
        probe_profile=probe_profile,
        probe_modules=probe_modules,
        registered_types=tuple(registered),
        probe_algorithm_version=probe_algorithm_version,
    )


__all__ = ()
