"""sketch：profile DSL 校验矩阵 + 面积纯函数。快测（不碰 FreeCAD）。"""
import math

import pytest

from vibecad.tools import sketch


class _NoopSession:
    pass


@pytest.mark.parametrize("profile,msg", [
    ("rect", "profile"),
    ({"type": "blob"}, "type"),
    ({"type": "rect", "length": 10}, "width"),
    ({"type": "rect", "length": 0, "width": 5}, "length"),
    ({"type": "circle"}, "radius"),
    ({"type": "polygon", "points": [[0, 0], [1, 0]]}, "points"),
    ({"type": "polygon", "points": [[0, 0], [1, 0], ["x", 1]]}, "points"),
    ({"type": "slot", "length": 10}, "width"),
])
def test_extrude_profile_validation(profile, msg):
    with pytest.raises(ValueError, match=msg):
        sketch.extrude_profile(_NoopSession(), profile=profile, height=5)


@pytest.mark.parametrize("kwargs,msg", [
    ({"profile": {"type": "circle", "radius": 3}, "height": 0}, "height"),
    ({"profile": {"type": "circle", "radius": 3}, "height": 5, "operation": "carve"}, "operation"),
])
def test_extrude_args_validation(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        sketch.extrude_profile(_NoopSession(), **kwargs)


def test_profile_area_formulas():
    assert sketch._profile_area({"type": "rect", "length": 20, "width": 10}) == 200
    assert sketch._profile_area({"type": "circle", "radius": 3}) == pytest.approx(9 * math.pi)
    assert sketch._profile_area({"type": "slot", "length": 10, "width": 4}) == \
        pytest.approx(40 + math.pi * 4)
    # shoelace：直角三角形 (0,0)(10,0)(0,8) → 40
    assert sketch._profile_area({"type": "polygon",
                                 "points": [[0, 0], [10, 0], [0, 8]]}) == pytest.approx(40)
