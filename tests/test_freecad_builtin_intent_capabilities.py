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
    current_freecad_intent_promotion_specs,
)
from vibecad.execution.freecad_reviewed_family_capabilities import (
    current_freecad_reviewed_family_capability_specs,
)


def _backend() -> CapabilityBackend:
    return CapabilityBackend(
        backend_id="freecad",
        backend_version=(1, 1, 0),
        build_fingerprint_sha256="a" * 64,
        platform_id="darwin-x86_64",
        discovery_profile=CapabilityExecutionProfile.HEADLESS,
    )


def test_current_specs_cover_all_reviewed_adapter_families() -> None:
    specs = current_freecad_intent_capability_specs()
    reviewed = current_freecad_reviewed_family_capability_specs()
    legacy_native_types = {
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
        "PartDesign::AdditiveBox",
        "PartDesign::SubtractiveBox",
        "PartDesign::AdditiveCylinder",
        "PartDesign::SubtractiveCylinder",
        "PartDesign::AdditiveSphere",
        "PartDesign::SubtractiveSphere",
        "PartDesign::AdditiveCone",
        "PartDesign::SubtractiveCone",
        "PartDesign::AdditiveEllipsoid",
        "PartDesign::SubtractiveEllipsoid",
        "PartDesign::AdditivePrism",
        "PartDesign::SubtractivePrism",
        "PartDesign::AdditiveWedge",
        "PartDesign::SubtractiveWedge",
        "PartDesign::AdditiveTorus",
        "PartDesign::SubtractiveTorus",
        "Sketcher::SketchObject",
        "PartDesign::Pad",
        "PartDesign::Pocket",
        "PartDesign::Scaled",
        "PartDesign::MultiTransform",
        "PartDesign::Fillet",
        "PartDesign::Chamfer",
        "PartDesign::Draft",
        "PartDesign::Thickness",
        "PartDesign::LinearPattern",
        "PartDesign::PolarPattern",
        "PartDesign::Mirrored",
        "PartDesign::Boolean",
    }
    assert len(specs) == 124
    assert len({item.native_type_id for item in specs}) == 102
    assert {item.native_type_id for item in specs} == legacy_native_types | {
        item.native_type_id for item in reviewed
    }
    assert len({item.operation_id for item in specs}) == len(specs)
    assert len({item.semantic_operation for item in specs}) == len(specs)
    assert {item.adapter_id for item in specs} == {
        "freecad_parametric_groove_adapter",
        "freecad_partdesign_promotion_adapter",
        "freecad_partdesign_reference_adapter",
        "freecad_partdesign_primitive_adapter",
        "freecad_planar_mechanical_v1_adapter",
        "freecad_partdesign_dressup_transform_adapter",
        "freecad_partdesign_pattern_adapter",
        "freecad_partdesign_boolean_adapter",
    } | {item.adapter_id for item in reviewed}
    assert all(item.verification is None for item in specs)


def test_native_promotion_keeps_one_owner_for_shared_sketch_type() -> None:
    specs = current_freecad_intent_promotion_specs()

    assert len(specs) == 104
    assert len({item.native_type_id for item in specs}) == 102
    sketch_specs = tuple(item for item in specs if item.native_type_id == "Sketcher::SketchObject")
    assert len(sketch_specs) == 1
    assert sketch_specs[0].adapter_id == "freecad_planar_mechanical_v1_adapter"
    assert all(item.verification is None for item in specs)


def test_current_catalog_is_deterministic_and_executable_not_verified() -> None:
    before = build_current_freecad_intent_capability_catalog(backend=_backend())
    after = build_current_freecad_intent_capability_catalog(backend=_backend())
    assert encode_capability_catalog(before) == encode_capability_catalog(after)

    operations = tuple(item for item in before.descriptors if item.kind is CapabilityKind.OPERATION)
    assert len(operations) == 124
    assert all(item.status is CapabilitySupportStatus.EXECUTABLE for item in operations)
    assert all(item.verification is None for item in operations)


def test_builtin_catalog_import_is_freecad_runtime_safe() -> None:
    prior = {name for name in sys.modules if name in {"FreeCAD", "Part", "Sketcher"}}
    module = importlib.import_module("vibecad.execution.freecad_builtin_intent_capabilities")
    importlib.reload(module)
    assert {name for name in sys.modules if name in {"FreeCAD", "Part", "Sketcher"}} == prior
