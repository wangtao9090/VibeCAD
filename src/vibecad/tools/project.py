"""FCStd 项目生命周期：保存/打开、删除以及进程内 undo/redo。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from vibecad.engine.session import Session


def _project_path(path: str, *, for_save: bool) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path 必须是非空字符串")
    result = Path(path).expanduser()
    if for_save and result.exists() and result.is_dir():
        return result.resolve()
    if result.suffix.lower() != ".fcstd":
        if for_save and not result.suffix:
            result = result.with_suffix(".FCStd")
        else:
            raise ValueError(f"项目路径必须使用 .FCStd 扩展名（得到 {result.name!r}）")
    return result.resolve()


def _history(doc: Any) -> dict[str, Any]:
    return {
        "undo_count": int(getattr(doc, "UndoCount", 0)),
        "redo_count": int(getattr(doc, "RedoCount", 0)),
    }


def _result_summary(session: Session) -> dict[str, Any]:
    roots = dict(session._result_roots)
    return {
        "parts": session.part_names(),
        "active_part": session.active_part,
        "result_roots": roots,
        "has_result": bool(roots),
    }


def _atomic_save_copy(doc: Any, target: Path) -> None:
    """同目录写完整副本后原子替换，保存中断时绝不破坏已有项目。"""
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=".FCStd", dir=target.parent,
    )
    os.close(fd)
    temporary = Path(raw_temp)
    # FreeCAD 自己创建 FCStd；预先存在的空文件在部分版本中会被当作坏文档。
    temporary.unlink()
    try:
        save_copy = getattr(doc, "saveCopy", None)
        if not callable(save_copy):
            raise RuntimeError("当前 FreeCAD Document 不支持安全的 saveCopy")
        save_copy(str(temporary))
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise RuntimeError(f"保存后未得到非空 FCStd 临时文件：{temporary}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def save_project(session: Session, path: str, *, overwrite: bool = True) -> dict[str, Any]:
    """保存完整参数化文档为 FCStd，并把 VibeCAD 活动零件/结果根写入文档。"""
    session._require_doc()
    target = _project_path(path, for_save=True)
    if target.exists() and target.is_dir():
        target = target / f"{session.doc.Name}.FCStd"
    if target.exists() and not overwrite:
        raise ValueError(f"项目文件已存在：{target}（如需覆盖请传 overwrite=true）")
    target.parent.mkdir(parents=True, exist_ok=True)

    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with silence_fd1():
        session.doc.recompute()
        session.persist_state()
        _atomic_save_copy(session.doc, target)
    if not target.is_file() or target.stat().st_size <= 0:
        raise RuntimeError(f"保存后未得到非空 FCStd 文件：{target}")
    session.mark_saved()
    return {
        "ok": True,
        "path": str(target),
        "size_bytes": target.stat().st_size,
        "document": session.doc.Name,
        **_result_summary(session),
        **_history(session.doc),
    }


def open_project(
    session: Session, path: str, *, discard_unsaved: bool = False,
) -> dict[str, Any]:
    """打开 FCStd 并恢复零件注册表、活动零件和显式结果根；标签快照不跨会话恢复。"""
    if not isinstance(discard_unsaved, bool):
        raise ValueError("discard_unsaved 必须是 bool")
    if session.doc is not None and session.is_dirty() and not discard_unsaved:
        raise ValueError(
            "当前项目有未保存修改；请先调用 save_project，或明确传 "
            "discard_unsaved=true 后再打开")
    source = _project_path(path, for_save=False)
    doc = session.load_document(source)
    return {
        "ok": True,
        "path": str(source),
        "document": doc.Name,
        "object_count": len(doc.Objects),
        "labels_restored": False,
        **_result_summary(session),
        **_history(doc),
    }


def _operation_name(doc: Any, attr: str) -> str | None:
    names = list(getattr(doc, attr, []) or [])
    return str(names[0]) if names else None


def undo(session: Session) -> dict[str, Any]:
    session._require_doc()
    doc = session.doc
    if int(getattr(doc, "UndoCount", 0)) <= 0:
        raise ValueError("没有可撤销的操作")
    operation = _operation_name(doc, "UndoNames")
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with silence_fd1():
        doc.undo()
    # doc.undo() 一旦成功，FreeCAD 历史已经移动；先同步本地栈，后续 recompute
    # 即使失败也不能让 root/active/revision 与 Document 永久错位。
    session.restore_roots_for_undo()
    recompute_error: Exception | None = None
    try:
        with silence_fd1():
            doc.recompute()
    except Exception as exc:  # FreeCAD 异常类型在不同构建中不稳定
        recompute_error = exc
    try:
        session.refresh_model_state(allow_root_fallback=False)
    except Exception as exc:
        raise RuntimeError("撤销已执行，但会话状态重建失败") from exc
    if recompute_error is not None:
        raise RuntimeError("撤销已执行且会话历史已同步，但文档重算失败") from recompute_error
    return {
        "ok": True,
        "operation": operation,
        "labels_stale": True,
        **_result_summary(session),
        **_history(doc),
    }


def redo(session: Session) -> dict[str, Any]:
    session._require_doc()
    doc = session.doc
    if int(getattr(doc, "RedoCount", 0)) <= 0:
        raise ValueError("没有可重做的操作")
    operation = _operation_name(doc, "RedoNames")
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with silence_fd1():
        doc.redo()
    session.restore_roots_for_redo()
    recompute_error: Exception | None = None
    try:
        with silence_fd1():
            doc.recompute()
    except Exception as exc:  # FreeCAD 异常类型在不同构建中不稳定
        recompute_error = exc
    try:
        session.refresh_model_state(allow_root_fallback=False)
    except Exception as exc:
        raise RuntimeError("重做已执行，但会话状态重建失败") from exc
    if recompute_error is not None:
        raise RuntimeError("重做已执行且会话历史已同步，但文档重算失败") from recompute_error
    return {
        "ok": True,
        "operation": operation,
        "labels_stale": True,
        **_result_summary(session),
        **_history(doc),
    }


def _dependent_closure(obj: Any) -> list[Any]:
    """返回下游依赖优先的删除顺序；App::Part 容器不是几何依赖。"""
    ordered: list[Any] = []
    visited: set[str] = set()

    def visit(current: Any) -> None:
        name = current.Name
        if name in visited:
            return
        visited.add(name)
        for dependent in getattr(current, "InList", []) or []:
            if getattr(dependent, "TypeId", "") != "App::Part":
                visit(dependent)
        ordered.append(current)

    visit(obj)
    return ordered


def delete_object(session: Session, name: str, *, cascade: bool = False) -> dict[str, Any]:
    """删除文档对象；存在下游依赖时默认拒绝，``cascade=true`` 才级联删除。"""
    if not isinstance(name, str) or not name:
        raise ValueError("name 必须是非空字符串")
    session._require_doc()
    try:
        obj = session.get_object(name)
    except KeyError as exc:
        raise ValueError(f"对象 {name!r} 不存在") from exc
    if getattr(obj, "TypeId", "") == "App::Part":
        raise ValueError("delete_object 不删除 App::Part 容器；请逐一删除零件内对象")
    owner = session.owner_of(name)
    if session._parts and owner is None:
        raise RuntimeError(f"对象 {name!r} 未归属任何零件——项目状态异常")

    order = _dependent_closure(obj)
    delete_names = [o.Name for o in order]
    dependents = [target for target in delete_names if target != name]
    if session._parts:
        wrong_owner = [
            target for target in delete_names if session.owner_of(target) != owner
        ]
        if wrong_owner:
            raise RuntimeError(
                f"删除依赖跨越零件边界：{wrong_owner}——项目依赖状态异常，已拒绝删除")
    if dependents and not cascade:
        raise ValueError(
            f"对象 {name!r} 被下游对象依赖：{dependents}；确认级联删除请传 cascade=true")

    # 当前 root 落在删除闭包中时，只沿其明确 Base 链寻找幸存 predecessor；绝不把
    # 隐藏 Tool/无关旧 solid 通过 heuristic 晋升为结果。
    current_root = session._result_roots.get(session._result_key(owner))
    root_obj = session.doc.getObject(current_root) if current_root in delete_names else None
    preferred = getattr(root_obj, "Base", None)
    while getattr(preferred, "Name", None) in delete_names:
        preferred = getattr(preferred, "Base", None)
    preferred_name = getattr(preferred, "Name", None)

    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with session._transaction("delete_object", part=owner):
        with silence_fd1():
            for target in delete_names:
                session.doc.removeObject(target)
            session.doc.recompute()
    if current_root in delete_names:
        session.clear_result_object(part=owner)
    session.refresh_model_state(allow_root_fallback=False)

    if preferred_name:
        restored = session.doc.getObject(preferred_name)
        if restored is not None and session._is_result_candidate(restored):
            session.set_result_object(restored, part=owner)

    return {
        "ok": True,
        "deleted": delete_names,
        "cascade": cascade,
        "labels_stale": True,
        **_result_summary(session),
        **_history(session.doc),
    }
