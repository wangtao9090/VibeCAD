"""进程内 FreeCAD 文档会话（D1a）。骨架：事务 + 几何断言；真实文档生命周期见 Task 3。

FreeCAD 仅能在 conda 运行时进程 import，故 Session 构造不 import FreeCAD，
import 延迟到 open_document（Task 3），在 silence_fd1() 内进行。
"""
from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from typing import Any


class Session:
    def __init__(self, checkpoint_dir: Path | None = None) -> None:
        self._doc: Any = None
        self._loaded: bool = False
        self._checkpoint_dir = checkpoint_dir
        self._labels: dict | None = None

    @property
    def doc(self) -> Any:
        return self._doc

    def _ensure_freecad(self) -> None:
        if not self._loaded:
            from vibecad.freecad_env import prepare_freecad_import
            prepare_freecad_import()
            self._loaded = True

    @contextlib.contextmanager
    def _transaction(self, label: str):
        if self._doc is None:  # 否则 AttributeError 穿透，绕过 RuntimeError/ValueError 契约
            raise RuntimeError("无活动文档——请先调用 new_document 创建文档")
        self._doc.openTransaction(label)
        try:
            yield
        except BaseException:
            self._doc.abortTransaction()
            raise
        else:
            self._doc.commitTransaction()

    def assert_valid_solid(self, shape: Any) -> None:
        """spec §2.4 规范②：recompute/solve 返回值不可信，几何断言是唯一可信成功判据。"""
        # isNull 必须先于 isValid：BRepCheck_Analyzer 对 NULL shape 不返回 False 而是
        # 直接抛 Part.OCCError（真机实测：对 fillet 切线缝合边 chamfer → NULL shape），
        # 那会绕过 RuntimeError/ValueError 契约把原始 OCC 错误泄漏给 server 层
        if shape.isNull():
            raise RuntimeError(
                "几何断言失败：形状为 NULL（OCCT 未能产生几何——所选边/面可能不支持该操作）")
        if not shape.isValid():
            raise RuntimeError("几何断言失败：形状无效（isValid=False）")
        if shape.Volume <= 0:
            raise RuntimeError(f"几何断言失败：体积为零或负（Volume={shape.Volume}）")

    def get_object(self, name: str) -> Any:
        obj = self._doc.getObject(name)
        if obj is None:
            raise KeyError(name)
        return obj

    def open_document(self, name: str) -> Any:
        self._labels = None  # 标签快照作用域 = 某文档的某次标注，换文档即失效
        self._ensure_freecad()
        from vibecad.freecad_env import silence_fd1
        with silence_fd1():
            import FreeCAD  # noqa: PLC0415
            self._doc = FreeCAD.newDocument(name)
            # headless 默认 UndoMode=0，openTransaction/abort 是 no-op
            # ——必须显式开启否则失败残留垃圾对象（HoleTool、null-shape Fillet 等）
            self._doc.UndoMode = 1
        return self._doc

    def close_document(self) -> None:
        self._labels = None  # 关文档必清标签；置于早退前，无文档时也不留残余快照
        if self._doc is None:
            return
        from vibecad.freecad_env import silence_fd1
        with silence_fd1():
            import FreeCAD  # noqa: PLC0415
            FreeCAD.closeDocument(self._doc.Name)
        self._doc = None

    def _checkpoint(self) -> Path:
        if self._doc is None:
            raise RuntimeError("无活动文档，无法 checkpoint")
        cp_dir = self._checkpoint_dir or (Path(tempfile.gettempdir()) / "vibecad_checkpoints")
        cp_dir.mkdir(parents=True, exist_ok=True)
        path = cp_dir / f"{self._doc.Name}.FCStd"
        from vibecad.freecad_env import silence_fd1
        with silence_fd1():
            self._doc.saveAs(str(path))
        return path

    def get_result_object(self) -> Any:
        """返回当前结果文档对象（含 Fillet/Chamfer）；无则抛 RuntimeError。"""
        if self._doc is None:
            raise RuntimeError("无活动文档")
        result_types = ("Part::Cut", "Part::Fuse", "Part::Common", "Part::Fillet", "Part::Chamfer")
        result = None
        for obj in self._doc.Objects:
            # 纵深防御：null/invalid shape 不当选（与 fallback 循环的纪律拉齐）
            if (getattr(obj, "TypeId", "") in result_types and hasattr(obj, "Shape")
                    and not obj.Shape.isNull() and getattr(obj.Shape, "Volume", 0) > 0):
                result = obj
        if result is None:
            for obj in self._doc.Objects:
                if hasattr(obj, "Shape") and getattr(obj.Shape, "Volume", 0) > 0:
                    result = obj
        if result is None:
            raise RuntimeError("文档中无有效 solid")
        return result

    def get_result_shape(self) -> Any:
        return self.get_result_object().Shape

    # ---- Round 5：标签注册表（标注快照 → 指纹解析）----
    def set_labels(self, faces: dict, edges: dict, shown: set | None = None) -> None:
        """存最近一次标注快照：{label: fingerprint}。

        shown = 本次标签表实际向 AI 展示过的键集合（None 视为全部——内部/测试用法）。
        注册表与现存快照完全相等（几何没变）时 shown 跨调用累积——"看面再看边"
        两次后两类标签都算展示过；注册表变化（几何变了/首次）则重置为本次 shown。
        """
        faces, edges = dict(faces), dict(edges)
        shown = set(faces) | set(edges) if shown is None else set(shown)
        if (self._labels is not None
                and self._labels["faces"] == faces and self._labels["edges"] == edges):
            shown |= self._labels["shown"]
        self._labels = {"faces": faces, "edges": edges, "shown": shown}

    def resolve_face(self, label: str) -> int:
        """面标签 → 当前结果形状的面索引；快照缺失/标签未知/未展示/匹配失败均抛
        LabelExpiredError。未展示 gate：注册表无论 mode 都全量注册，但 AI 只见过
        标签表里画出来的条目——未展示的标签指认是编造，必须响亮拒绝。"""
        from vibecad.engine import naming  # noqa: PLC0415
        if not self._labels or label not in self._labels["faces"]:
            raise naming.LabelExpiredError(
                f"未知面标签 {label!r}——请先调用 render_part(annotate='faces') 获取标注")
        if label not in self._labels["shown"]:
            raise naming.LabelExpiredError(
                f"面标签 {label!r} 尚未在标注图中向你展示过"
                "——请先调用 render_part(annotate='faces') 查看标注图再指认")
        return naming.match_face(self._labels["faces"][label], self.get_result_shape().Faces)

    def resolve_edge(self, label: str) -> int:
        from vibecad.engine import naming  # noqa: PLC0415
        if not self._labels or label not in self._labels["edges"]:
            raise naming.LabelExpiredError(
                f"未知边标签 {label!r}——请先调用 render_part(annotate='edges') 获取标注")
        if label not in self._labels["shown"]:
            raise naming.LabelExpiredError(
                f"边标签 {label!r} 尚未在标注图中向你展示过"
                "——请先调用 render_part(annotate='edges') 查看标注图再指认")
        return naming.match_edge(self._labels["edges"][label], self.get_result_shape().Edges)
