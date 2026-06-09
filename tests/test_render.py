import pytest

from vibecad.feedback import render

_TETRA_VERTS = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0), (0.0, 0.0, 10.0)]
_TETRA_FACETS = [(0, 1, 2), (0, 1, 3), (0, 2, 3), (1, 2, 3)]


def test_mesh_to_png_returns_png_bytes():
    png = render.mesh_to_png(_TETRA_VERTS, _TETRA_FACETS)
    assert png[:4] == b"\x89PNG"
    assert len(png) > 1000


def test_mesh_to_png_each_view():
    for v in ("iso", "front", "top", "right", "back"):
        assert render.mesh_to_png(_TETRA_VERTS, _TETRA_FACETS, view=v)[:4] == b"\x89PNG"


def test_mesh_to_png_rejects_bad_view():
    with pytest.raises(ValueError, match="view"):
        render.mesh_to_png(_TETRA_VERTS, _TETRA_FACETS, view="nope")
