# src/vibecad/engine/naming.py
"""标签注册表的指纹核心：面/边几何指纹 + 容差匹配（解 FreeCAD 重算后索引重排）。
纯逻辑模块——不 import FreeCAD（吃 duck-typed 几何对象），dev venv 可快测。"""
from __future__ import annotations

import string
from typing import Any


class LabelExpiredError(ValueError):
    """标签指代的几何已变更或无法唯一匹配——需重新标注。"""


def face_fingerprint(face: Any) -> dict:
    surface = type(face.Surface).__name__
    c = face.CenterOfMass
    fp: dict[str, Any] = {
        "kind": "Face", "surface": surface, "area": float(face.Area),
        "center": (float(c.x), float(c.y), float(c.z)), "axis": None,
    }
    ax = getattr(face.Surface, "Axis", None)  # Plane 法向 / Cylinder 轴向
    if ax is not None:
        fp["axis"] = (float(ax.x), float(ax.y), float(ax.z))
    if surface == "Cylinder":
        fp["radius"] = float(face.Surface.Radius)
    return fp


def edge_fingerprint(edge: Any) -> dict:
    c = edge.CenterOfMass
    return {"kind": "Edge", "curve": type(edge.Curve).__name__,
            "length": float(edge.Length),
            "midpoint": (float(c.x), float(c.y), float(c.z))}


def _vec_close(a, b, tol: float) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def _axis_match(a, b) -> bool:
    if (a is None) != (b is None):
        return False
    if a is None:
        return True
    return _vec_close(a, b, 1e-6) or _vec_close([-x for x in a], b, 1e-6)  # 定向不稳，反号同面


def match_face(fp: dict, faces: list[Any], *, tol: float = 1e-3) -> int:
    """在当前 faces 中找指纹唯一匹配的索引；0 或多个命中 → LabelExpiredError。"""
    hits = []
    for i, f in enumerate(faces):
        cand = face_fingerprint(f)
        if cand["surface"] != fp["surface"]:
            continue
        if abs(cand["area"] - fp["area"]) > max(tol, 1e-3 * abs(fp["area"])):
            continue
        if not _vec_close(cand["center"], fp["center"], tol):
            continue
        if not _axis_match(cand["axis"], fp["axis"]):
            continue
        hits.append(i)
    if len(hits) != 1:
        raise LabelExpiredError(
            f"面标签无法唯一匹配当前几何（命中 {len(hits)} 个）——几何可能已变更，"
            "请重新调用 render_part(annotate='faces') 获取最新标注")
    return hits[0]


def match_edge(fp: dict, edges: list[Any], *, tol: float = 1e-3) -> int:
    hits = []
    for i, e in enumerate(edges):
        cand = edge_fingerprint(e)
        if cand["curve"] != fp["curve"]:
            continue
        if abs(cand["length"] - fp["length"]) > max(tol, 1e-3 * abs(fp["length"])):
            continue
        if not _vec_close(cand["midpoint"], fp["midpoint"], tol):
            continue
        hits.append(i)
    if len(hits) != 1:
        raise LabelExpiredError(
            f"边标签无法唯一匹配当前几何（命中 {len(hits)} 个）——几何可能已变更，"
            "请重新调用 render_part(annotate='edges') 获取最新标注")
    return hits[0]


_SIDES = {0: ("左面", "右面"), 1: ("前面", "后面"), 2: ("底面", "顶面")}


def semantic_name(fp: dict, bbox: tuple) -> str | None:
    """轴对齐平面且贴包围盒边界 → 顶/底/前/后/左/右面；其余 None。
    bbox=(xmin,ymin,zmin,xmax,ymax,zmax)。"""
    if fp["surface"] != "Plane" or not fp["axis"]:
        return None
    ax = fp["axis"]
    i = max(range(3), key=lambda k: abs(ax[k]))
    if abs(ax[i]) < 0.99:
        return None
    lo, hi = bbox[i], bbox[i + 3]
    span = max(hi - lo, 1e-9)
    c = fp["center"][i]
    if abs(c - hi) <= 1e-6 + 1e-3 * span:
        return _SIDES[i][1]
    if abs(c - lo) <= 1e-6 + 1e-3 * span:
        return _SIDES[i][0]
    return None


def face_summary(fp: dict, bbox: tuple) -> str:
    """指纹 → 给 AI/用户读的一行描述（标签表内容）。"""
    sem = semantic_name(fp, bbox)
    if fp["surface"] == "Plane":
        head = f"{sem}·平面" if sem else "平面"
        return f"{head} 面积{fp['area']:.0f}mm² 中心{tuple(round(v, 1) for v in fp['center'])}"
    if fp["surface"] == "Cylinder":
        return f"圆柱面 r={fp.get('radius', 0):g}mm 中心{tuple(round(v, 1) for v in fp['center'])}"
    return f"{fp['surface']} 面积{fp['area']:.0f}mm²"


def edge_summary(fp: dict) -> str:
    kind = {"Line": "直线边", "Circle": "圆边"}.get(fp["curve"], fp["curve"])
    return f"{kind} 长{fp['length']:.1f}mm 中点{tuple(round(v, 1) for v in fp['midpoint'])}"


def face_labels(n: int) -> list[str]:
    """A..Z, AA, AB…（面标签序列）。"""
    out = []
    for i in range(n):
        s, k = "", i
        while True:
            s = string.ascii_uppercase[k % 26] + s
            k = k // 26 - 1
            if k < 0:
                break
        out.append(s)
    return out


def edge_labels(n: int) -> list[str]:
    return [f"E{i + 1}" for i in range(n)]
