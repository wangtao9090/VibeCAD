"""进程内 FreeCAD 文档会话（D1a）。骨架：事务 + 几何断言；真实文档生命周期见 Task 3。

FreeCAD 仅能在 conda 运行时进程 import，故 Session 构造不 import FreeCAD，
import 延迟到 open_document（Task 3），在 silence_fd1() 内进行。

Round 8：多零件注册表（App::Part 容器方案，Task 0 spike 选定）。
铁律：`_parts` 为空（从未调 new_part）时一切行为与 R7 完全一致——单零件用户零感知。
"""
from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from typing import Any

#: 单零件模式（_parts 空）下标签注册表的命名空间键
_SINGLE = "__single__"

#: 隐式首零件名：单零件模式造过几何后调 new_part，既有对象归入此零件
_IMPLICIT_PART = "Part1"


class Session:
    def __init__(self, checkpoint_dir: Path | None = None) -> None:
        self._doc: Any = None
        self._loaded: bool = False
        self._checkpoint_dir = checkpoint_dir
        # 标签注册表按零件分命名空间：{part_key: {"faces":…, "edges":…, "shown":…}}；
        # part_key = 活动零件名，单零件模式恒为 _SINGLE（对外行为与 R7 零差异）
        self._labels: dict[str, dict] | None = None
        # 多零件注册表：{零件名: {"container": App::Part 对象, "objects": set[对象 Name]}}
        self._parts: dict[str, dict] = {}
        self._active_part: str | None = None

    @property
    def doc(self) -> Any:
        return self._doc

    def _ensure_freecad(self) -> None:
        if not self._loaded:
            from vibecad.freecad_env import prepare_freecad_import
            prepare_freecad_import()
            self._loaded = True

    def _require_doc(self) -> None:
        if self._doc is None:  # 否则 AttributeError 穿透，绕过 RuntimeError/ValueError 契约
            raise RuntimeError("无活动文档——请先调用 new_document 创建文档")

    @contextlib.contextmanager
    def _transaction(self, label: str):
        self._require_doc()
        # 差集法对象归属：多零件模式在事务入口记对象名快照，成功提交时把新增对象
        # 归入活动零件；_parts 空（单零件模式）不启动——零开销、行为与 R7 完全一致
        before = {o.Name for o in self._doc.Objects} if self._parts else None
        self._doc.openTransaction(label)
        try:
            yield
        except BaseException:
            self._doc.abortTransaction()
            raise
        else:
            if before is not None:
                try:
                    # commit 之前归属：容器 Group 变化与几何创建同事务，回滚则一并消失
                    self._claim_new_objects(before)
                except BaseException:
                    self._doc.abortTransaction()
                    raise
            self._doc.commitTransaction()

    # ---- Round 8：多零件注册表（App::Part 容器承载，差集法对象归属）----
    @property
    def active_part(self) -> str | None:
        return self._active_part

    def part_names(self) -> list[str]:
        return list(self._parts)

    def new_part(self, name: str) -> dict[str, Any]:
        """创建命名零件（App::Part 容器）并设为活动零件。

        单零件模式已造过几何时，既有对象先归入隐式零件 Part1——单零件用户
        从不调 new_part，首次调用即从单零件模式无损升级为多零件模式。
        """
        self._require_doc()
        if not name or not isinstance(name, str):
            raise ValueError("name 必须是非空字符串")
        if name in self._parts:
            raise ValueError(f"零件 {name!r} 已存在（已有零件：{list(self._parts)}）")
        implicit = self._register_first_part_if_needed()
        if name in self._parts:  # name 恰与隐式零件撞名（如 new_part("Part1")）
            raise ValueError(f"零件 {name!r} 已存在（与隐式首零件名冲突）")
        container = self._register_part_container(name)
        self._parts[name] = {"container": container, "objects": set()}
        self._active_part = name
        return {"part": name, "implicit_part": implicit}

    def set_active_part(self, name: str) -> None:
        if name not in self._parts:
            raise ValueError(f"零件 {name!r} 不存在（已有零件：{list(self._parts)}）")
        self._active_part = name

    def owner_of(self, obj_name: str) -> str | None:
        """反查对象归属的零件名（_parts objects 集合）；单零件模式（_parts 空）
        或对象未归属任何零件返回 None。

        守卫锚定纪律（终审系统性根因）：modify/transform 等按对象名操作的工具，
        全部完整性快照与断言必须锚定**被操作对象所属零件**（owner），而非活动
        零件——active=B 时改 A 的对象，用 B 的 shape 做快照会让 A 的孔被吞而
        守卫只盯着 B 报 ok。"""
        for pname, info in self._parts.items():
            if obj_name in info["objects"]:
                return pname
        return None

    def _register_part_container(self, name: str) -> Any:
        """新建 App::Part 容器：内部 Name 用 ASCII 前缀（FreeCAD 自动唯一化），
        用户零件名（可中文）存 Label。"""
        from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
        with silence_fd1():
            container = self._doc.addObject("App::Part", "VibePart")
            container.Label = name
        return container

    def _register_first_part_if_needed(self) -> str | None:
        """单零件模式已有几何对象时，把全部既有对象归入隐式零件 Part1。

        几何没变，单零件命名空间的标签快照随归属迁移到 Part1（不强迫重标注）。
        返回隐式零件名（发生归入时），否则 None。
        """
        if self._parts:
            return None
        existing = [o for o in self._doc.Objects if getattr(o, "TypeId", "") != "App::Part"]
        if not existing:
            return None
        container = self._register_part_container(_IMPLICIT_PART)
        for obj in existing:
            container.addObject(obj)
        self._parts[_IMPLICIT_PART] = {
            "container": container, "objects": {o.Name for o in existing}}
        if self._labels and _SINGLE in self._labels:
            self._labels[_IMPLICIT_PART] = self._labels.pop(_SINGLE)
        return _IMPLICIT_PART

    def _claim_new_objects(self, before: set[str]) -> None:
        """差集法：把本事务新增的文档对象（容器除外）归入当前活动零件。"""
        if self._active_part is None:  # 纵深防御：_parts 非空时 active 必有值
            raise RuntimeError("多零件模式下无活动零件——内部状态异常")
        info = self._parts[self._active_part]
        for obj in self._doc.Objects:
            if obj.Name in before or getattr(obj, "TypeId", "") == "App::Part":
                continue
            info["container"].addObject(obj)
            info["objects"].add(obj.Name)

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
        self._parts = {}  # 零件注册表作用域 = 单文档，换文档即重置
        self._active_part = None
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
        self._parts = {}  # 零件注册表同款清理（容器引用随文档失效，不得跨文档残留）
        self._active_part = None
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

    @staticmethod
    def _find_result(candidates) -> Any | None:
        """结果对象查找（R7 原逻辑原样保留）：结果类型表优先，fallback 取最后的有体积对象。"""
        result_types = ("Part::Cut", "Part::Fuse", "Part::Common", "Part::Fillet", "Part::Chamfer")
        result = None
        for obj in candidates:
            # 纵深防御：null/invalid shape 不当选（与 fallback 循环的纪律拉齐）
            if (getattr(obj, "TypeId", "") in result_types and hasattr(obj, "Shape")
                    and not obj.Shape.isNull() and getattr(obj.Shape, "Volume", 0) > 0):
                result = obj
        if result is None:
            for obj in candidates:
                if hasattr(obj, "Shape") and getattr(obj.Shape, "Volume", 0) > 0:
                    result = obj
        return result

    def get_result_object(self, part: str | None = None) -> Any:
        """返回结果文档对象（含 Fillet/Chamfer）；无则抛 RuntimeError。

        多零件模式在指定零件（默认活动零件）的对象集内做同款查找；
        _parts 空（单零件模式）走 R7 原全文档逻辑，行为零变化。
        """
        if self._doc is None:
            raise RuntimeError("无活动文档")
        if not self._parts:
            result = self._find_result(self._doc.Objects)
            if result is None:
                raise RuntimeError("文档中无有效 solid")
            return result
        key = part if part is not None else self._active_part
        if key not in self._parts:
            raise ValueError(f"零件 {key!r} 不存在（已有零件：{list(self._parts)}）")
        names = self._parts[key]["objects"]
        result = self._find_result([o for o in self._doc.Objects if o.Name in names])
        if result is None:
            raise RuntimeError(f"零件 {key!r} 中无有效 solid")
        return result

    def get_result_shape(self, part: str | None = None) -> Any:
        return self.get_result_object(part).Shape

    def get_assembly_shape(self) -> Any:
        """全装配 shape：_parts 空 → 单零件结果 shape（与 R7 等价）；非空 → 各零件
        结果 shape 应用容器位姿（spike 选型：transformed(Placement.toMatrix())，
        组内 shape 保持局部坐标）后合成 compound。"""
        if not self._parts:
            return self.get_result_shape()
        from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
        with silence_fd1():
            import Part  # noqa: PLC0415
            shapes = [
                self.get_result_shape(name).transformed(info["container"].Placement.toMatrix())
                for name, info in self._parts.items()
            ]
            return Part.makeCompound(shapes)

    # ---- Round 5：标签注册表（标注快照 → 指纹解析）；Round 8：按零件分命名空间 ----
    def _label_key(self, part: str | None) -> str:
        """标签命名空间键：指定零件 > 活动零件 > 单零件哨兵（_parts 空时恒为后者）。"""
        return part if part is not None else (self._active_part or _SINGLE)

    def set_labels(self, faces: dict, edges: dict, shown: set | None = None,
                   part: str | None = None) -> None:
        """存最近一次标注快照：{label: fingerprint}，按零件命名空间隔离（默认活动零件）。

        shown = 本次标签表实际向 AI 展示过的键集合（None 视为全部——内部/测试用法）。
        注册表与同零件现存快照完全相等（几何没变）时 shown 跨调用累积——"看面再看边"
        两次后两类标签都算展示过；注册表变化（几何变了/首次）则重置为本次 shown。
        """
        key = self._label_key(part)
        faces, edges = dict(faces), dict(edges)
        shown = set(faces) | set(edges) if shown is None else set(shown)
        snap = (self._labels or {}).get(key)
        if snap is not None and snap["faces"] == faces and snap["edges"] == edges:
            shown |= snap["shown"]
        if self._labels is None:
            self._labels = {}
        self._labels[key] = {"faces": faces, "edges": edges, "shown": shown}

    def _match_shape(self, part: str | None) -> Any:
        """resolve_face/resolve_edge 的匹配目标 shape——必须与标注指纹同坐标系。

        单零件模式（_parts 空）：结果 shape（局部即全局），R7 行为逐字不变。
        装配模式：标注指纹采集自 get_assembly_shape() 的**全局坐标**（容器位姿已
        应用到 compound），匹配目标必须同为全局——零件容器 Placement 非单位时返回
        get_result_shape(part).transformed(Placement.toMatrix())，否则原局部 shape。

        "全局匹配、局部消费"语义自洽推演：OCCT transformed() 不重排子元素——变换后
        shape 的 Faces/Edges 与原局部 shape 严格逐索引同序（真机断言验证），故本方法
        匹配出的索引可直接用于局部 shape.Faces[idx]/Edges[idx]。消费方（features/
        sketch/assembly）拿索引后在**局部** shape 上取面心/法向做几何计算，而新建的
        图元（孔/凸台等）同样落在零件局部坐标系内（容器 Placement 统一应用于整组），
        因此局部坐标恰是正确坐标——全局只用于"认面"，局部用于"造物"，互不矛盾。
        """
        # part=None 时无参调用：单零件模式对外调用形状与 R7 逐字一致（测试 fake 同款签名）
        shape = self.get_result_shape() if part is None else self.get_result_shape(part)
        if not self._parts:
            return shape
        key = part if part is not None else self._active_part
        placement = self._parts[key]["container"].Placement
        if placement.isIdentity():
            return shape
        return shape.transformed(placement.toMatrix())

    def resolve_face(self, label: str, part: str | None = None) -> int:
        """面标签 → 该零件（默认活动零件）结果形状的面索引；快照缺失/标签未知/未展示/
        匹配失败均抛 LabelExpiredError。未展示 gate：注册表无论 mode 都全量注册，但 AI
        只见过标签表里画出来的条目——未展示的标签指认是编造，必须响亮拒绝。

        装配模式下指纹匹配在全局坐标系进行（见 _match_shape：标注来自装配 compound
        的全局面，零件容器 Placement 非单位时局部指纹永远对不上）；返回的索引因
        transformed 不重排子元素而与局部 shape.Faces 同序，消费方安全用于局部几何。"""
        from vibecad.engine import naming  # noqa: PLC0415
        snap = (self._labels or {}).get(self._label_key(part))
        if not snap or label not in snap["faces"]:
            raise naming.LabelExpiredError(
                f"未知面标签 {label!r}——请先调用 render_part(annotate='faces') 获取标注")
        if label not in snap["shown"]:
            raise naming.LabelExpiredError(
                f"面标签 {label!r} 尚未在标注图中向你展示过"
                "——请先调用 render_part(annotate='faces') 查看标注图再指认")
        return naming.match_face(snap["faces"][label], self._match_shape(part).Faces)

    def resolve_edge(self, label: str, part: str | None = None) -> int:
        """边标签 → 边索引；坐标系纪律与 resolve_face 一致（见 _match_shape）。"""
        from vibecad.engine import naming  # noqa: PLC0415
        snap = (self._labels or {}).get(self._label_key(part))
        if not snap or label not in snap["edges"]:
            raise naming.LabelExpiredError(
                f"未知边标签 {label!r}——请先调用 render_part(annotate='edges') 获取标注")
        if label not in snap["shown"]:
            raise naming.LabelExpiredError(
                f"边标签 {label!r} 尚未在标注图中向你展示过"
                "——请先调用 render_part(annotate='edges') 查看标注图再指认")
        return naming.match_edge(snap["edges"][label], self._match_shape(part).Edges)
