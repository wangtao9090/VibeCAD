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
        if not shape.isValid() or shape.isNull():
            raise RuntimeError(
                f"几何断言失败：形状无效（isValid={shape.isValid()}, isNull={shape.isNull()}）"
            )
        if shape.Volume <= 0:
            raise RuntimeError(f"几何断言失败：体积为零或负（Volume={shape.Volume}）")

    def get_object(self, name: str) -> Any:
        obj = self._doc.getObject(name)
        if obj is None:
            raise KeyError(name)
        return obj

    def open_document(self, name: str) -> Any:
        self._ensure_freecad()
        from vibecad.freecad_env import silence_fd1
        with silence_fd1():
            import FreeCAD  # noqa: PLC0415
            self._doc = FreeCAD.newDocument(name)
        return self._doc

    def close_document(self) -> None:
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

    def get_result_shape(self) -> Any:
        if self._doc is None:
            raise RuntimeError("无活动文档")
        boolean_types = ("Part::Cut", "Part::Fuse", "Part::Common")
        result = None
        for obj in self._doc.Objects:
            if getattr(obj, "TypeId", "") in boolean_types and hasattr(obj, "Shape"):
                result = obj
        if result is None:
            for obj in self._doc.Objects:
                if hasattr(obj, "Shape") and getattr(obj.Shape, "Volume", 0) > 0:
                    result = obj
        if result is None:
            raise RuntimeError("文档中无有效 solid")
        return result.Shape
