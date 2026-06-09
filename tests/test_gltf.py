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


def test_build_glb_empty_ok():
    glb = gltf.build_glb([])
    assert glb[:4] == b"glTF"
