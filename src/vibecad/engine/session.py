"""进程内 FreeCAD 文档会话（D1a）。骨架：事务 + 几何断言；真实文档生命周期见 Task 3。

FreeCAD 仅能在 conda 运行时进程 import，故 Session 构造不 import FreeCAD，
import 延迟到 open_document（Task 3），在 silence_fd1() 内进行。

Round 8：多零件注册表（App::Part 容器方案，Task 0 spike 选定）。
铁律：`_parts` 为空（从未调 new_part）时一切行为与 R7 完全一致——单零件用户零感知。
"""
from __future__ import annotations

import contextlib
import copy
import json
import tempfile
import uuid
from pathlib import Path
from typing import Any

#: 单零件模式（_parts 空）下标签注册表的命名空间键
_SINGLE = "__single__"

#: 隐式首零件名：单零件模式造过几何后调 new_part，既有对象归入此零件
_IMPLICIT_PART = "Part1"

# 写入 FCStd 文档本身的轻量状态。FreeCAD 会持久化 Document 动态属性，因而无需
# 旁车文件；旧项目没有该属性时仍可通过几何回退打开。
_STATE_PROPERTY = "VibeCADState"
_STATE_SCHEMA = 1

# Object-level identity lives on each FreeCAD DocumentObject.  These properties are the
# persistence boundary: Session deliberately keeps no parallel identity registry that could
# become authoritative after recompute, checkpoint, close, or reload.
_IDENTITY_PROPERTIES = (
    "VibeCADObjectId",
    "VibeCADFeatureId",
    "VibeCADSemanticRole",
    "VibeCADProvenance",
)
_IDENTITY_GROUP = "VibeCAD"
_IDENTITY_PROPERTY_DOCS = {
    "VibeCADObjectId": "Persistent VibeCAD object identity",
    "VibeCADFeatureId": "Persistent VibeCAD feature identity",
    "VibeCADSemanticRole": "VibeCAD semantic object role",
    "VibeCADProvenance": "Canonical VibeCAD provenance JSON",
}


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
        # 显式结果根：避免“最后一个有体积对象”启发式在多图元/删除后选错结果。
        # key 与标签命名空间一致：单零件为 _SINGLE，多零件为零件名。
        self._result_roots: dict[str, str] = {}
        self._undo_result_roots: list[dict[str, str]] = []
        self._redo_result_roots: list[dict[str, str]] = []
        self._undo_active_parts: list[str | None] = []
        self._redo_active_parts: list[str | None] = []
        self._revision_id = uuid.uuid4().hex
        self._saved_revision_id: str | None = None
        self._saved_active_part: str | None = None
        self._saved_result_roots: dict[str, str] | None = None
        self._undo_revisions: list[str] = []
        self._redo_revisions: list[str] = []

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
    def _transaction(
        self, label: str, part: str | None = None, *, claim_new_objects: bool = True,
    ):
        self._require_doc()
        # 差集法对象归属：多零件模式在事务入口记对象名快照，成功提交时把新增对象
        # 归入活动零件；_parts 空（单零件模式）不启动——零开销、行为与 R7 完全一致
        before = (
            {o.Name for o in self._doc.Objects}
            if self._parts and claim_new_objects else None
        )
        roots_before = dict(self._result_roots)
        active_before = self._active_part
        revision_before = self._revision_id
        labels_before = copy.deepcopy(self._labels)
        parts_before = {
            name: {**info, "objects": set(info["objects"])}
            for name, info in self._parts.items()
        }

        def restore_python_state() -> None:
            self._labels = labels_before
            self._parts = {
                name: {**info, "objects": set(info["objects"])}
                for name, info in parts_before.items()
            }
            self._result_roots = roots_before
            self._active_part = active_before
            self._revision_id = revision_before

        def abort_best_effort() -> None:
            # 原始事务异常必须保持为主错误；FreeCAD abort 自身失败不能掩盖根因。
            with contextlib.suppress(Exception):
                self._doc.abortTransaction()

        self._doc.openTransaction(label)
        try:
            yield
        except BaseException:
            restore_python_state()
            abort_best_effort()
            raise
        else:
            try:
                if before is not None:
                    # commit 之前归属：容器 Group 变化与几何创建同事务，回滚则一并消失
                    self._claim_new_objects(before, part=part)
                self._doc.commitTransaction()
            except BaseException:
                restore_python_state()
                abort_best_effort()
                raise
            self._undo_result_roots.append(roots_before)
            self._undo_active_parts.append(active_before)
            self._undo_revisions.append(revision_before)
            self._redo_result_roots.clear()
            self._redo_active_parts.clear()
            self._redo_revisions.clear()
            self._revision_id = uuid.uuid4().hex

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
        parts_before = dict(self._parts)
        active_before = self._active_part
        labels_before = None if self._labels is None else dict(self._labels)
        try:
            # App::Part 会自动创建 Origin/轴/基准面等内部对象；它们不是零件几何，
            # 本事务只创建容器，显式禁止差集归属，避免空零件被误判为“有对象”。
            with self._transaction("new_part", claim_new_objects=False):
                implicit = self._register_first_part_if_needed()
                if name in self._parts:  # name 与隐式零件撞名（如 new_part("Part1")）
                    raise ValueError(f"零件 {name!r} 已存在（与隐式首零件名冲突）")
                container = self._register_part_container(name)
                self._parts[name] = {"container": container, "objects": set()}
                self._active_part = name
        except BaseException:
            self._parts = parts_before
            self._active_part = active_before
            self._labels = labels_before
            raise
        return {"part": name, "implicit_part": implicit}

    def set_active_part(self, name: str) -> None:
        if name not in self._parts:
            raise ValueError(f"零件 {name!r} 不存在（已有零件：{list(self._parts)}）")
        if name == self._active_part:
            return
        self._active_part = name
        self._revision_id = uuid.uuid4().hex

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
        if _SINGLE in self._result_roots:
            self._result_roots[_IMPLICIT_PART] = self._result_roots.pop(_SINGLE)
        return _IMPLICIT_PART

    def _claim_new_objects(self, before: set[str], part: str | None = None) -> None:
        """差集法：把本事务新增的文档对象（容器除外）归入当前活动零件。

        两阶段纪律（终审 M-1）：先全部 addObject（任一失败 → 异常上抛、事务
        abort，容器 Group 变化随之回滚），全部成功后一次性 update 注册集合——
        绝不让集合处于半更新状态：abort 后 FreeCAD 会重用对象 Name（真机取证
        Probe→Probe），半更新残名会让下个事务的新对象被误判为已归属。"""
        owner = part if part is not None else self._active_part
        if owner is None:  # 纵深防御：_parts 非空时 active 必有值
            raise RuntimeError("多零件模式下无活动零件——内部状态异常")
        if owner not in self._parts:
            raise ValueError(f"零件 {owner!r} 不存在（已有零件：{list(self._parts)}）")
        info = self._parts[owner]
        new_objs = [obj for obj in self._doc.Objects
                    if obj.Name not in before
                    and getattr(obj, "TypeId", "") != "App::Part"]
        for obj in new_objs:  # 阶段一：容器隶属（失败上抛 → 事务 abort 整体回滚）
            info["container"].addObject(obj)
        # 阶段二：全部成功后一次性入册（集合无半更新状态可言）
        info["objects"].update(obj.Name for obj in new_objs)

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

    # ---- Stage 3：持久 object / feature identity primitives ----
    def _require_owned_identity_object(self, obj: Any) -> Any:
        """Return one live object owned by this Session's current Document.

        A stale Python proxy from a closed or replaced document must never be accepted merely
        because its ``Name`` happens to match an object in the new document.
        """
        self._require_doc()
        name = getattr(obj, "Name", None)
        if not isinstance(name, str) or not name:
            raise ValueError("identity 对象必须有非空 Name")
        try:
            current = self._doc.getObject(name)
        except Exception as exc:
            raise ValueError("identity 对象不属于当前文档") from exc
        if current is not obj:
            raise ValueError("identity 对象不属于当前文档")
        return obj

    @staticmethod
    def _identity_property_presence(obj: Any) -> tuple[bool, ...]:
        try:
            properties = set(obj.PropertiesList)
        except Exception as exc:
            raise ValueError("对象 identity 属性列表不可读取") from exc
        return tuple(name in properties for name in _IDENTITY_PROPERTIES)

    @staticmethod
    def _validate_identity_property_envelope(obj: Any) -> bool:
        """Validate property storage without interpreting selector-domain values.

        ``False`` means the object has never been attached.  A partial attachment, wrong
        property type, or missing persistence/editor flags is corruption and fails loudly.
        """
        presence = Session._identity_property_presence(obj)
        if not any(presence):
            return False
        if not all(presence):
            raise ValueError("对象 identity 属性不完整")
        for name in _IDENTITY_PROPERTIES:
            try:
                property_type = obj.getTypeIdOfProperty(name)
                editor_modes = set(obj.getEditorMode(name))
                property_status = set(obj.getPropertyStatus(name))
            except Exception as exc:
                raise ValueError("对象 identity 属性元数据不可读取") from exc
            if property_type != "App::PropertyString":
                raise ValueError("对象 identity 属性必须是 App::PropertyString")
            if not {"ReadOnly", "Hidden"}.issubset(editor_modes):
                raise ValueError("对象 identity property flags 缺少 ReadOnly/Hidden")
            if "LockDynamic" not in property_status:
                raise ValueError("对象 identity property flags 缺少 LockDynamic")
        return True

    @staticmethod
    def _parse_identity(obj: Any) -> Any:
        # Selector owns canonical identifier / role / provenance validation.  The import is
        # intentionally lazy so Session construction and ordinary legacy CAD use remain free of
        # workflow imports and FreeCAD can still be imported only inside its managed runtime.
        from vibecad.execution.selectors import parse_entity_identity  # noqa: PLC0415

        return parse_entity_identity(obj)

    def iter_identified_objects(self) -> tuple[tuple[Any, Any], ...]:
        """Read all attached identities from the live Document, with their object proxies.

        Completely untagged legacy/internal FreeCAD objects are not silently migrated.  Partial,
        malformed, or duplicate authority fails; callers decide when an import transaction may
        attach new identities.
        """
        self._require_doc()
        tracked: list[Any] = []
        for obj in tuple(self._doc.Objects):
            if self._validate_identity_property_envelope(obj):
                tracked.append(obj)

        from vibecad.execution.selectors import index_entity_identities  # noqa: PLC0415

        # The selector index is the single authority for canonical parsing and duplicate checks.
        # Reparse below only to return stable object/identity pairs; nothing is cached in Session.
        index_entity_identities(tuple(tracked))
        records = tuple((obj, self._parse_identity(obj)) for obj in tracked)
        return tuple(sorted(records, key=lambda item: item[1].object_id))

    def list_object_identities(self) -> tuple[tuple[Any, Any], ...]:
        """Return fresh ``(DocumentObject, EntityIdentity)`` pairs in object-id order."""
        return self.iter_identified_objects()

    def read_object_identity(self, obj: Any) -> Any:
        """Read one identity through the full-document duplicate/malformed gate."""
        target = self._require_owned_identity_object(obj)
        if not self._validate_identity_property_envelope(target):
            raise ValueError("对象未附加 VibeCAD identity")
        for current, identity in self.iter_identified_objects():
            if current is target:
                return identity
        raise ValueError("对象未附加 VibeCAD identity")

    def attach_object_identity(self, obj: Any, identity: Any) -> Any:
        """Attach one already validated ``EntityIdentity`` as locked dynamic properties.

        The caller owns the surrounding FreeCAD transaction.  Existing authority is never
        overwritten: an exact retry is idempotent; partial, malformed, duplicate, or different
        identity fails closed.  Every read after attachment comes back from Document properties.
        """
        target = self._require_owned_identity_object(obj)
        from vibecad.execution.selectors import (  # noqa: PLC0415
            EntityIdentity,
            encode_provenance_metadata,
        )

        if type(identity) is not EntityIdentity:
            raise TypeError("identity 必须是 EntityIdentity")
        if identity.object_type != getattr(target, "TypeId", None):
            raise ValueError("identity object_type 与 FreeCAD 对象 TypeId 不一致")
        provenance = encode_provenance_metadata(identity.provenance)
        values = {
            "VibeCADObjectId": identity.object_id,
            "VibeCADFeatureId": identity.feature_id or "",
            "VibeCADSemanticRole": identity.semantic_role.value,
            "VibeCADProvenance": provenance,
        }

        presence = self._identity_property_presence(target)
        if any(presence):
            current = self.read_object_identity(target)
            if current == identity:
                return current
            raise ValueError("对象已附加不同的 VibeCAD identity")

        # Detect duplicate authority before adding locked properties.  Any exception leaves the
        # object untouched; creation itself should be part of the caller's document transaction.
        existing = self.iter_identified_objects()
        for _, current in existing:
            if current.object_id == identity.object_id:
                raise ValueError("重复 object_id")
            if identity.feature_id is not None and current.feature_id == identity.feature_id:
                raise ValueError("重复 feature_id")

        for name in _IDENTITY_PROPERTIES:
            target.addProperty(
                "App::PropertyString",
                name,
                _IDENTITY_GROUP,
                _IDENTITY_PROPERTY_DOCS[name],
                0,
                True,
                True,
                True,
            )
        for name, value in values.items():
            setattr(target, name, value)

        attached = self.read_object_identity(target)
        if attached != identity:
            raise ValueError("FreeCAD identity 属性写入后校验失败")
        return attached

    def _reset_model_state(self) -> None:
        self._labels = None
        self._parts = {}
        self._active_part = None
        self._result_roots = {}
        self._undo_result_roots = []
        self._redo_result_roots = []
        self._undo_active_parts = []
        self._redo_active_parts = []
        self._revision_id = uuid.uuid4().hex
        self._saved_revision_id = None
        self._saved_active_part = None
        self._saved_result_roots = None
        self._undo_revisions = []
        self._redo_revisions = []

    @staticmethod
    def _close_owned_document(doc: Any) -> None:
        """只关闭仍由 FreeCAD 全局注册表指向同一代理的文档。"""
        from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
        with silence_fd1():
            import FreeCAD  # noqa: PLC0415
            name = getattr(doc, "Name", None)
            if not isinstance(name, str) or not name:
                raise RuntimeError("拒绝关闭没有有效 Name 的 FreeCAD 文档")
            try:
                current = FreeCAD.getDocument(name)
            except Exception as exc:
                raise RuntimeError(f"FreeCAD 文档 {name!r} 已不在全局注册表中") from exc
            if current is not doc:
                raise RuntimeError(f"拒绝关闭已被其他 Session 占用的 FreeCAD 文档 {name!r}")
            FreeCAD.closeDocument(name)

    def _replace_document(self, doc: Any, *, restore_state: bool) -> Any:
        """原子切换活动文档：新文档成功获得后才关闭旧文档，避免打开失败丢会话。"""
        old = self._doc
        state_before = (
            self._labels, self._parts, self._active_part, self._result_roots,
            self._undo_result_roots, self._redo_result_roots,
            self._undo_active_parts, self._redo_active_parts,
            self._revision_id, self._saved_revision_id,
            self._saved_active_part, self._saved_result_roots,
            self._undo_revisions, self._redo_revisions,
        )
        try:
            self._doc = doc
            self._reset_model_state()
            self._doc.UndoMode = 1
            if restore_state:
                self._rebuild_parts()
                self._restore_persisted_state()
                self._stabilize_result_roots()
                self.mark_saved()
            if old is not None and old is not doc:
                self._close_owned_document(old)
        except BaseException:
            self._doc = old
            (
                self._labels, self._parts, self._active_part, self._result_roots,
                self._undo_result_roots, self._redo_result_roots,
                self._undo_active_parts, self._redo_active_parts,
                self._revision_id, self._saved_revision_id,
                self._saved_active_part, self._saved_result_roots,
                self._undo_revisions, self._redo_revisions,
            ) = state_before
            if doc is not old:
                with contextlib.suppress(Exception):
                    self._close_owned_document(doc)
            raise
        return self._doc

    def open_document(self, name: str) -> Any:
        self._ensure_freecad()
        from vibecad.freecad_env import silence_fd1
        with silence_fd1():
            import FreeCAD  # noqa: PLC0415
            doc = FreeCAD.newDocument(name)
        # headless 默认 UndoMode=0，openTransaction/abort 是 no-op；必须显式开启。
        return self._replace_document(doc, restore_state=False)

    def load_document(self, path: str | Path) -> Any:
        """打开 FCStd；只有新文件完整恢复后才替换当前会话。"""
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"项目文件不存在：{source}")
        if source.suffix.lower() != ".fcstd":
            raise ValueError(f"项目文件必须是 .FCStd（得到 {source.name!r}）")
        self._ensure_freecad()
        from vibecad.freecad_env import silence_fd1
        with silence_fd1():
            import FreeCAD  # noqa: PLC0415
            # 用独立 candidate.load，而不是 openDocument(path)：后者在同一路径已经
            # 打开时会复用旧 Document，导致 discard_unsaved=true 也无法真正从磁盘重载。
            # candidate 完整恢复成功后才替换旧会话；失败则清理 candidate，旧会话不动。
            doc = FreeCAD.newDocument()
            try:
                doc.load(str(source))
                doc.recompute()
            except BaseException:
                # 候选清理失败不能覆盖真正的 load/recompute 错误。
                with contextlib.suppress(Exception):
                    self._close_owned_document(doc)
                raise
        return self._replace_document(doc, restore_state=True)

    def close_document(self) -> None:
        if self._doc is None:
            self._reset_model_state()
            return
        # FreeCAD 关闭成功后才发布“无文档”状态；失败时完整保留可继续操作的会话。
        self._close_owned_document(self._doc)
        self._doc = None
        self._reset_model_state()

    def _checkpoint(self) -> Path:
        if self._doc is None:
            raise RuntimeError("无活动文档，无法 checkpoint")
        cp_dir = self._checkpoint_dir or (Path(tempfile.gettempdir()) / "vibecad_checkpoints")
        cp_dir.mkdir(parents=True, exist_ok=True)
        path = cp_dir / f"{self._doc.Name}.FCStd"
        from vibecad.freecad_env import silence_fd1
        with silence_fd1():
            self.persist_state()
            if hasattr(self._doc, "saveCopy"):
                self._doc.saveCopy(str(path))
            else:  # 兼容测试 fake / 旧 FreeCAD
                self._doc.saveAs(str(path))
        return path

    # ---- 显式结果根 + FCStd 会话状态 ----
    def _result_key(self, part: str | None = None) -> str:
        if not self._parts:
            if part is not None:
                raise ValueError("单零件模式不接受 part 参数")
            return _SINGLE
        key = part if part is not None else self._active_part
        if key not in self._parts:
            raise ValueError(f"零件 {key!r} 不存在（已有零件：{list(self._parts)}）")
        return key

    @staticmethod
    def _is_result_candidate(obj: Any) -> bool:
        shape = getattr(obj, "Shape", None)
        if shape is None:
            return False
        try:
            if shape.isNull() or getattr(shape, "Volume", 0) <= 0:
                return False
            return not hasattr(shape, "isValid") or shape.isValid()
        except Exception:  # FreeCAD/OCC 的坏 shape 不能成为根
            return False

    def set_result_object(self, obj: Any, part: str | None = None) -> None:
        """把对象设为单零件/指定零件的显式结果根。

        可在事务尚未执行对象归属收尾前调用，因此这里只校验名称与零件命名空间，
        不要求新对象已经出现在 ``_parts[part]['objects']`` 中。
        """
        name = obj if isinstance(obj, str) else getattr(obj, "Name", None)
        if not isinstance(name, str) or not name:
            raise ValueError("结果对象必须有非空 Name")
        key = self._result_key(part)
        if self._parts and key not in self._parts:
            raise ValueError(f"零件 {key!r} 不存在（已有零件：{list(self._parts)}）")
        self._result_roots[key] = name

    def clear_result_object(self, part: str | None = None) -> None:
        self._result_roots.pop(self._result_key(part), None)

    def _objects_for_part(self, part: str | None) -> list[Any]:
        if not self._parts:
            return list(self._doc.Objects)
        key = part if part is not None else self._active_part
        if key not in self._parts:
            raise ValueError(f"零件 {key!r} 不存在（已有零件：{list(self._parts)}）")
        names = self._parts[key]["objects"]
        return [o for o in self._doc.Objects if o.Name in names]

    def _rebuild_parts(self) -> None:
        """从 FCStd 内的 App::Part.Group 重建进程内注册表。"""
        parts: dict[str, dict] = {}
        for container in getattr(self._doc, "Objects", []):
            if getattr(container, "TypeId", "") != "App::Part":
                continue
            label = str(getattr(container, "Label", "") or container.Name)
            name = label if label not in parts else f"{label} ({container.Name})"
            members = {
                obj.Name for obj in getattr(container, "Group", [])
                if getattr(obj, "TypeId", "") != "App::Part"
            }
            parts[name] = {"container": container, "objects": members}
        self._parts = parts
        self._active_part = next(iter(parts), None)

    def persist_state(self) -> None:
        """把活动零件和显式结果根写入 FCStd Document 动态属性。"""
        self._require_doc()
        state = json.dumps({
            "schema": _STATE_SCHEMA,
            "active_part": self._active_part,
            "result_roots": self._result_roots,
            "revision_id": self._revision_id,
        }, ensure_ascii=False, sort_keys=True)
        props = set(getattr(self._doc, "PropertiesList", []))
        if _STATE_PROPERTY not in props:
            self._doc.addProperty(
                "App::PropertyString", _STATE_PROPERTY, "VibeCAD",
                "VibeCAD 会话状态（由程序管理）")
        setattr(self._doc, _STATE_PROPERTY, state)

    def _restore_persisted_state(self) -> None:
        raw = getattr(self._doc, _STATE_PROPERTY, "")
        if not raw:
            return
        try:
            state = json.loads(str(raw))
            if state.get("schema") != _STATE_SCHEMA:
                return
            roots = state.get("result_roots", {})
            if isinstance(roots, dict):
                self._result_roots = {
                    str(k): str(v) for k, v in roots.items()
                    if isinstance(k, str) and isinstance(v, str) and v
                }
            active = state.get("active_part")
            if active in self._parts:
                self._active_part = active
            revision = state.get("revision_id")
            if isinstance(revision, str) and revision:
                self._revision_id = revision
                self._saved_revision_id = revision
        except (TypeError, ValueError, AttributeError):
            # 外部/旧 FCStd 的损坏元数据不妨碍几何本体打开；后续走确定性回退。
            self._result_roots = {}

    def _stabilize_result_roots(self, *, allow_fallback: bool = True) -> None:
        """过滤悬空根，并为旧项目/undo 后状态选择一次兼容回退。"""
        keys = list(self._parts) if self._parts else [_SINGLE]
        valid: dict[str, str] = {}
        for key in keys:
            part = key if self._parts else None
            candidates = self._objects_for_part(part)
            by_name = {o.Name: o for o in candidates}
            root = self._result_roots.get(key)
            if root in by_name and self._is_result_candidate(by_name[root]):
                valid[key] = root
                continue
            if allow_fallback:
                fallback = self._find_result(candidates)
                if fallback is not None:
                    valid[key] = fallback.Name
        self._result_roots = valid

    def refresh_model_state(self, *, allow_root_fallback: bool = True) -> None:
        """undo/redo/delete 后根据当前 Document 重建归属并清除过期标签。"""
        self._require_doc()
        active = self._active_part
        roots = dict(self._result_roots)
        self._labels = None
        self._rebuild_parts()
        if active in self._parts:
            self._active_part = active
        self._result_roots = roots
        self._stabilize_result_roots(allow_fallback=allow_root_fallback)

    def restore_roots_for_undo(self) -> None:
        """在 FreeCAD ``doc.undo()`` 后同步会话历史；无根历史则让回退逻辑接管。"""
        current = dict(self._result_roots)
        if self._undo_result_roots:
            self._result_roots = self._undo_result_roots.pop()
        else:
            self._result_roots = {}
        self._redo_result_roots.append(current)
        current_active = self._active_part
        if self._undo_active_parts:
            self._active_part = self._undo_active_parts.pop()
        self._redo_active_parts.append(current_active)
        current_revision = self._revision_id
        if self._undo_revisions:
            self._revision_id = self._undo_revisions.pop()
        else:
            self._revision_id = uuid.uuid4().hex
        self._redo_revisions.append(current_revision)

    def restore_roots_for_redo(self) -> None:
        """在 FreeCAD ``doc.redo()`` 后恢复对应的显式根和活动零件历史。"""
        current = dict(self._result_roots)
        if self._redo_result_roots:
            self._result_roots = self._redo_result_roots.pop()
        else:
            self._result_roots = {}
        self._undo_result_roots.append(current)
        current_active = self._active_part
        if self._redo_active_parts:
            self._active_part = self._redo_active_parts.pop()
        self._undo_active_parts.append(current_active)
        current_revision = self._revision_id
        if self._redo_revisions:
            self._revision_id = self._redo_revisions.pop()
        else:
            self._revision_id = uuid.uuid4().hex
        self._undo_revisions.append(current_revision)

    def mark_saved(self) -> None:
        self._saved_revision_id = self._revision_id
        self._saved_active_part = self._active_part
        self._saved_result_roots = dict(self._result_roots)

    def is_dirty(self) -> bool:
        return self._doc is not None and (
            self._revision_id != self._saved_revision_id
            or self._active_part != self._saved_active_part
            or self._result_roots != self._saved_result_roots
        )

    @staticmethod
    def _find_result(candidates) -> Any | None:
        """结果对象查找（R7 原逻辑原样保留）：结果类型表优先，fallback 取最后的有体积对象。"""
        result_types = ("Part::Cut", "Part::Fuse", "Part::Common", "Part::Fillet", "Part::Chamfer")
        result = None
        for obj in candidates:
            if (getattr(obj, "TypeId", "") in result_types
                    and Session._is_result_candidate(obj)):
                result = obj
        if result is None:
            for obj in candidates:
                if Session._is_result_candidate(obj):
                    result = obj
        return result

    def get_result_object(self, part: str | None = None) -> Any:
        """返回显式结果根；旧项目首次访问时做一次兼容回退并稳定下来。"""
        if self._doc is None:
            raise RuntimeError("无活动文档")
        key = self._result_key(part)
        candidates = self._objects_for_part(part)
        by_name = {o.Name: o for o in candidates}
        root = self._result_roots.get(key)
        if root in by_name and self._is_result_candidate(by_name[root]):
            return by_name[root]
        result = self._find_result(candidates)
        if result is None:
            if self._parts:
                raise RuntimeError(f"零件 {key!r} 中无有效 solid")
            raise RuntimeError("文档中无有效 solid")
        # 兼容没有 VibeCADState 的旧 FCStd：只在首次选择时回退，此后使用显式根。
        self._result_roots[key] = result.Name
        return result

    def get_result_shape(self, part: str | None = None) -> Any:
        return self.get_result_object(part).Shape

    def get_assembly_shape(self) -> Any:
        """全装配 shape：_parts 空 → 单零件结果 shape（与 R7 等价）；非空 → 各零件
        结果 shape 应用容器位姿（spike 选型：transformed(Placement.toMatrix())，
        组内 shape 保持局部坐标）后合成 compound。

        空零件（objects 空，new_part 后未建几何）跳过——无 shape 可合成，不该让
        渲染/describe/export 整体崩掉（终审 M-2：describe_assembly 的 error 字段
        因此处先抛而成死代码）。全部零件皆空时响亮抛错。
        注意：跳过规则必须与 server._build_part_map 一致（compound 面/边序 =
        各零件按迭代序拼接，二者错位会让标注归属切片计数错乱）。"""
        if not self._parts:
            return self.get_result_shape()
        from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
        with silence_fd1():
            import Part  # noqa: PLC0415
            shapes = [
                self.get_result_shape(name).transformed(info["container"].Placement.toMatrix())
                for name, info in self._parts.items() if info["objects"]
            ]
            if not shapes:
                raise RuntimeError(
                    "装配中无任何零件有几何——请先用 add_box/extrude_profile 等创建几何")
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
