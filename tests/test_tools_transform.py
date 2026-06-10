# tests/test_tools_transform.py
"""transform：校验矩阵快测（不碰 FreeCAD）。"""
import pytest

from vibecad.tools import transform


class _NoopSession:
    pass


@pytest.mark.parametrize("kwargs,msg", [
    ({"name": "", "position": [0, 0, 0]}, "name"),
    ({"name": "Box", "position": [0, 0]}, "position"),
    ({"name": "Box", "position": ["a", 0, 0]}, "position"),
    ({"name": "Box", "position": [float("nan"), 0, 0]}, "position"),
])
def test_move_part_validation(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        transform.move_part(_NoopSession(), **kwargs)


@pytest.mark.parametrize("kwargs,msg", [
    ({"name": "", "axis": "z", "angle": 90}, "name"),
    ({"name": "Box", "axis": "w", "angle": 90}, "axis"),
    ({"name": "Box", "axis": "z", "angle": 0}, "angle"),
    ({"name": "Box", "axis": "z", "angle": 360}, "angle"),
    ({"name": "Box", "axis": "z", "angle": float("nan")}, "angle"),
])
def test_rotate_part_validation(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        transform.rotate_part(_NoopSession(), **kwargs)
