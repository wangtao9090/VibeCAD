"""Focused tests for the internal managed FreeCAD discovery-v2 collector."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import sys
import types
from pathlib import Path

import pytest

import vibecad.execution.freecad_discovery_runtime_v2 as runtime_discovery
from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
    CapabilitySupportStatus,
)
from vibecad.execution.freecad_discovery_runtime_v2 import (
    FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
    collect_managed_freecad_discovery_v2,
)
from vibecad.execution.freecad_discovery_v2 import (
    FREECAD_DISCOVERY_V2_ALGORITHM_VERSION,
    validate_freecad_capability_page_set,
)
from vibecad.runtime.spec import FREECAD_VERSION, PYTHON_VERSION


class _Type:
    def __init__(
        self,
        name: str,
        module: str,
        parent: _Type | None = None,
        *,
        bad: bool = False,
    ) -> None:
        self.Name = name
        self.Module = module
        self._parent = parent
        self._bad = bad

    def isBad(self) -> bool:  # noqa: N802 - exact FreeCAD API spelling
        return self._bad

    def getParent(self) -> _Type:
        return self._parent


class _Registry:
    def __init__(self) -> None:
        bad = _Type("BadType", "Base", bad=True)
        base = _Type("Base::BaseClass", "Base", bad)
        document = _Type("App::DocumentObject", "App", base)
        prop = _Type("App::PropertyLength", "App", base)
        extension = _Type("App::Extension", "App", base)
        box = _Type("Part::Box", "Part", document)
        pad = _Type("PartDesign::Feature", "PartDesign", document)
        sketch = _Type("Sketcher::SketchObject", "Sketcher", document)
        self.values = (bad, base, document, prop, extension, box, pad, sketch)
        self.derived = {
            "App::DocumentObject": (document, box, pad, sketch),
            "App::Property": (prop,),
            "App::Extension": (extension,),
        }

    def getNumTypes(self) -> int:  # noqa: N802 - exact FreeCAD API spelling
        return len(self.values)

    def fromKey(self, key: int) -> _Type:  # noqa: N802 - exact FreeCAD API spelling
        return self.values[key]

    def getAllDerivedFrom(self, base: str) -> list[_Type]:  # noqa: N802
        return list(self.derived[base])


class _Base:
    def __init__(self, registry: _Registry) -> None:
        self.TypeId = registry


class _FreeCAD:
    GuiUp = 0

    def __init__(self, *, version: tuple[int, int, int] = FREECAD_VERSION) -> None:
        self.Base = _Base(_Registry())
        self._version = version
        self.documents: dict[str, object] = {}

    def Version(self) -> list[str]:  # noqa: N802 - exact FreeCAD API spelling
        return [
            *(str(item) for item in self._version),
            "20260813 (Git shallow)",
            "Unknown",
            "2026/08/13 00:00:00",
            "(HEAD detached)",
            "0123456789abcdef",
        ]

    def listDocuments(self) -> dict[str, object]:  # noqa: N802 - exact FreeCAD API spelling
        return dict(self.documents)


def _collect(
    monkeypatch: pytest.MonkeyPatch,
    *,
    freecad: _FreeCAD | None = None,
    probe_modules: tuple[str, ...] = FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
    importer=None,
):
    monkeypatch.setattr(runtime_discovery, "_current_python_version", lambda: PYTHON_VERSION)
    monkeypatch.setattr(runtime_discovery.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(runtime_discovery.platform, "machine", lambda: "AMD64")
    freecad = freecad or _FreeCAD()
    calls: list[str] = []

    def safe_import(module_name: str) -> types.ModuleType:
        calls.append(module_name)
        return types.ModuleType(module_name)

    bundle = collect_managed_freecad_discovery_v2(
        freecad=freecad,
        probe_modules=probe_modules,
        max_descriptors_per_page=3,
        module_importer=safe_import if importer is None else importer,
    )
    return bundle, calls


def test_collects_bound_complete_discovered_only_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, calls = _collect(monkeypatch)

    assert calls == sorted(FREECAD_DISCOVERY_V2_ALLOWED_MODULES)
    assert bundle.snapshot.backend_version == FREECAD_VERSION
    assert bundle.snapshot.probe_profile is CapabilityExecutionProfile.HEADLESS
    assert bundle.snapshot.platform_id == "macos.x86_64"
    assert bundle.snapshot.probe_modules == tuple(sorted(FREECAD_DISCOVERY_V2_ALLOWED_MODULES))
    assert bundle.snapshot.probe_algorithm_version == FREECAD_DISCOVERY_V2_ALGORITHM_VERSION
    assert (
        bundle.snapshot.build_fingerprint_sha256
        == hashlib.sha256(
            runtime_discovery._BUILD_FINGERPRINT_DOMAIN
            + b'{"freecad_version":["1","1","0","20260813 (Git shallow)",'
            b'"Unknown","2026/08/13 00:00:00","(HEAD detached)",'
            b'"0123456789abcdef"]}'
        ).hexdigest()
    )
    assert bundle.manifest.snapshot_sha256 == bundle.snapshot.snapshot_sha256
    assert validate_freecad_capability_page_set(bundle.manifest, bundle.pages) == bundle.pages
    assert all(
        descriptor.status is CapabilitySupportStatus.DISCOVERED
        for page in bundle.pages
        for descriptor in page.descriptors
    )
    assert all(
        not descriptor.execution_profiles and descriptor.verification is None
        for page in bundle.pages
        for descriptor in page.descriptors
    )
    native_ids = {item.native_type_id for item in bundle.snapshot.registered_types}
    assert {
        "App::DocumentObject",
        "Part::Box",
        "PartDesign::Feature",
        "Sketcher::SketchObject",
    } <= native_ids


def test_module_order_is_canonical_and_duplicate_or_unknown_is_rejected_before_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forward, forward_calls = _collect(
        monkeypatch,
        probe_modules=("Sketcher", "Part", "PartDesign"),
    )
    reverse, reverse_calls = _collect(
        monkeypatch,
        probe_modules=("PartDesign", "Sketcher", "Part"),
    )
    assert forward_calls == reverse_calls == ["Part", "PartDesign", "Sketcher"]
    assert forward.snapshot.snapshot_sha256 == reverse.snapshot.snapshot_sha256
    assert forward.manifest.manifest_sha256 == reverse.manifest.manifest_sha256

    calls: list[str] = []

    def importer(module_name: str) -> types.ModuleType:
        calls.append(module_name)
        return types.ModuleType(module_name)

    for rejected in (
        ("Part", "Part"),
        ("FreeCADGui",),
        ("Draft",),
        ([],),
        (),
        ["Part"],
    ):
        with pytest.raises(CapabilityCatalogError) as failure:
            _collect(
                monkeypatch,
                probe_modules=rejected,  # type: ignore[arg-type]
                importer=importer,
            )
        assert failure.value.code is CapabilityCatalogErrorCode.INVALID_INPUT
    assert calls == []


def test_headless_document_and_gui_invariants_gate_every_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_discovery, "_current_python_version", lambda: PYTHON_VERSION)
    freecad = _FreeCAD()
    calls: list[str] = []

    def creates_document(module_name: str) -> types.ModuleType:
        calls.append(module_name)
        freecad.documents["Unexpected"] = object()
        return types.ModuleType(module_name)

    with pytest.raises(CapabilityCatalogError) as document_failure:
        collect_managed_freecad_discovery_v2(
            freecad=freecad,
            probe_modules=("Part", "PartDesign"),
            module_importer=creates_document,
        )
    assert document_failure.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    assert document_failure.value.path == "freecad/modules/0"
    assert calls == ["Part"]

    freecad = _FreeCAD()

    def loads_gui(module_name: str) -> types.ModuleType:
        monkeypatch.setitem(sys.modules, "FreeCADGui", types.ModuleType("FreeCADGui"))
        return types.ModuleType(module_name)

    with pytest.raises(CapabilityCatalogError) as gui_failure:
        collect_managed_freecad_discovery_v2(
            freecad=freecad,
            probe_modules=("Part",),
            module_importer=loads_gui,
        )
    assert gui_failure.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    assert gui_failure.value.path == "freecad/modules/0"


def test_pin_import_and_registry_failures_are_bounded_and_non_reflective(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_discovery, "_current_python_version", lambda: (9, 9))
    with pytest.raises(CapabilityCatalogError) as python_mismatch:
        collect_managed_freecad_discovery_v2(
            freecad=_FreeCAD(),
            probe_modules=("Part",),
        )
    assert python_mismatch.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    assert python_mismatch.value.path == "python/Version"

    monkeypatch.setattr(runtime_discovery, "_current_python_version", lambda: PYTHON_VERSION)
    with pytest.raises(CapabilityCatalogError) as freecad_mismatch:
        collect_managed_freecad_discovery_v2(
            freecad=_FreeCAD(version=(9, 9, 9)),
            probe_modules=("Part",),
        )
    assert freecad_mismatch.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    assert freecad_mismatch.value.path == "freecad/Version"

    def explosive_import(_module_name: str) -> object:
        raise RuntimeError("sensitive host import detail")

    with pytest.raises(CapabilityCatalogError) as import_failure:
        collect_managed_freecad_discovery_v2(
            freecad=_FreeCAD(),
            probe_modules=("Part",),
            module_importer=explosive_import,
        )
    assert import_failure.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    assert str(import_failure.value) == "integrity_failure"
    assert "sensitive" not in str(import_failure.value)
    assert len(import_failure.value.path.encode("utf-8")) <= 256


def test_registry_drift_and_post_composition_promotion_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_discovery, "_current_python_version", lambda: PYTHON_VERSION)
    freecad = _FreeCAD()
    registry = freecad.Base.TypeId
    original_count = registry.getNumTypes
    calls = 0

    def drifting_count() -> int:
        nonlocal calls
        calls += 1
        return original_count() + (1 if calls > 1 else 0)

    registry.getNumTypes = drifting_count
    with pytest.raises(CapabilityCatalogError) as drift:
        collect_managed_freecad_discovery_v2(
            freecad=freecad,
            probe_modules=("Part",),
            module_importer=lambda name: types.ModuleType(name),
        )
    assert drift.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    assert drift.value.path == "freecad/TypeId"

    real_builder = runtime_discovery.build_paged_freecad_type_catalog

    def promoted_builder(snapshot, *, max_descriptors_per_page):
        bundle = real_builder(
            snapshot,
            max_descriptors_per_page=max_descriptors_per_page,
        )
        descriptor = bundle.pages[0].descriptors[0]
        promoted = dataclasses.replace(
            descriptor,
            status=CapabilitySupportStatus.REPRESENTABLE,
        )
        page = dataclasses.replace(
            bundle.pages[0],
            descriptors=(promoted, *bundle.pages[0].descriptors[1:]),
        )
        return dataclasses.replace(bundle, pages=(page, *bundle.pages[1:]))

    monkeypatch.setattr(runtime_discovery, "build_paged_freecad_type_catalog", promoted_builder)
    with pytest.raises(CapabilityCatalogError):
        _collect(monkeypatch)


@pytest.mark.slow
def test_real_managed_freecad_collects_complete_headless_discovered_bundle() -> None:
    python_raw = os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON")
    if not python_raw:
        pytest.skip("managed FreeCAD Python was not requested")
    if Path(python_raw).resolve() != Path(sys.executable).resolve():
        pytest.fail("the test must run inside the requested managed FreeCAD Python")

    from vibecad.freecad_env import prepare_freecad_import

    prepare_freecad_import()
    import FreeCAD  # noqa: PLC0415

    before_documents = FreeCAD.listDocuments()
    before_gui_up = FreeCAD.GuiUp
    before_gui_module = "FreeCADGui" in sys.modules
    bundle = collect_managed_freecad_discovery_v2(
        freecad=FreeCAD,
        probe_modules=FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
    )

    assert before_documents == FreeCAD.listDocuments() == {}
    assert before_gui_up == FreeCAD.GuiUp == 0
    assert before_gui_module is False
    assert "FreeCADGui" not in sys.modules
    assert bundle.snapshot.probe_modules == tuple(sorted(FREECAD_DISCOVERY_V2_ALLOWED_MODULES))
    assert bundle.snapshot.build_fingerprint_sha256 == runtime_discovery._build_fingerprint(
        tuple(FreeCAD.Version())
    )
    assert bundle.manifest.type_count == len(bundle.snapshot.registered_types)
    assert bundle.manifest.type_count > 200
    assert len(bundle.pages) > 1
    assert validate_freecad_capability_page_set(bundle.manifest, bundle.pages) == bundle.pages
    assert all(
        descriptor.status is CapabilitySupportStatus.DISCOVERED
        and not descriptor.execution_profiles
        and descriptor.verification is None
        for page in bundle.pages
        for descriptor in page.descriptors
    )
