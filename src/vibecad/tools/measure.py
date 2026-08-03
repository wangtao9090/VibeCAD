"""只读几何测量：整体属性、最短距离、平面夹角与两平行面间距。"""
from __future__ import annotations

import math
from typing import Any

from vibecad.engine.session import Session

_KINDS = {"summary", "distance", "angle", "thickness"}
_ENTITIES = {"object", "face", "edge"}


def _vector(v: Any) -> list[float]:
    return [float(v.x), float(v.y), float(v.z)]


def _global_object_shape(session: Session, name: str | None) -> tuple[Any, str]:
    if name is None:
        return session.get_assembly_shape(), "assembly" if session._parts else "result"
    if not isinstance(name, str) or not name:
        raise ValueError("对象引用必须是非空字符串")
    try:
        obj = session.get_object(name)
    except KeyError as exc:
        raise ValueError(f"对象 {name!r} 不存在") from exc
    shape = getattr(obj, "Shape", None)
    if shape is None or not session._is_result_candidate(obj):
        raise ValueError(f"对象 {name!r} 没有有效实体 Shape")
    owner = session.owner_of(name)
    if session._parts:
        if owner is None:
            raise RuntimeError(f"对象 {name!r} 未归属任何零件——项目状态异常")
        shape = shape.transformed(session._parts[owner]["container"].Placement.toMatrix())
    return shape, name


def _global_result_shape(session: Session, part: str | None) -> Any:
    shape = session.get_result_shape(part)
    if not session._parts:
        return shape
    key = part if part is not None else session.active_part
    if key not in session._parts:
        raise ValueError(f"零件 {key!r} 不存在（已有零件：{session.part_names()}）")
    return shape.transformed(session._parts[key]["container"].Placement.toMatrix())


def _label_entity(session: Session, label: str | None, entity: str,
                  part: str | None) -> tuple[Any, str]:
    if not isinstance(label, str) or not label:
        raise ValueError(f"{entity} 标签必须是非空字符串")
    shape = _global_result_shape(session, part)
    if entity == "face":
        index = session.resolve_face(label, part=part)
        return shape.Faces[index], label
    if entity == "edge":
        index = session.resolve_edge(label, part=part)
        return shape.Edges[index], label
    raise ValueError(f"标签实体只能是 face 或 edge（得到 {entity!r}）")


def _entity(session: Session, ref: str | None, entity: str,
            part: str | None) -> tuple[Any, str]:
    if entity == "object":
        return _global_object_shape(session, ref)
    return _label_entity(session, ref, entity, part)


def _distance(a: Any, b: Any) -> tuple[float, list[list[list[float]]]]:
    try:
        distance, point_pairs, _infos = a.distToShape(b)
    except Exception as exc:  # OCCError 在不同 FreeCAD 构建中类型路径不同
        raise RuntimeError(f"几何距离计算失败：{exc}") from exc
    pairs = [[_vector(p1), _vector(p2)] for p1, p2 in point_pairs]
    if not math.isfinite(float(distance)) or not pairs:
        raise RuntimeError("几何距离计算没有返回有效最近点")
    return float(distance), pairs


def _planar_normal(face: Any) -> Any:
    if type(face.Surface).__name__ != "Plane":
        raise ValueError("angle/thickness 只支持平面标签")
    u0, u1, v0, v1 = face.ParameterRange
    normal = face.normalAt((u0 + u1) / 2.0, (v0 + v1) / 2.0)
    normal.normalize()
    return normal


def _plane_reference(face: Any) -> Any:
    """返回严格位于平面上的代表点；平面间距不能用有限 Face 的边缘最短距。"""
    center = getattr(face, "CenterOfMass", None)
    if center is not None:
        return center
    u0, u1, v0, v1 = face.ParameterRange
    return face.valueAt((u0 + u1) / 2.0, (v0 + v1) / 2.0)


def _center_mm(shape: Any) -> list[float]:
    """Shape 有质心则直接用；FreeCAD Compound 需按 solid 体积加权。"""
    center = getattr(shape, "CenterOfMass", None)
    if center is not None:
        return _vector(center)
    weighted = [0.0, 0.0, 0.0]
    total = 0.0
    for solid in getattr(shape, "Solids", []) or []:
        volume = float(getattr(solid, "Volume", 0.0))
        solid_center = getattr(solid, "CenterOfMass", None)
        if volume <= 0 or solid_center is None:
            continue
        total += volume
        weighted[0] += volume * float(solid_center.x)
        weighted[1] += volume * float(solid_center.y)
        weighted[2] += volume * float(solid_center.z)
    if total > 0:
        return [component / total for component in weighted]
    bb = shape.BoundBox
    return [
        float((bb.XMin + bb.XMax) / 2.0),
        float((bb.YMin + bb.YMax) / 2.0),
        float((bb.ZMin + bb.ZMax) / 2.0),
    ]


def _summary(shape: Any, target: str) -> dict[str, Any]:
    bb = shape.BoundBox
    return {
        "ok": True,
        "kind": "summary",
        "target": target,
        "units": {"length": "mm", "area": "mm^2", "volume": "mm^3"},
        "volume_mm3": float(shape.Volume),
        "area_mm2": float(shape.Area),
        "edge_length_mm": float(sum(edge.Length for edge in shape.Edges)),
        "bbox_mm": {
            "min": [float(bb.XMin), float(bb.YMin), float(bb.ZMin)],
            "max": [float(bb.XMax), float(bb.YMax), float(bb.ZMax)],
            "size": [float(bb.XLength), float(bb.YLength), float(bb.ZLength)],
        },
        "center_mm": _center_mm(shape),
        "solids": len(shape.Solids),
        "faces": len(shape.Faces),
        "edges": len(shape.Edges),
    }


def measure(
    session: Session,
    kind: str = "summary",
    first: str | None = None,
    second: str | None = None,
    entity: str = "object",
    part_a: str | None = None,
    part_b: str | None = None,
) -> dict[str, Any]:
    """执行可靠、显式的几何测量。

    - summary：``first`` 省略时量当前结果/装配，也可指定对象名；
    - distance：两个 object/face/edge 的最短距离；
    - angle：两个平面标签的法线角与无方向平面夹角；
    - thickness：两个明确选定的平行平面标签间距（不做不可靠自动猜厚）。
    """
    session._require_doc()
    if kind not in _KINDS:
        raise ValueError(f"kind 必须是 {sorted(_KINDS)}（得到 {kind!r}）")
    if entity not in _ENTITIES:
        raise ValueError(f"entity 必须是 {sorted(_ENTITIES)}（得到 {entity!r}）")

    if kind == "summary":
        if second is not None:
            raise ValueError("summary 不接受 second")
        if entity != "object":
            raise ValueError("summary 的 entity 必须是 object")
        shape, target = _global_object_shape(session, first)
        return _summary(shape, target)

    if first is None or second is None:
        raise ValueError(f"{kind} 必须同时提供 first 和 second")
    if kind in ("angle", "thickness") and entity != "face":
        raise ValueError(f"{kind} 的 entity 必须是 face")

    a, a_name = _entity(session, first, entity, part_a)
    b, b_name = _entity(session, second, entity, part_b)
    if kind == "distance":
        distance, pairs = _distance(a, b)
        return {
            "ok": True,
            "kind": kind,
            "entity": entity,
            "first": a_name,
            "second": b_name,
            "distance_mm": distance,
            "closest_points_mm": pairs,
            "solutions": len(pairs),
        }

    n1, n2 = _planar_normal(a), _planar_normal(b)
    dot = max(-1.0, min(1.0, float(n1.dot(n2))))
    normal_angle = math.degrees(math.acos(dot))
    plane_angle = min(normal_angle, 180.0 - normal_angle)
    if kind == "angle":
        return {
            "ok": True,
            "kind": kind,
            "first": a_name,
            "second": b_name,
            "normal_angle_deg": normal_angle,
            "plane_angle_deg": plane_angle,
        }

    if plane_angle > 1e-5:
        raise ValueError(
            f"thickness 要求两个面平行（当前无方向夹角 {plane_angle:.6g}°）")
    p1, p2 = _plane_reference(a), _plane_reference(b)
    dx, dy, dz = p2.x - p1.x, p2.y - p1.y, p2.z - p1.z
    distance = abs(dx * n1.x + dy * n1.y + dz * n1.z)
    if distance <= 1e-9:
        raise ValueError("两个平面相交或重合，不能形成正厚度")
    return {
        "ok": True,
        "kind": kind,
        "first": a_name,
        "second": b_name,
        "thickness_mm": distance,
        "reference_points_mm": [_vector(p1), _vector(p2)],
    }
