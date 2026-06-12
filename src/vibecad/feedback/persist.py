"""每步工程图落盘：Cowork 等客户端不向用户渲染 ImageContent（2026-06-12 真机实证），
返回里的 view_file 路径是用户看图的通道（AI 可 `open <path>` 弹图）。纯 stdlib。"""
from __future__ import annotations

import re
from pathlib import Path

_KEEP = 20


def _sanitize(name: str) -> str:
    return re.sub(r"[^\w.-]", "_", name)[:64] or "untitled"


def views_dir(doc_name: str) -> Path:
    from vibecad.runtime import paths  # noqa: PLC0415 - 懒加载，保 feedback 包导入轻

    return paths.vibecad_home() / "views" / _sanitize(doc_name)


def save_view(png: bytes, doc_name: str, tool: str) -> str:
    """写 <home>/views/<doc>/<NNN>-<tool>.png，滚动保留最近 _KEEP 张，返回绝对路径。"""
    d = views_dir(doc_name)
    d.mkdir(parents=True, exist_ok=True)
    nums = [int(m.group(1)) for p in d.glob("*.png")
            if (m := re.match(r"(\d{3})-", p.name))]
    path = d / f"{max(nums, default=0) + 1:03d}-{_sanitize(tool)}.png"
    path.write_bytes(png)
    for old in sorted(d.glob("*.png"))[:-_KEEP]:
        old.unlink(missing_ok=True)
    return str(path)
