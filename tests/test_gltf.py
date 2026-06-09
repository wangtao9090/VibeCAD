import numpy as np
import pytest
from pygltflib import GLTF2

from vibecad.feedback import gltf


def test_build_glb_two_parts(tmp_path):
    parts = [
        {"verts": [(0, 0, 0), (1, 0, 0), (0, 1, 0)], "facets": [(0, 1, 2)],
         "extras": {"face_index": 0, "geom_type": "Plane"}},
        {"verts": [(0, 0, 1), (1, 0, 1), (0, 1, 1)], "facets": [(0, 1, 2)],
         "extras": {"face_index": 1, "geom_type": "Cylinder"}},
    ]
    glb = gltf.build_glb(parts)
    assert glb[:4] == b"glTF"
    p = tmp_path / "t.glb"
    p.write_bytes(glb)
    g = GLTF2().load(str(p))
    prims = g.meshes[0].primitives
    assert len(prims) == 2
    assert prims[0].extras["face_index"] == 0
    assert prims[1].extras["geom_type"] == "Cylinder"
    # 验证第 2 个 primitive 的 POSITION 几何数据（证 multi-part buffer 偏移正确）
    blob = g.binary_blob()
    acc = g.accessors[g.meshes[0].primitives[1].attributes.POSITION]
    bv = g.bufferViews[acc.bufferView]
    raw = blob[bv.byteOffset:bv.byteOffset + bv.byteLength]
    data = np.frombuffer(raw, dtype=np.float32).reshape(-1, 3)
    assert np.allclose(data, [(0, 0, 1), (1, 0, 1), (0, 1, 1)])


def test_build_glb_empty_ok():
    glb = gltf.build_glb([])
    assert glb[:4] == b"glTF"


def test_export_gltf_raises_on_empty_shape(tmp_path):
    """空 Faces 列表 → 几何断言失败，且文件不应被写入。"""

    class FakeShape:
        Faces = []

    out_path = tmp_path / "x.glb"
    with pytest.raises(RuntimeError, match="几何断言失败"):
        gltf.export_gltf(FakeShape(), str(out_path))
    assert not out_path.exists()
