from __future__ import annotations

import pytest

from vibecad.feedback.release_drawing import DRAWING_VIEWS, assembly_drawing_pdf
from vibecad.validation import BomObservation, BomRowObservation

COMPONENT_ID = "object_11111111111111111111111111111111"


def _view(offset: float = 0.0) -> dict[str, object]:
    return {
        "vis": [
            [
                (offset, 0.0),
                (offset + 10.0, 0.0),
                (offset + 10.0, 10.0),
                (offset, 10.0),
                (offset, 0.0),
            ]
        ],
        "hid": [],
        "circles": [],
    }


def _bom() -> BomObservation:
    row = BomRowObservation(
        part_number="BRACKET-001",
        description="Mounting bracket",
        material="Aluminum 6061",
        density_kg_m3=2700,
        quantity=1,
        unit_mass_kg=0.0027,
        total_mass_kg=0.0027,
        component_ids=(COMPONENT_ID,),
        geometry_digest="1" * 64,
    )
    return BomObservation(
        component_count=1,
        rows=(row,),
        total_quantity=1,
        total_mass_kg=0.0027,
        complete=True,
    )


def test_assembly_drawing_pdf_is_deterministic_and_binds_bom_items() -> None:
    views = {name: _view() for name in DRAWING_VIEWS}
    components = {COMPONENT_ID: _view()}

    first, first_items = assembly_drawing_pdf(
        views=views,
        component_isometric_views=components,
        bom=_bom(),
        project_id="project_22222222222222222222222222222222",
        revision_id="revision_33333333333333333333333333333333",
    )
    second, second_items = assembly_drawing_pdf(
        views=views,
        component_isometric_views=components,
        bom=_bom(),
        project_id="project_22222222222222222222222222222222",
        revision_id="revision_33333333333333333333333333333333",
    )

    assert first == second
    assert first.startswith(b"%PDF-")
    assert b"%%EOF" in first[-32:]
    assert first_items == second_items == ((1, COMPONENT_ID),)


def test_assembly_drawing_rejects_component_projection_not_in_bom() -> None:
    with pytest.raises(ValueError, match="component projections"):
        assembly_drawing_pdf(
            views={name: _view() for name in DRAWING_VIEWS},
            component_isometric_views={},
            bom=_bom(),
            project_id="project_22222222222222222222222222222222",
            revision_id="revision_33333333333333333333333333333333",
        )
