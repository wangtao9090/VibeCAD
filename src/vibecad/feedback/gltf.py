"""glTF (.glb) 导出：逐面 tessellate → 每面一 primitive，extras 写面级元数据（spec D6）。
pygltflib/numpy 仅在函数内 import。"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def build_glb(parts: list[dict]) -> bytes:
    """每个 part → 一个 mesh primitive（POSITION + indices），extras 写入面级元数据。

    parts=[{"verts":[(x,y,z)...], "facets":[(i,j,k)...], "extras":{...}}]
    返回 .glb 二进制（以 b"glTF" 开头）。
    """
    import numpy as np  # noqa: PLC0415
    from pygltflib import (  # noqa: PLC0415
        ARRAY_BUFFER,
        ELEMENT_ARRAY_BUFFER,
        FLOAT,
        GLTF2,
        SCALAR,
        UNSIGNED_INT,
        VEC3,
        Accessor,
        Attributes,
        Buffer,
        BufferView,
        Mesh,
        Node,
        Primitive,
        Scene,
    )

    blob = bytearray()
    buffer_views: list = []
    accessors: list = []
    primitives: list = []
    for part in parts:
        if not part["verts"] or not part.get("facets"):
            continue  # 跳过空 verts 或空 facets 的 part，防止生成无效 glb
        verts = np.asarray(part["verts"], dtype=np.float32)
        idx = np.asarray(part["facets"], dtype=np.uint32).reshape(-1)
        pos_bytes = verts.tobytes()
        idx_bytes = idx.tobytes()
        pos_off = len(blob)
        blob += pos_bytes
        idx_off = len(blob)
        blob += idx_bytes

        pos_bv = len(buffer_views)
        buffer_views.append(
            BufferView(
                buffer=0,
                byteOffset=pos_off,
                byteLength=len(pos_bytes),
                target=ARRAY_BUFFER,
            )
        )
        idx_bv = len(buffer_views)
        buffer_views.append(
            BufferView(
                buffer=0,
                byteOffset=idx_off,
                byteLength=len(idx_bytes),
                target=ELEMENT_ARRAY_BUFFER,
            )
        )

        pos_acc = len(accessors)
        accessors.append(
            Accessor(
                bufferView=pos_bv,
                componentType=FLOAT,
                count=int(len(verts)),
                type=VEC3,
                min=verts.min(axis=0).tolist(),
                max=verts.max(axis=0).tolist(),
            )
        )
        idx_acc = len(accessors)
        accessors.append(
            Accessor(
                bufferView=idx_bv,
                componentType=UNSIGNED_INT,
                count=int(idx.size),
                type=SCALAR,
            )
        )
        primitives.append(
            Primitive(
                attributes=Attributes(POSITION=pos_acc),
                indices=idx_acc,
                extras=part.get("extras"),
            )
        )

    gltf = GLTF2(
        scenes=[Scene(nodes=[0])],
        nodes=[Node(mesh=0)],
        meshes=[Mesh(primitives=primitives)],
        accessors=accessors,
        bufferViews=buffer_views,
        buffers=[Buffer(byteLength=len(blob))],
    )
    gltf.set_binary_blob(bytes(blob))
    # save_to_bytes() 在 pygltflib 1.x 返回 list[bytes]，join 后得到合法 glb。
    glb = gltf.save_to_bytes()
    return glb if isinstance(glb, bytes) else b"".join(glb)


def export_gltf(shape: Any, path: str, *, doc_name: str = "part") -> str:
    """逐 face tessellate → build_glb → 写 .glb。

    每 face 一 primitive，extras={face_index, geom_type}。
    """
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415

    parts: list[dict] = []
    with silence_fd1():
        for i, face in enumerate(shape.Faces):
            fv, ff = face.tessellate(0.1)
            verts_list = [(p.x, p.y, p.z) for p in fv]
            facets_list = [tuple(t) for t in ff]
            if not verts_list or not facets_list:
                continue  # 跳过退化面（无顶点/无三角形）
            parts.append(
                {
                    "verts": verts_list,
                    "facets": facets_list,
                    "extras": {
                        "part": doc_name,
                        "face_index": i,
                        "geom_type": type(face.Surface).__name__,
                    },
                }
            )
    if not parts:
        raise RuntimeError("几何断言失败：形状无可镶嵌面，glTF 导出中止")
    Path(path).write_bytes(build_glb(parts))
    return path
