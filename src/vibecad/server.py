"""VibeCAD MCP server（FastMCP, stdio）。握手必须秒回：模块级不 import FreeCAD、不下载。"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations

from vibecad import __version__
from vibecad.engine.session import Session
from vibecad.feedback import annotate as _annotate
from vibecad.feedback import multiview as _multiview
from vibecad.feedback import persist as _persist
from vibecad.feedback import render as _render
from vibecad.feedback import text as _feedback_text
from vibecad.freecad_env import (
    prepare_freecad_import as _prepare_freecad_import,
)
from vibecad.freecad_env import (
    silence_fd1 as _silence_fd1,
)
from vibecad.runtime import paths, status
from vibecad.runtime import uninstall as _uninstall
from vibecad.runtime.installer import RuntimeInstaller
from vibecad.supervisor import SWAP_EXIT, runtime_swappable  # 单向依赖；换芯判据唯一真源（C1）
from vibecad.tools import assembly as _assembly
from vibecad.tools import export as _export
from vibecad.tools import features as _features
from vibecad.tools import modeling as _modeling
from vibecad.tools import modify as _modify
from vibecad.tools import sketch as _sketch
from vibecad.tools import transform as _transform

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # m10：杜绝隐式拉起 GUI

mcp = FastMCP("vibecad")
mcp._mcp_server.version = __version__  # FastMCP 不透传 version；勿用构造参数（1.27.2 TypeError）
_installer = RuntimeInstaller()  # 进度由 installer 落 status.json，server 读盘
_session = Session()  # 跨 MCP 调用维持同一活动文档（单零件先行）；构造不 import FreeCAD
_install_thread: threading.Thread | None = None

# Round 11 换芯自退：运行时装好后 server 主动退出，由监督进程重启进 conda 解释器。
# Q2 已锁 C 分支（宿主对意外退出不自动重启，见计划 Spike 结果节）：supervisor 见
# SWAP_EXIT=75（顶部 import）即换芯重启子进程并重放握手；真退出/崩溃用其他码原样透传给宿主。
_swap_timer: threading.Timer | None = None


def _in_conda_runtime() -> bool:
    """当前进程是否就是 conda 运行时 python（决定能否进程内 import FreeCAD）。"""
    try:
        return os.path.realpath(sys.executable) == os.path.realpath(paths.active_runtime_python())
    except OSError:
        return False


def _schedule_swap(delay: float = 1.0) -> None:
    """运行时就绪但本进程仍是引导解释器 → 延迟自退换芯（延迟给当前响应 flush 留时间）。
    幂等：已安排则不叠加。仅经 _try_schedule_swap 调用（判据 + 监督检查的唯一入口）。
    C 分支：监督进程见 SWAP_EXIT 即换 conda python 重启子进程并重放握手。

    I5 竞态说明：Timer 到点的 os._exit(SWAP_EXIT) 与进程真崩溃存在竞态——真崩溃恰
    在此刻可能被 75 掩盖成一次「换芯」。有意接受不加同步：概率极低，且反复形态会被
    supervisor 的换芯循环护栏拦住并响亮退出。"""
    global _swap_timer
    if _swap_timer is not None:
        return
    _swap_timer = threading.Timer(delay, os._exit, args=(SWAP_EXIT,))
    _swap_timer.daemon = True
    _swap_timer.start()


def _supervised() -> bool:
    """本进程是否由监督进程拉起（I4，见 supervisor._spawn 的 env 注入）。裸 server
    （直接 python -m vibecad.server）自杀后无人重启 = 服务凭空消失，绝不自杀。"""
    return os.environ.get("VIBECAD_SUPERVISED") == "1"


def _try_schedule_swap() -> bool:
    """换芯触发点唯一入口：可换芯则安排自退并返回 True；False = 调用方给诚实回退
    （needs_reconnect / 提示重连文案）。判据两条，缺一不可：
    ① runtime_swappable()（C1）——与 supervisor._server_cmd 同一真源；哨兵在而
      conda python 缺失时若照旧自杀，supervisor 只会落回 bootstrap 再自杀，无限循环；
    ② _supervised()（I4）——裸 server 自杀无人重启。"""
    if not (runtime_swappable() and _supervised()):
        return False
    _schedule_swap()
    return True


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def ping() -> str:
    """连通性自检。"""
    return f"vibecad ok (v{__version__})"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_runtime_status() -> dict[str, Any]:
    """查询 FreeCAD 运行时安装进度（跨进程读 status.json）。"""
    d = status.read_status().to_dict()
    d["needs_reconnect"] = False  # Round 11：自退换芯后客户端零感知（字段保留做兼容）
    if status.runtime_ready() and not _in_conda_runtime() and not _try_schedule_swap():
        # 不能自动换芯（裸 server 无人重启 / conda python 缺失）：诚实告知需重连（I4/C1）
        d["needs_reconnect"] = True
    return d


def _spawn_install() -> None:
    global _install_thread
    if _install_thread and _install_thread.is_alive():
        return
    _install_thread = threading.Thread(target=_safe_install, name="vibecad-install", daemon=True)
    _install_thread.start()


def _safe_install() -> None:
    try:
        _installer.install()
    except Exception:  # noqa: BLE001 - 失败态已落 status.json
        pass
    else:
        # 安装成功即安排自退换芯——用户全程不开口也自动获得 CAD 能力（Round 11）。
        # 不可换芯时此处无对话通道，留给 get_runtime_status/_runtime_guard 诚实反馈。
        if not _in_conda_runtime():
            _try_schedule_swap()


def _ensure_runtime_impl() -> dict[str, Any]:
    if _installer.is_ready():
        msg = "FreeCAD 运行时已就绪"
        if not _in_conda_runtime():
            if _try_schedule_swap():  # Round 11：自退换芯替代手动重连
                msg += "；正在自动切换到运行时解释器，数秒后即可直接使用 CAD 能力"
            else:  # I4/C1 回退：不能自杀时诚实提示重连
                msg += "；需重启/重连 vibecad MCP 连接后方可使用 CAD 能力"
        return {"status": "ready", "message": msg}
    if _install_thread and _install_thread.is_alive():
        return {"status": "in_progress", "message": "安装进行中，请轮询 get_runtime_status"}
    _spawn_install()
    return {
        "status": "started",
        "message": "已开始后台安装 FreeCAD 运行时（约 2-3GB），请轮询 get_runtime_status",
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def ensure_runtime() -> dict[str, Any]:
    """确保 FreeCAD 运行时就绪：未就绪则后台开始安装并立即返回，用 get_runtime_status 轮询。"""
    return _ensure_runtime_impl()



def _build_box_and_export() -> dict[str, Any]:
    import tempfile
    _prepare_freecad_import()
    out = os.path.join(tempfile.gettempdir(), "vibecad_smoke.step")
    with _silence_fd1():
        import FreeCAD  # noqa: PLC0415 - 懒加载：仅 conda runtime 进程内 import
        import Part  # noqa: PLC0415
        box = Part.makeBox(10, 10, 10)
        box.exportStep(out)
        bb = box.BoundBox
        result = {"ok": True, "volume": box.Volume, "bbox": [bb.XLength, bb.YLength, bb.ZLength],
                  "step": out, "freecad_version": list(FreeCAD.Version())}
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def smoke_cad() -> dict[str, Any]:
    """地基验证：进程内造 10×10×10 Box，导出 STEP，返回体积/包围盒/路径。"""
    # Round 11：复用 _runtime_guard（换芯触发逻辑单点收敛，不再各写一份"请重连"文案）
    guard = _runtime_guard()
    if guard:
        return guard
    return _build_box_and_export()


def _runtime_guard() -> dict[str, Any] | None:
    if not _installer.is_ready():
        st = status.read_status()
        if st.phase == status.Phase.FAILED:
            return {"ok": False, "phase": st.phase.value,
                    "message": f"CAD 引擎安装失败：{st.error or st.message}；"
                               "可调用 ensure_runtime 重试"}
        if st.phase == status.Phase.NOT_STARTED:
            return {"ok": False, "phase": st.phase.value,
                    "message": "CAD 引擎未安装：调用 ensure_runtime 开始（约 2-3GB，仅一次）"}
        return {"ok": False, "phase": st.phase.value, "percent": st.percent,
                "message": f"正在准备 CAD 引擎：{st.message or st.phase.value}"
                           f"（{st.percent:.0f}%）。就绪后自动接管，无需任何手动操作，"
                           "可用 get_runtime_status 看进度"}
    if not _in_conda_runtime():
        if _try_schedule_swap():  # Round 11：自退换芯替代手动重连
            _msg = "运行时已就绪，正在自动切换到运行时解释器（数秒内完成），请稍后重试本操作"
        else:  # I4/C1 回退：裸 server / 运行时不完整时不自杀，诚实提示重连
            _msg = "运行时已就绪，但本进程无法自动切换解释器；请重启/重连 vibecad MCP 连接后重试"
        return {"ok": False, "message": _msg}
    return None


def _build_part_map() -> dict[str, Any] | None:
    """装配模式构造 {零件名: 全局 shape}（容器位姿已应用，与 get_assembly_shape
    的 compound 同坐标系），用于渲染分色与标签表"（零件：X）"归属后缀；
    单零件模式返回 None。调用方需置于 _silence_fd1() 内（transformed 走 OCCT）。
    空零件跳过——必须与 get_assembly_shape 的跳过规则一致（compound 拼接序）。"""
    if not _session._parts:
        return None
    return {
        name: _session.get_result_shape(name).transformed(
            info["container"].Placement.toMatrix())
        for name, info in _session._parts.items() if info["objects"]
    }


def _store_labels(faces_reg: dict, edges_reg: dict, shown: set) -> None:
    """标签快照入库：单零件模式写单命名空间（R7 原行为）。

    装配模式（终审 I-1 死锁修复）：把全 compound 注册表写入**每个零件**的命名
    空间——指纹是全局坐标，resolve 时 _match_shape(part) 用各零件自己的全局
    shape 匹配，天然只命中本零件的面（跨零件标签的指纹在该零件面集中命中 0
    → 照旧响亮失败），不会串扰。此前只写活动零件命名空间：非活动零件被移动后
    其快照永久过期，而重标注永远只刷活动零件——"请重新标注"提示成了死循环
    （server 无 set_active_part 工具）。"""
    if _session._parts:
        for pname in _session._parts:
            _session.set_labels(faces_reg, edges_reg, shown=shown, part=pname)
    else:
        _session.set_labels(faces_reg, edges_reg, shown=shown)


def _edges_of_global_index(ef_idx: int, part_map: dict[str, Any] | None) -> int:
    """resolve_face 返回的是零件**局部**面索引；annotate 消费的是装配 compound
    的**全局**面索引（终审 C-C：错位导致画错零件的边+表注堂而皇之错）。
    装配模式把局部索引平移：全局 = 活动零件之前各零件（part_map 迭代序 =
    compound 拼接序）的面数之和 + 局部索引。单零件模式（part_map None）原样。"""
    if part_map is None:
        return ef_idx
    offset = 0
    for name, p_shape in part_map.items():
        if name == _session.active_part:
            return offset + ef_idx
        offset += len(p_shape.Faces)
    # 活动零件不在 part_map（空零件等异常态）——静默用错索引画错图违反纪律
    raise RuntimeError(
        f"活动零件 {_session.active_part!r} 不在装配渲染序列中——无法定位 edges_of 面")


def _attach_view(result: dict[str, Any], tool: str = "step") -> Any:
    """成功结果附三视图拼图 + 当场刷新标签表；附图失败不连坐（保留操作成功 +
    render_error + 退回 labels_stale 提示）——绝不因附图失败把成功操作报成失败，
    也绝不静默吞掉渲染错误。

    Round 8：改用 get_assembly_shape()（单零件模式等价）；装配模式传入 part_map 给
    render_multiview 用于 iso 格分色和标签表零件归属后缀。
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return result
    try:
        with _silence_fd1():
            shape = _session.get_assembly_shape()
            png, table, faces_reg, edges_reg = _multiview.render_multiview(
                shape, part_map=_build_part_map())
        _store_labels(faces_reg, edges_reg, shown=set(table.keys()))
        result.pop("labels_stale", None)
        result.pop("hint", None)
        try:
            with _silence_fd1():
                result["parts"] = _modify.list_parameters(_session.doc, session=_session)
        except Exception:  # noqa: BLE001 - 参数清单失败不应丢弃已成功的渲染：
            # labels/Image 已就绪，parts 只是辅助清单，兜底空 dict 而非把整个
            # 附图降级到 render_error 路径（消除"渲染成功却报渲染失败"的语义矛盾）
            result["parts"] = {}
        try:
            doc_name = getattr(_session.doc, "Name", "untitled")
            result["view_file"] = _persist.save_view(png, doc_name, tool)
        except Exception as exc:  # noqa: BLE001 - 落盘失败不连坐（与 render_error 同理）
            result["view_file_error"] = f"图已生成但落盘失败：{exc}"
        result["labels"] = table
        # 顺序 [dict, Image] 是有意为之（与 render_part 的 [Image, str] 相反）：
        # 建模返回以结构化结果为主、图为附件；render_part 以图为主。勿"对齐"。
        return [result, Image(data=png, format="png")]
    except Exception as exc:  # noqa: BLE001 - 事务已提交，纯展示阶段刻意宽抓：
        # 此处任何异常（实测 TechDraw 的 TypeError、潜在 ImportError/IndexError）
        # 穿透都会把已成功的操作谎报成 isError，诱发 AI 客户端重试叠出重复对象。
        # 宽抓不违反"绝不静默"——失败本身响亮（render_error 带类型名+文本）。
        # 项目其他处（操作路径/只读 render_part 路径）维持窄抓纪律，此处是唯一例外。
        # setdefault：tools 层带了 labels_stale/hint 就尊重其语义，只兜底补缺
        # （modeling 的"_labels 非空才带 stale"、fillet/chamfer 的 edges hint 不被架空）。
        result.setdefault("labels_stale", True)
        result.setdefault("hint", "几何已变更，调用 render_part(annotate='faces') 查看最新标注")
        result["render_error"] = f"自动渲染失败（{type(exc).__name__}）：{exc}"
        return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def new_document(name: str) -> dict[str, Any]:
    """新建一个 CAD 文档（单零件工作区）。"""
    return _runtime_guard() or _modeling.new_document(_session, name)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def add_box(length: float, width: float, height: float,
            position: list[float] | None = None) -> Any:
    """添加参数化长方体（mm）；position=[x,y,z] 放置位置（默认原点）。成功后自动附三视图拼图。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        result = _modeling.add_box(
            _session, length, width, height,
            position=tuple(position) if position is not None else (0.0, 0.0, 0.0))
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"创建失败：{exc}"}
    return _attach_view(result, tool="add_box")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def add_cylinder(radius: float, height: float,
                 position: list[float] | None = None, axis: str = "z") -> Any:
    """添加参数化圆柱（mm）；position=[x,y,z] 放置位置，axis=x|y|z 圆柱轴向（默认 z）。
    成功后自动附三视图拼图。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        result = _modeling.add_cylinder(
            _session, radius, height,
            position=tuple(position) if position is not None else (0.0, 0.0, 0.0), axis=axis)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"创建失败：{exc}"}
    return _attach_view(result, tool="add_cylinder")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def boolean_cut(base_name: str, tool_name: str) -> Any:
    """布尔差集：从 base 减去 tool，返回结果对象名与体积。成功后自动附三视图拼图。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        result = _modeling.boolean_cut(_session, base_name, tool_name)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"布尔运算失败：{exc}"}
    return _attach_view(result, tool="boolean_cut")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def export_part(output_dir: str, fmt: str = "both", split: bool = False) -> dict[str, Any]:
    """导出当前结果为 STEP/STL/glTF（fmt: step|stl|gltf|both|all）到 output_dir。
    split=True：装配模式时 per-part 导出 STEP（<doc>_<零件名>.step），每文件独立验证。
    单零件模式下 split 被忽略（行为与旧版完全一致）。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        return _export.export_part(_session, output_dir, fmt=fmt, split=split)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"导出失败：{exc}"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def describe_part() -> dict[str, Any]:
    """返回当前结果零件的文本诊断（体积/包围盒/质心/实体数/有效性）。
    装配模式：返回 per-part 摘要 + assembly_bbox + interference 清单。
    单零件模式：原格式不变（体积/包围盒/质心/实体数/有效性）。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        with _silence_fd1():
            # Round 8：装配模式分流（_parts 非空用 describe_assembly，否则原格式）
            if _session._parts:
                return _feedback_text.describe_assembly(_session)
            return _feedback_text.describe_shape(_session.get_result_shape())
    except (RuntimeError, ValueError) as exc:
        # 终审 M-2：无文档/全空装配等态此前异常穿透成 isError——结构化失败
        return {"ok": False, "message": f"描述失败：{exc}"}


def _maybe_save(png: bytes, save_to: str | None) -> dict[str, str]:
    """render_part 的显式另存：失败不连坐（save_error 字段，渲染结果照常返回）。"""
    if not save_to:
        return {}
    try:
        p = Path(save_to).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(png)
        return {"saved": str(p)}
    except OSError as exc:
        return {"save_error": f"保存失败：{exc}"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def render_part(view: str = "iso", annotate: str | None = None,
                edges_of: str | None = None,
                save_to: str | None = None) -> Any:
    """渲染当前零件 PNG（view: iso|front|top|right|back|multi）。
    annotate='faces'：面标注图+标签表+尺寸线（之后可用面标签如 'A' 调 add_hole）；
    annotate='edges'：边标注图（edges_of='A' 只画 A 面的边；
    之后可调 fillet_edges/chamfer_edges）。
    view='multi'：2×2 三视图+标注 iso 拼图（已含标注，不可与 annotate/edges_of 组合）。
    save_to=绝对路径：另存渲染 PNG 到该文件。每步建模返回的 view_file 是自动落盘路径——
    客户端不显示内嵌图时（如 Cowork），可直接用系统命令打开该文件给用户看图。"""
    guard = _runtime_guard()
    if guard:
        return guard
    if edges_of is not None and annotate != "edges":
        _msg = ("edges_of 仅在 annotate='edges' 时有效"
                "——要看某面的边，请 render_part(annotate='edges', edges_of='A')")
        return {"ok": False, "message": _msg}
    if view == "multi":
        if annotate is not None or edges_of is not None:
            return {"ok": False,
                    "message": "view='multi' 已含标注 iso 格，不能与 annotate/edges_of 组合"}
        try:
            with _silence_fd1():
                # Round 8：改用装配 shape；装配模式传入 part_map 用于分色和归属标注
                shape = _session.get_assembly_shape()
                png, table, faces_reg, edges_reg = _multiview.render_multiview(
                    shape, part_map=_build_part_map())
            _store_labels(faces_reg, edges_reg, shown=set(table.keys()))
            return [Image(data=png, format="png"),
                    json.dumps({"ok": True, "labels": table,
                                **_maybe_save(png, save_to)}, ensure_ascii=False)]
        except Exception as exc:  # noqa: BLE001 - 与 _attach_view 同理：同一条
            # render_multiview 渲染链含 TechDraw/matplotlib 深栈（实测有 TypeError），
            # 窄抓会让同一失败在 attach 路径被结构化、在 multi 路径穿透成 isError。
            # 宽抓后一律转结构化 {ok:False}——失败本身响亮（带类型名+文本），不静默。
            return {"ok": False, "message": f"渲染失败（{type(exc).__name__}）：{exc}"}
    try:
        with _silence_fd1():
            # Round 8：改用装配 shape（单零件模式等价）；装配模式 part_map
            # 同样接入 annotate 路径（分色 + 标签表零件归属后缀）
            shape = _session.get_assembly_shape()
            part_map = _build_part_map()
            # is not None（非 falsy）：空串必须进 resolve_face 撞出"未知面标签 ''"响亮失败
            ef_idx = _session.resolve_face(edges_of) if edges_of is not None else None
            if ef_idx is not None:
                # 终审 C-C：局部面索引 → compound 全局面索引（装配模式平移）
                ef_idx = _edges_of_global_index(ef_idx, part_map)
        if annotate is None:
            png = _render.render_png(shape, view=view)
            extra = _maybe_save(png, save_to)
            if extra:
                return [Image(data=png, format="png"),
                        json.dumps({"ok": True, **extra}, ensure_ascii=False)]
            return Image(data=png, format="png")
        png, table, faces_reg, edges_reg = _annotate.render_annotated(
            shape, mode=annotate, edges_of=ef_idx, view=view, part_map=part_map)
        # shown=本次表里实际展示的键：未展示过的标签不可被指认（防 AI 编造盲选）
        _store_labels(faces_reg, edges_reg, shown=set(table.keys()))
        return [Image(data=png, format="png"),
                json.dumps({"ok": True, "labels": table,
                            **_maybe_save(png, save_to)}, ensure_ascii=False)]
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"渲染失败：{exc}"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def add_hole(face: str, diameter: float, depth: float | None = None,
             offset: list[float] | None = None,
             pattern: dict | None = None,
             counterbore_diameter: float | None = None,
             counterbore_depth: float | None = None) -> Any:
    """在指定面打圆孔（face=面标签，来自 render_part(annotate='faces')）。
    depth 省略=通孔；offset=[u,v] 面内毫米偏移（原点=面外边界包络中点，矩形面即
    几何中心，不随已打的孔漂移；省略=原点处）。
    pattern={"type":"linear","count":4,"spacing":10} 或 {"type":"circular","count":6,"radius":18}
    实现线性/圆形阵列；省略=单孔（向后兼容）。
    counterbore_diameter+counterbore_depth 成对提供=沉头孔（孔口同轴切大径浅圆柱，
    "安装孔+沉头槽"一步完成；要求大径>diameter、沉头深<盲孔 depth；阵列时每孔都带沉头）。
    成功后自动附三视图拼图。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        result = _features.add_hole(_session, face, diameter, depth,
                                    tuple(offset) if offset is not None else (0.0, 0.0),
                                    pattern=pattern,
                                    counterbore_diameter=counterbore_diameter,
                                    counterbore_depth=counterbore_depth)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"打孔失败：{exc}"}
    return _attach_view(result, tool="add_hole")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def fillet_edges(edges: list[str], radius: float) -> Any:
    """对边标签列表做圆角（标签来自 render_part(annotate='edges')）。成功后自动附三视图拼图。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        result = _features.fillet_edges(_session, edges, radius)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"圆角失败：{exc}"}
    return _attach_view(result, tool="fillet_edges")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def chamfer_edges(edges: list[str], size: float) -> Any:
    """对边标签列表做倒角（标签来自 render_part(annotate='edges')）。成功后自动附三视图拼图。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        result = _features.chamfer_edges(_session, edges, size)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"倒角失败：{exc}"}
    return _attach_view(result, tool="chamfer_edges")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def modify_part(name: str, parameter: str, value: float) -> Any:
    """修改参数化对象的参数（如 name='Box', parameter='length', value=45）——
    依赖链（布尔/孔/圆角）自动重算。可改对象与参数见每步返回的 parts 字段。
    成功后自动附三视图拼图（工程图尺寸当场更新）。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        result = _modify.modify_part(_session, name, parameter, value)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"参数修改失败：{exc}"}
    return _attach_view(result, tool="modify_part")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def move_part(name: str, position: list[float]) -> Any:
    """把图元移动到绝对位置 [x, y, z]（mm）——依赖链自动重算，成功后自动附三视图。
    可移动对象见 parts 字段（布尔/圆角结果跟随其图元，不可直接移动）。
    对象名来自 parts 字段（如 'Box'、'Cylinder'、'HoleTool'）。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        result = _transform.move_part(_session, name, tuple(position) if position else position)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"移动失败：{exc}"}
    return _attach_view(result, tool="move_part")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def rotate_part(name: str, axis: str = "z", angle: float = 90.0) -> Any:
    """绕全局轴旋转图元（以对象包围盒中心为旋转中心，角度制）——依赖链自动重算，成功后自动附三视图。
    可旋转对象见 parts 字段（布尔/圆角结果跟随其图元，不可直接旋转）。
    axis=x|y|z（全局轴方向）；angle 范围 (-360, 360) 非零，正值逆时针（右手定则）。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        result = _transform.rotate_part(_session, name, axis=axis, angle=angle)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"旋转失败：{exc}"}
    return _attach_view(result, tool="rotate_part")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def extrude_profile(profile: dict, height: float, face: str | None = None,
                    offset: list[float] | None = None,
                    operation: str = "pad") -> Any:
    """拉伸 profile 轮廓（pad 加料 / pocket 减料），成功后自动附三视图。
    profile 示例：{"type":"rect","length":20,"width":10}、{"type":"circle","radius":5}、
    {"type":"slot","length":20,"width":8}、{"type":"polygon","points":[[0,0],[10,0],[0,8]]}。
    face=面标签（来自标注图，见 render_part(annotate='faces') 返回的标签表）；省略=全局 XY 平面。
    offset=[u,v] 面内毫米偏移轮廓中心（原点=面外边界包络中点，矩形面即几何中心，
    不随已打的孔漂移）；height=拉伸高度（mm）；operation=pad|pocket。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        result = _sketch.extrude_profile(
            _session, profile, height,
            face=face,
            offset=tuple(offset) if offset is not None else (0.0, 0.0),
            operation=operation)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"拉伸失败：{exc}"}
    return _attach_view(result, tool="extrude_profile")


# ---------------------------------------------------------------------------
# Round 8：装配工具（18→21）
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def new_part(name: str) -> Any:
    """创建命名零件并将其设为活动零件（开始多零件装配模式）。
    首次调用时，文档中已有几何对象会自动归入隐式零件 "Part1"。
    成功后自动附三视图拼图（含多零件分色）。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        result = _session.new_part(name)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"新建零件失败：{exc}"}
    result["ok"] = True
    return _attach_view(result, tool="new_part")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def set_active_part(name: str) -> Any:
    """切换活动零件——后续建模/特征/标注工具默认作用于该零件
    （在非活动零件上继续加工的恢复路径）。成功后自动附三视图拼图。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        _session.set_active_part(name)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"切换零件失败：{exc}"}
    return _attach_view({"ok": True, "active_part": name}, tool="set_active_part")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def place_part(part: str, position: list[float] | None = None,
               rotation_axis: str | None = None, angle: float | None = None) -> Any:
    """设置零件绝对位置 and/or 叠加旋转（装配位姿）。
    position=[x,y,z]：零件原点移到绝对坐标（mm）。
    rotation_axis=x|y|z + angle：绕零件包围盒中心旋转（角度制，(-360,360) 内非零）。
    至少提供 position 或 rotation_axis+angle 之一。成功后自动附三视图拼图。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        result = _assembly.place_part(
            _session, part, position=position,
            rotation_axis=rotation_axis, angle=angle)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"零件位置设置失败：{exc}"}
    return _attach_view(result, tool="place_part")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def align_parts(moving_part: str, moving_face: str,
                target_part: str, target_face: str,
                offset: list[float] | None = None,
                gap: float = 0.0,
                allow_interference: bool = False) -> Any:
    """面贴面对齐：moving_part 的 moving_face 贴向 target_part 的 target_face。
    面标签来自 render_part(annotate='faces') 的标签表（每零件需分别标注）。
    offset=[u,v]：面内毫米偏移（默认 [0,0]=两面基准点对齐；基准=面外边界包络
    中点，矩形面即几何中心，不随已打的孔漂移）。
    gap：贴合间隙（mm，0=接触，正值=间隙，负值=叠入）。
    allow_interference=True：允许干涉放行（默认 False=检测到干涉则拒绝）。
    成功后自动附三视图拼图。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        result = _assembly.align_parts(
            _session, moving_part, moving_face, target_part, target_face,
            offset=tuple(offset) if offset is not None else (0.0, 0.0),
            gap=gap, allow_interference=allow_interference)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"装配对齐失败：{exc}"}
    return _attach_view(result, tool="align_parts")


# ---------------------------------------------------------------------------
# Round 11：卸载（两段式确认 + 自退换芯执行删除）
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def uninstall_runtime(confirm: bool = False) -> dict[str, Any]:
    """卸载 CAD 引擎（删除全部已下载的运行时，约 2-3GB；扩展本体请在客户端设置里移除）。
    不带 confirm：仅预览将删除的路径与大小；confirm=true：执行删除。"""
    home = paths.vibecad_home()
    if not confirm:
        size = _uninstall.dir_size_mb(home) if home.exists() else 0.0
        return {"ok": True, "confirm_required": True, "path": str(home),
                "size_mb": round(size, 1),
                "message": "将删除以上目录；确认请再次调用 uninstall_runtime(confirm=true)"}
    info = _uninstall.request_uninstall()
    if info.get("marked"):
        if _try_schedule_swap():
            info["message"] = ("已计划删除：server 即将自动重启完成清理。"
                                "扩展本体如需移除，请在客户端设置（Extensions）里 Remove。")
        else:  # I4/C1 回退：裸 server 不能自杀，删除要等下次启动早期执行
            info["message"] = ("已计划删除：请重启 server 后生效（下次启动时自动完成清理）。"
                                "扩展本体如需移除，请在客户端设置（Extensions）里 Remove。")
    return info


def main() -> None:
    if _auto_install_enabled():
        _spawn_install()
    mcp.run()


def _auto_install_enabled() -> bool:
    return os.environ.get("VIBECAD_AUTO_INSTALL", "") not in ("", "0", "false", "False")


if __name__ == "__main__":
    main()
