"""Deterministic one-page assembly drawing for the bounded P2 release slice."""

from __future__ import annotations

import io
from collections.abc import Mapping
from typing import Any

from vibecad.feedback.multiview import _VIEW_TFS, project_view
from vibecad.validation import BomObservation

DRAWING_VIEWS = ("front", "right", "top", "isometric")
MAX_RELEASE_DRAWING_BYTES = 160_000
_ISO_DIRECTION = (1.0, -1.0, 1.0)


def _identity_2d(x: float, y: float) -> tuple[float, float]:
    return x, y


def _points(view: Mapping[str, object]) -> tuple[tuple[float, float], ...]:
    result: list[tuple[float, float]] = []
    for key in ("vis", "hid"):
        polylines = view.get(key)
        if type(polylines) is not list:
            raise ValueError("invalid drawing projection")
        for polyline in polylines:
            if type(polyline) is not list:
                raise ValueError("invalid drawing projection")
            for point in polyline:
                if (
                    type(point) not in {tuple, list}
                    or len(point) != 2
                    or type(point[0]) not in {int, float}
                    or type(point[1]) not in {int, float}
                ):
                    raise ValueError("invalid drawing projection")
                result.append((float(point[0]), float(point[1])))
    if not result:
        raise ValueError("empty drawing projection")
    return tuple(result)


def _bbox(view: Mapping[str, object]) -> tuple[float, float, float, float]:
    points = _points(view)
    xs = tuple(point[0] for point in points)
    ys = tuple(point[1] for point in points)
    return min(xs), min(ys), max(xs), max(ys)


def _draw_projection(axis: Any, view: Mapping[str, object], title: str) -> None:
    for key, color, width, linestyle in (
        ("hid", "#8a8a8a", 0.45, (0, (4, 3))),
        ("vis", "#111111", 0.75, "solid"),
    ):
        polylines = view[key]
        for polyline in polylines:
            xs, ys = zip(*polyline, strict=True)
            axis.plot(xs, ys, color=color, linewidth=width, linestyle=linestyle)
    axis.set_aspect("equal")
    axis.axis("off")
    axis.set_title(title, fontsize=8, pad=2)


def assembly_drawing_pdf(
    *,
    views: Mapping[str, Mapping[str, object]],
    component_isometric_views: Mapping[str, Mapping[str, object]],
    bom: BomObservation,
    project_id: str,
    revision_id: str,
) -> tuple[bytes, tuple[tuple[int, str], ...]]:
    """Render one A3 landscape PDF and return stable item-to-component balloons."""

    if (
        type(bom) is not BomObservation
        or not bom.complete
        or not bom.rows
        or type(project_id) is not str
        or not project_id
        or type(revision_id) is not str
        or not revision_id
        or tuple(views) != DRAWING_VIEWS
    ):
        raise ValueError("release drawing input is incomplete")
    balloon_items = tuple(
        (index, row.component_ids[0]) for index, row in enumerate(bom.rows, start=1)
    )
    if set(component_isometric_views) != set(bom.component_ids):
        raise ValueError("component projections do not match the BOM")
    view_boxes = {name: _bbox(view) for name, view in views.items()}
    component_boxes = {
        component_id: _bbox(component_isometric_views[component_id])
        for component_id in bom.component_ids
    }

    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    figure = plt.figure(figsize=(16.54, 11.69))
    try:
        grid = figure.add_gridspec(
            3,
            4,
            width_ratios=(1.0, 1.0, 1.0, 1.18),
            height_ratios=(1.0, 1.0, 0.22),
            hspace=0.2,
            wspace=0.12,
        )
        axes = {
            "front": figure.add_subplot(grid[0, 0:2]),
            "right": figure.add_subplot(grid[0, 2]),
            "top": figure.add_subplot(grid[1, 0:2]),
            "isometric": figure.add_subplot(grid[1, 2]),
        }
        titles = {
            "front": "FRONT",
            "right": "RIGHT",
            "top": "TOP",
            "isometric": "ISOMETRIC",
        }
        span = max(max(x1 - x0, y1 - y0) for x0, y0, x1, y1 in view_boxes.values())
        if span <= 0:
            raise ValueError("degenerate drawing projection")
        for name in DRAWING_VIEWS:
            axis = axes[name]
            _draw_projection(axis, views[name], titles[name])
            x0, y0, x1, y1 = view_boxes[name]
            center_x = (x0 + x1) / 2
            center_y = (y0 + y1) / 2
            axis.set_xlim(center_x - span * 0.62, center_x + span * 0.62)
            axis.set_ylim(center_y - span * 0.62, center_y + span * 0.62)

        iso_axis = axes["isometric"]
        for item_number, component_id in balloon_items:
            x0, y0, x1, y1 = component_boxes[component_id]
            anchor = ((x0 + x1) / 2, (y0 + y1) / 2)
            side = -1 if item_number % 2 else 1
            vertical = ((item_number - 1) % 5 - 2) * span * 0.08
            label = (anchor[0] + side * span * 0.2, anchor[1] + vertical)
            iso_axis.annotate(
                str(item_number),
                xy=anchor,
                xytext=label,
                ha="center",
                va="center",
                fontsize=7,
                bbox={"boxstyle": "circle,pad=0.22", "fc": "white", "ec": "#111111"},
                arrowprops={"arrowstyle": "-", "color": "#111111", "linewidth": 0.6},
            )

        bom_axis = figure.add_subplot(grid[0:2, 3])
        bom_axis.axis("off")
        table_rows = [
            [
                str(index),
                row.part_number,
                row.description,
                row.material,
                str(row.quantity),
                f"{float(row.total_mass_kg):.6g}",
            ]
            for index, row in enumerate(bom.rows, start=1)
        ]
        table = bom_axis.table(
            cellText=table_rows,
            colLabels=("ITEM", "PART NUMBER", "DESCRIPTION", "MATERIAL", "QTY", "MASS kg"),
            colWidths=(0.08, 0.21, 0.27, 0.2, 0.08, 0.16),
            cellLoc="left",
            colLoc="left",
            loc="upper center",
        )
        table.auto_set_font_size(False)
        table.set_fontsize(5.5)
        table.scale(1.0, 1.35)
        bom_axis.set_title("FLAT BILL OF MATERIALS", fontsize=8, pad=8)

        title_axis = figure.add_subplot(grid[2, :])
        title_axis.axis("off")
        title = title_axis.table(
            cellText=(
                ("PROJECT", project_id, "REVISION", revision_id),
                ("DRAWING", "ASSEMBLY", "SCALE", "AUTO   SHEET 1/1"),
            ),
            colWidths=(0.1, 0.4, 0.1, 0.4),
            cellLoc="left",
            loc="center",
        )
        title.auto_set_font_size(False)
        title.set_fontsize(7)
        title.scale(1.0, 1.25)

        output = io.BytesIO()
        figure.savefig(
            output,
            format="pdf",
            metadata={
                "Title": "VibeCAD Assembly Drawing",
                "Author": "VibeCAD",
                "Creator": "VibeCAD",
                "CreationDate": None,
                "ModDate": None,
            },
        )
        raw = output.getvalue()
    finally:
        plt.close(figure)
    if (
        not raw.startswith(b"%PDF-")
        or b"%%EOF" not in raw[-32:]
        or len(raw) > MAX_RELEASE_DRAWING_BYTES
    ):
        raise ValueError("release drawing exceeds the bounded PDF contract")
    return raw, balloon_items


def render_assembly_drawing(
    session: object,
    *,
    bom: BomObservation,
    project_id: str,
    revision_id: str,
) -> tuple[bytes, tuple[tuple[int, str], ...]]:
    """Project one live/reloaded managed assembly and render its release PDF."""

    records = tuple(session.list_component_identity_records())  # type: ignore[attr-defined]
    if not records or tuple(record[2].object_id for record in records) != bom.component_ids:
        raise ValueError("managed assembly does not match the BOM")
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415

    with silence_fd1():
        assembly_shape = session.get_assembly_shape()  # type: ignore[attr-defined]
        views = {
            name: project_view(assembly_shape, direction, transform)
            for name, (direction, transform) in _VIEW_TFS.items()
        }
        views["isometric"] = project_view(assembly_shape, _ISO_DIRECTION, _identity_2d)
        component_views = {}
        for part_name, container, identity, _members in records:
            shape = session.get_result_shape(part_name).transformed(  # type: ignore[attr-defined]
                container.Placement.toMatrix()
            )
            component_views[identity.object_id] = project_view(
                shape,
                _ISO_DIRECTION,
                _identity_2d,
            )
    ordered_views = {name: views[name] for name in DRAWING_VIEWS}
    return assembly_drawing_pdf(
        views=ordered_views,
        component_isometric_views=component_views,
        bom=bom,
        project_id=project_id,
        revision_id=revision_id,
    )
