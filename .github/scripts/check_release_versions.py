#!/usr/bin/env python3
"""发布前校验 tag 与五个分发版本面一致，并可绑定 exact Git checkout。纯 stdlib。"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

_GIT_OBJECT_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


class ReleaseIdentityError(ValueError):
    """The checked-out source is not the exact clean commit named by the release tag."""


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


def _git_output(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise ReleaseIdentityError(f"git {' '.join(args)} 失败")
    return result.stdout.strip()


def verify_release_identity(
    root: Path,
    tag: str,
    *,
    expected_ref: str,
    expected_object: str,
    require_clean: bool,
) -> None:
    """Bind the workflow event, tag, checkout, and optional clean-tree invariant."""

    tag_ref = f"refs/tags/{tag}"
    if expected_ref != tag_ref:
        raise ReleaseIdentityError(f"workflow ref 必须是 {tag_ref!r}（得到 {expected_ref!r}）")
    if _GIT_OBJECT_RE.fullmatch(expected_object) is None:
        raise ReleaseIdentityError("workflow object 必须是完整 Git object id")

    head_commit = _git_output(root, "rev-parse", "--verify", "HEAD^{commit}")
    tag_commit = _git_output(root, "rev-parse", "--verify", f"{tag_ref}^{{commit}}")
    event_commit = _git_output(root, "rev-parse", "--verify", f"{expected_object}^{{commit}}")
    if not head_commit == tag_commit == event_commit:
        raise ReleaseIdentityError("checkout HEAD、发布 tag 与 workflow object 未解析到同一 commit")
    if require_clean and _git_output(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ReleaseIdentityError("发布 checkout 必须是 clean worktree")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tag",
        nargs="?",
        default=os.environ.get("GITHUB_REF_NAME", ""),
        help="发布 tag（默认读取 GITHUB_REF_NAME）",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="仓库根目录")
    parser.add_argument("--expected-ref", help="workflow 的完整 tag ref（例如 refs/tags/v1.2.3）")
    parser.add_argument("--expected-object", help="workflow 提供的完整 Git object id")
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="拒绝 tracked 或 untracked worktree 漂移",
    )
    args = parser.parse_args(argv)

    if (args.expected_ref is None) != (args.expected_object is None):
        parser.error("--expected-ref 与 --expected-object 必须同时提供")
    if args.require_clean and args.expected_ref is None:
        parser.error("--require-clean 需要同时提供 workflow ref 与 object")

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

    if args.expected_ref is not None and args.expected_object is not None:
        try:
            verify_release_identity(
                args.root.resolve(),
                args.tag,
                expected_ref=args.expected_ref,
                expected_object=args.expected_object,
                require_clean=args.require_clean,
            )
        except (OSError, subprocess.SubprocessError, ReleaseIdentityError) as exc:
            print(f"::error::发布 Git 身份校验失败：{exc}", file=sys.stderr)
            return 1

    suffix = "，Git tag/commit/checkout 已绑定" if args.expected_ref is not None else ""
    print(f"发布版本校验通过：v{expected}（tag 与五个分发版本面{suffix}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
