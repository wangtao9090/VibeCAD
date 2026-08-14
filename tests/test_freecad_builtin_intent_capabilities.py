from __future__ import annotations

import importlib
import sys

from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityExecutionProfile,
    CapabilityKind,
    CapabilitySupportStatus,
    encode_capability_catalog,
)
from vibecad.execution.freecad_builtin_intent_capabilities import (
    build_current_freecad_intent_capability_catalog,
    current_freecad_intent_capability_specs,
)


def _backend() -> CapabilityBackend:
    return CapabilityBackend(
        backend_id="freecad",
        backend_version=(1, 1, 0),
        build_fingerprint_sha256="a" * 64,
        platform_id="darwin-x86_64",
        discovery_profile=CapabilityExecutionProfile.HEADLESS,
    )


def test_current_specs_cover_all_three_reviewed_adapter_families() -> None:
    specs = current_freecad_intent_capability_specs()
    assert len(specs) == 12
    assert {item.native_type_id for item in specs} == {
        "PartDesign::Groove",
        "PartDesign::AdditiveLoft",
        "PartDesign::SubtractiveLoft",
        "PartDesign::AdditivePipe",
        "PartDesign::SubtractivePipe",
        "PartDesign::AdditiveHelix",
        "PartDesign::SubtractiveHelix",
        "PartDesign::Plane",
        "PartDesign::Line",
        "PartDesign::Point",
        "PartDesign::ShapeBinder",
        "PartDesign::SubShapeBinder",
    }
    assert len({item.operation_id for item in specs}) == len(specs)
    assert len({item.semantic_operation for item in specs}) == len(specs)
    assert {item.adapter_id for item in specs} == {
        "freecad_parametric_groove_adapter",
        "freecad_partdesign_promotion_adapter",
        "freecad_partdesign_reference_adapter",
    }
    assert all(item.verification is None for item in specs)


def test_current_catalog_is_deterministic_and_executable_not_verified() -> None:
    before = build_current_freecad_intent_capability_catalog(backend=_backend())
    after = build_current_freecad_intent_capability_catalog(backend=_backend())
    assert encode_capability_catalog(before) == encode_capability_catalog(after)

    operations = tuple(item for item in before.descriptors if item.kind is CapabilityKind.OPERATION)
    assert len(operations) == 12
    assert all(item.status is CapabilitySupportStatus.EXECUTABLE for item in operations)
    assert all(item.verification is None for item in operations)


def test_builtin_catalog_import_is_freecad_runtime_safe() -> None:
    prior = {name for name in sys.modules if name in {"FreeCAD", "Part", "Sketcher"}}
    module = importlib.import_module("vibecad.execution.freecad_builtin_intent_capabilities")
    importlib.reload(module)
    assert {name for name in sys.modules if name in {"FreeCAD", "Part", "Sketcher"}} == prior
