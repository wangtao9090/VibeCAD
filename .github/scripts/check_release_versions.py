#!/usr/bin/env python3
"""发布前校验 tag 与六个分发版本面一致。纯 stdlib。"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path


def _source_version(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: list[str] = []
    for node in tree.body:
        value_node: ast.expr | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            value_node = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__version__"
        ):
            value_node = node.value
        if value_node is not None:
            value = ast.literal_eval(value_node)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{path} 的 __version__ 必须是非空字符串字面量")
            values.append(value)
    if len(values) != 1:
        raise ValueError(f"{path} 必须且只能定义一次顶层 __version__（实际 {len(values)} 次）")
    return values[0]


def collect_versions(root: Path, tag: str) -> dict[str, str]:
    if not tag.startswith("v") or len(tag) == 1:
        raise ValueError(f"发布 tag 必须是 v<version>（得到 {tag!r}）")
    with (root / "pyproject.toml").open("rb") as fh:
        pyproject_version = tomllib.load(fh)["project"]["version"]
    manifest_version = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["version"]
    source_version = _source_version(root / "src" / "vibecad" / "__init__.py")
    package_version = (
        ET.parse(root / "freecad" / "VibeCAD" / "package.xml").getroot().findtext("version")
    )
    with (root / "uv.lock").open("rb") as fh:
        locked = [
            package["version"]
            for package in tomllib.load(fh)["package"]
            if package.get("name") == "vibecad"
        ]
    if len(locked) != 1:
        raise ValueError(f"uv.lock 必须且只能包含一个 vibecad 包（实际 {len(locked)} 个）")
    versions = {
        "tag": tag[1:],
        "pyproject.toml": pyproject_version,
        "manifest.json": manifest_version,
        "vibecad.__version__": source_version,
        "freecad package.xml": package_version,
        "uv.lock": locked[0],
    }
    if not all(isinstance(value, str) and value for value in versions.values()):
        raise ValueError("各处分发版本都必须是非空字符串")
    return versions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tag",
        nargs="?",
        default=os.environ.get("GITHUB_REF_NAME", ""),
        help="发布 tag（默认读取 GITHUB_REF_NAME）",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="仓库根目录")
    args = parser.parse_args(argv)

    try:
        versions = collect_versions(args.root.resolve(), args.tag)
    except (
        OSError,
        SyntaxError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        ET.ParseError,
    ) as exc:
        print(f"::error::无法读取发布版本：{exc}", file=sys.stderr)
        return 2

    expected = versions["tag"]
    mismatches = {
        location: version for location, version in versions.items() if version != expected
    }
    if mismatches:
        details = "，".join(f"{location}={version}" for location, version in versions.items())
        print(f"::error::发布版本不一致：{details}", file=sys.stderr)
        return 1

    print(f"发布版本校验通过：v{expected}（tag 与六个分发版本面）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
