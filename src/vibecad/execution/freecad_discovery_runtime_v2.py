"""Internal headless runtime collector for paged FreeCAD discovery v2.

This is the narrow composition seam between one already-selected managed
FreeCAD process and the pure contracts in :mod:`freecad_discovery_v2`.  Only a
small reviewed module allowlist may be imported.  Imports may register native
TypeIds, but this collector never creates a document, instantiates a native
type, imports ``FreeCADGui``, or promotes a discovered descriptor.

The caller remains responsible for process isolation and for authenticating
the returned snapshot/manifest at a wider trust boundary.  This module is not
part of the MCP or durable-storage surface.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import platform
import sys
from collections.abc import Callable

from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
    CapabilitySupportStatus,
)
from vibecad.execution.freecad_capabilities import (
    FreeCadNativeTypeCategory,
    FreeCadRegisteredType,
)
from vibecad.execution.freecad_discovery_v2 import (
    DEFAULT_FREECAD_DISCOVERY_V2_PAGE_DESCRIPTORS,
    FREECAD_DISCOVERY_V2_ALGORITHM_VERSION,
    FREECAD_DISCOVERY_V2_SCHEMA_VERSION,
    MAX_FREECAD_DISCOVERY_V2_TYPES,
    FreeCadDiscoverySnapshotV2,
    FreeCadPagedCapabilityCatalog,
    build_paged_freecad_type_catalog,
    validate_freecad_capability_page_set,
)
from vibecad.runtime.spec import FREECAD_VERSION, PYTHON_VERSION

# Expansion requires an explicit code change plus a real headless import probe.
# Python workbench packages, GUI modules, and arbitrary model-provided names are
# deliberately absent.
FREECAD_DISCOVERY_V2_ALLOWED_MODULES = (
    "Part",
    "PartDesign",
    "Sketcher",
)

_BUILD_FINGERPRINT_DOMAIN = b"vibecad-freecad-managed-build-v1\0"
_MAX_VERSION_FIELDS = 16
_MAX_VERSION_FIELD_BYTES = 256


def _fail(code: CapabilityCatalogErrorCode, path: str) -> None:
    raise CapabilityCatalogError(code, path)


def _current_python_version() -> tuple[int, int]:
    return tuple(sys.version_info[:2])


def _freecad_version(freecad: object) -> tuple[str, ...]:
    try:
        raw = freecad.Version()
    except Exception:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/Version")
    if type(raw) not in {list, tuple} or not 3 <= len(raw) <= _MAX_VERSION_FIELDS:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/Version")
    fields: list[str] = []
    for index, item in enumerate(raw):
        if type(item) is not str:
            _fail(
                CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                f"freecad/Version/{index}",
            )
        try:
            size = len(item.encode("utf-8"))
        except UnicodeError:
            _fail(
                CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                f"freecad/Version/{index}",
            )
        if not item or size > _MAX_VERSION_FIELD_BYTES or not item.isprintable():
            _fail(
                CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                f"freecad/Version/{index}",
            )
        fields.append(item)
    try:
        actual = tuple(int(fields[index]) for index in range(3))
    except (TypeError, ValueError, OverflowError):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/Version")
    if actual != FREECAD_VERSION:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/Version")
    return tuple(fields)


def _build_fingerprint(version_fields: tuple[str, ...]) -> str:
    try:
        raw = json.dumps(
            {"freecad_version": list(version_fields)},
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/Version")
    return hashlib.sha256(_BUILD_FINGERPRINT_DOMAIN + raw).hexdigest()


def _platform_id() -> str:
    try:
        system = platform.system().strip().lower()
        machine = platform.machine().strip().lower()
    except Exception:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "platform")
    system = {"darwin": "macos", "windows": "windows"}.get(system, system)
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)
    value = f"{system}.{machine}"
    if not system or not machine:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "platform")
    return value


def _require_headless_empty(freecad: object, path: str) -> None:
    try:
        gui_up = freecad.GuiUp
        documents = freecad.listDocuments()
    except Exception:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)
    if (
        type(gui_up) is not int
        or gui_up != 0
        or type(documents) is not dict
        or documents
        or "FreeCADGui" in sys.modules
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)


def _probe_modules(
    *,
    freecad: object,
    probe_modules: object,
    module_importer: object,
) -> tuple[str, ...]:
    if type(probe_modules) is not tuple or not probe_modules:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "probe_modules")
    if any(type(item) is not str for item in probe_modules):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "probe_modules")
    if len(set(probe_modules)) != len(probe_modules) or any(
        item not in FREECAD_DISCOVERY_V2_ALLOWED_MODULES for item in probe_modules
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "probe_modules")
    if not isinstance(module_importer, Callable):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "module_importer")
    modules = tuple(sorted(probe_modules))
    _require_headless_empty(freecad, "freecad/headless")
    for index, module_name in enumerate(modules):
        try:
            imported = module_importer(module_name)
            imported_name = imported.__name__
        except Exception:
            _fail(
                CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                f"freecad/modules/{index}",
            )
        if type(imported_name) is not str or imported_name != module_name:
            _fail(
                CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                f"freecad/modules/{index}",
            )
        _require_headless_empty(freecad, f"freecad/modules/{index}")
    return modules


def _derived_names(type_registry: object, base: str) -> set[str]:
    try:
        values = type_registry.getAllDerivedFrom(base)
    except Exception:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/TypeId")
    if type(values) not in {list, tuple}:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/TypeId")
    if len(values) > MAX_FREECAD_DISCOVERY_V2_TYPES:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "freecad/TypeId")
    names: list[str] = []
    for item in values:
        try:
            name = item.Name
        except Exception:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/TypeId")
        if type(name) is not str:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/TypeId")
        names.append(name)
    if len(set(names)) != len(names):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/TypeId")
    return set(names)


def _collect_registered_types(freecad: object) -> tuple[FreeCadRegisteredType, ...]:
    try:
        type_registry = freecad.Base.TypeId
        count = type_registry.getNumTypes()
    except Exception:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/TypeId")
    if type(count) is not int or count < 0:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/TypeId")
    # FreeCAD reserves key zero for one BadType sentinel.
    if count > MAX_FREECAD_DISCOVERY_V2_TYPES + 1:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "freecad/TypeId")

    document_objects = _derived_names(type_registry, "App::DocumentObject")
    property_types = _derived_names(type_registry, "App::Property")
    extension_types = _derived_names(type_registry, "App::Extension")
    registered: list[FreeCadRegisteredType] = []
    bad_count = 0
    for key in range(count):
        try:
            native_type = type_registry.fromKey(key)
            is_bad = native_type.isBad()
            if type(is_bad) is not bool:
                raise ValueError
            if is_bad:
                bad_count += 1
                continue
            native_type_id = native_type.Name
            declaring_module = native_type.Module
            parent = native_type.getParent()
            parent_is_bad = parent.isBad()
            if type(parent_is_bad) is not bool:
                raise ValueError
            parent_native_type_id = None if parent_is_bad else parent.Name
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
    try:
        final_count = type_registry.getNumTypes()
    except Exception:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/TypeId")
    if type(final_count) is not int or final_count != count or bad_count > 1:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/TypeId")
    return tuple(registered)


def collect_managed_freecad_discovery_v2(
    *,
    freecad: object,
    probe_modules: tuple[str, ...],
    max_descriptors_per_page: int = DEFAULT_FREECAD_DISCOVERY_V2_PAGE_DESCRIPTORS,
    module_importer: Callable[[str], object] = importlib.import_module,
) -> FreeCadPagedCapabilityCatalog:
    """Collect and compose one exact managed, headless, discovered-only bundle."""

    if _current_python_version() != PYTHON_VERSION:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "python/Version")
    version_before = _freecad_version(freecad)
    modules = _probe_modules(
        freecad=freecad,
        probe_modules=probe_modules,
        module_importer=module_importer,
    )
    registered = _collect_registered_types(freecad)
    _require_headless_empty(freecad, "freecad/headless")
    version_after = _freecad_version(freecad)
    if version_after != version_before:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "freecad/Version")
    snapshot = FreeCadDiscoverySnapshotV2(
        schema_version=FREECAD_DISCOVERY_V2_SCHEMA_VERSION,
        backend_version=tuple(int(item) for item in version_before[:3]),
        build_fingerprint_sha256=_build_fingerprint(version_before),
        platform_id=_platform_id(),
        probe_profile=CapabilityExecutionProfile.HEADLESS,
        probe_modules=modules,
        registered_types=registered,
        probe_algorithm_version=FREECAD_DISCOVERY_V2_ALGORITHM_VERSION,
    )
    bundle = build_paged_freecad_type_catalog(
        snapshot,
        max_descriptors_per_page=max_descriptors_per_page,
    )
    if validate_freecad_capability_page_set(bundle.manifest, bundle.pages) != bundle.pages:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "pages")
    if any(
        descriptor.status is not CapabilitySupportStatus.DISCOVERED
        for page in bundle.pages
        for descriptor in page.descriptors
    ):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "pages/descriptors")
    return bundle


__all__ = ()
