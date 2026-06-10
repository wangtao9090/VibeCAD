"""features：参数校验快测（真机 happy-path 在 @slow 部分，后续任务补）。"""
import pytest

from vibecad.tools import features


class _NoopSession:
    pass


@pytest.mark.parametrize("kwargs,msg", [
    ({"face": "", "diameter": 6}, "face"),
    ({"face": "A", "diameter": 0}, "diameter"),
    ({"face": "A", "diameter": -2}, "diameter"),
    ({"face": "A", "diameter": 6, "depth": 0}, "depth"),
    ({"face": "A", "diameter": 6, "offset": [1]}, "offset"),
    ({"face": "A", "diameter": 6, "offset": ["a", "b"]}, "offset"),
])
def test_add_hole_validation(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        features.add_hole(_NoopSession(), **kwargs)


@pytest.mark.parametrize("fn,kwargs,msg", [
    (features.fillet_edges, {"edges": [], "radius": 2}, "edges"),
    (features.fillet_edges, {"edges": ["E1"], "radius": 0}, "radius"),
    (features.fillet_edges, {"edges": "E1", "radius": 2}, "edges"),
    (features.chamfer_edges, {"edges": [], "size": 1}, "edges"),
    (features.chamfer_edges, {"edges": ["E1"], "size": -1}, "size"),
])
def test_edge_features_validation(fn, kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        fn(_NoopSession(), **kwargs)


def test_inplane_axes_orthonormal():
    e1, e2 = features._inplane_axes((0.0, 0.0, 1.0))
    assert abs(sum(a * b for a, b in zip(e1, e2, strict=True))) < 1e-9
    assert abs(sum(a * a for a in e1) - 1) < 1e-9 and abs(e1[2]) < 1e-9


def test_inplane_axes_arbitrary_normal():
    import math
    n = (1 / math.sqrt(3),) * 3
    e1, e2 = features._inplane_axes(n)
    for e in (e1, e2):
        assert abs(sum(a * b for a, b in zip(e, n, strict=True))) < 1e-9  # 与法向正交
