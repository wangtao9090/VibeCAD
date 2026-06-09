"""进程内 FreeCAD 文档会话（D1a）。骨架：事务 + 几何断言；真实文档生命周期见 Task 3。

FreeCAD 仅能在 conda 运行时进程 import，故 Session 构造不 import FreeCAD，
import 延迟到 open_document（Task 3），在 silence_fd1() 内进行。
"""
from __future__ import annotations

import contextlib
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
